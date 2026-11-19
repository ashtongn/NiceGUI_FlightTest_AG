#!/usr/bin/env python3
"""
Shared pieces for the G40 checkout harnesses:
bench/mod_fec_matrix.py, bench/snr_sweep.py, bench/port_matrix.py and the
driver bench/first_checkout.py.

Everything here is radio-agnostic plumbing on top of fsk_loopback_test.Link:
the operator console's port: card/handle mapping (link_tab._PORT_MAP), the
Block 3 cable-move order, a telemetry collector (DETECTION / WATCH_UPDATE /
STATUS), a keyed-tone helper and the JSON + markdown result writers. Nothing
in this module radiates by itself.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import socket
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from dpmg.link import proto                                      # noqa: E402
from dpmg.link.proto import MsgType, CmdStatus              # noqa: E402
from fsk_loopback_test import Link                # noqa: E402,F401  (re-exported)

CENTER = 425_025_000
RATE_NARROW = 1_920_000          # decode-friendly rate every per-case test runs at
RATE_WIDE = 30_720_000           # the dense / 25-signal detection rate
SLOT_BW_HZ = 20_000.0            # dpmg_service.DET_DEFAULT_SLOT_BW: "within 1 slot"
# hardware-legal G40 TX attenuations in dB, as the operator console offers them
ATTEN_STEPS = [0, 1, 2, 3, 5, 7, 10, 15, 20, 25, 30, 35, 40, 41.75]

# the operator console's port map (link_tab._PORT_MAP): connector: card + handles
PORT_MAP = {
    "TRx1A": {"card": 0, "rx": "RXA1", "tx": "TXA1"},
    "TRx2A": {"card": 0, "rx": "RXA2", "tx": "TXA2"},
    "TRx1B": {"card": 1, "rx": "RXA1", "tx": "TXA1"},
    "TRx2B": {"card": 1, "rx": "RXA2", "tx": "TXA2"},
}

# Port matrix moves: (move, rx_port, tx_port, case id, expectation).
# The PORT case ids keep their original numbering, which differs from the cable-move order. "refused" = the service must
# reject the init (G40_SAME_CARD: RX on a "2" jack + TX on the "1" jack of the
# same card cannot co-stream in the NV100 FIXED port mode).
PORT_MOVES = [
    (1,  "TRx1A", "TRx1B", "PORT-G-01", "pass"),
    (2,  "TRx1A", "TRx2B", "PORT-G-03", "pass"),
    (3,  "TRx1A", "TRx2A", "PORT-G-02", "pass"),
    (4,  "TRx2A", "TRx1A", "PORT-G-06", "refused"),
    (5,  "TRx2A", "TRx1B", "PORT-G-04", "pass"),
    (6,  "TRx2A", "TRx2B", "PORT-G-05", "pass"),
    (7,  "TRx1B", "TRx1A", "PORT-G-07", "pass"),
    (8,  "TRx1B", "TRx2A", "PORT-G-08", "pass"),
    (9,  "TRx1B", "TRx2B", "PORT-G-09", "pass"),
    (10, "TRx2B", "TRx1B", "PORT-G-12", "refused"),
    (11, "TRx2B", "TRx1A", "PORT-G-10", "pass"),
    (12, "TRx2B", "TRx2A", "PORT-G-11", "pass"),
]


# small utils
def iso_now() -> str:
    return _dt.datetime.now().replace(microsecond=0).isoformat()


def date_tag() -> str:
    return _dt.date.today().isoformat()


def fnum(x, nd=1):
    """Number: short string, '-' for None/NaN."""
    try:
        if x is None or x != x:
            return "-"
        return f"{float(x):.{nd}f}"
    except (TypeError, ValueError):
        return str(x)


def md_table(headers, rows) -> str:
    out = ["| " + " | ".join(str(h) for h in headers) + " |",
           "|" + "---|" * len(headers)]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out) + "\n"


def write_results(json_path: str, data: dict, md_text: str | None = None):
    """Write `data` to json_path and, when given, md_text to the same stem
    with a .md extension. Returns (json_path, md_path_or_None)."""
    json_path = os.path.abspath(json_path)
    os.makedirs(os.path.dirname(json_path), exist_ok=True)
    with open(json_path, "w") as f:
        json.dump(data, f, indent=1, default=str)
    md_path = None
    if md_text is not None:
        md_path = os.path.splitext(json_path)[0] + ".md"
        with open(md_path, "w") as f:
            f.write(md_text)
    return json_path, md_path


class _Ack:
    """Stand-in for a proto.CmdAck when Link.cmd bailed (SystemExit on a
    rejected set_plan / set_tx_waveform or a lost CMD_ACK)."""
    def __init__(self, status, result):
        self.status = status
        self.result = result
        self.cmd_id = -1


def safe_cmd(link, name, params, timeout=60.0):
    """Link.cmd that never raises: a rejected set_plan / set_tx_waveform (which
    Link.cmd turns into SystemExit so a sweep cannot silently test the wrong
    waveform) comes back as a REJECTED ack, a lost ack as FAILED. Callers
    record the failure against the case and move on to the next one."""
    try:
        a = link.cmd(name, params, timeout=timeout)
        return a if a is not None else _Ack(CmdStatus.FAILED, {"err": "no ack"})
    except SystemExit as e:
        msg = str(e)
        st = CmdStatus.REJECTED if "rejected" in msg else CmdStatus.FAILED
        return _Ack(st, {"err": msg[:300]})


def ack_err(ack) -> str:
    r = getattr(ack, "result", None) or {}
    return str(r.get("err", r))[:200] if isinstance(r, dict) else str(r)[:200]


# reference clock / GPSDO
# Every Setup A checkout runs on the GPSDO reference with the 3.3 V GPS antenna
# bias on, and keys only after the GPSDO reports lock (the bias is off after
# every G40 reboot; without it the active antenna is dead and lock never comes).
# The gate sits in the init helpers (init_ports here, fsk_loopback_test.
# init_loopback): an init on gpsdo returns only once lock is confirmed, or
# raises GpsLockError, so no stage can key on an unlocked reference.
CLOCKS = ("internal", "gpsdo", "external", "ext_10mhz", "ext_pps")
LOCK_TIMEOUT_S = 600.0
LOCK_POLL_S = 5.0


GPS_ABORT_TAG = "[GPSDO ABORT]"


class GpsLockError(SystemExit):
    """The GPSDO did not lock (or the antenna bias would not come on) in time.
    A SystemExit so a harness stops before it keys, with the message as text.
    The text starts with GPS_ABORT_TAG so first_checkout can spot it in a
    stage log and stop the run."""
    def __init__(self, msg):
        super().__init__(f"{GPS_ABORT_TAG} {msg}")


# The harness switches the 3.3 V GPS antenna bias on only when the run
# was started with --ant-bias. The flag is passed to child stages through this
# environment variable, so first_checkout's sub-scripts inherit the consent.
ANT_BIAS_ENV = "DPMG_BENCH_ANT_BIAS"


class _AntBiasFlag(argparse.Action):
    def __init__(self, option_strings, dest, **kw):
        super().__init__(option_strings, dest, nargs=0, default=False, **kw)

    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, True)
        os.environ[ANT_BIAS_ENV] = "1"


def add_ant_bias_arg(ap):
    """--ant-bias: allow this run to switch the GPS antenna bias on."""
    ap.add_argument("--ant-bias", action=_AntBiasFlag, dest="ant_bias",
                    help="allow this run to switch the 3.3 V GPS antenna bias on when it reads "
                         "off. Only with an active antenna fitted: a passive antenna can be a DC "
                         "short. Without it a gpsdo run with the bias off refuses to key")


def ant_bias_allowed() -> bool:
    return os.environ.get(ANT_BIAS_ENV) == "1"


# Peak of every scenario the run builds (gen_scenario peak_fs). Set by
# --peak-fs and passed to child stages through this environment variable, the
# same way as --ant-bias; the bench Link adds it to each gen_scenario command
# that does not carry its own. Unset, the builder default (0.45) applies.
PEAK_FS_ENV = "DPMG_BENCH_PEAK_FS"


class _PeakFsAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)
        os.environ[PEAK_FS_ENV] = repr(float(values))


def add_peak_fs_arg(ap):
    """--peak-fs: peak of the scenarios this run builds, as a fraction of full scale."""
    ap.add_argument("--peak-fs", type=float, default=None, action=_PeakFsAction,
                    dest="peak_fs",
                    help="peak of every scenario this run builds, as a fraction of DAC full "
                         "scale (default: the builder's 0.45). Scenarios built before "
                         "2026-10-31 were always 0.9; pass 0.9 to repeat those runs")


def bench_peak_fs():
    """The run's --peak-fs, or None for the builder default."""
    v = os.environ.get(PEAK_FS_ENV)
    return float(v) if v else None


