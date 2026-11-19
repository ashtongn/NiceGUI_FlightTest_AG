"""Unit tests for the mission file module: load, save, validate.

Run from this directory with python3:

    python3 tests/test_mission_file.py

No external test runner needed; it uses a tiny assert harness so it runs the
same way on the dev box and anywhere the repo is importable.
"""

import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)          # repo root
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from dpmg.mission import mission_file as mf
from dpmg.mission import catalog


# tiny harness

_passed = 0
_failed = 0


def check(cond, msg):
    global _passed, _failed
    if cond:
        _passed += 1
    else:
        _failed += 1
        print(f"  FAIL: {msg}")


def errors(conflicts):
    return [c for c in conflicts if c.level == "error"]


def messages(conflicts):
    return " | ".join(c.message for c in conflicts)


# fixtures

def one_radio_plan(steps, authorization="TEST-1"):
    return {
        "format": mf.FORMAT,
        "name": "test plan",
        "authorization": {"reference": authorization, "note": ""},
        "radios": [{"id": "G40-0001", "type": "g40",
                    "techniques": ["tx", "rx", "secure"]}],
        "steps": steps,
    }


def step(sid, tech, start, stop, radio="G40-0001", radiates=True, params=None):
    return {"id": sid, "radio": radio, "technique": tech,
            "radiates": radiates, "params": params or {},
            "schedule": {"start_s": start, "stop_s": stop}}


# tests

def test_roundtrip_save_load():
    plan = one_radio_plan([step("s1", "jammer.barrage", 0, 30)])
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "plan.dpmg-mission.json")
        mf.save(plan, path)
        back = mf.load(path)
    check(back["name"] == "test plan", "name survives roundtrip")
    check(len(back["steps"]) == 1, "step survives roundtrip")
    check(back["format"] == mf.FORMAT, "format stamped on save")


def test_clean_plan_validates():
    plan = one_radio_plan([
        step("s1", "jammer.barrage", 0, 30),
        step("s2", "iq_playback", 40, 70, radiates=True,
             params={"source": "scenario", "name": "mod_coding_card"}),
    ])
    conflicts = mf.validate(plan)
    check(errors(conflicts) == [], f"clean plan has no errors: {messages(conflicts)}")


def test_double_booked_radio():
    plan = one_radio_plan([
        step("s1", "jammer.barrage", 0, 30),
        step("s2", "jammer.tone", 20, 50),      # overlaps s1 on the same radio
    ])
    conflicts = errors(mf.validate(plan))
    check(any("double booked" in c.message for c in conflicts),
          "overlap on one radio is a conflict")


def test_touching_steps_are_not_overlap():
    plan = one_radio_plan([
        step("s1", "jammer.barrage", 0, 30),
        step("s2", "jammer.tone", 30, 60),      # starts exactly when s1 stops
    ])
    conflicts = errors(mf.validate(plan))
    check(not any("double booked" in c.message for c in conflicts),
          "back-to-back steps do not count as overlap")


def test_two_radios_same_time_ok():
    plan = {
        "format": mf.FORMAT, "name": "coordinated",
        "authorization": {"reference": "TEST-2"},
        "radios": [{"id": "A", "techniques": ["tx"]},
                   {"id": "B", "techniques": ["tx"]}],
        "steps": [step("s1", "jammer.barrage", 0, 30, radio="A"),
                  step("s2", "jammer.tone", 0, 30, radio="B")],
    }
    conflicts = errors(mf.validate(plan))
    check(conflicts == [], f"same start across radios is allowed: {messages(conflicts)}")


def test_radio_cannot_run_technique():
    plan = {
        "format": mf.FORMAT, "name": "rx only",
        "authorization": {"reference": "TEST-3"},
        "radios": [{"id": "RXBOX", "techniques": ["rx"]}],    # no tx tag
        "steps": [step("s1", "jammer.barrage", 0, 30, radio="RXBOX")],
    }
    conflicts = errors(mf.validate(plan))
    check(any("cannot run" in c.message for c in conflicts),
          "tx technique on an rx-only radio is a conflict")


def test_record_on_rx_only_is_ok():
    plan = {
        "format": mf.FORMAT, "name": "rx only",
        "authorization": {"reference": "TEST-3b"},
        "radios": [{"id": "RXBOX", "techniques": ["rx"]}],
        "steps": [step("s1", "record", 0, 10, radio="RXBOX", radiates=False)],
    }
    conflicts = errors(mf.validate(plan))
    check(conflicts == [], f"record needs only rx: {messages(conflicts)}")


