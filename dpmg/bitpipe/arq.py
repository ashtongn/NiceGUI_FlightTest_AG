"""
Selective repeat ARQ over the 16 bit link sequence.

TxWindow keeps the frames not yet acknowledged (at most `window`), decides
when to retransmit (a frame passed over by later acknowledged frames, or one
that has waited rto_frames transmitted frames with no acknowledgement; the
timeout stretches to the measured round trip when the path is slower than
the configured value), and counts tries so a dead link is reported rather
than silently spun on.
RxReorder holds frames that arrive out of order, hands them out in sequence
and supplies the ack and sack fields the TX side puts in every frame.

Time is counted in transmitted frames, not seconds: the stream is continuous,
so one frame slot is the natural tick and the same code paces itself to any
bit rate.
"""
from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .frame import SEQ_MOD, seq_diff, seq_lt


@dataclass
class TxEntry:
    seq: int
    stream: int
    body: bytes                 # the sealed payload, re-sent byte for byte
    tries: int = 0
    last_tx: int = -1           # frame tick of the last transmission
    t_first: float = 0.0        # monotonic time of the first transmission
    nack_hint: bool = False     # a later frame was acknowledged while this was pending


class TxWindow:
    def __init__(self, window: int = 16, rto_frames: int = 6, max_retries: int = 50, rto_max_frames: int = 64):
        self.window = int(window)
        self.rto = max(1, int(rto_frames))
        self.rto_max = max(self.rto, int(rto_max_frames))
        self.rtt_frames = 0.0           # smoothed round trip in frame slots, from the acks
        self.rtt_s = 0.0                # smoothed round trip in seconds (first try frames only)
        self.rtt_s_last = 0.0
        self.max_retries = int(max_retries)
        self.pending: "OrderedDict[int, TxEntry]" = OrderedDict()
        self.next_seq = 0
        self.tick = 0
        self.stats = {"sent": 0, "retx": 0, "acked": 0, "gave_up": 0}
        self.link_down = False

    def can_send_new(self) -> bool:
        return len(self.pending) < self.window

    def add(self, seq: int, stream: int, body: bytes) -> TxEntry:
        if seq != self.next_seq:
            raise ValueError(f"seq {seq} is not the next sequence {self.next_seq}")
        e = TxEntry(seq, stream, bytes(body))
        self.pending[seq] = e
        self.next_seq = (self.next_seq + 1) % SEQ_MOD
        return e

    def sent(self, e: TxEntry, retransmit: bool) -> None:
        if e.tries == 0:
            e.t_first = time.monotonic()
        e.tries += 1
        e.last_tx = self.tick
        e.nack_hint = False
        self.stats["retx" if retransmit else "sent"] += 1

    def advance(self) -> None:
        self.tick += 1

    def on_ack(self, ack: int, sack: int) -> None:
        """Peer expects `ack` next and has the frames flagged in `sack`."""
        done = []
        for seq, e in self.pending.items():
            if seq_lt(seq, ack):
                done.append(seq)
            else:
                d = seq_diff(seq, ack) - 1
                if 0 <= d < 16 and (sack >> d) & 1:
                    done.append(seq)
        for seq in done:
            e = self.pending.pop(seq, None)
            self.stats["acked"] += 1
            if e is not None and e.tries == 1 and e.last_tx >= 0:
                rtt = float(self.tick - e.last_tx)
                self.rtt_frames = rtt if self.rtt_frames == 0 else 0.75 * self.rtt_frames + 0.25 * rtt
                rs = time.monotonic() - e.t_first
                self.rtt_s_last = rs
                self.rtt_s = rs if self.rtt_s == 0 else 0.75 * self.rtt_s + 0.25 * rs
        self.link_down = False
        if done:
            newest = max(done, key=lambda s: seq_diff(s, ack))
            for seq, e in self.pending.items():
                if seq_lt(seq, newest):
                    e.nack_hint = True

    @property
    def rto_effective(self) -> int:
        """The configured timeout, stretched to 1.5 times the measured round
        trip plus two frames when the link is slower than that, capped."""
        return int(min(self.rto_max, max(self.rto, 1.5 * self.rtt_frames + 2)))

    def pick_retransmit(self) -> Optional[TxEntry]:
        """Oldest frame due for retransmission, or None."""
        rto = self.rto_effective
        for e in self.pending.values():
            if e.tries == 0:
                continue
            age = self.tick - e.last_tx
            if (e.nack_hint and age >= 2) or age >= rto:
                if e.tries >= self.max_retries and not self.link_down:
                    self.link_down = True
                    self.stats["gave_up"] += 1
                return e
        return None

    def outstanding(self) -> int:
        return len(self.pending)


class RxReorder:
    def __init__(self, window: int = 16):
        self.window = int(window)
        self.expected = 0
        self.held: Dict[int, Tuple[int, bytes]] = {}
        self.stats = {"delivered": 0, "dups": 0, "out_of_window": 0, "held_max": 0}

    def offer(self, seq: int, stream: int, body: bytes) -> List[Tuple[int, int, bytes]]:
        """Accept one data frame; return the (seq, stream, body) now deliverable in order."""
        d = seq_diff(seq, self.expected)
        if d < 0:
            self.stats["dups"] += 1
            return []
        if d >= self.window:
            self.stats["out_of_window"] += 1
            return []
        if seq in self.held:
            self.stats["dups"] += 1
            return []
        self.held[seq] = (stream, bytes(body))
        self.stats["held_max"] = max(self.stats["held_max"], len(self.held))
        out = []
        while self.expected in self.held:
            s, b = self.held.pop(self.expected)
            out.append((self.expected, s, b))
            self.expected = (self.expected + 1) % SEQ_MOD
            self.stats["delivered"] += 1
        return out

    def ack_fields(self) -> Tuple[int, int]:
        sack = 0
        for i in range(16):
            if ((self.expected + 1 + i) % SEQ_MOD) in self.held:
                sack |= 1 << i
        return self.expected, sack
