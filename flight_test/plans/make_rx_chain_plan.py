#!/usr/bin/env python3
"""Mission file: the ground G40 keys into the payload antenna port and the
payload G40 decodes through its RX chain.

  python3 make_rx_chain_plan.py
  python3 make_rx_chain_plan.py --utc 20:30
  python3 make_rx_chain_plan.py --tx-atten 30 --rx-atten 60

Written to plans/rx_chain/ next to this script, with a README.

Cable: the ground G40's TX jack to the payload antenna port (T/R switch J0).
The payload does not key in this plan, so the switch stays in RX and the signal
goes J0, J1, RX attenuator, LNA, splitter, payload G40 RX jack.

Each waveform is two steps that start together on the two radios:

  Decode step   payload G40: sets the RX attenuator, loads the decode plan for
                the waveform and receives.
  Comm step     ground G40: keys the same waveform 3 s later for 13 s.

Without --utc the step times count from Run mission. With --utc HH:MM the plan
is set to "Start all at planned UTC times" and the first waveform starts at that
time on --date; press Run mission at least two minutes before it.

Levels. A G40 gives about +20 dBm at full scale and 0 dB TX attenuation. These
waveforms average about 10 dB under full scale, so at the default 30 dB the
ground G40 puts about -20 dBm into J0. The RX attenuator comes before the LNA.
With it at 60 dB (and about 6 dB of its own loss) the LNA sees about -88 dBm and
the payload G40 about -65 dBm, a normal cabled level. Change --rx-atten if the
ground G40 comes in through a pad or a coupler: take the pad's dB off it.

Plans are checked with the repo's mission_file. Nothing here touches a radio.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "rx_chain")
NAME = "ax35_to_payload_rx"

FS = 1_920_000
SETUP_S = 65.0      # from Run mission to the first waveform: both radios are set to their jacks in it
LEAD_S = 3.0        # the Decode step starts this long before the key
KEY_S = 13.0        # each Comm step
PITCH_S = 22.0      # one waveform to the next

G40_FULL_SCALE_DBM = 20.0   # at 0 dB TX attenuation, bench 2 November
WAVEFORM_BACKOFF_DB = 10.0  # peak 0.45 of full scale, less the waveform's peak to average
SWITCH_LOSS_DB = 1.0
ATTEN_OWN_LOSS_DB = 6.0
LNA_GAIN_DB = 28.5          # ZRL-3500+ at 1300 MHz
SPLITTER_LOSS_DB = 5.0
LNA_MAX_IN_DBM = 10.0
MIN_TX_ATTEN_DB = 10.0      # ground G40 on this cable: keeps the LNA under its limit with the attenuator open

COMM_SET = [
    ("FSK", 4800, -400_000, {"tone_spacing_hz": 5000}, "RX CHAIN FSK"),
    ("4FSK", 4800, -300_000, {"tone_spacing_hz": 5000}, "RX CHAIN 4FSK"),
    ("BPSK", 9600, -200_000, {}, "RX CHAIN BPSK"),
    ("QPSK", 9600, -100_000, {}, "RX CHAIN QPSK"),
    ("OQPSK", 9600, 500_000, {}, "RX CHAIN OQPSK"),
    ("8PSK", 9600, 100_000, {}, "RX CHAIN 8PSK"),
    ("16QAM", 9600, 200_000, {}, "RX CHAIN 16QAM"),
    ("QPSK", 9600, 300_000, {"fec": {"type": "conv", "rate": "1/2"}}, "RX CHAIN QPSK CONV 1/2"),
    ("FSK", 4800, 400_000, {"tone_spacing_hz": 5000, "fec": {"type": "conv", "rate": "1/2"}},
     "RX CHAIN FSK CONV 1/2"),
]


def radio_row(name, address, clock=None):
    r = {"id": name, "type": "g40", "techniques": ["tx", "rx"], "sample_rate": FS}
    if address:
        r["address"] = address
    # the clock Run mission brings the radio up on when it is not set up yet
    if clock and clock != "internal":
        r["clock_source"] = clock
    return r


def spec(name, a, mod, sr, off, extra, msg):
    s = {"modulation": mod, "symbol_rate": sr, "offset_hz": off, "amplitude": 0.9, "message": msg}
    s.update(extra)
    return {"name": name, "sample_rate_hz": FS, "center_freq_hz": float(a.center), "loop_s": 0.2,
            "peak_fs": 0.45, "signals": [s]}


def decode_step(sid, a, sp, start, stop):
    return {"id": sid, "radio": a.payload, "technique": "scenario_decode", "radiates": False,
            "params": {"spec": sp, "spec_name": sp["name"], "source": "console", "spec_path": "",
                       "match_radio_rate": True, "rx_atten_db": float(a.rx_atten), "center_hz": float(a.center),
                       "rx_gain_index": None},
            "schedule": {"start_s": float(start), "stop_s": float(stop)},
            "ports": {"rx": a.rx}}


def comm_step(sid, a, sp, start, stop):
    return {"id": sid, "radio": a.ground, "technique": "scenario_json", "radiates": True,
            "params": {"spec": sp, "spec_name": sp["name"], "source": "console", "spec_path": "",
                       "match_radio_rate": True, "peak_fs": 0.45, "center_hz": float(a.center),
                       "tx_center_hz": None, "tx_atten_db": float(a.tx_atten), "rx_gain_index": None},
            "schedule": {"start_s": float(start), "stop_s": float(stop)},
            "ports": {"rx": "none", "tx": a.tx}}


def levels(a):
    """Estimated dBm at J0, at the LNA input and at the payload G40 RX jack."""
    j0 = G40_FULL_SCALE_DBM - WAVEFORM_BACKOFF_DB - a.tx_atten - a.path_loss
    lna = j0 - SWITCH_LOSS_DB - ATTEN_OWN_LOSS_DB - a.rx_atten
    return j0, lna, lna + LNA_GAIN_DB - SPLITTER_LOSS_DB


def clock(sec):
    sec = int(round(sec)) % 86400
    return f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"


def build(a):
    if a.utc:
        hh, mm = (int(x) for x in a.utc.split(":")[:2])
        t0 = hh * 3600 + mm * 60
        anchor = {"mode": "clock", "date": a.date, "tz": "UTC"}
        when = f"The first waveform starts at {clock(t0)} UTC on {a.date}; press Run mission at least two minutes before."
    else:
        t0 = SETUP_S
        anchor = {"mode": "on_tx", "date": a.date}
        when = f"Times count from Run mission; the first waveform starts at {clock(t0)}."
    j0, lna, g40 = levels(a)
    end = t0 + (len(COMM_SET) - 1) * PITCH_S + LEAD_S + KEY_S + 1.0
    m = {
        "format": "dpmg-mission/1",
        "name": f"Ground to payload RX chain: {a.ground} keys, {a.payload} decodes",
        "notes": (f"Cable {a.ground} {a.tx} to the payload antenna port (T/R switch J0). {a.ground} keys "
                  f"{len(COMM_SET)} comm waveforms in turn at TX attenuation {a.tx_atten:g} dB, {KEY_S:g} s each. "
                  f"{a.payload} decodes each on {a.rx} through the payload RX chain: switch, RX attenuator at "
                  f"{a.rx_atten:g} dB, LNA, splitter. {when} The last one ends at {clock(end)}. "
                  f"Estimated level: {j0:.0f} dBm at J0, {lna:.0f} dBm at the LNA, {g40:.0f} dBm at {a.payload}. "
                  f"Nothing keys on the payload in this plan. The payload must not key while {a.ground} is on "
                  f"the antenna port: the HPA output would go into {a.ground}. Keep {a.ground} at "
                  f"{MIN_TX_ATTEN_DB:g} dB TX attenuation or more on this cable."),
        "authorization": {"reference": a.auth,
                          "note": "cabled: ground G40 TX jack to the payload antenna port, no antenna, "
                                  "the payload does not key"},
        "anchor": anchor,
        "radios": [radio_row(a.payload, a.payload_address, a.payload_clock),
                   radio_row(a.ground, a.ground_address)],
        "steps": [],
    }
    t = t0
    for n, (mod, sr, off, extra, msg) in enumerate(COMM_SET, 1):
        sp = spec(f"rxc{n}", a, mod, sr, off, extra, msg)
        m["steps"].append(decode_step(f"rxc_rx{n:02d}", a, sp, t, t + LEAD_S + KEY_S + 1.0))
        m["steps"].append(comm_step(f"rxc_tx{n:02d}", a, sp, t + LEAD_S, t + LEAD_S + KEY_S))
        t += PITCH_S
    return m, t0, end


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=os.path.normpath(os.path.join(HERE, "..", "..")),
                    help="the flight repo, for the plan check")
    ap.add_argument("--center", type=float, default=1325e6, help="RF center of every step (Hz)")
    ap.add_argument("--payload", default="G40-AX3G", help="payload G40 row name (its serial name)")
    ap.add_argument("--payload-address", default="192.168.0.23:47810", help="payload G40 as IP:port, blank for none")
    ap.add_argument("--payload-clock", default="external",
                    choices=["internal", "external", "ext_10mhz", "ext_pps", "gpsdo"],
                    help="clock the payload radio comes up on when Run mission has to set it up: external is "
                         "the 10 MHz and the 1 PPS from the payload clock board")
    ap.add_argument("--ground", default="G40-AX35", help="ground G40 row name (its serial name)")
    ap.add_argument("--ground-address", default="192.168.0.22:47810", help="ground G40 as IP:port, blank for none")
    ap.add_argument("--rx", default="TRx1A", help="payload G40 RX jack")
    ap.add_argument("--tx", default="TRx1B", help="ground G40 TX jack")
    ap.add_argument("--tx-atten", type=float, default=30.0, help="ground G40 TX attenuation (dB)")
    ap.add_argument("--rx-atten", type=float, default=60.0, help="payload RX attenuator during the decodes (dB)")
    ap.add_argument("--path-loss", type=float, default=0.0,
                    help="pad or coupler between the ground G40 and J0 (dB), for the level estimate only")
    ap.add_argument("--utc", default="", help="start at this UTC clock time, HH:MM; blank counts from Run mission")
    ap.add_argument("--date", default=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d"),
                    help="UTC date of the plan")
    ap.add_argument("--auth", default="FLIGHT-2026-11", help="authorization reference written into the plan")
    a = ap.parse_args()

    if a.tx_atten < MIN_TX_ATTEN_DB:
        sys.exit(f"TX attenuation {a.tx_atten:g} dB is under {MIN_TX_ATTEN_DB:g} dB: with the RX attenuator open "
                 f"that is over the LNA's {LNA_MAX_IN_DBM:g} dBm input limit. Not written.")

    m, t0, end = build(a)
    j0, lna, g40 = levels(a)
    name = NAME + ("_utc" if a.utc else "")

    sys.path.insert(0, a.repo)
    from dpmg.mission import mission_file as mf

    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, f"{name}.dpmg-mission.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(m, f, indent=1)
        f.write("\n")
    found = mf.validate(mf.load(path))
    errs = [c for c in found if c.level == "error"]
    print(f"{name}: {len(m['steps'])} steps, {len(COMM_SET)} keyed, {clock(t0)} to {clock(end)}, "
          f"{len(errs)} error(s), {len(found) - len(errs)} other")
    for c in found:
        print(f"  {c.level}: {c.step_id}: {c.message}")
    print(f"levels: J0 {j0:.1f} dBm, LNA {lna:.1f} dBm, {a.payload} {g40:.1f} dBm")

    table = "\n".join(f"| {n} | {clock(t0 + (n - 1) * PITCH_S + LEAD_S)} | {c[0]} | {c[2] / 1e3:+.0f} | {c[4]} |"
                      for n, c in enumerate(COMM_SET, 1))
    start_txt = (f"{a.utc} UTC on {a.date} (the plan is set to planned UTC times; press Run mission at least "
                 "two minutes before)" if a.utc else "from Run mission")
    readme = f"""# Ground G40 into the payload RX chain

