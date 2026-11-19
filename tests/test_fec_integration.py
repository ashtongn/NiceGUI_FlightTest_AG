"""
FEC and modem integration: coded bits through the real higher-order modulator
+ soft_llr hook (the documented LLR source) into the Tier-1 soft Viterbi and the
ACM chain. Proves the soft-LLR sign/scale convention lines up end to end.

Exit 0 = all pass. Deterministic seeds.
"""
from __future__ import annotations
import sys
import os
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dpmg.modulation.higher_order import get_higher_order_modulator
from dpmg.fec import conv, acm

checks = []
def chk(name, ok, detail=""):
    checks.append(bool(ok)); print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")


def run_link(mod_name, coded_bits, ebn0_db, code_rate, rng):
    """Modulate coded bits (sps=1), AWGN at Eb/N0, return per-coded-bit LLRs via
    the modulator's soft_llr (llr > 0 means bit 1), trimmed to len(coded_bits)."""
    m = get_higher_order_modulator(mod_name, symbol_rate=1000, samples_per_symbol=1)
    bps = m.bits_per_symbol
    bitstr = "".join(str(b) for b in coded_bits)
    res = m.modulate(bitstr)
    iq = np.asarray(res.iq_samples, dtype=complex)         # Es=1 avg symbol energy
    ebn0 = 10.0 ** (ebn0_db / 10.0)
    esn0 = ebn0 * bps * code_rate                          # Es/N0 = Eb/N0 * bps * R
    n0 = 1.0 / esn0
    sigma = np.sqrt(n0 / 2.0)
    iq = iq + sigma * (rng.standard_normal(len(iq)) + 1j * rng.standard_normal(len(iq)))
    llr = m.soft_llr(iq, n0=n0)
    return np.asarray(llr[:len(coded_bits)], dtype=np.float64)


print("== soft_llr -> Tier-1 Viterbi (8PSK, 16QAM) ==")
for si, mod_name in enumerate(("8PSK", "16QAM")):
    for ri, rate in enumerate(("1/2", "3/4")):
        rng = np.random.default_rng(1000 + si * 10 + ri)  # deterministic
        R = conv.rate_of(rate)
        nbits = 600
        errs = tot = 0
        for _ in range(4):
            data = rng.integers(0, 2, nbits).tolist()
            coded = conv.encode(data, rate)
            llr = run_link(mod_name, coded, ebn0_db=7.0, code_rate=R, rng=rng)
            dec = conv.decode_soft(llr, rate, n_info=nbits)
            errs += int(np.count_nonzero(np.array(dec) ^ np.array(data)))
            tot += nbits
        ber = errs / tot
        chk(f"{mod_name} conv {rate} @7dB Eb/N0", ber < 1e-2, f"BER={ber:.3e}")

print("== LLR sign sanity (noiseless) ==")
# a clean symbol stream must yield LLRs whose signs equal the transmitted bits
m = get_higher_order_modulator("8PSK", symbol_rate=1000, samples_per_symbol=1)
rng = np.random.default_rng(1)
bits = rng.integers(0, 2, 300).tolist()
res = m.modulate("".join(map(str, bits)))
llr = m.soft_llr(np.asarray(res.iq_samples, dtype=complex), n0=1e-3)
sign_bits = (np.asarray(llr[:len(bits)]) > 0).astype(int).tolist()
chk("llr>0 <=> bit1 (matches conv/ldpc convention)", sign_bits == bits)

print("== ACM chain through a real constellation ==")
# pick a MODCOD that uses 8PSK, push coded bits through its modulation + soft_llr
mc = next(x for x in acm.ACM_TABLE if x.modulation == "8PSK")
rng = np.random.default_rng(9)
data = rng.integers(0, 2, 500).tolist()
coded = acm.acm_encode(data, mc)                # Tier-1 FEC for that MODCOD
llr = run_link(mc.modulation, coded, ebn0_db=8.0, code_rate=mc.code_rate, rng=rng)
dec = acm.acm_decode(llr, mc, n_info=len(data))
ber = float(np.count_nonzero(np.array(dec) ^ np.array(data))) / len(data)
chk(f"ACM {mc.name} end-to-end @8dB Eb/N0", ber < 1e-2, f"BER={ber:.3e}")

print(f"\n{sum(checks)}/{len(checks)} passed")
sys.exit(0 if all(checks) else 1)
