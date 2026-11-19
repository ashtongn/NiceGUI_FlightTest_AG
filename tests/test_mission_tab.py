"""Tests for the tab's pure helpers: the time-unit conversion used by the
start/stop editor. The widget behaviour itself is checked in the browser; this
locks the number to seconds math.

    python3 tests/test_mission_tab.py
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)          # repo root
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from dpmg.mission import mission_tab as mt


_passed = 0
_failed = 0


def check(cond, msg):
    global _passed, _failed
    if cond:
        _passed += 1
    else:
        _failed += 1
        print(f"  FAIL: {msg}")


def test_to_seconds():
    check(mt._to_seconds(5, "s") == 5.0, "5 s -> 5 s")
    check(mt._to_seconds(2, "min") == 120.0, "2 min -> 120 s")
    check(mt._to_seconds(1, "hr") == 3600.0, "1 hr -> 3600 s")
    check(mt._to_seconds(500, "ms") == 0.5, "500 ms -> 0.5 s")
    check(mt._to_seconds(None, "s") == 0.0, "blank value -> 0 s")


def test_best_unit_picks_readable():
    check(mt._best_unit(0) == (0.0, "s"), "zero shows as seconds")
    check(mt._best_unit(10) == (10.0, "s"), "10 s stays seconds")
    check(mt._best_unit(120) == (2.0, "min"), "120 s -> 2 min")
    check(mt._best_unit(3600) == (1.0, "hr"), "3600 s -> 1 hr")
    check(mt._best_unit(0.5) == (500.0, "ms"), "half a second -> 500 ms")
    v, u = mt._best_unit(90)
    check((v, u) == (90.0, "s"), "90 s is not a whole minute, stays seconds")
    v, u = mt._best_unit(86400)
    check((v, u) == (24.0, "hr"), "a full day -> 24 hr")


def test_roundtrip():
    for seconds in (0.25, 1, 45, 90, 600, 3600, 7200, 86400):
        v, u = mt._best_unit(seconds)
        back = mt._to_seconds(v, u)
        check(abs(back - seconds) < 1e-9, f"{seconds} s roundtrips through {v}{u}")


def test_parse_address():
    check(mt._parse_address("192.168.0.30:47898") == ("192.168.0.30", 47898),
          "ip:port parses")
    check(mt._parse_address("192.168.0.30") == ("192.168.0.30", 47810),
          "ip alone takes the service's default command port")
    check(mt._parse_address("  g40-b.local:47810 ") == ("g40-b.local", 47810),
          "a host name works and spaces are trimmed")
    check(mt._parse_address("") is None, "blank is no address")
    check(mt._parse_address("1.2.3.4:abc") is None, "a bad port is refused")
    check(mt._parse_address("1.2.3.4:70000") is None, "a port out of range is refused")
    check(mt._parse_address(":47810") is None, "a port with no host is refused")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{_passed} passed, {_failed} failed, {len(tests)} test functions")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())


def test_os_skew_takes_half_the_round_trip_out():
    # the radio stamped 10.3 while the console sent at 10.0 and heard back at
    # 10.2: the radio runs 0.2 s ahead, not 0.3
    from dpmg.mission.mission_tab import os_skew_s, CLOCK_SKEW_WARN_S
    assert abs(os_skew_s(10.3, 10.0, 10.2) - 0.2) < 1e-9
    assert os_skew_s(None, 10.0, 10.2) is None
    assert os_skew_s("x", 10.0, 10.2) is None
    assert CLOCK_SKEW_WARN_S == 1.0


def test_parse_utc_text_and_console_clock_refusal():
    from dpmg.mission.mission_tab import parse_utc_text, set_console_clock
    assert parse_utc_text("2026-10-27 04:05:06") == 1793073906.0
    assert parse_utc_text("2026-10-27 04:05") == 1793073900.0
    assert parse_utc_text("yesterday") is None
    assert parse_utc_text("") is None
    # as a plain user sudo -n is refused: a clear message with the command to run
    ok, msg = set_console_clock(1793073906.0)
    assert ok is False and "run in a terminal: sudo" in msg and "2026-10-27 04:05:06" in msg


def test_rate_text_carries_the_rf_bandwidth():
    from dpmg.mission.mission_tab import rate_text, rf_bw_hz, bw_text
    assert rate_text(2_000_000) == "2 MSps, RF ~1.6 MHz"
    assert rate_text(1_000_000, "g40") == "1 MSps, RF ~800 kHz"
    assert rf_bw_hz(10e6, "hackrf") == 7.5e6
    assert bw_text(24_576_000) == "24.576 MHz"


def test_match_planned_radio_binds_by_name_or_prefix():
    from dpmg.mission.mission_tab import match_planned_radio
    live = ["B200MINI-3000001", "G40-SIM", "X40-0001"]
    assert match_planned_radio("B200mini", live) == "B200MINI-3000001"
    assert match_planned_radio("g40-sim", live) == "G40-SIM"
    assert match_planned_radio("B200", live) is None          # no dash after it
    assert match_planned_radio("B200MINI", ["B200MINI-1", "B200MINI-2"]) is None
    assert match_planned_radio("", live) is None


def test_every_catalog_group_has_a_timeline_kind_and_red_is_only_conflicts():
    # a group missing from the palette would fall back to tx/rx by radiating,
    # so a scenario JSON comm step could look like a plain transmitter
    from dpmg.mission import catalog
    from dpmg.mission.mission_tab import _group_color, _kind_style
    from dpmg.web import theme
    for g in catalog.GROUPS:
        assert g in theme.TIMELINE_GROUP_KIND or any(
            catalog.get(t) and catalog.get(t).group == g for t in theme.TIMELINE_TECHNIQUE_KIND), \
            f"no timeline kind for catalog group {g}"
    assert _group_color("scenario_json") == theme.TIMELINE_KINDS["comm"]["fill"]
    assert _group_color("scenario_json") != _group_color("set_rate")
    assert _kind_style("record") == theme.TIMELINE_KINDS["rx"]
    assert _kind_style("replay") == theme.TIMELINE_KINDS["playback"]
    # no fill is red: red means a conflict
    for k in theme.TIMELINE_KINDS.values():
        r, g_, b_ = (int(k["fill"][i:i + 2], 16) for i in (1, 3, 5))
        assert not (r > 150 and g_ < 90 and b_ < 90), k


class _FakeHost:
    connected = True
    session_open = True
    host_type = 0
    status = None


class _FakeMgr:
    def __init__(self, serials=()):
        self.hosts = {s: _FakeHost() for s in serials}


def _session(tmp_path):
    from dpmg.mission import map_element as me
    from dpmg.mission.mission_tab import _PlanSession
    sess = _PlanSession(_FakeMgr(["G40-SIM", "X40-SIM"]), path=str(tmp_path / "plan.json"))
    sess.mission["radios"] = [{"id": "G40-SIM", "type": "g40"}, {"id": "X40-SIM", "type": "x40"}]
    sess.mission["steps"] = [
        {"id": "l1", "radio": "G40-SIM", "technique": "tools.tone", "radiates": True,
         "params": {}, "schedule": {"start_s": 0, "stop_s": 10},
         "trigger": {"mode": "while_inside", "confirm": "auto"},
         "location": {"start": {"lat": 32.22, "lon": -110.97, "radius_m": 500}}}]
    return sess, me


def test_map_view_is_shared_state_on_the_session(tmp_path):
    sess, me = _session(tmp_path)
    radios = ["G40-SIM", "X40-SIM"]
    assert sess.view == {"radio": None, "show": me.SHOW_SELECTED}
    assert sess.shown(radios) is None                  # nothing selected: everything
    rev = sess.view_rev
    assert sess.select_radio("X40-SIM") is True and sess.view_rev == rev + 1
    assert sess.select_radio("X40-SIM") is False       # no change, no redraw
    assert sess.shown(radios) == {"X40-SIM"}
    # a second page sees the same view: it reads the session, not its own state
    assert me.shown_radios(sess.view, radios) == {"X40-SIM"}
    # the show bar: all, a set of your own, back to selected only
    assert sess.set_show(me.SHOW_ALL) and sess.shown(radios) is None
    assert sess.toggle_show("G40-SIM", radios) and sess.view["show"] == ["X40-SIM"]
    assert sess.toggle_show("G40-SIM", radios) and sess.view["show"] == ["G40-SIM", "X40-SIM"]
    assert sess.set_show(me.SHOW_SELECTED) and sess.shown(radios) == {"X40-SIM"}
    # the first tick on selected only keeps the selected radio and adds the other
    assert sess.toggle_show("G40-SIM", radios) and sess.view["show"] == ["G40-SIM", "X40-SIM"]
    assert sess.set_show({"X40-SIM"}) and sess.view["show"] == ["X40-SIM"]
    assert sess.select_radio(None) and sess.view["radio"] is None


def test_select_radio_from_the_header_needs_an_open_session(tmp_path):
    from dpmg.mission import mission_tab as mt
    mgr = _FakeMgr(["G40-SIM"])
    # no planner page has opened a session for this manager: nothing happens,
    # and no session is made as a side effect
    assert mt.select_radio(mgr, "G40-SIM") is False
    assert mt.radio_color(mgr, "G40-SIM") is None
    assert id(mgr) not in mt._SESSIONS
    sess = mt._session_for(mgr)
    sess.mission["radios"] = [{"id": "G40-SIM"}]
    seen = []
    sess.pages.add(lambda: seen.append(sess.view["radio"]))
    assert mt.select_radio(mgr, "G40-SIM") is True
    assert seen == ["G40-SIM"]                         # the pages were told
    assert mt.radio_color(mgr, "G40-SIM") == mt.me.RADIO_COLORS[0]
    assert mt.radio_color(mgr, "X40-SIM") is None
    mt._SESSIONS.pop(id(mgr), None)


def test_triggers_see_every_radio_whatever_the_map_shows(tmp_path):
    # the map is limited to the X40 while the platform enters a G40 task area:
    # the engine still reports the G40 entry
    from dpmg.mission import triggers as tg
    sess, me = _session(tmp_path)
    sess.select_radio("X40-SIM")
    assert sess.shown(["G40-SIM", "X40-SIM"]) == {"X40-SIM"}
    assert me.step_fences(sess.mission, radios=sess.shown(["G40-SIM", "X40-SIM"])) == []
    sess.triggers_armed = True
    sess.triggers.reset()
    fix = tg.Fix(32.22, -110.97, t=sess.triggers.clock())
    events = sess.triggers.update(sess.mission, fix)
    assert [(e["kind"], e["step"]["radio"]) for e in events] == [("start", "G40-SIM")]


def test_relative_timeline_reads_elapsed_time_and_fits_short_missions():
    # a new plan is relative (T+ from Run mission), not the 24 h UTC clock
    assert mt._fresh_mission()["anchor"]["mode"] == "on_tx"
    assert [mt._fmt_time(t) for t in (0, 7.5, 90, 1800, 3725)] == \
        ["0:00", "0:07.5", "1:30", "30:00", "1:02:05"]
    # empty: an hour; with steps: the plan plus room, 5 min steps, 1 h floor
    assert mt.relative_window_s(0, False) == 3600
    assert mt.relative_window_s(120, True) == 3600
    assert mt.relative_window_s(1800, True) == 3600
    assert mt.relative_window_s(3600, True) == 3900
    assert mt.relative_window_s(5000, True) == 5400


def test_parse_duration_reads_times_as_typed():
    pd = mt.parse_duration
    assert pd("2:30") == 150 and pd("1:05:00") == 3900 and pd("0:07.5") == 7.5
    assert pd("90") == 90 and pd("90s") == 90 and pd("5m") == 300
    assert pd("1.5h") == 5400 and pd("1h 30m") == 5400 and pd("500ms") == 0.5
    assert pd("T+ 12:30") == 750 and pd(" 2 min ") == 120
    # the editor shows m:ss, and what it shows reads back the same
    for t in (0, 7.5, 90, 1800, 3725):
        assert pd(mt._fmt_time(t)) == t
    for bad in ("", None, "abc", "1:75", "5 parsecs", "1:2:3:4", "-5", "5m x"):
        assert pd(bad) is None, bad


def test_bulk_edit_writes_a_shared_setting_only_where_it_exists():
    from dpmg.mission import catalog
    def step(sid, tech, start, atten=None):
        p = catalog.get(tech).fresh_params()
        if atten is not None:
            p["tx_atten_db"] = atten
        return {"id": sid, "radio": "R", "technique": tech, "params": p,
                "schedule": {"start_s": start, "stop_s": start + 30}}
    steps = [step("a", "jammer.noise", 0, 10), step("b", "jammer.tone", 60, 20),
             step("c", "rx_dwell", 120)]
    shared = {f.key: (ids, common) for f, ids, common in mt.bulk_fields(steps)}
    ids, common = shared["tx_atten_db"]
    assert ids == ["a", "b"] and common is mt.MIXED      # rx_dwell has no TX atten
    n = mt.apply_bulk(steps, {"tx_atten_db": 30}, priority=2, duration_s=90)
    assert n == 3
    assert [s["params"].get("tx_atten_db") for s in steps] == [30, 30, None]
    assert "tx_atten_db" not in steps[2]["params"]
    assert all(s["priority"] == 2 for s in steps)
    assert [s["schedule"] for s in steps][1] == {"start_s": 60, "stop_s": 150}
    shared = {f.key: common for f, _, common in mt.bulk_fields(steps)}
    assert shared["tx_atten_db"] == 30
