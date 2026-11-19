"""
The wire radio_state follows the scheduler's RX drain thread
=============================================================================
INIT-09: the engineering health line must not sit at
"stream: STREAMING / frames 0 / fps 0" with no RX drain running. Mapping
backend.health.state straight onto RadioState does exactly that, because the
flag is card level: EpiqBackend.open() starts the card RX and reports STREAMING before any
Start RX, and FullDuplexScheduler.stop_rx() leaves it STREAMING while a TX
thread is alive. MockBackend.open() reports IDLE, so the simulator shows neither.

Checks (offline, MockBackend + the real FullDuplexScheduler, no network):
  1. fresh init (mock IDLE): INIT_OK
  2. G40-like: card STREAMING, no drain thread: INIT_OK, no TASK_RUNNING
  3. start_rx: STREAMING + TASK_RUNNING
  4. start_tx CONTINUOUS while RX runs: STREAMING + TX_ACTIVE
  5. stop_rx with the TX thread alive: INIT_OK, TX_ACTIVE kept
  6. start_rx again while TX is keyed: STREAMING
  7. stop_tx while RX runs: still STREAMING
  8. stop_rx with no TX: INIT_OK
  9. backend ERROR: FAULT (untouched)
 10. close: UNINIT
 11. mock scheduler: dead RX ref + alive TX thread, card STREAMING: INIT_OK

Exit 0 = all passed.

    python3 tests/test_rx_state_report.py
"""
from __future__ import annotations

import os
import sys
import threading
import time
import types

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dpmg.link import proto                                            # noqa: E402
from dpmg.link.proto import HostType, RadioState, Status          # noqa: E402
from dpmg.radio import sdr_core as sdr                                  # noqa: E402
from dpmg.radio import dpmg_service as S                                # noqa: E402

checks = []


def chk(name, ok, detail=""):
    checks.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")


class SlowTxMock(sdr.MockBackend):
    """MockBackend whose send() takes a few ms so a CONTINUOUS TX thread does
    not hot-loop while the test drives RX."""

    def send(self, samples):
        time.sleep(0.005)
        super().send(samples)


def make_service():
    svc = S.RadioService(serial="G40-SIM", host_type=HostType.G40_RX, kind="mock",
                         simulate=True, cmd_port=0, discovery_port=0,
                         broadcast="127.0.0.1")
    cfg = sdr.SDRConfig(sample_rate=1e6, center_freq=100e6)
    be = SlowTxMock(cfg)
    be.open()
    svc.backend = be
    svc.sched = sdr.FullDuplexScheduler(be, tx_mode=sdr.TXMode.CONTINUOUS)
    return svc


def state(svc):
    return svc._status().radio_state


def flags(svc):
    return svc._status().flags


def tx_alive(svc):
    t = getattr(svc.sched, "_tx", None)
    return t is not None and t.is_alive()


def rx_alive(svc):
    t = getattr(svc.sched, "_rx", None)
    return t is not None and t.is_alive()


