"""
DSSS and CSS/LoRa modulate: demodulate. Noiseless must be BER 0; a second
pass adds noise above the signal (negative per-sample SNR) to show the spread
gain pulls it back. Deterministic. Exit 0 = all pass.
"""
from __future__ import annotations

import sys
import os
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dpmg.modulation.spread_spectrum import (
    DSSSModulator, DSSSDemodulator, LoRaCSSModulator, LoRaCSSDemodulator)

rng = np.random.default_rng(0x5EED)
checks = []


def chk(name, ok, detail=""):
    checks.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")


def ber(tx, rx):
    n = min(len(tx), len(rx))
    return 1.0 if n == 0 else sum(a != b for a, b in zip(tx[:n], rx[:n])) / n


def add_noise(iq, snr_db):
    x = np.asarray(iq, dtype=np.complex64)
    p = np.mean(np.abs(x) ** 2) + 1e-12
    npow = p / (10 ** (snr_db / 10.0))
    noise = (rng.standard_normal(len(x)) + 1j * rng.standard_normal(len(x)))
    noise *= np.sqrt(npow / 2.0)
    return x + noise.astype(np.complex64)


def main():
    print("DSSS + CSS/LoRa round-trip\n")

    # DSSS: SF=31, samples_per_bit = 31*4
    SF, spc = 31, 4
    bits = "".join(rng.integers(0, 2, 300).astype(str))
    m = DSSSModulator(1200, SF * spc, spread_factor=SF)
    iq = m.modulate(bits).iq_samples
    d = DSSSDemodulator(m.sample_rate, 1200, SF * spc, spread_factor=SF)
    rx = d.demodulate(np.asarray(iq)).bit_string
    chk("DSSS noiseless BER 0", ber(bits, rx) < 1e-9, f"(SF={SF})")
    # -6 dB per-sample SNR: raw BPSK fails, DSSS gain (~15 dB) recovers
    rxn = d.demodulate(add_noise(iq, -6.0)).bit_string
    chk("DSSS recovers at -6 dB SNR (spread gain)", ber(bits, rxn) < 0.02,
        f"(BER={ber(bits, rxn):.3f})")

    # CSS/LoRa: SF=7, BW=125k
    for sf in (7, 9):
        lbits = "".join(rng.integers(0, 2, sf * 40).astype(str))
        lm = LoRaCSSModulator(sf=sf, bw=125_000.0, os=1)
        liq = lm.modulate(lbits).iq_samples
        ld = LoRaCSSDemodulator(sf=sf, bw=125_000.0, os=1)
        lrx = ld.demodulate(np.asarray(liq)).bit_string
        chk(f"LoRa CSS SF{sf} noiseless BER 0", ber(lbits, lrx) < 1e-9)
        lrxn = ld.demodulate(add_noise(liq, -3.0)).bit_string
        chk(f"LoRa CSS SF{sf} recovers at -3 dB SNR", ber(lbits, lrxn) < 0.02,
            f"(BER={ber(lbits, lrxn):.3f})")

    print(f"\nresult: {sum(checks)}/{len(checks)} checks passed")
    return 0 if checks and all(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
