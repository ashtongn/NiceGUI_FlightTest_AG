"""
secure_ground.py: the ground-station side of secure messages.

The radio forwards every CRC-valid sealed body it decodes as a SEALED_FRAME
datagram (proto.SealedFrame) and never holds keys. This module opens them on
the x86 with the process SecureContext (secure_frame.get_context(), built from
DPMG_KEY_FILE), keeps the per-band result the GUI shows in place of the
radio's "[sealed N B]" placeholder, and the verdict counters.

    g = GroundOpener()
    g.poll(host)                  # open anything new host.sealed holds
    g.last(host.serial, band)     # {"msg", "verdict", "t", "seq"} or None
    g.recent(n)                   # last n results across hosts
    g.status()                    # context + counters for the panel

Importable without NiceGUI (tests, the bench). Never raises on bad input.
"""
from __future__ import annotations

import threading
import time
from collections import deque

from dpmg.secure import api as sf


class GroundOpener:
    def __init__(self, ctx=None, keep: int = 200):
        self._ctx = ctx
        self.lock = threading.Lock()
        self._seen: dict = {}          # serial: last seq opened
        self._last: dict = {}          # (serial, band): result dict
        self._recent = deque(maxlen=keep)
        self.opened = 0
        self.dropped = 0               # seq gaps noticed

    @property
    def ctx(self):
        return self._ctx or sf.get_context(create=True, tx=False)

    def open_one(self, serial: str, frame) -> dict:
        ctx = self.ctx
        res = {"serial": serial, "band": getattr(frame, "band", ""), "seq": int(getattr(frame, "seq", 0)),
               "f_center_hz": float(getattr(frame, "f_center_hz", 0.0)), "t": time.time(),
               "t_radio": float(getattr(frame, "t_unix", 0.0)), "msg": "", "verdict": sf.NO_KEY,
               "detail": sf.last_error() or "no key loaded on this host", "header": None}
        if ctx is None:
            return res
        r = ctx.open(bytes(getattr(frame, "body", b"")))
        d = r.as_dict()
        res.update(verdict=r.verdict, detail=r.detail, key_id=r.key_id, header=d.get("header"))
        if r.ok:
            res["msg"] = sf._msg_of(r.plaintext)
        return res

    def poll(self, host) -> list:
        """Open every not-yet-seen sealed frame on `host` (a link_client host).
        Returns the new results (newest last)."""
        out = []
        serial = getattr(host, "serial", "?")
        frames = list(getattr(host, "sealed", []) or [])
        with self.lock:
            last = self._seen.get(serial, 0)
            for fr in frames:
                seq = int(getattr(fr, "seq", 0))
                if seq <= last:
                    continue
                if last and seq > last + 1:
                    self.dropped += seq - last - 1
                last = seq
                res = self.open_one(serial, fr)
                self.opened += 1
                self._last[(serial, res["band"])] = res
                self._recent.append(res)
                out.append(res)
            self._seen[serial] = last
        return out

    def last(self, serial: str, band: str):
        return self._last.get((serial, band))

    def recent(self, n: int = 20) -> list:
        return list(self._recent)[-n:]

    def status(self) -> dict:
        ctx = self.ctx
        d = {"flag": sf.secure_msg_enabled(), "active": ctx is not None, "opened": self.opened,
             "dropped": self.dropped, "err": "" if ctx is not None else sf.last_error()}
        if ctx is not None:
            st = ctx.status()
            d.update(profile=st["profile"], aead=st["aead"], suite=st["on_air_suite"],
                     overhead_bits=st["overhead_bits"], overhead_bits_long=st["overhead_bits_long"],
                     keys=st["keys"], counters=st["counters"], alarms=st["alarms"], crypto=st["crypto"])
        return d


_GROUND = None
_GROUND_LOCK = threading.Lock()


def ground() -> GroundOpener:
    """The GUI process's opener."""
    global _GROUND
    with _GROUND_LOCK:
        if _GROUND is None:
            _GROUND = GroundOpener()
        return _GROUND


def reset():
    global _GROUND
    with _GROUND_LOCK:
        _GROUND = None
