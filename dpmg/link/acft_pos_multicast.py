"""
acft_pos_multicast.py: aircraft position, heading and speed from a UDP multicast feed.

The ground station can receive the aircraft's own nav solution on a multicast
group instead of (or alongside) the radio's STATUS-heartbeat GPS. Group and
port are operator-set; the wire format is not fixed, so parsing is pluggable:
NMEA 0183, a permissive JSON object and Cursor on Target XML events are
handled, auto-detected per datagram.

NMEA sentences read, from any talker (GP, GN, GL, GA, ...):
  GGA  position, time of day, fix quality, satellites used, HDOP, altitude
  RMC  position, time and date, course and speed over ground (valid fixes only)
  GLL  position and time of day
  VTG  course over ground true and magnetic, speed in knots and km/h
  GSA  fix mode (2D or 3D), satellites used, PDOP, HDOP, VDOP
  GSV  satellites in view (the count only)
A sentence with a checksum that does not match is dropped. Sentences are
combined, not picked: a feed sends a burst each second, sometimes over
several datagrams, and the fix keeps the newest value of each field from
whichever sentence carried it within a short window (FixCombiner, 1.5 s by
default). Position and time come from GGA, RMC or GLL, altitude from GGA,
heading and speed from VTG or RMC, fix mode and DOP from GSA, satellites from
GGA, GSA and GSV. When two sentences in one burst disagree on the position the
one with the later time of day wins, then GGA over RMC over GLL. The fix says
which sentences fed it (sentences, field_src).

JSON: an object, an array of objects, or several objects in one datagram (one per
line, back to back, or pretty printed over several lines). Key names are read in
any case. A position can sit in a sub-object (position, pos, location, gps, nav,
navigation, ins, state, aircraft, platform, ownship, data, up to three levels
down); the top level wins over a sub-object, a named sub-object over any other,
and a sub-object for some other point (home, target, waypoint, origin,
destination, launch) is never read. Keys accepted (first match wins):
  position   lat, latitude, lat_deg; lon, lng, long, longitude, lon_deg
  altitude   alt, altitude, alt_m, altitude_m, elevation (metres); alt_ft,
             altitude_ft, alt_feet, altitude_feet, elevation_ft, alt_msl_ft,
             msl_ft (feet, converted to metres)
  time       time_utc, utc, timestamp, time, t, epoch (epoch seconds or
             milliseconds, or ISO 8601 text; no zone means UTC)
  heading    heading, hdg, course, cog, track, heading_deg, course_deg,
             track_deg (degrees true); heading_mag, mag_heading, hdg_mag,
             course_mag (degrees magnetic)
  speed      speed, spd, sog, gs, ground_speed, speed_mps (metres per second);
             speed_kt, speed_kts, speed_knots, sog_kt, gs_kt (knots);
             speed_kmh, speed_kph (km/h)
  fix        fix_mode, mode, fix_type, fix ("2D", "3D", "none" or 1, 2, 3 as
             in GSA); fix_quality, quality (0 to 8 as in GGA)
  dop        hdop, pdop, vdop
  satellites sats, satellites, num_sats, numsv, sats_used, satellites_used;
             sats_in_view, satellites_in_view, sats_view
A position with none of the optional fields is enough for a fix. An object with
no position but other fields (heading and speed, say) adds them to the fix the
way a VTG sentence does. A latitude outside +-90 or a longitude outside +-180 is
not taken as a position.

Cursor on Target (CoT) XML, format setting xml: one <event> per datagram, or several back to back,
each with or without its own <?xml ...?> line. Read from each event:
  position   point lat and lon (decimal degrees)
  altitude   point hae (metres above the ellipsoid; 9999999 means not known)
  time       event time (ISO 8601, UTC)
  heading    detail/track course (degrees true, course over ground)
  speed      detail/track speed (metres per second)
The event uid is kept in the fix source, as "cot:Blackjack.PPLI.1162", so the
panel shows whose position it is. Every event on the group is read: if the group
carries more than one uid, set the uid filter (the listener's cot_uid) or the fix
jumps between them. An event at 0, 0 with ce 9999999 is the CoT way to say no
position and is not taken as one. A datagram with a DOCTYPE or an ENTITY in it
is dropped. attitude, spin and the other detail parts are not read.

Stdlib only. The listener runs its own daemon thread and exposes the latest
fix under a lock, so a NiceGUI timer can poll it without touching sockets.
"""
from __future__ import annotations

import calendar
import collections
import json
import os
import re
import socket
import struct
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class AircraftPos:
    lat_deg: float
    lon_deg: float
    alt_m: Optional[float]          # None when the sentence carries no altitude
    t_mono: float                   # monotonic arrival time, for staleness
    source: str                     # "nmea:GPGGA+GPVTG" / "json": what fed the fix
    raw: str
    # the feed's own time stamp as UTC epoch seconds, when the sentence carries
    # one (GGA, RMC and GLL do, JSON may); None when it does not. The ground
    # station compares it with its own clock on arrival (t_wall).
    t_feed_utc: Optional[float] = None
    t_wall: float = field(default_factory=time.time)
    # the rest is optional: None (or empty) when nothing carried it
    heading_deg: Optional[float] = None       # course over ground, degrees true
    heading_mag_deg: Optional[float] = None   # course over ground, degrees magnetic
    speed_mps: Optional[float] = None         # ground speed, metres per second
    fix_mode: Optional[str] = None            # "2D", "3D" or "none" (GSA)
    fix_quality: Optional[int] = None         # GGA quality: 0 none, 1 GPS, 2 DGPS, ...
    pdop: Optional[float] = None
    hdop: Optional[float] = None
    vdop: Optional[float] = None
    sats_used: Optional[int] = None           # satellites in the solution (GGA, GSA)
    sats_view: Optional[int] = None           # satellites in view (GSV)
    sentences: list = field(default_factory=list)   # names that fed the fix, e.g. GPGGA
    field_src: dict = field(default_factory=dict)   # field name: sentence (or "json")
    field_t: dict = field(default_factory=dict)     # field name: monotonic time it was set

    @property
    def speed_kt(self) -> Optional[float]:
        return None if self.speed_mps is None else self.speed_mps / KT_MPS

    def field_age_s(self, name: str, now_mono: float = None) -> Optional[float]:
        """Seconds since the named field was last carried, or None."""
        t = self.field_t.get(name)
        if t is None:
            return None
        now_mono = time.monotonic() if now_mono is None else now_mono
        return max(0.0, now_mono - float(t))


KT_MPS = 0.514444                   # one knot in metres per second
KMH_MPS = 1.0 / 3.6


@dataclass
class Packet:
    """One datagram as received, for the raw packet view and recordings."""
    t_wall: float                   # arrival, wall clock (epoch seconds)
    t_mono: float
    addr: str                       # sender ip:port
    raw: bytes
    pos: Optional[AircraftPos]      # the parsed fix, or None when it did not parse

    @property
    def text(self) -> str:
        return self.raw.decode("ascii", "replace").strip()


# feed time stamps

