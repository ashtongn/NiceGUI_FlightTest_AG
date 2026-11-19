"""Offline map tile store for the mission planning tab.

Holds the worldwide base map and the on-demand area downloads as PMTiles files,
plus the renderer assets (style, glyphs, sprites), outside the source tree and
outside the application image. The store reports its size against a budget and
lists what is stored with each entry's zoom range, size and age.

Layout under the store directory (default ~/.dpmg/maps, override DPMG_MAP_STORE):

  world-base.pmtiles      the shipped worldwide base (max zoom 7)
  areas/<name>.pmtiles    one file per downloaded area of interest
  assets/                 style.json, glyphs/, sprites/ for the offline renderer
  index.json              the catalogue: base and areas, zoom range, size, age

This module does no downloading and reaches no network. It reads and writes the
index, reports sizes against the budget, and answers whether a point or a view
is covered by what is stored. The base map is built on the x86 by
tools/maps/build_base_map.py and the area downloads are pulled by the tab
(dpmg/mission/base_map.py); both record what
they wrote here through add_base and add_area.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import sys


DEFAULT_DIR = os.path.expanduser("~/.dpmg/maps")
INDEX_NAME = "index.json"
BASE_FILE = "world-base.pmtiles"
AREAS_DIR = "areas"
ASSETS_DIR = "assets"

# the default store budget (adjustable); 20 GB leaves room for a continent at
# zoom 12 or several states at zoom 14 on a laptop disk
DEFAULT_BUDGET_BYTES = 20 * 1024 ** 3        # 20 GB
BUDGET_WARN_FRACTION = 0.8                   # warn once the store passes 80 %

# map data older than this is flagged stale in the view; a review aid, not a block
DEFAULT_STALE_DAYS = 180

# the shipped base is built to this max zoom; a view past it needs an area
BASE_MAXZOOM = 7
AREA_MAXZOOM = 14


def store_dir() -> str:
    """The map store directory: DPMG_MAP_STORE if set, else ~/.dpmg/maps."""
    return os.environ.get("DPMG_MAP_STORE") or DEFAULT_DIR


def _p(*parts) -> str:
    return os.path.join(store_dir(), *parts)


def ensure_store() -> str:
    """Make the store directory and its subdirectories if they are missing.
    Returns the store directory."""
    d = store_dir()
    os.makedirs(os.path.join(d, AREAS_DIR), exist_ok=True)
    os.makedirs(os.path.join(d, ASSETS_DIR), exist_ok=True)
    return d


def index_path() -> str:
    return _p(INDEX_NAME)


# the go-pmtiles tool

BIN_DIR = "bin"


def pmtiles_tool_name(platform: str = None) -> str:
    """The go-pmtiles file name on this platform: pmtiles.exe on Windows,
    pmtiles elsewhere."""
    return "pmtiles.exe" if (platform or sys.platform) == "win32" else "pmtiles"


def pmtiles_tool_path(platform: str = None) -> str:
    """Where vendor_map_assets.py puts the go-pmtiles tool in the store:
    <store>/bin/pmtiles, or <store>/bin/pmtiles.exe on Windows."""
    return _p(BIN_DIR, pmtiles_tool_name(platform))


def find_pmtiles_tool(platform: str = None):
    """Locate the go-pmtiles binary: PATH first (pmtiles, and pmtiles.exe on
    Windows, which shutil.which also resolves through PATHEXT), then the store's
    bin directory with either name. Returns the path, or None when it is not
    installed."""
    platform = platform or sys.platform
    for name in ("pmtiles", "pmtiles.exe") if platform == "win32" else ("pmtiles",):
        found = shutil.which(name)
        if found:
            return found
    for name in (pmtiles_tool_name(platform), "pmtiles", "pmtiles.exe"):
        cand = _p(BIN_DIR, name)
        if os.path.isfile(cand):
            return cand
    return None


def _blank_index() -> dict:
    return {"base": None, "areas": [], "budget_bytes": DEFAULT_BUDGET_BYTES}


def load_index() -> dict:
    """Read index.json, or a blank index if it is missing or unreadable. Never
    raises: a corrupt index reads as empty so the tab still opens."""
    try:
        with open(index_path(), "r", encoding="utf-8") as f:
            idx = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return _blank_index()
    if not isinstance(idx, dict):
        return _blank_index()
    idx.setdefault("base", None)
    idx.setdefault("areas", [])
    idx.setdefault("budget_bytes", DEFAULT_BUDGET_BYTES)
    if not isinstance(idx["areas"], list):
        idx["areas"] = []
    return idx


def save_index(idx: dict) -> None:
    ensure_store()
    with open(index_path(), "w", encoding="utf-8") as f:
        json.dump(idx, f, indent=2)
        f.write("\n")


# names and sizes

def safe_area_name(name: str) -> str:
    """A filesystem-safe area name: letters, digits, dash and underscore. Keeps
    the download from writing outside the areas directory."""
    s = re.sub(r"[^A-Za-z0-9_-]+", "-", (name or "").strip()).strip("-")
    return s or "area"


def area_file(name: str) -> str:
    """The relative path an area's pmtiles is stored at, from its name."""
    return os.path.join(AREAS_DIR, safe_area_name(name) + ".pmtiles")


