#!/usr/bin/env python3
"""
test_uhd_tx_queue.py: UHD TX queue for a live stream and underflow counting,
no hardware.

  1. async reports: UHD underflow reports (plain and in packet) add to the
     backend's health underruns and tx_underflows, sequence errors are kept
     apart, a burst ack counts as nothing
  2. a slow block: the scheduler feeds a fake radio that plays samples in real
     time and holds only a few ms once a send returns; every fifth block takes
     longer to build than the radio holds. The radio must never run dry.
  3. framing: start of burst once, no end between blocks, every queued block
     reaches the radio before end_tx_burst closes the burst, and a repeated
     waveform afterwards goes out as whole bursts again
  4. the queue holds at most tx_queue_blocks blocks
  5. an API without async reports: polling stops once and says why

    python3 tests/test_uhd_tx_queue.py
"""
import sys
import threading
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np                                     # noqa: E402

FAILS = []

EV = types.SimpleNamespace(burst_ack=1, underflow=2, seq_error=4, time_error=8,
                           underflow_in_packet=16, seq_error_in_packet=32, user_payload=64)


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


class FakeRadio:
    """TX streamer stand in. Plays samples at fs; send() waits until no more
    than slack_s is left queued, as UHD does once its buffers are full, and
    counts an underflow when a block arrives after the queued samples ran out."""

    def __init__(self, fs=100e3, slack_s=0.005, realtime=True):
        self.fs, self.slack, self.realtime = fs, slack_s, realtime
        self.sent = []                                 # (n, start, end)
        self.events = []
        self.play_end = None
        self.dry = 0
        self.lock = threading.Lock()

    def send(self, buf, md, timeout):
        n = buf.shape[1]
        if self.realtime:
            now = time.monotonic()
            if md.start_of_burst or self.play_end is None:
                self.play_end = now
            elif now > self.play_end + 0.002:
                self.dry += 1
                self.events.append(EV.underflow)
                self.play_end = now
            wait = self.play_end - now - self.slack
            if wait > 0:
                time.sleep(wait)
            self.play_end += n / self.fs
            if md.end_of_burst:
                self.play_end = None
        with self.lock:
            self.sent.append((n, bool(md.start_of_burst), bool(md.end_of_burst)))
        return n

    def recv_async_msg(self, md, timeout):
        with self.lock:
            if not self.events:
                return False
            md.event_code = self.events.pop(0)
        return True


def backend(radio, qblocks=3):
    from dpmg.radio import sdr_core as sdr
    b = sdr.UhdBackend.__new__(sdr.UhdBackend)
    b._uhd = types.SimpleNamespace(types=types.SimpleNamespace(
        TXMetadata=lambda: types.SimpleNamespace(),
        TXAsyncMetadata=lambda: types.SimpleNamespace(event_code=0),
        TXMetadataEventCode=EV))
    b._tx_stream = radio
    b.health = sdr.StreamHealth()
    b.tx_queue_blocks = qblocks
    return b


def case_reports():
    print("[1] async reports")
    r = FakeRadio(realtime=False)
    b = backend(r)
    r.events = [EV.underflow, EV.burst_ack, EV.underflow_in_packet, EV.seq_error]
    b.send(np.ones(100, dtype=np.complex64))
    check(b.health.underruns == 2, f"two underflow reports counted in health ({b.health.underruns})")
    check(getattr(b, "tx_underflows", 0) == 2, f"tx_underflows 2 ({getattr(b, 'tx_underflows', 0)})")
    check(getattr(b, "tx_seq_errors", 0) == 1, "sequence error kept apart")
    b.tx_stream_mode = True
    r.events = [EV.underflow]
    for _ in range(3):
        b.send(np.ones(100, dtype=np.complex64))
    b.end_tx_burst()
    check(b.health.underruns == 3, f"a report during a stream is counted by the end ({b.health.underruns})")