def test_step_on_unknown_radio():
    plan = one_radio_plan([step("s1", "jammer.barrage", 0, 30, radio="GHOST")])
    conflicts = errors(mf.validate(plan))
    check(any("does not list" in c.message for c in conflicts),
          "a step on a radio not in the plan is a conflict")


def test_unknown_technique():
    plan = one_radio_plan([step("s1", "jammer.deathray", 0, 30)])
    conflicts = errors(mf.validate(plan))
    check(any("unknown technique" in c.message for c in conflicts),
          "a technique not in the catalog is a conflict")


def test_radiating_without_authorization():
    plan = one_radio_plan([step("s1", "jammer.barrage", 0, 30)],
                          authorization="")
    conflicts = errors(mf.validate(plan))
    check(any("no authorization" in c.message for c in conflicts),
          "a radiating step with no authorization is a conflict")


def test_step_authorization_overrides_missing_mission_authorization():
    plan = one_radio_plan([step("s1", "jammer.barrage", 0, 30)],
                          authorization="")
    plan["steps"][0]["authorization"] = {"reference": "STEP-OK"}
    conflicts = errors(mf.validate(plan))
    check(not any("no authorization" in c.message for c in conflicts),
          "a per-step authorization covers a radiating step")


def test_non_radiating_needs_no_authorization():
    plan = one_radio_plan([step("s1", "record", 0, 10, radiates=False)],
                          authorization="")
    conflicts = errors(mf.validate(plan))
    check(not any("no authorization" in c.message for c in conflicts),
          "a non-radiating step needs no authorization")


def test_times_out_of_order():
    plan = one_radio_plan([step("s1", "jammer.barrage", 30, 10)])
    conflicts = errors(mf.validate(plan))
    check(any("not after start" in c.message for c in conflicts),
          "stop before start is a conflict")


def test_negative_start():
    plan = one_radio_plan([step("s1", "jammer.barrage", -5, 10)])
    conflicts = errors(mf.validate(plan))
    check(any("before mission start" in c.message for c in conflicts),
          "a negative start time is a conflict")


def test_step_over_limit():
    plan = one_radio_plan([step("s1", "jammer.barrage", 0, mf.MAX_STEP_S + 100)])
    conflicts = errors(mf.validate(plan))
    check(any("per-step limit" in c.message for c in conflicts),
          "a step longer than the per-step limit is a conflict")


def test_mission_over_limit():
    start = mf.MAX_MISSION_S + 100
    plan = one_radio_plan([step("s1", "jammer.barrage", start, start + 30)])
    conflicts = errors(mf.validate(plan))
    check(any("mission limit" in c.message for c in conflicts),
          "a step past the mission limit is a conflict")


def test_duration_instead_of_stop():
    plan = one_radio_plan([{
        "id": "s1", "radio": "G40-0001", "technique": "jammer.barrage",
        "radiates": True, "params": {},
        "schedule": {"start_s": 10, "duration_s": 20}}])
    conflicts = errors(mf.validate(plan))
    check(conflicts == [], f"duration_s instead of stop_s works: {messages(conflicts)}")
    check(mf.step_stop_s(plan["steps"][0]) == 30, "stop derives from duration")


def test_load_rejects_wrong_format():
    bad = {"format": "something-else", "steps": []}
    try:
        mf.loads(bad)
        check(False, "loads should reject an unknown format")
    except mf.MissionError as e:
        check("not a dpmg-mission" in str(e), f"clear message for bad format: {e}")


def test_load_rejects_newer_version():
    newer = {"format": "dpmg-mission/9", "steps": []}
    try:
        mf.loads(newer)
        check(False, "loads should reject a newer version")
    except mf.MissionError as e:
        check("this tool reads" in str(e), f"clear message for newer version: {e}")


def test_load_rejects_bad_json():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "bad.json")
        with open(path, "w") as f:
            f.write("{not json")
        try:
            mf.load(path)
            check(False, "load should reject bad JSON")
        except mf.MissionError as e:
            check("not valid JSON" in str(e), f"clear message for bad JSON: {e}")


