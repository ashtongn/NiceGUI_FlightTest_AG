"""Mission run model: drive the existing service commands on a timeline.

This is the part that keys TX, and it does it only through commands the dpmg
service already has, the same ones the jammer, playback, tools, transponder and
record panels send. There is no new key path here. The single run confirm and
the per-radio radiate confirm live in the tab (mission_tab.py); by the time the
runner's run() is called the operator has already confirmed.

What it does:
  step_commands(step)   pure mapping from a step to the (start, stop) service
                        commands. Testable without a radio.
  plan_events(mission)  the ordered list of timed actions for a whole plan.
  MissionRunner         awaits each action at its time, driving host.cmd, and
                        writes every action to an audit log. stop_all() cancels
                        the run and sends stop_tx to every radio at once.

step_commands holds the catalog-to-command mapping, one case per technique.
Techniques the runner does not drive (secure messaging) are reported, not keyed.
"""

from __future__ import annotations

import asyncio
import json
import time

from . import catalog
from . import ports
from . import scenario_spec
from .. import rec_state, run_notice
from ..link.proto import RadioState, Status
from ..radio import g40_clock as _g40_clock


# Techniques the runner does not drive. They can sit on a timeline and
# be validated, but the runner does not key them; it logs that it skipped them.
NOT_WIRED = {"secure.send"}


# how long to wait on a command. A scenario build at 30.72 MSps can take over
# a minute on the radio (the playback panel allows 180 s), the rest are quick.
CMD_TIMEOUT_S = 30.0
GEN_SCENARIO_TIMEOUT_S = 180.0

# step keys the runner turns into their own commands; never passed on to
# gen_test_waveform
_RF_KEYS = ("center_hz", "tx_center_hz", "tx_atten_db", "retune_hz", "rx_gain_index")
# the per-step tuning commands, all sent before the step's own commands
_RF_CMDS = frozenset({"set_center", "set_tx_center", "set_gain", "set_tx_atten"})

# a set_rate step maps to this internal command name; _start_step recognizes it
# and re-initializes the radio instead of passing it to host.cmd (there is no
# live "set sample rate", only a full init).
RECONFIGURE_RATE = "__set_sample_rate__"
# set_plan parameter: use the plan_path from this step's gen_scenario, known only at run time
PLAN_FROM_GEN = "__plan_from_gen__"

# commands that build or load a TX waveform on the radio. They take 1 to 4 s
# (longest at 30.72 MSps), so a step that sends one at its start time keys late
# and runs short. The runner sends them up to BUILD_LEAD_S ahead, in the gap
# after the step before it on that radio has stopped, and only start_tx at the
# start time.
_BUILD_CMDS = frozenset({"gen_test_waveform", "gen_scenario", "set_tx_waveform",
                         "replay_recording"})
BUILD_LEAD_S = 10.0


def rate_change_hz(step: dict):
    """The sample rate a set_rate step asks for, in Hz, or None for any other
    step."""
    if step.get("technique") != "set_rate":
        return None
    hz = (step.get("params") or {}).get("sample_rate")
    try:
        hz = float(hz)
    except (TypeError, ValueError):
        return None
    return hz if hz > 0 else None


def _init_params_from_snapshot(snap: dict, sample_rate_hz: float) -> dict:
    """Build an init params block that keeps the radio as it is now but changes
    only the sample rate. The snapshot is get_config's result; the planner does
    not hold a per-radio init block (by design), so the config to keep comes
    from the live radio. Fields map to what the service's init takes."""
    p = {"sample_rate": float(sample_rate_hz)}
    for src, dst in (("center_hz", "center_freq"), ("rx_gain", "rx_gain"),
                     ("tx_gain", "tx_gain"), ("clock_source", "clock_source"),
                     ("rx_antenna", "rx_antenna"), ("tx_antenna", "tx_antenna"),
                     ("kind", "kind")):
        v = snap.get(src)
        if v not in (None, ""):
            p[dst] = v
    extras = {}
    for src, dst in (("card", "card"), ("tx_card", "tx_card"),
                     ("chan_mode", "chan_mode")):
        v = snap.get(src)
        if v not in (None, ""):
            extras[dst] = v
    # a radio running a separate TX rate on the other card keeps that split; the
    # service refuses a split unless TX is cross-card, so only carry it when the
    # radio reported a distinct TX card.
    txr = snap.get("tx_sample_rate")
    if txr and extras.get("tx_card") is not None \
            and extras.get("tx_card") != extras.get("card"):
        extras["tx_sample_rate"] = float(txr)
        if snap.get("tx_center_hz") not in (None, ""):
            extras["tx_center_freq"] = float(snap["tx_center_hz"])
    if extras:
        p["extras"] = extras
    return p


# bringing up a radio that is not initialized (bring_up_plan)
BRING_UP_RATE_HZ = float(catalog.DEFAULT_RATE_HZ)   # for a plan with no rate
BRING_UP_TX_ATTEN_DB = 30.0         # the DPMG app's default, for a plan with no atten
BRING_UP_TIMEOUT_S = 90.0           # first RFIC cal is about 35 s on a G40
BRING_UP_RETRY_S = 60.0             # a failed init can stop the service: do not repeat it per step
CLOCK_SOURCES = tuple(_g40_clock.G40_CHOICES)   # the tokens a G40 takes; X40 rows take X40_CHOICES


def _first_param(steps: list, key: str):
    for s in steps:
        v = (s.get("params") or {}).get(key)
        if v not in (None, "", 0):
            return float(v)
    return None


def bring_up_plan(radio: dict, steps: list, hz=None, pm=None) -> dict:
    """How to initialize a radio that is not initialized, from what the plan
    says: the radio row's start rate and clock, and the frequency, ports and TX
    attenuation of its first steps. steps are this radio's steps, the first to
    run first. Returns {"params", "rx_on", "text"}, or {"err"} when the plan
    does not say enough to do it safely. Nothing here keys the radio."""
    rtype = radio.get("type") or "g40"
    center = _first_param(steps, "center_hz")
    tx_center = _first_param(steps, "tx_center_hz")
    if center is None:
        center = tx_center
    if center is None:
        return {"err": "no step gives a frequency"}
    rate = hz or radio.get("sample_rate")
    if not rate:
        rate = next((rate_change_hz(s) for s in steps if rate_change_hz(s)), None)
    rate = float(rate or BRING_UP_RATE_HZ)
    clock = str(radio.get("clock_source") or "internal")
    _ck = _g40_clock.resolve(clock, "x40" if str(rtype).lower() == "x40" else "g40")
    if _ck.get("err"):
        return {"err": _ck["err"]}

    chosen = [ports.step_ports(s) for s in steps]
    rx = next((r for r, _ in chosen if r and r != ports.RX_NONE), None)
    tx = next((t for _, t in chosen if t), None)
    epiq = str(rtype).lower() in ("g40", "x40")
    if tx is None and epiq and any(s.get("radiates") for s in steps):
        allowed = [p for p in ports.tx_options(rtype) if pm and ports._pm.allows(pm, p, "tx")]
        if len(allowed) == 1:
            tx = allowed[0]
        else:
            return {"err": "a radiating step has no TX port chosen"}
    # RX is off only for a radio whose steps all say RX none
    rx_on = not chosen or any(r != ports.RX_NONE for r, _ in chosen)
    if rx is None and tx:
        rx = ports.rx_for_none(rtype, None, tx, pm)
    if rx is None and epiq:
        rx = next((p for p in ports.rx_options(rtype)
                   if not pm or ports._pm.allows(pm, p, "rx")), None)
    why = ports.conflict(rtype, rx, tx) if rx and tx else None
    if why:
        return {"err": str(why)}

    params = {"kind": rtype, "sample_rate": rate, "center_freq": center,
              "clock_source": clock}
    pf = ports.init_fields(rtype, rx, tx)
    extras = pf.pop("extras", {})
    params.update(pf)
    if tx_center is not None and tx:
        extras["tx_center_freq"] = tx_center
    if extras:
        params["extras"] = extras
    if epiq and tx:
        # Epiq tx_gain is the attenuation; the service's default is lower
        params["tx_gain"] = _first_param(steps, "tx_atten_db") or BRING_UP_TX_ATTEN_DB
    text = (f"{rate / 1e6:g} MSps, {center / 1e6:g} MHz, "
            f"{'RX ' + str(rx) if rx_on and rx else 'RX on' if rx_on else 'RX off'}, "
            f"{'TX ' + str(tx) + ', ' if tx else ''}clock {clock}")
    return {"params": params, "rx_on": rx_on, "text": text}


