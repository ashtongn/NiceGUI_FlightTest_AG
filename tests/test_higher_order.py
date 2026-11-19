#!/usr/bin/env python3
"""Self-check for dpmg.modulation.higher_order.

For every constellation:
  (a) noiseless modulate: demodulate round-trips a 2000-bit payload, BER = 0
      (checked for both rectangular and RRC pulse shaping),
  (b) BER at 15 dB Es/N0 is small (loosened for the dense APSK rings),
  (c) soft_llr sign agrees with the hard decision on a clean signal.

Exit 0 iff every case passes.
"""

import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root
from dpmg.modulation.higher_order import get_higher_order_modulator, rrc_taps  # noqa: E402

RNG = np.random.default_rng(1234)
SYMBOL_RATE = 4800.0
SPS = 8
PAYLOAD_BITS = 2000
ESN0_DB = 15.0

# (name, ber threshold at 15 dB). Denser rings (APSK) get looser bounds.
CASES = [
    ("8PSK", 1e-2),
    ("8QAM", 1e-2),
    ("16QAM", 1e-2),
    ("16APSK", 3e-2),
    ("32APSK", 1e-1),
]


def random_bits(n: int) -> str:
    return "".join("1" if x else "0" for x in RNG.integers(0, 2, n))


def ber(ref: str, got: str) -> float:
    n = len(ref)
    got = got[:n]  # demod may return an extra zero-padded symbol
    if len(got) < n:
        return 1.0
    return sum(1 for a, b in zip(ref, got) if a != b) / n


def run_case(name: str, thresh: float) -> list:
    results = []

    # (a) noiseless round-trip, rectangular and RRC.
    payload = random_bits(PAYLOAD_BITS)
    for shaping, kw in (("rect", {}), ("rrc", {"use_rrc": True, "rolloff": 0.35})):
        mod = get_higher_order_modulator(name, SYMBOL_RATE, SPS, amplitude=1.0, **kw)
        iq = mod.modulate(payload).iq_samples
        b = ber(payload, mod.demodulate(iq))
        results.append((f"{name} noiseless round-trip ({shaping})", b == 0.0, f"BER={b:.2e}"))

    # (b) BER at 15 dB Es/N0 (rectangular; noise scaled for the sps averaging).
    mod = get_higher_order_modulator(name, SYMBOL_RATE, SPS, amplitude=1.0)
    payload = random_bits(PAYLOAD_BITS)
    iq = np.asarray(mod.modulate(payload).iq_samples)
    esn0 = 10.0 ** (ESN0_DB / 10.0)
    # Es = amplitude^2 = 1; averaging sps samples cuts noise var by sps, so set
    # per-sample complex noise variance N0*sps to hit the target per-symbol Es/N0.
    n0_sample = SPS / esn0
    sigma = math.sqrt(n0_sample / 2.0)
    noisy = iq + sigma * (RNG.standard_normal(len(iq)) + 1j * RNG.standard_normal(len(iq)))
    b = ber(payload, mod.demodulate(noisy))
    results.append((f"{name} BER @ {ESN0_DB:.0f} dB", b < thresh, f"BER={b:.2e} < {thresh:.0e}"))

    # (c) soft_llr sign agrees with hard decision on a clean signal.
    mod = get_higher_order_modulator(name, SYMBOL_RATE, SPS, amplitude=1.0)
    payload = random_bits(PAYLOAD_BITS)
    iq = mod.modulate(payload).iq_samples
    hard = mod.demodulate(iq)
    llr = mod.soft_llr(iq, n0=1.0)  # llr > 0 favours bit 1 per spec formula
    llr_bits = "".join("1" if v > 0 else "0" for v in llr)
    agree = llr_bits[: len(hard)] == hard
    results.append((f"{name} LLR sign vs hard decision", agree, "match" if agree else "mismatch"))

    return results


def main() -> int:
    # rrc_taps sanity: symmetric, unit energy.
    h = rrc_taps(0.35, SPS, 10)
    assert abs(np.sum(h ** 2) - 1.0) < 1e-9, "rrc taps not unit energy"
    assert np.allclose(h, h[::-1]), "rrc taps not symmetric"

    all_results = []
    for name, thresh in CASES:
        all_results.extend(run_case(name, thresh))

    print("=" * 60)
    n_pass = 0
    for label, ok, detail in all_results:
        tag = "PASS" if ok else "FAIL"
        if ok:
            n_pass += 1
        print(f"[{tag}] {label:<40} {detail}")
    print("=" * 60)
    total = len(all_results)
    print(f"{n_pass}/{total} checks passed")

    if n_pass == total:
        print("ALL PASS")
        return 0
    print("FAILURES PRESENT")
    return 1


if __name__ == "__main__":
    sys.exit(main())
