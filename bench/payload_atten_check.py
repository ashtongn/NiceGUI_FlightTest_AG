#!/usr/bin/env python3
"""PAYLOAD-03 through the service commands the GUI buttons send (atten_get,
atten_set). No init, RX or TX. Refuses to start if TX is active or the HPA line is keyed,
and sets 120 dB again on any exit.

    payload_atten_check.py run      read, set 30 dB, read, set 120 dB with the power-on default, read
    payload_atten_check.py after    read back after the USB power cycle
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
OUT = os.path.join(OUT, "payload_atten.json")


def result(ack):
    r = ack.result if isinstance(ack.result, dict) else {}
    return int(ack.status), r


def idle_check(link):
    c = cc.safe_cmd(link, "get_config", {}, timeout=5).result or {}
    p = cc.safe_cmd(link, "payload_status", {}, timeout=5).result or {}
    pa = (p.get("pa") or {}) if isinstance(p, dict) else {}
    return c.get("tx_active"), pa.get("keyed"), c.get("state")


def main(mode):
    res = {}
    if os.path.exists(OUT):
        res = json.load(open(OUT))
    link = Link(HOST, PORT)
    tx, keyed, state = idle_check(link)
    print(f"pre-flight: state {state} tx_active {tx} keyed {keyed}", flush=True)
    if tx or keyed:
        print("NOT RUNNING: TX is active or the HPA line is keyed")
        return 2
    if mode == "after":
        st, r = result(cc.safe_cmd(link, "atten_get", {}, timeout=10))
        res["after_power_cycle"] = {"status": st, **r, "at": cc.iso_now()}
        print("after the USB power cycle:", json.dumps(res["after_power_cycle"]), flush=True)
        json.dump(res, open(OUT, "w"), indent=1, default=str)
        return 0 if st == 0 else 1

    res["at"] = cc.iso_now()
    st, r = result(cc.safe_cmd(link, "atten_get", {}, timeout=10))
    res["step2_read"] = {"status": st, **r}
    print("step 2 read:", json.dumps(res["step2_read"]), flush=True)
    rc = 1
    try:
        st, r = result(cc.safe_cmd(link, "atten_set", {"db": 30.0}, timeout=10))
        res["step3_set30"] = {"status": st, **r}
        print("step 3 set 30:", json.dumps(res["step3_set30"]), flush=True)
        time.sleep(0.5)
        st, r = result(cc.safe_cmd(link, "atten_get", {}, timeout=10))
        res["step3_read"] = {"status": st, **r}
        print("step 3 read:", json.dumps(res["step3_read"]), flush=True)
        rc = 0
    finally:
        st, r = result(cc.safe_cmd(link, "atten_set", {"db": 120.0, "startup": True}, timeout=10))
        res["step4_set120_startup"] = {"status": st, **r}
        print("step 4 set 120 with power-on default:", json.dumps(res["step4_set120_startup"]), flush=True)
        time.sleep(0.5)
        st, r2 = result(cc.safe_cmd(link, "atten_get", {}, timeout=10))
        res["step4_read"] = {"status": st, **r2}
        print("step 4 read:", json.dumps(res["step4_read"]), flush=True)
        json.dump(res, open(OUT, "w"), indent=1, default=str)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "run"))
