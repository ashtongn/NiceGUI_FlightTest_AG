"""rf_frontend: config parsing/validation, line-spec forms, sequencing order and
timing, fail-safe, watchdog, fault input, interlocks, idempotence, reload, and the
sdr_core hook points over MockBackend: all offline (MemoryGpio)."""
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dpmg.payload import rf_frontend as rf  # noqa: E402


# fakes
class FakeClock:
    """Deterministic clock; sleep() advances it (so settle/hang are measurable)."""

    def __init__(self):
        self.t = 1000.0
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def mk(cfg=None, clock=None, gpio=None, **kw):
    clock = clock or FakeClock()
    cfg = cfg or rf.FrontendConfig(frontend="mock")
    fe = rf.MockFrontend(cfg, gpio=gpio, clock=clock.now, sleep=clock.sleep, **kw)
    fe.open()
    return fe, clock


def gpio_cfg(tr_mode="gpio", fault=False, **over):
    d = {"frontend": "mock",
         "pa_enable": {"chip": "gpiochip0", "line": "cpu-gp-out1", "fallback_line": "PG.06",
                       "settle_ms": 2, "hang_ms": 20},
         "tr_switch": {"mode": tr_mode, "chip": "gpiochip0", "line": "cpu-gp-out2",
                       "fallback_line": "PN.01", "tx_level": 1, "settle_ms": 1},
         "pa_fault_in": {"enabled": fault, "chip": "gpiochip1", "line": "cpu-gp-in",
                         "fallback_line": "PCC.01", "active_low": True},
         "watchdog_ms": 500}
    for k, v in over.items():
        sec, key = k.split(".")
        d[sec][key] = v
    return rf.config_from_dict(d, source="test")


