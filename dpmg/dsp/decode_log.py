"""
decode_log.py: live decode-event log for the channelizer.

Thread-safe ring buffer of decode events. The worker (background thread) appends
events on a positive demod; the UI timer reads recent events to display them. Kept
in memory (on-screen panel requirement); easy to also tee to disk later.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class DecodeEvent:
    ts: float                       # epoch seconds
    ch_idx: int
    valid: bool
    message: str = ""
    bits: str = ""
    snr_db: float = 0.0

    @property
    def ts_str(self) -> str:
        lt = time.localtime(self.ts)
        return time.strftime("%H:%M:%S", lt) + f".{int((self.ts % 1)*1000):03d}"


class DecodeLog:
    def __init__(self, maxlen: int = 500):
        self._events: deque = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._counts: dict[int, int] = {}     # ch_idx: count of valid decodes

    def add(self, ev: DecodeEvent) -> None:
        with self._lock:
            self._events.append(ev)
            if ev.valid:
                self._counts[ev.ch_idx] = self._counts.get(ev.ch_idx, 0) + 1

    def recent(self, n: int = 50) -> list[DecodeEvent]:
        with self._lock:
            return list(self._events)[-n:][::-1]   # newest first

    def count_for(self, ch_idx: int) -> int:
        with self._lock:
            return self._counts.get(ch_idx, 0)

    def clear(self) -> None:
        with self._lock:
            self._events.clear()
            self._counts.clear()


# one shared log per device key
LOGS: dict[str, DecodeLog] = {}


def get_log(key: str) -> DecodeLog:
    if key not in LOGS:
        LOGS[key] = DecodeLog()
    return LOGS[key]


if __name__ == "__main__":
    log = get_log("x40")
    log.add(DecodeEvent(ts=time.time(), ch_idx=0, valid=True, message="HELLO",
                        bits="1011001000111011", snr_db=18.2))
    log.add(DecodeEvent(ts=time.time(), ch_idx=2, valid=True, message="ACK-7",
                        bits="0101", snr_db=15.0))
    for ev in log.recent():
        print(f"[{ev.ts_str}] ch{ev.ch_idx} valid={ev.valid} '{ev.message}' "
              f"snr={ev.snr_db}dB")
    print("ch0 valid count:", log.count_for(0))
    print("decode_log self-test OK")
