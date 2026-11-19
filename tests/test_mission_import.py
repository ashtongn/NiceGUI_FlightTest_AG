"""Tests for the CFE importers (mission_import.py) and the rx_dwell technique.

    python3 tests/test_mission_import.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)          # repo root
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from dpmg.mission import catalog                         # noqa: E402
from dpmg.mission import mission_file as mf              # noqa: E402
from dpmg.mission import mission_import as mi            # noqa: E402
from dpmg.mission.mission_runner import step_commands   # noqa: E402

_passed = 0
_failed = 0


def check(cond, msg):
    global _passed, _failed
    if cond:
        _passed += 1
    else:
        _failed += 1
        print(f"  FAIL: {msg}")


CSV = """Freq,Gain,Time
# a comment
914.0,25,10
915.5,200,5

916.0,255,2.5
"""

XML = """<?xml version="1.0"?>
<role xmlns="urn:cfe" name="rx-tx">
  <radio serial="12345"/>
  <services>
    <service name="tone-smoke">
      <method>tone</method>
      <config><center>915.0</center><samplerate>7680000</samplerate><port>5000</port></config>
    </service>
    <Service name="play" method="StreamIQ">
      <Config center_mhz="916.25" samplerate="1.92e6" file="/data/cap.c64"/>
    </Service>
    <service name="vita-out"><method>vita</method><config/></service>
    <service name="odd"><method>teleport</method><config/></service>
  </services>
</role>"""


def test_parse_autoschedule():
    rows = mi.parse_autoschedule(CSV)
    check([r["freq_mhz"] for r in rows] == [914.0, 915.5, 916.0] and rows[2]["time_s"] == 2.5,
          f"three rows, header and comment skipped: {rows}")
    try:
        mi.parse_autoschedule("Freq,Gain,Time\n914,25\n")
        check(False, "short row raises")
    except ValueError as ex:
        check("line 2" in str(ex), f"short row names the line: {ex}")
    try:
        mi.parse_autoschedule("")
        check(False, "empty raises")
    except ValueError:
        check(True, "empty raises")


def test_steps_from_autoschedule():
    rows = mi.parse_autoschedule(CSV)
    ids = iter(["a", "b", "c"])
    steps, notes = mi.steps_from_autoschedule(rows, "G40-SIM", "g40", start_s=100.0,
                                              new_id=lambda: next(ids))
    check([s["technique"] for s in steps] == ["rx_dwell"] * 3 and not any(s["radiates"] for s in steps),
          "rx_dwell steps, none radiate")
    check([(s["schedule"]["start_s"], s["schedule"]["stop_s"]) for s in steps]
          == [(100.0, 110.0), (110.0, 115.0), (115.0, 117.5)], "back to back from the start")
    check(steps[0]["params"]["center_hz"] == 914e6, "MHz to Hz")
    check(steps[0]["params"]["rx_gain_index"] is None and steps[1]["params"]["rx_gain_index"] == 200
          and steps[2]["params"]["rx_gain_index"] == 255, "only valid G40 indexes are carried")
    check(any("25" in n and "left as set" in n for n in notes), f"note about the dropped gain: {notes}")
    steps_x, _ = mi.steps_from_autoschedule(rows, "X40-1", "x40")
    check(steps_x[1]["params"]["rx_gain_index"] == 200, "200 is valid on an X40 too")


def test_rx_dwell_catalog_and_commands():
    e = catalog.get("rx_dwell")
    check(e is not None and not e.radiates and e.needs == (catalog.TAG_RX,), "catalog entry")
    step = {"id": "s1", "radio": "R", "technique": "rx_dwell", "radiates": False,
            "params": {"center_hz": 914e6, "rx_gain_index": 200},
            "schedule": {"start_s": 0, "stop_s": 10}}
    start, stop = step_commands(step)
    check(start == [("set_center", {"hz": 914e6}), ("set_gain", {"index": 200})] and stop == [],
          f"tune and gain at start, nothing at stop: {start}")
    step["params"]["rx_gain_index"] = None
    check(step_commands(step)[0] == [("set_center", {"hz": 914e6})], "no gain: just the retune")


def test_parse_cfe_xml_lenient():
    svcs = mi.parse_cfe_xml(XML)
    check([s["method"] for s in svcs] == ["tone", "streamiq", "vita", "teleport"],
          f"methods from child tags and attributes, any case: {[s['method'] for s in svcs]}")
    check(svcs[0]["config"]["center"] == "915.0" and svcs[1]["config"]["center_mhz"] == "916.25"
          and svcs[1]["config"]["file"] == "/data/cap.c64", "config from children and attributes")
    try:
        mi.parse_cfe_xml("<role><radio/></role>")
        check(False, "no services raises")
    except ValueError:
        check(True, "no services raises")
    try:
        mi.parse_cfe_xml("<role><service>")
        check(False, "bad xml raises ValueError")
    except ValueError:
        check(True, "bad xml raises ValueError")


def test_steps_from_cfe_xml():
    svcs = mi.parse_cfe_xml(XML)
    steps, notes, rate = mi.steps_from_cfe_xml(svcs, "G40-SIM", "g40")
    check([s["technique"] for s in steps] == ["tools.tone", "iq_playback", "record"],
          f"tone, playback, record; the odd one skipped: {[s['technique'] for s in steps]}")
    check(any("teleport" in n and "skipped" in n for n in notes), f"skip note: {notes}")
    check(steps[0]["params"]["center_hz"] == 915e6 and steps[1]["params"]["center_hz"] == 916.25e6,
          "centers in Hz from a bare MHz value and a _mhz key")
    check(steps[1]["params"]["path"] == "/data/cap.c64", "playback file")
    check(rate == 7.68e6, f"first valid samplerate becomes the start rate: {rate}")
    check(steps[1]["schedule"]["start_s"] == steps[0]["schedule"]["stop_s"], "back to back")
    check(steps[0]["radiates"] and not steps[2]["radiates"], "radiates from the catalog")


def test_import_into_append_and_replace():
    m = mf.new_mission("t")
    m["authorization"] = {"reference": "IMP-1"}
    m["radios"].append({"id": "G40-SIM", "type": "g40", "techniques": ["tx", "rx"]})
    m["steps"].append({"id": "old", "radio": "G40-SIM", "technique": "tools.tone", "radiates": True,
                       "params": {}, "schedule": {"start_s": 0, "stop_s": 5}})
    rows = mi.parse_autoschedule(CSV)
    steps, _ = mi.steps_from_autoschedule(rows, "G40-SIM", "g40", start_s=5.0)
    n = mi.import_into(m, steps, "G40-SIM", replace=False)
    check(n == 4 and m["steps"][0]["id"] == "old", "append keeps the old step")
    errs = [c for c in mf.validate(m) if c.level == "error"]
    check(not errs, f"appended dwells validate (no double booking): {[c.message for c in errs]}")
    steps2, _, rate = mi.steps_from_cfe_xml(mi.parse_cfe_xml(XML), "G40-SIM", "g40")
    n = mi.import_into(m, steps2, "G40-SIM", replace=True, start_rate=rate)
    check(n == 3 and all(s["id"] != "old" for s in m["steps"]), "replace drops the radio's old steps")
    check(m["radios"][0]["sample_rate"] == 7680000, "start rate set on the radio")
    q = mf.loads(__import__("json").loads(mf.dumps(m)))
    check(len(q["steps"]) == 3, "round trip")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{_passed} passed, {_failed} failed, {len(tests)} test functions")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
