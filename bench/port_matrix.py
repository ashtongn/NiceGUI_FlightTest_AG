#!/usr/bin/env python3
"""
G40 RF port matrix (PORT-G-01..12, 17, 18;
measures M-22 port pairs and M-23 RX uniformity).

Walks the 12 ordered (RX port, TX port) pairs of TRx1A / TRx2A / TRx1B / TRx2B
in the plan's cable-move order. For each move it prints the cable to fit (one
20 dB pad), waits for Enter, inits through the service with the operator
console's port: card/handle mapping (same card = dual chan_mode FDD), keys a
CW tone at +1 MHz and then scenarios/json/fsk_sweep.json, and scores: tone
detected within 1 slot, at least 5 of 6 messages decoded, init time, over/
underruns, and a STOP RX / CLOSE / START re-init on the same ports (PORT-G-18).
Moves 4 and 10 (RX on a "2" jack with TX on the "1" jack of the same card,
PORT-G-06 / PORT-G-12) must be rejected by the service before init; an init
that is accepted there is a FAIL.

Nothing transmits without --arm (without it every pair is init + RX only).

  python3 bench/port_matrix.py --host 192.168.0.22 --arm
  python3 bench/port_matrix.py --host 192.168.0.22 --arm --pairs 1,2,3
  python3 bench/port_matrix.py --host 192.168.0.22 --arm --pairs TRx1A:TRx1B,TRx2A:TRx1A
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# results go outside the source tree (output/ is not tracked); DPMG_BENCH_RESULTS overrides
RESULTS_DIR = os.environ.get("DPMG_BENCH_RESULTS") or os.path.join(os.path.dirname(HERE), "output", "bench")
os.makedirs(RESULTS_DIR, exist_ok=True)
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from dpmg.link.proto import CmdStatus                 # noqa: E402
from fsk_loopback_test import Link          # noqa: E402
import checkout_common as cc                # noqa: E402

TONE_OFFSET_HZ = 310_000          # default --tone-offset: mid 20 kHz slot. +1 MHz (the plan's
                                  # wording) is outside the +-960 kHz span of the 1.92 MSps RX, and
                                  # +300 kHz sits on a slot edge, in the detector's guard-gap blind
                                  # strip (a same-card tone with no skirt is invisible)
SCENARIO = os.path.join(os.path.dirname(HERE), "scenarios", "json", "fsk_sweep.json")
DEFAULT_OUT = os.path.join(RESULTS_DIR, "PORT_MATRIX_RESULTS.json")
DECODE_BAR = 5                              # of 6 fsk_sweep messages


def select_moves(spec):
    """--pairs: move numbers (1,2,3), RX:TX names, or 'all'."""
    if not spec or spec == "all":
        return list(cc.PORT_MOVES)
    want = [x.strip() for x in spec.split(",") if x.strip()]
    out = []
    for w in want:
        if ":" in w:
            rx, tx = w.split(":", 1)
            m = [mv for mv in cc.PORT_MOVES if mv[1].lower() == rx.lower() and mv[2].lower() == tx.lower()]
        else:
            m = [mv for mv in cc.PORT_MOVES if str(mv[0]) == w]
        if not m:
            raise SystemExit(f"--pairs: unknown entry {w!r} (move number 1-12 or RX:TX like TRx1A:TRx1B)")
        out.append(m[0])
    return out


def cable_prompt(move, rx, tx, expect, no_prompt, pad_db=20):
    rel = "same card" if cc.PORT_MAP[rx]["card"] == cc.PORT_MAP[tx]["card"] else "cross-card"
    print(f"\n{'=' * 70}\n MOVE {move:>2}   RX {rx}  <--[{pad_db:g} dB pad]--  TX {tx}   ({rel}; "
          f"{'expect REFUSED before init' if expect == 'refused' else 'expect pass'})\n"
          f" cable: {tx} ---> {pad_db:g} dB pad ---> {rx}      no antenna on either jack\n{'=' * 70}",
          flush=True)
    if no_prompt:
        return True
    if not sys.stdin.isatty():
        print(" (no terminal: continuing without a prompt)", flush=True)
        return True
    ans = input(" fit the cable, then press Enter (s = skip this move, q = quit): ").strip().lower()
    if ans == "q":
        raise KeyboardInterrupt
    return ans != "s"


def run_move(link, move, rx, tx, case_id, expect, args, spec):
    rec = {"move": move, "rx_port": rx, "tx_port": tx, "case": case_id, "expect": expect,
           "relationship": "same card, dual FDD" if cc.PORT_MAP[rx]["card"] == cc.PORT_MAP[tx]["card"]
           else "cross-card", "init_status": None, "init_s": None, "init_err": None,
           "fps": None, "overruns": None, "underruns": None,
           "tone": None, "decode": None, "reinit": None, "verdict": None, "notes": [], "gps": None}
    n_gps = len(cc.gps_log(link))
    # on gpsdo an OK init returns only once the GPSDO is locked (GpsLockError otherwise)
    ack, dt = cc.init_ports(link, rx, tx, int(args.rate), int(args.center), args.gain, args.atten,
                            clock=args.clock, lock_timeout=args.lock_timeout)
    if len(cc.gps_log(link)) > n_gps:
        rec["gps"] = cc.gps_log(link)[-1]
    rec["init_status"] = CmdStatus(ack.status).name
    rec["init_s"] = dt
    if ack.status != CmdStatus.OK:
        rec["init_err"] = cc.ack_err(ack)
        rec["g40_same_card"] = bool((ack.result or {}).get("g40_same_card"))
    if expect == "refused":
        if ack.status == CmdStatus.REJECTED and rec.get("g40_same_card"):
            rec["verdict"] = "PASS"
            rec["notes"].append("refused before init with the G40_SAME_CARD rule, as expected")
        elif ack.status == CmdStatus.OK:
            rec["verdict"] = "FAIL"
            rec["notes"].append("init was ACCEPTED: a silent init that streams nothing is a FAIL")
            cc.teardown(link)
        else:
            rec["verdict"] = "FAIL"
            rec["notes"].append(f"refused, but not by the same-card rule: {rec['init_err']}")
        print(f"[move {move}] {rec['verdict']}: {rec['notes'][-1]}", flush=True)
        return rec
    if ack.status != CmdStatus.OK:
        rec["verdict"] = "FAIL"
        rec["notes"].append(f"init {rec['init_status']}: {rec['init_err']}")
        print(f"[move {move}] FAIL: {rec['notes'][-1]}", flush=True)
        return rec
    if ack.result.get("chan_mode"):
        rec["chan_mode"] = ack.result.get("chan_mode")
    time.sleep(1.0)
    st = cc.status_snapshot(link, 3.0)
    if st:
        rec.update(fps=st["fps"], overruns=st["overruns"], underruns=st["underruns"])
        print(f"[rx] fps={st['fps']} overruns={st['overruns']} underruns={st['underruns']}", flush=True)

    if not args.arm:
        rec["verdict"] = "INIT-OK (unarmed)"
        rec["notes"].append("not armed: RX only, no tone / decode scoring")
    else:
        # CW tone at +1 MHz: detected within one slot of the truth?
        tone_off = int(args.tone_offset)
        f_truth = int(args.center) + tone_off
        t_on, tel, g = cc.key_tone(link, tone_off, hold_s=args.tone_hold, after_s=2.0,
                                   ampl=0.5, name="pm_tone")
        d = tel.det_near(f_truth, within_hz=5 * cc.SLOT_BW_HZ)
        tone = {"keyed": g.status == CmdStatus.OK, "detected": d is not None,
                "within_slot": bool(d and abs(d["f"] - f_truth) <= cc.SLOT_BW_HZ),
                "freq_err_hz": round(d["f"] - f_truth, 1) if d else None,
                "snr_db": round(d["snr"], 1) if d else None,
                "n_detections": len(tel.detections), "offset_hz": tone_off}
        if g.status != CmdStatus.OK:
            tone["err"] = cc.ack_err(g)
        rec["tone"] = tone
        print(f"[tone] {'detected' if d else 'NOT detected'}"
              + (f" err {tone['freq_err_hz']} Hz SNR {tone['snr_db']} dB"
                 f" ({'within' if tone['within_slot'] else 'OUTSIDE'} 1 slot)" if d else ""),
              flush=True)
        # fsk_sweep.json: >= 5 of 6 messages decoded
        expect_msgs = {s["message"] for s in spec["signals"]}
        dec = {"keyed": False, "expected": sorted(expect_msgs), "recovered": [], "hits": 0,
               "bands": {}}
        ga = cc.safe_cmd(link, "gen_scenario", {"spec": spec, "load_tx": True}, timeout=120)
        if ga.status != CmdStatus.OK:
            dec["err"] = f"gen_scenario: {cc.ack_err(ga)}"
        else:
            sp = cc.safe_cmd(link, "set_plan", {"plan_path": ga.result["plan_path"]}, timeout=30)
            if sp.status != CmdStatus.OK:
                dec["err"] = f"set_plan: {cc.ack_err(sp)}"
            else:
                time.sleep(args.settle)
                cc.drain(link)
                txa = cc.safe_cmd(link, "start_tx", {"mode": "CONTINUOUS"}, timeout=30)
                if txa.status != CmdStatus.OK:
                    dec["err"] = f"start_tx: {cc.ack_err(txa)}"
                else:
                    dec["keyed"] = True
                    print(f"[decode] TX keyed, collecting {args.decode_s:g} s ...", flush=True)
                    tel2 = cc.collect(link, args.decode_s)
                    cc.safe_cmd(link, "stop_tx", {}, timeout=30)
                    # the sweep signals are continuous: their DETECTION events
                    # (with SNR) only arrive once the bursts end after stop_tx
                    cc.collect(link, 3.0, tel2)
                    got = tel2.decoded_set()
                    # level: detection SNR of the sweep bursts (the detector's
                    # CW-tone SNR wobbles 20 dB between keys; FSK is linear)
                    snrs = sorted(d["snr"] for d in tel2.detections
                                  if abs(d["f"] - int(args.center)) < args.rate / 2)
                    if snrs:
                        dec["det_snr_db"] = {"median": round(snrs[len(snrs) // 2], 1),
                                             "max": round(snrs[-1], 1), "n": len(snrs)}
                    dec["recovered"] = sorted(expect_msgs & got)
                    dec["hits"] = len(dec["recovered"])
                    dec["extra"] = sorted(got - expect_msgs)
                    dec["bands"] = {n: {"msg": b.get("msg", ""), "dn": b.get("dn", 0)}
                                    for n, b in tel2.bands.items()}
        rec["decode"] = dec
        print(f"[decode] {dec['hits']}/{len(expect_msgs)} messages"
              + (f"  det SNR median {dec['det_snr_db']['median']} dB (n={dec['det_snr_db']['n']})"
                 if dec.get("det_snr_db") else "")
              + (f"  ({dec.get('err')})" if dec.get("err") else ""), flush=True)
        st2 = cc.status_snapshot(link, 2.0)
        if st2:
            rec.update(overruns=st2["overruns"], underruns=st2["underruns"])
        ok = tone["detected"] and tone["within_slot"] and dec["hits"] >= DECODE_BAR
        rec["verdict"] = "PASS" if ok else "FAIL"
        if not tone["detected"]:
            rec["notes"].append("tone not detected")
        elif not tone["within_slot"]:
            rec["notes"].append(f"tone {tone['freq_err_hz']} Hz off (> 1 slot)")
        if dec["hits"] < DECODE_BAR:
            rec["notes"].append(f"only {dec['hits']}/{len(expect_msgs)} messages decoded")

    # PORT-G-18: STOP RX then CLOSE then START keeps the ports, no rc=-16
    if not args.skip_reinit:
        cc.safe_cmd(link, "stop_rx", {}, timeout=30)
        cc.safe_cmd(link, "close", {}, timeout=30)
        time.sleep(0.5)
        a2, dt2 = cc.init_ports(link, rx, tx, int(args.rate), int(args.center), args.gain,
                                args.atten, start_rx=False, clock=args.clock, gate=False)
        rec["reinit"] = {"status": CmdStatus(a2.status).name, "init_s": dt2,
                         "err": cc.ack_err(a2) if a2.status != CmdStatus.OK else None}
        if a2.status != CmdStatus.OK:
            rec["notes"].append(f"re-init after CLOSE failed: {cc.ack_err(a2)}")
            if rec["verdict"] == "PASS":
                rec["verdict"] = "FAIL"
    cc.teardown(link)
    print(f"[move {move}] {rec['verdict']}" + (f"  ({'; '.join(rec['notes'])})" if rec["notes"] else ""),
          flush=True)
    return rec


def uniformity(records):
    """PORT-G-17 / M-23: spread of the level across RX ports for one TX port,
    and across TX ports for one RX port (armed pairs only), on two metrics:
    the CW tone's detection SNR and the fsk_sweep bursts' median detection SNR."""
    groups = {}
    for r in records:
        rx, tx = r["rx_port"], r["tx_port"]
        if r.get("tone") and r["tone"].get("snr_db") is not None:
            v = r["tone"]["snr_db"]
            groups.setdefault(f"tone: TX {tx} -> RX ports", []).append((rx, v))
            groups.setdefault(f"tone: RX {rx} <- TX ports", []).append((tx, v))
        if (r.get("decode") or {}).get("det_snr_db"):
            v = r["decode"]["det_snr_db"]["median"]
            groups.setdefault(f"fsk: TX {tx} -> RX ports", []).append((rx, v))
            groups.setdefault(f"fsk: RX {rx} <- TX ports", []).append((tx, v))
    out = []
    for name, vals in sorted(groups.items()):
        if len(vals) < 2:
            continue
        s = [v for _, v in vals]
        out.append({"group": name, "ports": {p: v for p, v in vals},
                    "spread_db": round(max(s) - min(s), 1), "pass": (max(s) - min(s)) <= 2.0})
    return out


def _gps_cell(g):
    """Per-move GPSDO column: time to lock after this move's init + ppm."""
    if not g:
        return "-"
    if g.get("clock") != "gpsdo":
        return g.get("clock", "-")
    return f"{cc.fnum(g.get('t_lock_s'), 0)} s, {cc.fnum(g.get('ppm'), 4)} ppm" if g.get("locked") else "**no lock**"


