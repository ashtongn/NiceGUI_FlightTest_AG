"""The decode step of the mission planner: the receive half of a comm waveform on another radio."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dpmg.mission import catalog, mission_file as mf                 # noqa: E402
from dpmg.mission.mission_runner import AuditLog, MissionRunner, step_commands, PLAN_FROM_GEN  # noqa: E402

SPEC = {"signals": [{"mod": "QPSK", "message": "HELLO"}]}
OTHER = {"signals": [{"mod": "FSK", "message": "OTHER"}]}


class Ack:
    def __init__(self, status=0, result=None):
        self.status, self.result = status, result or {}


class Host:
    def __init__(self, plan="data/waveforms/x.plan.json", fail=()):
        self.calls, self.plan, self.fail = [], plan, set(fail)
        self.serial = "G40-RX"

    async def cmd(self, name, params=None, timeout=None):
        self.calls.append((name, dict(params or {})))
        if name in self.fail:
            return Ack(1, {"err": "forced"})
        if name == "get_config":
            return Ack(0, {"inited": True, "achieved_rate": 1.92e6, "sample_rate": 1.92e6, "kind": "g40"})
        return Ack(0, {"plan_path": self.plan} if name == "gen_scenario" else {})

    def names(self):
        return [n for n, _ in self.calls]


def step(sid, tech, a, b, radio, spec=SPEC, **kw):
    p = dict(catalog.get(tech).fresh_params(), source="console", spec=spec)
    p.update(kw)
    return {"id": sid, "radio": radio, "technique": tech, "radiates": catalog.get(tech).radiates, "params": p,
            "schedule": {"start_s": a, "stop_s": b}}


def warnings(steps):
    return [c.message for c in mf._decode_conflicts(steps)]


def test_entry_does_not_radiate_and_receives():
    e = catalog.get("scenario_decode")
    assert e.radiates is False and catalog.receives("scenario_decode") and e.group == "Comms"


def test_commands_build_without_arming_and_never_key():
    start, stop = step_commands(step("d", "scenario_decode", 0, 30, "RX"))
    names = [n for n, _ in start]
    assert names == ["gen_scenario", "set_plan", "start_rx"] and stop == []
    assert dict(start)["gen_scenario"]["load_tx"] is False and dict(start)["gen_scenario"]["spec"] == SPEC
    assert "start_tx" not in names and "gen_test_waveform" not in names


def test_radio_file_goes_as_a_path():
    start, _ = step_commands(step("d", "scenario_decode", 0, 30, "RX", spec=None, source="radio", spec_path="scenarios/json/a.json"))
    assert dict(start)["gen_scenario"]["spec_path"] == "scenarios/json/a.json"


def run(h, s):
    r = MissionRunner(lambda rid: h, AuditLog(), speed=0.0)
    asyncio.run(r.start_step_now(s))
    return r


def test_runner_loads_the_plan_the_build_returned():
    h = Host()
    run(h, step("d", "scenario_decode", 0, 30, "RX"))
    plan = next(p for n, p in h.calls if n == "set_plan")
    assert plan == {"plan_path": "data/waveforms/x.plan.json"} and PLAN_FROM_GEN not in plan
    assert "start_rx" in h.names() and "start_tx" not in h.names()


def test_failed_build_loads_no_plan():
    h = Host(fail=("gen_scenario",))
    run(h, step("d", "scenario_decode", 0, 30, "RX"))
    assert "set_plan" not in h.names()


def test_build_with_no_plan_path_stops_the_step():
    h = Host(plan=None)
    r = run(h, step("d", "scenario_decode", 0, 30, "RX"))
    assert "set_plan" not in h.names() and "start_rx" not in h.names()
    assert any("no decode plan" in str(e) for e in getattr(r.audit, "entries", []))


def test_a_decode_step_never_counts_as_active_tx():
    h = Host()
    r = run(h, step("d", "scenario_decode", 0, 30, "RX"))
    assert "d" not in r.active_steps


def test_check_plan_warns_with_nothing_on_air():
    assert "no Comm waveform" in warnings([step("d", "scenario_decode", 0, 30, "RX")])[0]
    assert "no Comm waveform" in warnings([step("d", "scenario_decode", 0, 30, "RX"), step("t", "scenario_json", 40, 50, "TX")])[0]


def test_check_plan_warns_when_the_scenario_differs():
    w = warnings([step("d", "scenario_decode", 0, 30, "RX"), step("t", "scenario_json", 5, 25, "TX", spec=OTHER)])
    assert "differs" in w[0]


def test_check_plan_is_quiet_on_a_matching_pair():
    assert warnings([step("d", "scenario_decode", 0, 30, "RX"), step("t", "scenario_json", 5, 25, "TX")]) == []


def test_needs_a_scenario():
    p = dict(catalog.get("scenario_decode").fresh_params())
    m = {"format": "dpmg-mission/1", "name": "t", "authorization": {"reference": "x"},
         "radios": [{"id": "RX", "type": "g40", "techniques": ["rx"]}],
         "steps": [{"id": "d", "radio": "RX", "technique": "scenario_decode", "radiates": False, "params": p,
                    "schedule": {"start_s": 0, "stop_s": 10}, "ports": {"rx": "TRx1A"}}]}
    msgs = [c.message for c in mf.validate(m)]
    assert any("no scenario JSON" in x for x in msgs)


def test_rx_attenuator_is_set_after_the_plan_and_restored_at_the_stop():
    start, stop = step_commands(step("d", "scenario_decode", 0, 30, "RX", rx_atten_db=40))
    assert [n for n, _ in start] == ["gen_scenario", "set_plan", "atten_set", "start_rx"]
    assert dict(start)["atten_set"] == {"db": 40.0} and stop == [("atten_set", {"db": 120.0})]


class AttenHost(Host):
    """atten_set refused with the given service error, everything else ok."""
    def __init__(self, err):
        super().__init__()
        self.err = err

    async def cmd(self, name, params=None, timeout=None):
        if name == "atten_set":
            self.calls.append((name, dict(params or {})))
            return Ack(2, {"err": self.err})
        return await super().cmd(name, params, timeout)


def test_a_radio_with_no_attenuator_still_starts_rx():
    # the ground X40 has no attenuator; the bench run lost every decode this way
    h = AttenHost("no attenuator configured (atten.transport: none)")
    r = run(h, step("d", "scenario_decode", 0, 30, "RX", rx_atten_db=40))
    assert h.names()[-2:] == ["atten_set", "start_rx"]
    assert "d" not in r._rx_held
    assert any("no RX attenuator on this radio" in line for line in r.audit.lines())


def test_a_refused_attenuator_value_still_stops_the_step():
    for err in ("40 dB is under the safe floor of 50 dB",
                "atten: USB write failed"):
        h = AttenHost(err)
        run(h, step("d", "scenario_decode", 0, 30, "RX", rx_atten_db=40))
        # the step stops there, and RX goes off so the radio decodes no old plan
        assert h.names()[-2:] == ["atten_set", "stop_rx"] and "start_rx" not in h.names()


def test_an_attenuator_off_the_usb_still_decodes_with_a_warning():
    # AX35 has none on the bench; the payload one could drop off the USB in flight.
    # The step's plan is loaded, so RX goes on and the audit says the level is unknown.
    for state in ("not connected", "lost"):
        h = AttenHost(f"attenuator not connected (atten.transport: auto, {state})")
        r = run(h, step("d", "scenario_decode", 0, 30, "RX", rx_atten_db=0))
        names = h.names()
        assert names.index("set_plan") < names.index("atten_set") < names.index("start_rx")
        assert "stop_rx" not in names and "d" not in r._rx_held
        assert any("WARNING RX attenuator not connected" in line for line in r.audit.lines())


def test_an_attenuator_off_the_usb_without_a_plan_still_stops():
    # set_plan refused: nothing new to decode against, so RX goes off as before
    class NoPlan(AttenHost):
        async def cmd(self, name, params=None, timeout=None):
            if name == "set_plan":
                self.calls.append((name, dict(params or {})))
                return Ack(2, {"err": "bad plan"})
            return await super().cmd(name, params, timeout)
    h = NoPlan("attenuator not connected (atten.transport: auto, lost)")
    run(h, step("d", "scenario_decode", 0, 30, "RX", rx_atten_db=0))
    assert "atten_set" not in h.names() and h.names()[-1] == "stop_rx"
    assert "start_rx" not in h.names()


def test_a_plan_brings_up_the_receiving_radio_and_runs_the_step():
    h = Host()
    h.radio_state = None
    plan = {"name": "p", "radios": [{"id": "RX", "type": "g40", "sample_rate": 1920000}],
            "steps": [dict(step("d", "scenario_decode", 0, 2, "RX", center_hz=915e6, rx_atten_db=40), ports={"rx": "TRx1A"})]}
    asyncio.run(MissionRunner(lambda rid: h, AuditLog(), speed=0.0).run(plan))
    names = h.names()
    assert names.index("gen_scenario") < names.index("set_plan") < len(names) - 1 - names[::-1].index("start_rx")
    assert "start_tx" not in names
    assert names.count("atten_set") == 2 and h.calls[-1] == ("atten_set", {"db": 120.0})


class SlowInit(Host):
    def __init__(self, secs):
        super().__init__()
        self.secs = secs

    async def cmd(self, name, params=None, timeout=None):
        if name == "init":
            await asyncio.sleep(self.secs)
        return await super().cmd(name, params, timeout)


def test_a_step_whose_slot_passes_in_a_re_init_is_skipped_and_said_so():
    h = SlowInit(1.5)
    audit = AuditLog()
    rate = {"id": "r", "radio": "RX", "technique": "set_rate", "radiates": False,
            "params": {"sample_rate": 7_680_000}, "schedule": {"start_s": 0.0, "stop_s": 0.1}}
    lost = {"id": "lost", "radio": "RX", "technique": "tools.tone", "radiates": True,
            "params": dict(catalog.get("tools.tone").fresh_params()), "schedule": {"start_s": 0.2, "stop_s": 0.5}}
    kept = {"id": "kept", "radio": "RX", "technique": "tools.tone", "radiates": True,
            "params": dict(catalog.get("tools.tone").fresh_params()), "schedule": {"start_s": 2.5, "stop_s": 2.8}}
    plan = {"name": "p", "radios": [{"id": "RX", "type": "g40"}], "steps": [rate, lost, kept]}
    asyncio.run(MissionRunner(lambda rid: h, audit, speed=1.0).run(plan))
    text = " ".join(str(e) for e in audit.entries)
    assert "step lost on RX skipped" in text and "step kept started" in text
    assert h.names().count("start_tx") == 1 and h.names().count("stop_tx") == 1


def test_init_status_names_the_radio_only_during_an_init():
    seen = []

    class Watch(SlowInit):
        async def cmd(self, name, params=None, timeout=None):
            if name == "init":
                async def later():
                    await asyncio.sleep(0.05)
                    seen.append(runner.init_status())
                asyncio.create_task(later())
            return await super().cmd(name, params, timeout)

    h = Watch(0.2)
    runner = MissionRunner(lambda rid: h, AuditLog(), speed=1.0)
    rate = {"id": "r", "radio": "RX", "technique": "set_rate", "radiates": False,
            "params": {"sample_rate": 7_680_000}, "schedule": {"start_s": 0.0, "stop_s": 0.1}}
    asyncio.run(runner.run({"name": "p", "radios": [{"id": "RX", "type": "g40"}], "steps": [rate]}))
    assert seen and seen[0].startswith("RX ") and runner.init_status() == ""


def test_banner_text_follows_start_and_re_init():
    from dpmg import run_notice as n
    n.starting(False)
    assert n.text() == ""
    n.starting(True)
    assert "steps have not started" in n.text()
    n.begin("RX")
    assert "RX 0 s" in n.text() and "coming up" in n.text()
    n.starting(False)
    assert n.text() == ""
    n.begin("RX")
    assert n.text().startswith("Radio re-initializing") and "RX" in n.text()
    n.end("RX")
    assert n.text() == ""


def test_location_decode_with_same_scenario_has_no_warning():
    spec = {"name": "a", "signals": [{"modulation": "QPSK"}]}
    where = {"start": {"lat": 33.5, "lon": -112.0, "radius_m": 800.0}}
    trig = {"mode": "dwell", "dwell_s": 20.0, "max_s": 60.0, "confirm": "auto"}
    rx = {"id": "rx", "technique": "scenario_decode", "params": {"spec": spec}, "location": where, "trigger": trig}
    tx = {"id": "tx", "technique": "scenario_json", "params": {"spec": spec}, "location": where, "trigger": trig}
    assert mf._decode_conflicts([rx, tx]) == []
    other = dict(tx, params={"spec": {"name": "b", "signals": [{"modulation": "8PSK"}]}})
    assert [c.step_id for c in mf._decode_conflicts([rx, other])] == ["rx"]


class ResetHost(Host):
    """atten_set answered with the given status and readback."""
    def __init__(self, status=0, readback=120.0, err=None):
        super().__init__()
        self.ans = (status, readback, err)

    async def cmd(self, name, params=None, timeout=None):
        if name == "atten_set":
            self.calls.append((name, dict(params or {})))
            st, rb, err = self.ans
            return Ack(st, {"readback_db": rb, "err": err} if st else {"readback_db": rb})
        return await super().cmd(name, params, timeout)


def stop_all_with_held(h):
    r = MissionRunner(lambda rid: h, AuditLog(), speed=0.0)
    r._rx_held = {"d": "RX"}
    asyncio.run(r.stop_all([h]))
    return r, "\n".join(r.audit.lines())


def test_stop_all_reports_the_attenuator_readback():
    r, log = stop_all_with_held(ResetHost())
    assert "back to 120 dB (reads back 120 dB)" in log and r.atten_not_reset == []


def test_stop_all_says_when_the_attenuator_is_not_back():
    r, log = stop_all_with_held(ResetHost(status=2, err="attenuator not connected"))
    assert "NOT back to 120 dB: refused: attenuator not connected" in log
    assert r.atten_not_reset == ["RX (refused: attenuator not connected)"]
    r, log = stop_all_with_held(ResetHost(readback=20.0))
    assert "NOT back to 120 dB: reads back 20 dB" in log and len(r.atten_not_reset) == 1
