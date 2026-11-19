"""Tests for the plan-to-map helpers in map_element.py (no browser).

    python3 tests/test_map_element.py
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)          # repo root
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from dpmg.mission import map_element as me              # noqa: E402
from dpmg.mission import map_store                      # noqa: E402

_passed = 0
_failed = 0


def check(cond, msg):
    global _passed, _failed
    if cond:
        _passed += 1
    else:
        _failed += 1
        print(f"  FAIL: {msg}")


def plan():
    return {"format": "dpmg-mission/1", "name": "t", "map": {},
            "radios": [{"id": "G40-SIM", "type": "g40"}, {"id": "X40-SIM", "type": "x40"}],
            "steps": [
                {"id": "s1", "radio": "G40-SIM", "technique": "jammer.barrage",
                 "radiates": True, "params": {}, "schedule": {"start_s": 0, "stop_s": 30},
                 "location": {"start": {"lat": 32.22, "lon": -110.97, "radius_m": 500},
                              "stop": {"polygon": [[-110.95, 32.30], [-110.90, 32.30],
                                                   [-110.90, 32.34]]}}},
                {"id": "s2", "radio": "G40-SIM", "technique": "tools.tone",
                 "radiates": True, "params": {}, "schedule": {"start_s": 40, "stop_s": 50}},
                {"id": "s3", "radio": "G40-SIM", "technique": "tools.tone",
                 "radiates": True, "params": {}, "schedule": {"start_s": 60, "stop_s": 70},
                 "location": {"start": {"lat": 999, "lon": 0, "radius_m": 5}}},
            ]}


def test_fences_and_markers():
    f = me.step_fences(plan())
    check([x["id"] for x in f] == ["s1:start", "s1:stop"],
          f"one fence per valid geofence, the bad one (s3) skipped: {[x['id'] for x in f]}")
    g40 = me.RADIO_COLORS[0]
    check(f[0]["kind"] == "point" and f[0]["radius_m"] == 500 and f[0]["color"] == g40
          and f[0]["radio"] == "G40-SIM" and f[0]["role"] == "start",
          f"start circle in the radio's colour, radio named: {f[0]}")
    check(f[1]["kind"] == "polygon" and len(f[1]["polygon"]) == 3 and f[1]["color"] == g40
          and f[1]["role"] == "stop", "stop polygon in the same radio colour, role stop")
    m = me.step_markers(plan())
    check([x["id"] for x in m] == ["s1:start", "s1:stop"], "markers follow the fences")
    check(abs(m[0]["lat"] - 32.22) < 1e-9 and abs(m[1]["lon"] + 110.9167) < 1e-3,
          f"marker at the point, polygon marker at the vertex mean: {m}")
    check(m[0]["label"] == "G40-SIM: jammer.barrage start", f"marker label names the radio: {m[0]['label']}")
    check(m[0]["fill"] is True and m[1]["fill"] is False, "start marker filled, stop marker hollow")
    check(m[0]["color"] == g40 and m[1]["color"] == g40, "markers in the radio's colour")


def two_radio_plan():
    p = plan()
    p["steps"].append({"id": "x1", "radio": "X40-SIM", "technique": "tools.tone",
                       "radiates": True, "params": {}, "schedule": {"start_s": 0, "stop_s": 10},
                       "trigger": {"mode": "while_inside", "confirm": "ask"},
                       "location": {"start": {"lat": 32.5, "lon": -111.2, "radius_m": 800}}})
    p["steps"].append({"id": "u1", "radio": "B200MINI-1", "technique": "tools.tone",
                       "radiates": True, "params": {}, "schedule": {"start_s": 0, "stop_s": 10},
                       "location": {"start": {"lat": 32.6, "lon": -111.3, "radius_m": 100}}})
    return p


def test_radio_colours_follow_the_radios_list():
    p = two_radio_plan()
    cols = me.radio_colors(p)
    check(cols["G40-SIM"] == me.RADIO_COLORS[0] and cols["X40-SIM"] == me.RADIO_COLORS[1],
          f"colour by position in the radios list: {cols}")
    check(cols["B200MINI-1"] == me.UNPLANNED_COLOR,
          "a radio with steps but no entry in the radios list is grey")
    check(me.radio_color(p, "nobody") == me.UNPLANNED_COLOR, "an unknown radio is grey")
    # four radios: four different colours, none the grey, all distinct from the path
    p["radios"] += [{"id": "R3"}, {"id": "R4"}]
    four = [me.radio_color(p, r) for r in ("G40-SIM", "X40-SIM", "R3", "R4")]
    check(len(set(four)) == 4 and me.UNPLANNED_COLOR not in four and me.PATH_COLOR not in four,
          f"four radios, four distinct colours apart from grey and the path: {four}")
    check(len(set(me.RADIO_COLORS)) == len(me.RADIO_COLORS), "the palette has no repeats")
    # the palette stays the same when a radio is added at the end
    p["radios"].append({"id": "R5"})
    check([me.radio_color(p, r) for r in ("G40-SIM", "X40-SIM", "R3", "R4")] == four,
          "adding a radio at the end recolours nobody")
    # stop areas carry the same colour as start areas: the role is in the
    # line style, not the colour
    f = me.step_fences(p)
    by_radio = {}
    for x in f:
        by_radio.setdefault(x["radio"], set()).add(x["color"])
    check(all(len(v) == 1 for v in by_radio.values()), f"one colour per radio over all its areas: {by_radio}")


def test_fences_filter_by_radio():
    p = two_radio_plan()
    allf = me.step_fences(p)
    check(sorted({x["radio"] for x in allf}) == ["B200MINI-1", "G40-SIM", "X40-SIM"],
          "no filter: every radio's areas")
    only_x = me.step_fences(p, radios={"X40-SIM"})
    check([x["id"] for x in only_x] == ["x1:start"], f"filtered to the X40: {[x['id'] for x in only_x]}")
    check(only_x[0]["label"].startswith("X40-SIM: tools.tone (while inside, p"),
          f"location task label leads with the radio: {only_x[0]['label']}")
    check(me.step_fences(p, radios=set()) == [], "an empty set draws nothing")
    check(me.step_fences(p, radios={"nobody"}) == [], "a radio with no steps draws nothing")
    mk = me.step_markers(p, radios={"G40-SIM", "X40-SIM"}, selected="X40-SIM")
    check([m["id"] for m in mk] == ["s1:start", "s1:stop", "x1:start"], "markers filter the same way")
    check([m["selected"] for m in mk] == [False, False, True], "the selected radio's markers are marked")
    check(all(x["selected"] is False for x in me.step_fences(p, radios=None, selected=None)),
          "nothing marked selected without a selection")


def test_shown_radios_and_show_text():
    radios = ["G40-SIM", "X40-SIM"]
    check(me.shown_radios({"radio": None, "show": me.SHOW_SELECTED}, radios) is None,
          "selected only with nothing selected shows everything")
    check(me.shown_radios({"radio": "X40-SIM", "show": me.SHOW_SELECTED}, radios) == {"X40-SIM"},
          "selected only follows the selected radio")
    check(me.shown_radios({"radio": "X40-SIM", "show": me.SHOW_ALL}, radios) is None,
          "all ignores the selection")
    check(me.shown_radios({"radio": "X40-SIM", "show": ["G40-SIM"]}, radios) == {"G40-SIM"},
          "a list is the set, whatever is selected")
    check(me.shown_radios({"radio": None, "show": []}, radios) == set(), "an empty list is nothing")
    check(me.shown_radios({}, radios) is None, "an empty view shows everything")
    p = two_radio_plan()
    t = me.show_text(p, {"radio": "X40-SIM", "show": me.SHOW_SELECTED}, radios)
    check("X40-SIM: 1 step(s), 1 area(s)" in t and "G40-SIM" not in t.split(".")[1],
          f"selected only names the one radio: {t}")
    check("Triggers watch every radio" in t and "flight path and overlays always show" in t,
          "the trigger rule and the path rule are in the text")
    p2 = plan()                             # X40 in the plan, no steps on it
    t = me.show_text(p2, {"radio": "X40-SIM", "show": me.SHOW_SELECTED}, radios)
    check("X40-SIM: no steps in this plan" in t, f"a radio with no steps says so: {t}")
    t = me.show_text(p2, {"radio": None, "show": me.SHOW_ALL}, radios)
    check(t.startswith("map shows every radio") and "G40-SIM: 3 step(s), 2 area(s)" in t
          and "X40-SIM: no steps in this plan" in t, f"all lists every radio: {t}")
    t = me.show_text(p2, {"radio": "G40-SIM", "show": []}, radios)
    check("no radio ticked" in t, f"an empty set says so: {t}")


def test_flight_path_round_trip():
    p = plan()
    check(me.flight_path(p) == [], "no path yet")
    n = me.set_flight_path(p, [[-111.0, 32.0], [-110.9, 32.1], ["x", 1], [-110.8, 32.2]])
    check(n == 3 and me.flight_path(p) == [[-111.0, 32.0], [-110.9, 32.1], [-110.8, 32.2]],
          f"bad point dropped: {me.flight_path(p)}")
    check(p["map"]["flight_path_source"] == "drawn", "source noted")
    me.set_flight_path(p, [])
    check("flight_path" not in p["map"], "empty clears it")


def test_path_from_geojson():
    ls = {"type": "LineString", "coordinates": [[-111, 32], [-110, 33]]}
    check(me.path_from_geojson(ls) == [[-111.0, 32.0], [-110.0, 33.0]], "LineString")
    feat = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-111, 32]}},
        {"type": "Feature", "geometry": {"type": "MultiLineString",
                                         "coordinates": [[[-110, 33], [-109, 34]]]}}]}
    check(me.path_from_geojson(json.dumps(feat)) == [[-111.0, 32.0], [-110.0, 33.0], [-109.0, 34.0]],
          "FeatureCollection with a point and a multi line, in order")
    try:
        me.path_from_geojson({"type": "Point", "coordinates": [1, 2]})
        check(False, "one point is not a path")
    except ValueError:
        check(True, "one point raises")
    try:
        me.path_from_geojson("{not json")
        check(False, "bad json raises")
    except ValueError:
        check(True, "bad json raises")


GPX = """<?xml version="1.0"?>
<gpx version="1.1" creator="t" xmlns="http://www.topografix.com/GPX/1/1">
 <wpt lat="33.0" lon="-112.0"><name>a</name></wpt>
 <trk><name>t</name><trkseg>
  <trkpt lat="33.45" lon="-112.10"><ele>400</ele></trkpt>
  <trkpt lat="33.46" lon="-112.05"></trkpt>
  <trkpt lat="999" lon="-112.00"></trkpt>
 </trkseg></trk>
