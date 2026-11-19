"""
Tier 3 - QC-LDPC array codes + normalized min-sum belief propagation.
(a) noiseless round-trip: huge correct-sign LLR: decode == data, BER=0
(b) parity: encoded word satisfies H c = 0 (GF2), info bits systematic
(c) coding gain vs uncoded BPSK across an Eb/N0 waterfall (real BER numbers)
(d) min-sum convergence: iterations drive unsatisfied checks down
Deterministic seeds. Exit 0 = all pass.
"""
from __future__ import annotations
import sys
import os
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dpmg.fec import ldpc
from dpmg.fec.channel import awgn_llr, uncoded_ber

checks = []
def chk(name, ok, detail=""):
    checks.append(bool(ok)); print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")

CODES = ldpc.names()          # qc-r12, qc-r23, qc-r34

# (a) noiseless round-trip
print("== noiseless round-trip (BER=0) ==")
for nm in CODES:
    c = ldpc.get_ldpc(nm)
    ok_all = True
    for seed in range(8):
        rng = np.random.default_rng(seed)
        u = rng.integers(0, 2, c.K).tolist()
        cw = c.encode(u)
        llr = np.array([(30.0 if b else -30.0) for b in cw])   # correct-sign, huge
        dec = c.decode(llr)
        ok_all &= (dec == u)
    chk(f"roundtrip {nm} (N={c.N},K={c.K},rate={c.rate:.3f})", ok_all)

# (b) parity check H c = 0 and systematic extraction
print("== parity H c = 0 (GF2) + girth ==")
for nm in CODES:
    c = ldpc.get_ldpc(nm)
    par_ok = True; sys_ok = True
    for seed in range(6):
        rng = np.random.default_rng(100 + seed)
        u = rng.integers(0, 2, c.K).tolist()
        cw = c.encode(u)
        par_ok &= c.parity_ok(cw)
        sys_ok &= (c.info_bits(cw) == u)          # info bits appear verbatim
    chk(f"H c = 0 {nm}", par_ok)
    chk(f"systematic info {nm}", sys_ok)
    chk(f"girth>=6 (no 4-cycle) {nm}", not ldpc._has_4cycle(c.H))

# (c) coding gain vs uncoded BPSK, real BER waterfall
print("== coding gain vs uncoded BPSK (Eb/N0 waterfall) ==")
def sim_ber(code, ebn0_db, nblk, seed):
    rng = np.random.default_rng(seed)
    errs = 0; tot = 0
    for _ in range(nblk):
        u = rng.integers(0, 2, code.K).tolist()
        cw = code.encode(u)
        llr, _ = awgn_llr(cw, ebn0_db, rate=code.rate, rng=rng)
        dec = code.decode(llr)
        errs += int(np.count_nonzero(np.array(dec) ^ np.array(u)))
        tot += code.K
    return errs / tot

WATERFALL = [3.0, 3.5]        # dB points reported for every code
for nm in CODES:
    c = ldpc.get_ldpc(nm)
    print(f"  --- {nm} rate {c.rate:.3f} ---")
    for eb in WATERFALL:
        b = sim_ber(c, eb, 60, seed=2025)
        unc = uncoded_ber(eb)
        gain = unc / max(b, 1e-9)
        print(f"      Eb/N0 {eb:.1f} dB  coded BER={b:.3e}  uncoded={unc:.3e}  ({gain:.1f}x)")
    # assert clear gain (>=5x) at the higher waterfall point, averaged over seeds
    eb = 3.5
    bs = [sim_ber(c, eb, 40, seed=s) for s in (11, 23, 37)]
    b = float(np.mean(bs))
    unc = uncoded_ber(eb)
    chk(f"coding gain {nm} @ {eb} dB", b < unc / 5.0,
        f"coded BER={b:.3e} << uncoded={unc:.3e} ({unc/max(b,1e-9):.1f}x)")

# (d) min-sum convergence: unsatisfied checks driven down
print("== min-sum convergence (unsatisfied checks vs iteration) ==")
for nm in CODES:
    c = ldpc.get_ldpc(nm)
    init_sum = 0; final_sum = 0; conv_blocks = 0; nblk = 12
    rng = np.random.default_rng(777)
    for _ in range(nblk):
        u = rng.integers(0, 2, c.K).tolist()
        cw = c.encode(u)
        llr, _ = awgn_llr(cw, 2.5, rate=c.rate, rng=rng)   # noisy: needs iterations
        dec, tr = c.decode_trace(llr)
        if not tr:
            continue
        init_sum += tr[0]
        final_sum += tr[-1]
        conv_blocks += (tr[-1] == 0)
    # aggregate unsatisfied-check count must fall sharply across iterations
    chk(f"unsat decreases {nm}", final_sum < init_sum,
        f"init={init_sum} -> final={final_sum}, converged {conv_blocks}/{nblk}")

print(f"\n{sum(checks)}/{len(checks)} passed")
sys.exit(0 if all(checks) else 1)
