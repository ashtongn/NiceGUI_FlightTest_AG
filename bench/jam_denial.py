#!/usr/bin/env python3
"""
jam_denial.py: JS-02 decode denial: a narrowband
victim (FSK 4800 and 16QAM, at 1.92 MSps) is jammed by a partial-band or spot
jammer stepped over a J/S ladder, one TX port, and the J/S at which the victim
stops decoding is recorded.

For each victim, jammer type and J/S step the composite TX waveform is built
digitally (victim loop + jammer scaled to the step, one file, peak-normalised to
--ampl) and either decoded offline (--replay) or keyed on Setup A, captured and
decoded through the victim's plan.

    python3 bench/jam_denial.py --replay
    python3 bench/jam_denial.py --host 192.168.0.22 --arm --clock gpsdo --pad-db 50
"""
from __future__ import annotations

import argparse
import math
import os
import subprocess
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
from dpmg.deploy_config import RADIO_CODE_DIR                       # noqa: E402

CENTER_1P92 = 425_025_000.0     # the bench Setup A centre for the JS-02 narrow victims
# time model for a hardware step (build + key + capture + decode), per step
T_STEP_S = 12.0


# victim signal set


# interferer baseband at the victim rate


def rms_power(x):
    a = np.abs(np.asarray(x)) ** 2
    return float(np.mean(a)) if a.size else 0.0


def compose(wanted, interf, ratio_db, ampl):
    """wanted + interf scaled so avg(interf)/avg(wanted) = ratio_db. Peak-
    normalised to ampl (FS). Returns (composite complex64, scale applied)."""
    pw = rms_power(wanted) or 1.0
    pj = rms_power(interf) or 1.0
    g = math.sqrt(pw / pj) * 10 ** (ratio_db / 20.0)
    comb = np.asarray(wanted) + g * np.asarray(interf)
    pk = float(np.max(np.abs(comb))) or 1.0
    return (comb * (ampl / pk)).astype(np.complex64), g


# decode scoring (both roles share it)


# summary helpers


# replay (offline)


# JS-02 decode-denial (generic narrowband victim vs partial-band / spot)
DENIAL_VICTIMS = {
    "fsk4800": {"modulation": "FSK", "symbol_rate": 4800, "tone_spacing_hz": 5000,
                "msg": "JS02 FSK4800"},
    "16qam":   {"modulation": "16QAM", "symbol_rate": 4800, "tone_spacing_hz": 5000,
                "msg": "JS02 16QAM"},
}
DENIAL_JAMMERS = ("partial", "spot")


def _build_victim_1p92(vkey, off, rate):
    """One narrowband victim scenario at `rate`. Returns (loop iq, plan, cc, message)."""
    from dpmg.scenario import gen_scenario_json as g
    import replay_chain as rc
    v = DENIAL_VICTIMS[vkey]
    msg = v["msg"]
    sig = {"modulation": v["modulation"], "symbol_rate": v["symbol_rate"],
           "tone_spacing_hz": v["tone_spacing_hz"], "offset_hz": off,
           "message": msg, "amplitude": 0.9}
    spec = {"name": f"js02_{vkey}", "sample_rate_hz": rate, "center_freq_hz": CENTER_1P92,
            "loop_s": 0.2, "signals": [sig]}
    with tempfile.TemporaryDirectory() as d:
        g.build_scenario(spec, d)
        iq, plan, wl = rc.load_scenario(d, spec["name"])
    return iq, plan, wl[0][0], msg


def _denial_jammer(kind, fs, dur, off, sym_rate, args, seed=0):
    """A partial-band noise patch or a spot CW tone centred on the victim."""
    from dpmg.dsp import tools_engine as te
    n = int(round(fs * dur))
    if kind == "partial":
        y = te.band_noise(fs, max(3.0 * sym_rate, 15e3), dur, ampl=1.0)
    elif kind == "spot":
        y = te.cw_tone(fs, 0.0, dur, ampl=1.0)
    else:
        raise SystemExit(f"unknown denial jammer {kind}")
    y = np.asarray(y, dtype=np.complex64)[:n]
    if len(y) < n:
        y = np.concatenate([y, np.zeros(n - len(y), np.complex64)])
    if off:                                           # shift the jammer onto the victim
        y = (y * np.exp(2j * np.pi * (off / fs) * np.arange(len(y)))).astype(np.complex64)
    if args.prf > 0 and args.duty < 1.0:
        y = te.apply_pulse(y, fs, float(args.prf), float(args.duty))
    return y


