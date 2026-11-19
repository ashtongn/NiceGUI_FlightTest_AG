#!/usr/bin/env python3
"""
fec_frame_opts.py: FEC-06 (frame options) and FEC-04 (residual FER),
offline (`--replay`) through
the gen_scenario then AWGN then GPU-model channelizer then decode_pool chain, then on
the G40 (Setup A).

FEC-06: frame options, pass bar "decode; wrong-CRC frames are rejected, not
shown". Each case is one FSK 4800 signal whose scenario `frame` dict varies one
option; the known message must decode, and the negatives must not:
  sync bits          default sync vs an alternate 16-bit sync word
  max_sync_errors    0 (exact) and 2 (tolerant); + a 1-bit-flipped sync that
                     decodes at tolerance 2 and is rejected at tolerance 0
  has_length         on (8-bit length byte) and off (fixed payload_len_bits)
  CRC presets        CRC-16-CCITT, CRC-32, CRC-9 round-trip; a corrupted-CRC
                     frame is rejected (negative, offline)

FEC-04: residual error, pass bar "post-FEC FER <= 1 % at threshold + 3 dB".
Offline: for conv 1/2 on FSK 4800 the decode threshold Es/N0 is found (lowest
step that still decodes), then --fer-frames independent frames are impaired at
threshold + 3 dB and the frame-error rate is the fraction that fail. The
threshold is measured in the same replay chain, so the +3 dB point is self-
consistent. On the radio this is the snr_sweep digital-backoff path at
threshold + 3 dB (reported; this harness runs the offline FER).

  python3 bench/fec_frame_opts.py --replay --out bench/RESULTS_<date>/fec_frame_opts_replay.json
  python3 bench/fec_frame_opts.py --host 192.168.0.22 --arm --pad-db 50 \\
      --replay-json <replay>.json --out ...fec_frame_opts.json
  python3 bench/fec_frame_opts.py --estimate

Nothing transmits without --arm. --resume keeps cases already complete in --out.
The negative (reject) cases and FEC-04 are offline-authoritative (the radio
builds only valid frames); they are reported, not keyed.
"""
from __future__ import annotations

import argparse
import json
import math
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

import numpy as np                                                  # noqa: E402

from dpmg.framing import frame_codec as fc                                            # noqa: E402
import checkout_common as cc                                        # noqa: E402

CENTER = cc.CENTER
SR = 4800
SPACING = 5000
OFF = 300_000.0
ALT_SYNC = "1100101011110000"
T_CASE_S = 5.5


def _frame(sync=None, max_err=2, has_len=True, crc="CRC-16-CCITT", plen=0):
    f = {"sync_bits": sync or fc.SYNC_BITS, "max_sync_errors": int(max_err),
         "has_length": bool(has_len), "crc": crc}
    if not has_len:
        f["payload_len_bits"] = int(plen)
    return f


def _plen_bits(msg):
    return len(msg.encode("utf-8")) * 8


def build_cases():
    """FEC-06 decode cases (keyed) + negative/record cases (offline)."""
    C = []
    # sync word
    C.append({"id": "FEC-06/sync-default", "plan": "FEC-06",
              "msg": "FRAME sync default", "frame": _frame()})
    C.append({"id": "FEC-06/sync-alt", "plan": "FEC-06",
              "msg": "FRAME sync alt", "frame": _frame(sync=ALT_SYNC)})
    # max_sync_errors
    C.append({"id": "FEC-06/syncerr-0", "plan": "FEC-06",
              "msg": "FRAME exact sync", "frame": _frame(max_err=0)})
    C.append({"id": "FEC-06/syncerr-2", "plan": "FEC-06",
              "msg": "FRAME tol sync", "frame": _frame(max_err=2)})
    # has_length
    C.append({"id": "FEC-06/haslen-on", "plan": "FEC-06",
              "msg": "FRAME len on", "frame": _frame(has_len=True)})
    _m = "FRAME len off"
    C.append({"id": "FEC-06/haslen-off", "plan": "FEC-06",
              "msg": _m, "frame": _frame(has_len=False, plen=_plen_bits(_m))})
    # CRC presets
    for crc in ("CRC-16-CCITT", "CRC-32", "CRC-9"):
        C.append({"id": f"FEC-06/crc-{crc}", "plan": "FEC-06",
                  "msg": f"FRAME {crc}", "frame": _frame(crc=crc)})
    # negatives (offline-authoritative): must be rejected
    C.append({"id": "FEC-06/neg-badcrc", "plan": "FEC-06", "msg": "FRAME badcrc",
              "frame": _frame(), "negative": "flip_crc"})
    C.append({"id": "FEC-06/neg-syncerr0-flip", "plan": "FEC-06", "msg": "FRAME exactflip",
              "frame": _frame(max_err=0), "negative": "flip_sync1"})
    return C


