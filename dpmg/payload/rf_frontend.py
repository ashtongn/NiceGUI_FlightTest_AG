"""rf_frontend.py: external RF front-end control for the Matchstiq G40.

Keys an external power amplifier (PA enable), drives a TX/RX switch, and watches a
PA fault/ready input through the G40's 27-pin GPIO socket (Orin NX lines via
libgpiod).  The pin mapping lives in ``rf_frontend.yaml`` (see
``rf_frontend.example.yaml``), never in code: the socket pins in use are expected to
change while the hardware is being bench-tested, and ``frontend_reload`` re-maps
them without restarting the service.

Everything is opt-in.  With ``frontend: none`` (the default when the file is absent)
nothing in here is instantiated and no front-end line is touched.

Design rules (docs/GPIO_FRONTEND.md, "How the software sequences it"):

* PA line is forced inactive at open, close, atexit, on every exception inside a
  sequence, and by a watchdog when keyed with no ``tx_tick`` for ``watchdog_ms``.
* TX on  = switch to TX then settle then PA on then settle.
* TX off = (hang, burst mode only) then PA off then settle then switch to RX.
* Software interlock: PA is never keyed while the switch is not in TX (gpio
  mode) or while the PA fault input is asserted.
* ``tr_switch`` mode ``proxy`` means the NV100 FPGA's TR_N lines drive the switch
  and software only tracks the state (no GPIO is touched).
* ``pa_enable`` mode ``heartbeat`` is for a permit board between the G40 and the
  HPA: while keyed the PA line toggles at ``heartbeat_hz`` and the board turns the
  toggling into an enable; a steady line at either level means off.  A boot level,
  a service that never starts, or a crash while keyed then all leave the HPA off.

Line specs: any line may be a logical name (``cpu-gp-out1``), a Tegra pad name
(``PG.06``), or a bare integer offset; a ``fallback_line`` is tried when the first
form does not resolve (the R36 device tree may not carry the logical names).
"""
from __future__ import annotations

import atexit
import copy
import json
import logging
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Dict, Optional

from dpmg.paths import REPO_DIR
from dpmg.radio import port_map as _port_map

log = logging.getLogger("rf_frontend")

DEFAULT_CONFIG_NAME = "rf_frontend.yaml"
CONFIG_ENV = "DPMG_FRONTEND_CONFIG"
CONSUMER = "dpmg-rf-frontend"

# errors
class FrontendError(Exception):
    """Any front-end control failure (the PA has already been forced LOW)."""


class ConfigError(FrontendError):
    """rf_frontend.yaml is malformed, has unknown keys, or bad values."""


class FrontendFault(FrontendError):
    """PA fault/ready input is asserted: keying refused."""


class InterlockError(FrontendError):
    """Sequencing rule violated (e.g. PA keyed with the switch in RX)."""


# config
# G40 27-pin GPIO socket, Orin-controlled lines (G-Series HW User Manual v1.5, Table 12
# and the "GPIO" section p.40).  These are the defaults the example file documents;
# the real assignment is whatever rf_frontend.yaml says.
G40_SOCKET_LINES = {
    # logical name : (socket pin, chip, Tegra pad, direction, purpose)
    "cpu-gp-out1": (17, "gpiochip0", "PG.06", "out", "CPU_GP_OUT_1 -> PA enable (default)"),
    "cpu-gp-out2": (15, "gpiochip0", "PN.01", "out", "CPU_GP_OUT_2 -> TX/RX switch, software drive (default)"),
    "cpu-gpio":    (16, "gpiochip0", "PP.06", "in/out", "CPU_GPIO, direction via cpu-gpio-direction (aon PCC.03)"),
    "cpu-gp-in":   (18, "gpiochip1", "PCC.01", "in", "CPU_GP_IN -> PA fault / ready input (default)"),
}

_ALLOWED = {
    "frontend": None,
    "config_version": None,
    "watchdog_ms": None,
    "pa_enable": {"chip", "line", "fallback_line", "active_high", "settle_ms", "hang_ms",
                  "mode", "heartbeat_hz", "dir_chip", "dir_line", "dir_fallback_line"},
    "tr_switch": {"mode", "chip", "line", "fallback_line", "tx_level", "settle_ms",
                  "dir_chip", "dir_line", "dir_fallback_line"},
    "pa_fault_in": {"enabled", "chip", "line", "fallback_line", "active_low"},
    "atten": {"transport", "path", "url", "startup_db", "max_db", "step_db"},
    # Optional analog readback of the T/R switch control line through an I2C ADC.
    # A digital input cannot tell a valid level from a floating one; the ADC reads
    # the real voltage and a half-rail level is called a fault.  See
    # docs/GPIO_FRONTEND.md and the design note gpio_switch_readback_adc.md.
    "tr_readback": {"enabled", "transport", "bus", "address", "channel",
                    "full_scale_v", "divider", "settle_ms",
                    "tx_window_v", "rx_window_v"},
    # Optional HPA health readback: the Empower 1189's DB9 analog monitor outputs
    # (pin 3 temperature, pin 2 supply current) through the same kind of I2C ADC,
    # usually two more channels of the tr_readback chip.  Display only: it never
    # gates a key.  See docs/GPIO_FRONTEND.md.
    "hpa_monitor": {"enabled", "transport", "bus", "address", "temp_channel",
                    "current_channel", "full_scale_v", "temp_offset_v", "temp_v_per_c",
                    "valid_when_off",
                    "current_v_per_a", "current_offset_v", "poll_s", "warn_c"},
    # Optional; present = RX-protection interlock on every PA key
    # (payload_guard.py, thresholds from payload_profiles.yaml)
    "payload": {"enabled", "profile", "profiles_file", "min_rx_atten_db", "margin_db",
                "protect_db", "auto_protect", "restore_rx_atten", "require_tr_gpio",
                "require_tr_readback", "load_power_dbm", "load_margin_db", "load_point",
                "tx_atten_max_db", "tx_atten_step_db", "lna_floor"},
    # Optional; how each SMA jack is connected and what it may do (port_map.py).
    # Jack names are the keys, so port_map.parse does the checking.
    "ports": None,
}
FRONTEND_KINDS = ("none", "gpio", "mock")
TR_MODES = ("proxy", "gpio")
PA_MODES = ("level", "heartbeat")
ATTEN_TRANSPORTS = ("none", "auto", "hidraw", "http", "mock")
TR_READBACK_TRANSPORTS = ("none", "i2c", "mock")
HPA_MONITOR_TRANSPORTS = TR_READBACK_TRANSPORTS
# HPA monitor states, from hpa_monitor_state()
HPA_MON_OK, HPA_MON_NOT_FITTED = "ok", "not fitted"
# the top-level ports: key, and the line set_ports leaves inside that section
_PORTS_KEY = re.compile(r"^ports\s*:")
_PORTS_STAMP = "# last set by "
# T/R switch readback states, from classify_tr()
TR_TX, TR_RX, TR_BAD = "tx", "rx", "indeterminate"


@dataclass
class LineCfg:
    chip: str = "gpiochip0"
    line: Any = None              # name (str), pad name (str) or int offset
    fallback_line: Any = None
    active_high: bool = True
    settle_ms: float = 0.0
    # optional level-translator direction line, held HIGH (= output) while the line is
    # in use: socket pin 16 (cpu-gpio) needs aon PCC.03 (cpu-gpio-direction) high
    dir_chip: str = "gpiochip1"
    dir_line: Any = None
    dir_fallback_line: Any = None


@dataclass
class PaCfg(LineCfg):
    chip: str = "gpiochip0"
    line: Any = "cpu-gp-out1"
    fallback_line: Any = "PG.06"
    settle_ms: float = 2.0
    hang_ms: float = 20.0
    # level: the line is held at the keyed level.  heartbeat: the line toggles at
    # heartbeat_hz while keyed, for a permit board that needs edges to enable
    mode: str = "level"
    heartbeat_hz: float = 200.0


@dataclass
class TrCfg(LineCfg):
    mode: str = "proxy"
    chip: str = "gpiochip0"
    line: Any = "cpu-gp-out2"
    fallback_line: Any = "PN.01"
    tx_level: int = 1             # line level that means transmit
    settle_ms: float = 1.0


@dataclass
class FaultCfg(LineCfg):
    enabled: bool = False
    chip: str = "gpiochip1"
    line: Any = "cpu-gp-in"
    fallback_line: Any = "PCC.01"
    active_low: bool = True       # fault asserted when the line reads LOW


@dataclass
class AttenCfg:
    transport: str = "none"
    path: str = "/dev/hidraw0"
    url: str = "http://192.168.0.40"
    startup_db: float = 120.0
    max_db: float = 120.0
    step_db: float = 0.05


@dataclass
class TrReadbackCfg:
    """Analog readback of the T/R switch control voltage through an I2C ADC.
    Windows are in node volts (after the divider), so they read in real-world
    terms.  enabled defaults False, which leaves the readback off."""
    enabled: bool = False
    transport: str = "i2c"            # i2c (ADS1x15) or mock
    bus: str = "/dev/i2c-1"
    address: int = 0x48
    channel: int = 0                  # single-ended AIN0..AIN3
    full_scale_v: float = 4.096       # ADS1x15 PGA full-scale range
    divider: float = 1.0              # node volts = adc volts * divider
    settle_ms: float = 2.0            # wait before the read, after the switch moves
    tx_window_v: list = field(default_factory=lambda: [2.7, 5.5])
    rx_window_v: list = field(default_factory=lambda: [-0.5, 0.8])


