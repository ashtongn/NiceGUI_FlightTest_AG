"""Mission file: the dpmg-mission JSON format, load, save and validate.

The mission file is a plain JSON timeline of TX techniques. It is data, not
an execution authority. Loading or validating a file keys nothing; running it
still goes through the single run confirm and the per-radio radiate confirm.

Format (docs/MISSION_PLANNER.md):

  {
    "format": "dpmg-mission/1",
    "name": "range day A",
    "notes": "...",
    "authorization": {"reference": "TEST-2026-014", "note": "chamber load"},
    "radios": [{"id": "G40-0001", "type": "g40", "techniques": ["tx", "rx"]}],
    "steps": [
      {
        "id": "s1",
        "radio": "G40-0001",
        "technique": "jammer.barrage",
        "params": {...},
        "radiates": true,
        "schedule": {"start_s": 0, "stop_s": 30}
      }
    ]
  }

schedule carries start_s and either stop_s or duration_s. A radio may list the
capability tags it can run (see catalog.py); an empty or missing list means it
can run anything.

validate() returns a list of Conflict objects (empty means the plan is clean).
It never raises on a bad plan; it reports. load() does raise on a file that is
not this format or not parseable, because there is nothing to show.
"""

from __future__ import annotations

import json

import datetime

from . import catalog
from . import ports
from . import scenario_spec
from ..radio import g40_clock as _g40_clock

# A radio may carry a role (dpmg/radio/radio_role.py); steps the role
# does not run are flagged. Optional so the planner still loads on its own.
try:
    from dpmg.radio import radio_role as _role
except ImportError:
    _role = None
FORMAT = "dpmg-mission/1"

DAY_S = 86400.0             # one day, the span of the clock timeline

# Plan limits. A total past these is flagged, not silently accepted.
MAX_MISSION_S = 86400.0     # whole timeline, seconds from mission start (24 h)
MAX_STEP_S = 3600.0         # any single step (1 h)
MAX_STEPS = 200             # steps in one plan


class MissionError(Exception):
    """A file that cannot be loaded at all (bad JSON, wrong format)."""


class Conflict:
    """One validation problem. level is 'error' or 'warning'. step_id names the
    step it belongs to, or None for a plan-wide problem."""

    __slots__ = ("level", "message", "step_id")

    def __init__(self, message, level="error", step_id=None):
        self.message = message
        self.level = level
        self.step_id = step_id

    def __repr__(self):
        where = f" [{self.step_id}]" if self.step_id else ""
        return f"<{self.level}{where}: {self.message}>"

    def as_dict(self):
        return {"level": self.level, "message": self.message,
                "step_id": self.step_id}


def new_mission(name="untitled mission") -> dict:
    """A blank, valid mission with no radios and no steps.

    anchor says how step times map to real time:
      mode "on_tx"  schedule times are seconds from when Run mission is confirmed
                    (the portable default, nothing is tied to a day).
      mode "clock"  schedule times are seconds past 00:00 UTC of anchor.date,
                    so the timeline reads as a 24 h UTC clock and the run fires
                    each step at that wall-clock time.
    """
    return {"format": FORMAT, "name": name, "notes": "",
            "authorization": {"reference": "", "note": ""},
            "anchor": {"mode": "on_tx", "date": "", "tz": "UTC"},
            "radios": [], "steps": [], "map": {}}


# anchor and clock-time helpers

def anchor_mode(mission: dict) -> str:
    return (mission.get("anchor") or {}).get("mode", "on_tx")


def is_clock_anchored(mission: dict) -> bool:
    return anchor_mode(mission) == "clock"


def anchor_midnight_epoch(mission: dict):
    """Unix epoch seconds of 00:00 UTC on the anchor date, or None if the
    mission is not clock-anchored or the date does not parse."""
    a = mission.get("anchor") or {}
    if a.get("mode") != "clock":
        return None
    d = a.get("date")
    if not d:
        return None
    try:
        dt = datetime.datetime.strptime(str(d), "%Y-%m-%d").replace(
            tzinfo=datetime.timezone.utc)
    except ValueError:
        return None
    return dt.timestamp()


def clock_hms(start_s: float) -> str:
    """A seconds-past-midnight value as HH:MM:SS (wraps at a day)."""
    s = int(round(float(start_s or 0))) % int(DAY_S)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def clock_hm(start_s: float) -> str:
    """A seconds-past-midnight value as HH:MM (for axis ticks)."""
    return clock_hms(start_s)[:5]


