"""Which radios are recording right now, as seen by this GUI process.

A record (record_iq) is receive only, so nothing on the radio card or the
spectrum changes while it runs. The mission runner and the record panel mark
the start and the end of each capture here, and the operator console shows a
REC badge and a line under the spectrum from it. One process holds it; the
radio itself is not asked.

    rec_state.start("B200MINI-3000001", "demo1", 5.0, "mission")
    rec_state.active("B200MINI-3000001")   # {"label", "t0", "seconds", "src"} or None
    rec_state.stop("B200MINI-3000001")
"""
from __future__ import annotations

import threading
import time

# a capture that never reported its end is dropped this long after it should
# have finished, so a lost ack cannot leave the badge on forever
GRACE_S = 30.0

_lock = threading.Lock()
_active: dict = {}


def start(serial, label, seconds, src="") -> None:
    with _lock:
        _active[str(serial)] = {"label": str(label or ""), "t0": time.monotonic(),
                                "seconds": float(seconds or 0.0), "src": str(src or "")}


def stop(serial) -> None:
    with _lock:
        _active.pop(str(serial), None)


def active(serial):
    """The capture running on this radio, or None."""
    with _lock:
        r = _active.get(str(serial))
        if r is None:
            return None
        if time.monotonic() - r["t0"] > r["seconds"] + GRACE_S:
            _active.pop(str(serial), None)
            return None
        return dict(r)


def text(serial) -> str:
    """Short text for the console, e.g. "REC demo1 3 s", or "" when idle."""
    r = active(serial)
    if r is None:
        return ""
    el = time.monotonic() - r["t0"]
    lab = f" {r['label']}" if r["label"] else ""
    return f"REC{lab} {el:.0f} s"