@dataclass
class HpaMonitorCfg:
    """Empower 1189 DB9 monitor outputs through an I2C ADC.  The datasheet scales
    are the defaults: temperature 10 mV per degree C above a 0.5 V offset, supply
    current 25 mV per 100 mA (0.25 V per A).  enabled defaults False."""
    enabled: bool = False
    transport: str = "i2c"            # i2c (ADS1x15) or mock
    bus: str = "/dev/i2c-1"
    address: int = 0x48
    temp_channel: int = 1             # single-ended AIN1: DB9 pin 3
    current_channel: int = 2          # single-ended AIN2: DB9 pin 2
    full_scale_v: float = 4.096       # both outputs stay under 2.5 V
    temp_offset_v: float = 0.5        # T = (V - temp_offset_v) / temp_v_per_c
    temp_v_per_c: float = 0.01
    current_v_per_a: float = 0.25     # I = (V - current_offset_v) / current_v_per_a
    current_offset_v: float = 0.0
    poll_s: float = 2.0               # a status call re-reads the chip at most this often
    # The 1189's outputs read nonsense while it is shut down (2026-11-03: 98 C and 1.6 A on a cool HPA
    # with the whole payload drawing 1.3 A), so by default a reading counts only while the HPA is keyed
    valid_when_off: bool = False
    warn_c: float = 70.0              # hot above this (the 1189 derates from 85 C)


@dataclass
class FrontendConfig:
    frontend: str = "none"
    config_version: int = 1
    watchdog_ms: float = 500.0
    pa_enable: PaCfg = field(default_factory=PaCfg)
    tr_switch: TrCfg = field(default_factory=TrCfg)
    pa_fault_in: FaultCfg = field(default_factory=FaultCfg)
    atten: AttenCfg = field(default_factory=AttenCfg)
    tr_readback: TrReadbackCfg = field(default_factory=TrReadbackCfg)
    hpa_monitor: HpaMonitorCfg = field(default_factory=HpaMonitorCfg)
    payload: Optional[dict] = None    # None = no payload interlock
    ports: dict = field(default_factory=dict)   # {} = no port restriction
    source: str = "defaults"      # file path, "defaults", or "defaults+overrides"

    def to_dict(self) -> dict:
        """Mapping in the config-file shape (only keys the file schema allows, so
        inherited dataclass fields such as tr_switch.active_high never leak in)."""
        d = asdict(self)
        for sec, allowed in _ALLOWED.items():
            if allowed is not None and isinstance(d.get(sec), dict):
                d[sec] = {k: v for k, v in d[sec].items() if k in allowed}
        return d


def parse_line_spec(value: Any) -> tuple[str, Any]:
    """Returns ("offset", int) | ("name", str) | ("none", None).  Accepts an int, a digit
    string, a logical name or a Tegra pad name."""
    if value is None or value == "":
        return ("none", None)
    if isinstance(value, bool):
        raise ConfigError(f"line spec {value!r}: booleans are not a line")
    if isinstance(value, int):
        if value < 0:
            raise ConfigError(f"line offset {value} must be >= 0")
        return ("offset", int(value))
    s = str(value).strip()
    if s.isdigit():
        return ("offset", int(s))
    if not s:
        return ("none", None)
    return ("name", s)


def chip_path(chip: str) -> str:
    c = str(chip).strip()
    if not c:
        raise ConfigError("chip must not be empty")
    return c if c.startswith("/") else f"/dev/{c}"


def _check_keys(section: str, data: dict, allowed: set) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ConfigError(f"{DEFAULT_CONFIG_NAME}: unknown key(s) {unknown} in '{section}' "
                          f"(allowed: {sorted(allowed)})")


def _num(section: str, key: str, v: Any, lo: float = 0.0, hi: float = 1e9) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ConfigError(f"{section}.{key}: expected a number, got {v!r}")
    if not (lo <= f <= hi):
        raise ConfigError(f"{section}.{key}: {f} outside [{lo}, {hi}]")
    return f


