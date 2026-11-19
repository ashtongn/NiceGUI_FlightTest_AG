#!/usr/bin/env python3
"""Example mission files that loop one G40 back on itself over a cable.

  python3 make_loopback_plans.py
  python3 make_loopback_plans.py --center 915e6 --rx TRx1A --tx TRx1B --tx-atten 30

Written to plans/loopback/ next to this script, with a README. They are for a
bare G40 on the bench: one cable with a pad from the TX jack to the RX jack, no
antenna and no amplifier. They are examples to copy from, not flight cards.

The planner does not let two steps overlap on one radio, so a loopback is two
steps one after the other on the same pair of jacks:

  Decode step   builds the scenario without arming TX, loads its decode plan and
                starts RX. It asks for both jacks, so the radio is set to the RX
                and the TX jack here and not again when the next step keys.
  Comm step     the same scenario on the same jacks, keyed. RX is still running
                on the plan the Decode step loaded, so the radio decodes what it
                sends and the messages show in its Decodes table.

Check plan gives one warning per Decode step ("no Comm waveform step on air
during it"). That is this layout and can be left. The first Decode step is long
because a G40 takes up to 50 s to change jacks; nothing keys during it.

The one row is named by --radio. Give it the G40's serial name (for example
--radio G40-AX35) and the row binds to that radio when it is online. Left at
the placeholder G40-LOOP, bind it in Mission radios with "bind to a connected
radio". Plans are validated with the repo's
mission_file. Nothing here touches a radio.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "loopback")

FS = 1_920_000
SETUP_S = 60.0      # first Decode step: time for the radio to be set to the two jacks
LOAD_S = 3.0        # later Decode steps on the same jacks: load the plan only
KEY_S = 13.0        # each Comm step
GAP_S = 5.0         # quiet time between waveforms
MOVE_GAP_S = 60.0   # port walk: quiet time before each move, the radio changes jacks in it
MOVE_S = 30.0       # port walk: Decode step of each move, the rest of the time for the cable

# the narrowband comm set of the flight plans (make_flight_plans.COMM_SET), with loopback messages
COMM_SET = [
    ("FSK", 4800, -400_000, {"tone_spacing_hz": 5000}, "LOOP FSK"),
    ("4FSK", 4800, -300_000, {"tone_spacing_hz": 5000}, "LOOP 4FSK"),
    ("BPSK", 9600, -200_000, {}, "LOOP BPSK"),
    ("QPSK", 9600, -100_000, {}, "LOOP QPSK"),
    ("OQPSK", 9600, 500_000, {}, "LOOP OQPSK"),
    ("8PSK", 9600, 100_000, {}, "LOOP 8PSK"),
    ("16QAM", 9600, 200_000, {}, "LOOP 16QAM"),
    ("QPSK", 9600, 300_000, {"fec": {"type": "conv", "rate": "1/2"}}, "LOOP QPSK CONV 1/2"),
    ("FSK", 4800, 400_000, {"tone_spacing_hz": 5000, "fec": {"type": "conv", "rate": "1/2"}}, "LOOP FSK CONV 1/2"),
]

# cable moves of the port walk: (TX jack, RX jack), always one card to the other.
# Every jack sends once and receives at least once.
WALK = [("TRx1B", "TRx1A"), ("TRx2B", "TRx1A"), ("TRx1A", "TRx1B"),
        ("TRx2A", "TRx1B"), ("TRx1B", "TRx2A"), ("TRx1A", "TRx2B")]


def sig(mod, sr, off, extra, msg):
    s = {"modulation": mod, "symbol_rate": sr, "offset_hz": off, "amplitude": 0.9, "message": msg}
    s.update(extra)
    return s


def spec(name, a, signals):
    return {"name": name, "sample_rate_hz": FS, "center_freq_hz": float(a.center), "loop_s": 0.2,
            "peak_fs": 0.45, "signals": signals}


def base(name, notes, a):
    return {
        "format": "dpmg-mission/1",
        "name": name,
        "notes": notes,
        "authorization": {"reference": a.auth,
                          "note": "cabled bench loopback: TX jack to RX jack through a pad, no antenna, no amplifier"},
        "anchor": {"mode": "on_tx"},
        "radios": [radio_row(a)],
        "steps": [],
    }


def radio_row(a):
    r = {"id": a.radio, "type": "g40", "techniques": ["tx", "rx"], "sample_rate": FS}
    if a.address:
        r["address"] = a.address
    return r


def decode_step(sid, a, sp, start, stop, rx, tx):
    return {"id": sid, "radio": a.radio, "technique": "scenario_decode", "radiates": False,
            "params": {"spec": sp, "spec_name": sp["name"], "source": "console", "spec_path": "",
                       "match_radio_rate": True, "rx_atten_db": None, "center_hz": float(a.center),
                       "rx_gain_index": None},
            "schedule": {"start_s": float(start), "stop_s": float(stop)},
            "ports": {"rx": rx, "tx": tx}}


def comm_step(sid, a, sp, start, stop, rx, tx):
    return {"id": sid, "radio": a.radio, "technique": "scenario_json", "radiates": True,
            "params": {"spec": sp, "spec_name": sp["name"], "source": "console", "spec_path": "",
                       "match_radio_rate": True, "peak_fs": 0.45, "center_hz": float(a.center),
                       "tx_center_hz": None, "tx_atten_db": float(a.tx_atten), "rx_gain_index": None},
            "schedule": {"start_s": float(start), "stop_s": float(stop)},
            "ports": {"rx": rx, "tx": tx}}


def add_loop(m, a, n, sp, t, rx, tx, load_s):
    """One loopback: Decode for load_s, then Comm for KEY_S. Returns the time after it."""
    m["steps"].append(decode_step(f"loop_rx{n:02d}", a, sp, t, t + load_s, rx, tx))
    m["steps"].append(comm_step(f"loop_tx{n:02d}", a, sp, t + load_s, t + load_s + KEY_S, rx, tx))
    return t + load_s + KEY_S + GAP_S


def cable(a, rx=None, tx=None):
    return f"{tx or a.tx} to {rx or a.rx} through the pad"


def plan_one(a):
    m = base(f"Loopback: one FSK message, {a.tx} to {a.rx}",
             f"One G40, cable {cable(a)}. The radio is set to the two jacks in the first minute, then keys "
             f"one FSK message for {KEY_S:g} s at TX attenuation {a.tx_atten:g} dB and decodes it itself: "
             f"LOOP FSK in the Decodes table.", a)
    mod, sr, off, extra, msg = COMM_SET[0]
    add_loop(m, a, 1, spec("loop1", a, [sig(mod, sr, off, extra, msg)]), 5.0, a.rx, a.tx, SETUP_S)
    return m


def plan_comm_set(a):
    m = base(f"Loopback: nine comm waveforms in turn, {a.tx} to {a.rx}",
             f"One G40, cable {cable(a)}. Nine waveforms one after the other, each keyed for {KEY_S:g} s "
             f"at TX attenuation {a.tx_atten:g} dB and decoded by the same radio: FSK, 4FSK, BPSK, QPSK, "
             f"OQPSK, 8PSK, 16QAM, then QPSK and FSK with rate 1/2 convolutional coding.", a)
    t = 5.0
    for n, (mod, sr, off, extra, msg) in enumerate(COMM_SET, 1):
        t = add_loop(m, a, n, spec(f"loop{n}", a, [sig(mod, sr, off, extra, msg)]), t, a.rx, a.tx,
                     SETUP_S if n == 1 else LOAD_S)
    return m


def plan_eight(a):
    offs = [-700_000, -500_000, -300_000, -100_000, 100_000, 300_000, 500_000, 700_000]
    sigs = [sig("FSK", 4800, off, {"tone_spacing_hz": 5000}, f"LOOP FSK {i}") for i, off in enumerate(offs, 1)]
    m = base(f"Loopback: eight FSK signals at once, {a.tx} to {a.rx}",
             f"One G40, cable {cable(a)}. One scenario with eight FSK signals 200 kHz apart, keyed together "
             f"for {2 * KEY_S:g} s at TX attenuation {a.tx_atten:g} dB. The radio decodes all eight: "
             f"LOOP FSK 1 to LOOP FSK 8.", a)
    sp = spec("loop8", a, sigs)
    m["steps"].append(decode_step("loop_rx01", a, sp, 5.0, 5.0 + SETUP_S, a.rx, a.tx))
    m["steps"].append(comm_step("loop_tx01", a, sp, 5.0 + SETUP_S, 5.0 + SETUP_S + 2 * KEY_S, a.rx, a.tx))
    return m


def plan_walk(a):
    lines = "; ".join(f"{n}: {tx} to {rx}" for n, (tx, rx) in enumerate(WALK, 1))
    m = base("Loopback: port walk, six cable moves",
             f"One G40 and one cable with the pad, moved six times. Before each move nothing is keyed for "
             f"{MOVE_GAP_S + MOVE_S:g} s ({MOVE_GAP_S:g} s gap, then a {MOVE_S:g} s Decode step): the radio "
             f"changes jacks and the cable is moved then. After it one FSK message naming the jacks is "
             f"keyed for {KEY_S:g} s at TX attenuation {a.tx_atten:g} dB. "
             f"Cable for each move, TX jack to RX jack: {lines}. The cable for move 1 can be fitted before "
             f"Run mission.", a)
    t = 5.0
    for n, (tx, rx) in enumerate(WALK, 1):
        msg = f"LOOP {tx[3:]} TO {rx[3:]}"
        sp = spec(f"walk{n}", a, [sig("FSK", 4800, -400_000, {"tone_spacing_hz": 5000}, msg)])
        t = add_loop(m, a, n, sp, t, rx, tx, SETUP_S if n == 1 else MOVE_S) - GAP_S + MOVE_GAP_S
    return m


def hms(sec):
    sec = int(round(sec))
    return f"{sec // 60:02d}:{sec % 60:02d}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=os.path.normpath(os.path.join(HERE, "..", "..")),
                    help="the flight repo, for validation")
    ap.add_argument("--center", type=float, default=1325e6, help="RF center of every step (Hz)")
    ap.add_argument("--radio", default="G40-LOOP",
                    help="the radio row's name in the plans: the G40's serial name, or a placeholder to bind by hand")
    ap.add_argument("--address", default="", help="the G40's address as IP:port, so the plan connects to it (blank for none)")
    ap.add_argument("--rx", default="TRx1A", help="RX jack of the fixed cable plans")
    ap.add_argument("--tx", default="TRx1B", help="TX jack of the fixed cable plans")
    ap.add_argument("--tx-atten", type=float, default=30.0, help="TX attenuation of every keyed step (dB)")
    ap.add_argument("--auth", default="BENCH-LOOPBACK", help="authorization reference written into each plan")
    a = ap.parse_args()

    # file name, plan, cable, what to look for in the Decodes table
    plans = [
        ("g40_loop_fsk", plan_one(a), f"{a.tx} to {a.rx}", "LOOP FSK"),
        ("g40_loop_comm_set", plan_comm_set(a), f"{a.tx} to {a.rx}",
         "one after the other: " + ", ".join(c[4] for c in COMM_SET)),
        ("g40_loop_eight_fsk", plan_eight(a), f"{a.tx} to {a.rx}", "LOOP FSK 1 to LOOP FSK 8, all together"),
        ("g40_loop_port_walk", plan_walk(a), "moved six times, see Port walk", "one per move, see Port walk"),
    ]

    sys.path.insert(0, a.repo)
    from dpmg.mission import mission_file as mf

    os.makedirs(OUT, exist_ok=True)
    rows, bad = [], 0
    for name, m, cable_txt, look in plans:
        path = os.path.join(OUT, f"{name}.dpmg-mission.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(m, f, indent=1)
            f.write("\n")
        found = mf.validate(mf.load(path))
        errs = [c for c in found if c.level == "error"]
        other = [c for c in found if c.level != "error" and "no Comm waveform step on air" not in c.message]
        verdict = f"{len(errs)} error(s)" if errs else "valid"
        for c in (errs + other)[:3]:
            verdict += f"; {c.step_id}: {c.message}"
        bad += len(errs)
        keyed = sum(1 for s in m["steps"] if s["radiates"])
        end = max(s["schedule"]["stop_s"] for s in m["steps"])
        rows.append(f"| `{name}.dpmg-mission.json` | {cable_txt} | {keyed} | {look} | {hms(end)} |")
        print(f"{name}: {len(m['steps'])} steps, {keyed} keyed, ends {hms(end)}, {verdict}")

    walk = "\n".join(f"| {n} | {tx} | {rx} | LOOP {tx[3:]} TO {rx[3:]} |" for n, (tx, rx) in enumerate(WALK, 1))
    readme = f"""# Loopback mission files for one G40

