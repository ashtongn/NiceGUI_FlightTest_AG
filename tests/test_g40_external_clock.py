"""On a G40 an external 10 MHz is the carrier's HMC7044 on its input 1 and the cards
on carrier edge; the 1 PPS is line PCC.00 and the cards' external source. The service
does those steps at init and checks the 10 MHz by counting the card clock against the
1 PPS. Everything here runs on fakes; nothing touches a radio.

    python3 -m pytest -q tests/test_g40_external_clock.py
"""
import json
import os
import struct
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bench"))

from dpmg.radio import g40_clock as gc, sdr_core     # noqa: E402
from dpmg.radio.skiq_bridge import SkiqBridge        # noqa: E402

STOCK = ("# hmc_device_g40\nclkin_input_source = 0\nprint_debug_msg = 0\nclkin_cfg[0] = active\n"
         "clkin_cfg[1] = active, ac, term100\n")
HMC_OK = "SPI interface works: 1\nBoth PLLs locked: 1\nReturn Code: 0\n"
HMC_NOLOCK = "SPI interface works: 1\nBoth PLLs locked: 0\nReturn Code: 0\n"


# the choices

def test_five_g40_choices_and_their_hardware_meaning():
    assert gc.G40_CHOICES == ("internal", "gpsdo", "external", "ext_10mhz", "ext_pps")
    assert tuple(gc.CLOCK_CHOICES) == gc.G40_CHOICES + ("x40_gpsdo",)
    assert gc.resolve("external")["ref"] == "external" and gc.resolve("external")["pps"] == "external"
    assert gc.resolve("ext_10mhz")["pps"] == "internal"
    assert gc.resolve("ext_pps") == dict(gc.CLOCK_CHOICES["ext_pps"], token="ext_pps")
    assert gc.resolve("EXTERNAL")["token"] == "external"
    assert gc.labels("g40")["ext_pps"] == "1 PPS only (own oscillator)"
    assert list(gc.labels("x40")) == ["internal", "external", "x40_gpsdo"]


def test_unknown_and_x40_only_refusals_say_what_to_pick():
    assert "not one of internal, gpsdo, external, ext_10mhz, ext_pps, x40_gpsdo" in gc.resolve("carrier")["err"]
    e = gc.resolve("ext_pps", "x40")["err"]
    assert "X40 has no 1 PPS switch" in e and "external" in e and "internal" in e
    assert "err" not in gc.resolve("external", "x40")
    assert gc.label("nonsense") == "nonsense"


# the HMC7044 file and tool

def test_hmc_file_edit_changes_only_the_input_line():
    assert gc.hmc_file_input(STOCK) == 0
    out = gc.hmc_file_with_input(STOCK, 1)
    assert gc.hmc_file_input(out) == 1
    # the debug messages go on, or the tool prints no lock line (G40-0001, 8 November)
    assert "print_debug_msg = 1" in out
    assert out.replace("clkin_input_source = 1", "clkin_input_source = 0") \
              .replace("print_debug_msg = 1", "print_debug_msg = 0") == STOCK
    with pytest.raises(ValueError):
        gc.hmc_file_with_input("nothing here\n", 1)


def test_hmc_output_parsing():
    assert gc.parse_hmc_output(HMC_OK) == {"locked": True, "spi": True, "rc": 0}
    assert gc.parse_hmc_output(HMC_NOLOCK)["locked"] is False
    assert gc.parse_hmc_output("garbage") == {"locked": None, "spi": None, "rc": None}


def _files(tmp_path):
    stock = tmp_path / "hmc_device_g40.txt"
    stock.write_text(STOCK)
    return str(stock), str(tmp_path / "state.json")


