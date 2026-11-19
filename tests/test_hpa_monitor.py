"""HPA_MONITOR: Empower 1189 temperature (DB9 pin 3) and supply current (DB9 pin 2)
through the ADS1115 on the G40's I2C-1 bus, next to the switch readback channel.

Pure maths for both conversions, the status path over the mock transport, and a
missing chip (or a missing smbus2) reporting "not fitted" without refusing a key.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from dpmg.payload import rf_frontend as rf
from dpmg.web import payload_tab as pt

from tests.test_rf_frontend import FakeClock


def hm_cfg(transport="mock", tr_readback=False, frontend="mock", **over):
    d = {"frontend": frontend,
         "pa_enable": {"line": "cpu-gp-out1", "fallback_line": "PG.06", "settle_ms": 0, "hang_ms": 0},
         "tr_switch": {"mode": "gpio", "line": "cpu-gp-out2", "fallback_line": "PN.01",
                       "tx_level": 1, "settle_ms": 0},
         "hpa_monitor": {"enabled": True, "transport": transport, "poll_s": 2.0, **over},
         "watchdog_ms": 0}
    if tr_readback:
        d["tr_readback"] = {"enabled": True, "transport": transport, "settle_ms": 0, "channel": 0}
    return rf.config_from_dict(d, source="test")


def mk(cfg):
    clock = FakeClock()
    fe = rf.MockFrontend(cfg, clock=clock.now, sleep=clock.sleep)
    fe.open()
    return fe, clock


# pure maths
class TestConversions:
    def test_temperature_datasheet_points(self):
        # 10 mV per degree C on a 0.5 V offset
        assert rf.hpa_temp_c_from_volts(0.5) == pytest.approx(0.0)
        assert rf.hpa_temp_c_from_volts(1.0) == pytest.approx(50.0)
        assert rf.hpa_temp_c_from_volts(1.35) == pytest.approx(85.0)
        assert rf.hpa_temp_c_from_volts(0.25) == pytest.approx(-25.0)

    def test_current_datasheet_points(self):
        # 25 mV per 100 mA: 0.3 A in shutdown, 1.5 A quiescent when enabled
        assert rf.hpa_current_a_from_volts(0.075) == pytest.approx(0.3)
        assert rf.hpa_current_a_from_volts(0.375) == pytest.approx(1.5)
        assert rf.hpa_current_a_from_volts(0.0) == pytest.approx(0.0)
        assert rf.hpa_current_a_from_volts(2.5) == pytest.approx(10.0)

    def test_custom_scales(self):
        assert rf.hpa_temp_c_from_volts(2.0, offset_v=1.0, v_per_c=0.02) == pytest.approx(50.0)
        assert rf.hpa_current_a_from_volts(1.1, v_per_a=0.5, offset_v=0.1) == pytest.approx(2.0)


# config
class TestConfig:
    def test_defaults_off_and_datasheet_scales(self):
        cfg = rf.FrontendConfig()
        h = cfg.hpa_monitor
        assert h.enabled is False
        assert (h.bus, h.address, h.temp_channel, h.current_channel) == ("/dev/i2c-1", 0x48, 1, 2)
        assert (h.temp_offset_v, h.temp_v_per_c) == (0.5, 0.01)
        assert (h.current_v_per_a, h.current_offset_v) == (0.25, 0.0)
        assert h.full_scale_v == 4.096 and h.warn_c == 70.0 and h.poll_s == 2.0

    def test_parses_and_round_trips(self):
        cfg = hm_cfg(address="0x49", temp_channel=3, current_channel=0, warn_c=65, poll_s=1)
        h = cfg.hpa_monitor
        assert h.enabled and h.transport == "mock" and h.address == 0x49
        assert (h.temp_channel, h.current_channel, h.warn_c, h.poll_s) == (3, 0, 65.0, 1.0)
        assert cfg.to_dict()["hpa_monitor"]["temp_channel"] == 3

    def test_rejects_bad_keys_and_channel_clashes(self):
        with pytest.raises(rf.ConfigError):
            rf.config_from_dict({"frontend": "mock", "hpa_monitor": {"nope": 1}})
        with pytest.raises(rf.ConfigError):
            hm_cfg(temp_channel=2, current_channel=2)
        with pytest.raises(rf.ConfigError):            # same chip, same channel as the switch readback
            hm_cfg(transport="i2c", tr_readback=True, temp_channel=0)
        hm_cfg(transport="i2c", tr_readback=True)      # default channels 1 and 2 are fine

    def test_disabled_has_no_effect(self):
        d = hm_cfg().to_dict()
        d.pop("source")
        d["hpa_monitor"]["enabled"] = False
        fe, _ = mk(rf.config_from_dict(d, source="test"))
        assert fe.hpa_monitor_state() is None
        assert "hpa_monitor" not in fe.status()
        fe.sequence_tx_on()
        assert fe.pa_level is True


# mock transport status path
class TestMockStatus:
    def test_status_carries_temperature_and_current(self):
        fe, _ = mk(hm_cfg())
        fe.set_hpa(temp_c=41.2, current_a=1.5)
        hm = fe.status()["hpa_monitor"]
        assert hm["state"] == rf.HPA_MON_OK and hm["enabled"] is True
        assert hm["hpa_temp_c"] == pytest.approx(41.2) and hm["hpa_current_a"] == pytest.approx(1.5)
        assert hm["hpa_temp_v"] == pytest.approx(0.912) and hm["hpa_current_v"] == pytest.approx(0.375)
        assert hm["hot"] is False and hm["warn_c"] == 70.0 and hm["reason"] is None

    def test_hot_above_warn(self):
        fe, _ = mk(hm_cfg(warn_c=70, valid_when_off=True))
        fe.set_hpa(temp_c=69.9)
        assert fe.status()["hpa_monitor"]["hot"] is False
        fe.set_hpa(temp_c=70.0)
        assert fe.status()["hpa_monitor"]["hot"] is True
        fe.set_hpa(temp_c=72.5)
        hm = fe.status()["hpa_monitor"]
        assert hm["hot"] is True and hm["hpa_temp_c"] == 72.5

    def test_poll_period_caches_between_reads(self):
        fe, clock = mk(hm_cfg(poll_s=2.0))
        first = fe.hpa_monitor_state()
        assert first["age_s"] == 0.0
        fe._hpa_adcs[0].node_v = 1.35                  # chip now says 85 C, cache not expired
        clock.sleep(0.5)
        again = fe.hpa_monitor_state()
        assert again["hpa_temp_c"] == first["hpa_temp_c"] and again["age_s"] == pytest.approx(0.5)
        clock.sleep(2.0)
        fresh = fe.hpa_monitor_state()
        assert fresh["hpa_temp_c"] == pytest.approx(85.0) and fresh["age_s"] == 0.0
        assert fe.hpa_monitor_state(force=True)["age_s"] == 0.0

    def test_sim_current_follows_the_key(self):
        fe, _ = mk(hm_cfg())
        assert fe.hpa_monitor_state()["hpa_current_a"] == pytest.approx(0.3)     # shutdown draw
        fe.sequence_tx_on()
        assert fe.hpa_monitor_state()["hpa_current_a"] == pytest.approx(1.5)     # quiescent
        fe.sequence_tx_off()
        assert fe.hpa_monitor_state()["hpa_current_a"] == pytest.approx(0.3)

    def test_shares_the_chip_with_the_switch_readback(self):
        fe, _ = mk(hm_cfg(tr_readback=True))
        fe.set_tr_volts(3.3)
        fe.set_hpa(temp_c=30.0, current_a=0.3)
        fe.sequence_tx_on()
        st = fe.status()
        assert st["tr_readback"]["state"] == rf.TR_TX
        assert st["hpa_monitor"]["hpa_temp_c"] == 30.0 and st["pa"]["keyed"] is True

    def test_reload_keeps_reading(self):
        fe, _ = mk(hm_cfg())
        fe.set_hpa(temp_c=50.0)
        fe.reload(hm_cfg())
        assert fe.status()["hpa_monitor"]["state"] == rf.HPA_MON_OK
        fe.close()
        assert fe.hpa_monitor_state() is None


# missing chip: not fitted, nothing refused
class _NoChipBus:
    """i2c-dev bus with nothing at the address: every transfer gets no ACK."""
    def __init__(self, n):
        self.n = n

    def write_i2c_block_data(self, addr, reg, data):
        raise OSError(121, "Remote I/O error")

    def read_i2c_block_data(self, addr, reg, n):
        raise OSError(121, "Remote I/O error")

    def close(self):
        pass


class _ChipBus:
    """A chip that answers: channel 1 reads 0.912 V (41.2 C), channel 2 reads
    0.375 V (1.5 A), as 16-bit codes at the 4.096 V range."""
    opened = 0

    def __init__(self, n):
        _ChipBus.opened += 1
        self._mux = None

    def write_i2c_block_data(self, addr, reg, data):
        assert reg == 0x01
        self._mux = (data[0] << 8 | data[1]) & 0x7000

    def read_i2c_block_data(self, addr, reg, n):
        assert reg == 0x00 and n == 2
        v = {0x5000: 0.912, 0x6000: 0.375}[self._mux]
        code = int(round(v / (4.096 / 32768.0)))
        return [(code >> 8) & 0xFF, code & 0xFF]

    def close(self):
        pass


def i2c_frontend(monkeypatch, smbus_cls):
    """A gpio-kind front end over the in-memory GPIO with the i2c transport and
    the given fake SMBus class (None = the module is missing)."""
    monkeypatch.setattr(rf, "_import_smbus", lambda: smbus_cls)
    monkeypatch.setattr(rf.time, "sleep", lambda s: None)
    cfg = hm_cfg(transport="i2c", frontend="mock")
    clock = FakeClock()
    fe = rf.MockFrontend(cfg, clock=clock.now, sleep=clock.sleep)
    # the mock kind would swap in MemoryAdcs; build the i2c readers as the gpio kind does
    fe.open()
    fe._hpa_adcs = rf.make_hpa_adcs(cfg.hpa_monitor, mock=False)
    fe._hpa_cache = None
    return fe


class TestMissingChip:
    def test_no_chip_reports_not_fitted_and_keys_anyway(self, monkeypatch):
        fe = i2c_frontend(monkeypatch, _NoChipBus)
        assert isinstance(fe._hpa_adcs[0], rf.I2cAds1x15Reader)
        hm = fe.status()["hpa_monitor"]
        assert hm["state"] == rf.HPA_MON_NOT_FITTED and hm["reason"] == "no ADC at that address"
        assert hm["hpa_temp_c"] is None and hm["hpa_current_a"] is None and hm["hot"] is False
        assert fe.last_error is None                    # a missing monitor is not a fault
        fe.sequence_tx_on()                             # nothing refused
        assert fe.pa_level is True and fe.status()["pa"]["keyed"] is True
        fe.sequence_tx_off()

    def test_smbus2_missing_reports_not_fitted(self, monkeypatch):
        fe = i2c_frontend(monkeypatch, None)
        hm = fe.status()["hpa_monitor"]
        assert hm["state"] == rf.HPA_MON_NOT_FITTED and hm["reason"] == "smbus2 missing"
        fe.sequence_tx_on()
        assert fe.pa_level is True

    def test_bus_node_missing(self, monkeypatch):
        class NoBus:
            def __init__(self, n):
                raise FileNotFoundError(f"/dev/i2c-{n}")
        fe = i2c_frontend(monkeypatch, NoBus)
        assert fe.status()["hpa_monitor"]["reason"] == "bus missing"

    def test_chip_present_reads_through_the_i2c_driver(self, monkeypatch):
        fe = i2c_frontend(monkeypatch, _ChipBus)
        hm = fe.status()["hpa_monitor"]
        assert hm["state"] == rf.HPA_MON_OK
        assert hm["hpa_temp_c"] == pytest.approx(41.2, abs=0.05)
        assert hm["hpa_current_a"] == pytest.approx(1.5, abs=0.01)
        assert fe._hpa_adcs[0].info()["channel"] == 1 and fe._hpa_adcs[1].info()["channel"] == 2

    def test_pga_range_follows_the_config(self):
        # the tightest ADS1x15 range that still covers full_scale_v (6.144 V for a
        # 4.096 V request would lose a bit of resolution)
        r = rf.I2cAds1x15Reader(full_scale_v=4.096)
        assert r.info()["full_scale_v"] == 4.096 and r._pga_bits == 0x0200
        assert rf.I2cAds1x15Reader(full_scale_v=2.5).info()["full_scale_v"] == 4.096
        assert rf.I2cAds1x15Reader(full_scale_v=5.0).info()["full_scale_v"] == 6.144
        assert rf.I2cAds1x15Reader(full_scale_v=0.1).info()["full_scale_v"] == 0.256
        assert rf.I2cAds1x15Reader(full_scale_v=10.0).info()["full_scale_v"] == 6.144

    def test_fail_reasons(self):
        assert rf.hpa_monitor_fail_reason(rf.FrontendError("x (smbus2 missing)")) == "smbus2 missing"
        assert rf.hpa_monitor_fail_reason(FileNotFoundError("/dev/i2c-1")) == "bus missing"
        assert rf.hpa_monitor_fail_reason(OSError(6, "No such device")) == "no ADC at that address"
        assert rf.hpa_monitor_fail_reason(ValueError("odd")).startswith("error: odd")


# Payload tab (pure helpers)
def _st(hm=None, hpa_adc="present"):
    st = {"configured": True, "frontend": "gpio", "sim": False, "pa": {"keyed": False, "level": 0},
          "tr": {"tx": False, "mode": "gpio"}, "fault": {"enabled": False}, "atten": None,
          "devices": {"atten": {"state": "off", "mode": "none"},
                      "tr_adc": {"state": "present", "bus": "/dev/i2c-1", "address": "0x48"},
                      "hpa_adc": {"state": hpa_adc, "bus": "/dev/i2c-1", "address": "0x48",
                                  "channels": [1, 2]}}}
    if hm is not None:
        st["hpa_monitor"] = hm
    return st


OK_HM = {"enabled": True, "state": "ok", "hpa_temp_c": 41.2, "hpa_current_a": 1.5,
         "hpa_temp_v": 0.912, "hpa_current_v": 0.375, "warn_c": 70.0, "hot": False,
         "reason": None, "age_s": 0.4}
HOT_HM = dict(OK_HM, hpa_temp_c=74.0, hpa_temp_v=1.24, hot=True)
NF_HM = {"enabled": True, "state": "not fitted", "hpa_temp_c": None, "hpa_current_a": None,
         "hpa_temp_v": None, "hpa_current_v": None, "warn_c": 70.0, "hot": False,
         "reason": "smbus2 missing", "age_s": 0.0}


class TestTab:
    def test_text(self):
        assert pt.hpa_monitor_text({}) == ("", False)
        assert pt.hpa_monitor_text(_st(OK_HM)) == ("41.2 C  1.50 A", False)
        assert pt.hpa_monitor_text(_st(HOT_HM)) == ("74.0 C  1.50 A", True)
        assert pt.hpa_monitor_text(_st(NF_HM, "not fitted")) == ("temp/current not fitted (smbus2 missing)", False)

    def test_device_chip(self):
        chips = {c["key"]: c for c in pt.device_chips(_st(OK_HM))}
        h = chips["hpa_adc"]
        assert h["title"] == "HPA monitor" and h["state"] == "present" and h["tone"] == "ok"
        assert h["extra"] == "41.2 C  1.50 A" and "DB9 pin 3" in h["tip"] and "0.912 V" in h["tip"]
        hot = {c["key"]: c for c in pt.device_chips(_st(HOT_HM))}["hpa_adc"]
        assert hot["tone"] == "warn" and hot["state"] == "present"
        nf = {c["key"]: c for c in pt.device_chips(_st(NF_HM, "not fitted"))}["hpa_adc"]
        assert nf["state"] == "not fitted" and nf["tone"] == "off" and nf["extra"] == ""
        assert "smbus2 missing" in nf["tip"]
        off = _st(); off["devices"]["hpa_adc"] = {"state": "off"}
        assert {c["key"]: c for c in pt.device_chips(off)}["hpa_adc"]["tone"] == "off"
        assert [c["key"] for c in pt.device_chips({"devices": {"atten": {"state": "off"}}})] == ["atten"]

    def test_changes_do_not_fire_on_a_new_reading(self):
        a = _st(OK_HM)
        b = _st(dict(OK_HM, hpa_temp_c=42.0))
        assert pt.device_changes(a, b) == []
        gone = _st(NF_HM, "not fitted")
        msgs = pt.device_changes(a, gone)
        assert msgs and msgs[0][0].startswith("HPA monitor not fitted")

    def test_diagram_shows_readings_and_hot(self):
        svg = pt.diagram_svg(_st(OK_HM), None, "P2")
        assert "41.2 C  1.50 A" in svg and 'height="104"' in svg
        hot = pt.diagram_svg(_st(HOT_HM), None, "P2")
        assert "HOT 74.0 C" in hot and 'fill="#fef3c7"' in hot          # amber box
        nf = pt.diagram_svg(_st(NF_HM, "not fitted"), None, "P2")
        assert "temp/current not fitted" in nf
        plain = pt.diagram_svg(_st(), None, "P2")
        assert 'height="90"' in plain and " C  " not in plain


# radio cards
class TestCardText:
    OK = {"state": "ok", "hpa_temp_c": 41.2, "hpa_current_a": 1.5, "warn_c": 70.0, "hot": False}

    def test_not_configured_is_blank(self):
        assert rf.hpa_card_text(None) == ("", None)
        assert rf.hpa_card_text({}) == ("", None)
        assert rf.hpa_card_text(None, "g40")[0].startswith("HPA temp/current: not set up")
        assert rf.hpa_card_text(None, "b200") == ("", None)

    def test_not_fitted_shows_the_reason(self):
        hm = {"state": "not fitted", "hpa_temp_c": None, "reason": "no ADC at that address"}
        assert rf.hpa_card_text(hm) == ("HPA temp/current: not fitted (no ADC at that address)", None)

    def test_levels(self):
        assert rf.hpa_card_text(self.OK) == ("HPA 41.2 °C, 1.50 A", "ok")
        assert rf.hpa_card_text(dict(self.OK, hpa_temp_c=72.0, hot=True))[1] == "warm"
        assert rf.hpa_card_text(dict(self.OK, hpa_current_a=12.5))[1] == "warm"
        assert rf.hpa_card_text(dict(self.OK, hpa_temp_c=85.0, hot=True))[1] == "hot"

    def test_mock_frontend_reading_goes_to_the_card(self):
        fe, _ = mk(hm_cfg(valid_when_off=True))
        txt, lvl = rf.hpa_card_text(fe.hpa_monitor_state())
        assert txt == "HPA 35.0 °C, 0.30 A" and lvl == "ok"


# can it be believed
class TestReadingQuality:
    def test_rules(self):
        q = rf.hpa_reading_quality
        assert q(0.75, 0.375, 25.0, 1.5, True) == rf.HPA_READING_OK
        assert q(0.75, 0.375, 25.0, 1.5, False) == rf.HPA_READING_OFF
        assert q(2.25, 2.26, 175.0, 9.0, True) == rf.HPA_READING_FLOATING        # the DB9 unplugged
        assert q(1.48, 0.41, 98.0, 1.64, True) == rf.HPA_READING_OK              # possible while keyed
        assert q(1.8, 0.5, 130.0, 2.0, True) == rf.HPA_READING_BAD
        assert q(0.1, 0.5, -40.0, 16.0, True) == rf.HPA_READING_BAD
        assert q(0.75, -0.1, 25.0, -0.4, True) == rf.HPA_READING_BAD

    def test_drop_is_shown_while_tx_runs(self):
        cfg = {"tx_active": True, "fe": {"kind": "gpio", "pa": False, "drop": "watchdog"}}
        assert rf.hpa_drop_text(cfg) == ("HPA OFF, TX still running: dropped by watchdog", "hot")
        assert rf.hpa_drop_text(dict(cfg, tx_active=False)) is None
        assert rf.hpa_drop_text({"tx_active": True, "fe": {"pa": True, "drop": "watchdog"}}) is None
        assert rf.hpa_drop_text({"tx_active": True, "fe": {"pa": False, "drop": None}}) is None
        assert rf.hpa_drop_text(None) is None

    def test_last_drop_set_by_safe_and_cleared_by_the_next_key(self):
        fe, _ = mk(hm_cfg())
        assert fe.status()["last_drop"] is None
        fe.safe("no key yet")
        assert fe.status()["last_drop"] is None
        fe.tr_switch(True)
        fe.pa_enable(True, manual=True)
        fe.safe("watchdog")
        assert fe.status()["last_drop"] == "watchdog"
        fe.tr_switch(True)
        fe.pa_enable(True, manual=True)
        assert fe.status()["last_drop"] is None

    def test_false_samples_near_zero_seen_while_keyed(self):
        # the first low-drive key, 2026-11-04: the ADC gave these with the HPA on
        q = rf.hpa_reading_quality
        assert q(0.002, 0.699, -49.8, 2.8, True) == rf.HPA_READING_BAD
        assert q(0.004, 0.002, -49.6, 0.01, True) == rf.HPA_READING_BAD
        assert q(0.897, 0.002, 39.7, 0.01, True) == rf.HPA_READING_BAD
        assert q(0.897, 0.687, 39.7, 2.75, True) == rf.HPA_READING_OK

    def test_the_false_98_c_cannot_come_back_with_the_hpa_off(self):
        fe, _ = mk(hm_cfg())
        fe.set_hpa_volts(temp_v=1.48, current_v=0.41)
        d = fe.hpa_monitor_state(force=True)
        assert d["quality"] == rf.HPA_READING_OFF and d["hot"] is False
        assert d["hpa_temp_v"] == 1.48                                           # the raw figures stay in the status
        assert rf.hpa_card_text(d) == ("HPA off: monitor not valid", None)
        assert pt.hpa_monitor_text({"hpa_monitor": d}) == ("off: monitor not valid", False)

    def test_keying_gives_a_reading_at_once_and_hot_still_warns(self):
        fe, _ = mk(hm_cfg())
        fe.set_hpa_volts(temp_v=1.25, current_v=0.375)                           # 75 C, 1.5 A
        assert fe.hpa_monitor_state()["quality"] == rf.HPA_READING_OFF
        fe.tr_switch(True)
        fe.pa_enable(True, manual=True)
        d = fe.hpa_monitor_state()                                               # no wait for poll_s
        assert d["quality"] == rf.HPA_READING_OK and d["hot"] is True
        assert rf.hpa_card_text(d) == ("HPA 75.0 °C, 1.50 A", "warm")
        fe.pa_enable(False)
        assert fe.hpa_monitor_state()["quality"] == rf.HPA_READING_OFF

    def test_floating_and_bad_are_amber_and_not_hot(self):
        fe, _ = mk(hm_cfg())
        fe.tr_switch(True)
        fe.pa_enable(True, manual=True)
        fe.set_hpa_volts(temp_v=2.25, current_v=2.26)
        d = fe.hpa_monitor_state(force=True)
        assert d["quality"] == rf.HPA_READING_FLOATING and d["hot"] is False
        assert rf.hpa_card_text(d) == ("HPA monitor: inputs floating, check the DB9", "warm")
        fe.set_hpa_volts(temp_v=1.8, current_v=0.5)
        d = fe.hpa_monitor_state(force=True)
        assert rf.hpa_card_text(d) == ("HPA monitor: bad reading", "warm")
        assert pt.hpa_monitor_text({"hpa_monitor": d}) == ("monitor: bad reading", False)

    def test_valid_when_off_shows_the_figures(self):
        fe, _ = mk(hm_cfg(valid_when_off=True))
        fe.set_hpa_volts(temp_v=0.75, current_v=0.075)
        d = fe.hpa_monitor_state(force=True)
        assert d["quality"] == rf.HPA_READING_OK and d["hpa_temp_c"] == 25.0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
