"""Tests for the aircraft feed tooling: saved settings, the listener's packet
history, fix rate and feed time, the test sender's datagrams and recordings,
the flight view's feed state, and the planner's two modes. No sockets are
opened except by the sender test, which stays on 127.0.0.1.

    python3 tests/test_feed_tooling.py
"""

import calendar
import os
import sys
import tempfile
import time
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)          # repo root
if REPO not in sys.path:
    sys.path.insert(0, REPO)

_TMP = tempfile.mkdtemp(prefix="dpmg_feed_test_")
os.environ["DPMG_FEED_SETTINGS"] = os.path.join(_TMP, "aircraft_feed.json")

from dpmg.link import acft_pos_multicast as am      # noqa: E402
from dpmg.link import feed_sender as fs             # noqa: E402
from dpmg.link import feed_settings                 # noqa: E402
from dpmg.mission import feed_state as fst          # noqa: E402
from dpmg.mission import triggers as tg             # noqa: E402
from dpmg.mission.position_feed import PositionFeed  # noqa: E402

_passed = 0
_failed = 0
DAY0 = calendar.timegm((2026, 11, 8, 0, 0, 0))      # 2026-11-08 00:00:00 UTC


def check(cond, msg):
    global _passed, _failed
    if cond:
        _passed += 1
    else:
        _failed += 1
        print(f"  FAIL: {msg}")


# settings

def test_settings_defaults_and_roundtrip():
    st = feed_settings.load()
    check(st["group"] == "239.2.1.1" and st["port"] == 5005, "defaults with no file")
    check(st["format"] == "auto" and st["iface"] == "0.0.0.0", "default format and iface")
    # the aircraft list is the truth; the top level keys mirror its first entry
    st["aircraft"][0].update(group="239.9.9.9", port="6000", iface="127.0.0.1", format="NMEA")
    st["sender"]["rate_hz"] = "5"
    path = feed_settings.save(st)
    check(os.path.exists(path), "settings file written")
    back = feed_settings.load()
    check(back["port"] == 6000 and isinstance(back["port"], int), "port saved as an int")
    check(back["format"] == "nmea", "format lower-cased")
    check(back["aircraft"][0]["name"] == "239.2.1.1:5005" and len(back["aircraft"]) == 1,
          "one aircraft, named from the values it was created with")
    check(back["sender"]["rate_hz"] == 5.0, "sender rate saved as a float")
    bad = feed_settings.normalize({"port": "x", "format": "csv", "group": ""})
    check(bad["port"] == 5005 and bad["format"] == "auto" and bad["group"] == "239.2.1.1",
          "bad values fall back to the defaults")
    lst = am.MulticastPosListener()
    feed_settings.apply(lst, back)
    check((lst.group, lst.port, lst.iface_ip, lst.fmt) == ("239.9.9.9", 6000, "127.0.0.1", "nmea"),
          "apply puts the saved values on a listener")
    check(lst._parser is am.parse_nmea_bytes, "nmea format picks the nmea parser")


# feed time stamps

def test_feed_time_parsing():
    now = DAY0 + 3600            # 2026-11-08 01:00:00 UTC
    t = am.nmea_time_utc("010000.50", now=now)
    check(abs(t - (now + 0.5)) < 1e-6, "GGA time takes today's date")
    t = am.nmea_time_utc("235959.00", now=now)
    check(abs(t - (now - 3600 - 1)) < 1e-6, "GGA time just before midnight takes yesterday")
    t = am.nmea_time_utc("120000.00", "081126")
    check(t == DAY0 + 12 * 3600, "RMC date and time give the exact epoch")
    check(am.nmea_time_utc("") is None and am.nmea_time_utc("9999") is None, "bad time fields give None")
    gga = am._nmea_gga(33.4, -112.0, 1500.0, DAY0 + 12 * 3600 + 5.25)
    p = am.parse_auto(gga.encode())
    check(p is not None and p.source == "nmea:GPGGA", "the test GGA parses")
    check(p.t_feed_utc is not None and abs(p.t_feed_utc % 86400 - (12 * 3600 + 5.25)) < 0.02,
          "GGA carries the time of day given")
    rmc = "$GPRMC,120000.00,A,3324.0000,N,11200.0000,W,0.0,0.0,081126,,,A"
    p = am.parse_nmea(rmc)
    check(p is not None and p.t_feed_utc == DAY0 + 12 * 3600, "RMC carries date and time")
    p = am.parse_auto(b'{"lat": 1, "lon": 2, "time": 1794182400.5}')
    check(p.t_feed_utc == 1794182400.5, "JSON epoch seconds")
    p = am.parse_auto(b'{"lat": 1, "lon": 2, "timestamp": 1794182400500}')
    check(p.t_feed_utc == 1794182400.5, "JSON epoch milliseconds")
    p = am.parse_auto(b'{"lat": 1, "lon": 2, "time_utc": "2026-11-08T12:00:00Z"}')
    check(p.t_feed_utc == DAY0 + 12 * 3600, "JSON ISO text")
    p = am.parse_auto(b'{"lat": 1, "lon": 2}')
    check(p is not None and p.t_feed_utc is None, "JSON with no time parses, time None")


