"""The planner's map: the Python side of static/mission_map.js.

Serves the map store and the renderer assets from the planner itself (so the
map is fully offline), builds the head HTML that loads the vendored
libraries, and turns the plan into what the map draws: the flight path (review
only), one marker per step location, the geofences, the overlay layers
imported from dropped files, and the platform position.

Each radio in the plan has a colour, taken from its place in the plan's
radios list (RADIO_COLORS), so no colour is saved in the plan file. The
same colour marks the radio's row, its timeline lane, its areas and the
labels on the map, and every map label names the radio. The map can be
limited to a set of radios (the selected one, a chosen few, or all); the
flight path and the overlays belong to the plan, not to a radio, so they
always show. What the map shows has no effect on the location triggers,
which watch every radio's areas.
MapPanel is the NiceGUI element plus the calls into the JS; events from the
map (clicks, drawn shapes, the view) come back through NiceGUI's
emitEvent('mission_map', ...) to the on_event callback.

Nothing here touches a radio.
"""

from __future__ import annotations

import json
import os
import uuid

from nicegui import app, ui

from . import map_store
from . import mission_file as mf
HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")
MAPS_URL = "/maps"                  # the store: .pmtiles need range requests (media route)
ASSETS_URL = MAPS_URL + "/assets"   # the renderer pieces (static route)
STATIC_URL = "/mission_static"      # mission_map.js

START_COLOR = "#16a34a"       # kept for callers that still want a role colour
STOP_COLOR = "#dc2626"
PATH_COLOR = "#374151"        # the flight path: neutral grey, it is the plan's not a radio's
OVERLAY_COLORS = ("#0d9488", "#7c3aed", "#b45309", "#be185d", "#4d7c0f", "#1d4ed8")
# One colour per radio, by its place in the plan's radios list. These are
# the Okabe and Ito colours, which stay apart for the common kinds of colour
# blindness; the order puts the four most different first (blue, orange,
# green, purple) and they all read against the light base map. The yellow
# is darkened so its labels stay legible on white.
RADIO_COLORS = ("#0072B2", "#E69F00", "#009E73", "#CC79A7",
                "#D55E00", "#56B4E9", "#8B7500", "#000000")
UNPLANNED_COLOR = "#6b7280"   # a radio with steps but no entry in the radios list
SHOW_SELECTED = "selected"    # the map follows the selected radio
SHOW_ALL = "all"              # every radio on the map

_mounted = {"done": False}


def mount():
    """Add the routes once per process. The assets mount goes first so it
    wins over the media route that covers the rest of the store."""
    if _mounted["done"]:
        return
    store = map_store.ensure_store()
    app.add_static_files(ASSETS_URL, os.path.join(store, map_store.ASSETS_DIR))
    app.add_media_files(MAPS_URL, store)
    app.add_static_files(STATIC_URL, STATIC_DIR)
    _mounted["done"] = True


def assets_missing() -> list:
    """Renderer pieces not in the store (vendor_map_assets.py fetches them)."""
    d = os.path.join(map_store.store_dir(), map_store.ASSETS_DIR)
    need = ("maplibre-gl.js", "maplibre-gl.css", "pmtiles.js", "basemaps.js",
            "fonts", "sprites")
    return [n for n in need if not os.path.exists(os.path.join(d, n))]


def head_html() -> str:
    return (f'<link rel="stylesheet" href="{ASSETS_URL}/maplibre-gl.css">\n'
            f'<script src="{ASSETS_URL}/maplibre-gl.js"></script>\n'
            f'<script src="{ASSETS_URL}/pmtiles.js"></script>\n'
            f'<script type="module">import * as b from "{ASSETS_URL}/basemaps.js"; '
            f'window.protomapsBasemaps = b;</script>\n'
            f'<script src="{STATIC_URL}/mission_map.js?v={_script_version()}"></script>\n')


def _script_version() -> str:
    """The map script's change time, so a browser never keeps an old copy
    after the file is updated (the static route allows an hour of caching)."""
    try:
        return str(int(os.path.getmtime(os.path.join(STATIC_DIR, "mission_map.js"))))
    except OSError:
        return "0"


