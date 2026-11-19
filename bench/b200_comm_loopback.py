#!/usr/bin/env python3
"""
Comm-waveform loopback across modulation types on an Ettus B200mini.

Validates the decode chain on a B200mini on a Jetson Orin Nano by transmitting
framed comm waveforms into a physical RF loopback and confirming the
channelizer decodes them back on the same radio.

  RF loopback (physical, cabled):  TX/RX  --[20 dB pad]-->  RX2

This reuses fsk_loopback_test.py's RadioLink (Link), case_spec, CASES and
dense_spec verbatim: all of that is hardware-agnostic. Only the init is
B200-specific (init_b200 here, replacing the G40's init_loopback): no card /
chan_mode keys, RX2 / TX/RX antennas, tx_gain is a gain in dB (B200 has no
attenuator), dc_offset/iq_balance quality hooks on.

B200 limit: the single transceiver + one USB3 link overflows the Jetson
if it runs full-duplex TX+RX at 30.72 MSps, so every loopback here runs at
<= 10 MSps (default 7.68 MSps, an exact B200 rate and 4x the G40's known-good
1.92 MSps, so the channelizer decimation stays integer).

Decode coverage: gen_scenario builds any modulation get_modulator supports (FSK,
4FSK, GFSK, BPSK, QPSK, pi/4-QPSK, DQPSK, OQPSK, 8PSK, 16QAM, 16APSK, DSSS, CSS)
with a matching decode plan, and the channelizer decode_pool demodulates all of
them, so the whole sweep goes through the one gen_scenario to WATCH_UPDATE
path, not only FSK/4FSK. survey_classify (blind AMC) is run as a
cross-check that the RX independently sees + classifies each waveform.

Nothing transmits without --arm.

  python3 bench/b200_comm_loopback.py --host 192.168.2.185 instructions
  python3 bench/b200_comm_loopback.py --host 192.168.2.185 --arm all
  python3 bench/b200_comm_loopback.py --host 192.168.2.185 --arm --set mods sweep
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)     # proto, modem, dpmg_service, gen_scenario_json
sys.path.insert(0, HERE)     # fsk_loopback_test (the template reused here)

from dpmg.link.proto import CmdStatus
# Reuse the template's hardware-agnostic pieces verbatim.
from fsk_loopback_test import Link as RadioLink, case_spec, CASES, CASE_OFFSET

# B200 profile
DEFAULT_RATE = 7_680_000        # exact B200 rate, 4x the G40 1.92 MSps good profile
RATE_CEILING = 10_000_000       # full-duplex TX+RX ceiling on the single B200/USB3
DEFAULT_CENTER = 915_000_000    # 915 MHz ISM
DEFAULT_RX_GAIN = 30.0          # dB (B200 RX gain 0..76 dB)
DEFAULT_TX_GAIN = 45.0          # dB (B200 TX gain 0..89.75; capped <=50 into the pad)
TX_GAIN_CAP = 50.0

# Extended comm-waveform sweep: every one of these builds through gen_scenario
# (get_modulator) and decodes through the channelizer (decode_pool). Each message
# self-identifies so a decode can never be confused with a stale band.
# (mod, symbol_rate, tone_spacing_hz_or_0, name, message, extra_sig_params)
MOD_CASES = [
    ("BPSK",      4800, 0, "bpsk_4800",   "BPSK TEST-001",      {}),
    ("QPSK",      4800, 0, "qpsk_4800",   "QPSK TEST-002",      {}),
    ("pi/4-QPSK", 4800, 0, "pi4qpsk_4800","PI4QPSK TEST-003",   {}),
    ("DQPSK",     4800, 0, "dqpsk_4800",  "DQPSK TEST-004",     {}),
    ("OQPSK",     4800, 0, "oqpsk_4800",  "OQPSK TEST-005",     {}),
    ("GFSK",      4800, 5000, "gfsk_4800", "GFSK TEST-006",     {"gfsk_bw_hz": 2400.0}),
    ("8PSK",      4800, 0, "psk8_4800",   "8PSK TEST-007",      {}),
    ("16QAM",     4800, 0, "qam16_4800",  "16QAM TEST-008",     {}),
    ("16APSK",    4800, 0, "apsk16_4800", "16APSK TEST-009",    {}),
    # spread spectrum: gen_scenario/build_signal uses ModemConfig defaults for the
    # spread params (SF15 DSSS / SF7 125k CSS), so TX and RX both use defaults.
    ("DSSS",      2400, 0, "dsss_sf15",   "DSSS TEST-010",      {}),
    ("CSS",       2400, 0, "css_sf7",     "CSS TEST-011",       {}),
]


# scenario spec builder
def mod_spec(mod, sr, spacing, message, center, offset=CASE_OFFSET,
            name=None, ampl=0.9, extra=None):
    """One-signal scenario spec for any modulation. For PSK/QAM/APSK the tone
    spacing is unused by the modulator but validate_spec still reads it for a
    rough bandwidth, so pass a sane default."""
    sig = {"modulation": mod, "symbol_rate": sr,
           "tone_spacing_hz": spacing or 5000,
           "offset_hz": offset, "message": message, "amplitude": ampl}
    if mod == "4FSK":
        sig["dibit_mapping"] = "binary"
    if extra:
        sig.update(extra)
    return {"name": name or "modcase", "center_freq_hz": center, "loop_s": 0.2,
            "signals": [sig]}


# B200 init (replaces the G40-specific init_loopback)
def init_b200(link, rate, center, rx_gain, tx_gain, clock="internal",
              rx_ant="RX2", tx_ant="TX/RX", no_init=False):
    rate = min(float(rate), RATE_CEILING)        # hard full-duplex ceiling
    tx_gain = min(float(tx_gain), TX_GAIN_CAP)    # keep safe into the 20 dB pad
    link.rx_conn, link.tx_conn = rx_ant, tx_ant
    if no_init:
        print("[init] --no-init: assuming card is already up and streaming",
              flush=True)
        link.cmd("start_rx", {}, soft=True)
        return
    extras = {"rf_bandwidth_hz": int(rate * 0.8), "tx_center_freq": center,
              "dc_offset": True, "iq_balance": True}
    print(f"[init] B200 {rate/1e6:.3f} Msps | center {center/1e6:.4f} MHz | "
          f"rx_gain {rx_gain} dB  tx_gain {tx_gain} dB", flush=True)
    p = {"kind": "b200", "sample_rate": rate, "center_freq": center,
         "rx_gain": rx_gain, "tx_gain": tx_gain, "clock_source": clock,
         "rx_antenna": rx_ant, "tx_antenna": tx_ant, "extras": extras,
         "psd_nfft": 4096, "psd_window": "hann", "psd_buffer": 65536}
    a = link.cmd("init", p, timeout=120)
    if a.status != CmdStatus.OK:
        raise SystemExit(f"init failed: {a.result}")
    r = a.result
    print(f"[init] OK  achieved={r.get('achieved_rate')} Hz  "
          f"rx_gain_db={r.get('rx_gain_db')}  libsidekiq={r.get('libsidekiq')}  "
          f"uhd/center={r.get('center_hz')}", flush=True)
    print("[ports] FDD loopback (both live):", flush=True)
    print(f"          TX out --> {tx_ant}  (radiates only while a message is keyed)",
          flush=True)
    print(f"          RX  in <-- {rx_ant}  (streams continuously)", flush=True)
    print(f"          cable: {tx_ant} --[20 dB]--> {rx_ant}", flush=True)
    link.cmd("start_rx", {})
    print(f"[RX {rx_ant}] streaming", flush=True)
    time.sleep(1.0)


# warmup
def warmup(link, center):
    """Key a throwaway FSK signal until the decode pipeline is hot, so the real
    sweep never pays the cold-start penalty."""
    print("[warmup] heating the decode pipeline...", flush=True)
    spec = mod_spec("FSK", 4800, 5000, "WARMUP", center, name="warmup")
    a = link.cmd("gen_scenario", {"spec": spec, "load_tx": True}, soft=True)
    if a is None or a.status != CmdStatus.OK:
        print(f"[warmup] gen_scenario failed: {a.result if a else 'no ack'}",
              flush=True)
        return False
    link.cmd("set_plan", {"plan_path": a.result["plan_path"]}, soft=True)
    time.sleep(2.0)
    link.cmd("start_tx", {"mode": "CONTINUOUS"}, soft=True)
    ok, dt, obs, _ = link.wait_decode("WARMUP", timeout=30)
    link.cmd("stop_tx", {}, soft=True)
    print(f"[warmup] {'HOT' if ok else 'NO DECODE'} (t={dt}s, saw {obs!r})", flush=True)
    return ok


# one decode case
def run_case(link, mod, sr, spacing, name, message, center, extra=None,
             settle=2.0, timeout=25.0):
    """gen_scenario then set_plan then start_tx CONTINUOUS then wait_decode (1 retry)
    then stop_tx. Returns a result dict."""
    spec = mod_spec(mod, sr, spacing, message, center, name=name, extra=extra)
    a = link.cmd("gen_scenario", {"spec": spec, "load_tx": True}, soft=True)
    if a is None or a.status != CmdStatus.OK:
        err = (a.result if a else "no ack")
        print(f"[{name}] BUILD FAIL: {err}", flush=True)
        return {"name": name, "mod": mod, "sr": sr, "decoded": False,
                "frames": 0, "t_s": 0.0, "status": f"build_fail:{str(err)[:120]}"}
    plan_path = a.result["plan_path"]
    link.cmd("set_plan", {"plan_path": plan_path}, soft=True)
    time.sleep(settle)
    link.cmd("start_tx", {"mode": "CONTINUOUS"}, soft=True)
    print(f"[TX {link.tx_conn} ON]  \"{message}\"  ({mod} {sr} sym/s, "
          f"+{CASE_OFFSET//1000} kHz)", flush=True)
    ok, dt, obs, cnt = link.wait_decode(message, timeout=timeout)
    if not ok:                                               # one retry
        link.cmd("stop_tx", {}, soft=True); time.sleep(1.0)
        link.cmd("start_tx", {"mode": "CONTINUOUS"}, soft=True)
        ok, dt, obs, cnt = link.wait_decode(message, timeout=timeout)
    link.cmd("stop_tx", {}, soft=True)
    print(f"[TX {link.tx_conn} OFF]", flush=True)
    if ok:
        print(f"[RX {link.rx_conn}]  DECODED \"{obs}\" in {dt}s ({cnt} frames)",
              flush=True)
    else:
        print(f"[RX {link.rx_conn}]  MISS after {dt}s (last saw {obs!r})", flush=True)
    return {"name": name, "mod": mod, "sr": sr, "decoded": bool(ok),
            "frames": int(cnt), "t_s": float(dt),
            "status": "decoded" if ok else f"no_decode(saw={obs!r})"}


# AMC cross-check
def classify_case(link, mod, sr, spacing, name, message, center, extra=None,
                  settle=2.0):
    """TX the waveform continuously and run survey_classify so the RX blindly
    detects + classifies it (independent of frame decode)."""
    spec = mod_spec(mod, sr, spacing, message, center, name=name, extra=extra)
    a = link.cmd("gen_scenario", {"spec": spec, "load_tx": True}, soft=True)
    if a is None or a.status != CmdStatus.OK:
        return {"name": name, "detected": False, "amc": None, "conf": None,
                "snr_db": None}
    link.cmd("set_plan", {"plan_path": a.result["plan_path"]}, soft=True)
    time.sleep(settle)
    link.cmd("start_tx", {"mode": "CONTINUOUS"}, soft=True)
    time.sleep(2.0)                                  # let RX fill after TX on
    sc = link.cmd("survey_classify",
                  {"seconds": 0.3, "thr_db": 10.0, "min_sep_hz": 150000,
                   "dc_guard_hz": 150000, "max_sigs": 6}, soft=True, timeout=30)
    link.cmd("stop_tx", {}, soft=True)
    det = {"name": name, "detected": False, "amc": None, "conf": None,
           "snr_db": None, "rf_hz": None}
    if sc is not None and sc.status == CmdStatus.OK:
        # pick the detection nearest the expected offset (center + CASE_OFFSET)
        want = center + CASE_OFFSET
        best = None
        for s in sc.result.get("signals", []):
            d = abs(float(s.get("rf_hz", 0)) - want)
            if best is None or d < best[0]:
                best = (d, s)
        if best is not None and best[0] < 400_000:
            s = best[1]
            det.update(detected=True, amc=s.get("modulation"),
                       conf=s.get("confidence"), snr_db=s.get("snr_db"),
                       rf_hz=s.get("rf_hz"))
    tag = (f"AMC={det['amc']} conf={det['conf']} SNR={det['snr_db']}dB"
           if det["detected"] else "not detected near offset")
    print(f"[AMC {name}] {tag}", flush=True)
    return det


# phases
def phase_fsk_sweep(link, center):
    print("\n=== FSK/4FSK SWEEP (channelizer decode) ===", flush=True)
    out = []
    for (mod, sr, sp, name, message) in CASES:
        out.append(run_case(link, mod, sr, sp, name, message, center))
        time.sleep(0.4)
    good = sum(1 for r in out if r["decoded"])
    print(f"[fsk-sweep] {good}/{len(out)} decoded", flush=True)
    return out


def phase_mod_sweep(link, center):
    print("\n=== COMM-MOD SWEEP (PSK/QAM/APSK/GFSK/spread, channelizer decode) ===",
          flush=True)
    out = []
    for (mod, sr, sp, name, message, extra) in MOD_CASES:
        out.append(run_case(link, mod, sr, sp, name, message, center, extra=extra))
        time.sleep(0.4)
    good = sum(1 for r in out if r["decoded"])
    print(f"[mod-sweep] {good}/{len(out)} decoded", flush=True)
    return out


def phase_classify(link, center):
    print("\n=== AMC CROSS-CHECK (blind survey_classify on each waveform) ===",
          flush=True)
    out = []
    probe = [("FSK", 4800, 5000, "amc_fsk", "FSK AMC", {}),
             ("BPSK", 4800, 0, "amc_bpsk", "BPSK AMC", {}),
             ("QPSK", 4800, 0, "amc_qpsk", "QPSK AMC", {}),
             ("16QAM", 4800, 0, "amc_qam16", "16QAM AMC", {})]
    for (mod, sr, sp, name, message, extra) in probe:
        out.append(classify_case(link, mod, sr, sp, name, message, center, extra=extra))
        time.sleep(0.4)
    return out


def cleanup(link):
    print("\n=== CLEANUP: stop TX + stop RX + close ===", flush=True)
    for c in ("stop_tx", "stop_rx", "close"):
        a = link.cmd(c, {}, timeout=30, soft=True)
        print(f"  {c}: {a.status if a else 'no ack'}", flush=True)


# main
def print_instructions():
    print("""
