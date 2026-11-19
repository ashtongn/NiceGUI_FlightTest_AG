#!/usr/bin/env python3
"""
Close clears the decode plan
============================
After close, get_config used to report the last decode plan, and a later
start_rx with no new plan decoded against it. The real RadioService (kind g40,
simulated) is given a plan, closed, and asked again.

Stdlib + numpy. Exit 0 = all passed.    python3 tests/test_close_clears_plan.py
"""
import asyncio, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dpmg.link.proto import CmdStatus, HostType
from dpmg.radio import dpmg_service as ds
from dpmg.radio.dpmg_service import RadioService

FAILS = []
def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)

PLAN = {"entries": [{"offset_hz": 300000.0, "modulation": "QPSK", "symbol_rate": 4800,
                     "message": "RANGE 05 KM"}]}

INIT = {"kind": "g40", "sample_rate": 1_920_000, "center_freq": 1_325_000_000,
        "rx_gain": 245, "tx_gain": 20.0, "clock_source": "internal",
        "rx_antenna": "RXA1", "tx_antenna": "TXA1",
        "extras": {"card": 0, "tx_card": 1, "chan_mode": "single", "rf_bandwidth_hz": 1_536_000}}

async def _cases():
    svc = RadioService(serial="G40-SIM", host_type=HostType.G40_RX, kind="g40",
                       simulate=True, cmd_port=47896, discovery_port=47897,
                       broadcast="127.0.0.1")
    svc.loop = asyncio.get_running_loop()
    ack = await svc._cmd_init(1, INIT)
    check(ack.status == CmdStatus.OK, f"simulated G40 init ({ack.result})")
    ack = await svc._cmd_set_plan(2, {"plan": PLAN, "anchor": "tx"})
    check(ack.status == CmdStatus.OK, f"set_plan accepted ({ack.result})")
    cfg = (await svc._cmd_get_config(3, {})).result
    check(cfg.get("plan_entries") == 1, f"get_config reports the plan: {cfg.get('plan_entries')}")
    ack = await svc._cmd_close(4)
    check(ack.status == CmdStatus.OK, "close")
    cfg = (await svc._cmd_get_config(5, {})).result
    check(cfg.get("plan_entries") is None, f"no plan after close: {cfg.get('plan_entries')}")
    check(svc._plan_anchor is None, "the plan anchor is cleared too")

def main():
    if not ds._HAVE_CHAN:
        print("channelizer not available here; set_plan is refused, nothing to check")
        return 0
    asyncio.run(_cases())
    print(f"\n{'FAILED: ' + str(len(FAILS)) if FAILS else 'all passed'}")
    return 1 if FAILS else 0

if __name__ == "__main__":
    sys.exit(main())
