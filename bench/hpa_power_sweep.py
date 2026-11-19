"""hpa_power_sweep.py: HPA output power sweep on the bench, read on a spectrum
analyzer at the coupled port of a directional coupler.

Bench chain (no antenna, no T/R switch, no LNA):
  G40 TX port then ZB3PD-63-S+ combiner then HPA then ZGDC30-372HP+ then dummy load
                                                  coupled port (30 dB down): analyzer

What it does, in this order:
  1. prints the plan: every (frequency, TX attenuation) step with the expected
     coupled-port level from payload_budget (profile P2b by default) and the
     bench coupler's coupling table; steps that would overdrive the HPA input
     are dropped
  2. GPIO check with no RF: keys the HPA line through the payload guard
     (payload_key) or, with no guard, through frontend_set; holds; asks whether
     the analyzer floor rose; unkeys and confirms the line is back in shutdown
  3. inits the G40 (TX on one of TRx1A, TRx2A, TRx1B; RX on TRx2B, nothing
     connected), loads a tone, then for each step sets the TX attenuation,
     starts a CONTINUOUS TX (the TX run keys the HPA through the guard), waits,
     asks for the analyzer marker reading, stops TX and checks the HPA line
     dropped
  4. closes the radio, leaves the front end SAFE, prints measured against
     expected and writes a CSV

Steps run lowest power first (highest attenuation).  A reading more than
--max-over dB above the expected level aborts the sweep, since the amplifier is
then giving more than the model says and the analyzer may be at risk.

  python3 bench/hpa_power_sweep.py --dry-run
  python3 bench/hpa_power_sweep.py --host 192.168.0.23 --freqs 915,2200 --attens 41.75,35,30,25,20,15,10
  python3 bench/hpa_power_sweep.py --gpio-only
  python3 bench/hpa_power_sweep.py --no-prompt --hold 3     (keys each step, no readings)
  python3 bench/hpa_power_sweep.py --host 192.168.0.23 --analyzer 192.168.10.3
        (FieldFox on LAN: the marker reading and a screenshot per step, no typing)

The analyzer input: at 0 dB TX attenuation the coupled port carries about
+20 dBm.  Keep the analyzer's own input attenuator in, or put a pad on the
coupled port and pass its value as --extra-loss so the expected column matches.

Do not run this with the HPA output into the T/R switch and the LNA: the
--lna-floor-off bench config has no LNA protection.
"""
from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import os
import socket
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from dpmg.link import proto                                   # noqa: E402
from dpmg.link.proto import Cmd, Hello, MsgType               # noqa: E402
from dpmg.payload import payload_budget as pb                 # noqa: E402

try:
    from dpmg.deploy_config import G40_HOST, CMD_PORT
except Exception:                                             # pragma: no cover
    G40_HOST, CMD_PORT = "", 47810

# jack: (NV100 card, RX handle, TX handle), as the operator console names them
PORTS = {"TRx1A": (0, "RXA1", "TXA1"), "TRx2A": (0, "RXA2", "TXA2"),
         "TRx1B": (1, "RXA1", "TXA1"), "TRx2B": (1, "RXA2", "TXA2")}
# results go outside the source tree (output/ is not tracked); DPMG_BENCH_RESULTS overrides
DEFAULT_OUT_DIR = os.path.join(os.environ.get("DPMG_BENCH_RESULTS") or os.path.join(ROOT, "output", "bench"),
                               f"{_dt.date.today():%Y-%m-%d}_hpa_bench_p2b", "sweeps")


