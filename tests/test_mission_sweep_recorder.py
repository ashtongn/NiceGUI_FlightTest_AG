"""bench/mission_sweep_recorder.py with a fake radio and a fake FieldFox: steps are
matched from get_config, the reading and the off reading land in the right sheet
rows of the copy, the template is untouched, and an over-limit reading sends
stop_tx."""
import argparse
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "bench"))
openpyxl = pytest.importorskip("openpyxl")

import mission_sweep_recorder as msr  # noqa: E402
from dpmg.payload import payload_budget as pb  # noqa: E402


def _mission(tmp_path, steps):
    p = tmp_path / "m.dpmg-mission.json"
    p.write_text(json.dumps({"format": "dpmg-mission/1", "steps": [
        {"id": sid, "params": {"tx_center_hz": f * 1e6, "tx_atten_db": att, "freq": 100e3, "ampl": 0.5},
         "schedule": {"start_s": i * 40.0}} for i, (sid, f, att) in enumerate(steps)]}))
    return str(p)


def _sheet(tmp_path, rows):
    wb = openpyxl.Workbook()
    wi = wb.active
    wi.title = "Inputs"
    wi["A5"] = "G40 TX port driving the combiner"
    wi["B5"] = "TRx1A"
    ws = wb.create_sheet("Sweep")
    for i, (f, att) in enumerate(rows):
        ws[f"A{5 + i}"] = f
        ws[f"B{5 + i}"] = att
    p = tmp_path / "sheet.xlsx"
    wb.save(p)
    return str(p)


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeLink:
    """get_config driven by the fake clock: (t0, t1, center_hz, atten) keyed windows."""

    def __init__(self, clk, windows):
        self.clk, self.windows, self.sent = clk, windows, []

    def cmd(self, name, params, timeout=15.0):
        self.sent.append(name)
        if name == "stop_tx":
            self.windows = [w for w in self.windows if w[0] > self.clk.t]
            return 0, {}
        for t0, t1, c, att in self.windows:
            if t0 <= self.clk.t < t1:
                return 0, {"tx_active": True, "tx_center_hz": c, "tx_gain": att}
        return 0, {"tx_active": False, "tx_center_hz": None, "tx_gain": None}


class FakeFx:
    def __init__(self, on_dbm):
        self.on_dbm, self.center, self.shots = on_dbm, None, []

    def setup(self, center_hz, **kw):
        self.center = center_hz

    def peak(self, sweeps=1):
        return self.on_dbm(self.center), self.center

    def marker_at(self, hz):
        return -80.0

    def screenshot(self, path):
        with open(path, "wb") as f:
            f.write(b"png")
        self.shots.append(path)
        return 3

    def resume(self):
        pass


def _args(**kw):
    a = dict(settle=2.0, off_delay=0.5, poll=0.5, timeout=600.0, span=200e3, rbw=10e3, ref=20.0,
             fx_atten=30.0, fx_atten_max=31.0, sweeps=1, stop_over=6.0, extra_loss=0.0, coupler=pb.BENCH_COUPLER, tx_port="TRx2A")
    a.update(kw)
    return argparse.Namespace(**a)


@pytest.fixture
def model():
    data = pb.apply_g40_max(pb.load_profiles(os.path.join(ROOT, "payload_profiles.yaml")), 13.6)
    return data, pb.profile(data, "P2b")


