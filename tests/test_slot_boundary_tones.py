"""
Slot-boundary blind strip: a tone on a 20 kHz slot edge is detected, at its frequency
=====================================================================================
(test_slot_boundary_tones.py)

With guard_frac 0.9 the CPU EnergyDetector integrates only the central 18 kHz of
each 20 kHz slot, leaving a 2 kHz dead strip at every boundary. A clean
Hann-windowed tone keeps its energy within +-2 bins (+-117 Hz at 1.92 MSps,
+-1.9 kHz at 30.72), so a tone on a boundary lights no slot: the +300 kHz port
probe on the same-card pair (no TX skirt) gives 0 detections while +310 kHz gives
17.1 dB (Block 3 finding F3), and the 0 Hz DET-09 emitter and every 200 kHz comb
tone show only through their skirt, 10 kHz off.

Fix (energy_detector.py): slots tile the band (guard_frac 1.0), Detection /
BurstEvent carry freq_hz = interpolated PSD peak, and a spectral valley between
two adjacent active slots' peaks keeps two tones either side of a boundary apart.

Pins, offline, through the same EnergyDetector.process + BurstTracker the
service tick uses:
  * a clean (skirt-free) tone swept across one full slot in 1 kHz steps, both
    edges included, at all four G40 rates: 1 burst at every offset, freq error
    < 5 kHz; the old guard_frac 0.9 misses the edges (the bug, reproduced);
  * a tone exactly at 0 Hz (the DC boundary) and in each DC slot (+-10 kHz);
  * two tones either side of one boundary (+-10 and +-8 kHz) stay two, each at
    its frequency; one tone on the boundary stays one;
  * no false valley split: band-limited noise over 3 slots and a 4800 Bd FSK
    straddling a boundary are one burst each;
  * skirted (cross-card-like) boundary tone, the 200 kHz comb clean and skirted,
    DET-08 placements and DET-09 25 emitters: counts unchanged, freq < 5 kHz.
Exit 0 = pass.
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np

from dpmg.dsp.energy_detector import EnergyDetector, BurstTracker
import test_det02_slot_clustering as d02

CENTER = d02.CENTER
RATES = d02.RATES
SLOT = d02.SLOT
NFFT = d02.NFFT
FERR_MAX = 5e3

checks = []


def chk(name, ok, detail=""):
    checks.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")


def clean(fs, tones, snr_db, rng, N=4 * NFFT):
    """Skirt-free tones (offset_hz, amplitude) in white noise; snr_db is the
    first tone over the per-slot noise power (full 20 kHz slot)."""
    t = np.arange(N) / fs
    amp0 = tones[0][1]
    p_slot = amp0 ** 2 / 10 ** (snr_db / 10.0)
    sigma = np.sqrt(p_slot * fs / (2.0 * SLOT))
    x = sigma * (rng.standard_normal(N) + 1j * rng.standard_normal(N))
    for off, amp in tones:
        x = x + amp * np.exp(2j * np.pi * off * t)
    return x.astype(np.complex64)


def bursts(fs, frames, guard_frac=None):
    kw = {} if guard_frac is None else {"guard_frac": guard_frac}
    det = EnergyDetector(fs=fs, center_hz=CENTER, slot_bw=SLOT, nfft=NFFT,
                         threshold_db=d02.THRESH_DB, **kw)
    tr = BurstTracker()
    t = 0.0
    for fr in frames:
        tr.update(det.process(fr), t)
        t += 0.2
    return tr.flush(t)


def off_of(e):
    return e.freq_hz - CENTER


def fmt(closed):
    return [round(off_of(e) / 1e3, 2) for e in closed]


# tests

def test_sweep_one_slot():
    print("\nclean tone swept across one full slot (300..320 kHz, 1 kHz steps, both edges)")
    offs = [300e3 + 1e3 * k for k in range(21)]
    for fs in RATES:
        n_ok, worst, bad = 0, 0.0, []
        legacy_miss = 0
        for i, off in enumerate(offs):
            rng = np.random.default_rng(1000 + i)
            frames = [clean(fs, [(off, 0.3)], 20.0, rng) for _ in range(3)]
            closed = bursts(fs, frames)
            if len(closed) == 1 and abs(off_of(closed[0]) - off) < FERR_MAX:
                n_ok += 1
                worst = max(worst, abs(off_of(closed[0]) - off))
            else:
                bad.append((off / 1e3, fmt(closed)))
            if not bursts(fs, frames, guard_frac=0.9):
                legacy_miss += 1
        chk(f"{fs/1e6:5.2f} MSps: 1 burst within 5 kHz at {n_ok}/21 offsets",
            n_ok == 21, f"worst {worst:.0f} Hz bad {bad}")
        chk(f"{fs/1e6:5.2f} MSps: old guard_frac 0.9 misses the edges ({legacy_miss} offsets blind)",
            legacy_miss >= 2)
    # boundary tone at the lowest SNR that clears the 10 dB threshold in the old
    # slot-centred case: the split costs at most 3 dB
    for fs in (1.92e6, 30.72e6):
        rng = np.random.default_rng(7)
        frames = [clean(fs, [(300e3 + 470.0, 0.3)], 14.0, rng) for _ in range(3)]
        closed = bursts(fs, frames)
        chk(f"{fs/1e6:5.2f} MSps: off-bin boundary tone at 14 dB slot SNR -> 1 burst",
            len(closed) == 1 and abs(off_of(closed[0]) - 300.47e3) < FERR_MAX, f"{fmt(closed)}")


def test_dc():
    print("\ntone exactly at 0 Hz (DC boundary) and in each DC slot")
    for fs in (1.92e6, 30.72e6):
        for off in (0.0, 10e3, -10e3):
            rng = np.random.default_rng(int(abs(off)) + 3)
            frames = [clean(fs, [(off, 0.3)], 25.0, rng) for _ in range(3)]
            closed = bursts(fs, frames)
            chk(f"{fs/1e6:5.2f} MSps tone at {off/1e3:+.0f} kHz -> 1 burst within 5 kHz",
                len(closed) == 1 and abs(off_of(closed[0]) - off) < FERR_MAX, f"{fmt(closed)}")


def test_two_tones_across_boundary():
    print("\ntwo tones either side of one boundary stay two")
    for fs in (1.92e6, 30.72e6):
        for d, a2 in ((10e3, 0.3), (8e3, 0.3), (10e3, 0.3 * 10 ** (-10 / 20))):
            rng = np.random.default_rng(11)
            tones = [(300e3 - d, 0.3), (300e3 + d, a2)]
            frames = [clean(fs, tones, 30.0, rng) for _ in range(3)]
            closed = sorted(bursts(fs, frames), key=off_of)
            ok = (len(closed) == 2 and all(abs(off_of(e) - o) < FERR_MAX
                                          for e, (o, _) in zip(closed, tones)))
            chk(f"{fs/1e6:5.2f} MSps tones at 300 -+ {d/1e3:.0f} kHz "
                f"(second {20*np.log10(a2/0.3):+.0f} dB) -> 2 bursts at the tones",
                ok, f"{fmt(closed)}")
        rng = np.random.default_rng(12)
        frames = [clean(fs, [(300e3, 0.3)], 30.0, rng) for _ in range(3)]
        closed = bursts(fs, frames)
        chk(f"{fs/1e6:5.2f} MSps one tone ON the boundary -> 1 burst", len(closed) == 1, f"{fmt(closed)}")


def test_no_false_valley_split():
    print("\nno false split: wide emitters across a boundary stay one")
    for fs in (1.92e6, 30.72e6):
        rng = np.random.default_rng(21)
        N = 4 * NFFT
        frames = []
        for _ in range(3):
            w = rng.standard_normal(N) + 1j * rng.standard_normal(N)
            f = np.fft.fftfreq(N, 1.0 / fs)
            W = np.fft.fft(w) * ((f > 300e3 - 25e3) & (f < 300e3 + 25e3))
            sig = np.fft.ifft(W)
            sig = 0.3 * sig / np.sqrt(np.mean(np.abs(sig) ** 2))
            # noise 30 dB under the emitter's power per slot (0.3^2 x 20/50 kHz)
            p_slot = 0.09 * (SLOT / 50e3) / 1e3
            sigma = np.sqrt(p_slot * fs / (2.0 * SLOT))
            frames.append((sig + sigma * (rng.standard_normal(N) + 1j * rng.standard_normal(N))
                           ).astype(np.complex64))
        closed = bursts(fs, frames)
        chk(f"{fs/1e6:5.2f} MSps 50 kHz noise emitter over 3 slots -> 1 burst", len(closed) == 1,
            f"{[(e.n_slots, round(off_of(e)/1e3, 1)) for e in closed]}")
        # short zero-padded frames (8192 < NFFT): the noisiest PSD the valley test
        # sees. 1.92 only: at 30.72 an 8192-sample frame (0.27 ms, 3.75 kHz
        # resolution, never a service tick) cannot resolve 16 kHz-apart tones
        # and the pre-existing slot clustering already splits wide emitters there
        nsplit, n2 = 0, 0
        for seed in range(20 if fs < 2e6 else 0):
            rng = np.random.default_rng(200 + seed)
            n = 8192
            f = np.fft.fftfreq(n, 1.0 / fs)
            w = rng.standard_normal(n) + 1j * rng.standard_normal(n)
            sig = np.fft.ifft(np.fft.fft(w) * (np.abs(f - 300e3 - rng.uniform(-10e3, 10e3)) < 25e3))
            sig = 0.3 * sig / np.sqrt(np.mean(np.abs(sig) ** 2))
            sigma = np.sqrt(0.09 * (SLOT / 50e3) / 1e3 * fs / (2.0 * SLOT))
            x = (sig + sigma * (rng.standard_normal(n) + 1j * rng.standard_normal(n))).astype(np.complex64)
            det = EnergyDetector(fs=fs, center_hz=CENTER, slot_bw=SLOT, nfft=NFFT)
            nsplit += len(d02.cluster_detections(det.process(x))) > 1
            two = clean(fs, [(292e3, 0.3), (308e3, 0.3)], 30.0, rng, N=n)
            n2 += len(d02.cluster_detections(det.process(two))) == 2
        if fs < 2e6:
            chk(f"{fs/1e6:5.2f} MSps 8192-sample frames: 50 kHz emitter never split ({nsplit}/20), "
                f"300 -+ 8 kHz tones split {n2}/20", nsplit == 0 and n2 == 20)
        rng = np.random.default_rng(22)
        frames = [d02.synth(fs, 4 * NFFT, [(300e3, 0.3)], rng, d02.sigma_for_snr(fs, 0.3, 35.0),
                            skirt_db=-80.0, image_db=-120.0, fsk_dev_hz=2400.0, fsk_baud=4800.0)
                  for _ in range(3)]
        closed = bursts(fs, frames)
        chk(f"{fs/1e6:5.2f} MSps 4800 Bd +-2.4 kHz FSK centred on a boundary -> 1 burst within 5 kHz",
            len(closed) == 1 and abs(off_of(closed[0]) - 300e3) < FERR_MAX, f"{fmt(closed)}")


def test_skirted_and_comb():
    print("\nskirted boundary tone (cross-card-like) and the 200 kHz comb")
    for fs in (1.92e6, 30.72e6):
        rng = np.random.default_rng(31)
        frames = [d02.synth(fs, 4 * NFFT, [(300e3, 0.3)], rng, d02.sigma_for_snr(fs, 0.3, 45.0))
                  for _ in range(3)]
        closed = bursts(fs, frames)
        chk(f"{fs/1e6:5.2f} MSps skirted tone at +300 kHz -> 1 burst within 5 kHz",
            len(closed) == 1 and abs(off_of(closed[0]) - 300e3) < FERR_MAX, f"{fmt(closed)}")
    comb = [(-3.5 + k) * 200e3 for k in range(8)]          # every tone on a slot edge
    for fs in RATES:
        for label, mk in (
                ("clean", lambda rng, fs=fs: clean(fs, [(o, 0.3) for o in comb], 40.0, rng)),
                ("skirted", lambda rng, fs=fs: d02.synth(fs, 4 * NFFT, [(o, 0.3) for o in comb], rng,
                                                         d02.sigma_for_snr(fs, 0.3, 60.0)))):
            rng = np.random.default_rng(41)
            closed = bursts(fs, [mk(rng) for _ in range(4)])
            hit = sum(1 for o in comb if any(abs(off_of(e) - o) < FERR_MAX for e in closed))
            chk(f"{fs/1e6:5.2f} MSps {label:<7} edge comb -> {len(closed)} bursts, {hit}/8 within 5 kHz",
                len(closed) == 8 and hit == 8, f"{fmt(closed)}")


def test_det08_det09_freq():
    print("\nDET-08 / DET-09 shapes: reported frequency now within 5 kHz")
    fs = 1.92e6
    slot_c = 15 * SLOT + 10e3
    errs, counts = [], []
    for k in range(10):
        off = slot_c - 9e3 + 2e3 * k
        rng = np.random.default_rng(100 + k)
        frames = [d02.synth(fs, 4 * NFFT, [(off, 0.3)], rng, d02.sigma_for_snr(fs, 0.3, 55.0))
                  for _ in range(2)]
        closed = bursts(fs, frames)
        near = [e for e in closed if abs(off_of(e) - off) <= SLOT]
        counts.append(len(near))
        errs.append(min(abs(off_of(e) - off) for e in near) if near else float("inf"))
    chk(f"DET-08 10 placements: one burst each {counts}, worst error {max(errs):.0f} Hz",
        all(c == 1 for c in counts) and max(errs) < FERR_MAX)
    fs = 30.72e6
    offs = [float(x) for x in np.linspace(-12e6, 12e6, 25)]
    rng = np.random.default_rng(9)
    frames = [d02.synth(fs, 4 * NFFT, [(o, 0.2) for o in offs], rng, d02.sigma_for_snr(fs, 0.2, 55.0),
                        fsk_dev_hz=2400.0, fsk_baud=4800.0) for _ in range(3)]
    closed = bursts(fs, frames)
    err = [min((abs(off_of(e) - o) for e in closed), default=float("inf")) for o in offs]
    chk(f"DET-09 25 FSK emitters: {len(closed)} bursts, {sum(x < FERR_MAX for x in err)}/25 within 5 kHz "
        f"(0 Hz emitter {err[12]:.0f} Hz)", len(closed) == 25 and max(err) < FERR_MAX)


def main() -> int:
    test_sweep_one_slot()
    test_dc()
    test_two_tones_across_boundary()
    test_no_false_valley_split()
    test_skirted_and_comb()
    test_det08_det09_freq()
    n_ok = sum(checks)
    print(f"\nresult: {n_ok}/{len(checks)} checks passed")
    return 0 if n_ok == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