def add_clock_args(ap):
    """--clock / --lock-timeout / --ant-bias / --peak-fs for every harness that inits the radio."""
    ap.add_argument("--clock", choices=("auto",) + CLOCKS, default="auto",
                    help="reference clock (init clock_source): gpsdo / internal / external. "
                         "auto (default) = gpsdo on a real G40, internal on a simulator "
                         "(serial contains SIM). On gpsdo nothing keys until the GPSDO reports "
                         "lock; the GPS antenna bias must already be on, or pass --ant-bias")
    add_ant_bias_arg(ap)
    add_peak_fs_arg(ap)
    ap.add_argument("--lock-timeout", type=float, default=LOCK_TIMEOUT_S, dest="lock_timeout",
                    help=f"seconds to wait for GPSDO lock after a gpsdo init before aborting "
                         f"(default {LOCK_TIMEOUT_S:.0f})")


def is_simulator(serial) -> bool:
    """A simulated service (dpmg_service.py --simulate --serial G40-SIM ...)."""
    return "SIM" in str(serial or "").upper()


def resolve_clock(clock, serial, kind="g40") -> str:
    """'auto': gpsdo for a real G40, internal for a simulator (or a radio kind
    without a GPSDO, e.g. the X40)."""
    if clock in (None, "", "auto"):
        return "gpsdo" if kind == "g40" and not is_simulator(serial) else "internal"
    if clock not in CLOCKS:
        raise SystemExit(f"unknown clock {clock!r}; one of {', '.join(CLOCKS)}")
    return clock