def map_config(center=None, zoom=None, flavor="light") -> dict:
    """What missionMap.init needs: the base and area tile files that are
    present, by URL, and where the assets are."""
    idx = map_store.load_index()
    cfg = {"assetsUrl": ASSETS_URL, "flavor": flavor, "areas": []}
    for e in map_store.entries(idx):
        if not e.get("present"):
            continue
        if e["kind"] == "base":
            cfg["baseUrl"] = f"{MAPS_URL}/{map_store.BASE_FILE}"
            cfg["baseMaxzoom"] = e.get("maxzoom", map_store.BASE_MAXZOOM)
        else:
            cfg["areas"].append({"name": e["name"],
                                 "url": f"{MAPS_URL}/{map_store.area_file(e['name'])}",
                                 "maxzoom": e.get("maxzoom", map_store.AREA_MAXZOOM),
                                 "bbox": e.get("bbox"),
                                 "tileType": e.get("tile_type", "vector")})
    if center is not None:
        cfg["center"] = [float(center[1]), float(center[0])]     # [lon, lat]
    if zoom is not None:
        cfg["zoom"] = float(zoom)
    return cfg


# plan: map data

def flight_path(mission: dict) -> list:
    """The review flight path as [[lon, lat], ...], or []."""
    fp = mf.map_block(mission).get("flight_path")
    if not isinstance(fp, list):
        return []
    out = []
    for p in fp:
        if isinstance(p, (list, tuple)) and len(p) == 2 and mf._latlon_ok(p[1], p[0]):
            out.append([float(p[0]), float(p[1])])
    return out


def set_flight_path(mission: dict, coords, source="drawn") -> int:
    """Store a path ([lon, lat] pairs); returns the number of points kept."""
    pts = [[float(p[0]), float(p[1])] for p in (coords or [])
           if isinstance(p, (list, tuple)) and len(p) == 2 and mf._latlon_ok(p[1], p[0])]
    m = mission.setdefault("map", {})
    if pts:
        m["flight_path"] = pts
        m["flight_path_source"] = source
    else:
        m.pop("flight_path", None)
        m.pop("flight_path_source", None)
    return len(pts)


def path_from_geojson(data) -> list:
    """[lon, lat] pairs from a GeoJSON LineString, MultiLineString (joined),
    a Feature or FeatureCollection holding one, or a bare coordinate list.
    Raises ValueError when nothing usable is in it."""
    if isinstance(data, (bytes, str)):
        data = json.loads(data)
    pts = []

    def _geom(g):
        if not isinstance(g, dict):
            return
        t = g.get("type")
        if t == "LineString":
            pts.extend(g.get("coordinates") or [])
        elif t == "MultiLineString":
            for part in g.get("coordinates") or []:
                pts.extend(part)
        elif t == "Point":
            pts.append(g.get("coordinates"))
        elif t == "Feature":
            _geom(g.get("geometry"))
        elif t == "FeatureCollection":
            for f in g.get("features") or []:
                _geom(f)
        elif t == "GeometryCollection":
            for gg in g.get("geometries") or []:
                _geom(gg)

    if isinstance(data, list):
        pts = data
    else:
        _geom(data)
    out = [[float(p[0]), float(p[1])] for p in pts
           if isinstance(p, (list, tuple)) and len(p) >= 2 and mf._latlon_ok(p[1], p[0])]
    if len(out) < 2:
        raise ValueError("no path with two or more points in the file")
    return out


def _strip(tag: str) -> str:
    return tag.split("}")[-1].lower()


def path_from_gpx(text) -> list:
    """[lon, lat] pairs from a GPX file: track points, then route points, then
    waypoints (the first kind that has two or more points wins)."""
    import xml.etree.ElementTree as ET
    root = ET.fromstring(text)
    found = {"trkpt": [], "rtept": [], "wpt": []}
    for el in root.iter():
        t = _strip(el.tag)
        if t in found:
            try:
                lat, lon = float(el.get("lat")), float(el.get("lon"))
            except (TypeError, ValueError):
                continue
            if mf._latlon_ok(lat, lon):
                found[t].append([lon, lat])
    for kind in ("trkpt", "rtept", "wpt"):
        if len(found[kind]) >= 2:
            return found[kind]
    raise ValueError("no track, route or waypoints with two or more points in the GPX")