def parse_clock(text) -> float:
    """Seconds past midnight from a clock string. Accepts HHMM, HH:MM and
    HH:MM:SS (also H:MM). Returns None if it does not parse or is out of range.
    """
    if text is None:
        return None
    t = str(text).strip()
    if not t:
        return None
    if ":" in t:
        parts = t.split(":")
    elif t.isdigit() and len(t) in (3, 4):
        parts = [t[:-2], t[-2:]]            # 1400: 14, 00
    else:
        return None
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    while len(nums) < 3:
        nums.append(0)
    h, m, s = nums[0], nums[1], nums[2]
    if not (0 <= h <= 23 and 0 <= m <= 59 and 0 <= s <= 59):
        return None
    return float(h * 3600 + m * 60 + s)


def step_stop_s(step: dict) -> float:
    """The stop time of a step, from stop_s or start_s + duration_s. Returns
    the start time if neither is usable (a zero-length step), so callers never
    divide by a missing field."""
    sch = step.get("schedule") or {}
    start = float(sch.get("start_s", 0.0) or 0.0)
    if sch.get("stop_s") is not None:
        return float(sch["stop_s"])
    if sch.get("duration_s") is not None:
        return start + float(sch["duration_s"])
    return start


def step_start_s(step: dict) -> float:
    sch = step.get("schedule") or {}
    return float(sch.get("start_s", 0.0) or 0.0)


def resolved_authorization(mission: dict, step: dict) -> str:
    """The authorization reference that covers a step: the step's own, else the
    mission's. Empty string means no authorization on file."""
    sa = (step.get("authorization") or {}).get("reference", "")
    if str(sa).strip():
        return str(sa).strip()
    ma = (mission.get("authorization") or {}).get("reference", "")
    return str(ma).strip()


# map and location triggers
#
# A step may carry a location: a start geofence and a stop geofence. A geofence
# is a point with a radius, or a drawn polygon. When the platform enters one the
# planner asks for a confirm, or keys with no question for an automatic location
# task or one confirmed once since arming (see the confirm modes below).
# The plan's map block names the base map and holds the flight path for review.

def map_block(mission: dict) -> dict:
    return mission.get("map") or {}


def step_location(step: dict) -> dict:
    return step.get("location") or {}


def geofence_kind(gf) -> str:
    """'point', 'polygon', or '' if it is neither (or empty)."""
    if not isinstance(gf, dict) or not gf:
        return ""
    if isinstance(gf.get("polygon"), list):
        return "polygon"
    if gf.get("lat") is not None and gf.get("lon") is not None:
        return "point"
    return ""


def _latlon_ok(lat, lon) -> bool:
    try:
        return -90.0 <= float(lat) <= 90.0 and -180.0 <= float(lon) <= 180.0
    except (TypeError, ValueError):
        return False


def geofence_center(gf):
    """A representative (lat, lon) for a geofence, or None. For a polygon it is
    the mean of its vertices, enough to centre the map on it."""
    k = geofence_kind(gf)
    if k == "point":
        return (float(gf["lat"]), float(gf["lon"])) if _latlon_ok(
            gf.get("lat"), gf.get("lon")) else None
    if k == "polygon":
        pts = [p for p in gf["polygon"]
               if isinstance(p, (list, tuple)) and len(p) == 2
               and _latlon_ok(p[1], p[0])]
        if not pts:
            return None
        return (sum(p[1] for p in pts) / len(pts),
                sum(p[0] for p in pts) / len(pts))
    return None


def validate_geofence(gf):
    """(ok, message). A point needs lat, lon in range and radius_m > 0. A
    polygon needs at least three [lon, lat] vertices, all in range. Polygon
    points are [lon, lat] to match GeoJSON."""
    k = geofence_kind(gf)
    if k == "":
        return False, "not a point (lat, lon, radius_m) or a polygon"
    if k == "point":
        if not _latlon_ok(gf.get("lat"), gf.get("lon")):
            return False, "lat/lon out of range"
        try:
            r = float(gf.get("radius_m"))
        except (TypeError, ValueError):
            return False, "radius_m is not a number"
        if r <= 0:
            return False, "radius_m must be greater than zero"
        return True, ""
    poly = gf.get("polygon")
    if not isinstance(poly, list) or len(poly) < 3:
        return False, "polygon needs at least three vertices"
    for p in poly:
        if not (isinstance(p, (list, tuple)) and len(p) == 2
                and _latlon_ok(p[1], p[0])):
            return False, "polygon vertex is not an in-range [lon, lat] pair"
    return True, ""


