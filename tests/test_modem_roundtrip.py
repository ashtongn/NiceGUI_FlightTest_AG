"""
Noiseless modulate: demodulate round-trip per modulation. BER 0 in the clean
channel is the bar; anything above it is a demod/dispatch bug, not SNR. Priority
order FSK, 4FSK first. Stdlib + numpy. Exit 0 = every priority mod recovers.
"""
from __future__ import annotations

import sys
import os
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dpmg.modulation.modulator import get_modulator
from dpmg.demodulation.demodulator import get_demodulator

SYMRATE = 4800
SPB = 10                       # sample_rate = SYMRATE*SPB = 48000
FS = SYMRATE * SPB
rng = np.random.default_rng(0xC0FFEE)

# tone plans (baseband Hz); deviations well inside +/-FS/2
FSK_T = dict(tone0=-2400.0, tone1=2400.0)
FSK4_T = dict(tone0=-3600.0, tone1=-1200.0, tone2=1200.0, tone3=3600.0)

PRIORITY = ("FSK", "4FSK")
CASES = [
    ("FSK",       FSK_T,  {}),
    ("4FSK",      FSK4_T, {"dibit_mapping": "binary"}),
    ("BPSK",      {},     {}),
    ("QPSK",      {},     {}),
    ("GFSK",      FSK_T,  {"gfsk_bw_hz": 2400.0}),
    ("π/4-QPSK",  {},     {}),
    ("DQPSK",     {},     {}),
    ("OQPSK",     {},     {}),
]


def _ber(tx: str, rx: str) -> float:
    n = min(len(tx), len(rx))
    if n == 0:
        return 1.0
    # align on the shorter; demods can drop a trailing partial symbol
    bad = sum(1 for a, b in zip(tx[:n], rx[:n]) if a != b)
    return bad / n


def run(mod: str, tones: dict, dkw: dict, nbits: int = 400):
    bits = "".join(rng.integers(0, 2, nbits).astype(str))
    m = get_modulator(mod, SYMRATE, SPB, amplitude=0.8, **tones, **{
        k: v for k, v in dkw.items() if k in ("dibit_mapping", "gfsk_bw_hz")})
    iq = np.asarray(m.modulate(bits).iq_samples, dtype=np.complex64)
    d = get_demodulator(mod, FS, SYMRATE, SPB, **tones, **dkw)
    res = d.demodulate(iq)
    rx = getattr(res, "bit_string", "") or ""
    # 4FSK pads odd bit counts to even; compare on the transmitted length
    ber = _ber(bits, rx)
    return ber, len(bits), len(rx)


def main():
    print(f"modem round-trip (noiseless, {SYMRATE} sym/s, sps {SPB}, fs {FS})\n")
    results = {}
    for mod, tones, dkw in CASES:
        try:
            ber, ntx, nrx = run(mod, tones, dkw)
            ok = ber < 1e-9
            results[mod] = ok
            tag = "PASS" if ok else ("FAIL" if mod in PRIORITY else "GAP ")
            print(f"  [{tag}] {mod:<10} BER={ber:6.3f}  tx={ntx} rx={nrx}")
        except Exception as e:
            results[mod] = False
            tag = "FAIL" if mod in PRIORITY else "GAP "
            print(f"  [{tag}] {mod:<10} raised: {type(e).__name__}: {e}")
    prio_ok = all(results.get(m) for m in PRIORITY)
    print(f"\npriority (FSK/4FSK): {'ALL PASS' if prio_ok else 'REGRESSION'}")
    return 0 if prio_ok else 1


if __name__ == "__main__":
    sys.exit(main())
