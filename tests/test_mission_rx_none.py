"""RX "none" on a mission step: a transmit only step.

The ports logic, the plan file round trip and checks, the runner's init
decision against a fake radio, and the HPA bench plan generator. Nothing keys.

    python3 -m pytest -q tests/test_mission_rx_none.py
"""

import asyncio
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from dpmg.mission import mission_file as mf
from dpmg.mission import mission_runner as mr
from dpmg.mission import ports
from dpmg.mission.mission_runner import MissionRunner, AuditLog
from dpmg.radio import sdr_core as sdr


# ports

def test_step_ports_reads_none_in_any_case():
    for v in ("none", "None", "NONE", " none "):
        assert ports.step_ports({"ports": {"rx": v, "tx": "TRx1B"}}) == ("none", "TRx1B")
    assert ports.step_ports({"ports": {"rx": None, "tx": "TRx1B"}}) == (None, "TRx1B")
    s = {}
    ports.set_step_ports(s, ports.RX_NONE, "TRx1B")
    assert s["ports"] == {"rx": "none", "tx": "TRx1B"}


def test_none_is_valid_and_never_a_conflict():
    assert ports.invalid("g40", "none", "TRx1B") == []
    assert ports.invalid("x40", "none", "TXA1") == []
    assert ports.conflict("g40", "none", "TRx1B") is None
    assert ports.conflict("x40", "none", "TXA1") is None
    assert ports.label("none", "TRx1B") == "RX none TX TRx1B"
    assert ports.rx_none_ok("g40") and ports.rx_none_ok("b200")
    assert not ports.rx_none_ok("x410")


def test_init_fields_ignores_none():
    f = ports.init_fields("g40", "none", "TRx1B")
    assert "rx_antenna" not in f
    assert f["tx_antenna"] == "TXA1" and f["extras"] == {"tx_card": 1}
    assert ports.init_fields("b200", "none", "TX/RX") == {"tx_antenna": "TX/RX"}


def test_rx_for_none_g40_goes_to_the_other_card():
    # the HPA bench case: only RX jack TRx2B, TX on TRx1B
    assert ports.rx_for_none("g40", "TRx2B", "TRx1B") == "TRx1A"
    # same SMA is not allowed either
    assert ports.rx_for_none("g40", "TRx1B", "TRx1B") == "TRx1A"
    # same card TX on a 2 jack only couples to the jack: move RX off that card
    assert ports.rx_for_none("g40", "TRx1B", "TRx2B") == "TRx1A"
    assert ports.rx_for_none("g40", "TRx1A", "TRx2A") == "TRx1B"
    # already on the other card: kept
    assert ports.rx_for_none("g40", "TRx2A", "TRx1B") == "TRx2A"
    assert ports.rx_for_none("g40", "TRx1A", "TRx2B") == "TRx1A"
    # nothing to fit against
    assert ports.rx_for_none("g40", "TRx2B", None) == "TRx2B"
    assert ports.rx_for_none("g40", None, None) is None
    for rx in ports.G40_PORTS:
        for tx in ports.G40_PORTS:
            got = ports.rx_for_none("g40", rx, tx)
            assert got != tx
            assert sdr.g40_same_card_conflict(got, tx) is None
    assert ports.rx_fitting("g40", "TRx1B") == ["TRx1A", "TRx2A"]


def test_rx_for_none_x40_and_b200():
    assert ports.rx_for_none("x40", "RXA1", "TXA1") == "RXB1"
    assert ports.rx_for_none("x40", "RXB2", "TXA1") == "RXB2"
    assert ports.rx_for_none("b200", "TX/RX", "TX/RX") == "TX/RX"
    assert ports.rx_for_none("b200", "RX2", "TX/RX") == "RX2"
    assert ports.rx_fitting("b200", "TX/RX") == []


def _g40_snap(rx_card, rxa, tx_card, txa):
    return {"rx_antenna": rxa, "card": rx_card, "tx_antenna": txa, "tx_card": tx_card}


def test_needs_reinit_with_none():
    # RX TRx2B, TX TRx1A: a step TX TRx1B RX none must move RX to card A
    snap = _g40_snap(1, "RXA2", 0, "TXA1")
    assert ports.needs_reinit(snap, "g40", "none", "TRx1B")
    assert ports.resolve(snap, "g40", "none", "TRx1B") == ("TRx1A", "TRx1B")
    # RX TRx1A, TX TRx1B: already fine, RX none changes nothing
    snap = _g40_snap(0, "RXA1", 1, "TXA1")
    assert not ports.needs_reinit(snap, "g40", "none", "TRx1B")
    assert not ports.needs_reinit(snap, "g40", "none", None)
    # TX change only: RX stays where it is
    assert ports.needs_reinit(snap, "g40", "none", "TRx2B")
    assert ports.resolve(snap, "g40", "none", "TRx2B") == ("TRx1A", "TRx2B")
    # TX unset comes back as the current TX so chan_mode is set for the pair
    snap = _g40_snap(0, "RXA1", 0, "TXA2")
    rx, tx = ports.resolve(snap, "g40", "none", None)
    assert (rx, tx) == ("TRx1B", "TRx2A")
    assert ports.init_fields("g40", rx, tx)["extras"]["chan_mode"] == "single"


