"""
Small clients for a container's local faces, for the GUI panel and scripts.

CtlClient     JSON lines on the control face: status(), send(), ping()
StreamClient  one application stream (chat, file): a reader thread collects
              what arrives, send() writes; reconnects when the container
              restarts

Both take an Endpoint spec ("tcp:127.0.0.1:47898" or "unix:/path") and never
raise on a missing container: status() returns None and the caller shows the
link as down.
"""
from __future__ import annotations

import json
import socket
import threading
import time
from typing import Optional

from . import transport as tp


class CtlClient:
    def __init__(self, endpoint, timeout: float = 2.0):
        self.ep = tp.Endpoint.parse(endpoint)
        self.timeout = float(timeout)
        self._sock: Optional[socket.socket] = None
        self._file = None
        self._lock = threading.Lock()
        self.last_error = ""

    def _connect(self):
        s = self.ep.connect(timeout=self.timeout)
        s.settimeout(self.timeout)
        self._sock = s
        self._file = s.makefile("rwb", buffering=0)

    def close(self) -> None:
        with self._lock:
            for x in (self._file, self._sock):
                try:
                    if x is not None:
                        x.close()
                except OSError:
                    pass
            self._sock = self._file = None

    def call(self, req: dict) -> Optional[dict]:
        with self._lock:
            for attempt in (0, 1):
                try:
                    if self._sock is None:
                        self._connect()
                    self._file.write(json.dumps(req).encode("utf-8") + b"\n")
                    line = self._file.readline()
                    if not line:
                        raise OSError("control face closed")
                    return json.loads(line)
                except (OSError, ValueError) as e:
                    self.last_error = str(e)
                    for x in (self._file, self._sock):
                        try:
                            if x is not None:
                                x.close()
                        except OSError:
                            pass
                    self._sock = self._file = None
            return None

    def status(self) -> Optional[dict]:
        r = self.call({"cmd": "status"})
        return r.get("status") if r and r.get("ok") else None

    def send(self, stream: int, data: bytes) -> bool:
        r = self.call({"cmd": "send", "stream": int(stream), "hex": bytes(data).hex()})
        return bool(r and r.get("ok"))

    def ping(self) -> bool:
        r = self.call({"cmd": "ping"})
        return bool(r and r.get("ok"))


class StreamClient:
    def __init__(self, endpoint, name: str = "stream"):
        self.ep = tp.Endpoint.parse(endpoint)
        self.name = name
        self._sock: Optional[socket.socket] = None
        self._lock = threading.Lock()
        self._rx = bytearray()
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True, name=f"stream-{name}")
        self._t.start()

    def connected(self) -> bool:
        return self._sock is not None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                s = self.ep.connect(timeout=2.0)
                s.settimeout(0.5)
            except OSError:
                time.sleep(1.0)
                continue
            self._sock = s
            try:
                while not self._stop.is_set():
                    try:
                        d = s.recv(8192)
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                    if not d:
                        break
                    with self._lock:
                        self._rx.extend(d)
            finally:
                self._sock = None
                try:
                    s.close()
                except OSError:
                    pass

    def send(self, data: bytes) -> bool:
        s = self._sock
        if s is None:
            return False
        try:
            s.sendall(bytes(data))
            return True
        except OSError:
            return False

    def take(self) -> bytes:
        """Everything received since the last take()."""
        with self._lock:
            out = bytes(self._rx)
            self._rx.clear()
            return out

    def close(self) -> None:
        self._stop.set()
        s = self._sock
        if s is not None:
            try:
                s.close()
            except OSError:
                pass
