"""Tests for the payload checkout sweep generator."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from dpmg.mission import mission_file as mf
from dpmg.mission import payload_sweep as ps


def test_points_count_and_ends():
    pts = ps.sweep_points(800e6, 2500e6, 5)
    assert len(pts) == 5
    assert pts[0] == 800e6 and pts[-1] == 2500e6
    # evenly spaced
    gaps = [pts[i + 1] - pts[i] for i in range(len(pts) - 1)]
    assert max(gaps) - min(gaps) < 1.0


def test_points_edge_cases():
    assert ps.sweep_points(800e6, 2500e6, 0) == []
    assert ps.sweep_points(800e6, 2500e6, 1) == [1650e6]      # midpoint
    # handed over backwards, still low to high
    pts = ps.sweep_points(2500e6, 800e6, 3)
    assert pts[0] == 800e6 and pts[-1] == 2500e6


def test_steps_shape_and_order():
    steps = ps.sweep_steps("G40-X", 800e6, 2500e6, 6, dwell_s=10.0, gap_s=2.0,
                           start_s=0.0, tx_atten_db=40.0)
    assert len(steps) == 6
    for s in steps:
        assert s["technique"] == "tools.tone"
        assert s["radiates"] is True
        assert s["radio"] == "G40-X"
        assert s["params"]["tx_atten_db"] == 40.0
        # one retune per step: a blank TX frequency keeps TX on the step's centre
        assert s["params"]["center_hz"] > 0 and s["params"]["tx_center_hz"] is None
        assert "ports" not in s
    # back to back, no overlap, 12 s apart (10 dwell + 2 gap)
    starts = [s["schedule"]["start_s"] for s in steps]
    assert starts == sorted(starts)
    for a, b in zip(steps, steps[1:]):
        assert b["schedule"]["start_s"] >= a["schedule"]["stop_s"]
    ids = [s["id"] for s in steps]
    assert len(set(ids)) == len(ids)          # unique


def test_generated_sweep_validates_clean():
    m = mf.new_mission("Payload sweep test")
    m["authorization"] = {"reference": "TEST-2026", "note": "bench"}
    m["radios"] = [{"id": "G40-X", "type": "g40", "techniques": ["rx", "tx"],
                    "sample_rate": 2_000_000}]
    m["steps"] = ps.sweep_steps("G40-X", 800e6, 2500e6, 8, dwell_s=10.0)
    conflicts = mf.validate(m)
    errors = [c for c in conflicts if getattr(c, "level", "error") == "error"]
    assert errors == [], [str(c) for c in errors]


def test_span_helper():
    assert ps.sweep_span_s(6, 10.0, 2.0) == 6 * 10.0 + 5 * 2.0
    assert ps.sweep_span_s(0, 10.0) == 0.0


def test_default_gap_leaves_room_to_build():
    steps = ps.sweep_steps("G40-X", 800e6, 2500e6, 4, dwell_s=10.0)
    for a, b in zip(steps, steps[1:]):
        assert b["schedule"]["start_s"] - a["schedule"]["stop_s"] == ps.MIN_GAP_S
    assert ps.gap_note(ps.MIN_GAP_S) == "" and ps.gap_note(10) == ""
    assert "late" in ps.gap_note(1.0)


def test_tx_jack_goes_on_every_step():
    steps = ps.sweep_steps("G40-X", 900e6, 1000e6, 3, dwell_s=10.0, tx_port="TRx2B", rx_none=True)
    assert all(s["ports"] == {"rx": "none", "tx": "TRx2B"} for s in steps)
    steps = ps.sweep_steps("X40-X", 900e6, 1000e6, 3, dwell_s=10.0, tx_port="TXA1")
    assert all(s["ports"] == {"tx": "TXA1"} for s in steps)


def _plan(steps, rate):
    m = mf.new_mission("Payload sweep test")
    m["authorization"] = {"reference": "TEST-2026", "note": "bench"}
    m["radios"] = [{"id": "G40-X", "type": "g40", "techniques": ["rx", "tx"], "sample_rate": rate}]
    m["steps"] = steps
    return m


def _gap_warnings(m):
    return [c for c in mf.validate(m) if "builds this waveform in that gap" in c.message]


def test_plan_check_warns_on_a_short_gap():
    # the old dialog defaults: 5 s dwell, 1 s gap
    tight = ps.sweep_steps("G40-X", 800e6, 2500e6, 6, dwell_s=5.0, gap_s=1.0)
    w = _gap_warnings(_plan(tight, 30_720_000))
    assert len(w) == 5 and all(c.level == "warning" for c in w)        # every step but the first
    assert len(_gap_warnings(_plan(tight, 1_920_000))) == 5
    # 3 s is enough at a low rate and not at a high one
    mid = ps.sweep_steps("G40-X", 800e6, 2500e6, 6, dwell_s=5.0, gap_s=3.0)
    assert _gap_warnings(_plan(mid, 1_920_000)) == []
    assert len(_gap_warnings(_plan(mid, 30_720_000))) == 5
    # the new defaults are clean at any rate
    assert _gap_warnings(_plan(ps.sweep_steps("G40-X", 800e6, 2500e6, 6, dwell_s=10.0), 61_440_000)) == []
