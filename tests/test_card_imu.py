"""test_card_imu.py: CARD_IMU: tilt, shock and motion from the NV100 accelerometer.

Covers: the level reference (first steady reading, then "set level"), tilt for a
known rotation, shock counting and the red hold time, the motion RMS window,
reset when the radio stops, the operator-card text and colour, the libsidekiq
bridge read (support check, enable, milli-g scaling, failure: None), the Epiq
backend, the simulator, and the service's set-level command.
"""
import math
import os
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from dpmg.radio import card_imu as ci  # noqa: E402


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _rot(deg):
    r = math.radians(deg)
    return (math.sin(r), 0.0, math.cos(r))     # 1 g tipped by deg about y


def test_reference_and_tilt():
    tr = ci.ImuTracker(clock=_Clock())
    assert tr.summary() is None
    for _ in range(ci.REF_SAMPLES - 1):
        tr.add((0.0, 0.0, 1.0))
    assert tr.summary()["tilt_deg"] is None            # still settling
    tr.add((0.0, 0.0, 1.0))
    assert tr.summary()["tilt_deg"] == 0.0
    tr.add(_rot(10))
    assert tr.summary()["tilt_deg"] == pytest.approx(10.0, abs=0.1)
    tr.set_level()
    tr.add(_rot(10))
    assert tr.summary()["tilt_deg"] == pytest.approx(0.0, abs=0.1)


def test_unsteady_first_sample_is_not_the_reference():
    tr = ci.ImuTracker(clock=_Clock())
    tr.add((0.0, 0.0, 1.6))                      # a bump while starting
    for _ in range(ci.REF_SAMPLES):
        tr.add((0.0, 0.0, 1.0))
    assert tr.summary()["tilt_deg"] == 0.0


def test_shock_count_and_hold():
    clk = _Clock()
    tr = ci.ImuTracker(clock=clk)
    tr.add((0.0, 0.0, 1.0))
    tr.add((0.0, 0.0, 1.0 + ci.SHOCK_G + 0.3))
    s = tr.summary()
    assert s["shocks"] == 1 and s["last_shock"]["g"] == pytest.approx(1.8, abs=0.01)
    txt, lvl = ci.card_text(s)
    assert "SHOCK" in txt and lvl == "hot"
    clk.t += ci.SHOCK_HOLD_S + 1
    tr.add((0.0, 0.0, 1.0))
    txt, lvl = ci.card_text(tr.summary())
    assert "SHOCK" not in txt and "1 shock(s)" in txt and lvl != "hot"


def test_motion_window():
    clk = _Clock()
    tr = ci.ImuTracker(clock=clk)
    for i in range(20):
        clk.t += ci.SAMPLE_S
        tr.add((0.0, 0.0, 1.0 + (0.1 if i % 2 else -0.1)))
    assert tr.summary()["motion"] is True
    for _ in range(int(ci.MOTION_WINDOW_S / ci.SAMPLE_S) + 4):
        clk.t += ci.SAMPLE_S
        tr.add((0.0, 0.0, 1.0))
    assert tr.summary()["motion"] is False


def test_reference_averages_startup_readings():
    tr = ci.ImuTracker(clock=_Clock())
    tr.add(_rot(6))                              # first reading off while the sensor settles
    for _ in range(ci.REF_SAMPLES - 1):
        tr.add((0.0, 0.0, 1.0))
    tr.add((0.0, 0.0, 1.0))
    assert tr.summary()["tilt_deg"] < 1.0        # averaged reference, not the first sample


def test_steady_offset_is_not_motion():
    clk = _Clock()
    tr = ci.ImuTracker(clock=clk)
    for _ in range(40):                          # the G40 sensor: 0.93 g at rest
        clk.t += ci.SAMPLE_S
        tr.add((-0.013, 0.026, 0.932))
    s = tr.summary()
    assert s["motion"] is False and s["tilt_deg"] < 0.5 and s["shocks"] == 0
    tr.add((0.0, 0.0, 0.932 + ci.SHOCK_G + 0.1))  # shock measured from the resting level
    assert tr.summary()["shocks"] == 1


def test_reset_keeps_shock_log():
    tr = ci.ImuTracker(clock=_Clock())
    tr.add((0.0, 0.0, 1.0))
    tr.add((0.0, 0.0, 2.0))
    tr.reset()
    assert tr.summary() is None and tr.shocks == 1
    assert ci.card_text(None) == ("motion: radio not started", None)


