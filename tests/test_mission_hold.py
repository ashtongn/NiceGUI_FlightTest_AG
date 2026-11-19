"""Tests for Stop this step and the hold on a step stopped by hand: position
loss and re-arming do not key it, leaving its area clears it, a timed step's
planned stop sends nothing afterward. Uses fake hosts, so nothing keys.

    python3 tests/test_mission_hold.py
"""

import asyncio
import os
import sys
import tempfile
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)          # repo root
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from dpmg.mission import mission_tab as mt                                 # noqa: E402
from dpmg.mission import triggers as tg                                    # noqa: E402
from dpmg.mission.mission_runner import (MissionRunner, AuditLog, run_changes,  # noqa: E402
                                         timeline_snapshot)

_passed = 0
_failed = 0


def check(cond, msg):
    global _passed, _failed
    if cond:
        _passed += 1
    else:
        _failed += 1
        print(f"  FAIL: {msg}")


class FakeAck:
    status = 0
    result = {}


class FakeHost:
    def __init__(self):
        self.calls = []

    async def cmd(self, name, params=None, timeout=None):
        self.calls.append(name)
        return FakeAck()


AREA = {"polygon": [[-112.02, 33.43], [-111.98, 33.43], [-111.98, 33.47], [-112.02, 33.47]]}
AREA2 = {"polygon": [[-112.02, 33.53], [-111.98, 33.53], [-111.98, 33.57], [-112.02, 33.57]]}
IN, OUT, IN2 = (33.45, -112.0), (33.40, -112.0), (33.55, -112.0)


def task(sid, area=AREA, confirm="auto"):
    return {"id": sid, "radio": "SIM", "technique": "tools.tone", "radiates": True,
            "params": {"seconds": 0.1, "freq": 1000.0, "center_hz": 915e6, "tx_atten_db": 40},
            "location": {"start": area}, "priority": 3,
            "trigger": {"mode": "while_inside", "max_s": 60, "confirm": confirm}}


def timed(sid, start, stop):
    return {"id": sid, "radio": "SIM", "technique": "tools.tone", "radiates": True,
            "params": {"seconds": 0.1, "freq": 1000.0, "center_hz": 915e6, "tx_atten_db": 40},
            "schedule": {"start_s": start, "stop_s": stop}}


def engine():
    now = [1000.0]
    eng = tg.TriggerEngine(clock=lambda: now[0])

    def fix(where):
        now[0] += 1
        return tg.Fix(where[0], where[1], t=now[0])
    return eng, fix


def kinds(events):
    return [(e["kind"], e["step"]["id"]) for e in events]


# the engine

def test_stopped_inside_stays_off_until_it_leaves_and_comes_back():
    eng, fix = engine()
    m = {"steps": [task("a")]}
    check(kinds(eng.update(m, fix(IN))) == [("start", "a")], "first entry")
    eng.mark_started("a")
    f = fix(IN)
    eng.hold(m["steps"][0], f)
    eng.mark_stopped("a", rearm=True)
    check(eng.is_held("a"), "held")
    check(eng.update(m, fix(IN)) == [], "still inside: nothing")
    check(eng.update(m, None) == [], "position lost: nothing")
    check(eng.update(m, fix(IN)) == [], "position back inside: nothing")
    check(eng.release_left(m, fix(IN)) == [], "inside does not clear it")
    check(eng.is_held("a"), "still held")
    f = fix(OUT)
    check(eng.release_left(m, f) == ["a"], "a reading outside clears it")
    check(eng.update(m, f) == [], "leaving is not a start")
    check(kinds(eng.update(m, fix(IN))) == [("start", "a")], "a new entry starts it")


def test_control_without_a_hold_position_loss_counts_as_entry():
    # what the hold protects against: the behaviour for a step nobody stopped stays as it was
    eng, fix = engine()
    m = {"steps": [task("a")]}
    eng.update(m, fix(IN))
    eng.update(m, None)
    check(kinds(eng.update(m, fix(IN))) == [("start", "a")], "good fix inside counts as entry")


