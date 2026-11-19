"""gen_hpa_bench_mission.py: a timed mission plan for the HPA bench power sweep.

    python3 tools/gen_hpa_bench_mission.py [--radio G40-0002] [--address 192.168.0.23:47810]
                                           [--freqs 915,2200] [--attens 41.75,35,30,25,20,15,10]
                                           [--dwell 20] [--gap 20] [--ampl 0.5]
                                           [--tx-port TRx1A] [--rx-port TRx2B|none]
                                           [--out output/bench/hpa_bench_sweep.dpmg-mission.json]

--rx-port none writes TX only steps: the runner then keeps the radio's RX if it
is on the other card from the TX jack and otherwise moves it there, so a TX
jack on the same card as the only cabled RX jack is not refused.

One tools.tone step per (frequency, TX attenuation), highest attenuation first,
keyed for --dwell seconds, then --gap seconds of silence to write the analyzer
reading into the Sweep sheet (tools/gen_hpa_bench_sheet.py).  The plan's notes
carry the expected coupled-port level of every step so the planner page shows
them next to the timeline.  Open the file with Open file on the mission page,
Check plan, bring the radio up on the Ground Link tab (ports as the steps say,
1.92 MSps), then Run mission.

The same payload_budget maths as the sweep script and the sheet sets the
expected levels, so the three agree.  Nothing here keys a radio.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from dpmg.mission import mission_file, catalog, ports               # noqa: E402
from dpmg.payload import payload_budget as pb                       # noqa: E402

DEFAULT_OUT = os.path.join(os.environ.get("DPMG_BENCH_RESULTS") or os.path.join(ROOT, "output", "bench"),
                           f"{_dt.date.today():%Y-%m-%d}_hpa_bench_p2b", "hpa_bench_sweep.dpmg-mission.json")


def _floats(text):
    return [float(x) for x in text.replace(";", ",").split(",") if x.strip()]


def _rx_text(a) -> str:
    """How the notes name the RX side."""
    if a.rx_port != ports.RX_NONE:
        return f"RX {a.rx_port}"
    park = ports.rx_for_none("g40", None, a.tx_port)
    return f"RX none (steps are TX only; RX on the other card, {park} if it is not there already)"


def build(a) -> dict:
    if str(a.rx_port).strip().lower() == ports.RX_NONE:
        a.rx_port = ports.RX_NONE
    data = pb.apply_g40_max(pb.load_profiles(a.profiles), a.g40_max_dbm)
    prof = pb.profile(data, a.profile)
    import math
    backoff = -20.0 * math.log10(a.ampl)
    choices = set(float(v) for v in catalog.TX_ATTEN_CHOICES)
    steps, lines = [], []
    t = float(a.lead)
    n = 0
    for f in _floats(a.freqs):
        for att in sorted(_floats(a.attens), reverse=True):
            if att not in choices:
                raise SystemExit(f"TX attenuation {att:g} dB is not a planner choice; use one of "
                                 f"{', '.join(f'{v:g}' for v in catalog.TX_ATTEN_CHOICES)}")
            x = pb.coupled_port_expected(data, prof, f, att, backoff, a.extra_loss)
            mx = x["hpa_max_input_dbm"]
            if mx is not None and x["hpa_in_dbm"] > mx - a.hpa_in_margin:
                lines.append(f"  SKIPPED {f:g} MHz {att:g} dB: HPA input {x['hpa_in_dbm']:.1f} dBm too close "
                             f"to its {mx:g} dBm max")
                continue
            n += 1
            sid = f"hpa_{n:02d}_{f:g}M_{att:g}dB".replace(".", "p")
            steps.append({
                "id": sid, "radio": a.radio, "technique": "tools.tone", "radiates": True,
                "params": {"center_hz": f * 1e6, "tx_center_hz": f * 1e6, "tx_atten_db": float(att),
                           "ampl": float(a.ampl), "freq": float(a.tone_hz), "seconds": 0.1, "kind": "tone"},
                "schedule": {"start_s": round(t, 1), "stop_s": round(t + a.dwell, 1)},
                "ports": {"rx": a.rx_port, "tx": a.tx_port},
            })
            mm = int(t // 60)
            lines.append(f"  {mm:02d}:{t % 60:04.1f}  step {n:2d}  {f:g} MHz  TX atten {att:g} dB  "
                         f"expected at the coupled port {x['coupled_dbm']:+.1f} dBm  "
                         f"(HPA out {x['hpa_out_dbm']:.1f} dBm, {x['hpa_out_w']:.1f} W, {x['region']})")
            t += a.dwell + a.gap
    total = t - a.gap
    notes = (f"HPA bench power sweep, profile {a.profile}: G40 {a.tx_port} into the ZB3PD-63-S+ combiner, "
             f"Empower 1189, ZGDC30-372HP+ coupler into a dummy load, analyzer on the coupled port "
             f"(extra loss {a.extra_loss:g} dB). No T/R switch, no LNA, nothing on the RX jacks. "
             f"Tone amplitude {a.ampl:g} ({backoff:.1f} dB backoff), {a.dwell:g} s keyed, {a.gap:g} s to write "
             f"the reading. Highest attenuation first. Total {total/60:.1f} min.\n"
             "Before Run mission: HPA DC on only after the Payload tab shows HPA off and switch RX; "
             "analyzer input attenuator in (the coupled port reaches +20 dBm at full drive); radio up on "
             f"the Ground Link tab with TX {a.tx_port}, {_rx_text(a)}, {a.rate/1e6:g} MSps; the bench "
             "front-end file (lna_floor false) loaded.\n"
             "Expected per step (fill the measured column of HPA_bench_power_sweep.xlsx):\n" + "\n".join(lines))
    plan = {
        "format": mission_file.FORMAT,
        "name": f"HPA bench power sweep {a.profile} {_dt.date.today():%Y-%m-%d}",
        "notes": notes,
        "authorization": {"reference": a.auth,
                          "note": "bench: HPA into the ZGDC30-372HP+ coupler and a dummy load, no antenna"},
        "anchor": {"mode": "on_tx", "date": f"{_dt.date.today():%Y-%m-%d}"},
        "radios": [{"id": a.radio, "type": "g40", "techniques": ["rx", "tx"], "address": a.address,
                    "sample_rate": int(a.rate)}],
        "steps": steps,
        "map": {},
    }
    return plan


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profiles", default=os.path.join(ROOT, "payload_profiles.yaml"))
    ap.add_argument("--profile", default="P2b")
    ap.add_argument("--radio", default="G40-0002", help="radio id as the GUI strip shows it")
    ap.add_argument("--address", default="192.168.0.23:47810")
    ap.add_argument("--rate", type=float, default=1.92e6)
    ap.add_argument("--tx-port", default="TRx1A")
    ap.add_argument("--rx-port", default="TRx2B", help="a G40 jack, or none for TX only steps")
    ap.add_argument("--freqs", default="915,2200", help="MHz")
    ap.add_argument("--attens", default="41.75,35,30,25,20,15,10", help="dB, planner choices only")
    ap.add_argument("--ampl", type=float, default=0.5)
    ap.add_argument("--tone-hz", type=float, default=100e3)
    ap.add_argument("--extra-loss", type=float, default=0.0)
    ap.add_argument("--hpa-in-margin", type=float, default=3.0)
    ap.add_argument("--dwell", type=float, default=20.0, help="seconds keyed per step")
    ap.add_argument("--gap", type=float, default=20.0, help="seconds of silence after each step")
    ap.add_argument("--lead", type=float, default=10.0, help="seconds before the first step")
    ap.add_argument("--auth", default=f"HPA-BENCH-{_dt.date.today():%Y-%m-%d}")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--g40-max-dbm", type=float, default=None, help="calibrated G40 max output (dBm)")
    a = ap.parse_args(argv)
    plan = build(a)
    problems = mission_file.validate(plan)
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    mission_file.save(plan, a.out)
    last = plan["steps"][-1]["schedule"]["stop_s"] if plan["steps"] else 0
    print(f"wrote {a.out}: {len(plan['steps'])} tone steps, {last/60:.1f} min")
    print(plan["notes"].split("Expected per step")[1] if plan["steps"] else "no steps")
    errs = [c for c in problems if getattr(c, "level", "error") == "error"]
    for c in problems:
        print(f"  check plan: {c.level}: {c.message}")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
