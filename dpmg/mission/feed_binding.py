"""Which aircraft each plan radio follows, and the location triggers run per
aircraft.

A plan radio may carry "aircraft": the name of one of the saved position
feeds (feed_settings.py). A radio with no field, or one naming an aircraft
that is no longer on the list, follows the first aircraft. The location
triggers of a step run on the position of its radio's aircraft; with one
aircraft on the list that is the one feed. Plain functions, no page code, no sockets.
"""

from __future__ import annotations

from . import mission_file as mf

RADIO_KEY = "aircraft"


def aircraft_of(radio, names) -> str | None:
    """The aircraft a plan radio follows: its own field when that name is on
    the list, else the first aircraft; None with no aircraft at all."""
    names = list(names or [])
    if not names:
        return None
    want = (radio or {}).get(RADIO_KEY) if isinstance(radio, dict) else None
    return want if want in names else names[0]


def radio_aircraft(mission, rid, names) -> str | None:
    radio = next((r for r in (mission or {}).get("radios", []) if r.get("id") == rid), None)
    return aircraft_of(radio, names)


def set_radio_aircraft(radio: dict, name, names) -> None:
    """Bind a plan radio to an aircraft; the first one (or None) clears the
    field, so an old plan stays as it was written."""
    if name and name in list(names or []) and name != list(names)[0]:
        radio[RADIO_KEY] = name
    else:
        radio.pop(RADIO_KEY, None)


def steps_by_aircraft(mission, names) -> dict:
    """{aircraft name: [steps]} for every step of the plan, each step under
    the aircraft its radio follows. A step on a radio not in the plan follows
    the first aircraft."""
    names = list(names or [])
    out = {n: [] for n in names}
    if not names:
        return out
    for step in (mission or {}).get("steps", []):
        out[radio_aircraft(mission, step.get("radio"), names)].append(step)
    return out


def _unknown(engine, sids):
    """What TriggerEngine.update does with no usable fix, for these steps only:
    forget which side of each fence the platform was, remembering a running
    step was inside so the next good fix can end it."""
    for sid in sids:
        s = engine._st.get(sid)
        if s is None:
            continue
        if s["in_start"] is True:
            s["inside_at_loss"] = True
        s["in_start"] = None
        s["in_stop"] = None


def tick_triggers(engine, mission, names, fix_of) -> list:
    """engine.update once per aircraft, each on its own fix (fix_of(name)),
    over the steps whose radios follow it. Returns every entry found. With
    one aircraft this is engine.update(mission, fix) exactly. Afterwards
    engine.enabled is True when any aircraft has a usable fix and
    engine.reason names each aircraft's state."""
    names = list(names or [])
    if len(names) <= 1:
        return engine.update(mission, fix_of(names[0]) if names else None)
    groups = steps_by_aircraft(mission, names)
    events, reasons, enabled, first_fix = [], [], False, None
    for name in names:
        fix = fix_of(name)
        if first_fix is None:
            first_fix = fix
        ok, why = engine.status(fix)
        reasons.append(f"{name}: {why}")
        sub = dict(mission, steps=groups.get(name, []))
        if ok:
            enabled = True
            events.extend(engine.update(sub, fix))
        else:
            _unknown(engine, [s.get("id") for s in sub["steps"] if s.get("id")])
    engine.last_fix = first_fix
    engine.enabled, engine.reason = enabled, "; ".join(reasons)
    return events


def status(engine, names, fix_of) -> tuple:
    """(enabled, reason) over every aircraft: enabled when any has a usable
    fix, the reason naming each one's state. One aircraft: engine.status."""
    names = list(names or [])
    if len(names) <= 1:
        return engine.status(fix_of(names[0]) if names else None)
    enabled, reasons = False, []
    for name in names:
        ok, why = engine.status(fix_of(name))
        enabled = enabled or ok
        reasons.append(f"{name}: {why}")
    return enabled, "; ".join(reasons)


def release_left(engine, mission, names, fix_of) -> list:
    """engine.release_left per aircraft."""
    names = list(names or [])
    if len(names) <= 1:
        return engine.release_left(mission, fix_of(names[0]) if names else None)
    # engine.release_left also releases a held step that is not in the plan
    # it is given, so each aircraft's pass sees only the holds of its own steps
    groups = steps_by_aircraft(mission, names)
    held = set(engine._held)
    out = []
    for name in names:
        sub = dict(mission, steps=groups.get(name, []))
        ids = {s.get("id") for s in sub["steps"]}
        engine._held = {h for h in held if h in ids}
        rel = engine.release_left(sub, fix_of(name))
        out.extend(rel)
        held -= set(rel)
    plan_ids = {s.get("id") for s in (mission or {}).get("steps", [])}
    out.extend(sorted(h for h in held if h not in plan_ids))
    engine._held = {h for h in held if h in plan_ids}
    return out


def hold_all(engine, mission, names, fix_of) -> None:
    """engine.hold_all per aircraft (STOP ALL TX)."""
    names = list(names or [])
    if len(names) <= 1:
        engine.hold_all(mission, fix_of(names[0]) if names else None)
        return
    for name, steps in steps_by_aircraft(mission, names).items():
        engine.hold_all(dict(mission, steps=steps), fix_of(name))


def hold(engine, mission, step, names, fix_of) -> None:
    """engine.hold on the fix of this step's radio's aircraft."""
    names = list(names or [])
    name = radio_aircraft(mission, (step or {}).get("radio"), names)
    engine.hold(step, fix_of(name) if name else None)


def is_located(step) -> bool:
    return bool(mf.step_location(step))
