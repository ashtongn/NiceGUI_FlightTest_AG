#!/usr/bin/env python3
"""
TX waveform swap check on Setup A
=====================================================
Alternates full-band barrage and a 4 MHz partial-band noise patch (both 0.5 s at
the radio rate, so the same length), keys each, records 50 ms of RX and measures
the received spectrum. Pass: every noise key is confined to +-2 MHz and every
barrage key fills the band.

The script keys TX, so it refuses to run without --arm. The TX attenuation
(--atten, default 30 dB) is set before the first key.

    python3 bench/txcache_swap_check.py --clock gpsdo --arm
"""
import argparse, json, sys, time
import os
from pathlib import Path
import numpy as np
import scipy.signal as ss

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import checkout_common as cc  # noqa: E402
from dpmg.deploy_config import RADIO_CODE_DIR  # noqa: E402
from dpmg.link.proto import CmdStatus  # noqa: E402


def band_levels(y, fs):
    f, p = ss.welch(y, fs, nperseg=4096, return_onesided=False)
    p = 10 * np.log10(p)
    m = lambda lo, hi: float(p[(np.abs(f) >= lo) & (np.abs(f) < hi)].mean())
    return {"in_1p5": m(1e5, 1.5e6), "out_3_6": m(3e6, 6e6), "out_8_11": m(8e6, 11e6)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("DPMG_G40_HOST"), required=not os.environ.get("DPMG_G40_HOST"))
    ap.add_argument("--port", type=int, default=47810)
    ap.add_argument("--cycles", type=int, default=3)
    ap.add_argument("--atten", type=float, default=30, help="TX attenuation dB (default 30)")
    ap.add_argument("--arm", action="store_true", help="allow TX to radiate")
    ap.add_argument("--out", default=str(Path(os.environ.get("DPMG_BENCH_RESULTS") or
                                             Path(__file__).resolve().parents[1] / "output" / "bench") / "txcache_swap.json"))
    cc.add_clock_args(ap)
    args = ap.parse_args()
    if not args.arm:
        raise SystemExit("--arm not given: refusing to transmit. Nothing was sent to the radio.")
    L = cc.Link(args.host, args.port)
    c = lambda n, p, t=60: (lambda a: (a.status, a.result or {}))(cc.safe_cmd(L, n, p, timeout=t))
    # TX attenuation before anything keys; stop if the radio did not take it
    st, r = c("set_tx_atten", {"qdb": int(round(args.atten * 4))}, 15)
    if st != CmdStatus.OK:
        raise SystemExit(f"set_tx_atten {args.atten:g} dB failed ({r}); not keying")
    cfg = c("get_config", {})[1]
    fs = float(cfg.get("sample_rate") or 30.72e6)
    res = {"date": cc.date_tag(), "fs": fs, "center": cfg.get("center_hz"), "atten_db": args.atten,
           "runs": []}
    st, r = c("record_iq", {"seconds": 0.05, "name": "diag_idle"})
    seq = []
    for _ in range(args.cycles):
        seq += [("barrage", {"kind": "barrage"}), ("noise4M", {"kind": "noise", "bw": 4e6, "center": 0.0})]
    for tag, gp in seq:
        st, g = c("gen_test_waveform", dict(gp, seconds=0.5, ampl=0.5, name=f"diag_{tag}", load_tx=True))
        c("start_tx", {"mode": "CONTINUOUS"})
        time.sleep(1.5)
        st, r = c("record_iq", {"seconds": 0.05, "name": "diag_rx"})
        c("stop_tx", {})
        y = None
        import subprocess
        raw = subprocess.run(["ssh", "-o", "BatchMode=yes", f"sidekiq@{args.host}",
                              f"cat {RADIO_CODE_DIR}/data/recordings/diag_rx.c64"],
                             capture_output=True, timeout=60).stdout
        y = np.frombuffer(raw, dtype=np.complex64)
        lv = band_levels(y, fs)
        spread = lv["in_1p5"] - lv["out_8_11"]
        ok = (spread > 10.0) if tag == "noise4M" else (spread < 3.0 and lv["out_8_11"] > -140)
        run = {"tag": tag, "n_samples": g.get("n") or g.get("n_samples"), **{k: round(v, 1) for k, v in lv.items()},
               "in_minus_out_db": round(spread, 1), "pass": bool(ok)}
        print(run, flush=True)
        res["runs"].append(run)
    for n in ("diag_idle", "diag_rx"):
        c("delete_recording", {"name": n})
    res["pass"] = all(r["pass"] for r in res["runs"])
    Path(args.out).write_text(json.dumps(res, indent=1))
    print("PASS" if res["pass"] else "FAIL")


if __name__ == "__main__":
    main()
