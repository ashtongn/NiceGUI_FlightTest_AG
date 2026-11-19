"""
amc_scenarios.py: build a library of multi-signal wideband IQ scenarios for
testing automatic detection + modulation classification. Each scenario sums
several modulated signals at different frequency offsets and bandwidths into one
capture, saved as a BLUE/.cdif file (with a JSON ground-truth sidecar).

    python3 tools/amc_scenarios.py <out_dir>
"""
import json
import os
import random
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # repo root, one up from tools/
sys.path.insert(0, _ROOT)

import numpy as np
import scipy.signal as ss

from dpmg.dsp import cdif_reader as cr
from dpmg.modulation.modulator import get_modulator

SPB = 8
CENTER = 1_000_000_000
DUR = 0.15


def _fsk_tones(mod, sr):
    if mod in ("FSK", "2FSK", "GFSK"):
        return dict(tone0=-0.35 * sr, tone1=0.35 * sr)
    if mod == "4FSK":
        return dict(tone0=-0.45 * sr, tone1=-0.15 * sr, tone2=0.15 * sr, tone3=0.45 * sr)
    return {}


def _one_signal(mod, sr, wb, n_out):
    bits = "".join(random.choice("01") for _ in range(max(2000, n_out // (SPB * 4))))
    bb = np.asarray(get_modulator(mod, sr, SPB, **_fsk_tones(mod, sr)).modulate(bits).iq_samples,
                    dtype=np.complex64)
    from fractions import Fraction as _Fr
    fr = _Fr(int(wb), int(sr * SPB)).limit_denominator(10000)
    x = ss.resample_poly(bb, fr.numerator, fr.denominator).astype(np.complex64)
    if len(x) < n_out:
        x = np.tile(x, int(np.ceil(n_out / len(x))))
    return x[:n_out]


def build(name, wb, sigs, out_dir):
    """sigs: list of (modulation, offset_hz, symbol_rate_hz, amplitude)."""
    n = int(wb * DUR)
    t = np.arange(n) / wb
    comp = np.zeros(n, dtype=np.complex64)
    truth = []
    for mod, off, sr, amp in sigs:
        s = _one_signal(mod, sr, wb, n)
        comp += (amp * s * np.exp(2j * np.pi * off * t)).astype(np.complex64)
        truth.append({"modulation": mod, "offset_hz": off, "symbol_rate_hz": sr,
                      "bw_hz": sr, "rf_hz": CENTER + off})
    comp *= np.float32(0.3 / (np.max(np.abs(comp)) or 1.0))
    path = os.path.join(out_dir, f"{name}.cdif")
    cr.write_cdif(path, comp, wb, CENTER, fmt="CI")
    meta = {"name": name, "sample_rate_hz": wb, "center_hz": CENTER,
            "duration_s": DUR, "n_signals": len(sigs), "signals": truth}
    with open(os.path.join(out_dir, f"{name}.truth.json"), "w") as fh:
        json.dump(meta, fh, indent=2)
    print(f"{name:20s} {wb/1e6:6.2f} MSps  {len(sigs)} signals  "
          f"{os.path.getsize(path)/1e6:5.1f} MB  "
          f"[{', '.join(s[0] for s in sigs)}]")
    return meta


# (mod, offset_hz, symbol_rate_hz, amplitude): offsets stay inside +/-0.4*rate
SCENARIOS = [
    ("mix3_varbw", 30_720_000, [
        ("QPSK", -9e6, 300e3, 0.8), ("16QAM", -1e6, 600e3, 1.0), ("8PSK", 8e6, 150e3, 0.7)]),
    ("mix4_psk_qam", 30_720_000, [
        ("FSK", -10e6, 200e3, 0.9), ("QPSK", -3e6, 400e3, 0.8),
        ("8PSK", 4e6, 250e3, 0.8), ("16QAM", 10e6, 500e3, 1.0)]),
    ("mix5_dense", 30_720_000, [
        ("QPSK", -11e6, 200e3, 0.8), ("8PSK", -5.5e6, 200e3, 0.8), ("16QAM", 0e6, 300e3, 1.0),
        ("4FSK", 5.5e6, 250e3, 0.9), ("QPSK", 11e6, 200e3, 0.8)]),
    ("narrow_15M", 15_360_000, [
        ("QPSK", -4e6, 150e3, 0.9), ("8PSK", 0e6, 150e3, 0.9), ("16QAM", 4e6, 200e3, 1.0)]),
    ("fsk_mix", 30_720_000, [
        ("FSK", -8e6, 250e3, 0.9), ("4FSK", -2e6, 350e3, 0.9),
        ("QPSK", 4e6, 300e3, 0.8), ("16QAM", 10e6, 500e3, 1.0)]),
    ("wide_bw_spread", 40_000_000, [
        ("QPSK", -14e6, 500e3, 0.8), ("16QAM", -4e6, 1e6, 1.0),
        ("8PSK", 6e6, 400e3, 0.8), ("4FSK", 14e6, 600e3, 0.9)]),
]


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else "amc_scn"
    os.makedirs(out_dir, exist_ok=True)
    random.seed(7); np.random.seed(7)
    manifest = [build(n, wb, s, out_dir) for n, wb, s in SCENARIOS]
    with open(os.path.join(out_dir, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=2)
    print(f"\n{len(manifest)} scenarios written to {out_dir}")


if __name__ == "__main__":
    main()
