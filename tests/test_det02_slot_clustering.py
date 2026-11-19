"""
DET-02 slot clustering: one emitter: one DETECTION burst (test_det02_slot_clustering.py)
=========================================================================================
Without clustering, an 8-tone comb on the G40 gives 48 / 118 /
62 / 53 bursts at 1.92 / 7.68 / 15.36 / 30.72 MSps instead of ~8: the energy
detector reports one burst per 20 kHz slot and every CW tone lights a skirt of
+-5..10 slots 26-50 dB down (TX phase noise / leakage), plus an IQ image 40-48 dB
below the tone at the mirror frequency.

This pins energy_detector.cluster_detections + the span-matching BurstTracker,
offline, with synthetic IQ run through the same EnergyDetector.process() the
service uses (dpmg_service._det_step):

  * 8-tone comb (200 kHz spacing, tones on slot boundaries like the real comb,
    and slot-centred) with a Lorentzian skirt and a -45 dB mirror image, at all
    four G40 rates: 8 +- 1 bursts (legacy per-slot tracker: far more);
  * two tones one inactive slot apart stay two; adjacent equal tones stay two;
    a 12 dB weaker tone touching a stronger one is not hidden; a 30 dB weaker
    touching one is folded in (the documented limit);
  * a tone straddling a slot boundary whose peak slot flips frame to frame is
    one burst, not a re-trigger;
  * DET-09 shape: 25 FSK emitters across +-12 MHz at 30.72 MSps: 25 bursts,
    every truth offset within one slot (no regression from clustering);
  * DET-08 shape: 10 placements within one slot: 1 burst each, error <= 1 slot.
Exit 0 = pass.
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np

from dpmg.dsp.energy_detector import (EnergyDetector, BurstTracker, Detection,
                             cluster_detections)

CENTER = 425_025_000.0
RATES = (1.92e6, 7.68e6, 15.36e6, 30.72e6)
SLOT = 20e3
NFFT = 32768
THRESH_DB = 10.0           # service default (DET_DEFAULT_THRESHOLD_DB)
SKIRT_DB = -36.0           # calibrates the Lorentzian skirt so the slot next to the tone
                           # sits ~-28 dB and the valley 5 slots out ~-38 dB (G40 LOG-01
                           # comb profile: -26..-31 and -41)
IMAGE_DB = -45.0           # IQ image at the mirror frequency (G40: -40..-48)

checks = []


def chk(name, ok, detail=""):
    checks.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")


# synthesis

def _shaped_skirt(N, fs, rng, fc=2e3):
    """Unit-ish complex noise with a one-pole (Lorentzian, 1/f^2) spectrum:
    the shape of a TX phase-noise skirt. Returned together with its power in
    the band 10..30 kHz (one slot off the carrier) so callers can scale it."""
    w = (rng.standard_normal(N) + 1j * rng.standard_normal(N)).astype(np.complex64)
    f = np.fft.fftfreq(N, 1.0 / fs)
    H = 1.0 / np.sqrt(1.0 + (f / fc) ** 2)
    S = np.fft.fft(w) * H
    band = (np.abs(f) >= 10e3) & (np.abs(f) <= 30e3)
    p_band = float(np.sum(np.abs(S[band]) ** 2) / (N * N)) / 2.0   # per side
    return np.fft.ifft(S).astype(np.complex64), p_band


def synth(fs, N, emitters, rng, noise_sigma, skirt_db=SKIRT_DB, image_db=IMAGE_DB,
          fsk_dev_hz=0.0, fsk_baud=0.0):
    """emitters: list of (offset_hz, amplitude). Each emitter = carrier (or FSK
    if fsk_dev_hz) x (1 + scaled skirt). Adds a mirror image of the whole
    signal at image_db and complex white noise of sigma per component."""
    t = np.arange(N) / fs
    sig = np.zeros(N, dtype=np.complex64)
    for off, amp in emitters:
        if fsk_dev_hz > 0:
            sps = max(1, int(round(fs / fsk_baud)))
            nsym = N // sps + 1
            bits = rng.integers(0, 2, nsym)
            dev = np.repeat(np.where(bits, fsk_dev_hz, -fsk_dev_hz), sps)[:N]
            phase = 2 * np.pi * np.cumsum(off + dev) / fs
            carrier = np.exp(1j * phase)
        else:
            carrier = np.exp(2j * np.pi * off * t)
        skirt, p_band = _shaped_skirt(N, fs, rng)
        g = 10 ** (skirt_db / 20.0) / np.sqrt(p_band)
        sig += (amp * carrier * (1.0 + g * skirt)).astype(np.complex64)
    img = (10 ** (image_db / 20.0)) * np.conj(sig)
    noise = noise_sigma * (rng.standard_normal(N) + 1j * rng.standard_normal(N))
    return (sig + img + noise).astype(np.complex64)


def sigma_for_snr(fs, amp, snr_db):
    """Noise sigma (per component) that puts a tone of `amp` snr_db above the
    detector's per-slot floor (slot integrates 0.9 x 20 kHz of noise PSD)."""
    p_slot = amp ** 2 / (10 ** (snr_db / 10.0))        # wanted noise power per slot
    return float(np.sqrt(p_slot * fs / (2.0 * 0.9 * SLOT)))


