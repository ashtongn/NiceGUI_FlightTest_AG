#!/usr/bin/env python3
"""
test_cfo_recovery.py: coherent / higher-order comm mods through the real
decode path (decode_pool._default_decode: demod_helper) under the carrier
offset of a two-radio link.

Modelled on a B200mini: HackRF Pro loopback: two undisciplined TCXOs leave
+170..+240 Hz between TX and RX at 4800 sym/s. Single-radio and GPSDO-locked
pairs have no offset, so they do not exercise what this covers: frames longer
than 104 bits in the QPSK data-aided path, CFO slope removal for pi/4-QPSK, and
carrier/AGC recovery for the higher-order slicer.

Each case: the bench's scenario (Modem burst: 7.68 MSps card rate at
+300 kHz, tiled like gen_scenario) is channelized to the production 48 kHz
channel, given a carrier offset, a random phase, 1.5 Hz/s drift and AWGN, and
must decode the bench's own 13-char message. Exit 0 = all passed.
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
from dpmg.modem.modem import Modem, ModemConfig
from dpmg.radio.dpmg_service import _resample_iq
from dpmg.scenario.gen_scenario_json import _canon_mod, _auto_sps
from test_rf_decode_replay import channelize

FS_CARD = 7_680_000.0
OFF = 300_000.0
FS_OUT = 48_000.0           # the worker's gate: symbol_rate * 10
SR = 4800
SYNC = {"sync_bits": "1011001000111011", "max_sync_errors": 2,
        "has_length": True, "crc": "CRC-16-CCITT"}

# (scenario modulation, plan modulation, message): the bench's MOD_CASES
CASES = [("BPSK", "BPSK", "BPSK TEST-001"),
         ("QPSK", "QPSK", "QPSK TEST-002"),
         ("pi/4-QPSK", "PI4-QPSK", "PI4QPSK TEST-003"),
         ("DQPSK", "DQPSK", "DQPSK TEST-004"),
         ("OQPSK", "OQPSK", "OQPSK TEST-005"),
         ("8PSK", "8PSK", "8PSK TEST-007"),
         ("16QAM", "16QAM", "16QAM TEST-008"),
         ("16APSK", "16APSK", "16APSK TEST-009")]
CFOS_HZ = (0.0, 200.0, -240.0)
SNR_DB = 20.0
DRIFT_HZ_S = 1.5


def build_channel(mod, msg, loop_s=0.2):
    m = Modem(ModemConfig(modulation=_canon_mod(mod), symbol_rate=SR,
                          samples_per_bit=_auto_sps(SR, 5000.0), amplitude=0.9))
    up = _resample_iq(m.transmit(msg), m.sample_rate, FS_CARD)
    iq = (up * np.exp(2j * np.pi * OFF / FS_CARD * np.arange(len(up)))).astype(np.complex64)
    nloop = int(FS_CARD * loop_s)
    mix = np.tile(iq, int(np.ceil(nloop / len(iq))))[:nloop]
    mix = (mix * (0.9 / (np.max(np.abs(mix)) or 1.0))).astype(np.complex64)
    return channelize(mix, OFF, fs_in=FS_CARD, fs_out=FS_OUT)


def impair(bb, cfo_hz, rng):
    n = np.arange(len(bb))
    f = cfo_hz + DRIFT_HZ_S * n / FS_OUT
    x = bb * np.exp(1j * rng.uniform(0, 2 * np.pi)) * np.exp(2j * np.pi * np.cumsum(f) / FS_OUT)
    p = np.mean(np.abs(x) ** 2)
    noise = (rng.standard_normal(len(x)) + 1j * rng.standard_normal(len(x))) * np.sqrt(p / 10 ** (SNR_DB / 10) / 2)
    return (x + noise).astype(np.complex64)


def main() -> int:
    rng = np.random.default_rng(0x5EED)
    only = [a for a in sys.argv[1:] if not a.startswith("-")]
    results = []
    print(f"CFO recovery (fs_out {FS_OUT:.0f}, {SR} sym/s, {SNR_DB:.0f} dB, {DRIFT_HZ_S} Hz/s drift):")
    for mod, plan, msg in CASES:
        if only and plan not in only:
            continue
        bb = build_channel(mod, msg)
        row = []
        for cfo in CFOS_HZ:
            spec = DecodeSpec(modulation=plan, fs_out=FS_OUT, sps=FS_OUT / SR,
                              demod_params={"symbol_rate": SR, "frame": SYNC}, lock={})
            r = _default_decode(0, impair(bb, cfo, rng), spec)
            ok = bool(r.valid and r.message == msg)
            results.append(ok)
            row.append(f"{cfo:+5.0f} Hz {'OK  ' if ok else 'FAIL'}")
        print(f"  {plan:9s} " + "  ".join(row))
    print(f"\n{sum(results)}/{len(results)} decoded")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