def case_slow_block():
    print("[2] slow block")
    from dpmg.radio import sdr_core as sdr
    fs, blk = 100e3, 2000                              # 20 ms blocks
    r = FakeRadio(fs=fs, slack_s=0.005)
    b = backend(r, qblocks=3)
    n = [0]

    def src():
        n[0] += 1
        time.sleep(0.040 if n[0] % 5 == 0 else 0.001)  # every fifth block 40 ms
        return np.ones(blk, dtype=np.complex64)

    s = sdr.FullDuplexScheduler(b, tx_mode=sdr.TXMode.CONTINUOUS)
    s.set_tx_source(src)
    s.start_tx()
    time.sleep(1.5)
    s.stop_tx()
    blocks = sum(1 for x in r.sent if x[0] == blk)
    check(blocks >= 50, f"stream kept the radio fed ({blocks} blocks in 1.5 s, ~75 is real time)")
    check(r.dry == 0, f"radio never ran dry ({r.dry} underflows, {n[0] // 5} slow blocks)")
    check(b.health.underruns == r.dry, "health underruns match what the radio reported")
    check(r.sent and r.sent[-1][2] is True, "burst closed when TX stopped")


def case_framing():
    print("[3] framing")
    r = FakeRadio(fs=1e6, slack_s=0.001)
    b = backend(r, qblocks=2)
    b.tx_stream_mode = True
    for k in range(6):
        b.send(np.full(1000 + k, 1, dtype=np.complex64))
    b.tx_stream_mode = False
    b.end_tx_burst()
    flags = [(s, e) for _n, s, e in r.sent]
    check(flags == [(True, False)] + [(False, False)] * 5 + [(False, True)],
          f"start once, no end between blocks, end at the close {flags}")
    check([x[0] for x in r.sent[:6]] == [1000 + k for k in range(6)], "every queued block sent, in order")
    b.end_tx_burst()
    check(len(r.sent) == 7, "a second end_tx_burst sends nothing")
    for _ in range(2):
        b.send(np.ones(50, dtype=np.complex64))
    check(r.sent[-2:] == [(50, True, True), (50, True, True)], "a repeated waveform afterwards: whole bursts")


def case_bound():
    print("[4] queue bound")
    gate = threading.Event()

    class Stuck(FakeRadio):
        def send(self, buf, md, timeout):
            gate.wait(5.0)
            return super().send(buf, md, timeout)

    r = Stuck(realtime=False)
    b = backend(r, qblocks=3)
    b.tx_stream_mode = True
    done = []

    def feed():
        for _ in range(8):
            b.send(np.ones(10, dtype=np.complex64))
            done.append(1)

    t = threading.Thread(target=feed, daemon=True)
    t.start()
    time.sleep(0.3)
    held = len(done)
    gate.set()
    t.join(5.0)
    b.end_tx_burst()
    # one block in the sender's hands plus three queued
    check(held == 4, f"source held back once the queue is full ({held} sends returned, 4 expected)")
    check(sum(1 for x in r.sent if x[0] == 10) == 8, "all eight blocks sent once released")


def case_no_async():
    print("[5] no async reports")
    r = FakeRadio(realtime=False)
    b = backend(r)
    del b._uhd.types.TXAsyncMetadata
    b.send(np.ones(10, dtype=np.complex64))
    b.send(np.ones(10, dtype=np.complex64))
    check(getattr(b, "_tx_async_off", False) and "TXAsyncMetadata" in (getattr(b, "_tx_async_err", "") or ""),
          f"polling off with the reason kept ({getattr(b, '_tx_async_err', None)})")
    check(len(r.sent) == 2, "sends carry on without async reports")


def main():
    case_reports()
    case_slow_block()
    case_framing()
    case_bound()
    case_no_async()
    print(f"\n{'PASS' if not FAILS else 'FAIL'}: {len(FAILS)} failure(s)")
    for f in FAILS:
        print("  -", f)
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
