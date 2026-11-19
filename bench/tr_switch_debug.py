#!/usr/bin/env python3
"""tr_switch_debug.py: find out why the payload T/R switch does or does not move.

Only the T/R switch line is ever commanded (frontend_set tr=tx / tr=rx).  No PA
command, no TX, no radio init.  The PA line is read back before every change and
the script stops if it ever reads keyed.  The switch is always left in RX, and
the FieldFox (rf mode) is always put back in SA mode, also on Ctrl-C.

    python3 bench/tr_switch_debug.py help
        the full how-to guide (also shown when run with no arguments)
    python3 bench/tr_switch_debug.py status
        switch line as the service sees it and as the kernel holds it, PA line, guard
    python3 bench/tr_switch_debug.py hold tx
        set the line to TX and wait for Enter while you meter, then RX and wait again
    python3 bench/tr_switch_debug.py toggle --n 10 --period 3
        flip TX / RX every period seconds, n times (supply current, scope, LED)
    python3 bench/tr_switch_debug.py rf --rf-port j1 --analyzer 192.168.10.3
        FieldFox NA S21 from J0 (Port 1) to the switch port on Port 2, in TX then RX

What the switch should do (dBWave PASR0200500600A): TTL high = J0-J1, TTL low =
J0-J2.  In TX the G40 drives socket pin 15 (PN.01) high, 3.3 V.  The switch needs
2.8 V or more on its TTL pin, +5 V bias and a ground common with the G40.  Its RF
ports must see no DC path to ground (DC blocks; leave an unused port open, not
terminated, for the rf test).

Radio = --host (default DPMG_G40_HOST or 192.168.0.23), port 47810.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import checkout_common as cc  # noqa: E402

GUIDE = """
T/R switch debug: how to use it
===============================

Runs on the laptop, from the repo folder.  It talks to the G40 radio service over
the network and, for the rf test, to the FieldFox over the LAN.  Nothing is copied
to the G40.

It only ever moves the switch control line (tr=tx / tr=rx).  It never keys the PA,
never transmits and never initialises the radio.  It stops if the PA line reads
keyed or the radio is transmitting, and it always leaves the switch in RX (also on
Ctrl-C).  The rf test always puts the FieldFox back in SA mode.


1. Before you start
-------------------
  Laptop on the bench Ethernet:      ip -br addr show eth0
                                     (expect 192.168.0.27, and 192.168.10.27 for the FieldFox)
  G40 answering:                     ping -c 2 192.168.0.23
  Repo folder:                       cd <repo root>
  Payload powered, switch +5 V on.  The 1189 HPA output on its coupler and dummy load,
  not on the switch while testing.  The 1189 DB9 left connected (it holds the HPA off).


2. Status (read-only, start here)
---------------------------------
  python3 bench/tr_switch_debug.py status

  One line: the switch line as the service sees it (tx=True/False) and as the kernel
  holds it (high/low), the PA line (keyed / asserted / off), whether TX is running,
  and the payload guard.  Expect: tx=False, line low, PA keyed=False line=off,
  tx_running=False.  If it says "no HELLO_ACK", the laptop cannot reach the G40
  (check step 1).


3. Hold (meter the control voltages)
------------------------------------
  python3 bench/tr_switch_debug.py hold tx

  Sets the line to TX, prints what to measure, and waits for Enter.  Then sets RX,
  prints again, and waits for Enter.  Black probe on the outer shell of any switch
  SMA connector (the switch ground).

                              in TX              in RX
    G40 socket pin 15         about 3.3 V        about 0 V
    switch TTL ("PIN") pin    2.8 V or more      under 0.8 V
    switch +5V pin            5 V                5 V (must not change)

  Reading the result:
    TTL pin 2.8 V or more in TX   control is fine; look at the switch, its pins, its bias
    TTL pin 0.8 to 2.8 V in TX    signal too weak or dragged down by something on the line
    TTL pin 0 V in both           the G40 wire is not reaching that pin
    +5V pin changes with TX/RX    TTL and +5V wires are swapped at the switch

  Start in RX instead:  python3 bench/tr_switch_debug.py hold rx


