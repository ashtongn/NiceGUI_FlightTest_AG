"""frontend_setup.py: build, validate and report the G40 GPIO front-end config
(rf_frontend.yaml: HPA enable, TX/RX switch select, HPA alarm input) for
``./deploy.sh --frontend``, so the operator never hand-edits the YAML.

The questions are asked by G40 27-pin socket pin number (the socket table is shown
first); chip, logical line name, Tegra pad fallback and, for pin 16, the level-
translator direction line are filled in from the pin.  Polarity comes from the HPA /
switch datasheet, or from a preset keyed by part number.

  python3 -m dpmg.payload.frontend_setup generate --out FILE [answers] [--current FILE] [--batch]
        ask the questions (or take them from flags), write FILE, validate it with
        rf_frontend.py's own loader, print it and the warnings
  python3 -m dpmg.payload.frontend_setup check FILE      validate a ready-made file
  python3 -m dpmg.payload.frontend_setup report RELOAD_OUTPUT --expect FILE
        read `bench/rf_frontend_bench.py reload` output, print what the radio
        resolved; exit 1 if the reload failed or does not match FILE
  python3 -m dpmg.payload.frontend_setup warn [FILE]     print the fail-safe warning (+ FILE's)
  python3 -m dpmg.payload.frontend_setup pins            print the socket table

Answers (each one given on the command line is not asked; with --batch nothing is
asked and an unanswered question takes the preset / --current / built-in default:
except a polarity the preset does not know, which must be given):

  --kind none|gpio|mock        is an HPA connected (none = feature off, mock = no hardware)
  --hpa MODEL                  empower-1189 | empower-1208 | empower-1211 | empower-1193 |
                               empower-1212 | custom            (alias --preset)
  --pa-pin 17|15|16            socket pin wired to the HPA enable
  --pa-active-high true|false  does HIGH on that pin turn the HPA on (datasheet)
  --switch MODEL               pe71s2041 | dbwave-pasr | custom
  --switch-pin 15|17|16|7-10   socket pin driving the switch; 7-10 = NV100 TR_N proxy
                               (alias --tr-pin)
  --tr-tx-level 1|0            switch control level that selects TX (datasheet)
  --fault-pin 18|none          socket pin carrying the HPA alarm, if any
  --fault-level high|low       alarm level on fault
  --level-shifter pullup|none  BOB-12009 / BSS138-style pull-up shifter between G40 and parts
  --watchdog-ms N              HPA dropped after N ms with no TX send (0 = off; default 500)
  --atten none|auto|hidraw[:/dev/hidrawN]|http://ip
                               RX attenuator (RCDAT-6G-120H): auto = found on USB when it
                               is plugged in (read only on attach), hidraw = opened at
                               start, http = Ethernet model.  Default: the current file's
                               atten section; a new file with no current file gets auto
  --port JACK=ROLE[:CHAIN]     a jack's lockout: role tx | rx | txrx | none, CHAIN = plain
                               text for the operator. Repeat per jack; a jack not listed
                               counts as none. With no --port the current file's ports:
                               section is kept
  --ports-clear                remove the lockout (every jack may do TX and RX)
  --current FILE               the radio's current rf_frontend.yaml: defaults, settle/hang
                               times, the attenuator section (kept unless --atten)
  shorthands: --tr-mode gpio|proxy, --pa-fault none|high|low, --pa-active-low
"""
from __future__ import annotations

import argparse
import ast
import datetime as _dt
import os
import re
import sys
import tempfile

from dpmg.payload import rf_frontend as rf  # noqa: E402
from dpmg.radio import port_map as _port_map  # noqa: E402

# the socket
# G40 27-pin GPIO socket, Epiq G-Series HW User Manual v1.5 Table 12 (as documented in
# rf_frontend.example.yaml).  pin: (signal, source, direction, what it can be used for)
SOCKET_TABLE = {
    1: ("+3.3 V", "carrier", "power", "33 mA supply; not a control line"),
    2: ("+3.3 V", "carrier", "power", "33 mA supply; not a control line"),
    3: ("GND", "", "ground", "ground"),
    4: ("GND", "", "ground", "ground"),
    5: ("NVA_GPIO_8", "NV100 card A FPGA", "in/out", "needs FPGA support; not usable"),
    6: ("NVB_GPIO_3", "NV100 card B FPGA", "in/out", "needs FPGA support; not usable"),
    7: ("NVA_TR_N_2", "NV100 card A FPGA", "out", "switch, FPGA proxy only (not yet scoped)"),
    8: ("NVB_TR_N_1", "NV100 card B FPGA", "out", "switch, FPGA proxy only (not yet scoped)"),
    9: ("NVA_TR_N_1", "NV100 card A FPGA", "out", "switch, FPGA proxy only (not yet scoped)"),
    10: ("NVB_TR_N_2", "NV100 card B FPGA", "out", "switch, FPGA proxy only (not yet scoped)"),
    11: ("NVA_GPIO_3", "NV100 card A FPGA", "in/out", "needs FPGA support; not usable"),
    12: ("NVB_GPIO_8", "NV100 card B FPGA", "in/out", "needs FPGA support; not usable"),
    13: ("GND", "", "ground", "ground"),
    14: ("GND", "", "ground", "ground"),
    15: ("CPU_GP_OUT_2", "Orin NX PN.01", "out", "HPA enable or switch (default: switch)"),
    16: ("CPU_GPIO", "Orin NX PP.06", "in/out", "HPA enable or switch; direction line PCC.03 set to output"),
    17: ("CPU_GP_OUT_1", "Orin NX PG.06", "out", "HPA enable or switch (default: HPA enable)"),
    18: ("CPU_GP_IN", "Orin NX PCC.01", "in", "HPA alarm / fault input"),
    19: ("GND", "", "ground", "ground"),
    20: ("GND", "", "ground", "ground"),
    21: ("SPI0 / I2C-1", "Orin NX", "bus", "SPI/I2C bus pin; not a GPIO line here"),
    22: ("SPI0 / I2C-1", "Orin NX", "bus", "SPI/I2C bus pin; not a GPIO line here"),
    23: ("SPI0 / I2C-1", "Orin NX", "bus", "SPI/I2C bus pin; not a GPIO line here"),
    24: ("SPI0 / I2C-1", "Orin NX", "bus", "SPI/I2C bus pin; not a GPIO line here"),
    25: ("SPI0 / I2C-1", "Orin NX", "bus", "SPI/I2C bus pin; not a GPIO line here"),
    26: ("+3.3 V", "carrier", "power", "33 mA supply; not a control line"),
    27: ("SPI0 / I2C-1", "Orin NX", "bus", "SPI/I2C bus pin; not a GPIO line here"),
}

