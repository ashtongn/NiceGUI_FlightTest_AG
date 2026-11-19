#!/usr/bin/env python3
"""
rx_drain_soak.py: RX drain soak for the native drain (rxdrain.c).

Setup A cross-card: TX TRx1B (card 1) through the pad into RX TRx1A (card 0), GPSDO.
One case = init at --rate, load the plan (anchor tx), start RX, run for
--minutes with the detector and PSD live (this script is a GUI session, so
the service sends PSD frames), optionally key the loaded TX waveform the whole
time (--tx, TX at --tx-rate on its own card). Counted over the case:

  overruns   STATUS overrun counter delta (forward timestamp gaps = card overruns)
  rxgap      timestamp mismatches in the dpmg_svc log ([RXGAP] lines, read over ssh)
  drain      the last [DRAIN] lines: engine, receive-loop ratio, post ms, cycle ratio
  fps / flags from STATUS, channelizer [STAT] dropped frames

Also --snapshot FILE (get_config to JSON) and --restore FILE (re-init to the
snapshot, plan, start RX, compare field by field; the TX waveform source is
kept by the service across inits, so it is only compared).

    python3 bench/rx_drain_soak.py --snapshot output/bench/rx_drain/before_get_config.json
    python3 bench/rx_drain_soak.py --rate 61.44e6 --minutes 10 --label rx61
    python3 bench/rx_drain_soak.py --rate 61.44e6 --minutes 10 --tx --tx-rate 30.72e6 --label rxtx61
    python3 bench/rx_drain_soak.py --restore output/bench/rx_drain/before_get_config.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# results go outside the source tree (output/ is not tracked); DPMG_BENCH_RESULTS overrides
RESULTS_DIR = os.environ.get("DPMG_BENCH_RESULTS") or os.path.join(os.path.dirname(HERE), "output", "bench")
os.makedirs(RESULTS_DIR, exist_ok=True)
sys.path.insert(0, HERE)
import checkout_common as cc                     # noqa: E402
from checkout_common import Link, safe_cmd, CmdStatus  # noqa: E402

PLAN_PATH = "data/waveforms/4fsk_dmr.plan.json"
OUT_DIR = os.path.join(RESULTS_DIR, "rx_drain")
COMPARE = ("inited", "state", "tx_active", "kind", "center_hz", "tx_center_hz", "sample_rate",
           "achieved_rate", "tx_sample_rate", "rx_gain", "tx_gain", "clock_source",
           "rx_antenna", "tx_antenna", "card", "tx_card", "chan_mode", "psd", "tx_wf",
           "plan_entries", "plan_anchor", "ant_bias", "ref_locked")


def ssh(host, cmd, timeout=60):
    r = subprocess.run(["ssh", "-o", "ConnectTimeout=8", f"sidekiq@{host}", cmd],
                       capture_output=True, text=True, timeout=timeout)
    return r.stdout


def svc_log_since(host, since_iso):
    return ssh(host, f"docker logs --since {since_iso} dpmg_svc 2>&1", timeout=120)


def rxgap_count(log):
    n = lines = 0
    for line in log.splitlines():
        if line.startswith("[RXGAP]"):
            lines += 1
            n += 1
            if "more in this batch" in line:
                n += int(line.split("(+")[1].split()[0])
    return n, lines


def get_config(link):
    a = safe_cmd(link, "get_config", {}, timeout=15)
    return a.result if isinstance(a.result, dict) else {}


def port_of(card, ant):
    for name, p in cc.PORT_MAP.items():
        if p["card"] == card and ant in (p["rx"], p["tx"]):
            return name
    raise SystemExit(f"no port for card {card} {ant}")


def init_case(link, rate, center, gain, atten, clock, tx_rate=None):
    p = cc.port_init_params("TRx1A", "TRx1B", rate, center, gain, atten, clock=clock)
    if tx_rate and abs(tx_rate - rate) > 1:
        p["extras"]["tx_sample_rate"] = int(tx_rate)        # TX card at its own rate
    t0 = time.time()
    a = safe_cmd(link, "init", p, timeout=150)
    if a.status != CmdStatus.OK:
        raise SystemExit(f"init failed: {CmdStatus(a.status).name} {cc.ack_err(a)}")
    print(f"[init] OK {rate/1e6:.2f} MSps (TX {float(tx_rate or rate)/1e6:.2f}) in "
          f"{time.time()-t0:.1f}s achieved={a.result.get('achieved_rate')}", flush=True)
    cc.gpsdo_gate(link, clock, label=f"rx_drain {rate/1e6:.2f}")
    a = safe_cmd(link, "set_plan", {"plan_path": PLAN_PATH, "anchor": "tx"}, timeout=60)
    if a.status != CmdStatus.OK:
        raise SystemExit(f"set_plan failed: {cc.ack_err(a)}")
    a = safe_cmd(link, "start_rx", {}, timeout=30)
    if a.status != CmdStatus.OK:
        raise SystemExit(f"start_rx failed: {cc.ack_err(a)}")


def soak(args):
    link = Link(args.host, args.port)
    init_case(link, args.rate, args.center, args.gain, args.atten, args.clock,
              args.tx_rate if args.tx else None)
    time.sleep(args.settle)                      # start-up transient, not counted
    st0 = cc.collect(link, 3.0).status or {}
    t_start = dt.datetime.now(dt.timezone.utc)
    since = t_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    keyed = False
    if args.tx and args.tx_file:
        a = safe_cmd(link, "set_tx_waveform", {"kind": "file", "path": args.tx_file}, timeout=60)
        if a.status != CmdStatus.OK:
            raise SystemExit(f"set_tx_waveform {args.tx_file}: {cc.ack_err(a)}")
        print(f"[tx] loaded {args.tx_file}: {a.result.get('waveform')}", flush=True)
    if args.tx and args.tx_gen:
        a = safe_cmd(link, "gen_test_waveform", json.loads(args.tx_gen), timeout=120)
        if a.status != CmdStatus.OK:
            raise SystemExit(f"gen_test_waveform: {cc.ack_err(a)}")
        print(f"[tx] generated + loaded: {json.loads(args.tx_gen)}", flush=True)
    if args.tx:
        print(f"[tx] keying: {get_config(link).get('tx_wf')}", flush=True)
        a = safe_cmd(link, "start_tx", {"mode": "CONTINUOUS"}, timeout=30)
        keyed = a.status == CmdStatus.OK
        print(f"[tx] start_tx {CmdStatus(a.status).name} {cc.ack_err(a) if not keyed else ''}",
              flush=True)
        if not keyed:
            raise SystemExit("TX did not start")
    per_min = []
    t_end = time.time() + 60.0 * args.minutes
    last = st0
    try:
        while time.time() < t_end:
            tel = cc.collect(link, min(60.0, max(1.0, t_end - time.time())))
            s = tel.status or last
            per_min.append({"t": round(time.time() - (t_end - 60.0 * args.minutes), 1),
                            "overruns": s.get("overruns", 0) - st0.get("overruns", 0),
                            "fps": s.get("fps"), "flags": s.get("flags"),
                            "detections": len(tel.detections)})
            last = s
            print(f"[soak] t+{per_min[-1]['t']:.0f}s overruns +{per_min[-1]['overruns']} "
                  f"fps {s.get('fps')} flags {s.get('flags')} det {len(tel.detections)}", flush=True)
    finally:
        if keyed:
            safe_cmd(link, "stop_tx", {}, timeout=30)
    log = svc_log_since(args.host, since)
    gaps, gap_lines = rxgap_count(log)
    drain = [l for l in log.splitlines() if l.startswith("[DRAIN]")]
    engine = [l for l in log.splitlines() if "drain engine" in l]
    stat = [l for l in log.splitlines() if l.startswith("[STAT]")]
    res = {"label": args.label, "at": t_start.isoformat(), "rate": args.rate,
           "tx": bool(args.tx), "tx_rate": args.tx_rate if args.tx else None,
           "tx_file": args.tx_file if args.tx else None,
           "minutes": args.minutes, "overruns": last.get("overruns", 0) - st0.get("overruns", 0),
           "rxgap_mismatches": gaps, "rxgap_lines": gap_lines,
           "drain_lines": len(drain), "drain_last": drain[-5:], "engine": engine[-1:],
           "chan_stat_last": stat[-2:], "per_min": per_min,
           "gps": cc.gps_log(link)}
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"soak_{args.label}.json")
    json.dump(res, open(path, "w"), indent=1)
    print(f"[soak] {args.label}: overruns {res['overruns']}  [RXGAP] mismatches {gaps} "
          f"({gap_lines} lines)  -> {path}", flush=True)
    for l in drain[-3:]:
        print("   " + l)
    return 0 if (res["overruns"] == 0 and gaps == 0) else 1


def snapshot(args):
    link = Link(args.host, args.port)
    c = get_config(link)
    os.makedirs(os.path.dirname(os.path.abspath(args.snapshot)), exist_ok=True)
    json.dump(c, open(args.snapshot, "w"), indent=1)
    print(f"[snapshot] written to {args.snapshot}")
    return 0


def restore(args):
    want = json.load(open(args.restore))
    link = Link(args.host, args.port)
    rxp = port_of(int(want["card"]), want["rx_antenna"])
    txp = port_of(int(want["tx_card"]), want["tx_antenna"])
    if (rxp, txp) != ("TRx1A", "TRx1B"):
        raise SystemExit(f"snapshot ports {rxp}/{txp} are not the Setup A pair this script restores")
    tx_rate = want.get("tx_sample_rate")
    init_case(link, float(want["sample_rate"]), float(want["center_hz"]), float(want["rx_gain"]),
              float(want["tx_gain"]), want["clock_source"], tx_rate)
    if want.get("plan_entries") is None:
        print("[restore] snapshot had no plan; plan loaded anyway (cannot unload)")
    time.sleep(3)
    have = get_config(link)
    diffs = []
    for k in COMPARE:
        if have.get(k) != want.get(k):
            diffs.append((k, want.get(k), have.get(k)))
    for k, w, h in diffs:
        print(f"[restore] DIFF {k}: before {w!r} now {h!r}")
    print(f"[restore] {len(COMPARE) - len(diffs)}/{len(COMPARE)} fields equal")
    if args.after:
        json.dump(have, open(args.after, "w"), indent=1)
    return 0 if not diffs else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=os.environ.get("DPMG_G40_HOST"), required=not os.environ.get("DPMG_G40_HOST"))
    ap.add_argument("--port", type=int, default=47810)
    ap.add_argument("--rate", type=float, default=61.44e6)
    ap.add_argument("--center", type=float, default=2.4e9)
    ap.add_argument("--gain", type=float, default=255)
    ap.add_argument("--atten", type=float, default=30)
    ap.add_argument("--clock", default="gpsdo")
    ap.add_argument("--minutes", type=float, default=10.0)
    ap.add_argument("--settle", type=float, default=5.0)
    ap.add_argument("--tx", action="store_true", help="key the loaded TX waveform for the whole case")
    ap.add_argument("--tx-rate", type=float, default=30.72e6, dest="tx_rate")
    ap.add_argument("--tx-file", dest="tx_file",
                    help="with --tx: load this file (set_tx_waveform kind=file) before keying")
    ap.add_argument("--tx-gen", dest="tx_gen",
                    help="with --tx: gen_test_waveform params (JSON, load_tx true) before keying")
    ap.add_argument("--label", default="case")
    ap.add_argument("--snapshot")
    ap.add_argument("--restore")
    ap.add_argument("--after", help="with --restore: write the restored get_config here")
    args = ap.parse_args(argv)
    if args.snapshot:
        return snapshot(args)
    if args.restore:
        return restore(args)
    return soak(args)


if __name__ == "__main__":
    sys.exit(main())