# build one FSK frame scenario, decode it offline
def _build(msg, frame, fec=None, out_dir=None):
    from dpmg.scenario import gen_scenario_json as g
    import replay_chain as rc
    sig = {"modulation": "FSK", "symbol_rate": SR, "tone_spacing_hz": SPACING,
           "offset_hz": OFF, "amplitude": 0.9, "frame": dict(frame)}
    if fec:
        # conv/block FEC frame: the modem builds it from the message (encode_fec)
        sig["message"] = msg
        sig["fec"] = dict(fec)
    else:
        # Gen_scenario's message path always emits the default frame
        # (encode_message: fixed sync + 8-bit len + CRC-16). To actually vary the
        # sync word / has_length / CRC preset on air, pre-build the exact frame
        # bits with the declared FrameSpec and ship them as raw `bits` (no extra
        # framing): the same path the GUI compose uses with frame.auto_wrap.
        spec = fc.FrameSpec.from_entry(frame)
        payload_bits = fc._bytes_to_bits(msg.encode("utf-8"))
        sig["bits"] = fc.build_frame(payload_bits, spec)
    name = "ffo_" + str(abs(hash((msg, json.dumps(frame, sort_keys=True), json.dumps(fec)))) % 10**8)
    spec = {"name": name, "sample_rate_hz": cc.RATE_NARROW, "center_freq_hz": CENTER,
            "loop_s": 0.2, "signals": [sig]}
    g.build_scenario(spec, out_dir)
    iq, plan, wl = rc.load_scenario(out_dir, name)
    return iq, plan, wl[0][0], wl[0][1]


def _decode_once(iq, plan, cc_, esn0_db, rng):
    import replay_chain as rc
    fs = float(plan.sample_rate_hz)
    _fo, decim = rc.worker_fs_out([(cc_, None)], fs)
    ps = rc.burst_power(iq)
    n0 = ps / (float(cc_.symbol_rate) * 10 ** (esn0_db / 10.0)) if esn0_db < 90 else 0.0
    x = np.roll(np.tile(iq, 2), int(rng.integers(0, len(iq)))) * np.exp(1j * rng.uniform(0, 2 * np.pi))
    if n0 > 0:
        x = rc.awgn(x, n0, fs, rng)
    bb, fse = rc.channelize_loop(rc.spectrum(x), fs, cc_.f_center_hz - plan.center_freq_hz, decim)
    return rc.decode(bb, cc_, fse.real if hasattr(fse, "real") else fse)