4. Toggle (watch without typing)
--------------------------------
  python3 bench/tr_switch_debug.py toggle --n 10 --period 3

  Flips TX / RX every 3 s, 10 changes, printing the state each time.  Use it to watch
  the 5 V supply current (the switch draws up to 100 mA; a working driver changes the
  current between TX and RX), a scope, or a meter.  --n and --period change the count
  and the time in each state.


5. RF (proves the switch actually moves)
----------------------------------------
  Cabling (switch out of the payload RF path):
    FieldFox Port 1 (RF Out) to a DC block to switch J0 (common, the antenna port)
    FieldFox Port 2 (RF In) to switch J1 (TX side, the HPA port)
    switch J2 (RX side): leave it open, no terminator (a 50 ohm load is a DC path)

  python3 bench/tr_switch_debug.py rf --rf-port j1

  Puts the FieldFox in NA mode (S21, 700 to 2500 MHz, about 0 dBm), sets TX and
  sweeps, sets RX and sweeps (the sweep is held while the switch changes), prints
  S21 at 700, 915, 1500, 2200 and 2500 MHz in each state, and a verdict.

  Then move Port 2 to J2 and leave J1 open:
  python3 bench/tr_switch_debug.py rf --rf-port j2

  Expected: the connected state reads about -1 to -6 dB (switch loss plus the
  FieldFox cables and the DC block), the isolated state -30 dB or lower.

    J1 port, switch TX          connected
    J1 port, switch RX          isolated
    J2 port, switch TX          isolated
    J2 port, switch RX          connected

  Verdict meanings:
    "switches correctly"        the switch works; any payload problem is elsewhere
    "connected in BOTH states"  stuck in that position: the switch does not see the
                                TTL change, has no bias, or its driver is faulty
    "isolated in BOTH states"   never reaches that port: stuck in the other position,
                                no bias, or that RF arm is dead
    "changes ... not as expected"  check which switch port is on Port 2

  Save FieldFox screenshots:   --shots <folder>
  Other FieldFox address:      --analyzer <host>


6. Options
----------
  --host <ip>        G40 address (default DPMG_G40_HOST, else 192.168.0.23)
  --port <n>         radio command port (default 47810)
  --n, --period      toggle count and seconds per state
  --analyzer <host>  FieldFox (default 192.168.10.3)
  --rf-port j1|j2    switch port on FieldFox Port 2 for the rf test
  --shots <folder>   rf test screenshots

  Ctrl-C stops any mode; the switch goes back to RX on the way out.


7. What the switch needs (dBWave PASR0200500600A)
-------------------------------------------------
  +5 V bias (up to 100 mA), a ground common with the G40, TTL on its PIN input:
  high (2.8 to 5 V) = J0-J1, low (0 to 0.8 V) = J0-J2.  In TX the G40 drives socket
  pin 15 (PN.01) high at 3.3 V.  DC blocks on all three RF ports (high-power ones on
  J0 and J1 in the payload).  Cold switching only: never change the line with RF
  power through it (this script refuses while the radio is transmitting).