def test_hmc_write_runs_the_tool_on_a_copy_and_records_the_lock(tmp_path):
    stock, state = _files(tmp_path)
    seen = {}

    def run(cmd, timeout):
        seen["cmd"] = cmd
        seen["text"] = open(cmd[1]).read()
        return 0, HMC_OK
    r = gc.hmc_write_input(1, run=run, tool="/usr/bin/hmc_from_file", stock_file=stock, state_file=state)
    assert r["ok"] and r["locked"] is True and r["input"] == 1 and "err" not in r
    assert seen["cmd"][0] == "/usr/bin/hmc_from_file" and seen["cmd"][1] != stock
    assert gc.hmc_file_input(seen["text"]) == 1
    assert not os.path.exists(seen["cmd"][1]), "the temp copy is removed"
    assert open(stock).read() == STOCK, "the boot file is never written"
    st = gc.hmc_state(state, stock)
    assert st["input"] == 1 and st["source"] == "written" and st["locked"] is True


def test_hmc_write_without_lock_is_not_ok(tmp_path):
    stock, state = _files(tmp_path)
    r = gc.hmc_write_input(1, run=lambda c, t: (0, HMC_NOLOCK), stock_file=stock, state_file=state)
    assert r["ok"] is False and r["locked"] is False and "err" not in r
    assert gc.hmc_state(state, stock)["locked"] is False


def test_hmc_write_reports_a_missing_tool_and_a_bad_file(tmp_path):
    stock, state = _files(tmp_path)
    r = gc.hmc_write_input(1, tool=str(tmp_path / "no_such_tool"), stock_file=stock, state_file=state)
    assert not r["ok"] and "not reachable" in r["err"] and "hmc_set_by_hand" in r["err"]
    r = gc.hmc_write_input(1, run=lambda c, t: (0, HMC_OK), stock_file=str(tmp_path / "missing.txt"),
                           state_file=state)
    assert not r["ok"] and "boot file" in r["err"]
    r = gc.hmc_write_input(1, run=lambda c, t: (3, "spi open failed"), stock_file=stock, state_file=state)
    assert not r["ok"] and "returned 3" in r["err"]
    r = gc.hmc_write_input(1, run=lambda c, t: (0, "GPIO Pin 0: Chip 0\nReturn Code: 0"), stock_file=stock,
                           state_file=state)
    assert not r["ok"] and r["locked"] is None and "no PLL lock report" in r["err"]


def test_hmc_state_falls_back_to_the_boot_file_after_a_reboot(tmp_path, monkeypatch):
    stock, state = _files(tmp_path)
    assert gc.hmc_state(state, stock) == {"input": 0, "source": "boot", "locked": None, "written_at": None}
    gc.hmc_write_input(1, run=lambda c, t: (0, HMC_OK), stock_file=stock, state_file=state)
    assert gc.hmc_state(state, stock)["source"] == "written"
    # the host rebooted after the write: the chip is back on the boot file
    monkeypatch.setattr(gc, "host_boot_time", lambda: time.time() + 10)
    assert gc.hmc_state(state, stock) == {"input": 0, "source": "boot", "locked": None, "written_at": None}
    assert gc.hmc_state(state, str(tmp_path / "none.txt"))["source"] == "unknown"


def test_by_hand_record_has_no_lock_word(tmp_path):
    stock, state = _files(tmp_path)
    gc.hmc_record_by_hand(1, state_file=state)
    st = gc.hmc_state(state, stock)
    assert st["input"] == 1 and st["locked"] is None and st["source"] == "written"


# the card clock against the 1 PPS

def test_clock_meter_counts_ticks_between_pulses():
    m = gc.ClockMeter(40e6)
    assert m.feed(None, 0.0)["pps_present"] is None
    assert m.feed(0, 0.5)["pps_present"] is None, "zero means no pulse yet"
    r = m.feed(1_000, 1.0)
    assert r["pps_present"] is True and r["ppm"] is None
    r = m.feed(1_000 + 40_000_000 - 2, 2.0)
    assert r["seconds"] == 1 and abs(r["ppm"] + 0.05) < 0.001
    # the same timestamp read again changes nothing
    assert m.feed(1_000 + 40_000_000 - 2, 2.5)["ppm"] == r["ppm"]
    # three pulses later, read once: the count is per second
    r = m.feed(1_000 + 40_000_000 - 2 + 3 * (40_000_000 - 5_468), 5.1)
    assert r["seconds"] == 3 and abs(r["ppm"] + 136.7) < 0.1