def _decode_victim_1p92(iq, plan, cc_, msg):
    import replay_chain as rc
    off = float(cc_.f_center_hz - plan.center_freq_hz)
    _fo, decim = rc.worker_fs_out([(cc_, None)], float(plan.sample_rate_hz))
    bb, fse = rc.channelize_loop(rc.spectrum(iq), float(plan.sample_rate_hz), off, decim)
    r = rc.decode(bb, cc_, fse)
    return bool(r.valid and msg in (r.messages or [r.message]))


def run_replay_denial(args):
    import replay_chain as rc
    rate = float(args.denial_rate)
    off = float(args.wanted_offset)
    rng = np.random.default_rng(args.seed)
    res = {"test": "jam_denial", "role": "denial", "mode": "replay", "date": _iso(),
           "sample_rate_hz": rate, "center_hz": CENTER_1P92, "victim_offset_hz": off,
           "ratios_db": args.ratios, "victims": list(DENIAL_VICTIMS),
           "jammers": list(DENIAL_JAMMERS), "by_victim": {}}
    for vkey in DENIAL_VICTIMS:
        iq, plan, cc_, msg = _build_victim_1p92(vkey, off, rate)
        dur = len(iq) / rate
        base = _decode_victim_1p92(iq, plan, cc_, msg)
        vres = {"baseline_decoded": base, "jammers": {}}
        print(f"[{vkey}] baseline decoded {base}", flush=True)
        for jk in DENIAL_JAMMERS:
            jam = _denial_jammer(jk, rate, dur, off, DENIAL_VICTIMS[vkey]["symbol_rate"], args)
            rows, fail_js = [], None
            for r in args.ratios:
                comp, g = compose(iq, jam, r, args.ampl)
                ok = _decode_victim_1p92(comp, plan, cc_, msg)
                rows.append({"ratio_db": r, "decoded": ok})
                if not ok and fail_js is None:
                    fail_js = r
                print(f"  {vkey:<8} {jk:<8} J/S {r:>4} dB  decoded {ok}", flush=True)
            vres["jammers"][jk] = {"rows": rows, "denial_js_db": fail_js}
        res["by_victim"][vkey] = vres
    return res


def run_hardware_denial(args):
    """JS-02 on the radio: build each composite at 1.92 MSps, key once per step,
    capture RX and decode the victim back. Records the J/S at which decode fails."""
    from fsk_loopback_test import Link, init_loopback
    import replay_chain as rc
    from dpmg.link.proto import CmdStatus
    rate = float(args.denial_rate)
    off = float(args.wanted_offset)
    link = Link(args.host, args.port)
    print(f"[link] {link.serial} @ {args.host}:{args.port}", flush=True)
    args.clock = cc.resolve_clock(args.clock, link.serial)
    init_loopback(link, int(rate), int(CENTER_1P92), args.atten, args.gain,
                  tx_card=args.tx_card, kind="g40", clock=args.clock, lock_timeout=args.lock_timeout)
    time.sleep(1.5)
    link.cmd("start_rx", {}, soft=True)
    host_wf = args.radio_code_dir.rstrip("/") + "/data/waveforms"
    svc_wf = args.svc_root.rstrip("/") + "/data/waveforms"
    res = {"test": "jam_denial", "role": "denial", "mode": "hardware", "date": _iso(),
           "host": args.host, "serial": link.serial, "sample_rate_hz": rate,
           "center_hz": CENTER_1P92, "victim_offset_hz": off, "pad_db": args.pad_db,
           "rx_gain": args.gain, "tx_atten": args.atten, "ratios_db": args.ratios,
           "loopback": f"TX {link.tx_conn} --[{args.pad_db:g} dB]--> RX {link.rx_conn}",
           "gps": cc.gps_summary(link, args.clock), "by_victim": {}}
    try:
        for vkey in DENIAL_VICTIMS:
            iq, plan, cc_, msg = _build_victim_1p92(vkey, off, rate)
            dur = len(iq) / rate
            # build the victim on the radio once for its decode plan (load_tx
            # False: the composite TX file replaces it per step)
            v = DENIAL_VICTIMS[vkey]
            vspec = {"name": f"js02_{vkey}", "sample_rate_hz": rate, "center_freq_hz": CENTER_1P92,
                     "loop_s": 0.2, "signals": [{"modulation": v["modulation"],
                     "symbol_rate": v["symbol_rate"], "tone_spacing_hz": v["tone_spacing_hz"],
                     "offset_hz": off, "message": msg, "amplitude": 0.9}]}
            gv = cc.safe_cmd(link, "gen_scenario", {"spec": vspec, "load_tx": False}, timeout=120)
            cc.safe_cmd(link, "set_plan", {"plan_path": gv.result["plan_path"]}, timeout=30)
            vres = {"jammers": {}}
            for jk in DENIAL_JAMMERS:
                jam = _denial_jammer(jk, rate, dur, off, DENIAL_VICTIMS[vkey]["symbol_rate"], args)
                rows, fail_js = [], None
                for r in args.ratios:
                    comp, g = compose(iq, jam, r, args.ampl)
                    name = f"js02_{vkey}_{jk}_{str(r).replace('-','m')}"
                    ok = _hw_denial_step(link, args, comp, rate, plan, cc_, msg, name, host_wf, svc_wf)
                    rows.append({"ratio_db": r, "decoded": ok})
                    if not ok and fail_js is None:
                        fail_js = r
                vres["jammers"][jk] = {"rows": rows, "denial_js_db": fail_js}
            res["by_victim"][vkey] = vres
            cc.write_results(args.out, res)
    finally:
        cc.teardown(link)
    return res


