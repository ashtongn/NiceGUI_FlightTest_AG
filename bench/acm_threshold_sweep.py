#!/usr/bin/env python3
"""
acm_threshold_sweep.py: measure the Es/N0 switching thresholds of the ACM
MODCOD ladder (dpmg/fec/acm.py) through DPMG's real decode path.

For every rung of acm.ACM_TABLE (BPSK 1/2 .. 32APSK 7/8, Tier-1 conv FEC):

  offline (--replay, default)
    build the mod x conv cell exactly as the bench does
    (bench/mod_fec_matrix.cell_spec: gen_scenario_json at 1.92 MSps),
    add AWGN over an Es/N0 ladder in 0.5 dB steps (Es/N0 at the channel symbol
    rate), channelize as the GPU does and decode with
    decode_pool._default_decode (bench/replay_chain), --windows decode windows
    per point, each with fresh noise on a warm lock. FER = 1 - windows that
    returned the exact message / windows. Also pools the pre-FEC channel BER.

  hardware (--hw, the first_checkout "acm_ladder" stage)
    the same cells keyed on Setup A (TX TRx1B then pad then RX TRx1A, gpsdo with
    the antenna bias on and lock confirmed before keying), the Es/N0 ladder
    made with the TX attenuator (+ digital backoff of the scenario amplitude
    past the attenuator ceiling), Es/N0 read from a capture_iq of each step
    (signal power over the measured noise floor in the channel), decode
    success counted over --windows-hw keyings' worth of decode windows.

Threshold = lowest Es/N0 at which FER <= --target-fer (default 1e-2) holds there
and at the next two 0.5 dB steps, measured with --windows windows per point in
a fine pass that starts at the coarse edge (monotone envelope of the coarse
pass otherwise). The suggested switching value
is that threshold + --margin-db (default 1.0 dB, on top of AcmController's own
margin + hysteresis). With W windows per point the smallest non-zero FER is
1/W, so FER <= 1e-2 needs W >= 100 to be resolved (the default 100 near the
edge; a coarse pass with --coarse-windows finds the edge first).

  python3 bench/acm_threshold_sweep.py --replay --out bench/RESULTS_<date>/acm_thresholds_replay.json
  python3 bench/acm_threshold_sweep.py --hw --arm --clock gpsdo --pad-db 50 \\
      --out bench/RESULTS_<date>/acm_ladder.json
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import numpy as np                                                  # noqa: E402

from dpmg.framing import frame_codec                                                  # noqa: E402
import checkout_common as cc                                        # noqa: E402
from dpmg.deploy_config import RADIO_CODE_DIR                       # noqa: E402
from dpmg.fec import acm                                            # noqa: E402

DEFAULT_OUT = os.path.join(HERE, "ACM_THRESHOLDS.json")
TARGET_FER = 1e-2


def rungs(names=None):
    out = [mc for mc in acm.ACM_TABLE if mc.fec_family == "conv"]
    if names:
        want = set(names)
        out = [mc for mc in out if mc.name in want]
    return out


def message_for(mc):
    return f"ACM {mc.name}"[:40]


def build_cell(mc, wd):
    """(loop iq, plan, channel, fs, decim, message, on-air ref bits)."""
    from dpmg.scenario import gen_scenario_json as g
    import replay_chain as rc
    import mod_fec_matrix as mfm
    msg = message_for(mc)
    name = "acm_" + mfm._slug(mc.name)
    spec = mfm.cell_spec(mc.modulation, mc.rate, msg, cc.CENTER, name=name)
    g.build_scenario(dict(spec, sample_rate_hz=float(cc.RATE_NARROW)), wd)
    iq, plan, wl = rc.load_scenario(wd, name)
    ch = wl[0][0]
    fs = float(plan.sample_rate_hz)
    _fo, decim = rc.worker_fs_out(wl, fs)
    ref = mfm.onair_bits(msg, mc.rate)
    return {"iq": iq, "plan": plan, "ch": ch, "fs": fs, "decim": decim, "message": msg,
            "ref": ref, "spec": spec, "modulation": mc.modulation}


# The QPSK-family modulators take symbol_rate as the bit rate (two bits per
# symbol on air), the others as the symbol rate; Es/N0 here is per channel
# symbol, so the noise density uses the on-air symbol rate.
_BITS_PER_PLAN_SYMBOL = {"QPSK": 2, "pi/4-QPSK": 2, "DQPSK": 2, "OQPSK": 2}


def channel_symbol_rate(modulation, plan_rate):
    return float(plan_rate) / _BITS_PER_PLAN_SYMBOL.get(modulation, 1)


def replay_point(cell, esn0_db, windows, rng, lock, base):
    """FER + pre-FEC BER at one Es/N0 over `windows` fresh-noise windows."""
    import replay_chain as rc
    ch, fs, decim = cell["ch"], cell["fs"], cell["decim"]
    ps = rc.burst_power(cell["iq"])
    n0 = ps / (channel_symbol_rate(cell["modulation"], ch.symbol_rate) * 10 ** (esn0_db / 10.0))
    ok = 0
    e = nb = 0
    for _ in range(windows):
        x = rc.awgn(base, n0, fs, rng)
        bb, fse = rc.channelize_loop(rc.spectrum(x), fs,
                                     ch.f_center_hz - cell["plan"].center_freq_hz, decim)
        r = rc.decode(bb, ch, fse, lock)
        if r.valid and cell["message"] in (r.messages or [r.message]):
            ok += 1
        b = rc.raw_ber(r.bit_string, cell["ref"], frame_codec.SYNC_BITS)
        if b is not None:
            e += b[0]
            nb += b[1]
    return {"esn0_db": round(esn0_db, 2), "windows": windows, "decoded": ok,
            "fer": 1.0 - ok / windows, "raw_ber": (e / nb) if nb else None,
            "raw_bits": nb}


def threshold(points, target):
    """Lowest Es/N0 from which every higher point meets FER <= target."""
    pts = sorted(points, key=lambda p: p["esn0_db"])
    thr = None
    for p in reversed(pts):
        if p["fer"] <= target:
            thr = p["esn0_db"]
        else:
            break
    return thr


def replay_rung(mc, args, rng, wd):
    import replay_chain as rc
    t0 = time.time()
    cell = build_cell(mc, wd)
    base = (np.roll(np.tile(cell["iq"], 2), int(rng.integers(0, len(cell["iq"]))))
            * np.exp(1j * rng.uniform(0, 2 * np.pi))).astype(np.complex64)
    lock = {}
    # warm the per-channel lock once on a noise-free copy (as mod_fec_matrix
    # --replay does: the radio's lock is warm long before a ladder step)
    bb0, fse0 = rc.channelize_loop(rc.spectrum(base), cell["fs"],
                                   cell["ch"].f_center_hz - cell["plan"].center_freq_hz,
                                   cell["decim"])
    rc.decode(bb0, cell["ch"], fse0, lock)
    hi = args.start_db if args.start_db is not None else mc.req_esn0_db + args.above_db
    lo = mc.req_esn0_db - args.below_db
    ladder = [round(hi - 0.5 * i, 2) for i in range(int(round((hi - lo) / 0.5)) + 1)]
    points = []
    dead = 0
    # coarse pass high: low, stop two points after the curve hits FER 1
    for es in ladder:
        p = replay_point(cell, es, args.coarse_windows, rng, lock, base)
        p["pass"] = "coarse"
        points.append(p)
        print(f"  {mc.name:<14} Es/N0 {es:5.1f}  FER {p['fer']:.3f} ({p['decoded']}/{p['windows']})"
              + (f"  rawBER {p['raw_ber']:.2e}" if p["raw_ber"] is not None else ""), flush=True)
        dead = dead + 1 if p["decoded"] == 0 else 0
        if dead >= 2:
            break
    # fine pass from the coarse edge upward with the full window count: the
    # threshold is where FER <= target holds at that point and the next two
    # 0.5 dB steps above it (a single clean point can be luck at 1/W)
    fine = []
    t = threshold(points, 0.0)
    if t is not None and args.windows > args.coarse_windows:
        es = t - 0.5
        ok_run = 0
        while es <= hi + 1e-9 and ok_run < 3:
            p = replay_point(cell, es, args.windows, rng, lock, base)
            p["pass"] = "fine"
            fine.append(p)
            ok_run = ok_run + 1 if p["fer"] <= args.target_fer else 0
            print(f"  {mc.name:<14} Es/N0 {es:5.1f}  FER {p['fer']:.4f} ({p['decoded']}/{p['windows']})  [fine]",
                  flush=True)
            es = round(es + 0.5, 2)
        points = [q for q in points if q["esn0_db"] not in {f["esn0_db"] for f in fine}] + fine
    if fine:
        thr = None
        run = 0
        for p in sorted(fine, key=lambda q: q["esn0_db"]):
            if p["fer"] <= args.target_fer:
                run += 1
                if run == 1:
                    cand = p["esn0_db"]
                if run >= 3 or p is sorted(fine, key=lambda q: q["esn0_db"])[-1]:
                    thr = cand
                    break
            else:
                run = 0
        return _rung_result(mc, cell, points, thr, args, t0)
    return _rung_result(mc, cell, points, threshold(points, args.target_fer), args, t0)


def _rung_result(mc, cell, points, thr, args, t0):
    return {"modcod": mc.name, "modulation": mc.modulation, "rate": mc.rate,
            "nominal_db": mc.req_esn0_db, "plan_symbol_rate": float(cell["ch"].symbol_rate),
            "symbol_rate": channel_symbol_rate(mc.modulation, cell["ch"].symbol_rate),
            "points": sorted(points, key=lambda p: p["esn0_db"]),
            "threshold_db": thr,
            "proposed_db": None if thr is None else round(thr + args.margin_db, 1),
            "seconds": round(time.time() - t0, 1)}


# hardware
def hw_rung(link, mc, args):
    """Key one MODCOD over Setup A and walk the TX attenuator down the ladder,
    measuring Es/N0 from a capture and counting decodes at each step."""
    import mod_fec_matrix as mfm
    from dpmg.link.proto import CmdStatus
    msg = message_for(mc)
    name = "acm_" + mfm._slug(mc.name)
    rows = []
    for step_i, (atten, amp) in enumerate(args.hw_steps):
        spec = mfm.cell_spec(mc.modulation, mc.rate, msg, int(args.center), name=name)
        spec["signals"][0]["amplitude"] = amp
        if args.decode_gate_db is not None and args.decode_gate_db >= 0:
            # opt-in per-channel decode gate (as snr_sweep --decode-gate-db): the
            # stock 12 dB slot gate stops submitting the channel below ~19 dB Es/N0
            spec["signals"][0]["decode_gate_db"] = float(args.decode_gate_db)
        g = cc.safe_cmd(link, "gen_scenario", {"spec": spec, "load_tx": True}, timeout=120)
        if g.status != CmdStatus.OK:
            rows.append({"atten": atten, "amp": amp, "status": f"build_fail: {cc.ack_err(g)}"})
            continue
        cc.safe_cmd(link, "set_plan", {"plan_path": g.result["plan_path"]}, timeout=30)
        cc.safe_cmd(link, "set_tx_atten", {"qdb": int(round(atten * 4))}, timeout=30)
        time.sleep(args.settle)
        st = cc.safe_cmd(link, "start_tx", {"mode": "CONTINUOUS"}, timeout=30)
        if st.status != CmdStatus.OK:
            rows.append({"atten": atten, "amp": amp, "status": f"tx_fail: {cc.ack_err(st)}"})
            continue
        try:
            es = _hw_esn0(link, args, spec, mc)
            ok_n, tries = 0, 0
            for _w in range(args.windows_hw):
                ok, _dt, _obs, _cnt = link.wait_decode(msg, timeout=args.window_s)
                tries += 1
                ok_n += int(bool(ok))
        finally:
            cc.safe_cmd(link, "stop_tx", {}, timeout=30)
        rows.append({"atten": atten, "amp": amp, "esn0_db": es, "windows": tries,
                     "decoded": ok_n, "fer": 1.0 - ok_n / max(1, tries)})
        print(f"  {mc.name:<14} atten {atten:5.1f} amp {amp:.3f}  Es/N0 {cc.fnum(es)}  "
              f"FER {rows[-1]['fer']:.2f} ({ok_n}/{tries})", flush=True)
        if ok_n == 0 and len(rows) >= 2 and rows[-2].get("decoded") == 0:
            break
    pts = [{"esn0_db": r["esn0_db"], "fer": r["fer"]} for r in rows if r.get("esn0_db") is not None]
    thr = threshold(pts, args.target_fer) if pts else None
    return {"modcod": mc.name, "modulation": mc.modulation, "rate": mc.rate,
            "nominal_db": mc.req_esn0_db, "steps": rows, "threshold_db": thr}


def _hw_esn0(link, args, spec, mc):
    """Es/N0 of the keyed burst from a capture_iq: power in the channel while
    the burst is on, over the noise density measured off-burst in the same
    channel (needs the scp path to the radio)."""
    import subprocess
    from dpmg.link.proto import CmdStatus
    sig = spec["signals"][0]
    secs = 0.3
    cap = cc.safe_cmd(link, "capture_iq", {"seconds": secs, "name": "acm_esn0"}, timeout=60)
    if cap.status != CmdStatus.OK:
        return None
    rpath = cap.result["path"]
    hpath = rpath.replace(args.svc_root.rstrip("/"), args.radio_code_dir.rstrip("/"), 1)
    loc = os.path.join(tempfile.gettempdir(), "acm_esn0.c64")
    try:
        subprocess.run(["scp", "-q", f"sidekiq@{args.host}:{hpath}", loc], check=True, timeout=120)
        iq = np.fromfile(loc, dtype=np.complex64)
    except Exception:                                                # noqa: BLE001
        return None
    fs = float(cap.result.get("fs") or args.rate)
    return esn0_from_capture(iq, fs, float(sig["offset_hz"]),
                             channel_symbol_rate(mc.modulation, sig["symbol_rate"]))


def esn0_from_capture(iq, fs, offset_hz, symrate):
    """Es/N0 (dB) of a burst in a capture: shift the channel to DC, filter to
    +-1.5 Rs, split on/off by envelope, Es = (P_on - P_off)/Rs, N0 = P_off / B.
    symrate is the on-air symbol rate (channel_symbol_rate)."""
    import scipy.signal as ss
    x = np.asarray(iq, dtype=np.complex128)
    x = x * np.exp(-2j * np.pi * offset_hz / fs * np.arange(x.size))
    sym = float(symrate)
    B = 3.0 * sym
    taps = ss.firwin(257, B / 2, fs=fs)
    y = ss.lfilter(taps, 1.0, x)[257:]
    p = np.abs(y) ** 2
    w = max(64, int(fs / sym) * 4)
    env = np.convolve(p, np.ones(w) / w, mode="same")
    hi = np.percentile(env, 95)
    on = env > 0.5 * hi
    off = env < 0.1 * hi
    if on.sum() < w or off.sum() < w:
        # no gap in the capture: noise from a band beside the channel instead
        z = x * np.exp(-2j * np.pi * (4 * B) / fs * np.arange(x.size))
        p_off = float(np.mean(np.abs(ss.lfilter(taps, 1.0, z)[257:]) ** 2))
        p_on = float(np.mean(p))
    else:
        p_on, p_off = float(np.mean(p[on])), float(np.mean(p[off]))
    n0 = p_off / B
    es = max(p_on - p_off, 1e-30) / sym
    return round(10 * math.log10(es / max(n0, 1e-30)), 2)


def markdown(meta, res):
    lines = [f"# ACM Es/N0 thresholds ({meta['mode']})", "",
             f"{meta['date']}  target FER {meta['target_fer']:g}  margin {meta['margin_db']:g} dB  "
             f"windows/point {meta.get('windows')} (coarse {meta.get('coarse_windows')})", ""]
    rows = []
    for r in res:
        rows.append([r["modcod"], f"{r['nominal_db']:g}", cc.fnum(r.get("threshold_db")),
                     cc.fnum(r.get("proposed_db")),
                     cc.fnum(None if r.get("threshold_db") is None else r["threshold_db"] - r["nominal_db"])])
    lines.append(cc.md_table(["MODCOD", "nominal dB", f"measured (FER<={meta['target_fer']:g}) dB",
                              "proposed dB", "measured - nominal"], rows))
    lines += ["", "FER per Es/N0 point:", ""]
    for r in res:
        pts = r.get("points") or [{"esn0_db": s.get("esn0_db"), "fer": s.get("fer")}
                                  for s in r.get("steps", [])]
        lines.append(f"- {r['modcod']}: " + ", ".join(
            f"{cc.fnum(p['esn0_db'])}:{p['fer']:.3g}" for p in pts if p.get("esn0_db") is not None))
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--replay", action="store_true", help="offline (default when --hw absent)")
    ap.add_argument("--hw", action="store_true", help="key the ladder on Setup A")
    ap.add_argument("--arm", action="store_true")
    ap.add_argument("--modcods", default="", help="comma list of acm.ACM_TABLE names (default all conv rungs)")
    ap.add_argument("--target-fer", type=float, default=TARGET_FER, dest="target_fer")
    ap.add_argument("--margin-db", type=float, default=1.0, dest="margin_db")
    ap.add_argument("--above-db", type=float, default=6.0, dest="above_db",
                    help="ladder starts this far above the nominal threshold")
    ap.add_argument("--below-db", type=float, default=8.0, dest="below_db",
                    help="ladder may run this far below the nominal threshold")
    ap.add_argument("--start-db", type=float, default=None, dest="start_db",
                    help="absolute Es/N0 the ladder starts at (overrides --above-db); the "
                         "decode path's edges sit well above the nominal table")
    ap.add_argument("--coarse-windows", type=int, default=20, dest="coarse_windows")
    ap.add_argument("--windows", type=int, default=100,
                    help="decode windows at the edge points (FER resolution 1/windows)")
    ap.add_argument("--seed", type=int, default=0xAC3)
    # hardware
    ap.add_argument("--host", default=os.environ.get("DPMG_G40_HOST"), required=not os.environ.get("DPMG_G40_HOST"))
    ap.add_argument("--port", type=int, default=47810)
    ap.add_argument("--rate", type=float, default=cc.RATE_NARROW)
    ap.add_argument("--center", type=float, default=cc.CENTER)
    ap.add_argument("--gain", type=int, default=255)
    ap.add_argument("--atten", type=float, default=30)
    ap.add_argument("--pad-db", type=float, default=50, dest="pad_db")
    ap.add_argument("--tx-card", type=int, default=1, dest="tx_card")
    ap.add_argument("--settle", type=float, default=1.0)
    ap.add_argument("--windows-hw", type=int, default=10, dest="windows_hw",
                    help="decode waits per hardware step")
    ap.add_argument("--window-s", type=float, default=3.0, dest="window_s",
                    help="seconds per hardware decode wait")
    ap.add_argument("--hw-ladder", default="0,10,20,30,35,40,41.75",
                    dest="hw_ladder", help="TX attenuations dB, steps below --atten dropped; past "
                    "41.75 (ceiling) the scenario amplitude is backed off in --backoff-db steps")
    ap.add_argument("--backoff-db", default="3,6,9,12,15,18", dest="backoff_db")
    ap.add_argument("--decode-gate-db", type=float, default=1.0, dest="decode_gate_db",
                    help="hardware: per-channel decode gate dB put on the signal (default 1, the "
                         "channelizer's opt-in decode gate; negative = leave the stock gate)")
    ap.add_argument("--radio-code-dir", default=RADIO_CODE_DIR, dest="radio_code_dir")
    ap.add_argument("--svc-root", default="/app_svc", dest="svc_root")
    ap.add_argument("--out", default=DEFAULT_OUT)
    cc.add_clock_args(ap)
    args = ap.parse_args()
    names = [s.strip() for s in args.modcods.split(",") if s.strip()]
    todo = rungs(names)
    t0 = time.time()
    res = []
    if not args.hw:
        rng = np.random.default_rng(args.seed)
        with tempfile.TemporaryDirectory() as wd:
            for mc in todo:
                res.append(replay_rung(mc, args, rng, wd))
                meta = {"test": "acm_threshold_sweep", "mode": "replay", "date": cc.iso_now(),
                        "target_fer": args.target_fer, "margin_db": args.margin_db,
                        "windows": args.windows, "coarse_windows": args.coarse_windows,
                        "rate_hz": float(cc.RATE_NARROW), "seed": args.seed,
                        "elapsed_s": round(time.time() - t0, 1),
                        "esn0_definition": "Es/N0 per on-air channel symbol (QPSK-family plan "
                                           "symbol_rate is the bit rate, /2), AWGN at 1.92 MSps "
                                           "(replay_chain.awgn)"}
                cc.write_results(args.out, dict(meta, results=res), markdown(meta, res))
    else:
        if not args.arm:
            print("*** --arm not given: refusing to radiate (Setup A TRx1B to pad to TRx1A). ***", flush=True)
            return 2
        from fsk_loopback_test import Link, init_loopback
        # the ladder never goes below --atten (same rule as snr_sweep)
        steps = [(float(a), 0.9) for a in args.hw_ladder.split(",")
                 if a.strip() and float(a) >= args.atten] or [(float(args.atten), 0.9)]
        print(f"[steps] TX attenuation dB: {', '.join(f'{a:g}' for a, _ in steps)}", flush=True)
        top = steps[-1][0] if steps else 41.75
        steps += [(top, 0.9 * 10 ** (-float(b) / 20)) for b in args.backoff_db.split(",") if b.strip()]
        args.hw_steps = steps
        link = Link(args.host, args.port)
        args.clock = cc.resolve_clock(args.clock, link.serial)
        init_loopback(link, int(args.rate), int(args.center), args.atten, args.gain,
                      tx_card=args.tx_card, kind="g40", clock=args.clock,
                      lock_timeout=args.lock_timeout)
        time.sleep(1.5)
        try:
            for mc in todo:
                res.append(hw_rung(link, mc, args))
                meta = {"test": "acm_threshold_sweep", "mode": "hardware", "date": cc.iso_now(),
                        "host": args.host, "serial": link.serial, "clock": args.clock,
                        "target_fer": args.target_fer, "margin_db": args.margin_db,
                        "windows": args.windows_hw, "coarse_windows": None,
                        "pad_db": args.pad_db, "rx_gain": args.gain,
                        "loopback": f"TX {link.tx_conn} --[{args.pad_db:g} dB]--> RX {link.rx_conn}",
                        "gps": cc.gps_summary(link, args.clock),
                        "elapsed_s": round(time.time() - t0, 1)}
                cc.write_results(args.out, dict(meta, results=res), markdown(meta, res))
        finally:
            cc.teardown(link)
    md = markdown(meta, res)
    print("\n" + md, flush=True)
    print(f"[saved] {args.out}", flush=True)
    return 0 if all(r.get("threshold_db") is not None for r in res) else 1


if __name__ == "__main__":
    sys.exit(main())
