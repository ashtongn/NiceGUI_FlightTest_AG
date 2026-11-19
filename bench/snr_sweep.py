#!/usr/bin/env python3
"""
SNR sweep on one G40: DET-04 minimum
detectable SNR (M-05), DET-06 detection latency (M-04), FEC-03 coding gain /
decode threshold per conv rate (M-08) and the AMC call per step (AMC-02).
Setup A: TX TRx1B through 50 dB into RX TRx1A, cross-card, 1.92 MSps.

One scenario (default FSK 4800 Bd / 5 kHz, +300 kHz) is built on the radio
once per FEC rate in --fec; then the TX attenuation is stepped through
the --steps list (default 0,1,2,3,5,7,10,15,20,25,30,35,40,41.75 dB) with
set_tx_atten, skipping any step below --atten (default 30, the bench floor
for G40 TX attenuation). At every step:
key TX, wait --hold s for the known message (decode count), run
classify_selection on the signal's span (AMC call), stop TX, and read the
DETECTION the burst leaves when it closes (the service emits detections when a
burst ends, so a steady signal reports on stop_tx): its SNR over the detector's
median floor and its latency (burst start minus the TX-on time).

Digital backoff (--backoff, off by default): the G40's TX attenuator stops
at 41.75 dB, and through a 50 dB pad FSK is still ~45 dB above the floor
there. Each --backoff step keeps the attenuator at the last --steps value and
rebuilds the scenario with the signal under test at 0.9*r (r = 10^(-dB/20))
plus a constant-envelope FSK "level anchor" at --anchor-hz carrying the rest
of the peak (0.9*(1-r)). gen_scenario normalises every build to peak 0.9, so
a lone signal always leaves at full scale whatever its amplitude; with the
anchor the mix already peaks at 0.9 and the normalisation is a no-op, so the
test signal goes out that many dB below the plain build. The two-signal plan is
loaded once per rate and later steps only swap the TX waveform, so the
channelizer bands stay hot. --gain-leg then optionally re-keys the deepest
backoff at lower RX gain indices. Per step the tables/JSON carry backoff_db,
rx_gain and loss_db = atten + backoff + 0.5 dB per RX gain index below
--gain (nominal); thresholds and the M-08 ordering are read on loss_db,
which equals atten_db when the option is off.

Nothing transmits without --arm.

  python3 bench/snr_sweep.py --host 192.168.0.22 --arm
  python3 bench/snr_sweep.py --host 192.168.0.22 --arm --fec none,1/2 --backoff default --pad-db 50
  python3 bench/snr_sweep.py --host 192.168.0.22 --arm --fec none,1/2,2/3,3/4,5/6,7/8
  python3 bench/snr_sweep.py --host 192.168.0.22 --arm --mod QPSK --fec none,1/2
  python3 bench/snr_sweep.py --host 192.168.0.22 --arm --fec none,1/2 --backoff default --decode-gate-db 3

--decode-gate-db N (off by default) puts "decode_gate_db": N on the signal
under test: the channelizer then submits it for decode while its GPU slot is
N dB over the median floor, instead of only inside the 12 dB detection set
(Es/N0 ~19 dB for FSK 4800, the FEC-03 / M-08 tie). Needs gen_scenario_json,
plan_adapter and channelizer_worker with decode_gate_db support on the radio.
"""
from __future__ import annotations

import argparse
import os
import socket
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# results go outside the source tree (output/ is not tracked); DPMG_BENCH_RESULTS overrides
RESULTS_DIR = os.environ.get("DPMG_BENCH_RESULTS") or os.path.join(os.path.dirname(HERE), "output", "bench")
os.makedirs(RESULTS_DIR, exist_ok=True)
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from dpmg.link import proto                                                       # noqa: E402
from dpmg.link.proto import CmdStatus, MsgType                               # noqa: E402
from fsk_loopback_test import Link, init_loopback, CASE_OFFSET     # noqa: E402
from b200_comm_loopback import mod_spec                            # noqa: E402
import checkout_common as cc                                       # noqa: E402

