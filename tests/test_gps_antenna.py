#!/usr/bin/env python3
"""
G40 GPS antenna 3.3 V bias + GNSS sky status
==========================================================
  1. PCA9685 channel-4 register decode (ant-bias-en, active-low): ON_H full-on
     = pin high = bias off; all-zero or OFF_H full-off = pin low = bias on; a
     PWM duty = unknown.  The write sequence decodes back to the request.
  2. set_bias against a fake PCA9685: on and off both stick, the line check refuses
     the write (and still reads back), a write error is reported, never raised.
  3. gpsd JSON parse: lines captured from the G40's gpsd 3.22 (3D fix,
     location fields stripped), a synthetic no-antenna sky (ss=0, no fix),
     garbage lines, and an unreachable gpsd: None.
  4. Simulator: bias defaults off; enable then fix ~20 s later then GPSDO lock.
  5. Real RadioService: gps_antenna_bias on the sim (mock + g40 --simulate),
     the real-g40 path with gps_antenna patched, REJECTED on x40, bad params,
     gps_status carries ant_bias/gnss before and after init.
  6. Ack size: worst-case gps_status / gps_antenna_bias acks < 1400 B.
  7. GUI hint: bias off + (0 heard or best C/N0 < 20 dB-Hz) for 2 min.

    python3 tests/test_gps_antenna.py     (exit 0 = all pass; pytest works too)
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root
from dpmg.radio import gps_antenna as ga
from dpmg.link import proto
from dpmg.link.proto import CmdAck, CmdStatus, HostType
from dpmg.radio.dpmg_service import RadioService

HERE = os.path.dirname(os.path.abspath(__file__))
FIX_LINES = os.path.join(HERE, "data", "gpsd_g40_fix.jsonl")


class FakePCA:
    """In-memory PCA9685 register file shared by every 'open' (like the chip)."""
    regs: dict = {}
    fail_write = False

    def __init__(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def read_reg(self, r):
        return FakePCA.regs.get(r, 0)

    def write_reg(self, r, v):
        if FakePCA.fail_write:
            raise OSError(121, "Remote I/O error")
        FakePCA.regs[r] = v


def _boot_state():                    # what the G40 boots with: ON_H full-on (bias off)
    FakePCA.regs = {0x16: 0, 0x17: 0x10, 0x18: 0, 0x19: 0}
    FakePCA.fail_write = False


# 1 ---------------------------------------------------------------------------

def test_decode():
    assert ga.led_regs(4) == (0x16, 0x17, 0x18, 0x19)
    assert ga.decode_bias(0, 0x10, 0, 0) is False        # pin HIGH (G40 boot)
    assert ga.decode_bias(0, 0, 0, 0) is True            # pin LOW (after gpioset =1)
    assert ga.decode_bias(0, 0, 0, 0x10) is True         # full-OFF (newer driver form)
    assert ga.decode_bias(0, 0x10, 0, 0x10) is True      # full-OFF wins over full-ON
    assert ga.decode_bias(0x00, 0x01, 0x00, 0x08) is None  # a PWM duty
    for en in (True, False):
        regs = {0x16: 0x33, 0x17: 0x05, 0x18: 0x44, 0x19: 0x17}   # arbitrary PWM state
        for r, v in ga.bias_write_seq(en):
            regs[r] = v
        assert ga.decode_bias(*(regs[r] for r in ga.led_regs())) is en, en


# 2 ---------------------------------------------------------------------------

def test_set_bias_fake_chip():
    _boot_state()
    ok = lambda: None                                    # noqa: E731  line check passes
    assert ga.read_bias(FakePCA) == {"ant_bias": False, "raw": "00 10 00 00"}
    r = ga.set_bias(True, FakePCA, ok)
    assert r["ok"] and r["ant_bias"] is True and r["raw"] == "00 00 00 00", r
    r = ga.set_bias(False, FakePCA, ok)
    assert r["ok"] and r["ant_bias"] is False and r["raw"] == "00 10 00 00", r
    # refused: nothing written, readback still reported
    r = ga.set_bias(True, FakePCA, lambda: "gpiochip2 line 4 is 'x', not 'ant-bias-en'")
    assert not r["ok"] and r["ant_bias"] is False and r["err"].startswith("refused"), r
    # bus error on write: reported, not raised
    FakePCA.fail_write = True
    r = ga.set_bias(True, FakePCA, ok)
    assert not r["ok"] and "bias write" in r["err"] and r["ant_bias"] is False, r
    # no i2c device at all (dev box): read fails soft
    def _nodev():
        raise FileNotFoundError(2, "No such file", "/dev/i2c-1")
    r = ga.read_bias(_nodev)
    assert r["ant_bias"] is None and "bias read" in r["err"], r
    r = ga.set_bias(True, _nodev, ok)
    assert r["ok"] is False and r["ant_bias"] is None
    # the default line check on a box without the PCA9685 refuses
    assert ga._line_check(sys_root=os.path.join(HERE, "no-such-sysfs")) is not None


# 3 ---------------------------------------------------------------------------

def test_parse_captured_fix():
    with open(FIX_LINES) as f:
        lines = f.readlines()
    g = ga.parse_gpsd(lines)
    assert g["fix_mode"] == 3 and g["fix"] == "3D", g
    assert g["sats_visible"] == 18 and g["sats_used"] == 18, g   # 23 in view, 18 heard
    assert g["cno_max"] == 44.0 and 25 < g["cno_median"] < 44, g
    assert g["time_utc"].startswith("2026-10-19T00:36"), g
    # the POLL line alone carries the same answer
    poll = [ln for ln in lines if '"class":"POLL"' in ln]
    gp = ga.parse_gpsd(poll)
    assert gp["fix"] == "3D" and gp["sats_used"] == 18, gp
    # bytes lines (socket path) parse the same
    assert ga.parse_gpsd([ln.encode() for ln in lines]) == g


def test_parse_no_antenna_and_garbage():
    # synthetic: active antenna unpowered: almanac sats listed with ss 0, no fix
    lines = [
        '{"class":"VERSION","release":"3.22"}',
        'not json at all',
        '{"class":"TPV","device":"/dev/epiq-gps-uart","mode":1,"time":"2013-01-01T00:00:05.000Z"}',
        '{"class":"SKY","device":"/dev/epiq-gps-uart","hdop":99.9}',             # DOP-only
        '{"class":"SKY","device":"/dev/epiq-gps-uart","nSat":6,"uSat":0,"satellites":['
        + ",".join('{"PRN":%d,"ss":0.0,"used":false}' % p for p in range(1, 7)) + "]}",
        '{"class":"SKY","device":"/dev/epiq-gps-uart","hdop":99.9}',             # must not wipe sats
        '{broken',
    ]
    g = ga.parse_gpsd(lines)
    assert g["fix"] == "none" and g["fix_mode"] == 1, g
    assert g["sats_visible"] == 0 and g["sats_used"] == 0, g
    assert g["cno_max"] is None and g["cno_median"] is None, g
    assert ga.parse_gpsd(['{"class":"VERSION"}', "x"]) is None
    assert ga.parse_gpsd([]) is None
    # nSat/uSat-only SKY (no satellites list)
    g2 = ga.parse_gpsd(['{"class":"TPV","mode":2}', '{"class":"SKY","nSat":9,"uSat":5}'])
    assert g2["fix"] == "2D" and g2["sats_visible"] == 9 and g2["sats_used"] == 5, g2


def test_query_gpsd_unreachable():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                            # nothing listening there now
    assert ga.query_gpsd(f"127.0.0.1:{port}", timeout=0.5) is None
    assert ga.query_gpsd("/nonexistent/gpsd.sock", timeout=0.2) is None


def test_query_gpsd_fake_server():
    """A one-shot fake gpsd that answers WATCH/POLL with the captured lines."""
    import threading
    with open(FIX_LINES, "rb") as f:
        payload = f.read()
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def _serve():
        c, _ = srv.accept()
        with c:
            c.recv(256)
            c.sendall(payload)
    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    g = ga.query_gpsd(f"127.0.0.1:{port}", timeout=1.5)
    srv.close()
    assert g and g["fix"] == "3D" and g["sats_used"] == 18, g


# 4 ---------------------------------------------------------------------------

class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_sim():
    clk = _Clock()
    sim = ga.SimGpsAntenna(clock=clk)
    assert sim.read_bias()["ant_bias"] is False
    assert sim.gnss()["sats_visible"] == 0 and not sim.gpsdo_locked()
    assert sim.set_bias(True)["ant_bias"] is True
    clk.t += 10
    g = sim.gnss()
    assert g["sats_visible"] > 0 and g["fix"] == "none", g
    clk.t += 15
    assert sim.gnss()["fix"] == "3D" and not sim.gpsdo_locked()
    clk.t += 30
    assert sim.gpsdo_locked()
    sim.set_bias(False)
    assert sim.gnss()["sats_visible"] == 0 and not sim.gpsdo_locked()


# 5 + 6 -----------------------------------------------------------------------

def _svc(kind, simulate=True, host_type=HostType.G40_RX):
    return RadioService(serial=f"{kind.upper()}-GPSSIM", host_type=host_type, kind=kind,
                        simulate=simulate, cmd_port=47896, discovery_port=47897,
                        broadcast="127.0.0.1")


def _init_p(clock="gpsdo"):
    return {"kind": "g40", "sample_rate": 1_920_000, "center_freq": 425_025_000,
            "rx_gain": 245, "clock_source": clock, "rx_antenna": "RXA1",
            "extras": {"card": 0, "chan_mode": "single"}}


def _size(ack):
    return len(ack.pack())


async def _service_sim(kind):
    svc = _svc(kind)
    svc.loop = asyncio.get_running_loop()
    clk = _Clock()
    svc._gps_sim._clock = clk
    a = await svc._cmd_gps_status(1, {})
    assert a.status == CmdStatus.REJECTED and a.result["ant_bias"] is False, a
    assert a.result["gnss"]["sats_visible"] == 0
    a = await _cmd(svc, 2, "gps_antenna_bias", {"enable": True})
    assert a.status == CmdStatus.OK and a.result["ant_bias"] is True and a.result.get("sim"), a
    ack = await svc._cmd_init(3, _init_p())
    assert ack.status == CmdStatus.OK, ack.result
    clk.t += 60
    a = await svc._cmd_gps_status(4, {})
    r = a.result
    assert a.status == CmdStatus.OK and r["ant_bias"] is True and r["gnss"]["fix"] == "3D", r
    assert r["ref_clock"] == "gpsdo" and r["gpsdo_locked"] is True and r["pps_present"], r
    assert _size(a) < proto.MAX_DATAGRAM
    a = await _cmd(svc, 5, "gps_antenna_bias", {"enable": False})
    assert a.status == CmdStatus.OK and a.result["ant_bias"] is False
    r = (await svc._cmd_gps_status(6, {})).result
    assert r["gpsdo_locked"] is False and r["gnss"]["sats_visible"] == 0, r
    for bad in ({}, {"enable": "yes"}, {"enable": 1}):
        a = await _cmd(svc, 7, "gps_antenna_bias", bad)
        assert a.status == CmdStatus.REJECTED, (bad, a)
    await svc._cmd_close(8)


def test_service_sim_mock():
    asyncio.run(_service_sim("mock"))


def test_service_sim_g40():
    asyncio.run(_service_sim("g40"))


async def _service_real_g40_patched():
    svc = _svc("g40", simulate=False)
    assert svc._gps_sim is None and svc._gps_real
    svc.loop = asyncio.get_running_loop()
    _boot_state()
    orig = (ga.I2cDev, ga._line_check, ga.query_gpsd)
    with open(FIX_LINES) as f:
        fix = ga.parse_gpsd(f.readlines())
    try:
        ga.I2cDev = FakePCA
        ga._line_check = lambda: None
        ga.query_gpsd = lambda *a, **k: fix
        a = await svc._cmd_gps_status(1, {})        # not inited: REJECTED but carries sky
        assert a.status == CmdStatus.REJECTED and a.result["ant_bias"] is False, a
        assert a.result["gnss"]["fix"] == "3D"
        a = await _cmd(svc, 2, "gps_antenna_bias", {"enable": True})
        assert a.status == CmdStatus.OK and a.result == {"ant_bias": True, "raw": "00 00 00 00"}, a
        assert FakePCA.regs[0x17] == 0 and FakePCA.regs[0x19] == 0
        a = await _cmd(svc, 3, "gps_antenna_bias", {"enable": False})
        assert a.status == CmdStatus.OK and a.result["ant_bias"] is False and FakePCA.regs[0x17] == 0x10
        ga._line_check = lambda: "ant-bias-en is held by another consumer"
        a = await _cmd(svc, 4, "gps_antenna_bias", {"enable": True})
        assert a.status == CmdStatus.FAILED and "refused" in a.result["err"] and a.result["ant_bias"] is False, a
        FakePCA.regs[0x17], FakePCA.regs[0x19] = 0x01, 0x08          # PWM duty: unknown
        r = svc._gps_antenna_info()
        assert r["ant_bias"] is None and "PWM" in r["ant_bias_err"], r
        # worst case: a full EpiqBackend-style status + sky + a long error
        st = {"gpsdo_supported": True, "gpsdo_enabled": True, "gpsdo_locked": True,
              "freq_accuracy_ppm": -0.0222222222, "ref_clock": "internal+gpsdo (apply skipped: x)",
              "ext_ref_freq_hz": 40_000_000, "ext_ref_present": True, "pps_present": True,
              "pps_source": 0, "supported": True, "ant_bias": None, "gnss": fix,
              "ant_bias_err": "x" * 200, "err": "y" * 300}
        assert _size(CmdAck(2**31, CmdStatus.OK, st)) < proto.MAX_DATAGRAM, _size(CmdAck(1, 0, st))
    finally:
        ga.I2cDev, ga._line_check, ga.query_gpsd = orig


def test_service_real_g40_patched():
    asyncio.run(_service_real_g40_patched())


async def _service_x40(simulate, monkeypatch=None):
    # the X40's bias is the ZynqMP's: never switched here. Its GNSS fix comes from
    # its gpsd (simulated, or the ZynqMP's address on a real X40)
    svc = _svc("x40", simulate=simulate, host_type=HostType.X40_TX)
    svc.loop = asyncio.get_running_loop()
    a = await _cmd(svc, 1, "gps_antenna_bias", {"enable": True})
    assert a.status == CmdStatus.REJECTED and "ZynqMP" in a.result["err"], a
    info = svc._gps_antenna_info()
    assert info["ant_bias"] is None and "ZynqMP" in info["ant_bias_err"]
    a = await svc._cmd_gps_status(2, {})
    assert a.result["ant_bias"] is None
    return a.result["gnss"]


def test_service_x40_rejected():
    assert asyncio.run(_service_x40(True))["fix"] == "3D"


def test_service_real_x40_reads_the_zynqmp_gpsd():
    seen = []
    orig = ga.query_gpsd
    ga.query_gpsd = lambda addr=None, timeout=1.5: seen.append(addr) or {"fix": "3D", "sats_used": 19}
    try:
        assert asyncio.run(_service_x40(False))["sats_used"] == 19
    finally:
        ga.query_gpsd = orig
    assert seen and set(seen) == {"192.168.55.100:2947"}


class _W:
    """Stand-in for the NiceGUI widgets GpsAntennaRow.update touches."""
    def __init__(self):
        self.text, self.value, self.enabled, self.visible = "", False, True, True
    def set_text(self, t):
        self.text = t
    def set_visibility(self, v):
        self.visible = bool(v)
    def enable(self):
        self.enabled = True
    def disable(self):
        self.enabled = False


def test_gui_hint_logic():
    from dpmg.web import link_tab as lt
    row = object.__new__(lt.GpsAntennaRow)
    row.cb, row.state_lbl, row.sky_lbl, row.hint_lbl, row.note_lbl = _W(), _W(), _W(), _W(), _W()
    row._sync, row._zero_since = False, None
    clk = _Clock()
    orig = lt.time.monotonic
    lt.time.monotonic = clk
    try:
        none = {"fix_mode": 1, "fix": "none", "sats_visible": 0, "sats_used": 0,
                "cno_max": None, "cno_median": None, "time_utc": None}
        weak = {"fix_mode": 3, "fix": "3D", "sats_visible": 15, "sats_used": 9,
                "cno_max": 12.0, "cno_median": 9.0, "time_utc": "x"}   # G40 reading, bias off
        good = dict(weak, cno_max=44.0, cno_median=34.0)
        def feed(bias, g, dt=10):
            row.update({"ant_bias": bias, "gnss": g}); clk.t += dt
            return row.hint_lbl.text
        assert feed(False, none, 0) == ""
        clk.t += 119; assert feed(False, none, 0) == ""
        clk.t += 1;   assert feed(False, none, 0).startswith("No satellites"), row.hint_lbl.text
        assert row.cb.value is False and row.state_lbl.text == "bias: OFF"
        assert feed(True, none, 200) == ""                       # bias on: never hint
        assert feed(False, weak, 121) == ""                      # timer restarts
        h = feed(False, weak, 0)
        assert h.startswith("Weak satellites only (C/N0 12 dB-Hz)") and "3.3 V bias" in h, h
        assert feed(False, good, 300) == "" and feed(False, good) == ""   # strong passive: no hint
        assert feed(False, None, 300) == ""                      # gpsd unreachable: no hint
        row.update({"ant_bias": None, "gnss": None})
        assert row.cb.enabled is False and "n/a" in row.state_lbl.text
        assert row.note_lbl.visible is False                     # the G40 reboot note: not for an X40
        assert lt._bias_who_text(True, {"ant_bias_by": None, "ant_bias": None}) == ""
    finally:
        lt.time.monotonic = orig


async def _cmd(svc, cid, name, p):
    """Through the service's real command dispatcher (name: handler)."""
    return await svc._apply(proto.Cmd(cid, name, p))


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    fails = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:
            fails += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{fails} failure(s) of {len(tests)}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
