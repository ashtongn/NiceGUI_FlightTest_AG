"""
gps_antenna.py: G40 GPS antenna 3.3 V bias (ant-bias-en) + GNSS sky status.

Two small, best-effort pieces for the GPSDO clock row in the GUI:

1. The 3.3 V antenna bias.  On the Matchstiq G40 (JP6 / Epiq BSP 1.3.0) the
   active-antenna LNA supply is switched by `ant-bias-en` = gpiochip2 line 4,
   which is channel 4 of a PCA9685 PWM chip on i2c-1 @ 0x44 (kernel driver
   pca9685-pwm), active low: pin low = bias on.  The boot leaves it off, so an
   active antenna (u-blox ANN-MB) sees no satellites until it is enabled.

   Readback reads the four channel-4 registers (LED4_ON_L/H, LED4_OFF_L/H =
   0x16..0x19) over /dev/i2c-1 without requesting the GPIO line (gpioget would
   re-request it as an input):
       ON_H  bit4 (0x10) set: pin high, bias off
       OFF_H bit4 set, or all four 0: pin low, bias on
       anything else (a PWM duty): unknown (None)

   Write = the register sequence the kernel driver's gpio_set() uses
   (OFF_L=0, OFF_H=0, ON_H=0x10 for high / 0 for low), plus ON_L=0.  Not gpioset: this
   kernel's pca9685 gpio_free() drives the pin low when the line is released,
   so `gpioset --active-low ...=0` (bias off) would turn the bias back on the
   moment gpioset exits.  A direct register write has no request/release, so
   both states stick.  Before any write the code checks that the PCA9685 on i2c-1
   0x44 owns a gpiochip whose line 4 is named "ant-bias-en" and that nobody
   holds that line; otherwise the write is refused.  The dpmg_svc container
   (privileged, root) sees /dev/i2c-1 and /dev/gpiochip2 but has no
   gpioset/i2cget binaries: this module needs neither.

   The level is lost at G40 reboot / power cycle (the boot leaves it off)
   unless the epiq-gps-ant-bias.service boot unit is installed on the host.

2. GNSS sky status from gpsd (gpsd 3.22 on the G40 host, 0.0.0.0:2947; the
   service runs with --network host so 127.0.0.1:2947 reaches it).  One
   ?WATCH + ?POLL round trip, short timeout, never raises.  1PPS ticks
   without a fix (the LEA-M8F free-runs), so satellites / fix are the real
   "the antenna works" signal.

SimGpsAntenna fakes both for --simulate so the GUI can be tested offline.

Stdlib only.
"""

from __future__ import annotations

import ctypes
import fcntl
import json
import os
import socket
import statistics
import time

# PCA9685 / ant-bias-en

I2C_BUS = 1
PCA_ADDR = 0x44
PCA_CH = 4
LINE_NAME = "ant-bias-en"
LED_FULL = 0x10                       # bit4 of LEDn_ON_H / LEDn_OFF_H


def led_regs(ch: int = PCA_CH):
    """(ON_L, ON_H, OFF_L, OFF_H) register addresses of PCA9685 channel ch."""
    base = 0x06 + 4 * int(ch)
    return base, base + 1, base + 2, base + 3


def decode_bias(on_l: int, on_h: int, off_l: int, off_h: int):
    """PCA9685 channel registers: antenna bias state (ant-bias-en is
    active-low).  True = bias ON (pin LOW), False = bias OFF (pin HIGH),
    None = a PWM duty / not a plain GPIO level."""
    if off_h & LED_FULL:              # full-OFF has the highest precedence
        return True
    if on_h & LED_FULL:
        return False
    if (on_l | on_h | off_l | off_h) == 0:
        return True
    return None


def bias_write_seq(enable: bool, ch: int = PCA_CH):
    """[(reg, val), ...] for bias ON/OFF: the kernel driver's gpio_set() order
    (clear both OFF regs, then set / clear full-ON), plus ON_L = 0 first so a
    stale PWM ON count cannot leave the pin toggling.  Bias ON = all four 0
    (the state gpioset leaves on the G40), bias OFF = ON_H full-on only."""
    on_l, on_h, off_l, off_h = led_regs(ch)
    pin_high = not enable             # active-low: bias ON = pin LOW
    return [(on_l, 0), (off_l, 0), (off_h, 0), (on_h, LED_FULL if pin_high else 0)]


# Linux i2c-dev I2C_RDWR (combined transfers, adapter-locked: the kernel
# driver cannot move the register pointer between the write and the read).
_I2C_RDWR = 0x0707
_I2C_M_RD = 0x0001


class _I2cMsg(ctypes.Structure):
    _fields_ = [("addr", ctypes.c_uint16), ("flags", ctypes.c_uint16),
                ("len", ctypes.c_uint16), ("buf", ctypes.POINTER(ctypes.c_uint8))]


class _I2cRdwr(ctypes.Structure):
    _fields_ = [("msgs", ctypes.POINTER(_I2cMsg)), ("nmsgs", ctypes.c_uint32)]


