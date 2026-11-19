"""Map data dropped onto the planner's map.

Two jobs. First, say what a dropped file is (sniff): by its extension, then
by its content when the extension is missing or wrong. Second, read it into
one neutral feature set (parse): lines, polygons and points, each with a
name, as [lon, lat] pairs, so the tab can turn them into a flight path, task
areas, task points or an overlay without caring where they came from.

Formats: GeoJSON, KML, KMZ (a zip with a doc.kml), GPX, CSV with latitude and
longitude columns (the header names are guessed), a zipped shapefile (pyshp,
pure Python, vendored in deploy/wheels), and the two tile containers,
PMTiles and MBTiles, which are not parsed here beyond their header and
metadata: the tab files them in the map store.

Everything here works offline and touches no radio.
"""

from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import sqlite3
import struct
import xml.etree.ElementTree as ET
import zipfile

from . import mission_file as mf

# the largest data file (GeoJSON, KML, ...) the tab will read; tile files are
# bounded by the map store budget instead
MAX_DATA_BYTES = 50 * 1024 * 1024
# the most files one drop may carry
MAX_DROP_FILES = 10
# more areas or points than this in one drop default to an overlay in the
# prompt rather than one location task each
MANY_FEATURES = 20

DATA_FORMATS = ("geojson", "kml", "kmz", "gpx", "csv", "shapefile")
TILE_FORMATS = ("pmtiles", "mbtiles")
ALL_FORMATS = DATA_FORMATS + TILE_FORMATS + ("mission",)

# what the drop accepts, for the operator-facing message
ACCEPTED_TEXT = ("GeoJSON (.geojson, .json), KML, KMZ, GPX, CSV with latitude and "
                 "longitude columns, a zipped shapefile, and PMTiles or MBTiles "
                 "map tiles")

_EXT_FORMAT = {".geojson": "geojson", ".json": "geojson", ".kml": "kml",
               ".kmz": "kmz", ".gpx": "gpx", ".csv": "csv", ".txt": "csv",
               ".tsv": "csv", ".zip": "shapefile", ".shp": "shapefile",
               ".pmtiles": "pmtiles", ".mbtiles": "mbtiles"}

_LAT_NAMES = ("lat", "latitude", "lat_dd", "lat_deg", "latitude_deg", "y")
_LON_NAMES = ("lon", "lng", "long", "longitude", "lon_dd", "lon_deg",
              "longitude_deg", "x")
_NAME_NAMES = ("name", "label", "title", "callsign", "site", "id", "description")
# radius columns and the factor that turns them into metres
_RADIUS_NAMES = (("radius_m", 1.0), ("radius_meters", 1.0), ("radius", 1.0),
                 ("r_m", 1.0), ("radius_km", 1000.0), ("radius_nm", 1852.0))

PMTILES_TILE_TYPES = {1: "vector", 2: "png", 3: "jpeg", 4: "webp", 5: "avif"}


class MapImportError(ValueError):
    """A file the drop cannot use, with a message meant for the operator."""


# the feature set

def empty_features(source: str = "", fmt: str = "") -> dict:
    return {"source": source, "format": fmt, "lines": [], "polygons": [],
            "points": [], "skipped": 0}


def feature_count(feats: dict) -> int:
    return len(feats["lines"]) + len(feats["polygons"]) + len(feats["points"])


def merge_features(sets: list) -> dict:
    """Several files from one drop as one set (names are kept per feature)."""
    out = empty_features(", ".join(s.get("source", "") for s in sets if s.get("source")),
                         ", ".join(sorted({s.get("format", "") for s in sets if s.get("format")})))
    for s in sets:
        for k in ("lines", "polygons", "points"):
            out[k].extend(s.get(k, []))
        out["skipped"] += int(s.get("skipped", 0))
    return out


def summary(feats: dict) -> str:
    """'2 lines, 1 area, 5 points' for the prompt."""
    parts = []
    for key, one, many in (("lines", "line", "lines"), ("polygons", "area", "areas"),
                           ("points", "point", "points")):
        n = len(feats.get(key, []))
        if n:
            parts.append(f"{n} {one if n == 1 else many}")
    s = ", ".join(parts) if parts else "nothing usable"
    if feats.get("skipped"):
        s += f" ({feats['skipped']} skipped)"
    return s


