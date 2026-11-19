#!/usr/bin/env python3
"""A feed format picked on any page reaches the running listener at once.

TRIG-83 step 4 (11 November): the planner's format set to json while an NMEA
sender ran left the listener on auto until the listen switch went off and on,
and auto picked on the Aircraft feed tab stayed unsaved until Listen, so the
planner's switch started it on json again. The format is saved when it
is picked (PanelFields.apply) and the listener takes it on its next
datagram, with no restart. The first test opens a real socket on 127.0.0.1.

    python3 tests/test_feed_format_live.py
"""
import os
import socket
import sys
import tempfile
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="dpmg_feed_fmt_")
os.environ["DPMG_FEED_SETTINGS"] = os.path.join(_TMP, "aircraft_feed.json")

from dpmg.link import feed_settings                          # noqa: E402
from dpmg.link.acft_pos_multicast import _nmea_gga            # noqa: E402
from dpmg.link.aircraft_feeds import AircraftFeeds, PanelFields  # noqa: E402

_passed = _failed = 0


def check(cond, msg):
    global _passed, _failed
    if cond:
        _passed += 1
        print("  ok  ", msg)
    else:
        _failed += 1
        print("  FAIL:", msg)


class FakeListener:
    """Records each start with the format it had. No socket."""

    def __init__(self):
        self.group, self.port, self.iface_ip = "239.2.1.1", 5005, "0.0.0.0"
        self.fmt, self.window_s = "auto", 1.5
        self.running = False
        self.starts = []
        self.latest = None

    def set_format(self, f):
        self.fmt = f

    def set_window(self, w):
        self.window_s = w

    def start(self):
        self.running = True
        self.starts.append(self.fmt)

    def stop(self):
        self.running = False


def _fields(pf, **over):
    """A page's fields as the page holds them: what it was last filled with,
    plus whatever the user picked or typed."""
    vals = dict(pf.shown)
    vals.update(over)
    return vals


def _fresh_file(**entry):
    e = dict({"name": "N1", "group": "239.2.1.1", "port": 5005, "iface": "127.0.0.1", "format": "auto"}, **entry)
    feed_settings.save({"aircraft": [e]})


def _saved_format():
    return feed_settings.load()["aircraft"][0]["format"]


def _send_nmea(grp, port, lst, want_parsed, n=40):
    """Send GGA sentences to the group via 127.0.0.1 until the listener has
    received n more or (with want_parsed) parsed one more. Returns (rx, parsed)
    added."""
    rx0, parsed0 = lst.rx, lst.parsed
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton("127.0.0.1"))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
    try:
        for i in range(n):
            s.sendto(_nmea_gga(33.4 + i * 0.001, -112.0, 1500.0).encode(), (grp, port))
            time.sleep(0.03)
            if want_parsed and lst.parsed > parsed0:
                break
            if not want_parsed and lst.rx - rx0 >= 5:
                break
    finally:
        s.close()
    time.sleep(0.1)
    return lst.rx - rx0, lst.parsed - parsed0


def test_format_change_on_running_listener():
    """The bench case on a real socket: NMEA arriving, the planner picks json,
    the listener stops parsing at once on the same socket; auto picked on the
    Aircraft feed tab and it parses again, with no Stop and Listen."""
    grp, port = "239.9.9.7", 55077
    _fresh_file(group=grp, port=port)
    feeds = AircraftFeeds()
    lst = feeds.get("N1")
    planner, eng = PanelFields(feeds), PanelFields(feeds)
    planner.fill("N1")
    eng.fill("N1")
    try:
        lst.start()
        sock, since = lst._sock, lst.started_at
        rx, parsed = _send_nmea(grp, port, lst, want_parsed=True)
        check(parsed >= 1 and lst.fmt == "auto", f"auto parses the NMEA sender (rx {rx}, parsed {parsed})")

        # the planner's format: json, no switch touched
        res = planner.apply(_fields(planner, format="json"))
        check(res is not None and res[2] and _saved_format() == "json", f"json saved when picked: {res}")
        check(lst.fmt == "json" and lst.running and lst._sock is sock and lst.started_at == since,
              "the running listener is on json at once, same socket, not restarted")
        rx, parsed = _send_nmea(grp, port, lst, want_parsed=False)
        check(rx >= 1 and parsed == 0, f"NMEA is received and no longer parsed (rx {rx}, parsed {parsed})")

        # the Aircraft feed tab follows (its tick refills), then auto is picked there
        check(eng.stale(), "the Aircraft feed tab sees the settings moved on")
        vals = eng.fill("N1", current=_fields(eng))
        check(vals["format"] == "json", f"and its format field follows to json: {vals['format']}")
        res = eng.apply(_fields(eng, format="auto"))
        check(res is not None and res[2] and _saved_format() == "auto", "auto saved when picked on the tab")
        check(lst.fmt == "auto" and lst._sock is sock and lst.started_at == since,
              "the listener is back on auto at once, same socket")
        rx, parsed = _send_nmea(grp, port, lst, want_parsed=True)
        check(parsed >= 1, f"and the NMEA sender parses again (rx {rx}, parsed {parsed})")
    finally:
        feeds.stop_all()
    check(not lst.running, "stopped")