# plan file

def _plan(steps):
    m = mf.new_mission("rx none")
    m["radios"] = [{"id": "G40-SIM", "type": "g40", "techniques": ["rx", "tx"]}]
    m["authorization"] = {"reference": "T-1"}
    m["steps"] = steps
    return m


def _st(sid, start, stop, rx=None, tx=None, tech="tools.tone"):
    s = {"id": sid, "radio": "G40-SIM", "technique": tech, "radiates": tech != "record",
         "params": {"kind": "tone"} if tech == "tools.tone" else {},
         "schedule": {"start_s": start, "stop_s": stop}}
    ports.set_step_ports(s, rx, tx)
    return s


def test_file_round_trip_keeps_none():
    m = _plan([_st("a", 0, 10, rx="none", tx="TRx1B"), _st("b", 20, 30)])
    q = mf.loads(json.loads(mf.dumps(m)))
    assert q["steps"][0]["ports"] == {"rx": "none", "tx": "TRx1B"}
    assert ports.step_ports(q["steps"][0]) == ("none", "TRx1B")
    assert "ports" not in q["steps"][1]


def test_old_plan_without_ports_unchanged():
    m = _plan([_st("a", 0, 10), _st("b", 20, 30, rx="TRx1B", tx="TRx1A")])
    q = mf.loads(json.loads(mf.dumps(m)))
    assert "ports" not in q["steps"][0]
    assert q["steps"][1]["ports"] == {"rx": "TRx1B", "tx": "TRx1A"}
    assert not any("RX none" in c.message for c in mf.validate(q))


def test_validate_rx_none_tx_on_the_rx_card_is_not_refused():
    m = _plan([_st("a", 10, 30, rx="none", tx="TRx1B"),
               _st("b", 50, 70, rx="none", tx="TRx1B")])
    cs = mf.validate(m)
    assert not [c for c in cs if c.level == "error"], cs
    none_w = [c for c in cs if c.message.startswith("RX none")]
    assert [c.step_id for c in none_w] == ["a"], none_w
    assert "TRx1A or TRx2A" in none_w[0].message and "moves RX to TRx1A" in none_w[0].message
    assert not any("re-initializes" in c.message for c in cs), cs


def test_validate_no_warning_when_the_connected_radio_rx_fits():
    m = _plan([_st("a", 10, 30, rx="none", tx="TRx2B"),
               _st("b", 50, 70, rx="none", tx="TRx2B")])
    rid = m["radios"][0]["id"]
    assert [c for c in mf.validate(m) if c.message.startswith("RX none")]      # offline, not known
    assert not [c for c in mf.validate(m, rx_now={rid: "TRx1A"})
                if c.message.startswith("RX none")]


def test_validate_names_the_move_when_the_connected_radio_rx_does_not_fit():
    m = _plan([_st("a", 10, 30, rx="none", tx="TRx2B"),
               _st("b", 50, 70, rx="none", tx="TRx2B")])
    rid = m["radios"][0]["id"]
    w = [c for c in mf.validate(m, rx_now={rid: "TRx1B"}) if c.message.startswith("RX none")]
    assert [c.step_id for c in w] == ["a"], w
    assert "RX on TRx1B cannot stay" in w[0].message and "moves RX to TRx1A" in w[0].message


def test_validate_names_the_rx_move_after_a_known_rx():
    m = _plan([_st("a", 0, 10, rx="TRx2B", tx="TRx1A"),
               _st("b", 70, 90, rx="none", tx="TRx1B"),
               _st("c", 110, 130, rx="none", tx="TRx1B")])
    cs = mf.validate(m)
    assert not [c for c in cs if c.level == "error"], cs
    move = [c for c in cs if c.message.startswith("RX none")]
    assert [c.step_id for c in move] == ["b"], move
    assert "moves RX to TRx1A" in move[0].message
    # gap of 60 s is enough, so no late start warning; c needs no move
    assert not any("re-initializes" in c.message for c in cs), cs
    # a short gap does warn, with the port the radio really ends up on
    m["steps"][1]["schedule"] = {"start_s": 20, "stop_s": 40}
    late = [c for c in mf.validate(m) if "re-initializes" in c.message]
    assert [c.step_id for c in late] == ["b"] and "RX TRx1A TX TRx1B" in late[0].message


def test_validate_rx_none_on_a_receiving_step_is_an_error():
    m = _plan([_st("r", 0, 3, rx="none", tech="record")])
    errs = [c for c in mf.validate(m) if c.level == "error" and "RX is none" in c.message]
    assert errs and errs[0].step_id == "r"


# runner

class _Ack:
    def __init__(self, status=0, result=None):
        self.status = status
        self.result = result or {}


