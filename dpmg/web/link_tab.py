"""
Ground-link UI (link_tab.py)
============================
Topology-blind GUI face of the ground-station split, in two pieces:

  build_link_header(): compact strip for the app header bar: per-host
                         link/state chips + aircraft position/altitude from
                         STATUS ("no fix" until a GPS source lands).
  build_remote_sdr(): "Remote Radios (Ground Link)" section at the top
                         of the SDR tab: discovery cards, control card
                         (init/RX/retune/gain + PSD nfft/window/buffer that
                         ride the init cmd), PSD chart
                         (tooltip/zoom/axes: parity with the local PSD
                         panel), detections table.

Everything reads link_client caches and sends idempotent CMDs; no sdr_core,
no sockets, no host addresses anywhere in this file. One process-global
LinkManager serves every browser client; per-client ui.timers only poll.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time

import numpy as np
from nicegui import app, ui

from dpmg.radio import g40_clock as _g40_clock
from dpmg.link import ground_log
from dpmg.link.link_client import LinkManager
from dpmg.link.proto import HostType, RadioState, deg_from_1e7
from dpmg.link import proto
from dpmg.radio import host_thermal as _ht   # Radio junction temperature from the heartbeat
from dpmg.radio import card_imu as _imu      # Tilt / shock / motion from the NV100 accelerometer
from dpmg.radio import barometer as _baro    # Host barometer (BMP581/585 on the payload I2C)
from dpmg.payload import rf_frontend as _rf  # HPA temperature and current text for the cards
from dpmg.link.acft_pos_multicast import MulticastPosListener, local_ipv4_addresses, FORMATS as FEED_FORMATS
from dpmg.link.aircraft_feeds import AircraftFeeds, PanelFields
from dpmg.link.acft_pos_multicast import summary_text as acft_summary_text, fix_rows as acft_fix_rows
from dpmg.link import feed_settings

log = logging.getLogger("link_tab")

# one manager per GUI process, shared by all browser clients
# A second GUI on the same host (dev server beside the deployed
# container) cannot share UDP 47811 across users, so the dev launcher may move the
# ANNOUNCE listener (run_gui_link.py [port] [discovery_port]).
import os as _os
from dpmg.link.link_client import SAVED_RADIOS_NAME
import sys as _sys
from dpmg import run_notice as _run_notice    # banner while a mission waits on a radio init
from dpmg.mission.mission_runner import send_stop_tx as _send_stop_tx
from dpmg import rec_state as _rec_state      # REC badge: captures the GUI started
from dpmg.radio import radio_role as _role                    # what each radio is acting as
from dpmg.web import role_fpga_ui as _rfu                   # radio role picker + FPGA image panel
from dpmg.radio import port_map as _ports                   # PORT_MAP: what each SMA jack may be used for
from dpmg.web import port_pick_ui as _ppu                   # PORT_MAP: greyed out jacks + chain tooltips
_DISCO_PORT = int(_os.environ.get("DPMG_DISCOVERY_PORT", 47811))


def saved_radios_path(env=None, here=None, disco_port=None):
    """Where Connect by IP addresses are kept. DPMG_SAVED_RADIOS overrides ('' =
    don't save). By default only the main GUI (discovery port 47811) saves, to
    output/ beside the code (the container's persistent mount); a dev GUI on
    another port must not dial the real radios from the main GUI's list."""
    env = _os.environ if env is None else env
    if "DPMG_SAVED_RADIOS" in env:
        return env["DPMG_SAVED_RADIOS"] or None
    if (disco_port if disco_port is not None else _DISCO_PORT) != 47811:
        return None
    if not here:
        from dpmg.paths import REPO_DIR as here
    return _os.path.join(here, "output", SAVED_RADIOS_NAME)


MGR = LinkManager(gui_id=_os.environ.get("DPMG_GUI_ID", "GCS-X86"),
                  discovery_port=_DISCO_PORT, saved_path=saved_radios_path())
_started = False

# Aircraft nav can arrive on a multicast group independent of the
# radio STATUS GPS. Off until the operator sets group/port and starts it; the
# header prefers this fix over STATUS when it is fresher (see build_link_header).
# one listener per saved aircraft (dpmg/link/aircraft_feeds.py): the header
# chip, the operator panel, the planner and the engineering Aircraft feed tab
# all read these. ACFT is the first aircraft's listener at start, kept for
# readers of the one feed; new code asks FEEDS.
FEEDS = AircraftFeeds(feed_settings.load())
ACFT = FEEDS.primary
ACFT_FRESH_S = 5.0                 # a multicast fix older than this yields to STATUS

REFRESH_S = 0.25                    # cache poll cadence (matches PSD rate)
DET_TABLE_MAX = 50

_STATE_TXT = {
    int(RadioState.UNINIT):    ("UNINIT", "text-gray-500"),
    int(RadioState.INIT_OK):   ("INIT_OK", "text-blue-600"),
    int(RadioState.STREAMING): ("STREAMING", "text-green-600"),
    int(RadioState.FAULT):     ("FAULT", "text-red-600"),
}

# Log information's laptop side files, per radio, outliving any one page
def radio_aircraft(serial):
    """The aircraft a radio is bound to in the live plan (the planner's radio
    field), else None, which the feeds read as the first aircraft."""
    try:
        from dpmg.mission import mission_tab as _mt
        return _mt.radio_aircraft_live(serial)
    except Exception:
        return None


def acft_for_log(serial):
    """(aircraft name, last fix) of the aircraft this radio follows, for the
    ground log's position rows."""
    return FEEDS.latest_named(radio_aircraft(serial))


GLOGS = ground_log.GroundLogs(lambda: MGR.hosts, acft_for_log,
                              lambda state: _STATE_TXT.get(state, ("?", ""))[0])


def aircraft_label(name) -> str:
    """The name shown for an aircraft: its own, or nothing when it is the only
    one and still carries the group:port name it was given by default."""
    if name is None:
        return ""
    e = FEEDS.entry(name) or {}
    if len(FEEDS) == 1 and name == feed_settings.default_name(e.get("group"), e.get("port")):
        return ""
    return str(name)


def rx_off_note(radio_state) -> str:
    """For a radio card: the RX port dropdown keeps its port with no stream on it,
    so say when RX is off."""
    return ", RX off" if radio_state == int(RadioState.INIT_OK) else ""


_HOST_TXT = {int(HostType.G40_RX): "G40 (RX)",
             int(HostType.X40_TX): "X40 (TX)",
             int(HostType.OTHER): "OTHER"}

# Canonical G40/X40 valid sample rates, taken from sdr_core: the same source
# the engineering scenario builder uses (scenario_tab._rate_table). The operator
# and engineering SDR dropdowns both build from these so they offer exactly the
# radio's supported rates, with nothing hand-maintained to drift.
try:
    from dpmg.radio.sdr_core import (G40_VALID_RATES_HZ as _G40_RATES,
                          X40_VALID_RATES_HZ as _X40_RATES)
except Exception:                    # sdr_core needs libsidekiq; fall back if absent
    _G40_RATES = [1_920_000, 7_680_000, 15_360_000, 30_720_000]
    _X40_RATES = [50_000_000]
# The A/B-side co-stream rule and the handle: connector tables,
# shared with the service so the GUI's pre-init warning and the service's
# post-init refusal say the same thing.
try:
    from dpmg.radio.sdr_core import (x40_same_side_conflict, X40_RX_CONNECTOR as _X40_RX_CONN)
except Exception:
    _X40_RX_CONN = {"RXA1": "J2", "RXA2": "J1", "RXB1": "J4", "RXB2": "J3"}

    def x40_same_side_conflict(rx_antenna, tx_antenna):
        rx, tx = str(rx_antenna or "").upper(), str(tx_antenna or "").upper()
        if rx in ("RXA1", "RXA2") and tx.startswith("TXA"):
            return (f"X40: RX {rx} ({_X40_RX_CONN[rx]}) and TX {tx} (J5) are both "
                    f"A-side: they share one LO and cannot stream at the same time. "
                    f"For RX+TX receive on RXB1 (J4) or RXB2 (J3); {rx} is receive-only.")
        return None
# The same-card reverse-role rule (RX on the "2" jack + TX on the
# "1" jack of one card cannot co-stream: FIXED port mode locks J1 = RX, J2 = TX),
# shared with the service so the GUI refusal and the service REJECTED say the same.
try:
    from dpmg.radio.sdr_core import g40_same_card_conflict, g40_fixed_mode_conflict
except Exception:
    def _g40_same_card_msg(rx_desc, tx_desc, side):
        return (f"G40: RX on {rx_desc} with TX on {tx_desc} cannot co-stream on one "
                f"card. Single-card FDD uses the NV100 FIXED RF-port mode, "
                f"which locks J1 (TRx1{side}) = RX and J2 (TRx2{side}) = TX, and TRX "
                f"mode flips both jacks together, so the \"2\" jack cannot receive "
                f"while the same card transmits. Supported single-card FDD is RX "
                f"TRx1{side} + TX TRx2{side}; otherwise use a cross-card pair.")

    def g40_same_card_conflict(rx_port, tx_port):
        rx, tx = str(rx_port or "").upper(), str(tx_port or "").upper()
        if (len(rx) == 5 and len(tx) == 5 and rx.startswith("TRX") and tx.startswith("TRX")
                and rx[4] == tx[4] and rx[3] == "2" and tx[3] == "1"):
            return _g40_same_card_msg(str(rx_port), str(tx_port), rx[4])
        return None

    def g40_fixed_mode_conflict(rx_antenna, tx_antenna, chan_mode, rx_card=0, tx_card=None):
        rx, tx = str(rx_antenna or "").upper(), str(tx_antenna or "").upper()
        tx_card = rx_card if tx_card is None else tx_card
        if (str(chan_mode or "").lower() == "dual" and int(rx_card or 0) == int(tx_card or 0)
                and rx == "RXA2" and tx in ("TXA1", "TXB1")):
            side = "B" if int(rx_card or 0) == 1 else "A"
            txn = "2" if tx == "TXB1" else "1"
            return _g40_same_card_msg(f"{rx} (TRx2{side})", f"{tx} (TRx{txn}{side})", side)
        return None
G40_SAMPLE_RATES = {int(hz): f"{hz/1e6:g} MSps" for hz in _G40_RATES}
X40_SAMPLE_RATES = {int(hz): f"{hz/1e6:g} MSps" for hz in _X40_RATES}

# Per-radio capability cache (serial: caps dict), shared by both
# the operator and engineering views so a radio is probed only once. The caps
# command (answered by sdr_core/dpmg_service) describes the connected SDR; the
# UI redraws its control set from `family`. family "epiq" (g40/x40) is the
# legacy default and is stored as the fallback whenever caps is unavailable, so
# radios/services that predate the caps command keep the old behavior exactly.
_CAPS_CACHE: dict[str, dict] = {}
_CAPS_INFLIGHT: set[str] = set()


# severity colours for the sensor status row (whole-line, worst severity).
# Rendered on a plain ui.label via .style(), not ui.html: ui.html content
# updates from the sensor timer raise "parent slot deleted" floods and knock
# out the shared refresh.
_SENS_AMBER = "#b8860b"
_SENS_RED = "#c0392b"
_SENS_NEUTRAL = "#4b5563"       # tailwind gray-600, matches the label's class


def _fmt_sensors(d, ext_ref=False):
    """One-line summary of a UHD `sensors` dict plus the worst per-field
    severity colour, returned as (text, css_color). Absent keys are omitted; an
    empty dict renders ("sensors: -", neutral). `ext_ref` True means the operator
    selected an external 10 MHz ref, so an unlocked ref is an alarm (red);
    otherwise ref is never red. Plain text (no markup) so it is safe on a
    ui.label. Shared by both views."""
    sev = 0                                 # 0 neutral, 1 amber, 2 red

    def bump(level):
        nonlocal sev
        if level > sev:
            sev = level

    parts = []
    if "ref_locked" in d:
        if d["ref_locked"]:
            parts.append("ref locked")
        elif ext_ref:                       # alarm only when external selected
            parts.append("ref unlocked"); bump(2)
        else:
            parts.append("ref unlocked")           # internal: unlocked is expected
    if "rx_lo_locked" in d:
        parts.append("RX LO " + ("locked" if d["rx_lo_locked"] else "unlocked"))
        if not d["rx_lo_locked"]:
            bump(2)
    if d.get("rx_rssi_dbfs") is not None:
        v = float(d["rx_rssi_dbfs"])
        parts.append(f"RSSI {v:.1f} dBFS")
        bump(2 if v >= -3 else 1 if v >= -10 else 0)
    if d.get("rx_temp_c") is not None:
        v = float(d["rx_temp_c"])
        parts.append(f"RX {v:.0f}°C")
        bump(2 if v >= 85 else 1 if v >= 70 else 0)
    if "tx_lo_locked" in d:
        parts.append("TX LO " + ("locked" if d["tx_lo_locked"] else "unlocked"))
        if not d["tx_lo_locked"]:
            bump(2)
    if d.get("tx_temp_c") is not None:
        v = float(d["tx_temp_c"])
        parts.append(f"TX {v:.0f}°C")
        bump(2 if v >= 85 else 1 if v >= 70 else 0)
    # SoapySDR / HackRF keys: applied gains, half-duplex TX state, front-end
    # switches, auto-detected reference
    if d.get("ref_source"):
        rs = str(d["ref_source"])
        parts.append(f"ref {rs}")
        if ext_ref and rs != "external":
            bump(2)
    if d.get("rx_gain_db") is not None:
        parts.append(f"RX gain {float(d['rx_gain_db']):.0f} dB")
    if d.get("tx_atten_db") is not None:
        parts.append(f"TX atten {float(d['tx_atten_db']):.0f} dB")
    amps = [n for n, k in (("RX", "rx_amp"), ("TX", "tx_amp")) if d.get(k)]
    if amps:
        parts.append("amp " + "+".join(amps))
        bump(1)
    if d.get("bias_tx"):
        parts.append("bias 3.3V"); bump(1)
    if "tx_active" in d:
        if d["tx_active"]:
            parts.append("TX KEYED (RX paused)"); bump(1)
        else:
            parts.append("RX")
    text = "sensors: " + ", ".join(parts) if parts else "sensors: -"
    color = _SENS_RED if sev == 2 else (_SENS_AMBER if sev == 1
                                        else _SENS_NEUTRAL)
    return text, color


def _want_caps(host):
    """Start the one-time caps probe for a connected radio that isn't focused,
    so its card can tell what it is (radio_kind) before it is initialized.
    Caps built for another FPGA image, or fetched before the radio's service
    last restarted, are dropped and fetched again."""
    if _rfu.caps_stale(_CAPS_CACHE.get(host.serial), host_config(host),
                       getattr(getattr(host, "status", None), "uptime_s", None)):
        _CAPS_CACHE.pop(host.serial, None)
    if getattr(host, "connected", False) and host.serial not in _CAPS_CACHE \
            and host.serial not in _CAPS_INFLIGHT:
        asyncio.create_task(_fetch_caps(host))


NO_RADIO_TXT = "no radio found"


def link_text(connected, cfg, lost="LINK LOST"):
    """(text, colour class) for a radio card's link line. LINK OK only means the
    service answers; when it reports its USB radio missing (get_config
    hw_present False) the line says so in amber."""
    if not connected:
        return lost, "text-red-600"
    if (cfg or {}).get("hw_present") is False:
        return f"LINK OK, {NO_RADIO_TXT}", "text-amber-600"
    return "LINK OK", "text-green-600"


def radio_kind(serial, cfg=None):
    """Radio kind from get_config (inited radios) or the caps probe, else None."""
    k = (cfg or {}).get("kind")
    if not k:
        c = _CAPS_CACHE.get(serial)
        k = c.get("kind") if isinstance(c, dict) else None
    return k


def atten_state_text(a) -> str:
    """The RF front-end attenuator line from frontend_status's atten part. With
    atten.transport auto it carries the plug-in state (DEVICE_WATCH): connected,
    not connected, lost, several found, error."""
    if a is None:
        return "atten: none configured"
    if not isinstance(a, dict):
        return "atten: -"
    parts = ["atten:"]
    if a.get("state"):
        parts.append(str(a["state"]))
    if a.get("att_db") is not None:
        parts.append(f"{a['att_db']} dB")
    who = " ".join(str(a[k]) for k in ("model", "serial") if a.get(k))
    if who:
        parts.append(who)
    if a.get("transport"):
        parts.append(f"[{a['transport']}]")
    elif a.get("path"):
        parts.append(f"[{a['path']}]")
    txt = ("atten: " + "  ".join(parts[1:])) if len(parts) > 1 else "atten: -"
    return txt + (f"  ERR {a.get('err')}" if a.get("err") else "")


def caps_family(serial):
    """Family for a focused radio: the advertised family once caps arrive
    (e.g. 'uhd' for a B200, 'epiq' for G40/X40), else None until the
    probe answers. Callers treat None/epiq identically (legacy G40/X40 path)."""
    c = _CAPS_CACHE.get(serial)
    return c.get("family") if isinstance(c, dict) else None


def eng_tx_confirm_text(serial, lo_txt, tx_mode, family=None, kind=None, handle=None,
                        jack=None, atten_db=None, tx_port=None, tx_gain_db=None,
                        gain_mode=None):
    """Main line of the engineering TX confirm. A caps-driven radio (family
    'uhd' or 'soapy', e.g. a B200) inits with its own TX port and the TX gain
    number box, so the confirm names those; the G40 attenuation field and the
    TRx jack name are hidden for it and mean nothing there. Everything else
    (G40/X40/mock) keeps the handle, jack and attenuation text."""
    if family in ("uhd", "soapy"):
        try:
            g = f"{float(tx_gain_db):g}"
        except (TypeError, ValueError):
            g = "?"
        what = "TX atten" if gain_mode == "atten" else "TX gain"
        radio = f"{kind.upper()} " if kind else ""
        return (f"Host {serial}: transmit on {radio}port {tx_port} at {lo_txt}, "
                f"{what} {g} dB, mode {tx_mode}.")
    return (f"Host {serial}: transmit on {handle} ({jack}) "
            f"at {lo_txt}, atten {atten_db} dB, "
            f"mode {tx_mode}.")


async def _fetch_caps(host, on_done=None):
    """Probe a radio's capabilities once and cache them. Guards against repeat
    and concurrent fetches. On any error/timeout (or a service that doesn't
    know the command) the epiq fallback is stored so the control set stays
    exactly as it was before caps. on_done() re-renders the view when caps land."""
    serial = getattr(host, "serial", None)
    if not serial or serial in _CAPS_INFLIGHT or serial in _CAPS_CACHE:
        return
    _CAPS_INFLIGHT.add(serial)
    try:
        ack = await host.cmd("caps", {}, timeout=5.0)
        c = getattr(ack, "result", None)
        if isinstance(c, dict) and c.get("family"):
            # the service's uptime now (see role_fpga_ui.caps_stale) and when
            # they came, so the views re-apply caps that were fetched again
            c = dict(c, _uptime_s=getattr(getattr(host, "status", None), "uptime_s", None),
                     _fetched=time.monotonic())
        _CAPS_CACHE[serial] = c if (isinstance(c, dict) and c.get("family")) \
            else {"family": "epiq"}
    except Exception:
        _CAPS_CACHE[serial] = {"family": "epiq"}
    finally:
        _CAPS_INFLIGHT.discard(serial)
        if on_done is not None:
            try:
                on_done()
            except Exception:
                pass


async def _start_mgr():
    """Start the process-global LinkManager, retrying if the discovery port
    is momentarily held by another GUI instance (e.g. a native dev GUI and
    the docker GUI overlapping during a redeploy). SO_REUSEPORT only shares
    a port between sockets of the same uid, so a root container cannot
    coexist with a user-run GUI: retry until the other side exits."""
    global _started
    if _started:
        return
    _started = True
    while True:
        try:
            await MGR.start()
            log.info("ground link manager started")
            return
        except OSError as e:
            log.warning("ground link bind failed (%s): another GUI holds "
                        "the discovery port? retrying in 10 s", e)
            await asyncio.sleep(10.0)

app.on_startup(_start_mgr)


async def _start_ground_logs():
    GLOGS.task = asyncio.create_task(GLOGS.run())

app.on_startup(_start_ground_logs)


def _fmt_age(age: float) -> str:
    return "--" if age == float("inf") else f"{age:.1f} s"


# header strip: link status + aircraft position/altitude

def _planner_color(serial):
    """The mission planner's colour for this radio (None when no planner page
    has opened a plan, or the radio is not in the plan)."""
    try:
        from dpmg.mission.mission_tab import radio_color
        return radio_color(MGR, serial)
    except Exception:
        return None


def _planner_select(serial):
    """A click on a radio in the header selects it in the mission planner, so
    the planner's map shows that radio's steps. Nothing else changes."""
    try:
        from dpmg.mission.mission_tab import select_radio
        select_radio(MGR, serial)
    except Exception:
        pass


def build_link_header():
    """Compact, dark-header-friendly strip. Chips per discovered host plus
    the aircraft position/altitude from the 1 Hz STATUS heartbeat. A radio's
    chip carries the planner's colour for it and selects it there on a
    click."""
    strip = ui.row().classes("items-center gap-4")

    def refresh():
        strip.clear()
        with strip:
            if not MGR.hosts:
                ui.label("link: listening for radios ...") \
                    .classes("text-xs text-gray-300 italic")
            for serial, h in sorted(MGR.hosts.items()):
                txt, _ = _STATE_TXT.get(h.radio_state, ("?", ""))
                ok = h.connected
                dot = "" if ok else " (no link)"
                color = "text-green-400" if ok else "text-red-400"
                tx_on = (h.status is not None and
                         h.status.flags & proto.Status.FLAG_TX_ACTIVE)
                rec = _rec_state.active(serial) is not None
                pcol = _planner_color(serial)
                chip = ui.row().classes("items-center gap-1 no-wrap cursor-pointer") \
                    .tooltip("click to select this radio in the mission planner")
                chip.on("click", lambda e, s=serial: _planner_select(s))
                with chip:
                    if pcol:
                        ui.element("div").style(
                            f"width:9px;height:9px;border-radius:2px;background:{pcol};"
                            "pointer-events:none")
                    ui.label(f"{serial} {txt}{rx_off_note(h.radio_state)}{dot}"
                             + (" TX ON" if tx_on else "") + (" REC" if rec else "")) \
                        .classes("font-mono text-xs " +
                                 ("text-red-400 font-bold" if (tx_on or rec) else color)) \
                        .style("pointer-events:none")
            # aircraft position: a fresh multicast fix wins; else fall back to
            # the first host reporting a valid STATUS GPS fix (G40 airborne).
            fresh = FEEDS.fresh(ACFT_FRESH_S)
            if fresh:
                # position and altitude, then heading and ground speed when
                # the feed carried them (VTG or RMC, or JSON heading and speed);
                # one chip per aircraft with a fresh fix
                for name, mc in fresh:
                    who = aircraft_label(name)
                    ui.label(f"ACFT {who + ' ' if who else ''}{acft_summary_text(mc)} (mcast)") \
                        .classes("font-mono text-xs text-amber-300") \
                        .tooltip(f"aircraft feed {name}: " + ", ".join(mc.sentences or [mc.source]))
            else:
                pos = None
                for h in MGR.hosts.values():
                    st = h.status
                    if st is not None and st.flags & proto.Status.FLAG_POS_VALID:
                        pos = st
                        break
                if pos is not None:
                    ui.label(f"ACFT {deg_from_1e7(pos.lat_1e7):+.5f} "
                             f"{deg_from_1e7(pos.lon_1e7):+.5f} "
                             f"{pos.alt_m:.0f} m") \
                        .classes("font-mono text-xs text-amber-300")
                elif FEEDS.any_running():
                    on = [(n, l) for n, l in FEEDS.entries() if l.running]
                    ui.label("ACFT pos: mcast " + ", ".join(
                        f"{aircraft_label(n) + ' ' if aircraft_label(n) else ''}{l.group}:{l.port}"
                        f" (no fix yet, rx={l.rx})" for n, l in on)).classes(
                        "font-mono text-xs text-gray-400")
                elif MGR.hosts:
                    ui.label("ACFT pos: no fix").classes(
                        "font-mono text-xs text-gray-400")

    ui.timer(0.5, refresh)


# telemetry chart builders (options mirror the local SDR Analysis panels)

WF_BINS = 150                       # match the local waterfall decimation
WF_MAX_ROWS = 60

# The operator console's TX-attenuation choices (hardware-legal
# NV100 values, 0 = max power) and centre-frequency unit multipliers, at module
# level so payload_tab draws the very same dropdown and unit selector.
TX_ATTEN_CHOICES = {str(d): f"{d} dB" for d in
                    [0, 1, 2, 3, 5, 7, 10, 15, 20, 25, 30, 35, 40, 41.75]}
FREQ_UNITS = {"Hz": 1.0, "kHz": 1e3, "MHz": 1e6, "GHz": 1e9}


def _fill_gain_select(sel, num, values, rng):
    """Populate a gain/attenuation select from the caps value
    list [[value, label], ...] (every entry is a setting the radio supports),
    pick the entry nearest the current number-box value (clamped into range)
    and mirror it into the number box, which stays the value the init/start
    paths read. Falls back to a min..max..step expansion if the caps carry no
    list (older service)."""
    if not values:
        try:
            from dpmg.radio import sdr_core as _sc              # same helper the backends use
            rxv, txv = _sc.gain_choices(_fill_gain_select._kind, _fill_gain_select._caps)
            values = rxv if rng is (_fill_gain_select._caps or {}).get("rx_gain") else txv
        except Exception:
            values = None
    if not values:
        try:
            lo, hi = float(rng.get("min", 0)), float(rng.get("max", 0))
            st = float(rng.get("step", 1.0)) or 1.0
            n = int(round((hi - lo) / st))
            values = [[round(lo + i * st, 4), f"{round(lo + i * st, 4):g} dB"]
                      for i in range(n + 1)]
        except (TypeError, ValueError):
            values = []
    opts = {str(v): str(lbl) for v, lbl in values}
    if not opts:
        return
    try:
        cur = float(num.value) if num.value is not None else float(values[0][0])
    except (TypeError, ValueError):
        cur = float(values[0][0])
    best = min((float(v) for v, _ in values), key=lambda v: abs(v - cur))
    key = next(k for k in opts if float(k) == best)
    sel.set_options(opts, value=key)
    num.set_value(best)


_fill_gain_select._kind = None     # context set by the caps adapters before a fill
_fill_gain_select._caps = None


# PSD plot y-axis. With plain auto-scale a flat noise floor (the B200's trace
# spans only 8-18 dB) gets stretched over the whole plot and its normal +-1 dB
# Welch jitter looks choppy. Below PSD_MIN_SPAN_DB the axis is held at that span
# on a 5 dB grid; a wider trace is auto-scaled as before.
PSD_MIN_SPAN_DB = 40.0


def psd_y_range(vals, prev=None, min_span=PSD_MIN_SPAN_DB):
    """(ymin, ymax) for the PSD plot, or (None, None) for auto-scale.

    A short trace is centred in a min_span window. The previous window is kept
    while the trace still fits inside it, so the axis doesn't step every frame."""
    try:
        v = np.asarray(vals, dtype=float)
        v = v[np.isfinite(v)]
    except (TypeError, ValueError):
        return None, None
    if v.size == 0:
        return None, None
    lo_v, hi_v = float(v.min()), float(v.max())
    if hi_v - lo_v >= min_span:
        return None, None
    if prev and prev[0] is not None and prev[0] <= lo_v and prev[1] >= hi_v:
        return prev
    lo = 5.0 * math.floor((lo_v - (min_span - (hi_v - lo_v)) / 2.0) / 5.0)
    hi = lo + min_span
    if hi < hi_v:                       # the 5 dB floor pushed the top under the data
        hi = 5.0 * math.ceil(hi_v / 5.0)
        lo = min(hi - min_span, 5.0 * math.floor(lo_v / 5.0))   # may widen to 45 dB
    return lo, hi


def _zoom_y_range(rng, vals, zoom):
    """Scale a y window about its centre by `zoom` (<1 zooms in, >1 out). An auto
    window (None, None) is first turned into a tight window around the data so the
    +/- buttons always have something to scale."""
    lo, hi = rng
    if lo is None or hi is None:
        try:
            v = np.asarray(vals, dtype=float)
            v = v[np.isfinite(v)]
        except (TypeError, ValueError):
            return rng
        if v.size == 0:
            return rng
        lo_v, hi_v = float(v.min()), float(v.max())
        pad = 0.05 * ((hi_v - lo_v) or 1.0)
        lo, hi = lo_v - pad, hi_v + pad
    mid = (lo + hi) / 2.0
    half = (hi - lo) / 2.0 * zoom
    return mid - half, mid + half


def _set_psd_y(chart, vals):
    """Apply psd_y_range to a chart built by _build_psd_chart (before update()).
    A manual y-zoom factor from the +/- buttons scales the window about its
    centre; at factor 1.0 the auto range is unchanged. The last vals are kept so
    a button press can re-apply the range with no new frame."""
    chart._dpmg_last_vals = vals
    base = psd_y_range(vals, getattr(chart, "_dpmg_yr_base", None))
    chart._dpmg_yr_base = base
    z = float(getattr(chart, "_dpmg_yzoom", 1.0) or 1.0)
    rng = _zoom_y_range(base, vals, z) if z != 1.0 else base
    chart._dpmg_yr = rng
    chart.options["yAxis"]["min"], chart.options["yAxis"]["max"] = rng


def _yzoom(chart, factor):
    """+/- button handler: scale the manual y-zoom factor (None resets to auto).
    The range is re-applied from the last vals so the plot responds at once,
    even when no new PSD frame has arrived."""
    if factor is None:
        chart._dpmg_yzoom = 1.0
    else:
        z = float(getattr(chart, "_dpmg_yzoom", 1.0) or 1.0) * float(factor)
        chart._dpmg_yzoom = max(0.15, min(6.0, z))
    vals = getattr(chart, "_dpmg_last_vals", None)
    if vals is not None:
        _set_psd_y(chart, vals)
    chart.update()


# PSD averaging buffer sent at INITIALIZE. The B200 gets 4x the samples (127
# Welch segments instead of 31, half the trace jitter); with the batched FFT in
# psd_trace.compute_psd that costs 49 ms per 4 Hz frame on the Orin Nano.
PSD_BUFFER_DEFAULT = 65536
B200_PSD_BUFFER = 262144


def psd_buffer_for(kind):
    return B200_PSD_BUFFER if kind == "b200" else PSD_BUFFER_DEFAULT


# B200 at high rates. From 50 MSps up the AD9364 runs with little decimation and
# its ADC's own shaped noise lifts the band edges (a U in the PSD, about +15 dB at
# 61.44 MSps with 40 dB RX gain). That noise is fixed, so more RX gain lifts the
# front-end noise above it: measured on the Super Nano's B200mini, the U was
# +15 dB at 40 dB gain, +1.3 dB at 60 and gone at 76.
B200_HIGH_RATE_HZ = 50e6
B200_HIGH_RATE_RX_GAIN_DB = 60.0
B200_HIGH_RATE_NOTE = ("50 MSps and up: RX gain set to 60 dB. Lower gain lets the "
                       "B200's own ADC noise lift the band edges (a U in the PSD). "
                       "Turn it down for strong signals.")


def b200_rate_gain(kind, rate_hz, gain_db, bumped_from):
    """What a rate pick does to a B200's RX gain.

    Returns (new_gain_db or None, bumped_from, show_note). At 50 MSps and up a
    gain below 60 dB goes to 60 and the old value is remembered; a gain already
    at 60 or above is left alone. Back below 50 MSps the remembered gain comes
    back, unless the operator has changed the gain since. Other radios: no-op."""
    try:
        high = (kind == "b200" and rate_hz is not None
                and float(rate_hz) >= B200_HIGH_RATE_HZ)
        g = None if gain_db is None else float(gain_db)
    except (TypeError, ValueError):
        return None, bumped_from, False
    if high:
        if g is None or g < B200_HIGH_RATE_RX_GAIN_DB:
            return (B200_HIGH_RATE_RX_GAIN_DB,
                    g if bumped_from is None else bumped_from, True)
        return None, bumped_from, True
    if bumped_from is not None and g == B200_HIGH_RATE_RX_GAIN_DB:
        return float(bumped_from), None, False
    return None, None, False


LO_NOTE_IDLE = "applies at START"
LO_NOTE_PENDING = "LO offset not applied yet, press START"
ENG_LO_NOTE_IDLE = "applies at INITIALIZE"
ENG_LO_NOTE_PENDING = "LO offset not applied yet, press INITIALIZE"


def lo_best_choice(rate_hz, sign, gain_db):
    """What the Best + / Best - button does for a B200 at this rate.

    Returns (lo_offset_mhz, raise_gain_to_60, message, notify_type), from
    sdr_core.UhdBackend.best_lo_offset: up to about 20 MSps the LO leak is put
    outside the band; at 25 to 30.72 MSps in the roll-off strip just past the
    usable band, which wants 60 dB RX gain to keep the far edge flat (the
    AD9364's own ADC noise rises there); from 40 MSps there is no room."""
    from dpmg.radio import sdr_core as _sc
    try:
        rate = float(rate_hz)
    except (TypeError, ValueError):
        return 0.0, False, "pick a sample rate first", "warning"
    lo, mcr, where = _sc.UhdBackend.best_lo_offset(rate, sign)
    if where == "none":
        return (0.0, False,
                f"No room for an LO offset at {rate / 1e6:g} MSps: the B200's master "
                f"clock tops out at 61.44 MHz, so the LO stays at center. "
                f"Applies at START.", "warning")
    if where == "outside":
        return (round(lo / 1e6, 6), False,
                f"LO offset {lo / 1e6:+.3f} MHz: the LO leak lands outside the band. "
                f"Applies at START.", "info")
    low = gain_db is None or float(gain_db) < B200_HIGH_RATE_RX_GAIN_DB
    return (round(lo / 1e6, 6), low,
            f"LO offset {lo / 1e6:+.3f} MHz: the LO leak sits in the roll-off strip "
            f"just past the usable band (master clock {mcr / 1e6:g} MHz)."
            + (" RX gain set to 60 dB so the far band edge stays flat." if low else "")
            + " Applies at START.", "info")


def _nearest_gain_key(sel, db):
    """The key of a gain select's option nearest db (None if it has none)."""
    opts = sel.options if isinstance(sel.options, dict) else {}
    try:
        return min(opts, key=lambda k: abs(float(k) - db)) if opts else None
    except (TypeError, ValueError):
        return None


def _build_psd_chart():
    # the chart sits in a relative box so the y-axis zoom buttons can float over
    # its top-left, by the y axis. the mouse wheel stays bound to the x-axis
    # dataZoom (dragging the slider or wheeling zooms frequency); the y span is
    # driven by the buttons instead, so the two never fight over the wheel.
    with ui.element("div").classes("relative w-full"):
        chart = ui.echart({
            "animation": False,
            "tooltip": {"trigger": "axis"},
            # wider left gutter so the floating y-zoom buttons, the axis name
            # and the tick labels each sit in their own lane and stop covering
            # each other.
            "grid": {"left": 100, "right": 20, "top": 34, "bottom": 60},
            # LineX brush selects a frequency span; brushEnd: the
            # carve panel reads the selected [f_lo, f_hi] and offers to save it.
            "toolbox": {"right": 10, "top": 0,
                        "feature": {"brush": {"type": ["lineX", "clear"],
                                              "title": {"lineX": "select band",
                                                        "clear": "clear"}}}},
            "brush": {"xAxisIndex": 0, "brushType": "lineX",
                      "throttleType": "debounce", "throttleDelay": 200,
                      "brushStyle": {"color": "rgba(70,130,255,0.18)"}},
            "xAxis": {"type": "value", "name": "Frequency (Hz)",
                      "scale": True, "nameLocation": "middle", "nameGap": 32},
            "yAxis": {"type": "value", "name": "PSD (dB/Hz)",
                      "scale": True, "nameLocation": "middle", "nameGap": 56},
            "dataZoom": [
                {"type": "inside", "xAxisIndex": 0, "filterMode": "none"},
                {"type": "slider", "xAxisIndex": 0, "height": 16, "bottom": 6},
            ],
            "series": [
                {"type": "line", "showSymbol": False, "lineStyle": {"width": 1},
                 "data": []},
                # peak-hold (max-hold) overlay: thin orange, filled when enabled
                {"type": "line", "name": "peak hold", "showSymbol": False,
                 "lineStyle": {"width": 1, "color": "#e8590c"}, "data": []},
            ],
        }).classes("w-full h-72")
        with ui.column().classes("absolute left-0 top-9 z-10 gap-1"):
            _ystyle = "min-width:24px;width:24px;height:24px;padding:0"
            ui.button("+", on_click=lambda: _yzoom(chart, 0.8)) \
                .props("dense unelevated color=grey-7").style(_ystyle) \
                .tooltip("zoom y in")
            ui.button("-", on_click=lambda: _yzoom(chart, 1.25)) \
                .props("dense unelevated color=grey-7").style(_ystyle) \
                .tooltip("zoom y out")
            ui.button("A", on_click=lambda: _yzoom(chart, None)) \
                .props("dense unelevated color=grey-5").style(_ystyle) \
                .tooltip("auto y-scale")
    lbl = ui.label("no PSD yet").classes("font-mono text-xs")
    return chart, lbl


def _build_wf_chart():
    chart = ui.echart({
        "tooltip": {"show": False},
        "grid": {"left": 70, "right": 20, "top": 10, "bottom": 60},
        "xAxis": {"type": "category", "data": [], "name": "Frequency (MHz)",
                  "nameLocation": "middle", "nameGap": 28},
        "yAxis": {"type": "category", "data": [], "name": "frame"},
        "visualMap": {"min": -120, "max": -20, "calculable": True,
                      "orient": "horizontal", "left": "center", "bottom": 0,
                      "inRange": {"color": ["#0d0887", "#7e03a8", "#cc4778",
                                            "#f89441", "#f0f921"]}},
        "series": [{"type": "heatmap", "data": [], "progressive": 4000}],
    }).classes("w-full h-72")
    lbl = ui.label("no rows yet").classes("font-mono text-xs")
    return chart, lbl


# SDR-tab section: remote radio control + telemetry

def _acft_fix_table():
    """The table of every field of the aircraft feed's last fix: value, the
    sentence or JSON object that carried it, and its age. The caller's timer
    puts acft_fix_rows(ACFT.latest) in .rows and calls update()."""
    cols = [{"name": "field", "label": "field", "field": "field", "align": "left"},
            {"name": "value", "label": "value", "field": "value", "align": "left",
             "classes": "font-mono"},
            {"name": "source", "label": "from", "field": "source", "align": "left",
             "classes": "font-mono"},
            {"name": "age", "label": "age", "field": "age", "align": "right",
             "classes": "font-mono"}]
    return ui.table(columns=cols, rows=acft_fix_rows(None), row_key="field") \
        .classes("w-full text-xs").props("dense flat bordered hide-bottom") \
        .tooltip("every field of the last fix: its value, the NMEA sentence or JSON "
                 "object that carried it, and the seconds since that sentence arrived; "
                 "the feed time row is the datagram's own time stamp against this "
                 "console's clock")


def _build_operator_acft_panel():
    """The operator page's Aircraft position panel: one block per aircraft on
    the saved list, each with its name, the feed's last fix as one line (fix,
    heading, speed, age) and the table of fields with their source sentence
    and age, red when the fix is stale and amber while nothing has arrived.
    Read only; the feeds are set up in the engineering Aircraft feed tab or
    the planner, and the listeners are the shared ones. The blocks are built
    again when the aircraft list changes."""
    with ui.expansion("Aircraft position (feed)", icon="flight",
                      value=False).classes("w-full border rounded") as exp:
        box = ui.column().classes("w-full gap-2")
    blocks = {"names": None, "by": {}}

    def _build():
        box.clear()
        blocks["by"] = {}
        blocks["names"] = FEEDS.names()
        with box:
            for name, _lst in FEEDS.entries():
                with ui.column().classes("w-full gap-0 border-l-4 pl-2") \
                        .style(f"border-color:{_aircraft_color(name)}"):
                    head = ui.label(name).classes("text-sm font-bold")
                    line = ui.label("").classes("font-mono text-sm")
                    note = ui.label("").classes("text-xs text-gray-600")
                    table = _acft_fix_table()
                blocks["by"][name] = (head, line, note, table)

    def _refresh():
        if blocks["names"] != FEEDS.names():
            _build()
        for name, lst in FEEDS.entries():
            head, line, note, table = blocks["by"][name]
            cur = lst.latest
            share = FEEDS.shares_with(name)
            head.set_text(f"{name}   {lst.group}:{lst.port}"
                          + (f" (shared with {', '.join(share)})" if share else ""))
            if not lst.running:
                line.set_text("feed not listening")
                line.classes(replace="font-mono text-sm text-gray-500")
                note.set_text("turn the feed on in the engineering Aircraft feed tab or the "
                              "mission planner; the last fix, if any, stays below")
            elif cur is None:
                line.set_text(f"listening {lst.group}:{lst.port} on {lst.iface_ip}: "
                              f"no fix yet (rx {lst.rx})")
                line.classes(replace="font-mono text-sm text-amber-700")
                note.set_text("")
            else:
                age = lst.age_s
                line.set_text(f"{acft_summary_text(cur)}   {age:.1f} s ago")
                line.classes(replace="font-mono text-sm " + (
                    "text-red-700" if age > ACFT_FRESH_S else "text-emerald-800"))
                note.set_text(f"from {', '.join(cur.sentences or [cur.source])} on "
                              f"{lst.group}:{lst.port}; a field with -- was not in "
                              f"the last {lst.window_s:.1f} s of sentences")
            table.rows = acft_fix_rows(cur)
            table.update()

    _build()
    ui.timer(1.0, _refresh)
    exp.on_value_change(lambda e: _refresh())
    return exp


def _aircraft_color(name) -> str:
    """The marker colour of this aircraft on the planner map, by list order."""
    from dpmg.mission.map_element import AIRCRAFT_COLORS
    names = FEEDS.names()
    i = names.index(name) if name in names else 0
    return AIRCRAFT_COLORS[i % len(AIRCRAFT_COLORS)]


def _build_acft_mcast_panel():
    """Engineering SDR tab control for the aircraft position feeds: pick an
    aircraft, set its group, port, interface and format, Listen or Stop it,
    and read its live status line and fix table. Add, rename and remove live
    on the Aircraft feed tab. The listeners are the shared ones."""
    with ui.expansion("Aircraft position (multicast)", icon="flight",
                      value=False).classes("w-full border rounded"):
        with ui.row().classes("items-center gap-3 flex-wrap"):
            ac_sel = ui.select(FEEDS.names(), value=FEEDS.first_name(), label="Aircraft") \
                .props("dense outlined").classes("w-44") \
                .tooltip("which saved aircraft feed these fields and buttons act on; the "
                         "list is edited on the Aircraft feed tab")
            grp_in = ui.input("Multicast group", value="") \
                .props("dense outlined").classes("w-44")
            port_in = ui.input("Port", value="") \
                .props("dense outlined").classes("w-28")
            # which interface joins the group.  0.0.0.0 follows the default
            # route (usually Wi-Fi), so a feed arriving on the lab switch is
            # missed; pick that interface's own address instead.
            _ifaces = ["0.0.0.0"] + local_ipv4_addresses()
            iface_sel = ui.select(_ifaces, value="0.0.0.0", label="Interface",
                                  with_input=True, new_value_mode="add-unique") \
                .props("dense outlined").classes("w-44")
            fmt_sel = ui.select(list(FEED_FORMATS), value="auto", label="Format") \
                .props("dense outlined").classes("w-28")
            start_btn = ui.button("Listen").props("color=positive dense")
            stop_btn = ui.button("Stop").props("color=negative outline dense")
            stat_lbl = ui.label("stopped").classes("font-mono text-xs "
                                                    "self-center")
        share_lbl = ui.label("").classes("text-xs text-amber-700")
        ui.label("Format auto: NMEA (GGA, RMC, GLL, VTG, GSA, GSV from any talker) or a "
                 "JSON object with lat/lon/alt keys, sniffed per datagram; json or nmea "
                 "takes only that form. The sentences of one burst are combined, so the "
                 "table below says which sentence each value came from and how old it is. "
                 "A fresh fix here overrides the radio STATUS GPS in the header. The values "
                 "are saved per aircraft and shared with the mission planner and the "
                 "engineering Aircraft feed panel.") \
            .classes("text-xs text-gray-600")
        fix_table = _acft_fix_table()

        def _lst():
            return FEEDS.get(ac_sel.value) or FEEDS.primary

        # what the fields last showed: Listen writes back only the fields
        # changed here, and the fields follow a save made on another page
        shown = PanelFields(FEEDS)

        def _field_values():
            return {"group": grp_in.value, "port": port_in.value,
                    "iface": iface_sel.value, "format": fmt_sel.value}

        def _fill(keep_edits=False):
            e = shown.fill(ac_sel.value, _field_values() if keep_edits else None)
            grp_in.set_value(e.get("group", ""))
            port_in.set_value(str(e.get("port", "")))
            if e.get("iface") and e["iface"] not in iface_sel.options:
                iface_sel.set_options(list(iface_sel.options) + [e["iface"]])
            iface_sel.set_value(e.get("iface", "0.0.0.0"))
            fmt_sel.set_value(e.get("format", "auto"))
            share_lbl.set_text(FEEDS.socket_note(ac_sel.value))
        _fill()
        ac_sel.on_value_change(lambda e: _fill())

        def _pick_format(_e=None):
            # a picked format is saved at once and a running listener takes
            # it on its next datagram; a fill from code is no edit
            try:
                res = shown.apply(_field_values())
            except OSError as ex:
                ui.notify(f"format not saved: {ex}", type="negative")
                return
            if res is not None and res[2]:
                MGR._evt(f"aircraft feed {res[0]}: format {res[1]['format']}")
                _fill(keep_edits=True)
        fmt_sel.on_value_change(_pick_format)

        def _start():
            try:
                int(str(port_in.value).strip())
            except ValueError:
                ui.notify(f"listen failed: port {port_in.value!r} is not a number", type="negative")
                return
            try:
                # a running listener whose group, port or interface changed
                # has already started again on the new ones
                name, _e, _changed = FEEDS.update(ac_sel.value, shown.edits(_field_values()))
                if not FEEDS.running(name):
                    FEEDS.start(name)
                lst = FEEDS.get(name)
                ui.notify(f"{name}: listening {lst.group}:{lst.port} on {lst.iface_ip} "
                          f"({lst.fmt})", type="positive")
                MGR._evt(f"aircraft feed {name}: listening {lst.group}:{lst.port} on {lst.iface_ip}")
            except (ValueError, OSError) as e:
                ui.notify(f"listen failed: {e}", type="negative")
            _fill()

        def _stop():
            FEEDS.stop(ac_sel.value)
            MGR._evt(f"aircraft feed {ac_sel.value}: stopped")
            ui.notify(f"{ac_sel.value}: stopped")

        start_btn.on_click(_start)
        stop_btn.on_click(_stop)

        def _refresh():
            FEEDS.follow_file()
            if list(ac_sel.options) != FEEDS.names():
                ac_sel.set_options(FEEDS.names(), value=FEEDS.resolve(ac_sel.value))
                _fill()
            elif shown.stale():
                _fill(keep_edits=True)
            lst = _lst()
            cur = lst.latest if lst is not None else None
            if lst is None or not lst.running:
                stat_lbl.set_text("stopped")
            elif cur is None:
                stat_lbl.set_text(f"listening: rx={lst.rx} bad={lst.bad}, "
                                  f"no fix yet")
            else:
                stat_lbl.set_text(f"{acft_summary_text(cur)} "
                                  f"[{cur.source}] {lst.age_s:.1f}s ago "
                                  f"(rx={lst.rx})")
            fix_table.rows = acft_fix_rows(cur)
            fix_table.update()

        ui.timer(0.5, _refresh)


def _build_carve_panel(psd_chart, get_host):
    """Carve one band out of the live RX and save its IQ on the radio. Drag a
    lineX box on the PSD to fill [f_lo, f_hi] (or type them), pick a filter, and
    save: the radio mixes that band to DC, filters, decimates, and writes it."""
    with ui.expansion("Carve region and save IQ on radio", icon="content_cut",
                      value=False).classes("w-full border rounded"):
        with ui.row().classes("items-center gap-2 flex-wrap"):
            flo = ui.number("f_lo (Hz)", value=None, format="%.0f") \
                .props("dense outlined").classes("w-40")
            fhi = ui.number("f_hi (Hz)", value=None, format="%.0f") \
                .props("dense outlined").classes("w-40")
            filt = ui.select(["hann", "hamming", "blackman", "rect", "none"],
                             value="hann", label="Filter") \
                .props("dense outlined").classes("w-32")
            secs = ui.number("seconds", value=1.0, format="%.2f") \
                .props("dense outlined").classes("w-24")
            fmt = ui.select(["c64", "dpmg"], value="c64", label="Format") \
                .props("dense outlined").classes("w-28")
            name = ui.input("Name", value="region") \
                .props("dense outlined").classes("w-32")
            carve_btn = ui.button("Carve & save").props("color=primary dense")
            amc_btn = ui.button("Classify modulation").props("color=secondary dense")
            survey_btn = ui.button("Survey all signals").props("color=accent dense")
        stat = ui.label("drag a box on the PSD, or type f_lo/f_hi").classes(
            "font-mono text-xs text-gray-600")
        amc_lbl = ui.label("").classes("font-mono text-sm font-bold text-indigo-700")
        survey_tbl = ui.column().classes("gap-0")

        # brushEnd fires with the selected axis range in Hz: fill the inputs
        def _on_brush(e):
            try:
                areas = (e.args or {}).get("areas") or []
                if not areas:
                    return
                lo, hi = areas[0]["coordRange"]
                flo.value = float(min(lo, hi))
                fhi.value = float(max(lo, hi))
                bw = abs(hi - lo)
                stat.set_text(f"selected {flo.value/1e6:.4f} to {fhi.value/1e6:.4f} "
                              f"MHz  ({bw/1e3:.1f} kHz)")
            except Exception as ex:
                stat.set_text(f"brush parse: {ex}")
        psd_chart.on("chart:brushEnd", _on_brush)

        async def _carve():
            host = get_host()
            if host is None:
                ui.notify("no host selected", type="warning"); return
            if flo.value is None or fhi.value is None or fhi.value <= flo.value:
                ui.notify("set f_lo < f_hi (drag a box on the PSD)",
                          type="warning"); return
            stat.set_text("carving...")
            try:
                ack = await host.cmd("extract_region", {
                    "f_lo_hz": float(flo.value), "f_hi_hz": float(fhi.value),
                    "filter": filt.value, "seconds": float(secs.value),
                    "name": name.value, "fmt": fmt.value}, timeout=30.0)
                r = ack.result or {}
                if ack.status == 0:
                    # The radio keeps a capture down to 50 % of the request
                    short = ""
                    if r.get("requested_s") and r.get("captured_s") is not None \
                            and r["captured_s"] < 0.95 * r["requested_s"]:
                        short = (f"  Warning: only captured {r['captured_s']:g} of "
                                 f"{r['requested_s']:g} s (RX could not keep up)")
                    stat.set_text(f"saved {r.get('path')}  "
                                  f"({r.get('n_out')} samp @ "
                                  f"{r.get('out_rate_hz',0)/1e3:.1f} kHz, "
                                  f"decim {r.get('decim')}){short}")
                    ui.notify(f"carved -> {r.get('path')}" + (" (short capture)" if short else ""),
                              type="warning" if short else "positive")
                else:
                    stat.set_text(f"failed: {r.get('err')}")
                    ui.notify(f"carve failed: {r.get('err')}", type="negative")
            except Exception as e:
                stat.set_text(f"error: {e}")
                ui.notify(f"carve error: {e}", type="negative")
        carve_btn.on_click(_carve)

        async def _classify():
            host = get_host()
            if host is None:
                ui.notify("no host selected", type="warning"); return
            if flo.value is None or fhi.value is None or fhi.value <= flo.value:
                ui.notify("drag a box around one signal on the PSD first",
                          type="warning"); return
            amc_lbl.set_text(""); stat.set_text("classifying...")
            try:
                ack = await host.cmd("classify_selection", {
                    "f_lo": float(flo.value), "f_hi": float(fhi.value),
                    "seconds": float(secs.value)}, timeout=30.0)
                r = ack.result or {}
                if ack.status == 0:
                    sr = r.get("sym_rate_hz", 0) or 0
                    amc_lbl.set_text(
                        f"{r.get('modulation','?')}   "
                        f"(conf {r.get('confidence',0):.0%}, "
                        f"{r.get('family','?')}" +
                        (f", ~{sr/1e3:.1f} ksym/s" if sr else "") + ")")
                    stat.set_text(f"classified {r.get('rf_center_hz',0)/1e6:.4f} MHz "
                                  f"span {r.get('sel_bw_hz',0)/1e3:.1f} kHz "
                                  f"@ {r.get('capture_rate_hz',0)/1e3:.0f} kHz")
                else:
                    stat.set_text(f"failed: {r.get('err')}")
                    ui.notify(f"classify failed: {r.get('err')}", type="negative")
            except Exception as e:
                stat.set_text(f"error: {e}")
                ui.notify(f"classify error: {e}", type="negative")
        amc_btn.on_click(_classify)

        async def _survey():
            host = get_host()
            if host is None:
                ui.notify("no host selected", type="warning"); return
            survey_tbl.clear(); amc_lbl.set_text(""); stat.set_text("surveying the band...")
            try:
                ack = await host.cmd("survey_classify",
                                     {"seconds": 0.3, "thr_db": 12.0,
                                      "min_sep_hz": 400e3}, timeout=60.0)
                r = ack.result or {}
                if ack.status != 0:
                    stat.set_text(f"survey failed: {r.get('err')}")
                    ui.notify(f"survey failed: {r.get('err')}", type="negative"); return
                sigs = r.get("signals", [])
                stat.set_text(f"{len(sigs)} signals @ floor {r.get('floor_db',0):.0f} dB")
                with survey_tbl:
                    for s in sigs:
                        sr = s.get("sym_rate_hz", 0) or 0
                        ui.label(
                            f"{s['rf_hz']/1e6:.3f} MHz  {s['snr_db']:.0f} dB  "
                            f"bw~{s['bw_hz']/1e3:.0f} kHz  ->  {s['modulation']} "
                            f"({s['family']}, {s['confidence']:.0%})").classes(
                            "font-mono text-xs")
            except Exception as e:
                stat.set_text(f"survey error: {e}")
                ui.notify(f"survey error: {e}", type="negative")
        survey_btn.on_click(_survey)


def _build_tools_panel(get_host):
    """Bench/flight-line tools: synth a test signal onto the radio's disk (and
    optionally load it as the TX waveform), and run an automated ground checkout
    (RX noise floor, then, only when armed, a TX to RX loopback + isolation
    read). Arming radiates on the inited TX handle, so gate it on the pad/specA
    being connected exactly like the TX tab."""
    with ui.expansion("Tools: test signals & ground checkout", icon="build",
                      value=False).classes("w-full border rounded"):

        # test-signal generator
        ui.label("Test-signal generator").classes("text-sm font-bold")
        with ui.row().classes("items-center gap-2 flex-wrap"):
            kind = ui.select(["chirp", "tone", "two_tone", "noise"],
                             value="chirp", label="Kind") \
                .props("dense outlined").classes("w-32")
            secs = ui.number("seconds", value=0.1, format="%.3f") \
                .props("dense outlined").classes("w-24")
            # kind-specific offsets (baseband, Hz from the TX LO)
            f0 = ui.number("f0/freq/f1 (Hz)", value=-400000, format="%.0f") \
                .props("dense outlined").classes("w-40")
            f1 = ui.number("f1/f2 (Hz)", value=400000, format="%.0f") \
                .props("dense outlined").classes("w-32")
            bw = ui.number("bw (Hz)", value=1000000, format="%.0f") \
                .props("dense outlined").classes("w-32")
            ampl = ui.number("ampl", value=0.5, format="%.2f") \
                .props("dense outlined").classes("w-20")
            gname = ui.input("Name", value="chirp") \
                .props("dense outlined").classes("w-28")
            load_tx = ui.checkbox("load as TX").props("dense")
            gen_btn = ui.button("Generate & save").props("color=primary dense")
        gstat = ui.label("chirp: f0 to f1 sweep, tone: freq, two_tone: f1,f2, "
                         "noise: bw").classes("font-mono text-xs text-gray-600")

        async def _gen():
            host = get_host()
            if host is None:
                ui.notify("no host selected", type="warning"); return
            k = kind.value
            params = {"kind": k, "seconds": float(secs.value),
                      "ampl": float(ampl.value), "name": gname.value,
                      "load_tx": bool(load_tx.value)}
            # map the shared boxes onto each generator's argument names
            if k == "chirp":
                params["f0"] = float(f0.value); params["f1"] = float(f1.value)
            elif k == "tone":
                params["freq"] = float(f0.value)
            elif k == "two_tone":
                params["f1"] = float(f0.value); params["f2"] = float(f1.value)
            elif k == "noise":
                params["bw"] = float(bw.value)
            gstat.set_text("generating...")
            try:
                ack = await host.cmd("gen_test_waveform", params, timeout=20.0)
                r = ack.result or {}
                if ack.status == 0:
                    for wm in r.get("warnings") or []:     # flag silent problems
                        ui.notify(wm, type="warning", multi_line=True,
                                  close_button=True, timeout=15000)
                    gstat.set_text(("Warning: " + (r.get("warnings") or [""])[0]) if r.get("warnings")
                                   else (f"saved {r.get('path')}  ({r.get('n')} samp @ "
                                   f"{r.get('fs',0)/1e6:.3f} MSps"
                                   f"{', loaded as TX' if r.get('loaded_tx') else ''})"))
                    ui.notify(f"generated -> {r.get('path')}"
                              + (f" ({len(r.get('warnings'))} warning(s))" if r.get("warnings") else ""),
                              type="warning" if r.get("warnings") else "positive")
                else:
                    gstat.set_text(f"failed: {r.get('err')}")
                    ui.notify(f"gen failed: {r.get('err')}", type="negative")
            except Exception as e:
                gstat.set_text(f"error: {e}")
                ui.notify(f"gen error: {e}", type="negative")
        gen_btn.on_click(_gen)

        ui.separator()

        # automated ground checkout
        ui.label("Automated ground checkout").classes("text-sm font-bold")
        with ui.row().classes("items-center gap-2 flex-wrap"):
            co_secs = ui.number("capture s", value=0.1, format="%.3f") \
                .props("dense outlined").classes("w-24")
            co_off = ui.number("loopback tone (Hz)", value=100000, format="%.0f") \
                .props("dense outlined").classes("w-40")
            co_armed = ui.checkbox("ARM (TX radiates)").props("dense color=red")
            co_btn = ui.button("Run checkout").props("color=primary dense")
        ui.label("Unarmed = RX noise-floor only. ARM keys the PA on the inited "
                 "TX handle, so only with the pad/specA connected.") \
            .classes("text-xs text-orange-700")
        co_table = ui.table(
            columns=[{"name": "test", "label": "test", "field": "test"},
                     {"name": "pass", "label": "pass", "field": "pass"},
                     {"name": "detail", "label": "detail", "field": "detail"}],
            rows=[], row_key="test").classes("w-full").props("dense")
        co_stat = ui.label("").classes("font-mono text-xs")

        async def _checkout():
            host = get_host()
            if host is None:
                ui.notify("no host selected", type="warning"); return
            if co_armed.value:
                ui.notify("ARMED: TX will radiate on the inited TX handle",
                          type="warning")
            co_stat.set_text("running checkout...")
            co_table.rows = []
            try:
                ack = await host.cmd("ground_checkout",
                                     {"seconds": float(co_secs.value),
                                      "armed": bool(co_armed.value),
                                      "tone_offset_hz": float(co_off.value)},
                                     timeout=30.0)
                r = ack.result or {}
                if ack.status != 0:
                    co_stat.set_text(f"failed: {r.get('err')}")
                    ui.notify(f"checkout failed: {r.get('err')}", type="negative")
                    return
                rows = []
                for t in r.get("report", []):
                    det = "  ".join(f"{k}={v}" for k, v in t.items()
                                    if k not in ("test", "pass"))
                    rows.append({"test": t.get("test"),
                                 "pass": "PASS" if t.get("pass") else "FAIL",
                                 "detail": det})
                co_table.rows = rows
                ok = r.get("pass")
                co_stat.set_text("PASS" if ok else "FAIL: see rows")
                ui.notify("checkout PASS" if ok else "checkout FAIL",
                          type="positive" if ok else "negative")
            except Exception as e:
                co_stat.set_text(f"error: {e}")
                ui.notify(f"checkout error: {e}", type="negative")
        co_btn.on_click(_checkout)

        ui.separator()

        # channelizer / read-IQ checkout
        ui.label("Channelizer test: read IQ + detect").classes("text-sm font-bold")
        with ui.row().classes("items-center gap-2 flex-wrap"):
            ct_secs = ui.number("read s", value=0.1, format="%.3f") \
                .props("dense outlined").classes("w-24")
            ct_thr = ui.number("thr dB", value=12, format="%d") \
                .props("dense outlined").classes("w-24")
            ct_btn = ui.button("Read IQ & detect").props("color=primary dense")
        ct_table = ui.table(
            columns=[{"name": "f", "label": "offset kHz", "field": "f"},
                     {"name": "rf", "label": "RF MHz", "field": "rf"},
                     {"name": "snr", "label": "SNR dB", "field": "snr"}],
            rows=[], row_key="f").classes("w-full").props("dense")
        ct_stat = ui.label("").classes("font-mono text-xs")

        async def _chantest():
            host = get_host()
            if host is None:
                ui.notify("no host selected", type="warning"); return
            ct_stat.set_text("reading...")
            try:
                ack = await host.cmd("channelizer_test",
                                     {"seconds": float(ct_secs.value),
                                      "thr_db": float(ct_thr.value)}, timeout=20.0)
                r = ack.result or {}
                if ack.status != 0:
                    ct_stat.set_text(f"failed: {r.get('err')}"); return
                ct_table.rows = [{"f": round(s["freq_hz"] / 1e3, 1),
                                  "rf": round(s.get("rf_hz", 0) / 1e6, 4),
                                  "snr": s["snr_db"]} for s in r.get("signals", [])]
                ct_stat.set_text(f"{r.get('n')} signals, floor {r.get('floor_db')} dB "
                                 f"@ {r.get('fs',0)/1e6:.2f} MSps")
            except Exception as e:
                ct_stat.set_text(f"error: {e}")
        ct_btn.on_click(_chantest)

        ui.separator()

        # External PA / TX-RX switch / attenuator
        # Manual bench controls for the GPIO sequencer (rf_frontend.py) and the
        # Mini-Circuits attenuator (mc_atten.py). The pin map lives in
        # rf_frontend.yaml on the radio; Reload re-maps it without a restart.
        ui.label("RF front-end: PA enable / TX-RX switch / attenuator") \
            .classes("text-sm font-bold")
        with ui.row().classes("items-center gap-2 flex-wrap"):
            fe_pa_on = ui.button("PA ON").props("color=red dense")
            fe_pa_off = ui.button("PA OFF").props("color=primary dense")
            fe_tr_tx = ui.button("Switch to TX").props("dense outline")
            fe_tr_rx = ui.button("Switch to RX").props("dense outline")
            fe_safe = ui.button("SAFE").props("color=orange dense")
            fe_stat_btn = ui.button("Status").props("dense flat")
            fe_reload = ui.button("Reload config").props("dense flat")
        with ui.row().classes("items-center gap-2 flex-wrap"):
            at_db = ui.number("atten dB", value=120.0, min=0, max=120, step=0.05,
                              format="%.2f").props("dense outlined").classes("w-28")
            at_set = ui.button("Set").props("color=primary dense")
            at_read = ui.button("Read").props("dense outline")
            at_startup = ui.checkbox("also power-on default").props("dense")
            at_lbl = ui.label("atten: -").classes("font-mono text-xs")
        fe_lbl = ui.label("front-end: press Status").classes("font-mono text-xs")
        fe_lines = ui.label("").classes("font-mono text-xs text-gray-600")
        ui.label("PA ON keys the amplifier for real when frontend: gpio. Refused while a "
                 "TX run is active. Lines are forced LOW on SAFE, reload, stop and exit.") \
            .classes("text-xs text-orange-700")

        def _fe_render(d):
            if not d:
                fe_lbl.set_text("front-end: no reply"); return
            fe = d.get("frontend") if isinstance(d.get("frontend"), dict) else None
            if fe is None:
                fe_lbl.set_text(f"front-end: {d.get('configured', 'none')} "
                                f"(config {d.get('config_path')})")
                fe_lines.set_text("")
            else:
                pa, tr, fl = fe.get("pa", {}), fe.get("tr", {}), fe.get("fault", {})
                fe_lbl.set_text(
                    f"front-end: {fe.get('frontend')}  PA {'KEYED' if pa.get('keyed') else 'off'}"
                    f" (level {pa.get('level')})  switch {'TX' if tr.get('tx') else 'RX'}"
                    f" [{tr.get('mode')}]  fault {fl.get('active') if fl.get('enabled') else 'n/a'}"
                    f"  hold {fe.get('hold')}  wd {fe.get('watchdog_ms')} ms"
                    f" trips {fe.get('watchdog_trips')}/{fe.get('fault_trips')}"
                    + (f"  ERR {fe.get('last_error')}" if fe.get("last_error") else ""))

                def _ln(name, x):
                    if not isinstance(x, dict) or "offset" not in x:
                        return f"{name}: {x.get('mode', 'n/a') if isinstance(x, dict) else x}"
                    return (f"{name}: {x.get('chip')}:{x.get('offset')} "
                            f"({x.get('line')} via {x.get('by')})")
                fe_lines.set_text("  |  ".join([_ln("PA", pa), _ln("switch", tr),
                                               _ln("fault", fl) if fl.get("enabled") else "fault: disabled",
                                               f"config {fe.get('config_source')}"]))
            a = d.get("atten")
            if isinstance(a, dict) or a is None:
                at_lbl.set_text(atten_state_text(a))       # DEVICE_WATCH: auto attach state too

        async def _fe_cmd(name, params, ok_msg=None, timeout=10.0):
            host = get_host()
            if host is None:
                ui.notify("no host selected", type="warning"); return None
            try:
                ack = await host.cmd(name, params, timeout=timeout)
            except Exception as e:
                ui.notify(f"{name} error: {e}", type="negative"); return None
            r = ack.result or {}
            if ack.status != 0:
                # name the radio: these buttons act on the selected card
                ui.notify(f"{getattr(host, 'serial', '?')}: {name}: {r.get('err')}", type="negative")
                if r.get("frontend") or r.get("status"):
                    _fe_render(r.get("status") or {"frontend": r.get("frontend")})
                return None
            if ok_msg:
                ui.notify(ok_msg, type="positive")
            return r

        async def _fe_status():
            r = await _fe_cmd("frontend_status", {})
            if r is not None:
                _fe_render(r)

        async def _fe_set(**params):
            r = await _fe_cmd("frontend_set", params)
            if r is not None:
                _fe_render({"frontend": r.get("frontend"), "atten": "keep"})
                # say what the radio now drives, so a button press is visible even
                # with nothing on the lines
                fe = r.get("frontend") or {}
                pa, tr = fe.get("pa") or {}, fe.get("tr") or {}
                tr_txt = (f"switch {'TX' if tr.get('tx') else 'RX'}"
                          + (f" (line {'HIGH' if tr.get('level') else 'LOW'})"
                             if tr.get("level") is not None else f" [{tr.get('mode')}]"))
                sel = get_host()
                ui.notify(f"{getattr(sel, 'serial', '?')}: {tr_txt}, "
                          f"PA {'KEYED' if pa.get('keyed') else 'off'}",
                          type="warning" if pa.get("keyed") else "positive")
                await _fe_status()

        async def _fe_reload():
            r = await _fe_cmd("frontend_reload", {}, timeout=15.0)
            if r is not None:
                ui.notify(f"front-end reloaded: {r.get('loaded')} ({r.get('frontend_kind')})",
                          type="positive")
                _fe_render(r.get("status") or {})

        # The attenuator / front-end lines follow the radio when another
        # view (Payload tab, another GUI) changes them; own actions are not re-shown
        fe_seen = {"atten": _vs._MISSING, "fe": _vs._MISSING, "at_focus": False}
        at_db.on("focus", lambda _e=None: fe_seen.update(at_focus=True))
        at_db.on("blur", lambda _e=None: fe_seen.update(at_focus=False))

        async def _fe_follow():
            try:
                host = get_host()
                cfg = host_config(host)
                if cfg is None:
                    return
                a = cfg.get("atten_db")
                if a is not None and a != fe_seen["atten"]:
                    at_lbl.set_text(f"atten: {a} dB (radio)")
                    if not fe_seen["at_focus"]:
                        at_db.value = float(a)
                fe_seen["atten"] = a
                # DEVICE_WATCH: atten.transport auto, plugged in or pulled out
                ast = cfg.get("atten_state")
                if ast is not None and ast != fe_seen.get("atten_state"):
                    if fe_seen.get("atten_state") is not None:
                        await _fe_status()         # full readback: model, serial, dB
                    else:
                        at_lbl.set_text(atten_state_text({"state": ast, "att_db": a}))
                fe_seen["atten_state"] = ast
                fe = cfg.get("fe")
                first = fe_seen["fe"] is _vs._MISSING
                if fe is not None and not first and fe != fe_seen["fe"]:
                    fe_seen["fe"] = fe
                    await _fe_status()             # full readback when something changed
                fe_seen["fe"] = fe
            except Exception:
                return
        ui.timer(1.0, _fe_follow)

        async def _at_set():
            r = await _fe_cmd("atten_set", {"db": float(at_db.value),
                                            "startup": bool(at_startup.value)})
            if r is not None:
                fe_seen["atten"] = r.get("readback_db", r.get("set_db"))
                at_lbl.set_text(f"atten: set {r.get('set_db')} dB, readback {r.get('readback_db')} dB"
                                + (f", power-on {r.get('startup_db')} dB" if r.get("startup_db") is not None else ""))

        async def _at_read():
            r = await _fe_cmd("atten_get", {"ident": True})
            if r is not None:
                at_lbl.set_text(f"atten: {r.get('att_db')} dB  [{r.get('transport')}]"
                                f"  {r.get('model', '')} {r.get('serial', '')}"
                                + (f"  ERR {r.get('err')}" if r.get("err") else ""))
                if r.get("att_db") is not None:
                    at_db.value = float(r["att_db"])
                    fe_seen["atten"] = r["att_db"]

        fe_pa_on.on_click(lambda: _fe_set(pa=True))
        fe_pa_off.on_click(lambda: _fe_set(pa=False))
        fe_tr_tx.on_click(lambda: _fe_set(tr="tx"))
        fe_tr_rx.on_click(lambda: _fe_set(tr="rx"))
        fe_safe.on_click(lambda: _fe_set(safe=True))
        fe_stat_btn.on_click(_fe_status)
        fe_reload.on_click(_fe_reload)
        at_set.on_click(_at_set)
        at_read.on_click(_at_read)

        ui.separator()

        # transponder: detect then ring then JSON response then latency
        # Emulated TX by default (no radiate) until a 2nd RF cable closes the
        # loop; ARM+not-emulate keys the loaded response over the wire.
        ui.label("Transponder: detect, respond, measure latency").classes("text-sm font-bold")
        with ui.row().classes("items-center gap-2 flex-wrap"):
            tp_secs = ui.number("watch s", value=3.0, format="%.1f") \
                .props("dense outlined").classes("w-24")
            tp_snr = ui.number("trigger SNR", value=15, format="%d") \
                .props("dense outlined").classes("w-28")
            tp_emul = ui.checkbox("emulate TX", value=True).props("dense")
            tp_arm = ui.checkbox("ARM (real TX)").props("dense color=red")
            tp_btn = ui.button("Run transponder").props("color=primary dense")
        ui.label("Response message (JSON): loaded from file or edited here") \
            .classes("text-xs text-gray-600")
        tp_json = ui.textarea(
            value='{"message": "ACK", "modulation": "FSK", "symbol_rate": 4800, '
                  '"tone_spacing_hz": 5000, "offset_hz": 0}') \
            .props("dense outlined autogrow").classes("w-full font-mono text-xs")
        with ui.row().classes("items-center gap-2"):
            tp_upload = ui.upload(label="Load response .json", auto_upload=True,
                                  max_files=1).props("dense").classes("max-w-xs")
        tp_lat = ui.label("").classes("font-mono text-sm")
        tp_ev = ui.label("").classes("font-mono text-xs text-gray-600")

        async def _on_json_upload(e):
            try:
                tp_json.value = await e.file.text()
                ui.notify("response JSON loaded", type="positive")
            except Exception as ex:
                ui.notify(f"json load: {ex}", type="negative")
        tp_upload.on_upload(_on_json_upload)

        async def _transponder():
            host = get_host()
            if host is None:
                ui.notify("no host selected", type="warning"); return
            import json as _json
            try:
                resp = _json.loads(tp_json.value)
            except Exception as ex:
                ui.notify(f"bad response JSON: {ex}", type="negative"); return
            if tp_arm.value and not tp_emul.value:
                ui.notify("ARMED: transponder will key the real TX response",
                          type="warning")
            tp_lat.set_text("running...")
            tp_ev.set_text("")
            try:
                ack = await host.cmd("transponder",
                                     {"seconds": float(tp_secs.value),
                                      "trigger_snr_db": float(tp_snr.value),
                                      "response": resp,
                                      "emulate": bool(tp_emul.value),
                                      "armed": bool(tp_arm.value)},
                                     timeout=float(tp_secs.value) + 15.0)
                r = ack.result or {}
                if ack.status != 0:
                    tp_lat.set_text(f"failed: {r.get('err')}"); return
                st = r.get("stats") or {}
                if st:
                    pl = st["proc_latency_ms"]; tl = st["total_latency_ms"]
                    tp_lat.set_text(
                        f"{st['events']} responses, proc latency mean {pl['mean']} ms "
                        f"({pl['min']} to {pl['max']}), total {tl['mean']} ms "
                        f"(incl. buffering), resp={r.get('response')}, "
                        f"{'EMULATED' if r.get('emulated') else 'REAL TX'}")
                else:
                    tp_lat.set_text(f"no detections above trigger (resp={r.get('response')})")
                ev = r.get("events", [])[:6]
                tp_ev.set_text(", ".join(
                    f"{e['detect_khz']:+.0f}kHz {e['snr_db']:.0f}dB {e['proc_latency_ms']:.1f}ms"
                    for e in ev))
            except Exception as e:
                tp_lat.set_text(f"error: {e}")
        tp_btn.on_click(_transponder)

        ui.separator()
        build_jammer_panel(get_host)


# Both views copy the service's get_config snapshot (link_client polls it
# once per status period per radio, shared by every view/browser) into their
# controls, without fighting an operator who is editing (view_sync.EditGuard).
from dpmg.link import view_sync as _vs

CONFIG_FRESH_S = 5.0                # an older snapshot is not applied (link trouble)


def _vs_track(guard, key, el, svc_now):
    """Feed a widget's focus / client edits to the EditGuard. Focus only counts for
    typed fields: a dropdown keeps focus after a pick (until the next click
    elsewhere, even with the tab in the background) without being edited."""
    if not hasattr(el, "options"):
        el.on("focus", lambda _e=None, k=key: guard.focus(k, True))
        el.on("blur", lambda _e=None, k=key: guard.focus(k, False))
    el.on("update:model-value",
          lambda _e=None, k=key: guard.edited(k, svc_now.get(k, _vs._MISSING)))


def _vs_set(el, value):
    """Set a widget from the radio's value; True if it changed. Selects only take
    values they offer. Programmatic: never fires the client-only live-apply
    handlers (those listen to update:model-value)."""
    if hasattr(el, "options"):
        keys = list(el.options.keys()) if isinstance(el.options, dict) else list(el.options)
        if value not in keys:
            return False
    cur = el.value
    try:
        if cur is not None and value is not None and float(cur) == float(value):
            return False
    except (TypeError, ValueError):
        if cur == value:
            return False
    el.set_value(value)
    return True


def host_config(host):
    """The host's get_config snapshot when fresh, else None."""
    if host is None or not getattr(host, "config", None):
        return None
    if getattr(host, "config_age", float("inf")) > CONFIG_FRESH_S:
        return None
    return host.config


# G40 3.3 V GPS antenna bias checkbox + gpsd sky status, shown with
# the GPSDO reference clock (engineering Clock row and the operator TUNING card).
GPS_NO_SAT_HINT_S = 120.0
GPS_CLOCKS = ("gpsdo", "x40_gpsdo")        # the clock choices that show the bias and sky row
# a warm receiver keeps tracking through an unpowered active antenna at
# 7-14 dB-Hz, so "no satellites" also means weak ones
GPS_WEAK_CNO_DBHZ = 20.0
_GPS_BIAS_TIP = ("Powers the GPS antenna LNA with 3.3 V on the G40 GPS jack. Only for "
                 "ACTIVE antennas (e.g. u-blox ANN-MB) - some passive antennas are a DC "
                 "short. The G40 boots with it OFF; the setting is lost at reboot unless "
                 "the epiq-gps-ant-bias boot unit is installed on the radio.")


def fmt_gnss(g) -> str:
    """gps_status 'gnss' dict: 'GPS 3D fix  18 used / 18 heard  C/N0 44 dB-Hz'."""
    if not g:
        return "GPS sky: n/a (gpsd)"
    fix = g.get("fix") or "none"
    s = "GPS " + (f"{fix} fix" if fix in ("2D", "3D") else "no fix")
    s += f"  {int(g.get('sats_used') or 0)} used / {int(g.get('sats_visible') or 0)} heard"
    if isinstance(g.get("cno_max"), (int, float)):
        s += f"  C/N0 {g['cno_max']:.0f} dB-Hz"
    return s


def _bias_who_text(bias_on, cfg) -> str:
    """Who last switched the GPS antenna bias, from get_config."""
    if cfg is None or "ant_bias_by" not in cfg:
        return ""                                   # older service: says nothing
    if "ant_bias" in cfg and cfg["ant_bias"] is None:
        return ""                                   # not switchable here (the X40)
    w = cfg.get("ant_bias_by")
    if not w:
        return ("ON since before this radio service started (set by a bench script, "
                "a boot unit or by hand)") if bias_on else ""
    return f"turned {'ON' if w.get('enable') else 'OFF'} by {w.get('by')}, {w.get('at')}"


class GpsAntennaRow:
    """Checkbox for the G40 GPS antenna bias, driven only by the service readback
    (gps_status.ant_bias / the gps_antenna_bias ack), plus the no-satellite hint.
    poll=True runs its own 10 s gps_status poll (operator view); otherwise the
    owner feeds update(result) from its existing poll (engineering view)."""

    def __init__(self, get_host, get_clock, show_sky=False, poll=False):
        self.get_host, self.get_clock = get_host, get_clock
        self._sync = False
        self._zero_since = None
        self._serial = None
        with ui.row().classes("items-center gap-3 flex-wrap") as self.row:
            self.cb = ui.checkbox("GPS antenna bias 3.3 V (active antenna)", value=False) \
                .props("dense").tooltip(_GPS_BIAS_TIP)
            self.state_lbl = ui.label("bias: ?").classes("font-mono text-xs self-center")
            self.sky_lbl = ui.label("").classes("font-mono text-xs self-center")
            self.sky_lbl.set_visibility(bool(show_sky))
            self.hint_lbl = ui.label("").classes("text-xs font-bold text-amber-700 self-center")
            self.by_lbl = ui.label("").classes("text-xs text-gray-600 self-center")
            self.note_lbl = ui.label("not persistent: lost at G40 reboot unless the boot unit is installed") \
                .classes("text-xs text-gray-500 self-center")
        self.row.set_visibility(False)
        # Only a real click (the client's update:model-value) may ask
        # and send. on_value_change also fires for readback sets, and its async
        # handler runs after the _sync guard has cleared, so opening a view with the
        # bias on would pop the enable confirm by itself (and its Cancel would switch
        # the bias off).
        self._busy = False
        self.cb.on("update:model-value", self._on_toggle)
        ui.timer(1.0, self._vis)
        if poll:
            ui.timer(10.0, self._poll)

    async def _vis(self):
        try:
            on = self.get_clock() in GPS_CLOCKS and self.get_host() is not None
            was = self.row.visible
            self.row.set_visibility(on)
            h = self.get_host()
            if h is not None and h.serial != self._serial:     # focus moved: forget
                self._serial, self._zero_since = h.serial, None
            if on:
                self.by_lbl.set_text(_bias_who_text(self.cb.value, host_config(h)))
            if on and not was:
                await self._poll()                  # read back at once, not in 10 s
        except Exception:
            return

    def _set_cb(self, v):
        self._sync = True
        try:
            self.cb.value = bool(v)
        finally:
            self._sync = False

    def sync(self, b):
        """Follow get_config's ant_bias (shared, 1 Hz) between the slower
        gps_status polls. Never while a toggle is in flight."""
        if self._busy or b is None:
            return
        if bool(self.cb.value) != bool(b):
            self._set_cb(b)
            self.state_lbl.set_text("bias: ON" if b else "bias: OFF")

    def update(self, r: dict | None):
        r = r or {}
        if "ant_bias" not in r:
            return                                  # older service: leave as is
        b = r.get("ant_bias")
        self.note_lbl.set_visibility(b is not None)     # the G40 note means nothing on an X40
        if b is None:
            self._set_cb(False)
            self.cb.disable()
            why = r.get("ant_bias_err") or "not available on this radio"
            self.state_lbl.set_text(f"bias: n/a ({why})"[:90])
        else:
            self.cb.enable()
            self._set_cb(b)
            self.state_lbl.set_text("bias: ON" if b else "bias: OFF")
        g = r.get("gnss")
        self.sky_lbl.set_text(fmt_gnss(g))
        heard = int((g or {}).get("sats_visible") or 0)
        cno = (g or {}).get("cno_max")
        weak = heard == 0 or (isinstance(cno, (int, float)) and cno < GPS_WEAK_CNO_DBHZ)
        zero = bool(g) and weak and b is False
        now = time.monotonic()
        if not zero:
            self._zero_since = None
        elif self._zero_since is None:
            self._zero_since = now
        self.hint_lbl.set_text(
            ("No satellites" if heard == 0 else f"Weak satellites only (C/N0 {cno:.0f} dB-Hz)")
            + " - active antenna? enable the 3.3 V bias"
            if zero and now - self._zero_since >= GPS_NO_SAT_HINT_S else "")

    async def _poll(self):
        host = self.get_host()
        if host is None or self.get_clock() not in GPS_CLOCKS:
            return
        try:
            ack = await host.cmd("gps_status", {}, timeout=6.0)
        except Exception:
            return
        self.update((ack.result or {}) if ack else {})

    async def _confirm_enable(self) -> bool:
        with ui.dialog() as dlg, ui.card().classes("max-w-lg"):
            ui.label("Enable 3.3 V on the GPS antenna jack?").classes("text-base font-bold")
            ui.label("Only for an ACTIVE antenna that needs LNA power (e.g. u-blox ANN-MB, "
                     "3-5 V). Some PASSIVE antennas are a DC short at the connector: 3.3 V "
                     "into one can damage the antenna or the G40 bias supply.")
            ui.label("Check the antenna type first. The G40 boots with the bias OFF, and it "
                     "goes back OFF at every reboot.").classes("text-sm text-gray-600")
            with ui.row().classes("justify-end w-full"):
                ui.button("Cancel", on_click=lambda: dlg.submit(False)).props("flat")
                ui.button("Active antenna - enable 3.3 V",
                          on_click=lambda: dlg.submit(True)).props("color=warning")
        return bool(await dlg)

    async def _on_toggle(self, e=None):
        if self._sync or self._busy:
            return
        want = bool(self.cb.value)                  # NiceGUI applied the click already
        host = self.get_host()
        if host is None:
            self._set_cb(not want)
            return
        self._busy = True
        try:
            await self._toggle(host, want)
        finally:
            self._busy = False

    async def _toggle(self, host, want):
        # Never on by default: enabling 3.3 V is always an explicit,
        # confirmed operator decision (a passive antenna may be a DC short)
        if want and not await self._confirm_enable():
            self._set_cb(False)
            self.cb.update()
            return
        self.cb.disable()
        try:
            gid = getattr(getattr(host, "mgr", None), "gui_id", None) or "GUI"
            ack = await host.cmd("gps_antenna_bias", {"enable": want, "by": f"GUI {gid}"},
                                 timeout=6.0)
            r = (ack.result or {}) if ack else {}
        except Exception as ex:
            r = {"err": str(ex)}
            ack = None
        finally:
            self.cb.enable()
        if ack is None or ack.status != proto.CmdStatus.OK:
            ui.notify(f"GPS antenna bias: {r.get('err', 'failed')}", type="warning")
        if "ant_bias" in r:
            self.update({"ant_bias": r.get("ant_bias"), "gnss": None} |
                        ({"ant_bias_err": r["err"]} if r.get("ant_bias") is None and r.get("err") else {}))
            self.sky_lbl.set_text("GPS sky: refreshing...")
        else:
            self._set_cb(not want)
        self.cb.update()                            # re-render: the readback wins
        self._zero_since = None


def ref_words(clock_token, ref_ok, cfg) -> str:
    """The reference part of a status line. With the G40 service's clock check in
    get_config it names the 10 MHz verdict and the measured ppm; otherwise the old
    words from the STATUS flag. On a GPS disciplined clock the GNSS fix from
    get_config goes next to the lock (g40_clock.gpsdo_verdict). Starts with ", " or
    is empty."""
    ck = (cfg or {}).get("clock") or {}
    ch = _g40_clock.CLOCK_CHOICES.get(str(clock_token or ""), {})
    if ck.get("choice"):
        ch = _g40_clock.CLOCK_CHOICES.get(ck["choice"], ch)
    if ch.get("ref") in ("gpsdo", "host"):
        return ", " + _g40_clock.gpsdo_verdict(ch["ref"], bool(ref_ok), (cfg or {}).get("gnss"))["text"]
    if ck.get("choice"):
        t = _g40_clock.clock_text({"choice": ck["choice"], "ten_mhz": ck.get("ten_mhz"),
                                   "clock_ppm": ck.get("clock_ppm")})
        tail = t.split(", ", 1)[1] if ", " in t else ""
        return (", " + tail) if tail else ""
    if ref_ok:
        return ", ref LOCKED"
    if ch.get("ref") == "external":
        return ", external ref not confirmed"
    return ""


def ref_state(clock_token, ref_ok, cfg) -> str:
    """locked, no_fix, acquiring, lost or neutral, for the colour of the clock field.
    no_fix (amber) is a GPSDO locked with no GPS fix, or the X40 GPSDO with none."""
    ck = (cfg or {}).get("clock") or {}
    ch = _g40_clock.CLOCK_CHOICES.get(str(ck.get("choice") or clock_token or ""), {})
    if ck.get("ten_mhz") == "LOST":
        return "lost"
    if ch.get("ref") in ("gpsdo", "host"):
        v = _g40_clock.gpsdo_verdict(ch["ref"], bool(ref_ok), (cfg or {}).get("gnss"))["state"]
        return v if v in ("locked", "no_fix", "acquiring") else "neutral"
    if ch.get("ref") == "external":
        return "locked" if ref_ok else "acquiring"
    return "neutral"


# Picking a new reference clock on a running radio applies it at once
# (re-init with the current settings, RX restarted), no START / INITIALIZE click.
async def confirm_clock_change(host, new_clock) -> bool:
    """Ask first when a live clock change could hurt: external needs a real
    10 MHz + 1PPS (the card hangs at init without one), and the re-init stops
    an active transmission. True = go ahead."""
    why = []
    ch = _g40_clock.CLOCK_CHOICES.get(str(new_clock), {})
    if ch.get("ref") == "external":
        why.append(f"{ch['label']}: the 10 MHz must be on the REF jack. The service "
                   "switches the HMC7044 to it and refuses the init if it does not lock; "
                   "a 10 MHz lost later shows as 10 MHz LOST on the clock line.")
    if ch.get("pps") == "external":
        why.append("The 1 PPS is taken from the 1PPS jack; the line shows 1 PPS ABSENT "
                   "when nothing is there.")
    if (host is not None and host.status is not None
            and host.status.flags & proto.Status.FLAG_TX_ACTIVE):
        why.append("The radio is TRANSMITTING. The re-init stops the transmission; "
                   "start it again afterwards.")
    if not why:
        return True
    with ui.dialog() as dlg, ui.card():
        ui.label(f"Switch reference clock to {_g40_clock.label(new_clock)}?").classes("text-base font-bold")
        ui.label("This re-initializes the radio with the current settings.")
        for w in why:
            ui.label(w)
        with ui.row().classes("justify-end w-full"):
            ui.button("Cancel", on_click=lambda: dlg.submit(False)).props("flat")
            ui.button("Switch clock", on_click=lambda: dlg.submit(True)).props("color=warning")
    return bool(await dlg)


def _tx_where(get_txport):
    """One small line showing which TX port this panel's TRANSMIT/PLAY will key
    (the port the operator armed with START). Updates live via a timer."""
    if get_txport is None:
        return
    lbl = ui.label("").classes("text-xs")

    def _upd():
        p = get_txport()
        if p and str(p).startswith("LOCKED "):
            lbl.set_text(f"TX locked out: {str(p)[7:]}")
            lbl.classes(replace="text-xs text-amber-700 font-bold")
        elif p:
            lbl.set_text(f"Keys TX on {p}")
            lbl.classes(replace="text-xs text-red-700 font-bold")
        else:
            lbl.set_text("TX not armed: set a TX port and START a radio first")
            lbl.classes(replace="text-xs text-gray-500")
    _upd()
    ui.timer(1.0, _upd)


def build_jammer_panel(get_host, title="Jammer: resiliency test signals",
                       get_txport=None):
    """Jamming-signal generator for in-chamber resiliency testing. Builds a
    jamming waveform on the selected radio (loads it as the TX waveform) and keys
    it, gated by a radiate confirm. Reused by the operator view and the Tools
    panel. The radio must be initialized with a TX-capable mode first."""
    with ui.card().classes("w-full"):
        ui.label(title).classes("text-base font-bold text-red-700")
        _tx_where(get_txport)
        with ui.row().classes("items-center gap-2 flex-wrap"):
            jtype = ui.select({
                "barrage": "Barrage noise (full band)",
                "noise": "Partial-band noise",
                "tone": "Spot / CW",
                "comb": "Comb (multi-tone)",
                "swept": "Swept (sweep jammer)",
                "chirp": "Chirp / LFM",
                "modulated": "Modulated (symbol jammer)",
            }, value="barrage", label="Type").props("dense outlined").classes("w-56")
            # TX power = peak as a fraction of DAC full scale; the whole 0..1
            # range is usable (no clipping) thanks to peak-normalization
            jampl = ui.select({1.0: "1.00, 0 dBFS (max)", 0.71: "0.71, -3 dBFS",
                               0.5: "0.50, -6 dBFS", 0.35: "0.35, -9 dBFS",
                               0.25: "0.25, -12 dBFS", 0.18: "0.18, -15 dBFS",
                               0.1: "0.10, -20 dBFS", 0.05: "0.05, -26 dBFS"},
                              value=0.5, label="TX power (FS)").props(
                "dense outlined").classes("w-44")
            # loop length of the generated waveform; stress-tested stable to ~2 s
            # (at 5 s / 1.2 GB the buffer stops transmitting cleanly), min 0.05 s
            jsecs = ui.select({0.05: "0.05 s", 0.1: "0.1 s", 0.25: "0.25 s",
                               0.5: "0.5 s", 1.0: "1.0 s", 2.0: "2.0 s"},
                              value=0.5, label="loop s").props(
                "dense outlined").classes("w-28")
        # commafy helper: show each Hz field's value grouped (+ MHz) in a small
        # grey echo beside it, so long numbers are readable as you type. Browser
        # number inputs can't carry thousands separators themselves, hence the echo.
        _hzecho = {}

        def _hz(field):
            lbl = ui.label("").classes("text-xs text-gray-500 self-center")
            def _upd():
                try:
                    v = float(field.value or 0)
                except (TypeError, ValueError):
                    lbl.set_text(""); return
                lbl.set_text(f"{int(round(v)):,} Hz" +
                             (f" ({v/1e6:g} MHz)" if abs(v) >= 1000 else ""))
            field.on_value_change(lambda e: _upd()); _upd()
            _hzecho[field] = lbl
            return field

        with ui.row().classes("items-center gap-2 flex-wrap"):
            p_bw = _hz(ui.number("bandwidth (Hz)", value=1_000_000, format="%.0f").props(
                "dense outlined").classes("w-40"))
            p_freq = _hz(ui.number("offset / center (Hz)", value=0, format="%.0f").props(
                "dense outlined").classes("w-40"))
            p_spacing = _hz(ui.number("tone spacing (Hz)", value=200_000, format="%.0f").props(
                "dense outlined").classes("w-40"))
            p_ntones = ui.number("# tones", value=8, format="%.0f").props(
                "dense outlined").classes("w-24")
            p_span = _hz(ui.number("sweep span (Hz)", value=5_000_000, format="%.0f").props(
                "dense outlined").classes("w-40"))
            p_period = ui.number("sweep period (s)", value=0.01, format="%.4f").props(
                "dense outlined").classes("w-36")
            p_swnoise = _hz(ui.number("sweep noise bw (0=CW)", value=0, format="%.0f").props(
                "dense outlined").classes("w-44"))
            p_f0 = _hz(ui.number("chirp f0 (Hz)", value=-5_000_000, format="%.0f").props(
                "dense outlined").classes("w-36"))
            p_f1 = _hz(ui.number("chirp f1 (Hz)", value=5_000_000, format="%.0f").props(
                "dense outlined").classes("w-36"))
            # modulated (symbol) jammer: random constellation symbols, RRC-shaped.
            # Set Rs/constellation to the victim's: per Amuru & Buehrer a QPSK
            # symbol jammer out-hurts equal-power AWGN against M-QAM.
            p_mod = ui.select({"qpsk": "QPSK", "bpsk": "BPSK", "8psk": "8PSK",
                               "16qam": "16QAM", "64qam": "64QAM"},
                              value="qpsk", label="constellation").props(
                "dense outlined").classes("w-32")
            p_rs = _hz(ui.number("symbol rate (Hz)", value=100_000, format="%.0f").props(
                "dense outlined").classes("w-40"))
            # symbol on-off keying (lambda): fraction of symbols keyed, each at
            # 1/sqrt(lambda) so average power holds: the paper's "pulsed QPSK"
            p_symduty = ui.select({1.0: "1.00 (continuous)", 0.5: "0.50",
                                   0.25: "0.25", 0.12: "0.12", 0.06: "0.06"},
                                  value=1.0, label="symbol duty").props(
                "dense outlined").classes("w-40")
        with ui.row().classes("items-center gap-2 flex-wrap"):
            p_prf = _hz(ui.number("pulse PRF (Hz, 0=CW)", value=0, format="%.0f").props(
                "dense outlined").classes("w-44"))
            # pulse duty (fraction of each PRF period the signal is on); the SDR
            # produces clean pulses across the whole range: lower bound is
            # on-time >= ~1 sample (so ~0.01 at normal PRFs)
            p_duty = ui.select({0.9: "0.90", 0.75: "0.75", 0.5: "0.50",
                                0.25: "0.25", 0.1: "0.10", 0.05: "0.05",
                                0.02: "0.02", 0.01: "0.01"},
                               value=0.5, label="duty").props(
                "dense outlined").classes("w-28")
        # live pulse readout: recomputes on-time / period / loop coverage as PRF,
        # duty, or loop change, so the operator sees whether it will pulse
        # before building. (The post-build validator still re-checks vs the real
        # sample rate.) These are sample-rate-independent except the sub-sample
        # guard, which uses the fastest G40 rate as a conservative floor.
        jpulse = ui.label("").classes("font-mono text-xs text-amber-700")

        def _pulse_readout():
            try:
                prf = float(p_prf.value or 0)
                duty = float(p_duty.value or 1.0)
                loop = float(jsecs.value or 0)
            except (TypeError, ValueError):
                jpulse.set_text(""); return
            if prf <= 0:
                jpulse.classes(replace="font-mono text-xs text-gray-500")
                jpulse.set_text("pulse: CW (PRF 0), set PRF > 0 to pulse"); return
            period = 1.0 / prf
            on_s = duty / prf
            on_txt = (f"{on_s*1e6:.2f} µs" if on_s < 1e-3 else
                      f"{on_s*1e3:.1f} ms" if on_s < 1.0 else f"{on_s:.2f} s")
            per_txt = f"{period*1e3:.0f} ms" if period < 1.0 else f"{period:.2f} s"
            if on_s < 1.0 / 30.72e6:               # shorter than one sample at any rate
                jpulse.classes(replace="font-mono text-xs text-red-600")
                jpulse.set_text(f"Warning: pulse ON time {on_txt} is shorter than one sample, "
                                f"so it rounds to 0 and nothing transmits. Lower PRF / raise duty.")
                return
            if loop < period:
                jpulse.classes(replace="font-mono text-xs text-red-600")
                jpulse.set_text(f"Warning: pulse ON {on_txt}, period {per_txt}, loop "
                                f"{loop:g}s < period, so it transmits CW. Raise loop to >= {period:g}s.")
                return
            cycles = loop / period
            seam = "" if abs(cycles - round(cycles)) < 0.01 else "  (non-integer, leaves a seam)"
            jpulse.classes(replace="font-mono text-xs text-emerald-700")
            jpulse.set_text(f"pulse: ON {on_txt} every {per_txt}, "
                            f"{cycles:.1f} cycle(s)/loop OK{seam}")
        for _w in (p_prf, p_duty, jsecs):
            _w.on_value_change(lambda e: _pulse_readout())
        _pulse_readout()
        jstat = ui.label("pick a type, Build + load, then TRANSMIT").classes(
            "font-mono text-xs text-gray-600")
        with ui.row().classes("items-center gap-2"):
            jbuild = ui.button("Build + load").props("color=primary")
            jtx = ui.button("TRANSMIT").props("color=negative")
            jstop = ui.button("STOP").props("color=grey")

        _pmap = {"barrage": [], "noise": [p_bw, p_freq], "tone": [p_freq],
                 "comb": [p_spacing, p_ntones, p_freq],
                 "swept": [p_span, p_period, p_swnoise, p_freq],
                 "chirp": [p_f0, p_f1],
                 "modulated": [p_mod, p_rs, p_symduty, p_freq]}
        _allp = [p_bw, p_freq, p_spacing, p_ntones, p_span, p_period,
                 p_swnoise, p_f0, p_f1, p_mod, p_rs, p_symduty]

        def _sync():
            shown = _pmap.get(jtype.value, [])
            for w in _allp:
                w.set_visibility(w in shown)
                if w in _hzecho:                      # hide each Hz echo with its field
                    _hzecho[w].set_visibility(w in shown)
        jtype.on_value_change(lambda e: _sync())
        _sync()

        def _params():
            k = jtype.value
            p = {"kind": k, "seconds": float(jsecs.value),
                 "ampl": float(jampl.value), "name": f"jam_{k}", "load_tx": True}
            if float(p_prf.value) > 0:
                p["prf"] = float(p_prf.value)
                p["duty"] = float(p_duty.value)
            if k == "noise":
                p["bw"] = float(p_bw.value)
                p["center"] = float(p_freq.value)   # place the noise patch off DC
            elif k == "tone":
                p["freq"] = float(p_freq.value)     # spot/CW: a single coherent line
            elif k == "comb":
                p.update(spacing=float(p_spacing.value),
                         n_tones=float(p_ntones.value), center=float(p_freq.value))
            elif k == "swept":
                p.update(span=float(p_span.value),
                         sweep_period_s=float(p_period.value),
                         noise_bw=float(p_swnoise.value), center=float(p_freq.value))
            elif k == "chirp":
                p.update(f0=float(p_f0.value), f1=float(p_f1.value))
            elif k == "modulated":
                p.update(mod=str(p_mod.value), sym_rate=float(p_rs.value),
                         sym_duty=float(p_symduty.value), center=float(p_freq.value))
            return p

        async def _build():
            host = get_host()
            if host is None:
                ui.notify("no radio selected", type="warning"); return
            jstat.set_text("building jamming waveform...")
            try:
                ack = await host.cmd("gen_test_waveform", _params(), timeout=30.0)
                r = ack.result or {}
                if ack.status == 0:
                    warns = r.get("warnings") or []
                    for wm in warns:                       # flag silent problems
                        ui.notify(wm, type="warning", multi_line=True,
                                  close_button=True, timeout=15000)
                    if warns:
                        jstat.set_text("Warning: " + warns[0])
                    else:
                        jstat.set_text(f"loaded {jtype.value}: {r.get('n')} samp @ "
                                       f"{r.get('fs', 0)/1e6:.3f} MSps, ready to TRANSMIT")
                    ui.notify(f"waveform loaded ({len(warns)} warning(s))" if warns
                              else "jamming waveform loaded",
                              type="warning" if warns else "positive")
                else:
                    jstat.set_text(f"failed: {r.get('err')}")
                    ui.notify(f"gen failed: {r.get('err')}", type="negative")
            except Exception as e:
                jstat.set_text(f"error: {e}")
        jbuild.on_click(_build)

        async def _tx():
            host = get_host()
            if host is None:
                ui.notify("no radio selected", type="warning"); return
            with ui.dialog() as dlg, ui.card():
                ui.label("Confirm JAM: this will RADIATE").classes(
                    "text-lg font-bold text-red-600")
                ui.label(f"Host {host.serial}: radiate the '{jtype.value}' jamming "
                         f"waveform on its configured TX port, CONTINUOUS.")
                ui.label("Confirm the chamber / load is connected and safe to key.") \
                    .classes("text-sm text-gray-600")
                with ui.row():
                    ui.button("Cancel", on_click=dlg.close).props("flat")
                    ui.button("KEY JAM NOW",
                              on_click=lambda: dlg.submit("go")).props("color=negative")
            if await dlg != "go":
                return
            try:
                a1 = await host.cmd("gen_test_waveform", _params(), timeout=30.0)
                if a1.status != 0:
                    jstat.set_text(f"gen failed: {(a1.result or {}).get('err')}"); return
                for wm in (a1.result or {}).get("warnings") or []:   # flag problems
                    ui.notify(wm, type="warning", multi_line=True,
                              close_button=True, timeout=15000)
                ack = await host.cmd("start_tx", {"mode": "CONTINUOUS"}, timeout=15.0)
                if ack.status == 0:
                    jstat.set_text(f"JAMMING: {jtype.value} radiating (STOP to end)")
                    ui.notify("jamming active", type="warning")
                else:
                    jstat.set_text(f"start_tx failed: {(ack.result or {}).get('err')}")
            except Exception as e:
                jstat.set_text(f"error: {e}")
        jtx.on_click(_tx)

        async def _stop():
            host = get_host()
            if host is None:
                return
            try:
                await host.cmd("stop_tx", {}, timeout=10.0)
                jstat.set_text("jam stopped")
                ui.notify("jam stopped", type="positive")
            except Exception as e:
                jstat.set_text(f"error: {e}")
        jstop.on_click(_stop)

        # Clear the JAMMING line once the radio is no longer radiating, however
        # TX stopped (STOP ALL TX, the per-radio stop, or the waveform ending).
        # The radio's status flag is the source of truth; only clear after we
        # have seen TX active so a slow first heartbeat does not clear it early.
        _seen_tx = {"on": False}

        def _watch_tx():
            if not jstat.text.startswith("JAMMING:"):
                _seen_tx["on"] = False
                return
            host = get_host()
            st = getattr(host, "status", None) if host is not None else None
            if st is None:
                return
            if st.flags & proto.Status.FLAG_TX_ACTIVE:
                _seen_tx["on"] = True
            elif _seen_tx["on"]:
                jstat.set_text("jam stopped (TX off)")
                _seen_tx["on"] = False
        ui.timer(1.0, _watch_tx)


def _fmt_bytes(b):
    b = float(b or 0)
    return f"{b/1e9:.2f} GB" if b >= 1e9 else f"{b/1e6:.1f} MB" if b >= 1e6 \
        else f"{b/1e3:.0f} kB"


def _fmt_rate(r):
    r = float(r or 0)
    return f"{r/1e6:g} MSps" if r >= 1e6 else f"{r/1e3:.1f} kSps"


def build_record_replay_panel(get_host, get_txport=None, get_band=None,
                              band_hint="the spectrum"):
    """Record and replay (engineering view, under the DSSS builder): records a
    stretch of live RX on the radio (full band, or the band boxed on the PSD) and
    replays it on TX, once or looped, at a gain offset relative to as captured.
    The recording stays on the radio (data/recordings, newest few kept). The
    digital gain is capped at DAC full scale; larger level changes go through TX
    attenuation. Keying uses the same radiate confirm as the jammer. get_band(): (f_lo_hz, f_hi_hz) or None."""
    st = {"recs": [], "max_s": 5.0, "rate": None, "serial": None, "pending": None,
          "keep": 6, "max_total": 0, "total": 0}
    with ui.card().classes("w-full"):
        ui.label("Record & replay").classes("text-base font-bold")
        _tx_where(get_txport)
        # record
        ui.label("Record (receive side, does not transmit)").classes(
            "text-xs font-bold text-gray-600")
        with ui.row().classes("items-center gap-2 flex-wrap"):
            r_src = ui.select({"band": "Selected band (PSD box)",
                               "full": "Full band (as received)"},
                              value="band", label="Record").props(
                "dense outlined").classes("w-60")
            r_secs = ui.number("seconds", value=1.0, min=0.01, max=5.0, step=0.1,
                               format="%.2f").props("dense outlined").classes("w-28")
            r_label = ui.input("label", value="").props(
                "dense outlined maxlength=16").classes("w-32")
            r_btn = ui.button("RECORD", icon="fiber_manual_record").props("color=primary")
        r_band_lbl = ui.label("").classes("text-xs font-mono")
        r_cap_lbl = ui.label("").classes("text-xs text-gray-500")
        # recordings on the radio
        with ui.row().classes("items-center gap-2 flex-wrap"):
            rec_sel = ui.select({}, label="Recording on the radio").props(
                "dense outlined options-dense").classes("w-96")
            ref_btn = ui.button(icon="refresh").props("flat dense").tooltip("refresh list")
            del_btn = ui.button(icon="delete").props("flat dense color=grey").tooltip(
                "delete this recording from the radio")
        rec_info = ui.label("").classes("text-xs font-mono text-gray-600")
        # replay
        ui.label("Replay (transmits)").classes("text-xs font-bold text-red-700")
        with ui.row().classes("items-center gap-2 flex-wrap"):
            p_mode = ui.select({"SINGLE_SHOT": "Once", "CONTINUOUS": "Loop"},
                               value="SINGLE_SHOT", label="Replay").props(
                "dense outlined").classes("w-28")
            p_delay = ui.number("start delay (s)", value=0, min=0, max=60, step=1,
                                format="%.0f").props("dense outlined").classes("w-32")
            p_gain = ui.number("gain vs captured (dB)", value=0.0, step=0.5,
                               format="%.1f").props("dense outlined").classes("w-44")
            # Disabled: this mode needs measured TX power + RX calibration
            p_match = ui.checkbox("Match received power").props("dense disable")
            p_match.tooltip("Needs measured TX output power (dBm at 0 dBFS per TX "
                            "attenuation) and an RX dBFS-to-dBm calibration; neither "
                            "is measured yet, so this mode is not available")
        p_peak = ui.label("").classes("text-xs font-mono")
        ui.label("Digital gain stops at DAC full scale; for a bigger level change use "
                 "TX atten in the radio's TX settings.").classes("text-xs text-gray-500")
        pstat = ui.label("record something, or pick a recording, then REPLAY").classes(
            "font-mono text-xs text-gray-600")
        with ui.row().classes("items-center gap-2"):
            p_btn = ui.button("REPLAY").props("color=negative")
            s_btn = ui.button("STOP").props("color=grey")

        def _cur():
            for m in st["recs"]:
                if m.get("n") == rec_sel.value:
                    return m
            return None

        def _rec_text(m):
            band = ("full band" if m.get("m") == "full" else
                    f"band {float(m.get('c') or 0)/1e6:.4f} MHz "
                    f"+/- {float(m.get('w') or 0)/2e3:.1f} kHz")
            pk = m.get("p")
            return (f"{m.get('s', 0):.3f} s, {_fmt_rate(m.get('r'))}, "
                    f"{_fmt_bytes(m.get('b'))}, {band}, peak "
                    + (f"{pk:.1f} dBFS" if pk is not None else "?"))

        def _upd_band():
            if r_src.value == "full":
                r_band_lbl.set_text("full band: includes the radio's DC spur / LO "
                                    "leakage and anything else in the span")
                r_band_lbl.classes(replace="text-xs font-mono text-amber-700")
                return
            b = get_band() if get_band else None
            if b:
                lo, hi = b
                r_band_lbl.set_text(f"band {lo/1e6:.4f} to {hi/1e6:.4f} MHz "
                                    f"({(hi-lo)/1e3:.1f} kHz)")
                r_band_lbl.classes(replace="text-xs font-mono text-blue-700")
            else:
                r_band_lbl.set_text(f"drag a box on {band_hint} (toolbox: select band) "
                                    "around the signal to record")
                r_band_lbl.classes(replace="text-xs font-mono text-gray-500")

        def _upd_cap():
            rate = st["rate"]
            mx = st["max_s"]
            r_secs.props(f"max={mx}")
            if rate:
                r_cap_lbl.set_text(
                    f"max {mx:g} s at {_fmt_rate(rate)} (~{_fmt_bytes(mx*rate*8)} full "
                    f"band), radio keeps the newest {st['keep']} recordings, "
                    f"<= {_fmt_bytes(st['max_total'])} ("
                    f"{_fmt_bytes(st['total'])} used)")
            else:
                r_cap_lbl.set_text("start the radio to record")

        def _upd_peak():
            m = _cur()
            if m is None or m.get("p") is None:
                p_peak.set_text("")
                return
            try:
                g = float(p_gain.value or 0)
            except (TypeError, ValueError):
                g = 0.0
            pk = float(m["p"])
            gmax = min(40.0, -0.1 - pk)
            gg = min(g, gmax)
            txt = (f"replay peak {pk + gg:.1f} dBFS (captured {pk:.1f} dBFS, "
                   f"{gg:+.1f} dB), max digital gain {gmax:+.1f} dB")
            if g > gmax + 1e-9:
                p_peak.set_text(f"Warning: capped at {gmax:+.1f} dB (would clip), " + txt)
                p_peak.classes(replace="text-xs font-mono text-amber-700 font-bold")
            else:
                p_peak.set_text(txt)
                p_peak.classes(replace="text-xs font-mono text-emerald-700")

        def _fill(sel_name=None):
            opts = {m["n"]: f"{m['n']}, " + _rec_text(m) for m in st["recs"]
                    if m.get("n")}
            keep = sel_name or rec_sel.value
            rec_sel.set_options(opts, value=keep if keep in opts else
                                (next(iter(opts)) if opts else None))
            _show_rec()

        def _show_rec():
            m = _cur()
            rec_info.set_text(_rec_text(m) if m else
                              ("no recordings on this radio yet" if not st["recs"] else ""))
            _upd_peak()

        async def _refresh(sel_name=None, quiet=True):
            host = get_host()
            if host is None:
                st["recs"], st["serial"] = [], None
                _fill(); _upd_cap(); return
            try:
                ack = await host.cmd("list_recordings", {}, timeout=8.0)
            except Exception as e:
                if not quiet:
                    pstat.set_text(f"list failed: {e}")
                return
            r = ack.result or {}
            if ack.status != 0:
                if not quiet:
                    pstat.set_text(f"list failed: {r.get('err')}")
                return
            st.update(recs=r.get("recs") or [], max_s=float(r.get("max_s") or 5.0),
                      rate=r.get("rate"), serial=host.serial,
                      keep=r.get("keep") or 6, max_total=r.get("max_total") or 0,
                      total=r.get("total") or 0)
            _fill(sel_name); _upd_cap()

        async def _record():
            host = get_host()
            if host is None:
                ui.notify("no radio selected", type="warning"); return
            params = {"seconds": float(r_secs.value or 1.0),
                      "label": str(r_label.value or "")}
            if r_src.value == "band":
                b = get_band() if get_band else None
                if not b:
                    ui.notify(f"drag a box on {band_hint} around the signal first "
                              "(or pick Full band)", type="warning"); return
                params["band"] = [float(b[0]), float(b[1])]
            secs = min(params["seconds"], st["max_s"])
            pstat.set_text(f"recording {secs:g} s...")
            r_btn.props("loading")
            _rec_state.start(host.serial, params["label"], secs, "panel")   # REC badge
            try:
                ack = await host.cmd("record_iq", params,
                                     timeout=30.0 + 15.0 * secs)
            except Exception as e:
                pstat.set_text(f"record error: {e}"); return
            finally:
                r_btn.props(remove="loading")
                _rec_state.stop(host.serial)
            r = ack.result or {}
            if ack.status != 0:
                pstat.set_text(f"record failed: {r.get('err')}")
                ui.notify(f"record failed: {r.get('err')}", type="negative"); return
            m = r.get("rec") or {}
            msg = f"recorded {m.get('n')}: " + _rec_text(m)
            if r.get("removed"):
                msg += f", removed old: {', '.join(r['removed'])}"
            pstat.set_text(msg)
            for w in r.get("warnings") or []:
                ui.notify(w, type="warning", multi_line=True, close_button=True,
                          timeout=10000)
            ui.notify(f"recording saved on the radio ({_fmt_bytes(m.get('b'))})",
                      type="positive")
            await _refresh(sel_name=m.get("n"))
        r_btn.on_click(_record)

        async def _delete():
            host = get_host()
            m = _cur()
            if host is None or m is None:
                return
            ack = await host.cmd("delete_recording", {"name": m["n"]}, timeout=8.0)
            if ack.status != 0:
                pstat.set_text(f"delete failed: {(ack.result or {}).get('err')}"); return
            pstat.set_text(f"deleted {m['n']}")
            await _refresh()
        del_btn.on_click(_delete)
        ref_btn.on_click(lambda: _refresh(quiet=False))

        async def _replay():
            host = get_host()
            m = _cur()
            if host is None:
                ui.notify("no radio selected", type="warning"); return
            if m is None:
                ui.notify("pick a recording first", type="warning"); return
            pstat.set_text(f"loading {m['n']}...")
            try:
                ack = await host.cmd("replay_recording",
                                     {"name": m["n"], "gain_db": float(p_gain.value or 0),
                                      "level_mode": "relative"}, timeout=60.0)
            except Exception as e:
                pstat.set_text(f"load error: {e}"); return
            r = ack.result or {}
            if ack.status != 0:
                pstat.set_text(f"load failed: {r.get('err')}")
                ui.notify(f"replay load failed: {r.get('err')}", type="negative"); return
            for w in r.get("warnings") or []:
                ui.notify(w, type="warning", multi_line=True, close_button=True,
                          timeout=10000)
            once = p_mode.value == "SINGLE_SHOT"
            delay = max(0.0, min(60.0, float(p_delay.value or 0)))
            dur = float(r.get("seconds") or 0)
            cap = (f" (asked {r.get('gain_req_db'):+.1f} dB, capped at DAC full scale)"
                   if r.get("capped") else "")
            with ui.dialog() as dlg, ui.card().classes("max-w-[34rem]"):
                ui.label("Confirm REPLAY: this will RADIATE").classes(
                    "text-lg font-bold text-red-600")
                ui.label(f"Host {host.serial}: transmit recording '{m['n']}' "
                         f"({dur:.3f} s) on its configured TX port, "
                         f"{'ONCE' if once else 'LOOPED until STOP'}"
                         + (f", starting {delay:g} s after you confirm" if delay else "")
                         + ".")
                ui.label(f"Level: {r.get('gain_db'):+.1f} dB vs as captured{cap}, "
                         f"peak {r.get('peak_dbfs'):.1f} dBFS, centred "
                         f"{float(r.get('offset_hz') or 0)/1e3:+.1f} kHz from the TX LO.") \
                    .classes("text-sm")
                ui.label("Replaying a recorded signal is for cabled tests or authorized "
                         "use only.").classes("text-sm font-bold text-amber-700")
                ui.label("Confirm the chamber / load is connected and safe to key.") \
                    .classes("text-sm text-gray-600")
                with ui.row():
                    ui.button("Cancel", on_click=dlg.close).props("flat")
                    ui.button("KEY REPLAY NOW",
                              on_click=lambda: dlg.submit("go")).props("color=negative")
            if await dlg != "go":
                pstat.set_text("loaded, not keyed, press REPLAY to key"); return
            token = object()
            st["pending"] = token
            t_end = time.monotonic() + delay
            while delay and time.monotonic() < t_end:
                if st["pending"] is not token:
                    return                       # STOP pressed during the delay
                pstat.set_text(f"keying in {t_end - time.monotonic():.0f} s "
                               "(STOP cancels)")
                await asyncio.sleep(0.25)
            if st["pending"] is not token:
                return
            st["pending"] = None
            try:
                ack = await host.cmd("start_tx", {"mode": p_mode.value}, timeout=15.0)
            except Exception as e:
                pstat.set_text(f"start_tx error: {e}"); return
            if ack.status != 0:
                pstat.set_text(f"start_tx failed: {(ack.result or {}).get('err')}"); return
            if once:
                pstat.set_text(f"REPLAYING once ({dur:.3f} s)")
                ui.notify("replay keyed (once)", type="warning")
                st["pending"] = token
                await asyncio.sleep(dur + 0.5)
                if st["pending"] is token:
                    st["pending"] = None
                    pstat.set_text(f"replay of {m['n']} done")
            else:
                pstat.set_text(f"REPLAYING {m['n']} in a loop (STOP to end)")
                ui.notify("replay keyed (loop)", type="warning")
        p_btn.on_click(_replay)

        async def _stop():
            st["pending"] = None                 # cancels a pending delayed start
            host = get_host()
            if host is None:
                return
            try:
                await host.cmd("stop_tx", {}, timeout=10.0)
                pstat.set_text("replay stopped")
            except Exception as e:
                pstat.set_text(f"error: {e}")
        s_btn.on_click(_stop)

        rec_sel.on_value_change(lambda e: _show_rec())
        p_gain.on_value_change(lambda e: _upd_peak())
        r_src.on_value_change(lambda e: _upd_band())

        async def _tick():
            _upd_band()
            host = get_host()
            ser = host.serial if host is not None else None
            # re-read the list (and the cap) when the radio, or its rate, changes
            cfg = host_config(host) or {}
            rate_now = cfg.get("achieved_rate") if cfg.get("inited") else None
            if ser != st["serial"] or (ser and not st["rate"]) or \
                    (rate_now and st["rate"] and abs(rate_now - st["rate"]) > 1.0):
                await _refresh()
        ui.timer(1.0, _tick)
        _upd_band(); _upd_cap()


# The radio's service folder (/app_svc) also holds the code
# (dpmg/, deploy/, tests ...). The browser shows only the folders that hold IQ
# and scenario files, and never goes above the service folder.
BROWSE_ROOT_DIRS = ("data", "scenarios")


def browse_filter(path, root, dirs, files, root_dirs=BROWSE_ROOT_DIRS):
    """What the radio file browser shows for one listing: at the service folder
    only `root_dirs` and no files; below it every folder except hidden ones and
    __pycache__. Returns (dirs, files)."""
    dirs = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
    if root and path.rstrip("/") == root.rstrip("/"):
        return [d for d in dirs if d in root_dirs], []
    return dirs, list(files)


class RemoteFilePicker(ui.dialog):
    """Browse the radio's filesystem (over the list_dir command) with the same
    clean look as the engineering view's LocalFilePicker: a path header, Up, and
    a scrollable list of folders and files to click. `start` is a folder
    relative to the service folder (falls back to the service folder if the radio
    does not have it). `await` it to get the chosen absolute path (or None)."""

    def __init__(self, get_host, start="", only_ext=()):
        super().__init__()
        self._get_host = get_host
        self.start = (start or "").strip("/")
        self.path = self.start
        self.parent_path = self.start
        self.root = None                 # absolute service folder, learned from the first listing
        self.only_ext = tuple(e.lower() for e in (only_ext or ()))
        with self, ui.card().classes("w-[680px] max-w-full"):
            self.header = ui.label("...").classes("text-sm font-mono break-all")
            with ui.row().classes("w-full items-center gap-1"):
                ui.button(icon="arrow_upward", on_click=self._up).props("flat dense") \
                    .tooltip("Up one level")
                ui.button(icon="refresh", on_click=lambda: self._refresh()).props("flat dense")
                ui.space()
                ui.button("Cancel", on_click=lambda: self.submit(None)).props("flat")
            self.listing = ui.column().classes("w-full max-h-80 overflow-auto gap-0")
        ui.timer(0.05, self._refresh, once=True)   # initial listing (async)

    async def _refresh(self, path=None):
        host = self._get_host()
        if host is None:
            self.header.set_text("no radio selected"); return
        try:
            ack = await host.cmd("list_dir",
                                 {"path": path or self.path, "exts": list(self.only_ext)},
                                 timeout=10.0)
        except Exception as e:
            self.header.set_text(f"error: {e}"); return
        r = ack.result or {}
        if ack.status != 0:
            if self.root is None and self.start:
                # the start folder is missing on this radio: open the service folder
                self.start = ""
                await self._refresh(""); return
            self.header.set_text(f"error: {r.get('err')}"); return
        self.path = r.get("path") or self.path
        if self.root is None:
            rel = "/" + self.start if self.start else ""
            self.root = self.path[:-len(rel)] if rel and self.path.endswith(rel) else self.path
        self.parent_path = r.get("parent") or self.path
        dirs, files = browse_filter(self.path, self.root, r.get("dirs", []), r.get("files", []))
        self.header.set_text(self.path + ("  (truncated)" if r.get("truncated") else ""))
        self.listing.clear()
        with self.listing:
            for d in dirs:
                ui.button(d, icon="folder", on_click=lambda d=d: self._enter(d)) \
                    .props("flat no-caps align=left").classes("w-full justify-start")
            for nm, kb in files:
                ui.button(f"{nm}   ({kb} KB)", icon="description",
                          on_click=lambda nm=nm: self.submit(self.path.rstrip("/") + "/" + nm)) \
                    .props("flat no-caps align=left").classes("w-full justify-start")

    async def _enter(self, d):
        await self._refresh(self.path.rstrip("/") + "/" + d)

    async def _up(self):
        if self.root and self.path.rstrip("/") == self.root.rstrip("/"):
            return                       # already at the service folder
        await self._refresh(self.parent_path)


def build_radio_browser(get_host, exts=(), on_pick=None):
    """Radio-file selector: a 'Browse radio...' button that opens the RemoteFilePicker
    (same clean look as the engineering LocalFilePicker) + a label of the chosen
    file. Returns (container, picker) where picker.value is the selected absolute
    path, so callers keep their existing `.value` reads. `exts` may be a tuple or
    a zero-arg callable returning one (evaluated at browse time, so one browser
    can filter by the current source). `on_pick(path)` fires when a file is chosen."""
    st = {"value": None}

    class _Picker:
        @property
        def value(self):
            return st["value"]

    picker = _Picker()
    container = ui.column().classes("gap-1 w-full")
    with container:
        with ui.row().classes("items-center gap-2"):
            browse_btn = ui.button("Browse radio...", icon="folder_open").props("outline dense")
            sel_lbl = ui.label("no file selected").classes(
                "text-xs font-mono text-gray-600 self-center break-all")

    async def _browse():
        if get_host() is None:
            ui.notify("select a radio first", type="warning"); return
        only = exts() if callable(exts) else exts
        # scenario JSON lives in scenarios/json, IQ files under data/
        start = "scenarios/json" if tuple(only) == (".json",) else "data"
        chosen = await RemoteFilePicker(get_host, start=start, only_ext=only)
        if chosen:
            st["value"] = chosen
            sel_lbl.set_text(chosen.rsplit("/", 1)[-1])
            if on_pick is not None:
                on_pick(chosen)
    browse_btn.on_click(_browse)
    return container, picker


async def _iq_rate_warn(info):
    """Prompt for a raw IQ file whose native rate != the SDR rate: resample it
    to the SDR rate, play as-is (scaled), or cancel. Returns "resample",
    "asis", or "cancel"."""
    fr = info.get("rate"); rr = info.get("radio_rate")
    scale = (rr / fr) if (fr and rr) else None
    with ui.dialog() as dlg, ui.card().classes("w-[36rem] gap-1"):
        ui.label("IQ file rate doesn't match the SDR").classes(
            "text-base font-bold text-amber-700")
        ui.label(f"File is {fr/1e6:g} MSps ({info.get('source')}), radio is "
                 f"{rr/1e6:g} MSps. Do you want it resampled to {rr/1e6:g} MSps "
                 f"so the frequencies stay correct?" if scale else "rate mismatch").classes(
            "text-xs text-amber-800")
        ui.label(f"Play as-is instead and every frequency scales x{scale:.4g} "
                 f"(not resampled)." if scale else "").classes("text-xs text-gray-600")
        with ui.row().classes("justify-end w-full"):
            ui.button("Cancel", on_click=lambda: dlg.submit("cancel")).props("flat")
            ui.button("Play as-is (scaled)",
                      on_click=lambda: dlg.submit("asis")).props("color=warning")
            ui.button(f"Resample to {rr/1e6:g} MSps",
                      on_click=lambda: dlg.submit("resample")).props("color=primary")
    return await dlg or "cancel"


async def start_tx_checked(host, set_status, mode="CONTINUOUS"):
    """start_tx, but if the service rejects because the armed waveform's rate
    doesn't match the radio (a load-before-START slips past the LOAD-time check),
    offer to resample to the SDR rate, play as-is (scaled), or cancel. Returns
    (ack, note) where note is a short string describing what happened (scaled /
    resampled) for the PLAYING status, or None. Returns None if cancelled."""
    ack = await host.cmd("start_tx", {"mode": mode}, timeout=15.0)
    r = ack.result or {}
    if ack.status != 0 and r.get("rate_mismatch"):
        wf, rr, sc = r.get("wf_rate"), r.get("radio_rate"), r.get("scale")
        with ui.dialog() as dlg, ui.card().classes("w-[36rem] gap-1"):
            ui.label("Waveform rate does not match the SDR rate").classes(
                "text-base font-bold text-amber-700")
            ui.label(f"The loaded waveform is {wf/1e6:g} MSps but the radio is "
                     f"{rr/1e6:g} MSps. Do you want it resampled to "
                     f"{rr/1e6:g} MSps so the frequencies stay correct?").classes(
                "text-xs text-amber-800")
            ui.label(f"Play as-is instead and every frequency scales x{sc:.4g} "
                     f"(the samples just clock out faster, not resampled).").classes(
                "text-xs text-gray-600")
            with ui.row().classes("justify-end w-full"):
                ui.button("Cancel", on_click=lambda: dlg.submit("cancel")).props("flat")
                ui.button("Play as-is (scaled)",
                          on_click=lambda: dlg.submit("asis")).props("color=warning")
                ui.button(f"Resample to {rr/1e6:g} MSps",
                          on_click=lambda: dlg.submit("resample")).props("color=primary")
        choice = await dlg
        if choice == "resample":
            set_status(f"resampling {wf/1e6:g} to {rr/1e6:g} MSps...")
            rs = await host.cmd("resample_waveform", {"load_tx": True}, timeout=180.0)
            if rs.status != 0:
                set_status(f"resample failed: {(rs.result or {}).get('err')}")
                return None
            ui.notify(f"resampled to {rr/1e6:g} MSps", type="positive")
            ack = await host.cmd("start_tx", {"mode": mode}, timeout=15.0)
            return ack, f"RESAMPLED to {rr/1e6:g} MSps (correct frequencies)"
        if choice != "asis":
            set_status("cancelled: rate mismatch"); return None
        ack = await host.cmd("start_tx",
                              {"mode": mode, "allow_rate_mismatch": True}, timeout=15.0)
        return ack, f"SCALED x{sc:.4g}: frequencies shifted (not resampled)"
    return ack, None


async def load_scenario_checked(host, base_args, set_status, on_retune=None):
    """Preflight a scenario build against the radio's current rate and tuning:
    warn on a rate mismatch (frequencies would scale) or out-of-band signals and
    let the operator rebuild at the radio's rate or play as-is, then offer to
    retune the SDR to the scenario's own center so the decode plan + spectrum
    line up. Returns the final gen_scenario ack, or None if the operator
    cancelled."""
    pre = await host.cmd("gen_scenario", dict(base_args, check_only=True), timeout=20.0)
    if pre.status != 0:
        set_status(f"check failed: {(pre.result or {}).get('err')}")
        return pre
    v = (pre.result or {}).get("validation") or {}
    errs, warns = v.get("errors") or [], v.get("warnings") or []
    can_rebuild = bool((pre.result or {}).get("can_rebuild"))
    rr = v.get("radio_rate")
    match_rate = False
    if errs or warns:
        with ui.dialog() as dlg, ui.card().classes("w-[34rem] gap-1"):
            if errs:
                ui.label("Scenario doesn't fit the SDR's current rate").classes(
                    "text-base font-bold text-red-600")
                for e in errs:
                    ui.label("- " + e).classes("text-xs text-red-700")
            if warns:
                ui.label("Sample-rate mismatch").classes(
                    "text-sm font-bold text-amber-700")
                for w in warns:
                    ui.label("- " + w).classes("text-xs text-amber-800")
            if can_rebuild and rr:
                ui.label(f"Rebuild makes the scenario native to {rr/1e6:g} MSps "
                         f"(offsets/spacings land where the JSON intends).").classes(
                    "text-xs text-gray-600")
            with ui.row().classes("justify-end w-full"):
                ui.button("Cancel",
                          on_click=lambda: dlg.submit("cancel")).props("flat")
                if not errs:   # only offer as-is when signals are in-band
                    ui.button("Play as-is (scaled)",
                              on_click=lambda: dlg.submit("asis")).props("color=warning")
                if can_rebuild:
                    ui.button("Rebuild at radio rate",
                              on_click=lambda: dlg.submit("rebuild")).props("color=primary")
        choice = await dlg
        if choice in (None, "cancel"):
            set_status("cancelled: nothing loaded"); return None
        match_rate = (choice == "rebuild")

    # center-frequency preflight: the scenario's intended RF center (baked into
    # its decode plan) vs the radio's current tuning. If they differ, offer to
    # retune so the plan + spectrum land on the signal (mirrors IQ playback).
    sc = (pre.result or {}).get("scenario_center_hz")
    rc = (pre.result or {}).get("radio_center_hz")
    if sc and rc and abs(sc - rc) >= 1e3:
        with ui.dialog() as cdlg, ui.card().classes("w-[36rem] gap-1"):
            ui.label("Scenario center does not match the SDR tuning").classes(
                "text-base font-bold text-amber-700")
            ui.label(f"This scenario is built for {sc/1e6:.4f} MHz but the SDR is "
                     f"tuned to {rc/1e6:.4f} MHz. Retune the SDR to {sc/1e6:.4f} MHz "
                     f"so the decode plan and spectrum line up with the signal?").classes(
                "text-xs text-amber-800")
            ui.label("Keep current tuning to play the scenario at the SDR's center "
                     "instead. Decoding follows the radio either way.").classes(
                "text-xs text-gray-600")
            with ui.row().classes("justify-end w-full"):
                ui.button("Cancel",
                          on_click=lambda: cdlg.submit("cancel")).props("flat")
                ui.button("Keep current tuning",
                          on_click=lambda: cdlg.submit("keep")).props("color=grey")
                ui.button(f"Retune to {sc/1e6:g} MHz",
                          on_click=lambda: cdlg.submit("retune")).props("color=primary")
        cc = await cdlg
        if cc in (None, "cancel"):
            set_status("cancelled: nothing loaded"); return None
        if cc == "retune":
            rt = await host.cmd("set_center", {"hz": float(sc)}, timeout=10.0)
            if rt.status == 0:
                if on_retune:
                    on_retune(float(sc))
                set_status(f"retuned SDR to {sc/1e6:g} MHz")
                ui.notify(f"SDR retuned to {sc/1e6:g} MHz", type="positive")
            else:
                set_status(f"retune failed: {(rt.result or {}).get('err')}")
                ui.notify("retune failed, loading at current center", type="warning")

    args = dict(base_args)
    if match_rate:
        args["match_radio_rate"] = True
    return await host.cmd("gen_scenario", args, timeout=180.0)


def _peak_input():
    """Peak field for panels that build a scenario JSON: the built mix is
    scaled so its peak is this fraction of full scale (gen_scenario peak_fs)."""
    return ui.number("Peak (FS)", value=0.45, min=0.05, max=1.0, step=0.05,
                     format="%.2f").props("dense outlined").classes("w-28").tooltip(
        "peak of the built waveform as a fraction of DAC full scale; the signals keep "
        "their levels relative to each other. Overrides a peak_fs in the JSON")


def _with_peak(args: dict, peak_in) -> dict:
    """args plus peak_fs from a _peak_input field, when it holds a value."""
    v = getattr(peak_in, "value", None)
    return dict(args, peak_fs=float(v)) if v not in (None, "") else dict(args)


def build_iq_playback_panel(get_host, get_txport=None, on_retune=None, on_reinit=None):
    """Operator IQ playback: load a waveform onto the selected radio and key it
    (radiate-gated). Source is one of: a scenario JSON uploaded from the ground,
    a scenario JSON already on the radio, or a pre-stored .iq.bin on the radio."""
    with ui.card().classes("w-full"):
        ui.label("IQ playback").classes("text-base font-bold")
        _tx_where(get_txport)
        with ui.row().classes("items-center gap-2 flex-wrap"):
            src = ui.select({
                "upload_json": "Scenario JSON: upload from ground",
                "upload_iq": "IQ file: upload from ground",
                "radio_json": "Scenario JSON: on the radio",
                "radio_iq": "IQ file: on the radio",
            }, value="radio_iq", label="Source").props("dense outlined").classes("w-72")
            peak_in = _peak_input()
        state = {"spec": None, "iq_bytes": None, "iq_name": None}

        # browse the radio filesystem; filter the picker by the current source
        browser_box, radio_pick = build_radio_browser(
            get_host,
            exts=lambda: ((".json",) if src.value == "radio_json"
                          else (".iq.bin", ".c64", ".bin", ".wav",
                                ".cdif", ".tmp", ".blue", ".prm")))
        up_row = ui.row().classes("items-center gap-2")
        with up_row:
            up = ui.upload(auto_upload=True).props("accept=.json").classes("w-72")
        up_iq_row = ui.row().classes("items-center gap-2")
        with up_iq_row:
            up_iq = ui.upload(auto_upload=True, max_file_size=512_000_000).props(
                "accept=.iq.bin,.c64,.bin,.wav,.cdif,.tmp,.blue,.prm").classes("w-80")
        pstat = ui.label("pick a source, then PLAY").classes(
            "font-mono text-xs text-gray-600")
        with ui.row().classes("items-center gap-2"):
            play_btn = ui.button("PLAY").props("color=negative")
            pstop_btn = ui.button("STOP").props("color=grey")

        # modal "working" overlay for the slow steps (re-init ~30 s, resample,
        # large uploads) so the operator sees progress before the confirm box
        with ui.dialog().props("persistent") as _busy_dlg, \
                ui.card().classes("items-center gap-3 p-5 min-w-[20rem]"):
            ui.spinner(size="lg", color="primary")
            _busy_lbl = ui.label("working...").classes(
                "text-sm font-mono text-center")
            ui.label("please wait, do not close").classes("text-xs text-gray-500")

        def _busy(msg):
            _busy_lbl.set_text(msg); pstat.set_text(msg); _busy_dlg.open()

        def _busy_done():
            _busy_dlg.close()

        def _sync_src():
            s = src.value
            up_row.set_visibility(s == "upload_json")
            up_iq_row.set_visibility(s == "upload_iq")
            peak_in.set_visibility(s in ("upload_json", "radio_json"))
            browser_box.set_visibility(s in ("radio_json", "radio_iq"))
        src.on_value_change(lambda e: _sync_src())

        async def _on_upload(e):
            try:
                state["spec"] = json.loads(await e.file.text())
                pstat.set_text(f"uploaded '{e.file.name}', press PLAY to build and key it")
            except Exception as ex:
                pstat.set_text(f"bad JSON: {ex}")
        up.on_upload(_on_upload)

        async def _on_iq_upload(e):
            state["iq_bytes"] = await e.file.read()
            state["iq_name"] = e.file.name
            pstat.set_text(f"staged '{e.file.name}' ({len(state['iq_bytes'])/1e6:.1f} MB), "
                           f"press PLAY to send it to the radio and key")
        up_iq.on_upload(_on_iq_upload)

        async def _maybe_retune(host, r):
            """Header formats (.cdif) carry their capture center frequency. If the
            SDR isn't tuned there, offer to retune so the file plays at its
            original frequency. Returns True to proceed, False to cancel."""
            fc, rc = r.get("file_center_hz"), r.get("radio_center_hz")
            if not fc or not rc or abs(fc - rc) < 1e3:
                return True
            with ui.dialog() as dlg, ui.card().classes("w-[36rem] gap-1"):
                ui.label("Recording center does not match the SDR tuning").classes(
                    "text-base font-bold text-amber-700")
                ui.label(f"This file was captured at {fc/1e6:.4f} MHz but the SDR is "
                         f"tuned to {rc/1e6:.4f} MHz. Retune the SDR to "
                         f"{fc/1e6:.4f} MHz so it plays at its original frequency?").classes(
                    "text-xs text-amber-800")
                ui.label("Keep current to play the recording's baseband content at the "
                         "SDR's center instead.").classes("text-xs text-gray-600")
                with ui.row().classes("justify-end w-full"):
                    ui.button("Cancel", on_click=lambda: dlg.submit("cancel")).props("flat")
                    ui.button("Keep current tuning",
                              on_click=lambda: dlg.submit("keep")).props("color=grey")
                    ui.button(f"Retune to {fc/1e6:g} MHz",
                              on_click=lambda: dlg.submit("retune")).props("color=primary")
            choice = await dlg
            if choice == "cancel":
                pstat.set_text("cancelled"); return False
            if choice == "retune":
                rt = await host.cmd("set_center", {"hz": float(fc)}, timeout=10.0)
                if rt.status == 0:
                    if on_retune:
                        on_retune(float(fc))
                    pstat.set_text(f"retuned SDR to {fc/1e6:g} MHz")
                    ui.notify(f"SDR retuned to {fc/1e6:g} MHz", type="positive")
                else:
                    pstat.set_text(f"retune failed: {(rt.result or {}).get('err')}")
                    ui.notify("retune failed, playing at current center", type="warning")
            return True

        async def _maybe_resample(host, r):
            """For a file whose rate != the SDR rate, let the operator pick the
            playback rate. Recommends the smallest valid rate that still fits the
            file (least storage); they can keep the current SDR rate or pick any.
            Changing the rate re-inits the SDR (refreshing the top controls), then
            the file is resampled to it. Returns True to proceed."""
            fr, rr, n = r.get("file_rate_hz"), r.get("radio_rate_hz"), r.get("n_samples") or 0
            if not fr or not rr or abs(fr - rr) < max(1.0, fr * 1e-3):
                return True                      # rate already matches (or unknown)
            is_x40 = getattr(host, "host_type", None) == int(HostType.X40_TX)
            allrates = X40_SAMPLE_RATES if is_x40 else G40_SAMPLE_RATES
            valid = sorted(hz for hz in allrates if hz >= fr - 1) or sorted(allrates)
            rec = valid[0]                       # smallest rate that fits = least storage
            native_ok = abs(rec - fr) <= max(1.0, fr * 1e-3)  # file's own rate achievable
            opts = {}
            for hz in sorted(set(valid) | {int(rr)}):
                mb = n * (hz / fr) * 8 / 1e6
                if abs(hz - fr) <= max(1.0, fr * 1e-3):
                    tag = "  (native rate: retune, no resample)"
                elif hz == int(rr):
                    tag = "  (current SDR rate: resamples the file)"
                else:
                    tag = ""
                opts[hz] = f"{hz/1e6:g} MSps   (~{mb:.0f} MB){tag}"
            with ui.dialog() as dlg, ui.card().classes("w-[40rem] gap-1"):
                ui.label("Pick the playback sample rate").classes(
                    "text-base font-bold text-amber-700")
                ui.label(f"This file is {fr/1e6:g} MSps but the SDR is at {rr/1e6:g} MSps. "
                         f"Pick the file's native rate to retune the SDR and play it as-is "
                         f"(full bandwidth, no resampling); pick a lower rate to resample "
                         f"the file down (loses bandwidth). Any choice other than the "
                         f"current SDR rate re-tunes the radio.").classes("text-xs text-amber-800")
                rate_sel = ui.select(opts, value=rec, label="Play at").props(
                    "dense outlined").classes("w-full")
                note = ui.label("").classes("text-xs text-gray-600")

                def _upd():
                    hz = int(rate_sel.value); mb = n * (hz / fr) * 8 / 1e6
                    if abs(hz - fr) <= max(1.0, fr * 1e-3):
                        tail = (f"re-inits the SDR to {hz/1e6:g} MSps and plays the file "
                                f"as-is, no resampling")
                    elif hz == int(rr):
                        tail = (f"keeps the SDR at {hz/1e6:g} MSps and resamples the file "
                                f"{fr/1e6:g} to {hz/1e6:g} MSps")
                    else:
                        tail = (f"re-inits the SDR to {hz/1e6:g} MSps and resamples the file "
                                f"{fr/1e6:g} to {hz/1e6:g} MSps")
                    note.set_text(f"~{mb:.0f} MB on the radio, {tail}")
                rate_sel.on_value_change(lambda e: _upd()); _upd()
                with ui.row().classes("justify-end w-full"):
                    ui.button("Cancel", on_click=lambda: dlg.submit(None)).props("flat")
                    ui.button("Apply", on_click=lambda: dlg.submit(int(rate_sel.value))) \
                        .props("color=primary")
            chosen = await dlg
            if chosen is None:
                pstat.set_text("cancelled: rate not set"); return False
            as_is = abs(chosen - fr) <= max(1.0, fr * 1e-3)   # native rate: no resample
            try:
                if chosen != int(rr):
                    if on_reinit is None:
                        pstat.set_text("can't change the SDR rate from here"); return False
                    _busy(f"Re-initializing SDR at {chosen/1e6:g} MSps... (~30 s)")
                    if not await on_reinit(chosen):
                        pstat.set_text("SDR re-init failed"); return False
                _busy(f"Loading at native {chosen/1e6:g} MSps..." if as_is
                      else f"Resampling {fr/1e6:g} to {chosen/1e6:g} MSps... (large files take a bit)")
                rs = await host.cmd("resample_waveform",
                                     {"fs_out": float(chosen), "load_tx": True}, timeout=240.0)
            finally:
                _busy_done()
            if rs.status != 0:
                pstat.set_text(f"resample failed: {(rs.result or {}).get('err')}"); return False
            if as_is:
                ui.notify(f"retuned to {chosen/1e6:g} MSps, playing the file as-is", type="positive")
                pstat.set_text(f"SDR @ {chosen/1e6:g} MSps, native playback, keying...")
            else:
                ui.notify(f"resampled to {chosen/1e6:g} MSps", type="positive")
                pstat.set_text(f"RESAMPLED {fr/1e6:g} to {chosen/1e6:g} MSps, keying...")
            return True

        async def _scenario_rate_change(host, err, minr, rr, native, fits):
            """Scenario sample-rate prompt. Fires when the scenario won't fit the
            SDR's current rate (fits=False) or when its native rate differs from
            the SDR's. Offers rates that fit, recommending the scenario's own
            native rate; the current SDR rate is offered too when it fits. Returns
            the chosen rate (== rr means keep current), or None to cancel."""
            is_x40 = getattr(host, "host_type", None) == int(HostType.X40_TX)
            allrates = X40_SAMPLE_RATES if is_x40 else G40_SAMPLE_RATES
            valid = sorted(hz for hz in allrates if hz >= minr - 1)
            if not valid:
                with ui.dialog() as dlg, ui.card().classes("w-[38rem] gap-1"):
                    ui.label("Scenario is too wide for this radio").classes(
                        "text-base font-bold text-red-600")
                    ui.label(f"It needs >= {minr/1e6:.3g} MSps, beyond the radio's max. "
                             f"Narrow the scenario's offsets.").classes("text-xs text-red-700")
                    ui.button("OK", on_click=lambda: dlg.submit(None)).props("flat")
                await dlg; return None
            # recommend the scenario's own native rate when it's a valid, fitting rate
            rec = (int(native) if (native and native >= minr - 1
                                   and int(native) in allrates) else valid[0])
            keep_ok = rr and rr >= minr - 1          # current rate fits: offer "keep"
            opts = {}
            for hz in sorted(set(valid) | ({int(rr)} if keep_ok else set())):
                lab = f"{hz/1e6:g} MSps"
                if native and hz == int(native):
                    lab += "  (scenario's native rate)"
                if rr and hz == int(rr):
                    lab += "  (current SDR rate)"
                opts[hz] = lab
            with ui.dialog() as dlg, ui.card().classes("w-[42rem] gap-1"):
                ui.label("Scenario sample rate" if fits
                         else "Scenario is too wide for the SDR rate").classes(
                    "text-base font-bold text-amber-700")
                if not fits and err:
                    ui.label(str(err)).classes("text-xs text-amber-800")
                ui.label(f"Designed for {native/1e6:g} MSps; the SDR is at {rr/1e6:g} MSps. "
                         f"Pick the rate to build and play at. Picking anything other than "
                         f"the current SDR rate re-tunes the radio.").classes(
                    "text-xs text-gray-600")
                rsel = ui.select(opts, value=rec, label="Build + play at").props(
                    "dense outlined").classes("w-full")
                with ui.row().classes("justify-end w-full"):
                    ui.button("Cancel", on_click=lambda: dlg.submit(None)).props("flat")
                    ui.button("Apply", on_click=lambda: dlg.submit(int(rsel.value))) \
                        .props("color=primary")
            return await dlg

        async def _arm(host):
            """Build/arm the selected source on the radio (the Load step is
            part of PLAY). Scenarios are always built at the SDR's rate so
            they play (and decode) at the correct frequencies: no scaling. Raw IQ
            is loaded as-is; a rate mismatch is handled at key time by the start_tx
            guard (resample / play-as-is). Returns True when armed and ready."""
            s = src.value
            if s == "radio_iq":
                if not radio_pick.value:
                    pstat.set_text("pick an IQ file on the radio"); return False
                ack = await host.cmd("set_tx_waveform",
                                     {"kind": "file", "path": radio_pick.value}, timeout=20.0)
                if ack.status != 0:
                    pstat.set_text(f"load failed: {(ack.result or {}).get('err')}"); return False
                r = ack.result or {}
                return (await _maybe_retune(host, r)) and (await _maybe_resample(host, r))
            if s == "upload_iq":
                if not state["iq_bytes"]:
                    pstat.set_text("upload an IQ file first"); return False
                mb = len(state['iq_bytes']) / 1e6
                _busy(f"Sending {mb:.1f} MB to {host.serial}... (large files take a bit)")
                try:
                    # names > 32 bytes are stored shortened: use the
                    # name send_file reports, not the browser's
                    # a BLUE recording under another ending goes up as .cdif, so the
                    # radio reads its header and not the header as samples
                    from dpmg.dsp import cdif_reader as _cdif
                    _sent_as = _cdif.blue_name(state["iq_name"], state["iq_bytes"][:12])
                    if _sent_as != state["iq_name"]:
                        pstat.set_text(f"'{state['iq_name']}' has a BLUE header: sending it as '{_sent_as}'")
                        state["iq_name"] = _sent_as
                    stored = await host.send_file(state["iq_name"], state["iq_bytes"],
                                                  timeout=max(60.0, mb * 8.0))   # ~scale with size
                    ack = await host.cmd("set_tx_waveform",
                                         {"kind": "file", "inbox": stored or state["iq_name"]},
                                         timeout=30.0)
                except Exception as ex:
                    pstat.set_text(f"upload failed: {ex}"); return False
                finally:
                    _busy_done()
                if ack.status != 0:
                    pstat.set_text(f"load failed: {(ack.result or {}).get('err')}"); return False
                r = ack.result or {}
                return (await _maybe_retune(host, r)) and (await _maybe_resample(host, r))
            if s == "upload_json":
                if state["spec"] is None:
                    pstat.set_text("upload a scenario .json first"); return False
                base = {"spec": state["spec"]}
            else:  # radio_json
                if not radio_pick.value:
                    pstat.set_text("pick a scenario JSON on the radio"); return False
                base = {"spec_path": radio_pick.value}
            base = _with_peak(base, peak_in)
            async def _build():
                pstat.set_text("building scenario at the SDR rate...")
                a = await host.cmd("gen_scenario",
                                   dict(base, load_tx=True, match_radio_rate=True),
                                   timeout=180.0)   # X24 at 30.72 MSps builds in ~80 s
                return a, (a.result or {})
            # preflight: does the scenario fit the SDR rate, and is the SDR at the
            # rate the scenario was designed for? Prompt if it won't fit or the
            # scenario's native rate differs from the SDR's.
            chk = await host.cmd("gen_scenario",
                                  dict(base, check_only=True, match_radio_rate=True), timeout=20.0)
            cr = chk.result or {}; cv = cr.get("validation") or {}
            native = cr.get("spec_rate_hz")
            rr = cv.get("radio_rate") or cr.get("radio_rate")
            minr = cv.get("min_rate_hz") or 0
            fits = chk.status == 0 and not cv.get("errors")
            differs = native and rr and abs(native - rr) > native * 1e-3
            if (not fits or differs) and on_reinit and rr:
                chosen = await _scenario_rate_change(
                    host, (cv.get("errors") or [None])[0], minr, rr, native, fits)
                if chosen is None:
                    pstat.set_text("cancelled"); return False
                if chosen != int(rr):
                    _busy(f"Re-initializing SDR at {chosen/1e6:g} MSps... (~30 s)")
                    try:
                        ok = await on_reinit(chosen)
                    finally:
                        _busy_done()
                    if not ok:
                        pstat.set_text("SDR re-init failed"); return False
            # The scenario names an RF center; if the radio is tuned
            # elsewhere, offer to retune. Either way the decode plan follows this
            # radio's TX, so it decodes where the signal is actually keyed.
            sc = cr.get("scenario_center_hz")
            rcen = cr.get("radio_center_hz")
            if sc and rcen and abs(float(sc) - float(rcen)) >= 1e3:
                with ui.dialog() as cdlg, ui.card().classes("w-[36rem] gap-1"):
                    ui.label("Scenario center differs from the radio's tuning").classes(
                        "text-base font-bold text-amber-700")
                    ui.label(f"This scenario is written for {float(sc)/1e6:.4f} MHz; the radio "
                             f"is tuned to {float(rcen)/1e6:.4f} MHz.").classes("text-xs")
                    ui.label("Retune: the radio moves to the scenario's center. Play here: it "
                             "keys at the current center. Decoding follows the radio either "
                             "way.").classes("text-xs text-gray-600")
                    with ui.row().classes("justify-end w-full"):
                        ui.button("Cancel", on_click=lambda: cdlg.submit("cancel")).props("flat")
                        ui.button(f"Play here ({float(rcen)/1e6:g} MHz)",
                                  on_click=lambda: cdlg.submit("keep")).props("color=grey")
                        ui.button(f"Retune to {float(sc)/1e6:g} MHz",
                                  on_click=lambda: cdlg.submit("retune")).props("color=primary")
                cc = await cdlg
                if cc in (None, "cancel"):
                    pstat.set_text("cancelled"); return False
                if cc == "retune":
                    rt = await host.cmd("set_center", {"hz": float(sc)}, timeout=10.0)
                    if rt.status == 0:
                        if on_retune:
                            on_retune(float(sc))
                        ui.notify(f"radio retuned to {float(sc)/1e6:g} MHz", type="positive")
                    else:
                        ui.notify(f"retune failed: {(rt.result or {}).get('err')}; "
                                  "playing at the current center", type="warning")
            ack, r = await _build()
            if ack.status != 0:
                pstat.set_text(f"build failed: {r.get('err')}")
                ui.notify(f"build failed: {r.get('err')}", type="negative"); return False
            if r.get("plan_path"):           # install the decode plan for the Decodes table
                try:
                    await host.cmd("set_plan", {"plan_path": r["plan_path"], "anchor": "tx"},
                                   timeout=10.0)     # Bands follow this radio's TX
                except Exception:
                    pass
            sr = r.get("sample_rate") or 0
            pstat.set_text(f"built at {sr/1e6:g} MSps (matched to SDR), keying...")
            return True

        async def _play():
            host = get_host()
            if host is None:
                ui.notify("no radio selected", type="warning"); return
            try:
                if not await _arm(host):      # build/arm first (no separate Load step)
                    return
            except Exception as e:
                pstat.set_text(f"error: {e}"); return
            with ui.dialog() as dlg, ui.card():
                ui.label("Confirm PLAY: this will RADIATE").classes(
                    "text-lg font-bold text-red-600")
                ui.label(f"Host {host.serial}: transmit the waveform on its "
                         f"configured TX port, CONTINUOUS.")
                ui.label("Confirm the chamber / load is connected and safe to key.") \
                    .classes("text-sm text-gray-600")
                with ui.row():
                    ui.button("Cancel", on_click=dlg.close).props("flat")
                    ui.button("KEY TX NOW",
                              on_click=lambda: dlg.submit("go")).props("color=negative")
            if await dlg != "go":
                pstat.set_text("ready, press PLAY to key"); return
            try:
                res = await start_tx_checked(host, pstat.set_text)
                if res is None:
                    return
                ack, note = res
                if ack.status == 0:
                    pstat.set_text(f"PLAYING: {note} (STOP to end)" if note
                                   else "PLAYING: waveform radiating (STOP to end)")
                    ui.notify(note or "playback active", type="warning")
                else:
                    pstat.set_text(f"start_tx failed: {(ack.result or {}).get('err')}")
            except Exception as e:
                pstat.set_text(f"error: {e}")
        play_btn.on_click(_play)

        async def _stop():
            host = get_host()
            if host is None:
                return
            await host.cmd("stop_tx", {}, timeout=10.0)
            pstat.set_text("stopped")
        pstop_btn.on_click(_stop)
        _sync_src()


# Bundled custom-waveform examples: each is a multi-emitter scenario mixing
# modulations and convolutional FEC rates, placed at several frequencies at
# once. Sized for a specific G40 sample rate (offsets fit that rate's usable
# span): init the radio at the labelled rate before playing. Built at the
# radio's current rate by gen_scenario (no sample_rate_hz in the spec).
_CENTER = 425_025_000
CUSTOM_EXAMPLES = {
    "coded_fsk_mix @1.92": {
        "name": "coded_fsk_mix", "center_freq_hz": _CENTER, "loop_s": 0.2,
        "signals": [
            {"modulation": "FSK", "symbol_rate": 4800, "tone_spacing_hz": 8000,
             "offset_hz": -400_000, "message": "FSK4800 CONV12", "amplitude": 0.4,
             "fec": {"type": "conv", "rate": "1/2"}},
            {"modulation": "4FSK", "symbol_rate": 4800, "tone_spacing_hz": 1296,
             "offset_hz": 0, "message": "DMR CONV34", "amplitude": 0.4,
             "dibit_mapping": "binary", "fec": {"type": "conv", "rate": "3/4"}},
            {"modulation": "FSK", "symbol_rate": 9600, "tone_spacing_hz": 12000,
             "offset_hz": 400_000, "message": "FSK9600 CONV78", "amplitude": 0.4,
             "fec": {"type": "conv", "rate": "7/8"}},
        ]},
    "multimod @7.68": {
        "name": "multimod", "center_freq_hz": _CENTER, "loop_s": 0.2,
        "signals": [
            {"modulation": "FSK", "symbol_rate": 4800, "tone_spacing_hz": 8000,
             "offset_hz": -1_500_000, "message": "FSK CONV12", "amplitude": 0.35,
             "fec": {"type": "conv", "rate": "1/2"}},
            {"modulation": "4FSK", "symbol_rate": 4800, "tone_spacing_hz": 1296,
             "offset_hz": -500_000, "message": "4FSK DMR", "amplitude": 0.35,
             "dibit_mapping": "binary"},
            {"modulation": "QPSK", "symbol_rate": 9600, "offset_hz": 500_000,
             "message": "QPSK UNCODED", "amplitude": 0.35},
            {"modulation": "FSK", "symbol_rate": 9600, "tone_spacing_hz": 12000,
             "offset_hz": 1_500_000, "message": "FSK CONV23", "amplitude": 0.35,
             "fec": {"type": "conv", "rate": "2/3"}},
        ]},
    "dense_coded @30.72": {
        "name": "dense_coded", "center_freq_hz": _CENTER, "loop_s": 0.2,
        "signals": [
            {"modulation": "FSK", "symbol_rate": 4800, "tone_spacing_hz": 8000,
             "offset_hz": o, "message": m, "amplitude": 0.2,
             **({"fec": {"type": "conv", "rate": r}} if r else {})}
            for (o, m, r) in [
                (-7_000_000, "D1 FSK CONV12", "1/2"),
                (-4_000_000, "D2 FSK CONV23", "2/3"),
                (-1_500_000, "D3 FSK CONV34", "3/4"),
                (1_500_000, "D4 FSK CONV78", "7/8"),
                (4_000_000, "D5 FSK UNCODED", None),
                (7_000_000, "D6 FSK CONV12", "1/2"),
            ]],
    },
}


def build_custom_waveform_panel(get_host, get_txport=None, on_retune=None):
    """Custom-waveform playback: pick a bundled example scenario (mixed
    modulations + FEC, several signals at once), or load a scenario .json from
    the ground or the radio, then key it over the loopback. Installs the
    scenario's decode plan so the Decodes table recovers it. Radiate-gated."""
    with ui.card().classes("w-full"):
        ui.label("Custom waveforms").classes("text-base font-bold")
        _tx_where(get_txport)
        with ui.row().classes("items-center gap-2 flex-wrap"):
            src = ui.select({
                "example": "Example (bundled)",
                "upload_json": "Scenario JSON: upload from ground",
                "radio_json": "Scenario JSON: on the radio",
            }, value="example", label="Source").props("dense outlined").classes("w-64")
            ex_pick = ui.select(list(CUSTOM_EXAMPLES.keys()),
                                value=list(CUSTOM_EXAMPLES.keys())[0],
                                label="Example").props("dense outlined").classes("w-56")
            peak_in = _peak_input()
        # browse the radio filesystem for the radio-JSON source
        browser_box, radio_pick = build_radio_browser(get_host, exts=(".json",))
        up_row = ui.row().classes("items-center gap-2")
        with up_row:
            up = ui.upload(auto_upload=True).props("accept=.json").classes("w-72")
        cstat = ui.label("pick a source, Load, then PLAY").classes(
            "font-mono text-xs text-gray-600")
        with ui.row().classes("items-center gap-2"):
            load_btn = ui.button("Load").props("color=primary")
            play_btn = ui.button("PLAY").props("color=negative")
            stop_btn = ui.button("STOP").props("color=grey")

        state = {"spec": None}

        def _sync():
            s = src.value
            ex_pick.set_visibility(s == "example")
            up_row.set_visibility(s == "upload_json")
            browser_box.set_visibility(s == "radio_json")
        src.on_value_change(lambda e: _sync())

        async def _on_upload(e):
            try:
                state["spec"] = json.loads(await e.file.text())
                cstat.set_text(f"uploaded '{e.file.name}', press Load to build it")
            except Exception as ex:
                cstat.set_text(f"bad JSON: {ex}")
        up.on_upload(_on_upload)

        async def _load():
            host = get_host()
            if host is None:
                ui.notify("no radio selected", type="warning"); return
            s = src.value
            try:
                if s == "example":
                    base = {"spec": CUSTOM_EXAMPLES.get(ex_pick.value), "load_tx": True}
                elif s == "upload_json":
                    if state["spec"] is None:
                        cstat.set_text("upload a scenario .json first"); return
                    base = {"spec": state["spec"], "load_tx": True}
                else:
                    if not radio_pick.value:
                        cstat.set_text("pick a scenario JSON on the radio"); return
                    base = {"spec_path": radio_pick.value, "load_tx": True}
                base = _with_peak(base, peak_in)
                ack = await load_scenario_checked(host, base, cstat.set_text,
                                                  on_retune=on_retune)
                if ack is None:
                    return
                r = ack.result or {}
                if ack.status != 0:
                    cstat.set_text(f"failed: {r.get('err')}")
                    ui.notify(f"load failed: {r.get('err')}", type="negative"); return
                plan_note = ""
                if r.get("plan_path"):
                    try:
                        await host.cmd("set_plan", {"plan_path": r["plan_path"], "anchor": "tx"}, timeout=10.0)
                        plan_note = " + decode plan set"
                    except Exception:
                        plan_note = " (plan set failed)"
                if r.get("loaded_tx") and not r.get("armed", True):
                    cstat.set_text(f"built {r.get('iq_path') or ''}{plan_note}, "
                                   f"START the radio, then PLAY (auto-arms)")
                    ui.notify("built, START the radio, then PLAY", type="info")
                else:
                    cstat.set_text(f"loaded {r.get('iq_path') or ''}{plan_note}, ready to PLAY")
                    ui.notify("custom waveform loaded", type="positive")
            except Exception as e:
                cstat.set_text(f"error: {e}")
        load_btn.on_click(_load)

        async def _play():
            host = get_host()
            if host is None:
                ui.notify("no radio selected", type="warning"); return
            with ui.dialog() as dlg, ui.card():
                ui.label("Confirm PLAY: this will RADIATE").classes(
                    "text-lg font-bold text-red-600")
                ui.label(f"Host {host.serial}: transmit the loaded waveform, CONTINUOUS.")
                with ui.row():
                    ui.button("Cancel", on_click=dlg.close).props("flat")
                    ui.button("KEY TX NOW",
                              on_click=lambda: dlg.submit("go")).props("color=negative")
            if await dlg != "go":
                return
            try:
                res = await start_tx_checked(host, cstat.set_text)
                if res is None:
                    return
                ack, note = res
                if ack.status == 0:
                    cstat.set_text(f"PLAYING: {note} (STOP to end)" if note
                                   else "PLAYING: radiating (STOP to end)")
                    ui.notify(note or "playback active", type="warning")
                else:
                    cstat.set_text(f"start_tx failed: {(ack.result or {}).get('err')}")
            except Exception as e:
                cstat.set_text(f"error: {e}")
        play_btn.on_click(_play)

        async def _stop():
            host = get_host()
            if host is not None:
                await host.cmd("stop_tx", {}, timeout=10.0)
                cstat.set_text("stopped")
        stop_btn.on_click(_stop)
        _sync()


def build_operator_sdr():
    """Operator view (served at /operator): a focus-view console for operating
    several radios at once: a STOP-ALL-TX bar, a selectable radio strip (color-
    coded, TX alarm, rename to roles), and the focused radio's detail panel (band
    controls, spectrum, detections/decodes, activity log, IQ playback, jammer).
    All radios run concurrently; the strip only changes which one the detail panel
    drives. Engineering knobs stay on '/'."""
    _run_notice.add_banner()
    sel = {"serial": None}
    labels = {}                         # serial: operator label (this session)
    logst = {"serial": None, "n": 0}    # activity-log cursor for the focused radio

    def _host():
        return MGR.hosts.get(sel["serial"]) if sel["serial"] else None

    def _name(serial):
        return labels.get(serial) or serial

    # supported sample rates / ports per radio kind; the dropdowns below follow
    # the focused radio.
    # operator labels show the sample rate + an estimated RF bandwidth (~0.8x the
    # rate). Same canonical rate values as the engineering view, richer labels.
    _RATES_G40 = {hz: f"{hz/1e6:g} MSps  (~{hz*0.8/1e6:.1f} MHz)"
                  for hz in G40_SAMPLE_RATES}
    _RATES_X40 = {hz: f"{hz/1e6:g} MSps  (~{hz*0.8/1e6:.1f} MHz)"
                  for hz in X40_SAMPLE_RATES}
    # G40 has four TRx ports, each one SMA that is RX or TX at a time. The RX and
    # TX dropdowns both list all four; _start maps the chosen port to its card +
    # handle. Same card then dual-mode FDD; different cards then independent LOs
    # (best isolation). RX and TX can't be the same SMA.
    _PORTS_G40 = {p: p for p in ("TRx1A", "TRx2A", "TRx1B", "TRx2B")}
    _PORT_MAP = {
        "TRx1A": {"card": 0, "rx": "RXA1", "tx": "TXA1"},
        "TRx2A": {"card": 0, "rx": "RXA2", "tx": "TXA2"},
        "TRx1B": {"card": 1, "rx": "RXA1", "tx": "TXA1"},
        "TRx2B": {"card": 1, "rx": "RXA2", "tx": "TXA2"},
    }
    # X40 (4R1T): all four RX connectors. B-side (J4/J3) pairs with the only TX
    # (TXA1/J5) for FDD; A-side (J2/J1) shares the TX LO so it is receive-only
    # (same-side rule: the port note warns and the service locks TX out).
    _RXPORTS_X40 = {"RXB1": "RXB1 / J4  (FDD with TX)",
                    "RXB2": "RXB2 / J3  (FDD with TX)",
                    "RXA1": "RXA1 / J2  (RX only, shares TX LO)",
                    "RXA2": "RXA2 / J1  (RX only, shares TX LO)"}
    _TXPORTS_X40 = {"TXA1": "TXA1 / J5"}
    # hardware-legal RX gain indices (G40 187-255, X40 195-255), dB above min
    _GAIN_G40 = {str(i): f"+{(i - 187) * 0.5:.1f} dB" for i in range(187, 256)}
    _GAIN_X40 = {str(i): f"+{(i - 195) * 0.5:.1f} dB" for i in range(195, 256)}
    # hardware-legal TX attenuations in dB (0 = max power)
    _ATTEN = TX_ATTEN_CHOICES           # Shared with payload_tab
    # frequency unit multipliers: Hz
    _FUNIT = FREQ_UNITS
    _cfgkind = {"v": None}
    busy = {"v": False}                 # init in progress: hold status + disable START
    txinfo = {"port": None}             # TX port the last START armed; shown by TX panels
    cards = {}                          # serial: {card + element refs}; built once
    empty_lbl = {"el": None}            # the "listening..." placeholder element
    refcur = {"state": None}            # last timing-ref colour state (avoid churn)
    ovlcur = {"v": None}                # last RX-overload colour state (avoid churn)
    synced = {"serial": None}           # which radio's live tuning we've pulled in

    with ui.column().classes("w-full max-w-[90rem] mx-auto gap-3 p-3"):
        # top bar: STOP ALL TX + radio count
        with ui.row().classes("w-full items-center justify-between"):
            ui.label("Operator Console").classes("text-2xl font-bold text-slate-800")
            with ui.row().classes("items-center gap-4"):
                count_lbl = ui.label("0 radios").classes("text-lg font-mono text-gray-600")
                stopall_btn = ui.button("STOP ALL TX", icon="dangerous") \
                    .props("color=negative size=lg").classes("text-lg font-bold")
        ui.label("RADIOS: click one to control it").classes(
            "text-sm font-bold text-gray-500")
        strip = ui.row().classes("w-full gap-3 flex-wrap")
        # the aircraft feed's last fix, field by field (the header chip has the
        # one line version); collapsed so the radio controls stay in reach
        _build_operator_acft_panel()
        ui.separator()
        with ui.row().classes("items-center gap-2"):
            # read-only: which radio is focused (name it on its card above)
            focus_lbl = ui.label("no radio selected").classes("text-xl font-bold")
            focus_serial_lbl = ui.label("").classes(
                "text-xs font-mono text-gray-500 self-center")
            tx_alarm_lbl = ui.label("").classes(
                "text-xl font-bold text-red-600 self-center")
            # the detail panel follows the focused radio's role (set in
            # the engineering view); no picker here
            role_note = ui.label("").classes("text-xs text-gray-600 self-center")
        with ui.card().classes("w-full gap-2") as ctl_card:
            # TUNING: whole-radio settings shared by RX and TX (one RFIC
            #      clock, one center, one sample rate / RF bandwidth) ----
            ui.label("TUNING  (applies to RX and TX)").classes(
                "text-xs font-bold text-slate-600")
            with ui.row().classes("items-center gap-3 flex-wrap"):
                center_in = ui.number("Center", value=425.025, format="%.6f") \
                    .props("dense outlined").classes("w-32")
                cunit_in = ui.select(list(_FUNIT.keys()), value="MHz", label="unit") \
                    .props("dense outlined").classes("w-24")
                rate_in = ui.select(_RATES_G40, value=30_720_000,
                                    label="Sample rate (RF bandwidth)") \
                    .props("dense outlined").classes("w-56")
                clk_in = ui.select(_g40_clock.labels("g40"),
                                   value="internal", label="Timing / ref clock") \
                    .props("dense outlined").classes("w-64")
                # UHD only: 1 PPS time ref is independent of the 10 MHz freq ref
                # (clk_in above). Hidden for epiq, where clk_in is the one knob.
                ppsref_in = ui.select(["none", "internal", "external"],
                                      value="internal", label="1 PPS (time ref)") \
                    .props("dense outlined").classes("w-40")
                ppsref_in.set_visibility(False)
            # Antenna bias + GPS sky (shown with the GPSDO clock)
            gps_row = GpsAntennaRow(_host, lambda: clk_in.value, show_sky=True, poll=True)
            # RECEIVE: RX port + gain (what you receive / the spectrum source)
            ui.label("RECEIVE").classes("text-xs font-bold text-green-700")
            with ui.row().classes("items-center gap-3 flex-wrap"):
                rxport_in = ui.select(_PORTS_G40, value="TRx1A", label="RX port") \
                    .props("dense outlined").classes("w-36")
                _ppu.hook_tips(rxport_in)
                rxgain_in = ui.select(_GAIN_G40, value="255", label="RX gain") \
                    .props("dense outlined").classes("w-32")
                # UHD only: precise dB gain (the 2-dB dropdown grid is lossy);
                # shown instead of rxgain_in when family=="uhd".
                rxgain_num = ui.number("RX gain (dB)", value=40, format="%g") \
                    .props("dense outlined").classes("w-32")
                rxgain_num.set_visibility(False)
                # Caps-driven radios pick from the radio's own
                # supported values (labels show the split the backend applies);
                # the hidden number box mirrors the choice for the start path.
                rxgain_sel = ui.select({}, label="RX gain (dB)") \
                    .props("dense outlined options-dense").classes("w-64")
                rxgain_sel.set_visibility(False)
                hr_note = ui.label(B200_HIGH_RATE_NOTE) \
                    .classes("text-xs text-amber-700 w-64")
                hr_note.set_visibility(False)
                # TX-only option: when off, START tunes + arms TX but doesn't
                # start the RX stream (no spectrum): for pure transmit/jamming
                rx_on_cb = ui.checkbox("Receive (monitor) on START", value=True)
            # UHD quality hooks (family "uhd" only; hidden for G40/X40)
            #      front-end corrections a UHD radio exposes but the NV100 path
            #      does not; the row is shown only when caps advertise them.
            #      (RX bandwidth lives in the engineering view only.)
            with ui.row().classes("items-center gap-3 flex-wrap") as uhd_qual_row:
                dciq_cb = ui.checkbox("DC/IQ auto-correct", value=True)
                looff_in = ui.number("LO offset (MHz)", value=0.0, format="%g") \
                    .props("dense outlined").classes("w-36") \
                    .tooltip("0 = automatic: the LO is moved off the band when the "
                             "rate allows. Best + / Best - pick the best offset for "
                             "the selected rate. Applies at START.")
                ui.button("Best +", on_click=lambda: _lo_best(+1)) \
                    .props("dense outline no-caps")
                ui.button("Best -", on_click=lambda: _lo_best(-1)) \
                    .props("dense outline no-caps")
                lo_note = ui.label(LO_NOTE_IDLE).classes("text-xs text-gray-500")
            uhd_qual_row.set_visibility(False)
            # SoapySDR front-end switches (caps "fe_hooks"; HackRF)
            #      RF amps, antenna bias tee, manual baseband filter. Shown only
            #      when the radio advertises them; ride init via extras.
            with ui.row().classes("items-center gap-3 flex-wrap") as fe_row:
                rxamp_cb = ui.checkbox("RX amp +14 dB", value=False)
                txamp_cb = ui.checkbox("TX amp +14 dB", value=False)
                bias_cb = ui.checkbox("Antenna bias 3.3 V", value=False)
                febw_in = ui.number("Baseband filter (MHz, 0=auto)", value=0,
                                    format="%g").props("dense outlined").classes("w-52")
            fe_row.set_visibility(False)
            # tx_box hides for roles that never transmit
            with ui.column().classes("w-full gap-2 p-0") as tx_box:
                # TRANSMIT: where the jammer, IQ playback and custom waveforms key
                ui.label("TRANSMIT  (jammer, IQ playback, custom waveforms key here)") \
                    .classes("text-xs font-bold text-red-700")
                hd_lbl = ui.label("half-duplex radio: receive pauses while TX is keyed "
                                  "(spectrum freezes until Stop TX)") \
                    .classes("text-xs text-amber-700")
                hd_lbl.set_visibility(False)
                with ui.row().classes("items-center gap-3 flex-wrap"):
                    txport_in = ui.select(_PORTS_G40, value="TRx1B", label="TX port") \
                        .props("dense outlined").classes("w-36")
                    _ppu.hook_tips(txport_in)
                    txatten_in = ui.select(_ATTEN, value="30", label="TX atten (dB)") \
                        .props("dense outlined").classes("w-36")
                    # UHD only: precise dB TX gain (not attenuation); shown instead
                    # of txatten_in when family=="uhd".
                    txgain_num = ui.number("TX gain (dB)", value=40, format="%g") \
                        .props("dense outlined").classes("w-32")
                    txgain_num.set_visibility(False)
                    txgain_sel = ui.select({}, label="TX atten (dB)") \
                        .props("dense outlined options-dense").classes("w-64")
                    txgain_sel.set_visibility(False)
                    portnote_lbl = ui.label("").classes("text-xs self-center")
            # actions
            with ui.row().classes("items-center gap-3 flex-wrap"):
                start_btn = ui.button("START", icon="play_arrow").props("color=positive")
                stop_btn = ui.button("STOP RX").props("color=grey")
                # no CLOSE button: START already tears down + re-inits the radio
                # (service _do_init releases the previous session first), so the
                # operator never has to release the card manually.

            op_ports = {"view": None, "sig": None}         # PORT_MAP

            def _port_note():
                # live hint about the RX+TX port pairing (isolation / conflict)
                rv, tv = rxport_in.value, txport_in.value
                _lk, _refused = _ppu.lock_note(op_ports["view"], rv, tv)   # PORT_MAP
                if _lk and _refused:
                    portnote_lbl.set_text(_lk)
                    portnote_lbl.classes(replace="text-xs self-center text-red-600 font-bold")
                    return
                if rv in _X40_RX_CONN:
                    _c = x40_same_side_conflict(rv, tv)
                    if _c:
                        portnote_lbl.set_text(
                            f"Warning: {rv} ({_X40_RX_CONN[rv]}) + {tv} (J5) are both A-side and share one LO. "
                            f"TX is LOCKED OUT while receiving on this port. "
                            f"Use RXB1 (J4) or RXB2 (J3) for RX+TX.")
                        portnote_lbl.classes(replace="text-xs self-center text-red-600 font-bold")
                    else:
                        portnote_lbl.set_text(
                            f"{rv} ({_X40_RX_CONN[rv]}) RX on the B side, {tv} (J5) TX on the A side: FDD OK")
                        portnote_lbl.classes(replace="text-xs self-center text-gray-600")
                    return
                rp, tp = _PORT_MAP.get(rv), _PORT_MAP.get(tv)
                if rp is None or tp is None:
                    portnote_lbl.set_text(""); return
                if rv == tv:
                    portnote_lbl.set_text("Warning: RX and TX must be different ports")
                    portnote_lbl.classes(replace="text-xs self-center text-red-600")
                elif g40_same_card_conflict(rv, tv):          # G40_SAME_CARD
                    portnote_lbl.set_text("Warning: " + g40_same_card_conflict(rv, tv))
                    portnote_lbl.classes(replace="text-xs self-center text-red-600 font-bold")
                elif _lk:                                     # PORT_MAP: same card, TX on the 2 jack
                    portnote_lbl.set_text(_lk)
                    portnote_lbl.classes(replace="text-xs self-center text-amber-700 font-bold")
                elif rp["card"] == tp["card"]:
                    portnote_lbl.set_text(f"{rv}+{tv}: same card, dual-mode FDD")
                    portnote_lbl.classes(replace="text-xs self-center text-amber-700")
                else:
                    portnote_lbl.set_text(f"RX {rv}, TX {tv}: separate cards, best isolation")
                    portnote_lbl.classes(replace="text-xs self-center text-gray-600")
            rxport_in.on_value_change(lambda e: _port_note())
            txport_in.on_value_change(lambda e: _port_note())
            _port_note()

            def _no_tx():
                # PORT_MAP: the map leaves this radio no jack to transmit from
                _pm = op_ports["view"] and op_ports["view"]["pm"]
                return bool(_pm) and not _ports.jacks_for(_pm, "tx")

            def _follow_ports(host, cfgkey):
                """PORT_MAP: grey out jacks the radio's port map locks, tooltip =
                chain. Redrawn when the map, the radio or its port list changes."""
                view = _ppu.port_view(host, host_config(host))
                sig = (host.serial, cfgkey, view and view["sig"])
                if sig == op_ports["sig"]:
                    return
                op_ports.update(view=view, sig=sig)
                _ppu.apply(rxport_in, view, "rx")
                _ppu.apply(txport_in, view, "tx")
                _port_note()
            status_lbl = ui.label("").classes("font-mono text-sm text-gray-700")
            # What the radio has loaded to transmit, and any edit here
            # that the radio does not have yet
            txwf_lbl = ui.label("TX waveform: none loaded").classes("font-mono text-xs text-gray-600")
            pend_lbl = ui.label("").classes("text-xs font-bold text-amber-700")
            pend_lbl.set_visibility(False)
            # UHD sensor telemetry (ref/LO lock, RSSI, temps); polled on a timer
            # only while a UHD radio is focused + up. Hidden for epiq. Plain
            # ui.label coloured by worst severity via .style() (ui.html here
            # floods "parent slot deleted" and breaks refresh).
            sensor_lbl = ui.label("").classes("font-mono text-xs text-gray-600")
            sensor_lbl.set_visibility(False)

        # Widgets that follow the radio (keys = view_sync.op_fields)
        vguard = _vs.EditGuard()
        svc_now = {}                        # last radio values, per key
        _OPW = {"center_mhz": center_in, "rate": rate_in, "clk": clk_in,
                "rx_port": rxport_in, "rx_gain": rxgain_in, "tx_port": txport_in,
                "tx_atten": txatten_in}
        _OPN = {"center_mhz": "Center", "rate": "Sample rate", "clk": "Timing / ref clock",
                "rx_port": "RX port", "rx_gain": "RX gain", "tx_port": "TX port",
                "tx_atten": "TX atten"}
        for _k, _el in _OPW.items():
            _vs_track(vguard, _k, _el, svc_now)

        async def _start():
            host = _host()
            if host is None:
                ui.notify("no radio selected", type="warning"); return
            rate = int(rate_in.value)
            center = int(round(float(center_in.value) * _FUNIT.get(cunit_in.value, 1e6)))
            is_x40 = getattr(host, "host_type", None) == int(HostType.X40_TX)
            fam = caps_family(host.serial)
            # epiq semantics: rx_gain is a hardware index, tx_gain is dB atten.
            # UHD overrides these to real dB below; everything else untouched.
            rx_gain_val = int(rxgain_in.value)
            tx_gain_val = float(txatten_in.value)
            if fam == "uhd":
                # UHD-family radio (e.g. B200): single antenna, gains in dB,
                # front-end quality hooks ride init via extras.
                caps = _CAPS_CACHE.get(host.serial) or {}
                kind = caps.get("kind", "b200")
                rx_ant = str(rxport_in.value
                             or (caps.get("rx_antennas") or ["RX2"])[0])
                tx_ant = str(txport_in.value
                             or (caps.get("tx_antennas") or ["TX/RX"])[0])
                rx_gain_val = float(rxgain_num.value)    # dB (precise number)
                tx_gain_val = float(txgain_num.value)    # dB gain (not atten)
                # operator view carries no bandwidth box: RX/TX bw follow the
                # rate (rx_bw_hz omitted means backend auto). 1 PPS time ref is
                # independent of the 10 MHz freq ref (clock_source below).
                extras = {"rf_bandwidth_hz": int(rate * 0.8),
                          "tx_center_freq": center,
                          "dc_offset": bool(dciq_cb.value),
                          "iq_balance": bool(dciq_cb.value),
                          "lo_offset_hz": float(looff_in.value or 0) * 1e6,
                          "time_source": str(ppsref_in.value or "internal")}
                lo_note.set_text(LO_NOTE_IDLE)                 # this START applies it
                lo_note.classes(replace="text-xs text-gray-500")
                tx_port_name = tx_ant
            elif fam == "soapy":
                # SoapySDR radio (e.g. xSDR): single RX/TX antenna from caps, RX
                # gain in dB. _apply_uhd_caps shows the dB number inputs; per the
                # caps (tx_gain mode "atten", 0..52) txgain_num is dB attenuation.
                caps = _CAPS_CACHE.get(host.serial) or {}
                kind = caps.get("kind", "xsdr")
                rx_ant = str(rxport_in.value or (caps.get("rx_antennas") or ["AUTO"])[0])
                tx_ant = str(txport_in.value or (caps.get("tx_antennas") or ["AUTO"])[0])
                rx_gain_val = float(rxgain_num.value)
                tx_gain_val = float(txgain_num.value)
                extras = {"tx_center_freq": center}
                if caps.get("fe_hooks"):
                    extras.update({"rx_amp": bool(rxamp_cb.value),
                                   "tx_amp": bool(txamp_cb.value),
                                   "bias_tx": bool(bias_cb.value)})
                    _febw = float(febw_in.value or 0) * 1e6
                    if _febw > 0:
                        extras["rx_bw_hz"] = _febw
                tx_port_name = tx_ant
            elif is_x40:
                kind = "x40"
                rx_ant = str(rxport_in.value or "RXB1")
                tx_ant = str(txport_in.value or "TXA1")
                extras = {"card": 0, "rf_bandwidth_hz": int(rate * 0.8),
                          "tx_center_freq": center}
                tx_port_name = f"{tx_ant} / J5"
                # A-side RX is allowed (receive-only) but TX is
                # locked until a re-START on J3/J4; the TX panels show the lock
                # and the service refuses to key regardless.
                _lock = x40_same_side_conflict(rx_ant, tx_ant)
                if _lock:
                    tx_port_name = (f"LOCKED {tx_ant} / J5, RX on {rx_ant} "
                                    f"({_X40_RX_CONN.get(rx_ant, '?')}) shares its LO; "
                                    f"re-START with RX on J3 or J4 to transmit")
            else:
                # map the chosen RX + TX ports to card + handle. Same card gives
                # dual-mode FDD; different cards give independent LOs (cross-card).
                kind = "g40"
                rxp = _PORT_MAP.get(rxport_in.value)
                txp = _PORT_MAP.get(txport_in.value)
                if rxp is None or txp is None:
                    ui.notify("pick an RX and a TX port", type="warning"); return
                if rxport_in.value == txport_in.value and not _no_tx():
                    ui.notify("RX and TX must be different ports (one SMA can't do both)",
                              type="negative"); return
                # Refuse the reverse-role pair before sending an
                # init the card cannot honour (the service rejects it too)
                _c = g40_same_card_conflict(rxport_in.value, txport_in.value)
                if _c:
                    ui.notify(_c, type="negative", close_button=True, timeout=12000)
                    return
                rx_ant, tx_ant = rxp["rx"], txp["tx"]
                chan_mode = "dual" if rxp["card"] == txp["card"] else "single"
                extras = {"card": rxp["card"], "tx_card": txp["card"],
                          "chan_mode": chan_mode, "rf_bandwidth_hz": int(rate * 0.8),
                          "tx_center_freq": center}
                tx_port_name = str(txport_in.value)
            p = {"kind": kind, "sample_rate": rate, "center_freq": center,
                 "rx_gain": rx_gain_val, "tx_gain": tx_gain_val,
                 "clock_source": str(clk_in.value or "internal"), "rx_antenna": rx_ant,
                 "tx_antenna": tx_ant, "extras": extras,
                 "psd_nfft": 4096, "psd_window": "hann", "psd_buffer": psd_buffer_for(kind)}
            if _no_tx():                                   # PORT_MAP: a receive only radio
                p.pop("tx_antenna", None)
                p["extras"] = {k: v for k, v in extras.items() if k != "tx_card"}
                p["extras"]["chan_mode"] = "single"
                tx_port_name = "LOCKED no TX jack on this radio"
            _why = _ports.init_refusal(op_ports["view"] and op_ports["view"]["pm"], kind, p)
            if _why:                                       # PORT_MAP: the service says the same
                status_lbl.set_text(_why)
                ui.notify(_why, type="negative", close_button=True, timeout=12000)
                return
            # mark busy so the refresh timer stops overwriting the status line,
            # and disable START so a second click can't queue another init
            busy["v"] = True
            start_btn.disable()
            status_lbl.set_text(f"initializing {_name(host.serial)} "
                                f"({kind} {rate/1e6:g} Msps), may take ~30 s...")
            try:
                ack = await host.cmd("init", p, timeout=120.0)
                if ack.status != 0:
                    status_lbl.set_text(f"init failed: {(ack.result or {}).get('err')}")
                    ui.notify("init failed", type="negative"); return False
                txinfo["port"] = tx_port_name       # TX panels read this
                applied_clk[host.serial] = p["clock_source"]
                if (ack.result or {}).get("tx_locked"):
                    ui.notify((ack.result or {}).get("warning", "TX locked out"),
                              type="warning", close_button=True, timeout=12000)
                if (ack.result or {}).get("same_card_tx"):  # PORT_MAP: legal, but no RF at the jack
                    ui.notify(ack.result.get("warning"), type="warning",
                              close_button=True, timeout=15000)
                # A locked TX reads "TX LOCKED OUT", never "armed"
                _txd = ("TX LOCKED OUT (no TX jack in the port map)" if _no_tx() else
                        "TX LOCKED OUT (A-side RX shares the TX LO)"
                        if str(tx_port_name).startswith("LOCKED ")
                        else f"TX armed on {tx_port_name}")
                if rx_on_cb.value:
                    status_lbl.set_text("starting RX...")
                    await host.cmd("start_rx", {}, timeout=15.0)
                    status_lbl.set_text(f"monitoring, {_txd}")
                    ui.notify(f"radio started: {_txd}", type="positive")
                else:
                    # TX-only: tuned + TX armed, no RX stream (no spectrum)
                    status_lbl.set_text(f"tuned, RX off, {_txd}")
                    ui.notify(f"tuned (RX off): {_txd}", type="positive")
                return True
            except Exception as e:
                status_lbl.set_text(f"error: {e}")
                return False
            finally:
                busy["v"] = False
                start_btn.enable()
        start_btn.on_click(_start)

        # A clock pick on a running radio re-runs START (same init
        # path + RX per the monitor checkbox). Client event only, so a caps /
        # focus refresh that resets the dropdown never re-inits the radio.
        applied_clk = {}                    # serial: clock_source of the last good START
        clkprev = {"v": clk_in.value}

        async def _live_clock(_e=None):
            host = _host()
            new = str(clk_in.value or "internal")
            prev, clkprev["v"] = clkprev["v"], new
            if host is None or busy["v"] or not _inited():
                return                      # not running: START will send it
            _cfg = host_config(host)        # The radio's clock, not this page's memory
            if (_cfg.get("clock_source") if _cfg and _cfg.get("inited")
                    else applied_clk.get(host.serial)) == new:
                return
            if not await confirm_clock_change(host, new):
                _cfg = host_config(host)
                clk_in.set_value((_cfg or {}).get("clock_source")
                                 or applied_clk.get(host.serial) or prev)
                clkprev["v"] = clk_in.value
                vguard.release("clk")
                return
            ui.notify(f"reference clock -> {new}: re-initializing {_name(host.serial)}")
            await _start()
        clk_in.on("update:model-value", _live_clock)

        # live retune: center / RX gain / TX atten apply without a re-init
        #     once the radio is up (sample rate / ports still need START; the clock
        #     re-runs START by itself, see above) ---
        def _inited():
            h = _host()
            return (h is not None and h.connected
                    and h.radio_state != RadioState.UNINIT)

        async def _live_center(_e=None):
            host = _host()
            if not _inited():
                return
            hz = int(round(float(center_in.value) * _FUNIT.get(cunit_in.value, 1e6)))
            await host.cmd("set_center", {"hz": hz}, timeout=10.0)
        center_in.on("keydown.enter", _live_center)
        center_in.on("blur", _live_center)
        cunit_in.on_value_change(lambda e: _live_center())

        async def _live_gain(_e=None):
            host = _host()
            if _inited():
                await host.cmd("set_gain", {"index": int(rxgain_in.value)}, timeout=10.0)
        rxgain_in.on("update:model-value", _live_gain)   # Clicks only, not syncs

        async def _live_atten(_e=None):
            host = _host()
            if _inited():
                await host.cmd("set_tx_atten",
                               {"qdb": int(round(float(txatten_in.value) * 4))}, timeout=10.0)
        txatten_in.on("update:model-value", _live_atten)

        # A pick mirrors into the hidden number box (what START
        # sends) and, when the radio is up, applies live: RX gain in dB via
        # set_gain (dB-family backends take dB), TX attenuation via set_tx_atten.
        async def _live_gain_sel(e):
            if e.value is None:
                return
            rxgain_num.set_value(float(e.value))
            if _hr.get("quiet") == e.value:
                # moved by the high-rate rule on a rate pick, not by the
                # operator: the new gain goes out with the next START/INITIALIZE
                _hr["quiet"] = None
                return
            host = _host()
            if _inited() and caps_family(host.serial) in ("uhd", "pluto", "soapy"):
                await host.cmd("set_gain", {"index": float(e.value)}, timeout=10.0)
        rxgain_sel.on_value_change(_live_gain_sel)

        # B200 at 50 MSps and up: RX gain to 60 dB (b200_rate_gain). Only on a
        # rate pick or when the radio's caps load; never sent live.
        _hr = {"kind": None, "bumped": None, "quiet": None}

        def _b200_rate_gain(_e=None, move_gain=True):
            new, bumped, show = b200_rate_gain(
                _hr["kind"], rate_in.value, rxgain_num.value, _hr["bumped"])
            hr_note.set_visibility(show)
            if not move_gain:
                return
            _hr["bumped"] = bumped
            if new is None:
                return
            key = _nearest_gain_key(rxgain_sel, new)
            if key is None:
                rxgain_num.set_value(new)
                return
            _hr["quiet"] = key if key != rxgain_sel.value else None
            rxgain_num.set_value(float(key))
            rxgain_sel.set_value(key)
        rate_in.on_value_change(_b200_rate_gain)

        def _lo_best(sign):
            """Best + / Best -: the best LO offset for the selected rate
            (lo_best_choice). Like every LO offset it goes out at START."""
            if _hr["kind"] != "b200":
                ui.notify("Best LO offset is worked out for the B200 only", type="warning")
                return
            lo_mhz, raise_gain, msg, kind = lo_best_choice(rate_in.value, sign,
                                                           rxgain_num.value)
            looff_in.set_value(lo_mhz)
            if raise_gain:
                key = _nearest_gain_key(rxgain_sel, B200_HIGH_RATE_RX_GAIN_DB)
                if key is not None:
                    _hr["quiet"] = key if key != rxgain_sel.value else None
                    rxgain_num.set_value(float(key))
                    rxgain_sel.set_value(key)
            ui.notify(msg, type=kind, multi_line=True)

        def _lo_changed(_e=None):
            pending = _inited()
            lo_note.set_text(LO_NOTE_PENDING if pending else LO_NOTE_IDLE)
            lo_note.classes(replace="text-xs " + ("text-amber-700 font-bold" if pending
                                                  else "text-gray-500"))
        looff_in.on_value_change(_lo_changed)

        async def _live_txgain_sel(e):
            if e.value is None:
                return
            txgain_num.set_value(float(e.value))
            host = _host()
            caps = _CAPS_CACHE.get(host.serial) if host is not None else None
            if (_inited() and caps and (caps.get("tx_gain") or {}).get("mode") == "atten"):
                await host.cmd("set_tx_atten",
                               {"qdb": int(round(float(e.value) * 4))}, timeout=10.0)
        txgain_sel.on_value_change(_live_txgain_sel)

        async def _stop():
            host = _host()
            if host is not None:
                await host.cmd("stop_rx", {}, timeout=10.0)
        stop_btn.on_click(_stop)

        async def _logging_stop(_e=None):
            host = _host()
            rl = GLOGS.get(sel["serial"]) if sel["serial"] else None
            # turn off any G40-side logging that was running: started here, or
            # reported by the radio: started from another view or GUI, this GUI's
            # RadioLog knows nothing of it, but the radio is still logging
            _lg = (host_config(host) or {}).get("log") or {}
            if host is not None and ((rl is not None and rl.radio_on)
                                     or any(_lg.values())):
                for cmd in ("log_detections", "log_telemetry"):
                    try:
                        await host.cmd(cmd, {"enable": False}, timeout=5.0)
                    except Exception:
                        pass
            if rl is not None:
                rl.stop()
            log_stat.set_text("logging off")

        async def _logging_start(_e=None):
            host = _host()
            if host is None:
                ui.notify("select a radio first", type="warning"); return
            cats = {"detections": cb_det.value, "position": cb_pos.value,
                    "health": cb_health.value, "decodes": cb_dec.value}
            if not any(cats.values()):
                log_stat.set_text("pick at least one thing to log"); return
            try:                                       # the GUI container runs in UTC
                utc_offset_s = -60 * int(await ui.run_javascript(
                    "new Date().getTimezoneOffset()", timeout=2.0))
            except Exception:
                utc_offset_s = None
            await _logging_stop()                      # clean restart
            dst = log_dst.value
            rl = GLOGS.get(host.serial)
            try:
                paths = rl.start(dst, cats, utc_offset_s)
            except OSError as e:
                log_stat.set_text(f"cannot open log file: {e}"); return
            parts = []
            if dst == "g40":
                notes = []
                if cats["detections"]:
                    rl.radio_on = True
                    a = await host.cmd("log_detections", {"enable": True}, timeout=8.0)
                    notes.append("detections to " + (a.result or {}).get("path", "?")
                                 if a.status == 0 else f"det err:{(a.result or {}).get('err')}")
                if cats["position"] or cats["health"]:
                    rl.radio_on = True
                    a = await host.cmd("log_telemetry", {"enable": True}, timeout=8.0)
                    notes.append("pos/health to " + (a.result or {}).get("path", "?")
                                 if a.status == 0 else f"telem err:{(a.result or {}).get('err')}")
                if notes:
                    parts.append("G40: " + ", ".join(notes))
            if paths:
                parts.append("local: " + ", ".join(paths))
            rl.note = "; ".join(parts)
            log_stat.set_text(rl.note)
            ui.notify("logging started", type="positive")

        async def _stop_all():
            hosts = list(MGR.hosts.values())
            # also stops a planner run and disarms its triggers, or the next step
            # would key again after this button. The planner code loads with its
            # page; not loaded means no run, and this button must not wait on an import
            mt = _sys.modules.get("dpmg.mission.mission_tab")
            sent = await (mt.stop_all_tx(MGR, hosts) if mt else _send_stop_tx(hosts))
            if sent == len(hosts):
                ui.notify(f"STOP ALL TX sent to {sent} radio(s)", type="warning")
            else:
                retry = mt.stop_unconfirmed(MGR) if mt else []
                ui.notify(f"STOP ALL TX reached {sent} of {len(hosts)} radio(s); "
                          + ("not answering: " + ", ".join(retry)
                             + f"; stop_tx is retried for {mt.MissionRunner.STOP_RETRY_S:g} s, "
                               "see the planner audit log"
                             if retry else "check the others"),
                          type="negative", timeout=0, close_button=True)
            bad = mt.atten_not_reset(MGR) if mt else []
            if bad:
                ui.notify("RX attenuator NOT back to 120 dB: " + "; ".join(bad),
                          type="negative", timeout=0, close_button=True)
        stopall_btn.on_click(_stop_all)

        def _save_card_name(serial, inp):
            v = (inp.value or "").strip()
            if v and v != serial:
                labels[serial] = v
            else:
                labels.pop(serial, None)      # blank or == serial: no custom name

        def _build_card(serial, h):
            """Create one radio card once (its editable name field must survive the
            0.5 s telemetry refresh, so the strip is updated in place, not rebuilt)."""
            with strip:
                card = ui.card().classes(
                    "cursor-pointer border-2 p-3 min-w-[190px] bg-slate-50 border-slate-300")
            card.on("click", lambda s=serial: sel.update(serial=s))
            with card:
                # the bold name box is the rename field (edit + Enter/leave to apply)
                name = ui.input(value=_name(serial)).props("borderless dense") \
                    .classes("text-lg font-bold w-full") \
                    .tooltip("edit this radio's name; clear to use its serial")
                name.on("keydown.enter", lambda e, s=serial, i=name: _save_card_name(s, i))
                name.on("blur", lambda e, s=serial, i=name: _save_card_name(s, i))
                serial_lbl = ui.label("").classes("text-xs font-mono text-gray-500")
                type_lbl = ui.label(_HOST_TXT.get(h.host_type, "?")).classes(
                    "text-xs text-gray-600")
                role_lbl = ui.label("").classes("text-xs")
                state_lbl = ui.label("").classes("text-base font-bold")
                link_lbl = ui.label("").classes("text-xs font-bold")
                pos_lbl = ui.label("").classes("text-xs font-mono")
                temp_lbl = ui.label("").classes("text-xs font-mono") \
                    .tooltip("Radio host chip temperature (Jetson Orin junction, the hottest point on the module). "
                             f"Amber from {_ht.WARN_C:.0f} C, red from {_ht.HOT_C:.0f} C; "
                             "the module throttles at 99 C and shuts down at 104 C")
                card_temp_lbl = ui.label("").classes("text-xs font-mono") \
                    .tooltip("Radio card temperatures (NV100 card A / card B, or the B200's own sensor), read while "
                             f"the radio is started. Amber from {_ht.CARD_WARN_C:.0f} C, "
                             f"red from {_ht.CARD_HOT_C:.0f} C")
                baro_lbl = ui.label("").classes("text-xs font-mono") \
                    .tooltip("Payload barometer (BMP581/585 on the radio host I2C), read every "
                             f"{_baro.POLL_S:.0f} s whether or not the radio is started. Altitude is the "
                             "pressure altitude from 1013.25 hPa. Amber when the reading is stale")
                hpa_lbl = ui.label("").classes("text-xs font-mono") \
                    .tooltip("Empower 1189 temperature (DB9 pin 3) and supply current (DB9 pin 2) through the "
                             "payload ADC, read every 2 s by the radio service. Only valid while the HPA is on. Amber from 70 C or above 12 A, "
                             "red from 85 C (the 1189 case maximum)")
                with ui.row().classes("items-center gap-1 no-wrap"):
                    imu_lbl = ui.label("").classes("text-xs font-mono") \
                        .tooltip("NV100 accelerometer, read while the radio is started. Tilt is "
                                 "measured from the orientation at start (or the last 'set level'). "
                                 f"Amber from {_imu.TILT_WARN_DEG:.0f}°, red from "
                                 f"{_imu.TILT_ALARM_DEG:.0f}° or after a shock over "
                                 f"{_imu.SHOCK_G:.1f} g (red for {_imu.SHOCK_HOLD_S:.0f} s)")
                    lvl_btn = ui.button("set level").props("flat dense size=xs no-caps") \
                        .classes("text-xs") \
                        .tooltip("take the radio's current orientation as level")
                    lvl_btn.on("click.stop", lambda e, s=serial: _imu_set_level(s))
                fpga_lbl = ui.label("").classes("text-xs")
            return {"card": card, "serial_lbl": serial_lbl, "state_lbl": state_lbl,
                    "role_lbl": role_lbl, "fpga_lbl": fpga_lbl, "_rc": {},
                    "link_lbl": link_lbl, "pos_lbl": pos_lbl, "temp_lbl": temp_lbl,
                    "card_temp_lbl": card_temp_lbl, "imu_lbl": imu_lbl, "lvl_btn": lvl_btn,
                    "baro_lbl": baro_lbl, "hpa_lbl": hpa_lbl}

        async def _imu_set_level(serial):
            h = MGR.hosts.get(serial)
            if h is None:
                return
            try:
                ack = await h.cmd("imu_set_level", {}, timeout=4.0)
            except Exception as ex:
                ui.notify(f"set level: {ex}", type="warning")
                return
            if ack is None or ack.status != proto.CmdStatus.OK:
                ui.notify(f"set level: {((ack.result or {}) if ack else {}).get('err', 'failed')}",
                          type="warning")
            else:
                ui.notify("level set to the current orientation", type="positive")

        def _update_card(refs, serial, h):
            """Update a card's mutable bits in place (never the name field)."""
            tx_on = (h.status is not None and
                     h.status.flags & proto.Status.FLAG_TX_ACTIVE)
            if tx_on:
                cls = "bg-red-100 border-red-600"
            elif not h.connected:
                cls = "bg-red-50 border-red-300"
            elif h.radio_state == RadioState.STREAMING:
                cls = "bg-green-50 border-green-500"
            else:
                cls = "bg-slate-50 border-slate-300"
            refs["card"].classes(
                replace=f"cursor-pointer border-2 p-3 min-w-[190px] {cls} "
                + ("ring-4 ring-primary" if serial == sel["serial"] else ""))
            refs["serial_lbl"].set_text(serial if labels.get(serial) else "")
            _rec = _rec_state.text(serial)       # receive-only capture in progress
            if tx_on:
                refs["state_lbl"].set_text("TRANSMITTING" + (f" + {_rec}" if _rec
                                                             else rx_off_note(h.radio_state)))
                refs["state_lbl"].classes(replace="text-base font-bold text-red-600")
            elif _rec:
                refs["state_lbl"].set_text(f"RECORDING ({_rec[4:]})")
                refs["state_lbl"].classes(replace="text-base font-bold text-red-600")
            else:
                txt, _c = _STATE_TXT.get(h.radio_state, ("?", ""))
                refs["state_lbl"].set_text(txt + rx_off_note(h.radio_state))
                refs["state_lbl"].classes(replace="text-base font-bold")
            _ltxt, _lcol = link_text(h.connected, host_config(h))
            refs["link_lbl"].set_text(_ltxt)
            refs["link_lbl"].classes(replace="text-xs font-bold " + _lcol)
            for _key, _fn in (("role_lbl", _rfu.role_text), ("fpga_lbl", _rfu.fpga_text)):
                _t = _fn(host_config(h))
                if refs["_rc"].get(_key) != _t:
                    refs["_rc"][_key] = _t
                    refs[_key].set_text(_t[0])
                    refs[_key].classes(replace=_t[1])
            s = h.status
            if s is not None and (s.flags & proto.Status.FLAG_POS_VALID):
                alt = f" {s.alt_m:.0f}m" if s.alt_m else ""
                refs["pos_lbl"].set_text(
                    f"GPS {s.lat_1e7/1e7:+.5f}, {s.lon_1e7/1e7:+.5f}{alt}")
                refs["pos_lbl"].classes(replace="text-xs font-mono text-blue-700")
            else:
                refs["pos_lbl"].set_text("no GPS fix")
                refs["pos_lbl"].classes(replace="text-xs font-mono text-gray-400")
            tj = _ht.from_wire(s.temp_c_x10) if s is not None else None
            if tj is None:
                refs["temp_lbl"].set_text("no chip temp")
                refs["temp_lbl"].classes(replace="text-xs font-mono text-gray-400")
            else:
                col = {"ok": "text-green-700", "warm": "text-amber-600 font-bold",
                       "hot": "text-red-600 font-bold"}[_ht.level(tj)]
                refs["temp_lbl"].set_text(f"Chip temp {tj:.0f} °C"
                                          + (" HOT" if _ht.level(tj) == "hot" else "")
                                          + _ht.power_text((host_config(h) or {}).get("host_power")))
                refs["temp_lbl"].classes(replace=f"text-xs font-mono {col}")
            _hcfg = host_config(h)
            cts = _ht.card_temps_from_config(_hcfg)
            if not cts:
                refs["card_temp_lbl"].set_text(_ht.last_card_text(_hcfg))   # last reading + age, or not started
                refs["card_temp_lbl"].classes(replace="text-xs font-mono text-gray-400")
            else:
                cw = _ht.worst(_ht.card_level(t) for _c, t in cts)
                ccol = {"ok": "text-green-700", "warm": "text-amber-600 font-bold",
                        "hot": "text-red-600 font-bold"}[cw]
                refs["card_temp_lbl"].set_text(
                    ", ".join(f"{_ht.card_label(_hcfg, c)} {t:.0f} °C" for c, t in cts)
                    + (" HOT" if cw == "hot" else ""))
                refs["card_temp_lbl"].classes(replace=f"text-xs font-mono {ccol}")
            _cfg = host_config(h)
            _want_caps(h)
            itxt, ilvl = _imu.card_text((_cfg or {}).get("imu"), radio_kind(h.serial, _cfg))
            refs["imu_lbl"].set_text(itxt)
            refs["imu_lbl"].classes(replace="text-xs font-mono " + {
                None: "text-gray-400", "ok": "text-green-700",
                "warm": "text-amber-600 font-bold", "hot": "text-red-600 font-bold"}[ilvl])
            refs["lvl_btn"].set_visibility(bool((_cfg or {}).get("imu")))
            btxt, blvl = _baro.card_text((_cfg or {}).get("baro"))
            refs["baro_lbl"].set_text(btxt)
            refs["baro_lbl"].classes(replace="text-xs font-mono " + {
                None: "text-gray-400", "ok": "text-green-700", "warm": "text-amber-600 font-bold"}[blvl])
            htxt, hlvl = _rf.hpa_drop_text(_cfg) or _rf.hpa_card_text((_cfg or {}).get("hpa_monitor"), radio_kind(h.serial, _cfg))
            refs["hpa_lbl"].set_text(htxt)
            refs["hpa_lbl"].set_visibility(bool(htxt))
            refs["hpa_lbl"].classes(replace="text-xs font-mono " + {
                None: "text-gray-400", "ok": "text-green-700",
                "warm": "text-amber-600 font-bold", "hot": "text-red-600 font-bold"}[hlvl])

        with ui.card().classes("w-full"):
            with ui.row().classes("w-full items-center justify-between"):
                with ui.row().classes("items-center gap-3"):
                    ui.label("Spectrum").classes("text-base font-bold")
                    # peak-hold (max-hold): accumulate the highest value per bin so
                    # narrow / intermittent spikes stand out and persist
                    pk_cb = ui.checkbox("Peak hold").props("dense")
                    pk_clear = ui.button("clear peak").props(
                        "flat dense size=sm no-caps").classes("text-xs")
                # readout for a drag-selection on the PSD (the lineX "select band"
                # brush tool in the chart toolbox)
                sel_bw_lbl = ui.label("select a band on the plot to measure it") \
                    .classes("text-xs font-mono text-gray-500")
            psd_chart, psd_lbl = _build_psd_chart()
            pkhold = {"acc": None}          # accumulated max-hold spectrum (np array)

            def _pk_reset():
                pkhold["acc"] = None
                psd_chart.options["series"][1]["data"] = []
                psd_chart.update()
            pk_clear.on_click(lambda: _pk_reset())
            pk_cb.on_value_change(lambda e: (None if pk_cb.value else _pk_reset()))

            def _sel_clear():
                sel_bw_lbl.set_text("select a band on the plot to measure it")
                sel_bw_lbl.classes(replace="text-xs font-mono text-gray-500")

            def _on_sel(e):
                try:
                    areas = (e.args or {}).get("areas") or []
                    rng = areas[0].get("coordRange") if areas else None
                    if not rng:            # no / cleared selection: reset the readout
                        _sel_clear(); return
                    lo, hi = rng
                    f0, f1, bw = min(lo, hi), max(lo, hi), abs(hi - lo)
                    bwt = f"{bw/1e6:.3f} MHz" if bw >= 1e6 else f"{bw/1e3:.1f} kHz"
                    sel_bw_lbl.set_text(
                        f"selection: {f0/1e6:.4f} to {f1/1e6:.4f} MHz, "
                        f"BW {bwt}, center {(f0+f1)/2/1e6:.4f} MHz")
                    sel_bw_lbl.classes(replace="text-xs font-mono text-blue-700")
                except Exception as ex:
                    sel_bw_lbl.set_text(f"selection parse: {ex}")
            psd_chart.on("chart:brushEnd", _on_sel)
            # the toolbox "clear" button (and tool deactivation) fire chart:brush
            # with empty areas: reset the readout so it doesn't linger
            psd_chart.on("chart:brush", _on_sel)

        # frozen header row for the scrollable tables (inline + "view all")
        ui.add_css("""
            .sticky-head thead tr th { position: sticky; top: 0; z-index: 2;
                background: #1a1e1a; }
        """)

        def _all_rows_dialog(title, cols, rows, key):
            with ui.dialog() as dlg, ui.card().classes("min-w-[28rem]"):
                ui.label(title).classes("text-base font-bold")
                ui.table(columns=cols, rows=rows, row_key=key) \
                    .props("dense").classes("w-full sticky-head").style(
                    "max-height:60vh; overflow:auto")
                ui.button("Close", on_click=dlg.close).props("flat")
            dlg.open()

        _DET_COLS = [
            {"name": "freq", "label": "freq (MHz)", "field": "freq"},
            {"name": "snr", "label": "SNR (dB)", "field": "snr"},
            {"name": "dur", "label": "dur (ms)", "field": "dur"}]
        _DEC_COLS = [
            {"name": "n", "label": "band", "field": "n"},
            {"name": "f", "label": "MHz", "field": "f"},
            {"name": "m", "label": "mod", "field": "m"},
            {"name": "msg", "label": "last msg", "field": "msg"}]

        # TX panels: IQ playback + jammer, side by side under the spectrum
        # The custom waveforms panel is hidden; the two remaining panels share the
        # full width (flex-1 each). To restore it, re-add the build_custom_waveform_panel
        # column below and the three will tile at a third each again.
        # (Record & replay lives in the engineering view, under the DSSS builder.)
        _get_txport = lambda: txinfo["port"]
        def _apply_retune(hz):
            # reflect a file-driven retune in the Center field
            cunit_in.value = "MHz"
            center_in.value = hz / 1e6

        async def _reinit_at(rate_hz):
            # re-init the SDR at a new rate (keeps the current tuning); setting
            # rate_in first refreshes the Sample-rate control at the top of page
            rate_in.value = int(rate_hz)
            return bool(await _start())
        with ui.row().classes("w-full gap-3 items-start flex-wrap"):
            with ui.column().classes("flex-1 min-w-[22rem]") as play_col:
                build_iq_playback_panel(_host, get_txport=_get_txport,
                                        on_retune=_apply_retune, on_reinit=_reinit_at)
            with ui.column().classes("flex-1 min-w-[22rem]") as jam_col:
                build_jammer_panel(_host, title="Jammer", get_txport=_get_txport)
            # Custom waveforms (hidden):
            # with ui.column().classes("flex-1 min-w-[22rem]"):
            #     build_custom_waveform_panel(_host, get_txport=_get_txport,
            #                                 on_retune=_apply_retune)

        # show only what the focused radio's role uses. The radio
        # refuses the rest anyway; this keeps the console to the job at hand.
        def _apply_role(r):
            r = r or _role.DEFAULT
            info = _role.ROLES.get(r, _role.ROLES[_role.DEFAULT])
            ctl_card.set_visibility(_role.shows(r, "tune"))
            tx_box.set_visibility(_role.shows(r, "tx"))
            play_col.set_visibility(_role.shows(r, "playback"))
            jam_col.set_visibility(_role.shows(r, "jammer"))
            det_row.set_visibility(_role.shows(r, "detections"))
            bp_link.set_visibility(_role.shows(r, "bitpipe") and r != "any")
            role_note.set_text("" if r == "any" else info["help"])

        role_seen = {"v": None}

        def _follow_role(cfg):
            r = (cfg or {}).get("role") or _role.DEFAULT
            if r != role_seen["v"]:
                role_seen["v"] = r
                _apply_role(r)

        bp_link = ui.link("Bitpipe link panel (opens /bitpipe)", "/bitpipe", new_tab=True) \
            .classes("text-sm")
        bp_link.set_visibility(False)

        # detections / decodes, side by side
        with ui.row().classes("w-full gap-3") as det_row:
            with ui.card().classes("flex-1"):
                with ui.row().classes("items-center justify-between w-full"):
                    ui.label("Detections").classes("text-sm font-bold")
                    det_more = ui.button("view all").props(
                        "flat dense size=sm no-caps").classes("text-xs")
                # keep the inline view short (newest 10) with a fixed height so a
                # busy band can't push the TX panels off-screen; "view all" opens
                # the full list in a dialog
                det_table = ui.table(columns=_DET_COLS, rows=[], row_key="freq") \
                    .props("dense hide-bottom").classes("w-full sticky-head").style(
                    "max-height:16rem; overflow:auto")

                def _show_all_dets():
                    host = _host()
                    if host is None:
                        return
                    rows = [
                        {"freq": f"{host.detections[i].freq_hz/1e6:.4f}",
                         "snr": f"{host.detections[i].snr_db:.1f}",
                         "dur": f"{host.detections[i].duration_ms:.0f}"}
                        for i in sorted(host.detections, reverse=True)]
                    _all_rows_dialog(f"All detections ({len(rows)})",
                                     _DET_COLS, rows, "freq")
                det_more.on_click(_show_all_dets)
                det_more.set_visibility(False)   # shown once >10 detections cached
            with ui.card().classes("flex-1"):
                with ui.row().classes("items-center justify-between w-full"):
                    ui.label("Decodes").classes("text-sm font-bold")
                    dec_more = ui.button("view all").props(
                        "flat dense size=sm no-caps").classes("text-xs")
                watch_table = ui.table(columns=_DEC_COLS, rows=[], row_key="n") \
                    .props("dense hide-bottom").classes("w-full sticky-head").style(
                    "max-height:16rem; overflow:auto")

                def _show_all_decodes():
                    rows = list(dec_all["rows"])
                    _all_rows_dialog(f"All decodes ({len(rows)})",
                                     _DEC_COLS, rows, "n")
                dec_more.on_click(_show_all_decodes)
                dec_more.set_visibility(False)   # shown once >10 decode bands
        dec_all = {"rows": []}   # full decode set for the "view all" dialog

        # logging (bottom): one button opens a popup to pick what to log
        ui.separator()
        with ui.row().classes("items-center gap-3"):
            log_btn = ui.button("Log information", icon="description") \
                .props("color=primary")
            log_state_lbl = ui.label("logging off").classes(
                "font-mono text-xs self-center text-gray-600")
        # activity log lives down here by the logging controls (out of the way
        # of the detections/decodes + TX panels up top)
        with ui.card().classes("w-full"):
            ui.label("Activity log").classes("text-sm font-bold")
            act_log = ui.log(max_lines=60).classes("w-full h-40 text-xs")
        with ui.dialog() as log_dlg, ui.card().classes("gap-2 min-w-[360px]"):
            ui.label("Log information").classes("text-lg font-bold")
            ui.label("Pick what to record, and where to store it.").classes(
                "text-sm text-gray-600")
            cb_det = ui.checkbox("Detections (freq, SNR, duration)")
            cb_pos = ui.checkbox("Aircraft / radio position (lat, lon, alt)")
            cb_health = ui.checkbox(
                "SDR health & status (over/underflows, timing ref, fps, uptime)") \
                .tooltip("The timing ref column is the GPS module's view. It does not "
                         "check a G40 external 10 MHz on the HMC path, see docs/LOGGING.md")
            cb_dec = ui.checkbox(
                "Decoded messages (time, band, frequency, modulation, message, frames)") \
                .tooltip("Always saved on this computer, the radio keeps no decode log")
            ui.separator()
            log_dst = ui.radio({"ground": "Local computer (this GCS)",
                                "g40": "On the radio (G40)"}, value="ground") \
                .props("inline")
            log_stat = ui.label("").classes("font-mono text-xs text-gray-600")
            with ui.row().classes("items-center gap-2 justify-end w-full"):
                log_start_btn = ui.button("Start logging").props("color=positive")
                log_stop_btn = ui.button("Stop logging").props("color=negative outline")
                ui.button("Close", on_click=log_dlg.close).props("flat")

        def _log_label_sync():
            """The logging line for the radio in focus, from the state that outlives
            this page. With no radio in focus, any radio still logging."""
            if sel["serial"] is not None:
                txt = ground_log.label_text(GLOGS.find(sel["serial"]),
                                            host_config(_host()))
            else:
                txt = "; ".join(f"{s}: {ground_log.label_text(r, None)}"
                                for s, r in GLOGS.logs.items() if r.on) or "logging off"
            log_state_lbl.set_text(txt)

        def _log_dialog_open():
            """Open the dialog showing what the radio in focus is really logging, which
            a fresh page's blank checkboxes would not."""
            rl = GLOGS.find(sel["serial"])
            lg = (host_config(_host()) or {}).get("log") or {}
            if rl is not None and rl.on:
                cb_det.value = rl.cats["detections"]
                cb_pos.value = rl.cats["position"]
                cb_health.value = rl.cats["health"]
                cb_dec.value = rl.cats["decodes"]
                log_dst.value = rl.dst
                log_stat.set_text(rl.note)
            elif any(lg.values()):             # started from another view or GUI
                cb_det.value = bool(lg.get("detections"))
                cb_pos.value = cb_health.value = bool(lg.get("telemetry"))
                log_dst.value = "g40"
                log_stat.set_text("radio reports: " + _vs.log_text({"log": lg}))
            else:
                log_stat.set_text(rl.note if rl is not None else "")
            log_dlg.open()

        async def _do_start(_e=None):
            await _logging_start()
            _log_label_sync()

        async def _do_stop(_e=None):
            await _logging_stop()
            _log_label_sync()
        log_start_btn.on_click(_do_start)
        log_stop_btn.on_click(_do_stop)
        log_btn.on_click(_log_dialog_open)

    def _apply_uhd_caps(caps):
        """Redraw the operator controls for a UHD-family radio (e.g. a B200mini)
        from its advertised capabilities. Additive: only reached when the
        focused radio's family is 'uhd'; the G40/X40 (epiq) branches below are
        untouched."""
        if caps.get("image_known") is False:        # X410 image with no table
            ui.notify(caps.get("note") or "unknown FPGA image", type="warning", timeout=10000)
        bwf = float(caps.get("usable_bw_factor", 0.8))
        srates = caps.get("sample_rates_hz") or list(G40_SAMPLE_RATES)
        _warn = float(caps.get("rate_warn_below_hz") or 0)
        ropts = {int(hz): f"{hz/1e6:g} MSps  (~{hz*bwf/1e6:.1f} MHz)"
                 + (f"  Warning: <{_warn/1e6:g} MSps not recommended" if hz < _warn else "")
                 for hz in srates}
        _dflt = int(caps.get("default_rate_hz") or 0)
        cur = int(rate_in.value) if (rate_in.value is not None
                                     and int(rate_in.value) in ropts) \
            else (_dflt if _dflt in ropts else list(ropts)[0])
        rate_in.set_options(ropts, value=cur)
        # front-end switches + half-duplex note (caps-driven, HackRF)
        fe = caps.get("fe_hooks") or {}
        fe_row.set_visibility(bool(fe))
        rxamp_cb.set_visibility(bool(fe.get("rx_amp")))
        txamp_cb.set_visibility(bool(fe.get("tx_amp")))
        bias_cb.set_visibility(bool(fe.get("bias_tee")))
        febw_in.set_visibility(bool(fe.get("rx_bandwidth")))
        hd_lbl.set_visibility(bool(caps.get("half_duplex")))
        # RX antenna is a real choice on this radio (RX2 or TX/RX): enable it;
        # TX stays the single TX/RX port.
        rxa = caps.get("rx_antennas") or ["RX2"]
        txa = caps.get("tx_antennas") or ["TX/RX"]
        rxv = rxport_in.value if rxport_in.value in rxa else (
            "RX2" if "RX2" in rxa else rxa[0])
        rxport_in.set_options({a: a for a in rxa}, value=rxv)
        rxport_in.props(remove="disable")
        txport_in.set_options({a: a for a in txa}, value=txa[0])
        txport_in.props("disable")
        # gains: precise dB number inputs (dropdown grid is lossy); show them,
        # hide the epiq selects, and clamp range/step to caps.
        rg = caps.get("rx_gain") or {}
        tg = caps.get("tx_gain") or {}
        _clamp_num(rxgain_num, rg)
        _clamp_num(txgain_num, tg)
        # The radio's supported values only; the number boxes
        # stay hidden mirrors of the selection.
        _fill_gain_select._kind, _fill_gain_select._caps = caps.get("kind"), caps
        _fill_gain_select(rxgain_sel, rxgain_num, caps.get("rx_gain_values"), rg)
        _fill_gain_select(txgain_sel, txgain_num, caps.get("tx_gain_values"), tg)
        rxgain_in.set_visibility(False)
        rxgain_num.set_visibility(False)
        rxgain_sel.set_visibility(True)
        txatten_in.set_visibility(False)
        txgain_num.set_visibility(False)
        txgain_sel.set_visibility(True)
        _tl = 'label="TX atten (dB)"' if tg.get("mode") == "atten" else 'label="TX gain (dB)"'
        txgain_num.props(_tl)
        txgain_sel.props(_tl)
        # reference split: clk_in = 10 MHz freq ref; ppsref_in = 1 PPS time ref
        cs = caps.get("clock_sources") or ["internal"]
        clk_in.set_options(cs, value="internal" if "internal" in cs else cs[0])
        clk_in.props('label="10 MHz ref (ext. auto-detected)"'
                     if caps.get("ext_ref_auto") else 'label="10 MHz ref"')
        # 1 PPS time reference: only radios that have one get the select
        ts = caps.get("time_sources")
        if ts is None:
            ts = ["none", "internal", "external"]
        if len(ts) > 1:
            ppsref_in.set_options(ts, value="internal" if "internal" in ts else ts[0])
        ppsref_in.set_visibility(len(ts) > 1)
        uhd_qual_row.set_visibility(bool(caps.get("quality_hooks")))
        _hr["kind"], _hr["bumped"] = caps.get("kind"), None
        # a radio that is already running keeps the gain it runs with; the
        # note still shows
        _b200_rate_gain(move_gain=not _inited())

    def _clamp_num(el, rng):
        """Clamp a ui.number's value into a caps {min,max} range (no-op if the
        value is unset or already inside)."""
        try:
            lo, hi = float(rng.get("min", 0)), float(rng.get("max", 1e9))
            v = float(el.value) if el.value is not None else None
        except (TypeError, ValueError):
            return
        if v is not None and not (lo <= v <= hi):
            el.set_value(max(lo, min(hi, v)))

    def _set_clock_list(kind):
        """The clock choices this kind can run: five on a G40, three on an X40
        (no FPGA GPSDO there; x40_gpsdo is its own)."""
        cs = _g40_clock.labels(kind)
        clk_in.set_options(cs, value=clk_in.value if clk_in.value in cs else "internal")

    def _restore_epiq_controls():
        """Reverse the UHD widget swap when focus returns to a G40/X40 radio:
        show the epiq gain selects, hide the dB number inputs and the 1 PPS
        split, and restore the single clock label + quality row hidden."""
        rxgain_num.set_visibility(False)
        rxgain_sel.set_visibility(False)
        rxgain_in.set_visibility(True)
        txgain_num.set_visibility(False)
        txgain_sel.set_visibility(False)
        txatten_in.set_visibility(True)
        ppsref_in.set_visibility(False)
        clk_in.props('label="Timing / ref clock"')
        uhd_qual_row.set_visibility(False)
        fe_row.set_visibility(False)
        hd_lbl.set_visibility(False)
        sensor_lbl.set_visibility(False)
        _hr["kind"], _hr["bumped"] = None, None
        hr_note.set_visibility(False)

    def _sync_from_radio(host):
        """Copy the radio's real settings (get_config) into the TUNING /
        RECEIVE / TRANSMIT controls, the bias row, the TX waveform readout and the
        logging line. Focused / just-edited fields are left alone (EditGuard) and
        flagged 'not applied yet' while they differ from the radio."""
        cfg = host_config(host)
        if cfg is None:
            return
        fam = caps_family(host.serial)
        svc = _vs.op_fields(cfg)
        # caps-driven widgets: centre, clock and the sample rate. Without the rate a
        # B200 card can show 30.72 MSps while the radio runs at 2 MSps, and START
        # then re-initializes it at 30.72 in the middle of a mission
        if fam in ("uhd", "pluto", "soapy"):
            svc = {k: v for k, v in svc.items() if k in ("center_mhz", "clk", "rate")}
        svc_now.clear()
        svc_now.update(svc)
        gps_row.sync(cfg.get("ant_bias"))
        txwf_lbl.set_text(_vs.tx_wf_text(cfg))
        if busy["v"]:
            return
        fac = _FUNIT.get(cunit_in.value, 1e6)
        for k, el in _OPW.items():
            if k not in svc or not vguard.may_apply(k, svc[k]):
                continue
            v = svc[k]
            if k == "center_mhz":
                v = round(float(cfg["center_hz"]) / fac, 6)
            elif k == "rate":
                keys = list(rate_in.options.keys()) if isinstance(rate_in.options, dict) else []
                v = _vs.nearest(keys, v)
                if v is None:
                    continue
            _vs_set(el, v)
        wv = {k: el.value for k, el in _OPW.items()}
        if wv.get("center_mhz") is not None:
            wv["center_mhz"] = float(wv["center_mhz"]) * fac / 1e6
        pend = _vs.pending_labels(vguard, wv, svc, _OPN)
        pend_lbl.set_text(("not applied yet: " + ", ".join(pend)
                           + ". Press START (centre/gain/atten apply on Enter / pick)")
                          if pend else "")
        pend_lbl.set_visibility(bool(pend))

    def refresh():
        hosts = MGR.hosts
        count_lbl.set_text(f"{len(hosts)} radio(s)")
        if sel["serial"] is None and hosts:
            sel["serial"] = sorted(hosts)[0]
        _log_label_sync()

        # selectable radio strip: built once per radio and updated in place so the
        # editable name field on each card survives the telemetry refresh
        cur = set(hosts)
        if not cur and empty_lbl["el"] is None:
            with strip:
                empty_lbl["el"] = ui.label("listening for radios...").classes(
                    "text-lg text-gray-500 italic")
        if cur and empty_lbl["el"] is not None:
            empty_lbl["el"].delete(); empty_lbl["el"] = None
        for serial in list(cards):          # drop cards for radios that are gone
            if serial not in cur:
                cards[serial]["card"].delete(); del cards[serial]
        for serial in sorted(cur):          # add cards for new radios
            if serial not in cards:
                cards[serial] = _build_card(serial, hosts[serial])
        for serial, h in hosts.items():     # refresh every card in place
            _update_card(cards[serial], serial, h)

        host = _host()
        if host is None:
            focus_lbl.set_text("no radio selected")
            tx_alarm_lbl.set_text("")
            focus_serial_lbl.set_text("")
            _follow_role(None)
            return
        # Probe the focused radio once; until caps arrive (or if the
        # radio has none) the family stays epiq = the legacy G40/X40 path, so
        # existing radios are unaffected. When caps land, on_done re-renders.
        if host.serial not in _CAPS_CACHE:
            asyncio.create_task(_fetch_caps(host, on_done=refresh))
        _fam = caps_family(host.serial)
        # retune the rate / port / gain dropdowns to the focused radio's kind.
        # "pluto" is caps-driven like "uhd" (rates/antennas/gains come straight
        # from the advertised caps), so it shares the _apply_uhd_caps path:
        # otherwise it would fall through to the G40 defaults (wrong rate dropdown).
        _k = ("uhd" if _fam in ("uhd", "pluto", "soapy")
              else "x40" if getattr(host, "host_type", None) == int(HostType.X40_TX)
              else "g40")
        # key on (serial, kind) so the controls re-apply both when focus moves
        # to a different radio and when a radio's caps arrive and flip its kind
        _key = (host.serial, _k, (_CAPS_CACHE.get(host.serial) or {}).get("fpga_image"),
                (_CAPS_CACHE.get(host.serial) or {}).get("_fetched"))
        if _key != _cfgkind["v"]:
            _cfgkind["v"] = _key
            if _k == "uhd":
                _apply_uhd_caps(_CAPS_CACHE.get(host.serial) or {})
            elif _k == "x40":
                rate_in.set_options(_RATES_X40, value=50_000_000)
                _set_clock_list("x40")
                rxport_in.set_options(_RXPORTS_X40, value="RXB1")
                txport_in.set_options(_TXPORTS_X40, value="TXA1")
                rxgain_in.set_options(_GAIN_X40, value="245")
                rxport_in.props(remove="disable")
                txport_in.props(remove="disable")
                _restore_epiq_controls()
            else:
                rate_in.set_options(_RATES_G40, value=int(rate_in.value)
                                    if int(rate_in.value or 0) in _RATES_G40 else 30_720_000)
                _set_clock_list("g40")
                rxport_in.set_options(_PORTS_G40, value="TRx1A")
                txport_in.set_options(_PORTS_G40, value="TRx1B")
                rxgain_in.set_options(_GAIN_G40, value="255")
                rxport_in.props(remove="disable")
                txport_in.props(remove="disable")
                _restore_epiq_controls()
        tx_on = (host.status is not None and
                 host.status.flags & proto.Status.FLAG_TX_ACTIVE)
        tx_alarm_lbl.set_text("TRANSMITTING" if tx_on else "")
        focus_lbl.set_text(_name(host.serial))
        _follow_role(host_config(host))
        # show the real unit serial in small print when a custom name is set
        focus_serial_lbl.set_text(host.serial if labels.get(host.serial) else "")
        st = host.status
        # one-time sync: when a radio is already initialized (e.g. after a page
        # reload, or when focus switches to it), pull its live center/rate/gain
        # into the TUNING controls so they show the real setting, not defaults.
        # (VIEW_SYNC replaces this with the full get_config sync below; kept for an
        # older service that has no get_config)
        if st is not None and not busy["v"] and synced["serial"] != host.serial \
                and getattr(host, "config_supported", None) is False \
                and st.radio_state != int(RadioState.UNINIT) and st.sample_rate_hz > 0:
            cunit_in.value = "MHz"
            center_in.value = round(st.center_hz / 1e6, 6)
            if caps_family(host.serial) in ("uhd", "pluto", "soapy"):
                # UHD/Pluto: snap to the radio's own advertised rates; rx_gain_idx
                # is not an epiq index here, so leave the dB gain dropdown alone.
                _rk = [int(hz) for hz in
                       ((_CAPS_CACHE.get(host.serial) or {}).get("sample_rates_hz") or [])]
                if _rk:
                    rate_in.value = min(_rk, key=lambda hz: abs(hz - st.sample_rate_hz))
            else:
                _isx40 = getattr(host, "host_type", None) == int(HostType.X40_TX)
                _rk = list(_RATES_X40 if _isx40 else _RATES_G40)
                if _rk:
                    rate_in.value = min(_rk, key=lambda hz: abs(hz - st.sample_rate_hz))
                _gk = str(int(st.rx_gain_idx))
                if _gk in (_GAIN_X40 if _isx40 else _GAIN_G40):
                    rxgain_in.value = _gk
            synced["serial"] = host.serial
        _sync_from_radio(host)              # VIEW_SYNC
        _follow_ports(host, _cfgkind["v"])  # PORT_MAP
        _refok = bool(st is not None and (st.flags & proto.Status.FLAG_REF_LOCKED))
        # colour the Timing/ref clock field: green when the reference is good
        # (GPSDO locked, or external confirmed), amber while a GPSDO acquires or
        # an external reference is not confirmed, neutral for internal
        _rs = ref_state(clk_in.value, _refok, host_config(host))
        if _rs != refcur["state"]:
            refcur["state"] = _rs
            clk_in.props(remove="bg-color")
            if _rs == "locked":
                clk_in.props("bg-color=green-2")
            elif _rs in ("acquiring", "no_fix"):
                clk_in.props("bg-color=amber-2")
            elif _rs == "lost":
                clk_in.props("bg-color=red-3")
        # RX ADC overload: colour the gain field red and warn (the service
        # auto-reduces gain; st.rx_gain_idx carries the live, backed-off value)
        _ovl = bool(st is not None and (st.flags & proto.Status.FLAG_RX_OVERLOAD))
        if _ovl != ovlcur["v"]:
            ovlcur["v"] = _ovl
            rxgain_in.props(remove="bg-color")
            if _ovl:
                rxgain_in.props("bg-color=red-4")
        # RX stale: the card is replaying old buffers (wedged FIFO): the PSD is
        # frozen and detection is paused on the radio. Distinct from PSD age
        # staleness (no telemetry): here frames arrive but the data isn't new.
        _stale = bool(st is not None and (st.flags & proto.Status.FLAG_RX_STALE))
        if st is not None and not busy["v"]:
            txt, _c = _STATE_TXT.get(st.radio_state, ("?", ""))
            ref = ref_words(clk_in.value, _refok, host_config(host))
            ovl = ""
            if _ovl:
                live = _GAIN_G40.get(str(int(st.rx_gain_idx)),
                                     _GAIN_X40.get(str(int(st.rx_gain_idx)),
                                                   f"idx {st.rx_gain_idx}"))
                ovl = f", RX OVERLOAD: auto-reduced to {live} (lower RX gain / raise TX atten)"
            stale = (", RX STALE: stream wedged (PSD frozen, detection paused); "
                     "re-init or power-cycle to clear" if _stale else "")
            _tj = _ht.from_wire(st.temp_c_x10)
            tmp = ("" if _tj is None else
                   f", chip {_tj:.0f} °C" + {"ok": "", "warm": " WARM",
                                            "hot": " HOT, near throttling"}[_ht.level(_tj)])
            _cts = _ht.card_temps_from_config(host_config(host))
            if _cts:
                _cw = _ht.worst(_ht.card_level(t) for _c, t in _cts)
                tmp += (", cards " + "/".join(f"{t:.0f}" for _c, t in _cts) + " °C"
                        + {"ok": "", "warm": " WARM", "hot": " HOT, near card limit"}[_cw])
            status_lbl.set_text(
                f"{'CONNECTED' if host.connected else 'STALE'}, {txt}, "
                f"frames {st.rx_frames}, fps {st.fps}, "
                f"over/under {st.overruns}/{st.underruns}, up {st.uptime_s} s{tmp}{ref}{ovl}{stale}")
        view = host.psd
        if view is not None:
            f, pw = view.freqs_hz, view.psd_db
            psd_chart.options["series"][0]["data"] = [
                [float(a), float(b)] for a, b in zip(f, pw)]
            # peak-hold overlay: max per bin since enabled/cleared. Reset when the
            # frequency axis moves (retune/span change) so old peaks don't smear.
            if pk_cb.value:
                pa = np.asarray(pw, dtype=float)
                key = (round(view.frame.center_hz), round(view.frame.span_hz), len(pa))
                if pkhold["acc"] is None or pkhold.get("key") != key:
                    pkhold["acc"] = pa.copy(); pkhold["key"] = key
                else:
                    np.maximum(pkhold["acc"], pa, out=pkhold["acc"])
                psd_chart.options["series"][1]["data"] = [
                    [float(a), float(b)] for a, b in zip(f, pkhold["acc"])]
            _set_psd_y(psd_chart, pkhold["acc"] if (pk_cb.value and pkhold["acc"] is not None
                                                    and len(pkhold["acc"]) == len(pw))
                       else pw)
            psd_chart.update()
            age = time.monotonic() - view.t_arrival
            _rec = _rec_state.text(host.serial)
            psd_lbl.set_text(
                (f"● {_rec}  |  " if _rec else "")
                + f"center {view.frame.center_hz/1e6:.4f} MHz | "
                f"span {view.frame.span_hz/1e3:.0f} kHz | {age:.1f} s ago"
                + ("   RX STALE (wedged, data not updating)" if _stale else "")
                + ("   STALE" if age > 2.5 else ""))
        _ndet = len(host.detections)
        det_table.rows = [
            {"freq": f"{host.detections[i].freq_hz/1e6:.4f}",
             "snr": f"{host.detections[i].snr_db:.1f}",
             "dur": f"{host.detections[i].duration_ms:.0f}"}
            for i in sorted(host.detections, reverse=True)[:10]]
        det_table.update()
        # surface the full count on the expander; hide it when nothing overflows
        det_more.set_text(f"view all ({_ndet})" if _ndet > 10 else "view all")
        det_more.set_visibility(_ndet > 10)
        _decs = [
            {"n": b.get("n", ""), "f": f"{b.get('f', 0)/1e6:.3f}",
             "m": b.get("m", ""), "msg": b.get("msg", "")}
            for b in (getattr(host, "watch_bands", []) or [])]
        dec_all["rows"] = _decs
        watch_table.rows = _decs[:10]
        watch_table.update()
        dec_more.set_text(f"view all ({len(_decs)})" if len(_decs) > 10 else "view all")
        dec_more.set_visibility(len(_decs) > 10)

        # per-radio activity log: the global link log filtered to this serial
        if logst["serial"] != sel["serial"]:
            act_log.clear()
            logst["serial"] = sel["serial"]
            logst["n"] = 0
            for en, ets, emsg in list(MGR.events):
                logst["n"] = max(logst["n"], en)
                if sel["serial"] and sel["serial"] in emsg:
                    act_log.push(f"{ets} {emsg}")
        else:
            for en, ets, emsg in list(MGR.events):
                if en > logst["n"]:
                    logst["n"] = en
                    if sel["serial"] and sel["serial"] in emsg:
                        act_log.push(f"{ets} {emsg}")
    ui.timer(0.5, refresh)

    async def _sensor_tick():
        # poll UHD sensor telemetry only while a UHD radio is focused + up;
        # epiq radios (G40/X40) keep the row hidden and never see a sensors cmd.
        # Fully guarded: the timer must never raise (a throw here floods the log
        # and can disrupt the shared refresh / caps rendering).
        try:
            host = _host()
            if host is None or caps_family(host.serial) not in ("uhd", "soapy"):
                sensor_lbl.set_visibility(False); return
            st = host.status
            if st is None or st.radio_state == int(RadioState.UNINIT):
                sensor_lbl.set_visibility(False); return
            try:
                ack = await host.cmd("sensors", {}, timeout=3.0)
            except Exception:
                return
            d = getattr(ack, "result", None) or {}
            txt, col = _fmt_sensors(d, ext_ref=(clk_in.value == "external"))
            sensor_lbl.set_text(txt)
            sensor_lbl.style(replace=f"color: {col}")
            sensor_lbl.set_visibility(True)
        except Exception:
            # client/slot gone or element deleted: swallow so the timer lives
            return
    ui.timer(3.0, _sensor_tick)


# Secure messaging (engineering view). Keys live on this
# host (the ground station): the panel shows the ground context (key ID / KCV,
# AEAD + nonce, verdict counters from opening the radio's forwarded sealed frames)
# plus the radio's own brief from get_config (flag, sealed frames seen /
# forwarded, secure plan entries). The radio has no keys and no enable switch.
def _build_secure_panel(get_host):
    from dpmg.secure import secure_ground as _sg
    with ui.card().classes("w-full") as card:
        with ui.row().classes("items-center w-full justify-between"):
            ui.label("Secure messaging (AES-256-GCM-SIV), keys on this ground station").classes("text-sm font-bold")
            state_lbl = ui.label("-").classes("font-mono text-xs")
        with ui.row().classes("items-center gap-4 flex-wrap"):
            key_lbl = ui.label("key: -").classes("font-mono text-xs")
            prof_lbl = ui.label("").classes("font-mono text-xs")
        with ui.row().classes("items-center gap-4 flex-wrap"):
            cnt_lbl = ui.label("auth ok 0, auth FAIL 0, replay 0, stale 0").classes("font-mono text-xs")
            radio_lbl = ui.label("radio: -").classes("font-mono text-xs")
        last_lbl = ui.label("").classes("font-mono text-xs text-green-700")
        alarm_lbl = ui.label("").classes("text-xs text-red-600")
        with ui.row().classes("items-center gap-2"):
            rl_btn = ui.button("Reload keys (ground)", icon="refresh").props("dense outline size=sm")
            det_btn = ui.button("Details", icon="info").props("dense outline size=sm")

    def _reload():
        from dpmg.secure import api as _sf
        _sf.set_context(None)
        ctx = _sf.get_context()
        if ctx is None:
            ui.notify("key reload failed: " + _sf.last_error(), type="negative")
        else:
            ui.notify(f"keys reloaded: {ctx.store.status()['key_id']} ({_sf.AEAD_NAME})", type="positive")

    async def _details():
        host = get_host()
        g = _sg.ground().status()
        radio = None
        if host is not None:
            try:
                ack = await host.cmd("secure_status", {}, timeout=8.0)
                radio = ack.result if ack.status == 0 else {"err": (ack.result or {}).get("err")}
            except TimeoutError:
                radio = {"err": "timeout"}
        with ui.dialog() as dlg, ui.card().classes("w-[44rem] max-w-full"):
            ui.label("Secure messaging status").classes("text-lg font-bold")
            ui.label("Ground station (keys, AEAD, verdicts):").classes("text-xs font-bold")
            ui.label(json.dumps(g, indent=1, default=str)).classes("font-mono text-xs whitespace-pre-wrap")
            ui.label("Radio (no keys; sealed frames forwarded):").classes("text-xs font-bold")
            ui.label(json.dumps(radio, indent=1, default=str)).classes("font-mono text-xs whitespace-pre-wrap")
            ui.button("Close", on_click=dlg.close).props("flat")
        dlg.open()

    rl_btn.on_click(_reload)
    det_btn.on_click(_details)

    def _refresh():
        host = get_host()
        g = _sg.ground()
        if host is not None:
            try:
                g.poll(host)
            except Exception:                                      # noqa: BLE001
                pass
        st = g.status()
        if not st.get("flag"):
            state_lbl.set_text("OFF (DPMG_SECURE_MSG unset on this ground station)")
            state_lbl.classes(replace="font-mono text-xs text-gray-500")
            key_lbl.set_text("key: -"); prof_lbl.set_text(""); alarm_lbl.set_text("")
            det_btn.disable(); rl_btn.disable()
            return
        det_btn.enable(); rl_btn.enable()
        if st.get("active"):
            state_lbl.set_text(f"KEYS LOADED  {st.get('aead', 'AES-256-GCM-SIV')}")
            state_lbl.classes(replace="font-mono text-xs text-green-700 font-bold")
            k = st.get("keys") or {}
            key_lbl.set_text(f"key {k.get('key_id', '-')}  kcv {k.get('kcv', '-')}  suite {st.get('suite', '-')}"
                             f"  net {k.get('net_id', '-')} sender {k.get('sender_id', '-')}")
            prof_lbl.set_text(f"nonce counter+slot (persisted)  (+{st.get('overhead_bits')} bits/frame, "
                              f"+{st.get('overhead_bits_long')} with the long header)")
            c = st.get("counters") or {}
            cnt_lbl.set_text(f"auth ok {c.get('AUTH_OK', 0)}, auth FAIL {c.get('AUTH_FAIL', 0)}, "
                             f"replay {c.get('REPLAY', 0) + c.get('REPLAY_GEN', 0)}, stale {c.get('STALE', 0)}, "
                             f"sealed {c.get('sealed', 0)}, refused {c.get('seal_refused', 0)}, "
                             f"opened {st.get('opened', 0)}, dropped {st.get('dropped', 0)}")
            al = st.get("alarms") or []
            alarm_lbl.set_text(al[-1] if al else "")
        else:
            state_lbl.set_text("FAULT: " + str(st.get("err", "no keys"))[:90])
            state_lbl.classes(replace="font-mono text-xs text-red-600 font-bold")
            key_lbl.set_text("key: -"); prof_lbl.set_text("")
        cfg = host_config(host)
        sec = (cfg or {}).get("secure") if cfg else None
        if not sec:
            radio_lbl.set_text("radio: no get_config" if host else "radio: no host")
        elif not sec.get("flag"):
            radio_lbl.set_text("radio: DPMG_SECURE_MSG off (sealed frames not parsed)"
                               + ("  FAULT: " + sec["err"] if sec.get("err") else ""))
        else:
            radio_lbl.set_text(f"radio: forwarding, sealed seen {sec.get('sealed_seen', 0)} fwd "
                               f"{sec.get('sealed_forwarded', 0)}, secure plan entries {sec.get('plan_secure_entries', 0)}")
        rec = g.recent(1)
        if rec:
            r = rec[-1]
            last_lbl.set_text(f"last: {r['band']} {r['verdict']} {r['msg']!r}" if r["msg"]
                              else f"last: {r['band']} {r['verdict']} {r.get('detail', '')}")
            last_lbl.classes(replace="font-mono text-xs " + ("text-green-700" if r["msg"] else "text-red-600"))

    ui.timer(1.0, _refresh)
    return card


def build_remote_sdr():
    sel = {"serial": None}          # per-client selection
    _eng_cfg = {"v": None}          # last (serial, family) the control set drew

    with ui.expansion("Remote Radios (Ground Link)", icon="settings_remote",
                      value=True).classes("w-full border rounded"):
        ui.label("Hosts are discovered by ANNOUNCE broadcast; identity is "
                 "the radio serial, never an address.") \
            .classes("text-xs text-gray-600")

        # Connect by IP when broadcast discovery is blocked
        # (firewalled/routed away). The GUI unicasts HELLO, so the reply +
        # telemetry ride the allowed established flow: no broadcast needed.
        with ui.row().classes("items-center gap-2"):
            ip_in = ui.input("Radio IP", placeholder="192.168.0.22") \
                .props("dense outlined").classes("w-44")
            port_in = ui.number("cmd port", value=47810, format="%d") \
                .props("dense outlined").classes("w-28")
            add_lbl = ui.label("").classes("text-xs text-gray-600 self-center")

            def _connect_ip():
                ip = (ip_in.value or "").strip()
                if not ip:
                    ui.notify("enter the radio IP", type="warning"); return
                msg = MGR.add_manual_host(ip, int(port_in.value or 47810), save=True)
                add_lbl.set_text(msg)
                ui.notify(msg, type="info")
                _draw_saved()
            ui.button("Connect by IP", icon="lan", on_click=_connect_ip) \
                .props("dense color=primary")

        # Saved addresses: dialled at every GUI start and every 2 s while
        # nothing answers, so a radio appears by itself once its service starts.
        saved_row = ui.row().classes("items-center gap-2 flex-wrap")
        _saved_sig = {"v": None}

        def _forget(ip, port):
            msg = MGR.forget_host(ip, port)
            ui.notify(msg, type="info")
            _draw_saved()

        def _draw_saved():
            rows = MGR.saved_status()
            if rows == _saved_sig["v"]:
                return
            _saved_sig["v"] = rows
            saved_row.clear()
            with saved_row:
                if not MGR.saved_path:
                    return
                ui.label("Saved:" if rows else
                         "Saved: none (Connect by IP adds the address here)") \
                    .classes("text-xs text-gray-600")
                for ip, port, serial in rows:
                    txt = f"{ip}:{port} " + (serial or "waiting for its service")
                    ui.chip(txt, removable=True, color="green-1" if serial else "grey-3",
                            on_value_change=lambda e, i=ip, p=port:
                                None if e.value else _forget(i, p)) \
                        .props("dense").classes("text-xs") \
                        .tooltip("connected" if serial else
                                 "nothing answers yet; it appears when its service starts. "
                                 "The x removes it from the saved list.")
        _draw_saved()
        ui.timer(2.0, _draw_saved)

        hosts_row = ui.row().classes("w-full gap-3")
        # aircraft position lives with the radios: multiple SDRs can be in
        # different aircraft or ground sites, so the position panel belongs by
        # the host list, not at the top of the tab.
        _build_acft_mcast_panel()
        _build_secure_panel(lambda: MGR.hosts.get(sel["serial"]))
        _rfu.build_fpga_panel(lambda: MGR.hosts.get(sel["serial"]),
                              lambda: MGR.hosts, host_config)
        _ppu.set_event_log(MGR._evt)                                    # PORT_MAP
        _ppu.build_lockout_panel(lambda: MGR.hosts.get(sel["serial"]), host_config,
                                 by=f"{MGR.gui_id} engineering view",
                                 caps_of=lambda h: _CAPS_CACHE.get(h.serial),
                                 kind_of=lambda h, cfg: radio_kind(h.serial, cfg))

        # control card
        with ui.card().classes("w-full"):
            with ui.row().classes("items-center gap-3"):
                host_sel = ui.select([], label="Host (serial)") \
                    .props("dense outlined").classes("w-64")
                conn_lbl = ui.label("no host").classes("font-mono text-sm")
                # same role as the operator console; panels here stay
                # (engineering view), the radio refuses what the role does not use
                eng_role = _rfu.RolePicker(lambda: MGR.hosts.get(sel["serial"]), "engineering")
            # The radio cards at the top pick the radio (as in the operator
            # console); the Host dropdown stays only as hidden selection state
            host_sel.set_visibility(False)
            conn_lbl.set_visibility(False)
            # RX config row
            # G40 host = two NV100 cards, two TRx ports each (4 RX + 4 TX
            # total): card0 TRx1A/2A, card1 TRx1B/2B. Handles are always
            # RXA1/RXA2 (TXA1/TXA2) on each card. Hardware rules encoded by
            # _update_constraints(): same card, shared sample rate (one
            # RFIC clock, LOs independent); same TRx port, one SMA, RX+TX
            # can never run simultaneously; different card, independent.
            # Operating-mode presets. Each is a tested recipe that keeps the
            # operator clear of the RFIC traps (TRx2 TX must use TxB1 + dual mode;
            # TxA2 is a dead end; A-side RX+TX can't be simultaneous). tx_hdl
            # None = RX-only. chan_mode 'dual' only for TxB1 (J2/TRx2).
            _MODES = {
                "RX only":                     dict(tx_hdl=None,  chan_mode="single", note="receive only"),
                "TX only: TRx1 (TxA1)":       dict(tx_hdl="TXA1", chan_mode="single", note="single-mode TX on TRx1"),
                "TX only: TRx2 (TxB1)":       dict(tx_hdl="TXB1", chan_mode="dual",   note="dual-mode TX on TRx2 (via TxB1)"),
                "Transceive FDD (RX TRx1 + TX TRx2)": dict(tx_hdl="TXB1", chan_mode="dual", note="full duplex, one card: RX TRx1A + TX TRx2A"),
                "Transceive TDD same-port (TRx1)":    dict(tx_hdl="TXA1", chan_mode="single", note="half-duplex on one antenna; RX pauses during TX"),
                # Same init the operator console sends for a cross-card
                # pair (RX on one card, TX on the other). The TX port list offers
                # TRx1 / TRx2 of the TX card.
                "Transceive cross-card (RX one card, TX the other)": dict(
                    tx_hdl="CROSS", chan_mode="single",
                    note="full duplex across the two cards: independent LOs, best isolation"),
            }
            with ui.row().classes("items-center gap-3 flex-wrap"):
                ui.label("Mode").classes("font-bold w-12 self-center")
                mode_in = ui.select(list(_MODES.keys()), value="RX only",
                                    label="Operating mode (proven)") \
                    .props("dense outlined").classes("w-96")
                mode_hint = ui.label("").classes("text-xs text-gray-600 self-center")

            # ref clock is a whole-card setting (RX+TX share the RFIC clock), so it
            # lives by Mode, not the TX row. gps_lbl polls GPSDO lock / 1PPS live.
            with ui.row().classes("items-center gap-3 flex-wrap"):
                ui.label("Clock").classes("font-bold w-12 self-center")
                clk_in = ui.select(_g40_clock.labels("g40"),
                                   value="internal", label="Reference clock (RX + TX)") \
                    .props("dense outlined").classes("w-64")
                # UHD only: 1 PPS time ref, independent of the 10 MHz freq ref
                # (clk_in). Hidden for epiq, where clk_in is the single knob.
                ppsref_in = ui.select(["none", "internal", "external"],
                                      value="internal", label="1 PPS (time ref)") \
                    .props("dense outlined").classes("w-44")
                ppsref_in.set_visibility(False)
                gps_lbl = ui.label("ref status: init a radio to read") \
                    .classes("font-mono text-xs self-center")
            # Antenna bias checkbox (GPSDO clock only); fed by _gps_refresh
            gps_ant = GpsAntennaRow(lambda: _host(), lambda: clk_in.value)

            with ui.row().classes("items-center gap-3 flex-wrap"):
                ui.label("RX").classes("font-bold w-8 self-center")
                kind_in = ui.select(["g40", "x40", "mock"], value="g40",
                                    label="Backend kind") \
                    .props("dense outlined").classes("w-32")
                card_in = ui.select({0: "card0 (NV100-1)", 1: "card1 (NV100-2)"},
                                    value=0, label="RX card") \
                    .props("dense outlined").classes("w-40")
                rxant_in = ui.select({"RXA1": "RXA1: TRx1A"},
                                     value="RXA1", label="RX port") \
                    .props("dense outlined").classes("w-40")
                _ppu.hook_tips(rxant_in)
                # supported sample rates per backend, shared with the operator
                # view (G40 spans the card's range; X40 floors at 50 Msps). The
                # card achieves the nearest supported rate and reports it back.
                _RATE_OPTS = {
                    "g40": G40_SAMPLE_RATES,
                    "x40": X40_SAMPLE_RATES,
                    "mock": {**G40_SAMPLE_RATES, **X40_SAMPLE_RATES},
                }
                rate_in = ui.select(_RATE_OPTS["g40"], value=30_720_000,
                                    label="Sample rate") \
                    .props("dense outlined").classes("w-36")

                def _sync_rate_opts():
                    opts = _RATE_OPTS.get(kind_in.value, _RATE_OPTS["g40"])
                    cur = int(rate_in.value) if rate_in.value else 0
                    rate_in.set_options(
                        opts, value=cur if cur in opts else list(opts)[-1])
                kind_in.on_value_change(lambda e: _sync_rate_opts())
                cf_in = ui.input("RX center (Hz)", value="425025000") \
                    .props("dense outlined").classes("w-36")
                # hardware-legal indices only (G40 187-255), dB labeled
                gain_in = ui.select(
                    {str(i): f"{i}  (+{(i - 187) * 0.5:.1f} dB)"
                     for i in range(187, 256)},
                    value="200", label="RX gain idx (dB re min)") \
                    .props("dense outlined").classes("w-44")
                # UHD only: precise dB gain (the index grid is epiq-only);
                # shown instead of gain_in when family=="uhd".
                rxgain_num = ui.number("RX gain (dB)", value=40, format="%g") \
                    .props("dense outlined").classes("w-32")
                rxgain_num.set_visibility(False)
                rxgain_sel = ui.select({}, label="RX gain (dB)") \
                    .props("dense outlined options-dense").classes("w-64")
                rxgain_sel.set_visibility(False)
                rxgain_sel.on_value_change(
                    lambda e: rxgain_num.set_value(float(e.value)) if e.value is not None else None)
                eng_hr_note = ui.label(B200_HIGH_RATE_NOTE) \
                    .classes("text-xs text-amber-700 w-64")
                eng_hr_note.set_visibility(False)

                # B200 at 50 MSps and up: RX gain to 60 dB (b200_rate_gain), on a
                # rate pick only; nothing is sent until SET GAIN / INITIALIZE
                eng_hr = {"bumped": None}

                def _eng_b200_rate_gain(_e=None, move_gain=True):
                    new, bumped, show = b200_rate_gain(
                        kind_in.value, rate_in.value, rxgain_num.value, eng_hr["bumped"])
                    eng_hr_note.set_visibility(show)
                    if not move_gain:
                        return
                    eng_hr["bumped"] = bumped
                    if new is None:
                        return
                    key = _nearest_gain_key(rxgain_sel, new)
                    rxgain_num.set_value(float(key) if key is not None else new)
                    if key is not None:
                        rxgain_sel.set_value(key)
                rate_in.on_value_change(_eng_b200_rate_gain)

            # UHD quality hooks + bandwidth (family "uhd" only)
            #      front-end corrections + explicit RX/TX analog bandwidth a UHD
            #      radio exposes; the row shows only when caps advertise them.
            #      Bandwidth lives here (engineering) only; 0 = auto (= rate).
            with ui.row().classes("items-center gap-3 flex-wrap") as uhd_qual_row:
                dciq_cb = ui.checkbox("DC/IQ auto-correct", value=True)
                looff_in = ui.number("LO offset (MHz)", value=0.0, format="%g") \
                    .props("dense outlined").classes("w-36") \
                    .tooltip("0 = automatic: the LO is moved off the band when the "
                             "rate allows. Best + / Best - pick the best offset for "
                             "the selected rate. Applies at INITIALIZE.")
                ui.button("Best +", on_click=lambda: _eng_lo_best(+1)) \
                    .props("dense outline no-caps")
                ui.button("Best -", on_click=lambda: _eng_lo_best(-1)) \
                    .props("dense outline no-caps")
                eng_lo_note = ui.label(ENG_LO_NOTE_IDLE).classes("text-xs text-gray-500")
                rxbw_in = ui.number("RX bw (MHz, 0=auto)", value=0, format="%g") \
                    .props("dense outlined").classes("w-40")
                txbw_in = ui.number("TX bw (MHz, 0=auto)", value=0, format="%g") \
                    .props("dense outlined").classes("w-40")
            uhd_qual_row.set_visibility(False)
            # SoapySDR front-end switches (caps "fe_hooks"; HackRF)
            with ui.row().classes("items-center gap-3 flex-wrap") as fe_row:
                rxamp_cb = ui.checkbox("RX amp +14 dB", value=False)
                txamp_cb = ui.checkbox("TX amp +14 dB", value=False)
                bias_cb = ui.checkbox("Antenna bias 3.3 V", value=False)
                febw_in = ui.number("Baseband filter (MHz, 0=auto)", value=0,
                                    format="%g").props("dense outlined").classes("w-52")
                hd_lbl = ui.label("half-duplex: RX pauses while TX is keyed") \
                    .classes("text-xs text-amber-700 self-center")
            fe_row.set_visibility(False)

            # TX config row (config only: no TX commands over the
            # link yet; nothing radiates until the TX stage) --------------
            with ui.row().classes("items-center gap-3 flex-wrap"):
                ui.label("TX").classes("font-bold w-8 self-center")
                txcard_in = ui.select({0: "card0 (NV100-1)", 1: "card1 (NV100-2)"},
                                      value=0, label="TX card") \
                    .props("dense outlined").classes("w-40")
                txant_in = ui.select({"TXA1": "TXA1: TRx1A"},
                                     value="TXA1", label="TX port") \
                    .props("dense outlined").classes("w-40")
                _ppu.hook_tips(txant_in)
                txcf_in = ui.input("TX center (Hz)", value="425025000") \
                    .props("dense outlined").classes("w-36")
                txrate_lbl = ui.label("rate: shared with RX") \
                    .classes("font-mono text-xs self-center text-gray-600")
                # Cross-card TX runs on the other card's own clock, so it
                # may use its own sample rate; shown in cross-card mode only
                txrate_in = ui.select(
                    {"same": "same as RX", **{str(k): v for k, v in G40_SAMPLE_RATES.items()}},
                    value="same", label="TX sample rate") \
                    .props("dense outlined").classes("w-40") \
                    .tooltip("cross-card only: the TX card has its own clock. TX waveforms "
                             "(tone, playback, scenarios, jammer, replay) are built at this rate")
                txrate_in.set_visibility(False)
                # hardware-legal attenuations only (0 = max power)
                txatten_in = ui.select(
                    {str(d): f"{d} dB" for d in
                     [0, 1, 2, 3, 5, 7, 10, 15, 20, 25, 30, 35, 40, 41.75]},
                    value="30", label="TX atten (0 = max pwr)") \
                    .props("dense outlined").classes("w-40")
                # UHD only: precise dB TX gain (not attenuation); shown instead
                # of txatten_in when family=="uhd".
                txgain_num = ui.number("TX gain (dB)", value=40, format="%g") \
                    .props("dense outlined").classes("w-32")
                txgain_num.set_visibility(False)
                txgain_sel = ui.select({}, label="TX atten (dB)") \
                    .props("dense outlined options-dense").classes("w-64")
                txgain_sel.set_visibility(False)
                txgain_sel.on_value_change(
                    lambda e: txgain_num.set_value(float(e.value)) if e.value is not None else None)
            constraint_lbl = ui.label("").classes("font-mono text-xs")
            # UHD sensor telemetry line; polled on a timer only while a UHD radio
            # is focused + up. Hidden for epiq. Plain ui.label coloured by worst
            # severity via .style() (ui.html here floods "parent slot deleted"
            # and breaks refresh).
            sensor_lbl = ui.label("").classes("font-mono text-xs text-gray-600")
            sensor_lbl.set_visibility(False)
            with ui.row().classes("items-center gap-3 flex-wrap"):
                # PSD shaping rides the init cmd (service-side PSDRecorder)
                nfft_in = ui.select(["1024", "4096", "16384", "65536"],
                                    value="4096", label="PSD FFT") \
                    .props("dense outlined").classes("w-32")
                win_in = ui.select(["hann", "hamming", "blackman",
                                    "rectangular"],
                                   value="hann", label="PSD window") \
                    .props("dense outlined").classes("w-36")
                buf_in = ui.select({"16384": "16k", "65536": "64k",
                                    "262144": "256k", "1048576": "1M"},
                                   value="65536", label="PSD buffer") \
                    .props("dense outlined").classes("w-32")
            with ui.row().classes("items-center gap-2"):
                init_btn = ui.button("Initialize")
                rx_btn = ui.button("Start RX").props("color=positive")
                rxstop_btn = ui.button("Stop RX").props("color=negative outline")
                close_btn = ui.button("Close")
                tune_btn = ui.button("Retune")
                gain_btn = ui.button("Set gain")
            # Status line under the RX buttons, as in the operator console
            eng_status_lbl = ui.label("").classes("font-mono text-sm text-gray-700")
            # TX controls (RADIATE: gated behind a specA confirm)
            with ui.row().classes("items-center gap-2"):
                ui.label("TX").classes("font-bold w-10 self-center "
                                          "text-amber-700")
                tx_kind_in = ui.select({"tone": "CW tone", "file": "file on radio"},
                                       value="tone", label="Waveform") \
                    .props("dense outlined").classes("w-36")
                tx_off_in = ui.number("Tone offset (kHz from TX LO)", value=1000,
                                      format="%g") \
                    .props("dense outlined").classes("w-52")
                tx_ampl_in = ui.number("Ampl (0-1)", value=0.5, format="%.2f",
                                       min=0, max=1) \
                    .props("dense outlined").classes("w-28")
                tx_file_in = ui.input("File path (on radio)", value="") \
                    .props("dense outlined").classes("w-56")
                tx_browse_btn = ui.button(icon="folder_open") \
                    .props("dense flat").tooltip("Browse waveforms on the radio")
                tx_mode_in = ui.select(["CONTINUOUS", "SINGLE_SHOT"],
                                       value="CONTINUOUS", label="TX mode") \
                    .props("dense outlined").classes("w-40")
            with ui.row().classes("items-center gap-2"):
                txset_btn = ui.button("Set TX waveform") \
                    .props("color=secondary")
                txstart_btn = ui.button("START TX").props("color=negative")
                txstop_btn = ui.button("Stop TX").props("color=positive outline")
                tx_state_lbl = ui.label("TX idle") \
                    .classes("font-mono text-sm self-center text-gray-500")
                # What the radio has loaded, whichever view loaded it
                eng_txwf_lbl = ui.label("TX waveform: none loaded") \
                    .classes("font-mono text-xs self-center text-gray-600")
            # build a TX waveform on the radio from a small JSON spec: no big
            # .iq.bin over the link; the radio also writes the matched decode plan
            with ui.expansion("Generate TX waveform from JSON (built on the radio)",
                              icon="auto_awesome").classes("w-full"):
                gen_json_in = ui.textarea(
                    "Scenario JSON",
                    value='{"name": "fsk_tx", "sample_rate_hz": 50000000,\n'
                          ' "center_freq_hz": 425025000, "loop_s": 0.1,\n'
                          ' "signals": [{"modulation": "FSK", "symbol_rate": 4800,\n'
                          '   "tone_spacing_hz": 5000, "offset_hz": 1000000,\n'
                          '   "message": "PING X40"}]}') \
                    .props("outlined autogrow").classes("w-full font-mono text-xs")
                with ui.row().classes("items-center gap-2"):
                    gen_peak_in = _peak_input()
                    gen_json_btn = ui.button("Build on radio + load TX") \
                        .props("color=primary dense")
                    gen_json_lbl = ui.label("").classes("font-mono text-xs self-center")
            # load an existing IQ file from this x86: pushed to the radio over the
            # link (fine for small files; large IQ is much faster via scp). Files
            # already on the G40/X40 use "File path (on radio)" above.
            with ui.expansion("Upload IQ from this PC to the radio",
                              icon="upload_file").classes("w-full"):
                up_lbl = ui.label("").classes("font-mono text-xs")

                async def _upload_iq(e):
                    host = _host()
                    if host is None:
                        ui.notify("no host selected", type="warning"); return
                    data = await e.file.read()
                    # a BLUE recording under another ending goes up as .cdif, so the
                    # radio reads its header and not the header as samples
                    from dpmg.dsp import cdif_reader as _cdif
                    name = _cdif.blue_name(e.file.name, data[:12])
                    mb = len(data) / 1e6
                    up_lbl.set_text(f"sending {mb:.1f} MB to {host.serial}"
                                    + (f" as {name} (BLUE header found)..." if name != e.file.name else "..."))
                    try:
                        stored = await host.send_file(name, data,
                                                      timeout=max(60.0, mb * 8.0))
                        ack = await host.cmd("set_tx_waveform",
                                             {"kind": "file", "inbox": stored or name}, timeout=30.0)
                    except Exception as ex:
                        up_lbl.set_text(f"upload failed: {ex}"); return
                    r = ack.result or {}
                    up_lbl.set_text("TX waveform set from upload"
                                    if ack.status == 0 else f"set failed: {r.get('err')}")

                ui.upload(label="Choose IQ file (.iq.bin / .c64 / .wav / .cdif / .tmp / .blue / .prm)...",
                          auto_upload=True,
                          max_file_size=512_000_000, on_upload=_upload_iq) \
                    .props("accept=.iq.bin,.c64,.bin,.wav,.cdif,.tmp,.blue,.prm").classes("w-full")
            cmd_lbl = ui.label("").classes("font-mono text-xs text-gray-600")
            # v2 stream health straight from the STATUS heartbeat
            health_lbl = ui.label("stream: --").classes("font-mono text-xs")
            health_lbl.set_visibility(False)       # See eng_status_lbl
            # Radio-side logging (set from the operator console) + edits
            # here that the radio does not have yet
            eng_log_lbl = ui.label("radio logging off").classes("font-mono text-xs text-gray-600")
            eng_pend_lbl = ui.label("").classes("text-xs font-bold text-amber-700")
            eng_pend_lbl.set_visibility(False)
            ui.label("Command / link log").classes("text-xs font-bold mt-1")
            cmd_log = ui.log(max_lines=200) \
                .classes("w-full h-36 font-mono text-xs")

        # telemetry
        # Tabs hold only the charts; the panels below them stay
        # visible on both tabs; detections + decode stack on the right
        with ui.row().classes("w-full gap-4 items-start"):
            with ui.card().classes("grow min-w-0"):
                with ui.tabs().classes("w-full") as tele_tabs:
                    ui.tab("PSD", label="Live PSD")
                    ui.tab("WF", label="Waterfall")
                ui.label("int8 over the link, 0.5 dB/LSB: the waterfall is "
                         "stacked PSD history, no extra wire traffic") \
                    .classes("text-xs text-gray-600")
                with ui.tab_panels(tele_tabs, value="PSD").classes("w-full") \
                        as tele_panels:
                    with ui.tab_panel("PSD"):
                        psd_chart, psd_lbl = _build_psd_chart()
                    with ui.tab_panel("WF"):
                        wf_chart, wf_lbl = _build_wf_chart()
            with ui.column().classes("w-96 gap-4"):
                with ui.card().classes("w-full"):
                    ui.label("Detections (closed bursts)") \
                        .classes("text-sm font-bold")
                    det_table = ui.table(
                        columns=[
                            {"name": "idx", "label": "#", "field": "idx",
                             "sortable": True},
                            {"name": "freq", "label": "freq (MHz)",
                             "field": "freq"},
                            {"name": "snr", "label": "SNR (dB)", "field": "snr"},
                            {"name": "dur", "label": "dur (ms)", "field": "dur"},
                        ],
                        rows=[], row_key="idx", pagination=8,
                    ).classes("w-full").props("dense")
                    det_lbl = ui.label("0 cached").classes("font-mono text-xs")

                # channelizer decode (v3 WATCH_UPDATE telemetry)
                with ui.card().classes("w-full"):
                    with ui.row().classes("items-center w-full justify-between"):
                        ui.label("Channelizer decode").classes("text-sm font-bold")
                        plan_btn = ui.button("Push plan...").props("dense outline size=sm")
                    watch_table = ui.table(
                        columns=[
                            {"name": "n", "label": "band", "field": "n"},
                            {"name": "f", "label": "MHz", "field": "f"},
                            {"name": "m", "label": "mod", "field": "m"},
                            {"name": "st", "label": "state", "field": "st"},
                            {"name": "msg", "label": "last msg", "field": "msg"},
                            {"name": "dn", "label": "decodes", "field": "dn"},
                        ],
                        rows=[], row_key="n", pagination=8,
                    ).classes("w-full").props("dense")
                    watch_lbl = ui.label("no plan loaded").classes("font-mono text-xs")
        with ui.column().classes("w-full gap-2"):
            _build_carve_panel(psd_chart, lambda: _host())
            _build_tools_panel(lambda: _host())
            # LFSR code sets + multi-user DSSS overlay
            # (engineering view only; dsss_builder_tab.py)
            from dpmg.web.dsss_builder_tab import build_dsss_builder_panel
            build_dsss_builder_panel(lambda: _host())
            # Record RX on the radio, replay it on TX
            eng_band = {"band": None}

            def _eng_brush(e, _b=eng_band):
                try:
                    areas = (e.args or {}).get("areas") or []
                    rng = areas[0].get("coordRange") if areas else None
                    if not rng:
                        _b["band"] = None; return
                    lo, hi = min(rng), max(rng)
                    _b["band"] = (float(lo), float(hi)) if hi > lo else None
                except Exception:
                    pass
            psd_chart.on("chart:brushEnd", _eng_brush)
            psd_chart.on("chart:brush", _eng_brush)

            def _eng_txport():
                try:                 # _host is defined further down this builder
                    cfg = host_config(_host()) or {}
                except NameError:
                    return None
                if not cfg.get("inited") or not cfg.get("tx_antenna"):
                    return None
                card = cfg.get("tx_card")
                card = cfg.get("card", 0) if card is None else card
                return _trx(str(cfg["tx_antenna"]), card)
            with ui.expansion("Record & replay: record RX on the radio, "
                              "replay it on TX", icon="replay",
                              value=False).classes("w-full border rounded"):
                build_record_replay_panel(lambda: _host(),
                                          get_txport=_eng_txport,
                                          get_band=lambda: eng_band["band"],
                                          band_hint="the Live PSD above")

    # command helpers

    def _host():
        return MGR.hosts.get(sel["serial"])

    async def _cmd(name, params=None, note="", timeout=5.0):
        host = _host()
        if host is None:
            ui.notify("no host selected", type="warning")
            return
        cmd_lbl.set_text(f"{name} ...")
        # commands go through the shared event feed so every connected
        # browser sees every command, not just the one that clicked
        MGR._evt(f"> {host.serial} {name} {params or ''}")
        try:
            ack = await host.cmd(name, params, timeout=timeout)
        except TimeoutError as e:
            cmd_lbl.set_text(f"{name}: TIMEOUT")
            MGR._evt(f"! {host.serial} {name} TIMEOUT after "
                     f"{timeout:.0f}s (state truth = STATUS)")
            msg = str(e)
            if name == "init" and (params or {}).get("clock_source") == "external":
                msg = ("init timed out on external clock: is the 10 MHz + 1PPS "
                       "reference connected? the card can't lock without it.")
            ui.notify(msg, type="negative")
            return
        status = "OK" if ack.status == 0 else f"status={ack.status}"
        cmd_lbl.set_text(f"{name}: {status} {ack.result or ''} {note}")
        MGR._evt(f"< {host.serial} {name} {status} {ack.result or ''}")
        # surface the calibrated dB for whatever gain index was applied
        if ack.status == 0 and "gain_db" in (ack.result or {}):
            host.gain_db = ack.result["gain_db"]
        if name == "init" and ack.status == 0 and "rx_gain_db" in ack.result:
            host.gain_db = ack.result["rx_gain_db"]
        # remember the remote host's libsidekiq for cross-version bug triage
        if name == "init" and ack.status == 0 and "libsidekiq" in ack.result:
            host.libsidekiq = ack.result["libsidekiq"]
            # cross-check: warn loudly when the loaded lib is not one of the
            # versions installed at the device's canonical /usr/lib/epiq:
            # some functionality may differ from what was bench-tested
            inst = ack.result.get("libsidekiq_installed", [])
            loaded = ack.result["libsidekiq"]
            if inst and not any(loaded.startswith(v) or v.startswith(loaded)
                                for v in inst):
                warn = (f"libsidekiq MISMATCH on {host.serial}: loaded "
                        f"{loaded}, device has {'/'.join(inst)}; behavior "
                        f"may differ from bench-tested functionality")
                MGR._evt(f"! {warn}")
                ui.notify(warn, type="warning", timeout=10000)
        if ack.status != 0:
            ui.notify(f"{name} rejected: {ack.result}", type="negative")
        return ack

    def _trx(port_key, card):
        """Physical connector for a handle on a G40 card, e.g. RXA1@card1 is
        TRx1B. RX handles follow the digit; the TX side of J2/TRx2 is driven
        by the B-side chain (TxB1: TRx2, RF-verified: TxA2 never reaches
        the connector), so TXB1 maps to TRx2 despite its trailing 1."""
        sfx = "B" if int(card or 0) == 1 else "A"
        n = "2" if (port_key in ("TXB1", "TXA2") or port_key.endswith("2")) \
            else "1"
        return f"TRx{n}{sfx}"

    eng_ports = {"view": None, "sig": None}                # PORT_MAP

    def _eng_jack(side, val, card):
        """PORT_MAP: dropdown value: jack name for the focused radio."""
        h = _host()
        if h is not None and caps_family(h.serial) in ("uhd", "pluto", "soapy"):
            return val
        return (_ports.rx_jack if side == "rx" else _ports.tx_jack)(kind_in.value, val, card)

    def _eng_mode_ok(pm, kind, mode):
        """PORT_MAP: can any card and handle choice run this mode under the map?"""
        m = _MODES[mode]
        for rc in (0, 1):
            tc = 1 - rc if m["tx_hdl"] == "CROSS" else rc
            for rx in ("RXA1", "RXA2"):
                for tx in (("TXA1", "TXA2") if m["tx_hdl"] == "CROSS" else (m["tx_hdl"],)):
                    p = {"rx_antenna": rx, "tx_antenna": tx, "extras": {"card": rc, "tx_card": tc}}
                    if "TDD" in mode and _ports.rx_jack(kind, rx, rc) != _ports.tx_jack(kind, tx, tc):
                        continue
                    if not _ports.init_refusal(pm, kind, p):
                        return True
        return False

    def _update_constraints():
        _update_constraints_base()
        if not eng_ports.get("ready"):              # still building the page
            return
        # PORT_MAP: grey out what the map locks, then say why on the constraint line
        view = eng_ports["view"]
        pm = view["pm"] if view else None
        _ppu.apply(rxant_in, view, "rx",
                   jack_of=lambda v: _eng_jack("rx", v, int(card_in.value or 0)))
        _ppu.apply(txant_in, view, "tx",
                   jack_of=lambda v: _eng_jack("tx", v, int(txcard_in.value or 0)))
        kind = kind_in.value
        if kind in ("g40", "mock"):
            _ppu.grey_labels(mode_in, [m for m in _MODES if pm and not _eng_mode_ok(pm, kind, m)])
        h = _host()
        if h is not None and caps_family(h.serial) in ("uhd", "pluto", "soapy"):
            return
        try:
            p = _init_params()
        except (TypeError, ValueError):             # a half typed number
            return
        why = _ports.init_refusal(pm, p["kind"], p)
        note = (_ports.same_card_tx_note(*_ports.session_jacks(p["kind"], p))
                if p.get("tx_antenna") else None)
        if view and view["err"]:
            constraint_lbl.set_text(f"LOCKED: the port map did not load: {view['err']}")
            constraint_lbl.classes(replace="font-mono text-xs text-red-600 font-bold")
        elif why:
            constraint_lbl.set_text("LOCKED: " + why)
            constraint_lbl.classes(replace="font-mono text-xs text-red-600 font-bold")
        elif note:
            constraint_lbl.set_text("WARNING: " + note)
            constraint_lbl.classes(replace="font-mono text-xs text-amber-700 font-bold")

    def _eng_follow_ports(host):
        view = _ppu.port_view(host, host_config(host))
        sig = (host.serial, view and view["sig"])
        if sig != eng_ports["sig"]:
            eng_ports.update(view=view, sig=sig)
            _update_constraints()

    def _update_constraints_base():
        """Port lists follow each row's card; the constraint line encodes
        the shared-hardware rules so an illegal combo is visible before
        init: same card = one RFIC clock (sample rate shared, LOs free);
        same TRx connector = one SMA (RX+TX cannot run simultaneously);
        different cards = fully independent (needs dual-card TX stage)."""
        _host_now = _host()
        if _host_now is not None and caps_family(_host_now.serial) == "uhd":
            # UHD radios have a single RX/TX antenna and no card/port matrix;
            # _apply_uhd_caps_eng owns their antenna dropdowns, so skip the
            # G40/X40 port rebuild (it would clobber the UHD options).
            constraint_lbl.set_text("UHD radio: single RX/TX antenna, gains in "
                                    "dB; front-end quality hooks apply at init")
            constraint_lbl.classes(replace="font-mono text-xs text-gray-600")
            return
        kind = kind_in.value
        rxc, txc = int(card_in.value or 0), int(txcard_in.value or 0)
        if kind == "x40":
            rx = {"RXA1": "RXA1: J2", "RXA2": "RXA2: J1",
                  "RXB1": "RXB1: J4", "RXB2": "RXB2: J3"}
            tx = {"TXA1": "TXA1: J5"}
            card_in.set_value(0)
            txcard_in.set_value(0)
        else:
            rx = {k: f"{k}: {_trx(k, rxc)}" for k in ("RXA1", "RXA2")}
            # the mode owns the TX handle (TxB1 for TRx2, TxA1 for TRx1);
            # rebuilding the list to TXA1/TXA2 here would clobber the
            # mode's choice and every TX display downstream would show the
            # wrong port (select, conflict line, the KEY-TX confirm dialog)
            _m = _MODES.get(mode_in.value, _MODES["RX only"])
            if _m["tx_hdl"] == "CROSS":
                if txc == rxc:
                    txc = 1 - rxc
                    txcard_in.set_value(txc)
                tx = {"TXA1": f"TXA1: {_trx('TXA1', txc)}",
                      "TXA2": f"TXA2: {'TRx2A' if txc == 0 else 'TRx2B'}"}
            elif _m["tx_hdl"] is not None:
                tx = {_m["tx_hdl"]: f"{_m['tx_hdl']}: {_trx(_m['tx_hdl'], txc)}"}
            else:
                tx = {k: f"{k}: {_trx(k, txc)}" for k in ("TXA1", "TXA2")}
        rxant_in.set_options(rx, value=rxant_in.value if rxant_in.value in rx
                             else ("RXB1" if kind == "x40" else "RXA1"))
        txant_in.set_options(tx, value=txant_in.value
                             if txant_in.value in tx else next(iter(tx)))

        if kind == "x40":
            # A TX mode with an A-side RX is a hard conflict
            _m = _MODES.get(mode_in.value, _MODES["RX only"])
            _c = (x40_same_side_conflict(rxant_in.value, "TXA1")
                  if _m["tx_hdl"] is not None else None)
            if _c:
                constraint_lbl.set_text("CONFLICT: " + _c)
                constraint_lbl.classes(replace="font-mono text-xs text-red-600 font-bold")
            else:
                constraint_lbl.set_text(
                    "X40 (4R1T): single card, one sample rate; TX only on TXA1 / J5; "
                    "FDD = TX J5 + RX B-side (RXB1 / J4, RXB2 / J3). A-side RX "
                    "(RXA1 / J2, RXA2 / J1) is receive-only.")
                constraint_lbl.classes(replace="font-mono text-xs text-gray-600")
            txrate_lbl.set_text("rate: shared with RX")
            return
        if rxc == txc:
            txrate_lbl.set_text("rate: shared with RX (same RFIC clock)")
            # A dual-mode (FDD) init with RX on RXA2 cannot
            # stream: FIXED port mode makes J2 the TX jack
            _m = _MODES.get(mode_in.value, _MODES["RX only"])
            _c = (g40_fixed_mode_conflict(rxant_in.value, _m["tx_hdl"],
                                          _m["chan_mode"], rxc, txc)
                  if _m["tx_hdl"] is not None else None)
            if _c:
                constraint_lbl.set_text("CONFLICT: " + _c)
                constraint_lbl.classes(replace="font-mono text-xs "
                                               "text-red-600 font-bold")
            elif _trx(rxant_in.value, rxc) == _trx(txant_in.value, txc):
                if "TDD" in str(mode_in.value or ""):
                    # same connector is the point of the TDD same-port mode
                    constraint_lbl.set_text(
                        f"TDD same-port on {_trx(rxant_in.value, rxc)}: "
                        f"RX pauses while TX keys (half-duplex by design)")
                    constraint_lbl.classes(replace="font-mono text-xs "
                                                   "text-gray-600")
                else:
                    constraint_lbl.set_text(
                        f"CONFLICT: RX and TX both on "
                        f"{_trx(rxant_in.value, rxc)}"
                        f" (one SMA connector) cannot operate simultaneously. "
                        f"Pick different TRx ports.")
                    constraint_lbl.classes(replace="font-mono text-xs "
                                                   "text-red-600 font-bold")
            else:
                constraint_lbl.set_text(
                    f"same card{rxc}: sample rate shared, RX/TX LOs "
                    f"independent, TX config rides init")
                constraint_lbl.classes(replace="font-mono text-xs "
                                               "text-gray-600")
        else:
            txrate_lbl.set_text("rate: same as RX (own card clock)")
            _m = _MODES.get(mode_in.value, _MODES["RX only"])
            if _m["tx_hdl"] == "CROSS":
                constraint_lbl.set_text(
                    f"cross-card: RX {_trx(rxant_in.value, rxc)} (card{rxc}), TX "
                    f"{'TRx2' if txant_in.value == 'TXA2' else 'TRx1'}{'A' if txc == 0 else 'B'} "
                    f"(card{txc}): independent clocks/LOs, best isolation")
                constraint_lbl.classes(replace="font-mono text-xs text-gray-600")
            elif _m["tx_hdl"] is not None:
                constraint_lbl.set_text(
                    f"RX card{rxc} and TX card{txc} differ: pick 'Transceive cross-card' "
                    f"to transmit on the other card (this mode sends no TX across cards)")
                constraint_lbl.classes(replace="font-mono text-xs text-orange-600")
            else:
                constraint_lbl.set_text(f"receive only on card{rxc}")
                constraint_lbl.classes(replace="font-mono text-xs text-gray-600")

    def _apply_mode():
        m = _MODES.get(mode_in.value, _MODES["RX only"])
        mode_hint.set_text(f"{m['note']}")
        # TX fields only matter when the mode transmits; grey them out otherwise
        _tx = m["tx_hdl"] is not None
        for _w in (txant_in, txcf_in, txatten_in, txcard_in):
            _w.set_enabled(_tx)
        if _tx and m["tx_hdl"] != "CROSS":           # Options from the TX card
            # the mode owns the TX handle (TxB1 for TRx2, TxA1 for TRx1)
            txant_in.set_options({m["tx_hdl"]:
                f"{m['tx_hdl']}: {'TRx2' if m['tx_hdl']=='TXB1' else 'TRx1'}"},
                value=m["tx_hdl"])
        _update_constraints()

    for _el in (card_in, txcard_in, kind_in, rxant_in, txant_in):
        _el.on_value_change(lambda e: _update_constraints())
    mode_in.on_value_change(lambda e: _apply_mode())
    _apply_mode()
    _update_constraints()

    # The TX rate picker shows only for cross-card G40 TX
    def _upd_txrate():
        m = _MODES.get(mode_in.value, _MODES["RX only"])
        h = _host()
        epiq = not (h is not None and caps_family(h.serial) in ("uhd", "pluto", "soapy"))
        show = bool(epiq and kind_in.value in ("g40", "mock") and m["tx_hdl"] == "CROSS"
                    and int(txcard_in.value or 0) != int(card_in.value or 0))
        if txrate_in.visible != show:
            txrate_in.set_visibility(show)
        if not show and txrate_in.value != "same":
            txrate_in.set_value("same")     # one card = one clock: never keep a hidden split
        if show:
            txrate_lbl.set_text("TX card has its own clock")
    for _el in (card_in, txcard_in, kind_in, mode_in):
        _el.on_value_change(lambda e: _upd_txrate())
    _upd_txrate()

    def _apply_uhd_caps_eng(caps):
        """Adapt the engineering control card for a UHD-family radio from its
        caps. Additive: only reached when the focused radio's family is 'uhd';
        the g40/x40/mock manual path is untouched for epiq radios."""
        kind = caps.get("kind", "b200")
        if caps.get("image_known") is False:        # X410 image with no table
            ui.notify(caps.get("note") or "unknown FPGA image", type="warning", timeout=10000)
        bwf = float(caps.get("usable_bw_factor", 0.8))
        # engineering view: the full rate list when the radio advertises one
        # (operator view only gets the specified/recommended subset)
        srates = (caps.get("sample_rates_hz_all") or caps.get("sample_rates_hz")
                  or list(G40_SAMPLE_RATES))
        _warn = float(caps.get("rate_warn_below_hz") or 0)
        ropts = {int(hz): f"{hz/1e6:g} MSps  (~{hz*bwf/1e6:.1f} MHz)"
                 + (f"  Warning: <{_warn/1e6:g} MSps not recommended" if hz < _warn else "")
                 for hz in srates}
        fe = caps.get("fe_hooks") or {}
        fe_row.set_visibility(bool(fe) or bool(caps.get("half_duplex")))
        rxamp_cb.set_visibility(bool(fe.get("rx_amp")))
        txamp_cb.set_visibility(bool(fe.get("tx_amp")))
        bias_cb.set_visibility(bool(fe.get("bias_tee")))
        febw_in.set_visibility(bool(fe.get("rx_bandwidth")))
        hd_lbl.set_visibility(bool(caps.get("half_duplex")))
        # register the uhd rate set so _sync_rate_opts keeps it on kind change
        _RATE_OPTS[kind] = ropts
        if kind not in (kind_in.options or []):
            kind_in.set_options(list(kind_in.options or []) + [kind], value=kind)
        else:
            kind_in.set_value(kind)
        # set rate options after kind (kind change triggers _sync_rate_opts)
        _dflt = int(caps.get("default_rate_hz") or 0)
        cur = int(rate_in.value) if (rate_in.value is not None
                                     and int(rate_in.value) in ropts) \
            else (_dflt if _dflt in ropts else list(ropts)[-1])
        rate_in.set_options(ropts, value=cur)
        # RX antenna is a real 2-choice (RX2 / TX/RX): enable it; TX single.
        rxa = caps.get("rx_antennas") or ["RX2"]
        txa = caps.get("tx_antennas") or ["TX/RX"]
        rxv = rxant_in.value if rxant_in.value in rxa else (
            "RX2" if "RX2" in rxa else rxa[0])
        rxant_in.set_options({a: a for a in rxa}, value=rxv)
        rxant_in.props(remove="disable")
        txant_in.set_options({a: a for a in txa}, value=txa[0])
        txant_in.props("disable")
        # gains: precise dB number inputs; show them, hide the epiq selects,
        # clamp range/step to caps.
        rg = caps.get("rx_gain") or {}
        tg = caps.get("tx_gain") or {}
        # clamp carried-over values into this radio's range; label by mode
        for _el, _rng in ((rxgain_num, rg), (txgain_num, tg)):
            try:
                _lo, _hi = float(_rng.get("min", 0)), float(_rng.get("max", 1e9))
                _v = float(_el.value) if _el.value is not None else None
            except (TypeError, ValueError):
                continue
            if _v is not None and not (_lo <= _v <= _hi):
                _el.set_value(max(_lo, min(_hi, _v)))
        # Supported values only (number boxes = hidden mirrors)
        _fill_gain_select._kind, _fill_gain_select._caps = caps.get("kind"), caps
        _fill_gain_select(rxgain_sel, rxgain_num, caps.get("rx_gain_values"), rg)
        _fill_gain_select(txgain_sel, txgain_num, caps.get("tx_gain_values"), tg)
        gain_in.set_visibility(False)
        rxgain_num.set_visibility(False)
        rxgain_sel.set_visibility(True)
        txatten_in.set_visibility(False)
        txgain_num.set_visibility(False)
        txgain_sel.set_visibility(True)
        _tl = 'label="TX atten (dB)"' if tg.get("mode") == "atten" else 'label="TX gain (dB)"'
        txgain_num.props(_tl)
        txgain_sel.props(_tl)
        # reference split: clk_in = 10 MHz freq ref; ppsref_in = 1 PPS time ref
        cs = caps.get("clock_sources") or ["internal"]
        clk_in.set_options(cs, value="internal" if "internal" in cs else cs[0])
        clk_in.props('label="10 MHz ref (ext. auto-detected)"'
                     if caps.get("ext_ref_auto") else 'label="10 MHz ref"')
        ts = caps.get("time_sources")
        if ts is None:
            ts = ["none", "internal", "external"]
        if len(ts) > 1:
            ppsref_in.set_options(ts, value="internal" if "internal" in ts else ts[0])
        ppsref_in.set_visibility(len(ts) > 1)
        uhd_qual_row.set_visibility(bool(caps.get("quality_hooks")))
        eng_hr["bumped"] = None
        _eng_b200_rate_gain(move_gain=False)      # note only; a running radio keeps its gain
        # B200 PSD buffer default; an initialized radio's own value comes back via _eng_sync
        buf_in.set_value(str(psd_buffer_for(kind)))

    def _eng_lo_best(sign):
        """Best + / Best - (lo_best_choice); goes out at INITIALIZE."""
        if kind_in.value != "b200":
            ui.notify("Best LO offset is worked out for the B200 only", type="warning")
            return
        lo_mhz, raise_gain, msg, kind = lo_best_choice(rate_in.value, sign,
                                                       rxgain_num.value)
        looff_in.set_value(lo_mhz)
        if raise_gain:
            key = _nearest_gain_key(rxgain_sel, B200_HIGH_RATE_RX_GAIN_DB)
            rxgain_num.set_value(float(key) if key is not None else B200_HIGH_RATE_RX_GAIN_DB)
            if key is not None:
                rxgain_sel.set_value(key)
        ui.notify(msg.replace("Applies at START", "Applies at INITIALIZE"),
                  type=kind, multi_line=True)

    def _eng_lo_changed(_e=None):
        pending = _eng_inited()
        eng_lo_note.set_text(ENG_LO_NOTE_PENDING if pending else ENG_LO_NOTE_IDLE)
        eng_lo_note.classes(replace="text-xs " + ("text-amber-700 font-bold" if pending
                                                  else "text-gray-500"))
    looff_in.on_value_change(_eng_lo_changed)

    def _init_params():
        host = _host()
        if host is not None and caps_family(host.serial) in ("uhd", "soapy"):
            # UHD-family radio: single antenna, gains in dB, no card/mode
            # matrix; front-end quality hooks ride init via extras. A "soapy"
            # radio (xSDR) shares this path; its txgain_num is dB attenuation
            # (caps tx_gain mode "atten") and it ignores iq_balance/lo_offset.
            caps = _CAPS_CACHE.get(host.serial) or {}
            extras = {"tx_center_freq": float(txcf_in.value),
                      "dc_offset": bool(dciq_cb.value),
                      "iq_balance": bool(dciq_cb.value),
                      "lo_offset_hz": float(looff_in.value or 0) * 1e6,
                      "time_source": str(ppsref_in.value or "internal")}
            eng_lo_note.set_text(ENG_LO_NOTE_IDLE)        # this INITIALIZE applies it
            eng_lo_note.classes(replace="text-xs text-gray-500")
            # explicit analog bandwidths (engineering only); 0 = auto: omit so
            # the backend defaults bandwidth to the sample rate.
            _rxbw = float(rxbw_in.value or 0) * 1e6
            _txbw = float(txbw_in.value or 0) * 1e6
            if caps.get("fe_hooks"):
                extras.update({"rx_amp": bool(rxamp_cb.value),
                               "tx_amp": bool(txamp_cb.value),
                               "bias_tx": bool(bias_cb.value)})
                _febw = float(febw_in.value or 0) * 1e6
                if _febw > 0:
                    _rxbw = _febw
            if _rxbw > 0:
                extras["rx_bw_hz"] = _rxbw
            if _txbw > 0:
                extras["tx_bw_hz"] = _txbw
            return {
                "kind": kind_in.value or caps.get("kind", "b200"),
                "sample_rate": float(rate_in.value),
                "center_freq": float(cf_in.value),
                "rx_gain": float(rxgain_num.value),     # dB (precise number)
                "rx_antenna": str(rxant_in.value),
                "tx_antenna": str(txant_in.value),
                "tx_gain": float(txgain_num.value),     # dB gain (not atten)
                "clock_source": clk_in.value or "internal",
                "extras": extras,
            }
        m = _MODES.get(mode_in.value, _MODES["RX only"])
        p = {
            "kind": kind_in.value,
            "sample_rate": float(rate_in.value),
            "center_freq": float(cf_in.value),
            "rx_gain": float(gain_in.value),
            "rx_antenna": rxant_in.value,
            "clock_source": clk_in.value,
            "extras": {"card": int(card_in.value),
                       "chan_mode": m["chan_mode"]},   # mode owns dual/single
        }
        if m["tx_hdl"] == "CROSS":                     # Operator-console init
            rxc, txc = int(card_in.value or 0), int(txcard_in.value or 0)
            if txc != rxc:
                p["tx_antenna"] = str(txant_in.value or "TXA1")    # TXA1 = TRx1x, TXA2 = TRx2x
                p["tx_gain"] = float(txatten_in.value)
                p["extras"].update({"tx_card": txc, "chan_mode": "single",
                                    "rf_bandwidth_hz": int(float(rate_in.value) * 0.8),
                                    "tx_center_freq": float(txcf_in.value)})
                # Only when it differs from the RX rate
                if (txrate_in.visible and txrate_in.value not in (None, "same")
                        and abs(float(txrate_in.value) - float(rate_in.value)) >= 1.0):
                    p["extras"]["tx_sample_rate"] = float(txrate_in.value)
            return p
        # TX config only when the mode transmits and (for dual-card) same card
        if m["tx_hdl"] is not None and \
                int(txcard_in.value or 0) == int(card_in.value or 0):
            p["tx_antenna"] = m["tx_hdl"]              # mode's proven TX handle
            p["tx_gain"] = float(txatten_in.value)    # dB attenuation
            p["extras"]["tx_center_freq"] = float(txcf_in.value)
        return p

    eng_ports["ready"] = True                      # _init_params exists now

    async def _do_initialize(_e=None, confirmed=False):
        # PORT_MAP: the same refusal the service gives, before touching the radio
        _v = eng_ports["view"]
        _ip = _init_params()
        _c = _ports.init_refusal(_v and _v["pm"], _ip["kind"], _ip)
        if _c:
            ui.notify(_c, type="negative", close_button=True, timeout=12000)
            return
        # Refuse a TX mode on an A-side RX port before touching the radio
        if kind_in.value == "x40":
            _m = _MODES.get(mode_in.value, _MODES["RX only"])
            _c = (x40_same_side_conflict(rxant_in.value, "TXA1")
                  if _m["tx_hdl"] is not None else None)
            if _c:
                ui.notify(_c, type="negative", close_button=True, timeout=12000)
                return
        # Refuse a dual-mode (FDD) init with RX on RXA2 and TX on
        # the same card before touching the radio (FIXED mode makes J2 the TX jack)
        if kind_in.value == "g40":
            _m = _MODES.get(mode_in.value, _MODES["RX only"])
            _c = (g40_fixed_mode_conflict(rxant_in.value, _m["tx_hdl"], _m["chan_mode"],
                                          int(card_in.value or 0), int(txcard_in.value or 0))
                  if _m["tx_hdl"] not in (None, "CROSS") else None)
            if _c:
                ui.notify(_c, type="negative", close_button=True, timeout=12000)
                return
        # external needs a real 10 MHz + 1PPS wired to the radio; without them the
        # card can't lock and init hangs, so make it a deliberate, confirmed choice
        _chx = _g40_clock.CLOCK_CHOICES.get(str(clk_in.value), {})
        if (_chx.get("ref") == "external" or _chx.get("pps") == "external") and not confirmed:
            with ui.dialog() as dlg, ui.card():
                ui.label(_chx.get("label", "External reference clock")).classes("text-base font-bold")
                if _chx.get("ref") == "external":
                    ui.label("Needs the 10 MHz on the REF jack. The service switches the "
                             "HMC7044 to it and refuses the init if it does not lock; a "
                             "10 MHz lost later shows as 10 MHz LOST on the status line.")
                if _chx.get("pps") == "external":
                    ui.label("Takes the 1 PPS from the 1PPS jack; the status shows 1 PPS "
                             "ABSENT when nothing is there.")
                with ui.row().classes("justify-end w-full"):
                    ui.button("Cancel", on_click=lambda: dlg.submit(False)).props("flat")
                    ui.button("Initialize anyway",
                              on_click=lambda: dlg.submit(True)).props("color=warning")
            if not await dlg:
                return
        _host_now = _host()
        ack = await _cmd("init", {
            **_init_params(),
            "psd_nfft": int(nfft_in.value),
            "psd_window": win_in.value,
            "psd_buffer": int(buf_in.value),
        }, timeout=60.0)         # NV100 first RFIC cal measured ~35 s on the G40
        if ack is not None and ack.status == 0 and _host_now is not None:
            eng_clk[_host_now.serial] = str(clk_in.value or "internal")
        if ack is not None and ack.status == 0 and (ack.result or {}).get("same_card_tx"):
            ui.notify(ack.result.get("warning"), type="warning",     # PORT_MAP
                      close_button=True, timeout=15000)
        return ack
    init_btn.on_click(_do_initialize)

    # A clock pick on an initialized radio re-runs INITIALIZE with the
    # current settings and restarts RX if it was streaming. Client event only.
    eng_clk = {}                            # serial: clock_source of the last good init
    eng_clkprev = {"v": clk_in.value}

    async def _live_clock_eng(_e=None):
        host = _host()
        new = str(clk_in.value or "internal")
        prev, eng_clkprev["v"] = eng_clkprev["v"], new
        _cfg = host_config(host)            # The radio's clock, not this page's memory
        _have = (_cfg.get("clock_source") if _cfg and _cfg.get("inited")
                 else eng_clk.get(host.serial) if host is not None else None)
        if (host is None or not host.connected
                or host.radio_state == RadioState.UNINIT
                or _have == new):
            return                          # not initialized: INITIALIZE will send it
        if not await confirm_clock_change(host, new):
            clk_in.set_value(_have or prev)
            eng_clkprev["v"] = clk_in.value
            eng_guard.release("clk")
            return
        was_rx = host.radio_state == RadioState.STREAMING
        ui.notify(f"reference clock -> {new}: re-initializing {host.serial}")
        ack = await _do_initialize(confirmed=True)
        if ack is not None and ack.status == 0 and was_rx:
            await _cmd("start_rx")
    clk_in.on("update:model-value", _live_clock_eng)

    # RX center (Enter or leaving the field), RX gain and TX atten (on pick)
    # apply live on an initialized radio without a re-init, as in the operator console.
    # Sample rate, ports, cards, mode and TX center still need Initialize; TX moves
    # with the RX center (keeping its offset). Client events only, never syncs.
    def _eng_inited():
        h = _host()
        return (h is not None and h.connected
                and h.radio_state != RadioState.UNINIT)

    async def _live_cf_eng(_e=None):
        if not _eng_inited():
            return                          # not initialized: INITIALIZE will send it
        try:
            hz = float(str(cf_in.value).strip())
        except (TypeError, ValueError):
            ui.notify("RX center: enter the frequency in Hz", type="warning")
            return
        cur = eng_svc.get("cf")
        if cur is not None and abs(float(cur) - hz) < 0.5:
            return                          # unchanged: no retune (it resets detection)
        await _cmd("set_center", {"hz": hz})
    cf_in.on("keydown.enter", _live_cf_eng)
    cf_in.on("blur", _live_cf_eng)

    async def _live_gain_eng(_e=None):
        if _eng_inited() and gain_in.value is not None:
            await _cmd("set_gain", {"index": float(gain_in.value)})
    gain_in.on("update:model-value", _live_gain_eng)

    async def _live_atten_eng(_e=None):
        if not _eng_inited() or txatten_in.value is None:
            return
        cfg = host_config(_host()) or {}
        if not cfg.get("tx_antenna"):
            return                          # no TX on this init: nothing to attenuate
        await _cmd("set_tx_atten", {"qdb": int(round(float(txatten_in.value) * 4))})
    txatten_in.on("update:model-value", _live_atten_eng)
    rx_btn.on_click(lambda: _cmd("start_rx"))
    rxstop_btn.on_click(lambda: _cmd("stop_rx"))
    close_btn.on_click(lambda: _cmd("close"))
    tune_btn.on_click(lambda: _cmd("set_center",
                                   {"hz": float(cf_in.value)}))
    async def _set_gain_click():
        # caps-driven radios (B200, Pluto, Soapy) take the dB value from the
        # gain dropdown; gain_in is the hidden Epiq index box (that box holds the
        # G40 index, e.g. 200, which a B200 clamps to its 76 dB maximum)
        host = _host()
        if host is not None and caps_family(host.serial) in ("uhd", "pluto", "soapy"):
            await _cmd("set_gain", {"index": float(rxgain_num.value)})
        else:
            await _cmd("set_gain", {"index": float(gain_in.value)})
    gain_btn.on_click(_set_gain_click)

    # TX plane (RADIATES)
    def _tx_wf_params():
        if tx_kind_in.value == "file":
            return {"kind": "file", "path": tx_file_in.value}
        return {"kind": "tone",
                "offset_hz": float(tx_off_in.value) * 1e3,
                "ampl": float(tx_ampl_in.value)}

    txset_btn.on_click(lambda: _cmd("set_tx_waveform", _tx_wf_params()))
    txstop_btn.on_click(lambda: _cmd("stop_tx"))

    async def _gen_from_json():
        try:
            spec = json.loads(gen_json_in.value)
        except Exception as e:
            ui.notify(f"bad JSON: {e}", type="negative"); return
        host = _host()
        if host is None:
            ui.notify("no host selected", type="warning"); return
        gen_json_lbl.set_text("building on radio...")
        try:
            ack = await host.cmd("gen_scenario",
                                 _with_peak({"spec": spec, "load_tx": True}, gen_peak_in),
                                 timeout=30.0)
        except Exception as e:
            gen_json_lbl.set_text(f"gen failed: {e}"); return
        r = ack.result or {}
        if ack.status != 0:
            gen_json_lbl.set_text(f"gen failed: {r.get('err')}"); return
        # point the file box at the freshly-built IQ and select file mode, so
        # START TX (already loaded) or a re-Set both work
        tx_file_in.value = r.get("iq_path", "")
        tx_kind_in.set_value("file")
        gen_json_lbl.set_text(
            f"built {r.get('signals')} sig, {r.get('bytes', 0) / 1e6:.1f} MB, "
            f"peak {r.get('peak_fs') or 0:.2f} FS, TX loaded  "
            f"| plan: {r.get('plan_path', '')}")
        MGR._evt(f"< {host.serial} gen_scenario -> {r.get('iq_path')}")
    gen_json_btn.on_click(_gen_from_json)

    async def _browse_radio_files():
        host = _host()
        if host is None:
            ui.notify("no host selected", type="warning"); return
        try:
            ack = await host.cmd("list_waveforms", {}, timeout=10.0)
        except Exception as e:
            ui.notify(f"list failed: {e}", type="negative"); return
        r = ack.result or {}
        files = r.get("files", [])
        with ui.dialog() as dlg, ui.card().classes("min-w-96"):
            ui.label("TX waveforms on the radio").classes("text-base font-bold")
            if r.get("truncated"):
                ui.label(f"(newest {len(files)} of {r.get('total', '?')}, "
                         f"use Browse radio... for the full tree)").classes(
                    "text-xs text-amber-700")
            if not files:
                ui.label("(none yet: generate one from JSON above, or upload)") \
                    .classes("text-sm text-gray-500")
            for f in files:
                ui.button(f"{f['name']}   ({f['bytes'] / 1e6:.1f} MB)",
                          on_click=lambda pth=f["path"]: dlg.submit(pth)) \
                    .props("flat no-caps align=left").classes("w-full")
            ui.button("Cancel", on_click=lambda: dlg.submit(None)).props("flat")
        path = await dlg
        if path:
            tx_file_in.value = path
            tx_kind_in.set_value("file")
    tx_browse_btn.on_click(_browse_radio_files)

    async def _start_tx_gated():
        host = _host()
        if host is None:
            ui.notify("no host selected", type="warning")
            return
        txlo = host.status  # TX LO echoed in the last init ack, if any
        lo_txt = (f"{float(txcf_in.value)/1e6:.4f} MHz"
                  if txcf_in.value else "the TX LO")
        with ui.dialog() as dlg, ui.card():
            ui.label("Confirm TX: this will RADIATE").classes(
                "text-lg font-bold text-red-600")
            # the mode owns the TX handle: name both the handle and the
            # physical SMA so the operator knows exactly which port keys.
            # A B200 (caps-driven) has neither: show its port and TX gain,
            # the values _init_params sends for it.
            _m = _MODES.get(mode_in.value, _MODES["RX only"])
            _hdl = txant_in.value if _m["tx_hdl"] in (None, "CROSS") else _m["tx_hdl"]
            _fam = caps_family(host.serial)
            _caps = _CAPS_CACHE.get(host.serial) or {}
            ui.label(eng_tx_confirm_text(
                host.serial, lo_txt, tx_mode_in.value, family=_fam,
                kind=kind_in.value or _caps.get("kind"), handle=_hdl,
                jack=_trx(_hdl, int(txcard_in.value or 0)), atten_db=txatten_in.value,
                tx_port=txant_in.value, tx_gain_db=txgain_num.value,
                gain_mode=(_caps.get("tx_gain") or {}).get("mode")))
            ui.label("Confirm a spectrum analyzer / load is connected to the "
                     "TX port and the setup is safe to key.").classes(
                "text-sm text-gray-600")
            with ui.row():
                ui.button("Cancel", on_click=dlg.close).props("flat")
                ui.button("KEY TX NOW", on_click=lambda: dlg.submit("go")) \
                    .props("color=negative")
        if await dlg != "go":
            return
        await _cmd("start_tx", {"mode": tx_mode_in.value})

    txstart_btn.on_click(_start_tx_gated)
    host_sel.on_value_change(lambda e: sel.update(serial=e.value))

    # Controls follow the radio (keys = view_sync.eng_fields)
    eng_guard = _vs.EditGuard()
    eng_svc = {}
    # apply order matters: kind rebuilds the rate list, mode/cards rebuild ports
    _ENGW = [("kind", kind_in), ("mode", mode_in), ("card", card_in), ("txcard", txcard_in),
             ("rxant", rxant_in), ("txant", txant_in), ("clk", clk_in), ("rate", rate_in), ("cf", cf_in),
             ("txcf", txcf_in), ("txrate", txrate_in), ("gain", gain_in), ("txatten", txatten_in),
             ("nfft", nfft_in), ("win", win_in), ("buf", buf_in),
             ("tx_kind", tx_kind_in), ("tx_off_khz", tx_off_in), ("tx_ampl", tx_ampl_in),
             ("tx_file", tx_file_in)]
    _ENGN = {"rate": "Sample rate", "cf": "RX center", "clk": "Reference clock",
             "mode": "Operating mode", "rxant": "RX port", "card": "RX card",
             "txcard": "TX card", "txant": "TX port", "txatten": "TX atten", "txcf": "TX center", "txrate": "TX sample rate",
             "nfft": "PSD FFT", "win": "PSD window", "buf": "PSD buffer"}
    for _k, _el in _ENGW:
        _vs_track(eng_guard, _k, _el, eng_svc)

    def _eng_sync(host):
        cfg = host_config(host)
        if cfg is None:
            return
        svc = _vs.eng_fields(cfg, current_mode=mode_in.value)
        if caps_family(host.serial) in ("uhd", "pluto", "soapy"):
            svc = {k: v for k, v in svc.items()
                   if k in ("clk", "cf", "txcf", "nfft", "win", "buf", "tx_kind",
                            "tx_off_khz", "tx_ampl", "tx_file")}
        eng_svc.clear()
        eng_svc.update(svc)
        gps_ant.sync(cfg.get("ant_bias"))
        eng_txwf_lbl.set_text(_vs.tx_wf_text(cfg))
        eng_log_lbl.set_text(_vs.log_text(cfg))
        for k, el in _ENGW:
            if k not in svc or not eng_guard.may_apply(k, svc[k]):
                continue
            v = svc[k]
            if k == "rate":
                keys = list(rate_in.options.keys()) if isinstance(rate_in.options, dict) else []
                v = _vs.nearest(keys, v)
                if v is None:
                    continue
            elif k == "kind" and v not in (kind_in.options or []):
                continue
            elif k in ("cf", "txcf", "tx_file"):
                if str(el.value or "") != str(v):
                    el.set_value(str(v))
                continue
            _vs_set(el, v)
        wv = {k: el.value for k, el in _ENGW}
        pend = _vs.pending_labels(eng_guard, wv, svc, _ENGN)
        eng_pend_lbl.set_text(("not applied yet: " + ", ".join(pend) + ". Initialize "
                               "applies them") if pend else "")
        eng_pend_lbl.set_visibility(bool(pend))

    # cache poll

    # Radio cards (persistent, updated in place)
    eng_cards = {}                          # serial: element refs
    eng_cards_key = {"v": None}
    eng_refcur = {"state": None}

    def _eng_pick(serial):
        sel["serial"] = serial
        host_sel.set_value(serial)

    async def _eng_level(serial):
        h = MGR.hosts.get(serial)
        if h is None:
            return
        try:
            ack = await h.cmd("imu_set_level", {}, timeout=4.0)
        except Exception as ex:
            ui.notify(f"set level: {ex}", type="warning")
            return
        if ack is None or ack.status != proto.CmdStatus.OK:
            ui.notify(f"set level: {((ack.result or {}) if ack else {}).get('err', 'failed')}",
                      type="warning")
        else:
            ui.notify("level set to the current orientation", type="positive")

    def _eng_make_card(serial):
        card = ui.card().classes("p-2 cursor-pointer")
        card.on("click", lambda e, s=serial: _eng_pick(s))
        with card:
            ui.label(serial).classes("font-bold font-mono")
            r = {"card": card, "_c": {}}
            r["type"] = ui.label("").classes("text-xs")
            r["addr"] = ui.label("").classes("text-xs font-mono text-gray-500")
            r["role"] = ui.label("").classes("text-xs")
            r["state"] = ui.label("").classes("text-sm font-bold")
            r["hb"] = ui.label("").classes("font-mono text-xs")
            r["fpga"] = ui.label("").classes("text-xs")
            r["link"] = ui.label("").classes("text-xs font-bold")
            r["temp"] = ui.label("").classes("text-xs font-mono") \
                .tooltip("Radio host chip temperature (Jetson Orin junction, the hottest point on the module). "
                         f"Amber from {_ht.WARN_C:.0f} C, red from {_ht.HOT_C:.0f} C; "
                         "the module throttles at 99 C and shuts down at 104 C")
            r["ctemp"] = ui.label("").classes("text-xs font-mono") \
                .tooltip("Radio card temperatures (NV100 card A / card B, or the B200's own sensor), read while "
                         f"the radio is started. Amber from {_ht.CARD_WARN_C:.0f} C, "
                         f"red from {_ht.CARD_HOT_C:.0f} C")
            r["baro"] = ui.label("").classes("text-xs font-mono") \
                .tooltip("Payload barometer (BMP581/585 on the radio host I2C), read every "
                         f"{_baro.POLL_S:.0f} s whether or not the radio is started. Altitude is the "
                         "pressure altitude from 1013.25 hPa. Amber when the reading is stale")
            r["hpa"] = ui.label("").classes("text-xs font-mono") \
                .tooltip("Empower 1189 temperature (DB9 pin 3) and supply current (DB9 pin 2) through the "
                         "payload ADC, read every 2 s by the radio service. Only valid while the HPA is on. Amber from 70 C or above 12 A, "
                         "red from 85 C (the 1189 case maximum)")
            with ui.row().classes("items-center gap-1 no-wrap"):
                r["imu"] = ui.label("").classes("text-xs font-mono") \
                    .tooltip("NV100 accelerometer, read while the radio is started. Tilt is "
                             "measured from the orientation at start (or the last 'set level'). "
                             f"Amber from {_imu.TILT_WARN_DEG:.0f}°, red from "
                             f"{_imu.TILT_ALARM_DEG:.0f}° or after a shock over "
                             f"{_imu.SHOCK_G:.1f} g (red for {_imu.SHOCK_HOLD_S:.0f} s)")
                r["lvl"] = ui.button("set level").props("flat dense size=xs no-caps") \
                    .classes("text-xs") \
                    .tooltip("take the radio's current orientation as level")
                r["lvl"].on("click.stop", lambda e, s=serial: _eng_level(s))
        return r

    def _eng_set(r, key, txt, cls):
        # update a card label only when its text or colour changed (4 Hz refresh)
        if r["_c"].get(key) != (txt, cls):
            r["_c"][key] = (txt, cls)
            r[key].set_text(txt)
            r[key].classes(replace=cls)

    def _eng_update_card(r, serial, h):
        sel_now = (serial == sel["serial"])
        tx_on = bool(h.status is not None and h.status.flags & proto.Status.FLAG_TX_ACTIVE)
        ccls = ("p-2 cursor-pointer" + (" ring-2 ring-primary" if sel_now else "")
                + (" bg-red-50" if tx_on else ""))
        if r["_c"].get("card") != ccls:
            r["_c"]["card"] = ccls
            r["card"].classes(replace=ccls)
        lib = getattr(h, "libsidekiq", None)
        _eng_set(r, "type", _HOST_TXT.get(h.host_type, "?") + (f" | libsidekiq v{lib}" if lib else ""),
                 "text-xs")
        addr = getattr(h, "addr", None)
        _eng_set(r, "addr", f"{addr[0]}:{addr[1]}" if addr else "", "text-xs font-mono text-gray-500")
        txt, color = _STATE_TXT.get(h.radio_state, ("?", "text-gray-500"))
        _eng_set(r, "state", txt + rx_off_note(h.radio_state) + (" TX ON" if tx_on else ""),
                 "text-sm font-bold " + ("text-red-600" if tx_on else color))
        _eng_set(r, "hb", f"hb {_fmt_age(h.status_age)} | up {h.status.uptime_s if h.status else 0} s"
                          f" | det {h.det_head}", "font-mono text-xs")
        _ltxt, _lcol = link_text(h.connected, host_config(h), lost="STALE")
        _eng_set(r, "link", _ltxt, "text-xs font-bold " + _lcol)
        col = {"ok": "text-green-700", "warm": "text-amber-600 font-bold",
               "hot": "text-red-600 font-bold"}
        s_ = h.status
        tj = _ht.from_wire(s_.temp_c_x10) if s_ is not None else None
        if tj is None:
            _eng_set(r, "temp", "no chip temp", "text-xs font-mono text-gray-400")
        else:
            _eng_set(r, "temp", f"Chip temp {tj:.0f} °C" + (" HOT" if _ht.level(tj) == "hot" else "")
                     + _ht.power_text((host_config(h) or {}).get("host_power")),
                     "text-xs font-mono " + col[_ht.level(tj)])
        cfg = host_config(h) or {}
        _eng_set(r, "role", *_rfu.role_text(cfg))
        _eng_set(r, "fpga", *_rfu.fpga_text(cfg))
        cts = _ht.card_temps_from_config(cfg)
        if not cts:
            _eng_set(r, "ctemp", _ht.last_card_text(cfg), "text-xs font-mono text-gray-400")
        else:
            cw = _ht.worst(_ht.card_level(t) for _c, t in cts)
            _eng_set(r, "ctemp", ", ".join(f"{_ht.card_label(cfg, c)} {t:.0f} °C"
                                                   for c, t in cts) + (" HOT" if cw == "hot" else ""),
                     "text-xs font-mono " + col[cw])
        _want_caps(h)
        itxt, ilvl = _imu.card_text(cfg.get("imu"), radio_kind(serial, cfg))
        _eng_set(r, "imu", itxt, "text-xs font-mono " + {
            None: "text-gray-400", "ok": "text-green-700",
            "warm": "text-amber-600 font-bold", "hot": "text-red-600 font-bold"}[ilvl])
        btxt, blvl = _baro.card_text(cfg.get("baro"))
        _eng_set(r, "baro", btxt, "text-xs font-mono " + {
            None: "text-gray-400", "ok": "text-green-700", "warm": "text-amber-600 font-bold"}[blvl])
        htxt, hlvl = _rf.hpa_drop_text(cfg) or _rf.hpa_card_text(cfg.get("hpa_monitor"), radio_kind(serial, cfg))
        _eng_set(r, "hpa", htxt, "text-xs font-mono " + {
            None: "text-gray-400", "ok": "text-green-700",
            "warm": "text-amber-600 font-bold", "hot": "text-red-600 font-bold"}[hlvl])
        vis = bool(cfg.get("imu"))
        if r["_c"].get("lvl_vis") != vis:
            r["_c"]["lvl_vis"] = vis
            r["lvl"].set_visibility(vis)

    evt_cursor = {"n": 0}
    # waterfall = stacked PSD history; rows keyed to the selected host
    wf = {"rows": [], "freqs": None, "serial": None, "last_n": 0}

    def refresh():
        # drain new link events into the log (discoveries, sessions, flaps)
        for n, ts, msg in list(MGR.events):
            if n > evt_cursor["n"]:
                evt_cursor["n"] = n
                mark = "" if msg[:1] in (">", "<", "!") else "* "
                cmd_log.push(f"{ts} {mark}{msg}")

        serials = sorted(MGR.hosts)
        if host_sel.options != serials:
            host_sel.set_options(serials)
            if sel["serial"] is None and serials:
                sel["serial"] = serials[0]
                host_sel.set_value(serials[0])

        # Build the radio cards once per host set, then update in place
        _hkey = tuple(sorted(MGR.hosts))
        if _hkey != eng_cards_key["v"]:
            eng_cards_key["v"] = _hkey
            eng_cards.clear()
            hosts_row.clear()
            with hosts_row:
                if not MGR.hosts:
                    ui.label("listening for ANNOUNCE ...") \
                        .classes("text-gray-500 italic")
                for _s in _hkey:
                    eng_cards[_s] = _eng_make_card(_s)
        for serial, h in sorted(MGR.hosts.items()):
            if serial in eng_cards:
                _eng_update_card(eng_cards[serial], serial, h)
        host = _host()
        eng_role.sync(host_config(host) if host is not None else None)
        if host is None:
            conn_lbl.set_text("no host")
            return
        conn_lbl.set_text(
            f"{host.serial} @ {host.addr[0]}:{host.addr[1]} "
            f"{'CONNECTED' if host.connected else 'STALE'}")

        # Probe caps once; adapt the control set for UHD-family
        # radios. epiq radios keep the manual kind/mode/port controls as-is.
        if host.serial not in _CAPS_CACHE:
            asyncio.create_task(_fetch_caps(host, on_done=refresh))
        _efam = caps_family(host.serial)
        _ekey = (host.serial, _efam, (_CAPS_CACHE.get(host.serial) or {}).get("fpga_image"),
                 (_CAPS_CACHE.get(host.serial) or {}).get("_fetched"))
        if _ekey != _eng_cfg["v"]:
            _eng_cfg["v"] = _ekey
            if _efam in ("uhd", "pluto", "soapy"):      # pluto is caps-driven like uhd
                _apply_uhd_caps_eng(_CAPS_CACHE.get(host.serial) or {})
            else:
                # restore the epiq control set (undo the UHD widget swap)
                gain_in.set_visibility(True)
                rxgain_num.set_visibility(False)
                rxgain_sel.set_visibility(False)
                txatten_in.set_visibility(True)
                txgain_num.set_visibility(False)
                txgain_sel.set_visibility(False)
                ppsref_in.set_visibility(False)
                rxant_in.props(remove="disable")
                txant_in.props(remove="disable")
                clk_in.props('label="Reference clock (RX + TX)"')
                uhd_qual_row.set_visibility(False)
                fe_row.set_visibility(False)
                sensor_lbl.set_visibility(False)
                buf_in.set_value(str(PSD_BUFFER_DEFAULT))
                # a B200 selected before leaves its kind, ports and clock list
                # behind; put the G40/X40 set back so INITIALIZE sends G40 ports
                _ek = "x40" if getattr(host, "host_type", None) == int(HostType.X40_TX) else "g40"
                kind_in.set_options(["g40", "x40", "mock"], value=_ek)
                _cs = _g40_clock.labels(_ek)     # the G40 lists its five choices, the X40 three
                clk_in.set_options(_cs, value=clk_in.value if clk_in.value in _cs else "internal")
                _sync_rate_opts()
                _apply_mode()           # rebuilds the RX/TX port lists for this kind

        _eng_sync(host)                     # VIEW_SYNC
        _eng_follow_ports(host)             # PORT_MAP
        st = host.status
        if st is not None:
            tx_on = bool(st.flags & proto.Status.FLAG_TX_ACTIVE)
            if tx_on:
                tx_state_lbl.set_text("TX ON: RADIATING")
                tx_state_lbl.classes(replace="font-mono text-sm self-center "
                                             "text-red-600 font-bold")
            else:
                tx_state_lbl.set_text("TX idle")
                tx_state_lbl.classes(replace="font-mono text-sm self-center "
                                             "text-gray-500")
            txt, _ = _STATE_TXT.get(st.radio_state, ("?", ""))
            gdb = getattr(host, "gain_db", None)
            gtxt = (f" | gain +{gdb:g} dB re min" if gdb is not None else "")
            _stale = bool(st.flags & proto.Status.FLAG_RX_STALE)
            stale = " | RX STALE (wedged, PSD frozen, detection paused)" if _stale else ""
            health_lbl.set_text(
                f"stream: {txt} | frames {st.rx_frames} | "
                f"fps {st.fps} | over/under "
                f"{st.overruns}/{st.underruns}{gtxt} | up {st.uptime_s} s | "
                f"hb {_fmt_age(host.status_age)}{stale}")
            bad = st.overruns > 0 or st.underruns > 0
            health_lbl.classes(replace="font-mono text-xs " +
                               ("text-red-600 font-bold" if _stale else
                                "text-orange-600" if bad else
                                "text-gray-700"))
        # Status line under the RX buttons (as the operator console's)
        _eref = bool(st is not None and (st.flags & proto.Status.FLAG_REF_LOCKED))
        _etj = _ht.from_wire(st.temp_c_x10) if st is not None else None
        _etmp = ("" if _etj is None else
                 f", chip {_etj:.0f} °C" + {"ok": "", "warm": " WARM",
                                          "hot": " HOT, near throttling"}[_ht.level(_etj)])
        _ects = _ht.card_temps_from_config(host_config(host) or {})
        if _ects:
            _ecw = _ht.worst(_ht.card_level(t) for _c, t in _ects)
            _etmp += (", cards " + "/".join(f"{t:.0f}" for _c, t in _ects) + " °C"
                      + {"ok": "", "warm": " WARM", "hot": " HOT, near card limit"}[_ecw])
        # Colour the Reference clock field like the operator console
        _ers = ref_state(clk_in.value, _eref, host_config(host))
        if _ers != eng_refcur["state"]:
            eng_refcur["state"] = _ers
            clk_in.props(remove="bg-color")
            if _ers == "locked":
                clk_in.props("bg-color=green-2")
            elif _ers in ("acquiring", "no_fix"):
                clk_in.props("bg-color=amber-2")
            elif _ers == "lost":
                clk_in.props("bg-color=red-3")
        _ecfg_r = host_config(host) or {}
        _etxr = _ecfg_r.get("tx_sample_rate")
        _erxr = _ecfg_r.get("sample_rate")
        if _etxr and not (_erxr and abs(float(_etxr) - float(_erxr)) < 1.0):
            _etmp = f", TX {float(_etxr)/1e6:g} MSps" + _etmp
        _eref_t = ref_words(clk_in.value, _eref, _ecfg_r)
        if st is not None:
            _etx, _ = _STATE_TXT.get(st.radio_state, ("?", ""))
            _estale = bool(st.flags & proto.Status.FLAG_RX_STALE)
            eng_status_lbl.set_text(
                f"{'CONNECTED' if host.connected else 'STALE'}, {_etx}, "
                f"frames {st.rx_frames}, fps {st.fps}, over/under {st.overruns}/{st.underruns}, "
                f"up {st.uptime_s} s{_etmp}{_eref_t}"
                + (", RX STALE (PSD frozen, detection paused)" if _estale else ""))
            eng_status_lbl.classes(replace="font-mono text-sm " + (
                "text-red-600 font-bold" if _estale else
                "text-orange-600" if (st.overruns or st.underruns) else "text-gray-700"))
        else:
            eng_status_lbl.set_text(f"{'CONNECTED' if host.connected else 'STALE'}, no status yet")
        view = host.psd
        if view is not None:
            f, p = view.freqs_hz, view.psd_db
            psd_chart.options["series"][0]["data"] = [
                [float(a), float(b)] for a, b in zip(f, p)]
            _set_psd_y(psd_chart, p)
            psd_chart.update()
            age = time.monotonic() - view.t_arrival
            # PSD is fire-and-forget UDP: when frames stop (link loss, RX
            # stop, service down) the chart silently freezes: make the
            # dropout unmissable instead
            if age > 2.5:
                psd_lbl.set_text(
                    f"PSD STALE: last frame {age:.1f} s ago "
                    f"(link degraded, RX stopped, or service down)")
                psd_lbl.classes(replace="font-mono text-xs text-red-600 "
                                        "font-bold")
                wf_lbl.set_text(f"waterfall frozen: no PSD for "
                                f"{age:.1f} s")
                wf_lbl.classes(replace="font-mono text-xs text-red-600 "
                                       "font-bold")
            else:
                psd_lbl.set_text(
                    f"{len(f)} bins | center {view.frame.center_hz/1e6:.4f} "
                    f"MHz | span {view.frame.span_hz/1e3:.0f} kHz | "
                    f"{age:.1f} s ago | {host.psd_frames_rx} frames rx")
                psd_lbl.classes(replace="font-mono text-xs")
                wf_lbl.classes(replace="font-mono text-xs")

            # waterfall accumulation: one row per new psd frame (dedupe by
            # the frame counter); host switch restarts the history
            if wf["serial"] != host.serial:
                wf.update(rows=[], freqs=None, serial=host.serial, last_n=0)
            if host.psd_frames_rx != wf["last_n"]:
                wf["last_n"] = host.psd_frames_rx
                st_ = max(1, len(f) // WF_BINS)
                wf["freqs"] = f[::st_][:WF_BINS]
                wf["rows"].append(p[::st_][:WF_BINS])
                if len(wf["rows"]) > WF_MAX_ROWS:
                    wf["rows"].pop(0)
            if tele_panels.value == "WF" and wf["rows"]:
                rows = wf["rows"]
                data = [[fi, ti, round(float(v), 1)]
                        for ti, r in enumerate(rows)
                        for fi, v in enumerate(r)]
                allv = np.concatenate(rows)
                wf_chart.options["xAxis"]["data"] = [
                    f"{x/1e6:.3f}" for x in wf["freqs"]]
                wf_chart.options["yAxis"]["data"] = list(range(len(rows)))
                wf_chart.options["visualMap"]["min"] = \
                    float(np.percentile(allv, 5))
                wf_chart.options["visualMap"]["max"] = \
                    float(np.percentile(allv, 99))
                wf_chart.options["series"][0]["data"] = data
                wf_chart.update()
                wf_lbl.set_text(f"{len(rows)}/{WF_MAX_ROWS} rows | "
                                f"{len(wf['freqs'])} bins | newest at top")

        rows = []
        for idx in sorted(host.detections, reverse=True)[:DET_TABLE_MAX]:
            d = host.detections[idx]
            rows.append({"idx": idx,
                         "freq": f"{d.freq_hz/1e6:.4f}",
                         "snr": f"{d.snr_db:.1f}",
                         "dur": f"{d.duration_ms:.0f}"})
        det_table.rows = rows
        det_table.update()
        det_lbl.set_text(f"{len(host.detections)} cached "
                         f"(head {host.det_head})")

        # channelizer decode rows (v3 WATCH_UPDATE)
        wrows = []
        for b in getattr(host, "watch_bands", []) or []:
            st = "decoding" if b.get("d") else ("present" if b.get("p") else "-")
            msg = b.get("msg", "")
            if str(msg).startswith("[sealed"):               # Opened on this host
                try:
                    from dpmg.secure import secure_ground as _sg
                    _g = _sg.ground()
                    _g.poll(host)
                    _r = _g.last(host.serial, b.get("n", ""))
                    if _r is not None:
                        msg = (_r["msg"] + " (AUTH_OK)") if _r["msg"] else f"{msg} {_r['verdict']}"
                except Exception:                                  # noqa: BLE001
                    pass
            wrows.append({"n": b.get("n", ""),
                          "f": f"{b.get('f', 0) / 1e6:.3f}",
                          "m": b.get("m", ""), "st": st,
                          "msg": msg, "dn": b.get("dn", 0)})
        watch_table.rows = wrows
        watch_table.update()
        _wage = time.monotonic() - getattr(host, "_watch_t", 0.0)
        if not wrows:
            watch_lbl.set_text("no plan loaded")
        elif getattr(host, "_watch_t", 0.0) and _wage < 3.0:
            watch_lbl.set_text(f"{len(wrows)} band(s), live")
        else:
            watch_lbl.set_text(f"{len(wrows)} band(s), stale {_wage:.0f}s")

    def _push_plan():
        host = _host()
        if host is None:
            ui.notify("no host selected", type="warning"); return
        with ui.dialog() as dlg, ui.card().classes("w-[36rem]"):
            ui.label("Load a channel plan on the radio").classes("font-bold")

            # Source A: a plan from the ground station
            # upload from the browser machine or paste JSON; the bytes cross the
            # link (inline if small, FILE_* chunked transfer if big).
            ui.label("From the ground station: upload or paste, sent over "
                     "the link").classes("text-xs font-bold text-gray-700 mt-1")
            ta = ui.textarea(placeholder='{"compose": {...}, "entries": [...]}') \
                .classes("w-full font-mono").props("rows=8 outlined dense")

            async def _on_upload(e):
                try:
                    txt = await e.file.text()
                    json.loads(txt)                      # validate before showing
                    ta.value = txt
                    ui.notify(f"loaded {e.file.name}", type="positive")
                except Exception as ex:
                    ui.notify(f"bad file {e.file.name}: {ex}", type="negative")

            ui.upload(label="Choose plan .json...", auto_upload=True,
                      max_files=1, on_upload=_on_upload) \
                .props('accept=.json flat dense').classes("w-full")

            ui.separator().classes("my-1")

            # Source B: a plan already on the radio
            # the G40 reads its own disk (plan_path): nothing crosses the link
            ui.label("Already on the radio (G40): the radio reads its own "
                     "disk, no transfer").classes("text-xs font-bold "
                                                   "text-gray-700")
            radio_path = ui.input(
                placeholder="/app_svc/plans/myplan.json (path on the G40)") \
                .classes("w-full font-mono").props("outlined dense")

            async def _apply(params, note):
                dlg.close()
                MGR._evt(f"> {host.serial} set_plan ({note})")
                try:
                    ack = await host.cmd("set_plan", params, timeout=15.0)
                    MGR._evt(f"< {host.serial} set_plan "
                             f"status={ack.status} {ack.result or ''}")
                    ui.notify(f"set_plan: {ack.result}",
                              type="positive" if ack.status == 0 else "negative")
                except Exception as e:
                    ui.notify(f"set_plan failed: {e}", type="negative")

            async def _send_ground():
                txt = (ta.value or "").strip()
                if not txt:
                    ui.notify("no plan pasted/uploaded", type="warning"); return
                try:
                    plan = json.loads(txt) if txt.startswith("{") \
                        else json.load(open(txt))
                except Exception as e:
                    ui.notify(f"bad plan: {e}", type="negative"); return
                params = {"plan": plan}
                # too big to ride inline in one datagram: FILE_* chunked push,
                # then point set_plan at the service's inbox copy
                blob = json.dumps(plan, separators=(",", ":")).encode()
                if len(blob) > proto.MAX_DATAGRAM - 200:
                    MGR._evt(f"> {host.serial} plan is {len(blob)} B, "
                             f"pushing via FILE_* transfer")
                    await host.send_file("pushed_plan.json", blob)
                    params = {"plan_inbox": "pushed_plan.json"}
                await _apply(params, f"{len(plan.get('entries', []))} entries, "
                                     f"from ground")

            async def _send_radio():
                p = (radio_path.value or "").strip()
                if not p:
                    ui.notify("no radio path given", type="warning"); return
                await _apply({"plan_path": p}, f"radio path {p}")

            with ui.row().classes("w-full justify-end mt-1"):
                ui.button("Cancel", on_click=dlg.close).props("flat")
                ui.button("Load from radio", on_click=_send_radio) \
                    .props("outline color=primary")
                ui.button("Push from ground", on_click=_send_ground) \
                    .props("color=primary")
        dlg.open()

    plan_btn.on_click(_push_plan)
    ui.timer(REFRESH_S, refresh)

    async def _sensor_tick():
        # poll UHD sensor telemetry only while a UHD radio is focused + up;
        # epiq radios keep the row hidden and never see a sensors cmd. Fully
        # guarded: the timer must never raise (a throw here floods the log and
        # can disrupt the shared refresh / caps rendering).
        try:
            host = _host()
            if host is None or caps_family(host.serial) not in ("uhd", "soapy"):
                sensor_lbl.set_visibility(False); return
            st = host.status
            if st is None or st.radio_state == int(RadioState.UNINIT):
                sensor_lbl.set_visibility(False); return
            try:
                ack = await host.cmd("sensors", {}, timeout=3.0)
            except Exception:
                return
            d = getattr(ack, "result", None) or {}
            txt, col = _fmt_sensors(d, ext_ref=(clk_in.value == "external"))
            sensor_lbl.set_text(txt)
            sensor_lbl.style(replace=f"color: {col}")
            sensor_lbl.set_visibility(True)
        except Exception:
            # client/slot gone or element deleted: swallow so the timer lives
            return
    ui.timer(3.0, _sensor_tick)

    # ref-clock status: GPSDO lock + 1PPS are live reads; the ext-10MHz line only
    # echoes the configured freq when external is selected (libsidekiq has no
    # "10 MHz present" bit: a good ext ref shows up as the card locking cleanly).
    # Slow poll (card read ~1 s each) and only once a radio is inited.
    async def _gps_refresh():
        host = _host()
        if host is None:
            gps_lbl.set_text("ref status: no host")
            return
        try:
            ack = await host.cmd("gps_status", {}, timeout=6.0)
        except Exception:
            return
        r = (ack.result or {}) if ack else {}
        gps_ant.update(r)                                   # GPS_ANT_BIAS
        _sky = ("   " + fmt_gnss(r.get("gnss"))
                if clk_in.value in GPS_CLOCKS and "gnss" in r else "")
        if not r or r.get("err"):
            gps_lbl.set_text("ref status: init a radio to read" + _sky)
            return
        rc = str(r.get("ref_clock", "?"))
        parts = [f"ref={rc}"]
        ck = r.get("clock") or {}
        if ck.get("choice"):
            # the G40 service's own clock check: the choice, the HMC7044 input and
            # lock, the card clock against the 1 PPS, the 10 MHz verdict
            parts = [f"clock {_g40_clock.label(ck['choice'])}"]
            _cref = (_g40_clock.CLOCK_CHOICES.get(ck["choice"]) or {}).get("ref")
            if _cref in ("gpsdo", "host"):
                # the lock with the fix beside it; locked with no fix is not GPS time
                parts.append(_g40_clock.gpsdo_verdict(
                    _cref, r.get("gpsdo_locked"), r.get("gnss"))["text"] or "GPSDO ?")
            if ck.get("hmc_input") is not None:
                hl = ck.get("hmc_locked")
                parts.append(f"HMC7044 input {ck['hmc_input']}"
                             + ("" if hl is None else (", PLLs locked" if hl else ", PLLs NOT locked")))
            txt = _g40_clock.clock_text({k: ck.get(k) for k in ("ten_mhz", "clock_ppm", "pps_present", "pps")})
            if ", " in txt:                 # nothing measured (an X40): no tail to add
                parts.append(txt.split(", ", 1)[1])
            if ck.get("why") and ck.get("ten_mhz") in ("LOST", "not measured"):
                parts.append(ck["why"])
            gps_lbl.set_text("   ".join(parts) + _sky)
            return
        # GPSDO line only when GPSDO is the actual source (else it is noise)
        if "gpsdo" in rc or r.get("gpsdo_enabled"):
            gl = r.get("gpsdo_locked")
            acc = r.get("freq_accuracy_ppm")
            v = _g40_clock.gpsdo_verdict("gpsdo", gl, r.get("gnss"))["text"] or "GPSDO ?"
            parts.append(v + (f" ({acc:+.3g} ppm)"
                         if gl and isinstance(acc, (int, float)) else ""))
        # external 10 MHz: configured/expected freq: libsidekiq has no live
        # "present" bit, so label it as such (a good ext ref = the card locks clean)
        ef = r.get("ext_ref_freq_hz")
        if rc == "external" and ef:
            parts.append(f"ext {ef / 1e6:.4g} MHz (configured)")
        # G40 on external: the GPS module's word on the carrier clock
        car = r.get("carrier")
        if car:
            parts.append(f"carrier {car.get('mode') or '?'}, source {car.get('disc_src') or '?'}"
                         + ("" if car.get("locked") else ", not confirmed"))
        # 1PPS on whatever source the card is set to (name it, so external vs host is clear)
        pp = r.get("pps_present")
        if pp is not None:
            src = {0: "external", 1: "host"}.get(r.get("pps_source"), r.get("pps_source"))
            src_s = f", src={src}" if src is not None else ""
            if r.get("pps_line"):
                src_s += f", line {r['pps_line']}"
            parts.append(f"1PPS {'present' if pp else 'absent'}{src_s}")
        gps_lbl.set_text("   ".join(parts) + _sky)
    ui.timer(10.0, _gps_refresh)