def test_summary():
    plan = one_radio_plan([
        step("s1", "jammer.barrage", 0, 30),
        step("s2", "record", 40, 60, radiates=False),
    ])
    s = mf.summary(plan)
    check(s["steps"] == 2, "summary counts steps")
    check(s["radiating"] == 1, "summary counts radiating steps")
    check(s["span_s"] == 60, "summary span is the last stop")
    check(s["radios"] == ["G40-0001"], "summary lists radios")


def test_catalog_covers_design_groups():
    groups = {g for g, _ in catalog.groups_for_tab()}
    for want in ("Jammers", "Comms", "IQ playback", "Tools generators", "Transponder",
                 "Record and replay", "Secure"):
        check(want in groups, f"catalog has the {want} group")
    for tech in ("jammer.barrage", "jammer.noise", "jammer.tone", "jammer.comb",
                 "jammer.swept", "jammer.chirp", "jammer.modulated",
                 "iq_playback", "scenario_json", "tools.tone", "transponder",
                 "record", "replay"):
        check(catalog.is_known(tech), f"catalog has {tech}")


def test_parse_clock():
    check(mf.parse_clock("1400") == 14 * 3600, "1400 -> 14:00")
    check(mf.parse_clock("14:00") == 14 * 3600, "14:00 -> 14:00")
    check(mf.parse_clock("09:30:15") == 9 * 3600 + 30 * 60 + 15, "HH:MM:SS parses")
    check(mf.parse_clock("0000") == 0, "midnight is 0 s")
    check(mf.parse_clock("9:05") == 9 * 3600 + 5 * 60, "H:MM parses")
    check(mf.parse_clock("2500") is None, "25:00 is out of range")
    check(mf.parse_clock("14:99") is None, "bad minutes rejected")
    check(mf.parse_clock("") is None, "blank is None")
    check(mf.parse_clock("abc") is None, "garbage is None")


def test_clock_labels():
    check(mf.clock_hms(0) == "00:00:00", "0 s -> 00:00:00")
    check(mf.clock_hms(14 * 3600) == "14:00:00", "14:00")
    check(mf.clock_hm(14 * 3600 + 30 * 60) == "14:30", "HH:MM label")
    check(mf.clock_hms(mf.DAY_S) == "00:00:00", "a full day wraps to 00:00:00")


def test_clock_roundtrip_with_parse():
    for t in ("00:00", "14:00", "23:59", "06:45"):
        secs = mf.parse_clock(t)
        check(mf.clock_hm(secs) == t, f"{t} roundtrips through seconds")


def test_anchor_defaults_on_tx():
    m = mf.new_mission()
    check(mf.anchor_mode(m) == "on_tx", "a new mission is on_tx by default")
    check(not mf.is_clock_anchored(m), "on_tx is not clock anchored")
    check(mf.anchor_midnight_epoch(m) is None, "no midnight epoch when on_tx")


def test_anchor_clock_epoch():
    m = mf.new_mission()
    m["anchor"] = {"mode": "clock", "date": "2026-10-23", "tz": "UTC"}
    check(mf.is_clock_anchored(m), "clock mode detected")
    ep = mf.anchor_midnight_epoch(m)
    import datetime
    want = datetime.datetime(2026, 10, 23, tzinfo=datetime.timezone.utc).timestamp()
    check(ep == want, "midnight epoch matches the UTC date")


def test_clock_anchor_needs_date():
    m = one_radio_plan([step("s1", "jammer.barrage", 0, 30)])
    m["anchor"] = {"mode": "clock", "date": "", "tz": "UTC"}
    conflicts = errors(mf.validate(m))
    check(any("valid anchor date" in c.message for c in conflicts),
          "clock anchor with no date is a conflict")
    m["anchor"]["date"] = "2026-10-23"
    conflicts = errors(mf.validate(m))
    check(not any("anchor date" in c.message for c in conflicts),
          "a valid date clears the anchor conflict")


def test_clock_anchor_step_at_1400():
    # a step scheduled at 14:00 for 30 min on a clock-anchored day
    start = mf.parse_clock("1400")
    m = one_radio_plan([step("s1", "jammer.barrage", start, start + 1800)])
    m["anchor"] = {"mode": "clock", "date": "2026-10-23", "tz": "UTC"}
    conflicts = errors(mf.validate(m))
    check(conflicts == [], f"a 14:00 half-hour step validates: {messages(conflicts)}")
    check(mf.clock_hm(mf.step_start_s(m["steps"][0])) == "14:00", "start reads 14:00")
    check(mf.clock_hm(mf.step_stop_s(m["steps"][0])) == "14:30", "stop reads 14:30")


