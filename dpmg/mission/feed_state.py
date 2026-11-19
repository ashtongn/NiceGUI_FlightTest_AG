"""What the flight view says about the live position feed.

A plain function over the plan session's position sources, so the row under
the map in the flight planner and the tests read the same thing: which source
the position comes from, the group, port and interface being listened on, the
time and age of the last fix, the fix rate, and a warning when there is no fix,
the fix is stale, or the position is a simulation rather than the aircraft.
"""

from __future__ import annotations

import time

from . import triggers as tg

STALE_S = tg.STALE_S          # the triggers' own limit, so the two agree


def _fmt_hms(t_wall) -> str:
    if not t_wall:
        return "--:--:--"
    return time.strftime("%H:%M:%S", time.gmtime(t_wall)) + "Z"


def feed_state(sess, now_wall: float = None, now_mono: float = None, name=None) -> dict:
    """The live feed state for the flight planner.

    sess has: listener (MulticastPosListener or None), feed (PositionFeed),
    motion (MotionPlayer), pos_group, pos_port, pos_iface. With name (one
    aircraft of several) the listener and feed are sess.listener_for(name) and
    sess.feed_for(name). Returns a dict:
      aircraft    the name given, or None
      listening   bool, the multicast listener is running
      where       "group:port via iface" (the listener's own values when it runs)
      format      the listener's parser setting, or "" without one
      source      where the position comes from: "aircraft feed", "<serial> GPS",
                  "simulated motion" or "none"
      last_fix    "HH:MM:SSZ" of the last fix's arrival, or "--:--:--"
      age_s       seconds since that fix, or None
      rate_hz     fixes per second from the listener, or None
      rx, parsed, bad   the listener's counters (0 without one)
      feed_offset_s     the feed's own time minus the console clock, or None
      heading_deg, speed_mps, fix_mode, sats_used
                  from the feed's last fix when the position is the feed's and
                  the fix carried them, else None
      sentences   the NMEA sentences (or "json") that fed that fix, or []
      warning     "" or the text to show in red or amber
      level       "ok", "warn" or "bad"
    """
    now_wall = time.time() if now_wall is None else now_wall
    now_mono = time.monotonic() if now_mono is None else now_mono
    lst, feedobj = _for(sess, name)
    running = False
    if lst is not None:
        r = getattr(lst, "running", False)
        running = bool(r() if callable(r) else r)
    where = (f"{getattr(lst, 'group', sess.pos_group)}:{getattr(lst, 'port', sess.pos_port)} "
             f"via {getattr(lst, 'iface_ip', sess.pos_iface)}") if lst is not None \
        else f"{sess.pos_group}:{sess.pos_port} via {sess.pos_iface}"
    fmt = str(getattr(lst, "fmt", "") or "") if lst is not None else ""
    rx = int(getattr(lst, "rx", 0) or 0) if lst is not None else 0
    parsed = int(getattr(lst, "parsed", 0) or 0) if lst is not None else 0
    bad = int(getattr(lst, "bad", 0) or 0) if lst is not None else 0

    fix = None
    try:
        fix = feedobj.current()
    except Exception:
        fix = None
    source = "none"
    age = None
    last_fix = "--:--:--"
    if fix is not None:
        source = str(getattr(fix, "source", "") or "position")
        age = max(0.0, now_mono - float(getattr(fix, "t", now_mono)))
        last_fix = _fmt_hms(now_wall - age)

    rate = None
    if lst is not None and hasattr(lst, "rate_hz"):
        try:
            rate = lst.rate_hz(now_mono=now_mono)
        except TypeError:
            rate = lst.rate_hz()
        except Exception:
            rate = None
    offset = None
    if lst is not None and hasattr(lst, "feed_offset_s"):
        try:
            o = lst.feed_offset_s()
            offset = None if o is None else float(o[0])
        except Exception:
            offset = None

    simulated = source == "simulated motion"
    motion = getattr(sess, "motion", None)
    if not simulated and motion is not None and getattr(motion, "state", "stopped") != "stopped":
        simulated = True
    if simulated:
        warning, level = ("SIMULATED position: the planner is following the simulated "
                          "motion, not the aircraft. Stop it in the engineering view "
                          "before a flight run."), "bad"
    elif fix is None:
        if running:
            warning, level = (f"no fix yet on {where}"
                              + (f" ({rx} datagram(s) received, none parsed)" if rx else
                                 " (nothing received)")), "bad"
        else:
            warning, level = "no position: the aircraft feed is not listening and no radio reports a GPS fix", "bad"
    elif age is not None and age > STALE_S:
        warning, level = f"stale fix: {age:.0f} s old, location triggers are off", "bad"
    elif not running and "GPS" in source:
        warning, level = f"position from {source}, the aircraft feed is not listening", "warn"
    elif offset is not None and abs(offset) > 2.0:
        warning, level = (f"feed time is {offset:+.1f} s from this console's clock; "
                          "check the console clock before a UTC plan"), "warn"
    else:
        warning, level = "", "ok"
    extra = fix_extras(sess, fix, name)
    return {"aircraft": name, "listening": running, "where": where, "format": fmt, "source": source,
            "last_fix": last_fix, "age_s": age, "rate_hz": rate, "rx": rx,
            "parsed": parsed, "bad": bad, "feed_offset_s": offset,
            "heading_deg": extra.get("heading_deg"), "speed_mps": extra.get("speed_mps"),
            "fix_mode": extra.get("fix_mode"), "sats_used": extra.get("sats_used"),
            "sentences": extra.get("sentences") or [],
            "warning": warning, "level": level}


