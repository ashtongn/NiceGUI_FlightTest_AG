#!/usr/bin/env python3
"""More than one aircraft position feed: the settings file migration (one old
feed becomes one named aircraft), the feed registry with two feeds holding
distinct positions, the per-radio binding of the location triggers, the
ground log's aircraft column, the planner feed row per aircraft and the map
markers. No sockets are opened.    python3 tests/test_multi_aircraft.py
"""
import csv
import glob
import json
import os
import sys
import tempfile
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="dpmg_multi_acft_")
os.environ["DPMG_FEED_SETTINGS"] = os.path.join(_TMP, "aircraft_feed.json")

from dpmg.link import acft_pos_multicast as am      # noqa: E402
from dpmg.link import aircraft_feeds as af          # noqa: E402
from dpmg.link import feed_settings                 # noqa: E402
from dpmg.link import ground_log as gl              # noqa: E402
from dpmg.mission import feed_binding as fb         # noqa: E402
from dpmg.mission import feed_state as fst          # noqa: E402
from dpmg.mission import map_element as me          # noqa: E402
from dpmg.mission import triggers as tg             # noqa: E402
from dpmg.mission.position_feed import PositionFeed  # noqa: E402

_passed = _failed = 0


def check(cond, msg):
    global _passed, _failed
    if cond:
        _passed += 1
        print("  ok  ", msg)
    else:
        _failed += 1
        print("  FAIL:", msg)


# settings

def test_settings_migration_and_list():
    old = {"group": "239.9.9.9", "port": 6000, "iface": "127.0.0.1", "format": "nmea",
           "sender": {"rate_hz": 2}}
    st = feed_settings.normalize(old)
    acs = st["aircraft"]
    check(len(acs) == 1 and acs[0]["name"] == "239.9.9.9:6000", f"an old file is one aircraft named from its values: {acs}")
    check((acs[0]["group"], acs[0]["port"], acs[0]["iface"], acs[0]["format"], acs[0]["window_s"])
          == ("239.9.9.9", 6000, "127.0.0.1", "nmea", 1.5), "with its old values and the default window")
    check(st["group"] == "239.9.9.9" and st["port"] == 6000 and st["format"] == "nmea",
          "the top level keys still read the one feed")
    with open(feed_settings.settings_path(), "w") as f:
        json.dump(old, f)
    back = feed_settings.load()
    check(back["aircraft"][0]["name"] == "239.9.9.9:6000" and back["sender"]["rate_hz"] == 2.0,
          "load migrates the old file in place")
    # the list is the truth: an edit goes on its first entry and the top level mirrors it
    back["aircraft"][0]["port"] = 6001
    back["port"] = 1
    feed_settings.save(back)
    again = feed_settings.load()
    check(again["aircraft"][0]["port"] == 6001 and again["port"] == 6001,
          "the top level keys mirror the first aircraft after a save")
    st2 = feed_settings.set_aircraft(again, {"name": "N2", "group": "239.9.9.10", "port": 6001, "format": "json"})
    check([e["name"] for e in st2["aircraft"]] == ["239.9.9.9:6000", "N2"], "a second aircraft is appended")
    check(feed_settings.shared_sockets(st2) == {}, "different groups: nothing shared")
    st3 = feed_settings.set_aircraft(st2, dict(feed_settings.find_aircraft(st2, "N2"), group="239.9.9.9"), old_name="N2")
    check(feed_settings.shared_sockets(st3) == {("239.9.9.9", 6001): ["239.9.9.9:6000", "N2"]}
          and feed_settings.shares_with(st3, "N2") == ["239.9.9.9:6000"],
          "same group and port, different format: allowed and named as shared")
    st4 = feed_settings.set_aircraft(st3, dict(feed_settings.find_aircraft(st3, "N2"), name="N1234"), old_name="N2")
    check(feed_settings.aircraft_names(st4) == ["239.9.9.9:6000", "N1234"], "rename keeps the list order")
    st5 = feed_settings.set_aircraft(st4, {"name": "N1234", "group": "239.1.1.1", "port": 7})
    check(feed_settings.aircraft_names(st5) == ["239.9.9.9:6000", "N1234"] and
          feed_settings.find_aircraft(st5, "N1234")["port"] == 7, "set by name replaces, no duplicate")
    st6 = feed_settings.set_aircraft(st5, {"name": "N1234", "group": "239.1.1.2", "port": 8}, old_name="nobody")
    check(feed_settings.aircraft_names(st6) == ["239.9.9.9:6000", "N1234", "N1234 2"], "a taken name gets a number")
    st7 = feed_settings.remove_aircraft(st6, "N1234 2")
    st7 = feed_settings.remove_aircraft(st7, "239.9.9.9:6000")
    check(feed_settings.aircraft_names(st7) == ["N1234"] and st7["group"] == "239.1.1.1",
          "remove, and the top level follows the new first aircraft")
    check(feed_settings.aircraft_names(feed_settings.remove_aircraft(st7, "N1234")) == ["N1234"],
          "the last aircraft cannot be removed")
    bad = feed_settings.normalize({"aircraft": [{"name": "", "port": "x", "window_s": 999}]})
    check(bad["aircraft"][0]["name"] == "239.2.1.1:5005" and bad["aircraft"][0]["window_s"] == 60.0,
          "bad entry values fall back")
    lst = am.MulticastPosListener()
    feed_settings.apply_entry(lst, {"group": "239.5.5.5", "port": 5555, "format": "nmea", "window_s": 3.0})
    check((lst.group, lst.port, lst.fmt, lst.window_s) == ("239.5.5.5", 5555, "nmea", 3.0),
          "apply_entry sets group, port, format and window")
    os.remove(feed_settings.settings_path())