def test_rx_gain_index_range():
    def plan(idx, rtype="g40"):
        m = one_radio_plan([step("s1", "jammer.barrage", 0, 10,
                                 params={"rx_gain_index": idx})])
        m["radios"][0]["type"] = rtype
        return errors(mf.validate(m))

    check(plan(231) == [], "a G40 index inside 187-255 is clean")
    check(plan(None) == [], "leave as set is clean")
    check(any("RX gain index 300" in c.message for c in plan(300)),
          "an index above 255 is flagged")
    check(any("RX gain index 190" in c.message for c in plan(190, "x40")),
          "190 is below the X40 minimum of 195")
    check(plan(190, "g40") == [], "190 is fine on a G40")


def test_replay_needs_a_recording():
    blank = one_radio_plan([step("s1", "replay", 0, 10, params={"name": ""})])
    errs = errors(mf.validate(blank))
    check(any("no recording chosen" in c.message and c.step_id == "s1"
              for c in errs),
          f"a replay with no recording is flagged: {messages(errs)}")
    named = one_radio_plan([step("s1", "replay", 0, 10,
                                 params={"name": "rec_0001", "gain_db": 0.0})])
    check(errors(mf.validate(named)) == [], "a replay with a recording is clean")
    check(catalog.get("replay").fresh_params().get("name") == "",
          "replay starts with no recording, so the picker opens on add")


def test_iq_playback_needs_a_file():
    blank = one_radio_plan([step("s1", "iq_playback", 0, 30,
                                 params={"source": "file", "path": ""})])
    errs = errors(mf.validate(blank))
    check(any("no file chosen" in c.message and c.step_id == "s1" for c in errs),
          f"a blank IQ playback is flagged: {messages(errs)}")
    picked = one_radio_plan([step("s1", "iq_playback", 0, 30,
                                  params={"source": "file",
                                          "path": "/app_svc/data/waveforms/a.c64"})])
    check(errors(mf.validate(picked)) == [], "an IQ playback with a file is clean")
    old = one_radio_plan([step("s1", "iq_playback", 0, 30,
                               params={"source": "scenario", "name": "mod_coding_card"})])
    check(errors(mf.validate(old)) == [], "an older plan's name key still counts")


def test_catalog_iq_playback_default_is_blank_file():
    from dpmg.mission import catalog
    p = catalog.get("iq_playback").fresh_params()
    check(p.get("source") == "file" and p.get("path") == "",
          f"iq_playback starts as an unpicked file: {p}")


TWO_SIG = {"name": "two", "sample_rate_hz": 1_920_000, "signals": [
    {"modulation": "FSK", "symbol_rate": 4800, "tone_spacing_hz": 5000,
     "offset_hz": -200_000.0, "message": "a"},
    {"modulation": "QPSK", "symbol_rate": 9600, "offset_hz": 150_000.0, "message": "b"}]}


def test_scenario_json_needs_a_spec_or_a_path():
    fresh = catalog.get("scenario_json").fresh_params()
    blank = one_radio_plan([step("s1", "scenario_json", 0, 30, params=dict(fresh))])
    errs = errors(mf.validate(blank))
    check(any("no scenario JSON" in c.message and c.step_id == "s1" for c in errs),
          f"a blank comm waveform is flagged as needing input: {messages(errs)}")
    inline = one_radio_plan([step("s1", "scenario_json", 0, 30,
                                  params=dict(fresh, source="console", spec=TWO_SIG,
                                              spec_name="two.json"))])
    check(errors(mf.validate(inline)) == [], f"an uploaded spec is clean: {messages(mf.validate(inline))}")
    on_radio = one_radio_plan([step("s1", "scenario_json", 0, 30,
                                    params=dict(fresh, source="radio",
                                                spec_path="scenarios/json/fsk_sweep.json"))])
    check(errors(mf.validate(on_radio)) == [], "a radio file is clean")
    junk = one_radio_plan([step("s1", "scenario_json", 0, 30,
                                params=dict(fresh, spec={"entries": [1]}))])
    errs = errors(mf.validate(junk))
    check(any("not a scenario JSON" in c.message for c in errs),
          f"a hand-edited non-scenario spec is flagged: {messages(errs)}")
    no_auth = one_radio_plan([step("s1", "scenario_json", 0, 30,
                                   params=dict(fresh, spec=TWO_SIG))], authorization="")
    check(any("authorization" in c.message for c in errors(mf.validate(no_auth))),
          "a comm waveform radiates, so it needs authorization")


