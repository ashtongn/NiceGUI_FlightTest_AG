"""
Payload view (payload_tab.py): the "Payload" tab of the engineering view
=========================================================================
Payload-level control and monitoring of the full RF chain around one G40.
The real payload has no coupler and no power meter: G40 TX then HPA then T/R switch
then antenna, and antenna then switch then LNA then RCDAT attenuator then G40 RX (there the
attenuator protects the G40 only; a profile with rx_order atten_first, Payload 2 as
built, has it directly after the switch, so it protects the LNA too).  It is one more tab in
/engineering; the operator console (/) does not show it.  It reuses the process-global LinkManager
(link_tab.MGR), so it talks to the same radio sessions, and it polls the radio
only while its tab is shown.

What it shows: a live block diagram (G40 TX attenuation, HPA keyed / off,
switch position, RX attenuator readback, PA fault input, watchdog), the
expected EIRP for the current settings computed from payload_profiles.yaml
with payload_budget (the same math the workbook uses), and warnings when the
computed HPA input, LNA input or G40 RX input exceed their limits, plus where
each stage starts to saturate (HPA compression region and the TX attenuation at
its P1dB / 3 dB points, LNA saturation by TX leakage).  A measurement panel
logs the bench checkout sweep: commanded and measured levels per frequency
against the expected safe-loopback value.

RX protection is enforced by the service (payload_guard.py via rf_frontend);
the page only adds browser-side checks and explanations on top:
  KEY   = payload_key on: switch: TX, guard confirms switch readback and the
          RX attenuator >= the minimum safe value (raising it to its protect
          value first, readback-confirmed), then HPA on
  UNKEY = payload_key off: HPA off then switch then RX then operator RX attenuation
          restored
Commands used: payload_status / payload_key / payload_restore,
frontend_set / frontend_reload, atten_set / atten_get, set_tx_atten.
"""
from __future__ import annotations

import asyncio
import html
import logging
import time

from pathlib import Path
import re as _re

from nicegui import app, ui

from dpmg.payload import payload_budget as pb
from dpmg.payload import step_eirp as se

# Datasheets: the PDFs in docs/datasheets are served at /datasheets; double-clicking a
# diagram block opens that block's datasheet in a popup window.
from dpmg.paths import REPO_ROOT  # noqa: E402
_DS_DIR = REPO_ROOT / "docs" / "datasheets"
try:
    if _DS_DIR.is_dir():
        app.add_static_files("/datasheets", str(_DS_DIR))
except Exception:                     # route already added (re-import) or no app
    pass
_DS_ROLES = ("g40_tx", "tx_combiner", "hpa", "switch", "antenna", "rx_atten", "rx_splitter",
             "lna", "g40_rx")


def datasheet_links(data: dict, prof_name: str) -> dict:
    """Role: datasheet URL for one profile: the local copy under /datasheets when
    present, else the first http source in the component's entry, else nothing."""
    out = {}
    try:
        raw = (data.get("profiles") or {})[prof_name]
    except Exception:
        return out
    comps = data.get("components") or {}
    for role in _DS_ROLES:
        c = comps.get(raw.get(role) or "") or {}
        f = c.get("datasheet")
        if f and (_DS_DIR / Path(f).name).is_file():
            out[role] = "/datasheets/" + Path(f).name
            continue
        for v in c.values():
            src = v.get("source") if isinstance(v, dict) else None
            m = _re.search(r"https?://\S+", src) if isinstance(src, str) else None
            if m:
                out[role] = m.group(0)
                break
    return out
from dpmg.link import view_sync as _vs
# The operator console's widgets and helpers, reused as-is: the
# TX-attenuation choices + caps dropdown fill (_fill_gain_select), the
# frequency unit table and the EditGuard wiring.
from dpmg.web.link_tab import (MGR, FREQ_UNITS, TX_ATTEN_CHOICES, _CAPS_CACHE, _fetch_caps,
                      _fill_gain_select, _vs_track, caps_family)

log = logging.getLogger("payload_tab")

POLL_S = 1.5
TX_ATTEN_MAX_DB = 41.75                 # NV100 TX attenuation, quarter-dB steps 0..167


def _f(v, fmt="{:.1f}", none="-"):
    try:
        return none if v is None else fmt.format(float(v))
    except (TypeError, ValueError):
        return none


def _e(s) -> str:
    return html.escape(str(s))


# device presence
# What is physically behind each block, read from payload_status: the front-end
# kind (gpio = real GPIO lines driven on the socket pins, mock = simulated,
# none = nothing), the attenuator transport (none = no device), the fault input
# (enabled or not), the payload guard (configured or not) and the service's
# --simulate flag. A block with nothing behind it is drawn dashed/grey so that a
# colour change on the diagram always means a real reading.
DRIVEN, SIM, NONE, DEVICE = "driven", "sim", "none", "device"
PRESENCE_ORDER = ("service", "hpa", "switch", "atten", "fault", "guard")
PRESENCE_TITLES = {"service": "service", "hpa": "HPA", "switch": "switch", "atten": "attenuator",
                   "fault": "fault input", "guard": "guard"}


def device_presence(st: dict | None) -> dict:
    """Pure (tested): payload_status: {block: {"kind": driven|sim|none|device, "text": str}}
    for hpa, switch, atten, fault, guard (+ service when it is a simulator).
    Empty status (no radio yet): {}."""
    st = st or {}
    if not st:
        return {}
    fe = str(st.get("frontend") or "none").lower()
    sim = bool(st.get("sim"))
    tr = st.get("tr") or {}
    fault = st.get("fault") or {}
    at = st.get("atten")
    out = {}

    def line(kind, text):
        return {"kind": kind, "text": text}
    if sim:
        out["service"] = line(SIM, "simulator (--simulate)")
    # HPA enable line
    if fe == "gpio":
        out["hpa"] = line(DRIVEN, "driven (GPIO line)")
    elif fe == "mock":
        out["hpa"] = line(SIM, "simulated (mock front end)")
    else:
        out["hpa"] = line(NONE, "not fitted / no device")
    # T/R switch
    mode = str(tr.get("mode") or "none").lower()
    if fe == "mock":
        out["switch"] = line(SIM, "simulated (mock front end)")
    elif fe == "gpio" and mode == "gpio":
        out["switch"] = line(DRIVEN, "driven (GPIO line)")
    elif fe == "gpio" and mode == "proxy":
        out["switch"] = line(DEVICE, "proxy (no readback)")
    else:
        out["switch"] = line(NONE, "not fitted / no device")
    # RX attenuator
    transport = str((at or {}).get("transport") or "none").lower()
    dv = ((st.get("devices") or {}).get("atten") or {})
    if dv.get("mode") == "auto" and at is None:
        # DEVICE_WATCH: atten.transport auto and nothing attached right now
        out["atten"] = line(NONE, f"auto: {dv.get('state') or 'not connected'}")
    elif dv.get("mode") == "auto" and transport != "mock":
        who = " ".join(str(dv[k]) for k in ("model", "serial") if dv.get(k))
        out["atten"] = line(DEVICE, f"connected ({who or transport})")
    elif at is None or transport == "none":
        out["atten"] = line(NONE, "no device (atten transport none)")
    elif transport == "mock":
        out["atten"] = line(SIM, "simulated (mock attenuator)")
    else:
        out["atten"] = line(DEVICE, f"device ({transport})")
    # PA fault input
    if not fault.get("enabled"):
        out["fault"] = line(NONE, "disabled / not fitted")
    elif fe == "mock":
        out["fault"] = line(SIM, "simulated")
    elif fe == "gpio":
        out["fault"] = line(DRIVEN, "read (GPIO line)")
    else:
        out["fault"] = line(NONE, "not fitted / no device")
    # payload guard (service-side RX protection)
    if st.get("configured"):
        out["guard"] = line(DEVICE, f"configured ({(st.get('guard') or {}).get('profile') or '?'})")
    else:
        out["guard"] = line(NONE, "not configured (no payload: section)")
    return out


# devices row (DEVICE_WATCH)
# payload_status "devices": the attenuator plug-in state (atten.transport auto)
# and the switch readback ADC presence probe. One chip per device.
TONE_OK, TONE_OFF, TONE_WARN = "ok", "off", "warn"
DEVICE_TITLES = {"atten": "RX attenuator", "tr_adc": "switch ADC", "hpa_adc": "HPA monitor"}
_TONE_BY_STATE = {"connected": TONE_OK, "present": TONE_OK,
                  "not connected": TONE_OFF, "lost": TONE_OFF, "off": TONE_OFF,
                  "absent": TONE_OFF, "sim": TONE_OFF, "not fitted": TONE_OFF}


def hpa_monitor_text(st: dict | None) -> tuple:
    """Pure (tested): payload_status: (text, hot) for the HPA monitor readings:
    "41.2 C  1.50 A" when the chip reads, "temp/current not fitted (reason)" when
    it does not, a short note when the reading cannot be believed (HPA off, floating
    inputs, impossible values), "" when no monitor is configured.  hot = above warn_c."""
    hm = (st or {}).get("hpa_monitor") or {}
    if not hm:
        return "", False
    if hm.get("state") != "ok" or hm.get("hpa_temp_c") is None:
        why = hm.get("reason")
        return (f"temp/current not fitted ({why})" if why else "temp/current not fitted"), False
    q = hm.get("quality", "ok")
    if q != "ok":
        return {"hpa off": "off: monitor not valid", "floating": "monitor inputs floating, check the DB9",
                "bad": "monitor: bad reading"}.get(q, f"monitor: {q}"), False
    t, i = float(hm["hpa_temp_c"]), float(hm.get("hpa_current_a") or 0.0)
    return f"{t:.1f} C  {i:.2f} A", bool(hm.get("hot"))


