#!/usr/bin/env python3
"""
spur_probe.py: find narrow spurs in the live RX band (no TX)
==============================================================
Records a short full-band RX capture on the radio (record_iq), copies it back,
and lists the strongest narrow lines by absolute frequency (DC +-100 kHz and the
outer 10% of the band ignored). Used to tell a fixed-frequency spur (clock
harmonic / outside signal: same absolute frequency after a retune) from a
sample-rate spur (same offset from center after a retune).

    python3 bench/spur_probe.py --tag now
"""
import os
import argparse, json, subprocess, sys, time
from pathlib import Path
import numpy as np
import scipy.signal as ss

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dpmg.deploy_config import RADIO_CODE_DIR  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import checkout_common as cc  # noqa: E402


def probe(link, host, tag, seconds=0.02, top=6):
    st, r = (lambda a: (a.status, a.result or {}))(cc.safe_cmd(link, "record_iq",
                                                                {"seconds": seconds, "name": "diag_spur"}, timeout=60))
    if st != 0:
        return {"tag": tag, "err": r}
    fs, fc = float(r["rate_hz"]), float(r["band_center_hz"])
    raw = subprocess.run(["ssh", "-o", "BatchMode=yes", f"sidekiq@{host}",
                          f"cat {RADIO_CODE_DIR}/data/recordings/diag_spur.c64"],
                         capture_output=True, timeout=60).stdout
    cc.safe_cmd(link, "delete_recording", {"name": "diag_spur"}, timeout=20)
    y = np.frombuffer(raw, dtype=np.complex64)
    f, p = ss.welch(y, fs, nperseg=16384, return_onesided=False)
    i = np.argsort(f); f, p = f[i], 10 * np.log10(p[i] + 1e-30)
    keep = (np.abs(f) > 100e3) & (np.abs(f) < 0.4 * fs)
    floor = float(np.median(p[keep]))
    pk, _ = ss.find_peaks(np.where(keep, p, -999), height=floor + 10, distance=8)
    pk = sorted(pk, key=lambda k: -p[k])[:top]
    return {"tag": tag, "fs": fs, "center": fc, "floor_db": round(floor, 1),
            "spurs": [{"abs_mhz": round(float(fc + f[k]) / 1e6, 4), "off_mhz": round(float(f[k]) / 1e6, 4),
                       "above_db": round(float(p[k] - floor), 1)} for k in pk]}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("DPMG_G40_HOST"), required=not os.environ.get("DPMG_G40_HOST"))
    ap.add_argument("--tag", default="now")
    a = ap.parse_args()
    print(json.dumps(probe(cc.Link(a.host, 47810), a.host, a.tag), indent=1))
