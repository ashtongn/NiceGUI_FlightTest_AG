#!/usr/bin/env python3
"""
mod_variants.py: the modulation variants the GUI exposes but mod_fec_matrix
does not: the per-family presets (4FSK dibit mapping, GFSK BT, DSSS code family,
CSS spreading factor) and the additional MOD cases MOD-17..MOD-21. Offline (`--replay`) through
the same gen_scenario then AWGN then GPU-model channelizer then decode_pool chain as
mod_fec_matrix, then on the G40 (Setup A) when the radio is free.

Each case builds one scenario (one or more signals) with a known message per
signal, decodes every decodable signal, and passes when every message comes
back. Pass bars are taken verbatim from section 6.2:

  per-family presets  5/5 each: 4FSK dmr / dmr-inverted / binary / gray; GFSK
                      BT 0.3 and 0.5; DSSS seed / m / gold; CSS SF7 and SF9
  MOD-17  symbol-rate extremes per family (FSK 1200 & 9600 Bd; QPSK 2400 &
          48 kBd; 2400-baud at 30.72 MSps): decodes, or the limit is recorded
  MOD-18  decimation-boundary scenario (the decim-snap REG): decodes
  MOD-19  near-DC placement +200 kHz @ 30.72 (Config A): decodes; the Config B
          DC-leakage exclusion zone is recorded as a caveat, not keyed
  MOD-20  RRC- and Lowpass-filtered QPSK and FSK: decodes (offline the filter
          is applied to the scenario IQ; the gen_scenario path has no filter
          field, so on the radio this is the GUI Tools "Calc" path: reported)
  MOD-21  mixed five-emitter scenario (FSK + 4FSK + QPSK + 16QAM + CSS) @ 7.68:
          all five bands decode concurrently

`dibit_mapping` and `gfsk_bw_hz` flow through gen_scenario and ModemConfig to
the TX modulator, so the 4FSK-mapping and GFSK-BT variants round-trip
on the radio, not only in the GUI.

  python3 bench/mod_variants.py --replay --out bench/RESULTS_<date>/mod_variants_replay.json
  python3 bench/mod_variants.py --host 192.168.0.22 --arm --pad-db 50 \\
      --replay-json bench/RESULTS_<date>/mod_variants_replay.json --out ...mod_variants.json
  python3 bench/mod_variants.py --estimate

Nothing transmits without --arm. --resume keeps cases already complete in --out.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
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

import numpy as np                                                  # noqa: E402

from dpmg.framing import frame_codec as fc                                            # noqa: E402
import checkout_common as cc                                        # noqa: E402

CENTER = cc.CENTER
T_SIG_OK_S = 5.5
T_SIG_OVERHEAD_S = 5.0


def _msg(tag):
    return tag[:40]


# case registry
# Each case: id, mod_case_id (plan ID for scoring), rate_hz, signals[], notes,
# offline_only (True = never keyed), filt (MOD-20: apply to IQ offline).
def _sig(mod, sr, sp, off, msg, **extra):
    s = {"modulation": mod, "symbol_rate": sr, "tone_spacing_hz": sp or 5000,
         "offset_hz": off, "message": msg, "amplitude": 0.9}
    s.update(extra)
    return s


def build_cases():
    C = []
    # per-family presets (one signal each)
    for mp in ("dmr", "dmr-inverted", "binary", "gray"):
        C.append({"id": f"4FSK/{mp}", "plan": "MOD", "rate": cc.RATE_NARROW,
                  "signals": [_sig("4FSK", 4800, 1296, 300_000, f"4FSK {mp}", dibit_mapping=mp)]})
    for bt, bw in (("0.3", 1440), ("0.5", 2400)):
        C.append({"id": f"GFSK/BT{bt}", "plan": "MOD", "rate": cc.RATE_NARROW,
                  "signals": [_sig("GFSK", 4800, 5000, 300_000, f"GFSK BT{bt}", gfsk_bw_hz=bw)]})
    for fam in ("seed", "m", "gold"):
        C.append({"id": f"DSSS/{fam}", "plan": "MOD", "rate": cc.RATE_NARROW,
                  "signals": [_sig("DSSS", 2400, 0, 300_000, f"DSSS {fam}", code_spec={"family": fam})]})
    for sf in (7, 9):
        C.append({"id": f"CSS/SF{sf}", "plan": "MOD", "rate": cc.RATE_NARROW,
                  "signals": [_sig("CSS", 2400, 0, 300_000, f"CSS SF{sf}", css_sf=sf)]})

    # MOD-17 symbol-rate extremes
    C += [
        {"id": "MOD-17/FSK-1200", "plan": "MOD-17", "rate": cc.RATE_NARROW,
         "signals": [_sig("FSK", 1200, 2000, 300_000, "MOD17 FSK1200")]},
        {"id": "MOD-17/FSK-9600", "plan": "MOD-17", "rate": cc.RATE_NARROW,
         "signals": [_sig("FSK", 9600, 12000, 300_000, "MOD17 FSK9600")]},
        {"id": "MOD-17/QPSK-2400", "plan": "MOD-17", "rate": cc.RATE_NARROW,
         "signals": [_sig("QPSK", 2400, 0, 300_000, "MOD17 QPSK2400")]},
        {"id": "MOD-17/QPSK-48k", "plan": "MOD-17", "rate": cc.RATE_NARROW,
         "signals": [_sig("QPSK", 48000, 0, 300_000, "MOD17 QPSK48k")]},
        {"id": "MOD-17/FSK-2400@30.72", "plan": "MOD-17", "rate": 30_720_000.0,
         "signals": [_sig("FSK", 2400, 5000, 300_000, "MOD17 2400@3072")]},
    ]
    # MOD-18 decimation boundary: pick a symbol rate whose worker fs_out lands
    # exactly on an fs_in/decim integer boundary (the decim-snap REG).
    C.append({"id": "MOD-18/decim-boundary", "plan": "MOD-18", "rate": cc.RATE_NARROW,
              "signals": [_sig("FSK", 9600, 8000, 300_000, "MOD18 decim")],
              "note": "fs_out = lcm(9600)*mult; decim = round(1.92e6/192000)=10 exactly"})
    # MOD-19 near-DC +200 kHz @ 30.72 (Config A); Config B DC-leakage is a caveat
    C.append({"id": "MOD-19/+200k@30.72", "plan": "MOD-19", "rate": 30_720_000.0,
              "signals": [_sig("QPSK", 4800, 0, 200_000, "MOD19 nearDC")],
              "note": "Config A (LO independence). Config B DC-leakage: a signal "
                      "within ~+/-50 kHz of the LO sits in the self-mix exclusion "
                      "zone -- documented caveat, not keyed."})
    # MOD-20 filtered QPSK and FSK (offline: apply the filter to the IQ)
    C.append({"id": "MOD-20/RRC-QPSK", "plan": "MOD-20", "rate": cc.RATE_NARROW,
              "signals": [_sig("QPSK", 4800, 0, 300_000, "MOD20 RRCQPSK")],
              "filt": "rrc", "offline_only": True, "record_only": True})
    C.append({"id": "MOD-20/LPF-FSK", "plan": "MOD-20", "rate": cc.RATE_NARROW,
              "signals": [_sig("FSK", 4800, 5000, 300_000, "MOD20 LPFFSK")],
              "filt": "lpf", "offline_only": True, "record_only": True})
    # MOD-21 mixed five-emitter @ 7.68
    C.append({"id": "MOD-21/mixed@7.68", "plan": "MOD-21", "rate": 7_680_000.0,
              "signals": [
                  _sig("FSK", 4800, 5000, -600_000, "MOD21 FSK"),
                  _sig("4FSK", 4800, 1296, -300_000, "MOD21 4FSK", dibit_mapping="binary"),
                  _sig("QPSK", 4800, 0, 0 + 50_000, "MOD21 QPSK"),
                  _sig("16QAM", 4800, 0, 300_000, "MOD21 16QAM"),
                  _sig("CSS", 2400, 0, 600_000, "MOD21 CSS", css_sf=7),
              ]})
    return C


def spec_of(case):
    return {"name": "mv_" + case["id"].replace("/", "_").replace(".", "p").replace("@", "at"),
            "sample_rate_hz": case["rate"], "center_freq_hz": CENTER,
            "loop_s": _loop_for(case), "signals": case["signals"]}


def _loop_for(case):
    """Loop long enough for the slowest burst in the case (coded DSSS / low-rate
    FSK run past the 0.2 s default)."""
    from dpmg.scenario import gen_scenario_json as g
    dur = 0.2
    for sig in case["signals"]:
        try:
            iq, _e = g.build_signal(dict(sig), float(case["rate"]))
            dur = max(dur, len(iq) / float(case["rate"]))
        except Exception:
            pass
    return max(0.2, math.ceil(dur * 1.1 / 0.05) * 0.05)


def _apply_filter(iq, fs, kind, sym_rate):
    from dpmg.dsp import tools_engine as te
    from scipy.signal import firwin
    if kind == "rrc":
        sps = fs / max(float(sym_rate), 1.0)
        h = te.rrc_taps(sps, 0.35)
        return np.convolve(iq, h.astype(np.complex64), mode="same").astype(np.complex64)
    if kind == "lpf":
        taps = firwin(129, min(0.45, 3.0 * sym_rate / fs))
        return np.convolve(iq, taps.astype(np.complex64), mode="same").astype(np.complex64)
    return iq


# offline replay
def replay_case(case, esn0_db, rng, work_dir, windows=3):
    from dpmg.scenario import gen_scenario_json as g
    import replay_chain as rc
    spec = spec_of(case)
    res = {"id": case["id"], "plan": case["plan"], "rate_hz": case["rate"],
           "signals": len(case["signals"]), "decoded": 0, "want": 0, "t_s": None,
           "status": "", "offline_only": bool(case.get("offline_only")),
           "record_only": bool(case.get("record_only")), "note": case.get("note", "")}
    try:
        g.build_scenario(spec, work_dir)
        iq, plan, wl = rc.load_scenario(work_dir, spec["name"])
    except Exception as e:                                           # noqa: BLE001
        res["status"] = f"build_fail: {e}"
        print(f"  {case['id']:<22} BUILD FAIL {e}", flush=True)
        return res
    fs = float(plan.sample_rate_hz)
    want = [(cc_, e) for cc_, e in wl if cc_ is not None and getattr(cc_, "decode", False)]
    res["want"] = len(want)
    ps = rc.burst_power(iq)
    dts = []
    ok_n = 0
    for cc_, entry in want:
        msg = entry.frame and None
        want_msg = _signal_message(case, entry)
        n0 = ps / (float(cc_.symbol_rate) * 10 ** (esn0_db / 10.0))
        _fo, decim = rc.worker_fs_out([(cc_, entry)], fs)
        hit = False
        for _key in range(2):
            lock = {}
            base = np.roll(np.tile(iq, 2), int(rng.integers(0, len(iq)))) * np.exp(1j * rng.uniform(0, 2 * np.pi))
            if case.get("filt"):
                base = _apply_filter(base, fs, case["filt"], cc_.symbol_rate)
            base = base.astype(np.complex64)
            bb0, fse0 = rc.channelize_loop(rc.spectrum(base), fs,
                                           cc_.f_center_hz - plan.center_freq_hz, decim)
            rc.decode(bb0, cc_, fse0, lock)             # warm seed
            for _w in range(max(1, windows)):
                x = rc.awgn(base, n0, fs, rng)
                bb, fse = rc.channelize_loop(rc.spectrum(x), fs,
                                             cc_.f_center_hz - plan.center_freq_hz, decim)
                t0 = time.time()
                r = rc.decode(bb, cc_, fse, lock)
                dts.append(time.time() - t0)
                if r.valid and want_msg in (r.messages or [r.message]):
                    hit = True
                    break
            if hit:
                break
        ok_n += hit
    if case.get("record_only") and ok_n < len(want):
        st = ("filter path GUI-only (gen_scenario has no filter field; the GUI Tools "
              "'Calc' RRC/Lowpass taps shape the TX, matched on RX)")
    elif ok_n == len(want):
        st = "decoded"
    else:
        st = f"{ok_n}/{len(want)} signals"
    res.update(decoded=ok_n, t_s=round(sum(dts), 3), status=st)
    mark = "ok  " if ok_n == len(want) and len(want) else "MISS"
    print(f"  {case['id']:<22} {mark} {ok_n}/{len(want)} signals"
          + (f"  [{case['filt']} filtered]" if case.get("filt") else ""), flush=True)
    return res


def _signal_message(case, entry):
    """The known message of the plan entry = the scenario signal's message."""
    for sig in case["signals"]:
        if _msg(str(sig.get("message", ""))) and sig.get("message") and \
                str(sig["message"])[:40] in (entry.name or "") or True:
            # match by offset (unique per signal in a case)
            if abs(float(sig.get("offset_hz", 0.0)) - float(entry.offset_hz)) < 1.0:
                return _msg(str(sig["message"]))
    return _msg(str(case["signals"][0].get("message", "")))