def nmea_time_utc(hhmmss: str, ddmmyy: str = "", now: float = None) -> Optional[float]:
    """UTC epoch seconds from an NMEA time field (hhmmss.ss) and, when the
    sentence has one (RMC), its date field (ddmmyy). With no date, GGA, the
    date is the one that puts the stamp nearest the ground station's own clock,
    so a feed crossing midnight stays right."""
    if not hhmmss or len(hhmmss) < 6:
        return None
    try:
        hh, mm = int(hhmmss[0:2]), int(hhmmss[2:4])
        ss = float(hhmmss[4:])
    except ValueError:
        return None
    if not (0 <= hh < 24 and 0 <= mm < 60 and 0 <= ss < 61):
        return None
    sod = hh * 3600 + mm * 60 + ss
    if ddmmyy and len(ddmmyy) >= 6:
        try:
            d, m, y = int(ddmmyy[0:2]), int(ddmmyy[2:4]), int(ddmmyy[4:6]) + 2000
            return calendar.timegm((y, m, d, 0, 0, 0)) + sod
        except (ValueError, OverflowError):
            return None
    now = time.time() if now is None else now
    day0 = now - (now % 86400)
    best = None
    for day in (day0 - 86400, day0, day0 + 86400):
        t = day + sod
        if best is None or abs(t - now) < abs(best - now):
            best = t
    return best


_TIME_KEYS = ("time_utc", "utc", "timestamp", "time", "t", "epoch")


def json_time_utc(d: dict) -> Optional[float]:
    """A feed time from a JSON object: epoch seconds (or milliseconds when the
    number is too big for seconds), or an ISO 8601 text with or without a zone
    (no zone means UTC). None when there is none it can read."""
    import datetime
    for k in _TIME_KEYS:
        if k not in d or d[k] is None:
            continue
        v = d[k]
        if isinstance(v, (int, float)):
            v = float(v)
            if v > 1e11:                  # milliseconds
                v /= 1000.0
            return v if v > 0 else None
        if isinstance(v, str):
            txt = v.strip().replace("Z", "+00:00")
            try:
                dt = datetime.datetime.fromisoformat(txt)
            except ValueError:
                try:
                    return float(txt)
                except ValueError:
                    return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return dt.timestamp()
    return None


# parsers. Each NMEA sentence or JSON object becomes a Reading (the fields it
# carried); a FixCombiner merges readings into the AircraftPos the GUI sees.

def _nmea_checksum_ok(sentence: str) -> bool:
    # "$....*HH": XOR of everything between '$' and '*' must equal HH. Feeds
    # without a checksum (no '*') are accepted; a present-but-wrong one is not.
    star = sentence.rfind("*")
    if star < 0:
        return True
    body, hexcs = sentence[1:star], sentence[star + 1:star + 3]
    try:
        want = int(hexcs, 16)
    except ValueError:
        return False
    got = 0
    for ch in body:
        got ^= ord(ch)
    return got == want


def nmea_checksum(body: str) -> str:
    """The two hex digits for a sentence body (everything between $ and *)."""
    cs = 0
    for ch in body:
        cs ^= ord(ch)
    return f"{cs:02X}"


def _nmea_deg(val: str, hemi: str) -> Optional[float]:
    # NMEA packs lat as ddmm.mmmm and lon as dddmm.mmmm: degrees are all but
    # the last two integer digits, the rest is minutes.
    if not val:
        return None
    try:
        dot = val.index(".")
    except ValueError:
        return None
    try:
        deg = float(val[:dot - 2] or "0")
        minutes = float(val[dot - 2:])
    except ValueError:
        return None
    dd = deg + minutes / 60.0
    if hemi in ("S", "W"):
        dd = -dd
    return dd


def _f(txt: str) -> Optional[float]:
    try:
        return float(txt) if txt not in ("", None) else None
    except ValueError:
        return None


def _i(txt: str) -> Optional[int]:
    try:
        return int(txt) if txt not in ("", None) else None
    except ValueError:
        return None


def _tod_s(hhmmss: str) -> Optional[float]:
    """Seconds since midnight from an NMEA time field, or None."""
    if not hhmmss or len(hhmmss) < 6:
        return None
    try:
        hh, mm, ss = int(hhmmss[0:2]), int(hhmmss[2:4]), float(hhmmss[4:])
    except ValueError:
        return None
    if not (0 <= hh < 24 and 0 <= mm < 60 and 0 <= ss < 61):
        return None
    return hh * 3600 + mm * 60 + ss


# the position sentences, best first, for the disagreement rule
POS_PRIORITY = {"GGA": 3, "RMC": 2, "GLL": 1}
SENTENCE_KINDS = ("GGA", "RMC", "GLL", "VTG", "GSA", "GSV")
_FIX_MODES = {1: "none", 2: "2D", 3: "3D"}


@dataclass
class Reading:
    """What one sentence (or one JSON object) carried. name is the sentence
    as sent (GPGGA) or "json"; kind is the three letter type or "json". Only
    the fields present are in values, keyed as the AircraftPos fields are
    (lat_deg, lon_deg, alt_m, heading_deg, ...). tod_s is the time of day the
    sentence stamped, for the position rule; date is (y, m, d) when it had one."""
    name: str
    kind: str
    values: dict
    raw: str
    tod_s: Optional[float] = None
    date: Optional[tuple] = None
    t_feed_utc: Optional[float] = None
    tag: str = ""                   # a CoT event's uid

    @property
    def has_position(self) -> bool:
        return "lat_deg" in self.values and "lon_deg" in self.values


def parse_sentence(line: str, now: float = None) -> Optional[Reading]:
    """One NMEA sentence to a Reading, or None when it is not one of the six
    kinds read here, its checksum is wrong, or its fields do not parse."""
    line = line.strip()
    if not line.startswith("$") or len(line) < 7 or not _nmea_checksum_ok(line):
        return None
    star = line.rfind("*")
    fields = (line[:star] if star >= 0 else line).split(",")
    name = fields[0][1:]
    kind = name[-3:]                # strip the talker id ($GP, $GN, ...)
    v = {}
    rd = Reading(name, kind, v, line)
    if kind == "GGA" and len(fields) >= 10:
        lat, lon = _nmea_deg(fields[2], fields[3]), _nmea_deg(fields[4], fields[5])
        if lat is None or lon is None:
            return None
        v["lat_deg"], v["lon_deg"] = lat, lon
        alt = _f(fields[9])
        if alt is not None:
            v["alt_m"] = alt
        q = _i(fields[6])
        if q is not None:
            v["fix_quality"] = q
        n = _i(fields[7])
        if n is not None:
            v["sats_used"] = n
        h = _f(fields[8])
        if h is not None:
            v["hdop"] = h
        rd.tod_s = _tod_s(fields[1])
        rd.t_feed_utc = nmea_time_utc(fields[1], now=now)
    elif kind == "RMC" and len(fields) >= 7:
        if fields[2] != "A":        # only a valid fix
            return None
        lat, lon = _nmea_deg(fields[3], fields[4]), _nmea_deg(fields[5], fields[6])
        if lat is None or lon is None:
            return None
        v["lat_deg"], v["lon_deg"] = lat, lon
        if len(fields) > 7:
            spd = _f(fields[7])
            if spd is not None:
                v["speed_mps"] = spd * KT_MPS
        if len(fields) > 8:
            crs = _f(fields[8])
            if crs is not None:
                v["heading_deg"] = crs % 360.0
        date = fields[9] if len(fields) > 9 else ""
        if date and len(date) >= 6:
            try:
                rd.date = (int(date[4:6]) + 2000, int(date[2:4]), int(date[0:2]))
            except ValueError:
                rd.date = None
        rd.tod_s = _tod_s(fields[1])
        rd.t_feed_utc = nmea_time_utc(fields[1], date, now=now)
    elif kind == "GLL" and len(fields) >= 5:
        if len(fields) > 6 and fields[6] and fields[6] != "A":
            return None             # status V: no valid position
        lat, lon = _nmea_deg(fields[1], fields[2]), _nmea_deg(fields[3], fields[4])
        if lat is None or lon is None:
            return None
        v["lat_deg"], v["lon_deg"] = lat, lon
        if len(fields) > 5:
            rd.tod_s = _tod_s(fields[5])
            rd.t_feed_utc = nmea_time_utc(fields[5], now=now)
    elif kind == "VTG" and len(fields) >= 2:
        # $--VTG,cogt,T,cogm,M,sog,N,sog,K,mode
        crs = _f(fields[1])
        if crs is not None:
            v["heading_deg"] = crs % 360.0
        if len(fields) > 3:
            mag = _f(fields[3])
            if mag is not None:
                v["heading_mag_deg"] = mag % 360.0
        kt = _f(fields[5]) if len(fields) > 5 else None
        kmh = _f(fields[7]) if len(fields) > 7 else None
        if kt is not None:
            v["speed_mps"] = kt * KT_MPS
        elif kmh is not None:
            v["speed_mps"] = kmh * KMH_MPS
        if not v:
            return None
    elif kind == "GSA" and len(fields) >= 3:
        # $--GSA,mode,fix,sv1..sv12,pdop,hdop,vdop
        m = _i(fields[2])
        if m is not None:
            v["fix_mode"] = _FIX_MODES.get(m, "none")
        ids = [x for x in fields[3:15] if x]
        if len(fields) >= 15:
            v["sats_used"] = len(ids)
        for idx, key in ((15, "pdop"), (16, "hdop"), (17, "vdop")):
            if len(fields) > idx:
                d = _f(fields[idx])
                if d is not None:
                    v[key] = d
        if not v:
            return None
    elif kind == "GSV" and len(fields) >= 4:
        # $--GSV,total msgs,msg no,sats in view,...; the count is all that is read
        n = _i(fields[3])
        if n is None:
            return None
        v["sats_view"] = n
    else:
        return None
    return rd