# Orin lines this tool assigns.  Offsets 41/85 (gpiochip0) and 13 (gpiochip1) were
# measured with gpioinfo on a bench G40, BSP R36.5; 98 (PP.06) and 15
# (PCC.03) follow the same tegra234 port layout but were not measured.
ORIN_LINES = {
    # pin: (logical name, chip, Tegra pad, R36 offset, direction line or None)
    17: ("cpu-gp-out1", "gpiochip0", "PG.06", 41, None),
    15: ("cpu-gp-out2", "gpiochip0", "PN.01", 85, None),
    16: ("cpu-gpio", "gpiochip0", "PP.06", 98,
         ("cpu-gpio-direction", "gpiochip1", "PCC.03", 15)),
    18: ("cpu-gp-in", "gpiochip1", "PCC.01", 13, None),
}
OUT_PINS = (17, 15, 16)
FAULT_PINS = (18,)
PROXY_PINS = {9: "card A chan 1", 7: "card A chan 2", 8: "card B chan 1", 10: "card B chan 2"}
_BY_LINE = {("/dev/" + c, off): pin for pin, (_n, c, _p, off, _d) in ORIN_LINES.items()}
_DIR_LINES = {("/dev/" + d[1], d[3]) for (_n, _c, _p, _o, d) in ORIN_LINES.values() if d}

# HPA presets keyed by part.  pa_active_high / fault_level None = unknown: the operator
# must enter it from the datasheet (no default, even with --batch).
HPA_PRESETS = {
    "empower-1189": {"label": "Empower 1189 / BBM3K5KKO (500-2500 MHz, 100 W; payload 1)",
                     "pa_active_high": False, "has_alarm": False, "fault_level": None,
                     "note": "DB-9 pin 5 Shutdown: HIGH = disabled, LOW = enabled; 5 V TTL; no alarm pin"},
    "empower-1208": {"label": "Empower 1208 (payload 2) -- polarity NOT on file",
                     "pa_active_high": None, "has_alarm": None, "fault_level": None,
                     "note": "enter the enable/shutdown polarity and the alarm level from its datasheet"},
    "empower-1211": {"label": "Empower 1211 (payload 3) -- polarity NOT on file",
                     "pa_active_high": None, "has_alarm": None, "fault_level": None,
                     "note": "enter the enable/shutdown polarity and the alarm level from its datasheet"},
    "empower-1193": {"label": "Empower 1193 / BBM2E4AKO (20-1000 MHz, 100 W)",
                     "pa_active_high": True, "has_alarm": True, "fault_level": "high",
                     "note": "D-sub pin 6 PA Enable (HIGH = on, 0/3.3 V); pin 7 Alarm HIGH = fault"},
    "empower-1212": {"label": "Empower 1212 (2000-6000 MHz, 50 W)",
                     "pa_active_high": True, "has_alarm": True, "fault_level": "high",
                     "note": "D-sub pin 6 PA Enable (HIGH = on, 0/3.3 V); pin 7 Alarm HIGH = fault"},
    "custom": {"label": "custom (answer from the HPA datasheet)",
               "pa_active_high": None, "has_alarm": None, "fault_level": None, "note": ""},
}
HPA_ALIASES = {"1189": "empower-1189", "1208": "empower-1208", "1211": "empower-1211",
               "1193": "empower-1193", "1212": "empower-1212",
               "bbm3k5kko": "empower-1189", "bbm2e4ako": "empower-1193"}
SWITCH_PRESETS = {
    "pe71s2041": {"label": "pSemi PE71S2041 (payload RF switch, 15 V supply) -- control level NOT on file",
                  "tx_level": None,
                  "note": "enter which control level selects the HPA (TX) port from its datasheet"},
    "dbwave-pasr": {"label": "dBwave PASR0200500600A SP2T", "tx_level": None,
                    "note": "TTL 1 = J0-J1, TTL 0 = J0-J2: TX level depends on which port the HPA is on"},
    "custom": {"label": "custom (answer from the switch datasheet)", "tx_level": None, "note": ""},
}
SWITCH_ALIASES = {"pe71s2041": "pe71s2041", "pasr": "dbwave-pasr",
                  "pasr0200500600a": "dbwave-pasr", "dbwave": "dbwave-pasr"}

DEFAULTS = {"kind": "gpio", "hpa": "custom", "pa_pin": 17, "pa_active_high": True,
            "switch": "custom", "switch_pin": 15, "tr_tx_level": 1, "fault_pin": "none",
            "fault_level": "high", "level_shifter": "none", "watchdog_ms": 500}
QUESTIONS = tuple(DEFAULTS)


class AnswerError(ValueError):
    """An answer that cannot be turned into a valid rf_frontend.yaml."""


def pin_reason(pin: int) -> str:
    sig, src, d, use = SOCKET_TABLE[pin]
    if d == "power":
        return f"socket pin {pin} is {sig} power"
    if d == "ground":
        return f"socket pin {pin} is ground"
    if d == "bus":
        return f"socket pin {pin} is an SPI0/I2C-1 bus pin, not a GPIO line"
    if pin in (5, 6, 11, 12):
        return f"socket pin {pin} ({sig}) is an NV100 FPGA GPIO with no software path yet"
    return f"socket pin {pin} ({sig})"


def check_pin(role: str, v):
    """Socket pin for role 'pa' | 'switch' | 'fault' (fault may be 'none')."""
    s = str(v).strip().lower()
    if role == "fault" and s in ("none", "no", "n", "0", "-", ""):
        return "none"
    try:
        pin = int(s)
    except ValueError:
        raise AnswerError(f"answer with a socket pin number (1-27){' or none' if role == 'fault' else ''}, got {v!r}")
    if pin not in SOCKET_TABLE:
        raise AnswerError(f"socket pin {pin} does not exist (the socket has pins 1-27)")
    if role == "fault":
        if pin in FAULT_PINS:
            return pin
        if pin in OUT_PINS:
            raise AnswerError(f"socket pin {pin} is used here as an output; the alarm input is pin 18 (CPU_GP_IN)")
        if pin in PROXY_PINS:
            raise AnswerError(f"socket pin {pin} is an NV100 TR_N output, not an input; the alarm input is pin 18")
        raise AnswerError(pin_reason(pin) + "; the alarm input is pin 18 (CPU_GP_IN)")
    if pin in OUT_PINS:
        return pin
    if pin in FAULT_PINS:
        raise AnswerError("socket pin 18 (CPU_GP_IN) is input-only; use 17, 15 or 16")
    if pin in PROXY_PINS:
        if role == "switch":
            return pin
        raise AnswerError(f"socket pin {pin} is an NV100 TR_N line the FPGA drives; it can only be "
                          f"the switch's proxy, not the HPA enable (use 17, 15 or 16)")
    raise AnswerError(pin_reason(pin) + "; use " + ("17, 15 or 16" if role == "pa" else
                                                     "15, 16, 17, or 7-10 for the FPGA proxy"))


# answer parsing
def _yes_no(v) -> bool:
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("y", "yes", "true", "1", "on", "high", "h"):
        return True
    if s in ("n", "no", "false", "0", "off", "low", "l"):
        return False
    raise AnswerError(f"expected yes/no (true/false), got {v!r}")