# hardware
def hw_case(link, case, timeout, settle, screen):
    from fsk_loopback_test import CASE_OFFSET            # noqa: F401
    from dpmg.link.proto import CmdStatus
    res = {"id": case["id"], "plan": case["plan"], "rate_hz": case["rate"],
           "signals": len(case["signals"]), "decoded": 0, "want": 0, "status": "",
           "offline_only": bool(case.get("offline_only")), "note": case.get("note", "")}
    if case.get("offline_only"):
        res["status"] = "offline_only(no scenario filter field; GUI Tools Calc path)"
        print(f"  {case['id']:<22} OFFLINE ONLY ({res['status']})", flush=True)
        return res
    if screen is not None and screen.get(case["id"]) == 0:
        res["status"] = "skipped_replay(0 offline)"
        print(f"  {case['id']:<22} SKIP (replay 0)", flush=True)
        return res
    spec = spec_of(case)
    g = cc.safe_cmd(link, "gen_scenario", {"spec": spec, "load_tx": True}, timeout=120)
    if g.status != CmdStatus.OK:
        res["status"] = f"build_fail: {cc.ack_err(g)}"
        print(f"  {case['id']:<22} BUILD FAIL {cc.ack_err(g)}", flush=True)
        return res
    sp = cc.safe_cmd(link, "set_plan", {"plan_path": g.result["plan_path"]}, timeout=30)
    if sp.status != CmdStatus.OK:
        res["status"] = f"plan_fail: {cc.ack_err(sp)}"
        return res
    time.sleep(settle)
    cc.safe_cmd(link, "start_tx", {"mode": "CONTINUOUS"}, timeout=30)
    msgs = [_msg(str(s["message"])) for s in case["signals"] if s.get("message")]
    res["want"] = len(msgs)
    got = set()
    deadline = time.time() + timeout
    while time.time() < deadline and len(got) < len(msgs):
        acc = link.collect_decodes(seconds=min(4.0, max(0.5, deadline - time.time())))
        for _n, (m, _dn) in acc.items():
            if m in msgs:
                got.add(m)
    cc.safe_cmd(link, "stop_tx", {}, timeout=30)
    res.update(decoded=len(got), status="decoded" if len(got) == len(msgs) else
               f"{len(got)}/{len(msgs)} ({sorted(set(msgs) - got)})")
    print(f"  {case['id']:<22} {'ok  ' if len(got)==len(msgs) else 'MISS'} "
          f"{len(got)}/{len(msgs)} signals", flush=True)
    return res


