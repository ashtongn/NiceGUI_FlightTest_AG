"""
LinkEngine: the whole bitpipe with no sockets and no threads.

    send(stream, data)          queue application bytes for a stream
    next_frame_bits()           the next block for the TX bit face (sync + coded bits)
    feed_bits(llr)              soft bits from the RX bit face; returns (stream, bytes) delivered
    feed_packed(data)           the same from packed hard bits
    status()                    counters for the panel

Order on TX: segment per stream, mux picks the next stream round robin, inner
record (stream, seq, data), seal, link header with ack and sack, CRC, FEC,
interleave, sync. With nothing to send an idle frame goes out so the stream
never stops and the far end keeps lock; a frame due for retransmission goes
before new data. On RX the reverse: deinterleave, decode, CRC, ack
bookkeeping for the peer's frames, reorder and dedup, open, header binding
check, deliver in order per stream.
"""
from __future__ import annotations

import struct
import threading
import time
from collections import OrderedDict, deque
from typing import Dict, List, Optional, Tuple

import numpy as np

from . import coding, frame as fr
from .arq import RxReorder, TxWindow
from .config import BitpipeConfig
from .crypto import NullCrypto
from .deframer import Deframer, sync_pattern

CONTROL_STREAM = 0


class LinkEngine:
    def __init__(self, cfg: BitpipeConfig, crypto=None, name: str = ""):
        self.cfg = cfg
        self.sizes = cfg.validate()
        self.name = name or cfg.name
        self.crypto = crypto or NullCrypto()
        self.lock = threading.RLock()
        self.tx = TxWindow(cfg.window, cfg.rto_frames, cfg.max_retries, cfg.rto_max_frames)
        self.rx = RxReorder(cfg.window)
        self.deframer = Deframer(cfg.sync_hex, self.sizes.air_bits, cfg.sync_max_errors, cfg.lock_miss_limit)
        self._sync_bits = (sync_pattern(cfg.sync_hex) > 0).astype(np.uint8)
        # whitening: a fixed PN sequence over the coded block, so an idle frame
        # of zeros still looks random on the air (the FSK threshold, the symbol
        # timing line and the PSK carrier loop all want balanced transitions)
        self._pn = (np.random.default_rng(0x1ACF).integers(0, 2, self.sizes.air_bits).astype(np.uint8)
                    if cfg.whiten else None)
        self._queues: "OrderedDict[int, bytearray]" = OrderedDict()
        self._rr: List[int] = []
        self._delivered: List[Tuple[int, bytes]] = []
        # link quality: channel bit errors counted by re-encoding every frame
        # that passed the CRC and comparing with the hard decisions that
        # arrived (the BER before FEC); the window keeps the last 64 frames
        self._ber_win: deque = deque(maxlen=64)
        self.ber_errors = 0
        self.ber_bits = 0
        self._pings: dict = {}
        self._ping_id = 0
        self.ping_rtt_s = None
        self.ping_rtt_last_s = None
        self.modem_status: dict = {}
        self._fer_hist: deque = deque()            # (time, bits seen, frames ok) for the windowed FER
        self.fer_window_s = 30.0
        self.stats = {"frames_tx": 0, "idle_tx": 0, "data_tx": 0, "frames_rx": 0, "data_rx": 0,
                      "idle_rx": 0, "seal_refused": 0, "open_failed": 0, "bind_mismatch": 0,
                      "bytes_in": 0, "bytes_out": 0, "peer_seen": False, "own_rx": 0}
        self._peer_t = None                        # monotonic time of the last frame from the peer

    # application side
    def send(self, stream: int, data: bytes) -> None:
        with self.lock:
            q = self._queues.setdefault(int(stream), bytearray())
            q.extend(bytes(data))
            self.stats["bytes_in"] += len(data)

    def pending_bytes(self, stream: Optional[int] = None) -> int:
        with self.lock:
            if stream is None:
                return sum(len(q) for q in self._queues.values())
            return len(self._queues.get(int(stream), b""))

    def unacked(self) -> int:
        with self.lock:
            return self.tx.outstanding()

    def idle(self) -> bool:
        """Nothing queued and nothing waiting for an acknowledgement."""
        with self.lock:
            return self.pending_bytes() == 0 and self.tx.outstanding() == 0

    # TX
    def _next_stream(self) -> Optional[int]:
        live = [s for s, q in self._queues.items() if len(q)]
        if not live:
            return None
        order = [s for s in self._rr if s in live] + [s for s in live if s not in self._rr]
        s = order[0]
        self._rr = order[1:] + [s]
        return s

    def next_frame(self) -> bytes:
        """The next link frame (before FEC), fixed length."""
        with self.lock:
            ack, sack = self.rx.ack_fields()
            hdr = fr.FrameHeader(ack=ack, sack=sack)
            payload = b""
            e = self.tx.pick_retransmit()
            if e is not None:
                hdr.flags = fr.FLAG_DATA | fr.FLAG_RETX
                hdr.stream, hdr.seq, payload = e.stream, e.seq, e.body
                self.tx.sent(e, retransmit=True)
                self.stats["data_tx"] += 1
            else:
                s = self._next_stream() if self.tx.can_send_new() else None
                if s is not None:
                    q = self._queues[s]
                    n = min(len(q), self.sizes.data_capacity)
                    chunk = bytes(q[:n])
                    seq = self.tx.next_seq
                    inner = fr.pack_inner(s, seq, chunk) if self.cfg.bind_header else chunk
                    try:
                        body = self.crypto.seal(inner)
                    except Exception as ex:                     # noqa: BLE001
                        self.stats["seal_refused"] += 1
                        self.stats["last_error"] = f"seal: {ex}"
                        s = None
                    else:
                        del q[:n]
                        e = self.tx.add(seq, s, body)
                        self.tx.sent(e, retransmit=False)
                        hdr.flags = fr.FLAG_DATA
                        hdr.stream, hdr.seq, payload = s, seq, body
                        self.stats["data_tx"] += 1
                if s is None:
                    hdr.flags = fr.FLAG_IDLE
                    hdr.seq = self.tx.next_seq
                    self.stats["idle_tx"] += 1
            if self.cfg.station == 1:
                hdr.flags |= fr.FLAG_STATION
            self.tx.advance()
            self.stats["frames_tx"] += 1
            return fr.pack_frame(hdr, payload, self.sizes.payload_bytes, self.cfg.crc)

    def frame_to_bits(self, frame_bytes: bytes) -> np.ndarray:
        info = coding.bytes_to_bits(frame_bytes)
        coded = coding.encode_block(info, self.cfg.fec)
        air = coding.interleave(coded, self.cfg.interleave_depth).astype(np.uint8)
        if self._pn is not None:
            air = air ^ self._pn
        return np.concatenate([self._sync_bits, air])

    def next_frame_bits(self) -> np.ndarray:
        """sync + interleaved coded bits of the next frame (uint8 0/1)."""
        return self.frame_to_bits(self.next_frame())

    def next_frame_packed(self) -> bytes:
        """The same as bytes, 8 bits a byte, MSB first (block_bits is a
        multiple of 8 only when the sizes allow; pad bits would break the
        stream, so the engine keeps a bit remainder between calls)."""
        bits = self.next_frame_bits()
        with self.lock:
            rem = getattr(self, "_tx_rem", np.zeros(0, dtype=np.uint8))
            allb = np.concatenate([rem, bits])
            n = (len(allb) // 8) * 8
            self._tx_rem = allb[n:]
            return np.packbits(allb[:n]).tobytes()

    # RX
    def _try_block(self, block_llr: np.ndarray) -> bool:
        if self._pn is not None:
            block_llr = np.where(self._pn == 1, -block_llr, block_llr)
        coded = coding.deinterleave(block_llr, self.cfg.interleave_depth, self.sizes.coded_bits)
        info, ok = coding.decode_block(coded, self.cfg.fec, self.sizes.info_bits)
        if not ok:
            return False
        parsed = fr.unpack_frame(coding.bits_to_bytes(info), self.sizes.payload_bytes, self.cfg.crc)
        if parsed is None:
            return False
        hdr, payload = parsed
        if self.cfg.station is not None and hdr.station == self.cfg.station:
            self.stats["own_rx"] += 1              # our own TX heard in our RX: keeps the
            return True                            # deframer locked, carries nothing for us
        self._peer_t = time.monotonic()
        self.stats["frames_rx"] += 1
        self.stats["peer_seen"] = True
        self._count_channel_errors(info, coded)
        self.tx.on_ack(hdr.ack, hdr.sack)
        if not hdr.data:
            self.stats["idle_rx"] += 1
            return True
        self.stats["data_rx"] += 1
        for seq, stream, body in self.rx.offer(hdr.seq, hdr.stream, payload):
            ok, rec = self.crypto.open(body)
            if not ok:
                self.stats["open_failed"] += 1
                self.stats["last_error"] = f"open: {getattr(self.crypto, 'last_error', '')}"
                continue
            if self.cfg.bind_header:
                inner = fr.unpack_inner(rec)
                if inner is None or inner[0] != stream or inner[1] != seq:
                    self.stats["bind_mismatch"] += 1
                    continue
                data = inner[2]
            else:
                data = rec
            if stream == CONTROL_STREAM and self._control(data):
                continue
            self._delivered.append((stream, data))
            self.stats["bytes_out"] += len(data)
        return True

    def _count_channel_errors(self, info: np.ndarray, coded_llr: np.ndarray) -> None:
        try:
            ref = coding.encode_block(info, self.cfg.fec)
        except Exception:                                        # noqa: BLE001
            return
        hard = (np.asarray(coded_llr)[:len(ref)] > 0).astype(np.uint8)
        e = int(np.count_nonzero(hard != ref[:len(hard)]))
        self._ber_win.append((e, len(hard)))
        self.ber_errors += e
        self.ber_bits += len(hard)

    # control stream: link ping
    # stream 0 records the engine answers itself: b"PI" id(4) t(8) is echoed as
    # b"PO" id(4) t(8); the sender's round trip includes queueing, ARQ and both
    # modems, which is what a user of the link sees
    def ping(self) -> int:
        with self.lock:
            self._ping_id = (self._ping_id + 1) & 0xFFFFFFFF
            t = time.monotonic()
            self._pings[self._ping_id] = t
            if len(self._pings) > 32:
                self._pings.pop(next(iter(self._pings)))
            self.send(CONTROL_STREAM, b"PI" + struct.pack("!Id", self._ping_id, t))
            return self._ping_id

    def _control(self, data: bytes) -> bool:
        if len(data) == 14 and data[:2] == b"PI":
            self.send(CONTROL_STREAM, b"PO" + data[2:])
            return True
        if len(data) == 14 and data[:2] == b"PO":
            pid, _t = struct.unpack("!Id", data[2:])
            t0 = self._pings.pop(pid, None)
            if t0 is not None:
                r = time.monotonic() - t0
                self.ping_rtt_last_s = r
                self.ping_rtt_s = r if self.ping_rtt_s is None else 0.7 * self.ping_rtt_s + 0.3 * r
            return True
        return False

    def quality(self) -> dict:
        """BER before FEC (last 64 frames, and total), frame error rate on the
        receive side over the last fer_window_s (frames that should have
        arrived, from the bit count, against frames that passed the CRC),
        round trips."""
        with self.lock:
            we = sum(e for e, _n in self._ber_win)
            wn = sum(n for _e, n in self._ber_win)
            seen = self.deframer.stats["bits_in"] / float(self.sizes.block_bits)
            ok = self.deframer.stats["frames_ok"]
            now = time.monotonic()
            self._fer_hist.append((now, seen, ok))
            while len(self._fer_hist) > 2 and now - self._fer_hist[1][0] >= self.fer_window_s:
                self._fer_hist.popleft()
            t0, seen0, ok0 = self._fer_hist[0]
            dseen, dok = seen - seen0, ok - ok0
            if dseen >= 4:                         # recent window: the link as it is now
                fer = min(1.0, max(0.0, 1.0 - dok / dseen))
            else:
                fer = max(0.0, 1.0 - ok / seen) if seen >= 1 else None
            return {"ber": (we / wn) if wn else None, "ber_total": (self.ber_errors / self.ber_bits) if self.ber_bits else None,
                    "ber_bits": self.ber_bits, "fer": fer, "frames_on_air": round(seen, 1), "frames_ok": ok,
                    "rtt_s": round(self.tx.rtt_s, 3) if self.tx.rtt_s else None,
                    "ping_rtt_s": round(self.ping_rtt_s, 3) if self.ping_rtt_s is not None else None,
                    "ping_rtt_last_s": round(self.ping_rtt_last_s, 3) if self.ping_rtt_last_s is not None else None,
                    "retx_ratio": (self.tx.stats["retx"] / max(1, self.tx.stats["sent"] + self.tx.stats["retx"]))}

    def feed_bits(self, llr: np.ndarray) -> List[Tuple[int, bytes]]:
        """Soft bits (llr > 0 = bit 1) in; (stream, bytes) delivered in order out."""
        with self.lock:
            self.deframer.feed(llr, self._try_block)
            out, self._delivered = self._delivered, []
            return out

    def feed_packed(self, data: bytes) -> List[Tuple[int, bytes]]:
        bits = np.unpackbits(np.frombuffer(bytes(data), dtype=np.uint8))
        return self.feed_hard(bits)

    def feed_hard(self, bits) -> List[Tuple[int, bytes]]:
        """Hard bits (uint8 0/1) in."""
        b = np.asarray(bits, dtype=np.uint8).reshape(-1)
        return self.feed_bits(np.where(b == 1, 1.0, -1.0).astype(np.float32))

    def feed_soft8(self, data: bytes) -> List[Tuple[int, bytes]]:
        """One signed byte per bit, +127 = certain 1, -127 = certain 0."""
        return self.feed_bits(np.frombuffer(bytes(data), dtype=np.int8).astype(np.float32) / 16.0)

    # status
    def status(self) -> dict:
        with self.lock:
            d = dict(self.stats)
            d.update({"name": self.name, "locked": self.deframer.locked, "unacked": self.tx.outstanding(),
                      "next_seq": self.tx.next_seq, "rx_expected": self.rx.expected,
                      "link_down": self.tx.link_down, "pending_bytes": self.pending_bytes(),
                      "peer_age_s": (None if self._peer_t is None
                                     else round(time.monotonic() - self._peer_t, 1)),
                      "rtt_frames": round(self.tx.rtt_frames, 1), "rto_frames": self.tx.rto_effective,
                      "tx": dict(self.tx.stats), "rx": dict(self.rx.stats),
                      "deframer": dict(self.deframer.stats), "crypto": self.crypto.status(),
                      "sizes": self.sizes.__dict__, "quality": self.quality(),
                      "modem": dict(self.modem_status)})
            return d