Four example mission files. Each makes one G40 send a signal out of one jack and decode it on another jack through a cable. They are for a bare G40 on the bench with no antenna and no amplifier. They are not flight cards and are not for the payload as wired.

Settings in these files: center {a.center / 1e6:g} MHz, {FS / 1e6:g} MS/s, TX attenuation {a.tx_atten:g} dB, radio row {a.radio}. make_loopback_plans.py in the folder above writes them.

## What you need

- One G40 running the DPMG radio service, reachable from the ground station laptop.
- One SMA cable with a pad (attenuator) in line. The bench scripts use 20 dB at this TX attenuation; later bench runs used 50 dB.
- The ground station GUI open in a browser.

## The files

| File | Cable, TX jack to RX jack | Keyed steps | Messages to look for | Length (min:s) |
|---|---|---|---|---|
{chr(10).join(rows)}

Start with `g40_loop_fsk`. It is the shortest and sends one message.

## What to change for your G40

The files as written name no real radio. Before a file can run, its radio row has to be your G40. Nothing else has to change for a bare G40 with a cable and pad.

| What | In the files now | Change it when | How |
|---|---|---|---|
| Radio name | {a.radio}{" (a placeholder)" if a.radio == "G40-LOOP" else ""} | always, unless this is already your G40's serial name | any one of the three ways below |
| Radio address | {a.address or "none"} | only if you want the plan to connect to the G40 by itself | write the files again with `--address IP:47810` |
| RF center | {a.center / 1e6:g} MHz | your bench uses another frequency | `--center`, in Hz |
| TX attenuation | {a.tx_atten:g} dB | your pad is smaller or larger than 20 to 50 dB | `--tx-atten`, in dB |
| Jacks | {a.tx} to {a.rx} | your cable is on other jacks, or a jack is locked to TX or RX on your G40 | `--tx` and `--rx` |

