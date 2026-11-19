#!/usr/bin/env python3
"""Mission files for the flight test cards.

This script is the one place the whole set is written from. The files it
writes are what the operator loads in the mission view; nothing is run on the
day. To change a value for the whole set, rerun it and every plan changes
together:

  python3 make_flight_plans.py --tx-atten 30 --flight-atten 30

Three folders are written next to this script:

  plans/           the single radio plans of the flight cards (Payload 2 only)
  plans/pairs/     one two radio plan per ground radio (G40 or X40): both radios
                   on one timeline from Run mission, downlink then uplink then
                   the location tasks; plus the open air go or no-go
  plans/bench/     the same two radio plans, plus a tone each way check
  plans/cabled/    the two radio plans for the cabled ground layer (GROUND-02):
                   the ground radio on the 50 dB pad from the coupler's coupled
                   port, so the uplink keys at a low TX attenuation and the
                   payload decodes with its RX attenuator open

Radios are named by their serial names, so each row binds to its own radio:
G40-AX3G is the G40 in Payload 2 (USB-C address), G40-AX35 the ground G40 and
X40-B00S the ground X40. A row that stays offline (another radio stood in) is
bound in Mission radios with "bind to a connected radio". Every plan
starts when Run mission is pressed except the time triggered plan, which keys
at UTC clock times set in the mission view. The location tasks are points with
range gates: the part of --flight-box between two ranges from the ground
radio's site (--ground-site), at --gates-km, each keying its range message
for --gate-key-s on entry, going out and coming back, plus the far end of the
box from --far-from-km keying while inside. A task with no part inside the box
makes the script exit non-zero. The location plans' map carries the flight
box and the site as overlays; --route is only the path drawn on the map and
played by the bench sender.

The flight band is 1300 to 1350 MHz with the center at 1325 MHz. After
writing, every radiating step's occupied band is checked against --band-lo
and --band-hi at its radio's rate, and the README lists the margin of each
plan and the steps within 10 MHz of an edge; a step outside the band is an
error and the script exits non-zero.

Rates stop at 30.72 MS/s. Every radiating step carries the plan's
authorization reference. The TX attenuation defaults to 30 dB. Plans are
validated with the repo's mission_file. Nothing here touches a radio.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

JAMMERS = [
    # technique, kind, label, fields as a function of the sample rate
    ("jammer.barrage", "barrage", "barrage", lambda fs: {}),
    ("jammer.noise", "noise", "partial-band noise", lambda fs: {"bw": fs / 6.0, "center": fs / 8.0}),
    ("jammer.tone", "tone", "spot tone", lambda fs: {"freq": fs / 8.0}),
    ("jammer.comb", "comb", "comb", lambda fs: {"spacing": fs / 16.0, "n_tones": 8, "center": 0.0}),
    ("jammer.swept", "swept", "swept noise",
     lambda fs: {"span": 0.8 * fs, "sweep_period_s": 0.001, "noise_bw": 0.0, "center": 0.0}),
    ("jammer.chirp", "chirp", "LFM chirp", lambda fs: {"f0": -0.4 * fs, "f1": 0.4 * fs}),
    ("jammer.modulated", "modulated", "QPSK modulated",
     lambda fs: {"mod": "qpsk", "sym_rate": fs / 4.0, "sym_duty": 1.0, "rolloff": 0.35, "center": 0.0}),
]

# the 30.72 MS/s set: 25 jammers one at a time across the band, each type at
# several offsets, widths and settings, then all 25 comb tones together
JAM_25 = [
    # technique, kind, label, fields as a function of the sample rate, prf (Hz), duty
    ("jammer.barrage", "barrage", "barrage", lambda fs: {}, 0.0, 0.5),
    ("jammer.noise", "noise", "noise 3 MHz at -9.2 MHz", lambda fs: {"bw": fs / 10, "center": -0.3 * fs}, 0.0, 0.5),
    ("jammer.noise", "noise", "noise 3 MHz at -3.1 MHz", lambda fs: {"bw": fs / 10, "center": -0.1 * fs}, 0.0, 0.5),
    ("jammer.noise", "noise", "noise 3 MHz at +3.1 MHz", lambda fs: {"bw": fs / 10, "center": 0.1 * fs}, 0.0, 0.5),
    ("jammer.noise", "noise", "noise 3 MHz at +9.2 MHz", lambda fs: {"bw": fs / 10, "center": 0.3 * fs}, 0.0, 0.5),
    ("jammer.tone", "tone", "tone at -10.8 MHz", lambda fs: {"freq": -0.35 * fs}, 0.0, 0.5),
    ("jammer.tone", "tone", "tone at -6.1 MHz", lambda fs: {"freq": -0.2 * fs}, 0.0, 0.5),
    ("jammer.tone", "tone", "tone at +3.8 MHz", lambda fs: {"freq": fs / 8}, 0.0, 0.5),
    ("jammer.tone", "tone", "tone at +6.1 MHz", lambda fs: {"freq": 0.2 * fs}, 0.0, 0.5),
    ("jammer.tone", "tone", "tone at +10.8 MHz", lambda fs: {"freq": 0.35 * fs}, 0.0, 0.5),
    ("jammer.comb", "comb", "comb, 8 tones 1.92 MHz apart", lambda fs: {"spacing": fs / 16, "n_tones": 8, "center": 0.0}, 0.0, 0.5),
    ("jammer.comb", "comb", "comb, 12 tones 0.96 MHz apart at -6.1 MHz",
     lambda fs: {"spacing": fs / 32, "n_tones": 12, "center": -0.2 * fs}, 0.0, 0.5),
    ("jammer.comb", "comb", "comb, 4 tones 3.84 MHz apart at +6.1 MHz",
     lambda fs: {"spacing": fs / 8, "n_tones": 4, "center": 0.2 * fs}, 0.0, 0.5),
    ("jammer.swept", "swept", "swept noise 24.6 MHz, 1 ms",
     lambda fs: {"span": 0.8 * fs, "sweep_period_s": 0.001, "noise_bw": 0.0, "center": 0.0}, 0.0, 0.5),
    ("jammer.swept", "swept", "swept noise 12.3 MHz at -6.1 MHz, 10 ms",
     lambda fs: {"span": 0.4 * fs, "sweep_period_s": 0.01, "noise_bw": 0.0, "center": -0.2 * fs}, 0.0, 0.5),
    ("jammer.swept", "swept", "swept noise 12.3 MHz at +6.1 MHz, 0.1 ms",
     lambda fs: {"span": 0.4 * fs, "sweep_period_s": 0.0001, "noise_bw": 0.0, "center": 0.2 * fs}, 0.0, 0.5),
    ("jammer.chirp", "chirp", "LFM chirp up, 24.6 MHz", lambda fs: {"f0": -0.4 * fs, "f1": 0.4 * fs}, 0.0, 0.5),
    ("jammer.chirp", "chirp", "LFM chirp down, 24.6 MHz", lambda fs: {"f0": 0.4 * fs, "f1": -0.4 * fs}, 0.0, 0.5),
    ("jammer.chirp", "chirp", "LFM chirp up, 6.1 MHz", lambda fs: {"f0": -0.1 * fs, "f1": 0.1 * fs}, 0.0, 0.5),
    ("jammer.modulated", "modulated", "QPSK 7.68 MBd",
     lambda fs: {"mod": "qpsk", "sym_rate": fs / 4, "sym_duty": 1.0, "rolloff": 0.35, "center": 0.0}, 0.0, 0.5),
    ("jammer.modulated", "modulated", "BPSK 1.92 MBd at -7.7 MHz",
     lambda fs: {"mod": "bpsk", "sym_rate": fs / 16, "sym_duty": 1.0, "rolloff": 0.35, "center": -0.25 * fs}, 0.0, 0.5),
    ("jammer.modulated", "modulated", "8PSK 0.96 MBd at +7.7 MHz",
     lambda fs: {"mod": "8psk", "sym_rate": fs / 32, "sym_duty": 1.0, "rolloff": 0.35, "center": 0.25 * fs}, 0.0, 0.5),
    ("jammer.modulated", "modulated", "QPSK 3.84 MBd, half the symbols on",
     lambda fs: {"mod": "qpsk", "sym_rate": fs / 8, "sym_duty": 0.5, "rolloff": 0.35, "center": 0.0}, 0.0, 0.5),
    ("jammer.barrage", "barrage", "pulsed barrage, 1 kHz, 20 % duty", lambda fs: {}, 1000.0, 0.2),
    ("jammer.tone", "tone", "pulsed tone at -3.1 MHz, 100 Hz, 50 % duty", lambda fs: {"freq": -0.1 * fs}, 100.0, 0.5),
]


# comm set for a G40 receiver (narrowband, 1.92 MS/s on both ends)
COMM_SET = [
    # modulation, symbol rate, offset, extra, message
    ("FSK", 4800, -400_000, {"tone_spacing_hz": 5000}, "COMM FSK"),
    ("4FSK", 4800, -300_000, {"tone_spacing_hz": 5000}, "COMM 4FSK"),
    ("BPSK", 9600, -200_000, {}, "COMM BPSK"),
    ("QPSK", 9600, -100_000, {}, "COMM QPSK"),
    ("OQPSK", 9600, 500_000, {}, "COMM OQPSK"),    # off center: at 0 Hz it never decoded on the bench (8 Nov, DC and LO leakage)
    ("8PSK", 9600, 100_000, {}, "COMM 8PSK"),
    ("16QAM", 9600, 200_000, {}, "COMM 16QAM"),
    ("QPSK", 9600, 300_000, {"fec": {"type": "conv", "rate": "1/2"}}, "COMM QPSK CONV 1/2"),
    ("FSK", 4800, 400_000, {"tone_spacing_hz": 5000, "fec": {"type": "conv", "rate": "1/2"}}, "COMM FSK CONV 1/2"),
]

# comm set for an X40 receiver: the X40 runs 50 MS/s and up, and its channelizer
# does not decode narrow signals (the 50 MS/s floor found in the X40 checkout),
# so these are wide: 100 kBd, still inside the air radio's 1.92 MS/s
COMM_SET_WIDE = [
    ("FSK", 100_000, -600_000, {"tone_spacing_hz": 100_000}, "WIDE FSK"),
    ("BPSK", 100_000, -300_000, {}, "WIDE BPSK"),
    ("QPSK", 100_000, 450_000, {}, "WIDE QPSK"),   # off center: at 0 Hz it never decoded on the bench (8 Nov)
    ("8PSK", 100_000, 300_000, {}, "WIDE 8PSK"),
    ("QPSK", 100_000, 600_000, {"fec": {"type": "conv", "rate": "1/2"}}, "WIDE QPSK CONV 1/2"),
]

# direct sequence spread spectrum demo, the same three waveforms for every receiver and
# both directions: a 15 chip m-sequence (x^4 + x + 1),
# one phase per waveform, 300 kchip/s, so 20 kBd. The rate is set by three limits checked
# offline (test_results/2026-11-09_dsss_flight_demo): the despreader removes a carrier
# offset only up to symbol rate / 4 for BPSK and / 8 for QPSK and OQPSK (5 and 2.5 kHz
# here); the G40 receiver at 1.92 MS/s decimates by 3 to 640 kHz, which must hold 2 samples
# per chip; and the X40's 42 ms decode window at 50 MS/s needs short frames. The main
# lobe is 600 kHz wide, so at 450 kHz off center it sits 150 to 750 kHz from the center:
# clear of 0 Hz and inside the air radio's 1.92 MS/s.
DSSS_CHIP_RATE = 300_000.0
DSSS_CODES = [{"family": "lfsr", "poly": [4, 1], "state": st, "form": "fibonacci", "length": 15}
              for st in (1, 2, 4)]
DSSS_SET = [
    # carrier, offset, message, code index
    ("bpsk", -450_000, "DSSS BPSK", 0),
    ("qpsk", 450_000, "DSSS QPSK", 1),
    ("oqpsk", -450_000, "DSSS OQPSK", 2),
]
DSSS_MOD = {"bpsk": "DSSS", "qpsk": "QPSK-DSSS", "oqpsk": "OQPSK-DSSS"}


def dsss_sig(carrier, off, msg, idx):
    return {"modulation": DSSS_MOD[carrier], "carrier": carrier, "chip_rate_hz": DSSS_CHIP_RATE,
            "code_spec": dict(DSSS_CODES[idx]), "samples_per_chip": 2, "offset_hz": float(off),
            "amplitude": 0.9, "message": msg}


GROUND_KINDS = {
    # kind: (type, RX port, TX port, decode rate)
    "g40": ("g40", "TRx1A", "TRx1B", 1_920_000),
    "x40": ("x40", "RXB1", "TXA1", 50_000_000),
}


class Ground:
    def __init__(self, kind, radio, address):
        self.kind = kind
        self.type, self.rx_port, self.tx_port, self.rate = GROUND_KINDS[kind]
        self.radio = radio
        self.address = address
        self.comm = COMM_SET if kind == "g40" else COMM_SET_WIDE


def base(name, notes, a, anchor=None, radios=None):
    return {
        "format": "dpmg-mission/1",
        "name": name,
        "notes": notes,
        "authorization": {"reference": a.auth,
                          "note": "bench: HPA output on the coupler and load, no antenna, unless the card says otherwise"},
        "anchor": anchor or {"mode": "on_tx", "date": a.date},
        "radios": radios or [air_radio(a)],
        "steps": [],
    }


def air_radio(a, rate=30_720_000, with_address=True):
    r = {"id": a.air_radio, "type": "g40", "techniques": ["tx", "rx"], "sample_rate": int(rate)}
    if with_address and a.air_address:
        r["address"] = a.air_address
    # the clock Run mission brings the payload radio up on when it is not set up yet;
    # a radio that is already up keeps the clock it has
    clock = getattr(a, "air_clock", "external")
    if clock and clock != "internal":
        r["clock_source"] = clock
    return r


def ground_radio(g, rate=None, with_address=True):
    r = {"id": g.radio, "type": g.type, "techniques": ["tx", "rx"], "sample_rate": int(rate or g.rate)}
    if with_address and g.address:
        r["address"] = g.address
    return r


def rf(a, atten):
    return {"center_hz": float(a.center), "tx_center_hz": None, "tx_atten_db": float(atten),
            "rx_gain_index": None}


def jam_step(sid, a, tech, kind, fields, start, stop, atten, ampl=0.5, prf=0.0, duty=0.5, seconds=0.05):
    p = {"kind": kind, **rf(a, atten), "ampl": ampl, "seconds": seconds, "prf": prf, "duty": duty}
    p.update(fields)
    return {"id": sid, "radio": a.air_radio, "technique": tech, "radiates": True, "params": p,
            "schedule": {"start_s": float(start), "stop_s": float(stop)},
            "ports": {"rx": "none", "tx": a.tx_port}}


def spec(name, fs, a, signals, loop_s=0.2):
    return {"name": name, "sample_rate_hz": int(fs), "center_freq_hz": float(a.center), "loop_s": loop_s,
            "peak_fs": 0.45, "signals": signals}


def comm_step(sid, a, sp, start, stop, atten, radio=None, tx_port=None, match=True):
    return {"id": sid, "radio": radio or a.air_radio, "technique": "scenario_json", "radiates": True,
            "params": {"spec": sp, "spec_name": sp["name"], "source": "console", "spec_path": "",
                       "match_radio_rate": match, "peak_fs": 0.45, **rf(a, atten)},
            "schedule": {"start_s": float(start), "stop_s": float(stop)},
            "ports": {"rx": "none", "tx": tx_port or a.tx_port}}


def decode_step(sid, a, sp, start, stop, radio, rx_port, rx_atten=40.0):
    # rx_atten None leaves the radio's attenuator as set: the ground radios, which
    # have none or keep their own setting
    return {"id": sid, "radio": radio, "technique": "scenario_decode", "radiates": False,
            "params": {"spec": sp, "spec_name": sp["name"], "source": "console", "spec_path": "",
                       "match_radio_rate": True, "rx_atten_db": rx_atten, "center_hz": float(a.center),
                       "rx_gain_index": None},
            "schedule": {"start_s": float(start), "stop_s": float(stop)},
            "ports": {"rx": rx_port}}


def iq_step(sid, a, path, start, stop, atten):
    return {"id": sid, "radio": a.air_radio, "technique": "iq_playback", "radiates": True,
            "params": {"source": "file", "path": path, **rf(a, atten)},
            "schedule": {"start_s": float(start), "stop_s": float(stop)},
            "ports": {"rx": "none", "tx": a.tx_port}}


def tone_step(sid, a, start, stop, atten, ampl=0.5):
    return jam_step(sid, a, "jammer.tone", "tone", {"freq": 1_000_000.0}, start, stop, atten, ampl=ampl)


def sig(mod, sr, off, extra, msg):
    s = {"modulation": mod, "symbol_rate": sr, "offset_hz": off, "amplitude": 0.9, "message": msg}
    s.update(extra)
    return s


# single radio plans (the air radio alone)

def plan_file_playback(a):
    """The radio runs 7.68 MS/s for B, C, E, F and A, then one Set sample rate step takes it
    to 1.92 MS/s for D, last. Choose IQ file then has nothing to change for D (the plan
    already sets its rate), and resamples B, C and E to 7.68 MS/s. Without the set rate
    step the planner would move the radio's start rate to 1.92 MS/s for the whole run
    and B and C would play at the wrong rate (dry run, 12 November)."""
    m = base("Flight: example files through the planner build",
             "The example files from files/ copied to the radio as data/waveforms/flight/. The radio starts at "
             "7.68 MS/s for B, C, E, F and A; B, C and E (2.4 and 5 MS/s) are not G40 rates, so Choose IQ file "
             "resamples them to 7.68 MS/s. F has no rate marker and plays at the radio's rate unless it gets a "
             ".rate sidecar. A is a spec the radio builds at its own rate. Then a Set sample rate step takes the "
             "radio to 1.92 MS/s (about 50 s) for D, which plays at its own rate.", a)
    m["radios"][0]["sample_rate"] = 7_680_000
    base_dir = "data/waveforms/flight/"
    t = 5.0
    for tag, fn in [("b", "fsk_pair_2m4.cdif"), ("c", "qpsk_msg_5m.c64"), ("e", "fsk_pair_2400k.iq.bin"),
                    ("f", "fsk_pair_nomark.iq.bin")]:
        m["steps"].append(iq_step(f"file_{tag}", a, base_dir + fn, t, t + 15.0, a.tx_atten))
        t += 25.0
    spec_path = os.path.join(HERE, "..", "files", "fsk_pair_2m4.json")
    if os.path.exists(spec_path):
        sp = json.load(open(spec_path))
    else:
        sp = spec("fsk_pair_2m4", 2_400_000, a, [
            sig("FSK", 4800, -300_000, {"tone_spacing_hz": 5000}, "FILE A LOW"),
            sig("FSK", 9600, 400_000, {"tone_spacing_hz": 10000}, "FILE A HIGH")], loop_s=0.25)
    m["steps"].append(comm_step("file_a", a, sp, t, t + 15.0, a.tx_atten))
    t += 25.0
    m["steps"].append({"id": "file_rate_d", "radio": a.air_radio, "technique": "set_rate", "radiates": False,
                       "params": {"sample_rate": 1_920_000},
                       "schedule": {"start_s": t, "stop_s": t + 2.0}})
    t += 60.0                                   # the re-init blocks the radio about 50 s
    m["steps"].append(iq_step("file_d", a, base_dir + "bpsk_msg_1m92.iq.bin", t, t + 15.0, a.tx_atten))
    return m


def plan_jam(a, fs, tag):
    m = base(f"Flight: seven jammers at {fs / 1e6:g} MS/s",
             f"One step per jammer type, 10 s each with 6 s clear between, radio at {fs / 1e6:g} MS/s, "
             f"TX attenuation {a.tx_atten:g} dB. FieldFox on the coupled port, wide span, one screenshot per step.",
             a)
    m["radios"][0]["sample_rate"] = int(fs)
    t = 5.0
    for i, (tech, kind, label, fn) in enumerate(JAMMERS, 1):
        m["steps"].append(jam_step(f"{tag}_{i:02d}_{kind}", a, tech, kind, fn(fs), t, t + 10.0, a.tx_atten))
        t += 16.0
    return m


def plan_jam25(a, fs, tag):
    """25 jammers one at a time, 10 s each with 6 s clear, then the 25 tone comb."""
    m = base(f"Flight: 25 jammers at {fs / 1e6:g} MS/s, then 25 at once",
             f"25 single jammer steps across the band, 10 s each with 6 s clear between, then one step "
             f"of 20 s with a 25 tone comb (1 MHz apart, 24 MHz wide) so all 25 lines show at once. "
             f"Radio at {fs / 1e6:g} MS/s, TX attenuation {a.tx_atten:g} dB. FieldFox on the coupled port, "
             f"wide span, one screenshot per step.", a)
    m["radios"][0]["sample_rate"] = int(fs)
    t = 5.0
    for i, (tech, kind, label, fn, prf, duty) in enumerate(JAM_25, 1):
        m["steps"].append(jam_step(f"{tag}_{i:02d}_{kind}", a, tech, kind, fn(fs), t, t + 10.0, a.tx_atten,
                                   prf=prf, duty=duty))
        t += 16.0
    m["steps"].append(jam_step(f"{tag}_26_comb25", a, "jammer.comb", "comb",
                               {"spacing": 1.0e6, "n_tones": 25, "center": 0.0}, t, t + 20.0, a.tx_atten))
    return m


def plan_power_ladder(a):
    m = base("Flight: power ladder through the 1189",
             "Barrage then spot tone at each TX attenuation of the ladder, 20 s each with 15 s clear for the "
             "FieldFox reading and the HPA case temperature. Radio at 30.72 MS/s. The service floor is 13 dB "
             "and the payload guard holds the RX attenuator at 120 dB.", a)
    fs = 30_720_000
    t = 5.0
    for atten in a.ladder:
        m["steps"].append(jam_step(f"lad_{int(atten):02d}_barrage", a, "jammer.barrage", "barrage", {},
                                   t, t + 20.0, atten))
        t += 35.0
        m["steps"].append(jam_step(f"lad_{int(atten):02d}_tone", a, "jammer.tone", "tone", {"freq": fs / 8.0},
                                   t, t + 20.0, atten))
        t += 35.0
    return m


def plan_time_utc(a):
    hh, mm = (int(x) for x in a.utc_start.split(":"))
    t0 = hh * 3600 + mm * 60
    m = base("Flight: steps at UTC clock times",
             f"Five comm steps at clock times from {a.utc_start} UTC on {a.date}, two minutes apart, 30 s each, "
             "the message naming its slot. On the day set the date and move the five steps in the mission view. "
             "Keyed from the console clock; check it against the aircraft feed time and a GPS time first.",
             a, anchor={"mode": "clock", "date": a.date, "tz": "UTC"})
    m["radios"][0]["sample_rate"] = 1_920_000
    for i in range(5):
        start = t0 + i * 120
        sp = spec(f"utc{i + 1}", 1_920_000, a, [
            sig("QPSK", 4800, 300_000, {}, f"UTC SLOT {i + 1} {(start // 3600):02d}{(start % 3600) // 60:02d}")])
        m["steps"].append(comm_step(f"utc{i + 1:02d}", a, sp, start, start + 30.0, a.tx_atten))
    return m


# Location tasks are range gates: a band across the flight box at a set range
# from the ground radio site, so any track through the box crosses it, going
# out and coming back. Each gate keys its message for a set time on entry and
# re-arms once the platform has left it. The far end of the box (from
# --far-from-km out) keys while the platform is in it. Every message is the
# same length so the ground radio's one decode entry frames them all.
GATE_CONFIRM = {5: "once"}           # the gate nearest the site asks once


def _gate_message(r_km):
    return f"RANGE {int(r_km):02d} KM"


FAR_MESSAGE = "FAR END BOX"


def _area_signal(msg, ground=None):
    # one message as one signal; wide when an X40 must decode it
    if ground is not None and ground.kind == "x40":
        return sig("QPSK", 100_000, 0, {}, msg)
    return sig("QPSK", 4800, 300_000, {}, msg)


EARTH_R_M = 6371008.8


def geo_range_m(p, q):
    # great circle distance between two [lat, lon] points
    la1, lo1, la2, lo2 = map(math.radians, (p[0], p[1], q[0], q[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * EARTH_R_M * math.asin(math.sqrt(h))


def _to_xy(site, p):
    # [lat, lon] to meters east and north of the site, flat earth
    k = math.cos(math.radians(site[0]))
    return ((p[1] - site[1]) * 111320.0 * k, (p[0] - site[0]) * 110540.0)


def _in_box(a, p):
    # ray casting on [lat, lon], the same test the trigger engine uses
    inside = False
    box = a.box_pts
    for i in range(len(box)):
        (y1, x1), (y2, x2) = box[i - 1], box[i]
        if (y1 > p[0]) != (y2 > p[0]) and p[1] < (x2 - x1) * (p[0] - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def _dest(p, bearing_deg, dist_m):
    # the point dist_m from p on bearing_deg, on the same sphere as geo_range_m
    la1, lo1, b, d = math.radians(p[0]), math.radians(p[1]), math.radians(bearing_deg), dist_m / EARTH_R_M
    la2 = math.asin(math.sin(la1) * math.cos(d) + math.cos(la1) * math.sin(d) * math.cos(b))
    lo2 = lo1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(la1), math.cos(d) - math.sin(la1) * math.sin(la2))
    return [round(math.degrees(la2), 6), round(math.degrees(lo2), 6)]


def _band(a, r0, r1):
    # a ring sector from r0 to r1 meters around the site, over the bearings
    # where the ring crosses the flight box, as [lon, lat] vertices; it runs
    # a little past the box edges rather than miss any part inside
    # bearing step for about 150 m between vertices on the outer arc
    step_deg = 360 / int(math.ceil(360 / max(0.25, min(2.0, math.degrees(150.0 / r1)))))
    n = int(round(360 / step_deg))
    radii = [r0 + (r1 - r0) * k / 8 for k in range(9)]
    hit = [any(_in_box(a, _dest(a.site_pt, i * step_deg, r)) for r in radii) for i in range(n)]
    if not any(hit):
        return []
    if all(hit):
        lo, cnt = 0, n
    else:
        # the longest run of bearings that cross the box, round the circle
        start = next(i for i in range(n) if not hit[i])
        best, run, lo = 0, 0, 0
        for k in range(1, n + 1):
            i = (start + k) % n
            run = run + 1 if hit[i] else 0
            if run > best:
                best, lo = run, (i - run + 1) % n
        cnt = best
    # one sample more each side covers the box edge between samples
    b = [(lo - 1 + k) * step_deg for k in range(min(cnt + 2, n + 1))]
    pts = [_dest(a.site_pt, t, r1) for t in b] + [_dest(a.site_pt, t, r0) for t in reversed(b)]
    return [[q[1], q[0]] for q in pts]


def _poly_area_km2(a, coords):
    xy = [_to_xy(a.site_pt, (c[1], c[0])) for c in coords]
    return abs(sum(xy[i - 1][0] * xy[i][1] - xy[i][0] * xy[i - 1][1] for i in range(len(xy)))) / 2e6


def area_list(a):
    # (step id, message, start polygon, mode, length s, confirm, inner km, outer km)
    out = []
    for r in a.gates_km:
        out.append((f"loc_r{int(r):02d}", _gate_message(r), _band(a, r * 1e3, r * 1e3 + a.gate_depth_m),
                    "dwell", a.gate_key_s, GATE_CONFIRM.get(int(r), "auto"), r, r + a.gate_depth_m / 1e3))
    far = max(geo_range_m(a.site_pt, q) for q in a.box_pts)
    out.append(("loc_far", FAR_MESSAGE, _band(a, a.far_from_km * 1e3, far + 1000.0),
                "while_inside", a.far_max_s, "auto", a.far_from_km, far / 1e3))
    return out


def map_block(a, route):
    # flight path, flight box and ground site for the planner map; fixed ids so
    # a rerun writes the same file
    return {"flight_path": [[p[1], p[0]] for p in route], "flight_path_source": "make_flight_plans.py --route",
            "overlays": [
                {"id": "ov-flight-box", "name": "Flight box", "source": "make_flight_plans.py --flight-box",
                 "color": "#e65100", "lines": [],
                 "polygons": [{"name": "Flight box", "coords": [[p[1], p[0]] for p in a.box_pts]}],
                 "points": []},
                {"id": "ov-ground-site", "name": "Ground radio site", "source": "make_flight_plans.py --ground-site",
                 "color": "#1565c0", "lines": [], "polygons": [],
                 "points": [{"name": "Ground radio site", "lat": a.site_pt[0], "lon": a.site_pt[1]}]}]}


def location_tasks(a, m, ground=None):
    m["map"] = map_block(a, a.route_pts)
    for sid, msg, poly, mode, length, confirm, r0, r1 in area_list(a):
        sp = spec(sid.replace("loc_", "gate_"), 1_920_000, a, [_area_signal(msg, ground)])
        step = comm_step(sid, a, sp, 0.0, 60.0, a.tx_atten)
        step.pop("schedule", None)
        step["location"] = {"start": {"polygon": poly}}
        step["priority"] = 3
        step["trigger"] = {"mode": mode, "dwell_s": float(length if mode == "dwell" else 20.0),
                           "max_s": float(length if mode != "dwell" else 120.0), "confirm": confirm}
        m["steps"].append(step)


def plan_location(a):
    m = base("Flight: range gates and the far end of the flight box",
             f"Two timed comm steps, then location tasks from the aircraft feed: range gates across the flight "
             f"box at {', '.join(f'{r:g}' for r in a.gates_km)} km from the ground radio site, each keying its "
             f"range message (RANGE 05 KM and so on) for {a.gate_key_s:g} s when the aircraft crosses it, going "
             f"out and coming back; and the far end of the box from {a.far_from_km:g} km, keying FAR END BOX "
             f"while the aircraft is in it (max {a.far_max_s:g} s). The {min(a.gates_km):g} km gate asks for a "
             "confirm the first time, the rest are automatic. Arm the triggers, listen to the aircraft feed, "
             "and watch the Live feed row. On the bench tools/sim_position.py plays the route.", a)
    m["radios"][0]["sample_rate"] = 1_920_000
    for i in range(2):
        sp = spec(f"time{i + 1}", 1_920_000, a, [sig("QPSK", 4800, 300_000, {}, f"TIME {i + 1} QPSK")])
        m["steps"].append(comm_step(f"tx_time{i + 1}", a, sp, 5.0 + i * 30.0, 20.0 + i * 30.0, a.tx_atten))
    location_tasks(a, m)
    return m


def plan_ground_fieldfox(a):
    m = base("Flight: one step per waveform for the FieldFox",
             f"Spot tone, the seven jammers and one QPSK comm, 20 s each with 15 s clear, at TX attenuation "
             f"{a.tx_atten:g} dB and 30.72 MS/s. Move the FieldFox marker and take one screenshot per step.", a)
    fs = 30_720_000
    t = 5.0
    m["steps"].append(tone_step("ff_00_tone", a, t, t + 20.0, a.tx_atten))
    t += 35.0
    for i, (tech, kind, label, fn) in enumerate(JAMMERS, 1):
        m["steps"].append(jam_step(f"ff_{i:02d}_{kind}", a, tech, kind, fn(fs), t, t + 20.0, a.tx_atten))
        t += 35.0
    sp = spec("ff_qpsk", fs, a, [sig("QPSK", 1_000_000, 0, {}, "GROUND QPSK")], loop_s=0.05)
    m["steps"].append(comm_step("ff_08_qpsk", a, sp, t, t + 20.0, a.tx_atten))
    return m


# open air go or no-go: both radios on their own antennas, the ground radio in place
# of the FieldFox, so the check is one plan with both radios bound by name

AIR_DOWN = {"g40": ("QPSK", 9600, -100_000, {}, "AIR QPSK"),
            "x40": ("QPSK", 100_000, 450_000, {}, "AIR WIDE QPSK")}
AIR_UP = ("QPSK", 9600, -100_000, {}, "AIR UPLINK QPSK")     # the air G40 decodes at 1.92 MS/s


def plan_air_pair(a, g):
    """Antenna on J0 and the ground radio on its own antenna. A tone at 40 dB and at the
    flight drive (the ground radio shows both and the difference between them), one comm
    message down that the ground radio decodes, one barrage, then one comm message up that
    the air radio decodes on TRx1A. Starts on Run mission, about 3.5 minutes."""
    m = base(f"Flight: open air go or no-go with the {g.kind.upper()} ground radio",
             f"Antenna on J0, {g.radio} ({g.type}) on its own antenna and receiving on {g.rx_port}. "
             f"{a.air_radio} keys a tone 10 s at 40 dB, then 10 s at the flight drive ({a.flight_atten:g} dB); "
             f"then one comm message that {g.radio} decodes; then one barrage 10 s at the flight drive; then "
             f"{g.radio} keys one comm message on {g.tx_port} at {a.ground_atten:g} dB that {a.air_radio} decodes "
             "on TRx1A (the air radio re-inits to that port first, about 50 s). Stop at the first thing that "
             "reads wrong.", a, radios=pair_radios(a, g, False))
    # the air radio runs 1.92 MS/s here, so the tone sits 300 kHz above the center, inside
    # its band (the 1 MHz tone of the single radio plans needs the 30.72 MS/s rate)
    for sid, t, att in (("air_01_tone_40", 5.0, 40.0), ("air_02_tone_flight", 45.0, a.flight_atten)):
        m["steps"].append(jam_step(sid, a, "jammer.tone", "tone", {"freq": 300_000.0}, t, t + 10.0, att))
    sp = spec("air_down", 1_920_000, a, [sig(*AIR_DOWN[g.kind])])
    m["steps"].append(decode_step("air_03_rx_down", a, sp, 80.0, 97.0, g.radio, g.rx_port, None))
    m["steps"].append(comm_step("air_03_tx_down", a, sp, 83.0, 96.0, a.flight_atten))
    m["steps"].append(jam_step("air_04_barrage_flight", a, "jammer.barrage", "barrage", {}, 125.0, 135.0,
                               a.flight_atten))
    sp = spec("air_up", 1_920_000, a, [sig(*AIR_UP)])
    m["steps"].append(decode_step("air_05_rx_up", a, sp, 190.0, 207.0, a.air_radio, "TRx1A"))
    m["steps"].append(comm_step("air_05_tx_up", a, sp, 193.0, 206.0, a.ground_atten,
                                radio=g.radio, tx_port=g.tx_port))
    return m


# two radio plans: the air radio and a ground radio (G40 or X40)

def pair_radios(a, g, with_address=True):
    # the payload keeps its address in every plan, so it binds by itself on that link;
    # a ground radio has an address only when one is given
    return [air_radio(a, 1_920_000, True), ground_radio(g, None, with_address)]


def plan_pair(a, g, with_address, cabled=False):
    """One plan per pairing: both radios bound, one timeline, one UTC anchor.
    Downlink pairs (air keys, ground decodes), then uplink pairs (ground keys,
    air decodes on TRx1A), then the ground radio's long decode step for the area
    messages with the air radio's location tasks along the route.

    cabled: the ground radio sits on the 50 dB pad from the coupled port of the
    J0 coupler. A signal put into the coupled port leaves by the coupler's input
    side, so it reaches J0 down by the pad and the coupling (about 80 dB); the
    uplink steps then key at a.cabled_ground_atten and the payload decodes with
    its RX attenuator at a.cabled_air_rx_atten."""
    t0 = 5.0
    ul_atten = a.cabled_ground_atten if cabled else a.ground_atten
    ul_rx_atten = a.cabled_air_rx_atten if cabled else 40.0
    m = base(f"{'Cabled pair' if cabled else 'Pair'} {g.kind.upper()} ground: {a.air_radio} and {g.radio}, one timeline",
             f"Both radios keyed from this ground station on one network, starting together on Run mission: "
             f"downlink, {a.air_radio} keys each comm waveform for 13 s and {g.radio} ({g.type}) decodes on "
             f"{g.rx_port}; then uplink, {g.radio} keys its waveforms on {g.tx_port} at {ul_atten:g} dB and "
             f"{a.air_radio} decodes on TRx1A through the payload RX chain, RX attenuator {ul_rx_atten:g} dB; then {g.radio} holds one decode step "
             "for every area message while the air radio's location tasks key along the route (arm the "
             "triggers, feed on). The areas are typed in the step editor once the route is known.",
             a, radios=pair_radios(a, g, with_address))
    t = t0
    down = [sig(mod, sr, off, extra, msg) for mod, sr, off, extra, msg in g.comm]
    down += [dsss_sig(c, off, msg, k) for c, off, msg, k in DSSS_SET if c in a.dsss]
    for i, sg in enumerate(down, 1):
        sp = spec(f"dl{i}", 1_920_000, a, [sg])
        m["steps"].append(decode_step(f"dl_rx{i:02d}", a, sp, t, t + 17.0, g.radio, g.rx_port, None))
        m["steps"].append(comm_step(f"dl_tx{i:02d}", a, sp, t + 3.0, t + 16.0, a.tx_atten))
        t += 22.0
    t_dl_end = t
    t += 55.0                                   # clear time between the sections: the air G40 re-inits
                                                # to RX TRx1A for the uplink, about 50 s (bench, 8 Nov)
    t_ul = t
    up = [sig(mod, sr, off, extra, msg.replace("COMM", "UPLINK")) for mod, sr, off, extra, msg in COMM_SET[2:6]]
    up += [dsss_sig(c, off, "UPLINK " + msg, k) for c, off, msg, k in DSSS_SET if c in a.dsss]
    for i, sg in enumerate(up, 1):
        sp = spec(f"ul{i}", 1_920_000, a, [sg])
        m["steps"].append(decode_step(f"ul_rx{i:02d}", a, sp, t, t + 17.0, a.air_radio, "TRx1A",
                                      rx_atten=ul_rx_atten))
        m["steps"].append(comm_step(f"ul_tx{i:02d}", a, sp, t + 3.0, t + 16.0, ul_atten,
                                    radio=g.radio, tx_port=g.tx_port))
        t += 22.0
    t += 30.0
    sp = spec("areas_all", 1_920_000, a, [_area_signal(_gate_message(min(a.gates_km)), g)])
    m["steps"].append(decode_step("loc_rx_areas", a, sp, t, t + a.area_decode_s, g.radio, g.rx_port, None))
    location_tasks(a, m, g)
    if cabled:
        m["notes"] += (" Cabled ground layer (CFG-G1): the ground radio on the 50 dB pad from the coupler's "
                       "coupled port. The uplink reaches J0 down by the pad and the coupling, about 80 dB, "
                       "so it keys at a low TX attenuation and the payload RX attenuator is open; the payload "
                       "guard still sets 120 dB whenever the payload keys. Not for the open air layer.")
    m["notes"] += (f" Sections from Run mission: downlink {_hms(t0)} to {_hms(t_dl_end)} ({len(down)} waveforms), "
                   f"uplink from {_hms(t_ul)} ({len(up)} waveforms), range gate decode from {_hms(t)} for "
                   f"{a.area_decode_s / 60:g} min.")
    return m


def _hms(sec):
    sec = int(round(sec)) % 86400
    return f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"


def plan_bench_two_radio_check(a, g):
    m = base(f"Bench {g.kind.upper()} and G40: tone each way, no HPA",
             f"Two tones each way through the pads: {a.air_radio} keys a tone on {a.tx_port} for 10 s and "
             f"{g.radio} shows it, then {g.radio} keys a tone on {g.tx_port} and {a.air_radio} shows it. Both "
             "radios on the GPSDO. Levels recorded, not graded.", a,
             radios=pair_radios(a, g, True))
    m["steps"].append(tone_step("b_air_tone", a, 5.0, 15.0, a.tx_atten))
    gt = jam_step("b_ground_tone", a, "jammer.tone", "tone", {"freq": 1_000_000.0}, 30.0, 40.0, a.ground_atten)
    gt["radio"] = g.radio
    gt["ports"] = {"rx": "none", "tx": g.tx_port}
    m["steps"].append(gt)
    return m


# band check: the occupied band of every radiating step at its radio's rate

EDGE_WARN_HZ = 10e6


def _sig_bw(sg):
    """Occupied bandwidth of one comm signal: FSK by its tone excursion plus a
    symbol rate skirt each side, a PSK or QAM by its symbol rate times 1.35
    (the RRC roll-off the builder uses), rounded up."""
    mod = str(sg.get("modulation", "")).upper()
    if "DSSS" in mod:
        return 2.0 * float(sg.get("chip_rate_hz") or 0.0)      # main lobe, null to null
    rs = float(sg.get("symbol_rate") or 0.0)
    if "FSK" in mod:
        m = 4 if mod.startswith("4") else 2
        return float(sg.get("tone_spacing_hz") or 1000.0) * (m - 1) + 2.0 * rs
    return 1.35 * rs


FILE_RATES = {"fsk_pair_2m4": 2.4e6, "fsk_pair_2400k": 2.4e6, "fsk_pair_nomark": 2.4e6,
              "qpsk_msg_5m": 5.0e6, "bpsk_msg_1m92": 1.92e6}


def occupied_offsets(step, fs):
    """(lo, hi) of the step's emission as offsets from its TX center (Hz), at
    radio rate fs, and a short note of what sets it. None for a step that does
    not radiate."""
    if not step.get("radiates"):
        return None
    p = step.get("params") or {}
    tech = step.get("technique", "")
    if tech.startswith("jammer.") or tech.startswith("tools."):
        k = p.get("kind")
        if k == "barrage":
            return -fs / 2, fs / 2, "barrage, the whole sample band"
        if k == "noise":
            c, bw = float(p.get("center", 0.0)), float(p.get("bw", fs))
            return c - bw / 2, c + bw / 2, "noise patch"
        if k == "tone":
            f = float(p.get("freq", 0.0))
            return f, f, "tone"
        if k == "comb":
            c, sp, n = float(p.get("center", 0.0)), float(p.get("spacing", 0.0)), int(p.get("n_tones", 1))
            return c - sp * (n - 1) / 2, c + sp * (n - 1) / 2, "comb"
        if k == "swept":
            c, span, nb = float(p.get("center", 0.0)), float(p.get("span", fs)), float(p.get("noise_bw", 0.0))
            return c - span / 2 - nb / 2, c + span / 2 + nb / 2, "sweep span"
        if k == "chirp":
            f0, f1 = float(p.get("f0", 0.0)), float(p.get("f1", 0.0))
            return min(f0, f1), max(f0, f1), "chirp"
        if k == "modulated":
            c = float(p.get("center", 0.0))
            half = float(p.get("sym_rate", 0.0)) * (1 + float(p.get("rolloff", 0.35))) / 2
            return c - half, c + half, "modulated"
        return -fs / 2, fs / 2, f"{k}, taken as the whole sample band"
    if tech == "scenario_json":
        sigs = (p.get("spec") or {}).get("signals") or []
        if not sigs:
            return -fs / 2, fs / 2, "scenario, taken as the whole sample band"
        lo = min(float(sg.get("offset_hz", 0.0)) - _sig_bw(sg) / 2 for sg in sigs)
        hi = max(float(sg.get("offset_hz", 0.0)) + _sig_bw(sg) / 2 for sg in sigs)
        return lo, hi, "comm signals"
    if tech == "iq_playback":
        base = os.path.basename(str(p.get("path", ""))).split(".")[0]
        r = FILE_RATES.get(base, fs)
        return -r / 2, r / 2, f"file band, its own rate {r / 1e6:g} MS/s"
    return -fs / 2, fs / 2, f"{tech}, taken as the whole sample band"


def band_check(m, lo_edge, hi_edge):
    """[(step id, abs lo, abs hi, margin, note)] for every radiating step."""
    rates = {r["id"]: float(r.get("sample_rate") or 30_720_000) for r in m.get("radios", [])}
    out = []
    for st in m.get("steps", []):
        fs = rates.get(st.get("radio"), 30_720_000.0)
        occ = occupied_offsets(st, fs)
        if occ is None:
            continue
        p = st.get("params") or {}
        fc = float(p.get("tx_center_hz") or p.get("center_hz") or 0.0)
        lo, hi = fc + occ[0], fc + occ[1]
        out.append((st.get("id"), lo, hi, min(lo - lo_edge, hi_edge - hi), occ[2]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=os.path.normpath(os.path.join(HERE, "..", "..")),
                    help="the flight repo, for validation")
    ap.add_argument("--center", type=float, default=1325e6, help="RF center of every step (Hz)")
    ap.add_argument("--band-lo", type=float, default=1300e6, help="lower edge of the authorized band (Hz)")
    ap.add_argument("--band-hi", type=float, default=1350e6, help="upper edge of the authorized band (Hz)")
    ap.add_argument("--air-radio", default="G40-AX3G", help="the payload G40's name in the plans")
    ap.add_argument("--air-address", default="192.168.0.23:47810",
                    help="its address on the site network (192.168.55.1:47810 on the USB-C link; blank for none)")
    ap.add_argument("--tx-port", default="TRx2B", help="the payload TX jack")
    ap.add_argument("--air-clock", default="external",
                    choices=["internal", "external", "ext_10mhz", "ext_pps", "gpsdo"],
                    help="clock the payload radio comes up on when Run mission has to set it up: external is "
                         "the 10 MHz and the 1 PPS from the payload clock board")
    ap.add_argument("--ground-g40", default="G40-AX35")
    ap.add_argument("--ground-g40-address", default="192.168.0.22:47810")
    ap.add_argument("--ground-x40", default="X40-B00S",
                    help="the X40's name; a prefix such as X40 binds to any X40-<serial> that comes online")
    ap.add_argument("--ground-x40-address", default="192.168.0.26:47810")
    ap.add_argument("--tx-atten", type=float, default=30.0, help="TX attenuation of the air radio's bench steps (dB)")
    ap.add_argument("--ground-atten", type=float, default=30.0, help="TX attenuation of the ground radio (dB)")
    ap.add_argument("--cabled-ground-atten", type=float, default=0.0,
                    help="ground radio TX attenuation on the uplink of the cabled pair plans (dB)")
    ap.add_argument("--cabled-air-rx-atten", type=float, default=0.0,
                    help="payload RX attenuator on the uplink decodes of the cabled pair plans (dB)")
    ap.add_argument("--flight-atten", type=float, default=30.0, help="the agreed flight drive (dB)")
    ap.add_argument("--ladder", default="40,30,25,20,15", help="power ladder attenuations (dB)")
    ap.add_argument("--route", default="43.566667,-112.883333 43.916667,-112.700000",
                    help="lat,lon points of the route for the location plans (default: a straight line from "
                         "the middle of the flight box's south edge to the middle of its north edge)")
    ap.add_argument("--flight-box", default="43.916667,-112.750000 43.916667,-112.650000 "
                                            "43.550000,-112.833333 43.583333,-112.933333",
                    help="lat,lon corners of the area the aircraft flies in, in order round the edge")
    ap.add_argument("--ground-site", default="43.598621,-112.905566",
                    help="lat,lon of the ground radio")
    ap.add_argument("--gates-km", default="5,10,15,20,25,30",
                    help="ranges of the range gates from the ground radio site (km)")
    ap.add_argument("--gate-depth-m", type=float, default=1000.0, help="depth of each range gate (m)")
    ap.add_argument("--gate-key-s", type=float, default=15.0, help="how long a gate keys once crossed (s)")
    ap.add_argument("--far-from-km", type=float, default=34.0,
                    help="range from the site where the far end of the box starts (km)")
    ap.add_argument("--far-max-s", type=float, default=300.0, help="longest the far end keys in one visit (s)")
    ap.add_argument("--area-decode-s", type=float, default=3600.0,
                    help="length of the ground radio's range gate decode step in the pair plans (s)")
    ap.add_argument("--date", default="2026-11-17")
    ap.add_argument("--utc-start", default="17:00")
    ap.add_argument("--auth", default="FLIGHT-2026-11")
    ap.add_argument("--dsss", default="bpsk,qpsk,oqpsk",
                    help="the DSSS demo waveforms to include in both directions (blank for none)")
    a = ap.parse_args()
    a.dsss = [x.strip().lower() for x in a.dsss.split(",") if x.strip()]
    a.ladder = [float(x) for x in a.ladder.split(",")]
    a.route_pts = [[float(p.split(",")[0]), float(p.split(",")[1])] for p in a.route.split()]
    a.box_pts = [[float(p.split(",")[0]), float(p.split(",")[1])] for p in a.flight_box.split()]
    a.site_pt = [float(x) for x in a.ground_site.split(",")]
    a.gates_km = [float(x) for x in a.gates_km.split(",") if x.strip()]
    grounds = [Ground("g40", a.ground_g40, a.ground_g40_address),
               Ground("x40", a.ground_x40, a.ground_x40_address)]

    plans = {
        "file_playback": plan_file_playback(a),
        "jam_1m92": plan_jam(a, 1_920_000, "j192"),
        "jam_7m68": plan_jam(a, 7_680_000, "j768"),
        "jam_30m72": plan_jam25(a, 30_720_000, "j3072"),
        "jam_power_ladder": plan_power_ladder(a),
        "trig_time_utc": plan_time_utc(a),
        "trig_location": plan_location(a),
        "ground_cabled_fieldfox": plan_ground_fieldfox(a),
    }
    for g in grounds:
        plans[f"pairs/{g.kind}_pair"] = plan_pair(a, g, False)
        plans[f"pairs/{g.kind}_air_go_nogo"] = plan_air_pair(a, g)
        plans[f"bench/{g.kind}_pair"] = plan_pair(a, g, True)
        plans[f"bench/{g.kind}_tone_each_way"] = plan_bench_two_radio_check(a, g)
        plans[f"cabled/{g.kind}_pair"] = plan_pair(a, g, False, cabled=True)
    # files this script no longer writes (the 61.44 set, the split pair plans) are removed
    for sub, names in (("", ["jam_61m44", "comm_chain_two_g40", "air_go_nogo"]),
                       ("pairs", [f"{k}_{d}" for k in ("g40", "x40") for d in ("downlink", "uplink", "location")]),
                       ("bench", [f"{k}_{d}" for k in ("g40", "x40") for d in ("downlink", "uplink", "location")])):
        for n in names:
            old = os.path.join(HERE, sub, f"{n}.dpmg-mission.json")
            if os.path.exists(old):
                os.remove(old)

    mf = None
    if a.repo and os.path.isdir(a.repo):
        sys.path.insert(0, a.repo)
        try:
            from dpmg.mission import mission_file as mf
        except Exception as e:
            print(f"(no validation: {e})")
    areas = area_list(a)
    bad_areas = [x[0] for x in areas if len(x[2]) < 3 or _poly_area_km2(a, x[2]) < 0.05]
    rows = []
    near, outside = [], []
    for name, m in plans.items():
        path = os.path.join(HERE, f"{name}.dpmg-mission.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(m, f, indent=1)
            f.write("\n")
        n_loc = sum(1 for s in m["steps"] if "location" in s)
        verdict = ""
        if mf is not None:
            loaded = mf.load(path)
            errs = [c for c in mf.validate(loaded) if c.level == "error"]
            verdict = f"{len(errs)} error(s)" if errs else "valid"
            for c in errs[:3]:
                verdict += f"; {c.step_id}: {c.message}"
        bc = band_check(m, a.band_lo, a.band_hi)
        margin = min((b[3] for b in bc), default=None)
        for b in bc:
            if b[3] < 0:
                outside.append((name, b))
            elif b[3] < EDGE_WARN_HZ:
                near.append((name, b))
        rows.append((f"{name}.dpmg-mission.json", m["name"], len(m["steps"]), n_loc,
                     ", ".join(r["id"] + ("" if r.get("address") else " (by name)") for r in m["radios"]), verdict,
                     "" if margin is None else f"{margin / 1e6:.2f}"))
    with open(os.path.join(HERE, "README.md"), "w", encoding="utf-8") as f:
        f.write("# Flight test mission files\n\n")
        f.write(f"Written by make_flight_plans.py: center {a.center / 1e6:g} MHz, payload {a.air_radio} at "
                f"{a.air_address or 'no address'} TX {a.tx_port}, ground G40 {a.ground_g40}, ground X40 "
                f"{a.ground_x40}, TX attenuation {a.tx_atten:g} dB, ground {a.ground_atten:g} dB, flight drive "
                f"{a.flight_atten:g} dB, time plan on {a.date} from {a.utc_start} UTC. Rates stop at 30.72 MS/s.\n\n")
        f.write("Load a plan in the mission view with Open file; nothing is run on the day. `plans/` are Payload 2 "
                "alone. `pairs/` is one plan per pairing with both radios on one timeline from Run mission "
                "(downlink, uplink, then the location tasks) and the open air go or no-go. `bench/` are the same "
                "two radio plans plus a tone each way check. `cabled/` are the pair plans for the cabled ground layer "
                f"(GROUND-02): uplink at ground TX attenuation {a.cabled_ground_atten:g} dB and the payload RX "
                f"attenuator at {a.cabled_air_rx_atten:g} dB, since the coupled port and the 50 dB pad take about "
                "80 dB off the uplink. Each radio row is named by the radio's serial name "
                "and binds to that radio when it is online; any row that stays offline is bound with \"bind to a connected radio\" in "
                "Mission radios. The location areas are points with a radius, placed along the default route "
                "inside the flight box and retyped in the step editor once the route is known. The time plan keys "
                "at UTC clock times: set its date and move its steps in the mission view.\n\n")
        f.write("| File | Plan name | Steps | Location tasks | Radios | Check | Least margin to a band edge (MHz) |\n"
                "|---|---|---|---|---|---|---|\n")
        for r in rows:
            f.write(f"| `{r[0]}` | {r[1]} | {r[2]} | {r[3]} | {r[4]} | {r[5]} | {r[6]} |\n")
        f.write(f"\n## Band check, {a.band_lo / 1e6:g} to {a.band_hi / 1e6:g} MHz\n\n")
        f.write("Every radiating step's occupied band at its radio's rate, from the step's parameters: a barrage "
                "is taken as the whole sample band, a noise patch, comb, sweep, chirp or modulated jammer by its "
                "own width, a comm signal by its offset and bandwidth (FSK by its tone excursion plus a symbol "
                "rate each side, PSK and QAM by 1.35 times the symbol rate), a playback file by its own rate. "
                "The radio's analog filter and the HPA are not modeled. A ground X40 runs at 50 MS/s, so its "
                "sample band is 1300 to 1350 MHz at a 1325 MHz center: its TX steps carry only the comm signals "
                "listed, but its noise floor and any image fill that band edge to edge.\n\n")
        if outside:
            f.write("Steps OUTSIDE the band:\n\n| Plan | Step | Low (MHz) | High (MHz) | What sets it |\n|---|---|---|---|---|\n")
            for name, b in outside:
                f.write(f"| {name} | {b[0]} | {b[1] / 1e6:.3f} | {b[2] / 1e6:.3f} | {b[4]} |\n")
            f.write("\n")
        else:
            f.write("No step goes outside the band.\n\n")
        if near:
            f.write(f"Steps within {EDGE_WARN_HZ / 1e6:g} MHz of an edge:\n\n"
                    "| Plan | Step | Low (MHz) | High (MHz) | Margin (MHz) | What sets it |\n|---|---|---|---|---|---|\n")
            for name, b in near:
                f.write(f"| {name} | {b[0]} | {b[1] / 1e6:.3f} | {b[2] / 1e6:.3f} | {b[3] / 1e6:.2f} | {b[4]} |\n")
        else:
            f.write(f"No step comes within {EDGE_WARN_HZ / 1e6:g} MHz of an edge.\n")
        f.write("\n## Location tasks\n\n")
        f.write(f"Ground radio site {a.site_pt[0]:.6f}, {a.site_pt[1]:.6f}. Flight box corners: "
                + "; ".join(f"{q[0]:.6f}, {q[1]:.6f}" for q in a.box_pts)
                + ". Route on the map (the bench sender plays it): " + "; ".join(f"{q[0]:.6f}, {q[1]:.6f}" for q in a.route_pts)
                + ". The same tasks are in trig_location and every pair plan. Each is the part of the flight box "
                "between two ranges from the ground radio site, so it works for any track through the box. A gate "
                "keys for a set time once entered and re-arms when the aircraft has left it, so it keys going out "
                "and again coming back; the far end keys while the aircraft is in it.\n\n")
        f.write("| Step | Message | From (km) | To (km) | Keys | Confirm | Area (km2) |\n|---|---|---|---|---|---|---|\n")
        for sid, msg, poly, mode, length, confirm, r0, r1 in areas:
            keys = f"{length:g} s on entry" if mode == "dwell" else f"while inside, max {length:g} s"
            f.write(f"| {sid} | {msg} | {r0:g} | {r1:.1f} | {keys} | {confirm} | {_poly_area_km2(a, poly):.1f} |\n")
        far = max(geo_range_m(a.site_pt, q) for q in a.box_pts)
        f.write(f"\nThe farthest corner of the flight box is {far / 1e3:.1f} km from the site.\n")
        if bad_areas:
            f.write("\nTasks with no part inside the flight box: " + ", ".join(bad_areas) + ".\n")
    for r in rows:
        print(f"{r[0]:44} {r[2]:3} steps {r[3]:2} loc  {r[5]:8} margin {r[6]} MHz")
    print(f"band {a.band_lo / 1e6:g} to {a.band_hi / 1e6:g} MHz: {len(outside)} step(s) outside, "
          f"{len(near)} within {EDGE_WARN_HZ / 1e6:g} MHz of an edge")
    for sid, msg, poly, mode, length, confirm, r0, r1 in areas:
        print(f"{sid:8} {msg}  {r0:g} to {r1:.1f} km  {mode} {length:g} s  {confirm:5}  "
              f"{len(poly)} vertices, {_poly_area_km2(a, poly):.1f} km2")
    if bad_areas:
        print("tasks with no part inside the flight box: " + ", ".join(bad_areas))
    return 1 if outside or bad_areas else 0


if __name__ == "__main__":
    sys.exit(main())
