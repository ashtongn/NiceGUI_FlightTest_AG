#!/usr/bin/env python3
"""
Operator-console functional sweep over the G40 cross-card loopback.

Drives the exact command set the /operator console issues: cross-card init
(RX TRx1A card0 + TX TRx1B card1), start_rx, the jammer generators, and IQ
scenario playback: across every G40 sample rate, and checks that energy
detections (and, where the symbol rate allows, channelizer decodes) come back
through the 20 dB loopback pad. This exercises the operator view's radio path
without the browser, so it is deterministic and loggable.

Nothing radiates without --arm.

  python3 bench/operator_loopback_test.py --host 192.168.0.22 --arm
"""
from __future__ import annotations

import argparse
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))   # repo root (proto.py)
sys.path.insert(0, _HERE)                    # bench/ (fsk_loopback_test.py)

from dpmg.link import proto  # noqa: E402  (path set above)
from dpmg.link.proto import MsgType, CmdStatus  # noqa: E402
from fsk_loopback_test import Link, init_loopback  # noqa: E402
import checkout_common as cc  # noqa: E402

CENTER = 425_025_000
RATES = [1_920_000, 7_680_000, 15_360_000, 30_720_000]

# jammer modes exactly as the operator Jammer panel builds them (kind + params)
JAMMERS = [
    ("barrage", {}),
    ("noise",   {"bw": 1_000_000}),
    ("tone",    {"freq": 300_000}),
    ("comb",    {"spacing": 200_000, "n_tones": 8, "center": 0}),
    ("swept",   {"span": 5_000_000, "sweep_period_s": 0.01, "noise_bw": 0, "center": 0}),
    ("chirp",   {"f0": -5_000_000, "f1": 5_000_000}),
]


def drain(link, secs=0.3):
    t0 = time.time()
    while time.time() - t0 < secs:
        try:
            link.s.recvfrom(65535)
        except Exception:
            break


def collect(link, seconds):
    """Listen for `seconds`; return (n_detections, peak_snr_db, n_decodes, last_msg)."""
    t0 = time.time()
    dets = {}
    decodes = {}
    last_msg = ""
    while time.time() - t0 < seconds:
        try:
            data, _ = link.s.recvfrom(65535)
        except Exception:
            continue
        try:
            mt, _seq, msg = proto.decode(data)
        except proto.ProtoError:
            continue
        if mt == MsgType.DETECTION:
            dets[msg.det_index] = msg.snr_db
        elif mt == MsgType.WATCH_UPDATE:
            for b in msg.bands:
                m = str(b.get("msg", ""))
                if m:
                    decodes[int(b.get("dn", 0))] = m
                    last_msg = m
    peak = max(dets.values()) if dets else float("nan")
    return len(dets), peak, len(decodes), last_msg


def scenario(rate):
    """A small multi-emitter FSK scenario sized to the rate's usable span, so the
    IQ-playback path has several signals at different frequencies at once. Built
    at the radio's current init rate (no sample_rate_hz in the spec)."""
    span = rate * 0.8
    offs = [-0.30 * span, -0.10 * span, 0.12 * span, 0.32 * span]
    mods = [("FSK", 4800, 8000), ("FSK", 9600, 12000),
            ("4FSK", 4800, 1296), ("FSK", 4800, 5000)]
    sigs = []
    for i, (fo, (mod, sr, sp)) in enumerate(zip(offs, mods)):
        sig = {"modulation": mod, "symbol_rate": sr, "tone_spacing_hz": sp,
               "offset_hz": int(fo), "message": f"OP-{mod}-{sr}-{i}",
               "amplitude": 0.20}
        if mod == "4FSK":
            sig["dibit_mapping"] = "binary"
        sigs.append(sig)
    return {"name": "op_scenario", "center_freq_hz": CENTER,
            "loop_s": 0.2, "signals": sigs}