"""

FREQS = (700e6, 915e6, 1500e6, 2200e6, 2500e6)
CONNECTED_DB = -6.0        # better than this: path connected (cables and blocks included)
ISOLATED_DB = -30.0        # worse than this: path isolated


class Radio:
    def __init__(self, host, port):
        self.link = cc.Link(host, port)

    def cmd(self, name, params=None, timeout=15.0):
        a = cc.safe_cmd(self.link, name, params or {}, timeout=timeout)
        return getattr(a, "status", None), (getattr(a, "result", None) or {})

    def state(self) -> dict:
        _, r = self.cmd("payload_status")
        pa, tr, g = r.get("pa") or {}, r.get("tr") or {}, r.get("guard") or {}
        return {"pa_keyed": pa.get("keyed"), "pa_level": pa.get("level"),
                "tr_tx": tr.get("tx"), "tr_level": tr.get("level"), "tr_mode": tr.get("mode"),
                "tx_level": tr.get("tx_level"), "tx_running": r.get("tx_running"),
                "guard_armed": g.get("armed"), "guard_profile": g.get("profile")}

    def set_tr(self, want: str) -> dict:
        before = self.state()
        if before["pa_keyed"] or before["pa_level"]:
            raise SystemExit(f"PA line reads keyed before tr={want}: stopping, nothing changed")
        if before["tx_running"]:
            raise SystemExit("radio is transmitting: stopping, nothing changed (cold switching only)")
        st, r = self.cmd("frontend_set", {"tr": want})
        if st not in (0, None) or r.get("err"):
            raise SystemExit(f"frontend_set tr={want} refused: {r.get('err')}")
        time.sleep(0.2)
        after = self.state()
        if after["pa_keyed"] or after["pa_level"]:
            raise SystemExit(f"PA line reads keyed after tr={want}: stopping")
        return after


def show(s: dict, prefix: str = "") -> None:
    print(f"{prefix}switch: service tx={s['tr_tx']}  kernel line={'high' if s['tr_level'] else 'low'}"
          f"  (mode {s['tr_mode']}, tx_level {s['tx_level']})   PA: keyed={s['pa_keyed']}"
          f" line={'asserted' if s['pa_level'] else 'off'}   tx_running={s['tx_running']}"
          f"   guard {s['guard_profile']} armed={s['guard_armed']}")


def back_to_rx(radio: Radio) -> None:
    try:
        s = radio.set_tr("rx")
        show(s, "left in RX  ")
    except SystemExit as e:
        print(f"could not return the switch to RX: {e}")


METER = {
    "tx": ("G40 socket pin 15: about 3.3 V.  Switch TTL pin (black on a switch SMA shell): "
           "2.8 V or more.  Switch +5V pin: 5 V.  Switch should connect J0-J1."),
    "rx": ("G40 socket pin 15: about 0 V.  Switch TTL pin: under 0.8 V.  Switch +5V pin: 5 V "
           "(unchanged).  Switch should connect J0-J2."),
}


def do_hold(radio: Radio, which: str) -> None:
    order = ["tx", "rx"] if which == "tx" else ["rx"]
    for want in order:
        s = radio.set_tr(want)
        show(s, f"set {want.upper():2s}  ")
        print(f"  meter now: {METER[want]}")
        input(f"  press Enter when done with {want.upper()} ")


def do_toggle(radio: Radio, n: int, period: float) -> None:
    for i in range(n):
        want = "tx" if i % 2 == 0 else "rx"
        s = radio.set_tr(want)
        show(s, f"{time.strftime('%H:%M:%S')} {want.upper():2s}  ")
        time.sleep(period)


def s21(fx, n_avg: int = 2) -> dict:
    for _ in range(n_avg):
        fx.write("INIT:IMM", check=False)
        fx.query("*OPC?")
    data = [float(x) for x in fx.query("CALC:DATA:FDATA?").split(",")]
    n = len(data)
    start, stop = 700e6, 2500e6
    fs = [start + i * (stop - start) / (n - 1) for i in range(n)]
    out = {}
    for hz in FREQS:
        i = min(range(n), key=lambda k: abs(fs[k] - hz))
        out[hz] = data[i]
    return out


def verdict(tx: dict, rx: dict, port: str) -> str:
    def conn(d):
        return all(v > CONNECTED_DB for v in d.values())

    def iso(d):
        return all(v < ISOLATED_DB for v in d.values())
    want_tx_conn = port == "j1"
    if want_tx_conn:
        if conn(tx) and iso(rx):
            return "J1 path switches correctly (connected in TX, isolated in RX)"
    else:
        if conn(rx) and iso(tx):
            return "J2 path switches correctly (connected in RX, isolated in TX)"
    if conn(tx) and conn(rx):
        return (f"{port.upper()} connected in BOTH states: switch stuck in the J0-{port.upper()} position "
                "(TTL not changing at the switch, bias missing, or driver fault)")
    if iso(tx) and iso(rx):
        return (f"{port.upper()} isolated in BOTH states: switch never reaches J0-{port.upper()} "
                "(stuck in the other position, no bias, or this RF arm is dead)")
    return f"{port.upper()} changes with the line but not as expected: check which port is cabled"


def do_rf(radio: Radio, analyzer: str, port: str, shots: str | None) -> None:
    import fieldfox as ff
    host, _, p = analyzer.partition(":")
    fx = ff.FieldFox(host, int(p or 5025), timeout_s=15)
    print(f"analyzer: {fx.idn}")
    print(f"cabling: FieldFox Port 1 to a DC block to switch J0; Port 2 to switch {port.upper()}; "
          f"the other switch port left open (no terminator)")
    try:
        fx.write('INST:SEL "NA"', check=True)
        fx.query("*OPC?")
        for c in ("CALC:PAR1:DEF S21", "CALC:PAR1:SEL", "CALC:FORM MLOG", "SENS:FREQ:STAR 700e6",
                  "SENS:FREQ:STOP 2500e6", "SENS:SWE:POIN 401", "INIT:CONT 0"):
            fx.write(c, check=True)
        res = {}
        for want in ("tx", "rx"):
            s = radio.set_tr(want)          # sweep is held while the line changes
            res[want] = s21(fx)
            row = "  ".join(f"{hz / 1e6:.0f} MHz {v:6.1f} dB" for hz, v in res[want].items())
            print(f"switch {want.upper()} (kernel line {'high' if s['tr_level'] else 'low'}):  {row}")
            if shots:
                os.makedirs(shots, exist_ok=True)
                fx.write("CALC:MARK1:ACT", check=True)
                fx.write("CALC:MARK1:X 915e6", check=True)
                path = os.path.join(shots, f"tr_switch_J0_{port}_{want}_{time.strftime('%H%M%S')}.png")
                fx.screenshot(path)
                print(f"  screenshot {path}")
        print("VERDICT:", verdict(res["tx"], res["rx"], port))
    finally:
        try:
            fx.write('INST:SEL "SA"', check=False)
            fx.query("*OPC?")
            fx.write("INIT:CONT 1", check=False)
        except Exception as e:
            print(f"FieldFox not restored to SA: {e}")
        fx.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    if argv is None:
        argv = sys.argv[1:]
    if not argv or argv[0] in ("help", "guide"):
        print(GUIDE)
        return 0
    ap.add_argument("mode", choices=("status", "hold", "toggle", "rf", "help"),
                    help="help prints the full how-to guide")
    ap.add_argument("which", nargs="?", default="tx", choices=("tx", "rx"), help="hold: start state")
    ap.add_argument("--host", default=os.environ.get("DPMG_G40_HOST", "192.168.0.23"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("DPMG_CMD_PORT", 47810)))
    ap.add_argument("--n", type=int, default=10, help="toggle: number of changes")
    ap.add_argument("--period", type=float, default=3.0, help="toggle: seconds in each state")
    ap.add_argument("--analyzer", default="192.168.10.3", help="rf: FieldFox host[:port]")
    ap.add_argument("--rf-port", dest="rf_port", default="j1", choices=("j1", "j2"),
                    help="rf: switch port cabled to FieldFox Port 2")
    ap.add_argument("--shots", default=None, help="rf: folder for FieldFox screenshots")
    a = ap.parse_args(argv)

    radio = Radio(a.host, a.port)
    show(radio.state(), "now        ")
    if a.mode == "status":
        return 0
    try:
        if a.mode == "hold":
            do_hold(radio, a.which)
        elif a.mode == "toggle":
            do_toggle(radio, a.n, a.period)
        else:
            do_rf(radio, a.analyzer, a.rf_port, a.shots)
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        back_to_rx(radio)
    return 0


if __name__ == "__main__":
    sys.exit(main())
