"""RX front-end protection interlock (payload_guard.py) as wired into
rf_frontend.RfFrontend and dpmg_service.RadioService: all offline (MemoryGpio,
mc_atten MockTransport, a fixture payload profile).

What must hold:
* no ``payload:`` section: no guard, behaviour unchanged
* every PA key (manual, payload_key, TX run) is refused unless the switch reads
  back TX and the RX attenuator reads back >= the minimum safe value; with
  auto_protect the guard raises it first and keys only after readback confirms
* unkey order: PA off then switch RX then operator RX attenuation restored
* SAFE / watchdog drops keep the attenuator high (restore is explicit)
* atten_set below the minimum is refused while keyed / switch in TX
* fail closed: no attenuator, unreadable attenuator, broken profile
"""
import asyncio
import json
import os
import sys
import time
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dpmg.payload import mc_atten as mc  # noqa: E402
from dpmg.payload import payload_budget as pb  # noqa: E402
from dpmg.payload import payload_guard as pg  # noqa: E402
from dpmg.payload import rf_frontend as rf  # noqa: E402
from dpmg.link.proto import CmdStatus  # noqa: E402

# fixture profile
# Synthetic numbers (not the datasheet file): no coupler, so HPA Psat 50 dBm
# straight to the switch; switch isolation 40 dB (worst at 3000 MHz), LNA gain
# 30 dB, then the attenuator (IL 4 dB) after the LNA, G40 max safe 0 dBm, margin
# 10 dB. The attenuator protects the G40 only:
#   leak at the switch RX port 50 - 40 = 10 dBm = LNA in (under its 15 dBm max),
#   LNA out 40, G40 in at 0 dB 40 - 4 = 36 dBm
#   so need 36 - (0 - 10) = 46 dB.
FIX = {
    "components": {
        "g40tx": {"max_output_dbm": {"value": 5}},
        "g40rx": {"max_safe_input_dbm": {"value": 0}, "noise_figure_db": {"value": 8}},
        "hpa": {"gain_db": {"value": 45}, "p1db_dbm": {"value": 48}, "psat_dbm": {"value": 50},
                "max_input_dbm": {"value": 10}},
        "cpl": {"mainline_loss_db": {"value": 0.3}, "coupling_db": {"value": 30},
                "power_rating_dbm": {"value": 53}},
        "pm": {"max_input_dbm": {"value": 20}},
        "sw": {"insertion_loss_db": {"value": 0.5},
               "isolation_db": {"points": [[1000, 50], [3000, 40]]},
               "power_rating_dbm": {"value": 50}},
        "ant": {"gain_dbi": {"value": 3}},
        "lna": {"gain_db": {"value": 30}, "nf_db": {"value": 1.0}, "max_input_dbm": {"value": 15}},
        "att": {"insertion_loss_db": {"value": 4}, "max_db": {"value": 120}, "step_db": {"value": 0.05}},
    },
    "profiles": {
        "PX": {"g40_tx": "g40tx", "g40_rx": "g40rx", "hpa": "hpa",
               "switch": "sw", "antenna": "ant", "lna": "lna",
               "rx_atten": "att", "band_mhz": [1000, 3000]},
    },
    "protection": {"margin_db": 10},
}
MIN_DB = 46.0


@pytest.fixture
def profiles_file(tmp_path):
    import yaml
    p = tmp_path / "payload_profiles.yaml"
    p.write_text(yaml.safe_dump(FIX))
    return str(p)


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def gpio_cfg(tr_mode="gpio", payload=None, fault=False):
    d = {"frontend": "mock",
         "pa_enable": {"line": "cpu-gp-out1", "settle_ms": 2, "hang_ms": 0},
         "tr_switch": {"mode": tr_mode, "line": "cpu-gp-out2", "tx_level": 1, "settle_ms": 1},
         "pa_fault_in": {"enabled": fault},
         "atten": {"transport": "mock"},
         "watchdog_ms": 500}
    if payload is not None:
        d["payload"] = payload
    return rf.config_from_dict(d, source="test")


def mk(att_db=120.0, tr_mode="gpio", **guard_kw):
    clk = FakeClock()
    fe = rf.MockFrontend(gpio_cfg(tr_mode), clock=clk.now, sleep=clk.sleep)
    fe.open()
    at = mc.MiniCircuitsAtten(mc.MockTransport(att=att_db), 120.0, 0.05)
    g = pg.RxGuard(at, MIN_DB, **guard_kw)
    fe.guard = g
    return fe, at, g, clk