class _G40Host:
    """A fake G40 service: get_config answers its snapshot, init applies the
    ports to it, everything else answers OK. Refuses what the service refuses."""

    def __init__(self, rx_card, rxa, tx_card, txa):
        self.calls = []
        self.snap = {"inited": True, "kind": "g40", "achieved_rate": 1.92e6,
                     "sample_rate": 1.92e6, "center_hz": 9.15e8, "rx_gain": 200,
                     "tx_gain": 0, "clock_source": "external",
                     "rx_antenna": rxa, "card": rx_card, "tx_antenna": txa,
                     "tx_card": tx_card, "chan_mode": "single" if rx_card != tx_card else "dual"}

    async def cmd(self, name, params=None, timeout=None):
        params = dict(params or {})
        self.calls.append((name, params))
        if name == "get_config":
            return _Ack(0, dict(self.snap))
        if name == "init":
            ex = params.get("extras") or {}
            why = sdr.g40_fixed_mode_conflict(
                params.get("rx_antenna"), params.get("tx_antenna"),
                "dual" if sdr.needs_dual_channel(str(params.get("rx_antenna") or ""),
                                                 str(params.get("tx_antenna") or ""), ex)
                else "single", ex.get("card", 0), ex.get("tx_card"))
            if why:
                return _Ack(1, {"err": why, "g40_same_card": True})
            self.snap.update({"rx_antenna": params.get("rx_antenna"),
                              "tx_antenna": params.get("tx_antenna"),
                              "card": ex.get("card"), "tx_card": ex.get("tx_card"),
                              "chan_mode": ex.get("chan_mode")})
            return _Ack(0, {"achieved_rate": params.get("sample_rate")})
        return _Ack(0)

    def names(self):
        return [n for n, _ in self.calls]


def _rstep(sid, start, stop, rx="none", tx="TRx1B"):
    s = {"id": sid, "radio": "SIM", "technique": "tools.tone", "radiates": True,
         "params": {"kind": "tone"}, "schedule": {"start_s": start, "stop_s": stop}}
    ports.set_step_ports(s, rx, tx)
    return s


def test_runner_moves_rx_once_for_a_run_of_none_steps():
    # radio up with RX TRx2B (card B), TX TRx1A: TX only steps on TRx1B
    h = _G40Host(1, "RXA2", 0, "TXA1")
    runner = MissionRunner(lambda rid: h, AuditLog(), speed=0.0)
    steps = [_rstep(f"s{i}", i * 10, i * 10 + 5) for i in range(4)]

    async def go():
        for s in steps:
            await runner._start_step(s)
            await runner._stop_step(s)
    asyncio.run(go())
    inits = [p for n, p in h.calls if n == "init"]
    assert len(inits) == 1, h.names()
    p = inits[0]
    assert p["rx_antenna"] == "RXA1" and p["tx_antenna"] == "TXA1"
    assert p["extras"]["card"] == 0 and p["extras"]["tx_card"] == 1
    assert p["extras"]["chan_mode"] == "single"
    assert p["sample_rate"] == 1.92e6 and p["clock_source"] == "external"
    assert h.names().count("start_tx") == 4
    lines = [e[2] for e in runner.audit.entries]
    assert any("RX TRx2B TX TRx1A" in l and "RX TRx1A TX TRx1B" in l and "RX none" in l
               for l in lines), lines
    assert not any("FAILED" in l for l in lines), lines


def test_runner_leaves_a_fitting_rx_alone():
    h = _G40Host(0, "RXA2", 1, "TXA1")        # RX TRx2A, TX TRx1B already
    runner = MissionRunner(lambda rid: h, AuditLog(), speed=0.0)
    asyncio.run(runner._start_step(_rstep("a", 0, 5)))
    asyncio.run(runner._prepare_ports(_rstep("b", 10, 15)))
    assert "init" not in h.names(), h.names()


def test_plan_events_prepare_a_none_step():
    plan = {"name": "p", "radios": [{"id": "SIM", "type": "g40"}],
            "steps": [_rstep("a", 0, 10, rx="TRx2B", tx="TRx1A"), _rstep("b", 60, 70)]}
    preps = [(e.t_s, e.step["id"]) for e in mr.plan_events(plan) if e.kind == "prepare"]
    assert (10.0, "b") in preps, preps


# bench plan generator

def test_gen_hpa_bench_mission_rx_none(tmp_path):
    sys.path.insert(0, os.path.join(REPO, "tools"))
    try:
        import gen_hpa_bench_mission as gen
    finally:
        sys.path.pop(0)
    out = tmp_path / "plan.dpmg-mission.json"
    rc = gen.main(["--rx-port", "None", "--tx-port", "TRx1B", "--freqs", "915",
                   "--attens", "41.75,35", "--out", str(out)])
    assert rc == 0
    plan = mf.load(str(out))
    assert plan["steps"]
    assert all(s["ports"] == {"rx": "none", "tx": "TRx1B"} for s in plan["steps"])
    assert "RX none" in plan["notes"] and "TRx1A" in plan["notes"]
    assert not [c for c in mf.validate(plan) if c.level == "error"]
