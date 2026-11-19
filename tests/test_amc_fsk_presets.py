#!/usr/bin/env python3
"""
test_amc_fsk_presets.py: amc.classify on the FSK-family scenario presets.

Without the FSK-vs-linear check the engineering view reads a playing
4fsk_dmr.json as 32APSK/16APSK/16QAM at every box width, and most other FSK
presets as QAM/APSK/PSK too. The presets are short bursts the radio loops
(4fsk_dmr: 52 symbols in 13.3 ms, ~19 % silence), unlike the continuous,
wide-tone signals of the other AMC tests. Pinned here, through an offline copy of the classify_selection and
survey_classify front ends:

  (a) 4fsk_dmr at every operator box width (1.5-5x occupied) at 15 and 30 dB
  (b) every FSK/4FSK signal in scenarios/json (plus mod_coding_card C01-C03)
      at 1.5x/15 dB, 3x/20 dB and 5x/30 dB
  (c) the survey path (detect then -10 dB width then extract) on four presets
  (d) linear neighbours in mod_coding_card still read as their family
  (e) the pieces: per-run spans, DC removal before the join, the box guard,
      the two-start tone fit

Deterministic seeds. Runs under pytest or as a script (exit 0 = all pass).
"""
from __future__ import annotations

import functools
import glob
import json
import os
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import numpy as np                                                  # noqa: E402
import scipy.signal as ss                                           # noqa: E402

from dpmg.dsp import amc                                                          # noqa: E402
from dpmg.scenario import gen_scenario_json as g                                       # noqa: E402

FSK_MODS = ("FSK", "4FSK", "GFSK")
CAPTURE_S = 0.2                 # classify_selection's default capture length


@functools.lru_cache(maxsize=None)
def _mix(fname: str):
    """The looped TX buffer of a preset (as build_scenario sums it), first
    CAPTURE_S seconds, and its signal list."""
    spec = json.load(open(os.path.join(ROOT, "scenarios", "json", fname)))
    spec = g.resolve_signal_offsets(g.expand_users(spec))
    fs = float(spec["sample_rate_hz"])
    n = int(fs * CAPTURE_S)
    mix = np.zeros(n, np.complex128)
    sigs = []
    for sig in spec["signals"]:
        iq, _e = g.build_signal(sig, fs)
        mix += np.tile(iq, int(np.ceil(n / len(iq))))[:n]
        sigs.append(sig)
    return mix, fs, tuple(sigs)


def _centre(sig) -> float:
    """Where an operator centres the box: the visible signal (offset plus
    tone_center_hz, e.g. Bell-202 sits 1.7 kHz above its offset)."""
    return float(sig.get("offset_hz", 0.0)) + float(sig.get("tone_center_hz", 0.0))


def _noisy(fname: str, sig, snr_db: float, seed: int):
    """Capture plus complex AWGN at snr_db in-band over the signal's occupied
    width (gen_scenario_json.signal_bw_hz), signal power = amplitude^2."""
    mix, fs, _ = _mix(fname)
    rng = np.random.default_rng(seed)
    p = float(sig.get("amplitude", 0.8)) ** 2
    n0 = p / (10 ** (snr_db / 10.0) * g.signal_bw_hz(sig))
    s = np.sqrt(n0 * fs / 2.0)
    return mix + s * (rng.standard_normal(mix.size) + 1j * rng.standard_normal(mix.size)), fs


