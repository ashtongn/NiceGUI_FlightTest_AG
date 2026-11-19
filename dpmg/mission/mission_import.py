"""Importers for the CFE files the planner can read.

CFE keeps no technique timeline, so these turn its two timeline-like
artifacts into plan steps on a radio you choose:

- autoSchedule.csv: rows of `Freq,Gain,Time` (MHz, the CFE receiver gain
  value, dwell seconds; for example `914.0,25,10`). Each row becomes an RX
  dwell step (technique rx_dwell: set_center, optionally set_gain, no TX),
  one after the other from the chosen start. The CFE gain value is only
  carried into the step when it is a valid hardware gain index for the radio
  type (G40 187 to 255, X40 195 to 255); otherwise the gain is left as set and
  the note says so.
- Role or service XML: every <service> with a <method> and a <config> becomes
  a step seeded from the method (tone, playback or streamiq, vita), one after
  the other at the catalog's default length; a samplerate in the config
  becomes the radio's start rate when it is valid for the radio. The parser
  matches tags leniently (any namespace, any case, <method> as a tag or an
  attribute), since the CFE XML layout varies; an unknown method is skipped
  with a note.

Both return (steps, notes): notes are plain strings for the audit log.
"""

from __future__ import annotations

import csv
import io
import uuid
import xml.etree.ElementTree as ET

from . import catalog
from . import mission_file as mf


# CFE service methods: planner techniques
METHOD_MAP = {"tone": "tools.tone", "playback": "iq_playback",
              "streamiq": "iq_playback", "vita": "record"}


def _new_id() -> str:
    return "s" + uuid.uuid4().hex[:8]


def _gain_range(radio_type):
    lo = catalog.RX_GAIN_MIN_X40 if (radio_type or "").lower() == "x40" else catalog.RX_GAIN_MIN_G40
    return lo, catalog.RX_GAIN_MAX


# autoSchedule.csv

def parse_autoschedule(text) -> list:
    """Rows as {"freq_mhz", "gain", "time_s"}. A header line, blank lines and
    comment lines (#) are skipped; a row with fewer than three numbers is a
    ValueError naming the line."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    rows = []
    for n, raw in enumerate(csv.reader(io.StringIO(text)), 1):
        cells = [c.strip() for c in raw]
        if not cells or not any(cells) or cells[0].startswith("#"):
            continue
        try:
            f, g, t = float(cells[0]), float(cells[1]), float(cells[2])
        except (ValueError, IndexError):
            low = ",".join(cells).lower()
            if "freq" in low and "time" in low:       # the header
                continue
            raise ValueError(f"line {n}: expected Freq,Gain,Time numbers, got {','.join(cells)!r}")
        if f <= 0 or t <= 0:
            raise ValueError(f"line {n}: frequency and time must be above zero")
        rows.append({"freq_mhz": f, "gain": g, "time_s": t})
    if not rows:
        raise ValueError("no Freq,Gain,Time rows in the file")
    return rows


def steps_from_autoschedule(rows, radio, radio_type=None, start_s=0.0, new_id=_new_id):
    """RX dwell steps, back to back from start_s. Returns (steps, notes)."""
    lo, hi = _gain_range(radio_type)
    steps, notes = [], []
    t = float(start_s)
    dropped = []
    for r in rows:
        params = {"center_hz": float(r["freq_mhz"]) * 1e6, "rx_gain_index": None}
        g = r.get("gain")
        if g is not None and float(g).is_integer() and lo <= int(g) <= hi:
            params["rx_gain_index"] = int(g)
        elif g is not None:
            dropped.append(f"{g:g}")
        steps.append({"id": new_id(), "radio": radio, "technique": "rx_dwell",
                      "radiates": False, "params": params,
                      "schedule": {"start_s": round(t, 3), "stop_s": round(t + float(r["time_s"]), 3)}})
        t += float(r["time_s"])
    notes.append(f"autoSchedule: {len(steps)} RX dwell step(s) on {radio}, "
                 f"{t - float(start_s):g} s in all")
    if dropped:
        notes.append(f"CFE gain value(s) {', '.join(sorted(set(dropped)))} are not "
                     f"{radio_type or 'G40'} gain indexes ({lo}-{hi}); RX gain left as set")
    return steps, notes


# role / service XML

def _local(tag) -> str:
    return str(tag).split("}")[-1].lower()


def _child_text(el, name):
    for c in el.iter():
        if c is not el and _local(c.tag) == name and (c.text or "").strip():
            return c.text.strip()
    return None


def parse_cfe_xml(text) -> list:
    """Every <service> as {"name", "method", "config": {lowercase tag or
    attribute: text}}. Lenient: any namespace or case; method as a child tag
    or an attribute; config children or attributes."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as ex:
        raise ValueError(f"not well-formed XML: {ex}")
    out = []
    for svc in root.iter():
        if _local(svc.tag) != "service":
            continue
        method = (svc.get("method") or _child_text(svc, "method") or "").strip().lower()
        cfg = {}
        cfg_el = next((c for c in svc.iter() if c is not svc and _local(c.tag) == "config"), None)
        if cfg_el is not None:
            for k, v in cfg_el.attrib.items():
                cfg[_local(k)] = v.strip()
            for c in cfg_el:
                if (c.text or "").strip():
                    cfg[_local(c.tag)] = c.text.strip()
        name = (svc.get("name") or _child_text(svc, "name") or method or "service")
        out.append({"name": name, "method": method, "config": cfg})
    if not out:
        raise ValueError("no <service> elements in the XML")
    return out