class FixCombiner:
    """Merges readings into one fix. Each field keeps its newest value with
    the sentence that carried it and when; a field older than window_s
    (against the newest reading) drops out. The position is one unit (lat,
    lon, the time it was stamped): a new position replaces the held one when
    its time of day is later, or, with the same or no time of day, when its
    sentence ranks the same or higher (GGA over RMC over GLL, JSON like GGA).
    fix() gives None until a position is held."""

    def __init__(self, window_s: float = 1.5):
        self.window_s = float(window_s)
        self._vals = {}             # field: (value, source name, t_mono)
        self._pos = None            # (lat, lon, tod_s, priority, name, t_mono, raw, t_feed_utc, tag)
        self._date = None           # (y, m, d) from the last RMC, with its t_mono
        self._names = []            # (name, t_mono) of the readings merged
        self.t_last = None

    def set_window(self, window_s: float) -> None:
        self.window_s = max(0.05, float(window_s))

    def add(self, rd: Reading, t_mono: float = None) -> None:
        t_mono = time.monotonic() if t_mono is None else t_mono
        self.t_last = t_mono
        self._names.append((rd.name, t_mono))
        self._names = [(n, t) for n, t in self._names if t_mono - t <= self.window_s]
        for k, val in rd.values.items():
            if k in ("lat_deg", "lon_deg"):
                continue
            self._vals[k] = (val, rd.name, t_mono)
        if rd.date is not None:
            self._date = (rd.date, t_mono)
        if rd.has_position:
            prio = POS_PRIORITY.get(rd.kind, 3)
            cand = (rd.values["lat_deg"], rd.values["lon_deg"], rd.tod_s, prio,
                    rd.name, t_mono, rd.raw, rd.t_feed_utc, rd.tag)
            held = self._pos
            if held is None or t_mono - held[5] > self.window_s:
                self._pos = cand
            elif cand[2] is not None and held[2] is not None and cand[2] != held[2]:
                if cand[2] > held[2] or (held[2] - cand[2]) > 43200:   # past midnight
                    self._pos = cand
            elif prio >= held[3]:
                self._pos = cand

    def fix(self, t_mono: float = None) -> Optional[AircraftPos]:
        """The combined fix as of t_mono (default: the newest reading's time),
        or None when no position is held within the window."""
        if self._pos is None:
            return None
        t_mono = self.t_last if t_mono is None else t_mono
        lat, lon, tod, prio, pname, pt, raw, t_feed, tag = self._pos
        if t_mono - pt > self.window_s:
            return None
        fresh = {k: v for k, v in self._vals.items() if t_mono - v[2] <= self.window_s}
        # a GGA or GLL time of day gets the date from an RMC in the same window
        if (t_feed is not None and tod is not None and self._date is not None
                and t_mono - self._date[1] <= self.window_s and pname[-3:] != "RMC"):
            y, m, d = self._date[0]
            try:
                t_feed = calendar.timegm((y, m, d, 0, 0, 0)) + tod
            except (ValueError, OverflowError):
                pass
        names = []
        for n, _t in sorted(self._names, key=lambda x: x[1]):
            if n not in names and t_mono - _t <= self.window_s:
                names.append(n)
        if pname not in names:
            names.insert(0, pname)
        if names == ["json"]:
            src = "json"
        elif names == ["cot"]:
            src = "cot:" + tag if tag else "cot"
        else:
            src = "nmea:" + "+".join(n for n in names)
        pos = AircraftPos(lat, lon, fresh["alt_m"][0] if "alt_m" in fresh else None,
                          pt, src, raw, t_feed_utc=t_feed)
        pos.field_src = {"lat_deg": pname, "lon_deg": pname}
        pos.field_t = {"lat_deg": pt, "lon_deg": pt}
        for k, (val, name, t) in fresh.items():
            setattr(pos, k, val)
            pos.field_src[k] = name
            pos.field_t[k] = t
        pos.sentences = names
        return pos

    def reset(self) -> None:
        self._vals.clear()
        self._pos = None
        self._date = None
        self._names = []


def parse_nmea(line: str, combiner: FixCombiner = None,
               t_mono: float = None) -> Optional[AircraftPos]:
    """One sentence to a fix. Without a combiner, a position sentence (GGA,
    RMC, GLL) gives a fix of its own and any other kind gives None; with one,
    the sentence is merged and the combined fix comes back (None until a
    position is held)."""
    rd = parse_sentence(line)
    if rd is None:
        return None
    if combiner is None:
        if not rd.has_position:
            return None
        combiner = FixCombiner()
    combiner.add(rd, t_mono)
    return combiner.fix(t_mono)


# accept the common spellings without forcing the feed to one schema
_LAT_KEYS = ("lat", "latitude", "lat_deg")
_LON_KEYS = ("lon", "lng", "long", "longitude", "lon_deg")
_ALT_KEYS = ("alt", "altitude", "alt_m", "altitude_m", "elevation")
_HDG_KEYS = ("heading", "hdg", "course", "cog", "track", "heading_deg", "course_deg",
             "track_deg")
_HDG_MAG_KEYS = ("heading_mag", "mag_heading", "hdg_mag", "course_mag")
_SPD_KEYS = (("speed", 1.0), ("spd", 1.0), ("sog", 1.0), ("gs", 1.0), ("ground_speed", 1.0),
             ("speed_mps", 1.0), ("speed_kt", KT_MPS), ("speed_kts", KT_MPS),
             ("speed_knots", KT_MPS), ("sog_kt", KT_MPS), ("gs_kt", KT_MPS),
             ("speed_kmh", KMH_MPS), ("speed_kph", KMH_MPS))