def test_clock_meter_sees_the_pulse_go_away_and_a_retimed_card():
    m = gc.ClockMeter()
    m.feed(100, 0.0)
    m.feed(100 + 40_000_000, 1.0)
    r = m.result(1.0 + gc.PPS_STALE_S + 0.1)
    assert r["pps_present"] is False and r["ppm"] is None and r["pps_age_s"] >= gc.PPS_STALE_S
    r = m.feed(50, 4.0)                  # timestamp went backwards
    assert r["pps_present"] is True and r["ppm"] is None


@pytest.mark.parametrize("choice,hmc,meas,want", [
    ("internal", {"input": 0}, {"ppm": 0.06}, None),
    ("ext_pps", {"input": 0}, {"ppm": 0.06}, None),
    ("external", {"input": 1, "locked": True}, {"ppm": -0.056, "pps_present": True}, "in use"),
    ("ext_10mhz", {"input": 1, "locked": True}, {"ppm": 2.0, "pps_present": True}, "in use"),
    ("external", {"input": 1, "locked": True}, {"ppm": -136.7, "pps_present": True}, "LOST"),
    ("external", {"input": 1, "locked": False}, {"ppm": -0.05, "pps_present": True}, "LOST"),
    ("external", {"input": 0}, {"ppm": -0.05, "pps_present": True}, "LOST"),
    ("external", {"input": 1, "locked": True}, {"ppm": None, "pps_present": False}, "not measured"),
    ("external", {"input": 1, "locked": True}, {"ppm": None, "pps_present": True}, "not measured"),
])
def test_ten_mhz_verdict(choice, hmc, meas, want):
    assert gc.ten_mhz_verdict(gc.resolve(choice), hmc, meas)["ten_mhz"] == want


def test_clock_text_for_the_chip():
    assert gc.clock_text({"choice": "external", "ten_mhz": "in use", "clock_ppm": -0.056,
                          "pps_present": True, "pps": "external"}) == \
        "external 10 MHz and 1 PPS, 10 MHz in use (-0.06 ppm), 1 PPS present (external)"
    assert "10 MHz LOST (-137 ppm)" in gc.clock_text({"choice": "external", "ten_mhz": "LOST", "clock_ppm": -136.7})
    assert "10 MHz not measured (no 1 PPS)" in gc.clock_text({"choice": "ext_10mhz", "ten_mhz": "not measured",
                                                              "pps_present": False})
    assert gc.clock_text({"choice": "ext_pps", "clock_ppm": 0.06, "pps_present": True, "pps": "external"}) == \
        "1 PPS only (own oscillator), clock +0.06 ppm vs 1 PPS, 1 PPS present (external)"
    assert gc.clock_text({}) == ""


# the 1 PPS line

class FakeGpio:
    def __init__(self):
        self.calls = []

    def open_line(self, chip, spec, fallback, direction, active_high, initial_active=False):
        self.calls.append((chip, spec, direction, active_high, initial_active))
        return SimpleNamespace(release=lambda: self.calls.append("released"))


def test_pps_line_low_for_external_and_high_for_internal():
    g = FakeGpio()
    gc.set_pps_line(True, backend=g)
    assert g.calls == [("gpiochip1", "PCC.00", "out", True, False), "released"]
    g = FakeGpio()
    gc.set_pps_line(False, backend=g)
    assert g.calls[0][4] is True


# the bridge

