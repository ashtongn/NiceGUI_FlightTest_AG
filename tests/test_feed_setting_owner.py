#!/usr/bin/env python3
"""One owner for the aircraft feed settings: a page writes only the fields
its user changed, on the settings as saved now, and every page's fields
follow a change saved elsewhere. A page built before another one saved (the
planner's feed row, the SDR tab panel) must not put its old interface back
when its listen switch is toggled; that left the feed dead on 0.0.0.0 (BENCH-91,
8 November). The last test opens a real socket on 127.0.0.1.

    python3 tests/test_feed_setting_owner.py
"""
import os
import socket
import sys
import tempfile
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="dpmg_feed_owner_")
os.environ["DPMG_FEED_SETTINGS"] = os.path.join(_TMP, "aircraft_feed.json")

from dpmg.link import feed_settings                          # noqa: E402
from dpmg.link.acft_pos_multicast import MulticastPosListener, _nmea_gga  # noqa: E402
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
    """Records each start with the interface it joined on. No socket."""

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
        self.starts.append(self.iface_ip)

    def stop(self):
        self.running = False


def _fields(pf, **over):
    """A page's fields as the page holds them: what it was last filled with,
    plus whatever the user typed."""
    vals = dict(pf.shown)
    vals.update(over)
    return vals


def _fresh_file(**entry):
    e = dict({"name": "N1", "group": "239.2.1.1", "port": 5005, "iface": "0.0.0.0", "format": "auto"}, **entry)
    feed_settings.save({"aircraft": [e]})


def test_entry_edits():
    shown = {"group": "239.2.1.1", "port": 5005, "iface": "0.0.0.0", "format": "auto", "window_s": 1.5}
    check(feed_settings.entry_edits(shown, {"group": " 239.2.1.1 ", "port": "5005", "iface": "0.0.0.0",
                                            "format": "auto"}) == {},
          "the same values as text, with spaces, are no edit")
    check(feed_settings.entry_edits(shown, {"port": 5005.0}) == {}, "a number field's float is no edit")
    check(feed_settings.entry_edits(shown, {"iface": "127.0.0.1", "port": 5005}) == {"iface": "127.0.0.1"},
          "only the changed field is an edit")
    check(feed_settings.entry_edits(shown, {"window_s": 5}) == {"window_s": 5.0}, "the window is a field too")


def test_update_aircraft_writes_only_changes():
    _fresh_file()
    path = feed_settings.settings_path()
    m0 = os.stat(path).st_mtime_ns
    time.sleep(0.01)
    st, changed = feed_settings.update_aircraft("N1", {})
    check(not changed and os.stat(path).st_mtime_ns == m0, "no change: the file is not written")
    st, changed = feed_settings.update_aircraft("nobody", {"iface": "127.0.0.1"})
    check(not changed and feed_settings.load()["aircraft"][0]["iface"] == "0.0.0.0",
          "an unknown aircraft changes nothing")
    # another program saved a new port after this one last read the file
    _fresh_file(port=6000)
    st, changed = feed_settings.update_aircraft("N1", {"iface": "127.0.0.1"})
    e = feed_settings.load()["aircraft"][0]
    check(changed and (e["iface"], e["port"]) == ("127.0.0.1", 6000),
          f"the change lands on the settings as saved now, the other port kept: {e}")


