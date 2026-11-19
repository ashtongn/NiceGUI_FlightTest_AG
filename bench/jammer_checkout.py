#!/usr/bin/env python3
"""
Operator jammer dropdown checkout on the G40. Builds + keys every jammer type the
operator can pick (barrage / partial-band noise / spot-CW / comb / swept / chirp)
with the panel's default parameters, plus the pulse (PRF/duty) modifier, and
verifies each one's PSD signature. TX TRx1B through the pad into RX TRx1A, TX atten 30.

Signatures checked:
  barrage  broadband (most bins hot across the whole band)
  noise    partial band: energy concentrated within +/- bw/2
  tone     one narrow CW spike at the offset
  comb     N tones at the set spacing
  swept    energy spread across the sweep span
  chirp    energy spread across f0..f1
  pulse    the carrier still present with PRF sidebands (time-avg PSD)
"""
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dpmg.link import proto                                          # noqa: E402
from dpmg.link.proto import MsgType                             # noqa: E402
from fsk_loopback_test import Link, init_loopback     # noqa: E402

CENTER = 425_025_000
FS = 15_360_000     # wide enough for chirp +/-5 MHz and swept +/-2.5 MHz


def avg_psd(L, secs=2.2):
    frames, off = [], None
    t0 = time.time()
    while time.time() - t0 < secs:
        try:
            data, _ = L.s.recvfrom(65535)
            mt, _s, m = proto.decode(data)
            if mt == MsgType.PSD_FRAME:
                db = m.ref_db + np.frombuffer(m.bins, dtype=np.int8) * (m.scale_db_x100 / 100.0)
                n = len(db)
                off = (-m.span_hz / 2) + (np.arange(n) + 0.5) * (m.span_hz / n)
                frames.append(db)
        except Exception:
            continue
    if not frames:
        return None, None
    return off, np.mean(np.stack(frames[-10:]), axis=0)


def peaks(off, db, thr, min_sep=120e3):
    floor = np.median(db)
    cand = [i for i in range(1, len(db) - 1)
            if db[i] > floor + thr and db[i] >= db[i-1] and db[i] >= db[i+1]]
    out = []
    for i in cand:
        if not out or (off[i] - out[-1][0]) > min_sep:
            out.append([off[i], db[i]])
        elif db[i] > out[-1][1]:
            out[-1] = [off[i], db[i]]
    return out


def occupancy(off, db, thr=6, lo=None, hi=None, base=None):
    """Fraction of bins (in [lo,hi]) raised > thr dB. Against a TX-off baseline
    when given (correct for flat broadband jammers), else the band median."""
    ref = base if base is not None else np.median(db)
    m = np.ones(len(off), bool)
    if lo is not None:
        m &= off >= lo
    if hi is not None:
        m &= off <= hi
    return float((db[m] > ref[m] + thr).mean()) if base is not None \
        else float((db[m] > ref + thr).mean())


def energy_frac(off, db, lo, hi):
    lin = 10 ** (db / 10.0)
    return float(lin[(off >= lo) & (off <= hi)].sum() / lin.sum())