class FakeLib:
    def __init__(self, sel=3):
        self.sel = sel
        self.writes = []
        self.pps = 0
        self.pps_writes = []
        self.ts = 0

    def skiq_read_ref_clock_select(self, card, ptr):
        ptr._obj.value = self.sel
        return 0

    def skiq_write_ref_clock_select(self, card, v):
        self.writes.append(v)
        self.sel = v
        return 0

    def skiq_write_ext_ref_clock_freq(self, card, f):
        raise AssertionError("the card's external frequency must not be written for carrier")

    def skiq_gpsdo_disable(self, card):
        return 0

    def skiq_write_1pps_source(self, card, v):
        self.pps_writes.append((card, v))
        self.pps = v
        return 0

    def skiq_read_1pps_source(self, card, ptr):
        ptr._obj.value = self.pps
        return 0

    def skiq_read_sys_timestamp_freq(self, card, ptr):
        ptr._obj.value = 40_000_000
        return 0

    def skiq_read_last_1pps_timestamp(self, card, rf, sy):
        rf._obj.value = self.ts
        sy._obj.value = self.ts
        return 0


def _bridge(sel=3):
    b = SkiqBridge.__new__(SkiqBridge)
    b.lib = FakeLib(sel)
    b._chk = lambda rc, what: None
    return b


def test_bridge_leaves_a_card_already_on_carrier():
    b = _bridge(3)
    assert b.set_ref_clock(0, "carrier") == "carrier"
    assert b.lib.writes == []


def test_bridge_writes_carrier_edge_when_it_is_not_selected():
    b = _bridge(0)
    assert b.set_ref_clock(0, "carrier") == "carrier"
    assert b.lib.writes == [3]


def test_bridge_pps_source_external_is_zero_and_reads_back():
    b = _bridge()
    assert b.set_pps_source(1, "external") == "external"
    assert b.lib.pps_writes == [(1, 0)]
    assert b.sys_timestamp_freq(0) == 40_000_000
    b.lib.ts = 12345
    assert b.read_last_1pps(0) == (12345, 12345)
    b.lib.skiq_read_last_1pps_timestamp = lambda *a: -34
    assert b.read_last_1pps(0) is None


# the backend sequence

def _backend(kind="g40", clock="external", extras=None):
    be = sdr_core.EpiqBackend.__new__(sdr_core.EpiqBackend)
    be._kind = kind
    be.config = SimpleNamespace(clock_source=clock, extras=extras or {})
    be.health = SimpleNamespace(state=None)
    be._card = 0
    return be


class Steps:
    """Records what _prepare_g40_clock does, in order, on fakes."""

    def __init__(self, monkeypatch, state=None, lock=True, tool_err=None, pps_error=None):
        self.calls = []
        self.state = dict(state or {"input": 0, "source": "boot", "locked": None})
        self.lock = lock
        self.tool_err = tool_err
        self.pps_error = pps_error
        monkeypatch.setattr(sdr_core._g40_clock, "hmc_state", lambda *a, **k: dict(self.state))
        monkeypatch.setattr(sdr_core._g40_clock, "hmc_write_input", self.write)
        monkeypatch.setattr(sdr_core._g40_clock, "hmc_record_by_hand", self.by_hand)
        monkeypatch.setattr(sdr_core._g40_clock, "set_pps_line", self.pps)

    def write(self, inp, **k):
        self.calls.append(("hmc", inp))
        if self.tool_err:
            return {"ok": False, "locked": None, "input": inp, "err": self.tool_err, "output": ""}
        ok = self.lock or inp == 0
        self.state = {"input": inp, "source": "written", "locked": ok}
        return {"ok": ok, "locked": ok, "input": inp, "output": "Both PLLs locked: %d" % ok}

    def by_hand(self, inp, **k):
        self.calls.append(("by_hand", inp))
        self.state = {"input": inp, "source": "written", "locked": None}

    def pps(self, external):
        if self.pps_error:
            raise OSError(self.pps_error)
        self.calls.append(("pcc00", "low" if external else "high"))


def test_external_sets_hmc_input_1_then_the_pps_line_low(monkeypatch):
    st = Steps(monkeypatch)
    be = _backend()
    be._prepare_g40_clock("external")
    assert st.calls == [("hmc", 1), ("pcc00", "low")]
    assert be._ref_source("external") == "carrier"
    assert be._hmc["input"] == 1 and be._hmc["locked"] is True and be._pps_line == "external"


