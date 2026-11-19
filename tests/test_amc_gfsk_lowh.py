#!/usr/bin/env python3
"""
test_amc_gfsk_lowh.py: amc.classify on the bench GFSK (GFSK_LOWH).

The amc_hw_check GFSK burst is not the 5 kHz-spacing signal its MOD_PARAMS row
suggests: gen_scenario_json._tones() has no GFSK case, so the modem defaults
apply (tones 1200/2200 Hz: h ~0.21 at 4800 Bd, BT 0.3). In the 60 kHz
classify_selection slice its -12 dB width is ~1.4 kHz while power_bw read the
noise (~12 kHz), so the FSK branch never cut the band, found no symbol rate and
the inst-freq histogram (noise ripples) called it 4FSK. Pinned here:

  (a) bench replay at Es/N0 20 dB: GFSK >= 4/5, FSK and 4FSK 5/5
  (a2) the wide-index GFSK the row means (h ~1.04, tones patched in): >= 4/5
       at 20 and 15 dB
  (b) _pr_resolvable: Gaussian-ISI tones pass, rectangular FSK shows no ISI
  (c) occupied_bw is unchanged by the _occupied_span split

Deterministic seeds. Runs under pytest or as a script (exit 0 = all pass).
"""
from __future__ import annotations

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "bench"))

import numpy as np                                                  # noqa: E402

from dpmg.dsp import amc                                                          # noqa: E402


def _bench_replay(mods, esn0_db=20.0, reps=5, seed=0xA3C):
    """amc_hw_check --replay for `mods` (same rng order as its main())."""
    import amc_hw_check as H
    import checkout_common as cc
    rng = np.random.default_rng(seed)
    out = {}
    with tempfile.TemporaryDirectory() as wd:
        for m in mods:
            rs = H.replay_mod(m, reps, esn0_db, 300.0, rng, wd, cc.RATE_NARROW, int(cc.CENTER))
            out[m] = [r["pred"] for r in rs]
    return out


def test_bench_gfsk_family_20db():
    got = _bench_replay(["FSK", "GFSK", "4FSK"])
    assert got["GFSK"].count("GFSK") >= 4, got
    assert got["FSK"].count("FSK") == 5, got
    assert got["4FSK"].count("4FSK") == 5, got


def test_bench_gfsk_wide_index():
    """The burst the GFSK row means (tones +/-2.5 kHz, h ~1.04, BT 0.3): what
    the radio would key if gen_scenario_json._tones() honoured tone_spacing_hz
    for GFSK. Patched here only; the generator is unchanged."""
    from dpmg.scenario import gen_scenario_json as g
    orig = g._tones

    def tones(sig):
        if str(sig["modulation"]).upper() == "GFSK":
            sp = float(sig.get("tone_spacing_hz", 1000.0))
            c = float(sig.get("tone_center_hz", 0.0))
            return {"tone0": c - sp / 2.0, "tone1": c + sp / 2.0}
        return orig(sig)

    g._tones = tones
    try:
        for esn0 in (20.0, 15.0):
            got = _bench_replay(["GFSK"], esn0_db=esn0)
            assert got["GFSK"].count("GFSK") >= 4, (esn0, got)
    finally:
        g._tones = orig


def _symbol_freqs(b, shaped, rng, noise=0.15):
    """Per-symbol mean frequency (tone units +/-1) of an NRZ stream at 8
    samples/symbol, optionally Gaussian-shaped (BT 0.3, the modulator's
    kernel), plus white per-symbol noise."""
    sps = 8
    nrz = np.repeat(2.0 * b - 1.0, sps)
    if shaped:
        sigma = 0.1325 * sps / 0.3
        n = np.arange(-int(3 * sigma), int(3 * sigma) + 1)
        k = np.exp(-0.5 * (n / sigma) ** 2)
        nrz = np.convolve(nrz, k / k.sum(), mode="same")
    F = nrz.reshape(-1, sps).mean(axis=1)
    return F + noise * rng.standard_normal(F.size)


def test_partial_response_gate():
    rng = np.random.default_rng(7)
    b = rng.integers(0, 2, 2000).astype(float)
    info = {}
    assert amc._pr_resolvable(_symbol_freqs(b, True, rng, noise=0.3), 0.0, info), info
    assert info["fsk_pr_b"] > 0.1, info
    info = {}
    assert not amc._pr_resolvable(_symbol_freqs(b, False, rng), 0.0, info), info
    assert abs(info["fsk_pr_b"]) < 0.05, info
    assert not amc._pr_resolvable(np.zeros(16), 0.0, {})


def test_occupied_span_matches_occupied_bw():
    rng = np.random.default_rng(3)
    fs = 60e3
    n = np.arange(12000)
    x = np.exp(2j * np.pi * 1700.0 / fs * n) + 0.05 * (rng.standard_normal(n.size)
                                                       + 1j * rng.standard_normal(n.size))
    c, w = amc._occupied_span(x, fs)
    assert w == amc.occupied_bw(x, fs)
    assert abs(c - 1700.0) < 100.0, c


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
