"""
burst_history.py: burst-event history for the energy-gated channelizer.
Records the full RF timeline: DETECTED (slot goes active), DECODED (frame validated), LOST (slot
goes silent). Thread-safe, bounded, CSV-exportable. Independent of the legacy decode_log path.

One global registry keyed by tab key (e.g. 'g40') so the worker writes and the UI reads the same
history.
"""
from __future__ import annotations
import threading, time, io, csv
from dataclasses import dataclass, field
from collections import deque
from typing import Optional


@dataclass
class BurstEvent:
    ts: float                      # unix time
    kind: str                      # 'DETECTED' | 'DECODED' | 'LOST'
    offset_khz: float              # freq offset from center (kHz)
    freq_hz: float                 # absolute frequency (Hz)
    power_db: float = 0.0
    message: str = ""              # for DECODED
    duration_s: float = 0.0        # for LOST (how long it was active)
    demod: str = ""                # modulation type (BPSK/FSK/...)


class BurstHistory:
    def __init__(self, maxlen: int = 5000):
        self._events: deque[BurstEvent] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def add(self, ev: BurstEvent) -> None:
        with self._lock:
            self._events.append(ev)

    def recent(self, n: int = 100) -> list[BurstEvent]:
        with self._lock:
            return list(self._events)[-n:]

    def all(self) -> list[BurstEvent]:
        with self._lock:
            return list(self._events)

    def clear(self) -> None:
        with self._lock:
            self._events.clear()

    def to_csv(self) -> str:
        with self._lock:
            evs = list(self._events)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["timestamp_iso", "unix_ts", "kind", "offset_khz", "freq_mhz",
                    "power_db", "message", "duration_s", "demod"])
        for e in evs:
            iso = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e.ts)) + \
                  (".%03d" % int((e.ts % 1) * 1000))
            w.writerow([iso, "%.3f" % e.ts, e.kind, "%.1f" % e.offset_khz,
                        "%.4f" % (e.freq_hz / 1e6), "%.1f" % e.power_db,
                        e.message, "%.2f" % e.duration_s, e.demod])
        return buf.getvalue()

    def counts(self) -> dict:
        with self._lock:
            evs = list(self._events)
        c = {"DETECTED": 0, "DECODED": 0, "LOST": 0}
        for e in evs:
            c[e.kind] = c.get(e.kind, 0) + 1
        return c


_HISTORIES: dict[str, BurstHistory] = {}
_REG_LOCK = threading.Lock()

def get_history(key: str) -> BurstHistory:
    with _REG_LOCK:
        h = _HISTORIES.get(key)
        if h is None:
            h = BurstHistory()
            _HISTORIES[key] = h
        return h


if __name__ == "__main__":
    h = get_history("test")
    now = time.time()
    h.add(BurstEvent(ts=now, kind="DETECTED", offset_khz=-9000, freq_hz=416.025e6, power_db=-30.5, demod="BPSK"))
    h.add(BurstEvent(ts=now+0.1, kind="DECODED", offset_khz=-9000, freq_hz=416.025e6, power_db=-30.5, message="ALPHA", demod="BPSK"))
    h.add(BurstEvent(ts=now+0.6, kind="LOST", offset_khz=-9000, freq_hz=416.025e6, duration_s=0.6, demod="BPSK"))
    print("counts:", h.counts())
    print(h.to_csv())