def path_from_kml(text) -> list:
    """[lon, lat] pairs from a KML file: every LineString's coordinates in
    document order, else Placemark Points. KML coordinates are
    "lon,lat[,alt]" separated by whitespace."""
    import xml.etree.ElementTree as ET
    root = ET.fromstring(text)
    lines, points = [], []
    for el in root.iter():
        t = _strip(el.tag)
        if t not in ("linestring", "point"):
            continue
        coords = next((c for c in el.iter() if _strip(c.tag) == "coordinates"), None)
        if coords is None or not (coords.text or "").strip():
            continue
        pts = []
        for tok in coords.text.split():
            parts = tok.split(",")
            if len(parts) < 2:
                continue
            try:
                lon, lat = float(parts[0]), float(parts[1])
            except ValueError:
                continue
            if mf._latlon_ok(lat, lon):
                pts.append([lon, lat])
        (lines if t == "linestring" else points).extend(pts)
    out = lines if len(lines) >= 2 else points
    if len(out) < 2:
        raise ValueError("no LineString or Placemark points with two or more points in the KML")
    return out


def path_from_file(name: str, text) -> list:
    """A flight path from a GeoJSON (.json, .geojson), GPX (.gpx) or KML
    (.kml) file, by extension; anything else is tried as GeoJSON. Raises
    ValueError when no path with two or more points is in it."""
    ext = os.path.splitext(str(name or ""))[1].lower()
    raw = text if isinstance(text, bytes) else str(text).encode("utf-8")
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    try:
        if ext == ".gpx":
            return path_from_gpx(text)
        if ext == ".kml":
            return path_from_kml(text)
        if ext in (".json", ".geojson", ""):
            return path_from_geojson(text)
    except ValueError:
        raise
    except Exception as ex:                  # bad XML or JSON
        raise ValueError(f"could not read {ext or 'the file'}: {ex}")
    # anything else (KMZ, CSV, a zipped shapefile) through the drop reader
    from . import map_import
    try:
        return map_import.path_from_features(map_import.parse(name, raw))
    except map_import.MapImportError as ex:
        raise ValueError(str(ex))


# overlay layers
#
# An overlay is map data the operator dropped on the map and only wants to
# see: lines, polygons and points with names, kept in the plan's map block
# under "overlays" so they come back when the plan is loaded. They take no
# part in triggers or validation.

def overlays(mission: dict) -> list:
    ov = mf.map_block(mission).get("overlays")
    return [o for o in ov if isinstance(o, dict)] if isinstance(ov, list) else []


def add_overlay(mission: dict, name: str, feats: dict, source: str = "") -> dict:
    """Store a feature set (map_import's shape: lines, polygons, points) as
    an overlay layer; returns the stored record. Coordinates are rounded to
    a metre or so to keep the plan file small."""
    existing = overlays(mission)
    color = OVERLAY_COLORS[len(existing) % len(OVERLAY_COLORS)]
    rec = {"id": f"ov-{uuid.uuid4().hex[:8]}", "name": str(name or "overlay"),
           "source": str(source or ""), "color": color,
           "lines": [{"name": str(ln.get("name", "")),
                      "coords": [[round(float(p[0]), 6), round(float(p[1]), 6)] for p in ln["coords"]]}
                     for ln in feats.get("lines", []) if len(ln.get("coords", [])) >= 2],
           "polygons": [{"name": str(pg.get("name", "")),
                         "coords": [[round(float(p[0]), 6), round(float(p[1]), 6)] for p in pg["coords"]]}
                        for pg in feats.get("polygons", []) if len(pg.get("coords", [])) >= 3],
           "points": []}
    for p in feats.get("points", []):
        if not mf._latlon_ok(p.get("lat"), p.get("lon")):
            continue
        q = {"name": str(p.get("name", "")), "lat": round(float(p["lat"]), 6),
             "lon": round(float(p["lon"]), 6)}
        if p.get("radius_m"):
            q["radius_m"] = float(p["radius_m"])
        rec["points"].append(q)
    m = mission.setdefault("map", {})
    m.setdefault("overlays", [])
    m["overlays"].append(rec)
    return rec