# listener history, rate, offset, format

def test_listener_history_rate_and_offset():
    lst = am.MulticastPosListener()
    t0w, t0m = 1794182400.0, 1000.0
    for i in range(6):
        raw = fs.datagram(-112.0, 33.4 + i * 1e-4, 1500.0, "json", t0w + i * 0.5 + 0.25)
        lst.feed(raw, ("10.0.0.5", 5005), t_wall=t0w + i * 0.5, t_mono=t0m + i * 0.5)
    lst.feed(b"garbage", ("10.0.0.5", 5005), t_wall=t0w + 3.0, t_mono=t0m + 3.0)
    check(lst.rx == 7 and lst.parsed == 6 and lst.bad == 1, "counters")
    hist = lst.recent(100)
    check(len(hist) == 7 and hist[-1].pos is None and hist[-1].text == "garbage", "history keeps the bad one too")
    check(hist[0].addr == "10.0.0.5:5005", "sender address kept")
    r = lst.rate_hz(window_s=10.0, now_mono=t0m + 3.0)
    check(r is not None and abs(r - 2.0) < 1e-6, f"rate 2 Hz from the arrival times, got {r}")
    check(lst.rate_hz(window_s=0.1, now_mono=t0m + 3.0) is None, "too few fixes in the window gives None")
    off = lst.feed_offset_s()
    check(off is not None and abs(off[0] - 0.25) < 1e-6 and off[3] == 6, "feed offset mean +0.25 s")
    check(lst.recent(2)[-1].t_wall == t0w + 3.0, "recent(n) gives the newest last")
    lst.clear_history()
    check(lst.recent() == [] and lst.rate_hz() is None, "clear_history")
    # format selection: json only rejects an NMEA datagram
    lst.set_format("json")
    p = lst.feed(am._nmea_gga(1.0, 2.0, 3.0).encode())
    check(p is None and lst.bad == 2, "json format drops an NMEA datagram")
    lst.set_format("nmea")
    p = lst.feed(am._nmea_gga(1.0, 2.0, 3.0).encode())
    check(p is not None, "nmea format takes it")
    p = lst.feed(b'{"lat": 1, "lon": 2}')
    check(p is None, "nmea format drops JSON")
    lst.set_format("auto")
    check(lst.feed(b'{"lat": 1, "lon": 2}') is not None, "auto takes both again")
    seen = []
    lst.on_packet = seen.append
    lst.feed(b'{"lat": 5, "lon": 6}')
    check(len(seen) == 1 and seen[0].pos.lat_deg == 5.0, "on_packet callback sees the packet")


# sender helpers and recordings

def test_sender_datagrams_and_path():
    pts = fs.parse_points("33.40,-112.07 33.45,-112.07; 33.50,-112.00")
    check(pts == [[-112.07, 33.4], [-112.07, 33.45], [-112.0, 33.5]], "lat,lon pairs to [lon, lat]")
    steps = list(fs.interpolate(pts[:2], speed_mps=1000.0, rate_hz=1.0))
    check(len(steps) == 7 and steps[-1] == pts[1], f"5.56 km at 1 km/s gives 6 steps plus the end, got {len(steps)}")
    for fmt in ("json", "nmea"):
        raw = fs.datagram(-112.07, 33.4, 1500.0, fmt, 1794182400.5)
        p = am.parse_auto(raw)
        check(p is not None and abs(p.lat_deg - 33.4) < 1e-5 and abs(p.lon_deg + 112.07) < 1e-5,
              f"{fmt} datagram parses back")
        check(p.alt_m is not None and abs(p.alt_m - 1500.0) < 0.1, f"{fmt} carries the altitude")
        check(p.t_feed_utc is not None, f"{fmt} carries a time")
    # recorder and replay file
    lst = am.MulticastPosListener()
    rec = fs.PacketRecorder(os.path.join(_TMP, "rec", "a.jsonl"))
    lst.on_packet = rec.write
    lst.feed(b'{"lat": 1, "lon": 2}', ("1.2.3.4", 9), t_wall=10.0)
    lst.feed(am._nmea_gga(1.0, 2.0, 3.0).encode(), ("1.2.3.4", 9), t_wall=11.0)
    rec.close()
    back = fs.read_recording(rec.path)
    check(len(back) == 2 and back[0][0] == 10.0 and back[1][1].startswith(b"$GPGGA"),
          "recording reads back with times and raw datagrams")
    rp = fs.RecordingReplayer("239.2.1.1", 5005, "127.0.0.1")
    check(rp.load(rec.path) == 2 and rp.state == "stopped", "replayer loads the file")
    # a sender on the loopback: starts, sends, stops, no exception. On a port
    # of its own, so a GUI listening on the saved port never sees test traffic
    ps = fs.PathSender("239.2.1.1", 5995, "127.0.0.1", fmt="json", rate_hz=20.0, speed_mps=5000.0)
    check(ps.set_path(pts) == 3 and ps.length_m() > 10000, "path set")
    check(ps.start(), "sender starts")
    time.sleep(0.5)
    ps.stop()
    check(ps.state == "stopped" and ps.sent >= 3 and not ps.error, f"sender sent {ps.sent}, error {ps.error!r}")
    check(not fs.PathSender("239.2.1.1", 5005).start(), "no path: start refused")