# config
class TestConfig:
    def test_defaults_when_file_missing(self, tmp_path):
        cfg = rf.load_config(str(tmp_path / "nope.yaml"))
        assert cfg.frontend == "none" and cfg.source == "defaults"
        assert rf.make_frontend(cfg) is None

    def test_missing_required(self, tmp_path):
        with pytest.raises(rf.ConfigError):
            rf.load_config(str(tmp_path / "nope.yaml"), required=True)

    def test_example_file_parses_to_none(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cfg = rf.load_config(os.path.join(here, "rf_frontend.example.yaml"))
        assert cfg.frontend == "none"
        assert cfg.pa_enable.line == "cpu-gp-out1" and cfg.pa_enable.fallback_line == "PG.06"
        assert cfg.tr_switch.mode == "proxy" and cfg.pa_fault_in.enabled is False
        assert cfg.atten.transport == "none" and cfg.atten.step_db == 0.05

    def test_yaml_roundtrip(self, tmp_path):
        p = tmp_path / "rf_frontend.yaml"
        p.write_text("frontend: gpio\npa_enable: {line: 41, settle_ms: 5}\n"
                     "tr_switch: {mode: gpio, line: PN.01}\nwatchdog_ms: 250\n")
        cfg = rf.load_config(str(p))
        assert cfg.frontend == "gpio" and cfg.pa_enable.line == 41
        assert cfg.pa_enable.settle_ms == 5 and cfg.tr_switch.mode == "gpio"
        assert cfg.watchdog_ms == 250 and cfg.source == str(p)

    def test_json_is_valid_yaml(self, tmp_path):
        p = tmp_path / "rf_frontend.yaml"
        p.write_text('{"frontend": "mock", "pa_enable": {"line": "PG.06"}}')
        assert rf.load_config(str(p)).pa_enable.line == "PG.06"

    @pytest.mark.parametrize("bad,needle", [
        ({"frontend": "gpio", "pa_enable": {"line": "x", "foo": 1}}, "foo"),
        ({"frontend": "gpio", "pa_enable": {"line": "x"}, "bogus_section": {}}, "bogus_section"),
        ({"frontend": "lasers"}, "frontend"),
        ({"frontend": "gpio", "pa_enable": {"line": "x"}, "tr_switch": {"mode": "fpga"}}, "mode"),
        ({"frontend": "gpio", "pa_enable": {"line": "x", "settle_ms": -1}}, "settle_ms"),
        ({"frontend": "gpio", "pa_enable": {"line": "x", "active_high": "maybe"}}, "active_high"),
        ({"frontend": "gpio", "pa_enable": {"line": "x"}, "atten": {"transport": "serial"}}, "transport"),
        ({"frontend": "gpio", "pa_enable": {"line": "x"}, "watchdog_ms": "soon"}, "watchdog_ms"),
        ({"frontend": "gpio", "pa_enable": {"line": None}}, "pa_enable.line"),
        ({"frontend": "gpio", "pa_enable": {"line": "x"}, "tr_switch": {"mode": "gpio", "line": ""}},
         "tr_switch.line"),
        ({"frontend": "gpio", "pa_enable": {"line": True}}, "boolean"),
        ({"frontend": "gpio", "pa_enable": "cpu-gp-out1"}, "mapping"),
    ])
    def test_rejects_unknown_keys_and_bad_values(self, bad, needle):
        with pytest.raises(rf.ConfigError) as ei:
            rf.config_from_dict(bad)
        assert needle in str(ei.value)

    def test_line_spec_three_forms(self):
        assert rf.parse_line_spec("cpu-gp-out1") == ("name", "cpu-gp-out1")
        assert rf.parse_line_spec("PG.06") == ("name", "PG.06")
        assert rf.parse_line_spec(41) == ("offset", 41)
        assert rf.parse_line_spec("41") == ("offset", 41)
        assert rf.parse_line_spec(None) == ("none", None)
        assert rf.parse_line_spec("") == ("none", None)
        with pytest.raises(rf.ConfigError):
            rf.parse_line_spec(-3)

    def test_chip_path(self):
        assert rf.chip_path("gpiochip0") == "/dev/gpiochip0"
        assert rf.chip_path("/dev/gpiochip2") == "/dev/gpiochip2"

    def test_overrides(self):
        cfg = rf.FrontendConfig()
        out = rf.apply_overrides(cfg, {"frontend": "mock", "pa_enable.line": "PN.01",
                                       "tr_switch.mode": "gpio", "atten.transport": "mock",
                                       "pa_fault_in.enabled": True, "ignored": None})
        assert out.frontend == "mock" and out.pa_enable.line == "PN.01"
        assert out.tr_switch.mode == "gpio" and out.atten.transport == "mock"
        assert out.pa_fault_in.enabled is True and out.source.endswith("+overrides")
        with pytest.raises(rf.ConfigError):
            rf.apply_overrides(cfg, {"nonsense.key": 1})
        with pytest.raises(rf.ConfigError):
            rf.apply_overrides(cfg, {"pa_enable": "x"})     # section, not scalar

    def test_default_path_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv(rf.CONFIG_ENV, str(tmp_path / "a.yaml"))
        assert rf.default_config_path() == str(tmp_path / "a.yaml")


# resolution
class TestResolution:
    def test_logical_name_then_fallback_then_offset(self):
        g = rf.MemoryGpio(names={"/dev/gpiochip0": {"PG.06": 41, "PN.01": 97}})  # no logical names
        fe, _ = mk(gpio_cfg("gpio"), gpio=g)
        st = fe.status()
        assert st["pa"]["offset"] == 41 and st["pa"]["by"] == "fallback"
        assert st["tr"]["offset"] == 97 and st["tr"]["by"] == "fallback"
        fe.close()
        g2 = rf.MemoryGpio()
        fe2, _ = mk(gpio_cfg("gpio", **{"pa_enable.line": 41, "tr_switch.line": "97"}), gpio=g2)
        st = fe2.status()
        assert st["pa"]["by"] == "offset" and st["tr"]["offset"] == 97
        fe2.close()
        fe3, _ = mk(gpio_cfg("gpio"))
        assert fe3.status()["pa"]["by"] == "name"
        assert fe3.status()["pa"]["chip"] == "/dev/gpiochip0"

    def test_unresolvable_line_fails_open_and_releases(self):
        g = rf.MemoryGpio(names={"/dev/gpiochip0": {}})
        fe = rf.MockFrontend(gpio_cfg("gpio"), gpio=g)
        with pytest.raises(rf.FrontendError) as ei:
            fe.open()
        assert "cannot resolve" in str(ei.value)
        assert g.lines == {} and fe.status()["open"] is False

    def test_offset_out_of_range(self):
        g = rf.MemoryGpio(num_lines=10)
        fe = rf.MockFrontend(gpio_cfg("proxy", **{"pa_enable.line": 500, "pa_enable.fallback_line": None}),
                             gpio=g)
        with pytest.raises(rf.FrontendError):
            fe.open()


# sequencing
class TestSequencing:
    def test_open_forces_pa_low(self):
        fe, _ = mk(gpio_cfg("gpio"))
        assert fe.pa_level is False and fe.tr_level is False
        assert fe.status()["pa"]["keyed"] is False

    def test_tx_on_order_and_timing(self):
        fe, clk = mk(gpio_cfg("gpio"))
        fe.sequence_tx_on()
        names = [c[1] for c in fe.calls]
        assert names == ["sequence_tx_on", "tr_switch", "pa_enable"]
        t_tr = next(c[0] for c in fe.calls if c[1] == "tr_switch")
        t_pa = next(c[0] for c in fe.calls if c[1] == "pa_enable")
        assert t_pa - t_tr == pytest.approx(0.001)          # tr settle 1 ms before PA
        assert clk.sleeps == [0.001, 0.002]                  # then PA settle 2 ms
        assert fe.pa_level is True and fe.tr_level is True
        # GPIO history: switch edge strictly before PA edge
        hist = [(o, v) for _, _, o, v in fe.gpio.history]
        assert hist[-2:] == [(97, True), (41, True)]

    def test_tx_off_burst_hang_then_pa_then_switch(self):
        fe, clk = mk(gpio_cfg("gpio"))
        fe.set_hold(False)
        fe.sequence_tx_on()
        clk.sleeps.clear(); fe.calls.clear()
        fe.sequence_tx_off()
        assert [c[1] for c in fe.calls] == ["sequence_tx_off", "pa_enable", "tr_switch"]
        assert clk.sleeps == [0.020, 0.002]                  # hang 20 ms, settle 2 ms
        assert fe.pa_level is False and fe.tr_level is False
        hist = [(o, v) for _, _, o, v in fe.gpio.history]
        assert hist[-2:] == [(41, False), (97, False)]       # PA off before switch: RX

    def test_tx_off_continuous_no_hang(self):
        fe, clk = mk(gpio_cfg("gpio"))
        fe.set_hold(True)
        fe.sequence_tx_on(); clk.sleeps.clear()
        fe.sequence_tx_off()
        assert clk.sleeps == [0.002]

    def test_idempotent(self):
        fe, _ = mk(gpio_cfg("gpio"))
        fe.sequence_tx_off()                                  # nothing keyed: no-op
        assert fe.gpio.history == []
        fe.sequence_tx_on(); n = len(fe.gpio.history)
        fe.sequence_tx_on(); fe.sequence_tx_on()
        assert len(fe.gpio.history) == n                      # no re-toggling
        fe.sequence_tx_off(); n = len(fe.gpio.history)
        fe.sequence_tx_off()
        assert len(fe.gpio.history) == n

    def test_proxy_mode_touches_no_switch_gpio(self):
        fe, _ = mk(gpio_cfg("proxy"))
        assert fe.tr_level is None
        fe.sequence_tx_on()
        assert fe.status()["tr"]["tx"] is True and fe.status()["tr"]["mode"] == "proxy"
        assert [o for _, _, o, _ in fe.gpio.history] == [41]  # only the PA line moved
        fe.sequence_tx_off()
        assert fe.status()["tr"]["tx"] is False

    def test_active_low_pa_and_tx_level_0(self):
        fe, _ = mk(gpio_cfg("gpio", **{"pa_enable.active_high": False, "tr_switch.tx_level": 0}))
        # MemoryGpio stores the logical "active" state; active_low is a request flag
        assert fe.status()["pa"]["active_high"] is False
        assert fe.tr_level is True                           # idle = RX = level 1
        fe.sequence_tx_on()
        assert fe.tr_level is False                          # TX = level 0
        fe.sequence_tx_off()
        assert fe.tr_level is True


# interlocks / fail-safe
class TestSafety:
    def test_interlock_pa_without_switch(self):
        fe, _ = mk(gpio_cfg("gpio"))
        with pytest.raises(rf.InterlockError):
            fe.pa_enable(True)
        assert fe.pa_level is False

    def test_interlock_switch_to_rx_while_keyed(self):
        fe, _ = mk(gpio_cfg("gpio"))
        fe.sequence_tx_on()
        with pytest.raises(rf.InterlockError):
            fe.tr_switch(False)
        assert fe.pa_level is True and fe.tr_level is True   # unchanged, PA still protected

    def test_gpio_write_failure_forces_safe(self):
        fe, _ = mk(gpio_cfg("gpio"))
        fe.sequence_tx_on()
        fe.gpio.fail_set = True
        with pytest.raises(rf.FrontendError):
            fe.sequence_tx_off()
        fe.gpio.fail_set = False
        assert fe.status()["pa"]["keyed"] is False and "failed" in fe.calls[-1][2]
        assert fe.last_error

    def test_tx_on_failure_mid_sequence_leaves_pa_low(self):
        fe, _ = mk(gpio_cfg("gpio"))
        orig = fe.gpio.lines[("/dev/gpiochip0", 41)].set_active

        def boom(active):
            if active:
                raise OSError("pa line dead")
            orig(active)
        fe.gpio.lines[("/dev/gpiochip0", 41)].set_active = boom
        with pytest.raises(rf.FrontendError):
            fe.sequence_tx_on()
        assert fe.pa_level is False and fe.tr_level is False  # switch returned to RX too

    def test_fault_input_refuses_keying_and_drops(self):
        fe, _ = mk(gpio_cfg("proxy", fault=True))
        assert fe.status()["fault"]["active"] is False
        fe.gpio.set_input("gpiochip1", 13, True)              # fault asserted
        with pytest.raises(rf.FrontendFault):
            fe.sequence_tx_on()
        assert fe.pa_level is False and fe.fault_trips == 1
        fe.gpio.set_input("gpiochip1", 13, False)
        fe.sequence_tx_on(); assert fe.pa_level is True
        fe.gpio.set_input("gpiochip1", 13, True)
        assert fe.watchdog_check() == "fault"
        assert fe.pa_level is False and fe.fault_trips == 2

    def test_close_and_safe_never_raise(self):
        fe, _ = mk(gpio_cfg("gpio"))
        fe.sequence_tx_on()
        fe.gpio.fail_set = True
        fe.safe("test")                                        # swallowed
        fe.close()
        assert fe.status()["open"] is False
        with pytest.raises(rf.FrontendError):
            fe.pa_enable(True)                                 # not open

    def test_status_shape(self):
        fe, _ = mk(gpio_cfg("gpio", fault=True))
        st = fe.status()
        for k in ("frontend", "open", "pa", "tr", "fault", "watchdog_ms", "watchdog_trips"):
            assert k in st
        assert "events" not in st and "configured" not in st["pa"]   # compact for the wire
        assert st["pa"]["line"] == "cpu-gp-out1" and st["pa"]["offset"] == 41
        assert st["fault"]["offset"] == 13 and st["fault"]["chip"] == "/dev/gpiochip1"
        full = fe.status(full=True)
        assert full["pa"]["configured"]["fallback_line"] == "PG.06" and "events" in full
        import json
        assert len(json.dumps(st)) < 1100                      # leaves room for the ack envelope


# watchdog
class TestWatchdog:
    def test_trips_without_ticks(self):
        fe, clk = mk(gpio_cfg("gpio"))
        fe.sequence_tx_on()
        assert fe.watchdog_check() is None
        clk.t += 0.4
        assert fe.watchdog_check() is None                    # 400 ms < 500 ms
        clk.t += 0.2
        assert fe.watchdog_check() == "watchdog"
        assert fe.pa_level is False and fe.watchdog_trips == 1
        assert fe.status()["pa"]["keyed"] is False

    def test_ticks_keep_it_alive(self):
        fe, clk = mk(gpio_cfg("gpio"))
        fe.sequence_tx_on()
        for _ in range(20):
            clk.t += 0.3; fe.tx_tick()
            assert fe.watchdog_check() is None
        assert fe.pa_level is True

    def test_long_single_shot_extends_deadline(self):
        fe, clk = mk(gpio_cfg("gpio"))
        fe.sequence_tx_on()
        fe.tx_tick(busy_s=5.0)                                # a 5 s waveform handed to the radio
        clk.t += 4.9
        assert fe.watchdog_check() is None
        clk.t += 0.7
        assert fe.watchdog_check() == "watchdog"

    def test_manual_hold_is_not_watchdog_timed(self):
        fe, clk = mk(gpio_cfg("proxy", fault=True))
        fe.pa_enable(True, manual=True)
        clk.t += 100.0
        assert fe.watchdog_check() is None and fe.pa_level is True
        assert fe.status()["watchdog_remaining_ms"] is None
        fe.gpio.set_input("gpiochip1", 13, True)              # fault still drops a manual hold
        assert fe.watchdog_check() == "fault" and fe.pa_level is False

    def test_disabled_when_zero(self):
        cfg = gpio_cfg("gpio"); cfg.watchdog_ms = 0
        fe, clk = mk(cfg)
        fe.sequence_tx_on(); clk.t += 1e6
        assert fe.watchdog_check() is None and fe.pa_level is True
        assert fe.status()["watchdog_remaining_ms"] is None
        assert fe._wd_thread is None                          # no thread when disabled

    def test_thread_drops_pa_for_real(self):
        cfg = gpio_cfg("gpio"); cfg.watchdog_ms = 60
        fe = rf.MockFrontend(cfg, watchdog_poll_s=0.01)      # real clock
        fe.open()
        fe.sequence_tx_on()
        assert fe.pa_level is True
        time.sleep(0.25)
        assert fe.pa_level is False and fe.watchdog_trips >= 1
        fe.close()
        assert fe._wd_thread is None


# reload
class TestReload:
    def test_reload_remaps_and_forces_low(self):
        fe, _ = mk(gpio_cfg("gpio"))
        fe.sequence_tx_on()
        new = gpio_cfg("gpio", **{"pa_enable.line": "cpu-gpio", "pa_enable.fallback_line": "PP.06"})
        fe.reload(new)
        assert fe.pa_level is False                           # new PA line idle
        st = fe.status()
        assert st["pa"]["offset"] == 118 and st["pa"]["line"] == "cpu-gpio"
        assert ("/dev/gpiochip0", 41) not in fe.gpio.lines   # old line released
        # forced low (PA then switch) before the old lines were released
        assert [(o, v) for _, _, o, v in fe.gpio.history[-2:]] == [(41, False), (97, False)]
        fe.sequence_tx_on()
        assert fe.gpio.level("gpiochip0", 118) is True

    def test_reload_bad_config_leaves_closed_and_safe(self):
        fe, _ = mk(gpio_cfg("gpio"))
        fe.sequence_tx_on()
        bad = gpio_cfg("gpio", **{"pa_enable.line": "no-such", "pa_enable.fallback_line": "nope"})
        with pytest.raises(rf.FrontendError):
            fe.reload(bad)
        assert fe.status()["open"] is False and fe.gpio.lines == {}


# sdr_core hooks
class TestSdrCoreHooks:
    @pytest.fixture
    def sdr(self):
        from dpmg.radio import sdr_core
        return sdr_core

    def test_no_frontend_is_noop(self, sdr):
        cfg = sdr.SDRConfig(sample_rate=1e6, center_freq=100e6)
        be = sdr.MockBackend(cfg); be.open()
        import numpy as np
        be.send(np.zeros(16, np.complex64)); be.stop_tx()
        assert be.health.tx_frames == 1 and getattr(be, "frontend", None) is None

    def test_mock_backend_keys_pa_around_tx(self, sdr):
        import numpy as np
        cfg = sdr.SDRConfig(sample_rate=1e6, center_freq=100e6)
        be = sdr.MockBackend(cfg); be.open()
        fe, _ = mk(gpio_cfg("gpio")); be.frontend = fe
        wave = np.zeros(1000, np.complex64)
        be.send(wave)
        assert fe.pa_level is True and fe.status()["watchdog_remaining_ms"] > 500  # tick + busy 1 ms
        be.send(wave)
        assert [c[1] for c in fe.calls].count("sequence_tx_on") == 1   # keyed once per run
        be.stop_tx()
        assert fe.pa_level is False and fe.tr_level is False
        be.send(wave)                                          # next run re-keys
        assert fe.pa_level is True
        be.stop_tx()

    def test_watchdog_fed_before_and_after_each_send(self, sdr):
        """A blocking send longer than watchdog_ms must not be dropped mid-burst: the
        deadline is extended by the send duration before the radio call."""
        import numpy as np
        cfg = sdr.SDRConfig(sample_rate=1e6, center_freq=100e6)
        be = sdr.MockBackend(cfg); be.open()
        fe, clk = mk(gpio_cfg("gpio")); be.frontend = fe
        ticks = []
        orig = fe.tx_tick
        fe.tx_tick = lambda busy_s=0.0: (ticks.append(busy_s), orig(busy_s))
        be.send(np.zeros(3_000_000, np.complex64))          # a 3 s send at 1 MSps
        assert ticks == [3.0, 3.0]                           # before + after
        assert fe.status()["watchdog_remaining_ms"] >= 3000
        clk.t += 2.9
        assert fe.watchdog_check() is None and fe.pa_level is True
        be.stop_tx()

    def test_scheduler_single_shot_keys_and_releases(self, sdr):
        import numpy as np
        cfg = sdr.SDRConfig(sample_rate=1e6, center_freq=100e6)
        be = sdr.MockBackend(cfg); be.open()
        fe, _ = mk(gpio_cfg("gpio")); be.frontend = fe
        sch = sdr.FullDuplexScheduler(be, tx_mode=sdr.TXMode.SINGLE_SHOT)
        sch.set_tx_waveform(np.zeros(2000, np.complex64))
        sch.start_tx()
        t0 = time.time()
        while sch._tx is not None and sch._tx.is_alive() and time.time() - t0 < 3:
            time.sleep(0.01)
        sch.stop_tx()
        names = [c[1] for c in fe.calls]
        assert names.index("sequence_tx_on") < names.index("sequence_tx_off")
        assert fe.pa_level is False and fe.status()["hold"] is False
        hist = [(o, v) for _, _, o, v in fe.gpio.history]
        assert hist == [(97, True), (41, True), (41, False), (97, False)]

    def test_scheduler_continuous_holds(self, sdr):
        import numpy as np
        cfg = sdr.SDRConfig(sample_rate=1e6, center_freq=100e6)
        be = sdr.MockBackend(cfg); be.open()
        fe, _ = mk(gpio_cfg("gpio")); be.frontend = fe
        sch = sdr.FullDuplexScheduler(be, tx_mode=sdr.TXMode.CONTINUOUS)
        sch.set_tx_waveform(np.zeros(2000, np.complex64))
        sch.start_tx()
        time.sleep(0.15)
        assert fe.status()["hold"] is True and fe.pa_level is True
        assert be.health.tx_frames > 1
        sch.stop_tx()
        assert fe.pa_level is False
        assert [c[1] for c in fe.calls].count("sequence_tx_on") == 1

    def test_fault_stops_tx_loop_without_hot_retry(self, sdr):
        import numpy as np
        cfg = sdr.SDRConfig(sample_rate=1e6, center_freq=100e6)
        be = sdr.MockBackend(cfg); be.open()
        fe, _ = mk(gpio_cfg("proxy", fault=True)); be.frontend = fe
        fe.gpio.set_input("gpiochip1", 13, True)              # PA fault asserted
        sch = sdr.FullDuplexScheduler(be, tx_mode=sdr.TXMode.CONTINUOUS)
        sch.set_tx_waveform(np.zeros(2000, np.complex64))
        sch.start_tx()
        time.sleep(0.3)
        assert be.health.tx_frames == 0                       # RF never started
        assert not sch._tx.is_alive()                         # loop exited, not retrying
        assert sch.metrics.underruns == 1 and "fault" in be.health.last_error
        assert fe.pa_level is False
        sch.stop_tx()


# T/R readback (ADC)
def rb_cfg(tr_mode="gpio", **rb_over):
    d = {"frontend": "mock",
         "pa_enable": {"line": "cpu-gp-out1", "fallback_line": "PG.06",
                       "settle_ms": 2, "hang_ms": 20},
         "tr_switch": {"mode": tr_mode, "line": "cpu-gp-out2", "fallback_line": "PN.01",
                       "tx_level": 1, "settle_ms": 1},
         "tr_readback": {"enabled": True, "transport": "mock", "settle_ms": 0,
                         "tx_window_v": [2.7, 5.5], "rx_window_v": [-0.5, 0.8]},
         "watchdog_ms": 500}
    d["tr_readback"].update(rb_over)
    return rf.config_from_dict(d, source="test")


class TestTrReadback:
    def test_config_parses_and_defaults_off(self):
        cfg = rf.FrontendConfig()
        assert cfg.tr_readback.enabled is False      # off unless asked for
        cfg = rb_cfg()
        assert cfg.tr_readback.enabled is True
        assert cfg.tr_readback.tx_window_v == [2.7, 5.5]
        assert cfg.tr_readback.rx_window_v == [-0.5, 0.8]

    def test_config_rejects_overlap_and_bad_window(self):
        with pytest.raises(rf.ConfigError):
            rb_cfg(tx_window_v=[1.0, 5.5], rx_window_v=[-0.5, 2.0])   # overlap
        with pytest.raises(rf.ConfigError):
            rb_cfg(tx_window_v=[5.5, 2.7])                            # lo above hi
        with pytest.raises(rf.ConfigError):
            rf.config_from_dict({"frontend": "mock", "tr_readback": {"nope": 1}})

    def test_classify(self):
        fe, _ = mk(rb_cfg())
        assert fe.classify_tr(3.3) == rf.TR_TX
        assert fe.classify_tr(0.0) == rf.TR_RX
        assert fe.classify_tr(2.5) == rf.TR_BAD        # half rail, neither window

    def test_key_allowed_in_tx_window(self):
        fe, _ = mk(rb_cfg())
        fe.set_tr_volts(3.3)
        fe.sequence_tx_on()
        assert fe.pa_level is True and fe.status()["pa"]["keyed"] is True

    def test_key_refused_in_dead_band(self):
        fe, _ = mk(rb_cfg())
        fe.set_tr_volts(2.5)                           # floating / half driven
        with pytest.raises(rf.FrontendFault):
            fe.sequence_tx_on()
        assert fe.pa_level is False                    # never keyed

    def test_key_refused_when_readback_is_rx(self):
        fe, _ = mk(rb_cfg())
        fe.set_tr_volts(0.0)                           # switch did not reach TX
        with pytest.raises(rf.InterlockError):
            fe.sequence_tx_on()
        assert fe.pa_level is False

    def test_proxy_mode_still_checked(self):
        # proxy mode does not enforce the digital switch interlock, but the ADC
        # readback still gates the key.
        fe, _ = mk(rb_cfg("proxy"))
        fe.set_tr_volts(0.0)
        with pytest.raises(rf.InterlockError):
            fe.sequence_tx_on()
        assert fe.pa_level is False

    def test_status_reports_state_and_volts(self):
        fe, _ = mk(rb_cfg())
        fe.set_tr_volts(3.3)
        rb = fe.status()["tr_readback"]
        assert rb == {"enabled": True, "state": rf.TR_TX, "volts": 3.3}

    def test_disabled_has_no_effect(self):
        fe, _ = mk(gpio_cfg("gpio"))                   # no tr_readback
        fe.sequence_tx_on()
        assert fe.pa_level is True
        assert "tr_readback" not in fe.status()
        assert fe._tr_adc is None


# heartbeat
def hb_cfg(**over):
    cfg = gpio_cfg("gpio", **over)
    cfg.pa_enable.mode = "heartbeat"
    cfg.pa_enable.heartbeat_hz = 200.0
    return cfg


class TestHeartbeat:
    """pa_enable.mode heartbeat: the PA line toggles while keyed and is steady
    (inactive) on every way out, so a permit board sees no edges unless the
    service is up and keying."""

    def _edges(self, fe, n=6):
        return [fe.heartbeat_step() for _ in range(n)]

    def test_config_parses_and_validates(self):
        d = gpio_cfg("gpio").to_dict()
        d.pop("source", None)
        d["pa_enable"].update({"mode": "heartbeat", "heartbeat_hz": 500})
        c2 = rf.config_from_dict(d, source="test")
        assert c2.pa_enable.mode == "heartbeat" and c2.pa_enable.heartbeat_hz == 500
        d["pa_enable"]["mode"] = "pulse"
        with pytest.raises(rf.ConfigError):
            rf.config_from_dict(d, source="test")
        d["pa_enable"].update({"mode": "heartbeat", "heartbeat_hz": 5})
        with pytest.raises(rf.ConfigError):
            rf.config_from_dict(d, source="test")
        assert gpio_cfg("gpio").pa_enable.mode == "level"

    def test_no_edges_until_keyed(self):
        fe, _ = mk(hb_cfg(), heartbeat_thread=False)
        assert self._edges(fe) == [False] * 6
        assert fe.pa_level is False and fe.hb_edges == 0

    def test_toggles_while_keyed(self):
        fe, clock = mk(hb_cfg(), heartbeat_thread=False)
        fe.sequence_tx_on()
        levels = []
        for _ in range(6):
            clock.t += 0.0025
            assert fe.heartbeat_step() is True
            levels.append(fe.pa_level)
        assert levels == [False, True, False, True, False, True]
        # the first gap includes the 2 ms PA settle after the key
        assert fe.hb_edges == 6 and 2.0 <= fe.hb_max_gap_ms <= 5.0

    def test_level_mode_never_toggles(self):
        fe, _ = mk(gpio_cfg("gpio"), heartbeat_thread=False)
        fe.sequence_tx_on()
        assert self._edges(fe) == [False] * 6 and fe.pa_level is True

    @pytest.mark.parametrize("how", ["tx_off", "safe", "close", "watchdog", "fault", "reload"])
    def test_every_exit_leaves_line_steady_inactive(self, how):
        cfg = hb_cfg(**({"pa_fault_in.enabled": True} if how == "fault" else {}))
        fe, clock = mk(cfg, heartbeat_thread=False)
        fe.sequence_tx_on()
        self._edges(fe, 3)
        if how == "tx_off":
            fe.sequence_tx_off()
        elif how == "safe":
            fe.safe("test")
        elif how == "close":
            fe.close()
        elif how == "watchdog":
            clock.t += 1.0
            assert fe.watchdog_check() == "watchdog"
        elif how == "fault":
            fe.gpio.set_input("gpiochip1", 13, True)   # fault asserted
            assert fe.watchdog_check() == "fault"
        elif how == "reload":
            fe.reload(hb_cfg())
        assert fe._keyed is False
        assert self._edges(fe) == [False] * 6           # no stray edge afterwards
        if how not in ("close",):
            assert fe.pa_level is False

    def test_write_failure_goes_safe(self):
        fe, _ = mk(hb_cfg(), heartbeat_thread=False)
        fe.sequence_tx_on()
        real = fe._pa.set_active
        calls = {"n": 0}

        def flaky(active):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("line gone")
            real(active)
        fe._pa.set_active = flaky
        assert fe.heartbeat_step() is False
        assert fe._keyed is False and "heartbeat" in (fe.last_error or "")

    def test_status_reports_heartbeat(self):
        fe, clock = mk(hb_cfg(), heartbeat_thread=False)
        fe.sequence_tx_on()
        clock.t += 0.0025
        fe.heartbeat_step()
        pa = fe.status()["pa"]
        assert pa["mode"] == "heartbeat" and pa["heartbeat_hz"] == 200.0
        assert pa["hb_edges"] == 1 and pa["hb_running"] is False   # no thread in this test
        assert "mode" not in mk(gpio_cfg("gpio"), heartbeat_thread=False)[0].status()["pa"]

    def test_thread_toggles_for_real_and_stops(self):
        cfg = hb_cfg(); cfg.watchdog_ms = 0
        fe = rf.MockFrontend(cfg)                           # real clock and thread
        fe.open()
        fe.sequence_tx_on()
        time.sleep(0.2)
        n = fe.hb_edges
        assert n >= 20, n                                  # 200 Hz for 0.2 s is ~80 edges
        assert fe.status()["pa"]["hb_running"] is True
        fe.sequence_tx_off()
        time.sleep(0.05)
        m = fe.hb_edges
        time.sleep(0.1)
        assert fe.hb_edges == m and fe.pa_level is False   # steady after unkey
        fe.close()
        assert fe._hb_thread is None


if __name__ == "__main__":  # OFF-01 loop runs `python3 tests/<file>.py`; delegate to pytest
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
