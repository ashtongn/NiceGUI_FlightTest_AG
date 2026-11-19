"""
dpmg.secure.nonce: sender-side nonce (IV) state that never repeats.

96-bit AES-GCM-SIV nonce (RFC 8452), deterministic counter + time-slot construction:

    fixed field (32)        sender16 (net_id || sender_id)  ||  generation (16)
    invocation field (64)   slot (32)  ||  counter (32)

  generation   persisted boot counter. Incremented and written to disk before
               the first frame of every process start (and bumped by
               KEYFILE_STEP whenever the key file changes). A radio restored
               from an image/volume backup therefore repeats a generation only
               if the backup is older than the state file (a known residual
               risk).
  slot         floor((t - midnight_utc(key day)) / slot_s). The sender persists
               a slot high-water mark per generation and refuses to transmit
               in any slot at or below it, in blocks of HWM_BLOCK slots so the
               disk sees one write per block. A GPS/clock regression therefore
               causes denial, never a repeated (key, IV).
  counter      per-slot, 0..1023 (10 bits on air). At 1024 the sender refuses
               (counter exhaustion) until the slot changes.

Receive side needs none of this: it rebuilds the IV from the header fields and
its own clock (secure_frame.SecureContext.open).
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import time
from dataclasses import dataclass

COUNTER_BITS = 10
COUNTER_MAX = (1 << COUNTER_BITS) - 1         # 1023 frames per slot
HWM_BLOCK = 8                                  # slots reserved per state write
KEYFILE_STEP = 256                             # generation bump when the key file changes
GEN_MAX = 0xFFFF
ENV_STATE_FILE = "DPMG_NONCE_STATE"


def default_state_path(key_path: str) -> str:
    return os.environ.get(ENV_STATE_FILE) or (os.path.splitext(key_path)[0] + ".nonce-state.json")


class NonceError(RuntimeError):
    """Refuse to transmit: state missing/unwritable, counter or generation
    exhausted, or a time regression below the reserved slots."""


class TimeSource:
    """now(): (unix seconds UTC, trust) with trust in
    {"trusted", "degraded", "untrusted", "system"}."""
    name = "system"

    def now(self) -> tuple:
        return time.time(), "system"


class FixedTime(TimeSource):
    """Tests and simulators: a settable clock."""
    name = "fixed"

    def __init__(self, t: float, trust: str = "trusted"):
        self.t, self.trust = float(t), trust

    def now(self) -> tuple:
        return self.t, self.trust

    def set(self, t: float, trust: str | None = None):
        self.t = float(t)
        if trust:
            self.trust = trust


class GpsdTime(TimeSource):
    """GPS time from gpsd (gps_antenna.query_gpsd / SimGpsAntenna.gnss) as an
    offset to the system clock, refreshed out of band by the service's 5 s
    reference loop so now() never blocks the seal path.

    trust: "trusted" with a 3D fix and a fresh refresh, "degraded" with a 2D
    fix or a stale (< HOLDOVER_S) refresh, "untrusted" otherwise (system clock
    stands in; secure messages still transmit unless Policy.require_trusted_time)."""
    name = "gpsd"
    HOLDOVER_S = 300.0

    def __init__(self, query=None):
        self.query = query                 # callable: gnss dict (time_utc, fix) or None
        self.offset = 0.0
        self.fix = None
        self.t_refresh = 0.0
        self.last = None

    def refresh(self, gnss: dict | None = None) -> dict:
        """Call from a worker thread. gnss = query_gpsd()-style dict."""
        if gnss is None and self.query is not None:
            try:
                gnss = self.query()
            except Exception:                                      # noqa: BLE001
                gnss = None
        self.last = gnss
        if gnss and gnss.get("time_utc"):
            try:
                ts = str(gnss["time_utc"]).replace("Z", "+00:00")
                t_gps = _dt.datetime.fromisoformat(ts).timestamp()
                self.offset = t_gps - time.time()
                self.fix = gnss.get("fix")
                self.t_refresh = time.monotonic()
            except ValueError:
                pass
        return {"fix": self.fix, "offset_s": round(self.offset, 3),
                "age_s": None if not self.t_refresh else round(time.monotonic() - self.t_refresh, 1)}

    def now(self) -> tuple:
        age = time.monotonic() - self.t_refresh if self.t_refresh else None
        if age is None or age > self.HOLDOVER_S or self.fix in (None, "none"):
            return time.time() + (self.offset if age is not None else 0.0), "untrusted"
        if self.fix == "3D" and age <= 10.0:
            return time.time() + self.offset, "trusted"
        return time.time() + self.offset, "degraded"


def utc_day(t: float) -> _dt.date:
    return _dt.datetime.fromtimestamp(t, _dt.timezone.utc).date()


def day_start(day: _dt.date) -> float:
    return _dt.datetime(day.year, day.month, day.day, tzinfo=_dt.timezone.utc).timestamp()


def slot_of(t: float, day: _dt.date, slot_s: float) -> int:
    """Slot index of time t relative to the key day's midnight (may be
    negative or > 86400/slot_s near the day boundary)."""
    return int((t - day_start(day)) // slot_s)


def build_iv(sender16: int, generation: int, slot: int, counter: int) -> bytes:
    if not (0 <= sender16 <= 0xFFFF and 0 <= generation <= GEN_MAX):
        raise ValueError("sender16/generation out of range")
    if not (0 <= counter <= COUNTER_MAX):
        raise ValueError("counter out of range")
    return (sender16.to_bytes(2, "big") + generation.to_bytes(2, "big")
            + (slot & 0xFFFFFFFF).to_bytes(4, "big") + counter.to_bytes(4, "big"))


@dataclass
class NonceState:
    path: str
    generation: int
    hwm: dict                  # str(generation): highest reserved slot
    key_sha256: str
    updated: str = ""

    def save(self) -> None:
        d = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(d, mode=0o700, exist_ok=True)
        tmp = self.path + ".tmp"
        doc = {"version": 1, "generation": self.generation, "hwm": self.hwm,
               "key_sha256": self.key_sha256,
               "updated": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")}
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
        try:
            dfd = os.open(d, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass

    @classmethod
    def load(cls, path: str) -> "NonceState | None":
        try:
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            raise NonceError(f"nonce state {path}: unreadable ({e}); refusing to transmit") from e
        try:
            return cls(path=path, generation=int(doc["generation"]), hwm=dict(doc.get("hwm") or {}),
                       key_sha256=str(doc.get("key_sha256", "")), updated=str(doc.get("updated", "")))
        except (KeyError, TypeError, ValueError) as e:
            raise NonceError(f"nonce state {path}: malformed ({e}); refusing to transmit") from e


class NonceSource:
    """One per sending process. open() bumps the persisted generation; next()
    hands out (iv_bytes, generation, slot, counter) and refuses on exhaustion."""

    def __init__(self, sender16: int, state_path: str, key_sha256: str, slot_s: float,
                 time_source: TimeSource | None = None, create: bool = True):
        self.sender16 = int(sender16)
        self.slot_s = float(slot_s)
        self.time = time_source or TimeSource()
        self.state_path = state_path
        st = NonceState.load(state_path)
        if st is None:
            if not create:
                raise NonceError(f"nonce state {state_path} missing; refusing to transmit")
            st = NonceState(path=state_path, generation=-1, hwm={}, key_sha256=key_sha256)
        step = 1
        if st.key_sha256 and key_sha256 and st.key_sha256 != key_sha256:
            step = KEYFILE_STEP                       # new key material: large jump
        gen = st.generation + step
        if gen > GEN_MAX:
            raise NonceError("boot generation exhausted (65535): a new key fill is required")
        st.generation = gen
        st.key_sha256 = key_sha256
        # keep only this generation's mark (older generations can never be reused)
        st.hwm = {str(gen): -1}
        try:
            st.save()                                  # before the first frame
        except OSError as e:
            raise NonceError(f"nonce state {state_path}: cannot write ({e}); refusing to transmit") from e
        self.state = st
        self.generation = gen
        self._slot = None            # current slot
        self._counter = 0
        self._reserved_to = -1       # highest slot reserved on disk for this generation
        self.day = None              # key day the slots are relative to
        self.frames = 0

    # helpers
    def current_slot(self) -> tuple:
        """(slot, day, trust) for now, relative to today's UTC midnight."""
        t, trust = self.time.now()
        day = utc_day(t)
        return slot_of(t, day, self.slot_s), day, trust

    def _reserve(self, slot: int) -> None:
        """Persist a high-water mark covering [slot, slot+HWM_BLOCK-1]."""
        self._reserved_to = slot + HWM_BLOCK - 1
        self.state.hwm[str(self.generation)] = self._reserved_to
        try:
            self.state.save()
        except OSError as e:
            raise NonceError(f"nonce state: cannot persist slot mark ({e}); refusing to transmit") from e

    def next(self, require_trusted: bool = False) -> tuple:
        """Reserve the next (slot, counter); returns (iv, generation, slot, counter, day).
        Raises NonceError rather than ever handing out a repeat."""
        slot, day, trust = self.current_slot()
        if require_trusted and trust not in ("trusted",):
            raise NonceError(f"time source is {trust}; policy requires trusted GPS time")
        if self.day is not None and day != self.day:
            if day < self.day:
                # the clock went back across midnight: yesterday's day key with
                # slots this generation may already have used: refuse
                raise NonceError(f"time regression: day {day} < {self.day}; refusing to transmit")
            # forward midnight rollover: slots restart relative to the new day
            # under a new day key, so the reservation restarts too (the (key, IV)
            # pair cannot repeat: the key changed and the generation never does).
            self._slot, self._reserved_to = None, -1
        self.day = day
        if self._slot is None or slot != self._slot:
            if self._slot is not None and slot < self._slot:
                raise NonceError(f"time regression: slot {slot} < current {self._slot}; refusing to transmit")
            if self._slot is None and slot <= self._reserved_to:
                raise NonceError("time regression below the reserved slot mark; refusing to transmit")
            self._slot, self._counter = slot, 0
            if slot > self._reserved_to:
                self._reserve(slot)
        if self._counter > COUNTER_MAX:
            raise NonceError(f"per-slot counter exhausted ({COUNTER_MAX + 1} frames in slot {slot}); "
                             f"wait for the next slot")
        ctr = self._counter
        self._counter += 1
        self.frames += 1
        return build_iv(self.sender16, self.generation, slot, ctr), self.generation, slot, ctr, day

    def status(self) -> dict:
        slot, day, trust = self.current_slot()
        return {"generation": self.generation, "slot": slot, "counter": self._counter,
                "reserved_to": self._reserved_to, "frames": self.frames,
                "time_trust": trust, "state_path": self.state_path}
