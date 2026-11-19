"""Offline base map and area downloads for the map store.

The world base and the on-demand areas are cut out of the current Protomaps
daily planet build with the go-pmtiles CLI and recorded in the store index
through map_store. The planner's area download dialog calls download_area();
tools/maps/build_base_map.py is the command line front end for build_base().
"""

from __future__ import annotations

import datetime
import os
import subprocess
import urllib.request

from . import map_store

BUILD_BASE_URL = "https://build.protomaps.com"


def find_pmtiles(explicit=None) -> str:
    """Locate the go-pmtiles binary: --pmtiles, then PATH, then the store's
    bin directory (pmtiles, or pmtiles.exe on Windows; see
    map_store.find_pmtiles_tool)."""
    if explicit:
        if os.path.exists(explicit):
            return explicit
        raise SystemExit(f"no pmtiles binary at {explicit}")
    found = map_store.find_pmtiles_tool()
    if not found:
        raise SystemExit(
            "pmtiles CLI not found. Run tools/maps/vendor_map_assets.py, or install "
            "go-pmtiles (https://github.com/protomaps/go-pmtiles/releases) "
            "or pass --pmtiles /path/to/pmtiles")
    return found


def _head_ok(url: str, timeout=15) -> bool:
    # the build server answers 403 to Python's default user agent
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "dpmg-map-store"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def latest_build_date(max_back_days=14) -> str:
    """The most recent daily build date that exists on the build server.
    Protomaps keeps only the last few dailies, so this walks back from today."""
    today = datetime.date.today()
    for i in range(max_back_days):
        d = (today - datetime.timedelta(days=i)).strftime("%Y%m%d")
        if _head_ok(f"{BUILD_BASE_URL}/{d}.pmtiles"):
            return d
    raise SystemExit(
        "no recent Protomaps daily build found; check "
        f"{BUILD_BASE_URL} and pass --date YYYYMMDD")


def planet_url(date: str) -> str:
    return f"{BUILD_BASE_URL}/{date}.pmtiles"


def _run(cmd, dry_run_label=""):
    print(("would run: " if dry_run_label else "run: ") + " ".join(cmd),
          flush=True)
    r = subprocess.run(cmd, capture_output=True, text=True)
    out = (r.stdout or "") + (r.stderr or "")
    print(out.rstrip(), flush=True)
    if r.returncode != 0:
        raise SystemExit(f"pmtiles failed ({r.returncode})")
    return out


def extract(pm, src_url, out_path, *, maxzoom, bbox=None, dry_run=False):
    """Extract a zoom-capped (and optionally bbox) region from the planet build.
    Returns the pmtiles output. With dry_run, reports the size and writes
    nothing."""
    cmd = [pm, "extract", src_url, out_path, f"--maxzoom={int(maxzoom)}"]
    if bbox:
        cmd.append("--bbox=" + ",".join(str(x) for x in bbox))
    if dry_run:
        cmd.append("--dry-run")
    return _run(cmd, dry_run_label="dry" if dry_run else "")


def build_base(*, maxzoom=map_store.BASE_MAXZOOM, date=None, pmtiles=None,
               dry_run=False) -> dict:
    """Build world-base.pmtiles into the store and record it. Returns the index
    (or an empty dict on a dry run)."""
    pm = find_pmtiles(pmtiles)
    date = date or latest_build_date()
    src = planet_url(date)
    store = map_store.ensure_store()
    out = os.path.join(store, map_store.BASE_FILE)
    print(f"base map: {src}  ->  {out}  (max zoom {maxzoom})", flush=True)
    if dry_run:
        extract(pm, src, out, maxzoom=maxzoom, dry_run=True)
        return {}
    extract(pm, src, out, maxzoom=maxzoom, dry_run=False)
    size = os.path.getsize(out)
    idx = map_store.add_base(maxzoom=maxzoom, size_bytes=size,
                             source=f"protomaps {date}")
    print(f"recorded base: {map_store.human_size(size)} "
          f"(store total {map_store.human_size(map_store.total_size(idx))} "
          f"of {map_store.human_size(map_store.budget_bytes(idx))})", flush=True)
    return idx


def download_area(name, bbox, *, maxzoom=map_store.AREA_MAXZOOM, date=None,
                  pmtiles=None, dry_run=False):
    """Pull one area of interest to the store (the tab calls this). bbox is
    [west, south, east, north]. With dry_run, returns the predicted size in
    bytes without writing, so the tab can show it before anything is downloaded."""
    pm = find_pmtiles(pmtiles)
    date = date or latest_build_date()
    src = planet_url(date)
    store = map_store.ensure_store()
    rel = map_store.area_file(name)
    out = os.path.join(store, rel)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    if dry_run:
        text = extract(pm, src, out, maxzoom=maxzoom, bbox=bbox, dry_run=True)
        return _parse_archive_size(text)
    extract(pm, src, out, maxzoom=maxzoom, bbox=bbox, dry_run=False)
    size = os.path.getsize(out)
    map_store.add_area(name, bbox, maxzoom=maxzoom, size_bytes=size,
                       source=f"protomaps {date}")
    return size


def _parse_archive_size(text):
    """Pull the byte count out of the pmtiles 'archive size of N MB' line.
    Returns bytes, or None."""
    import re
    m = re.search(r"archive size of ([\d.]+)\s*([KMGT]?B)", text)
    if not m:
        return None
    n = float(m.group(1))
    mult = {"B": 1, "KB": 1024, "MB": 1024 ** 2,
            "GB": 1024 ** 3, "TB": 1024 ** 4}[m.group(2)]
    return int(n * mult)