class Radio:
    """Minimal UDP command client, the same exchange the GUI uses."""

    def __init__(self, host: str, port: int):
        self.addr = (host, port)
        self.s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.s.bind(("0.0.0.0", 0))
        self.s.settimeout(0.2)
        self.seq = 0
        self.acks = {}
        self.cid = int(time.time()) % 100000 * 10
        self.status = None
        self.hello_ack = None

    def _send(self, m):
        self.seq += 1
        self.s.sendto(proto.encode(m, self.seq), self.addr)

    def _pump(self, deadline):
        while time.monotonic() < deadline:
            try:
                data, _ = self.s.recvfrom(4096)
            except socket.timeout:
                continue
            try:
                mt, _, msg = proto.decode(data)
            except proto.ProtoError:
                continue
            if mt == MsgType.HELLO_ACK:
                self.hello_ack = msg
            elif mt == MsgType.CMD_ACK:
                self.acks[msg.cmd_id] = msg
            elif mt == MsgType.STATUS:
                self.status = msg

    def hello(self):
        end = time.monotonic() + 6
        while self.hello_ack is None and time.monotonic() < end:
            self._send(Hello("GCS-HPA-SWEEP"))
            self._pump(time.monotonic() + 0.4)
        if self.hello_ack is None:
            raise SystemExit(f"no HELLO_ACK from {self.addr[0]}:{self.addr[1]}")

    def cmd(self, name, params=None, timeout=8.0):
        cid = self.cid
        self.cid += 1
        c = Cmd(cid, name, params or {})
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self._send(c)
            self._pump(time.monotonic() + 0.25)
            if cid in self.acks:
                a = self.acks[cid]
                return a.status, (a.result or {})
        raise TimeoutError(name)

    def ok(self, name, params=None, timeout=8.0):
        st, r = self.cmd(name, params, timeout)
        if st != 0:
            raise RuntimeError(f"{name}: {r.get('err') or r}")
        return r

    def frontend(self) -> dict:
        st, r = self.cmd("frontend_status")
        return r.get("frontend") or {}

    def payload(self) -> dict:
        st, r = self.cmd("payload_status")
        return r or {}


def _floats(text: str) -> list:
    return [float(x) for x in text.replace(";", ",").split(",") if x.strip()]


def plan_steps(data, prof, freqs, attens, backoff_db, extra_loss_db, coupler, hpa_in_margin_db):
    """Expected level per step; steps that would push the HPA input past its
    maximum less hpa_in_margin_db are marked skip."""
    steps = []
    for f in freqs:
        for att in sorted(attens, reverse=True):
            x = pb.coupled_port_expected(data, prof, f, att, backoff_db, extra_loss_db, coupler)
            mx = x["hpa_max_input_dbm"]
            x["skip"] = (mx is not None) and (x["hpa_in_dbm"] > mx - hpa_in_margin_db)
            steps.append(x)
    return steps


def print_plan(steps, tol_db):
    print(f"\n{'freq MHz':>9} {'atten':>6} {'G40 out':>8} {'HPA in':>7} {'HPA out':>8} {'W':>7} "
          f"{'coupling':>8} {'expected':>9}  note")
    for x in steps:
        note = "SKIP: HPA input too high" if x["skip"] else (x["region"] or "")
        print(f"{x['freq_mhz']:9.1f} {x['tx_atten_db']:6.2f} {x['g40_out_dbm']:8.1f} {x['hpa_in_dbm']:7.1f} "
              f"{x['hpa_out_dbm']:8.1f} {x['hpa_out_w']:7.1f} {x['coupling_db']:8.2f} "
              f"{x['coupled_dbm']:9.1f}  {note}")
    print(f"expected = HPA out - coupling - extra loss; pass band is +/- {tol_db:g} dB\n")


def _ask(prompt: str, no_prompt: bool, default: str = "") -> str:
    if no_prompt:
        return default
    try:
        return input(prompt).strip()
    except EOFError:
        return default


def _lines_safe(fe: dict) -> bool:
    return not (fe.get("pa") or {}).get("keyed") and not (fe.get("tr") or {}).get("tx")