def replay_fec06(case, rng, work_dir, esn0_db=30.0):
    res = {"id": case["id"], "plan": "FEC-06", "msg": case["msg"],
           "negative": case.get("negative", ""), "status": "", "decoded": 0, "want": 1}
    iq, plan, cc_, entry = _build(case["msg"], case["frame"], out_dir=work_dir)
    neg = case.get("negative")
    if not neg:
        hit = 0
        for _ in range(3):
            r = _decode_once(iq, plan, cc_, esn0_db, rng)
            hit += bool(r.valid and case["msg"] in (r.messages or [r.message]))
        res.update(decoded=hit, want=1, status="decoded" if hit else "no_decode")
        res["pass"] = hit > 0
        print(f"  {case['id']:<26} {'ok' if hit else 'MISS':<4} decode {hit}/3", flush=True)
        return res
    # negative: build a valid frame's bits, corrupt, confirm find_frame rejects
    from dpmg.framing import frame_codec as _fc
    bits = _fc.encode_message(case["msg"].encode("utf-8"))
    spec = _fc.FrameSpec.from_entry(case["frame"])
    if neg == "flip_crc":
        b = list(bits); b[-1] = "1" if b[-1] == "0" else "0"; bad = "".join(b)
        rejected = not _fc.find_frame("01" * 20 + bad, spec, max_search=4096).valid
    elif neg == "flip_sync1":
        # one sync-bit flip: rejected at max_sync_errors 0 (this case), accepted at 2
        sb = list(spec.sync_bits); sb[3] = "1" if sb[3] == "0" else "0"
        payload = bits[len(spec.sync_bits):]
        flipped = "".join(sb) + payload
        rej0 = not _fc.find_frame(flipped, spec, max_search=4096).valid
        spec2 = _fc.FrameSpec.from_entry(_frame(max_err=2))
        acc2 = _fc.find_frame(flipped, spec2, max_search=4096).valid
        rejected = rej0 and acc2
    else:
        rejected = False
    res.update(decoded=int(rejected), want=1, status="rejected" if rejected else "LEAKED",
               record_only=True)
    res["pass"] = bool(rejected)
    print(f"  {case['id']:<26} {'ok' if rejected else 'LEAK':<4} negative {'rejected' if rejected else 'LEAKED'}", flush=True)
    return res


# FEC-04 residual FER
def replay_fec04(rng, work_dir, fer_frames=200, rate="1/2"):
    """Find the conv-1/2 FSK decode threshold, then measure post-FEC FER at
    threshold + 3 dB over fer_frames independent impairments."""
    fec = {"type": "conv", "rate": rate}
    iq, plan, cc_, entry = _build("FEC04 FER", _frame(), fec=fec, out_dir=work_dir)
    msg = "FEC04 FER"
    # threshold: lowest 1 dB step from 14 down to 2 that still decodes >= 3/4
    thr = None
    for esn0 in range(14, 1, -1):
        hits = sum(bool((r := _decode_once(iq, plan, cc_, float(esn0), rng)).valid
                        and msg in (r.messages or [r.message])) for _ in range(4))
        if hits >= 3:
            thr = esn0
        else:
            if thr is not None:
                break
    if thr is None:
        return {"plan": "FEC-04", "rate": rate, "status": "no threshold found",
                "pass": False, "want": 1, "decoded": 0}
    test_esn0 = thr + 3.0
    fails = 0
    for _ in range(fer_frames):
        r = _decode_once(iq, plan, cc_, test_esn0, rng)
        if not (r.valid and msg in (r.messages or [r.message])):
            fails += 1
    fer = fails / fer_frames
    res = {"plan": "FEC-04", "rate": rate, "threshold_esn0_db": thr,
           "test_esn0_db": test_esn0, "frames": fer_frames, "frame_errors": fails,
           "fer": fer, "pass": fer <= 0.01, "want": 1, "decoded": int(fer <= 0.01),
           "status": f"FER {fer:.3%} at {test_esn0:g} dB (threshold {thr} dB + 3)"}
    print(f"  FEC-04 conv {rate}  threshold {thr} dB, test {test_esn0:g} dB  "
          f"FER {fails}/{fer_frames} = {fer:.3%}  {'PASS' if res['pass'] else 'FAIL'}", flush=True)
    return res


