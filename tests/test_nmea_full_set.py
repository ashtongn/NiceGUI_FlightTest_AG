"""The aircraft feed's full NMEA set and the combined fix: each sentence kind
(GGA, RMC, GLL, VTG, GSA, GSV) from any talker, the combine window, the
position disagreement rule, bad checksums, the JSON heading and speed keys
with their units, the GUI text helpers, the sentence rates, the planner's
feed row and marker, the sender's burst with dropped sentences, and the
position log columns. No sockets, no radio.

    python3 tests/test_nmea_full_set.py
"""

import calendar
import csv
import glob
import os
import sys
import tempfile
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

_TMP = tempfile.mkdtemp(prefix="dpmg_nmea_test_")
os.environ["DPMG_FEED_SETTINGS"] = os.path.join(_TMP, "aircraft_feed.json")

from dpmg.link import acft_pos_multicast as am      # noqa: E402
from dpmg.link import feed_sender as fs             # noqa: E402
from dpmg.link import ground_log as gl              # noqa: E402
from dpmg.mission import feed_state as fst          # noqa: E402
from dpmg.mission import map_element as me          # noqa: E402
from dpmg.mission import triggers as tg             # noqa: E402
from dpmg.mission.position_feed import PositionFeed  # noqa: E402

_passed = 0
_failed = 0
DAY0 = calendar.timegm((2026, 11, 8, 0, 0, 0))
KT = am.KT_MPS


def check(cond, msg):
    global _passed, _failed
    if cond:
        _passed += 1
    else:
        _failed += 1
        print(f"  FAIL: {msg}")


def near(a, b, tol=1e-6):
    return a is not None and b is not None and abs(a - b) <= tol


def S(body):
    """A sentence from its body, checksum added."""
    return am._sentence(body)


def ed(line, idx, val):
    """The sentence with field idx replaced and the checksum redone."""
    f = line[1:line.rfind("*")].split(",")
    f[idx] = val
    return S(",".join(f))


GGA = S("GPGGA,120001.00,3327.1260,N,11200.1020,W,1,08,0.9,1240.0,M,0.0,M,,")
RMC = S("GPRMC,120001.00,A,3327.1260,N,11200.1020,W,141.9,087.0,081126,,,A")
GLL = S("GNGLL,3327.1260,N,11200.1020,W,120001.00,A,A")
VTG = S("GPVTG,087.0,T,077.5,M,141.9,N,262.8,K,A")
GSA = S("GNGSA,A,3,02,03,04,05,06,07,08,09,,,,,1.6,0.9,1.3")
GSV = S("GPGSV,3,1,11,02,45,120,40,03,30,200,38,04,60,010,42,05,15,300,35")


# each sentence on its own

