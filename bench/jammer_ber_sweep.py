#!/usr/bin/env python3
"""
jammer_ber_sweep.py: BER vs jamming power, modulated (symbol) jammer vs barrage.

Reproduces Amuru & Buehrer, "Optimal Jamming Strategies in Digital Communications:
Impact of Modulation" (GLOBECOM 2014): against an M-QAM/PSK victim, a modulated
(e.g. QPSK) jammer raises the victim's error probability more than equal-average-
power AWGN ("barrage"). The script sweeps J/S and compares the two.

Two modes:

  --sim (default): pure-numpy symbol-synchronous sweep (tools_engine.ber_vs_js).
    Fast, deterministic, no hardware. This is the paper's result.

      python3 bench/jammer_ber_sweep.py --victim 16qam --snr 20

  --loopback: build a combined victim(known sequence)+jammer baseband at each J/S,
    play it over the single-card TX to RX loopback, capture it back (capture_iq),
    and measure BER offline after timing/phase sync. Compares the same two jammers
    over the real RF chain. Needs the one-card FDD loopback up (TX port to pad to
    RX port) and --arm to radiate.

      python3 bench/jammer_ber_sweep.py --loopback --host 192.168.0.22 --arm

Writes a CSV and a plotly-ready JSON next to the script (or --out DIR).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))   # repo root
sys.path.insert(0, _HERE)

import numpy as np                                   # noqa: E402
from dpmg.dsp import tools_engine as te                            # noqa: E402
from dpmg.deploy_config import RADIO_CODE_DIR                      # noqa: E402

VICTIMS = ("bpsk", "qpsk", "8psk", "16qam", "64qam")


# reporting
def _print_table(res: dict, jammers):
    print(f"\nBER vs J/S, victim {res['victim'].upper()} @ {res['snr_db']:g} dB "
          f"Es/N0   (n_sym per point varies)")
    hdr = "  J/S dB " + "".join(f"{j:>12}" for j in jammers) + "   modQPSK/barrage"
    print(hdr); print("  " + "-" * (len(hdr) - 2))
    base = jammers[-1]                                # last listed = reference
    for i, js in enumerate(res["js_db"]):
        row = f"  {js:>6g} "
        for j in jammers:
            row += f"{res[j][i]:>12.4e}"
        ref = res[base][i]
        rel = (res[jammers[0]][i] / ref) if ref > 0 else float("nan")
        row += f"   x{rel:>5.2f}"
        print(row)
    print()


def _write_outputs(res: dict, jammers, outdir, tag):
    os.makedirs(outdir, exist_ok=True)
    csv = os.path.join(outdir, f"ber_sweep_{tag}.csv")
    with open(csv, "w") as f:
        f.write("js_db," + ",".join(jammers) + "\n")
        for i, js in enumerate(res["js_db"]):
            f.write(f"{js}," + ",".join(f"{res[j][i]:.6e}" for j in jammers) + "\n")
    # plotly-ready traces
    plot = {"title": f"BER vs J/S -- {res['victim'].upper()} @ {res['snr_db']:g} dB",
            "xaxis": "J/S (dB)", "yaxis": "BER", "ylog": True,
            "traces": [{"name": j, "x": res["js_db"], "y": res[j]} for j in jammers]}
    if res.get("gps"):
        plot["gps"] = res["gps"]
    pj = os.path.join(outdir, f"ber_sweep_{tag}.json")
    json.dump(plot, open(pj, "w"), indent=2)
    print(f"wrote {csv}\n      {pj}")


# sim mode
def run_sim(args):
    jammers = tuple(args.jammers)
    js = list(range(args.js_lo, args.js_hi + 1, args.js_step))
    res = te.ber_vs_js(args.victim, snr_db=args.snr, js_db_list=js,
                       n_sym=args.nsym, seed=args.seed, jammers=jammers)
    _print_table(res, jammers)
    _write_outputs(res, jammers, args.out or _HERE, f"sim_{args.victim}_{args.snr:g}dB")
    worse = all(res[jammers[0]][i] >= res[jammers[-1]][i] - 1e-4
                for i in range(len(js)))
    print(f"modulated '{jammers[0]}' >= '{jammers[-1]}' (barrage) at every J/S: "
          f"{'YES (matches the paper)' if worse else 'NO'}")


# loopback mode (over-RF)
def _sync_ber(rx, fs, sym_rate, tx_syms, victim, center, rolloff=0.35):
    """Timing+phase sync a capture against the known victim symbol sequence,
    then measure BER. Applies the matched RRC filter, correlates against the
    (matched-filtered) known replica for symbol timing, samples at symbol
    centers, derotates by the ML phase, and compares Gray-labelled bits. The
    matched filter is needed for a dense victim (16/64-QAM): without it even
    a clean capture reads ~0.13 BER from residual ISI (with it a clean 16QAM
    loopback reads <1e-4). The TX and RX share the single card's LO, so there is
    no meaningful CFO to track: a constant ML phase is sufficient."""
    const = te.constellation(victim)
    sps = fs / max(float(sym_rate), 1.0)
    h = te.rrc_taps(sps, rolloff)                    # matched filter
    rxf = np.convolve(rx, h, mode="same")
    ref = te.symbol_waveform(fs, sym_rate, const[tx_syms], center=center, ampl=1.0)
    reff = np.convolve(ref, h, mode="same")
    # coarse timing via cross-correlation magnitude (works through the jammer)
    xc = np.abs(np.correlate(rxf, reff[:min(len(reff), 4000)], mode="valid"))
    if xc.size == 0:
        return float("nan"), 0
    start = int(np.argmax(xc))
    idx = start + np.round(np.arange(len(tx_syms)) * sps).astype(int)
    idx = idx[idx < len(rxf)]
    samp = rxf[idx]
    known = const[tx_syms[:len(idx)]]
    if len(samp) < 16:
        return float("nan"), len(samp)
    phi = np.angle(np.vdot(known, samp))             # ML residual constant phase
    samp = samp * np.exp(-1j * phi)
    samp = samp / (np.sqrt(np.mean(np.abs(samp) ** 2)) or 1.0)
    rx_idx = te._hard_decide(samp.astype(np.complex128), const)
    bpsym = int(round(np.log2(len(const))))
    xor = tx_syms[:len(idx)] ^ rx_idx
    be = np.unpackbits(xor.astype(np.uint8)[:, None], axis=1)[:, -bpsym:].sum()
    return float(be) / (len(idx) * bpsym), len(idx)


def run_loopback(args):
    from fsk_loopback_test import Link, init_loopback   # noqa: E402
    import checkout_common as cc                        # noqa: E402
    if not args.host:
        sys.exit("--loopback needs --host")
    rate = args.rate
    center = args.center
    sym_rate = args.sym_rate
    rng = np.random.default_rng(args.seed)
    const = te.constellation(args.victim)
    nsym = args.loop_nsym
    tx_syms = rng.integers(0, len(const), nsym)
    victim_bb = te.symbol_waveform(rate, sym_rate, const[tx_syms],
                                   center=args.offset, ampl=1.0)

    link = Link(args.host, args.port)
    args.clock = cc.resolve_clock(args.clock, link.serial)
    # --tx-card 1 = cross-card (TX TRx1B then pad then RX TRx1A, Setup A);
    # default None keeps the single-card FDD loopback
    init_loopback(link, rate, center, atten=args.atten, gain=args.gain,
                  tx_card=args.tx_card, clock=args.clock, lock_timeout=args.lock_timeout)
    link.cmd("start_rx", {})
    jammers = tuple(args.jammers)
    js_list = list(range(args.js_lo, args.js_hi + 1, args.js_step))
    res = {"victim": args.victim, "snr_db": args.snr, "js_db": js_list,
           "gps": cc.gps_summary(link, args.clock)}
    for j in jammers:
        res[j] = []
    remote = f"sidekiq@{args.host}"
    # scp runs on the host, so it must use the host path; the service runs inside
    # the container and only accepts paths under its root (_within_base confines
    # to /app_svc). The radio code dir is bind-mounted from HOST_CODE on the host
    # to SVC in the container.
    HOST_CODE = args.radio_code_dir.rstrip("/")
    SVC = args.svc_root.rstrip("/")
    HOST_ROOT = f"{HOST_CODE}/data/waveforms"               # scp destination (host)
    SVC_ROOT = f"{SVC}/data/waveforms"                      # set_tx_waveform (container)

    def _to_host(ctr_path):                                 # map /app_svc/... to the host path
        return ctr_path.replace(SVC, HOST_CODE, 1) if ctr_path.startswith(SVC) else ctr_path

    for j in jammers:
        for js in js_list:
            # equal average power: both baseband streams are RMS-normalized, so
            # scale the jammer by 10^(js/20) relative to the victim.
            if j in ("barrage", "awgn", "noise"):
                jam = te.band_noise(rate, rate * 0.9, len(victim_bb) / rate, ampl=1.0)
            else:
                jam = te.modulated_jam(rate, sym_rate, len(victim_bb) / rate,
                                       ampl=1.0, mod=j, center=args.offset)
            jam = jam[:len(victim_bb)]
            jr = np.sqrt(np.mean(np.abs(jam) ** 2)) or 1.0
            jam = jam / jr * (10.0 ** (js / 20.0))
            comb = victim_bb + jam
            comb = (comb / (np.max(np.abs(comb)) or 1.0) * args.ampl).astype(np.complex64)
            fn = f"berjam_{j}_{js}"
            local = os.path.join("/tmp", fn + ".c64")
            comb.tofile(local)
            subprocess.run(["scp", "-q", local, f"{remote}:{HOST_ROOT}/{fn}.c64"],
                           check=True, timeout=60)
            if not args.arm:
                res[j].append(float("nan")); continue
            link.cmd("set_tx_waveform", {"kind": "file",
                     "path": f"{SVC_ROOT}/{fn}.c64"}, timeout=30)
            link.cmd("start_tx", {"mode": "CONTINUOUS"})
            time.sleep(0.3)
            cap = link.cmd("capture_iq", {"seconds": args.cap_s, "name": fn},
                           timeout=30)
            link.cmd("stop_tx", {})
            if cap.status != 0:
                res[j].append(float("nan")); continue
            cpath = _to_host(cap.result["path"])           # service returns a ctr path
            lcap = os.path.join("/tmp", fn + "_rx.c64")
            subprocess.run(["scp", "-q", f"{remote}:{cpath}", lcap],
                           check=True, timeout=60)
            rx = np.fromfile(lcap, dtype=np.complex64)
            # shift the victim offset back to DC before sync
            if args.offset:
                rx = rx * np.exp(-2j * np.pi * (args.offset / rate) * np.arange(len(rx)))
            ber, n = _sync_ber(rx.astype(np.complex128), rate, sym_rate,
                               tx_syms, args.victim, 0.0)
            res[j].append(ber)
            print(f"  {j:>8} J/S {js:>3} dB -> BER {ber:.3e}  ({n} sym)")
    link.cmd("stop_rx", {}); link.cmd("close", {})
    _print_table(res, jammers)
    _write_outputs(res, jammers, args.out or _HERE, f"loop_{args.victim}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--loopback", action="store_true", help="over-RF mode (needs --host)")
    ap.add_argument("--victim", default="16qam", choices=VICTIMS)
    ap.add_argument("--jammers", nargs="+", default=["qpsk", "barrage"],
                    help="first is the modulated jammer, last is the reference")
    ap.add_argument("--snr", type=float, default=20.0, help="victim Es/N0 dB (sim)")
    ap.add_argument("--js-lo", type=int, default=-10)
    ap.add_argument("--js-hi", type=int, default=20)
    ap.add_argument("--js-step", type=int, default=2)
    ap.add_argument("--nsym", type=int, default=200_000, help="symbols/point (sim)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", default="", help="output dir (default: bench/)")
    # loopback
    ap.add_argument("--host", default="")
    ap.add_argument("--port", type=int, default=47810, help="service cmd port")
    ap.add_argument("--radio-code-dir", default=RADIO_CODE_DIR,
                    dest="radio_code_dir",
                    help="host code dir on the radio (scp destination root)")
    ap.add_argument("--svc-root", default="/app_svc", dest="svc_root",
                    help="container root the code dir is bind-mounted to")
    ap.add_argument("--arm", action="store_true", help="radiate over the loopback")
    ap.add_argument("--rate", type=float, default=1_920_000.0)
    ap.add_argument("--center", type=float, default=425_025_000.0)
    ap.add_argument("--sym-rate", type=float, default=120_000.0, dest="sym_rate")
    ap.add_argument("--offset", type=float, default=0.0, help="victim baseband offset Hz")
    ap.add_argument("--ampl", type=float, default=0.6, help="combined TX peak (FS)")
    ap.add_argument("--atten", type=float, default=30.0)
    ap.add_argument("--gain", type=float, default=255.0)
    ap.add_argument("--loop-nsym", type=int, default=20_000, dest="loop_nsym")
    ap.add_argument("--cap-s", type=float, default=0.1, dest="cap_s")
    ap.add_argument("--tx-card", type=int, default=None, dest="tx_card",
                    help="loopback: put TX on this card (1 = cross-card Setup A); "
                         "default single-card FDD")
    sys.path.insert(0, _HERE)
    import checkout_common as cc                        # noqa: E402
    cc.add_clock_args(ap)
    args = ap.parse_args()
    (run_loopback if args.loopback else run_sim)(args)


if __name__ == "__main__":
    main()