B200mini comm-waveform loopback

  Radio:  B200mini on a Jetson Orin Nano @ 192.168.2.185 (dpmg_service --kind b200)
  Cable:  TX/RX --[20 dB pad]--> RX2   (physical RF loopback, already cabled)
  Rate:   <= 10 MSps (default 7.68); the single B200/USB3 overflows full-duplex
          at 30.72. tx_gain is a gain in dB (B200 has no attenuator), capped <=50.

Nothing radiates without --arm.

  python3 bench/b200_comm_loopback.py --host 192.168.2.185 --arm all
""", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", nargs="?", default="all",
                    choices=["instructions", "sweep", "mods", "classify",
                             "cleanup", "all"])
    ap.add_argument("--host", default="192.168.2.185")
    ap.add_argument("--port", type=int, default=47810)
    ap.add_argument("--rate", type=float, default=DEFAULT_RATE)
    ap.add_argument("--center", type=float, default=DEFAULT_CENTER)
    ap.add_argument("--rx-gain", type=float, default=DEFAULT_RX_GAIN)
    ap.add_argument("--tx-gain", type=float, default=DEFAULT_TX_GAIN)
    ap.add_argument("--set", choices=["fsk", "mods", "all"], default="all",
                    help="which decode sweep(s) the 'sweep'/'all' phase runs")
    ap.add_argument("--arm", action="store_true", help="allow TX to radiate")
    ap.add_argument("--no-init", action="store_true", help="skip init (already up)")
    ap.add_argument("--no-cleanup", action="store_true", help="leave card streaming")
    ap.add_argument("--save", default=None, help="write the result JSON here")
    args = ap.parse_args()

    if args.phase == "instructions":
        print_instructions()
        return

    print_instructions()
    if not args.arm:
        print("*** --arm not given: refusing to radiate. Re-run with --arm. ***",
              flush=True)
        return

    link = RadioLink(args.host, args.port)
    print(f"[link] session up with {link.serial} @ {args.host}:{args.port}",
          flush=True)

    init_b200(link, args.rate, args.center, args.rx_gain, args.tx_gain,
              no_init=args.no_init)

    report = {"host": args.host, "rate_hz": min(args.rate, RATE_CEILING),
              "center_hz": args.center, "rx_gain_db": args.rx_gain,
              "tx_gain_db": min(args.tx_gain, TX_GAIN_CAP), "serial": link.serial}

    if args.phase in ("all", "sweep", "mods"):
        warmup(link, args.center)

    if args.phase in ("all", "sweep"):
        if args.set in ("fsk", "all"):
            report["fsk_sweep"] = phase_fsk_sweep(link, args.center)
        if args.set in ("mods", "all"):
            report["mod_sweep"] = phase_mod_sweep(link, args.center)
    elif args.phase == "mods":
        report["mod_sweep"] = phase_mod_sweep(link, args.center)

    if args.phase in ("all", "classify"):
        report["classify"] = phase_classify(link, args.center)

    if args.phase == "cleanup" or (args.phase == "all" and not args.no_cleanup):
        cleanup(link)

    # summary table
    print("\n================= RESULTS =================", flush=True)
    for key, title in (("fsk_sweep", "FSK/4FSK"), ("mod_sweep", "COMM MODS")):
        if key not in report:
            continue
        print(f"\n  {title} (channelizer full-frame decode @ "
              f"{report['rate_hz']/1e6:g} MSps):", flush=True)
        for r in report[key]:
            v = "DECODE" if r["decoded"] else "  --  "
            print(f"    [{v}] {r['name']:<14} {r['mod']:<10} "
                  f"{r['frames']:>3} fr  {r['t_s']:>4}s  {r['status']}", flush=True)
    if "classify" in report:
        print("\n  AMC cross-check (blind survey_classify):", flush=True)
        for d in report["classify"]:
            print(f"    {d['name']:<12} "
                  f"{'DET' if d['detected'] else 'none'}  "
                  f"AMC={d['amc']} conf={d['conf']} SNR={d['snr_db']}", flush=True)

    save = args.save or os.path.join(
        os.environ.get("TMPDIR", "/tmp"), "b200_comm_loopback_results.json")
    try:
        with open(save, "w") as f:
            json.dump(report, f, indent=1)
        print(f"\n[saved] {save}", flush=True)
    except OSError as e:
        print(f"[save failed] {e}", flush=True)


if __name__ == "__main__":
    main()
