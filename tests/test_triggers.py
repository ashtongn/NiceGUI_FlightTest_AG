"""Tests for the location triggers (triggers.py): geometry, entries, re-arm
on leaving, stop fences only for running steps, stale and missing position.

    python3 tests/test_triggers.py
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)          # repo root
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from dpmg.mission import triggers as tg                # noqa: E402

_passed = 0
_failed = 0


def check(cond, msg):
    global _passed, _failed
    if cond:
        _passed += 1
    else:
        _failed += 1
        print(f"  FAIL: {msg}")


# Tucson area: a start circle and a stop polygon
START = {"lat": 32.2226, "lon": -110.9747, "radius_m": 500.0}
STOP = {"polygon": [[-110.95, 32.30], [-110.90, 32.30], [-110.90, 32.34],
                    [-110.95, 32.34]]}


def mkstep(sid, start=None, stop=None):
    loc = {}
    if start:
        loc["start"] = start
    if stop:
        loc["stop"] = stop
    s = {"id": sid, "radio": "G40-SIM", "technique": "jammer.barrage",
         "radiates": True, "params": {}, "schedule": {"start_s": 0, "stop_s": 30}}
    if loc:
        s["location"] = loc
    return s


def test_haversine():
    d = tg.haversine_m(32.2226, -110.9747, 32.2226, -110.9747)
    check(d == 0, "same point is 0 m")
    d = tg.haversine_m(0, 0, 0, 1)
    check(abs(d - 111195) < 200, f"one degree of longitude at the equator ~111 km: {d:.0f}")


def test_point_in_polygon():
    poly = STOP["polygon"]
    check(tg.point_in_polygon(32.32, -110.92, poly), "centre is inside")
    check(not tg.point_in_polygon(32.20, -110.92, poly), "south of it is outside")
    check(not tg.point_in_polygon(32.32, -111.10, poly), "west of it is outside")
    check(not tg.point_in_polygon(32.32, -110.92, poly[:2]), "two vertices is no polygon")


def test_inside_geofence_kinds():
    check(tg.inside(START, 32.2240, -110.9747), "150 m north of the start point is inside")
    check(not tg.inside(START, 32.2300, -110.9747), "800 m north is outside")
    check(tg.inside(STOP, 32.32, -110.92), "polygon inside")
    check(not tg.inside({}, 32.0, -110.0), "empty geofence is never inside")
    check(not tg.inside({"lat": "x", "lon": 1, "radius_m": 5}, 32.0, -110.0),
          "bad numbers are outside, not an error")


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_start_entry_once_then_rearm():
    clk = Clock()
    eng = tg.TriggerEngine(clock=clk)
    m = {"steps": [mkstep("s1", start=START)]}
    outside = tg.Fix(32.2300, -110.9747, t=clk.t, source="mcast")
    inside = tg.Fix(32.2230, -110.9747, t=clk.t, source="mcast")
    check(eng.update(m, outside) == [], "outside: nothing")
    check(eng.enabled and "position" in eng.reason, f"enabled with a fresh fix: {eng.reason}")
    ev = eng.update(m, inside)
    check(len(ev) == 1 and ev[0]["kind"] == "start" and ev[0]["step"]["id"] == "s1",
          f"entering the start fence prompts once: {ev}")
    check(eng.update(m, inside) == [], "still inside: no second prompt")
    check(eng.update(m, outside) == [], "leaving: nothing")
    ev = eng.update(m, inside)
    check(len(ev) == 1, "re-entering prompts again (dismissed earlier)")
    eng.mark_started("s1")
    eng.update(m, outside)
    check(eng.update(m, inside) == [], "a started step gets no start prompt")


def test_stop_entry_only_while_running():
    clk = Clock()
    eng = tg.TriggerEngine(clock=clk)
    m = {"steps": [mkstep("s1", start=START, stop=STOP)]}
    in_stop = tg.Fix(32.32, -110.92, t=clk.t)
    check(eng.update(m, in_stop) == [], "in the stop fence before the step runs: nothing")
    eng.mark_started("s1")
    far = tg.Fix(32.0, -111.5, t=clk.t)
    eng.update(m, far)
    ev = eng.update(m, in_stop)
    check(len(ev) == 1 and ev[0]["kind"] == "stop", f"entering the stop fence while running: {ev}")
    eng.mark_stopped("s1")
    eng.update(m, far)
    check(eng.update(m, in_stop) == [], "stopped step: no more prompts")
    check(eng.update(m, tg.Fix(32.2230, -110.9747, t=clk.t)) == [],
          "a stopped step gets no start prompt either")


def test_first_fix_inside_counts_as_entry():
    clk = Clock()
    eng = tg.TriggerEngine(clock=clk)
    m = {"steps": [mkstep("s1", start=START)]}
    ev = eng.update(m, tg.Fix(32.2230, -110.9747, t=clk.t))
    check(len(ev) == 1, "unknown -> inside is an entry")


def test_stale_and_missing_position():
    clk = Clock()
    eng = tg.TriggerEngine(clock=clk)
    m = {"steps": [mkstep("s1", start=START)]}
    inside = tg.Fix(32.2230, -110.9747, t=clk.t)
    eng.update(m, inside)                          # entered, prompted
    check(eng.update(m, None) == [] and not eng.enabled and "no position" in eng.reason,
          f"no fix disables: {eng.reason}")
    old = tg.Fix(32.2230, -110.9747, t=clk.t - 30)
    check(eng.update(m, old) == [] and not eng.enabled and "stale" in eng.reason,
          f"30 s old fix disables: {eng.reason}")
    ev = eng.update(m, tg.Fix(32.2230, -110.9747, t=clk.t))
    check(len(ev) == 1, "a fresh fix inside after an outage counts as an entry again")
    check(eng.step_state("s1")["in_start"] is True, "state records inside")


def test_steps_without_location_are_ignored():
    clk = Clock()
    eng = tg.TriggerEngine(clock=clk)
    m = {"steps": [mkstep("s1"), mkstep("s2", start=START)]}
    check([s["id"] for s in tg.located_steps(m)] == ["s2"], "located_steps")
    ev = eng.update(m, tg.Fix(32.2230, -110.9747, t=clk.t))
    check([e["step"]["id"] for e in ev] == ["s2"], "only the located step prompts")
    eng.reset()
    check(eng.step_state("s2")["in_start"] is None, "reset forgets the states")


def test_leave_event_and_rearm_for_location_tasks():
    clk = Clock()
    eng = tg.TriggerEngine(clock=clk)
    task = mkstep("L1", start=START)
    task["trigger"] = {"mode": "while_inside", "max_s": 600, "confirm": "once"}
    m = {"steps": [task]}
    inside = tg.Fix(32.2230, -110.9747, t=clk.t)
    outside = tg.Fix(32.2300, -110.9747, t=clk.t)
    ev = eng.update(m, inside)
    check([e["kind"] for e in ev] == ["start"], "entry")
    check(eng.update(m, outside) == [], "leaving before it started: nothing")
    eng.update(m, inside)                                  # prompts again (dismissed earlier)
    eng.mark_started("L1")
    ev = eng.update(m, outside)
    check([e["kind"] for e in ev] == ["leave"], f"leaving while running: leave event: {ev}")
    eng.mark_stopped("L1", rearm=True)
    st = eng.step_state("L1")
    check(not st["started"] and not st["stopped"], "re-armed: not started, not stopped")
    ev = eng.update(m, inside)
    check([e["kind"] for e in ev] == ["start"], "re-entry prompts again after the re-arm")
    eng.mark_started("L1")
    eng.mark_stopped("L1")                                 # a one-shot stop
    eng.update(m, outside)
    check(eng.update(m, inside) == [], "a one-shot stopped step stays quiet")


def test_leave_after_position_lost_inside():
    """Position lost while running inside, back outside: the step must end."""
    now = [100.0]
    eng = tg.TriggerEngine(clock=lambda: now[0])
    m = {"steps": [mkstep("a", start={"lat": 33.5, "lon": -112.0, "radius_m": 1000.0})]}
    check([e["kind"] for e in eng.update(m, tg.Fix(33.5, -112.0, t=now[0]))] == ["start"], "entry")
    eng.mark_started("a")
    now[0] += 30
    check(eng.update(m, tg.Fix(33.5, -112.0, t=70.0)) == [], "no events while stale")
    now[0] += 1
    check([e["kind"] for e in eng.update(m, tg.Fix(34.0, -112.0, t=now[0]))] == ["leave"],
          "back outside ends the running step")
    now[0] += 1
    check(eng.update(m, tg.Fix(34.0, -112.0, t=now[0])) == [], "only one leave")

    # back inside after the loss: still running, no leave, and a later exit still ends it
    eng = tg.TriggerEngine(clock=lambda: now[0])
    eng.update(m, tg.Fix(33.5, -112.0, t=now[0]))
    eng.mark_started("a")
    now[0] += 30
    eng.update(m, tg.Fix(33.5, -112.0, t=now[0] - 30))
    now[0] += 1
    check(eng.update(m, tg.Fix(33.5, -112.0, t=now[0])) == [], "back inside: no event")
    now[0] += 1
    check([e["kind"] for e in eng.update(m, tg.Fix(34.0, -112.0, t=now[0]))] == ["leave"], "later exit ends it")

    # never inside before the loss: a first fix outside is not a leave
    eng = tg.TriggerEngine(clock=lambda: now[0])
    eng.update(m, None)
    eng.mark_started("a")
    now[0] += 1
    check(eng.update(m, tg.Fix(34.0, -112.0, t=now[0])) == [], "never inside: no leave")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{_passed} passed, {_failed} failed, {len(tests)} test functions")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