def longest_line(feats: dict):
    """The line with the most ground length, or None."""
    best, best_len = None, -1.0
    for ln in feats.get("lines", []):
        d = _line_length_m(ln["coords"])
        if d > best_len:
            best, best_len = ln, d
    return best


def path_from_features(feats: dict) -> list:
    """A flight path from a set: the longest line, else the points in file
    order. Raises MapImportError when neither gives two points."""
    ln = longest_line(feats)
    if ln is not None and len(ln["coords"]) >= 2:
        return [list(p) for p in ln["coords"]]
    pts = [[p["lon"], p["lat"]] for p in feats.get("points", [])]
    if len(pts) >= 2:
        return pts
    raise MapImportError("no line or two or more points to make a flight path from")


def bbox(feats: dict):
    """[west, south, east, north] over everything in the set, or None."""
    xs, ys = [], []
    for ln in feats.get("lines", []) + feats.get("polygons", []):
        for p in ln["coords"]:
            xs.append(p[0]); ys.append(p[1])
    for p in feats.get("points", []):
        xs.append(p["lon"]); ys.append(p["lat"])
    if not xs:
        return None
    return [min(xs), min(ys), max(xs), max(ys)]


def _line_length_m(coords) -> float:
    d = 0.0
    for a, b in zip(coords, coords[1:]):
        dlat = (b[1] - a[1]) * 111320.0
        dlon = (b[0] - a[0]) * 111320.0 * math.cos(math.radians((a[1] + b[1]) / 2.0))
        d += math.hypot(dlat, dlon)
    return d


def _clean_coords(raw) -> list:
    """[lon, lat] floats from loose pairs (extra members such as altitude
    are dropped); pairs out of range are left out."""
    out = []
    for p in raw or []:
        if isinstance(p, (list, tuple)) and len(p) >= 2 and mf._latlon_ok(p[1], p[0]):
            out.append([float(p[0]), float(p[1])])
    return out


def _unclose(ring: list) -> list:
    if len(ring) > 1 and ring[0] == ring[-1]:
        return ring[:-1]
    return ring


def _add_line(feats, name, coords):
    c = _clean_coords(coords)
    if len(c) >= 2:
        feats["lines"].append({"name": name or "", "coords": c})
    else:
        feats["skipped"] += 1


def _add_polygon(feats, name, ring):
    c = _unclose(_clean_coords(ring))
    if len(c) >= 3:
        feats["polygons"].append({"name": name or "", "coords": c})
    else:
        feats["skipped"] += 1


def _add_point(feats, name, lon, lat, radius_m=None):
    if mf._latlon_ok(lat, lon):
        p = {"name": name or "", "lat": float(lat), "lon": float(lon)}
        r = _radius_value(radius_m)
        if r:
            p["radius_m"] = r
        feats["points"].append(p)
    else:
        feats["skipped"] += 1


def _radius_value(v):
    try:
        r = float(v)
    except (TypeError, ValueError):
        return None
    return r if r > 0 else None


# sniffing

def sniff(name: str, data: bytes) -> str:
    """The format of a file, or '' when it is none of ours. The extension is
    taken first; when it is missing, unknown, or does not match what the bytes
    say, the bytes decide. data may be just the start of the file (the first
    few kilobytes are enough)."""
    ext = os.path.splitext(str(name or ""))[1].lower()
    by_ext = _EXT_FORMAT.get(ext, "")
    by_content = _sniff_content(data)
    if by_content == "zip":
        by_content = _sniff_zip(data) if _looks_complete_zip(data) else ""
        if not by_content:
            return by_ext if by_ext in ("kmz", "shapefile") else "shapefile"
    if by_content:
        # a .json that holds a mission plan, a .txt that is really CSV, and so
        # on: the content wins over a loose extension
        if by_ext in ("geojson", "csv", "") or by_ext != by_content:
            return by_content
        return by_ext
    if by_ext == "csv":
        return "csv" if _looks_like_csv(data) else ""
    return by_ext


def _looks_complete_zip(data: bytes) -> bool:
    try:
        return zipfile.is_zipfile(io.BytesIO(data))
    except Exception:
        return False