def test_each_sentence():
    rd = am.parse_sentence(GGA)
    check(rd is not None and rd.kind == "GGA" and rd.name == "GPGGA", "GGA reads")
    check(near(rd.values["lat_deg"], 33.4521) and near(rd.values["lon_deg"], -112.0017), "GGA position")
    check(rd.values["alt_m"] == 1240.0 and rd.values["fix_quality"] == 1 and rd.values["sats_used"] == 8
          and rd.values["hdop"] == 0.9, "GGA altitude, quality, satellites, HDOP")
    check(rd.tod_s == 12 * 3600 + 1 and rd.date is None, "GGA time of day, no date")
    rd = am.parse_sentence(RMC)
    check(rd is not None and rd.kind == "RMC" and near(rd.values["speed_mps"], 141.9 * KT)
          and rd.values["heading_deg"] == 87.0, "RMC speed in knots and course")
    check(rd.date == (2026, 11, 8) and rd.t_feed_utc == DAY0 + 12 * 3600 + 1, "RMC date gives the exact epoch")
    check(am.parse_sentence(RMC.replace(",A,", ",V,", 1).rsplit("*", 1)[0] + "*00") is None
          and am.parse_sentence(S("GPRMC,120001.00,V,3327.1260,N,11200.1020,W,,,081126,,,N")) is None,
          "RMC with status V is dropped")
    rd = am.parse_sentence(GLL)
    check(rd is not None and rd.kind == "GLL" and rd.name == "GNGLL" and near(rd.values["lat_deg"], 33.4521)
          and rd.tod_s == 12 * 3600 + 1, "GLL from the GN talker: position and time")
    check(am.parse_sentence(S("GPGLL,3327.1260,N,11200.1020,W,120001.00,V,N")) is None, "GLL status V dropped")
    rd = am.parse_sentence(VTG)
    check(rd is not None and rd.kind == "VTG" and rd.values["heading_deg"] == 87.0
          and rd.values["heading_mag_deg"] == 77.5 and near(rd.values["speed_mps"], 141.9 * KT),
          "VTG true, magnetic and speed from the knots field")
    rd = am.parse_sentence(S("GPVTG,087.0,T,,M,,N,262.8,K,A"))
    check(rd is not None and near(rd.values["speed_mps"], 262.8 / 3.6, 1e-6) and "heading_mag_deg" not in rd.values,
          "VTG falls back to km/h, blank magnetic stays out")
    check(not rd.has_position, "VTG carries no position")
    rd = am.parse_sentence(GSA)
    check(rd is not None and rd.values["fix_mode"] == "3D" and rd.values["sats_used"] == 8
          and (rd.values["pdop"], rd.values["hdop"], rd.values["vdop"]) == (1.6, 0.9, 1.3), "GSA mode, count, DOP")
    rd = am.parse_sentence(S("GAGSA,A,2,02,03,04,,,,,,,,,,2.5,1.8,1.7"))
    check(rd is not None and rd.values["fix_mode"] == "2D" and rd.values["sats_used"] == 3, "GSA 2D from GA talker")
    rd = am.parse_sentence(S("GLGSA,A,1,,,,,,,,,,,,,,,"))
    check(rd is not None and rd.values["fix_mode"] == "none" and rd.values["sats_used"] == 0, "GSA no fix")
    rd = am.parse_sentence(GSV)
    check(rd is not None and rd.values == {"sats_view": 11}, "GSV gives the count in view only")
    check(am.parse_sentence(S("GPZDA,120001.00,08,11,2026,00,00")) is None, "an unread kind gives None")
    check(am.parse_sentence("not a sentence") is None and am.parse_sentence("$") is None, "junk gives None")


def test_bad_checksums():
    bad = GGA[:-2] + "00"
    check(am.parse_sentence(bad) is None, "GGA with a wrong checksum is dropped")
    for line in (RMC, GLL, VTG, GSA, GSV):
        wrong = line[:-1] + ("0" if line[-1] != "0" else "1")
        check(am.parse_sentence(wrong) is None, f"{line[1:6]} with a wrong checksum is dropped")
    check(am.parse_sentence(GGA.rsplit("*", 1)[0]) is not None, "a sentence with no checksum is accepted")
    check(am.parse_sentence(GGA[:-2] + "zz") is None, "a non hex checksum is dropped")
    data = (GGA + "\r\n" + VTG[:-2] + "00" + "\r\n" + GSA + "\r\n").encode()
    names, nbad = am.sentences_in(data)
    check(names == ["GPGGA", "GNGSA"] and nbad == 1, "sentences_in counts the bad line")
    lst = am.MulticastPosListener()
    pos = lst.feed(data, t_mono=10.0)
    check(pos is not None and pos.heading_deg is None and pos.fix_mode == "3D" and lst.bad_sentences == 1,
          "a bad VTG in a burst leaves the rest in the fix")


# the combine window

