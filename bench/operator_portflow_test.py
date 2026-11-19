#!/usr/bin/env python3
"""
Validate the operator view's RX/TX port flow on a G40.

For each RX-port + TX-port pairing the operator dropdowns allow, build the exact
init the operator view's _start() produces (port: card/handle, same-card=dual /
different-card=cross-card) and confirm the radio inits + streams. The two pairings
that match the physical TRx1A<->TRx1B 20 dB loopback also get a TX tone keyed to
confirm the loopback closes through that pairing. Same-card pairings are init-only
(their TX port is not cabled, so card1 TRx2B is not keyed).

  python3 bench/operator_portflow_test.py --host 192.168.0.22 --arm
"""
from __future__ import annotations

import argparse
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

from dpmg.link import proto  # noqa: E402
from dpmg.link.proto import MsgType, CmdStatus  # noqa: E402
from fsk_loopback_test import Link  # noqa: E402

CENTER = 425_025_000
RATE = 1_920_000

# mirrors link_tab._PORT_MAP exactly
PORT_MAP = {
    "TRx1A": {"card": 0, "rx": "RXA1", "tx": "TXA1"},
    "TRx2A": {"card": 0, "rx": "RXA2", "tx": "TXA2"},
    "TRx1B": {"card": 1, "rx": "RXA1", "tx": "TXA1"},
    "TRx2B": {"card": 1, "rx": "RXA2", "tx": "TXA2"},
}

# (rx_port, tx_port, cabled?): cabled pairs match TRx1A<->TRx1B and get a TX check
PAIRS = [
    ("TRx1A", "TRx1B", True),    # cross-card, the proven default
    ("TRx1B", "TRx1A", True),    # cross-card reversed (RX card1 / TX card0)
    ("TRx1A", "TRx2A", False),   # same-card dual, card0
    ("TRx2A", "TRx1B", False),   # cross-card, RX on TRx2
    ("TRx1B", "TRx2B", False),   # same-card dual, card1
]


def init_p(rx_port, tx_port, gain, atten):
    rxp, txp = PORT_MAP[rx_port], PORT_MAP[tx_port]
    chan_mode = "dual" if rxp["card"] == txp["card"] else "single"
    extras = {"card": rxp["card"], "tx_card": txp["card"], "chan_mode": chan_mode,
              "rf_bandwidth_hz": int(RATE * 0.8), "tx_center_freq": CENTER}
    p = {"kind": "g40", "sample_rate": RATE, "center_freq": CENTER,
         "rx_gain": gain, "tx_gain": atten, "clock_source": "internal",
         "rx_antenna": rxp["rx"], "tx_antenna": txp["tx"], "extras": extras,
         "psd_nfft": 4096, "psd_window": "hann", "psd_buffer": 65536}
    return p, chan_mode


def status_settled(link, settle=2.5, window=3.0):
    """Let the scheduler reach steady state, then return the last STATUS seen in
    a window. The first STATUS after start_rx covers a partial ramp-up second
    (fps reads low, esp. in dual mode), so settle past it and report a full
    steady-state second."""
    time.sleep(settle)
    # drain the backlog so the read starts on fresh frames
    link.s.setblocking(False)
    try:
        while True:
            link.s.recvfrom(65535)
    except Exception:
        pass
    link.s.setblocking(True)
    link.s.settimeout(1.0)
    last = None
    t0 = time.time()
    while time.time() - t0 < window:
        try:
            data, _ = link.s.recvfrom(65535)
            mt, _s, m = proto.decode(data)
            if mt == MsgType.STATUS:
                last = m                       # keep the latest, not the first
        except Exception:
            continue
    return last


def tone_detects(link, secs=3):
    """Key a CW tone and count energy detections (loopback closed?)."""
    link.cmd("gen_test_waveform",
             {"kind": "tone", "freq": 300_000, "seconds": 0.5, "ampl": 0.5,
              "name": "jam_tone", "load_tx": True}, timeout=60)
    t0 = time.time()
    while time.time() - t0 < 0.3:
        try:
            link.s.recvfrom(65535)
        except Exception:
            break
    link.cmd("start_tx", {"mode": "CONTINUOUS"})
    dets, t0 = 0, time.time()
    while time.time() - t0 < secs:
        try:
            data, _ = link.s.recvfrom(65535)
            mt, _s, _m = proto.decode(data)
            if mt == MsgType.DETECTION:
                dets += 1
        except Exception:
            continue
    link.cmd("stop_tx", {})
    return dets


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("DPMG_G40_HOST"), required=not os.environ.get("DPMG_G40_HOST"))
    ap.add_argument("--port", type=int, default=47810)
    ap.add_argument("--arm", action="store_true", help="key TX on the cabled pairs")
    args = ap.parse_args()

    link = Link(args.host, args.port)
    print(f"connected to {link.serial}\n", flush=True)
    results = []
    for rx_port, tx_port, cabled in PAIRS:
        gain, atten = (255 if cabled else 200), 30    # 30 dB TX attenuation floor on every path
        p, chan_mode = init_p(rx_port, tx_port, gain, atten)
        tag = f"RX {rx_port} + TX {tx_port} ({chan_mode})"
        print(f"=== {tag} ===", flush=True)
        a = link.cmd("init", p, timeout=120)
        if a.status != CmdStatus.OK:
            print(f"  INIT FAIL: {a.result}\n", flush=True)
            results.append((tag, "INIT-FAIL", "")); continue
        r = a.result or {}
        print(f"  init OK  achieved={r.get('achieved_rate')} chan_mode={r.get('chan_mode')}",
              flush=True)
        link.cmd("start_rx", {})
        st = status_settled(link)
        fps = getattr(st, "fps", -1) if st else -1
        xr = (st.overruns + st.underruns) if st else -1
        extra = ""
        if cabled and args.arm:
            n = tone_detects(link)
            extra = f" | TX tone: {n} detections ({'loopback OK' if n > 0 else 'no det'})"
        print(f"  streaming fps={fps} over+under={xr}{extra}\n", flush=True)
        results.append((tag, "OK", f"fps={fps} xr={xr}{extra}"))
        link.cmd("stop_rx", {})
        link.cmd("close", {})
        time.sleep(1.0)

    print("#" * 60 + "\n SUMMARY\n" + "#" * 60, flush=True)
    for tag, verdict, note in results:
        print(f"  {verdict:<10} {tag:<34} {note}", flush=True)


if __name__ == "__main__":
    main()
