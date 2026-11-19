#!/usr/bin/env python3
"""
High-rate consumer trims (61.44 MSps detector and PSD load): offline
=====================================================================
  1. PSDRecorder.push_frame keeps the same rolling buffer and PSD when it only
     copies the tail of a long frame
  2. _det_step above 40 MSps: detector sees the newest DET_MAX_SAMPLES_HIGH_RATE
     samples, clip check every CLIP_STRIDE_HIGH_RATE-th sample
  3. _det_step at 30.72 MSps: whole frame for both (unchanged)

    python3 tests/test_high_rate_consumers.py      (or pytest)
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dpmg.dsp import psd_trace as pt          # noqa: E402
from dpmg.radio import dpmg_service as ds       # noqa: E402

_checks = []


def check(ok, what):
    _checks.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + what)


def old_push(buf, iq, cap):
    return np.concatenate([buf, iq])[-cap:]


def main():
    rng = np.random.default_rng(7)
    rec = pt.PSDRecorder(nfft=4096, window="hann", buffer_samples=65536)
    ref = np.zeros(0, np.complex64)
    same = True
    for n in (1000, 30000, 2_084_864, 40000, 65536, 70000):
        iq = (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64)
        rec.push_frame(iq, 61.44e6, 2.4e9, rf_timestamp=0)
        ref = old_push(ref, iq, 65536)
        same &= rec._buf.tobytes() == ref.tobytes()
    check(same, "PSD rolling buffer identical to concatenate-then-cap for 6 frame sizes")

    seen = {}

    class Det:
        def process(self, s):
            seen["det_len"] = len(s); seen["det_last"] = s[-1]
            return []

    class Trk:
        def update(self, dets, t):
            return []

    svc = SimpleNamespace(det=Det(), tracker=Trk())
    n = 2_084_864
    s = np.zeros(n, np.complex64)
    s[-1] = 0.5 + 0.5j
    s[::ds.CLIP_STRIDE_HIGH_RATE][:1000] = 0.99      # clipped samples on the stride grid
    _, clip_hi = ds.RadioService._det_step(svc, SimpleNamespace(samples=s, sample_rate=61.44e6))
    check(seen["det_len"] == ds.DET_MAX_SAMPLES_HIGH_RATE and seen["det_last"] == s[-1],
          f"61.44 MSps: detector gets the newest {ds.DET_MAX_SAMPLES_HIGH_RATE} samples ({seen['det_len']})")
    want = 1000 / len(s[::ds.CLIP_STRIDE_HIGH_RATE])
    check(abs(clip_hi - want) < 1e-12, f"61.44 MSps: clip fraction over the stride ({clip_hi:.6f})")
    _, clip_lo = ds.RadioService._det_step(svc, SimpleNamespace(samples=s, sample_rate=30.72e6))
    check(seen["det_len"] == n, f"30.72 MSps: detector gets the whole frame ({seen['det_len']})")
    check(abs(clip_lo - 1000 / n) < 1e-12, f"30.72 MSps: clip fraction over every sample ({clip_lo:.6f})")
    n_ok = sum(_checks)
    print(f"{n_ok}/{len(_checks)} checks passed")
    return 0 if n_ok == len(_checks) else 1


def test_high_rate_consumers():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