def _for(sess, name):
    """(listener, feed) for one aircraft, or the session's own pair."""
    if name and hasattr(sess, "listener_for") and hasattr(sess, "feed_for"):
        return sess.listener_for(name), sess.feed_for(name)
    return getattr(sess, "listener", None), getattr(sess, "feed", None)


def fix_extras(sess, fix, name=None) -> dict:
    """Heading, speed, fix mode, satellites and the sentences behind the
    position the planner is using, when that position is the aircraft feed's
    last fix (same source, same place). The triggers' Fix carries lat, lon and
    time only, so this reads the listener's AircraftPos beside it. Empty for a
    radio GPS, simulated motion, no fix, or a fix without those fields."""
    if fix is None or not str(getattr(fix, "source", "")).startswith("aircraft feed"):
        return {}
    lst, _feed = _for(sess, name)
    if lst is None:
        return {}
    try:
        pos = lst.latest
    except Exception:
        return {}
    if pos is None:
        return {}
    try:
        if abs(float(pos.lat_deg) - float(fix.lat)) > 1e-7 or \
                abs(float(pos.lon_deg) - float(fix.lon)) > 1e-7:
            return {}
    except (TypeError, ValueError):
        return {}
    return {"heading_deg": getattr(pos, "heading_deg", None),
            "heading_mag_deg": getattr(pos, "heading_mag_deg", None),
            "speed_mps": getattr(pos, "speed_mps", None),
            "fix_mode": getattr(pos, "fix_mode", None),
            "sats_used": getattr(pos, "sats_used", None),
            "sentences": list(getattr(pos, "sentences", None) or [])}


def feed_state_text(st: dict) -> str:
    """One line for the flight view's feed row."""
    age = "--" if st["age_s"] is None else f"{st['age_s']:.1f} s"
    rate = "--" if st["rate_hz"] is None else f"{st['rate_hz']:.2f} Hz"
    lst = "listening" if st["listening"] else "not listening"
    hs = []
    if st.get("heading_deg") is not None:
        hs.append(f"hdg {int(round(st['heading_deg'])) % 360:03d}")
    if st.get("speed_mps") is not None:
        hs.append(f"{st['speed_mps'] / 0.514444:.0f} kt")
    if st.get("fix_mode"):
        hs.append(str(st["fix_mode"]) + (f" {st['sats_used']} sats" if st.get("sats_used") is not None else ""))
    return ((f"{st['aircraft']}: " if st.get("aircraft") else "")
            + f"source {st['source']}; feed {st['where']} {lst}"
            + (f" [{st['format']}]" if st["format"] else "")
            + f"; last fix {st['last_fix']}, age {age}; rate {rate}; rx {st['rx']}"
            + (f"; {', '.join(hs)}" if hs else ""))