def _gps_line(meta):
    """Header line: clock + the first lock (acquisition) + the spread over moves."""
    clock = meta.get("clock", "internal")
    checks = [c for c in (meta.get("gps") or {}).get("checks", []) if c.get("locked")]
    line = cc.gps_header(clock, checks[0] if checks else None)
    if clock == "gpsdo" and len(checks) > 1:
        ppm = [c["ppm"] for c in checks if c.get("ppm") is not None]
        line += (f"; {len(checks)} lock checks (one per keyed init), max re-lock "
                 f"{cc.fnum(max(c['t_lock_s'] for c in checks), 0)} s"
                 + (f", ppm {cc.fnum(min(ppm), 4)}..{cc.fnum(max(ppm), 4)}" if ppm else ""))
    return line


def markdown(meta, records, uni):
    rows = []
    for r in records:
        tone = r.get("tone") or {}
        dec = r.get("decode") or {}
        rows.append([r["move"], r["case"], r["rx_port"], r["tx_port"], r["relationship"], r["expect"],
                     f"{r['init_status']} ({cc.fnum(r['init_s'], 1)} s)",
                     ("yes" if tone.get("within_slot") else "no" if tone else "-")
                     + (f" ({tone.get('freq_err_hz')} Hz, {tone.get('snr_db')} dB)" if tone.get("detected") else ""),
                     f"{dec.get('hits', '-')}/6" if dec else "-",
                     f"{dec['det_snr_db']['median']} (n={dec['det_snr_db']['n']})" if dec.get("det_snr_db") else "-",
                     f"{r.get('overruns', '-')}/{r.get('underruns', '-')}",
                     (r.get("reinit") or {}).get("status", "-"),
                     _gps_cell(r.get("gps")),
                     f"**{r['verdict']}**" if r["verdict"] not in ("PASS",) else r["verdict"],
                     "; ".join(r["notes"])])
    n_ok = sum(1 for r in records if r["verdict"] == "PASS")
    txt = [f"# G40 port matrix -- {meta['serial']} @ {meta['host']}", "",
           f"{meta['date']}  rate {meta['rate_hz']/1e6:g} MSps  center {meta['center_hz']/1e6:.4f} MHz  "
           f"rx_gain {meta['rx_gain']}  tx_atten {meta['tx_atten']} dB  armed {meta['armed']}  "
           f"pad {meta.get('pad_db', 20):g} dB  tone +{meta['tone_offset_hz']/1e6:g} MHz, "
           f"scenario fsk_sweep.json (bar {DECODE_BAR}/6)", "",
           "- reference: " + _gps_line(meta), "",
           f"- M-22 port pairs: {n_ok}/{len(records)} pass or refused as listed", ""]
    if uni:
        txt += [f"- M-23 / PORT-G-17 RX uniformity (tone SNR spread <= 2 dB): "
                f"{sum(1 for u in uni if u['pass'])}/{len(uni)} groups within 2 dB", ""]
    txt += [cc.md_table(["move", "case", "RX", "TX", "relationship", "expect", "init",
                         "tone in slot", "decoded", "fsk det SNR dB", "over/under", "re-init",
                         "GPSDO lock", "verdict", "notes"], rows)]
    if uni:
        txt += ["", "## Uniformity (PORT-G-17)", "",
                cc.md_table(["group", "SNR per port (dB)", "spread dB", "<= 2 dB"],
                            [[u["group"], ", ".join(f"{p} {v}" for p, v in u["ports"].items()),
                              u["spread_db"], "yes" if u["pass"] else "**no**"] for u in uni])]
    return "\n".join(txt) + "\n"


