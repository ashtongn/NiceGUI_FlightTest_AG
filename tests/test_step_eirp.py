"""Per-step expected EIRP of a mission plan (dpmg/payload/step_eirp.py)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dpmg.payload import payload_budget as pb  # noqa: E402
from dpmg.payload import step_eirp as se  # noqa: E402

FS = 30_720_000


def jam(sid, kind, ampl=0.5, atten=30.0, **extra):
    p = {"kind": kind, "center_hz": 1325e6, "tx_center_hz": None, "tx_atten_db": atten, "ampl": ampl,
         "seconds": 0.05, "prf": 0.0, "duty": 0.5}
    p.update(extra)
    return {"id": sid, "radio": "G40-X", "technique": f"jammer.{kind}", "radiates": True, "params": p,
            "schedule": {"start_s": 5.0, "stop_s": 15.0}}


def comm(sid, mod="QPSK", peak_fs=0.45):
    spec = {"name": sid, "sample_rate_hz": 1_920_000, "center_freq_hz": 1325e6, "loop_s": 0.2, "peak_fs": peak_fs,
            "signals": [{"modulation": mod, "symbol_rate": 9600, "offset_hz": -100_000, "amplitude": 0.9,
                         "message": "TEST"}]}
    return {"id": sid, "radio": "G40-X", "technique": "scenario_json", "radiates": True,
            "params": {"spec": spec, "spec_name": sid, "source": "console", "match_radio_rate": True,
                       "peak_fs": peak_fs, "center_hz": 1325e6, "tx_center_hz": None, "tx_atten_db": 30.0}}


def plan(steps, rate=FS):
    return {"name": "t", "radios": [{"id": "G40-X", "type": "g40", "sample_rate": rate}], "steps": steps}


def test_tone_backoff_is_its_amplitude():
    b = se.step_backoff(jam("a", "tone", ampl=0.5, freq=1e6), FS)
    assert b["how"] == "built"
    assert abs(b["peak_db"] - 6.02) < 0.05 and abs(b["avg_db"] - 6.02) < 0.1
    b = se.step_backoff(jam("a", "tone", ampl=1.0, freq=1e6), FS)
    assert abs(b["avg_db"]) < 0.1


def test_noise_average_is_well_under_its_peak():
    b = se.step_backoff(jam("a", "barrage", ampl=0.5), FS)
    assert abs(b["peak_db"] - 6.02) < 0.05
    assert 8.0 < b["papr_db"] < 16.0
    assert abs(b["avg_db"] - (b["peak_db"] + b["papr_db"])) < 1e-9


def test_pulsing_lowers_the_average_only():
    cw = se.step_backoff(jam("a", "tone", freq=1e6), FS)
    pulsed = se.step_backoff(jam("a", "tone", freq=1e6, prf=1000.0, duty=0.25), FS)
    assert abs(pulsed["peak_db"] - cw["peak_db"]) < 0.05
    assert 5.0 < pulsed["avg_db"] - cw["avg_db"] < 7.0            # a quarter of the time on: 6 dB


def test_scenario_backoff_from_peak_fs():
    b = se.step_backoff(comm("c", peak_fs=0.45), 1_920_000)
    assert b["how"] == "built" and abs(b["peak_db"] - 6.94) < 0.05
    assert 0.0 <= b["papr_db"] < 6.0


def test_file_and_unknown_steps():
    f = {"id": "f", "radio": "G40-X", "technique": "iq_playback", "radiates": True,
         "params": {"source": "file", "path": "x.c64", "center_hz": 1325e6, "tx_atten_db": 30.0}}
    assert se.step_backoff(f, FS)["how"] == "file"
    assert se.step_backoff({"technique": "record", "params": {}}, FS)["how"] == "none"


def test_rows_follow_the_plan():
    data = pb.load_profiles()
    m = plan([jam("tone30", "tone", atten=30.0, freq=1e6), jam("tone20", "tone", atten=20.0, freq=1e6),
              jam("barrage", "barrage", atten=30.0), comm("qpsk"),
              {"id": "rx", "radio": "G40-X", "technique": "scenario_decode", "radiates": False, "params": {}},
              dict(jam("other", "tone"), radio="G40-Y")])
    rows = se.plan_rows(data, "P2", m, "G40-X", 30.0, 1325.0, keyed={"tone20"})
    assert [r["id"] for r in rows] == ["tone30", "tone20", "barrage", "qpsk"]
    by = {r["id"]: r for r in rows}
    # 10 dB less attenuation is 10 dB more EIRP while the HPA is linear
    assert abs((by["tone20"]["eirp_dbm"] - by["tone30"]["eirp_dbm"]) - 10.0) < 0.3
    # same setting, lower average for noise, same peak
    assert by["barrage"]["eirp_dbm"] < by["tone30"]["eirp_dbm"] - 8.0
    assert abs(by["barrage"]["peak_eirp_dbm"] - by["tone30"]["peak_eirp_dbm"]) < 0.1
    assert by["tone20"]["keyed"] and not by["tone30"]["keyed"]
    # the row agrees with the page's own model at the same settings
    e = pb.eirp_chain(pb.profile(data, "P2"), 1325.0, 30.0, by["tone30"]["backoff_db"])
    assert abs(e["eirp_dbm"] - by["tone30"]["eirp_dbm"]) < 1e-6


def test_blank_step_values_take_the_radios():
    data = pb.load_profiles()
    s = jam("a", "tone", freq=1e6)
    s["params"]["tx_atten_db"] = None
    s["params"]["center_hz"] = None
    rows = se.plan_rows(data, "P2", plan([s]), "G40-X", 25.0, 1325.0)
    assert rows[0]["tx_atten_db"] == 25.0 and rows[0]["atten_from_radio"] and rows[0]["freq_mhz"] == 1325.0
    rows = se.plan_rows(data, "P2", plan([s]), "G40-X", None, None)
    assert rows[0]["eirp_dbm"] is None


def test_signature_changes_with_a_step():
    m = plan([jam("a", "tone", freq=1e6)])
    s0 = se.plan_signature(m, "G40-X")
    m["steps"][0]["params"]["tx_atten_db"] = 20.0
    assert se.plan_signature(m, "G40-X") != s0
    assert se.plan_signature(None, "G40-X") == ""


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