# config
class TestConfig:
    def test_absent_payload_is_none(self):
        assert gpio_cfg().payload is None

    def test_payload_parsed(self):
        c = gpio_cfg(payload={"profile": "P1", "margin_db": 12, "auto_protect": "yes"})
        assert c.payload == {"profile": "P1", "margin_db": 12.0, "auto_protect": True}

    def test_unknown_key_rejected(self):
        with pytest.raises(rf.ConfigError, match="unknown key"):
            gpio_cfg(payload={"profile": "P1", "bogus": 1})

    def test_profile_required(self):
        with pytest.raises(rf.ConfigError, match="payload.profile"):
            gpio_cfg(payload={"margin_db": 3})

    def test_disabled_section_is_none(self):
        assert gpio_cfg(payload={"enabled": False}).payload is None

    def test_load_power_parsed(self):
        c = gpio_cfg(payload={"profile": "P1", "load_power_dbm": 33, "load_margin_db": 3,
                              "load_point": "hpa", "tx_atten_max_db": 40, "tx_atten_step_db": 0.5})
        assert c.payload["load_power_dbm"] == 33.0 and c.payload["load_point"] == "hpa"
        assert c.payload["tx_atten_max_db"] == 40.0 and c.payload["tx_atten_step_db"] == 0.5

    def test_bad_load_point_rejected(self):
        with pytest.raises(rf.ConfigError, match="load_point"):
            gpio_cfg(payload={"profile": "P1", "load_point": "coupler"})

    def test_example_yaml_still_parses(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        c = rf.load_config(os.path.join(here, "rf_frontend.example.yaml"))
        assert c.payload is None


# guard on RfFrontend
class TestGuardKeying:
    def test_no_guard_keys_as_before(self):
        clk = FakeClock()
        fe = rf.MockFrontend(gpio_cfg(), clock=clk.now, sleep=clk.sleep).open()
        fe.tr_switch(True); fe.pa_enable(True, manual=True)
        assert fe.pa_level is True

    def test_switch_rx_still_refused_first(self):
        fe, at, g, _ = mk()
        with pytest.raises(rf.InterlockError, match="switch is in RX"):
            fe.pa_enable(True)
        assert fe.pa_level is False and at.t.commands == []     # guard never consulted

    def test_keys_when_already_protected(self):
        fe, at, g, _ = mk(att_db=120.0)
        fe.tr_switch(True); fe.pa_enable(True, manual=True)
        assert fe.pa_level is True and g.armed and g.saved_db is None
        assert not any(c.startswith(":SETATT") for c in at.t.commands)

    def test_at_minimum_still_goes_to_maximum(self):
        fe, at, g, _ = mk(att_db=MIN_DB)
        fe.tr_switch(True); fe.pa_enable(True, manual=True)
        assert fe.pa_level is True and at.t.att == 120.0 and g.saved_db == MIN_DB

    def test_lower_protect_value_in_the_config_is_ignored(self):
        fe, at, g, _ = mk(att_db=60.0, protect_db=30.0)
        assert g.protect_db == 120.0
        fe.tr_switch(True); fe.pa_enable(True, manual=True)
        assert at.t.att == 120.0

    def test_raised_before_the_switch_moves(self):
        fe, at, g, _ = mk(att_db=10.0)
        seen = []
        orig = fe._tr.set_active
        fe._tr.set_active = lambda v: (seen.append(at.t.att), orig(v))[1]
        fe.tr_switch(True)
        assert seen == [120.0] and fe.tr_level is True and fe.pa_level is False

    def test_failed_raise_lets_the_switch_move_but_refuses_the_key(self):
        fe, at, g, _ = mk(att_db=10.0)
        g.atten = None
        fe.tr_switch(True)                       # harmless with the HPA off
        assert fe.tr_level is True and "no RX attenuator" in fe.last_error
        with pytest.raises(rf.InterlockError, match="no RX attenuator"):
            fe.pa_enable(True, manual=True)
        assert fe.pa_level is False

    def test_auto_protect_raises_then_keys(self):
        fe, at, g, _ = mk(att_db=10.0)
        fe.tr_switch(True); fe.pa_enable(True, manual=True)
        assert fe.pa_level is True and at.t.att == 120.0 and g.saved_db == 10.0
        # attenuator was raised, then read back again by the key, before the PA line went high
        assert ":SETATT=120.00" in at.t.commands and at.t.commands[-1] == ":ATT?"
        assert [h[3] for h in fe.gpio.history if h[2] == 41] == [True]

    def test_unkey_order_pa_switch_restore(self):
        fe, at, g, clk = mk(att_db=10.0)
        order = []
        orig_set = at.set_db
        at.set_db = lambda db: (order.append(("atten", db, fe.pa_level, fe.tr_level)), orig_set(db))[1]
        fe.sequence_tx_on()
        fe.sequence_tx_off()
        assert fe.pa_level is False and fe.tr_level is False
        assert at.t.att == 10.0 and g.saved_db is None and not g.armed
        # raise happened with the PA off and the switch still in RX; restore the same
        assert order == [("atten", 120.0, False, False), ("atten", 10.0, False, False)]

    def test_auto_protect_off_refuses(self):
        fe, at, g, _ = mk(att_db=10.0, auto_protect=False)
        fe.tr_switch(True)
        with pytest.raises(rf.InterlockError, match="RX protection: RX attenuator 10.00 dB"):
            fe.pa_enable(True, manual=True)
        assert fe.pa_level is False and at.t.att == 10.0 and g.refusals == 1

    def test_readback_failure_refuses(self):
        fe, at, g, _ = mk()
        fe.tr_switch(True)
        at.t.fail_next = OSError("usb gone")
        with pytest.raises(rf.InterlockError, match="readback failed"):
            fe.pa_enable(True, manual=True)
        assert fe.pa_level is False

    def test_device_ignoring_set_refuses(self):
        fe, at, g, _ = mk(att_db=10.0)
        at.t.scpi_orig = at.t.scpi

        def stuck(cmd, timeout=1.0):
            if cmd.startswith(":SETATT"):
                at.t.commands.append(cmd)
                return "1"                 # claims ok, value unchanged
            return at.t.scpi_orig(cmd, timeout)
        at.t.scpi = stuck
        fe.tr_switch(True)
        with pytest.raises(rf.InterlockError, match="after protect set"):
            fe.pa_enable(True, manual=True)
        assert fe.pa_level is False

    def test_set_rejected_refuses(self):
        fe, at, g, _ = mk(att_db=10.0, protect_db=130.0)   # > device max: device says 2
        fe.tr_switch(True)
        with pytest.raises(rf.InterlockError, match="failed"):
            fe.pa_enable(True, manual=True)
        assert fe.pa_level is False

    def test_no_attenuator_refuses(self):
        fe, at, g, _ = mk()
        g.atten = None
        fe.tr_switch(True)
        with pytest.raises(rf.InterlockError, match="no RX attenuator"):
            fe.pa_enable(True, manual=True)
        assert fe.pa_level is False

    def test_proxy_mode_refused(self):
        fe, at, g, _ = mk(tr_mode="proxy")
        fe.tr_switch(True)
        with pytest.raises(rf.InterlockError, match="proxy"):
            fe.pa_enable(True, manual=True)
        assert fe.pa_level is False

    def test_switch_readback_mismatch_refused(self):
        fe, at, g, _ = mk()
        fe.tr_switch(True)
        fe._tr.value = False                      # line not actually at the TX level
        with pytest.raises(rf.InterlockError, match="not confirmed in TX"):
            fe.pa_enable(True, manual=True)

    def test_adc_readback_allows_proxy_and_gates_key(self):
        # proxy mode is normally refused (test above); with require_tr_readback and
        # an ADC fitted, the guard accepts proxy and the ADC confirms the switch.
        clk = FakeClock()
        cfg = rf.config_from_dict(
            {"frontend": "mock",
             "pa_enable": {"line": "cpu-gp-out1", "settle_ms": 2, "hang_ms": 0},
             "tr_switch": {"mode": "proxy", "line": "cpu-gp-out2", "tx_level": 1},
             "atten": {"transport": "mock"},
             "tr_readback": {"enabled": True, "transport": "mock", "settle_ms": 0,
                             "tx_window_v": [2.7, 5.5], "rx_window_v": [-0.5, 0.8]},
             "watchdog_ms": 500}, source="test")
        fe = rf.MockFrontend(cfg, clock=clk.now, sleep=clk.sleep); fe.open()
        at = mc.MiniCircuitsAtten(mc.MockTransport(att=120.0), 120.0, 0.05)
        fe.guard = pg.RxGuard(at, MIN_DB, require_tr_readback=True)
        fe.tr_switch(True)
        fe.set_tr_volts(2.5)                      # half rail: ADC says bad
        with pytest.raises((rf.FrontendFault, rf.InterlockError)):
            fe.pa_enable(True, manual=True)
        assert fe.pa_level is False
        fe.set_tr_volts(3.3)                      # TX window: keys
        fe.pa_enable(True, manual=True)
        assert fe.pa_level is True

    def test_config_error_fails_closed(self):
        fe, at, g, _ = mk(config_error="profiles not found")
        fe.tr_switch(True)
        with pytest.raises(rf.InterlockError, match="config error"):
            fe.pa_enable(True, manual=True)

    def test_watchdog_drop_keeps_atten_high(self):
        fe, at, g, clk = mk(att_db=10.0)
        fe.sequence_tx_on()
        clk.t += 1.0
        assert fe.watchdog_check() == "watchdog"
        assert fe.pa_level is False and fe.tr_level is False
        assert at.t.att == 120.0 and g.saved_db == 10.0 and not g.armed
        assert g.restore_now(fe) is None and at.t.att == 10.0 and g.saved_db is None

    def test_restore_refused_while_keyed(self):
        fe, at, g, _ = mk(att_db=10.0)
        fe.tr_switch(True); fe.pa_enable(True, manual=True)
        assert "refused" in g.restore_now(fe) and at.t.att == 120.0

    def test_atten_request_rules(self):
        fe, at, g, _ = mk()
        assert g.atten_request_ok(10.0, fe) is None              # RX, unkeyed: free
        fe.tr_switch(True)
        assert "switch is in TX" in g.atten_request_ok(10.0, fe)
        assert "switch is in TX" in g.atten_request_ok(119.0, fe)
        assert g.atten_request_ok(120.0, fe) is None
        fe.pa_enable(True, manual=True)
        assert "keyed" in g.atten_request_ok(45.0, fe)
        assert g.atten_request_ok(120.0, fe) is None

    def test_tx_run_path_through_guard(self):
        import numpy as np
        from dpmg.radio import sdr_core as sdr
        fe, at, g, _ = mk(att_db=20.0)
        be = sdr.MockBackend(sdr.SDRConfig(sample_rate=1e6, center_freq=2.2e9)); be.open()
        be.frontend = fe
        be.send(np.zeros(1000, np.complex64))
        assert fe.pa_level is True and at.t.att == 120.0
        be.stop_tx()
        assert fe.pa_level is False and at.t.att == 20.0

    def test_tx_run_refused_without_attenuator(self):
        import numpy as np
        from dpmg.radio import sdr_core as sdr
        fe, at, g, _ = mk()
        g.atten = None
        be = sdr.MockBackend(sdr.SDRConfig(sample_rate=1e6, center_freq=2.2e9)); be.open()
        be.frontend = fe
        with pytest.raises(rf.InterlockError):
            be.send(np.zeros(1000, np.complex64))
        assert fe.pa_level is False and fe.tr_level is False


class TestBuildGuard:
    def test_min_from_profile(self, profiles_file):
        at = mc.MiniCircuitsAtten(mc.MockTransport(), 120, 0.05)
        g = pg.build_guard({"profile": "PX", "profiles_file": profiles_file}, at,
                           pb.load_profiles, pb.min_safe_rx_atten, pb.profile)
        assert g.config_error is None and g.min_db == pytest.approx(MIN_DB)
        assert g.detail["worst_freq_mhz"] == 3000.0

    def test_floor_and_margin(self, profiles_file):
        at = mc.MiniCircuitsAtten(mc.MockTransport(), 120, 0.05)
        g = pg.build_guard({"profile": "PX", "profiles_file": profiles_file, "margin_db": 0,
                            "min_rx_atten_db": 40}, at,
                           pb.load_profiles, pb.min_safe_rx_atten, pb.profile)
        assert g.min_db == pytest.approx(40.0)                 # computed 36.0 < floor 40

    def test_bad_profile_fails_closed(self, profiles_file):
        at = mc.MiniCircuitsAtten(mc.MockTransport(), 120, 0.05)
        g = pg.build_guard({"profile": "NOPE", "profiles_file": profiles_file}, at,
                           pb.load_profiles, pb.min_safe_rx_atten, pb.profile)
        assert g.config_error and g.min_db == 120.0

    def test_missing_file_fails_closed(self, tmp_path):
        g = pg.build_guard({"profile": "P1", "profiles_file": str(tmp_path / "x.yaml")}, None,
                           pb.load_profiles, pb.min_safe_rx_atten, pb.profile)
        assert "not found" in g.config_error


# load power limit
class TestLoadPowerLimit:
    # the HPA keyed into a small bench pad: refuse until the G40 TX attenuation is
    # high enough that the delivered power stays under the pad rating, and refuse
    # outright when no TX attenuation in range is enough.  The live TX attenuation
    # reaches the guard through fe.tx_atten_getter (the service sets it).

    def test_build_computes_min_tx_atten(self, profiles_file):
        at = mc.MiniCircuitsAtten(mc.MockTransport(), 120, 0.05)
        g = pg.build_guard({"profile": "PX", "profiles_file": profiles_file,
                            "load_power_dbm": 33, "load_margin_db": 3}, at,
                           pb.load_profiles, pb.min_safe_rx_atten, pb.profile, pb.min_safe_tx_atten)
        assert g.config_error is None and g.load_power_dbm == 33.0 and g.tx_load_achievable
        data = pb.load_profiles(profiles_file)
        want = pb.min_safe_tx_atten(data, pb.profile(data, "PX"), 33.0, margin_db=3)
        assert g.min_tx_atten_db == pytest.approx(want["min_tx_atten_db"])
        assert "load" in g.status() and g.status()["load"]["achievable"]

    def test_no_min_tx_fn_leaves_load_off(self, profiles_file):
        # build_guard called the old way (no min_tx_atten) ignores load_power_dbm
        at = mc.MiniCircuitsAtten(mc.MockTransport(), 120, 0.05)
        g = pg.build_guard({"profile": "PX", "profiles_file": profiles_file,
                            "load_power_dbm": 20}, at,
                           pb.load_profiles, pb.min_safe_rx_atten, pb.profile)
        assert g.load_power_dbm is None and "load" not in g.status()

    def test_key_refused_below_min_tx_atten(self):
        fe, at, g, _ = mk(load_power_dbm=20.0, min_tx_atten_db=30.0, tx_load_achievable=True)
        fe.tr_switch(True)
        fe.tx_atten_getter = lambda: 25.0                 # below the 30 dB minimum
        with pytest.raises(rf.InterlockError, match="below the minimum"):
            fe.pa_enable(True, manual=True)
        assert fe.pa_level is False
        fe.tx_atten_getter = lambda: 30.0                 # at the minimum: keys
        fe.pa_enable(True, manual=True)
        assert fe.pa_level is True

    def test_unknown_tx_atten_fails_closed(self):
        fe, at, g, _ = mk(load_power_dbm=20.0, min_tx_atten_db=30.0)
        fe.tr_switch(True)
        fe.tx_atten_getter = lambda: None                 # readable but unknown
        with pytest.raises(rf.InterlockError, match="unknown"):
            fe.pa_enable(True, manual=True)
        assert fe.pa_level is False
        fe2, _, _, _ = mk(load_power_dbm=20.0, min_tx_atten_db=30.0)
        fe2.tr_switch(True)                               # no getter at all = unknown
        with pytest.raises(rf.InterlockError, match="unknown"):
            fe2.pa_enable(True, manual=True)

    def test_unachievable_refuses_outright(self):
        fe, at, g, _ = mk(load_power_dbm=20.0, min_tx_atten_db=41.75, tx_load_achievable=False)
        fe.tr_switch(True)
        fe.tx_atten_getter = lambda: 41.75                # even at max it still refuses
        with pytest.raises(rf.InterlockError, match="higher-power load"):
            fe.pa_enable(True, manual=True)
        assert fe.pa_level is False

    def test_no_load_limit_ignores_tx_atten(self):
        fe, at, g, _ = mk()                               # no load_power_dbm
        fe.tr_switch(True)
        fe.tx_atten_getter = lambda: 0.0                  # load check never engages
        fe.pa_enable(True, manual=True)
        assert fe.pa_level is True


# service
def _svc(cfg_path):
    from dpmg.radio.dpmg_service import RadioService
    from dpmg.link.proto import HostType
    return RadioService(serial="G40-PAYLOAD-TEST", host_type=HostType.G40_RX, kind="g40",
                        simulate=True, cmd_port=0, discovery_port=0, broadcast="127.0.0.1",
                        frontend_config=cfg_path)


def _write_fe(tmp_path, profiles_file, payload=True, **pl):
    import yaml
    d = {"frontend": "mock",
         "pa_enable": {"line": "cpu-gp-out1", "settle_ms": 0, "hang_ms": 0},
         "tr_switch": {"mode": "gpio", "line": "cpu-gp-out2", "tx_level": 1, "settle_ms": 0},
         "atten": {"transport": "mock"}, "watchdog_ms": 500}
    if payload:
        d["payload"] = {"profile": "PX", "profiles_file": profiles_file, **pl}
    p = tmp_path / "rf_frontend.yaml"
    p.write_text(yaml.safe_dump(d))
    return str(p)


def run(coro_fn):
    async def main():
        return await coro_fn()
    return asyncio.run(main())


class TestLnaFloor:
    """The G40 TX attenuation floor that keeps the LNA under its max input when
    the switch isolation alone is not enough (payload_budget.min_lna_safe_tx_atten)."""

    def test_built_from_the_profile(self, profiles_file):
        from dpmg.payload import payload_budget as pb
        at = mc.MiniCircuitsAtten(mc.MockTransport(att=120.0), 120.0, 0.05)
        g = pg.build_guard({"profile": "PX", "profiles_file": profiles_file}, at,
                           pb.load_profiles, pb.min_safe_rx_atten, pb.profile,
                           pb.min_safe_tx_atten, lna_tx_floor=pb.min_lna_safe_tx_atten)
        want = pb.min_lna_safe_tx_atten(pb.load_profiles(profiles_file),
                                        pb.profile(pb.load_profiles(profiles_file), "PX"))
        assert want["needed"] and g.lna_min_tx_atten_db == pytest.approx(want["min_tx_atten_db"])
        assert g.status()["lna_min_tx_atten_db"] == pytest.approx(want["min_tx_atten_db"])
        # built without the floor function: no LNA floor
        g2 = pg.build_guard({"profile": "PX", "profiles_file": profiles_file}, at,
                            pb.load_profiles, pb.min_safe_rx_atten, pb.profile)
        assert g2.lna_min_tx_atten_db is None and "lna_min_tx_atten_db" not in g2.status()

    def test_lna_floor_false_bench_config(self, profiles_file):
        # bench without the LNA (HPA into a coupler and a dummy load): lna_floor false
        # leaves the floor out, and the status says so
        from dpmg.payload import payload_budget as pb
        at = mc.MiniCircuitsAtten(mc.MockTransport(att=120.0), 120.0, 0.05)
        g = pg.build_guard({"profile": "PX", "profiles_file": profiles_file, "lna_floor": False}, at,
                           pb.load_profiles, pb.min_safe_rx_atten, pb.profile,
                           pb.min_safe_tx_atten, lna_tx_floor=pb.min_lna_safe_tx_atten)
        assert g.lna_min_tx_atten_db is None and g.tx_floor_db() is None
        st = g.status()
        assert st["lna_floor_off"] is True and "lna_min_tx_atten_db" not in st
        assert g.config_error is None
        # the default keeps the floor and does not mention the switch
        g2 = pg.build_guard({"profile": "PX", "profiles_file": profiles_file}, at,
                            pb.load_profiles, pb.min_safe_rx_atten, pb.profile,
                            pb.min_safe_tx_atten, lna_tx_floor=pb.min_lna_safe_tx_atten)
        assert g2.lna_min_tx_atten_db is not None and "lna_floor_off" not in g2.status()

    def test_no_floor_with_the_attenuator_first(self):
        from dpmg.payload import payload_budget as pb
        real = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "payload_profiles.yaml")
        at = mc.MiniCircuitsAtten(mc.MockTransport(att=120.0), 120.0, 0.05)
        g = pg.build_guard({"profile": "P2a", "profiles_file": real}, at, pb.load_profiles,
                           pb.min_safe_rx_atten, pb.profile, pb.min_safe_tx_atten,
                           lna_tx_floor=pb.min_lna_safe_tx_atten)
        assert g.config_error is None and g.lna_min_tx_atten_db is None
        assert g.min_db == pytest.approx(pb.min_safe_rx_atten(pb.load_profiles(real),
                                                              pb.profile(pb.load_profiles(real), "P2a"))["min_rx_atten_db"])

    def test_p2_as_built_guard(self):
        # the attenuator directly after the switch: min safe 23.95 dB and no TX floor
        from dpmg.payload import payload_budget as pb
        real = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "payload_profiles.yaml")
        at = mc.MiniCircuitsAtten(mc.MockTransport(att=120.0), 120.0, 0.05)
        g = pg.build_guard({"profile": "P2", "profiles_file": real}, at, pb.load_profiles,
                           pb.min_safe_rx_atten, pb.profile, pb.min_safe_tx_atten,
                           lna_tx_floor=pb.min_lna_safe_tx_atten)
        assert g.config_error is None and g.min_db == pytest.approx(23.95)
        assert g.lna_min_tx_atten_db is None and g.tx_floor_db() is None
        st = g.status()
        assert st["lna_unprotected"] is False and st["worst_freq_mhz"] == 700.0
        assert "lna_min_tx_atten_db" not in st

    def test_key_refused_below_the_floor(self):
        fe, at, g, _ = mk(lna_min_tx_atten_db=36.75)
        fe.tr_switch(True)
        fe.tx_atten_getter = lambda: 30.0
        with pytest.raises(rf.InterlockError, match="below the 36.75 dB floor"):
            fe.pa_enable(True)
        assert not fe._keyed
        fe.tx_atten_getter = lambda: 36.75
        fe.pa_enable(True)
        assert fe._keyed

    def test_unknown_tx_atten_fails_closed(self):
        fe, at, g, _ = mk(lna_min_tx_atten_db=10.0)
        fe.tr_switch(True)
        with pytest.raises(rf.InterlockError, match="unknown"):
            fe.pa_enable(True)                  # no getter at all
        fe.tx_atten_getter = lambda: None
        with pytest.raises(rf.InterlockError, match="unknown"):
            fe.pa_enable(True)

    def test_unachievable_refuses_at_any_atten(self):
        fe, at, g, _ = mk(lna_min_tx_atten_db=41.75, lna_tx_achievable=False)
        fe.tr_switch(True)
        fe.tx_atten_getter = lambda: 41.75
        with pytest.raises(rf.InterlockError, match="cannot be protected"):
            fe.pa_enable(True)

    def test_set_tx_atten_refused_while_keyed(self):
        fe, at, g, _ = mk(lna_min_tx_atten_db=20.0)
        assert g.tx_atten_request_ok(5.0, fe) is None       # not keyed: the next key refuses
        fe.tr_switch(True)
        fe.tx_atten_getter = lambda: 25.0
        fe.pa_enable(True)
        assert "below the 20.00 dB floor" in g.tx_atten_request_ok(19.5, fe)
        assert g.tx_atten_request_ok(20.0, fe) is None
        assert g.tx_atten_request_ok(30.0, fe) is None

    def test_floor_is_the_higher_of_lna_and_load(self):
        _, _, g, _ = mk(lna_min_tx_atten_db=20.0, load_power_dbm=33.0, min_tx_atten_db=32.0)
        assert g.tx_floor_db() == 32.0
        _, _, g, _ = mk(lna_min_tx_atten_db=20.0)
        assert g.tx_floor_db() == 20.0
        _, _, g, _ = mk()
        assert g.tx_floor_db() is None