def remove_overlay(mission: dict, overlay_id: str) -> bool:
    m = mission.setdefault("map", {})
    before = overlays(mission)
    after = [o for o in before if o.get("id") != overlay_id]
    if len(after) == len(before):
        return False
    if after:
        m["overlays"] = after
    else:
        m.pop("overlays", None)
    return True


def overlay_count(ov: dict) -> int:
    return len(ov.get("lines", [])) + len(ov.get("polygons", [])) + len(ov.get("points", []))


# radio colours and the map's radio filter

def radio_colors(mission: dict) -> dict:
    """radio id: colour, by position in the plan's radios list. A radio that
    has steps but is not in the list gets the grey."""
    out = {}
    for i, r in enumerate(mission.get("radios", []) or []):
        rid = r.get("id") if isinstance(r, dict) else None
        if rid and rid not in out:
            out[rid] = RADIO_COLORS[i % len(RADIO_COLORS)]
    for s in mission.get("steps", []) or []:
        rid = s.get("radio")
        if rid and rid not in out:
            out[rid] = UNPLANNED_COLOR
    return out


def radio_color(mission: dict, radio_id) -> str:
    return radio_colors(mission).get(radio_id, UNPLANNED_COLOR)


def shown_radios(view: dict, all_radios) -> set | None:
    """Which radios the map draws, from the shared view state: None means
    every radio. view["show"] is SHOW_SELECTED (follow view["radio"]; with no
    radio selected everything shows), SHOW_ALL, or a list of radio ids."""
    show = (view or {}).get("show", SHOW_SELECTED)
    if show == SHOW_ALL:
        return None
    if isinstance(show, (list, tuple, set, frozenset)):
        return set(show)
    rid = (view or {}).get("radio")
    return {rid} if rid else None


def show_text(mission: dict, view: dict, all_radios) -> str:
    """The one-line statement over the map of what it shows. A radio with no
    steps says so in words. The last sentence is always the trigger rule."""
    shown = shown_radios(view, all_radios)
    radios = list(all_radios or [])
    sel = (view or {}).get("radio")
    fences = step_fences(mission)

    def _count(rid):
        n_steps = sum(1 for s in mission.get("steps", []) if s.get("radio") == rid)
        n_areas = sum(1 for f in fences if f.get("radio") == rid)
        if not n_steps:
            return f"{rid}: no steps in this plan"
        return f"{rid}: {n_steps} step(s), {n_areas} area(s)"

    if shown is None:
        if sel and (view or {}).get("show", SHOW_SELECTED) == SHOW_SELECTED:
            head = "map shows every radio"
        elif sel:
            head = f"map shows every radio ({sel} selected)"
        else:
            head = "map shows every radio (no radio selected)"
        parts = [_count(r) for r in radios]
        body = "; ".join(parts) if parts else "no radios in the plan"
    else:
        names = [r for r in radios if r in shown] + sorted(shown - set(radios))
        if not names:
            body = "no radio ticked: tick a radio, or choose all"
        else:
            body = "; ".join(_count(r) for r in names)
        head = "map shows " + ("the selected radio only"
                               if (view or {}).get("show", SHOW_SELECTED) == SHOW_SELECTED
                               else ", ".join(names) if names else "nothing")
    return f"{head}. {body}. The flight path and overlays always show. " \
           "Triggers watch every radio's areas whatever the map shows."


