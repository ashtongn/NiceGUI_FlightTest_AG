#!/usr/bin/env python3
"""
Bench harness reference clock + GPSDO pre-flight (bench/checkout_common.py)
==========================================================================
Offline, against a fake service link (no socket, no radio):

  1. resolve_clock: auto: gpsdo on a real G40, internal on a simulator / X40.
  2. The init carries the clock: port_init_params / init_ports and
     fsk_loopback_test.init_loopback send clock_source as asked.
  3. gpsdo gate (with --ant-bias): bias off: gps_antenna_bias {enable: true}, ant_bias confirmed
     in gps_status, gps_status polled until locked; the lock record keeps time
     to lock, ppm, satellites used and C/N0. Bias already on: no bias command.
  4. Refusals: an unlocked receiver, a bias that will not come on, and an init
     the service did not put on the GPSDO all raise GpsLockError (tagged) out of
     the init helper, so the harness never gets to start_tx.
  5. internal clock: no GPS commands at all.
  6. gps_preflight: RX-only init on gpsdo, then close, also when it fails.
  7. Report headers: FIRST_CHECKOUT and PORT_MATRIX carry clock, lock time, ppm.
  8. first_checkout.main: an unlocked pre-flight aborts the run before any stage
     (rc 3, report says ABORTED); a locked one hands --clock gpsdo to every stage.

    python3 tests/test_bench_gpsdo_preflight.py     (exit 0 = all pass; pytest works too)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "bench"))

from dpmg.link.proto import CmdStatus                      # noqa: E402
import checkout_common as cc                     # noqa: E402
import fsk_loopback_test as flt                  # noqa: E402

# These cases cover the consented path (a run started with --ant-bias,
# so the harness may switch the bias on). The refusal without consent is covered
# in tests/test_bias_opt_in.py.
os.environ[cc.ANT_BIAS_ENV] = "1"
import first_checkout as fc                      # noqa: E402
import port_matrix as pm                         # noqa: E402

cc.LOCK_POLL_S = 0.0                             # no sleeps between polls offline

GNSS = {"fix_mode": 3, "fix": "3D", "sats_visible": 21, "sats_used": 14,
        "cno_max": 45.0, "cno_median": 37.0, "time_utc": None}


class FakeLink:
    """Stand-in for fsk_loopback_test.Link: answers init / gps_status /
    gps_antenna_bias / close like dpmg_service, and logs every command."""

    def __init__(self, serial="G40-0001-0001", bias=False, lock_after=3,
                 bias_works=True, apply_clock=True, ext_ref=True, pps=True):
        self.serial = serial
        self.bias = bias
        self.lock_after = lock_after          # gps_status polls after init until locked; None = never
        self.bias_works = bias_works
        self.apply_clock = apply_clock
        self.ext_ref = ext_ref                # a 10 MHz standard is on REF IN (external clock)
        self.pps = pps                        # a 1 PPS edge is arriving
        self.inited = False
        self.clock = None
        self.polls = 0
        self.cmds = []
        self.rx_conn = self.tx_conn = "-"

    def names(self):
        return [n for n, _ in self.cmds]

    def params(self, name):
        return [p for n, p in self.cmds if n == name]

    def cmd(self, name, params, timeout=120.0, **_kw):
        self.cmds.append((name, dict(params)))
        ok = CmdStatus.OK
        if name == "init":
            self.inited, self.polls = True, 0
            self.clock = params.get("clock_source") if self.apply_clock else "internal"
            return cc._Ack(ok, {"chan_mode": "single", "achieved_rate": params["sample_rate"]})
        if name == "gps_antenna_bias":
            if not self.bias_works:
                return cc._Ack(CmdStatus.FAILED, {"err": "i2c write failed", "ant_bias": False})
            self.bias = bool(params["enable"])
            return cc._Ack(ok, {"ant_bias": self.bias})
        if name == "gps_status":
            ant = {"ant_bias": self.bias, "gnss": dict(GNSS) if self.bias else None}
            if not self.inited:
                return cc._Ack(CmdStatus.REJECTED, {"err": "not initialized", **ant})
            self.polls += 1
            locked = (self.clock == "gpsdo" and self.bias and self.lock_after is not None
                      and self.polls >= self.lock_after)
            ext = self.clock == "external" and self.ext_ref
            return cc._Ack(ok, {"supported": True, "ref_clock": self.clock,
                                "gpsdo_enabled": self.clock == "gpsdo", "gpsdo_locked": locked,
                                "freq_accuracy_ppm": 0.011 if locked else None,
                                "ext_ref_present": ext if self.clock == "external" else False,
                                "ext_ref_freq_hz": 10_000_000 if ext else 0,
                                "pps_present": bool(self.pps), **ant})
        if name == "close":
            self.inited = False
        return cc._Ack(ok, {})


def _raises_lock(fn):
    try:
        fn()
    except cc.GpsLockError as e:
        assert str(e.code).startswith(cc.GPS_ABORT_TAG), e.code
        return str(e.code)
    raise AssertionError("expected GpsLockError")


# 1
def test_resolve_clock():
    assert cc.resolve_clock("auto", "G40-0001-0001") == "gpsdo"
    assert cc.resolve_clock("auto", "G40-SIM") == "internal"
    assert cc.resolve_clock("auto", "G40-0001-0001", kind="x40") == "internal"
    assert cc.resolve_clock("internal", "G40-0001-0001") == "internal"
    assert cc.resolve_clock("gpsdo", "G40-SIM") == "gpsdo"
    assert cc.resolve_clock("external", "x") == "external"
    ap = argparse.ArgumentParser()
    cc.add_clock_args(ap)
    a = ap.parse_args([])
    assert a.clock == "auto" and a.lock_timeout == 600.0
    a = ap.parse_args(["--clock", "gpsdo", "--lock-timeout", "30"])
    assert a.clock == "gpsdo" and a.lock_timeout == 30.0


# 2
def test_init_carries_clock():
    for clk in cc.CLOCKS:
        p = cc.port_init_params("TRx1A", "TRx1B", 1_920_000, cc.CENTER, 255, 5, clock=clk)
        assert p["clock_source"] == clk
    assert cc.port_init_params("TRx1A", None, 1_920_000, cc.CENTER, 255, 5)["clock_source"] == "internal"
    link = FakeLink()
    a, _dt = cc.init_ports(link, "TRx1A", "TRx1B", 1_920_000, cc.CENTER, 255, 5, clock="gpsdo")
    assert a.status == CmdStatus.OK
    assert link.params("init")[0]["clock_source"] == "gpsdo"
    link = FakeLink()
    flt.init_loopback(link, 1_920_000, cc.CENTER, 5, 255, tx_card=1, clock="gpsdo")
    assert link.params("init")[0]["clock_source"] == "gpsdo"
    link = FakeLink()
    flt.init_loopback(link, 1_920_000, cc.CENTER, 5, 255, tx_card=1)      # old callers: internal
    assert link.params("init")[0]["clock_source"] == "internal"


# 3
def test_gate_enables_bias_and_waits_for_lock():
    link = FakeLink(bias=False, lock_after=4)
    flt.init_loopback(link, 1_920_000, cc.CENTER, 5, 255, tx_card=1, clock="gpsdo", lock_timeout=10)
    n = link.names()
    assert n[0] == "init"
    assert [p["enable"] for p in link.params("gps_antenna_bias")] == [True]
    i_bias = n.index("gps_antenna_bias")
    assert "gps_status" in n[i_bias + 1:], "ant_bias must be re-read after the enable"
    assert link.polls >= 4, "must poll until the 4th status reports lock"
    rec = cc.gps_log(link)[-1]
    assert rec["clock"] == "gpsdo" and rec["locked"] is True and rec["bias_enabled_now"] is True
    assert rec["ppm"] == 0.011 and rec["sats_used"] == 14
    assert rec["cno_max"] == 45.0 and rec["cno_median"] == 37.0
    assert isinstance(rec["t_lock_s"], float) and rec["polls"] >= 1
    s = cc.gps_summary(link, "gpsdo")
    assert s["locked"] is True and s["ppm"] == 0.011 and len(s["checks"]) == 1
    json.dumps(s)                                                         # JSON-safe


def test_gate_bias_already_on_sends_no_bias_cmd():
    link = FakeLink(bias=True, lock_after=1)
    cc.init_ports(link, "TRx1A", "TRx1B", 1_920_000, cc.CENTER, 255, 5, clock="gpsdo")
    assert "gps_antenna_bias" not in link.names()
    assert cc.gps_log(link)[-1]["bias_enabled_now"] is False
    # gate before start_rx: start_rx comes after the lock poll
    n = link.names()
    assert n.index("start_rx") > max(i for i, x in enumerate(n) if x == "gps_status")


# 4
def test_unlocked_receiver_aborts_before_keying():
    link = FakeLink(lock_after=None)
    t0 = time.time()
    msg = _raises_lock(lambda: flt.init_loopback(link, 1_920_000, cc.CENTER, 5, 255, tx_card=1,
                                                 clock="gpsdo", lock_timeout=0.3))
    assert time.time() - t0 < 5
    assert "did not lock" in msg and "Refusing to key" in msg
    assert "start_tx" not in link.names()
    # init_ports raises too, before start_rx
    link = FakeLink(lock_after=None)
    _raises_lock(lambda: cc.init_ports(link, "TRx1A", "TRx1B", 1_920_000, cc.CENTER, 255, 5,
                                       clock="gpsdo", lock_timeout=0.3))
    assert "start_rx" not in link.names() and "start_tx" not in link.names()


def test_bias_that_will_not_come_on_aborts():
    link = FakeLink(bias=False, bias_works=False)
    msg = _raises_lock(lambda: cc.init_ports(link, "TRx1A", "TRx1B", 1_920_000, cc.CENTER, 255, 5,
                                             clock="gpsdo", lock_timeout=5))
    assert "antenna bias did not come on" in msg
    assert [p["enable"] for p in link.params("gps_antenna_bias")] == [True]


def test_init_not_on_gpsdo_aborts_early():
    link = FakeLink(apply_clock=False)               # service ignored clock_source
    t0 = time.time()
    msg = _raises_lock(lambda: cc.gpsdo_gate(link, "gpsdo", timeout=60) if
                       cc.init_ports(link, "TRx1A", None, 1_920_000, cc.CENTER, 255, 5,
                                     clock="internal") else None)
    assert "GPSDO not enabled" in msg and time.time() - t0 < 5


# 5
def test_internal_clock_sends_no_gps_commands():
    link = FakeLink()
    flt.init_loopback(link, 1_920_000, cc.CENTER, 5, 255, tx_card=1, clock="internal")
    assert not [x for x in link.names() if x.startswith("gps")], link.names()
    rec = cc.gps_log(link)[-1]
    assert rec["clock"] == "internal" and rec["locked"] is None
    assert cc.gps_preflight(FakeLink(), "internal")["locked"] is None


# 6
def test_preflight_rx_only_init_then_close():
    link = FakeLink(bias=False, lock_after=3)
    rec = cc.gps_preflight(link, "gpsdo", timeout=10)
    n = link.names()
    assert n[0] == "gps_status" and n[1] == "gps_antenna_bias", n   # bias before the init
    init = link.params("init")
    assert len(init) == 1 and init[0]["clock_source"] == "gpsdo"
    assert "tx_antenna" not in init[0], "pre-flight must not set up TX"
    assert "start_tx" not in n and "start_rx" not in n
    assert n[-1] == "close"
    assert rec["locked"] is True and rec["label"] == "preflight"
    assert rec["ppm"] == 0.011 and rec["sats_used"] == 14 and rec["t_lock_s"] >= 0

    link = FakeLink(lock_after=None)
    _raises_lock(lambda: cc.gps_preflight(link, "gpsdo", timeout=0.3))
    assert link.names()[-1] == "close", "a failed pre-flight still closes the radio"


# 7
def _locked_rec():
    link = FakeLink(lock_after=2)
    return cc.gps_preflight(link, "gpsdo", timeout=10)


def test_first_checkout_header():
    pre = _locked_rec()
    args = argparse.Namespace(host="192.168.0.22", port=47810, arm=True, quick=True, skip=[],
                              clock="gpsdo", gps_preflight=pre, aborted=None)
    rep, md = fc.build_report(args, {}, {}, {}, time.time())
    head = md.split("## Cases")[0]
    assert "clock **gpsdo**, locked in" in head and "0.0110 ppm" in head and "sats used 14" in head
    assert rep["clock"] == "gpsdo" and rep["gps_preflight"]["ppm"] == 0.011
    args.gps_preflight, args.aborted = {"clock": "gpsdo", "locked": False}, "pre-flight: x"
    _rep, md = fc.build_report(args, {}, {}, {}, time.time())
    assert "lock **NOT confirmed**" in md and "RUN ABORTED" in md
    args.clock, args.gps_preflight, args.aborted = "internal", {"clock": "internal"}, None
    _rep, md = fc.build_report(args, {}, {}, {}, time.time())
    assert "clock **internal** (GPSDO not in use)" in md
    # per-stage column from the stage JSON's gps block
    link = FakeLink(lock_after=2)
    cc.init_ports(link, "TRx1A", "TRx1B", 1_920_000, cc.CENTER, 255, 5, clock="gpsdo")
    g = fc.stage_gps({"gps": cc.gps_summary(link, "gpsdo")})
    assert g["all_locked"] is True and g["n_checks"] == 1
    assert "1 x locked" in fc._gps_stage_cell("gpsdo", {"status": "ok", "gps": g})
    assert fc._gps_stage_cell("gpsdo", {"status": "ok"}) == "no lock record"


def test_port_matrix_header():
    link = FakeLink(lock_after=2)
    cc.init_ports(link, "TRx1A", "TRx1B", 1_920_000, cc.CENTER, 255, 5, clock="gpsdo")
    cc.init_ports(link, "TRx1A", "TRx2B", 1_920_000, cc.CENTER, 255, 5, clock="gpsdo")
    meta = {"serial": "G40", "host": "h", "date": "d", "rate_hz": 1_920_000, "center_hz": cc.CENTER,
            "rx_gain": 255, "tx_atten": 5, "armed": True, "pad_db": 50, "tone_offset_hz": 310_000,
            "clock": "gpsdo", "gps": cc.gps_summary(link, "gpsdo")}
    rec = {"move": 1, "case": "PORT-G-01", "rx_port": "TRx1A", "tx_port": "TRx1B",
           "relationship": "cross-card", "expect": "pass", "init_status": "OK", "init_s": 1.0,
           "verdict": "PASS", "notes": [], "gps": cc.gps_log(link)[0]}
    md = pm.markdown(meta, [rec], [])
    head = md.split("| move")[0]
    assert "reference: clock **gpsdo**, locked in" in head and "0.0110 ppm" in head
    assert "2 lock checks" in head
    assert "GPSDO lock" in md and "s, 0.0110 ppm" in md
    meta["clock"], meta["gps"] = "internal", None
    assert "clock **internal**" in pm.markdown(meta, [], [])


# 8
def _run_fc_main(argv, fake, capture=None):
    saved = (sys.argv, flt.Link, fc.run_stage)
    flt.Link = lambda host, port: fake
    if capture is not None:
        def _stage(name, argv_, out_dir, env, timeout_s, stdin_tty=True):
            capture.append((name, argv_))
            return {"argv": argv_, "log": "-", "rc": 0, "seconds": 0.0, "status": "ok"}
        fc.run_stage = _stage
    sys.argv = ["first_checkout.py"] + argv
    try:
        return fc.main()
    finally:
        sys.argv, flt.Link, fc.run_stage = saved


def test_first_checkout_aborts_on_unlocked_preflight():
    with tempfile.TemporaryDirectory() as d:
        rp = os.path.join(d, "FC.json")
        called = []
        fake = FakeLink(lock_after=None)
        rc = _run_fc_main(["--host", "h", "--no-prompt", "--arm", "--clock", "gpsdo",
                           "--lock-timeout", "0.3", "--out-dir", d, "--report", rp], fake, called)
        assert rc == 3
        assert called == [], "no stage may run after a failed pre-flight"
        rep = json.load(open(rp))
        assert rep["aborted"].startswith("pre-flight:") and "did not lock" in rep["aborted"]
        assert rep["stages"] == {} and rep["gps_preflight"]["locked"] is False
        assert "RUN ABORTED" in open(os.path.splitext(rp)[0] + ".md").read()
        assert "start_tx" not in fake.names() and fake.names()[-1] == "close"


def test_first_checkout_passes_clock_to_every_stage():
    with tempfile.TemporaryDirectory() as d:
        rp = os.path.join(d, "FC.json")
        called = []
        fake = FakeLink(lock_after=2)
        _run_fc_main(["--host", "h", "--no-prompt", "--clock", "auto", "--lock-timeout", "30",
                      "--only", "operator_loopback,fec_loopback,mod_fec_matrix,snr_sweep,port_matrix",
                      "--out-dir", d, "--report", rp], fake, called)
        assert [n for n, _ in called] == ["operator_loopback", "fec_loopback", "mod_fec_matrix",
                                          "snr_sweep", "port_matrix"]
        for name, argv in called:
            i = argv.index("--clock")
            assert argv[i + 1] == "gpsdo", (name, argv)          # auto on a real serial = gpsdo
            assert argv[argv.index("--lock-timeout") + 1] == "30"
        rep = json.load(open(rp))
        assert rep["clock"] == "gpsdo" and rep["gps_preflight"]["locked"] is True
        # a simulator serial resolves auto to internal
        called.clear()
        _run_fc_main(["--host", "h", "--no-prompt", "--only", "port_matrix",
                      "--out-dir", d, "--report", rp], FakeLink(serial="G40-SIM"), called)
        argv = called[0][1]
        assert argv[argv.index("--clock") + 1] == "internal"


def test_stage_gps_abort_stops_the_run():
    with tempfile.TemporaryDirectory() as d:
        rp = os.path.join(d, "FC.json")
        log = os.path.join(d, "fec_loopback.log")
        with open(log, "w") as f:
            f.write(f"[init] OK\n{cc.GPS_ABORT_TAG} GPSDO did not lock within 600 s\n")
        called = []
        saved = (sys.argv, flt.Link, fc.run_stage)
        flt.Link = lambda host, port: FakeLink(lock_after=1)

        def _stage(name, argv_, out_dir, env, timeout_s, stdin_tty=True):
            called.append(name)
            if name == "fec_loopback":
                return {"argv": argv_, "log": log, "rc": 1, "seconds": 0.0, "status": "failed"}
            return {"argv": argv_, "log": "-", "rc": 0, "seconds": 0.0, "status": "ok"}
        fc.run_stage = _stage
        sys.argv = ["first_checkout.py", "--host", "h", "--no-prompt", "--clock", "gpsdo",
                    "--only", "operator_loopback,fec_loopback,port_matrix",
                    "--out-dir", d, "--report", rp]
        try:
            fc.main()
        finally:
            sys.argv, flt.Link, fc.run_stage = saved
        assert called == ["operator_loopback", "fec_loopback"], called
        rep = json.load(open(rp))
        assert rep["aborted"].startswith("stage fec_loopback:")


# 9
# External reference (bench standard on REF IN and 1PPS IN, no GPS antenna):
# the gate confirms a measured 10 MHz and a live 1 PPS, no bias commands, and
# refuses when either is missing.
def test_external_gate_confirms_ref_and_pps():
    link = FakeLink(bias=False)
    a, _dt = cc.init_ports(link, "TRx1B", "TRx1A", 1_920_000, cc.CENTER, 250, 30,
                           clock="external", lock_timeout=10)
    assert a.status == CmdStatus.OK
    assert link.params("init")[0]["clock_source"] == "external"
    assert "gps_antenna_bias" not in link.names(), "nothing to bias on an external reference"
    assert "gps_status" in link.names()
    rec = cc.gps_log(link)[-1]
    assert rec["clock"] == "external" and rec["locked"] is True
    assert rec["ext_ref_freq_hz"] == 10_000_000 and rec["pps_present"] is True
    json.dumps(cc.gps_summary(link, "external"))


def test_external_gate_refuses_without_ref_or_pps():
    for kw in ({"ext_ref": False}, {"pps": False}):
        link = FakeLink(**kw)
        msg = _raises_lock(lambda: cc.init_ports(link, "TRx1B", "TRx1A", 1_920_000, cc.CENTER,
                                                 250, 30, clock="external", lock_timeout=0.01))
        assert "external reference not confirmed" in msg, msg
        assert "start_tx" not in link.names()


def test_external_gate_internal_unchanged():
    link = FakeLink()
    cc.init_ports(link, "TRx1A", "TRx1B", 1_920_000, cc.CENTER, 255, 5, clock="internal")
    assert "gps_status" not in link.names()
    assert cc.gps_log(link)[-1]["locked"] is None


def test_external_preflight():
    link = FakeLink()
    rec = cc.gps_preflight(link, "external", timeout=10)
    assert rec["clock"] == "external" and rec["locked"] is True and rec["label"] == "preflight"
    assert link.names()[-1] == "close" and not link.inited
    link = FakeLink(ext_ref=False)
    msg = _raises_lock(lambda: cc.gps_preflight(link, "external", timeout=0.01))
    assert "external reference" in msg and not link.inited


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS  {name}")
            except Exception as e:                                # noqa: BLE001
                fails += 1
                import traceback
                traceback.print_exc()
                print(f"FAIL  {name}: {e}")
    print(f"\n{fails} failure(s)")
    sys.exit(1 if fails else 0)