def gpio_check(r: Radio, hold_s: float, no_prompt: bool) -> dict:
    """Key the HPA line with no RF, hold, unkey.  Through the guard when there is
    one (payload_key, which also drives the switch line), else frontend_set."""
    out = {"keyed": False, "released": False, "floor_rose": None, "via": None}
    pl = r.payload()
    guard = bool(pl.get("configured"))
    out["via"] = "payload_key" if guard else "frontend_set"
    fe = r.frontend()
    if not _lines_safe(fe):
        raise RuntimeError("front end not in the safe state before the GPIO check; "
                           "press SAFE in the Payload tab first")
    print(f"GPIO check: keying the HPA line through {out['via']} for {hold_s:g} s (no RF)")
    if guard:
        st, res = r.cmd("payload_key", {"on": True}, timeout=15)
        if st != 0:
            raise RuntimeError(f"payload_key on refused: {res.get('err')}")
    else:
        r.ok("frontend_set", {"tr": "tx"})
        r.ok("frontend_set", {"pa": True})
    t0 = time.monotonic()
    try:
        while time.monotonic() - t0 < hold_s:
            fe = r.frontend()
            pa, tr = fe.get("pa") or {}, fe.get("tr") or {}
            out["keyed"] = bool(pa.get("keyed"))
            print(f"  t+{time.monotonic()-t0:4.1f}s HPA line keyed={pa.get('keyed')} level={pa.get('level')} "
                  f"switch tx={tr.get('tx')} watchdog_trips={fe.get('watchdog_trips')}")
            time.sleep(1.0)
        ans = _ask("  Did the analyzer noise floor rise while keyed? [y/n/skip]: ", no_prompt, "skip").lower()
        out["floor_rose"] = None if ans in ("", "skip", "s") else ans.startswith("y")
    finally:
        if guard:
            r.cmd("payload_key", {"on": False}, timeout=15)
        else:
            r.cmd("frontend_set", {"safe": True})
        time.sleep(0.3)
        fe = r.frontend()
        out["released"] = _lines_safe(fe)
        print(f"  released: HPA line keyed={(fe.get('pa') or {}).get('keyed')} switch tx={(fe.get('tr') or {}).get('tx')}")
    if not out["keyed"]:
        print("  warning: the service never reported the HPA line keyed")
    if not out["released"]:
        raise RuntimeError("HPA line did not release after the GPIO check; stop here")
    return out


def wait_clock(r: Radio, clock: str, timeout_s: float, no_prompt: bool) -> None:
    """gpsdo: wait for the GPSDO lock; external: wait for the reference present."""
    if clock == "internal":
        return
    t0 = time.monotonic()
    last = {}
    while time.monotonic() - t0 < timeout_s:
        st, g = r.cmd("gps_status")
        last = g
        flat = json.dumps(g)
        if clock == "gpsdo" and ('"locked": true' in flat):
            print(f"  GPSDO locked after {time.monotonic()-t0:.0f} s")
            return
        if clock == "external" and ('"ext_ref_present": true' in flat):
            print(f"  external reference present after {time.monotonic()-t0:.0f} s")
            return
        time.sleep(2.0)
    print(f"  clock {clock}: no lock after {timeout_s:.0f} s: {json.dumps(last)[:300]}")
    if _ask("  continue anyway? [y/N]: ", no_prompt, "n").lower() != "y":
        raise RuntimeError(f"clock {clock} not locked")


def open_analyzer(a):
    """FieldFox client from --analyzer host[:port], or None."""
    if not a.analyzer:
        return None
    from bench.fieldfox import FieldFox
    host, _, port = a.analyzer.partition(":")
    fx = FieldFox(host, int(port or 5025))
    print(f"analyzer: {fx.idn}")
    return fx


def shots_dir_for(out_csv: str) -> str:
    d = os.path.dirname(os.path.abspath(out_csv))
    base = os.path.dirname(d) if os.path.basename(d) == "sweeps" else d
    return os.path.join(base, "screenshots")


