"""
channelizer_client.py: client side of the GPU channelizer service, for the DPMG app.

Wraps the shm + named-semaphore IPC (measured end to end at ~63% of the
real-time budget) in a class. The app feeds wideband IQ frames; the GPU
service returns per-channel baseband IQ. Runs in the DPMG app container (python:3.11);
needs `posix_ipc` (pip install posix_ipc) and the app container started with --ipc=host
so it shares the host IPC namespace with the GPU service container.

Two pieces:
  - FrameAccumulator: collects the radio's small RX blocks (~1018 samples) into the
    large channelizer frames (e.g. 131072) the GPU service expects.
  - ChannelizerClient: the shm/semaphore transport + control protocol.

posix_ipc notes:
  - Semaphore.acquire() returns None on success and raises posix_ipc.BusyError on
    timeout. Never write `if not sem.acquire(...)`. Use try/except BusyError.
  - The service unlinks stale IPC on startup so counts begin at 0; the client drains
    any stray result posts before its first real exchange.
"""

from __future__ import annotations

import mmap
import struct
from collections import deque
from typing import Optional
import time
import struct

import numpy as np

try:
    import posix_ipc
    _HAVE_IPC = True
except Exception:
    posix_ipc = None
    _HAVE_IPC = False

# must match gpu_service.py exactly
MAX_FRAME = 1048576
MAX_CH = 30
MAX_OUT = 32768  # must match gpu_service.py (shared shm size)
FRAME_BYTES = MAX_FRAME * 8
RESULT_BYTES = MAX_CH * MAX_OUT * 8
SHM_RHDR   = "/dpmg_rhdr"
RHDR_FMT   = "<QII"
RHDR_BYTES = struct.calcsize(RHDR_FMT)
CONTROL_BYTES = 4096
# GPU energy detection: slot powers from the service
SHM_SLOTS  = "/dpmg_slots"
MAX_SLOTS  = 4096
SLOTS_BYTES = MAX_SLOTS * 4
SHM_SHDR   = "/dpmg_shdr"
SHDR_FMT   = "<QI dd"
SHDR_BYTES = struct.calcsize(SHDR_FMT)

NAME_FRAME = "/dpmg_frame"
NAME_RESULT = "/dpmg_result"
NAME_CONTROL = "/dpmg_control"
SEM_FRAME = "/dpmg_frame_ready"
SEM_RESULT = "/dpmg_result_ready"

_CTRL_FMT = "<4sIQ dd I d I"
_CTRL_HEAD = struct.calcsize(_CTRL_FMT)
_CHAN_FMT = "<dd"
_CHAN_SZ = struct.calcsize(_CHAN_FMT)


class FrameAccumulator:
    """Collects small RX blocks into fixed-size channelizer frames.

    The X40 delivers ~1018-sample blocks; the channelizer wants e.g. 131072. Push
    blocks in; when enough have accumulated, pop() returns one full frame (and keeps
    any leftover for the next frame).
    """

    def __init__(self, frame_len: int):
        self.frame_len = int(frame_len)
        self._buf: deque = deque()
        self._have = 0
        self._last_ts = None
        self._last_n = 0
        self.gaps = 0

    def push(self, samples: np.ndarray, ts=None) -> None:
        s = np.asarray(samples, dtype=np.complex64)
        if not s.size:
            return
        # Contiguity gate. rf_timestamp is a sample counter; the next batch must start at
        # last_ts + last_n. If it doesn't, samples were lost between drains: concatenating
        # across the hole produces a spliced frame that decodes as garbage with no error
        # anywhere. Discard the partial frame instead: those samples are lost either
        # way, and a spliced frame is silent corruption.
        if ts is not None and self._last_ts is not None:
            if ts != self._last_ts + self._last_n:
                self.gaps += 1
                self._buf.clear()
                self._have = 0
        if ts is not None:
            self._last_ts = ts
            self._last_n = s.size
        self._buf.append(s)
        self._have += s.size

    def ready(self) -> bool:
        return self._have >= self.frame_len

    def pop(self) -> Optional[np.ndarray]:
        if self._have < self.frame_len:
            return None
        # concatenate enough blocks to cover one frame
        chunks = []
        need = self.frame_len
        while need > 0:
            blk = self._buf[0]
            if blk.size <= need:
                chunks.append(blk)
                need -= blk.size
                self._buf.popleft()
                self._have -= blk.size
            else:
                chunks.append(blk[:need])
                self._buf[0] = blk[need:]
                self._have -= need
                need = 0
        return np.concatenate(chunks)

    def reset(self) -> None:
        self._buf.clear()
        self._have = 0


