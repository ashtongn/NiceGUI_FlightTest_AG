"""Test sender and recorder for the aircraft position feed.

The sender plays a path ([lon, lat] pairs) on the multicast group as JSON
objects or NMEA bursts (GGA, RMC, VTG, GSA, GSV), the same forms
tools/sim_position.py and the aircraft use, with a time stamp in each, so the
listener's feed-time check has something to read. The heading is the path
direction and the speed the one set, so the GUI can be shown with heading and
speed; drop names sentences to leave out (VTG, say) to show it without. It can
also replay a recording made by PacketRecorder (a .jsonl of datagrams with
their arrival times) at the recorded spacing. It runs in its own thread;
start() and stop() are what the engineering panel calls.

Nothing here touches a radio.
"""

from __future__ import annotations

import json
import math
import os
import socket
import threading
import time
from typing import Optional

from .acft_pos_multicast import nmea_burst


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1, lon1, lat2, lon2) -> float:
    """Initial great-circle bearing from the first point to the second,
    degrees true, 0 to 360."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def interpolate(path, speed_mps: float, rate_hz: float):
    """Yield [lon, lat] along the path at speed, rate_hz fixes per second."""
    for lon, lat, _hdg in interpolate_heading(path, speed_mps, rate_hz):
        yield [lon, lat]


def interpolate_heading(path, speed_mps: float, rate_hz: float):
    """Yield (lon, lat, heading_deg) along the path: the heading is the
    bearing of the leg being flown, and the last point keeps the last leg's."""
    step_m = max(1e-3, float(speed_mps) / max(1e-3, float(rate_hz)))
    hdg = 0.0
    for i in range(len(path) - 1):
        (lon0, lat0), (lon1, lat1) = path[i], path[i + 1]
        d = haversine_m(lat0, lon0, lat1, lon1)
        if d > 0:
            hdg = bearing_deg(lat0, lon0, lat1, lon1)
        n = max(1, int(math.ceil(d / step_m)))
        for k in range(n):
            f = k / n
            yield (lon0 + (lon1 - lon0) * f, lat0 + (lat1 - lat0) * f, hdg)
    if path:
        yield (path[-1][0], path[-1][1], hdg)


def parse_points(text: str) -> list:
    """lat,lon pairs separated by spaces, semicolons or newlines, to [lon, lat]."""
    out = []
    for tok in str(text or "").replace(";", " ").replace("\n", " ").split():
        if "," not in tok:
            continue
        lat, lon = tok.split(",", 1)
        out.append([float(lon), float(lat)])
    return out


def datagram(lon: float, lat: float, alt_m: float, fmt: str, t_utc: float = None,
             heading_deg: float = None, speed_mps: float = None, drop=(),
             aux: bool = True, sats: int = 8) -> bytes:
    """One datagram for a position. JSON: {"lat", "lon", "alt", "time"} plus
    "heading" (degrees true) and "speed" (m/s) when given, "fix_mode" "3D" and
    "sats". NMEA: the burst GGA, RMC, VTG, GSA, GSV as lines of one datagram,
    less the kinds in drop; aux False leaves out GSA and GSV. With no heading
    the JSON has no heading key and the RMC and VTG courses are blank."""
    t = time.time() if t_utc is None else float(t_utc)
    if str(fmt).lower() == "nmea":
        lines = nmea_burst(lat, lon, alt_m, t, heading_deg=heading_deg,
                           speed_mps=speed_mps or 0.0, drop=drop, aux=aux, sats=sats)
        return ("\r\n".join(lines) + "\r\n").encode()
    d = {"lat": round(lat, 7), "lon": round(lon, 7), "alt": alt_m, "time": round(t, 3)}
    if heading_deg is not None:
        d["heading"] = round(float(heading_deg) % 360.0, 1)
    if speed_mps is not None:
        d["speed"] = round(float(speed_mps), 2)
    d["fix_mode"] = "3D"
    d["sats"] = int(sats)
    return json.dumps(d).encode()


