#!/usr/bin/env python3
"""
X40 RF-port exposure + same-side co-stream rule
================================================================
The X40 is 4R1T: RX on J1-J4, one TX on J5 (TXA1). The A-side receivers RXA1 (J2)
and RXA2 (J1) share the transmitter's LO, so they cannot stream while J5 keys;
FDD is TX J5 + RX B-side (RXB1/J4, RXB2/J3).

  1. the shared rule (sdr_core.x40_same_side_conflict) flags exactly the A-side
     RX + TXA1 pairs, case-insensitively, and nothing else
  2. the real RadioService (kind x40, MockBackend) accepts an A-side init
     (receive-only is legitimate) but reports tx_locked + a warning, and then
     rejects start_tx, transceive, armed ground_checkout and armed+real
     transponder with x40_same_side=True
  3. a B-side init carries no lock and start_tx is not refused for that reason
  4. a G40 is never affected (no lock even with RXA1 + TXA1)

Stdlib + numpy. Exit 0 = all passed.    python3 tests/test_x40_ports.py
"""
import asyncio, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dpmg.radio import sdr_core as sdr
from dpmg.link.proto import CmdStatus, HostType
from dpmg.radio.dpmg_service import RadioService

FAILS = []
def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)

def test_rule():
    print("[1] shared rule")
    for rx in ("RXA1", "RXA2", "rxa1"):
        check(sdr.x40_same_side_conflict(rx, "TXA1") is not None, f"{rx}+TXA1 conflicts")
    for rx in ("RXB1", "RXB2", "rxb2"):
        check(sdr.x40_same_side_conflict(rx, "TXA1") is None, f"{rx}+TXA1 is FDD-clean")
    check(sdr.x40_same_side_conflict("RXA1", None) is None, "no TX handle -> no conflict")
    msg = sdr.x40_same_side_conflict("RXA2", "TXA1")
    check("J1" in msg and "J5" in msg and "J4" in msg, "message names the connectors + the fix")
    check(set(sdr.X40_RX_CONNECTOR) == {"RXA1", "RXA2", "RXB1", "RXB2"}, "all four RX handles mapped")

def _svc(kind, host_type):
    return RadioService(serial=f"{kind.upper()}-SIM", host_type=host_type, kind=kind,
                        simulate=True, cmd_port=47890, discovery_port=47891,
                        broadcast="127.0.0.1")

def _init_p(rx, tx="TXA1"):
    return {"kind": "x40", "sample_rate": 50_000_000, "center_freq": 425_025_000,
            "rx_gain": 245, "tx_gain": 20.0, "clock_source": "internal",
            "rx_antenna": rx, "tx_antenna": tx,
            "extras": {"card": 0, "tx_center_freq": 425_025_000}}

async def _service_cases():
    print("[2] X40 service: A-side RX locks TX")
    svc = _svc("x40", HostType.X40_TX)
    svc.loop = asyncio.get_running_loop()
    cid = 1
    ack = await svc._cmd_init(cid, _init_p("RXA1")); cid += 1
    check(ack.status == CmdStatus.OK, f"A-side init is accepted ({ack.result})")
    check(bool((ack.result or {}).get("tx_locked")), "init result carries tx_locked")
    check("J2" in str((ack.result or {}).get("warning")), "init warning names J2")
    for cmd, p in (("start_tx", {"mode": "CONTINUOUS"}),
                   ("transceive", {}),
                   ("ground_checkout", {"armed": True, "seconds": 0.05}),
                   ("transponder", {"armed": True, "emulate": False, "seconds": 0.1})):
        a = await getattr(svc, f"_cmd_{cmd}")(cid, p); cid += 1
        check(a.status == CmdStatus.REJECTED and (a.result or {}).get("x40_same_side"),
              f"{cmd} refused with x40_same_side ({a.status}, {(a.result or {}).get('err', '')[:50]})")
    a = await svc._cmd_ground_checkout(cid, {"armed": False, "seconds": 0.05}); cid += 1
    check(not (a.result or {}).get("x40_same_side"), "unarmed checkout is not blocked by the rule")

    print("[3] X40 service: B-side RX has no lock")
    ack = await svc._cmd_init(cid, _init_p("RXB1")); cid += 1
    check(ack.status == CmdStatus.OK and not (ack.result or {}).get("tx_locked"),
          "B-side init has no tx_locked")
    a = await svc._cmd_start_tx(cid, {"mode": "CONTINUOUS"}); cid += 1
    check(not (a.result or {}).get("x40_same_side"),
          f"start_tx not refused by the rule on J4 (status {a.status})")
    await svc._cmd_close(cid); cid += 1

    print("[4] G40 never affected")
    g = _svc("g40", HostType.G40_RX if hasattr(HostType, "G40_RX") else HostType.X40_TX)
    g.loop = asyncio.get_running_loop()
    p = _init_p("RXA1"); p["kind"] = "g40"; p["sample_rate"] = 1_920_000
    ack = await g._cmd_init(cid, p); cid += 1
    check(ack.status == CmdStatus.OK and not (ack.result or {}).get("tx_locked"),
          "G40 RXA1+TXA1 init has no lock")
    a = await g._cmd_start_tx(cid, {"mode": "CONTINUOUS"}); cid += 1
    check(not (a.result or {}).get("x40_same_side"), "G40 start_tx not refused by the rule")
    await g._cmd_close(cid)

def main():
    test_rule()
    asyncio.run(_service_cases())
    print(f"\n{len(FAILS)} failure(s)")
    return 1 if FAILS else 0

if __name__ == "__main__":
    sys.exit(main())