def parse_answer(key: str, v):
    """Normalise one answer; raises AnswerError with a plain-words reason."""
    if v is None:
        raise AnswerError("an answer is required here (no default: see the datasheet)")
    s = str(v).strip().lower() if not isinstance(v, (bool, int)) else v
    if key == "kind":
        if s not in rf.FRONTEND_KINDS:
            raise AnswerError(f"HPA connection must be one of {', '.join(rf.FRONTEND_KINDS)}, got {v!r}")
        return s
    if key == "hpa":
        s = HPA_ALIASES.get(s, s)
        if s not in HPA_PRESETS:
            raise AnswerError(f"HPA must be one of {', '.join(HPA_PRESETS)}, got {v!r}")
        return s
    if key == "switch":
        s = SWITCH_ALIASES.get(s, s)
        if s not in SWITCH_PRESETS:
            raise AnswerError(f"switch must be one of {', '.join(SWITCH_PRESETS)}, got {v!r}")
        return s
    if key == "pa_pin":
        return check_pin("pa", v)
    if key == "switch_pin":
        return check_pin("switch", v)
    if key == "fault_pin":
        return check_pin("fault", v)
    if key == "pa_active_high":
        return _yes_no(v)
    if key == "tr_tx_level":
        if str(v).strip() not in ("0", "1"):
            raise AnswerError(f"TX level must be 1 or 0, got {v!r}")
        return int(str(v).strip())
    if key == "fault_level":
        s = {"h": "high", "1": "high", "l": "low", "0": "low"}.get(s, s)
        if s not in ("high", "low"):
            raise AnswerError(f"alarm level on fault must be high or low, got {v!r}")
        return s
    if key == "level_shifter":
        s = {"y": "pullup", "yes": "pullup", "bss138": "pullup", "bob-12009": "pullup",
             "n": "none", "no": "none"}.get(s, s)
        if s not in ("pullup", "none"):
            raise AnswerError(f"level shifter must be pullup (BOB-12009 / BSS138) or none, got {v!r}")
        return s
    if key == "watchdog_ms":
        try:
            ms = float(str(v).strip())
        except ValueError:
            raise AnswerError(f"watchdog must be a number of ms, got {v!r}")
        if not (ms == 0 or 50 <= ms <= 60000):
            raise AnswerError(f"watchdog {ms:g} ms: use 0 (off) or 50..60000")
        return int(ms) if ms == int(ms) else ms
    raise AnswerError(f"unknown question {key!r}")


def pin_of_spec(chip, *specs):
    """Orin socket pin a (chip, line spec) names, or None."""
    for spec in specs:
        kind, val = rf.parse_line_spec(spec)
        for pin, (name, c, pad, off, _d) in ORIN_LINES.items():
            if rf.chip_path(chip) != "/dev/" + c:
                continue
            if (kind == "offset" and val == off) or (kind == "name" and val in (name, pad)):
                return pin
    return None


_META_RE = re.compile(r"^# frontend_setup:(.*)$", re.M)


def read_meta(text: str) -> dict:
    """The generator's machine-readable header line (`# frontend_setup: k=v ...`)."""
    m = _META_RE.search(text or "")
    if not m:
        return {}
    return dict(kv.split("=", 1) for kv in m.group(1).split() if "=" in kv)


def defaults_from_current(cfg: rf.FrontendConfig, meta: dict | None = None) -> dict:
    """Answers that reproduce a current config (only fields this tool can express)."""
    d = {"kind": cfg.frontend, "pa_active_high": cfg.pa_enable.active_high,
         "tr_tx_level": cfg.tr_switch.tx_level, "watchdog_ms": cfg.watchdog_ms}
    if cfg.pa_fault_in.enabled:
        d["fault_pin"] = pin_of_spec(cfg.pa_fault_in.chip, cfg.pa_fault_in.line,
                                     cfg.pa_fault_in.fallback_line) or "none"
        d["fault_level"] = "low" if cfg.pa_fault_in.active_low else "high"
    else:
        d["fault_pin"] = "none"
    pin = pin_of_spec(cfg.pa_enable.chip, cfg.pa_enable.line, cfg.pa_enable.fallback_line)
    if pin in OUT_PINS:
        d["pa_pin"] = pin
    if cfg.tr_switch.mode == "gpio":
        pin = pin_of_spec(cfg.tr_switch.chip, cfg.tr_switch.line, cfg.tr_switch.fallback_line)
        if pin in OUT_PINS:
            d["switch_pin"] = pin
    else:
        d["switch_pin"] = 9
    for k in ("hpa", "switch", "switch_pin", "level_shifter"):
        if meta and k in meta:
            try:
                d[k] = parse_answer(k, meta[k])
            except AnswerError:
                pass
    if isinstance(d["watchdog_ms"], float) and d["watchdog_ms"] == int(d["watchdog_ms"]):
        d["watchdog_ms"] = int(d["watchdog_ms"])
    return d


# questions
def socket_table_text() -> str:
    rows = ["  G40 27-pin GPIO socket (Epiq G-Series HW User Manual v1.5, Table 12; 3.3 V logic)",
            "   pin  signal         source              dir     use here",
            "   ---  -------------  ------------------  ------  ------------------------------------------"]
    for pin in sorted(SOCKET_TABLE):
        sig, src, d, use = SOCKET_TABLE[pin]
        mark = "*" if (pin in OUT_PINS or pin in FAULT_PINS or pin in PROXY_PINS) else " "
        rows.append(f"  {mark}{pin:>3}  {sig:<13}  {src:<18}  {d:<6}  {use}")
    rows.append("   * = offered: HPA enable 17/15/16, switch 15/17/16 or 7-10 (FPGA proxy), alarm 18")
    return "\n".join(rows) + "\n"


def _prompt(text: str, default, key: str, inp, out, extra=None):
    """Ask until a valid answer.  default None = no default (an answer is required).
    ``extra(value)`` may raise AnswerError for a cross-question rule."""
    while True:
        out.write(f"  {text} [{'' if default is None else default}]: ")
        out.flush()
        raw = inp.readline()
        if raw == "":                      # EOF
            out.write("\n")
            if default is None:
                raise AnswerError(f"{text.split('?')[0]}?: no answer and no default")
        raw = raw.strip()
        try:
            val = parse_answer(key, raw if raw else default)
            if extra is not None:
                extra(val)
            return val
        except AnswerError as e:
            out.write(f"    !! {e}\n")
            if raw == "" and default is not None:   # a bad default would loop forever
                raise


def _yn(b) -> str | None:
    return None if b is None else ("y" if b else "n")


