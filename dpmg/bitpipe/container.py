"""
The bitpipe container: a LinkEngine behind local socket faces.

Faces (all bound the same way, BitpipeConfig.transport: TCP on 127.0.0.1 by
default or a Unix socket path, one implementation behind the switch):

- application face: one listener per stream (app_base_port + stream id, or
  <name>-app<stream>.sock). Bytes a client writes go out on that stream;
  bytes arriving on the stream go to every connected client (buffered until
  one connects). `nc localhost 47901` is a chat terminal.
- bit face (the data plane): one connection to the modem. With
  bit_framing="records" the modem drives it: it sends REC_PULL with the
  number of bits it wants and gets whole frames back (idle frames when the
  bitpipe has nothing), so the RF stream never gaps and the bitpipe keeps
  no clock and no pacing timer; back pressure is the socket itself. The
  modem sends what it demodulated as REC_BITS (hard) or REC_SOFT8 (soft)
  records in any chunking. A REC_HELLO goes each way on connect; the modem's
  hello may name its format and the frame size it expects.
  With bit_framing="raw" the bytes are the packed bits, pushed as fast as the
  socket takes them: for a file or nc, not for a modem.
- control face: JSON lines, one object per line: {"cmd": "status"},
  {"cmd": "send", "stream": 1, "text": "..."} or "hex", {"cmd": "stop"}.
  {"cmd": "ping"} queues a link ping (round trip shows in status quality).
  What the GUI already drives stays on the UDP command protocol; this is the
  bitpipe specific side channel.

In process the container is also usable without sockets: send(stream, data)
and an on_deliver(stream, data) callback (the tests and the loopback use it).
"""
from __future__ import annotations

import json
import logging
import socket
import threading
import time
from typing import Callable, Dict, List, Optional

from . import transport as tp
from .config import BitpipeConfig
from .engine import LinkEngine

log = logging.getLogger("dpmg.bitpipe")
APP_BUFFER_CAP = 4 << 20


class _StreamSink:
    def __init__(self):
        self.clients: List[socket.socket] = []
        self.pending = bytearray()
        self.lock = threading.Lock()

    def deliver(self, data: bytes) -> None:
        with self.lock:
            if not self.clients:
                self.pending.extend(data)
                if len(self.pending) > APP_BUFFER_CAP:
                    del self.pending[:len(self.pending) - APP_BUFFER_CAP]
                return
            dead = []
            for c in self.clients:
                try:
                    c.sendall(data)
                except OSError:
                    dead.append(c)
            for c in dead:
                self.clients.remove(c)

    def attach(self, c: socket.socket) -> None:
        with self.lock:
            self.clients.append(c)
            if self.pending:
                try:
                    c.sendall(bytes(self.pending))
                except OSError:
                    pass
                self.pending.clear()

    def detach(self, c: socket.socket) -> None:
        with self.lock:
            if c in self.clients:
                self.clients.remove(c)