def plan_steps_on(mission: dict, rid, first=None) -> list:
    """A radio's steps in start order, with `first` (the step about to run) at
    the front, for bring_up_plan. Location tasks come after the timeline."""
    from .mission_file import step_start_s, is_location_task
    mine = [s for s in (mission or {}).get("steps", []) if s.get("radio") == rid]
    mine.sort(key=lambda s: (is_location_task(s), step_start_s(s)))
    if first is None:
        return mine
    return [first] + [s for s in mine if s.get("id") != first.get("id")]


def _cmd_timeout_s(name: str, params: dict) -> float:
    """How long to wait for a command's ack.  Most commands answer at once, so
    the generic timeout holds.  gen_scenario builds a waveform first.  The
    transponder runs its whole RX dwell before it acks, so waiting exactly the
    generic time on a 30 s dwell logged a false "not acked" error on the bench;
    give it the dwell plus a margin."""
    if name == "gen_scenario":
        return GEN_SCENARIO_TIMEOUT_S
    if name == "transponder":
        try:
            dwell = float((params or {}).get("seconds", 5.0))
        except (TypeError, ValueError):
            dwell = 5.0
        return max(CMD_TIMEOUT_S, dwell + 10.0)
    return CMD_TIMEOUT_S


def _gen_params(step: dict) -> dict:
    """Service params for gen_test_waveform from a jammer/tools step, as the
    jammer panel builds them: the generator keys, prf and duty only when
    pulsing (prf above 0), load_tx and a name so the result is armed as the TX
    source."""
    p = {k: v for k, v in (step.get("params") or {}).items()
         if k not in _RF_KEYS and v is not None}
    # a plan written by hand (not through the catalog) may leave the generator
    # kind out; the service then falls back to a chirp, so every tone and noise
    # step would radiate a chirp.  Each jammer.* and tools.* technique is named
    # after its generator kind, so take it from the technique when it is absent.
    if "kind" not in p:
        tech = str(step.get("technique") or "")
        suffix = tech.split(".", 1)[1] if "." in tech else ""
        if suffix:
            p["kind"] = suffix
    if float(p.get("prf") or 0) <= 0:
        p.pop("prf", None)
        p.pop("duty", None)
    p["load_tx"] = True
    p.setdefault("name", f"mission_{step.get('id', 'step')}")
    return p


def _rf_cmds(params: dict) -> list:
    """Per-step tuning set before the step starts, the way the control card
    sets it live: set_center in Hz, set_gain as the hardware gain index,
    set_tx_atten in quarter dB, set_tx_center to put TX on its own frequency.
    None of these re-initializes the radio. A blank (None) value leaves the
    radio as it is (a blank TX frequency is handled by the runner, see
    MissionRunner._tx_back_to_rx). retune_hz is the older name for
    the playback retune. RX gain goes after the center because the radio looks
    up its gain calibration at the current center."""
    out = []
    hz = params.get("center_hz")
    if hz in (None, "", 0):
        hz = params.get("retune_hz")
    if hz not in (None, "") and float(hz) > 0:
        out.append(("set_center", {"hz": float(hz)}))
    # TX on its own frequency: after set_center, which moves TX along with RX
    tx = params.get("tx_center_hz")
    if tx not in (None, "") and float(tx) > 0:
        out.append(("set_tx_center", {"hz": float(tx)}))
    gain = params.get("rx_gain_index")
    if gain not in (None, ""):
        out.append(("set_gain", {"index": int(gain)}))
    att = params.get("tx_atten_db")
    if att not in (None, ""):
        out.append(("set_tx_atten", {"qdb": int(round(float(att) * 4))}))
    return out


def _no_attenuator(err) -> bool:
    """True when the service refused atten_set because the radio has no
    attenuator configured (atten.transport none), not because of the value."""
    return str(err or "").startswith("no attenuator configured")


def _atten_gone(err) -> bool:
    """True when the radio has an attenuator set up but it is not on the USB
    now (never found, or lost since), as opposed to a refused value."""
    return str(err or "").startswith("attenuator not connected")


def step_commands(step: dict):
    """Return (start_cmds, stop_cmds) for a step, each a list of (name, params)
    tuples to pass to host.cmd. Returns (None, None) for a technique the runner
    does not drive. Raises ValueError for a technique not in the catalog."""
    tech = step.get("technique")
    entry = catalog.get(tech)
    if entry is None:
        raise ValueError(f"unknown technique {tech!r}")
    if tech in NOT_WIRED:
        return None, None

    params = dict(step.get("params") or {})

    if tech == "set_rate":
        hz = rate_change_hz(step)
        if hz is None:
            return None, None
        # the runner reads this and re-initializes the radio; nothing to stop
        return [(RECONFIGURE_RATE, {"sample_rate": hz})], []

    if tech == "rx_dwell":
        # a receive dwell: the RF tuning commands only, nothing keys
        start = [(n, pr) for n, pr in _rf_cmds(params)
                 if n in ("set_center", "set_gain")]
        return start, []

    if tech.startswith("jammer.") or tech.startswith("tools."):
        start = _rf_cmds(params) + [("gen_test_waveform", _gen_params(step)),
                                    ("start_tx", {"mode": "CONTINUOUS"})]
        return start, [("stop_tx", {})]

    if tech == "scenario_json" or (tech == "iq_playback"
                                   and params.get("source") == "scenario"):
        # a comm waveform from a scenario JSON, built on the radio the way the
        # playback panel builds one. The spec rides inline when it came from
        # the console (the runner swaps it for spec_inbox at start time when it
        # is too big for one datagram); a radio file goes as spec_path. An
        # iq_playback step with source scenario is an older plan that did not
        # pass through mission_file.loads; it maps the same way.
        start = _rf_cmds(params)
        gp = {"load_tx": True,
              "match_radio_rate": bool(params.get("match_radio_rate", True))}
        if params.get("peak_fs") not in (None, ""):
            gp["peak_fs"] = float(params["peak_fs"])
        spec = params.get("spec")
        if scenario_spec.is_scenario_spec(spec):
            gp["spec"] = spec
        else:
            gp["spec_path"] = str(params.get("spec_path") or params.get("path")
                                  or params.get("name") or "")
        start += [("gen_scenario", gp), ("start_tx", {"mode": "CONTINUOUS"})]
        return start, [("stop_tx", {})]

    if tech == "scenario_decode":
        # the receive half of a comm waveform: same build with TX not armed, then its
        # decode plan on this radio. No start_tx, so there is nothing to stop.
        start = [(n, pr) for n, pr in _rf_cmds(params) if n in ("set_center", "set_gain")]
        gp = {"load_tx": False, "match_radio_rate": bool(params.get("match_radio_rate", True))}
        spec = params.get("spec")
        if scenario_spec.is_scenario_spec(spec):
            gp["spec"] = spec
        else:
            gp["spec_path"] = str(params.get("spec_path") or "")
        start += [("gen_scenario", gp), ("set_plan", {PLAN_FROM_GEN: True})]
        stop = []
        if params.get("rx_atten_db") not in (None, ""):
            start.append(("atten_set", {"db": float(params["rx_atten_db"])}))
            stop.append(("atten_set", {"db": 120.0}))
        return start + [("start_rx", {})], stop

    if tech == "iq_playback":
        start = _rf_cmds(params)
        # the file's path on the radio, as list_waveforms reports it. Older plans
        # used a "name" key for the same thing.
        path = params.get("path") or params.get("name", "")
        # the service reads "path" (or "inbox"), same as the playback panel
        start.append(("set_tx_waveform", {"kind": "file", "path": path}))
        start.append(("start_tx", {"mode": "CONTINUOUS"}))
        return start, [("stop_tx", {})]

    if tech == "transponder":
        p = {"seconds": max(1.0, _duration_s(step)),
             # threshold_db is the older name for the trigger SNR
             "trigger_snr_db": float(params.get("trigger_snr_db",
                                                params.get("threshold_db", 15.0))),
             "emulate": bool(params.get("emulate", True)),
             "armed": bool(params.get("armed", False))}
        if params.get("response"):
            p["response"] = params["response"]
        for k in ("dwell", "ring"):
            if params.get(k) not in (None, ""):
                p[k] = float(params[k]) if k == "dwell" else int(params[k])
        # transponder runs its own RX loop for `seconds`; stop_tx is a harmless
        # belt-and-braces at the scheduled stop.
        return _rf_cmds(params) + [("transponder", p)], [("stop_tx", {})]

    if tech == "record":
        p = {"seconds": max(1.0, _duration_s(step))}
        band = params.get("band")
        if band == "range":
            lo, hi = float(params.get("f_lo_hz") or 0), float(params.get("f_hi_hz") or 0)
            if hi > lo:
                p["band"] = [lo, hi]
        elif isinstance(band, (list, tuple)) and len(band) == 2:
            p["band"] = [float(band[0]), float(band[1])]
        if params.get("label"):
            p["label"] = str(params["label"])[:16]
        # record is receive-only and self-limiting; nothing to stop. Its tuning
        # (center, RX gain) is applied first so the capture is where asked.
        return _rf_cmds(params) + [("record_iq", p)], []

    if tech == "replay":
        start = _rf_cmds(params) + [
            ("replay_recording", {"name": params.get("name", ""),
                                  "gain_db": float(params.get("gain_db", 0.0)),
                                  "level_mode": "relative"}),
            ("start_tx", {"mode": params.get("mode", "SINGLE_SHOT")})]
        return start, [("stop_tx", {})]

    # Any catalog entry that reaches here has no run mapping yet.
    return None, None