def gps_status(link, timeout=10.0) -> tuple:
    """(ack_status, result dict). gps_status before an init comes back REJECTED
    "not initialized" but still carries ant_bias + gnss (host GPIO / gpsd)."""
    a = safe_cmd(link, "gps_status", {}, timeout=timeout)
    r = a.result if isinstance(a.result, dict) else {}
    return a.status, r


def ensure_ant_bias(link) -> dict:
    """Confirm the 3.3 V GPS antenna bias is on. Switch it on only when the run
    was started with --ant-bias. Raises GpsLockError."""
    _st, r = gps_status(link)
    if r.get("ant_bias") is True:
        return {"ant_bias": True, "enabled_now": False}
    if not ant_bias_allowed():
        raise GpsLockError(
            f"GPS antenna bias reads {r.get('ant_bias')} (OFF). Not switching 3.3 V onto the "
            f"GPS antenna jack without consent. Check the antenna is an ACTIVE antenna, then "
            f"either tick 'GPS antenna bias 3.3 V' in the GUI or re-run with --ant-bias. "
            f"Use --clock internal only if this checkout does not need the GPSDO")
    print(f"[gps] antenna bias reads {r.get('ant_bias')}: sending gps_antenna_bias enable "
          f"(--ant-bias given)", flush=True)
    b = safe_cmd(link, "gps_antenna_bias",
                 {"enable": True, "by": f"bench {os.path.basename(sys.argv[0])}"}, timeout=15)
    br = b.result if isinstance(b.result, dict) else {}
    if b.status != CmdStatus.OK or br.get("ant_bias") is not True:
        raise GpsLockError(f"GPS antenna bias did not come on: gps_antenna_bias "
                           f"{CmdStatus(b.status).name} {br}. Not keying on an unpowered "
                           f"GPS antenna; check the antenna / service, or run with --clock internal "
                           f"only if the checkout does not need the GPSDO")
    _st, r = gps_status(link)
    if r.get("ant_bias") is not True:
        raise GpsLockError(f"gps_antenna_bias acked but gps_status reads ant_bias="
                           f"{r.get('ant_bias')} ({r.get('ant_bias_err', '')})")
    print("[gps] antenna bias ON (confirmed in gps_status)", flush=True)
    return {"ant_bias": True, "enabled_now": True}


def lock_record(r: dict, t_lock_s, polls, label="") -> dict:
    """The lock data a report keeps: time to lock, ppm, satellites, C/N0."""
    g = r.get("gnss") if isinstance(r.get("gnss"), dict) else {}
    return {"clock": "gpsdo", "locked": bool(r.get("gpsdo_locked")), "label": label,
            "at": iso_now(), "t_lock_s": round(float(t_lock_s), 1), "polls": polls,
            "ppm": r.get("freq_accuracy_ppm"), "ref_clock": r.get("ref_clock"),
            "pps_present": r.get("pps_present"), "ant_bias": r.get("ant_bias"),
            "fix": g.get("fix"), "sats_used": g.get("sats_used"),
            "sats_visible": g.get("sats_visible"), "cno_max": g.get("cno_max"),
            "cno_median": g.get("cno_median"), "sim": bool(r.get("sim"))}