def test_scenario_json_round_trips_with_the_spec_inline():
    fresh = catalog.get("scenario_json").fresh_params()
    m = one_radio_plan([step("s1", "scenario_json", 0, 30,
                             params=dict(fresh, source="console", spec=TWO_SIG,
                                         spec_name="two.json", tx_atten_db=20))])
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "plan.json")
        mf.save(m, path)
        back = mf.load(path)
    p = back["steps"][0]["params"]
    check(p["spec"] == TWO_SIG and p["spec_name"] == "two.json" and p["tx_atten_db"] == 20,
          "the plan file carries the uploaded spec")
    check(back["steps"][0]["technique"] == "scenario_json", "technique kept")


def test_old_scenario_playback_migrates_on_load():
    # a plan saved before the Comm waveform entry: an IQ playback with source
    # scenario and the JSON's path on the radio
    m = one_radio_plan([
        step("s1", "iq_playback", 0, 30,
             params={"source": "scenario", "path": "scenarios/json/mod_coding_card.json",
                     "center_hz": 915e6, "tx_center_hz": None, "tx_atten_db": 10,
                     "rx_gain_index": None}),
        step("s2", "iq_playback", 40, 60,
             params={"source": "scenario", "name": "fsk_sweep"}),
        step("s3", "iq_playback", 70, 90,
             params={"source": "file", "path": "/app_svc/data/waveforms/cap.c64"})])
    back = mf.loads(json.loads(mf.dumps(m)))
    s1, s2, s3 = back["steps"]
    check(s1["technique"] == "scenario_json" and s1["radiates"] is True, "scenario playback became a comm waveform")
    p1 = s1["params"]
    check(p1["source"] == "radio" and p1["spec_path"] == "scenarios/json/mod_coding_card.json"
          and p1["spec"] is None and p1["match_radio_rate"] is True,
          f"the radio path moved to spec_path, built at the radio's rate: {p1}")
    check(p1["center_hz"] == 915e6 and p1["tx_atten_db"] == 10, "RF settings kept")
    check("path" not in p1 and "name" not in p1, "old keys dropped")
    check(s1["schedule"] == {"start_s": 0, "stop_s": 30}, "times kept")
    check(s2["technique"] == "scenario_json" and s2["params"]["spec_path"] == "fsk_sweep",
          "the older name key is the path too")
    check(s3["technique"] == "iq_playback" and s3["params"]["path"].endswith("cap.c64"),
          "an IQ file playback is left alone")
    check(errors(mf.validate(back)) == [], f"the migrated plan is clean: {messages(mf.validate(back))}")
    # migrate_step on its own, for callers that build steps without loads
    st = step("x", "iq_playback", 0, 1, params={"source": "scenario", "path": "a.json"})
    check(mf.migrate_step(st)["technique"] == "scenario_json", "migrate_step works alone")
    st = step("y", "tools.tone", 0, 1, params={"freq": 1.0})
    check(mf.migrate_step(dict(st)) == st, "other steps unchanged")


def warnings(conflicts):
    return [c for c in conflicts if c.level == "warning"]


def test_start_rate_must_be_valid_for_the_radio():
    m = one_radio_plan([step("s1", "jammer.barrage", 0, 10)])
    m["radios"][0]["sample_rate"] = 7_680_000
    check(warnings(mf.validate(m)) == [], "a valid G40 start rate is clean")
    m["radios"][0]["sample_rate"] = 50_000_000      # an X40 rate on a G40
    check(any("start rate" in c.message for c in warnings(mf.validate(m))),
          "an invalid start rate for the radio type is warned")


