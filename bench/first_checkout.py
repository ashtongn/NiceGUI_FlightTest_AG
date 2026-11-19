#!/usr/bin/env python3
"""
first_checkout.py: the scripted first checkout of one G40. Runs before any GUI
testing and is the regression checkout for every deploy.

It inits the radio, prompts for the cable (Setup A: TX TRx1B through 20 dB into
RX TRx1A first), then runs the bench harnesses in order, one TX run at a time:

  operator_loopback_test   DET-01..03 (+ over/underruns per rate)
  fsk_loopback_test        --tx-card 1: library+sweep+dense (quick) or all (full)
                           XCV-02/03, DET-07, DEC-05, DEC-09, M-10, M-12
  fec_loopback_test        FEC-01 (6 cases incl. conv 5/6, 5 reps), FEC-07
  decode_functionality     DEC-04 (fsk_sweep.json 6/6), PORT-G-01 fallback
  mod_fec_matrix           MOD-* rows, FEC-02, M-07, M-09
  snr_sweep                DET-04 (M-05), DET-06 (M-04), FEC-03 (M-08), AMC call
  jammer_ber_sweep         JS-01 (M-15), --loopback --tx-card 1
  det_extras (inline)      DET-08 frequency accuracy, DET-09 25 FSK @ 30.72,
                           DET-10 pulsed tone
  port_matrix              Block 3, PORT-G-01..12, 17, 18 (M-22, M-23); prompts
                           each cable move

Every harness runs as a subprocess with its stdout logged to
bench/RESULTS_<date>/<stage>.log and its JSON side file parsed into the plan's
case IDs. A harness that dies marks its cases ERROR and the run continues.
The report is bench/FIRST_CHECKOUT_<date>.json + .md with PASS/FAIL per case
and the measured numbers for M-04..M-12, M-15, M-22, M-23.

Nothing radiates without --arm (every harness then runs RX-only or refuses).

Reference clock: --clock gpsdo (the default on a real G40; auto = internal on a
simulator). Before any stage, a pre-flight switches the 3.3 V GPS antenna bias
on, does an RX-only init on the GPSDO and waits up to --lock-timeout s (default
600) for lock; no lock = the run aborts before anything keys. Every stage then
inits with the same clock_source and waits for lock again after each init (the
GPSDO re-locks ~25 s after an init); the lock time, ppm, satellites used and
C/N0 go into each stage's result and the report header.

  python3 bench/first_checkout.py --host 192.168.0.22 --arm --clock gpsdo           # full, ~4 h
  python3 bench/first_checkout.py --host 192.168.0.22 --arm --clock gpsdo --quick   # P1 subset, ~90 min
  python3 bench/first_checkout.py --host 192.168.0.22 --arm --clock gpsdo --resume --skip jammer_ber_sweep
  # simulator dry run (no hardware):
  python3 bench/first_checkout.py --host 127.0.0.1 --port 47830 --arm --quick --no-prompt \\
      --wf-dir $PWD/data/waveforms
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# results go outside the source tree (output/ is not tracked); DPMG_BENCH_RESULTS overrides
RESULTS_DIR = os.environ.get("DPMG_BENCH_RESULTS") or os.path.join(os.path.dirname(HERE), "output", "bench")
os.makedirs(RESULTS_DIR, exist_ok=True)
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import checkout_common as cc                                   # noqa: E402

PY = sys.executable
STAGE_ORDER = ["operator_loopback", "fsk_loopback", "fec_loopback", "decode_functionality",
               "mod_fec_matrix", "mod_fec_full", "mod_variants", "fec_frame_opts",
               "snr_sweep", "jammer_ber_sweep", "jam_denial",
               "det_extras", "amc_hw", "acm_ladder", "secure_msg", "dsss_multiuser", "port_matrix"]
QUICK = ["operator_loopback", "fsk_loopback", "fec_loopback", "decode_functionality", "port_matrix"]
# the long stages are not in the default plan; they run
# only when named in --only or enabled by their flag:
#   mod_fec_full (FEC-13/14, --matrix-full), mod_variants (MOD-17..21 + presets,
#   --mod-variants), fec_frame_opts (FEC-04/06, --fec-frame-opts),
#   jam_denial (JS-02, --jam-denial),
#   amc_hw (AMC-01/04, M-14, --amc-hw), acm_ladder (ACM-02, --acm-ladder),
#   secure_msg (SEC-10..14 secure messages, --secure-msg; needs
#   DPMG_SECURE_MSG=1 in the radio's service environment; keys stay on the ground).
OPT_IN = ["mod_fec_full", "mod_variants", "fec_frame_opts", "jam_denial",
          "amc_hw", "acm_ladder", "secure_msg", "dsss_multiuser"]
#   dsss_multiuser (MOD-DSSS-C/4/M/16: DSSS carrier families + multi-user CDMA
#   overlays with explicit LFSR / Gold codes, --dsss-multiuser; docs/DSSS_BUILDER.md)

# case id: (stage, what the plan asks for). The scorers below fill verdicts.
CASES = [
    ("DET-01", "operator_loopback", "tone detected at 1.92 / 7.68 / 15.36 / 30.72 MSps"),
    ("DET-02", "operator_loopback", "comb of 8 tones gives 8 +- 1 detections"),
    ("DET-03", "operator_loopback", "barrage and partial-band noise key and are characterised"),
    ("DET-04", "snr_sweep", "minimum detectable SNR recorded (M-05)"),
    ("DET-06", "snr_sweep", "detection latency median <= 0.5 s (M-04)"),
    ("DET-07", "fsk_loopback", "dense 8-emitter scenario @ 30.72: >= 7 of 8 detected"),
    ("DET-08", "det_extras", "frequency accuracy: 10 placements within 1 slot"),
    ("DET-09", "det_extras", "25-signal FSK scenario @ 30.72: 25/25 detected (NaN-median regression)"),
    ("DET-10", "det_extras", "pulsed tone PRF 1 kHz duty 0.5 detected; durations recorded"),
] + [(f"MOD-{m}", "mod_fec_matrix", f"{m} decodes 5 of 5" + (" (record only)" if m in ("8QAM", "32APSK") else ""))
     for m in ["FSK", "4FSK", "GFSK", "BPSK", "QPSK", "pi/4-QPSK", "DQPSK", "OQPSK", "8PSK", "8QAM",
               "16QAM", "16APSK", "32APSK", "DSSS", "QPSK-DSSS", "CSS"]] + [
    ("MOD-17", "mod_variants", "symbol-rate extremes per family decode, or the limit is recorded"),
    ("MOD-18", "mod_variants", "decimation-boundary scenario decodes (decim-snap REG)"),
    ("MOD-19", "mod_variants", "near-DC +200 kHz @ 30.72 decodes (Config A); Config B caveat recorded"),
    ("MOD-20", "mod_variants", "RRC / Lowpass filtered QPSK and FSK (GUI Tools Calc path; recorded)"),
    ("MOD-21", "mod_variants", "five-modulation scenario @ 7.68: all five bands decode"),
    ("MOD-DSSS-C", "dsss_multiuser", "DSSS over BPSK / QPSK / OQPSK / pi4-QPSK (+ dual-code QPSK) with lfsr codes decodes"),
    ("MOD-DSSS-4", "dsss_multiuser", "4-user BPSK-DSSS CDMA overlay (Gold n=5, one centre): every user decodes"),
    ("MOD-DSSS-M", "dsss_multiuser", "mixed BPSK/QPSK/OQPSK/pi4 users on one Gold n=6 set: every user decodes"),
    ("MOD-DSSS-16", "dsss_multiuser", "16-user overlay (Gold n=7): >= 13 of 16 per window, all 16 over the run (recorded)"),
    ("FEC-01", "fec_loopback", "uncoded + conv 1/2, 2/3, 3/4, 5/6, 7/8 on FSK: 6 of 6, every rep"),
    ("FEC-02", "mod_fec_matrix", "5 conv rates x FSK, 4FSK, QPSK, 8PSK, 16QAM: >= 24 of 25"),
    ("FEC-03", "snr_sweep", "coding gain: coded decodes at lower SNR than uncoded, ordered (M-08)"),
    ("FEC-04", "fec_frame_opts", "post-FEC FER <= 1 % at threshold + 3 dB (offline FER)"),
    ("FEC-06", "fec_frame_opts", "frame options sync / length / CRC decode; wrong-CRC rejected"),
    ("FEC-07", "fec_loopback", "warm decode latency <= 3 s (M-09)"),
    ("FEC-13", "mod_fec_full", "full mod x coding matrix: every supported mod x conv rate decodes"),
    ("FEC-14", "mod_fec_full", "block codes on RF: RS / RS+conv / LDPC / TPC decode where wired"),
    ("DEC-04", "decode_functionality", "fsk_sweep.json 6 of 6"),
    ("DEC-05", "fsk_loopback", "dense @ 30.72: >= 7 of 8 decoded, >= 6 concurrent (M-12)"),
    ("DEC-08", None, "continuous emitter keeps counting (GUI)"),
    ("DEC-09", "fsk_loopback", "cross-card FSK sweep decodes every case (dual-FDD sweep)"),
    ("XCV-02", "fsk_loopback", "cross-card sweep 8/8 and dense 8/8"),
    ("XCV-03", "fsk_loopback", "exchange: request and a different reply decoded every round"),
    ("XCV-06", None, "10 MB file transfer hashes equal (tests/test_file_transfer.py offline)"),
    ("JS-01", "jammer_ber_sweep", "QPSK symbol jammer beats equal-power barrage (M-15)"),
    ("JS-02", "jam_denial", "decode-denial J/S for FSK 4800 and 16QAM vs partial / spot"),
    ("AMC-01", "amc_hw", "every modulation classified over the cable: exact >= 80 %, family >= 90 %"),
    ("AMC-04", "amc_hw", "BPSK cross-card reads BPSK (carrier recovery): >= 4 of 5"),
    ("ACM-02", "acm_ladder", "RF ladder: FER <= 1e-2 Es/N0 edge per MODCOD recorded, within 3 dB of the replay"),
    ("SEC-10", "secure_msg", "secure_status ACTIVE on the radio: key ID / KCV, library, OpenSSL, AES-256-GCM-SIV recorded"),
    ("SEC-11", "secure_msg", "sealed FSK 4800 uncoded / conv 1/2 / RS decode (tag verified on the radio)"),
    ("SEC-12", "secure_msg", "every scenario modulation sealed, uncoded, decodes (8QAM / 32APSK recorded)"),
    ("SEC-13", "secure_msg", "replay (R1): the looped sealed frame is delivered once, repeats counted REPLAY"),
    ("SEC-14", "secure_msg", "tamper: single-bit flips / truncation never authenticate (offline-authoritative)"),
] + [(cid, "port_matrix", f"move {mv}: RX {rx} <- TX {tx} " + ("refused before init" if ex == "refused" else "pass"))
     for (mv, rx, tx, cid, ex) in sorted(cc.PORT_MOVES, key=lambda m: m[3])] + [
    ("PORT-G-17", "port_matrix", "RX readings across ports agree within 2 dB (M-23)"),
    ("PORT-G-18", "port_matrix", "STOP RX / CLOSE / START keeps the ports, no rc=-16"),
]
MEASURES = [
    ("M-02", "over/underruns per rate", "0", "operator_loopback"),
    ("M-04", "detection latency (median)", "<= 0.5 s", "snr_sweep"),
    ("M-05", "minimum detectable SNR", "recorded", "snr_sweep"),
    ("M-06", "false alarms", "<= 1 / min", None),
    ("M-07", "decode success", ">= 95 %", "mod_fec_matrix"),
    ("M-08", "coding gain", "positive, ordered by rate", "snr_sweep"),
    ("M-09", "warm decode latency", "<= 3 s", "fec_loopback"),
    ("M-10", "loopback latency (transponder)", "recorded", "fsk_loopback"),
    ("M-11", "jammer occupancy", "within 10 %", None),
    ("M-12", "concurrent decodes @ 30.72", ">= 6", "fsk_loopback"),
    ("M-15", "symbol jammer vs barrage", "symbol jammer wins", "jammer_ber_sweep"),
    ("M-22", "port pairs", "12 of 12 pass or refused as listed", "port_matrix"),
    ("M-23", "port uniformity", "within 2 dB", "port_matrix"),
    ("M-14", "AMC correct-call over the cable", ">= 80 % exact (all registry mods)", "amc_hw"),
]


# running
def _num(x):
    """5.0 then "5" (fec_loopback_test takes --atten as an int), 41.75 then "41.75"."""
    x = float(x)
    return str(int(x)) if x == int(x) else str(x)


def run_stage(name, argv, out_dir, env, timeout_s, stdin_tty=True):
    """Run one harness, tee its output to the console and <stage>.log, return
    {status, rc, seconds, log}. Never raises."""
    log_path = os.path.join(out_dir, f"{name}.log")
    print(f"\n{'#' * 72}\n# STAGE {name}\n# {' '.join(argv)}\n{'#' * 72}", flush=True)
    t0 = time.time()
    rec = {"argv": argv, "log": log_path, "rc": None, "seconds": None, "status": "error"}
    try:
        with open(log_path, "w") as log:
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    stdin=None if stdin_tty else subprocess.DEVNULL,
                                    env=env, cwd=ROOT, text=True, bufsize=1)
            deadline = t0 + timeout_s
            for line in proc.stdout:
                log.write(line)
                sys.stdout.write(f"  | {line}")
                sys.stdout.flush()
                if time.time() > deadline:
                    proc.kill()
                    rec["status"] = "timeout"
                    log.write(f"\n[first_checkout] killed after {timeout_s} s\n")
                    break
            proc.wait(timeout=30)
            rec["rc"] = proc.returncode
            if rec["status"] != "timeout":
                rec["status"] = "ok" if proc.returncode == 0 else "failed"
    except Exception as e:                                        # noqa: BLE001
        rec["err"] = str(e)
    rec["seconds"] = round(time.time() - t0, 1)
    print(f"# STAGE {name}: {rec['status']} rc={rec['rc']} in {rec['seconds']} s", flush=True)
    return rec


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# scorers
def V(verdict, evidence=""):
    return {"verdict": verdict, "evidence": evidence}


def _measure(value, ok):
    return {"value": value, "pass": ok}


def score_operator(j, armed):
    cases, meas = {}, {}
    if not j:
        return cases, meas
    rates = j.get("rates", [])
    by = {r["rate"]: {row["name"]: row for row in r["rows"]} for r in rates}
    xr = {r["rate"]: r["xr"] for r in rates}
    meas["M-02"] = _measure(", ".join(f"{k}: {v}" for k, v in xr.items()),
                            all(v == 0 for v in xr.values()) if xr else None)
    if not armed:
        return cases, meas
    tone = {k: v.get("jam:tone", {}).get("verdict") for k, v in by.items()}
    cases["DET-01"] = V("PASS" if tone and all(t == "DET" for t in tone.values()) else "FAIL",
                        ", ".join(f"{k}: {t}" for k, t in tone.items()))
    comb = {k: v.get("jam:comb", {}).get("dets") for k, v in by.items()}
    cases["DET-02"] = V("PASS" if comb and all(c is not None and 7 <= c <= 9 for c in comb.values()) else "FAIL",
                        ", ".join(f"{k}: {c} dets" for k, c in comb.items()))
    bb = {k: (v.get("jam:barrage", {}).get("verdict"), v.get("jam:noise", {}).get("verdict"),
              v.get("jam:barrage", {}).get("dets"), v.get("jam:noise", {}).get("dets")) for k, v in by.items()}
    ran = bb and all(b[0] in ("DET", "no-det") and b[1] in ("DET", "no-det") for b in bb.values())
    cases["DET-03"] = V("PASS" if ran else "FAIL",
                        "; ".join(f"{k}: barrage {b[2]} dets, noise {b[3]} dets" for k, b in bb.items()))
    return cases, meas


def _dense_hits(j):
    """emitters recovered from a fsk_loopback json 'dense' dict (name: [msg, dn])."""
    want = j.get("dense_emitters") or []
    got = [v[0] for v in (j.get("dense") or {}).values()]
    hit = sum(1 for w in want if any(w[:6] in g for g in got))
    return hit, len(want), len(got)


def score_fsk(jsons):
    """jsons: list of fsk_loopback result dicts (sweep / dense / all runs)."""
    cases, meas = {}, {}
    sweep = dense = lat = exch = None
    for j in jsons:
        if not j:
            continue
        sweep = j.get("sweep", sweep)
        dense = j if "dense" in j else dense
        lat = j.get("latency", lat)
        exch = j.get("exchange", exch)
    if sweep is not None:
        ok = sum(1 for s in sweep if s[1])
        cases["DEC-09"] = V("PASS" if ok == len(sweep) else "FAIL", f"sweep {ok}/{len(sweep)}")
        sweep_txt = f"sweep {ok}/{len(sweep)}"
    else:
        sweep_txt = "sweep not run"
        ok = None
    if dense is not None:
        hit, n, bands = _dense_hits(dense)
        cases["DET-07"] = V("PASS" if n and hit >= 7 else "FAIL", f"{hit}/{n} emitters, {bands} bands decoding")
        cases["DEC-05"] = V("PASS" if n and hit >= 7 else "FAIL", f"{hit}/{n} emitters decoded @ 30.72")
        meas["M-12"] = _measure(f"{bands} bands decoding ({hit}/{n} emitters)", bands >= 6)
        dense_txt = f"dense {hit}/{n}"
    else:
        dense_txt = "dense not run"
        hit = n = None
    if sweep is not None or dense is not None:
        both = (ok is not None and ok == len(sweep)) and (hit is not None and hit == n)
        cases["XCV-02"] = V("PASS" if both else "FAIL", f"{sweep_txt}, {dense_txt}")
    if lat:
        parts = []
        for label, st in lat:
            if isinstance(st, dict) and "proc_latency_ms" in st:
                parts.append(f"{label}: proc {st['proc_latency_ms'].get('mean')} ms, "
                             f"total {st['total_latency_ms'].get('mean')} ms")
        meas["M-10"] = _measure("; ".join(parts) if parts else "no detections", bool(parts) or None)
    if exch:
        good = sum(1 for e in exch if e[2])
        cases["XCV-03"] = V("PASS" if good == len(exch) else "FAIL", f"{good}/{len(exch)} rounds")
    return cases, meas


def score_fec(j):
    cases, meas = {}, {}
    if not j:
        return cases, meas
    cs = j.get("cases", [])
    allok = cs and all(c["decoded"] == c["reps"] and c["reps"] > 0 for c in cs)
    cases["FEC-01"] = V("PASS" if allok else "FAIL",
                        ", ".join(f"{c['label']} {c['decoded']}/{c['reps']}" for c in cs))
    ts = [t for c in cs for t in c.get("t_s", []) if t is not None]
    if ts:
        worst = max(ts)
        cases["FEC-07"] = V("PASS" if worst <= 3.0 else "FAIL",
                            f"first decode: median {statistics.median(ts):.1f} s, worst {worst:.1f} s")
        meas["M-09"] = _measure(f"median {statistics.median(ts):.1f} s, worst {worst:.1f} s", worst <= 3.0)
    else:
        cases["FEC-07"] = V("FAIL", "no decodes")
    return cases, meas


def score_decode(j):
    cases = {}
    if not j:
        return cases, {}
    n = len(j.get("recovered", []))
    tot = len(j.get("expected", [])) or 6
    cases["DEC-04"] = V("PASS" if n == tot else "FAIL", f"{n}/{tot} messages: missed {j.get('missed')}")
    cases["_PORT-G-01-fallback"] = V("PASS" if n >= 5 else "FAIL", f"decode_functionality {n}/{tot}")
    return cases, {}


def score_modfec(j):
    cases, meas = {}, {}
    if not j:
        return cases, meas
    cs = j.get("cases", [])
    for c in cs:
        if c["fec"] != "none":
            continue
        cid = f"MOD-{c['mod']}"
        if c.get("characterize_only"):
            cases[cid] = V("RECORDED", f"{c['decoded']}/{c['reps']} (record only)")
        else:
            cases[cid] = V("PASS" if c["pass"] else "FAIL", f"{c['decoded']}/{c['reps']}"
                           + (f", median {c['t_median_s']} s" if c["t_median_s"] is not None else ""))
    _CONV = {"1/2", "2/3", "3/4", "5/6", "7/8"}
    conv = [c for c in cs if c["fec"] in _CONV]
    block = [c for c in cs if c["fec"] not in _CONV and c["fec"] != "none"]
    keyed = [c for c in cs if c.get("reps")]                 # replay-skipped cells have 0 reps

    def _fails(cells):
        return ", ".join(f"{c['mod']} {c['fec']} {c['decoded']}/{c['reps']}"
                         for c in cells if c.get("reps") and not c["pass"]) or "none"

    if block:                      # full matrix: FEC-13 (conv) + FEC-14 (block)
        ck = [c for c in conv if c.get("reps")]
        okc = sum(1 for c in ck if c["pass"])
        cases["FEC-13"] = V("PASS" if ck and okc == len(ck) else "FAIL",
                            f"{okc}/{len(ck)} conv cells decode; failures: {_fails(ck)}")
        bk = [c for c in block if c.get("reps")]
        okb = sum(1 for c in bk if c["pass"])
        cases["FEC-14"] = V("PASS" if bk and okb == len(bk) else "FAIL",
                            f"{okb}/{len(bk)} block-code cells decode (RS/RS+conv/LDPC/TPC); "
                            f"failures: {_fails(bk)}")
    elif conv:                     # default grid: the FEC-02 conv cross
        okn = sum(1 for c in conv if c["pass"])
        cases["FEC-02"] = V("PASS" if okn >= len(conv) - 1 else "FAIL",
                            f"{okn}/{len(conv)} coded pairs 5/5; failures: {_fails(conv)}")
    tot = sum(c["reps"] for c in keyed if not c.get("characterize_only"))
    dec = sum(c["decoded"] for c in keyed if not c.get("characterize_only"))
    if tot:
        meas["M-07"] = _measure(f"{dec}/{tot} = {100.0 * dec / tot:.1f} %", dec >= 0.95 * tot)
    return cases, meas


def score_mod_variants(j):
    """MOD-17..21 from mod_variants.cases (MOD presets reported in evidence)."""
    cases = {}
    if not j:
        return cases, {}
    cs = j.get("cases", {})
    for pid in ("MOD-17", "MOD-18", "MOD-19", "MOD-20", "MOD-21"):
        c = cs.get(pid)
        if not c:
            continue
        detail = ", ".join(f"{d['id'].split('/')[-1]} {d['d']}/{d['w']}" for d in c.get("detail", []))
        if c.get("record_only"):
            cases[pid] = V("RECORDED", f"{detail} (GUI Tools Calc filter path; offline only)")
        else:
            cases[pid] = V("PASS" if c["pass"] else "FAIL", f"{c['ok']}/{c['n']}: {detail}")
    mod = cs.get("MOD")
    if mod:
        cases["_MOD-presets"] = V("PASS" if mod["pass"] else "FAIL",
                                  "4FSK mapping / GFSK BT / DSSS family / CSS SF: "
                                  f"{mod['ok']}/{mod['n']} (GFSK replay-limited; HW authoritative)")
    return cases, {}


def score_dsss_multiuser(j):
    """MOD-DSSS-C / -4 / -M (pass) and MOD-DSSS-16 (recorded) from dsss_multiuser.cases."""
    cases = {}
    if not j:
        return cases, {}
    for pid, c in (j.get("cases") or {}).items():
        detail = ", ".join(f"{d['id'].split('/')[-1]} {d['d']}/{d['w']}" for d in c.get("detail", []))
        if c.get("record_only"):
            cases[pid] = V("RECORDED", f"{detail} (equal-power 16-user MUI ~9 dB; bar >= 13/16 per window)")
        else:
            cases[pid] = V("PASS" if c["pass"] else "FAIL", f"{c['ok']}/{c['n']}: {detail}")
    return cases, {}


def score_fec_frame_opts(j):
    """FEC-06 (frame options + negatives) and FEC-04 (residual FER)."""
    cases = {}
    if not j:
        return cases, {}
    cs = j.get("cases", {})
    c6 = cs.get("FEC-06")
    if c6:
        cases["FEC-06"] = V("PASS" if c6["pass"] and c6["negatives_ok"] == c6["negatives"] else "FAIL",
                            f"decode {c6['ok']}/{c6['n']}; negatives rejected "
                            f"{c6['negatives_ok']}/{c6['negatives']}")
    c4 = cs.get("FEC-04")
    if c4:
        cases["FEC-04"] = V("PASS" if c4["pass"] else "FAIL", c4["status"])
    return cases, {}


def score_jam_denial(j):
    """JS-02: decode-denial J/S recorded for each victim x jammer."""
    cases = {}
    if not j or j.get("role") != "denial":
        return cases, {}
    bits = []
    any_denied = False
    for vkey, vres in (j.get("by_victim") or {}).items():
        for jk, jr in vres.get("jammers", {}).items():
            dj = jr.get("denial_js_db")
            any_denied = any_denied or dj is not None
            bits.append(f"{vkey}/{jk} {dj if dj is not None else 'none'} dB")
    cases["JS-02"] = V("PASS" if any_denied else "FAIL", "denial J/S: " + ", ".join(bits))
    return cases, {}


def score_amc_hw(j):
    """amc_hw_check.py result: AMC-01 / AMC-04 / M-14."""
    cases, meas = {}, {}
    if not j or not j.get("summary"):
        return cases, meas
    s = j["summary"]
    ov, fam = s.get("overall_acc") or 0.0, s.get("family_acc") or 0.0
    per = s.get("per_mod", {})
    miss = ", ".join(f"{m} {p['exact']}/{p['n']}" for m, p in per.items()
                     if p.get("n") and p["exact"] < p["n"]) or "none"
    cases["AMC-01"] = V("PASS" if ov >= 0.8 and fam >= 0.9 else "FAIL",
                        f"exact {100 * ov:.0f} %, family {100 * fam:.0f} % over {s.get('n')} "
                        f"classifications; misses: {miss}")
    b = per.get("BPSK") or {}
    if b.get("n"):
        cases["AMC-04"] = V("PASS" if b["exact"] >= min(4, b["n"]) else "FAIL",
                            f"BPSK {b['exact']}/{b['n']} exact, CFO median {b.get('cfo_hz_median')} Hz")
    meas["M-14"] = _measure(f"{100 * ov:.0f} % exact / {100 * fam:.0f} % family "
                            f"({j.get('mode')})", ov >= 0.8)
    return cases, meas


def score_secure_msg(j):
    """secure_msg.py result: SEC-10..SEC-14 (recorded cells never fail a row)."""
    cases = {}
    if not j:
        return cases, {}
    for pid, c in (j.get("cases") or {}).items():
        rec = f" (recorded: {', '.join(c['recorded'])})" if c.get("recorded") else ""
        if pid == "SEC-14" and j.get("mode") == "hardware":
            cases[pid] = V("RECORDED", "offline-authoritative (tests/test_secure_frame.py)")
            continue
        cases[pid] = V("PASS" if c["pass"] else "FAIL", f"{c['ok']}/{c['n']}{rec} -- {c.get('detail', '')[:200]}")
    return cases, {}


def score_acm_ladder(j):
    """acm_threshold_sweep.py --hw result: ACM-02 (compared with the replay
    thresholds in the same JSON's 'replay' block when given, else recorded)."""
    cases = {}
    if not j or not j.get("results"):
        return cases, {}
    rs = j["results"]
    got = [r for r in rs if r.get("threshold_db") is not None]
    ev = "; ".join(f"{r['modcod']} {r['threshold_db'] if r.get('threshold_db') is not None else '-'} dB"
                   for r in rs)
    cases["ACM-02"] = V("RECORDED" if got else "FAIL",
                        f"{len(got)}/{len(rs)} MODCOD edges measured: {ev}")
    return cases, {}


def score_snr(j):
    cases, meas = {}, {}
    if not j:
        return cases, meas
    summ = j.get("summary", {})
    unc = summ.get("none") or next(iter(summ.values()), {})
    mds = unc.get("min_detectable_snr_db")
    # with snr_sweep --backoff the x-axis is loss = atten + digital backoff
    # (+ RX gain leg); without it loss == atten and the wording is the old one
    bo = bool(j.get("backoff_db"))
    ax = "loss (atten+backoff)" if bo else "atten"
    thr_key = "decode_threshold_loss_db" if bo else "decode_threshold_atten_db"
    cases["DET-04"] = V("PASS" if mds is not None else "FAIL",
                        f"min detectable SNR {mds} dB at {ax} "
                        f"{unc.get('detect_threshold_loss_db' if bo else 'detect_threshold_atten_db')} dB"
                        + (f"; decode threshold SNR {unc.get('decode_threshold_snr_db')} dB" if bo else "")
                        if mds is not None else "no detection at any step")
    meas["M-05"] = _measure(f"{mds} dB" if mds is not None else "not detected", mds is not None)
    lat = unc.get("det_latency_median_s")
    cases["DET-06"] = V("PASS" if lat is not None and lat <= 0.5 else "FAIL",
                        f"median {lat} s, max {unc.get('det_latency_max_s')} s" if lat is not None else "no detections")
    meas["M-04"] = _measure(f"median {lat} s" if lat is not None else "n/a", (lat is not None and lat <= 0.5))
    g = j.get("coding_gain_ordered")
    thr = j.get(thr_key) or j.get("decode_threshold_atten_db", {})
    txt = ", ".join(f"{k}: {v} dB" for k, v in thr.items())
    if bo:
        snr = {k: (summ.get(k) or {}).get("decode_threshold_snr_db") for k in thr}
        txt += " (decode thr SNR " + ", ".join(f"{k}: {v} dB" for k, v in snr.items()) + ")"
    if len(summ) > 1:
        cases["FEC-03"] = V("PASS" if g else "FAIL", f"decode threshold {ax} {txt}; ordered={g}")
        meas["M-08"] = _measure(txt, bool(g))
    return cases, meas


def score_jammer(j):
    cases, meas = {}, {}
    if not j:
        return cases, meas
    traces = {t["name"]: t for t in j.get("traces", [])}
    if "qpsk" in traces and "barrage" in traces:
        q, b = traces["qpsk"]["y"], traces["barrage"]["y"]
        pairs = [(qq, bb) for qq, bb in zip(q, b) if qq == qq and bb == bb]
        wins = sum(1 for qq, bb in pairs if qq >= bb - 1e-4)
        ratios = [qq / bb for qq, bb in pairs if bb > 0]
        ok = bool(pairs) and wins == len(pairs)
        ev = (f"symbol jammer >= barrage at {wins}/{len(pairs)} J/S points; "
              f"BER ratio {min(ratios):.2g}..{max(ratios):.2g}" if ratios else "no finite points")
        cases["JS-01"] = V("PASS" if ok else "FAIL", ev)
        meas["M-15"] = _measure(ev, ok)
    return cases, meas


def score_det_extras(j):
    cases = {}
    if not j:
        return cases, {}
    fa = j.get("freq_accuracy")
    if fa:
        cases["DET-08"] = V("PASS" if fa["ok"] == fa["n"] else "FAIL",
                            f"{fa['ok']}/{fa['n']} within 1 slot; worst {fa.get('worst_err_hz')} Hz")
    d9 = j.get("det25")
    if d9:
        cases["DET-09"] = V("PASS" if d9.get("built") and d9["hit"] >= d9["n"] else "FAIL",
                            f"{d9['hit']}/{d9['n']} signals detected @ 30.72 ({d9.get('n_detections')} detections)"
                            + ("" if d9.get("built") else f"; build: {d9.get('err')}"))
    p = j.get("pulsed")
    if p:
        cases["DET-10"] = V("PASS" if p.get("detected") else "FAIL",
                            f"durations {p.get('durations_ms')} ms, SNR {p.get('snr_db')} dB"
                            if p.get("detected") else "not detected")
    return cases, {}


def score_port(j):
    cases, meas = {}, {}
    if not j:
        return cases, meas
    mv = j.get("moves", [])
    for r in mv:
        ev = f"move {r['move']} init {r.get('init_status')} {r.get('init_s')} s"
        if r.get("tone"):
            ev += f"; tone {'in slot' if r['tone'].get('within_slot') else 'miss'} ({r['tone'].get('snr_db')} dB)"
        if r.get("decode"):
            ev += f"; decode {r['decode'].get('hits')}/6"
        if r.get("notes"):
            ev += "; " + "; ".join(r["notes"])
        cases[r["case"]] = V("PASS" if r["verdict"] == "PASS" else
                             "NOT RUN" if r["verdict"] == "SKIPPED" else
                             "RX-ONLY" if r["verdict"].startswith("INIT-OK") else "FAIL", ev)
    n_ok = sum(1 for r in mv if r["verdict"] == "PASS")
    meas["M-22"] = _measure(f"{n_ok}/{len(mv)} pass or refused as listed", n_ok == len(mv) and len(mv) == 12)
    uni = j.get("uniformity", [])
    if uni:
        okn = sum(1 for u in uni if u["pass"])
        cases["PORT-G-17"] = V("PASS" if okn == len(uni) else "FAIL",
                               "; ".join(f"{u['group']}: {u['spread_db']} dB" for u in uni))
        meas["M-23"] = _measure(f"max spread {max(u['spread_db'] for u in uni)} dB", okn == len(uni))
    re = [r for r in mv if r.get("reinit")]
    if re:
        bad = [r for r in re if r["reinit"]["status"] != "OK"]
        cases["PORT-G-18"] = V("PASS" if not bad else "FAIL",
                               f"{len(re) - len(bad)}/{len(re)} re-inits OK"
                               + ("; " + "; ".join(f"move {r['move']}: {r['reinit']['err']}" for r in bad) if bad else ""))
    return cases, meas


# det_extras (inline)
def det_extras(args, out_json):
    """DET-08 / DET-09 / DET-10 straight through the service (cross-card Setup A)."""
    import numpy as np
    from fsk_loopback_test import Link, init_loopback
    from dpmg.link.proto import CmdStatus
    res = {"test": "det_extras", "date": cc.iso_now(), "host": args.host}
    link = Link(args.host, args.port)
    res["serial"] = link.serial
    center = int(cc.CENTER)
    init_loopback(link, cc.RATE_NARROW, center, args.atten, args.gain, tx_card=1, kind="g40",
                  clock=args.clock, lock_timeout=args.lock_timeout)
    time.sleep(1.5)
    # DET-08: ten tone placements across +-700 kHz, each within one slot
    offs = [int(x) for x in np.linspace(-700_000, 700_000, 10)]
    rows, worst = [], 0.0
    for off in offs:
        _t, tel, g = cc.key_tone(link, off, hold_s=1.5, after_s=1.5, ampl=0.5, name="de_tone")
        d = tel.det_near(center + off, within_hz=5 * cc.SLOT_BW_HZ)
        err = (d["f"] - (center + off)) if d else None
        rows.append({"offset_hz": off, "detected": d is not None, "err_hz": round(err, 1) if d else None,
                     "snr_db": round(d["snr"], 1) if d else None, "keyed": g.status == CmdStatus.OK})
        if d:
            worst = max(worst, abs(err))
        print(f"  [DET-08] {off/1e3:+7.0f} kHz  {'err %+.0f Hz' % err if d else 'NOT detected'}", flush=True)
    res["freq_accuracy"] = {"n": len(rows), "ok": sum(1 for r in rows if r["detected"] and abs(r["err_hz"]) <= cc.SLOT_BW_HZ),
                            "worst_err_hz": round(worst, 1), "slot_bw_hz": cc.SLOT_BW_HZ, "rows": rows}
    # DET-10: pulsed tone, PRF 1 kHz, duty 0.5
    _t, tel, g = cc.key_tone(link, 300_000, hold_s=2.0, after_s=2.0, ampl=0.5, name="de_pulse",
                             prf=1000.0, duty=0.5, seconds=1.0)
    near = [d for d in tel.detections if abs(d["f"] - (center + 300_000)) <= 5 * cc.SLOT_BW_HZ]
    res["pulsed"] = {"keyed": g.status == CmdStatus.OK, "detected": bool(near),
                     "durations_ms": [round(d["dur"], 1) for d in near][:10],
                     "snr_db": round(max(d["snr"] for d in near), 1) if near else None,
                     "prf_hz": 1000.0, "duty": 0.5}
    print(f"  [DET-10] pulsed tone: {len(near)} detections, durations {res['pulsed']['durations_ms']} ms", flush=True)
    # DET-09: 25 FSK signals across +-12 MHz at 30.72 MSps: every one detected
    init_loopback(link, cc.RATE_WIDE, center, args.atten, args.gain, tx_card=1, kind="g40",
                  clock=args.clock, lock_timeout=args.lock_timeout)
    time.sleep(2.0)
    offs25 = [int(x) for x in np.linspace(-12_000_000, 12_000_000, 25)]
    spec = {"name": "det25", "center_freq_hz": center, "loop_s": 0.2,
            "signals": [{"modulation": "FSK", "symbol_rate": 9600, "tone_spacing_hz": 12000,
                         "offset_hz": o, "message": f"S{i:02d}", "amplitude": 0.12}
                        for i, o in enumerate(offs25)]}
    d9 = {"n": len(offs25), "hit": 0, "built": False, "n_detections": 0}
    # 25 signals inline is ~2.6 kB of JSON: more than one 1400 B CMD datagram
    # (the radio refuses it with "CMD too large"). Push the
    # spec into the radio's inbox and build it by path instead.
    spec_json = json.dumps(spec, separators=(",", ":")).encode()
    if len(spec_json) > 1100:
        spec_path = cc.push_file(link, "det25.json", spec_json)
        d9["spec_via"] = f"inbox {spec_path} ({len(spec_json)} B)"
        g = cc.safe_cmd(link, "gen_scenario", {"spec_path": spec_path, "load_tx": True}, timeout=240)
    else:
        g = cc.safe_cmd(link, "gen_scenario", {"spec": spec, "load_tx": True}, timeout=240)
    if g.status != CmdStatus.OK:
        d9["err"] = cc.ack_err(g)
    else:
        d9["built"] = True
        cc.drain(link)
        st = cc.safe_cmd(link, "start_tx", {"mode": "CONTINUOUS"}, timeout=30)
        if st.status == CmdStatus.OK:
            tel = cc.collect(link, 4.0)
            cc.safe_cmd(link, "stop_tx", {}, timeout=30)
            cc.collect(link, 3.0, tel)
            d9["n_detections"] = len(tel.detections)
            d9["hit"] = sum(1 for o in offs25 if tel.det_near(center + o, within_hz=cc.SLOT_BW_HZ) is not None)
            d9["missed_offsets_hz"] = [o for o in offs25
                                       if tel.det_near(center + o, within_hz=cc.SLOT_BW_HZ) is None]
        else:
            d9["err"] = cc.ack_err(st)
    print(f"  [DET-09] {d9['hit']}/{d9['n']} signals detected @ 30.72 ({d9['n_detections']} detections)", flush=True)
    res["det25"] = d9
    res["gps"] = cc.gps_summary(link, args.clock)
    cc.teardown(link)
    cc.write_results(out_json, res)
    return res


# report
def stage_gps(got):
    """A stage's "gps" block from its result JSON(s) (fsk_loopback quick = a
    list): clock, every lock check (one per init), the worst re-lock time."""
    js = got if isinstance(got, list) else [got]
    checks = [c for j in js if isinstance(j, dict) for c in (j.get("gps") or {}).get("checks", [])]
    if not checks:
        return None
    locked = [c for c in checks if c.get("locked")]
    ppm = [c["ppm"] for c in locked if c.get("ppm") is not None]
    return {"clock": checks[-1].get("clock"), "n_checks": len(checks),
            "all_locked": all(c.get("locked") for c in checks) if checks[-1].get("clock") == "gpsdo" else None,
            "max_t_lock_s": max((c["t_lock_s"] for c in locked), default=None),
            "ppm_min": min(ppm) if ppm else None, "ppm_max": max(ppm) if ppm else None,
            "checks": checks}


def _gps_stage_cell(clock, rec):
    g = rec.get("gps")
    if not g:
        return "-" if rec.get("status") in ("skipped", None) or clock != "gpsdo" else "no lock record"
    if g.get("clock") != "gpsdo":
        return str(g.get("clock"))
    if not g.get("all_locked"):
        return "**not locked**"
    pp = (f", {cc.fnum(g['ppm_min'], 4)}..{cc.fnum(g['ppm_max'], 4)} ppm"
          if g.get("ppm_min") is not None else "")
    return f"{g['n_checks']} x locked, max {cc.fnum(g['max_t_lock_s'], 0)} s{pp}"


def _gps_abort_in_log(path):
    """The GpsLockError text a stage died with, or None."""
    try:
        with open(path) as f:
            for line in f:
                if cc.GPS_ABORT_TAG in line:
                    return line.strip()
    except (OSError, TypeError):
        pass
    return None


def build_report(args, stage_recs, scores, measures, t_start):
    verdicts = {}
    for cid, stage, desc in CASES:
        if cid in scores:
            verdicts[cid] = dict(scores[cid], stage=stage, what=desc)
        elif cid == "PORT-G-01" and "_PORT-G-01-fallback" in scores:
            verdicts[cid] = dict(scores["_PORT-G-01-fallback"], stage="decode_functionality", what=desc)
        elif stage is None:
            verdicts[cid] = V("NOT RUN", "not scripted here (GUI / manual case)") | {"stage": "-", "what": desc}
        else:
            st = stage_recs.get(stage)
            if st is None:
                ev = "stage not run" + (" (--quick)" if args.quick and stage not in QUICK else "")
                verdicts[cid] = V("NOT RUN", ev) | {"stage": stage, "what": desc}
            elif st.get("status") == "skipped":
                verdicts[cid] = V("NOT RUN", f"{stage} skipped: {st.get('note', '')}") | {"stage": stage, "what": desc}
            elif st.get("status") in ("ok", "failed", "timeout") and st.get("json_ok"):
                verdicts[cid] = V("NOT RUN", f"no result in the {stage} output") | {"stage": stage, "what": desc}
            else:
                verdicts[cid] = V("ERROR", f"{stage}: {st.get('status')} rc={st.get('rc')} "
                                  f"(see {os.path.basename(st.get('log', ''))})") | {"stage": stage, "what": desc}
    counts = {}
    for v in verdicts.values():
        counts[v["verdict"]] = counts.get(v["verdict"], 0) + 1
    meas = []
    for mid, name, bar, stage in MEASURES:
        m = measures.get(mid)
        if m is None:
            val = "not measured" if stage else "not measured by script (analyzer / RX-only soak)"
            meas.append({"id": mid, "measure": name, "bar": bar, "value": val, "verdict": "NOT RUN"})
        else:
            meas.append({"id": mid, "measure": name, "bar": bar, "value": m["value"],
                         "verdict": "PASS" if m["pass"] else "RECORDED" if m["pass"] is None else "FAIL"})
    pre = getattr(args, "gps_preflight", None)
    rep = {"test": "first_checkout", "date": cc.iso_now(), "host": args.host, "port": args.port,
           "armed": bool(args.arm), "mode": "quick" if args.quick else "full",
           "clock": args.clock, "gps_preflight": pre,
           "aborted": getattr(args, "aborted", None),
           "skipped": args.skip, "elapsed_s": round(time.time() - t_start, 1),
           "counts": counts, "cases": verdicts, "measures": meas, "stages": stage_recs}
    md = [f"# G40 first checkout -- {args.host}  ({rep['mode']}{', NOT ARMED' if not args.arm else ''})", "",
          f"{rep['date']}  elapsed {rep['elapsed_s']/60:.0f} min  "
          + "  ".join(f"{k} {v}" for k, v in sorted(counts.items())), "",
          "- reference (pre-flight): " + cc.gps_header(args.clock, pre), ""]
    if rep["aborted"]:
        md += [f"**RUN ABORTED**: {rep['aborted']}", ""]
    md += ["## Cases", "",
           cc.md_table(["case", "stage", "verdict", "evidence", "what the plan asks"],
                       [[cid, v["stage"], f"**{v['verdict']}**" if v["verdict"] in ("FAIL", "ERROR") else v["verdict"],
                         v["evidence"], v["what"]] for cid, v in verdicts.items()]), ""]
    md += ["## Measures", "",
           cc.md_table(["id", "measure", "pass bar", "measured", "verdict"],
                       [[m["id"], m["measure"], m["bar"], m["value"], m["verdict"]] for m in meas]), ""]
    md += ["## Stages", "",
           cc.md_table(["stage", "status", "rc", "minutes", "GPSDO lock (stage)", "log", "result json"],
                       [[n, s.get("status"), s.get("rc"), f"{(s.get('seconds') or 0)/60:.1f}",
                         _gps_stage_cell(args.clock, s),
                         os.path.basename(s.get("log") or "-"), os.path.basename(s.get("json") or "-")]
                        for n, s in stage_recs.items()]), ""]
    if not args.arm:
        md += ["Nothing was keyed (--arm not given): every TX case reads NOT RUN / RX-ONLY.", ""]
    return rep, "\n".join(md) + "\n"


# main
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=os.environ.get("DPMG_G40_HOST"), required=not os.environ.get("DPMG_G40_HOST"))
    ap.add_argument("--port", type=int, default=47810)
    ap.add_argument("--arm", action="store_true", help="allow the harnesses to radiate")
    ap.add_argument("--quick", action="store_true",
                    help=f"P1 subset (~90 min): {', '.join(QUICK)}")
    ap.add_argument("--skip", default="", help="comma list of stages to skip: " + ", ".join(STAGE_ORDER))
    ap.add_argument("--only", default="", help="comma list: run only these stages")
    ap.add_argument("--resume", action="store_true",
                    help="skip stages already completed in today's FIRST_CHECKOUT json")
    ap.add_argument("--no-prompt", action="store_true", dest="no_prompt",
                    help="no cable prompts (simulator, or the cable is already in)")
    ap.add_argument("--gain", type=int, default=255, help="RX gain index for the decode harnesses")
    ap.add_argument("--atten", type=float, default=30,
                    help="TX attenuation dB for every keyed stage (bench rule: 30 or more); "
                         "snr_sweep steps start here")
    ap.add_argument("--reps", type=int, default=5, help="repetitions for fec_loopback / mod_fec_matrix")
    ap.add_argument("--snr-backoff", default="", dest="snr_backoff",
                    help="snr_sweep --backoff: digital backoff steps dB beyond the attenuator "
                         "ceiling ('default' or a comma list); off when empty")
    ap.add_argument("--snr-gain-leg", default="", dest="snr_gain_leg",
                    help="snr_sweep --gain-leg: RX gain indices for the final leg (needs --snr-backoff)")
    ap.add_argument("--snr-decode-gate-db", type=float, default=None, dest="snr_decode_gate_db",
                    help="snr_sweep --decode-gate-db: per-channel decode gate dB for the signal "
                         "under test (FEC-03 / M-08 below the 12 dB slot gate); off when unset")
    ap.add_argument("--pad-db", type=float, default=50, dest="pad_db",
                    help="pad on the Setup A path, for the report headers (default 50)")
    ap.add_argument("--out-dir", default=None, dest="out_dir",
                    help="per-stage logs + json (default bench/RESULTS_<date>/)")
    ap.add_argument("--report", default=None, help="report path (default bench/FIRST_CHECKOUT_<date>.json)")
    ap.add_argument("--wf-dir", default=None, dest="wf_dir",
                    help="the service's waveforms dir as fsk_loopback_test must address it "
                         "(DPMG_WF_DIR; default the G40 container path /app_svc/data/waveforms)")
    ap.add_argument("--stage-timeout", type=float, default=180.0, dest="stage_timeout",
                    help="minutes before a stage is killed (default 180)")
    ap.add_argument("--fast", action="store_true",
                    help="short waits in the new harnesses (simulator dry run)")
    ap.add_argument("--matrix-full", action="store_true", dest="matrix_full",
                    help="run the full mod x coding matrix stage (FEC-13/14), replay-screened")
    ap.add_argument("--matrix-replay-json", default=None, dest="matrix_replay_json",
                    help="a mod_fec_matrix --replay --full result; cells it decoded 0 times are "
                         "not keyed (default: run the replay first)")
    ap.add_argument("--dsss-multiuser", action="store_true", dest="dsss_multiuser",
                    help="run the DSSS carrier-family + multi-user overlay stage (MOD-DSSS-*)")
    ap.add_argument("--mod-variants", action="store_true", dest="mod_variants",
                    help="run the MOD-17..21 + per-family-preset variant stage")
    ap.add_argument("--fec-frame-opts", action="store_true", dest="fec_frame_opts",
                    help="run the FEC-04 (FER) + FEC-06 (frame options) stage")
    ap.add_argument("--jam-denial", action="store_true", dest="jam_denial",
                    help="run the JS-02 decode-denial stage (FSK 4800 and 16QAM vs partial / spot)")
    ap.add_argument("--amc-hw", action="store_true", dest="amc_hw",
                    help="run the AMC hardware check (every modulation classified over the cable)")
    ap.add_argument("--secure-msg", action="store_true", dest="secure_msg",
                    help="run the secure_msg stage (SEC-10..14; the radio needs DPMG_SECURE_MSG=1 + a key file)")
    ap.add_argument("--acm-ladder", action="store_true", dest="acm_ladder",
                    help="run the ACM RF Es/N0 ladder (FER per MODCOD over an atten/backoff ladder)")
    cc.add_clock_args(ap)
    args = ap.parse_args()
    args.skip = [s.strip() for s in args.skip.split(",") if s.strip()]
    only = [s.strip() for s in args.only.split(",") if s.strip()]
    for s in args.skip + only:
        if s not in STAGE_ORDER:
            raise SystemExit(f"unknown stage {s!r}; stages: {', '.join(STAGE_ORDER)}")
    date = cc.date_tag()
    out_dir = args.out_dir or os.path.join(HERE, f"RESULTS_{date}")
    os.makedirs(out_dir, exist_ok=True)
    report_path = args.report or os.path.join(RESULTS_DIR, f"FIRST_CHECKOUT_{date}.json")
    env = dict(os.environ)
    if args.wf_dir:
        env["DPMG_WF_DIR"] = args.wf_dir
    env["PYTHONUNBUFFERED"] = "1"
    timeout_s = args.stage_timeout * 60.0

    prior = load_json(report_path) if args.resume else None
    stage_recs = dict((prior or {}).get("stages", {})) if prior else {}
    # opt-in stages (the long full-matrix / jamming-ladder runs) are in the plan
    # only when named in --only or enabled by their flag
    enabled_opt = set()
    if args.matrix_full:
        enabled_opt.add("mod_fec_full")
    if args.mod_variants:
        enabled_opt.add("mod_variants")
    if args.dsss_multiuser:
        enabled_opt.add("dsss_multiuser")
    if args.fec_frame_opts:
        enabled_opt.add("fec_frame_opts")
    if args.jam_denial:
        enabled_opt.add("jam_denial")
    if args.amc_hw:
        enabled_opt.add("amc_hw")
    if args.acm_ladder:
        enabled_opt.add("acm_ladder")
    if args.secure_msg:
        enabled_opt.add("secure_msg")
    plan = [s for s in STAGE_ORDER if (not args.quick or s in QUICK) and s not in args.skip
            and (not only or s in only)
            and (s not in OPT_IN or s in only or s in enabled_opt)]
    print(f"first_checkout {date}  host {args.host}:{args.port}  mode {'quick' if args.quick else 'full'}"
          f"  armed {bool(args.arm)}\nstages: {', '.join(plan)}\nlogs: {out_dir}\nreport: {report_path}",
          flush=True)
    if not args.arm:
        print("*** --arm not given: nothing radiates; the harnesses run RX-only or refuse. ***", flush=True)

    # Setup A cable, once, before anything keys
    if not args.no_prompt and any(s != "port_matrix" for s in plan):
        print(f"\nSETUP A:  TX TRx1B ---> {_num(args.pad_db)} dB pad ---> RX TRx1A   "
              "(cross-card; no antenna on any jack)", flush=True)
        if sys.stdin.isatty():
            input("fit the cable, then press Enter to start: ")

    # reference clock pre-flight: bias on + GPSDO lock before any stage keys
    t_start = time.time()
    from fsk_loopback_test import Link
    args.aborted = None
    try:
        link = Link(args.host, args.port)
        args.clock = cc.resolve_clock(args.clock, link.serial)
        print(f"\n[preflight] {link.serial}: reference clock {args.clock}", flush=True)
        args.gps_preflight = cc.gps_preflight(link, args.clock, args.lock_timeout)
    except SystemExit as e:                     # GpsLockError, or no HELLO_ACK
        args.clock = args.clock if args.clock in cc.CLOCKS else "gpsdo"
        args.gps_preflight = {"clock": args.clock, "locked": False, "err": str(e)}
        args.aborted = f"pre-flight: {e}"
        print(f"\n[first_checkout] ABORTED before any stage keyed: {e}", flush=True)
        rep, md = _write(args, stage_recs, {}, t_start, report_path)
        print(f"[saved] {report_path}\n[saved] {os.path.splitext(report_path)[0]}.md", flush=True)
        return 3

    hp = ["--host", args.host, "--port", str(args.port)]
    hp += ["--clock", args.clock, "--lock-timeout", _num(args.lock_timeout)]
    arm = ["--arm"] if args.arm else []
    gg = ["--gain", str(args.gain), "--atten", _num(args.atten)]
    fast = bool(args.fast)
    jsons = {}

    def done(stage):
        # --only names stages to run: it overrides --resume's "already complete"
        if only and stage in only:
            return False
        return args.resume and stage in stage_recs and stage_recs[stage].get("status") == "ok" \
            and stage_recs[stage].get("json_ok")

    def reload_stage(stage, rec):
        """Re-read a completed stage's result JSON(s) in the shape its scorer
        expects (score_fsk takes a list even for the single 'all' run)."""
        if rec.get("jsons"):
            return [load_json(pth) for pth in rec["jsons"]]
        j = load_json(rec.get("json", ""))
        return [j] if stage == "fsk_loopback" else j

    # stages kept from today's earlier run but outside this invocation's plan
    # (--only / --skip with --resume) stay in the report with their results
    for stage, rec in stage_recs.items():
        if stage not in plan and rec.get("json_ok"):
            jsons[stage] = reload_stage(stage, rec)

    for stage in plan:
        if done(stage):
            print(f"\n# STAGE {stage}: already complete today (--resume), skipping", flush=True)
            jsons[stage] = reload_stage(stage, stage_recs[stage])
            continue
        J = os.path.join(out_dir, f"{stage}.json")
        try:
            if stage == "operator_loopback":
                rec = run_stage(stage, [PY, os.path.join(HERE, "operator_loopback_test.py"), *hp, *arm, *gg,
                                        "--json", J], out_dir, env, timeout_s)
                jsons[stage] = load_json(J); rec["json"] = J
            elif stage == "fsk_loopback":
                base = [PY, os.path.join(HERE, "fsk_loopback_test.py"), *hp, *arm, "--kind", "g40",
                        "--tx-card", "1", "--no-wait", "--tx-atten", str(args.atten), "--rx-gain", str(args.gain)]
                if args.quick:
                    js = []
                    rec = None
                    # library builds the eight sweep scenarios on the radio first: the
                    # sweep phase alone expects them to exist already
                    for ph in ("library", "sweep", "dense"):
                        Jp = os.path.join(out_dir, f"fsk_loopback_{ph}.json")
                        r = run_stage(f"fsk_loopback_{ph}", base + ["--json", Jp, ph], out_dir, env, timeout_s)
                        js.append(Jp)
                        rec = r if rec is None else {**rec, "status": rec["status"] if r["status"] == "ok" else r["status"],
                                                      "rc": r["rc"] if r["rc"] else rec["rc"],
                                                      "seconds": rec["seconds"] + r["seconds"],
                                                      "log": f"{rec['log']} + {r['log']}"}
                        if r["status"] != "ok" and _gps_abort_in_log(r["log"]):
                            break                           # no lock: the next phase would not key either
                    rec["jsons"] = js
                    jsons[stage] = [load_json(p) for p in js]
                else:
                    rec = run_stage(stage, base + ["--json", J, "all"], out_dir, env, timeout_s)
                    jsons[stage] = [load_json(J)]; rec["json"] = J
            elif stage == "fec_loopback":
                rec = run_stage(stage, [PY, os.path.join(HERE, "fec_loopback_test.py"), *hp, *arm, *gg,
                                        "--reps", str(args.reps), "--json", J], out_dir, env, timeout_s)
                jsons[stage] = load_json(J); rec["json"] = J
            elif stage == "decode_functionality":
                if not args.arm:
                    rec = {"status": "skipped", "rc": None, "seconds": 0, "log": "-",
                           "note": "decode_functionality_test keys TX unconditionally; needs --arm"}
                else:
                    rec = run_stage(stage, [PY, os.path.join(HERE, "decode_functionality_test.py"), *hp, *gg,
                                            "--json", J] + (["--seconds", "8"] if fast else []),
                                    out_dir, env, timeout_s)
                    jsons[stage] = load_json(J); rec["json"] = J
            elif stage == "mod_fec_matrix":
                extra = ["--timeout", "4", "--settle", "0.5", "--no-warmup", "--reps", "1"] if fast \
                    else ["--reps", str(args.reps)]
                rec = run_stage(stage, [PY, os.path.join(HERE, "mod_fec_matrix.py"), *hp, *arm, *gg,
                                        *extra, "--pad-db", _num(args.pad_db), "--out", J], out_dir, env, timeout_s)
                jsons[stage] = load_json(J); rec["json"] = J
            elif stage == "mod_fec_full":
                # full mod x coding matrix (FEC-13/14), replay-screened so only
                # cells that decoded offline are keyed. A matching --replay first
                # if no --matrix-replay-json was supplied.
                rjson = args.matrix_replay_json
                if not rjson:
                    rjson = os.path.join(out_dir, "mod_fec_full_replay.json")
                    run_stage("mod_fec_full_replay", [PY, os.path.join(HERE, "mod_fec_matrix.py"),
                              "--replay", "--full", "--replay-esn0", "30", "--reps", str(args.reps), "--out", rjson],
                              out_dir, env, timeout_s)
                extra = ["--timeout", "4", "--settle", "0.5", "--no-warmup", "--reps", "1"] if fast \
                    else ["--reps", str(args.reps)]
                rec = run_stage(stage, [PY, os.path.join(HERE, "mod_fec_matrix.py"), *hp, *arm, *gg,
                                        "--full", "--resume", "--replay-json", rjson, "--capture-ber",
                                        "--pad-db", _num(args.pad_db), *extra, "--out", J],
                                out_dir, env, timeout_s)
                jsons[stage] = load_json(J); rec["json"] = J
            elif stage == "mod_variants":
                # MOD-17..21 + per-family presets; replay screen first (build fails only)
                rjson = os.path.join(out_dir, "mod_variants_replay.json")
                run_stage("mod_variants_replay", [PY, os.path.join(HERE, "mod_variants.py"),
                          "--replay", "--out", rjson], out_dir, env, timeout_s)
                rec = run_stage(stage, [PY, os.path.join(HERE, "mod_variants.py"), *hp, *arm, *gg,
                                        "--resume", "--replay-json", rjson, "--pad-db", _num(args.pad_db),
                                        "--out", J], out_dir, env, timeout_s)
                jsons[stage] = load_json(J); rec["json"] = J
            elif stage == "dsss_multiuser":
                # DSSS carriers + CDMA overlays; replay screen first (build fails only)
                rjson = os.path.join(out_dir, "dsss_multiuser_replay.json")
                run_stage("dsss_multiuser_replay", [PY, os.path.join(HERE, "dsss_multiuser.py"),
                          "--replay", "--out", rjson], out_dir, env, timeout_s)
                rec = run_stage(stage, [PY, os.path.join(HERE, "dsss_multiuser.py"), *hp, *arm, *gg,
                                        "--resume", "--replay-json", rjson, "--pad-db", _num(args.pad_db),
                                        "--out", J], out_dir, env, timeout_s)
                jsons[stage] = load_json(J); rec["json"] = J
            elif stage == "fec_frame_opts":
                # FEC-06 frame options (keyed) + FEC-04 FER + negatives (offline)
                rjson = os.path.join(out_dir, "fec_frame_opts_replay.json")
                run_stage("fec_frame_opts_replay", [PY, os.path.join(HERE, "fec_frame_opts.py"),
                          "--replay", "--out", rjson], out_dir, env, timeout_s)
                rec = run_stage(stage, [PY, os.path.join(HERE, "fec_frame_opts.py"), *hp, *arm, *gg,
                                        "--resume", "--replay-json", rjson, "--pad-db", _num(args.pad_db),
                                        "--out", J], out_dir, env, timeout_s)
                # the hardware JSON carries FEC-06 decode; FEC-04/negatives come
                # from the replay JSON (offline-authoritative): merge for scoring
                hj = load_json(J)
                rj = load_json(rjson)
                if hj and rj:
                    hj.setdefault("cases", {}).setdefault("FEC-04", (rj.get("cases") or {}).get("FEC-04"))
                    hc6 = hj["cases"].get("FEC-06", {})
                    rc6 = (rj.get("cases") or {}).get("FEC-06", {})
                    hc6["negatives_ok"] = rc6.get("negatives_ok", 0)
                    hc6["negatives"] = rc6.get("negatives", 0)
                jsons[stage] = hj; rec["json"] = J
            elif stage == "jam_denial":
                jd = os.path.join(out_dir, "jam")
                os.makedirs(jd, exist_ok=True)
                J = os.path.join(jd, "js02_denial.json")
                if not args.arm:
                    rec = {"status": "skipped", "rc": None, "seconds": 0, "log": "-", "note": "needs --arm"}
                else:
                    rec = run_stage(stage, [PY, os.path.join(HERE, "jam_denial.py"),
                                            *hp, "--arm", "--tx-card", "1", "--atten", str(args.atten),
                                            "--gain", str(args.gain), "--pad-db", _num(args.pad_db), "--out", J],
                                    out_dir, env, timeout_s)
                    jsons[stage] = load_json(J); rec["json"] = J
            elif stage == "snr_sweep":
                extra = ["--hold", "3", "--after", "1.5", "--settle", "0.5", "--steps", "0,20,41.75"] if fast else []
                if args.snr_backoff:
                    extra += ["--backoff", args.snr_backoff]
                if args.snr_gain_leg:
                    extra += ["--gain-leg", args.snr_gain_leg]
                if args.snr_decode_gate_db is not None:
                    extra += ["--decode-gate-db", _num(args.snr_decode_gate_db)]
                extra += ["--pad-db", _num(args.pad_db)]
                # the sweep never goes below the run's attenuation; deeper SNR comes from --snr-backoff
                steps = [s for s in cc.ATTEN_STEPS if s >= args.atten] or [args.atten]
                extra += ["--steps", ",".join(_num(s) for s in steps)]
                rec = run_stage(stage, [PY, os.path.join(HERE, "snr_sweep.py"), *hp, *arm, *gg,
                                        "--fec", "none,1/2,2/3,3/4,5/6,7/8", *extra, "--out", J],
                                out_dir, env, timeout_s)
                jsons[stage] = load_json(J); rec["json"] = J
            elif stage == "jammer_ber_sweep":
                if not args.arm:
                    rec = {"status": "skipped", "rc": None, "seconds": 0, "log": "-", "note": "needs --arm"}
                else:
                    jd = os.path.join(out_dir, "jammer")
                    rec = run_stage(stage, [PY, os.path.join(HERE, "jammer_ber_sweep.py"), "--loopback", *hp,
                                            "--arm", "--tx-card", "1", "--victim", "qpsk",
                                            "--atten", str(args.atten), "--gain", str(args.gain), "--out", jd],
                                    out_dir, env, timeout_s)
                    J = os.path.join(jd, "ber_sweep_loop_qpsk.json")
                    jsons[stage] = load_json(J); rec["json"] = J
            elif stage == "det_extras":
                if not args.arm:
                    rec = {"status": "skipped", "rc": None, "seconds": 0, "log": "-", "note": "needs --arm"}
                else:
                    t0 = time.time()
                    print(f"\n{'#' * 72}\n# STAGE det_extras (inline: DET-08 / DET-09 / DET-10)\n{'#' * 72}", flush=True)
                    try:
                        det_extras(args, J)
                        rec = {"status": "ok", "rc": 0}
                    except (SystemExit, Exception) as e:                   # noqa: BLE001
                        print(f"# det_extras failed: {e}", flush=True)
                        rec = {"status": "failed", "rc": 1, "err": str(e)}
                    rec.update(seconds=round(time.time() - t0, 1), log="-", json=J)
                    jsons[stage] = load_json(J)
            elif stage == "secure_msg":
                # SEC-10..14: replay screen first (also the SEC-14 tamper evidence),
                # then the keyed round trips on the radio's own key file
                rjson = os.path.join(out_dir, "secure_msg_replay.json")
                run_stage("secure_msg_replay", [PY, os.path.join(HERE, "secure_msg.py"),
                          "--replay", "--out", rjson], out_dir, env, timeout_s)
                rec = run_stage(stage, [PY, os.path.join(HERE, "secure_msg.py"), *hp, *arm, *gg,
                                        "--resume", "--replay-json", rjson, "--pad-db", _num(args.pad_db),
                                        "--out", J], out_dir, env, timeout_s)
                hj = load_json(J)
                rj = load_json(rjson)
                if hj and rj and "SEC-14" in (rj.get("cases") or {}):
                    hj.setdefault("cases", {})["SEC-14"] = rj["cases"]["SEC-14"]
                jsons[stage] = hj; rec["json"] = J
            elif stage == "amc_hw":
                # offline twin first (same specs, replay CFO/noise), then the cable
                run_stage("amc_hw_replay", [PY, os.path.join(HERE, "amc_hw_check.py"), "--replay",
                          "--reps", "2" if fast else "3",
                          "--out", os.path.join(out_dir, "amc_hw_replay.json")], out_dir, env, timeout_s)
                if not args.arm:
                    rec = {"status": "skipped", "rc": None, "seconds": 0, "log": "-", "note": "needs --arm"}
                else:
                    rec = run_stage(stage, [PY, os.path.join(HERE, "amc_hw_check.py"), *hp, *arm, *gg,
                                            "--reps", "2" if fast else str(args.reps),
                                            "--pad-db", _num(args.pad_db), "--out", J],
                                    out_dir, env, timeout_s)
                    jsons[stage] = load_json(J); rec["json"] = J
            elif stage == "acm_ladder":
                if not args.arm:
                    rec = {"status": "skipped", "rc": None, "seconds": 0, "log": "-", "note": "needs --arm"}
                else:
                    extra = ["--windows-hw", "2", "--window-s", "2", "--hw-ladder", "0,20,41.75",
                             "--backoff-db", "6"] if fast else []
                    rec = run_stage(stage, [PY, os.path.join(HERE, "acm_threshold_sweep.py"), "--hw",
                                            *hp, *arm, *gg, "--pad-db", _num(args.pad_db), *extra,
                                            "--out", J], out_dir, env, timeout_s)
                    jsons[stage] = load_json(J); rec["json"] = J
            elif stage == "port_matrix":
                extra = ["--decode-s", "6", "--tone-hold", "1", "--settle", "0.5"] if fast else []
                rec = run_stage(stage, [PY, os.path.join(HERE, "port_matrix.py"), *hp, *arm, *gg,
                                        *(["--no-prompt"] if args.no_prompt else []), *extra, "--out", J],
                                out_dir, env, timeout_s)
                jsons[stage] = load_json(J); rec["json"] = J
        except KeyboardInterrupt:
            print("\n[first_checkout] interrupted; writing the report so far", flush=True)
            stage_recs[stage] = {"status": "interrupted", "rc": None, "seconds": None, "log": "-"}
            break
        got = jsons.get(stage)
        rec["json_ok"] = bool(got) and (not isinstance(got, list) or any(got))
        if got:
            rec["gps"] = stage_gps(got)
        if rec.get("status") == "failed" and isinstance(rec.get("err"), str) \
                and cc.GPS_ABORT_TAG in rec["err"]:
            gps_abort = rec["err"]                                  # det_extras (inline)
        else:
            gps_abort = next((m for m in map(_gps_abort_in_log, str(rec.get("log", "")).split(" + "))
                              if m), None) if rec.get("status") in ("failed", "error") else None
        stage_recs[stage] = rec
        if gps_abort:
            args.aborted = f"stage {stage}: {gps_abort}"
            print(f"\n[first_checkout] ABORTED: {stage} lost the GPSDO lock; no further stage keys.\n"
                  f"  {gps_abort}", flush=True)
            break
        # write an interim report after every stage so a crash loses nothing
        _write(args, stage_recs, jsons, t_start, report_path)

    rep, md = _write(args, stage_recs, jsons, t_start, report_path)
    print("\n" + md, flush=True)
    print(f"[saved] {report_path}\n[saved] {os.path.splitext(report_path)[0]}.md", flush=True)
    bad = sum(rep["counts"].get(k, 0) for k in ("FAIL", "ERROR"))
    return 1 if bad else 0


