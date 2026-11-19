#!/usr/bin/env python3
"""
dsss_multiuser.py: DSSS carrier families + multi-user CDMA overlays
(DSSS_BUILDER, docs/DSSS_BUILDER.md): offline replay through the real decode
path, then the same scenarios keyed on the G40 (Setup A, one card, TRx1B to pad to
TRx1A) when the radio is free. Stage `dsss_multiuser` of
bench/first_checkout.py (opt-in: --dsss-multiuser or --only dsss_multiuser).

Cases (each one scenario JSON built by gen_scenario_json, every plan entry
with an explicit code spec):
  MOD-DSSS-C  one DSSS signal per carrier family: BPSK / QPSK / OQPSK /
              pi4-QPSK with an lfsr code, plus dual-code QPSK; 5/5 each
  MOD-DSSS-4  dsss_4user_bpsk preset: 4 users, Gold n=5 (31 chips), 2400 Bd,
              one centre; every user decodes
  MOD-DSSS-M  dsss_mixed_carriers preset: BPSK/QPSK/OQPSK/pi4 users, Gold
              n=6 (63 chips); every user decodes
  MOD-DSSS-16 dsss_16user preset: 16 users, Gold n=7 (127 chips), 1200 Bd;
              >= 13 of 16 per window, all 16 over the run (equal-power MUI
              ~9 dB predicted, docs/DSSS_BUILDER.md): recorded

  python3 bench/dsss_multiuser.py --replay --out bench/RESULTS_<date>/dsss_multiuser_replay.json
  python3 bench/dsss_multiuser.py --host 192.168.0.22 --arm --clock gpsdo --pad-db 50 \\
      --replay-json bench/RESULTS_<date>/dsss_multiuser_replay.json --out ...dsss_multiuser.json

Nothing transmits without --arm. --resume keeps cases already complete in --out.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# results go outside the source tree (output/ is not tracked); DPMG_BENCH_RESULTS overrides
RESULTS_DIR = os.environ.get("DPMG_BENCH_RESULTS") or os.path.join(os.path.dirname(HERE), "output", "bench")
os.makedirs(RESULTS_DIR, exist_ok=True)
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import numpy as np                                                  # noqa: E402

import checkout_common as cc                                        # noqa: E402

CENTER = cc.CENTER
OFF = 300_000.0


def _sig(carrier, msg, **extra):
    s = {"modulation": "DSSS", "symbol_rate": 2400, "offset_hz": OFF, "message": msg,
         "amplitude": 0.9, "carrier": carrier, "user": msg.split()[0], "name": msg.split()[0]}
    s.update(extra)
    return s


def build_cases():
    from dpmg.scenario import gen_scenario_json as g
    C = []
    lf = {"poly": [5, 2], "state": 1}
    cars = [("bpsk", {"lfsr": lf}), ("qpsk", {"lfsr": dict(lf, state=9)}),
            ("oqpsk", {"lfsr": {"poly": [6, 1], "form": "galois"}}),
            ("pi4qpsk", {"code_spec": {"family": "gold", "polys": [[5, 2], [5, 4, 3, 2]], "offset": 3}}),
            ("qpsk", {"code_spec": {"family": "gold", "polys": [[5, 2], [5, 4, 3, 2]], "offset": 1},
                      "code_spec_q": {"family": "gold", "polys": [[5, 2], [5, 4, 3, 2]], "offset": 6}})]
    for i, (car, extra) in enumerate(cars):
        tag = f"{car}{'-dual' if 'code_spec_q' in extra else ''}"
        C.append({"id": f"MOD-DSSS-C/{tag}", "plan": "MOD-DSSS-C", "rate": cc.RATE_NARROW,
                  "spec": {"name": f"dsss_car_{tag}", "sample_rate_hz": cc.RATE_NARROW,
                           "center_freq_hz": CENTER, "loop_s": 0.25,
                           "signals": [_sig(car, f"C{i} {tag.upper()}", **extra)]}})
    for spec in g.dsss_preset_specs():
        pid = {"dsss_4user_bpsk": "MOD-DSSS-4", "dsss_mixed_carriers": "MOD-DSSS-M",
               "dsss_16user": "MOD-DSSS-16"}[spec["name"]]
        C.append({"id": f"{pid}/{spec['name']}", "plan": pid, "rate": cc.RATE_NARROW,
                  "spec": dict(spec, center_freq_hz=CENTER),
                  "need": 13 if pid == "MOD-DSSS-16" else None,
                  "record_only": pid == "MOD-DSSS-16"})
    return C


def _messages(spec):
    from dpmg.scenario import gen_scenario_json as g
    ex = g.expand_users(spec)
    return {str(s.get("user") or s.get("name")): str(s.get("message", "")) for s in ex["signals"]}


# offline replay
def replay_case(case, snr_db, cfo_hz, rng, work_dir, reps=2):
    import test_dsss_multiuser as T
    res = {"id": case["id"], "plan": case["plan"], "decoded": 0, "want": 0, "status": "",
           "record_only": bool(case.get("record_only")), "users": {}}
    try:
        iq, plan, raw = T.build(case["spec"], work_dir)
        rows, fs_out = T.decode_users(iq, plan, rng, reps=reps, cfo_hz=cfo_hz, snr_db=snr_db)
    except Exception as e:                                           # noqa: BLE001
        res["status"] = f"build_fail: {e}"
        print(f"  {case['id']:<30} BUILD FAIL {e}", flush=True)
        return res
    want = len(rows)
    ok = sum(1 for r in rows if r["ok"] == reps)
    for r in rows:
        sin = [v for v in r["sinr_db"] if v is not None]
        res["users"][r["user"]] = {"ok": r["ok"], "reps": reps, "bit_err": r["bit_err"],
                                   "sinr_db": (round(float(np.median(sin)), 1) if sin else None)}
    rep = raw.get("dsss_overlay") or {}
    res.update(decoded=ok, want=want, fs_out=fs_out,
               predicted_mui_snr_db=rep.get("expected_mui_snr_db"),
               status="decoded" if ok == want else f"{ok}/{want} users")
    need = case.get("need") or want
    mark = "ok  " if ok >= need else "MISS"
    print(f"  {case['id']:<30} {mark} {ok}/{want} users every rep"
          + (f"  (predicted MUI SNR >= {rep['expected_mui_snr_db']:.1f} dB)" if rep.get("expected_mui_snr_db") else ""),
          flush=True)
    return res


# hardware
def hw_case(link, case, timeout, settle, screen):
    from dpmg.link.proto import CmdStatus
    res = {"id": case["id"], "plan": case["plan"], "decoded": 0, "want": 0, "status": "",
           "record_only": bool(case.get("record_only")), "users": {}}
    if screen is not None and screen.get(case["id"]) == 0:
        res["status"] = "skipped_replay(0 offline)"
        print(f"  {case['id']:<30} SKIP (replay 0)", flush=True)
        return res
    spec = case["spec"]
    msgs = _messages(spec)
    packed = json.dumps({"n": "gen_scenario", "p": {"spec": spec, "load_tx": True}}, separators=(",", ":"))
    if len(packed) > 1150:
        path = cc.push_file(link, f"{spec['name']}.json", json.dumps(spec).encode())
        gp = {"spec_path": path, "load_tx": True}
    else:
        gp = {"spec": spec, "load_tx": True}
    g = cc.safe_cmd(link, "gen_scenario", gp, timeout=120)
    if g.status != CmdStatus.OK:
        res["status"] = f"build_fail: {cc.ack_err(g)}"
        print(f"  {case['id']:<30} BUILD FAIL {cc.ack_err(g)}", flush=True)
        return res
    sp = cc.safe_cmd(link, "set_plan", {"plan_path": g.result["plan_path"]}, timeout=30)
    if sp.status != CmdStatus.OK:
        res["status"] = f"plan_fail: {cc.ack_err(sp)}"
        return res
    time.sleep(settle)
    cc.safe_cmd(link, "start_tx", {"mode": "CONTINUOUS"}, timeout=30)
    res["want"] = len(msgs)
    got = {}
    deadline = time.time() + timeout
    while time.time() < deadline and len(got) < len(msgs):
        acc = link.collect_decodes(seconds=min(4.0, max(0.5, deadline - time.time())))
        for band, (m, dn) in acc.items():
            if band in msgs and m == msgs[band]:
                got[band] = dn
    cc.safe_cmd(link, "stop_tx", {}, timeout=30)
    res["users"] = {u: {"ok": int(u in got), "decodes": int(got.get(u, 0))} for u in msgs}
    need = case.get("need") or len(msgs)
    res.update(decoded=len(got), status="decoded" if len(got) == len(msgs) else
               f"{len(got)}/{len(msgs)} ({sorted(set(msgs) - set(got))})")
    print(f"  {case['id']:<30} {'ok  ' if len(got) >= need else 'MISS'} {len(got)}/{len(msgs)} users",
          flush=True)
    return res


def summarize(results, cases):
    need = {c["id"]: c.get("need") for c in cases}
    by_plan = {}
    for r in results:
        by_plan.setdefault(r["plan"], []).append(r)
    out = {}
    for pid, rs in by_plan.items():
        scored = [r for r in rs if not str(r.get("status", "")).startswith("skipped")]
        ok = sum(1 for r in scored if r["want"] and r["decoded"] >= (need.get(r["id"]) or r["want"]))
        out[pid] = {"plan": pid, "n": len(scored), "ok": ok, "pass": bool(scored) and ok == len(scored),
                    "record_only": all(r.get("record_only") for r in rs) if rs else False,
                    "detail": [{"id": r["id"], "d": r["decoded"], "w": r["want"], "status": r["status"]}
                               for r in rs]}
    return out


def markdown(meta, results, cases_sum):
    replay = meta.get("mode") == "replay"
    lines = [f"# DSSS carriers + multi-user overlays -- {'OFFLINE REPLAY' if replay else meta.get('serial', '')}",
             "", f"{meta['date']}  " + (f"SNR {meta.get('snr_db')} dB, CFO {meta.get('cfo_hz')} Hz"
                                          if replay else f"{meta.get('loopback', '')}  pad {meta.get('pad_db')} dB"), ""]
    for pid in ("MOD-DSSS-C", "MOD-DSSS-4", "MOD-DSSS-M", "MOD-DSSS-16"):
        c = cases_sum.get(pid)
        if not c:
            continue
        verdict = "RECORDED" if c["record_only"] else ("PASS" if c["pass"] else "FAIL")
        lines.append(f"- **{pid}**: {c['ok']}/{c['n']} {verdict} -- "
                     + ", ".join(f"{d['id'].split('/')[-1]} {d['d']}/{d['w']}"
                                 + ("" if d["w"] and d["d"] == d["w"] else f" [{d['status']}]")
                                 for d in c["detail"]))
    lines += ["", "Per-user detail (bit errors / despread SINR offline, decode counts on hardware) is in the JSON. "
              "MOD-DSSS-16 is recorded: an equal-power 16-user overlay at 127 chips leaves ~9 dB of "
              "multi-user interference per user, so a frame or two per window may miss; the bar is "
              ">= 13 of 16 per window, all 16 over the run (docs/DSSS_BUILDER.md)."]
    return "\n".join(lines)


def _load_replay(path):
    try:
        j = json.load(open(path))
        return {r["id"]: r["decoded"] for r in j.get("results", [])}
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=os.environ.get("DPMG_G40_HOST"), required=not os.environ.get("DPMG_G40_HOST"))
    ap.add_argument("--port", type=int, default=47810)
    ap.add_argument("--arm", action="store_true")
    ap.add_argument("--replay", action="store_true", help="offline, no radio")
    ap.add_argument("--replay-snr", type=float, default=25.0, dest="replay_snr")
    ap.add_argument("--replay-cfo", type=float, default=250.0, dest="replay_cfo")
    ap.add_argument("--replay-json", default=None, dest="replay_json")
    ap.add_argument("--seed", type=int, default=0xD55)
    ap.add_argument("--only", default="", help="comma substring filter on case ids")
    ap.add_argument("--gain", type=int, default=255)
    ap.add_argument("--atten", type=float, default=30)
    ap.add_argument("--pad-db", type=float, default=20, dest="pad_db")
    ap.add_argument("--tx-card", type=int, default=1, dest="tx_card")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--settle", type=float, default=2.0)
    ap.add_argument("--no-init", action="store_true")
    ap.add_argument("--no-cleanup", action="store_true")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--out", default=os.path.join(RESULTS_DIR, "DSSS_MULTIUSER_RESULTS.json"))
    cc.add_clock_args(ap)
    args = ap.parse_args()

    C = build_cases()
    if args.only:
        subs = [s.strip() for s in args.only.split(",") if s.strip()]
        C = [c for c in C if any(s in c["id"] for s in subs)]
    prior = []
    if args.resume and os.path.exists(args.out):
        try:
            prior = json.load(open(args.out)).get("results", [])
        except Exception:
            prior = []
    done_ids = {r["id"] for r in prior}
    results = [r for r in prior if r["id"] in {c["id"] for c in C}]
    t0 = time.time()
    if args.replay:
        rng = np.random.default_rng(args.seed)
        with tempfile.TemporaryDirectory() as d:
            for c in C:
                if args.resume and c["id"] in done_ids:
                    continue
                results.append(replay_case(c, args.replay_snr, args.replay_cfo, rng, d))
        meta = {"test": "dsss_multiuser", "mode": "replay", "date": cc.iso_now(), "snr_db": args.replay_snr,
                "cfo_hz": args.replay_cfo, "seed": args.seed, "elapsed_s": round(time.time() - t0, 1)}
    else:
        if not args.arm:
            print("*** --arm not given: refusing to radiate. ***", flush=True)
            return 2
        from fsk_loopback_test import Link, init_loopback
        screen = _load_replay(args.replay_json) if args.replay_json else None
        link = Link(args.host, args.port)
        args.clock = cc.resolve_clock(args.clock, link.serial)
        if not args.no_init:
            init_loopback(link, int(cc.RATE_NARROW), int(CENTER), args.atten, args.gain,
                          tx_card=args.tx_card, kind="g40", clock=args.clock, lock_timeout=args.lock_timeout)
            time.sleep(1.5)
        else:
            cc.gpsdo_gate(link, args.clock, args.lock_timeout, label="--no-init")
        try:
            for c in C:
                if args.resume and c["id"] in done_ids:
                    continue
                results.append(hw_case(link, c, args.timeout, args.settle, screen))
        finally:
            if not args.no_cleanup:
                cc.teardown(link)
        meta = {"test": "dsss_multiuser", "mode": "hardware", "date": cc.iso_now(), "serial": link.serial,
                "loopback": "Setup A TRx1B -> pad -> TRx1A", "pad_db": args.pad_db, "clock": args.clock,
                "elapsed_s": round(time.time() - t0, 1)}
    cases_sum = summarize(results, C)
    md = markdown(meta, results, cases_sum)
    cc.write_results(args.out, {"meta": meta, "results": results, "cases": cases_sum}, md)
    print("\n" + md, flush=True)
    bad = [p for p, c in cases_sum.items() if not c["pass"] and not c["record_only"]]
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