</gpx>"""

KML = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
 <Placemark><name>route</name><LineString><coordinates>
   -112.10,33.45,400 -112.05,33.46,410
   -112.00,33.47
 </coordinates></LineString></Placemark>
 <Placemark><Point><coordinates>-111.9,33.5,0</coordinates></Point></Placemark>
</Document></kml>"""


def test_path_from_gpx_and_kml():
    check(me.path_from_file("route.gpx", GPX) == [[-112.10, 33.45], [-112.05, 33.46]],
          "GPX track points, the bad one dropped, waypoint ignored")
    check(me.path_from_file("route.kml", KML) == [[-112.10, 33.45], [-112.05, 33.46], [-112.00, 33.47]],
          "KML LineString coordinates with and without altitude")
    only_pts = KML.replace("<LineString><coordinates>", "<LineString><coordinates></coordinates></LineString><x>") \
                  .replace("</coordinates></LineString>", "</x>", 1)
    try:
        me.path_from_file("x.kml", only_pts)
        check(False, "one point is no path")
    except ValueError:
        check(True, "one point is no path")
    try:
        me.path_from_file("x.gpx", "<gpx><trk><trkseg><trkpt lat='1' lon='2'/></trkseg></trk></gpx>")
        check(False, "one trkpt raises")
    except ValueError:
        check(True, "one trkpt raises")
    try:
        me.path_from_file("x.kml", "<kml><bad")
        check(False, "bad xml raises ValueError")
    except ValueError:
        check(True, "bad xml raises ValueError")
    check(me.path_from_file("r.geojson", json.dumps({"type": "LineString", "coordinates": [[-111, 32], [-110, 33]]}))
          == [[-111.0, 32.0], [-110.0, 33.0]], "GeoJSON by extension")
    check(me.path_from_file("", b'{"type": "LineString", "coordinates": [[-111, 32], [-110, 33]]}')
          == [[-111.0, 32.0], [-110.0, 33.0]], "bytes with no extension are tried as GeoJSON")