def test_combine_window():
    c = am.FixCombiner(window_s=1.5)
    c.add(am.parse_sentence(GGA), 100.0)
    c.add(am.parse_sentence(VTG), 100.1)
    c.add(am.parse_sentence(GSA), 100.2)
    c.add(am.parse_sentence(GSV), 100.3)
    p = c.fix(100.3)
    check(p is not None and near(p.lat_deg, 33.4521) and p.alt_m == 1240.0, "position and altitude from GGA")
    check(p.heading_deg == 87.0 and p.heading_mag_deg == 77.5 and near(p.speed_mps, 141.9 * KT),
          "heading and speed from VTG")
    check(p.fix_mode == "3D" and p.pdop == 1.6 and p.sats_used == 8 and p.sats_view == 11, "GSA and GSV fields")
    check(p.sentences == ["GPGGA", "GPVTG", "GNGSA", "GPGSV"], f"the fix says which sentences fed it: {p.sentences}")
    check(p.source == "nmea:GPGGA+GPVTG+GNGSA+GPGSV", f"source lists them: {p.source}")
    check(p.field_src["heading_deg"] == "GPVTG" and p.field_src["fix_mode"] == "GNGSA"
          and p.field_src["alt_m"] == "GPGGA" and p.field_src["sats_used"] == "GNGSA", "field sources")
    check(p.field_t["heading_deg"] == 100.1 and p.field_age_s("heading_deg", 101.1) == 1.0, "field ages")
    check(p.field_src["hdop"] == "GNGSA", "newest value wins: HDOP from GSA, which came after GGA")
    # GGA only in the next burst, VTG missing: heading drops out after the window
    c.add(am.parse_sentence(ed(GGA, 1, "120002.00")), 101.0)
    p = c.fix(101.0)
    check(p.heading_deg == 87.0, "VTG 0.9 s old is still in the fix")
    c.add(am.parse_sentence(ed(GGA, 1, "120003.00")), 102.0)
    p = c.fix(102.0)
    check(p is not None and p.heading_deg is None and p.speed_mps is None and p.fix_mode is None,
          "VTG and GSA 1.9 s old have dropped out, the position stays")
    check(p.sentences == ["GPGGA"] and p.source == "nmea:GPGGA", "only GGA is left in the source")
    # the window is settable
    c.set_window(3.0)
    c.add(am.parse_sentence(VTG), 102.5)
    c.add(am.parse_sentence(ed(GGA, 1, "120005.00")), 105.0)
    check(c.fix(105.0).heading_deg == 87.0, "with a 3 s window the 2.5 s old VTG still counts")
    check(c.fix(109.0) is None, "a position older than the window gives no fix")
    # split over datagrams, through the listener
    lst = am.MulticastPosListener()
    check(lst.window_s == 1.5, "listener default window 1.5 s")
    lst.set_window(2.0)
    check(lst.window_s == 2.0, "listener window settable")
    p1 = lst.feed((GGA + "\r\n").encode(), t_mono=200.0)
    check(p1 is not None and p1.heading_deg is None, "GGA alone: a fix with no heading")
    p2 = lst.feed((VTG + "\r\n").encode(), t_mono=200.2)
    check(p2 is not None and p2.heading_deg == 87.0 and near(p2.lat_deg, 33.4521),
          "VTG in its own datagram joins the GGA position")
    check(lst.latest is p2 and lst.parsed == 2 and lst.bad == 0, "a heading only datagram counts as parsed")
    p3 = lst.feed((GSA + "\r\n" + GSV + "\r\n").encode(), t_mono=200.4)
    check(p3.fix_mode == "3D" and p3.sats_view == 11 and p3.heading_deg == 87.0, "GSA and GSV join too")
    none = lst.feed((VTG + "\r\n").encode(), t_mono=205.0)
    check(none is None and lst.bad == 1, "a VTG with no position inside the window is not a fix")
    # the date from an RMC in the window completes a GGA time of day
    c = am.FixCombiner()
    c.add(am.parse_sentence(RMC), 300.0)
    c.add(am.parse_sentence(ed(GGA, 1, "120002.00")), 300.5)
    p = c.fix(300.5)
    check(p.t_feed_utc == DAY0 + 12 * 3600 + 2 and p.field_src["lat_deg"] == "GPGGA",
          "GGA wins the position (later time of day) and takes the RMC date")