class ChannelizerClient:
    """Talks to the GPU channelizer service over shared memory + named semaphores."""

    def __init__(self, frame_len: int = 131072, fs_out: float = 250_000.0):
        if not _HAVE_IPC:
            raise RuntimeError("posix_ipc not available -- pip install posix_ipc")
        self.frame_len = int(frame_len)
        self.fs_out = float(fs_out)
        self._seq = 0
        self.desync = 0
        self._chans: list[tuple[float, float]] = []   # (f_center, bw)
        self._connected = False
        self._open()

    def _open(self) -> None:
        # open (do not create with O_EXCL): the service creates them. If the service
        # isn't up yet, O_CREAT lets us attach once it is.
        self._shm_f = posix_ipc.SharedMemory(NAME_FRAME, flags=posix_ipc.O_CREAT, size=FRAME_BYTES)
        self._shm_r = posix_ipc.SharedMemory(NAME_RESULT, flags=posix_ipc.O_CREAT, size=RESULT_BYTES)
        self._shm_c = posix_ipc.SharedMemory(NAME_CONTROL, flags=posix_ipc.O_CREAT, size=CONTROL_BYTES)
        self._map_f = mmap.mmap(self._shm_f.fd, FRAME_BYTES)
        self._map_r = mmap.mmap(self._shm_r.fd, RESULT_BYTES)
        self._map_c = mmap.mmap(self._shm_c.fd, CONTROL_BYTES)
        self._sem_frame = posix_ipc.Semaphore(SEM_FRAME, flags=posix_ipc.O_CREAT, initial_value=0)
        self._sem_result = posix_ipc.Semaphore(SEM_RESULT, flags=posix_ipc.O_CREAT, initial_value=0)
        self._frame_np = np.ndarray((MAX_FRAME,), dtype=np.complex64, buffer=self._map_f)
        self._result_np = np.ndarray((MAX_CH, MAX_OUT), dtype=np.complex64, buffer=self._map_r)
        self._shm_h = posix_ipc.SharedMemory(SHM_RHDR, flags=posix_ipc.O_CREAT, size=RHDR_BYTES)
        self._map_h = mmap.mmap(self._shm_h.fd, RHDR_BYTES)
        self._shm_h.close_fd()
        # slot-power SHM (GPU energy detection)
        self._shm_s = posix_ipc.SharedMemory(SHM_SLOTS, flags=posix_ipc.O_CREAT, size=SLOTS_BYTES)
        self._map_s = mmap.mmap(self._shm_s.fd, SLOTS_BYTES)
        self._slots_np = np.ndarray((MAX_SLOTS,), dtype=np.float32, buffer=self._map_s)
        self._shm_sh = posix_ipc.SharedMemory(SHM_SHDR, flags=posix_ipc.O_CREAT, size=SHDR_BYTES)
        self._map_sh = mmap.mmap(self._shm_sh.fd, SHDR_BYTES)
        self._shm_sh.close_fd()
        self._last_slots = None
        self._last_slot_meta = None
        self._slot_age = 0
        self._drain_results()

    def _drain_results(self) -> None:
        while True:
            try:
                self._sem_result.acquire(timeout=0)
            except posix_ipc.BusyError:
                break

    def set_channels(self, channels: list[tuple[float, float]]) -> None:
        """channels: list of (f_center_hz, bw_hz). Up to MAX_CH."""
        self._chans = list(channels)[:MAX_CH]

    @property
    def decim(self) -> int:
        return max(1, int(round(self.frame_len / (self.frame_len // max(1, int(round(50e6 / self.fs_out)))))))

    def process(self, wideband: np.ndarray, fs_in: float, capture_center: float,
                timeout_s: float = 1.0) -> Optional[dict[int, np.ndarray]]:
        """Send one wideband frame, get back {ch_idx: baseband IQ}. None on timeout."""
        if not self._chans:
            return {}
        n = min(len(wideband), self.frame_len)
        self._seq += 1
        # write control header
        struct.pack_into(_CTRL_FMT, self._map_c, 0, b"DPMG", 1, self._seq,
                         float(fs_in), float(capture_center), n, float(self.fs_out),
                         len(self._chans))
        off = _CTRL_HEAD
        for (fcen, bw) in self._chans:
            struct.pack_into(_CHAN_FMT, self._map_c, off, float(fcen), float(bw))
            off += _CHAN_SZ
        # write frame + signal
        self._frame_np[:n] = wideband[:n].astype(np.complex64)
        self._sem_frame.release()
        _deadline = time.time() + float(timeout_s)
        _stale = 0
        while True:
            _remain = _deadline - time.time()
            if _remain <= 0:
                self.desync += _stale
                return None
            try:
                self._sem_result.acquire(timeout=_remain)
            except posix_ipc.BusyError:
                self.desync += _stale
                return None
            got_seq, out_len, n_chan = struct.unpack_from(RHDR_FMT, self._map_h, 0)
            if got_seq == self._seq:
                # read GPU slot powers. Keep the most recent valid slots even if the slot
                # header seq lags the frame seq by a little: a burst spans many frames so
                # one-frame-old detection is fine. Track the age (in seqs) so the worker/GUI
                # can surface staleness rather than the client silently skipping detection.
                try:
                    s_seq, n_slots, slot_lo_hz, slot_bw = struct.unpack_from(SHDR_FMT, self._map_sh, 0)
                    if n_slots > 0:
                        self._last_slots = np.array(self._slots_np[:n_slots])
                        self._last_slot_meta = (float(slot_lo_hz), float(slot_bw))
                        self._slot_age = int(self._seq - s_seq)   # 0 = fresh, >0 = frames behind
                    # if n_slots==0 we keep the previous _last_slots (do not null) and age grows
                    elif self._last_slots is not None:
                        self._slot_age = getattr(self, "_slot_age", 0) + 1
                except Exception:
                    pass
                break
            _stale += 1
        if _stale:
            self.desync += _stale
            print(f"[SEQ] discarded {_stale} stale result(s) before seq={self._seq}", flush=True)
        out_len = min(int(out_len), MAX_OUT)
        out = {}
        for i in range(min(len(self._chans), int(n_chan))):
            out[i] = self._result_np[i, :out_len].copy()
        return out

    def last_slot_powers(self):
        """Return (slot_db_array, (slot_lo_hz, slot_bw)) from the last process() call,
        or (None, None). slot_lo_hz is the offset of slot 0's low edge from capture center."""
        return self._last_slots, self._last_slot_meta

    def slot_age(self) -> int:
        """How many frames stale the current slot powers are. 0 = fresh this frame.
        Exposed so the GUI can show detection staleness instead of the client silently skipping."""
        return int(getattr(self, "_slot_age", 0))

    def close(self) -> None:
        for m in (self._map_f, self._map_r, self._map_c, self._map_h,
                  getattr(self, "_map_s", None), getattr(self, "_map_sh", None)):
            if m is None:
                continue
            try:
                m.close()
            except Exception:
                pass


# standalone smoke test against a running service
def _smoke():
    if not _HAVE_IPC:
        print("posix_ipc not available")
        return
    fs_in, fc, frame_len, fs_out = 50e6, 425e6, 131072, 250e3
    offs = [-10e6, 5e6, 15e6]
    t = np.arange(frame_len) / fs_in
    wb = (np.random.randn(frame_len) + 1j*np.random.randn(frame_len)).astype(np.complex64) * 0.02
    for fo in offs:
        wb += np.exp(2j*np.pi*fo*t).astype(np.complex64)

    cli = ChannelizerClient(frame_len=frame_len, fs_out=fs_out)
    cli.set_channels([(fc + fo, 100e3) for fo in offs])
    out = cli.process(wb, fs_in, fc, timeout_s=60)
    if out is None:
        print("SMOKE: no response from service (is it running with --ipc=host?)")
        return
    ok = True
    decim = int(round(fs_in / fs_out)); out_len = frame_len // decim
    for i, fo in enumerate(offs):
        bb = out[i]
        sp = np.fft.fftshift(np.fft.fft(bb * np.hanning(len(bb))))
        fax = np.fft.fftshift(np.fft.fftfreq(len(bb), 1.0/fs_out))
        peak = fax[np.argmax(np.abs(sp)**2)]
        good = abs(peak) < 3*(fs_out/len(bb))
        ok = ok and good
        print(f"  ch{i} off={fo/1e6:+g}MHz peak@{peak/1e3:+.2f}kHz {'OK' if good else 'FAIL'}")
    print("SMOKE:", "PASS" if ok else "FAIL")


if __name__ == "__main__":
    _smoke()
