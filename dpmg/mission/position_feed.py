"""Where the platform position comes from for the location triggers.

The GUI already shows two position sources: the aircraft position multicast
feed (acft_pos_multicast.MulticastPosListener, NMEA or JSON on a multicast
group) and the GPS fix in each radio's STATUS heartbeat. This picks the best
of them the same way the GUI header does: a fresh multicast fix wins, else
the first connected radio reporting a valid fix. Nothing else; the triggers
module decides whether the result is fresh enough to act on.
"""

from __future__ import annotations

import time

from dpmg.link import proto
from dpmg.link.proto import deg_from_1e7

from . import triggers as tg
class PositionFeed:
    """current(): triggers.Fix or None. mgr is the LinkManager (its hosts'
    STATUS carries the GPS fix); listener is a MulticastPosListener or None."""

    def __init__(self, mgr=None, listener=None, mcast_max_age_s: float = tg.STALE_S,
                 motion=None, name=None):
        self.mgr = mgr
        self.listener = listener
        self.name = name                  # the aircraft, when it is one of several
        self.mcast_max_age_s = float(mcast_max_age_s)
        self.motion = motion              # motion_sim.MotionPlayer or None

    def current(self):
        """Simulated motion while it plays, else a fresh multicast fix, else a
        radio GPS fix, else the last multicast fix however old (so the page can
        say "stale" rather than "none"), else None. The triggers module decides
        what is fresh enough to act on."""
        if self.motion is not None:
            try:
                sim = self.motion.fix()
            except Exception:
                sim = None
            if sim is not None:
                return sim
        fix = self._from_multicast()
        if fix is not None and tg.fix_age_s(fix) <= self.mcast_max_age_s:
            return fix
        radio = self._from_radios()
        if radio is not None:
            return radio
        return fix

    def _from_multicast(self):
        lst = self.listener
        if lst is None:
            return None
        try:
            pos = lst.latest                    # AircraftPos (property) or None
            if pos is None:
                return None
            return tg.Fix(float(pos.lat_deg), float(pos.lon_deg),
                          None if pos.alt_m is None else float(pos.alt_m),
                          t=float(pos.t_mono),
                          source="aircraft feed" + (f" {self.name}" if self.name else ""))
        except Exception:
            return None

    def _from_radios(self):
        if self.mgr is None:
            return None
        for serial, h in list(getattr(self.mgr, "hosts", {}).items()):
            st = getattr(h, "status", None)
            if st is None or not (st.flags & proto.Status.FLAG_POS_VALID):
                continue
            age = getattr(h, "status_age", float("inf"))
            if age == float("inf"):
                continue
            return tg.Fix(deg_from_1e7(st.lat_1e7), deg_from_1e7(st.lon_1e7),
                          float(st.alt_m) if st.alt_m else None,
                          t=time.monotonic() - float(age), source=f"{serial} GPS")
        return None

    def sources_text(self) -> str:
        """A short note on what is being watched, for the page."""
        parts = []
        if self.motion is not None and getattr(self.motion, "state", "stopped") != "stopped":
            parts.append(f"simulated motion ({self.motion.state})")
        if self.listener is not None:
            try:
                g = getattr(self.listener, "group", "?")
                p = getattr(self.listener, "port", "?")
                r = getattr(self.listener, "running", True)
                r = r() if callable(r) else r          # a property on the real listener
                parts.append(f"aircraft feed {g}:{p}" + ("" if r else " (not started)"))
            except Exception:
                parts.append("aircraft feed")
        if self.mgr is not None:
            n = len(getattr(self.mgr, "hosts", {}))
            parts.append(f"radio GPS ({n} radio{'s' if n != 1 else ''})")
        return ", ".join(parts) or "no position source"