_FIX_MODE_KEYS = ("fix_mode", "mode", "fix_type", "fix")
_FIX_Q_KEYS = ("fix_quality", "quality")
_SATS_KEYS = ("sats", "satellites", "num_sats", "numsv", "sats_used", "satellites_used")
_SATS_VIEW_KEYS = ("sats_in_view", "satellites_in_view", "sats_view")
_ALT_FT_KEYS = ("alt_ft", "altitude_ft", "alt_feet", "altitude_feet", "elevation_ft",
                "alt_msl_ft", "msl_ft")
FT_M = 0.3048
# sub-objects that may carry the aircraft's own fields, read before any other
_POS_GROUPS = ("position", "pos", "location", "loc", "gps", "gnss", "nav", "navigation", "ins",
               "fix", "state", "aircraft", "platform", "ownship", "data", "attitude",
               "orientation", "velocity", "motion", "kinematics")
# sub-objects that hold some other point, never read for the aircraft
_OTHER_POINTS = ("home", "target", "waypoint", "wp", "origin", "destination", "dest", "launch",
                 "takeoff", "next", "goal", "base")


def _first(d: dict, keys) -> Optional[float]:
    for k in keys:
        if k in d and d[k] is not None:
            try:
                return float(d[k])
            except (TypeError, ValueError):
                return None
    return None


def _json_speed_mps(d: dict) -> Optional[float]:
    for k, scale in _SPD_KEYS:
        if k in d and d[k] is not None:
            try:
                return float(d[k]) * scale
            except (TypeError, ValueError):
                return None
    return None


def _json_fix_mode(d: dict) -> Optional[str]:
    for k in _FIX_MODE_KEYS:
        if k not in d or d[k] is None:
            continue
        val = d[k]
        if isinstance(val, bool):
            return "3D" if val else "none"
        if isinstance(val, (int, float)):
            return _FIX_MODES.get(int(val), "none")
        txt = str(val).strip().upper()
        if txt in ("2D", "3D"):
            return txt
        if txt in ("NONE", "NO FIX", "NOFIX", "0", "1", "N", "V"):
            return "none"
        return None
    return None


def _lower_keys(d: dict) -> dict:
    return {str(k).strip().lower(): v for k, v in d.items()}


def _pos_of(d: dict):
    """(lat, lon) when both are there and in range, else None."""
    lat, lon = _first(d, _LAT_KEYS), _first(d, _LON_KEYS)
    if lat is None or lon is None or not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        return None
    return lat, lon


def _flat_view(obj: dict, depth: int = 0) -> dict:
    """One flat, lower case view of a JSON object. Its own plain values come first.
    The position is taken as a pair from the top level, else from the first
    position-like sub-object (position, gps, nav, ...) that has one, else from
    the first other sub-object that has one. Other fields are filled from the
    position-like sub-objects and from the sub-object the position came from.
    Sub-objects for another point (home, target, ...) are skipped."""
    top = _lower_keys(obj)
    out = {k: v for k, v in top.items() if not isinstance(v, (dict, list))}
    subs = []
    if depth < 3:
        subs = [(k, _flat_view(v, depth + 1)) for k, v in top.items()
                if isinstance(v, dict) and k not in _OTHER_POINTS]
    named = [sv for k, sv in subs if k in _POS_GROUPS]
    other = [sv for k, sv in subs if k not in _POS_GROUPS]
    pos_from = None
    if _pos_of(out) is None:
        for sv in named + other:
            if _pos_of(sv) is not None:
                pos_from = sv
                break
    for sv in named + ([pos_from] if pos_from is not None and pos_from not in named else []):
        for k, v in sv.items():
            if k in _LAT_KEYS or k in _LON_KEYS:
                continue
            out.setdefault(k, v)
    if pos_from is not None:
        for k in _LAT_KEYS + _LON_KEYS:
            if k in pos_from:
                out[k] = pos_from[k]
    return out


def _json_values(text: str):
    """Every JSON value in text, in order: one object, an array, several objects
    back to back or one per line, pretty printed or not. None when the text is
    not JSON all the way through."""
    dec = json.JSONDecoder()
    vals, i, n = [], 0, len(text)
    while True:
        while i < n and text[i] in " \t\r\n,":
            i += 1
        if i >= n:
            return vals
        try:
            val, i = dec.raw_decode(text, i)
        except ValueError:
            return None
        vals.append(val)


def _json_objects(vals) -> list:
    """The objects in a list of JSON values, arrays opened up."""
    out = []
    for v in vals:
        if isinstance(v, dict):
            out.append(v)
        elif isinstance(v, list):
            out.extend(_json_objects(v))
    return out


def _json_reading(obj, raw: str) -> Optional[Reading]:
    """The fields one JSON object carried, or None when it carried none of them."""
    if not isinstance(obj, dict):
        return None
    d = _flat_view(obj)
    v = {}
    pos = _pos_of(d)
    if pos is not None:
        v["lat_deg"], v["lon_deg"] = pos
    alt = _first(d, _ALT_KEYS)
    if alt is None:
        ft = _first(d, _ALT_FT_KEYS)
        if ft is not None:
            alt = ft * FT_M
    if alt is not None:
        v["alt_m"] = alt
    hdg = _first(d, _HDG_KEYS)
    if hdg is not None:
        v["heading_deg"] = hdg % 360.0
    mag = _first(d, _HDG_MAG_KEYS)
    if mag is not None:
        v["heading_mag_deg"] = mag % 360.0
    spd = _json_speed_mps(d)
    if spd is not None:
        v["speed_mps"] = spd
    mode = _json_fix_mode(d)
    if mode is not None:
        v["fix_mode"] = mode
    q = _first(d, _FIX_Q_KEYS)
    if q is not None:
        v["fix_quality"] = int(q)
    for k in ("hdop", "pdop", "vdop"):
        val = _first(d, (k,))
        if val is not None:
            v[k] = val
    n = _first(d, _SATS_KEYS)
    if n is not None:
        v["sats_used"] = int(n)
    n = _first(d, _SATS_VIEW_KEYS)
    if n is not None:
        v["sats_view"] = int(n)
    if not v:
        return None
    t = json_time_utc(d)
    rd = Reading("json", "json", v, raw, t_feed_utc=t)
    if t is not None:
        rd.tod_s = t % 86400
    return rd


def json_readings(text: str) -> list:
    """A Reading for every JSON object in text that carried a field read here."""
    vals = _json_values(text.strip())
    if not vals:
        return []
    objs = _json_objects(vals)
    one = len(objs) == 1
    out = []
    for o in objs:
        rd = _json_reading(o, text.strip() if one else json.dumps(o, separators=(",", ":")))
        if rd is not None:
            out.append(rd)
    return out


def parse_json_reading(data: str) -> Optional[Reading]:
    """The first JSON object in data that carried a field read here."""
    rds = json_readings(data)
    return rds[0] if rds else None


def parse_json(data: str, combiner: FixCombiner = None,
               t_mono: float = None) -> Optional[AircraftPos]:
    rds = json_readings(data)
    if not rds:
        return None
    combiner = FixCombiner() if combiner is None else combiner
    for rd in rds:
        combiner.add(rd, t_mono)
    return combiner.fix(t_mono)


# Cursor on Target XML

_COT_EVENT = re.compile(r"<event\b.*?</event\s*>", re.S)
_COT_UNKNOWN = 9999999.0
_ISO = re.compile(r"(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d):(\d\d)(\.\d+)?\s*(Z|[+-]\d\d:?\d\d)?$")