FECS_ALL = ["none", "1/2", "2/3", "3/4", "5/6", "7/8"]
# digital backoff ladder beyond the attenuator ceiling: 5 dB to the region
# where FSK 4800 through a 50 dB pad reaches its thresholds, then 2 dB steps so
# the conv rates' coding gains (a few dB apart) do not tie
DEFAULT_BACKOFF = [5, 10, 15, 20, 25, 28, 30, 32, 34, 36, 38, 40, 42, 44, 46, 48, 50]
DEFAULT_ANCHOR_HZ = -600_000     # anchor well clear of the +300 kHz signal, its image and the DC slot
DEFAULT_OUT = os.path.join(RESULTS_DIR, "SNR_SWEEP_RESULTS.json")


def _steps(s):
    out = []
    for x in str(s).split(","):
        x = x.strip()
        if x:
            v = float(x)
            out.append(int(v) if v == int(v) else v)
    return out


def _spec_for(args, fec, center, message, tag, backoff_db=0.0):
    """Scenario spec for one FEC rate; backoff_db > 0 adds the level anchor
    (see the module docstring): signal under test at 0.9*r, anchor FSK at
    0.9*(1-r) so the builder's peak-0.9 normalisation changes nothing."""
    r = 10.0 ** (-float(backoff_db) / 20.0)
    name = f"snr_{tag}" + (f"_b{int(round(backoff_db * 10)):03d}" if backoff_db else "")
    spec = mod_spec(args.mod, args.sr, args.spacing, message, center,
                    offset=int(args.offset), name=name, ampl=0.9 * r)
    if fec != "none":
        spec["signals"][0]["fec"] = {"type": "conv", "rate": fec}
    if getattr(args, "decode_gate_db", None) is not None:
        # Per-channel decode gate on the signal under test only
        spec["signals"][0]["decode_gate_db"] = float(args.decode_gate_db)
    if backoff_db:
        spec["signals"].append({"modulation": "FSK", "symbol_rate": 4800, "tone_spacing_hz": 5000,
                                "offset_hz": int(args.anchor_hz), "message": "LEVEL ANCHOR",
                                "amplitude": 0.9 * (1.0 - r)})
    return spec


def _new_rec(args, atten, backoff=0.0, gain=None):
    gain = args.gain if gain is None else gain
    return {"atten_db": atten, "atten_applied": None, "backoff_db": backoff, "rx_gain": gain,
            "loss_db": round(float(atten) + float(backoff) + 0.5 * (args.gain - gain), 2),
            "decoded": False, "frames": 0, "decode_t_s": None, "det_snr_db": None,
            "det_latency_s": None, "det_freq_err_hz": None, "amc": None, "amc_conf": None,
            "amc_family": None}


def _wait_fresh_decode(link, expected, timeout, dn_before):
    """Like Link.wait_decode, but a band row only counts when its decoded-frame
    counter has grown past dn_before. Every step of one rate keys the same
    message, and a channelizer band keeps reporting its last message (and
    count) after the signal has gone below the decoder's reach: the first
    50 dB-pad run read "decode YES" with a frozen frame count for six steps
    past the real threshold. Returns (ok, seconds, observed, frames)."""
    link._drain()
    t0 = time.time()
    seen, last_n = "", 0
    while time.time() - t0 < timeout:
        try:
            data, _ = link.s.recvfrom(65535)
        except socket.timeout:
            continue
        try:
            mt, _seq, msg = proto.decode(data)
        except proto.ProtoError:
            continue
        if mt != MsgType.WATCH_UPDATE:
            continue
        for b in msg.bands:
            m = str(b.get("msg", ""))
            if m:
                seen, last_n = m, int(b.get("dn", 0))
            if m == expected and int(b.get("dn", 0)) > dn_before:
                return True, round(time.time() - t0, 1), m, int(b.get("dn", 0))
    return False, timeout, seen, last_n