# the registry

class FakeListener(am.MulticastPosListener):
    """The real parser and state, no socket: start() only sets a flag."""

    def start(self):
        self._started = True

    def stop(self):
        self._started = False

    @property
    def running(self):
        return bool(getattr(self, "_started", False))


def _two():
    return feed_settings.normalize({"aircraft": [
        {"name": "A", "group": "239.2.1.1", "port": 5005, "format": "json"},
        {"name": "B", "group": "239.2.1.1", "port": 5005, "format": "nmea"}]})


def test_registry_two_feeds():
    feeds = af.AircraftFeeds(_two(), listener_factory=FakeListener)
    check(feeds.names() == ["A", "B"] and feeds.first_name() == "A" and len(feeds) == 2, "two aircraft in order")
    check(feeds.resolve("B") == "B" and feeds.resolve("gone") == "A" and feeds.resolve(None) == "A",
          "an unknown or missing name resolves to the first aircraft")
    a, b = feeds.get("A"), feeds.get("B")
    check(a.fmt == "json" and b.fmt == "nmea" and feeds.shared_sockets() == {("239.2.1.1", 5005): ["A", "B"]},
          "same socket, formats differ, and the share is reported")
    check("shares 239.2.1.1:5005 with B" in feeds.socket_note("A") and feeds.socket_note("gone") == "",
          f"the socket note: {feeds.socket_note('A')}")
    a.feed(b'{"lat": 33.40, "lon": -112.07, "alt": 1500}')
    b.feed(am._nmea_gga(33.47, -111.93, 900.0).encode())
    check(a.feed(am._nmea_gga(1.0, 2.0, 3.0).encode()) is None, "the json feed drops NMEA")
    check(abs(feeds.latest("A").lat_deg - 33.40) < 1e-6 and abs(feeds.latest("B").lat_deg - 33.47) < 1e-6,
          "each aircraft holds its own position")
    name, pos = feeds.latest_named("B")
    check(name == "B" and abs(pos.lon_deg + 111.93) < 1e-6, "latest_named gives the resolved name and the fix")
    name, pos = feeds.latest_named("nobody")
    check(name == "A" and abs(pos.lat_deg - 33.40) < 1e-6, "an unbound radio follows the first aircraft")
    fresh = feeds.fresh(5.0)
    check([n for n, _p in fresh] == ["A", "B"], "fresh lists both with a fix")
    feeds.start("B")
    check(feeds.running("B") and not feeds.running("A") and feeds.any_running(), "start one of them")
    # a settings save that changes nothing on B keeps its listener; A's group change restarts it
    st = _two()
    st["aircraft"][0]["group"] = "239.2.1.2"
    feeds.reload(st)
    check(feeds.get("B") is b and feeds.get("A") is a and a.group == "239.2.1.2", "reload keeps the listener objects")
    check(feeds.shared_sockets() == {}, "no longer shared")
    check(feeds.rename("B", "N1234") and feeds.get("N1234") is b and feeds.names() == ["A", "N1234"],
          "rename moves the listener, state kept")
    check(not feeds.rename("A", "N1234") and not feeds.rename("zz", "q"), "rename refuses a taken or unknown name")
    feeds.reload(feed_settings.normalize({"aircraft": [{"name": "N1234", "group": "239.2.1.1", "port": 5005}]}))
    check(feeds.names() == ["N1234"] and feeds.primary is b and not a.running, "a removed aircraft's listener is stopped")
    check(feeds.get("A") is None and abs(feeds.latest("A").lat_deg - 33.47) < 1e-6,
          "an old name resolves to the first aircraft")