def iso_time_utc(txt: str) -> Optional[float]:
    """UTC epoch seconds from an ISO 8601 time with any number of decimals
    (2026-11-17T15:24:24.97Z). No zone means UTC. None when it does not read."""
    m = _ISO.match(str(txt or "").strip())
    if not m:
        return None
    y, mo, d, hh, mm, ss = (int(x) for x in m.groups()[:6])
    try:
        t = calendar.timegm((y, mo, d, hh, mm, ss))
    except (ValueError, OverflowError):
        return None
    t += float(m.group(7) or 0.0)
    zone = m.group(8)
    if zone and zone != "Z":
        sign = -1 if zone[0] == "-" else 1
        digits = zone[1:].replace(":", "")
        t -= sign * (int(digits[:2]) * 3600 + int(digits[2:4]) * 60)
    return t


def _cot_tag(el) -> str:
    return el.tag.rsplit("}", 1)[-1].lower()


def _cot_child(el, name: str):
    for c in el:
        if _cot_tag(c) == name:
            return c
    return None


def _cot_reading(chunk: str) -> Optional[Reading]:
    """One <event> to a Reading, or None when it is not one or carries nothing."""
    import xml.etree.ElementTree as ET
    try:
        ev = ET.fromstring(chunk)
    except ET.ParseError:
        return None
    if _cot_tag(ev) != "event":
        return None
    v = {}
    pt = _cot_child(ev, "point")
    if pt is not None:
        lat, lon, ce = _f(pt.get("lat", "")), _f(pt.get("lon", "")), _f(pt.get("ce", ""))
        no_pos = lat == 0.0 and lon == 0.0 and ce is not None and ce >= _COT_UNKNOWN
        if (lat is not None and lon is not None and not no_pos
                and -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            v["lat_deg"], v["lon_deg"] = lat, lon
            hae = _f(pt.get("hae", ""))
            if hae is not None and abs(hae) < _COT_UNKNOWN:
                v["alt_m"] = hae
    det = _cot_child(ev, "detail")
    trk = _cot_child(det, "track") if det is not None else None
    if trk is not None:
        crs, spd = _f(trk.get("course", "")), _f(trk.get("speed", ""))
        if crs is not None:
            v["heading_deg"] = crs % 360.0
        if spd is not None and spd >= 0.0:
            v["speed_mps"] = spd
    if not v:
        return None
    t = iso_time_utc(ev.get("time") or ev.get("start") or "")
    rd = Reading("cot", "cot", v, chunk, t_feed_utc=t, tag=str(ev.get("uid") or "").strip())
    if t is not None:
        rd.tod_s = t % 86400
    return rd


def cot_readings(text: str, uid: str = "") -> list:
    """A Reading for every CoT event in text that carried a field read here.
    uid, when set, keeps only the events with that uid."""
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        return []
    out = []
    for m in _COT_EVENT.finditer(text):
        rd = _cot_reading(m.group(0))
        if rd is not None and (not uid or rd.tag == uid):
            out.append(rd)
    return out


def cot_uids(data: bytes) -> list:
    """The uid of every CoT event in a datagram, in order."""
    return [rd.tag for rd in cot_readings(_decode(data))]


def _parse_text(text: str, combiner: FixCombiner, t_mono: float,
                want: str, cot_uid: str = "") -> Optional[AircraftPos]:
    """Every reading of a datagram into the combiner; the fix after the last
    one. want is "json", "nmea", "cot" or "auto". A datagram that is JSON all
    the way through (one object, an array, several objects, pretty printed) is
    read as a whole, and so is one that starts with XML; anything else line by
    line, so NMEA and one-line JSON can mix."""
    got_any = False
    body = text.strip()
    if want in ("auto", "cot") and body[:1] == "<":
        for rd in cot_readings(body, cot_uid):
            combiner.add(rd, t_mono)
            got_any = True
        return combiner.fix(t_mono) if got_any else None
    if want == "cot":
        return None
    if want in ("auto", "json") and body[:1] in "{[":
        vals = _json_values(body)
        if vals is not None:
            for rd in json_readings(body):
                combiner.add(rd, t_mono)
                got_any = True
            return combiner.fix(t_mono) if got_any else None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        rds = []
        if line[0] in "{[" and want in ("auto", "json"):
            rds = json_readings(line)
        elif line[0] == "$" and want in ("auto", "nmea"):
            rd = parse_sentence(line)
            rds = [rd] if rd is not None else []
        for rd in rds:
            combiner.add(rd, t_mono)
            got_any = True
    if not got_any:
        return None
    return combiner.fix(t_mono)


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8", "ignore").strip()
    except Exception:
        return ""


def parse_auto(data: bytes, combiner: FixCombiner = None,
               t_mono: float = None, cot_uid: str = "") -> Optional[AircraftPos]:
    text = _decode(data)
    if not text or text[0] not in "{[$<":
        return None
    return _parse_text(text, FixCombiner() if combiner is None else combiner, t_mono, "auto", cot_uid)


def parse_nmea_bytes(data: bytes, combiner: FixCombiner = None,
                     t_mono: float = None) -> Optional[AircraftPos]:
    text = _decode(data)
    if not text:
        return None
    return _parse_text(text, FixCombiner() if combiner is None else combiner, t_mono, "nmea")


def parse_json_bytes(data: bytes, combiner: FixCombiner = None,
                     t_mono: float = None) -> Optional[AircraftPos]:
    text = _decode(data)
    if not text:
        return None
    return _parse_text(text, FixCombiner() if combiner is None else combiner, t_mono, "json")


def parse_cot_bytes(data: bytes, combiner: FixCombiner = None,
                    t_mono: float = None, cot_uid: str = "") -> Optional[AircraftPos]:
    text = _decode(data)
    if not text:
        return None
    return _parse_text(text, FixCombiner() if combiner is None else combiner, t_mono, "cot", cot_uid)


def sentences_in(data: bytes) -> list:
    """The NMEA sentence names in a datagram that parse (checksum good, a kind
    read here), in order, and the count of lines that start with $ but did
    not: (names, bad)."""
    names, bad = [], 0
    for line in _decode(data).splitlines():
        line = line.strip()
        if not line.startswith("$"):
            continue
        rd = parse_sentence(line)
        if rd is None:
            bad += 1
        else:
            names.append(rd.name)
    return names, bad


FORMATS = ("auto", "json", "nmea", "xml")


def parser_for(fmt: str) -> Callable[..., Optional[AircraftPos]]:
    """The parser for a feed format setting: auto (sniff each datagram),
    json, nmea or xml (Cursor on Target events; cot is taken as the same). An
    unknown name is auto. Each takes (data, combiner=None, t_mono=None)."""
    f = str(fmt or "auto").strip().lower()
    if f in ("xml", "cot"):
        return parse_cot_bytes
    if f == "json":
        return parse_json_bytes
    if f == "nmea":
        return parse_nmea_bytes
    return parse_auto


# text for the GUI, shared by the header chip, the panels and the planner row

def heading_text(pos) -> str:
    """"hdg 087" or "" when no heading is known."""
    h = getattr(pos, "heading_deg", None)
    return "" if h is None else f"hdg {int(round(h)) % 360:03d}"


def speed_text(pos) -> str:
    """"142 kt" or "" when no speed is known."""
    s = getattr(pos, "speed_mps", None)
    return "" if s is None else f"{s / KT_MPS:.0f} kt"


def summary_text(pos, sep: str = ", ") -> str:
    """The one line chip text: position, altitude, then heading and speed when
    known, as "+33.45210 -112.00170 alt 1240 m, hdg 087, 142 kt"."""
    alt = "alt -- m" if pos.alt_m is None else f"alt {pos.alt_m:.0f} m"
    parts = [f"{pos.lat_deg:+.5f} {pos.lon_deg:+.5f} {alt}"]
    for t in (heading_text(pos), speed_text(pos)):
        if t:
            parts.append(t)
    return sep.join(parts)


FIX_FIELDS = (
    # (field, label, formatter)
    ("lat_deg", "latitude", lambda v: f"{v:+.6f}"),
    ("lon_deg", "longitude", lambda v: f"{v:+.6f}"),
    ("alt_m", "altitude", lambda v: f"{v:.1f} m"),
    ("heading_deg", "heading (true)", lambda v: f"{v:05.1f} deg"),
    ("heading_mag_deg", "heading (magnetic)", lambda v: f"{v:05.1f} deg"),
    ("speed_mps", "ground speed", lambda v: f"{v / KT_MPS:.1f} kt ({v:.1f} m/s)"),
    ("fix_mode", "fix mode", lambda v: str(v)),
    ("fix_quality", "fix quality", lambda v: _QUALITY.get(int(v), str(v))),
    ("pdop", "PDOP", lambda v: f"{v:.1f}"),
    ("hdop", "HDOP", lambda v: f"{v:.1f}"),
    ("vdop", "VDOP", lambda v: f"{v:.1f}"),
    ("sats_used", "satellites used", lambda v: str(int(v))),
    ("sats_view", "satellites in view", lambda v: str(int(v))),
)
_QUALITY = {0: "0 no fix", 1: "1 GPS", 2: "2 DGPS", 3: "3 PPS", 4: "4 RTK fixed",
            5: "5 RTK float", 6: "6 dead reckoning", 7: "7 manual", 8: "8 simulation"}


def _hms(t_wall, ms=True) -> str:
    if not t_wall:
        return "--:--:--"
    base = time.strftime("%H:%M:%S", time.gmtime(t_wall))
    return base + (f".{int((t_wall % 1) * 1000):03d}" if ms else "") + "Z"


def fix_rows(pos, now_mono: float = None, now_wall: float = None) -> list:
    """One row per field for a table: field, value, source (the sentence or
    key set that carried it, "--" when nothing did), age (seconds since that
    sentence, "--"). The last rows are the feed time against this console's
    clock and the arrival time. pos None gives every row empty."""
    now_mono = time.monotonic() if now_mono is None else now_mono
    now_wall = time.time() if now_wall is None else now_wall
    rows = []
    for key, label, fmt in FIX_FIELDS:
        val = getattr(pos, key, None) if pos is not None else None
        if val is None:
            rows.append({"field": label, "value": "--", "source": "--", "age": "--"})
            continue
        src = pos.field_src.get(key, pos.source.split(":", 1)[-1].split("+")[0])
        age = pos.field_age_s(key, now_mono)
        if age is None:
            age = max(0.0, now_mono - pos.t_mono)
        rows.append({"field": label, "value": fmt(val), "source": src, "age": f"{age:.1f} s"})
    if pos is None:
        rows.append({"field": "feed time", "value": "--", "source": "--", "age": "--"})
        rows.append({"field": "arrived (GCS clock)", "value": "--", "source": "--", "age": "--"})
        return rows
    age_all = max(0.0, now_mono - pos.t_mono)
    if pos.t_feed_utc is None:
        rows.append({"field": "feed time", "value": "none in the datagram", "source": "--",
                     "age": f"{age_all:.1f} s"})
    else:
        off = pos.t_feed_utc - pos.t_wall
        rows.append({"field": "feed time", "value": f"{_hms(pos.t_feed_utc)} ({off:+.3f} s vs GCS)",
                     "source": pos.field_src.get("lat_deg", "--"), "age": f"{age_all:.1f} s"})
    rows.append({"field": "arrived (GCS clock)", "value": _hms(pos.t_wall),
                 "source": pos.source, "age": f"{age_all:.1f} s"})
    return rows


def sentence_rates_text(rates: dict, expected=SENTENCE_KINDS) -> str:
    """One line like "GGA 1.0 Hz, RMC 1.0 Hz, VTG 1.0 Hz, GSA 0.2 Hz, GSV none",
    the expected kinds first (none when never seen, "last 23 s ago" when seen
    but not in the window), then any other sentence seen."""
    parts = []
    bykind = {}
    for name, r in rates.items():
        bykind.setdefault(name[-3:], []).append((name, r))
    for kind in expected:
        got = bykind.pop(kind, None)
        if not got:
            parts.append(f"{kind} none")
            continue
        for name, r in got:
            parts.append(_rate_part(name, r))
    for kind in sorted(bykind):
        for name, r in bykind[kind]:
            parts.append(_rate_part(name, r))
    return ", ".join(parts)


def _rate_part(name: str, r: dict) -> str:
    hz = r.get("hz")
    if hz is None or hz <= 0:
        age = r.get("age_s")
        return f"{name} last {age:.0f} s ago" if age is not None else f"{name} none"
    return f"{name} {hz:.1f} Hz" if hz >= 0.95 else f"{name} {hz:.2f} Hz"


# socket setup, shared by the listener and its tests

def _bind_address(iface_ip: str, platform: str = None) -> str:
    """The address the listener socket binds to. Linux: always "" (all
    interfaces); binding the interface's own address there would make the
    kernel drop the multicast, so the membership does the filtering. Windows
    is the other way round: a socket bound to INADDR_ANY only sees the group
    on the default-route adapter (usually Wi-Fi), while a socket bound to an
    adapter's unicast address receives the group on that adapter. So on
    win32 bind the chosen interface address when one is given."""
    platform = platform or sys.platform
    ip = (iface_ip or "").strip()
    if platform == "win32" and ip and ip != "0.0.0.0":
        return ip
    return ""


def _setup_multicast_socket(s, group: str, port: int, iface_ip: str,
                            platform: str = None) -> None:
    """SO_REUSEADDR (several listeners on one port, both platforms), bind as
    _bind_address says, then IP_ADD_MEMBERSHIP for the group on iface_ip
    (0.0.0.0 lets the stack pick the adapter)."""
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((_bind_address(iface_ip, platform), int(port)))
    mreq = struct.pack("4s4s", socket.inet_aton(group),
                       socket.inet_aton(iface_ip or "0.0.0.0"))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)


