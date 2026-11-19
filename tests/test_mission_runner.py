"""Tests for the run model: the step-to-command mapping and the timed drive.

Uses a fake host that records every cmd() instead of a radio, so nothing keys.
Run with python3:

    python3 tests/test_mission_runner.py
"""

import asyncio
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)          # repo root
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from types import SimpleNamespace

from dpmg.link.proto import RadioState, Status
from dpmg.mission import catalog
from dpmg.mission import mission_runner as mr
from dpmg.mission.mission_runner import MissionRunner, AuditLog, step_commands, plan_events


_passed = 0
_failed = 0


def check(cond, msg):
    global _passed, _failed
    if cond:
        _passed += 1
    else:
        _failed += 1
        print(f"  FAIL: {msg}")


class FakeAck:
    def __init__(self, status=0, result=None):
        self.status = status
        self.result = result or {}


class FakeHost:
    """Records cmd calls; answers OK unless a name is in `fail`."""

    def __init__(self, serial="SIM", fail=()):
        self.serial = serial
        self.calls = []        # list of (name, params)
        self.fail = set(fail)

    async def cmd(self, name, params=None, timeout=None):
        self.calls.append((name, dict(params or {})))
        if name in self.fail:
            return FakeAck(status=1, result={"err": "forced failure"})
        return FakeAck(status=0)

    def names(self):
        return [n for n, _ in self.calls]


def mkstep(sid, tech, start, stop, radio="SIM", radiates=True, params=None):
    return {"id": sid, "radio": radio, "technique": tech, "radiates": radiates,
            "params": params or {}, "schedule": {"start_s": start, "stop_s": stop}}


# mapping tests

def test_jammer_maps_to_gen_then_start():
    start, stop = step_commands(mkstep("s1", "jammer.barrage", 0, 10))
    names = [n for n, _ in start]
    check(names == ["gen_test_waveform", "start_tx"], f"jammer start cmds: {names}")
    check(start[0][1]["load_tx"] is True, "gen_test_waveform arms the TX source")
    check(start[1][1]["mode"] == "CONTINUOUS", "jammer keys CONTINUOUS")
    check(stop == [("stop_tx", {})], "jammer stop is stop_tx")


def test_tools_maps_like_jammer():
    start, _ = step_commands(mkstep("s1", "tools.tone", 0, 5))
    check([n for n, _ in start] == ["gen_test_waveform", "start_tx"],
          "tools generator uses gen_test_waveform then start_tx")


def test_old_iq_playback_scenario_still_maps():
    # an iq_playback step with source scenario (a plan from before the Comm
    # waveform entry that did not pass through mission_file.loads) maps like
    # scenario_json, by the old name key
    start, stop = step_commands(mkstep(
        "s1", "iq_playback", 0, 30,
        params={"source": "scenario", "name": "mod_coding_card"}))
    names = [n for n, _ in start]
    check(names == ["gen_scenario", "start_tx"], f"scenario playback cmds: {names}")
    check(start[0][1]["spec_path"] == "mod_coding_card", "scenario by name")
    check(stop == [("stop_tx", {})], "stops with stop_tx")


TWO_SIG = {"name": "two", "sample_rate_hz": 1_920_000, "signals": [
    {"modulation": "FSK", "symbol_rate": 4800, "tone_spacing_hz": 5000,
     "offset_hz": -200_000.0, "message": "a"},
    {"modulation": "QPSK", "symbol_rate": 9600, "offset_hz": 150_000.0, "message": "b"}]}


def test_scenario_json_console_spec_inline():
    p = dict(catalog.get("scenario_json").fresh_params(),
             source="console", spec=TWO_SIG, spec_name="two.json")
    start, stop = step_commands(mkstep("s1", "scenario_json", 0, 30, params=p))
    check([n for n, _ in start] == ["gen_scenario", "start_tx"],
          f"comm waveform: build then key: {[n for n, _ in start]}")
    gp = start[0][1]
    check(gp == {"spec": TWO_SIG, "load_tx": True, "match_radio_rate": True, "peak_fs": 0.45},
          f"gen_scenario gets the inline spec, loads TX, matches the rate: {gp}")
    check("spec_path" not in gp and "spec_name" not in gp, "no path keys ride along")
    check(start[1] == ("start_tx", {"mode": "CONTINUOUS"}), "keys continuous")
    check(stop == [("stop_tx", {})], "stops with stop_tx")


def test_scenario_json_radio_file():
    p = dict(catalog.get("scenario_json").fresh_params(),
             source="radio", spec_path="scenarios/json/fsk_sweep.json",
             match_radio_rate=False, tx_atten_db=20)
    start, _ = step_commands(mkstep("s1", "scenario_json", 0, 30, params=p))
    check([n for n, _ in start] == ["set_tx_atten", "gen_scenario", "start_tx"],
          f"tuning first, then build and key: {[n for n, _ in start]}")
    check(start[1][1] == {"spec_path": "scenarios/json/fsk_sweep.json", "load_tx": True,
                          "match_radio_rate": False, "peak_fs": 0.45},
          f"a radio file goes as spec_path, rate option honoured: {start[1][1]}")


def test_scenario_json_blank_still_maps_without_raising():
    # validation flags it as needs input; the mapping itself sends an empty
    # path, which the service rejects, rather than raising in the planner
    p = catalog.get("scenario_json").fresh_params()
    start, _ = step_commands(mkstep("s1", "scenario_json", 0, 30, params=p))
    check(start[0][1].get("spec_path") == "" and "spec" not in start[0][1],
          f"blank step maps to an empty spec_path: {start[0][1]}")