def test_card_text_levels():
    assert ci.card_text({"tilt_deg": 1.0, "motion": False, "shocks": 0})[1] == "ok"
    assert ci.card_text({"tilt_deg": 1.0, "motion": True, "shocks": 0})[1] == "warm"
    assert ci.card_text({"tilt_deg": ci.TILT_WARN_DEG, "motion": False, "shocks": 0})[1] == "warm"
    assert ci.card_text({"tilt_deg": ci.TILT_ALARM_DEG, "motion": False, "shocks": 0})[1] == "hot"
    assert "settling" in ci.card_text({"tilt_deg": None, "motion": False, "shocks": 0})[0]


class _Fn:
    def __init__(self, impl):
        self.impl = impl

    def __call__(self, *a):
        return self.impl(*a)


class _Lib:
    pass


def test_bridge_read_accel():
    from dpmg.radio import skiq_bridge
    br = skiq_bridge.SkiqBridge.__new__(skiq_bridge.SkiqBridge)
    br.lib = _Lib()
    assert br.read_accel(0) is None                          # no symbol
    calls = []

    def supported(card, p):
        p._obj.value = True
        return 0

    def state(card, p):
        p._obj.value = 0
        return 0

    def write(card, en):
        calls.append(("enable", card, en))
        return 0

    def read(card, px, py, pz):
        px._obj.value, py._obj.value, pz._obj.value = 12, -5, 998
        return 0
    br.lib.skiq_is_accel_supported = _Fn(supported)
    br.lib.skiq_read_accel_state = _Fn(state)
    br.lib.skiq_write_accel_state = _Fn(write)
    br.lib.skiq_read_accel = _Fn(read)
    assert br.read_accel(0) == pytest.approx((0.012, -0.005, 0.998))
    assert calls == [("enable", 0, 1)]
    br.read_accel(0)
    assert calls == [("enable", 0, 1)]                       # enabled once
    br.lib.skiq_read_accel = _Fn(lambda *a: -11)             # -EAGAIN
    assert br.read_accel(0) is None


def test_bridge_unsupported_card():
    from dpmg.radio import skiq_bridge
    br = skiq_bridge.SkiqBridge.__new__(skiq_bridge.SkiqBridge)
    br.lib = _Lib()

    def unsupported(card, p):
        p._obj.value = False
        return 0
    br.lib.skiq_is_accel_supported = _Fn(unsupported)
    br.lib.skiq_read_accel = _Fn(lambda *a: 0)
    assert br.read_accel(1) is None


def test_backends():
    from dpmg.radio import sdr_core
    assert sdr_core.SDRBackend.accel(object()) is None
    eb = sdr_core.EpiqBackend.__new__(sdr_core.EpiqBackend)
    assert eb.accel() is None

    class _Sk:
        def read_accel(self, c):
            return (0.0, 0.0, 1.0) if c == 0 else None
    eb._skiq, eb._card = _Sk(), 0
    assert eb.accel() == (0.0, 0.0, 1.0)
    mb = sdr_core.MockBackend.__new__(sdr_core.MockBackend)
    x, y, z = mb.accel()
    assert abs(ci.magnitude((x, y, z)) - 1.0) < 0.05


def test_service_set_level():
    from dpmg.radio import dpmg_service
    from dpmg.link.proto import CmdStatus
    svc = dpmg_service.RadioService.__new__(dpmg_service.RadioService)
    assert svc._cmd_imu_set_level(1, {}).status == CmdStatus.REJECTED
    tr = svc._imu_tracker()
    for _ in range(ci.REF_SAMPLES):
        tr.add((0.0, 0.0, 1.0))
    tr.add(_rot(20))
    ack = svc._cmd_imu_set_level(2, {})
    assert ack.status == CmdStatus.OK and ack.result["imu"]["tilt_deg"] == pytest.approx(0.0, abs=0.1)


if __name__ == "__main__":  # OFF-01 loop runs `python3 tests/<file>.py`; delegate to pytest
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))


def test_radio_without_imu_says_so():
    # B200/HackRF/LimeSDR have no accelerometer: "no IMU", not "radio not started"
    for k in ("b200", "hackrf", "limesdr", "pluto"):
        assert ci.card_text(None, k) == ("motion: no IMU", None)
    for k in ("g40", "x40", "mock", None):
        assert ci.card_text(None, k) == ("motion: radio not started", None)
    assert ci.has_imu(None) is None and ci.has_imu("G40") is True
