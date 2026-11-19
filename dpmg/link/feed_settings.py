"""Saved settings for the aircraft position feeds.

One file, ~/.dpmg/aircraft_feed.json (DPMG_FEED_SETTINGS names another), read
by every place that listens to a position feed: the operator console's
aircraft panel, the planner's feed row and location triggers, the engineering
Aircraft feed tab and the test senders. A page changes only the fields its
user changed (update_aircraft, through AircraftFeeds.update), written on the
settings as saved now, so a page built before another one saved cannot put
the old values back. The shared listeners take the values at once and every
page's fields follow.

Keys:
  aircraft   a list, one entry per position feed: name, group, port, iface,
             format ("auto", "json", "nmea" or "xml") and window_s (the combine
             window in seconds). The order is the order the panels show them;
             the first one is the default a radio follows when its plan does
             not bind it to another.
  sender     the test sender's own defaults (rate_hz, speed_mps, alt_m, loop,
             iface, format).
  group, port, iface, format
             the first aircraft's values, kept at the top level as well so an
             older reader still sees the one feed it always did.

A file from before the list (group, port, iface and format at the top level
only) loads as one aircraft named after those values, "239.2.1.1:5005" say,
so a station with one aircraft sees no change. Two aircraft may share a group
and port (one socket each, both receive every datagram) and differ only in
format; shared_sockets() names such pairs so a panel can say so. Nothing here
opens a socket.
"""

from __future__ import annotations

import json
import os

FORMATS = ("auto", "json", "nmea", "xml")
WINDOW_DEFAULT_S = 1.5
NAME_MAX = 32

DEFAULTS = {
    "group": "239.2.1.1",
    "port": 5005,
    "iface": "0.0.0.0",
    "format": "auto",
    "sender": {"rate_hz": 1.0, "speed_mps": 60.0, "alt_m": 1500.0, "loop": False,
               "iface": "127.0.0.1", "format": "json"},
}
ENTRY_KEYS = ("name", "group", "port", "iface", "format", "window_s")


def settings_path() -> str:
    return (os.environ.get("DPMG_FEED_SETTINGS")
            or os.path.join(os.path.expanduser("~"), ".dpmg", "aircraft_feed.json"))


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _port(v) -> int:
    try:
        port = int(v or DEFAULTS["port"])
    except (TypeError, ValueError):
        port = DEFAULTS["port"]
    return port if 0 < port < 65536 else DEFAULTS["port"]


def _fmt(v, allowed=FORMATS, default="auto") -> str:
    fmt = str(v or default).strip().lower()
    if fmt == "cot":                # Cursor on Target, listed as xml
        fmt = "xml"
    return fmt if fmt in allowed else default


def default_name(group, port) -> str:
    """The name an aircraft gets when none was given: its group and port."""
    return f"{group}:{port}"


def normalize_entry(e: dict, taken=()) -> dict:
    """One aircraft entry with its types and ranges checked: port an int in 1
    to 65535, format one of the three names, blank group or iface back to the
    default, the window 0.2 to 60 s, a blank name made from group and port and
    a name already taken given a number."""
    e = dict(e or {})
    group = str(e.get("group") or DEFAULTS["group"]).strip()
    port = _port(e.get("port"))
    iface = str(e.get("iface") or DEFAULTS["iface"]).strip()
    try:
        window = float(e.get("window_s", WINDOW_DEFAULT_S))
    except (TypeError, ValueError):
        window = WINDOW_DEFAULT_S
    window = min(60.0, max(0.2, window))
    name = " ".join(str(e.get("name") or "").split())[:NAME_MAX] or default_name(group, port)
    base, n = name, 2
    while name in taken:
        name, n = f"{base} {n}", n + 1
    return {"name": name, "group": group, "port": port, "iface": iface,
            "format": _fmt(e.get("format")), "window_s": window}


def normalize(st: dict) -> dict:
    """The whole settings dict with the aircraft list filled: from "aircraft"
    when the file has one, else one aircraft from the top level keys (the old
    one feed file). The top level keys then mirror the first aircraft."""
    st = dict(st or {})
    raw = st.get("aircraft")
    entries = []
    if isinstance(raw, list) and raw:
        for e in raw:
            if isinstance(e, dict):
                entries.append(normalize_entry(e, [x["name"] for x in entries]))
    if not entries:
        entries = [normalize_entry({k: st.get(k, DEFAULTS[k]) for k in ("group", "port", "iface", "format")})]
    # with a list, the list is the truth: the top level keys are only its
    # mirror for older readers, so an edit goes on aircraft[0], not up here
    first = entries[0]
    out = _merge(DEFAULTS, {k: v for k, v in st.items() if k not in ("aircraft",)})
    out.update(group=first["group"], port=first["port"], iface=first["iface"], format=first["format"])
    out["aircraft"] = entries
    snd = out.get("sender") or {}
    for key, cast in (("rate_hz", float), ("speed_mps", float), ("alt_m", float)):
        try:
            snd[key] = cast(snd.get(key, DEFAULTS["sender"][key]))
        except (TypeError, ValueError):
            snd[key] = DEFAULTS["sender"][key]
    snd["loop"] = bool(snd.get("loop", False))
    snd["iface"] = str(snd.get("iface") or DEFAULTS["sender"]["iface"]).strip()
    snd["format"] = _fmt(snd.get("format"), ("json", "nmea"), "json")
    out["sender"] = snd
    return out