def merge(paths, out):
    """Combine per-move JSONs (e.g. --pairs 1 ... --pairs 12 run one cable at a
    time) into one result set; the last file wins for a repeated move."""
    by_move, meta = {}, None
    for pth in paths:
        j = json.load(open(pth))
        meta = meta or j
        for r in j.get("moves", []):
            by_move[r["move"]] = r
    records = [by_move[k] for k in sorted(by_move)]
    uni = uniformity(records)
    meta = {k: v for k, v in meta.items() if k not in ("moves", "uniformity")}
    checks = [c for pth in paths for c in (json.load(open(pth)).get("gps") or {}).get("checks", [])]
    if checks:
        meta["gps"] = dict(meta.get("gps") or {}, checks=checks)
    meta.update(date=cc.iso_now(), merged_from=[os.path.basename(p) for p in paths],
                elapsed_s=round(sum(json.load(open(p)).get("elapsed_s", 0) for p in paths), 1))
    data = dict(meta, moves=records, uniformity=uni)
    md = markdown(meta, records, uni)
    jp, mp = cc.write_results(out, data, md)
    print(md + f"\n[saved] {jp}\n[saved] {mp}", flush=True)
    return 0 if all(r["verdict"] in ("PASS", "INIT-OK (unarmed)") for r in records) else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=os.environ.get("DPMG_G40_HOST"), required=not os.environ.get("DPMG_G40_HOST"))
    ap.add_argument("--port", type=int, default=47810)
    ap.add_argument("--arm", action="store_true", help="key the tone and the scenario")
    ap.add_argument("--pairs", default="all",
                    help="subset: move numbers '1,2,3' or 'TRx1A:TRx1B,...' (default all 12)")
    ap.add_argument("--rate", type=float, default=cc.RATE_NARROW)
    ap.add_argument("--center", type=float, default=cc.CENTER)
    ap.add_argument("--gain", type=int, default=255, help="RX gain index")
    ap.add_argument("--atten", type=float, default=30, help="TX attenuation dB")
    ap.add_argument("--scenario", default=SCENARIO, help="scenario JSON (default fsk_sweep.json)")
    ap.add_argument("--decode-s", type=float, default=25.0, dest="decode_s",
                    help="seconds to collect decodes per pair")
    ap.add_argument("--tone-hold", type=float, default=2.0, dest="tone_hold")
    ap.add_argument("--tone-offset", type=float, default=TONE_OFFSET_HZ, dest="tone_offset",
                    help=f"CW tone offset from center, Hz (default {TONE_OFFSET_HZ}; must sit "
                         "inside +-rate/2)")
    ap.add_argument("--pad-db", type=float, default=20, dest="pad_db",
                    help="pad in the loop, for the prompts and the report header (default 20)")
    ap.add_argument("--settle", type=float, default=2.0)
    ap.add_argument("--skip-reinit", action="store_true", dest="skip_reinit",
                    help="skip the PORT-G-18 STOP/CLOSE/START check per pair")
    ap.add_argument("--no-prompt", action="store_true", dest="no_prompt",
                    help="do not wait for Enter between moves (simulator / one cable)")
    ap.add_argument("--out", default=DEFAULT_OUT, help=f"result JSON (default {DEFAULT_OUT})")
    ap.add_argument("--merge", nargs="+", metavar="JSON", default=None,
                    help="no radio: merge per-move result JSONs (one invocation per cable move) "
                         "into --out, recomputing PORT-G-17 uniformity")
    cc.add_clock_args(ap)
    args = ap.parse_args()
    if args.merge:
        return merge(args.merge, args.out)

    moves = select_moves(args.pairs)
    if abs(args.tone_offset) >= args.rate / 2:
        raise SystemExit(f"--tone-offset {args.tone_offset:g} Hz is outside +-{args.rate/2:g} Hz "
                         f"(rate {args.rate:g} Sps): the tone could never be received")
    spec = json.load(open(args.scenario))
    if not args.arm:
        print("*** --arm not given: pairs are init + RX only (no tone, no decode). ***", flush=True)
    link = Link(args.host, args.port)
    print(f"[link] {link.serial} @ {args.host}:{args.port}   {len(moves)} move(s)", flush=True)
    args.clock = cc.resolve_clock(args.clock, link.serial)
    print(f"[link] reference clock {args.clock}"
          + ("  (every keyed init waits for GPSDO lock)" if args.clock == "gpsdo" else ""), flush=True)
    records = []
    aborted = None
    t0 = time.time()
    try:
        for (move, rx, tx, case_id, expect) in moves:
            if not cable_prompt(move, rx, tx, expect, args.no_prompt, args.pad_db):
                records.append({"move": move, "rx_port": rx, "tx_port": tx, "case": case_id,
                                "expect": expect, "verdict": "SKIPPED", "notes": ["skipped by operator"],
                                "relationship": "", "init_status": "-", "init_s": None})
                continue
            records.append(run_move(link, move, rx, tx, case_id, expect, args, spec))
    except KeyboardInterrupt:
        print("\n[port_matrix] interrupted; writing what was collected", flush=True)
        cc.teardown(link)
    except cc.GpsLockError as e:
        aborted = str(e)
        print(f"\n[port_matrix] ABORTED, nothing keyed on an unlocked reference: {e}", flush=True)
        cc.teardown(link)
    uni = uniformity(records)
    meta = {"test": "port_matrix", "date": cc.iso_now(), "host": args.host, "serial": link.serial,
            "rate_hz": int(args.rate), "center_hz": int(args.center), "rx_gain": args.gain,
            "tx_atten": args.atten, "armed": bool(args.arm), "tone_offset_hz": int(args.tone_offset),
            "pad_db": args.pad_db,
            "scenario": os.path.basename(args.scenario), "decode_bar": DECODE_BAR,
            "elapsed_s": round(time.time() - t0, 1), "clock": args.clock,
            "gps": cc.gps_summary(link, args.clock), "aborted": aborted}
    data = dict(meta, moves=records, uniformity=uni)
    md = markdown(meta, records, uni)
    if aborted:
        md += f"\n**ABORTED**: {aborted}\n"
    jp, mp = cc.write_results(args.out, data, md)
    print("\n" + md, flush=True)
    print(f"[saved] {jp}\n[saved] {mp}", flush=True)
    if aborted:
        return 3
    return 0 if all(r["verdict"] in ("PASS", "INIT-OK (unarmed)") for r in records) else 1


if __name__ == "__main__":
    sys.exit(main())