def _decimate_to(x, fs, target):
    """The whole-factor decimation loop of classify_selection / survey."""
    cur = float(fs)
    while cur / target >= 2.0 and x.size > 512:
        q = int(min(8, cur // target))
        if q < 2:
            break
        x = ss.decimate(x, q, ftype="fir", zero_phase=True)
        cur /= q
    return x, cur


def classify_selection(x, fs, f_c, box_hz):
    """dpmg_service._cmd_classify_selection, offline: box centre to DC,
    decimate to ~2.5x the box, amc.classify with the box width."""
    y = x * np.exp(-2j * np.pi * f_c / fs * np.arange(x.size))
    y, fs2 = _decimate_to(y, fs, max(box_hz * 2.5, 1.0))
    return amc.classify(y, fs2, box_hz)


def survey(x, fs, thr=12.0, min_sep=200e3, max_n=8, dc_guard=150e3):
    """dpmg_service._cmd_survey_classify._do, offline (same steps)."""
    from dpmg.dsp import tools_engine as te
    sigs, _floor = te.detect_signals(x, fs, thr_db=thr, min_sep_hz=min_sep, max_sigs=max_n + 6)
    f, psd = te.psd_db(x, fs)
    cand = []
    for s in sigs:
        ki = int(np.argmin(np.abs(f - s["freq_hz"])))
        lo = hi = ki
        while lo > 0 and psd[lo] > psd[ki] - 10.0:
            lo -= 1
        while hi < len(psd) - 1 and psd[hi] > psd[ki] - 10.0:
            hi += 1
        bw = max(float(f[hi] - f[lo]), fs * 2e-3)
        if abs(s["freq_hz"]) < dc_guard and bw < 40e3:
            continue
        cand.append((s["snr_db"], s["freq_hz"], bw))
    out = []
    for _snr, foff, bw in sorted(cand, reverse=True)[:max_n]:
        y = x * np.exp(-2j * np.pi * foff / fs * np.arange(x.size))
        y, cur = _decimate_to(y, fs, max(bw * 2.5, 1.0))
        out.append((foff, amc.classify(y, cur)))
    return out


def _fsk_cases():
    """(file, signal) for every FSK-family signal in scenarios/json."""
    out = []
    for path in sorted(glob.glob(os.path.join(ROOT, "scenarios", "json", "*.json"))):
        fname = os.path.basename(path)
        spec = json.load(open(path))
        if not any(g._canon_mod(s.get("modulation", "")) in FSK_MODS
                   for s in spec.get("signals", [])):
            continue
        for sig in _mix(fname)[2]:
            if g._canon_mod(sig["modulation"]) in FSK_MODS:
                out.append((fname, sig))
    return out


def _seed(*parts) -> int:
    """Deterministic noise seed per case (str hash() is salted per process)."""
    return zlib.crc32(repr(parts).encode())


def _sel(fname, sig, snr, fac, seed=0):
    x, fs = _noisy(fname, sig, snr, _seed(fname, sig.get("name"), sig.get("offset_hz"), snr, fac, seed))
    return classify_selection(x, fs, _centre(sig), fac * g.signal_bw_hz(sig))


def test_dmr_every_box_width():
    """The reported case: 4fsk_dmr read as 32APSK/16APSK/16QAM at every box."""
    sig = _mix("4fsk_dmr.json")[2][0]
    got = []
    for snr in (15.0, 30.0):
        for fac in (1.5, 2.0, 3.0, 4.0, 5.0):
            r = _sel("4fsk_dmr.json", sig, snr, fac)
            got.append((snr, fac, r["modulation"]))
    assert all(m in FSK_MODS for _s, _f, m in got), got
    assert sum(m == "4FSK" for _s, _f, m in got) >= 9, got


def test_every_fsk_preset():
    cases = _fsk_cases()
    assert len(cases) >= 20, len(cases)       # 4fsk_*, fsk_*, fsk*_sweep, C01-C03
    miss, exact, n = [], 0, 0
    for fname, sig in cases:
        for snr, fac in ((15.0, 1.5), (20.0, 3.0), (30.0, 5.0)):
            r = _sel(fname, sig, snr, fac)
            n += 1
            want = amc.canonical(g._canon_mod(sig["modulation"]))
            exact += r["modulation"] == want
            if r["family"] != "fsk":
                miss.append((fname, sig.get("name"), snr, fac, r["modulation"]))
    assert not miss, miss
    assert exact >= 0.9 * n, (exact, n)


def test_survey_path():
    """Survey all signals: the -10 dB width of each detection sets the cut."""
    got = []
    for fname in ("4fsk_dmr.json", "4fsk_wide.json", "fsk_9600_gmsk.json", "fsk_afsk_bell202.json"):
        sig = _mix(fname)[2][0]
        x, fs = _noisy(fname, sig, 20.0, 7)
        res = survey(x, fs)
        assert res, fname
        foff, r = min(res, key=lambda t: abs(t[0] - _centre(sig)))
        assert abs(foff - _centre(sig)) < 5e3, (fname, foff)
        got.append((fname, r["modulation"]))
    assert all(m in FSK_MODS for _f, m in got), got


def test_linear_neighbours_keep_their_family():
    """The FSK comparison runs for every capture; the linear bursts of
    mod_coding_card must still read as their own family (20 dB, 2x box)."""
    sigs = _mix("mod_coding_card.json")[2]
    bad = []
    for sig in sigs:
        mod = amc.canonical(g._canon_mod(sig["modulation"]))
        if mod in FSK_MODS:
            continue
        r = _sel("mod_coding_card.json", sig, 20.0, 2.0)
        if r["family"] != amc.FAMILY.get(mod):
            bad.append((sig["name"], r["modulation"]))
    assert len(bad) <= 1, bad


def test_active_runs_spans():
    """A survey-width cut of 4fsk_dmr (30 kHz, ~320-sample bursts) is split
    into its bursts, and the spans tile the joined array."""
    sig = _mix("4fsk_dmr.json")[2][0]
    x, fs = _noisy("4fsk_dmr.json", sig, 30.0, 3)
    y = x * np.exp(-2j * np.pi * _centre(sig) / fs * np.arange(x.size))
    y, fs2 = _decimate_to(y, fs, 2.5 * 6094.0)     # the survey's -10 dB width
    assert fs2 == 30e3, fs2
    j, spans = amc._active_runs(y, fs2)
    assert len(spans) >= 12, spans
    assert spans[0][0] == 0 and spans[-1][1] == j.size
    assert all(a1 == b0 for (_a0, b0), (a1, _b1) in zip(spans, spans[1:]))
    # continuous input: one span, returned as is
    t = np.exp(2j * np.pi * 0.01 * np.arange(20000))
    j2, spans2 = amc._active_runs(t, 1.0)
    assert spans2 == [(0, t.size)] and j2 is t


def test_dc_removed_before_join():
    """The joined runs of a looped burst are periodic and can carry a real
    line at 0 Hz; removing the joined mean smeared every tone. A cut 800 Hz
    off centre (the survey's detection offset) must still read 4FSK."""
    sig = _mix("4fsk_dmr.json")[2][0]
    mix, fs, _ = _mix("4fsk_dmr.json")
    y = mix * np.exp(-2j * np.pi * (_centre(sig) - 800.0) / fs * np.arange(mix.size))
    y, fs2 = _decimate_to(y, fs, 2.5 * 6094.0)
    j, _spans = amc._active_runs(y - y.mean(), fs2)
    assert abs(j.mean()) > 0.05 * np.sqrt(np.mean(np.abs(j) ** 2))  # the line is there
    assert amc.classify(y, fs2)["modulation"] == "4FSK"


def test_box_guard():
    sig = _mix("mod_coding_card.json")[2][2]                    # C03 4FSK, neighbours +-100 kHz
    box = 5.0 * g.signal_bw_hz(sig)
    x, fs = _noisy("mod_coding_card.json", sig, 20.0, 11)
    y = x * np.exp(-2j * np.pi * _centre(sig) / fs * np.arange(x.size))
    y, fs2 = _decimate_to(y, fs, box * 2.5)
    assert fs2 >= 3.0 * box                                     # the neighbours are in the capture
    yf, on = amc._box_guard(y, fs2, box)
    assert on and yf is not y
    assert amc.classify(y, fs2, box)["family"] == "fsk"
    # alone in the capture (4fsk_dmr, same box): untouched
    sig = _mix("4fsk_dmr.json")[2][0]
    x, fs = _noisy("4fsk_dmr.json", sig, 20.0, 12)
    z = x * np.exp(-2j * np.pi * _centre(sig) / fs * np.arange(x.size))
    z, fs3 = _decimate_to(z, fs, box * 2.5)
    z2, on2 = amc._box_guard(z, fs3, box)
    assert not on2 and z2 is z
    # a target wider than a too-tight box crosses its edge: that is not a
    # neighbour, and when a real neighbour forces the filter, the whole target
    # lobe is kept (here +/-2 kHz noise band in a +/-1.5 kHz box)
    rng = np.random.default_rng(5)
    fs4, m = 20e3, 40000
    wn = rng.standard_normal(m) + 1j * rng.standard_normal(m)
    tgt = np.convolve(wn, ss.firwin(255, 2000.0, fs=fs4), mode="same")
    tgt += 0.02 * (rng.standard_normal(m) + 1j * rng.standard_normal(m))
    _t, on3 = amc._box_guard(tgt, fs4, 3000.0)
    assert not on3
    nb = 0.5 * np.exp(2j * np.pi * 6000.0 / fs4 * np.arange(m))
    out, on4 = amc._box_guard(tgt + nb, fs4, 3000.0)
    assert on4

    def band(v, lo, hi):
        F = np.fft.fftfreq(v.size, 1 / fs4)
        V = np.abs(np.fft.fft(v)) ** 2
        return V[(np.abs(F) >= lo) & (np.abs(F) <= hi)].sum()
    assert band(out, 1500.0, 1900.0) > 0.8 * band(tgt, 1500.0, 1900.0)
    assert band(out, 5800.0, 6200.0) < 0.01 * band(nb, 5800.0, 6200.0)


def test_fit_fsk_uneven_tones():
    """Tones used unevenly (short looped payload) put the median a tone off
    centre; the two-start fit still finds the 4-level comb."""
    rng = np.random.default_rng(2)
    lv = rng.choice([-3.0, -1.0, 1.0, 3.0], size=600, p=[0.45, 0.35, 0.1, 0.1])
    F = 1000.0 * lv + 120.0 * rng.standard_normal(lv.size)
    ll4, c4, d4, s4 = amc._fit_fsk(F, np.array([-1.5, -0.5, 0.5, 1.5]))
    ll2, *_ = amc._fit_fsk(F, np.array([-0.5, 0.5]))
    assert abs(d4 - 2000.0) < 150.0, d4
    assert abs(c4) < 150.0, c4
    assert ll4 > ll2 + 0.1


def _main():
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    bad = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  [PASS] {name}")
        except AssertionError as e:
            bad += 1
            print(f"  [FAIL] {name}: {e}")
    print(f"{len(tests) - bad}/{len(tests)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(_main())