def _hw_denial_step(link, args, comp, rate, plan, cc_, msg, name, host_wf, svc_wf):
    """Push the composite as the TX waveform, key, capture RX, decode the victim
    offline. The victim plan is already set by run_hardware_denial."""
    import replay_chain as rc
    from dpmg.link.proto import CmdStatus
    loc = os.path.join(tempfile.gettempdir(), name + ".c64")
    comp.astype(np.complex64).tofile(loc)
    try:
        subprocess.run(["scp", "-q", loc, f"sidekiq@{args.host}:{host_wf}/{name}.c64"],
                       check=True, timeout=120)
    except Exception as e:                                          # noqa: BLE001
        print(f"  {name} scp_fail {e}", flush=True)
        return False
    finally:
        try:
            os.remove(loc)
        except OSError:
            pass
    cc.safe_cmd(link, "set_tx_waveform", {"kind": "file", "path": f"{svc_wf}/{name}.c64"}, timeout=60)
    cc.safe_cmd(link, "start_tx", {"mode": "CONTINUOUS"}, timeout=30)
    time.sleep(args.settle)
    cap = cc.safe_cmd(link, "capture_iq", {"seconds": round(1.5 * len(comp) / rate, 2),
                      "name": name + "_rx"}, timeout=60)
    cc.safe_cmd(link, "stop_tx", {}, timeout=30)
    if cap.status != CmdStatus.OK:
        return False
    rpath = cap.result["path"].replace(args.svc_root.rstrip("/"), args.radio_code_dir.rstrip("/"), 1)
    lcap = os.path.join(tempfile.gettempdir(), name + "_rx.c64")
    try:
        subprocess.run(["scp", "-q", f"sidekiq@{args.host}:{rpath}", lcap], check=True, timeout=120)
        rx = np.fromfile(lcap, dtype=np.complex64)
    except Exception:
        return False
    finally:
        try:
            os.remove(lcap)
        except OSError:
            pass
    nloop = len(comp)
    rxloop = rx[:nloop] if len(rx) >= nloop else np.concatenate([rx, np.zeros(nloop - len(rx), np.complex64)])
    return _decode_victim_1p92(rxloop, plan, cc_, msg)


# reporting
def _iso():
    import datetime
    return datetime.datetime.now().isoformat(timespec="seconds")


def markdown(res):
    import checkout_common as cc
    return _md_denial(res, cc)

def _md_denial(res, cc):
    replay = res.get("mode") == "replay"
    lines = ["# JS-02 decode-denial J/S -- " + ("OFFLINE REPLAY" if replay else
             f"{res.get('serial')} @ {res.get('host')}"), "",
             f"{res['date']}  victims FSK 4800 + 16QAM @ {res['sample_rate_hz']/1e6:g} MSps, "
             f"offset {res['victim_offset_hz']/1e3:g} kHz"
             + ("" if replay else f"  {res.get('loopback','')}  pad {res.get('pad_db')} dB"), "",
             "R = jammer / victim power, loop-average. denial J/S = the lowest R at which "
             "the victim stops decoding. partial = narrow noise patch on the victim; spot = "
             "CW on the victim.", ""]
    if not replay and res.get("gps"):
        lines += [cc.gps_header((res.get("gps") or {}).get("clock", "internal"), res.get("gps")), ""]
    for vkey, vres in (res.get("by_victim") or {}).items():
        lines.append(f"## {vkey}" + (f" (baseline decoded {vres.get('baseline_decoded')})"
                                      if "baseline_decoded" in vres else ""))
        for jk, jres in vres["jammers"].items():
            dj = jres.get("denial_js_db")
            lines.append(f"- {jk}: denial J/S = "
                         + (f"{dj} dB" if dj is not None else "not denied over the ladder")
                         + ": " + ", ".join(f"{r['ratio_db']}:{'Y' if r['decoded'] else 'N'}"
                                             for r in jres["rows"]))
        lines.append("")
    return "\n".join(lines) + "\n"