def test_hold_survives_reset():
    eng, fix = engine()
    m = {"steps": [task("a")]}
    eng.hold(m["steps"][0], fix(IN))
    eng.reset()
    check(eng.is_held("a"), "arming again keeps the hold")
    check(eng.update(m, fix(IN)) == [], "armed inside: nothing")


def test_late_arm_with_no_stop_still_keys():
    eng, fix = engine()
    m = {"steps": [task("a")]}
    eng.reset()
    check(kinds(eng.update(m, fix(IN))) == [("start", "a")], "arming late inside keys as before")


def test_hold_only_when_inside():
    eng, fix = engine()
    m = {"steps": [task("a")]}
    eng.hold(m["steps"][0], fix(OUT))
    check(not eng.is_held("a"), "stopped outside: no hold")


def test_hold_all_takes_only_steps_the_platform_is_inside():
    eng, fix = engine()
    m = {"steps": [task("a"), task("b", AREA2), timed("t", 0, 10)]}
    eng.hold_all(m, fix(IN))
    check(eng.held() == ["a"], f"only the area it is in: {eng.held()}")
    check(kinds(eng.update(m, fix(IN2))) == [("start", "b")], "another task's area still keys")


def test_no_position_holds_only_what_was_inside_or_running():
    eng, fix = engine()
    m = {"steps": [task("a"), task("b", AREA2), task("c", AREA2)]}
    eng.hold_all(m, None)
    check(eng.held() == [], "position never known and nothing running: no hold")
    eng.update(m, fix(IN))
    eng.update(m, None)
    eng.mark_started("c")
    eng.hold_all(m, None)
    check(eng.held() == ["a", "c"], f"last known inside, or running: {eng.held()}")


def test_step_gone_from_the_plan_is_released():
    eng, fix = engine()
    m = {"steps": [task("a")]}
    eng.hold(m["steps"][0], fix(IN))
    check(eng.release_left({"steps": []}, None) == ["a"], "released with no position too")
    eng.hold(m["steps"][0], fix(IN))
    eng.forget("a")
    check(not eng.is_held("a"), "forget clears it")


def test_hold_ignores_a_step_with_no_area():
    eng, fix = engine()
    eng.hold(timed("t", 0, 10), fix(IN))
    check(eng.held() == [], "timed step: no hold")


# the runner

def test_planned_stop_after_a_hand_stop_sends_nothing():
    host = FakeHost()
    runner = MissionRunner(lambda rid: host, AuditLog(), speed=1.0)
    t1 = timed("t1", 0, 0.4)
    loc = task("L1")

    async def go():
        run = asyncio.create_task(runner.run({"name": "t", "steps": [t1, loc]}))
        await asyncio.sleep(0.15)
        check("t1" in runner.active_steps, "the timed step keyed")
        check(await runner.stop_step_by_hand(t1), "stopped")
        check("t1" not in runner.active_steps, "not keyed now")
        await runner.start_step_now(loc)
        n = host.calls.count("stop_tx")
        await run
        check(host.calls.count("stop_tx") == n, "its planned stop sent no stop_tx")
        check("L1" in runner.active_steps, "the location task kept the radio")
        await runner.stop_step_now(loc)
    asyncio.run(go())


def test_hand_stop_leaves_the_rest_of_the_run_going():
    host = FakeHost()
    runner = MissionRunner(lambda rid: host, AuditLog(), speed=1.0)
    t1, t2 = timed("t1", 0, 0.3), timed("t2", 0.4, 0.6)

    async def go():
        run = asyncio.create_task(runner.run({"name": "t", "steps": [t1, t2]}))
        await asyncio.sleep(0.15)
        await runner.stop_step_by_hand(t1)
        check(runner.running, "the run goes on")
        await asyncio.sleep(0.35)
        check("t2" in runner.active_steps, "the next step keyed at its time")
        await run
        check(not runner.active_steps, "the next step stopped at its time")
    asyncio.run(go())
    check(host.calls.count("start_tx") == 2, f"both keyed: {host.calls}")
    check(host.calls.count("stop_tx") == 2, f"one stop each: {host.calls}")