def run_rate(link, rate, gain, atten, arm, clock="internal", lock_timeout=None):
    tag = f"{rate/1e6:.2f} Msps"
    print(f"\n{'='*64}\n RATE {tag}\n{'='*64}", flush=True)
    init_loopback(link, rate, CENTER, atten, gain, tx_card=1, kind="g40",
                  clock=clock, lock_timeout=lock_timeout)
    time.sleep(1.5)

    # confirm streaming health from a STATUS frame
    fps = xr = -1
    t0 = time.time()
    while time.time() - t0 < 4:
        try:
            data, _ = link.s.recvfrom(65535)
            mt, _s, m = proto.decode(data)
            if mt == MsgType.STATUS:
                fps, xr = m.fps, m.overruns + m.underruns
                break
        except Exception:
            continue
    print(f"[rx] streaming fps={fps} over+under={xr}", flush=True)

    rows = []
    if not arm:
        print("[skip] not armed: RX/PSD only", flush=True)
        return {"rate": tag, "fps": fps, "xr": xr, "rows": rows}

    # jammer modes
    for kind, extra in JAMMERS:
        p = {"kind": kind, "seconds": 0.5, "ampl": 0.5,
             "name": f"jam_{kind}", "load_tx": True, **extra}
        a = link.cmd("gen_test_waveform", p, timeout=60)
        if a.status != CmdStatus.OK:
            print(f"  jam {kind:<8} gen FAIL {a.result}", flush=True)
            rows.append((f"jam:{kind}", "GEN-FAIL", 0, 0)); continue
        drain(link)
        link.cmd("start_tx", {"mode": "CONTINUOUS"})
        nd, peak, _, _ = collect(link, 3.0)
        link.cmd("stop_tx", {})
        # DETECTION events are emitted when a burst ends: a steady tone / comb is one
        # burst that closes only after stop_tx (hold-over + clustering), so
        # listen after the key too and count both windows
        nd2, peak2, _, _ = collect(link, 2.5)
        nd += nd2
        peak = peak2 if (nd2 and not (peak == peak)) else (max(peak, peak2) if nd2 else peak)
        verdict = "DET" if nd > 0 else "no-det"
        print(f"  jam {kind:<8} {verdict:<7} dets={nd:<4} peakSNR={peak:5.1f} dB  (after stop: {nd2})", flush=True)
        rows.append((f"jam:{kind}", verdict, nd, round(peak, 1)))
        time.sleep(0.4)

    # IQ playback: multi-emitter scenario
    spec = scenario(rate)
    a = link.cmd("gen_scenario", {"spec": spec, "load_tx": True}, timeout=90)
    if a.status != CmdStatus.OK:
        print(f"  iq scenario gen FAIL {a.result}", flush=True)
        rows.append(("iq:scenario", "GEN-FAIL", 0, 0))
    else:
        drain(link)
        link.cmd("start_tx", {"mode": "CONTINUOUS"})
        nd, peak, ndec, lastm = collect(link, 8.0)
        link.cmd("stop_tx", {})
        verdict = "DET" if nd > 0 else "no-det"
        print(f"  iq scenario ({len(spec['signals'])} signals) {verdict} "
              f"dets={nd} decodes={ndec} last={lastm!r}", flush=True)
        rows.append(("iq:scenario", verdict, nd, ndec))

    link.cmd("stop_rx", {})
    link.cmd("close", {})
    return {"rate": tag, "fps": fps, "xr": xr, "rows": rows}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("DPMG_G40_HOST"), required=not os.environ.get("DPMG_G40_HOST"))
    ap.add_argument("--port", type=int, default=47810)
    ap.add_argument("--arm", action="store_true", help="allow TX (radiates into the pad)")
    ap.add_argument("--gain", type=int, default=255,
                    help="RX gain index; 255 for the 30 dB attenuation and 50 dB pad")
    ap.add_argument("--atten", type=float, default=30,
                    help="TX attenuation dB (bench rule: 30 or more through the 50 dB pad)")
    ap.add_argument("--json", default=None, metavar="PATH",
                    help="also write the per-rate summaries as JSON here")
    cc.add_clock_args(ap)
    args = ap.parse_args()

    link = Link(args.host, args.port)
    print(f"connected to {link.serial} at {args.host}:{args.port}", flush=True)
    args.clock = cc.resolve_clock(args.clock, link.serial)

    summaries = []
    for rate in RATES:
        summaries.append(run_rate(link, rate, args.gain, args.atten, args.arm,
                                  clock=args.clock, lock_timeout=args.lock_timeout))

    print(f"\n{'#'*64}\n SUMMARY\n{'#'*64}", flush=True)
    for s in summaries:
        print(f"\n{s['rate']}  (fps={s['fps']} over+under={s['xr']})", flush=True)
        for name, verdict, a, b in s["rows"]:
            print(f"   {name:<16} {verdict:<8} {a} {b}", flush=True)
    if args.json:
        import json
        out = {"host": args.host, "serial": link.serial, "center_hz": CENTER,
               "gain": args.gain, "atten": args.atten, "armed": bool(args.arm),
               "gps": cc.gps_summary(link, args.clock),
               "rates": [{"rate": s["rate"], "fps": s["fps"], "xr": s["xr"],
                          "rows": [{"name": n, "verdict": v, "dets": a, "x": b}
                                   for (n, v, a, b) in s["rows"]]} for s in summaries]}
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w") as f:
            json.dump(out, f, indent=1, default=str)
        print(f"[saved] {args.json}", flush=True)


if __name__ == "__main__":
    main()