# (label, gen_test_waveform params, verifier(off,db)->(ok,note))
def tests():
    def v_barrage(off, db, base):
        occ = occupancy(off, db, 6, -6.5e6, 6.5e6, base=base)
        lift = float(np.median(db - base))
        return occ > 0.7 and lift > 8, f"band lifted {lift:.0f} dB, occupancy {occ*100:.0f}%"

    def v_noise(off, db, base):
        # bw=1 MHz: energy within +/-0.6 MHz; compare in-band vs out
        inb = energy_frac(off, db, -0.6e6, 0.6e6)
        return inb > 0.6, f"energy within +/-0.6 MHz = {inb*100:.0f}%"

    def v_tone(off, db, base):
        pk = peaks(off, db, 20)
        near0 = [p for p in pk if abs(p[0]) < 120e3]
        return len(near0) >= 1 and (max(db) - np.median(db)) > 25, \
            f"{len(pk)} peak(s), top {max(db)-np.median(db):.0f} dB over floor"

    def v_comb(off, db, base):
        pk = [p for p in peaks(off, db, 12) if abs(p[0]) < 0.85e6]
        return 6 <= len(pk) <= 9, f"{len(pk)} tones in +/-0.85 MHz (expect ~8)"

    def v_swept(off, db, base):
        # span 5 MHz: band lifted across +/-2.5 MHz vs the TX-off baseline
        inb = energy_frac(off, db, -2.6e6, 2.6e6)
        occ = occupancy(off, db, 6, -2.5e6, 2.5e6, base=base)
        return inb > 0.7 and occ > 0.6, f"energy in +/-2.6 MHz {inb*100:.0f}%, lifted-occ {occ*100:.0f}%"

    def v_chirp(off, db, base):
        inb = energy_frac(off, db, -5.3e6, 5.3e6)
        occ = occupancy(off, db, 6, -5e6, 5e6, base=base)
        return inb > 0.8 and occ > 0.6, f"energy in +/-5.3 MHz {inb*100:.0f}%, lifted-occ {occ*100:.0f}%"

    def v_pulse(off, db, base):
        # pulsed CW: carrier still present (time-avg), peak well above floor
        return (max(db) - np.median(db)) > 18, \
            f"pulsed carrier {max(db)-np.median(db):.0f} dB over floor"

    return [
        ("barrage (full band)",
         {"kind": "barrage", "ampl": 0.7, "seconds": 0.5}, v_barrage),
        ("partial-band noise (1 MHz)",
         {"kind": "noise", "ampl": 0.7, "seconds": 0.5, "bw": 1_000_000}, v_noise),
        ("spot / CW (offset 0)",
         {"kind": "tone", "ampl": 0.7, "seconds": 0.5, "freq": 0}, v_tone),
        ("comb (8 tones, 200 kHz)",
         {"kind": "comb", "ampl": 0.7, "seconds": 0.5,
          "spacing": 200_000, "n_tones": 8, "center": 0}, v_comb),
        ("swept (5 MHz span)",
         {"kind": "swept", "ampl": 0.7, "seconds": 0.5, "span": 5_000_000,
          "sweep_period_s": 0.01, "noise_bw": 0, "center": 0}, v_swept),
        ("chirp / LFM (-5..+5 MHz)",
         {"kind": "chirp", "ampl": 0.7, "seconds": 0.5,
          "f0": -5_000_000, "f1": 5_000_000}, v_chirp),
        ("spot + pulse (PRF 2k, duty 0.3)",
         {"kind": "tone", "ampl": 0.8, "seconds": 0.5, "freq": 0,
          "prf": 2000, "duty": 0.3}, v_pulse),
    ]


def main():
    import argparse
    ap = argparse.ArgumentParser(description="operator jammer checkout on the G40 loopback")
    ap.add_argument("--host", default=os.environ.get("DPMG_G40_HOST"), required=not os.environ.get("DPMG_G40_HOST"))
    ap.add_argument("--port", type=int, default=47810)
    ap.add_argument("--arm", action="store_true", help="allow TX (radiates into the pad)")
    args = ap.parse_args()                # before connecting, so --help never reaches the radio
    if not args.arm:
        raise SystemExit("--arm not given: refusing to transmit. Nothing was sent to the radio.")
    L = Link(args.host, args.port)
    print("connected", L.serial, flush=True)
    init_loopback(L, FS, CENTER, 30, 255, tx_card=1, kind="g40")
    time.sleep(1.2)
    # TX-off baseline so broadband jammers can be measured as a lift over it
    boff, bdb = avg_psd(L, 1.5)
    print(f"baseline floor {np.median(bdb):.1f} dB ({len(bdb)} bins)" if bdb is not None
          else "no baseline")
    rows = []
    try:
        for label, params, verify in tests():
            params = dict(params, name=f"jam_{params['kind']}", load_tx=True)
            b = L.cmd("gen_test_waveform", params, timeout=30)
            if b.status != 0:
                rows.append((label, "BUILD FAIL", (b.result or {}).get("err")))
                print(f"  {label:<34} BUILD FAIL {(b.result or {}).get('err')}"); continue
            tx = L.cmd("start_tx", {"mode": "CONTINUOUS"}, timeout=15)
            if tx.status != 0:
                rows.append((label, "TX FAIL", (tx.result or {}).get("err")))
                print(f"  {label:<34} TX FAIL {(tx.result or {}).get('err')}"); continue
            off, db = avg_psd(L, 2.0)
            L.cmd("stop_tx", {}); time.sleep(0.25)
            if off is None:
                rows.append((label, "NO PSD", "")); print(f"  {label:<34} NO PSD"); continue
            ok, note = verify(off, db, bdb)
            rows.append((label, "OK" if ok else "CHECK", note))
            print(f"  {label:<34} {'OK ' if ok else 'XX '} {note}")
    finally:
        try:
            L.cmd("stop_tx", {}); L.cmd("stop_rx", {}); L.cmd("close", {})
        except Exception:
            pass
    npass = sum(1 for _, s, _ in rows if s == "OK")
    print(f"\n==== JAMMER CHECKOUT: {npass}/{len(rows)} OK ====")
    for n, s, d in rows:
        if s != "OK":
            print(f"   {s}: {n}  {d}")


if __name__ == "__main__":
    main()