def _key_step(link, args, out, rec, message, f_sig, half):
    """Key the armed waveform once, watch for the message, AMC the span, stop
    and read the DETECTION the burst leaves. Appends rec to out["steps"]."""
    # frames already counted for this message before keying: the previous
    # step's count, or whatever the band reports while idle
    dn_before = out.get("_dn", 0)
    idle = cc.collect(link, 0.8)
    dn_before = max(dn_before, idle.messages.get(message, 0))
    rec["frames_before"] = dn_before
    cc.drain(link)
    t_on = time.time()
    st = cc.safe_cmd(link, "start_tx", {"mode": "CONTINUOUS"}, timeout=30)
    label = (f"atten {rec['atten_db']:>5} dB{'' if rec['atten_applied'] else '*'}"
             + (f"  backoff {rec['backoff_db']:>4} dB" if rec["backoff_db"] else "")
             + (f"  gain {rec['rx_gain']}" if rec["rx_gain"] != args.gain else ""))
    if st.status != CmdStatus.OK:
        rec["tx_note"] = cc.ack_err(st)
        out["steps"].append(rec)
        print(f"  {label}  TX FAIL {cc.ack_err(st)}", flush=True)
        return
    ok, dt, _obs, cnt = _wait_fresh_decode(link, message, args.hold, dn_before)
    rec.update(decoded=bool(ok), frames=int(cnt), decode_t_s=float(dt) if ok else None)
    out["_dn"] = max(dn_before, int(cnt))
    if ok and args.hold - dt > 0.5:                # keep TX up for the rest of the hold
        time.sleep(min(args.hold - dt, 2.0))
    if not args.no_amc:
        c = cc.safe_cmd(link, "classify_selection",
                        {"f_lo": f_sig - half, "f_hi": f_sig + half, "seconds": 0.2},
                        timeout=30)
        if c.status == CmdStatus.OK:
            rec.update(amc=c.result.get("modulation"), amc_conf=c.result.get("confidence"),
                       amc_family=c.result.get("family"))
    cc.safe_cmd(link, "stop_tx", {}, timeout=30)
    t_off = time.time()                            # stop_tx acked: burst must now close
    tel = cc.collect(link, args.after)
    d = tel.det_near(f_sig, within_hz=max(half, 3 * cc.SLOT_BW_HZ))
    if d is not None:
        # DETECTION events are emitted when a burst closes, so the latency the
        # wire can show is stop_tx-ack: event arrival, both on this clock.
        # d["t"] is the burst start on the radio's clock: its offset from
        # t_on is recorded as clock skew, not latency (first G40 run: -5.1 s).
        rec.update(det_snr_db=round(d["snr"], 1), det_latency_s=round(d["rx"] - t_off, 3),
                   clock_offset_s=round(d["t"] - t_on, 3),
                   det_freq_err_hz=round(d["f"] - f_sig, 1), det_duration_ms=round(d["dur"], 1))
    out["steps"].append(rec)
    print(f"  {label}  "
          f"det {'SNR ' + cc.fnum(rec['det_snr_db']) + ' dB' if d else 'none      '}  "
          f"lat {cc.fnum(rec['det_latency_s'], 3)} s  "
          f"decode {'YES' if ok else 'no '} ({cnt} fr, t={cc.fnum(rec['decode_t_s'])}s)  "
          f"AMC {rec['amc']} ({cc.fnum(rec['amc_conf'], 2)})", flush=True)
    time.sleep(0.3)