def test_position_disagreement():
    gga_a = GGA
    rmc_b = S("GPRMC,120001.00,A,3327.2000,N,11200.1020,W,141.9,087.0,081126,,,A")   # same tod, other place
    gll_c = S("GPGLL,3327.3000,N,11200.1020,W,120001.00,A,A")
    # same time of day: GGA over RMC over GLL whatever the order
    c = am.FixCombiner()
    c.add(am.parse_sentence(gll_c), 1.0)
    c.add(am.parse_sentence(rmc_b), 1.1)
    c.add(am.parse_sentence(gga_a), 1.2)
    check(near(c.fix(1.2).lat_deg, 33.4521) and c.fix(1.2).field_src["lat_deg"] == "GPGGA", "GGA last wins")
    c = am.FixCombiner()
    c.add(am.parse_sentence(gga_a), 1.0)
    c.add(am.parse_sentence(rmc_b), 1.1)
    c.add(am.parse_sentence(gll_c), 1.2)
    check(near(c.fix(1.2).lat_deg, 33.4521), "GGA first still wins over RMC and GLL at the same time")
    c = am.FixCombiner()
    c.add(am.parse_sentence(gll_c), 1.0)
    c.add(am.parse_sentence(rmc_b), 1.1)
    check(near(c.fix(1.1).lat_deg, 33.4533333, 1e-5) and c.fix(1.1).field_src["lat_deg"] == "GPRMC",
          "RMC over GLL")
    # a later time of day wins over the rank
    rmc_later = S("GPRMC,120002.00,A,3327.2000,N,11200.1020,W,141.9,087.0,081126,,,A")
    c = am.FixCombiner()
    c.add(am.parse_sentence(gga_a), 2.0)
    c.add(am.parse_sentence(rmc_later), 2.1)
    p = c.fix(2.1)
    check(near(p.lat_deg, 33.4533333, 1e-5) and p.field_src["lat_deg"] == "GPRMC", "RMC with a later time wins")
    check(p.alt_m == 1240.0, "altitude from the GGA stays with the RMC position")
    gll_later = S("GPGLL,3327.3000,N,11200.1020,W,120003.00,A,A")
    c.add(am.parse_sentence(gll_later), 2.2)
    check(near(c.fix(2.2).lat_deg, 33.455, 1e-5), "GLL with a later time wins over both")
    c.add(am.parse_sentence(gga_a), 2.3)
    check(near(c.fix(2.3).lat_deg, 33.455, 1e-5), "an earlier GGA arriving late does not take the position back")
    # same sentence kind twice: the newest value wins
    c = am.FixCombiner()
    c.add(am.parse_sentence(gga_a), 3.0)
    c.add(am.parse_sentence(ed(GGA, 2, "3327.5000")), 3.5)
    check(near(c.fix(3.5).lat_deg, 33.4583333, 1e-5), "a second GGA with the same time replaces the first")
    # no time of day on either: rank, then newest
    nt_rmc = S("GPRMC,,A,3327.2000,N,11200.1020,W,141.9,087.0,081126,,,A")
    nt_gll = S("GPGLL,3327.3000,N,11200.1020,W,,A,A")
    c = am.FixCombiner()
    c.add(am.parse_sentence(nt_rmc), 4.0)
    c.add(am.parse_sentence(nt_gll), 4.1)
    check(near(c.fix(4.1).lat_deg, 33.4533333, 1e-5), "no time on either: RMC outranks GLL")
    # past midnight: 23:59:59 then 00:00:00 is later
    c = am.FixCombiner()
    c.add(am.parse_sentence(S("GPGGA,235959.00,3327.1260,N,11200.1020,W,1,08,0.9,1240.0,M,0.0,M,,")), 5.0)
    c.add(am.parse_sentence(S("GPRMC,000000.00,A,3327.2000,N,11200.1020,W,141.9,087.0,091126,,,A")), 5.5)
    check(near(c.fix(5.5).lat_deg, 33.4533333, 1e-5), "the first fix after midnight counts as later")


# JSON