def _sniff_content(data: bytes) -> str:
    if not data:
        return ""
    if data[:7] == b"PMTiles":
        return "pmtiles"
    if data[:16] == b"SQLite format 3\x00":
        return "mbtiles"
    if data[:4] == b"PK\x03\x04":
        return "zip"
    text = data[:65536].decode("utf-8", "replace").lstrip("﻿ \t\r\n")
    if not text:
        return ""
    if text[0] in "{[":
        if '"dpmg-mission/' in text[:400]:
            return "mission"
        return "geojson"
    if text[0] == "<":
        low = text[:4096].lower()
        if "<gpx" in low:
            return "gpx"
        if "<kml" in low or "<placemark" in low or "opengis.net/kml" in low:
            return "kml"
        return ""
    if _looks_like_csv(data):
        return "csv"
    return ""


def _sniff_zip(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = [n.lower() for n in z.namelist()]
    except zipfile.BadZipFile:
        return ""
    if any(n.endswith(".shp") for n in names):
        return "shapefile"
    if any(n.endswith(".kml") for n in names):
        return "kmz"
    return ""


def _looks_like_csv(data: bytes) -> bool:
    text = data[:8192].decode("utf-8", "replace").lstrip("﻿")
    first = text.splitlines()[0].lower() if text.strip() else ""
    cols = [c.strip().strip('"') for c in re.split(r"[,;\t|]", first)]
    return _pick_col(cols, _LAT_NAMES) is not None and _pick_col(cols, _LON_NAMES) is not None


# parsing

def parse(name: str, data: bytes, fmt: str = None) -> dict:
    """The feature set in a data file. fmt is the sniffed format (sniffed here
    when not given). Raises MapImportError for a tile file, a mission plan, an
    unknown format or a file with nothing usable in it."""
    fmt = fmt or sniff(name, data)
    if fmt in TILE_FORMATS:
        raise MapImportError(f"{name} is a {fmt} tile file, not map data")
    if fmt == "mission":
        raise MapImportError(f"{name} is a mission plan; use Load for it")
    if fmt not in DATA_FORMATS:
        raise MapImportError(f"{name}: not a map data file this map reads")
    if len(data) > MAX_DATA_BYTES:
        raise MapImportError(f"{name} is {len(data) // (1024 * 1024)} MB; the limit for a "
                             f"data file is {MAX_DATA_BYTES // (1024 * 1024)} MB")
    try:
        if fmt == "geojson":
            feats = features_from_geojson(data)
        elif fmt == "kml":
            feats = features_from_kml(data)
        elif fmt == "kmz":
            feats = features_from_kmz(data)
        elif fmt == "gpx":
            feats = features_from_gpx(data)
        elif fmt == "csv":
            feats = features_from_csv(data)
        else:
            feats = features_from_shapefile_zip(data)
    except MapImportError as ex:
        raise MapImportError(f"{name}: {ex}")
    except Exception as ex:                        # bad XML, JSON, zip
        raise MapImportError(f"could not read {name} as {fmt}: {ex}")
    feats["source"] = str(name or "")
    feats["format"] = fmt
    if feature_count(feats) == 0:
        raise MapImportError(f"{name}: no lines, areas or points with usable "
                             f"coordinates in it")
    return feats


def _text(data) -> str:
    if isinstance(data, bytes):
        return data.decode("utf-8-sig", "replace")
    return str(data)


# GeoJSON

def _prop_name(props: dict) -> str:
    if not isinstance(props, dict):
        return ""
    for k in _NAME_NAMES:
        for kk, v in props.items():
            if str(kk).lower() == k and v not in (None, ""):
                return str(v)
    return ""


def _prop_radius(props: dict):
    if not isinstance(props, dict):
        return None
    for k, factor in _RADIUS_NAMES:
        for kk, v in props.items():
            if str(kk).lower() == k:
                r = _radius_value(v)
                if r:
                    return r * factor
    return None


def features_from_geojson(data) -> dict:
    """Every geometry in a GeoJSON document: Point, MultiPoint, LineString,
    MultiLineString, Polygon (outer ring; holes are dropped), MultiPolygon,
    GeometryCollection, inside Features or a FeatureCollection, or bare. A
    Feature's name comes from its properties; a point with a radius property
    (radius_m, radius, radius_km, radius_nm) carries it as a circle."""
    obj = json.loads(_text(data)) if isinstance(data, (bytes, str)) else data
    feats = empty_features()

    def geom(g, name, radius):
        if not isinstance(g, dict):
            return
        t = g.get("type")
        c = g.get("coordinates")
        if t == "Point":
            if isinstance(c, (list, tuple)) and len(c) >= 2:
                _add_point(feats, name, c[0], c[1], radius)
            else:
                feats["skipped"] += 1
        elif t == "MultiPoint":
            for p in c or []:
                if isinstance(p, (list, tuple)) and len(p) >= 2:
                    _add_point(feats, name, p[0], p[1], radius)
        elif t == "LineString":
            _add_line(feats, name, c)
        elif t == "MultiLineString":
            for part in c or []:
                _add_line(feats, name, part)
        elif t == "Polygon":
            if c:
                _add_polygon(feats, name, c[0])
        elif t == "MultiPolygon":
            for poly in c or []:
                if poly:
                    _add_polygon(feats, name, poly[0])
        elif t == "GeometryCollection":
            for gg in g.get("geometries") or []:
                geom(gg, name, radius)
        elif t == "Feature":
            props = g.get("properties") or {}
            geom(g.get("geometry"), _prop_name(props) or name, _prop_radius(props))
        elif t == "FeatureCollection":
            for f in g.get("features") or []:
                geom(f, "", None)

    if isinstance(obj, list):
        # a bare coordinate list is a path
        _add_line(feats, "", obj)
    else:
        geom(obj, "", None)
    return feats


# KML and KMZ

def _tag(el) -> str:
    return el.tag.split("}")[-1].lower()


def _kml_coords(text) -> list:
    """'lon,lat[,alt] lon,lat ...' to [lon, lat] pairs."""
    out = []
    for tok in (text or "").split():
        parts = tok.split(",")
        if len(parts) < 2:
            continue
        try:
            out.append([float(parts[0]), float(parts[1])])
        except ValueError:
            continue
    return out


def _child_text(el, tag) -> str:
    for c in el:
        if _tag(c) == tag:
            return (c.text or "").strip()
    return ""


def features_from_kml(data) -> dict:
    """Placemarks with Point, LineString, Polygon (outer boundary), gx:Track
    and MultiGeometry; a document with no Placemark is walked for the same
    geometries. The Placemark name goes on each of its geometries."""
    root = ET.fromstring(_text(data))
    feats = empty_features()
    marks = [el for el in root.iter() if _tag(el) == "placemark"]
    if not marks:
        marks = [root]
    for pm in marks:
        name = _child_text(pm, "name")
        _kml_geoms(pm, name, feats)
    return feats


def _kml_geoms(el, name, feats):
    for g in el.iter():
        t = _tag(g)
        if t == "point":
            pts = _kml_coords(_first_coords_text(g))
            if pts:
                _add_point(feats, name, pts[0][0], pts[0][1])
            else:
                feats["skipped"] += 1
        elif t == "linestring":
            _add_line(feats, name, _kml_coords(_first_coords_text(g)))
        elif t == "track":
            coords = []
            for c in g:
                if _tag(c) == "coord":
                    parts = (c.text or "").split()
                    if len(parts) >= 2:
                        try:
                            coords.append([float(parts[0]), float(parts[1])])
                        except ValueError:
                            pass
            _add_line(feats, name, coords)
        elif t == "polygon":
            outer = next((c for c in g if _tag(c) == "outerboundaryis"), None)
            src = outer if outer is not None else g
            ring = next((r for r in src.iter() if _tag(r) == "linearring"), None)
            if ring is None:
                feats["skipped"] += 1
                continue
            _add_polygon(feats, name, _kml_coords(_first_coords_text(ring)))


def _first_coords_text(el) -> str:
    c = next((x for x in el.iter() if _tag(x) == "coordinates"), None)
    return (c.text or "") if c is not None else ""


def features_from_kmz(data: bytes) -> dict:
    """The doc.kml (or the first .kml) inside a KMZ."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        kml = next((n for n in names if n.lower() == "doc.kml"), None) \
            or next((n for n in names if n.lower().endswith(".kml")), None)
        if kml is None:
            raise MapImportError("the KMZ holds no .kml file")
        return features_from_kml(z.read(kml))


# GPX

def features_from_gpx(data) -> dict:
    """Tracks (segments joined, one line per track), routes (one line each)
    and waypoints (points), each named from its name element."""
    root = ET.fromstring(_text(data))
    feats = empty_features()

    def latlon(el):
        try:
            return float(el.get("lon")), float(el.get("lat"))
        except (TypeError, ValueError):
            return None

    for el in root.iter():
        t = _tag(el)
        if t in ("trk", "rte"):
            name = _child_text(el, "name")
            ptag = "trkpt" if t == "trk" else "rtept"
            coords = [ll for ll in (latlon(p) for p in el.iter() if _tag(p) == ptag) if ll]
            _add_line(feats, name, coords)
        elif t == "wpt":
            ll = latlon(el)
            if ll:
                _add_point(feats, _child_text(el, "name"), ll[0], ll[1])
            else:
                feats["skipped"] += 1
    return feats


# CSV

def _pick_col(cols, names):
    for n in names:
        for i, c in enumerate(cols):
            if c == n:
                return i
    return None


def csv_columns(header: list) -> dict:
    """Which columns hold the latitude, longitude, name and radius, by header
    name (case and spaces ignored). lat and lon are None when not found."""
    cols = [str(c or "").strip().strip('"').lower().replace(" ", "_") for c in header]
    out = {"lat": _pick_col(cols, _LAT_NAMES), "lon": _pick_col(cols, _LON_NAMES),
           "name": _pick_col(cols, _NAME_NAMES), "radius": None, "radius_factor": 1.0}
    for n, factor in _RADIUS_NAMES:
        i = _pick_col(cols, (n,))
        if i is not None:
            out["radius"], out["radius_factor"] = i, factor
            break
    return out


def features_from_csv(data) -> dict:
    """One point per row from a CSV (comma, semicolon, tab or pipe) with a
    header naming the latitude and longitude columns (lat, latitude, y; lon,
    lng, long, longitude, x). A name column (name, label, title, callsign,
    site, id, description) and a radius column (radius_m, radius, radius_km,
    radius_nm) are used when present."""
    text = _text(data)
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        raise MapImportError("the CSV is empty")
    cols = csv_columns(rows[0])
    if cols["lat"] is None or cols["lon"] is None:
        raise MapImportError("no latitude and longitude columns in the CSV header "
                             "(looked for lat/latitude/y and lon/lng/longitude/x)")
    feats = empty_features()
    for r in rows[1:]:
        try:
            lat = float(r[cols["lat"]].strip())
            lon = float(r[cols["lon"]].strip())
        except (IndexError, ValueError):
            feats["skipped"] += 1
            continue
        name = r[cols["name"]].strip() if cols["name"] is not None and cols["name"] < len(r) else ""
        radius = None
        if cols["radius"] is not None and cols["radius"] < len(r):
            rv = _radius_value(r[cols["radius"]].strip())
            radius = rv * cols["radius_factor"] if rv else None
        _add_point(feats, name, lon, lat, radius)
    return feats


# zipped shapefile

def _ring_is_clockwise(ring) -> bool:
    s = 0.0
    for a, b in zip(ring, ring[1:] + ring[:1]):
        s += (b[0] - a[0]) * (b[1] + a[1])
    return s > 0


def features_from_shapefile_zip(data: bytes) -> dict:
    """A zip holding one or more shapefiles (.shp with .dbf and .shx). Points,
    multipoints, polylines (one line per part) and polygons (clockwise rings
    are outer rings; holes are dropped). Needs geographic coordinates: a .prj
    naming a projected system is refused, and so are coordinates outside the
    latitude and longitude range. Names come from a name-like dbf field."""
    try:
        import shapefile                       # pyshp
    except ImportError:
        raise MapImportError("shapefile support is not installed on this console "
                             "(the pyshp wheel in deploy/wheels)")
    feats = empty_features()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        members = {n.lower(): n for n in z.namelist()}
        shps = [n for n in members if n.endswith(".shp")]
        if not shps:
            raise MapImportError("no .shp inside the zip")
        for shp in shps:
            base = shp[:-4]
            prj = members.get(base + ".prj")
            if prj:
                wkt = z.read(prj).decode("utf-8", "replace")
                if "PROJCS" in wkt.upper():
                    raise MapImportError("the shapefile uses a projected coordinate system; "
                                         "export it in WGS84 latitude and longitude first")
            kw = {"shp": io.BytesIO(z.read(members[shp]))}
            for ext in (".shx", ".dbf"):
                if members.get(base + ext):
                    kw[ext[1:]] = io.BytesIO(z.read(members[base + ext]))
            with shapefile.Reader(**kw) as rd:
                _shapefile_features(rd, feats)
    return feats


def _shapefile_features(rd, feats):
    fields = [f[0] for f in rd.fields if f[0] != "DeletionFlag"]
    name_idx = None
    for n in _NAME_NAMES:
        for i, f in enumerate(fields):
            if f.lower() == n:
                name_idx = i
                break
        if name_idx is not None:
            break
    have_dbf = rd.dbf is not None
    for i, shape in enumerate(rd.iterShapes()):
        name = ""
        if have_dbf and name_idx is not None:
            try:
                name = str(rd.record(i)[name_idx] or "").strip()
            except Exception:
                name = ""
        kind = (shape.shapeTypeName or "").upper()
        pts = [[float(p[0]), float(p[1])] for p in shape.points]
        if pts and not all(mf._latlon_ok(p[1], p[0]) for p in pts):
            raise MapImportError("the shapefile's coordinates are not latitude and longitude "
                                 "(values out of range); export it in WGS84 first")
        parts = list(shape.parts) + [len(pts)] if pts else []
        if kind.startswith("POINT"):
            if pts:
                _add_point(feats, name, pts[0][0], pts[0][1])
            else:
                feats["skipped"] += 1
        elif kind.startswith("MULTIPOINT"):
            for p in pts:
                _add_point(feats, name, p[0], p[1])
        elif kind.startswith("POLYLINE"):
            for a, b in zip(parts, parts[1:]):
                _add_line(feats, name, pts[a:b])
        elif kind.startswith("POLYGON"):
            for a, b in zip(parts, parts[1:]):
                ring = _unclose(pts[a:b])
                if len(ring) >= 3 and (_ring_is_clockwise(ring) or len(parts) == 2):
                    _add_polygon(feats, name, ring)
        else:
            feats["skipped"] += 1


# tile files

PMTILES_HEADER_LEN = 127


def pmtiles_header(head: bytes) -> dict:
    """The zoom range, bounds, centre and tile type from a PMTiles v3 header
    (the first 127 bytes of the file)."""
    if len(head) < PMTILES_HEADER_LEN or head[:7] != b"PMTiles":
        raise MapImportError("not a PMTiles file")
    version = head[7]
    if version != 3:
        raise MapImportError(f"PMTiles version {version}; this map reads version 3")
    tile_type = head[99]
    minz, maxz = head[100], head[101]
    w, s, e, n = struct.unpack_from("<iiii", head, 102)
    cz = head[118]
    clon, clat = struct.unpack_from("<ii", head, 119)
    return {"tile_type": PMTILES_TILE_TYPES.get(tile_type, "unknown"),
            "minzoom": int(minz), "maxzoom": int(maxz),
            "bbox": [w / 1e7, s / 1e7, e / 1e7, n / 1e7],
            "center": [clat / 1e7, clon / 1e7, int(cz)]}


def mbtiles_info(path: str) -> dict:
    """The metadata table of an MBTiles file: bounds, zoom range and tile
    format (pbf is vector)."""
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = con.execute("SELECT name, value FROM metadata").fetchall()
        finally:
            con.close()
    except sqlite3.Error as ex:
        raise MapImportError(f"not an MBTiles file ({ex})")
    meta = {str(k): str(v) for k, v in rows}
    out = {"tile_type": "unknown", "minzoom": 0, "maxzoom": 14, "bbox": None}
    fmt = meta.get("format", "").lower()
    out["tile_type"] = "vector" if fmt in ("pbf", "mvt") else (fmt or "unknown")
    for key in ("minzoom", "maxzoom"):
        try:
            out[key] = int(float(meta[key]))
        except (KeyError, ValueError):
            pass
    try:
        b = [float(x) for x in meta["bounds"].split(",")]
        if len(b) == 4:
            out["bbox"] = b
    except (KeyError, ValueError):
        pass
    return out


def area_name_for(filename: str) -> str:
    """A store area name from a dropped file name: the stem, made safe."""
    from . import map_store
    stem = os.path.splitext(os.path.basename(str(filename or "")))[0]
    return map_store.safe_area_name(stem or "area")