class BitpipeContainer:
    def __init__(self, cfg: BitpipeConfig, crypto=None, on_deliver: Optional[Callable[[int, bytes], None]] = None,
                 app_sockets: bool = True):
        self.cfg = cfg
        self.engine = LinkEngine(cfg, crypto, cfg.name)
        self.on_deliver = on_deliver
        self.app_sockets = app_sockets
        self.sinks: Dict[int, _StreamSink] = {int(s): _StreamSink() for s in cfg.streams}
        self._threads: List[threading.Thread] = []
        self._listeners: List[socket.socket] = []
        self._endpoints: List[tp.Endpoint] = []
        self._stop = threading.Event()
        self._peer: Optional[socket.socket] = None
        self._peer_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self.started = False
        self.peer_hello: dict = {}
        self.bit_stats = {"bytes_tx": 0, "bytes_rx": 0, "connections": 0, "errors": 0, "pulls": 0,
                          "frames_sent": 0, "records_rx": 0, "format": cfg.bit_format}

    # lifecycle
    def start(self) -> None:
        if self.started:
            return
        self._stop.clear()
        # bind every listening face here, before returning, so a client that
        # connects right after start() never races the accept threads
        if self.app_sockets:
            for s in self.cfg.streams:
                ls = self._listener(self.app_endpoint(int(s)))
                self._spawn(self._accept_loop, ls, (lambda c, _s=int(s): self._spawn(self._app_client, _s, c)))
        if self.cfg.ctl_enabled:
            ls = self._listener(self.ctl_endpoint())
            self._spawn(self._accept_loop, ls, lambda c: self._spawn(self._ctl_client, c))
        if self.cfg.bit_connect:
            self._spawn(self._bit_connect)
        else:
            ls = self._listener(self.bit_endpoint())
            self._spawn(self._accept_loop, ls, self._serve_peer)
        self.started = True

    def stop(self) -> None:
        self._stop.set()
        for ls in self._listeners:
            try:
                ls.close()
            except OSError:
                pass
        self._close_peer()
        for sink in self.sinks.values():
            for c in list(sink.clients):
                try:
                    c.close()
                except OSError:
                    pass
        for t in self._threads:
            t.join(timeout=2.0)
        for ep in self._endpoints:
            ep.cleanup()
        self.started = False

    def _spawn(self, fn, *a) -> None:
        t = threading.Thread(target=fn, args=a, daemon=True, name=f"{self.cfg.name}-{fn.__name__}")
        t.start()
        self._threads.append(t)

    def _listener(self, ep: tp.Endpoint) -> socket.socket:
        ls = ep.listen()
        self._listeners.append(ls)
        self._endpoints.append(ep)
        return ls

    def _accept_loop(self, ls: socket.socket, handler) -> None:
        while not self._stop.is_set():
            try:
                c, _addr = ls.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            handler(c)

    # application face
    def send(self, stream: int, data: bytes) -> None:
        self.engine.send(stream, data)

    def _dispatch(self, stream: int, data: bytes) -> None:
        if self.on_deliver is not None:
            try:
                self.on_deliver(stream, data)
            except Exception:                                    # noqa: BLE001
                log.exception("on_deliver")
        sink = self.sinks.get(int(stream))
        if sink is not None and self.app_sockets:
            sink.deliver(data)

    def app_endpoint(self, stream: int) -> tp.Endpoint:
        return self.cfg.endpoint("app", stream)

    def bit_endpoint(self) -> tp.Endpoint:
        return self.cfg.endpoint("bit")

    def ctl_endpoint(self) -> tp.Endpoint:
        return self.cfg.endpoint("ctl")

    def _app_client(self, stream: int, c: socket.socket) -> None:
        sink = self.sinks[stream]
        sink.attach(c)
        c.settimeout(0.5)
        try:
            while not self._stop.is_set():
                try:
                    data = c.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not data:
                    break
                self.engine.send(stream, data)
        finally:
            sink.detach(c)
            try:
                c.close()
            except OSError:
                pass

    # control face (JSON lines)
    def handle_ctl(self, req: dict) -> dict:
        cmd = str(req.get("cmd", ""))
        if cmd == "status":
            return {"ok": True, "status": self.status()}
        if cmd == "send":
            stream = int(req.get("stream", 1))
            if "hex" in req:
                data = bytes.fromhex(str(req["hex"]))
            else:
                data = str(req.get("text", "")).encode("utf-8")
            self.engine.send(stream, data)
            return {"ok": True, "queued": len(data), "stream": stream}
        if cmd == "ping":
            return {"ok": True, "ping_id": self.engine.ping()}
        if cmd == "stop":
            threading.Thread(target=self.stop, daemon=True).start()
            return {"ok": True, "stopping": True}
        return {"ok": False, "err": f"unknown cmd {cmd!r}"}

    def _ctl_client(self, c: socket.socket) -> None:
        c.settimeout(0.5)
        buf = bytearray()
        try:
            while not self._stop.is_set():
                try:
                    data = c.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not data:
                    break
                buf.extend(data)
                while b"\n" in buf:
                    line, _, rest = bytes(buf).partition(b"\n")
                    buf = bytearray(rest)
                    if not line.strip():
                        continue
                    try:
                        req = json.loads(line.decode("utf-8"))
                        resp = self.handle_ctl(req if isinstance(req, dict) else {})
                    except Exception as ex:                      # noqa: BLE001
                        resp = {"ok": False, "err": str(ex)}
                    c.sendall(json.dumps(resp, default=str).encode("utf-8") + b"\n")
        finally:
            try:
                c.close()
            except OSError:
                pass

    # bit face
    def _bit_connect(self) -> None:
        ep = tp.Endpoint.parse(self.cfg.bit_connect)
        while not self._stop.is_set():
            try:
                c = ep.connect(timeout=2.0)
            except OSError:
                time.sleep(0.5)
                continue
            self._serve_peer(c)

    def _serve_peer(self, c: socket.socket) -> None:
        """Run the data plane over one peer connection until it drops."""
        tp.tune(c)
        with self._peer_lock:
            self._close_peer()
            self._peer = c
        self.bit_stats["connections"] += 1
        try:
            if self.cfg.bit_framing == "records":
                self._records_loop(c)
            else:
                self._raw_loop(c)
        finally:
            with self._peer_lock:
                if self._peer is c:
                    self._peer = None
            try:
                c.close()
            except OSError:
                pass

    def _close_peer(self) -> None:
        p = self._peer
        if p is not None:
            try:
                p.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                p.close()
            except OSError:
                pass

    def _send(self, c: socket.socket, data: bytes) -> bool:
        with self._send_lock:
            try:
                c.sendall(data)
            except OSError:
                return False
        self.bit_stats["bytes_tx"] += len(data)
        return True

    def hello(self) -> dict:
        s = self.engine.sizes
        return {"bitpipe": self.cfg.name, "block_bits": s.block_bits, "sync_bits": s.sync_bits,
                "payload_bytes": s.payload_bytes, "frame_bytes": s.frame_bytes, "data_capacity": s.data_capacity,
                "fec": self.cfg.fec, "crypto": self.engine.crypto.name, "rx_format": self.cfg.bit_format,
                "pull": True}

    def _on_hello(self, obj: dict) -> None:
        self.peer_hello = obj
        fmt = obj.get("format") or obj.get("tx_format")
        if fmt in ("packed", "soft8"):
            self.bit_stats["format"] = fmt
        want = obj.get("payload_bytes")
        if want is not None and int(want) != self.engine.sizes.payload_bytes:
            self.bit_stats["errors"] += 1
            self.engine.stats["last_error"] = (f"modem expects {want} B frames, "
                                               f"container runs {self.engine.sizes.payload_bytes} B")

    def _records_loop(self, c: socket.socket) -> None:
        self._send(c, tp.pack_json(tp.REC_HELLO, self.hello()))
        reader = tp.RecordReader()
        while not self._stop.is_set():
            try:
                data = c.recv(8192)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break
            self.bit_stats["bytes_rx"] += len(data)
            for rtype, payload in reader.feed(data):
                self.bit_stats["records_rx"] += 1
                try:
                    if not self._on_record(c, rtype, payload):
                        return
                except Exception:                                # noqa: BLE001
                    log.exception("bitpipe %s: record %d", self.cfg.name, rtype)
                    self.bit_stats["errors"] += 1

    def _on_record(self, c: socket.socket, rtype: int, payload: bytes) -> bool:
        if rtype == tp.REC_PULL:
            want = tp.BITS_HDR.unpack_from(payload, 0)[0] if len(payload) >= 4 else self.engine.sizes.block_bits
            self.bit_stats["pulls"] += 1
            sent = 0
            while sent < max(1, want):
                bits = self.engine.next_frame_bits()
                if not self._send(c, tp.pack_record(tp.REC_BITS, tp.pack_bits(bits))):
                    return False
                sent += len(bits)
                self.bit_stats["frames_sent"] += 1
            return True
        if rtype == tp.REC_BITS:
            bits = tp.unpack_bits(payload)
            if bits is None:
                self.bit_stats["errors"] += 1
                return True
            for stream, data in self.engine.feed_hard(bits):
                self._dispatch(stream, data)
            return True
        if rtype == tp.REC_SOFT8:
            for stream, data in self.engine.feed_soft8(payload):
                self._dispatch(stream, data)
            return True
        if rtype == tp.REC_HELLO:
            obj = tp.parse_json(payload)
            if obj:
                self._on_hello(obj)
            return True
        if rtype == tp.REC_STATUS:
            return self._send(c, tp.pack_json(tp.REC_STATUS, self.status()))
        if rtype == tp.REC_MODEM:
            obj = tp.parse_json(payload)
            if obj is not None:
                obj["received_at"] = time.time()
                self.engine.modem_status = obj
            return True
        self.bit_stats["errors"] += 1
        return True

    def _raw_loop(self, c: socket.socket) -> None:
        """Raw bytes both ways: push frames as fast as the socket takes them,
        decode whatever arrives (packed or soft8 per bit_format)."""
        done = threading.Event()

        def pusher():
            while not (self._stop.is_set() or done.is_set()):
                try:
                    data = self.engine.next_frame_packed()
                except Exception:                                # noqa: BLE001
                    log.exception("bitpipe %s: next frame", self.cfg.name)
                    self.bit_stats["errors"] += 1
                    time.sleep(0.1)
                    continue
                if not self._send(c, data):
                    break
                self.bit_stats["frames_sent"] += 1
            done.set()

        t = threading.Thread(target=pusher, daemon=True, name=f"{self.cfg.name}-raw-tx")
        t.start()
        feed = self.engine.feed_soft8 if self.cfg.bit_format == "soft8" else self.engine.feed_packed
        try:
            while not (self._stop.is_set() or done.is_set()):
                try:
                    data = c.recv(8192)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not data:
                    break
                self.bit_stats["bytes_rx"] += len(data)
                try:
                    for stream, payload in feed(data):
                        self._dispatch(stream, payload)
                except Exception:                                # noqa: BLE001
                    log.exception("bitpipe %s: rx", self.cfg.name)
                    self.bit_stats["errors"] += 1
        finally:
            done.set()
            t.join(timeout=2.0)

    # status
    def connected(self) -> bool:
        return self._peer is not None

    def status(self) -> dict:
        d = self.engine.status()
        d["bit_face"] = dict(self.bit_stats, connected=self.connected(), framing=self.cfg.bit_framing,
                             endpoint=self.bit_endpoint().describe(), peer_hello=self.peer_hello)
        d["app_face"] = {int(s): {"endpoint": self.app_endpoint(s).describe(),
                                  "clients": len(self.sinks[int(s)].clients),
                                  "buffered": len(self.sinks[int(s)].pending)} for s in self.cfg.streams}
        d["ctl_face"] = self.ctl_endpoint().describe() if self.cfg.ctl_enabled else None
        d["transport"] = self.cfg.transport
        return d