def _linux_iface_addresses(net_dir: str = "/sys/class/net") -> list:
    """IPv4 address of every interface listed in /sys/class/net that has one,
    read with the SIOCGIFADDR ioctl. Needs no external command. Interfaces
    with no IPv4 address, or that are down, are skipped. Never raises."""
    out = []
    try:
        import fcntl
        names = sorted(os.listdir(net_dir))
    except (ImportError, OSError):
        return out
    for name in names:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                req = struct.pack("256s", name[:15].encode())
                res = fcntl.ioctl(s.fileno(), 0x8915, req)      # SIOCGIFADDR
                out.append(socket.inet_ntoa(res[20:24]))
            finally:
                s.close()
        except OSError:
            continue
    return out


def local_ipv4_addresses() -> list:
    """This host's IPv4 addresses, for an interface picker: the ones the name
    resolver knows for the hostname plus the address of the default route,
    then 127.0.0.1. Order kept, duplicates dropped. Never raises; on a host
    with no network it is ["127.0.0.1"]. On Windows this is how the
    operator finds the USB NCM adapter address (192.168.55.100 for the first
    Super Nano, 192.168.56.100 for the second)."""
    out = []

    def _add(ip):
        if ip and ip not in out and not ip.startswith("127."):
            out.append(ip)

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            _add(info[4][0])
    except (socket.gaierror, OSError):
        pass
    if sys.platform != "win32":
        # Linux resolves the hostname to 127.0.1.1 only, so ask the stack
        try:
            import subprocess
            r = subprocess.run(["ip", "-4", "-o", "addr", "show", "scope", "global"],
                               capture_output=True, text=True, timeout=3)
            for line in r.stdout.splitlines():
                parts = line.split()
                if "inet" in parts:
                    _add(parts[parts.index("inet") + 1].split("/")[0])
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        # the GUI container has no ip command, so also ask the kernel for each
        # interface's address (finds the wired site network, not only the
        # default route)
        for ip in _linux_iface_addresses():
            _add(ip)
    try:
        # a connected UDP socket is never sent on; it only picks the route
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("10.255.255.255", 9))
            _add(probe.getsockname()[0])
        finally:
            probe.close()
    except OSError:
        pass
    out.append("127.0.0.1")
    return out