# per-radio binding of the triggers

def _mission():
    return {"name": "two aircraft", "radios": [{"id": "R1", "type": "g40"},
                                              {"id": "R2", "type": "g40", "aircraft": "B"},
                                              {"id": "R3", "type": "g40", "aircraft": "gone"}],
            "steps": [{"id": "s1", "radio": "R1", "technique": "cw", "t0": 0, "dur": 10,
                       "location": {"start": {"kind": "point", "lat": 33.40, "lon": -112.07, "radius_m": 500}}},
                      {"id": "s2", "radio": "R2", "technique": "cw", "t0": 0, "dur": 10,
                       "location": {"start": {"kind": "point", "lat": 33.47, "lon": -111.93, "radius_m": 500}}},
                      {"id": "s3", "radio": "R9", "technique": "cw", "t0": 0, "dur": 10}]}


def test_binding_and_triggers():
    names = ["A", "B"]
    m = _mission()
    check(fb.radio_aircraft(m, "R1", names) == "A" and fb.radio_aircraft(m, "R2", names) == "B",
          "a radio with no field follows the first aircraft, a bound one its own")
    check(fb.radio_aircraft(m, "R3", names) == "A" and fb.radio_aircraft(m, "R9", names) == "A",
          "an unknown aircraft name or a radio not in the plan: the first aircraft")
    check(fb.radio_aircraft(m, "R2", []) is None, "no aircraft at all: None")
    groups = fb.steps_by_aircraft(m, names)
    check([s["id"] for s in groups["A"]] == ["s1", "s3"] and [s["id"] for s in groups["B"]] == ["s2"],
          "steps grouped by their radio's aircraft")
    r = {"id": "X"}
    fb.set_radio_aircraft(r, "B", names)
    check(r.get("aircraft") == "B", "bind to the second aircraft writes the field")
    fb.set_radio_aircraft(r, "A", names)
    check("aircraft" not in r, "bind to the first clears it, so the file stays as before")
    fb.set_radio_aircraft(r, "nobody", names)
    check("aircraft" not in r, "an unknown name is not written")

    now = [100.0]
    eng = tg.TriggerEngine(clock=lambda: now[0])
    fixes = {"A": tg.Fix(33.40, -112.07, None, t=100.0, source="aircraft feed A"),
             "B": tg.Fix(40.0, -100.0, None, t=100.0, source="aircraft feed B")}
    ev = fb.tick_triggers(eng, m, names, fixes.get)
    check([e["step"]["id"] for e in ev] == ["s1"], f"A inside s1's area, B away: only s1 fires: {[e['step']['id'] for e in ev]}")
    check(eng.enabled and "A: position" in eng.reason and "B: position" in eng.reason, f"reason names both: {eng.reason}")
    fixes["B"] = tg.Fix(33.47, -111.93, None, t=100.0, source="aircraft feed B")
    ev = fb.tick_triggers(eng, m, names, fixes.get)
    check([e["step"]["id"] for e in ev] == ["s2"], "B now inside s2's area: s2 fires, s1 not again")
    eng.mark_started("s1")
    # A's feed goes stale: s1 keeps its state for B's good fix, B's step is untouched
    fixes["A"] = None
    ev = fb.tick_triggers(eng, m, names, fixes.get)
    check(ev == [] and eng.enabled and eng.reason.startswith("A: no position"), f"A lost: still enabled through B: {eng.reason}")
    check(eng.step_state("s1")["in_start"] is None and eng.step_state("s2")["in_start"] is True,
          "only A's steps forget which side of the fence they were")
    fixes["A"] = tg.Fix(34.0, -113.0, None, t=100.0, source="aircraft feed A")
    ev = fb.tick_triggers(eng, m, names, fixes.get)
    check([e["kind"] for e in ev] == ["leave"] and ev[0]["step"]["id"] == "s1", "A back outside: s1 (running) gets its leave")
    ok, why = fb.status(eng, names, fixes.get)
    check(ok and why.count(";") == 1, f"status over both: {why}")
    ok, why = fb.status(eng, names, lambda n: None)
    check(not ok and "A: no position" in why and "B: no position" in why, "no fix anywhere: off")
    # one aircraft: the old path, bit for bit
    eng1 = tg.TriggerEngine(clock=lambda: now[0])
    eng2 = tg.TriggerEngine(clock=lambda: now[0])
    fx = tg.Fix(33.40, -112.07, None, t=100.0, source="aircraft feed")
    a = fb.tick_triggers(eng1, m, ["A"], lambda n: fx)
    b = eng2.update(m, fx)
    check([e["step"]["id"] for e in a] == [e["step"]["id"] for e in b] == ["s1"] and eng1.reason == eng2.reason,
          "one aircraft: the same as engine.update")
    # hold per aircraft: STOP ALL TX holds the step whose own aircraft is inside
    eng3 = tg.TriggerEngine(clock=lambda: now[0])
    fixes = {"A": tg.Fix(33.40, -112.07, None, t=100.0), "B": tg.Fix(0.0, 0.0, None, t=100.0)}
    fb.hold_all(eng3, m, names, fixes.get)
    check(eng3.held() == ["s1"], f"hold_all per aircraft: {eng3.held()}")
    fb.hold(eng3, m, m["steps"][1], names, lambda n: tg.Fix(33.47, -111.93, None, t=100.0) if n == "B" else None)
    check(eng3.held() == ["s1", "s2"], "hold reads the step's own aircraft")
    fixes["A"] = tg.Fix(0.0, 0.0, None, t=100.0)
    fixes["B"] = tg.Fix(33.47, -111.93, None, t=100.0)
    rel = fb.release_left(eng3, m, names, fixes.get)
    check(rel == ["s1"] and eng3.held() == ["s2"], f"release_left per aircraft: A left, B still inside: {rel}")


