"""
Two bitpipe containers back to back through a software channel, no radio.

    python3 -m dpmg.bitpipe.loopback                 clear link, clean channel
    python3 -m dpmg.bitpipe.loopback --secure --ber 0.01 --drop 0.02
    python3 -m dpmg.bitpipe.loopback --fec rs --payload 100 --file-kb 32

Container A listens on its bit face, container B on its own; a relay plays
the two modems: it holds the clock, pulls one frame at a time from each
container at the configured bit rate (REC_PULL), runs the frame through the
channel (random bit flips at a bit error rate, whole frames dropped with a
probability, so the ARQ has work to do) and hands it to the other container
as received bits. The containers keep no clock: the modem is the only clock
in the chain, so the stream rate is the modem's symbol rate and nothing else. The application faces are used the way a user would: clients on the
chat and file ports of both containers, text lines one way and the other at
the same time, then a file each way. The run passes when every line and both
files arrive intact. --transport unix runs the same thing over Unix sockets.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np

from . import transport as tp
from .config import BitpipeConfig
from .container import BitpipeContainer
from .crypto import NullCrypto, loopback_crypto_pair
from . import streams as st


@dataclass
class Channel:
    """Impairments on one direction: bit flips at a rate, whole frames (or
    a slice out of one, to force a re-sync) dropped with a probability."""
    ber: float = 0.0
    drop_prob: float = 0.0
    seed: int = 1
    rng: np.random.Generator = field(init=False)
    stats: dict = field(default_factory=lambda: {"frames": 0, "bits": 0, "flipped": 0, "dropped": 0, "cut": 0})

    def __post_init__(self):
        self.rng = np.random.default_rng(self.seed)

    def apply(self, bits: np.ndarray) -> Optional[np.ndarray]:
        """Bits of one frame in; impaired bits out, None when the frame is lost."""
        self.stats["frames"] += 1
        if self.drop_prob > 0 and self.rng.random() < self.drop_prob:
            if self.rng.random() < 0.5:
                self.stats["dropped"] += 1
                return None
            cut = int(self.rng.integers(1, max(2, len(bits) // 4)))      # a slip: part of the frame vanishes
            self.stats["cut"] += 1
            bits = bits[cut:]
        self.stats["bits"] += len(bits)
        if self.ber > 0:
            flips = (self.rng.random(len(bits)) < self.ber).astype(np.uint8)
            self.stats["flipped"] += int(flips.sum())
            bits = bits ^ flips
        return bits


class Relay:
    """The modem stand in: pulls frames from both containers on a clock and
    cross couples them through two channels."""

    def __init__(self, a: tp.Endpoint, b: tp.Endpoint, ch_ab: Channel, ch_ba: Channel, bit_rate: float = 48000.0):
        self.a, self.b, self.ch_ab, self.ch_ba = a, b, ch_ab, ch_ba
        self.bit_rate = float(bit_rate)
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self.sa: Optional[socket.socket] = None
        self.sb: Optional[socket.socket] = None
        self.locks = {}
        self.hellos = {}
        self.stats = {"pulls": 0, "late": 0, "frames_fwd": 0}

    def start(self) -> None:
        self.sa = self._connect(self.a)
        self.sb = self._connect(self.b)
        self.locks = {self.sa: threading.Lock(), self.sb: threading.Lock()}
        for src, dst, ch, nm in ((self.sa, self.sb, self.ch_ab, "a>b"), (self.sb, self.sa, self.ch_ba, "b>a")):
            t = threading.Thread(target=self._pump, args=(src, dst, ch, nm), daemon=True, name=f"relay-{nm}")
            t.start()
            self._threads.append(t)

    def _connect(self, ep: tp.Endpoint) -> socket.socket:
        deadline = time.monotonic() + 10.0
        while True:
            try:
                return tp.tune(ep.connect(timeout=2.0))
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.1)

    def _send(self, sock: socket.socket, data: bytes) -> None:
        with self.locks[sock]:
            sock.sendall(data)

    def _pump(self, src: socket.socket, dst: socket.socket, ch: Channel, nm: str) -> None:
        reader = tp.RecordReader()
        pending: List[np.ndarray] = []
        block = None
        t_next = time.monotonic()
        self._send(src, tp.pack_json(tp.REC_HELLO, {"modem": f"relay-{nm}", "format": "packed",
                                                     "bit_rate": self.bit_rate}))
        while not self._stop.is_set():
            # one pull per frame period: the relay is the clock
            self._send(src, tp.pack_record(tp.REC_PULL, tp.BITS_HDR.pack(1)))
            self.stats["pulls"] += 1
            got = None
            while got is None and not self._stop.is_set():
                try:
                    data = src.recv(8192)
                except socket.timeout:
                    continue
                except OSError:
                    return
                if not data:
                    return
                for rtype, payload in reader.feed(data):
                    if rtype == tp.REC_BITS:
                        got = tp.unpack_bits(payload)
                    elif rtype == tp.REC_HELLO:
                        self.hellos[nm] = tp.parse_json(payload) or {}
                        block = self.hellos[nm].get("block_bits")
            if got is None:
                return
            out = ch.apply(got)
            if out is not None and len(out):
                try:
                    self._send(dst, tp.pack_record(tp.REC_BITS, tp.pack_bits(out)))
                except OSError:
                    return
                self.stats["frames_fwd"] += 1
            if self.bit_rate > 0:
                t_next += (block or len(got)) / self.bit_rate
                dt = t_next - time.monotonic()
                if dt > 0:
                    time.sleep(dt)
                elif dt < -0.5:
                    self.stats["late"] += 1
                    t_next = time.monotonic()

    def stop(self) -> None:
        self._stop.set()
        for s in (self.sa, self.sb):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
        for t in self._threads:
            t.join(timeout=2.0)


class _AppClient:
    """A TCP client on one application port that records everything it reads."""

    def __init__(self, ep: tp.Endpoint):
        self.sock = ep.connect(timeout=2.0)
        self.sock.settimeout(0.2)
        self.rx = bytearray()
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self.t = threading.Thread(target=self._reader, daemon=True)
        self.t.start()

    def _reader(self):
        while not self._stop.is_set():
            try:
                d = self.sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not d:
                break
            with self.lock:
                self.rx.extend(d)

    def send(self, data: bytes) -> None:
        self.sock.sendall(data)

    def received(self) -> bytes:
        with self.lock:
            return bytes(self.rx)

    def close(self):
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass


def make_pair(cfg_a: BitpipeConfig, cfg_b: BitpipeConfig, secure: bool = False, freshness_slots: int = 300):
    """Two containers with matching settings; secure=True gives them throw
    away keys through loopback_crypto_pair."""
    if secure:
        ca, cb = loopback_crypto_pair(freshness_slots=freshness_slots)
        cfg_a.crypto = cfg_b.crypto = "secure"
    else:
        ca, cb = NullCrypto(), NullCrypto()
        cfg_a.crypto = cfg_b.crypto = "none"
    return BitpipeContainer(cfg_a, ca), BitpipeContainer(cfg_b, cb)


def default_configs(base_port: int = 47900, transport: str = "tcp", **over) -> tuple:
    """Two configs that do not collide: A on base_port-2 (ctl), -1 (bit), base_port+stream
    (app); B ten ports up. With transport unix the names pick the socket files."""
    a = BitpipeConfig(name="A", ctl_port=base_port - 2, bit_port=base_port - 1, app_base_port=base_port,
                      transport=transport, **over)
    b = BitpipeConfig(name="B", ctl_port=base_port + 8, bit_port=base_port + 9, app_base_port=base_port + 10,
                      transport=transport, **over)
    return a, b


def _wait(pred, timeout: float, step: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(step)
    return False


def run_loopback(cfg_a: BitpipeConfig, cfg_b: BitpipeConfig, secure: bool = True, ber: float = 0.0,
                 drop: float = 0.0, lines: int = 20, file_kb: int = 8, timeout: float = 120.0,
                 seed: int = 1, verbose: bool = True, bit_rate: float = 48000.0) -> dict:
    """The acceptance run: text both ways, then a file both ways. Returns a
    report dict with ok=True when everything arrived intact. bit_rate is the
    relay's clock (the modem's rate), not a bitpipe setting."""
    A, B = make_pair(cfg_a, cfg_b, secure=secure)
    A.start()
    B.start()
    relay = Relay(A.bit_endpoint(), B.bit_endpoint(), Channel(ber, drop, seed=seed), Channel(ber, drop, seed=seed + 1),
                  bit_rate=bit_rate)
    rng = np.random.default_rng(seed)
    report = {"ok": False, "secure": secure, "ber": ber, "drop": drop}
    t0 = time.monotonic()
    try:
        relay.start()
        if not _wait(lambda: A.connected() and B.connected(), 10.0):
            report["error"] = "bit faces did not connect"
            return report
        ca = _AppClient(A.app_endpoint(st.STREAM_CHAT))
        cb = _AppClient(B.app_endpoint(st.STREAM_CHAT))
        fa = _AppClient(A.app_endpoint(st.STREAM_FILE))
        fb = _AppClient(B.app_endpoint(st.STREAM_FILE))
        time.sleep(0.2)
        # text both ways at the same time
        la = [f"A>B line {i} {rng.integers(0, 1 << 30)}\n".encode() for i in range(lines)]
        lb = [f"B>A line {i} {rng.integers(0, 1 << 30)}\n".encode() for i in range(lines)]
        for x, y in zip(la, lb):
            ca.send(x)
            cb.send(y)
        want_a, want_b = b"".join(la), b"".join(lb)
        ok_text = _wait(lambda: cb.received() == want_a and ca.received() == want_b, timeout / 2)
        report["text_ok"] = ok_text
        report["text_time_s"] = round(time.monotonic() - t0, 2)
        if verbose:
            print(f"text both ways: {'PASS' if ok_text else 'FAIL'} "
                  f"({len(cb.received())}/{len(want_a)} B at B, {len(ca.received())}/{len(want_b)} B at A, "
                  f"{report['text_time_s']} s)")
        # a file each way
        data_a = rng.bytes(file_kb * 1024)
        data_b = rng.bytes(file_kb * 1024)
        t1 = time.monotonic()
        fa.send(st.file_record("a_to_b.bin", data_a))
        fb.send(st.file_record("b_to_a.bin", data_b))
        # parse whatever has arrived so far from scratch on each poll

        def done():
            ra, rb = st.FileReceiver(), st.FileReceiver()
            got_b = rb.feed(fb.received())
            got_a = ra.feed(fa.received())
            return bool(got_b) and bool(got_a)
        ok_wait = _wait(done, timeout)
        rb_final, ra_final = st.FileReceiver(), st.FileReceiver()
        got_b = rb_final.feed(fb.received())
        got_a = ra_final.feed(fa.received())
        ok_file = (ok_wait and got_b and got_a and got_b[0].ok and got_a[0].ok
                   and got_b[0].data == data_a and got_a[0].data == data_b)
        report["file_ok"] = bool(ok_file)
        report["file_time_s"] = round(time.monotonic() - t1, 2)
        report["file_bytes"] = file_kb * 1024
        if verbose:
            print(f"file both ways: {'PASS' if ok_file else 'FAIL'} ({file_kb} kB each, "
                  f"{report['file_time_s']} s; at B {len(fb.received())} B, at A {len(fa.received())} B)")
        for c in (ca, cb, fa, fb):
            c.close()
        report["ok"] = bool(ok_text and ok_file)
    finally:
        report["status_a"] = A.status()
        report["status_b"] = B.status()
        report["channel_ab"] = dict(relay.ch_ab.stats)
        report["channel_ba"] = dict(relay.ch_ba.stats)
        report["relay"] = dict(relay.stats, hellos=relay.hellos)
        report["elapsed_s"] = round(time.monotonic() - t0, 2)
        relay.stop()
        A.stop()
        B.stop()
    return report


def _summary(rep: dict) -> str:
    def one(s):
        return (f"tx {s['frames_tx']} (data {s['data_tx']}, idle {s['idle_tx']}) rx {s['frames_rx']} "
                f"retx {s['tx']['retx']} dups {s['rx']['dups']} open_failed {s['open_failed']} "
                f"locked {s['locked']} locks {s['deframer']['locks']} bad {s['deframer']['frames_bad']}")
    return (f"A: {one(rep['status_a'])}\nB: {one(rep['status_b'])}\n"
            f"channel a>b {rep['channel_ab']}  b>a {rep['channel_ba']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="bitpipe loopback, two containers back to back, no radio")
    ap.add_argument("--secure", action="store_true", help="AES-256-GCM-SIV on (throw away keys)")
    ap.add_argument("--fec", default="conv", help="conv | rs | rs+conv | ldpc | tpc | none")
    ap.add_argument("--rate", default="1/2", help="conv rate")
    ap.add_argument("--code", default=None, help="ldpc or tpc code name")
    ap.add_argument("--payload", type=int, default=64)
    ap.add_argument("--depth", type=int, default=8, help="interleave depth")
    ap.add_argument("--bit-rate", type=float, default=48000.0, help="the relay's (modem's) clock, bit/s")
    ap.add_argument("--transport", default="tcp", help="tcp (127.0.0.1) or unix (socket files)")
    ap.add_argument("--ber", type=float, default=0.0)
    ap.add_argument("--drop", type=float, default=0.0, help="probability a frame is lost or cut")
    ap.add_argument("--lines", type=int, default=20)
    ap.add_argument("--file-kb", type=int, default=8)
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--base-port", type=int, default=47900)
    ap.add_argument("--seed", type=int, default=3)
    ap.add_argument("--json", action="store_true", help="print the full report")
    a = ap.parse_args(argv)
    fec = None if a.fec == "none" else {"type": a.fec}
    if fec and a.fec in ("conv", "rs+conv"):
        fec["rate"] = a.rate
    if fec and a.code:
        fec["code"] = a.code
    cfg_a, cfg_b = default_configs(a.base_port, transport=a.transport, payload_bytes=a.payload, fec=fec,
                                   interleave_depth=a.depth, crypto="secure" if a.secure else "none")
    print(f"bitpipe loopback: fec {fec} payload {a.payload} B secure {a.secure} ber {a.ber} drop {a.drop} "
          f"relay clock {a.bit_rate:g} bit/s {a.transport}; {cfg_a.sizes.block_bits} bits a frame on the stream, "
          f"{cfg_a.sizes.data_capacity} data bytes a frame")
    rep = run_loopback(cfg_a, cfg_b, secure=a.secure, ber=a.ber, drop=a.drop, lines=a.lines,
                       file_kb=a.file_kb, timeout=a.timeout, seed=a.seed, bit_rate=a.bit_rate)
    print(_summary(rep))
    if a.json:
        print(json.dumps(rep, indent=1, default=str))
    print("LOOPBACK:", "PASS" if rep["ok"] else "FAIL")
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
