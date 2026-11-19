#!/usr/bin/env python3
"""
Per-card TX sample rate on Setup A
==================================================
Cross-card: RX TRx1A (card0) at --rx-rate, TX TRx1B (card1) at --tx-rate, 50 dB pad.

  1. init with extras tx_sample_rate; get_config must report it
  2. CW tone at +310 kHz built at the TX rate: the detection must sit at +310 kHz
     (if the TX card ran at the RX rate the tone would land at 310 kHz x rx/tx)
  3. mod_coding_card.json built at the TX rate, plan set, keyed: count C01..C13
  4. --control: the same card with TX at the RX rate, for comparison

    python3 bench/tx_rate_split_check.py --clock gpsdo [--control]
"""
import argparse, json, sys, time
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import checkout_common as cc  # noqa: E402
from dpmg.link.proto import CmdStatus  # noqa: E402

CARD = "scenarios/json/mod_coding_card.json"
MSGS = [f"C{i:02d} OK" for i in range(1, 14)]
TONE_OFF = 310e3


def cmd(link, name, p, timeout=60):
    a = cc.safe_cmd(link, name, p, timeout=timeout)
    r = a.result or {}
    print(f"[{name}] {CmdStatus(a.status).name} "
          + ", ".join(f"{k}={r[k]}" for k in list(r)[:6]), flush=True)
    return a.status, r


def decode_counts(link, seconds):
    t = cc.Telem(); t0 = time.time()
    while time.time() - t0 < seconds:
        cc.collect(link, 2.0, t)
        if all(m in t.messages for m in MSGS):
            break
    return dict(t.messages), round(time.time() - t0, 1)


def run(link, args, tx_rate, tag):
    out = {"rx_rate": args.rx_rate, "tx_rate": tx_rate}
    p = cc.port_init_params("TRx1A", "TRx1B", args.rx_rate, args.center, args.rx_gain,
                            args.tx_atten, clock=args.clock)
    if abs(tx_rate - args.rx_rate) >= 1:
        p["extras"]["tx_sample_rate"] = float(tx_rate)
    st, r = cmd(link, "init", p, timeout=120)
    out["init"] = CmdStatus(st).name
    if st != CmdStatus.OK:
        return out
    cc.gpsdo_gate(link, args.clock, label=tag)
    cmd(link, "start_rx", {}, timeout=30)
    _, cfg = cmd(link, "get_config", {}, timeout=10)
    out["cfg_tx_sample_rate"] = cfg.get("tx_sample_rate")
    out["cfg_sample_rate"] = cfg.get("sample_rate")
    time.sleep(2)
    # tone
    t_on, tel, g = cc.key_tone(link, TONE_OFF, hold_s=3.0, after_s=2.0, ampl=0.5,
                               name="txrate_tone")
    gr = g.result or {}
    d = tel.det_near(args.center + TONE_OFF, within_hz=5 * cc.SLOT_BW_HZ)
    wrong = args.center + TONE_OFF * args.rx_rate / tx_rate
    dw = (tel.det_near(wrong, within_hz=5 * cc.SLOT_BW_HZ)
          if abs(wrong - args.center - TONE_OFF) > 10 * cc.SLOT_BW_HZ else None)
    out["tone"] = {"built_fs": gr.get("fs"), "n_det": len(tel.detections),
                   "hit": d is not None, "f_off_hz": (round(d["f"] - args.center) if d else None),
                   "snr_db": (d.get("snr") if d else None),
                   "at_scaled_freq": dw is not None}
    print(f"[{tag} tone] {out['tone']}", flush=True)
    # scenario
    st, g = cmd(link, "gen_scenario", {"spec_path": CARD, "load_tx": True,
                                       "match_radio_rate": True}, timeout=180)
    out["scenario_build_rate"] = g.get("sample_rate")
    if st == CmdStatus.OK:
        cmd(link, "set_plan", {"plan_path": g["plan_path"]}, timeout=30)
        cmd(link, "start_tx", {"mode": "CONTINUOUS"}, timeout=30)
        got, secs = decode_counts(link, args.decode_s)
        cmd(link, "stop_tx", {}, timeout=30)
        ok = [m for m in MSGS if m in got]
        out["scenario"] = {"decoded": len(ok), "of": 13, "seconds": secs,
                           "missing": [m for m in MSGS if m not in got]}
        print(f"[{tag} scenario] {len(ok)}/13 in {secs}s missing {out['scenario']['missing']}",
              flush=True)
    st_, _ = cmd(link, "get_config", {}, timeout=10)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("DPMG_G40_HOST"), required=not os.environ.get("DPMG_G40_HOST"))
    ap.add_argument("--port", type=int, default=47810)
    ap.add_argument("--rx-rate", type=float, default=30.72e6)
    ap.add_argument("--tx-rate", type=float, default=1.92e6)
    ap.add_argument("--center", type=float, default=425.025e6)
    ap.add_argument("--rx-gain", type=int, default=200)
    ap.add_argument("--tx-atten", type=float, default=30, help="TX attenuation dB (default 30)")
    ap.add_argument("--decode-s", type=float, default=60)
    ap.add_argument("--control", action="store_true")
    ap.add_argument("--out", default=str(Path(os.environ.get("DPMG_BENCH_RESULTS") or
                                             Path(__file__).resolve().parents[1] / "output" / "bench") / "tx_rate_split.json"))
    cc.add_clock_args(ap)
    args = ap.parse_args()
    link = cc.Link(args.host, args.port)
    res = {"date": cc.date_tag(), "split": run(link, args, args.tx_rate, "split")}
    if args.control:
        res["control"] = run(link, args, args.rx_rate, "control")
    cc.safe_cmd(link, "stop_tx", {}, timeout=30)
    Path(args.out).write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    main()
