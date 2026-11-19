#!/usr/bin/env python3
"""Bench tool for the G40 on Payload 2. Every run appends to RUN_LOG.md in DPMG_BENCH_RESULTS
and writes its readings to a CSV, so results do not have to be copied by hand.

Read only:
    payload_bench.py preflight              everything that should be true before the day starts
    payload_bench.py status                 one line, repeat with --watch <seconds>
    payload_bench.py expected               levels expected at the FieldFox for the level check
    payload_bench.py clock [--secs N]       gps_status every 2 s, printing each change, for PAYLOAD-09

Radio (each one prints its plan and stops unless given --go):
    payload_bench.py init --clock internal --rate 30.72e6 [--rx TRx1B] [--rx-off]    init only, nothing keyed
    payload_bench.py key ATTEN SECS [--freq --rate --wave-secs --tag]   one tone, logged, HPA off after
    payload_bench.py rx-ladder --freq F --steps 120,100,80,...          RX only, tone level per attenuator step
    payload_bench.py rx-after-tx [--freq F]                             short key, then RX checks
    payload_bench.py tone-on ATTEN                                      start a tone and leave it on, for PLANNER-05
    payload_bench.py rx-auto --pad 10 --route coupled                   PAYLOAD-10 with a second G40 as the source, payload G40 never keys
    payload_bench.py rx-auto --pad 20 --route j0 --src-atten 40         same with the source on J0 and J0 off the load
    payload_bench.py src-on --pad 10 --route coupled --freq F, src-off  second G40 tone on and left on at F + 1.1 MHz, then off
    payload_bench.py idle                                               stop TX and RX, show the state
    payload_bench.py save                                               copy the front end file and service log here

Limits in every key: TX attenuation 13 dB or more, 30 min at most, stop at 70 C on three good
readings, stop if the HPA drops unless --expect-drop. TX stops on any exit.

--route has no default. coupled: source on the coupled port, J0 on the coupler and load. j0: source on J0, off the load.
DPMG_G40_HOST is the payload G40, DPMG_SRC_HOST the source G40, DPMG_FIELDFOX the analyzer; empty runs with no FieldFox.
DPMG_G40_SSH gives the ssh target for the G40 and DPMG_CMD_PORT the service port.
"""
import argparse
import csv
import json
import os
import signal
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import checkout_common as cc                    # noqa: E402
from dpmg.link import proto                      # noqa: E402
from dpmg.link.proto import MsgType              # noqa: E402
from fsk_loopback_test import Link               # noqa: E402

HOST = os.environ.get("DPMG_G40_HOST", "")
if not HOST:
    sys.exit("Set DPMG_G40_HOST to the payload G40 address first.")
PORT = int(os.environ.get("DPMG_CMD_PORT", "47810"))
FF = os.environ.get("DPMG_FIELDFOX", "") or "none"
OUT = os.environ.get("DPMG_BENCH_RESULTS", ".")
REPO = os.path.dirname(HERE)
SSH = ["ssh", "-o", "ConnectTimeout=4", os.environ.get("DPMG_G40_SSH", f"sidekiq@{HOST}")]
RX_PORT, TX_PORT = "TRx1A", "TRx2B"
MIN_ATTEN, MAX_SECS, STOP_C = 13.0, 1800.0, 70.0
SRC_HOST = os.environ.get("DPMG_SRC_HOST", "")
SRC_PORT = int(os.environ.get("DPMG_SRC_CMD_PORT", "47810"))
# source G40: highest output on file, model coupling, PAYLOAD-10 limit at J0
SRC_MAX_DBM, COUPLED_DB, J0_LIMIT, SRC_MAX_ATTEN = 20.0, 31.0, -40.0, 41.75
ROUTE_LOSS = {"coupled": COUPLED_DB, "j0": 0.0}
LOOK_ON_LOAD = ("Look first: J0 must be on the coupler and load, source only on the coupled port. "
                "This command cannot see the cabling.")
TONE_OFFSET, RX_MIN_ATTEN, RX_STOP_MARGIN = 1.1e6, 30.0, 25.0
# files that have to match between this tree, the radio and the GUI
RADIO_FILES = ["dpmg/radio/dpmg_service.py", "dpmg/radio/sdr_core.py", "dpmg/radio/skiq_bridge.py",
               "dpmg/radio/g40_clock.py", "dpmg/payload/rf_frontend.py"]
GUI_FILES = ["dpmg/mission/mission_runner.py", "dpmg/mission/mission_tab.py", "dpmg/web/link_tab.py",
             "dpmg/payload/rf_frontend.py", "dpmg/radio/g40_clock.py", "dpmg/radio/sdr_core.py"]


def now():
    return time.strftime("%H:%M:%S")


def log(line):
    print(line, flush=True)
    with open(os.path.join(OUT, "RUN_LOG.md"), "a") as fh:
        fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {line}\n")


def cmd(link, name, params=None, timeout=10):
    a = cc.safe_cmd(link, name, params or {}, timeout=timeout)
    return int(a.status), (a.result if isinstance(a.result, dict) else {})


def snap(link):
    _, c = cmd(link, "get_config", timeout=5)
    _, p = cmd(link, "payload_status", timeout=5)
    hm, pa, g, tr = p.get("hpa_monitor") or {}, p.get("pa") or {}, p.get("guard") or {}, p.get("tr") or {}
    wd = p.get("wd") or {}
    return {"clock": now(), "state": c.get("state"), "tx_active": c.get("tx_active"),
            "rate": c.get("sample_rate"), "clock_source": c.get("clock_source"),
            "keyed": pa.get("keyed"), "level": pa.get("level"), "tr_tx": tr.get("tx"),
            "temp_c": hm.get("hpa_temp_c"), "cur_a": hm.get("hpa_current_a"), "quality": hm.get("quality"),
            "rx_atten": g.get("readback_db"), "floor": (g.get("load") or {}).get("min_tx_atten_db"), "refusal": g.get("last_refusal"),
            "trips": wd.get("trips"), "tx_atten": p.get("tx_atten_db"), "drop": (c.get("fe") or {}).get("drop")}


def line(s):
    return (f"state {s['state']} tx {s['tx_active']} keyed {s['keyed']} switch_tx {s['tr_tx']} "
            f"rx_atten {s['rx_atten']} temp {s['temp_c']} cur {s['cur_a']} ({s['quality']})")