def sweep_one(link, args, fec, center):
    """Build the scenario for one FEC rate and walk the attenuation steps,
    then (with --backoff) the digital backoff and RX gain legs."""
    message = f"SNR {args.mod} {fec}"[:40]
    tag = "U" if fec == "none" else fec.replace("/", "")
    spec = _spec_for(args, fec, center, message, tag)
    out = {"fec": fec, "message": message, "steps": [], "build": "ok"}
    g = cc.safe_cmd(link, "gen_scenario", {"spec": spec, "load_tx": True}, timeout=120)
    if g.status != CmdStatus.OK:
        out["build"] = f"build_fail: {cc.ack_err(g)}"
        print(f"[{fec}] BUILD FAIL {cc.ack_err(g)}", flush=True)
        return out
    sp = cc.safe_cmd(link, "set_plan", {"plan_path": g.result["plan_path"]}, timeout=30)
    if sp.status != CmdStatus.OK:
        out["build"] = f"plan_fail: {cc.ack_err(sp)}"
        print(f"[{fec}] PLAN FAIL {cc.ack_err(sp)}", flush=True)
        return out
    time.sleep(args.settle)
    f_sig = center + args.offset
    # selection span for the AMC call: the occupied bandwidth plus a margin
    half = max(2.0 * args.sr + args.spacing, 10_000.0) * 1.5
    print(f"\n=== {args.mod} {args.sr} Bd  fec={fec}  message={message!r} ===", flush=True)
    for step in args.steps:
        rec = _new_rec(args, step)
        a = cc.safe_cmd(link, "set_tx_atten", {"qdb": int(round(step * 4))}, timeout=15)
        rec["atten_applied"] = (a.status == CmdStatus.OK)
        if a.status != CmdStatus.OK:
            rec["atten_note"] = cc.ack_err(a)
        _key_step(link, args, out, rec, message, f_sig, half)
    if not args.backoff:
        return out
    # digital backoff leg: attenuator parked at the ceiling (last step).
    # The two-signal plan is identical for every backoff (same entries and
    # offsets): load it once, then only swap the TX waveform per step so the
    # channelizer bands stay hot and no step pays a cold first decode.
    ceiling = args.steps[-1]
    plan_loaded = False
    for b in args.backoff:
        rec = _new_rec(args, ceiling, backoff=b)
        rec["atten_applied"] = True
        g = cc.safe_cmd(link, "gen_scenario",
                        {"spec": _spec_for(args, fec, center, message, tag, b),
                         "load_tx": not plan_loaded}, timeout=120)
        if g.status != CmdStatus.OK:
            rec["build_note"] = f"build_fail: {cc.ack_err(g)}"
            out["steps"].append(rec)
            print(f"  backoff {b} dB  BUILD FAIL {cc.ack_err(g)}", flush=True)
            continue
        if not plan_loaded:
            sp = cc.safe_cmd(link, "set_plan", {"plan_path": g.result["plan_path"]}, timeout=30)
            if sp.status != CmdStatus.OK:
                rec["build_note"] = f"plan_fail: {cc.ack_err(sp)}"
                out["steps"].append(rec)
                print(f"  backoff {b} dB  PLAN FAIL {cc.ack_err(sp)}", flush=True)
                continue
            plan_loaded = True
        else:
            w = cc.safe_cmd(link, "set_tx_waveform", {"kind": "file", "path": g.result["iq_path"]},
                            timeout=60)
            if w.status != CmdStatus.OK:
                rec["build_note"] = f"waveform_fail: {cc.ack_err(w)}"
                out["steps"].append(rec)
                print(f"  backoff {b} dB  WAVEFORM FAIL {cc.ack_err(w)}", flush=True)
                continue
        time.sleep(args.settle)
        _key_step(link, args, out, rec, message, f_sig, half)
    # RX gain leg: the deepest backoff re-keyed at lower RX gain indices
    out.pop("_dn", None) if not (args.gain_leg and plan_loaded) else None
    if args.gain_leg and plan_loaded:
        for gi in args.gain_leg:
            rec = _new_rec(args, ceiling, backoff=args.backoff[-1], gain=gi)
            rec["atten_applied"] = True
            a = cc.safe_cmd(link, "set_gain", {"index": int(gi)}, timeout=15)
            rec["gain_applied"] = (a.status == CmdStatus.OK)
            if a.status != CmdStatus.OK:
                rec["gain_note"] = cc.ack_err(a)
            time.sleep(0.5)
            _key_step(link, args, out, rec, message, f_sig, half)
        cc.safe_cmd(link, "set_gain", {"index": int(args.gain)}, timeout=15)
        time.sleep(0.5)
    out.pop("_dn", None)
    return out


def _loss(s):
    return s.get("loss_db", s["atten_db"])


def _fill_est_snr(steps):
    """est_snr_db per step: the detector's SNR where it saw the signal, else
    the last detected SNR minus the extra loss since (the legs are linear in
    loss, 1 dB/dB on the G40), so decode thresholds below the detector's
    reach still get an SNR figure."""
    ref = None
    for s in steps:
        if s.get("det_snr_db") is not None:
            ref = (s["det_snr_db"], _loss(s))
            s["est_snr_db"] = s["det_snr_db"]
        elif ref is not None:
            s["est_snr_db"] = round(ref[0] - (_loss(s) - ref[1]), 1)
        else:
            s["est_snr_db"] = None


