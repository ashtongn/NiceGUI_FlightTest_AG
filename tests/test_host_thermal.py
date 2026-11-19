"""test_host_thermal.py: the radio's junction temperature in the
status heartbeat (host_thermal.py, dpmg_service._temp_x10, the operator card).

Covers: reading a fake /sys/class/thermal tree, junction preferred over the
other zones, fallback to the hottest zone, unreadable zones skipped, the wire
encoding through proto.Status pack/unpack, the "no reading" values, and the
warm / hot thresholds the operator console colours by.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from dpmg.radio import host_thermal as ht  # noqa: E402
from dpmg.link import proto  # noqa: E402


def _zone(root, i, name, milli):
    d = root / f"thermal_zone{i}"
    d.mkdir()
    (d / "type").write_text(name + "\n")
    (d / "temp").write_text(f"{milli}\n")


def test_reads_zones_and_prefers_junction(tmp_path):
    _zone(tmp_path, 0, "cpu-thermal", 65531)
    _zone(tmp_path, 1, "gpu-thermal", 71000)
    _zone(tmp_path, 2, "tj-thermal", 66000)
    z = ht.read_zones(str(tmp_path))
    assert z == {"cpu-thermal": 65.531, "gpu-thermal": 71.0, "tj-thermal": 66.0}
    assert ht.junction_c(z) == 66.0


def test_falls_back_to_hottest_zone(tmp_path):
    _zone(tmp_path, 0, "cpu-thermal", 52000)
    _zone(tmp_path, 1, "soc0-thermal", 58500)
    assert ht.junction_c(ht.read_zones(str(tmp_path))) == 58.5


def test_unreadable_zone_skipped_and_empty_tree(tmp_path):
    _zone(tmp_path, 0, "cpu-thermal", 50000)
    bad = tmp_path / "thermal_zone1"
    bad.mkdir()
    (bad / "type").write_text("broken\n")
    (bad / "temp").write_text("not-a-number\n")
    assert ht.read_zones(str(tmp_path)) == {"cpu-thermal": 50.0}
    assert ht.junction_c(ht.read_zones(str(tmp_path / "missing"))) is None


def test_eagain_zone_skipped(tmp_path, monkeypatch):
    # Orin Nano cv0-2 zones: the read gives None and Python raises TypeError
    _zone(tmp_path, 0, "cv0-thermal", 1)
    _zone(tmp_path, 1, "tj-thermal", 37468)
    real_open = open

    class _Eagain:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            raise TypeError("can't concat NoneType to bytes")

    def fake_open(path, *a, **k):
        if path.endswith(os.path.join("thermal_zone0", "temp")):
            return _Eagain()
        return real_open(path, *a, **k)

    monkeypatch.setattr(ht, "open", fake_open, raising=False)
    z = ht.read_zones(str(tmp_path))
    assert z == {"tj-thermal": 37.468}
    assert ht.junction_c(z) == 37.468


@pytest.mark.parametrize("c", [66.0, 78.8, 104.0, -5.5])
def test_wire_round_trip(c):
    st = proto.Status(serial="G40-TEST", host_type=1, radio_state=0, temp_c_x10=ht.to_wire(c))
    back = proto.Status.unpack(st.pack())
    assert ht.from_wire(back.temp_c_x10) == pytest.approx(c, abs=0.05)


def test_no_reading_values():
    assert ht.to_wire(None) == ht.NO_READING
    assert ht.from_wire(ht.NO_READING) is None
    assert ht.from_wire(0) is None          # an older service sends 0
    st = proto.Status(serial="G40-TEST", host_type=1, radio_state=0, temp_c_x10=ht.NO_READING)
    assert ht.from_wire(proto.Status.unpack(st.pack()).temp_c_x10) is None


def test_levels():
    assert ht.level(None) is None
    assert ht.level(66.0) == "ok"
    assert ht.level(ht.WARN_C) == "warm"
    assert ht.level(94.9) == "warm"
    assert ht.level(ht.HOT_C) == "hot"


def test_service_fills_heartbeat(monkeypatch):
    from dpmg.radio import dpmg_service
    monkeypatch.setattr(ht, "read_zones", lambda root=ht.THERMAL_ROOT: {"tj-thermal": 66.0})
    svc = dpmg_service.RadioService.__new__(dpmg_service.RadioService)
    assert ht.from_wire(svc._temp_x10()) == 66.0
    monkeypatch.setattr(ht, "read_zones", lambda root=ht.THERMAL_ROOT: {"tj-thermal": 80.0})
    assert ht.from_wire(svc._temp_x10()) == 66.0      # cached for 5 s
    svc._tj_t = -1e9
    assert ht.from_wire(svc._temp_x10()) == 80.0


# NV100 card temperatures

def test_card_levels_and_worst():
    assert ht.card_level(None) is None
    assert ht.card_level(45) == "ok"
    assert ht.card_level(ht.CARD_WARN_C) == "warm"
    assert ht.card_level(ht.CARD_HOT_C) == "hot"
    assert ht.worst(["ok", "warm"]) == "warm"
    assert ht.worst(["ok", "hot", "warm"]) == "hot"
    assert ht.worst([None]) is None


def test_card_temps_from_config():
    assert ht.card_temps_from_config(None) == []
    assert ht.card_temps_from_config({"card_temps": {}}) == []
    assert ht.card_temps_from_config({"card_temps": {"1": 47, "0": 45, "x": "bad"}}) == [(0, 45.0), (1, 47.0)]


def test_card_label():
    assert ht.card_label(None, 0) == "card A"
    assert ht.card_label({"card_names": {}}, 1) == "card B"
    assert ht.card_label({"card_names": {"0": "B200mini"}}, 0) == "B200mini"


def test_card_last_and_age_text():
    assert ht.card_last_from_config(None) == []
    cfg = {"card_temps_last": {"0": [50, 240], "x": "bad"}, "card_names": {"0": "B200mini"}}
    assert ht.card_last_from_config(cfg) == [(0, 50.0, 240)]
    assert [ht.age_text(s) for s in (12, 240, 7200, 3 * 86400)] == ["12 s", "4 min", "2 h", "3 days"]
    assert ht.last_card_text(cfg) == "B200mini 50 °C, 4 min ago (radio closed)"
    assert ht.last_card_text({}) == "card temp: radio not started"


def test_service_keeps_last_reading_after_close():
    import time as _t
    from dpmg.radio import dpmg_service
    svc = dpmg_service.RadioService.__new__(dpmg_service.RadioService)
    svc.backend = None
    assert svc._card_fields() == {"card_temps": {}, "card_temps_last": {}, "card_names": {}}
    svc._card_temps = {}                                   # radio closed: no live reading
    svc._card_last = {0: (50, _t.time() - 240)}
    svc._card_names_last = {"0": "B200mini"}
    f = svc._card_fields()
    assert f["card_temps"] == {} and f["card_names"] == {"0": "B200mini"}
    assert f["card_temps_last"]["0"][0] == 50 and 239 <= f["card_temps_last"]["0"][1] <= 242
    svc._card_temps = {0: (51, _t.monotonic())}           # live again: no "last" entry
    f = svc._card_fields()
    assert f["card_temps"] == {"0": 51} and f["card_temps_last"] == {}


def test_uhd_card_temp_and_name():
    from dpmg.radio import sdr_core

    class _Sensor:
        def to_real(self):
            return 44.6

    class _Usrp:
        def get_rx_sensor(self, name, ch):
            assert name == "temp"
            return _Sensor()

        def get_mboard_name(self):
            return "B200mini"

    ub = sdr_core.UhdBackend.__new__(sdr_core.UhdBackend)
    ub.usrp = None
    assert ub.card_temps() == {}                        # not open
    ub.usrp = _Usrp()
    assert ub.card_temps() == {0: 45}
    assert ub.card_names == {"0": "B200mini"}


class _FakeFn:
    def __init__(self, rc, value):
        self.rc, self.value = rc, value

    def __call__(self, card, ptr):
        ptr._obj.value = self.value
        return self.rc


class _FakeLib:
    pass


def test_bridge_read_temp():
    from dpmg.radio import skiq_bridge
    br = skiq_bridge.SkiqBridge.__new__(skiq_bridge.SkiqBridge)
    br.lib = _FakeLib()
    assert br.read_temp(0) is None                      # symbol missing
    br.lib.skiq_read_temp = _FakeFn(0, 52)
    assert br.read_temp(0) == 52
    br.lib.skiq_read_temp = _FakeFn(-11, 0)             # -EAGAIN (FPGA GPSDO owns the sensor)
    assert br.read_temp(0) is None


def test_backend_card_temps():
    from dpmg.radio import sdr_core
    assert sdr_core.SDRBackend.card_temps(object()) == {}
    eb = sdr_core.EpiqBackend.__new__(sdr_core.EpiqBackend)
    assert eb.card_temps() == {}                        # not initialised

    class _Sk:
        def read_temp(self, c):
            return {0: 44, 1: None}[c]
    eb._skiq, eb._card, eb._tx_card = _Sk(), 0, 1
    assert eb.card_temps() == {0: 44}                   # card 1 gave no reading
    eb._tx_card = 0
    assert eb.card_temps() == {0: 44}                   # same card read once


if __name__ == "__main__":  # OFF-01 loop runs `python3 tests/<file>.py`; delegate to pytest
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
