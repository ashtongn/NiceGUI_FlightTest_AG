"""hpa_toggle_check.py: see the HPA follow the G40's enable line on the analyzer.

Bench: G40 TX then combiner then HPA then coupler then dummy load, analyzer on the
coupled port.  The G40 runs at a high TX attenuation (41.75 dB by default) so
the amplifier output stays in the milliwatts.

Sequence, with the HPA DC already on:
  1. init the G40 at the high attenuation, load a weak tone, no TX yet
  2. HPA line alone: payload_key on for hold seconds (floor rises on the
     analyzer, no tone), then off (floor drops)
  3. tone cycles: start_tx CONTINUOUS for hold seconds (the TX run keys the HPA
     line and the tone shows at the coupled port), stop_tx (tone gone, line
     released), repeated --cycles times
  4. close the radio, front end SAFE

Every state change prints what the service reports for the HPA line, so a
wrong polarity shows up as "keyed" with no tone, or a tone with the line off.

  python3 bench/hpa_toggle_check.py --host 192.168.0.23 [--atten 41.75] [--freq 915] [--hold 6] [--cycles 3]
  python3 bench/hpa_toggle_check.py --host 192.168.0.23 --analyzer 192.168.10.3
        (FieldFox on LAN: peak reading and a screenshot in every state, into the
         test_results folder of the day)
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from bench.hpa_power_sweep import PORTS, Radio, G40_HOST, CMD_PORT, open_analyzer, DEFAULT_OUT_DIR    # noqa: E402
from dpmg.payload import payload_budget as pb                          # noqa: E402


def line(r: Radio) -> str:
    fe = r.frontend()
    pa, tr = fe.get("pa") or {}, fe.get("tr") or {}
    return f"HPA line keyed={pa.get('keyed')} level={pa.get('level')} switch tx={tr.get('tx')}"


def hold(r: Radio, secs: float, label: str) -> bool:
    t0 = time.monotonic()
    keyed_seen = False
    while time.monotonic() - t0 < secs:
        fe = r.frontend()
        k = bool((fe.get("pa") or {}).get("keyed"))
        keyed_seen = keyed_seen or k
        print(f"  {label} t+{time.monotonic()-t0:4.1f}s  HPA line keyed={k}  watchdog_trips={fe.get('watchdog_trips')}")
        time.sleep(1.0)
    return keyed_seen


class Shots:
    """Analyzer reading plus screenshot per state, when --analyzer is given."""

    def __init__(self, fx, a, shots_dir):
        self.fx, self.a, self.dir, self.n = fx, a, shots_dir, 0
        self.readings = []
        if fx is not None:
            fx.setup(a.freq * 1e6 + 100e3, a.span, a.rbw, a.ref, a.an_atten)

    def take(self, state: str):
        if self.fx is None:
            return None
        self.n += 1
        y, x = self.fx.peak(sweeps=2)
        shot = ""
        if self.dir:
            os.makedirs(self.dir, exist_ok=True)
            shot = os.path.join(self.dir, f"toggle_{self.n:02d}_{state}.png")
            self.fx.screenshot(shot)
        print(f"  analyzer [{state}]: peak {y:+.2f} dBm at {x/1e6:.6f} MHz" + (f"  -> {shot}" if shot else ""))
        self.readings.append((state, y, x, shot))
        return y

    def close(self):
        if self.fx is not None:
            try:
                self.fx.resume()
                self.fx.close()
            except Exception:
                pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=os.environ.get("DPMG_G40_HOST", G40_HOST))
    ap.add_argument("--port", type=int, default=int(os.environ.get("DPMG_CMD_PORT", CMD_PORT)))
    ap.add_argument("--profile", default="P2b")
    ap.add_argument("--tx-port", default="TRx1A", choices=sorted(PORTS))
    ap.add_argument("--rx-port", default="TRx2B", choices=sorted(PORTS))
    ap.add_argument("--freq", type=float, default=915.0, help="MHz")
    ap.add_argument("--atten", type=float, default=41.75, help="G40 TX attenuation, dB")
    ap.add_argument("--ampl", type=float, default=0.5)
    ap.add_argument("--rate", type=float, default=1.92e6)
    ap.add_argument("--clock", default="internal", choices=("internal", "gpsdo", "external"))
    ap.add_argument("--hold", type=float, default=6.0, help="seconds in each state")
    ap.add_argument("--cycles", type=int, default=3, help="tone on/off cycles")
    ap.add_argument("--skip-line-only", action="store_true", help="no HPA-line-only key (no guard, or TX atten unknown)")
    ap.add_argument("--analyzer", default=None, help="FieldFox host[:port] on LAN")
    ap.add_argument("--span", type=float, default=200e3)
    ap.add_argument("--rbw", type=float, default=10e3)
    ap.add_argument("--ref", type=float, default=20.0)
    ap.add_argument("--an-atten", type=float, default=20.0)
    ap.add_argument("--no-screens", action="store_true")
    ap.add_argument("--g40-max-dbm", type=float, default=None, help="model override from calibration")
    a = ap.parse_args(argv)

    data = pb.apply_g40_max(pb.load_profiles(os.path.join(os.path.dirname(HERE), "payload_profiles.yaml")), a.g40_max_dbm)
    prof = pb.profile(data, a.profile)
    x = pb.coupled_port_expected(data, prof, a.freq, a.atten, -20.0 * math.log10(a.ampl))
    print(f"{a.freq:g} MHz, TX atten {a.atten:g} dB, amplitude {a.ampl:g}: HPA in {x['hpa_in_dbm']:.1f} dBm, "
          f"HPA out {x['hpa_out_dbm']:.1f} dBm ({x['hpa_out_w']*1e3:.0f} mW), coupled port about {x['coupled_dbm']:+.1f} dBm")

    r = Radio(a.host, a.port)
    r.hello()
    r._pump(time.monotonic() + 1.0)
    rs = getattr(r.status, "radio_state", None) if r.status else None
    pl = r.payload()
    print(f"radio state {rs}; guard configured={pl.get('configured')}; {line(r)}")
    if rs not in (0, None):
        print("radio is not idle; close it from the GUI first")
        return 2
    rxc, rxh, _ = PORTS[a.rx_port]
    txc, _, txh = PORTS[a.tx_port]
    f_hz = a.freq * 1e6
    p = {"kind": "g40", "sample_rate": a.rate, "center_freq": f_hz, "rx_gain": 230, "tx_gain": a.atten,
         "clock_source": a.clock, "rx_antenna": rxh, "tx_antenna": txh,
         "extras": {"card": rxc, "tx_card": txc, "chan_mode": "dual" if rxc == txc else "single",
                    "rf_bandwidth_hz": int(a.rate * 0.8), "tx_center_freq": f_hz},
         "psd_nfft": 4096, "psd_window": "hann", "psd_buffer": 65536}
    ok = True
    shots = Shots(open_analyzer(a), a,
                  None if a.no_screens else os.path.join(os.path.dirname(DEFAULT_OUT_DIR), "screenshots"))
    shots.take("idle_before")
    try:
        print(f"init: TX {a.tx_port}, RX {a.rx_port}, {a.rate/1e6:g} MSps, {a.freq:g} MHz, atten {a.atten:g} dB")
        r.ok("init", p, timeout=120)
        r.ok("set_tx_atten", {"qdb": int(round(a.atten * 4))})
        r.ok("gen_test_waveform", {"kind": "tone", "freq": 100e3, "seconds": 1.0, "ampl": a.ampl,
                                   "name": "hpa_toggle_tone", "load_tx": True}, timeout=20)
        print("tone loaded (not transmitting)")

        if pl.get("configured") and not a.skip_line_only:
            print(f"\n[1] HPA line ON alone, no tone, {a.hold:g} s: expect the analyzer floor to rise")
            st, res = r.cmd("payload_key", {"on": True}, timeout=15)
            if st != 0:
                print(f"  payload_key on refused: {res.get('err')}")
                ok = False
            else:
                shots.take("line_on")
                seen = hold(r, a.hold, "line on ")
                ok = ok and seen
            r.cmd("payload_key", {"on": False}, timeout=15)
            time.sleep(0.5)
            print(f"  HPA line OFF: {line(r)}  (floor should drop)")
            shots.take("line_off")
            time.sleep(max(2.0, a.hold / 2))
        else:
            print("\n[1] HPA-line-only key skipped")

        for i in range(1, a.cycles + 1):
            print(f"\n[tone cycle {i}/{a.cycles}] start_tx: TX run keys the HPA line; expect the tone at about "
                  f"{x['coupled_dbm']:+.1f} dBm")
            st, res = r.cmd("start_tx", {"mode": "CONTINUOUS"}, timeout=15)
            if st != 0:
                print(f"  start_tx refused: {res.get('err')}")
                ok = False
                break
            shots.take(f"tone_on_{i}")
            seen = hold(r, a.hold, "tone on ")
            ok = ok and seen
            r.cmd("stop_tx", {}, timeout=10)
            time.sleep(0.5)
            print(f"  stop_tx: {line(r)}  (tone and floor should drop)")
            shots.take(f"tone_off_{i}")
            time.sleep(a.hold)
    finally:
        try:
            r.cmd("stop_tx", {}, timeout=10)
        except Exception:
            pass
        try:
            r.cmd("frontend_set", {"safe": True})
        except Exception:
            pass
        try:
            r.cmd("close", {}, timeout=30)
        except Exception:
            pass
        time.sleep(0.5)
        print(f"\nclosed: {line(r)}")
        shots.take("idle_after")
        shots.close()
    if shots.readings:
        print("\nanalyzer readings:")
        for st, y, x, shot in shots.readings:
            print(f"  {st:14s} {y:+8.2f} dBm at {x/1e6:.6f} MHz  {os.path.basename(shot) if shot else ''}")
    print("RESULT:", "service saw the line keyed in every ON state" if ok else "a key was refused or never reported keyed")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
