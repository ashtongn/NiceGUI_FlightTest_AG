#!/usr/bin/env python3
"""PAYLOAD-01: switch refused to RX while keyed, then a manual hold past the
watchdog and SAFE. HPA line only, no TX waveform. Refuses to start unless TX is off, the line
is off and the TX attenuation reads 40 dB. Sends SAFE on any exit.
"""
import json
import os
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


def cmd(link, name, params=None, timeout=10):
    a = cc.safe_cmd(link, name, params or {}, timeout=timeout)
    return int(a.status), (a.result if isinstance(a.result, dict) else {})


def snap(link, t0):
    _, p = cmd(link, "payload_status")
    _, f = cmd(link, "frontend_status")
    fe = f.get("frontend") or f
    hm, pa, g, at = p.get("hpa_monitor") or {}, p.get("pa") or {}, p.get("guard") or {}, p.get("atten") or {}
    return {"t_s": round(time.time() - t0, 1), "keyed": pa.get("keyed"), "level": pa.get("level"),
            "tr_tx": (p.get("tr") or {}).get("tx"), "armed": g.get("armed"), "atten_db": at.get("att_db"),
            "temp_c": hm.get("hpa_temp_c"), "cur_v": hm.get("hpa_current_v"), "cur_a": hm.get("hpa_current_a"),
            "quality": hm.get("quality"), "wd_trips": fe.get("watchdog_trips", fe.get("wd_trips")),
            "fault_trips": fe.get("fault_trips"), "watchdog_ms": fe.get("watchdog_ms"), "hold": fe.get("hold")}


def show(s):
    print(f"  {s['t_s']:5.1f} s keyed {s['keyed']} level {s['level']} switch TX {s['tr_tx']} RX atten {s['atten_db']} "
          f"temp {s['temp_c']} C cur {s['cur_a']} A wd {s['watchdog_ms']} trips {s['wd_trips']} hold {s['hold']}", flush=True)


def main():
    link = Link(HOST, PORT)
    t0 = time.time()
    res = {"at": cc.iso_now(), "samples": []}
    _, c = cmd(link, "get_config")
    s = snap(link, t0)
    show(s)
    if c.get("tx_active") or s["keyed"] or s["tr_tx"]:
        print("NOT KEYING: TX active, line keyed or switch in TX")
        return 2
    if c.get("tx_gain") != 40.0:
        print("NOT KEYING: TX attenuation is not 40 dB:", c.get("tx_gain"))
        return 2
    rc = 1
    try:
        print(f"switch check: key at {time.strftime('%T')}", flush=True)
        st, r = cmd(link, "payload_key", {"on": True}, timeout=15)
        res["step3_key"] = {"status": st, "err": r.get("err")}
        print("  key status", st, r.get("err") or "", flush=True)
        if st != 0:
            return 1
        time.sleep(1.0)
        s = snap(link, t0)
        res["samples"].append(s)
        show(s)
        st, r = cmd(link, "frontend_set", {"tr": "rx"})
        res["step3_try_rx"] = {"status": st, "err": r.get("err")}
        print("  switch to RX while keyed: status", st, r.get("err"), flush=True)
        time.sleep(2.0)
        s = snap(link, t0)
        res["samples"].append(s)
        show(s)
        res["step3_still_keyed"] = bool(s["keyed"]) and bool(s["tr_tx"])
        st, r = cmd(link, "payload_key", {"on": False}, timeout=15)
        print(f"  unkey at {time.strftime('%T')}: status", st, r.get("err") or "", flush=True)
        time.sleep(1.0)
        s = snap(link, t0)
        res["step3_after"] = s
        show(s)
        if s["keyed"]:
            print("the line did not drop on command, stopping")
            return 1

        print("gap 5 s", flush=True)
        time.sleep(5.0)
        print(f"manual hold: switch to TX, PA ON at {time.strftime('%T')}", flush=True)
        st, r = cmd(link, "frontend_set", {"tr": "tx"})
        res["step6_tr"] = {"status": st, "err": r.get("err")}
        print("  switch to TX: status", st, r.get("err") or "", flush=True)
        if st != 0:
            return 1
        st, r = cmd(link, "frontend_set", {"pa": True})
        res["step6_pa"] = {"status": st, "err": r.get("err")}
        print("  PA ON: status", st, r.get("err") or "", flush=True)
        if st != 0:
            return 1
        end = time.time() + 5.0
        while time.time() < end:
            s = snap(link, t0)
            res["samples"].append(s)
            show(s)
            time.sleep(0.7)
        res["step6_held"] = bool(s["keyed"])
        st, r = cmd(link, "frontend_set", {"safe": True})
        print(f"  SAFE at {time.strftime('%T')}: status", st, r.get("err") or "", flush=True)
        time.sleep(1.0)
        s = snap(link, t0)
        res["step6_after"] = s
        show(s)
        rc = 0
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {e}"
        print("FAILED:", res["error"], flush=True)
    finally:
        st, r = cmd(link, "frontend_set", {"safe": True})
        print("final SAFE: status", st, flush=True)
        time.sleep(0.5)
        s = snap(link, t0)
        res["final"] = s
        show(s)
        with open(os.path.join(OUT, "payload_interlocks.json"), "w") as fh:
            json.dump(res, fh, indent=1, default=str)
    return rc


if __name__ == "__main__":
    sys.exit(main())