def datagrams(lon: float, lat: float, alt_m: float, fmt: str, t_utc: float = None,
              heading_deg: float = None, speed_mps: float = None, drop=(),
              aux: bool = True, split: bool = False) -> list:
    """The datagrams for one position: one, or with split True and NMEA one
    per sentence (a feed that sends each sentence on its own)."""
    one = datagram(lon, lat, alt_m, fmt, t_utc, heading_deg, speed_mps, drop, aux)
    if not split or str(fmt).lower() != "nmea":
        return [one]
    return [ln.encode() + b"\r\n" for ln in one.decode().split("\r\n") if ln]


def open_sender_socket(iface: str = None, ttl: int = 1) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, int(ttl))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
    if iface and iface != "0.0.0.0":
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(iface))
    return s


class PathSender:
    """Plays a path on the feed. state is "stopped" or "playing"; sent counts
    datagrams; last is the last [lon, lat] sent."""

    def __init__(self, group: str, port: int, iface: str = "127.0.0.1",
                 fmt: str = "json", rate_hz: float = 1.0, speed_mps: float = 60.0,
                 alt_m: float = 1500.0, loop: bool = False, hold_s: float = 5.0,
                 time_offset_s: float = 0.0, drop=(), heading: bool = True,
                 aux_every: int = 1, split: bool = False):
        self.group, self.port, self.iface = str(group), int(port), str(iface or "0.0.0.0")
        self.fmt = str(fmt or "json").lower()
        self.rate_hz = max(0.1, float(rate_hz))
        self.speed_mps = max(0.1, float(speed_mps))
        self.alt_m = float(alt_m)
        self.loop = bool(loop)
        self.hold_s = max(0.0, float(hold_s))
        # added to the time stamp in each datagram: a way to try a feed whose
        # clock is off against the console
        self.time_offset_s = float(time_offset_s)
        # which NMEA sentences to leave out ("VTG" or "RMC,VTG"); heading False
        # sends no heading at all (JSON without the key, NMEA with blank
        # courses); aux_every n sends GSA and GSV on every nth burst only;
        # split sends each NMEA sentence in its own datagram
        self.drop = ([d.strip().upper() for d in drop.split(",") if d.strip()]
                     if isinstance(drop, str) else [str(d).upper() for d in drop])
        self.heading = bool(heading)
        self.aux_every = max(1, int(aux_every))
        self.split = bool(split)
        self.path = []
        self.state = "stopped"
        self.sent = 0
        self.last = None
        self.error = ""
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def set_path(self, coords) -> int:
        self.path = [[float(p[0]), float(p[1])] for p in (coords or [])
                     if isinstance(p, (list, tuple)) and len(p) == 2]
        return len(self.path)

    def length_m(self) -> float:
        return sum(haversine_m(a[1], a[0], b[1], b[0])
                   for a, b in zip(self.path, self.path[1:]))

    def start(self) -> bool:
        if len(self.path) < 2:
            self.error = "the path needs two or more points"
            return False
        self.stop()
        self._stop.clear()
        self.error = ""
        self.sent = 0
        self._thread = threading.Thread(target=self._run, name="feed-sender", daemon=True)
        self.state = "playing"
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        t, self._thread = self._thread, None
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2.0)
        self.state = "stopped"

    def _run(self) -> None:
        try:
            s = open_sender_socket(self.iface)
        except OSError as e:
            self.error = f"socket: {e}"
            self.state = "stopped"
            return
        period = 1.0 / self.rate_hz
        forward = True
        try:
            while not self._stop.is_set():
                pts = self.path if forward else list(reversed(self.path))
                hdg = None
                for lon, lat, hdg in interpolate_heading(pts, self.speed_mps, self.rate_hz):
                    if self._stop.is_set():
                        return
                    self._send(s, lon, lat, hdg, self.speed_mps)
                    time.sleep(period)
                end = time.monotonic() + self.hold_s
                while not self._stop.is_set() and time.monotonic() < end:
                    self._send(s, pts[-1][0], pts[-1][1], hdg, 0.0)   # holding still
                    time.sleep(period)
                if not self.loop:
                    return
                forward = not forward
        except OSError as e:
            self.error = f"send: {e}"
        finally:
            try:
                s.close()
            except OSError:
                pass
            self.state = "stopped"

    def _send(self, s, lon, lat, heading_deg=None, speed_mps=None):
        self._burst = getattr(self, "_burst", 0) + 1
        aux = (self._burst % self.aux_every) == 0
        for dg in datagrams(lon, lat, self.alt_m, self.fmt, time.time() + self.time_offset_s,
                            heading_deg if self.heading else None, speed_mps,
                            drop=self.drop, aux=aux, split=self.split):
            s.sendto(dg, (self.group, self.port))
            self.sent += 1
        self.last = [lon, lat]
        self.last_heading = heading_deg if self.heading else None


