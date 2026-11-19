#!/usr/bin/env python3
"""
test_radio_role.py: the radio role (what a radio is acting as) on the
simulated service, no hardware.

  1. the shared table: every role has a label and panels, any refuses nothing,
     stops pass in every role
  2. set_role: unknown refused, stored across a service restart, --role only
     seeds a radio with no stored role
  3. the service refuses what the role does not use (monitor: start_tx,
     set_tx_waveform, transponder; bitpipe: set_tx_waveform; dcfe: init),
     with the role in the reply, and still takes stops
  4. a role change is refused while TX runs and dcfe is refused while open
  5. get_config carries the role, and set_role bumps cfg_seq
  6. mission planner: technique_allowed and the validator flag a step the
     radio's role cannot run

    python3 tests/test_radio_role.py
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
_TMP = tempfile.mkdtemp(prefix="dpmg_role_")
os.environ["DPMG_DATA_DIR"] = _TMP

from dpmg.radio import dpmg_service as ds                              # noqa: E402
from dpmg.radio import radio_role as rr                                # noqa: E402
from dpmg.link.proto import Cmd, CmdStatus, HostType             # noqa: E402

FAILS = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


INIT = {"kind": "g40", "sample_rate": 1_920_000, "center_freq": 430_500_000,
        "rx_gain": 220, "tx_gain": 20.0, "clock_source": "internal",
        "rx_antenna": "RXA1", "tx_antenna": "TXA1",
        "extras": {"card": 0, "chan_mode": "single", "tx_center_freq": 430_500_000}}


def make(serial="G40-ROLE", role=None):
    svc = ds.RadioService(serial=serial, host_type=HostType.G40_RX, kind="g40",
                          simulate=True, cmd_port=48110, discovery_port=48111,
                          broadcast="127.0.0.1", role=role)
    svc.loop = asyncio.get_running_loop()
    return svc


class _Ep:
    def send(self, msg, addr):
        pass


def table():
    print("[1] role table")
    check(set(rr.ORDER) == set(rr.ROLES), "every role is in the display order")
    for r in rr.ORDER:
        check(rr.label(r) and rr.short(r), f"{r}: has a label and a short name")
    check(all(rr.refusal("any", c) is None for c in
              ("start_tx", "set_tx_waveform", "transponder", "bitpipe_start", "init")),
          "any refuses nothing")
    for r in rr.ORDER:
        check(all(rr.refusal(r, c) is None for c in ("stop_tx", "stop_rx", "close", "get_config",
                                                      "set_role", "fpga_status")),
              f"{r}: stops and status always pass")
    check(rr.refusal("monitor", "start_tx") and not rr.can_tx("monitor"), "monitor refuses start_tx")
    check(rr.refusal("bitpipe", "start_tx") and rr.refusal("bitpipe", "start_tx", True) is None,
          "bitpipe keys TX only with the stream modem attached")
    check(rr.refusal("dcfe", "init") and rr.refusal("dcfe", "start_rx"), "dcfe refuses init and start_rx")
    check(rr.shows("jammer", "jammer") and not rr.shows("jammer", "playback"),
          "jammer shows the jammer panel, not IQ playback")
    check(not rr.shows("monitor", "tx"), "monitor hides the TX controls")
    check(rr.label("nonsense") == rr.label("any"), "an unknown role reads as any")


async def service():
    print("[2] set_role and persistence")
    svc = make()
    svc.ep = _Ep()
    check(svc._role == "any", "a new radio starts as any")
    a = await svc._apply(Cmd(1, "set_role", {"role": "pilot"}))
    check(a.status == CmdStatus.REJECTED, "an unknown role is refused")
    seq0 = svc._cfg_seq
    a = await svc._apply(Cmd(2, "set_role", {"role": "monitor", "by": "test"}))
    check(a.status == CmdStatus.OK and a.result.get("prev") == "any", "set_role monitor")
    svc2 = make()
    check(svc2._role == "monitor" and svc2._role_by == "test", "the role survives a service restart")
    svc3 = make(serial="G40-OTHER", role="jammer")
    check(svc3._role == "jammer", "--role seeds a radio with no stored role")
    svc4 = make(role="jammer")
    check(svc4._role == "monitor", "--role does not override a stored role")

    print("[3] the service refuses what the role does not use")
    a = await svc._apply(Cmd(10, "init", INIT))
    check(a.status == CmdStatus.OK, "init as monitor")
    for n, (c, p) in enumerate((("start_tx", {}), ("set_tx_waveform", {"mode": "tone"}),
                                ("transponder", {}), ("bitpipe_start", {}),
                                ("replay_recording", {}))):
        a = await svc._apply(Cmd(11 + n, c, p))
        check(a.status == CmdStatus.REJECTED and a.result.get("role") == "monitor"
              and "role" in a.result.get("err", ""), f"monitor refuses {c}")
    a = await svc._apply(Cmd(20, "start_rx", {}))
    check(a.status == CmdStatus.OK, "monitor still receives")
    a = await svc._apply(Cmd(21, "stop_tx", {}))
    check(a.status == CmdStatus.OK, "stop_tx always passes")
    a = await svc._apply(Cmd(22, "get_config", {}))
    check(a.result.get("role") == "monitor", "get_config carries the role")

    print("[4] role change while TX runs, dcfe while open")
    a = await svc._apply(Cmd(30, "set_role", {"role": "jammer"}))
    check(a.status == CmdStatus.OK, "monitor to jammer with TX off")
    check(svc._cfg_seq == seq0, "cfg_seq counts only acked state commands through _apply_and_ack")
    a = await svc._apply(Cmd(31, "set_tx_waveform", {"mode": "tone", "offset_hz": 100_000,
                                                     "ampl": 0.1}))
    check(a.status == CmdStatus.OK, f"jammer loads a waveform ({(a.result or {}).get('err', 'ok')})")
    a = await svc._apply(Cmd(32, "start_tx", {"mode": "CONTINUOUS"}))
    check(a.status == CmdStatus.OK, f"jammer keys TX ({(a.result or {}).get('err', 'ok')})")
    a = await svc._apply(Cmd(33, "set_role", {"role": "monitor"}))
    check(a.status == CmdStatus.REJECTED and "TX" in a.result.get("err", ""),
          "role change refused while TX runs")
    a = await svc._apply(Cmd(34, "set_role", {"role": "bitpipe"}))
    check(a.status == CmdStatus.REJECTED, "no role change at all while TX runs")
    await svc._apply(Cmd(35, "stop_tx", {}))
    a = await svc._apply(Cmd(36, "set_role", {"role": "bitpipe"}))
    check(a.status == CmdStatus.OK, "bitpipe after stop_tx")
    a = await svc._apply(Cmd(37, "start_tx", {"mode": "CONTINUOUS"}))
    check(a.status == CmdStatus.REJECTED and "stream modem" in a.result.get("err", ""),
          "bitpipe refuses start_tx without the stream modem")
    a = await svc._apply(Cmd(38, "set_role", {"role": "dcfe"}))
    check(a.status == CmdStatus.REJECTED and "close" in a.result.get("err", ""),
          "dcfe refused while the radio is open")
    await svc._apply(Cmd(39, "close", {}))
    a = await svc._apply(Cmd(40, "set_role", {"role": "dcfe"}))
    check(a.status == CmdStatus.OK, "dcfe once closed")
    a = await svc._apply(Cmd(41, "init", INIT))
    check(a.status == CmdStatus.REJECTED and "dCFE" in a.result.get("err", ""),
          "dcfe refuses init")
    a = await svc._apply(Cmd(42, "set_role", {"role": "any"}))
    check(a.status == CmdStatus.OK, "back to any")

    print("[5] cfg_seq through the ack path")
    svc.ep = _Ep()
    s0 = svc._cfg_seq
    await svc._apply_and_ack(Cmd(50, "set_role", {"role": "playback"}), ("127.0.0.1", 1))
    check(svc._cfg_seq == s0 + 1, "set_role bumps cfg_seq so the GUIs re-read")
    await svc._apply_and_ack(Cmd(51, "set_role", {"role": "any"}), ("127.0.0.1", 1))
    await svc._teardown()


def mission():
    print("[6] mission planner")
    try:
        from dpmg.mission import catalog, mission_file
    except ImportError:
        print("  skip (no mission planner in this tree)")
        return
    grp = lambda t: catalog.get(t).group
    check(rr.technique_allowed("jammer", "jammer.barrage", grp("jammer.barrage")),
          "a jammer radio runs a jammer step")
    check(not rr.technique_allowed("monitor", "jammer.barrage", grp("jammer.barrage")),
          "a monitor radio cannot run a jammer step")
    check(rr.technique_allowed("monitor", "record", grp("record")), "a monitor radio records")
    check(rr.technique_allowed("any", "iq_playback", grp("iq_playback")), "any runs everything")
    plan = mission_file.new_mission("role test")
    if plan is None:
        check(False, "mission_file.new_mission exists")
        return
    plan["radios"] = [{"id": "R1", "type": "g40", "role": "monitor"}]
    plan["steps"] = [{"id": "s1", "radio": "R1", "technique": "jammer.barrage",
                      "schedule": {"start_s": 0, "duration_s": 10},
                      "params": catalog.get("jammer.barrage").fresh_params()}]
    conf = mission_file.validate(plan)
    msgs = [c.message if hasattr(c, "message") else str(c) for c in conf]
    check(any("role" in m for m in msgs), f"validator flags the step for the role ({msgs[:2]})")
    plan["radios"][0]["role"] = "jammer"
    conf = mission_file.validate(plan)
    msgs = [c.message if hasattr(c, "message") else str(c) for c in conf]
    check(not any("role" in m for m in msgs), "a jammer radio passes")


def main():
    table()
    asyncio.run(service())
    mission()
    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}")
        sys.exit(1)
    print("all passed")


if __name__ == "__main__":
    main()