def run_bursts(fs, frames, margin_db=20.0, gap=1, thresh=THRESH_DB):
    """Feed frames through EnergyDetector.process + BurstTracker like the
    service tick, then flush (a silent frame + the hold-over); return the
    closed bursts."""
    det = EnergyDetector(fs=fs, center_hz=CENTER, slot_bw=SLOT, nfft=NFFT,
                         threshold_db=thresh)
    tr = BurstTracker(cluster_margin_db=margin_db, cluster_gap=gap)
    t = 0.0
    for fr in frames:
        tr.update(det.process(fr), t)
        t += 0.2
    closed = tr.flush(t)
    return closed, det


def comb_frames(fs, offsets, snr_db, n_frames=4, N=4 * NFFT, seed=1, amp=0.3):
    rng = np.random.default_rng(seed)
    sigma = sigma_for_snr(fs, amp, snr_db)
    return [synth(fs, N, [(o, amp) for o in offsets], rng, sigma) for _ in range(n_frames)]


# tests

def test_cluster_function():
    print("\ncluster_detections unit cases")
    mk = lambda s, p: Detection(slot=s, center_hz=CENTER + s * SLOT, power_db=p, bin_lo=0, bin_hi=0)
    # tone at slot 10 with a skirt 30 dB down on both sides: one emitter spanning 7..13
    run = [mk(7, -40), mk(8, -36), mk(9, -31), mk(10, 0), mk(11, -30), mk(12, -35), mk(13, -40)]
    out = cluster_detections(run, 20.0, 1)
    chk("skirt run -> 1 emitter at the peak", len(out) == 1 and out[0].slot == 10,
        f"{[(d.slot, d.slot_lo, d.slot_hi) for d in out]}")
    chk("emitter span covers the whole run", out[0].slot_lo == 7 and out[0].slot_hi == 13 and out[0].n_slots == 7)
    # two equal tones 10 slots apart with skirts touching: 2
    run2 = run + [mk(s, p) for s, p in [(14, -40), (15, -36), (16, -31), (17, 0), (18, -30), (19, -35), (20, -40)]]
    out2 = cluster_detections(run2, 20.0, 1)
    chk("two equal peaks in one run -> 2 emitters", [d.slot for d in out2] == [10, 17], f"{[d.slot for d in out2]}")
    chk("valley split between them", out2[0].slot_hi in (13, 14) and out2[1].slot_lo in (14, 15),
        f"{[(d.slot_lo, d.slot_hi) for d in out2]}")
    # a 12 dB weaker local max touching the strong one: kept
    run3 = run + [mk(14, -20), mk(15, -12), mk(16, -22)]
    out3 = cluster_detections(run3, 20.0, 1)
    chk("12 dB weaker touching emitter kept", [d.slot for d in out3] == [10, 15], f"{[d.slot for d in out3]}")
    # a 30 dB weaker local max touching: folded in (documented limit)
    run4 = run + [mk(14, -36), mk(15, -30), mk(16, -38)]
    out4 = cluster_detections(run4, 20.0, 1)
    chk("30 dB weaker touching bump folded in", [d.slot for d in out4] == [10] and out4[0].slot_hi == 16)
    # one inactive slot between: comparable emitters stay apart, a skirt-level
    # island (the flickering far tail seen at +-2010 kHz on the G40) is folded
    out5 = cluster_detections([mk(10, 0), mk(12, -3)], 20.0, 1)
    chk("one inactive slot between equal emitters -> 2 (gap=1)", [d.slot for d in out5] == [10, 12])
    out6 = cluster_detections([mk(10, 0), mk(12, -35)], 20.0, 1)
    chk("skirt-level island across one inactive slot folded (gap=1)", [d.slot for d in out6] == [10]
        and out6[0].slot_hi == 12)
    out6b = cluster_detections([mk(10, 0), mk(13, -35)], 20.0, 1)
    chk("two inactive slots between -> not bridged (gap=1)", [d.slot for d in out6b] == [10, 13])
    out6c = cluster_detections([mk(10, 0), mk(12, -35)], 20.0, 0)
    chk("gap=0 never bridges an inactive slot", [d.slot for d in out6c] == [10, 12])
    out7 = cluster_detections(run, None, 1)
    chk("margin None -> legacy per-slot passthrough", len(out7) == len(run))
    out8 = cluster_detections([mk(3, -5)], 20.0, 1)
    chk("isolated slot passes through", len(out8) == 1 and out8[0].slot == 3 and out8[0].n_slots == 1)