def wait_gpsdo_lock(link, timeout=LOCK_TIMEOUT_S, poll_s=None, label="") -> dict:
    """Poll gps_status (needs an init on gpsdo) until the GPSDO reports lock.
    Returns lock_record(); raises GpsLockError on timeout, or early when the
    service shows the init is not on the GPSDO at all."""
    poll_s = LOCK_POLL_S if poll_s is None else poll_s
    t0 = time.time()
    polls, not_gpsdo, last_print, r = 0, 0, 0.0, {}
    while True:
        st, r = gps_status(link)
        polls += 1
        el = time.time() - t0
        if st == CmdStatus.OK and r.get("gpsdo_locked") and r.get("ant_bias") is not False:
            rec = lock_record(r, el, polls, label)
            print(f"[gps] GPSDO LOCKED{' (' + label + ')' if label else ''} after {rec['t_lock_s']} s: "
                  f"{fnum(rec['ppm'], 4)} ppm, sats used {rec['sats_used']}/{rec['sats_visible']}, "
                  f"C/N0 max {rec['cno_max']} median {rec['cno_median']} dB-Hz, fix {rec['fix']}",
                  flush=True)
            return rec
        # an init on internal/external answers gpsdo_enabled false every poll
        not_gpsdo = not_gpsdo + 1 if (st == CmdStatus.OK and r.get("gpsdo_enabled") is False) else 0
        if not_gpsdo >= 3:
            raise GpsLockError(f"GPSDO not enabled after the init (ref_clock {r.get('ref_clock')}, "
                               f"gpsdo_enabled false): the init did not apply clock_source gpsdo")
        if el >= timeout:
            g = r.get("gnss") if isinstance(r.get("gnss"), dict) else {}
            raise GpsLockError(
                f"GPSDO did not lock within {timeout:.0f} s{' (' + label + ')' if label else ''}: "
                f"gps_status {CmdStatus(st).name} locked={r.get('gpsdo_locked')} "
                f"ant_bias={r.get('ant_bias')} fix={g.get('fix')} sats used {g.get('sats_used')}"
                f"/{g.get('sats_visible')} err={r.get('err', '-')}. Refusing to key. Check the GPS "
                f"antenna (sky view, bias), or raise --lock-timeout")
        if el - last_print >= 30.0 or polls == 1:
            last_print = el
            g = r.get("gnss") if isinstance(r.get("gnss"), dict) else {}
            print(f"[gps] waiting for GPSDO lock: {el:.0f}/{timeout:.0f} s  "
                  f"locked={r.get('gpsdo_locked')} bias={r.get('ant_bias')} fix={g.get('fix')} "
                  f"sats {g.get('sats_used')}/{g.get('sats_visible')}", flush=True)
        time.sleep(poll_s)


def gps_log(link) -> list:
    """Every lock check this Link made (one per init), oldest first."""
    if not hasattr(link, "gps_checks"):
        link.gps_checks = []
    return link.gps_checks


# An external reference (bench standard into REF IN and 1PPS IN, no GPS antenna)
# is either there or it is not, so the wait is short: this covers the card
# measuring the input after the init.
EXT_REF_TIMEOUT_S = 60.0