def test_map_config_from_store(tmp=None):
    import tempfile
    d = tempfile.mkdtemp()
    os.environ["DPMG_MAP_STORE"] = d
    try:
        cfg = me.map_config(center=(34.2, -111.5), zoom=6)
        check("baseUrl" not in cfg and cfg["areas"] == [] and cfg["center"] == [-111.5, 34.2],
              f"empty store: no base, no areas, centre as [lon, lat]: {cfg}")
        map_store.ensure_store()
        open(os.path.join(d, map_store.BASE_FILE), "wb").write(b"x" * 10)
        map_store.add_base(maxzoom=7, size_bytes=10)
        os.makedirs(os.path.join(d, "areas"), exist_ok=True)
        open(os.path.join(d, "areas", "arizona.pmtiles"), "wb").write(b"x" * 20)
        map_store.add_area("arizona", [-114.82, 31.33, -109.04, 37.0], maxzoom=14, size_bytes=20)
        map_store.add_area("ghost", [0, 0, 1, 1], maxzoom=14, size_bytes=5)   # no file
        cfg = me.map_config()
        check(cfg["baseUrl"] == "/maps/world-base.pmtiles" and cfg["baseMaxzoom"] == 7, "base url")
        check([a["name"] for a in cfg["areas"]] == ["arizona"]
              and cfg["areas"][0]["url"] == "/maps/areas/arizona.pmtiles"
              and cfg["areas"][0]["maxzoom"] == 14, f"present areas only: {cfg['areas']}")
        check(me.assets_missing() == ["maplibre-gl.js", "maplibre-gl.css", "pmtiles.js",
                                      "basemaps.js", "fonts", "sprites"], "assets missing list")
        html = me.head_html()
        check("maplibre-gl.js" in html and "mission_map.js" in html
              and "protomapsBasemaps" in html, "head html loads the local pieces")
    finally:
        os.environ.pop("DPMG_MAP_STORE", None)


def test_position_marker():
    from dpmg.mission import triggers as tg
    m = me.position_marker(tg.Fix(32.1, -110.2, t=0.0), valid=False)
    check(m["lat"] == 32.1 and m["valid"] is False and "stale" in m["label"], "stale marker")
    check(me.position_marker(None) is None, "no fix, no marker")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{_passed} passed, {_failed} failed, {len(tests)} test functions")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
