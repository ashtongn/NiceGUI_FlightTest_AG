#!/usr/bin/env python3
"""G40 pair plan with comms both ways in turn, the ground G40 on two jacks.

  python3 make_twoway_pair_plan.py
  python3 make_twoway_pair_plan.py --ground-atten 20 --air-rx-atten 20
  python3 make_twoway_pair_plan.py --repeats 3

Written to plans/pairs_twoway/ next to this script, with a README.

The ground G40 receives on one jack and transmits on another, each on its own
antenna, with a small HPA after the TX jack. The payload has one antenna behind
its T/R switch, so the two directions take turns:

  down   the payload keys a comm waveform through its HPA, the ground G40
         decodes it on its RX jack
  up     the ground G40 keys the same kind of waveform on its TX jack, the
         payload decodes it on TRx1A through its RX chain

Every step names both jacks of its radio (payload RX TRx1A and TX TRx2B, ground
RX and TX as given), so each radio is set up once in the first minute and is
not re-initialized between the directions. pairs/g40_pair runs all of the
downlink, re-inits the payload, then runs the uplink; this file goes down, up,
down, up, one waveform at a time.

The ground HPA is not known to this script. --ground-atten is the ground G40's
TX attenuation in front of it: set it for that HPA's input limit and gain
before keying. Plans are checked with the repo's mission_file. Nothing here
touches a radio.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "pairs_twoway")
NAME = "g40_twoway"
sys.path.insert(0, HERE)
import make_flight_plans as fp  # noqa: E402

FS = 1_920_000
SETUP_S = 65.0      # from Run mission to the first waveform: both radios are set to their jacks in it
LEAD_S = 3.0        # the Decode step starts this long before the key
KEY_S = 13.0        # each Comm step
TURN_S = 21.0       # from the start of one direction to the start of the other
CYCLE_S = 2 * TURN_S


def hms(sec):
    sec = int(round(sec))
    return f"{sec // 60:02d}:{sec % 60:02d}"


def waveforms(a):
    """(down signal, up signal) for each turn: the waveforms the payload decodes on the bench,
    the same kind each way, with the pair plan's messages."""
    out = []
    for mod, sr, off, extra, msg in fp.COMM_SET[2:6]:
        out.append((fp.sig(mod, sr, off, extra, msg), fp.sig(mod, sr, off, extra, msg.replace("COMM", "UPLINK"))))
    for c, off, msg, k in fp.DSSS_SET:
        if c in a.dsss:
            out.append((fp.dsss_sig(c, off, msg, k), fp.dsss_sig(c, off, "UPLINK " + msg, k)))
    return out


def both_jacks(step, rx, tx):
    step["ports"] = {"rx": rx, "tx": tx}
    return step