def wait_ext_ref(link, timeout=EXT_REF_TIMEOUT_S, poll_s=None, label="") -> dict:
    """Poll gps_status (after an init on clock external) until the reference is
    confirmed (ext_ref_present: on a G40 the HMC7044 locked to the 10 MHz and the card
    clock counts right against the 1 PPS, "10 MHz in use"; on an X40 the external
    select applied), a live 1 PPS edge (pps_present, measured) and, on a G40, the 1 PPS
    line on external: the GPS module makes a 1 PPS of its own, so pps_present alone
    does not show the external one.
    Returns a record like lock_record(); raises GpsLockError when any is missing
    at the timeout, so no stage keys on a free-running card that was meant to be on
    the bench standard."""
    poll_s = LOCK_POLL_S if poll_s is None else poll_s
    t0 = time.time()
    polls, r = 0, {}
    while True:
        st, r = gps_status(link)
        polls += 1
        el = time.time() - t0
        ref_ok = st == CmdStatus.OK and r.get("ext_ref_present") is True
        pps_ok = st == CmdStatus.OK and r.get("pps_present") is True
        line_ok = r.get("pps_line", "external") == "external"
        if ref_ok and pps_ok and line_ok:
            rec = {"clock": "external", "locked": True, "label": label, "at": iso_now(),
                   "t_lock_s": round(el, 1), "polls": polls,
                   "ref_clock": r.get("ref_clock"), "ext_ref_freq_hz": r.get("ext_ref_freq_hz"),
                   "carrier": r.get("carrier"), "pps_line": r.get("pps_line"),
                   "pps_present": True, "pps_source": r.get("pps_source"),
                   "ant_bias": r.get("ant_bias"), "sim": bool(r.get("sim"))}
            print(f"[ref] EXTERNAL REFERENCE CONFIRMED{' (' + label + ')' if label else ''} after "
                  f"{rec['t_lock_s']} s: select {r.get('ref_clock')}, carrier {r.get('carrier')}, "
                  f"1 PPS live",
                  flush=True)
            return rec
        if el >= timeout:
            raise GpsLockError(
                f"external reference not confirmed within {timeout:.0f} s"
                f"{' (' + label + ')' if label else ''}: gps_status {CmdStatus(st).name} "
                f"ref_clock={r.get('ref_clock')} ext_ref_present={r.get('ext_ref_present')} "
                f"({r.get('ext_ref_freq_hz')} Hz) carrier={r.get('carrier')} "
                f"pps_present={r.get('pps_present')} pps_line={r.get('pps_line')} "
                f"ten_mhz={r.get('ten_mhz')} clock_ppm={r.get('clock_ppm')} "
                f"err={r.get('err', '-')}. Refusing to key. Check the 10 MHz and 1 PPS cables "
                f"from the bench standard into the G40 and that the init asked for clock external")
        if polls == 1 or el - (polls - 1) * poll_s < 0.01:
            print(f"[ref] waiting for the external reference: {el:.0f}/{timeout:.0f} s  "
                  f"10 MHz={r.get('ext_ref_present')} ({r.get('ext_ref_freq_hz')} Hz) "
                  f"1 PPS={r.get('pps_present')}", flush=True)
        time.sleep(poll_s)


def gpsdo_gate(link, clock, timeout=LOCK_TIMEOUT_S, label="", poll_s=None) -> dict:
    """The pre-key gate after an init: on gpsdo, bias on + lock confirmed; on
    external, a measured 10 MHz and a live 1 PPS confirmed (wait_ext_ref); either
    raises GpsLockError rather than return a radio that is not on its reference.
    On internal just a record saying so. Each check is appended to gps_log(link)
    for the stage's result JSON."""
    if clock == "external":
        rec = wait_ext_ref(link, min(timeout, EXT_REF_TIMEOUT_S), poll_s=poll_s, label=label)
    elif clock != "gpsdo":
        rec = {"clock": clock, "locked": None, "label": label, "at": iso_now(),
               "note": "GPSDO not in use"}
    else:
        bias = ensure_ant_bias(link)
        rec = wait_gpsdo_lock(link, timeout, poll_s=poll_s, label=label)
        rec["bias_enabled_now"] = bias["enabled_now"]
    gps_log(link).append(rec)
    return rec


def gps_summary(link, clock) -> dict:
    """What a stage puts under "gps" in its result JSON: the clock, the last
    lock check and every check (one per init)."""
    checks = list(gps_log(link))
    last = checks[-1] if checks else None
    return {"clock": clock, "locked": (last or {}).get("locked"),
            "t_lock_s": (last or {}).get("t_lock_s"), "ppm": (last or {}).get("ppm"),
            "sats_used": (last or {}).get("sats_used"), "cno_max": (last or {}).get("cno_max"),
            "checks": checks}


def gps_header(clock, rec) -> str:
    """One markdown line for a report header: clock, lock time, ppm, sats, C/N0."""
    if clock != "gpsdo":
        return f"clock **{clock}** (GPSDO not in use)"
    if not rec or not rec.get("locked"):
        return "clock **gpsdo**, lock **NOT confirmed**"
    return (f"clock **gpsdo**, locked in {fnum(rec.get('t_lock_s'), 1)} s, "
            f"{fnum(rec.get('ppm'), 4)} ppm, sats used {rec.get('sats_used')}, "
            f"C/N0 max {fnum(rec.get('cno_max'), 0)} / median {fnum(rec.get('cno_median'), 0)} dB-Hz"
            + (" (simulated)" if rec.get("sim") else ""))


