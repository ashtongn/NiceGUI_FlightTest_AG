#!/usr/bin/env python3
"""
secure_msg.py: secure messages (AES-256-GCM-SIV) over the Setup A cable (SEC-10..SEC-14),
offline first (--replay), then on the G40.

Keys live on the ground (this bench process): every frame is sealed here
(frame_codec.encode_secure) and shipped to the radio as raw on-air `bits` in
the scenario spec; the radio modulates it, decodes it back on its RX card,
finds the CRC-valid sealed body and forwards it unopened as a SEALED_FRAME
datagram; this bench opens it and compares the plaintext. The radio needs
only DPMG_SECURE_MSG=1 in the dpmg_svc environment (to parse + forward) and
no key file. The bench key: DPMG_KEY_FILE on this host, else a throw-away key
generated in a temp dir for the run (recorded in the JSON as "key_source").

Cases:
  SEC-10  radio secure_status: flag on, no keys on the radio, crypto line of
          both hosts recorded (library, OpenSSL, AEAD AES-256-GCM-SIV, nonce)
  SEC-11  sealed FSK 4800 round trip: uncoded, conv 1/2, RS: the forwarded
          body opens on the ground with the sent plaintext within --timeout
  SEC-12  every scenario modulation, sealed, uncoded (--mods narrows); 8QAM /
          32APSK recorded only (as the MOD rows)
  SEC-13  replay (R1): the looped scenario repeats the same sealed frame on
          air; the ground must deliver it once and count every repeat REPLAY
  SEC-14  tamper / bit flips / truncation: offline-authoritative (the radio
          builds only what it is given); recorded from the replay run

  python3 bench/secure_msg.py --replay --out bench/RESULTS_<date>/secure_msg_replay.json
  python3 bench/secure_msg.py --host 192.168.0.22 --arm --clock gpsdo --pad-db 50 \\
      --replay-json <replay>.json --out bench/RESULTS_<date>/secure_msg.json
  python3 bench/secure_msg.py --estimate

Nothing transmits without --arm. --resume keeps cases already complete in --out.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import socket
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

import checkout_common as cc                                        # noqa: E402

CENTER = cc.CENTER
SR = 4800
SPACING = 5000
OFF = 300_000.0
T_CASE_S = 6.0
ESN0_DB = 30.0
SEC11_CODINGS = {"none": None, "1/2": {"type": "conv", "rate": "1/2"}, "rs": {"type": "rs"}}
RECORD_ONLY_MODS = {"8QAM", "32APSK"}
REPLAY_LIMITED = {"GFSK"}                       # replay model only; the radio decodes GFSK


def mods_all():
    import mod_fec_matrix as m
    return list(m.MOD_PARAMS)


def build_cases(mods=None):
    C = [{"id": "SEC-10", "plan": "SEC-10", "kind": "status"}]
    for label, fec in SEC11_CODINGS.items():
        C.append({"id": f"SEC-11/fsk-{label.replace('/', '')}", "plan": "SEC-11", "kind": "roundtrip",
                  "mod": "FSK", "fec": fec, "label": label})
    for mod in (mods or mods_all()):
        C.append({"id": f"SEC-12/{mod.replace('/', '')}", "plan": "SEC-12", "kind": "roundtrip",
                  "mod": mod, "fec": None, "label": "none",
                  "record_only": mod in RECORD_ONLY_MODS})
    C.append({"id": "SEC-13", "plan": "SEC-13", "kind": "replay"})
    C.append({"id": "SEC-14", "plan": "SEC-14", "kind": "tamper"})
    return C


# ground-side key / context
def ground_setup(work_dir):
    """The bench's SecureContext: DPMG_KEY_FILE when the operator set one on
    this host, else a throw-away key in work_dir. Returns (ctx, key_source)."""
    from dpmg.secure import api as sf
    os.environ[sf.FLAG_SECURE_MSG] = "1"
    src = "operator key file " + os.environ[sf.ENV_KEY_FILE] if os.environ.get(sf.ENV_KEY_FILE) else "throw-away"
    if not os.environ.get(sf.ENV_KEY_FILE):
        p = os.path.join(work_dir, "bench-keys.json")
        sf.write_key_file(sf.generate_key_file(200, 1, label="bench"), p)
        os.environ[sf.ENV_KEY_FILE] = p
        os.environ["DPMG_NONCE_STATE"] = os.path.join(work_dir, "bench-nonce.json")
    sf.set_context(None)
    ctx = sf.get_context()
    if ctx is None:
        raise SystemExit(f"cannot build the ground secure context: {sf.last_error()}")
    return ctx, src


def _message(case, ctx):
    cap = ctx.capacity(case.get("fec"), long_header=True)
    return f"SEC {case['mod']} {case['label']}"[:min(cap or 1, 40)]


def _spec_for(case, message, ctx):
    """Seal on the ground, ship bits: the scenario the radio (or the replay) builds."""
    from dpmg.framing import frame_codec as fc
    from dpmg.scenario import gen_scenario_json as g
    import mod_fec_matrix as m
    mod = case["mod"]
    spec = m.cell_spec(mod, "none", message, CENTER, name="sec_" + case["id"].split("/")[-1].replace("-", "_").lower())
    sig = spec["signals"][0]
    sig["bits"] = fc.encode_secure(message.encode("utf-8"), case.get("fec"), ctx=ctx)
    sig["secure"] = True
    if case.get("fec"):
        sig["fec"] = dict(case["fec"])
    sig.pop("message", None)
    spec["sample_rate_hz"] = float(cc.RATE_NARROW)
    try:
        iq0, _e = g.build_signal(sig, float(cc.RATE_NARROW))
        spec["loop_s"] = max(0.2, math.ceil(len(iq0) / float(cc.RATE_NARROW) * 1.1 / 0.05) * 0.05)
    except Exception:                                               # noqa: BLE001
        pass
    return spec


# offline replay
def replay_roundtrip(case, rng, work_dir, ctx, windows=3):
    from dpmg.scenario import gen_scenario_json as g
    import replay_chain as rc
    from dpmg.secure import api as sf
    res = {"id": case["id"], "plan": case["plan"], "mod": case["mod"], "fec": case["label"],
           "decoded": 0, "want": 1, "status": "", "verdicts": [], "record_only": bool(case.get("record_only"))}
    msg = _message(case, ctx)
    res["message"] = msg
    try:
        spec = _spec_for(case, msg, ctx)
        g.build_scenario(spec, work_dir)
        iq, plan, wl = rc.load_scenario(work_dir, spec["name"])
    except Exception as e:                                           # noqa: BLE001
        res.update(status=f"build_fail: {e}")
        res["pass"] = False
        return res
    ch = wl[0][0]
    fs = float(plan.sample_rate_hz)
    _fo, decim = rc.worker_fs_out(wl, fs)
    n0 = rc.burst_power(iq) / (float(ch.symbol_rate) * 10 ** (ESN0_DB / 10.0))
    ok = False
    for _key in range(2):
        lock = {}
        base = (np.roll(np.tile(iq, 2), int(rng.integers(0, len(iq))))
                * np.exp(1j * rng.uniform(0, 2 * np.pi))).astype(np.complex64)
        bb0, fse0 = rc.channelize_loop(rc.spectrum(base), fs, ch.f_center_hz - plan.center_freq_hz, decim)
        rc.decode(bb0, ch, fse0, lock)
        for _w in range(windows):
            x = rc.awgn(base, n0, fs, rng)
            bb, fse = rc.channelize_loop(rc.spectrum(x), fs, ch.f_center_hz - plan.center_freq_hz, decim)
            r = rc.decode(bb, ch, fse, lock)
            if r.valid and r.sealed:
                r = sf.open_result(r, ctx)                           # ground opens
                res["verdicts"] += [v["verdict"] for v in (r.secure or {}).get("verdicts", [])]
                if r.valid and msg in r.messages:
                    ok = True
                    break
        if ok:
            break
    res.update(decoded=int(ok), status="decoded" if ok else ("no_decode" if not res["verdicts"]
                                                              else "opened_no_message"))
    res["pass"] = ok or bool(case.get("record_only")) or case["mod"] in REPLAY_LIMITED
    flag = "ok  " if ok else ("rec " if res["pass"] else "MISS")
    print(f"  {case['id']:<22} {flag} {res['status']:<18} {','.join(res['verdicts'][:4])}", flush=True)
    return res


def replay_status(ctx, key_source):
    from dpmg.secure import api as sf
    env = sf.probe(run_selftest=True)
    st = ctx.status()
    res = {"id": "SEC-10", "plan": "SEC-10", "ground": {"key_id": st["keys"]["key_id"], "kcv": st["keys"]["kcv"],
                                                        "suite": st["on_air_suite"], "profile": st["profile"],
                                                        "aead": st["aead"], "overhead_bits": st["overhead_bits"],
                                                        "overhead_bits_long": st["overhead_bits_long"],
                                                        "crypto": env.summary(),
                                                        "selftest": env.selftest_ok, "key_source": key_source},
           "radio": None, "status": "offline (ground context only)"}
    res["pass"] = bool(env.selftest_ok)
    print(f"  SEC-10                 {'ok  ' if res['pass'] else 'FAIL'} ground {st['aead']}, "
          f"+{st['overhead_bits']}/{st['overhead_bits_long']} bits/frame; {env.summary()}", flush=True)
    return res


def replay_replay_case(ctx, rng, work_dir):
    """R1 offline: decode the same looped frame several times: delivered once."""
    from dpmg.scenario import gen_scenario_json as g
    import replay_chain as rc
    from dpmg.secure import api as sf
    case = {"id": "SEC-13", "plan": "SEC-13", "mod": "FSK", "fec": None, "label": "none"}
    msg = "SEC REPLAY R1"
    spec = _spec_for(case, msg, ctx)
    g.build_scenario(spec, work_dir)
    iq, plan, wl = rc.load_scenario(work_dir, spec["name"])
    ch = wl[0][0]
    fs = float(plan.sample_rate_hz)
    _fo, decim = rc.worker_fs_out(wl, fs)
    first, replays = 0, 0
    for i in range(4):
        base = (np.roll(np.tile(iq, 2), int(rng.integers(0, len(iq))))
                * np.exp(1j * rng.uniform(0, 2 * np.pi))).astype(np.complex64)
        bb, fse = rc.channelize_loop(rc.spectrum(base), fs, ch.f_center_hz - plan.center_freq_hz, decim)
        r = rc.decode(bb, ch, fse, {})
        if r.valid and r.sealed:
            r = sf.open_result(r, ctx)
            first += int(r.valid and msg in r.messages)
            replays += sum(1 for v in r.secure["verdicts"] if v["verdict"] == sf.REPLAY)
    res = {"id": "SEC-13", "plan": "SEC-13", "delivered": first, "replays": replays,
           "status": f"delivered {first}, replays rejected {replays}"}
    res["pass"] = first == 1 and replays >= 1
    print(f"  SEC-13                 {'ok  ' if res['pass'] else 'FAIL'} {res['status']}", flush=True)
    return res


def replay_tamper(ctx, rng):
    """SEC-14 offline: single-bit flips over a sealed body never authenticate."""
    body = ctx.seal(b"TAMPER SEC-14")
    leaked = 0
    n = len(body) * 8
    for i in range(n):
        bad = bytearray(body)
        bad[i // 8] ^= 1 << (i % 8)
        leaked += int(ctx.open(bytes(bad)).ok)
    trunc = ctx.open(body[:-1]).ok or ctx.open(body + b"\x00").ok
    res = {"id": "SEC-14", "plan": "SEC-14", "flips": n, "accepted": leaked, "truncation_accepted": bool(trunc),
           "status": f"{n} single-bit flips, {leaked} accepted; truncation/extension accepted: {trunc}"}
    res["pass"] = leaked == 0 and not trunc
    print(f"  SEC-14                 {'ok  ' if res['pass'] else 'FAIL'} {res['status']}", flush=True)
    return res


# hardware
def wait_sealed(link, ctx, expected, timeout, hold_s=0.0):
    """Read SEALED_FRAME datagrams off the bench link socket, open them on the
    ground, return (ok, seconds, n_frames, verdicts). hold_s > 0 keeps
    collecting after the first match (SEC-13 replay counting)."""
    from dpmg.link import proto
    from dpmg.link.proto import MsgType
    link._drain()
    t0 = time.time()
    ok, t_ok, n, verdicts = False, None, 0, []
    deadline = t0 + timeout
    while time.time() < deadline:
        try:
            data, _ = link.s.recvfrom(65535)
        except socket.timeout:
            continue
        try:
            mt, _seq, msg = proto.decode(data)
        except proto.ProtoError:
            continue
        if mt != MsgType.SEALED_FRAME:
            continue
        n += 1
        r = ctx.open(msg.body)
        verdicts.append(r.verdict)
        if r.ok and r.plaintext.decode("utf-8", errors="replace") == expected and not ok:
            ok, t_ok = True, round(time.time() - t0, 1)
            deadline = time.time() + hold_s
    return ok, t_ok, n, verdicts


def hw_status(link, ctx, key_source):
    from dpmg.link.proto import CmdStatus
    from dpmg.secure import api as sf
    a = cc.safe_cmd(link, "secure_status", {}, timeout=20)
    env = sf.probe(run_selftest=True)
    st = ctx.status()
    res = {"id": "SEC-10", "plan": "SEC-10", "status": "",
           "ground": {"key_id": st["keys"]["key_id"], "kcv": st["keys"]["kcv"], "suite": st["on_air_suite"],
                      "profile": st["profile"], "aead": st["aead"], "overhead_bits": st["overhead_bits"],
                      "overhead_bits_long": st["overhead_bits_long"], "crypto": env.summary(),
                      "selftest": env.selftest_ok, "key_source": key_source}}
    if a.status != CmdStatus.OK:
        res.update(status=f"secure_status refused: {cc.ack_err(a)}", radio=None)
        res["pass"] = False
    else:
        r = a.result
        b = r.get("brief", {})
        cr = r.get("crypto", {})
        res["radio"] = {"flag": bool(r.get("flag")), "keys_on_radio": bool(r.get("keys_on_radio")),
                        "crypto": f"{cr.get('library')} {cr.get('version')} on {cr.get('backend')}",
                        "aead": r.get("aead"),
                        "sealed_seen": b.get("sealed_seen"), "sealed_forwarded": b.get("sealed_forwarded")}
        res["status"] = ("radio forwarding (no keys)" if r.get("flag") else
                         "radio flag OFF: set DPMG_SECURE_MSG=1 in dpmg_svc")
        res["pass"] = bool(r.get("flag")) and not r.get("keys_on_radio") and bool(env.selftest_ok)
    print(f"  SEC-10                 {'ok  ' if res['pass'] else 'FAIL'} {res['status']}; ground {st['aead']} "
          f"+{st['overhead_bits']}/{st['overhead_bits_long']} b/frame", flush=True)
    return res


def hw_roundtrip(link, ctx, case, timeout, settle, screen):
    from dpmg.link.proto import CmdStatus
    res = {"id": case["id"], "plan": case["plan"], "mod": case["mod"], "fec": case["label"],
           "decoded": 0, "want": 1, "status": "", "record_only": bool(case.get("record_only"))}
    if screen is not None and screen.get(case["id"]) == 0 and case["mod"] not in REPLAY_LIMITED:
        res.update(status="skipped_replay")
        res["pass"] = bool(case.get("record_only"))
        return res
    msg = _message(case, ctx)
    res["message"] = msg
    spec = _spec_for(case, msg, ctx)
    g = cc.safe_cmd(link, "gen_scenario", {"spec": spec, "load_tx": True}, timeout=120)
    if g.status != CmdStatus.OK:
        res.update(status=f"build_fail: {cc.ack_err(g)}")
        res["pass"] = bool(case.get("record_only"))
        print(f"  {case['id']:<22} BUILD FAIL {res['status']}", flush=True)
        return res
    cc.safe_cmd(link, "set_plan", {"plan_path": g.result["plan_path"]}, timeout=30)
    time.sleep(settle)
    cc.safe_cmd(link, "start_tx", {"mode": "CONTINUOUS"}, timeout=30)
    ok, dt, n, verdicts = wait_sealed(link, ctx, msg, timeout)
    cc.safe_cmd(link, "stop_tx", {}, timeout=30)
    res.update(decoded=int(ok), t_s=dt, sealed_frames=n, verdicts=verdicts[:8],
               status="decoded" if ok else ("no_sealed_frame" if n == 0 else "opened_no_message"))
    res["pass"] = bool(ok) or bool(case.get("record_only"))
    print(f"  {case['id']:<22} {'ok  ' if ok else ('rec ' if res['pass'] else 'MISS')} {res['status']}"
          f"{'' if dt is None else f'  {dt:.1f} s'}  frames {n}", flush=True)
    return res


def hw_replay_case(link, ctx, timeout, settle):
    """SEC-13 on the radio: key a looped sealed frame and keep listening ~10 s;
    the ground delivers the first copy and counts every repeat as REPLAY."""
    from dpmg.link.proto import CmdStatus
    from dpmg.secure import api as sf
    case = {"id": "SEC-13", "plan": "SEC-13", "mod": "FSK", "fec": None, "label": "none"}
    msg = "SEC REPLAY R1"
    res = {"id": "SEC-13", "plan": "SEC-13", "status": ""}
    spec = _spec_for(case, msg, ctx)
    g = cc.safe_cmd(link, "gen_scenario", {"spec": spec, "load_tx": True}, timeout=120)
    if g.status != CmdStatus.OK:
        res.update(status=f"build_fail: {cc.ack_err(g)}")
        res["pass"] = False
        return res
    cc.safe_cmd(link, "set_plan", {"plan_path": g.result["plan_path"]}, timeout=30)
    time.sleep(settle)
    c0 = dict(ctx.counters)
    cc.safe_cmd(link, "start_tx", {"mode": "CONTINUOUS"}, timeout=30)
    ok, dt, n, verdicts = wait_sealed(link, ctx, msg, timeout, hold_s=10.0)
    cc.safe_cmd(link, "stop_tx", {}, timeout=30)
    d_ok = ctx.counters[sf.AUTH_OK] - c0[sf.AUTH_OK]
    d_rep = ctx.counters[sf.REPLAY] - c0[sf.REPLAY]
    res.update(first_decode=bool(ok), sealed_frames=n, auth_ok_delta=d_ok, replay_delta=d_rep,
               status=f"first decode {'yes' if ok else 'NO'}; frames {n}; auth_ok +{d_ok}; replay rejected +{d_rep}")
    res["pass"] = bool(ok) and d_ok == 1 and d_rep >= 1
    print(f"  SEC-13                 {'ok  ' if res['pass'] else 'FAIL'} {res['status']}", flush=True)
    return res


# summary / report
def summarize(results):
    out = {}
    for pid in ("SEC-10", "SEC-11", "SEC-12", "SEC-13", "SEC-14"):
        rs = [r for r in results if r.get("plan") == pid]
        if not rs:
            continue
        scored = [r for r in rs if not r.get("record_only") and not str(r.get("status", "")).startswith("skipped")]
        ok = sum(1 for r in scored if r.get("pass"))
        # a plan with only recorded cells (SEC-14 on the radio is offline-only) is
        # recorded, not failed: it does not set the exit code
        rec_only = not scored and all(r.get("record_only") for r in rs)
        out[pid] = {"n": len(scored), "ok": ok, "pass": rec_only or (bool(scored) and ok == len(scored)),
                    "record_only": rec_only,
                    "recorded": [r["id"] for r in rs if r.get("record_only")],
                    "detail": "; ".join(f"{r['id'].split('/')[-1]}:{r.get('status', '')}" for r in rs)[:600]}
    return out


def markdown(meta, results, cases):
    replay = meta.get("mode") == "replay"
    lines = [f"# Secure messages SEC-10..SEC-14 -- {'OFFLINE REPLAY' if replay else meta.get('serial', '')}", "",
             f"{meta['date']}  {('Es/N0 ' + str(ESN0_DB) + ' dB') if replay else meta.get('loopback', '')}",
             f"keys on the ground ({meta.get('key_source', '?')}); radio holds no keys", ""]
    for pid, c in cases.items():
        verdict = "RECORDED" if c.get("record_only") else ("PASS" if c["pass"] else "FAIL")
        lines.append(f"- **{pid}**: {c['ok']}/{c['n']} {verdict}"
                     + (f" (recorded: {', '.join(c['recorded'])})" if c["recorded"] else ""))
    lines.append("")
    for r in results:
        lines.append(f"  - {r['id']}: {r.get('status', '')}")
    lines += ["", "SEC-14 (tamper) is offline-authoritative; on the radio SEC-13 uses the looped "
              "scenario itself as the replayer (every repeat of the sealed frame must be rejected on the ground)."]
    return "\n".join(lines) + "\n"


def _load_replay(path):
    j = json.load(open(path))
    return {r["id"]: (1 if r.get("decoded") else 0) for r in j.get("results", []) if "decoded" in r}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=os.environ.get("DPMG_G40_HOST"), required=not os.environ.get("DPMG_G40_HOST"))
    ap.add_argument("--port", type=int, default=47810)
    ap.add_argument("--arm", action="store_true")
    ap.add_argument("--replay", action="store_true")
    ap.add_argument("--replay-json", default=None, dest="replay_json")
    ap.add_argument("--mods", default="", help="comma list of SEC-12 modulations ('' = all, 'none' = skip SEC-12)")
    ap.add_argument("--seed", type=int, default=0x5EC)
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
    ap.add_argument("--out", default=os.path.join(RESULTS_DIR, "SECURE_MSG_RESULTS.json"))
    cc.add_clock_args(ap)
    args = ap.parse_args()

    mods = None if not args.mods else ([] if args.mods == "none" else [m.strip() for m in args.mods.split(",")])
    C = build_cases(mods)
    if args.estimate:
        keyed = [c for c in C if c["kind"] in ("roundtrip", "replay")]
        print(f"[estimate] {len(keyed)} keyed cases x ~{T_CASE_S:g} s + SEC-13 hold 10 s = "
              f"~{(len(keyed) * T_CASE_S + 10) / 60:.0f} min", flush=True)
        return 0

    prior = []
    if args.resume and os.path.exists(args.out):
        try:
            prior = json.load(open(args.out)).get("results", [])
        except Exception:                                            # noqa: BLE001
            pass
    done = {r["id"] for r in prior}
    results = list(prior)
    t0 = time.time()
    with tempfile.TemporaryDirectory() as d:
        ctx, key_source = ground_setup(d)
        from dpmg.secure import api as sf
        if args.replay:
            rng = np.random.default_rng(args.seed)
            for c in C:
                if args.resume and c["id"] in done:
                    continue
                if c["kind"] == "status":
                    results.append(replay_status(ctx, key_source))
                elif c["kind"] == "roundtrip":
                    results.append(replay_roundtrip(c, rng, d, ctx))
                elif c["kind"] == "replay":
                    results.append(replay_replay_case(ctx, rng, d))
                elif c["kind"] == "tamper":
                    results.append(replay_tamper(ctx, rng))
            meta = {"test": "secure_msg", "mode": "replay", "date": cc.iso_now(), "esn0_db": ESN0_DB,
                    "seed": args.seed, "key_source": key_source, "aead": sf.AEAD_NAME, "nonce": sf.NONCE_NAME,
                    "overhead_bits": ctx.status()["overhead_bits"], "elapsed_s": round(time.time() - t0, 1)}
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
                st = hw_status(link, ctx, key_source)
                results.append(st)
                if not (st.get("radio") or {}).get("flag"):
                    print("[secure_msg] the radio is not parsing sealed frames: set DPMG_SECURE_MSG=1 in the "
                          "dpmg_svc environment (no key file needed on the radio)",
                          flush=True)
                else:
                    for c in C:
                        if args.resume and c["id"] in done:
                            continue
                        if c["kind"] == "roundtrip":
                            results.append(hw_roundtrip(link, ctx, c, args.timeout, args.settle, screen))
                        elif c["kind"] == "replay":
                            results.append(hw_replay_case(link, ctx, args.timeout, args.settle))
                        elif c["kind"] == "tamper":
                            results.append({"id": "SEC-14", "plan": "SEC-14", "record_only": True, "pass": True,
                                            "status": "offline_only(tests/test_secure_frame.py bit flips)"})
            finally:
                if not args.no_cleanup:
                    cc.teardown(link)
            meta = {"test": "secure_msg", "mode": "hardware", "date": cc.iso_now(), "host": args.host,
                    "serial": link.serial, "rx_gain": args.gain, "tx_atten": args.atten, "pad_db": args.pad_db,
                    "loopback": f"TX {link.tx_conn} --[{args.pad_db:g} dB]--> RX {link.rx_conn}",
                    "gps": cc.gps_summary(link, args.clock), "key_source": key_source, "aead": sf.AEAD_NAME, "nonce": sf.NONCE_NAME,
                    "overhead_bits": ctx.status()["overhead_bits"], "elapsed_s": round(time.time() - t0, 1)}
        sf.set_context(None)

    cases = summarize(results)
    md = markdown(meta, results, cases)
    cc.write_results(args.out, dict(meta, cases=cases, results=results), md)
    print("\n" + md, flush=True)
    print(f"[saved] {args.out}", flush=True)
    return 0 if all(c["pass"] for c in cases.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