# listener

class MulticastPosListener:
    """Joins a multicast group and keeps the latest parsed fix. start()/stop()
    are idempotent so the GUI can retune the group/port live."""

    HISTORY = 200                   # datagrams kept for the raw packet view
    SENTENCE_HISTORY = 2000         # (t_mono, name) of sentences, for the rates
    WINDOW_S = 1.5                  # how long one sentence's fields count

    def __init__(self, group: str = "239.2.1.1", port: int = 5005,
                 iface_ip: str = "0.0.0.0",
                 parser: Callable[..., Optional[AircraftPos]] = None,
                 fmt: str = "auto", window_s: float = None):
        self.group = group
        self.port = int(port)
        self.iface_ip = iface_ip
        self.fmt = str(fmt or "auto")
        self._own_parser = parser is not None
        self._parser = parser or parser_for(self.fmt)
        # CoT only: read events with this uid and no other. Blank reads every event.
        self.cot_uid = os.environ.get("DPMG_FEED_COT_UID", "").strip()
        self._cot_seen = {}         # uid: monotonic time of its last event
        self.combiner = FixCombiner(self.WINDOW_S if window_s is None else window_s)
        self._sentences = collections.deque(maxlen=self.SENTENCE_HISTORY)
        self.bad_sentences = 0      # lines starting with $ that did not parse
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._latest: Optional[AircraftPos] = None
        self._history = collections.deque(maxlen=self.HISTORY)
        self.rx = self.parsed = self.bad = 0
        self.last_error = ""
        self.started_at: Optional[float] = None
        # called with each Packet from the listener thread (a recorder)
        self.on_packet: Optional[Callable[["Packet"], None]] = None

    def set_format(self, fmt: str) -> None:
        """Pick the parser: auto, json or nmea. Takes effect on the next
        datagram; no restart needed."""
        self.fmt = str(fmt or "auto")
        self._own_parser = False
        self._parser = parser_for(self.fmt)

    @property
    def window_s(self) -> float:
        return self.combiner.window_s

    def set_window(self, window_s: float) -> None:
        """How long a sentence's fields stay part of the fix (seconds)."""
        self.combiner.set_window(window_s)

    # lifecycle

    def start(self) -> None:
        self.stop()
        self._stop.clear()
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        _setup_multicast_socket(s, self.group, self.port, self.iface_ip)
        s.settimeout(0.5)           # so the recv loop can see the stop flag
        self._sock = s
        self.started_at = time.time()
        self._thread = threading.Thread(target=self._run, name="acft-mcast",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t, self._thread = self._thread, None
        if t is not None:
            t.join(timeout=1.5)
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # state

    @property
    def latest(self) -> Optional[AircraftPos]:
        with self._lock:
            return self._latest

    @property
    def age_s(self) -> float:
        with self._lock:
            if self._latest is None:
                return float("inf")
            return time.monotonic() - self._latest.t_mono

    def recent(self, n: int = 50) -> list:
        """The last n datagrams, newest last."""
        with self._lock:
            items = list(self._history)
        return items[-int(n):] if n else items

    def clear_history(self) -> None:
        with self._lock:
            self._history.clear()
            self._sentences.clear()

    def sentence_rates(self, window_s: float = 10.0, now_mono: float = None) -> dict:
        """Per sentence name (GPGGA, GPVTG, ..., json): {"hz", "count",
        "age_s"} over the last window_s. hz is the count over the window (or
        over the time since the first sentence, when shorter); a name seen
        before but not in the window has hz 0 and its age."""
        now_mono = time.monotonic() if now_mono is None else now_mono
        with self._lock:
            items = list(self._sentences)
        out = {}
        if not items:
            return out
        span = min(float(window_s), max(0.5, now_mono - items[0][0]))
        last = {}
        for t, name in items:
            last[name] = t
            if now_mono - t <= window_s:
                out.setdefault(name, 0)
                out[name] += 1
        res = {}
        for name, t in last.items():
            n = out.get(name, 0)
            res[name] = {"hz": (n / span) if n else 0.0, "count": n,
                         "age_s": max(0.0, now_mono - t)}
        return res

    def cot_uids_seen(self, window_s: float = 10.0, now_mono: float = None) -> list:
        """The CoT uids heard on the group in the last window_s, whatever the
        uid filter is set to."""
        now_mono = time.monotonic() if now_mono is None else now_mono
        return sorted(u for u, t in dict(self._cot_seen).items() if now_mono - t <= window_s)

    def rate_hz(self, window_s: float = 10.0, now_mono: float = None) -> Optional[float]:
        """Parsed fixes per second over the last window_s, from the arrival
        times of the datagrams that parsed. None with fewer than two fixes in
        the window."""
        now_mono = time.monotonic() if now_mono is None else now_mono
        with self._lock:
            ts = [p.t_mono for p in self._history
                  if p.pos is not None and now_mono - p.t_mono <= window_s]
        if len(ts) < 2:
            return None
        span = ts[-1] - ts[0]
        if span <= 0:
            return None
        return (len(ts) - 1) / span

    def feed_offset_s(self, n: int = 20):
        """The feed's own time minus this host's clock at arrival, over the last
        n fixes that carry a time: (mean, min, max, count), or None when the feed
        carries no time. Positive means the feed is ahead of this console."""
        with self._lock:
            offs = [p.pos.t_feed_utc - p.t_wall for p in list(self._history)[-int(n):]
                    if p.pos is not None and p.pos.t_feed_utc is not None]
        if not offs:
            return None
        return (sum(offs) / len(offs), min(offs), max(offs), len(offs))

    def feed(self, data: bytes, addr=("?", 0), t_wall: float = None,
             t_mono: float = None) -> Optional[AircraftPos]:
        """Take one datagram as if it had arrived on the socket (the replay
        path and the tests use this). Returns the parsed fix or None."""
        t_wall = time.time() if t_wall is None else t_wall
        t_mono = time.monotonic() if t_mono is None else t_mono
        self.rx += 1
        if self._own_parser:
            pos = self._parser(data)
        else:
            if self._parser in (parse_auto, parse_cot_bytes):
                pos = self._parser(data, self.combiner, t_mono, self.cot_uid)
                if data.lstrip()[:1] == b"<":
                    for uid in cot_uids(data):
                        self._cot_seen[uid] = t_mono
            else:
                pos = self._parser(data, self.combiner, t_mono)
        names, bad_lines = sentences_in(data) if self.fmt != "json" else ([], 0)
        if not names and data[:1] == b"{" and pos is not None:
            names = ["json"]
        with self._lock:
            for n in names:
                self._sentences.append((t_mono, n))
        self.bad_sentences += bad_lines
        if pos is not None:
            pos.t_wall = t_wall
            pos.t_mono = t_mono
            self.parsed += 1
        else:
            self.bad += 1
        pkt = Packet(t_wall, t_mono, f"{addr[0]}:{addr[1]}", bytes(data), pos)
        with self._lock:
            self._history.append(pkt)
            if pos is not None:
                self._latest = pos
        cb = self.on_packet
        if cb is not None:
            try:
                cb(pkt)
            except Exception:
                pass
        return pos

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError as e:
                self.last_error = str(e)
                break
            self.feed(data, addr)


# test sender: emit synthetic fixes so the whole path can be exercised
# without the real aircraft feed. `python3 -m dpmg.link.acft_pos_multicast send [nmea|json]`

def _nmea_latlon(lat: float, lon: float) -> str:
    la, lo = abs(lat), abs(lon)
    lats = f"{int(la)*100 + (la - int(la))*60:09.4f}"
    lons = f"{int(lo)*100 + (lo - int(lo))*60:010.4f}"
    return f"{lats},{'N' if lat >= 0 else 'S'},{lons},{'E' if lon >= 0 else 'W'}"


def _nmea_hms(t_utc: float = None) -> str:
    t = time.time() if t_utc is None else float(t_utc)
    tm = time.gmtime(t)
    return f"{tm.tm_hour:02d}{tm.tm_min:02d}{tm.tm_sec:02d}.{int((t % 1) * 100):02d}"


def _sentence(body: str) -> str:
    return f"${body}*{nmea_checksum(body)}"


def _nmea_gga(lat: float, lon: float, alt: float, t_utc: float = None,
              sats: int = 8, hdop: float = 0.9, quality: int = 1) -> str:
    """A GPGGA sentence with a checksum. The time field is the UTC given (epoch
    seconds), else now."""
    return _sentence(f"GPGGA,{_nmea_hms(t_utc)},{_nmea_latlon(lat, lon)},{quality},"
                     f"{sats:02d},{hdop:.1f},{alt:.1f},M,0.0,M,,")


def _nmea_rmc(lat: float, lon: float, t_utc: float = None, speed_mps: float = 0.0,
              heading_deg: float = 0.0) -> str:
    """A GPRMC sentence: valid fix, speed in knots, course true, the date."""
    t = time.time() if t_utc is None else float(t_utc)
    tm = time.gmtime(t)
    return _sentence(f"GPRMC,{_nmea_hms(t)},A,{_nmea_latlon(lat, lon)},"
                     f"{speed_mps / KT_MPS:05.1f},{heading_deg % 360:05.1f},"
                     f"{tm.tm_mday:02d}{tm.tm_mon:02d}{tm.tm_year % 100:02d},,,A")


def _nmea_gll(lat: float, lon: float, t_utc: float = None) -> str:
    return _sentence(f"GPGLL,{_nmea_latlon(lat, lon)},{_nmea_hms(t_utc)},A,A")


def _nmea_vtg(heading_deg: float, speed_mps: float, mag_var_deg: float = 0.0) -> str:
    """A GPVTG sentence: course true and magnetic, speed in knots and km/h."""
    return _sentence(f"GPVTG,{heading_deg % 360:05.1f},T,{(heading_deg - mag_var_deg) % 360:05.1f},M,"
                     f"{speed_mps / KT_MPS:05.1f},N,{speed_mps * 3.6:05.1f},K,A")


def _nmea_gsa(fix_mode: int = 3, sats: int = 8, pdop: float = 1.6, hdop: float = 0.9,
              vdop: float = 1.3) -> str:
    """A GPGSA sentence: automatic mode, the fix mode (1 none, 2 2D, 3 3D),
    sats satellite ids, then PDOP, HDOP, VDOP."""
    ids = [f"{i + 2:02d}" for i in range(min(12, max(0, int(sats))))]
    ids += [""] * (12 - len(ids))
    return _sentence(f"GPGSA,A,{int(fix_mode)},{','.join(ids)},{pdop:.1f},{hdop:.1f},{vdop:.1f}")


def _nmea_gsv(sats_in_view: int = 11) -> str:
    """The first GPGSV message of a set, carrying the count in view; the
    per satellite fields are left blank, the count is all that is read."""
    total = max(1, (int(sats_in_view) + 3) // 4)
    return _sentence(f"GPGSV,{total},1,{int(sats_in_view):02d},,,,,,,,,,,,,,,,")


BURST_KINDS = ("GGA", "RMC", "VTG", "GSA", "GSV")


def nmea_burst(lat: float, lon: float, alt: float, t_utc: float = None,
               heading_deg: float = None, speed_mps: float = 0.0, drop=(),
               aux: bool = True, sats: int = 8, sats_in_view: int = 11,
               fix_mode: int = 3) -> list:
    """The sentences a receiver sends each second, in the usual order GGA,
    RMC, VTG, GSA, GSV, as a list of strings. drop names kinds to leave out
    ("VTG", or "RMC,VTG"); aux False leaves out GSA and GSV (for a feed that
    sends them less often). heading None gives RMC and VTG with no course."""
    if isinstance(drop, str):
        drop = [d.strip().upper() for d in drop.split(",") if d.strip()]
    drop = {str(d).upper() for d in drop}
    hdg = 0.0 if heading_deg is None else float(heading_deg)
    out = []
    if "GGA" not in drop:
        out.append(_nmea_gga(lat, lon, alt, t_utc, sats=sats))
    if "RMC" not in drop:
        rmc = _nmea_rmc(lat, lon, t_utc, speed_mps, hdg)
        if heading_deg is None:
            # no course: blank the field and recompute the checksum
            f = rmc[1:rmc.rfind("*")].split(",")
            f[8] = ""
            rmc = _sentence(",".join(f))
        out.append(rmc)
    if "VTG" not in drop:
        vtg = _nmea_vtg(hdg, speed_mps)
        if heading_deg is None:
            f = vtg[1:vtg.rfind("*")].split(",")
            f[1] = f[3] = ""
            vtg = _sentence(",".join(f))
        out.append(vtg)
    if aux:
        if "GSA" not in drop:
            out.append(_nmea_gsa(fix_mode, sats))
        if "GSV" not in drop:
            out.append(_nmea_gsv(sats_in_view))
    return out


def _test_send(fmt: str, group="239.2.1.1", port=5005, iface=None) -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    if iface:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(iface))
    lat, lon, alt = 39.7392, -104.9903, 1609.0
    n = 0
    while True:
        n += 1
        lat += 0.0001
        if fmt == "json":
            msg = json.dumps({"lat": lat, "lon": lon, "alt": alt + n, "time": time.time()})
        else:
            msg = _nmea_gga(lat, lon, alt + n)
        s.sendto(msg.encode(), (group, port))
        print(f"sent #{n}: {msg}")
        time.sleep(1.0)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "send":
        # group, port and interface follow the saved feed settings when there
        # are any, so the test sender lands where the GUI listens
        try:
            from dpmg.link import feed_settings as _fs
            _st = _fs.load()
        except Exception:
            _st = {}
        _test_send(sys.argv[2] if len(sys.argv) > 2 else "nmea",
                   group=_st.get("group", "239.2.1.1"), port=int(_st.get("port", 5005)),
                   iface=sys.argv[3] if len(sys.argv) > 3 else None)
    else:
        print("usage: python3 -m dpmg.link.acft_pos_multicast send [nmea|json] [iface]")