def test_set_rate_step_rate_checked_by_type():
    good = one_radio_plan([step("r1", "set_rate", 0, 2, radiates=False,
                                params={"sample_rate": 7_680_000})])
    check(mf.validate(good) == [], f"a valid set_rate is clean: {messages(mf.validate(good))}")
    bad = one_radio_plan([step("r1", "set_rate", 0, 2, radiates=False,
                               params={"sample_rate": 50_000_000})])
    check(any("not a valid rate" in c.message for c in warnings(mf.validate(bad))),
          "an invalid set_rate rate is warned")
    empty = one_radio_plan([step("r1", "set_rate", 0, 2, radiates=False,
                                 params={"sample_rate": 0})])
    check(any("has no rate" in c.message for c in errors(mf.validate(empty))),
          "a set_rate with no rate is an error")


def test_set_rate_needs_no_authorization():
    # set_rate does not radiate, so it does not need an authorization reference
    m = one_radio_plan([step("r1", "set_rate", 0, 2, radiates=False,
                             params={"sample_rate": 7_680_000})], authorization="")
    check(not any("authorization" in c.message for c in mf.validate(m)),
          "a non-radiating rate change needs no authorization")


def test_step_inside_a_rate_change_is_an_error():
    # a rate change at 8s then a keyed step at 9s, inside the re-init time
    m = one_radio_plan([
        step("r1", "set_rate", 8, 8.5, radiates=False,
             params={"sample_rate": 7_680_000}),
        step("s1", "jammer.barrage", 9, 20)])
    check(any("takes about 50 s to re-initialize" in c.message and c.step_id == "s1" and c.level == "error"
              for c in mf.validate(m)),
          f"a step inside the re-init time is an error: {messages(mf.validate(m))}")
    # the same change well ahead (at 0, step at 60) is clean
    ok = one_radio_plan([
        step("r1", "set_rate", 0, 2, radiates=False,
             params={"sample_rate": 7_680_000}),
        step("s1", "jammer.barrage", 60, 70)])
    check(not any("re-initialize" in c.message for c in mf.validate(ok)),
          "a rate change with enough lead is clean")
    # a step still running when the change starts
    run = one_radio_plan([
        step("s1", "jammer.barrage", 0, 20),
        step("r1", "set_rate", 10, 12, radiates=False, params={"sample_rate": 7_680_000})])
    check(any("still running" in c.message and c.level == "error" for c in mf.validate(run)),
          "a step running through a rate change is an error")
    # a change to the rate the radio already has costs nothing
    same = one_radio_plan([
        step("r1", "set_rate", 0, 2, radiates=False, params={"sample_rate": 30_720_000}),
        step("s1", "jammer.barrage", 3, 10)])
    same["radios"][0]["sample_rate"] = 30_720_000
    check(not any("re-initialize" in c.message for c in mf.validate(same)),
          "no re-init block when the rate does not change")


def test_new_mission_has_map_block():
    m = mf.new_mission()
    check(m.get("map") == {}, "a new mission carries an empty map block")


def test_loads_fills_map_and_rejects_bad_map():
    m = mf.loads({"format": mf.FORMAT, "steps": [], "radios": []})
    check(m.get("map") == {}, "loads fills a missing map block")
    try:
        mf.loads({"format": mf.FORMAT, "map": "no"})
        check(False, "a non-object map should raise")
    except mf.MissionError:
        check(True, "a non-object map raises MissionError")


def test_valid_point_geofence_is_clean():
    s = step("s1", "jammer.barrage", 0, 30)
    s["location"] = {"start": {"lat": 34.1, "lon": -118.2, "radius_m": 500}}
    m = one_radio_plan([s])
    check(errors(mf.validate(m)) == [],
          f"a valid start point geofence is clean: {messages(mf.validate(m))}")


def test_valid_polygon_geofence_is_clean():
    s = step("s1", "jammer.barrage", 0, 30)
    # polygon vertices are [lon, lat]
    s["location"] = {"stop": {"polygon": [[-118.2, 34.1], [-118.0, 34.1],
                                          [-118.0, 34.3]]}}
    m = one_radio_plan([s])
    check(errors(mf.validate(m)) == [], "a valid stop polygon is clean")


def test_bad_point_geofence_flagged():
    s = step("s1", "jammer.barrage", 0, 30)
    s["location"] = {"start": {"lat": 200, "lon": 0, "radius_m": 500}}
    errs = errors(mf.validate(m := one_radio_plan([s])))
    check(any("start geofence is not valid" in c.message and c.step_id == "s1"
              for c in errs), f"out-of-range lat is flagged: {messages(errs)}")


