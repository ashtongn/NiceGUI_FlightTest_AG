#!/usr/bin/env python3
"""PAYLOAD-01: one tone at one TX attenuation, 30 s, temperature and current logged about
once a second, FieldFox peak and screenshot at the middle. Stops TX on any exit, and early at 70 C
or if the HPA line drops. Results per level in tone_<atten>dB.json and .csv. Set DPMG_FIELDFOX to
the analyzer address, or leave it empty to run with no analyzer.

    payload_tone_levels.py setup          init on internal, TX TRx2B, RX off, FieldFox to 30 dB attenuation; no key
    payload_tone_levels.py key <atten>    set the attenuation, load the tone, key for 30 s
"""
import csv
import json
import os
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import checkout_common as cc                    # noqa: E402
from fsk_loopback_test import Link              # noqa: E402

HOST, PORT = os.environ.get("DPMG_G40_HOST", ""), int(os.environ.get("DPMG_CMD_PORT", "47810"))
if not HOST:
    sys.exit("Set DPMG_G40_HOST to the payload G40 address first.")
OUT = os.environ.get("DPMG_BENCH_RESULTS", ".")
FF = os.environ.get("DPMG_FIELDFOX", "")
RX_PORT, TX_PORT = "TRx1A", "TRx2B"
RATE, CENTER, AMPL, TONE_HZ, SECONDS = 30_720_000, 915_000_000, 0.5, 100e3, 30.0
STOP_C = 70.0


def cmd(link, name, params=None, timeout=10):
    a = cc.safe_cmd(link, name, params or {}, timeout=timeout)
    return int(a.status), (a.result if isinstance(a.result, dict) else {})


def sample(link, t0, phase):
    _, p = cmd(link, "payload_status")
    _, c = cmd(link, "get_config")
    hm, pa, g = p.get("hpa_monitor") or {}, p.get("pa") or {}, p.get("guard") or {}
    return {"t_s": round(time.time() - t0, 1), "phase": phase, "tx_active": c.get("tx_active"), "keyed": pa.get("keyed"),
            "quality": hm.get("quality"), "temp_v": hm.get("hpa_temp_v"), "temp_c": hm.get("hpa_temp_c"),
            "cur_v": hm.get("hpa_current_v"), "cur_a": hm.get("hpa_current_a"), "rx_atten_db": g.get("readback_db"),
            "refusal": g.get("last_refusal")}


def med(rows, key):
    v = [r[key] for r in rows if isinstance(r.get(key), (int, float)) and r.get("quality") == "ok"]
    return round(statistics.median(v), 3) if v else None


def setup():
    from fieldfox import FieldFox
    link = Link(HOST, PORT)
    _, c = cmd(link, "get_config")
    _, p = cmd(link, "payload_status")
    print("before: state", c.get("state"), "tx_active", c.get("tx_active"), "pa", p.get("pa"), flush=True)
    if c.get("tx_active") or (p.get("pa") or {}).get("keyed"):
        print("NOT SETTING UP: TX active or the line keyed")
        return 2
    ip = cc.port_init_params(RX_PORT, TX_PORT, RATE, CENTER, 200, 40.0, clock="internal")
    st, r = cmd(link, "init", ip, timeout=120)
    print("init", st, r.get("achieved_rate"), r.get("err") or "", flush=True)
    if st != 0:
        return 1
    if not FF:
        return 0
    fx = FieldFox(FF)
    fx.write("POW:ATT 30")
    print("FieldFox attenuation", fx.query("POW:ATT?"), "centre", fx.query("FREQ:CENT?"), "span", fx.query("FREQ:SPAN?"),
          "RBW", fx.query("BAND?"), "ref", fx.query("DISP:WIND:TRAC:Y:RLEV?"), flush=True)
    return 0


