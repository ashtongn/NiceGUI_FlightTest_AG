"""device_watch.py: plug-in detection for the payload's connectable devices.

Two watchers the service runs while the front end is open:

* ``AttenWatch``: with ``atten.transport: auto`` the Mini-Circuits RCDAT USB
  attenuator is looked for every 2 s on hidraw (mc_atten.find_hidraw).  When
  exactly one is found it is opened and read (model, serial, attenuation) but
  never written: an attach must not change what the RX chain sees.  The open
  unit is handed to the service (and through it to the payload guard) by the
  ``publish`` callback; when it goes away (node gone, or an I/O error on a read)
  it is published as None and closed, so with a payload section the guard
  refuses keys again until it is back.
* ``AdcProbe``: a read of the ADS1x15 config register at the ``tr_readback`` bus
  and address, every 5 s, for information only.  It never enables the
  tr_readback interlock (that stays with ``tr_readback.enabled``).

``DeviceWatcher`` runs both from one daemon thread.  Nothing here touches a
GPIO line, and nothing runs on the service's event loop.

Locking: the attenuator object is shared by the service loop (atten_set,
payload_status), the guard (keying, under its lock) and the TX path (through the
guard).  ``lock`` is that same lock (the guard's when there is one, else the
service's attenuator lock).  The watcher opens and reads a new unit before it
is shared, takes the lock only to publish or unpublish it, and skips its own
liveness read when the lock is busy (a key in progress) instead of waiting.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable, List, Optional

from dpmg.payload import mc_atten as _mc

log = logging.getLogger("device_watch")

ATTEN_SCAN_S = 2.0
ADC_PROBE_S = 5.0

# attenuator states
AT_OFF = "off"                       # transport none (nothing to look for)
AT_NONE = "not connected"
AT_CONNECTED = "connected"
AT_LOST = "lost"
AT_SEVERAL = "several attenuators found, set atten.path"
AT_ERROR = "error"                   # shown as "error: <reason>"

# ADC states
ADC_PRESENT = "present"
ADC_ABSENT = "absent"
ADC_NO_SMBUS = "smbus2 missing"
ADC_NO_BUS = "bus missing"
ADC_OFF = "off"                      # front end not open, nothing probed
ADC_SIM = "sim"                      # mock front end: no I2C hardware

ADS1X15_CONFIG_REG = 0x01


def _short(e, n: int = 60) -> str:
    s = str(e).strip() or type(e).__name__
    return s[:n]


class AttenWatch:
    """Attach / detach the USB attenuator as it is plugged in and out.

    ``publish(atten_or_None)`` is called with ``lock`` held.  ``find`` returns
    the candidate hidraw nodes (mc_atten.find_hidraw); ``open_fn(path)`` returns
    an opened MiniCircuitsAtten.  Both are replaceable for the tests."""

    def __init__(self, cfg, publish: Callable[[Optional[object]], None],
                 lock=None, find: Callable[[], List[str]] = None,
                 open_fn: Callable[[str], object] = None,
                 clock: Callable[[], float] = time.time, logger=None,
                 on_read: Callable[[float], None] = None):
        self.cfg = cfg
        self.on_read = on_read               # each good reading, for the payload guard
        self.path_cfg = str(getattr(cfg, "path", "auto") or "auto")
        self.max_db = float(getattr(cfg, "max_db", 120.0))
        self.step_db = float(getattr(cfg, "step_db", 0.05))
        self._publish = publish
        self.lock = lock if lock is not None else threading.RLock()
        self._find = find or _mc.find_hidraw
        self._open = open_fn or self._open_hidraw
        self._clock = clock
        self._log = logger or log
        self.atten = None                    # the unit this watcher opened, or None
        self.state = AT_NONE
        self.path: Optional[str] = None
        self.model: Optional[str] = None
        self.serial: Optional[str] = None
        self.db: Optional[float] = None
        self.since: Optional[float] = None   # wall time of the last state change
        self.err: Optional[str] = None
        self._err_logged: Optional[str] = None
        self._read_fail = 0
        self.attaches = 0
        self.detaches = 0
        self.scans = 0
        self._scan_lock = threading.Lock()   # one scan at a time (thread + a direct call)

    # helpers
    def _open_hidraw(self, path: str):
        return _mc.MiniCircuitsAtten(_mc.HidrawTransport(path), self.max_db, self.step_db)

    def _candidates(self) -> List[str]:
        try:
            found = list(self._find())
        except Exception as e:                   # sysfs unreadable: same as nothing found
            self._log.debug("find_hidraw: %s", e)
            found = []
        if self.path_cfg not in ("", "auto"):
            found = [p for p in found if p == self.path_cfg]
        return found

    def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self.since = self._clock()

    def status(self) -> dict:
        st = self.state if self.state != AT_ERROR else f"error: {self.err}"
        return {"state": st, "model": self.model, "serial": self.serial, "path": self.path,
                "db": self.db, "since": (None if self.since is None else int(self.since))}

    # one scan (the thread calls this every ATTEN_SCAN_S; the tests call it directly)
    def scan(self) -> str:
        with self._scan_lock:
            return self._scan()

    def _scan(self) -> str:
        self.scans += 1
        found = self._candidates()
        if self.atten is not None:
            if self.path not in found:
                self._lose("node gone")
            else:
                self._check_alive()
            return self.state
        if not found:
            if self.state not in (AT_LOST, AT_NONE):
                self._set_state(AT_NONE)
            return self.state
        if len(found) > 1:
            if self.state != AT_SEVERAL:
                self._log.warning("attenuator: %d units found (%s); set atten.path to pick one",
                                  len(found), ", ".join(found))
            self._set_state(AT_SEVERAL)
            return self.state
        self._attach(found[0])
        return self.state

    def _attach(self, path: str) -> None:
        at = None
        try:
            at = self._open(path)
            model, serial, db = at.model(), at.serial(), float(at.get_db())
        except Exception as e:
            if at is not None:
                try: at.close()
                except Exception: pass
            reason = _short(e)
            if reason != self._err_logged:
                self._log.warning("attenuator at %s found but not opened: %s", path, reason)
                self._err_logged = reason
            self.err = reason
            self.path = path
            self._set_state(AT_ERROR)
            return
        # read only: no set_db and no startup value on attach
        with self.lock:
            self._publish(at)
            self.atten = at
        self.path, self.model, self.serial, self.db = path, model, serial, round(db, 2)
        self.err, self._err_logged, self._read_fail = None, None, 0
        self.attaches += 1
        self._set_state(AT_CONNECTED)
        self._log.info("attenuator connected: %s %s SN %s at %.2f dB", path, model, serial, db)

    def _check_alive(self) -> None:
        at = self.atten
        if not self.lock.acquire(blocking=False):
            return                               # a key or a set is using it: next scan
        try:
            if at is not self.atten:
                return
            try:
                self.db = round(float(at.get_db()), 2)
                self._read_fail = 0
                if self.on_read is not None:
                    try:
                        self.on_read(self.db)
                    except Exception as e:   # a hook problem is not a lost attenuator
                        self._log.warning("attenuator on_read: %s", _short(e))
                return
            except OSError as e:                 # unplugged: ENODEV / EIO on the fd
                why = f"I/O error: {_short(e)}"
            except Exception as e:
                # a timeout or a garbled reply: give it one more scan before calling it lost
                self._read_fail += 1
                if self._read_fail < 2:
                    return
                why = f"no reply: {_short(e)}"
        finally:
            self.lock.release()
        self._lose(why)

    def _lose(self, why: str) -> None:
        at = self.atten
        with self.lock:
            self._publish(None)
            self.atten = None
        if at is not None:
            try: at.close()
            except Exception: pass
        self.detaches += 1
        self.db = None
        self._read_fail = 0
        self._set_state(AT_LOST)
        self._log.warning("attenuator lost (%s): %s", self.path, why)

    def close(self) -> None:
        """Unpublish and close the unit (front end stop / reload)."""
        with self._scan_lock:
            self._close()

    def _close(self) -> None:
        if self.atten is not None:
            at = self.atten
            with self.lock:
                self._publish(None)
                self.atten = None
            try: at.close()
            except Exception: pass


def _import_smbus():
    try:
        from smbus2 import SMBus
        return SMBus
    except ImportError:
        try:
            from smbus import SMBus
            return SMBus
        except ImportError:
            return None


class AdcProbe:
    """Is the switch readback ADC on the bus?  One read of the ADS1x15 config
    register, nothing written.  ``smbus_cls`` (an SMBus-like class taking the bus
    number) and ``exists`` are for the tests; by default smbus2, else smbus."""

    def __init__(self, bus: str = "/dev/i2c-1", address: int = 0x48, smbus_cls=None,
                 exists: Callable[[str], bool] = os.path.exists,
                 clock: Callable[[], float] = time.time):
        self.bus = str(bus)
        self.address = int(address)
        self._smbus_cls = smbus_cls
        self._exists = exists
        self._clock = clock
        self.state = ADC_OFF
        self.since: Optional[float] = None
        self.probes = 0

    def _set(self, st: str) -> str:
        if st != self.state:
            self.state = st
            self.since = self._clock()
        return st

    def probe(self) -> str:
        self.probes += 1
        if not self._exists(self.bus):
            return self._set(ADC_NO_BUS)
        cls = self._smbus_cls or _import_smbus()
        if cls is None:
            return self._set(ADC_NO_SMBUS)
        n = self.bus
        if n.startswith("/dev/i2c-"):
            try:
                n = int(n.rsplit("-", 1)[-1])
            except ValueError:
                pass
        try:
            b = cls(n)
        except FileNotFoundError:
            return self._set(ADC_NO_BUS)
        except Exception as e:
            return self._set(f"error: {_short(e, 40)}")
        try:
            b.read_i2c_block_data(self.address, ADS1X15_CONFIG_REG, 2)
            return self._set(ADC_PRESENT)
        except OSError:                          # no ACK at that address
            return self._set(ADC_ABSENT)
        except Exception as e:
            return self._set(f"error: {_short(e, 40)}")
        finally:
            try: b.close()
            except Exception: pass

    def off(self, state: str = ADC_OFF) -> None:
        self._set(state)

    def status(self) -> dict:
        return {"state": self.state, "bus": self.bus, "address": hex(self.address)}


class DeviceWatcher:
    """One daemon thread: AttenWatch.scan every ``atten_s`` (when given) and
    AdcProbe.probe every ``adc_s`` while ``adc_allowed()`` is True (front end
    open, nothing keyed, no TX run).  ``step(now)`` does one pass, for tests."""

    def __init__(self, atten: Optional[AttenWatch] = None, adc: Optional[AdcProbe] = None,
                 adc_allowed: Callable[[], bool] = lambda: True,
                 atten_s: float = ATTEN_SCAN_S, adc_s: float = ADC_PROBE_S,
                 atten_delay_s: float = 0.0, clock: Callable[[], float] = time.monotonic):
        self.atten = atten
        self.adc = adc
        self._adc_allowed = adc_allowed
        self.atten_s, self.adc_s = float(atten_s), float(adc_s)
        self._clock = clock
        self._atten_delay = float(atten_delay_s)   # the caller already scanned once
        self._next_at = 0.0
        self._next_adc = 0.0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def step(self, now: Optional[float] = None) -> None:
        now = self._clock() if now is None else now
        if self.atten is not None and now >= self._next_at:
            self._next_at = now + self.atten_s
            try:
                self.atten.scan()
            except Exception:
                log.exception("attenuator scan")
        if self.adc is not None and now >= self._next_adc:
            try:
                ok = bool(self._adc_allowed())
            except Exception:
                ok = False
            if ok:                               # skipped passes retry on the next tick
                self._next_adc = now + self.adc_s
                try:
                    self.adc.probe()
                except Exception:
                    log.exception("ADC probe")

    def _run(self) -> None:
        while not self._stop.wait(0.25):
            self.step()

    def start(self) -> "DeviceWatcher":
        if self._thread is None and (self.atten is not None or self.adc is not None):
            self._stop.clear()
            self._next_at = self._clock() + self._atten_delay
            self._thread = threading.Thread(target=self._run, name="payload-devwatch", daemon=True)
            self._thread.start()
        return self

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stop(self, timeout: float = 3.0) -> None:
        """Stop the thread, then unpublish and close the attenuator it opened."""
        self._stop.set()
        th, self._thread = self._thread, None
        if th is not None and th is not threading.current_thread():
            th.join(timeout)
        if self.atten is not None:
            self.atten.close()