def test_zero_radius_flagged():
    s = step("s1", "jammer.barrage", 0, 30)
    s["location"] = {"start": {"lat": 34.1, "lon": -118.2, "radius_m": 0}}
    errs = errors(mf.validate(one_radio_plan([s])))
    check(any("start geofence is not valid" in c.message for c in errs),
          "a zero radius is flagged")


def test_short_polygon_flagged():
    s = step("s1", "jammer.barrage", 0, 30)
    s["location"] = {"stop": {"polygon": [[0, 0], [1, 1]]}}
    errs = errors(mf.validate(one_radio_plan([s])))
    check(any("stop geofence is not valid" in c.message for c in errs),
          "a two-point polygon is flagged")


def test_no_location_is_clean():
    m = one_radio_plan([step("s1", "jammer.barrage", 0, 30)])
    check(errors(mf.validate(m)) == [], "a step with no location is clean")


def test_geofence_center():
    check(mf.geofence_center({"lat": 34.1, "lon": -118.2, "radius_m": 1})
          == (34.1, -118.2), "point centre is its lat/lon")
    c = mf.geofence_center({"polygon": [[-118.0, 34.0], [-118.0, 36.0],
                                        [-116.0, 35.0]]})
    check(abs(c[0] - 35.0) < 1e-9, "polygon centre lat is the vertex mean")
    check(mf.geofence_center({}) is None, "empty geofence has no centre")


def test_location_survives_roundtrip():
    s = step("s1", "jammer.barrage", 0, 30)
    s["location"] = {"start": {"lat": 34.1, "lon": -118.2, "radius_m": 500}}
    plan = one_radio_plan([s])
    plan["map"] = {"base_map": "world-base",
                   "flight_path": {"path": "paths/a.geojson"}}
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "p.dpmg-mission.json")
        mf.save(plan, path)
        back = mf.load(path)
    check(back["steps"][0]["location"]["start"]["radius_m"] == 500,
          "a step location survives save and load")
    check(back["map"]["base_map"] == "world-base", "the map block survives")


def test_location_task_model_and_validation():
    gf = {"lat": 33.45, "lon": -112.07, "radius_m": 500}
    t = mf.new_location_task("L1", "jammer.barrage", "G40-SIM", gf, mode="dwell",
                             dwell_s=45, priority=2, confirm="always")
    check(mf.is_location_task(t) and t["trigger"]["mode"] == "dwell"
          and t["priority"] == 2 and t["radiates"] and "schedule" not in t, "task built")
    check(mf.task_length_s(t) == 45, "dwell length")
    check(mf.step_priority({"id": "x"}) == 5 and mf.step_priority(t) == 2
          and mf.step_priority({"id": "y", "priority": 99}) == 9
          and mf.step_priority({"id": "z", "priority": "bad"}) == 5, "priority defaults and clamps")
    m = mf.new_mission("t")
    m["authorization"] = {"reference": "A-1"}
    m["radios"].append({"id": "G40-SIM", "techniques": ["tx", "rx"]})
    m["steps"].append(t)
    m["steps"].append({"id": "s1", "radio": "G40-SIM", "technique": "tools.tone",
                       "radiates": True, "params": {}, "schedule": {"start_s": 0, "stop_s": 30}})
    out = mf.validate(m)
    errs = [c for c in out if c.level == "error"]
    warns = [c for c in out if c.level == "warning"]
    check(not errs, f"a task and a step on one radio is no error: {[c.message for c in errs]}")
    check(any("shares radio G40-SIM" in c.message and "stops the timeline step" in c.message
              for c in warns), f"collision warning says the task wins: {[c.message for c in warns]}")
    t["priority"] = 5
    check(any("equal priority" in c.message for c in mf.validate(m)), "equal priority warning")
    t["priority"] = 7
    check(any("waits for the radio" in c.message for c in mf.validate(m)), "lower priority waits")
    # no time checks for a task, but its trigger must make sense
    t["trigger"]["dwell_s"] = 0
    check(any("no run length" in c.message for c in mf.validate(m)), "zero dwell is an error")
    t["trigger"]["dwell_s"] = 45
    t["trigger"]["mode"] = "sometimes"
    check(any("mode" in c.message for c in mf.validate(m)), "bad mode is an error")
    t["trigger"]["mode"] = "while_inside"
    t["location"] = {}
    check(any("no area" in c.message for c in mf.validate(m)), "a task needs an area")
    t["location"] = {"start": gf}
    m["steps"][1]["priority"] = 12
    check(any("priority 12" in c.message for c in mf.validate(m)), "priority out of range warns")
    a = mf.new_location_task("L2", "tools.tone", "G40-SIM", gf, confirm="auto")
    check(a["trigger"]["confirm"] == "auto" and "auto" in mf.CONFIRM_LABELS, "automatic confirm rule")
    check(mf.new_location_task("L3", "tools.tone", "G40-SIM", gf, confirm="maybe")["trigger"]["confirm"] == "once",
          "unknown confirm rule falls back to once")
    # round trip keeps the blocks
    q = mf.loads(json.loads(mf.dumps(m)))
    check(mf.is_location_task(q["steps"][0]) and q["steps"][0]["trigger"]["confirm"] == "always",
          "trigger block survives save and load")


