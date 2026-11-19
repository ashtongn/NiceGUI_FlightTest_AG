#!/usr/bin/env python3
"""Fetch the offline map renderer pieces once, on the x86, into the map store.

The planner's map is MapLibre GL JS drawing a Protomaps vector base from
PMTiles files. For true offline use every piece is kept in the store
(~/.dpmg/maps/assets) and served by the planner itself; nothing loads from a
CDN at run time. This script downloads them, pinned to known versions:

  maplibre-gl.js, maplibre-gl.css   the renderer (unpkg, MAPLIBRE_VERSION)
  pmtiles.js                        reads .pmtiles over HTTP range requests
  basemaps.js                       Protomaps style layers (npm tarball)
  fonts/                            label glyphs (protomaps/basemaps-assets)
  sprites/                          icons (same repository)
  bin/pmtiles                       the go-pmtiles tool, used by build_base_map
                                    (bin/pmtiles.exe when run on Windows)
  manifest.json                     what was fetched, versions and sha256 sums

Run on a machine with internet, then copy the store to the console. Run it on
the console itself when that is a Windows laptop, so the Windows build of the
pmtiles tool is fetched (the renderer pieces are the same on every platform):

    python3 tools/maps/vendor_map_assets.py            # fetch everything missing
    python3 tools/maps/vendor_map_assets.py --force    # fetch again even if present
    python3 tools/maps/vendor_map_assets.py --check    # only report what is there

Sizes: about 1 MB for MapLibre, 150 KB pmtiles, 450 KB basemaps, 6.5 MB fonts
and sprites, 17 MB for the pmtiles tool. Nothing here goes into the repos.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import stat
import sys
import tarfile
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))      # repo root, two up from tools/maps
if REPO not in sys.path:
    sys.path.insert(0, REPO)
from dpmg.mission import map_store  # noqa: E402

MAPLIBRE_VERSION = "5.15.0"
PMTILES_VERSION = "4.5.0"
BASEMAPS_VERSION = "5.7.2"
GO_PMTILES_VERSION = "1.31.2"

FILES = {
    "maplibre-gl.js": f"https://unpkg.com/maplibre-gl@{MAPLIBRE_VERSION}/dist/maplibre-gl.js",
    "maplibre-gl.css": f"https://unpkg.com/maplibre-gl@{MAPLIBRE_VERSION}/dist/maplibre-gl.css",
    "pmtiles.js": f"https://unpkg.com/pmtiles@{PMTILES_VERSION}/dist/pmtiles.js",
}
BASEMAPS_TGZ = (f"https://registry.npmjs.org/@protomaps/basemaps/-/"
                f"basemaps-{BASEMAPS_VERSION}.tgz")
ASSETS_ZIP = "https://github.com/protomaps/basemaps-assets/archive/refs/heads/main.zip"
# go-pmtiles ships a tar.gz for Linux and a zip for Windows, both x86_64
_GO_PMTILES_BASE = (f"https://github.com/protomaps/go-pmtiles/releases/download/"
                    f"v{GO_PMTILES_VERSION}/go-pmtiles_{GO_PMTILES_VERSION}_")
GO_PMTILES_LINUX = _GO_PMTILES_BASE + "Linux_x86_64.tar.gz"
GO_PMTILES_WINDOWS = _GO_PMTILES_BASE + "Windows_x86_64.zip"


def go_pmtiles_url(platform: str = None) -> str:
    """The go-pmtiles release archive for this platform."""
    return GO_PMTILES_WINDOWS if (platform or sys.platform) == "win32" else GO_PMTILES_LINUX


GO_PMTILES = go_pmtiles_url()


def assets_dir() -> str:
    return os.path.join(map_store.ensure_store(), map_store.ASSETS_DIR)


def bin_path() -> str:
    """<store>/bin/pmtiles, or pmtiles.exe on Windows."""
    return map_store.pmtiles_tool_path()


def manifest_path() -> str:
    return os.path.join(assets_dir(), "manifest.json")


def load_manifest() -> dict:
    try:
        with open(manifest_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"files": {}}


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "dpmg-mission-planner"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def _save(rel: str, data: bytes, manifest: dict, source: str, version: str):
    path = os.path.join(assets_dir(), rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    manifest["files"][rel] = {"source": source, "version": version,
                              "size_bytes": len(data), "sha256": _sha256(path)}
    print(f"  {rel}: {map_store.human_size(len(data))}")


def fetch_renderer(manifest: dict, force: bool):
    for rel, url in FILES.items():
        if not force and os.path.exists(os.path.join(assets_dir(), rel)):
            print(f"  {rel}: present")
            continue
        ver = MAPLIBRE_VERSION if rel.startswith("maplibre") else PMTILES_VERSION
        _save(rel, _fetch(url), manifest, url, ver)


def fetch_basemaps(manifest: dict, force: bool):
    """The style layer generator. The npm tarball's package.json names the
    browser module (its "module" or "main" entry); that one file is kept."""
    rel = "basemaps.js"
    if not force and os.path.exists(os.path.join(assets_dir(), rel)):
        print(f"  {rel}: present")
        return
    data = _fetch(BASEMAPS_TGZ)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        names = tf.getnames()
        pkg = json.load(tf.extractfile("package/package.json"))
        entry = None
        exp = pkg.get("exports")
        if isinstance(exp, dict):
            e = exp.get(".", exp)
            if isinstance(e, dict):
                entry = e.get("import") or e.get("default") or e.get("module")
                if isinstance(entry, dict):
                    entry = entry.get("default")
            elif isinstance(e, str):
                entry = e
        entry = entry or pkg.get("module") or pkg.get("main")
        if not entry:
            raise SystemExit("basemaps package.json names no module entry")
        member = "package/" + entry.lstrip("./")
        if member not in names:
            raise SystemExit(f"{member} not in the basemaps tarball: {names[:20]}")
        js = tf.extractfile(member).read()
    _save(rel, js, manifest, BASEMAPS_TGZ + "#" + member, BASEMAPS_VERSION)


def fetch_fonts_sprites(manifest: dict, force: bool):
    have_fonts = os.path.isdir(os.path.join(assets_dir(), "fonts"))
    have_sprites = os.path.isdir(os.path.join(assets_dir(), "sprites"))
    if not force and have_fonts and have_sprites:
        print("  fonts/, sprites/: present")
        return
    data = _fetch(ASSETS_ZIP)
    n = 0
    total = 0
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            parts = info.filename.split("/", 1)
            if len(parts) < 2 or info.is_dir():
                continue
            rel = parts[1]
            if not (rel.startswith("fonts/") or rel.startswith("sprites/")):
                continue
            path = os.path.join(assets_dir(), rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with zf.open(info) as src, open(path, "wb") as dst:
                shutil.copyfileobj(src, dst)
            n += 1
            total += info.file_size
    manifest["files"]["fonts+sprites"] = {"source": ASSETS_ZIP, "version": "main",
                                         "size_bytes": total, "count": n}
    print(f"  fonts/ and sprites/: {n} files, {map_store.human_size(total)}")


def extract_pmtiles_tool(data: bytes, path: str, platform: str = None) -> None:
    """Write the pmtiles binary out of a go-pmtiles release archive: the
    Windows zip holds pmtiles.exe, the Linux tar.gz holds pmtiles. On POSIX
    the file is made executable; Windows has no execute bit."""
    platform = platform or sys.platform
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if platform == "win32":
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            member = next((i for i in zf.infolist() if not i.is_dir()
                           and i.filename.lower().endswith("pmtiles.exe")), None)
            if member is None:
                raise SystemExit("no pmtiles.exe in the go-pmtiles zip")
            with zf.open(member) as src, open(path, "wb") as dst:
                shutil.copyfileobj(src, dst)
        return
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        member = next((m for m in tf.getmembers() if m.name.endswith("pmtiles")
                       and m.isfile()), None)
        if member is None:
            raise SystemExit("no pmtiles binary in the go-pmtiles archive")
        with tf.extractfile(member) as src, open(path, "wb") as dst:
            shutil.copyfileobj(src, dst)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP)


def fetch_pmtiles_tool(manifest: dict, force: bool):
    path = bin_path()
    rel = "bin/" + os.path.basename(path)
    if not force and os.path.exists(path):
        print(f"  {rel}: present")
        return
    url = go_pmtiles_url()
    extract_pmtiles_tool(_fetch(url), path)
    manifest["files"][rel] = {"source": url,
                              "version": GO_PMTILES_VERSION,
                              "size_bytes": os.path.getsize(path),
                              "sha256": _sha256(path)}
    print(f"  {rel}: {map_store.human_size(os.path.getsize(path))}")


REQUIRED = ("maplibre-gl.js", "maplibre-gl.css", "pmtiles.js", "basemaps.js",
            "fonts", "sprites")


def check() -> list:
    """Names of the required renderer pieces missing from the store."""
    d = assets_dir()
    return [r for r in REQUIRED if not os.path.exists(os.path.join(d, r))]


def main(argv=None):
    ap = argparse.ArgumentParser(description="vendor the offline map assets")
    ap.add_argument("--force", action="store_true", help="fetch again even if present")
    ap.add_argument("--check", action="store_true", help="only report what is there")
    a = ap.parse_args(argv)
    print(f"map store: {map_store.store_dir()}")
    if a.check:
        missing = check()
        print("missing: " + (", ".join(missing) if missing else "nothing"))
        return 1 if missing else 0
    manifest = load_manifest()
    fetch_renderer(manifest, a.force)
    fetch_basemaps(manifest, a.force)
    fetch_fonts_sprites(manifest, a.force)
    fetch_pmtiles_tool(manifest, a.force)
    with open(manifest_path(), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1)
    missing = check()
    print("missing after fetch: " + (", ".join(missing) if missing else "nothing"))
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