# hardware
def hw_fec06(link, case, timeout, settle, screen):
    from dpmg.link.proto import CmdStatus
    res = {"id": case["id"], "plan": "FEC-06", "msg": case["msg"],
           "negative": case.get("negative", ""), "decoded": 0, "want": 1, "status": ""}
    if case.get("negative"):
        res.update(status="offline_only(negative; the radio builds only valid frames)",
                   record_only=True)
        res["pass"] = True
        print(f"  {case['id']:<26} OFFLINE ONLY (negative)", flush=True)
        return res
    if screen is not None and screen.get(case["id"]) == 0:
        res.update(status="skipped_replay")
        res["pass"] = False
        return res
    from dpmg.scenario import gen_scenario_json  # noqa
    _spec = fc.FrameSpec.from_entry(case["frame"])
    sig = {"modulation": "FSK", "symbol_rate": SR, "tone_spacing_hz": SPACING,
           "offset_hz": OFF, "amplitude": 0.9, "frame": dict(case["frame"]),
           "bits": fc.build_frame(fc._bytes_to_bits(case["msg"].encode("utf-8")), _spec)}
    spec = {"name": "ffo_" + case["id"].split("/")[-1].replace("-", "_"),
            "sample_rate_hz": cc.RATE_NARROW, "center_freq_hz": CENTER, "loop_s": 0.2,
            "signals": [sig]}
    g = cc.safe_cmd(link, "gen_scenario", {"spec": spec, "load_tx": True}, timeout=120)
    if g.status != CmdStatus.OK:
        res.update(status=f"build_fail: {cc.ack_err(g)}")
        res["pass"] = False
        return res
    cc.safe_cmd(link, "set_plan", {"plan_path": g.result["plan_path"]}, timeout=30)
    time.sleep(settle)
    cc.safe_cmd(link, "start_tx", {"mode": "CONTINUOUS"}, timeout=30)
    ok, _dt, _obs, _cnt = link.wait_decode(case["msg"], timeout=timeout)
    cc.safe_cmd(link, "stop_tx", {}, timeout=30)
    res.update(decoded=int(ok), status="decoded" if ok else "no_decode")
    res["pass"] = bool(ok)
    print(f"  {case['id']:<26} {'ok' if ok else 'MISS'}  {'decoded' if ok else 'no_decode'}", flush=True)
    return res


# summary / report
def summarize(fec06, fec04):
    scored06 = [r for r in fec06 if not r.get("record_only")
                and not str(r.get("status", "")).startswith("skipped")]
    ok06 = sum(1 for r in scored06 if r.get("pass"))
    neg = [r for r in fec06 if r.get("record_only")]
    okneg = sum(1 for r in neg if r.get("pass"))
    out = {"FEC-06": {"n": len(scored06), "ok": ok06, "pass": bool(scored06) and ok06 == len(scored06),
                      "negatives_ok": okneg, "negatives": len(neg)}}
    if fec04:
        out["FEC-04"] = {"pass": bool(fec04.get("pass")), "fer": fec04.get("fer"),
                         "status": fec04.get("status")}
    return out


def markdown(meta, fec06, fec04, cases):
    replay = meta.get("mode") == "replay"
    lines = [f"# FEC frame options (FEC-06) + residual FER (FEC-04) -- {'OFFLINE REPLAY' if replay else meta.get('serial','')}",
             "", f"{meta['date']}  {('Es/N0 ' + str(meta.get('esn0_db')) + ' dB (FEC-06 decode)') if replay else meta.get('loopback','')}", ""]
    c6 = cases["FEC-06"]
    lines.append(f"- **FEC-06**: decode {c6['ok']}/{c6['n']} "
                 + ("PASS" if c6["pass"] else "FAIL")
                 + f"; negatives rejected {c6['negatives_ok']}/{c6['negatives']}")
    for r in fec06:
        lines.append(f"  - {r['id']}: {r['status']}")
    if "FEC-04" in cases:
        c4 = cases["FEC-04"]
        lines += ["", f"- **FEC-04**: {c4['status']} -- "
                  + ("PASS (FER <= 1 %)" if c4["pass"] else "FAIL")]
    lines += ["", "Negatives (bad CRC, exact-sync 1-bit flip) and FEC-04 FER are "
              "offline-authoritative (the radio builds only valid frames, and FER needs a "
              "controlled Es/N0 = the snr_sweep digital-backoff path at threshold + 3 dB)."]
    return "\n".join(lines) + "\n"


