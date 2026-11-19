"""Build the offline world base map on the x86 and put it in the map store.

Runs on the x86 (which has internet). The radios and the G40 never need a
connection: this writes the base map once and it is copied to the console store.

It pulls a worldwide vector base from the current Protomaps daily planet build
with the go-pmtiles CLI, capped at a chosen max zoom (7 by default, a few hundred
MB for the whole world), and records it in the store index through map_store.

    python3 tools/maps/build_base_map.py --dry-run          # report the size only
    python3 tools/maps/build_base_map.py                     # extract and record the base
    python3 tools/maps/build_base_map.py --maxzoom 8         # more baked-in detail, bigger
    python3 tools/maps/build_base_map.py --date 20261024     # pin a build date

The pmtiles binary is found on PATH, or point --pmtiles at it. Get it from
https://github.com/protomaps/go-pmtiles/releases if it is not installed.

On-demand area downloads use the same tool with a bounding box; see
download_area() in dpmg/mission/base_map.py, which the tab calls. The
renderer assets (style, glyphs, sprites) are handled separately by the map
element setup, not by this script.
"""

from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))      # repo root, two up from tools/maps
if REPO not in sys.path:
    sys.path.insert(0, REPO)
from dpmg.mission import map_store  # noqa: E402
from dpmg.mission.base_map import build_base, download_area  # noqa: E402,F401


def main(argv=None):
    ap = argparse.ArgumentParser(description="build the offline world base map")
    ap.add_argument("--maxzoom", type=int, default=map_store.BASE_MAXZOOM)
    ap.add_argument("--date", default=None, help="build date YYYYMMDD")
    ap.add_argument("--pmtiles", default=None, help="path to the pmtiles binary")
    ap.add_argument("--dry-run", action="store_true",
                    help="report the size without downloading")
    a = ap.parse_args(argv)
    build_base(maxzoom=a.maxzoom, date=a.date, pmtiles=a.pmtiles,
               dry_run=a.dry_run)


if __name__ == "__main__":
    main()
