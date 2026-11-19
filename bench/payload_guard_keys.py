#!/usr/bin/env python3
"""PAYLOAD-04: the HPA line keyed with payload_key at TX attenuation 40 dB, no TX
waveform started. Stops the key on any exit. Refuses to start if TX is active or the line is
already keyed, or if the TX attenuation does not read 40 dB.

    payload_guard_keys.py unplugged  with the attenuator USB out, send one key that must be refused
    payload_guard_keys.py keyed      key about 20 s, try the two settings that must be refused
    payload_guard_keys.py pull       key and wait up to 60 s for the attenuator USB to be pulled
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
    _, c = cmd(link, "get_config")
    hm, pa, g, at = p.get("hpa_monitor") or {}, p.get("pa") or {}, p.get("guard") or {}, p.get("atten") or {}
    return {"t_s": round(time.time() - t0, 1), "keyed": pa.get("keyed"), "tr_tx": (p.get("tr") or {}).get("tx"),
            "armed": g.get("armed"), "atten_db": at.get("att_db"), "tx_active": c.get("tx_active"),
            "tx_gain": c.get("tx_gain"), "temp_c": hm.get("hpa_temp_c"), "cur_a": hm.get("hpa_current_a"),
            "cur_v": hm.get("hpa_current_v"), "quality": hm.get("quality"),
            "last_refusal": g.get("last_refusal")}


def show(s):
    print(f"  {s['t_s']:5.1f} s keyed {s['keyed']} switch TX {s['tr_tx']} armed {s['armed']} RX atten {s['atten_db']} "
          f"temp {s['temp_c']} C cur {s['cur_a']} A ({s['cur_v']} V) {s['quality']}", flush=True)


def main(step):
    link = Link(HOST, PORT)
    t0 = time.time()
    res = {"step": step, "at": cc.iso_now(), "samples": []}
    first = snap(link, t0)
    show(first)
    if first["tx_active"] or first["keyed"]:
        print("NOT KEYING: TX is active or the line is already keyed")
        return 2
    if first["tx_gain"] != 40.0:
        print("NOT KEYING: TX attenuation is not 40 dB:", first["tx_gain"])
        return 2
    if step == "unplugged":
        st, r = cmd(link, "payload_key", {"on": True}, timeout=15)
        print("key with the attenuator unplugged: status", st, r.get("err"), flush=True)
        show(snap(link, t0))
        return 0 if st != 0 else 1
    keyed = False
    rc = 1
    try:
        if step == "keyed":
            st, r = cmd(link, "atten_set", {"db": 30.0})
            print("RX attenuator to 30 dB before the key:", st, r, flush=True)
            if st != 0:
                return 1
        st, r = cmd(link, "payload_key", {"on": True}, timeout=15)
        res["key"] = {"status": st, "err": r.get("err")}
        print(f"KEY at {time.strftime('%T')}: status {st} {r.get('err') or ''}", flush=True)
        if st != 0:
            return 1
        keyed = True
        s = snap(link, t0)
        res["samples"].append(s)
        show(s)
        if step == "keyed":
            st, r = cmd(link, "atten_set", {"db": 30.0})
            res["try_rx_30"] = {"status": st, "err": r.get("err")}
            print(f"try RX attenuator 30 dB while keyed: status {st} {r.get('err')}", flush=True)
            st, r = cmd(link, "set_tx_atten", {"qdb": 48})
            res["try_tx_12"] = {"status": st, "err": r.get("err")}
            print(f"try TX attenuation 12 dB while keyed: status {st} {r.get('err')}", flush=True)
            end = time.time() + 16
            while time.time() < end:
                s = snap(link, t0)
                res["samples"].append(s)
                show(s)
                if s["keyed"] is False:
                    print("the line dropped by itself", flush=True)
                    break
                time.sleep(1.5)
            rc = 0
        else:
            print("PULL THE ATTENUATOR USB NOW", flush=True)
            end = time.time() + 60
            dropped = False
            while time.time() < end:
                s = snap(link, t0)
                res["samples"].append(s)
                show(s)
                if s["keyed"] is False:
                    dropped = True
                    res["dropped_at_s"] = s["t_s"]
                    print(f"HPA LINE DROPPED at {time.strftime('%T')}", flush=True)
                    break
                time.sleep(0.4)
            res["dropped"] = dropped
            rc = 0 if dropped else 1
            if not dropped:
                print("no drop within 60 s", flush=True)
    finally:
        st, r = cmd(link, "payload_key", {"on": False}, timeout=15)
        res["unkey"] = {"status": st, "err": r.get("err")}
        print(f"UNKEY at {time.strftime('%T')}: status {st} {r.get('err') or ''}", flush=True)
        time.sleep(1.0)
        s = snap(link, t0)
        res["after"] = s
        show(s)
        print("last refusal:", s["last_refusal"], flush=True)
        with open(os.path.join(OUT, f"{step}.json"), "w") as fh:
            json.dump(res, fh, indent=1, default=str)
        res["keyed_at_all"] = keyed
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