# init by port
def port_init_params(rx_port, tx_port, rate, center, rx_gain, tx_atten, kind="g40",
                     clock="internal"):
    """The exact init the /operator console sends for an (RX port, TX port)
    pick: same card then dual chan_mode FDD, different cards then single mode each
    with tx_card. tx_port None = RX only. clock = the console's Clock pick
    (top-level clock_source)."""
    rxp = PORT_MAP[rx_port]
    extras = {"card": rxp["card"], "rf_bandwidth_hz": int(rate * 0.8),
              "tx_center_freq": center}
    p = {"kind": kind, "sample_rate": int(rate), "center_freq": center,
         "rx_gain": rx_gain, "tx_gain": tx_atten, "clock_source": clock,
         "rx_antenna": rxp["rx"], "extras": extras,
         "psd_nfft": 4096, "psd_window": "hann", "psd_buffer": 65536}
    if tx_port is not None:
        txp = PORT_MAP[tx_port]
        extras["tx_card"] = txp["card"]
        extras["chan_mode"] = "dual" if rxp["card"] == txp["card"] else "single"
        p["tx_antenna"] = txp["tx"]
    return p


def init_ports(link, rx_port, tx_port, rate, center, rx_gain, tx_atten,
               kind="g40", start_rx=True, timeout=120.0, clock="internal",
               lock_timeout=LOCK_TIMEOUT_S, gate=True):
    """Init through the service with the operator console's mapping. Returns
    (ack, seconds). Does not raise on REJECTED/FAILED (the port matrix expects
    two refusals); start_rx only follows an OK init. On clock gpsdo an OK init
    is followed by gpsdo_gate (bias on + lock), which raises GpsLockError
    rather than return an unlocked radio; seconds is the init alone. gate=False
    skips the lock wait for an init that will not key (PORT-G-18 re-init)."""
    p = port_init_params(rx_port, tx_port, rate, center, rx_gain, tx_atten, kind, clock)
    rel = ("same card, dual-mode FDD" if tx_port and PORT_MAP[rx_port]["card"] ==
           PORT_MAP[tx_port]["card"] else "separate cards")
    print(f"[init] RX {rx_port} ({p['rx_antenna']}@card{p['extras']['card']})"
          + (f"  TX {tx_port} ({p['tx_antenna']}@card{p['extras']['tx_card']})  "
             f"[{rel}]" if tx_port else "  RX only")
          + f"  {rate/1e6:.2f} MSps  atten {tx_atten} dB  gain {rx_gain}  clock {clock}", flush=True)
    t0 = time.time()
    a = safe_cmd(link, "init", p, timeout=timeout)
    dt = round(time.time() - t0, 2)
    if a.status == CmdStatus.OK:
        print(f"[init] OK in {dt}s: chan_mode={a.result.get('chan_mode')} "
              f"achieved={a.result.get('achieved_rate')} Hz "
              f"libsidekiq={a.result.get('libsidekiq')}", flush=True)
        link.rx_conn, link.tx_conn = rx_port, tx_port or "-"
        if gate:
            gpsdo_gate(link, clock, lock_timeout, label=f"RX {rx_port} TX {tx_port or '-'}")
        if start_rx:
            safe_cmd(link, "start_rx", {}, timeout=30)
    else:
        print(f"[init] {CmdStatus(a.status).name} in {dt}s: {ack_err(a)}", flush=True)
    return a, dt


def teardown(link):
    """stop_tx + stop_rx + close, never raising."""
    for c in ("stop_tx", "stop_rx", "close"):
        safe_cmd(link, c, {}, timeout=30)