def _load_replay(path):
    j = json.load(open(path))
    return {r["id"]: (0 if str(r.get("status", "")).startswith("build_fail") else 1)
            for r in j.get("fec06", [])}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=os.environ.get("DPMG_G40_HOST"), required=not os.environ.get("DPMG_G40_HOST"))
    ap.add_argument("--port", type=int, default=47810)
    ap.add_argument("--arm", action="store_true")
    ap.add_argument("--replay", action="store_true")
    ap.add_argument("--replay-esn0", type=float, default=30.0, dest="replay_esn0")
    ap.add_argument("--replay-json", default=None, dest="replay_json")
    ap.add_argument("--fer-frames", type=int, default=200, dest="fer_frames")
    ap.add_argument("--skip-fer", action="store_true", dest="skip_fer",
                    help="skip FEC-04 (the 200-frame FER sweep)")
    ap.add_argument("--seed", type=int, default=0xFEC04)
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
    ap.add_argument("--out", default=os.path.join(RESULTS_DIR, "FEC_FRAME_OPTS_RESULTS.json"))
    cc.add_clock_args(ap)
    args = ap.parse_args()

    C = build_cases()
    if args.estimate:
        keyed = [c for c in C if not c.get("negative")]
        print(f"[estimate] FEC-06 {len(keyed)} keyed cases x ~{T_CASE_S:g} s = "
              f"~{len(keyed) * T_CASE_S / 60:.0f} min; FEC-04 + negatives offline", flush=True)
        return 0

    prior06, prior04 = [], None
    if args.resume and os.path.exists(args.out):
        try:
            j = json.load(open(args.out)); prior06 = j.get("fec06", []); prior04 = j.get("fec04")
        except Exception:
            pass
    done = {r["id"] for r in prior06}
    fec06 = list(prior06)
    fec04 = prior04

    t0 = time.time()
    if args.replay:
        rng = np.random.default_rng(args.seed)
        with tempfile.TemporaryDirectory() as d:
            for c in C:
                if args.resume and c["id"] in done:
                    continue
                fec06.append(replay_fec06(c, rng, d, args.replay_esn0))
            if not args.skip_fer and fec04 is None:
                fec04 = replay_fec04(rng, d, args.fer_frames)
        meta = {"test": "fec_frame_opts", "mode": "replay", "date": cc.iso_now(),
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
                if args.resume and c["id"] in done:
                    continue
                fec06.append(hw_fec06(link, c, args.timeout, args.settle, screen))
        finally:
            if not args.no_cleanup:
                cc.teardown(link)
        if not args.skip_fer and fec04 is None:
            print("[FEC-04] FER is offline-authoritative (controlled Es/N0); "
                  "run --replay for it or snr_sweep at threshold+3 dB on the radio", flush=True)
        meta = {"test": "fec_frame_opts", "mode": "hardware", "date": cc.iso_now(),
                "host": args.host, "serial": link.serial, "rx_gain": args.gain,
                "tx_atten": args.atten, "pad_db": args.pad_db,
                "loopback": f"TX {link.tx_conn} --[{args.pad_db:g} dB]--> RX {link.rx_conn}",
                "gps": cc.gps_summary(link, args.clock), "elapsed_s": round(time.time() - t0, 1)}

    cases = summarize(fec06, fec04)
    md = markdown(meta, fec06, fec04, cases)
    cc.write_results(args.out, dict(meta, cases=cases, fec06=fec06, fec04=fec04), md)
    print("\n" + md, flush=True)
    print(f"[saved] {args.out}", flush=True)
    bad = (not cases["FEC-06"]["pass"]) or (cases.get("FEC-04") and not cases["FEC-04"]["pass"])
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
