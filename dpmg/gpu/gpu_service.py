"""
gpu_service.py: GPU channelizer service (runs in the L4T + CuPy container).

Listens on shared memory for wideband IQ frames from the DPMG app, channelizes them
on the GPU (batched), and writes per-channel baseband back. Cross-container IPC via
named POSIX shared memory + named POSIX semaphores (work across container
boundaries, unlike multiprocessing primitives).

SHM layout (all names fixed, both sides agree):
  /dpmg_frame    : MAX_FRAME complex64: wideband input (app: service)
  /dpmg_result   : MAX_CH * MAX_OUT complex64: per-channel baseband (service: app)
  /dpmg_control  : a small header (see ControlHeader) describing fs, capture center,
                   n_channels, frame_len, and the per-channel (f_center, bw).
Semaphores:
  /dpmg_frame_ready  : app posts when a frame (and current control) is written
  /dpmg_result_ready : service posts when the result is written

Protocol per frame:
  app: write control (if changed) + frame: post frame_ready
  svc: wait frame_ready then read control+frame then channelize then write result then post result_ready
  app: wait result_ready: read result

Run inside the service container:
    python3 gpu_service.py
"""

from __future__ import annotations

import struct
import sys
import time
import numpy as np
import posix_ipc

sys.path.insert(0, "/app")
from gpu_channelizer2 import GPUChannelizerBatched, ChanSpec   # noqa: E402

# fixed capacity (sized for the worst case so shm regions are constant) ----
MAX_FRAME = 1048576         # max wideband samples per frame (cplx64)
MAX_CH = 30
MAX_OUT = 32768            # low rates give a small decim and a long output
DECIM_DEFAULT = 200

FRAME_BYTES = MAX_FRAME * 8
RESULT_BYTES = MAX_CH * MAX_OUT * 8
# control header: magic, version, seq, fs_in(double), capture_center(double),
# frame_len(uint32), fs_out(double), n_chan(uint32), then n_chan*(f_center double, bw double)
CONTROL_BYTES = 4096

NAME_FRAME = "/dpmg_frame"
NAME_RESULT = "/dpmg_result"
NAME_CONTROL = "/dpmg_control"
SEM_FRAME = "/dpmg_frame_ready"
SEM_RESULT = "/dpmg_result_ready"

_CTRL_FMT = "<4sIQ dd I d I"   # magic, ver, seq, fs_in, cap_center, frame_len, fs_out, n_chan
SHM_RHDR   = "/dpmg_rhdr"          # Result header (seq, out_len, n_chan)
RHDR_FMT   = "<QII"
RHDR_BYTES = struct.calcsize(RHDR_FMT)
# GPU energy detection: slot powers returned alongside channelized output
SHM_SLOTS  = "/dpmg_slots"         # (MAX_SLOTS,) float32 slot powers in dB
MAX_SLOTS  = 4096                  # >= 1536 slots for 30.72 MHz / 20 kHz
SLOTS_BYTES = MAX_SLOTS * 4
SHM_SHDR   = "/dpmg_shdr"          # slot header: seq, n_slots, slot_lo_hz(double), slot_bw(double)
SHDR_FMT   = "<QI dd"
SHDR_BYTES = struct.calcsize(SHDR_FMT)
_CTRL_HEAD = struct.calcsize(_CTRL_FMT)
_CHAN_FMT = "<dd"              # f_center, bw  per channel
_CHAN_SZ = struct.calcsize(_CHAN_FMT)


def _open_or_create_shm(name, size):
    try:
        m = posix_ipc.SharedMemory(name, flags=posix_ipc.O_CREAT, size=size)
    except posix_ipc.ExistentialError:
        m = posix_ipc.SharedMemory(name)
    return m


def _map(shm, size):
    import mmap
    return mmap.mmap(shm.fd, size)


def parse_control(buf: bytes):
    magic, ver, seq, fs_in, cap, frame_len, fs_out, n_chan = struct.unpack_from(_CTRL_FMT, buf, 0)
    if magic != b"DPMG":
        return None
    chans = []
    off = _CTRL_HEAD
    for _ in range(n_chan):
        fcen, bw = struct.unpack_from(_CHAN_FMT, buf, off)
        chans.append((fcen, bw))
        off += _CHAN_SZ
    return dict(seq=seq, fs_in=fs_in, capture_center=cap, frame_len=int(frame_len),
                fs_out=fs_out, n_chan=int(n_chan), chans=chans)


def _unlink_stale():
    """Remove any stale shm/semaphores from a previous run so counts start at 0."""
    for n in (NAME_FRAME, NAME_RESULT, NAME_CONTROL, SHM_SLOTS, SHM_SHDR):
        try:
            posix_ipc.unlink_shared_memory(n)
        except posix_ipc.ExistentialError:
            pass
    for n in (SEM_FRAME, SEM_RESULT):
        try:
            posix_ipc.unlink_semaphore(n)
        except posix_ipc.ExistentialError:
            pass


