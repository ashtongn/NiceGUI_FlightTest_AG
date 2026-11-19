#!/usr/bin/env python3
"""
test_amc_carrier.py: amc.classify carrier recovery.

On the G40 cross-card link the TX and RX LOs are not coherent; the legacy
classifier (global cumulants, 4th-power de-rotation) reads BPSK as QPSK there.
These checks pin the carrier-recovery path against the project's own TX
modulators with the impairments of amc_eval.PROFILES:

  (a) BPSK under a few-hundred-Hz CFO + phase wander classifies as BPSK
  (b) every registry modulation classifies exactly under the G40 profile
  (c) the reported CFO matches the applied one (PSK and QAM orders)
  (d) carrier_for restores BPSK's 2-fold symmetry (|E y^2| ~ 1)
  (e) pi/4-QPSK vs QPSK stay apart with an offset (the Rs/8 ambiguity)
  (f) FSK / CSS are unaffected by CFO and report no carrier order
  (g) the wire 'features' still fit the service's 1400 B ack
  (h) short or noise-only input does not raise
  (i) the gen_scenario burst on the classify_selection path (radio proxy)

Deterministic seeds. Runs under pytest or as a script (exit 0 = all pass).
"""
from __future__ import annotations

import json
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "bench"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import numpy as np                                                  # noqa: E402

from dpmg.dsp import amc                                                          # noqa: E402
import amc_eval as E                                                # noqa: E402


def _case(mod, snr, prof="g40", seed=0, cfo_fixed=None):
    random.seed(seed)
    np.random.seed(seed)
    rng = np.random.RandomState(seed + 1)
    iq, fs = E.gen(mod)
    p = dict(E.PROFILES[prof])
    if cfo_fixed is not None:
        p["cfo"] = 0.0
    iq, f0 = E.impair(iq, fs, rng=rng, **p)
    if cfo_fixed is not None:
        iq = (iq * np.exp(2j * np.pi * cfo_fixed / fs * np.arange(iq.size))).astype(np.complex64)
        f0 = cfo_fixed
    return E.awgn(iq, snr), fs, f0


def test_bpsk_with_cfo_is_bpsk():
    got = [amc.classify(*_case("BPSK", 15, "g40", seed=s)[:2])["modulation"] for s in range(5)]
    assert got.count("BPSK") == 5, got


def test_bpsk_harsh_carrier():
    got = [amc.classify(*_case("BPSK", 10, "harsh", seed=10 + s)[:2])["modulation"] for s in range(3)]
    assert got.count("BPSK") == 3, got


def test_every_modulation_g40_profile():
    miss = []
    for i, mod in enumerate(E.ALL_MODS):
        for s in range(2):
            iq, fs, _ = _case(mod, 15, "g40", seed=100 + 7 * i + s)
            pred = amc.classify(iq, fs)["modulation"]
            if pred != amc.canonical(mod):
                miss.append((mod, pred))
    # 32 classifications; allow one miss (the offline sweep runs 10/class)
    assert len(miss) <= 1, miss


def test_cfo_estimate():
    for mod, cfo in (("QPSK", 250.0), ("8PSK", -180.0), ("16QAM", 120.0), ("BPSK", 400.0)):
        iq, fs, f0 = _case(mod, 20, "none", seed=3, cfo_fixed=cfo)
        r = amc.classify(iq, fs)
        assert r["modulation"] == mod, (mod, r["modulation"])
        assert r["carrier"]["order"] is not None, r["carrier"]
        assert abs(r["carrier"]["cfo_hz"] - f0) < 5.0, (mod, r["carrier"], f0)


def test_carrier_for_restores_bpsk_symmetry():
    iq, fs, _ = _case("BPSK", 15, "g40", seed=5)
    x = amc._norm(iq)
    rs, _p = amc.baud_rate(x, fs)
    assert abs(rs - E.SYM_RATE) < 0.01 * E.SYM_RATE, rs
    y0 = amc.symbols(x, fs, rs)
    yn0 = y0 / np.abs(y0)
    before = abs(np.mean(yn0 ** 2))
    y, _cfo, _pstd, prom = amc.carrier_for(y0, rs, 2, "phase")
    yn = y / np.abs(y)
    after = abs(np.mean(yn ** 2))
    assert before < 0.5, before          # the spinning constellation of the bug
    assert after > 0.9, after
    assert prom > amc.PSK_LINE_PROM


def test_pi4_vs_qpsk_with_offset():
    for s, cfo in enumerate((-280.0, 150.0, 290.0)):
        for mod in ("pi/4-QPSK", "QPSK"):
            iq, fs, _ = _case(mod, 15, "none", seed=40 + s, cfo_fixed=cfo)
            pred = amc.classify(iq, fs)["modulation"]
            assert pred == mod, (mod, cfo, pred)


def test_fsk_and_css_unaffected():
    for mod in ("FSK", "4FSK", "GFSK", "CSS"):
        for cfo in (0.0, 300.0):
            iq, fs, _ = _case(mod, 15, "none", seed=60, cfo_fixed=cfo)
            r = amc.classify(iq, fs)
            assert r["modulation"] == mod, (mod, cfo, r["modulation"])
            assert r["carrier"]["order"] is None, (mod, r["carrier"])


def test_wire_features_fit_datagram():
    biggest = 0
    for i, mod in enumerate(E.ALL_MODS):
        iq, fs, _ = _case(mod, 15, "g40", seed=200 + i)
        res = amc.classify(iq, fs)
        feats = {k: (round(v, 4) if isinstance(v, float) else v)
                 for k, v in res["features"].items()}
        ack = {"modulation": res["modulation"], "family": res["family"],
               "confidence": res["confidence"], "sym_rate_hz": res["sym_rate_hz"],
               "rf_center_hz": 425325000.0, "sel_bw_hz": 123456.7,
               "capture_rate_hz": 240000.0, "n": feats.get("n", 0), "features": feats}
        biggest = max(biggest, len(json.dumps(ack, separators=(",", ":"))))
    assert biggest < 900, biggest         # + header/framing stays under 1400 B


def test_degenerate_inputs():
    r = amc.classify(np.zeros(100, np.complex64), 1e5)
    assert r["modulation"] == "unknown"
    rng = np.random.default_rng(1)
    n = (rng.standard_normal(20000) + 1j * rng.standard_normal(20000)).astype(np.complex64)
    r = amc.classify(n, 1e5)
    assert isinstance(r["modulation"], str) and 0.0 <= r["confidence"] <= 1.0


def test_radio_path_proxy():
    """gen_scenario_json bursts (exactly what the G40 keys) through the
    classify_selection front end, G40 carrier profile."""
    E.OPTS["frontend"] = "service"
    try:
        r = E.run_matrix(["BPSK", "QPSK", "8PSK", "16QAM", "FSK", "DSSS"], 20.0, 2,
                         E.PROFILES["g40"], seed=11)
    finally:
        E.OPTS["frontend"] = "none"
    assert r["acc"]["BPSK"] == 1.0, r["confusion"]["BPSK"]
    assert r["overall"] >= 0.8, r["confusion"]


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