def collect_answers(given: dict, current: dict | None = None, batch: bool = False,
                    inp=None, out=None) -> dict:
    """Merge flags, presets, --current and prompts into a complete answer set."""
    inp = inp or sys.stdin
    out = out or sys.stdout
    base = dict(DEFAULTS)
    if current:
        base.update(current)
    a: dict = {}

    def get(key, text, default, required=False, extra=None):
        """required = the preset does not know this value: no default, ever."""
        if key in given:
            a[key] = given[key]
        elif batch:
            if required:
                raise AnswerError(f"{text.split('?')[0]}? -- not known for this part; give it "
                                  f"on the command line (--{key.replace('_', '-')})")
            a[key] = parse_answer(key, default)
        else:
            a[key] = _prompt(text, None if required else default, key, inp, out, extra)
            return a[key]
        if extra is not None:
            extra(a[key])
        return a[key]

    def not_pa_pin(pin):
        if pin == a.get("pa_pin"):
            raise AnswerError(f"socket pin {pin} is already the HPA enable; one pin cannot have two roles")

    kind = get("kind", "Is an HPA connected?  none = no HPA (front end off), gpio = real HPA "
                       "on the G40 GPIO socket, mock = no hardware (GUI testing)", base["kind"])
    if kind == "none":
        for k in QUESTIONS:
            a.setdefault(k, given.get(k, base[k]))
        return _finish(a)

    if not batch:
        out.write("\n" + socket_table_text() + "\n")

    # HPA
    if not batch and "hpa" not in given:
        out.write("    HPA presets: " + "; ".join(f"{k} = {v['label']}" for k, v in HPA_PRESETS.items()) + "\n")
    hpa = get("hpa", "Which HPA? (empower-1189 / -1208 / -1211 / -1193 / -1212 / custom)", base["hpa"])
    p = HPA_PRESETS[hpa]
    if p["note"]:
        out.write(f"    {p['label']}: {p['note']}\n")
    pa_pin = get("pa_pin", "Which socket pin is wired to the HPA enable? (17, 15 or 16)", base["pa_pin"])
    if p["pa_active_high"] is None:
        get("pa_active_high", "Does a HIGH level on that pin turn the HPA ON? (y = HIGH = on, "
                              "n = LOW = on, e.g. a shutdown pin; from the HPA datasheet)",
            _yn(base["pa_active_high"]), required=(hpa != "custom" and base.get("hpa") != hpa))
    else:
        if "pa_active_high" in given and given["pa_active_high"] != p["pa_active_high"]:
            raise AnswerError(f"{hpa}: its enable is {'HIGH' if p['pa_active_high'] else 'LOW'} = on; "
                              f"--pa-active-high {str(given['pa_active_high']).lower()} contradicts "
                              f"the preset (use --hpa custom for a modified unit)")
        a["pa_active_high"] = p["pa_active_high"]
        out.write(f"    HPA enable: {'HIGH' if p['pa_active_high'] else 'LOW'} = HPA on (from the preset)\n")

    # switch
    if not batch and "switch" not in given:
        out.write("    switch presets: " + "; ".join(f"{k} = {v['label']}" for k, v in SWITCH_PRESETS.items()) + "\n")
    sw = get("switch", "Which TX/RX switch? (pe71s2041 / dbwave-pasr / custom)", base["switch"])
    sp = SWITCH_PRESETS[sw]
    if sp["note"]:
        out.write(f"    {sp['label']}: {sp['note']}\n")
    free = [x for x in (15, 17, 16) if x != pa_pin][0]
    sw_default = base["switch_pin"] if base["switch_pin"] != pa_pin else free
    sw_pin = get("switch_pin", f"Which socket pin drives the TX/RX switch? ({free} is a free output; "
                               f"7-10 = NV100 TR_N FPGA proxy, NOT YET SCOPED; card A = 9/7, B = 8/10)",
                 sw_default, extra=not_pa_pin)
    if sw_pin in PROXY_PINS:
        out.write(f"    !! pin {sw_pin} = {SOCKET_TABLE[sw_pin][0]} ({PROXY_PINS[sw_pin]}): proxy mode. TR_N "
                  "polarity and timing are not yet scoped; software only tracks the switch, nothing "
                  "drives it, and the HPA/switch interlock is not enforced.\n")
        a.setdefault("tr_tx_level", given.get("tr_tx_level", base["tr_tx_level"]))
    else:
        get("tr_tx_level", "Which switch control level selects TX (the HPA port)? (1 = HIGH, 0 = LOW; "
                           "from the switch datasheet)",
            base["tr_tx_level"], required=(sw != "custom" and base.get("switch") != sw))

    # alarm
    if p["has_alarm"] is False:
        if given.get("fault_pin", "none") != "none":
            raise AnswerError(f"{hpa} has no alarm output; --fault-pin must be none")
        a["fault_pin"] = "none"
        out.write("    HPA alarm: none (this HPA has no alarm pin)\n")
    else:
        fault_default = base["fault_pin"]
        if p["has_alarm"] and "fault_pin" not in (current or {}):
            fault_default = 18
        fp = get("fault_pin", "Which socket pin, if any, carries the HPA alarm/fault? (18 or none)",
                 fault_default)
        if fp != "none":
            if p["fault_level"] is not None:
                if given.get("fault_level", p["fault_level"]) != p["fault_level"]:
                    raise AnswerError(f"{hpa}: its alarm goes {p['fault_level'].upper()} on fault; "
                                      f"--fault-level contradicts the preset")
                a["fault_level"] = p["fault_level"]
                out.write(f"    HPA alarm: {p['fault_level'].upper()} = fault (from the preset)\n")
            else:
                get("fault_level", "Is the alarm HIGH or LOW on fault? (high / low; from the HPA datasheet)",
                    base["fault_level"], required=(hpa != "custom" and base.get("hpa") != hpa))

    get("level_shifter", "Is there a pull-up level shifter (BOB-12009 / BSS138, 3.3 -> 5 V) between "
                         "the G40 and the HPA/switch? (pullup / none)", base["level_shifter"])
    get("watchdog_ms", "Watchdog: drop the HPA after how many ms with no TX send? (0 = off)",
        base["watchdog_ms"])
    return _finish(a)


def _finish(a: dict) -> dict:
    for k in QUESTIONS:
        a.setdefault(k, DEFAULTS[k])
    if a["kind"] != "none" and a["switch_pin"] == a["pa_pin"]:
        raise AnswerError(f"socket pin {a['pa_pin']} cannot be both the HPA enable and the switch")
    a["tr_mode"] = "proxy" if a["switch_pin"] in PROXY_PINS else "gpio"
    return a


# YAML
def _flow(d: dict) -> str:
    def v(x):
        if isinstance(x, bool):
            return "true" if x else "false"
        if isinstance(x, (list, tuple)):
            return "[" + ", ".join(v(i) for i in x) + "]"
        if isinstance(x, (int, float)):
            return f"{x:g}" if isinstance(x, float) else str(x)
        s = str(x)
        return s if s and all(c.isalnum() or c in "._-/:" for c in s) and s.lower() not in (
            "true", "false", "yes", "no", "on", "off", "null", "none") else '"' + s.replace('"', '\\"') + '"'
    return "{" + ", ".join(f"{k}: {v(x)}" for k, x in d.items()) + "}"


def _num_out(x):
    return int(x) if float(x) == int(x) else float(x)


def _line_keys(pin: int) -> dict:
    name, chip, pad, _off, dirl = ORIN_LINES[pin]
    d = {"chip": chip, "line": name, "fallback_line": pad}
    if dirl:
        d.update({"dir_chip": dirl[1], "dir_line": dirl[0], "dir_fallback_line": dirl[2]})
    return d


