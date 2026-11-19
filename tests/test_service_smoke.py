"""
Smoke test for dpmg_service.py
==============================
Starts the real RadioService (simulate=True: MockBackend) in a background
thread with its own asyncio loop, then drives it from a plain blocking-socket
client the way the ground GUI's link layer does:

  1. discover the service by its ANNOUNCE broadcast (identity = serial)
  2. open a session (HELLO / HELLO_ACK)
  3. init the (mock) radio, set center, start RX: all absolute setpoints
  4. confirm STATUS heartbeat reflects STREAMING + last_cmd_id + uptime
  5. confirm CMD idempotency: replaying a cmd_id returns the stored ack and
     does not re-apply
  6. receive PSD_FRAMEs at ~4 Hz; decode one and check the
     reconstructed spectrum: bin count, freq axis vs commanded center/rate,
     dB values within int8 quantization tolerance of a ground-truth
     compute_psd of the service's own rolling buffer
  7. pure unit round-trip: quantize_psd: reconstruct <= 0.25 dB
     (reference decimated by group-max like the service) + a tone in a
     non-group-start bin survives decimation (anti-scalloping)
     max error (half LSB at 0.5 dB/LSB)
  8. DETECTION: the mock's continuous 4-FSK tones hold slots
     active; stop_rx force-closes the open bursts, so detections arrive with
     contiguous det_index, tone-accurate centers, positive duration and SNR
  9. STATUS.det_index matches the emitted count (gap detection aid)
 10. DETECTION_REQ ring replay: wipe the client cache, request
     [0, head) and verify every detection is re-delivered

Stdlib + numpy. Exit 0 = all passed.

    python3 test_service_smoke.py
"""

from __future__ import annotations

import asyncio
import socket
import sys
import threading
import time

import numpy as np

import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root
from dpmg.link import proto
from dpmg.link.proto import Cmd, DetectionReq, Hello, HostType, MsgType, RadioState

from dpmg.radio.dpmg_service import (PSD_MAX_BINS, PSD_SCALE_DB_X100, RadioService,
                          quantize_psd)

CMD_PORT = 47820
DISCOVERY_PORT = 47821
SERIAL = "G40-0001-SIM"


# service under test, in its own thread + loop

class ServiceThread:
    def __init__(self):
        self.svc = RadioService(
            serial=SERIAL, host_type=HostType.G40_RX, kind="mock",
            simulate=True, cmd_port=CMD_PORT, discovery_port=DISCOVERY_PORT,
            broadcast="127.0.0.1")
        self._ready = threading.Event()
        self._loop = None
        self._stop = None
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._stop = asyncio.Event()
        self._ready.set()
        loop.run_until_complete(self.svc.serve(self._stop))
        loop.close()

    def start(self):
        self._t.start()
        self._ready.wait(2.0)

    def stop(self):
        if self._loop and self._stop:
            self._loop.call_soon_threadsafe(self._stop.set)
        self._t.join(timeout=3.0)


# blocking-socket test client