def test_external_10mhz_only_keeps_the_internal_pulse(monkeypatch):
    st = Steps(monkeypatch)
    be = _backend(clock="ext_10mhz")
    be._prepare_g40_clock("ext_10mhz")
    assert st.calls == [("hmc", 1), ("pcc00", "high")]
    assert be._ref_source("ext_10mhz") == "carrier"


def test_pps_only_keeps_the_own_oscillator_and_takes_the_chassis_pulse(monkeypatch):
    st = Steps(monkeypatch)
    be = _backend(clock="ext_pps")
    be._prepare_g40_clock("ext_pps")
    assert st.calls == [("pcc00", "low")], "the HMC7044 is already on input 0: nothing to write"
    assert be._ref_source("ext_pps") == "internal"


def test_internal_puts_the_hmc_back_when_it_was_on_the_ref_jack(monkeypatch):
    st = Steps(monkeypatch, state={"input": 1, "source": "written", "locked": True})
    be = _backend(clock="internal")
    be._prepare_g40_clock("internal")
    assert st.calls == [("hmc", 0), ("pcc00", "high")]
    st2 = Steps(monkeypatch, state={"input": None, "source": "unknown", "locked": None})
    _backend(clock="gpsdo")._prepare_g40_clock("gpsdo")
    assert st2.calls == [("hmc", 0), ("pcc00", "high")], "unknown state is written too"
    assert _backend(clock="gpsdo")._ref_source("gpsdo") == "gpsdo"


def test_external_refused_when_the_hmc_does_not_lock_and_is_put_back(monkeypatch):
    st = Steps(monkeypatch, lock=False)
    be = _backend()
    with pytest.raises(sdr_core.SDRConnectionError, match="did not lock to the 10 MHz"):
        be._prepare_g40_clock("external")
    assert st.calls == [("hmc", 1), ("hmc", 0)], "no PPS line write, chip back on input 0"
    assert be.health.state == sdr_core.SDRDeviceState.ERROR


def test_external_refused_when_the_tool_is_not_reachable(monkeypatch):
    Steps(monkeypatch, tool_err="/usr/bin/hmc_from_file is not reachable from the service")
    with pytest.raises(sdr_core.SDRConnectionError, match="not reachable"):
        _backend()._prepare_g40_clock("external")


def test_internal_goes_on_when_the_tool_is_not_reachable(monkeypatch):
    st = Steps(monkeypatch, state={"input": None, "source": "unknown", "locked": None}, tool_err="no tool")
    be = _backend(clock="internal")
    be._prepare_g40_clock("internal")
    assert st.calls == [("hmc", 0), ("pcc00", "high")]


def test_hmc_set_by_hand_skips_the_write(monkeypatch):
    st = Steps(monkeypatch)
    be = _backend(extras={"hmc_set_by_hand": True})
    be._prepare_g40_clock("external")
    assert st.calls == [("by_hand", 1), ("pcc00", "low")]
    assert be._hmc["locked"] is None


def test_unknown_token_and_x40_only_tokens_are_refused_before_anything_runs(monkeypatch):
    st = Steps(monkeypatch)
    with pytest.raises(sdr_core.SDRConnectionError, match="not one of"):
        _backend(clock="carrier")._prepare_g40_clock("carrier")
    with pytest.raises(sdr_core.SDRConnectionError, match="X40 has no 1 PPS switch"):
        _backend(kind="x40", clock="ext_pps")._prepare_g40_clock("ext_pps")
    assert st.calls == []


def test_x40_external_keeps_its_own_input_and_touches_nothing(monkeypatch):
    st = Steps(monkeypatch)
    be = _backend(kind="x40")
    be._prepare_g40_clock("external")
    assert st.calls == [] and be._ref_source("external") == "external"