def test_comb_all_rates():
    print("\nDET-02 synthetic 8-tone comb (200 kHz spacing, skirt %.0f dB, image %.0f dB)" % (SKIRT_DB, IMAGE_DB))
    offsets_boundary = [(-3.5 + k) * 200e3 for k in range(8)]     # +-100, +-300, +-500, +-700 kHz (on slot edges)
    offsets_centred = [o + 10e3 for o in offsets_boundary]          # slot-centred
    for fs in RATES:
        for label, offs, snr in (("edge", offsets_boundary, 60.0),
                                 ("centred", offsets_centred, 60.0),
                                 ("centred-lowsnr", offsets_centred, 22.0)):
            frames = comb_frames(fs, offs, snr)
            closed, det = run_bursts(fs, frames)
            n = len(closed)
            legacy, _ = run_bursts(fs, frames, margin_db=None)
            peak = max((e.peak_power_db - det._floor_db) for e in closed) if closed else float("nan")
            ok = 7 <= n <= 9
            chk(f"{fs/1e6:5.2f} MSps {label:<14} clustered bursts = {n} (legacy {len(legacy)})",
                ok, f"peakSNR {peak:.1f} dB spans {sorted(e.n_slots for e in closed)}")
            if snr >= 60 and fs > 2e6:      # at 1.92 the skirt stays under threshold
                chk(f"{fs/1e6:5.2f} MSps {label:<14} legacy per-slot tracker over-counts",
                    len(legacy) > 9, f"legacy {len(legacy)}")
            # every tone found within one slot of truth
            hits = sum(1 for o in offs if any(abs((e.center_hz - CENTER) - o) <= SLOT for e in closed))
            chk(f"{fs/1e6:5.2f} MSps {label:<14} tones within 1 slot", hits == 8, f"{hits}/8")
    # short zero-padded frame at 1.92 MSps (frame shorter than NFFT)
    frames = comb_frames(1.92e6, offsets_centred, 60.0, N=8192)
    closed, _ = run_bursts(1.92e6, frames)
    chk(f" 1.92 MSps 8192-sample frames clustered bursts = {len(closed)}", 7 <= len(closed) <= 9)


def test_two_tones():
    print("\ntwo-emitter separation")
    fs = 7.68e6
    base = 12 * SLOT + 10e3          # slot-centred
    # SNR 50 dB keeps the -45 dB image under the 10 dB threshold (see test_image)
    for label, d2, amp2, want, snr in (
            ("one inactive slot between, equal, skirts below threshold", 2 * SLOT, 0.3, 2, 22.0),
            ("one inactive slot between, equal, skirts visible", 2 * SLOT, 0.3, 2, 50.0),
            # two equal tones in adjacent slots: the spectral valley between
            # their PSD peaks splits them (without it the grid's resolution
            # limit gives 1 burst, 2-slot span; see
            # tests/test_slot_boundary_tones.py)
            ("adjacent slots, equal (valley split: 2 bursts)", 1 * SLOT, 0.3, 2, 50.0),
            ("3 slots apart, second 12 dB weaker", 3 * SLOT, 0.3 * 10 ** (-12 / 20), 2, 50.0),
            ("3 slots apart, second 30 dB weaker (documented limit)", 3 * SLOT, 0.3 * 10 ** (-30 / 20), 1, 50.0)):
        rng = np.random.default_rng(3)
        sigma = sigma_for_snr(fs, 0.3, snr)
        frames = [synth(fs, 4 * NFFT, [(base, 0.3), (base + d2, amp2)], rng, sigma) for _ in range(3)]
        closed, _ = run_bursts(fs, frames)
        chk(f"{label}: {len(closed)} bursts", len(closed) == want,
            f"want {want}; centres {[round((e.center_hz-CENTER)/1e3) for e in closed]}")
        if d2 == SLOT:
            ev = sorted(closed, key=lambda e: e.slot)
            chk("  adjacent equal tones: disjoint spans, each at its tone",
                len(ev) == 2 and ev[0].slot_hi < ev[1].slot_lo
                and all(abs(f - g) < 1e3 for f, g in zip(sorted(e.freq_hz - CENTER for e in closed),
                                                         (base, base + d2))),
                f"{[(e.n_slots, round((e.freq_hz - CENTER) / 1e3, 2)) for e in closed]}")