Written by make_rx_chain_plan.py. `{name}.dpmg-mission.json`: {a.ground} keys {len(COMM_SET)} comm
waveforms in turn and {a.payload} decodes each one through the payload RX chain. Center
{a.center / 1e6:g} MHz, 1.92 MS/s. Times are {start_txt}.

## Cable

{a.ground} jack {a.tx} to the payload antenna port (T/R switch J0). Nothing else on J0.

    {a.ground} {a.tx}, cable, switch J0, switch J1, RX attenuator, LNA, splitter, {a.payload} {a.rx}

The payload does not key in this plan, so the switch stays in RX and the HPA stays off.
The payload must not key while {a.ground} is on the antenna port: the HPA output would go
into {a.ground}. Take the cable off J0 before loading any plan that keys the payload.

## Levels

| Where | Estimate (dBm) | Limit (dBm) |
|---|---|---|
| J0, from {a.ground} at {a.tx_atten:g} dB TX attenuation | {j0:.0f} | |
| LNA input, RX attenuator at {a.rx_atten:g} dB | {lna:.0f} | {LNA_MAX_IN_DBM:g} |
| {a.payload} {a.rx} | {g40:.0f} | 20 |

Keep {a.ground} at {MIN_TX_ATTEN_DB:g} dB TX attenuation or more on this cable; the script refuses less.
Too weak or too strong at {a.payload}: change the RX attenuator, 10 dB at a time, with
`python3 make_rx_chain_plan.py --rx-atten 50` and load the file again. If {a.ground} comes in
through a pad or a coupler, take that many dB off the RX attenuator setting.

## Run it

1. Both radios connected in the ground station. {a.payload} shows the RX attenuator as connected.
2. Mission view, Open file, `{name}.dpmg-mission.json`. Check plan: no errors.
3. Run mission. In the first minute each radio is set to its jack; nothing keys.
4. Watch Decodes on {a.payload}: one message per waveform, in the order below.

| No | Keys at | Waveform | Offset (kHz) | Message |
|---|---|---|---|---|
{table}

Each keys for {KEY_S:g} s. The last one ends at {clock(end)}.

## Other set-ups

    python3 make_rx_chain_plan.py --utc 20:30                 start at a UTC clock time today
    python3 make_rx_chain_plan.py --utc 20:30 --date 2026-11-18
    python3 make_rx_chain_plan.py --ground G40-AX3B --ground-address 192.168.0.24:47810

## Status

Checked in the planner's plan check only. Not run on the radios yet.
"""
    with open(os.path.join(OUT, "README.md"), "w", encoding="utf-8") as f:
        f.write(readme)
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