def estimate(args):
    n = len(DENIAL_VICTIMS) * len(DENIAL_JAMMERS) * len(args.ratios)
    s = n * T_STEP_S
    print(f"[estimate] {n} steps x ~{T_STEP_S:g} s = ~{s/60:.0f} min ({s/3600:.2f} h) "
          f"of radio time (build + key + capture + decode per step)", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--denial-rate", type=float, default=1_920_000.0, dest="denial_rate",
                    help="victim sample rate Hz (default 1.92 MSps)")
    ap.add_argument("--replay", action="store_true", help="offline, no radio")
    ap.add_argument("--estimate", action="store_true", help="print hardware time and exit")
    ap.add_argument("--host", default=os.environ.get("DPMG_G40_HOST"), required=not os.environ.get("DPMG_G40_HOST"))
    ap.add_argument("--port", type=int, default=47810)
    ap.add_argument("--arm", action="store_true", help="allow TX to radiate")
    ap.add_argument("--ratios", type=int, nargs="+", default=[-20, -10, 0, 10, 20, 30],
                    help="power ratios dB (interferer/wanted), space-separated, e.g. -20 -10 0 10 20 30")
    ap.add_argument("--ampl", type=float, default=0.6, help="composite TX peak (FS)")
    ap.add_argument("--gain", type=int, default=255, help="RX gain index")
    ap.add_argument("--atten", type=float, default=30, help="TX attenuation dB")
    ap.add_argument("--pad-db", type=float, default=20, dest="pad_db", help="Setup A pad (report)")
    ap.add_argument("--tx-card", type=int, default=1, dest="tx_card")
    ap.add_argument("--settle", type=float, default=2.0)
    ap.add_argument("--capture", action="store_true", help="(hardware) decode captured RX (always on)")
    ap.add_argument("--seed", type=int, default=0x5F24)
    ap.add_argument("--replay-json", default=None, dest="replay_json",
                    help="(hardware) a --replay result; (jammer, R) with 0 clean copies are skipped")
    ap.add_argument("--wanted-offset", type=float, default=300_000.0, dest="wanted_offset",
                    help="victim offset Hz")
    # interferer-type parameters (GUI defaults)
    ap.add_argument("--patch-bw", type=float, default=6_000_000.0, dest="patch_bw")
    ap.add_argument("--patch-center", type=float, default=-6_000_000.0, dest="patch_center")
    ap.add_argument("--tone-center", type=float, default=-5_500_000.0, dest="tone_center")
    ap.add_argument("--comb-n", type=int, default=24, dest="comb_n")
    ap.add_argument("--comb-spacing", type=float, default=1_000_000.0, dest="comb_spacing")
    ap.add_argument("--swept-span", type=float, default=24_000_000.0, dest="swept_span")
    ap.add_argument("--swept-period", type=float, default=0.01, dest="swept_period")
    ap.add_argument("--swept-noise-bw", type=float, default=0.0, dest="swept_noise_bw")
    ap.add_argument("--mod-rate", type=float, default=1_000_000.0, dest="mod_rate")
    ap.add_argument("--mod-const", default="qpsk", dest="mod_const")
    ap.add_argument("--mod-center", type=float, default=0.0, dest="mod_center")
    ap.add_argument("--mod-duty", type=float, default=1.0, dest="mod_duty")
    ap.add_argument("--prf", type=float, default=0.0, help="pulse the interferer at this PRF Hz")
    ap.add_argument("--duty", type=float, default=1.0, help="pulse duty 0..1 (with --prf)")
    ap.add_argument("--radio-code-dir", default=RADIO_CODE_DIR, dest="radio_code_dir")
    ap.add_argument("--svc-root", default="/app_svc", dest="svc_root")
    ap.add_argument("--out", default=os.path.join(RESULTS_DIR, "JAM_DENIAL_RESULTS.json"))
    import checkout_common as cc
    cc.add_clock_args(ap)
    args = ap.parse_args()

    if args.estimate:
        estimate(args)
        return 0
    if args.replay:
        res = run_replay_denial(args)
    else:
        if not args.arm:
            print("*** --arm not given: refusing to radiate. ***", flush=True)
            return 2
        res = run_hardware_denial(args)
    md = markdown(res)
    jp, mp = cc.write_results(args.out, res, md)
    print("\n" + md, flush=True)
    print(f"[saved] {jp}\n[saved] {mp}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