# the position feed names its aircraft, the feed row and the markers

def test_feed_state_and_markers():
    la, lb = FakeListener(group="239.2.1.1", port=5005), FakeListener(group="239.2.1.1", port=5006)
    la.start()
    lb.start()
    t0 = time.monotonic()
    la.feed(b'{"lat": 33.40, "lon": -112.07, "alt": 1500, "heading": 90, "speed_kt": 120}', t_mono=t0)
    lb.feed(b'{"lat": 33.47, "lon": -111.93, "alt": 900}', t_mono=t0)
    feeds = {"A": PositionFeed(None, la, name="A"), "B": PositionFeed(None, lb, name="B")}
    lst = {"A": la, "B": lb}
    sess = SimpleNamespace(listener=la, feed=feeds["A"], motion=None,
                           pos_group="239.2.1.1", pos_port=5005, pos_iface="0.0.0.0",
                           listener_for=lambda n: lst[n], feed_for=lambda n: feeds[n])
    check(feeds["B"].current().source == "aircraft feed B", "the fix names its aircraft")
    st = fst.feed_state(sess, now_mono=t0 + 1.0, name="B")
    check(st["aircraft"] == "B" and st["where"].startswith("239.2.1.1:5006") and st["level"] == "ok"
          and st["source"] == "aircraft feed B", f"feed state per aircraft: {st['where']} {st['source']}")
    txt = fst.feed_state_text(st)
    check(txt.startswith("B: source aircraft feed B; feed 239.2.1.1:5006"), f"the row names the aircraft: {txt}")
    sta = fst.feed_state(sess, now_mono=t0 + 1.0, name="A")
    check(sta["heading_deg"] == 90.0 and abs(sta["speed_mps"] - 120 * 0.514444) < 1e-6,
          "fix extras come from that aircraft's listener")
    st0 = fst.feed_state(sess, now_mono=t0 + 1.0)
    check(st0["aircraft"] is None and not fst.feed_state_text(st0).startswith("B:"), "no name: as before")
    fa, fbx = feeds["A"].current(), feeds["B"].current()
    marks = me.position_markers([("A", fa, True, 90.0), ("B", fbx, False, None), ("C", None, True, None)])
    check(len(marks) == 2 and marks[0]["label"] == "A hdg 090" and marks[0]["heading"] == 90.0
          and marks[1]["label"] == "B (stale)" and marks[0]["color"] != marks[1]["color"],
          f"one named marker per aircraft with a fix: {marks}")
    one = me.position_marker(fa, valid=True)
    check(one["label"] == "platform" and "name" not in one, "the one platform marker reads as before")