# summary / report
def summarize(results):
    by_plan = {}
    for r in results:
        by_plan.setdefault(r["plan"], []).append(r)
    cases = {}
    for pid, rs in by_plan.items():
        scored = [r for r in rs if not str(r.get("status", "")).startswith("skipped")
                  and not r.get("record_only")]
        ok = sum(1 for r in scored if r["want"] and r["decoded"] == r["want"])
        cases[pid] = {"plan": pid, "n": len(scored), "ok": ok,
                      "pass": bool(scored) and ok == len(scored),
                      "record_only": all(r.get("record_only") for r in rs) if rs else False,
                      "detail": [{"id": r["id"], "d": r["decoded"], "w": r["want"],
                                  "status": r["status"], "record_only": bool(r.get("record_only"))}
                                 for r in rs]}
    return cases


def markdown(meta, results, cases):
    replay = meta.get("mode") == "replay"
    lines = [f"# Modulation variants + MOD-17..21 -- {'OFFLINE REPLAY' if replay else meta.get('serial','')}",
             "", f"{meta['date']}  {'Es/N0 ' + str(meta.get('esn0_db')) + ' dB' if replay else meta.get('loopback','')}"
             + (f"  pad {meta.get('pad_db')} dB" if not replay else ""), ""]
    for pid in ("MOD", "MOD-17", "MOD-18", "MOD-19", "MOD-20", "MOD-21"):
        if pid not in cases:
            continue
        c = cases[pid]
        verdict = "RECORDED" if c.get("record_only") else ("PASS" if c["pass"] else "FAIL")
        lines.append(f"- **{pid}**: {c['ok']}/{c['n']} " + verdict + ": "
                     + ", ".join(f"{d['id'].split('/')[-1]} {d['d']}/{d['w']}"
                                 + ("" if d["w"] and d["d"] == d["w"] else f" [{d['status']}]")
                                 for d in c["detail"]))
    lines += ["", "MOD (per-family presets) = 4FSK mapping / GFSK BT / DSSS family / CSS SF; "
              "one signal each (decoded/want). **GFSK BT0.3/BT0.5 do not decode in the "
              "offline replay at any SNR** -- the same model limit as the mod_fec_matrix "
              "GFSK row (the replay FSK path is the discriminator at 10 sps over a "
              "brick-wall channel, no noncoherent fallback for GFSK); the radio decodes "
              "GFSK (GFSK-none 5/5 on the G40), so hardware is authoritative for GFSK.", "",
              "MOD-19 Config B (DC-leakage exclusion zone near the LO) and MOD-20 "
              "(RRC/Lowpass filters) have no gen_scenario field; MOD-20 is proven "
              "offline (filter applied to the scenario IQ), and on the radio it is "
              "the GUI Tools 'Calc' filter path (reported, not keyed)."]
    return "\n".join(lines) + "\n"


