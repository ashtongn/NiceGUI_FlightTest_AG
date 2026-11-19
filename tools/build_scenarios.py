#!/usr/bin/env python3
"""
build_scenarios.py: generate a library of flight-test scenarios.

Each scenario is a named set of signals (modulation, band offset, message).
Each one is written under scenarios/ as:
  <name>.plan.json   channel plan (compose + entries): push to the radio for
                     RX decode, or hand to the SDR tab PUSH PLAN dialog.
  <name>.iq.bin      TX composite at the scenario sample rate: key with the
                     SDR-tab "file on radio" TX to play the scenario.
  scenarios/README.md   one-line description of every scenario.

The signals are framed with the standard sync/CRC so the channelizer decodes
them. Run:  python3 tools/build_scenarios.py   (writes everything, no radio needed).
"""
from __future__ import annotations
import json, os, sys
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # repo root, one up from tools/
sys.path.insert(0, _ROOT)
import numpy as np
from dpmg.modem.modem import Modem, ModemConfig
from dpmg.radio.dpmg_service import _resample_iq

OUT = "scenarios"
LOOP_S = 0.20
SYNC = {"sync_bits": "1011001000111011", "max_sync_errors": 2,
        "has_length": True, "crc": "CRC-16-CCITT"}

# per-modulation modem kwargs + plan params (audio-tone FSK matches spacing)
MOD = {
    "FSK":  (dict(modulation="FSK", symbol_rate=4800, samples_per_bit=40,
                  tone0=1200, tone1=2200),
             dict(symbol_rate=4800.0, tone_spacing_hz=1000.0), 1700.0),
    "4FSK": (dict(modulation="4FSK", symbol_rate=4800, samples_per_bit=40,
                  tone0=-1800, tone1=-600, tone2=600, tone3=1800),
             dict(symbol_rate=4800.0, tone_spacing_hz=1200.0,
                  dibit_mapping="binary"), 0.0),
    "GFSK": (dict(modulation="GFSK", symbol_rate=4800, samples_per_bit=40,
                  tone0=1200, tone1=2200, gfsk_bw_hz=2400),
             dict(symbol_rate=4800.0, tone_spacing_hz=1000.0,
                  gfsk_bw_hz=2400.0), 1700.0),
    "BPSK": (dict(modulation="BPSK", symbol_rate=4800, samples_per_bit=40),
             dict(symbol_rate=4800.0), 0.0),
    "QPSK": (dict(modulation="QPSK", symbol_rate=4800, samples_per_bit=40),
             dict(symbol_rate=4800.0), 0.0),
    "DQPSK": (dict(modulation="DQPSK", symbol_rate=4800, samples_per_bit=40),
              dict(symbol_rate=4800.0), 0.0),
    "OQPSK": (dict(modulation="OQPSK", symbol_rate=4800, samples_per_bit=40),
              dict(symbol_rate=4800.0), 0.0),
    "π/4-QPSK": (dict(modulation="π/4-QPSK", symbol_rate=4800,
                      samples_per_bit=40),
                 dict(symbol_rate=4800.0, plan_modulation="PI4-QPSK"), 0.0),
    "DSSS": (dict(modulation="DSSS", symbol_rate=2777.7778, samples_per_bit=90,
                  spread_factor=15),
             dict(symbol_rate=2777.7778, spread_factor=15), 0.0),
    "CSS":  (dict(modulation="CSS", css_sf=7, css_bw=125_000.0, css_os=1),
             dict(symbol_rate=976.5625, css_sf=7, css_bw=125_000.0, css_os=2),
             0.0),
}

# scenario = (name, sample_rate_hz, description, [ (mod, offset_hz, msg) ... ])
def _spread(mods, span, base_off=0.0):
    """place `mods` evenly across `span` centered on base_off"""
    offs = np.linspace(-span / 2, span / 2, len(mods)) + base_off
    return [(m, float(o), f"PING {i+1}") for i, (m, o) in enumerate(zip(mods, offs))]