def key(atten):
    from fieldfox import FieldFox
    if not 13.0 <= atten <= 41.75:
        print("TX attenuation out of the range for this run")
        return 2
    fx = FieldFox(FF) if FF else None
    link = Link(HOST, PORT)
    t0 = time.time()
    rows = []
    res = {"at": cc.iso_now(), "atten_db": atten, "ampl": AMPL, "center": CENTER, "rate": RATE, "seconds": SECONDS,
           "clock": "internal", "tx_port": TX_PORT}
    _, c = cmd(link, "get_config")
    s = sample(link, t0, "before")
    print("pre-flight: state", c.get("state"), "tx", c.get("tx_active"), "keyed", s["keyed"], "temp", s["temp_c"], flush=True)
    if c.get("state") != 1 or c.get("tx_active") or s["keyed"]:
        print("NOT KEYING: radio not initialized and idle")
        return 2
    rc = 1
    try:
        st, r = cmd(link, "set_tx_atten", {"qdb": int(round(atten * 4))})
        if st != 0:
            print("set_tx_atten refused:", r.get("err"))
            return 1
        st, r = cmd(link, "gen_test_waveform", {"kind": "tone", "freq": TONE_HZ, "seconds": 1.0, "ampl": AMPL,
                                                "name": "card_tone", "load_tx": True}, timeout=30)
        if st != 0:
            print("tone build failed:", r.get("err"))
            return 1
        for _ in range(3):
            rows.append(sample(link, t0, "ready"))
            time.sleep(0.5)
        st, r = cmd(link, "start_tx", {"mode": "CONTINUOUS"}, timeout=15)
        res["start_tx_status"] = st
        if st != 0:
            print("start_tx refused:", r.get("err"))
            return 1
        t_on = time.time()
        print(f"KEYED at {time.strftime('%T')}", flush=True)
        stop_why = None
        while time.time() - t_on < SECONDS:
            s = sample(link, t0, "keyed")
            rows.append(s)
            print(f"  {time.time() - t_on:5.1f} s keyed {s['keyed']} temp {s['temp_c']} C ({s['temp_v']} V) cur {s['cur_a']} A ({s['cur_v']} V) "
                  f"{s['quality']} rx {s['rx_atten_db']}", flush=True)
            ok = [x for x in rows[-3:] if x.get("quality") == "ok" and isinstance(x.get("temp_c"), (int, float))]
            if len(ok) == 3 and min(x["temp_c"] for x in ok) >= STOP_C:
                stop_why = f"temperature at {STOP_C} C"
                break
            if s["keyed"] is False and time.time() - t_on > 3.0:
                stop_why = "the HPA line dropped"
                break
            if fx and "analyzer_peak_dbm" not in res and time.time() - t_on > SECONDS / 2:
                y, x = fx.peak(sweeps=1)
                res["analyzer_peak_dbm"], res["analyzer_peak_hz"] = y, x
                print(f"  FieldFox peak {y:+.2f} dBm at {x / 1e6:.6f} MHz", flush=True)
                fx.screenshot(os.path.join(OUT, f"fieldfox_{int(atten)}dB.png"))
            time.sleep(0.6)
        res["stop_reason"] = stop_why or "time"
        rc = 0 if stop_why is None else 1
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {e}"
        print("FAILED:", res["error"], flush=True)
    finally:
        st, _ = cmd(link, "stop_tx", timeout=15)
        try:
            if fx:
                fx.resume()             # peak() leaves the FieldFox on a held sweep
        except Exception as e:
            print("FieldFox resume:", e, flush=True)
        print(f"stop_tx at {time.strftime('%T')} status {st}", flush=True)
        for _ in range(4):
            rows.append(sample(link, t0, "after"))
            time.sleep(1.0)
        _, c = cmd(link, "get_config")
        _, p = cmd(link, "payload_status")
        res["after"] = {"state": c.get("state"), "tx_active": c.get("tx_active"), "pa": p.get("pa"), "tr": p.get("tr")}
        print("after:", res["after"], flush=True)
        res["keyed_summary"] = {k: med([x for x in rows if x["phase"] == "keyed"], k) for k in ("temp_v", "temp_c", "cur_v", "cur_a", "rx_atten_db")}
        print("keyed medians:", res["keyed_summary"], flush=True)
        with open(os.path.join(OUT, f"tone_{int(atten)}dB.json"), "w") as fh:
            json.dump(res, fh, indent=1, default=str)
        with open(os.path.join(OUT, f"tone_{int(atten)}dB.csv"), "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    return rc


if __name__ == "__main__":
    if sys.argv[1] == "setup":
        sys.exit(setup())
    sys.exit(key(float(sys.argv[2])))
