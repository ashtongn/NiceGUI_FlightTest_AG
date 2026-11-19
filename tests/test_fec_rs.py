"""
Tier 2 - RS(255,223), block interleaver, RS x Viterbi concatenation.
(a) noiseless round-trip, symbol errors <= t corrected, > t flagged
(b) interleaver invertibility + burst-error breakup
(c) concatenation coding gain vs uncoded at threshold Eb/N0
Deterministic seeds. Exit 0 = all pass.
"""
from __future__ import annotations
import sys
import os
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dpmg.fec import rs, conv
from dpmg.fec.channel import awgn_llr, uncoded_ber

checks = []
def chk(name, ok, detail=""):
    checks.append(bool(ok)); print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")

rng = np.random.default_rng(0x2255)

# GF sanity
print("== GF(2^8) ==")
ok = all(rs.gmul(a, rs.ginv(a)) == 1 for a in range(1, 256))
chk("multiplicative inverse", ok)
chk("gmul assoc sample", rs.gmul(rs.gmul(3, 7), 11) == rs.gmul(3, rs.gmul(7, 11)))

# (a) RS round-trip + correction power
print("== RS(255,223) ==")
def rand_msg(seed):
    r = np.random.default_rng(seed)
    return r.integers(0, 256, rs.K).tolist()

clean_ok = True
for seed in range(10):
    m = rand_msg(seed)
    cw = rs.encode(m)
    dec, ne, ok = rs.decode(cw)
    clean_ok &= (dec == m and ne == 0 and ok)
chk("noiseless round-trip (10 msgs)", clean_ok)

# inject exactly t=16 symbol errors: must correct
corr_ok = True
for seed in range(10):
    r = np.random.default_rng(1000 + seed)
    m = rand_msg(seed)
    cw = rs.encode(m)
    pos = r.choice(rs.N, size=16, replace=False)
    for p in pos:
        e = int(r.integers(1, 256))
        cw[p] ^= e
    dec, ne, ok = rs.decode(cw)
    corr_ok &= (dec == m and ok and ne == 16)
chk("corrects t=16 symbol errors", corr_ok)

# t+1 = 17 errors: must not silently return wrong data (flag ok=False or detect)
flag_ok = True
for seed in range(10):
    r = np.random.default_rng(2000 + seed)
    m = rand_msg(seed)
    cw = rs.encode(m)
    pos = r.choice(rs.N, size=20, replace=False)
    for p in pos:
        cw[p] ^= int(r.integers(1, 256))
    dec, ne, ok = rs.decode(cw)
    # acceptable outcomes: flagged uncorrectable, or (rarely) miscorrected; it
    # may report the original as clean only when it really recovered it.
    if ok and dec == m:
        pass  # legitimately recovered (unlikely with 20 errs but fine)
    elif not ok:
        pass  # correctly flagged
    else:
        flag_ok = False  # claimed success but wrong data
chk("20 errors never falsely 'ok' with wrong data", flag_ok)

# (b) interleaver
print("== block interleaver ==")
data = rng.integers(0, 256, 255 * 5).tolist()
il = rs.block_interleave(data, depth=5)
back = rs.block_deinterleave(il, depth=5, orig_len=len(data))
chk("interleave invertible", back == data)

# burst breakup: an interleaved RS array survives a burst that would kill one word
depth = 5
words = [rs.encode(rand_msg(seed)) for seed in range(depth)]
flat = []
for w in words:
    flat += w
il = rs.block_interleave(flat, depth * rs.N // rs.N if False else depth)
# interleave the depth codewords column-wise by hand so a burst hits 1 sym/word
mat = np.array(words, dtype=np.int64)          # depth x 255
inter = mat.T.reshape(-1).tolist()             # column-major
# burst of 16*depth consecutive symbols
b0 = 1000
for i in range(b0, b0 + 16 * depth):
    inter[i] ^= 0x5A
# de-interleave
dmat = np.array(inter, dtype=np.int64).reshape(rs.N, depth).T
burst_ok = True
for wi in range(depth):
    dec, ne, ok = rs.decode(dmat[wi].tolist())
    burst_ok &= (dec == words[wi][:rs.K] and ok)
chk("interleaved burst broken up & corrected", burst_ok)

# (c) RS x Viterbi concatenation coding gain
print("== RS x Viterbi concatenation ==")
def concat_ber(ebn0_db, nwords, seed):
    r = np.random.default_rng(seed)
    R_conv = conv.rate_of("1/2")
    R_total = (rs.K / rs.N) * R_conv        # info-bit rate through the stack
    sym_err = 0; tot_sym = 0
    for _ in range(nwords):
        m = r.integers(0, 256, rs.K).tolist()
        cw = rs.encode(m)                    # 255 symbols
        bits = rs.bytes_to_bits(cw)          # 2040 bits
        coded = conv.encode(bits, "1/2")
        llr, _ = awgn_llr(coded, ebn0_db, rate=R_total, rng=r)
        dbits = conv.decode_soft(llr, "1/2", n_info=len(bits))
        rxsym = rs.bits_to_bytes(dbits)[:rs.N]
        dec, ne, ok = rs.decode(rxsym)
        sym_err += sum(1 for a, b in zip(dec, m) if a != b)
        tot_sym += rs.K
    return sym_err / tot_sym

# At Eb/N0 ~ 3 dB the concatenated stack should drive symbol error very low.
EBN0 = 3.0
cb = concat_ber(EBN0, 6, seed=42)
unc_bit = uncoded_ber(EBN0)
chk("concat near error-free @3dB", cb < 1e-3, f"concat sym-err={cb:.3e}, uncoded bit-BER={unc_bit:.3e}")

# and at a very low SNR the inner Viterbi feeds too many errors: RS may fail:
# only check that the stack runs and is better than uncoded at moderate SNR
cb2 = concat_ber(2.0, 6, seed=7)
chk("concat runs @2dB", cb2 < uncoded_ber(2.0), f"concat={cb2:.3e} < uncoded={uncoded_ber(2.0):.3e}")

print(f"\n{sum(checks)}/{len(checks)} passed")
sys.exit(0 if all(checks) else 1)
