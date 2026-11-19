"""
The bitpipe link frame (before FEC) and the inner record that gets sealed.

    flags(1) stream(1) seq(2) ack(2) sack(2) len(1) payload(P) crc(2 or 4)

flags: version in the top nibble; bit 0 idle, bit 1 data, bit 2 retransmit.
seq is the 16 bit link sequence of a data frame (idle frames repeat the next
one). ack is the next sequence the sender expects from its peer; sack bit i
means sequence ack + 1 + i has been received. Every frame carries ack and
sack, idle frames included, so the reverse direction is the ack channel.

The payload field is fixed at P bytes and len says how many are used. What
goes in it is the sealed body from SecureContext.seal (or the inner record in
the clear when encryption is off). The inner record is stream(1) seq(2) data,
so after the tag verifies the receiver can check that the clear header was
not swapped: the link header is not AAD, this binding stands in for it.

CRC over everything before it, CRC-16/CCITT-FALSE by default (crc_util).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Optional, Tuple

from dpmg.framing import crc_util

VERSION = 1
FLAG_IDLE = 0x01
FLAG_DATA = 0x02
FLAG_RETX = 0x04
FLAG_STATION = 0x08                                # sent by station 1 of a two radio link
HEADER_FMT = "!BBHHHB"
HEADER_BYTES = struct.calcsize(HEADER_FMT)        # 9
INNER_FMT = "!BH"
INNER_HEADER_BYTES = struct.calcsize(INNER_FMT)   # 3
SEQ_MOD = 1 << 16


@dataclass
class FrameHeader:
    flags: int = 0
    stream: int = 0
    seq: int = 0
    ack: int = 0
    sack: int = 0
    length: int = 0

    @property
    def version(self) -> int:
        return (self.flags >> 4) & 0xF

    @property
    def idle(self) -> bool:
        return bool(self.flags & FLAG_IDLE)

    @property
    def data(self) -> bool:
        return bool(self.flags & FLAG_DATA)

    @property
    def retx(self) -> bool:
        return bool(self.flags & FLAG_RETX)

    @property
    def station(self) -> int:
        return 1 if self.flags & FLAG_STATION else 0


def crc_bytes_of(crc_name: str) -> int:
    return 4 if crc_name == "CRC-32" else 2


def pack_frame(hdr: FrameHeader, payload: bytes, payload_bytes: int, crc_name: str = "CRC-16-CCITT") -> bytes:
    """Fixed length frame bytes. The payload is zero padded to payload_bytes."""
    payload = bytes(payload)
    if len(payload) > payload_bytes:
        raise ValueError(f"payload {len(payload)} B exceeds the {payload_bytes} B field")
    flags = (VERSION << 4) | (hdr.flags & 0x0F)
    body = struct.pack(HEADER_FMT, flags, hdr.stream & 0xFF, hdr.seq % SEQ_MOD, hdr.ack % SEQ_MOD,
                       hdr.sack & 0xFFFF, len(payload)) + payload + bytes(payload_bytes - len(payload))
    crc = crc_util.crc_of(body, crc_name)
    return body + crc.to_bytes(crc_bytes_of(crc_name), "big")


def unpack_frame(data: bytes, payload_bytes: int,
                 crc_name: str = "CRC-16-CCITT") -> Optional[Tuple[FrameHeader, bytes]]:
    """(header, payload) when the CRC passes and the header is sane, else None."""
    n_crc = crc_bytes_of(crc_name)
    want = HEADER_BYTES + payload_bytes + n_crc
    if len(data) != want:
        return None
    body, crc = data[:-n_crc], int.from_bytes(data[-n_crc:], "big")
    if crc_util.crc_of(body, crc_name) != crc:
        return None
    flags, stream, seq, ack, sack, length = struct.unpack(HEADER_FMT, body[:HEADER_BYTES])
    if (flags >> 4) != VERSION or length > payload_bytes:
        return None
    hdr = FrameHeader(flags, stream, seq, ack, sack, length)
    return hdr, body[HEADER_BYTES:HEADER_BYTES + length]


def pack_inner(stream: int, seq: int, data: bytes) -> bytes:
    return struct.pack(INNER_FMT, stream & 0xFF, seq % SEQ_MOD) + bytes(data)


def unpack_inner(rec: bytes) -> Optional[Tuple[int, int, bytes]]:
    if len(rec) < INNER_HEADER_BYTES:
        return None
    stream, seq = struct.unpack(INNER_FMT, rec[:INNER_HEADER_BYTES])
    return stream, seq, rec[INNER_HEADER_BYTES:]


def seq_lt(a: int, b: int) -> bool:
    """a before b in the 16 bit circular sequence space."""
    return ((b - a) % SEQ_MOD) != 0 and ((b - a) % SEQ_MOD) < (SEQ_MOD // 2)


def seq_diff(a: int, b: int) -> int:
    """a - b taken circularly, in -32768..32767."""
    d = (a - b) % SEQ_MOD
    return d - SEQ_MOD if d >= SEQ_MOD // 2 else d
