#!/usr/bin/env python3
"""
Channelizer accumulator backlog cap: offline
=============================================================
The worker moves its bounded input queue into the frame accumulator every
pass. With no consumer (or one slower than the RX rate) the accumulator must
stay under backlog_frames frames and count what it drops; with no overload
nothing is dropped.

    python3 tests/test_channelizer_backlog.py      (or pytest)
"""
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dpmg.dsp import channelizer_worker as W   # noqa: E402

_checks = []


def check(ok, what):
    _checks.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + what)


def run_feed(n_batches, batch, frame_len=65536, sleep_s=0.0):
    w = W.ChannelizerWorker(frame_len=frame_len)
    w._client = None                      # no GPU: nothing is ever popped
    th = threading.Thread(target=w._run, daemon=True)
    th.start()
    peak = 0
    ts = 0
    for _ in range(n_batches):
        w.feed(SimpleNamespace(samples=np.zeros(batch, np.complex64), sample_rate=61.44e6,
                               center_freq=2.4e9, rf_timestamp=ts))
        ts += batch
        peak = max(peak, w._acc._have)
        if sleep_s:
            time.sleep(sleep_s)
    time.sleep(0.05)
    w._stop.set()
    th.join(2)
    return w, peak


def main():
    fl = 65536
    w, peak = run_feed(400, 32768, fl, sleep_s=0.0005)
    cap = fl * w.backlog_frames
    check(peak <= cap + 64 * 32768, f"accumulator bounded: peak {peak} <= cap {cap} + one queue")
    check(w._acc._have <= cap, f"accumulator under the cap at the end ({w._acc._have})")
    check(w._dropped > 0 and w.backlog_drops > 0,
          f"overload counted: dropped {w._dropped} frames in {w.backlog_drops} drops")
    w2, peak2 = run_feed(10, 32768, fl)
    check(w2._dropped == 0 and w2.backlog_drops == 0 and w2._acc._have == 10 * 32768,
          f"below the cap nothing is dropped (have {w2._acc._have})")
    n_ok = sum(_checks)
    print(f"{n_ok}/{len(_checks)} checks passed")
    return 0 if n_ok == len(_checks) else 1


def test_channelizer_backlog():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