# flight view feed state

class FakeListener:
    def __init__(self, running=True, group="239.2.1.1", port=5005, iface="0.0.0.0", fmt="auto"):
        self.group, self.port, self.iface_ip, self.fmt = group, port, iface, fmt
        self.running = running
        self.latest = None
        self.rx = self.parsed = self.bad = 0
        self._rate = None
        self._off = None

    def rate_hz(self, window_s=10.0, now_mono=None):
        return self._rate

    def feed_offset_s(self, n=20):
        return self._off


def _sess(listener, motion_state="stopped", fix=None):
    motion = SimpleNamespace(state=motion_state, fix=lambda: fix if motion_state != "stopped" else None)
    sess = SimpleNamespace(listener=listener, motion=motion,
                           pos_group="239.2.1.1", pos_port=5005, pos_iface="0.0.0.0")
    sess.feed = PositionFeed(None, listener, motion=motion)
    return sess


def test_feed_state_levels():
    now_m = time.monotonic()
    st = fst.feed_state(_sess(None), now_mono=now_m)
    check(st["level"] == "bad" and "not listening" in st["warning"], "no listener: bad, says not listening")
    check(st["where"] == "239.2.1.1:5005 via 0.0.0.0", "where from the session defaults")
    lst = FakeListener(running=True)
    lst.rx = 3
    st = fst.feed_state(_sess(lst), now_mono=now_m)
    check(st["level"] == "bad" and "no fix yet" in st["warning"] and "3 datagram" in st["warning"],
          "listening, nothing parsed: bad with the count")
    lst.latest = am.AircraftPos(33.4, -112.0, 1500.0, now_m - 1.0, "json", "{}")
    lst.parsed = 3
    lst._rate = 1.0
    st = fst.feed_state(_sess(lst), now_mono=now_m)
    check(st["level"] == "ok" and st["source"] == "aircraft feed" and st["warning"] == "",
          f"fresh fix: ok, got {st['level']} {st['warning']!r}")
    check(abs(st["age_s"] - 1.0) < 0.05 and st["rate_hz"] == 1.0, "age and rate carried")
    txt = fst.feed_state_text(st)
    check("source aircraft feed" in txt and "listening" in txt and "1.00 Hz" in txt, f"text line: {txt}")
    lst.latest = am.AircraftPos(33.4, -112.0, 1500.0, now_m - 30.0, "json", "{}")
    st = fst.feed_state(_sess(lst), now_mono=now_m)
    check(st["level"] == "bad" and "stale" in st["warning"], "30 s old fix: stale")
    lst.latest = am.AircraftPos(33.4, -112.0, 1500.0, now_m - 1.0, "json", "{}")
    lst._off = (3.5, 3.4, 3.6, 5)
    st = fst.feed_state(_sess(lst), now_mono=now_m)
    check(st["level"] == "warn" and "feed time" in st["warning"] and st["feed_offset_s"] == 3.5,
          "feed time off by 3.5 s: warn")
    lst._off = None
    sim = tg.Fix(33.0, -112.0, None, t=now_m, source="simulated motion")
    st = fst.feed_state(_sess(lst, motion_state="playing", fix=sim), now_mono=now_m)
    check(st["level"] == "bad" and "SIMULATED" in st["warning"] and st["source"] == "simulated motion",
          "simulated motion playing: red warning")
    st = fst.feed_state(_sess(lst, motion_state="paused", fix=sim), now_mono=now_m)
    check("SIMULATED" in st["warning"], "paused motion still counts as simulated")
    check(fst.STALE_S == tg.STALE_S, "the feed state uses the triggers' stale limit")


def test_planner_modes():
    from dpmg.mission import mission_tab as mt
    check(mt.MODES == ("flight", "engineering"), "two modes")
    try:
        mt.build_mission_tab(None, mode="other")
        check(False, "a bad mode is refused")
    except ValueError as e:
        check("mode" in str(e), "a bad mode is refused with a clear message")
    import inspect
    check(inspect.signature(mt.build_mission_tab).parameters["mode"].default == "flight",
          "the default view is the flight view")
    sess = mt._PlanSession(SimpleNamespace(hosts={}), path=os.path.join(_TMP, "plan.json"))
    check(sess.listener is not None and not sess.listening, "the session has the shared listener, not running")
    check(sess.pos_format in ("auto", "json", "nmea"), "the session carries the feed format")
    check(sess.pos_group == feed_settings.load()["group"], "group from the saved settings")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{_passed} passed, {_failed} failed, {len(tests)} test functions")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