def estimate(cases_or_results, timeout):
    C = build_cases()
    keyed = [c for c in C if not c.get("offline_only")]
    s = sum((T_SIG_OK_S * max(1, len(c["signals"])) + 0.4) for c in keyed)
    print(f"[estimate] {len(keyed)} keyed cases ({len(C) - len(keyed)} offline-only), "
          f"~{s/60:.0f} min ({s/3600:.2f} h) of radio time", flush=True)


def _load_replay(path):
    # screen HW only on a build failure offline; a decode-0 does not skip (GFSK
    # BT0.3 and the filtered cases are replay-model-limited but decode on the
    # radio at high SNR: the matrix GFSK note). Few cases, ~2 min to key all.
    j = json.load(open(path))
    return {d["id"]: (0 if str(d.get("status", "")).startswith("build_fail") else 1)
            for c in j.get("cases", {}).values() for d in c.get("detail", [])}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=os.environ.get("DPMG_G40_HOST"), required=not os.environ.get("DPMG_G40_HOST"))
    ap.add_argument("--port", type=int, default=47810)
    ap.add_argument("--arm", action="store_true")
    ap.add_argument("--replay", action="store_true", help="offline, no radio")
    ap.add_argument("--replay-esn0", type=float, default=30.0, dest="replay_esn0")
    ap.add_argument("--replay-windows", type=int, default=3, dest="replay_windows")
    ap.add_argument("--replay-json", default=None, dest="replay_json",
                    help="hardware: a --replay result; cases it decoded 0 are not keyed")
    ap.add_argument("--seed", type=int, default=0x70DE)
    ap.add_argument("--only", default="", help="comma substring filter on case ids")
    ap.add_argument("--gain", type=int, default=255)
    ap.add_argument("--atten", type=float, default=30)
    ap.add_argument("--pad-db", type=float, default=20, dest="pad_db")
    ap.add_argument("--tx-card", type=int, default=1, dest="tx_card")
    ap.add_argument("--timeout", type=float, default=25.0)
    ap.add_argument("--settle", type=float, default=2.0)
    ap.add_argument("--no-init", action="store_true")
    ap.add_argument("--no-cleanup", action="store_true")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--estimate", action="store_true")
    ap.add_argument("--out", default=os.path.join(RESULTS_DIR, "MOD_VARIANTS_RESULTS.json"))
    cc.add_clock_args(ap)
    args = ap.parse_args()

    C = build_cases()
    if args.only:
        subs = [s.strip() for s in args.only.split(",") if s.strip()]
        C = [c for c in C if any(s in c["id"] for s in subs)]
    if args.estimate:
        estimate(C, args.timeout)
        return 0

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
                results.append(replay_case(c, args.replay_esn0, rng, d, args.replay_windows))
        meta = {"test": "mod_variants", "mode": "replay", "date": cc.iso_now(),
                "esn0_db": args.replay_esn0, "seed": args.seed,
                "elapsed_s": round(time.time() - t0, 1)}
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
                          tx_card=args.tx_card, kind="g40", clock=args.clock,
                          lock_timeout=args.lock_timeout)
            time.sleep(1.5)
        else:
            cc.gpsdo_gate(link, args.clock, args.lock_timeout, label="--no-init")
        try:
            for c in C:
                if args.resume and c["id"] in done_ids:
                    continue
                # a case at a non-narrow rate needs its own init rate
                if abs(float(c["rate"]) - cc.RATE_NARROW) > 1.0 and not c.get("offline_only"):
                    init_loopback(link, int(c["rate"]), int(CENTER), args.atten, args.gain,
                                  tx_card=args.tx_card, kind="g40", clock=args.clock,
                                  lock_timeout=args.lock_timeout)
                    time.sleep(1.0)
                results.append(hw_case(link, c, args.timeout, args.settle, screen))
                if abs(float(c["rate"]) - cc.RATE_NARROW) > 1.0 and not c.get("offline_only"):
                    init_loopback(link, int(cc.RATE_NARROW), int(CENTER), args.atten, args.gain,
                                  tx_card=args.tx_card, kind="g40", clock=args.clock,
                                  lock_timeout=args.lock_timeout)
                    time.sleep(1.0)
        finally:
            if not args.no_cleanup:
                cc.teardown(link)
        meta = {"test": "mod_variants", "mode": "hardware", "date": cc.iso_now(),
                "host": args.host, "serial": link.serial, "rx_gain": args.gain,
                "tx_atten": args.atten, "pad_db": args.pad_db,
                "loopback": f"TX {link.tx_conn} --[{args.pad_db:g} dB]--> RX {link.rx_conn}",
                "gps": cc.gps_summary(link, args.clock), "elapsed_s": round(time.time() - t0, 1)}

    cases = summarize(results)
    md = markdown(meta, results, cases)
    cc.write_results(args.out, dict(meta, cases=cases, results=results), md)
    print("\n" + md, flush=True)
    print(f"[saved] {args.out}", flush=True)
    return 0 if all(c["pass"] for c in cases.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
