"""
Stream ids and the file record format carried on the file stream.

A stream is a byte pipe (like TCP). Chat is lines of UTF-8 text. A file is a
record: magic, name, size, sha256, then the bytes, so the receiver can name
the file, show progress and check it. Audio has a reserved stream id.
"""
from __future__ import annotations

import hashlib
import os
import struct
from dataclasses import dataclass, field
from typing import List, Optional

STREAM_CONTROL = 0
STREAM_CHAT = 1
STREAM_FILE = 2
STREAM_AUDIO = 3
NAMES = {STREAM_CONTROL: "control", STREAM_CHAT: "chat", STREAM_FILE: "file", STREAM_AUDIO: "audio"}

FILE_MAGIC = b"DPF1"
_FIXED = struct.Struct("!4sBQ32s")            # magic, name length, size, sha256


def file_record(name: str, data: bytes) -> bytes:
    nm = os.path.basename(name).encode("utf-8")[:255]
    return _FIXED.pack(FILE_MAGIC, len(nm), len(data), hashlib.sha256(data).digest()) + nm + bytes(data)


def file_record_from_path(path: str) -> bytes:
    with open(path, "rb") as f:
        return file_record(os.path.basename(path), f.read())


@dataclass
class ReceivedFile:
    name: str
    size: int
    sha256: str
    data: bytes
    ok: bool


@dataclass
class FileReceiver:
    """Feed bytes from the file stream; completed files come back out."""
    buf: bytearray = field(default_factory=bytearray)
    received: List[ReceivedFile] = field(default_factory=list)
    errors: int = 0

    def progress(self) -> Optional[tuple]:
        """(name, bytes so far, size) of the file in flight, or None."""
        h = self._header()
        if h is None:
            return None
        name, size, _digest, start = h
        return name, min(size, len(self.buf) - start), size

    def _header(self):
        if len(self.buf) < _FIXED.size:
            return None
        magic, nlen, size, digest = _FIXED.unpack_from(self.buf, 0)
        if magic != FILE_MAGIC:
            return None
        if len(self.buf) < _FIXED.size + nlen:
            return None
        name = bytes(self.buf[_FIXED.size:_FIXED.size + nlen]).decode("utf-8", "replace")
        return name, size, digest, _FIXED.size + nlen

    def feed(self, data: bytes) -> List[ReceivedFile]:
        self.buf.extend(data)
        out = []
        while True:
            if len(self.buf) >= 4 and bytes(self.buf[:4]) != FILE_MAGIC:
                # out of step: skip to the next magic
                i = self.buf.find(FILE_MAGIC, 1)
                self.errors += 1
                del self.buf[:i if i > 0 else len(self.buf)]
                continue
            h = self._header()
            if h is None:
                break
            name, size, digest, start = h
            if len(self.buf) < start + size:
                break
            body = bytes(self.buf[start:start + size])
            del self.buf[:start + size]
            rf = ReceivedFile(name, size, digest.hex(), body, hashlib.sha256(body).digest() == digest)
            self.received.append(rf)
            out.append(rf)
        return out
