#!/usr/bin/env python3
"""
Same-card "2"-jack TX start-order probe (Block 3 F4)
====================================================
F4: same-card dual TX on TRx2x (RX TRx1x + TX TRx2x on one NV100, TXA2 driven as
handle B1, data in the B1 half, FIXED ports) only reaches the same card's RX by
on-card coupling (the level at TRx1x does not change with the RX jack open),
while the identical handle + packing radiates cross-card (move 2, +90 dB). The
J2_TX_PIN routing experiment did not change that (move 9: tx to J2 rc=-95, coupling
only). The remaining structural difference between the two paths is that only the
same-card path configures and starts a receiver on the transmitting card, and it
does both before the transmitter starts. This probe changes only that order,
through the real sdr_core.EpiqBackend open + send path, one key per run:

  --variant baseline     product order: RX config + start_rx in open(), TX on first send
  --variant tx_first     RX config in open(), start_rx held until TX is streaming
  --variant rx_after_tx  every RX-only call held (LO, gain mode/index, DC cal, block
                         size, start_rx) until TX is streaming, so the TX card is set up
                         exactly like the cross-card TX card and the receiver comes after
  --variant rx_cycle     product open, then stop_rx then start TX then start_rx again
                         (what a product fix would do on every key)

Reading the ladder (cable connected vs RX jack open, as for F4):
  baseline coupling-level, tx_first above: streaming order (fix = start TX first /
                                             restart RX after start_tx: rx_cycle)
  only rx_after_tx above: an RX config call on the TX card (bisect
                                             with --hold)
  nothing above: not start order; RX on the TX card at all
The PSD is split into an early window (first --early-s after start_rx) and the rest,
so a J2 that radiates and then dies when the receiver comes up is visible too. TX-off
reference collected after stop_tx with RX still running.

Runs inside the G40 dpmg_svc container with the service radio UNINIT
(checkout_common.teardown first); only ever keys into the fitted pad. Staging dir
pattern as the J2_TX_PIN check (/app_svc/staging_j2pin with sdr_core.py,
skiq_bridge.py, bench/ copied in):
  docker exec -w /app_svc/staging_j2pin dpmg_svc python3 bench/samecard_j2_probe.py \\
      --arm --card 1 --variant tx_first --out m9_tx_first_connected.json
Move-9 cable (TRx2B then 50 dB then TRx1B) = --card 1; move-3 cable (TRx2A then TRx1A) = --card 0.
"""
from __future__ import annotations
import argparse, json, os, sys, threading, time, traceback
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
# staging: sdr_core / skiq_bridge resolve from the folder above bench/ (the staging
# copy); modules the staging folder does not carry (tools_engine, rf_frontend) fall
# back to the live service tree, appended last so it never shadows the staged files.
sys.path.append(os.environ.get("DPMG_LIVE_TREE", "/app_svc"))
import numpy as np                                   # noqa: E402
from dpmg.radio import sdr_core as sdr                               # noqa: E402
from dpmg.radio import skiq_bridge as _skb                           # noqa: E402

RX_ONLY_CALLS = ("set_rx_lo", "set_rx_gain_mode", "set_rx_gain_index", "apply_rx_cal",
                 "start_rx")
VARIANT_HOLD = {"baseline": (), "rx_cycle": (), "tx_first": ("start_rx",),
                "rx_after_tx": RX_ONLY_CALLS}
# on-card coupling level of the ampl-0.5 tone over the PSD median, pin off (F4 evidence)
COUPLING_DB = {0: 31.0, 1: 43.0}


def psd_db(iq, fs, nfft=4096):
    n = (len(iq) // nfft) * nfft
    if n < nfft:
        return None, None
    x = iq[:n].reshape(-1, nfft) * np.hanning(nfft).astype(np.float32)
    p = np.mean(np.abs(np.fft.fftshift(np.fft.fft(x, axis=1), axes=1)) ** 2, axis=0)
    return np.fft.fftshift(np.fft.fftfreq(nfft, 1.0 / fs)), 10.0 * np.log10(p + 1e-20)


def tone_report(iq, fs, offset_hz, win_hz=5e3):
    f, db = psd_db(iq, fs)
    if f is None:
        return {"samples": int(len(iq)), "tone_snr_db": None}
    floor = float(np.median(db))
    sel = np.abs(f - offset_hz) <= win_hz
    img = np.abs(f + offset_hz) <= win_hz
    dc = np.abs(f) <= 15e3
    return {"samples": int(len(iq)), "floor_db": round(floor, 1),
            "tone_snr_db": round(float(np.max(db[sel])) - floor, 1),
            "image_snr_db": round(float(np.max(db[img])) - floor, 1),
            "dc_snr_db": round(float(np.max(db[dc])) - floor, 1)}


def collect(b, seconds):
    chunks, t0 = [], time.time()
    while time.time() - t0 < seconds:
        fr = b.recv()
        if fr is not None and len(fr.samples):
            chunks.append(np.asarray(fr.samples, dtype=np.complex64))
    return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.complex64)