def run_sweep(r: Radio, a, steps, fx=None, shots_dir=None) -> list:
    rxc, rxh, _ = PORTS[a.rx_port]
    txc, _, txh = PORTS[a.tx_port]
    live = [x for x in steps if not x["skip"]]
    if not live:
        raise RuntimeError("every step was skipped")
    f0 = live[0]["freq_mhz"] * 1e6
    att0 = live[0]["tx_atten_db"]
    chan_mode = "dual" if rxc == txc else "single"
    p = {"kind": "g40", "sample_rate": a.rate, "center_freq": f0, "rx_gain": 230, "tx_gain": att0,
         "clock_source": a.clock, "rx_antenna": rxh, "tx_antenna": txh,
         "extras": {"card": rxc, "tx_card": txc, "chan_mode": chan_mode,
                    "rf_bandwidth_hz": int(a.rate * 0.8), "tx_center_freq": f0},
         "psd_nfft": 4096, "psd_window": "hann", "psd_buffer": 65536}
    print(f"init: TX {a.tx_port} (card {txc} {txh}), RX {a.rx_port} (card {rxc} {rxh}), "
          f"{a.rate/1e6:g} MSps, {f0/1e6:g} MHz, clock {a.clock}, TX atten {att0:g} dB")
    res = r.ok("init", p, timeout=120)
    print(f"  init ok: chan_mode {res.get('chan_mode')} rate {res.get('achieved_rate')}")
    wait_clock(r, a.clock, a.clock_timeout, a.no_prompt)
    backoff = -20.0 * __import__("math").log10(a.ampl)
    res = r.ok("gen_test_waveform", {"kind": "tone", "freq": a.tone_hz, "seconds": 1.0,
                                     "ampl": a.ampl, "name": "hpa_sweep_tone", "load_tx": True}, timeout=20)
    print(f"  tone loaded: {a.tone_hz/1e3:g} kHz offset, amplitude {a.ampl:g} ({backoff:.1f} dB backoff)")
    rows = []
    cur_f = f0
    abort = None
    n = 0
    try:
        for x in live:
            n += 1
            f_hz = x["freq_mhz"] * 1e6
            if abs(f_hz - cur_f) > 1.0:
                r.ok("set_tx_center", {"hz": f_hz})
                cur_f = f_hz
                time.sleep(0.5)
            r.ok("set_tx_atten", {"qdb": int(round(x["tx_atten_db"] * 4))})
            print(f"\n[{x['freq_mhz']:g} MHz, TX atten {x['tx_atten_db']:g} dB] expected at the coupled port "
                  f"{x['coupled_dbm']:+.1f} dBm (HPA out {x['hpa_out_dbm']:.1f} dBm, {x['hpa_out_w']:.1f} W"
                  f"{', ' + x['region'] if x['region'] else ''})")
            st, res = r.cmd("start_tx", {"mode": "CONTINUOUS"}, timeout=15)
            if st != 0:
                print(f"  start_tx refused: {res.get('err')}")
                rows.append({**x, "measured_dbm": None, "note": f"refused: {res.get('err')}"})
                continue
            time.sleep(1.0)
            fe = r.frontend()
            keyed = bool((fe.get("pa") or {}).get("keyed"))
            print(f"  TX running, HPA line keyed={keyed}")
            reading = None
            note = ""
            shot = ""
            an_hz = None
            try:
                if fx is not None:
                    fx.setup(f_hz + a.tone_hz, a.span, a.rbw, a.ref, a.an_atten)
                    reading, an_hz = fx.peak(sweeps=2)
                    print(f"  analyzer peak {reading:+.2f} dBm at {an_hz/1e6:.6f} MHz")
                    if shots_dir:
                        os.makedirs(shots_dir, exist_ok=True)
                        shot = os.path.join(shots_dir, f"{n:02d}_{x['freq_mhz']:g}MHz_{x['tx_atten_db']:g}dB.png")
                        fx.screenshot(shot)
                        print(f"  screenshot {shot}")
                    if not a.no_prompt and a.hold > 2.0:
                        time.sleep(a.hold - 2.0)
                elif a.no_prompt:
                    time.sleep(max(0.0, a.hold - 1.0))
                else:
                    ans = _ask("  analyzer marker (dBm), blank = no reading, q = stop the sweep: ", False)
                    if ans.lower() == "q":
                        abort = "stopped by the operator"
                    elif ans:
                        try:
                            reading = float(ans)
                        except ValueError:
                            note = f"unreadable entry {ans!r}"
            finally:
                r.cmd("stop_tx", {}, timeout=10)
                time.sleep(0.3)
                fe = r.frontend()
                if not _lines_safe(fe):
                    time.sleep(1.0)
                    fe = r.frontend()
                dropped = _lines_safe(fe)
                print(f"  TX stopped, HPA line released={dropped}")
                if not dropped:
                    abort = "HPA line did not release after stop_tx"
            rows.append({**x, "keyed": keyed, "measured_dbm": reading, "analyzer_hz": an_hz,
                         "screenshot": os.path.basename(shot) if shot else "", "note": note})
            if reading is not None:
                d = reading - x["coupled_dbm"]
                verdict = "PASS" if abs(d) <= a.tol else "FAIL"
                print(f"  measured {reading:+.1f} dBm, delta {d:+.1f} dB: {verdict}")
                if d > a.max_over:
                    abort = (f"reading {d:+.1f} dB above the expected level (limit {a.max_over:g} dB): "
                             "more power than modelled, protect the analyzer")
            if abort:
                print(f"\nSWEEP STOPPED: {abort}")
                break
    finally:
        try:
            r.cmd("stop_tx", {}, timeout=10)
        except Exception:
            pass
        try:
            r.cmd("frontend_set", {"safe": True})
        except Exception:
            pass
        try:
            r.cmd("close", {}, timeout=30)
        except Exception:
            pass
        if fx is not None:
            try:
                fx.resume()
                fx.close()
            except Exception:
                pass
        fe = r.frontend()
        print(f"\nradio closed; HPA line keyed={(fe.get('pa') or {}).get('keyed')} "
              f"switch tx={(fe.get('tr') or {}).get('tx')} RX atten {(r.payload().get('atten') or {}).get('att_db')} dB")
    return rows


