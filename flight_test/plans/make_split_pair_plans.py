#!/usr/bin/env python3
"""The G40 pair plan split in two: one file where the payload only transmits
and one where the payload only receives.

  python3 make_split_pair_plans.py
  python3 make_split_pair_plans.py --repeats 3
  python3 make_split_pair_plans.py --tx-atten 20 --ground-atten 10 --air-rx-atten 20

Written to plans/pairs_split/ next to this script, with a README.

pairs/g40_pair runs the downlink, then the uplink, then the range gates on one
timeline, and the payload G40 re-inits from transmit to receive in the middle.
These two files take the first two sections apart, so each can be run on its
own and as often as wanted:

  g40_payload_tx   the payload keys each comm waveform through the HPA and the
                   ground G40 decodes it. The payload does not receive.
  g40_payload_rx   the ground G40 keys each waveform and the payload decodes it
                   on TRx1A through its RX chain. Nothing keys on the payload,
                   so the HPA stays off.

Both start on Run mission. The first minute sets each radio to its jack; the
first waveform keys at 01:08. Each waveform keys for 13 s, one every 22 s.
--repeats runs the whole set again, back to back.

The waveforms and messages are those of pairs/g40_pair. Plans are checked with
the repo's mission_file. Nothing here touches a radio.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "pairs_split")
sys.path.insert(0, HERE)
import make_flight_plans as fp  # noqa: E402

SETUP_S = 65.0      # from Run mission to the first waveform: both radios are set to their jacks in it
LEAD_S = 3.0        # the Decode step starts this long before the key
KEY_S = 13.0        # each Comm step
PITCH_S = 22.0      # one waveform to the next
FS = 1_920_000


def hms(sec):
    sec = int(round(sec))
    return f"{sec // 60:02d}:{sec % 60:02d}"


def down_set(a):
    out = [fp.sig(mod, sr, off, extra, msg) for mod, sr, off, extra, msg in fp.COMM_SET]
    return out + [fp.dsss_sig(c, off, msg, k) for c, off, msg, k in fp.DSSS_SET if c in a.dsss]


def up_set(a):
    out = [fp.sig(mod, sr, off, extra, msg.replace("COMM", "UPLINK")) for mod, sr, off, extra, msg in fp.COMM_SET[2:6]]
    return out + [fp.dsss_sig(c, off, "UPLINK " + msg, k) for c, off, msg, k in fp.DSSS_SET if c in a.dsss]


def radios(a, g):
    return [fp.air_radio(a, FS), fp.ground_radio(g, FS)]


def timeline(a, sigs, tag):
    """(step id number, spec, start of the Decode step) for every waveform of every pass."""
    t, n = SETUP_S, 0
    for _rep in range(a.repeats):
        for i, sg in enumerate(sigs, 1):
            n += 1
            yield n, fp.spec(f"{tag}{i}", FS, a, [sg]), t
            t += PITCH_S


def plan_payload_tx(a, g):
    sigs = down_set(a)
    end = SETUP_S + a.repeats * len(sigs) * PITCH_S
    m = fp.base(f"Pair G40, payload transmits: {a.air_radio} keys, {g.radio} decodes",
                f"{a.air_radio} keys {len(sigs)} comm waveforms in turn on {a.tx_port} through the HPA at TX "
                f"attenuation {a.tx_atten:g} dB, {KEY_S:g} s each, and {g.radio} decodes each on {g.rx_port}. "
                f"The payload does not receive in this plan. Starts on Run mission: the first minute sets each "
                f"radio to its jack, the first waveform keys at {hms(SETUP_S + LEAD_S)}, the last ends at "
                f"{hms(end - PITCH_S + LEAD_S + KEY_S)}"
                + (f", {a.repeats} passes back to back." if a.repeats > 1 else "."),
                a, radios=radios(a, g))
    for n, sp, t in timeline(a, sigs, "dl"):
        m["steps"].append(fp.decode_step(f"dl_rx{n:02d}", a, sp, t, t + LEAD_S + KEY_S + 1.0, g.radio, g.rx_port, None))
        m["steps"].append(fp.comm_step(f"dl_tx{n:02d}", a, sp, t + LEAD_S, t + LEAD_S + KEY_S, a.tx_atten))
    return m, sigs


def plan_payload_rx(a, g):
    sigs = up_set(a)
    end = SETUP_S + a.repeats * len(sigs) * PITCH_S
    m = fp.base(f"Pair G40, payload receives: {g.radio} keys, {a.air_radio} decodes",
                f"{g.radio} keys {len(sigs)} comm waveforms in turn on {g.tx_port} at TX attenuation "
                f"{a.ground_atten:g} dB, {KEY_S:g} s each, and {a.air_radio} decodes each on {a.air_rx_port} through "
                f"the payload RX chain with the RX attenuator at {a.air_rx_atten:g} dB. Nothing keys on the payload, "
                f"so the HPA stays off. Starts on Run mission: the first minute sets each radio to its jack, the "
                f"first waveform keys at {hms(SETUP_S + LEAD_S)}, the last ends at "
                f"{hms(end - PITCH_S + LEAD_S + KEY_S)}"
                + (f", {a.repeats} passes back to back." if a.repeats > 1 else "."),
                a, radios=radios(a, g))
    m["authorization"]["note"] = "the ground G40 keys on its own antenna, the payload does not key"
    for n, sp, t in timeline(a, sigs, "ul"):
        m["steps"].append(fp.decode_step(f"ul_rx{n:02d}", a, sp, t, t + LEAD_S + KEY_S + 1.0, a.air_radio,
                                         a.air_rx_port, rx_atten=a.air_rx_atten))
        m["steps"].append(fp.comm_step(f"ul_tx{n:02d}", a, sp, t + LEAD_S, t + LEAD_S + KEY_S, a.ground_atten,
                                       radio=g.radio, tx_port=g.tx_port))
    return m, sigs


def sig_rows(sigs):
    rows = []
    for i, s in enumerate(sigs, 1):
        rate = f"{s['symbol_rate']} Bd" if "symbol_rate" in s else f"{s['chip_rate_hz'] / 1e3:g} kchip/s"
        rows.append(f"| {i} | {hms(SETUP_S + (i - 1) * PITCH_S + LEAD_S)} | {s['modulation']} | {rate} | "
                    f"{s['offset_hz'] / 1e3:+.0f} | {s['message']} |")
    return "\n".join(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=os.path.normpath(os.path.join(HERE, "..", "..")),
                    help="the flight repo, for the plan check")
    ap.add_argument("--center", type=float, default=1325e6, help="RF center of every step (Hz)")
    ap.add_argument("--air-radio", default="G40-AX3G", help="the payload G40's name in the plans")
    ap.add_argument("--air-address", default="192.168.0.23:47810", help="its address as IP:port (blank for none)")
    ap.add_argument("--tx-port", default="TRx2B", help="the payload TX jack")
    ap.add_argument("--air-clock", default="external",
                    choices=["internal", "external", "ext_10mhz", "ext_pps", "gpsdo"],
                    help="clock the payload radio comes up on when Run mission has to set it up: external is "
                         "the 10 MHz and the 1 PPS from the payload clock board")
    ap.add_argument("--air-rx-port", default="TRx1A", help="the payload RX jack")
    ap.add_argument("--tx-atten", type=float, default=30.0, help="payload TX attenuation, payload transmits (dB)")
    ap.add_argument("--ground", default="G40-AX35", help="the ground G40's name in the plans")
    ap.add_argument("--ground-address", default="192.168.0.22:47810")
    ap.add_argument("--ground-atten", type=float, default=0.0, help="ground G40 TX attenuation, payload receives (dB)")
    ap.add_argument("--air-rx-atten", type=float, default=0.0,
                    help="payload RX attenuator during the decodes, payload receives (dB)")
    ap.add_argument("--repeats", type=int, default=1, help="passes through the waveform set, back to back")
    ap.add_argument("--dsss", default="bpsk,qpsk,oqpsk", help="the DSSS waveforms to include (blank for none)")
    ap.add_argument("--date", default=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d"))
    ap.add_argument("--auth", default="FLIGHT-2026-11")
    a = ap.parse_args()
    a.dsss = [x.strip().lower() for x in a.dsss.split(",") if x.strip()]
    a.repeats = max(1, a.repeats)
    g = fp.Ground("g40", a.ground, a.ground_address)

    sys.path.insert(0, a.repo)
    from dpmg.mission import mission_file as mf

    os.makedirs(OUT, exist_ok=True)
    rows, tables, bad = [], [], 0
    for name, (m, sigs), keys in (("g40_payload_tx", plan_payload_tx(a, g), a.air_radio),
                                  ("g40_payload_rx", plan_payload_rx(a, g), g.radio)):
        path = os.path.join(OUT, f"{name}.dpmg-mission.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(m, f, indent=1)
            f.write("\n")
        found = mf.validate(mf.load(path))
        errs = [c for c in found if c.level == "error"]
        bad += len(errs)
        keyed = sum(1 for s in m["steps"] if s["radiates"])
        end = max(s["schedule"]["stop_s"] for s in m["steps"])
        rows.append(f"| `{name}.dpmg-mission.json` | {m['name']} | {keys} | {keyed} | {hms(end)} | "
                    f"{'valid' if not errs else 'errors'} |")
        tables.append(f"### {name}\n\n| No | Keys at | Waveform | Rate | Offset (kHz) | Message |\n|---|---|---|---|---|---|\n"
                      + sig_rows(sigs))
        print(f"{name}: {len(m['steps'])} steps, {keyed} keyed, ends {hms(end)}, {len(errs)} error(s), "
              f"{len(found) - len(errs)} other")
        for c in found:
            print(f"  {c.level}: {c.step_id}: {c.message}")

    passes = f"{a.repeats} passes back to back; the times below are the first pass." if a.repeats > 1 else "One pass."
    readme = f"""# G40 pair plans, split by direction

