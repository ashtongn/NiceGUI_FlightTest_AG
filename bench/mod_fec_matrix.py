#!/usr/bin/env python3
"""
Modulation x coding decode matrix on one G40 (cases MOD-*, FEC-02, FEC-13, FEC-14; measures M-07 decode success and M-09 warm
decode latency). Setup A: TX TRx1B through the pad into RX TRx1A, cross-card, 1.92 MSps.

For every (modulation, coding) cell a one-signal scenario with a known message
is built on the radio (gen_scenario, the b200_comm_loopback mod_spec plus the
coding's fec dict), its decode plan is loaded, TX is keyed and the channelizer's
WATCH_UPDATE stream is watched for that exact message. Each cell is keyed
--reps times with a per-rep message, so a lingering decode from the previous
rep can never count.

Codings (CODINGS below; every one goes through frame_codec.encode_fec on TX and
fec_find_and_decode in the decode pool, SYNC uncoded + coded LEN/PAYLOAD/CRC):
  none                       uncoded frame
  1/2 2/3 3/4 5/6 7/8        K=7 conv, punctured, soft Viterbi
  rs                         shortened RS(255,223)
  rs+1/2 .. rs+7/8           RS outer x conv inner (concatenated)
  ldpc-1/2 ldpc-2/3 ldpc-3/4 QC-LDPC qc-r12/r23/r34, zero-filled, min-sum
  tpc-16 tpc-32              turbo product eH(16,11)^2 / eH(32,26)^2, Chase-Pyndiah
Schemes in dpmg/fec/ that cannot ride one RF frame are listed in NOT_RUNNABLE and
in the report, with the wiring they lack. A coding the local frame_codec cannot
encode is reported the same way; the radio builds the scenario with its own
frame_codec, so a coding missing there shows as build_fail on that cell.

Grids:
  default   every modulation uncoded (the MOD rows) + the five conv rates on FSK,
            4FSK, QPSK, 8PSK and 16QAM (FEC-02, 25 pairs)
  --full    every modulation x every coding (FEC-13 / FEC-14)
  --mods / --fecs / --fec-mods narrow either grid ('all' = every entry)

Offline first (--replay, no radio): each cell is built locally with the same
spec, AWGN is added at --replay-esn0 dB Es/N0 (channel symbol rate), the loop is
channelized as the GPU does (bench/replay_chain.py) and decoded by
decode_pool._default_decode with the worker's DecodeSpec. Records decodes per
cell, decode compute time and the pre-FEC channel BER. Feed that JSON to a
hardware run with --replay-json: cells that decoded 0 times offline are not keyed
(status skipped_replay), so radio time goes to cells that can decode.

The scenario loop is sized to the frame (loop_s >= 1.1 x burst): a coded DSSS
frame at 2400 Bd runs past the 0.2 s default loop and would be truncated on air.

Hardware extras: --capture-ber grabs 0.3+ s of RX IQ on the first rep of each
cell (capture_iq, scp back), replays it through the same decode and records the
pre-FEC BER over the air. --resume keeps the cells already complete in --out.
--estimate prints the hardware run time for the chosen grid and exits.

Nothing transmits without --arm.

  python3 bench/mod_fec_matrix.py --replay --full --reps 3 --out bench/RESULTS_<date>/mod_fec_full_replay.json
  python3 bench/mod_fec_matrix.py --estimate --full --reps 3 --replay-json bench/RESULTS_<date>/mod_fec_full_replay.json
  python3 bench/mod_fec_matrix.py --host 192.168.0.22 --arm --full --reps 3 --pad-db 50 \\
      --replay-json bench/RESULTS_<date>/mod_fec_full_replay.json --capture-ber --resume \\
      --out bench/RESULTS_<date>/mod_fec_full.json
  python3 bench/mod_fec_matrix.py --host 192.168.0.22 --arm --mods FSK,QPSK --fecs none,1/2 --reps 2
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import subprocess
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

from dpmg.framing import frame_codec                                                  # noqa: E402
from dpmg.link.proto import CmdStatus                                         # noqa: E402
from fsk_loopback_test import Link, init_loopback, CASE_OFFSET      # noqa: E402
from b200_comm_loopback import mod_spec                             # noqa: E402
import checkout_common as cc                                        # noqa: E402
from dpmg.deploy_config import RADIO_CODE_DIR                       # noqa: E402

# modulation: (symbol_rate, tone_spacing_hz or 0, extra signal params)
MOD_PARAMS = {
    "FSK":        (4800, 5000, {}),
    "4FSK":       (4800, 1296, {}),
    "GFSK":       (4800, 5000, {}),
    "BPSK":       (4800, 0, {}),
    "QPSK":       (4800, 0, {}),
    "pi/4-QPSK":  (4800, 0, {}),
    "DQPSK":      (4800, 0, {}),
    "OQPSK":      (4800, 0, {}),
    "8PSK":       (4800, 0, {}),
    "8QAM":       (4800, 0, {}),
    "16QAM":      (4800, 0, {}),
    "16APSK":     (4800, 0, {}),
    "32APSK":     (4800, 0, {}),
    "DSSS":       (2400, 0, {}),       # BPSK-DSSS, SF15 (modem default)
    "QPSK-DSSS":  (2400, 0, {}),
    "CSS":        (2400, 0, {}),       # LoRa-like, SF7 / 125 kHz (modem default)
}
MODS_ALL = list(MOD_PARAMS)

CONV_RATES = ["1/2", "2/3", "3/4", "5/6", "7/8"]
# label: fec dict carried by the scenario signal and the plan (None = uncoded)
CODINGS = {"none": None}
CODINGS.update({r: {"type": "conv", "rate": r} for r in CONV_RATES})
CODINGS["rs"] = {"type": "rs"}
CODINGS.update({f"rs+{r}": {"type": "rs+conv", "rate": r} for r in CONV_RATES})
CODINGS.update({"ldpc-1/2": {"type": "ldpc", "code": "qc-r12"},
                "ldpc-2/3": {"type": "ldpc", "code": "qc-r23"},
                "ldpc-3/4": {"type": "ldpc", "code": "qc-r34"},
                "tpc-16": {"type": "tpc", "code": "eH(16,11)^2"},
                "tpc-32": {"type": "tpc", "code": "eH(32,26)^2"}})
FECS_ALL = ["none"] + CONV_RATES                 # the default grid's codings
FECS_FULL = list(CODINGS)
FEC_MODS_DEFAULT = ["FSK", "4FSK", "QPSK", "8PSK", "16QAM"]      # FEC-02 cross
# 8QAM / 32APSK were never RF-swept: the plan says "record the result", so they
# do not count against the MOD pass bar
CHARACTERIZE_ONLY = {"8QAM", "32APSK"}

# dpmg/fec/ content with no RF frame path (reported, never keyed)
NOT_RUNNABLE = [
    {"scheme": "RS block interleaver (rs.block_interleave, depth > 1)",
     "why": "one RF frame is one shortened RS codeword (the 8-bit LEN caps a frame at "
            "255 payload bytes); interleaving needs several codewords per frame",
     "missing": "multi-codeword RS framing (split LEN+PAYLOAD+CRC over depth codewords) "
                "+ interleave/deinterleave in frame_codec._block_encode/_block_decode"},
    {"scheme": "TPC eH(8,4)^2 (k = 16)",
     "why": "16 info bits per block cannot hold the 24-bit LEN+CRC plus one payload byte",
     "missing": "multi-block TPC framing (several product blocks per frame)"},
    {"scheme": "ACM (acm.AcmController / select_modcod)",
     "why": "a closed loop: needs the RX Es/N0 fed back to the TX to switch MODCOD; "
            "each MODCOD it picks is a mod x conv cell of this matrix",
     "missing": "RX->TX Es/N0 report + a TX scenario that changes MODCOD on the fly "
                "(transceive path); ACM LDPC rungs via acm_encode"},
    {"scheme": "soft-decision LLRs for PSK / QAM / APSK / DSSS / CSS",
     "why": "not a scheme: the decode pool hands every FEC decoder hard bits (+-6 LLR) "
            "except 2-FSK (noncoherent fallback, frame_codec.SoftBits)",
     "missing": "demod_helper soft output (higher_order.soft_llr exists) carried as "
                "SoftBits for the other modulations"},
]

DEFAULT_OUT = os.path.join(RESULTS_DIR, "MOD_FEC_MATRIX_RESULTS.json")
# hardware time model (Setup A): a decoding rep costs ~5.5 s
# (build + plan + settle + first decode + stop); a miss waits --timeout twice
T_REP_OK_S = 5.5
T_REP_OVERHEAD_S = 5.0
T_CAPTURE_S = 4.0


def _slug(s: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in s.lower()).strip("_")


def _csv(s, allowed=None, name="value"):
    items = [x.strip() for x in str(s).split(",") if x.strip()]
    if items == ["all"] and allowed is not None:
        return list(allowed)
    if allowed is not None:
        bad = [x for x in items if x not in allowed]
        if bad:
            raise SystemExit(f"unknown {name} {bad}; choose from {list(allowed)}")
    return items


def build_grid(mods, fecs, fec_mods):
    grid = []
    for mod in mods:
        for fec in fecs:
            if fec != "none" and mod not in fec_mods:
                continue
            grid.append((mod, fec))
    return grid


def coding_wired(label):
    """(True, "") when the local frame_codec encodes this coding, else (False, why)."""
    fec = CODINGS[label]
    if fec is None:
        return True, ""
    try:
        frame_codec.encode_fec(b"probe", fec)
        return True, ""
    except Exception as e:                                           # noqa: BLE001
        return False, str(e)


def cell_message(mod, fec, rep):
    """Per-rep message that fits one block of the coding (eH(16,11)^2 carries
    12 payload bytes)."""
    cap = 40
    f = CODINGS[fec]
    if f is not None:
        cap = min(cap, (frame_codec.fec_max_info_bits(f) - 24) // 8)
    tag = f" R{rep + 1}"
    msg = f"{mod} {fec}{tag}"
    if len(msg) > cap:
        msg = f"{mod} {fec}"[:cap - len(tag)].rstrip() + tag
    return msg


def cell_spec(mod, fec, message, center, name=None):
    """The scenario spec for one cell, its loop sized to the frame."""
    from dpmg.scenario import gen_scenario_json as g
    sr, sp, extra = MOD_PARAMS[mod]
    spec = mod_spec(mod, sr, sp, message, center, name=name, extra=extra or None)
    if CODINGS[fec] is not None:
        spec["signals"][0]["fec"] = dict(CODINGS[fec])
    # The generator truncates a burst longer than loop_s
    try:
        iq, _e = g.build_signal(spec["signals"][0], float(cc.RATE_NARROW))
        dur = len(iq) / float(cc.RATE_NARROW)
        spec["loop_s"] = max(0.2, math.ceil(dur * 1.1 / 0.05) * 0.05)
    except Exception:                                                # noqa: BLE001
        pass                    # leave the default; the build reports the error
    return spec


def onair_bits(message, fec):
    data = message.encode("utf-8")
    f = CODINGS[fec]
    return frame_codec.encode_fec(data, f) if f else frame_codec.encode_message(data)


# hardware
def _capture_ber(link, args, mod, fec, message, loop_s, rep):
    """capture_iq while TX is keyed, scp it back, decode it offline with the
    plan the radio built: (errors, bits) of the pre-FEC channel or None."""
    import replay_chain as rc
    secs = round(max(0.3, 1.5 * loop_s), 2)
    name = f"mfm_cap_{_slug(mod)}_{_slug(fec)}_r{rep + 1}"
    cap = cc.safe_cmd(link, "capture_iq", {"seconds": secs, "name": name}, timeout=60)
    if cap.status != CmdStatus.OK:
        return {"err": f"capture: {cc.ack_err(cap)}"}
    rpath = cap.result["path"]
    hpath = rpath.replace(args.svc_root.rstrip("/"), args.radio_code_dir.rstrip("/"), 1)
    loc = os.path.join(tempfile.gettempdir(), name + ".c64")
    try:
        subprocess.run(["scp", "-q", f"sidekiq@{args.host}:{hpath}", loc], check=True, timeout=120)
        iq = np.fromfile(loc, dtype=np.complex64)
    except Exception as e:                                           # noqa: BLE001
        return {"err": f"scp: {e}"}
    finally:
        try:
            os.remove(loc)
        except OSError:
            pass
    with tempfile.TemporaryDirectory() as d:
        from dpmg.scenario import gen_scenario_json as g
        spec = cell_spec(mod, fec, message, int(args.center), name="cap")
        g.build_scenario(dict(spec, sample_rate_hz=float(args.rate)), d)
        _iq, plan, wl = rc.load_scenario(d, "cap")
    ch = wl[0][0]
    fs = float(cap.result.get("fs") or args.rate)
    _fo, decim = rc.worker_fs_out(wl, fs)
    bb, fse = rc.channelize_loop(rc.spectrum(iq), fs,
                                 ch.f_center_hz - float(cap.result.get("center_hz") or args.center),
                                 decim)
    r = rc.decode(bb, ch, fse)
    b = rc.raw_ber(r.bit_string, onair_bits(message, fec), frame_codec.SYNC_BITS)
    return {"seconds": secs, "decoded": bool(r.valid and message in (r.messages or [r.message])),
            "errors": None if b is None else b[0], "bits": None if b is None else b[1],
            "ber": None if b is None else b[0] / b[1]}


def run_pair(link, mod, fec, rep, center, timeout, settle, retry=True, args=None):
    """gen_scenario then set_plan then start_tx then wait for the exact message."""
    message = cell_message(mod, fec, rep)
    fec_tag = "U" if fec == "none" else _slug(fec)
    name = f"mfm_{_slug(mod)}_{fec_tag}"
    spec = cell_spec(mod, fec, message, center, name=name)
    res = {"mod": mod, "fec": fec, "rep": rep + 1, "message": message,
           "decoded": False, "frames": 0, "t_s": None, "status": "",
           "loop_s": spec["loop_s"]}
    g = cc.safe_cmd(link, "gen_scenario", {"spec": spec, "load_tx": True}, timeout=120)
    if g.status != CmdStatus.OK:
        res["status"] = f"build_fail: {cc.ack_err(g)}"
        print(f"  {mod:<10} {fec:<8} r{rep + 1}  BUILD FAIL  {cc.ack_err(g)}", flush=True)
        return res
    sp_ack = cc.safe_cmd(link, "set_plan", {"plan_path": g.result["plan_path"]}, timeout=30)
    if sp_ack.status != CmdStatus.OK:
        res["status"] = f"plan_fail: {cc.ack_err(sp_ack)}"
        print(f"  {mod:<10} {fec:<8} r{rep + 1}  PLAN FAIL   {cc.ack_err(sp_ack)}", flush=True)
        return res
    time.sleep(settle)
    st = cc.safe_cmd(link, "start_tx", {"mode": "CONTINUOUS"}, timeout=30)
    if st.status != CmdStatus.OK:
        res["status"] = f"tx_fail: {cc.ack_err(st)}"
        print(f"  {mod:<10} {fec:<8} r{rep + 1}  TX FAIL     {cc.ack_err(st)}", flush=True)
        return res
    ok, dt, obs, cnt = link.wait_decode(message, timeout=timeout)
    if not ok and retry:                               # one re-key (cold pipeline)
        cc.safe_cmd(link, "stop_tx", {}, timeout=30); time.sleep(1.0)
        cc.safe_cmd(link, "start_tx", {"mode": "CONTINUOUS"}, timeout=30)
        ok, dt, obs, cnt = link.wait_decode(message, timeout=timeout)
    if args is not None and getattr(args, "capture_ber", False) and rep == 0:
        res["capture"] = _capture_ber(link, args, mod, fec, message, spec["loop_s"], rep)
    cc.safe_cmd(link, "stop_tx", {}, timeout=30)
    res.update(decoded=bool(ok), frames=int(cnt), t_s=float(dt) if ok else None,
               status="decoded" if ok else f"no_decode(saw={obs!r})")
    capb = (res.get("capture") or {}).get("ber")
    print(f"  {mod:<10} {fec:<8} r{rep + 1}  {'DECODED' if ok else 'miss   '}  "
          f"t={dt}s frames={cnt}" + ("" if ok else f"  saw={obs!r}")
          + (f"  rawBER {capb:.2e}" if capb is not None else ""), flush=True)
    return res


# replay
def replay_pair(mod, fec, rep, esn0_db, rng, work_dir, windows=3):
    """The offline twin of run_pair: same spec built locally, AWGN at esn0_db,
    channelized like the GPU, decoded by the decode pool."""
    from dpmg.scenario import gen_scenario_json as g
    import replay_chain as rc
    message = cell_message(mod, fec, rep)
    name = f"mfm_{_slug(mod)}_{_slug(fec)}"
    res = {"mod": mod, "fec": fec, "rep": rep + 1, "message": message,
           "decoded": False, "frames": 0, "t_s": None, "status": "",
           "esn0_db": esn0_db}
    try:
        spec = cell_spec(mod, fec, message, cc.CENTER, name=name)
        res["loop_s"] = spec["loop_s"]
        # the radio's gen_scenario builds at its own rate; locally say it
        g.build_scenario(dict(spec, sample_rate_hz=float(cc.RATE_NARROW)), work_dir)
        iq, plan, wl = rc.load_scenario(work_dir, name)
    except Exception as e:                                           # noqa: BLE001
        res["status"] = f"build_fail: {e}"
        print(f"  {mod:<10} {fec:<8} r{rep + 1}  BUILD FAIL  {e}", flush=True)
        return res
    ch = wl[0][0]
    fs = float(plan.sample_rate_hz)
    _fo, decim = rc.worker_fs_out(wl, fs)
    ps = rc.burst_power(iq)
    n0 = ps / (float(ch.symbol_rate) * 10 ** (esn0_db / 10.0))
    ref = onair_bits(message, fec)
    # Run_pair waits --timeout for the message (many decode windows
    # on one keying, the worker's warm lock carried between them), then re-keys
    # once. A keying fixes the carrier phase and the sub-sample timing; each
    # window gets fresh noise. Each window holds two loops (the worker's window is
    # [prev frame, cur frame], 2 x 1M samples) so one burst is always whole.
    ok, r, b, dts, win = False, None, None, [], 0
    for _key in range(2):
        lock = {}
        base = (np.roll(np.tile(iq, 2), int(rng.integers(0, len(iq))))
                * np.exp(1j * rng.uniform(0, 2 * np.pi))).astype(np.complex64)
        # A continuously-keyed emitter is acquired long before the
        # bench reads it, so the worker's per-channel lock is warm. The costly
        # cold acquire (QPSK's ~51-point CFO sweep, DQPSK, higher-order) then
        # runs once, not per window. Seed `lock` from one noise-free acquire of
        # this exact timing/phase (not scored, not timed) so the scored windows
        # are warm: otherwise acquire variance alone fails ~1/3 of linear-mod
        # windows that the radio would not miss.
        bb0, fse0 = rc.channelize_loop(rc.spectrum(base), fs,
                                       ch.f_center_hz - plan.center_freq_hz, decim)
        rc.decode(bb0, ch, fse0, lock)
        for _w in range(max(1, windows)):
            win += 1
            x = rc.awgn(base, n0, fs, rng)
            bb, fse = rc.channelize_loop(rc.spectrum(x), fs, ch.f_center_hz - plan.center_freq_hz, decim)
            t0 = time.time()
            r = rc.decode(bb, ch, fse, lock)
            dts.append(time.time() - t0)
            ok = bool(r.valid and message in (r.messages or [r.message]))
            bw = rc.raw_ber(r.bit_string, ref, frame_codec.SYNC_BITS)
            if bw is not None and (b is None or ok):
                b = bw
            if ok:
                break
        if ok:
            break
    dt = sum(dts)
    res.update(decoded=ok, frames=len(r.messages or ([r.message] if r.valid else [])),
               t_s=round(dt, 3), windows=win, status="decoded" if ok else
               f"no_decode(saw={(r.message or '')[:40]!r})",
               raw_errors=None if b is None else b[0], raw_bits=None if b is None else b[1],
               fs_out=round(fse, 1))
    print(f"  {mod:<10} {fec:<8} r{rep + 1}  {'DECODED' if ok else 'miss   '}  "
          f"windows {win}  decode {1e3 * dt:6.0f} ms  rawBER "
          + (f"{b[0] / b[1]:.2e}" if b else "  n/a  "), flush=True)
    return res


def _ber(rs):
    """Pooled pre-FEC BER over the runs that measured it (replay or capture)."""
    e = n = 0
    for r in rs:
        if r.get("raw_bits"):
            e += r["raw_errors"]; n += r["raw_bits"]
        cap = r.get("capture") or {}
        if cap.get("bits"):
            e += cap["errors"]; n += cap["bits"]
    return (e / n) if n else None


def summarize(runs, grid, reps):
    cases = []
    for mod, fec in grid:
        rs = [r for r in runs if r["mod"] == mod and r["fec"] == fec]
        keyed = [r for r in rs if not str(r.get("status", "")).startswith("skipped")]
        ok = [r for r in keyed if r["decoded"]]
        ts = [r["t_s"] for r in ok if r["t_s"] is not None]
        cases.append({"mod": mod, "fec": fec, "reps": len(keyed), "decoded": len(ok),
                      "t_median_s": round(statistics.median(ts), 3) if ts else None,
                      "t_max_s": max(ts) if ts else None,
                      "t_first_s": (ok[0]["t_s"] if ok else None),
                      "raw_ber": _ber(rs),
                      "characterize_only": mod in CHARACTERIZE_ONLY,
                      "skipped": len(rs) - len(keyed),
                      "pass": (len(ok) == len(keyed) and len(keyed) > 0),
                      "statuses": sorted({r["status"].split("(")[0].split(":")[0] for r in rs})})
    return cases


def _fmt_ber(b):
    return "" if b is None else ("0" if b == 0 else f"{b:.1e}")


def markdown(meta, cases, mods, fecs, unwired=None):
    by = {(c["mod"], c["fec"]): c for c in cases}
    rows, brows = [], []
    for mod in mods:
        label = mod + (" (record only)" if mod in CHARACTERIZE_ONLY else "")
        row, brow = [label], [label]
        for fec in fecs:
            c = by.get((mod, fec))
            if c is None:
                row.append(""); brow.append("")
            elif c["reps"] == 0 and c["skipped"]:
                row.append("skip"); brow.append("")
            else:
                mark = "" if c["pass"] else " **FAIL**"
                tt = c["t_median_s"]
                row.append(f"{c['decoded']}/{c['reps']}{mark}"
                           + (f" ({tt:g} s)" if tt is not None else ""))
                brow.append(_fmt_ber(c["raw_ber"]))
        rows.append(row); brows.append(brow)
    replay = meta.get("mode") == "replay"
    mod_cases = [c for c in cases if c["fec"] == "none" and not c["characterize_only"]]
    fec_cases = [c for c in cases if c["fec"] in CONV_RATES]
    blk_cases = [c for c in cases if c["fec"] not in CONV_RATES and c["fec"] != "none"]
    total = sum(c["reps"] for c in cases)
    dec = sum(c["decoded"] for c in cases)
    warm = [c["t_median_s"] for c in cases if c["t_median_s"] is not None]
    if replay:
        head = (f"# Modulation x coding matrix -- OFFLINE REPLAY (no radio)\n\n"
                f"{meta['date']}  rate {meta['rate_hz']/1e6:g} MSps  Es/N0 {meta['esn0_db']:g} dB "
                f"(channel symbol rate)  reps {meta['reps']} x up to 2 keyings x {meta.get('windows_per_keying', 1)} windows  seed {meta['seed']}  "
                f"chain: gen_scenario_json -> AWGN -> GPU-FFT channelizer model -> "
                f"decode_pool._default_decode")
        tcol = "median decode compute s"
    else:
        head = (f"# Modulation x coding matrix -- {meta['serial']} @ {meta['host']}\n\n"
                f"{meta['date']}  rate {meta['rate_hz']/1e6:g} MSps  center {meta['center_hz']/1e6:.4f} MHz  "
                f"rx_gain {meta['rx_gain']}  tx_atten {meta['tx_atten']} dB  reps {meta['reps']}  "
                f"loopback {meta['loopback']}\n\n{cc.gps_header((meta.get('gps') or {}).get('clock', 'internal'), meta.get('gps'))}")
        tcol = "median seconds to first decode"
    txt = [head, "",
           f"- MOD rows (uncoded, all reps bar): {sum(1 for c in mod_cases if c['pass'])}/{len(mod_cases)} pass",
           f"- conv cells (FEC-02 / FEC-13): {sum(1 for c in fec_cases if c['pass'])}/{len(fec_cases)} pass",
           f"- block-code cells RS / RS+conv / LDPC / TPC (FEC-14): "
           f"{sum(1 for c in blk_cases if c['pass'])}/{len(blk_cases)} pass",
           f"- decode success over every keyed rep{' (M-07)' if not replay else ''}: {dec}/{total}"
           + (f" = {100.0 * dec / total:.1f} %" if total else ""),
           (f"- {'decode compute' if replay else 'M-09 warm decode latency'}: median of per-cell medians "
            f"{statistics.median(warm):.2f} s, worst cell median {max(warm):.2f} s")
           if warm else "- no decodes",
           f"- cells skipped (replay 0 decodes, not keyed): {sum(1 for c in cases if c['skipped'] and not c['reps'])}"
           if any(c["skipped"] for c in cases) else "",
           "",
           f"Cells: decoded/reps ({tcol}).", "",
           cc.md_table(["modulation \\ coding"] + fecs, rows),
           "", "## Pre-FEC channel BER",
           "", ("Hard bits the decode pool recovered vs the known on-air frame, best sync hit, "
                "pooled over the reps (replay) or the rep-1 capture (hardware --capture-ber); "
                "empty = not measured / no sync found."), "",
           cc.md_table(["modulation \\ coding"] + fecs, brows)]
    fails = [c for c in cases if not c["pass"] and c["reps"]]
    if fails:
        txt += ["", "## Failures", "",
                cc.md_table(["mod", "coding", "decoded", "statuses"],
                            [[c["mod"], c["fec"], f"{c['decoded']}/{c['reps']}",
                              ", ".join(c["statuses"])] for c in fails])]
    nr = list(NOT_RUNNABLE)
    for lab, why in (unwired or {}).items():
        nr.insert(0, {"scheme": f"coding {lab} ({CODINGS[lab]})", "why": f"local frame_codec: {why}",
                      "missing": "frame_codec FEC_BLOCK framing (this tree has it; deploy it)"})
    txt += ["", "## Not runnable over RF (reported, not keyed)", "",
            cc.md_table(["scheme", "why", "missing wiring"],
                        [[n["scheme"], n["why"], n["missing"]] for n in nr])]
    return "\n".join(t for t in txt if t is not None) + "\n"


def estimate_s(grid, reps, timeout, replay=None, capture=False, retry=True):
    """Hardware seconds for the grid (T_REP_* model); replay = {(mod, fec): decoded}
    marks the cells that will be skipped (0 offline) or will miss."""
    tot = 0.0
    n_skip = 0
    for mod, fec in grid:
        if replay is not None and _screenable(fec) and replay.get((mod, fec)) == 0:
            n_skip += 1                          # block code undecodable offline: not keyed
            continue
        # a cell replay decoded fully is cheap; one it only sometimes decoded (or that
        # was not in the replay) may miss on some reps and wait --timeout (x2 re-key)
        part = replay is not None and 0 < (replay.get((mod, fec)) or 0) < reps
        per = T_REP_OK_S + (0.4 * (T_REP_OVERHEAD_S + timeout * (2 if retry else 1)) if part else 0.0)
        tot += reps * per + (T_CAPTURE_S if capture else 0.0) + 0.4 * reps
    return tot, n_skip


def _screenable(fec):
    """Cells the offline screen may skip: the block-code RF paths only. An
    uncoded or conv cell is always keyed: the replay FSK discriminator (10 sps,
    no noncoherent fallback for GFSK) under-reports some mods (GFSK) that the
    radio decodes at high SNR, and uncoded/conv are the established baseline."""
    return fec not in ("none", "1/2", "2/3", "3/4", "5/6", "7/8")


def _load_replay(path):
    # only cells actually run offline (reps > 0) carry a screen verdict; a
    # partial/aborted replay leaves un-run cells at 0 reps: never skip those
    j = json.load(open(path))
    return {(c["mod"], c["fec"]): c["decoded"] for c in j.get("cases", []) if c.get("reps")}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=os.environ.get("DPMG_G40_HOST"), required=not os.environ.get("DPMG_G40_HOST"))
    ap.add_argument("--port", type=int, default=47810)
    ap.add_argument("--arm", action="store_true", help="allow TX to radiate")
    ap.add_argument("--reps", type=int, default=5, help="keyings per pair (default 5)")
    ap.add_argument("--full", action="store_true",
                    help="every modulation x every coding (sets --fecs all --fec-mods all)")
    ap.add_argument("--mods", default="all",
                    help=f"comma list or 'all' (default): {','.join(MODS_ALL)}")
    ap.add_argument("--fecs", default=None,
                    help=f"comma list of codings incl. 'none' or 'all' (default {','.join(FECS_ALL)}; "
                         f"all = {','.join(FECS_FULL)})")
    ap.add_argument("--fec-mods", default=None, dest="fec_mods",
                    help="which modulations get the coded pairs (default the FEC-02 "
                         "five; 'all' crosses everything)")
    ap.add_argument("--rate", type=float, default=cc.RATE_NARROW, help="sample rate Hz")
    ap.add_argument("--center", type=float, default=cc.CENTER)
    ap.add_argument("--gain", type=int, default=255, help="RX gain index")
    ap.add_argument("--atten", type=float, default=30, help="TX attenuation dB")
    ap.add_argument("--pad-db", type=float, default=20, dest="pad_db",
                    help="pad on the Setup A path (report only; default 20)")
    ap.add_argument("--tx-card", type=int, default=1, dest="tx_card",
                    help="TX card (1 = cross-card Setup A, default)")
    ap.add_argument("--timeout", type=float, default=25.0, help="seconds to wait per keying")
    ap.add_argument("--settle", type=float, default=2.0, help="seconds after set_plan")
    ap.add_argument("--no-warmup", action="store_true")
    ap.add_argument("--no-init", action="store_true", help="skip init (already up)")
    ap.add_argument("--no-cleanup", action="store_true", help="leave the card streaming")
    ap.add_argument("--resume", action="store_true",
                    help="keep the cells already complete (all reps) in --out and run the rest")
    ap.add_argument("--replay", action="store_true",
                    help="offline: build + AWGN + channelize + decode locally, no radio")
    ap.add_argument("--replay-esn0", type=float, default=20.0, dest="replay_esn0",
                    help="replay Es/N0 dB at the channel symbol rate (default 20)")
    ap.add_argument("--replay-windows", type=int, default=3, dest="replay_windows",
                    help="replay decode windows per keying (fresh noise, warm lock), "
                         "x2 keyings like the hardware re-key (default 3)")
    ap.add_argument("--seed", type=int, default=0xF0EC, help="replay noise seed")
    ap.add_argument("--replay-json", default=None, dest="replay_json",
                    help="hardware: a --replay result; cells it decoded 0 times are not keyed")
    ap.add_argument("--capture-ber", action="store_true", dest="capture_ber",
                    help="hardware: rep-1 capture_iq of each cell, decoded offline for the "
                         "pre-FEC BER (scp to/from the radio)")
    ap.add_argument("--radio-code-dir", default=RADIO_CODE_DIR,
                    dest="radio_code_dir", help="host code dir on the radio (scp root)")
    ap.add_argument("--svc-root", default="/app_svc", dest="svc_root",
                    help="container root the code dir is bind-mounted to")
    ap.add_argument("--estimate", action="store_true",
                    help="print the hardware run time for the grid and exit")
    ap.add_argument("--out", default=DEFAULT_OUT, help=f"result JSON (default {DEFAULT_OUT}); "
                    "a .md with the same stem is written next to it")
    cc.add_clock_args(ap)
    args = ap.parse_args()

    fecs_arg = args.fecs or ("all" if args.full else ",".join(FECS_ALL))
    fec_mods_arg = args.fec_mods or ("all" if args.full else ",".join(FEC_MODS_DEFAULT))
    mods = _csv(args.mods, MODS_ALL, "modulation")
    fecs = _csv(fecs_arg, FECS_FULL, "coding")
    fec_mods = _csv(fec_mods_arg, MODS_ALL, "fec-mods")
    unwired = {}
    for lab in fecs:
        ok, why = coding_wired(lab)
        if not ok:
            unwired[lab] = why
    if unwired:
        print(f"[codings] not encodable by the local frame_codec, dropped: {unwired}", flush=True)
    fecs = [f for f in fecs if f not in unwired]
    grid = build_grid(mods, fecs, fec_mods)
    replay_tab = _load_replay(args.replay_json) if args.replay_json else None
    print(f"grid: {len(grid)} (mod, coding) cells x {args.reps} reps", flush=True)

    if args.estimate:
        s, nskip = estimate_s(grid, args.reps, args.timeout, replay_tab, args.capture_ber)
        print(f"[estimate] {len(grid) - nskip} cells keyed ({nskip} skipped by replay), "
              f"{args.reps} reps: ~{s / 60:.0f} min ({s / 3600:.2f} h) of radio time"
              + (" incl. rep-1 captures" if args.capture_ber else ""), flush=True)
        return 0

    prior_runs = []
    if args.resume and os.path.exists(args.out):
        try:
            prior_runs = json.load(open(args.out)).get("runs", [])
        except Exception:                                            # noqa: BLE001
            prior_runs = []

    def complete(mod, fec):
        rs = [r for r in prior_runs if r["mod"] == mod and r["fec"] == fec]
        return len(rs) >= max(1, args.reps) or any(str(r.get("status", "")).startswith("skipped")
                                                     for r in rs)

    todo = [c for c in grid if not complete(*c)]
    runs = [r for r in prior_runs if (r["mod"], r["fec"]) in set(grid) and complete(r["mod"], r["fec"])]
    if args.resume:
        print(f"[resume] {len(grid) - len(todo)} cells kept from {args.out}, {len(todo)} to run",
              flush=True)

    t_start = time.time()
    if args.replay:
        rng = np.random.default_rng(args.seed)
        with tempfile.TemporaryDirectory() as wd:
            for mod, fec in todo:
                for rep in range(max(1, args.reps)):
                    runs.append(replay_pair(mod, fec, rep, args.replay_esn0, rng, wd,
                                               args.replay_windows))
                cases = summarize(runs, grid, args.reps)          # interim save per cell
                meta = {"test": "mod_fec_matrix", "mode": "replay", "date": cc.iso_now(),
                        "rate_hz": float(cc.RATE_NARROW), "esn0_db": args.replay_esn0,
                        "reps": args.reps, "seed": args.seed,
                        "windows_per_keying": args.replay_windows,
                        "elapsed_s": round(time.time() - t_start, 1)}
                cc.write_results(args.out, dict(meta, mods=mods, fecs=fecs, fec_mods=fec_mods,
                                                cases=cases, runs=runs, unwired=unwired,
                                                not_runnable=NOT_RUNNABLE),
                                 markdown(meta, cases, mods, fecs, unwired))
        cases = summarize(runs, grid, args.reps)
        md = markdown(meta, cases, mods, fecs, unwired)
        print("\n" + md, flush=True)
        print(f"[saved] {args.out}", flush=True)
        return 0

    if not args.arm:
        print("*** --arm not given: refusing to radiate. Re-run with --arm once the "
              "loopback cable (TRx1B to pad to TRx1A) is in. ***", flush=True)
        return 2

    link = Link(args.host, args.port)
    print(f"[link] {link.serial} @ {args.host}:{args.port}", flush=True)
    args.clock = cc.resolve_clock(args.clock, link.serial)
    rate = int(args.rate)
    center = int(args.center)
    if not args.no_init:
        init_loopback(link, rate, center, args.atten, args.gain, tx_card=args.tx_card, kind="g40",
                      clock=args.clock, lock_timeout=args.lock_timeout)
        time.sleep(1.5)
    else:                               # radio already up: still no keying before lock
        cc.gpsdo_gate(link, args.clock, args.lock_timeout, label="--no-init")
    warm = None
    if not args.no_warmup:
        warm = run_pair(link, "FSK", "none", 98, center, args.timeout, args.settle, retry=False)
        print(f"[warmup] {'hot' if warm['decoded'] else 'no decode'}", flush=True)

    def meta_now():
        return {"test": "mod_fec_matrix", "mode": "hardware", "date": cc.iso_now(),
                "host": args.host, "serial": link.serial, "rate_hz": rate, "center_hz": center,
                "rx_gain": args.gain, "tx_atten": args.atten, "reps": args.reps,
                "offset_hz": CASE_OFFSET, "tx_card": args.tx_card, "pad_db": args.pad_db,
                "loopback": f"TX {link.tx_conn} --[{args.pad_db:g} dB]--> RX {link.rx_conn}",
                "elapsed_s": round(time.time() - t_start, 1),
                "warmup_decoded": (warm or {}).get("decoded"),
                "replay_json": args.replay_json,
                "gps": cc.gps_summary(link, args.clock)}

    try:
        for mod, fec in todo:
            if (replay_tab is not None and _screenable(fec)
                    and replay_tab.get((mod, fec)) == 0):
                runs.append({"mod": mod, "fec": fec, "rep": 0, "message": "", "decoded": False,
                             "frames": 0, "t_s": None, "status": "skipped_replay(0 offline)"})
                print(f"  {mod:<10} {fec:<8} SKIP (block code, replay decoded 0)", flush=True)
                continue
            for rep in range(max(1, args.reps)):
                runs.append(run_pair(link, mod, fec, rep, center, args.timeout, args.settle,
                                     args=args))
                time.sleep(0.4)
            cases = summarize(runs, grid, args.reps)              # interim save per cell
            cc.write_results(args.out, dict(meta_now(), mods=mods, fecs=fecs, fec_mods=fec_mods,
                                            cases=cases, runs=runs, unwired=unwired,
                                            not_runnable=NOT_RUNNABLE))
    finally:
        if not args.no_cleanup:
            cc.teardown(link)

    cases = summarize(runs, grid, args.reps)
    meta = meta_now()
    md = markdown(meta, cases, mods, fecs, unwired)
    data = dict(meta, mods=mods, fecs=fecs, fec_mods=fec_mods, cases=cases, runs=runs,
                unwired=unwired, not_runnable=NOT_RUNNABLE)
    jp, mp = cc.write_results(args.out, data, md)
    print("\n" + md, flush=True)
    print(f"[saved] {jp}\n[saved] {mp}", flush=True)
    return 0 if all(c["pass"] for c in cases if not c["characterize_only"] and c["reps"]) else 1


if __name__ == "__main__":
    sys.exit(main())
