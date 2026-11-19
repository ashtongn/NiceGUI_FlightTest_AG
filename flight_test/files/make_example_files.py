#!/usr/bin/env python3
"""Example IQ files for the file playback card (FILE-07).

Writes, next to this script:

  fsk_pair_2m4.json          scenario spec, two FSK signals at 2.4 MS/s (not a G40 rate)
  fsk_pair_2m4.iq.bin        the spec built: raw complex64 at 2.4 MS/s, with
  fsk_pair_2m4.plan.json     the matching decode plan (carries the rate)
  fsk_pair_2m4.cdif          the same samples as a BLUE .cdif, rate and center in the header
  fsk_pair_2400k.iq.bin      the same samples, raw, the rate only in the file name
  fsk_pair_nomark.iq.bin     the same samples, raw, no rate anywhere (the hard case)
  qpsk_msg_5m.c64            a QPSK message at 5 MS/s (not a G40 rate), with
  qpsk_msg_5m.c64.json       the carve sidecar (out_rate_hz, band_center_hz)
  bpsk_msg_1m92.iq.bin       a BPSK message at 1.92 MS/s (a G40 rate), with
  bpsk_msg_1m92.plan.json    its decode plan
  MANIFEST.md                what each file is, its rate, size and sha256

Run from anywhere with the flight repo on the path:

  python3 make_example_files.py [--repo <flight copy root>] [--center 1325e6]

Nothing here touches a radio. The files are copied to the radio by the card.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=os.path.normpath(os.path.join(HERE, "..", "..")))
    ap.add_argument("--center", type=float, default=1325e6, help="RF center written into the specs and the cdif")
    ap.add_argument("--loop-s", type=float, default=0.25, help="loop length of each file in seconds")
    a = ap.parse_args()
    sys.path.insert(0, a.repo)
    import numpy as np
    from dpmg.scenario.gen_scenario_json import build_scenario
    from dpmg.dsp import cdif_reader
    from dpmg.dsp.region_extract import CarveMeta
    from dataclasses import asdict

    out = HERE
    rows = []

    def record(name, rate, note):
        p = os.path.join(out, name)
        h = hashlib.sha256(open(p, "rb").read()).hexdigest()[:16]
        rows.append((name, rate, os.path.getsize(p), h, note))

    # A: the odd-rate scenario spec, built
    spec_a = {
        "name": "fsk_pair_2m4",
        "description": "Two FSK signals with messages, authored at 2.4 MS/s, a rate the G40 does not run",
        "sample_rate_hz": 2_400_000, "center_freq_hz": a.center, "loop_s": a.loop_s, "peak_fs": 0.45,
        "signals": [
            {"name": "FSK low", "modulation": "FSK", "symbol_rate": 4800, "tone_spacing_hz": 5000,
             "offset_hz": -300_000, "amplitude": 0.8, "message": "FILE A LOW"},
            {"name": "FSK high", "modulation": "FSK", "symbol_rate": 9600, "tone_spacing_hz": 10000,
             "offset_hz": 400_000, "amplitude": 0.8, "message": "FILE A HIGH"},
        ]}
    json.dump(spec_a, open(os.path.join(out, "fsk_pair_2m4.json"), "w"), indent=1)
    build_scenario(spec_a, out)
    record("fsk_pair_2m4.json", 2.4e6, "scenario spec (A); the planner builds it on the radio")
    record("fsk_pair_2m4.iq.bin", 2.4e6, "A built: rate in fsk_pair_2m4.plan.json")
    record("fsk_pair_2m4.plan.json", 2.4e6, "decode plan for A")
    iq = np.fromfile(os.path.join(out, "fsk_pair_2m4.iq.bin"), dtype=np.complex64)

    # B: the same as a .cdif with the rate and center in the header
    cdif_reader.write_cdif(os.path.join(out, "fsk_pair_2m4.cdif"), iq, 2_400_000, a.center,
                           keywords={"COMMENT": "DPMG flight test example, FSK pair at 2.4 MS/s"})
    back, meta = cdif_reader.read_cdif(os.path.join(out, "fsk_pair_2m4.cdif"))
    assert abs(meta.sample_rate_hz - 2.4e6) < 1 and len(back) == len(iq)
    record("fsk_pair_2m4.cdif", 2.4e6, "B: BLUE header carries 2.4 MS/s and the center")

    # E and F: raw copies, rate in the name only, and no rate at all
    shutil.copyfile(os.path.join(out, "fsk_pair_2m4.iq.bin"), os.path.join(out, "fsk_pair_2400k.iq.bin"))
    record("fsk_pair_2400k.iq.bin", 2.4e6, "E: raw, the rate only in the file name (_2400k)")
    shutil.copyfile(os.path.join(out, "fsk_pair_2m4.iq.bin"), os.path.join(out, "fsk_pair_nomark.iq.bin"))
    record("fsk_pair_nomark.iq.bin", 2.4e6, "F: raw, no rate marker; the operator must supply 2.4 MS/s")

    # C: a QPSK message at 5 MS/s as a .c64 recording with a carve sidecar
    spec_c = {
        "name": "qpsk_msg_5m", "sample_rate_hz": 5_000_000, "center_freq_hz": a.center,
        "loop_s": a.loop_s, "peak_fs": 0.45,
        "signals": [{"name": "QPSK", "modulation": "QPSK", "symbol_rate": 50_000, "offset_hz": 600_000,
                     "amplitude": 0.8, "message": "FILE C QPSK 5M"}]}
    build_scenario(spec_c, out)
    iq_c = np.fromfile(os.path.join(out, "qpsk_msg_5m.iq.bin"), dtype=np.complex64)
    iq_c.tofile(os.path.join(out, "qpsk_msg_5m.c64"))
    meta_c = CarveMeta(src_center_hz=a.center, band_center_hz=a.center, band_bw_hz=4_000_000.0,
                       in_rate_hz=5_000_000.0, out_rate_hz=5_000_000.0, decim=1, filter="none",
                       n_out=int(len(iq_c)))
    json.dump(asdict(meta_c), open(os.path.join(out, "qpsk_msg_5m.c64.json"), "w"), indent=1)
    for n in ("qpsk_msg_5m.iq.bin", "qpsk_msg_5m.plan.json"):
        os.remove(os.path.join(out, n))
    record("qpsk_msg_5m.c64", 5e6, "C: raw complex64 recording, rate in the .c64.json sidecar")
    record("qpsk_msg_5m.c64.json", 5e6, "sidecar for C")

    # D: a BPSK message at a native G40 rate
    spec_d = {
        "name": "bpsk_msg_1m92", "sample_rate_hz": 1_920_000, "center_freq_hz": a.center,
        "loop_s": a.loop_s, "peak_fs": 0.45,
        "signals": [{"name": "BPSK", "modulation": "BPSK", "symbol_rate": 9600, "offset_hz": 250_000,
                     "amplitude": 0.8, "message": "FILE D BPSK"}]}
    build_scenario(spec_d, out)
    record("bpsk_msg_1m92.iq.bin", 1.92e6, "D: raw at a G40 rate, rate in bpsk_msg_1m92.plan.json")
    record("bpsk_msg_1m92.plan.json", 1.92e6, "decode plan for D")

    with open(os.path.join(out, "MANIFEST.md"), "w") as f:
        f.write("# Example files for the file playback card (FILE-07)\n\n")
        f.write(f"Built by make_example_files.py, center {a.center / 1e6:g} MHz, loop {a.loop_s:g} s. "
                "Copy the whole folder to the radio as data/waveforms/flight/ (the card says how).\n\n")
        f.write("| File | Rate (MS/s) | Bytes | sha256 (first 16) | What it is |\n|---|---|---|---|---|\n")
        for name, rate, size, h, note in rows:
            f.write(f"| `{name}` | {rate / 1e6:g} | {size} | {h} | {note} |\n")
        f.write("\nRates the G40 runs natively, from dpmg/radio/sdr_core.py: 1.92, 2.4576, 2.8, 3.84, 4.0, "
                "4.9152, 5.6, 7.68 ... 61.44 MS/s. 2.4 and 5.0 MS/s are not in that list, so B, C, E and F "
                "must be resampled before they play.\n")
    for r in rows:
        print(f"{r[0]:28} {r[1] / 1e6:6g} MS/s {r[2]:>10} B")
    return 0


if __name__ == "__main__":
    sys.exit(main())