def tr_button_labels(st: dict | None) -> tuple:
    """Pure (tested): payload_status: (TX button text, RX button text) naming the level
    each one puts on the switch line (G40 pin 15) under the radio's tr_switch.tx_level.
    Plain names when the switch is not on a GPIO line or the level is not reported."""
    tr = (st or {}).get("tr") or {}
    if tr.get("mode") != "gpio" or tr.get("tx_level") not in (0, 1):
        return "Switch to TX", "Switch to RX"
    high, low = "TTL high, 3.3 V", "TTL low, 0 V"
    tx_high = int(tr["tx_level"]) == 1
    return (f"Switch to TX ({high if tx_high else low})",
            f"Switch to RX ({low if tx_high else high})")


def device_tone(state) -> str:
    """Green connected / present, grey not connected / lost / absent / off, amber
    for several found, an error, a missing bus or a missing smbus2."""
    return _TONE_BY_STATE.get(str(state or "off"), TONE_WARN)


def device_chips(st: dict | None) -> list:
    """Pure (tested): payload_status to [{"key", "title", "state", "tone", "tip"}].
    An older service without a devices block gives []."""
    dv = (st or {}).get("devices") or {}
    out = []
    a = dv.get("atten")
    if isinstance(a, dict):
        state = str(a.get("state") or "off")
        if state == "off":
            tip = "atten.transport: none (no attenuator in use)"
        else:
            bits = [f"{a.get('model') or 'model ?'} SN {a.get('serial') or '?'}"
                    if (a.get("model") or a.get("serial")) else None,
                    a.get("path"), (f"{a['db']:g} dB" if isinstance(a.get("db"), (int, float)) else None),
                    (f"since {time.strftime('%H:%M:%S', time.localtime(a['since']))}"
                     if isinstance(a.get("since"), (int, float)) else None),
                    f"transport {a.get('mode') or '?'}"]
            tip = ", ".join(b for b in bits if b)
        out.append({"key": "atten", "title": DEVICE_TITLES["atten"], "state": state,
                    "tone": device_tone(state), "tip": tip})
    c = dv.get("tr_adc")
    if isinstance(c, dict):
        state = str(c.get("state") or "off")
        tip = (f"ADS1x15 at {c.get('bus') or '?'} {c.get('address') or '?'} (presence only; "
               "the readback interlock follows tr_readback.enabled)")
        out.append({"key": "tr_adc", "title": DEVICE_TITLES["tr_adc"], "state": state,
                    "tone": device_tone(state), "tip": tip})
    h = dv.get("hpa_adc")
    if isinstance(h, dict):
        # HPA_MONITOR: the 1189's DB9 temperature (pin 3) and supply current (pin 2)
        state = str(h.get("state") or "off")
        text, hot = hpa_monitor_text(st)
        hm = (st or {}).get("hpa_monitor") or {}
        ch = h.get("channels") or []
        if state == "off":
            tip = "hpa_monitor not enabled in rf_frontend.yaml"
        else:
            tip = (f"ADS1x15 at {h.get('bus') or '?'} {h.get('address') or '?'}, temperature on AIN"
                   f"{ch[0] if len(ch) > 0 else '?'} (DB9 pin 3), supply current on AIN"
                   f"{ch[1] if len(ch) > 1 else '?'} (DB9 pin 2)")
            if hm.get("hpa_temp_v") is not None:
                tip += (f"; raw {hm['hpa_temp_v']:.3f} V / {hm.get('hpa_current_v', 0):.3f} V, "
                        f"warn above {hm.get('warn_c', 70):g} C, read {hm.get('age_s', 0):g} s ago")
            elif hm.get("reason"):
                tip += f"; {hm['reason']}"
        tone = TONE_WARN if hot else device_tone(state)
        out.append({"key": "hpa_adc", "title": DEVICE_TITLES["hpa_adc"], "state": state,
                    "tone": tone, "tip": tip, "extra": (text if state in ("present", "sim") else "")})
    return out


def device_changes(prev: dict | None, cur: dict | None) -> list:
    """Pure (tested): [(message, notify type), ...] for devices that appeared or
    went away between two payload_status replies. No previous reply = nothing
    (the first poll after a radio pick does not announce what is already there)."""
    if not prev:
        return []
    before = {c["key"]: c for c in device_chips(prev)}
    out = []
    for c in device_chips(cur):
        b = before.get(c["key"])
        if b is None or b["state"] == c["state"]:
            continue
        if c["tone"] == TONE_OK:
            out.append((f"{c['title']} {c['state']}: {c['tip']}", "positive"))
        elif b["tone"] == TONE_OK:
            out.append((f"{c['title']} {c['state']} (was {b['state']})", "warning"))
        elif c["tone"] == TONE_WARN:
            out.append((f"{c['title']}: {c['state']}", "warning"))
    return out


# radio pick + TX control
def pick_default_radio(serials, family_of=None):
    """Pure (tested): the Radio picker default. A G40 or X40 first: caps family
    'epiq', or while caps are not fetched yet a serial starting G40 / X40. Else
    the first serial in sorted order. None for no radios."""
    ss = sorted(serials or [])
    if not ss:
        return None
    fam = family_of or (lambda s: None)
    for s in ss:
        if fam(s) == "epiq":
            return s
    for s in ss:
        if fam(s) is None and str(s).upper().startswith(("G40", "X40")):
            return s
    return ss[0]


G40_TX_LABEL = "G40 TX atten (dB)"


def tx_control(family=None, caps: dict | None = None) -> dict:
    """Pure (tested): what the TX level control means for the selected radio.

    {"label", "mode": "atten"|"gain", "radio": budget TX model key or None}.
    G40 / X40 (family epiq) and no radio: the G40 attenuation and the G40 output
    model. A B200 (family uhd, kind b200): its TX gain and the b200 model in
    payload_profiles.yaml radios:. Any other caps radio: its own label and mode,
    and no budget model (radio None), so no G40 numbers are computed for it."""
    caps = caps or {}
    if family in (None, "epiq"):
        return {"label": G40_TX_LABEL, "mode": "atten", "radio": "g40"}
    mode = "atten" if (caps.get("tx_gain") or {}).get("mode") == "atten" else "gain"
    kind = str(caps.get("kind") or "").lower()
    if family == "uhd" and kind == "b200":
        return {"label": "B200 TX gain (dB)", "mode": "gain", "radio": "b200"}
    name = caps.get("label") or kind.upper() or str(family)
    return {"label": f"{name} TX {'atten' if mode == 'atten' else 'gain'} (dB)", "mode": mode,
            "radio": None}


def tx_model_kw(txc: dict) -> dict:
    """Extra payload_budget arguments for the TX model: none for the G40 (no
    radio argument), radio=<key> for another modelled radio."""
    r = (txc or {}).get("radio")
    return {} if r in (None, "g40") else {"radio": r}


def saturation_text(sat: dict, tx: dict | None = None) -> str:
    """Pure (tested): the saturation line. G40: the TX attenuation at the HPA
    P1dB / 3 dB points. A gain radio (B200): the TX gain at those points."""
    gain = (tx or {}).get("control") == "gain"
    mx = float((tx or {}).get("max_db", TX_ATTEN_MAX_DB))

    def _v(v):
        if v is None:
            return "-"
        if gain:                         # more gain drives the HPA harder
            if v > mx:
                return f"never (even at {mx:g} dB)"
            if v < 0:
                return f"always (needs {v:.1f} dB)"
            return f"{v:.1f} dB"
        if v < 0:
            return "never (even at 0 dB)"
        if v > TX_ATTEN_MAX_DB:
            return f"always (needs {v:.1f} dB)"
        return f"{v:.1f} dB"
    head = (f"saturation: HPA {sat['hpa_region']}, {sat['hpa_compression_db']:.1f} dB compression, "
            f"{sat['hpa_below_psat_db']:.1f} dB below Psat, input back-off from P1dB "
            f"{sat['hpa_backoff_p1db_db']:+.1f} dB, ")
    if gain:
        return head + (f"{(tx or {}).get('label', 'TX gain')} for HPA P1dB {_v(sat.get('tx_gain_p1db_db'))}, "
                       f"for 3 dB compression {_v(sat.get('tx_gain_3db_db'))}")
    return head + (f"G40 TX atten for HPA P1dB {_v(sat['tx_atten_p1db_db'])}, "
                   f"for 3 dB compression {_v(sat['tx_atten_3db_db'])}")


NO_MODEL_NOTE = ("Payload profiles describe the G40 chain (and the B200); select one of them "
                 "for the budget numbers.")


def tx_set_command(family, caps: dict | None, value_db) -> tuple:
    """Pure (tested): the (command, params) "Set on radio" sends to the selected radio.
    G40 / X40: set_tx_atten in quarter dB. A caps radio goes through the same
    set_tx_atten the operator console and the mission planner use for every
    radio: atten mode sends the value as attenuation, gain mode (B200) sends the
    distance below the top of the gain range, which the UHD backend turns back
    into gain = top - attenuation."""
    v = float(value_db)
    if family in (None, "epiq"):
        return "set_tx_atten", {"qdb": int(round(v * 4))}
    tg = (caps or {}).get("tx_gain") or {}
    if tg.get("mode") == "atten":
        return "set_tx_atten", {"qdb": int(round(v * 4))}
    top = float(tg.get("max", 89.75))
    return "set_tx_atten", {"qdb": int(round(max(0.0, top - v) * 4))}


def follow_tx_value(family, caps: dict | None, st: dict | None):
    """Pure (tested): from payload_status, the value the TX control should show, in
    the control's own unit, or None. The service echoes tx_atten_db: the last
    set_tx_atten (attenuation) or, before one, the init value (gain on a UHD
    radio). For a gain-mode radio an attenuation is turned back into gain."""
    st = st or {}
    v = st.get("tx_atten_db")
    if v is None:
        return None
    v = float(v)
    if family in (None, "epiq"):
        return v
    tg = (caps or {}).get("tx_gain") or {}
    if tg.get("mode") == "atten" or st.get("tx_atten_src") != "set_tx_atten":
        return v
    return round(float(tg.get("max", 89.75)) - v, 2)