def atten_section(spec: str | None, current: rf.FrontendConfig | None = None) -> dict:
    """The atten: mapping for ``--atten SPEC`` (none | auto | hidraw[:/dev/hidrawN] |
    http://ip).  No spec: the current file's section as it is, or auto for a new
    file.  max_db, step_db and startup_db always come from the current file."""
    cur = (current or rf.FrontendConfig()).to_dict()["atten"]
    if spec is None:
        if current is not None:
            return dict(cur)
        spec = "auto"
    s = str(spec).strip()
    low = s.lower()
    at = {k: cur[k] for k in ("max_db", "step_db", "startup_db") if k in cur}
    if low == "none":
        return {"transport": "none", "path": cur.get("path", "auto"), **at}
    if low == "auto":
        return {"transport": "auto", "path": "auto", **at}
    if low.startswith("hidraw"):
        path = s.split(":", 1)[1].strip() if ":" in s else "auto"
        if path != "auto" and not path.startswith("/dev/hidraw"):
            raise AnswerError(f"--atten hidraw path must be /dev/hidrawN, got {path!r}")
        return {"transport": "hidraw", "path": path, **at}
    if low.startswith("http://") or low.startswith("https://"):
        if len(s.split("//", 1)[1].strip("/")) == 0:
            raise AnswerError("--atten http needs an address, e.g. http://192.168.0.40")
        return {"transport": "http", "url": s.rstrip("/"), **at}
    raise AnswerError(f"--atten must be none, auto, hidraw[:/dev/hidrawN] or http://ip, got {spec!r}")


def ports_from_args(port_flags, clear: bool):
    """The ports: map from --port JACK=ROLE[:CHAIN] flags, {} for --ports-clear,
    None when neither was given (keep the current file's)."""
    if clear and port_flags:
        raise AnswerError("--ports-clear and --port contradict each other")
    if clear:
        return {}
    if not port_flags:
        return None
    sec = {}
    for spec in port_flags:
        jack, sep, rest = str(spec).partition("=")
        jack = jack.strip()
        if not sep or not jack:
            raise AnswerError(f"--port needs JACK=ROLE[:CHAIN], got {spec!r}")
        role, _, chain = rest.partition(":")
        match = [j for j in _port_map.G40_JACKS if j.lower() == jack.lower()]
        if not match:
            raise AnswerError(f"--port {jack}: a G40 has {', '.join(_port_map.G40_JACKS)}")
        sec[match[0]] = {"role": role.strip().lower(), "chain": chain.strip()}
    try:
        return _port_map.parse(sec)
    except ValueError as e:
        raise AnswerError(f"--port: {e}")


