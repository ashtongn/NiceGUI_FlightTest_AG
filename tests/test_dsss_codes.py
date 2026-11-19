"""
DSSS spreading-code library + BPSK/QPSK spreading round-trips.

Covers: code families (seed / m-sequence / Gold / custom) resolve to the right
length and despread cleanly; BPSK-DSSS and QPSK-DSSS both recover noiselessly
and below the noise floor (processing gain). QPSK carries a 4-fold rotation
ambiguity, resolved at the modem/frame layer (find_and_decode over rotations);
this test scores best-over-rotations at the raw-bit level. Deterministic.
Exit 0 = all pass.
"""
from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np

from dpmg.modulation.spreading_codes import resolve_code, code_len
from dpmg.modulation.spread_spectrum import DSSSModulator, DSSSDemodulator

checks = []


def chk(name, ok, detail=""):
    checks.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")


def _ber_bpsk(tx, rx):
    n = min(len(tx), len(rx))
    e = sum(a != b for a, b in zip(tx[:n], rx[:n]))
    return min(e, n - e) / n            # allow global inversion (180 amb)


def roundtrip(code_spec, modem, snr=None, seed=0):
    rng = np.random.default_rng(seed)
    sf = code_len(code_spec, 15)
    spc = 4
    bits = "".join(rng.integers(0, 2, 240).astype(str))
    m = DSSSModulator(1200, sf * spc, spread_factor=15,
                      code_spec=code_spec, modem=modem)
    base = np.asarray(m.modulate(bits).iq_samples)
    rots = range(4) if modem == "qpsk" else range(1)
    best = 1.0
    for r in rots:
        iq = base * np.exp(1j * np.pi / 2 * r)
        if snr is not None:
            p = np.mean(np.abs(iq) ** 2); npw = p / 10 ** (snr / 10)
            iq = iq + ((rng.standard_normal(len(iq)) +
                        1j * rng.standard_normal(len(iq))) * np.sqrt(npw / 2))
        d = DSSSDemodulator(m.sample_rate, 1200, sf * spc, spread_factor=15,
                            code_spec=code_spec, modem=modem)
        best = min(best, _ber_bpsk(bits, d.demodulate(iq).bit_string))
    return best, sf


def main():
    print("DSSS codes + BPSK/QPSK spreading\n")

    # code library properties
    c = resolve_code({"family": "m", "n": 7})
    ac = [abs(int(np.dot(c, np.roll(c, k)))) for k in range(1, len(c))]
    chk("m-seq n=7 length 127 + ideal autocorr", len(c) == 127 and max(ac) == 1)
    chk("gold n=6 length 63", code_len({"family": "gold", "n": 6}) == 63)
    chk("custom chips parse", list(resolve_code({"family": "custom",
        "chips": "+-+"})) == [1, -1, 1])
    chk("seed back-compat length", code_len({"family": "seed", "length": 15}) == 15)

    print(" BPSK-DSSS:")
    for cs in ({"family": "seed", "length": 15}, {"family": "m", "n": 5},
               {"family": "gold", "n": 6, "index": 3},
               {"family": "custom", "chips": "+-+--++-+--+-++"}):
        ber, sf = roundtrip(cs, "bpsk")
        chk(f"bpsk {cs.get('family')} sf={sf} noiseless", ber < 1e-9)
    ber, _ = roundtrip({"family": "m", "n": 6}, "bpsk", snr=-3)
    chk("bpsk m n=6 @ -3 dB SNR (spread gain)", ber < 0.02, f"(BER={ber:.3f})")

    print(" QPSK-DSSS:")
    for cs in ({"family": "seed", "length": 15}, {"family": "m", "n": 5},
               {"family": "gold", "n": 6, "index": 3},
               {"family": "custom", "chips": "+-+--++-"}):
        ber, sf = roundtrip(cs, "qpsk")
        chk(f"qpsk {cs.get('family')} sf={sf} noiseless", ber < 1e-9)
    ber, _ = roundtrip({"family": "m", "n": 7}, "qpsk", snr=0)
    chk("qpsk m n=7 @ 0 dB SNR (spread gain)", ber < 0.03, f"(BER={ber:.3f})")

    print(f"\nresult: {sum(checks)}/{len(checks)} checks passed")
    return 0 if checks and all(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