def _bool(section: str, key: str, v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip().lower() in ("true", "yes", "on", "1"):
        return True
    if isinstance(v, str) and v.strip().lower() in ("false", "no", "off", "0"):
        return False
    raise ConfigError(f"{section}.{key}: expected true/false, got {v!r}")


def _window(section: str, key: str, v: Any) -> tuple:
    """A [lo, hi] voltage window, lo <= hi."""
    if not (isinstance(v, (list, tuple)) and len(v) == 2):
        raise ConfigError(f"{section}.{key}: expected [lo, hi], got {v!r}")
    lo = _num(section, f"{key}[0]", v[0], -100, 100)
    hi = _num(section, f"{key}[1]", v[1], -100, 100)
    if lo > hi:
        raise ConfigError(f"{section}.{key}: lo {lo} is above hi {hi}")
    return [lo, hi]


def config_from_dict(data: dict, source: str = "dict") -> FrontendConfig:
    """Validate a raw mapping (from YAML/JSON) into a FrontendConfig.  Unknown keys
    and bad values raise ConfigError with the offending path."""
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"{source}: top level must be a mapping, got {type(data).__name__}")
    _check_keys("<top>", data, set(_ALLOWED))
    cfg = FrontendConfig(source=source)

    fe = str(data.get("frontend", cfg.frontend)).strip().lower()
    if fe not in FRONTEND_KINDS:
        raise ConfigError(f"frontend: {fe!r} not in {FRONTEND_KINDS}")
    cfg.frontend = fe
    if "config_version" in data:
        cfg.config_version = int(_num("<top>", "config_version", data["config_version"], 1, 1000))
    if "watchdog_ms" in data:
        cfg.watchdog_ms = _num("<top>", "watchdog_ms", data["watchdog_ms"], 0, 60000)

    def _line_common(section: str, d: dict, target: LineCfg) -> None:
        if "chip" in d:
            target.chip = str(d["chip"]).strip()
            chip_path(target.chip)
        if "line" in d:
            parse_line_spec(d["line"])
            target.line = d["line"]
        if "fallback_line" in d:
            parse_line_spec(d["fallback_line"])
            target.fallback_line = d["fallback_line"]
        if "settle_ms" in d:
            target.settle_ms = _num(section, "settle_ms", d["settle_ms"], 0, 5000)
        if "dir_chip" in d:
            target.dir_chip = str(d["dir_chip"]).strip()
            chip_path(target.dir_chip)
        for k in ("dir_line", "dir_fallback_line"):
            if k in d:
                parse_line_spec(d[k])
                setattr(target, k, d[k])

    pa = data.get("pa_enable") or {}
    if not isinstance(pa, dict):
        raise ConfigError("pa_enable: must be a mapping")
    _check_keys("pa_enable", pa, _ALLOWED["pa_enable"])
    _line_common("pa_enable", pa, cfg.pa_enable)
    if "active_high" in pa:
        cfg.pa_enable.active_high = _bool("pa_enable", "active_high", pa["active_high"])
    if "hang_ms" in pa:
        cfg.pa_enable.hang_ms = _num("pa_enable", "hang_ms", pa["hang_ms"], 0, 10000)
    if "mode" in pa:
        m = str(pa["mode"]).strip().lower()
        if m not in PA_MODES:
            raise ConfigError(f"pa_enable.mode: {pa['mode']!r} not one of {PA_MODES}")
        cfg.pa_enable.mode = m
    if "heartbeat_hz" in pa:
        cfg.pa_enable.heartbeat_hz = _num("pa_enable", "heartbeat_hz", pa["heartbeat_hz"],
                                          10, 2000)

    tr = data.get("tr_switch") or {}
    if not isinstance(tr, dict):
        raise ConfigError("tr_switch: must be a mapping")
    _check_keys("tr_switch", tr, _ALLOWED["tr_switch"])
    _line_common("tr_switch", tr, cfg.tr_switch)
    if "mode" in tr:
        m = str(tr["mode"]).strip().lower()
        if m not in TR_MODES:
            raise ConfigError(f"tr_switch.mode: {m!r} not in {TR_MODES}")
        cfg.tr_switch.mode = m
    if "tx_level" in tr:
        lvl = int(_num("tr_switch", "tx_level", tr["tx_level"], 0, 1))
        cfg.tr_switch.tx_level = lvl

    fi = data.get("pa_fault_in") or {}
    if not isinstance(fi, dict):
        raise ConfigError("pa_fault_in: must be a mapping")
    _check_keys("pa_fault_in", fi, _ALLOWED["pa_fault_in"])
    _line_common("pa_fault_in", fi, cfg.pa_fault_in)
    if "enabled" in fi:
        cfg.pa_fault_in.enabled = _bool("pa_fault_in", "enabled", fi["enabled"])
    if "active_low" in fi:
        cfg.pa_fault_in.active_low = _bool("pa_fault_in", "active_low", fi["active_low"])

    at = data.get("atten") or {}
    if not isinstance(at, dict):
        raise ConfigError("atten: must be a mapping")
    _check_keys("atten", at, _ALLOWED["atten"])
    if "transport" in at:
        t = str(at["transport"]).strip().lower()
        if t not in ATTEN_TRANSPORTS:
            raise ConfigError(f"atten.transport: {t!r} not in {ATTEN_TRANSPORTS}")
        cfg.atten.transport = t
    if "path" in at:
        cfg.atten.path = str(at["path"])
    elif cfg.atten.transport == "auto":
        cfg.atten.path = "auto"       # auto with no path: take the single unit found
    if "url" in at:
        cfg.atten.url = str(at["url"]).rstrip("/")
    if "max_db" in at:
        cfg.atten.max_db = _num("atten", "max_db", at["max_db"], 0, 200)
    if "step_db" in at:
        cfg.atten.step_db = _num("atten", "step_db", at["step_db"], 0.01, 10)
    if "startup_db" in at:
        cfg.atten.startup_db = _num("atten", "startup_db", at["startup_db"], 0, cfg.atten.max_db)

    rb = data.get("tr_readback") or {}
    if not isinstance(rb, dict):
        raise ConfigError("tr_readback: must be a mapping")
    _check_keys("tr_readback", rb, _ALLOWED["tr_readback"])
    if "enabled" in rb:
        cfg.tr_readback.enabled = _bool("tr_readback", "enabled", rb["enabled"])
    if "transport" in rb:
        t = str(rb["transport"]).strip().lower()
        if t not in TR_READBACK_TRANSPORTS:
            raise ConfigError(f"tr_readback.transport: {t!r} not in {TR_READBACK_TRANSPORTS}")
        cfg.tr_readback.transport = t
    if "bus" in rb:
        cfg.tr_readback.bus = str(rb["bus"])
    if "address" in rb:
        av = rb["address"]
        av = int(av, 0) if isinstance(av, str) else int(av)
        if not (0 <= av <= 127):
            raise ConfigError(f"tr_readback.address: {av} outside 0..127")
        cfg.tr_readback.address = av
    if "channel" in rb:
        cfg.tr_readback.channel = int(_num("tr_readback", "channel", rb["channel"], 0, 3))
    if "full_scale_v" in rb:
        cfg.tr_readback.full_scale_v = _num("tr_readback", "full_scale_v", rb["full_scale_v"], 0.1, 10)
    if "divider" in rb:
        cfg.tr_readback.divider = _num("tr_readback", "divider", rb["divider"], 0.01, 100)
    if "settle_ms" in rb:
        cfg.tr_readback.settle_ms = _num("tr_readback", "settle_ms", rb["settle_ms"], 0, 1000)
    for wk in ("tx_window_v", "rx_window_v"):
        if wk in rb:
            setattr(cfg.tr_readback, wk, _window("tr_readback", wk, rb[wk]))
    if cfg.tr_readback.enabled:
        lo_tx, hi_tx = cfg.tr_readback.tx_window_v
        lo_rx, hi_rx = cfg.tr_readback.rx_window_v
        if lo_tx <= hi_rx and lo_rx <= hi_tx:
            raise ConfigError("tr_readback: tx_window_v and rx_window_v overlap, "
                              "so a level could read as both; leave a dead band between them")

    hm = data.get("hpa_monitor") or {}
    if not isinstance(hm, dict):
        raise ConfigError("hpa_monitor: must be a mapping")
    _check_keys("hpa_monitor", hm, _ALLOWED["hpa_monitor"])
    if "enabled" in hm:
        cfg.hpa_monitor.enabled = _bool("hpa_monitor", "enabled", hm["enabled"])
    if "transport" in hm:
        t = str(hm["transport"]).strip().lower()
        if t not in HPA_MONITOR_TRANSPORTS:
            raise ConfigError(f"hpa_monitor.transport: {t!r} not in {HPA_MONITOR_TRANSPORTS}")
        cfg.hpa_monitor.transport = t
    if "bus" in hm:
        cfg.hpa_monitor.bus = str(hm["bus"])
    if "address" in hm:
        av = hm["address"]
        av = int(av, 0) if isinstance(av, str) else int(av)
        if not (0 <= av <= 127):
            raise ConfigError(f"hpa_monitor.address: {av} outside 0..127")
        cfg.hpa_monitor.address = av
    for ck in ("temp_channel", "current_channel"):
        if ck in hm:
            setattr(cfg.hpa_monitor, ck, int(_num("hpa_monitor", ck, hm[ck], 0, 3)))
    if "full_scale_v" in hm:
        cfg.hpa_monitor.full_scale_v = _num("hpa_monitor", "full_scale_v", hm["full_scale_v"], 0.1, 10)
    for nk, lo, hi in (("temp_offset_v", -10, 10), ("temp_v_per_c", 1e-6, 10),
                       ("current_v_per_a", 1e-6, 100), ("current_offset_v", -10, 10),
                       ("poll_s", 0, 3600), ("warn_c", -40, 200)):
        if nk in hm:
            setattr(cfg.hpa_monitor, nk, _num("hpa_monitor", nk, hm[nk], lo, hi))
    if "valid_when_off" in hm:
        cfg.hpa_monitor.valid_when_off = _bool("hpa_monitor", "valid_when_off", hm["valid_when_off"])
    if cfg.hpa_monitor.enabled:
        if cfg.hpa_monitor.temp_channel == cfg.hpa_monitor.current_channel:
            raise ConfigError("hpa_monitor: temp_channel and current_channel must differ")
        rb = cfg.tr_readback
        if rb.enabled and rb.transport == "i2c" and cfg.hpa_monitor.transport == "i2c" \
                and rb.bus == cfg.hpa_monitor.bus and rb.address == cfg.hpa_monitor.address \
                and rb.channel in (cfg.hpa_monitor.temp_channel, cfg.hpa_monitor.current_channel):
            raise ConfigError(f"hpa_monitor: channel {rb.channel} is the tr_readback channel "
                              "on the same chip")

    pl = data.get("payload")
    if pl is not None:
        if not isinstance(pl, dict):
            raise ConfigError("payload: must be a mapping")
        _check_keys("payload", pl, _ALLOWED["payload"])
        pl = dict(pl)
        for k in ("enabled", "auto_protect", "restore_rx_atten", "require_tr_gpio",
                  "require_tr_readback"):
            if k in pl:
                pl[k] = _bool("payload", k, pl[k])
        for k in ("min_rx_atten_db", "protect_db"):
            if pl.get(k) is not None:
                pl[k] = _num("payload", k, pl[k], 0, 200)
        if pl.get("margin_db") is not None:
            pl["margin_db"] = _num("payload", "margin_db", pl["margin_db"], 0, 60)
        # load power protection: refuse to key the HPA into a load rated below the
        # delivered power (a small pad is G40-level only, not for the HPA output)
        if pl.get("load_power_dbm") is not None:
            pl["load_power_dbm"] = _num("payload", "load_power_dbm", pl["load_power_dbm"], -30, 80)
        if pl.get("load_margin_db") is not None:
            pl["load_margin_db"] = _num("payload", "load_margin_db", pl["load_margin_db"], 0, 60)
        if pl.get("tx_atten_max_db") is not None:
            pl["tx_atten_max_db"] = _num("payload", "tx_atten_max_db", pl["tx_atten_max_db"], 0, 120)
        if pl.get("tx_atten_step_db") is not None:
            pl["tx_atten_step_db"] = _num("payload", "tx_atten_step_db", pl["tx_atten_step_db"], 0.01, 10)
        lp = pl.get("load_point")
        if lp is not None and str(lp) not in ("antenna", "switch", "hpa"):
            raise ConfigError("payload.load_point must be antenna, switch or hpa")
        if pl.get("enabled", True):
            if not str(pl.get("profile") or "").strip():
                raise ConfigError("payload.profile is required (e.g. P1)")
            cfg.payload = pl

    if "ports" in data:
        try:
            cfg.ports = _port_map.parse(data["ports"])
        except ValueError as e:
            raise ConfigError(f"ports: {e}")

    # cross-field sanity
    if cfg.frontend == "gpio":
        if parse_line_spec(cfg.pa_enable.line)[0] == "none":
            raise ConfigError("pa_enable.line is required when frontend: gpio")
        if cfg.tr_switch.mode == "gpio" and parse_line_spec(cfg.tr_switch.line)[0] == "none":
            raise ConfigError("tr_switch.line is required when tr_switch.mode: gpio")
    return cfg


def apply_overrides(cfg: FrontendConfig, overrides: Optional[Dict[str, Any]]) -> FrontendConfig:
    """Dotted-key overrides (CLI flags): {'frontend': 'gpio', 'pa_enable.line': 'PG.06',
    'tr_switch.mode': 'gpio', 'atten.transport': 'hidraw', ...}.  None values are
    ignored so argparse defaults can be passed straight through."""
    if not overrides:
        return cfg
    raw = cfg.to_dict()
    raw.pop("source", None)
    applied = False
    for k, v in overrides.items():
        if v is None:
            continue
        applied = True
        parts = k.split(".")
        if len(parts) == 1:
            if parts[0] not in _ALLOWED or _ALLOWED[parts[0]] is not None:
                raise ConfigError(f"override {k!r}: not a scalar top-level key")
            raw[parts[0]] = v
        elif len(parts) == 2 and parts[0] in _ALLOWED and _ALLOWED[parts[0]] is not None:
            raw.setdefault(parts[0], {})[parts[1]] = v
        else:
            raise ConfigError(f"override {k!r}: unknown key")
    if not applied:
        return cfg
    return config_from_dict(raw, source=cfg.source + "+overrides")


def default_config_path() -> str:
    p = os.environ.get(CONFIG_ENV)
    if p:
        return p
    return os.path.join(REPO_DIR, DEFAULT_CONFIG_NAME)


def load_config(path: Optional[str] = None,
                overrides: Optional[Dict[str, Any]] = None,
                required: bool = False) -> FrontendConfig:
    """Load rf_frontend.yaml (YAML or JSON).  A missing file is not an error unless
    required=True: it yields the defaults (frontend: none) so an un-configured G40
    runs with no front-end control."""
    path = path or default_config_path()
    if not os.path.exists(path):
        if required:
            raise ConfigError(f"front-end config not found: {path}")
        cfg = FrontendConfig(source="defaults")
        return apply_overrides(cfg, overrides)
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    data: Any
    try:
        import yaml  # pyyaml is a project dependency
        data = yaml.safe_load(text)
    except ImportError:                      # pragma: no cover
        import json
        data = json.loads(text)
    except Exception as e:
        raise ConfigError(f"{path}: cannot parse: {e}")
    cfg = config_from_dict(data, source=path)
    return apply_overrides(cfg, overrides)


# ports: on disk
def _yaml_load(text: str, path: str) -> Any:
    try:
        import yaml
        return yaml.safe_load(text)
    except Exception as e:
        raise ConfigError(f"{path}: cannot parse: {e}")


def _ports_block_span(lines: list) -> Optional[tuple]:
    """(first, end) line indexes of the top-level ports: block, None if there is
    none. The block runs to the next line that starts in column 0, so a comment
    above the next key stays with that key. Blank lines after it are not part of it."""
    first = next((i for i, l in enumerate(lines) if _PORTS_KEY.match(l)), None)
    if first is None:
        return None
    end = first + 1
    while end < len(lines) and (not lines[end].strip() or lines[end][0] in " \t"):
        end += 1
    while end > first + 1 and not lines[end - 1].strip():
        end -= 1
    return first, end


