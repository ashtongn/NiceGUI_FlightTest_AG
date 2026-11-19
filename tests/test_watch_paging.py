"""WATCH_UPDATE paging: a plan with more decode bands than one datagram holds
is sent in full (proto.WatchUpdate.paginate) and the GUI merges the pages by
band name (HostLink._merge_watch), so no band is dropped. Covers the decode
panel showing only the first ~9 of 24 bands."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dpmg.link import proto                                       # noqa: E402


def _bands(n, msg_len=48):
    return [{"n": f"band_{i:02d}", "f": 974e6 + i * 1e6, "m": "FSK",
             "p": True, "d": True, "msg": "x" * msg_len, "dn": i, "tn": i}
            for i in range(n)]


def test_paginate_fits_and_keeps_every_band():
    bands = _bands(24)
    pages = proto.WatchUpdate.paginate("G40-SIM", bands)
    assert len(pages) > 1, "24 full bands should need more than one datagram"
    seen = {}
    for pg in pages:
        raw = pg.pack()
        assert len(raw) <= proto.MAX_DATAGRAM
        u = proto.WatchUpdate.unpack(raw)
        assert u.total == 24
        for b in u.bands:
            seen[b["n"]] = b
    assert len(seen) == 24


def test_empty_plan_is_one_page():
    pages = proto.WatchUpdate.paginate("S", [])
    assert len(pages) == 1 and pages[0].bands == []


def test_single_datagram_pack_still_safe():
    # a WatchUpdate built directly (not via paginate) never overflows
    w = proto.WatchUpdate("S", _bands(40))
    assert len(w.pack()) <= proto.MAX_DATAGRAM


def test_gui_merge_assembles_all_bands():
    from dpmg.link import link_client as lc
    h = lc.HostLink.__new__(lc.HostLink)
    h.watch_bands = []
    h._watch_t = 0.0
    h._watch_map = {}
    for pg in proto.WatchUpdate.paginate("G40-SIM", _bands(24)):
        h._merge_watch(pg)
    assert len(h.watch_bands) == 24
    # sorted by frequency, ascending
    freqs = [b["f"] for b in h.watch_bands]
    assert freqs == sorted(freqs)


def test_gui_merge_ages_out_old_bands():
    import time
    from dpmg.link import link_client as lc
    h = lc.HostLink.__new__(lc.HostLink)
    h.watch_bands = []
    h._watch_t = 0.0
    h._watch_map = {}
    for pg in proto.WatchUpdate.paginate("G40-SIM", _bands(24)):
        h._merge_watch(pg)
    assert len(h.watch_bands) == 24
    # a later, smaller plan: old bands age out once their pages stop arriving
    for name in list(h._watch_map):
        row, _t = h._watch_map[name]
        h._watch_map[name] = (row, time.monotonic() - lc.WATCH_TTL_S - 1.0)
    for pg in proto.WatchUpdate.paginate("G40-SIM", _bands(3)):
        h._merge_watch(pg)
    assert len(h.watch_bands) == 3


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