def test_a_new_run_forgets_the_hand_stop():
    host = FakeHost()
    runner = MissionRunner(lambda rid: host, AuditLog(), speed=1.0)
    t1 = timed("t1", 0, 0.1)
    runner._stopped_by_hand.add("t1")
    asyncio.run(runner.run({"name": "t", "steps": [t1]}))
    check(host.calls.count("stop_tx") == 1, f"its planned stop runs in a new run: {host.calls}")


def test_hand_stop_of_a_step_not_keyed_changes_nothing():
    host = FakeHost()
    runner = MissionRunner(lambda rid: host, AuditLog(), speed=1.0)
    t1 = timed("t1", 0, 1)
    check(asyncio.run(runner.stop_step_by_hand(t1)) is False, "nothing to stop")
    check("t1" not in runner._stopped_by_hand, "its planned stop is untouched")
    check(host.calls == [], "no command sent")


def test_location_task_can_stop_again_after_a_hand_stop():
    # a task keyed again after a hand stop must still send its stop
    host = FakeHost()
    runner = MissionRunner(lambda rid: host, AuditLog(), speed=1.0)
    loc = task("L1")

    async def go():
        await runner.start_step_now(loc)
        await runner.stop_step_by_hand(loc)
        n = host.calls.count("stop_tx")
        await runner.start_step_now(loc)
        await runner.stop_step_now(loc, reason="because the platform left its area")
        check(host.calls.count("stop_tx") > n, "the second stop went out")
        check("L1" not in runner.active_steps, "not keyed")
    asyncio.run(go())


# the plan session

def session(steps, where):
    host = FakeHost()
    path = os.path.join(tempfile.mkdtemp(), "plan.json")
    sess = mt._PlanSession(SimpleNamespace(hosts={"SIM": host}), path=path)
    sess.mission = {"name": "t", "steps": steps}
    sess.runner.get_host = lambda rid: host
    sess.runner.get_any_host = lambda rid: host
    pos = {"where": where}

    def current():
        w = pos["where"]
        return tg.Fix(w[0], w[1], t=sess.triggers.clock()) if w else None
    sess.feed.current = current

    def tick():
        sess._trig_t = 0.0
        return [(e["kind"], e["step"]["id"]) for e in sess.trigger_tick()[1]]
    return sess, host, pos, tick


def test_session_stop_this_step_inside():
    loc = task("L1", confirm="once")
    sess, host, pos, tick = session([loc], IN)

    async def go():
        sess.arm_triggers(True)
        check(tick() == [("start", "L1")], "entry")
        await sess.runner.start_step_now(loc)
        sess.triggers.mark_started("L1")
        sess.confirmed_once.add("L1")
        check(await sess.stop_step(loc), "stopped")
        check(sess.triggers_armed, "the triggers stay on")
        check(sess.triggers.is_held("L1"), "held")
        check("L1" not in sess.confirmed_once, "it asks again")
        check(tick() == [], "no re-key inside")
        pos["where"] = None
        check(tick() == [], "position lost")
        pos["where"] = IN
        check(tick() == [], "position back inside: no re-key")
        sess.arm_triggers(False)
        sess.arm_triggers(True)
        check(tick() == [], "off and on inside: no re-key")
        pos["where"] = OUT
        check(tick() == [], "leaving")
        check(not sess.triggers.is_held("L1"), "released")
        check(any("no longer held" in e[2] for e in sess.audit.entries), "audit line")
        pos["where"] = IN
        check(tick() == [("start", "L1")], "next entry starts it")
    asyncio.run(go())
    check(host.calls.count("start_tx") == 1, f"keyed once: {host.calls}")


