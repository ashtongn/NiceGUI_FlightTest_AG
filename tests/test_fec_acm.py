"""
Tier 5 - ACM controller + chain dispatch.
(a) MODCOD ladder is monotone in Es/N0 threshold and spectral efficiency
(b) selector picks robust rung at low SNR, high-eff rung at high SNR
(c) hysteresis prevents flapping on a noisy SNR estimate
(d) end-to-end: acm_encode then AWGN@modcod then acm_decode round-trips at adequate SNR
    and adapts throughput up as SNR climbs
Deterministic seeds. Exit 0 = all pass.
"""
from __future__ import annotations
import sys
import os
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dpmg.fec import acm
from dpmg.fec.channel import awgn_llr

checks = []
def chk(name, ok, detail=""):
    checks.append(bool(ok)); print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")

# (a) monotone ladder
print("== MODCOD ladder ==")
reqs = [mc.req_esn0_db for mc in acm.ACM_TABLE]
effs = [mc.spectral_eff for mc in acm.ACM_TABLE]
chk("req_esn0 strictly increasing", all(b > a for a, b in zip(reqs, reqs[1:])), str(reqs))
chk("spectral_eff strictly increasing", all(b > a for a, b in zip(effs, effs[1:])),
    " ".join(f"{e:.2f}" for e in effs))
for mc in acm.ACM_TABLE:
    print(f"    {mc.name:16s} {mc.modulation:6s} {mc.fec_family}:{mc.rate}"
          f"  req={mc.req_esn0_db:4.1f}dB  eff={mc.spectral_eff:.2f} b/sym")

# (b) selector
print("== selector ==")
chk("low SNR -> most robust", acm.select_modcod(0.0).idx == 0)
chk("very high SNR -> top rung", acm.select_modcod(30.0).idx == len(acm.ACM_TABLE) - 1)
# monotone selection
prev = -1
mono = True
for snr in np.arange(0, 20, 0.5):
    idx = acm.select_modcod(float(snr)).idx
    if idx < prev:
        mono = False
    prev = idx
chk("selection monotone in SNR", mono)

# (c) hysteresis anti-flap
print("== hysteresis ==")
MARGIN = 1.0
ctl = acm.AcmController(margin_db=MARGIN, hysteresis_db=1.0)
# dither on the memoryless decision boundary (req + margin). The controller's
# up-shift needs an extra +hyst, so it parks one rung below and stays put while
# the memoryless selector flaps across the point.
thr = acm.ACM_TABLE[2].req_esn0_db + MARGIN
snr_seq = [thr + s for s in np.random.default_rng(3).normal(0, 0.3, 200)]
for snr in snr_seq:
    ctl.update(float(snr))
chk("few switches under dither", ctl.switches <= 6, f"switches={ctl.switches}")
# memoryless selector on the same sequence
flaps = 0
prevn = None
for snr in snr_seq:
    n = acm.select_modcod(float(snr), margin_db=MARGIN).name
    if prevn is not None and n != prevn:
        flaps += 1
    prevn = n
chk("controller flaps less than memoryless", ctl.switches < flaps, f"ctl={ctl.switches} vs memoryless={flaps}")

# a clear SNR ramp should walk the controller up the ladder
ctl2 = acm.AcmController()
seen = set()
for snr in np.arange(0, 25, 0.25):
    seen.add(ctl2.update(float(snr)).idx)
chk("ramp climbs ladder", len(seen) >= 5 and ctl2.current.idx == len(acm.ACM_TABLE) - 1,
    f"reached idx={ctl2.current.idx}, visited {len(seen)} rungs")

# (d) end-to-end encode/AWGN/decode at each rung's operating SNR
print("== end-to-end MODCOD round-trip ==")
def rt_ber(mc, esn0_db, seed, nbits=800):
    r = np.random.default_rng(seed)
    data = r.integers(0, 2, nbits).tolist()
    coded = acm.acm_encode(data, mc)
    # esn0: ebn0 for the channel helper (channel takes Eb/N0, rate=code_rate)
    ebn0 = esn0_db - 10 * np.log10(mc.bits_per_symbol * mc.code_rate) \
        + 10 * np.log10(mc.bits_per_symbol)  # convert Es/N0: Eb/N0 = Es/N0 - 10log10(bits*rate)
    # the same via spectral_eff: Eb/N0 = Es/N0 - 10log10(spectral_eff)
    ebn0 = esn0_db - 10 * np.log10(mc.spectral_eff)
    llr, _ = awgn_llr(coded, ebn0, rate=mc.code_rate, rng=r)
    dec = acm.acm_decode(llr, mc, n_info=nbits)
    return float(np.count_nonzero(np.array(dec) ^ np.array(data))) / nbits

# give each rung a few dB above its nominal threshold and expect low BER
ee_ok = True
for mc in acm.ACM_TABLE:
    b = rt_ber(mc, mc.req_esn0_db + 4.0, seed=mc.idx + 1)
    status = "ok" if b < 5e-2 else "HIGH"
    print(f"    {mc.name:16s} @Es/N0={mc.req_esn0_db+4.0:4.1f}dB  BER={b:.3e}  [{status}]")
    ee_ok &= (b < 5e-2)
chk("all rungs round-trip below 5e-2 at +4dB margin", ee_ok)

# throughput adapts: top rung carries more info bits/symbol than bottom rung
chk("adaptation increases throughput",
    acm.ACM_TABLE[-1].spectral_eff > 4 * acm.ACM_TABLE[0].spectral_eff)

print(f"\n{sum(checks)}/{len(checks)} passed")
sys.exit(0 if all(checks) else 1)