def summarize(sweeps, truth_mod):
    """Thresholds per FEC rate + the headline measures."""
    summ = {}
    for sw in sweeps:
        steps = sw["steps"]
        _fill_est_snr(steps)
        det = [s for s in steps if s["det_snr_db"] is not None]
        dec = [s for s in steps if s["decoded"]]
        lat = [s["det_latency_s"] for s in det if s["det_latency_s"] is not None]
        amc_ok = [s for s in steps if s["amc"] and str(s["amc"]).upper() == truth_mod.upper()]
        amc_fam = [s for s in steps if s["amc"]]
        summ[sw["fec"]] = {
            "build": sw["build"],
            "steps_detected": len(det), "steps_decoded": len(dec), "steps": len(steps),
            "detect_threshold_atten_db": max((s["atten_db"] for s in det), default=None),
            # loss = atten + digital backoff + nominal RX gain reduction: the
            # same as atten when --backoff is off
            "detect_threshold_loss_db": max((_loss(s) for s in det), default=None),
            "min_detectable_snr_db": min((s["det_snr_db"] for s in det), default=None),
            "decode_threshold_atten_db": max((s["atten_db"] for s in dec), default=None),
            "decode_threshold_loss_db": max((_loss(s) for s in dec), default=None),
            "decode_threshold_snr_db": min((s["det_snr_db"] for s in dec
                                            if s["det_snr_db"] is not None), default=None),
            # the same on est_snr_db (extrapolated below the detector's reach)
            "decode_threshold_snr_est_db": min((s["est_snr_db"] for s in dec
                                                if s.get("est_snr_db") is not None), default=None),
            "det_latency_median_s": round(statistics.median(lat), 3) if lat else None,
            "det_latency_max_s": round(max(lat), 3) if lat else None,
            "amc_exact_steps": len(amc_ok), "amc_called_steps": len(amc_fam),
            "atten_applied": all(s["atten_applied"] for s in steps) if steps else False,
        }
    # coding-gain ordering: each coded rate should still decode at a higher
    # loss (lower SNR) than uncoded, and 1/2 >= 2/3 >= 3/4 >= 5/6 >= 7/8.
    # Read on loss_db (= atten_db without --backoff, so nothing changes there)
    order = [f for f in ["1/2", "2/3", "3/4", "5/6", "7/8"] if f in summ]
    thr_att = {f: summ[f]["decode_threshold_atten_db"] for f in summ}
    thr = {f: summ[f]["decode_threshold_loss_db"] for f in summ}
    gain_ok = None
    if "none" in thr and order and thr["none"] is not None:
        vals = [thr[f] for f in order]
        if all(v is not None for v in vals):
            gain_ok = all(v >= thr["none"] for v in vals) and \
                all(vals[i] >= vals[i + 1] for i in range(len(vals) - 1))
    return summ, {"coding_gain_ordered": gain_ok, "decode_threshold_atten_db": thr_att,
                  "decode_threshold_loss_db": thr}