def builds_ahead(step: dict) -> bool:
    """True for a radiating step whose waveform can be built before its start:
    it builds or loads one, then keys with start_tx, and does not change the
    sample rate on the way."""
    if not step.get("radiates"):
        return False
    try:
        cmds, _ = step_commands(step)
    except ValueError:
        return False
    if not cmds or cmds[-1][0] != "start_tx":
        return False
    names = [n for n, _ in cmds]
    return RECONFIGURE_RATE not in names and any(n in _BUILD_CMDS for n in names)


def _duration_s(step: dict) -> float:
    from .mission_file import step_start_s, step_stop_s
    return step_stop_s(step) - step_start_s(step)


def _plan_radio(mission: dict, radio_id):
    return next((r for r in mission.get("radios", [])
                 if r.get("id") == radio_id), None)


def effective_rate_at(mission: dict, radio_id, t_s: float):
    """The sample rate a radio is at by time t_s: its plan start rate, then the
    rate of the latest set_rate step on that radio whose start is at or before
    t_s. Returns None if the plan sets no rate for the radio before then (the
    radio keeps whatever rate it was brought up at). Used to show a step's run
    rate and to decide whether a chosen file needs matching."""
    from .mission_file import step_start_s
    r = _plan_radio(mission, radio_id)
    rate = (r or {}).get("sample_rate")
    best_t = None
    for s in mission.get("steps", []):
        if s.get("radio") != radio_id:
            continue
        hz = rate_change_hz(s)
        if hz is None:
            continue
        st = step_start_s(s)
        if st <= t_s + 1e-6 and (best_t is None or st >= best_t):
            best_t, rate = st, hz
    try:
        return float(rate) if rate else None
    except (TypeError, ValueError):
        return None


class Event:
    __slots__ = ("t_s", "kind", "step")

    def __init__(self, t_s, kind, step):
        self.t_s = t_s          # seconds from mission start
        self.kind = kind        # "start" or "stop"
        self.step = step

    def __repr__(self):
        return f"<{self.kind} {self.step.get('id')} @ {self.t_s:g}s>"


def timeline_snapshot(mission: dict) -> dict:
    """The steps a run drives by the clock, as text by step id, to tell later
    what changed since the run started. It holds effective values and the
    commands a step would send, because Apply in the step editor fills in the
    default priority and turns 45 into 45.0 with nothing really changed. Location
    tasks follow the open plan."""
    from .mission_file import is_location_task, step_priority, step_start_s, step_stop_s
    return {s.get("id"): json.dumps(dict(s, params=None, commands=step_commands(s),
                                         priority=step_priority(s),
                                         schedule=[float(step_start_s(s)), float(step_stop_s(s))]),
                                    sort_keys=True, default=str)
            for s in mission.get("steps", []) if not is_location_task(s)}


def run_changes(started: dict, mission: dict) -> dict:
    """How the plan on screen differs from the steps a run started with: step
    ids that were removed, added, moved, or had other settings changed."""
    now = timeline_snapshot(mission)
    moved, settings = [], []
    for sid in sorted(set(started) & set(now)):
        if started[sid] == now[sid]:
            continue
        a, b = json.loads(started[sid]), json.loads(now[sid])
        if a.pop("schedule", None) != b.pop("schedule", None):
            moved.append(sid)
        if a != b:
            settings.append(sid)
    return {"removed": sorted(set(started) - set(now)),
            "added": sorted(set(now) - set(started)),
            "moved": moved, "settings": settings}


def plan_events(mission: dict) -> list:
    """The ordered timeline of actions for a plan: a start event at each step's
    start_s and a stop event at its stop_s, sorted by time (stops before starts
    at the same instant, so a radio is freed before the next step keys it).
    Steps the runner does not drive are left out."""
    from .mission_file import step_start_s, step_stop_s
    from .mission_file import is_location_task
    events = []
    timed = {}                            # radio: its timeline steps, for prepare times
    for step in mission.get("steps", []):
        if is_location_task(step):        # driven by its area, not the clock
            continue
        start_cmds, _ = step_commands(step)
        if start_cmds is None:
            continue
        events.append(Event(step_start_s(step), "start", step))
        events.append(Event(step_stop_s(step), "stop", step))
        timed.setdefault(step.get("radio"), []).append(step)
    # a step on other SMA jacks re-initializes its radio (about 30 s on an Epiq
    # card). Done at the start time it ate into the step, so the move happens
    # in the gap before it instead: lead time ahead of the start, but never
    # before the previous step on that radio has stopped. The waveform build
    # goes in the same gap, after the move (builds_ahead)
    rtype = {r.get("id"): r.get("type") for r in mission.get("radios", [])}
    for rid, steps in timed.items():
        lead = float(ports.REINIT_S.get(str(rtype.get(rid) or "").lower(), 30)) + 5.0
        prev_stop = 0.0
        for step in sorted(steps, key=step_start_s):
            start = step_start_s(step)
            if any(ports.step_ports(step)):
                t_prep = max(prev_stop, start - lead)
                if t_prep < start:
                    events.append(Event(t_prep, "prepare", step))
            if builds_ahead(step):
                t_build = max(prev_stop, start - BUILD_LEAD_S)
                if t_build < start:
                    events.append(Event(t_build, "build", step))
            prev_stop = max(prev_stop, step_stop_s(step))
    # stop, prepare, build, then start at the same instant, so a radio is freed
    # before it is moved, moved before its waveform is built, and built before
    # the next step keys it
    order = {"stop": 0, "prepare": 1, "build": 2, "start": 3}
    events.sort(key=lambda e: (e.t_s, order.get(e.kind, 2)))
    return events


def same_time_groups(events: list) -> list:
    """Split the ordered event list into runs that share one instant, keeping
    the plan_events order (stops before starts) inside each run."""
    groups = []
    for ev in events:
        if groups and abs(ev.t_s - groups[-1][0].t_s) < 1e-6:
            groups[-1].append(ev)
        else:
            groups.append([ev])
    return groups


class AuditLog:
    """A plain append-only log of edits, runs, confirms, triggers and stops.
    Each entry is (wall_time, kind, detail). Kept in memory; the tab shows it
    and can write it to a file."""

    def __init__(self):
        self.entries = []

    def add(self, kind: str, detail: str):
        self.entries.append((time.time(), kind, detail))
        return self.entries[-1]

    def lines(self) -> list:
        out = []
        for t, kind, detail in self.entries:
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))
            out.append(f"{ts}  {kind:8s}  {detail}")
        return out

    def clear(self):
        self.entries = []


async def send_stop_tx(hosts, missed=None) -> int:
    """stop_tx to every radio together, so a radio that does not answer holds up
    no other. Returns how many answered; the ones that did not are added to
    the list `missed` when one is given."""
    hosts = list(hosts)

    async def _one(h):
        try:
            await h.cmd("stop_tx", {}, timeout=5.0)
            return True
        except Exception:
            return False
    got = await asyncio.gather(*(_one(h) for h in hosts))
    if missed is not None:
        missed.extend(h for h, ok in zip(hosts, got) if not ok)
    return sum(got)


def _host_label(host) -> str:
    """A radio's name for the audit log: its serial, else its address."""
    return str(getattr(host, "serial", None) or getattr(host, "addr", None) or host)