class TestService:
    def test_set_tx_atten_floor_while_keyed(self, tmp_path, profiles_file):
        svc = _svc(_write_fe(tmp_path, profiles_file))

        class _Be:
            def __init__(self):
                self.config = types.SimpleNamespace(tx_gain=20.0, center_freq=2.0e9, tx_center_freq=None)
                self.writes = []

            def set_tx_attenuation_qdb(self, q):
                self.writes.append(q)

        async def go():
            svc.loop = asyncio.get_running_loop()
            await svc._run(svc._frontend_start)
            svc.backend = _Be()                 # init value 20 dB, read by the guard
            await svc._cmd_atten_set(1, {"db": 60.0})
            assert (await svc._cmd_payload_key(2, {"on": True})).status == CmdStatus.OK
            floor = svc.guard.lna_min_tx_atten_db
            r = await svc._cmd_set_tx_atten(3, {"qdb": int((floor - 1.0) * 4)})
            assert r.status == CmdStatus.REJECTED and "floor" in r.result["err"]
            r = await svc._cmd_set_tx_atten(4, {"qdb": int(round((floor + 1.0) * 4))})
            assert r.status == CmdStatus.OK
            assert svc.backend.writes == [int(round((floor + 1.0) * 4))]
            await svc._cmd_payload_key(5, {"on": False})
            await svc._run(svc._frontend_stop)
        run(go)

    def test_payload_key_cycle(self, tmp_path, profiles_file):
        svc = _svc(_write_fe(tmp_path, profiles_file))

        async def go():
            svc.loop = asyncio.get_running_loop()
            await svc._run(svc._frontend_start)
            svc._tx_atten_db = 20.0      # PX needs a 5 dB LNA TX floor (10 dB margin)
            at = svc.atten
            r = await svc._cmd_atten_set(1, {"db": 12.0})
            assert r.status == CmdStatus.OK and r.result["readback_db"] == 12.0
            st = (await svc._cmd_payload_status(2, {})).result
            assert st["configured"] and st["guard"]["min_db"] == pytest.approx(MIN_DB)
            assert len(json.dumps(st)) < 1300                    # fits one CMD_ACK datagram
            r = await svc._cmd_payload_key(3, {"on": True})
            assert r.status == CmdStatus.OK, r.result
            s = r.result["status"]
            assert s["pa"]["keyed"] and s["tr"]["tx"] and s["atten"]["att_db"] == 120.0
            assert s["guard"]["saved_db"] == 12.0 and s["guard"]["armed"]
            # a script cannot lower the attenuator under the keyed HPA
            r = await svc._cmd_atten_set(4, {"db": 0.0})
            assert r.status == CmdStatus.REJECTED and r.result.get("guard")
            assert at.get_db() == 120.0
            r = await svc._cmd_atten_set(5, {"db": 80.0})          # above the minimum: still refused
            assert r.status == CmdStatus.REJECTED and r.result.get("guard")
            assert at.get_db() == 120.0
            r = await svc._cmd_payload_key(6, {"on": False})
            s = r.result["status"]
            assert r.status == CmdStatus.OK and not s["pa"]["keyed"] and not s["tr"]["tx"]
            assert s["atten"]["att_db"] == 12.0 and s["guard"]["saved_db"] is None
            await svc._run(svc._frontend_stop)
        run(go)

    def test_frontend_set_pa_goes_through_guard(self, tmp_path, profiles_file):
        svc = _svc(_write_fe(tmp_path, profiles_file, auto_protect=False))

        async def go():
            svc.loop = asyncio.get_running_loop()
            await svc._run(svc._frontend_start)
            await svc._cmd_atten_set(1, {"db": 5.0})
            assert (await svc._cmd_frontend_set(2, {"tr": "tx"})).status == CmdStatus.OK
            r = await svc._cmd_frontend_set(3, {"pa": True})
            assert r.status == CmdStatus.FAILED and "RX protection" in r.result["err"]
            assert svc.frontend.pa_level is False
            r = await svc._cmd_payload_key(4, {"on": True})
            assert r.status == CmdStatus.FAILED
            assert r.result["status"]["pa"]["keyed"] is False
            assert r.result["status"]["tr"]["tx"] is False         # failed key: SAFE
            await svc._run(svc._frontend_stop)
        run(go)

    def test_startup_value_below_min_refused(self, tmp_path, profiles_file):
        svc = _svc(_write_fe(tmp_path, profiles_file))

        async def go():
            svc.loop = asyncio.get_running_loop()
            await svc._run(svc._frontend_start)
            r = await svc._cmd_atten_set(1, {"db": 10.0, "startup": True})
            assert r.status == CmdStatus.REJECTED and "power-on" in r.result["err"]
            r = await svc._cmd_atten_set(2, {"db": 120.0, "startup": True})
            assert r.status == CmdStatus.OK and r.result["startup_db"] == 120.0
            await svc._run(svc._frontend_stop)
        run(go)

    def test_no_payload_section_unchanged(self, tmp_path, profiles_file):
        svc = _svc(_write_fe(tmp_path, profiles_file, payload=False))

        async def go():
            svc.loop = asyncio.get_running_loop()
            await svc._run(svc._frontend_start)
            assert svc.guard is None and svc.frontend.guard is None
            await svc._cmd_atten_set(1, {"db": 0.0})
            await svc._cmd_frontend_set(2, {"tr": "tx"})
            r = await svc._cmd_frontend_set(3, {"pa": True})
            assert r.status == CmdStatus.OK and svc.frontend.pa_level is True
            r = await svc._cmd_payload_key(4, {"on": True})
            assert r.status == CmdStatus.REJECTED and "not configured" in r.result["err"]
            st = (await svc._cmd_payload_status(5, {})).result
            assert st["configured"] is False
            # unkey is the safe direction: it works without a guard (the PA was put
            # on by frontend_set above) and leaves the HPA off with the switch on RX
            r = await svc._cmd_payload_key(6, {"on": False})
            assert r.status == CmdStatus.OK, r.result
            assert svc.frontend.pa_level is False and svc.frontend._tr_tx is False
            assert r.result["status"]["pa"]["keyed"] is False
            await svc._run(svc._frontend_stop)
        run(go)

    def test_restore_after_safe(self, tmp_path, profiles_file):
        svc = _svc(_write_fe(tmp_path, profiles_file))

        async def go():
            svc.loop = asyncio.get_running_loop()
            await svc._run(svc._frontend_start)
            svc._tx_atten_db = 20.0      # PX needs a 5 dB LNA TX floor (10 dB margin)
            await svc._cmd_atten_set(1, {"db": 30.0})
            assert (await svc._cmd_payload_key(2, {"on": True})).status == CmdStatus.OK
            await svc._cmd_frontend_set(3, {"safe": True})
            st = (await svc._cmd_payload_status(4, {})).result
            assert st["atten"]["att_db"] == 120.0 and st["guard"]["saved_db"] == 30.0
            r = await svc._cmd_payload_restore(5, {})
            assert r.status == CmdStatus.OK and r.result["status"]["atten"]["att_db"] == 30.0
            await svc._run(svc._frontend_stop)
        run(go)

    def test_reload_rebuilds_guard(self, tmp_path, profiles_file):
        svc = _svc(_write_fe(tmp_path, profiles_file))

        async def go():
            svc.loop = asyncio.get_running_loop()
            await svc._run(svc._frontend_start)
            g1 = svc.guard
            r = await svc._cmd_frontend_reload(1, {})
            assert r.status == CmdStatus.OK
            assert svc.guard is not None and svc.guard is not g1 and svc.frontend.guard is svc.guard
            await svc._run(svc._frontend_stop)
        run(go)

    def test_set_tx_atten_echo(self, tmp_path, profiles_file):
        from dpmg.radio import sdr_core as sdr
        svc = _svc(_write_fe(tmp_path, profiles_file))

        async def go():
            svc.loop = asyncio.get_running_loop()
            await svc._run(svc._frontend_start)
            svc.backend = sdr.MockBackend(sdr.SDRConfig(sample_rate=1e6, center_freq=2.2e9))
            svc.backend.open()
            r = await svc._cmd_set_tx_atten(1, {"qdb": 40})
            assert r.status == CmdStatus.OK
            st = (await svc._cmd_payload_status(2, {})).result
            assert st["tx_atten_db"] == 10.0 and st["tx_center_hz"] == 2.2e9
            svc.backend = None
            await svc._run(svc._frontend_stop)
        run(go)


