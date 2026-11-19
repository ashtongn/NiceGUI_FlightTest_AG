"""payload_guard.py: RX front-end protection interlock for payload keying.

Opt-in through a ``payload:`` section in rf_frontend.yaml.  When present, the
service attaches an ``RxGuard`` to the RfFrontend; ``RfFrontend.pa_enable(True)``
then calls ``before_key()`` for every keying path (the /payload page, the Tools
panel's PA ON, a script sending frontend_set, the TX run's sequence_tx_on), so
nothing that talks to the service can key the HPA around it.

Rules (the payload: section of rf_frontend.example.yaml):

* key = T/R switch confirmed in TX (line readback, gpio mode only) and the RX
  attenuator read back at ``protect_db`` (the attenuator maximum, 120 dB; a lower
  value in the config is ignored).  There is no receive during TX: the RX chain is
  fully attenuated whatever the band or power.  ``min_db``, the worst case over the
  band from payload_profiles.yaml (HPA at Psat, lowest switch isolation), is kept as
  the limit on the power-on value and for the status.  The attenuator goes up before
  the switch line moves (before_switch_tx, best effort) and is confirmed again in
  before_key, which is the check that refuses.  With ``auto_protect`` on, the guard
  remembers the operator's value and sets ``protect_db``; with it off a lower
  readback refuses the key.  Where the profile has the attenuator after the LNA and
  the switch isolation alone cannot protect the LNA from the HPA at Psat
  (lna_unprotected in the status), the G40 TX attenuation must also be at or above
  ``lna_min_tx_atten_db`` (payload_budget.min_lna_safe_tx_atten); an unknown TX
  attenuation refuses (fail closed).
* while armed or with the switch in TX, atten_set below ``protect_db`` and
  set_tx_atten below the TX floor (the LNA floor and the load floor, whichever is
  higher) are refused by the service.  The service drops the HPA if the attenuator
  goes away or reads low during TX.
* unkey = HPA off, settle, switch RX (RfFrontend.sequence_tx_off / tr_switch),
  then ``after_rx()`` restores the operator's value.  A watchdog / fault / SAFE
  drop does not restore: the attenuator stays high until the operator restores
  it (payload_restore) with the switch in RX.
* no attenuator, an unreadable attenuator, or a broken profile = refuse to key
  (fail closed).
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional


class GuardRefused(Exception):
    """Keying refused by the RX protection guard (raised as InterlockError by
    rf_frontend so every caller handles it like the other interlocks)."""


class RxGuard:
    def __init__(self, atten, min_db: float, protect_db: Optional[float] = None,
                 auto_protect: bool = True, restore: bool = True, tol_db: float = 0.05,
                 require_tr_gpio: bool = True, require_tr_readback: bool = False,
                 load_power_dbm: Optional[float] = None, min_tx_atten_db: Optional[float] = None,
                 tx_load_achievable: bool = True, load_detail: Optional[dict] = None,
                 lna_min_tx_atten_db: Optional[float] = None, lna_tx_achievable: bool = True,
                 lna_floor_off: bool = False,
                 profile: str = "", detail: Optional[dict] = None,
                 config_error: Optional[str] = None, clock: Callable[[], float] = time.monotonic,
                 atten_max_db: Optional[float] = None):
        self.atten = atten
        self.min_db = float(min_db)
        # atten_max_db: the configured maximum, for a guard built while an
        # auto-attached attenuator is not plugged in yet
        mx = float(getattr(atten, "max_db", 120.0)) if atten is not None else \
            (120.0 if atten_max_db is None else float(atten_max_db))
        # never below the attenuator's maximum: the RX chain is fully attenuated in TX
        self.protect_db = mx if protect_db is None else max(mx, float(protect_db))
        if self.protect_db < self.min_db:
            self.protect_db = self.min_db
        self.auto_protect = bool(auto_protect)
        self.restore = bool(restore)
        self.tol_db = float(tol_db)
        self.require_tr_gpio = bool(require_tr_gpio)
        # also confirm the switch with the analog ADC readback (rf_frontend
        # tr_readback); lets proxy mode be used, since the ADC reads the real
        # control voltage whichever drives the switch
        self.require_tr_readback = bool(require_tr_readback)
        # load power protection: the smallest G40 TX attenuation that keeps the
        # power delivered to the bench load under its rating, worst case over the
        # band.  None = no load limit configured.  tx_load_achievable is False when
        # no TX attenuation in range is enough (the HPA-into-a-small-pad case): then
        # keying is refused outright.
        self.load_power_dbm = None if load_power_dbm is None else float(load_power_dbm)
        self.min_tx_atten_db = None if min_tx_atten_db is None else float(min_tx_atten_db)
        self.tx_load_achievable = bool(tx_load_achievable)
        self.load_detail = dict(load_detail or {})
        # LNA protection: the smallest G40 TX attenuation that keeps the leak
        # through the switch isolation under the LNA max input less the margin,
        # worst case over the band.  None = the isolation alone is enough.
        self.lna_min_tx_atten_db = None if lna_min_tx_atten_db is None else float(lna_min_tx_atten_db)
        self.lna_tx_achievable = bool(lna_tx_achievable)
        # bench only: the config said lna_floor: false, so no LNA floor is enforced
        # even though the profile needs one (reported so it is never silent)
        self.lna_floor_off = bool(lna_floor_off)
        self.profile = profile
        self.detail = dict(detail or {})
        self.config_error = config_error
        self.lock = threading.RLock()       # serialises every attenuator access
        self.armed = False                  # from before_key until the switch is back in RX
        self.saved_db: Optional[float] = None   # operator value to restore after unkey
        self.last_readback: Optional[float] = None
        self.last_refusal: Optional[str] = None
        self.refusals = 0
        self.restores = 0
        self._clock = clock

    def set_atten(self, atten) -> None:
        """Attach (or with None detach) the RX attenuator after the guard was
        built: atten.transport auto.  Without one every key is refused."""
        with self.lock:
            self.atten = atten
            self.last_readback = None

    # helpers
    def _read(self) -> float:
        if self.atten is None:
            raise GuardRefused("no RX attenuator configured: RX protection cannot be confirmed")
        try:
            v = float(self.atten.get_db())
        except Exception as e:
            raise GuardRefused(f"RX attenuator readback failed: {e}")
        self.last_readback = v
        return v

    def _refuse(self, msg: str):
        self.last_refusal = msg
        self.refusals += 1
        raise GuardRefused(msg)

    def ok_db(self, db: float) -> bool:
        return float(db) >= self.min_db - self.tol_db

    def protected(self, db: float) -> bool:
        """At the TX protect value (the attenuator maximum)?"""
        return float(db) >= self.protect_db - self.tol_db

    def _raise_to_protect(self) -> float:
        """Read the attenuator and set the protect value if it is lower, remembering
        the operator's value.  Returns the confirmed readback; raises GuardRefused."""
        v = self._read()
        if self.protected(v):
            return v
        if self.saved_db is None:
            self.saved_db = v
        try:
            self.atten.set_db(self.protect_db)
        except Exception as e:
            raise GuardRefused(f"RX attenuator set to {self.protect_db:.2f} dB failed: {e}")
        v = self._read()
        if not self.protected(v):
            raise GuardRefused(f"RX attenuator readback {v:.2f} dB after protect set is below "
                               f"{self.protect_db:.2f} dB")
        return v

    def _check_load_atten(self, fe) -> None:
        """Refuse to key when the HPA would drive the bench load past its rating.
        Called only when a load power limit is configured.  Reads the live G40 TX
        attenuation through the frontend getter the service sets; an unknown TX
        attenuation fails closed."""
        if not self.tx_load_achievable:
            mn = self.min_tx_atten_db
            self._refuse(f"load rated {self.load_power_dbm:g} dBm cannot be protected with the HPA "
                         f"keyed at any TX attenuation up to {mn:g} dB: use a higher-power load")
        tx = self._tx_atten(fe)
        if tx is None:
            self._refuse(f"load power limit set ({self.load_power_dbm:g} dBm) but the current TX "
                         "attenuation is unknown: refusing to key (fail closed)")
        if float(tx) < self.min_tx_atten_db - self.tol_db:
            self._refuse(f"TX attenuation {float(tx):.2f} dB below the minimum {self.min_tx_atten_db:.2f} dB "
                         f"that keeps the {self.load_power_dbm:g} dBm load within its rating")

    def _tx_atten(self, fe) -> Optional[float]:
        """Live G40 TX attenuation through the frontend getter the service sets,
        None when unknown."""
        getter = getattr(fe, "tx_atten_getter", None)
        if not callable(getter):
            return None
        try:
            tx = getter()
        except Exception as e:
            self.last_refusal = f"tx atten readback: {e}"
            return None
        return None if tx is None else float(tx)

    def _check_lna_atten(self, fe) -> None:
        """Refuse to key when the HPA could leak more into the LNA than it takes.
        Called only when the profile needs an LNA floor."""
        mn = self.lna_min_tx_atten_db
        if not self.lna_tx_achievable:
            self._refuse(f"the LNA cannot be protected with the HPA keyed at any TX attenuation up "
                         f"to {mn:g} dB: more switch isolation or a limiter is needed")
        tx = self._tx_atten(fe)
        if tx is None:
            self._refuse(f"LNA floor {mn:.2f} dB TX attenuation but the current TX attenuation is "
                         "unknown: refusing to key (fail closed)")
        if tx < mn - self.tol_db:
            self._refuse(f"TX attenuation {tx:.2f} dB below the {mn:.2f} dB floor that keeps the "
                         "LNA under its max input (switch isolation alone is not enough)")

    def tx_floor_db(self) -> Optional[float]:
        """The TX attenuation floor in force: the higher of the LNA floor and
        the load floor, None when neither applies."""
        fl = [v for v in (self.lna_min_tx_atten_db,
                          self.min_tx_atten_db if self.load_power_dbm is not None else None)
              if v is not None]
        return max(fl) if fl else None

    def tx_atten_request_ok(self, db: float, fe) -> Optional[str]:
        """Service set_tx_atten check: None = allowed, else the refusal reason.
        Below the floor is refused while armed or keyed; when not keyed, the next
        before_key refuses instead."""
        floor = self.tx_floor_db()
        keyed = bool(fe is not None and getattr(fe, "_keyed", False))
        if floor is not None and (self.armed or keyed) and float(db) < floor - self.tol_db:
            return (f"TX attenuation {float(db):g} dB below the {floor:.2f} dB floor while keyed "
                    "(payload guard)")
        return None

    # hooks called by RfFrontend (inside its lock)
    def before_key(self, fe) -> None:
        """Raise GuardRefused unless the RX side is protected.  Called with the
        PA still off; may raise the attenuator (auto_protect)."""
        with self.lock:
            if self.config_error:
                self._refuse(f"payload protection config error: {self.config_error}")
            tr = fe.cfg.tr_switch
            adc = getattr(fe, "_tr_adc", None)
            confirm_by_adc = self.require_tr_readback and adc is not None
            if self.require_tr_gpio and tr.mode != "gpio" and not confirm_by_adc:
                self._refuse("payload guard needs tr_switch.mode: gpio (switch position must be "
                             "software-confirmed), or require_tr_readback with an ADC readback; "
                             "proxy mode cannot be read back from the line")
            if tr.mode == "gpio":
                h = getattr(fe, "_tr", None)
                try:
                    lvl = bool(h.is_active()) if h is not None else None
                except Exception as e:
                    lvl = None
                    self.last_refusal = f"switch readback: {e}"
                if lvl is None or lvl != fe._tr_level(True) or not fe._tr_tx:
                    self._refuse("T/R switch not confirmed in TX (line readback)")
            if confirm_by_adc:
                # the ADC reads the real switch control voltage; a dead-band or
                # wrong-window level raises, reported as a guard refusal
                try:
                    fe.require_tr_readback(True)
                except Exception as e:
                    self._refuse(f"T/R switch not confirmed in TX by ADC readback: {e}")
            if self.load_power_dbm is not None:
                self._check_load_atten(fe)
            if self.lna_min_tx_atten_db is not None:
                self._check_lna_atten(fe)
            try:
                if self.auto_protect:
                    self._raise_to_protect()
                else:
                    v = self._read()
                    if not self.protected(v):
                        self._refuse(f"RX attenuator {v:.2f} dB is below {self.protect_db:.2f} dB "
                                     "(auto_protect off)")
            except GuardRefused as e:
                if self.last_refusal != str(e):
                    self._refuse(str(e))
                raise
            self.armed = True
            self.last_refusal = None

    def before_switch_tx(self) -> Optional[str]:
        """The switch is about to go to TX: raise the attenuator first.  Never raises,
        since moving the switch with the HPA off is harmless; before_key still refuses
        the key if this did not work.  Returns the problem, or None."""
        if not self.auto_protect:
            return None
        with self.lock:
            try:
                self._raise_to_protect()
            except GuardRefused as e:
                return str(e)
        return None

    def after_rx(self, fe) -> Optional[str]:
        """Switch is back in RX with the PA off: restore the operator's value.
        Never raises; returns an error string on failure."""
        with self.lock:
            self.armed = False
            if not self.restore or self.saved_db is None:
                return None
            if getattr(fe, "_keyed", False) or getattr(fe, "_tr_tx", False):
                return "not restored: PA keyed or switch in TX"
            return self._restore_locked()

    def on_safe(self, reason: str) -> None:
        """SAFE / watchdog / fault drop: PA off and switch RX already forced.
        Disarm, keep the protective attenuation (saved value stays pending)."""
        self.armed = False

    # operator
    def _restore_locked(self) -> Optional[str]:
        v = self.saved_db
        try:
            self.atten.set_db(v)
            rb = self._read()
        except Exception as e:
            return f"restore {v} dB failed: {e}"
        self.saved_db = None
        self.restores += 1
        return None if abs(rb - v) <= max(self.tol_db, 0.1) else f"restore readback {rb} != {v}"

    def restore_now(self, fe) -> Optional[str]:
        with self.lock:
            if self.saved_db is None:
                return None
            if fe is not None and (getattr(fe, "_keyed", False) or getattr(fe, "_tr_tx", False)):
                return "refused: PA keyed or switch in TX"
            self.armed = False
            return self._restore_locked()

    def atten_request_ok(self, db: float, fe) -> Optional[str]:
        """Service atten_set check: None = allowed, else the refusal reason."""
        keyed = bool(fe is not None and getattr(fe, "_keyed", False))
        tx = bool(fe is not None and getattr(fe, "_tr_tx", False))
        if (self.armed or keyed or tx) and not self.protected(db):
            return (f"RX attenuation stays at {self.protect_db:g} dB while "
                    f"{'keyed' if keyed or self.armed else 'the switch is in TX'}: {db:g} dB refused "
                    "(payload guard)")
        return None

    def status(self) -> dict:
        d = {"profile": self.profile, "min_db": round(self.min_db, 2),
             "protect_db": round(self.protect_db, 2), "auto": self.auto_protect,
             "armed": self.armed, "saved_db": self.saved_db,
             "readback_db": self.last_readback, "refusals": self.refusals,
             "last_refusal": (self.last_refusal[:90] if self.last_refusal else None),
             "config_error": (self.config_error[:90] if self.config_error else None),
             **{k: self.detail[k] for k in ("worst_freq_mhz", "margin_db", "lna_unprotected")
                if k in self.detail}}
        if self.lna_min_tx_atten_db is not None:
            d["lna_min_tx_atten_db"] = round(self.lna_min_tx_atten_db, 2)
            d["lna_tx_achievable"] = self.lna_tx_achievable
        if self.lna_floor_off:
            d["lna_floor_off"] = True
        if self.load_power_dbm is not None:
            d["load"] = {"load_power_dbm": self.load_power_dbm,
                         "min_tx_atten_db": (None if self.min_tx_atten_db is None
                                             else round(self.min_tx_atten_db, 2)),
                         "achievable": self.tx_load_achievable,
                         **{k: self.load_detail[k] for k in ("point", "worst_freq_mhz", "margin_db")
                            if k in self.load_detail}}
        return d


