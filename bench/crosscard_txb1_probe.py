#!/usr/bin/env python3
"""
Cross-card "2"-jack TX handle probe (PORT-G-03 question)
========================================================
Runs inside the G40 dpmg_svc container (it needs libsidekiq), with the service's
radio closed (UNINIT): it claims both NV100 cards itself through the real
sdr_core.EpiqBackend cross-card path and only changes the handle rule in-process,
so what it measures is exactly what a one-line product change would ship.

  RX TRx1A (card 0, RXA1, single chan_mode)  <--[50 dB pad]--  TX card 1 J2/TRx2B
  TX card 1 in DUAL chan_mode streaming on --handle TXB1 (skiq handle 2) or TXA2 (1)

Reads a TX-off baseline PSD on card 0, keys a CW tone at --offset for --tx-s
seconds (only with --arm), reads the PSD again and reports the tone SNR at the
offset plus the three strongest peaks anywhere in the span. Always stop_tx,
stop_rx, skiq_exit (radio left free for the service).

From the x86 (service must be UNINIT first; checkout_common.teardown does it):
  scp bench/crosscard_txb1_probe.py sidekiq@192.168.0.22:<radio code folder>/bench/
  ssh sidekiq@192.168.0.22 docker exec dpmg_svc python3 /app_svc/bench/crosscard_txb1_probe.py --arm --handle TXB1

Same-card J2_TX_PIN check (Block 3 F4, move-3 cable TRx2A then 50 dB pad then TRx1A):
--tx-card 0 sends the open down the single-card dual path (RXA1 + TXA2 to B1, FIXED),
so the probe measures exactly what the console's move 3 does. One key per run:
  ... crosscard_txb1_probe.py --arm --tx-card 0 --handle TXA2 --offset 310000 --out /app_svc/bench/j2pin_connected.json
  (operator moves the pad end from TRx1A to a 50 ohm load, TRx1A open)
  ... crosscard_txb1_probe.py --arm --tx-card 0 --handle TXA2 --offset 310000 --out /app_svc/bench/j2pin_rx_open.json
J2_TX_PIN is opt-in (`docker exec -e DPMG_J2_TX_PIN=1`); on hardware (move 9) it does not
cure F4 (tx to J2 rc=-95, coupling only). Start-order variants: bench/samecard_j2_probe.py.
Real J2 radiation = connected tone far above the ~31 dB on-card coupling level
(cross-card through the same pad read +90 dB) and it falls back to that level
with TRx1A open. "j2_pin" in the JSON carries the routing writes/readbacks.
"""
from __future__ import annotations
import argparse, json, os, sys, threading, time, traceback
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import numpy as np                                   # noqa: E402
from dpmg.radio import sdr_core as sdr                               # noqa: E402
from dpmg.radio import skiq_bridge as skb                            # noqa: E402