def _num(v, scale=1.0):
    try:
        return float(str(v).strip()) * scale
    except (TypeError, ValueError):
        return None


def _hz(cfg, *keys):
    """A frequency from the config: Hz as given, or MHz when the key says so
    or the value is small enough to be MHz."""
    for k in keys:
        if k in cfg:
            v = _num(cfg[k])
            if v is None:
                return None
            if k.endswith("mhz") or v < 1e5:
                v *= 1e6
            return v
    return None


def steps_from_cfe_xml(services, radio, radio_type=None, start_s=0.0, new_id=_new_id):
    """Steps seeded from the services, one after the other. Returns
    (steps, notes, start_rate_hz or None)."""
    steps, notes = [], []
    t = float(start_s)
    rate = None
    for svc in services:
        tech = METHOD_MAP.get(svc.get("method", ""))
        if tech is None:
            notes.append(f"service {svc.get('name')!r}: method {svc.get('method')!r} has no "
                         f"planner technique; skipped")
            continue
        entry = catalog.get(tech)
        cfg = svc.get("config") or {}
        params = entry.fresh_params()
        center = _hz(cfg, "center_mhz", "centermhz", "center", "centerfreq", "frequency", "freq", "tunecfmhz")
        if center:
            params["center_hz"] = center
        sr = _hz(cfg, "samplerate_mhz", "samplerate", "sample_rate", "rate")
        if sr and catalog.is_valid_rate(sr, radio_type):
            rate = rate or sr
        elif sr:
            notes.append(f"service {svc.get('name')!r}: samplerate {sr / 1e6:g} MSps is not valid "
                         f"for a {radio_type or 'G40'}; not used")
        if tech == "iq_playback" and cfg.get("file"):
            params["path"] = cfg["file"]
        step = {"id": new_id(), "radio": radio, "technique": tech,
                "radiates": bool(entry.radiates), "params": params,
                "schedule": {"start_s": round(t, 3), "stop_s": round(t + entry.default_dur, 3)},
                "notes": f"from CFE service {svc.get('name')} ({svc.get('method')})"}
        steps.append(step)
        t += entry.default_dur
    notes.insert(0, f"CFE XML: {len(steps)} step(s) on {radio} from {len(services)} service(s)")
    if rate:
        notes.append(f"start sample rate {rate / 1e6:g} MSps from the service config")
    return steps, notes, rate


def import_into(mission: dict, steps, radio, *, replace=False, start_rate=None):
    """Put imported steps into a plan: append (default) or replace the steps
    already on that radio. Returns the number of steps now on the radio."""
    if replace:
        mission["steps"] = [s for s in mission.get("steps", []) if s.get("radio") != radio]
    mission.setdefault("steps", []).extend(steps)
    if start_rate:
        for r in mission.get("radios", []):
            if r.get("id") == radio:
                r["sample_rate"] = int(start_rate)
    return sum(1 for s in mission["steps"] if s.get("radio") == radio)
