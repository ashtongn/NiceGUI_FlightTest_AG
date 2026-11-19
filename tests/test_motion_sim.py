"""Tests for motion_sim.MotionPlayer and path_through_areas (fake clock).

    python3 tests/test_motion_sim.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)          # repo root
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from dpmg.mission import motion_sim as ms              # noqa: E402
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


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


PATH = [[-112.10, 33.45], [-112.00, 33.45]]      # about 9.3 km east along a parallel


def test_play_moves_along_the_path():
    clk = Clock()
    m = ms.MotionPlayer(clock=clk)
    m.set_path(PATH)
    m.speed_mps = 100.0
    check(m.position() is None and m.fix() is None, "stopped: no position")
    check(m.play(), "play starts")
    check(m.position() == PATH[0], "at the start on play")
    clk.t += 10.0
    p = m.position()
    d = tg.haversine_m(PATH[0][1], PATH[0][0], p[1], p[0])
    check(abs(d - 1000.0) < 20, f"10 s at 100 m/s is about 1 km along: {d:.0f} m")
    f = m.fix()
    check(f is not None and f.source == "simulated motion" and f.t == clk.t, "fix from the player")
    clk.t += 1000.0
    p = m.position()
    check(p == PATH[1] and m.state == "paused", "runs to the end and pauses there")
    pr = m.progress()
    check(abs(pr["fraction"] - 1.0) < 1e-6 and pr["total_m"] > 9000, f"progress at the end: {pr}")


def test_pause_resume_stop_and_loop():
    clk = Clock()
    m = ms.MotionPlayer(clock=clk)
    m.set_path(PATH)
    m.speed_mps = 1000.0
    m.play()
    clk.t += 2.0
    m.pause()
    at = m.position()
    clk.t += 50.0
    check(m.position() == at and m.state == "paused", "paused: does not move")
    m.play()
    clk.t += 1.0
    check(m.position() != at, "resumes from where it paused")
    m.stop()
    check(m.position() is None and m.state == "stopped", "stop")
    m.loop = True
    m.play()
    clk.t += 12.0                                   # 12 km: past the end, turned around
    p = m.position()
    check(m.state == "playing" and p != PATH[1] and not m.progress()["forward"],
          f"loop: still playing, coming back: {p}")


def test_bad_path():
    m = ms.MotionPlayer(clock=Clock())
    m.set_path([[-112.0, 33.0]])
    check(not m.play() and m.position() is None, "one point is no path")
    m.set_path([])
    check(m.length_m() == 0.0, "empty path has no length")


def test_path_through_areas():
    plan = {"steps": [
        {"id": "a", "location": {"start": {"lat": 33.45, "lon": -112.07, "radius_m": 500}}},
        {"id": "b"},
        {"id": "c", "location": {"start": {"polygon": [[-111.9, 33.5], [-111.8, 33.5], [-111.8, 33.6]]}}},
    ]}
    path = ms.path_through_areas(plan, lead_m=3000.0)
    check(len(path) == 4, f"lead-in, two centres, lead-out: {path}")
    check(path[0][0] < -112.07 and abs(path[1][1] - 33.45) < 1e-9, "starts 3 km west of the first area")
    d = tg.haversine_m(path[0][1], path[0][0], path[1][1], path[1][0])
    check(abs(d - 3000.0) < 50, f"lead-in about 3 km: {d:.0f}")
    check(path[-1][0] > path[-2][0], "ends east of the last area")
    check(ms.path_through_areas({"steps": []}) == [], "no areas, no path")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{_passed} passed, {_failed} failed, {len(tests)} test functions")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
