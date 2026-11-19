"""The X40 clock choices and the GNSS fix next to every GPSDO lock.

An X40 has no FPGA GPSDO: skiq_gpsdo_enable returns -95 on it by design (Sidekiq API
4.25.0-1 p26 and p274). Its GPSDO is in its u-blox module and libsidekiq reaches it as
the host reference clock (SDM 4.25.0-1 p58), which is the x40_gpsdo choice. gpsdo on an
X40 is refused before anything is torn down, in the service and in Check plan.

On a G40 a GPSDO lock with the 1 PPS source on external says nothing about a fix (SDM
p59), so every place that shows the lock shows the fix too, amber when there is none.
Everything here runs on fakes and the simulator; nothing touches a radio.

    python3 -m pytest -q tests/test_x40_clock_gps_fix.py
"""
import asyncio
import csv
import glob
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dpmg.link import proto                                      # noqa: E402
from dpmg.link.proto import CmdStatus, HostType                  # noqa: E402
from dpmg.radio import g40_clock as gc, gps_antenna as ga, sdr_core  # noqa: E402
from dpmg.radio.dpmg_service import RadioService                 # noqa: E402
from dpmg.radio.skiq_bridge import SkiqBridge                    # noqa: E402

FIX3D = {"fix_mode": 3, "fix": "3D", "sats_visible": 21, "sats_used": 19}
NOFIX = {"fix_mode": 1, "fix": "none", "sats_visible": 1, "sats_used": 0}


# the choices

def test_x40_lists_only_what_it_can_run():
    assert gc.X40_CHOICES == ("internal", "external", "x40_gpsdo")
    assert list(gc.labels("x40")) == ["internal", "external", "x40_gpsdo"]
    assert "x40_gpsdo" not in gc.labels("g40") and "gpsdo" in gc.labels("g40")
    assert gc.resolve("x40_gpsdo", "x40")["ref"] == "host"


def test_gpsdo_on_an_x40_is_refused_with_what_to_pick():
    e = gc.resolve("gpsdo", "x40")["err"]
    assert "-95" in e and "x40_gpsdo" in e and "external" in e and "internal" in e
    assert "BSP v3.0.0" in e
    e = gc.resolve("x40_gpsdo", "g40")["err"]
    assert "X40 choice" in e and "gpsdo" in e


def test_caps_for_each_kind():
    x = sdr_core.backend_caps("x40")
    assert x["clock_sources"] == ["internal", "external", "x40_gpsdo"]
    assert "gpsdo" not in x["time_sources"] and x["has_gpsdo"] is False
    g = sdr_core.backend_caps("g40")
    assert g["clock_sources"] == list(gc.G40_CHOICES) and g["has_gpsdo"] is True


# the verdict

def test_fix_text():
    assert gc.fix_text(FIX3D) == "GPS fix 3D, 19 sats"
    assert gc.fix_text({"fix": "2D", "sats_used": 4}) == "GPS fix 2D, 4 sats"
    assert gc.fix_text(NOFIX) == "no GPS fix"
    assert gc.fix_text(None) == "GPS fix not read"


def test_gpsdo_verdict():
    v = gc.gpsdo_verdict("gpsdo", True, FIX3D)
    assert v == {"state": "locked", "text": "GPSDO locked, GPS fix 3D, 19 sats"}
    v = gc.gpsdo_verdict("gpsdo", True, NOFIX)
    assert v == {"state": "no_fix", "text": "locked to 1 PPS, no GPS fix"}
    assert gc.gpsdo_verdict("gpsdo", True, None)["state"] == "no_fix"
    assert gc.gpsdo_verdict("gpsdo", False, FIX3D)["state"] == "acquiring"
    assert gc.gpsdo_verdict("gpsdo", None, FIX3D)["state"] is None
    assert gc.gpsdo_verdict("internal", True, FIX3D)["state"] is None
    v = gc.gpsdo_verdict("host", None, FIX3D)
    assert v["state"] == "unread" and "lock not readable" in v["text"] and "3D" in v["text"]
    v = gc.gpsdo_verdict("host", None, NOFIX)
    assert v["state"] == "no_fix" and "no GPS fix" in v["text"]


# the backend and the bridge

class FakeLib:
    def __init__(self, sel=0):
        self.sel = sel
        self.writes = []
        self.gpsdo_enables = 0

    def skiq_read_ref_clock_select(self, card, ptr):
        ptr._obj.value = self.sel
        return 0

    def skiq_write_ref_clock_select(self, card, v):
        self.writes.append(v)
        self.sel = v
        return 0

    def skiq_gpsdo_enable(self, card):
        self.gpsdo_enables += 1
        return -95

    def skiq_gpsdo_disable(self, card):
        return -95


def _backend(kind="x40", clock="x40_gpsdo"):
    be = sdr_core.EpiqBackend.__new__(sdr_core.EpiqBackend)
    be._kind = kind
    be.config = SimpleNamespace(clock_source=clock, extras={})
    be.health = SimpleNamespace(state=None)
    be._card = 0
    return be