def markdown(meta, sweeps, summ, extra):
    txt = [f"# SNR sweep -- {meta['serial']} @ {meta['host']}", "",
           f"{meta['date']}  {meta['mod']} {meta['sr']} Bd / {meta['spacing']} Hz at "
           f"{meta['offset_hz']/1e3:+.0f} kHz  rate {meta['rate_hz']/1e6:g} MSps  "
           f"rx_gain {meta['rx_gain']}  loopback {meta['loopback']}"
           + (f"  backoff {meta['backoff_db']} dB (anchor {meta['anchor_hz']/1e3:+.0f} kHz)"
              if meta.get("backoff_db") else "")
           + (f"  gain leg {meta['gain_leg']}" if meta.get("gain_leg") else "")
           + (f"  decode gate {meta['decode_gate_db']:g} dB"
              if meta.get("decode_gate_db") is not None else ""), ""]
    rows = []
    for fec, s in summ.items():
        rows.append([fec, s["build"], f"{s['steps_detected']}/{s['steps']}",
                     cc.fnum(s["detect_threshold_atten_db"], 2), cc.fnum(s["detect_threshold_loss_db"], 2),
                     cc.fnum(s["min_detectable_snr_db"]),
                     f"{s['steps_decoded']}/{s['steps']}", cc.fnum(s["decode_threshold_atten_db"], 2),
                     cc.fnum(s["decode_threshold_loss_db"], 2),
                     cc.fnum(s["decode_threshold_snr_db"]), cc.fnum(s.get("decode_threshold_snr_est_db")),
                     cc.fnum(s["det_latency_median_s"], 3),
                     f"{s['amc_exact_steps']}/{s['amc_called_steps']}"])
    txt += ["## Per FEC rate", "",
            cc.md_table(["fec", "build", "detected steps", "detect thr atten dB", "detect thr loss dB",
                         "min detectable SNR dB (M-05)", "decoded steps",
                         "decode thr atten dB", "decode thr loss dB", "decode thr SNR dB",
                         "decode thr SNR est dB", "det event latency median s (M-04)",
                         "AMC exact / called"], rows), ""]
    g = extra["coding_gain_ordered"]
    txt.append("- loss dB = TX atten + digital backoff + 0.5 dB per RX gain index below the "
               "sweep gain (nominal); thresholds and the M-08 ordering are read on loss")
    txt.append("- est SNR dB = detector SNR where the signal was detected, else the last detected "
               "SNR minus the extra loss since (linear extrapolation): decode thresholds below the "
               "detector's reach are quoted on it")
    txt.append("- decoded = the band reported the step's message with a frame count ABOVE the "
               "previous step's (a band keeps showing its last message after the signal is gone)")
    txt.append("- det event latency = stop_tx ack -> DETECTION arrival on the ground clock "
               "(events are emitted when a burst closes); clock offset = radio burst-start "
               "stamp minus TX-on on the ground clock (radio/ground clock skew, not latency)")
    txt.append(f"- M-08 coding gain ordered (coded decodes at higher atten than uncoded, "
               f"1/2 >= 2/3 >= 3/4 >= 5/6 >= 7/8): "
               f"{'YES' if g else 'NO' if g is False else 'n/a (not every rate reached a threshold)'}")
    if not all(s["atten_applied"] for s in summ.values()):
        txt.append("- NOTE: set_tx_atten was not honoured on every step (backend without a TX "
                   "attenuator, e.g. the simulator): the step column is the setpoint only")
    txt.append("")
    for sw in sweeps:
        txt += [f"## Steps -- fec {sw['fec']}", "",
                cc.md_table(["atten dB", "backoff dB", "rx gain", "loss dB", "det SNR dB", "est SNR dB",
                             "det event latency s", "clock offset s",
                             "freq err Hz", "decoded", "frames", "decode t s", "AMC", "conf"],
                            [[s["atten_db"], s.get("backoff_db", 0), s.get("rx_gain", meta["rx_gain"]),
                              cc.fnum(_loss(s), 2), cc.fnum(s["det_snr_db"]), cc.fnum(s.get("est_snr_db")),
                              cc.fnum(s["det_latency_s"], 3),
                              cc.fnum(s.get("clock_offset_s"), 3),
                              cc.fnum(s["det_freq_err_hz"], 0), "yes" if s["decoded"] else "no",
                              s["frames"], cc.fnum(s["decode_t_s"]), s["amc"] or "-",
                              cc.fnum(s["amc_conf"], 2)] for s in sw["steps"]]), ""]
    return "\n".join(txt) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=os.environ.get("DPMG_G40_HOST"), required=not os.environ.get("DPMG_G40_HOST"))
    ap.add_argument("--port", type=int, default=47810)
    ap.add_argument("--arm", action="store_true", help="allow TX to radiate")
    ap.add_argument("--mod", default="FSK", help="modulation (default FSK)")
    ap.add_argument("--sr", type=int, default=4800, help="symbol rate (default 4800)")
    ap.add_argument("--spacing", type=int, default=5000, help="FSK tone spacing Hz")
    ap.add_argument("--fec", default="none",
                    help="comma list of conv rates incl. 'none' (default none); "
                         "e.g. none,1/2,2/3,3/4,5/6,7/8 for FEC-03")
    ap.add_argument("--steps", default=",".join(str(s) for s in cc.ATTEN_STEPS),
                    help="TX attenuation steps dB (default the operator console's list)")
    ap.add_argument("--hold", type=float, default=6.0, help="seconds keyed per step")
    ap.add_argument("--after", type=float, default=2.0, help="seconds to listen after stop_tx")
    ap.add_argument("--settle", type=float, default=2.0)
    ap.add_argument("--offset", type=float, default=CASE_OFFSET, help="signal offset Hz")
    ap.add_argument("--rate", type=float, default=cc.RATE_NARROW)
    ap.add_argument("--center", type=float, default=cc.CENTER)
    ap.add_argument("--gain", type=int, default=255, help="RX gain index")
    ap.add_argument("--atten", type=float, default=30,
                    help="initial TX attenuation dB, also the sweep floor: steps below it are "
                         "dropped (default 30)")
    ap.add_argument("--tx-card", type=int, default=1, dest="tx_card")
    ap.add_argument("--no-amc", action="store_true", dest="no_amc",
                    help="skip the classify_selection call per step")
    ap.add_argument("--backoff", default="",
                    help="digital backoff steps dB beyond the attenuator ceiling (comma list, "
                         f"or 'default' = {','.join(str(b) for b in DEFAULT_BACKOFF)}); off when empty")
    ap.add_argument("--anchor-hz", type=float, default=DEFAULT_ANCHOR_HZ, dest="anchor_hz",
                    help="offset of the level-anchor FSK used by --backoff (default -600 kHz)")
    ap.add_argument("--gain-leg", default="", dest="gain_leg",
                    help="RX gain indices to re-key the deepest backoff at (comma list; needs --backoff)")
    ap.add_argument("--decode-gate-db", type=float, default=None, dest="decode_gate_db",
                    help="opt-in decode gate (dB slot margin) for the signal under test: decode "
                         "while its GPU slot is this far over the floor instead of the 12 dB "
                         "detection gate (FEC-03 / M-08 below Es/N0 ~19 dB); off when unset")
    ap.add_argument("--pad-db", type=float, default=50, dest="pad_db",
                    help="pad between TX and RX, for the report header only (default 20)")
    ap.add_argument("--no-init", action="store_true")
    ap.add_argument("--no-cleanup", action="store_true")
    ap.add_argument("--out", default=DEFAULT_OUT, help=f"result JSON (default {DEFAULT_OUT})")
    cc.add_clock_args(ap)
    args = ap.parse_args()
    # the sweep never goes below --atten; first_checkout filters the same way
    args.steps = [s for s in _steps(args.steps) if s >= args.atten] or _steps(args.atten)
    print(f"[steps] TX attenuation dB: {', '.join(str(s) for s in args.steps)}", flush=True)
    args.backoff = list(DEFAULT_BACKOFF) if args.backoff.strip() == "default" else _steps(args.backoff)
    args.gain_leg = [int(g) for g in _steps(args.gain_leg)]
    if args.gain_leg and not args.backoff:
        raise SystemExit("--gain-leg needs --backoff")
    fecs = [f.strip() for f in args.fec.split(",") if f.strip()]
    bad = [f for f in fecs if f not in FECS_ALL]
    if bad:
        raise SystemExit(f"unknown fec {bad}; choose from {FECS_ALL}")
    if not args.arm:
        print("*** --arm not given: refusing to radiate. ***", flush=True)
        return 2

    link = Link(args.host, args.port)
    print(f"[link] {link.serial} @ {args.host}:{args.port}", flush=True)
    args.clock = cc.resolve_clock(args.clock, link.serial)
    rate, center = int(args.rate), int(args.center)
    if not args.no_init:
        init_loopback(link, rate, center, args.atten, args.gain, tx_card=args.tx_card, kind="g40",
                      clock=args.clock, lock_timeout=args.lock_timeout)
        time.sleep(1.5)
    else:                               # radio already up: still no keying before lock
        cc.gpsdo_gate(link, args.clock, args.lock_timeout, label="--no-init")
    t0 = time.time()
    sweeps = [sweep_one(link, args, fec, center) for fec in fecs]
    # restore the starting attenuation before leaving
    cc.safe_cmd(link, "set_tx_atten", {"qdb": int(round(args.atten * 4))}, timeout=15)
    if not args.no_cleanup:
        cc.teardown(link)
    summ, extra = summarize(sweeps, args.mod)
    meta = {"test": "snr_sweep", "date": cc.iso_now(), "host": args.host, "serial": link.serial,
            "mod": args.mod, "sr": args.sr, "spacing": args.spacing, "offset_hz": args.offset,
            "rate_hz": rate, "center_hz": center, "rx_gain": args.gain,
            "steps_db": args.steps, "hold_s": args.hold, "tx_card": args.tx_card,
            "backoff_db": args.backoff, "anchor_hz": args.anchor_hz, "gain_leg": args.gain_leg,
            "decode_gate_db": args.decode_gate_db,
            "loopback": f"TX {link.tx_conn} --[{args.pad_db:g} dB]--> RX {link.rx_conn}",
            "elapsed_s": round(time.time() - t0, 1), "gps": cc.gps_summary(link, args.clock)}
    data = dict(meta, fecs=fecs, summary=summ, **extra, sweeps=sweeps)
    md = markdown(meta, sweeps, summ, extra)
    jp, mp = cc.write_results(args.out, data, md)
    print("\n" + md, flush=True)
    print(f"[saved] {jp}\n[saved] {mp}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
