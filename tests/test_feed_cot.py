"""Cursor on Target XML on the aircraft position feed (acft_pos_multicast).

The sample is the shape captured from the aircraft system on 2026-11-17: one
<event> per datagram, each with its own <?xml ...?> line.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dpmg.link import acft_pos_multicast as m  # noqa: E402
from dpmg.link import feed_settings  # noqa: E402

EVENT = (
    '<?xml version="1.0" standalone="yes"?>\n'
    '<event uid="{uid}" version="2.0" how="m-g" time="2026-11-17T15:24:{sec}Z" qos="0-r-c" '
    'stale="2026-11-17T15:26:{sec}Z" type="a-f-A-M-F-Q-r" start="2026-11-17T15:24:{sec}Z">\n'
    '  <detail>\n'
    '    <_flow-tags_ insitu-icomc2-000000ff="2026-11-17T15:24:{sec}Z" />\n'
    '    <track course="322.9" speed="0.6" />\n'
    '    <spatial>\n'
    '      <attitude roll="-0.71" pitch="0.71" yaw="141.88" />\n'
    '      <spin roll="-0.94" pitch="-2.25" yaw="2" />\n'
    '    </spatial>\n'
    '    <remarks />\n'
    '    <status version="0.2" />\n'
    '    <uid />\n'
    '  </detail>\n'
    '  <point lat="{lat}" lon="{lon}" hae="1475.94" le="9999999" ce="9999999" />\n'
    '</event>')


def event(sec="24.97", uid="Blackjack.PPLI.1162", lat="43.598640326782757", lon="-112.90564738514499"):
    return EVENT.format(sec=sec, uid=uid, lat=lat, lon=lon).encode()


def test_one_event():
    pos = m.parse_auto(event())
    assert pos is not None
    assert abs(pos.lat_deg - 43.598640326782757) < 1e-9
    assert abs(pos.lon_deg + 112.90564738514499) < 1e-9
    assert abs(pos.alt_m - 1475.94) < 1e-6
    assert abs(pos.heading_deg - 322.9) < 1e-6
    assert abs(pos.speed_mps - 0.6) < 1e-6
    assert pos.source == "cot:Blackjack.PPLI.1162"
    assert pos.sentences == ["cot"]
    assert abs(pos.t_feed_utc - m.iso_time_utc("2026-11-17T15:24:24.97Z")) < 1e-6


def test_time_with_two_decimals():
    # datetime.fromisoformat refuses two decimals before Python 3.11
    t = m.iso_time_utc("2026-11-17T15:24:24.97Z")
    assert abs(t - 1794929064.97) < 1e-6
    assert m.iso_time_utc("2026-11-17T08:24:24-07:00") == 1794929064.0
    assert m.iso_time_utc("not a time") is None


def test_events_back_to_back():
    data = event("24.97") + event("25.97") + event("26.99", lat="43.6")
    pos = m.parse_auto(data)
    assert abs(pos.lat_deg - 43.6) < 1e-9
    assert len(m.cot_readings(data.decode())) == 3


def test_format_setting():
    assert "xml" in m.FORMATS and "xml" in feed_settings.FORMATS
    assert feed_settings._fmt("cot") == "xml" and feed_settings._fmt("XML") == "xml"
    for name in ("xml", "cot"):
        assert m.parser_for(name)(event()) is not None
        assert m.parser_for(name)(b'{"lat": 43.5, "lon": -112.9}') is None
    assert m.parser_for("json")(event()) is None
    assert m.parser_for("nmea")(event()) is None


def test_other_formats_unchanged():
    assert m.parse_auto(b'{"lat": 43.5, "lon": -112.9}').source == "json"
    line = "GPGGA,152424.00,4335.9184,N,11254.3388,W,1,08,0.9,1475.9,M,,M,,"
    pos = m.parse_auto(f"${line}*{m.nmea_checksum(line)}".encode())
    assert pos is not None and pos.source == "nmea:GPGGA"


def test_no_position_marker():
    assert m.parse_auto(event(lat="0", lon="0")) is None
    assert m.parse_auto(event(lat="91")) is None


def test_bad_xml_dropped():
    assert m.parse_auto(b"<event uid='x'><point lat='43.5' lon='-112.9'></event>") is None
    assert m.parse_auto(b'<!DOCTYPE x [<!ENTITY a "b">]>' + event()) is None
    assert m.parse_auto(b"<html><body>no</body></html>") is None


def test_uid_filter():
    two = event(uid="Other.PPLI.7", lat="40.0") + event()
    assert m.parse_auto(two, cot_uid="Blackjack.PPLI.1162").lat_deg > 43.0
    assert m.parse_auto(two, cot_uid="Other.PPLI.7").lat_deg == 40.0
    assert m.parse_auto(two, cot_uid="Nobody") is None
    assert m.cot_uids(two) == ["Other.PPLI.7", "Blackjack.PPLI.1162"]


def test_listener_takes_cot():
    lis = m.MulticastPosListener(fmt="auto")
    lis.cot_uid = ""
    assert lis.feed(event("24.97"), t_mono=10.0) is not None
    assert lis.feed(event("25.97", uid="Other.PPLI.7", lat="40.0"), t_mono=11.0) is not None
    assert lis.cot_uids_seen(now_mono=11.5) == ["Blackjack.PPLI.1162", "Other.PPLI.7"]
    lis.cot_uid = "Blackjack.PPLI.1162"
    assert lis.feed(event("26.97", uid="Other.PPLI.7", lat="40.0"), t_mono=20.0) is None
    pos = lis.feed(event("27.97"), t_mono=21.0)
    assert pos is not None and pos.lat_deg > 43.0 and lis.latest is pos


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
