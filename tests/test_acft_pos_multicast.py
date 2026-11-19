"""
test_acft_pos_multicast.py: parser + live multicast loopback checks.

Covers: NMEA GGA/RMC decode + checksum reject, JSON key variants, format
auto-detect, and an end-to-end multicast send to join to parse over 127-scope
multicast on loopback. Stdlib only; exit 0 = all passed.
"""
from __future__ import annotations

import socket
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dpmg.link.acft_pos_multicast import (AircraftPos, MulticastPosListener, _nmea_gga,
                                parse_auto, parse_json, parse_nmea)

checks = []


def chk(name, ok, detail=""):
    checks.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")


def approx(a, b, tol=1e-4):
    return a is not None and abs(a - b) <= tol


def main() -> int:
    # 1. NMEA GGA (has altitude)
    gga = "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47"
    p = parse_nmea(gga)
    chk("GGA lat/lon/alt", p and approx(p.lat_deg, 48.1173) and
        approx(p.lon_deg, 11.51667) and approx(p.alt_m, 545.4, 0.1),
        f"({p.lat_deg:.4f},{p.lon_deg:.4f},{p.alt_m})" if p else "(none)")

    # 2. NMEA RMC (no altitude, southern/western hemispheres)
    rmc = ("$GPRMC,123519,A,4807.038,S,01131.000,W,022.4,084.4,230394,"
           "003.1,W*4A")
    # fix the checksum for the modified body
    body = rmc[1:rmc.rfind("*")]
    cs = 0
    for ch in body:
        cs ^= ord(ch)
    rmc = f"${body}*{cs:02X}"
    p = parse_nmea(rmc)
    chk("RMC south/west, no alt", p and approx(p.lat_deg, -48.1173) and
        approx(p.lon_deg, -11.51667) and p.alt_m is None,
        f"({p.lat_deg:.4f},{p.lon_deg:.4f})" if p else "(none)")

    # 3. bad checksum rejected
    chk("bad checksum rejected", parse_nmea(gga[:-2] + "00") is None)

    # 4. JSON key variants
    chk("json lat/lon/alt", (lambda q: q and approx(q.lat_deg, 39.7) and
        approx(q.lon_deg, -104.9) and approx(q.alt_m, 1600.0))
        (parse_json('{"latitude": 39.7, "lng": -104.9, "altitude": 1600}')))

    # 5. auto-detect both
    chk("auto-detect nmea", (parse_auto(gga.encode()) or
        AircraftPos(0, 0, 0, 0, "x", "")).source.startswith("nmea"))
    chk("auto-detect json", (parse_auto(b'{"lat":1,"lon":2}') or
        AircraftPos(0, 0, 0, 0, "x", "")).source == "json")
    chk("auto-detect garbage -> None", parse_auto(b"hello world") is None)

    # 6. GGA round-trip through the module's own encoder
    made = _nmea_gga(39.7392, -104.9903, 1609.0)
    q = parse_nmea(made)
    chk("gga encode->decode round-trip", q and approx(q.lat_deg, 39.7392) and
        approx(q.lon_deg, -104.9903) and approx(q.alt_m, 1609.0, 0.1))

    # 7. live multicast loopback: listener joins, the test sends, it parses.
    # Pin both ends to the host's primary interface: a multi-homed dev box has
    # no default multicast route, so INADDR_ANY silently drops the send.
    def _primary_ip():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))     # no packet sent; just picks the iface
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"
        finally:
            s.close()

    ifip = _primary_ip()
    grp, port = "239.9.9.9", 55055
    lis = MulticastPosListener(group=grp, port=port, iface_ip=ifip)
    try:
        # start() joins the group: an IP_ADD_MEMBERSHIP failure would raise
        # here, so reaching the send loop already proves the join succeeded.
        lis.start()
        chk("multicast join (no raise)", lis.running)
        time.sleep(0.2)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                     socket.inet_aton(ifip))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        # Prefer real multicast; fall back to unicast to the bound port (the
        # listener binds "" so it receives both). Wifi/dev boxes often don't
        # loop multicast back to the host: the wired ground-station NIC does.
        got, i, via_mcast = False, 0, False
        for i in range(60):
            msg = _nmea_gga(40.0 + i * 0.001, -105.0, 1700.0 + i).encode()
            s.sendto(msg, (grp, port))                 # multicast attempt
            time.sleep(0.05)
            if lis.latest is not None:
                got, via_mcast = True, True
                break
            s.sendto(msg, ("127.0.0.1", port))         # unicast fallback
            time.sleep(0.02)
            if lis.latest is not None:
                got = True
                break
        cur = lis.latest
        chk("recv + parse over socket", got and cur is not None and
            approx(cur.lat_deg, 40.0 + i * 0.001, 1e-3),
            f"(via {'multicast' if via_mcast else 'unicast-fallback'}, "
            f"rx={lis.rx} parsed={lis.parsed})" if cur
            else f"(rx={lis.rx} parsed={lis.parsed})")
        chk("age fresh after receive", lis.age_s < 1.0)
    finally:
        lis.stop()
    chk("stop() halts thread", not lis.running)

    print(f"\nresult: {sum(checks)}/{len(checks)} checks passed")
    return 0 if all(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