def holding_bridge_factory(real_cls, names, state):
    """Real SkiqBridge whose `names` calls are queued (not executed) while
    state['hold'] is True; state['queue'] keeps them in call order for replay."""
    def factory():
        br = real_cls()
        for name in names:
            real = getattr(br, name)

            def wrap(*a, _name=name, _real=real, **k):
                if state["hold"]:
                    state["queue"].append((_name, _real, a, k))
                    return None
                return _real(*a, **k)
            setattr(br, name, wrap)
        state["bridge"] = br
        return br
    return factory


def replay(state):
    state["hold"] = False
    done = []
    for name, real, a, k in state["queue"]:
        rec = [name] + [int(x) if isinstance(x, (int, np.integer)) else str(x) for x in a]
        try:
            real(*a, **k)
        except Exception as e:
            # open() tolerates these (e.g. RX gain-mode refusals); the replay must too, or start_rx never runs
            rec.append(f"err: {e}")
        done.append(rec)
    state["queue"] = []
    return done


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--arm", action="store_true", help="radiate the tone (into the fitted pad only)")
    ap.add_argument("--card", type=int, default=1, help="the NV100 card (RX TRx1x + TX TRx2x on it)")
    ap.add_argument("--variant", default="tx_first", choices=sorted(VARIANT_HOLD))
    ap.add_argument("--hold", default=None,
                    help="comma list overriding which RX-only calls are held until TX streams "
                         f"(bisect rx_after_tx); choices {','.join(RX_ONLY_CALLS)}")
    ap.add_argument("--rate", type=float, default=1.92e6)
    ap.add_argument("--center", type=float, default=425.025e6)
    ap.add_argument("--offset", type=float, default=310e3, help="slot centre, not the +300 kHz edge")
    ap.add_argument("--atten", type=float, default=30.0)
    ap.add_argument("--gain", type=int, default=255)
    ap.add_argument("--settle-s", type=float, default=0.3, help="delay from TX streaming to RX start")
    ap.add_argument("--early-s", type=float, default=0.5)
    ap.add_argument("--tx-s", type=float, default=3.0)
    ap.add_argument("--coupling-db", type=float, default=None,
                    help="on-card coupling level for the verdict (default: F4 evidence for the card)")
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)

    hold = tuple(VARIANT_HOLD[a.variant])
    if a.hold is not None:
        hold = tuple(h for h in a.hold.split(",") if h)
        bad = [h for h in hold if h not in RX_ONLY_CALLS]
        if bad:
            ap.error(f"--hold: not RX-only calls: {bad}")
    coupling = a.coupling_db if a.coupling_db is not None else COUPLING_DB.get(a.card, 31.0)
    res = {"probe": "samecard_j2", "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "variant": a.variant, "held": list(hold), "card": a.card, "rate_hz": int(a.rate),
           "center_hz": int(a.center), "offset_hz": int(a.offset), "tx_atten_db": a.atten,
           "rx_gain": a.gain, "armed": bool(a.arm), "coupling_db": coupling,
           "j2_tx_pin": os.environ.get("DPMG_J2_TX_PIN", "0"),
           "modules": {"sdr_core": sdr.__file__, "skiq_bridge": _skb.__file__},
           "open": None, "replayed": [],
           "early": None, "late": None, "tx_off": None, "errors": [], "verdict": None}

    state = {"hold": bool(hold), "queue": [], "bridge": None}
    real_cls = sdr.SkiqBridge
    if hold:
        sdr.SkiqBridge = holding_bridge_factory(real_cls, hold, state)
    ex = {"card": a.card, "tx_card": a.card, "chan_mode": "dual",
          "rf_bandwidth_hz": int(a.rate * 0.8), "tx_center_freq": int(a.center)}
    cfg = sdr.SDRConfig(sample_rate=a.rate, center_freq=a.center, rx_gain=a.gain, tx_gain=a.atten,
                        rx_antenna="RXA1", tx_antenna="TXA2", extras=ex).validate()
    b = sdr.make_backend("g40", cfg, simulate=False)
    b.frontend = None
    t0 = time.time()
    try:
        b.open()
    except Exception as e:
        res["errors"].append(f"open: {e}"); res["verdict"] = "OPEN-FAILED"
        print(json.dumps(res, indent=1)); return 2
    finally:
        sdr.SkiqBridge = real_cls
    br = b._skiq
    res["open"] = {"seconds": round(time.time() - t0, 1), "chan_mode": b._chan_mode,
                   "tx_hdl": b._tx_hdl, "rx_hdl": b._rx_hdl, "same_card_fdd": b._same_card_fdd(),
                   "held_calls": [q[0] for q in state["queue"]],
                   "tx_block_samples": getattr(br, "tx_block_samples", None)}
    try:
        res["open"]["rf_port_diag"] = str(br.rf_port_diag(a.card, b._rx_hdl, b._tx_hdl))
    except Exception as e:
        res["open"]["rf_port_diag"] = f"n/a ({e})"
    print("[probe] open:", json.dumps(res["open"], default=str), flush=True)

    stop = threading.Event()
    sent = {"n": 0, "err": None}
    try:
        if not a.arm:
            res["replayed"] = replay(state)
            res["verdict"] = "INIT-OK (unarmed)"
        else:
            if a.variant == "rx_cycle":
                br.stop_rx(a.card, b._rx_hdl)
                res["replayed"].append(["stop_rx", a.card, int(b._rx_hdl)])
            from dpmg.dsp import tools_engine
            from dpmg.dsp.tools_engine import cw_tone
            res["modules"]["tools_engine"] = tools_engine.__file__
            tone = cw_tone(b._achieved_rate, a.offset, 0.5, 0.5)

            def tx_loop():
                while not stop.is_set():
                    try:
                        b.send(tone); sent["n"] += 1
                    except Exception as e:
                        sent["err"] = str(e); break
            th = threading.Thread(target=tx_loop, daemon=True)
            th.start()
            t_key = time.time()
            while not getattr(b, "_tx_streaming", False) and not sent["err"] and time.time() - t_key < 3.0:
                time.sleep(0.01)
            res["tx_start_s"] = round(time.time() - t_key, 3)
            time.sleep(a.settle_s)
            if a.variant == "rx_cycle":
                br.start_rx(a.card, b._rx_hdl)
                res["replayed"].append(["start_rx", a.card, int(b._rx_hdl)])
            else:
                res["replayed"] += replay(state)
            early = collect(b, a.early_s)
            late = collect(b, a.tx_s)
            stop.set(); th.join(timeout=3.0)
            b.stop_tx()
            time.sleep(0.3)
            off = collect(b, 1.0)
            fs = b._achieved_rate
            res["early"] = tone_report(early, fs, a.offset)
            res["late"] = tone_report(late, fs, a.offset)
            res["tx_off"] = tone_report(off, fs, a.offset)
            res["keyed"] = {"sends": sent["n"], "send_err": sent["err"],
                            "tx_frames": b.health.tx_frames, "last_error": b.health.last_error}
            best = max((r.get("tone_snr_db") or -99) for r in (res["early"], res["late"]))
            if sent["err"]:
                res["verdict"] = "TX-REFUSED"
            elif best >= coupling + 15.0:
                res["verdict"] = "ABOVE-COUPLING"
            elif best >= coupling - 6.0:
                res["verdict"] = "COUPLING-LEVEL"
            else:
                res["verdict"] = "BELOW-COUPLING"
            print("[probe] early:", res["early"], "late:", res["late"], "off:", res["tx_off"], flush=True)
    except Exception as e:
        res["errors"].append(f"run: {e}"); traceback.print_exc()
        res["verdict"] = res["verdict"] or "ERROR"
    finally:
        stop.set()
        state["hold"] = False
        for step in ("stop_tx", "close"):
            try:
                getattr(b, step)()
            except Exception as e:
                res["errors"].append(f"{step}: {e}")
        print(f"[probe] teardown done (state {b.health.state.value}); radio free", flush=True)
    print(json.dumps(res, indent=1, default=str))
    if a.out:
        with open(a.out, "w") as fh:
            json.dump(res, fh, indent=1, default=str)
    return 0 if res["verdict"] in ("ABOVE-COUPLING", "COUPLING-LEVEL", "BELOW-COUPLING",
                                   "INIT-OK (unarmed)") else 1


if __name__ == "__main__":
    sys.exit(main())