def test_json_fields_and_units():
    p = am.parse_auto(b'{"lat": 33.4, "lon": -112.0, "alt": 1500, "heading": 87, "speed": 73}')
    check(p is not None and p.heading_deg == 87.0 and p.speed_mps == 73.0, "heading and speed in m/s")
    check(p.field_src["heading_deg"] == "json" and p.sentences == ["json"] and p.source == "json",
          "json fields say json")
    for key, scale in (("speed_kt", KT), ("speed_kts", KT), ("speed_knots", KT), ("sog_kt", KT), ("gs_kt", KT),
                       ("speed_kmh", 1 / 3.6), ("speed_kph", 1 / 3.6), ("sog", 1.0), ("gs", 1.0),
                       ("ground_speed", 1.0), ("spd", 1.0), ("speed_mps", 1.0)):
        p = am.parse_auto(('{"lat": 1, "lon": 2, "%s": 100}' % key).encode())
        check(near(p.speed_mps, 100 * scale), f"{key} in the right unit")
    for key in ("hdg", "course", "cog", "track", "heading_deg", "course_deg", "track_deg"):
        p = am.parse_auto(('{"lat": 1, "lon": 2, "%s": 370}' % key).encode())
        check(p.heading_deg == 10.0, f"{key} is a heading, wrapped to 0 to 360")
    p = am.parse_auto(b'{"lat": 1, "lon": 2, "heading": 87, "heading_mag": 77.5}')
    check(p.heading_mag_deg == 77.5, "magnetic heading key")
    for val, want in (('"3D"', "3D"), ('"2d"', "2D"), ("3", "3D"), ("2", "2D"), ("1", "none"), ('"none"', "none"),
                      ('"no fix"', "none")):
        p = am.parse_auto(('{"lat": 1, "lon": 2, "fix_mode": %s}' % val).encode())
        check(p.fix_mode == want, f"fix_mode {val} reads as {want}")
    p = am.parse_auto(b'{"lat": 1, "lon": 2, "fix": "3D", "fix_quality": 2, "hdop": 0.8, "pdop": 1.5, '
                      b'"vdop": 1.2, "sats": 9, "sats_in_view": 14}')
    check(p.fix_mode == "3D" and p.fix_quality == 2 and p.hdop == 0.8 and p.pdop == 1.5 and p.vdop == 1.2
          and p.sats_used == 9 and p.sats_view == 14, "fix, quality, DOP and satellites")
    p = am.parse_auto(b'{"lat": 1, "lon": 2, "numsv": 7, "satellites_in_view": 12, "quality": 1}')
    check(p.sats_used == 7 and p.sats_view == 12 and p.fix_quality == 1, "other satellite and quality keys")
    p = am.parse_auto(b'{"lat": 33.4, "lon": -112.0}')
    check(p is not None and p.heading_deg is None and p.speed_mps is None and p.fix_mode is None
          and p.sats_used is None, "position only works as before")
    p = am.parse_auto(b'{"lat": 1, "lon": 2, "speed": "fast"}')
    check(p is not None and p.speed_mps is None, "a speed that is not a number is left out")
    # the sender's JSON parses back with heading and speed
    p = am.parse_auto(fs.datagram(-112.0, 33.4, 1500.0, "json", 1794182400.0, 87.0, 73.0))
    check(p.heading_deg == 87.0 and p.speed_mps == 73.0 and p.fix_mode == "3D" and p.sats_used == 8,
          "sender JSON round trip")


# GUI text, rows, rates