# the ground log names the aircraft

def test_ground_log_aircraft_column():
    out = os.path.join(_TMP, "out")
    pos = am.parse_auto(b'{"lat": 33.4, "lon": -112.0, "alt": 10}')
    host = SimpleNamespace(serial="G40-SIM", status=SimpleNamespace(flags=0), connected=True, config={},
                           detections={}, det_head=0, watch_bands=[])
    by = {"G40-SIM": ("N1234", pos)}
    glogs = gl.GroundLogs(lambda: {"G40-SIM": host}, lambda serial: by.get(serial), str, out_dir=out)
    t = 1794096001.0
    glogs.get("G40-SIM").start("ground", {"position": True}, 0, now=t)
    glogs.tick(now=t)
    by["G40-SIM"] = ("N1234", None)
    glogs.tick(now=t + 1)
    by["G40-SIM"] = pos
    glogs.tick(now=t + 2)
    with open(glob.glob(os.path.join(out, "position_*.csv"))[0], newline="") as fp:
        rows = list(csv.reader(fp))
    check(rows[0][-1] == "aircraft", "header ends with the aircraft column")
    check(rows[1][5] == "acft_mcast" and rows[1][-1] == "N1234", f"a feed row carries the aircraft: {rows[1]}")
    check(rows[2][5] == "no_fix" and rows[2][-1] == "N1234", f"no fix still names the aircraft: {rows[2]}")
    check(rows[3][5] == "acft_mcast" and rows[3][-1] == "" and len(rows[3]) == len(rows[0]),
          "a bare fix (old caller) leaves the column blank")
    glogs.get("G40-SIM").stop()
    zero = gl.GroundLogs(lambda: {}, lambda: None)
    check(zero._acft_for("x") is None, "a zero argument get_acft still works")


def test_sim_position_aircraft_option():
    feed_settings.save(_two())
    import importlib
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
    sp = importlib.import_module("sim_position")
    importlib.reload(sp)
    rc = sp.main(["--aircraft", "nobody", "--points", "1,2 3,4", "--once"])
    check(rc == 2, "an unknown aircraft name is refused")
    os.remove(feed_settings.settings_path())


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(t.__name__)
        t()
    print(f"\n{_passed} passed, {_failed} failed, {len(tests)} test functions")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