def test_stale_panel_toggle_keeps_newer_iface():
    """The 8 November case: the interface set to 127.0.0.1 on the Aircraft
    feed tab, then the planner's feed row and the SDR tab panel, built while
    the setting was 0.0.0.0, toggle listen."""
    from dpmg.mission import mission_tab as mt
    _fresh_file()
    feeds = AircraftFeeds(listener_factory=FakeListener)
    sess = mt._PlanSession(SimpleNamespace(hosts={}), path=os.path.join(_TMP, "plan.json"))
    sess.feeds = feeds
    sess._follow_primary()
    lst = feeds.get("N1")

    planner, operator, eng = PanelFields(feeds), PanelFields(feeds), PanelFields(feeds)
    for pf in (planner, operator, eng):
        pf.fill("N1")
    planner_fields = _fields(planner)           # 0.0.0.0, never touched again
    operator_fields = _fields(operator)
    check(planner_fields["iface"] == "0.0.0.0" and sess.pos_iface == "0.0.0.0", "all start on 0.0.0.0")

    # the Aircraft feed tab: interface 127.0.0.1, Save and Listen
    name, e, changed = feeds.update("N1", eng.edits(_fields(eng, iface="127.0.0.1")))
    feeds.start(name)
    check(changed and feed_settings.load()["aircraft"][0]["iface"] == "127.0.0.1", "saved 127.0.0.1")
    check(lst.starts == ["127.0.0.1"], f"listening on 127.0.0.1: {lst.starts}")
    check(sess.pos_iface == "127.0.0.1", "the planner session reads the new interface at once")
    check(planner.stale() and operator.stale() and eng.stale(), "every page sees the settings moved on")

    # the planner's switch: off, then on again, its fields still 0.0.0.0
    sess.stop_listener("N1")
    sess.start_listener(name="N1", changes=planner.edits(planner_fields))
    check(feed_settings.load()["aircraft"][0]["iface"] == "127.0.0.1",
          "a stale planner toggling listen leaves the saved interface at 127.0.0.1")
    check(lst.running and lst.iface_ip == "127.0.0.1" and lst.starts[-1] == "127.0.0.1",
          f"and the listener starts again on 127.0.0.1: {lst.starts}")

    # the switch set on from code while already running starts nothing
    n = len(lst.starts)
    sess.start_listener(name="N1", changes=planner.edits(planner_fields))
    check(len(lst.starts) == n, "listen on a running listener with nothing changed: no restart")

    # the SDR tab panel's Listen, its fields still 0.0.0.0
    name, e, changed = feeds.update("N1", operator.edits(operator_fields))
    check(not changed and e["iface"] == "127.0.0.1" and lst.iface_ip == "127.0.0.1",
          "a stale SDR tab panel pressing Listen changes nothing")

    # the pages refill: a field the user is typing in keeps its value
    vals = planner.fill("N1", current=dict(planner_fields, group="239.3.3.3"))
    check(vals["iface"] == "127.0.0.1" and vals["group"] == "239.3.3.3" and not planner.stale(),
          f"the planner row follows 127.0.0.1 and keeps a group being typed: {vals}")
    vals = operator.fill("N1", current=operator_fields)
    check(vals["iface"] == "127.0.0.1", "the SDR tab panel follows 127.0.0.1")

    # a real change on the planner still goes through, and only that field
    sess.start_listener(name="N1", changes=planner.edits(dict(_fields(planner), port=5006)))
    e = feed_settings.load()["aircraft"][0]
    check((e["iface"], e["port"]) == ("127.0.0.1", 5006),
          f"the planner's port edit is saved, the interface kept: {e}")
    check(lst.starts[-1] == "127.0.0.1" and lst.port == 5006, "the listener started again on the new port")
    sess.stop_listener("N1")


def _igmp_on(group, dev):
    """True when /proc/net/igmp lists this group on this device (Linux only;
    None when the file is not there)."""
    try:
        with open("/proc/net/igmp", "r") as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    hexgrp = socket.inet_aton(group)[::-1].hex().upper()
    cur = None
    for ln in lines[1:]:
        if not ln.startswith(("\t", " ")):
            parts = ln.split()
            cur = parts[1].rstrip(":") if len(parts) > 1 else None
        elif cur == dev and ln.split() and ln.split()[0].upper() == hexgrp:
            return True
    return False


def test_iface_change_restarts_socket():
    grp, port = "239.9.9.8", 55066
    _fresh_file(group=grp, port=port, iface="0.0.0.0")
    feeds = AircraftFeeds()
    lst = feeds.get("N1")
    try:
        try:
            lst.start()
        except OSError as e:
            print(f"  (no default multicast route here, starting on 0.0.0.0 failed: {e})")
        sock1 = lst._sock
        check(_igmp_on(grp, "lo") in (False, None), "on 0.0.0.0 the group is not joined on lo")

        feeds.update("N1", {"iface": "127.0.0.1"})
        check(lst.running and lst._sock is not None and lst._sock is not sock1,
              "changing the interface opened a new socket")
        check(lst.iface_ip == "127.0.0.1", "on 127.0.0.1")
        on_lo = _igmp_on(grp, "lo")
        check(on_lo in (True, None), f"the group is joined on lo ({'/proc/net/igmp' if on_lo else 'no igmp file'})")

        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton("127.0.0.1"))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        for i in range(40):
            s.sendto(_nmea_gga(33.4 + i * 0.001, -112.0, 1500.0).encode(), (grp, port))
            time.sleep(0.05)
            if lst.latest is not None:
                break
        s.close()
        check(lst.latest is not None, f"a fix sent to the group via 127.0.0.1 arrives (rx {lst.rx})")

        sock2 = lst._sock
        feeds.update("N1", {"format": "nmea"})
        check(lst._sock is sock2 and lst.fmt == "nmea", "a format change keeps the socket")

        # another program writes the file: the registry follows and restarts
        rev = feeds.rev
        time.sleep(0.01)
        st = feed_settings.load()
        st["aircraft"][0]["port"] = port + 1
        feed_settings.save(st)
        check(feeds.follow_file() and feeds.rev > rev, "a save by another program is picked up")
        check(lst.running and lst._sock is not sock2 and lst.port == port + 1,
              "and the listener started again on the new port")
        check(not feeds.follow_file(), "nothing new on the next look")
    finally:
        feeds.stop_all()
    check(not lst.running, "stopped")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(t.__name__)
        t()
    print(f"\n{_passed} passed, {_failed} failed, {len(tests)} test functions")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
