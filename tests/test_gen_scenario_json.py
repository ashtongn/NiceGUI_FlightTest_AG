"""
test_gen_scenario_json.py: round-trip check of the JSON FSK/4FSK generator.

Self-contained: builds every preset scenario into a throwaway temp dir, then
decodes each signal from its own .iq.bin using the plan params (offset, modulation,
symbol rate, reconstructed tones) and confirms the message comes back CRC-valid.
Exercises the full symbol-rate x tone-spacing sweep. No on-disk fixtures required,
so it passes on a fresh clone. Exit 0 = all signals in all scenarios decoded.
"""
from __future__ import annotations
import glob
import json
import os
import shutil
import sys
import tempfile
from math import gcd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
from scipy.signal import resample_poly
from dpmg.demodulation.demodulator import get_demodulator
from dpmg.framing import frame_codec as fc

checks = []


def _tones(entry):
    spacing = float(entry["params"]["tone_spacing_hz"])
    center = float(entry.get("meta", {}).get("tone_center_hz", 0.0))
    if entry["modulation"].upper() == "FSK":
        return dict(tone0=center - spacing / 2, tone1=center + spacing / 2)
    return dict(tone0=center - 1.5 * spacing, tone1=center - 0.5 * spacing,
                tone2=center + 0.5 * spacing, tone3=center + 1.5 * spacing)


def decode_one(iq, fs, entry):
    off = float(entry["offset_hz"])
    mod = entry["modulation"].upper()
    symrate = float(entry["params"]["symbol_rate"])
    n = np.arange(len(iq))
    bb = iq * np.exp(-2j * np.pi * off / fs * n)
    tgt = int(symrate * 32)                       # decode at 32 samples/symbol
    g = gcd(int(round(fs)), tgt)
    bb2 = resample_poly(bb, tgt // g, int(round(fs)) // g).astype(np.complex64)
    kw = _tones(entry)
    if mod == "4FSK":
        kw["dibit_mapping"] = entry["params"].get("dibit_mapping", "binary")
    for toff in range(32):
        d = get_demodulator(mod, float(tgt), symrate, 32, **kw)
        bits = d.demodulate(bb2[toff:]).bit_string
        r = fc.find_and_decode(bits, max_search=len(bits))
        if getattr(r, "valid", False):
            pl = getattr(r, "payload", None) or getattr(r, "message", None)
            if isinstance(pl, (bytes, bytearray)):
                pl = bytes(pl).decode("ascii", "replace")
            return pl
    return None


def main() -> int:
    from dpmg.scenario import gen_scenario_json as g
    # build the preset library into a throwaway dir so the test needs no committed
    # (and no git-ignored, clean-me-away) IQ fixtures; decode straight back out.
    tmp = tempfile.mkdtemp(prefix="genscn_test_")
    try:
        for spec in g.preset_specs():
            g.build_scenario(spec, tmp)           # writes <name>.iq.bin + <name>.plan.json
        for pj in sorted(glob.glob(os.path.join(tmp, "*.plan.json"))):
            plan = json.load(open(pj))
            name = os.path.basename(pj).split(".")[0]
            fs = float(plan["compose"]["sample_rate_hz"])
            iq = np.fromfile(os.path.join(tmp, f"{name}.iq.bin"), dtype=np.complex64)
            for e in plan["entries"]:
                msg = decode_one(iq, fs, e)
                ok = msg is not None and msg != ""
                checks.append(ok)
                print(f"  [{'PASS' if ok else 'FAIL'}] {name:16} {e['name']:16} "
                      f"sr={e['params']['symbol_rate']:.0f} sp={e['params']['tone_spacing_hz']:.0f}"
                      f"  -> {msg!r}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\nresult: {sum(checks)}/{len(checks)} signals decoded")
    return 0 if checks and all(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
