"""INIT_CRASH_GUARD: an init that took the whole service down (libsidekiq exits
on a critical error, for example a G40 external-clock init with no 10 MHz on
REF IN) leaves a marker; after the restart the same init is refused for a while,
so a client resending it cannot restart the service over and over. Offline,
simulated radios, a temporary data folder."""
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="dpmg_initguard_")
os.environ["DPMG_DATA_DIR"] = os.path.join(_TMP, "data")

from dpmg.radio import dpmg_service as ds                  # noqa: E402
from dpmg.link.proto import Cmd, CmdStatus, HostType       # noqa: E402

FAILS = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def svc(serial="G40-GUARD"):
    s = ds.RadioService(serial=serial, host_type=HostType.G40_RX, kind="g40", simulate=True,
                        cmd_port=48150, discovery_port=48151, broadcast="127.0.0.1")
    s.loop = asyncio.get_running_loop()
    return s


def init_p(clock, **extra):
    return {"kind": "g40", "sample_rate": 1.92e6, "center_freq": 425.025e6, "rx_gain": 200,
            "tx_gain": 30.0, "clock_source": clock, "rx_antenna": "RXA1",
            "extras": {"card": 0}, **extra}


def plant_marker(s, clock, age_s=0.0):
    with open(s._init_mark_path, "w") as fh:
        json.dump({"t": time.time() - age_s, "cmd_id": 7, "kind": "g40",
                   "clock_source": clock}, fh)


async def main():
    print("[1] a clean init leaves no marker")
    s = svc()
    a = await s._apply(Cmd(1, "init", init_p("internal")))
    check(a.status == CmdStatus.OK, f"init internal ok ({a.result})")
    check(not os.path.exists(s._init_mark_path), "marker removed after the init returned")
    await s._teardown()

    print("[2] a marker left by a dead process refuses the same init")
    plant_marker(s, "external")
    s2 = svc()
    check(s2._crashed_init is not None, "marker read at start")
    check(not os.path.exists(s2._init_mark_path), "marker removed once read")
    a = await s2._apply(Cmd(2, "init", init_p("external")))
    err = (a.result or {}).get("err", "")
    check(a.status == CmdStatus.REJECTED and "10 MHz on REF" in err and "force_after_crash" in err,
          f"same clock refused with the reason ({err})")
    a = await s2._apply(Cmd(3, "init", init_p("external")))
    check(a.status == CmdStatus.REJECTED, "a resend is refused too (no restart loop)")

    print("[3] another clock goes ahead and ends the hold")
    a = await s2._apply(Cmd(4, "init", init_p("internal")))
    check(a.status == CmdStatus.OK, f"internal init ok ({a.result})")
    check(s2._crashed_init is None, "a good init clears the hold")
    await s2._teardown()

    print("[4] force_after_crash retries it")
    plant_marker(s, "external")
    s3 = svc()
    a = await s3._apply(Cmd(5, "init", init_p("external", force_after_crash=True)))
    check(a.status != CmdStatus.REJECTED or "stopped the radio service" not in
          (a.result or {}).get("err", ""), f"forced init not refused by the guard ({a.result})")
    await s3._teardown()

    print("[5] an old marker does not hold")
    plant_marker(s, "external", age_s=ds.INIT_CRASH_HOLD_S + 5)
    s4 = svc()
    check(s4._crashed_init_refusal(init_p("external")) is None, "past the hold: allowed")

    print("[6] the refusal fits one datagram")
    plant_marker(s, "external")
    s5 = svc()
    a = await s5._apply(Cmd(6, "init", init_p("external")))
    from dpmg.link import proto
    check(len(proto.encode(a, 1)) <= proto.MAX_DATAGRAM, "refusal ack fits")


if __name__ == "__main__":
    asyncio.run(main())
    print("all passed" if not FAILS else f"{len(FAILS)} FAILED")
    sys.exit(1 if FAILS else 0)