if __name__ == "__main__":  # OFF-01 loop runs `python3 tests/<file>.py`; delegate to pytest
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))


# Payload 2 as built
class TestPayload2Guard:
    def _guard(self, **cfg):
        from dpmg.payload import payload_budget as pb
        at = mc.MiniCircuitsAtten(mc.MockTransport(att=30.0), 120.0, 0.05)
        return pg.build_guard({"profile": "P2", **cfg}, at, pb.load_profiles, pb.min_safe_rx_atten,
                              pb.profile, min_tx_atten=pb.min_safe_tx_atten,
                              lna_tx_floor=pb.min_lna_safe_tx_atten)

    def test_attenuator_first_needs_no_lna_floor_and_goes_to_maximum(self):
        g = self._guard()
        assert g.config_error is None and g.protect_db == 120.0 and g.lna_min_tx_atten_db is None

    def test_switch_limit_sets_a_tx_floor(self):
        g = self._guard(load_power_dbm=47, load_margin_db=3, load_point="switch")
        assert g.config_error is None and g.tx_load_achievable
        assert g.min_tx_atten_db == pytest.approx(13.0) and g.tx_floor_db() == pytest.approx(13.0)

    def test_switch_limit_refuses_low_tx_attenuation_and_unknown(self):
        fe, at, g, _ = mk()
        fe.guard = g = self._guard(load_power_dbm=47, load_margin_db=3, load_point="switch")
        fe.tr_switch(True)
        with pytest.raises(rf.InterlockError, match="unknown"):
            fe.pa_enable(True, manual=True)         # no getter: fail closed
        fe.tx_atten_getter = lambda: 5.0
        with pytest.raises(rf.InterlockError, match="below the minimum"):
            fe.pa_enable(True, manual=True)
        assert fe.pa_level is False
        fe.tx_atten_getter = lambda: 13.0
        fe.pa_enable(True, manual=True)
        assert fe.pa_level is True
        assert "floor" in g.tx_atten_request_ok(5.0, fe) and g.tx_atten_request_ok(13.0, fe) is None
