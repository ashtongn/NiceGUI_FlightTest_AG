#!/usr/bin/env python3
"""
record_replay_check.py: Record & replay on a real G40, Setup A.

Cable: TX TRx1B then 50 dB pad then RX TRx1A. Clock gpsdo (the antenna bias must
already be on; this script never switches it). Transmits on TRx1B.

  1. init 1.92 MSps (RX gain 200, TX atten 30), build + key the 13-signal test
     card scenarios/json/mod_coding_card.json (gen_scenario spec_path + set_plan
     + start_tx CONTINUOUS) and wait for C01 OK .. C13 OK live
  2. record it while keyed: 1 s full band (rr_card_full) and a selected band
     around C07 (-50 kHz, rr_band_c07)
  3. stop TX, listen quietly (decode counters must stop)
  4. replay rr_card_full in a loop at 0 dB (as captured; over the cabled loop it
     arrives weaker, as the loop gain applies twice), then at the largest
     digital gain (the panel's cap): every C01..C13 counter must move past
     where the live card left it (fresh decodes from the replay). Two narrow
     bands (C04, C11) are recorded to measure the received level
  5. replay 6 dB below the cap: the level bands must drop ~6 dB
  6. replay rr_band_c07 alone (at its cap): C07 decodes, the others do not move
  7. stop TX, delete the level recordings (keeps rr_card_full / rr_band_c07)

Writes bench/RECORD_REPLAY_<date>.json + .md. Does not restore the demo state;
do that afterwards (checkout_common.init_ports(..., 30.72e6, ..., 255, 20, ...)).

    python3 bench/record_replay_check.py [--host 192.168.0.22] [--port 47810]
"""
from __future__ import annotations

import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# results go outside the source tree (output/ is not tracked); DPMG_BENCH_RESULTS overrides
RESULTS_DIR = os.environ.get("DPMG_BENCH_RESULTS") or os.path.join(os.path.dirname(HERE), "output", "bench")
os.makedirs(RESULTS_DIR, exist_ok=True)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import checkout_common as cc                      # noqa: E402
from dpmg.link.proto import CmdStatus                       # noqa: E402

CARD = "scenarios/json/mod_coding_card.json"
MSGS = [f"C{i:02d} OK" for i in range(1, 14)]
RATE = 1_920_000
CENTER = cc.CENTER
C07 = (CENTER - 80e3, CENTER - 20e3)              # C07 at -50 kHz, DC and neighbours out
LVL = {"C04": (CENTER - 365e3, CENTER - 335e3), "C11": (CENTER + 335e3, CENTER + 365e3)}


def cmd(link, name, params, timeout=60.0, must=True):
    a = cc.safe_cmd(link, name, params, timeout=timeout)
    r = a.result if isinstance(a.result, dict) else {}
    ok = a.status == CmdStatus.OK
    print(f"[{name}] {CmdStatus(a.status).name} "
          + ", ".join(f"{k}={r[k]}" for k in list(r)[:8] if k not in ("rec", "warnings")),
          flush=True)
    if must and not ok:
        raise SystemExit(f"{name} failed: {r}")
    return r


def decode_counts(link, seconds, stop_when_all=None):
    t = cc.Telem()
    t0 = time.time()
    while time.time() - t0 < seconds:
        cc.collect(link, 2.0, t)
        if stop_when_all and all(m in t.messages for m in stop_when_all):
            break
    return {m: n for m, n in t.messages.items()}