def test_image():
    print("\nIQ image is not masked: reported as its own emitter once it clears the threshold")
    fs = 7.68e6
    off = 12 * SLOT + 10e3
    for snr, want in ((50.0, 1), (62.0, 2)):
        rng = np.random.default_rng(5)
        frames = [synth(fs, 4 * NFFT, [(off, 0.3)], rng, sigma_for_snr(fs, 0.3, snr)) for _ in range(3)]
        closed, det = run_bursts(fs, frames)
        cents = sorted(round((e.center_hz - CENTER) / 1e3) for e in closed)
        chk(f"tone at {snr:.0f} dB SNR with a {IMAGE_DB:.0f} dB image -> {len(closed)} bursts",
            len(closed) == want, f"want {want}; centres {cents} kHz")
        if want == 2:
            chk("second burst sits at the mirror frequency", any(abs((e.center_hz - CENTER) + off) <= SLOT for e in closed))


def test_straddle_no_retrigger():
    print("\nboundary-straddling tone: peak slot flips between frames")
    mk = lambda s, p: Detection(slot=s, center_hz=CENTER + s * SLOT, power_db=p, bin_lo=0, bin_hi=0)
    seq = [[mk(10, -20.0), mk(11, -21.0)], [mk(10, -21.5), mk(11, -20.5)],
           [mk(10, -20.2), mk(11, -20.8)], [mk(10, -21.0), mk(11, -20.0)]]
    tr = BurstTracker()
    t = 0.0
    for dets in seq:
        tr.update(dets, t); t += 0.2
    closed = tr.flush(t)
    chk("one burst over 4 frames", len(closed) == 1, f"{len(closed)}")
    chk("burst spans both slots, duration = whole key", closed and closed[0].n_slots == 2
        and abs(closed[0].t_stop - closed[0].t_start - 0.8) < 1e-9)
    legacy = BurstTracker(cluster_margin_db=None)
    t = 0.0
    for dets in seq:
        legacy.update(dets, t); t += 0.2
    chk("legacy tracker reports one burst per slot", len(legacy.flush(t)) == 2)


def test_det09_dense_25():
    print("\nDET-09 shape: 25 FSK emitters across +-12 MHz @ 30.72 MSps")
    fs = 30.72e6
    offs = [float(x) for x in np.linspace(-12e6, 12e6, 25)]
    rng = np.random.default_rng(9)
    sigma = sigma_for_snr(fs, 0.2, 55.0)
    frames = [synth(fs, 4 * NFFT, [(o, 0.2) for o in offs], rng, sigma,
                    fsk_dev_hz=2400.0, fsk_baud=4800.0) for _ in range(3)]
    closed, _ = run_bursts(fs, frames)
    hits = [o for o in offs if any(abs((e.center_hz - CENTER) - o) <= SLOT for e in closed)]
    chk(f"25/25 emitters within one slot ({len(hits)}/25)", len(hits) == 25,
        f"missed {[o/1e3 for o in offs if o not in hits]}")
    chk(f"burst count 25 +- 1 ({len(closed)})", 24 <= len(closed) <= 26)


def test_det08_placements():
    print("\nDET-08 shape: 10 placements within one slot @ 1.92 MSps")
    fs = 1.92e6
    slot_c = 15 * SLOT + 10e3
    errs = []
    counts = []
    totals = []
    for k in range(10):
        off = slot_c - 9e3 + 2e3 * k            # -9..+9 kHz around the slot centre
        rng = np.random.default_rng(100 + k)
        frames = [synth(fs, 4 * NFFT, [(off, 0.3)], rng, sigma_for_snr(fs, 0.3, 55.0)) for _ in range(2)]
        closed, _ = run_bursts(fs, frames)
        near = [e for e in closed if abs((e.center_hz - CENTER) - off) <= SLOT]
        counts.append(len(near))
        totals.append(len(closed))
        errs.append(min(abs((e.center_hz - CENTER) - off) for e in near) if near else float("inf"))
    chk(f"one burst per placement at the tone {counts}", all(c == 1 for c in counts))
    chk(f"at most one extra burst (the -45 dB image at ~threshold) {totals}", all(t <= 2 for t in totals))
    chk(f"worst error {max(errs)/1e3:.1f} kHz <= 1 slot", max(errs) <= SLOT)


def main() -> int:
    test_cluster_function()
    test_comb_all_rates()
    test_two_tones()
    test_image()
    test_straddle_no_retrigger()
    test_det09_dense_25()
    test_det08_placements()
    n_ok = sum(checks)
    print(f"\nresult: {n_ok}/{len(checks)} checks passed")
    return 0 if n_ok == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
