#!/usr/bin/env python3
"""Test position player for the planner's location triggers.

Sends a position along a path on the same multicast feed the GUI already
listens to (acft_pos_multicast), so the planner's triggers see a real feed
while the simulated G40 has no GPS, or on a laptop with no aircraft feed.
Nothing in the planner is special-cased for it. The heading is the direction
of the leg being flown and the speed is --speed, so the GUI shows heading and
speed as it would from the aircraft. Run it from the repo root:

    python3 tools/sim_position.py --plan                    # the autosaved plan's flight path
    python3 tools/sim_position.py --geojson path.geojson    # a LineString / Feature(Collection)
    python3 tools/sim_position.py --points "32.2,-110.97 32.3,-110.9"   # lat,lon pairs
    options: --speed 60 (m/s), --rate 1 (fixes per second), --loop, --hold 10
             (seconds to sit at each end), --group and --port (default: the saved
             feed settings), --format json|nmea, --iface 127.0.0.1 (this host; the
             planner's iface must match), --alt 1500, --time-offset 0,
             --once (send the first point once and exit)
    the NMEA burst: --format nmea sends GGA, RMC, VTG, GSA and GSV each tick, in
             one datagram; --drop VTG (or --drop RMC,VTG) leaves sentences out;
             --no-heading sends no course at all (JSON without the key, NMEA with
             blank courses); --aux-every 5 sends GSA and GSV on every fifth tick
             only; --split sends each sentence in its own datagram

Ctrl-C stops it. It never sends anything but positions.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)                        # the repo root, one up from tools
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from dpmg.mission import map_element as me          # noqa: E402
from dpmg.mission import mission_file as mf         # noqa: E402
from dpmg.mission import mission_tab                # noqa: E402  (AUTOSAVE_PATH)
from dpmg.link import feed_settings                 # noqa: E402

# group, port and format default to the saved feed settings (the values the
# GUI listens on), so a player started with no options lands where the planner
# is listening; the interface stays 127.0.0.1 for a planner on this host
_FEED = feed_settings.load()


def load_path(args) -> list:
    """[lon, lat] pairs from the chosen source."""
    if args.points:
        out = []
        for tok in args.points.replace(";", " ").split():
            lat, lon = tok.split(",")
            out.append([float(lon), float(lat)])
        return out
    if args.geojson:
        with open(args.geojson, "r", encoding="utf-8") as f:
            return me.path_from_geojson(f.read())
    m = mf.load(args.plan_file or mission_tab.AUTOSAVE_PATH)
    pts = me.flight_path(m)
    if len(pts) < 2:
        raise SystemExit("the plan has no flight path with two or more points; draw one "
                         "on the map or pass --geojson / --points")
    return pts


def interpolate(path, speed_mps, rate_hz):
    """Yield (lon, lat, heading_deg) along the path at speed, rate fixes per
    second; the heading is the bearing of the leg being flown."""
    from dpmg.link.feed_sender import interpolate_heading
    yield from interpolate_heading(path, speed_mps, rate_hz)


def main(argv=None):
    ap = argparse.ArgumentParser(description="play a platform position on the multicast feed")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--plan", action="store_true", help="the autosaved plan's flight path (default)")
    src.add_argument("--geojson", help="GeoJSON file with the path")
    src.add_argument("--points", help="lat,lon pairs separated by spaces")
    ap.add_argument("--plan-file", default=None, help="a plan file instead of the autosave")
    ap.add_argument("--speed", type=float, default=60.0, help="metres per second")
    ap.add_argument("--rate", type=float, default=1.0, help="fixes per second")
    ap.add_argument("--hold", type=float, default=5.0, help="seconds to sit at each end")
    ap.add_argument("--loop", action="store_true", help="go back and forth until stopped")
    ap.add_argument("--once", action="store_true", help="send the first point once and exit")
    ap.add_argument("--alt", type=float, default=1500.0)
    ap.add_argument("--aircraft", default=None,
                    help="take group, port and format from this saved aircraft's feed "
                         "(default: the first aircraft in the saved settings)")
    ap.add_argument("--group", default=None,
                    help="multicast group (default: the saved feed setting)")
    ap.add_argument("--port", type=int, default=None,
                    help="UDP port (default: the saved feed setting)")
    ap.add_argument("--format", choices=["json", "nmea"], default=None,
                    help="datagram form: JSON object or NMEA GGA (default follows the saved "
                         "feed setting; auto gives json)")
    ap.add_argument("--time-offset", type=float, default=0.0,
                    help="seconds added to the time stamp in each datagram, to try a feed "
                         "whose clock is off against the console")
    ap.add_argument("--iface", default="127.0.0.1",
                    help="send on this interface: 127.0.0.1 reaches a planner on this host "
                         "(set its iface to 127.0.0.1 too); the host's own IP for the network")
    ap.add_argument("--drop", default="",
                    help="NMEA sentences to leave out of each burst, as GGA,RMC,VTG,GSA,GSV "
                         "(for example --drop VTG shows the GUI without a VTG heading)")
    ap.add_argument("--no-heading", action="store_true",
                    help="send no course at all: JSON without a heading key, NMEA with blank "
                         "RMC and VTG courses")
    ap.add_argument("--aux-every", type=int, default=1,
                    help="send GSA and GSV on every nth tick only (default every tick)")
    ap.add_argument("--split", action="store_true",
                    help="send each NMEA sentence in its own datagram")
    a = ap.parse_args(argv)
    feed = feed_settings.find_aircraft(_FEED, a.aircraft) if a.aircraft else _FEED["aircraft"][0]
    if feed is None:
        print(f"no saved aircraft named {a.aircraft!r}; the list has: "
              + ", ".join(e["name"] for e in _FEED["aircraft"]), file=sys.stderr)
        return 2
    a.group = a.group or feed["group"]
    a.port = a.port or int(feed["port"])
    a.format = a.format or ("nmea" if feed["format"] == "nmea" else "json")

    path = load_path(a)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(a.iface))

    from dpmg.link.feed_sender import datagrams

    tick = {"n": 0}

    def send(lon, lat, heading=None, speed=None):
        # each datagram carries a time stamp, as the aircraft's would, so the
        # feed-time check in the GUI has something to read
        tick["n"] += 1
        aux = (tick["n"] % max(1, a.aux_every)) == 0
        for dg in datagrams(lon, lat, a.alt, a.format, time.time() + a.time_offset,
                            None if a.no_heading else heading, speed,
                            drop=a.drop, aux=aux, split=a.split):
            s.sendto(dg, (a.group, a.port))

    burst = "json object" if a.format == "json" else (
        "NMEA " + ",".join(k for k in ("GGA", "RMC", "VTG", "GSA", "GSV")
                           if k not in a.drop.upper()))
    print(f"path: {len(path)} points, {a.speed:g} m/s, {a.rate:g} Hz to {a.group}:{a.port} "
          f"as {burst}" + (" without heading" if a.no_heading else ""), flush=True)
    if a.once:
        send(path[0][0], path[0][1], None, 0.0)
        print(f"sent {path[0][1]:.5f} {path[0][0]:.5f} once", flush=True)
        return 0
    period = 1.0 / a.rate
    forward = True
    try:
        while True:
            pts = path if forward else list(reversed(path))
            n = 0
            hdg = None
            for lon, lat, hdg in interpolate(pts, a.speed, a.rate):
                send(lon, lat, hdg, a.speed)
                n += 1
                if n % int(max(1, 10 * a.rate)) == 0:
                    print(f"  at {lat:.5f} {lon:.5f} hdg {hdg:03.0f}", flush=True)
                time.sleep(period)
            end = time.time() + a.hold
            while time.time() < end:
                send(pts[-1][0], pts[-1][1], hdg, 0.0)
                time.sleep(period)
            if not a.loop:
                print("end of path", flush=True)
                return 0
            forward = not forward
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
