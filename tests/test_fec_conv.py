"""
Tier 1 - K=7 punctured convolutional + soft Viterbi.
(a) noiseless round-trip BER=0 at every rate & seed
(b) coding gain vs uncoded BPSK at threshold Eb/N0
(c) monotone: higher code rate: less gain (sanity)
Exit 0 = all pass. Deterministic seeds.
"""
from __future__ import annotations
import sys
import os
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dpmg.fec import conv
from dpmg.fec.channel import awgn_llr, uncoded_ber, ber

checks = []
def chk(name, ok, detail=""):
    checks.append(bool(ok)); print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")

RATES = ["1/2", "2/3", "3/4", "5/6", "7/8"]

# (a) noiseless round-trip
print("== noiseless round-trip ==")
for rate in RATES:
    ok_all = True
    for seed in range(8):
        rng = np.random.default_rng(seed)
        n = 400
        data = rng.integers(0, 2, n).tolist()
        coded = conv.encode(data, rate)
        # noiseless LLR: big magnitude, correct sign (bit1 to +, bit0 to -)
        llr = np.array([(20.0 if c else -20.0) for c in coded])
        dec = conv.decode_soft(llr, rate, n_info=n)
        ok_all &= (dec == data)
    chk(f"roundtrip rate {rate}", ok_all)

# rate check: coded length
print("== code-rate lengths ==")
for rate in RATES:
    n = 700
    coded = conv.encode([0]*n, rate)
    r = conv.rate_of(rate)
    # coded ~ (n+6)/r ; allow tail overhead
    exp = (n + conv.M) / r
    chk(f"length rate {rate}", abs(len(coded) - exp) <= 8, f"len={len(coded)} exp~{exp:.0f}")

# (b) coding gain vs uncoded at a threshold Eb/N0
print("== coding gain vs uncoded (Eb/N0) ==")
def sim_ber(rate, ebn0_db, nbits, seed):
    rng = np.random.default_rng(seed)
    R = conv.rate_of(rate)
    errs = 0; tot = 0
    for _ in range(6):
        data = rng.integers(0, 2, nbits).tolist()
        coded = conv.encode(data, rate)
        llr, _ = awgn_llr(coded, ebn0_db, rate=R, rng=rng)
        dec = conv.decode_soft(llr, rate, n_info=nbits)
        errs += int(np.count_nonzero(np.array(dec) ^ np.array(data)))
        tot += nbits
    return errs / tot

# Each rate is checked at an Eb/N0 above its own threshold (higher rates need
# more SNR). Deterministic seeds: no hash() (PYTHONHASHSEED-randomised).
# uncoded BPSK BER: 1.25e-2 @4dB, 6.0e-3 @5dB.
RATE_PT = {"1/2": (4.0, 11), "3/4": (4.0, 12), "7/8": (5.0, 13)}
for rate, (ebn0, seed) in RATE_PT.items():
    unc = uncoded_ber(ebn0)
    b = sim_ber(rate, ebn0, 3000, seed=seed)
    gain = unc / max(b, 1e-9)
    chk(f"coding gain rate {rate} @{ebn0}dB", b < unc,
        f"coded BER={b:.3e} vs uncoded {unc:.3e} ({gain:.1f}x better)")

# (c) rate-1/2 should beat rate-7/8 (more redundancy) at same Eb/N0
b12 = sim_ber("1/2", 4.0, 3000, seed=7)
b78 = sim_ber("7/8", 4.0, 3000, seed=7)
chk("1/2 stronger than 7/8", b12 <= b78 + 1e-4, f"1/2={b12:.3e} 7/8={b78:.3e}")

print(f"\n{sum(checks)}/{len(checks)} passed")
sys.exit(0 if all(checks) else 1)