def build_guard(payload_cfg: dict, atten, load_profiles: Callable[[str], dict],
                min_safe: Callable[..., dict], profile_fn: Callable[..., Any],
                min_tx_atten: Optional[Callable[..., dict]] = None,
                atten_max_db: Optional[float] = None,
                lna_tx_floor: Optional[Callable[..., dict]] = None) -> RxGuard:
    """Construct the guard from rf_frontend.yaml's ``payload:`` mapping.  A broken
    profile still yields a guard: one that refuses every key (fail closed).
    ``min_tx_atten`` (payload_budget.min_safe_tx_atten) is used only when the
    config carries ``load_power_dbm``.  ``atten_max_db`` stands in for the
    attenuator's maximum when ``atten`` is None (auto, not plugged in yet).
    ``lna_tx_floor`` (payload_budget.min_lna_safe_tx_atten) sets the LNA floor
    when the profile needs one; ``lna_floor: false`` in the config leaves it out
    (bench work with the LNA not connected) and the status says lna_floor_off."""
    prof_name = str(payload_cfg.get("profile", ""))
    lna_off = not bool(payload_cfg.get("lna_floor", True))
    path = payload_cfg.get("profiles_file") or None
    floor = payload_cfg.get("min_rx_atten_db")
    margin = payload_cfg.get("margin_db")
    kw = dict(protect_db=payload_cfg.get("protect_db"),
              auto_protect=bool(payload_cfg.get("auto_protect", True)),
              lna_floor_off=lna_off,
              restore=bool(payload_cfg.get("restore_rx_atten", True)),
              require_tr_gpio=bool(payload_cfg.get("require_tr_gpio", True)),
              require_tr_readback=bool(payload_cfg.get("require_tr_readback", False)),
              atten_max_db=atten_max_db)
    load_power = payload_cfg.get("load_power_dbm")

    def _load_kw(data, prof) -> dict:
        """Compute the min TX attenuation for the configured load, or None/fail
        closed.  Load protection on top of a broken profile still refuses (the
        caller already sets config_error), so this only runs on a good profile."""
        if load_power is None or min_tx_atten is None:
            return {}
        lt = min_tx_atten(data, prof, float(load_power),
                          margin_db=payload_cfg.get("load_margin_db"),
                          point=str(payload_cfg.get("load_point", "antenna")),
                          atten_max_db=float(payload_cfg.get("tx_atten_max_db", 41.75)),
                          step_db=float(payload_cfg.get("tx_atten_step_db", 0.25)))
        return dict(load_power_dbm=float(load_power), min_tx_atten_db=lt["min_tx_atten_db"],
                    tx_load_achievable=bool(lt["achievable"]),
                    load_detail={"point": lt["point"], "worst_freq_mhz": lt["worst_freq_mhz"],
                                 "margin_db": lt["margin_db"]})
    def _lna_kw(data, prof) -> dict:
        if lna_tx_floor is None or lna_off:
            return {}
        r = lna_tx_floor(data, prof, margin_db=margin,
                         atten_max_db=float(payload_cfg.get("tx_atten_max_db", 41.75)),
                         step_db=float(payload_cfg.get("tx_atten_step_db", 0.25)))
        if not r["needed"]:
            return {}
        return dict(lna_min_tx_atten_db=r["min_tx_atten_db"], lna_tx_achievable=bool(r["achievable"]))
    mx = float(getattr(atten, "max_db", 120.0)) if atten is not None else \
        (120.0 if atten_max_db is None else float(atten_max_db))
    try:
        data = load_profiles(path)
        prof = profile_fn(data, prof_name)
        ms = min_safe(data, prof, margin_db=margin)
        mn = float(ms["min_rx_atten_db"])
        if floor is not None:
            mn = max(mn, float(floor))
        load_kw = {**_load_kw(data, prof), **_lna_kw(data, prof)}
        if not ms["achievable"] or mn > mx:
            return RxGuard(atten, mx, profile=prof_name, detail=ms,
                           config_error=f"min safe RX atten {mn:.2f} dB exceeds the attenuator "
                                        f"max {mx:.2f} dB", **kw, **load_kw)
        detail = {"worst_freq_mhz": ms["worst_freq_mhz"], "margin_db": ms["margin_db"],
                  "lna_unprotected": bool(ms["lna_unprotected_mhz"])}
        return RxGuard(atten, mn, profile=prof_name, detail=detail, **kw, **load_kw)
    except Exception as e:
        # load_power set but the profile is broken: fail closed on keying anyway.
        # config_error already refuses every key, so no load numbers are needed.
        return RxGuard(atten, mx, profile=prof_name, config_error=str(e),
                       load_power_dbm=load_power, tx_load_achievable=False, **kw)