def gps_preflight(link, clock, timeout=LOCK_TIMEOUT_S, rx_port="TRx1A",
                  rate=RATE_NARROW, center=CENTER) -> dict:
    """Run-level pre-flight before any stage keys: on gpsdo, an RX-only init on
    the GPSDO (nothing transmits), antenna bias on + confirmed, lock polled for
    up to `timeout` s, then close. Returns the lock record (t_lock_s counted
    from the bias check, t_init_to_lock_s from the init); raises GpsLockError
    when lock does not come (or the init fails). Any other clock: a record saying the GPSDO is not used."""
    if clock == "external":
        # bench standard on REF IN and 1PPS IN: same RX-only init, then the
        # init_ports gate confirms the 10 MHz and the 1 PPS (wait_ext_ref)
        print(f"[preflight] clock external: RX-only init on {rx_port} (nothing keyed), "
              f"confirm the 10 MHz and 1 PPS from the bench standard", flush=True)
        try:
            a, _dt = init_ports(link, rx_port, None, rate, center, 200, 30, start_rx=False,
                                clock="external", lock_timeout=timeout, timeout=120.0)
            if a.status != CmdStatus.OK:
                raise GpsLockError(f"pre-flight RX-only init on external failed: "
                                   f"{CmdStatus(a.status).name} {ack_err(a)}. With no reference "
                                   f"on REF IN the card can hang at init: check the cables")
            rec = dict(gps_log(link)[-1])
            rec.update(label="preflight")
            return rec
        finally:
            teardown(link)
    if clock != "gpsdo":
        return {"clock": clock, "locked": None, "at": iso_now(), "note": "GPSDO not in use"}
    print(f"[preflight] clock gpsdo: RX-only init on {rx_port} (nothing keyed), "
          f"antenna bias on, wait up to {timeout:.0f} s for GPSDO lock", flush=True)
    try:
        t0 = time.time()
        bias = ensure_ant_bias(link)        # before init: the receiver starts acquiring now
        a, _dt = init_ports(link, rx_port, None, rate, center, 200, 30, start_rx=False,
                            clock="gpsdo", lock_timeout=timeout, timeout=120.0)
        if a.status != CmdStatus.OK:
            raise GpsLockError(f"pre-flight RX-only init on gpsdo failed: "
                               f"{CmdStatus(a.status).name} {ack_err(a)}")
        rec = dict(gps_log(link)[-1])
        rec.update(label="preflight", t_init_to_lock_s=rec.get("t_lock_s"),
                   t_lock_s=round(time.time() - t0, 1), bias_enabled_now=bias["enabled_now"])
        return rec
    finally:
        teardown(link)


# telemetry
class Telem:
    """What the service pushed during one collection window."""
    def __init__(self):
        self.detections = []        # [{idx, t, f, bw, snr, dur}]
        self.bands = {}             # band name: last WATCH_UPDATE row
        self.messages = {}          # decoded message: max decoded_n seen
        self.status = None          # last STATUS as a dict
        self.n_watch = 0

    def decoded_set(self):
        return set(self.messages)

    def det_near(self, f_hz, within_hz=None):
        """The detection nearest f_hz (optionally within +-within_hz), or None."""
        best = None
        for d in self.detections:
            df = abs(d["f"] - f_hz)
            if within_hz is not None and df > within_hz:
                continue
            if best is None or df < best[0]:
                best = (df, d)
        return best[1] if best else None


def drain(link):
    link.s.setblocking(False)
    try:
        while True:
            link.s.recvfrom(65535)
    except (BlockingIOError, OSError):
        pass
    finally:
        link.s.settimeout(1.0)


def collect(link, seconds, telem: Telem | None = None, stop_on_msg=None) -> Telem:
    """Listen for `seconds` and fold DETECTION / WATCH_UPDATE / STATUS into a
    Telem. stop_on_msg: return early once that decoded message is seen."""
    t = telem or Telem()
    t0 = time.time()
    link.s.settimeout(0.5)
    while time.time() - t0 < seconds:
        try:
            data, _ = link.s.recvfrom(65535)
        except socket.timeout:
            continue
        except OSError:
            break
        try:
            mt, _seq, m = proto.decode(data)
        except proto.ProtoError:
            continue
        if mt == MsgType.DETECTION:
            # "t" is the service's burst-start stamp on the radio's clock;
            # "rx" is when the event reached this host (local clock). Only
            # same-clock differences are latencies (G40 and ground clocks have
            # been ~5 s apart).
            t.detections.append({"idx": int(m.det_index), "t": float(m.t_unix),
                                 "f": float(m.freq_hz), "bw": float(m.bw_hz),
                                 "snr": float(m.snr_db), "dur": float(m.duration_ms),
                                 "rx": time.time()})
        elif mt == MsgType.WATCH_UPDATE:
            t.n_watch += 1
            for b in m.bands:
                name = str(b.get("n", b.get("f", "?")))
                t.bands[name] = dict(b)
                msg = str(b.get("msg", ""))
                if msg:
                    t.messages[msg] = max(t.messages.get(msg, 0), int(b.get("dn", 0)))
                    if stop_on_msg is not None and msg == stop_on_msg:
                        link.s.settimeout(1.0)
                        return t
        elif mt == MsgType.STATUS:
            t.status = {"fps": int(m.fps), "overruns": int(m.overruns),
                        "underruns": int(m.underruns), "radio_state": int(m.radio_state),
                        "flags": int(m.flags), "rx_gain_idx": int(m.rx_gain_idx),
                        "center_hz": float(m.center_hz),
                        "sample_rate_hz": float(m.sample_rate_hz)}
    link.s.settimeout(1.0)
    return t


