#!/usr/bin/env python3
"""
test_uplink_cfo_9600.py: the flight uplink set (9600 Bd BPSK, QPSK, OQPSK,
8PSK from make_flight_plans.py) through the real decode path
(decode_pool._default_decode: demod_helper) at the G40's 1.92 MS/s gate,
under the carrier offset of two free-running radios.

On the flight bench (X40-0001 keying, G40-0001 receiving at 1.92 MS/s, both on
their internal clocks) BPSK and QPSK decoded every time, OQPSK never, and 8PSK
came and went between runs, all at about 48 dB slot SNR. Two TCXOs at 915 MHz
1 to 3 ppm apart leave 0.9 to 2.7 kHz. OQPSK had no coarse carrier step (its
per-symbol slope wraps past 600 Hz at 4800 sym/s) and the 8PSK search stopped
at 0.3 Rs (2880 Hz). This sweeps the offset out to 5 kHz.

Each case: the signal is built by gen_scenario_json.build_signal at 1.92 MS/s
(what gen_scenario builds on the radio), tiled like build_scenario,
channelized to the worker's 96 kHz gate (lcm of the plan rates x 10), given a
carrier offset, a random phase, 1.5 Hz/s drift and AWGN, and must decode the
plan's own message. Exit 0 = all passed.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import numpy as np

from dpmg.dsp.decode_pool import DecodeSpec, _default_decode
from dpmg.scenario.gen_scenario_json import build_signal
from test_rf_decode_replay import channelize

FS_RX = 1_920_000.0         # G40 rate in the flight plans
FS_OUT = 96_000.0           # the worker's gate for one 9600 Bd plan: 10 samples/symbol
SR = 9600.0
# (modulation, offset from the plan generator): the uplink set as on the bench
CASES = [("BPSK", -200_000.0), ("QPSK", -100_000.0),
         ("OQPSK", 500_000.0), ("8PSK", 100_000.0)]
CFOS_HZ = (0.0, 700.0, -1500.0, 2600.0, -3000.0, 4500.0)
SNR_DB = 20.0
DRIFT_HZ_S = 1.5
N_OUT = 24_000              # 0.25 s of channel: a dozen or more frames of each


def build_channel(mod, off, loop_s=0.2):
    sig = {"modulation": mod, "symbol_rate": SR, "offset_hz": off,
           "amplitude": 0.9, "message": f"UPLINK {mod}"}
    iq, entry = build_signal(sig, FS_RX)
    nloop = int(FS_RX * loop_s)
    mix = np.tile(iq, int(np.ceil(nloop / len(iq))))[:nloop]
    mix = (mix * (0.45 / (np.max(np.abs(mix)) or 1.0))).astype(np.complex64)
    bb = channelize(mix, off, fs_in=FS_RX, fs_out=FS_OUT)
    return bb[1000:1000 + N_OUT], entry


def impair(bb, cfo_hz, rng):
    n = np.arange(len(bb))
    f = cfo_hz + DRIFT_HZ_S * n / FS_OUT
    x = bb * np.exp(1j * rng.uniform(0, 2 * np.pi)) * np.exp(2j * np.pi * np.cumsum(f) / FS_OUT)
    p = np.mean(np.abs(x) ** 2)
    noise = (rng.standard_normal(len(x)) + 1j * rng.standard_normal(len(x))) * np.sqrt(p / 10 ** (SNR_DB / 10) / 2)
    return (x + noise).astype(np.complex64)


def main() -> int:
    only = [a for a in sys.argv[1:] if not a.startswith("-")]
    results = []
    print(f"Uplink CFO (fs_out {FS_OUT:.0f}, {SR:.0f} Bd, {SNR_DB:.0f} dB, {DRIFT_HZ_S} Hz/s drift):")
    for mod, off in CASES:
        if only and mod not in only:
            continue
        bb, entry = build_channel(mod, off)
        msg = f"UPLINK {mod}"
        row = []
        for cfo in CFOS_HZ:
            # one draw per case, so a case does not depend on which ran before it
            rng = np.random.default_rng([0x9600, CASES.index((mod, off)), int(cfo) & 0xFFFF])
            spec = DecodeSpec(modulation=entry["modulation"], fs_out=FS_OUT, sps=FS_OUT / SR,
                              demod_params={**entry["params"], "frame": entry["frame"]},
                              lock={})
            r = _default_decode(0, impair(bb, cfo, rng), spec)
            ok = bool(r.valid and r.message == msg)
            results.append(ok)
            row.append(f"{cfo:+5.0f} Hz {'OK  ' if ok else 'FAIL'}")
        print(f"  {mod:6s} " + "  ".join(row))
    print(f"\n{sum(results)}/{len(results)} decoded")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
