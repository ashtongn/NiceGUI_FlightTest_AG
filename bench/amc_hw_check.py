#!/usr/bin/env python3
"""
amc_hw_check.py: blind AMC over the cable: key every DPMG modulation on Setup A
and classify it with the service's classify_selection (amc.classify with carrier
recovery), N captures per modulation. Reports per-modulation accuracy, the
confusion matrix and the carrier read-outs (CFO, recovery order, fitted Es/N0).

Setup A: TX TRx1B (card 1) through a 50 dB pad into RX TRx1A (card 0), cross-card,
so the TX and RX LOs are not coherent. On this link a classifier without carrier
recovery reads BPSK as QPSK. The reference clock is gpsdo with the 3.3 V antenna bias on and lock
confirmed before anything keys (checkout_common.gpsdo_gate via init_loopback).

  # offline dry run (no radio): same scenario specs (random raw bits, no frame),
  # built locally at 1.92 MSps,
  # AWGN at --replay-esn0, the classify_selection front end, amc.classify
  python3 bench/amc_hw_check.py --replay --reps 3 --out /tmp/amc_replay.json

  # hardware (radio must be free; one init, one close)
  python3 bench/amc_hw_check.py --arm --clock gpsdo --pad-db 50 --reps 5 \\
      --out output/bench/amc_hw.json

Pass bar (AMC-01..03): every modulation
classified, overall exact >= 80 %, BPSK exact >= 4 of 5 (needs the carrier
recovery), family correct >= 90 %. DQPSK counts as QPSK (indistinguishable with
random data, amc.EQUIVALENT).
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
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import numpy as np                                                  # noqa: E402

from dpmg.dsp import amc                                                          # noqa: E402
from dpmg.link.proto import CmdStatus                                         # noqa: E402
import checkout_common as cc                                        # noqa: E402

MODS = ["FSK", "GFSK", "4FSK", "BPSK", "QPSK", "pi/4-QPSK", "DQPSK", "OQPSK",
        "8PSK", "8QAM", "16QAM", "16APSK", "32APSK", "DSSS", "QPSK-DSSS", "CSS"]
DEFAULT_OUT = os.path.join(RESULTS_DIR, "AMC_HW_RESULTS.json")


def _slug(s):
    return "".join(ch if ch.isalnum() else "_" for ch in s.lower()).strip("_")


def span_hz(spec_sig, fs):
    """Brush half-width around the signal: the generated burst's -20 dB
    occupied width x1.3 (what an operator would select), at least 10 kHz."""
    from dpmg.scenario import gen_scenario_json as g
    iq, _e = g.build_signal(spec_sig, float(fs))
    x = np.asarray(iq, dtype=np.complex64)
    off = float(spec_sig.get("offset_hz", 0.0))
    n = min(1 << 16, x.size)
    X = np.abs(np.fft.fftshift(np.fft.fft(x[:n] * np.hanning(n)))) ** 2
    f = np.fft.fftshift(np.fft.fftfreq(n, 1.0 / fs)) - off
    band = f[X > X.max() * 1e-2]
    bw = float(band.max() - band.min()) if band.size else 20e3
    return max(0.65 * bw, 10e3)


# Random on-air bits (gen_scenario_json RAW_BITS: no framing, nothing to
# decode) so every constellation point is used equally: a text payload makes
# the symbol stream non-uniform (ASCII high bits are fixed: a 16APSK frame of
# text uses only a few of its points, and a periodic 8-bit structure looks like
# a spreading code), which is a payload property, not the modulation's.
import random as _random
_RNG = _random.Random(0xA3C)
_BITS = "".join(_RNG.choice("01") for _ in range(6000))


def cell(mod, center):
    import mod_fec_matrix as mfm
    message = f"AMC {mod}"
    spec = mfm.cell_spec(mod, "none", message, center, name=f"amc_{_slug(mod)}")
    sig = spec["signals"][0]
    sig["bits"] = _BITS
    # loop = the burst + 5 %, so a 0.2 s capture is mostly signal
    from dpmg.scenario import gen_scenario_json as g
    try:
        iq, _e = g.build_signal(sig, float(cc.RATE_NARROW))
        spec["loop_s"] = round(1.05 * len(iq) / float(cc.RATE_NARROW), 4)
    except Exception:                                                # noqa: BLE001
        pass
    return spec, message


def frontend(x, fs, f_c, half):
    """dpmg_service._cmd_classify_selection's front end (selection to DC,
    decimate to ~2.5x its width), for the offline replay."""
    import scipy.signal as ss
    n = np.arange(x.size)
    y = x * np.exp(-2j * np.pi * f_c / fs * n)
    cur, target = float(fs), max(2 * half * 2.5, 1.0)
    while cur / target >= 2.0:
        q = int(min(8, cur // target))
        if q < 2:
            break
        y = ss.decimate(y, q, ftype="fir", zero_phase=True)
        cur /= q
    return y, cur


def replay_mod(mod, reps, esn0_db, cfo_hz, rng, wd, rate, center):
    from dpmg.scenario import gen_scenario_json as g
    import replay_chain as rc
    spec, _msg = cell(mod, center)
    g.build_scenario(dict(spec, sample_rate_hz=float(rate)), wd)
    iq, plan, wl = rc.load_scenario(wd, spec["name"])
    ch = wl[0][0]
    fs = float(plan.sample_rate_hz)
    sig = spec["signals"][0]
    half = span_hz(sig, fs)
    ps = rc.burst_power(iq)
    n0 = ps / (float(ch.symbol_rate) * 10 ** (esn0_db / 10.0))
    out = []
    for r in range(reps):
        cfo = rng.uniform(-cfo_hz, cfo_hz)
        n = np.arange(2 * iq.size)
        base = np.roll(np.tile(iq, 2), int(rng.integers(0, iq.size)))
        base = base * np.exp(1j * (2 * np.pi * cfo / fs * n + rng.uniform(0, 2 * np.pi)))
        x = rc.awgn(base.astype(np.complex64), n0, fs, rng)
        seg = x[: int(0.2 * fs)]
        y, fs2 = frontend(seg, fs, float(sig["offset_hz"]), half)
        res = amc.classify(y, fs2)
        out.append(_rec(mod, r, res, cfo_true=cfo))
    return out


def _rec(mod, rep, res, cfo_true=None):
    feats = res.get("features", {}) or {}
    pred = res.get("modulation")
    exp = amc.canonical(mod)
    return {"mod": mod, "rep": rep + 1, "pred": pred, "expected": exp,
            "exact": pred == exp,
            "family_ok": amc.FAMILY.get(pred) == amc.FAMILY.get(exp),
            "confidence": res.get("confidence"), "sym_rate_hz": res.get("sym_rate_hz"),
            "cfo_hz": feats.get("car_cfo_hz"), "order": feats.get("car_order"),
            "esn0_db": feats.get("esn0_db"), "cfo_true_hz": cfo_true}


def hw_mod(link, mod, reps, center, settle, seconds, rate):
    spec, message = cell(mod, center)
    sig = spec["signals"][0]
    f_sig = center + float(sig["offset_hz"])
    half = span_hz(sig, rate)
    out = []
    # the 6000 random raw bits make every cell ~6.2 kB, over the 1400 B CMD
    # datagram: push the spec into the service inbox and build from spec_path
    # (checkout_common.push_file, as DET-09 does); small specs stay inline
    packed = json.dumps({"n": "gen_scenario", "p": {"spec": spec, "load_tx": True}}, separators=(",", ":"))
    if len(packed) > 1200:
        rel = cc.push_file(link, f"{spec['name']}.json", json.dumps(spec).encode(), timeout=60.0)
        g = cc.safe_cmd(link, "gen_scenario", {"spec_path": rel, "load_tx": True}, timeout=120)
    else:
        g = cc.safe_cmd(link, "gen_scenario", {"spec": spec, "load_tx": True}, timeout=120)
    if g.status != CmdStatus.OK:
        print(f"  {mod:<10} BUILD FAIL {cc.ack_err(g)}", flush=True)
        return [{"mod": mod, "rep": 0, "pred": None, "expected": amc.canonical(mod),
                 "exact": False, "family_ok": False, "status": f"build_fail: {cc.ack_err(g)}"}]
    sp = cc.safe_cmd(link, "set_plan", {"plan_path": g.result["plan_path"]}, timeout=30)
    if sp.status != CmdStatus.OK:
        return [{"mod": mod, "rep": 0, "pred": None, "expected": amc.canonical(mod),
                 "exact": False, "family_ok": False, "status": f"plan_fail: {cc.ack_err(sp)}"}]
    time.sleep(settle)
    st = cc.safe_cmd(link, "start_tx", {"mode": "CONTINUOUS"}, timeout=30)
    if st.status != CmdStatus.OK:
        return [{"mod": mod, "rep": 0, "pred": None, "expected": amc.canonical(mod),
                 "exact": False, "family_ok": False, "status": f"tx_fail: {cc.ack_err(st)}"}]
    try:
        time.sleep(1.5)                       # TX ramp + one full loop on air
        for r in range(reps):
            c = cc.safe_cmd(link, "classify_selection",
                            {"f_lo": f_sig - half, "f_hi": f_sig + half, "seconds": seconds},
                            timeout=60)
            if c.status != CmdStatus.OK:
                rec = {"mod": mod, "rep": r + 1, "pred": None, "expected": amc.canonical(mod),
                       "exact": False, "family_ok": False, "status": f"classify_fail: {cc.ack_err(c)}"}
            else:
                rec = _rec(mod, r, c.result)
                rec["capture_rate_hz"] = c.result.get("capture_rate_hz")
                rec["sel_bw_hz"] = c.result.get("sel_bw_hz")
            out.append(rec)
            time.sleep(0.3)
    finally:
        cc.safe_cmd(link, "stop_tx", {}, timeout=30)
    return out


def summarize(runs, mods):
    labels = list(dict.fromkeys(amc.canonical(m) for m in mods))
    conf = {m: {} for m in mods}
    per = {}
    for r in runs:
        p = r.get("pred") or "none"
        conf[r["mod"]][p] = conf[r["mod"]].get(p, 0) + 1
    for m in mods:
        rs = [r for r in runs if r["mod"] == m and r.get("rep", 0) > 0 and "status" not in r]
        n = len(rs)
        cfos = [r["cfo_hz"] for r in rs if r.get("cfo_hz") is not None]
        per[m] = {"n": n, "exact": sum(r["exact"] for r in rs),
                  "family": sum(r["family_ok"] for r in rs),
                  "acc": (sum(r["exact"] for r in rs) / n) if n else None,
                  "cfo_hz_median": float(np.median(cfos)) if cfos else None,
                  "esn0_db_median": (float(np.median([r["esn0_db"] for r in rs if r.get("esn0_db") is not None]))
                                     if any(r.get("esn0_db") is not None for r in rs) else None)}
    tot = sum(p["n"] for p in per.values())
    ex = sum(p["exact"] for p in per.values())
    fam = sum(p["family"] for p in per.values())
    return {"labels": labels, "confusion": conf, "per_mod": per,
            "overall_acc": (ex / tot) if tot else None,
            "family_acc": (fam / tot) if tot else None, "n": tot}


def markdown(meta, s, mods):
    preds = []
    for m in mods:
        for p in s["confusion"][m]:
            if p not in preds:
                preds.append(p)
    cols = [p for p in s["labels"] if p in preds] + [p for p in preds if p not in s["labels"]]
    hdr = ["expected \\ predicted"] + cols + ["exact", "CFO med Hz", "Es/N0 med dB"]
    rows = []
    for m in mods:
        p = s["per_mod"][m]
        rows.append([m] + [str(s["confusion"][m].get(c, "")) or "." for c in cols]
                    + [f"{p['exact']}/{p['n']}", cc.fnum(p["cfo_hz_median"]),
                       cc.fnum(p["esn0_db_median"])])
    lines = [f"# AMC hardware check ({meta['mode']})", "",
             f"{meta['date']}  rate {meta['rate_hz'] / 1e6:g} MSps  "
             + (f"Es/N0 {meta['esn0_db']:g} dB, CFO +/-{meta['cfo_hz']:g} Hz (replay)"
                if meta["mode"] == "replay" else
                f"{meta.get('loopback', '')}  clock {meta.get('clock')}  atten {meta.get('tx_atten')} dB"),
             "",
             f"- overall exact: {cc.fnum(100 * (s['overall_acc'] or 0))} % of {s['n']}; "
             f"family: {cc.fnum(100 * (s['family_acc'] or 0))} %", "",
             cc.md_table(hdr, rows)]
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=os.environ.get("DPMG_G40_HOST"), required=not os.environ.get("DPMG_G40_HOST"))
    ap.add_argument("--port", type=int, default=47810)
    ap.add_argument("--arm", action="store_true", help="allow TX to radiate")
    ap.add_argument("--mods", default="all", help=f"comma list or 'all': {','.join(MODS)}")
    ap.add_argument("--reps", type=int, default=5, help="classifications per modulation")
    ap.add_argument("--seconds", type=float, default=0.2, help="capture per classification")
    ap.add_argument("--rate", type=float, default=cc.RATE_NARROW)
    ap.add_argument("--center", type=float, default=cc.CENTER)
    ap.add_argument("--gain", type=int, default=255)
    ap.add_argument("--atten", type=float, default=30)
    ap.add_argument("--pad-db", type=float, default=50, dest="pad_db")
    ap.add_argument("--tx-card", type=int, default=1, dest="tx_card")
    ap.add_argument("--settle", type=float, default=1.0)
    ap.add_argument("--no-init", action="store_true")
    ap.add_argument("--replay", action="store_true", help="offline, no radio")
    ap.add_argument("--replay-esn0", type=float, default=20.0, dest="replay_esn0")
    ap.add_argument("--replay-cfo", type=float, default=300.0, dest="replay_cfo",
                    help="replay CFO drawn uniform in +/- Hz (default 300)")
    ap.add_argument("--seed", type=int, default=0xA3C)
    ap.add_argument("--out", default=DEFAULT_OUT)
    cc.add_clock_args(ap)
    args = ap.parse_args()
    mods = MODS if args.mods == "all" else [m.strip() for m in args.mods.split(",") if m.strip()]
    bad = [m for m in mods if m not in MODS]
    if bad:
        raise SystemExit(f"unknown modulation {bad}")
    t0 = time.time()
    runs = []
    if args.replay:
        rng = np.random.default_rng(args.seed)
        with tempfile.TemporaryDirectory() as wd:
            for m in mods:
                rs = replay_mod(m, args.reps, args.replay_esn0, args.replay_cfo, rng, wd,
                                args.rate, int(args.center))
                runs += rs
                print(f"  {m:<10} " + " ".join(str(r["pred"]) for r in rs), flush=True)
        meta = {"test": "amc_hw_check", "mode": "replay", "date": cc.iso_now(),
                "rate_hz": float(args.rate), "esn0_db": args.replay_esn0,
                "cfo_hz": args.replay_cfo, "reps": args.reps, "seed": args.seed,
                "elapsed_s": round(time.time() - t0, 1)}
    else:
        if not args.arm:
            print("*** --arm not given: refusing to radiate (Setup A: TRx1B to 50 dB pad to TRx1A). ***",
                  flush=True)
            return 2
        from fsk_loopback_test import Link, init_loopback
        link = Link(args.host, args.port)
        args.clock = cc.resolve_clock(args.clock, link.serial)
        print(f"[link] {link.serial} @ {args.host}:{args.port}  clock {args.clock}", flush=True)
        if not args.no_init:
            init_loopback(link, int(args.rate), int(args.center), args.atten, args.gain,
                          tx_card=args.tx_card, kind="g40", clock=args.clock,
                          lock_timeout=args.lock_timeout)
            time.sleep(1.5)
        else:
            cc.gpsdo_gate(link, args.clock, args.lock_timeout, label="--no-init")
        try:
            for m in mods:
                rs = hw_mod(link, m, args.reps, int(args.center), args.settle, args.seconds, args.rate)
                runs += rs
                print(f"  {m:<10} " + " ".join(str(r.get("pred")) for r in rs), flush=True)
                time.sleep(0.5)
            meta = {"test": "amc_hw_check", "mode": "hardware", "date": cc.iso_now(),
                    "host": args.host, "serial": link.serial, "rate_hz": float(args.rate),
                    "center_hz": int(args.center), "rx_gain": args.gain, "tx_atten": args.atten,
                    "tx_card": args.tx_card, "pad_db": args.pad_db, "reps": args.reps,
                    "clock": args.clock,
                    "loopback": f"TX {link.tx_conn} --[{args.pad_db:g} dB]--> RX {link.rx_conn}",
                    "gps": cc.gps_summary(link, args.clock),
                    "elapsed_s": round(time.time() - t0, 1)}
        finally:
            cc.teardown(link)
    s = summarize(runs, mods)
    md = markdown(meta, s, mods)
    cc.write_results(args.out, dict(meta, mods=mods, summary=s, runs=runs), md)
    print("\n" + md, flush=True)
    print(f"[saved] {args.out}", flush=True)
    ok = (s["overall_acc"] or 0) >= 0.8 and (s["family_acc"] or 0) >= 0.9
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
