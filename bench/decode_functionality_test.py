#!/usr/bin/env python3
"""
End-to-end decode functionality test over the cross-card loopback.
Builds the fsk_sweep scenario (6 FSK signals, each a known message), loads its
decode plan into the channelizer, keys it, and checks the channelizer recovers
each band's message. TX TRx1B through the pad into RX TRx1A. Moderate RX gain to avoid
ADC overload (the service would auto-back-off anyway).
"""
import json, os, sys, time
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dpmg.link import proto
from dpmg.link.proto import MsgType
from fsk_loopback_test import Link, init_loopback
import checkout_common as cc

CENTER = 425_025_000
FS = 1_920_000
SPEC = json.load(open(os.path.join(os.path.dirname(__file__), "..",
                                   "scenarios", "json", "fsk_sweep.json")))
EXPECT = {s.get("message") for s in SPEC["signals"]}


def collect_decodes(L, seconds):
    """Return {band_name: (last_msg, decode_count)} and the set of messages."""
    out, msgs = {}, {}
    t0 = time.time()
    while time.time() - t0 < seconds:
        try:
            data, _ = L.s.recvfrom(65535)
            mt, _s, m = proto.decode(data)
        except Exception:
            continue
        if mt != MsgType.WATCH_UPDATE:
            continue
        for b in m.bands:
            msg = str(b.get("msg", ""))
            dn = int(b.get("dn", 0))
            name = str(b.get("n", b.get("f", "?")))
            if msg:
                out[name] = (msg, dn)
                msgs[msg] = msgs.get(msg, 0) + 1
    return out, msgs


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=os.environ.get("DPMG_G40_HOST"), required=not os.environ.get("DPMG_G40_HOST"), help="radio IP (or set DPMG_G40_HOST)")
    ap.add_argument("--port", type=int, default=47810)
    ap.add_argument("--gain", type=int, default=255,
                    help="RX gain index; 255 for the 30 dB attenuation and 50 dB pad")
    ap.add_argument("--atten", type=float, default=30,
                    help="TX attenuation dB (bench rule: 30 or more through the 50 dB pad)")
    ap.add_argument("--seconds", type=float, default=25.0, help="decode collection window")
    ap.add_argument("--json", default=None, metavar="PATH",
                    help="also write the per-message result as JSON here")
    cc.add_clock_args(ap)
    args = ap.parse_args()

    L = Link(args.host, args.port)
    print("connected", L.serial)
    print(f"scenario: {len(SPEC['signals'])} FSK signals @ {FS/1e6:g} MSps")
    print(f"expected messages: {sorted(EXPECT)}\n")
    # moderate gain (idx 210 ~ +11 dB) so the strong loopback doesn't clip
    args.clock = cc.resolve_clock(args.clock, L.serial)
    init_loopback(L, FS, CENTER, args.atten, args.gain, tx_card=1, kind="g40",
                  clock=args.clock, lock_timeout=args.lock_timeout)
    time.sleep(1.5)
    ack = L.cmd("gen_scenario", {"spec": SPEC, "load_tx": True}, timeout=40)
    r = ack.result or {}
    print(f"gen_scenario: status={ack.status} plan={os.path.basename(str(r.get('plan_path')))}")
    sp = L.cmd("set_plan", {"plan_path": r["plan_path"]}, timeout=15)
    print(f"set_plan: status={sp.status} {(sp.result or {})}")
    L.cmd("start_tx", {"mode": "CONTINUOUS"})
    print(f"TX keyed; collecting decodes {args.seconds:g} s...\n")
    out, msgs = collect_decodes(L, float(args.seconds))
    L.cmd("stop_tx", {}); L.cmd("stop_rx", {}); L.cmd("close", {})

    print("decoded bands:")
    for name, (msg, dn) in sorted(out.items()):
        print(f"   {name:<16} x{dn:<4} '{msg}'")
    got = set(msgs)
    hit = EXPECT & got
    print(f"\nrecovered {len(hit)}/{len(EXPECT)} expected messages")
    for e in sorted(EXPECT):
        print(f"   {'OK ' if e in got else 'XX '} '{e}'" + ("" if e in got else "  (not decoded)"))
    extra = got - EXPECT
    if extra:
        print(f"   (also saw: {sorted(extra)})")
    verdict = "PASS" if hit == EXPECT else "PARTIAL/FAIL"
    print(f"\n==== DECODE TEST: {verdict} ({len(hit)}/{len(EXPECT)}) ====")
    if args.json:
        res = {"host": args.host, "serial": L.serial, "rate_hz": FS, "center_hz": CENTER,
               "gain": args.gain, "atten": args.atten, "seconds": args.seconds,
               "expected": sorted(EXPECT), "recovered": sorted(hit),
               "missed": sorted(EXPECT - got), "extra": sorted(extra),
               "bands": {n: {"msg": m, "dn": dn} for n, (m, dn) in out.items()},
               "verdict": verdict, "gps": cc.gps_summary(L, args.clock)}
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w") as f:
            json.dump(res, f, indent=1)
        print(f"[saved] {args.json}")


if __name__ == "__main__":
    main()