def test_pps_line_failure_refuses_only_the_choices_that_need_the_chassis_pulse(monkeypatch):
    Steps(monkeypatch, pps_error="busy")
    with pytest.raises(sdr_core.SDRConnectionError, match="1 PPS line not set"):
        _backend(clock="ext_pps")._prepare_g40_clock("ext_pps")
    be = _backend(clock="ext_10mhz")
    be._prepare_g40_clock("ext_10mhz")
    assert be._pps_line is None


def test_pps_source_written_external_on_every_card_after_init(monkeypatch, capsys):
    be = _backend()
    be._skiq = _bridge()
    be._write_pps_source([0, 1])
    assert be._skiq.lib.pps_writes == [(0, 0), (1, 0)]
    be._skiq.lib.skiq_write_1pps_source = None
    be._write_pps_source([0])            # absent API: logged, not fatal
    assert "1 PPS source" in capsys.readouterr().out


# the status the service and the GUI read

def _measured(monkeypatch, ts_list):
    """A backend on external with the HMC on input 1, fed a list of last-1PPS timestamps."""
    Steps(monkeypatch)
    be = _backend()
    be._prepare_g40_clock("external")
    be._skiq = _bridge()
    be._hw_lock = __import__("threading").Lock()
    out = []
    for ts in ts_list:
        be._skiq.lib.ts = ts
        out.append(be.clock_measure())
    return be, out


def test_clock_status_in_use_then_lost(monkeypatch):
    be, out = _measured(monkeypatch, [0, 1000, 1000 + 40_000_000 - 2])
    assert out[0]["ten_mhz"] == "not measured" and out[0]["pps_present"] is None
    assert out[2]["ten_mhz"] == "in use" and abs(out[2]["clock_ppm"] + 0.05) < 0.01
    assert be.ref_locked() is True
    st = {"ref_clock": "carrier", "ext_ref_present": False, "pps_present": None}
    be._add_clock_status(st)
    assert st["ext_ref_present"] is True and st["ref_clock"] == "external (carrier)"
    assert st["pps_present"] is True and st["clock"]["choice"] == "external" and st["ten_mhz"] == "in use"
    be._skiq.lib.ts = 1000 + 2 * 40_000_000 - 2 - 5_468
    assert be.clock_measure()["ten_mhz"] == "LOST"
    assert be.ref_locked() is False


def test_clock_status_with_no_pulse(monkeypatch):
    be, out = _measured(monkeypatch, [5000])
    be._meter.last_t -= gc.PPS_STALE_S + 1
    cs = be.clock_status()
    assert cs["pps_present"] is False and cs["ten_mhz"] == "not measured" and "no 1 PPS" in cs["why"]
    assert be.ref_locked() is None


def test_ref_locked_by_choice(monkeypatch):
    Steps(monkeypatch)
    be = _backend(clock="ext_pps")
    be._prepare_g40_clock("ext_pps")
    assert be.ref_locked() is None
    be = _backend(clock="gpsdo")
    be._prepare_g40_clock("gpsdo")
    be.gpsdo_locked = lambda: True
    assert be.ref_locked() is True


def test_caps_list_the_choices():
    caps = sdr_core.backend_caps("g40") if hasattr(sdr_core, "backend_caps") else None
    if caps is None:
        pytest.skip("no caps helper by that name")
    assert caps["clock_sources"] == list(gc.G40_CHOICES) and caps["clock_labels"]["ext_pps"]


# the bench gate

def _gate(monkeypatch, r):
    import checkout_common as cc
    from dpmg.link.proto import CmdStatus
    monkeypatch.setattr(cc, "gps_status", lambda link: (CmdStatus.OK, r))
    return cc


def test_gate_passes_with_the_10mhz_in_use_and_the_line_external(monkeypatch):
    cc = _gate(monkeypatch, {"ext_ref_present": True, "pps_present": True, "pps_line": "external",
                             "ref_clock": "external (carrier)", "ten_mhz": "in use", "clock_ppm": -0.056})
    rec = cc.wait_ext_ref(None, timeout=0.01, poll_s=0.01)
    assert rec["locked"] and rec["pps_line"] == "external"
    assert "ext_pps" in cc.CLOCKS and "ext_10mhz" in cc.CLOCKS