def push_file(link, name, data: bytes, timeout=30.0, file_id=None):
    """Push `data` to the service's inbox as data/inbox/<basename(name)> with
    the FILE_OFFER / FILE_CHUNK / FILE_ACK transfer (cumulative ack, window 8).
    Returns the service-relative path to hand to gen_scenario {"spec_path"} or
    set_plan. A CMD datagram is capped at 1400 B, so any spec bigger than
    ~1.2 kB (e.g. the 25-signal DET-09 scenario) has to come in this way."""
    from dpmg.link.proto import FileOffer, FileChunk, FileAck
    n = proto.FILE_CHUNK_PAYLOAD
    chunks = [data[i:i + n] for i in range(0, len(data), n)] or [b""]
    fid = int(file_id if file_id is not None else (int(time.time() * 10) & 0x7FFFFFFF))
    name = os.path.basename(name)
    offer = proto.encode(FileOffer(fid, len(chunks), len(data), name), 0)
    hc = None
    t_end = time.time() + timeout
    link.s.settimeout(0.3)

    def _acks():
        nonlocal hc
        try:
            while True:
                d, _ = link.s.recvfrom(65535)
                try:
                    mt, _seq, m = proto.decode(d)
                except proto.ProtoError:
                    continue
                if mt == MsgType.FILE_ACK and m.file_id == fid:
                    hc = max(hc if hc is not None else -1, int(m.highest_contig))
                    link.s.settimeout(0.05)
        except (socket.timeout, BlockingIOError, OSError):
            pass
        finally:
            link.s.settimeout(0.3)

    while hc is None:
        if time.time() > t_end:
            raise SystemExit("push_file: FILE_OFFER never acked")
        link.s.sendto(offer, link.addr)
        _acks()
    while hc < len(chunks) - 1:
        if time.time() > t_end:
            raise SystemExit(f"push_file: stalled at chunk {hc + 1}/{len(chunks)}")
        base = hc + 1
        for i in range(base, min(base + 8, len(chunks))):
            link.s.sendto(proto.encode(FileChunk(fid, i, chunks[i]), 0), link.addr)
        _acks()
    link.s.settimeout(1.0)
    return f"data/inbox/{name}"


def status_snapshot(link, seconds=3.0):
    """Last STATUS heartbeat seen in `seconds` (fps, over/underruns...), or None."""
    return collect(link, seconds).status


def key_tone(link, offset_hz, hold_s=2.0, after_s=2.0, ampl=0.5, name="chk_tone",
             prf=None, duty=None, seconds=0.5):
    """Build a CW tone at offset_hz (gen_test_waveform), key it for hold_s,
    stop, then listen after_s for the burst to close (DETECTION events are
    emitted when a burst ends, so a steady tone reports only after stop_tx).
    Returns (t_on, Telem, gen_ack)."""
    p = {"kind": "tone", "freq": float(offset_hz), "seconds": float(seconds),
         "ampl": float(ampl), "name": name, "load_tx": True}
    if prf:
        p["prf"] = float(prf)
        p["duty"] = float(duty if duty is not None else 0.5)
    g = safe_cmd(link, "gen_test_waveform", p, timeout=60)
    if g.status != CmdStatus.OK:
        return None, Telem(), g
    drain(link)
    t_on = time.time()
    st = safe_cmd(link, "start_tx", {"mode": "CONTINUOUS"}, timeout=30)
    if st.status != CmdStatus.OK:
        return t_on, Telem(), st
    tel = collect(link, hold_s)
    safe_cmd(link, "stop_tx", {}, timeout=30)
    collect(link, after_s, tel)
    return t_on, tel, g
