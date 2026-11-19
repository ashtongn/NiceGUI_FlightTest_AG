"""Tests for the offline map tile store. Every test points DPMG_MAP_STORE at a
temp directory, so nothing here touches the real ~/.dpmg/maps."""

import importlib
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)          # repo root
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from dpmg.mission import map_store   # noqa: E402


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("DPMG_MAP_STORE", str(tmp_path / "maps"))
    importlib.reload(map_store)
    map_store.ensure_store()
    return tmp_path / "maps"


def _write(path, nbytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"\0" * nbytes)


def test_store_dir_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("DPMG_MAP_STORE", str(tmp_path / "x"))
    importlib.reload(map_store)
    assert map_store.store_dir() == str(tmp_path / "x")


def test_blank_index_when_missing(store):
    idx = map_store.load_index()
    assert idx["base"] is None
    assert idx["areas"] == []
    assert idx["budget_bytes"] == map_store.DEFAULT_BUDGET_BYTES


def test_corrupt_index_reads_blank(store):
    with open(map_store.index_path(), "w") as f:
        f.write("{ not json")
    idx = map_store.load_index()
    assert idx["base"] is None and idx["areas"] == []


def test_add_base_records_size_and_present(store):
    _write(store / map_store.BASE_FILE, 1000)
    map_store.add_base(maxzoom=7, source="protomaps 20261024")
    assert map_store.base_present()
    idx = map_store.load_index()
    assert idx["base"]["maxzoom"] == 7
    assert idx["base"]["size_bytes"] == 1000
    assert map_store.total_size() == 1000


def test_base_not_present_without_file(store):
    map_store.add_base(maxzoom=7, size_bytes=5)
    # index has a base row but the file was never written
    assert map_store.base_present() is False


def test_add_and_remove_area(store):
    f = store / map_store.area_file("Range A")
    _write(f, 2048)
    map_store.add_area("Range A", [4.0, 51.0, 5.0, 52.0], maxzoom=14,
                       source="protomaps")
    idx = map_store.load_index()
    assert len(idx["areas"]) == 1
    assert idx["areas"][0]["name"] == "Range-A"
    assert map_store.total_size() == 2048
    map_store.remove_area("Range A")
    assert map_store.load_index()["areas"] == []
    assert not os.path.exists(f)


def test_add_area_replaces_same_name(store):
    _write(store / map_store.area_file("site"), 100)
    map_store.add_area("site", [0, 0, 1, 1], size_bytes=100)
    _write(store / map_store.area_file("site"), 200)
    map_store.add_area("site", [0, 0, 2, 2], size_bytes=200)
    idx = map_store.load_index()
    assert len(idx["areas"]) == 1
    assert idx["areas"][0]["bbox"] == [0, 0, 2, 2]


def test_safe_area_name_blocks_traversal(store):
    # the name itself must have no path separators or dots, so the file lands
    # inside the areas directory and nowhere else
    assert "/" not in map_store.safe_area_name("../../etc/passwd")
    assert ".." not in map_store.safe_area_name("../../etc/passwd")
    assert map_store.safe_area_name("../../etc") == "etc"
    assert map_store.safe_area_name("") == "area"
    # the stored path stays under areas/
    assert map_store.area_file("../../etc/passwd").startswith(
        map_store.AREAS_DIR + "/")


def test_budget_and_warn(store):
    map_store.set_budget(0.000001)   # about 1 KB
    _write(store / map_store.area_file("a"), 900)
    map_store.add_area("a", [0, 0, 1, 1], size_bytes=900)
    b = map_store.budget_bytes()
    assert map_store.over_warn() is (map_store.total_size() >= b * 0.8)
    assert map_store.would_exceed_budget(1000) is True
    assert map_store.would_exceed_budget(1) is (map_store.total_size() + 1 > b)


def test_free_bytes_never_negative(store):
    map_store.set_budget(0)
    _write(store / map_store.area_file("a"), 10)
    map_store.add_area("a", [0, 0, 1, 1], size_bytes=10)
    assert map_store.free_bytes() == 0


def test_point_in_bbox():
    bb = [4.0, 51.0, 5.0, 52.0]      # west, south, east, north
    assert map_store.point_in_bbox(51.5, 4.5, bb) is True
    assert map_store.point_in_bbox(50.5, 4.5, bb) is False   # south of box
    assert map_store.point_in_bbox(51.5, 6.0, bb) is False   # east of box
    assert map_store.point_in_bbox(51.5, "x", bb) is False
    assert map_store.point_in_bbox(51.5, 4.5, [1, 2, 3]) is False


def test_detail_at_base_and_area(store):
    _write(store / map_store.BASE_FILE, 10)
    map_store.add_base(maxzoom=7)
    # a point with only the base
    assert map_store.detail_at(10.0, 10.0) == 7
    # add an area that covers a different point to z14
    _write(store / map_store.area_file("hi"), 10)
    map_store.add_area("hi", [4.0, 51.0, 5.0, 52.0], maxzoom=14)
    assert map_store.detail_at(51.5, 4.5) == 14
    assert map_store.detail_at(10.0, 10.0) == 7


def test_detail_at_no_base(store):
    assert map_store.detail_at(10.0, 10.0) == -1


def test_view_warnings_no_base(store):
    w = map_store.view_warnings(10.0, 10.0, 3)
    assert any("no base map" in m for m in w)


def test_view_warnings_zoom_past_stored(store):
    _write(store / map_store.BASE_FILE, 10)
    map_store.add_base(maxzoom=7)
    w = map_store.view_warnings(10.0, 10.0, 12)
    assert any("more detail" in m for m in w)
    # inside the base zoom, no zoom warning
    assert not any("more detail" in m
                   for m in map_store.view_warnings(10.0, 10.0, 5))


def test_view_warnings_stale(store):
    _write(store / map_store.BASE_FILE, 10)
    map_store.add_base(maxzoom=7)
    # backdate the base created stamp well past the stale window
    idx = map_store.load_index()
    idx["base"]["created"] = "2000-01-01T00:00:00+00:00"
    map_store.save_index(idx)
    w = map_store.view_warnings(10.0, 10.0, 5, max_age_days=180)
    assert any("older than" in m for m in w)


def test_human_size():
    assert map_store.human_size(0) == "0 B"
    assert map_store.human_size(512) == "512 B"
    assert map_store.human_size(46 * 1024 ** 2).endswith("MB")
    assert map_store.human_size(2 * 1024 ** 3).endswith("GB")


def test_age_days_none_without_stamp():
    assert map_store.age_days({}) is None
    assert map_store.age_days({"created": "not-a-date"}) is None