class Client:
    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", DISCOVERY_PORT))
        self.sock.settimeout(0.2)
        self.seq = 0
        self.svc_addr = None
        self.serial = None
        self.last_status = None
        self.acks = {}                       # cmd_id: CmdAck
        self.next_cmd_id = 1
        self.psd_frames = []                 # (t_arrival, PsdFrame)
        self.dets = {}                       # det_index: Detection

    def _send(self, msg, addr):
        self.seq += 1
        self.sock.sendto(proto.encode(msg, self.seq), addr)

    def _pump(self, deadline):
        """Read whatever is waiting, dispatching to caches, until deadline.
        Rides out recv timeouts: telemetry gaps (PSD at 4 Hz > the 0.2 s
        socket timeout) must not end the collection window early."""
        while time.monotonic() < deadline:
            try:
                data, addr = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            try:
                mtype, _seq, msg = proto.decode(data)
            except proto.ProtoError:
                continue
            if mtype == MsgType.ANNOUNCE:
                self.serial = msg.serial
                self.svc_addr = ("127.0.0.1", msg.cmd_port)
            elif mtype == MsgType.HELLO_ACK:
                self._hello_ack = msg
            elif mtype == MsgType.STATUS:
                self.last_status = msg
            elif mtype == MsgType.PSD_FRAME:
                self.psd_frames.append((time.monotonic(), msg))
            elif mtype == MsgType.DETECTION:
                self.dets[msg.det_index] = msg
            elif mtype == MsgType.CMD_ACK:
                self.acks[msg.cmd_id] = msg

    def discover(self, timeout=6.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end and self.svc_addr is None:
            self._pump(min(end, time.monotonic() + 0.5))
        if self.svc_addr is None:
            raise TimeoutError("no ANNOUNCE received")
        return self.serial

    def hello(self, timeout=5.0):
        self._hello_ack = None
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self._send(Hello("GCS-X86-SIM"), self.svc_addr)
            self._pump(time.monotonic() + 0.3)
            if getattr(self, "_hello_ack", None) is not None:
                return self._hello_ack
        raise TimeoutError("HELLO never acked")

    def cmd(self, name, params=None, cmd_id=None, timeout=4.0):
        cid = cmd_id if cmd_id is not None else self.next_cmd_id
        if cmd_id is None:
            self.next_cmd_id += 1
        c = Cmd(cid, name, params or {})
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self._send(c, self.svc_addr)
            self._pump(time.monotonic() + 0.25)
            if cid in self.acks:
                return self.acks[cid]
        raise TimeoutError(f"cmd {cid} ({name}) never acked")

    def collect_status(self, seconds=2.5):
        end = time.monotonic() + seconds
        self._pump(end)
        return self.last_status


# test sequence

def main():
    checks = []

    def check(name, ok, detail=""):
        checks.append(bool(ok))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")

    svc = ServiceThread()
    svc.start()
    cli = Client()
    try:
        print("service smoke test (MockBackend)\n")

        serial = cli.discover()
        check("ANNOUNCE discovery", serial == SERIAL, f"(serial={serial})")

        ack = cli.hello()
        check("HELLO/HELLO_ACK session", ack.accepted == 1)

        init = cli.cmd("init", {
            "kind": "mock", "sample_rate": 1_000_000.0,
            "center_freq": 425_025_000.0, "rx_gain": 245.0, "rx_chunk": 4096})
        check("init applied", init.status == proto.CmdStatus.OK,
              f"(rate={init.result.get('achieved_rate')})")

        sc = cli.cmd("set_center", {"hz": 426_900_000.0})
        check("set_center applied",
              sc.status == proto.CmdStatus.OK and
              sc.result.get("center_hz") == 426_900_000.0)

        # TX on its own frequency: RX center stays, get_config reports both
        txc = cli.cmd("set_tx_center", {"hz": 427_500_000.0})
        gc = cli.cmd("get_config")
        check("set_tx_center moves only TX",
              txc.status == proto.CmdStatus.OK
              and txc.result.get("tx_center_hz") == 427_500_000.0
              and txc.result.get("center_hz") == 426_900_000.0
              and gc.result.get("tx_center_hz") == 427_500_000.0
              and gc.result.get("center_hz") == 426_900_000.0,
              f"(ack={txc.result} cfg tx={gc.result.get('tx_center_hz')})")

        # CLOCK_SYNC: the console can read this host's OS clock and ask for a
        # step; as a plain process the step is refused with a clear reason
        gs = cli.cmd("gps_status")
        check("gps_status reports host_time",
              abs(float(gs.result.get("host_time", 0)) - time.time()) < 5.0,
              f"(host_time={gs.result.get('host_time')})")
        stt = cli.cmd("set_time", {"utc": time.time()})
        check("set_time steps the clock or refuses for permission",
              (stt.status == proto.CmdStatus.OK and "stepped_s" in stt.result)
              or (stt.status == proto.CmdStatus.REJECTED
                  and "permission" in str(stt.result.get("err"))),
              f"(ack={stt.result})")
        bad = cli.cmd("set_time", {"utc": 1.0})
        check("set_time refuses a nonsense time",
              bad.status == proto.CmdStatus.REJECTED and "refusing" in str(bad.result.get("err")))

        rx = cli.cmd("start_rx")
        check("start_rx applied", rx.status == proto.CmdStatus.OK)

        st = cli.collect_status(2.5)
        streaming = st is not None and st.radio_state == RadioState.STREAMING
        check("STATUS heartbeat shows STREAMING",
              streaming, f"(state={st.radio_state if st else None})")
        check("STATUS.last_cmd_id tracks latest cmd",
              st is not None and st.last_cmd_id == cli.next_cmd_id - 1,
              f"(last_cmd_id={st.last_cmd_id if st else None})")
        check("STATUS.serial + uptime sane",
              st is not None and st.serial == SERIAL and st.uptime_s >= 0)
        check("STATUS task-running flag set",
              st is not None and bool(st.flags & proto.Status.FLAG_TASK_RUNNING))

        # PSD_FRAME telemetry
        nf = len(cli.psd_frames)
        check("PSD_FRAME rate in 2-5 Hz band", 6 <= nf <= 13,
              f"({nf} frames in 2.5 s pump)")

        ok_decode = False
        if nf:
            _, pf = cli.psd_frames[-1]
            bins = np.frombuffer(pf.bins, dtype=np.int8)
            db = pf.ref_db + bins.astype(float) * (pf.scale_db_x100 / 100.0)
            ok_decode = (0 < bins.size <= PSD_MAX_BINS
                         and pf.scale_db_x100 == PSD_SCALE_DB_X100)
            check("PSD_FRAME decodes (nbins, scale)", ok_decode,
                  f"(nbins={bins.size}, scale={pf.scale_db_x100})")
            check("PSD freq axis matches commanded center/rate",
                  abs(pf.center_hz - 426_900_000.0) < 5e3
                  and abs(pf.span_hz - 1_000_000.0) < 2e4,
                  f"(center={pf.center_hz/1e6:.4f} MHz, "
                  f"span={pf.span_hz/1e3:.0f} kHz)")

            # ground truth: the service recorder's own last float PSD,
            # quantized the same way. Consecutive Welch averages share ~94%
            # of the rolling buffer, so a one-frame skew stays within a dB.
            ref = svc.svc.psd.last_psd if svc.svc.psd else None
            if ref is not None and ok_decode:
                rf = quantize_psd(ref[0], ref[1])
                rdb = rf.ref_db + np.frombuffer(rf.bins, np.int8).astype(float) \
                    * (rf.scale_db_x100 / 100.0)
                if rdb.size == db.size:
                    diff = np.abs(rdb - db)
                    check("PSD dB matches service ground truth",
                          diff.mean() < 1.0 and diff.max() < 4.0,
                          f"(mean={diff.mean():.2f} dB, max={diff.max():.2f} dB)")
                else:
                    check("PSD dB matches service ground truth", False,
                          f"(size mismatch {rdb.size} vs {db.size})")
            else:
                check("PSD dB matches service ground truth", False,
                      "(no recorder last_psd)")

            # semantic: mock 4-FSK tones live at +/-0.06 and +/-0.18 fs;
            # tone band must sit well above the guard band noise floor.
            f_axis = np.linspace(pf.center_hz - pf.span_hz / 2,
                                 pf.center_hz + pf.span_hz / 2, bins.size)
            off = (f_axis - 426_900_000.0) / 1_000_000.0     # frac of fs
            tone = db[np.abs(off) < 0.20]
            guard = db[(np.abs(off) > 0.35) & (np.abs(off) < 0.48)]
            contrast = tone.mean() - guard.mean()
            check("PSD shows mock 4-FSK tone band over noise",
                  contrast > 5.0, f"(contrast={contrast:.1f} dB)")
        else:
            for name in ("PSD_FRAME decodes (nbins, scale)",
                         "PSD freq axis matches commanded center/rate",
                         "PSD dB matches service ground truth",
                         "PSD shows mock 4-FSK tone band over noise"):
                check(name, False, "(no frames)")

        # pure unit round-trip: quantize: reconstruct <= half-LSB error.
        # quantize_psd decimates to <= PSD_MAX_BINS by taking the max over
        # each group of `step` input bins (not a p[::step] subsample) so a
        # narrow tone that lands in a dropped bin is not scalloped away; the
        # freq axis keeps each group's start bin so it stays linear. The
        # reference here must be decimated the same way, otherwise the
        # comparison measures the noise spread inside a group (~7 dB at
        # sigma 2 dB), not the int8 quantization.
        f_u = np.linspace(400e6, 401e6, 3000)
        p_u = -110.0 + 25.0 * np.sin(np.linspace(0, 8, 3000)) \
            + np.random.default_rng(7).normal(0, 2.0, 3000)
        qf = quantize_psd(f_u, p_u)
        q_bins = np.frombuffer(qf.bins, np.int8).astype(float)
        q_db = qf.ref_db + q_bins * (qf.scale_db_x100 / 100.0)
        step = max(1, p_u.size // PSD_MAX_BINS)
        n_grp = (p_u.size // step) * step
        p_dec = p_u[:n_grp].reshape(-1, step).max(axis=1)[:q_bins.size]
        f_dec = f_u[:n_grp].reshape(-1, step)[:, 0][:q_bins.size]
        f_rec = np.linspace(qf.center_hz - qf.span_hz / 2,
                            qf.center_hz + qf.span_hz / 2, q_bins.size)
        err = np.abs(q_db - p_dec).max()
        check("quantize_psd round-trip <= half-LSB",
              q_bins.size <= PSD_MAX_BINS and err <= 0.2501
              and np.abs(f_rec - f_dec).max() < 1.0,
              f"(nbins={q_bins.size}, step={step}, max_err={err:.3f} dB)")

        # anti-scalloping: a narrow tone placed in a bin the old p[::step]
        # subsample would have dropped must survive decimation at full
        # height (within half an LSB), not vanish into the floor.
        if step > 1:
            p_t = np.full(3000, -120.0)
            tone_i = 5 * step + 1                 # not a group-start bin
            p_t[tone_i] = -70.0
            qt = quantize_psd(f_u, p_t)
            t_db = qt.ref_db + np.frombuffer(qt.bins, np.int8).astype(float) \
                * (qt.scale_db_x100 / 100.0)
            peak = float(t_db.max())
            check("quantize_psd keeps a tone in a dropped bin",
                  abs(peak - (-70.0)) <= 0.2501
                  and int(np.argmax(t_db)) == tone_i // step,
                  f"(peak={peak:.2f} dB at bin {int(np.argmax(t_db))}, "
                  f"tone in input bin {tone_i})")
        else:
            check("quantize_psd keeps a tone in a dropped bin", True,
                  "(step=1, no decimation)")

        # idempotency: replay the set_center cmd_id: identical stored ack,
        # no re-apply. Use the same cmd_id (2: hello has none; init=1, sc=2).
        replay = cli.cmd("set_center", {"hz": 999_000_000.0}, cmd_id=sc.cmd_id)
        check("CMD idempotent replay (stored ack, no re-apply)",
              replay.result.get("center_hz") == 426_900_000.0,
              f"(center stayed {replay.result.get('center_hz')})")

        stop = cli.cmd("stop_rx")
        check("stop_rx applied", stop.status == proto.CmdStatus.OK)

        # DETECTION plane
        # The mock's 4-FSK tones (+/-60 kHz, +/-180 kHz of center) hold their
        # slots active the whole run; stop_rx force-closed those bursts, so
        # the DETECTIONs are on the wire now. Pump to collect them + the next
        # STATUS (1 Hz) whose det_index must reflect the emission.
        cli._pump(time.monotonic() + 1.6)
        nd = len(cli.dets)
        idx = sorted(cli.dets)
        check("DETECTIONs emitted on burst close", nd >= 2, f"({nd} bursts)")
        check("det_index contiguous from 0", idx == list(range(nd)))

        tones = np.array([-180e3, -60e3, 60e3, 180e3])
        ok_tone = ok_shape = nd > 0
        for d in cli.dets.values():
            off = d.freq_hz - 426_900_000.0
            ok_tone &= bool(np.min(np.abs(tones - off)) <= 20e3)
            # bw_hz = the emitter's clustered slot span (DET-02 slot clustering):
            # a positive whole number of 20 kHz slots, one for a clean tone
            ok_shape &= (d.duration_ms > 0 and d.snr_db > 5.0 and d.bw_hz >= 20e3
                         and abs(d.bw_hz / 20e3 - round(d.bw_hz / 20e3)) < 1e-6)
        check("detection centers land on mock tone slots", ok_tone)
        check("detections carry duration/SNR/bw", ok_shape,
              "" if not nd else
              f"(snr~{np.mean([d.snr_db for d in cli.dets.values()]):.0f} dB, "
              f"dur~{np.mean([d.duration_ms for d in cli.dets.values()]):.0f} ms)")

        st2 = cli.last_status
        check("STATUS.det_index matches emitted count",
              st2 is not None and st2.det_index == nd,
              f"(det_index={st2.det_index if st2 else None}, emitted={nd})")

        # ring replay: forget everything, gap-fill from index 0
        cli.dets.clear()
        end = time.monotonic() + 3.0
        while len(cli.dets) < nd and time.monotonic() < end:
            cli._send(DetectionReq(0, max(nd, 1)), cli.svc_addr)
            cli._pump(time.monotonic() + 0.3)
        check("DETECTION_REQ ring replay recovers all",
              sorted(cli.dets) == list(range(nd)),
              f"({len(cli.dets)}/{nd} recovered)")

    finally:
        cli.sock.close()
        svc.stop()

    n = sum(checks)
    print(f"\nresult: {n}/{len(checks)} checks passed")
    return 0 if n == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