def test_text_helpers_and_rows():
    lst = am.MulticastPosListener()
    data = ("\r\n".join([GGA, RMC, VTG, GSA, GSV]) + "\r\n").encode()
    p = lst.feed(data, t_wall=DAY0 + 12 * 3600 + 1.2, t_mono=500.0)
    check(am.summary_text(p) == "+33.452100 -112.001700 alt 1240 m, hdg 087, 142 kt"
          or am.summary_text(p) == "+33.45210 -112.00170 alt 1240 m, hdg 087, 142 kt",
          f"summary text: {am.summary_text(p)}")
    check(am.heading_text(p) == "hdg 087" and am.speed_text(p) == "142 kt", "heading and speed text")
    q = am.parse_auto(b'{"lat": 33.4, "lon": -112.0}')
    check(am.summary_text(q) == "+33.40000 -112.00000 alt -- m" and am.heading_text(q) == "", "no heading, no text")
    rows = am.fix_rows(p, now_mono=501.0, now_wall=DAY0 + 12 * 3600 + 2.2)
    by = {r["field"]: r for r in rows}
    check(by["heading (true)"]["value"] == "087.0 deg" and by["heading (true)"]["source"] == "GPVTG"
          and by["heading (true)"]["age"] == "1.0 s", f"heading row: {by['heading (true)']}")
    check(by["heading (magnetic)"]["value"] == "077.5 deg", "magnetic row")
    check(by["ground speed"]["value"].startswith("141.9 kt (73.0 m/s)"), f"speed row: {by['ground speed']}")
    check(by["fix mode"]["value"] == "3D" and by["fix mode"]["source"] == "GNGSA", "fix mode row")
    check(by["fix quality"]["value"] == "1 GPS" and by["satellites used"]["value"] == "8"
          and by["satellites in view"]["value"] == "11", "quality and satellite rows")
    check(by["feed time"]["value"].startswith("12:00:01.000Z (-0.200 s vs GCS)"), f"feed time row: {by['feed time']}")
    check(by["arrived (GCS clock)"]["source"] == p.source, "arrival row carries the source")
    empty = am.fix_rows(None)
    check(len(empty) == len(am.FIX_FIELDS) + 2 and all(r["value"] == "--" for r in empty), "no fix: every row empty")
    rows = am.fix_rows(q, now_mono=10.0)
    byq = {r["field"]: r for r in rows}
    check(byq["heading (true)"]["value"] == "--" and byq["latitude"]["source"] == "json", "json rows")
    # sentence rates: GGA, RMC, VTG at 1 Hz for 10 s, GSA and GSV every 5 s
    lst = am.MulticastPosListener()
    for i in range(10):
        lines = [GGA, RMC, VTG] + ([GSA, GSV] if i % 5 == 0 else [])
        lst.feed(("\r\n".join(lines) + "\r\n").encode(), t_mono=1000.0 + i)
    r = lst.sentence_rates(window_s=10.0, now_mono=1009.5)
    check(near(r["GPGGA"]["hz"], 1.0, 0.11) and r["GPGGA"]["count"] == 10, f"GGA about 1 Hz: {r['GPGGA']}")
    check(near(r["GNGSA"]["hz"], 0.2, 0.03) and r["GNGSA"]["count"] == 2, f"GSA about 0.2 Hz: {r['GNGSA']}")
    txt = am.sentence_rates_text(r)
    check(txt.startswith("GPGGA 1.") and "GLL none" in txt and "GNGSA 0.2" in txt, f"rates line: {txt}")
    r = lst.sentence_rates(window_s=10.0, now_mono=1025.0)
    check(r["GPVTG"]["hz"] == 0.0 and near(r["GPVTG"]["age_s"], 16.0), "a stopped sentence has hz 0 and its age")
    txt = am.sentence_rates_text(r)
    check("GPVTG last 16 s ago" in txt, f"stopped sentence in the line: {txt}")
    check(am.sentence_rates_text({}) == "GGA none, RMC none, GLL none, VTG none, GSA none, GSV none",
          "nothing seen: every kind none")


# planner row and marker

class FakeListener:
    def __init__(self):
        self.group, self.port, self.iface_ip, self.fmt = "239.2.1.1", 5005, "0.0.0.0", "auto"
        self.running = True
        self.latest = None
        self.rx = self.parsed = self.bad = 0

    def rate_hz(self, window_s=10.0, now_mono=None):
        return 1.0

    def feed_offset_s(self, n=20):
        return None