def test_step_ports():
    from dpmg.mission import ports
    m = mf.new_mission("ports")
    m["radios"] = [{"id": "G40-SIM", "type": "g40", "techniques": ["rx", "tx"]}]
    m["authorization"] = {"reference": "T-1"}

    def st(sid, start, stop, rx=None, tx=None):
        s = {"id": sid, "radio": "G40-SIM", "technique": "tools.tone", "radiates": True,
             "params": {"kind": "tone"}, "schedule": {"start_s": start, "stop_s": stop}}
        ports.set_step_ports(s, rx, tx)
        return s

    # helpers: options per type, mapping to init fields, current ports from a snapshot
    check(ports.rx_options("g40") == list(ports.G40_PORTS) and ports.tx_options("b200") == ["TX/RX"],
          "port options per radio type")
    f = ports.init_fields("g40", "TRx1B", "TRx1A")
    check(f["rx_antenna"] == "RXA1" and f["tx_antenna"] == "TXA1"
          and f["extras"] == {"card": 1, "tx_card": 0, "chan_mode": "single"},
          f"cross-card G40 pair maps to single chan_mode with tx_card: {f}")
    f = ports.init_fields("g40", "TRx1A", "TRx2A")
    check(f["extras"]["chan_mode"] == "dual", "same-card pair is dual chan_mode")
    check(ports.init_fields("b200", "RX2", "TX/RX") == {"rx_antenna": "RX2", "tx_antenna": "TX/RX"},
          "B200 ports are the antenna names")
    snap = {"rx_antenna": "RXA1", "card": 1, "tx_antenna": "TXA1", "tx_card": 0}
    check(ports.current_ports(snap, "g40") == ("TRx1B", "TRx1A"), "snapshot reads back as jacks")
    check(not ports.needs_reinit(snap, "g40", "TRx1B", "TRx1A"), "same jacks: no re-init")
    check(ports.needs_reinit(snap, "g40", None, "TRx2B"), "another TX jack: re-init")

    # validation: a name the radio does not have, a pair that cannot stream, a change
    m["steps"] = [st("a", 0, 10, rx="TRx9Z")]
    check(any("TRx9Z" in c.message and c.level == "error" for c in mf.validate(m)),
          "unknown port name is an error")
    m["steps"] = [st("a", 0, 10, rx="TRx2A", tx="TRx1A")]
    check(any("cannot co-stream" in c.message and c.level == "error" for c in mf.validate(m)),
          "RX on the 2 jack with TX on the 1 jack of one card is refused")
    m["steps"] = [st("a", 0, 10, rx="TRx1B", tx="TRx1A"), st("b", 20, 30, rx="TRx1B", tx="TRx1A"),
                  st("c", 40, 50, tx="TRx2B")]
    cs = mf.validate(m)
    check(not [c for c in cs if c.level == "error"], f"clean port plan has no errors: {cs}")
    warns = [c for c in cs if c.level == "warning" and "re-initializes" in c.message]
    check([c.step_id for c in warns] == ["c"], f"only the step that changes ports warns: {warns}")
    check("TX TRx2B" in warns[0].message and "RX TRx1B" in warns[0].message,
          "the warning names the ports the radio moves to")
    q = mf.loads(json.loads(mf.dumps(m)))
    check(ports.step_ports(q["steps"][2]) == (None, "TRx2B"), "ports survive save and load")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{_passed} passed, {_failed} failed, {len(tests)} test functions")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