# location tasks and priority
#
# A location task is a step with a "trigger" block instead of a timeline time:
# it keys when the platform enters its area (location.start), after the
# operator's confirm. mode "while_inside" runs until the platform leaves (or
# max_s); mode "dwell" runs dwell_s once the point is reached. Every step has a
# priority, 1 (highest) to 9; when a location task and a timeline step want
# the same radio the higher priority wins and the other stops or waits.

TRIGGER_MODES = ("while_inside", "dwell")
TRIGGER_MODE_LABELS = {"while_inside": "while inside the area",
                       "dwell": "at the point, for a set time"}
# confirm rules for a location task: "once" = confirm the first entry, then
# automatic while armed; "always" = confirm every entry; "auto" = key on entry
# with no prompt at all (the arming switch is the operator's go-ahead)
CONFIRM_MODES = ("once", "always", "auto")
CONFIRM_LABELS = {"once": "confirm the first entry, then automatic",
                  "always": "confirm every entry",
                  "auto": "automatic: key on entry, no confirm (while armed)"}
PRIORITY_MIN, PRIORITY_MAX = 1, 9
DEFAULT_PRIORITY = 5                  # timeline steps
DEFAULT_LOCATION_PRIORITY = 3         # location tasks: above the timeline by default
DEFAULT_DWELL_S = 60.0
DEFAULT_MAX_S = 600.0


def is_location_task(step: dict) -> bool:
    return isinstance(step.get("trigger"), dict) and bool(step.get("trigger"))


def trigger_block(step: dict) -> dict:
    return step.get("trigger") or {}


def step_priority(step: dict) -> int:
    """1 (highest) to 9; a missing value is the default for the step kind."""
    d = DEFAULT_LOCATION_PRIORITY if is_location_task(step) else DEFAULT_PRIORITY
    try:
        return max(PRIORITY_MIN, min(PRIORITY_MAX, int(step.get("priority", d))))
    except (TypeError, ValueError):
        return d


def task_length_s(step: dict) -> float:
    """How long a location task keys once it starts: dwell_s for a dwell task,
    the safety cap max_s for a while-inside task."""
    t = trigger_block(step)
    key, default = (("dwell_s", DEFAULT_DWELL_S) if t.get("mode") == "dwell"
                    else ("max_s", DEFAULT_MAX_S))
    v = t.get(key)
    return float(default if v is None else v)


def new_location_task(step_id, technique, radio, geofence, *, mode="while_inside",
                      dwell_s=DEFAULT_DWELL_S, max_s=DEFAULT_MAX_S,
                      priority=DEFAULT_LOCATION_PRIORITY, confirm="once") -> dict:
    """A location task step for the plan. geofence is the start area (a point
    with radius_m or a polygon). Params come from the catalog entry."""
    entry = catalog.get(technique)
    step = {"id": step_id, "radio": radio, "technique": technique,
            "radiates": bool(entry.radiates) if entry else True,
            "params": entry.fresh_params() if entry else {},
            "location": {"start": dict(geofence)},
            "trigger": {"mode": mode if mode in TRIGGER_MODES else "while_inside",
                        "dwell_s": float(dwell_s), "max_s": float(max_s),
                        "confirm": confirm if confirm in CONFIRM_MODES else "once"},
            "priority": max(PRIORITY_MIN, min(PRIORITY_MAX, int(priority)))}
    return step