# RX attenuator grid
RX_ATTEN_MAX_DB = 120.0
RX_ATTEN_OTHER = "other"                # dropdown key that opens the precise 0.05 dB entry


def rx_atten_step(db, max_db: float = RX_ATTEN_MAX_DB) -> float:
    """Snap to the RCDAT-6G-120H grid: 0.05 dB steps up to 105 dB, 0.10 dB above
    (mc_atten.py), clamped to 0..max_db."""
    v = min(max(float(db), 0.0), float(max_db))
    step = 0.05 if v <= 105.0 else 0.10
    return round(round(v / step) * step, 2)


def rx_atten_key(db) -> str:
    return f"{float(db):.2f}"


def rx_atten_options(min_db=None, extra=(), max_db: float = RX_ATTEN_MAX_DB) -> dict:
    """Pure (tested): the RX attenuation dropdown {key: label}: every 1 dB from 0
    to max_db plus the guard's exact minimum safe value and any extra values
    (readback, protect, saved operator value), each on the device grid, and an
    'other...' entry for a precise 0.05 dB value. Entries under the minimum safe
    attenuation are suffixed '(below min safe)' (the service refuses them while
    the HPA is keyed or the switch is in TX)."""
    vals = {float(v) for v in range(0, int(max_db) + 1)}
    for v in list(extra) + [min_db]:
        if v is not None:
            try:
                vals.add(rx_atten_step(v, max_db))
            except (TypeError, ValueError):
                pass
    out = {}
    for v in sorted(vals):
        lbl = f"{v:g} dB"
        if min_db is not None and v < float(min_db) - 1e-9:
            lbl += " (below min safe)"
        out[rx_atten_key(v)] = lbl
    out[RX_ATTEN_OTHER] = "other... (precise 0.05 dB entry)"
    return out


def presence_lines(st: dict | None) -> list:
    """[(title, kind, text), ...] in display order, for the status strip."""
    pr = device_presence(st)
    return [(PRESENCE_TITLES[k], pr[k]["kind"], pr[k]["text"]) for k in PRESENCE_ORDER if k in pr]


# measurement panel
MEAS_TOL_DB = 3.0                       # default pass band around the expected level


def measurement_check(expected_dbm, measured_dbm, tol_db: float = MEAS_TOL_DB):
    """Pure (tested): True/False if the measured level is within tol_db of the
    expected, or None when either value is missing (nothing to judge yet)."""
    if expected_dbm is None or measured_dbm is None:
        return None
    try:
        return abs(float(measured_dbm) - float(expected_dbm)) <= float(tol_db)
    except (TypeError, ValueError):
        return None


# block diagram
def diagram_svg(st: dict, calc: dict | None, profile_label: str, ds: dict | None = None) -> str:
    """Pure function (tested): service status + payload_budget summary: SVG.
    ``ds``: role: datasheet URL; each linked block opens it on double-click."""
    ds = ds or {}

    def L(role, svg_part):
        url = ds.get(role)
        if not url:
            return svg_part
        return (f'<g data-ds="{_e(url)}" style="cursor:pointer"><title>double-click: datasheet'
                f'</title>{svg_part}</g>')
    st = st or {}
    pa = st.get("pa") or {}
    tr = st.get("tr") or {}
    fault = st.get("fault") or {}
    at = st.get("atten") or {}
    g = st.get("guard") or {}
    e = (calc or {}).get("eirp") or {}
    leak = (calc or {}).get("leak") or {}
    # LNA TX floor: the radio guard's value when it has one, else the computed one
    lf = (calc or {}).get("lna_tx_floor") or {}
    lna_floor = g.get("lna_min_tx_atten_db",
                      lf.get("min_tx_atten_db") if lf.get("needed") else None)
    keyed = bool(pa.get("keyed"))
    tx = bool(tr.get("tx"))
    flags = " ".join(e.get("flags") or [])
    tx_title = {"b200": "B200 TX"}.get(e.get("tx_radio"), "G40 TX")

    def box(x, y, w, h, title, lines, state="idle"):
        # "nodev": nothing physically behind the block: dashed grey, no colour
        # (the figures are still the computed budget, hence the caption)
        if state == "nodev":
            out = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" fill="#ffffff" '
                   f'stroke="#94a3b8" stroke-width="2" stroke-dasharray="6 4" class="nodev"/>',
                   f'<text x="{x + w / 2}" y="{y + 16}" text-anchor="middle" font-size="12" '
                   f'font-weight="bold" fill="#64748b">{_e(title)}</text>']
            for i, ln in enumerate(lines):
                out.append(f'<text x="{x + w / 2}" y="{y + 32 + i * 14}" text-anchor="middle" '
                           f'font-size="11" font-family="monospace" fill="#94a3b8">{_e(ln)}</text>')
            out.append(f'<text x="{x + w - 6}" y="{y + h - 5}" text-anchor="end" font-size="9" '
                       f'font-style="italic" fill="#64748b">no device</text>')
            return "".join(out)
        fill = {"idle": "#f8fafc", "on": "#fee2e2", "ok": "#dcfce7", "warn": "#fef3c7",
                "bad": "#fecaca"}[state]
        stroke = {"idle": "#64748b", "on": "#dc2626", "ok": "#16a34a", "warn": "#d97706",
                  "bad": "#b91c1c"}[state]
        out = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" fill="{fill}" '
               f'stroke="{stroke}" stroke-width="2"/>',
               f'<text x="{x + w / 2}" y="{y + 16}" text-anchor="middle" font-size="12" '
               f'font-weight="bold" fill="#0f172a">{_e(title)}</text>']
        for i, ln in enumerate(lines):
            out.append(f'<text x="{x + w / 2}" y="{y + 32 + i * 14}" text-anchor="middle" '
                       f'font-size="11" font-family="monospace" fill="#1e293b">{_e(ln)}</text>')
        return "".join(out)

    def arrow(x1, y1, x2, y2, live=False, rx=False):
        c = ("#16a34a" if rx else "#dc2626") if live else "#94a3b8"
        return (f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{c}" '
                f'stroke-width="{3 if live else 2}" marker-end="url(#ah)"/>')

    def port_label(x, y, text, anchor="middle"):
        return (f'<text x="{x}" y="{y}" text-anchor="{anchor}" font-size="11" font-weight="bold" '
                f'fill="#0f172a">{_e(text)}</text>')

    def rx_splitter_part():
        """The RX splitter block and one arrow per output into the G40 RX block,
        each named with its jack when the profile records them."""
        out = [arrow(slot2[0], 278, 295, 278, not tx, rx=True),
               L("rx_splitter", box(215, 240, 80, 76, "RX splitter",
                                    [f"{_f(split_db, '{:.2f}')} dB", "per port"]))]
        for i, y in enumerate((252, 278, 304)):
            out.append(arrow(215, y, 145, y, not tx, rx=True))
            if i < len(rx_jacks) and len(rx_jacks) > 1:
                out.append(f'<text x="192" y="{y - 4}" text-anchor="middle" font-size="10" '
                           f'fill="#334155">{_e(rx_jacks[i])}</text>')
        return "".join(out)

    # atten_first puts the RX attenuator next to the switch and the LNA after it.
    # With an RX splitter the row also has to fit that box and the jack arrows.
    atten_first = (calc or {}).get("rx_order") == "atten_first"
    split_db = (calc or {}).get("rx_splitter_db")
    slot1, slot2, g40rx_x, g40rx_w = ((505, 182), (320, 160), 10, 130) if split_db is not None \
        else ((460, 180), (240, 180), 40, 150)
    (att_x, att_w), (lna_x, lna_w) = (slot1, slot2) if atten_first else (slot2, slot1)
    # the profile's cabling record: which G40 jacks, which switch port faces what
    jacks = (calc or {}).get("g40_ports") or {}
    tx_jacks, rx_jacks = jacks.get("tx") or [], jacks.get("rx") or []
    sw_ports = (calc or {}).get("switch_ports") or {}
    # the fixed pad between the G40 and the HPA, in line when the profile has one
    pad_db = e.get("fixed_pad_db") or 0
    g40tx_w, hpa_x, hpa_w = (120, 240, 170) if pad_db else (150, 200, 175)
    if tx_jacks and e.get("tx_radio") != "b200":
        tx_title = f"{tx_title} {tx_jacks[0]}" if len(tx_jacks) == 1 else tx_title
    else:
        tx_jacks = []
    # a switch with no CW rating on file says so in its block
    sw_lines = [f"TX in {_f(e.get('switch_in_dbm'))} dBm", f"IL {_f(e.get('switch_il_db'), '{:.2f}')} dB",
                f"iso {_f(leak.get('switch_isolation_db'))} dB  [{tr.get('mode', '?')}]"]
    if e and e.get("switch_max_dbm") is None:
        sw_lines.append("CW rating unknown")
    sw_h = 34 + len(sw_lines) * 14
    region = ((calc or {}).get("saturation") or {}).get("hpa_region")
    hpa_text, hpa_hot = hpa_monitor_text(st)      # HPA_MONITOR: temperature + current
    hpa_state = "bad" if "HPA input" in flags else (
        "warn" if (region in ("past P1dB", "SATURATED") or hpa_hot) else ("on" if keyed else "idle"))
    sw_state = "bad" if "switch" in flags else ("on" if tx else "ok")
    lna_state = "bad" if (leak and not leak.get("lna_ok")) else "idle"
    g40rx_state = "bad" if (leak and not leak.get("g40_ok") and tx) else "idle"
    att_state = "idle"
    if at.get("att_db") is not None and g.get("min_db") is not None:
        att_state = "ok" if float(at["att_db"]) >= float(g["min_db"]) - 0.05 else "warn"
    # nothing behind the block: no colour at all (device_presence decides)
    pres = device_presence(st)
    if pres.get("hpa", {}).get("kind") == NONE:
        hpa_state = "nodev"
    if pres.get("switch", {}).get("kind") == NONE:
        sw_state = "nodev"
    if pres.get("atten", {}).get("kind") == NONE:
        att_state = "nodev"
    parts = [
        '<svg viewBox="0 0 980 330" xmlns="http://www.w3.org/2000/svg" '
        'style="width:100%;max-width:980px;background:white">',
        '<defs><marker id="ah" markerWidth="8" markerHeight="8" refX="7" refY="4" '
        'orient="auto"><path d="M0,0 L8,4 L0,8 z" fill="#64748b"/></marker></defs>',
        f'<text x="10" y="18" font-size="13" fill="#334155">profile {_e(profile_label)}'
        f', {_f(e.get("freq_mhz"), "{:.1f}")} MHz</text>',
        # TX row: G40 TX, [pad], HPA, T/R switch, antenna (no coupler, no meter)
        L("g40_tx", box(10, 40, g40tx_w, 90 if len(tx_jacks) > 1 else 76, tx_title,
                        [(f"gain {_f(e.get('tx_gain_db'), '{:.2f}')} dB" if e.get("tx_control") == "gain"
                          else f"atten {_f(e.get('tx_atten_db'), '{:.2f}')} dB"),
                         f"backoff {_f(e.get('backoff_db'))} dB",
                         f"out {_f(e.get('g40_out_dbm'))} dBm"]
                        + ([" ".join(tx_jacks)] if len(tx_jacks) > 1 else []),
            "on" if st.get("tx_running") else "idle")),
        arrow(10 + g40tx_w, 78, 150 if pad_db else hpa_x, 78, keyed),
        (box(150, 40, 70, 76, "TX pad", [f"{float(pad_db):g} dB"]) + arrow(220, 78, hpa_x, 78, keyed)
         if pad_db else ""),
        # TX combiner between the radio TX port(s) and the HPA, when the profile has one
        (L("tx_combiner", box(20, 150, 170, 48, "TX combiner",
            [f"loss {_f(e.get('tx_combiner_db'), '{:.2f}')} dB (split incl.)"]))
         if e.get("tx_combiner_db") is not None else ""),
        L("hpa", box(hpa_x, 40, hpa_w, 90 + (14 if hpa_text else 0), "HPA " + ("KEYED" if keyed else "off"),
            [f"in {_f(e.get('hpa_in_dbm'))} / max {_f(e.get('hpa_max_input_dbm'))}",
             f"out {_f(e.get('hpa_out_dbm'))} dBm ({_f(e.get('hpa_out_w'), '{:.0f}')} W)",
             f"{region or '-'}  comp {_f(e.get('hpa_compression_db'))} dB",
             f"pin {_f(pa.get('level'), '{:.0f}')}  fault "
             + ("n/a" if not fault.get("enabled") else ("ACTIVE" if fault.get("active") else "ok"))]
            + ([("HOT " if hpa_hot else "") + hpa_text] if hpa_text else []),
            hpa_state)),
        arrow(hpa_x + hpa_w, 78, 470, 78, keyed and tx),
        L("switch", box(470, 40, 160, sw_h, "T/R switch: " + ("TX" if tx else "RX"), sw_lines, sw_state)),
        arrow(630, 78, 700, 78, keyed and tx),
        L("antenna", box(700, 40, 210, 76, "Antenna",
            [f"gain {_f(e.get('ant_gain_dbi'))} dBi",
             f"EIRP {_f(e.get('eirp_dbm'))} dBm / {_f(e.get('eirp_dbw'))} dBW",
             ("radiating" if keyed and tx else "EIRP if keyed")], "on" if keyed and tx else "idle")),
        # switch port labels on the three runs, when the profile records them
        port_label((hpa_x + hpa_w + 470) / 2, 72, f"{sw_ports['tx']} TX") if sw_ports.get("tx") else "",
        port_label(665, 72, f"{sw_ports['antenna']} ant") if sw_ports.get("antenna") else "",
        port_label(556, 40 + sw_h + 16, f"{sw_ports['rx']} RX", "start") if sw_ports.get("rx") else "",
        # RX row (right to left): switch, then the LNA and RX attenuator in the
        # profile's order (rx_order), then the RX splitter if any, then G40 RX
        arrow(550, 40 + sw_h, 550, 240, not tx, rx=True),
        L("lna", box(lna_x, 240, lna_w, 76, "LNA",
            [f"TX leak in {_f(leak.get('lna_in_dbm'))}", f"max in {_f(leak.get('lna_max_input_dbm'))} dBm",
             (f"G40 TX floor {_f(lna_floor, '{:.2f}')} dB" if lna_floor is not None
              else f"gain {_f(leak.get('lna_gain_db'))} dB")], lna_state)),
        arrow(slot1[0], 278, slot2[0] + slot2[1], 278, not tx, rx=True),
        L("rx_atten", box(att_x, 240, att_w, 76, "RX atten RCDAT",
            [f"readback {_f(at.get('att_db'), '{:.2f}')} dB",
             f"min safe {_f(g.get('min_db'), '{:.2f}')} dB",
             ("saved " + _f(g.get("saved_db"), "{:.2f}") + " dB") if g.get("saved_db") is not None
             else ("armed" if g.get("armed") else
                   # attenuator first: it takes the TX leak head on, so show it against its rating
                   (f"TX leak in {_f(leak.get('atten_in_dbm'))} / max {_f(leak.get('atten_max_input_dbm'))}"
                    if atten_first and leak else ""))], att_state)),
        (rx_splitter_part() if split_db is not None
         else arrow(slot2[0], 278, g40rx_x + g40rx_w + 5, 278, not tx, rx=True)),
        L("g40_rx", box(g40rx_x, 232 if split_db is not None else 240, g40rx_w,
                        92 if split_db is not None else 76,
                        "G40 RX " + rx_jacks[0] if len(rx_jacks) == 1 else "G40 RX",
            [f"TX leak {_f(leak.get('g40_in_dbm'))} dBm", f"max {_f(leak.get('g40_max_safe_dbm'))} dBm",
             ""], g40rx_state)),
        "</svg>"]
    return "".join(parts)