def step_fences(mission: dict, radios=None, selected=None) -> list:
    """Every geofence in the plan, for the map, with its role, its radio and
    the radio's colour. radios limits it to those radio ids (None = all);
    selected marks the selected radio's fences so the map can draw them
    stronger when several radios show."""
    out = []
    colors = radio_colors(mission)
    for s in mission.get("steps", []):
        rid = s.get("radio")
        if radios is not None and rid not in radios:
            continue
        loc = mf.step_location(s)
        for role in ("start", "stop"):
            gf = loc.get(role)
            k = mf.geofence_kind(gf)
            if not k:
                continue
            ok, _why = mf.validate_geofence(gf)
            if not ok:
                continue
            # the radio leads every label: with two SDRs in a plan the map
            # has to say which one each area belongs to
            if mf.is_location_task(s):
                mode = mf.trigger_block(s).get("mode", "while_inside")
                label = (f"{rid}: {s.get('technique')} "
                         f"({'while inside' if mode == 'while_inside' else 'dwell'}, "
                         f"p{mf.step_priority(s)})")
            else:
                label = f"{rid}: {s.get('technique')} {role}"
            if gf.get("name"):
                label = f"{gf['name']}: {label}"
            row = {"id": f"{s.get('id')}:{role}", "step": s.get("id"), "role": role,
                   "kind": k, "label": label, "radio": rid,
                   "color": colors.get(rid, UNPLANNED_COLOR),
                   "selected": bool(selected) and rid == selected}
            if k == "point":
                row.update(lat=float(gf["lat"]), lon=float(gf["lon"]),
                           radius_m=float(gf["radius_m"]))
            else:
                row["polygon"] = [[float(p[0]), float(p[1])] for p in gf["polygon"]]
            out.append(row)
    return out


def step_markers(mission: dict, radios=None, selected=None) -> list:
    """One marker per geofence, at its centre, labelled with the radio and
    the step, in the radio's colour; a start marker is a filled dot, a stop
    marker a hollow one."""
    out = []
    for f in step_fences(mission, radios=radios, selected=selected):
        gf = ({"lat": f["lat"], "lon": f["lon"], "radius_m": f["radius_m"]}
              if f["kind"] == "point" else {"polygon": f["polygon"]})
        c = mf.geofence_center(gf)
        if c is None:
            continue
        out.append({"id": f["id"], "step": f["step"], "lat": c[0], "lon": c[1],
                    "label": f["label"], "color": f["color"], "radio": f["radio"],
                    "role": f["role"], "fill": f["role"] == "start",
                    "selected": f.get("selected", False)})
    return out


# one colour per aircraft marker, in list order (the first is purple); a
# stale marker is grey whatever its aircraft
AIRCRAFT_COLORS = ("#7c3aed", "#0891b2", "#db2777", "#ca8a04", "#059669", "#ea580c")


def position_marker(fix, valid=True, heading_deg=None, name=None, index=0) -> dict | None:
    """The platform marker: a dot, or an arrow turned to heading_deg (degrees
    true) when a heading is known; the label carries it as hdg 087. With a
    name (an aircraft of several) the label is the name and the marker has
    that aircraft's colour."""
    if fix is None:
        return None
    who = str(name) if name else "platform"
    m = {"lat": float(fix.lat), "lon": float(fix.lon),
         "label": who if valid else f"{who} (stale)", "valid": bool(valid),
         "color": AIRCRAFT_COLORS[int(index or 0) % len(AIRCRAFT_COLORS)]}
    if name:
        m["name"] = str(name)
    if heading_deg is not None:
        try:
            h = float(heading_deg) % 360.0
        except (TypeError, ValueError):
            return m
        m["heading"] = h
        m["label"] += f" hdg {int(round(h)) % 360:03d}"
    return m


def position_markers(items) -> list:
    """One marker per aircraft from [(name, fix, valid, heading_deg)], in
    list order; aircraft with no fix are left out."""
    out = []
    for i, (name, fix, valid, hdg) in enumerate(items or []):
        m = position_marker(fix, valid=valid, heading_deg=hdg, name=name, index=i)
        if m is not None:
            out.append(m)
    return out


# the element