def ports_block(ports: dict, stamp: Optional[str] = None) -> list:
    """The ports: section as text lines, one jack per line, panel order. Chain text
    goes through JSON, which YAML reads back unchanged."""
    if not ports:
        return []
    lines = ["ports:"]
    if stamp:
        lines.append(f"  {_PORTS_STAMP}{' '.join(str(stamp).split())}")
    for jack in _port_map.ordered(ports):
        e = ports[jack]
        lines.append(f"  {jack}: {{role: {e['role']}, chain: {json.dumps(e.get('chain') or '')}}}")
    return lines


def replace_ports_block(text: str, ports: dict, stamp: Optional[str] = None) -> str:
    """text with its ports: section swapped for this one (ports empty: removed, no
    section yet: added at the end). Every other line stays as it is, comments too."""
    lines = text.split("\n")
    new = ports_block(ports, stamp)
    span = _ports_block_span(lines)
    if span is not None:
        lines[span[0]:span[1]] = new
        return "\n".join(lines)
    if not new:
        return text
    return text.rstrip("\n") + "\n\n" + "\n".join(new) + "\n"


def read_ports_stamp(path: str) -> Optional[str]:
    """Who last set the ports: section through set_ports, from the line it wrote."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().split("\n")
    except OSError:
        return None
    span = _ports_block_span(lines)
    for line in lines[span[0]:span[1]] if span else []:
        if line.strip().startswith(_PORTS_STAMP):
            return line.strip()[len(_PORTS_STAMP):] or None
    return None


def _write_keeping_owner(path: str, text: str) -> None:
    """Replace the file in one step, with its owner and mode kept: the service runs
    as root in its container and the file belongs to the radio's login user, who has
    to be able to edit it afterwards. A new file takes the owner of its folder."""
    folder = os.path.dirname(os.path.abspath(path))
    was = os.stat(path) if os.path.exists(path) else None
    owner = was or os.stat(folder)
    tmp = f"{path}.tmp{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, was.st_mode & 0o7777 if was else 0o664)
        if hasattr(os, "chown") and os.geteuid() == 0:
            os.chown(tmp, owner.st_uid, owner.st_gid)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def write_ports(path: str, ports: dict, stamp: Optional[str] = None) -> FrontendConfig:
    """Write the ports: section of the config file and return the loaded result.
    The new text is checked first: it must load, its ports must be the ones asked
    for and everything else must read the same as before; if not, the file is not
    touched. Raises ConfigError (OSError if the write itself fails)."""
    try:
        ports = _port_map.parse(ports)
    except ValueError as e:
        raise ConfigError(f"ports: {e}")
    text = "config_version: 1\nfrontend: none\n"
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    new_text = replace_ports_block(text, ports, stamp)
    old, new = _yaml_load(text, path), _yaml_load(new_text, path)
    if not isinstance(old, dict) or not isinstance(new, dict):
        raise ConfigError(f"{path}: top level is not a mapping, so ports: was not written")
    old_rest = {k: v for k, v in old.items() if k != "ports"}
    new_rest = {k: v for k, v in new.items() if k != "ports"}
    if old_rest != new_rest:
        raise ConfigError(f"{path}: changing ports: would change other settings, so it was "
                          f"not written (is the file in an unusual format?)")
    cfg = config_from_dict(new, source=path)
    if cfg.ports != ports:
        raise ConfigError(f"{path}: ports: did not read back as written, so it was not "
                          f"written (is the file in an unusual format?)")
    _write_keeping_owner(path, new_text)
    return cfg


# GPIO backends
class GpioHandle:
    """One requested line.  Subclasses: gpiod v2, gpiod v1, in-memory."""
    chip: str = ""
    offset: int = -1
    spec: Any = None
    resolved_by: str = ""       # "name" | "fallback" | "offset"
    direction: str = "out"

    def set_active(self, active: bool) -> None: raise NotImplementedError
    def is_active(self) -> bool: raise NotImplementedError
    def release(self) -> None: pass

    def info(self) -> dict:
        return {"chip": self.chip, "offset": self.offset, "line": self.spec,
                "resolved_by": self.resolved_by, "direction": self.direction}


class GpioBackend:
    def open_line(self, chip: str, spec: Any, fallback: Any, direction: str,
                  active_high: bool, initial_active: bool = False) -> GpioHandle:
        raise NotImplementedError


def _resolve(chip_obj_lookup: Callable[[Any], int], spec: Any, fallback: Any) -> tuple[int, str]:
    """Try spec then fallback; each may be a name or an offset.  Returns (offset, how)."""
    errs = []
    for how, cand in (("primary", spec), ("fallback", fallback)):
        kind, val = parse_line_spec(cand)
        if kind == "none":
            continue
        try:
            off = chip_obj_lookup(val)
            return int(off), ("offset" if kind == "offset" else ("name" if how == "primary" else "fallback"))
        except Exception as e:
            errs.append(f"{cand!r}: {e}")
    raise FrontendError("cannot resolve line (" + "; ".join(errs) + ")")


class GpiodBackend(GpioBackend):
    """libgpiod python bindings.  v2.x (pip 'gpiod') preferred; v1.6 (Ubuntu
    python3-libgpiod, as on the G40 host) supported through a small shim."""

    def __init__(self):
        try:
            import gpiod  # noqa: F401
        except ImportError as e:
            raise FrontendError("python 'gpiod' module not installed (vendor the aarch64 wheel "
                                "from deploy/wheels or apt install python3-libgpiod)") from e
        self._gpiod = gpiod
        self.v2 = hasattr(gpiod, "request_lines")

    def open_line(self, chip, spec, fallback, direction, active_high, initial_active=False):
        path = chip_path(chip)
        if self.v2:
            return self._open_v2(path, spec, fallback, direction, active_high, initial_active)
        return self._open_v1(path, spec, fallback, direction, active_high, initial_active)

    # v2
    def _open_v2(self, path, spec, fallback, direction, active_high, initial_active):
        gpiod = self._gpiod
        from gpiod.line import Direction, Value
        chip = gpiod.Chip(path)
        try:
            nlines = chip.get_info().num_lines

            def lookup(v):
                if isinstance(v, int):
                    if v >= nlines:
                        raise FrontendError(f"offset {v} >= {nlines} lines on {path}")
                    return v
                return chip.line_offset_from_id(v)
            offset, how = _resolve(lookup, spec, fallback)
        finally:
            chip.close()
        if direction == "out":
            settings = gpiod.LineSettings(direction=Direction.OUTPUT, active_low=not active_high,
                                          output_value=Value.ACTIVE if initial_active else Value.INACTIVE)
        else:
            settings = gpiod.LineSettings(direction=Direction.INPUT, active_low=not active_high)
        req = gpiod.request_lines(path, consumer=CONSUMER, config={offset: settings})

        class H(GpioHandle):
            def set_active(self_, active):
                req.set_value(offset, Value.ACTIVE if active else Value.INACTIVE)

            def is_active(self_):
                return req.get_value(offset) == Value.ACTIVE

            def release(self_):
                try: req.release()
                except Exception: pass
        h = H(); h.chip, h.offset, h.spec, h.resolved_by, h.direction = path, offset, spec, how, direction
        return h

    # v1.6 shim
    def _open_v1(self, path, spec, fallback, direction, active_high, initial_active):
        gpiod = self._gpiod
        chip = gpiod.chip(path)

        def lookup(v):
            if isinstance(v, int):
                if v >= chip.num_lines:
                    raise FrontendError(f"offset {v} >= {chip.num_lines} lines on {path}")
                return v
            ln = chip.find_line(v)
            if ln is None:
                raise FrontendError(f"no line named {v!r} on {path}")
            return ln.offset
        offset, how = _resolve(lookup, spec, fallback)
        line = chip.get_line(offset)
        cfg = gpiod.line_request()
        cfg.consumer = CONSUMER
        cfg.request_type = (gpiod.line_request.DIRECTION_OUTPUT if direction == "out"
                            else gpiod.line_request.DIRECTION_INPUT)
        cfg.flags = 0 if active_high else gpiod.line_request.FLAG_ACTIVE_LOW
        if direction == "out":
            line.request(cfg, 1 if initial_active else 0)
        else:
            line.request(cfg)

        class H(GpioHandle):
            def set_active(self_, active):
                line.set_value(1 if active else 0)

            def is_active(self_):
                return bool(line.get_value())

            def release(self_):
                try: line.release()
                except Exception: pass
        h = H(); h.chip, h.offset, h.spec, h.resolved_by, h.direction = path, offset, spec, how, direction
        return h


class MemoryGpio(GpioBackend):
    """In-memory GPIO for MockFrontend and tests.  ``names`` maps chip: {name: offset};
    anything not in the table fails to resolve (so fallback paths are testable).
    ``inputs`` holds forced input values keyed by (chip, offset)."""

    DEFAULT_NAMES = {
        "/dev/gpiochip0": {"cpu-gp-out1": 41, "PG.06": 41, "cpu-gp-out2": 97, "PN.01": 97,
                           "cpu-gpio": 118, "PP.06": 118},
        "/dev/gpiochip1": {"cpu-gp-in": 13, "PCC.01": 13, "cpu-gpio-direction": 15, "PCC.03": 15},
    }

    def __init__(self, names: Optional[dict] = None, num_lines: int = 164):
        self.names = copy.deepcopy(names if names is not None else self.DEFAULT_NAMES)
        self.num_lines = num_lines
        self.lines: Dict[tuple, "MemoryGpio._H"] = {}
        self.inputs: Dict[tuple, bool] = {}
        self.history: list = []          # (t, chip, offset, active)
        self.fail_set: bool = False      # raise on set_active (fault injection)

    class _H(GpioHandle):
        def __init__(self, owner, key, active_high, initial):
            self._o, self._k = owner, key
            self.active_high = active_high
            self.value = bool(initial)

        def set_active(self, active):
            if self._o.fail_set:
                raise OSError("injected GPIO write failure")
            self.value = bool(active)
            self._o.history.append((time.monotonic(), self.chip, self.offset, self.value))

        def is_active(self):
            if self.direction == "in":
                return bool(self._o.inputs.get(self._k, False))
            return self.value

        def release(self):
            self._o.lines.pop(self._k, None)

    def open_line(self, chip, spec, fallback, direction, active_high, initial_active=False):
        path = chip_path(chip)
        table = self.names.get(path, {})

        def lookup(v):
            if isinstance(v, int):
                if v >= self.num_lines:
                    raise FrontendError(f"offset {v} >= {self.num_lines}")
                return v
            if v not in table:
                raise FrontendError(f"no line named {v!r} on {path}")
            return table[v]
        offset, how = _resolve(lookup, spec, fallback)
        key = (path, offset)
        if key in self.lines:
            raise FrontendError(f"line {path}:{offset} already requested")
        h = MemoryGpio._H(self, key, active_high, initial_active)
        h.chip, h.offset, h.spec, h.resolved_by, h.direction = path, offset, spec, how, direction
        self.lines[key] = h
        return h

    # test helpers
    def level(self, chip, offset) -> Optional[bool]:
        h = self.lines.get((chip_path(chip), offset))
        return None if h is None else h.value

    def set_input(self, chip, offset, active: bool) -> None:
        self.inputs[(chip_path(chip), offset)] = bool(active)


# the front end
# ADC readback
def hpa_temp_c_from_volts(v: float, offset_v: float = 0.5, v_per_c: float = 0.01) -> float:
    """Empower 1189 DB9 pin 3: 10 mV per degree C on a 0.5 V offset, so 0.5 V is
    0 C and 1.0 V is 50 C.  Pure, for the tests."""
    return (float(v) - float(offset_v)) / float(v_per_c)


def hpa_current_a_from_volts(v: float, v_per_a: float = 0.25, offset_v: float = 0.0) -> float:
    """Empower 1189 DB9 pin 2: 25 mV per 100 mA, so 0.075 V is the 0.3 A shutdown
    draw and 0.375 V the 1.5 A quiescent draw with the HPA enabled."""
    return (float(v) - float(offset_v)) / float(v_per_a)


def _import_smbus():
    """smbus2, else the older smbus, else None (the service container has neither
    until the wheel is installed)."""
    try:
        from smbus2 import SMBus
        return SMBus
    except ImportError:
        try:
            from smbus import SMBus
            return SMBus
        except ImportError:
            return None


# One lock per I2C bus: a single-shot ADS1x15 conversion is a config write, a
# wait and a read, and two readers on one chip (the switch readback and the HPA
# monitor) must not interleave those or one reads the other's channel.
_BUS_LOCKS: Dict[str, threading.Lock] = {}
_BUS_LOCKS_GUARD = threading.Lock()


def _bus_lock(name: str) -> threading.Lock:
    with _BUS_LOCKS_GUARD:
        lk = _BUS_LOCKS.get(name)
        if lk is None:
            lk = _BUS_LOCKS[name] = threading.Lock()
        return lk


class AdcReader:
    """Reads one analog channel and returns the node voltage (after any divider)."""
    def read_volts(self) -> float: raise NotImplementedError
    def info(self) -> dict: return {}
    def release(self) -> None: pass


class MemoryAdc(AdcReader):
    """In-memory ADC for the mock front end and the tests.  ``node_v`` is the node
    voltage the next read returns; set it to walk through TX, RX and the dead band."""
    def __init__(self, node_v: float = 0.0):
        self.node_v = float(node_v)

    def read_volts(self) -> float:
        return float(self.node_v)

    def info(self) -> dict:
        return {"transport": "mock", "node_v": self.node_v}


class I2cAds1x15Reader(AdcReader):
    """Single-ended one-shot read of a TI ADS1015 or ADS1115 over Linux i2c-dev.
    Off by default; the register values follow the datasheet but have not been run
    on the bench yet, so verify against a meter before trusting it.  The 16-bit
    scaling works for the ADS1015 too (it left-justifies 12 bits, so the same volts
    come back in coarser steps)."""
    _PGA = ((6.144, 0x0000), (4.096, 0x0200), (2.048, 0x0400),
            (1.024, 0x0600), (0.512, 0x0800), (0.256, 0x0A00))
    _MUX = {0: 0x4000, 1: 0x5000, 2: 0x6000, 3: 0x7000}   # single-ended AIN0..3

    def __init__(self, bus: str = "/dev/i2c-1", address: int = 0x48, channel: int = 0,
                 full_scale_v: float = 4.096, divider: float = 1.0, label: str = "tr_readback",
                 smbus_cls=None):
        self.bus_name = str(bus)
        self.addr = int(address)
        self.channel = int(channel)
        self.divider = float(divider)
        self.label = label                    # names the reader in error text
        self._fs, self._pga_bits = self._pick_pga(float(full_scale_v))
        self._smbus_cls = smbus_cls           # tests inject a fake; None = smbus2/smbus
        self._bus = None
        self._lock = _bus_lock(self.bus_name)

    @classmethod
    def for_tr_readback(cls, cfg: TrReadbackCfg) -> "I2cAds1x15Reader":
        return cls(cfg.bus, cfg.address, cfg.channel, cfg.full_scale_v, cfg.divider)

    def _pick_pga(self, fs: float):
        """The smallest PGA range that still covers ``fs`` (the table runs from the
        widest range down, so the last one that fits is the tightest)."""
        best = self._PGA[0]
        for f, bits in self._PGA:
            if fs <= f + 1e-9:
                best = (f, bits)
        return best

    def _open(self):
        if self._bus is not None:
            return
        SMBus = self._smbus_cls or _import_smbus()
        if SMBus is None:
            raise FrontendError(f"{self.label} i2c needs the smbus2 or smbus module (smbus2 missing)")
        n = self.bus_name
        if isinstance(n, str) and n.startswith("/dev/i2c-"):
            n = int(n.rsplit("-", 1)[-1])
        self._bus = SMBus(n)

    def read_volts(self) -> float:
        self._open()
        mux = self._MUX.get(self.channel)
        if mux is None:
            raise FrontendError(f"{self.label} channel {self.channel} is not 0..3")
        # OS=1 start, MUX, PGA, MODE=1 single-shot, DR=128 SPS, comparator off.
        conf = 0x8000 | mux | self._pga_bits | 0x0100 | 0x0080 | 0x0003
        with self._lock:              # the whole conversion, so another channel cannot cut in
            self._bus.write_i2c_block_data(self.addr, 0x01, [(conf >> 8) & 0xFF, conf & 0xFF])
            time.sleep(0.009)         # 128 SPS conversion plus a little margin
            d = self._bus.read_i2c_block_data(self.addr, 0x00, 2)
        raw = (d[0] << 8) | d[1]
        if raw & 0x8000:
            raw -= 1 << 16
        return raw * (self._fs / 32768.0) * self.divider

    def info(self) -> dict:
        return {"transport": "i2c", "bus": self.bus_name, "address": hex(self.addr),
                "channel": self.channel, "full_scale_v": self._fs, "divider": self.divider}

    def release(self) -> None:
        if self._bus is not None:
            try: self._bus.close()
            except Exception: pass
            self._bus = None


def make_adc(cfg: TrReadbackCfg, mock: bool = False) -> Optional[AdcReader]:
    """None when the readback is off.  A mock front end always gets a MemoryAdc so
    the sim and the tests need no I2C hardware."""
    if not cfg.enabled or cfg.transport == "none":
        return None
    if mock or cfg.transport == "mock":
        return MemoryAdc()
    if cfg.transport == "i2c":
        return I2cAds1x15Reader.for_tr_readback(cfg)
    return None


def make_hpa_adcs(cfg: HpaMonitorCfg, mock: bool = False):
    """(temperature reader, current reader), or None when the monitor is off.  The
    mock pair starts at a cold HPA in shutdown (35 C, 0.3 A) so the simulator shows
    plausible figures."""
    if not cfg.enabled or cfg.transport == "none":
        return None
    if mock or cfg.transport == "mock":
        return (MemoryAdc(cfg.temp_offset_v + 35.0 * cfg.temp_v_per_c),
                MemoryAdc(cfg.current_offset_v + 0.3 * cfg.current_v_per_a))
    if cfg.transport == "i2c":
        return (I2cAds1x15Reader(cfg.bus, cfg.address, cfg.temp_channel, cfg.full_scale_v,
                                 label="hpa_monitor temp"),
                I2cAds1x15Reader(cfg.bus, cfg.address, cfg.current_channel, cfg.full_scale_v,
                                 label="hpa_monitor current"))
    return None


HPA_HOT_C = 85.0      # 1189 datasheet: case maximum, thermal overload starts here
HPA_MAX_A = 12.0      # 1189 datasheet: maximum supply current at 100 W out


HPA_READING_OK, HPA_READING_OFF, HPA_READING_FLOATING, HPA_READING_BAD = "ok", "hpa off", "floating", "bad"
HPA_TEMP_RANGE_C = (-40.0, 125.0)      # outside this the 1189 cannot be reporting a real temperature
HPA_CURRENT_LIMIT_A = 15.0             # above this it cannot be a real supply current
HPA_NEAR_ZERO_A = 0.05                 # below this it is a false ADC sample near 0 V, not a real current


def hpa_reading_quality(temp_v: float, current_v: float, temp_c: float, current_a: float, hpa_on: bool) -> str:
    """Can this reading be believed?  HPA off: the outputs are not valid.  Both inputs
    high and within 0.15 V of each other: nothing is driving them, as with the DB9
    unplugged (about 2.2 V on both).  Otherwise the numbers must be physically possible,
    which also catches a false sample near 0 V from the ADC while keyed."""
    if not hpa_on:
        return HPA_READING_OFF
    if temp_v > 1.5 and abs(temp_v - current_v) < 0.15:
        return HPA_READING_FLOATING
    if not HPA_TEMP_RANGE_C[0] <= temp_c <= HPA_TEMP_RANGE_C[1] or not HPA_NEAR_ZERO_A <= current_a <= HPA_CURRENT_LIMIT_A:
        return HPA_READING_BAD
    return HPA_READING_OK


HPA_QUALITY_TEXT = {HPA_READING_OFF: "HPA off: monitor not valid",
                    HPA_READING_FLOATING: "HPA monitor: inputs floating, check the DB9",
                    HPA_READING_BAD: "HPA monitor: bad reading"}


def hpa_card_text(hm, kind=None) -> tuple:
    """(text, level) for the radio cards from get_config 'hpa_monitor'.  With no
    monitor configured a G40 card says so and any other radio gets "".  Level None
    (grey) when the chip does not read or the HPA is off, 'warm' (amber) for a
    reading that cannot be real, from warn_c or above HPA_MAX_A, 'hot' (red) from
    HPA_HOT_C."""
    if not hm:
        return ("HPA temp/current: not set up (hpa_monitor in rf_frontend.yaml)", None) \
            if kind == "g40" else ("", None)
    if hm.get("state") != HPA_MON_OK or hm.get("hpa_temp_c") is None:
        why = hm.get("reason")
        return "HPA temp/current: not fitted" + (f" ({why})" if why else ""), None
    q = hm.get("quality", HPA_READING_OK)
    if q != HPA_READING_OK:
        return HPA_QUALITY_TEXT.get(q, f"HPA monitor: {q}"), (None if q == HPA_READING_OFF else "warm")
    t, i = float(hm["hpa_temp_c"]), float(hm.get("hpa_current_a") or 0.0)
    txt = f"HPA {t:.1f} °C, {i:.2f} A"
    if t >= HPA_HOT_C:
        return txt, "hot"
    if hm.get("hot") or t >= float(hm.get("warn_c", 70.0)) or i > HPA_MAX_A:
        return txt, "warm"
    return txt, "ok"


def hpa_drop_text(cfg) -> Optional[tuple]:
    """(text, 'hot') for the radio cards from get_config when the PA was dropped (a
    watchdog, a fault, the RX guard) and TX is still running, so what the card says
    is not 'transmitting' alone.  None otherwise."""
    fe = (cfg or {}).get("fe") or {}
    if not (cfg or {}).get("tx_active") or fe.get("pa") or not fe.get("drop"):
        return None
    return f"HPA OFF, TX still running: dropped by {fe['drop']}", "hot"


def hpa_monitor_fail_reason(e: BaseException) -> str:
    """Short reason text for a monitor read that failed: the module, the bus node
    or the chip is missing, else the error itself."""
    txt = str(e)
    if "smbus2 missing" in txt:
        return "smbus2 missing"
    if isinstance(e, FileNotFoundError):
        return "bus missing"
    if isinstance(e, OSError):
        # ENXIO / EREMOTEIO: no ACK at that address, so no chip there
        return "no ADC at that address"
    return f"error: {txt[:60]}"


class RfFrontend:
    """PA enable + TX/RX switch + fault input sequencer.  Thread-safe; every public
    method may be called from the TX thread, the service loop, or the watchdog."""

    def __init__(self, cfg: FrontendConfig, gpio: Optional[GpioBackend] = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 watchdog_poll_s: float = 0.05, heartbeat_thread: bool = True):
        self.cfg = cfg
        self._gpio = gpio
        self._clock, self._sleep = clock, sleep
        self._lock = threading.RLock()
        self._pa: Optional[GpioHandle] = None
        self._tr: Optional[GpioHandle] = None
        self._fault: Optional[GpioHandle] = None
        self._tr_adc: Optional[AdcReader] = None     # T/R switch analog readback, or None
        self._hpa_adcs = None                        # (temp, current) AdcReaders, or None
        self._hpa_cache: Optional[dict] = None       # last hpa_monitor_state(), with "_t"
        self._hpa_logged: Optional[str] = None       # last failure reason logged
        self._dirs: list = []                       # direction lines held for _pa / _tr
        self._keyed = False
        self._tr_tx = False
        self._hold = False
        self._opened = False
        self._deadline: Optional[float] = None      # watchdog deadline while keyed
        self.last_drop: Optional[str] = None        # why the PA last went off other than by a clean unkey
        self._wd_poll = watchdog_poll_s
        self._wd_stop = threading.Event()
        self._wd_thread: Optional[threading.Thread] = None
        self._hb_thread: Optional[threading.Thread] = None
        self._hb_thread_on = heartbeat_thread       # False: tests call heartbeat_step()
        self._hb_level = False                      # last level the heartbeat wrote
        self._hb_last: Optional[float] = None       # time of the last edge
        self.hb_edges = 0
        self.hb_max_gap_ms = 0.0                    # longest gap between edges while keyed
        self.watchdog_trips = 0
        self.fault_trips = 0
        self.last_error: Optional[str] = None
        self.events: deque = deque(maxlen=64)
        self._atexit_registered = False
        # Payload_guard.RxGuard (or None).  pa_enable(True) asks it
        # before every key; tr_switch(False) lets it restore the RX attenuator.
        self.guard = None
        # Live G40 TX attenuation (dB), set by the service so the guard's load
        # power check can read it at key time.  None = unknown (guard fails closed
        # when a load power limit is configured).
        self.tx_atten_getter: Optional[Callable[[], Optional[float]]] = None

    # lifecycle
    @property
    def kind(self) -> str:
        return self.cfg.frontend

    def _event(self, name: str, **detail) -> None:
        self.events.append({"t": self._clock(), "event": name, **detail})

    def open(self) -> "RfFrontend":
        with self._lock:
            if self._opened:
                return self
            if self._gpio is None:
                self._gpio = GpiodBackend()
            pa, tr, fi = self.cfg.pa_enable, self.cfg.tr_switch, self.cfg.pa_fault_in
            try:
                for sec in ((pa, tr) if tr.mode == "gpio" else (pa,)):
                    if parse_line_spec(sec.dir_line)[0] != "none" or \
                            parse_line_spec(sec.dir_fallback_line)[0] != "none":
                        # translator direction = output before the line itself is driven
                        self._dirs.append(self._gpio.open_line(
                            sec.dir_chip, sec.dir_line, sec.dir_fallback_line, "out",
                            True, initial_active=True))
                self._pa = self._gpio.open_line(pa.chip, pa.line, pa.fallback_line, "out",
                                                pa.active_high, initial_active=False)
                if tr.mode == "gpio":
                    # line is driven as a raw level: tx_level says which level is TX
                    self._tr = self._gpio.open_line(tr.chip, tr.line, tr.fallback_line, "out",
                                                    True, initial_active=self._tr_level(False))
                if fi.enabled:
                    # "active" == fault asserted
                    self._fault = self._gpio.open_line(fi.chip, fi.line, fi.fallback_line, "in",
                                                       active_high=not fi.active_low)
                if self.cfg.tr_readback.enabled:
                    # the I2C bus is opened lazily on the first read, so a missing
                    # ADC does not stop the front end opening (it fails at key time)
                    self._tr_adc = make_adc(self.cfg.tr_readback,
                                            mock=(self.cfg.frontend == "mock"))
                if self.cfg.hpa_monitor.enabled:
                    # display only: a missing chip reads as "not fitted", never a refusal
                    self._hpa_adcs = make_hpa_adcs(self.cfg.hpa_monitor,
                                                   mock=(self.cfg.frontend == "mock"))
                    self._hpa_cache = None
            except Exception as e:
                self._release_lines()
                self.last_error = f"open: {e}"
                raise FrontendError(f"front-end open failed: {e}") from e
            self._keyed = False
            self._tr_tx = False
            self._opened = True
            self._event("open", pa=self._pa.info(), tr=(self._tr.info() if self._tr else tr.mode),
                        fault=(self._fault.info() if self._fault else "disabled"))
            if not self._atexit_registered:
                atexit.register(self.safe, "atexit")
                self._atexit_registered = True
            if self.cfg.watchdog_ms > 0 and self._wd_thread is None:
                self._wd_stop.clear()
                self._wd_thread = threading.Thread(target=self._watchdog_loop,
                                                   name="rf-frontend-wd", daemon=True)
                self._wd_thread.start()
            if self.cfg.pa_enable.mode == "heartbeat" and self._hb_thread_on \
                    and self._hb_thread is None:
                self._wd_stop.clear()
                self._hb_thread = threading.Thread(target=self._heartbeat_loop,
                                                   name="rf-frontend-hb", daemon=True)
                self._hb_thread.start()
            return self

    def _release_lines(self) -> None:
        for h in (self._pa, self._tr, self._fault, *self._dirs):
            if h is not None:
                try: h.release()
                except Exception: pass
        self._pa = self._tr = self._fault = None
        self._dirs = []
        if self._tr_adc is not None:
            try: self._tr_adc.release()
            except Exception: pass
            self._tr_adc = None
        if self._hpa_adcs is not None:
            for r in self._hpa_adcs:
                try: r.release()
                except Exception: pass
            self._hpa_adcs = None
            self._hpa_cache = None

    def close(self) -> None:
        with self._lock:
            self.safe("close")
            self._wd_stop.set()
            self._release_lines()
            self._opened = False
        for t in (self._wd_thread, self._hb_thread):
            if t is not None and t is not threading.current_thread():
                t.join(timeout=1.0)
        self._wd_thread = None
        self._hb_thread = None

    def reload(self, cfg: FrontendConfig) -> None:
        """Re-map lines from a new config.  PA is forced LOW and all lines released
        before the new mapping is requested, so a pin can be moved while the service runs."""
        with self._lock:
            self.safe("reload")
            self._wd_stop.set()
            self._release_lines()
            self._opened = False
            for t in (self._wd_thread, self._hb_thread):
                if t is not None and t is not threading.current_thread():
                    t.join(timeout=1.0)
            self._wd_thread = None
            self._hb_thread = None
            self.cfg = cfg
            self.open()

    def safe(self, reason: str = "safe") -> None:
        """Force the PA LOW and the switch to RX.  Never raises."""
        with self._lock:
            if self._pa is not None:
                try:
                    self._pa.set_active(False)
                except Exception as e:
                    self.last_error = f"safe/pa: {e}"
            if self._keyed:
                self.last_drop = reason              # kept until the PA is next keyed
            self._keyed = False
            self._deadline = None
            self._hb_level = False
            self._hb_last = None
            if self._tr is not None:
                try:
                    self._tr.set_active(self._tr_level(False))
                except Exception as e:
                    self.last_error = f"safe/tr: {e}"
            self._tr_tx = False
            self._hold = False
            if self.guard is not None:              # Disarm, keep atten high
                try: self.guard.on_safe(reason)
                except Exception: pass
            self._event("safe", reason=reason)

    # primitives
    def _tr_level(self, tx: bool) -> bool:
        lvl = self.cfg.tr_switch.tx_level
        return bool(lvl) if tx else not bool(lvl)

    def fault_active(self) -> Optional[bool]:
        if self._fault is None:
            return None
        try:
            return bool(self._fault.is_active())
        except Exception as e:
            self.last_error = f"fault read: {e}"
            return True                       # unreadable fault input = assume fault

    def classify_tr(self, v: float) -> str:
        """Which window a readback voltage lands in: TR_TX, TR_RX, or TR_BAD for a
        level in neither (floating, half-driven, a dead level shifter)."""
        lo, hi = self.cfg.tr_readback.tx_window_v
        if lo <= v <= hi:
            return TR_TX
        lo, hi = self.cfg.tr_readback.rx_window_v
        if lo <= v <= hi:
            return TR_RX
        return TR_BAD

    def tr_readback_state(self):
        """(volts, state) from the ADC, or None when the readback is not fitted.
        An unreadable ADC returns (None, TR_BAD), same idea as the fault input."""
        if self._tr_adc is None:
            return None
        try:
            v = float(self._tr_adc.read_volts())
        except Exception as e:
            self.last_error = f"tr_readback: {e}"
            return (None, TR_BAD)
        return (v, self.classify_tr(v))

    def hpa_monitor_state(self, force: bool = False) -> Optional[dict]:
        """HPA temperature and supply current from the DB9 monitor outputs, or
        None when the monitor is not configured.  Reads the chip at most every
        poll_s (the cached reading comes back in between, with its age).  A
        missing chip, bus or module gives state "not fitted" with the reason and
        no figures.  Display only: never raises and never touches last_error."""
        if self._hpa_adcs is None:
            return None
        cfg = self.cfg.hpa_monitor
        now = self._clock()
        c = self._hpa_cache
        on = bool(self._keyed)                # a key or unkey makes the last reading out of date
        if c is not None and not force and (now - c["_t"]) < cfg.poll_s and c.get("_on") == on:
            out = dict(c)
            out["age_s"] = round(max(0.0, now - out.pop("_t")), 1)
            out.pop("_on", None)
            return out
        d = {"enabled": True, "state": HPA_MON_OK, "hpa_temp_c": None, "hpa_current_a": None,
             "hpa_temp_v": None, "hpa_current_v": None, "warn_c": cfg.warn_c, "hot": False,
             "reason": None}
        try:
            tv = float(self._hpa_adcs[0].read_volts())
            iv = float(self._hpa_adcs[1].read_volts())
        except Exception as e:
            d["state"] = HPA_MON_NOT_FITTED
            d["reason"] = hpa_monitor_fail_reason(e)
            if d["reason"] != self._hpa_logged:
                log.warning("rf_frontend: HPA monitor %s (%s)", d["reason"], e)
                self._hpa_logged = d["reason"]
        else:
            if self._hpa_logged is not None:
                log.info("rf_frontend: HPA monitor reading again")
                self._hpa_logged = None
            t_c = hpa_temp_c_from_volts(tv, cfg.temp_offset_v, cfg.temp_v_per_c)
            i_a = hpa_current_a_from_volts(iv, cfg.current_v_per_a, cfg.current_offset_v)
            q = hpa_reading_quality(tv, iv, t_c, i_a, bool(self._keyed) or cfg.valid_when_off)
            d.update({"hpa_temp_c": round(t_c, 1), "hpa_current_a": round(i_a, 2),
                      "hpa_temp_v": round(tv, 3), "hpa_current_v": round(iv, 3),
                      "quality": q, "hot": bool(q == HPA_READING_OK and t_c >= cfg.warn_c)})
        self._hpa_cache = dict(d, _t=now, _on=on)
        d["age_s"] = 0.0
        return d

    def require_tr_readback(self, want_tx: bool) -> None:
        """Confirm the switch readback is in the commanded window before keying.
        A half-rail or unreadable level raises FrontendFault; the wrong window
        raises InterlockError.  Does nothing when the readback is not fitted."""
        if self._tr_adc is None:
            return
        s = self.cfg.tr_readback.settle_ms
        if s > 0:
            self._sleep(s / 1000.0)
        v, state = self.tr_readback_state()
        want = TR_TX if want_tx else TR_RX
        if state == TR_BAD:
            vtxt = "unreadable" if v is None else f"{v:.2f} V"
            raise FrontendFault(f"T/R readback {vtxt} is outside both windows: keying refused")
        if state != want:
            raise InterlockError(f"T/R readback reads {state} ({v:.2f} V), wanted {want}")

    def pa_enable(self, on: bool, manual: bool = False) -> None:
        """manual=True (operator bench control): the PA stays keyed until told
        otherwise; no TX loop feeds the watchdog, so it is not armed.  The fault
        input still drops it."""
        with self._lock:
            self._require_open()
            on = bool(on)
            if on:
                if self.fault_active():
                    self.safe("fault")
                    self.fault_trips += 1
                    raise FrontendFault("PA fault input asserted: keying refused")
                if self.cfg.tr_switch.mode == "gpio" and not self._tr_tx:
                    raise InterlockError("switch is in RX: PA not keyed")
                # analog confirm of the real switch control voltage (if fitted).
                # Works in proxy mode too, where the digital interlock above does not.
                self.require_tr_readback(True)
                if self.guard is not None:          # payload guard (PA still off here)
                    try:
                        self.guard.before_key(self)
                    except Exception as e:
                        self.last_error = f"guard: {e}"
                        self._event("guard_refused", reason=str(e)[:120])
                        raise InterlockError(f"RX protection: {e}") from e
            try:
                self._pa.set_active(on)
            except Exception as e:
                self.last_error = f"pa: {e}"
                self.safe("pa write failed")
                raise FrontendError(f"PA line write failed: {e}") from e
            self._keyed = on
            if on:
                self.last_drop = None
            self._hb_level = on
            self._hb_last = self._clock() if on else None
            self._deadline = (self._wd_deadline() if on and not manual else None)
            self._event("pa", on=on, manual=manual)

    def tr_switch(self, tx: bool) -> None:
        with self._lock:
            self._require_open()
            tx = bool(tx)
            if tx:
                self._guard_before_switch_tx()
            if self.cfg.tr_switch.mode == "proxy":
                self._tr_tx = tx             # FPGA TR_N drives the switch; track only
                self._event("tr", tx=tx, mode="proxy")
                if not tx:
                    self._guard_after_rx()
                return
            if not tx and self._keyed:
                raise InterlockError("PA is keyed: switch not returned to RX")
            try:
                self._tr.set_active(self._tr_level(tx))
            except Exception as e:
                self.last_error = f"tr: {e}"
                self.safe("tr write failed")
                raise FrontendError(f"switch line write failed: {e}") from e
            self._tr_tx = tx
            self._event("tr", tx=tx, mode="gpio")
            if not tx:
                self._guard_after_rx()

    def _guard_before_switch_tx(self) -> None:
        """RX attenuator to its protect value before the switch moves to TX.  A failure
        is noted, not raised: pa_enable's before_key is what refuses the key."""
        if self.guard is None:
            return
        try:
            err = self.guard.before_switch_tx()
        except Exception as e:
            err = str(e)
        if err:
            self.last_error = f"guard before TX: {err}"[:120]

    def _guard_after_rx(self) -> None:
        """Switch back in RX with the PA off: restore the operator's
        RX attenuation.  Never raises (a failed restore leaves it high = safe)."""
        if self.guard is None:
            return
        try:
            err = self.guard.after_rx(self)
        except Exception as e:
            err = str(e)
        if err:
            self.last_error = f"guard restore: {err}"[:120]
            self._event("guard_restore", err=err[:120])

    # sequences used by the TX path
    def set_hold(self, hold: bool) -> None:
        """Continuous TX: keep the PA keyed between sends, no hang on stop."""
        self._hold = bool(hold)

    def sequence_tx_on(self) -> None:
        with self._lock:
            self._require_open()
            if self._keyed:
                self._deadline = self._wd_deadline()
                return
            try:
                self.tr_switch(True)
                self._sleep(self.cfg.tr_switch.settle_ms / 1000.0)
                self.pa_enable(True)
                self._sleep(self.cfg.pa_enable.settle_ms / 1000.0)
            except Exception:
                self.safe("tx_on failed")
                raise
            self._event("tx_on")

    def sequence_tx_off(self) -> None:
        with self._lock:
            if not self._opened or (not self._keyed and not self._tr_tx):
                return
            try:
                if self._keyed and not self._hold and self.cfg.pa_enable.hang_ms > 0:
                    self._sleep(self.cfg.pa_enable.hang_ms / 1000.0)
                self.pa_enable(False)
                self._sleep(self.cfg.pa_enable.settle_ms / 1000.0)
                self.tr_switch(False)
            except Exception:
                self.safe("tx_off failed")
                raise
            self._hold = False                   # a stopped run no longer holds
            self._event("tx_off")

    def tx_tick(self, busy_s: float = 0.0) -> None:
        """Feed the watchdog from the TX path.  ``busy_s`` = how long the send just
        handed to the radio will keep it busy (a long single-shot waveform must not
        trip the watchdog mid-burst)."""
        if self._keyed:
            self._deadline = self._wd_deadline(busy_s)

    # watchdog
    def _wd_deadline(self, busy_s: float = 0.0) -> Optional[float]:
        """None when watchdog_ms == 0 (disabled)."""
        wd = float(self.cfg.watchdog_ms)
        if wd <= 0:
            return None
        return self._clock() + max(0.0, float(busy_s)) + wd / 1000.0

    def watchdog_check(self) -> Optional[str]:
        """One watchdog evaluation (the thread calls this; tests call it directly).
        Returns the trip reason or None."""
        with self._lock:
            if not self._keyed:
                return None
            if self.fault_active():
                self.fault_trips += 1
                self.safe("fault while keyed")
                return "fault"
            if self._deadline is not None and self._clock() > self._deadline:
                self.watchdog_trips += 1
                self.safe("watchdog")
                return "watchdog"
            return None

    def _watchdog_loop(self) -> None:
        while not self._wd_stop.wait(self._wd_poll):
            try:
                r = self.watchdog_check()
                if r:
                    log.warning("rf_frontend: PA dropped by %s", r)
            except Exception as e:             # pragma: no cover
                self.last_error = f"watchdog: {e}"

    # heartbeat (pa_enable.mode heartbeat)
    def heartbeat_step(self) -> bool:
        """Write the next heartbeat edge if keyed.  Called by the heartbeat thread
        (tests call it directly).  Under the lock, so a safe() in between can never
        be followed by a stray edge.  Returns True when an edge was written."""
        with self._lock:
            if not self._keyed or self._pa is None or self.cfg.pa_enable.mode != "heartbeat":
                return False
            now = self._clock()
            if self._hb_last is not None:
                self.hb_max_gap_ms = max(self.hb_max_gap_ms, (now - self._hb_last) * 1000.0)
            nxt = not self._hb_level
            try:
                self._pa.set_active(nxt)
            except Exception as e:
                self.last_error = f"heartbeat: {e}"
                self.safe("heartbeat write failed")
                return False
            self._hb_level = nxt
            self._hb_last = now
            self.hb_edges += 1
            return True

    def _heartbeat_loop(self) -> None:
        half = 0.5 / max(1.0, float(self.cfg.pa_enable.heartbeat_hz))
        while not self._wd_stop.wait(half):
            try:
                self.heartbeat_step()
            except Exception as e:             # pragma: no cover
                self.last_error = f"heartbeat: {e}"

    # introspection
    def _require_open(self) -> None:
        if not self._opened:
            raise FrontendError("front-end not open")

    def status(self, full: bool = False) -> dict:
        """Compact by default: it rides in a CMD_ACK datagram (hard cap 1400 B on the
        wire), so no nested config echoes and no event log.  full=True adds the
        configured specs and the last events for local diagnostics."""
        with self._lock:
            now = self._clock()

            def li(h, section):
                d = {"line": section.line}
                if h is not None:
                    d.update({"chip": h.chip, "offset": h.offset, "by": h.resolved_by})
                    if parse_line_spec(getattr(section, "dir_line", None))[0] != "none" or \
                            parse_line_spec(getattr(section, "dir_fallback_line", None))[0] != "none":
                        dh = next((x for x in self._dirs
                                   if x.spec in (section.dir_line, section.dir_fallback_line)), None)
                        d["dir"] = None if dh is None else f"{dh.chip.rsplit('/', 1)[-1]}:{dh.offset}"
                    try:
                        d["level"] = bool(h.is_active())
                    except Exception as e:
                        d["level"] = None; d["err"] = str(e)[:60]
                if full:
                    d["configured"] = {"chip": section.chip, "line": section.line,
                                       "fallback_line": section.fallback_line}
                return d
            st = {
                "frontend": self.cfg.frontend,
                "open": self._opened,
                "config_source": self.cfg.source,
                "last_drop": self.last_drop,
                "pa": {"keyed": self._keyed, "active_high": self.cfg.pa_enable.active_high,
                       **({"mode": "heartbeat", "heartbeat_hz": self.cfg.pa_enable.heartbeat_hz,
                           "hb_running": bool(self._keyed and self._hb_thread is not None
                                              and self._hb_thread.is_alive()),
                           "hb_edges": self.hb_edges,
                           "hb_max_gap_ms": round(self.hb_max_gap_ms, 1)}
                          if self.cfg.pa_enable.mode == "heartbeat" else {}),
                       "settle_ms": self.cfg.pa_enable.settle_ms,
                       "hang_ms": self.cfg.pa_enable.hang_ms, **li(self._pa, self.cfg.pa_enable)},
                "tr": {"mode": self.cfg.tr_switch.mode, "tx": self._tr_tx,
                       "tx_level": self.cfg.tr_switch.tx_level,
                       "settle_ms": self.cfg.tr_switch.settle_ms,
                       **(li(self._tr, self.cfg.tr_switch) if self.cfg.tr_switch.mode == "gpio" else {})},
                "fault": {"enabled": self.cfg.pa_fault_in.enabled,
                          "active": self.fault_active(),
                          "active_low": self.cfg.pa_fault_in.active_low,
                          **(li(self._fault, self.cfg.pa_fault_in) if self._fault else {})},
                "hold": self._hold,
                "watchdog_ms": self.cfg.watchdog_ms,
                "watchdog_trips": self.watchdog_trips,
                "fault_trips": self.fault_trips,
                "watchdog_remaining_ms": (None if self._deadline is None
                                          else round((self._deadline - now) * 1000.0, 1)),
                "last_error": (self.last_error[:80] if self.last_error else None),
            }
            if self.cfg.tr_readback.enabled:
                rb = self.tr_readback_state()
                if rb is None:
                    st["tr_readback"] = {"enabled": True}
                else:
                    v, state = rb
                    st["tr_readback"] = {"enabled": True, "state": state,
                                         "volts": (None if v is None else round(v, 3))}
            if self.cfg.hpa_monitor.enabled:
                hm = self.hpa_monitor_state()
                st["hpa_monitor"] = hm if hm is not None else {"enabled": True}
            if full:
                st["events"] = list(self.events)[-8:]
            return st


