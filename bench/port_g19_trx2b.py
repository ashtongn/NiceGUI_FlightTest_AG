#!/usr/bin/env python3
"""
PORT-G-19 on Block 3 move 9 (TX TRx2B through the pad into RX TRx1B, card 1 dual FDD):
jammer and transponder keyed from the non-default TX port TRx2B.

  1. barrage jammer (gen_test_waveform kind=barrage) from TRx2B: the RX PSD on
     TRx1B must lift broadband over the TX-off baseline (same check as
     bench/jammer_checkout.py v_barrage).
  2. transponder: an FSK interrogation loops TRx2B to TRx1B while the service
     transponder (emulate=True, as fsk_loopback_test's latency phase) must
     report >= 1 event with proc/total latency.

Nothing radiates without --arm. Writes --out JSON (+ .md).
  python3 bench/port_g19_trx2b.py --host 192.168.0.22 --arm --pad-db 50
"""
from __future__ import annotations
import argparse, json, os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
# results go outside the source tree (output/ is not tracked); DPMG_BENCH_RESULTS overrides
RESULTS_DIR = os.environ.get("DPMG_BENCH_RESULTS") or os.path.join(os.path.dirname(HERE), "output", "bench")
os.makedirs(RESULTS_DIR, exist_ok=True)
sys.path.insert(0, os.path.dirname(HERE)); sys.path.insert(0, HERE)
import numpy as np                                  # noqa: E402
from dpmg.link.proto import CmdStatus                         # noqa: E402
from fsk_loopback_test import Link, case_spec, CASE_OFFSET   # noqa: E402
import checkout_common as cc                        # noqa: E402
from jammer_checkout import avg_psd, occupancy      # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=os.environ.get("DPMG_G40_HOST"), required=not os.environ.get("DPMG_G40_HOST")); ap.add_argument("--port", type=int, default=47810)
    ap.add_argument("--arm", action="store_true"); ap.add_argument("--rx", default="TRx1B")
    ap.add_argument("--tx", default="TRx2B"); ap.add_argument("--rate", type=float, default=cc.RATE_NARROW)
    ap.add_argument("--center", type=float, default=cc.CENTER); ap.add_argument("--gain", type=int, default=255)
    ap.add_argument("--atten", type=float, default=30); ap.add_argument("--pad-db", type=float, default=50, dest="pad_db")
    ap.add_argument("--out", default=os.path.join(RESULTS_DIR, "PORT_G19_RESULTS.json"))
    a = ap.parse_args()
    link = Link(a.host, a.port)
    res = {"test": "port_g19", "date": cc.iso_now(), "host": a.host, "serial": link.serial,
           "rx_port": a.rx, "tx_port": a.tx, "rate_hz": int(a.rate), "center_hz": int(a.center),
           "rx_gain": a.gain, "tx_atten": a.atten, "pad_db": a.pad_db, "armed": bool(a.arm),
           "jammer": None, "transponder": None, "verdict": None, "notes": []}
    ack, dt = cc.init_ports(link, a.rx, a.tx, int(a.rate), int(a.center), a.gain, a.atten)
    res["init_status"] = CmdStatus(ack.status).name; res["init_s"] = dt
    if ack.status != CmdStatus.OK:
        res["verdict"] = "FAIL"; res["notes"].append(f"init: {cc.ack_err(ack)}")
    elif not a.arm:
        res["verdict"] = "INIT-OK (unarmed)"; cc.teardown(link)
    else:
        try:
            time.sleep(1.2)
            boff, bdb = avg_psd(link, 1.5)
            base = float(np.median(bdb)) if bdb is not None else None
            # 1. barrage jammer from TRx2B
            jam = {"keyed": False, "baseline_floor_db": base}
            g = cc.safe_cmd(link, "gen_test_waveform", {"kind": "barrage", "ampl": 0.7, "seconds": 0.5,
                                                        "name": "g19_barrage", "load_tx": True}, timeout=30)
            if g.status != CmdStatus.OK:
                jam["err"] = f"gen: {cc.ack_err(g)}"
            else:
                tx = cc.safe_cmd(link, "start_tx", {"mode": "CONTINUOUS"}, timeout=15)
                if tx.status != CmdStatus.OK:
                    jam["err"] = f"start_tx: {cc.ack_err(tx)}"
                else:
                    jam["keyed"] = True
                    off, db = avg_psd(link, 2.0)
                    cc.safe_cmd(link, "stop_tx", {}, timeout=15); time.sleep(0.3)
                    if off is not None and bdb is not None:
                        lift = float(np.median(db) - base)
                        occ = occupancy(off, db, 6, -a.rate / 2 * 0.9, a.rate / 2 * 0.9, base=bdb)
                        jam.update(median_lift_db=round(lift, 1), lifted_occupancy=round(float(occ), 2),
                                   received=bool(lift > 6 and occ > 0.6))
                    else:
                        jam["err"] = "no PSD frames"
            res["jammer"] = jam
            print(f"[jammer] {jam}", flush=True)
            # 2. transponder on the card-1 pair (emulated TX reply, as XCV latency)
            spec = case_spec("FSK", 4800, 5000, "G19 FSK 4800", name="g19lat")
            tr = {"events": 0}
            ga = cc.safe_cmd(link, "gen_scenario", {"spec": spec, "load_tx": True}, timeout=60)
            if ga.status != CmdStatus.OK:
                tr["err"] = f"gen_scenario: {cc.ack_err(ga)}"
            else:
                cc.safe_cmd(link, "start_tx", {"mode": "CONTINUOUS"}, timeout=15); time.sleep(1.5)
                resp = {"message": "ACK", "modulation": "FSK", "symbol_rate": 4800,
                        "tone_spacing_hz": 5000, "offset_hz": CASE_OFFSET}
                ta = cc.safe_cmd(link, "transponder", {"seconds": 6.0, "trigger_snr_db": 10.0, "emulate": True,
                                                       "response": resp, "min_sep_hz": 20000, "max_events": 5},
                                 timeout=25)
                cc.safe_cmd(link, "stop_tx", {}, timeout=15)
                st = (ta.result or {}).get("stats", {}) if ta.status == CmdStatus.OK else {}
                if st:
                    tr.update(events=st.get("events", 0), proc_latency_ms=st.get("proc_latency_ms"),
                              total_latency_ms=st.get("total_latency_ms"))
                else:
                    tr["err"] = cc.ack_err(ta) if ta.status != CmdStatus.OK else "no stats"
            res["transponder"] = tr
            print(f"[transponder] {tr}", flush=True)
            ok = bool(jam.get("received")) and tr.get("events", 0) >= 1
            res["verdict"] = "PASS" if ok else "FAIL"
            if not jam.get("received"):
                res["notes"].append("barrage from TRx2B not seen on TRx1B")
            if tr.get("events", 0) < 1:
                res["notes"].append("transponder reported no events")
        finally:
            cc.teardown(link)
    md = (f"# PORT-G-19 jammer + transponder from {a.tx} -> {a.rx} ({res['serial']}, pad {a.pad_db:g} dB)\n\n"
          f"{res['date']}  init {res['init_status']} ({res['init_s']} s)  verdict **{res['verdict']}**\n\n"
          f"- jammer: {json.dumps(res['jammer'])}\n- transponder: {json.dumps(res['transponder'])}\n"
          + ("- notes: " + "; ".join(res["notes"]) + "\n" if res["notes"] else ""))
    jp, mp = cc.write_results(a.out, res, md)
    print(md + f"[saved] {jp}\n[saved] {mp}", flush=True)
    return 0 if res["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