def build(a):
    g = fp.Ground("g40", a.ground, a.ground_address)
    pairs = waveforms(a)
    n = len(pairs) * a.repeats
    end = SETUP_S + n * CYCLE_S - (CYCLE_S - TURN_S - LEAD_S - KEY_S)
    m = fp.base(f"Pair G40, both ways in turn: {a.air_radio} and {g.radio}, ground on two jacks",
                f"{len(pairs)} comm waveforms, each sent down and then up. Down: {a.air_radio} keys on {a.tx_port} "
                f"through the HPA at TX attenuation {a.tx_atten:g} dB for {KEY_S:g} s and {g.radio} decodes on "
                f"{a.ground_rx}. Up: {g.radio} keys on {a.ground_tx} into the ground HPA at TX attenuation "
                f"{a.ground_atten:g} dB for {KEY_S:g} s and {a.air_radio} decodes on {a.air_rx_port} through the "
                f"payload RX chain, RX attenuator {a.air_rx_atten:g} dB. {g.radio} receives on {a.ground_rx} and "
                f"transmits on {a.ground_tx}, each on its own antenna. Both radios are set to their two jacks in "
                f"the first minute and are not re-initialized between the directions. Starts on Run mission: "
                f"the first downlink keys at {hms(SETUP_S + LEAD_S)}, the first uplink at "
                f"{hms(SETUP_S + TURN_S + LEAD_S)}, the last step ends at {hms(end)}"
                + (f", {a.repeats} passes back to back." if a.repeats > 1 else ".")
                + f" Set the ground TX attenuation for the ground HPA before keying.",
                a, radios=[fp.air_radio(a, FS), fp.ground_radio(g, FS)])
    m["authorization"]["note"] = "open air: the payload on its antenna, the ground G40 on its own TX and RX antennas"
    rows = []
    t, k = SETUP_S, 0
    for _rep in range(a.repeats):
        for i, (down, up) in enumerate(pairs, 1):
            k += 1
            sp = fp.spec(f"dl{i}", FS, a, [down])
            m["steps"].append(both_jacks(
                fp.decode_step(f"dl_rx{k:02d}", a, sp, t, t + LEAD_S + KEY_S + 1.0, g.radio, a.ground_rx, None),
                a.ground_rx, a.ground_tx))
            m["steps"].append(both_jacks(
                fp.comm_step(f"dl_tx{k:02d}", a, sp, t + LEAD_S, t + LEAD_S + KEY_S, a.tx_atten),
                a.air_rx_port, a.tx_port))
            u = t + TURN_S
            sp = fp.spec(f"ul{i}", FS, a, [up])
            m["steps"].append(both_jacks(
                fp.decode_step(f"ul_rx{k:02d}", a, sp, u, u + LEAD_S + KEY_S + 1.0, a.air_radio, a.air_rx_port,
                               rx_atten=a.air_rx_atten),
                a.air_rx_port, a.tx_port))
            m["steps"].append(both_jacks(
                fp.comm_step(f"ul_tx{k:02d}", a, sp, u + LEAD_S, u + LEAD_S + KEY_S, a.ground_atten,
                             radio=g.radio, tx_port=a.ground_tx),
                a.ground_rx, a.ground_tx))
            if _rep == 0:
                rows.append(f"| {i} | {hms(t + LEAD_S)} | {down['message']} | {hms(u + LEAD_S)} | {up['message']} |")
            t += CYCLE_S
    return m, rows, end


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=os.path.normpath(os.path.join(HERE, "..", "..")),
                    help="the flight repo, for the plan check")
    ap.add_argument("--center", type=float, default=1325e6, help="RF center of every step (Hz)")
    ap.add_argument("--air-radio", default="G40-AX3G", help="the payload G40's name in the plan")
    ap.add_argument("--air-address", default="192.168.0.23:47810", help="its address as IP:port (blank for none)")
    ap.add_argument("--air-clock", default="external",
                    choices=["internal", "external", "ext_10mhz", "ext_pps", "gpsdo"],
                    help="clock the payload radio comes up on when Run mission has to set it up")
    ap.add_argument("--tx-port", default="TRx2B", help="the payload TX jack")
    ap.add_argument("--air-rx-port", default="TRx1A", help="the payload RX jack")
    ap.add_argument("--tx-atten", type=float, default=10.0, help="payload TX attenuation on the downlink (dB)")
    ap.add_argument("--air-rx-atten", type=float, default=0.0,
                    help="payload RX attenuator during the uplink decodes (dB); raise it when the radios are close")
    ap.add_argument("--ground", default="G40-AX35", help="the ground G40's name in the plan")
    ap.add_argument("--ground-address", default="192.168.0.22:47810")
    ap.add_argument("--ground-rx", default="TRx1A", help="the ground G40 RX jack, to its RX antenna")
    ap.add_argument("--ground-tx", default="TRx1B", help="the ground G40 TX jack, to the ground HPA")
    ap.add_argument("--ground-atten", type=float, default=30.0,
                    help="ground G40 TX attenuation in front of the ground HPA (dB)")
    ap.add_argument("--repeats", type=int, default=1, help="passes through the waveform set, back to back")
    ap.add_argument("--dsss", default="bpsk,qpsk,oqpsk", help="the DSSS waveforms to include (blank for none)")
    ap.add_argument("--date", default=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d"))
    ap.add_argument("--auth", default="FLIGHT-2026-11")
    a = ap.parse_args()
    a.dsss = [x.strip().lower() for x in a.dsss.split(",") if x.strip()]
    a.repeats = max(1, a.repeats)
    if a.ground_rx == a.ground_tx:
        sys.exit("the ground RX and TX jacks are the same: this plan is for a ground G40 on two jacks. Not written.")

    m, rows, end = build(a)
    sys.path.insert(0, a.repo)
    from dpmg.mission import mission_file as mf

    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, f"{NAME}.dpmg-mission.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(m, f, indent=1)
        f.write("\n")
    found = mf.validate(mf.load(path))
    errs = [c for c in found if c.level == "error"]
    keyed_air = sum(1 for s in m["steps"] if s["radiates"] and s["radio"] == a.air_radio)
    keyed_gnd = sum(1 for s in m["steps"] if s["radiates"] and s["radio"] != a.air_radio)
    print(f"{NAME}: {len(m['steps'])} steps, {keyed_air} keyed on {a.air_radio}, {keyed_gnd} on {a.ground}, "
          f"ends {hms(end)}, {len(errs)} error(s), {len(found) - len(errs)} other")
    for c in found:
        print(f"  {c.level}: {c.step_id}: {c.message}")

    passes = f"{a.repeats} passes back to back; the times below are the first pass." if a.repeats > 1 else "One pass."
    readme = f"""# G40 pair plan, both ways in turn

Written by make_twoway_pair_plan.py. `{NAME}.dpmg-mission.json`: comm waveforms sent down from the
payload and up from the ground, one after the other, with the ground G40 on two jacks. Center
{a.center / 1e6:g} MHz, 1.92 MS/s on both radios. {passes} The last step ends at {hms(end)}.

## Cabling

| Radio | Jack | Goes to |
|---|---|---|
| {a.ground} | {a.ground_rx} | the ground RX antenna |
| {a.ground} | {a.ground_tx} | the ground HPA, then the ground TX antenna |
| {a.air_radio} | {a.tx_port} | 20 dB pad, 1189 HPA, T/R switch, payload antenna |
| {a.air_radio} | {a.air_rx_port} | payload RX chain, from the same antenna through the T/R switch |

The payload has one antenna, so it cannot send and receive at the same moment. The two
directions take turns, {TURN_S:g} s apart.

## Settings to check before keying

| Setting | Value in this file | What sets it |
|---|---|---|
| Payload TX attenuation, downlink | {a.tx_atten:g} dB | the T/R switch rating and the range |
| Ground G40 TX attenuation, uplink | {a.ground_atten:g} dB | the ground HPA: its input limit and its gain. Not known to this script |
| Payload RX attenuator, uplink | {a.air_rx_atten:g} dB | how strong the uplink is at the payload |

The ground G40 gives about +20 dBm at full scale and 0 dB TX attenuation, and these
waveforms average about 7 dB under that. At {a.ground_atten:g} dB it puts about {20 - 7 - a.ground_atten:.0f} dBm into the
ground HPA. Work out what that HPA then puts into its antenna before the first key.
With the two radios close together on the ground the uplink is strong: raise the payload
RX attenuator with `--air-rx-atten 30` for a ground check and take it back down to fly.
The ground G40 does not switch its HPA on or off; that is done at the HPA.

## Run it

1. Both radios connected. Ground RX and TX antennas apart from each other.
2. Mission view, Open file. Check plan: no errors.
3. Run mission, read the confirm, key it. Nothing keys in the first minute.
4. Watch Decodes on {a.ground} for the downlink messages and on {a.air_radio} for the uplink
   messages, in the order below.
5. STOP ALL TX to end early.

| Turn | Down keys at | Down message ({a.ground} decodes) | Up keys at | Up message ({a.air_radio} decodes) |
|---|---|---|---|---|
{chr(10).join(rows)}

## Change it

    python3 make_twoway_pair_plan.py --ground-atten 20            more drive into the ground HPA
    python3 make_twoway_pair_plan.py --air-rx-atten 30            radios close together
    python3 make_twoway_pair_plan.py --ground-rx TRx2A --ground-tx TRx1B
    python3 make_twoway_pair_plan.py --repeats 3                  three passes, for a longer leg

## Status

Checked in the planner's plan check only. Not run on the radios.
"""
    with open(os.path.join(OUT, "README.md"), "w", encoding="utf-8") as f:
        f.write(readme)
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
