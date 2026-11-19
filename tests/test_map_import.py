"""Tests for the map drop import: the format sniffer, every parser, the tile
file readers and the overlay helpers. Fixtures are in tests/data/map_import
(make_fixtures.py writes the binary ones).

    python3 -m pytest tests/test_map_import.py
"""

import importlib
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)          # the repo root, one up from tests/
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from dpmg.mission import map_import as mi      # noqa: E402
from dpmg.mission import map_element as me     # noqa: E402
from dpmg.mission import map_store             # noqa: E402
from dpmg.mission import mission_file as mf    # noqa: E402

DATA = os.path.join(HERE, "data", "map_import")


def fx(name) -> bytes:
    with open(os.path.join(DATA, name), "rb") as f:
        return f.read()


# sniffer

@pytest.mark.parametrize("name,data,want", [
    ("route.geojson", fx("route.geojson"), "geojson"),
    ("route.json", fx("route.geojson"), "geojson"),
    ("noext", fx("route.geojson"), "geojson"),
    ("plan.json", b'{"format": "dpmg-mission/1", "steps": []}', "mission"),
    ("areas.kml", fx("areas.kml"), "kml"),
    ("areas.xml", fx("areas.kml"), "kml"),
    ("areas.kmz", fx("areas.kmz"), "kmz"),
    ("areas.zip", fx("areas.kmz"), "kmz"),
    ("areas_shp.zip", fx("areas_shp.zip"), "shapefile"),
    ("track.gpx", fx("track.gpx"), "gpx"),
    ("track.txt", fx("track.gpx"), "gpx"),
    ("sites.csv", fx("sites.csv"), "csv"),
    ("sites_tab.txt", fx("sites_tab.txt"), "csv"),
    ("not_geo.csv", fx("not_geo.csv"), ""),
    ("tiny.pmtiles", fx("tiny.pmtiles"), "pmtiles"),
    ("renamed.bin", fx("tiny.pmtiles"), "pmtiles"),
    ("tiny.mbtiles", fx("tiny.mbtiles"), "mbtiles"),
    ("photo.jpg", b"\xff\xd8\xff\xe0" + b"\0" * 100, ""),
    ("empty.geojson", b"", "geojson"),
])
def test_sniff(name, data, want):
    assert mi.sniff(name, data) == want


def test_sniff_head_only_zip_falls_back_to_extension():
    # only the first bytes of a KMZ: a zip for sure, the extension says which
    head = fx("areas.kmz")[:64]
    assert mi.sniff("x.kmz", head) == "kmz"
    assert mi.sniff("x.zip", head) == "shapefile"


# GeoJSON

def test_geojson_everything():
    f = mi.parse("route.geojson", fx("route.geojson"))
    assert f["format"] == "geojson" and f["source"] == "route.geojson"
    assert [ln["name"] for ln in f["lines"]] == ["Route 1", "Short", ""]
    assert len(f["lines"][0]["coords"]) == 3
    assert [pg["name"] for pg in f["polygons"]] == ["Area A", "Two boxes", "Two boxes"]
    assert len(f["polygons"][0]["coords"]) == 3           # closing vertex and the hole dropped
    names = [p["name"] for p in f["points"]]
    assert names == ["Site 1", "Site 2", "", ""]
    assert f["points"][0]["radius_m"] == 800.0
    assert f["points"][1]["radius_m"] == 1500.0           # radius_km
    assert f["points"][1]["lon"] == -110.87               # altitude dropped
    assert "radius_m" not in f["points"][2]
    assert f["skipped"] == 1                              # the out-of-range point
    assert mi.summary(f) == "3 lines, 3 areas, 4 points (1 skipped)"


def test_geojson_bare_geometry_and_coordinate_list():
    f = mi.features_from_geojson({"type": "LineString", "coordinates": [[-111, 32], [-110, 33]]})
    assert len(f["lines"]) == 1 and f["lines"][0]["coords"] == [[-111.0, 32.0], [-110.0, 33.0]]
    f = mi.features_from_geojson(json.dumps([[-111, 32], [-110, 33], [-109, 34]]))
    assert len(f["lines"][0]["coords"]) == 3


def test_geojson_with_nothing_usable_is_an_error():
    with pytest.raises(mi.MapImportError):
        mi.parse("x.geojson", b'{"type": "FeatureCollection", "features": []}')
    with pytest.raises(mi.MapImportError):
        mi.parse("x.geojson", b"{not json")


# KML and KMZ