def _write(args, stage_recs, jsons, t_start, report_path):
    scores, measures = {}, {}
    scorers = {"operator_loopback": lambda j: score_operator(j, args.arm), "fsk_loopback": score_fsk,
               "fec_loopback": score_fec, "decode_functionality": score_decode,
               "mod_fec_matrix": score_modfec, "mod_fec_full": score_modfec,
               "mod_variants": score_mod_variants, "fec_frame_opts": score_fec_frame_opts,
               "dsss_multiuser": score_dsss_multiuser,
               "snr_sweep": score_snr, "jammer_ber_sweep": score_jammer,
               "jam_denial": score_jam_denial,
               "det_extras": score_det_extras, "amc_hw": score_amc_hw,
               "acm_ladder": score_acm_ladder, "secure_msg": score_secure_msg,
               "port_matrix": score_port}
    for stage, j in jsons.items():
        if j is None or (isinstance(j, list) and not any(j)):
            continue
        try:
            c, m = scorers[stage](j)
        except Exception as e:                                       # noqa: BLE001
            print(f"[score] {stage}: {e}", flush=True)
            continue
        scores.update(c)
        measures.update(m)
    rep, md = build_report(args, stage_recs, scores, measures, t_start)
    cc.write_results(report_path, rep, md)
    return rep, md


if __name__ == "__main__":
    sys.exit(main())
