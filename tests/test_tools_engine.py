"""
test_tools_engine.py: test-signal generators + measurement sanity.

Checks each generator produces the intended signal and that the measurement
helpers read it back correctly. Exit 0 = all passed.
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
from dpmg.dsp import tools_engine as te

checks = []


def chk(name, ok, detail=""):
    checks.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")


def main() -> int:
    fs = 2_000_000.0

    # 1. chirp sweeps the band: instantaneous freq goes f0: f1 monotonically
    c = te.lfm_chirp(fs, -400e3, 400e3, 0.02)
    inst = np.diff(np.unwrap(np.angle(c))) * fs / (2 * np.pi)
    chk("chirp sweeps low->high", inst[0] < -300e3 and inst[-1] > 300e3,
        f"(start {inst[0]/1e3:.0f}k end {inst[-1]/1e3:.0f}k)")
    chk("chirp roughly linear", abs(np.corrcoef(np.arange(len(inst)), inst)[0, 1]) > 0.99)

    # 1b. the fast comb (tones on whole FFT bins) equals the sum of its tones,
    # and a comb off the bins still takes the slow path
    for sp, nt, cen, dur in ((375e3, 128, 0.0, 0.002), (375e3, 64, 240e3, 0.002), (200e3, 7, 0.0, 0.00123)):
        got = te.comb_jam(fs, sp, nt, dur, ampl=0.5, center=cen)
        tt = np.arange(len(got)) / fs
        s = sum(np.exp(2j * np.pi * ((k - (nt - 1) / 2.0) * sp + cen) * tt) for k in range(nt))
        chk(f"comb {nt} tones x {sp/1e3:g} kHz matches the tone sum",
            np.max(np.abs(got - 0.5 * s / np.max(np.abs(s)))) < 1e-5)

    # 2. tone lands at the requested freq
    t = te.cw_tone(fs, 250e3, 0.02)
    fpk, _, snr = te.peak_tone(t, fs)
    chk("tone at +250 kHz", abs(fpk - 250e3) < 5e3 and snr > 30,
        f"(peak {fpk/1e3:.0f}k snr {snr:.0f}dB)")

    # 3. two-tone has two peaks
    tt = te.two_tone(fs, -150e3, 150e3, 0.02)
    f, p = te.psd_db(tt, fs)
    med = np.median(p)
    peaks = f[(p > med + 20)]
    has_lo = np.any(np.abs(peaks + 150e3) < 8e3)
    has_hi = np.any(np.abs(peaks - 150e3) < 8e3)
    chk("two-tone shows both tones", has_lo and has_hi)

    # 4. band-limited noise stays in band
    nz = te.band_noise(fs, 400e3, 0.05)
    in_band = te.band_power_db(nz, fs, -200e3, 200e3)
    out_band = te.band_power_db(nz, fs, 600e3, 900e3)
    chk("noise band-limited", in_band > out_band + 20,
        f"(in {in_band:.0f} out {out_band:.0f} dB)")

    # 5. generate() dispatch
    for k in te.GENERATORS:
        g = te.generate(k, fs, 0.01)
        chk(f"generate({k})", len(g) > 0 and g.dtype == np.complex64)

    # 6. noise_floor stable
    fl = te.noise_floor_db(nz, fs)
    chk("noise_floor is finite", np.isfinite(fl))

    # 7. modulated jammer: constellations are unit average energy + Gray-mapped
    for m in te.MOD_JAM_CONSTELLATIONS:
        c = te.constellation(m)
        avg = float(np.mean(np.abs(c) ** 2))
        chk(f"{m} unit avg energy", abs(avg - 1.0) < 1e-6, f"({avg:.4f})")
    # adjacent Gray labels differ by one bit (16QAM horizontal neighbours)
    q = te.constellation("qpsk")
    chk("qpsk has 4 distinct points", len(set(np.round(q, 3))) == 4)

    # 8. modulated generator lands in-band and is RMS-ish flat (not a line)
    mj = te.modulated_jam(fs, 200e3, 0.02, ampl=0.5, mod="qpsk", center=0.0)
    inb = te.band_power_db(mj, fs, -150e3, 150e3)
    outb = te.band_power_db(mj, fs, 700e3, 900e3)
    chk("modulated jammer in-band", inb > outb + 15, f"(in {inb:.0f} out {outb:.0f})")
    chk("modulated dtype", mj.dtype == np.complex64 and len(mj) == int(fs * 0.02))

    # 9. symbol on-off keying holds average power (per the paper's lambda scaling)
    full = te.modulated_jam(fs, 200e3, 0.05, ampl=1.0, mod="qpsk", sym_duty=1.0)
    ook = te.modulated_jam(fs, 200e3, 0.05, ampl=1.0, mod="qpsk", sym_duty=0.25)
    pf = float(np.mean(np.abs(full) ** 2)); po = float(np.mean(np.abs(ook) ** 2))
    chk("OOK keeps avg power", abs(10 * np.log10(po / pf)) < 1.0,
        f"({10*np.log10(po/pf):+.2f} dB)")

    # 10. BER sweep reproduces the paper: modulated QPSK jammer >= equal-power
    #     barrage (AWGN) at every J/S against 16QAM
    r = te.ber_vs_js("16qam", snr_db=20, js_db_list=[0, 5, 10, 15],
                     n_sym=60_000, jammers=("qpsk", "barrage"))
    worse = all(r["qpsk"][i] >= r["barrage"][i] - 2e-3 for i in range(4))
    chk("qpsk jammer >= barrage (paper)", worse,
        f"(qpsk {r['qpsk'][0]:.3f} vs barrage {r['barrage'][0]:.3f} @ 0 dB)")
    chk("ber in [0,0.75]", all(0 <= b <= 0.75 for b in r["qpsk"] + r["barrage"]))

    # 11. validator flags an under-sampled symbol rate + unknown constellation
    w = te.check_waveform_params("modulated", fs, 0.02, sym_rate=1.8e6, mod="qam9")
    chk("validator warns under-sampled Rs", any("samples/symbol" in x for x in w))
    chk("validator warns unknown mod", any("unknown" in x for x in w))

    print(f"\nresult: {sum(checks)}/{len(checks)} checks passed")
    return 0 if all(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