def main():
    print("INIT09_RX_STATE: radio_state follows the RX drain thread\n")
    svc = make_service()
    be, sc = svc.backend, svc.sched

    # 1. fresh init on the mock: INIT_OK
    chk("fresh init (mock IDLE) -> INIT_OK", state(svc) == int(RadioState.INIT_OK),
        f"(state={state(svc)})")

    # 2. what EpiqBackend.open() does: card RX started, STREAMING, no drain thread
    be.health.state = sdr.SDRDeviceState.STREAMING
    chk("G40-like card STREAMING + no drain thread -> INIT_OK",
        state(svc) == int(RadioState.INIT_OK), f"(state={state(svc)})")
    chk("  ... and FLAG_TASK_RUNNING clear",
        not (flags(svc) & Status.FLAG_TASK_RUNNING))

    # 3. start_rx: STREAMING + TASK_RUNNING
    sc.start_rx()
    chk("start_rx -> STREAMING", state(svc) == int(RadioState.STREAMING),
        f"(state={state(svc)}, rx_alive={rx_alive(svc)})")
    chk("  ... and FLAG_TASK_RUNNING set", bool(flags(svc) & Status.FLAG_TASK_RUNNING))

    # 4. key TX (CONTINUOUS) while RX runs: STREAMING + TX_ACTIVE
    sc.set_tx_waveform(np.zeros(1024, np.complex64), allow_live=True)
    sc.start_tx()
    chk("start_tx while RX runs: TX thread alive", tx_alive(svc))
    chk("  ... state STREAMING", state(svc) == int(RadioState.STREAMING))
    chk("  ... FLAG_TX_ACTIVE set", bool(flags(svc) & Status.FLAG_TX_ACTIVE))

    # 5. the INIT-09 case: stop_rx with the TX thread alive: RX reads stopped
    sc.stop_rx()
    chk("stop_rx with TX alive: RX drain gone", not rx_alive(svc))
    chk("  ... TX thread still alive", tx_alive(svc))
    chk("  ... backend flag left STREAMING by the scheduler (card level)",
        be.health.state == sdr.SDRDeviceState.STREAMING)
    chk("  ... wire state INIT_OK", state(svc) == int(RadioState.INIT_OK),
        f"(state={state(svc)})")
    chk("  ... FLAG_TASK_RUNNING clear", not (flags(svc) & Status.FLAG_TASK_RUNNING))
    chk("  ... FLAG_TX_ACTIVE still set (TX reported on its own)",
        bool(flags(svc) & Status.FLAG_TX_ACTIVE))

    # 6. Start RX again while TX is keyed: STREAMING
    sc.start_rx()
    chk("start_rx again while TX keyed -> STREAMING",
        state(svc) == int(RadioState.STREAMING))

    # 7. stop_tx while RX runs: still STREAMING
    sc.stop_tx()
    chk("stop_tx while RX runs: TX thread gone", not tx_alive(svc))
    chk("  ... state still STREAMING", state(svc) == int(RadioState.STREAMING))
    chk("  ... FLAG_TX_ACTIVE clear", not (flags(svc) & Status.FLAG_TX_ACTIVE))

    # 8. stop_rx with no TX: INIT_OK
    sc.stop_rx()
    chk("stop_rx with no TX -> INIT_OK", state(svc) == int(RadioState.INIT_OK))

    # 9. ERROR stays FAULT whatever the threads do
    be.health.state = sdr.SDRDeviceState.ERROR
    chk("backend ERROR -> FAULT", state(svc) == int(RadioState.FAULT))
    be.health.state = sdr.SDRDeviceState.IDLE

    # 10. close: UNINIT
    sc.stop()
    be.close()
    chk("close -> UNINIT", state(svc) == int(RadioState.UNINIT))
    svc.sched = None
    svc.backend = None
    chk("torn down (no backend) -> UNINIT", state(svc) == int(RadioState.UNINIT))

    # 11. mock scheduler: RX thread reference dead, TX thread alive, card STREAMING
    stop = threading.Event()
    t = threading.Thread(target=stop.wait, daemon=True)
    t.start()
    dead = threading.Thread(target=lambda: None)
    dead.start(); dead.join()
    svc2 = S.RadioService(serial="G40-SIM2", host_type=HostType.G40_RX, kind="mock",
                          simulate=True, cmd_port=0, discovery_port=0,
                          broadcast="127.0.0.1")
    svc2.backend = types.SimpleNamespace(
        health=types.SimpleNamespace(state=sdr.SDRDeviceState.STREAMING))
    svc2.sched = types.SimpleNamespace(_rx=dead, _tx=t)
    chk("mock scheduler: dead RX ref + alive TX, card STREAMING -> INIT_OK",
        svc2._radio_state() == int(RadioState.INIT_OK), f"(state={svc2._radio_state()})")
    svc2.sched._rx = None
    chk("mock scheduler: no RX ref + alive TX -> INIT_OK",
        svc2._radio_state() == int(RadioState.INIT_OK))
    stop.set(); t.join(1.0)

    n = sum(checks)
    print(f"\nresult: {n}/{len(checks)} checks passed")
    return 0 if n == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
