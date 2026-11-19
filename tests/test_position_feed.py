"""Tests for position_feed.PositionFeed with fake sources (no sockets).

    python3 tests/test_position_feed.py
"""

import os
import sys
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)          # repo root
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from dpmg.link import proto                          # noqa: E402
from dpmg.mission.position_feed import PositionFeed   # noqa: E402

_passed = 0
_failed = 0


def check(cond, msg):
    global _passed, _failed
    if cond:
        _passed += 1
    else:
        _failed += 1
        print(f"  FAIL: {msg}")


class FakeListener:
    def __init__(self, pos=None, age=0.0, group="239.2.1.1", port=5005):
        self._pos, self._age, self.group, self.port = pos, age, group, port

    @property
    def latest(self):
        return self._pos

    @property
    def age_s(self):
        return self._age if self._pos is not None else float("inf")

    def running(self):
        return True


def acft(lat, lon, alt=None, age=0.0):
    return types.SimpleNamespace(lat_deg=lat, lon_deg=lon, alt_m=alt,
                                 t_mono=time.monotonic() - age, source="json", raw="")


def host_with_fix(lat, lon, valid=True, age=1.0):
    st = proto.Status(serial="G40-SIM", host_type=0, radio_state=2)
    st.lat_1e7 = int(lat * 1e7)
    st.lon_1e7 = int(lon * 1e7)
    st.alt_m = 1500.0
    if valid:
        st.flags |= proto.Status.FLAG_POS_VALID
    return types.SimpleNamespace(status=st, status_age=age)


def test_multicast_wins_when_fresh():
    mgr = types.SimpleNamespace(hosts={"G40-SIM": host_with_fix(33.0, -112.0)})
    f = PositionFeed(mgr, FakeListener(acft(32.2, -110.9, 700.0), age=2.0))
    fix = f.current()
    check(fix is not None and fix.source == "aircraft feed" and fix.alt == 700.0
          and abs(fix.lat - 32.2) < 1e-9, f"multicast fix first: {fix}")


def test_stale_multicast_falls_back_to_radio_gps():
    mgr = types.SimpleNamespace(hosts={"G40-SIM": host_with_fix(33.0, -112.0, age=1.5)})
    f = PositionFeed(mgr, FakeListener(acft(32.2, -110.9, age=60.0), age=60.0))
    fix = f.current()
    check(fix is not None and fix.source == "G40-SIM GPS" and abs(fix.lon + 112.0) < 1e-6,
          f"radio GPS when the feed is stale: {fix}")
    check(abs((time.monotonic() - fix.t) - 1.5) < 0.5, "fix time from the status age")


def test_stale_multicast_with_no_radio_fix_is_still_reported():
    mgr = types.SimpleNamespace(hosts={})
    f = PositionFeed(mgr, FakeListener(acft(32.2, -110.9, age=40.0), age=40.0))
    fix = f.current()
    check(fix is not None and fix.source == "aircraft feed" and (time.monotonic() - fix.t) > 30,
          "the old feed fix comes back so the page can say stale")


def test_simulated_motion_wins_while_playing():
    from dpmg.mission import motion_sim as ms
    mgr = types.SimpleNamespace(hosts={"G40-SIM": host_with_fix(33.0, -112.0)})
    m = ms.MotionPlayer()
    m.set_path([[-112.10, 33.45], [-112.00, 33.45]])
    f = PositionFeed(mgr, FakeListener(acft(32.2, -110.9)), motion=m)
    check(f.current().source == "aircraft feed", "stopped player: the feed as before")
    m.play()
    fix = f.current()
    check(fix.source == "simulated motion" and abs(fix.lon + 112.10) < 1e-6,
          f"playing: the simulated position wins: {fix}")
    check("simulated motion (playing)" in f.sources_text(), f.sources_text())
    m.stop()
    check(f.current().source == "aircraft feed", "stopped again: back to the feed")


def test_no_source_gives_none():
    mgr = types.SimpleNamespace(hosts={"G40-SIM": host_with_fix(33.0, -112.0, valid=False)})
    f = PositionFeed(mgr, FakeListener(None))
    check(f.current() is None, "no valid fix anywhere")
    check(PositionFeed(None, None).current() is None, "no sources at all")
    check("aircraft feed" in f.sources_text() and "1 radio" in f.sources_text(),
          f"sources text: {f.sources_text()}")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{_passed} passed, {_failed} failed, {len(tests)} test functions")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
