"""
ModemLink: the modem side of the bitpipe data plane.

It connects to a container's bit face as the pull client, modulates the
frames it pulls into IQ at the radio's TX rate without gaps, and turns the
radio's RX stream into soft bits that go back to the container. The radio
code (or the offline harness) only calls two things:

    next_tx_block()      the next block of TX IQ (block_s seconds at tx_fs)
    on_rx(iq, fs)        a chunk of RX IQ at the radio's RX rate

Who dials whom: by default the modem connects to the container's bit face.
With listen= the modem listens instead and the container dials in (its
bit_connect setting). That is the topology for a container on the laptop
and a modem on a radio: the laptop's firewall drops new inbound connections,
while its outbound connection to the radio and the replies on it pass.
allow_from limits the listener to one peer address.

Pulling: a block needs a known number of bits; when the reservoir is short
the link sends one pull for the shortfall and waits for the container's
frames (idle frames when it has nothing), so the modem is the only clock and
the stream never gaps. With no container connected the block is silence and
underruns are counted; a radio should not be keyed in that state.

Nothing here opens a radio. dpmg_service.py wires next_tx_block into the
scheduler's TX loop and on_rx into the RX frame hook; the offline harness
(sim_link.py) joins two of these through an IqChannel.
"""
from __future__ import annotations

import logging
import socket
import threading
import time
from collections import deque
from typing import Optional

import numpy as np

from . import transport as tp
from .stream_modem import StreamDemodulator, StreamModulator, WaveformConfig

log = logging.getLogger("dpmg.bitpipe.modem")