def test_planner_feed_row_and_marker():
    import time
    now_m = time.monotonic()
    lst = FakeListener()
    data = ("\r\n".join([GGA, RMC, VTG, GSA, GSV]) + "\r\n").encode()
    pos = am.parse_auto(data, t_mono=now_m - 0.5)
    pos.t_mono = now_m - 0.5
    lst.latest = pos
    lst.parsed = lst.rx = 1
    motion = SimpleNamespace(state="stopped", fix=lambda: None)
    sess = SimpleNamespace(listener=lst, motion=motion, pos_group="239.2.1.1", pos_port=5005, pos_iface="0.0.0.0")
    sess.feed = PositionFeed(None, lst, motion=motion)
    st = fst.feed_state(sess, now_mono=now_m)
    check(st["level"] == "ok" and st["heading_deg"] == 87.0 and near(st["speed_mps"], 141.9 * KT)
          and st["fix_mode"] == "3D" and st["sats_used"] == 8, f"feed state carries heading, speed, fix: {st}")
    check(st["sentences"] == ["GPGGA", "GPRMC", "GPVTG", "GNGSA", "GPGSV"], "feed state carries the sentences")
    txt = fst.feed_state_text(st)
    check(txt.endswith("; hdg 087, 142 kt, 3D 8 sats"), f"row text ends with heading and speed: {txt}")
    fix = sess.feed.current()
    ex = fst.fix_extras(sess, fix)
    check(ex["heading_deg"] == 87.0, "fix_extras reads the listener beside the triggers' Fix")
    check(fst.fix_extras(sess, tg.Fix(1.0, 2.0, t=now_m, source="G40 GPS")) == {}, "a radio GPS fix has no extras")
    check(fst.fix_extras(sess, None) == {}, "no fix, no extras")
    m = me.position_marker(fix, valid=True, heading_deg=ex["heading_deg"])
    check(m["heading"] == 87.0 and m["label"] == "platform hdg 087", f"marker with heading: {m}")
    m = me.position_marker(fix, valid=False, heading_deg=367.0)
    check(m["heading"] == 7.0 and m["label"] == "platform (stale) hdg 007", "stale marker wraps the heading")
    m = me.position_marker(fix, valid=True)
    check("heading" not in m and m["label"] == "platform", "no heading: the dot as before")
    check(me.position_marker(fix, heading_deg="x") == me.position_marker(fix), "a bad heading is ignored")
    # without heading the row reads as before
    lst.latest = am.parse_auto(b'{"lat": 33.4521, "lon": -112.0017}', t_mono=now_m - 0.5)
    lst.latest.t_mono = now_m - 0.5
    st = fst.feed_state(sess, now_mono=now_m)
    check(st["heading_deg"] is None and "hdg" not in fst.feed_state_text(st), "no heading: no heading text")
    check(len(fix.__dataclass_fields__) == 5, "the triggers' Fix is unchanged: lat, lon, alt, t, source")


# the sender's burst

def test_sender_burst_and_drop():
    lines = am.nmea_burst(33.4521, -112.0017, 1240.0, DAY0 + 12 * 3600 + 1, heading_deg=87.0, speed_mps=73.0)
    kinds = [ln[3:6] for ln in lines]
    check(kinds == ["GGA", "RMC", "VTG", "GSA", "GSV"], f"the full burst in order: {kinds}")
    check(all(am.parse_sentence(ln) is not None for ln in lines), "every sentence of the burst parses")
    p = am.parse_auto(("\r\n".join(lines) + "\r\n").encode())
    check(near(p.lat_deg, 33.4521, 1e-5) and p.heading_deg == 87.0 and near(p.speed_mps, 73.0, 0.05)
          and p.fix_mode == "3D" and p.sats_view == 11 and p.alt_m == 1240.0, "the burst parses back whole")
    check(p.t_feed_utc == DAY0 + 12 * 3600 + 1, "the burst's time is the one given (RMC date)")
    kinds = [ln[3:6] for ln in am.nmea_burst(1, 2, 3, drop="VTG")]
    check(kinds == ["GGA", "RMC", "GSA", "GSV"], "drop VTG")
    kinds = [ln[3:6] for ln in am.nmea_burst(1, 2, 3, drop=["rmc", "VTG"])]
    check(kinds == ["GGA", "GSA", "GSV"], "drop a list, any case")
    kinds = [ln[3:6] for ln in am.nmea_burst(1, 2, 3, aux=False)]
    check(kinds == ["GGA", "RMC", "VTG"], "aux False leaves out GSA and GSV")
    lines = am.nmea_burst(1, 2, 3, heading_deg=None, speed_mps=10.0)
    p = am.parse_auto(("\r\n".join(lines) + "\r\n").encode())
    check(p is not None and p.heading_deg is None and near(p.speed_mps, 10.0, 0.05), "no heading: blank courses, speed kept")
    # drop VTG: the heading still comes from RMC
    dg = fs.datagram(-112.0017, 33.4521, 1240.0, "nmea", None, 87.0, 73.0, drop="VTG")
    p = am.parse_auto(dg)
    check(p.heading_deg == 87.0 and p.field_src["heading_deg"] == "GPRMC", "VTG dropped: heading from RMC")
    dg = fs.datagram(-112.0017, 33.4521, 1240.0, "nmea", None, 87.0, 73.0, drop="RMC,VTG")
    p = am.parse_auto(dg)
    check(p is not None and p.heading_deg is None and p.speed_mps is None, "RMC and VTG dropped: no heading")
    dgs = fs.datagrams(-112.0017, 33.4521, 1240.0, "nmea", None, 87.0, 73.0, split=True)
    check(len(dgs) == 5 and all(d.count(b"$") == 1 for d in dgs), "split: one sentence per datagram")
    lst = am.MulticastPosListener()
    for i, d in enumerate(dgs):
        last = lst.feed(d, t_mono=50.0 + i * 0.05)
    check(last.heading_deg == 87.0 and last.sats_view == 11, "the split burst combines in the listener")
    check(near(fs.bearing_deg(33.4, -112.0, 33.5, -112.0), 0.0, 1e-6) and near(fs.bearing_deg(33.4, -112.0, 33.4, -111.9), 90.0, 0.1)
          and near(fs.bearing_deg(33.5, -112.0, 33.4, -112.0), 180.0, 1e-6), "bearings north, east, south")
    steps = list(fs.interpolate_heading([[-112.0, 33.4], [-112.0, 33.5]], 1000.0, 1.0))
    check(all(near(h, 0.0, 1e-6) for _, _, h in steps) and len(steps) == 13, "heading along a leg")
    ps = fs.PathSender("239.2.1.1", 5995, "127.0.0.1", fmt="nmea", drop="VTG", heading=False, aux_every=5, split=True)
    check(ps.drop == ["VTG"] and ps.heading is False and ps.aux_every == 5 and ps.split, "sender options kept")