def load(path: str) -> dict:
    """Read a mission file. Raises MissionError on bad JSON or a format this
    version does not know, with a message meant for the operator."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise MissionError(f"no such file: {path}")
    except json.JSONDecodeError as e:
        raise MissionError(f"not valid JSON: {e}")
    return loads(data)


def loads(data) -> dict:
    """Validate the top-level shape and format of an already-parsed mission.
    Raises MissionError if it is not a dpmg-mission file this version opens."""
    if not isinstance(data, dict):
        raise MissionError("mission file is not a JSON object")
    fmt = data.get("format")
    if fmt != FORMAT:
        if isinstance(fmt, str) and fmt.startswith("dpmg-mission/"):
            raise MissionError(
                f"this is a {fmt} file but this tool reads {FORMAT}. "
                f"Open it in a matching version.")
        raise MissionError(
            f"not a dpmg-mission file (format is {fmt!r}, expected {FORMAT!r})")
    # Fill the optional containers so callers never guess at missing keys.
    data.setdefault("name", "untitled mission")
    data.setdefault("notes", "")
    data.setdefault("authorization", {"reference": "", "note": ""})
    data.setdefault("anchor", {"mode": "on_tx", "date": "", "tz": "UTC"})
    data.setdefault("radios", [])
    data.setdefault("steps", [])
    data.setdefault("map", {})
    if not isinstance(data["map"], dict):
        raise MissionError("map must be an object")
    if not isinstance(data["radios"], list):
        raise MissionError("radios must be a list")
    if not isinstance(data["steps"], list):
        raise MissionError("steps must be a list")
    for st in data["steps"]:
        if isinstance(st, dict):
            migrate_step(st)
    return data


def migrate_step(step: dict) -> dict:
    """Bring a step from an older plan up to the current catalog, in place.
    An IQ playback whose source was "scenario" (a scenario JSON on the radio)
    is the Comm waveform entry now: technique scenario_json with the file as
    spec_path. Its RF settings and times are kept. Returns the step."""
    p = step.get("params")
    if step.get("technique") == "iq_playback" and isinstance(p, dict) \
            and p.get("source") == "scenario":
        path = str(p.get("path") or p.get("name") or "")
        q = {k: v for k, v in p.items() if k not in ("source", "path", "name")}
        q.update({"source": "radio", "spec": None, "spec_name": "",
                  "spec_path": path, "match_radio_rate": True})
        step["technique"] = "scenario_json"
        step["params"] = q
        step["radiates"] = True
    return step


def save(mission: dict, path: str) -> None:
    """Write a mission to disk as readable JSON. Stamps the format so a saved
    file always says what it is."""
    out = dict(mission)
    out["format"] = FORMAT
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
        f.write("\n")


def dumps(mission: dict) -> str:
    out = dict(mission)
    out["format"] = FORMAT
    return json.dumps(out, indent=2) + "\n"


def rate_change_block_s(radio_type) -> float:
    """Seconds a rate change keeps a radio from doing anything else, for Check plan and
    the block the timeline draws."""
    return float(ports.REINIT_S.get(str(radio_type or "g40").lower(), 30))


def _spec_key(step: dict):
    """What decides whether two comm steps carry the same scenario."""
    p = step.get("params") or {}
    return p.get("spec") if p.get("spec") else p.get("spec_path")


def _decode_conflicts(steps) -> list:
    """A decode step with nothing to decode: no comm waveform on air during it, or one
    built from another scenario. Warnings, since a signal may come from elsewhere."""
    out = []
    comm = [s for s in steps if s.get("technique") == "scenario_json"]
    for s in steps:
        if s.get("technique") != "scenario_decode":
            continue
        sid = s.get("id") or "(no id)"
        if is_location_task(s):
            # no clock time to compare, so look for the same scenario among the comm steps
            if not any(_spec_key(c) == _spec_key(s) for c in comm):
                out.append(Conflict("Decode location task has no Comm waveform step with "
                                    "the same scenario JSON", level="warning", step_id=sid))
            continue
        live = [c for c in comm if _overlaps(step_start_s(s), step_stop_s(s), step_start_s(c), step_stop_s(c))]
        if not live:
            out.append(Conflict("Decode step has no Comm waveform step on air during it",
                                level="warning", step_id=sid))
        elif not any(_spec_key(c) == _spec_key(s) for c in live):
            out.append(Conflict("Decode step's scenario JSON differs from the Comm waveform "
                                "on air during it, so it will not decode that signal",
                                level="warning", step_id=sid))
    return out


def _overlaps(a_start, a_stop, b_start, b_stop) -> bool:
    """Two half-open intervals [start, stop) overlap. Steps that only touch at
    an endpoint (one stops exactly when the next starts) do not."""
    return a_start < b_stop and b_start < a_stop


def _port_conflicts(steps, radios, port_maps=None, rx_now=None) -> list:
    """Per-step RX / TX port choices: a name the radio does not have, a pair
    the radio cannot stream together (errors), and a change of ports between
    consecutive timeline steps on one radio, which re-initializes it when the
    later step starts (a warning, so the operator leaves a gap). RX none on a
    step that receives is an error; where RX none makes the runner move RX
    (see ports.rx_for_none) the step gets a warning that says to which port.
    port_maps is {radio id: port map} for the radios whose map is known (from
    get_config); a jack the map locks is an error, and RX on a 1 jack with TX on
    the 2 jack of one G40 card gets a warning (coupling only on the bench).
    rx_now is {radio id: RX port} for the connected radios: an RX none step
    that leaves that port alone gets no warning."""
    out = []
    timeline: dict = {}
    port_maps = port_maps or {}
    for step in steps:
        sid = step.get("id") or "(no id)"
        rx, tx = ports.step_ports(step)
        if not rx and not tx:
            continue
        rtype = (radios.get(step.get("radio")) or {}).get("type")
        if rx == ports.RX_NONE and catalog.receives(step.get("technique")):
            out.append(Conflict(
                f"RX is none but {step.get('technique')} receives; pick an RX port "
                f"or leave it on the radio's current", step_id=sid))
        for msg in ports.invalid(rtype, rx, tx, port_maps.get(step.get("radio"))):
            out.append(Conflict(msg, step_id=sid))
        why = ports.conflict(rtype, rx, tx)
        if why:
            out.append(Conflict(why, step_id=sid))
        if not is_location_task(step):
            timeline.setdefault(step.get("radio"), []).append(step)
    for rid, lst in timeline.items():
        rtype = (radios.get(rid) or {}).get("type")
        pm = port_maps.get(rid)
        secs = ports.REINIT_S.get(str(rtype or "").lower(), 30)
        need = secs + 5
        prev = (None, None)
        prev_stop = 0.0
        told_none = False
        for step in sorted(lst, key=step_start_s):
            rx, tx = ports.step_ports(step)
            if rx == ports.RX_NONE:
                txe = tx or prev[1]
                if prev[0]:
                    rx = ports.rx_for_none(rtype, prev[0], txe, pm)
                    if rx != prev[0]:
                        out.append(Conflict(
                            f"RX none: RX on {prev[0]} cannot stay while radio {rid} "
                            f"transmits on {txe}; the runner moves RX to {rx} "
                            f"(receive only) before this step", level="warning",
                            step_id=step.get("id")))
                elif (rx_now or {}).get(rid):
                    # connected: what the runner will do is known
                    now = rx_now[rid]
                    rx = ports.rx_for_none(rtype, now, txe, pm)
                    if rx and rx != now and not told_none:
                        told_none = True
                        out.append(Conflict(
                            f"RX none: RX on {now} cannot stay while radio {rid} "
                            f"transmits on {txe}; the runner moves RX to {rx} "
                            f"(receive only, about {secs} s re-init) before this step",
                            level="warning", step_id=step.get("id")))
                    rx = None
                else:
                    rx = None
                    pick = ports.rx_for_none(rtype, None, txe, pm)
                    keep = ports.rx_fitting(rtype, txe, pm)
                    if pick and keep and not told_none:
                        told_none = True
                        out.append(Conflict(
                            f"RX none: unless radio {rid} is receiving on "
                            f"{' or '.join(keep)} when this step starts, the runner "
                            f"moves RX to {pick} (receive only, about {secs} s "
                            f"re-init) before it", level="warning",
                            step_id=step.get("id")))
            cur = (rx or prev[0], tx or prev[1])
            note = ports.same_card_note(rtype, cur[0], cur[1])
            if note:
                out.append(Conflict(note, level="warning", step_id=step.get("id")))
            # the runner moves the radio in the gap before the step; warn only
            # when that gap is too short and the step will start late
            gap = step_start_s(step) - prev_stop
            if prev != (None, None) and cur != prev and gap < need:
                out.append(Conflict(
                    f"port change to {ports.label(*cur)} re-initializes radio {rid} "
                    f"(about {secs} s, RX stops) but the gap before this step is "
                    f"{max(gap, 0):g} s; leave {need} s or the step starts late",
                    level="warning", step_id=step.get("id")))
            prev = cur
            prev_stop = max(prev_stop, step_stop_s(step))
    return out


# A radiating step's waveform is built on the radio before it keys: 1 to 4 s on
# a G40, longest at the high sample rates. The runner builds it in the gap after
# the step before it on that radio has stopped. With less gap than the build
# takes, the step keys late and runs short, and a step whose whole slot has
# passed is skipped.
BUILD_GAP_S = 2.0            # under 10 MS/s
BUILD_GAP_FAST_S = 4.0       # 10 MS/s and up


def build_gap_need_s(sample_rate) -> float:
    try:
        fast = float(sample_rate or 0) >= 10e6
    except (TypeError, ValueError):
        fast = False
    return BUILD_GAP_FAST_S if fast else BUILD_GAP_S


def _build_gap_conflicts(steps, radios) -> list:
    """A warning for each timeline step that builds a waveform with less gap
    after the keyed step before it than the build takes. A step that follows
    a receive step is left alone: a loopback keys straight after its Decode
    step on purpose and its key is long enough to carry a late start."""
    try:
        from .mission_runner import builds_ahead
    except Exception:
        return []
    out = []
    timeline = {}
    for step in steps:
        if not is_location_task(step) and step.get("schedule"):
            timeline.setdefault(step.get("radio"), []).append(step)
    for rid, lst in timeline.items():
        need = build_gap_need_s((radios.get(rid) or {}).get("sample_rate"))
        prev_stop, prev_keyed = None, False
        for step in sorted(lst, key=step_start_s):
            start = step_start_s(step)
            if prev_stop is not None and prev_keyed and builds_ahead(step):
                gap = start - prev_stop
                if 0 <= gap < need:
                    out.append(Conflict(
                        f"{gap:g} s after the step before it on radio {rid}: the radio builds this "
                        f"waveform in that gap and needs about {need:g} s, so the step keys late and "
                        f"runs short, or is skipped; leave {need:g} s or more",
                        level="warning", step_id=step.get("id")))
            if prev_stop is None or step_stop_s(step) >= prev_stop:
                prev_stop, prev_keyed = step_stop_s(step), bool(step.get("radiates"))
    return out


def validate(mission: dict, *, max_mission_s=MAX_MISSION_S,
             max_step_s=MAX_STEP_S, max_steps=MAX_STEPS, port_maps=None, rx_now=None) -> list:
    """Check a mission and return the conflicts, worst first is not promised;
    callers group by level. An empty list means the plan may run (after the
    operator confirm). Never raises for plan content.

    Flags: unknown format version (reported by load/loads before this is even
    reached), a radio booked twice at once, a step on a radio that cannot run
    its technique, a radiating step with no authorization, times out of order,
    and a plan or step past the limits."""
    out = []
    radios = {r.get("id"): r for r in mission.get("radios", []) if r.get("id")}
    steps = mission.get("steps", [])

    if len(steps) > max_steps:
        out.append(Conflict(
            f"plan has {len(steps)} steps, over the limit of {max_steps}"))

    # a clock-anchored plan needs a valid date, and its day is a fixed 24 h
    if is_clock_anchored(mission) and anchor_midnight_epoch(mission) is None:
        out.append(Conflict(
            "clock-anchored plan needs a valid anchor date (YYYY-MM-DD)"))

    # a planned radio's start sample rate must be one the radio can take
    for r in mission.get("radios", []):
        hz = r.get("sample_rate")
        if hz and not catalog.is_valid_rate(hz, r.get("type")):
            out.append(Conflict(
                f"radio {r.get('id')} start rate {float(hz) / 1e6:g} MSps is not "
                f"a valid rate for a {r.get('type') or 'g40'} radio", level="warning"))
        # the clock Run mission would bring it up on must be one this kind can run:
        # an X40 on gpsdo is refused by its service (no FPGA GPSDO on an X40)
        rtype = str(r.get("type") or "g40").lower()
        if rtype in ("g40", "x40") and r.get("clock_source"):
            ck = _g40_clock.resolve(r.get("clock_source"), rtype)
            if ck.get("err"):
                out.append(Conflict(f"radio {r.get('id')}: {ck['err']}"))

    seen_ids = set()
    # per radio, the (start, stop, step_id) intervals, to find double bookings
    by_radio: dict = {}

    out.extend(_port_conflicts(steps, radios, port_maps, rx_now))
    out.extend(_build_gap_conflicts(steps, radios))

    for step in steps:
        sid = step.get("id") or "(no id)"
        if sid in seen_ids:
            out.append(Conflict(f"duplicate step id {sid}", step_id=sid))
        seen_ids.add(sid)

        tech = step.get("technique")
        entry = catalog.get(tech) if tech else None
        if entry is None:
            out.append(Conflict(
                f"unknown technique {tech!r}; not in the catalog", step_id=sid))

        rid = step.get("radio")
        radio = radios.get(rid)
        if radio is None:
            out.append(Conflict(
                f"step is on radio {rid!r}, which the plan does not list",
                step_id=sid))
        elif entry is not None:
            can = radio.get("techniques") or radio.get("capabilities") or []
            if can:
                missing = [tag for tag in entry.needs if tag not in can]
                if missing:
                    out.append(Conflict(
                        f"radio {rid} cannot run {tech} "
                        f"(needs {', '.join(entry.needs)}, has {', '.join(can)})",
                        step_id=sid))
            role = radio.get("role")
            if role and _role is not None and not _role.technique_allowed(
                    role, tech, entry.group):
                out.append(Conflict(
                    f"radio {rid} is in the {_role.label(role)} role, which does "
                    f"not run {tech}", step_id=sid))

        start = step_start_s(step)
        stop = step_stop_s(step)
        location_task = is_location_task(step)
        if location_task:
            t = trigger_block(step)
            if t.get("mode") not in TRIGGER_MODES:
                out.append(Conflict(f"location task mode {t.get('mode')!r} is not "
                                    f"one of {', '.join(TRIGGER_MODES)}", step_id=sid))
            try:
                n = task_length_s(step)
            except (TypeError, ValueError):
                n = 0.0
            if n <= 0:
                out.append(Conflict("location task has no run length (dwell or max)",
                                    step_id=sid))
            elif n > max_step_s:
                out.append(Conflict(
                    f"location task would run {n:g}s, over the per-step limit of "
                    f"{max_step_s:g}s", step_id=sid))
            if not step_location(step).get("start"):
                out.append(Conflict("location task has no area (draw or pick one)",
                                    step_id=sid))
        else:
            if start < 0:
                out.append(Conflict(
                    f"start time {start:g}s is before mission start", step_id=sid))
            if stop <= start:
                out.append(Conflict(
                    f"stop time {stop:g}s is not after start {start:g}s",
                    step_id=sid))
            dur = stop - start
            if dur > max_step_s:
                out.append(Conflict(
                    f"step runs {dur:g}s, over the per-step limit of {max_step_s:g}s",
                    step_id=sid))
            if stop > max_mission_s:
                out.append(Conflict(
                    f"step ends at {stop:g}s, past the mission limit of "
                    f"{max_mission_s:g}s", step_id=sid))
        pr = step.get("priority")
        if pr is not None:
            try:
                ok_pr = PRIORITY_MIN <= int(pr) <= PRIORITY_MAX
            except (TypeError, ValueError):
                ok_pr = False
            if not ok_pr:
                out.append(Conflict(f"priority {pr!r} is not {PRIORITY_MIN} to "
                                    f"{PRIORITY_MAX}", level="warning", step_id=sid))

        if tech == "set_rate":
            hz = (step.get("params") or {}).get("sample_rate")
            rtype = (radio or {}).get("type")
            if not hz or float(hz) <= 0:
                out.append(Conflict("Set sample rate has no rate", step_id=sid))
            elif not catalog.is_valid_rate(hz, rtype):
                out.append(Conflict(
                    f"sample rate {float(hz) / 1e6:g} MSps is not a valid rate "
                    f"for a {rtype or 'g40'} radio", level="warning", step_id=sid))

        if tech == "iq_playback":
            p = step.get("params") or {}
            if not str(p.get("path") or p.get("name") or "").strip():
                out.append(Conflict(
                    "IQ playback has no file chosen (pick one in the step "
                    "editor)", step_id=sid))
        elif tech in ("scenario_json", "scenario_decode"):
            # needs input: a spec uploaded on the console (inline) or a file
            # on the radio. An inline spec that is not a scenario is flagged
            # too, so a hand-edited plan cannot send junk to gen_scenario.
            p = step.get("params") or {}
            if p.get("spec") is not None and not scenario_spec.is_scenario_spec(p.get("spec")):
                out.append(Conflict(
                    "Comm waveform's spec is not a scenario JSON (it needs a "
                    "'signals' list or DSSS 'users')", step_id=sid))
            elif not scenario_spec.step_spec_source(p):
                out.append(Conflict(
                    "Comm waveform has no scenario JSON (upload one from this "
                    "console or choose a file on the radio in the step editor)",
                    step_id=sid))
        elif tech == "replay":
            p = step.get("params") or {}
            if not str(p.get("name") or "").strip():
                out.append(Conflict(
                    "Replay has no recording chosen (pick one in the step "
                    "editor)", step_id=sid))

        # RX gain is the hardware index set_gain takes: 187-255 on a G40,
        # 195-255 on an X40
        gain = (step.get("params") or {}).get("rx_gain_index")
        if gain not in (None, ""):
            lo = (catalog.RX_GAIN_MIN_X40 if (radio or {}).get("type") == "x40"
                  else catalog.RX_GAIN_MIN_G40)
            try:
                g = int(gain)
            except (TypeError, ValueError):
                g = None
            if g is None or not lo <= g <= catalog.RX_GAIN_MAX:
                out.append(Conflict(
                    f"RX gain index {gain} is outside {lo}-{catalog.RX_GAIN_MAX} "
                    f"for this radio", step_id=sid))

        radiates = bool(step.get("radiates"))
        if radiates and not resolved_authorization(mission, step):
            out.append(Conflict(
                "radiating step has no authorization on file "
                "(set the mission or step authorization reference)",
                step_id=sid))

        # location triggers are optional; when present, the geofences must be
        # well formed.
        loc = step_location(step)
        for which in ("start", "stop"):
            gf = loc.get(which)
            if gf in (None, {}):
                continue
            ok, why = validate_geofence(gf)
            if not ok:
                out.append(Conflict(
                    f"{which} geofence is not valid: {why}", step_id=sid))

        if rid is not None and not location_task:
            by_radio.setdefault(rid, []).append((start, stop, sid))

    # double booking: any two steps on the same radio whose times overlap
    for rid, intervals in by_radio.items():
        intervals.sort()
        for i in range(len(intervals)):
            a_start, a_stop, a_id = intervals[i]
            for j in range(i + 1, len(intervals)):
                b_start, b_stop, b_id = intervals[j]
                if b_start >= a_stop:
                    break    # sorted by start; nothing later can overlap a
                if _overlaps(a_start, a_stop, b_start, b_stop):
                    out.append(Conflict(
                        f"radio {rid} is double booked: steps {a_id} and {b_id} "
                        f"overlap in time", step_id=b_id))

    # a location task shares its radio with timeline steps: it may want the
    # radio while one of them is keyed. Priority decides (lower number wins);
    # say so before the run rather than at the moment
    for task in steps:
        if not is_location_task(task):
            continue
        rid = task.get("radio")
        others = [s for s in steps if s.get("radio") == rid and not is_location_task(s)
                  and s.get("radiates")]
        if not others:
            continue
        tp = step_priority(task)
        op = sorted({step_priority(s) for s in others})
        if tp < min(op):
            how = "it stops the timeline step"
        elif tp > max(op):
            how = "it waits for the radio"
        else:
            how = "equal priority: the one already keyed keeps the radio"
        out.append(Conflict(
            f"location task (priority {tp}) shares radio {rid} with "
            f"{len(others)} timeline step(s) (priority "
            f"{', '.join(str(p) for p in op)}); if they meet, {how}",
            level="warning", step_id=task.get("id") or "(no id)"))

    out.extend(_decode_conflicts(steps))

    # a set_rate re-initializes the radio. A step that starts during that time would
    # lose its slot, and one still running would have its radio taken away. Both are
    # errors, so the plan does not run until the time is left clear.
    by_radio_steps: dict = {}
    for step in steps:
        rid = step.get("radio")
        if rid is not None:
            by_radio_steps.setdefault(rid, []).append(step)
    for rid, rsteps in by_radio_steps.items():
        radio = next((r for r in mission.get("radios", []) if r.get("id") == rid), None) or {}
        need = rate_change_block_s(radio.get("type"))
        rate = radio.get("sample_rate")
        timed = [s for s in rsteps if not is_location_task(s)]
        for r in sorted((s for s in timed if s.get("technique") == "set_rate"), key=step_start_s):
            hz, rs = (r.get("params") or {}).get("sample_rate"), step_start_s(r)
            if rate and hz and abs(float(hz) - float(rate)) <= 1.0:
                continue                         # already at that rate, so no re-init
            rate = hz or rate
            for s in timed:
                if s is r or s.get("technique") == "set_rate":
                    continue
                st, sp = step_start_s(s), step_stop_s(s)
                if rs <= st < rs + need:
                    out.append(Conflict(
                        f"step starts {st - rs:g} s after a sample-rate change on {rid}, which "
                        f"takes about {need:g} s to re-initialize. Start it at {rs + need:g} s or later",
                        step_id=s.get("id") or "(no id)"))
                elif st < rs < sp:
                    out.append(Conflict(
                        f"step is still running when the sample-rate change on {rid} starts at "
                        f"{rs:g} s. End it by then", step_id=s.get("id") or "(no id)"))
    return out


def summary(mission: dict) -> dict:
    """A short read-only summary of a plan: step count, radiating count, the
    radios used, the total span, and the authorization reference. For the tab
    header and review mode."""
    steps = mission.get("steps", [])
    radiating = [s for s in steps if s.get("radiates")]
    span = max((step_stop_s(s) for s in steps), default=0.0)
    radios = sorted({s.get("radio") for s in steps if s.get("radio")})
    return {
        "name": mission.get("name", "untitled mission"),
        "steps": len(steps),
        "radiating": len(radiating),
        "radios": radios,
        "span_s": span,
        "authorization": (mission.get("authorization") or {}).get("reference", ""),
    }