# page
def build_payload_page(is_active=None):
    """``is_active``: callable: bool; when given, the status poll runs only while
    it returns True (the engineering view passes "Payload tab selected")."""
    sel = {"serial": None}
    state = {"st": None, "t": 0.0, "busy": False, "data": None, "err": None}
    try:
        state["data"] = pb.load_profiles()
    except Exception as e:
        state["err"] = f"payload_profiles.yaml: {e}"
    names = pb.profile_names(state["data"]) if state["data"] else []
    # Band per profile, so the front-end low and high are clear in the dropdown
    # itself (and the readout below). The switch or antenna sets the low edge on the
    # current builds, the HPA the high.
    prof_bands = {}
    prof_opts = {}
    for _n in names:
        try:
            _z = pb.band_limits(state["data"], pb.profile(state["data"], _n))
            prof_bands[_n] = _z
            prof_opts[_n] = f"{_n}   {_z['lo']:.0f}-{_z['hi']:.0f} MHz"
        except Exception:
            prof_opts[_n] = _n

    def _host():
        return MGR.hosts.get(sel["serial"]) if sel["serial"] else None

    with ui.column().classes("w-full max-w-[90rem] mx-auto gap-3 p-3"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.label("Payload: RF chain, EIRP and RX protection").classes("text-2xl font-bold text-slate-800")
            prof_err = ui.label(state["err"] or "").classes("text-red-700 text-sm")
        # DEVICE_WATCH: one chip per connectable device, live from payload_status
        dev_row = ui.row().classes("w-full gap-2 items-center flex-wrap").props('data-test="payload-devices"')

        # selection + operating point
        with ui.card().classes("w-full"):
            with ui.row().classes("items-end gap-3 flex-wrap"):
                radio_sel = ui.select({}, label="Radio").props("dense outlined").classes("w-56")
                # the bench build named in workbook_defaults, else the first profile
                _dflt = ((state["data"] or {}).get("workbook_defaults") or {}).get("profile")
                prof_sel = ui.select(prof_opts, value=(_dflt if _dflt in names else
                                                       (names[0] if names else None)),
                                     label="Payload profile").props("dense outlined").classes("w-56")
                # same value + unit pair as the operator console's "Center" entry
                freq_in = ui.number("TX freq", value=2200.0, format="%.6f") \
                    .props("dense outlined").classes("w-32")
                funit_in = ui.select(list(FREQ_UNITS.keys()), value="MHz", label="unit") \
                    .props("dense outlined").classes("w-24")
                follow_cb = ui.checkbox("follow radio TX LO", value=True).props("dense")
                # same dropdown as the operator console's "TX atten (dB)": the static
                # hardware-legal list for a G40/X40 (family epiq) or no radio, the
                # radio's own capability list (_fill_gain_select) for caps-driven
                # radios. txat_in is the hidden number mirror the EIRP math and
                # "Set on radio" read (exactly the console's pattern).
                txat_sel = ui.select(dict(TX_ATTEN_CHOICES), value="30", label="G40 TX atten (dB)") \
                    .props("dense outlined options-dense").classes("w-36")
                txat_in = ui.number("G40 TX atten (dB)", value=30.0, min=0, max=TX_ATTEN_MAX_DB,
                                    step=0.25, format="%.2f").props("dense outlined").classes("w-36")
                txat_in.set_visibility(False)
                txat_btn = ui.button("Set on radio").props("dense outline")
                bo_in = ui.number("waveform backoff (dB)", value=3.0, min=0, max=40, step=0.5,
                                  format="%.1f").props("dense outlined").classes("w-40")
                poll_cb = ui.checkbox("live", value=True).props("dense")
            # "follow radio" must not undo a value the operator typed but
            # has not sent yet (otherwise the 1 Hz poll puts the radio's TX atten
            # back before "Set on radio" is pressed).
            # Same rule as the SDR views: a focused field is never overwritten, an
            # edit holds until the radio's value changes or 20 s pass unfocused.
            _guard = _vs.EditGuard()
            _svc_now = {}
            _vs_track(_guard, "freq", freq_in, _svc_now)       # typed field: focus + edits
            _vs_track(_guard, "txat", txat_sel, _svc_now)      # dropdown: picks only
            # the unit is the operator's choice once picked; until then "follow"
            # sets it to the natural unit of the radio's LO (MHz for any G40 LO)
            unit_touched = {"v": False}
            funit_in.on("update:model-value", lambda _e=None: unit_touched.__setitem__("v", True))
            ui.label("Backoff = average power below the G40's full-scale output for the loaded "
                     "waveform (amplitude 0.5 ~ 6 dB; add the waveform PAPR for peak). TX attenuation "
                     "and the RX attenuator readback follow the radio when 'follow' is on and the "
                     "service reports them.") \
                .classes("text-xs text-gray-600")
            # Front-end band of the selected profile: the low and high the whole
            # chain passes, and which part sets each edge.
            band_lbl = ui.label("").classes("text-xs text-sky-800 font-medium") \
                .props('data-test="payload-band"')
            # shown when the selected radio has no TX model in payload_profiles.yaml
            model_lbl = ui.label("").classes("text-xs text-amber-800 font-medium") \
                .props('data-test="payload-model-note"')

        # diagram + readouts
        with ui.card().classes("w-full"):
            svg = ui.html(diagram_svg({}, None, "-"), sanitize=False).classes("w-full")
            # datasheets: double-click a block: its datasheet in a popup window
            svg.on("dblclick", js_handler="(e) => { const g = e.target.closest('[data-ds]'); "
                   "if (g) window.open(g.dataset.ds, '_blank', 'popup,width=950,height=1050'); }")
            ui.label("Double-click a block to open its datasheet.").classes("text-xs text-gray-600")
            # status strip: what is physically behind each block (device_presence)
            strip_row = ui.row().classes("gap-2 flex-wrap items-center").props('data-test="payload-presence"')
            ui.label("Colours (red keyed/TX, green OK, amber compressing or below the minimum, dark red "
                     "over a limit) are shown only for a block with a device or a driven GPIO line "
                     "behind it; a dashed grey block has no device (nothing to read), and "
                     "\"simulated\" means the mock front end of a --simulate service.") \
                .classes("text-xs text-gray-600")
            with ui.row().classes("gap-6 flex-wrap items-start"):
                eirp_lbl = ui.label("EIRP -").classes("text-xl font-mono font-bold")
                guard_lbl = ui.label("guard -").classes("font-mono text-sm")
            sat_lbl = ui.label("").classes("font-mono text-sm text-slate-700")
            warn_box = ui.column().classes("gap-1 w-full")

        # STEP_EIRP: the expected EIRP of each keyed step of the plan open in the planner
        with ui.card().classes("w-full"):
            ui.label("Mission steps: expected EIRP").classes("text-sm font-bold")
            ui.label("Each keyed step of the plan open in the mission planner, on the selected radio and "
                     "profile. The frequency and TX attenuation are the step's own (a blank one takes the "
                     "radio's, marked *). The backoff is measured from the step's waveform, built here the "
                     "way the radio builds it: avg is the average power under full scale, peak is the peak. "
                     "Model values from the profile, not a measurement.").classes("text-xs text-gray-600")
            steps_follow_cb = ui.checkbox("while a step is keyed, the EIRP figure above uses that step's backoff",
                                          value=True).props("dense")
            steps_lbl = ui.label("").classes("text-xs text-sky-800 font-medium") \
                .props('data-test="payload-step-eirp-note"')
            steps_box = ui.column().classes("gap-0 w-full").props('data-test="payload-step-eirp"')

        # controls
        with ui.card().classes("w-full"):
            ui.label("HPA keying (service-enforced RX protection)").classes("text-sm font-bold")
            with ui.row().classes("items-center gap-2 flex-wrap"):
                key_btn = ui.button("KEY HPA").props("color=red")
                unkey_btn = ui.button("UNKEY").props("color=primary")
                safe_btn = ui.button("SAFE").props("color=orange")
                tr_tx_btn = ui.button("Switch to TX").props("outline dense")
                tr_rx_btn = ui.button("Switch to RX").props("outline dense")
                reload_btn = ui.button("Reload front-end config").props("flat dense")
            ui.label("KEY = RX attenuator to its maximum (read back), switch to TX, then HPA on. The "
                     "attenuator stays at its maximum for all of TX, so the RX chain hears nothing "
                     "while transmitting, and the HPA drops if it goes away or reads low. UNKEY = HPA "
                     "off, switch to RX, then your RX attenuation is restored. A TX run started from "
                     "the operator console keys through the same service check.") \
                .classes("text-xs text-orange-800")
            ui.separator()
            ui.label("RX attenuator (RCDAT-6G-120H)").classes("text-sm font-bold")
            with ui.row().classes("items-center gap-2 flex-wrap"):
                # dropdown in the TX-attenuation style: 1 dB grid + the guard's exact
                # minimum safe value + readback/protect/saved values + "other..." for a
                # precise 0.05 dB entry; at_in is the hidden mirror "Set" sends.
                at_sel = ui.select(rx_atten_options(), value=rx_atten_key(30.0), label="RX atten (dB)") \
                    .props("dense outlined options-dense").classes("w-56")
                at_in = ui.number("RX atten (dB)", value=30.0, min=0, max=RX_ATTEN_MAX_DB, step=0.05,
                                  format="%.2f").props("dense outlined").classes("w-32")
                at_in.set_visibility(False)
                at_set_btn = ui.button("Set").props("color=primary dense")
                at_read_btn = ui.button("Read").props("outline dense")
                restore_btn = ui.button("Restore my RX atten").props("outline dense")
            ui.label("While the HPA is keyed or the switch is in TX, any setting below the maximum is "
                     "refused, here and by the service. The readback follows the radio when 'follow' "
                     "is on; a value the list does not offer is added as it is read.") \
                .classes("text-xs text-gray-600")
            _vs_track(_guard, "rxat", at_sel, _svc_now)        # Picks only

        # checkout measurements (bench loopback sweep log)
        with ui.card().classes("w-full"):
            ui.label("Checkout measurements (bench loopback)").classes("text-sm font-bold")
            ui.label("One row per sweep frequency. Expected levels are the safe bench loopback "
                     "(default 40 dB TX attenuation, 0.5 of full scale so about 6 dB backoff, 20 dB "
                     "pad). The power meter column is the coupler-fixture characterization reading; the "
                     "RX column is the loopback reading (dBFS or dBm, your choice). Enter the measured "
                     "values and the row passes when each is within the tolerance of the expected.") \
                .classes("text-xs text-gray-600")
            with ui.row().classes("items-end gap-2 flex-wrap"):
                m_freq = ui.number("freq (MHz)", value=2200.0, format="%.3f") \
                    .props("dense outlined").classes("w-32")
                m_txat = ui.number("cmd TX atten (dB)", value=40.0, min=0, max=TX_ATTEN_MAX_DB,
                                   step=0.25, format="%.2f").props("dense outlined").classes("w-36")
                m_bo = ui.number("backoff (dB)", value=6.0, min=0, max=40, step=0.5, format="%.1f") \
                    .props("dense outlined").classes("w-32")
                m_pad = ui.number("loop pad (dB)", value=20.0, min=0, step=0.5, format="%.1f") \
                    .props("dense outlined").classes("w-32")
                m_tol = ui.number("tol (dB)", value=MEAS_TOL_DB, min=0.1, step=0.5, format="%.1f") \
                    .props("dense outlined").classes("w-28")
                m_add = ui.button("Add row").props("color=primary dense")
                m_here = ui.button("Use page freq / atten").props("outline dense")
                m_clear = ui.button("Clear").props("flat dense")
            meas_box = ui.column().classes("w-full gap-1").props('data-test="payload-meas"')
        log_lbl = ui.label("").classes("font-mono text-xs text-gray-700 whitespace-pre-line")

    events: list[str] = []

    def _log(msg):
        events.insert(0, time.strftime("%H:%M:%S ") + msg)
        del events[8:]
        log_lbl.set_text("\n".join(events))

    # computation (pure, from the profile file)
    def _freq_hz() -> float:
        """The entered TX frequency in Hz (value x unit), as the console does."""
        try:
            return float(freq_in.value or 0) * FREQ_UNITS.get(funit_in.value, 1e6)
        except (TypeError, ValueError):
            return 0.0

    _STRIP_CLS = {DRIVEN: "bg-slate-200 text-slate-800",
                  SIM: "bg-violet-100 text-violet-800",
                  DEVICE: "bg-emerald-100 text-emerald-800",
                  NONE: "border border-dashed border-slate-400 text-slate-500 bg-white"}

    _strip_last = {"v": None}
    _CHIP_CLS = {TONE_OK: "bg-emerald-100 text-emerald-800 border border-emerald-400",
                 TONE_OFF: "bg-slate-100 text-slate-500 border border-slate-300",
                 TONE_WARN: "bg-amber-100 text-amber-800 border border-amber-400"}
    _dev_last = {"v": None}

    def _render_devices(st):
        chips = device_chips(st)
        key = [(c["key"], c["state"], c["tip"], c.get("extra"), c["tone"]) for c in chips]
        if key == _dev_last["v"]:
            return
        _dev_last["v"] = key
        dev_row.clear()
        with dev_row:
            ui.label("Devices:").classes("text-sm font-bold text-slate-700")
            if not chips:
                ui.label("no device report (select a radio; an older service has none)") \
                    .classes("text-xs text-gray-500")
            for c in chips:
                txt = f"{c['title']}: {c['state']}" + (f"  {c['extra']}" if c.get("extra") else "")
                with ui.label(txt).classes(
                        "text-xs font-mono px-2 py-0.5 rounded " + _CHIP_CLS.get(c["tone"], "")) \
                        .props(f'data-test="device-{c["key"]}" data-tone="{c["tone"]}"'):
                    ui.tooltip(c["tip"])

    def _render_strip(st):
        lines = presence_lines(st)
        if lines == _strip_last["v"]:
            return                                  # no DOM churn on every poll
        _strip_last["v"] = lines
        strip_row.clear()
        with strip_row:
            ui.label("fitted:").classes("text-xs text-gray-500")
            if not lines:
                ui.label("no status yet (select a radio)").classes("text-xs text-gray-500")
            for title, kind, text in lines:
                ui.label(f"{title}: {text}").classes(
                    "text-xs font-mono px-2 py-0.5 rounded " + _STRIP_CLS.get(kind, ""))

    def _txc():
        """tx_control for the selected radio (G40 attenuation by default)."""
        h = _host()
        if h is None:
            return tx_control(None, None)
        return tx_control(caps_family(h.serial), _CAPS_CACHE.get(h.serial))

    def _calc():
        data = state["data"]
        if not data or not prof_sel.value:
            return None
        txc = _txc()
        if txc["radio"] is None:
            return None                     # no TX model for this radio: no numbers, not wrong ones
        st = state["st"] or {}
        rx_db = (st.get("atten") or {}).get("att_db")
        try:
            return pb.live_summary(data, prof_sel.value, _freq_hz() / 1e6,
                                   float(txat_in.value or 0), float(bo_in.value or 0),
                                   bool((st.get("pa") or {}).get("keyed")), rx_db,
                                   **tx_model_kw(txc))
        except Exception as e:
            return {"error": str(e)}

    def _render():
        st = state["st"] or {}
        calc = _calc()
        ttx, trx = tr_button_labels(st)          # the level each button sends, per tx_level
        tr_tx_btn.set_text(ttx)
        tr_rx_btn.set_text(trx)
        label = prof_sel.value or "-"
        z = prof_bands.get(prof_sel.value)
        if z:
            f_mhz = _freq_hz() / 1e6
            in_band = z["lo"] <= f_mhz <= z["hi"]
            msg = (f"Front-end band {z['lo']:.0f} to {z['hi']:.0f} MHz "
                   f"(low edge set by the {z['lo_by']}, high by the {z['hi_by']}).")
            if not in_band:
                msg += f"  TX freq {f_mhz:.1f} MHz is outside this band."
            band_lbl.set_text(msg)
            band_lbl.classes(replace="text-xs font-medium "
                             + ("text-rose-700" if not in_band else "text-sky-800"))
        else:
            band_lbl.set_text("")
        if calc and "error" in calc:
            prof_err.set_text(f"profile math: {calc['error']}")
            calc = None
        elif not state["err"]:
            prof_err.set_text("")
        _svg = diagram_svg(st, calc, label, datasheet_links(state["data"] or {}, prof_sel.value or ""))
        if _svg != state.get("svg_last"):      # redraw only on change (keeps double-clicks intact)
            state["svg_last"] = _svg
            svg.set_content(_svg)
        _render_strip(st)
        _render_devices(st)
        txc = _txc()
        model_lbl.set_text("" if txc["radio"] is not None else NO_MODEL_NOTE)
        _rebuild_rx_options()
        e = (calc or {}).get("eirp") or {}
        keyed = bool((st.get("pa") or {}).get("keyed"))
        if not e:
            eirp_lbl.set_text("EIRP -")
        if e:
            eirp_lbl.set_text(f"EIRP {'(radiating)' if keyed and (st.get('tr') or {}).get('tx') else '(if keyed)'} "
                              f"{e['eirp_dbm']:.1f} dBm = {e['eirp_dbw']:.1f} dBW = {e['eirp_w']:.1f} W   "
                              f"HPA out {e['hpa_out_dbm']:.1f} dBm")
        sat = (calc or {}).get("saturation") or {}
        if sat:
            sat_lbl.set_text(saturation_text(sat, (calc or {}).get("tx")))
        else:
            sat_lbl.set_text("")
        g = st.get("guard")
        if not st:
            guard_lbl.set_text("no status yet (select a radio)")
        elif not st.get("configured"):
            guard_lbl.set_text("payload guard NOT configured on the radio (no payload: section in "
                               "rf_frontend.yaml), so keying from this page is refused")
        else:
            gui_min = ((calc or {}).get("min_safe") or {}).get("min_rx_atten_db")
            txt = (f"guard {g.get('profile')}: min safe RX atten {g.get('min_db')} dB "
                   f"(worst {g.get('worst_freq_mhz')} MHz, margin {g.get('margin_db')} dB)  "
                   f"protect {g.get('protect_db')} dB  armed {g.get('armed')}  "
                   f"refusals {g.get('refusals')}")
            if g.get("last_refusal"):
                txt += f"\nlast refusal: {g['last_refusal']}"
            if g.get("config_error"):
                txt += f"\nCONFIG ERROR (keying refused): {g['config_error']}"
            if gui_min is not None and g.get("profile") == prof_sel.value and \
                    abs(float(gui_min) - float(g.get("min_db") or 0)) > 0.1:
                txt += (f"\nGUI profile file gives {gui_min} dB; the radio's payload_profiles.yaml "
                        "differs from this GUI's copy")
            if g.get("profile") and prof_sel.value and g.get("profile") != prof_sel.value:
                txt += f"\nradio is configured for {g.get('profile')}; this page shows {prof_sel.value} (what-if)"
            guard_lbl.set_text(txt)
        warn_box.clear()
        warns = list((calc or {}).get("warnings") or [])
        if st.get("fe_err"):
            warns.append(f"front-end: {st['fe_err']}")
        if (st.get("atten") or {}).get("err"):
            warns.append(f"attenuator: {st['atten']['err']}")
        if (st.get("fault") or {}).get("active"):
            warns.append("PA FAULT input asserted")
        hm = st.get("hpa_monitor") or {}
        notes = list((calc or {}).get("notes") or [])
        if hm.get("hot"):                       # HPA_MONITOR: above warn_c
            warns.append(f"HPA temperature {hm.get('hpa_temp_c')} C is above {hm.get('warn_c'):g} C "
                         "(the 1189 derates from 85 C): let it cool before more TX")
            if not state.get("hpa_hot"):
                ui.notify(f"HPA hot: {hm.get('hpa_temp_c')} C", type="warning")
        elif hm and hm.get("state") != "ok":
            notes.append(f"HPA temperature/current monitor not fitted ({hm.get('reason') or 'no reading'})")
        state["hpa_hot"] = bool(hm.get("hot"))
        with warn_box:
            for w in warns:
                ui.label("Warning: " + w).classes("text-sm text-red-700")
            for n in notes:
                ui.label("Note: " + n).classes("text-sm text-amber-700")

    # service I/O
    async def _cmd(name, params=None, timeout=10.0, quiet=False):
        h = _host()
        if h is None:
            if not quiet:
                ui.notify("no radio selected", type="warning")
            return None, None
        try:
            ack = await h.cmd(name, params or {}, timeout=timeout)
        except Exception as e:
            if not quiet:
                ui.notify(f"{name}: {e}", type="negative")
            return None, None
        r = ack.result or {}
        if ack.status != 0:
            if not quiet:
                ui.notify(f"{name}: {r.get('err')}", type="negative", multi_line=True)
                _log(f"{name} REFUSED: {r.get('err')}")
            return False, r
        return True, r

    async def _poll(force=False):
        if state["busy"] or (not poll_cb.value and not force):
            return
        if is_active is not None and not force and not is_active():
            return
        if _host() is None:
            return
        state["busy"] = True
        try:
            ok, r = await _cmd("payload_status", timeout=4.0, quiet=True)
            if ok:
                # DEVICE_WATCH: a device plugged in or pulled out since the last poll
                for msg, kind in device_changes(state.get("dev_prev"), r):
                    ui.notify(msg, type=kind, multi_line=True)
                    _log(msg)
                state["dev_prev"] = r
                state["st"] = r
                state["t"] = time.monotonic()
                if follow_cb.value:
                    # Follow the radio only where the operator is not
                    # mid-edit (EditGuard: focus, or an unsent edit)
                    if r.get("tx_center_hz"):
                        hz = float(r["tx_center_hz"])
                        _svc_now["freq"] = hz
                        if _guard.may_apply("freq", hz):
                            if not unit_touched["v"]:
                                nat = "MHz" if hz >= 1e6 else ("kHz" if hz >= 1e3 else "Hz")
                                if funit_in.value != nat:
                                    funit_in.set_value(nat)
                            fv = round(hz / FREQ_UNITS.get(funit_in.value, 1e6), 6)
                            if freq_in.value != fv:
                                freq_in.value = fv
                    _h = _host()
                    av = follow_tx_value(caps_family(_h.serial) if _h else None,
                                         _CAPS_CACHE.get(_h.serial) if _h else None, r)
                    if av is not None:
                        _svc_now["txat"] = av
                        if _guard.may_apply("txat", av) and txat_in.value != av:
                            _set_atten_widgets(av)
                    rb = (r.get("atten") or {}).get("att_db")
                    if rb is not None:
                        rb = float(rb)
                        _svc_now["rxat"] = rb
                        if _guard.may_apply("rxat", rb) and float(at_in.value or -1) != rb:
                            _set_rx_widgets(rb)
            _render()
        finally:
            state["busy"] = False

    def _refresh_hosts():
        opts = {s: s for s in sorted(MGR.hosts)}
        if opts != radio_sel.options:
            radio_sel.options = opts
            radio_sel.update()
        if radio_sel.value is None and opts:
            # a G40 / X40 first (epiq caps, or a G40 / X40 serial before caps)
            radio_sel.value = pick_default_radio(opts, caps_family)

    def _atten_key(av: float) -> str:
        """The dropdown key for a dB value (the console's str(d) keys)."""
        for k in list(txat_sel.options) if isinstance(txat_sel.options, dict) else []:
            try:
                if abs(float(k) - av) < 1e-6:
                    return k
            except ValueError:
                pass
        return f"{av:g}"

    def _set_atten_widgets(av: float):
        """Radio readback: hidden mirror + dropdown. A value the list does not
        offer is added as '<v> dB (radio)' so the dropdown never misreports."""
        txat_in.value = av
        key = _atten_key(av)
        opts = dict(txat_sel.options) if isinstance(txat_sel.options, dict) else {}
        if key not in opts:
            opts[key] = f"{av:g} dB (radio)"
            txat_sel.set_options(opts, value=key)
        elif txat_sel.value != key:
            txat_sel.set_value(key)

    def _apply_caps():
        """The operator console's rule. Caps-driven radios (uhd /
        pluto / soapy) get their own supported TX values; a G40/X40 (epiq) or an
        unknown radio keeps the static hardware-legal list."""
        h = _host()
        caps = _CAPS_CACHE.get(h.serial) if h is not None else None
        cur = float(txat_in.value or 0)
        txc = _txc()
        # the label follows the radio: G40 TX atten, or the caps radio's own TX
        # gain / atten (the EIRP math then uses that radio's model, or none)
        txat_sel.props(f'label="{txc["label"]}"')
        txat_in.props(f'label="{txc["label"]}"')
        if caps and caps_family(h.serial) in ("uhd", "pluto", "soapy"):
            tg = caps.get("tx_gain") or {}
            txat_in.min = float(tg.get("min", 0.0))
            txat_in.max = float(tg.get("max", 89.75))
            txat_in.props(f'step={float(tg.get("step", 0.25)):g}')
            _fill_gain_select._kind, _fill_gain_select._caps = caps.get("kind"), caps
            _fill_gain_select(txat_sel, txat_in, caps.get("tx_gain_values"), tg)
        else:
            txat_in.min, txat_in.max = 0.0, TX_ATTEN_MAX_DB
            txat_in.props("step=0.25")
            txat_sel.set_options(dict(TX_ATTEN_CHOICES), value=None)
            _set_atten_widgets(min(max(cur, 0.0), TX_ATTEN_MAX_DB))
        _render()

    def _on_radio(e):
        sel["serial"] = e.value
        state["st"] = None
        state["dev_prev"] = None                 # a new radio: do not announce its devices
        h = _host()
        if h is not None and h.serial not in _CAPS_CACHE:
            asyncio.create_task(_fetch_caps(h, on_done=_apply_caps))
        _apply_caps()

    def _on_atten_pick(e):
        if e.value is None:
            return
        try:
            txat_in.value = float(e.value)
        except (TypeError, ValueError):
            return
        _render()

    # RX attenuator dropdown
    def _rx_extra():
        st = state["st"] or {}
        g = st.get("guard") or {}
        return [float(at_in.value or 0), g.get("protect_db"), g.get("saved_db"),
                (st.get("atten") or {}).get("att_db")]

    def _rx_min():
        return ((state["st"] or {}).get("guard") or {}).get("min_db")

    def _rebuild_rx_options():
        opts = rx_atten_options(_rx_min(), _rx_extra())
        if opts != at_sel.options:
            at_sel.set_options(opts, value=rx_atten_key(float(at_in.value or 0)))

    def _set_rx_widgets(v: float):
        """A value (readback, Read, precise entry): hidden mirror + dropdown."""
        v = rx_atten_step(v)
        at_in.value = v
        _rebuild_rx_options()
        key = rx_atten_key(v)
        if at_sel.value != key:
            at_sel.set_value(key)

    async def _rx_other():
        with ui.dialog() as dlg, ui.card():
            ui.label("Precise RX attenuation").classes("text-lg font-bold")
            ui.label("0.05 dB steps up to 105 dB, 0.10 dB above (RCDAT-6G-120H).") \
                .classes("text-xs text-gray-600")
            num = ui.number("precise RX atten (dB)", value=float(at_in.value or 0), min=0,
                            max=RX_ATTEN_MAX_DB, step=0.05, format="%.2f") \
                .props("dense outlined").classes("w-40")
            with ui.row():
                ui.button("Use", on_click=lambda: dlg.submit(num.value)).props("color=primary")
                ui.button("Cancel", on_click=lambda: dlg.submit(None)).props("flat")
        v = await dlg
        if v is None:
            _set_rx_widgets(float(at_in.value or 0))          # back to the previous pick
            return
        _set_rx_widgets(float(v))

    async def _on_rx_pick(e):
        if e.value is None:
            return
        if e.value == RX_ATTEN_OTHER:
            await _rx_other()
            return
        try:
            at_in.value = float(e.value)
        except (TypeError, ValueError):
            return

    radio_sel.on_value_change(_on_radio)
    txat_sel.on_value_change(_on_atten_pick)
    at_sel.on_value_change(_on_rx_pick)
    for el in (prof_sel, freq_in, funit_in, bo_in):
        el.on_value_change(lambda _e: _render())

    # actions
    async def _key():
        st = state["st"] or {}
        if not st.get("configured"):
            ui.notify("payload guard not configured on the radio: keying refused", type="negative")
            return
        calc = _calc() or {}
        e = calc.get("eirp") or {}
        if e and e.get("hpa_in_dbm") is not None and e["hpa_in_dbm"] > e["hpa_max_input_dbm"]:
            ui.notify(f"computed HPA input {e['hpa_in_dbm']:.1f} dBm exceeds its max "
                      f"{e['hpa_max_input_dbm']:.1f} dBm: "
                      + ("lower the B200 TX gain first" if e.get("tx_control") == "gain"
                         else "raise the G40 TX attenuation first"),
                      type="negative", multi_line=True)
            _log("KEY refused in the browser: HPA input over max")
            return
        g = st.get("guard") or {}
        at = (st.get("atten") or {}).get("att_db")
        with ui.dialog() as dlg, ui.card():
            ui.label("Key the HPA?").classes("text-lg font-bold text-red-700")
            ui.label(f"RX attenuator to {g.get('protect_db')} dB and read back (now {at} dB), switch "
                     "to TX, then HPA on. Your RX setting comes back after UNKEY.")
            if e:
                ui.label(f"Expected EIRP {e['eirp_dbm']:.1f} dBm ({e['eirp_w']:.1f} W), "
                         f"HPA out {e['hpa_out_dbm']:.1f} dBm.")
            for w in calc.get("warnings") or []:
                ui.label("Warning: " + w).classes("text-sm text-red-700")
            with ui.row():
                ui.button("KEY", on_click=lambda: dlg.submit(True)).props("color=red")
                ui.button("Cancel", on_click=lambda: dlg.submit(False)).props("flat")
        if not await dlg:
            return
        ok, r = await _cmd("payload_key", {"on": True})
        if ok:
            state["st"] = r.get("status") or state["st"]
            _log("HPA KEYED")
            ui.notify("HPA keyed", type="warning")
        elif r and r.get("status"):
            state["st"] = r["status"]
        _render()

    async def _unkey():
        ok, r = await _cmd("payload_key", {"on": False})
        if ok:
            state["st"] = r.get("status") or state["st"]
            _log("HPA off, switch RX, RX atten restored")
            ui.notify("HPA off", type="positive")
        _render()

    async def _fe(**params):
        ok, r = await _cmd("frontend_set", params)
        if ok:
            _log(f"frontend_set {params}")
        await _poll(force=True)

    async def _reload():
        ok, r = await _cmd("frontend_reload", {}, timeout=15.0)
        if ok:
            _log(f"front-end reloaded: {r.get('loaded')}")
            ui.notify("front-end reloaded", type="positive")
        await _poll(force=True)

    async def _at_set():
        v = rx_atten_step(float(at_in.value or 0))
        st = state["st"] or {}
        pr = (st.get("guard") or {}).get("protect_db")
        hot = bool((st.get("pa") or {}).get("keyed")) or bool((st.get("tr") or {}).get("tx"))
        if pr is not None and v < float(pr) - 0.05 and hot:
            # the service rule, applied in the browser first (payload_guard)
            ui.notify(f"RX atten stays at {float(pr):g} dB while the HPA is keyed or the switch is "
                      f"in TX: {v:g} dB refused", type="negative", multi_line=True)
            _log(f"atten_set {v:g} dB refused in the browser: RX atten stays at maximum in TX")
            return
        ok, r = await _cmd("atten_set", {"db": v})
        if ok:
            _log(f"RX atten set {r.get('set_db')} dB, readback {r.get('readback_db')} dB")
            _guard.release("rxat")           # sent: follow the readback again
        await _poll(force=True)

    async def _at_read():
        ok, r = await _cmd("atten_get", {})
        if ok and r.get("att_db") is not None:
            _set_rx_widgets(float(r["att_db"]))
            _guard.release("rxat")
            _log(f"RX atten readback {r['att_db']} dB")
        await _poll(force=True)

    async def _restore():
        ok, r = await _cmd("payload_restore", {})
        if ok:
            _log("RX attenuation restored")
        await _poll(force=True)

    async def _txat():
        h = _host()
        fam = caps_family(h.serial) if h is not None else None
        name, params = tx_set_command(fam, _CAPS_CACHE.get(h.serial) if h is not None else None,
                                      float(txat_in.value or 0))
        ok, r = await _cmd(name, params)
        if ok:
            _log(f"{_txc()['label'].replace(' (dB)', '')} {float(txat_in.value or 0):.2f} dB"
                 + (f" (set_tx_atten {params['qdb'] / 4:.2f} dB below full)" if fam not in (None, "epiq") else ""))
            _guard.release("txat")           # sent: follow the radio's readback again
        await _poll(force=True)

    # checkout measurements
    meas_rows: list[dict] = []           # each: freq, txat, bo, pad, tol, exp, meas_meter, meas_rx

    def _meas_expected(f_mhz, txat, bo, pad):
        data = state["data"]
        if not data or not prof_sel.value:
            return None
        try:
            return pb.bench_loopback_expected(data, pb.profile(data, prof_sel.value),
                                              f_mhz, txat, bo, pad)
        except Exception:
            return None

    def _set_pass(lbl, rw):
        exp = rw.get("exp") or {}
        checks = [measurement_check(exp.get(k), rw.get(mk), rw.get("tol", MEAS_TOL_DB))
                  for k, mk in (("meter_dbm", "meas_meter"), ("rx_level_dbm", "meas_rx"))]
        checks = [c for c in checks if c is not None]
        if not checks:
            lbl.set_text("-")
            lbl.classes(replace="w-16 text-sm font-bold text-slate-400")
            return
        ok = all(checks)
        lbl.set_text("pass" if ok else "fail")
        lbl.classes(replace="w-16 text-sm font-bold " + ("text-green-700" if ok else "text-red-700"))

    def _render_meas():
        meas_box.clear()
        with meas_box:
            with ui.row().classes("w-full gap-2 text-xs font-bold text-slate-600"):
                for t, w in (("freq MHz", "w-20"), ("cmd TX atten", "w-24"), ("exp meter dBm", "w-24"),
                             ("meas meter dBm", "w-28"), ("exp RX dBm", "w-24"),
                             ("meas RX", "w-28"), ("pass", "w-16"), ("", "w-10")):
                    ui.label(t).classes(w)
            if not meas_rows:
                ui.label("no rows yet; set a frequency and press Add row").classes("text-xs text-gray-500")
            for i, rw in enumerate(meas_rows):
                exp = rw.get("exp") or {}
                with ui.row().classes("w-full gap-2 items-center"):
                    ui.label(f"{rw['freq']:.1f}").classes("w-20 font-mono text-sm")
                    ui.label(f"{rw['txat']:.2f}").classes("w-24 font-mono text-sm")
                    ui.label(_f(exp.get("meter_dbm"))).classes("w-24 font-mono text-sm")
                    mm = ui.number(value=rw.get("meas_meter"), format="%.1f") \
                        .props("dense outlined").classes("w-28")
                    ui.label(_f(exp.get("rx_level_dbm"))).classes("w-24 font-mono text-sm")
                    mx = ui.number(value=rw.get("meas_rx"), format="%.1f") \
                        .props("dense outlined").classes("w-28")
                    pass_lbl = ui.label("-").classes("w-16 text-sm font-bold text-slate-400")
                    ui.button(icon="delete", on_click=lambda _=None, k=i: _del_meas(k)) \
                        .props("flat dense round")

                    def _upd(_=None, k=i, M=mm, X=mx, L=pass_lbl):
                        meas_rows[k]["meas_meter"] = M.value
                        meas_rows[k]["meas_rx"] = X.value
                        _set_pass(L, meas_rows[k])
                    mm.on_value_change(_upd)
                    mx.on_value_change(_upd)
                    _set_pass(pass_lbl, rw)

    def _add_meas():
        f = float(m_freq.value or 0)
        rw = {"freq": f, "txat": float(m_txat.value or 0), "bo": float(m_bo.value or 0),
              "pad": float(m_pad.value or 0), "tol": float(m_tol.value or MEAS_TOL_DB),
              "meas_meter": None, "meas_rx": None}
        rw["exp"] = _meas_expected(f, rw["txat"], rw["bo"], rw["pad"])
        meas_rows.append(rw)
        _render_meas()

    def _del_meas(k):
        if 0 <= k < len(meas_rows):
            meas_rows.pop(k)
        _render_meas()

    def _meas_here():
        m_freq.set_value(round(_freq_hz() / 1e6, 3))
        try:
            m_txat.set_value(float(txat_in.value or m_txat.value))
        except (TypeError, ValueError):
            pass

    m_add.on_click(_add_meas)
    m_here.on_click(_meas_here)
    m_clear.on_click(lambda: (meas_rows.clear(), _render_meas()))
    _render_meas()

    key_btn.on_click(_key)
    unkey_btn.on_click(_unkey)
    safe_btn.on_click(lambda: _fe(safe=True))
    tr_tx_btn.on_click(lambda: _fe(tr="tx"))
    tr_rx_btn.on_click(lambda: _fe(tr="rx"))
    reload_btn.on_click(_reload)
    at_set_btn.on_click(_at_set)
    at_read_btn.on_click(_at_read)
    restore_btn.on_click(_restore)
    txat_btn.on_click(_txat)

    # STEP_EIRP
    _STEP_COLS = (("step", "w-44"), ("starts", "w-16"), ("waveform", "w-28"), ("MHz", "w-20"),
                  ("TX atten", "w-20"), ("backoff avg", "w-24"), ("peak", "w-16"), ("HPA out", "w-20"),
                  ("EIRP avg", "w-28"), ("EIRP peak", "w-20"), ("HPA", "w-24"), ("", "w-20"))
    steps_state = {"sig": None, "busy": False}

    def _render_steps(rows, note):
        steps_lbl.set_text(note)
        steps_box.clear()
        with steps_box:
            if not rows:
                return
            with ui.row().classes("gap-2 items-center border-b border-gray-300"):
                for t, w in _STEP_COLS:
                    ui.label(t).classes(w + " text-xs font-bold text-slate-600")
            for r in rows:
                over = bool(r["warnings"]) or r["region"] not in ("", "linear")
                tone = "bg-red-100" if r["keyed"] else ("bg-amber-50" if over else "")
                with ui.row().classes("gap-2 items-center " + tone):
                    cells = (
                        str(r["id"]), r["when"], r["what"], _f(r["freq_mhz"], "{:.3f}"),
                        _f(r["tx_atten_db"], "{:.2f}") + ("*" if r["atten_from_radio"] else ""),
                        _f(r["backoff_db"]) + (" dB" if r["backoff_db"] is not None else "")
                        + ("" if r["how"] == "built" else f" ({r['how']})"),
                        _f(r["peak_backoff_db"]), _f(r["hpa_out_dbm"]),
                        (f"{r['eirp_dbm']:.1f} dBm ({r['eirp_w']:.3g} W)" if r["eirp_dbm"] is not None else "-"),
                        _f(r["peak_eirp_dbm"]), r["region"] or "-", "KEYED" if r["keyed"] else "")
                    for (_t, w), txt in zip(_STEP_COLS, cells):
                        lab = ui.label(txt).classes(w + " font-mono text-xs")
                        if r["warnings"] and _t == "HPA":
                            lab.tooltip("; ".join(str(x) for x in r["warnings"]))

    async def _steps_tick():
        if steps_state["busy"] or (is_active is not None and not is_active()):
            return
        data = state["data"]
        if not data or not prof_sel.value:
            return
        try:
            from dpmg.mission import mission_tab as _mt
            mission, keyed = _mt.live_plan()
        except Exception:
            mission, keyed = None, set()
        h = _host()
        rid = h.serial if h is not None else None
        if rid is None and mission:
            rid = next((s.get("radio") for s in mission.get("steps", []) if s.get("radiates")), None)
        txat = float(txat_in.value or 0)
        f_mhz = _freq_hz() / 1e6
        sig = (se.plan_signature(mission, rid), prof_sel.value, rid, round(txat, 2), round(f_mhz, 3),
               tuple(sorted(keyed)))
        if sig == steps_state["sig"]:
            return
        steps_state["busy"] = True
        try:
            if mission is None:
                rows, note = [], "no plan yet: open the mission planner once so the plan loads"
            else:
                rows = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: se.plan_rows(data, prof_sel.value, mission, rid, txat, f_mhz, keyed))
                name = mission.get("name") or "untitled"
                if not rows:
                    note = f"plan '{name}': no keyed steps on {rid or 'the selected radio'}"
                else:
                    top = max((r for r in rows if r["eirp_dbm"] is not None),
                              key=lambda r: r["eirp_dbm"], default=None)
                    note = (f"plan '{name}', {rid}, profile {prof_sel.value}: {len(rows)} keyed steps"
                            + (f", highest average EIRP {top['eirp_dbm']:.1f} dBm on {top['id']}" if top else ""))
            steps_state["sig"] = sig
            _render_steps(rows, note)
            # the page's own figure follows the step that is keyed now
            now = next((r for r in rows if r["keyed"] and r["backoff_db"] is not None), None)
            if now is not None and steps_follow_cb.value:
                bo = round(now["backoff_db"], 1)
                if float(bo_in.value or 0) != bo:
                    bo_in.value = bo
                    _render()
        except Exception as e:
            steps_lbl.set_text(f"step EIRP: {e}")
        finally:
            steps_state["busy"] = False

    ui.timer(1.0, _refresh_hosts)
    ui.timer(POLL_S, _poll)
    ui.timer(2.0, _steps_tick)
    _render()