def psd_db(iq: np.ndarray, fs: float, nfft: int = 4096):
    """Welch PSD (hann, no overlap), fftshifted, dB. Returns (freq_hz, db)."""
    n = (len(iq) // nfft) * nfft
    if n < nfft:
        return None, None
    x = iq[:n].reshape(-1, nfft) * np.hanning(nfft).astype(np.float32)
    p = np.mean(np.abs(np.fft.fftshift(np.fft.fft(x, axis=1), axes=1)) ** 2, axis=0)
    f = np.fft.fftshift(np.fft.fftfreq(nfft, 1.0 / fs))
    return f, 10.0 * np.log10(p + 1e-20)


def tone_report(f, db, offset_hz, win_hz=5e3, excl_dc_hz=20e3):
    """Tone power at offset (max within +-win) vs the median floor, and the three
    strongest peaks outside the DC hump."""
    floor = float(np.median(db))
    sel = np.abs(f - offset_hz) <= win_hz
    tone = float(np.max(db[sel])) if sel.any() else float("nan")
    peaks = []
    d = db.copy()
    d[np.abs(f) < excl_dc_hz] = -1e9
    for _ in range(3):
        i = int(np.argmax(d))
        peaks.append({"hz": int(round(f[i])), "db_over_floor": round(float(d[i] - floor), 1)})
        d[max(0, i - 8):i + 9] = -1e9
    return {"floor_db": round(floor, 1), "tone_db": round(tone, 1),
            "tone_snr_db": round(tone - floor, 1), "peaks": peaks}


def collect(b, seconds: float) -> np.ndarray:
    chunks, t0 = [], time.time()
    while time.time() - t0 < seconds:
        fr = b.recv()
        if fr is not None and len(fr.samples):
            chunks.append(np.asarray(fr.samples, dtype=np.complex64))
    return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.complex64)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--arm", action="store_true", help="radiate the tone (into the fitted pad only)")
    ap.add_argument("--handle", default="TXB1", choices=("TXB1", "TXA2", "TXA1"),
                    help="TX handle on the TX card (TXB1 = skiq 2, TXA2 = 1, TXA1 = 0/J1 control)")
    ap.add_argument("--rx", default="RXA1"); ap.add_argument("--rx-card", type=int, default=0)
    ap.add_argument("--tx-card", type=int, default=1)
    ap.add_argument("--rate", type=float, default=1.92e6); ap.add_argument("--center", type=float, default=425.025e6)
    ap.add_argument("--offset", type=float, default=300e3); ap.add_argument("--atten", type=float, default=30.0)
    ap.add_argument("--gain", type=int, default=255)
    ap.add_argument("--baseline-s", type=float, default=1.0); ap.add_argument("--tx-s", type=float, default=3.0)
    ap.add_argument("--pack", default="tree", choices=("tree", "first", "second"),
                    help="dual TX block layout: 'tree' = whatever skiq_bridge does now (second half "
                         "since DUAL_TX_SECOND_HALF); 'first' / 'second' force the data into the "
                         "first (A1) or second (B1/A2) channel half of each dual block. Measured on the G40: "
                         "first = nothing at the far RX on A2 or B1; second on B1 = +90 dB; second on A2 = nothing")
    ap.add_argument("--out", default=None, help="JSON result path (default: print only)")
    a = ap.parse_args()

    # the one product rule under test: which handles need (and get) dual mode on
    # the TX card. The tree says TXA2 only; the probe says TXB1 too.
    _orig = sdr.needs_dual_channel
    sdr.needs_dual_channel = lambda rx, tx, ex: (tx in ("TXA2", "TXB1")) or _orig(rx, tx, ex)

    if a.pack in ("first", "second"):
        # bench-only: force the samples into one channel half of each dual block
        # (header untouched), whichever half the tree's packing used. Patched on
        # the class before open().
        _orig_prep = skb.SkiqBridge._prepare_tx_blocks

        def _prep_forced(self, samples, iq_order_iq):
            blocks = _orig_prep(self, samples, iq_order_iq)
            if getattr(self, "tx_dual", False):
                hdr16 = skb.TX_HEADER_BYTES // 2
                step = getattr(self, "tx_block_samples", skb.TX_BLOCK_SAMPLES)
                first = blocks[:, hdr16:hdr16 + 2 * step]
                second = blocks[:, hdr16 + 2 * step:hdr16 + 4 * step]
                data = first if np.any(first) else second
                blocks = blocks.copy()
                blocks[:, hdr16:] = 0
                if a.pack == "first":
                    blocks[:, hdr16:hdr16 + 2 * step] = data
                else:
                    blocks[:, hdr16 + 2 * step:hdr16 + 4 * step] = data
            return np.ascontiguousarray(blocks)
        skb.SkiqBridge._prepare_tx_blocks = _prep_forced
        sdr.SkiqBridge._prepare_tx_blocks = _prep_forced

    res = {"probe": "crosscard_txb1", "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "handle": a.handle, "skiq_tx_hdl": skb.TX_HDL[a.handle], "pack": a.pack, "rx": a.rx,
           "rx_card": a.rx_card, "tx_card": a.tx_card, "rate_hz": int(a.rate), "center_hz": int(a.center),
           "offset_hz": int(a.offset), "tx_atten_db": a.atten, "rx_gain": a.gain, "armed": bool(a.arm),
           "open": None, "baseline": None, "keyed": None, "errors": [], "verdict": None}
    ex = {"card": a.rx_card, "tx_card": a.tx_card, "chan_mode": "single",
          "rf_bandwidth_hz": int(a.rate * 0.8), "tx_center_freq": int(a.center)}
    cfg = sdr.SDRConfig(sample_rate=a.rate, center_freq=a.center, rx_gain=a.gain, tx_gain=a.atten,
                        rx_antenna=a.rx, tx_antenna=a.handle, extras=ex).validate()
    b = sdr.make_backend("g40", cfg, simulate=False)
    b.frontend = None
    t0 = time.time()
    try:
        b.open()
    except Exception as e:
        res["errors"].append(f"open: {e}"); res["verdict"] = "OPEN-FAILED"
        print(json.dumps(res, indent=1)); return 2
    br = b._skiq
    res["open"] = {"seconds": round(time.time() - t0, 1), "rx_chan_mode": b._chan_mode,
                   "tx_chan_mode": getattr(b, "_tx_chan_mode", None),
                   "tx_card_readback": br.read_chan_mode(a.tx_card),
                   "rx_card_readback": br.read_chan_mode(a.rx_card),
                   "tx_hdl": b._tx_hdl, "tx_dual_packing": bool(getattr(br, "tx_dual", False)),
                   "tx_block_samples": getattr(br, "tx_block_samples", None),
                   "achieved_rate": b._achieved_rate, "tx_cfg_err": getattr(b, "_tx_cfg_err", None),
                   "rf_port_diag_tx": None}
    try:
        res["open"]["rf_port_diag_tx"] = str(br.rf_port_diag(a.tx_card, 0, b._tx_hdl))
    except Exception as e:
        res["open"]["rf_port_diag_tx"] = f"n/a ({e})"
    res["open"]["j2_pin"] = getattr(b, "_j2_pin", None)        # J2_TX_PIN (same-card only)
    print("[probe] open:", json.dumps(res["open"], default=str), flush=True)
    stop = threading.Event()
    sent = {"n": 0, "err": None}
    try:
        time.sleep(0.5)
        iq = collect(b, a.baseline_s)
        f, db = psd_db(iq, b._achieved_rate)
        res["baseline"] = {"samples": int(len(iq))}
        if f is not None:
            res["baseline"].update(tone_report(f, db, a.offset))
        print("[probe] baseline:", json.dumps(res["baseline"]), flush=True)
        if a.arm:
            from dpmg.dsp.tools_engine import cw_tone
            tone = cw_tone(b._achieved_rate, a.offset, 0.5, 0.5)

            def tx_loop():
                while not stop.is_set():
                    try:
                        b.send(tone); sent["n"] += 1
                    except Exception as e:
                        sent["err"] = str(e); break
            th = threading.Thread(target=tx_loop, daemon=True); th.start()
            time.sleep(0.6)
            iq = collect(b, a.tx_s)
            stop.set(); th.join(timeout=3.0)
            f, db = psd_db(iq, b._achieved_rate)
            res["keyed"] = {"samples": int(len(iq)), "sends": sent["n"], "send_err": sent["err"],
                            "tx_frames": b.health.tx_frames, "last_error": b.health.last_error,
                            "tx_streaming": bool(getattr(b, "_tx_streaming", False)),
                            "j2_pin": getattr(b, "_j2_pin", None)}
            if f is not None:
                res["keyed"].update(tone_report(f, db, a.offset))
            print("[probe] keyed:", json.dumps(res["keyed"]), flush=True)
            if sent["err"]:
                res["verdict"] = "TX-REFUSED"
            elif res["keyed"].get("tone_snr_db", 0) >= 10.0:
                res["verdict"] = "RADIATES"
            else:
                res["verdict"] = "NO-RF-AT-RX"
        else:
            res["verdict"] = "INIT-OK (unarmed)"
    except Exception as e:
        res["errors"].append(f"run: {e}"); traceback.print_exc()
        res["verdict"] = res["verdict"] or "ERROR"
    finally:
        stop.set()
        for step in ("stop_tx", "close"):
            try:
                getattr(b, step)()
            except Exception as e:
                res["errors"].append(f"{step}: {e}")
        print(f"[probe] teardown done (state {b.health.state.value}); radio free", flush=True)
    print(json.dumps(res, indent=1))
    if a.out:
        with open(a.out, "w") as fh:
            json.dump(res, fh, indent=1)
    return 0 if res["verdict"] in ("RADIATES", "INIT-OK (unarmed)") else 1


if __name__ == "__main__":
    sys.exit(main())