def test_x40_gpsdo_selects_the_host_reference_and_never_the_fpga_gpsdo():
    be = _backend()
    be._prepare_g40_clock("x40_gpsdo")
    assert be._ref_source("x40_gpsdo") == "host"
    assert be._ref_source("external") == "external" and be._ref_source("internal") == "internal"
    b = SkiqBridge.__new__(SkiqBridge)
    b.lib = FakeLib()
    b._chk = lambda rc, what: None
    assert b.set_ref_clock(0, be._ref_source("x40_gpsdo")) == "host"
    assert b.lib.writes == [2] and b.lib.gpsdo_enables == 0


def test_gpsdo_on_an_x40_backend_is_refused_before_the_card_opens():
    with pytest.raises(sdr_core.SDRConnectionError, match="x40_gpsdo"):
        _backend(clock="gpsdo")._prepare_g40_clock("gpsdo")


def test_x40_lock_is_unknown_and_status_says_why():
    be = _backend()
    be._prepare_g40_clock("x40_gpsdo")
    assert be.ref_locked() is None
    be._skiq = SimpleNamespace(read_gps_status=lambda card: {"ref_clock": "host", "gpsdo_locked": None})
    st = be.gps_status()
    assert st["clock"]["choice"] == "x40_gpsdo" and st["clock"]["ref"] == "host"
    assert st["gpsdo_locked"] is None and "cannot be read" in st["gpsdo_note"]


def test_x40_external_gets_no_hmc_verdict():
    # the G40 verdict would say LOST for want of the HMC7044's input 1
    be = _backend(clock="external")
    be._prepare_g40_clock("external")
    cs = be.clock_status()
    assert cs["ten_mhz"] is None and cs["choice"] == "external"


# the service (simulator)

def _svc(kind, host_type, port=47870):
    return RadioService(serial=f"{kind.upper()}-CLKSIM", host_type=host_type, kind=kind,
                        simulate=True, cmd_port=port, discovery_port=port + 1,
                        broadcast="127.0.0.1")


def _x40_init(clock):
    return {"kind": "x40", "sample_rate": 50_000_000, "center_freq": 915_000_000,
            "rx_gain": 245, "clock_source": clock, "rx_antenna": "RXB1",
            "extras": {"card": 0}}


async def _x40_service():
    svc = _svc("x40", HostType.X40_TX)
    svc.loop = asyncio.get_running_loop()
    a = await svc._cmd_init(1, _x40_init("internal"))
    assert a.status == CmdStatus.OK, a.result
    be = svc.backend
    a = await svc._cmd_init(2, _x40_init("gpsdo"))
    assert a.status == CmdStatus.REJECTED and "x40_gpsdo" in a.result["err"], a.result
    assert svc.backend is be, "the running radio is left alone"
    a = await svc._cmd_init(3, _x40_init("x40_gpsdo"))
    assert a.status == CmdStatus.OK, a.result
    r = (await svc._cmd_gps_status(4, {})).result
    assert r["ref_clock"] == "host" and r["gpsdo_locked"] is None, r
    assert r["clock"]["choice"] == "x40_gpsdo" and r["gnss"]["fix"] == "3D"
    assert r["ant_bias"] is None and "ZynqMP" in r["ant_bias_err"]
    a = await svc._apply(proto.Cmd(5, "gps_antenna_bias", {"enable": True}))
    assert a.status == CmdStatus.REJECTED and "ZynqMP" in a.result["err"]
    cfg = (await svc._cmd_get_config(6, {})).result
    assert cfg["clock_source"] == "x40_gpsdo" and cfg["gnss"] == {"fix": "3D", "sats_used": 16}
    assert cfg["ref_locked"] is None and cfg["ant_bias"] is None
    assert len(a.pack()) < proto.MAX_DATAGRAM
    await svc._cmd_close(7)


def test_x40_service_refuses_gpsdo_and_runs_x40_gpsdo():
    asyncio.run(_x40_service())


async def _g40_no_fix(monkeypatch):
    monkeypatch.setenv("DPMG_SIM_NO_GPS_FIX", "1")
    svc = _svc("g40", HostType.G40_RX, port=47874)
    svc.loop = asyncio.get_running_loop()
    svc._gps_sim = ga.SimGpsAntenna(bias_on=True)
    a = await svc._cmd_init(1, {"kind": "g40", "sample_rate": 1_920_000, "center_freq": 915_000_000,
                                "rx_gain": 245, "clock_source": "gpsdo", "rx_antenna": "RXA1",
                                "extras": {"card": 0, "chan_mode": "single"}})
    assert a.status == CmdStatus.OK, a.result
    r = (await svc._cmd_gps_status(2, {})).result
    assert r["gpsdo_locked"] is True and r["gnss"]["fix"] == "none" and r["gnss"]["sats_used"] == 0
    cfg = (await svc._cmd_get_config(3, {})).result
    assert cfg["gnss"] == {"fix": "none", "sats_used": 0}
    v = gc.gpsdo_verdict("gpsdo", r["gpsdo_locked"], r["gnss"])
    assert v["state"] == "no_fix" and v["text"] == "locked to 1 PPS, no GPS fix"
    await svc._cmd_close(4)


