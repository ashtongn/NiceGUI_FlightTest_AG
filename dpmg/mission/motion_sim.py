"""Simulated platform motion for checking location tasks against the timeline.

Flies a point along a path ([lon, lat] pairs, the plan's flight path or one
built through the task areas) at a set ground speed, with play, pause, stop
and loop. The planner feeds its position into the same position path the
triggers use (position_feed), labelled "simulated motion", so a
location task and a timeline run can be exercised together without a GPS or
the multicast feed. Nothing here touches a radio.
"""

from __future__ import annotations

import time

from . import triggers as tg
DEFAULT_SPEED_MPS = 60.0


class MotionPlayer:
    """Position along a path as a function of time. clock() is monotonic
    seconds; state is "stopped", "playing" or "paused"."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.path = []                  # [[lon, lat], ...]
        self.speed_mps = DEFAULT_SPEED_MPS
        self.loop = False
        self.state = "stopped"
        self._dist_m = 0.0              # distance along the path at the last tick
        self._t = None                  # clock value at the last tick
        self._forward = True

    # path

    def set_path(self, coords):
        self.path = [[float(p[0]), float(p[1])] for p in (coords or [])
                     if isinstance(p, (list, tuple)) and len(p) == 2]
        self.stop()

    def length_m(self) -> float:
        return sum(tg.haversine_m(a[1], a[0], b[1], b[0])
                   for a, b in zip(self.path, self.path[1:]))

    # transport

    def play(self):
        if len(self.path) < 2:
            return False
        if self.state == "stopped":
            self._dist_m = 0.0
            self._forward = True
        self._t = self.clock()
        self.state = "playing"
        return True

    def pause(self):
        if self.state == "playing":
            self._advance()
            self.state = "paused"

    def stop(self):
        self.state = "stopped"
        self._dist_m = 0.0
        self._t = None
        self._forward = True

    def _advance(self):
        now = self.clock()
        if self._t is None:
            self._t = now
            return
        self._dist_m += max(0.0, now - self._t) * float(self.speed_mps)
        self._t = now
        total = self.length_m()
        if total <= 0:
            return
        if self._dist_m >= total:
            if self.loop:
                self._dist_m = self._dist_m % total
                self._forward = not self._forward
            else:
                self._dist_m = total
                self.state = "paused"            # at the end; play resumes from the start

    # position

    def _point_at(self, d_m):
        pts = self.path if self._forward else list(reversed(self.path))
        if d_m <= 0:
            return list(pts[0])
        run = 0.0
        for a, b in zip(pts, pts[1:]):
            seg = tg.haversine_m(a[1], a[0], b[1], b[0])
            if run + seg >= d_m and seg > 0:
                f = (d_m - run) / seg
                return [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f]
            run += seg
        return list(pts[-1])

    def position(self):
        """[lon, lat] now, or None when stopped or with no path."""
        if self.state == "stopped" or len(self.path) < 2:
            return None
        if self.state == "playing":
            self._advance()
        return self._point_at(self._dist_m)

    def fix(self):
        """A triggers.Fix for the position feed, or None."""
        p = self.position()
        if p is None:
            return None
        return tg.Fix(p[1], p[0], None, t=self.clock(), source="simulated motion")

    def progress(self) -> dict:
        total = self.length_m()
        if self.state == "playing":
            self._advance()
        return {"state": self.state, "done_m": self._dist_m, "total_m": total,
                "fraction": (self._dist_m / total) if total > 0 else 0.0,
                "forward": self._forward}


def path_through_areas(mission: dict, lead_m: float = 3000.0) -> list:
    """A path that visits every task area in plan order: a lead-in point
    lead_m west of the first area, then each area's centre, then a point
    lead_m east of the last. [lon, lat] pairs; [] when the plan has no areas."""
    from . import mission_file as mf
    centres = []
    for s in mission.get("steps", []):
        gf = mf.step_location(s).get("start")
        c = mf.geofence_center(gf) if gf else None
        if c is not None:
            centres.append([c[1], c[0]])
    if not centres:
        return []
    import math
    lat0 = centres[0][1]
    dlon = lead_m / (111320.0 * max(0.05, math.cos(math.radians(lat0))))
    first = [centres[0][0] - dlon, centres[0][1]]
    last = [centres[-1][0] + dlon, centres[-1][1]]
    return [first] + centres + [last]