@pytest.mark.parametrize("r", [
    {"ext_ref_present": True, "pps_present": True, "pps_line": "internal"},
    {"ext_ref_present": None, "pps_present": True, "pps_line": "external"},
    {"ext_ref_present": False, "pps_present": True, "pps_line": "external", "ten_mhz": "LOST"},
    {"ext_ref_present": True, "pps_present": False, "pps_line": "external"},
])
def test_gate_refuses_the_rest(monkeypatch, r):
    cc = _gate(monkeypatch, r)
    with pytest.raises(cc.GpsLockError, match="Refusing to key"):
        cc.wait_ext_ref(None, timeout=0.01, poll_s=0.01)


# the GPS module readers, kept for the tools

def smgr(flags):
    return gc.ubx_frame(*gc.CFG_SMGR, bytes(16) + struct.pack("<I", flags))


def mon(src, ext_int1=1):
    return gc.ubx_frame(*gc.MON_SMGR, bytes(12) + bytes([src, 0, 0, ext_int1]))


def test_frame_checksum_matches_a_hand_worked_poll():
    assert gc.ubx_frame(0x06, 0x62) == bytes.fromhex("b5620662" "0000" "683e")


def test_find_ubx_skips_nmea_and_bad_frames():
    good = smgr(gc.SMGR_FLAGS_EXT10M)
    bad = bytearray(good)
    bad[-1] ^= 0xFF
    buf = b"$GNRMC,,V*00\r\n" + bytes(bad) + b"$GNGGA\r\n" + good
    assert gc.find_ubx(buf, *gc.CFG_SMGR) == good[6:-2]
    assert gc.find_ubx(b"$GNRMC\r\n", *gc.CFG_SMGR) is None


@pytest.mark.parametrize("data,mode,src,locked", [
    (smgr(gc.SMGR_FLAGS_EXT10M) + mon(3), "ext10M", "EXTINT1", True),
    (smgr(gc.SMGR_FLAGS_INTERNAL), "internal", None, False),
    (b"$GNRMC\r\n", None, None, None),
])
def test_read_carrier(data, mode, src, locked):
    st = gc.read_carrier(poll=lambda port, frames, want, timeout_s: data)
    assert (st["mode"], st["disc_src"], st["locked"]) == (mode, src, locked)


def test_read_carrier_never_raises():
    def boom(*a):
        raise OSError("no such port")
    assert gc.read_carrier(poll=boom)["locked"] is None


# the status line words in the two GUI views

def test_status_line_words_follow_the_clock_check():
    pytest.importorskip("nicegui")
    from dpmg.web import link_tab as lt
    lost = {"clock": {"choice": "external", "ten_mhz": "LOST", "clock_ppm": -136.7}}
    ok = {"clock": {"choice": "external", "ten_mhz": "in use", "clock_ppm": -0.056}}
    pps = {"clock": {"choice": "ext_pps", "ten_mhz": None, "clock_ppm": 0.06}}
    assert lt.ref_words("external", False, lost) == ", 10 MHz LOST (-137 ppm)"
    assert lt.ref_words("external", True, ok) == ", 10 MHz in use (-0.06 ppm)"
    assert lt.ref_words("ext_pps", False, pps) == ", clock +0.06 ppm vs 1 PPS"
    assert lt.ref_words("gpsdo", False, {"clock": {"choice": "gpsdo"}}) == ", GPSDO acquiring, GPS fix not read"
    # an older service with no clock dict: the old words
    assert lt.ref_words("external", False, {}) == ", external ref not confirmed"
    assert lt.ref_words("internal", False, {}) == ""
    assert lt.ref_state("external", False, lost) == "lost"
    assert lt.ref_state("external", True, ok) == "locked"
    assert lt.ref_state("ext_pps", False, pps) == "neutral"
    assert lt.ref_state("gpsdo", False, {}) == "acquiring"
