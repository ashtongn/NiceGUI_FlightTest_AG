"""card_imu.py: tilt, shock and motion from the NV100 on-card accelerometer.

The service samples the RX card's accelerometer (skiq_read_accel, in g) a few
times a second while the radio is started and feeds every sample to an
ImuTracker. The tracker reports:

* tilt: the angle between the current gravity direction and the reference
  ("level") direction. The reference is the average of the first REF_SAMPLES
  steady readings after the radio starts (the sensor needs a moment to settle), or the reading when the operator presses "set level". The card's
  mounting inside the G40 does not matter, because tilt is measured against that
  reference, not against a fixed axis.
* shock: a sample whose magnitude differs from the resting magnitude (the
  reference's; the NV100 sensor reads ~0.93 g at rest, not 1 g) by more than SHOCK_G. Each one
  is counted, and the latest is kept with its size and time. The GUI shows it red
  for SHOCK_HOLD_S.
* motion: the standard deviation of the magnitude over the last MOTION_WINDOW_S
  is above MOTION_RMS_G (vibration or handling). A steady offset is not motion.

Sampling a few times a second can miss a shock shorter than the sample interval.
"""
from __future__ import annotations

import math
import time
from collections import deque

TILT_WARN_DEG = 5.0       # amber: the unit has moved off its reference orientation
TILT_ALARM_DEG = 15.0     # red
SHOCK_G = 0.5             # |a| departs from 1 g by more than this = a shock
SHOCK_HOLD_S = 60.0       # the GUI keeps a shock red this long
MOTION_RMS_G = 0.05       # RMS departure from 1 g over the window = moving / vibrating
MOTION_WINDOW_S = 5.0
SAMPLE_S = 0.25           # service sampling period
REF_SAMPLES = 8           # the level reference averages this many steady readings (2 s)


def magnitude(v) -> float:
    return math.sqrt(sum(c * c for c in v))


def angle_deg(a, b) -> float:
    """Angle between two acceleration vectors, in degrees."""
    ma, mb = magnitude(a), magnitude(b)
    if ma <= 0 or mb <= 0:
        return 0.0
    cos = sum(x * y for x, y in zip(a, b)) / (ma * mb)
    return math.degrees(math.acos(max(-1.0, min(1.0, cos))))


class ImuTracker:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.ref = None
        self.last = None
        self._ref_acc = []
        self.window = deque()
        self.shocks = 0
        self.last_shock = None          # {"g": peak |a|, "at": "YYYY-MM-DD HH:MM:SS", "t": epoch}

    def set_level(self):
        """Use the latest reading as the level reference."""
        if self.last is not None:
            self.ref = tuple(self.last)

    def add(self, v):
        """Feed one (x, y, z) sample in g."""
        if v is None:
            return
        now = self.clock()
        self.last = tuple(float(c) for c in v)
        m = magnitude(self.last)
        rest = magnitude(self.ref) if self.ref else 1.0
        dev = abs(m - rest)
        if self.ref is None and dev < 0.15:     # average the first steady readings = level
            self._ref_acc.append(self.last)
            if len(self._ref_acc) >= REF_SAMPLES:
                n = len(self._ref_acc)
                self.ref = tuple(sum(v[i] for v in self._ref_acc) / n for i in range(3))
                self._ref_acc = []
        self.window.append((now, m))
        while self.window and now - self.window[0][0] > MOTION_WINDOW_S:
            self.window.popleft()
        if dev > SHOCK_G:
            self.shocks += 1
            self.last_shock = {"g": round(m, 2), "t": now,
                               "at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))}

    def reset(self):
        """Radio stopped: forget the samples and the reference, keep the shock log."""
        self.ref = None
        self.last = None
        self._ref_acc = []
        self.window.clear()

    def summary(self) -> dict | None:
        """Compact state for get_config, or None with no reading yet."""
        if self.last is None:
            return None
        ms = [v for _t, v in self.window]
        mean = sum(ms) / len(ms) if ms else 0.0
        rms = math.sqrt(sum((v - mean) ** 2 for v in ms) / len(ms)) if ms else 0.0
        out = {"g": [round(c, 3) for c in self.last],
               "tilt_deg": round(angle_deg(self.last, self.ref), 1) if self.ref else None,
               "motion": rms > MOTION_RMS_G, "rms_g": round(rms, 3),
               "shocks": self.shocks}
        if self.last_shock:
            out["last_shock"] = {"g": self.last_shock["g"], "at": self.last_shock["at"],
                                 "age_s": round(self.clock() - self.last_shock["t"], 1)}
        return out


def tilt_level(tilt):
    if tilt is None:
        return None
    if tilt >= TILT_ALARM_DEG:
        return "hot"
    if tilt >= TILT_WARN_DEG:
        return "warm"
    return "ok"


# Radio kinds with an on-card accelerometer (Epiq cards, and the mock service
# that stands in for them). USB radios (B200, HackRF, LimeSDR, Pluto) have none.
IMU_KINDS = ("g40", "x40", "mock")


def has_imu(kind):
    """True/False for a known radio kind, None when the kind isn't known yet."""
    return None if not kind else str(kind).lower() in IMU_KINDS


def card_text(imu, kind=None) -> tuple:
    """(text, level) for the operator radio card from get_config 'imu'.
    `kind` is the radio kind when known, so a radio with no IMU says so
    instead of 'radio not started'."""
    if not imu:
        if has_imu(kind) is False:
            return "motion: no IMU", None
        return "motion: radio not started", None
    tilt = imu.get("tilt_deg")
    parts = [f"tilt {tilt:.1f}°" if tilt is not None else "tilt: settling"]
    parts.append("moving" if imu.get("motion") else "still")
    lvl = tilt_level(tilt) or "ok"
    if imu.get("motion") and lvl == "ok":
        lvl = "warm"
    sh = imu.get("last_shock")
    if sh and sh.get("age_s", 1e9) <= SHOCK_HOLD_S:
        parts.append(f"SHOCK {sh['g']:.1f} g")
        lvl = "hot"
    elif imu.get("shocks"):
        parts.append(f"{imu['shocks']} shock(s), last {sh['at'] if sh else '?'}")
    return "motion: " + ", ".join(parts), lvl