def write_csv(path: str, rows: list, tol_db: float) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cols = ["freq_mhz", "tx_atten_db", "backoff_db", "g40_out_dbm", "tx_combiner_db", "hpa_in_dbm",
            "hpa_out_dbm", "hpa_out_w", "region", "coupling_db", "extra_loss_db", "coupled_dbm",
            "measured_dbm", "delta_db", "verdict", "analyzer_hz", "screenshot", "keyed", "note"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for x in rows:
            m = x.get("measured_dbm")
            d = None if m is None else m - x["coupled_dbm"]
            v = "" if d is None else ("PASS" if abs(d) <= tol_db else "FAIL")
            vals = [x.get(c) if c not in ("delta_db", "verdict") else (d if c == "delta_db" else v)
                    for c in cols]
            w.writerow([round(z, 3) if isinstance(z, float) else z for z in vals])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=os.environ.get("DPMG_G40_HOST", G40_HOST) or None,
                    required=not os.environ.get("DPMG_G40_HOST", G40_HOST))
    ap.add_argument("--port", type=int, default=int(os.environ.get("DPMG_CMD_PORT", CMD_PORT)))
    ap.add_argument("--profiles", default=os.path.join(ROOT, "payload_profiles.yaml"))
    ap.add_argument("--profile", default="P2b")
    ap.add_argument("--coupler", default=pb.BENCH_COUPLER, help="bench coupler component name")
    ap.add_argument("--tx-port", default="TRx1A", choices=sorted(PORTS))
    ap.add_argument("--rx-port", default="TRx2B", choices=sorted(PORTS))
    ap.add_argument("--freqs", default="2200", help="MHz, comma separated")
    ap.add_argument("--attens", default="41.75,35,30,25,20,15,10", help="G40 TX attenuation dB, comma separated")
    ap.add_argument("--ampl", type=float, default=0.5, help="tone amplitude, full scale 1.0 (0.5 = 6 dB backoff)")
    ap.add_argument("--tone-hz", type=float, default=100e3, help="tone offset from the TX centre")
    ap.add_argument("--rate", type=float, default=1.92e6)
    ap.add_argument("--clock", default="internal", choices=("internal", "gpsdo", "external"))
    ap.add_argument("--clock-timeout", type=float, default=90.0)
    ap.add_argument("--extra-loss", type=float, default=0.0, help="pad or cable on the coupled port, dB")
    ap.add_argument("--tol", type=float, default=3.0, help="pass band around the expected level, dB")
    ap.add_argument("--max-over", type=float, default=6.0, help="abort when a reading is this far above expected")
    ap.add_argument("--hpa-in-margin", type=float, default=3.0, help="keep the HPA input this far under its max")
    ap.add_argument("--hold", type=float, default=4.0, help="seconds keyed per step with --no-prompt, and the GPIO hold")
    ap.add_argument("--no-prompt", action="store_true", help="no readings asked, just key each step")
    ap.add_argument("--yes", action="store_true", help="skip the bench confirmation")
    ap.add_argument("--gpio-only", action="store_true", help="only the no-RF GPIO check")
    ap.add_argument("--skip-gpio", action="store_true", help="straight to the sweep")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, touch no radio")
    ap.add_argument("--out", default=None, help="CSV path (default output/bench/<today>_hpa_bench_p2b/sweeps/hpa_sweep_<time>.csv)")
    ap.add_argument("--analyzer", default=None, help="FieldFox host[:port] on LAN; readings and screenshots taken by SCPI")
    ap.add_argument("--span", type=float, default=200e3, help="analyzer span around the tone, Hz")
    ap.add_argument("--rbw", type=float, default=10e3, help="analyzer RBW, Hz")
    ap.add_argument("--ref", type=float, default=20.0, help="analyzer reference level, dBm")
    ap.add_argument("--an-atten", type=float, default=20.0, help="analyzer input attenuation, dB")
    ap.add_argument("--no-screens", action="store_true", help="no analyzer screenshots")
    ap.add_argument("--g40-max-dbm", type=float, default=None,
                    help="override the G40 max output (dBm) used by the model, from a calibration step")
    a = ap.parse_args(argv)

    if a.tx_port == a.rx_port:
        ap.error("TX and RX ports must differ")
    data = pb.apply_g40_max(pb.load_profiles(a.profiles), a.g40_max_dbm)
    prof = pb.profile(data, a.profile)
    freqs, attens = _floats(a.freqs), _floats(a.attens)
    backoff = -20.0 * __import__("math").log10(a.ampl)
    steps = plan_steps(data, prof, freqs, attens, backoff, a.extra_loss, a.coupler, a.hpa_in_margin)
    print(f"profile {a.profile}: {prof.label}" + (f"  [G40 max output overridden to {a.g40_max_dbm:g} dBm]" if a.g40_max_dbm is not None else ""))
    print(f"bench coupler {a.coupler}, extra loss on the coupled port {a.extra_loss:g} dB, "
          f"tone amplitude {a.ampl:g} ({backoff:.1f} dB backoff)")
    print_plan(steps, a.tol)
    if a.dry_run:
        return 0

    r = Radio(a.host, a.port)
    r.hello()
    r._pump(time.monotonic() + 1.0)
    fe = r.frontend()
    pl = r.payload()
    g = pl.get("guard") or {}
    rs = getattr(r.status, "radio_state", None) if r.status else None
    print(f"radio {a.host}: state {rs}, front end {fe.get('frontend')}, HPA line keyed={(fe.get('pa') or {}).get('keyed')}, "
          f"switch tx={(fe.get('tr') or {}).get('tx')}")
    if pl.get("configured"):
        print(f"guard: profile {g.get('profile')}, min safe RX atten {g.get('min_db')} dB, "
              f"RCDAT reads {(pl.get('atten') or {}).get('att_db')} dB, LNA floor "
              f"{'OFF (bench config)' if g.get('lna_floor_off') else g.get('lna_min_tx_atten_db')}")
        if g.get("lna_min_tx_atten_db") and not g.get("lna_floor_off"):
            lo = min(x["tx_atten_db"] for x in steps if not x["skip"])
            if lo < float(g["lna_min_tx_atten_db"]):
                print(f"Note: the guard will refuse steps below {g['lna_min_tx_atten_db']} dB TX attenuation "
                      "(LNA floor). For the coupler bench load the lna_floor: false front-end file.")
    else:
        print("guard: not configured (TX runs key the HPA line with no RX protection checks)")
    if rs not in (0, None):
        print(f"radio state {rs} is not idle; close it from the GUI first")
        return 2
    if not a.yes:
        print("\nBench check: HPA output into the directional coupler and a dummy load rated for the HPA, "
              "analyzer on the coupled port with its input attenuator in, nothing on the RX jacks, "
              "HPA DC on, one G40 TX port cabled to the combiner and its other inputs terminated.")
        if _ask("Proceed? [y/N]: ", False, "n").lower() != "y":
            print("stopped before touching the radio")
            return 1

    result = {"gpio": None, "rows": []}
    if not a.skip_gpio:
        result["gpio"] = gpio_check(r, a.hold, a.no_prompt)
    if a.gpio_only:
        print(json.dumps(result["gpio"]))
        return 0
    out = a.out or os.path.join(DEFAULT_OUT_DIR, f"hpa_sweep_{_dt.datetime.now():%Y%m%d_%H%M%S}.csv")
    fx = open_analyzer(a)
    rows = run_sweep(r, a, steps, fx, None if (a.no_screens or fx is None) else shots_dir_for(out))
    write_csv(out, rows, a.tol)
    print(f"\n{'freq MHz':>9} {'atten':>6} {'expected':>9} {'measured':>9} {'delta':>6}  verdict")
    for x in rows:
        m = x.get("measured_dbm")
        d = "" if m is None else f"{m - x['coupled_dbm']:+6.1f}"
        v = "" if m is None else ("PASS" if abs(m - x["coupled_dbm"]) <= a.tol else "FAIL")
        print(f"{x['freq_mhz']:9.1f} {x['tx_atten_db']:6.2f} {x['coupled_dbm']:+9.1f} "
              f"{'' if m is None else f'{m:+9.1f}':>9} {d:>6}  {v} {x.get('note', '')}")
    print(f"\nCSV: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
