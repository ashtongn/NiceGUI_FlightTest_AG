"""
Bitpipe configuration and the frame sizes that follow from it.

Everything the receiver needs to know in advance is here: the frame is a
fixed size, so once the sync word is found the receiver knows exactly how many
coded bits follow and can flywheel from frame to frame.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from . import frame as _frame
from . import coding as _coding

CCSDS_ASM = "1ACFFC1D"            # 32 bit attached sync marker


@dataclass
class BitpipeConfig:
    name: str = "bitpipe"
    # link frame
    payload_bytes: int = 64                   # fixed payload field, 1..232
    fec: Optional[dict] = field(default_factory=lambda: {"type": "conv", "rate": "1/2"})
    interleave_depth: int = 8                 # 1 = off
    whiten: bool = True                       # xor the coded block with a fixed PN sequence (sync stays clear)
    crc: str = "CRC-16-CCITT"                 # or CRC-32
    sync_hex: str = CCSDS_ASM
    sync_max_errors: int = 3                  # bit errors tolerated when searching
    lock_miss_limit: int = 4                  # failed frames in a row before re-search
    # ARQ
    window: int = 16                          # outstanding frames, at most 16 (sack bits)
    rto_frames: int = 6                       # retransmit after this many frames with no ack
    rto_max_frames: int = 64                  # ceiling for the round trip adapted timeout
    max_retries: int = 50                     # then the link is reported down (keeps trying)
    # which end of a two radio link this is, 0 or 1; frames carry the bit and
    # an end drops frames with its own bit. Each radio's RX hears some of its
    # own TX (inside the radio and through the cabling); while the far end is
    # silent that leak decodes, and taken as the peer's frames it delivered the
    # end's own data to itself and acknowledged frames the peer never got.
    # None: one end only, which hears itself on purpose (one radio loopback).
    station: Optional[int] = None
    bind_header: bool = True                  # inner record carries stream + seq
    # crypto: "none" or "secure" (secure_frame SecureContext, keys on this host)
    crypto: str = "none"
    freshness_slots: int = 300                # SecureContext policy for a stream (1 s slots)
    # key source for crypto="secure": "file" (DPMG_KEY_FILE or key_file, kept on
    # the x86) or "static" (a key document handed in); a device unlocked
    # provider is not implemented
    key_provider: str = "file"
    key_file: Optional[str] = None
    # streams this container serves on its application face
    streams: dict = field(default_factory=lambda: {1: "chat", 2: "file", 3: "audio"})
    # container boundary: every face binds the same way. tcp = 127.0.0.1 ports
    # (app_base_port + stream, bit_port, ctl_port); unix = socket files under
    # unix_dir named <name>-app<stream>.sock, <name>-bit.sock, <name>-ctl.sock
    transport: str = "tcp"
    app_host: str = "127.0.0.1"
    app_base_port: int = 47900
    bit_host: str = "127.0.0.1"
    bit_port: int = 47899
    ctl_port: int = 47898
    unix_dir: str = "/tmp/dpmg-bitpipe"
    bit_connect: Optional[object] = None      # "tcp:host:port", "unix:/path" or (host, port): connect, not listen
    # bit face framing: records (magic + type + length prefixed, so a modem can
    # pull whole frames and send soft or hard bits on one stream) or raw (the
    # bytes are the packed bits; a plain consumer such as nc or a file)
    bit_framing: str = "records"
    bit_format: str = "packed"                # raw framing RX decode: packed (8 bits a byte) or soft8 (int8 LLR a bit)
    ctl_enabled: bool = True                  # JSON line control and status channel

    def endpoint(self, face: str, stream: int = 0):
        """The Endpoint a face binds: face is "app", "bit" or "ctl"."""
        from .transport import Endpoint
        if self.transport == "unix":
            nm = f"{self.name}-app{int(stream)}.sock" if face == "app" else f"{self.name}-{face}.sock"
            return Endpoint("unix", path=os.path.join(self.unix_dir, nm))
        if face == "app":
            return Endpoint("tcp", self.app_host, int(self.app_base_port) + int(stream))
        if face == "bit":
            return Endpoint("tcp", self.bit_host, int(self.bit_port))
        return Endpoint("tcp", self.bit_host, int(self.ctl_port))

    def validate(self) -> "FrameSizes":
        if not 1 <= int(self.payload_bytes) <= 232:
            raise ValueError("payload_bytes must be 1..232")
        if not 1 <= int(self.window) <= 16:
            raise ValueError("window must be 1..16 (one sack bit per frame)")
        if self.station not in (None, 0, 1):
            raise ValueError("station must be None, 0 or 1")
        if self.crc not in ("CRC-16-CCITT", "CRC-32"):
            raise ValueError("crc must be CRC-16-CCITT or CRC-32")
        if self.bit_format not in ("packed", "soft8"):
            raise ValueError("bit_format must be packed or soft8")
        if self.bit_framing not in ("records", "raw"):
            raise ValueError("bit_framing must be records or raw")
        if self.transport not in ("tcp", "unix"):
            raise ValueError("transport must be tcp or unix")
        if self.key_provider not in ("file", "static", "device"):
            raise ValueError("key_provider must be file, static or device")
        if self.crypto not in ("none", "secure"):
            raise ValueError("crypto must be none or secure")
        if int(self.interleave_depth) < 1:
            raise ValueError("interleave_depth must be >= 1")
        sizes = FrameSizes.from_config(self)
        limit = _coding.max_info_bits(self.fec)
        if limit is not None and sizes.info_bits > limit:
            raise ValueError(f"frame of {sizes.info_bits} info bits does not fit one "
                             f"{_coding.kind(self.fec)} block ({limit} bits); shrink payload_bytes")
        return sizes

    @property
    def sizes(self) -> "FrameSizes":
        return FrameSizes.from_config(self)


@dataclass
class FrameSizes:
    header_bytes: int
    payload_bytes: int
    crc_bytes: int
    frame_bytes: int
    info_bits: int
    coded_bits: int           # FEC output for one frame
    air_bits: int             # after interleaver padding
    sync_bits: int
    block_bits: int           # sync + air: what one frame occupies on the stream
    data_capacity: int        # application bytes one frame carries

    @classmethod
    def from_config(cls, cfg: BitpipeConfig) -> "FrameSizes":
        crc_bytes = 4 if cfg.crc == "CRC-32" else 2
        frame_bytes = _frame.HEADER_BYTES + int(cfg.payload_bytes) + crc_bytes
        info_bits = 8 * frame_bytes
        coded = _coding.coded_len(info_bits, cfg.fec)
        d = max(1, int(cfg.interleave_depth))
        air = -(-coded // d) * d
        sync_bits = 4 * len(cfg.sync_hex)
        overhead = 0
        if cfg.crypto == "secure":
            from . import crypto as _crypto
            overhead = _crypto.SECURE_OVERHEAD_BYTES
        if cfg.bind_header:
            overhead += _frame.INNER_HEADER_BYTES
        return cls(_frame.HEADER_BYTES, int(cfg.payload_bytes), crc_bytes, frame_bytes, info_bits,
                   coded, air, sync_bits, sync_bits + air, max(0, int(cfg.payload_bytes) - overhead))