class PushHost(FakeHost):
    """A FakeHost with the link's FILE_* push."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.pushed = []       # (name, bytes)

    async def send_file(self, name, data, timeout=60.0, window=8):
        self.pushed.append((name, bytes(data)))
        return name[:28] + ".json" if len(name) > 32 else name


def test_small_console_spec_rides_inline_at_start():
    host = PushHost()
    runner = MissionRunner(lambda rid: host, AuditLog(), speed=0.0)
    p = dict(catalog.get("scenario_json").fresh_params(), source="console",
             spec=TWO_SIG, spec_name="two.json")
    asyncio.run(runner.start_step_now(mkstep("s1", "scenario_json", 0, 10, params=p)))
    gen = next(pr for n, pr in host.calls if n == "gen_scenario")
    check(host.pushed == [], "a small spec is not pushed as a file")
    check(gen.get("spec") == TWO_SIG and "spec_inbox" not in gen, f"inline spec sent: {gen.keys()}")


def test_big_console_spec_goes_by_spec_inbox_at_start():
    from dpmg.mission import scenario_spec as ss
    host = PushHost()
    audit = AuditLog()
    runner = MissionRunner(lambda rid: host, audit, speed=0.0)
    big = {"name": "big", "signals": [dict(TWO_SIG["signals"][0], message="m" * 60)
                                      for _ in range(24)]}
    check(ss.needs_inbox(big), "the test spec is over the inline limit")
    p = dict(catalog.get("scenario_json").fresh_params(), source="console",
             spec=big, spec_name="big.json")
    asyncio.run(runner.start_step_now(mkstep("s7", "scenario_json", 0, 10, params=p)))
    check(len(host.pushed) == 1 and host.pushed[0][0] == "mission_s7.json",
          f"the spec was pushed once under the step's name: {[n for n, _ in host.pushed]}")
    check(host.pushed[0][1] == ss.spec_blob(big), "the pushed bytes are the compact spec")
    gen = next(pr for n, pr in host.calls if n == "gen_scenario")
    check(gen.get("spec_inbox") == "mission_s7.json" and "spec" not in gen,
          f"gen_scenario then names the inbox copy instead of the spec: {gen}")
    check(gen.get("load_tx") is True and gen.get("match_radio_rate") is True,
          "the other gen_scenario options are kept")
    check(any("pushed as mission_s7.json" in l for l in audit.lines()), "audit notes the push")
    check([n for n, _ in host.calls][:2] == ["gen_scenario", "start_tx"], "then build and key")


def test_failed_spec_push_does_not_key():
    class BadPush(PushHost):
        async def send_file(self, name, data, timeout=60.0, window=8):
            raise TimeoutError("FILE_OFFER never acked")
    host = BadPush()
    audit = AuditLog()
    runner = MissionRunner(lambda rid: host, audit, speed=0.0)
    big = {"name": "big", "signals": [dict(TWO_SIG["signals"][0], message="m" * 60)
                                      for _ in range(24)]}
    p = dict(catalog.get("scenario_json").fresh_params(), source="console", spec=big)
    asyncio.run(runner.start_step_now(mkstep("s8", "scenario_json", 0, 10, params=p)))
    check("gen_scenario" not in host.names() and "start_tx" not in host.names(),
          f"nothing built or keyed after a failed push: {host.names()}")
    check(any("spec push" in l and "error" in l for l in audit.lines()), "audit explains the push failure")


def test_gen_scenario_gets_the_long_timeout():
    class TimeoutHost(FakeHost):
        def __init__(self):
            super().__init__()
            self.timeouts = {}

        async def cmd(self, name, params=None, timeout=None):
            self.timeouts[name] = timeout
            return await super().cmd(name, params, timeout)
    host = TimeoutHost()
    runner = MissionRunner(lambda rid: host, AuditLog(), speed=0.0)
    p = dict(catalog.get("scenario_json").fresh_params(), source="radio",
             spec_path="scenarios/json/x.json")
    asyncio.run(runner.start_step_now(mkstep("s1", "scenario_json", 0, 10, params=p)))
    check(host.timeouts.get("gen_scenario") == mr.GEN_SCENARIO_TIMEOUT_S == 180.0,
          f"a scenario build waits up to 180 s: {host.timeouts}")
    check(host.timeouts.get("start_tx") == mr.CMD_TIMEOUT_S == 30.0, "other commands keep 30 s")


def test_scenario_json_catalog_entry():
    e = catalog.get("scenario_json")
    check(e is not None and e.group == "Comms" and e.radiates and e.default_dur == 30.0,
          "Comms group, radiates, 30 s")
    check(e.label == "Comm waveform from a scenario JSON", f"label: {e.label}")
    p = e.fresh_params()
    check(p.get("source") == "console" and p.get("spec") is None and p.get("spec_path") == ""
          and p.get("match_radio_rate") is True, f"fresh params: {p}")
    check({"center_hz", "tx_center_hz", "tx_atten_db", "rx_gain_index"} <= set(p),
          "carries the RF fields")
    src = e.field("source")
    check([v for v, _ in src.choices] == ["console", "radio"], "source: console or radio")
    iq = catalog.get("iq_playback")
    check(iq.field("source") is None and iq.fresh_params().get("source") == "file",
          "IQ playback no longer offers a scenario source but still says file")


def test_iq_playback_file_with_retune():
    start, _ = step_commands(mkstep(
        "s1", "iq_playback", 0, 30,
        params={"source": "file", "name": "cap.iq.bin", "retune_hz": 915e6}))
    names = [n for n, _ in start]
    check(names == ["set_center", "set_tx_waveform", "start_tx"],
          f"file playback with retune: {names}")
    check(start[0][1]["hz"] == 915e6, "retune uses set_center")
    # an old plan's "name" key still reaches the service as "path"
    check(start[1][1] == {"kind": "file", "path": "cap.iq.bin"},
          f"set_tx_waveform sends path: {start[1][1]}")


def test_iq_playback_file_path_key():
    path = "/app_svc/data/waveforms/cap.c64"
    start, _ = step_commands(mkstep(
        "s1", "iq_playback", 0, 30, params={"source": "file", "path": path}))
    check([n for n, _ in start] == ["set_tx_waveform", "start_tx"],
          "file playback without retune skips set_center")
    # the service only reads "path" (or "inbox") for kind=file
    check(start[0][1] == {"kind": "file", "path": path},
          f"set_tx_waveform params: {start[0][1]}")


def test_old_iq_playback_scenario_path_key():
    start, _ = step_commands(mkstep(
        "s1", "iq_playback", 0, 30,
        params={"source": "scenario", "path": "/app_svc/plans/x.json"}))
    check(start[0] == ("gen_scenario",
                       {"spec_path": "/app_svc/plans/x.json", "load_tx": True,
                        "match_radio_rate": True}),
          f"scenario playback uses spec_path at the radio's rate: {start[0]}")


def test_transponder_maps_to_transponder():
    start, _ = step_commands(mkstep("s1", "transponder", 0, 20,
                                    params={"threshold_db": 12.0}))
    check(start[0][0] == "transponder", "transponder technique uses transponder cmd")
    check(start[0][1]["seconds"] == 20, "transponder runs for the step duration")
    check(start[0][1]["trigger_snr_db"] == 12.0, "threshold maps to trigger_snr_db")
    check(start[0][1]["emulate"] is True, "transponder defaults to emulate (no radiate)")


def test_record_is_receive_only():
    start, stop = step_commands(mkstep("s1", "record", 0, 10, radiates=False,
                                       params={"band": "full"}))
    check(start[0][0] == "record_iq", "record uses record_iq")
    check(stop == [], "record has no stop command (self-limiting)")


def test_replay_maps_to_replay_then_start():
    start, stop = step_commands(mkstep("s1", "replay", 0, 10,
                                       params={"name": "rec_1", "mode": "CONTINUOUS"}))
    names = [n for n, _ in start]
    check(names == ["replay_recording", "start_tx"], f"replay cmds: {names}")
    check(start[1][1]["mode"] == "CONTINUOUS", "replay honours the loop mode")
    check(stop == [("stop_tx", {})], "replay stop is stop_tx")


def test_jammer_pulse_fields_only_when_pulsing():
    cw = catalog.get("jammer.noise").fresh_params()          # prf 0 = CW
    start, _ = step_commands(mkstep("s1", "jammer.noise", 0, 10, params=cw))
    gp = start[0][1]
    check("prf" not in gp and "duty" not in gp, f"CW jammer sends no prf/duty: {gp}")
    check(gp["kind"] == "noise" and gp["bw"] == 1.0e6 and gp["seconds"] == 0.5,
          f"noise jammer sends its panel keys: {gp}")
    pulsed = dict(cw, prf=1000.0, duty=0.25)
    gp = step_commands(mkstep("s1", "jammer.noise", 0, 10, params=pulsed))[0][0][1]
    check(gp["prf"] == 1000.0 and gp["duty"] == 0.25, f"pulsed jammer sends prf/duty: {gp}")


def test_rf_center_and_atten_before_keying():
    p = dict(catalog.get("jammer.tone").fresh_params(), center_hz=915e6, tx_atten_db=20)
    start, _ = step_commands(mkstep("s1", "jammer.tone", 0, 10, params=p))
    names = [n for n, _ in start]
    check(names == ["set_center", "set_tx_atten", "gen_test_waveform", "start_tx"],
          f"retune and atten come first: {names}")
    check(start[0][1] == {"hz": 915e6}, "set_center in Hz")
    check(start[1][1] == {"qdb": 80}, "set_tx_atten in quarter dB (20 dB = 80)")
    check("center_hz" not in start[2][1] and "tx_atten_db" not in start[2][1],
          "RF keys are not passed to gen_test_waveform")
    blank = catalog.get("jammer.tone").fresh_params()        # both left blank
    names = [n for n, _ in step_commands(mkstep("s1", "jammer.tone", 0, 10,
                                                params=blank))[0]]
    check(names == ["gen_test_waveform", "start_tx"], "blank RF fields leave the radio alone")
    p = dict(catalog.get("replay").fresh_params(), name="r1", tx_atten_db=41.75)
    start, _ = step_commands(mkstep("s1", "replay", 0, 10, params=p))
    check(start[0] == ("set_tx_atten", {"qdb": 167}), f"replay atten too: {start[0]}")


def test_transponder_panel_keys():
    p = dict(catalog.get("transponder").fresh_params(), armed=True, emulate=False,
             trigger_snr_db=9.0, dwell=0.1, ring=32)
    start, _ = step_commands(mkstep("s1", "transponder", 0, 20, params=p))
    tp = start[0][1]
    check(tp["trigger_snr_db"] == 9.0 and tp["armed"] is True and tp["emulate"] is False
          and tp["dwell"] == 0.1 and tp["ring"] == 32
          and tp["response"]["message"] == "ACK",
          f"transponder sends every panel key: {tp}")


def test_record_band_and_label():
    p = dict(catalog.get("record").fresh_params(), band="range",
             f_lo_hz=914e6, f_hi_hz=916e6, label="pass1")
    start, _ = step_commands(mkstep("s1", "record", 0, 3, radiates=False, params=p))
    check(start[0][1] == {"seconds": 3.0, "band": [914e6, 916e6], "label": "pass1"},
          f"record band as [f_lo, f_hi] plus label: {start[0][1]}")
    p = catalog.get("record").fresh_params()
    start, _ = step_commands(mkstep("s1", "record", 0, 3, radiates=False, params=p))
    check("band" not in start[0][1], "full band sends no band")


def test_replay_level_mode():
    start, _ = step_commands(mkstep("s1", "replay", 0, 10, params={"name": "r1"}))
    check(start[0][1]["level_mode"] == "relative", "replay gain is relative, as the panel")


def test_catalog_fields_consistent():
    for tech, e in catalog.CATALOG.items():
        keys = [f.key for f in e.fields]
        check(len(keys) == len(set(keys)), f"{tech}: no duplicate field keys")
        for f in e.fields:
            check(f.key in e.params, f"{tech}.{f.key}: default in fresh params")
            if f.kind == "select":
                check(any(v == f.default for v, _l in f.choices),
                      f"{tech}.{f.key}: default {f.default!r} is one of the choices")
        if tech.startswith(("jammer.", "tools.")):
            check(e.params.get("kind"), f"{tech}: carries its generator kind")
            secs = e.params.get("seconds")
            check(secs is not None and 0.001 <= secs <= 5.0,
                  f"{tech}: loop length {secs} within the service's 0.001..5 s")
    duty = catalog.get("jammer.barrage").field("duty")
    check(not duty.shown({"prf": 0}) and duty.shown({"prf": 100.0}),
          "pulse duty only shows while pulsing")


def test_secure_not_wired():
    start, stop = step_commands(mkstep("s1", "secure.send", 0, 2,
                                       params={"recipient": "x", "body": "hi"}))
    check(start is None and stop is None, "secure send is not driven in v1")


def test_unknown_technique_raises():
    try:
        step_commands(mkstep("s1", "jammer.deathray", 0, 10))
        check(False, "unknown technique should raise")
    except ValueError:
        check(True, "unknown technique raises ValueError")


def test_plan_events_order():
    plan = {"steps": [
        mkstep("s1", "jammer.barrage", 0, 30),
        mkstep("s2", "jammer.tone", 30, 60),      # starts when s1 stops
    ]}
    evs = plan_events(plan)
    order = [(e.kind, e.step["id"]) for e in evs]
    check(order == [("start", "s1"), ("stop", "s1"), ("start", "s2"), ("stop", "s2")],
          f"events in time order with stop before start at the seam: {order}")


def test_plan_events_skips_not_wired():
    plan = {"steps": [mkstep("s1", "secure.send", 0, 2)]}
    check(plan_events(plan) == [], "a not-wired step produces no run events")


# drive tests

def test_drive_keys_and_stops():
    host = FakeHost()
    audit = AuditLog()
    runner = MissionRunner(lambda rid: host, audit, speed=0.0)
    plan = {"name": "t", "steps": [
        mkstep("s1", "jammer.barrage", 0, 10),
        mkstep("s2", "tools.tone", 10, 20),
    ]}
    asyncio.run(runner.run(plan))
    names = host.names()
    # two starts (gen+start each) and two stops
    check(names.count("gen_test_waveform") == 2, f"two gen calls: {names}")
    check(names.count("start_tx") == 2, "two start_tx calls")
    check(names.count("stop_tx") == 2, "two stop_tx calls")
    check(runner.finished, "runner reports finished")
    check(not runner.is_running(), "runner is idle after finishing")


def test_drive_logs_each_action():
    host = FakeHost()
    audit = AuditLog()
    runner = MissionRunner(lambda rid: host, audit, speed=0.0)
    plan = {"name": "t", "steps": [mkstep("s1", "jammer.barrage", 0, 5)]}
    asyncio.run(runner.run(plan))
    text = "\n".join(audit.lines())
    check("start mission" in text, "audit records the run start")
    check("step s1 started" in text, "audit records a step start")
    check("step s1 stopped" in text, "audit records a step stop")


def test_failed_cmd_stops_the_step():
    host = FakeHost(fail={"start_tx"})
    audit = AuditLog()
    runner = MissionRunner(lambda rid: host, audit, speed=0.0)
    plan = {"name": "t", "steps": [mkstep("s1", "jammer.barrage", 0, 5)]}
    asyncio.run(runner.run(plan))
    text = "\n".join(audit.lines())
    check("FAILED" in text, "a failed start_tx is logged as FAILED")
    check("s1" not in runner.active_steps, "a failed step is not marked active")


def test_missing_radio_is_skipped_not_keyed():
    audit = AuditLog()
    runner = MissionRunner(lambda rid: None, audit, speed=0.0)   # no host
    plan = {"name": "t", "steps": [mkstep("s1", "jammer.barrage", 0, 5)]}
    asyncio.run(runner.run(plan))
    text = "\n".join(audit.lines())
    check("not connected" in text, "a step on a missing radio is skipped and logged")


def test_stop_all_sends_stop_tx_to_every_host():
    h1, h2 = FakeHost("A"), FakeHost("B")
    audit = AuditLog()
    runner = MissionRunner(lambda rid: h1, audit, speed=0.0)

    async def go():
        sent = await runner.stop_all([h1, h2])
        return sent

    sent = asyncio.run(go())
    check(sent == 2, "stop_all reaches both hosts")
    check(h1.names() == ["stop_tx"] and h2.names() == ["stop_tx"],
          "each host got stop_tx")
    check("STOP ALL TX" in "\n".join(audit.lines()), "stop_all is audited")


def test_stop_all_cancels_a_running_mission():
    host = FakeHost()
    audit = AuditLog()
    runner = MissionRunner(lambda rid: host, audit, speed=1.0)   # real time

    async def go():
        # a long step so the run is still going when the test stops it
        plan = {"name": "t", "steps": [mkstep("s1", "jammer.barrage", 0, 60)]}
        task = asyncio.create_task(runner.run(plan))
        await asyncio.sleep(0.2)            # let s1 key
        await runner.stop_all([host])
        try:
            await task
        except asyncio.CancelledError:
            pass
        return host.names()

    names = asyncio.run(go())
    check("start_tx" in names, "s1 keyed before the stop")
    check("stop_tx" in names, "stop_all sent stop_tx")
    check(not runner.is_running(), "runner is idle after stop_all")


class SlowHost(FakeHost):
    """A FakeHost whose commands take time, stamping when each one lands."""

    def __init__(self, serial, delay=0.15):
        super().__init__(serial)
        self.delay = delay
        self.stamps = []       # (name, monotonic time) as each cmd completes

    async def cmd(self, name, params=None, timeout=None):
        await asyncio.sleep(self.delay)
        self.stamps.append((name, time.monotonic()))
        return await super().cmd(name, params, timeout)


def test_same_time_starts_run_in_parallel():
    hosts = {"A": SlowHost("A"), "B": SlowHost("B")}
    runner = MissionRunner(lambda rid: hosts.get(rid), AuditLog(), speed=0.0)
    plan = {"name": "t", "steps": [
        mkstep("a1", "jammer.barrage", 0, 10, radio="A"),
        mkstep("b1", "jammer.barrage", 0, 10, radio="B"),
    ]}
    asyncio.run(runner.run(plan))
    ta = next(t for n, t in hosts["A"].stamps if n == "start_tx")
    tb = next(t for n, t in hosts["B"].stamps if n == "start_tx")
    # one after the other would put B's start_tx a whole setup (2 x 0.15 s) later
    check(abs(ta - tb) < 0.1, f"radios sharing a start time key together "
                              f"(start_tx {abs(ta - tb) * 1000:.0f} ms apart)")


def test_same_radio_order_kept_in_a_group():
    host = FakeHost("A")
    runner = MissionRunner(lambda rid: host, AuditLog(), speed=0.0)
    plan = {"name": "t", "steps": [
        mkstep("s1", "jammer.barrage", 0, 10, radio="A"),
        mkstep("s2", "jammer.tone", 10, 20, radio="A"),
    ]}
    asyncio.run(runner.run(plan))
    names = host.names()
    i_stop = names.index("stop_tx")
    i_start2 = [i for i, n in enumerate(names) if n == "start_tx"][1]
    check(i_stop < i_start2, f"on one radio the stop at 10 s goes before the "
                             f"next start at 10 s: {names}")


def test_offline_radio_skipped_rest_runs():
    hosts = {"A": FakeHost("A")}           # B is planned but not connected
    audit = AuditLog()
    runner = MissionRunner(lambda rid: hosts.get(rid), audit, speed=0.0)
    plan = {"name": "t", "steps": [
        mkstep("a1", "jammer.barrage", 0, 10, radio="A"),
        mkstep("b1", "jammer.barrage", 0, 10, radio="B"),
    ]}
    asyncio.run(runner.run(plan))
    text = "\n".join(audit.lines())
    check("start_tx" in hosts["A"].names(), "the connected radio still keys")
    check("radio B not connected; skipped" in text,
          "the offline radio's step is logged as skipped")
    check(runner.finished, "the run finishes with a radio offline")


def test_step_tuning_order_and_keys():
    p = dict(catalog.get("jammer.barrage").fresh_params(),
             center_hz=915.5e6, rx_gain_index=231, tx_atten_db=10)
    start, _ = step_commands(mkstep("s1", "jammer.barrage", 0, 10, params=p))
    names = [n for n, _ in start]
    check(names[:3] == ["set_center", "set_gain", "set_tx_atten"],
          f"center, then RX gain, then TX atten before keying: {names}")
    check(start[1][1] == {"index": 231}, f"set_gain takes the index: {start[1][1]}")
    gen = dict(start)["gen_test_waveform"]
    check("rx_gain_index" not in gen and "center_hz" not in gen,
          "tuning keys are not passed to gen_test_waveform")
    check(not any(n in ("init", "start_rx") for n in names),
          "per-step tuning never re-initializes the radio")


def test_blank_tuning_leaves_radio_alone():
    p = catalog.get("jammer.barrage").fresh_params()
    start, _ = step_commands(mkstep("s1", "jammer.barrage", 0, 10, params=p))
    names = [n for n, _ in start]
    check(not any(n in ("set_center", "set_gain", "set_tx_atten") for n in names),
          f"blank tuning sends no tuning commands: {names}")


def test_record_tuning():
    p = dict(catalog.get("record").fresh_params(), center_hz=433.92e6,
             rx_gain_index=200)
    start, _ = step_commands(mkstep("s1", "record", 0, 2, radiates=False, params=p))
    names = [n for n, _ in start]
    check(names == ["set_center", "set_gain", "record_iq"],
          f"record tunes then records: {names}")
    check(start[0][1] == {"hz": 433.92e6}, "record retunes to its center")


def test_catalog_tuning_fields():
    for tech in ("jammer.barrage", "tools.tone", "iq_playback", "transponder",
                 "replay", "record"):
        e = catalog.get(tech)
        keys = [f.key for f in e.fields]
        check("center_hz" in keys and "rx_gain_index" in keys,
              f"{tech} offers RF center and RX gain")
        check(("tx_atten_db" in keys) == e.radiates,
              f"{tech} offers TX atten only if it keys")
        check(e.fresh_params().get("rx_gain_index") is None,
              f"{tech} RX gain defaults to leave as set")
    vals = [v for v, _ in catalog.RX_GAIN_CHOICES]
    check(min(vals) == 187 and max(vals) == 255 and len(vals) == 69,
          "RX gain choices are the G40 indices 187-255")


# sample rate

class RateHost(FakeHost):
    """A fake radio that answers get_config with a config snapshot and lets a
    test see the init params and command order."""

    def __init__(self, serial="SIM", rate=30_720_000.0, inited=True, **kw):
        super().__init__(serial=serial, **kw)
        self.snap = {"inited": inited, "achieved_rate": float(rate),
                     "sample_rate": float(rate), "center_hz": 9e8,
                     "rx_gain": 200, "tx_gain": 0, "clock_source": "gpsdo",
                     "rx_antenna": "RxA1", "kind": "g40", "card": 0,
                     "chan_mode": "single"}

    async def cmd(self, name, params=None, timeout=None):
        self.calls.append((name, dict(params or {})))
        if name in self.fail:
            return FakeAck(status=1, result={"err": "forced failure"})
        if name == "get_config":
            return FakeAck(status=0, result=dict(self.snap))
        if name == "init":
            self.snap["achieved_rate"] = float(params.get("sample_rate"))
            self.snap["sample_rate"] = float(params.get("sample_rate"))
            return FakeAck(status=0, result={"achieved_rate": self.snap["achieved_rate"]})
        return FakeAck(status=0)


def test_set_rate_maps_to_internal_command():
    start, stop = step_commands(mkstep(
        "r1", "set_rate", 0, 2, radiates=False, params={"sample_rate": 7_680_000}))
    check(start == [(mr.RECONFIGURE_RATE, {"sample_rate": 7_680_000.0})],
          f"set_rate maps to the reconfigure command: {start}")
    check(stop == [], "set_rate has nothing to stop")


def test_set_rate_with_no_rate_is_not_run():
    start, _ = step_commands(mkstep("r1", "set_rate", 0, 2, radiates=False,
                                    params={"sample_rate": 0}))
    check(start is None, "a set_rate with no rate is skipped")


def test_init_params_keep_config_change_only_rate():
    snap = {"inited": True, "achieved_rate": 30.72e6, "center_hz": 9e8,
            "rx_gain": 200, "tx_gain": 0, "clock_source": "gpsdo",
            "rx_antenna": "RxA1", "tx_antenna": None, "kind": "g40",
            "card": 0, "tx_card": None, "chan_mode": "single"}
    p = mr._init_params_from_snapshot(snap, 7_680_000)
    check(p["sample_rate"] == 7_680_000.0, "rate is the new one")
    check(p["center_freq"] == 9e8 and p["rx_gain"] == 200, "center and gain kept")
    check(p["rx_antenna"] == "RxA1" and "tx_antenna" not in p,
          "a None antenna is left out, not sent as None")
    check(p["extras"] == {"card": 0, "chan_mode": "single"},
          f"extras keep card and chan_mode: {p['extras']}")


def test_set_rate_step_reinits_the_radio():
    h = RateHost(rate=30_720_000)
    runner = MissionRunner(lambda rid: h, AuditLog(), speed=0.0)
    plan = {"name": "r", "radios": [{"id": "SIM", "type": "g40"}],
            "steps": [mkstep("r1", "set_rate", 0, 2, radiates=False,
                             params={"sample_rate": 7_680_000})]}
    asyncio.run(runner.run(plan))
    names = h.names()
    check("get_config" in names and "init" in names and "start_rx" in names,
          f"set_rate reads config, re-inits and restarts RX: {names}")
    init_p = next(p for n, p in h.calls if n == "init")
    check(init_p["sample_rate"] == 7_680_000.0, "init uses the new rate")
    check(h.snap["achieved_rate"] == 7_680_000.0, "radio ends at the new rate")


def _split_host(rx, tx):
    h = RateHost(rate=rx)
    h.snap.update({"tx_card": 1, "tx_sample_rate": float(tx)})
    return h


def test_plan_rate_moves_a_tx_card_on_its_own_rate():
    h = _split_host(30_720_000, 30_720_000)
    runner = MissionRunner(lambda rid: h, AuditLog(), speed=0.0)
    plan = {"name": "r", "radios": [{"id": "SIM", "type": "g40", "sample_rate": 61_440_000}], "steps": []}
    asyncio.run(runner.run(plan))
    init_p = next(p for n, p in h.calls if n == "init")
    check(init_p["sample_rate"] == 61_440_000.0, "RX goes to the plan's rate")
    check("tx_sample_rate" not in init_p["extras"], f"TX follows it, no split kept: {init_p['extras']}")


def test_plan_rate_corrects_tx_when_rx_is_already_there():
    h = _split_host(61_440_000, 30_720_000)
    runner = MissionRunner(lambda rid: h, AuditLog(), speed=0.0)
    plan = {"name": "r", "radios": [{"id": "SIM", "type": "g40", "sample_rate": 61_440_000}], "steps": []}
    asyncio.run(runner.run(plan))
    init_p = next((p for n, p in h.calls if n == "init"), None)
    check(init_p is not None, "RX at the rate but TX not: still re-initializes")
    check(init_p and "tx_sample_rate" not in init_p["extras"], "the TX split is gone")


def test_no_plan_rate_leaves_a_tx_split_alone():
    h = _split_host(30_720_000, 1_920_000)
    runner = MissionRunner(lambda rid: h, AuditLog(), speed=0.0)
    plan = {"name": "r", "radios": [{"id": "SIM", "type": "g40"}], "steps": []}
    asyncio.run(runner.run(plan))
    check("init" not in h.names(), "a plan with no start rate does not touch the radio")


def test_reinit_keeps_rx_off_on_a_radio_brought_up_without_it():
    h = RateHost(rate=30_720_000)
    h.radio_state = RadioState.INIT_OK
    runner = MissionRunner(lambda rid: h, AuditLog(), speed=0.0)
    plan = {"name": "r", "radios": [{"id": "SIM", "type": "g40"}],
            "steps": [mkstep("r1", "set_rate", 0, 2, radiates=False,
                             params={"sample_rate": 7_680_000})]}
    asyncio.run(runner.run(plan))
    names = h.names()
    check("init" in names and "start_rx" not in names,
          f"RX was off before the re-init, so it stays off: {names}")


def test_reinit_for_rx_none_step_leaves_rx_off():
    h = RateHost(rate=7_680_000)
    h.radio_state = RadioState.STREAMING
    runner = MissionRunner(lambda rid: h, AuditLog(), speed=0.0)
    st = mkstep("t1", "tools.tone", 0, 2, params=catalog.get("tools.tone").fresh_params())
    st["ports"] = {"rx": "none", "tx": "TRx2B"}
    plan = {"name": "r", "radios": [{"id": "SIM", "type": "g40"}], "steps": [st]}
    asyncio.run(runner.run(plan))
    names = h.names()
    check("init" in names and "start_rx" not in names,
          f"a TX only step that needs a re-init does not start RX: {names}")


def test_reinit_for_receiving_step_starts_rx():
    h = RateHost(rate=7_680_000)
    h.radio_state = RadioState.INIT_OK
    runner = MissionRunner(lambda rid: h, AuditLog(), speed=0.0)
    st = mkstep("d1", "rx_dwell", 0, 2, radiates=False, params={"center_hz": 9e8})
    st["ports"] = {"rx": "TRx1B", "tx": "TRx2A"}
    plan = {"name": "r", "radios": [{"id": "SIM", "type": "g40"}], "steps": [st]}
    asyncio.run(runner.run(plan))
    names = h.names()
    check("init" in names and "start_rx" in names,
          f"a step that listens gets RX even if it was off: {names}")


def test_set_rate_noop_when_already_at_rate():
    h = RateHost(rate=7_680_000)
    runner = MissionRunner(lambda rid: h, AuditLog(), speed=0.0)
    plan = {"name": "r", "radios": [{"id": "SIM", "type": "g40"}],
            "steps": [mkstep("r1", "set_rate", 0, 2, radiates=False,
                             params={"sample_rate": 7_680_000})]}
    asyncio.run(runner.run(plan))
    check("init" not in h.names(), "no re-init when the radio is already at the rate")


def test_set_rate_skipped_when_not_initialized():
    h = RateHost(inited=False)
    runner = MissionRunner(lambda rid: h, AuditLog(), speed=0.0)
    plan = {"name": "r", "radios": [{"id": "SIM", "type": "g40"}],
            "steps": [mkstep("r1", "set_rate", 0, 2, radiates=False,
                             params={"sample_rate": 7_680_000})]}
    asyncio.run(runner.run(plan))
    check("init" not in h.names(),
          "a rate change on an uninitialized radio does not init blind")


class UpHost(RateHost):
    """A fake radio that starts not initialized and comes up on init."""

    def __init__(self, **kw):
        super().__init__(inited=False, **kw)
        self.radio_state = RadioState.UNINIT

    async def cmd(self, name, params=None, timeout=None):
        ack = await super().cmd(name, params, timeout)
        if name == "init" and ack.status == 0:
            self.snap.update(inited=True, rx_antenna=params.get("rx_antenna"),
                             tx_antenna=params.get("tx_antenna"),
                             center_hz=params.get("center_freq"),
                             **(params.get("extras") or {}))
            self.radio_state = RadioState.INIT_OK
        return ack


def _tx_step(sid="t1", start=0, stop=2, rx="none", tx="TRx2B", **params):
    s = mkstep(sid, "tools.tone", start, stop,
               params={"center_hz": 915e6, "tx_center_hz": 915e6, "tx_atten_db": 25.0, **params})
    s["ports"] = {"rx": rx, "tx": tx}
    return s


def test_bring_up_plan_tx_only():
    how = mr.bring_up_plan({"id": "SIM", "type": "g40", "sample_rate": 61_440_000},
                           [_tx_step()])
    p = how["params"]
    check(p["sample_rate"] == 61_440_000 and p["center_freq"] == 915e6, f"rate, center: {p}")
    check(p["clock_source"] == "internal", "clock defaults to internal")
    check(p["tx_gain"] == 25.0, "TX attenuation comes from the step")
    check(p["extras"]["tx_card"] == 1 and p["extras"]["card"] == 0,
          f"TX TRx2B with RX on the other card: {p['extras']}")
    check(how["rx_on"] is False, "all steps RX none leaves RX off")


def test_bring_up_plan_needs_frequency_and_tx_port():
    r = {"id": "SIM", "type": "g40"}
    check("frequency" in mr.bring_up_plan(r, [mkstep("a", "set_rate", 0, 2, radiates=False,
                                                     params={"sample_rate": 7_680_000})])["err"],
          "no frequency is refused")
    s = _tx_step()
    s.pop("ports")
    check("TX port" in mr.bring_up_plan(r, [s])["err"], "a radiating step with no TX port is refused")


def test_bring_up_plan_clock_from_row_and_rx_on():
    how = mr.bring_up_plan({"id": "SIM", "type": "g40", "clock_source": "gpsdo"},
                           [_tx_step(rx="TRx1A")])
    check(how["params"]["clock_source"] == "gpsdo", "the row's clock is used")
    check(how["params"]["sample_rate"] == mr.BRING_UP_RATE_HZ, "a blank rate takes the default")
    check(how["rx_on"] is True, "a step with an RX port starts RX")
    check("err" in mr.bring_up_plan({"id": "SIM", "clock_source": "bogus"}, [_tx_step()]),
          "an unknown clock is refused")


def test_run_brings_up_radio_before_the_timeline():
    h = UpHost()
    runner = MissionRunner(lambda rid: h, AuditLog(), speed=0.0)
    plan = {"name": "r", "radios": [{"id": "SIM", "type": "g40", "sample_rate": 61_440_000}],
            "steps": [_tx_step()]}
    asyncio.run(runner.run(plan))
    names = h.names()
    check(names.count("init") == 1, f"one init: {names}")
    check("start_rx" not in names, "RX stays off on a TX only plan")
    first_key = min(i for i, n in enumerate(names) if n not in ("get_config", "init"))
    check(names.index("init") < first_key, f"init comes before any step command: {names}")


def test_failed_bring_up_is_not_repeated():
    h = UpHost(fail={"init"})
    audit = AuditLog()
    runner = MissionRunner(lambda rid: h, audit, speed=0.0)
    plan = {"name": "r", "radios": [{"id": "SIM", "type": "g40"}],
            "steps": [_tx_step("a", 0, 1), _tx_step("b", 2, 3)]}
    asyncio.run(runner.run(plan))
    check(h.names().count("init") == 1, f"one try per run: {h.names()}")
    check("set_center" not in h.names() and "gen_test_waveform" not in h.names(),
          f"no step command goes out: {h.names()}")


def test_uninitialized_radio_with_no_timeline_steps_is_left_alone():
    h = UpHost()
    runner = MissionRunner(lambda rid: h, AuditLog(), speed=0.0)
    plan = {"name": "r", "radios": [{"id": "SIM", "type": "g40"}], "steps": []}
    asyncio.run(runner.run(plan))
    check("init" not in h.names(), "no steps, no init")


def test_run_stops_a_tx_left_running_before_the_plan():
    h = RateHost(rate=61_440_000)
    h.status = SimpleNamespace(flags=Status.FLAG_TX_ACTIVE)
    audit = AuditLog()
    runner = MissionRunner(lambda rid: h, audit, speed=0.0)
    plan = {"name": "r", "radios": [{"id": "SIM", "type": "g40"}], "steps": [_tx_step()]}
    asyncio.run(runner.run(plan))
    names = h.names()
    check("stop_tx" in names and names.index("stop_tx") < names.index("start_tx"),
          f"the old TX is stopped before the first step keys: {names}")
    check(any("already transmitting" in e[2] for e in audit.entries), "the audit log says so")


def test_run_leaves_a_radio_with_no_tx_alone():
    h = RateHost(rate=61_440_000)
    runner = MissionRunner(lambda rid: h, AuditLog(), speed=0.0)
    plan = {"name": "r", "radios": [{"id": "SIM", "type": "g40"}], "steps": [_tx_step()]}
    asyncio.run(runner.run(plan))
    names = h.names()
    check(names.index("start_tx") < names.index("stop_tx"), f"only the step's own stop: {names}")


def test_location_task_stops_a_tx_left_running():
    h = RateHost(rate=61_440_000)
    h.status = SimpleNamespace(flags=Status.FLAG_TX_ACTIVE)
    runner = MissionRunner(lambda rid: h, AuditLog(), speed=0.0)
    runner.plan_source = lambda: {"radios": [{"id": "SIM", "type": "g40"}], "steps": []}
    async def go():
        await runner.start_step_now(_tx_step(), dur=0.01)
    asyncio.run(go())
    names = h.names()
    check(names.index("stop_tx") < names.index("start_tx"), f"stopped before keying: {names}")


def test_failed_bring_up_ends_the_run_with_the_reason():
    h = UpHost(fail={"init"})
    audit = AuditLog()
    runner = MissionRunner(lambda rid: h, audit, speed=0.0)
    plan = {"name": "r", "radios": [{"id": "SIM", "type": "g40"}],
            "steps": [_tx_step("a", 0, 1), _tx_step("b", 2, 3)]}
    asyncio.run(runner.run(plan))
    lines = [e[2] for e in audit.entries]
    check(not runner.running and runner.finished is False, "the run is over and not finished")
    check("forced failure" in (runner.run_error or ""), f"the reason is kept: {runner.run_error}")
    check(any("mission ended early" in l for l in lines), f"the audit log says so: {lines}")
    check(not any("mission finished" in l for l in lines), "it does not say finished")
    check(not any(" stopped" in l for l in lines), f"no stop for a step that never ran: {lines}")
    check("stop_tx" not in h.names(), f"nothing to stop: {h.names()}")


def test_next_run_clears_the_failure():
    h = UpHost(fail={"init"})
    runner = MissionRunner(lambda rid: h, AuditLog(), speed=0.0)
    plan = {"name": "r", "radios": [{"id": "SIM", "type": "g40"}], "steps": [_tx_step()]}
    asyncio.run(runner.run(plan))
    check(runner.run_error, "failed the first time")
    h.fail = set()
    asyncio.run(runner.run(plan))
    check(runner.run_error is None and runner.finished, "clean on the next run")


def test_other_radio_keeps_running_when_one_cannot_be_brought_up():
    bad, good = UpHost(serial="BAD", fail={"init"}), RateHost(serial="GOOD", rate=61_440_000)
    hosts = {"BAD": bad, "GOOD": good}
    runner = MissionRunner(lambda rid: hosts.get(rid), AuditLog(), speed=0.0)
    s1, s2 = _tx_step("a"), _tx_step("b")
    s1["radio"], s2["radio"] = "BAD", "GOOD"
    plan = {"name": "r", "radios": [{"id": "BAD", "type": "g40"}, {"id": "GOOD", "type": "g40"}],
            "steps": [s1, s2]}
    asyncio.run(runner.run(plan))
    check("start_tx" in good.names(), f"the good radio ran: {good.names()}")
    check("start_tx" not in bad.names(), f"the bad radio did not: {bad.names()}")
    check(runner.finished and "BAD" in (runner.run_error or ""), f"named: {runner.run_error}")


def test_plan_with_no_frequency_marks_the_radio_dead_in_a_run():
    h = UpHost()
    runner = MissionRunner(lambda rid: h, AuditLog(), speed=0.0)
    plan = {"name": "r", "radios": [{"id": "SIM", "type": "g40"}],
            "steps": [mkstep("r1", "set_rate", 0, 2, radiates=False, params={"sample_rate": 7_680_000})]}
    asyncio.run(runner.run(plan))
    check("frequency" in (runner.run_error or ""), f"reason: {runner.run_error}")
    check("init" not in h.names(), "no init without a frequency")


def test_location_task_brings_up_then_keys():
    h = UpHost()
    runner = MissionRunner(lambda rid: h, AuditLog(), speed=0.0)
    runner.plan_source = lambda: {"radios": [{"id": "SIM", "type": "g40"}], "steps": []}
    asyncio.run(runner._start_step(_tx_step()))
    names = h.names()
    check(names.count("init") == 1, f"one init: {names}")
    check("t1" in runner.active_steps, f"the step keyed after the init: {names}")
    check(names.index("init") < names.index("start_tx"), f"init first: {names}")


def test_stop_all_during_bring_up_does_not_key():
    class SlowUp(UpHost):
        runner = None

        async def cmd(self, name, params=None, timeout=None):
            ack = await super().cmd(name, params, timeout)
            if name == "init":
                await self.runner.stop_all([])
            return ack
    h = SlowUp()
    audit = AuditLog()
    runner = SlowUp.runner = MissionRunner(lambda rid: h, audit, speed=0.0)
    runner.plan_source = lambda: {"radios": [{"id": "SIM", "type": "g40"}], "steps": []}
    asyncio.run(runner._start_step(_tx_step()))
    check("t1" not in runner.active_steps, "the step is not active")
    check(not any(n in h.names() for n in ("set_center", "tx_tone", "gen_test_waveform")),
          f"nothing keys after STOP ALL: {h.names()}")


def test_per_radio_start_rate_applied_before_timeline():
    h = RateHost(rate=30_720_000)
    audit = AuditLog()
    runner = MissionRunner(lambda rid: h, audit, speed=0.0)
    plan = {"name": "r",
            "radios": [{"id": "SIM", "type": "g40", "sample_rate": 7_680_000}],
            "steps": [mkstep("s1", "jammer.barrage", 0, 5)]}
    asyncio.run(runner.run(plan))
    names = h.names()
    # the init for the start rate comes before the first gen_test_waveform
    check(names.index("init") < names.index("gen_test_waveform"),
          f"start rate is set before the first keyed step: {names}")
    init_p = next(p for n, p in h.calls if n == "init")
    check(init_p["sample_rate"] == 7_680_000.0, "start rate re-inits to 7.68")


def test_effective_rate_follows_start_rate_then_changes():
    plan = {"radios": [{"id": "SIM", "type": "g40", "sample_rate": 30_720_000}],
            "steps": [mkstep("c", "set_rate", 100, 102, radiates=False,
                             params={"sample_rate": 7_680_000})]}
    check(mr.effective_rate_at(plan, "SIM", 50) == 30_720_000.0,
          "before the change, the start rate holds")
    check(mr.effective_rate_at(plan, "SIM", 150) == 7_680_000.0,
          "after the change, the new rate holds")
    check(mr.effective_rate_at(plan, "SIM", 100) == 7_680_000.0,
          "at the change instant, the new rate holds")


def test_set_rate_catalog_entry():
    e = catalog.get("set_rate")
    check(e is not None and not e.radiates, "set_rate is in the catalog and does not key")
    check(e.needs == (), "set_rate needs no capability tag")
    check([f.key for f in e.fields] == ["sample_rate"], "set_rate has one field, the rate")


def test_tx_frequency_after_center_before_keying():
    start, _ = step_commands(mkstep("s1", "jammer.barrage", 0, 10, params={
        "center_hz": 1.0e9, "tx_center_hz": 1.2e9, "tx_atten_db": 40}))
    names = [n for n, _ in start]
    check(names[:3] == ["set_center", "set_tx_center", "set_tx_atten"],
          f"set_center then set_tx_center then atten: {names}")
    check(dict(start)["set_tx_center"] == {"hz": 1.2e9}, "TX frequency in Hz")
    gen = dict(start)["gen_test_waveform"]
    check("tx_center_hz" not in gen, "TX frequency not passed to the generator")
    start, _ = step_commands(mkstep("s2", "jammer.barrage", 0, 10,
                                    params={"tx_center_hz": None}))
    check("set_tx_center" not in [n for n, _ in start], "blank TX frequency sends nothing")


class _CfgHost(FakeHost):
    """FakeHost whose get_config reports an RX center."""

    def __init__(self, center_hz):
        super().__init__()
        self.center_hz = center_hz

    async def cmd(self, name, params=None, timeout=None):
        if name == "get_config":
            self.calls.append((name, {}))
            return FakeAck(0, {"center_hz": self.center_hz})
        return await super().cmd(name, params, timeout)


def test_blank_tx_after_a_moved_tx_goes_back_to_rx():
    host = _CfgHost(1.0e9)
    runner = MissionRunner(lambda rid: host, AuditLog(), speed=0.0)
    plan = {"name": "t", "steps": [
        mkstep("s1", "jammer.barrage", 0, 10, params={"tx_center_hz": 1.2e9}),
        mkstep("s2", "tools.tone", 10, 20),                       # blank: back to RX
        mkstep("s3", "tools.tone", 20, 30),                       # already back: nothing
        mkstep("s4", "jammer.barrage", 30, 40, params={"tx_center_hz": 1.1e9}),
        mkstep("s5", "jammer.barrage", 40, 50, params={"center_hz": 9.0e8}),
    ]}
    asyncio.run(runner.run(plan))
    tx = [p["hz"] for n, p in host.calls if n == "set_tx_center"]
    check(tx == [1.2e9, 1.0e9, 1.1e9, 9.0e8],
          f"TX moved, back to RX (get_config), moved, back to the new RF center: {tx}")
    check(host.names().count("get_config") == 1,
          "get_config only when the RF center is blank")
    i_tx = [i for i, (n, _) in enumerate(host.calls) if n == "set_tx_center"][-1]
    check(host.calls[i_tx - 1][0] == "set_center", "back-to-RX after the step's set_center")


def test_blank_tx_never_moved_sends_nothing():
    host = _CfgHost(1.0e9)
    runner = MissionRunner(lambda rid: host, AuditLog(), speed=0.0)
    asyncio.run(runner.run({"name": "t", "steps": [
        mkstep("s1", "jammer.barrage", 0, 10), mkstep("s2", "tools.tone", 10, 20)]}))
    check("set_tx_center" not in host.names() and "get_config" not in host.names(),
          f"a radio's own TX setting is left alone: {host.names()}")


def test_tx_frequency_field_on_radiating_steps_only():
    for e in catalog.CATALOG.values():
        keys = [f.key for f in e.fields]
        if "center_hz" not in keys:
            continue
        check(("tx_center_hz" in keys) == bool(e.radiates),
              f"{e.technique}: TX frequency field iff it radiates")
        if e.group in ("Jammers", "Tools generators"):
            check(keys.index("center_hz") == 0, f"{e.technique}: RF center first")
    check(not catalog.get("set_rate").radiates, "set_rate does not radiate")
    check("whole band" in catalog.get("jammer.barrage").hint, "barrage hint")


def test_start_step_now_keys_then_stops_after_its_length():
    host = FakeHost()
    changes = []
    runner = MissionRunner(lambda rid: host, AuditLog(), speed=0.0,
                           on_change=lambda: changes.append(1))
    step = mkstep("s1", "jammer.barrage", 0, 10, params={"tx_atten_db": 40})

    async def go():
        ok = await runner.start_step_now(step)
        check(ok == "started" and "s1" in runner.active_steps, "started and active")
        check(not runner.running, "a single step does not take the timeline over")
        await asyncio.sleep(0.05)                      # speed 0: the auto-stop fires at once
        return ok
    asyncio.run(go())
    names = host.names()
    check(names == ["set_tx_atten", "gen_test_waveform", "start_tx", "stop_tx"],
          f"tuning, key, then the auto stop: {names}")
    check("s1" not in runner.active_steps and changes, "stopped and the page was told")


def test_stop_step_now_and_stop_all_cancel_the_auto_stop():
    host = FakeHost()
    runner = MissionRunner(lambda rid: host, AuditLog(), speed=1.0)
    s1 = mkstep("s1", "jammer.barrage", 0, 600)
    s2 = mkstep("s2", "tools.tone", 0, 600, radio="SIM-B")    # its own radio

    async def go():
        await runner.start_step_now(s1)
        await runner.start_step_now(s2)
        check(len(runner._now_tasks) == 2, "two auto-stops pending")
        check(await runner.stop_step_now(s1), "stop_step_now stops a running step")
        check("s1" not in runner.active_steps and "s2" in runner.active_steps, "only s1 stopped")
        check(not await runner.stop_step_now(s1), "stopping it again is a no-op")
        check(await runner.start_step_now(s2) == "running", "starting a running step again is refused")
        await runner.stop_all([host])
        await asyncio.sleep(0)
        check(runner.active_steps == set() and runner._now_tasks == {},
              "STOP ALL clears the steps and their auto-stops")
    asyncio.run(go())
    check(host.names().count("stop_tx") == 2, f"s1 stop then STOP ALL: {host.names()}")


def test_start_step_now_offline_radio_does_not_start():
    runner = MissionRunner(lambda rid: None, AuditLog(), speed=0.0)
    step = mkstep("s1", "jammer.barrage", 0, 10)
    ok = asyncio.run(runner.start_step_now(step))
    check(ok == "failed" and not runner.active_steps, "offline radio: not started")


def mktask(sid, tech, mode="while_inside", priority=3, dwell_s=60, max_s=600, radio="SIM"):
    from dpmg.mission import mission_file as mf
    return mf.new_location_task(sid, tech, radio, {"lat": 33.45, "lon": -112.07, "radius_m": 500},
                                mode=mode, dwell_s=dwell_s, max_s=max_s, priority=priority)


def test_location_task_takes_the_radio_from_a_lower_priority_step():
    host = FakeHost()
    runner = MissionRunner(lambda rid: host, AuditLog(), speed=1.0)
    tl = mkstep("t1", "tools.tone", 0, 600)                     # priority 5
    task = mktask("L1", "jammer.barrage", priority=3)

    async def go():
        await runner.start_step_now(tl)
        r = await runner.start_step_now(task)
        check(r == "started" and runner.active_steps == {"L1"}, f"task pre-empts the step: {r}")
    asyncio.run(go())
    names = host.names()
    check(names.index("stop_tx") < len(names) - 1 and names.count("start_tx") == 2,
          f"the step was stopped before the task keyed: {names}")
    check(any("takes radio" in e[2] for e in runner.audit.entries), "audit says who took the radio")


def test_planned_stop_of_a_taken_step_leaves_the_location_task_keyed():
    host = FakeHost()
    runner = MissionRunner(lambda rid: host, AuditLog(), speed=1.0)
    timed = mkstep("t1", "tools.tone", 0, 0.4)                 # priority 5
    task = mktask("L1", "jammer.barrage", mode="dwell", dwell_s=60, priority=3)
    plan = {"name": "t", "steps": [timed]}

    async def go():
        run = asyncio.ensure_future(runner.run(plan))
        await asyncio.sleep(0.15)
        check("t1" in runner.active_steps, "the timed step is keyed")
        r = await runner.start_step_now(task)
        check(r == "started", f"the task takes the radio: {r}")
        n = host.names().count("stop_tx")
        await run
        check("L1" in runner.active_steps, "the task is still keyed after the timed step's planned stop")
        check(host.names().count("stop_tx") == n, f"the planned stop sent nothing: {host.names()}")
        await runner.stop_step_now(task)
    asyncio.run(go())


def test_location_task_waits_for_a_higher_priority_step_then_starts():
    host = FakeHost()
    runner = MissionRunner(lambda rid: host, AuditLog(), speed=1.0)
    tl = mkstep("t1", "tools.tone", 0, 600, params={})
    tl["priority"] = 2
    task = mktask("L1", "jammer.barrage", priority=3)
    still_inside = {"v": True}

    async def go():
        await runner.start_step_now(tl)
        r = await runner.start_step_now(task, want=lambda: still_inside["v"])
        check(r == "waiting" and runner.waiting_on("SIM") is task, f"task waits: {r}")
        check(runner.active_steps == {"t1"}, "the step keeps the radio")
        await runner.stop_step_now(tl)
        await asyncio.sleep(0.05)
        check(runner.active_steps == {"L1"}, "the waiting task starts when the radio frees")
        await runner.stop_step_now(task)
    asyncio.run(go())


def test_waiting_task_dropped_when_its_condition_ends():
    host = FakeHost()
    runner = MissionRunner(lambda rid: host, AuditLog(), speed=1.0)
    tl = mkstep("t1", "tools.tone", 0, 600)
    tl["priority"] = 1
    task = mktask("L1", "jammer.barrage", priority=3)
    inside = {"v": True}

    async def go():
        await runner.start_step_now(tl)
        await runner.start_step_now(task, want=lambda: inside["v"])
        inside["v"] = False                                     # the platform left
        await runner.stop_step_now(tl)
        await asyncio.sleep(0.05)
        check(runner.active_steps == set() and runner.waiting_on("SIM") is None,
              "dropped, nothing keyed")
        check(any("no longer holds" in e[2] for e in runner.audit.entries), "audit says why")
    asyncio.run(go())


def test_timeline_step_versus_a_keyed_location_task():
    host = FakeHost()
    runner = MissionRunner(lambda rid: host, AuditLog(), speed=1.0)
    task = mktask("L1", "jammer.barrage", priority=3)
    low = mkstep("t1", "tools.tone", 0, 600)                     # priority 5: skipped
    high = mkstep("t2", "tools.tone", 0, 600)
    high["priority"] = 1                                         # takes the radio

    async def go():
        await runner.start_step_now(task)
        await runner._start_timeline_step(low)
        check(runner.active_steps == {"L1"}, "lower-priority timeline step is skipped")
        check(any("skipped: radio SIM busy with location task" in e[2]
                  for e in runner.audit.entries), "audit says it was skipped")
        await runner._start_timeline_step(high)
        check(runner.active_steps == {"t2"}, "higher-priority timeline step takes the radio")
        await runner.stop_step_now(high)
    asyncio.run(go())


def test_location_task_length_and_plan_events_skip_it():
    from dpmg.mission import mission_file as mf
    dwell = mktask("L1", "jammer.barrage", mode="dwell", dwell_s=45)
    hold = mktask("L2", "jammer.barrage", mode="while_inside", max_s=300)
    check(mf.task_length_s(dwell) == 45 and mf.task_length_s(hold) == 300, "lengths")
    plan = {"steps": [dwell, hold, mkstep("t1", "tools.tone", 0, 10)]}
    ev = plan_events(plan)
    check([e.step["id"] for e in ev] == ["t1", "t1"], "the timeline ignores location tasks")
    host = FakeHost()
    runner = MissionRunner(lambda rid: host, AuditLog(), speed=0.0)

    async def go():
        r = await runner.start_step_now(dwell)
        await asyncio.sleep(0.05)
        return r
    r = asyncio.run(go())
    check(r == "started" and "L1" not in runner.active_steps, "a dwell task stops by itself")


def test_timeline_run_respects_a_keyed_location_task():
    host = FakeHost()
    # real time: at speed 0 the task's auto-stop fires at once, which is not the point here
    runner = MissionRunner(lambda rid: host, AuditLog(), speed=1.0)
    task = mktask("L1", "jammer.barrage", priority=3)
    plan = {"name": "t", "steps": [task, mkstep("t1", "tools.tone", 0, 0.2)]}

    async def go():
        await runner.start_step_now(task)
        await runner.run(plan)
        check("L1" in runner.active_steps, "the task is still keyed after the run")
        await runner.stop_step_now(task)
    asyncio.run(go())
    check(host.names().count("start_tx") == 1, f"the timeline step never keyed: {host.names()}")
    check(any("skipped: radio SIM busy with location task L1" in e[2]
              for e in runner.audit.entries), "audit: skipped for the task")


def test_step_ports_reinit_the_radio_when_they_differ():
    h = RateHost(rate=30_720_000)
    h.snap.update({"rx_antenna": "RXA1", "card": 0, "tx_antenna": "TXA1", "tx_card": 0})
    runner = MissionRunner(lambda rid: h, AuditLog(), speed=0.0)
    s1 = mkstep("p1", "tools.tone", 0, 1)                 # no ports: radio as it is
    s2 = mkstep("p2", "tools.tone", 2, 3)
    s2["ports"] = {"rx": "TRx1B", "tx": "TRx1A"}          # RX moves to card B
    s3 = mkstep("p3", "tools.tone", 4, 5)
    s3["ports"] = {"rx": "TRx1B", "tx": "TRx1A"}          # same again: nothing to do
    plan = {"name": "p", "radios": [{"id": "SIM", "type": "g40"}], "steps": [s1, s2, s3]}

    async def go():
        await runner._start_step(s1)
        n1 = h.names().count("init")
        await runner._start_step(s2)
        n2 = h.names().count("init")
        h.snap.update({"rx_antenna": "RXA1", "card": 1, "tx_antenna": "TXA1", "tx_card": 0})
        await runner._start_step(s3)
        n3 = h.names().count("init")
        return n1, n2, n3
    n1, n2, n3 = asyncio.run(go())
    check(n1 == 0, "a step without ports does not touch the init")
    check(n2 == 1, f"a step on other ports re-inits once: {n2}")
    check(n3 == 1, "a step already on its ports does not re-init")
    init_p = next(p for n, p in h.calls if n == "init")
    check(init_p["rx_antenna"] == "RXA1" and init_p["tx_antenna"] == "TXA1"
          and init_p["extras"]["card"] == 1 and init_p["extras"]["tx_card"] == 0
          and init_p["extras"]["chan_mode"] == "single"
          and init_p["sample_rate"] == 30_720_000.0,
          f"init carries the new ports and keeps the rate: {init_p}")
    names = h.names()
    gens = [i for i, n in enumerate(names) if n == "gen_test_waveform"]
    check(gens[0] < names.index("init") < gens[1],
          "the re-init sits between the first step and the second step's own commands")
    check(any("ports RX TRx1A TX TRx1A -> RX TRx1B TX TRx1A" in e[2] for e in runner.audit.entries),
          "audit says which ports the radio moved between")


def test_port_move_happens_in_the_gap_before_the_step():
    # a prepare event sits ahead of a step on other ports: lead time before the
    # start, never before the previous step on the radio has stopped
    s1 = mkstep("a", "tools.tone", 0, 10)
    s1["ports"] = {"rx": "TRx1B", "tx": "TRx1A"}
    s2 = mkstep("b", "tools.tone", 60, 80)
    s2["ports"] = {"rx": "TRx2B"}
    s3 = mkstep("c", "tools.tone", 85, 90)
    s3["ports"] = {"rx": "TRx1B", "tx": "TRx1A"}
    plan = {"name": "p", "radios": [{"id": "SIM", "type": "g40"}], "steps": [s1, s2, s3]}
    ev = [(e.t_s, e.kind, e.step["id"]) for e in mr.plan_events(plan)]
    preps = [(t, sid) for t, k, sid in ev if k == "prepare"]
    check((10.0, "b") in preps, f"b is prepared as soon as a stops, inside its 55 s lead: {preps}")
    check((80.0, "c") in preps, f"c is prepared as soon as b stops, not before: {preps}")
    check(not any(sid == "a" for _, sid in preps), "a step at t=0 has no room ahead of it")
    i_stop_b = ev.index((80.0, "stop", "b"))
    i_prep_c = ev.index((80.0, "prepare", "c"))
    check(i_stop_b < i_prep_c, "at one instant the stop comes before the prepare")

    # live: the re-init runs at the prepare event, so the step itself starts at
    # once on the new ports, with no init of its own
    h = RateHost(rate=30_720_000)
    h.snap.update({"rx_antenna": "RXA1", "card": 1, "tx_antenna": "TXA1", "tx_card": 0})
    runner = MissionRunner(lambda rid: h, AuditLog(), speed=0.0)
    asyncio.run(runner._prepare_ports(s2))
    n_init = h.names().count("init")
    h.snap.update({"rx_antenna": "RXA2", "card": 1})
    asyncio.run(runner._start_step(s2))
    check(n_init == 1 and h.names().count("init") == 1,
          f"one init, at the prepare, none at the start: {h.names()}")
    check(any("ahead of its start" in e[2] and "re-init ports" in e[2]
              for e in runner.audit.entries), "audit says the move was ahead of the start")


def test_stop_reaches_a_stale_radio():
    # the radio drops out (re-initialized by someone else) between a step's
    # start and stop: get_host says None, but the stop must still be sent and
    # retried until it lands, never just logged as stopped
    h = FakeHost()
    stale = {"v": False}
    runner = MissionRunner(lambda rid: None if stale["v"] else h, AuditLog(), speed=0.0,
                           get_any_host=lambda rid: h)
    runner.STOP_RETRY_S = 5.0
    step = mkstep("j1", "jammer.barrage", 0, 1)

    async def go():
        await runner._start_step(step)
        stale["v"] = True
        await runner._stop_step(step)
        await asyncio.sleep(0.2)            # the retry task runs
    asyncio.run(go())
    check(h.names().count("stop_tx") >= 1, f"stop_tx went to the stale radio: {h.names()}")
    check(any("sending the stop anyway" in e[2] for e in runner.audit.entries),
          "audit says the radio was not answering")
    check(any("stopped on SIM (after" in e[2] for e in runner.audit.entries),
          "audit confirms the stop landed")


def test_record_step_marks_the_radio_while_it_records():
    # the console's REC badge comes from rec_state: set while record_iq is in
    # flight, cleared when its ack comes back
    from dpmg import rec_state
    seen = {}

    class RecHost(FakeHost):
        async def cmd(self, name, params=None, timeout=None):
            if name == "record_iq":
                seen["during"] = rec_state.active("SIM")
                seen["text"] = rec_state.text("SIM")
            return await super().cmd(name, params, timeout)
    h = RecHost()
    runner = MissionRunner(lambda rid: h, AuditLog(), speed=0.0)
    step = mkstep("r1", "record", 0, 30, radiates=False, params={"label": "demo1"})
    asyncio.run(runner._start_step(step))
    check(seen.get("during") is not None and seen["during"]["label"] == "demo1",
          f"radio marked as recording during the capture: {seen}")
    check(seen.get("text", "").startswith("REC demo1"), f"console text: {seen.get('text')}")
    check(rec_state.active("SIM") is None, "mark cleared once the record ack is back")


def test_auto_stop_frees_the_radio_for_a_waiting_task():
    # a dwell task's planned length runs out while another task waits for the
    # radio: the stop must finish (stop_tx acked, "stopped on" logged) and the
    # waiting task must start, not be lost to the auto-stop cancelling itself.
    # The host waits a moment for each ack like a real link: a cancel only
    # lands at a suspension point, and an instant fake never suspends
    class SlowHost(FakeHost):
        async def cmd(self, name, params=None, timeout=None):
            await asyncio.sleep(0.01)
            return await super().cmd(name, params, timeout)
    h = SlowHost()
    runner = MissionRunner(lambda rid: h, AuditLog(), speed=1.0)
    tone = mktask("tone", "tools.tone", priority=3)
    noise = mktask("noise", "jammer.noise", priority=3)

    async def go():
        await runner.start_step_now(tone, dur=0.2)
        r = await runner.start_step_now(noise, dur=0.2, want=lambda: True)
        check(r == "waiting", f"the second task waits for the radio: {r}")
        await asyncio.sleep(0.6)          # tone's auto-stop fires, then noise runs
        await runner.stop_step_now(noise)
    asyncio.run(go())
    lines = [e[2] for e in runner.audit.entries]
    check(any("step tone stopped on SIM" in l for l in lines),
          f"the tone's stop completed: {lines}")
    check(any("starting the waiting location task noise" in l for l in lines),
          "the waiting task was started when the radio went free")
    check(h.names().count("start_tx") == 2, f"both tasks keyed: {h.names()}")


class HungHost(FakeHost):
    """Never answers stop_tx inside the timeout."""

    async def cmd(self, name, params=None, timeout=None):
        self.calls.append((name, dict(params or {})))
        await asyncio.sleep(timeout or 5.0)
        raise TimeoutError("no answer")


def test_stop_all_dead_radio_holds_up_no_other():
    dead, live = HungHost("DEAD"), FakeHost("LIVE")
    runner = MissionRunner(lambda rid: live, AuditLog(), speed=0.0)
    landed = {}

    async def go():
        real = live.cmd

        async def stamp(name, params=None, timeout=None):
            landed[name] = time.monotonic()
            return await real(name, params, timeout)
        live.cmd = stamp
        t0 = time.monotonic()
        sent = await runner.stop_all([dead, live])
        return sent, landed["stop_tx"] - t0

    sent, took = asyncio.run(go())
    check(sent == 1, "the count is the radios that answered")
    check(took < 0.5, f"the live radio got stop_tx at once, not after the dead one: {took:.2f} s")
    check("did not answer" in "\n".join(runner.audit.lines()), "the silent radio is audited")


def test_stop_all_closes_decode_step_attenuator():
    host = FakeHost()
    runner = MissionRunner(lambda rid: host, AuditLog(), speed=0.0)
    step = mkstep("d1", "scenario_decode", 0, 60, radiates=False,
                  params={"spec_path": "x.json", "rx_atten_db": 20})

    async def go():
        real = host.cmd

        async def with_plan(name, params=None, timeout=None):
            ack = await real(name, params, timeout)
            if name == "gen_scenario":
                ack.result = {"plan_path": "/tmp/p.json"}
            return ack
        host.cmd = with_plan
        await runner._start_step(step)
        check(("atten_set", {"db": 20.0}) in host.calls, "the step opened the attenuator")
        await runner.stop_all([host])

    asyncio.run(go())
    check(host.calls[-1] == ("atten_set", {"db": 120.0}), "STOP ALL TX set it back to 120 dB")
    check(not runner._rx_held, "nothing left held")


def test_run_total_is_fixed_when_the_run_starts():
    host = FakeHost()
    runner = MissionRunner(lambda rid: host, AuditLog(), speed=1.0)

    async def go():
        plan = {"name": "t", "steps": [mkstep("s1", "jammer.barrage", 0, 60),
                                       mkstep("s2", "jammer.barrage", 70, 90)]}
        task = asyncio.create_task(runner.run(plan))
        await asyncio.sleep(0.2)
        plan["steps"].clear()               # Delete all steps during the run
        total = runner.run_stop_s
        await runner.stop_all([host])
        try:
            await task
        except asyncio.CancelledError:
            pass
        return total

    check(asyncio.run(go()) == 90.0, "run total stays at the plan's last stop after the steps are deleted")


def test_stop_all_blocks_a_start_in_progress():
    class Slow(FakeHost):
        async def cmd(self, name, params=None, timeout=None):
            if name == "gen_test_waveform":
                await asyncio.sleep(0.3)
            return await super().cmd(name, params, timeout)
    host = Slow()
    runner = MissionRunner(lambda rid: host, AuditLog(), speed=0.0)

    async def go():
        starting = asyncio.create_task(runner.start_step_now(mkstep("t1", "jammer.barrage", 0, 60)))
        await asyncio.sleep(0.1)
        await runner.stop_all([host])
        return await starting

    r = asyncio.run(go())
    check("start_tx" not in host.names(), "start_tx never went out after STOP ALL TX")
    check(r == "failed", f"the start reports failed: {r}")
    check(not runner.active_steps, "nothing is marked keyed")


def test_step_skipped_for_a_location_task_sends_no_stop():
    host = FakeHost()
    runner = MissionRunner(lambda rid: host, AuditLog(), speed=0.0)
    task = mkstep("loc", "jammer.barrage", 0, 60)
    task["trigger"] = {"mode": "dwell", "confirm": "auto"}
    task["priority"] = 1
    timed = mkstep("t1", "jammer.barrage", 0, 10)
    timed["priority"] = 5

    async def go():
        runner.active_steps.add("loc")
        runner._active["loc"] = task
        await runner._start_timeline_step(timed)
        host.calls.clear()
        await runner._stop_step(timed)

    asyncio.run(go())
    check("stop_tx" not in host.names(), "its planned stop did not stop the location task")
    check("loc" in runner.active_steps, "the location task is still keyed")


# waveform built ahead of the start

class SlowBuildHost(FakeHost):
    """A fake radio whose waveform build takes `build_s`, and which notes the
    time of each command."""

    def __init__(self, build_s=0.3, **kw):
        super().__init__(**kw)
        self.build_s = build_s
        self.times = []        # (monotonic time, name)

    async def cmd(self, name, params=None, timeout=None):
        if name in ("gen_test_waveform", "gen_scenario"):
            await asyncio.sleep(self.build_s)
        self.times.append((time.monotonic(), name))
        return await super().cmd(name, params, timeout)


def test_build_events_go_in_the_gap_before_a_step():
    plan = {"name": "t", "steps": [mkstep("a", "jammer.barrage", 0, 10),
                                   mkstep("b", "jammer.tone", 16, 26),
                                   mkstep("c", "jammer.tone", 60, 70),
                                   mkstep("rec", "record", 80, 90, radiates=False)]}
    ev = [(e.kind, e.step["id"], e.t_s) for e in plan_events(plan)]
    builds = [x for x in ev if x[0] == "build"]
    check(not any(x[1] == "a" for x in builds), f"a step at 0 s has no gap, it builds at its start: {builds}")
    check(("build", "b", 10.0) in builds, f"a step 6 s after the last builds when that one stops: {builds}")
    check(("build", "c", 60.0 - mr.BUILD_LEAD_S) in builds,
          f"a step after a long gap builds BUILD_LEAD_S ahead: {builds}")
    check(not any(x[1] == "rec" for x in builds), "a record step builds nothing ahead")
    i_stop = ev.index(("stop", "a", 10.0))
    i_build = ev.index(("build", "b", 10.0))
    check(i_stop < i_build, "the build comes after the step before it has stopped")


def test_decode_and_rate_steps_are_not_built_ahead():
    dec = mkstep("d", "scenario_decode", 0, 60, radiates=False, params={"spec_path": "x.json"})
    rate = mkstep("r", "set_rate", 0, 1, radiates=False, params={"sample_rate": 7_680_000})
    play = mkstep("p", "iq_playback", 0, 10, params={"path": "a.cdif"})
    check(not mr.builds_ahead(dec), "a Decode step keys nothing, nothing to build ahead")
    check(not mr.builds_ahead(rate), "a rate change is not built ahead")
    check(mr.builds_ahead(play), "a file playback loads its file ahead")


def test_step_keys_on_time_with_its_waveform_built_ahead():
    host = SlowBuildHost(build_s=0.3)
    runner = MissionRunner(lambda rid: host, AuditLog(), speed=1.0)
    plan = {"name": "t", "steps": [mkstep("a", "jammer.barrage", 0.0, 0.3),
                                   mkstep("b", "jammer.tone", 0.9, 1.2)]}

    async def go():
        t0 = time.monotonic()
        await runner.run(plan)
        return t0

    t0 = asyncio.run(go())
    starts = [t - t0 for t, n in host.times if n == "start_tx"]
    gens = [t - t0 for t, n in host.times if n == "gen_test_waveform"]
    check(len(starts) == 2 and len(gens) == 2, f"two builds and two keys: {host.names()}")
    # run_t0 is set after the start rates; allow the command delay of the fake
    check(abs(starts[1] - 0.9) < 0.15, f"step b keyed on time, not a build late: {starts[1]:.2f} s")
    check(gens[1] < 0.9, f"its waveform was built before its start: {gens[1]:.2f} s")
    check(host.names().count("gen_test_waveform") == 2, "each waveform built once")
    check(any("built ahead" in e[2] for e in runner.audit.entries), "the audit log says so")


def test_build_ahead_redone_when_the_radio_was_set_up_meanwhile():
    host = FakeHost()
    runner = MissionRunner(lambda rid: host, AuditLog(), speed=0.0)
    step = mkstep("b", "jammer.tone", 10, 20)
    other = mkstep("x", "jammer.barrage", 0, 5)

    async def go():
        runner._build_ahead(step)
        await runner._wait_build("b")
        check(host.names() == ["gen_test_waveform"], f"built ahead, not keyed: {host.names()}")
        await runner._start_step(other)          # another step loads its waveform
        await runner._stop_step(other)
        check("b" in runner._built, "the built waveform is still on record")
        host.calls.clear()
        await runner._start_step(step)

    asyncio.run(go())
    check(host.names() == ["gen_test_waveform", "start_tx"],
          f"the step builds its own waveform again before it keys: {host.names()}")


def test_stop_all_drops_a_waveform_built_ahead():
    host = FakeHost()
    runner = MissionRunner(lambda rid: host, AuditLog(), speed=0.0)
    step = mkstep("b", "jammer.tone", 10, 20)

    async def go():
        runner._build_ahead(step)
        await runner._wait_build("b")
        await runner.stop_all([host])
        host.calls.clear()
        await runner._start_step(step)

    asyncio.run(go())
    check(host.names() == ["gen_test_waveform", "start_tx"],
          f"after STOP ALL TX the step builds again: {host.names()}")


def test_build_ahead_skipped_while_a_step_holds_the_radio():
    host = FakeHost()
    runner = MissionRunner(lambda rid: host, AuditLog(), speed=0.0)
    task = mkstep("loc", "jammer.barrage", 0, 60)
    step = mkstep("b", "jammer.tone", 10, 20)

    async def go():
        runner.active_steps.add("loc")
        runner._active["loc"] = task
        runner._build_ahead(step)
        await runner._wait_build("b")

    asyncio.run(go())
    check(host.calls == [], f"nothing sent while the radio is keyed: {host.names()}")
    check(any("not built ahead" in e[2] for e in runner.audit.entries), "the audit log says why")


# a Decode step that fails part way

def _decode_host(fail=(), plan_path="/tmp/p.json"):
    host = FakeHost(fail=fail)
    real = host.cmd

    async def with_plan(name, params=None, timeout=None):
        ack = await real(name, params, timeout)
        if name == "gen_scenario" and ack.status == 0:
            ack.result = {"plan_path": plan_path}
        return ack
    host.cmd = with_plan
    return host


def test_failed_decode_step_stops_rx():
    host = _decode_host(fail={"set_plan"})
    runner = MissionRunner(lambda rid: host, AuditLog(), speed=0.0)
    step = mkstep("d1", "scenario_decode", 0, 60, radiates=False, params={"spec_path": "x.json"})
    asyncio.run(runner._start_step(step))
    check("start_rx" not in host.names(), "no start_rx after the failure")
    check(host.names()[-1] == "stop_rx", f"RX stopped so no old plan decodes: {host.names()}")
    check(any("RX stopped" in e[2] for e in runner.audit.entries), "the audit log says so")


def test_failed_decode_step_closes_the_attenuator_it_opened():
    host = _decode_host(fail={"start_rx"})
    runner = MissionRunner(lambda rid: host, AuditLog(), speed=0.0)
    step = mkstep("d1", "scenario_decode", 0, 60, radiates=False,
                  params={"spec_path": "x.json", "rx_atten_db": 20})
    asyncio.run(runner._start_step(step))
    check(("atten_set", {"db": 20.0}) in host.calls, "the step opened the attenuator")
    check(host.calls[-2:] == [("atten_set", {"db": 120.0}), ("stop_rx", {})],
          f"back to 120 dB, then RX off: {host.calls[-2:]}")
    check(not runner._rx_held, "nothing left held")


def test_failed_decode_step_leaves_rx_for_another_decode_step():
    host = _decode_host()
    runner = MissionRunner(lambda rid: host, AuditLog(), speed=0.0)
    good = mkstep("d1", "scenario_decode", 0, 60, radiates=False, params={"spec_path": "x.json"})
    bad = mkstep("d2", "scenario_decode", 0, 60, radiates=False, params={"spec_path": "y.json"})

    async def go():
        await runner._start_step(good)
        host.fail = {"set_plan"}
        await runner._start_step(bad)

    asyncio.run(go())
    check("stop_rx" not in host.names(), f"RX kept for d1: {host.names()}")
    check(any("RX left on for d1" in e[2] for e in runner.audit.entries), "the audit log says why")


def test_failed_tx_step_sends_no_stop_rx():
    host = FakeHost(fail={"gen_test_waveform"})
    runner = MissionRunner(lambda rid: host, AuditLog(), speed=0.0)
    asyncio.run(runner._start_step(mkstep("t", "jammer.tone", 0, 10)))
    check("stop_rx" not in host.names(), f"a TX step leaves RX alone: {host.names()}")


# STOP ALL TX retries a radio that did not answer

class LateHost(FakeHost):
    """Does not answer stop_tx the first `miss` times."""

    def __init__(self, miss=1, **kw):
        super().__init__(**kw)
        self.miss = miss

    async def cmd(self, name, params=None, timeout=None):
        if name == "stop_tx" and self.miss > 0:
            self.miss -= 1
            self.calls.append((name, dict(params or {})))
            raise TimeoutError("not acked")
        return await super().cmd(name, params, timeout)


def test_stop_all_retries_a_radio_that_did_not_answer():
    live, late = FakeHost("LIVE"), LateHost(miss=2, serial="LATE")
    runner = MissionRunner(lambda rid: None, AuditLog(), speed=0.0)
    runner.STOP_RETRY_GAP_S = 0.05

    async def go():
        sent = await runner.stop_all([live, late])
        first = list(runner.stop_unconfirmed)
        await asyncio.sleep(0.3)
        return sent, first

    sent, first = asyncio.run(go())
    check(sent == 1, f"one radio answered at once: {sent}")
    check(first == ["LATE"], f"the silent radio is listed: {first}")
    check(late.names().count("stop_tx") == 3, f"retried until it answered: {late.names()}")
    check(runner.stop_unconfirmed == [], "cleared once it answered")
    text = "\n".join(runner.audit.lines())
    check("LATE answered stop_tx (retry 2)" in text, "the audit log says when it answered")


def test_stop_all_retry_gives_up_and_says_so():
    dead = LateHost(miss=10_000, serial="DEAD")
    runner = MissionRunner(lambda rid: None, AuditLog(), speed=0.0)
    runner.STOP_RETRY_GAP_S = 0.05
    runner.STOP_RETRY_S = 0.3

    async def go():
        await runner.stop_all([dead])
        await asyncio.sleep(0.6)

    asyncio.run(go())
    check(runner.stop_unconfirmed == ["DEAD"], "still listed")
    check("DEAD STOP NOT CONFIRMED" in "\n".join(runner.audit.lines()), "the audit log says so")


def test_new_run_cancels_a_stop_all_retry():
    dead = LateHost(miss=10_000, serial="DEAD")
    host = FakeHost()
    runner = MissionRunner(lambda rid: host, AuditLog(), speed=0.0)
    runner.STOP_RETRY_GAP_S = 0.05

    async def go():
        await runner.stop_all([dead])
        await runner.run({"name": "t", "steps": [mkstep("a", "jammer.tone", 0, 1)]})
        n = dead.names().count("stop_tx")
        await asyncio.sleep(0.3)
        return n

    n = asyncio.run(go())
    check(dead.names().count("stop_tx") == n, "no stop_tx after the new run started")
    check(runner.stop_unconfirmed == [], "the list is cleared for the new run")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{_passed} passed, {_failed} failed, {len(tests)} test functions")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
