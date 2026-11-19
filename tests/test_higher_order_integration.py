"""
test_higher_order_integration.py: higher-order mods through the shared
get_modulator/get_demodulator registries (not the standalone module). Proves the
Tools/Encoder/plan chain can drive 8PSK/8QAM/16QAM/16APSK/32APSK like any mod.
Exit 0 = all passed.
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np

from dpmg.modulation.modulator import get_modulator
from dpmg.demodulation.demodulator import get_demodulator

checks = []


def chk(name, ok, detail=""):
    checks.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")


def main() -> int:
    sym_rate = 100_000.0
    sps = 8
    fs = sym_rate * sps
    mods = ["8PSK", "8QAM", "16QAM", "16APSK", "32APSK"]

    rng = np.random.default_rng(7)
    for m in mods:
        mod = get_modulator(m, sym_rate, sps)
        demod = get_demodulator(m, fs, sym_rate, sps)
        b = demod.bits_per_symbol
        bits = "".join(rng.integers(0, 2, 400 * b).astype(str))
        res = mod.modulate(bits)
        iq = np.asarray(res.iq_samples, dtype=complex)

        # 1. noiseless round-trip is exact (rectangular hold, no channel)
        out = demod.demodulate(iq)
        chk(f"{m} noiseless round-trip", out.bit_string[:len(bits)] == bits,
            f"({b} b/sym, {len(iq)} samp)")

        # 2. LLR sign agrees with the hard decision
        llr = np.asarray(out.soft_bits_llr[:len(bits)])
        hard_from_llr = "".join("1" if x > 0 else "0" for x in llr)
        agree = np.mean([a == c for a, c in zip(hard_from_llr, bits)])
        chk(f"{m} LLR sign vs hard", agree > 0.999, f"(agree {agree:.3f})")

        # 3. survives a mild AWGN channel (20 dB Es/N0)
        p = np.mean(np.abs(iq) ** 2)
        n = (rng.standard_normal(len(iq)) + 1j * rng.standard_normal(len(iq)))
        noisy = iq + np.sqrt(p / (2 * 10 ** (20 / 10))) * n
        o2 = demod.demodulate(noisy)
        ber = np.mean([a != c for a, c in zip(o2.bit_string[:len(bits)], bits)])
        chk(f"{m} BER @20dB Es/N0", ber < 1e-2, f"(ber {ber:.4f})")

    print(f"\nresult: {sum(checks)}/{len(checks)} checks passed")
    return 0 if all(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
