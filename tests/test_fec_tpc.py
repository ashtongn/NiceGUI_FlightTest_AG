"""
Tier 4 - TPC (2D extended-Hamming product) + Chase-Pyndiah SISO.
(a) noiseless round-trip BER=0 (each config, multiple seeds)
(b) component extended Hamming: SEC (corrects any single error) + DED (detects
    every double error)
(c) product code corrects a scattered pattern that exceeds the component code's
    per-line power (the point of 2D iteration)
(d) coding gain vs uncoded BPSK at a threshold Eb/N0 (real BER numbers)
(e) iteration helps: BER(n_iter=4) <= BER(n_iter=1) at a low SNR
Deterministic seeds. Exit 0 = all pass.
"""
from __future__ import annotations
import sys
import os
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dpmg.fec import tpc
from dpmg.fec.channel import awgn_llr, uncoded_ber

checks = []
def chk(name, ok, detail=""):
    checks.append(bool(ok)); print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")

BIG = 12.0
def clean_llr(cw):
    return np.where(np.asarray(cw) > 0, BIG, -BIG).astype(float)

# (a) noiseless round-trip
print("== noiseless round-trip ==")
for name in ["eH(8,4)^2", "eH(16,11)^2"]:
    t = tpc.get_tpc(name)
    ok = True
    for s in range(6):
        rng = np.random.default_rng(s)
        data = rng.integers(0, 2, t.k).tolist()
        cw = t.encode(data)
        dec = t.decode(clean_llr(cw), n_iter=4, p=4)
        ok &= np.array_equal(dec, data)
    chk(f"roundtrip {name} (k={t.k},n={t.n},R={t.rate:.3f})", ok)

# (b) component SEC-DED
print("== component extended Hamming SEC-DED ==")
for m in (3, 4, 5):
    c = tpc.ExtHamming(m)
    rng = np.random.default_rng(10 + m)
    sec = ded = True
    for _ in range(300):
        u = rng.integers(0, 2, c.k)
        cw = c.encode(u)
        e1 = cw.copy(); e1[rng.integers(0, c.n)] ^= 1
        d, nc, ok = c.decode_hard(e1)
        sec &= (np.array_equal(d, u) and ok and nc == 1)
        e2 = cw.copy(); e2[rng.choice(c.n, 2, replace=False)] ^= 1
        _, _, ok2 = c.decode_hard(e2)
        ded &= (not ok2)                       # double error must be flagged
    chk(f"eH({c.n},{c.k}) SEC corrects single", sec)
    chk(f"eH({c.n},{c.k}) DED detects double", ded)

# (c) product corrects a scattered pattern beyond per-line power
print("== 2D correction beyond component power ==")
t = tpc.get_tpc("eH(16,11)^2")
beyond_ok = True
for s in range(8):
    rng = np.random.default_rng(300 + s)
    data = rng.integers(0, 2, t.k).tolist()
    cw = t.encode(data)
    M = np.asarray(cw).reshape(t.nr, t.nc).copy()
    # put 2 errors in each of 3 rows (each row exceeds SEC t=1); columns clean up
    for ri in rng.choice(t.nr, 3, replace=False):
        for ci in rng.choice(t.nc, 2, replace=False):
            M[ri, ci] ^= 1
    llr = clean_llr(M.reshape(-1))
    dec = t.decode(llr, n_iter=6, p=4)
    beyond_ok &= np.array_equal(dec, data)
chk("scattered 2-per-row errors corrected by 2D iteration", beyond_ok)

# (d) coding gain vs uncoded at threshold Eb/N0
print("== coding gain vs uncoded (Eb/N0) ==")
def sim(name, ebn0, nblk, seed):
    t = tpc.get_tpc(name)
    rng = np.random.default_rng(seed)
    errs = tot = 0
    for _ in range(nblk):
        data = rng.integers(0, 2, t.k).tolist()
        cw = t.encode(data)
        llr, _ = awgn_llr(cw, ebn0, rate=t.rate, rng=rng)
        dec = t.decode(llr, n_iter=4, p=4)
        errs += int(np.count_nonzero(np.array(dec) ^ np.array(data)))
        tot += t.k
    return errs / tot

# waterfall for the record
print("  eH(16,11)^2 waterfall:")
for ebn0 in (1.5, 2.0, 2.5, 3.0):
    b = sim("eH(16,11)^2", ebn0, 25, seed=int(ebn0 * 17) + 1)
    print(f"    Eb/N0={ebn0:4.1f} dB  coded BER={b:.3e}  uncoded={uncoded_ber(ebn0):.3e}")

EBN0 = 2.5
b = sim("eH(16,11)^2", EBN0, 40, seed=999)
unc = uncoded_ber(EBN0)
chk(f"coding gain @ {EBN0} dB", b < unc / 3.0, f"coded={b:.3e} vs uncoded={unc:.3e} ({unc/max(b,1e-9):.0f}x)")

# (e) iteration helps
print("== iteration helps ==")
def sim_iter(ni, seed):
    t = tpc.get_tpc("eH(16,11)^2")
    rng = np.random.default_rng(seed)
    errs = tot = 0
    for _ in range(30):
        data = rng.integers(0, 2, t.k).tolist()
        cw = t.encode(data)
        llr, _ = awgn_llr(cw, 1.5, rate=t.rate, rng=rng)
        dec = t.decode(llr, n_iter=ni, p=4)
        errs += int(np.count_nonzero(np.array(dec) ^ np.array(data)))
        tot += t.k
    return errs / tot
b1 = sim_iter(1, 55)
b4 = sim_iter(4, 55)
chk("4 iters <= 1 iter", b4 <= b1, f"1-iter={b1:.3e}  4-iter={b4:.3e}")

print(f"\n{sum(checks)}/{len(checks)} passed")
sys.exit(0 if all(checks) else 1)