class Csv:
    def __init__(self, tag):
        self.path = os.path.join(OUT, f"{tag}_{time.strftime('%H%M%S')}.csv")
        self.fh = open(self.path, "w", newline="")
        self.w = None

    def row(self, d):
        if self.w is None:
            self.w = csv.DictWriter(self.fh, fieldnames=list(d), extrasaction="ignore")
            self.w.writeheader()
        self.w.writerow(d)
        self.fh.flush()


def fieldfox():
    if FF == "none":
        return None
    from fieldfox import FieldFox
    return FieldFox(FF)


def ssh(cmdline, timeout=12):
    try:
        r = subprocess.run(SSH + [cmdline], capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as e:
        return 1, str(e)


def sha(paths, root=REPO):
    out = {}
    for f in paths:
        r = subprocess.run(["sha256sum", os.path.join(root, f)], capture_output=True, text=True)
        out[f] = r.stdout[:12]
    return out


# ------------------------------------------------------------------ read only

def preflight(a):
    res = []

    def chk(ok, what, detail=""):
        tag = {True: "PASS", False: "FAIL", None: "CHECK"}[ok]
        res.append(tag)
        print(f"  {tag:5} {what}{'  ' + str(detail) if detail != '' else ''}", flush=True)
    print(f"preflight {HOST}:{PORT} at {now()}")
    try:
        link = Link(HOST, PORT)
    except Exception as e:
        chk(False, "service answers", e)
        return 1
    chk(True, "service answers", link.serial)
    st, c = cmd(link, "get_config", timeout=5)
    st2, p = cmd(link, "payload_status", timeout=5)
    s = snap(link)
    chk(st == 0 and st2 == 0, "get_config and payload_status")
    chk(s["tx_active"] in (False, None), "no TX running", s["tx_active"])
    chk(s["keyed"] is False, "HPA line not keyed", s["keyed"])
    chk(s["tr_tx"] is False, "switch line on RX", s["tr_tx"])
    g = p.get("guard") or {}
    chk(g.get("profile") == "P2", "guard profile P2", g.get("profile"))
    chk(s["floor"] == 13.0, "TX floor 13 dB", s["floor"])
    chk(isinstance(s["rx_atten"], (int, float)) and s["rx_atten"] >= 120.0, "RX attenuator at 120 dB or more", s["rx_atten"])
    pm = c.get("ports") or {}
    chk(pm.get("TRx2B") == "tx" and pm.get("TRx1A") == "rx", "port map TRx2B TX, TRx1A RX", pm)
    chk(None, "clock source", s["clock_source"])
    chk(None, "radio state (0 closed, 1 ready)", s["state"])
    chk(None, "HPA monitor", (p.get("hpa_monitor") or {}).get("state"))
    if HOST.startswith("127."):
        print("  simulator: radio and GUI file checks skipped")
    else:
        rc, out = ssh("docker exec -w /app_svc dpmg_svc sha256sum " + " ".join(RADIO_FILES))
        have = {ln.split()[1]: ln.split()[0][:12] for ln in out.splitlines() if len(ln.split()) == 2}
        want = sha(RADIO_FILES)
        bad = [f for f in RADIO_FILES if have.get(f) != want[f]]
        chk(rc == 0 and not bad, "radio code matches this tree", bad or "")
        rc, out = ssh("docker inspect -f '{{.State.Status}} restarts={{.RestartCount}} started={{.State.StartedAt}}' dpmg_svc")
        chk(rc == 0 and out.startswith("running"), "dpmg_svc", out[:70])
        rc, out = ssh("/usr/bin/epiq-gps-ref")
        chk(None, "carrier setting epiq-gps-ref", out)
        rc, out = ssh("gpioinfo gpiochip1 | grep PCC.00; docker exec dpmg_svc ls /dev/ttyACM0 /dev/gpiochip1")
        chk(rc == 0, "PCC.00, GPS port and gpiochip1 visible", " ".join(out.split())[:90])
        rc, out = ssh("df -h --output=pcent,avail ~ | tail -1; uptime -p")
        chk(None, "disk and uptime", " ".join(out.split()))
    try:
        r = subprocess.run(["docker", "exec", "dpmg", "sh", "-c", "cd /app && sha256sum " + " ".join(GUI_FILES)],
                           capture_output=True, text=True, timeout=10)
        have = {ln.split()[1]: ln.split()[0][:12] for ln in r.stdout.splitlines() if len(ln.split()) == 2}
        want = sha(GUI_FILES)
        bad = [f for f in GUI_FILES if have.get(f) != want[f]]
        chk(r.returncode == 0 and not bad, "GUI code matches this tree", bad or "")
    except Exception as e:
        chk(False, "GUI container", e)
    if FF != "none":
        try:
            fx = fieldfox()
            chk(True, "FieldFox", f"{fx.idn[:40]} atten {fx.query('POW:ATT?')} ref {fx.query('DISP:WIND:TRAC:Y:RLEV?')}")
            fx.close()
        except Exception as e:
            chk(False, "FieldFox", str(e)[:70])
    print(f"  {res.count('PASS')} pass, {res.count('FAIL')} fail, {res.count('CHECK')} to read")
    log(f"preflight: {res.count('PASS')} pass, {res.count('FAIL')} fail")
    return 1 if "FAIL" in res else 0


def status(a):
    link = Link(HOST, PORT)
    end = time.time() + (a.watch or 0)
    while True:
        print(now(), line(snap(link)), flush=True)
        if time.time() >= end:
            return 0
        time.sleep(1.0)


def expected(a):
    from dpmg.payload import payload_budget as pb
    data = pb.load_profiles(os.path.join(REPO, "payload_profiles.yaml"))
    prof = pb.profile(data, a.profile)
    extra = a.extra_loss
    print(f"profile {a.profile}, model only, cable losses are estimates, {a.backoff:g} dB below full scale, "
          f"extra loss on the coupled port {extra} dB. The P2 model ran 1.3 to 3.5 dB over the FieldFox (RL-027).")
    print(f"{'MHz':>6} {'atten':>6} {'HPA in':>8} {'HPA out':>8} {'W':>7} {'coupled':>8}  region")
    rows = []
    for f in a.freqs:
        for at in a.attens:
            e = pb.coupled_port_expected(data, prof, f, at, backoff_db=a.backoff, extra_loss_db=extra)
            print(f"{f:6.0f} {at:6.1f} {e['hpa_in_dbm']:8.1f} {e['hpa_out_dbm']:8.1f} {e['hpa_out_w']:7.1f} "
                  f"{e['coupled_dbm']:8.1f}  {e.get('region')}")
            rows.append(e)
    with open(os.path.join(OUT, "expected_levels.json"), "w") as fh:
        json.dump(rows, fh, indent=1, default=str)
    return 0


def clock(a):
    link = Link(HOST, PORT)
    out = Csv("clock")
    prev, end = None, time.time() + a.secs
    keys = ("ref_clock", "ext_ref_present", "pps_present", "pps_source", "pps_line", "carrier")
    while time.time() < end:
        st, r = cc.gps_status(link)
        cur = {k: r.get(k) for k in keys}
        out.row(dict(cur, t=now(), status=int(st)))
        if cur != prev:
            log(f"clock {now()}: " + " ".join(f"{k}={cur[k]}" for k in keys))
            prev = cur
        time.sleep(2.0)
    _, c = cmd(link, "get_config", timeout=5)
    print("heartbeat ref_locked:", c.get("ref_locked"), " csv:", out.path)
    return 0


# ------------------------------------------------------------------ radio

def plan_only(a, text):
    print("PLAN:", text)
    if not a.go:
        print("Nothing sent. Add --go to run it.")
        return True
    return False


def do_init(a):
    if plan_only(a, f"init G40 on clock {a.clock}, {a.rate/1e6:g} MS/s, {a.freq/1e6:g} MHz, TX {TX_PORT} at 40 dB, "
                    f"RX {a.rx}{' not started' if a.rx_off else ' started'}. Nothing keyed."):
        return 0
    link = Link(HOST, PORT)
    s = snap(link)
    if s["tx_active"] or s["keyed"]:
        log("init refused here: TX active or line keyed")
        return 2
    p = cc.port_init_params(a.rx, TX_PORT, int(a.rate), int(a.freq), 200, 40.0, clock=a.clock)
    t0 = time.time()
    st, r = cmd(link, "init", p, timeout=150)
    log(f"init clock {a.clock} rate {a.rate/1e6:g} RX {a.rx}: status {st} in {time.time()-t0:.1f} s {r.get('err') or ''}")
    if st != 0:
        return 1
    cmd(link, "set_tx_atten", {"qdb": 160})
    if not a.rx_off:
        log(f"start_rx: {cmd(link, 'start_rx', timeout=30)[0]}")
    print(line(snap(link)))
    return 0


def key(a):
    atten, secs = a.atten, a.secs
    if atten < MIN_ATTEN or secs > MAX_SECS or secs <= 0:
        print(f"refused: attenuation under {MIN_ATTEN} dB or time over {MAX_SECS:.0f} s")
        return 2
    if plan_only(a, f"tone {a.tone_ampl} FS, {a.wave_secs:g} s waveform, TX attenuation {atten:g} dB for {secs:g} s. "
                    f"Radio must already be initialized. Stops at {STOP_C:g} C. {LOOK_ON_LOAD}"):
        return 0
    link = Link(HOST, PORT)
    s = snap(link)
    if s["state"] not in (1, 2) or s["tx_active"] or s["keyed"]:
        log(f"key {a.tag}: not keying, radio not ready and idle: {line(s)}")
        return 2
    fx = None
    try:
        fx = fieldfox()
    except Exception as e:
        print("FieldFox not used:", e)
    out = Csv(a.tag)
    rc, stop_why, rows, stopped = 1, "time up", [], {"v": False}

    def stop(*_):
        if not stopped["v"]:
            stopped["v"] = True
            log(f"stop_tx: {cmd(link, 'stop_tx', timeout=30)[0]}")

    def on_sig(*_):
        stop()
        raise SystemExit(130)
    signal.signal(signal.SIGINT, on_sig)
    signal.signal(signal.SIGTERM, on_sig)
    try:
        st, r = cmd(link, "set_tx_atten", {"qdb": int(round(atten * 4))})
        if st != 0:
            log(f"key {a.tag}: set_tx_atten refused: {r.get('err')}")
            return 1
        t_b = time.time()
        st, r = cmd(link, "gen_test_waveform", {"kind": "tone", "freq": 100e3, "seconds": a.wave_secs,
                                                "ampl": a.tone_ampl, "name": "bd_tone", "load_tx": True}, timeout=90)
        if st != 0:
            log(f"key {a.tag}: tone refused: {r.get('err')}")
            return 1
        print(f"waveform built in {time.time()-t_b:.1f} s")
        st, r = cmd(link, "start_tx", {"mode": "CONTINUOUS"}, timeout=20)
        if st != 0:
            log(f"key {a.tag}: start_tx refused: {r.get('err')}")
            return 1
        t_on = time.time()
        log(f"key {a.tag}: KEYED {now()} atten {atten:g} dB, {s['rate']} S/s")
        was_keyed, ff_done, hot = False, False, 0
        while time.time() - t_on < secs:
            d = snap(link)
            d["t_s"] = round(time.time() - t_on, 1)
            out.row(d)
            rows.append(d)
            if d["keyed"]:
                was_keyed = True
            elif (was_keyed or d["t_s"] > 3) and not a.expect_drop and d["tx_active"]:
                stop_why = f"HPA dropped at {d['t_s']} s, drop={d['drop']}, trips={d['trips']}"
                break
            hot = hot + 1 if d["quality"] == "ok" and isinstance(d["temp_c"], (int, float)) and d["temp_c"] >= STOP_C else 0
            if hot >= 3:
                stop_why = f"{STOP_C:g} C reached at {d['t_s']} s"
                break
            if fx and not ff_done and d["t_s"] >= min(secs / 2, 10):
                try:
                    lvl, hz = fx.peak()
                    log(f"key {a.tag}: FieldFox {lvl:.1f} dBm at {hz/1e6:.4f} MHz")
                    fx.screenshot(os.path.join(OUT, f"{a.tag}_{time.strftime('%H%M%S')}.png"))
                    fx.resume()
                except Exception as e:
                    print("FieldFox reading failed:", e)
                ff_done = True
            print(now(), f"t={d['t_s']}", line(d), flush=True)
            time.sleep(1.0)
        rc = 0 if stop_why == "time up" else 3
    finally:
        stop()
        time.sleep(1.5)
        e = snap(link)
        good = [r for r in rows if r["quality"] == "ok" and isinstance(r.get("temp_c"), (int, float))]
        peak = max((r["temp_c"] for r in good), default=None)
        amps = [r["cur_a"] for r in good if isinstance(r.get("cur_a"), (int, float))]
        log(f"key {a.tag}: ended, {stop_why}. Peak {peak} C, current "
            f"{min(amps) if amps else None} to {max(amps) if amps else None} A, keyed samples "
            f"{sum(1 for r in rows if r['keyed'])}/{len(rows)}, trips {e['trips']}, refusals {e['refusal']}")
        log(f"key {a.tag}: LEFT {line(e)}. csv {os.path.basename(out.path)}")
        if fx:
            try:
                fx.resume()
                fx.close()
            except Exception:
                pass
    return rc


def psd_peak(link, freq_hz, seconds=2.0, span_hz=40e3):
    """Highest bin near freq_hz and the median floor, from the PSD frames the service sends."""
    import statistics
    end = time.time() + seconds
    pk, floors, n = None, [], 0
    link.s.settimeout(0.5)
    while time.time() < end:
        try:
            data, _ = link.s.recvfrom(65535)
            mt, _seq, m = proto.decode(data)
        except (socket.timeout, proto.ProtoError):
            continue
        if mt != MsgType.PSD_FRAME:
            continue
        n += 1
        bins = [(b - 256 if b > 127 else b) for b in m.bins]
        sc = m.scale_db_x100 / 100.0
        db = [m.ref_db + b * sc for b in bins]
        floors.append(statistics.median(db))
        nb = len(db)
        i = int(round((freq_hz - (m.center_hz - m.span_hz / 2)) / m.span_hz * nb))
        w = max(1, int(span_hz / m.span_hz * nb))
        seg = db[max(0, i - w):min(nb, i + w + 1)]
        if seg:
            v = max(seg)
            pk = v if pk is None or v > pk else pk
    link.s.settimeout(1.0)
    return pk, (statistics.median(floors) if floors else None), n


def rx_ladder(a):
    steps = [float(x) for x in a.steps.split(",")]
    if plan_only(a, f"RX only on the current init, no TX. Attenuator {steps} dB, tone looked for near {a.freq/1e6:g} MHz. "
                    f"Back to 120 dB after. Source must be on and under -40 dBm at J0."):
        return 0
    link = Link(HOST, PORT)
    s = snap(link)
    if s["tx_active"] or s["keyed"] or s["state"] != 2:
        log(f"rx-ladder: not running: {line(s)}")
        return 2
    out = Csv(a.tag)
    try:
        for db in steps:
            st, r = cmd(link, "atten_set", {"db": db})
            if st != 0:
                log(f"rx-ladder: {db} dB refused: {r.get('err')}")
                continue
            time.sleep(0.5)
            cc.drain(link)
            pk, fl, n = psd_peak(link, a.freq, a.dwell)
            rb = snap(link)["rx_atten"]
            out.row({"atten_db": db, "readback_db": rb, "peak_dbfs": pk, "floor_dbfs": fl, "frames": n})
            log(f"rx-ladder {a.tag}: atten {db:g} dB (reads {rb}), peak {pk}, floor {fl}, {n} PSD frames")
    finally:
        st, _ = cmd(link, "atten_set", {"db": 120.0})
        log(f"rx-ladder {a.tag}: attenuator back to 120 dB, status {st}, csv {os.path.basename(out.path)}")
    return 0


def rx_after_tx(a):
    if plan_only(a, f"RX running on the current init, attenuator {a.rx_atten:g} dB. Key 5 s at {a.atten:g} dB TX "
                    f"attenuation, then check RX frames still advance, attenuator returns, switch on RX. {LOOK_ON_LOAD}"):
        return 0
    link = Link(HOST, PORT)
    s = snap(link)
    if s["tx_active"] or s["keyed"] or s["state"] != 2:
        log(f"rx-after-tx: not running: {line(s)}")
        return 2
    try:
        cmd(link, "atten_set", {"db": a.rx_atten})
        time.sleep(0.5)
        cc.drain(link)
        pk0, fl0, n0 = psd_peak(link, a.freq, 2.0)
        k = argparse.Namespace(atten=a.atten, secs=5.0, tone_ampl=0.5, wave_secs=0.5, tag="rx_after_tx", go=True,
                               expect_drop=False)
        rc = key(k)
        time.sleep(1.0)
        e = snap(link)
        cc.drain(link)
        pk1, fl1, n1 = psd_peak(link, a.freq, 2.0)
        ok = rc == 0 and e["rx_atten"] == a.rx_atten and e["tr_tx"] is False and n1 > 0
        log(f"rx-after-tx: {'PASS' if ok else 'CHECK'}. Attenuator {e['rx_atten']} (set {a.rx_atten:g}), switch_tx "
            f"{e['tr_tx']}, PSD frames {n0} before and {n1} after, floor {fl0} to {fl1}, peak {pk0} to {pk1}")
    finally:
        st, _ = cmd(link, "atten_set", {"db": 120.0})
        log(f"rx-after-tx: attenuator back to 120 dB, status {st}")
    return 0 if ok else 1


def src_level(atten, pad, route):
    """Calculated worst case at J0 on this route, with this G40 at the highest output on file and a 0.5 tone."""
    return SRC_MAX_DBM - 6.0 - atten - pad - ROUTE_LOSS[route]


def route_plan(route):
    if route == "j0":
        return ("Route j0: source on J0 through the pad. Look first: nothing on J0 except the source and pad. The HPA must "
                "not have a path to J0: switch out of the HPA path, or nothing keys (no mission running, any plan or "
                "location trigger checked and off, no bd key, tone-on or rx-after-tx).")
    return "Route coupled: J0 stays on the coupler and load. Look first: J0 on the coupler and load, source only on the coupled port."


def src_ok(a):
    lvl = src_level(a.src_atten, a.pad, a.route)
    if a.src_atten > SRC_MAX_ATTEN or not lvl <= J0_LIMIT:
        print(f"refused: route {a.route}, {a.src_atten:g} dB plus a {a.pad:g} dB pad is {lvl:.1f} dBm at J0 worst case, "
              f"limit {J0_LIMIT:g} dBm, or attenuation is over {SRC_MAX_ATTEN:g} dB")
        return False
    return True


def src_start(src, freq, a):
    """Tone at freq + TONE_OFFSET: source LO 1 MHz up, so the tone is clear of DC on both radios."""
    st, r = cmd(src, "stop_tx", timeout=30)
    p = cc.port_init_params(RX_PORT if a.src_tx != RX_PORT else "TRx1B", a.src_tx, int(a.rate),
                            int(freq + TONE_OFFSET - 100e3), 200, a.src_atten, clock="internal")
    st, r = cmd(src, "init", p, timeout=150)
    if st == 0:
        st, r = cmd(src, "set_tx_atten", {"qdb": int(round(a.src_atten * 4))})
    if st == 0:
        st, r = cmd(src, "gen_test_waveform", {"kind": "tone", "freq": 100e3, "seconds": 0.5, "ampl": 0.5,
                                               "name": "bd_src", "load_tx": True}, timeout=90)
    if st == 0:
        st, r = cmd(src, "start_tx", {"mode": "CONTINUOUS"}, timeout=20)
    if st != 0:
        raise RuntimeError(f"source: {r.get('err')}")


def ladder_eval(rows, off_peak=None):
    """Linear where the tone is 15 dB or more over the floor: dB for dB within 1 dB, off drops it 10 dB."""
    up = [r for r in rows if r["peak"] is not None and r["floor"] is not None and r["peak"] >= r["floor"] + 15]
    errs = [(b["peak"] - a["peak"]) - (a["atten"] - b["atten"]) for a, b in zip(up, up[1:])]
    worst = max((abs(e) for e in errs), default=None)
    drop = up[-1]["peak"] - off_peak if up and off_peak is not None else None
    ok = len(up) >= 2 and worst <= 1.0 and (drop is None or drop >= 10.0)
    return {"ok": ok, "points": len(up), "worst_db": worst, "off_drop_db": drop}


def level_at(rows, attens):
    """Mean of peak + attenuator over the given settings: one level for a jack and frequency."""
    v = [r["peak"] + r["atten"] for r in rows if r["atten"] in attens and r["peak"] is not None]
    return sum(v) / len(v) if v else None


def model_gain(freq_hz):
    from dpmg.payload import payload_budget as pb
    data = pb.load_profiles(os.path.join(REPO, "payload_profiles.yaml"))
    return sum(s["gain_db"] for s in pb.rx_cascade(pb.profile(data, "P2"), freq_hz / 1e6, 0))


def rx_auto(a):
    """PAYLOAD-10 with a second G40 as the source. The payload G40 never keys in this command."""
    cases = [(j, 915e6) for j in a.jacks.split(",")] + [(a.jacks.split(",")[0], f) for f in a.edges]
    steps = [float(x) for x in a.steps.split(",")]
    if not src_ok(a):
        return 2
    if min(steps) < RX_MIN_ATTEN:
        print(f"refused: RX attenuator under {RX_MIN_ATTEN:g} dB")
        return 2
    if plan_only(a, f"Payload G40 {HOST}: RX only, never keyed. Source G40 {SRC_HOST}: tone on {a.src_tx} at "
                    f"{a.src_atten:g} dB, pad {a.pad:g} dB, at most {src_level(a.src_atten, a.pad, a.route):.1f} dBm at J0, calculated. "
                    f"{len(cases)} ladders: {[(j, f/1e6) for j, f in cases]} MHz. Switch moved to TX once with the PA off. "
                    f"{route_plan(a.route)} Both radios stopped and the attenuator at 120 dB after."):
        return 0
    link, src = Link(HOST, PORT), Link(SRC_HOST, SRC_PORT)
    for name, l in (("payload", link), ("source", src)):
        s = snap(l)
        if s["tx_active"] or s["keyed"]:
            log(f"rx-auto: not running, {name} is not idle: {line(s)}")
            return 2

    def on_sig(*_):
        raise SystemExit(130)
    signal.signal(signal.SIGINT, on_sig)
    signal.signal(signal.SIGTERM, on_sig)
    out, results, rc = Csv(a.tag), {}, 1
    log(f"rx-auto: route {a.route}, source {a.src_atten:g} dB, pad {a.pad:g} dB, at most "
        f"{src_level(a.src_atten, a.pad, a.route):.1f} dBm at J0, calculated")
    try:
        for n, (jack, freq) in enumerate(cases):
            cmd(link, "stop_rx", timeout=30)
            st, r = cmd(link, "init", cc.port_init_params(jack, TX_PORT, int(a.rate), int(freq), 200, 40.0,
                                                          clock="internal"), timeout=150)
            if st != 0:
                raise RuntimeError(f"init {jack}: {r.get('err')}")
            cmd(link, "set_tx_atten", {"qdb": 160})
            st, r = cmd(link, "atten_set", {"db": 120.0})
            if st != 0:
                raise RuntimeError(f"attenuator: {r.get('err')}")
            st, r = cmd(link, "start_rx", timeout=30)
            if st != 0:
                raise RuntimeError(f"start_rx: {r.get('err')}")
            src_start(src, freq, a)
            tone = freq + TONE_OFFSET
            rows = []
            for db in steps:
                st, r = cmd(link, "atten_set", {"db": db})
                if st != 0:
                    raise RuntimeError(f"attenuator {db:g} dB: {r.get('err')}")
                time.sleep(0.5)
                cc.drain(link)
                pk, fl, frames = psd_peak(link, tone, a.dwell)
                s = snap(link)
                if s["keyed"] or s["tx_active"]:
                    raise RuntimeError(f"payload G40 keyed during RX: {line(s)}")
                row = {"jack": jack, "freq_mhz": freq / 1e6, "atten": db, "readback": s["rx_atten"], "peak": pk,
                       "floor": fl, "frames": frames, "point": "tone"}
                rows.append(row)
                out.row(row)
                log(f"rx-auto {jack} {freq/1e6:g} MHz: atten {db:g}, peak {pk}, floor {fl}, {frames} frames")
                if pk is not None and fl is not None and pk >= fl + a.stop_margin:
                    break
            iso = None
            if n == 0:
                pk0 = rows[-1]["peak"]
                st, r = cmd(link, "frontend_set", {"tr": "tx"})
                if st == 0:
                    time.sleep(0.5)
                    cc.drain(link)
                    pk1, fl1, _ = psd_peak(link, tone, a.dwell)
                    cmd(link, "frontend_set", {"tr": "rx"})
                    time.sleep(0.5)
                    cc.drain(link)
                    pk2, _, _ = psd_peak(link, tone, a.dwell)
                    iso = (pk0 - pk1) if pk0 is not None and pk1 is not None else None
                    log(f"rx-auto switch: RX {pk0}, switch on TX with the PA off {pk1} (floor {fl1}), back on RX {pk2}, "
                        f"tone fell {iso} dB")
                    out.row({"jack": jack, "freq_mhz": freq / 1e6, "atten": rows[-1]["atten"], "peak": pk1, "floor": fl1,
                             "point": "switch on TX"})
                else:
                    log(f"rx-auto switch: not run, {r.get('err')}")
            cmd(src, "stop_tx", timeout=30)
            time.sleep(1.0)
            cc.drain(link)
            off, fl, _ = psd_peak(link, tone, a.dwell)
            out.row({"jack": jack, "freq_mhz": freq / 1e6, "atten": rows[-1]["atten"], "peak": off, "floor": fl,
                     "point": "source off"})
            cmd(link, "atten_set", {"db": 120.0})
            e = ladder_eval(rows, off)
            results[(jack, freq)] = dict(e, rows=rows, iso=iso)
            log(f"rx-auto {jack} {freq/1e6:g} MHz: {'PASS' if e['ok'] else 'FAIL'}, {e['points']} points over the floor, "
                f"worst {e['worst_db']} dB from dB for dB, source off drops it {e['off_drop_db']} dB")
        rc = 0 if all(v["ok"] for v in results.values()) else 1
        ref_key = cases[0]
        ref = results[ref_key]["rows"]
        for key_, v in results.items():
            common = {r["atten"] for r in ref if r["peak"] is not None and r["peak"] >= r["floor"] + 6} & \
                     {r["atten"] for r in v["rows"] if r["peak"] is not None and r["peak"] >= r["floor"] + 6}
            if key_ == ref_key or not common:
                continue
            d = level_at(v["rows"], common) - level_at(ref, common)
            if key_[1] == ref_key[1]:
                want = -3.3 if key_[0].startswith("TRx2") else 0.0
                ok = abs(d - want) <= 2.0
                log(f"rx-auto jack {key_[0]} against {ref_key[0]}: {d:+.1f} dB, expected {want:+.1f} within 2, {'PASS' if ok else 'FAIL'}")
            else:
                want = model_gain(key_[1]) - model_gain(ref_key[1])
                ok = abs(d - want) <= 4.0
                log(f"rx-auto {key_[1]/1e6:g} MHz against {ref_key[1]/1e6:g}: {d:+.1f} dB, model gain {want:+.1f} within 4, "
                    f"source output change not removed, {'PASS' if ok else 'CHECK'}")
            rc = rc if ok or key_[1] != ref_key[1] else 1
        log(f"rx-auto: {'PASS' if rc == 0 else 'FAIL'} on route {a.route}. Absolute level against the Payload tab is still by hand. csv {os.path.basename(out.path)}")
    except Exception as e:
        log(f"rx-auto: stopped, {e}")
        rc = 1
    finally:
        log(f"rx-auto: payload stop_tx {cmd(link, 'stop_tx', timeout=30)[0]}")
        log(f"rx-auto: source stop_tx {cmd(src, 'stop_tx', timeout=30)[0]}, stop_rx {cmd(src, 'stop_rx', timeout=30)[0]}")
        log(f"rx-auto: attenuator 120 dB, status {cmd(link, 'atten_set', {'db': 120.0})[0]}, stop_rx {cmd(link, 'stop_rx', timeout=30)[0]}")
        cmd(link, "frontend_set", {"tr": "rx"})
        log("rx-auto: payload " + line(snap(link)))
        log("rx-auto: source " + line(snap(src)))
    return rc


def src_on(a):
    if not src_ok(a):
        return 2
    read = "bd rx-ladder" if a.route == "j0" else "bd rx-ladder or rx-after-tx"
    if plan_only(a, f"Source G40 {SRC_HOST}: tone at {(a.freq + TONE_OFFSET)/1e6:g} MHz on {a.src_tx}, {a.src_atten:g} dB, "
                    f"pad {a.pad:g} dB, at most {src_level(a.src_atten, a.pad, a.route):.1f} dBm at J0, calculated, and LEFT ON "
                    f"until src-off. {route_plan(a.route)} Read the tone with {read} --freq {(a.freq + TONE_OFFSET)/1e6:g}e6."):
        return 0
    src = Link(SRC_HOST, SRC_PORT)
    src_start(src, a.freq, a)
    log(f"src-on: TONE LEFT ON at {(a.freq + TONE_OFFSET)/1e6:g} MHz, {a.src_atten:g} dB, pad {a.pad:g} dB, route {a.route}, {now()}")
    return 0


def rx_look(a):
    """Whole band look at the payload G40: strongest PSD bins with the source on and off. The payload G40 never keys."""
    if a.source and not src_ok(a):
        return 2
    if plan_only(a, f"Payload G40 {HOST}: RX only on {a.rx}, {a.freq/1e6:g} MHz, attenuator {a.atten:g} dB, never keyed. "
                    + (f"Source G40 {SRC_HOST}: tone at {(a.freq + TONE_OFFSET)/1e6:g} MHz on {a.src_tx}, {a.src_atten:g} dB, "
                       f"pad {a.pad:g} dB, route {a.route}. {route_plan(a.route)} " if a.source else "No source. ")
                    + "Prints the strongest bins, then stops both radios."):
        return 0
    import statistics
    link = Link(HOST, PORT)
    src = Link(SRC_HOST, SRC_PORT) if a.source else None

    def look(tag):
        time.sleep(1.0)
        cc.drain(link)
        link.s.settimeout(0.5)
        end, frames, last = time.time() + a.dwell, 0, None
        while time.time() < end:
            try:
                data, _ = link.s.recvfrom(65535)
                mt, _seq, m = proto.decode(data)
            except (socket.timeout, proto.ProtoError):
                continue
            if mt == MsgType.PSD_FRAME:
                frames, last = frames + 1, m
        link.s.settimeout(1.0)
        if last is None:
            log(f"rx-look {tag}: no PSD frames")
            return
        bins = [(b - 256 if b > 127 else b) for b in last.bins]
        db = [last.ref_db + b * last.scale_db_x100 / 100.0 for b in bins]
        nb, lo = len(db), last.center_hz - last.span_hz / 2
        top = sorted(range(nb), key=lambda i: db[i], reverse=True)[:6]
        log(f"rx-look {tag}: {frames} frames, centre {last.center_hz/1e6:g} MHz, span {last.span_hz/1e6:g} MHz, {nb} bins, "
            f"floor {statistics.median(db):.1f}. Top: " + ", ".join(f"{lo + (i + 0.5) * last.span_hz / nb:.0f} Hz {db[i]:.1f}" for i in top))

    def on_sig(*_):
        raise SystemExit(130)
    signal.signal(signal.SIGTERM, on_sig)
    rc = 1
    try:
        s = snap(link)
        if s["tx_active"] or s["keyed"]:
            raise RuntimeError(f"payload not idle: {line(s)}")
        st, r = cmd(link, "init", cc.port_init_params(a.rx, TX_PORT, int(a.rate), int(a.freq), a.rx_gain, 40.0, clock="internal"), timeout=150)
        if st != 0:
            raise RuntimeError(f"init {r.get('err')}")
        cmd(link, "set_tx_atten", {"qdb": 160})
        for name, p in (("atten_set", {"db": a.atten}), ("start_rx", {})):
            st, r = cmd(link, name, p, timeout=30)
            if st != 0:
                raise RuntimeError(f"{name}: {r.get('err')}")
        look("source off")
        if src:
            src_start(src, a.freq, a)
            look("source on")
            cmd(src, "stop_tx", timeout=30)
            look("source off again")
        rc = 0
    except Exception as e:
        log(f"rx-look: stopped, {e}")
    finally:
        if src:
            log(f"rx-look: source stop_tx {cmd(src, 'stop_tx', timeout=30)[0]}, close {cmd(src, 'close', timeout=60)[0]}")
        log(f"rx-look: attenuator 120 dB {cmd(link, 'atten_set', {'db': 120.0})[0]}, stop_rx {cmd(link, 'stop_rx', timeout=30)[0]}, "
            f"close {cmd(link, 'close', timeout=60)[0]}")
        log("rx-look: payload " + line(snap(link)))
    return rc


def src_read(a):
    """Source G40 through its pad straight into the FieldFox, not J0. Tone on, read at each attenuation, tone off."""
    attens = [float(x) for x in a.attens.split(",")]
    if min(attens) < MIN_ATTEN or max(attens) > SRC_MAX_ATTEN or a.pad < 20:
        print(f"refused: attenuation {MIN_ATTEN:g} to {SRC_MAX_ATTEN:g} dB and a pad of 20 dB or more")
        return 2
    if plan_only(a, f"Source G40 {SRC_HOST} only: tone at {(a.freq + TONE_OFFSET)/1e6:g} MHz on {a.src_tx}, pad {a.pad:g} dB, read on the "
                    f"FieldFox at {attens} dB, then TX off and radio closed. Look first: the pad output goes to the FieldFox, not J0."):
        return 0
    src = Link(SRC_HOST, SRC_PORT)
    s = snap(src)
    if s["tx_active"]:
        log(f"src-read: not running, source not idle: {line(s)}")
        return 2
    fx, res = fieldfox(), []

    def on_sig(*_):
        raise SystemExit(130)
    signal.signal(signal.SIGTERM, on_sig)
    rc = 1
    try:
        a.src_atten = attens[0]
        src_start(src, a.freq, a)
        for db in attens:
            st, r = cmd(src, "set_tx_atten", {"qdb": int(round(db * 4))})
            if st != 0:
                raise RuntimeError(f"set_tx_atten {db:g}: {r.get('err')}")
            time.sleep(2.0)
            lv = [fx.peak() for _ in range(3)]
            res.append((db, lv[-1][0], lv[-1][1]))
            log(f"src-read: {db:g} dB, pad {a.pad:g}: " + ", ".join(f"{p:.1f} dBm at {h/1e6:.4f} MHz" for p, h in lv))
        fx.screenshot(os.path.join(OUT, f"src_read_{time.strftime('%H%M%S')}.png"))
        rc = 0
    except Exception as e:
        log(f"src-read: stopped, {e}")
    finally:
        log(f"src-read: stop_tx {cmd(src, 'stop_tx', timeout=30)[0]}, stop_rx {cmd(src, 'stop_rx', timeout=30)[0]}, "
            f"close {cmd(src, 'close', timeout=60)[0]}")
        log("src-read: source " + line(snap(src)))
        try:
            fx.resume()
            fx.close()
        except Exception:
            pass
    if len(res) > 1:
        log(f"src-read: {res[0][0]:g} to {res[-1][0]:g} dB moved the reading {res[0][1]-res[-1][1]:.1f} dB")
    return rc


def src_off(a):
    src = Link(SRC_HOST, SRC_PORT)
    log(f"src-off: stop_tx {cmd(src, 'stop_tx', timeout=30)[0]}, stop_rx {cmd(src, 'stop_rx', timeout=30)[0]}")
    log("src-off: " + line(snap(src)))
    return 0


def tone_on(a):
    if a.atten < MIN_ATTEN:
        print(f"refused: attenuation under {MIN_ATTEN} dB")
        return 2
    if plan_only(a, f"start a 0.5 FS tone at TX attenuation {a.atten:g} dB and LEAVE IT ON until Run mission, "
                    f"STOP ALL TX or payload_bench.py idle stops it. Radio must already be initialized. {LOOK_ON_LOAD}"):
        return 0
    link = Link(HOST, PORT)
    s = snap(link)
    if s["state"] not in (1, 2) or s["tx_active"] or s["keyed"]:
        log(f"tone-on: not keying: {line(s)}")
        return 2
    st, r = cmd(link, "set_tx_atten", {"qdb": int(round(a.atten * 4))})
    if st == 0:
        st, r = cmd(link, "gen_test_waveform", {"kind": "tone", "freq": 100e3, "seconds": 0.5, "ampl": 0.5,
                                                "name": "bd_tone", "load_tx": True}, timeout=60)
    if st == 0:
        st, r = cmd(link, "start_tx", {"mode": "CONTINUOUS"}, timeout=20)
    log(f"tone-on: status {st} {r.get('err') or ''}, {a.atten:g} dB, TX LEFT ON at {now()}" if st == 0
        else f"tone-on: refused: {r.get('err')}")
    return 0 if st == 0 else 1


def save(a):
    out = os.path.join(OUT, "saved_" + time.strftime("%H%M%S"))
    os.makedirs(out, exist_ok=True)
    for name, c in (("rf_frontend.yaml", "cat ~/work/NiceGui_test/rf_frontend.yaml"),
                    ("dpmg_svc_log.txt", "docker logs --since 24h dpmg_svc 2>&1 | tail -20000"),
                    ("gps_ref.txt", "/usr/bin/epiq-gps-ref"),
                    ("gpio.txt", "gpioinfo gpiochip0 | grep -E 'PG.06|PN.01'; gpioinfo gpiochip1 | grep PCC.00")):
        rc, txt = ssh(c, timeout=40)
        with open(os.path.join(out, name), "w") as fh:
            fh.write(txt + "\n")
        print(name, "rc", rc, len(txt), "bytes")
    log(f"save: {os.path.basename(out)}")
    return 0


def idle(a):
    link = Link(HOST, PORT)
    log(f"idle: stop_tx {cmd(link, 'stop_tx', timeout=30)[0]}, stop_rx {cmd(link, 'stop_rx', timeout=30)[0]}")
    log("idle: " + line(snap(link)))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="c", required=True)
    sp.add_parser("preflight")
    x = sp.add_parser("status")
    x.add_argument("--watch", type=float)
    x = sp.add_parser("expected")
    x.add_argument("--profile", default="P2")
    x.add_argument("--freqs", type=lambda s: [float(v) for v in s.split(",")], default=[700.0, 915.0, 1500.0, 2200.0, 2500.0])
    x.add_argument("--attens", type=lambda s: [float(v) for v in s.split(",")], default=[40.0, 30.0])
    x.add_argument("--extra-loss", type=float, default=0.0)
    x.add_argument("--backoff", type=float, default=6.0, help="dB below full scale, 6 for a 0.5 tone")
    x = sp.add_parser("clock")
    x.add_argument("--secs", type=float, default=60.0)
    x = sp.add_parser("init")
    x.add_argument("--clock", default="internal", choices=("internal", "external"))
    x.add_argument("--rate", type=float, default=30.72e6)
    x.add_argument("--freq", type=float, default=915e6)
    x.add_argument("--rx", default=RX_PORT, choices=("TRx1A", "TRx1B", "TRx2A"))
    x.add_argument("--rx-off", action="store_true")
    x.add_argument("--go", action="store_true")
    x = sp.add_parser("key")
    x.add_argument("atten", type=float)
    x.add_argument("secs", type=float)
    x.add_argument("--tone-ampl", type=float, default=0.5)
    x.add_argument("--wave-secs", type=float, default=0.5)
    x.add_argument("--tag", default="key")
    x.add_argument("--expect-drop", action="store_true")
    x.add_argument("--go", action="store_true")
    x = sp.add_parser("rx-ladder")
    x.add_argument("--freq", type=float, default=915e6)
    x.add_argument("--steps", default="120,100,90,80,70,60,50,40")
    x.add_argument("--dwell", type=float, default=2.0)
    x.add_argument("--tag", default="rx_ladder")
    x.add_argument("--go", action="store_true")
    x = sp.add_parser("rx-after-tx")
    x.add_argument("--freq", type=float, default=915e6)
    x.add_argument("--atten", type=float, default=40.0)
    x.add_argument("--rx-atten", type=float, default=60.0)
    x.add_argument("--go", action="store_true")
    x = sp.add_parser("tone-on")
    x.add_argument("atten", type=float)
    x.add_argument("--go", action="store_true")
    x = sp.add_parser("rx-auto")
    x.add_argument("--pad", type=float, required=True, help="pad on the source G40, dB")
    x.add_argument("--route", required=True, choices=sorted(ROUTE_LOSS), help="where the source goes in, no default")
    x.add_argument("--src-atten", type=float, default=20.0)
    x.add_argument("--src-tx", default="TRx1B")
    x.add_argument("--rate", type=float, default=30.72e6)
    x.add_argument("--jacks", default="TRx1A,TRx1B,TRx2A")
    x.add_argument("--edges", type=lambda s: [float(v) for v in s.split(",") if v], default=[700e6, 2500e6])
    x.add_argument("--steps", default="120,110,100,90,80,70,60,50,40,30")
    x.add_argument("--stop-margin", type=float, default=RX_STOP_MARGIN, help="stop the ladder this many dB over the floor")
    x.add_argument("--dwell", type=float, default=1.5)
    x.add_argument("--tag", default="rx_auto")
    x.add_argument("--go", action="store_true")
    x = sp.add_parser("src-on")
    x.add_argument("--pad", type=float, required=True, help="pad on the source G40, dB")
    x.add_argument("--route", required=True, choices=sorted(ROUTE_LOSS), help="where the source goes in, no default")
    x.add_argument("--freq", type=float, default=915e6)
    x.add_argument("--src-atten", type=float, default=20.0)
    x.add_argument("--src-tx", default="TRx1B")
    x.add_argument("--rate", type=float, default=30.72e6)
    x.add_argument("--go", action="store_true")
    x = sp.add_parser("rx-look")
    x.add_argument("--rx", default=RX_PORT, choices=("TRx1A", "TRx1B", "TRx2A"))
    x.add_argument("--freq", type=float, default=915e6)
    x.add_argument("--rate", type=float, default=30.72e6)
    x.add_argument("--atten", type=float, default=30.0)
    x.add_argument("--rx-gain", type=int, default=200)
    x.add_argument("--dwell", type=float, default=2.0)
    x.add_argument("--source", action="store_true")
    x.add_argument("--pad", type=float, default=20.0)
    x.add_argument("--route", default="j0", choices=sorted(ROUTE_LOSS))
    x.add_argument("--src-atten", type=float, default=40.0)
    x.add_argument("--src-tx", default="TRx1B")
    x.add_argument("--go", action="store_true")
    x = sp.add_parser("src-read")
    x.add_argument("--pad", type=float, required=True, help="pad on the source G40, dB")
    x.add_argument("--attens", default="20,30")
    x.add_argument("--freq", type=float, default=915e6)
    x.add_argument("--src-tx", default="TRx1B")
    x.add_argument("--rate", type=float, default=30.72e6)
    x.add_argument("--go", action="store_true")
    sp.add_parser("src-off")
    sp.add_parser("idle")
    sp.add_parser("save")
    a = ap.parse_args()
    return {"preflight": preflight, "status": status, "expected": expected, "clock": clock, "init": do_init, "key": key,
            "rx-ladder": rx_ladder, "rx-after-tx": rx_after_tx, "tone-on": tone_on, "idle": idle,
            "rx-auto": rx_auto, "src-on": src_on, "src-off": src_off, "src-read": src_read, "rx-look": rx_look,
            "save": save}[a.c](a)


if __name__ == "__main__":
    sys.exit(main())