class MockFrontend(RfFrontend):
    """Same sequencing code over an in-memory GPIO; every call is recorded with a
    timestamp in ``calls`` (and the line history in ``gpio.history``)."""

    def __init__(self, cfg: Optional[FrontendConfig] = None, gpio: Optional[MemoryGpio] = None,
                 **kw):
        if cfg is None:
            cfg = FrontendConfig(frontend="mock")
        elif cfg.frontend == "none":
            cfg = copy.deepcopy(cfg); cfg.frontend = "mock"
        self.gpio = gpio if gpio is not None else MemoryGpio()
        super().__init__(cfg, gpio=self.gpio, **kw)
        self.calls: list = []

    def _rec(self, name, *a):
        self.calls.append((self._clock(), name) + tuple(a))

    def pa_enable(self, on, manual=False):
        self._rec("pa_enable", bool(on)); super().pa_enable(on, manual=manual)
        self._hpa_sim_key(bool(on))
    def tr_switch(self, tx):      self._rec("tr_switch", bool(tx)); super().tr_switch(tx)
    def sequence_tx_on(self):     self._rec("sequence_tx_on");      super().sequence_tx_on()
    def sequence_tx_off(self):    self._rec("sequence_tx_off");     super().sequence_tx_off()
    def safe(self, reason="safe"):
        self._rec("safe", reason); super().safe(reason)
        self._hpa_sim_key(False)

    @property
    def pa_level(self) -> Optional[bool]:
        return None if self._pa is None else self._pa.value

    @property
    def tr_level(self) -> Optional[bool]:
        return None if self._tr is None else self._tr.value

    def set_tr_volts(self, v: float) -> None:
        """Set the node voltage the mock ADC returns (tr_readback must be enabled)."""
        if not isinstance(self._tr_adc, MemoryAdc):
            raise RuntimeError("tr_readback is not enabled on this mock front end")
        self._tr_adc.node_v = float(v)

    def set_hpa_volts(self, temp_v: Optional[float] = None, current_v: Optional[float] = None) -> None:
        """Set the raw volts the mock HPA monitor returns (hpa_monitor must be
        enabled).  Pins the current, so keying no longer moves it."""
        if self._hpa_adcs is None:
            raise RuntimeError("hpa_monitor is not enabled on this mock front end")
        if temp_v is not None:
            self._hpa_adcs[0].node_v = float(temp_v)
        if current_v is not None:
            self._hpa_adcs[1].node_v = float(current_v)
            self._hpa_follow_key = False
        self._hpa_cache = None

    def set_hpa(self, temp_c: Optional[float] = None, current_a: Optional[float] = None) -> None:
        """Same, in engineering units through the configured scales."""
        cfg = self.cfg.hpa_monitor
        self.set_hpa_volts(
            None if temp_c is None else cfg.temp_offset_v + float(temp_c) * cfg.temp_v_per_c,
            None if current_a is None else cfg.current_offset_v + float(current_a) * cfg.current_v_per_a)

    def _hpa_sim_key(self, on: bool) -> None:
        """Simulator: the 1189 draws about 0.3 A in shutdown and 1.5 A quiescent
        once enabled, so the mock current follows the key unless a test pinned it."""
        if self._hpa_adcs is None or not getattr(self, "_hpa_follow_key", True):
            return
        cfg = self.cfg.hpa_monitor
        self._hpa_adcs[1].node_v = cfg.current_offset_v + (1.5 if on else 0.3) * cfg.current_v_per_a
        self._hpa_cache = None


def make_frontend(cfg: FrontendConfig, **kw) -> Optional[RfFrontend]:
    """None for frontend: none; an opened RfFrontend/MockFrontend otherwise."""
    if cfg.frontend == "none":
        return None
    if cfg.frontend == "mock":
        return MockFrontend(cfg, **kw).open()
    return RfFrontend(cfg, **kw).open()