def main():
    print("[gpu_service] starting; clearing any stale IPC objects")
    _unlink_stale()
    print("[gpu_service] creating shm + semaphores")
    shm_f = _open_or_create_shm(NAME_FRAME, FRAME_BYTES)
    shm_r = _open_or_create_shm(NAME_RESULT, RESULT_BYTES)
    shm_c = _open_or_create_shm(NAME_CONTROL, CONTROL_BYTES)
    map_f = _map(shm_f, FRAME_BYTES)
    map_r = _map(shm_r, RESULT_BYTES)
    map_c = _map(shm_c, CONTROL_BYTES)

    sem_frame = posix_ipc.Semaphore(SEM_FRAME, flags=posix_ipc.O_CREAT, initial_value=0)
    sem_result = posix_ipc.Semaphore(SEM_RESULT, flags=posix_ipc.O_CREAT, initial_value=0)

    frame_np = np.ndarray((MAX_FRAME,), dtype=np.complex64, buffer=map_f)
    result_np = np.ndarray((MAX_CH, MAX_OUT), dtype=np.complex64, buffer=map_r)
    shm_h = posix_ipc.SharedMemory(SHM_RHDR, flags=posix_ipc.O_CREAT, size=RHDR_BYTES)
    map_h = _map(shm_h, RHDR_BYTES)
    shm_h.close_fd()
    # slot-power output (GPU energy detection)
    shm_s = _open_or_create_shm(SHM_SLOTS, SLOTS_BYTES)
    map_s = _map(shm_s, SLOTS_BYTES)
    slots_np = np.ndarray((MAX_SLOTS,), dtype=np.float32, buffer=map_s)
    shm_sh = posix_ipc.SharedMemory(SHM_SHDR, flags=posix_ipc.O_CREAT, size=SHDR_BYTES)
    map_sh = _map(shm_sh, SHDR_BYTES)
    shm_sh.close_fd()

    ch = None
    cur_sig = None   # (frame_len, fs_in, fs_out, capture_center, tuple(chans))
    print("[gpu_service] ready, waiting for frames")

    n_done = 0
    t_report = time.time()
    while True:
        sem_frame.acquire()                       # wait for a frame
        ctrl = parse_control(bytes(map_c[:CONTROL_BYTES]))
        if ctrl is None or ctrl["n_chan"] == 0:
            sem_result.release()
            continue

        sig = (ctrl["frame_len"], ctrl["fs_in"], ctrl["fs_out"],
               ctrl["capture_center"], tuple(ctrl["chans"]))
        if sig != cur_sig:
            # (re)build the channelizer for the new config
            ch = GPUChannelizerBatched(ctrl["fs_in"], ctrl["fs_out"], ctrl["frame_len"])
            for i, (fcen, bw) in enumerate(ctrl["chans"]):
                ch.set_channel(i, ChanSpec(fcen, bw), ctrl["capture_center"])
            ch.configure_slots(ctrl["capture_center"], slot_bw=20e3)   # GPU detector grid
            cur_sig = sig
            print(f"[gpu_service] reconfigured: {ctrl['n_chan']} channels, "
                  f"frame_len={ctrl['frame_len']}, fs_out={ctrl['fs_out']/1e3:g}kHz, "
                  f"slots={ch._n_slots}")

        n = ctrl["frame_len"]
        frame = np.array(frame_np[:n])          # copy: never process a live shm view
        out = ch.process(frame, ctrl["capture_center"], return_host=True)
        w_written = 0
        for i in range(ctrl["n_chan"]):
            bb = out.get(i)
            if bb is not None:
                w = min(len(bb), MAX_OUT)
                result_np[i, :w] = bb[:w]
                if w > w_written:
                    w_written = w
        # write GPU-computed slot powers (energy detection) alongside the result
        _sdb = ch.last_slot_powers_db()
        if _sdb is not None:
            _ns = min(len(_sdb), MAX_SLOTS)
            slots_np[:_ns] = _sdb[:_ns].astype(np.float32)
            struct.pack_into(SHDR_FMT, map_sh, 0, int(ctrl["seq"]), int(_ns),
                             float(getattr(ch, "_slot_lo_hz", 0.0)),
                             float(getattr(ch, "_slot_bw", 20e3)))
        else:
            struct.pack_into(SHDR_FMT, map_sh, 0, int(ctrl["seq"]), 0, 0.0, 20e3)
        struct.pack_into(RHDR_FMT, map_h, 0,
                         int(ctrl["seq"]), int(w_written), int(ctrl["n_chan"]))
        sem_result.release()

        n_done += 1
        if time.time() - t_report > 2.0:
            print(f"[gpu_service] processed {n_done} frames")
            t_report = time.time()


if __name__ == "__main__":
    main()