def test_stale_page_does_not_write_old_format():
    """The second half of the bench case: a page whose format field was not
    refilled yet uses its listen switch. Its field is the one it was filled
    with, so it is no edit and the saved format stands."""
    from dpmg.mission import mission_tab as mt
    _fresh_file()
    feeds = AircraftFeeds(listener_factory=FakeListener)
    sess = mt._PlanSession(SimpleNamespace(hosts={}), path=os.path.join(_TMP, "plan.json"))
    sess.feeds = feeds
    sess._follow_primary()
    lst = feeds.get("N1")
    planner, eng = PanelFields(feeds), PanelFields(feeds)
    planner.fill("N1")
    eng.fill("N1")
    sess.start_listener(name="N1", changes=planner.edits(_fields(planner)))
    check(lst.running and lst.starts == ["auto"], f"listening on auto: {lst.starts}")

    # the planner picks json: at once, no switch
    n = len(lst.starts)
    planner.apply(_fields(planner, format="json"))
    check(lst.fmt == "json" and len(lst.starts) == n and sess.pos_format == "json",
          "the planner's json reaches the running listener without a restart")
    planner_fields = _fields(planner)
    check(planner_fields["format"] == "json" and planner.edits(planner_fields) == {},
          "the planner field holds the saved json, no edit left for its switch to send")

    # auto picked on the Aircraft feed tab (stale there, its field still auto)
    check(eng.stale() and eng.shown["format"] == "auto", "the tab was filled before the planner's change")
    res = eng.apply(_fields(eng, format="nmea"))
    check(res is not None and _saved_format() == "nmea" and lst.fmt == "nmea",
          "a format picked on a stale tab is still saved and taken")
    eng.fill("N1", current=_fields(eng))
    eng.apply(_fields(eng, format="auto"))
    check(_saved_format() == "auto" and lst.fmt == "auto", "auto picked on the tab: saved and on the listener")

    # the planner, not refilled yet, toggles its switch with json still in its field
    check(planner.stale(), "the planner has not refilled yet")
    sess.stop_listener("N1")
    sess.start_listener(name="N1", changes=planner.edits(planner_fields))
    check(_saved_format() == "auto", "the stale planner's switch leaves the saved format on auto")
    check(lst.running and lst.fmt == "auto" and lst.starts[-1] == "auto",
          f"and the listener starts again on auto: {lst.starts}")

    # its tick then refills the field to auto
    vals = planner.fill("N1", current=planner_fields)
    check(vals["format"] == "auto" and not planner.stale(), f"the planner field follows to auto: {vals['format']}")
    sess.stop_listener("N1")


def test_fill_from_code_writes_nothing():
    """A page puts the saved value in its field from code, which fires the
    field's change handler: apply finds no edit and the file is not written.
    A group being typed is not saved with a picked format."""
    _fresh_file()
    feeds = AircraftFeeds(listener_factory=FakeListener)
    page, other = PanelFields(feeds), PanelFields(feeds)
    page.fill("N1")
    other.fill("N1")
    other.apply(_fields(other, format="nmea"))
    sig = os.stat(feed_settings.settings_path()).st_mtime_ns
    vals = page.fill("N1", current=_fields(page))
    check(vals["format"] == "nmea", "the page refills to nmea")
    time.sleep(0.01)
    check(page.apply(dict(vals)) is None, "the refill's change event is no edit")
    check(os.stat(feed_settings.settings_path()).st_mtime_ns == sig, "and the file is not written")

    res = page.apply(dict(vals, group="239.3.3.3", format="json"))
    e = feed_settings.load()["aircraft"][0]
    check(res is not None and e["format"] == "json" and e["group"] == "239.2.1.1",
          f"picking json saves the format only, the group being typed waits for Listen: {e}")
    check(page.edits(dict(vals, group="239.3.3.3", format="json")) == {"group": "239.3.3.3"},
          "the group is still an edit for Listen to send, the format no longer is")


def test_format_written_by_another_program():
    """Another GUI process (or a hand edit) saves the format: the tick's
    follow_file puts it on the running listener."""
    _fresh_file()
    feeds = AircraftFeeds(listener_factory=FakeListener)
    lst = feeds.get("N1")
    feeds.start("N1")
    time.sleep(0.01)
    st = feed_settings.load()
    st["aircraft"][0]["format"] = "json"
    feed_settings.save(st)
    check(feeds.follow_file() and lst.fmt == "json" and lst.starts == ["auto"],
          "the running listener follows a format saved elsewhere, no restart")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(t.__name__)
        t()
    print(f"\n{_passed} passed, {_failed} failed, {len(tests)} test functions")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
