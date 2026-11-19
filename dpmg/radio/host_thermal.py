"""host_thermal.py: on-chip temperature of the radio host, for the status heartbeat.

The G40's Orin module exposes its thermal zones under /sys/class/thermal
(cpu, gpu, cv0-2, soc0-2 and tj). `tj-thermal` is the junction temperature,
the hottest point on the module, and is the one the module's own fan and
throttling act on. Trip points read on a bench G40 (JetPack 6):

    tj-thermal   fan stages at 35 / 74 / 95 C, critical (shutdown) 104 C
    other zones  passive 70 C, passive (throttle) 99 C, critical 104 C

The heartbeat carries one value, Status.temp_c_x10 (deci-degrees C). A value of
NO_READING (or 0 from an older service) means "no reading".
"""
from __future__ import annotations

import glob
import os

THERMAL_ROOT = "/sys/class/thermal"
HWMON_ROOT = "/sys/class/hwmon"
JUNCTION_ZONE = "tj-thermal"
WARN_C = 70.0      # first passive trip on the G40 zones
HOT_C = 95.0       # top fan stage; throttling starts at 99 C, shutdown at 104 C
NO_READING = -32768

# NV100 on-card sensor (skiq_read_temp, whole degrees C). libsidekiq
# publishes no limit; these follow common industrial board practice and should be
# checked against Epiq's NV100 datasheet.
CARD_WARN_C = 80.0        # idle NV100 cards read 73-75 C on a bench G40
CARD_HOT_C = 90.0
CARD_NAMES = {0: "A", 1: "B"}


def read_ina3221(root: str = HWMON_ROOT):
    """Orin module rails from the INA3221 power monitor (hwmon 'ina3221'), as
    {"rails": [{"label", "v", "a", "w"}, ...], "in_w": VDD_IN watts or None},
    or None when the host has no INA3221 (laptop, simulate)."""
    for h in sorted(glob.glob(os.path.join(root, "hwmon*"))):
        try:
            with open(os.path.join(h, "name")) as f:
                if f.read().strip() != "ina3221":
                    continue
        except OSError:
            continue
        rails = []
        for i in (1, 2, 3):
            try:
                with open(os.path.join(h, f"in{i}_input")) as f:
                    mv = int(f.read().strip())
                with open(os.path.join(h, f"curr{i}_input")) as f:
                    ma = int(f.read().strip())
            except (OSError, ValueError, TypeError):
                continue
            try:
                with open(os.path.join(h, f"in{i}_label")) as f:
                    label = f.read().strip()
            except OSError:
                label = f"ch{i}"
            rails.append({"label": label, "v": round(mv / 1000.0, 3), "a": round(ma / 1000.0, 3),
                          "w": round(mv * ma / 1e6, 2)})
        if not rails:
            return None
        vin = next((r for r in rails if r["label"] == "VDD_IN"), None)
        return {"rails": rails, "in_w": vin["w"] if vin else None}
    return None


def power_text(host_power) -> str:
    """Suffix for the chip temperature line from get_config 'host_power', or ''."""
    if not host_power or host_power.get("in_w") is None:
        return ""
    return f", module {host_power['in_w']:.1f} W"


def read_zones(root: str = THERMAL_ROOT) -> dict:
    """{zone type: degrees C} for every readable thermal zone under root."""
    out = {}
    for z in sorted(glob.glob(os.path.join(root, "thermal_zone*"))):
        try:
            with open(os.path.join(z, "type")) as f:
                name = f.read().strip()
            with open(os.path.join(z, "temp")) as f:
                milli = int(f.read().strip())
        except (OSError, ValueError, TypeError):
            # TypeError: on the Orin Nano the cv0-2 zones answer EAGAIN and the
            # text read hands back None instead of raising OSError. Uncaught, one
            # such zone loses the whole reading ("no chip temp" on the Super Nano).
            continue
        out[name] = milli / 1000.0
    return out


def junction_c(zones: dict):
    """The junction temperature if the host reports it, else the hottest zone."""
    if not zones:
        return None
    if JUNCTION_ZONE in zones:
        return zones[JUNCTION_ZONE]
    return max(zones.values())


def to_wire(c) -> int:
    """Degrees C: Status.temp_c_x10 (int16)."""
    if c is None:
        return NO_READING
    return max(-32767, min(32767, int(round(c * 10))))


def from_wire(v):
    """Status.temp_c_x10: degrees C, or None when the radio sent no reading."""
    if v in (NO_READING, 0, None):
        return None
    return v / 10.0


def level(c):
    """'ok' / 'warm' / 'hot', or None with no reading."""
    if c is None:
        return None
    if c >= HOT_C:
        return "hot"
    if c >= WARN_C:
        return "warm"
    return "ok"


def card_level(c):
    """'ok' / 'warm' / 'hot' for an NV100 card reading, or None with no reading."""
    if c is None:
        return None
    if c >= CARD_HOT_C:
        return "hot"
    if c >= CARD_WARN_C:
        return "warm"
    return "ok"


def card_temps_from_config(cfg):
    """get_config 'card_temps' ({"0": 41, ...}): sorted [(card, deg C)]."""
    raw = (cfg or {}).get("card_temps") or {}
    out = []
    for k, v in raw.items():
        try:
            out.append((int(k), float(v)))
        except (TypeError, ValueError):
            continue
    return sorted(out)


def card_last_from_config(cfg):
    """get_config 'card_temps_last' ({"0": [50, 240], ...}): sorted
    [(card, deg C, seconds since it was read)]: the last reading of a card the
    service can no longer read (radio closed). Empty from an older service."""
    raw = (cfg or {}).get("card_temps_last") or {}
    out = []
    for k, v in raw.items():
        try:
            out.append((int(k), float(v[0]), int(v[1])))
        except (TypeError, ValueError, IndexError):
            continue
    return sorted(out)


def age_text(seconds) -> str:
    """'12 s', '4 min', '3 h', '2 days' for how old a reading is."""
    s = max(0, int(seconds))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    if s < 2 * 86400:
        return f"{s // 3600} h"
    return f"{s // 86400} days"


def last_card_text(cfg) -> str:
    """The card line when there is no live reading: the last readings with
    their age, or "card temp: radio not started" when there are none."""
    last = card_last_from_config(cfg)
    if not last:
        return "card temp: radio not started"
    return ", ".join(f"{card_label(cfg, c)} {t:.0f} °C, {age_text(a)} ago"
                     for c, t, a in last) + " (radio closed)"


def card_label(cfg, card) -> str:
    """Name for a radio card on the operator card: the service's own name for
    it (get_config 'card_names', a B200 says "B200"), else the NV100 letter."""
    names = (cfg or {}).get("card_names") or {}
    return names.get(str(card)) or f"card {CARD_NAMES.get(card, card)}"


def worst(levels):
    """The most severe of several levels ('hot' > 'warm' > 'ok'), or None."""
    order = {"ok": 0, "warm": 1, "hot": 2}
    lv = [x for x in levels if x in order]
    return max(lv, key=order.get) if lv else None
