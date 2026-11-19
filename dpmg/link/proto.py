"""
DPMG wire protocol  (proto.py), version in VERSION below
=================================
Shared, single source of truth. Copied verbatim to every host (GUI x86,
G40 service, X40 service). Stdlib only: struct, json, dataclasses, enum.

Design rules
------------
- One message per UDP datagram, always < 1400 bytes on the wire.
- Header (8 bytes, network order): magic(2s) version(B) msg_type(B) seq(I).
  seq is per-sender, wraps at 2**32.
- Telemetry (STATUS / PSD_FRAME / DETECTION) is fire-and-forget,
  idempotent snapshots, last-value-wins. No retransmit.
- Commands (CMD) are absolute setpoints, retried by the sender until a
  CMD_ACK with the same cmd_id arrives. Duplicates must be harmless.
- DETECTION events carry a monotonically increasing det_index; the
  receiver requests gap-fills with DETECTION_REQ.
- FILE_* implements a minimal cumulative-ack chunked transfer for
  pushing scenario IQ files to a TX host.
- VERSION is checked in HELLO; a mismatch is refused.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field
from enum import IntEnum

VERSION = 3      # v3: WATCH_UPDATE channelizer decode telemetry
#                 (v2: STATUS stream health rx_frames/fps/xruns)
MAGIC = b"DG"
HEADER_FMT = "!2sBBI"                 # magic, version, msg_type, seq
HEADER_SIZE = struct.calcsize(HEADER_FMT)   # 8
MAX_DATAGRAM = 1400                   # hard wire-size ceiling
FILE_CHUNK_PAYLOAD = 1024             # keeps FILE_CHUNK < MAX_DATAGRAM


class MsgType(IntEnum):
    ANNOUNCE      = 0x01   # service: broadcast          (periodic, 2 s)
    HELLO         = 0x02   # gui: service                (start of session)
    HELLO_ACK     = 0x03   # service: gui
    STATUS        = 0x10   # service: gui                (1 Hz heartbeat)
    PSD_FRAME     = 0x11   # service: gui                (2-5 Hz, int8 bins)
    DETECTION     = 0x12   # service: gui                (event)
    DETECTION_REQ = 0x13   # gui: service                (gap-fill request)
    WATCH_UPDATE  = 0x14   # service: gui  (v3: channelizer decode telemetry)
    SEALED_FRAME  = 0x15   # service: gui  (secure messaging: one CRC-valid sealed body,
                           #   unopened: the ground station holds the keys)
    CMD           = 0x20   # gui: service                (retried until ack)
    CMD_ACK       = 0x21   # service: gui
    FILE_OFFER    = 0x30   # gui: service                (announce transfer)
    FILE_CHUNK    = 0x31   # gui: service
    FILE_ACK      = 0x32   # service: gui                (cumulative ack)


class HostType(IntEnum):
    G40_RX = 0
    X40_TX = 1
    OTHER  = 2


class RadioState(IntEnum):
    UNINIT    = 0
    INIT_OK   = 1
    STREAMING = 2
    FAULT     = 3


class CmdStatus(IntEnum):
    OK       = 0
    REJECTED = 1
    FAILED   = 2
    BUSY     = 3


class ProtoError(Exception):
    pass


# helpers

def _pack_str(s: str, n: int) -> bytes:
    b = s.encode("utf-8")[:n]
    return b.ljust(n, b"\x00")


def _unpack_str(b: bytes) -> str:
    return b.rstrip(b"\x00").decode("utf-8", errors="replace")


# message payloads

@dataclass
class Announce:
    serial: str            # radio serial = permanent identity (never the IP)
    host_type: int         # HostType
    svc_version: int       # service software version, informational
    cmd_port: int          # UDP port the service listens on
    _FMT = "!16sBBH"

    def pack(self) -> bytes:
        return struct.pack(self._FMT, _pack_str(self.serial, 16),
                           self.host_type, self.svc_version, self.cmd_port)

    @classmethod
    def unpack(cls, b: bytes) -> "Announce":
        serial, ht, sv, port = struct.unpack(cls._FMT, b)
        return cls(_unpack_str(serial), ht, sv, port)


@dataclass
class Hello:
    gui_id: str            # identifies the ground station instance
    proto_version: int = VERSION
    _FMT = "!16sB"

    def pack(self) -> bytes:
        return struct.pack(self._FMT, _pack_str(self.gui_id, 16),
                           self.proto_version)

    @classmethod
    def unpack(cls, b: bytes) -> "Hello":
        gid, pv = struct.unpack(cls._FMT, b)
        return cls(_unpack_str(gid), pv)


@dataclass
class HelloAck:
    serial: str
    accepted: int          # 1 = session open, 0 = refused (version mismatch)
    proto_version: int = VERSION
    _FMT = "!16sBB"

    def pack(self) -> bytes:
        return struct.pack(self._FMT, _pack_str(self.serial, 16),
                           self.accepted, self.proto_version)

    @classmethod
    def unpack(cls, b: bytes) -> "HelloAck":
        serial, acc, pv = struct.unpack(cls._FMT, b)
        return cls(_unpack_str(serial), acc, pv)


@dataclass
class Status:
    """1 Hz heartbeat. Position is the position of this host."""
    serial: str
    host_type: int         # HostType
    radio_state: int       # RadioState
    flags: int = 0         # bit0: position_valid, bit1: task_running
    temp_c_x10: int = 0    # deci-degrees C, signed
    lat_1e7: int = 0       # degrees * 1e7, signed  (i32 covers +/-214 deg)
    lon_1e7: int = 0
    alt_m: float = 0.0
    uptime_s: int = 0
    last_cmd_id: int = 0   # highest cmd_id applied (dup detection aid)
    det_index: int = 0     # current detection ring head (gap detection aid)
    rx_frames: int = 0     # v2: scheduler frames drained (u32, wraps)
    fps: int = 0           # v2: scheduler frames/sec (u16, saturating:
                           #     Epiq drains thousands of chunk-frames/s)
    overruns: int = 0      # v2: card overruns (u16, saturating)
    underruns: int = 0     # v2: card underruns (u16, saturating)
    rx_gain_idx: int = 0   # v2.3: live RX gain index (so the GUI tracks auto back-off)
    center_hz: float = 0.0 # v2.4: live RX/TX center (so a reloaded GUI re-syncs tuning)
    sample_rate_hz: float = 0.0  # v2.4: live sample rate
    _FMT = "!16sBBHhiifIIIIHHHHdd"

    FLAG_POS_VALID    = 1 << 0
    FLAG_TASK_RUNNING = 1 << 1
    FLAG_TX_ACTIVE    = 1 << 2   # v2.1: TX datapath streaming (radiating)
    FLAG_REF_LOCKED   = 1 << 3   # v2.2: timing reference (GPSDO) disciplined + locked
    FLAG_RX_OVERLOAD  = 1 << 4   # v2.3: RX ADC near/at full-scale (clipping)
    FLAG_RX_STALE     = 1 << 5   # v2.5: RX stream replaying stale buffers (wedged
                                 #       FIFO): the PSD is frozen, detection paused
    FLAG_SECURE_ON    = 1 << 6   # v2.6: secure messaging active
                                 #       (keys loaded, sealed frames are opened)
    FLAG_SECURE_FAULT = 1 << 7   # v2.6: secure messaging requested but refused (no key file,
                                 #       crypto self-test) or an auth-fail alarm

    def pack(self) -> bytes:
        return struct.pack(self._FMT, _pack_str(self.serial, 16),
                           self.host_type, self.radio_state, self.flags,
                           self.temp_c_x10, self.lat_1e7, self.lon_1e7,
                           self.alt_m, self.uptime_s, self.last_cmd_id,
                           self.det_index, self.rx_frames & 0xFFFFFFFF,
                           min(0xFFFF, self.fps),
                           min(0xFFFF, self.overruns),
                           min(0xFFFF, self.underruns),
                           min(0xFFFF, max(0, int(self.rx_gain_idx))),
                           float(self.center_hz), float(self.sample_rate_hz))

    @classmethod
    def unpack(cls, b: bytes) -> "Status":
        (serial, ht, rs, fl, t, lat, lon, alt,
         up, lc, di, fr, fps, ov, un, gidx, cen, srate) = struct.unpack(cls._FMT, b)
        return cls(_unpack_str(serial), ht, rs, fl, t, lat, lon, alt,
                   up, lc, di, fr, fps, ov, un, gidx, cen, srate)


@dataclass
class PsdFrame:
    """Decimated PSD. bins are int8: db = ref_db + bin * (scale_db_x100/100)."""
    center_hz: float
    span_hz: float
    ref_db: float
    scale_db_x100: int     # e.g. 50 gives 0.5 dB per LSB
    bins: bytes            # int8 array, len(bins) == nbins
    _FMT = "!dffHH"        # center, span, ref, scale, nbins

    MAX_BINS = MAX_DATAGRAM - HEADER_SIZE - struct.calcsize("!dffHH")  # ~1372

    def pack(self) -> bytes:
        if len(self.bins) > self.MAX_BINS:
            raise ProtoError(f"PSD nbins {len(self.bins)} > {self.MAX_BINS}")
        head = struct.pack(self._FMT, self.center_hz, self.span_hz,
                           self.ref_db, self.scale_db_x100, len(self.bins))
        return head + self.bins

    @classmethod
    def unpack(cls, b: bytes) -> "PsdFrame":
        n = struct.calcsize(cls._FMT)
        center, span, ref, scale, nbins = struct.unpack(cls._FMT, b[:n])
        bins = b[n:n + nbins]
        if len(bins) != nbins:
            raise ProtoError("PSD_FRAME truncated")
        return cls(center, span, ref, scale, bins)


@dataclass
class Detection:
    det_index: int         # monotonically increasing ring index
    t_unix: float
    freq_hz: float
    bw_hz: float
    snr_db: float
    duration_ms: float
    flags: int = 0
    _FMT = "!IddfffH"

    def pack(self) -> bytes:
        return struct.pack(self._FMT, self.det_index, self.t_unix,
                           self.freq_hz, self.bw_hz, self.snr_db,
                           self.duration_ms, self.flags)

    @classmethod
    def unpack(cls, b: bytes) -> "Detection":
        return cls(*struct.unpack(cls._FMT, b))


@dataclass
class DetectionReq:
    start_index: int
    count: int
    _FMT = "!IH"

    def pack(self) -> bytes:
        return struct.pack(self._FMT, self.start_index, self.count)

    @classmethod
    def unpack(cls, b: bytes) -> "DetectionReq":
        return cls(*struct.unpack(cls._FMT, b))


@dataclass
class Cmd:
    """Command plane. Low-rate, so params ride as compact JSON.
    Commands must be absolute setpoints (idempotent)."""
    cmd_id: int
    name: str              # e.g. "set_center", "start_scenario", "stop"
    params: dict = field(default_factory=dict)
    _FMT = "!I"

    def pack(self) -> bytes:
        body = json.dumps({"n": self.name, "p": self.params},
                          separators=(",", ":")).encode("utf-8")
        out = struct.pack(self._FMT, self.cmd_id) + body
        if len(out) + HEADER_SIZE > MAX_DATAGRAM:
            raise ProtoError("CMD too large for one datagram")
        return out

    @classmethod
    def unpack(cls, b: bytes) -> "Cmd":
        (cmd_id,) = struct.unpack(cls._FMT, b[:4])
        d = json.loads(b[4:].decode("utf-8"))
        return cls(cmd_id, d["n"], d.get("p", {}))


@dataclass
class CmdAck:
    cmd_id: int
    status: int            # CmdStatus
    result: dict = field(default_factory=dict)
    _FMT = "!IB"

    def pack(self) -> bytes:
        body = json.dumps(self.result, separators=(",", ":")).encode("utf-8")
        return struct.pack(self._FMT, self.cmd_id, self.status) + body

    @classmethod
    def unpack(cls, b: bytes) -> "CmdAck":
        cmd_id, status = struct.unpack(cls._FMT, b[:5])
        result = json.loads(b[5:].decode("utf-8")) if len(b) > 5 else {}
        return cls(cmd_id, status, result)


@dataclass
class WatchUpdate:
    """v3 channelizer decode telemetry: one datagram carries the per-band decode
    summary (from ChannelizerWorker.band_status). Fire-and-forget, last-value-
    wins like PSD. Keys are short to fit many bands under MAX_DATAGRAM; the band
    list is truncated if it would overflow."""
    serial: str
    bands: list = field(default_factory=list)   # [{n,f,m,p,d,msg,dn,tn}]
    total: int = 0                              # bands in the whole snapshot
    _FMT = "!16s"

    def pack(self) -> bytes:
        head = struct.pack(self._FMT, _pack_str(self.serial, 16))
        bands = list(self.bands)
        total = self.total or len(bands)
        while True:
            body = json.dumps({"b": bands, "t": total},
                              separators=(",", ":")).encode("utf-8")
            if len(head) + len(body) + HEADER_SIZE <= MAX_DATAGRAM or not bands:
                break
            bands = bands[:-1]                  # drop the last band, retry
        return head + body

    @classmethod
    def unpack(cls, b: bytes) -> "WatchUpdate":
        serial = _unpack_str(struct.unpack(cls._FMT, b[:16])[0])
        d = json.loads(b[16:].decode("utf-8")) if len(b) > 16 else {"b": []}
        return cls(serial, d.get("b", []), int(d.get("t", 0)))

    @classmethod
    def paginate(cls, serial: str, bands: list) -> list:
        """Split bands into pages that each fit one datagram, so a plan with
        more bands than one datagram holds is sent in full instead of
        truncated. Every page carries the whole-snapshot count in `total`."""
        total = len(bands)
        if not bands:
            return [cls(serial, [], 0)]
        head = struct.calcsize(cls._FMT)

        def fits(page):
            body = json.dumps({"b": page, "t": total},
                              separators=(",", ":")).encode("utf-8")
            return head + len(body) + HEADER_SIZE <= MAX_DATAGRAM

        pages, i = [], 0
        while i < len(bands):
            page = []
            while i < len(bands) and (not page or fits(page + [bands[i]])):
                page.append(bands[i]); i += 1
            pages.append(cls(serial, page, total))
        return pages


@dataclass
class SealedFrame:
    """One sealed body (LEN || HDR || CT || TAG, CRC checked, not
    opened) the radio's decode pool found, forwarded to the ground station for
    opening. The radio holds no key material. Fire-and-forget; `seq` lets the
    ground notice drops. body <= 1 + 10 + 255 + 16 bytes, so one datagram."""
    serial: str
    band: str              # plan entry name
    f_center_hz: float
    seq: int
    t_unix: float
    body: bytes
    _FMT = "!16s16sdId"

    def pack(self) -> bytes:
        return struct.pack(self._FMT, _pack_str(self.serial, 16), _pack_str(self.band, 16),
                           float(self.f_center_hz), int(self.seq) & 0xFFFFFFFF,
                           float(self.t_unix)) + bytes(self.body)

    @classmethod
    def unpack(cls, b: bytes) -> "SealedFrame":
        n = struct.calcsize(cls._FMT)
        serial, band, f, seq, t = struct.unpack(cls._FMT, b[:n])
        return cls(_unpack_str(serial), _unpack_str(band), f, seq, t, bytes(b[n:]))


@dataclass
class FileOffer:
    file_id: int
    total_chunks: int
    total_bytes: int
    name: str              # destination-relative filename
    _FMT = "!IIQ32s"

    def pack(self) -> bytes:
        return struct.pack(self._FMT, self.file_id, self.total_chunks,
                           self.total_bytes, _pack_str(self.name, 32))

    @classmethod
    def unpack(cls, b: bytes) -> "FileOffer":
        fid, tc, tb, name = struct.unpack(cls._FMT, b)
        return cls(fid, tc, tb, _unpack_str(name))


@dataclass
class FileChunk:
    file_id: int
    chunk_idx: int
    payload: bytes
    _FMT = "!II"

    def pack(self) -> bytes:
        if len(self.payload) > FILE_CHUNK_PAYLOAD:
            raise ProtoError("FILE_CHUNK payload too large")
        return struct.pack(self._FMT, self.file_id, self.chunk_idx) + self.payload

    @classmethod
    def unpack(cls, b: bytes) -> "FileChunk":
        fid, idx = struct.unpack(cls._FMT, b[:8])
        return cls(fid, idx, b[8:])


@dataclass
class FileAck:
    file_id: int
    highest_contig: int    # highest contiguous chunk_idx received (-1: none)
    _FMT = "!Ii"

    def pack(self) -> bytes:
        return struct.pack(self._FMT, self.file_id, self.highest_contig)

    @classmethod
    def unpack(cls, b: bytes) -> "FileAck":
        return cls(*struct.unpack(cls._FMT, b))


# top-level encode / decode

_MSG_CLASS = {
    MsgType.ANNOUNCE:      Announce,
    MsgType.HELLO:         Hello,
    MsgType.HELLO_ACK:     HelloAck,
    MsgType.STATUS:        Status,
    MsgType.PSD_FRAME:     PsdFrame,
    MsgType.DETECTION:     Detection,
    MsgType.DETECTION_REQ: DetectionReq,
    MsgType.WATCH_UPDATE:  WatchUpdate,
    MsgType.SEALED_FRAME:  SealedFrame,
    MsgType.CMD:           Cmd,
    MsgType.CMD_ACK:       CmdAck,
    MsgType.FILE_OFFER:    FileOffer,
    MsgType.FILE_CHUNK:    FileChunk,
    MsgType.FILE_ACK:      FileAck,
}
_CLASS_MSG = {v: k for k, v in _MSG_CLASS.items()}


def encode(msg, seq: int) -> bytes:
    """Wrap a message dataclass into a full datagram."""
    mtype = _CLASS_MSG[type(msg)]
    out = struct.pack(HEADER_FMT, MAGIC, VERSION, mtype,
                      seq & 0xFFFFFFFF) + msg.pack()
    if len(out) > MAX_DATAGRAM:
        raise ProtoError(f"{mtype.name} datagram {len(out)} > {MAX_DATAGRAM}")
    return out


def decode(datagram: bytes):
    """Return (MsgType, seq, message). Raises ProtoError on garbage."""
    if len(datagram) < HEADER_SIZE:
        raise ProtoError("short datagram")
    magic, ver, mtype, seq = struct.unpack(HEADER_FMT,
                                           datagram[:HEADER_SIZE])
    if magic != MAGIC:
        raise ProtoError("bad magic")
    if ver != VERSION:
        raise ProtoError(f"version mismatch: got {ver}, want {VERSION}")
    try:
        mt = MsgType(mtype)
    except ValueError:
        raise ProtoError(f"unknown msg_type 0x{mtype:02x}")
    try:
        msg = _MSG_CLASS[mt].unpack(datagram[HEADER_SIZE:])
    except (struct.error, json.JSONDecodeError, KeyError) as e:
        raise ProtoError(f"{mt.name} payload decode failed: {e}")
    return mt, seq, msg


# convenience: degrees to and from fixed-point
def deg_to_1e7(deg: float) -> int:
    return int(round(deg * 1e7))


def deg_from_1e7(v: int) -> float:
    return v / 1e7
