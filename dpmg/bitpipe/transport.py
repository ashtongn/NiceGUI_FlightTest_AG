"""
The container boundary: where a face binds and how bytes are framed on it.

An Endpoint is TCP on 127.0.0.1 (the default) or a Unix domain socket path;
one config switch (BitpipeConfig.transport) chooses for every face of a
container, and a peer address can name either form ("tcp:host:port" or
"unix:/path"). Shared memory would be a third kind behind the same two
calls (listen, connect) and is not built.

The data plane (the bit face) is a length prefixed record stream:

    magic "BP"(2) type(1) length(4, big endian) payload(length)

The magic is there so a reader that joins mid stream or loses a byte finds
the next record again instead of trusting a wrong length.

    REC_PULL    modem to container: bit count(4) wanted; the container answers with
                whole frames until at least that many bits are sent (idle frames
                when it has nothing), so the modem pulls at its symbol rate and
                the bitpipe has no clock of its own
    REC_BITS    bit count(4) then packed bits, 8 a byte, MSB first; container to
                modem it is one whole frame, modem to container any chunk of
                the received stream
    REC_SOFT8   one signed byte per bit, +127 certain 1, -127 certain 0 (RX only)
    REC_HELLO   JSON: who is on the other side and what it needs
    REC_STATUS  empty from the modem = request; JSON status back
    REC_MODEM   modem to container, about once a second: the modem's own JSON
                status (waveform, carrier offset, eye, underruns), kept by the
                container so one status call shows both ends of the bit face

Records let a modem hand over variable length blocks and soft bits on the
same stream, and a dropped or late record is one unit for the channel model.
The raw option (no records, the bytes are the packed bits, pushed under the
socket's back pressure) stays for a plain consumer such as a file or nc and
is not meant for a modem.

Control is not here: that stays on the existing UDP command protocol in
dpmg_service.py, or the small JSON line channel the container offers
(container.py, ctl face).
"""
from __future__ import annotations

import json
import os
import socket
import stat
import struct
from dataclasses import dataclass
from typing import List, Optional, Tuple

REC_MAGIC = b"BP"
REC_HDR = struct.Struct("!2sBI")        # magic, type, length
REC_BITS, REC_SOFT8, REC_HELLO, REC_STATUS, REC_PULL, REC_MODEM = 0, 1, 2, 3, 4, 5
REC_MAX = 1 << 20
BITS_HDR = struct.Struct("!I")          # bit count ahead of the packed bits in a REC_BITS record


@dataclass
class Endpoint:
    kind: str = "tcp"             # tcp | unix
    host: str = "127.0.0.1"
    port: int = 0
    path: str = ""

    @classmethod
    def parse(cls, spec) -> "Endpoint":
        """"tcp:host:port", "unix:/path", a (host, port) tuple, or an Endpoint."""
        if isinstance(spec, Endpoint):
            return spec
        if isinstance(spec, (tuple, list)) and len(spec) == 2:
            return cls("tcp", str(spec[0]), int(spec[1]))
        s = str(spec)
        if s.startswith("unix:"):
            return cls("unix", path=s[5:])
        if s.startswith("tcp:"):
            s = s[4:]
        host, _, port = s.rpartition(":")
        return cls("tcp", host or "127.0.0.1", int(port))

    def describe(self) -> str:
        return f"unix:{self.path}" if self.kind == "unix" else f"tcp:{self.host}:{self.port}"

    def listen(self, backlog: int = 4, timeout: float = 0.5) -> socket.socket:
        if self.kind == "unix":
            d = os.path.dirname(self.path)
            if d:
                os.makedirs(d, mode=0o700, exist_ok=True)
            try:
                if stat.S_ISSOCK(os.stat(self.path).st_mode):
                    os.unlink(self.path)                         # stale socket from a previous run
            except FileNotFoundError:
                pass
            ls = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            ls.bind(self.path)
            os.chmod(self.path, 0o600)
        elif self.kind == "tcp":
            ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            ls.bind((self.host, int(self.port)))
        else:
            raise ValueError(f"unsupported transport {self.kind!r}")
        ls.listen(backlog)
        ls.settimeout(timeout)
        return ls

    def connect(self, timeout: float = 2.0) -> socket.socket:
        if self.kind == "unix":
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect(self.path)
            return s
        if self.kind == "tcp":
            return socket.create_connection((self.host, int(self.port)), timeout=timeout)
        raise ValueError(f"unsupported transport {self.kind!r}")

    def cleanup(self) -> None:
        if self.kind == "unix":
            try:
                os.unlink(self.path)
            except OSError:
                pass


def tune(sock: socket.socket, bufsize: int = 16384, timeout: float = 0.5) -> socket.socket:
    """Small buffers (the ARQ must not see seconds of queued frames), no Nagle, a poll timeout."""
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, bufsize)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, bufsize)
    if sock.family == socket.AF_INET:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.settimeout(timeout)
    return sock


# records
def pack_record(rtype: int, payload: bytes) -> bytes:
    payload = bytes(payload)
    if len(payload) > REC_MAX:
        raise ValueError("record too large")
    return REC_HDR.pack(REC_MAGIC, int(rtype) & 0xFF, len(payload)) + payload


def pack_json(rtype: int, obj) -> bytes:
    return pack_record(rtype, json.dumps(obj, separators=(",", ":"), default=str).encode("utf-8"))


class RecordReader:
    """Feed bytes, get (type, payload) records back; tolerates any chunking."""

    def __init__(self):
        self.buf = bytearray()
        self.errors = 0

    def feed(self, data: bytes) -> List[Tuple[int, bytes]]:
        self.buf.extend(data)
        out = []
        while len(self.buf) >= REC_HDR.size:
            magic, rtype, n = REC_HDR.unpack_from(self.buf, 0)
            if magic != REC_MAGIC or n > REC_MAX:
                self.errors += 1
                del self.buf[:1]                                  # out of step: slide one byte
                continue
            if len(self.buf) < REC_HDR.size + n:
                break
            out.append((rtype, bytes(self.buf[REC_HDR.size:REC_HDR.size + n])))
            del self.buf[:REC_HDR.size + n]
        return out


def parse_json(payload: bytes) -> Optional[dict]:
    try:
        obj = json.loads(payload.decode("utf-8"))
        return obj if isinstance(obj, dict) else None
    except (ValueError, UnicodeDecodeError):
        return None


def pack_bits(bits) -> bytes:
    """A REC_BITS payload: bit count, then the bits packed MSB first."""
    import numpy as np
    b = np.asarray(bits, dtype=np.uint8).reshape(-1)
    return BITS_HDR.pack(len(b)) + np.packbits(b).tobytes()


def unpack_bits(payload: bytes):
    """The bits of a REC_BITS payload as a uint8 0/1 array (None when malformed)."""
    import numpy as np
    if len(payload) < BITS_HDR.size:
        return None
    n = BITS_HDR.unpack_from(payload, 0)[0]
    bits = np.unpackbits(np.frombuffer(payload[BITS_HDR.size:], dtype=np.uint8))
    if len(bits) < n:
        return None
    return bits[:n]
