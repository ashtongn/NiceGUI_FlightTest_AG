"""
DET-02 time splits: one burst per continuously keyed emitter at 1.92 MSps (test_det02_time_splits.py)
====================================================================================================
With slot clustering but no hold-over, the 8-tone
comb keyed TRx2B then 50 dB pad then TRx1A gave 8 bursts at 7.68 MSps but 20 at
1.92 MSps (tests/data/det02/det02_comb_move2_*.json):
  * 16 = every tone closed and reopened once: the cluster dropped out of the
    detector for one tick (5+1+13 and 7+1+11 ticks of a 19-tick key);
  * 4 = one-tick islands 1-2 inactive slots off the +-300 kHz tone's span,
    7-14 dB below it, on a tick where that tone was missing.
7.68 run 1 shows the same split on the +-300 kHz pair (10 bursts).

This pins BurstTracker's hold-over (hold_ticks) + island rule (island_slots,
tied to cluster_margin_db), offline:
  * replay of the four measured comb runs: the JSONs hold per-burst records
    only (offset, bw, snr, duration, det_index in run 2), so the per-tick
    per-slot detections are rebuilt from them (tones = their 2..9-slot span,
    islands = one slot, one tick); the legacy settings (hold 0, no islands)
    must reproduce the measured counts exactly (20 / 20 / 10 / 8), the
    defaults must give 8 +- 1 at both rates, and the islands are swept over
    every tick after key-on;
  * synthetic IQ through the same EnergyDetector.process + BurstTracker the
    service tick runs (50 ms frames like the Epiq drain batch): a comb with a
    one-frame dropout and a one-frame island at 1.92 and 7.68: 8;
  * a pulsed emitter whose off time exceeds the hold-over: separate bursts,
    each as long as its on time; off for one tick: one burst;
  * two real emitters that stop and restart: two bursts each; a weaker real
    neighbour that persists still gets its burst (start back-dated); a skirt
    island more than the cluster margin down does not; flush() closes held
    bursts at their first missed tick.
Exit 0 = pass.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np

from dpmg.dsp.energy_detector import (EnergyDetector, BurstTracker, Detection,
                             DEFAULT_HOLD_TICKS, DEFAULT_ISLAND_SLOTS)
import test_det02_slot_clustering as d02

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RES = os.path.join(ROOT, "tests/data/det02")
CENTER = d02.CENTER
SLOT = 20e3
COMB = [-700e3, -500e3, -300e3, -100e3, 100e3, 300e3, 500e3, 700e3]
SKIRT_STEP_DB = 6.0        # level drop per slot outward inside a replayed tone span

checks = []


def chk(name, ok, detail=""):
    checks.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")


def mk(fs, off_hz, p):
    """Per-slot Detection on the EnergyDetector grid of rate fs (slot s
    centred at -fs/2 + (s + 0.5) * 20 kHz)."""
    s = int(round((off_hz + fs / 2.0) / SLOT - 0.5))
    return Detection(slot=s, center_hz=CENTER - fs / 2.0 + (s + 0.5) * SLOT,
                     power_db=float(p), bin_lo=0, bin_hi=0)


# replay

def _tick_s(durs_ms):
    """Detector tick of a run (0.2 s sleep + processing): the period that puts
    every burst duration closest to a whole number of ticks."""
    best = None
    for T in np.arange(0.200, 0.230, 0.0002):
        err = sum(abs(d / 1e3 / T - round(d / 1e3 / T)) for d in durs_ms)
        if best is None or err < best[0]:
            best = (err, float(T))
    return best[1]


def _tone_slots(fs, tone, peak_off, bw, snr):
    """Per-slot detections of a comb tone sitting on a slot edge: the peak slot
    the service reported at snr, the other straddling slot 1 dB down, the rest
    of the reported bw as a skirt falling SKIRT_STEP_DB per slot."""
    n = max(2, int(round(bw / SLOT)))
    other = 2 * tone - peak_off                      # the slot across the edge
    dets = [mk(fs, peak_off, snr), mk(fs, other, snr - 1.0)]
    lo_off, hi_off = min(peak_off, other), max(peak_off, other)
    k = 1
    while len(dets) < n:
        side = lo_off - k * SLOT if len(dets) % 2 == 0 else hi_off + k * SLOT
        dets.append(mk(fs, side, snr - 1.0 - SKIRT_STEP_DB * k))
        if len(dets) % 2 == 0:
            k += 1
    return dets


def rebuild(path):
    """Rebuild the per-tick per-slot detections of one measured comb run.
    Returns (fs, ticks: list[list[Detection]], tick_s, islands, measured_count)."""
    j = json.load(open(path))
    fs = float(j["rate_hz"])
    case = next(c for c in j["cases"] if c["kind"] == "comb")
    dets = case["dets"]
    has_idx = all("idx" in d for d in dets)
    order = sorted(range(len(dets)), key=lambda i: dets[i]["idx"]) if has_idx else list(range(len(dets)))
    T = _tick_s([d["dur_ms"] for d in dets])
    nt = lambda d: int(round(d["dur_ms"] / 1e3 / T))
    tones = {t: [] for t in COMB}
    islands = []
    for i in order:
        d = dets[i]
        tone = min(COMB, key=lambda t: abs(t - d["off_hz"]))
        (tones[tone] if abs(tone - d["off_hz"]) <= SLOT else islands).append(d)
    n_key = max(sum(nt(b) for b in bs) + len(bs) - 1 for bs in tones.values())
    ticks = [[] for _ in range(n_key + 2)]
    gap_tick = None
    close_tick = {}                                  # det index: tick it closed on
    for tone, bs in tones.items():
        t0 = 0
        for b in bs:                                 # first, (one-tick gap), second
            for k in range(t0, t0 + nt(b)):
                ticks[k] += _tone_slots(fs, tone, b["off_hz"], b["bw_hz"], b["snr_db"])
            if t0 == 0 and len(bs) > 1:
                gap_tick = nt(b)
            close_tick[id(b)] = t0 + nt(b)
            t0 += nt(b) + 1
    # islands: on the tick the run-2 det_index order puts them (they closed after
    # every tone burst = seen on the last key tick); run 1 carries no order, so
    # on the common gap tick (the test below also sweeps every tick)
    for b in islands:
        at = (n_key - nt(b)) if has_idx else (gap_tick if gap_tick is not None else 1)
        for k in range(at, at + nt(b)):
            ticks[k].append(mk(fs, b["off_hz"], b["snr_db"]))
    return fs, ticks, T, islands, case["n_detections"]


def replay(ticks, T, **kw):
    tr = BurstTracker(**kw)
    closed = []
    for k, dets in enumerate(ticks):
        closed += tr.update(dets, k * T)
    closed += tr.flush(len(ticks) * T)
    return closed


def test_replay():
    print("\nreplay of the measured comb runs (TRx2B -> 50 dB -> TRx1A, 2026-10-18)")
    legacy = dict(hold_ticks=0, island_slots=-1)
    for name in ("det02_comb_move2_1920000", "det02_comb_move2_1920000_run2",
                 "det02_comb_move2_7680000", "det02_comb_move2_7680000_run2"):
        path = os.path.join(RES, name + ".json")
        if not os.path.exists(path):
            chk(f"{name}: result file present", False, path)
            continue
        fs, ticks, T, islands, measured = rebuild(path)
        old = replay(ticks, T, **legacy)
        chk(f"{name}: rebuild + legacy tracker reproduces the measured {measured} bursts",
            len(old) == measured, f"got {len(old)} (tick {T * 1e3:.1f} ms, {len(islands)} islands)")
        new = replay(ticks, T)
        found = [t for t in COMB if any(abs((e.center_hz - CENTER) - t) <= SLOT for e in new)]
        chk(f"{name}: hold-over + islands -> 8 +- 1 bursts ({len(new)})", 7 <= len(new) <= 9,
            f"{sorted(round((e.center_hz - CENTER) / 1e3) for e in new)} kHz")
        chk(f"{name}: all 8 tones found within one slot ({len(found)}/8)", len(found) == 8)
        n_key = max(k for k, d in enumerate(ticks) if d) + 1
        durs = sorted(round((e.t_stop - e.t_start) / T) for e in new)
        chk(f"{name}: burst durations span the key ({durs} ticks of {n_key})",
            all(n_key - 1 <= x <= n_key for x in durs))
        if islands:
            # sweep the islands over every tick after key-on (key-on itself is
            # the documented limit: nothing is open yet to judge them against)
            worst = 0
            for at in range(1, n_key):
                tk = [[d for d in ts if abs((d.center_hz - CENTER) - min(COMB, key=lambda t: abs(t - (d.center_hz - CENTER)))) <= SLOT]
                      for ts in ticks]
                for b in islands:
                    tk[at].append(mk(fs, b["off_hz"], b["snr_db"]))
                worst = max(worst, len(replay(tk, T)))
            chk(f"{name}: islands on any tick 1..{n_key - 1} -> still <= 9 (worst {worst})", worst <= 9)


# synthetic IQ

def test_comb_dropout_iq():
    print("\nsynthetic comb IQ, one dropout frame + one island frame (50 ms frames)")
    for fs in (1.92e6, 7.68e6):
        rng = np.random.default_rng(int(fs) % 1000)
        N = int(0.05 * fs)                                   # one Epiq drain batch
        amp = 0.3
        sigma = d02.sigma_for_snr(fs, amp, 60.0)
        tones = [(o, amp) for o in COMB]
        # islands are slot-centred while the comb tones sit in the slot-edge guard
        # gap (seen ~30-40 dB down through their skirt), so -32 dB here reads a few
        # dB below the tones, as the 1.92 islands did (7-14 dB below)
        island = [(250e3, amp * 10 ** (-32 / 20.0)), (-350e3, amp * 10 ** (-32 / 20.0))]
        frames = []
        for k in range(8):
            if k == 3:                                       # the one-tick dropout
                frames.append(d02.synth(fs, N, [], rng, sigma))
            elif k == 5:                                     # tones + two islands
                frames.append(d02.synth(fs, N, tones + island, rng, sigma))
            else:
                frames.append(d02.synth(fs, N, tones, rng, sigma))
        for label, kw, want in (("legacy (hold 0, no islands)", dict(hold_ticks=0, island_slots=-1), None),
                                ("default", {}, 8)):
            det = EnergyDetector(fs=fs, center_hz=CENTER, slot_bw=SLOT, nfft=d02.NFFT,
                                 threshold_db=d02.THRESH_DB)
            tr = BurstTracker(**kw)
            closed = []
            for k, fr in enumerate(frames):
                closed += tr.update(det.process(fr), 0.2 * k)
            closed += tr.update([], 0.2 * len(frames))
            closed += tr.flush(0.2 * (len(frames) + 1))
            if want is None:
                chk(f"{fs / 1e6:.2f} MSps {label}: splits + islands show ({len(closed)} > 9)", len(closed) > 9)
            else:
                found = [t for t in COMB if any(abs((e.center_hz - CENTER) - t) <= SLOT for e in closed)]
                chk(f"{fs / 1e6:.2f} MSps {label}: {len(closed)} bursts, 8 +- 1, tones {len(found)}/8",
                    7 <= len(closed) <= 9 and len(found) == 8)
                chk(f"{fs / 1e6:.2f} MSps durations = 8 frames",
                    all(abs((e.t_stop - e.t_start) - 1.6) < 1e-6 for e in closed if
                        any(abs((e.center_hz - CENTER) - t) <= SLOT for t in COMB)))


def test_pulsed_iq():
    print("\npulsed tone @ 1.92 MSps through EnergyDetector + BurstTracker (tick 0.2 s)")
    fs = 1.92e6
    N = int(0.05 * fs)
    rng = np.random.default_rng(7)
    amp = 0.3
    sigma = d02.sigma_for_snr(fs, amp, 45.0)
    off = 310e3                                              # slot-centred
    for off_ticks, hold, want in ((3, DEFAULT_HOLD_TICKS, 2), (2, DEFAULT_HOLD_TICKS, 2),
                                  (1, DEFAULT_HOLD_TICKS, 1), (1, 0, 2), (2, 2, 1), (3, 2, 2)):
        pattern = [1] * 4 + [0] * off_ticks + [1] * 4
        det = EnergyDetector(fs=fs, center_hz=CENTER, slot_bw=SLOT, nfft=d02.NFFT,
                             threshold_db=d02.THRESH_DB)
        tr = BurstTracker(hold_ticks=hold)
        closed = []
        for k, on in enumerate(pattern):
            closed += tr.update(det.process(d02.synth(fs, N, [(off, amp)] if on else [], rng, sigma)), 0.2 * k)
        for k in range(len(pattern), len(pattern) + hold + 1):   # silence until it closes
            closed += tr.update(det.process(d02.synth(fs, N, [], rng, sigma)), 0.2 * k)
        at = [e for e in closed if abs((e.center_hz - CENTER) - off) <= SLOT]
        durs = [round(e.t_stop - e.t_start, 3) for e in at]
        ok = len(at) == want and len(closed) == want
        if want == 2:
            ok = ok and durs == [0.8, 0.8]                  # each burst = its on time
        else:
            ok = ok and durs == [round(0.2 * len(pattern), 3)]
        chk(f"off {off_ticks} tick(s), hold {hold} -> {want} burst(s)", ok, f"durations {durs} s")


# tracker units

def test_tracker_units():
    print("\ntracker units (per-slot detections, 1.92 MSps grid)")
    fs = 1.92e6
    tone = lambda off, p=30.0: [mk(fs, off - 10e3, p), mk(fs, off + 10e3, p - 1)]

    # two real emitters 200 kHz apart that stop and restart (off 3 ticks): 4
    tr = BurstTracker()
    seq = [tone(-100e3) + tone(100e3)] * 3 + [[]] * 3 + [tone(-100e3) + tone(100e3)] * 3 + [[]] * 2
    closed = []
    for k, d in enumerate(seq):
        closed += tr.update(d, 0.2 * k)
    chk("two emitters off 3 ticks then back -> 2 bursts each (4)", len(closed) == 4,
        f"{[(round((e.center_hz - CENTER) / 1e3), round(e.t_start, 1)) for e in closed]}")

    # a weaker real neighbour (10 dB down, 2 inactive slots away) appearing while
    # the strong one is open: pending one tick, then opens with t_start back-dated
    tr = BurstTracker()
    strong = tone(100e3)
    weak = [mk(fs, 170e3, 20.0)]
    closed = []
    seq = [strong] * 2 + [strong + weak] * 4 + [[]] * 2
    for k, d in enumerate(seq):
        closed += tr.update(d, 0.2 * k)
    w = [e for e in closed if abs(e.center_hz - CENTER - 170e3) < SLOT / 2]
    chk("persistent weaker neighbour -> its own burst", len(closed) == 2 and len(w) == 1)
    chk("its start is back-dated to the first sighting", w and abs(w[0].t_start - 0.4) < 1e-9 and
        abs(w[0].t_stop - 1.2) < 1e-9, f"{[(e.t_start, e.t_stop) for e in w]}")

    # the same neighbour for one tick: no burst (island)
    tr = BurstTracker()
    closed = []
    seq = [strong] * 2 + [strong + weak] + [strong] * 2 + [[]] * 2
    for k, d in enumerate(seq):
        closed += tr.update(d, 0.2 * k)
    chk("one-tick weaker neighbour -> no burst (1 total)", len(closed) == 1)

    # a skirt island more than the cluster margin below, persisting: no burst
    tr = BurstTracker()
    closed = []
    seq = [strong] * 2 + [strong + [mk(fs, 170e3, 5.0)]] * 4 + [[]] * 2
    for k, d in enumerate(seq):
        closed += tr.update(d, 0.2 * k)
    chk("persistent island > margin below -> folded (1 total)", len(closed) == 1)

    # a stronger new neighbour opens at once (no pending)
    tr = BurstTracker()
    closed = []
    seq = [strong] * 2 + [strong + [mk(fs, 170e3, 40.0)]] + [[]] * 2
    for k, d in enumerate(seq):
        closed += tr.update(d, 0.2 * k)
    s = [e for e in closed if abs(e.center_hz - CENTER - 170e3) < SLOT / 2]
    chk("stronger new neighbour -> burst at once", len(closed) == 2 and s and abs(s[0].t_start - 0.4) < 1e-9)

    # a weaker emitter farther than island_slots: opens at once
    tr = BurstTracker()
    closed = []
    far = 110e3 + 20e3 * (DEFAULT_ISLAND_SLOTS + 2)
    seq = [strong] * 2 + [strong + [mk(fs, far, 20.0)]] + [[]] * 2
    for k, d in enumerate(seq):
        closed += tr.update(d, 0.2 * k)
    chk(f"weaker emitter > {DEFAULT_ISLAND_SLOTS} inactive slots away -> one-tick burst kept", len(closed) == 2)

    # flush closes a held burst at its first missed tick
    tr = BurstTracker()
    tr.update(strong, 0.0); tr.update(strong, 0.2); tr.update([], 0.4)
    fl = tr.flush(0.6)
    chk("flush closes a held burst, t_stop = first miss", len(fl) == 1 and abs(fl[0].t_stop - 0.4) < 1e-9)

    # DET-10 shape: a 2 s key at the 0.2 s tick: one burst, duration = key
    tr = BurstTracker()
    closed = []
    for k in range(10):
        closed += tr.update(strong, 0.2 * k)
    for k in range(10, 13):
        closed += tr.update([], 0.2 * k)
    chk("2 s key -> 1 burst of 2.0 s (DET-10 duration unchanged)",
        len(closed) == 1 and abs(closed[0].t_stop - closed[0].t_start - 2.0) < 1e-9)


def main() -> int:
    test_tracker_units()
    test_replay()
    test_comb_dropout_iq()
    test_pulsed_iq()
    n_ok = sum(checks)
    print(f"\nresult: {n_ok}/{len(checks)} checks passed")
    return 0 if n_ok == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