def _entry_size(entry: dict) -> int:
    """The on-disk size of an entry's file if it is there, else the size_bytes
    recorded when it was written, else zero."""
    if not entry:
        return 0
    rel = entry.get("file")
    if rel:
        try:
            return os.path.getsize(_p(rel))
        except OSError:
            pass
    try:
        return int(entry.get("size_bytes") or 0)
    except (TypeError, ValueError):
        return 0


def total_size(idx: dict = None) -> int:
    """Bytes used by the base plus every area (from disk where the file exists,
    else the recorded size)."""
    idx = idx if idx is not None else load_index()
    n = _entry_size(idx.get("base"))
    for a in idx.get("areas", []):
        n += _entry_size(a)
    return n


def budget_bytes(idx: dict = None) -> int:
    idx = idx if idx is not None else load_index()
    v = idx.get("budget_bytes")
    if v is None:                    # a budget of 0 is a real value, not missing
        return DEFAULT_BUDGET_BYTES
    try:
        return int(v)
    except (TypeError, ValueError):
        return DEFAULT_BUDGET_BYTES


def set_budget(gb: float) -> dict:
    """Set the store budget in gigabytes and save the index."""
    idx = load_index()
    idx["budget_bytes"] = max(0, int(round(float(gb) * 1024 ** 3)))
    save_index(idx)
    return idx


def free_bytes(idx: dict = None) -> int:
    idx = idx if idx is not None else load_index()
    return max(0, budget_bytes(idx) - total_size(idx))


def over_warn(idx: dict = None) -> bool:
    idx = idx if idx is not None else load_index()
    b = budget_bytes(idx)
    return b > 0 and total_size(idx) >= b * BUDGET_WARN_FRACTION


def would_exceed_budget(extra_bytes: int, idx: dict = None) -> bool:
    """True if adding extra_bytes would put the store over its budget."""
    idx = idx if idx is not None else load_index()
    return total_size(idx) + max(0, int(extra_bytes)) > budget_bytes(idx)


def human_size(n: int) -> str:
    """A short byte count: 0 B, 46 MB, 1.8 GB."""
    n = float(max(0, int(n)))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(n)} {unit}"
            return f"{n:.0f} {unit}" if n >= 10 else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# age

def _parse_iso(ts):
    if not ts:
        return None
    try:
        return datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def age_days(entry: dict):
    """How old an entry is in days from its created stamp, or None if unknown."""
    dt = _parse_iso((entry or {}).get("created"))
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    now = datetime.datetime.now(datetime.timezone.utc)
    return max(0.0, (now - dt).total_seconds() / 86400.0)


def is_stale(entry: dict, max_age_days: float = DEFAULT_STALE_DAYS) -> bool:
    d = age_days(entry)
    return d is not None and d > max_age_days


# base and areas

def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(
        microsecond=0).isoformat()


def base_present(idx: dict = None) -> bool:
    idx = idx if idx is not None else load_index()
    b = idx.get("base")
    return bool(b) and os.path.exists(_p(b.get("file", BASE_FILE)))


def add_base(*, minzoom=0, maxzoom=BASE_MAXZOOM, size_bytes=None,
             source="", name="world-base", idx: dict = None) -> dict:
    """Record the shipped base map in the index (the file is written by the
    build script). Returns the saved index."""
    idx = idx if idx is not None else load_index()
    idx["base"] = {"name": name, "file": BASE_FILE,
                   "minzoom": int(minzoom), "maxzoom": int(maxzoom),
                   "size_bytes": _resolved_size(BASE_FILE, size_bytes),
                   "created": _now_iso(), "source": source}
    save_index(idx)
    return idx


def _resolved_size(rel, size_bytes):
    if size_bytes is not None:
        try:
            return int(size_bytes)
        except (TypeError, ValueError):
            pass
    try:
        return os.path.getsize(_p(rel))
    except OSError:
        return 0


def get_area(name: str, idx: dict = None):
    idx = idx if idx is not None else load_index()
    sn = safe_area_name(name)
    return next((a for a in idx.get("areas", [])
                 if safe_area_name(a.get("name", "")) == sn), None)


