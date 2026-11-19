#!/usr/bin/env python3
"""
test_uhd_stream_tx.py: UHD TX burst framing for a live stream, no hardware.

  1. a repeated waveform: every send is a whole burst (start and end), as before
  2. a live stream (tx_stream_mode): start of burst on the first send only, no
     end of burst between sends, and end_tx_burst() closes it once with a
     short zero packet; a second end_tx_burst() sends nothing (the queue and
     underflow counting are in test_uhd_tx_queue.py)
  3. the scheduler: with a TX source the backend is put in stream mode, and
     the burst is closed when the TX loop stops; without a source no stream mode

    python3 tests/test_uhd_stream_tx.py
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


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


class TxStream:
    def __init__(self):
        self.sent = []                                 # (n samples, start, end)

    def send(self, buf, md, timeout):
        self.sent.append((buf.shape[1], bool(md.start_of_burst), bool(md.end_of_burst)))
        return buf.shape[1]


def backend():
    from dpmg.radio import sdr_core as sdr
    b = sdr.UhdBackend.__new__(sdr.UhdBackend)
    b._uhd = types.SimpleNamespace(types=types.SimpleNamespace(TXMetadata=lambda: types.SimpleNamespace()))
    b._tx_stream = TxStream()
    b.health = sdr.SDRHealth() if hasattr(sdr, "SDRHealth") else types.SimpleNamespace(tx_frames=0, last_error="")
    return b


def case_bursts():
    print("[1] repeated waveform")
    b = backend()
    for _ in range(3):
        b.send(np.ones(100, dtype=np.complex64))
    check(all(s == (100, True, True) for s in b._tx_stream.sent), f"each send a whole burst {b._tx_stream.sent}")
    b.end_tx_burst()
    check(len(b._tx_stream.sent) == 3, "end_tx_burst does nothing when no burst is open")

    print("[2] live stream")
    b = backend()
    b.tx_stream_mode = True
    for _ in range(4):
        b.send(np.ones(100, dtype=np.complex64))
    b.end_tx_burst()                       # stream blocks go out on a sender thread
    flags = [(s, e) for _n, s, e in b._tx_stream.sent[:4]]
    check(flags == [(True, False), (False, False), (False, False), (False, False)],
          f"start of burst once, no end between sends {flags}")
    last = b._tx_stream.sent[-1]
    check(last[1] is False and last[2] is True and last[0] > 0, f"end_tx_burst closes the burst {last}")
    b.end_tx_burst()
    check(len(b._tx_stream.sent) == 5, "a second end_tx_burst sends nothing")


def case_scheduler():
    print("[3] scheduler")
    from dpmg.radio import sdr_core as sdr

    class B:
        def __init__(self):
            self.modes = []
            self.ends = 0
            self.health = types.SimpleNamespace(state=None, last_error="", tx_frames=0)

        def send(self, x):
            self.modes.append(getattr(self, "tx_stream_mode", None))
            time.sleep(0.005)

        def end_tx_burst(self):
            self.ends += 1

        def stop_tx(self):
            pass

    be = B()
    s = sdr.FullDuplexScheduler(be, tx_mode=sdr.TXMode.CONTINUOUS)
    s.set_tx_source(lambda: np.ones(64, dtype=np.complex64))
    s.start_tx()
    time.sleep(0.15)
    s.stop_tx()
    check(be.modes and all(m is True for m in be.modes), f"stream mode on for every send ({len(be.modes)} sends)")
    check(be.ends == 1 and be.tx_stream_mode is False, "burst closed once when the loop stopped, stream mode off")
    be2 = B()
    s2 = sdr.FullDuplexScheduler(be2, tx_mode=sdr.TXMode.CONTINUOUS)
    s2.set_tx_waveform(np.ones(64, dtype=np.complex64))
    s2.start_tx()
    time.sleep(0.1)
    s2.stop_tx()
    check(be2.modes and all(m is False for m in be2.modes), "a cached waveform never runs in stream mode")


def main():
    case_bursts()
    case_scheduler()
    print(f"\n{'PASS' if not FAILS else 'FAIL'}: {len(FAILS)} failure(s)")
    for f in FAILS:
        print("  -", f)
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
