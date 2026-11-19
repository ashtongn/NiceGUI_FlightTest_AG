#!/usr/bin/env python3
"""
FEC round-trip loopback test on one G40 (cross-card, TRx1B into TRx1A, 20 dB pad).

Transmits FSK/4FSK scenarios whose frame body is forward-error-corrected
(convolutional, punctured 1/2..7/8) and confirms the channelizer Viterbi-decodes
them back. An uncoded control runs first to prove the chain. Runs at 1.92 Msps
(decode-friendly). Nothing transmits without --arm.

  python3 bench/fec_loopback_test.py --host 192.168.0.22 --arm
  python3 bench/fec_loopback_test.py --host 192.168.0.22 --arm --reps 5 --json out.json
"""
from __future__ import annotations

import argparse
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

from dpmg.link.proto import CmdStatus  # noqa: E402
from fsk_loopback_test import Link, init_loopback, warmup  # noqa: E402
import checkout_common as cc  # noqa: E402

CENTER = 425_025_000
RATE = 1_920_000

# (label, modulation, symbol_rate, tone_spacing, fec-or-None, message)
CASES = [
    ("uncoded-ctrl", "FSK", 4800, 8000, None,                         "CTRL-NOFEC VK3ABC"),
    ("conv-1/2",     "FSK", 4800, 8000, {"type": "conv", "rate": "1/2"}, "FEC12 GRID-QF44"),
    ("conv-2/3",     "FSK", 4800, 8000, {"type": "conv", "rate": "2/3"}, "FEC23 HDG-270"),
    ("conv-3/4", "4FSK", 4800, 1296, {"type": "conv", "rate": "3/4"},    "FEC34 DMR-POSN"),
    ("conv-5/6",     "FSK", 4800, 8000, {"type": "conv", "rate": "5/6"}, "FEC56 BRG-090"),
    ("conv-7/8",     "FSK", 9600, 12000, {"type": "conv", "rate": "7/8"}, "FEC78 SPD-450"),
]


def spec_for(mod, sr, sp, fec, message):
    sig = {"modulation": mod, "symbol_rate": sr, "tone_spacing_hz": sp,
           "offset_hz": 300_000, "message": message, "amplitude": 0.9}
    if mod == "4FSK":
        sig["dibit_mapping"] = "binary"
    if fec:
        sig["fec"] = fec
    return {"name": "fec_case", "center_freq_hz": CENTER, "loop_s": 0.2,
            "sample_rate_hz": RATE, "signals": [sig]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("DPMG_G40_HOST"), required=not os.environ.get("DPMG_G40_HOST"))
    ap.add_argument("--port", type=int, default=47810)
    ap.add_argument("--arm", action="store_true")
    ap.add_argument("--gain", type=int, default=200)
    ap.add_argument("--atten", type=int, default=30)
    ap.add_argument("--reps", type=int, default=1,
                    help="key each case this many times (FEC-01 asks for 5)")
    ap.add_argument("--json", default=None, metavar="PATH",
                    help="also write per-case results as JSON here")
    cc.add_clock_args(ap)
    args = ap.parse_args()

    link = Link(args.host, args.port)
    print(f"connected to {link.serial}", flush=True)
    args.clock = cc.resolve_clock(args.clock, link.serial)
    init_loopback(link, RATE, CENTER, args.atten, args.gain, tx_card=1, kind="g40",
                  clock=args.clock, lock_timeout=args.lock_timeout)
    if not args.arm:
        print("not armed: RX only; exiting"); return
    time.sleep(1.5)
    warmup(link)                       # heat the decode pipeline (first decode is cold)

    results = []
    reps = max(1, int(args.reps))
    for label, mod, sr, sp, fec, message in CASES:
        for rep in range(reps):
            spec = spec_for(mod, sr, sp, fec, message)
            a = link.cmd("gen_scenario", {"spec": spec, "load_tx": False}, timeout=90)
            if a.status != CmdStatus.OK:
                print(f"  {label:<13} gen FAIL {a.result}", flush=True)
                results.append((label, "GEN-FAIL", "", rep, None)); continue
            iq = a.result.get("iq_path"); plan = a.result.get("plan_path")
            link.cmd("set_tx_waveform", {"kind": "file", "path": iq})
            link.cmd("set_plan", {"plan_path": plan})
            time.sleep(2.0)
            link.cmd("start_tx", {"mode": "CONTINUOUS"})
            ok, dt, obs, cnt = link.wait_decode(message, timeout=30)
            if not ok:                       # one retry (cold decode pipeline)
                link.cmd("stop_tx", {}); time.sleep(1.0)
                link.cmd("start_tx", {"mode": "CONTINUOUS"})
                ok, dt, obs, cnt = link.wait_decode(message, timeout=30)
            link.cmd("stop_tx", {})
            verdict = f"DECODED t={dt}s" if ok else "no-decode"
            print(f"  {label:<13} {mod:<4} {sr}bd fec={fec['rate'] if fec else '-':<4} "
                  f"-> {verdict}  saw={obs!r}", flush=True)
            results.append((label, "DECODED" if ok else "NO-DECODE", obs, rep,
                            dt if ok else None))
            time.sleep(0.5)

    link.cmd("stop_rx", {}); link.cmd("close", {})
    print("\n==== FEC ROUND-TRIP SUMMARY ====", flush=True)
    summary = {}
    for label, v, obs, rep, dt in results:
        print(f"  {label:<13} rep{rep + 1} {v}", flush=True)
        e = summary.setdefault(label, {"reps": 0, "decoded": 0, "t_s": []})
        e["reps"] += 1
        if v == "DECODED":
            e["decoded"] += 1
            e["t_s"].append(dt)
    for label, e in summary.items():
        print(f"  {label:<13} {e['decoded']}/{e['reps']}", flush=True)
    if args.json:
        import json
        out = {"host": args.host, "serial": link.serial, "rate_hz": RATE,
               "center_hz": CENTER, "gain": args.gain, "atten": args.atten,
               "reps": reps, "gps": cc.gps_summary(link, args.clock),
               "cases": [{"label": l, "mod": m, "symbol_rate": sr, "tone_spacing_hz": sp,
                          "fec": (fec or {}).get("rate", "none"), "message": msg,
                          "decoded": summary.get(l, {}).get("decoded", 0),
                          "reps": summary.get(l, {}).get("reps", 0),
                          "t_s": summary.get(l, {}).get("t_s", [])}
                         for (l, m, sr, sp, fec, msg) in CASES],
               "runs": [{"label": l, "verdict": v, "saw": o, "rep": r, "t_s": dt}
                        for (l, v, o, r, dt) in results]}
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w") as f:
            json.dump(out, f, indent=1)
        print(f"[saved] {args.json}", flush=True)


if __name__ == "__main__":
    main()