class MapPanel:
    """The map div plus the calls into missionMap. on_event(dict) gets every
    event the JS emits (type: ready, view, click, marker_click, drawn,
    draw_cancel, drop, error)."""

    def __init__(self, on_event=None, height_px: int = 420, center=None, zoom=None):
        mount()
        self.on_event = on_event
        self.ready = False
        self.view = None                      # last (lat, lon, zoom) from the map
        self.el = ui.element("div").classes("w-full rounded border") \
            .style(f"height:{int(height_px)}px;min-height:200px")
        self.dom_id = f"mm-{self.el.id}"
        self.el.props(f'id="{self.dom_id}"')
        self.cfg = map_config(center=center, zoom=zoom)
        ui.on("mission_map", self._on_event)
        # build the map a moment after the page has its socket. The timer is
        # made in the connect handler, not here: a timer made at build time
        # wakes on a deleted page when the page never connects (see
        # _start_timers in mission_tab). The handler runs again on a
        # reconnect of the same page, where the map already exists, hence
        # the flag.
        self._inited = False
        ui.context.client.on_connect(self._arm_init)

    def _arm_init(self):
        if self._inited:
            return
        self._inited = True
        with self.el:
            ui.timer(0.1, self._init, once=True)

    def _init(self):
        self._js(f"window.missionMap.init({json.dumps(self.dom_id)}, {json.dumps(self.cfg)})")

    def _on_event(self, e):
        d = e.args if isinstance(e.args, dict) else {}
        t = d.get("type")
        if t == "ready":
            self.ready = True
        elif t == "view":
            self.view = (d.get("lat"), d.get("lon"), d.get("zoom"))
        if self.on_event is not None:
            self.on_event(d)

    def _js(self, code: str):
        # run in this element's page: a timer or background task has no page
        # context of its own, and run_javascript needs one
        try:
            with self.el:
                ui.run_javascript(code)
        except Exception:
            pass                              # page gone

    # pushes

    def set_path(self, coords):
        self._js(f"window.missionMap.setPath({json.dumps(coords or [])})")

    def set_markers(self, markers):
        self._js(f"window.missionMap.setMarkers({json.dumps(markers or [])})")

    def set_geofences(self, fences):
        self._js(f"window.missionMap.setGeofences({json.dumps(fences or [])})")

    def set_position(self, marker):
        self._js(f"window.missionMap.setPosition({json.dumps(marker)})")

    def set_positions(self, markers):
        """One marker per aircraft (position_markers)."""
        self._js(f"window.missionMap.setPositions({json.dumps(markers or [])})")

    def set_overlays(self, layers):
        self._js(f"window.missionMap.setOverlays({json.dumps(layers or [])})")

    def set_areas(self):
        self.cfg = map_config()
        self._js(f"window.missionMap.setAreas({json.dumps(self.cfg.get('areas', []))})")

    def enable_drop(self, uploader, max_files: int = 10):
        """Files dropped anywhere on the map go to this ui.upload element
        (hidden, multiple, auto_upload), which posts them to the server."""
        self._js(f"window.missionMap.enableDrop({int(uploader.id)}, {int(max_files)})")

    def push_plan(self, mission: dict, radios=None, selected=None):
        """Draw the plan: the path and the overlays whole, the markers and
        areas of the given radios only (None = every radio)."""
        self.set_path(flight_path(mission))
        self.set_markers(step_markers(mission, radios=radios, selected=selected))
        self.set_geofences(step_fences(mission, radios=radios, selected=selected))
        self.set_overlays(overlays(mission))

    def fit_bbox(self, bbox):
        w, s, e, n = (float(x) for x in bbox)
        self._js(f"window.missionMap.fitBbox({w}, {s}, {e}, {n})")

    def fit(self, what="all"):
        self._js(f"window.missionMap.fit({json.dumps(what)})")

    def fly_to(self, lat, lon, zoom=None):
        self._js(f"window.missionMap.flyTo({float(lat)}, {float(lon)}, "
                 f"{'null' if zoom is None else float(zoom)})")

    def set_draw_mode(self, mode, target=""):
        self._js(f"window.missionMap.setDrawMode({json.dumps(mode)}, {json.dumps(target)})")