def build_yaml(a: dict, current: rf.FrontendConfig | None = None, stamp: str | None = None,
               atten: str | None = None, ports_stamp: str | None = None,
               ports: dict | None = None) -> str:
    """rf_frontend.yaml text for a complete answer set.  settle/hang times and the
    attenuator section come from ``current``; ``atten`` (the --atten spec)
    replaces the attenuator section, see atten_section.  The port map is ``ports``
    when given (--port / --ports-clear), else kept from ``current``; ``ports_stamp``
    is its "last set by" line."""
    at_sec = atten_section(atten, current)
    cur = current or rf.FrontendConfig()
    stamp = stamp or _dt.datetime.now().strftime("%Y-%m-%d")
    a = _finish(dict(a))
    fault_on = a["fault_pin"] != "none"
    fault_low = (a["fault_level"] == "low") if fault_on else cur.pa_fault_in.active_low
    pa = {**_line_keys(a["pa_pin"]), "active_high": bool(a["pa_active_high"]),
          "settle_ms": _num_out(cur.pa_enable.settle_ms), "hang_ms": _num_out(cur.pa_enable.hang_ms)}
    if a["tr_mode"] == "gpio":
        tr = {"mode": "gpio", **_line_keys(a["switch_pin"]), "tx_level": int(a["tr_tx_level"]),
              "settle_ms": _num_out(cur.tr_switch.settle_ms)}
    else:
        tr = {"mode": "proxy", "tx_level": int(a["tr_tx_level"]),
              "settle_ms": _num_out(cur.tr_switch.settle_ms)}
    fi = {"enabled": fault_on, **_line_keys(18), "active_low": bool(fault_low)}
    at = {k: (_num_out(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v)
          for k, v in at_sec.items()}
    pa_sig = SOCKET_TABLE[a["pa_pin"]][0]
    meta = " ".join(f"{k}={a[k]}" for k in ("hpa", "switch", "switch_pin", "level_shifter"))
    lines = [
        f"# rf_frontend.yaml -- generated by ./deploy.sh --frontend (frontend_setup.py) {stamp}.",
        f"# HPA: {HPA_PRESETS[a['hpa']]['label']}.  Switch: {SWITCH_PRESETS[a['switch']]['label']}.",
        "# Re-run ./deploy.sh --frontend to change it; every key: rf_frontend.example.yaml.",
        f"# frontend_setup: {meta}",
        "# FAIL-SAFE: a hard kill leaves the HPA enable pin at its last level and every Empower",
        "# HPA is ON with its control open: the interface must make an open control mean HPA OFF.",
        "config_version: 1",
        f"frontend: {a['kind']}",
        f"# HPA enable on socket pin {a['pa_pin']} ({pa_sig}); line {'HIGH' if a['pa_active_high'] else 'LOW'} = HPA on"
        + ("; direction line PCC.03 held HIGH (output)" if a["pa_pin"] == 16 else ""),
        f"pa_enable:   {_flow(pa)}",
        (f"# TX/RX switch on socket pin {a['switch_pin']} ({SOCKET_TABLE[a['switch_pin']][0]}); "
         f"level {a['tr_tx_level']} = TX" + ("; direction line PCC.03 held HIGH" if a["switch_pin"] == 16 else "")
         if a["tr_mode"] == "gpio" else
         f"# TX/RX switch from socket pin {a['switch_pin']} ({SOCKET_TABLE[a['switch_pin']][0]}, "
         f"{PROXY_PINS[a['switch_pin']]}): NV100 TR_N proxy, NOT YET SCOPED; no line driven"),
        f"tr_switch:   {_flow(tr)}",
        (f"# HPA alarm on socket pin 18 (CPU_GP_IN); fault = line {'LOW' if fault_low else 'HIGH'}"
         if fault_on else "# HPA alarm input (socket pin 18) not wired: disabled"),
        f"pa_fault_in: {_flow(fi)}",
        f"watchdog_ms: {_num_out(a['watchdog_ms'])}",
        ("# RX attenuator: found on USB when plugged in, read only on attach (no dB written)"
         if at.get("transport") == "auto" else None),
        f"atten:       {_flow(at)}",
    ]
    lines = [ln for ln in lines if ln is not None]
    # The I2C ADC sections are not asked for here either; carry each over from the
    # current file when it is enabled, so a rerun keeps the switch readback and the
    # HPA temperature / current monitor.
    cur_d = cur.to_dict()
    for sec, what in (("tr_readback", "T/R switch analog readback through the I2C ADC"),
                      ("hpa_monitor", "HPA temperature + supply current through the I2C ADC")):
        if getattr(cur, sec).enabled:
            d = {k: (_num_out(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v)
                 for k, v in cur_d[sec].items()}
            d["address"] = f"0x{int(cur_d[sec]['address']):02x}"
            lines += [f"# {what}; kept from the current file",
                      f"{sec + ':':<13}{_flow(d)}"]
    # The payload guard section is not asked for here; carry it over from the
    # current file so a rerun never drops the RX protection check.
    pl = {k: (_num_out(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v)
          for k, v in (cur.payload or {}).items() if v is not None}
    if pl:
        lines += ["# RX protection check on every HPA key (payload_guard.py); kept from the current file",
                  f"payload:     {_flow(pl)}"]
    text = "\n".join(lines) + "\n"
    # The port map is not asked for here either: it comes from --port / --ports-clear,
    # else from the current file, so a rerun never unlocks the jacks.
    pm = cur.ports if ports is None else ports
    if pm:
        text = rf.replace_ports_block(text, pm, ports_stamp)
    return text


# validation
class _G40Gpio(rf.MemoryGpio):
    """MemoryGpio with the G40 R36 line table: pad names and offsets only (that device
    tree has no logical names, so a line resolves the same way it will on the radio),
    and only the socket lines (plus the pin-16 direction line) this tool knows."""

    def __init__(self):
        names = {}
        for _pin, (_n, chip, pad, off, dirl) in ORIN_LINES.items():
            names.setdefault("/dev/" + chip, {})[pad] = off
            if dirl:
                names.setdefault("/dev/" + dirl[1], {})[dirl[2]] = dirl[3]
        super().__init__(names=names)

    def open_line(self, chip, spec, fallback, direction, active_high, initial_active=False):
        for cand in (spec, fallback):
            kind, val = rf.parse_line_spec(cand)
            key = (rf.chip_path(chip), val)
            if kind == "offset" and key not in _BY_LINE and key not in _DIR_LINES:
                raise rf.FrontendError(f"offset {val} on {chip} is not a G40 socket line "
                                       f"this tool knows (pins 15/16/17/18)")
        return super().open_line(chip, spec, fallback, direction, active_high, initial_active)


def validate_text(text: str) -> tuple[rf.FrontendConfig, list]:
    """Parse ``text`` with rf_frontend's own loader, then open it against the G40 line
    table.  Returns (config, [(role, socket pin, chip, offset, resolved_by)]).
    Raises rf.ConfigError / rf.FrontendError with the reason."""
    fd, path = tempfile.mkstemp(suffix=".yaml", prefix="rf_frontend_check_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        cfg = rf.load_config(path, required=True)
    finally:
        os.unlink(path)
    cfg.source = "generated"
    resolved = []
    if cfg.frontend == "none":
        return cfg, resolved
    fe = rf.RfFrontend(cfg, gpio=_G40Gpio(), watchdog_poll_s=3600)
    try:
        fe.open()
        for role, h, want in (("HPA enable", fe._pa, OUT_PINS), ("TX/RX switch", fe._tr, OUT_PINS),
                              ("HPA alarm", fe._fault, FAULT_PINS)):
            if h is None:
                continue
            pin = _BY_LINE.get((h.chip, h.offset))
            if pin not in want:
                raise rf.FrontendError(f"{role} resolves to {h.chip}:{h.offset} (socket pin {pin}), "
                                       f"which cannot be used as a{'n input' if want == FAULT_PINS else 'n output'}")
            if pin == 16 and not fe._dirs:
                raise rf.FrontendError(f"{role} on socket pin 16 needs dir_line cpu-gpio-direction "
                                       f"(PCC.03) so its translator is set to output")
            resolved.append((role, pin, h.chip, h.offset, h.resolved_by))
        for h in fe._dirs:
            if (h.chip, h.offset) not in _DIR_LINES:
                raise rf.FrontendError(f"direction line resolves to {h.chip}:{h.offset}, not PCC.03")
            resolved.append(("direction", 16, h.chip, h.offset, h.resolved_by))
        if fe._pa.is_active():
            raise rf.FrontendError("HPA enable would open keyed")
        if fe._tr is not None and fe._tr.is_active() != fe._tr_level(False):
            raise rf.FrontendError("TX/RX switch would open at the TX level")
    finally:
        fe.close()
    return cfg, resolved


def warnings_for(cfg: rf.FrontendConfig, meta: dict | None = None) -> list:
    meta = meta or {}
    w = []
    if cfg.frontend == "none":
        return w
    hpa, shifter = meta.get("hpa"), meta.get("level_shifter")
    pa_pin = pin_of_spec(cfg.pa_enable.chip, cfg.pa_enable.line, cfg.pa_enable.fallback_line)
    if hpa == "empower-1189":
        w.append("Empower 1189: its Shutdown input (DB-9 pin 5) is 5 V TTL. " +
                 ("The pull-up shifter supplies the 5 V level." if shifter == "pullup" else
                  f"Put a level shifter / driver between socket pin {pa_pin} (3.3 V) and the HPA; "
                  f"do not wire it directly."))
    if hpa in ("empower-1208", "empower-1211"):
        w.append(f"{hpa}: polarity is NOT on file; the values in this file are what you entered. "
                 f"Check them against the datasheet before connecting the HPA.")
    if shifter == "pullup":
        w.append("Pull-up level shifter (BOB-12009 / BSS138): the HPA-side and switch-side lines read "
                 "HIGH whenever the G40 is off, booting, or its pad is not driven (before the boot "
                 "config runs). The HPA must be OFF when its control is HIGH, or the interface needs "
                 "an inverting or pull-down stage.")
        if not cfg.pa_enable.active_high:
            w.append("This HPA's enable is active-LOW (HIGH = off), which is consistent with that.")
        if cfg.pa_enable.active_high:
            w.append("DANGER: with this shifter and an active-HIGH enable, the HPA turns ON whenever the G40 "
                     "is off or booting. Add an inverting or pull-down stage before connecting it.")
        if cfg.tr_switch.mode == "gpio" and cfg.tr_switch.tx_level == 1:
            w.append("DANGER: with this shifter and tx_level 1, an undriven switch control selects TX. "
                     "Strap/invert it so an open control means RX.")
        w.append("Two bidirectional translators in series (the G40 carrier's own plus the shifter): "
                 "check the levels and edges at the HPA and switch on a scope before relying on them.")
    if 16 in (pa_pin, pin_of_spec(cfg.tr_switch.chip, cfg.tr_switch.line, cfg.tr_switch.fallback_line)):
        w.append("Socket pin 16 (cpu-gpio) needs its direction line PCC.03 held HIGH; that needs the "
                 "rf_frontend.py from this tree running on the radio (deploy it and restart dpmg_svc). "
                 "Offsets PP.06=98 / PCC.03=15 are from the tegra234 layout, not measured.")
    if not cfg.pa_enable.active_high and shifter != "pullup":
        w.append("Active-low HPA enable: the G40 holds the pin HIGH (HPA off) only while the service "
                 "holds it. With the control open the HPA is ON, so the interface must pull the "
                 "shutdown/disable input to its OFF level (pull-up to the HPA's logic rail).")
    if cfg.tr_switch.mode == "proxy":
        w.append("TX/RX switch in proxy mode: the NV100 TR_N lines are NOT YET SCOPED (polarity, "
                 "lead time unknown); nothing in software drives the switch and the HPA/switch "
                 "interlock is not enforced. Scope pins 7-10 during a burst before relying on it.")
    elif cfg.tr_switch.tx_level == 0 and shifter != "pullup":
        w.append("Switch tx_level 0: a LOW (or pulled-down / open) control selects TX. Strap the "
                 "switch control with a pull-UP so an open control means RX.")
    if cfg.watchdog_ms == 0:
        w.append("watchdog_ms 0: the HPA watchdog is OFF; a stalled TX loop keeps the HPA keyed.")
    if cfg.frontend == "mock":
        w.append("frontend mock: no GPIO is touched; the sequencing runs in memory only.")
    return w


def failsafe_text(cfg: rf.FrontendConfig | None = None) -> str:
    pin, active_high = 17, True
    if cfg is not None:
        pin = pin_of_spec(cfg.pa_enable.chip, cfg.pa_enable.line, cfg.pa_enable.fallback_line) or pin
        active_high = cfg.pa_enable.active_high
    fix = ("an external buffer with an input pull-down (10-47 kOhm) at the HPA end, so an open or\n"
           "!!   undriven control means HPA OFF" if active_high else
           "an interface that pulls the HPA's shutdown/disable input to its OFF level (HIGH,\n"
           "!!   pull-up to its logic rail) when the control is open. A pull-down would turn THIS\n"
           "!!   HPA ON")
    return (
        f"!! FAIL-SAFE (read before connecting an HPA)\n"
        f"!!   A hard kill of the service (SIGKILL, OOM, crash, docker kill) leaves socket pin {pin}\n"
        f"!!   (HPA enable) driven at its LAST level: the Tegra pad stays a GPIO output after the\n"
        f"!!   line is released. All Empower HPAs turn ON when their control input is open.\n"
        f"!!   REQUIRED: {fix}; and strap the TX/RX switch control so an open\n"
        f"!!   control selects RX.\n")


# reload report
def parse_reload_output(text: str):
    """(status_int, result_dict) from bench `reload` output, or (None, error str)."""
    for line in text.splitlines():
        if line.startswith("reload:"):
            try:
                st, res = ast.literal_eval(line[len("reload:"):].strip())
                return int(st), res
            except Exception as e:
                return None, f"cannot parse reload reply: {e}"
    tail = " | ".join(text.strip().splitlines()[-3:]) if text.strip() else "no output"
    return None, f"no reload reply from the radio ({tail})"


def report(text: str, expect: rf.FrontendConfig | None, out=None) -> bool:
    """Print what the radio resolved.  True = reload OK and matches ``expect``."""
    out = out or sys.stdout
    st, res = parse_reload_output(text)
    if st is None:
        out.write(f"  [XX] reload failed: {res}\n")
        return False
    if st != 0:
        out.write(f"  [XX] reload refused/failed (status {st}): {res.get('err')}\n")
        return False
    status = res.get("status") or {}
    f = status.get("frontend")
    out.write(f"  loaded: {res.get('loaded')}   frontend: {res.get('frontend_kind')}\n")
    ok = True
    if expect is not None and res.get("frontend_kind") != expect.frontend:
        out.write(f"  [XX] radio loaded frontend {res.get('frontend_kind')!r}, expected {expect.frontend!r}\n")
        ok = False
    if f is None:
        out.write("  front end off (frontend: none): no GPIO lines held.\n")
        return ok and (expect is None or expect.frontend == "none")
    pa, tr, fault = f.get("pa", {}), f.get("tr", {}), f.get("fault", {})

    def where(d):
        if "offset" not in d:
            return "not opened"
        pin = _BY_LINE.get((d.get("chip"), d.get("offset")))
        s = f"{d.get('chip')}:{d.get('offset')} (socket pin {pin}, by {d.get('by')})"
        return s + (f" dir={d['dir']}" if "dir" in d else "")
    out.write(f"  HPA enable: {where(pa)}  active_high={pa.get('active_high')}  "
              f"level={pa.get('level')}  keyed={pa.get('keyed')}\n")
    if tr.get("mode") == "gpio":
        out.write(f"  TX/RX sw  : {where(tr)}  tx_level={tr.get('tx_level')}  level={tr.get('level')}"
                  f"  (RX = level {not bool(tr.get('tx_level'))})\n")
    else:
        out.write(f"  TX/RX sw  : mode {tr.get('mode')} (FPGA TR_N; no line driven)\n")
    if fault.get("enabled"):
        out.write(f"  HPA alarm : {where(fault)}  active_low={fault.get('active_low')}  "
                  f"fault now={fault.get('active')}\n")
    else:
        out.write("  HPA alarm : disabled\n")
    out.write(f"  watchdog  : {f.get('watchdog_ms')} ms   last_error: {f.get('last_error')}\n")
    if pa.get("level") is not False or pa.get("keyed"):
        out.write("  [XX] HPA line does not read inactive (level false) after the reload\n")
        ok = False
    else:
        out.write("  [OK] HPA enable reads level false (HPA off)\n")
    if tr.get("mode") == "gpio":
        if tr.get("level") is not (not bool(tr.get("tx_level"))):
            out.write("  [XX] switch does not read the RX level after the reload\n")
            ok = False
        else:
            out.write(f"  [OK] switch reads level {str(tr.get('level')).lower()} (RX)\n")
    if fault.get("enabled") and fault.get("active"):
        out.write("  [!!] HPA alarm input reads ASSERTED now: keying will be refused until it clears\n")
    if expect is not None:
        want = {"pa.active_high": expect.pa_enable.active_high, "tr.mode": expect.tr_switch.mode,
                "fault.enabled": expect.pa_fault_in.enabled,
                "watchdog_ms": float(expect.watchdog_ms),
                "pa.pin": pin_of_spec(expect.pa_enable.chip, expect.pa_enable.line,
                                      expect.pa_enable.fallback_line)}
        if expect.tr_switch.mode == "gpio":
            want["tr.tx_level"] = expect.tr_switch.tx_level
            want["tr.pin"] = pin_of_spec(expect.tr_switch.chip, expect.tr_switch.line,
                                         expect.tr_switch.fallback_line)
        if expect.pa_fault_in.enabled:
            want["fault.active_low"] = expect.pa_fault_in.active_low
        got = {"pa.active_high": pa.get("active_high"), "tr.mode": tr.get("mode"),
               "tr.tx_level": tr.get("tx_level"), "fault.enabled": fault.get("enabled"),
               "fault.active_low": fault.get("active_low"),
               "watchdog_ms": (float(f["watchdog_ms"]) if f.get("watchdog_ms") is not None else None),
               "pa.pin": _BY_LINE.get((pa.get("chip"), pa.get("offset"))),
               "tr.pin": _BY_LINE.get((tr.get("chip"), tr.get("offset")))}
        if expect.frontend == "mock":             # mock offsets come from the in-memory table
            want.pop("pa.pin"); want.pop("tr.pin", None)
        bad = {k: (got[k], v) for k, v in want.items() if got[k] != v}
        if bad:
            out.write(f"  [XX] radio config differs from the file (got, want): {bad}\n")
            ok = False
    return ok


# CLI
def _read(path):
    if not path or not os.path.exists(path) or os.path.getsize(path) == 0:
        return None
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _load_current(path):
    text = _read(path)
    if text is None:
        return None, {}
    try:
        return rf.load_config(path, required=True), read_meta(text)
    except rf.ConfigError as e:
        print(f"  (current config not usable for defaults: {e})")
        return None, {}


def _print_checked(text, cfg, resolved):
    print("----- rf_frontend.yaml -----")
    sys.stdout.write(text)
    print("----------------------------")
    print(f"  [OK] parsed by rf_frontend.py: frontend={cfg.frontend}, HPA active_high="
          f"{cfg.pa_enable.active_high}, switch mode={cfg.tr_switch.mode} tx_level="
          f"{cfg.tr_switch.tx_level}, alarm={'on' if cfg.pa_fault_in.enabled else 'off'}"
          f"{(' active_low=' + str(cfg.pa_fault_in.active_low)) if cfg.pa_fault_in.enabled else ''}"
          f", watchdog={cfg.watchdog_ms:g} ms")
    for role, pin, chip, off, by in resolved:
        what = "PCC.03 direction for pin 16" if role == "direction" else f"socket pin {pin}"
        print(f"  [OK] {role:<12} -> {what} = {chip}:{off} (resolves by {by}, as on the R36 G40)")
    for jack in _port_map.ordered(cfg.ports):
        e = cfg.ports[jack]
        print(f"  [OK] port {jack:<6} {_port_map.ROLE_TEXT[e['role']]}"
              + (f": {e['chain']}" if e["chain"] else ""))
    for w in warnings_for(cfg, read_meta(text)):
        print(f"  !! {w}")
    print(failsafe_text(cfg), end="")


ANSWER_FLAGS = {  # flag dest: answer key
    "kind": "kind", "hpa": "hpa", "pa_pin": "pa_pin", "pa_active_high": "pa_active_high",
    "switch": "switch", "switch_pin": "switch_pin", "tr_tx_level": "tr_tx_level",
    "fault_pin": "fault_pin", "fault_level": "fault_level", "level_shifter": "level_shifter",
    "watchdog_ms": "watchdog_ms",
}


def _given_from_args(args) -> dict:
    raw = {k: getattr(args, d) for d, k in ANSWER_FLAGS.items() if getattr(args, d, None) is not None}
    if args.pa_active_low:
        if raw.get("pa_active_high") not in (None, "false", "no", "n", "0"):
            raise AnswerError("--pa-active-low contradicts --pa-active-high")
        raw["pa_active_high"] = "false"
    if args.pa_fault is not None:                   # shorthand: none | high | low
        pf = str(args.pa_fault).strip().lower()
        if pf in ("none", "no", "off"):
            raw.setdefault("fault_pin", "none")
        elif pf in ("high", "low"):
            raw.setdefault("fault_pin", "18")
            raw.setdefault("fault_level", pf)
        else:
            raise AnswerError(f"--pa-fault must be none, high or low, got {args.pa_fault!r}")
    given = {k: parse_answer(k, v) for k, v in raw.items()}
    if args.tr_mode is not None:                    # shorthand: gpio | proxy
        m = str(args.tr_mode).strip().lower()
        if m not in rf.TR_MODES:
            raise AnswerError(f"--tr-mode must be gpio or proxy, got {args.tr_mode!r}")
        pin = given.get("switch_pin")
        if m == "proxy":
            if pin is not None and pin not in PROXY_PINS:
                raise AnswerError(f"--tr-mode proxy needs a TR_N pin (7-10), not {pin}")
            given.setdefault("switch_pin", 9)
        elif pin in PROXY_PINS:
            raise AnswerError(f"--tr-mode gpio cannot use TR_N pin {pin}")
    return given


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="frontend_setup.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate")
    g.add_argument("--out", required=True)
    g.add_argument("--current", default=None)
    g.add_argument("--batch", action="store_true")
    g.add_argument("--kind")
    g.add_argument("--hpa", "--preset", dest="hpa")
    g.add_argument("--pa-pin")
    g.add_argument("--pa-active-high")
    g.add_argument("--pa-active-low", action="store_true")
    g.add_argument("--switch")
    g.add_argument("--switch-pin", "--tr-pin", dest="switch_pin")
    g.add_argument("--tr-tx-level", "--switch-tx-level", dest="tr_tx_level")
    g.add_argument("--fault-pin")
    g.add_argument("--fault-level")
    g.add_argument("--pa-fault")
    g.add_argument("--tr-mode")
    g.add_argument("--level-shifter")
    g.add_argument("--watchdog-ms")
    g.add_argument("--atten", default=None,
                   help="none | auto | hidraw[:/dev/hidrawN] | http://ip (default: keep the "
                        "current file's atten section, auto for a new file)")
    g.add_argument("--port", action="append", metavar="JACK=ROLE[:CHAIN]",
                   help="a jack's lockout, repeat per jack (roles tx, rx, txrx, none)")
    g.add_argument("--ports-clear", action="store_true", help="remove the port lockout")
    c = sub.add_parser("check")
    c.add_argument("file")
    r = sub.add_parser("report")
    r.add_argument("output")
    r.add_argument("--expect", default=None)
    w = sub.add_parser("warn")
    w.add_argument("file", nargs="?")
    sub.add_parser("pins")
    args = ap.parse_args(argv)

    if args.cmd == "generate":
        cur_cfg, meta = _load_current(args.current)
        try:
            given = _given_from_args(args)
            answers = collect_answers(given, defaults_from_current(cur_cfg, meta) if cur_cfg else None,
                                      batch=args.batch)
            ports = ports_from_args(args.port, args.ports_clear)
            stamp_p = ((rf.read_ports_stamp(args.current) if args.current else None) if ports is None
                       else "deploy.sh --frontend " + _dt.datetime.now().strftime("%Y-%m-%d"))
            text = build_yaml(answers, cur_cfg, atten=args.atten, ports_stamp=stamp_p, ports=ports)
            cfg, resolved = validate_text(text)
        except (AnswerError, rf.FrontendError) as e:
            print(f"  [XX] {e}", file=sys.stderr)
            return 2
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        _print_checked(text, cfg, resolved)
        return 0

    if args.cmd == "check":
        try:
            with open(args.file, "r", encoding="utf-8") as f:
                text = f.read()
            cfg, resolved = validate_text(text)
        except (OSError, rf.FrontendError) as e:
            print(f"  [XX] {args.file}: {e}", file=sys.stderr)
            return 2
        _print_checked(text, cfg, resolved)
        return 0

    if args.cmd == "report":
        with open(args.output, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        expect = rf.load_config(args.expect, required=True) if args.expect else None
        return 0 if report(text, expect) else 1

    if args.cmd == "warn":
        cfg, meta = _load_current(args.file) if args.file else (None, {})
        if cfg is not None:
            for x in warnings_for(cfg, meta):
                print(f"  !! {x}")
        print(failsafe_text(cfg), end="")
        return 0

    if args.cmd == "pins":
        sys.stdout.write(socket_table_text())
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