def _check_kml_set(f):
    assert [pg["name"] for pg in f["polygons"]] == ["Range A"]
    assert len(f["polygons"][0]["coords"]) == 4              # outer ring only
    assert [ln["name"] for ln in f["lines"]] == ["Ingress", "Flight"]
    assert f["lines"][0]["coords"][0] == [-111.0, 32.0]      # altitude dropped
    assert f["lines"][1]["coords"] == [[-111.1, 32.5], [-111.0, 32.6]]   # gx:Track
    assert [p["name"] for p in f["points"]] == ["Site 1", "Both"]
    assert f["skipped"] == 1


def test_kml():
    _check_kml_set(mi.parse("areas.kml", fx("areas.kml")))


def test_kmz():
    f = mi.parse("areas.kmz", fx("areas.kmz"))
    assert f["format"] == "kmz"
    _check_kml_set(f)


def test_kmz_without_kml_inside():
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("readme.txt", "x")
    with pytest.raises(mi.MapImportError):
        mi.features_from_kmz(buf.getvalue())


# GPX

def test_gpx_tracks_routes_waypoints():
    f = mi.parse("track.gpx", fx("track.gpx"))
    assert [ln["name"] for ln in f["lines"]] == ["Route", "Sortie 3"]
    assert len(f["lines"][1]["coords"]) == 4                 # two segments joined
    assert [p["name"] for p in f["points"]] == ["WP1", "WP2"]
    assert mi.longest_line(f)["name"] == "Sortie 3"


# CSV

def test_csv_header_detection_and_radius_units():
    f = mi.parse("sites.csv", fx("sites.csv"))
    assert [p["name"] for p in f["points"]] == ["Alpha", "Bravo", "Delta"]
    assert f["points"][0]["radius_m"] == 500.0               # 0.5 km
    assert "radius_m" not in f["points"][1]                  # blank radius
    assert f["points"][2]["radius_m"] == 2000.0
    assert f["skipped"] == 1                                 # Charlie's bad latitude


def test_csv_semicolon_and_tab():
    f = mi.parse("sites_semicolon.csv", fx("sites_semicolon.csv"))
    assert [p["name"] for p in f["points"]] == ["1", "2", "3"]
    assert f["points"][0]["lat"] == 33.40 and f["points"][0]["lon"] == -112.07
    f = mi.parse("sites_tab.txt", fx("sites_tab.txt"))
    assert f["points"] == [{"name": "Tower", "lat": 33.3, "lon": -111.7}]


def test_csv_without_lat_lon_columns():
    with pytest.raises(mi.MapImportError):
        mi.features_from_csv(fx("not_geo.csv"))
    assert mi.csv_columns(["Site", "LAT", "Long", "Radius (m)"]) == {
        "lat": 1, "lon": 2, "name": 0, "radius": None, "radius_factor": 1.0}
    assert mi.csv_columns(["name", "y", "x", "radius_nm"])["radius_factor"] == 1852.0


# shapefile

def test_shapefile_zip_polygons_points_and_names():
    pytest.importorskip("shapefile")
    f = mi.parse("areas_shp.zip", fx("areas_shp.zip"))
    assert f["format"] == "shapefile"
    assert sorted(pg["name"] for pg in f["polygons"]) == ["Box one", "Box with hole"]
    assert all(len(pg["coords"]) == 4 for pg in f["polygons"])   # the hole is not a polygon
    assert sorted(p["name"] for p in f["points"]) == ["P1", "P2"]


def test_shapefile_projected_is_refused():
    pytest.importorskip("shapefile")
    with pytest.raises(mi.MapImportError, match="projected"):
        mi.parse("projected_shp.zip", fx("projected_shp.zip"))


# the set

def test_path_from_features_longest_line_then_points():
    f = mi.parse("track.gpx", fx("track.gpx"))
    assert mi.path_from_features(f) == mi.longest_line(f)["coords"]
    g = mi.parse("sites_semicolon.csv", fx("sites_semicolon.csv"))
    assert mi.path_from_features(g) == [[-112.07, 33.4], [-112.07, 33.45], [-112.0, 33.5]]
    with pytest.raises(mi.MapImportError):
        mi.path_from_features(mi.empty_features())


def test_merge_bbox_and_summary():
    a = mi.parse("sites_tab.txt", fx("sites_tab.txt"))
    b = mi.parse("track.gpx", fx("track.gpx"))
    m = mi.merge_features([a, b])
    assert m["source"] == "sites_tab.txt, track.gpx" and m["format"] == "csv, gpx"
    assert mi.feature_count(m) == 5
    assert mi.bbox(m) == [-111.7, 32.0, -110.9, 33.3]
    assert mi.summary(mi.empty_features()) == "nothing usable"