def add_area(name, bbox, *, minzoom=0, maxzoom=AREA_MAXZOOM, size_bytes=None,
             source="", tile_type="vector", idx: dict = None) -> dict:
    """Record a downloaded area (its pmtiles file is written by the caller).
    Replaces an area of the same name. bbox is [west, south, east, north].
    tile_type is "vector" for the usual Protomaps tiles; a dropped raster
    set (png, jpeg, webp) says so and the map draws it as an image layer.
    Returns the saved index."""
    idx = idx if idx is not None else load_index()
    rel = area_file(name)
    rec = {"name": safe_area_name(name), "file": rel, "bbox": list(bbox),
           "minzoom": int(minzoom), "maxzoom": int(maxzoom),
           "size_bytes": _resolved_size(rel, size_bytes),
           "created": _now_iso(), "source": source}
    if tile_type and tile_type != "vector":
        rec["tile_type"] = str(tile_type)
    idx["areas"] = [a for a in idx.get("areas", [])
                    if safe_area_name(a.get("name", "")) != rec["name"]]
    idx["areas"].append(rec)
    save_index(idx)
    return idx


def remove_area(name: str, idx: dict = None) -> dict:
    """Delete an area's pmtiles file and drop it from the index."""
    idx = idx if idx is not None else load_index()
    sn = safe_area_name(name)
    for a in idx.get("areas", []):
        if safe_area_name(a.get("name", "")) == sn:
            try:
                os.remove(_p(a.get("file", area_file(sn))))
            except OSError:
                pass
    idx["areas"] = [a for a in idx.get("areas", [])
                    if safe_area_name(a.get("name", "")) != sn]
    save_index(idx)
    return idx


def entries(idx: dict = None) -> list:
    """A flat list for the area manager: the base first (if present) then the
    areas, each with name, kind, zoom range, size and age in days."""
    idx = idx if idx is not None else load_index()
    out = []
    b = idx.get("base")
    if b:
        out.append({"name": b.get("name", "world-base"), "kind": "base",
                    "bbox": None, "minzoom": b.get("minzoom", 0),
                    "maxzoom": b.get("maxzoom", BASE_MAXZOOM),
                    "size_bytes": _entry_size(b), "age_days": age_days(b),
                    "source": b.get("source", ""),
                    "present": os.path.exists(_p(b.get("file", BASE_FILE)))})
    for a in idx.get("areas", []):
        out.append({"name": a.get("name", ""), "kind": "area",
                    "bbox": a.get("bbox"), "minzoom": a.get("minzoom", 0),
                    "maxzoom": a.get("maxzoom", AREA_MAXZOOM),
                    "size_bytes": _entry_size(a), "age_days": age_days(a),
                    "source": a.get("source", ""),
                    "tile_type": a.get("tile_type", "vector"),
                    "present": os.path.exists(_p(a.get("file", "")))})
    return out


# coverage

def point_in_bbox(lat, lon, bbox) -> bool:
    """bbox is [west, south, east, north]. Longitudes outside the box fail; this
    does not wrap the antimeridian (areas of interest do not straddle it)."""
    try:
        w, s, e, n = (float(bbox[0]), float(bbox[1]),
                      float(bbox[2]), float(bbox[3]))
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError, IndexError):
        return False
    return s <= lat <= n and w <= lon <= e


def detail_at(lat, lon, idx: dict = None) -> int:
    """The best max zoom available at a point: an area's max zoom if one covers
    it, else the base's max zoom if the base is present, else -1 (no coverage)."""
    idx = idx if idx is not None else load_index()
    best = -1
    if base_present(idx):
        best = int((idx.get("base") or {}).get("maxzoom", BASE_MAXZOOM))
    for a in idx.get("areas", []):
        if a.get("bbox") and point_in_bbox(lat, lon, a["bbox"]) \
                and os.path.exists(_p(a.get("file", ""))):
            best = max(best, int(a.get("maxzoom", AREA_MAXZOOM)))
    return best


def view_warnings(center_lat, center_lon, zoom, *,
                  max_age_days=DEFAULT_STALE_DAYS, idx: dict = None) -> list:
    """Warnings for the current map view, as plain strings: no base map, the
    view zoomed past what is stored here, or the covering data is old. A review
    aid for the tab, matching the map safeguards in the requirements."""
    idx = idx if idx is not None else load_index()
    out = []
    if not base_present(idx):
        out.append("no base map in the store: build it on the x86 and copy it "
                   "to " + store_dir())
        return out
    have = detail_at(center_lat, center_lon, idx)
    if zoom is not None and have >= 0 and float(zoom) > have + 0.5:
        out.append(f"view is at zoom {float(zoom):.0f} but stored map data here "
                   f"goes to zoom {have}: download this area for more detail")
    # staleness of whatever covers the centre
    old = []
    b = idx.get("base")
    if b and is_stale(b, max_age_days):
        old.append("base")
    for a in idx.get("areas", []):
        if a.get("bbox") and point_in_bbox(center_lat, center_lon, a["bbox"]) \
                and is_stale(a, max_age_days):
            old.append(a.get("name", "area"))
    if old:
        out.append("map data here is older than "
                   f"{int(max_age_days)} days ({', '.join(old)})")
    return out