The serial name is the name the G40 announces, for example G40-AX35. It is on the radio's card in the SDR tab once the radio is connected.

Three ways to put your G40's name in, pick one:

1. Edit the .json. The name {a.radio} is in the file once under "radios" as "id" and once in every step as "radio". Replace every {a.radio} with your G40's serial name in a text editor, and save. All of them must change, or Check plan reports steps on a radio the plan does not have. From a terminal in this folder, for all four files:

```
sed -i 's/{a.radio}/G40-AX35/g' *.dpmg-mission.json
```

2. Bind it in the mission view, step 5 below. This changes only the plan that is open, not the file. Save plan in the mission view downloads the changed plan if you want to keep it.

3. Write the files again with the name in them. Run this from the folder above this one. It writes all four files and this page again:

```
python3 make_loopback_plans.py --radio G40-AX35
python3 make_loopback_plans.py --radio G40-AX35 --address 192.168.0.22:47810
```

The other settings are in the .json too, but in many places: the center is in every step as "center_hz" and inside each step's "spec" as "center_freq_hz", the TX attenuation is "tx_atten_db" in every keyed step, and the jacks are "ports" in every step. Change those with the script or in the mission view's step editor, not by hand.

## How to run one

1. Fit the cable with the pad between the two jacks in the table and tighten both ends.
2. In the GUI open the engineering view, SDR tab, and connect the G40. It shows up on its own when the laptop hears it; otherwise use Connect by IP.
3. Open the mission view: http://localhost:8080/mission, or "mission view" in the header.
4. Press Open file and pick one of the files in this folder.
5. Under Mission radios the plan has one radio, named {a.radio}. If that is not your G40's serial name it is shown as not online: pick your G40 in its "bind to a connected radio" box. The row takes your G40's name and its steps go with it. The box is there only while a G40 is connected.
6. Press Check plan. There should be no errors. One warning for each Decode step is expected: "Decode step has no Comm waveform step on air during it".
7. Press Run mission and confirm. Nothing is keyed in the first minute: the radio is being set to the two jacks.
8. Go back to the SDR tab and watch the Decodes table of the radio. Each message in the table above appears while its step is keyed, {KEY_S:g} s per step.
9. The plan stops keying by itself at the end. STOP ALL TX in the mission view stops it at any time. RX stays on afterwards: stop RX or close the radio on the SDR tab when done.