def test_parse_refuses_tiles_plans_unknown_and_big(monkeypatch):
    with pytest.raises(mi.MapImportError, match="tile"):
        mi.parse("tiny.pmtiles", fx("tiny.pmtiles"))
    with pytest.raises(mi.MapImportError, match="Load"):
        mi.parse("plan.json", b'{"format": "dpmg-mission/1"}')
    with pytest.raises(mi.MapImportError, match="not a map data file"):
        mi.parse("photo.jpg", b"\xff\xd8\xff" + b"\0" * 50)
    monkeypatch.setattr(mi, "MAX_DATA_BYTES", 100)
    with pytest.raises(mi.MapImportError, match="limit"):
        mi.parse("route.geojson", fx("route.geojson"))


# tile files

def test_pmtiles_header():
    h = mi.pmtiles_header(fx("tiny.pmtiles"))
    assert h["tile_type"] == "vector" and h["minzoom"] == 0 and h["maxzoom"] == 12
    assert h["bbox"] == pytest.approx([-114.82, 31.33, -109.04, 37.0])
    assert h["center"] == pytest.approx([34.2, -111.9, 6])
    with pytest.raises(mi.MapImportError):
        mi.pmtiles_header(b"PMTiles\x02" + b"\0" * 200)
    with pytest.raises(mi.MapImportError):
        mi.pmtiles_header(b"nope")


def test_mbtiles_info():
    info = mi.mbtiles_info(os.path.join(DATA, "tiny.mbtiles"))
    assert info == {"tile_type": "vector", "minzoom": 0, "maxzoom": 10,
                    "bbox": [-114.82, 31.33, -109.04, 37.0]}
    with pytest.raises(mi.MapImportError):
        mi.mbtiles_info(os.path.join(DATA, "tiny.pmtiles"))


def test_area_name_for():
    assert mi.area_name_for("My Area (v2).pmtiles") == "My-Area-v2"
    assert mi.area_name_for("/tmp/x/phoenix.mbtiles") == "phoenix"


# overlays in the plan

def test_overlay_round_trip_through_the_plan_file():
    plan = mf.new_mission("t") if hasattr(mf, "new_mission") else \
        {"format": mf.FORMAT, "name": "t", "radios": [], "steps": [], "map": {}}
    f = mi.parse("route.geojson", fx("route.geojson"))
    rec = me.add_overlay(plan, "dropped", f, source="route.geojson")
    assert rec["id"].startswith("ov-") and rec["color"] == me.OVERLAY_COLORS[0]
    assert me.overlay_count(rec) == mi.feature_count(f) == 10
    assert rec["points"][0]["radius_m"] == 800.0
    rec2 = me.add_overlay(plan, "second", mi.parse("track.gpx", fx("track.gpx")))
    assert rec2["color"] == me.OVERLAY_COLORS[1]
    back = mf.loads(json.loads(mf.dumps(plan)))
    assert [o["name"] for o in me.overlays(back)] == ["dropped", "second"]
    assert me.remove_overlay(back, rec["id"]) is True
    assert me.remove_overlay(back, "ov-nope") is False
    assert [o["name"] for o in me.overlays(back)] == ["second"]
    me.remove_overlay(back, rec2["id"])
    assert "overlays" not in back["map"]
    # a plan without the key reads as no overlays
    assert me.overlays({"map": {}}) == [] and me.overlays({}) == []


def test_path_from_file_reads_the_other_formats_too():
    pts = me.path_from_file("areas.kmz", fx("areas.kmz"))
    assert pts == [[-111.0, 32.0], [-110.9, 32.1], [-110.8, 32.2]]       # the longest line
    pts = me.path_from_file("sites_semicolon.csv", fx("sites_semicolon.csv"))
    assert len(pts) == 3
    with pytest.raises(ValueError):
        me.path_from_file("photo.jpg", b"\xff\xd8\xff" + b"\0" * 50)


# tile type in the store

def test_store_records_raster_tile_type(tmp_path, monkeypatch):
    monkeypatch.setenv("DPMG_MAP_STORE", str(tmp_path / "maps"))
    importlib.reload(map_store)
    map_store.ensure_store()
    map_store.add_area("vec", [-1, -1, 1, 1], size_bytes=10)
    map_store.add_area("ras", [-1, -1, 1, 1], size_bytes=10, tile_type="png")
    idx = map_store.load_index()
    assert "tile_type" not in map_store.get_area("vec", idx)
    assert map_store.get_area("ras", idx)["tile_type"] == "png"
    kinds = {e["name"]: e["tile_type"] for e in map_store.entries(idx)}
    assert kinds == {"vec": "vector", "ras": "png"}