class I2cDev:
    """Minimal /dev/i2c-N register access via I2C_RDWR (no I2C_SLAVE claim,
    so it works while pca9685-pwm is bound to the address)."""

    def __init__(self, bus: int = I2C_BUS, addr: int = PCA_ADDR, dev: str | None = None):
        self.path = dev or f"/dev/i2c-{int(bus)}"
        self.addr = int(addr)
        self.fd = os.open(self.path, os.O_RDWR)

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def _xfer(self, msgs):
        arr = (_I2cMsg * len(msgs))(*msgs)
        fcntl.ioctl(self.fd, _I2C_RDWR, _I2cRdwr(arr, len(msgs)))

    def read_reg(self, reg: int) -> int:
        wb = (ctypes.c_uint8 * 1)(reg & 0xFF)
        rb = (ctypes.c_uint8 * 1)()
        self._xfer([_I2cMsg(self.addr, 0, 1, wb), _I2cMsg(self.addr, _I2C_M_RD, 1, rb)])
        return int(rb[0])

    def write_reg(self, reg: int, val: int) -> None:
        wb = (ctypes.c_uint8 * 2)(reg & 0xFF, val & 0xFF)
        self._xfer([_I2cMsg(self.addr, 0, 2, wb)])


def _raw_str(regs) -> str:
    return " ".join(f"{v:02x}" for v in regs)


def read_bias(i2c_factory=None) -> dict:
    """{"ant_bias": True|False|None, "raw": "on_l on_h off_l off_h"} or
    {"ant_bias": None, "err": ...}.  Never raises."""
    try:
        with (i2c_factory or I2cDev)() as d:
            regs = [d.read_reg(r) for r in led_regs()]
        return {"ant_bias": decode_bias(*regs), "raw": _raw_str(regs)}
    except Exception as e:
        return {"ant_bias": None, "err": f"bias read: {e}"[:160]}


def _line_check(sys_root: str = "/sys") -> str | None:
    """None if the PCA9685 on i2c-1 0x44 exposes a gpiochip whose line 4 is
    "ant-bias-en" and is not held by anyone; else the reason to refuse."""
    dev = os.path.join(sys_root, "bus", "i2c", "devices", f"{I2C_BUS}-{PCA_ADDR:04x}")
    try:
        chips = [n for n in os.listdir(dev) if n.startswith("gpiochip")]
    except OSError:
        return f"no PCA9685 at i2c-{I2C_BUS} 0x{PCA_ADDR:02x}"
    if not chips:
        return f"i2c-{I2C_BUS} 0x{PCA_ADDR:02x} exposes no gpiochip"
    chip = "/dev/" + sorted(chips)[0]
    try:
        import gpiod                                   # v2 bindings (dpmg_svc image)
        with gpiod.Chip(chip) as c:
            info = c.get_line_info(PCA_CH)
            name, used = info.name, info.used
    except ImportError:
        return "python gpiod not available to verify the line name"
    except Exception as e:
        return f"line info {chip}:{PCA_CH}: {e}"[:160]
    if name != LINE_NAME:
        return f"{chip} line {PCA_CH} is {name!r}, not {LINE_NAME!r}"
    if used:
        return f"{LINE_NAME} is held by another consumer"
    return None


def set_bias(enable: bool, i2c_factory=None, line_check=None) -> dict:
    """Switch the 3.3 V antenna bias and read it back.  Returns the read_bias()
    dict plus ok/err.  Never raises."""
    try:
        why = (line_check or _line_check)()
    except Exception as e:
        why = f"line check: {e}"
    if why:
        out = read_bias(i2c_factory)
        out.update(ok=False, err=f"refused: {why}"[:200])
        return out
    try:
        with (i2c_factory or I2cDev)() as d:
            for reg, val in bias_write_seq(bool(enable)):
                d.write_reg(reg, val)
    except Exception as e:
        out = read_bias(i2c_factory)
        out.update(ok=False, err=f"bias write: {e}"[:200])
        return out
    out = read_bias(i2c_factory)
    out["ok"] = out.get("ant_bias") is bool(enable)
    if not out["ok"] and "err" not in out:
        out["err"] = "readback does not match the request"
    return out


# gpsd

GPSD_DEFAULT = os.environ.get("DPMG_GPSD", "127.0.0.1:2947")
_FIX_NAMES = {0: "none", 1: "none", 2: "2D", 3: "3D"}


