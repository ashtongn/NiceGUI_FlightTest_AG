#!/usr/bin/env python3
"""Low drive TX checks on the payload G40: all at 40 dB into the load, RX off. Prints its plan unless --go.

    waveform at 61.44 and 30.72 MS/s, key and stop cycles, guard refusals while keyed, one hold.

Stops at the first check that goes wrong. Closes the radio on every exit.
"""
import argparse
import os
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import payload_bench as bd                      # noqa: E402

ATTEN = 40.0


def init(rate):
    a = argparse.Namespace(clock="internal", rate=rate, freq=915e6, rx=bd.RX_PORT, rx_off=True, go=True)
    if bd.do_init(a) != 0:
        raise RuntimeError(f"init at {rate/1e6:g} MS/s failed")


def key(secs, wave, tag):
    a = argparse.Namespace(atten=ATTEN, secs=secs, tone_ampl=0.5, wave_secs=wave, tag=tag, expect_drop=False, go=True)
    if bd.key(a) != 0:
        raise RuntimeError(f"key {tag} did not run its full time")


def check_after_stop(link, tag):
    s = bd.snap(link)
    if s["tx_active"] or s["keyed"] or s["tr_tx"] or (s["trips"] or 0) != 0:
        raise RuntimeError(f"{tag}: not clean after stop: {bd.line(s)}, trips {s['trips']}")


def refusals(link):
    bd.cmd(link, "atten_set", {"db": 120.0})
    st, r = bd.cmd(link, "set_tx_atten", {"qdb": int(ATTEN * 4)})
    if st == 0:
        st, r = bd.cmd(link, "gen_test_waveform", {"kind": "tone", "freq": 100e3, "seconds": 0.5, "ampl": 0.5,
                                                   "name": "bd_tone", "load_tx": True}, timeout=90)
    if st == 0:
        st, r = bd.cmd(link, "start_tx", {"mode": "CONTINUOUS"}, timeout=20)
    if st != 0:
        raise RuntimeError(f"refusal check could not key: {r.get('err')}")
    try:
        time.sleep(2.0)
        low_tx, rt = bd.cmd(link, "set_tx_atten", {"qdb": 48})
        low_rx, rr = bd.cmd(link, "atten_set", {"db": 30.0})
        s = bd.snap(link)
        bd.log(f"tx-checks refusals: TX 12 dB status {low_tx} ({rt.get('err')}); RX 30 dB status {low_rx} ({rr.get('err')}); "
               f"tx_atten {s['tx_atten']}, rx_atten {s['rx_atten']}")
        if low_tx == 0 or low_rx == 0 or s["rx_atten"] < 120.0 or s["tx_atten"] != ATTEN:
            raise RuntimeError("a low setting was accepted while keyed")
    finally:
        bd.log(f"tx-checks refusals: stop_tx {bd.cmd(link, 'stop_tx', timeout=30)[0]}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cycles", type=int, default=20)
    ap.add_argument("--cycle-secs", type=float, default=10.0)
    ap.add_argument("--hold-secs", type=float, default=600.0)
    ap.add_argument("--go", action="store_true")
    a = ap.parse_args()
    print(f"PLAN: {bd.HOST}, TX {bd.TX_PORT}, {ATTEN:g} dB, tone 0.5, RX off. 20 s at 61.44 and 30.72 MS/s, "
          f"{a.cycles} cycles of {a.cycle_secs:g} s, refusals, hold {a.hold_secs:g} s. Radio closed after.")
    if not a.go:
        print("Nothing sent. Add --go to run it.")
        return 0

    def on_sig(*_):
        raise SystemExit(130)
    signal.signal(signal.SIGTERM, on_sig)
    link, rc = bd.Link(bd.HOST, bd.PORT), 1
    s = bd.snap(link)
    if s["tx_active"] or s["keyed"]:
        bd.log(f"tx-checks: not running, not idle: {bd.line(s)}")
        return 2
    try:
        init(61.44e6)
        key(20.0, 1.0, "wave_61m44")
        check_after_stop(link, "61.44")
        init(30.72e6)
        key(20.0, 1.0, "wave_30m72")
        check_after_stop(link, "30.72")
        for n in range(a.cycles):
            key(a.cycle_secs, 0.5, f"cycle{n + 1:02d}")
            check_after_stop(link, f"cycle {n + 1}")
        refusals(link)
        check_after_stop(link, "refusals")
        key(a.hold_secs, 0.5, "hold_40db")
        check_after_stop(link, "hold")
        rc = 0
        bd.log("tx-checks: all passed")
    except Exception as e:
        bd.log(f"tx-checks: STOPPED, {e}")
    finally:
        bd.log(f"tx-checks: stop_tx {bd.cmd(link, 'stop_tx', timeout=30)[0]}, stop_rx {bd.cmd(link, 'stop_rx', timeout=30)[0]}, "
               f"close {bd.cmd(link, 'close', timeout=60)[0]}")
        bd.log("tx-checks: LEFT " + bd.line(bd.snap(link)))
    return rc


if __name__ == "__main__":
    sys.exit(main())