Written by make_split_pair_plans.py. The downlink and the uplink of `pairs/g40_pair` as two
files, so each runs on its own. Center {a.center / 1e6:g} MHz, 1.92 MS/s on both radios, payload
{a.air_radio}, ground radio {a.ground}. {passes}

| File | Plan name | Keys | Keyed steps | Ends | Check |
|---|---|---|---|---|---|
{chr(10).join(rows)}

## Which radio does what

| | g40_payload_tx | g40_payload_rx |
|---|---|---|
| {a.air_radio} | keys on {a.tx_port} through the HPA, {a.tx_atten:g} dB | receives on {a.air_rx_port}, RX attenuator {a.air_rx_atten:g} dB |
| {a.ground} | receives on {g.rx_port} | keys on {g.tx_port}, {a.ground_atten:g} dB |
| HPA | keyed on every step | stays off |

In g40_payload_rx the ground G40 has no amplifier: at 0 dB TX attenuation it gives about
+10 dBm into its antenna cable. If the payload's decodes are weak, that is the first limit.
If they are too strong with the radios close together on the ground, raise the RX
attenuator with `--air-rx-atten 20` and load the file again.

## Run it

1. Both radios connected. The payload on its antenna, the ground G40 on its own antenna.
2. Mission view, Open file. Check plan: no errors.
3. Run mission, read the confirm, key it. Nothing keys in the first minute.
4. Watch Decodes on the receiving radio: one message per waveform, in the order below.
5. STOP ALL TX to end early.

Run one file, let it finish, then open the other. Going from payload transmits to payload
receives, the payload G40 changes jacks in the first minute of the second file.

## The waveforms

{(chr(10) + chr(10)).join(tables)}

## Change it

    python3 make_split_pair_plans.py --repeats 3              three passes, for a longer flight leg
    python3 make_split_pair_plans.py --tx-atten 20            more payload drive
    python3 make_split_pair_plans.py --ground-atten 10 --air-rx-atten 20
    python3 make_split_pair_plans.py --dsss ""                without the DSSS waveforms

## Status

Checked in the planner's plan check only. Not run on the radios.
"""
    with open(os.path.join(OUT, "README.md"), "w", encoding="utf-8") as f:
        f.write(readme)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