def parse_gpsd(lines) -> dict | None:
    """gpsd JSON lines (POLL, or streamed TPV / SKY): compact gnss dict, or
    None if no TPV/SKY was seen.  Keys: fix_mode (0-3), fix ("none"/"2D"/"3D"),
    sats_visible (heard: C/N0 > 0), sats_used, cno_max, cno_median (dB-Hz),
    time_utc."""
    tpv = sky = None
    for ln in lines:
        if isinstance(ln, (bytes, bytearray)):
            ln = ln.decode("utf-8", "replace")
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            m = json.loads(ln)
        except ValueError:
            continue
        cls = m.get("class")
        if cls == "POLL":
            t = m.get("tpv") or []
            s = [x for x in (m.get("sky") or []) if x.get("satellites") is not None] \
                or (m.get("sky") or [])
            if t:
                tpv = t[-1]
            if s:
                sky = s[-1]
        elif cls == "TPV":
            tpv = m
        elif cls == "SKY":
            # gpsd 3.2x also emits DOP-only SKY reports; keep the one with sats
            if m.get("satellites") is not None or sky is None:
                sky = m
    if tpv is None and sky is None:
        return None
    mode = int((tpv or {}).get("mode", 0) or 0)
    sats = (sky or {}).get("satellites") or []
    cno = [float(s["ss"]) for s in sats
           if isinstance(s.get("ss"), (int, float)) and s["ss"] > 0]
    if sats:
        visible = len(cno) if any("ss" in s for s in sats) else len(sats)
        used = sum(1 for s in sats if s.get("used"))
    else:
        visible = int((sky or {}).get("nSat", 0) or 0)
        used = int((sky or {}).get("uSat", 0) or 0)
    return {"fix_mode": mode, "fix": _FIX_NAMES.get(mode, "?"),
            "sats_visible": visible, "sats_used": used,
            "cno_max": round(max(cno), 1) if cno else None,
            "cno_median": round(statistics.median(cno), 1) if cno else None,
            "time_utc": (tpv or {}).get("time") or (sky or {}).get("time")}


def query_gpsd(addr: str = GPSD_DEFAULT, timeout: float = 1.5) -> dict | None:
    """One WATCH + POLL round trip to gpsd: parse_gpsd() dict, or None if
    gpsd is unreachable / silent.  Never raises."""
    deadline = time.monotonic() + float(timeout)
    try:
        if addr.startswith("/"):
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            target = addr
        else:
            host, _, port = addr.rpartition(":")
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            target = (host or "127.0.0.1", int(port or 2947))
        s.settimeout(max(0.1, float(timeout)))
        with s:
            s.connect(target)
            s.sendall(b'?WATCH={"enable":true,"json":true};\n?POLL;\n')
            buf, lines = b"", []
            while time.monotonic() < deadline:
                s.settimeout(max(0.05, deadline - time.monotonic()))
                try:
                    chunk = s.recv(8192)
                except socket.timeout:
                    break
                if not chunk:
                    break
                buf += chunk
                *done, buf = buf.split(b"\n")
                lines += done
                if any(b'"class":"POLL"' in ln for ln in done):
                    break
                if len(lines) > 400:
                    break
            return parse_gpsd(lines)
    except Exception:
        return None


# simulator

class SimGpsAntenna:
    """--simulate stand-in: the bias defaults off (as the G40 boots); ~20 s
    after it is enabled the fake receiver gets a 3D fix, satellites ramp up,
    and gpsdo_locked() goes True ~25 s after the fix.

    bias_on=True starts with the bias on (the X40, where the ZynqMP turns it on at
    boot). no_fix=True (or DPMG_SIM_NO_GPS_FIX=1) keeps the receiver at one weak
    satellite and no fix while the GPSDO still locks to the free running 1 PPS, as
    G40-0001 did on 8 November."""

    FIX_AFTER_S = 20.0
    LOCK_AFTER_FIX_S = 25.0

    def __init__(self, clock=time.monotonic, bias_on=False, no_fix=None):
        self._clock = clock
        self.bias = bool(bias_on)
        self._on_t = clock() - self.FIX_AFTER_S - self.LOCK_AFTER_FIX_S if bias_on else None
        self.no_fix = (os.environ.get("DPMG_SIM_NO_GPS_FIX") == "1") if no_fix is None else bool(no_fix)

    def set_bias(self, enable: bool) -> dict:
        enable = bool(enable)
        if enable and not self.bias:
            self._on_t = self._clock()
        if not enable:
            self._on_t = None
        self.bias = enable
        return self.read_bias() | {"ok": True}

    def read_bias(self) -> dict:
        return {"ant_bias": self.bias,
                "raw": "00 00 00 00" if self.bias else "00 10 00 00", "sim": True}

    def _since_on(self):
        return None if self._on_t is None else self._clock() - self._on_t

    def gnss(self) -> dict:
        t = self._since_on()
        now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
        if t is None or t < 3.0:
            return {"fix_mode": 1, "fix": "none", "sats_visible": 0, "sats_used": 0,
                    "cno_max": None, "cno_median": None, "time_utc": None}
        if self.no_fix:
            return {"fix_mode": 1, "fix": "none", "sats_visible": 1, "sats_used": 0,
                    "cno_max": 25.0, "cno_median": 25.0, "time_utc": "2019-04-07T04:16:31.000Z"}
        vis = min(22, 4 + int(t))
        fix = t >= self.FIX_AFTER_S
        used = min(16, max(0, vis - 6)) if fix else 0
        return {"fix_mode": 3 if fix else 1, "fix": "3D" if fix else "none",
                "sats_visible": vis, "sats_used": used,
                "cno_max": 46.0 if fix else 31.0, "cno_median": 38.0 if fix else 24.0,
                "time_utc": now if fix else None}

    def gpsdo_locked(self) -> bool:
        t = self._since_on()
        return t is not None and t >= self.FIX_AFTER_S + self.LOCK_AFTER_FIX_S