## If no message appears

- Look at the radio's spectrum while a step is keyed. The one message file puts its signal 400 kHz below the center. No signal there means the cable is on the wrong jacks or the step did not key: read the audit log in the mission view.
- A signal that fills the display or an RX overload warning means too much level: add pad, or write the files again with a higher --tx-atten.
- Check that the radio row is bound to the G40 that has the cable on it.

## How a loopback is laid out

The planner does not let two steps overlap on one radio, so each loopback is two steps in a row on the same pair of jacks. The Decode step builds the scenario without arming TX, loads its decode plan and starts RX. The Comm step after it keys the same scenario. RX is still running on that plan, so the radio decodes its own signal.

- The warning in step 6 comes from this layout: the Decode step ends when the Comm step starts.
- The first Decode step is {SETUP_S:g} s long because a G40 takes up to 50 s to change jacks.
- Both steps name both jacks. If the Comm step named only its TX jack, the radio would be set up again when it keys and RX would stop.
- RX on a "2" jack with TX on the "1" jack of the same card is refused by the radio. TX on a "2" jack looped to its own card shows on-card coupling only. These files always go from one card to the other.

## Port walk

`g40_loop_port_walk` uses one cable and moves it six times. Each move sends a message that names its two jacks.

| Move | TX jack | RX jack | Message |
|---|---|---|---|
{walk}

Fit the cable for move 1 before Run mission. After each keyed step ends nothing is keyed for {MOVE_GAP_S + MOVE_S:g} s: move the cable to the next row's jacks in that time. The radio changes jacks in the same gap. The mission view shows which step is running.

## Changing a setting

The files are written by `make_loopback_plans.py`, one folder up. Run it again to change them all together:

```
python3 make_loopback_plans.py --center 915e6 --tx-atten 40
python3 make_loopback_plans.py --rx TRx1B --tx TRx1A
```

--center is the RF center in Hz, --tx-atten the TX attenuation in dB, --rx and --tx the jacks of the three fixed cable files, --radio the name of the radio row and --address the radio's address. A single step can also be changed in the mission view's step editor after the file is opened.

## Test status

The files pass the planner's check. They have not been run on a G40 yet, so no decode has been seen from them. The first run on a radio is the test.
"""
    with open(os.path.join(OUT, "README.md"), "w", encoding="utf-8") as f:
        f.write(readme)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