def test_g40_sim_locked_with_no_fix(monkeypatch):
    asyncio.run(_g40_no_fix(monkeypatch))


def test_sim_antenna_no_fix_still_locks():
    t = [0.0]
    sim = ga.SimGpsAntenna(clock=lambda: t[0], no_fix=True)
    sim.set_bias(True)
    t[0] = 100.0
    assert sim.gpsdo_locked() and sim.gnss()["fix"] == "none"
    assert ga.SimGpsAntenna(clock=lambda: 0.0, bias_on=True, no_fix=False).bias is True


# the GUI words and colours

def test_status_line_and_colour_carry_the_fix():
    pytest.importorskip("nicegui")
    from dpmg.web import link_tab as lt
    ok = {"clock": {"choice": "gpsdo"}, "gnss": {"fix": "3D", "sats_used": 17}}
    nofix = {"clock": {"choice": "gpsdo"}, "gnss": {"fix": "none", "sats_used": 0}}
    assert lt.ref_words("gpsdo", True, ok) == ", GPSDO locked, GPS fix 3D, 17 sats"
    assert lt.ref_state("gpsdo", True, ok) == "locked"
    assert lt.ref_words("gpsdo", True, nofix) == ", locked to 1 PPS, no GPS fix"
    assert lt.ref_state("gpsdo", True, nofix) == "no_fix"
    # an older service sends no gnss: never green on the lock alone
    assert lt.ref_state("gpsdo", True, {}) == "no_fix"
    x = {"clock": {"choice": "x40_gpsdo"}, "gnss": {"fix": "3D", "sats_used": 19}}
    assert lt.ref_words("x40_gpsdo", False, x) == ", X40 GPSDO, GPS fix 3D, 19 sats, lock not readable"
    assert lt.ref_state("x40_gpsdo", False, x) == "neutral"
    assert lt.ref_state("x40_gpsdo", False, {"gnss": None}) == "no_fix"
    assert lt.GPS_CLOCKS == ("gpsdo", "x40_gpsdo")


# Check plan and Run mission

def _plan(rtype, clock):
    return {"name": "t", "radios": [{"id": "R1", "type": rtype, "clock_source": clock}], "steps": []}


def test_check_plan_fails_an_x40_on_gpsdo():
    from dpmg.mission import mission_file as mf
    errs = [c for c in mf.validate(_plan("x40", "gpsdo")) if c.level == "error"]
    assert len(errs) == 1 and "radio R1" in errs[0].message and "x40_gpsdo" in errs[0].message
    assert not [c for c in mf.validate(_plan("x40", "x40_gpsdo")) if "clock source" in c.message]
    assert [c for c in mf.validate(_plan("g40", "x40_gpsdo")) if "X40 choice" in c.message]
    assert not [c for c in mf.validate(_plan("g40", "gpsdo")) if "clock source" in c.message]
    assert not [c for c in mf.validate(_plan("b200", "gpsdo")) if "clock source" in c.message]


def test_bring_up_refuses_an_x40_on_gpsdo():
    from dpmg.mission import mission_runner as mr
    steps = [{"radio": "R1", "params": {"center_hz": 915e6}}]
    assert "x40_gpsdo" in mr.bring_up_plan({"id": "R1", "type": "x40", "clock_source": "gpsdo"}, steps)["err"]


# the health log

def test_health_log_carries_the_fix(tmp_path):
    from dpmg.link import ground_log as gl
    host = SimpleNamespace(serial="G40-T", connected=True, detections={}, det_head=0, watch_bands=[],
                           config={"clock_source": "gpsdo", "clock": {"choice": "gpsdo"},
                                   "gnss": {"fix": "none", "sats_used": 0}},
                           status=SimpleNamespace(radio_state=2, flags=proto.Status.FLAG_REF_LOCKED,
                                                  overruns=0, underruns=0, fps=30, uptime_s=9))
    glogs = gl.GroundLogs(lambda: {"G40-T": host}, lambda: None, lambda s: "STREAMING",
                          out_dir=str(tmp_path))
    glogs.get("G40-T").start("ground", {"health": True}, 0, now=1_790_000_000.0)
    glogs.tick(now=1_790_000_000.0)
    f = sorted(glob.glob(str(tmp_path / "health_G40-T_*.csv")))[-1]
    rows = list(csv.reader(open(f, newline="", encoding="utf-8")))
    assert rows[0][-3:] == ["gps_fix", "gps_sats", "gpsdo"]
    assert rows[1][-7:-3] == ["gpsdo", "1", "", ""]
    assert rows[1][-3:] == ["none", "0", "no_fix"]