def test_session_stop_all_inside_then_rearm():
    a, b = task("A"), task("B", AREA2)
    sess, host, pos, tick = session([a, b], IN)

    async def go():
        sess.arm_triggers(True)
        tick()
        await sess.runner.start_step_now(a)
        sess.triggers.mark_started("A")
        await sess.stop_all([host])
        check(not sess.triggers_armed, "disarmed")
        check(sess.triggers.held() == ["A"], "held")
        check(not sess.runner.active_steps, "nothing keyed")
        sess.arm_triggers(True)
        check(tick() == [], "re-armed inside: no re-key")
        pos["where"] = IN2
        check(tick() == [("start", "B")], "another area keys")
        check(not sess.triggers.is_held("A"), "left A, so A is released")
    asyncio.run(go())


def test_session_hold_clears_while_disarmed():
    a = task("A")
    sess, host, pos, tick = session([a], IN)

    async def go():
        await sess.stop_all([host])
        check(sess.triggers.is_held("A"), "held, inside with the triggers off")
        pos["where"] = OUT
        tick()
        check(not sess.triggers.is_held("A"), "a reading outside clears it with the triggers off")
        pos["where"] = IN
        sess.arm_triggers(True)
        check(tick() == [("start", "A")], "armed late after leaving: keys as before")
    asyncio.run(go())


def test_session_timed_plan_with_no_areas_is_not_affected():
    t1 = timed("t1", 0, 5)
    sess, host, pos, tick = session([t1], None)

    async def go():
        await sess.runner.start_step_now(t1)
        check(await sess.stop_step(t1), "stopped")
        check(sess.triggers.held() == [], "no hold")
        check("t1" not in sess.triggers._st, "no trigger state made for it")
        await sess.stop_all([host])
        check(sess.triggers.held() == [], "no hold after STOP ALL TX")
    asyncio.run(go())


# the warning for an edit during a run

def test_run_changes_names_each_kind_of_edit():
    a, b = timed("a", 0, 5), timed("b", 10, 15)
    plan = {"steps": [a, b, task("L1")]}
    started = timeline_snapshot(plan)
    check("L1" not in started, "a location task is not part of the run")
    empty = {"removed": [], "added": [], "moved": [], "settings": []}
    check(run_changes(started, plan) == empty, "no edit: nothing")
    check(run_changes(started, {"steps": []})["removed"] == ["a", "b"], "delete all")
    check(run_changes(started, {"steps": [a, b, timed("n", 20, 25)]})["added"] == ["n"], "added")
    moved = timed("b", 30, 35)
    ch = run_changes(started, {"steps": [a, moved]})
    check(ch["moved"] == ["b"] and ch["settings"] == [], f"moved only: {ch}")
    edited = timed("b", 10, 15)
    edited["params"]["tx_atten_db"] = 13
    ch = run_changes(started, {"steps": [a, edited]})
    check(ch["settings"] == ["b"] and ch["moved"] == [], f"settings only: {ch}")
    both = timed("b", 30, 35)
    both["params"]["tx_atten_db"] = 13
    ch = run_changes(started, {"steps": [a, both]})
    check(ch["settings"] == ["b"] and ch["moved"] == ["b"], f"both: {ch}")
    check(run_changes(started, {"steps": [a, b]}) == empty, "a location task edit: nothing")
    # Apply in the step editor fills in the default priority and writes floats
    same = timed("b", 10.0, 15.0)
    same["priority"] = 5
    same["params"]["center_hz"] = float(same["params"]["center_hz"])
    check(run_changes(started, {"steps": [a, same]}) == empty,
          f"Apply with nothing changed: {run_changes(started, {'steps': [a, same]})}")
    higher = timed("b", 10, 15)
    higher["priority"] = 1
    check(run_changes(started, {"steps": [a, higher]})["settings"] == ["b"], "a real priority change")


def test_run_records_what_it_started_with_and_a_run_changes_no_step():
    host = FakeHost()
    runner = MissionRunner(lambda rid: host, AuditLog(), speed=0.0)
    plan = {"name": "t", "steps": [timed("a", 0, 5), timed("b", 10, 15)]}
    asyncio.run(runner.run(plan))
    check(sorted(runner.started_steps) == ["a", "b"], "recorded")
    check(not any(run_changes(runner.started_steps, plan).values()), "the run itself is not an edit")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{_passed} passed, {_failed} failed, {len(tests)} test functions")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