def level(link, tag):
    out = {}
    for sig, band in LVL.items():
        r = cmd(link, "record_iq", {"seconds": 0.5, "band": list(band), "name": f"rr_lvl_{sig}"})
        out[sig] = {"rms_dbfs": r["rms_dbfs"], "peak_dbfs": r["peak_dbfs"]}
    print(f"[level {tag}] {out}", flush=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("DPMG_G40_HOST"), required=not os.environ.get("DPMG_G40_HOST"))
    ap.add_argument("--port", type=int, default=47810)
    ap.add_argument("--rx-gain", type=int, default=200)
    ap.add_argument("--tx-atten", type=float, default=30)
    ap.add_argument("--clock", default="gpsdo", help="gpsdo (default); internal on a simulator")
    args = ap.parse_args()
    res = {"at": cc.iso_now(), "setup": "A: TX TRx1B -> 50 dB pad -> RX TRx1A",
           "rate": RATE, "rx_gain": args.rx_gain, "tx_atten": args.tx_atten}
    link = cc.Link(args.host, args.port)
    res["serial"] = link.serial
    try:
        a, _ = cc.init_ports(link, "TRx1A", "TRx1B", RATE, CENTER, args.rx_gain,
                             args.tx_atten, start_rx=True, clock=args.clock)
        if a.status != CmdStatus.OK:
            raise SystemExit("init failed")
        res["gps"] = cc.gps_log(link)[-1]
        # 1. live test card
        g = cmd(link, "gen_scenario", {"spec_path": CARD, "load_tx": True,
                                       "match_radio_rate": True}, timeout=180)
        cmd(link, "set_plan", {"plan_path": g["plan_path"]}, timeout=30,
            must=args.clock == "gpsdo")            # a sim may keep its data outside its cwd
        cmd(link, "start_tx", {"mode": "CONTINUOUS"}, timeout=30)
        live = decode_counts(link, 60 if args.clock == "gpsdo" else 4, stop_when_all=MSGS)
        res["live"] = {m: live.get(m) for m in MSGS}
        print(f"[live] {sum(m in live for m in MSGS)}/13 decoded", flush=True)
        time.sleep(3)                                  # a few more frames into the counters
        # 2. record while keyed
        res["rec_full"] = cmd(link, "record_iq", {"seconds": 1.0, "name": "rr_card_full"})
        res["rec_band"] = cmd(link, "record_iq", {"seconds": 1.0, "band": list(C07),
                                                  "name": "rr_band_c07"})
        live2 = decode_counts(link, 4)
        # 3. stop, quiet
        cmd(link, "stop_tx", {})
        time.sleep(2)
        quiet = decode_counts(link, 8)
        base = {m: max(live.get(m, 0), live2.get(m, 0), quiet.get(m, 0)) for m in MSGS}
        res["baseline_dn"] = base
        time.sleep(4)
        quiet2 = decode_counts(link, 6)
        res["quiet_moved"] = [m for m in MSGS if quiet2.get(m, 0) > base[m]]
        base = {m: max(base[m], quiet2.get(m, 0)) for m in MSGS}
        # 4. replay full at 0 dB (as captured). Over a cabled loop the replay goes
        # through the loop gain a second time, so 0 dB arrives weaker than the card
        res["replay_0"] = cmd(link, "replay_recording", {"name": "rr_card_full", "gain_db": 0.0})
        cmd(link, "start_tx", {"mode": "CONTINUOUS"}, timeout=30)
        r0 = decode_counts(link, 30)
        res["replay_0_fresh"] = [m for m in MSGS if r0.get(m, 0) > base[m]]
        print(f"[replay 0 dB] fresh decodes {len(res['replay_0_fresh'])}/13", flush=True)
        res["level_0"] = level(link, "0 dB")
        cmd(link, "stop_tx", {})
        base = {m: max(base[m], r0.get(m, 0)) for m in MSGS}
        # 4b. replay at the largest digital gain (the panel's cap): decodes must return
        gmax = float(res["replay_0"]["gain_max_db"])
        res["gain_max_db"] = gmax
        res["replay_max"] = cmd(link, "replay_recording", {"name": "rr_card_full", "gain_db": gmax})
        cmd(link, "start_tx", {"mode": "CONTINUOUS"}, timeout=30)
        rm = decode_counts(link, 45)
        res["replay_max_dn"] = {m: rm.get(m) for m in MSGS}
        res["replay_max_fresh"] = [m for m in MSGS if rm.get(m, 0) > base[m]]
        print(f"[replay {gmax:+.1f} dB] fresh decodes {len(res['replay_max_fresh'])}/13: "
              f"{res['replay_max_fresh']}", flush=True)
        res["level_max"] = level(link, f"{gmax:+.1f} dB")
        cmd(link, "stop_tx", {})
        base = {m: max(base[m], rm.get(m, 0)) for m in MSGS}
        # 5. 6 dB lower: the received level must drop ~6 dB
        res["replay_m6"] = cmd(link, "replay_recording", {"name": "rr_card_full",
                                                          "gain_db": gmax - 6.0})
        cmd(link, "start_tx", {"mode": "CONTINUOUS"}, timeout=30)
        res["level_m6"] = level(link, f"{gmax - 6:+.1f} dB")
        r6 = decode_counts(link, 30)
        res["replay_m6_fresh"] = [m for m in MSGS if r6.get(m, 0) > base[m]]
        cmd(link, "stop_tx", {})
        base = {m: max(base[m], r6.get(m, 0)) for m in MSGS}
        res["level_drop_db"] = {s: round(res["level_max"][s]["rms_dbfs"] -
                                         res["level_m6"][s]["rms_dbfs"], 2) for s in LVL}
        print(f"[level drop] {res['level_drop_db']}", flush=True)
        # 6. selected-band replay (C07 only). The GPU decoder keeps working through
        # its backlog for some seconds after STOP; let it drain before the baseline
        drained = decode_counts(link, 15)
        base = {m: max(base[m], drained.get(m, 0)) for m in MSGS}
        quiet3 = decode_counts(link, 8)
        base = {m: max(base[m], quiet3.get(m, 0)) for m in MSGS}
        bg = cmd(link, "replay_recording", {"name": "rr_band_c07", "gain_db": 0.0})
        res["replay_band"] = cmd(link, "replay_recording",
                                 {"name": "rr_band_c07", "gain_db": bg["gain_max_db"]})
        cmd(link, "start_tx", {"mode": "CONTINUOUS"}, timeout=30)
        rb = decode_counts(link, 30)
        cmd(link, "stop_tx", {})
        res["replay_band_fresh"] = [m for m in MSGS if rb.get(m, 0) > base[m]]
        print(f"[band replay] fresh {res['replay_band_fresh']}", flush=True)
        # 7. list, then delete the level recordings
        res["list"] = cmd(link, "list_recordings", {})
        for s in LVL:
            cmd(link, "delete_recording", {"name": f"rr_lvl_{s}"}, must=False)
    finally:
        cc.safe_cmd(link, "stop_tx", {}, timeout=30)
    ok = (len([m for m in MSGS if res["live"].get(m)]) == 13
          and len(res["replay_max_fresh"]) == 13 and not res["quiet_moved"]
          and all(4.5 <= d <= 7.5 for d in res["level_drop_db"].values())
          and res["replay_band_fresh"] == ["C07 OK"])
    res["pass"] = ok
    md = (f"# Record & replay check {res['at']}\n\n"
          f"- radio {res['serial']}, {res['setup']}, {RATE/1e6:g} MSps, RX gain "
          f"{args.rx_gain}, TX atten {args.tx_atten} dB, clock {args.clock}\n"
          f"- live card: {sum(1 for m in MSGS if res['live'].get(m))}/13\n"
          f"- recorded: {res['rec_full']['seconds']} s full band "
          f"({res['rec_full']['size_bytes']/1e6:.1f} MB, peak {res['rec_full']['peak_dbfs']} dBFS); "
          f"band C07 {res['rec_band']['rate_hz']/1e3:.0f} kSps\n"
          f"- quiet after STOP: counters moved for {res['quiet_moved'] or 'none'}\n"
          f"- replay 0 dB (as captured): fresh decodes {len(res['replay_0_fresh'])}/13, "
          f"C04/C11 band rms {res['level_0']['C04']['rms_dbfs']}/"
          f"{res['level_0']['C11']['rms_dbfs']} dBFS\n"
          f"- replay at the gain cap {res['gain_max_db']:+.1f} dB: fresh decodes "
          f"{len(res['replay_max_fresh'])}/13, C04/C11 band rms "
          f"{res['level_max']['C04']['rms_dbfs']}/{res['level_max']['C11']['rms_dbfs']} dBFS\n"
          f"- 6 dB lower: fresh decodes {len(res['replay_m6_fresh'])}/13, level drop "
          f"{res['level_drop_db']} dB\n"
          f"- band replay (C07 only): fresh {res['replay_band_fresh']}\n"
          f"- result: {'PASS' if ok else 'FAIL'}\n")
    cc.write_results(os.path.join(RESULTS_DIR, f"RECORD_REPLAY_{cc.date_tag()}.json"), res, md)
    print(md)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