SCENARIOS = [
    ("fsk_single", 1_920_000, "one FSK carrier at +200 kHz",
     [("FSK", 200_000.0, "PING 1")]),
    ("mixed_five", 1_920_000, "FSK/4FSK/GFSK/BPSK/QPSK across 1.5 MHz",
     _spread(["FSK", "4FSK", "GFSK", "BPSK", "QPSK"], 1_500_000)),
    ("psk_quad", 1_920_000, "BPSK/QPSK/DQPSK/OQPSK across 1.2 MHz",
     _spread(["BPSK", "QPSK", "DQPSK", "OQPSK"], 1_200_000)),
    ("spread_pair", 1_920_000, "DSSS + CSS, well separated",
     [("DSSS", -400_000.0, "PING 1"), ("CSS", 400_000.0, "PING 2")]),
    ("all_ten_mods", 3_840_000, "one of every modulation across 3 MHz",
     _spread(["FSK", "4FSK", "GFSK", "BPSK", "QPSK", "π/4-QPSK", "DQPSK",
              "OQPSK", "DSSS", "CSS"], 3_000_000)),
    ("wideband_10fsk", 10_000_000, "10 FSK carriers across 8 MHz",
     _spread(["FSK"] * 10, 8_000_000)),
    ("wideband_25fsk", 20_000_000, "25 FSK carriers across 16 MHz (stress)",
     _spread(["FSK"] * 25, 16_000_000)),
    ("narrowband_dense", 1_920_000, "8 FSK packed in 700 kHz",
     _spread(["FSK"] * 8, 700_000)),
]


def build(name, fs, desc, signals):
    nloop = int(fs * LOOP_S)
    mix = np.zeros(nloop, dtype=np.complex64)
    entries = []
    for mod, off, msg in signals:
        mkw, params, foff = MOD[mod]
        m = Modem(ModemConfig(**mkw))
        up = _resample_iq(m.transmit(msg), m.sample_rate, fs)
        reps = int(np.ceil(nloop / len(up)))
        s = np.tile(up, reps)[:nloop]
        nn = np.arange(nloop)
        mix += (s * np.exp(2j * np.pi * off / fs * nn)).astype(np.complex64)
        plan_mod = params.get("plan_modulation", mod)
        clean = {k: v for k, v in params.items() if k != "plan_modulation"}
        entries.append({"name": f"{mod}@{off/1e3:+.0f}k",
                        "modulation": plan_mod, "params": clean,
                        "offset_hz": off + foff, "power_db": 0.0,
                        "role": "known", "decode": True, "frame": dict(SYNC)})
    peak = float(np.max(np.abs(mix)) or 1.0)
    mix = (mix * (0.9 / peak)).astype(np.complex64)
    mix.tofile(os.path.join(OUT, f"{name}.iq.bin"))
    plan = {"schema_version": 1, "description": desc,
            "compose": {"tx_radio": "g40", "sample_rate_hz": fs,
                        "center_freq_hz": 425_025_000.0,
                        "rf_bandwidth_hz": fs * 0.8},
            "entries": entries}
    json.dump(plan, open(os.path.join(OUT, f"{name}.plan.json"), "w"), indent=1)
    return len(signals), mix.nbytes


def main():
    os.makedirs(OUT, exist_ok=True)
    rows = []
    for name, fs, desc, signals in SCENARIOS:
        nsig, nbytes = build(name, fs, desc, signals)
        rows.append((name, fs, nsig, nbytes, desc))
        print(f"  {name:18s} {nsig:2d} sig @ {fs/1e6:5.1f} MSps "
              f"({nbytes/1e6:5.1f} MB)  {desc}")
    with open(os.path.join(OUT, "README.md"), "w") as f:
        f.write("# Flight-test scenario library\n\n")
        f.write("Each scenario has a `.plan.json` (push to the radio / SDR-tab "
                "PUSH PLAN for RX decode) and a `.iq.bin` (key with SDR-tab "
                "\"file on radio\" TX). Center 425.025 MHz.\n\n")
        f.write("| scenario | signals | sample rate | size | description |\n")
        f.write("|---|---|---|---|---|\n")
        for name, fs, nsig, nbytes, desc in rows:
            f.write(f"| {name} | {nsig} | {fs/1e6:.1f} MSps | "
                    f"{nbytes/1e6:.1f} MB | {desc} |\n")
    print(f"\nwrote {len(rows)} scenarios to {OUT}/ (+ README.md)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