class PacketRecorder:
    """Writes every datagram the listener receives to a .jsonl file: one line
    per packet with t_wall, addr and the raw text. Hook it to the listener
    with listener.on_packet = recorder.write; close() unhooks nothing, the
    caller does that."""

    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._f = open(path, "a", encoding="utf-8")
        self.count = 0
        self._lock = threading.Lock()

    def write(self, pkt) -> None:
        line = json.dumps({"t_wall": pkt.t_wall, "addr": pkt.addr,
                           "raw": pkt.raw.decode("ascii", "replace")})
        with self._lock:
            self._f.write(line + "\n")
            self._f.flush()
            self.count += 1

    def close(self) -> None:
        with self._lock:
            try:
                self._f.close()
            except OSError:
                pass


def read_recording(path: str) -> list:
    """[(t_wall, raw bytes)] from a PacketRecorder file, in file order."""
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                out.append((float(d["t_wall"]), str(d["raw"]).encode("ascii", "replace")))
            except (ValueError, KeyError, TypeError):
                continue
    return out


class RecordingReplayer:
    """Sends a recording back on the feed with the recorded spacing (scaled by
    speed). The datagrams go out as recorded, time stamps included, so the
    feed-time check shows the recording's own clock against this console."""

    def __init__(self, group: str, port: int, iface: str = "127.0.0.1",
                 speed: float = 1.0, loop: bool = False):
        self.group, self.port, self.iface = str(group), int(port), str(iface or "0.0.0.0")
        self.speed = max(0.01, float(speed))
        self.loop = bool(loop)
        self.packets = []
        self.state = "stopped"
        self.sent = 0
        self.error = ""
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def load(self, path: str) -> int:
        self.packets = read_recording(path)
        return len(self.packets)

    def start(self) -> bool:
        if not self.packets:
            self.error = "the recording is empty"
            return False
        self.stop()
        self._stop.clear()
        self.error = ""
        self.sent = 0
        self._thread = threading.Thread(target=self._run, name="feed-replay", daemon=True)
        self.state = "playing"
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        t, self._thread = self._thread, None
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2.0)
        self.state = "stopped"

    def _run(self) -> None:
        try:
            s = open_sender_socket(self.iface)
        except OSError as e:
            self.error = f"socket: {e}"
            self.state = "stopped"
            return
        try:
            while not self._stop.is_set():
                prev = None
                for t_wall, raw in self.packets:
                    if self._stop.is_set():
                        return
                    if prev is not None:
                        gap = max(0.0, (t_wall - prev) / self.speed)
                        if self._stop.wait(min(gap, 10.0)):
                            return
                    prev = t_wall
                    s.sendto(raw, (self.group, self.port))
                    self.sent += 1
                if not self.loop:
                    return
        except OSError as e:
            self.error = f"send: {e}"
        finally:
            try:
                s.close()
            except OSError:
                pass
            self.state = "stopped"
