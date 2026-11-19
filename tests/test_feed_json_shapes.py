"""
test_feed_json_shapes.py: the aircraft feed's JSON reader across the shapes a
platform may send.

Covers: capitalised keys, a position in a sub-object (one and two levels down),
pretty printed JSON, an array, several objects in one datagram, altitude in
feet, sub-objects for another point (home, target) skipped, an out of range
latitude refused, a heading and speed message with no position merged into the
held fix, the json and nmea format settings each ignoring the other, and NMEA
unchanged. Stdlib only; exit 0 = all passed.
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dpmg.link.acft_pos_multicast import FixCombiner, parse_auto, parser_for

checks = []


def chk(name, ok, detail=""):
    checks.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")


def approx(a, b, tol=1e-3):
    return a is not None and abs(a - b) <= tol


def fix(text, fmt="auto", combiner=None, t=None):
    data = text.encode() if isinstance(text, str) else text
    return parser_for(fmt)(data, combiner or FixCombiner(), time.monotonic() if t is None else t)


def at(p, lat=34.05, lon=-117.6):
    return p is not None and approx(p.lat_deg, lat) and approx(p.lon_deg, lon)


def main() -> int:
    p = fix('{"Latitude": 34.05, "Longitude": -117.6, "Altitude": 1200, "Heading": 90}')
    chk("capitalised keys", at(p) and approx(p.alt_m, 1200) and approx(p.heading_deg, 90))

    p = fix('{"position": {"lat": 34.05, "lon": -117.6}, "heading": 90}')
    chk("position in a sub-object", at(p) and approx(p.heading_deg, 90))

    p = fix('{"nav": {"position": {"Lat": 34.05, "Lon": -117.6, "alt_ft": 4000},'
            ' "attitude": {"heading": 45}}}')
    chk("two levels down, feet, heading from a sibling", at(p) and approx(p.alt_m, 1219.2, 0.1)
        and approx(p.heading_deg, 45))

    p = fix(json.dumps({"lat": 34.05, "lon": -117.6, "alt": 1200}, indent=2))
    chk("pretty printed over several lines", at(p) and approx(p.alt_m, 1200))

    p = fix('[{"lat": 34.05, "lon": -117.6}]')
    chk("array of one object", at(p))

    p = fix('{"lat": 34.05, "lon": -117.6}\n{"heading": 10, "speed_kt": 120}')
    chk("two objects in one datagram", at(p) and approx(p.heading_deg, 10)
        and approx(p.speed_mps, 120 * 0.514444, 0.01))

    p = fix('{"lat": 34.05, "lon": -117.6} {"heading": 20}')
    chk("two objects back to back", at(p) and approx(p.heading_deg, 20))

    p = fix('{"lat": 34.05, "lon": -117.6, "alt_ft": 4000}')
    chk("altitude in feet to metres", at(p) and approx(p.alt_m, 1219.2, 0.1))

    p = fix('{"lat": 34.05, "lon": -117.6, "alt": 1500, "alt_ft": 4000}')
    chk("metres win over feet", at(p) and approx(p.alt_m, 1500))

    p = fix('{"home": {"lat": 1, "lon": 2}, "target": {"lat": 3, "lon": 4},'
            ' "gps": {"lat": 34.05, "lon": -117.6}}')
    chk("home and target skipped, gps read", at(p))

    chk("only a home point gives no fix", fix('{"home": {"lat": 1, "lon": 2}}') is None)

    p = fix('{"lat": 34.05, "lon": -117.6, "gps": {"lat": 10, "lon": 20}}')
    chk("the top level wins over a sub-object", at(p))

    chk("latitude out of range refused", fix('{"lat": 340.5, "lon": -117.6}') is None)

    p = fix('{"lat": "34.05", "lon": "-117.6", "speed_kts": "140"}')
    chk("numbers as text", at(p) and approx(p.speed_mps, 140 * 0.514444, 0.01))

    c, t = FixCombiner(), time.monotonic()
    fix('{"lat": 34.05, "lon": -117.6}', combiner=c, t=t)
    p = fix('{"heading": 200, "speed_kt": 150}', combiner=c, t=t + 0.2)
    chk("heading message merges into the held fix", at(p) and approx(p.heading_deg, 200))
    chk("a heading message alone gives no fix", fix('{"heading": 200}') is None)

    chk("format nmea ignores JSON", fix('{"lat": 34.05, "lon": -117.6}', fmt="nmea") is None)
    gga = "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47"
    chk("format json ignores NMEA", fix(gga, fmt="json") is None)
    p = parse_auto(gga.encode())
    chk("NMEA unchanged on auto", p is not None and approx(p.lat_deg, 48.1173, 1e-4)
        and approx(p.alt_m, 545.4, 0.1))
    p = fix(gga + "\n" + '{"heading": 33}')
    chk("NMEA and one-line JSON mixed in a datagram", p is not None and approx(p.heading_deg, 33))

    chk("not JSON and not NMEA gives no fix", fix("hello") is None and fix("{broken") is None)

    print(f"\nresult: {sum(checks)}/{len(checks)} checks passed")
    return 0 if all(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