def load(path: str = None) -> dict:
    """The saved settings with the defaults filled in. A missing or unreadable
    file gives the defaults; it never raises."""
    path = path or settings_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            st = json.load(f)
        if not isinstance(st, dict):
            st = {}
    except (OSError, ValueError):
        st = {}
    return normalize(st)


def save(st: dict, path: str = None) -> str:
    """Write the settings (normalized) atomically and return the path."""
    path = path or settings_path()
    st = normalize(st)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)
    return path


# the aircraft list

def aircraft_list(st: dict = None) -> list:
    return list(normalize(st if st is not None else load())["aircraft"])


def aircraft_names(st: dict = None) -> list:
    return [e["name"] for e in aircraft_list(st)]


def find_aircraft(st: dict, name) -> dict | None:
    for e in normalize(st)["aircraft"]:
        if e["name"] == name:
            return e
    return None


def set_aircraft(st: dict, entry: dict, old_name=None) -> dict:
    """Add or change one aircraft (old_name names the one to replace, for a
    rename) and return the settings. The list order is kept."""
    st = normalize(st)
    entries = st["aircraft"]
    key = old_name if old_name is not None else (entry or {}).get("name")
    idx = next((i for i, e in enumerate(entries) if e["name"] == key), None)
    taken = [e["name"] for i, e in enumerate(entries) if i != idx]
    new = normalize_entry(entry, taken)
    if idx is None:
        entries.append(new)
    else:
        entries[idx] = new
    return normalize(st)


# one aircraft's fields as a panel holds them: a panel writes only the ones its
# user changed, on top of the settings as saved now, so a panel built before
# another one saved cannot put the old values back

EDIT_KEYS = ("group", "port", "iface", "format", "window_s")


def field_value(key, value):
    """A panel field's value in the form the settings keep it (port an int,
    the format a known name, text stripped), for comparing two of them."""
    if key == "port":
        return _port(value)
    if key == "format":
        return _fmt(value)
    if key == "window_s":
        try:
            return min(60.0, max(0.2, float(value)))
        except (TypeError, ValueError):
            return WINDOW_DEFAULT_S
    return str(value if value is not None else "").strip()


def entry_edits(shown: dict, values: dict) -> dict:
    """The fields a panel's user changed: each key of values whose value is
    not the one the panel last showed (shown, filled from the settings)."""
    out = {}
    for k in EDIT_KEYS:
        if k in values and field_value(k, values[k]) != field_value(k, (shown or {}).get(k)):
            out[k] = field_value(k, values[k])
    return out


def update_aircraft(name, changes: dict, path: str = None, write: bool = True):
    """Change only the given fields of one aircraft, on the settings as they
    are on disk now, and save when anything changed (write False leaves the
    saving to the caller). Returns (settings, changed). An unknown name
    changes nothing."""
    st = load(path)
    e = find_aircraft(st, name)
    if e is None:
        return st, False
    new = dict(e)
    new.update({k: v for k, v in (changes or {}).items() if k in EDIT_KEYS})
    new = normalize_entry(new, [x["name"] for x in st["aircraft"] if x["name"] != name])
    if new == e:
        return st, False
    st = set_aircraft(st, new, old_name=name)
    if write:
        save(st, path)
    return st, True


def remove_aircraft(st: dict, name) -> dict:
    """Take one aircraft off the list; the last one stays (there is always a
    feed to listen to)."""
    st = normalize(st)
    if len(st["aircraft"]) > 1:
        st["aircraft"] = [e for e in st["aircraft"] if e["name"] != name]
    return normalize(st)


def shared_sockets(st: dict = None) -> dict:
    """{(group, port): [names]} for every group and port more than one
    aircraft listens on. Allowed (each has its own socket and both receive
    every datagram), but worth saying on the panel."""
    by = {}
    for e in aircraft_list(st):
        by.setdefault((e["group"], e["port"]), []).append(e["name"])
    return {k: v for k, v in by.items() if len(v) > 1}


def shares_with(st: dict, name) -> list:
    """The other aircraft on this one's group and port."""
    e = find_aircraft(st, name)
    if e is None:
        return []
    return [n for n in shared_sockets(st).get((e["group"], e["port"]), []) if n != name]


def apply_entry(listener, entry: dict) -> None:
    """Put one aircraft's group, port, interface, format and window on a
    listener (a MulticastPosListener). Does not start it."""
    e = normalize_entry(entry)
    listener.group = e["group"]
    listener.port = int(e["port"])
    listener.iface_ip = e["iface"]
    if hasattr(listener, "set_format"):
        listener.set_format(e["format"])
    if hasattr(listener, "set_window"):
        listener.set_window(e["window_s"])


def apply(listener, st: dict = None) -> None:
    """Put the first aircraft's settings (or the one entry given) on a
    listener. Does not start it."""
    st = st if st is not None else load()
    if isinstance(st, dict) and "aircraft" not in st and "name" in st:
        apply_entry(listener, st)
        return
    apply_entry(listener, normalize(st)["aircraft"][0])


def describe_entry(e: dict) -> str:
    e = normalize_entry(e)
    return f"{e['group']}:{e['port']} via {e['iface']} ({e['format']})"


def describe(st: dict = None) -> str:
    st = normalize(st if st is not None else load())
    acs = st["aircraft"]
    if len(acs) == 1:
        return describe_entry(acs[0])
    return "; ".join(f"{e['name']} {describe_entry(e)}" for e in acs)