class ModemLink:
    def __init__(self, wf: WaveformConfig, container, tx_fs: float, rx_fs: float, offset_hz: float = 0.0,
                 block_s: float = 0.05, name: str = "modem", soft: bool = True, pull_timeout_s: float = 2.0,
                 listen=None, allow_from: Optional[str] = None, block_multiple: int = 1):
        self.wf = wf
        self.listen_ep = tp.Endpoint.parse(listen) if listen else None
        self.allow_from = allow_from or None
        self.ep = self.listen_ep if self.listen_ep is not None else tp.Endpoint.parse(container)
        self._listener: Optional[socket.socket] = None
        self.tx_fs = float(tx_fs)
        self.rx_fs = float(rx_fs)
        self.offset_hz = float(offset_hz)
        self.block_s = float(block_s)
        self.name = name
        self.soft = bool(soft)
        self.pull_timeout_s = float(pull_timeout_s)
        self.mod = StreamModulator(wf, self.tx_fs)
        self.demod = StreamDemodulator(wf, self.rx_fs, offset_hz=self.offset_hz)
        # TX blocks are a whole number of the radio's own TX block: the G40's
        # bridge pads every send to a whole card block with zeros, so any other
        # length puts a hole in the stream each block and the receiver slips
        m = max(1, int(block_multiple or 1))
        self.block_multiple = m
        self.block_samples = max(1, int(round(self.block_s * self.tx_fs / m))) * m
        self._bits: deque = deque()                 # arrays of bits pulled, not yet modulated
        self._nbits = 0
        self._iq_rem = np.zeros(0, dtype=np.complex64)
        self._cv = threading.Condition()
        self._sock: Optional[socket.socket] = None
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.hello: dict = {}
        self.container_status: dict = {}
        self._status_cv = threading.Condition()
        self.stats = {"pulls": 0, "frames_in": 0, "bits_in": 0, "blocks_out": 0, "underruns": 0,
                      "rx_chunks": 0, "llr_out": 0, "records_out": 0, "connected": False,
                      "connections": 0, "errors": 0, "refused_peers": 0, "peer": ""}
        self._nco = 0.0                             # TX offset mixer phase
        self.status_period_s = 1.0
        self._last_status = 0.0

    # lifecycle
    def start(self) -> None:
        """Start the link thread. In listen mode the port is bound here, so a
        port in use or a bad address raises to the caller."""
        if self._thread is not None:
            return
        self._stop.clear()
        if self.listen_ep is not None:
            self._listener = self.listen_ep.listen(backlog=1, timeout=0.5)
        self._thread = threading.Thread(target=self._run_listen if self._listener is not None else self._run,
                                        daemon=True, name=f"{self.name}-link")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
            self._listener = None
            if self.listen_ep is not None:
                self.listen_ep.cleanup()
        s = self._sock
        if s is not None:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        with self._cv:
            self._cv.notify_all()

    def connected(self) -> bool:
        return self._sock is not None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                s = tp.tune(self.ep.connect(timeout=2.0))
            except OSError:
                time.sleep(0.5)
                continue
            self._serve(s, self.ep.describe())

    def _run_listen(self) -> None:
        ls = self._listener
        while not self._stop.is_set() and ls is not None:
            try:
                s, addr = ls.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            peer = addr[0] if isinstance(addr, tuple) else str(addr or "local")
            if self.allow_from and isinstance(addr, tuple) and peer != self.allow_from:
                self.stats["refused_peers"] += 1
                log.warning("%s: refused a container from %s (allow_from %s)", self.name, peer, self.allow_from)
                try:
                    s.close()
                except OSError:
                    pass
                continue
            self._serve(tp.tune(s), peer)          # one container at a time; the next waits in the backlog

    def _serve(self, s: socket.socket, peer: str) -> None:
        self._sock = s
        self.stats["connected"] = True
        self.stats["connections"] += 1
        self.stats["peer"] = peer
        try:
            self._send(tp.pack_json(tp.REC_HELLO, self.my_hello()))
            self._reader(s)
        finally:
            self._sock = None
            self.stats["connected"] = False
            try:
                s.close()
            except OSError:
                pass
            with self._cv:
                self._cv.notify_all()

    def my_hello(self) -> dict:
        return {"modem": self.name, "format": "soft8" if self.soft else "packed", "modulation": self.wf.kind,
                "symbol_rate": self.mod.symbol_rate, "tx_fs": self.tx_fs, "rx_fs": self.rx_fs,
                "offset_hz": self.offset_hz}

    def _send(self, data: bytes) -> bool:
        s = self._sock
        if s is None:
            return False
        with self._send_lock:
            try:
                s.sendall(data)
                return True
            except OSError:
                return False

    def _reader(self, s: socket.socket) -> None:
        reader = tp.RecordReader()
        while not self._stop.is_set():
            try:
                data = s.recv(16384)
            except socket.timeout:
                continue
            except OSError:
                return
            if not data:
                return
            for rtype, payload in reader.feed(data):
                if rtype == tp.REC_BITS:
                    bits = tp.unpack_bits(payload)
                    if bits is None:
                        self.stats["errors"] += 1
                        continue
                    with self._cv:
                        self._bits.append(bits)
                        self._nbits += len(bits)
                        self.stats["frames_in"] += 1
                        self.stats["bits_in"] += len(bits)
                        self._cv.notify_all()
                elif rtype == tp.REC_HELLO:
                    self.hello = tp.parse_json(payload) or {}
                elif rtype == tp.REC_STATUS:
                    with self._status_cv:
                        self.container_status = tp.parse_json(payload) or {}
                        self._status_cv.notify_all()

    # TX
    def _bits_for_samples(self, n_samples: int) -> int:
        return int(np.ceil(n_samples / self.mod.sps)) * self.wf.bits_per_symbol

    def _take_bits(self, want: int) -> np.ndarray:
        """Up to `want` bits from the reservoir, pulling the shortfall from the
        container and waiting at most pull_timeout_s for it."""
        with self._cv:
            if self._nbits < want and self._sock is not None:
                self.stats["pulls"] += 1
                self._cv.release()
                try:
                    self._send(tp.pack_record(tp.REC_PULL, tp.BITS_HDR.pack(want - self._nbits)))
                finally:
                    self._cv.acquire()
                deadline = time.monotonic() + self.pull_timeout_s
                while self._nbits < want and self._sock is not None and not self._stop.is_set():
                    left = deadline - time.monotonic()
                    if left <= 0:
                        break
                    self._cv.wait(left)
            out = []
            n = 0
            while self._bits and n < want:
                b = self._bits[0]
                take = min(len(b), want - n)
                out.append(b[:take])
                if take < len(b):
                    self._bits[0] = b[take:]
                else:
                    self._bits.popleft()
                n += take
            self._nbits -= n
        return np.concatenate(out) if out else np.zeros(0, dtype=np.uint8)

    def next_tx_block(self) -> np.ndarray:
        """block_samples of IQ at tx_fs: the bitpipe stream modulated without
        gaps, shifted by offset_hz. Silence (and an underrun count) only when
        no container is connected or it did not answer the pull in time."""
        need = self.block_samples
        parts = [self._iq_rem]
        have = len(self._iq_rem)
        if have < need:
            want = self._bits_for_samples(need - have)
            bits = self._take_bits(want)
            if len(bits):
                parts.append(self.mod.modulate(bits))
                have += len(parts[-1])
        if have < need:
            self.stats["underruns"] += 1
            parts.append(np.zeros(need - have, dtype=np.complex64))
        iq = np.concatenate(parts)
        out, self._iq_rem = iq[:need], iq[need:]
        if self.offset_hz:
            ph = self._nco + 2.0 * np.pi * self.offset_hz / self.tx_fs * np.arange(1, need + 1)
            out = (out * np.exp(1j * (ph - 2.0 * np.pi * self.offset_hz / self.tx_fs))).astype(np.complex64)
            self._nco = float(ph[-1] % (2.0 * np.pi))
        self.stats["blocks_out"] += 1
        return out.astype(np.complex64)

    # RX
    def on_rx(self, iq, fs: Optional[float] = None) -> int:
        """A chunk of RX IQ; returns the number of soft bits sent to the container."""
        if fs is not None and abs(float(fs) - self.rx_fs) > 0.5:
            self.rx_fs = float(fs)
            self.demod = StreamDemodulator(self.wf, self.rx_fs, offset_hz=self.offset_hz)
        llr = self.demod.demodulate(iq)
        self.stats["rx_chunks"] += 1
        self._maybe_push_status()                 # also while the receiver hears nothing
        if len(llr) == 0:
            return 0
        self.stats["llr_out"] += len(llr)
        if self.soft:
            rec = tp.pack_record(tp.REC_SOFT8, np.clip(llr * 16.0, -127, 127).astype(np.int8).tobytes())
        else:
            rec = tp.pack_record(tp.REC_BITS, tp.pack_bits((llr > 0).astype(np.uint8)))
        if self._send(rec):
            self.stats["records_out"] += 1
        return len(llr)

    def _maybe_push_status(self) -> None:
        now = time.monotonic()
        if now - self._last_status >= self.status_period_s:
            self._last_status = now
            self._send(tp.pack_json(tp.REC_MODEM, self.status()))

    # status
    def request_container_status(self, timeout: float = 1.0) -> dict:
        with self._status_cv:
            self.container_status = {}
            if not self._send(tp.pack_record(tp.REC_STATUS, b"")):
                return {}
            self._status_cv.wait(timeout)
            return dict(self.container_status)

    def describe_ep(self) -> str:
        return ("listen:" + self.ep.describe()) if self.listen_ep is not None else self.ep.describe()

    def brief(self) -> dict:
        """The modem in a few hundred bytes: what a UDP command ack carries
        (one datagram, under 1400 bytes). status() has the rest."""
        d = self.demod.stats
        s = self.stats
        return {"mod": self.wf.kind, "baud": round(self.mod.symbol_rate, 3), "offset_hz": self.offset_hz,
                "tx_block": self.block_samples,
                "tx_fs": self.tx_fs, "rx_fs": self.rx_fs, "container": self.describe_ep(), "peer": s.get("peer", ""),
                "connected": bool(s["connected"]), "pulls": s["pulls"], "frames_in": s["frames_in"],
                "underruns": s["underruns"], "rx_chunks": s["rx_chunks"], "errors": s["errors"],
                "acquired": bool(d.get("acquired")), "cfo_hz": round(float(d.get("cfo_hz", 0.0)), 1),
                "eye": round(float(d.get("eye", 0.0)), 3)}

    def status(self) -> dict:
        return {"name": self.name, "container": self.describe_ep(), "waveform": {
            "modulation": self.wf.kind, "symbol_rate": self.mod.symbol_rate, "tones": self.wf.tone_list(),
            "offset_hz": self.offset_hz, "tx_fs": self.tx_fs, "rx_fs": self.rx_fs, "tx_sps": self.mod.sps},
            "stats": dict(self.stats, reservoir_bits=self._nbits), "demod": self.demod.status(),
            "container_hello": dict(self.hello)}


def container_brief(st: Optional[dict]) -> Optional[dict]:
    """A container status reduced to what fits beside the modem brief in one
    command ack: lock, traffic, quality. None passes through."""
    if not st:
        return None
    q = st.get("quality", {}) or {}
    return {"locked": st.get("locked"), "peer_seen": st.get("peer_seen"), "link_down": st.get("link_down"),
            "frames_tx": st.get("frames_tx"), "data_tx": st.get("data_tx"), "retx": st.get("tx", {}).get("retx"),
            "frames_rx": st.get("frames_rx"), "frames_bad": st.get("deframer", {}).get("frames_bad"),
            "open_failed": st.get("open_failed"), "unacked": st.get("unacked"),
            "pending_bytes": st.get("pending_bytes"), "bytes_out": st.get("bytes_out"),
            "ber": q.get("ber"), "fer": q.get("fer"), "rtt_s": q.get("rtt_s"),
            "crypto": (st.get("crypto") or {}).get("crypto")}
