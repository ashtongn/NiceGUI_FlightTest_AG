"""
test_scenario_peak_fs.py: the scenario builder's peak_fs setting.

The builder sums the signals at their relative amplitudes and scales the mix
so its peak is peak_fs (default 0.45). Checks the default, the spec key, the
argument overriding the spec, that relative levels survive the scaling, the
range check and bench warning in validate_spec, the planner passing the step's
value to gen_scenario, and the bench --peak-fs option.
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "bench"))
import numpy as np
import pytest

from dpmg.scenario import gen_scenario_json as g

FS = 1_920_000.0


def _spec(peak=None, two=False):
    sigs = [{"modulation": "FSK", "symbol_rate": 4800, "tone_spacing_hz": 5000,
             "offset_hz": 100_000, "message": "PEAK A", "amplitude": 0.8}]
    if two:
        sigs.append({"modulation": "CW", "offset_hz": -200_000, "amplitude": 0.2,
                     "duration_s": 0.05})
    s = {"name": "peak_t", "sample_rate_hz": FS, "center_freq_hz": 425_025_000.0,
         "loop_s": 0.05, "signals": sigs}
    if peak is not None:
        s["peak_fs"] = peak
    return s


def _build(spec, peak_fs=None):
    with tempfile.TemporaryDirectory() as d:
        g.build_scenario(spec, d, peak_fs)
        iq = np.fromfile(os.path.join(d, "peak_t.iq.bin"), dtype=np.complex64)
        plan = json.load(open(os.path.join(d, "peak_t.plan.json")))
    return iq, plan


def test_default_peak_is_045():
    iq, plan = _build(_spec())
    assert np.max(np.abs(iq)) == pytest.approx(0.45, abs=1e-4)
    assert plan["compose"]["peak_fs"] == pytest.approx(0.45)


def test_spec_peak_used():
    iq, plan = _build(_spec(peak=0.3))
    assert np.max(np.abs(iq)) == pytest.approx(0.3, abs=1e-4)
    assert plan["compose"]["peak_fs"] == pytest.approx(0.3)


def test_argument_overrides_spec():
    iq, _ = _build(_spec(peak=0.3), peak_fs=0.5)
    assert np.max(np.abs(iq)) == pytest.approx(0.5, abs=1e-4)


def test_relative_levels_kept():
    lo, _ = _build(_spec(two=True), peak_fs=0.45)
    hi, _ = _build(_spec(two=True), peak_fs=0.9)
    # the same mix, only scaled: 0.9 / 0.45 = 2 everywhere
    assert np.allclose(hi, 2.0 * lo, atol=1e-5)


@pytest.mark.parametrize("bad", [0.0, 0.01, 1.01, 1.5, "loud"])
def test_out_of_range_refused(bad):
    with pytest.raises(ValueError):
        g.resolve_peak_fs(_spec(), bad)
    v = g.validate_spec(_spec(), FS, peak_fs=bad)
    assert not v["ok"] and any("peak_fs" in e for e in v["errors"])


def test_up_to_full_scale_allowed():
    v = g.validate_spec(_spec(), FS, peak_fs=0.7)
    assert v["ok"] and v["peak_fs"] == pytest.approx(0.7)
    assert not any("peak_fs" in w for w in v["warnings"])
    v = g.validate_spec(_spec(), FS, peak_fs=1.0)
    assert v["ok"] and v["peak_fs"] == pytest.approx(g.PEAK_FS_CEILING)
    iq, _ = _build(_spec(), peak_fs=1.0)
    assert np.max(np.abs(iq)) == pytest.approx(g.PEAK_FS_CEILING, abs=1e-4)
    v = g.validate_spec(_spec(), FS)
    assert v["ok"] and v["peak_fs"] == pytest.approx(0.45)
    assert not any("peak_fs" in w for w in v["warnings"])


def test_planner_passes_peak():
    from dpmg.mission import mission_runner as mr
    from dpmg.mission import catalog
    fields = {f.key: f for f in catalog.get("scenario_json").fields}
    assert fields["peak_fs"].default == pytest.approx(0.45)
    step = {"id": "s1", "radio": "g40", "technique": "scenario_json",
            "params": {"source": "console", "spec": _spec(), "peak_fs": 0.35}}
    start, _stop = mr.step_commands(step)
    gp = next(p for n, p in start if n == "gen_scenario")
    assert gp["peak_fs"] == pytest.approx(0.35)
    step["params"].pop("peak_fs")
    start, _stop = mr.step_commands(step)
    gp = next(p for n, p in start if n == "gen_scenario")
    assert "peak_fs" not in gp


def test_bench_peak_option(monkeypatch):
    import checkout_common as cc
    monkeypatch.delenv(cc.PEAK_FS_ENV, raising=False)
    assert cc.bench_peak_fs() is None
    ap = argparse.ArgumentParser()
    cc.add_clock_args(ap)
    a = ap.parse_args(["--peak-fs", "0.9"])
    assert a.peak_fs == pytest.approx(0.9)
    assert cc.bench_peak_fs() == pytest.approx(0.9)
    monkeypatch.delenv(cc.PEAK_FS_ENV, raising=False)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