def test_fills_rows_and_off_readings(tmp_path, model):
    data, prof = model
    steps = msr.load_steps(_mission(tmp_path, [("s1", 915, 41.75), ("s2", 915, 35.0)]))
    tpl = _sheet(tmp_path, [(915, 41.75), (915, 35), (2200, 41.75)])
    sheet = msr.Sheet(tpl, str(tmp_path / "filled.xlsx"), "TRx2A")
    clk = FakeClock()
    link = FakeLink(clk, [(10, 30, 915e6, 41.75), (50, 70, 915e6, 35.0)])
    exp = {s["id"]: msr.expected_dbm(data, prof, s, 0.0, pb.BENCH_COUPLER) for s in steps}
    fx = FakeFx(lambda c: exp["s1"] + 0.5)          # 0.5 dB over the model, well inside the limit
    rows = msr.run(_args(), link, fx, sheet, steps, data, prof, str(tmp_path / "out.csv"),
                   str(tmp_path), clock=clk.now, sleep=clk.sleep)
    assert [r["step"] for r in rows] == ["s1", "s2"]
    assert "stop_tx" not in link.sent and set(link.sent) == {"get_config"}
    wb = openpyxl.load_workbook(tmp_path / "filled.xlsx")
    ws = wb["Sweep"]
    assert ws["N5"].value == pytest.approx(exp["s1"] + 0.5, abs=0.01)
    assert ws["N6"].value is not None and ws["N7"].value is None
    assert "HPA off after unkey -80.0 dBm" in ws["Q5"].value and "TRx2A_s1_on.png" in ws["Q5"].value
    assert wb["Inputs"]["B5"].value == "TRx2A"
    # the template is not touched
    assert openpyxl.load_workbook(tpl)["Sweep"]["N5"].value is None
    assert openpyxl.load_workbook(tpl)["Inputs"]["B5"].value == "TRx1A"
    assert len(fx.shots) == 2 and all(os.path.exists(p) for p in fx.shots)
    lines = open(tmp_path / "out.csv").read().splitlines()
    assert len(lines) == 3 and lines[1].split(",")[1] == "s1"


def test_reading_waits_for_settle(tmp_path, model):
    data, prof = model
    steps = msr.load_steps(_mission(tmp_path, [("s1", 915, 41.75)]))
    sheet = msr.Sheet(_sheet(tmp_path, [(915, 41.75)]), str(tmp_path / "f.xlsx"))
    clk = FakeClock()
    taken = []
    fx = FakeFx(lambda c: (taken.append(clk.t), -20.0)[1])
    msr.run(_args(settle=4.0), FakeLink(clk, [(10, 30, 915e6, 41.75)]), fx, sheet, steps, data, prof,
            str(tmp_path / "o.csv"), str(tmp_path), clock=clk.now, sleep=clk.sleep)
    assert taken and taken[0] >= 14.0


def test_unmatched_state_is_ignored(tmp_path, model):
    data, prof = model
    steps = msr.load_steps(_mission(tmp_path, [("s1", 915, 41.75)]))
    sheet = msr.Sheet(_sheet(tmp_path, [(915, 41.75)]), str(tmp_path / "f.xlsx"))
    clk = FakeClock()
    fx = FakeFx(lambda c: -20.0)
    rows = msr.run(_args(timeout=60.0), FakeLink(clk, [(10, 30, 915e6, 30.0)]), fx, sheet, steps, data, prof,
                   str(tmp_path / "o.csv"), str(tmp_path), clock=clk.now, sleep=clk.sleep)
    assert rows == [] and fx.shots == []


def test_over_limit_sends_stop_tx(tmp_path, model):
    data, prof = model
    steps = msr.load_steps(_mission(tmp_path, [("s1", 915, 41.75), ("s2", 915, 35.0)]))
    sheet = msr.Sheet(_sheet(tmp_path, [(915, 41.75), (915, 35)]), str(tmp_path / "f.xlsx"))
    clk = FakeClock()
    link = FakeLink(clk, [(10, 30, 915e6, 41.75), (50, 70, 915e6, 35.0)])
    exp1 = msr.expected_dbm(data, prof, steps[0], 0.0, pb.BENCH_COUPLER)
    fx = FakeFx(lambda c: exp1 + 9.0)
    rows = msr.run(_args(), link, fx, sheet, steps, data, prof, str(tmp_path / "o.csv"), str(tmp_path),
                   clock=clk.now, sleep=clk.sleep)
    assert "stop_tx" in link.sent and [r["step"] for r in rows] == ["s1"]


def test_after_a_stop_later_keys_are_stopped_too(tmp_path, model):
    data, prof = model
    steps = msr.load_steps(_mission(tmp_path, [("s1", 915, 41.75), ("s2", 915, 35.0)]))
    sheet = msr.Sheet(_sheet(tmp_path, [(915, 41.75), (915, 35)]), str(tmp_path / "f.xlsx"))
    clk = FakeClock()
    link = FakeLink(clk, [(10, 30, 915e6, 41.75), (50, 70, 915e6, 35.0)])
    exp1 = msr.expected_dbm(data, prof, steps[0], 0.0, pb.BENCH_COUPLER)
    fx = FakeFx(lambda c: exp1 + 9.0)
    msr.run(_args(), link, fx, sheet, steps, data, prof, str(tmp_path / "o.csv"), str(tmp_path),
            clock=clk.now, sleep=clk.sleep)
    assert link.sent.count("stop_tx") >= 2