# the log columns

def test_position_log_columns():
    check(gl.HEADERS["position"] == ("gcs_time", "t_unix", "lat", "lon", "alt_m", "source",
                                     "heading_deg", "speed_kt", "fix_mode", "sats", "aircraft"),
          "position header: the new columns after the old ones, the aircraft name last")
    check(gl.header("position", 0)[0] == "gcs_time (UTC)" and gl.header("position", 0)[1] == "t_unix",
          "gcs_time and t_unix stay first")
    out = os.path.join(_TMP, "out")
    data = ("\r\n".join([GGA, RMC, VTG, GSA, GSV]) + "\r\n").encode()
    pos = am.parse_auto(data)
    host = SimpleNamespace(serial="G40-SIM", status=SimpleNamespace(flags=0), connected=True, config={},
                           detections={}, det_head=0, watch_bands=[])
    glogs = gl.GroundLogs(lambda: {"G40-SIM": host}, lambda: pos, str, out_dir=out)
    glogs.get("G40-SIM").start("ground", {"position": True}, 0, now=DAY0 + 1)
    glogs.tick(now=DAY0 + 1)
    with open(glob.glob(os.path.join(out, "position_*.csv"))[0], newline="") as fp:
        rows = list(csv.reader(fp))
    check(rows[0] == gl.header("position", 0), "header row")
    check(rows[1][5:] == ["acft_mcast", "87.0", "141.9", "3D", "8", ""], f"feed row columns: {rows[1]}")
    check(len(rows[1]) == len(gl.HEADERS["position"]), "row length matches the header")
    pos2 = am.parse_auto(b'{"lat": 33.4, "lon": -112.0}')
    glogs.get_acft = lambda: pos2
    glogs.tick(now=DAY0 + 2)
    with open(glob.glob(os.path.join(out, "position_*.csv"))[0], newline="") as fp:
        rows = list(csv.reader(fp))
    check(rows[2][5:] == ["acft_mcast", "", "", "", "", ""], f"no heading: blank columns: {rows[2]}")
    glogs.get_acft = lambda: None
    glogs.tick(now=DAY0 + 3)
    with open(glob.glob(os.path.join(out, "position_*.csv"))[0], newline="") as fp:
        rows = list(csv.reader(fp))
    check(rows[3][5] == "no_fix" and len(rows[3]) == len(gl.HEADERS["position"]), "no_fix row keeps the width")
    glogs.get("G40-SIM").stop()


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{_passed} passed, {_failed} failed, {len(tests)} test functions")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
