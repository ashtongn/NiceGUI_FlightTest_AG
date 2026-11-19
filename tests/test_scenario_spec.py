"""Tests for dpmg/mission/scenario_spec.py: what counts as a scenario JSON for
the planner's comm waveform step, the upload check, the inline-or-inbox size
rule and the one-line summary the step editor shows. No radio, no GUI.

    python3 tests/test_scenario_spec.py
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from dpmg.mission import scenario_spec as ss

_passed = 0
_failed = 0


def check(cond, msg):
    global _passed, _failed
    if cond:
        _passed += 1
    else:
        _failed += 1
        print(f"  FAIL: {msg}")


TWO = {
    "name": "two", "sample_rate_hz": 1_920_000,
    "signals": [
        {"modulation": "FSK", "symbol_rate": 4800, "tone_spacing_hz": 5000,
         "offset_hz": -200_000.0, "message": "a"},
        {"modulation": "QPSK", "symbol_rate": 9600, "offset_hz": 150_000.0,
         "message": "b"},
    ]}


def test_what_is_a_scenario():
    check(ss.is_scenario_spec(TWO), "a signals list is a scenario")
    check(ss.is_scenario_spec({"users": [{"id": 1}]}), "a DSSS users list is a scenario")
    check(not ss.is_scenario_spec({"signals": []}), "an empty signals list is not")
    check(not ss.is_scenario_spec({"signals": [1, 2]}), "signals must be objects")
    check(not ss.is_scenario_spec([TWO]), "a list is not")
    check(not ss.is_scenario_spec({"entries": []}), "a channel plan is not")
    check(not ss.is_scenario_spec(None), "None is not")


def test_parse_upload():
    spec = ss.parse_upload(json.dumps(TWO), "two.json")
    check(spec["signals"][1]["modulation"] == "QPSK", "upload parses to the spec")
    try:
        ss.parse_upload("{not json", "bad.json")
        check(False, "bad JSON raises")
    except ValueError as e:
        check("bad.json" in str(e) and "not valid JSON" in str(e), f"bad JSON message: {e}")
    try:
        ss.parse_upload(json.dumps({"format": "dpmg-mission/1", "steps": []}), "plan.json")
        check(False, "a mission plan raises")
    except ValueError as e:
        check("signals" in str(e), f"not-a-scenario message names signals: {e}")


def test_inline_or_inbox():
    check(not ss.needs_inbox(TWO), "a two-signal spec rides inline")
    big = {"signals": [dict(TWO["signals"][0], message="x" * 40) for _ in range(20)]}
    check(len(ss.spec_blob(big)) > ss.SPEC_INLINE_MAX, "the 20-signal spec is over the limit")
    check(ss.needs_inbox(big), "an over-limit spec goes by spec_inbox")
    check(ss.spec_blob(TWO) == json.dumps(TWO, separators=(",", ":")).encode(),
          "the blob is compact JSON")


def test_summarize():
    s = ss.summarize(TWO)
    check(s["signals"] == 2, f"two signals: {s}")
    check(s["modulations"] == ["FSK", "QPSK"], f"modulations in order: {s['modulations']}")
    # FSK: 5 kHz spacing plus 2 x 4800 skirt = 14.6 kHz around -200 kHz;
    # QPSK 9600 is about 20 kHz around +150 kHz
    check(abs(s["lo_hz"] - (-207_300.0)) < 500, f"low edge: {s['lo_hz']}")
    check(155_000 < s["hi_hz"] < 170_000, f"high edge: {s['hi_hz']}")
    check(abs(s["span_hz"] - (s["hi_hz"] - s["lo_hz"])) < 1e-6, "span is hi minus lo")
    check(s["sample_rate_hz"] == 1_920_000.0 and s["name"] == "two", "rate and name carried")
    d = ss.summarize({"users": [{"id": 1}, {"id": 2}], "dsss": {"code": "gold31"}})
    check(d["signals"] in (0, 2), f"a users spec summarizes without raising: {d}")


def test_summary_text():
    s = ss.summarize(TWO)
    off = ss.summary_text(s)
    check(off.startswith("2 signals: FSK, QPSK"), f"offline line starts with the count: {off}")
    check("written for 1.92 MSps" in off and "radio offline" in off, f"offline says so: {off}")
    ok = ss.summary_text(s, {"validation": {"errors": [], "warnings": [],
                                            "build_rate": 30_720_000.0}})
    check("builds at 30.72 MSps" in ok and ok.endswith("fits"), f"fit verdict: {ok}")
    bad = ss.summary_text(s, {"validation": {"errors": ["signal 1 reaches 2 MHz"],
                                             "build_rate": 1_920_000.0}})
    check("does not fit: signal 1 reaches 2 MHz" in bad, f"error verdict: {bad}")
    warn = ss.summary_text(s, {"validation": {"errors": [], "warnings": ["scales x16"],
                                              "build_rate": 1_920_000.0}})
    check("note: scales x16" in warn, f"warning carried: {warn}")
    one = ss.summary_text({"signals": 1, "modulations": ["CW"], "span_hz": 0})
    check(one.startswith("1 signal: CW"), f"singular: {one}")


def test_step_source_and_label():
    check(ss.step_spec_source({"spec": TWO, "spec_name": "two.json"}) == "console", "inline is console")
    check(ss.step_spec_source({"spec": None, "spec_path": "scenarios/json/a.json"}) == "radio",
          "a path is radio")
    check(ss.step_spec_source({"spec": None, "spec_path": ""}) == "", "neither is blank")
    check(ss.step_spec_source({"spec": {"nope": 1}, "spec_path": "x"}) == "radio",
          "a non-scenario inline value does not count as console")
    check(ss.step_spec_label({"spec": TWO, "spec_name": "two.json"}) == "two.json", "uploaded name")
    check(ss.step_spec_label({"spec": TWO}) == "two", "falls back to the spec's name")
    check(ss.step_spec_label({"spec_path": "scenarios/json/a.json"}) == "a.json", "radio base name")
    check(ss.step_spec_label({}) == "", "blank when nothing chosen")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{_passed} passed, {_failed} failed, {len(tests)} test functions")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