def test_reading_far_below_expected_sends_stop_tx(tmp_path, model):
    data, prof = model
    steps = msr.load_steps(_mission(tmp_path, [("s1", 915, 41.75)]))
    sheet = msr.Sheet(_sheet(tmp_path, [(915, 41.75)]), str(tmp_path / "f.xlsx"))
    clk = FakeClock()
    link = FakeLink(clk, [(10, 30, 915e6, 41.75)])
    exp1 = msr.expected_dbm(data, prof, steps[0], 0.0, pb.BENCH_COUPLER)
    fx = FakeFx(lambda c: exp1 - 20.0)
    args = _args()
    args.stop_under = 12.0
    msr.run(args, link, fx, sheet, steps, data, prof, str(tmp_path / "o.csv"), str(tmp_path),
            clock=clk.now, sleep=clk.sleep)
    assert "stop_tx" in link.sent


def test_extra_loss_per_frequency():
    assert msr.extra_loss_db("2.5", 915) == 2.5
    assert msr.extra_loss_db("915:3.83,2200:7.51", 915) == 3.83
    assert msr.extra_loss_db("915:3.83,2200:7.51", 2200) == 7.51


class OverRangeFx(FakeFx):
    """Over range below a given input attenuation, then fine."""

    def __init__(self, on_dbm, ok_from_db):
        super().__init__(on_dbm)
        self.ok_from_db, self.att = ok_from_db, None

    def setup(self, center_hz, atten_db=None, **kw):
        self.center, self.att = center_hz, atten_db

    def peak(self, sweeps=1):
        if self.att < self.ok_from_db:
            raise RuntimeError('INIT:IMM: 210,"ADC Over Range"')
        return super().peak(sweeps)


def test_over_range_raises_input_atten(tmp_path, model):
    data, prof = model
    steps = msr.load_steps(_mission(tmp_path, [("s1", 915, 15.0)]))
    sheet = msr.Sheet(_sheet(tmp_path, [(915, 15)]), str(tmp_path / "f.xlsx"))
    clk = FakeClock()
    exp = msr.expected_dbm(data, prof, steps[0], 0.0, pb.BENCH_COUPLER)
    fx = OverRangeFx(lambda c: exp, ok_from_db=31.0)
    rows = msr.run(_args(), FakeLink(clk, [(10, 30, 915e6, 15.0)]), fx, sheet, steps, data, prof,
                   str(tmp_path / "o.csv"), str(tmp_path), clock=clk.now, sleep=clk.sleep)
    assert rows and rows[0]["fx_atten_db"] == 31.0
    assert "input atten 31 dB" in openpyxl.load_workbook(tmp_path / "f.xlsx")["Sweep"]["Q5"].value


def test_analyzer_error_does_not_end_the_recording(tmp_path, model):
    data, prof = model
    steps = msr.load_steps(_mission(tmp_path, [("s1", 915, 15.0), ("s2", 915, 10.0)]))
    sheet = msr.Sheet(_sheet(tmp_path, [(915, 15), (915, 10)]), str(tmp_path / "f.xlsx"))
    clk = FakeClock()
    exp = msr.expected_dbm(data, prof, steps[1], 0.0, pb.BENCH_COUPLER)
    fx = OverRangeFx(lambda c: exp, ok_from_db=99.0)          # never fine
    calls = {"n": 0}
    real_peak = OverRangeFx.peak

    def peak(self, sweeps=1):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("socket timeout")
        return FakeFx.peak(self, sweeps)
    fx.peak = peak.__get__(fx)
    rows = msr.run(_args(), FakeLink(clk, [(10, 30, 915e6, 15.0), (50, 70, 915e6, 10.0)]), fx, sheet,
                   steps, data, prof, str(tmp_path / "o.csv"), str(tmp_path), clock=clk.now, sleep=clk.sleep)
    ws = openpyxl.load_workbook(tmp_path / "f.xlsx")["Sweep"]
    assert "analyzer error" in ws["Q5"].value and ws["N5"].value is None
    assert [r["step"] for r in rows] == ["s2"] and ws["N6"].value is not None
    assert real_peak is OverRangeFx.peak
