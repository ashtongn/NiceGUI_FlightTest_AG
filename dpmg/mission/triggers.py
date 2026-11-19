"""Location triggers for the mission planner.

A step may carry a start geofence and a stop geofence (mission_file: a point
with a radius in metres, or a polygon of [lon, lat] vertices). This module
watches the platform position and says when the platform enters one of them.
That is all it does, and nothing here talks to a radio. The tab turns an entry
into a prompt that a person confirms (the same radiate confirm as TX ALL),
except that an automatic location task, or one confirmed once since arming,
keys with no prompt.

Rules:
- an entry is a transition from outside (or unknown) to inside; one prompt
  per entry, and the fence re-arms when the platform leaves it
- a start fence only matters for a step that has not run yet; a stop fence
  only for a step that is running
- no position, or a position older than STALE_S, disables the triggers and
  the reason is reported so the page can say so; inside/outside becomes
  unknown, so a fix that comes back inside a fence counts as an entry, and a
  fix that comes back outside the start area ends a step that was running inside it
- the stop control always works regardless (that is the tab's STOP ALL TX)
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from . import mission_file as mf
STALE_S = 10.0                 # a fix older than this disables the triggers
EARTH_R_M = 6371008.8


@dataclass
class Fix:
    """One platform position. t is time.monotonic() at arrival."""
    lat: float
    lon: float
    alt: float | None = None
    t: float = 0.0
    source: str = ""


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R_M * math.asin(math.sqrt(a))


def point_in_polygon(lat, lon, polygon) -> bool:
    """Ray casting over [lon, lat] vertices. A point on an edge counts as
    inside often enough for a geofence; the ring need not be closed."""
    pts = [(float(p[0]), float(p[1])) for p in polygon]
    if len(pts) < 3:
        return False
    inside = False
    x, y = float(lon), float(lat)
    j = len(pts) - 1
    for i in range(len(pts)):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if (yi > y) != (yj > y):
            xcross = (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi
            if x < xcross:
                inside = not inside
        j = i
    return inside


def inside(gf, lat, lon) -> bool:
    """Is (lat, lon) inside a geofence as mission_file describes one?"""
    k = mf.geofence_kind(gf)
    if k == "point":
        try:
            return haversine_m(lat, lon, float(gf["lat"]), float(gf["lon"])) \
                <= float(gf.get("radius_m") or 0)
        except (TypeError, ValueError):
            return False
    if k == "polygon":
        try:
            return point_in_polygon(lat, lon, gf["polygon"])
        except (TypeError, ValueError, IndexError):
            return False
    return False


def fix_age_s(fix, now=None) -> float:
    if fix is None:
        return float("inf")
    return (time.monotonic() if now is None else now) - float(fix.t)


class TriggerEngine:
    """Tracks every step's start and stop fences against the platform position
    and reports entries. One per plan session; the tab calls update() about
    once a second with the current fix (or None)."""

    def __init__(self, stale_s: float = STALE_S, clock=time.monotonic):
        self.stale_s = float(stale_s)
        self.clock = clock
        self.enabled = False
        self.reason = "no position"
        self.last_fix = None
        self._st = {}                     # step id: state dict
        self._held = set()                # step ids stopped by hand, see hold()

    # per-step bookkeeping

    def _s(self, sid):
        return self._st.setdefault(sid, {"in_start": None, "in_stop": None,
                                         "started": False, "stopped": False})

    def mark_started(self, sid):
        s = self._s(sid)
        s["started"] = True
        s["stopped"] = False

    def mark_stopped(self, sid, rearm=False):
        """rearm=True (a location task) puts the step back to not started, so
        it prompts again on its next entry (after the platform has left)."""
        s = self._s(sid)
        if rearm:
            s["started"] = False
            s["stopped"] = False
        else:
            s["stopped"] = True

    def forget(self, sid):
        self._st.pop(sid, None)
        self._held.discard(sid)

    def reset(self):
        """Forget every step (a new run, or a plan reloaded). Holds stay, so
        turning the switch off and on does not key a step someone stopped."""
        self._st = {}

    # A stop by hand holds a step while the platform is inside its start area:
    # no start event until a good position puts the platform outside it. Losing
    # the position or re-arming does not clear a hold.

    def hold(self, step, fix):
        """Hold one step just stopped by hand. With no good position, hold it
        only if the platform was last known inside or the step was running."""
        sid = step.get("id")
        gf = mf.step_location(step).get("start")
        if not sid or not gf:
            return
        if self.status(fix)[0]:
            if inside(gf, fix.lat, fix.lon):
                self._held.add(sid)
            return
        s = self._st.get(sid) or {}
        if s.get("in_start") is True or s.get("inside_at_loss") or s.get("started"):
            self._held.add(sid)

    def hold_all(self, mission: dict, fix):
        """STOP ALL TX: hold every step the platform is inside, keyed or not.
        Call this before reset(), which forgets where the platform was."""
        for step in mission.get("steps", []):
            self.hold(step, fix)

    def is_held(self, sid) -> bool:
        return sid in self._held

    def held(self) -> list:
        return sorted(self._held)

    def release_left(self, mission: dict, fix) -> list:
        """Clear the hold of each step the platform is now outside of, or that
        is no longer in the plan. Returns those step ids."""
        if not self._held:
            return []
        steps = {s.get("id"): s for s in mission.get("steps", [])}
        good = self.status(fix)[0]
        out = []
        for sid in sorted(self._held):
            gf = mf.step_location(steps[sid]).get("start") if sid in steps else None
            if not gf or (good and not inside(gf, fix.lat, fix.lon)):
                self._held.discard(sid)
                out.append(sid)
        return out

    def step_state(self, sid) -> dict:
        return dict(self._s(sid))

    # position validity

    def status(self, fix) -> tuple:
        """(enabled, reason). Enabled only with a fresh fix."""
        if fix is None:
            return False, "no position: location triggers off"
        age = fix_age_s(fix, self.clock())
        if age > self.stale_s:
            return False, (f"position stale ({age:.0f} s old): location "
                           f"triggers off")
        src = f" ({fix.source})" if fix.source else ""
        return True, f"position {fix.lat:+.5f} {fix.lon:+.5f}{src}, {age:.0f} s old"

    # the check

    def update(self, mission: dict, fix) -> list:
        """Compare the fix with every located step. Returns the entries found
        this time as a list of {"kind": "start"|"stop"|"leave", "step": step,
        "geofence": gf}: "start" = entered the start area, "stop" = entered
        the stop area while running, "leave" = a running step's platform left
        its start area (a while-inside task ends on that). With no usable fix
        it returns [] and marks every fence unknown, so the next good fix
        inside a fence counts as entry."""
        self.last_fix = fix
        self.enabled, self.reason = self.status(fix)
        events = []
        if not self.enabled:
            for s in self._st.values():
                # remember a running step was inside, so the next good fix can end it
                if s["in_start"] is True:
                    s["inside_at_loss"] = True
                s["in_start"] = None
                s["in_stop"] = None
            return events
        for step in mission.get("steps", []):
            sid = step.get("id")
            loc = mf.step_location(step)
            if not sid or not loc:
                continue
            s = self._s(sid)
            gf = loc.get("start")
            if gf:
                now_in = inside(gf, fix.lat, fix.lon)
                if now_in and s["in_start"] is not True \
                        and not s["started"] and not s["stopped"] and sid not in self._held:
                    events.append({"kind": "start", "step": step, "geofence": gf})
                elif not now_in and (s["in_start"] is True or s.get("inside_at_loss")) \
                        and s["started"] and not s["stopped"]:
                    events.append({"kind": "leave", "step": step, "geofence": gf})
                s["in_start"] = now_in
                s["inside_at_loss"] = False
            gf = loc.get("stop")
            if gf:
                now_in = inside(gf, fix.lat, fix.lon)
                if now_in and s["in_stop"] is not True \
                        and s["started"] and not s["stopped"]:
                    events.append({"kind": "stop", "step": step, "geofence": gf})
                s["in_stop"] = now_in
        return events


def located_steps(mission: dict) -> list:
    """Steps that carry at least one geofence."""
    return [s for s in mission.get("steps", [])
            if mf.step_location(s).get("start") or mf.step_location(s).get("stop")]