class MissionRunner:
    """Run a validated mission against live radios. One runner per tab.

    get_host(radio_id) returns a HostLink (with .cmd) or None if that radio is
    not connected. audit is an AuditLog. speed compresses the timeline for
    tests (1.0 = real time, 0.0 = fire every action in order with no waiting).
    on_change is called after each action so the UI can refresh the run state.
    """

    def __init__(self, get_host, audit: AuditLog, speed: float = 1.0,
                 on_change=None, get_any_host=None):
        self.get_host = get_host
        # the link to a radio even while it is stale (no recent STATUS), so a
        # stop still goes out; get_host alone returns None for a stale radio
        self.get_any_host = get_any_host or get_host
        self.audit = audit
        self.speed = speed
        self.on_change = on_change
        self._task = None
        self.running = False
        self.active_steps = set()       # step ids currently keyed
        self.finished = False
        self._tx_moved = set()          # radios whose TX a step put on its own frequency
        self._now_tasks = {}            # step id: auto-stop task (location-triggered starts)
        self._active = {}               # step id: step dict, for every keyed step
        self._waiting = {}              # radio id: (step, dur, want) task waiting for the radio
        self.plan_source = None         # returns the open plan, for a location task's bring-up
        self._plan = None               # the plan this run is on
        self._stops = 0                 # counts STOP ALL, so a slow bring-up does not key after one
        self._rx_held = {}              # step id: radio, for a Decode step that opened the RX attenuator
        self.run_stop_s = None          # last stop time of the plan the current run started with
        self.atten_not_reset = []       # radios whose RX attenuator the last STOP ALL did not get back to 120 dB
        self._bring_locks = {}          # radio id: lock, one bring-up at a time
        self._bring_failed = {}         # radio id: time of its last failed bring-up
        self._lost_slot = set()         # steps skipped because a re-init used up their slot
        self._stopped_by_hand = set()   # timed steps stopped by hand or taken by a location task: their planned stop sends nothing
        self.started_steps = {}         # timeline_snapshot of the plan this run started on
        self.run_t0 = None              # monotonic start of the timed part of a run
        self.init_busy = {}             # radio id: monotonic start of an init in progress
        self._dead = {}                 # radio id: why it could not be brought up in this run
        self.run_error = None           # what ended or cut short the last run, for the run pill
        self._built = {}                # step id: (radio generation, STOP ALL count, commands left), waveform built ahead
        self._build_tasks = {}          # step id: task building its waveform ahead of the start
        self._radio_gen = {}            # radio id: counts setups and re-inits, so a waveform built ahead is known to be still loaded
        self._radio_locks = {}          # radio id: lock, one step setting up a radio at a time
        self._decoding = {}             # step id: radio, for a Decode step that started RX for its plan
        self._stop_retries = {}         # radio label: task retrying a stop_tx that STOP ALL TX could not deliver
        self.stop_unconfirmed = []      # radios still not answering stop_tx after STOP ALL TX

    def is_running(self) -> bool:
        return self.running

    def elapsed_s(self):
        """Seconds into the timeline (plan time) while a relative plan runs,
        None otherwise: the position line the planner draws across the lanes.
        Counted from the moment the timeline started firing, after the start
        rates were applied."""
        t0 = getattr(self, "run_t0", None)
        if not self.running or t0 is None or self.speed <= 0:
            return None
        return (time.monotonic() - t0) / self.speed

    async def run(self, mission: dict):
        """Run the whole timeline. Assumes the operator has already confirmed.
        Returns when the last stop fires or stop_all() cancels it."""
        if self.running:
            self.audit.add("run", "run requested while already running; ignored")
            return
        self.running = True
        self.finished = False
        self.run_t0 = None
        # steps keyed by a location trigger stay keyed (and known) across a
        # timeline run, so the priority check sees them; STOP ALL clears all
        self._tx_moved = set()
        self._plan = mission
        self.started_steps = timeline_snapshot(mission)
        self._lost_slot = set()
        self._stopped_by_hand = set()
        self._bring_failed = {}
        self._dead = {}
        self._built = {}
        self._build_tasks = {}
        self.run_error = None
        # a stop from the last STOP ALL TX still retrying must not stop this run
        self._cancel_stop_retries()
        self.audit.add("run", f"start mission '{mission.get('name', '?')}' "
                              f"({sum(1 for e in plan_events(mission) if e.kind == 'start')} "
                              f"runnable steps)")
        self._task = asyncio.current_task()
        # a clock-anchored plan fires each step at its absolute UTC time; an
        # on_tx plan runs offsets from now.
        from .mission_file import anchor_midnight_epoch
        anchor_epoch = anchor_midnight_epoch(mission)
        events = plan_events(mission)
        # the run's end, fixed now, so the run state total does not follow edits made mid run
        self.run_stop_s = max((e.t_s for e in events if e.kind == "stop"), default=0.0)
        self._timeline_radios = {e.step.get("radio") for e in events if e.kind == "start"}
        run_notice.starting(True)
        try:
            for rid in sorted(self._timeline_radios, key=str):
                host = self.get_host(rid)
                if host is not None and self.active_on(rid) is None:
                    await self._stop_running_tx(host, rid, "before the plan starts")
            await self._apply_start_rates(mission)
            if not self._all_radios_dead():
                await self._drive(events, anchor_epoch)
            if self._all_radios_dead():
                self.audit.add("run", f"mission ended early: {self.run_error}")
            else:
                self.finished = True
                self.audit.add("run", "mission finished")
        except asyncio.CancelledError:
            self.audit.add("run", "mission run cancelled")
            raise
        finally:
            self.running = False
            run_notice.starting(False)
            if self.on_change:
                self.on_change()

    def _all_radios_dead(self) -> bool:
        """True when no radio on the timeline can run, so the run has nothing to do."""
        radios = getattr(self, "_timeline_radios", None)
        return bool(self._dead) and bool(radios) and all(r in self._dead for r in radios)

    def _mark_dead(self, rid, why):
        """A radio could not be brought up during a run. Its steps are skipped for
        the rest of the run with no more tries: with no 10 MHz on an external clock
        each try stops the radio service."""
        if rid in self._dead:
            return
        self._dead[rid] = why
        self.run_error = "; ".join(f"{r}: {w}" for r, w in self._dead.items())
        self.audit.add("run", f"radio {rid} not brought up, its steps in this run are "
                              f"skipped: {why}")
        if self.on_change:
            self.on_change()

    async def _stop_running_tx(self, host, rid, when):
        """Stop a TX left running outside the plan. A step's waveform cannot load
        over it, and the plan's TX attenuation would apply to the old waveform."""
        st = getattr(host, "status", None)
        if st is None or not int(getattr(st, "flags", 0)) & Status.FLAG_TX_ACTIVE:
            return
        self.audit.add("run", f"radio {rid} was already transmitting: stopping it {when}")
        try:
            await host.cmd("stop_tx", {}, timeout=10.0)
        except Exception as e:
            self.audit.add("cmd", f"radio {rid}: stopping the running TX: error {e}")

    async def _drive(self, events: list, anchor_epoch=None):
        t0 = self.run_t0 = time.monotonic()
        run_notice.starting(False)
        for group in same_time_groups(events):
            if self._all_radios_dead():
                return
            t_s = group[0].t_s
            if anchor_epoch is not None:
                # absolute wall-clock scheduling
                delay = (anchor_epoch + t_s) - time.time()
                if delay < -1.0:
                    keep = []
                    for ev in group:
                        if ev.kind == "build":
                            continue              # its start is late too; it builds then
                        if ev.kind == "start":
                            self.audit.add(
                                "run", f"step {ev.step.get('id')} start time "
                                       f"already passed ({-delay:.0f}s ago); skipped")
                            self._lost_slot.add(ev.step.get("id"))
                        else:
                            keep.append(ev)
                    group = keep
            else:
                delay = (t0 + t_s * self.speed) - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            if group:
                await self._fire(group)
            if self.on_change:
                self.on_change()

    async def _fire(self, group: list):
        """Fire the events that share one instant. Each radio's events run in
        order (a stop before the next start on that radio); different radios
        run in parallel, so radios with the same start time key together rather
        than one waiting for another's waveform build. Alignment between radios
        is then down to the link and command delay."""
        by_radio = {}
        for ev in group:
            by_radio.setdefault(ev.step.get("radio"), []).append(ev)

        async def _one_radio(evs):
            for ev in evs:
                if ev.kind == "start":
                    await self._start_timeline_step(ev.step)
                elif ev.kind == "build":
                    self._build_ahead(ev.step)
                elif ev.kind == "prepare":
                    await self._prepare_ports(ev.step)
                elif ev.step.get("id") not in self._stopped_by_hand:
                    await self._stop_step(ev.step)

        await asyncio.gather(*(_one_radio(evs) for evs in by_radio.values()))

    async def _prepare_ports(self, step):
        """Move a radio onto a coming step's ports ahead of its start (the
        "prepare" event plan_events puts in the gap). Skipped while something
        is keyed on the radio (a location task): the step's own start then
        does the move."""
        rid = step.get("radio")
        sid = step.get("id")
        host = self.get_host(rid)
        if host is None or rid in self._dead:
            return
        other = self.active_on(rid)
        if other is not None:
            self.audit.add("run", f"{sid} on {rid}: port move ahead of the step skipped, "
                                  f"{other.get('id')} holds the radio")
            return
        await self._apply_rate(host, rid, f"{sid} (ahead of its start)", None, step=step)

    def _build_ahead(self, step):
        """Build a coming step's waveform now, in the background so other radios'
        events are not held up; the step's start waits for it and then sends only
        start_tx. Skipped while a step is keyed on the radio: the start then
        builds as before."""
        sid = step.get("id")
        rid = step.get("radio")
        if rid in self._dead or self.get_host(rid) is None:
            return
        other = self.active_on(rid)
        if other is not None:
            self.audit.add("run", f"{sid} on {rid}: waveform not built ahead, "
                                  f"{other.get('id')} holds the radio")
            return
        self._build_tasks[sid] = asyncio.create_task(self._start_step(step, build_only=True))

    async def _wait_build(self, sid):
        """Wait for a step's waveform build ahead, if one is running."""
        t = self._build_tasks.pop(sid, None)
        if t is None:
            return
        try:
            await t
        except asyncio.CancelledError:
            if not t.cancelled():
                raise
        except Exception as e:
            self.audit.add("run", f"step {sid}: build ahead error {e}")

    # priority between the timeline and location tasks

    def active_on(self, rid):
        """The keyed step on a radio, or None."""
        for sid in self.active_steps:
            st = self._active.get(sid)
            if st is not None and st.get("radio") == rid:
                return st
        return None

    async def _start_timeline_step(self, step):
        """A timeline step coming due. If a location task holds the radio, the
        higher priority wins: the task is stopped, or the step is skipped."""
        from .mission_file import is_location_task, step_priority, step_start_s, step_stop_s
        await self._wait_build(step.get("id"))
        late = self._seconds_late(step_start_s(step))
        if late > 1.0:
            sid = step.get("id")
            if self._now_s() >= step_stop_s(step):
                self._lost_slot.add(sid)
                self.audit.add("run", f"step {sid} on {step.get('radio')} skipped: its whole slot passed "
                                      f"while the radio was busy, {late:.0f} s late")
                return
            self.audit.add("run", f"step {sid} on {step.get('radio')} started {late:.0f} s late")
        other = self.active_on(step.get("radio"))
        if other is not None and is_location_task(other):
            if step_priority(step) < step_priority(other):
                self.audit.add("run", f"step {step.get('id')} (priority "
                                      f"{step_priority(step)}) takes radio "
                                      f"{step.get('radio')} from location task "
                                      f"{other.get('id')} (priority {step_priority(other)})")
                await self.stop_step_now(other, reason="pre-empted by the timeline")
            else:
                self.audit.add("run", f"step {step.get('id')} (priority "
                                      f"{step_priority(step)}) skipped: radio "
                                      f"{step.get('radio')} busy with location task "
                                      f"{other.get('id')} (priority {step_priority(other)})")
                self._lost_slot.add(step.get("id"))
                return
        await self._start_step(step)

    def init_status(self) -> str:
        """Which radios are initializing right now, for the status pill. Empty when none."""
        now = time.monotonic()
        return ", ".join(f"{r} {now - t:.0f} s" for r, t in sorted(self.init_busy.items(), key=str))

    def _now_s(self) -> float:
        """Seconds into the run on the plan's timeline; 0 when no timed run is going."""
        if self.run_t0 is None or not self.speed:
            return 0.0
        return (time.monotonic() - self.run_t0) / self.speed

    def _seconds_late(self, t_s: float) -> float:
        return max(0.0, self._now_s() - t_s) if self.run_t0 is not None and self.speed else 0.0

    async def _start_step(self, step, build_only=False):
        """Set a step up on its radio and key it. build_only does the set up only
        (tuning, ports, the waveform build), up to start_tx, and keeps what is
        left for the step's start; that start keys at once if nothing has set up
        or re-initialized the radio since."""
        sid = step.get("id")
        rid = step.get("radio")
        host = self.get_host(rid)
        start_cmds, _ = step_commands(step)
        stops = self._stops
        if host is None:
            if not build_only:
                self.audit.add("run", f"step {sid}: radio {rid} not connected; skipped")
            return
        if self.running and rid in self._dead:
            return
        async with self._radio_locks.setdefault(rid, asyncio.Lock()):
            built = self._built.pop(sid, None)
            if (not build_only and built is not None
                    and built[:2] == (self._radio_gen.get(rid, 0), stops)
                    and getattr(host, "radio_state", None) != RadioState.UNINIT):
                self.audit.add("run", f"step {sid} on {rid}: waveform built ahead, keying")
                await self._send_start_cmds(host, sid, rid, step, built[2], stops)
                return
            self._radio_gen[rid] = self._radio_gen.get(rid, 0) + 1
            # not initialized (a location task, or a radio that restarted mid run)
            if getattr(host, "radio_state", None) == RadioState.UNINIT:
                if not await self._apply_rate(host, rid, sid, rate_change_hz(step), step=step):
                    return
                if stops != self._stops:
                    self.audit.add("run", f"step {sid} on {rid}: not keyed, STOP ALL during init")
                    return
            start_cmds = await self._tx_back_to_rx(host, rid, step, list(start_cmds))
            # a step on other SMA jacks than the radio is on: re-initialize first
            # (the set_rate path below does rate and ports in one init)
            if any(ports.step_ports(step)) and not any(n == RECONFIGURE_RATE
                                                         for n, _ in start_cmds):
                if not await self._apply_rate(host, rid, sid, None, step=step):
                    return
            if build_only:
                k = next((i for i, (n, _) in enumerate(start_cmds) if n == "start_tx"),
                         len(start_cmds))
                if not await self._send_start_cmds(host, sid, rid, step, start_cmds[:k],
                                                   stops, build_only=True):
                    return
                # anything since (a re-init, another step's set up) bumps the count
                self._built[sid] = (self._radio_gen.get(rid, 0), stops, start_cmds[k:])
                self.audit.add("run", f"step {sid} on {rid}: waveform built ahead of its start")
                return
            await self._send_start_cmds(host, sid, rid, step, start_cmds, stops)

    async def _send_start_cmds(self, host, sid, rid, step, start_cmds, stops,
                               build_only=False) -> bool:
        """Send a step's start commands in order, stopping at the first failure.
        Marks the step keyed at the end unless build_only. True when all went."""
        plan_path = None
        plan_set = False
        for name, params in start_cmds:
            if stops != self._stops:
                self.audit.add("run", f"step {sid} on {rid}: not keyed, STOP ALL while it was starting")
                return False
            if name == RECONFIGURE_RATE:
                if not await self._apply_rate(host, rid, sid,
                                              float(params["sample_rate"]), step=step):
                    return False
                continue
            if params.get(PLAN_FROM_GEN):
                if not plan_path:
                    self.audit.add("cmd", f"step {sid} on {rid}: gen_scenario gave no decode plan")
                    await self._decode_failed(host, sid, rid, step)
                    return False
                params = {"plan_path": plan_path}
            if name == "gen_scenario":
                params = await self._spec_over_the_link(host, sid, rid, params)
                if params is None:
                    await self._decode_failed(host, sid, rid, step)
                    return False
            # a record is receive only and changes nothing the console shows;
            # mark it so the operator console can show REC while it runs
            if name == "record_iq":
                rec_state.start(rid, params.get("label") or sid,
                                float(params.get("seconds") or 0.0), "mission")
            try:
                ack = await host.cmd(name, params, timeout=_cmd_timeout_s(name, params))
                ok = getattr(ack, "status", 1) == 0
                detail = f"step {sid} on {rid}: {name} {'ok' if ok else 'FAILED'}"
                err = (getattr(ack, "result", None) or {}).get("err")
                if not ok:
                    detail += f" ({err})"
                self.audit.add("cmd", detail)
                if not ok and name == "atten_set" and _no_attenuator(err):
                    # a radio with no attenuator at all (a ground radio): there is
                    # nothing to set, so the step goes on and still starts RX. One
                    # that has an attenuator but refused the value, or lost it,
                    # stops here as before.
                    self.audit.add("run", f"step {sid} on {rid}: no RX attenuator on "
                                          f"this radio, step goes on without it")
                    continue
                if (not ok and name == "atten_set" and plan_set and _atten_gone(err)
                        and step.get("technique") == "scenario_decode"):
                    # the radio's attenuator is off the USB (missing on the bench,
                    # or lost in flight). The step's own plan is loaded, so RX goes
                    # on and decodes at whatever the attenuator last held; signal
                    # levels from this step are not to be trusted. Any other
                    # refusal still stops the step and RX.
                    self.audit.add("run", f"step {sid} on {rid}: WARNING RX attenuator not "
                                          f"connected, RX starts at an unknown attenuation "
                                          f"({err})")
                    continue
                if not ok:
                    await self._decode_failed(host, sid, rid, step)
                    return False
                if name == "set_plan":
                    plan_set = True
                if name == "gen_scenario":
                    plan_path = (getattr(ack, "result", None) or {}).get("plan_path")
                if name == "atten_set":
                    self._rx_held[sid] = rid
                if name == "start_rx" and step.get("technique") == "scenario_decode":
                    self._decoding[sid] = rid
            except Exception as e:
                self.audit.add("cmd", f"step {sid} on {rid}: {name} error {e}")
                await self._decode_failed(host, sid, rid, step)
                return False
            finally:
                # the record ack comes back when the capture is written
                if name == "record_iq":
                    rec_state.stop(rid)
        if build_only:
            return True
        if step.get("radiates"):
            self.active_steps.add(sid)
            self._active[sid] = step
        self.audit.add("run", f"step {sid} started on {rid}")
        return True

    async def _decode_failed(self, host, sid, rid, step):
        """A Decode step that failed part way: put back what it changed, so the
        radio is not left receiving on the last plan with its RX attenuator open.
        The attenuator goes back to 120 dB if this step opened it, and RX stops
        unless another Decode step on the radio is still using it."""
        if step.get("technique") != "scenario_decode":
            return
        if self._rx_held.pop(sid, None) is not None:
            try:
                ack = await host.cmd("atten_set", {"db": 120.0}, timeout=10.0)
                ok = getattr(ack, "status", 1) == 0
                self.audit.add("run", f"step {sid} on {rid}: failed, RX attenuator "
                                      + ("back to 120 dB" if ok else "NOT back to 120 dB"))
            except Exception as e:
                self.audit.add("run", f"step {sid} on {rid}: failed, RX attenuator "
                                      f"NOT back to 120 dB ({e})")
        self._decoding.pop(sid, None)
        others = [o for o, r in self._decoding.items() if r == rid]
        if others:
            self.audit.add("run", f"step {sid} on {rid}: failed, RX left on for "
                                  f"{', '.join(sorted(others))}")
            return
        try:
            await host.cmd("stop_rx", {}, timeout=10.0)
            self.audit.add("run", f"step {sid} on {rid}: failed, RX stopped so the "
                                  f"radio does not decode an old plan")
        except Exception as e:
            self.audit.add("cmd", f"step {sid} on {rid}: stop_rx error {e}")

    async def _spec_over_the_link(self, host, sid, rid, params):
        """A console spec that is too big for one command datagram goes over
        the FILE_* transfer first (host.send_file, the playback panel's way)
        and gen_scenario then reads it from the inbox as spec_inbox. A small
        spec and a radio path are returned unchanged. None on a failed push,
        which the audit log explains."""
        spec = params.get("spec")
        if not isinstance(spec, dict) or not scenario_spec.needs_inbox(spec):
            return params
        blob = scenario_spec.spec_blob(spec)
        name = f"mission_{sid}.json"
        try:
            stored = await host.send_file(name, blob, timeout=60.0)
        except Exception as e:
            self.audit.add("cmd", f"step {sid} on {rid}: spec push ({len(blob)} B) "
                                  f"error {e}")
            return None
        self.audit.add("cmd", f"step {sid} on {rid}: spec ({len(blob)} B) pushed as "
                              f"{stored or name}")
        out = {k: v for k, v in params.items() if k != "spec"}
        out["spec_inbox"] = stored or name
        return out

    async def _tx_back_to_rx(self, host, rid, step, cmds):
        """A blank TX frequency means TX on the RX frequency. Nothing needs
        sending for that unless an earlier step of this run put this radio's TX
        on its own frequency (set_center keeps that gap): then add a
        set_tx_center to the RX center, which is the step's RF center or, if
        that is blank too, the radio's current center from get_config."""
        if any(n == "set_tx_center" for n, _ in cmds):
            self._tx_moved.add(rid)
            return cmds
        if rid not in self._tx_moved or not step.get("radiates"):
            return cmds
        rx = next((float(p["hz"]) for n, p in cmds if n == "set_center"), None)
        if rx is None:
            try:
                cfg = await host.cmd("get_config", {}, timeout=10.0)
                rx = float((getattr(cfg, "result", None) or {}).get("center_hz") or 0) or None
            except Exception as e:
                self.audit.add("cmd", f"step {step.get('id')} on {rid}: get_config error {e}")
        if rx is None:
            self.audit.add("run", f"step {step.get('id')} on {rid}: RX center unknown, "
                                  f"TX left where the last step put it")
            return cmds
        self._tx_moved.discard(rid)
        at = next((i for i, (n, _) in enumerate(cmds) if n not in _RF_CMDS), len(cmds))
        return cmds[:at] + [("set_tx_center", {"hz": rx})] + cmds[at:]

    async def _stop_step(self, step):
        sid = step.get("id")
        rid = step.get("radio")
        host = self.get_host(rid)
        _, stop_cmds = step_commands(step)
        if self.running and rid in self._dead and sid not in self.active_steps:
            return                               # never started, nothing to stop
        if sid in self._lost_slot:
            return                               # skipped when it came due, nothing to stop
        self._rx_held.pop(sid, None)
        self._decoding.pop(sid, None)
        self.active_steps.discard(sid)
        self._active.pop(sid, None)
        t = self._now_tasks.pop(sid, None)
        if t is not None and not t.done():
            t.cancel()
        if not stop_cmds:
            self.audit.add("run", f"step {sid} stopped")
        elif host is None:
            # the radio is stale or gone. A radiating step must not be left
            # keyed: send the stop over whatever link there is, and keep
            # trying in the background until the radio answers
            self.audit.add("run", f"step {sid}: radio {rid} not answering at its stop; "
                                  f"sending the stop anyway and retrying until it answers")
            self._stop_retry(sid, rid, stop_cmds)
        else:
            for name, params in stop_cmds:
                try:
                    await host.cmd(name, params, timeout=10.0)
                except Exception as e:
                    self.audit.add("cmd", f"step {sid} on {rid}: {name} error {e}")
                    if name == "stop_tx":
                        self._stop_retry(sid, rid, [(name, params)])
            self.audit.add("run", f"step {sid} stopped on {rid}")
        self._release_radio(rid)

    # how long a stop keeps being retried on a radio that does not answer
    STOP_RETRY_S = 120.0
    STOP_RETRY_GAP_S = 2.0      # between STOP ALL TX retries

    def _stop_retry(self, sid, rid, stop_cmds):
        """Retry a step's stop commands until one round is acknowledged or
        STOP_RETRY_S passes; the outcome goes in the audit log either way."""
        async def _go():
            t_end = time.monotonic() + self.STOP_RETRY_S
            n = 0
            while time.monotonic() < t_end:
                n += 1
                host = self.get_any_host(rid)
                if host is not None:
                    try:
                        ok = True
                        for name, params in stop_cmds:
                            ack = await host.cmd(name, params, timeout=5.0)
                            ok = ok and getattr(ack, "status", 1) == 0
                        if ok:
                            self.audit.add("run", f"step {sid} stopped on {rid} "
                                                  f"(after {n} attempt(s))")
                            if self.on_change:
                                self.on_change()
                            return
                    except Exception:
                        pass
                await asyncio.sleep(2.0)
            self.audit.add("run", f"step {sid} on {rid}: STOP NOT CONFIRMED after "
                                  f"{self.STOP_RETRY_S:g} s; press STOP ALL TX and check the radio")
            if self.on_change:
                self.on_change()
        try:
            asyncio.get_event_loop().create_task(_go())
        except RuntimeError:
            pass

    def _retry_stop_all(self, host):
        """Keep sending stop_tx to a radio that did not answer STOP ALL TX until
        it answers or STOP_RETRY_S passes. A new Run mission cancels the retry."""
        label = _host_label(host)
        old = self._stop_retries.pop(label, None)
        if old is not None and not old.done():
            old.cancel()

        async def _go():
            t_end = time.monotonic() + self.STOP_RETRY_S
            n = 0
            while time.monotonic() < t_end:
                await asyncio.sleep(self.STOP_RETRY_GAP_S)
                n += 1
                h = self.get_any_host(label) or host
                try:
                    ack = await h.cmd("stop_tx", {}, timeout=5.0)
                    if getattr(ack, "status", 1) == 0:
                        self.audit.add("stop", f"STOP ALL TX: {label} answered stop_tx "
                                               f"(retry {n})")
                        if label in self.stop_unconfirmed:
                            self.stop_unconfirmed.remove(label)
                        self._changed()
                        return
                except Exception:
                    pass
            self.audit.add("stop", f"STOP ALL TX: {label} STOP NOT CONFIRMED after "
                                   f"{self.STOP_RETRY_S:g} s; check the radio")
            self._changed()

        try:
            self._stop_retries[label] = asyncio.get_event_loop().create_task(_go())
        except RuntimeError:
            pass

    def _cancel_stop_retries(self):
        for t in self._stop_retries.values():
            if not t.done():
                t.cancel()
        self._stop_retries = {}
        self.stop_unconfirmed = []

    def _release_radio(self, rid):
        """A radio just went free: start the location task waiting for it, if
        its condition still holds (the platform is still in its area)."""
        w = self._waiting.pop(rid, None)
        if w is None:
            return
        step, dur, want = w
        if want is not None and not want():
            self.audit.add("run", f"location task {step.get('id')} waited for radio "
                                  f"{rid} but its condition no longer holds; dropped")
            return
        self.audit.add("run", f"radio {rid} free: starting the waiting location task "
                              f"{step.get('id')}")
        asyncio.get_event_loop().create_task(self.start_step_now(step, dur=dur, want=want))

    async def _apply_rate(self, host, rid, sid, hz, step=None) -> bool:
        """Re-initialize a radio to sample rate hz (None = keep its rate) and,
        when `step` carries an RX / TX port choice, onto those ports, keeping
        its other settings (read back with get_config). A no-op if the radio is
        already there. Returns True on success. Used by a set_rate step, by a
        step on other ports than the radio is on, and by the per-radio start
        rates at run start."""
        try:
            cfg = await host.cmd("get_config", {}, timeout=10.0)
        except Exception as e:
            self.audit.add("cmd", f"{sid} on {rid}: get_config error {e}")
            return False
        snap = getattr(cfg, "result", None) or {}
        if not snap.get("inited"):
            return await self._bring_up(host, rid, sid, hz, step, snap)
        cur = float(snap.get("achieved_rate") or snap.get("sample_rate") or 0)
        rate_ok = hz is None or (cur and abs(cur - hz) <= max(1.0, hz * 1e-3))
        # a plan's rate is the radio's rate: a TX card left on its own rate from
        # the console would alias every waveform built for the plan's band
        txr = float(snap.get("tx_sample_rate") or 0)
        tx_off = hz is not None and txr and abs(txr - hz) > max(1.0, hz * 1e-3)
        if tx_off:
            rate_ok = False
        rxp, txp = ports.step_ports(step) if step else (None, None)
        rtype = snap.get("kind")
        # RX none: keep the radio's RX if it can stay with this TX, else a
        # harmless port (ports.rx_for_none); from here on it is a real port
        rx_none = rxp == ports.RX_NONE
        rxp, txp = ports.resolve(snap, rtype, rxp, txp)
        port_change = ports.needs_reinit(snap, rtype, rxp, txp)
        if rate_ok and not port_change:
            if hz is not None:
                self.audit.add("run", f"{sid} on {rid}: already at "
                                      f"{hz / 1e6:g} MSps")
            return True
        if hz is None:
            hz = cur or float(snap.get("sample_rate") or 0)
        # a host that does not report its state counts as RX running
        was_rx = getattr(host, "radio_state", RadioState.STREAMING) == RadioState.STREAMING
        init_p = _init_params_from_snapshot(snap, hz)
        why = []
        if tx_off:
            init_p.get("extras", {}).pop("tx_sample_rate", None)
            why.append(f"TX {txr / 1e6:g} -> {hz / 1e6:g} MSps")
        if not rate_ok and abs(cur - hz) > max(1.0, hz * 1e-3):
            why.append(f"{cur / 1e6:g} -> {hz / 1e6:g} MSps")
        if port_change:
            pf = ports.init_fields(rtype, rxp, txp)
            ex = dict(init_p.get("extras") or {})
            ex.update(pf.pop("extras", {}))
            init_p.update(pf)
            if ex:
                init_p["extras"] = ex
            was = ports.label(*ports.current_ports(snap, rtype)) or "unknown ports"
            why.append(f"ports {was} -> {ports.label(rxp, txp)}"
                       + (" (step has RX none)" if rx_none else ""))
        self.audit.add("run", f"{sid} on {rid}: re-init {', '.join(why)} "
                              f"(RX stops briefly)")
        self._radio_gen[rid] = self._radio_gen.get(rid, 0) + 1
        try:
            self.init_busy[rid] = time.monotonic()
            run_notice.begin(rid)
            ack = await host.cmd("init", init_p, timeout=60.0)
        except Exception as e:
            self.audit.add("cmd", f"{sid} on {rid}: init error {e}")
            return False
        finally:
            self.init_busy.pop(rid, None)
            run_notice.end(rid)
        ok = getattr(ack, "status", 1) == 0
        if not ok:
            err = (getattr(ack, "result", None) or {}).get("err")
            self.audit.add("cmd", f"{sid} on {rid}: init FAILED ({err})")
            return False
        got = float((getattr(ack, "result", None) or {}).get("achieved_rate") or hz)
        self.audit.add("cmd", f"{sid} on {rid}: init ok at {got / 1e6:g} MSps")
        # RX comes back only if it was running or this step listens. A TX only
        # step, or a radio brought up with RX off, stays with no RX stream.
        receives = bool(step) and catalog.receives(step.get("technique"))
        if rx_none or not (was_rx or receives):
            self.audit.add("run", f"{sid} on {rid}: RX left off")
            return True
        try:
            await host.cmd("start_rx", {}, timeout=10.0)
        except Exception as e:
            self.audit.add("cmd", f"{sid} on {rid}: start_rx error {e}")
        return True

    def _current_plan(self):
        if self.running and self._plan is not None:
            return self._plan
        return self.plan_source() if self.plan_source else self._plan

    async def _bring_up(self, host, rid, sid, hz, step, snap) -> bool:
        """Initialize a radio that is not initialized, from the plan
        (bring_up_plan), and start RX unless all its steps say RX none. Keys
        nothing. In a run a failure ends that radio's part of it (_mark_dead); outside
        a run a failed init is not repeated for BRING_UP_RETRY_S: with no 10 MHz on an
        external clock each try stops the radio service."""
        lock = self._bring_locks.setdefault(rid, asyncio.Lock())
        async with lock:
            if self.running and rid in self._dead:
                return False
            failed = self._bring_failed.get(rid)
            if failed is not None and time.monotonic() - failed < BRING_UP_RETRY_S:
                self.audit.add("run", f"{sid} on {rid}: skipped, its init just failed")
                return False
            try:
                cfg = await host.cmd("get_config", {}, timeout=10.0)
                snap = getattr(cfg, "result", None) or snap
            except Exception:
                pass
            if snap.get("inited"):              # another step brought it up meanwhile
                return True
            plan = self._current_plan() or {}
            radio = next((r for r in plan.get("radios", []) if r.get("id") == rid), None)
            if radio is None:
                self.audit.add("run", f"{sid} on {rid}: radio not initialized and not "
                                      f"in the plan, so no settings for it")
                return False
            how = bring_up_plan(radio, plan_steps_on(plan, rid, step), hz, snap.get("ports"))
            if how.get("err"):
                self.audit.add("run", f"{sid} on {rid}: radio not initialized and cannot "
                                      f"bring it up: {how['err']}")
                if self.running:
                    self._mark_dead(rid, how["err"])
                return False
            self.audit.add("run", f"{sid} on {rid}: radio not initialized, bringing it "
                                  f"up: {how['text']}")
            self._radio_gen[rid] = self._radio_gen.get(rid, 0) + 1
            try:
                self.init_busy[rid] = time.monotonic()
                run_notice.begin(rid)
                try:
                    ack = await host.cmd("init", how["params"], timeout=BRING_UP_TIMEOUT_S)
                finally:
                    self.init_busy.pop(rid, None)
                    run_notice.end(rid)
            except Exception as e:
                self._bring_failed[rid] = time.monotonic()
                self.audit.add("cmd", f"{sid} on {rid}: init error {e}")
                if self.running:
                    self._mark_dead(rid, f"init error {e}")
                return False
            if getattr(ack, "status", 1) != 0:
                err = (getattr(ack, "result", None) or {}).get("err")
                self._bring_failed[rid] = time.monotonic()
                self.audit.add("cmd", f"{sid} on {rid}: init FAILED ({err})")
                if self.running:
                    self._mark_dead(rid, f"init failed: {err}")
                return False
            got = float((getattr(ack, "result", None) or {}).get("achieved_rate")
                        or how["params"]["sample_rate"])
            self.audit.add("cmd", f"{sid} on {rid}: init ok at {got / 1e6:g} MSps")
            if not how["rx_on"]:
                self.audit.add("run", f"{sid} on {rid}: RX left off")
                return True
            try:
                await host.cmd("start_rx", {}, timeout=10.0)
            except Exception as e:
                self.audit.add("cmd", f"{sid} on {rid}: start_rx error {e}")
            return True

    async def _apply_start_rates(self, mission: dict):
        """Set each planned radio to its start sample rate before the timeline
        runs, so the first step on it already has the right rate. A radio with
        no start rate is left at whatever it was brought up at, and one that is
        not initialized is brought up now if the timeline uses it. Offline
        radios are skipped (the timeline then skips their steps too)."""
        for r in mission.get("radios", []):
            hz = r.get("sample_rate")
            rid = r.get("id")
            host = self.get_host(rid)
            if host is not None and getattr(host, "radio_state", None) == RadioState.UNINIT:
                if not any(e.kind == "start" and e.step.get("radio") == rid
                           for e in plan_events(mission)):
                    continue
                await self._apply_rate(host, rid, f"{rid} start", float(hz) if hz else None)
                continue
            if not hz:
                continue
            if host is None:
                self.audit.add("run", f"radio {rid}: start rate "
                                      f"{float(hz) / 1e6:g} MSps skipped, not "
                                      f"connected")
                continue
            await self._apply_rate(host, rid, f"{rid} start", float(hz))

    # single steps outside the timeline (location triggers)

    async def start_step_now(self, step, dur=None, want=None) -> str:
        """Key one step right now, outside the timeline: a start area the
        operator confirmed. It stops after dur seconds (default: the step's
        planned length, or a location task's dwell or max), on stop_step_now
        or on STOP ALL TX. If another step holds the radio the higher priority
        wins: a lower-priority holder is stopped; otherwise this step waits
        for the radio (want() is asked then whether it is still wanted).
        Returns "started", "waiting", "running" or "failed"."""
        from .mission_file import (step_start_s, step_stop_s, step_priority,
                                  is_location_task, task_length_s)
        sid = step.get("id")
        rid = step.get("radio")
        if sid in self.active_steps:
            self.audit.add("run", f"step {sid}: already running; trigger ignored")
            return "running"
        other = self.active_on(rid)
        if other is not None and other.get("id") != sid:
            if step_priority(step) < step_priority(other):
                self.audit.add("run", f"location task {sid} (priority {step_priority(step)}) "
                                      f"takes radio {rid} from {other.get('id')} "
                                      f"(priority {step_priority(other)})")
                self._waiting.pop(rid, None)
                if not is_location_task(other):
                    # its planned stop must not stop this task later
                    self._stopped_by_hand.add(other.get("id"))
                await self.stop_step_now(other, reason="pre-empted by a location task")
            else:
                self.audit.add("run", f"location task {sid} (priority {step_priority(step)}) "
                                      f"waits: radio {rid} busy with {other.get('id')} "
                                      f"(priority {step_priority(other)})")
                self._waiting[rid] = (step, dur, want)
                self._changed()
                return "waiting"
        self.audit.add("trigger", f"step {sid} on {rid}: started from its location")
        host = self.get_host(rid)
        if host is not None and self.active_on(rid) is None:
            await self._stop_running_tx(host, rid, "for this location task")
        await self._start_step(step)
        started = sid in self.active_steps or not step.get("radiates")
        if not started:
            self._changed()
            return "failed"
        if dur is None:
            dur = (task_length_s(step) if is_location_task(step)
                   else max(0.0, step_stop_s(step) - step_start_s(step)))
        old = self._now_tasks.pop(sid, None)
        if old is not None and not old.done():
            old.cancel()
        if step.get("radiates"):
            self._now_tasks[sid] = asyncio.create_task(self._auto_stop(step, dur))
        self._changed()
        return "started"

    def waiting_on(self, rid):
        """The location task waiting for a radio, or None."""
        w = self._waiting.get(rid)
        return w[0] if w else None

    def cancel_waiting(self, sid):
        for rid, w in list(self._waiting.items()):
            if w[0].get("id") == sid:
                self._waiting.pop(rid, None)
                return True
        return False

    async def _auto_stop(self, step, dur):
        try:
            await asyncio.sleep(dur * (self.speed if self.speed > 0 else 0))
        except asyncio.CancelledError:
            return
        sid = step.get("id")
        # this task is the step's auto-stop: take it out of _now_tasks first,
        # or _stop_step cancels it (cancelling itself) halfway through the
        # stop, the waiting location task is never started and, with unlucky
        # timing, stop_tx itself never goes out
        if self._now_tasks.get(sid) is asyncio.current_task():
            self._now_tasks.pop(sid, None)
        if sid in self.active_steps:
            self.audit.add("run", f"step {sid}: planned length "
                                  f"({dur:g} s) reached, stopping")
            await self._stop_step(step)
            self._changed()

    async def stop_step_now(self, step, reason="from its stop location (confirmed)") -> bool:
        """Stop one running step now (a confirmed stop area, leaving the area,
        or the operator). Returns True when it was running."""
        sid = step.get("id")
        t = self._now_tasks.pop(sid, None)
        if t is not None and not t.done():
            t.cancel()
        if sid not in self.active_steps:
            self.cancel_waiting(sid)
            self.audit.add("run", f"step {sid}: not running; stop ignored")
            return False
        self.audit.add("trigger", f"step {sid} on {step.get('radio')}: stopped {reason}")
        await self._stop_step(step)
        self._changed()
        return True

    async def stop_step_by_hand(self, step) -> bool:
        """Stop this step: one keyed step off, the rest of the run carries on.
        Its planned stop then sends nothing, as another step may hold the radio
        by that time. Returns True when it was running."""
        if step.get("id") in self.active_steps:
            self._stopped_by_hand.add(step.get("id"))
        return await self.stop_step_now(step, reason="by hand (Stop this step)")

    def _changed(self):
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:
                pass

    async def stop_all(self, hosts):
        """STOP ALL TX: cancel the run, send stop_tx to every radio together and
        close the RX attenuator a Decode step opened. hosts is the iterable of
        HostLinks to stop (the tab passes every connected radio, not only the
        ones this plan used). Returns how many radios answered."""
        self.audit.add("stop", "STOP ALL TX")
        self._stops += 1
        if self._task is not None and not self._task.done():
            self._task.cancel()
        for t in list(self._now_tasks.values()) + list(self._build_tasks.values()):
            if not t.done():
                t.cancel()
        self._now_tasks = {}
        self._build_tasks = {}
        self._built = {}
        self._waiting = {}
        self._active = {}
        self.running = False
        self.active_steps = set()
        self._decoding = {}
        held, self._rx_held = self._rx_held, {}
        hosts = list(hosts)
        missed = []
        sent = await send_stop_tx(hosts, missed)
        self.audit.add("stop", f"stop_tx sent to {sent} radio(s)")
        self.stop_unconfirmed = [_host_label(h) for h in missed]
        if missed:
            self.audit.add("stop", f"{len(missed)} radio(s) did not answer stop_tx: "
                                   f"{', '.join(self.stop_unconfirmed)}; retrying every "
                                   f"{self.STOP_RETRY_GAP_S:g} s for {self.STOP_RETRY_S:g} s")
        for h in missed:
            self._retry_stop_all(h)
        self.atten_not_reset = []
        for sid, rid in held.items():
            why = None
            try:
                ack = await self.get_any_host(rid).cmd("atten_set", {"db": 120.0}, timeout=5.0)
                res = getattr(ack, "result", None) or {}
                rb = res.get("readback_db")
                if getattr(ack, "status", 1) != 0:
                    why = f"refused: {res.get('err')}"
                elif rb is not None and abs(float(rb) - 120.0) > 0.5:
                    why = f"reads back {float(rb):g} dB"
                else:
                    self.audit.add("stop", f"step {sid}: RX attenuator on {rid} back to 120 dB"
                                           + (f" (reads back {float(rb):g} dB)" if rb is not None else ""))
            except Exception as e:
                why = str(e)
            if why:
                self.audit.add("stop", f"step {sid}: RX attenuator on {rid} NOT back to 120 dB: {why}")
                self.atten_not_reset.append(f"{rid} ({why})")
        if self.on_change:
            self.on_change()
        return sent
