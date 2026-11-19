"""Mission planning tab (NiceGUI).

The catalog, the per-radio timeline, the validation flags, save and reopen, and
the run controls (Run mission behind one confirm, STOP ALL TX) with the audit log.

This builds the tab into whatever container the caller is in. It takes a
LinkManager so it can list connected radios and reach them to key. It never
adds a new key path: running a step drives the same service commands the
jammer, playback, tools, transponder and record panels send, through
mission_runner, and only after the operator works the single run confirm.

    build_mission_tab(mgr)

mgr is a link_client.LinkManager. The app mounts it as the /mission view
(mission_page in dpmg/web/nicegui_app.py) on the SDR tab's LinkManager.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import sys
import time
import uuid

from nicegui import background_tasks, ui

from .. import run_notice
from . import catalog
from . import map_element as me
from . import map_import as mimp
from . import map_store
from . import mission_file as mf
from . import mission_import as mi
from . import motion_sim as ms
from . import payload_sweep as ps
from . import ports
from . import scenario_spec as ss
from . import triggers as tg

# Payload band per profile is optional: the planner still works without the
# payload files (they live at the repo root, not in this package).
try:
    from dpmg.payload import payload_budget as _pb
except Exception:
    _pb = None
try:
    from dpmg.radio import radio_role as _role       # the radio's role
except Exception:
    _role = None
from .mission_runner import (MissionRunner, AuditLog, send_stop_tx, run_changes, effective_rate_at, bring_up_plan,
                             plan_steps_on, CLOCK_SOURCES)
from .position_feed import PositionFeed
from .feed_state import feed_state, feed_state_text, fix_extras
from . import feed_binding as fb
from dpmg.link import feed_settings
from dpmg.link.aircraft_feeds import PanelFields
from dpmg.link.proto import Status
from dpmg.web import theme as _theme
from dpmg.web import port_pick_ui as _ppu            # port map choices
from dpmg.radio import g40_clock as _g40_clock       # the clock choices and the clock line
from dpmg.link.acft_pos_multicast import FORMATS as FEED_FORMATS
from dpmg.link.acft_pos_multicast import MulticastPosListener, local_ipv4_addresses


LANE_H = 70                 # pixel height of one radio lane
def _kind_style(technique: str) -> dict:
    """The timeline palette entry (fill, border, text) for a technique: by what
    the step does, so nothing is red unless it is in conflict (theme.py)."""
    e = catalog.get(technique)
    kind = _theme.timeline_kind(technique, e.group if e else "",
                                bool(e.radiates) if e else False)
    return _theme.TIMELINE_KINDS[kind]


def _group_color(technique: str) -> str:
    return _kind_style(technique)["fill"]
# relative (elapsed-time) timeline window. Missions usually run 30 to 60 min:
# an empty plan spans an hour, a plan with steps spans its length (padded and
# rounded up to 5 min) but never less than an hour.
EMPTY_WINDOW_S = 3600.0
MIN_WINDOW_S = 3600.0
WINDOW_ROUND_S = 300.0
# how much time the timeline shows across its width by default, whatever the
# plan's length (a longer plan, or the 24 h UTC day, scrolls sideways)
VIEW_S = 3600.0
MIN_VIEW_S = 20.0           # deepest zoom: this much time across the width
STEP_MIN_PX = 128           # a step block is never narrower than this, so its name reads
NEW_STEP_LEAD_S = 60.0      # a catalog step on the UTC clock starts this far ahead
CLOCK_SKEW_WARN_S = 1.0     # a radio OS clock this far from the console shows amber


def match_planned_radio(rid, serials):
    """The live serial a planned radio named `rid` (no address) stands for:
    the same name, case aside, or a serial that starts with it and a dash
    ('B200mini' binds to 'B200MINI-3000001'). None unless exactly one fits."""
    key = str(rid or "").strip().lower()
    if not key:
        return None
    hits = [s for s in serials
            if str(s).lower() == key or str(s).lower().startswith(key + "-")]
    return hits[0] if len(hits) == 1 else None
# the RF bandwidth a sample rate gives, the main app's usable_bw_factor per
# radio kind (sdr_core): 0.8 of the rate, HackRF 0.75
USABLE_BW_FACTOR = {"hackrf": 0.75}
USABLE_BW_DEFAULT = 0.8


def rf_bw_hz(rate_hz, radio_type=None):
    """Estimated usable RF bandwidth for a sample rate on a radio type."""
    return float(rate_hz) * USABLE_BW_FACTOR.get((radio_type or "").lower(), USABLE_BW_DEFAULT)


def bw_text(hz):
    hz = float(hz)
    return f"{hz / 1e6:g} MHz" if hz >= 1e6 else f"{hz / 1e3:g} kHz"


def rate_text(rate_hz, radio_type=None):
    """A sample rate as the operator reads it, with the RF bandwidth it gives:
    '2 MSps, RF ~1.6 MHz'."""
    return f"{float(rate_hz) / 1e6:g} MSps, RF ~{bw_text(rf_bw_hz(rate_hz, radio_type))}"


def os_skew_s(host_time, t_sent, t_got):
    """The radio's OS clock minus the console's, with half the round trip
    taken out (the radio stamps its reply somewhere in the middle). None when
    the radio did not report host_time (an older service build)."""
    try:
        return float(host_time) - (float(t_sent) + float(t_got)) / 2.0
    except (TypeError, ValueError):
        return None


def parse_utc_text(text):
    """'YYYY-MM-DD HH:MM:SS' (seconds optional) in UTC to epoch seconds, or
    None when it does not parse."""
    import datetime
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.datetime.strptime((text or "").strip(), fmt)
            return dt.replace(tzinfo=datetime.timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def _windows_is_admin() -> bool:
    """Whether this process runs elevated (Windows only; False elsewhere)."""
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def windows_set_clock_command(stamp: str) -> str:
    """The PowerShell that sets the Windows clock to a UTC stamp
    ('YYYY-MM-DD HH:MM:SS'). The stamp is parsed as UTC and converted to local
    time in PowerShell, so the laptop's time zone does not matter. w32tm cannot
    set an arbitrary time (it only resyncs with a time source), hence Set-Date."""
    return ("$t = [datetime]::SpecifyKind([datetime]::ParseExact('" + stamp +
            "', 'yyyy-MM-dd HH:mm:ss', [cultureinfo]::InvariantCulture), 'Utc')"
            ".ToLocalTime(); Set-Date -Date $t | Out-Null")


def _set_console_clock_windows(stamp: str, run=None, is_admin=None):
    """Windows branch of set_console_clock. Setting the clock needs the
    SeSystemtimePrivilege, which only an elevated process has. When this
    console runs elevated the Set-Date runs straight away; otherwise it is
    launched through Start-Process -Verb RunAs, which raises the Windows
    administrator (UAC) prompt on the console's screen, and the message says
    so. If the prompt is declined, or PowerShell is missing, the message
    carries the command to run in a PowerShell opened as administrator.
    run and is_admin are injectable for the unit tests."""
    import subprocess
    run = run or subprocess.run
    admin = _windows_is_admin() if is_admin is None else bool(is_admin)
    inner = windows_set_clock_command(stamp)
    shown = "Set-Date (as administrator): " + inner
    if admin:
        cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", inner]
        how = ""
    else:
        # the elevated child returns its exit code through -PassThru
        quoted = inner.replace("'", "''")
        outer = ("$p = Start-Process powershell -Verb RunAs -Wait -PassThru "
                 "-ArgumentList @('-NoProfile', '-NonInteractive', '-Command', '"
                 + quoted + "'); exit $p.ExitCode")
        cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", outer]
        how = " (the Windows administrator prompt was shown for it)"
    try:
        r = run(cmd, capture_output=True, text=True, timeout=120)
    except Exception as e:
        return False, (f"console clock not set ({e}); run in a PowerShell opened as "
                       f"administrator: {shown}")
    if r.returncode != 0:
        why = (r.stderr or r.stdout or "").strip().splitlines()
        why = why[-1][:80] if why else ("administrator prompt declined or refused"
                                        if not admin else "refused")
        return False, (f"console clock not set ({why}); run in a PowerShell opened as "
                       f"administrator: {shown}")
    return True, f"console clock set to {stamp} UTC{how}"


def set_console_clock(epoch):
    """Step this console's own clock to epoch (UTC). Returns (ok, message).

    Linux: root is needed, so this tries passwordless sudo: chrony's settime
    when chrony is installed (it is the time source for the radios, and
    timedatectl refuses while it runs), else date. When sudo wants a password
    the message carries the command to run in a terminal instead.

    Windows: PowerShell Set-Date, through the administrator (UAC) prompt when
    this console is not already elevated; see _set_console_clock_windows."""
    import shutil, subprocess
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(epoch))
    if sys.platform == "win32":
        return _set_console_clock_windows(stamp)
    env = dict(os.environ, TZ="UTC")
    if shutil.which("chronyc"):
        cmds = [["sudo", "-n", "chronyc", "settime", stamp],
                ["sudo", "-n", "chronyc", "makestep"]]
        shown = f"sudo chronyc settime '{stamp}' && sudo chronyc makestep"
    else:
        cmds = [["sudo", "-n", "date", "-u", "-s", stamp]]
        shown = f"sudo date -u -s '{stamp}'"
    for c in cmds:
        try:
            r = subprocess.run(c, capture_output=True, text=True, timeout=10, env=env)
        except Exception as e:
            return False, f"console clock not set ({e}); run in a terminal: {shown}"
        if r.returncode != 0:
            why = (r.stderr or r.stdout).strip().splitlines()
            why = why[-1][:80] if why else "refused"
            return False, f"console clock not set ({why}); run in a terminal: {shown}"
    return True, f"console clock set to {stamp} UTC"

# time units for the start/stop editor; the schedule itself is always seconds
_UNITS = {"ms": 0.001, "s": 1.0, "min": 60.0, "hr": 3600.0}
_UNIT_ORDER = ["ms", "s", "min", "hr"]


def _to_seconds(value, unit) -> float:
    return float(value or 0) * _UNITS.get(unit, 1.0)


def _best_unit(seconds):
    """Pick a readable (value, unit) for a time in seconds: whole hours or
    minutes when it divides cleanly, milliseconds below a second, else seconds."""
    s = float(seconds or 0)
    if s == 0:
        return 0.0, "s"
    for u in ("hr", "min"):
        q = s / _UNITS[u]
        if q >= 1 and abs(q - round(q)) < 1e-9:
            return q, u
    if s < 1.0:
        return s / _UNITS["ms"], "ms"
    return s, "s"


_NICE_TICKS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600,
               7200, 10800, 21600, 43200]


def _nice_tick(raw_s: float) -> float:
    """A readable ruler tick interval (seconds) at least raw_s."""
    for t in _NICE_TICKS:
        if t >= raw_s:
            return float(t)
    return float(_NICE_TICKS[-1])


def _today_utc() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def _utc_now_hms() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")


# where scenario spec JSONs live on the radio, relative to the service's working
# directory (the playback panel's scenario picker starts here too)
SCENARIO_DIR = "scenarios/json"


def _parse_address(text):
    """A planned radio's address, 'ip:port' or 'ip', as (ip, port) with the
    service's default command port (47810), or None if it does not parse."""
    t = (text or "").strip()
    if not t:
        return None
    host, _, port = t.rpartition(":") if ":" in t else (t, "", "")
    try:
        p = int(port) if port else 47810
    except ValueError:
        return None
    if not host or not 0 < p < 65536:
        return None
    return host, p


def _playback_path(params: dict) -> str:
    # the playback file's path on the radio; older plans used "name"
    return str(params.get("path") or params.get("name") or "")


def _params_view(params: dict) -> dict:
    """The params as the read-only preview shows them: an inline scenario spec
    is folded to one line so the preview stays readable."""
    out = dict(params or {})
    if isinstance(out.get("spec"), dict):
        n = ss.summarize(out["spec"]).get("signals", 0)
        out["spec"] = (f"<inline scenario JSON: {n} signal(s), "
                       f"{len(ss.spec_blob(out['spec']))} bytes>")
    return out


def _step_detail(step: dict) -> str:
    # a short note after the technique name: the file an IQ playback plays, the
    # recording a replay sends, and any RF center, TX atten or RX gain the step
    # sets
    p = step.get("params") or {}
    bits = []
    if mf.is_location_task(step):
        t = mf.trigger_block(step)
        bits.append("while inside" if t.get("mode") != "dwell"
                    else f"dwell {float(t.get('dwell_s') or mf.DEFAULT_DWELL_S):g} s")
        bits.append(f"p{mf.step_priority(step)}")
    if step.get("technique") == "set_rate":
        hz = p.get("sample_rate")
        return f" ({rate_text(hz)})" if hz else " (no rate)"
    if step.get("technique") == "iq_playback":
        f = _playback_path(p)
        bits.append(os.path.basename(f) if f else "no file")
    elif step.get("technique") in ("scenario_json", "scenario_decode"):
        lab = ss.step_spec_label(p)
        bits.append(lab if lab else "no scenario JSON")
    elif step.get("technique") == "replay":
        bits.append(p.get("name") or "no recording")
    hz = p.get("center_hz") or p.get("retune_hz")
    if hz:
        bits.append(f"{float(hz) / 1e6:.6g} MHz")
    if p.get("tx_atten_db") not in (None, ""):
        bits.append(f"atten {float(p['tx_atten_db']):g} dB")
    if p.get("rx_gain_index") not in (None, ""):
        bits.append(f"RX gain {p['rx_gain_index']}")
    pl = ports.label(*ports.step_ports(step))
    if pl:
        bits.append(pl)
    return f" ({', '.join(bits)})" if bits else ""


# frequency fields in the step editor: shown in a unit, stored in Hz
_HZ_UNITS = {"Hz": 1.0, "kHz": 1e3, "MHz": 1e6, "GHz": 1e9}


def _best_hz_unit(hz, blank_unit="MHz"):
    if hz in (None, ""):
        return None, blank_unit
    a = abs(float(hz))
    for u in ("GHz", "MHz", "kHz"):
        if a >= _HZ_UNITS[u]:
            return float(hz) / _HZ_UNITS[u], u
    return float(hz), ("kHz" if a == 0 else "Hz")


# select widgets cannot carry None as an option key; this stands in for it
_BLANK = "__blank__"


def _new_step_id() -> str:
    return "s" + uuid.uuid4().hex[:8]


def _fmt_time(t: float) -> str:
    """Elapsed plan time as a stopwatch reads it: m:ss, h:mm:ss from an hour,
    with a tenth of a second only when the time has one (0:07.5)."""
    t = round(max(0.0, float(t)), 1)
    whole = int(t)
    tenth = int(round((t - whole) * 10))
    h, rem = divmod(whole, 3600)
    m, s = divmod(rem, 60)
    out = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    return out + (f".{tenth}" if tenth else "")


_DUR_UNITS = {"ms": 0.001, "s": 1.0, "sec": 1.0, "secs": 1.0, "second": 1.0,
              "seconds": 1.0, "m": 60.0, "min": 60.0, "mins": 60.0, "minute": 60.0,
              "minutes": 60.0, "h": 3600.0, "hr": 3600.0, "hrs": 3600.0,
              "hour": 3600.0, "hours": 3600.0}
_DUR_TOKEN = re.compile(r"(\d+(?:\.\d*)?|\.\d+)\s*([a-z]*)")


def parse_duration(text):
    """A time typed the way people say it, as seconds, or None if it does not
    read. m:ss or h:mm:ss (2:30, 1:05:00), a number with a unit (90s, 5m,
    1.5h, 500ms, 1h 30m), or a bare number of seconds (90). A leading T+ is
    ignored, so the start of a relative step reads the same."""
    t = str(text if text is not None else "").strip().lower()
    t = re.sub(r"^t\s*\+\s*|^\+", "", t).strip()
    if not t:
        return None
    if ":" in t:
        parts = t.split(":")
        if len(parts) > 3 or any(not re.fullmatch(r"\d+(\.\d*)?", p.strip())
                                 for p in parts):
            return None
        vals = [float(p) for p in parts]
        if any(v >= 60 for v in vals[1:]):
            return None
        sec = 0.0
        for v in vals:
            sec = sec * 60.0 + v
        return sec
    pos, sec = 0, 0.0
    for m in _DUR_TOKEN.finditer(t):
        if t[pos:m.start()].strip(" ,"):
            return None
        unit = m.group(2) or "s"
        if unit not in _DUR_UNITS:
            return None
        sec += float(m.group(1)) * _DUR_UNITS[unit]
        pos = m.end()
    if pos == 0 or t[pos:].strip():
        return None
    return sec


def _fmt_dur(t: float) -> str:
    """A duration in short words (30s, 2:00, 1.5 h) for the planning chips."""
    t = float(t)
    if t >= 3600:
        return f"{t / 3600:.1f} h"
    if t >= 60:
        return f"{int(t // 60)}:{int(t % 60):02d}"
    return f"{t:g}s"


def relative_window_s(span_s: float, has_steps: bool) -> float:
    """How much elapsed time the relative timeline shows for a plan whose last
    step stops at span_s: an hour when empty, else the plan plus a little
    room (5 %, at least a minute) rounded up to 5 min, never under an hour."""
    if not has_steps:
        return max(EMPTY_WINDOW_S, float(span_s))
    span = max(0.0, float(span_s))
    want = span + max(60.0, span * 0.05)
    return max(MIN_WINDOW_S, math.ceil(want / WINDOW_ROUND_S) * WINDOW_ROUND_S)


# multi-step (bulk) editing: the settings several selected steps share
MIXED = object()        # a shared field whose value differs between the steps
_BULK_SKIP_KEYS = {"source", "path", "name", "spec_path", "spec"}   # set by pickers


def bulk_fields(steps):
    """The catalog fields a set of steps can be bulk-edited on, in catalog
    order: [(field, [step ids that have it], common value or MIXED)]. A field
    joins when every step that has it agrees on its kind; for a select, only
    the choices every such step offers are kept (none left: field skipped).
    JSON fields and the file/scenario/recording picks are left to the single
    editor."""
    by_key, order = {}, []
    for s in steps:
        entry = catalog.get(s.get("technique"))
        for f in (entry.fields if entry else []):
            if f.kind == "json" or f.key in _BULK_SKIP_KEYS:
                continue
            if f.key not in by_key:
                by_key[f.key] = []
                order.append(f.key)
            by_key[f.key].append((s, f))
    out = []
    for key in order:
        pairs = by_key[key]
        kinds = {f.kind for _, f in pairs}
        if len(kinds) != 1:
            continue
        f0 = pairs[0][1]
        if f0.kind == "select":
            vals = [v for v, _ in f0.choices]
            for _, f in pairs[1:]:
                ok = {v for v, _ in f.choices}
                vals = [v for v in vals if v in ok]
            if not vals:
                continue
            labels = dict(f0.choices)
            f0 = catalog.Field(f0.key, f0.label, f0.kind, f0.default,
                               choices=[(v, labels[v]) for v in vals],
                               optional=all(f.optional for _, f in pairs),
                               help=f0.help, blank=f0.blank)
        values = [(s.get("params") or {}).get(key, f.default) for s, f in pairs]
        common = values[0] if all(v == values[0] for v in values) else MIXED
        out.append((f0, [s.get("id") for s, _ in pairs], common))
    return out


def apply_bulk(steps, params=None, priority=None, duration_s=None):
    """Write params (key: value) into every step whose technique has that
    field, and a priority and duration into every timed step. Returns the
    number of steps changed."""
    params = params or {}
    changed = 0
    for s in steps:
        entry = catalog.get(s.get("technique"))
        keys = {f.key for f in (entry.fields if entry else [])}
        hit = False
        for k, v in params.items():
            if k in keys:
                s.setdefault("params", {})[k] = v
                hit = True
        if not mf.is_location_task(s):
            if priority is not None:
                s["priority"] = max(1, min(9, int(priority)))
                hit = True
            if duration_s is not None:
                st = mf.step_start_s(s)
                s["schedule"] = {"start_s": st, "stop_s": round(st + float(duration_s), 3)}
                hit = True
        changed += hit
    return changed


# Where the plan is kept between page loads and GUI restarts.
AUTOSAVE_PATH = (os.environ.get("DPMG_MISSION_AUTOSAVE")
                 or os.path.join(os.path.expanduser("~"), ".dpmg",
                                 "mission_autosave.json"))


def _fresh_mission():
    # a fresh plan is relative: step times are elapsed from Run mission (T+),
    # which suits the 30 to 60 min missions flown. "Start all at planned UTC
    # times" under Mission radios switches a plan to the 24 h UTC clock.
    return mf.new_mission("new mission")


class _PlanSession:
    """One plan per GUI process (per LinkManager). The plan, its audit log and
    its runner live here rather than in the page, so a browser refresh or a
    second browser shows the same plan, and a run that is going keeps going
    and stays stoppable from any page. Open pages register a hook the runner
    calls to redraw them. The plan is written to AUTOSAVE_PATH whenever it
    changes, so it also survives a restart of the GUI."""

    def __init__(self, mgr, path=None):
        self.mgr = mgr
        self.path = path or AUTOSAVE_PATH
        self.audit = AuditLog()
        self.pages = set()
        self.mission = self._load_or_new()
        self._saved = mf.dumps(self.mission)
        self.runner = MissionRunner(self.get_host, self.audit, speed=1.0,
                                    get_any_host=lambda rid: self.mgr.hosts.get(rid),
                                    on_change=self._notify)
        self.runner.plan_source = lambda: self.mission
        # location triggers: one engine per plan, armed by the
        # operator, fed by the aircraft multicast feed or a radio's GPS
        self.triggers = tg.TriggerEngine()
        self.motion = ms.MotionPlayer()       # simulated platform motion (checkouts)
        # One aircraft feed listener for the whole GUI: the operator header's
        # panel, the planner's feed row and the engineering Aircraft feed panel
        # all drive link_tab.ACFT, so what one starts the others see. Without
        # the web package (tests, the dev page) the session has its own. Group,
        # port, interface and format come from the saved feed settings.
        self.feeds = shared_feeds()           # one listener per saved aircraft
        self.listener = self.feeds.primary
        self.feed = PositionFeed(mgr, self.listener, motion=self.motion)
        self._feeds_by = {}                   # aircraft name: PositionFeed
        self.triggers_armed = False
        self.prompted = set()                 # step ids with a trigger prompt open
        self.confirmed_once = set()           # location tasks confirmed once this arming
        self.last_fix = None
        self._trig_t = 0.0
        # the map view, shared by every open page like the plan itself: the
        # selected radio and which radios the map draws (map_element.shown_radios).
        # view_rev counts changes so a page knows to redraw its rows and bar.
        self.view = {"radio": None, "show": me.SHOW_SELECTED}
        self.view_rev = 0

    # the shared map view

    def select_radio(self, radio_id) -> bool:
        """Make this radio the selected one (None clears). Returns True when
        it changed; the caller notifies the pages."""
        rid = radio_id or None
        if self.view.get("radio") == rid:
            return False
        self.view["radio"] = rid
        self.view_rev += 1
        return True

    def set_show(self, show) -> bool:
        """SHOW_SELECTED, SHOW_ALL or a list of radio ids."""
        if isinstance(show, (set, frozenset, tuple)):
            show = sorted(show)
        if self.view.get("show") == show:
            return False
        self.view["show"] = show
        self.view_rev += 1
        return True

    def toggle_show(self, radio_id, all_radios) -> bool:
        """Tick or untick one radio on the map. Starts from what the map shows
        now, so the first tick on "selected only" keeps the selected radio."""
        cur = me.shown_radios(self.view, all_radios)
        cur = set(all_radios) if cur is None else set(cur)
        if radio_id in cur:
            cur.discard(radio_id)
        else:
            cur.add(radio_id)
        return self.set_show(sorted(cur))

    def shown(self, all_radios):
        return me.shown_radios(self.view, all_radios)

    def _load_or_new(self):
        try:
            m = mf.load(self.path)
            self.audit.add("edit", f"reopened plan '{m.get('name', '?')}' "
                                   f"({len(m.get('steps', []))} step(s)) from "
                                   f"{self.path}")
            return m
        except Exception:
            return _fresh_mission()

    def is_online(self, radio_id):
        h = self.mgr.hosts.get(radio_id)
        if h is None:
            return False
        return bool(getattr(h, "connected", getattr(h, "session_open", False)))

    def get_host(self, radio_id):
        # only a radio with a live session; a planned radio that is offline (or
        # has gone stale) gets None, so the runner skips its steps and logs it
        return self.mgr.hosts.get(radio_id) if self.is_online(radio_id) else None

    def _notify(self):
        for hook in list(self.pages):
            try:
                hook()
            except Exception:
                self.pages.discard(hook)      # its page is gone

    # position feed and triggers

    # the first aircraft's feed settings, as the registry holds them now. The
    # interface is the one the listener joins the group on: 0.0.0.0 = the
    # default route (a feed from the network); 127.0.0.1 for a player on this host

    def _first_entry(self) -> dict:
        return self.feeds.entry(self.feeds.first_name()) or feed_settings.normalize_entry({})

    @property
    def pos_group(self):
        return self._first_entry()["group"]

    @property
    def pos_port(self):
        return int(self._first_entry()["port"])

    @property
    def pos_iface(self):
        return self._first_entry()["iface"]

    @property
    def pos_format(self):
        return self._first_entry()["format"]

    @property
    def listening(self) -> bool:
        return self.listener is not None and bool(self.listener.running)

    # the aircraft feeds: one listener per saved aircraft, a plan radio bound
    # to one of them (feed_binding.py), the first by default

    def aircraft_names(self) -> list:
        return self.feeds.names()

    def _follow_primary(self):
        """The first aircraft can change on a settings save; the session's
        own listener and feed stay the first one's."""
        if self.feeds.primary is not self.listener:
            self.listener = self.feeds.primary
            self.feed.listener = self.listener

    def listener_for(self, name):
        return self.feeds.listener_for(name)

    def feed_for(self, name):
        """The position source of one aircraft (its feed, else a radio GPS,
        else the simulated motion while it plays, as self.feed does)."""
        rn = self.feeds.resolve(name)
        lst = self.feeds.get(rn)
        if rn is None or lst is None or rn == self.feeds.first_name():
            return self.feed             # the first aircraft is the session's own feed
        pf = self._feeds_by.get(rn)
        if pf is None or pf.listener is not lst:
            pf = self._feeds_by[rn] = PositionFeed(self.mgr, lst, motion=self.motion,
                                                   name=rn if len(self.feeds) > 1 else None)
        return pf

    def fix_of(self, name):
        try:
            return self.feed_for(name).current()
        except Exception:
            return None

    def radio_aircraft(self, rid):
        """The aircraft this plan radio follows (its field, else the first)."""
        return fb.radio_aircraft(self.mission, rid, self.aircraft_names())

    def aircraft_shown(self, name):
        """The name the page prints for an aircraft: nothing when it is the
        only one and still has the group:port name it got by default."""
        e = self.feeds.entry(name) or {}
        if len(self.feeds) == 1 and name == feed_settings.default_name(e.get("group"), e.get("port")):
            return None
        return name

    def listening_for(self, name) -> bool:
        return self.feeds.running(self.feeds.resolve(name))

    def start_listener(self, name=None, changes=None):
        """Listen to one aircraft's position feed (the first when no name is
        given). changes holds only the fields the page's user changed
        (PanelFields.edits); they are saved on the settings as they are now,
        so a page whose fields hold older values puts nothing back. A
        listener already running is left alone unless its group, port or
        interface changed, and then it starts again on the new ones."""
        try:
            name, e, changed = self.feeds.update(name, changes or {})
        except OSError:
            name = self.feeds.resolve(name)
            e, changed = self.feeds.entry(name), True
        self._follow_primary()
        if self.feeds.running(name) and not changed:
            return
        if not self.feeds.running(name):
            self.feeds.start(name)
        self.audit.add("trigger", f"aircraft {name}: listening for its position on "
                                  f"{e['group']}:{e['port']} via {e['iface']} ({e['format']})")

    def stop_listener(self, name=None):
        name = self.feeds.resolve(name)
        if self.feeds.running(name):
            self.feeds.stop(name)
            self.audit.add("trigger", f"aircraft {name}: stopped listening for its position")

    def arm_triggers(self, on: bool):
        on = bool(on)
        if on == self.triggers_armed:
            return
        self.triggers_armed = on
        self.triggers.reset()
        self.confirmed_once = set()          # a new arming needs fresh confirms
        n_auto = sum(1 for s in self.mission.get("steps", [])
                     if mf.is_location_task(s) and mf.trigger_block(s).get("confirm") == "auto")
        self.audit.add("trigger", "location triggers ARMED: entering a start "
                                  "area asks for a confirm before keying, and a task "
                                  "confirmed once then keys without asking"
                                  + (f"; {n_auto} automatic task(s) key on entry without asking"
                                     if n_auto else "")
                       if on else "location triggers off")
        if on:
            self._audit_held()
        # a plan of location tasks may never see Run mission, which is what sets
        # the plan's sample rate; arming sets it instead, before any key
        if on and not self.runner.running:
            asyncio.get_event_loop().create_task(self.runner._apply_start_rates(self.mission))

    async def stop_all(self, hosts):
        """STOP ALL TX from either page: hold each step the platform is inside and
        disarm the triggers first, so nothing keys again until someone arms them and
        the platform has left, then stop the run and every radio."""
        fb.hold_all(self.triggers, self.mission, self.aircraft_names(), self.fix_of)
        self.arm_triggers(False)
        sent = await self.runner.stop_all(hosts)
        self._audit_held()
        return sent

    async def stop_step(self, step):
        """Stop this step: that step off, the run and the triggers carry on. It
        stays off until the platform leaves its area, and a task that asks for a
        confirm asks again on its next entry. Returns True when it was running."""
        sid = step.get("id")
        fb.hold(self.triggers, self.mission, step, self.aircraft_names(), self.fix_of)
        self.confirmed_once.discard(sid)
        stopped = await self.runner.stop_step_by_hand(step)
        if mf.step_location(step):
            self.triggers.mark_stopped(sid, rearm=mf.is_location_task(step))
        self._audit_held()
        return stopped

    def _audit_held(self):
        held = self.triggers.held()
        if held:
            self.audit.add("trigger", f"held until the platform leaves the area: {', '.join(held)}")

    def trigger_tick(self):
        """Once per second from any open page: read the position, run the
        engine when armed. Returns (fix, entries). Entries are handed to the
        first page that asks within half a second, so two pages do not both
        prompt for one entry."""
        self.feeds.follow_file()
        self._follow_primary()
        names = self.aircraft_names()
        fix = self.feed.current()
        self.last_fix = fix
        now = time.monotonic()
        if now - self._trig_t < 0.5:
            return fix, []
        self._trig_t = now
        # a step the runner finished on its own (planned length reached) is
        # done for the engine too
        waiting = {w[0].get("id") for w in self.runner._waiting.values()}
        by_id = {s.get("id"): s for s in self.mission.get("steps", [])}
        for sid, st in list(self.triggers._st.items()):
            if st["started"] and not st["stopped"] and sid not in self.runner.active_steps \
                    and sid not in waiting:
                self.triggers.mark_stopped(sid, rearm=mf.is_location_task(by_id.get(sid) or {}))
        for sid in fb.release_left(self.triggers, self.mission, names, self.fix_of):
            self.audit.add("trigger", f"{sid}: no longer held, the platform left its area")
        if not self.triggers_armed:
            self.triggers.enabled, self.triggers.reason = fb.status(self.triggers, names, self.fix_of)
            return fix, []
        # each radio's steps run on the position of the aircraft its row names
        return fix, fb.tick_triggers(self.triggers, self.mission, names, self.fix_of)

    def autosave(self):
        """Write the plan if it changed since the last write. Atomic (temp file
        then rename), so a crash mid-write never leaves half a plan."""
        text = mf.dumps(self.mission)
        if text == self._saved:
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, self.path)
            self._saved = text
        except OSError:
            pass                              # read-only home: refresh still works


def shared_listener():
    """The GUI's one aircraft feed listener (link_tab.ACFT) when the web
    package is loaded, else a listener of this module's own, configured from
    the saved feed settings either way."""
    global _OWN_LISTENER
    try:
        from dpmg.web.link_tab import ACFT as _acft
        return _acft
    except Exception:
        pass
    if _OWN_LISTENER is None:
        _OWN_LISTENER = MulticastPosListener()
        feed_settings.apply(_OWN_LISTENER)
    return _OWN_LISTENER


def shared_feeds():
    """The GUI's aircraft feed registry (link_tab.FEEDS) when the web package
    is loaded, else one of this module's own from the saved settings."""
    global _OWN_FEEDS
    try:
        from dpmg.web.link_tab import FEEDS as _feeds
        return _feeds
    except Exception:
        pass
    if _OWN_FEEDS is None:
        from dpmg.link.aircraft_feeds import AircraftFeeds
        _OWN_FEEDS = AircraftFeeds()
    return _OWN_FEEDS


def radio_aircraft_live(serial):
    """The aircraft a radio is bound to in an open plan session (its row's
    field, else the first aircraft), or None when no plan has the radio."""
    for sess in list(_SESSIONS.values()):
        try:
            r = next((r for r in sess.mission.get("radios", []) if r.get("id") == serial), None)
            if r is not None:
                return fb.aircraft_of(r, sess.aircraft_names())
        except Exception:
            continue
    return None


_OWN_LISTENER = None
_OWN_FEEDS = None
_SESSIONS: dict = {}


def live_plan():
    """(mission, keyed step ids) of the open plan session: the plan shown in the
    planner and the steps keyed right now. (None, empty set) until a planner
    page has been opened since the ground station started. For the engineering
    Payload page's per-step EIRP table; read only."""
    for sess in list(_SESSIONS.values()):
        try:
            return sess.mission, set(sess.runner.active_steps)
        except Exception:
            continue
    return None, set()

# the two views of the one planner
MODE_FLIGHT = "flight"              # /mission: the live feed state, no simulated motion
IQ_DIR = "data/waveforms"            # the radio's IQ folder, relative to the service's folder
IQ_EXTS = (".iq.bin", ".c64")         # what list_waveforms counts as an IQ file
MODE_ENGINEERING = "engineering"    # the engineering tab: everything, motion included
MODES = (MODE_FLIGHT, MODE_ENGINEERING)


def _session_for(mgr) -> _PlanSession:
    sess = _SESSIONS.get(id(mgr))
    if sess is None or sess.mgr is not mgr:
        sess = _SESSIONS[id(mgr)] = _PlanSession(mgr)
    return sess


def select_radio(mgr, radio_id) -> bool:
    """Select a radio in the planner from another page (the link header's
    radio label). Nothing happens when no planner page has opened a session,
    so the operator page never starts one by itself."""
    sess = _SESSIONS.get(id(mgr))
    if sess is None or sess.mgr is not mgr:
        return False
    if sess.select_radio(radio_id):
        sess._notify()
    return True


def radio_color(mgr, radio_id):
    """The planner's colour for a radio, or None when there is no plan
    session or the radio is not in the plan."""
    sess = _SESSIONS.get(id(mgr))
    if sess is None or sess.mgr is not mgr:
        return None
    return me.radio_colors(sess.mission).get(radio_id)


def atten_not_reset(mgr) -> list:
    """Radios whose RX attenuator the last STOP ALL TX did not get back to 120 dB,
    each with the reason; empty with no plan session."""
    sess = _SESSIONS.get(id(mgr))
    return list(sess.runner.atten_not_reset) if sess is not None else []


def stop_unconfirmed(mgr) -> list:
    """Radios that did not answer the last STOP ALL TX and are still being
    retried; empty with no plan session."""
    sess = _SESSIONS.get(id(mgr))
    return list(sess.runner.stop_unconfirmed) if sess is not None else []


async def stop_all_tx(mgr, hosts) -> int:
    """STOP ALL TX for the operator page. With no plan session open there is no
    run or trigger to stop, only the radios."""
    sess = _SESSIONS.get(id(mgr))
    return await (sess.stop_all(hosts) if sess is not None else send_stop_tx(hosts))


PAGE_CSS = """
.mp-root { max-width: 1800px; margin: 0 auto; font-size: 13px; }
.mp-root .q-card { box-shadow: none; border: 1px solid #2c322c; }
.mp-root .mp-section { border-top: 1px solid #2c322c; margin-top: 8px; padding-top: 6px;
                       width: 100%; }
.mp-root .mp-section-title { font-size: 11px; font-weight: 700; color: #8d968b;
                             text-transform: uppercase; letter-spacing: .08em; }
.mp-root .mp-pill { padding: 3px 12px; font-weight: 600;
                    font-size: 12px; white-space: nowrap; }
.mp-root .mp-cat-card { padding: 4px 6px; cursor: pointer; }
.mp-root .mp-cat-card:hover { background: #1f241f; }
.mp-root .mp-lane { background: #101310; border: 1px solid #2c322c; }
.mp-root .mp-lane::after { content:''; position:absolute; top:0; bottom:0; left:var(--cx,-10px); width:1px;
    background:#d0a644; opacity:.55; pointer-events:none; z-index:3; }
.mp-root .mp-cursor-read { font:11px 'JetBrains Mono',Menlo,monospace; color:#d0a644; min-width:72px; text-align:right; }
.mp-root .mp-bench { border:1px dashed #4a5340; background:#111410; padding:6px 8px; margin-bottom:6px; }
.mp-root .mp-bench-title { font:600 11px 'JetBrains Mono',Menlo,monospace; letter-spacing:.14em; color:#d0a644; }
.mp-root .mp-bench-zone { display:flex; flex-wrap:wrap; gap:6px; min-height:40px; align-items:center; padding:4px 0; }
.mp-root .mp-bench-empty { color:#5d665a; font-size:11px; letter-spacing:.08em; text-transform:uppercase; }
.mp-root .mp-bench-chip { display:flex; align-items:center; gap:8px; padding:4px 4px 4px 8px; cursor:grab;
    font-size:11px; font-weight:600; }
.mp-root .mp-bench-dur { font:10px 'JetBrains Mono',Menlo,monospace; opacity:.85; }
.mp-root .mp-bench-x { color:inherit !important; }
.mp-root .mm-drop-hot { outline: 2px dashed #d0a644; outline-offset: -4px; }
.mp-root .mp-bar { background: #1a1e1a; border: 1px solid #2c322c;
                   padding: 4px 8px; }
.mp-root .mp-lane[data-rid] { user-select: none; cursor: crosshair; }
.mp-marquee { position: fixed; z-index: 9999; pointer-events: none;
              border: 1px dashed #d0a644; background: rgba(208,166,68,.12); }
.mp-root .mp-dirty .q-field__control { box-shadow: inset 0 0 0 2px #d0a644;
                                        background: rgba(208,166,68,.14); }
.mp-root .mp-step { z-index:1; line-height:1.25; }
.mp-root .mp-step-primary { z-index:5; }
.mp-root .mp-step-line { pointer-events:none; white-space:nowrap; overflow:hidden;
                         text-overflow:ellipsis; max-width:100%; }
/* view toolbar: Timeline / Map switch and the console, one 44 px row */
.mp-root .mp-toolbar { display:flex; flex-wrap:wrap; width:100%; margin:12px 0 8px; }
.mp-root .mp-seg { display:flex; border:1px solid var(--border); background:var(--raised); }
.mp-root .mp-seg-btn { display:flex; align-items:center; justify-content:center; gap:8px;
    min-width:140px; height:42px; padding:0 16px; cursor:pointer; user-select:none;
    color:var(--muted); background:var(--raised); font-size:12px; font-weight:600;
    letter-spacing:.08em; text-transform:uppercase; box-shadow:inset 0 -2px 0 transparent; }
.mp-root .mp-seg-btn + .mp-seg-btn { border-left:1px solid var(--border); }
.mp-root .mp-seg-btn .q-icon { font-size:20px; }
.mp-root .mp-seg-btn:hover { background:#1f241f; color:var(--text); }
.mp-root .mp-seg-btn.mp-seg-on { background:var(--surface); color:var(--accent);
    box-shadow:inset 0 -2px 0 var(--accent); }
.mp-root .mp-seg-btn:focus-visible { outline:1px solid var(--accent); outline-offset:-3px; }
.mp-root .mp-console { flex:1; min-width:0; display:flex; flex-wrap:wrap; align-items:center;
    gap:4px 10px; min-height:44px; padding:0 12px; background:var(--raised);
    border:1px solid var(--border); border-left:0; }
.mp-root .mp-clock { font:12px 'JetBrains Mono',Menlo,monospace; color:var(--text); letter-spacing:.04em; }
.mp-root .mp-clock .q-icon { font-size:15px; color:var(--muted); }
.mp-root .mp-console .q-chip { margin:0; }
.mp-root .mp-console-act { color:var(--accent) !important; font-size:11px; padding:0 0 0 10px;
    min-height:28px; border-left:1px solid var(--border); }
.mp-root .mp-console-act .q-icon { font-size:16px; }
.mp-root .mp-console-status { margin-left:auto; }
.mp-root .mp-status { font:600 11px 'JetBrains Mono',Menlo,monospace; letter-spacing:.06em;
    text-transform:uppercase; padding:2px 8px; border:1px solid currentColor; }
.mp-root .mp-status .q-icon { font-size:14px; }
.mp-root .mp-status-ok { color:#7fa650; border-color:#7fa65055; }
.mp-root .mp-status-warn { color:#e0a030; border-color:#e0a03055; }
.mp-root .mp-status-err { color:#e5484d; border-color:#e5484d77; background:#e5484d14; }
/* Step editor / Audit log: Quasar tabs drawn as the same segmented control */
.mp-root .mp-segtabs { background:transparent !important; border:0 !important; min-height:0; }
.mp-root .mp-segtabs .q-tabs__content { flex:0 0 auto; border:1px solid var(--border);
    background:var(--raised); overflow:hidden; }
.mp-root .mp-segtabs .q-tab { min-width:140px; min-height:42px; height:42px; padding:0 16px;
    color:var(--muted); background:var(--raised); font-size:12px; font-weight:600;
    letter-spacing:.08em; text-transform:uppercase; opacity:1; }
.mp-root .mp-segtabs .q-tab + .q-tab { border-left:1px solid var(--border); }
.mp-root .mp-segtabs .q-tab__content { gap:8px; min-width:0; }
.mp-root .mp-segtabs .q-tab__icon { font-size:20px; width:20px; height:20px; margin:0; }
.mp-root .mp-segtabs .q-tab__label { font-size:12px; font-weight:600; letter-spacing:.08em; line-height:1; }
.mp-root .mp-segtabs .q-tab:hover { background:#1f241f; color:var(--text); }
.mp-root .mp-segtabs .q-tab--active, .mp-root .mp-segtabs .q-tab--active:hover {
    background:var(--surface); color:var(--accent) !important; box-shadow:inset 0 -2px 0 var(--accent); }
.mp-root .mp-segtabs .q-tab__indicator { display:none; }
.mp-root .mp-segtabs .q-focus-helper { display:none; }
@media (max-width: 1100px) {
  .mp-root .mp-segtabs .q-tabs__content { flex:1 1 auto; }
  .mp-root .mp-segtabs .q-tab { flex:1; }
  .mp-root .mp-seg { width:100%; }
  .mp-root .mp-seg-btn { flex:1; }
  .mp-root .mp-console { flex-basis:100%; border-left:1px solid var(--border); border-top:0;
      padding:6px 12px; }
}
.mp-root .mp-selcount { font-size: 11px; color: #d0a644; font-weight: 600; }
"""

CURSOR_JS = """<script>
document.addEventListener('mousemove',function(e){
  const lane=e.target.closest&&e.target.closest('.mp-lane[data-total]');
  const rd=document.getElementById('tl-cursor');
  document.querySelectorAll('.mp-lane[data-total]').forEach(l=>{if(l!==lane)l.style.removeProperty('--cx');});
  if(!lane){if(rd)rd.textContent='';return;}
  const r=lane.getBoundingClientRect(),x=e.clientX-r.left;
  lane.style.setProperty('--cx',x+'px');
  if(!rd)return;
  const t=Math.max(0,Math.min(1,x/r.width))*parseFloat(lane.dataset.total);
  const h=Math.floor(t/3600),m=Math.floor(t%3600/60),s=Math.floor(t%60),p=n=>String(n).padStart(2,'0');
  rd.textContent=(lane.dataset.clock==='1'||h>0)?p(h)+':'+p(m)+':'+p(s):p(m)+':'+p(s);
});
// marquee: press on an empty part of a lane and drag a box across steps (and
// across lanes) to select them; shift/ctrl/cmd adds to the selection
(function(){
  let st=null,box=null;
  document.addEventListener('pointerdown',function(e){
    if(e.button!==0)return;
    const lane=e.target.closest&&e.target.closest('.mp-lane[data-rid]');
    if(!lane||e.target!==lane)return;
    st={x:e.clientX,y:e.clientY,add:e.shiftKey||e.ctrlKey||e.metaKey,moved:false};
  });
  document.addEventListener('pointermove',function(e){
    if(!st)return;
    const dx=e.clientX-st.x,dy=e.clientY-st.y;
    if(!st.moved&&Math.abs(dx)<5&&Math.abs(dy)<5)return;
    st.moved=true;
    if(!box){box=document.createElement('div');box.className='mp-marquee';document.body.appendChild(box);}
    box.style.left=Math.min(st.x,e.clientX)+'px';box.style.top=Math.min(st.y,e.clientY)+'px';
    box.style.width=Math.abs(dx)+'px';box.style.height=Math.abs(dy)+'px';
  });
  document.addEventListener('pointerup',function(e){
    if(!st)return;
    const s=st;st=null;
    if(box){box.remove();box=null;}
    if(!s.moved)return;
    window._mpMarqUntil=performance.now()+300;
    const x0=Math.min(s.x,e.clientX),x1=Math.max(s.x,e.clientX);
    const y0=Math.min(s.y,e.clientY),y1=Math.max(s.y,e.clientY);
    const lanes=[];
    document.querySelectorAll('.mp-lane[data-rid]').forEach(l=>{
      const r=l.getBoundingClientRect();
      if(r.bottom<y0||r.top>y1||r.width<=0)return;
      const tot=parseFloat(l.dataset.total),f=v=>Math.max(0,Math.min(1,(v-r.left)/r.width))*tot;
      lanes.push({rid:l.dataset.rid,t0:f(x0),t1:f(x1)});
    });
    if(lanes.length&&window.emitEvent)window.emitEvent('mp_marquee',{lanes:lanes,add:s.add});
  });
})();
</script>"""


def build_mission_tab(mgr, mode: str = MODE_FLIGHT):
    """The planner page body. mode picks the view: "flight" (the /mission view,
    the live feed state under the map and no simulated motion) or
    "engineering" (the same planner with every control, the simulated motion
    row included). One code path; the mode only decides which rows exist."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, not {mode!r}")
    engineering = mode == MODE_ENGINEERING
    sess = _session_for(mgr)
    me.mount()
    ui.add_head_html(me.head_html())
    ui.add_css(_theme.themed(PAGE_CSS))
    ui.add_css(_theme.TIMELINE_CSS)
    ui.add_head_html(CURSOR_JS)
    root = ui.element("div").classes("mp-root w-full")
    root.__enter__()
    map_ui = {"panel": None, "sig": None, "warn": None, "pos": None,
              "center": (34.2, -111.7), "zoom": 5.8}     # Arizona by default
    # per-page view state; the plan itself is sess.mission
    state = {
        "selected": None,          # primary selected step id (the one the editor shows)
        "sel": set(),              # all selected step ids (ctrl-click adds to this)
        "conflicts": [],           # last validate() result
        "drag": None,              # current drag payload
        "view_s": VIEW_S,          # timeline zoom: seconds shown across the width
        "homed": False,            # the first draw scrolls to where the plan is
        "scroll_cmd": None,        # one-shot scroll target for the next rebuild
        "radio_clock": {},         # serial: last gps_status result
        "scen_note": {},           # step id: the comm waveform's one-line summary
        "scen_note_lbl": {},       # step id: the label showing it in the editor
    }
    audit = sess.audit
    runner = sess.runner
    get_host = sess.get_host
    _is_online = sess.is_online

    def on_run_change():
        # the runner (same event loop) calls this through sess._notify, and so
        # does a map view change made on any page (a radio selected, the show
        # bar worked): the rows, the lanes, the map and the bar all follow
        if state.get("view_rev") != sess.view_rev:
            state["view_rev"] = sess.view_rev
            _refresh_radios()
            _refresh_show_bar()
        _refresh_timeline()
        _refresh_audit()
        _refresh_run_state()

    sess.pages.add(on_run_change)
    try:
        ui.context.client.on_disconnect(lambda: sess.pages.discard(on_run_change))
    except Exception:
        pass

    # helpers over the mission

    def mission():
        return sess.mission

    def lane_radios():
        """Radio ids to draw lanes for: the plan's radios plus any connected
        radio not yet in the plan (so you can drop onto a live radio)."""
        ids = [r["id"] for r in mission().get("radios", [])]
        for serial in mgr.hosts:
            # "@ip:port" is a manual connect still waiting for its serial
            if serial not in ids and not str(serial).startswith("@"):
                ids.append(serial)
        return ids

    def _plan_radio(radio_id):
        return next((r for r in mission().get("radios", [])
                     if r.get("id") == radio_id), None)

    def _tx_on(radio_id):
        # TX running on this radio, from its last status heartbeat
        st = getattr(mgr.hosts.get(radio_id), "status", None)
        return bool(st is not None and int(getattr(st, "flags", 0)) & Status.FLAG_TX_ACTIVE)

    def _radio_status(radio_id):
        """(text, ok) for a radio: offline, or its radio state and reference
        lock from the last status heartbeat. ok means it can run steps now."""
        if not _is_online(radio_id):
            return "offline", False
        st = getattr(mgr.hosts.get(radio_id), "status", None)
        rs = getattr(st, "radio_state", None)
        text = {0: "connected, not set up", 1: "ready, RX off", 2: "ready (RX running)",
                3: "fault"}.get(rs, "connected")
        if _tx_on(radio_id):
            text += ", TX ON"
        try:
            from dpmg.link.proto import Status
            ref_bit = Status.FLAG_REF_LOCKED
        except Exception:
            ref_bit = 1 << 3
        # a GPS disciplined clock says the fix too: locked with no fix is locked to
        # a free running 1 PPS, not to GPS (g40_clock.gpsdo_verdict)
        _cfg = getattr(mgr.hosts.get(radio_id), "config", None) or {}
        _ref = (_g40_clock.CLOCK_CHOICES.get(str((_cfg.get("clock") or {}).get("choice")
                                                 or _cfg.get("clock_source") or "")) or {}).get("ref")
        _lk = bool(st is not None and int(getattr(st, "flags", 0)) & ref_bit)
        if rs in (1, 2) and _ref in ("gpsdo", "host") and "gnss" in _cfg:
            text += ", " + _g40_clock.gpsdo_verdict(_ref, _lk, _cfg.get("gnss"))["text"]
        elif _lk:
            text += ", ref locked"
        return text, rs in (1, 2)

    def _steps_on(radio_id):
        return [s for s in mission()["steps"] if s.get("radio") == radio_id]

    def _radio_kind_address(radio_id, planned):
        """(kind, address) for the radio line: the plan's own values when the
        radio is in the plan, else what the link knows about the connected
        host, so a radio that is online but not planned yet still shows where
        it is. Either part is "" when nothing is known."""
        kind = (planned or {}).get("type", "") or ""
        address = (planned or {}).get("address", "") or ""
        host = mgr.hosts.get(radio_id)
        if host is not None:
            if not kind:
                ht = getattr(host, "host_type", None)
                kind = {0: "g40", 1: "x40"}.get(int(ht), "") if ht is not None else ""
            addr = tuple(getattr(host, "addr", ()) or ())
            if not address and len(addr) == 2:
                address = f"{addr[0]}:{addr[1]}"
        if not kind:
            # a host added by address whose announce never arrived reports no
            # type; the serial prefix (G40-0001, X40-0001) says what it is
            head = str(radio_id).split("-", 1)[0].lower()
            kind = head if head in ("g40", "x40") else ""
        return kind.upper(), address

    def _radio_color(radio_id):
        return me.radio_color(mission(), radio_id)

    def _is_selected_radio(radio_id):
        return sess.view.get("radio") == radio_id

    def _select_radio(radio_id):
        """A click on a radio (its row, its lane, the link header): the map
        follows it when the show bar is on "selected only"."""
        if sess.select_radio(radio_id):
            sess._notify()

    def _refresh_radios():
        radios_box.clear()
        with radios_box:
            ids = lane_radios()
            if not ids:
                ui.label("no radios yet: add one, or connect a radio").classes(
                    "text-xs text-gray-500")
            for rid in ids:
                r = _plan_radio(rid)
                text, ok = _radio_status(rid)
                online = _is_online(rid)
                n = len(_steps_on(rid))
                kind, address = _radio_kind_address(rid, r)
                acn = sess.aircraft_names()
                ac = fb.aircraft_of(r, acn) if len(acn) > 1 else None
                bits = [b for b in (kind, address, text,
                                    f"{n} step(s)" if n else "",
                                    f"aircraft {ac}" if ac else "")
                        if b]
                if r is None:
                    bits.append("not in the plan yet")
                color = ("text-emerald-600" if ok else
                         "text-amber-600" if online else "text-gray-400")
                rcol = _radio_color(rid)
                picked = _is_selected_radio(rid)
                # one block per radio: name and buttons on the first line, the
                # status under it, the start rate on its own line. The
                # left edge carries the radio's colour, the same one as its
                # lane and its areas on the map; the selected radio is tinted.
                # Clicking the block selects the radio.
                row_box = ui.column().classes("gap-0 w-full border rounded px-1 py-0.5 cursor-pointer") \
                    .style(f"border-left:5px solid {rcol};"
                           + (f"background:{rcol}1f;" if picked else "")) \
                    .tooltip("click to select this radio: the map shows its steps")
                row_box.on("click", lambda e, rid=rid: _select_radio(rid))
                with row_box:
                    with ui.row().classes("items-center gap-0 w-full no-wrap"):
                        ui.icon("circle").classes(f"text-[10px] {color} mr-1")
                        ui.label(rid).classes("text-xs font-mono truncate grow min-w-0"
                                              + (" font-bold" if picked else ""))
                        if picked:
                            ui.icon("check").classes("text-sm text-gray-700 mr-1") \
                                .tooltip("selected: the map follows this radio")
                        if r is not None and r.get("address") and not online:
                            ui.button(icon="link",
                                      on_click=lambda rid=rid: _connect_radio(rid)) \
                                .props("dense flat size=sm").tooltip(
                                f"connect to {r['address']}")
                        if r is not None and n:
                            ui.button(icon="delete_sweep",
                                      on_click=lambda rid=rid: _clear_radio(rid)) \
                                .props("dense flat size=sm").tooltip(
                                f"clear all {n} steps on this radio")
                        if r is not None:
                            ui.button(icon="close",
                                      on_click=lambda rid=rid: _remove_radio(rid)) \
                                .props("dense flat size=sm").tooltip("remove from the plan")
                    ui.chip(", ".join(bits), color=("green-1" if ok else "amber-1" if online
                                                      else "grey-3"),
                            text_color=("green-9" if ok else "amber-10" if online else "grey-8")) \
                        .props("dense").classes("text-[10px]").tooltip(", ".join(bits))
                    if r is not None:
                        _rate_select(r)
                        _clock_select(r)
                        _role_select(r)
                        _aircraft_select(r)
                # outside the row's block: a click inside the block selects the
                # radio and redraws this list, which would close its menu
                if r is not None and not online:
                    _bind_select(rid, r)

    def _bind_candidates(r):
        """Connected radios a planned row could stand for: online, not in the
        plan already, and of the row's type when the plan names one."""
        want = str(r.get("type") or "").lower()
        out = []
        for h in mgr.hosts.values():
            serial = str(h.serial)
            if not h.session_open or serial.startswith("@") or _plan_radio(serial) is not None:
                continue
            ht = getattr(h, "host_type", None)
            kind = {0: "g40", 1: "x40"}.get(int(ht)) if ht in (0, 1) else ""
            if want and kind and kind != want:
                continue
            out.append(serial)
        return sorted(out)

    def _bind_select(rid, r):
        """A planned radio that is not online: pick the connected radio it
        stands for. Its steps move with it, so a plan written for one radio
        runs on a stand-in without editing the file."""
        cands = _bind_candidates(r)
        if not cands:
            return

        def _bind(e, rid=rid):
            serial = e.value
            host = mgr.hosts.get(serial)
            pr = _plan_radio(rid)
            if not serial or host is None or pr is None or _plan_radio(serial) is not None:
                return
            pr["id"] = serial
            for s in _steps_on(rid):
                s["radio"] = serial
            if getattr(host, "addr", None):
                pr["address"] = f"{host.addr[0]}:{host.addr[1]}"
            audit.add("edit", f"planned radio {rid} bound to {serial} by hand")
            _revalidate()
            _refresh_all()
        ui.select({s: s for s in cands}, label="bind to a connected radio",
                  on_change=_bind).props("dense outlined").classes("w-full text-xs mb-1") \
            .tooltip("this planned radio is not online: pick the connected radio it stands for; "
                     "its steps move with it")

    def _aircraft_select(r):
        """Which aircraft's position this radio's location triggers follow.
        Shown with two or more aircraft on the saved list; the first is the
        default, and a plan from before the field keeps it."""
        names = sess.aircraft_names()
        if len(names) < 2:
            return
        cur = fb.aircraft_of(r, names)
        sel = ui.select({n: f"aircraft: {n}" for n in names}, value=cur).props("dense outlined") \
            .classes("w-full text-xs mt-0.5").tooltip(
            "the position feed this radio's location triggers run on; the Live feed "
            "rows, the operator console and the map name the aircraft the same way")

        def _set():
            fb.set_radio_aircraft(r, sel.value, names)
            audit.add("edit", f"radio {r.get('id')} follows aircraft {sel.value}")
            _revalidate()
            _refresh_all()
        sel.on_value_change(_set)

    def _clock_select(r):
        """The clock Run mission initializes this radio on if it is not
        initialized. A radio that is already up keeps its own clock."""
        _labels = _g40_clock.labels("x40" if str(r.get("type") or "g40").lower() == "x40" else "g40")
        _cur = r.get("clock_source") or "internal"
        if _cur not in _labels:
            _labels = dict(_labels, **{_cur: _cur})
        sel = ui.select(_labels, value=_cur,
                        label="clock if not set up").props("dense outlined") \
            .classes("w-full text-xs mt-0.5").tooltip(
            "used only when Run mission has to initialize this radio. The external 10 MHz "
            "choices need the 10 MHz on REF (the service sets the HMC7044 and refuses the "
            "init when it does not lock); 1 PPS only is the aircraft's pulse with the "
            "radio's own oscillator. An X40 has no gpsdo: x40_gpsdo is its own GPSDO "
            "through the host reference, whose lock cannot be read")

        def _set():
            r["clock_source"] = sel.value
            audit.add("edit", f"radio {r.get('id')} clock if not set up: {sel.value}")
            _revalidate()
            _refresh_all()
        sel.on_value_change(_set)

    def _rate_select(r):
        """The radio's start sample rate: the rate it is set to before the
        timeline runs. Blank leaves it at whatever it was brought up at. A
        scheduled Set sample rate step changes it later in the mission."""
        rates = catalog.valid_rates_for(r.get("type"))
        opts = {_BLANK: "start rate"}
        opts.update({str(hz): rate_text(hz, r.get("type")) for hz in rates})
        cur = r.get("sample_rate")
        val = str(int(cur)) if cur and str(int(cur)) in opts else _BLANK
        sel = ui.select(opts, value=val, label="start sample rate").props("dense outlined") \
            .classes("w-full text-xs mt-0.5").tooltip(
            "the rate the radio is set to before the timeline runs; blank keeps the "
            "radio's current rate")

        def _set():
            if sel.value == _BLANK:
                r.pop("sample_rate", None)
            else:
                r["sample_rate"] = int(sel.value)
            audit.add("edit", f"radio {r.get('id')} start rate "
                              f"{'cleared' if sel.value == _BLANK else opts[sel.value]}")
            _revalidate()
            _refresh_all()
        sel.on_value_change(_set)

    def _live_role(rid):
        # the role the connected radio reports (get_config), or None
        h = mgr.hosts.get(rid)
        cfg = getattr(h, "config", None) if h is not None else None
        return (cfg or {}).get("role")

    def _role_select(r):
        """The role the plan expects this radio in. Steps the role does not run
        are flagged by the validator; blank means no check. The radio's own role
        (set on its card) is shown, and a mismatch is called out."""
        if _role is None:
            return
        live = _live_role(r.get("id"))
        opts = {_BLANK: "role: no check" + (f" (radio is {_role.label(live)})" if live else "")}
        opts.update({k: f"role: {v}" for k, v in _role.choices().items()})
        cur = r.get("role")
        val = cur if cur in opts else _BLANK
        sel = ui.select(opts, value=val).props("dense outlined") \
            .classes("w-full text-xs mt-0.5").tooltip(
            "the role this radio has in the mission; steps it does not run are "
            "flagged. The radio itself refuses what its own role does not use, so "
            "set the same role on its card")
        if cur and live and live != cur:
            ui.label(f"radio is set to {_role.label(live)}").classes(
                "text-[10px] text-amber-700")

        def _set():
            if sel.value == _BLANK:
                r.pop("role", None)
            else:
                r["role"] = sel.value
            audit.add("edit", f"radio {r.get('id')} role "
                              f"{'cleared' if sel.value == _BLANK else sel.value}")
            _revalidate()
            _refresh_all()
        sel.on_value_change(_set)

    def _radios_sig():
        # what the radios list shows, so the timer only redraws on a change
        return tuple((rid, _radio_status(rid), len(_steps_on(rid)),
                      bool(_plan_radio(rid)),
                      (_plan_radio(rid) or {}).get("sample_rate"),
                      (_plan_radio(rid) or {}).get("clock_source"),
                      (_plan_radio(rid) or {}).get("role"), _live_role(rid),
                      _live_rx_ports().get(rid),
                      (_plan_radio(rid) or {}).get(fb.RADIO_KEY), tuple(sess.aircraft_names()))
                     for rid in lane_radios())

    def _add_radio_dialog():
        with dialog_host:
            with ui.dialog() as dlg, ui.card().classes("w-[28rem] gap-1"):
                ui.label("Add a radio to the plan").classes("text-base font-bold")
                ui.label("It does not have to be online now. Use its serial "
                         "(for example G40-0001) so it joins its lane by itself "
                         "when it connects.").classes("text-xs text-gray-500")
                rid_in = ui.input("serial or name").props("dense outlined") \
                    .classes("w-full")
                type_in = ui.select({"g40": "G40", "x40": "X40", "other": "other"},
                                    value="g40", label="type").props(
                    "dense outlined").classes("w-full")
                addr_in = ui.input("address (optional), ip or ip:port",
                                   placeholder="192.168.0.30:47810").props(
                    "dense outlined").classes("w-full")
                msg = ui.label("").classes("text-xs text-red-600")

                def _ok():
                    rid = (rid_in.value or "").strip()
                    if not rid or rid.startswith("@"):
                        msg.set_text("give the radio a serial or name")
                        return
                    if _plan_radio(rid) is not None:
                        msg.set_text(f"{rid} is already in the plan")
                        return
                    addr = (addr_in.value or "").strip()
                    if addr and _parse_address(addr) is None:
                        msg.set_text("address should be ip or ip:port")
                        return
                    dlg.submit({"id": rid, "type": type_in.value, "address": addr})

                with ui.row().classes("justify-end w-full gap-2"):
                    ui.button("Cancel", on_click=lambda: dlg.submit(None)).props("flat")
                    ui.button("Add this radio", on_click=_ok).props("color=primary")

        async def _wait():
            res = await dlg
            dlg.delete()
            if not res:
                return
            radio = {"id": res["id"], "techniques": ["tx", "rx"]}
            if res["type"] in ("g40", "x40"):
                radio["type"] = res["type"]
            if res["address"]:
                ip, port = _parse_address(res["address"])
                radio["address"] = f"{ip}:{port}"
            mission()["radios"].append(radio)
            audit.add("edit", f"planned radio {res['id']} added"
                              + (f" at {radio['address']}" if "address" in radio else ""))
            _revalidate()
            _refresh_all()
            if "address" in radio and not _is_online(radio["id"]):
                # an address given means connect now; the link button on its
                # row stays for a retry if it does not answer
                await _connect_radio(radio["id"])

        background_tasks.create(_wait(), name="mission-add-radio")

    async def _connect_radio(rid):
        """Dial a planned radio's address. The serial comes back in the radio's
        HELLO_ACK; if it differs from the planned name, the plan radio and its
        steps take the real serial so the lane binds to the live radio."""
        with dialog_host:
            r = _plan_radio(rid)
            addr = _parse_address((r or {}).get("address", ""))
            if r is None or addr is None:
                ui.notify(f"{rid} has no address to connect to", type="warning")
                return
            audit.add("radio", f"{rid}: {mgr.add_manual_host(*addr)}")
            _refresh_audit()
            host = None
            for _ in range(40):
                await asyncio.sleep(0.2)
                host = next((h for h in mgr.hosts.values()
                             if tuple(getattr(h, "addr", ()) or ()) == addr), None)
                if host is not None and host.session_open \
                        and not str(host.serial).startswith("@"):
                    break
            else:
                ui.notify(f"no answer from {addr[0]}:{addr[1]}; {rid} stays "
                          f"planned and offline", type="warning")
                _refresh_all()
                return
            serial = host.serial
            if serial != rid:
                if _plan_radio(serial) is not None:
                    ui.notify(f"{addr[0]}:{addr[1]} is {serial}, which is already "
                              f"in the plan", type="negative")
                    return
                r["id"] = serial
                for s in _steps_on(rid):
                    s["radio"] = serial
                audit.add("edit", f"planned radio {rid} is {serial}; renamed")
            ht = getattr(host, "host_type", None)
            if not r.get("type") and ht in (0, 1):
                r["type"] = {0: "g40", 1: "x40"}[int(ht)]
            ui.notify(f"connected to {serial}", type="positive")
            _revalidate()
            _refresh_all()

    def _remove_radio(rid):
        n = len(_steps_on(rid))
        if n:
            ui.notify(f"{rid} has {n} step(s); delete or move them first",
                      type="warning")
            return
        mission()["radios"] = [r for r in mission().get("radios", [])
                               if r.get("id") != rid]
        audit.add("edit", f"planned radio {rid} removed")
        # a removed radio leaves the map view too
        if sess.view.get("radio") == rid:
            sess.select_radio(None)
        show = sess.view.get("show")
        if isinstance(show, list) and rid in show:
            sess.set_show([r for r in show if r != rid])
        _revalidate()
        _refresh_all()

    async def _clear_radio(rid):
        # wipe every step on one radio (keeps the radio and its settings). Asks
        # first, like the clear-all button does.
        n = len(_steps_on(rid))
        if n == 0:
            ui.notify(f"{rid} has no steps", type="info")
            return
        with dialog_host:
            with ui.dialog() as dlg, ui.card():
                ui.label(f"Remove all {n} steps on {rid}?").classes("text-base")
                ui.label("The radio and its settings stay. This cannot be "
                         "undone.").classes("text-xs text-gray-500")
                with ui.row().classes("justify-end w-full"):
                    ui.button("Cancel", on_click=lambda: dlg.submit(False)) \
                        .props("flat")
                    ui.button("Clear this radio's steps", on_click=lambda: dlg.submit(True)) \
                        .props("color=negative")
        if not await dlg:
            return
        gone = {s.get("id") for s in _steps_on(rid)}
        mission()["steps"] = [s for s in mission()["steps"]
                              if s.get("radio") != rid]
        state["sel"] = {s for s in state.get("sel", set()) if s not in gone}
        if state.get("selected") in gone:
            state["selected"] = None
        audit.add("edit", f"cleared {n} steps on {rid}")
        _revalidate()
        _refresh_all()
        ui.notify(f"cleared {n} steps on {rid}", type="warning")

    def ensure_radio(radio_id):
        """Add a radio to the plan if a step is placed on it. Defaults its
        capability tags from the connected host when there is one."""
        # radio type from the link (HostType 0 = G40, 1 = X40); validate uses it
        # to check RX gain against that radio's index range
        ht = getattr(mgr.hosts.get(radio_id), "host_type", None)
        rtype = {0: "g40", 1: "x40"}.get(int(ht)) if ht is not None else None
        for r in mission()["radios"]:
            if r["id"] == radio_id:
                if rtype and not r.get("type"):
                    r["type"] = rtype
                return
        tags = ["tx", "rx"]
        radio = {"id": radio_id, "techniques": tags}
        if rtype:
            radio["type"] = rtype
        mission()["radios"].append(radio)

    def _default_start(radio_id):
        """Start for a step added from the catalog (not dropped at a time). On
        the UTC clock that is a minute from now, rounded up to 10 s, or after
        the radio's last step if that is later, so a new step is never already
        in the past; on a relative plan it is 0."""
        now = _now_spm() if _tl_clock() else None
        if now is None:
            return 0.0
        t = math.ceil((now + NEW_STEP_LEAD_S) / 10.0) * 10.0
        last = max((mf.step_stop_s(x) for x in mission()["steps"]
                    if x.get("radio") == radio_id), default=0.0)
        return min(max(t, last), mf.DAY_S - 1.0)

    def timeline_span():
        timed = [s for s in mission()["steps"] if not mf.is_location_task(s)]
        span = max((mf.step_stop_s(s) for s in timed), default=0.0)
        return relative_window_s(span, bool(timed))

    def _plan_stop_s():
        # where a run ends; timeline_span is the window drawn around it, a
        # little longer, and a location task is not part of a run
        return max((mf.step_stop_s(s) for s in mission()["steps"]
                    if not mf.is_location_task(s)), default=0.0)

    def add_step(technique, radio_id, start_s=None):
        entry = catalog.get(technique)
        if entry is None:
            ui.notify(f"unknown technique {technique}", type="negative")
            return
        ensure_radio(radio_id)
        if start_s is None:
            start_s = _default_start(radio_id)
        sid = _new_step_id()
        step = {
            "id": sid, "radio": radio_id, "technique": technique,
            "radiates": entry.radiates, "params": entry.fresh_params(),
            "schedule": {"start_s": round(float(start_s), 1),
                         "stop_s": round(float(start_s) + entry.default_dur, 1)},
        }
        mission()["steps"].append(step)
        state["selected"] = sid
        state["sel"] = {sid}
        audit.add("edit", f"added {technique} on {radio_id} at "
                          f"{_fmt_time(start_s)}")
        _revalidate()
        _refresh_all()
        if technique == "iq_playback":
            # a playback step is nothing without its file, so ask for it now
            background_tasks.create(_pick_playback_file(sid, new=True),
                                    name="mission-pick-playback")
        elif technique == "replay":
            # likewise a replay step needs its recording
            background_tasks.create(_pick_recording(sid, new=True),
                                    name="mission-pick-recording")
        elif technique in ("scenario_json", "scenario_decode"):
            # and a comm waveform needs its scenario JSON
            background_tasks.create(_pick_scenario(sid, new=True),
                                    name="mission-pick-scenario")

    def _find_step(sid):
        return next((s for s in mission()["steps"] if s.get("id") == sid), None)

    async def _pick_playback_file(sid, new=False):
        """Ask which file an IQ playback step plays: one of the radio's IQ files
        (list_waveforms, the same list the playback panel offers) or a typed
        path. Cancel on a step that was just added removes it again; on an
        existing step it leaves the step as it was. (A scenario JSON is the
        Comm waveform entry, _pick_scenario.)"""
        step = _find_step(sid)
        if step is None:
            return
        params = step.get("params") or {}
        host = mgr.hosts.get(step.get("radio"))
        with dialog_host:
            with ui.dialog().props("persistent") as dlg, \
                    ui.card().classes("w-[36rem] gap-1"):
                ui.label(f"IQ playback: choose the file on {step.get('radio')}") \
                    .classes("text-base font-bold")
                status = ui.label("").classes("text-xs text-gray-500")
                listing = ui.column().classes(
                    "w-full gap-0 max-h-72 overflow-y-auto")
                typed = ui.input("or type a path on the radio",
                                 value=_playback_path(params)).props(
                    "dense outlined").classes("w-full")

                def _use_typed():
                    p = (typed.value or "").strip()
                    if not p:
                        status.set_text("type a path first, or pick a file above")
                        return
                    dlg.submit(("file", p))

                with ui.row().classes("justify-end w-full gap-2"):
                    ui.button("Cancel", on_click=lambda: dlg.submit(None)) \
                        .props("flat")
                    ui.button("Use typed path", on_click=_use_typed) \
                        .props("color=primary")

                async def _list():
                    listing.clear()
                    if host is None:
                        status.set_text("radio not connected; type the file's "
                                        "path instead")
                        return
                    status.set_text("listing IQ files on the radio...")
                    try:
                        ack = await host.cmd("list_waveforms", {"kind": "iq"},
                                             timeout=10.0)
                    except Exception as e:
                        status.set_text(f"list failed: {e}; type the path instead")
                        return
                    r = ack.result or {}
                    if getattr(ack, "status", 1) != 0:
                        status.set_text(f"list failed: {r.get('err')}")
                        return
                    files = [dict(f, size=f"{f.get('bytes', 0) / 1e6:.1f} MB")
                             for f in r.get("files", [])]
                    if not files:
                        status.set_text("no IQ files at the top of the radio's IQ folder")
                        await _folders(IQ_DIR, listing)
                        return
                    note = f"{len(files)} IQ file(s), newest first"
                    if r.get("truncated"):
                        note += f" (newest {len(files)} of {r.get('total', '?')})"
                    status.set_text(note)
                    with listing:
                        for f in files:
                            ui.button(
                                f"{f['name']}   ({f['size']})",
                                on_click=lambda p=f["path"]: dlg.submit(("file", p))) \
                                .props("flat dense no-caps align=left").classes("w-full")
                    # list_waveforms reads the top of data/waveforms only, so the
                    # folders under it (flight/ and the like) are offered as well
                    await _folders(IQ_DIR, listing)

                async def _folders(path, into):
                    try:
                        ack = await host.cmd("list_dir", {"path": path, "exts": list(IQ_EXTS)},
                                             timeout=10.0)
                    except Exception:
                        return None
                    r = ack.result or {}
                    if getattr(ack, "status", 1) != 0:
                        return None
                    with into:
                        for d in r.get("dirs", []):
                            ui.button(f"{d}/", icon="folder",
                                      on_click=lambda q=f"{path}/{d}": _open(q)) \
                                .props("flat dense no-caps align=left").classes("w-full")
                    return r

                async def _open(path):
                    """One folder on the radio: its subfolders and IQ files."""
                    listing.clear()
                    status.set_text(f"listing {path} on the radio...")
                    with listing:
                        ui.button("back to the newest files", icon="arrow_back",
                                  on_click=_list).props("flat dense no-caps align=left")
                    r = await _folders(path, listing)
                    if r is None:
                        status.set_text(f"cannot list {path}; type the path instead")
                        return
                    files = r.get("files", [])
                    status.set_text(f"{path}: {len(files)} IQ file(s)"
                                    + (" (first 30 shown)" if r.get("truncated") else ""))
                    with listing:
                        for name, kb in files:
                            ui.button(f"{name}   ({kb / 1000:.1f} MB)",
                                      on_click=lambda q=f"{path}/{name}": dlg.submit(("file", q))) \
                                .props("flat dense no-caps align=left").classes("w-full")

                ui.timer(0.05, _list, once=True)

            result = await dlg
            dlg.delete()
            step = _find_step(sid)
            if step is None:
                return
            if not result:
                if new:
                    delete_step(sid)
                    ui.notify("IQ playback not added: no file chosen")
                return
            source, path = result
            p = step.setdefault("params", {})
            p["source"] = source
            p["path"] = path
            p.pop("name", None)
            audit.add("edit", f"{sid} plays {source} {path}")
            if source == "file":
                await _reconcile_file_rate(step, host)
            _revalidate()
            _refresh_all()

    async def _pick_scenario(sid, new=False):
        """Ask where a comm waveform step's scenario JSON comes from: a .json
        uploaded from this console (checked here, then kept inline in the step
        so the plan file carries it and the radio need not have it), one already
        on the radio under scenarios/json (list_dir, the folder the playback
        panel's picker starts in) or a typed path there. Cancel on a step that
        was just added removes it again; on an existing step it leaves the step
        as it was."""
        step = _find_step(sid)
        if step is None:
            return
        params = step.get("params") or {}
        host = mgr.hosts.get(step.get("radio"))
        with dialog_host:
            with ui.dialog().props("persistent") as dlg, \
                    ui.card().classes("w-[38rem] gap-1"):
                ui.label(f"Comm waveform: choose the scenario JSON for "
                         f"{step.get('radio')}").classes("text-base font-bold")
                ui.label("From this console: the file is read here and kept in "
                         "the plan; the radio builds the waveform when the step "
                         "runs").classes("text-xs font-bold text-gray-700 mt-1")
                up_note = ui.label("").classes("text-xs text-red-700")

                async def _on_upload(e):
                    try:
                        spec = ss.parse_upload(await e.file.text(), e.file.name)
                    except ValueError as ex:
                        up_note.set_text(str(ex))
                        return
                    dlg.submit(("console", spec, e.file.name))

                ui.upload(label="Upload scenario .json from this console",
                          auto_upload=True, max_files=1, on_upload=_on_upload) \
                    .props("accept=.json,application/json flat dense bordered") \
                    .classes("w-full")
                ui.separator().classes("my-1")
                ui.label(f"Already on the radio ({SCENARIO_DIR}): the radio reads "
                         f"its own disk").classes("text-xs font-bold text-gray-700")
                status = ui.label("").classes("text-xs text-gray-500")
                listing = ui.column().classes(
                    "w-full gap-0 max-h-60 overflow-y-auto")
                typed = ui.input("or type a path on the radio",
                                 value=str(params.get("spec_path") or "")).props(
                    "dense outlined").classes("w-full")

                def _use_typed():
                    p = (typed.value or "").strip()
                    if not p:
                        status.set_text("type a path first, or pick a file above")
                        return
                    dlg.submit(("radio", p))

                with ui.row().classes("justify-end w-full gap-2"):
                    ui.button("Cancel", on_click=lambda: dlg.submit(None)) \
                        .props("flat")
                    ui.button("Use typed path", on_click=_use_typed) \
                        .props("color=primary")

                async def _list():
                    listing.clear()
                    if host is None:
                        status.set_text("radio not connected; upload from this "
                                        "console or type the file's path")
                        return
                    status.set_text("listing scenario JSON files on the radio...")
                    try:
                        ack = await host.cmd(
                            "list_dir", {"path": SCENARIO_DIR, "exts": [".json"]},
                            timeout=10.0)
                    except Exception as e:
                        status.set_text(f"list failed: {e}; type the path instead")
                        return
                    r = ack.result or {}
                    if getattr(ack, "status", 1) != 0:
                        status.set_text(f"list failed: {r.get('err')}")
                        return
                    base = str(r.get("path", "")).rstrip("/")
                    files = [(n, f"{base}/{n}", kb) for n, kb in r.get("files", [])]
                    if not files:
                        status.set_text(f"no scenario JSON files in {SCENARIO_DIR} "
                                        f"on the radio")
                        return
                    note = f"{len(files)} scenario JSON file(s) in {SCENARIO_DIR}"
                    if r.get("truncated"):
                        note += " (list cut short, type the path for others)"
                    status.set_text(note)
                    with listing:
                        for name, path, kb in files:
                            ui.button(f"{name}   ({kb} KB)",
                                      on_click=lambda p=path: dlg.submit(("radio", p))) \
                                .props("flat dense no-caps align=left").classes("w-full")

                ui.timer(0.05, _list, once=True)

            result = await dlg
            dlg.delete()
            step = _find_step(sid)
            if step is None:
                return
            if not result:
                if new:
                    delete_step(sid)
                    ui.notify("Comm waveform not added: no scenario JSON chosen")
                return
            p = step.setdefault("params", {})
            if result[0] == "console":
                _, spec, name = result
                p.update({"source": "console", "spec": spec, "spec_name": name,
                          "spec_path": ""})
                audit.add("edit", f"{sid} comm waveform from console file {name} "
                                  f"({ss.summarize(spec).get('signals', 0)} signal(s), "
                                  f"{len(ss.spec_blob(spec))} B, kept in the plan)")
            else:
                _, path = result
                p.update({"source": "radio", "spec": None, "spec_name": "",
                          "spec_path": path})
                audit.add("edit", f"{sid} comm waveform from radio file {path}")
            state["scen_note"].pop(sid, None)
            _revalidate()
            _refresh_all()
            await _check_scenario(sid)

    async def _check_scenario(sid):
        """Write the one-line note under a comm waveform step: the spec's
        signal count, modulations and width (read from the spec itself when it
        is inline), and the radio's verdict from a check_only gen_scenario when
        the radio is online (does it fit at the radio's rate, any scaling note).
        For a file on the radio the count and modulations come back from the
        service with that verdict. Nothing is built or keyed."""
        step = _find_step(sid)
        if step is None:
            return
        p = step.get("params") or {}
        src = ss.step_spec_source(p)
        if not src:
            state["scen_note"].pop(sid, None)
            return
        local = ss.summarize(p["spec"]) if src == "console" else None
        host = get_host(step.get("radio"))
        if host is None:
            note = (ss.summary_text(local) if local
                    else "radio offline: the file is checked when it is online")
        else:
            args = {"check_only": True,
                    "match_radio_rate": bool(p.get("match_radio_rate", True))}
            if p.get("peak_fs") not in (None, ""):
                args["peak_fs"] = float(p["peak_fs"])
            try:
                if src == "console":
                    if ss.needs_inbox(p["spec"]):
                        # too big for one datagram: push it, then check the copy
                        stored = await host.send_file(f"mission_{sid}.json",
                                                      ss.spec_blob(p["spec"]), timeout=60.0)
                        args["spec_inbox"] = stored or f"mission_{sid}.json"
                    else:
                        args["spec"] = p["spec"]
                else:
                    args["spec_path"] = p["spec_path"]
                ack = await host.cmd("gen_scenario", args, timeout=20.0)
            except Exception as e:
                note = ((ss.summary_text(local) + "; ") if local else "") + f"check failed: {e}"
            else:
                r = getattr(ack, "result", None) or {}
                if getattr(ack, "status", 1) != 0:
                    note = (((ss.summary_text(local) + "; ") if local else "")
                            + f"radio refused the check: {r.get('err')}")
                else:
                    summ = local or r.get("summary")
                    if summ:
                        note = ss.summary_text(summ, r)
                    else:
                        # an older service without the summary: the verdict only
                        v = r.get("validation") or {}
                        note = ("checked on the radio: does not fit: " + "; ".join(v["errors"])
                                if v.get("errors") else
                                f"checked on the radio: fits, builds at "
                                f"{float(v.get('build_rate') or 0) / 1e6:g} MSps")
        state["scen_note"][sid] = note
        lbl = state["scen_note_lbl"].get(sid)
        try:
            lbl.set_text(note)
        except Exception:
            _refresh_editor()

    # lead before the step when a sample-rate change has to be inserted to match
    # a file, so the re-init finishes before the step keys
    _RATE_LEAD_S = 5.0

    async def _reconcile_file_rate(step, host):
        """Make the radio play an IQ file at the file's own rate (the
        default): if the file's native rate differs from the rate
        the radio is at when the step runs, set the radio to the file's rate
        (its start rate, or a Set sample rate step before this one). If the
        file's rate is not one the radio can take, resample the file down to the
        radio's rate instead. Needs the radio connected to read the file's rate;
        offline, it leaves the step alone."""
        if host is None:
            return
        path = (step.get("params") or {}).get("path") or ""
        rid = step.get("radio")
        rtype = (_plan_radio(rid) or {}).get("type")
        try:
            ack = await host.cmd("waveform_rate", {"path": path}, timeout=10.0)
        except Exception as e:
            ui.notify(f"could not read the file's rate: {e}", type="warning")
            return
        r = getattr(ack, "result", None) or {}
        native = r.get("rate")
        if not native:
            ui.notify("file rate is not marked; it will play at the radio's "
                      "current rate", type="warning")
            return
        native = float(native)
        start_s = mf.step_start_s(step)
        target = effective_rate_at(mission(), rid, start_s) or r.get("radio_rate")
        if target and abs(native - float(target)) <= native * 1e-3:
            return                    # already matched
        if catalog.is_valid_rate(native, rtype):
            # match the radio to the file (the default). If no rate change comes
            # before this step, set the radio's start rate; otherwise add a Set
            # sample rate step just before it.
            precedes = any(s.get("radio") == rid
                           and s.get("technique") == "set_rate"
                           and mf.step_start_s(s) <= start_s + 1e-6
                           for s in mission()["steps"] if s is not step)
            if not precedes:
                _plan_radio(rid)["sample_rate"] = int(native)
                ui.notify(f"radio {rid} start rate set to {rate_text(native, r.get('type'))} "
                          f"to match the file", type="positive")
            else:
                lead = max(_RATE_LEAD_S, mf.rate_change_block_s(r.get("type")))
                at = max(0.0, round(start_s - lead, 1))
                mission()["steps"].append({
                    "id": _new_step_id(), "radio": rid, "technique": "set_rate",
                    "radiates": False,
                    "params": {"sample_rate": int(native)},
                    "schedule": {"start_s": at, "stop_s": at + 2.0}})
                ui.notify(f"added a sample-rate change to {rate_text(native, r.get('type'))} "
                          f"before this step to match the file", type="positive")
            return
        # the file's rate is not one the radio can take: resample it down to the
        # radio's rate once, now, so there is no work when the step runs
        if not target:
            ui.notify("set the radio's start rate first, then this file can be "
                      "resampled to it", type="warning")
            return
        ui.notify(f"resampling {os.path.basename(path)} from {native / 1e6:g} to "
                  f"{float(target) / 1e6:g} MSps...")
        try:
            rack = await host.cmd("resample_waveform",
                                  {"src_path": path, "fs_in": native,
                                   "fs_out": float(target), "load_tx": False},
                                  timeout=120.0)
        except Exception as e:
            ui.notify(f"resample failed: {e}", type="negative")
            return
        rr = getattr(rack, "result", None) or {}
        if getattr(rack, "status", 1) != 0 or not rr.get("path"):
            ui.notify(f"resample failed: {rr.get('err')}", type="negative")
            return
        step["params"]["path"] = rr["path"]
        audit.add("edit", f"{step.get('id')} resampled to {float(target) / 1e6:g} "
                          f"MSps: {rr['path']}")
        ui.notify(f"resampled to {float(target) / 1e6:g} MSps", type="positive")

    async def _pick_recording(sid, new=False):
        """Ask which recording a replay step sends, from the radio's list
        (list_recordings, the same list the record/replay panel shows), or a
        typed name. Cancel on a step that was just added removes it again; on
        an existing step it leaves the step as it was."""
        step = _find_step(sid)
        if step is None:
            return
        host = mgr.hosts.get(step.get("radio"))
        with dialog_host:
            with ui.dialog().props("persistent") as dlg, \
                    ui.card().classes("w-[32rem] gap-1"):
                ui.label(f"Replay: choose the recording on {step.get('radio')}") \
                    .classes("text-base font-bold")
                status = ui.label("").classes("text-xs text-gray-500")
                listing = ui.column().classes("w-full gap-0 max-h-72 overflow-y-auto")
                typed = ui.input("or type the recording name",
                                 value=(step.get("params") or {}).get("name", "")) \
                    .props("dense outlined").classes("w-full")

                def _use_typed():
                    n = (typed.value or "").strip()
                    if not n:
                        status.set_text("type a name first, or pick a recording above")
                        return
                    dlg.submit(n)

                with ui.row().classes("justify-end w-full gap-2"):
                    ui.button("Cancel", on_click=lambda: dlg.submit(None)).props("flat")
                    ui.button("Use typed name", on_click=_use_typed) \
                        .props("color=primary")

                async def _list():
                    if host is None:
                        status.set_text("radio not connected; type the name instead")
                        return
                    status.set_text("listing recordings on the radio...")
                    try:
                        ack = await host.cmd("list_recordings", {}, timeout=10.0)
                    except Exception as e:
                        status.set_text(f"list failed: {e}; type the name instead")
                        return
                    r = ack.result or {}
                    recs = r.get("recs") or []
                    if getattr(ack, "status", 1) != 0 or not recs:
                        status.set_text(r.get("err") or "no recordings on the radio")
                        return
                    status.set_text(f"{len(recs)} recording(s), newest first"
                                    + (" (more on the radio)" if r.get("more") else ""))
                    with listing:
                        for m in recs:
                            txt = (f"{m.get('n')}   {m.get('s', 0):g} s at "
                                   f"{(m.get('r') or 0) / 1e6:g} MSps, {m.get('m')}")
                            ui.button(txt, on_click=lambda n=m.get("n"): dlg.submit(n)) \
                                .props("flat dense no-caps align=left").classes("w-full")

                ui.timer(0.05, _list, once=True)
            name = await dlg
            dlg.delete()
            step = _find_step(sid)
            if step is None:
                return
            if not name:
                if new:
                    delete_step(sid)
                    ui.notify("Replay not added: no recording chosen")
                return
            step.setdefault("params", {})["name"] = name
            audit.add("edit", f"{sid} replays {name}")
            _revalidate()
            _refresh_all()

    def delete_step(sid):
        mission()["steps"] = [s for s in mission()["steps"] if s.get("id") != sid]
        state.get("sel", set()).discard(sid)
        if state["selected"] == sid:
            state["selected"] = next(iter(state.get("sel", set())), None)
        audit.add("edit", f"removed step {sid}")
        _revalidate()
        _refresh_all()

    def _delete_selected():
        # delete every selected step (the Delete / Backspace key). Falls back to
        # the single primary when nothing is in the multi-select set.
        ids = list(state.get("sel", set())) or (
            [state["selected"]] if state.get("selected") else [])
        if not ids:
            return
        keep = set(ids)
        mission()["steps"] = [s for s in mission()["steps"]
                              if s.get("id") not in keep]
        state["sel"] = set()
        state["selected"] = None
        audit.add("edit", f"removed {len(ids)} steps" if len(ids) > 1
                  else f"removed step {ids[0]}")
        _revalidate()
        _refresh_all()

    async def _do_clear():
        # wipe every step from the plan (keeps the radios and the anchor). Asks
        # first, since it throws the whole timeline away.
        n = len(mission()["steps"])
        if n == 0:
            ui.notify("the plan is already empty", type="info")
            return
        with dialog_host:
            with ui.dialog() as dlg, ui.card():
                ui.label(f"Remove all {n} steps from this plan?") \
                    .classes("text-base")
                ui.label("Radios and the start settings stay. This cannot be "
                         "undone.").classes("text-xs text-gray-500")
                with ui.row().classes("justify-end w-full"):
                    ui.button("Cancel", on_click=lambda: dlg.submit(False)) \
                        .props("flat")
                    ui.button("Delete all steps", on_click=lambda: dlg.submit(True)) \
                        .props("color=negative").tooltip("remove every step (radios and start settings stay)")
        if not await dlg:
            return
        mission()["steps"] = []
        state["sel"] = set()
        state["selected"] = None
        audit.add("edit", f"cleared the plan ({n} steps)")
        _revalidate()
        _refresh_all()
        ui.notify(f"cleared {n} steps", type="warning")

    async def _sweep_dialog():
        # Add a payload checkout sweep: a row of CW tone steps stepping the
        # centre across a band. Defaults to the safe bench levels. The band can
        # be filled from a payload profile (the front-end band). The RX record
        # is armed separately in the SDR tab, so it does not double-book here.
        radios = [r.get("id") for r in mission().get("radios", []) if r.get("id")]
        if not radios:
            ui.notify("add a radio to the plan first", type="warning")
            return
        default_radio = lane_target.value if lane_target.value in radios else radios[0]
        prof_bands = {}
        if _pb is not None:
            try:
                data = _pb.load_profiles()
                for nm in _pb.profile_names(data):
                    prof_bands[nm] = _pb.band_limits(data, _pb.profile(data, nm))
            except Exception:
                prof_bands = {}
        with dialog_host:
            with ui.dialog() as dlg, ui.card().classes("w-[34rem] gap-2"):
                ui.label("Payload checkout sweep").classes("text-lg font-bold")
                ui.label("Adds a row of CW tone steps stepping the centre across the band. "
                         "Levels default to the safe bench loopback. Arm the RX record in "
                         "the SDR tab to capture it.").classes("text-xs text-gray-600")
                with ui.row().classes("items-end gap-2 w-full flex-wrap"):
                    radio_sel = ui.select(radios, value=default_radio, label="radio") \
                        .props("dense outlined").classes("w-44")
                    prof_sel = None
                    if prof_bands:
                        prof_sel = ui.select(
                            {nm: f"{nm}  {z['lo']:.0f}-{z['hi']:.0f} MHz"
                             for nm, z in prof_bands.items()},
                            label="profile band (fills low/high)") \
                            .props("dense outlined clearable").classes("w-60")
                with ui.row().classes("items-end gap-2 w-full flex-wrap"):
                    lo_in = ui.number("low (MHz)", value=800.0, min=1, step=1) \
                        .props("dense outlined").classes("w-28")
                    hi_in = ui.number("high (MHz)", value=2500.0, min=1, step=1) \
                        .props("dense outlined").classes("w-28")
                    pts_in = ui.number("points", value=18, min=1, step=1) \
                        .props("dense outlined").classes("w-24")
                with ui.row().classes("items-end gap-2 w-full flex-wrap"):
                    dwell_in = ui.number("dwell (s)", value=ps.DEFAULT_DWELL_S, min=0.5, step=0.5) \
                        .props("dense outlined").classes("w-28")
                    gap_in = ui.number("gap (s)", value=ps.MIN_GAP_S, min=0, step=0.5) \
                        .props("dense outlined").classes("w-24")
                    atten_in = ui.number("TX atten (dB)", value=40.0, min=0,
                                         max=41.75, step=0.25) \
                        .props("dense outlined").classes("w-32")
                    # the TX jack goes on every tone, so the sweep does not depend on
                    # the jack the radio happens to be on when it runs
                    port_in = ui.select({}, label="TX jack").props("dense outlined").classes("w-36")
                span_lbl = ui.label("").classes("text-xs text-sky-800 font-medium")
                gap_lbl = ui.label("").classes("text-xs text-orange-800 font-medium")

                def _tx_jacks(_=None):
                    rid = radio_sel.value
                    rtype = (_plan_radio(rid) or {}).get("type")
                    pm = _live_port_maps().get(rid) or {}
                    jacks = [j for j in ports.tx_options(rtype)
                             if not pm or str((pm.get(j) or {}).get("role") or "") in ("tx", "txrx")]
                    used = [ports.step_ports(s)[1] for s in mission().get("steps", [])
                            if s.get("radio") == rid and ports.step_ports(s)[1]]
                    opts = {"": "as the radio is"}
                    opts.update({j: j for j in jacks})
                    port_in.options = opts
                    pick = next((j for j in used if j in jacks), None)
                    if pick is None and pm and jacks:
                        pick = jacks[0]
                    port_in.value = pick or ""
                    port_in.update()
                radio_sel.on_value_change(_tx_jacks)
                _tx_jacks()

                def _span(_=None):
                    try:
                        gap_lbl.set_text(ps.gap_note(float(gap_in.value or 0)))
                    except Exception:
                        gap_lbl.set_text("")
                    try:
                        n = int(pts_in.value or 0)
                        s = ps.sweep_span_s(n, float(dwell_in.value or 0),
                                            float(gap_in.value or 0))
                        lo = float(lo_in.value or 0)
                        hi = float(hi_in.value or 0)
                        step_mhz = (abs(hi - lo) / (n - 1)) if n > 1 else 0.0
                        span_lbl.set_text(f"{n} tones, about {s / 60:.1f} min, "
                                          f"~{step_mhz:.0f} MHz apart")
                    except Exception:
                        span_lbl.set_text("")
                for el in (pts_in, dwell_in, gap_in, lo_in, hi_in):
                    el.on_value_change(_span)
                _span()
                if prof_sel is not None:
                    def _fill(_=None):
                        z = prof_bands.get(prof_sel.value)
                        if z:
                            lo_in.value = round(z["lo"], 0)
                            hi_in.value = round(z["hi"], 0)
                            _span()
                    prof_sel.on_value_change(_fill)

                def _add():
                    radio_id = radio_sel.value
                    n = int(pts_in.value or 0)
                    if not radio_id or n < 1:
                        ui.notify("pick a radio and at least one point", type="warning")
                        return
                    start_s = _default_start(radio_id)
                    rtype = (_plan_radio(radio_id) or {}).get("type")
                    gap = ps.MIN_GAP_S if gap_in.value is None else float(gap_in.value)
                    # the first tone needs the same room to retune and build as the rest
                    start_s += max(gap, ps.MIN_GAP_S)
                    steps = ps.sweep_steps(
                        radio_id, float(lo_in.value) * 1e6, float(hi_in.value) * 1e6,
                        n, float(dwell_in.value or ps.DEFAULT_DWELL_S), gap_s=gap,
                        start_s=start_s, tx_atten_db=float(atten_in.value or 40),
                        tx_port=port_in.value or None, rx_none=ports.rx_none_ok(rtype))
                    ensure_radio(radio_id)
                    mission()["steps"].extend(steps)
                    audit.add("edit", f"added payload sweep: {n} tones "
                                      f"{float(lo_in.value):.0f}-{float(hi_in.value):.0f} MHz "
                                      f"on {radio_id}, dwell {float(dwell_in.value or ps.DEFAULT_DWELL_S):g} s, "
                                      f"gap {gap:g} s, TX jack {port_in.value or 'as the radio is'}")
                    _revalidate()
                    _refresh_all()
                    ui.notify(f"added {n} sweep tones on {radio_id}", type="positive")
                    dlg.submit(None)
                with ui.row().classes("justify-end w-full gap-2"):
                    ui.button("Cancel", on_click=lambda: dlg.submit(None)).props("flat")
                    ui.button("Add sweep", on_click=_add).props("color=primary")
        await dlg

    def move_step(sid, new_start):
        for s in mission()["steps"]:
            if s.get("id") == sid:
                dur = mf.step_stop_s(s) - mf.step_start_s(s)
                ns = max(0.0, round(float(new_start), 1))
                s["schedule"] = {"start_s": ns, "stop_s": round(ns + dur, 1)}
                audit.add("edit", f"moved {sid} to {_fmt_time(ns)}")
                _revalidate()
                _refresh_all()
                return

    def _live_port_maps():
        # each connected radio's port map, for the plan checks. Roles are
        # enough here; the radio itself refuses a locked jack whatever the plan says
        out = {}
        for rid, h in mgr.hosts.items():
            cfg = getattr(h, "config", None) or {}
            if cfg.get("ports"):
                out[rid] = cfg["ports"]
        return out

    def _live_rx_ports():
        # the RX port each connected radio is on, so a TX only step that leaves
        # it alone gets no warning
        out = {}
        for rid, h in mgr.hosts.items():
            rtype = (_plan_radio(rid) or {}).get("type")
            rx, _tx = ports.current_ports(getattr(h, "config", None), rtype)
            if rx and getattr(h, "connected", False):
                out[rid] = rx
        return out

    def _revalidate():
        state["conflicts"] = mf.validate(mission(), port_maps=_live_port_maps(),
                                         rx_now=_live_rx_ports())
        return state["conflicts"]

    def conflicts_for(sid):
        return [c for c in state["conflicts"] if c.step_id == sid]

    def has_errors():
        return any(c.level == "error" for c in state["conflicts"])

    # top bar

    with ui.row().classes("items-center gap-3 w-full flex-wrap"):
        ui.label("Mission planning").classes("text-lg font-bold shrink-0")
        name_in = ui.input("mission name").props("dense outlined").classes("w-56")
        name_in.value = mission()["name"]

        def _on_name(e):
            mission()["name"] = name_in.value or "untitled mission"
        name_in.on_value_change(_on_name)

        auth_in = ui.input("authorization ref").props("dense outlined").classes("w-44")
        auth_in.value = mission().get("authorization", {}).get("reference", "")

        def _on_auth(e):
            mission().setdefault("authorization", {})["reference"] = auth_in.value or ""
            _revalidate(); _refresh_all()
        auth_in.on_value_change(_on_auth)

        # the file actions as one group; the radiate controls on the right with
        # the run state beside them, never next to an everyday button. Load is
        # a plain button that opens the hidden uploader's file picker.
        with ui.button_group().props("outline"):
            ui.button("Check plan", icon="rule", on_click=lambda: _do_validate()) \
                .props("outline no-caps").tooltip("check the plan for double bookings, missing authorization, limits and clashes; keys nothing")
            ui.button("Open file", icon="upload_file", on_click=lambda: up.run_method("pickFiles")) \
                .props("outline no-caps").tooltip("a saved plan (JSON) replaces the plan; a CFE autoSchedule.csv or role XML imports steps")
            ui.button("Save plan", icon="download", on_click=lambda: _do_save()).props("outline no-caps").tooltip("download the plan as a JSON file")
            ui.button("Delete all steps", icon="delete_sweep", on_click=lambda: _do_clear()) \
                .props("outline no-caps")
            ui.button("Payload sweep", icon="graphic_eq",
                      on_click=lambda: _sweep_dialog()) \
                .props("outline no-caps") \
                .tooltip("add a row of stepped CW tones across a band, for the payload checkout; "
                         "record the RX side from the SDR tab alongside")
        up = ui.upload(label="Load", auto_upload=True).props(
            "accept=.json,.csv,.xml").style("display:none")
        ui.space()
        run_state_lbl = ui.label("idle").classes("mp-pill bg-gray-200 text-gray-700")
        run_notice.add_banner()
        with ui.button_group():
            run_btn = ui.button("Run mission", icon="play_arrow", on_click=lambda: _do_run()) \
                .props("color=negative").tooltip("confirm first, then key every step at its time; nothing radiates before the confirm")
            ui.button("STOP ALL TX", icon="dangerous", on_click=lambda: _do_stop_all()) \
                .props("color=negative outline").tooltip("stop every connected radio now and cancel the run")
        stop_steps_row = ui.row().classes("items-center gap-1")
    plan_changed_lbl = ui.label("").classes(
        "w-full text-sm font-bold text-amber-900 bg-amber-100 rounded px-2 py-1")
    plan_changed_lbl.set_visibility(False)

    # the view toolbar: the Timeline / Map switch on the left, then the
    # console block (the console clock and each radio's clock, the clock
    # buttons, and the conflict status last). One row with one shared border;
    # on a narrow screen the console wraps under a full-width switch. The
    # conflict detail lines sit under it and fold away on a status click.
    main_view = {"segs": {}}
    with ui.element("div").classes("mp-toolbar"):
        with ui.element("div").classes("mp-seg"):
            for _name, _icon in (("Timeline", "view_timeline"), ("Map", "map")):
                _seg = ui.element("div").classes("mp-seg-btn").props(
                    f'role=button tabindex=0 aria-label="{_name}"')
                _seg.on("click", lambda e, n=_name: _show_main(n))
                _seg.on("keydown.enter", lambda e, n=_name: _show_main(n))
                with _seg:
                    ui.icon(_icon)
                    ui.label(_name)
                main_view["segs"][_name] = _seg
        with ui.element("div").classes("mp-console"):
            clock_strip = ui.row().classes("items-center gap-2 flex-wrap")
            # the two clock buttons are built once, here, and only the chips are
            # redrawn on the 5 s poll: a button rebuilt with the chips can lose a
            # click that lands during the rebuild.
            sync_btn = ui.button("Sync radio clocks", icon="sync",
                                 on_click=lambda: background_tasks.create(_sync_radio_clocks())) \
                .props("dense flat").classes("mp-console-act") \
                .tooltip("set every connected radio's OS clock to this console's UTC "
                         "(steps are keyed from the console clock either way)")
            ui.button("Set time by hand", icon="edit_calendar",
                      on_click=lambda: background_tasks.create(_set_time_dialog())) \
                .props("dense flat").classes("mp-console-act") \
                .tooltip("no internet, no GPS: type a UTC time and push it to the radios "
                         "and/or this console")
            conflict_chips = ui.row().classes("items-center gap-2 flex-wrap mp-console-status")
    conflict_box = ui.column().classes("w-full gap-0 px-2")
    state["conflicts_open"] = True
    # dialogs opened from inside the timeline (a drop) are parented here, so a
    # timeline re-render does not delete a dialog that is still open
    # map and location triggers

    def _refresh_map(force=False):
        """Push the plan's path, markers and geofences to the map when they
        changed (called from the timeline refresh, so every edit reaches it)."""
        panel = map_ui.get("panel")
        if panel is None:
            return
        shown = sess.shown(lane_radios())
        sel = sess.view.get("radio")
        sig = json.dumps([me.flight_path(mission()),
                          me.step_fences(mission(), radios=shown, selected=sel),
                          me.overlays(mission())], sort_keys=True)
        if not force and sig == map_ui.get("sig"):
            return
        map_ui["sig"] = sig
        panel.push_plan(mission(), radios=shown, selected=sel)
        _refresh_show_text()

    def _refresh_show_text():
        lbl = map_ui.get("show_lbl")
        if lbl is not None:
            lbl.set_text(me.show_text(mission(), sess.view, lane_radios()))

    def _refresh_show_bar():
        """The show-on-map bar: the selected only / all toggle and one chip per
        radio in its colour, filled when the map draws it."""
        box = map_ui.get("show_box")
        if box is None:
            return
        box.clear()
        radios = lane_radios()
        shown = sess.shown(radios)
        show = sess.view.get("show", me.SHOW_SELECTED)
        with box:
            ui.label("Show on map").classes("text-xs font-bold")
            tog = ui.toggle({me.SHOW_SELECTED: "selected only", me.SHOW_ALL: "all"},
                            value=show if show in (me.SHOW_SELECTED, me.SHOW_ALL) else None) \
                .props("dense no-caps unelevated toggle-color=grey-8 size=sm") \
                .tooltip("selected only: the map follows the radio you click; "
                         "all: every radio's steps and areas at once")

            def _tog(e):
                if e.value in (me.SHOW_SELECTED, me.SHOW_ALL) and sess.set_show(e.value):
                    sess._notify()
            tog.on_value_change(_tog)
            for rid in radios:
                col = _radio_color(rid)
                on = shown is None or rid in shown
                picked = _is_selected_radio(rid)
                chip = ui.element("div").classes("cursor-pointer select-none").style(
                    f"display:inline-flex;align-items:center;gap:5px;padding:1px 8px;"
                    f"border-radius:999px;font-size:11px;font-family:monospace;"
                    f"border:2px solid {col};"
                    + (f"background:{col};color:white;" if on else f"background:transparent;color:{col};")
                    + ("box-shadow:0 0 0 2px #f2f2f2;" if picked else "")) \
                    .tooltip(f"{'hide' if on else 'show'} {rid} on the map"
                             + (" (selected)" if picked else ""))
                chip.on("click", lambda e, rid=rid: (sess.toggle_show(rid, lane_radios())
                                                     and sess._notify()))
                with chip:
                    ui.label("■" if on else "□").style("pointer-events:none;font-size:12px")
                    ui.label(rid + (f" ({len(_steps_on(rid))})" if _steps_on(rid) else " (0)")) \
                        .style("pointer-events:none")
            ui.icon("info").classes("text-gray-400 cursor-help").tooltip(
                "One colour per radio, the same on its row, its lane, its areas and the "
                "map labels. The flight path (grey) and dropped overlays belong to the "
                "plan and always show. A solid edge and a filled dot mark a start area, "
                "a dashed edge and a hollow dot a stop area. The location triggers watch "
                "every radio's areas whatever the map shows.")
        _refresh_show_text()

    def _map_warnings(lat=None, lon=None, zoom=None):
        lbl = map_ui.get("warn")
        if lbl is None:
            return
        msgs = []
        miss = me.assets_missing()
        if miss:
            msgs.append("map renderer pieces missing from the store: " + ", ".join(miss)
                        + " (run vendor_map_assets.py on the x86)")
        if lat is not None:
            msgs.extend(map_store.view_warnings(lat, lon, zoom))
        elif not map_store.base_present():
            msgs.extend(map_store.view_warnings(0, 0, None))
        lbl.set_text(" | ".join(msgs))
        lbl.set_visibility(bool(msgs))

    def _on_map_event(d):
        t = d.get("type")
        panel = map_ui.get("panel")
        if t == "ready":
            _refresh_map(force=True)
            if me.flight_path(mission()) or me.step_fences(mission(), radios=sess.shown(lane_radios())):
                panel.fit("all")
            _map_warnings(*map_ui["center"], map_ui["zoom"])
            if map_ui.get("drop_up") is not None:
                panel.enable_drop(map_ui["drop_up"], mimp.MAX_DROP_FILES)
        elif t == "view":
            _map_warnings(d.get("lat"), d.get("lon"), d.get("zoom"))
        elif t == "marker_click":
            sid = str(d.get("id", "")).split(":")[0]
            if any(x.get("id") == sid for x in mission()["steps"]):
                _select(sid)
        elif t == "marker_moved":
            _on_marker_moved(d)
        elif t == "drawn":
            _on_drawn(d)
        elif t == "draw_cancel":
            map_ui["draw_lbl"].set_text("")
        elif t == "drop":
            # the browser is posting the files; the handler below takes over
            map_ui["draw_lbl"].set_text("reading " + ", ".join(
                str(n) for n in (d.get("names") or [])[:3]) + " ...")
        elif t == "error":
            ui.notify(f"map: {d.get('message')}", type="warning")

    def _on_drawn(d):
        target = str(d.get("target") or "")
        map_ui["draw_lbl"].set_text("")
        if target == "path" and d.get("coords"):
            n = me.set_flight_path(mission(), d["coords"], source="drawn")
            audit.add("edit", f"flight path drawn on the map ({n} points)")
            _refresh_timeline(); _refresh_audit()
            ui.notify(f"flight path: {n} points", type="positive")
        elif target == "area" and d.get("bbox"):
            background_tasks.create(_area_download_dialog(d["bbox"]))
        elif target == "newtask":
            background_tasks.create(_new_task_dialog(d))
        elif target.startswith("loc:"):
            _, sid, role = target.split(":", 2)
            step = next((x for x in mission()["steps"] if x.get("id") == sid), None)
            if step is None:
                return
            loc = dict(step.get("location") or {})
            old = dict(loc.get(role) or {})
            if d.get("mode") == "point":
                loc[role] = {"lat": round(float(d["lat"]), 6), "lon": round(float(d["lon"]), 6),
                             "radius_m": float(old.get("radius_m") or 500.0)}
            elif d.get("mode") == "polygon":
                loc[role] = {"polygon": [[round(float(p[0]), 6), round(float(p[1]), 6)]
                                         for p in d["polygon"]]}
            else:
                return
            step["location"] = loc
            audit.add("edit", f"{sid}: {role} area set on the map ({d.get('mode')})")
            _revalidate(); _refresh_timeline(); _refresh_editor(); _refresh_audit()
            ui.notify(f"{role} area set", type="positive")

    def _on_marker_moved(d):
        """A step area dragged on the map: a point moves to the new place, a
        polygon is shifted by the same amount its centre moved."""
        parts = str(d.get("id", "")).split(":")
        if len(parts) != 2:
            return
        sid, role = parts
        step = next((x for x in mission()["steps"] if x.get("id") == sid), None)
        if step is None:
            return
        loc = dict(step.get("location") or {})
        gf = dict(loc.get(role) or {})
        kind = mf.geofence_kind(gf)
        try:
            lat, lon = float(d["lat"]), float(d["lon"])
        except (KeyError, TypeError, ValueError):
            return
        if kind == "point":
            gf["lat"], gf["lon"] = round(lat, 6), round(lon, 6)
        elif kind == "polygon":
            c = mf.geofence_center(gf)
            if c is None:
                return
            dlat, dlon = lat - c[0], lon - c[1]
            gf["polygon"] = [[round(float(p[0]) + dlon, 6), round(float(p[1]) + dlat, 6)]
                             for p in gf["polygon"]]
        else:
            return
        loc[role] = gf
        step["location"] = loc
        audit.add("edit", f"{sid}: {role} area moved on the map to {lat:.5f}, {lon:.5f}")
        _revalidate(); _refresh_timeline()
        if state.get("selected") == sid:
            _refresh_editor()
        _refresh_audit()

    def _start_draw(mode, target, hint):
        panel = map_ui.get("panel")
        if panel is None:
            return
        if map_ui.get("show_map"):
            map_ui["show_map"]()
        panel.set_draw_mode(mode, target)
        map_ui["draw_lbl"].set_text(hint)

    async def _import_path(e):
        fname = getattr(e.file, "name", "") or ""
        try:
            pts = me.path_from_file(fname, await e.file.text())
        except Exception as ex:
            ui.notify(f"could not read a path from that file: {ex}", type="negative")
            return
        n = me.set_flight_path(mission(), pts, source=f"file {fname}")
        audit.add("edit", f"flight path imported from {fname} ({n} points)")
        _refresh_timeline(); _refresh_audit()
        map_ui["panel"].fit("path")
        ui.notify(f"flight path imported: {n} points", type="positive")
        try:
            map_ui["path_up"].reset()           # so the next file is taken too
        except Exception:
            pass

    def _clear_path():
        if not me.flight_path(mission()):
            return
        me.set_flight_path(mission(), [])
        audit.add("edit", "flight path cleared")
        _refresh_timeline(); _refresh_audit()

    # files dropped on the map
    #
    # The map's JS hands dropped files to a hidden uploader, which posts them
    # here in one request per drop. Tile files go to the map store (one prompt
    # each); every data file is read into one feature set and one prompt asks
    # what the lines, areas and points become.

    async def _file_head(f, nbytes=65536):
        async for chunk in f.iterate(chunk_size=nbytes):
            return bytes(chunk[:nbytes])
        return b""

    async def _on_drop_files(e):
        files = list(getattr(e, "files", None) or [])
        if not files:
            return
        sets, problems, tiles = [], [], []
        for f in files:
            name = getattr(f, "name", "") or "file"
            try:
                head = await _file_head(f)
                fmt = mimp.sniff(name, head)
                if fmt in mimp.TILE_FORMATS:
                    tiles.append((f, name, fmt))
                    continue
                if fmt == "mission":
                    problems.append(f"{name} is a mission plan; use Load for it")
                    continue
                size = int(f.size())
                if size > mimp.MAX_DATA_BYTES:
                    problems.append(f"{name} is {size // (1024 * 1024)} MB; the limit for a "
                                    f"data file is {mimp.MAX_DATA_BYTES // (1024 * 1024)} MB")
                    continue
                data = await f.read()
                sets.append(mimp.parse(name, data))
            except mimp.MapImportError as ex:
                problems.append(str(ex))
            except Exception as ex:
                problems.append(f"{name}: could not be read ({ex})")
        with dialog_host:
            map_ui["draw_lbl"].set_text("")
            for msg in problems:
                ui.notify(msg, type="negative", timeout=8000, close_button=True)
            if problems and not sets and not tiles:
                ui.notify("the map takes " + mimp.ACCEPTED_TEXT, type="info", timeout=8000)
        for f, name, fmt in tiles:
            await _tile_drop(f, name, fmt)
        if sets:
            await _drop_prompt(mimp.merge_features(sets))

    def _task_choices():
        techs = {}
        for group, entries in catalog.groups_for_tab():
            for en in entries:
                if en.technique == "set_rate":
                    continue
                techs[en.technique] = f"{group}: {en.label}"
        return techs

    def _become(n, noun):
        return f"the {noun} becomes" if n == 1 else f"the {n} {noun}s become"

    async def _drop_prompt(feats):
        """What the dropped data becomes: a line the flight path, areas and
        points location tasks, and anything else an overlay layer."""
        lines, polys, points = feats["lines"], feats["polygons"], feats["points"]
        radios = lane_radios()
        many = mimp.MANY_FEATURES
        stem = os.path.splitext(os.path.basename(feats.get("source", "").split(",")[0]))[0]
        with dialog_host:
            with ui.dialog() as dlg, ui.card().classes("w-[36rem] gap-1"):
                ui.label("Dropped on the map: " + (feats.get("source") or "file")) \
                    .classes("text-base font-bold")
                ui.label(f"{mimp.summary(feats)} ({feats.get('format')})") \
                    .classes("text-xs text-gray-500")
                line_in = poly_in = pt_in = None
                if lines:
                    opts = {"path": ("the flight path" if len(lines) == 1 else
                                     "the flight path (the longest; the others an overlay)"),
                            "overlay": "an overlay layer", "skip": "leave out"}
                    line_in = ui.select(opts, value="path", label=_become(len(lines), "line")) \
                        .props("dense outlined").classes("w-full")
                    if me.flight_path(mission()):
                        ui.label("the plan already has a flight path; it is replaced") \
                            .classes("text-xs text-amber-700")
                if polys:
                    opts = {"tasks": "task areas (one location task each)",
                            "overlay": "an overlay layer", "skip": "leave out"}
                    poly_in = ui.select(opts, value="tasks" if len(polys) <= many else "overlay",
                                        label=_become(len(polys), "area")) \
                        .props("dense outlined").classes("w-full")
                if points:
                    opts = {"tasks": "task points (one location task each)",
                            "overlay": "an overlay layer", "skip": "leave out"}
                    if not lines and len(points) >= 2:
                        opts["path"] = "the flight path, joined in file order"
                    pt_in = ui.select(opts, value="tasks" if len(points) <= many else "overlay",
                                      label=_become(len(points), "point")) \
                        .props("dense outlined").classes("w-full")
                if (polys and len(polys) > many) or (points and len(points) > many):
                    ui.label(f"more than {many} shapes default to an overlay; choose task "
                             "areas or points to make a location task from each") \
                        .classes("text-xs text-gray-500")
                task_box = ui.column().classes("w-full gap-1")
                with task_box:
                    ui.label("LOCATION TASKS").classes("text-[10px] text-gray-500 font-bold mt-1")
                    techs = _task_choices()
                    tech_in = ui.select(techs, value="jammer.barrage" if "jammer.barrage" in techs
                                        else next(iter(techs)), label="task") \
                        .props("dense outlined").classes("w-full")
                    radio_in = ui.select(radios, value=radios[0] if radios else None, label="radio") \
                        .props("dense outlined").classes("w-full")
                    mode_in = ui.select(dict(mf.TRIGGER_MODE_LABELS),
                                        value="dwell" if (points and not polys) else "while_inside",
                                        label="when").props("dense outlined").classes("w-full")
                    with ui.row().classes("items-center gap-1 w-full flex-wrap"):
                        dwell_in = ui.number("run for (s)", value=mf.DEFAULT_DWELL_S, min=1,
                                             format="%g").props("dense outlined").classes("w-28")
                        max_in = ui.number("at most (s)", value=mf.DEFAULT_MAX_S, min=1,
                                           format="%g").props("dense outlined").classes("w-28")
                        rad_in = ui.number("point radius (m)", value=500.0, min=1, format="%g") \
                            .props("dense outlined").classes("w-32") \
                            .tooltip("for points without a radius of their own")
                        prio_in = ui.number("priority", value=mf.DEFAULT_LOCATION_PRIORITY,
                                            min=1, max=9, step=1, format="%d") \
                            .props("dense outlined").classes("w-24")
                    conf_in = ui.select(dict(mf.CONFIRM_LABELS), value="once",
                                        label="confirm").props("dense outlined").classes("w-full")
                    if not radios:
                        ui.label("no radio in the plan: add one first, or make these an "
                                 "overlay").classes("text-xs text-amber-700")
                ov_box = ui.column().classes("w-full gap-1")
                with ov_box:
                    ov_name = ui.input("overlay name", value=stem or "overlay") \
                        .props("dense outlined").classes("w-full")
                zoom_in = ui.checkbox("zoom the map to it", value=True).props("dense")

                def _vis(_=None):
                    want_tasks = (poly_in is not None and poly_in.value == "tasks") or \
                                 (pt_in is not None and pt_in.value == "tasks")
                    want_ov = (line_in is not None and (line_in.value == "overlay" or
                                                        (line_in.value == "path" and len(lines) > 1))) or \
                              (poly_in is not None and poly_in.value == "overlay") or \
                              (pt_in is not None and pt_in.value == "overlay")
                    task_box.set_visibility(want_tasks)
                    ov_box.set_visibility(want_ov)
                    dwell_in.set_visibility(mode_in.value == "dwell")
                    max_in.set_visibility(mode_in.value != "dwell")
                    rad_in.set_visibility(pt_in is not None and pt_in.value == "tasks")
                for w in (line_in, poly_in, pt_in, mode_in):
                    if w is not None:
                        w.on_value_change(_vis)
                _vis()
                with ui.row().classes("justify-end w-full gap-2"):
                    ui.button("Cancel", on_click=lambda: dlg.submit(False)).props("flat")
                    ui.button("Import", on_click=lambda: dlg.submit(True)).props("color=primary")
        if not await dlg:
            return
        line_c = line_in.value if line_in is not None else "skip"
        poly_c = poly_in.value if poly_in is not None else "skip"
        pt_c = pt_in.value if pt_in is not None else "skip"
        ov = mimp.empty_features(feats.get("source"), feats.get("format"))
        done = []
        src = f"file {feats.get('source')}"
        if line_c == "path":
            best = mimp.longest_line(feats)
            n = me.set_flight_path(mission(), best["coords"], source=src)
            done.append(f"flight path {n} points")
            ov["lines"].extend(ln for ln in lines if ln is not best)
        elif line_c == "overlay":
            ov["lines"].extend(lines)
        if pt_c == "path":
            n = me.set_flight_path(mission(), [[p["lon"], p["lat"]] for p in points], source=src)
            done.append(f"flight path {n} points")
        elif pt_c == "overlay":
            ov["points"].extend(points)
        if poly_c == "overlay":
            ov["polygons"].extend(polys)
        want_tasks = poly_c == "tasks" or pt_c == "tasks"
        new_ids = []
        if want_tasks:
            if not radio_in.value:
                with dialog_host:
                    ui.notify("no radio to put the tasks on; the areas and points were "
                              "kept as an overlay instead", type="warning", timeout=8000)
                if poly_c == "tasks":
                    ov["polygons"].extend(polys)
                if pt_c == "tasks":
                    ov["points"].extend(points)
            else:
                ensure_radio(radio_in.value)
                shapes = []
                if poly_c == "tasks":
                    shapes += [({"polygon": [[round(float(p[0]), 6), round(float(p[1]), 6)]
                                             for p in pg["coords"]]}, pg.get("name", ""))
                               for pg in polys]
                if pt_c == "tasks":
                    shapes += [({"lat": round(float(p["lat"]), 6), "lon": round(float(p["lon"]), 6),
                                 "radius_m": float(p.get("radius_m") or rad_in.value or 500.0)},
                                p.get("name", "")) for p in points]
                for gf, nm in shapes:
                    sid = _new_step_id()
                    step = mf.new_location_task(
                        sid, tech_in.value, radio_in.value, gf, mode=mode_in.value,
                        dwell_s=float(dwell_in.value or mf.DEFAULT_DWELL_S),
                        max_s=float(max_in.value or mf.DEFAULT_MAX_S),
                        priority=int(prio_in.value or mf.DEFAULT_LOCATION_PRIORITY),
                        confirm=conf_in.value)
                    if nm:
                        step["location"]["start"]["name"] = str(nm)
                    mission()["steps"].append(step)
                    new_ids.append(sid)
                done.append(f"{len(new_ids)} location task{'s' if len(new_ids) != 1 else ''} "
                            f"({tech_in.value} on {radio_in.value})")
        if mimp.feature_count(ov):
            rec = me.add_overlay(mission(), ov_name.value or stem or "overlay", ov,
                                 source=feats.get("source", ""))
            done.append(f"overlay '{rec['name']}' ({mimp.summary(ov)})")
        if not done:
            return
        audit.add("import", f"map drop {feats.get('source')}: " + "; ".join(done))
        with dialog_host:
            _revalidate(); _refresh_all(); _refresh_audit()
            if new_ids:
                _select(new_ids[0])
            if zoom_in.value:
                b = mimp.bbox(feats)
                if b:
                    map_ui["panel"].fit_bbox(b)
            ui.notify("imported: " + "; ".join(done), type="positive", timeout=6000)

    async def _overlays_dialog():
        with dialog_host:
            with ui.dialog() as dlg, ui.card().classes("w-[34rem] gap-1"):
                ui.label("Overlay layers on the map").classes("text-base font-bold")
                ui.label("Dropped map data kept for viewing; saved in the plan file.") \
                    .classes("text-xs text-gray-500")
                box = ui.column().classes("w-full gap-1")

                def _fill():
                    box.clear()
                    with box:
                        ovs = me.overlays(mission())
                        if not ovs:
                            ui.label("none. Drop a GeoJSON, KML, KMZ, GPX, CSV or zipped "
                                     "shapefile on the map to add one.").classes("text-xs text-gray-500")
                        for o in ovs:
                            with ui.row().classes("items-center gap-2 w-full no-wrap"):
                                ui.element("div").style(
                                    f"width:12px;height:12px;border-radius:3px;background:{o.get('color')}")
                                ui.label(o.get("name", "")).classes("text-sm grow")
                                ui.label(mimp.summary(o)).classes("text-xs text-gray-500 font-mono")
                                ui.button(icon="center_focus_strong",
                                          on_click=lambda _, oid=o["id"]: _zoom(oid)) \
                                    .props("dense flat round size=sm").tooltip("zoom the map to it")
                                ui.button(icon="delete", on_click=lambda _, oid=o["id"]: _remove(oid)) \
                                    .props("dense flat round size=sm color=negative") \
                                    .tooltip("remove this overlay from the map and the plan")

                def _zoom(oid):
                    o = next((x for x in me.overlays(mission()) if x.get("id") == oid), None)
                    if o is None:
                        return
                    b = mimp.bbox(o)
                    if b:
                        map_ui["panel"].fit_bbox(b)

                def _remove(oid):
                    o = next((x for x in me.overlays(mission()) if x.get("id") == oid), None)
                    if o is None or not me.remove_overlay(mission(), oid):
                        return
                    audit.add("edit", f"overlay '{o.get('name')}' removed")
                    _refresh_map(); _refresh_audit()
                    _fill()

                _fill()
                with ui.row().classes("justify-end w-full"):
                    ui.button("Close", on_click=lambda: dlg.submit(True)).props("flat")
        await dlg

    async def _tile_drop(f, fname, fmt):
        """A dropped PMTiles or MBTiles file: into the map store as an area,
        after the budget check and a confirm. MBTiles are converted with the
        go-pmtiles tool from the store's bin directory."""
        store = map_store.ensure_store()
        tmp = os.path.join(store, map_store.AREAS_DIR, f".drop-{uuid.uuid4().hex[:8]}.{fmt}")

        def _discard():
            try:
                os.remove(tmp)
            except OSError:
                pass

        try:
            await f.save(tmp)
            size = os.path.getsize(tmp)
            if fmt == "pmtiles":
                with open(tmp, "rb") as fh:
                    info = mimp.pmtiles_header(fh.read(mimp.PMTILES_HEADER_LEN))
                tool = None
            else:
                info = mimp.mbtiles_info(tmp)
                tool = map_store.find_pmtiles_tool()
                if tool is None:
                    raise mimp.MapImportError(
                        "MBTiles need the go-pmtiles tool to convert them "
                        f"(expected at {map_store.pmtiles_tool_path()}; vendor_map_assets.py "
                        "fetches it)")
        except mimp.MapImportError as ex:
            _discard()
            with dialog_host:
                ui.notify(f"{fname}: {ex}", type="negative", timeout=8000, close_button=True)
            return
        except Exception as ex:
            _discard()
            with dialog_host:
                ui.notify(f"{fname}: could not be stored ({ex})", type="negative", timeout=8000)
            return
        if info.get("tile_type") == "unknown":
            _discard()
            with dialog_host:
                ui.notify(f"{fname}: the tile type is not one this map draws "
                          "(vector, png, jpeg or webp)", type="negative", timeout=8000)
            return
        bbox = info.get("bbox") or [-180.0, -85.0, 180.0, 85.0]
        idx = map_store.load_index()
        over = map_store.would_exceed_budget(size, idx)
        with dialog_host:
            with ui.dialog() as dlg, ui.card().classes("w-[32rem] gap-1"):
                ui.label("Map tiles dropped: " + fname).classes("text-base font-bold")
                name_in = ui.input("area name", value=mimp.area_name_for(fname)) \
                    .props("dense outlined").classes("w-full")
                ui.label(f"{fmt}, {info.get('tile_type')} tiles, zoom {info.get('minzoom')} to "
                         f"{info.get('maxzoom')}, {map_store.human_size(size)}") \
                    .classes("text-xs font-mono")
                ui.label("covers " + ", ".join(f"{v:.2f}" for v in bbox)
                         + ("" if info.get("bbox") else "  (no bounds in the file; the whole world is assumed)")) \
                    .classes("text-xs font-mono")
                tot = map_store.total_size(idx)
                bud = map_store.budget_bytes(idx)
                ui.label(f"store {map_store.human_size(tot)} of {map_store.human_size(bud)} budget; "
                         f"after this {map_store.human_size(tot + size)}"
                         + ("  OVER BUDGET" if over else "")) \
                    .classes("text-xs font-mono " + ("text-red-700 font-bold" if over else ""))
                if over:
                    ui.label("Remove an area under Stored map areas or raise the budget "
                             "first; nothing was added.").classes("text-xs text-red-700")
                exists_lbl = ui.label("").classes("text-xs text-amber-700")

                def _chk(_=None):
                    nm = map_store.safe_area_name(name_in.value or "")
                    exists_lbl.set_text(f"replaces the stored area '{nm}'"
                                        if map_store.get_area(nm, idx) else "")
                name_in.on_value_change(_chk)
                _chk()
                with ui.row().classes("justify-end w-full gap-2"):
                    ui.button("Cancel", on_click=lambda: dlg.submit(False)).props("flat")
                    ok = ui.button("Add to the map store", on_click=lambda: dlg.submit(True)) \
                        .props("color=primary")
                    if over:
                        ok.disable()
        # the dialog opens either way, so an over-budget drop is explained;
        # its add button is disabled then
        if not await dlg or over:
            _discard()
            return
        name = map_store.safe_area_name(name_in.value or mimp.area_name_for(fname))
        final = os.path.join(store, map_store.area_file(name))
        try:
            if fmt == "pmtiles":
                os.replace(tmp, final)
            else:
                with dialog_host:
                    ui.notify(f"converting {fname} to PMTiles ...", type="info", timeout=4000)
                proc = await asyncio.create_subprocess_exec(
                    tool, "convert", tmp, final, "--force",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
                out, _ = await proc.communicate()
                _discard()
                if proc.returncode != 0:
                    raise RuntimeError(f"pmtiles convert failed: "
                                       f"{out.decode('utf-8', 'replace')[-300:]}")
            size = os.path.getsize(final)
            map_store.add_area(name, bbox, minzoom=info.get("minzoom", 0),
                               maxzoom=info.get("maxzoom", map_store.AREA_MAXZOOM),
                               size_bytes=size, source=f"dropped {fname}",
                               tile_type=info.get("tile_type", "vector"))
        except Exception as ex:
            _discard()
            with dialog_host:
                ui.notify(f"{fname}: {ex}", type="negative", timeout=8000, close_button=True)
            return
        audit.add("import", f"map area '{name}' added from {fname} ({map_store.human_size(size)})")
        with dialog_host:
            map_ui["panel"].set_areas()
            if info.get("bbox"):
                map_ui["panel"].fit_bbox(bbox)
            _refresh_audit()
            _map_warnings(*map_ui["center"], map_ui["zoom"])
            ui.notify(f"area '{name}' stored: {map_store.human_size(size)}", type="positive")

    async def _area_manager_dialog():
        with dialog_host:
            with ui.dialog() as dlg, ui.card().classes("w-[40rem] gap-1"):
                ui.label("Map data on this console").classes("text-base font-bold")
                box = ui.column().classes("w-full gap-1")
                total_lbl = ui.label("").classes("text-xs font-mono")

                def _fill():
                    box.clear()
                    idx = map_store.load_index()
                    with box:
                        for e in map_store.entries(idx):
                            with ui.row().classes("items-center gap-2 w-full no-wrap"):
                                age = e.get("age_days")
                                old = map_store.is_stale(idx["base"] if e["kind"] == "base"
                                                         else map_store.get_area(e["name"], idx) or {})
                                txt = (f"{e['name']}  {e['kind']}  zoom {e['minzoom']}-{e['maxzoom']}  "
                                       f"{map_store.human_size(e['size_bytes'])}  "
                                       + (f"{age:.0f} d old" if age is not None else "age ?")
                                       + ("  OLD" if old else "")
                                       + ("" if e["present"] else "  FILE MISSING"))
                                ui.label(txt).classes("text-xs font-mono grow"
                                                      + (" text-red-600" if not e["present"] else ""))
                                if e["kind"] == "area":
                                    ui.button(icon="delete",
                                              on_click=lambda n=e["name"]: _remove(n)) \
                                        .props("dense flat color=negative")
                    tot = map_store.total_size(idx)
                    bud = map_store.budget_bytes(idx)
                    total_lbl.set_text(f"total {map_store.human_size(tot)} of "
                                       f"{map_store.human_size(bud)} budget"
                                       + ("  (over 80 percent)" if map_store.over_warn(idx) else ""))

                async def _remove(name):
                    with dialog_host:
                        with ui.dialog() as d2, ui.card():
                            ui.label(f"Remove the downloaded area '{name}'?")
                            with ui.row().classes("justify-end w-full"):
                                ui.button("Cancel", on_click=lambda: d2.submit(False)).props("flat")
                                ui.button("Remove area", on_click=lambda: d2.submit(True)).props("color=negative")
                    if await d2:
                        map_store.remove_area(name)
                        audit.add("edit", f"map area '{name}' removed")
                        _refresh_audit()
                        map_ui["panel"].set_areas()
                        _fill()

                _fill()
                ui.label("Download more detail: draw a box on the map (Download a box), "
                         "the size is shown before anything is fetched. Needs internet "
                         "on this console.").classes("text-xs text-gray-500")
                with ui.row().classes("justify-end w-full gap-2"):
                    ui.button("Download a box", icon="crop_square",
                              on_click=lambda: (dlg.submit(None),
                                                _start_draw("box", "area",
                                                            "click two corners of the area to download"))) \
                        .props("dense outline")
                    ui.button("Close", on_click=lambda: dlg.submit(None)).props("flat")
        await dlg

    async def _area_download_dialog(bbox):
        from dpmg.mission import base_map as bbm
        from nicegui import run as nrun
        w, s_, e_, n = [round(float(x), 4) for x in bbox]
        with dialog_host:
            with ui.dialog() as dlg, ui.card().classes("w-[32rem] gap-1"):
                ui.label("Download an area").classes("text-base font-bold")
                ui.label(f"box west {w}  south {s_}  east {e_}  north {n}, up to zoom "
                         f"{map_store.AREA_MAXZOOM}").classes("text-xs font-mono")
                name_in = ui.input("area name", value="area").props("dense outlined").classes("w-full")
                size_lbl = ui.label("size not checked yet").classes("text-xs")
                prog = ui.label("").classes("text-xs text-amber-700")
                pred = {"bytes": None}

                async def _check():
                    size_lbl.set_text("checking the size (dry run)...")
                    try:
                        b = await nrun.io_bound(bbm.download_area, name_in.value or "area",
                                               [w, s_, e_, n], dry_run=True)
                    except BaseException as ex:
                        size_lbl.set_text(f"dry run failed: {ex}")
                        return
                    pred["bytes"] = b
                    over = b is not None and map_store.would_exceed_budget(b)
                    size_lbl.set_text(("about " + map_store.human_size(b) if b is not None
                                       else "size unknown")
                                      + (" : OVER the store budget" if over else ""))
                    dl_btn.set_enabled(b is not None and not over)

                async def _download():
                    name = map_store.safe_area_name(name_in.value or "area")
                    dl_btn.disable()
                    prog.set_text(f"downloading '{name}'... (the map updates when it is done)")
                    audit.add("edit", f"map area '{name}' download started "
                                      f"({map_store.human_size(pred['bytes'] or 0)})")
                    _refresh_audit()
                    try:
                        size = await nrun.io_bound(bbm.download_area, name, [w, s_, e_, n])
                    except BaseException as ex:
                        prog.set_text(f"download failed: {ex}")
                        audit.add("edit", f"map area '{name}' download FAILED: {ex}")
                        _refresh_audit()
                        return
                    audit.add("edit", f"map area '{name}' downloaded ({map_store.human_size(size)})")
                    _refresh_audit()
                    map_ui["panel"].set_areas()
                    dlg.submit(True)
                    ui.notify(f"area '{name}' ready: {map_store.human_size(size)}", type="positive")

                with ui.row().classes("justify-end w-full gap-2"):
                    ui.button("Cancel", on_click=lambda: dlg.submit(False)).props("flat")
                    ui.button("Check download size", on_click=_check).props("dense outline")
                    dl_btn = ui.button("Download tiles", on_click=_download).props("dense color=primary")
                    dl_btn.disable()
        await dlg

    def _build_map_card():
        card = ui.card().classes("w-full min-w-0 gap-1")
        with card:
            with ui.row().classes("items-center gap-1 w-full no-wrap"):
                ui.element("div").classes("grow")
                ui.icon("info").classes("text-gray-400 cursor-help").tooltip(
                    "flight path and step areas, coloured per radio. Click a radio (its row, "
                    "its lane or its name in the header) and the map shows that radio's steps; "
                    "the Show on map bar overlays several or all. The location triggers watch "
                    "every radio's areas whatever the map shows. With the triggers armed, "
                    "entering a start area asks for a confirm, but an automatic task, or one "
                    "confirmed once since arming, keys without asking. Drop map files anywhere "
                    "on the map: " + mimp.ACCEPTED_TEXT)
            with ui.row().classes("items-center gap-1 w-full flex-wrap"):
                ui.button("Draw flight path", icon="timeline",
                          on_click=lambda: _start_draw("path", "path",
                                                       "click along the route, double-click to finish, Esc cancels")) \
                    .props("dense outline no-caps")
                map_ui["path_up"] = up = ui.upload(on_upload=_import_path, auto_upload=True, max_files=1) \
                    .props('accept=".json,.geojson,.gpx,.kml,.kmz,.csv,.txt,.zip"').style("display:none")
                ui.button("Import flight path", icon="upload_file",
                          on_click=lambda: map_ui["path_up"].run_method("pickFiles")) \
                    .props("dense outline no-caps").tooltip(
                    "a flight path from a GeoJSON, GPX, KML, KMZ, CSV or zipped shapefile; "
                    "or drop any of them on the map")
                # the drop target's uploader: the map's JS hands it the dropped
                # files, one request per drop, so one prompt asks about them all
                map_ui["drop_up"] = ui.upload(multiple=True, auto_upload=True,
                                              max_files=mimp.MAX_DROP_FILES,
                                              on_multi_upload=_on_drop_files,
                                              on_rejected=lambda: ui.notify(
                                                  f"drop at most {mimp.MAX_DROP_FILES} files at a time",
                                                  type="warning")) \
                    .style("display:none")
                ui.button("Overlays", icon="layers_clear",
                          on_click=lambda: background_tasks.create(_overlays_dialog())) \
                    .props("dense flat no-caps").tooltip("dropped map data kept for viewing: list, zoom, remove")
                ui.button("Clear flight path", icon="clear", on_click=_clear_path).props("dense flat no-caps").tooltip("remove the flight path from the map")
                ui.button("Fit map to plan", icon="fit_screen",
                          on_click=lambda: map_ui["panel"].fit("all")).props("dense flat no-caps").tooltip("frame the path, the areas and the markers")
                ui.button("Stored map areas", icon="layers",
                          on_click=lambda: background_tasks.create(_area_manager_dialog())) \
                    .props("dense flat no-caps").tooltip("map data on this console: list, remove, download a box")
                ui.button("New location task", icon="add_location",
                          on_click=_new_task_shape_dialog).props("dense outline no-caps") \
                    .tooltip("draw a box, polygon or point, then pick the task from the catalog")
                map_ui["draw_lbl"] = ui.label("").classes("text-xs text-amber-700")
            # which radios the map draws, and the words that say what it shows
            map_ui["show_box"] = ui.row().classes("items-center gap-2 w-full flex-wrap mp-bar")
            map_ui["show_lbl"] = ui.label("").classes("text-xs text-gray-700 w-full")
            map_ui["warn"] = ui.label("").classes("text-xs text-amber-700 font-bold")
            map_ui["warn"].set_visibility(False)
            map_ui["panel"] = me.MapPanel(on_event=_on_map_event, height_px=400,
                                          center=map_ui["center"], zoom=map_ui["zoom"])
            # as tall as the screen allows, with room for the rows under it and
            # the editor tabs below the card
            map_ui["panel"].el.style("height:max(420px, calc(100vh - 360px))")
            # position feed and the trigger arm
            with ui.row().classes("items-center gap-2 w-full flex-wrap mp-bar"):
                ui.icon("satellite_alt").classes("text-gray-500").tooltip(
                    "Position: the aircraft multicast feed (JSON or NMEA) wins, else the "
                    "first radio with a GPS fix. Stale (over 10 s) or missing position turns "
                    "the triggers off.")
                ac_names = sess.aircraft_names()
                ac_sel = map_ui["ac_sel"] = ui.select(ac_names, value=sess.feeds.first_name(),
                                                      label="aircraft") \
                    .props("dense outlined").classes("w-40") \
                    .tooltip("which aircraft's feed the switch and the fields act on; a "
                             "radio row says which aircraft its triggers follow")
                ac_sel.set_visibility(len(ac_names) > 1)
                pos_sw = ui.switch("listen to the aircraft feed", value=sess.listening) \
                    .props("dense")
                grp_in = ui.input("group", value=sess.pos_group).props("dense outlined").classes("w-32")
                prt_in = ui.number("port", value=sess.pos_port, format="%d").props("dense outlined") \
                    .classes("w-24")
                # the console's own addresses, so the operator can pick the adapter
                # the feed arrives on (on Windows the Jetson USB NCM adapter,
                # 192.168.55.100 or 192.168.56.100) instead of typing it
                ifc_in = ui.input("iface", value=sess.pos_iface,
                                  autocomplete=["0.0.0.0"] + local_ipv4_addresses()) \
                    .props("dense outlined").classes("w-36") \
                    .tooltip("this console's address on the adapter the feed arrives on "
                             "(the list offers them); 0.0.0.0 joins on the default adapter, "
                             "which on Windows is usually Wi-Fi, not the Jetson USB link; "
                             "127.0.0.1 for sim_position.py on this host")
                fmt_in = ui.select(list(FEED_FORMATS), value=sess.pos_format, label="format") \
                    .props("dense outlined").classes("w-24")
                # above the field: below it the tooltip covered the first
                # item of the open list, auto
                with fmt_in:
                    ui.tooltip("the datagram format: auto sniffs each one; json or nmea "
                               "takes only that form").props('anchor="top middle" self="bottom middle"')
                arm_sw = map_ui["arm"] = ui.switch("location triggers armed",
                                                   value=sess.triggers_armed).props("dense")
                map_ui["pos"] = ui.label("").classes("text-xs font-mono grow")

                # what the fields last showed: the switch writes back only the
                # fields changed here, and the fields follow a save made on
                # another page (_trigger_tick refills them)
                feed_fields = map_ui["feed_fields"] = PanelFields(sess.feeds)

                def _field_values():
                    return {"group": grp_in.value, "port": prt_in.value,
                            "iface": ifc_in.value, "format": fmt_in.value}

                def _fill_feed_fields(keep_edits=False):
                    vals = feed_fields.fill(ac_sel.value, _field_values() if keep_edits else None)
                    grp_in.set_value(vals.get("group", ""))
                    prt_in.set_value(vals.get("port", 5005))
                    ifc_in.set_value(vals.get("iface", "0.0.0.0"))
                    fmt_in.set_value(vals.get("format", "auto"))
                map_ui["fill_feed_fields"] = _fill_feed_fields
                _fill_feed_fields()

                def _set_listen_sw(want):
                    # set from code (following the shared listener): not a
                    # press, so _listen must not act on it
                    state["listen_sw_sync"] = True
                    try:
                        pos_sw.value = want
                    finally:
                        state["listen_sw_sync"] = False
                map_ui["set_listen_sw"] = _set_listen_sw

                def _listen(e):
                    if state.get("listen_sw_sync"):
                        return
                    if e.value:
                        try:
                            sess.start_listener(name=ac_sel.value,
                                                changes=feed_fields.edits(_field_values()))
                        except (ValueError, OSError) as ex:
                            ui.notify(f"listen failed: {ex}", type="negative")
                        _fill_feed_fields()
                    else:
                        sess.stop_listener(ac_sel.value)
                    _refresh_audit()
                pos_sw.on_value_change(_listen)
                map_ui["listen_sw"] = pos_sw

                def _pick_format(_e=None):
                    # a picked format is saved at once and a running listener
                    # takes it on its next datagram; a fill from code is no
                    # edit and writes nothing
                    try:
                        res = feed_fields.apply(_field_values())
                    except OSError as ex:
                        ui.notify(f"format not saved: {ex}", type="negative")
                        return
                    if res is not None and res[2]:
                        name, e, _changed = res
                        audit.add("trigger", f"aircraft {name}: feed format {e['format']}"
                                  + (", the running listener takes it at once"
                                     if sess.listening_for(name) else ""))
                        _refresh_audit()
                fmt_in.on_value_change(_pick_format)

                def _pick_aircraft(_e=None):
                    # the fields and the switch show the picked aircraft's feed
                    _fill_feed_fields()
                    _set_listen_sw(sess.listening_for(ac_sel.value))
                ac_sel.on_value_change(_pick_aircraft)
            if not engineering:
                # the flight view: the live feed state in place of the motion row
                with ui.row().classes("items-center gap-2 w-full flex-wrap mp-bar"):
                    ui.icon("sensors").classes("text-gray-500").tooltip(
                        "The live position feed: where it comes from, the last fix and "
                        "its age, and the fix rate. Red when there is no fix, the fix is "
                        "stale or the position is simulated. The simulated motion "
                        "controls are in the engineering view only.")
                    ui.label("Live feed").classes("text-xs font-bold")
                    # one line (and warning) per aircraft, built again when
                    # the aircraft list changes
                    map_ui["feed_box"] = ui.column().classes("gap-0 grow")
                    map_ui["feed_rows"] = {}

                def _arm(e):
                    sess.arm_triggers(bool(e.value))
                    _refresh_audit()
                arm_sw.on_value_change(_arm)
            # simulated motion: fly the platform along the flight path so the
            # location tasks can be checked out beside a timeline run. The
            # engineering view only; the flight view shows the live feed instead.
            with ui.row().classes("items-center gap-2 w-full flex-wrap mp-bar") as motion_row:
                motion_row.set_visibility(engineering)
                ui.icon("flight").classes("text-gray-500").tooltip(
                    "The simulated position is used instead of the feed while it plays, and "
                    "the triggers see it the same way. Arm the triggers and press Run mission to "
                    "see location tasks and the timeline together.")
                ui.label("Simulate platform motion").classes("text-xs font-bold")
                spd_in = ui.number("speed (m/s)", value=sess.motion.speed_mps, min=1,
                                   format="%g").props("dense outlined").classes("w-28")
                loop_cb = ui.checkbox("loop", value=sess.motion.loop).props("dense")
                play_btn = ui.button("Play motion", icon="play_arrow", on_click=lambda: _motion("play")) \
                    .props("dense outline no-caps").tooltip("fly the simulated platform along the path")
                ui.button("Pause", icon="pause", on_click=lambda: _motion("pause")) \
                    .props("dense flat no-caps")
                ui.button("Stop motion", icon="stop", on_click=lambda: _motion("stop")) \
                    .props("dense flat no-caps").tooltip("stop the simulated platform and go back to the real feed")
                ui.button("Path through the task areas", icon="route",
                          on_click=lambda: _motion("areas")).props("dense flat no-caps") \
                    .tooltip("builds a flight path that visits every task area in plan order")
                map_ui["motion"] = ui.label("").classes("text-xs font-mono")

                def _motion(what):
                    m = sess.motion
                    try:
                        m.speed_mps = max(1.0, float(spd_in.value or ms.DEFAULT_SPEED_MPS))
                    except (TypeError, ValueError):
                        pass
                    m.loop = bool(loop_cb.value)
                    if what == "areas":
                        path = ms.path_through_areas(mission())
                        if not path:
                            ui.notify("no task areas in the plan yet", type="warning")
                            return
                        n = me.set_flight_path(mission(), path, source="through the task areas")
                        audit.add("edit", f"flight path built through the task areas ({n} points)")
                        _refresh_timeline(); _refresh_audit()
                        map_ui["panel"].fit("path")
                        return
                    if what == "play":
                        if m.state != "paused":
                            m.set_path(me.flight_path(mission()))
                        if not m.play():
                            ui.notify("draw or import a flight path first (or build one "
                                      "through the task areas)", type="warning")
                            return
                        audit.add("trigger", f"simulated motion playing at {m.speed_mps:g} m/s"
                                             + (" (loop)" if m.loop else ""))
                    elif what == "pause":
                        m.pause()
                        audit.add("trigger", "simulated motion paused")
                    else:
                        m.stop()
                        audit.add("trigger", "simulated motion stopped")
                    _refresh_audit()
                    _trigger_tick()
                pass
        return card

    def _ac_note(rid) -> str:
        """", aircraft N1234" for an audit line, with two or more aircraft."""
        if len(sess.aircraft_names()) < 2:
            return ""
        return f", aircraft {sess.radio_aircraft(rid)}"

    def _trigger_tick():
        fix, events = sess.trigger_tick()
        panel = map_ui.get("panel")
        eng = sess.triggers
        extra = fix_extras(sess, fix)
        names = sess.aircraft_names()
        if panel is not None:
            # one marker per aircraft, named, an arrow when its feed carries a heading
            if len(names) <= 1:
                items = [(sess.aircraft_shown(names[0]) if names else None, fix, eng.enabled,
                          extra.get("heading_deg"))]
            else:
                items = []
                for name in names:
                    fx = sess.fix_of(name)
                    items.append((name, fx, eng.status(fx)[0],
                                  fix_extras(sess, fx, name).get("heading_deg")))
            panel.set_positions(me.position_markers(items))
        lbl = map_ui.get("pos")
        if lbl is not None:
            armed = "ARMED" if sess.triggers_armed else "off"
            held = eng.held()
            hs = []
            if extra.get("heading_deg") is not None:
                hs.append(f"hdg {int(round(extra['heading_deg'])) % 360:03d}")
            if extra.get("speed_mps") is not None:
                hs.append(f"{extra['speed_mps'] / 0.514444:.0f} kt")
            lbl.set_text(f"triggers {armed}; {eng.reason}" + (f", {', '.join(hs)}" if hs else "")
                         + f"; sources: {sess.feed.sources_text()}"
                         + (f"; held until the platform leaves the area: {', '.join(held)}" if held else ""))
            lbl.classes(replace="text-xs font-mono grow " + (
                "text-emerald-700" if (sess.triggers_armed and eng.enabled)
                else "text-amber-700" if sess.triggers_armed else "text-gray-500"))
        sw = map_ui.get("arm")
        if sw is not None and sw.value != sess.triggers_armed:
            sw.value = sess.triggers_armed       # STOP ALL TX disarms, from either page
        if eng.held() != state.get("held_drawn", []):
            state["held_drawn"] = eng.held()
            _refresh_timeline()
        # the listen switch follows the shared listener, which the operator
        # header panel or the engineering feed panel may have started or stopped
        acs = map_ui.get("ac_sel")
        if acs is not None and list(acs.options) != names:
            acs.set_options(names, value=sess.feeds.resolve(acs.value))
            acs.set_visibility(len(names) > 1)
        # the fields follow a save made on another page; a field changed
        # here and not yet applied keeps its value
        ff = map_ui.get("feed_fields")
        if ff is not None and (ff.stale() or ff.name != sess.feeds.resolve(acs.value)):
            map_ui["fill_feed_fields"](keep_edits=True)
        lsw = map_ui.get("listen_sw")
        want = sess.listening_for(acs.value) if acs is not None else sess.listening
        if lsw is not None and bool(lsw.value) != want:
            map_ui["set_listen_sw"](want)
        fbox = map_ui.get("feed_box")
        if fbox is not None:
            rows = map_ui["feed_rows"]
            if list(rows) != names:
                fbox.clear()
                rows.clear()
                with fbox:
                    for name in names:
                        rows[name] = (ui.label("").classes("text-xs font-mono"),
                                      ui.label("").classes("text-xs font-bold text-red-700 w-full"))
            for name in names:
                flbl, fw = rows[name]
                st = feed_state(sess, name=name if len(names) > 1 else None)
                if len(names) == 1 and sess.aircraft_shown(name):
                    st["aircraft"] = name
                flbl.set_text(feed_state_text(st))
                flbl.classes(replace="text-xs font-mono " + (
                    "text-emerald-700" if st["level"] == "ok"
                    else "text-amber-700" if st["level"] == "warn" else "text-red-700"))
                fw.set_text(st["warning"])
                fw.classes(replace="text-xs font-bold w-full " + (
                    "text-amber-700" if st["level"] == "warn" else "text-red-700"))
                fw.set_visibility(bool(st["warning"]))
        mlbl = map_ui.get("motion")
        if mlbl is not None:
            pr = sess.motion.progress()
            mlbl.set_text("" if pr["state"] == "stopped" else
                          f"{pr['state']}: {pr['done_m'] / 1000:.2f} of {pr['total_m'] / 1000:.2f} km"
                          f"{'' if pr['forward'] else ' (returning)'}")
        for ev in events:
            sid = ev["step"].get("id")
            if ev["kind"] == "leave":
                background_tasks.create(_on_leave(ev))
            elif ev["kind"] == "start" and mf.is_location_task(ev["step"]) \
                    and (sid in sess.confirmed_once
                         or mf.trigger_block(ev["step"]).get("confirm") == "auto"):
                background_tasks.create(_auto_start(ev))
            else:
                background_tasks.create(_trigger_prompt(ev))

    async def _trigger_prompt(ev):
        step = ev["step"]
        sid = step.get("id")
        kind = ev["kind"]
        if sid in sess.prompted:
            return
        sess.prompted.add(sid)
        try:
            rid = step.get("radio")
            where = (f"{ev['geofence'].get('radius_m'):g} m around "
                     f"{ev['geofence'].get('lat')}, {ev['geofence'].get('lon')}"
                     if mf.geofence_kind(ev["geofence"]) == "point" else "the drawn area")
            audit.add("trigger", f"{sid} on {rid}: platform entered its {kind} area ({where})"
                                 f"{_ac_note(rid)}")
            _refresh_audit()
            online = _is_online(rid)
            rtext, rok = _radio_status(rid)       # same readiness check as Run mission
            auth = mf.resolved_authorization(mission(), step) if step.get("radiates") else "n/a"
            errs = [c for c in state["conflicts"]
                    if getattr(c, "level", "") == "error" and getattr(c, "step_id", None) == sid]
            ok = online and rok and (auth or not step.get("radiates")) and not errs
            task = mf.is_location_task(step)
            other = runner.active_on(rid)
            clash = ""
            if kind == "start" and other is not None and other.get("id") != sid:
                mine, theirs = mf.step_priority(step), mf.step_priority(other)
                clash = (f"Radio {rid} is keyed by {other.get('id')} (priority {theirs}); this "
                         + (f"task (priority {mine}) will stop it." if mine < theirs else
                            f"task (priority {mine}) will wait until the radio is free."))
            with dialog_host:
                with ui.dialog() as dlg, ui.card().classes("w-[34rem] gap-1"):
                    if kind == "start":
                        ui.label("Location task: area entered" if task else
                                 "Location trigger: start area entered").classes(
                            "text-lg font-bold text-red-600")
                        ui.label(f"{step['technique']}{_step_detail(step)}  on  {rid}") \
                            .classes("text-sm font-mono")
                        if task:
                            t = mf.trigger_block(step)
                            how = (f"runs {float(t.get('dwell_s') or mf.DEFAULT_DWELL_S):g} s"
                                   if t.get("mode") == "dwell" else
                                   f"runs while the platform is inside (at most "
                                   f"{float(t.get('max_s') or mf.DEFAULT_MAX_S):g} s)")
                            ui.label(f"Keying it RADIATES on that radio; it {how}, or until "
                                     "STOP ALL TX." + (" After this confirm, later entries key "
                                                       "without asking while armed."
                                                       if t.get("confirm", "once") == "once"
                                                       else "")).classes("text-xs")
                        else:
                            ui.label(f"Planned length {mf.step_stop_s(step) - mf.step_start_s(step):g} s. "
                                     "Keying it now RADIATES on that radio until the length is up, "
                                     "a stop area is entered and confirmed, or STOP ALL TX.") \
                                .classes("text-xs")
                        if clash:
                            ui.label(clash).classes("text-xs text-amber-700 font-bold")
                        ui.label(f"radio: {rtext if online else 'OFFLINE'}"
                                 + ("" if rok or not online else " (bring it up first)")
                                 + f"; authorization: {auth or 'NONE on file'}"
                                 + (f"; {len(errs)} validation error(s) on this step" if errs else "")) \
                            .classes("text-xs " + ("text-emerald-700" if ok else "text-red-600"))
                        ui.label("Confirm the chamber / load is connected and safe to key.") \
                            .classes("text-xs text-gray-500")
                        with ui.row().classes("justify-end w-full gap-2"):
                            ui.button("Don't key it", on_click=lambda: dlg.submit("no")).props("flat")
                            b = ui.button("KEY IT NOW", on_click=lambda: dlg.submit("go")) \
                                .props("color=negative")
                            if not ok:
                                b.disable()
                    else:
                        ui.label("Location trigger: stop area entered").classes(
                            "text-lg font-bold text-amber-700")
                        ui.label(f"{step['technique']}  on  {rid} is running") \
                            .classes("text-sm font-mono")
                        with ui.row().classes("justify-end w-full gap-2"):
                            ui.button("Keep running", on_click=lambda: dlg.submit("no")).props("flat")
                            ui.button("STOP IT NOW", on_click=lambda: dlg.submit("go")) \
                                .props("color=negative")
            choice = await dlg
            if choice == "go" and kind == "start" and not sess.triggers_armed:
                audit.add("trigger", f"{sid}: not keyed, the triggers were turned off while "
                                     f"this prompt was open")
                _refresh_audit()
                return
            if choice == "go" and kind == "start" and sess.triggers.is_held(sid):
                audit.add("trigger", f"{sid}: not keyed, it is held until the platform "
                                     f"leaves its area")
                _refresh_audit()
                return
            if choice != "go":
                audit.add("trigger", f"{sid}: {kind} prompt dismissed; the area re-arms "
                                     f"when the platform leaves it")
                _refresh_audit()
                return
            if kind == "start":
                audit.add("confirm", f"{sid} on {rid}: start confirmed from the location trigger")
                r = await runner.start_step_now(step, want=_want(sid) if task else None)
                if r in ("started", "waiting"):
                    sess.triggers.mark_started(sid)
                    if task and mf.trigger_block(step).get("confirm", "once") == "once":
                        sess.confirmed_once.add(sid)
                note = ((f"{sid} keyed on {rid}", "warning") if r == "started" else
                        (f"{sid} waits for radio {rid}", "warning") if r == "waiting" else
                        (f"{sid} did not start (see the audit log)", "negative"))
            else:
                audit.add("confirm", f"{sid} on {rid}: stop confirmed from the location trigger")
                await runner.stop_step_now(step)
                sess.triggers.mark_stopped(sid)
                note = (f"{sid} stopped on {rid}", "warning")
            # after the await this task has no page context of its own; the
            # dialog host's slot gives the refreshes and ui.notify one
            with dialog_host:
                _refresh_audit(); _refresh_run_state(); _refresh_timeline()
                ui.notify(note[0], type=note[1])
        except Exception as ex:
            audit.add("trigger", f"{sid}: prompt handling failed: {ex}")
            _refresh_audit()
        finally:
            sess.prompted.discard(sid)

    def _trigger_editor(step):
        """Mode, length, confirm and priority of a location task. Returns
        (reader: (trigger dict, priority), widgets for dirty marking)."""
        t = mf.trigger_block(step)
        with ui.row().classes("items-center gap-1 w-full flex-wrap"):
            mode_in = ui.select(dict(mf.TRIGGER_MODE_LABELS),
                                value=t.get("mode") if t.get("mode") in mf.TRIGGER_MODES
                                else "while_inside", label="when") \
                .props("dense outlined").classes("w-56")
        with ui.row().classes("items-center gap-1 w-full flex-wrap"):
            dwell_in = ui.number("run for (s)", value=float(t.get("dwell_s") or mf.DEFAULT_DWELL_S),
                                 min=1, format="%g").props("dense outlined").classes("w-28") \
                .tooltip("a dwell task keys for this long once the point is reached")
            max_in = ui.number("at most (s)", value=float(t.get("max_s") or mf.DEFAULT_MAX_S),
                               min=1, format="%g").props("dense outlined").classes("w-28") \
                .tooltip("a while-inside task stops when the platform leaves, or after this")
        with ui.row().classes("items-center gap-1 w-full flex-wrap"):
            conf_in = ui.select(dict(mf.CONFIRM_LABELS),
                                value=t.get("confirm") if t.get("confirm") in mf.CONFIRM_MODES
                                else "once", label="confirm").props("dense outlined").classes("w-72")
            prio_in = ui.number("priority (1 high, 9 low)", value=mf.step_priority(step),
                                min=1, max=9, step=1, format="%d") \
                .props("dense outlined").classes("w-40").tooltip(
                "against a timeline step on the same radio the higher priority wins; "
                "equal priority waits")

        def _vis(_=None):
            dwell_in.set_visibility(mode_in.value == "dwell")
            max_in.set_visibility(mode_in.value != "dwell")
        mode_in.on_value_change(_vis)
        _vis()

        def _read():
            mode = mode_in.value
            try:
                dwell = float(dwell_in.value or 0)
                mx = float(max_in.value or 0)
                prio = int(prio_in.value or mf.DEFAULT_LOCATION_PRIORITY)
            except (TypeError, ValueError):
                raise ValueError("trigger: run for, at most and priority must be numbers")
            if mode == "dwell" and dwell <= 0:
                raise ValueError("trigger: run for must be more than 0 s")
            if mode != "dwell" and mx <= 0:
                raise ValueError("trigger: at most must be more than 0 s")
            if not 1 <= prio <= 9:
                raise ValueError("trigger: priority is 1 to 9")
            return ({"mode": mode, "dwell_s": dwell, "max_s": mx,
                     "confirm": conf_in.value}, prio)
        return _read, (mode_in, dwell_in, max_in, conf_in, prio_in)

    def _new_task_shape_dialog():
        """New location task: first the shape on the map, then the task."""
        with dialog_host:
            with ui.dialog() as dlg, ui.card().classes("gap-1"):
                ui.label("New location task").classes("text-base font-bold")
                ui.label("Draw where it applies, then pick the task.").classes("text-xs text-gray-500")
                with ui.row().classes("gap-2"):
                    ui.button("Draw a box", icon="crop_square",
                              on_click=lambda: (dlg.submit(None), _start_draw(
                                  "box", "newtask", "click two corners of the area"))).props("outline").tooltip("drag a box on the map")
                    ui.button("Draw a polygon", icon="polyline",
                              on_click=lambda: (dlg.submit(None), _start_draw(
                                  "polygon", "newtask",
                                  "click the corners, double-click to finish"))).props("outline").tooltip("click the corners, double-click to finish")
                    ui.button("Mark a point", icon="place",
                              on_click=lambda: (dlg.submit(None), _start_draw(
                                  "point", "newtask", "click the point"))).props("outline").tooltip("click the map once")
                    ui.button("Cancel", on_click=lambda: dlg.submit(None)).props("flat")
        background_tasks.create(dlg)

    async def _new_task_dialog(d):
        """The task for a shape just drawn (map first): technique, radio, mode,
        length, radius for a point, priority, confirm."""
        mode_drawn = d.get("mode")
        if mode_drawn == "box" and d.get("bbox"):
            w, s_, e_, n = d["bbox"]
            gf = {"polygon": [[w, s_], [e_, s_], [e_, n], [w, n]]}
        elif mode_drawn == "polygon" and d.get("polygon"):
            gf = {"polygon": [[float(p[0]), float(p[1])] for p in d["polygon"]]}
        elif mode_drawn == "point":
            gf = {"lat": round(float(d["lat"]), 6), "lon": round(float(d["lon"]), 6),
                  "radius_m": 500.0}
        else:
            return
        radios = lane_radios()
        techs = {}
        for group, entries in catalog.groups_for_tab():
            for e in entries:
                if e.technique == "set_rate":
                    continue
                techs[e.technique] = f"{group}: {e.label}"
        with dialog_host:
            with ui.dialog() as dlg, ui.card().classes("w-[36rem] gap-1"):
                ui.label("New location task").classes("text-base font-bold")
                ui.label({"box": "a box", "polygon": "a drawn area", "point": "a point"}[mode_drawn]
                         + " on the map").classes("text-xs text-gray-500")
                tech_in = ui.select(techs, value="jammer.barrage" if "jammer.barrage" in techs
                                    else next(iter(techs)), label="task").props("dense outlined") \
                    .classes("w-full")
                radio_in = ui.select(radios, value=radios[0] if radios else None, label="radio") \
                    .props("dense outlined").classes("w-full")
                mode_in = ui.select(dict(mf.TRIGGER_MODE_LABELS),
                                    value="dwell" if mode_drawn == "point" else "while_inside",
                                    label="when").props("dense outlined").classes("w-full")
                with ui.row().classes("items-center gap-1 w-full flex-wrap"):
                    dwell_in = ui.number("run for (s)", value=mf.DEFAULT_DWELL_S, min=1,
                                         format="%g").props("dense outlined").classes("w-28")
                    max_in = ui.number("at most (s)", value=mf.DEFAULT_MAX_S, min=1,
                                       format="%g").props("dense outlined").classes("w-28")
                    rad_in = ui.number("radius (m)", value=500.0, min=1, format="%g") \
                        .props("dense outlined").classes("w-28")
                    rad_in.set_visibility(mode_drawn == "point")
                with ui.row().classes("items-center gap-1 w-full flex-wrap"):
                    prio_in = ui.number("priority (1 high, 9 low)", value=mf.DEFAULT_LOCATION_PRIORITY,
                                        min=1, max=9, step=1, format="%d") \
                        .props("dense outlined").classes("w-40")
                    conf_in = ui.select(dict(mf.CONFIRM_LABELS), value="once",
                                        label="confirm").props("dense outlined").classes("w-72")
                ui.label("Timeline steps on the same radio: the higher priority wins, equal "
                         "priority waits for the radio.").classes("text-xs text-gray-500")

                def _vis(_=None):
                    dwell_in.set_visibility(mode_in.value == "dwell")
                    max_in.set_visibility(mode_in.value != "dwell")
                mode_in.on_value_change(_vis)
                _vis()
                with ui.row().classes("justify-end w-full gap-2"):
                    ui.button("Cancel", on_click=lambda: dlg.submit(False)).props("flat")
                    ui.button("Add task", on_click=lambda: dlg.submit(True)).props("color=primary")
        if not await dlg:
            return
        if not radio_in.value:
            ui.notify("no radio to put the task on; add one first", type="warning")
            return
        if mode_drawn == "point":
            gf["radius_m"] = float(rad_in.value or 500.0)
        sid = _new_step_id()
        step = mf.new_location_task(sid, tech_in.value, radio_in.value, gf,
                                    mode=mode_in.value,
                                    dwell_s=float(dwell_in.value or mf.DEFAULT_DWELL_S),
                                    max_s=float(max_in.value or mf.DEFAULT_MAX_S),
                                    priority=int(prio_in.value or mf.DEFAULT_LOCATION_PRIORITY),
                                    confirm=conf_in.value)
        ensure_radio(radio_in.value)
        mission()["steps"].append(step)
        audit.add("edit", f"location task {sid}: {tech_in.value} on {radio_in.value}, "
                          f"{mf.TRIGGER_MODE_LABELS.get(mode_in.value)}, priority "
                          f"{step['priority']}")
        # after the await this task has no page context; the dialog host's
        # slot gives the refreshes and the notify one
        with dialog_host:
            _revalidate(); _refresh_all(); _refresh_audit()
            _select(sid)
            ui.notify("location task added; arm the triggers under the map to use it",
                      type="positive")

    def _want(sid):
        """Still wanted while waiting for the radio: the platform is in its area."""
        return lambda: (sess.triggers.step_state(sid).get("in_start") is True
                        and not sess.triggers.is_held(sid))

    async def _auto_start(ev):
        """A location task confirmed once before: key it without asking."""
        step = ev["step"]
        sid = step.get("id")
        rid = step.get("radio")
        if not sess.triggers_armed or sess.triggers.is_held(sid):
            return                               # stopped in the moment since the entry
        rtext, rok = _radio_status(rid)
        auth = mf.resolved_authorization(mission(), step) if step.get("radiates") else "n/a"
        if not (_is_online(rid) and rok and (auth or not step.get("radiates"))):
            audit.add("trigger", f"{sid} on {rid}: entered its area but not keyed: radio "
                                 f"{rtext}, authorization {auth or 'none'}")
            _refresh_audit()
            return
        why = ("its confirm rule is automatic" if mf.trigger_block(step).get("confirm") == "auto"
               else "confirmed earlier")
        audit.add("trigger", f"{sid} on {rid}: entered its area; {why}, keying without asking"
                             f"{_ac_note(rid)}")
        r = await runner.start_step_now(step, want=_want(sid))
        if r in ("started", "waiting"):
            sess.triggers.mark_started(sid)
        with dialog_host:
            _refresh_audit(); _refresh_run_state(); _refresh_timeline()

    async def _on_leave(ev):
        """The platform left a running task's area: a while-inside task stops;
        a dwell task runs on to its length."""
        step = ev["step"]
        sid = step.get("id")
        if not mf.is_location_task(step) or mf.trigger_block(step).get("mode") == "dwell":
            return
        await runner.stop_step_now(step, reason="because the platform left its area")
        sess.triggers.mark_stopped(sid, rearm=True)
        with dialog_host:
            _refresh_audit(); _refresh_run_state(); _refresh_timeline()

    def _section(title):
        with ui.element("div").classes("mp-section"):
            ui.label(title).classes("mp-section-title")

    def _location_editor(step, sid, form_widgets, roles=("start", "stop")):
        """The start and stop areas of a step in the editor (a location task
        has just its area). Typed values go in on Apply (the returned reader);
        a map pick writes the step at once. Returns a reader giving the
        location dict ({} = none)."""
        loc = dict(step.get("location") or {})
        only_area = tuple(roles) == ("start",)
        _section("Location")
        ui.label("the area this task keys in" if only_area else "start and stop areas") \
            .classes("text-xs text-gray-500").tooltip(
            "Entering the area asks to key this task." if only_area else
            "Entering the start area asks to key this step; entering the stop "
            "area asks to stop it. Nothing keys without the confirm.")
        readers = {}
        for role in roles:
            gf = dict(loc.get(role) or {})
            kind0 = mf.geofence_kind(gf) or "none"
            with ui.row().classes("items-center gap-1 w-full flex-wrap"):
                ui.label("area" if only_area else f"{role} area").classes("text-xs w-14 shrink-0")
                ksel = ui.select({"none": "none", "point": "point + radius", "polygon": "polygon"},
                                 value=kind0).props("dense outlined").classes("w-32")
                pick = ui.button(icon="place",
                                 on_click=lambda r=role: _start_draw(
                                     "point", f"loc:{sid}:{r}", f"click the map to place the {r} point")) \
                    .props("dense flat").tooltip("pick the point on the map")
                drawb = ui.button(icon="polyline",
                                  on_click=lambda r=role: _start_draw(
                                      "polygon", f"loc:{sid}:{r}",
                                      f"click the {r} area's corners, double-click to finish")) \
                    .props("dense flat").tooltip("draw the polygon on the map")
            with ui.row().classes("items-center gap-1 w-full flex-wrap") as prow:
                lat_in = ui.number("lat", value=gf.get("lat"), format="%.6f", step=0.0001) \
                    .props("dense outlined").classes("w-28")
                lon_in = ui.number("lon", value=gf.get("lon"), format="%.6f", step=0.0001) \
                    .props("dense outlined").classes("w-28")
                rad_in = ui.number("radius m", value=gf.get("radius_m", 500.0), format="%g", min=1) \
                    .props("dense outlined").classes("w-24")
            npts = len(gf.get("polygon") or [])
            poly_lbl = ui.label(f"polygon: {npts} vertices" if npts else
                                "polygon: none yet, draw it on the map").classes("text-xs text-gray-600")
            if gf.get("name"):
                ui.label(f"from the file: {gf['name']}").classes("text-xs text-gray-600")

            def _vis(k=None, ksel=ksel, prow=prow, poly_lbl=poly_lbl, pick=pick, drawb=drawb):
                v = ksel.value
                prow.set_visibility(v == "point")
                poly_lbl.set_visibility(v == "polygon")
                pick.set_visibility(v == "point")
                drawb.set_visibility(v == "polygon")
            ksel.on_value_change(_vis)
            _vis()
            form_widgets += [ksel, lat_in, lon_in, rad_in]

            def _read(ksel=ksel, lat_in=lat_in, lon_in=lon_in, rad_in=rad_in, gf=gf, role=role):
                v = ksel.value
                if v == "none":
                    return None
                if v == "point":
                    if lat_in.value in (None, "") or lon_in.value in (None, ""):
                        raise ValueError(f"{role} area: give a lat and lon or pick it on the map")
                    out = {"lat": float(lat_in.value), "lon": float(lon_in.value),
                           "radius_m": float(rad_in.value or 0)}
                else:
                    out = {"polygon": gf.get("polygon") or []}
                if gf.get("name"):
                    out["name"] = gf["name"]       # the name it had in a dropped file
                ok, why = mf.validate_geofence(out)
                if not ok:
                    raise ValueError(f"{role} area: {why}")
                return out
            readers[role] = _read

        def _reader():
            out = {}
            for role, rd in readers.items():
                v = rd()
                if v:
                    out[role] = v
            return out
        return _reader

    dialog_host = ui.element("div")

    # Delete / Backspace removes the selected step(s) without opening the editor;
    # Esc clears the selection.
    # ignore keeps it from firing while the operator is typing in a field.
    def _on_key(e):
        if not e.action.keydown:
            return
        k = str(getattr(e.key, "name", e.key))
        if k in ("Delete", "Backspace"):
            _delete_selected()
        elif k == "Escape":
            _clear_selection()

    ui.keyboard(on_key=_on_key,
                ignore=["input", "select", "button", "textarea"])

    # body: catalog | timeline | editor+audit

    with ui.row().classes("w-full gap-3 no-wrap items-start"):
        # the left column scrolls on its own, so Mission radios and the whole
        # Catalog stay reachable without scrolling the page
        with ui.column().classes("w-72 shrink-0 gap-3").style(
                "max-height:calc(100vh - 150px);overflow-y:auto"):
            # the radios planned for this mission and how they start together
            with ui.card().classes("w-full gap-1"):
                ui.label("Mission radios").classes("text-sm font-bold")
                # anchor: how step times map to real time. on_tx = every radio
                # runs from the Run mission confirm; clock = every radio runs on the
                # 24 h UTC timeline for a chosen date.
                anchor_sel = ui.select(
                    {"on_tx": "Start all together on Run mission",
                     "clock": "Start all at planned UTC times"},
                    label="start").props("dense outlined").classes("w-full")
                anchor_sel.value = mf.anchor_mode(mission())
                date_in = ui.input("date (UTC)").props("dense outlined") \
                    .classes("w-full")
                date_in.value = (mission().get("anchor") or {}).get("date", "")
                ui.label("All radios run from one common start and line up to "
                         "within the link and command delay.").classes(
                    "text-xs text-gray-500")
                radios_box = ui.column().classes("w-full gap-0")
                ui.button("Add radio", icon="add",
                          on_click=lambda: _add_radio_dialog()).props(
                    "dense outline no-caps").classes("w-full")

                def _on_anchor(e=None):
                    # a change made by _refresh_all while it copies a loaded plan
                    # into these fields is not a user edit: writing the field
                    # values back then would put the previous plan's date into
                    # the plan just opened
                    if state.get("anchor_sync"):
                        return
                    a = mission().setdefault("anchor", {"mode": "on_tx", "tz": "UTC"})
                    was = a.get("mode")
                    a["mode"] = anchor_sel.value
                    if a["mode"] == "clock" and not date_in.value:
                        date_in.value = _today_utc()
                    a["date"] = date_in.value or ""
                    date_in.set_visibility(a["mode"] == "clock")
                    # pick a sane view when the timeline kind changes: fill the
                    # width with the whole window (the 24 h day on the clock,
                    # the mission's elapsed window when relative).
                    if a["mode"] != was:
                        state["view_s"] = VIEW_S
                        state["homed"] = False
                    _revalidate(); _refresh_all()
                anchor_sel.on_value_change(_on_anchor)
                date_in.on_value_change(_on_anchor)
                date_in.set_visibility(anchor_sel.value == "clock")

            # catalog: collapsible groups, a search box, click or drag
            with ui.card().classes("w-full gap-1"):
                with ui.row().classes("items-center gap-1 w-full no-wrap"):
                    ui.label("Catalog").classes("text-sm font-bold grow")
                    ui.icon("info").classes("text-gray-400 cursor-help").tooltip(
                        "click a technique to add it to the selected radio, or drag it "
                        "onto a lane; an amber tower marks one that radiates")
                lane_target = ui.select([], label="add to radio").props(
                    "dense outlined").classes("w-full")
                cat_search = ui.input(placeholder="search the catalog").props(
                    "dense outlined clearable").classes("w-full")
                cat_cards = []                 # (label lower, card, group expansion)
                with ui.element("div").style("width:100%").classes("gap-1"):
                    for group, entries in catalog.groups_for_tab():
                        with ui.expansion(group, value=True).props("dense expand-separator") \
                                .classes("w-full text-xs") as grp:
                            for entry in entries:
                                card = _catalog_card(entry, lane_target, state, add_step,
                                                     lane_radios)
                                cat_cards.append((entry.label.lower(), card, grp))

                def _cat_filter(_=None):
                    q = (cat_search.value or "").strip().lower()
                    shown_groups = set()
                    for lab, card, grp in cat_cards:
                        hit = (not q) or (q in lab)
                        card.set_visibility(hit)
                        if hit:
                            shown_groups.add(id(grp))
                    for _lab, _card, grp in cat_cards:
                        grp.set_visibility(id(grp) in shown_groups)
                cat_search.on_value_change(_cat_filter)

        # the middle: Timeline and Map as two tabs, so each gets the whole
        # width and height the operator has, and the step editor and the audit
        # log under them as a second pair of tabs. The two views stay in the
        # DOM (shown and hidden, not tab panels) so the map and its events
        # exist from the first paint whichever tab is open.
        with ui.column().classes("grow min-w-0 gap-2"):
            timeline_card = ui.card().classes("w-full min-w-0")
            with timeline_card:
                with ui.row().classes("items-center gap-1 w-full no-wrap"):
                    ui.label("Timeline").classes("text-sm font-bold grow")
                    ui.icon("info").classes("text-gray-400 cursor-help").tooltip(
                        "one lane per radio, with the radio's colour on its left edge and "
                        "as a band on each block; click a block to edit, ctrl-click to select "
                        "several, drag onto a lane to move to that radio or time; a red "
                        "edge and ! mean a conflict (the only red); the fill says what the "
                        "step does (amber transmit, blue receive, brown jammer, grey tool); "
                        "hatched and faded: its radio is offline; "
                        "click a lane's name or empty space to select that radio for the map")
                timeline_box = ui.column().classes("w-full gap-1 min-w-0")
            map_card = _build_map_card()

            def _show_main(which):
                for _n, _el in main_view["segs"].items():
                    _el.classes(add="mp-seg-on" if _n == which else "",
                                remove="" if _n == which else "mp-seg-on")
                    _el.props(f'aria-pressed={"true" if _n == which else "false"}')
                timeline_card.set_visibility(which == "Timeline")
                map_card.set_visibility(which == "Map")
                if which == "Map":
                    # the map was laid out while hidden; let it take its size
                    ui.run_javascript("window.missionMap && window.missionMap.resize "
                                      "&& window.missionMap.resize()")

            map_ui["show_map"] = lambda: _show_main("Map")
            _show_main("Timeline")

            # the same segmented look as the Timeline / Map switch
            with ui.tabs().props("dense inline-label align=left") \
                    .classes("w-full mp-segtabs") as right_tabs:
                tab_editor = ui.tab("Step editor", icon="edit_note")
                tab_audit = ui.tab("Audit log", icon="receipt_long")
            with ui.tab_panels(right_tabs, value=tab_editor).classes(
                    "w-full").props("keep-alive"):
                with ui.tab_panel(tab_editor).classes("p-0"):
                    editor_box = ui.card().classes("w-full")
                with ui.tab_panel(tab_audit).classes("p-0"):
                    audit_box = ui.log(max_lines=200).classes(
                        "w-full h-72 text-xs font-mono")

    # rendering

    def _refresh_all():
        name_in.value = mission()["name"]
        auth_in.value = mission().get("authorization", {}).get("reference", "")
        # keep the start controls in step with the plan (a loaded file brings
        # its own anchor); only write on a real change so this does not loop
        a = mission().get("anchor") or {}
        state["anchor_sync"] = True
        try:
            if (date_in.value or "") != (a.get("date") or ""):
                date_in.value = a.get("date") or ""
            if anchor_sel.value != a.get("mode", "on_tx"):
                anchor_sel.value = a.get("mode", "on_tx")
        finally:
            state["anchor_sync"] = False
        date_in.set_visibility(anchor_sel.value == "clock")
        _refresh_radios()
        _refresh_show_bar()
        state["radios_sig"] = _radios_sig()
        _refresh_catalog_target()
        _refresh_clock_strip()
        _refresh_conflicts()
        _refresh_timeline()
        _refresh_editor()
        _refresh_run_state()

    def _refresh_catalog_target():
        opts = lane_radios()
        lane_target.options = opts
        if lane_target.value not in opts:
            lane_target.value = opts[0] if opts else None
        lane_target.update()

    def _refresh_conflicts():
        # a step that needs a file picked (IQ playback placeholder) is
        # "needs input", not an error: an expected blank until the file is
        # chosen
        def _needs_input(c):
            m = c.message or ""
            return "no file chosen" in m or "no scenario JSON" in m
        errs = [c for c in state["conflicts"] if c.level == "error" and not _needs_input(c)]
        needs = [c for c in state["conflicts"] if c.level == "error" and _needs_input(c)]
        warns = [c for c in state["conflicts"] if c.level == "warning"]

        def _toggle():
            state["conflicts_open"] = not state.get("conflicts_open", True)
            _refresh_conflicts()

        conflict_chips.clear()
        with conflict_chips:
            if not errs and not warns and not needs:
                with ui.row().classes("items-center gap-1 no-wrap mp-status mp-status-ok"):
                    ui.icon("check_circle")
                    ui.label("no conflicts")
            for n, word, kind, icon in ((len(errs), "error", "err", "error"),
                                        (len(warns), "warning", "warn", "warning"),
                                        (len(needs), "needs input", "warn", "edit_note")):
                if n:
                    st = ui.row().classes(
                        f"items-center gap-1 no-wrap mp-status mp-status-{kind} cursor-pointer") \
                        .props("role=button tabindex=0") \
                        .tooltip("show or hide the details")
                    st.on("click", lambda e: _toggle())
                    with st:
                        ui.icon(icon)
                        ui.label(f"{n} {word}{'' if n == 1 or word == 'needs input' else 's'}")
        conflict_box.clear()
        conflict_box.set_visibility(bool(errs or warns or needs) and state.get("conflicts_open", True))
        with conflict_box:
            for c in errs:
                where = f"[{c.step_id}] " if c.step_id else ""
                ui.label(f"error: {where}{c.message}").classes("text-xs text-red-600")
            for c in needs:
                where = f"[{c.step_id}] " if c.step_id else ""
                ui.label(f"needs input: {where}{c.message}").classes("text-xs text-amber-700")
            for c in warns:
                where = f"[{c.step_id}] " if c.step_id else ""
                ui.label(f"warning: {where}{c.message}").classes("text-xs text-amber-700")

    def _refresh_clock_strip():
        clock_strip.clear()
        with clock_strip:
            with ui.row().classes("items-center gap-1 no-wrap mp-clock") \
                    .tooltip("this console's clock, UTC: steps are keyed from it"):
                ui.icon("schedule")
                ui.label(f"CONSOLE UTC {_utc_now_hms()}")
            for serial, host in list(mgr.hosts.items()):
                cl = state["radio_clock"].get(serial) or {}
                clk = cl.get("ref_clock", "?")
                locked = cl.get("gpsdo_locked")
                # on an external 10 MHz there is no GPSDO to lock; a measured
                # reference on the input is the equivalent
                if not locked and "external" in str(clk):
                    locked = cl.get("ext_ref_present")
                # a G40 service that measures its clock sends a clock dict: the
                # choice by name, the card clock against the 1 PPS and whether the
                # 10 MHz is in use or LOST (g40_clock.clock_text)
                ck = cl.get("clock") or {}
                ten = ck.get("ten_mhz")
                gnss = cl.get("gnss") or {}
                # a receiver with no fix still gives a time (2019-04-07 on a cold
                # start), which is not GPS time
                tutc = gnss.get("time_utc") if _g40_clock.has_fix(gnss) else None
                # the radio's GPS/UTC time when it has a fix; otherwise say so.
                if tutc:
                    tshort = str(tutc)[11:19] or str(tutc)
                    tdesc = f"GPS {tshort}"
                    skew = _skew_vs_console(tutc)
                    if skew is not None:
                        tdesc += f" (skew {skew:+.1f}s)"
                else:
                    # gnss is a dict (gpsd answered, no fix yet) on a G40 and
                    # None on a radio with no GPS receiver, such as the B200
                    # (the fix itself is in the lock words on a GPS disciplined clock)
                    tdesc = "no GPS time" if cl.get("gnss") else "no GPS receiver"
                lock_txt = ("ref LOCKED" if locked
                            else "ref not confirmed" if "external" in str(clk)
                            else "ref unlocked")
                if ck.get("choice"):
                    clk = _g40_clock.label(ck["choice"])
                    if ten is not None:
                        lock_txt = _g40_clock.clock_text({k: ck.get(k) for k in ("ten_mhz", "clock_ppm")})
                        lock_txt = lock_txt.split(", ", 1)[1] if ", " in lock_txt else lock_txt
                    elif ck.get("clock_ppm") is not None and ck.get("ref") not in ("gpsdo", "host"):
                        lock_txt = f"clock {ck['clock_ppm']:+.2f} ppm vs 1 PPS"
                # GPS disciplined: the fix next to the lock. Green only for a lock
                # with a fix; locked with no fix is amber, the X40 GPSDO (lock not
                # readable) grey
                gref = (_g40_clock.CLOCK_CHOICES.get(str(ck.get("choice") or "")) or {}).get("ref") \
                    or ("gpsdo" if cl.get("gpsdo_enabled") else None)
                gv = _g40_clock.gpsdo_verdict(gref, cl.get("gpsdo_locked"), cl.get("gnss"))
                if gv["text"]:
                    lock_txt = gv["text"]
                    locked = gv["state"] == "locked"
                # the 1 PPS input, when the radio reports it: on the aircraft
                # the 1 PPS comes from the platform and no 10 MHz is promised,
                # so say which is there. Nothing in DPMG keys from the PPS; it
                # is a time mark for the radio's own logs.
                pps = ck.get("pps_present", cl.get("pps_present"))
                if pps is not None:
                    src = ck.get("pps") or cl.get("pps_line") or ("external" if cl.get("pps_source") == 0 else None)
                    lock_txt += f", 1 PPS {'present' if pps else 'ABSENT'}" + (f" ({src})" if src else "")
                # the radio host's OS clock against the console's; the mission
                # is keyed from the console, so this is about the radio's own
                # logs and recordings, but a big skew is still worth a look
                skew = cl.get("_os_skew_s")
                if skew is None:
                    os_txt, skewed = "OS clock not reported", False
                else:
                    # the radio host's clock right now (console now + its skew)
                    radio_now = time.strftime("%H:%M:%S", time.gmtime(time.time() + skew))
                    os_txt = f"OS clock {radio_now} ({skew:+.1f} s)"
                    skewed = abs(skew) > CLOCK_SKEW_WARN_S
                good = bool(locked) and not skewed
                lost = ten == "LOST"
                grey = gv["state"] == "unread" and not skewed
                ui.chip(f"{serial}: clk {clk}, {lock_txt}, {tdesc}, {os_txt}", icon="radio",
                        color="red-1" if lost else "green-1" if good else "grey-3" if grey else "amber-1",
                        text_color="red-10" if lost else "green-9" if good else "grey-9" if grey
                        else "amber-10").props("dense") \
                    .tooltip(f"host {(getattr(host, 'addr', None) or ('?',))[0]}: clock source, "
                             "reference lock, GPS time and that host's OS clock against this "
                             "console (half the round trip taken out); radios on one Jetson "
                             "share its clock")
            # the aircraft feed's own time against this console, when the
            # feed carries one: a UTC plan is keyed from the console clock, so a
            # console that disagrees with the aircraft is worth seeing here
            try:
                off = sess.listener.feed_offset_s() if sess.listener is not None else None
            except Exception:
                off = None
            if off is not None and sess.listening:
                mean, lo, hi, n = off
                bad = abs(mean) > CLOCK_SKEW_WARN_S
                ui.chip(f"aircraft feed time {mean:+.1f} s vs console", icon="flight",
                        color="amber-1" if bad else "green-1",
                        text_color="amber-10" if bad else "green-9").props("dense") \
                    .tooltip(f"the feed's own time stamps minus this console's clock at "
                             f"arrival, over the last {n} fix(es): {lo:+.2f} to {hi:+.2f} s. "
                             "Steps are keyed from the console clock, so a large offset "
                             "means the console or the aircraft clock needs a look")
        sync_btn.set_visibility(bool(mgr.hosts))

    def _skew_vs_console(time_utc):
        import datetime
        try:
            s = str(time_utc).replace("Z", "+00:00")
            dt = datetime.datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            now = datetime.datetime.now(datetime.timezone.utc)
            return (dt - now).total_seconds()
        except Exception:
            return None

    def _bind_live_radios():
        """Planned radios that are not online yet: one with no address binds
        to a live radio whose serial matches its name (and takes its address);
        one with an address whose radio answered under another serial takes
        that serial. Runs every few seconds so a radio that comes up later
        (a Super Nano plugged in after the plan was made) joins by itself."""
        live = {h.serial: h for h in mgr.hosts.values()
                if h.session_open and not str(h.serial).startswith("@")}
        changed = False
        for r in list(mission().get("radios", [])):
            rid = r.get("id")
            if rid in live:
                continue
            addr = _parse_address(r.get("address", ""))
            if addr is not None:
                host = next((h for h in live.values()
                             if tuple(getattr(h, "addr", ()) or ()) == addr), None)
            else:
                serial = match_planned_radio(rid, live.keys())
                host = live.get(serial) if serial else None
            if host is None or _plan_radio(host.serial) is not None:
                continue
            if addr is None and getattr(host, "addr", None):
                r["address"] = f"{host.addr[0]}:{host.addr[1]}"
            r["id"] = host.serial
            for s in _steps_on(rid):
                s["radio"] = host.serial
            ht = getattr(host, "host_type", None)
            if not r.get("type") and ht in (0, 1):
                r["type"] = {0: "g40", 1: "x40"}[int(ht)]
            audit.add("radio", f"planned radio {rid} is {host.serial}"
                      + (f" at {r['address']}" if addr is None else "") + "; bound")
            changed = True
        if changed:
            _refresh_all()

    async def _poll_radio_clocks():
        try:
            _bind_live_radios()
        except Exception as e:
            audit.add("radio", f"binding planned radios failed: {e}")
        for serial, host in list(mgr.hosts.items()):
            try:
                t0 = time.time()
                ack = await host.cmd("gps_status", {}, timeout=3.0)
                t1 = time.time()
                res = dict(ack.result or {})
                # a radio that is not initialized still reports its host_time
                if getattr(ack, "status", 1) == 0 or "host_time" in res:
                    res["_os_skew_s"] = os_skew_s(res.get("host_time"), t0, t1)
                    res["_rtt_s"] = t1 - t0
                    state["radio_clock"][serial] = res
            except Exception:
                pass
        _refresh_clock_strip()

    async def _set_time_dialog():
        """Set the time by hand, for a field setup with no internet and no
        GPS: a UTC time pushed to the radios at once (set_time), to this
        console (passwordless sudo on Linux, the administrator prompt on
        Windows, else the command is shown), or both."""
        with dialog_host:
            with ui.dialog() as dlg, ui.card().classes("w-[32rem] gap-1"):
                ui.label("Set the time by hand").classes("text-base font-bold")
                if sys.platform == "win32":
                    how_console = ("Setting this console's clock opens the Windows "
                                   "administrator prompt unless the console was started as "
                                   "administrator. ")
                    ntp_note = ("A Windows console is not the radios' time source; push "
                                "the time to the radios as well.")
                else:
                    how_console = ""
                    ntp_note = ("The console is the radios' NTP source, so a console set "
                                "also reaches them on their next poll (within a few minutes).")
                ui.label("For a setup with no internet and no GPS. Type the UTC time, then "
                         "push it to the radios (at once), to this console, or both. "
                         + how_console + ntp_note) \
                    .classes("text-xs text-gray-600")
                t_in = ui.input("UTC date and time",
                                value=time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())) \
                    .props("dense outlined").classes("w-full font-mono")
                msg = ui.label("").classes("text-xs min-h-[1.1rem]")

                def _target():
                    t = parse_utc_text(t_in.value)
                    if t is None:
                        msg.set_text("use YYYY-MM-DD HH:MM:SS, UTC")
                    return t

                async def _radios():
                    t = _target()
                    if t is None:
                        return
                    dlg.close()
                    await _sync_radio_clocks(target=t)

                async def _console():
                    t = _target()
                    if t is None:
                        return
                    ok, text = await asyncio.get_running_loop().run_in_executor(
                        None, set_console_clock, t)
                    with dialog_host:
                        audit.add("clock", text)
                        _refresh_audit()
                        _refresh_clock_strip()
                    msg.set_text(text)
                    msg.classes(replace="text-xs min-h-[1.1rem] " + ("text-green-700" if ok else "text-amber-700"))

                with ui.row().classes("justify-end w-full gap-1"):
                    ui.button("Cancel", on_click=dlg.close).props("flat")
                    ui.button("Set this console", on_click=_console).props("outline") \
                        .tooltip("needs the Windows administrator prompt (approve it when it "
                                 "appears); otherwise the command to run is shown here"
                                 if sys.platform == "win32" else
                                 "needs passwordless sudo on this console; otherwise the "
                                 "command to run is shown here")
                    ui.button("Set the radios", on_click=_radios).props("color=primary") \
                        .tooltip("push this time to every connected radio host now")
            dlg.open()

    async def _sync_radio_clocks(target=None):
        """Step every connected radio's OS clock to this console's UTC, or to
        `target` (epoch seconds typed by hand), with the service's set_time
        (needs the privileged container). Reports per radio in the audit log
        and a toast, then re-reads the clocks."""
        done, failed = [], []
        what = ("set by hand to " + time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(target))
                if target is not None else "synced to the console")
        synced = {}                      # host ip: the radio that stepped it
        for serial, host in list(mgr.hosts.items()):
            ip = (getattr(host, "addr", None) or ("?",))[0]
            if ip in synced:
                # several radio services on one Jetson share its OS clock
                done.append(f"{serial} shares the host clock with {synced[ip]}")
                continue
            rtt = (state["radio_clock"].get(serial) or {}).get("_rtt_s") or 0.0
            try:
                base = time.time() if target is None else target
                ack = await host.cmd("set_time", {"utc": base + rtt / 2.0}, timeout=3.0)
            except Exception as e:
                failed.append(f"{serial}: no answer ({e})")
                continue
            res = ack.result or {}
            if getattr(ack, "status", 1) == 0:
                synced[ip] = serial
                done.append(f"{serial} ({ip}) stepped {float(res.get('stepped_s', 0.0)):+.2f} s")
            else:
                err = str(res.get("err", "refused"))
                if "unknown cmd" in err:
                    err = "radio service has no set_time (older build)"
                failed.append(f"{serial}: {err}")
        await _poll_radio_clocks()
        # a radio with GPS time that disagrees with the console: the console
        # is what keys the steps, and it has just been pushed to the radios, so
        # say so rather than let a wrong console clock pass quietly
        gps_warn = []
        for serial in list(mgr.hosts):
            gn = (state["radio_clock"].get(serial) or {}).get("gnss") or {}
            # with no fix the receiver still reports a time (2019-04-07 on a cold
            # start), which is not GPS time, so only a fix is compared
            tutc = gn.get("time_utc") if _g40_clock.has_fix(gn) else None
            sk = _skew_vs_console(tutc) if tutc else None
            if sk is not None and abs(sk) > CLOCK_SKEW_WARN_S:
                gps_warn.append(f"{serial} GPS time is {sk:+.1f} s from this console; "
                                "the console is the reference for keying, check it")
        with dialog_host:
            for line in done:
                audit.add("clock", f"{what}: {line}")
            for line in failed:
                audit.add("clock", f"clock sync failed: {line}")
            for line in gps_warn:
                audit.add("clock", f"clock check: {line}")
                ui.notify(line, type="warning", multi_line=True, close_button=True)
            _refresh_audit()
            if done and not failed:
                ui.notify("radio clocks synced: " + "; ".join(done), type="positive")
            elif failed:
                ui.notify("clock sync: " + "; ".join(done + failed),
                          type="warning", multi_line=True, close_button=True)
            else:
                ui.notify("no connected radios to sync", type="info")

    def _tl_clock():
        return mf.is_clock_anchored(mission())

    def _tl_total():
        # clock-anchored plans always show the full 24 h day; otherwise the
        # timeline spans the steps.
        return mf.DAY_S if _tl_clock() else timeline_span()

    def _tl_label(t):
        return mf.clock_hm(t) if _tl_clock() else _fmt_time(t)

    def _tl_label_s(t):
        # to the second, for lists a step is read from (a 30 s step at HH:MM reads 01:01-01:01)
        return mf.clock_hms(t) if _tl_clock() else _fmt_time(t)

    def _now_spm():
        # current time as seconds past midnight of the anchor date, or None
        import time as _t
        mid = mf.anchor_midnight_epoch(mission())
        if mid is None:
            return None
        spm = _t.time() - mid
        return spm if 0 <= spm <= mf.DAY_S else None

    def _visible(total=None):
        # seconds shown across the timeline's width: the zoom, never more than
        # the whole span (then the span just fills the width, no scroll)
        total = max(_tl_total(), 1.0) if total is None else total
        return max(min(MIN_VIEW_S, total), min(state["view_s"], total))

    def _timed_bounds():
        timed = [s for s in mission()["steps"] if not mf.is_location_task(s)]
        if not timed:
            return None
        return (min(mf.step_start_s(s) for s in timed),
                max(mf.step_stop_s(s) for s in timed))

    def _home_t():
        # where the view opens: T+0 on a relative plan whose first step is in
        # its first stretch; otherwise (and always on the UTC day) a little
        # before the first step, or before now on an empty UTC plan
        total = max(_tl_total(), 1.0)
        vis = _visible(total)
        b = _timed_bounds()
        if b is not None:
            t = b[0]
        elif _tl_clock():
            t = _now_spm() or 0.0
        else:
            t = 0.0
        if not _tl_clock() and t < vis * 0.75:
            return 0.0
        t = math.floor(max(0.0, t - 60.0) / WINDOW_ROUND_S) * WINDOW_ROUND_S
        return max(0.0, min(t, total - vis))

    def _set_view(view_s, scroll_cmd=None):
        # zoom to view_s seconds across the width. By default the time at the
        # centre of the view stays put. The scroll has to be recomputed from
        # the client's view width, so it is handed to _refresh_timeline as a
        # command and run from inside the rebuild (the button that fired this
        # is about to be cleared, so a run_javascript out here would hit a
        # deleted slot).
        total = max(_tl_total(), 1.0)
        old = _visible(total)
        state["view_s"] = max(MIN_VIEW_S, min(total, float(view_s)))
        state["scroll_cmd"] = scroll_cmd or ("anchor", old, _visible(total), total)
        _refresh_timeline()

    def _zoom(factor):
        _set_view(_visible() * factor)

    def _whole_day():
        # the whole span across the width, no horizontal scroll
        _set_view(_tl_total(), ("abs", 0.0))

    def _hour_view():
        # back to the default one-hour scale, at the start of the plan
        state["view_s"] = VIEW_S
        state["homed"] = False
        _refresh_timeline()

    def _fit_steps():
        # frame every timed step: from T+0 on a relative plan (the run starts
        # there), from just before the first step on the UTC day
        b = _timed_bounds()
        if b is None:
            _hour_view()
            return
        lo = 0.0 if not _tl_clock() else max(0.0, b[0] - 60.0)
        span = max(MIN_VIEW_S, (b[1] - lo) * 1.05)
        _set_view(span, ("time", lo, min(span, max(_tl_total(), 1.0))))

    def _pos(t, total):
        # left offset for a time, as a percent of the track (the track is as
        # wide as total / visible viewports, so this holds at any zoom)
        return f"{(t / total * 100.0) if total > 0 else 0:.4f}%"

    def _span(dur, total):
        # width for a duration, matching _pos
        return f"{(dur / total * 100.0) if total > 0 else 0:.4f}%"

    def _run_elapsed():
        # plan seconds into the current run, for the position line; a
        # clock-anchored plan has the wall-clock marker instead
        return None if _tl_clock() else runner.elapsed_s()

    def _move_run_marker():
        # the run position line across the lanes and its time on the ruler,
        # moved every second while a plan runs (no timeline rebuild needed);
        # hidden when nothing runs
        el = _run_elapsed()
        total = max(_tl_total(), 1.0)
        shown = el is not None and 0 <= el <= total
        left = _pos(el if shown else 0.0, total)
        disp = "block" if shown else "none"
        for m in state.get("run_markers", []):
            m.style(f"left:{left};display:{disp}")
        lab = state.get("run_ruler")
        if lab is not None:
            lab.set_text(_tl_label(float(int(el))) if shown else "")
            lab.style(f"left:{left};display:{disp}")

    def _refresh_timeline():
        _refresh_map()
        state["run_markers"] = []
        state["run_ruler"] = None
        timeline_box.clear()
        total = max(_tl_total(), 1.0)
        vis = _visible(total)
        if not state.get("homed"):
            # first draw, a plan just loaded or the timeline kind changed:
            # open on the one-hour view where the plan is
            state["homed"] = True
            state["scroll_cmd"] = ("time", _home_t(), vis)
        # the ruler and the lanes share one scroll viewport. "inner" is the
        # whole span, as many viewports wide as the span holds visible windows
        # (an hour by default), so a longer plan or the UTC day scrolls instead
        # of squeezing. Everything inside positions off the inner's left edge
        # (time 0) in percent, so cursor-to-time needs no gutter maths.
        inner_w = "100%" if vis >= total else f"{total / vis * 100.0:.4f}%"
        with timeline_box:
            with ui.row().classes("items-center gap-1"):
                ui.button(icon="zoom_out", on_click=lambda: _zoom(1.6)) \
                    .props("dense flat").tooltip("show more time")
                ui.button(icon="zoom_in", on_click=lambda: _zoom(1 / 1.6)) \
                    .props("dense flat").tooltip("show less time, wider steps")
                ui.label((f"{vis / 3600:.3g} h" if vis >= 3600 else f"{vis / 60:.3g} min"
                          if vis >= 60 else f"{vis:.3g} s") + " across").classes("text-xs font-mono text-gray-500") \
                    .tooltip("how much time the width of the timeline shows")
                ui.button("1 h", on_click=lambda: _hour_view()).props("dense flat") \
                    .tooltip("back to the one-hour view at the start of the plan")
                if _tl_clock():
                    ui.button("show whole day", on_click=lambda: _whole_day()).props("dense flat").tooltip("show the full 24 h")
                ui.button("fit all steps", on_click=lambda: _fit_steps()).props("dense flat").tooltip("zoom the timeline to every step")
                ui.button(icon="chevron_left", on_click=lambda: _pan(-0.5)).props("dense flat").tooltip("pan left")
                ui.button(icon="chevron_right", on_click=lambda: _pan(0.5)).props("dense flat").tooltip("pan right")
                ui.button("to selected", icon="my_location", on_click=lambda: _jump_selected()).props("dense flat").tooltip("scroll to the selected step")
                ui.label("UTC" if _tl_clock() else "T+ elapsed from Run mission (m:ss)").classes(
                    "text-xs text-gray-500 ml-2")
                _b = _timed_bounds()
                if _b is not None and _b[1] > vis and not _tl_clock():
                    ui.label(f"plan runs to {_fmt_time(_b[1])}").classes(
                        "text-xs text-amber-700 ml-2").tooltip(
                        "steps reach past the view: scroll, or fit all steps")
                ui.label("wheel to zoom, shift-wheel to pan, drag a box or "
                         "shift-click to select several").classes(
                    "text-xs text-gray-400 ml-2")
                nsel = len(state.get("sel", set()))
                if nsel > 1:
                    ui.label(f"{nsel} selected").classes("mp-selcount ml-2")
                    ui.button("clear", on_click=lambda: _clear_selection()) \
                        .props("dense flat no-caps size=sm").tooltip("Esc")
                ui.label("").props("id=tl-cursor").classes("mp-cursor-read ml-auto")
            _bench()
            # each lane (and the ruler) gets its own horizontal scroller, so a
            # scrollbar sits under every radio. They are kept in lock-step by the
            # JS below, so the lanes stay time-aligned. Wheel zooms about the
            # cursor, shift-wheel (or a trackpad sideways swipe) pans.
            wheel_js = ("(e)=>{const el=e.currentTarget;"
                        "if(e.shiftKey||Math.abs(e.deltaX)>Math.abs(e.deltaY)){"
                        "e.preventDefault();el.scrollLeft+=(e.deltaX||e.deltaY);return;}"
                        "e.preventDefault();const r=el.getBoundingClientRect();"
                        "emit(e.deltaY,e.clientX-r.left,el.scrollLeft,el.clientWidth);}")

            def _hscroll():
                sv = ui.element("div").classes("mission-hs").style(
                    "width:100%;overflow-x:auto;overflow-y:hidden")
                sv.on("wheel", lambda e: _on_wheel(e), throttle=0.04,
                      js_handler=wheel_js)
                return sv

            with _hscroll():
                with ui.element("div").style(
                        f"position:relative;width:{inner_w};min-width:{inner_w}"):
                    _ruler(total)
            for rid in lane_radios():
                with _hscroll():
                    with ui.element("div").style(
                            f"position:relative;width:{inner_w};min-width:{inner_w}"):
                        _lane(rid, total)
            _location_strip()
            # keep every lane's horizontal scroll in lock-step, and hold the
            # scroll position across a rebuild (a rebuild happens on every
            # select and every zoom, so without this the timeline would snap
            # back to 00:00). window._missionScroll holds the wanted offset;
            # user scrolls write it, and it is re-applied here.
            #
            # Two things make this fiddly. clear()/rebuild re-renders the lanes
            # on Vue's next tick, and NiceGUI can run this script before that
            # tick lands, so the offset is re-applied for several frames: as soon
            # as the new lanes appear their scrollLeft is set and onscroll bound.
            # And on a zoom the lane gets wider, so for a frame or two the new
            # width is not laid out yet; setting scrollLeft then gets clamped to
            # the old (smaller) max, which fires onscroll. So a short hold window
            # follows every rebuild during which onscroll ignores events: that
            # stops a clamp (or the script's own set) from overwriting
            # the wanted offset and dragging the view back toward 00:00. The
            # loop keeps re-applying until the width settles and the set sticks.
            # a zoom or a fit-mode switch hands in a new wanted offset (pre).
            # "abs" sets it outright; "anchor" recomputes it from the view width
            # so the centre time holds across the zoom. This runs here, inside
            # the rebuild's slot, because the control that triggered it (a zoom
            # button, the wheel over a lane) has just been cleared.
            pre = ""
            cmd = state.get("scroll_cmd")
            state["scroll_cmd"] = None
            if cmd and cmd[0] == "abs":
                pre = f"window._missionScroll={float(cmd[1]):.1f};"
            elif cmd and cmd[0] == "time":
                # put this time at the left edge; px per s is the viewport
                # width over the visible seconds
                _, t0, vis_s = cmd
                pre = ("const _b=document.querySelectorAll('.mission-hs');"
                       "if(_b.length){const _cw=_b[0].clientWidth;"
                       f"window._missionScroll=Math.max(0,{float(t0):.3f}*_cw/{vis_s:.3f});}}")
            elif cmd and cmd[0] == "anchor":
                _, old_vis, new_vis, span = cmd
                pre = ("const _b=document.querySelectorAll('.mission-hs');"
                       "if(_b.length){const _cw=_b[0].clientWidth;"
                       f"const _op=_cw/{old_vis:.3f},_np=_cw/{new_vis:.3f};"
                       "const _ct=((window._missionScroll||0)+_cw/2)/_op;"
                       f"let _ns=_ct*_np-_cw/2;const _mx={span:.3f}*_np-_cw;"
                       "if(_ns>_mx)_ns=_mx;if(_ns<0)_ns=0;window._missionScroll=_ns;}")
            ui.run_javascript(
                "(()=>{" + pre + "window._missionHold=performance.now()+300;let n=0;"
                "const tick=()=>{"
                "const bars=[...document.querySelectorAll('.mission-hs')];"
                "if(bars.length){const x0=window._missionScroll||0;"
                "bars.forEach(b=>{if(Math.abs(b.scrollLeft-x0)>1)b.scrollLeft=x0;});"
                "bars.forEach(b=>{b.onscroll=()=>{"
                "if(performance.now()<(window._missionHold||0))return;"
                "const x=b.scrollLeft;window._missionScroll=x;"
                "bars.forEach(o=>{if(o!==b&&Math.abs(o.scrollLeft-x)>1)o.scrollLeft=x;});};});}"
                "if(n++<9)requestAnimationFrame(tick);};"
                "tick();requestAnimationFrame(tick);})();")

    def _tl_tick(total):
        # about eight ticks across the visible width (10 min on the hour view,
        # 3 h on the whole day). The total/800 floor caps how many labels the
        # ruler draws when zoomed right in on a long span, so a deep zoom
        # cannot spawn thousands of DOM nodes.
        return _nice_tick(max(_visible(total) / 8.0, total / 800.0))

    def _ruler(total):
        # nice ticks at _tl_tick's spacing, labelled with the plan time
        tick = _tl_tick(total)
        with ui.element("div").style(
                "position:relative;height:18px;width:100%;"
                "border-bottom:1px solid var(--border)"):
            t = 0.0
            while t <= total + 0.1:
                # keep the first and last labels inside the track instead of
                # centring them, so neither spills past the edge
                if t <= 0.1:
                    shift = "translateX(0)"
                elif t >= total - tick * 0.5:
                    shift = "translateX(-100%)"
                else:
                    shift = "translateX(-50%)"
                ui.label(_tl_label(t)).style(
                    f"position:absolute;left:{_pos(t, total)};"
                    f"font-size:10px;color:var(--muted);font-family:monospace;transform:{shift}")
                t += tick
            # where the run is now, in plan time; _move_run_marker keeps it
            # current and hides it when nothing runs
            state["run_ruler"] = ui.label("").style(
                "position:absolute;left:0;top:0;font-size:10px;font-weight:600;"
                "color:var(--accent);background:var(--bg);padding:0 2px;transform:translateX(-50%);"
                "display:none")

    def _location_strip():
        """Location tasks have no time: they key from their areas. Shown as
        chips under the lanes; click one to edit it."""
        tasks = [s for s in mission()["steps"] if mf.is_location_task(s)]
        if not tasks:
            return
        ui.label("location tasks (keyed by place, not time; armed under the map)") \
            .classes("text-xs mt-1 text-gray-600").style(
            "position:sticky;left:0;width:fit-content")
        waiting_ids = {w[0].get("id") for w in runner._waiting.values()}
        # one lane per radio, like the timed steps, so with two SDRs in the plan
        # it is plain which radio each task keys on. Plan order for the radios;
        # a task on a radio that is not in the plan gets its own lane at the end.
        plan_rids = [r.get("id") for r in mission().get("radios", [])]
        by_radio = {}
        for s in tasks:
            by_radio.setdefault(s.get("radio"), []).append(s)
        order = [r for r in plan_rids if r in by_radio] + \
                [r for r in by_radio if r not in plan_rids]
        for rid in order:
            _lane_title(rid, "  location tasks")
            rcol = _radio_color(rid)
            strip = ui.element("div").classes("mp-lane").style(
                f"position:relative;min-height:{LANE_H}px;width:100%;display:flex;"
                "flex-wrap:wrap;gap:6px;padding:7px 6px;box-sizing:border-box;"
                f"border-left:5px solid {rcol};"
                + (f"background-color:{rcol}14;" if _is_selected_radio(rid) else ""))
            strip.on("click", lambda e, r=rid: _select_radio(r),
                     js_handler="(e)=>{if(e.target===e.currentTarget) emit();}")
            with strip:
                for s in by_radio[rid]:
                    sid = s.get("id")
                    active = sid in runner.active_steps
                    bad = any(c.level == "error" and c.step_id == sid for c in state["conflicts"])
                    primary = state["selected"] == sid
                    in_sel = sid in state.get("sel", set())
                    status = ("KEYED" if active else "waiting for the radio" if sid in waiting_ids
                              else "held until the platform leaves" if sess.triggers.is_held(sid)
                              else "")
                    rule = ("automatic" if mf.trigger_block(s).get("confirm") == "auto"
                            else "confirmed once" if sid in sess.confirmed_once else "needs confirm")
                    ks = _kind_style(s.get("technique", ""))
                    border, shadow = _block_edge(ks, primary, in_sel, bad)
                    if active:      # keyed now: an amber glow, the fill stays its kind
                        shadow = ", ".join(x for x in (shadow, f"0 0 10px {_theme.ACCENT}") if x)
                    blk = ui.element("div").classes(
                        "mp-step" + ("" if _is_online(rid) else " mp-step-offline")).style(
                        f"position:relative;width:300px;height:{LANE_H - 14}px;background:{ks['fill']};"
                        f"color:{ks['text']};border:{border};font-size:11px;overflow:hidden;"
                        f"padding:3px 6px 3px 12px;cursor:pointer;box-shadow:{shadow or 'none'}")
                    blk.on("click", lambda e, sid=sid: _select(sid))
                    with blk:
                        _radio_tag(rid)
                        if bad:
                            ui.label("!").classes("mp-step-badge")
                        ui.label(f"{s['technique']}{_step_detail(s)}"
                                 + (f"  {status}" if status else "")).style(
                            "pointer-events:none;white-space:nowrap;font-weight:600")
                        ui.label(f"on {rid}  ({rule})").style(
                            "pointer-events:none;opacity:0.85;font-size:10px")

    def _lane_title(rid, suffix=""):
        """The radio's name over its lane: its colour square, the link dot,
        the name (bold when selected); click selects the radio."""
        connected = _is_online(rid)
        dot = "●" if connected else "○"
        picked = _is_selected_radio(rid)
        row = ui.row().classes("items-center gap-1 mt-1 no-wrap cursor-pointer").style(
            "position:sticky;left:0;width:fit-content")
        row.on("click", lambda e, rid=rid: _select_radio(rid))
        with row:
            ui.element("div").style(
                f"width:10px;height:10px;border-radius:2px;background:{_radio_color(rid)};"
                "pointer-events:none")
            ui.label(f"{dot} {rid}{suffix}" + ("" if connected else "  (offline)")).classes(
                "text-xs font-mono "
                + ("text-emerald-700" if connected else "text-gray-400")
                + (" font-bold" if picked else "")).style("pointer-events:none")
            if picked:
                ui.label("selected").classes("text-[10px] text-gray-600").style(
                    "pointer-events:none")
        return row

    def _lane(rid, total):
        # the radio name sits above its lane and sticks to the left edge, so it
        # stays readable while the lane is scrolled. A planned radio that is not
        # online says so; its steps are skipped if it is still offline at Run mission.
        _lane_title(rid)
        # light grid lines at the ruler's tick spacing
        tick = _tl_tick(total)
        period = _span(tick, total)
        rcol = _radio_color(rid)
        lane = ui.element("div").classes("mp-lane").props(
            f'data-total="{total:.1f}" data-clock="{1 if _tl_clock() else 0}" '
            f'data-rid="{rid}"').style(
            f"position:relative;height:{LANE_H}px;width:100%;"
            f"border-left:5px solid {rcol};"
            + (f"background-color:{rcol}14;" if _is_selected_radio(rid) else "")
            + f"background-image:repeating-linear-gradient(to right,var(--grid) 0 1px,"
            f"transparent 1px {period})")
        lane.on("dragover.prevent", lambda e: None)
        # a click on the empty lane (not on a block) selects the radio; the
        # click that ends a marquee box does not
        lane.on("click", lambda e, r=rid: _select_radio(r),
                js_handler="(e)=>{if(e.target===e.currentTarget&&"
                "performance.now()>(window._mpMarqUntil||0)) emit();}")
        # emit the drop x within the lane (measured from the lane's left edge, so
        # it is right even when scrolled) and the lane's full pixel width, so the
        # time maps the same way whether the lane is fitted or pixel-zoomed.
        # args is left as None so both emitted values reach the handler.
        lane.on("drop", lambda e, r=rid: _on_lane_drop(e, r),
                js_handler="(e)=>{e.preventDefault();const el=e.currentTarget;"
                "const r=el.getBoundingClientRect();"
                "emit(e.clientX - r.left, el.clientWidth);}")
        with lane:
            # the current-time marker, when the anchor date is today
            now = _now_spm()
            if now is not None:
                ui.element("div").style(
                    f"position:absolute;left:{_pos(now, total)};top:0;"
                    f"width:2px;height:{LANE_H}px;background:{_theme.TIMELINE_NOW};opacity:0.8")
            # the run position line (relative plans): drawn in every lane,
            # shown and moved by _move_run_marker while a plan runs
            state.setdefault("run_markers", []).append(ui.element("div").style(
                f"position:absolute;left:0;top:0;width:2px;height:{LANE_H}px;"
                "background:var(--accent);opacity:0.9;display:none;pointer-events:none;z-index:2"))
            for step in mission()["steps"]:
                if step.get("radio") == rid and not mf.is_location_task(step):
                    _step_block(step, total)

    def _on_wheel(e):
        a = getattr(e, "args", None) or []
        if len(a) < 4:
            return
        try:
            dy, vx, sl, cw = float(a[0]), float(a[1]), float(a[2]), float(a[3])
        except (TypeError, ValueError):
            return
        if cw <= 0:
            return
        total = max(_tl_total(), 1.0)
        vis = _visible(total)
        cur_px = cw / vis
        # the time under the cursor, kept fixed across the zoom
        t_cursor = max(0.0, min(total, (vx + sl) / cur_px))
        new_vis = max(MIN_VIEW_S, min(total, vis * (1.25 if dy > 0 else 1.0 / 1.25)))
        if abs(new_vis - vis) < 1e-6:
            return
        state["view_s"] = new_vis
        new_px = cw / new_vis
        new_scroll = t_cursor * new_px - vx
        max_scroll = max(0.0, total * new_px - cw)
        new_scroll = max(0.0, min(max_scroll, new_scroll))
        # hand the post-zoom offset to the rebuild so its loop lands the lanes
        # on the cursor; emitting it out here would hit the just-cleared lane.
        state["scroll_cmd"] = ("abs", new_scroll)
        _refresh_timeline()

    def _radio_tag(rid):
        """The radio's colour as a band down the left of a block. The fill
        stays the catalog group colour, so the two do not fight: the group
        says what the step does, the band says whose radio it is."""
        ui.element("div").style(
            f"position:absolute;left:0;top:0;bottom:0;width:6px;"
            f"background:{_radio_color(rid)};pointer-events:none")

    def _block_edge(ks, primary, in_sel, bad):
        """A block's border and outer rings (theme palette): the editor's step
        gets a light edge and an amber ring, other selected steps an amber
        edge, a step in conflict a red edge and a faint red ring."""
        multi = in_sel and len(state.get("sel", set())) > 1
        border = (f"2px solid {_theme.TIMELINE_PRIMARY}" if primary else
                  f"2px solid {_theme.TIMELINE_CONFLICT}" if bad else
                  f"2px solid {_theme.TIMELINE_SELECTED}" if multi else
                  f"1px solid {ks['border']}")
        rings = []
        if primary:
            rings.append(f"0 0 0 1px {_theme.TIMELINE_SELECTED}")
        if bad:
            rings.append(f"0 0 0 {4 if primary else 3}px {_theme.TIMELINE_CONFLICT_RING}")
        return border, ", ".join(rings)

    def _step_block(step, total):
        sid = step.get("id")
        start = mf.step_start_s(step)
        stop = mf.step_stop_s(step)
        bad = bool(conflicts_for(sid))
        in_sel = sid in state.get("sel", set())
        primary = state["selected"] == sid
        ks = _kind_style(step.get("technique", ""))
        border, shadow = _block_edge(ks, primary, in_sel, bad)
        if step.get("technique") == "set_rate":
            # the time the re-init takes, drawn so nothing is placed inside it
            need = mf.rate_change_block_s((_plan_radio(step.get("radio")) or {}).get("type"))
            ui.element("div").style(
                f"position:absolute;left:{_pos(start, total)};top:7px;"
                f"width:{_span(need, total)};height:{LANE_H - 14}px;border-radius:6px;"
                "background:repeating-linear-gradient(45deg,color-mix(in srgb,var(--muted) 33%,transparent),color-mix(in srgb,var(--muted) 33%,transparent) 6px,transparent 6px,"
                "transparent 12px);border:1px dashed var(--faint);pointer-events:none").tooltip(
                f"re-init, about {need:g} s: keep steps out of this time")
        # the step's true extent as a thin bar along the lane floor: the block
        # above it is held to a readable minimum width, so a short step's block
        # can run past its stop; this bar never does
        ui.element("div").style(
            f"position:absolute;left:{_pos(start, total)};bottom:2px;"
            f"width:{_span(stop - start, total)};min-width:2px;height:3px;"
            f"background:{ks['border']};pointer-events:none")
        entry = catalog.get(step.get("technique", ""))
        name = (entry.label if entry else "") or step.get("technique", "?")
        detail = _step_detail(step)
        times = f"{_tl_label(start)}-{_tl_label(stop)}"
        dur = _fmt_dur(round(stop - start, 1))
        tip = "\n".join(x for x in (
            ("! " if bad else "") + name,
            f"{step.get('technique', '?')}{detail}",
            f"{times}  ({dur})  on {step.get('radio')}",
            *[c.message for c in conflicts_for(sid)][:3]) if x)
        blk = ui.element("div").classes(
            "mp-step" + (" mp-step-primary" if primary else "")
            + ("" if _is_online(step.get("radio")) else " mp-step-offline")).style(
            f"position:absolute;left:{_pos(start, total)};top:7px;"
            f"width:{_span(stop - start, total)};min-width:{STEP_MIN_PX}px;"
            f"height:{LANE_H - 16}px;background:{ks['fill']};color:{ks['text']};"
            f"border:{border};font-size:11px;overflow:hidden;"
            f"padding:3px {22 if bad else 6}px 3px 12px;cursor:pointer;"
            f"box-shadow:{shadow or 'none'}").props("draggable=true")
        blk.on("click",
               lambda e, s=sid: _select(s, _additive(e)),
               js_handler="(e)=>emit(e.ctrlKey||e.metaKey||e.shiftKey)")
        blk.on("dragstart", lambda e, s=sid: _begin_drag(s))
        with blk:
            _radio_tag(step.get("radio"))
            if bad:
                ui.label("!").classes("mp-step-badge")
            ui.label(name).classes("mp-step-line").style(
                "font-weight:600")
            ui.label(detail.strip(" ()") or step.get("technique", "")).classes(
                "mp-step-line").style("opacity:0.85;font-size:10px")
            ui.label(f"{times} · {dur}").classes("mp-step-line").style(
                "opacity:0.85;font-size:10px;font-family:monospace")
            ui.tooltip(tip).classes("whitespace-pre-line text-xs")

    # planning timeline: a time-less holding lane above the radio lanes.
    # Items keep a duration only; the drop point on a radio lane sets the start.
    # Dragging into a lane copies, so the item stays staged for reuse.

    def _staged():
        return mission().setdefault("staged", [])

    def _stage_item(technique, params=None, dur_s=None, radiates=None):
        entry = catalog.get(technique)
        if entry is None:
            ui.notify(f"unknown technique {technique}", type="negative")
            return
        import copy as _copy
        _staged().append({
            "id": "stg-" + uuid.uuid4().hex[:6], "technique": technique,
            "label": entry.label, "fresh": params is None,
            "params": _copy.deepcopy(params) if params is not None else entry.fresh_params(),
            "radiates": entry.radiates if radiates is None else bool(radiates),
            "dur_s": round(float(dur_s if dur_s is not None else entry.default_dur), 1)})
        audit.add("edit", f"staged {technique} on the planning timeline")
        sess.autosave()
        _refresh_timeline()

    def _stage_step(sid):
        st = _find_step(sid)
        if st is None or mf.is_location_task(st):
            return
        _stage_item(st["technique"], st.get("params") or {},
                    mf.step_stop_s(st) - mf.step_start_s(st), st.get("radiates"))

    def _unstage(stg_id):
        mission()["staged"] = [x for x in _staged() if x.get("id") != stg_id]
        sess.autosave()
        _refresh_timeline()

    def _place_staged(stg_id, radio_id, start_s):
        item = next((x for x in _staged() if x.get("id") == stg_id), None)
        if item is None:
            return
        import copy as _copy
        if item.get("fresh"):
            # nothing filled in yet: the normal add path asks for the file
            add_step(item["technique"], radio_id, start_s)
            sid = state.get("selected")
            st = _find_step(sid)
            if st is not None:
                st["schedule"]["stop_s"] = round(st["schedule"]["start_s"] + item["dur_s"], 1)
                _revalidate()
                _refresh_all()
            return
        ensure_radio(radio_id)
        start_s = round(float(start_s), 1)
        sid = _new_step_id()
        mission()["steps"].append({
            "id": sid, "radio": radio_id, "technique": item["technique"],
            "radiates": item.get("radiates", False),
            "params": _copy.deepcopy(item.get("params") or {}),
            "schedule": {"start_s": start_s, "stop_s": round(start_s + item["dur_s"], 1)}})
        state["selected"] = sid
        state["sel"] = {sid}
        audit.add("edit", f"placed staged {item['technique']} on {radio_id} at {_fmt_time(start_s)}")
        _revalidate()
        _refresh_all()

    def _on_bench_drop(_e):
        drag = state.get("drag")
        state["drag"] = None
        if not drag:
            return
        if drag["type"] == "new":
            _stage_item(drag["technique"])
        elif drag["type"] == "move":
            _stage_step(drag["id"])
        elif drag["type"] == "move_group":
            for sid in drag.get("ids", []):
                _stage_step(sid)

    def _bench():
        with ui.element("div").classes("mp-bench w-full"):
            with ui.row().classes("items-center gap-2 w-full no-wrap"):
                ui.label("PLANNING").classes("mp-bench-title")
                ui.label("staging, no times. Drag a catalog item or a step here to keep it; "
                         "drag a chip onto an RX/TX lane to place a copy there.") \
                    .classes("text-xs text-gray-500")
            zone = ui.element("div").classes("mp-bench-zone")
            zone.on("dragover.prevent", lambda e: None)
            zone.on("drop", _on_bench_drop, js_handler="(e)=>{e.preventDefault();emit();}")
            with zone:
                if not _staged():
                    ui.label("drop signals here").classes("mp-bench-empty")
                for item in _staged():
                    chip = ui.element("div").classes("mp-bench-chip").props("draggable=true") \
                        .style(f"background:{_kind_style(item['technique'])['fill']};"
                               f"color:{_kind_style(item['technique'])['text']}")
                    chip.on("dragstart", lambda e, i=item["id"]: state.update(
                        drag={"type": "staged", "id": i}))
                    with chip:
                        ui.label(item.get("label") or item["technique"]).classes("mp-bench-name")
                        ui.label(_fmt_dur(item["dur_s"])).classes("mp-bench-dur")
                        ui.button(icon="close", on_click=lambda _e, i=item["id"]: _unstage(i)) \
                            .props("dense flat round size=xs").classes("mp-bench-x") \
                            .tooltip("remove from planning")

    def _pan(frac):
        ui.run_javascript(
            "(()=>{const b=document.querySelector('.mission-hs');if(!b)return;"
            f"b.scrollLeft+=b.clientWidth*{frac};}})()")

    def _jump_selected():
        st = _find_step(state.get("selected")) if state.get("selected") else None
        if st is None:
            ui.notify("select a step first", type="info")
            return
        vis = _visible()
        ui.run_javascript(
            "(()=>{const b=document.querySelector('.mission-hs');if(!b)return;"
            f"b.scrollLeft=Math.max(0,{mf.step_start_s(st):.1f}*b.clientWidth/{vis:.3f}"
            "-b.clientWidth/3);})()")

    def _on_lane_drop(e, radio_id):
        x = 0.0
        lane_w = 0.0
        a = getattr(e, "args", None)
        if isinstance(a, dict):
            x = float(a.get("offsetX", 0) or 0)
        elif isinstance(a, (list, tuple)) and a:
            x = float(a[0] or 0)
            if len(a) > 1:
                lane_w = float(a[1] or 0)
        total = max(_tl_total(), 1.0)
        if lane_w > 0:
            # the drop sits at this fraction along the lane, so at this time
            start_s = max(0.0, min(total, x / lane_w * total))
        else:
            start_s = 0.0
        drag = state.get("drag")
        state["drag"] = None
        if not drag:
            return
        if drag["type"] == "new":
            add_step(drag["technique"], radio_id, start_s)
        elif drag["type"] == "staged":
            _place_staged(drag["id"], radio_id, start_s)
        elif drag["type"] == "move":
            # moving to another lane changes the radio too
            sid = drag["id"]
            for s in mission()["steps"]:
                if s.get("id") == sid:
                    s["radio"] = radio_id
                    ensure_radio(radio_id)
            move_step(sid, start_s)
        elif drag["type"] == "move_group":
            # drop the whole selection: the grabbed chip lands at the drop time,
            # the others keep their offsets from it, and all move to this lane.
            ids = drag.get("ids", [])
            by_id = {s.get("id"): s for s in mission()["steps"]}
            anchor = by_id.get(drag.get("anchor"))
            if anchor is None:
                return
            delta = start_s - mf.step_start_s(anchor)
            ensure_radio(radio_id)
            for sid in ids:
                s = by_id.get(sid)
                if s is None:
                    continue
                dur = mf.step_stop_s(s) - mf.step_start_s(s)
                ns = max(0.0, round(mf.step_start_s(s) + delta, 1))
                s["schedule"] = {"start_s": ns, "stop_s": round(ns + dur, 1)}
                s["radio"] = radio_id
            audit.add("edit", f"moved {len(ids)} steps to {radio_id} "
                              f"(anchor {_fmt_time(start_s)})")
            _revalidate()
            _refresh_all()

    def _additive(e):
        """True when the click came with shift, ctrl or cmd held. The emitted value
        arrives as a bare bool for a single emit, or wrapped in a list; handle
        both so ctrl-click never throws."""
        a = getattr(e, "args", None)
        if isinstance(a, (list, tuple)):
            return bool(a[0]) if a else False
        return bool(a)

    def _select(sid, additive=False):
        """Click selects just this step. Shift/Ctrl/Cmd-click toggles it in the
        multi-selection (so several can be dragged together and edited at
        once). The primary is the
        one the editor shows."""
        sel = state.setdefault("sel", set())
        if additive:
            if sid in sel:
                sel.discard(sid)
                if state["selected"] == sid:
                    state["selected"] = next(iter(sel), None)
            else:
                sel.add(sid)
                state["selected"] = sid
        else:
            if sid in sel and len(sel) > 1:
                # a plain click on a chip that is already part of a
                # multi-selection keeps the group (so pressing it to drag does
                # not collapse the selection first); it becomes primary.
                # Clear the group by clicking an unselected chip or empty lane.
                state["selected"] = sid
            else:
                sel.clear()
                sel.add(sid)
                state["selected"] = sid
        # the step's radio becomes the selected radio, so the map follows what
        # is being worked on; the notify redraws every page, this one included
        step = _find_step(state["selected"]) if state["selected"] else None
        if step is not None and sess.select_radio(step.get("radio")):
            sess._notify()
        else:
            _refresh_timeline()
        _refresh_editor()
        # jump to the editor tab so a click always shows what it selected
        if state["selected"] is not None:
            try:
                right_tabs.set_value(tab_editor)
            except Exception:
                pass

    def _begin_drag(sid):
        """A chip that is part of a multi-selection drags the whole selection
        (relative times preserved); otherwise it drags alone."""
        sel = state.get("sel", set())
        if sid in sel and len(sel) > 1:
            state["drag"] = {"type": "move_group", "ids": list(sel), "anchor": sid}
        else:
            state["drag"] = {"type": "move", "id": sid}

    def _select_many(ids, additive=False):
        """Select a set of steps at once (the marquee box). additive adds them
        to the current selection; otherwise they replace it."""
        sel = state.setdefault("sel", set())
        ids = set(ids)
        if not additive:
            sel.clear()
        sel |= ids
        if state["selected"] not in sel:
            first = min((s for s in mission()["steps"] if s.get("id") in sel),
                        key=mf.step_start_s, default=None)
            state["selected"] = first.get("id") if first else None
        _refresh_timeline()
        _refresh_editor()
        if sel:
            try:
                right_tabs.set_value(tab_editor)
            except Exception:
                pass

    def _clear_selection():
        if not state.get("sel") and state.get("selected") is None:
            return
        state["sel"] = set()
        state["selected"] = None
        _refresh_timeline()
        _refresh_editor()

    def _on_marquee(e):
        # a box dragged across the lanes: every timed step on a crossed lane
        # that overlaps the box's time range
        a = getattr(e, "args", None) or {}
        hits = set()
        for h in a.get("lanes") or []:
            try:
                rid, t0, t1 = h.get("rid"), float(h.get("t0")), float(h.get("t1"))
            except (TypeError, ValueError, AttributeError):
                continue
            for s in mission()["steps"]:
                if (s.get("radio") == rid and not mf.is_location_task(s)
                        and mf.step_start_s(s) < t1 and mf.step_stop_s(s) > t0):
                    hits.add(s.get("id"))
        if hits or not a.get("add"):
            _select_many(hits, additive=bool(a.get("add")))

    ui.on("mp_marquee", _on_marquee)

    def _bulk_editor():
        """The editor for several selected steps: the settings they share.
        A field shows the common value, or "mixed" where the steps differ.
        Only the fields changed here are written, each to every selected step
        that has that setting; the rest of each step is left alone."""
        ids = state.get("sel", set())
        steps = [s for s in mission()["steps"] if s.get("id") in ids]
        n = len(steps)
        timed = [s for s in steps if not mf.is_location_task(s)]
        ui.label("Edit selected steps").classes("text-sm font-bold")
        ui.label(f"{n} steps selected").classes("text-xs font-mono text-amber-700")
        ui.label("Change a field, then Apply: it is written to every selected step "
                 "that has that setting. Untouched fields, and fields left on "
                 "mixed, stay as they are on each step.").classes(
            "text-xs text-gray-500 whitespace-normal")
        with ui.row().classes("gap-1 w-full"):
            for s in sorted(steps, key=mf.step_start_s):
                ui.button(f"{s.get('technique')} · {s.get('radio')}",
                          on_click=lambda _e, i=s.get("id"): _select(i)) \
                    .props("dense flat no-caps size=sm").classes("text-[10px]") \
                    .tooltip("edit this step alone")

        touched = {}          # key: reader returning the new value
        apply_btn = None

        def _row(label, tip):
            r = ui.row().classes("items-center gap-1 w-full")
            with r:
                ui.label(label).classes(
                    "text-xs w-32 shrink-0 whitespace-normal leading-tight").tooltip(tip or "")
            return r

        def _note_count(cnt):
            if cnt < n:
                ui.label(f"{cnt} of {n}").classes("text-[10px] text-gray-500") \
                    .tooltip(f"only {cnt} of the selected steps have this setting")

        def _touch(key, reader, widgets):
            def _h(_=None):
                touched[key] = reader
                for w in widgets:
                    w.classes("mp-dirty")
                if apply_btn is not None:
                    apply_btn.props(remove="color=primary")
                    apply_btn.props("color=warning")
                    apply_btn.set_text(f"Apply {len(touched)} change(s) to selected")
            for w in widgets:
                w.on_value_change(_h)

        if timed:
            _section("Timing")
            prios = {mf.step_priority(s) for s in timed}
            with _row("priority (1 high, 9 low)", "when a location task wants the radio, "
                      "the higher priority wins"):
                pr = ui.number(value=prios.pop() if len(prios) == 1 else None,
                               placeholder="mixed", min=1, max=9, step=1, format="%d") \
                    .props("dense outlined").classes("w-28")
                _note_count(len(timed))
            _touch("__priority__", lambda: pr.value, [pr])
            durs = {round(mf.step_stop_s(s) - mf.step_start_s(s), 3) for s in timed}
            with _row("duration", "each step keeps its start; its stop moves. "
                      "Type m:ss (2:30), 90s, 5m or 1.5h"):
                dn = ui.input(value=_fmt_time(next(iter(durs))) if len(durs) == 1 else "",
                              placeholder="mixed").props("dense outlined").classes("w-28")
                ui.label("m:ss, 90s, 5m").classes("text-[10px] text-gray-500")
                _note_count(len(timed))

            def _read_dur():
                if not (dn.value or "").strip():
                    return None
                v = parse_duration(dn.value)
                if v is None:
                    raise ValueError("duration must be m:ss, h:mm:ss, or like 90s, 5m")
                return v
            _touch("__duration__", _read_dur, [dn])

        shared = bulk_fields(steps)
        section_done = set()
        _RF = {"center_hz", "tx_center_hz", "tx_atten_db", "rx_gain_index"}
        shared = [x for x in shared if x[0].key in _RF] + \
                 [x for x in shared if x[0].key not in _RF]
        # a setting only one selected step has is better edited on that step;
        # it is kept, folded away, so the shared ones stand out
        common_f = [x for x in shared if len(x[1]) > 1 or n == 1]
        lone_f = [x for x in shared if not (len(x[1]) > 1 or n == 1)]
        lone_box = None
        for i, (f, sids, common) in enumerate(common_f + lone_f):
            if i == len(common_f):
                lone_box = ui.expansion(f"settings only one step has ({len(lone_f)})") \
                    .classes("w-full text-xs")
            if lone_box is None:
                sec = "RF" if f.key in _RF else "Options"
                if sec not in section_done:
                    section_done.add(sec)
                    _section(sec)
            mixed = common is MIXED
            with (lone_box if lone_box is not None else ui.element("div").classes("w-full")), \
                    _row(f.label, f.help):
                if f.kind == "hz":
                    nv, nu = (None, "MHz") if mixed else _best_hz_unit(
                        common, "MHz" if f.optional else "kHz")
                    num = ui.number(value=None if nv is None else round(nv, 6), format="%g",
                                    placeholder="mixed" if mixed else (f.blank if f.optional else "")) \
                        .props("dense outlined").classes("grow").style("min-width:7rem")
                    unit = ui.select(list(_HZ_UNITS), value=nu).props("dense outlined") \
                        .classes("w-20 shrink-0")

                    def _read(num=num, unit=unit, f=f):
                        if num.value in (None, ""):
                            if f.optional:
                                return None
                            raise ValueError(f"{f.label} is empty")
                        return float(num.value) * _HZ_UNITS[unit.value]
                    ws = [num, unit]
                elif f.kind == "num":
                    num = ui.number(value=None if mixed else common, format="%g",
                                    min=f.min, max=f.max, step=f.step,
                                    placeholder="mixed" if mixed else "") \
                        .props("dense outlined").classes("w-28")

                    def _read(num=num, f=f):
                        if num.value in (None, ""):
                            if f.optional:
                                return None
                            raise ValueError(f"{f.label} is empty")
                        v = float(num.value)
                        return int(v) if f.step == 1 else v
                    ws = [num]
                elif f.kind in ("select", "bool"):
                    if f.kind == "bool":
                        opts = {True: "on", False: "off"}
                        key = None if mixed else bool(common)
                    else:
                        opts = {(_BLANK if v is None else v): lab for v, lab in f.choices}
                        key = None if mixed else (_BLANK if common is None else common)
                        if key is not None and key not in opts:
                            opts[key] = f"{common} (from file)"
                    if mixed:
                        opts = {"__mixed__": "mixed", **opts}
                        key = "__mixed__"
                    sel = ui.select(opts, value=key).props("dense outlined").classes("w-40")

                    def _read(sel=sel):
                        if sel.value == "__mixed__":
                            raise LookupError
                        return None if sel.value == _BLANK else sel.value
                    ws = [sel]
                else:
                    tx = ui.input(value="" if mixed or common is None else str(common),
                                  placeholder="mixed" if mixed else "") \
                        .props("dense outlined").classes("w-48")

                    def _read(tx=tx):
                        return tx.value or ""
                    ws = [tx]
                _note_count(len(sids))
            _touch(f.key, _read, ws)

        if not shared and not timed:
            ui.label("these steps share no settings").classes("text-xs text-gray-500")

        def _apply(_=None):
            params, prio, dur = {}, None, None
            try:
                for k, rd in touched.items():
                    try:
                        v = rd()
                    except LookupError:          # left on (or put back to) mixed
                        continue
                    if k == "__priority__":
                        prio = None if v in (None, "") else int(v)
                    elif k == "__duration__":
                        dur = v
                    else:
                        params[k] = v
            except (TypeError, ValueError) as ex:
                ui.notify(str(ex), type="negative")
                return
            if dur is not None and dur <= 0:
                ui.notify("duration must be above zero", type="negative")
                return
            if not params and prio is None and dur is None:
                ui.notify("nothing changed", type="info")
                return
            cnt = apply_bulk(steps, params, prio, dur)
            what = [f"{k}={v}" for k, v in params.items()]
            if prio is not None:
                what.append(f"priority={prio}")
            if dur is not None:
                what.append(f"duration={_fmt_dur(dur)}")
            audit.add("edit", f"bulk edit of {cnt} step(s): " + ", ".join(what))
            _revalidate(); _refresh_conflicts(); _refresh_timeline()
            _refresh_audit(); _refresh_editor()
            ui.notify(f"applied to {cnt} step(s)", type="positive")

        ui.label("").classes("min-h-[0.5rem]")
        with ui.element("div").classes("w-full").style(
                "position:sticky;bottom:0;background:var(--surface,#fff);padding:6px 0 2px;"
                "border-top:1px solid var(--border);z-index:1"):
            with ui.row().classes("gap-2"):
                apply_btn = ui.button(f"Apply to {n} selected", on_click=_apply) \
                    .props("dense color=primary").tooltip(
                    "write the changed (amber) fields into every selected step that has them")
                ui.button("Clear selection", on_click=_clear_selection) \
                    .props("dense flat no-caps").tooltip("Esc")
                ui.button(f"Delete {n}", icon="delete", on_click=_delete_selected) \
                    .props("dense color=negative outline")

    def _refresh_editor():
        editor_box.clear()
        if len(state.get("sel", set())) > 1:
            with editor_box:
                _bulk_editor()
            return
        sid = state["selected"]
        step = next((s for s in mission()["steps"] if s.get("id") == sid), None)
        rtype = ((_plan_radio(step.get("radio")) or {}).get("type")) if step else None
        with editor_box:
            if step is None:
                ui.label("Step editor").classes("text-sm font-bold")
                ui.label("select a step to edit it").classes(
                    "text-xs text-gray-500")
                return
            ui.label("Step editor").classes("text-sm font-bold")
            ui.label(f"{step['technique']}  on  {step['radio']}").classes(
                "text-xs font-mono")
            _section("Timing" if not mf.is_location_task(step) else "Trigger")
            nsel = len(state.get("sel", set()))
            if nsel > 1:
                ui.label(f"{nsel} steps selected: drag any one on the timeline "
                         "to move them together").classes(
                    "text-xs text-amber-700")

            # start and stop. In clock mode they are HH:MM(:SS) UTC times; in
            # relative mode a number plus a time-unit dropdown. The schedule is
            # always stored in seconds either way.
            clock = _tl_clock()
            start0 = mf.step_start_s(step)
            stop0 = mf.step_stop_s(step)
            loc_task = mf.is_location_task(step)
            prio_in = None
            if loc_task:
                trig_read, time_fields = _trigger_editor(step)

                def _read_start():
                    return None

                def _read_stop():
                    return None

                def _write_stop(sec):
                    pass
            else:
                # start and stop read as the timeline does (T+ m:ss, or UTC
                # HH:MM:SS on the clock); the duration is typed as m:ss, 90s,
                # 5m and so on, and keeps stop in step with it
                if clock:
                    fmt_t, parse_t, unit_txt = mf.clock_hms, mf.parse_clock, "UTC"
                    t_ph, t_tip = "HH:MM:SS", "UTC time of day, HH:MM or HH:MM:SS"
                else:
                    fmt_t, parse_t, unit_txt = _fmt_time, parse_duration, "T+"
                    t_ph = "m:ss"
                    t_tip = ("time from Run mission: m:ss or h:mm:ss (12:30), "
                             "or 90s, 5m, 1h 10m")
                d_tip = "how long the step runs: m:ss (2:30), 90s, 5m, 1.5h"
                with ui.row().classes("items-center gap-1 no-wrap"):
                    ui.label("start").classes("w-14 text-xs")
                    st = ui.input(value=fmt_t(start0), placeholder=t_ph).props(
                        "dense outlined").classes("w-28").tooltip(t_tip)
                    ui.label(unit_txt).classes("text-xs text-gray-500")
                with ui.row().classes("items-center gap-1 no-wrap"):
                    ui.label("duration").classes("w-14 text-xs")
                    du = ui.input(value=_fmt_time(stop0 - start0), placeholder="m:ss").props(
                        "dense outlined").classes("w-28").tooltip(d_tip)
                    ui.label("m:ss, 90s, 5m").classes("text-xs text-gray-500")
                with ui.row().classes("items-center gap-1 no-wrap"):
                    ui.label("stop").classes("w-14 text-xs")
                    sp = ui.input(value=fmt_t(stop0), placeholder=t_ph).props(
                        "dense outlined").classes("w-28").tooltip(t_tip)
                    ui.label(unit_txt).classes("text-xs text-gray-500")
                time_fields = (st, du, sp)
                _sync = {"busy": False}

                def _sync_times(e, src):
                    # duration and stop follow each other; a new start moves the
                    # stop and keeps the duration
                    if _sync["busy"]:
                        return
                    a0, d0, b0 = parse_t(st.value), parse_duration(du.value), parse_t(sp.value)
                    _sync["busy"] = True
                    try:
                        if src in ("start", "dur") and a0 is not None and d0 is not None:
                            sp.value = fmt_t(a0 + d0)
                        elif src == "stop" and a0 is not None and b0 is not None and b0 > a0:
                            du.value = _fmt_time(b0 - a0)
                    finally:
                        _sync["busy"] = False
                st.on("blur", lambda e: _sync_times(e, "start"))
                du.on("blur", lambda e: _sync_times(e, "dur"))
                sp.on("blur", lambda e: _sync_times(e, "stop"))
                for _w, _src in ((st, "start"), (du, "dur"), (sp, "stop")):
                    _w.on("keydown.enter", lambda e, s_=_src: _sync_times(e, s_))

                def _read_start():
                    return parse_t(st.value)

                def _read_stop():
                    a0, d0 = parse_t(st.value), parse_duration(du.value)
                    b0 = parse_t(sp.value)
                    # the duration wins when both were typed and disagree,
                    # unless only the stop was changed
                    if a0 is not None and d0 is not None and \
                            (b0 is None or du.value != _fmt_time(stop0 - start0)):
                        return a0 + d0
                    return b0

                def _write_stop(sec):
                    sp.value = fmt_t(sec)
                    a0 = parse_t(st.value)
                    if a0 is not None:
                        du.value = _fmt_time(sec - a0)

            if not loc_task:
                prio_in = ui.number("priority (1 high, 9 low)", value=mf.step_priority(step),
                                    min=1, max=9, step=1, format="%d") \
                    .props("dense outlined").classes("w-44").tooltip(
                    "when a location task wants this radio, the higher priority wins")
                time_fields = time_fields + (prio_in,)

            # radiating is a property of the technique (catalog), not a choice
            _rad_entry = catalog.get(step["technique"])
            rad = ui.checkbox("radiates", value=bool(_rad_entry.radiates if _rad_entry
                                                     else step.get("radiates"))) \
                .props("disable").tooltip("set by the step type")

            # which SMA jacks this step uses on its radio. Blank = whatever the
            # radio is on. Ports are init parameters on the Epiq cards, so a
            # step on other ports re-initializes the radio before it starts.
            # RX none is for a step that only transmits: the runner keeps the
            # radio's RX if it can stay, else parks it on a harmless port.
            rxport_in = txport_in = None
            _rxo, _txo = ports.rx_options(rtype), ports.tx_options(rtype)
            if _rxo or _txo:
                _srx, _stx = ports.step_ports(step)
                _rx_none = (ports.rx_none_ok(rtype)
                            and not catalog.receives(step["technique"]))
                _rxc = {"": "radio's current", **{p: p for p in _rxo}}
                if _rx_none or _srx == ports.RX_NONE:
                    _rxc[ports.RX_NONE] = "none (TX only)"
                _section("Ports")
                with ui.row().classes("items-center gap-2"):
                    if _rxo:
                        rxport_in = ui.select(_rxc, value=_srx or "", label="RX port") \
                            .props("dense outlined").classes("w-44").tooltip(
                            "none: this step only transmits; the runner keeps the "
                            "radio's RX where it can stay, else moves it to a "
                            "harmless port (G40: the other card; Check plan says which)")
                    if _txo:
                        txport_in = ui.select({"": "radio's current", **{p: p for p in _txo}},
                                              value=_stx or "", label="TX port") \
                            .props("dense outlined").classes("w-44")
                # grey out what the radio's port map locks, the tooltip names the chain.
                # The stored choice is left alone; Check plan flags a locked one.
                _ph = mgr.hosts.get(step.get("radio"))

                def _draw_ports():
                    _v = _ppu.port_view(_ph, getattr(_ph, "config", None), on_ready=_draw_ports)
                    for _w, _sd in ((rxport_in, "rx"), (txport_in, "tx")):
                        if _w is not None:
                            _ppu.apply(_w, _v, _sd, skip=("", ports.RX_NONE), move=False)
                if _ph is not None:
                    _draw_ports()
                ui.label("other ports than the radio is on re-initialize it before this "
                         f"step (about {ports.REINIT_S.get(str(rtype or '').lower(), 30)} s, "
                         "RX stops); Check plan warns where that happens") \
                    .classes("text-xs text-gray-500")

            if step["technique"] == "iq_playback":
                pth = _playback_path(step.get("params") or {})
                src_txt = ("scenario" if (step.get("params") or {}).get("source")
                           == "scenario" else "IQ file")
                with ui.row().classes("items-center gap-2 w-full no-wrap mt-1"):
                    ui.label(f"{src_txt}: {os.path.basename(pth)}" if pth
                             else "no file chosen").classes(
                        "text-xs font-mono truncate "
                        + ("" if pth else "text-red-600 font-bold")).tooltip(pth)
                    ui.button("Choose IQ file", icon="folder_open",
                              on_click=lambda: _pick_playback_file(sid)).props(
                        "dense outline no-caps")

            if step["technique"] in ("scenario_json", "scenario_decode"):
                p0 = step.get("params") or {}
                src0 = ss.step_spec_source(p0)
                lab0 = ss.step_spec_label(p0)
                head = {"console": f"console file: {lab0} (kept in the plan)",
                        "radio": f"radio file: {lab0}"}.get(src0, "no scenario JSON chosen")
                with ui.row().classes("items-center gap-2 w-full no-wrap mt-1"):
                    ui.label(head).classes(
                        "text-xs font-mono truncate "
                        + ("" if src0 else "text-red-600 font-bold")).tooltip(
                        p0.get("spec_path") or lab0 or "")
                    ui.button("Choose scenario JSON", icon="folder_open",
                              on_click=lambda: _pick_scenario(sid)).props(
                        "dense outline no-caps")
                with ui.row().classes("items-center gap-2 w-full no-wrap"):
                    state["scen_note_lbl"][sid] = ui.label(
                        state["scen_note"].get(sid, "")).classes(
                        "text-xs text-gray-600 whitespace-normal grow")
                    if src0:
                        ui.button("Check on radio", on_click=lambda: _check_scenario(sid)) \
                            .props("dense flat no-caps").tooltip(
                            "ask the radio whether this scenario fits at its rate "
                            "(check only, nothing is built or keyed)")
                if src0 and sid not in state["scen_note"]:
                    background_tasks.create(_check_scenario(sid),
                                            name="mission-scenario-check")

            if step["technique"] == "replay":
                rname = (step.get("params") or {}).get("name") or ""
                with ui.row().classes("items-center gap-2 w-full no-wrap mt-1"):
                    ui.label(f"recording: {rname}" if rname else "no recording chosen") \
                        .classes("text-xs font-mono truncate "
                                 + ("" if rname else "text-red-600 font-bold"))
                    ui.button("Choose recording", icon="folder_open",
                              on_click=lambda: _pick_recording(sid)).props(
                        "dense outline no-caps")

            # every option the technique has, as the panels offer it (catalog
            # fields). The playback file and replay recording are chosen with
            # their buttons above, not typed here.
            entry = catalog.get(step["technique"])
            cur = dict(step.get("params") or {})
            picked = {"iq_playback": ("source", "path"), "replay": ("name",),
                      "scenario_json": ("source", "spec_path"),
                      "scenario_decode": ("source", "spec_path")}.get(
                step["technique"], ())
            fields = [f for f in (entry.fields if entry else []) if f.key not in picked]
            readers = {}          # key: function returning the field's value
            rows = []             # (field, row element) for show_if
            form_widgets = []
            if entry is not None and entry.hint:
                ui.label(entry.hint).classes("text-xs text-gray-600")
            _RF_KEYS_EDIT = {"center_hz", "tx_center_hz", "tx_atten_db", "rx_gain_index"}
            rf_fields = [f for f in fields if f.key in _RF_KEYS_EDIT]
            fields = rf_fields + [f for f in fields if f.key not in _RF_KEYS_EDIT]
            section_done = set()
            for f in fields:
                sec = "RF" if f.key in _RF_KEYS_EDIT else "Options"
                if sec not in section_done:
                    section_done.add(sec)
                    _section(sec)
                val = cur.get(f.key, f.default)
                tip = f.help or ""
                # the row wraps when the column is narrow, so a wide field (an
                # hz number plus its unit) drops the unit to the next line
                # instead of squeezing the number box to a sliver
                with ui.row().classes("items-center gap-1 w-full") as row:
                    ui.label(f.label).classes(
                        "text-xs w-32 shrink-0 whitespace-normal leading-tight") \
                        .tooltip(tip)
                    if f.kind == "hz":
                        nv, nu = _best_hz_unit(val, "MHz" if f.optional else "kHz")
                        # grow to fill the row, but never below 7rem so the value
                        # (or the "keep radio's" / "same as RX" placeholder) shows
                        num = ui.number(value=None if nv is None else round(nv, 6),
                                        format="%g",
                                        placeholder=f.blank if f.optional else "") \
                            .props("dense outlined").classes("grow") \
                            .style("min-width:7rem")
                        unit = ui.select(list(_HZ_UNITS), value=nu) \
                            .props("dense outlined").classes("w-20 shrink-0")
                        last_u = {"u": nu}

                        def _rescale(e, num=num, last_u=last_u):
                            # keep the frequency, change only how it is shown
                            if num.value not in (None, ""):
                                num.value = round(float(num.value)
                                                  * _HZ_UNITS[last_u["u"]]
                                                  / _HZ_UNITS[e.value], 9)
                            last_u["u"] = e.value
                        unit.on_value_change(_rescale)
                        if f.key == "sample_rate":
                            # the rate as RF bandwidth, live, the same rule as
                            # the main app (0.8 of the rate)
                            bw_lbl = ui.label("").classes("text-xs text-gray-500 w-full")

                            def _bw(num=num, unit=unit, lbl=bw_lbl):
                                try:
                                    hz = float(num.value) * _HZ_UNITS[unit.value]
                                    lbl.set_text("RF bandwidth about "
                                                 + bw_text(rf_bw_hz(hz, rtype)))
                                except (TypeError, ValueError, KeyError):
                                    lbl.set_text("")
                            num.on_value_change(lambda e, fn=_bw: fn())
                            unit.on_value_change(lambda e, fn=_bw: fn())
                            _bw()

                        def _read(num=num, unit=unit, f=f):
                            if num.value in (None, ""):
                                if f.optional:
                                    return None
                                raise ValueError(f"{f.label} is empty")
                            return float(num.value) * _HZ_UNITS[unit.value]
                        form_widgets += [num, unit]
                    elif f.kind == "num":
                        num = ui.number(value=val, format="%g", min=f.min, max=f.max,
                                        step=f.step).props("dense outlined") \
                            .classes("w-28")

                        def _read(num=num, f=f):
                            if num.value in (None, ""):
                                if f.optional:
                                    return None
                                raise ValueError(f"{f.label} is empty")
                            v = float(num.value)
                            return int(v) if f.step == 1 else v
                        form_widgets.append(num)
                    elif f.kind == "select":
                        opts = {(_BLANK if v is None else v): lab for v, lab in f.choices}
                        key = _BLANK if val is None else val
                        if key not in opts:      # a value from an older or hand-made file
                            opts[key] = f"{val} (from file)"
                        sel = ui.select(opts, value=key).props("dense outlined") \
                            .classes("w-40")

                        def _read(sel=sel):
                            return None if sel.value == _BLANK else sel.value
                        form_widgets.append(sel)
                    elif f.kind == "bool":
                        cb = ui.checkbox(value=bool(val))

                        def _read(cb=cb):
                            return bool(cb.value)
                        form_widgets.append(cb)
                    elif f.kind == "json":
                        ta = ui.textarea(value=json.dumps(val or {}, indent=1)) \
                            .props("dense outlined autogrow").classes(
                            "w-full font-mono text-xs")

                        def _read(ta=ta, f=f):
                            try:
                                v = json.loads(ta.value or "{}")
                            except Exception as ex:
                                raise ValueError(f"{f.label}: bad JSON ({ex})")
                            if not isinstance(v, dict):
                                raise ValueError(f"{f.label} must be a JSON object")
                            return v
                        form_widgets.append(ta)
                    else:                        # text
                        tx = ui.input(value="" if val is None else str(val)) \
                            .props("dense outlined").classes("w-48")

                        def _read(tx=tx):
                            return tx.value or ""
                        form_widgets.append(tx)
                readers[f.key] = _read
                rows.append((f, row))

            def _form_values(strict=True):
                out = {}
                for k, rd in readers.items():
                    try:
                        out[k] = rd()
                    except ValueError:
                        if strict:
                            raise
                return out

            def _show_rows():
                vals = {**cur, **_form_values(strict=False)}
                for f, row in rows:
                    row.set_visibility(f.shown(vals))

            loc_read = _location_editor(step, sid, form_widgets,
                                        roles=("start",) if loc_task else ("start", "stop"))

            with ui.expansion("params as JSON (read only)").classes("w-full text-xs"):
                pj = ui.code(json.dumps(_params_view(cur), indent=2), language="json") \
                    .classes("w-full text-xs")

            def _preview():
                try:
                    merged = {**dict(step.get("params") or {}), **_form_values()}
                    pj.set_content(json.dumps(_params_view(merged), indent=2))
                except ValueError as ex:
                    pj.set_content(f"// {ex}")
            _show_rows()

            # reserve the note's line height so showing "unsaved changes" does
            # not push the Apply button down (a shifted button means a click
            # where Apply was lands nowhere, and the edit looks like it did not
            # take)
            dirty_note = ui.label("").classes("text-xs min-h-[1.1rem]")
            # the buttons stay in view at the bottom of a long editor
            with ui.element("div").classes("w-full").style(
                    "position:sticky;bottom:0;background:var(--surface,#fff);padding:6px 0 2px;"
                    "border-top:1px solid var(--border);z-index:1"):
                with ui.row().classes("gap-2"):
                    apply_btn = ui.button("Apply changes").props("dense color=primary").tooltip("write the amber fields into the plan")
                    ui.button("Delete step", icon="delete",
                              on_click=lambda: delete_step(sid)).props(
                        "dense color=negative outline")

            # pending-edit state: edits are not committed to the step until Apply,
            # and the edited fields plus the Apply button change colour until then.
            _dirty_fields = time_fields + tuple(w for w in form_widgets
                                                if not isinstance(w, ui.checkbox))

            def _mark_dirty(e):
                w = getattr(e, "sender", None)
                if w is not None and not isinstance(w, ui.checkbox):
                    w.classes("mp-dirty")
                apply_btn.props(remove="color=primary")
                apply_btn.props("color=warning")
                dirty_note.set_text("unsaved changes, click Apply")
                dirty_note.classes(
                    replace="text-xs text-amber-700 font-bold min-h-[1.1rem]")

            def _mark_clean():
                for w in _dirty_fields:
                    w.classes(remove="mp-dirty")
                apply_btn.props(remove="color=warning")
                apply_btn.props("color=primary")
                dirty_note.set_text("")
                dirty_note.classes(replace="text-xs min-h-[1.1rem]")

            def _apply(_=None):
                try:
                    vals = _form_values()
                    loc = loc_read()
                except ValueError as ex:
                    ui.notify(str(ex), type="negative")
                    return
                if loc_task:
                    try:
                        trig, prio = trig_read()
                    except ValueError as ex:
                        ui.notify(str(ex), type="negative")
                        return
                    step["trigger"] = trig
                    step["priority"] = prio
                    step.pop("schedule", None)
                    start_s = stop_s = 0.0
                else:
                    start_s = _read_start()
                    stop_s = _read_stop()
                    if start_s is None or stop_s is None:
                        ui.notify("times must be HH:MM or HH:MM:SS" if clock else
                                  "times must be m:ss, h:mm:ss, or like 90s, 5m",
                                  type="negative")
                        return
                    if stop_s <= start_s:
                        ui.notify("the step must stop after it starts", type="negative")
                        return
                    step["schedule"] = {"start_s": round(start_s, 3),
                                        "stop_s": round(stop_s, 3)}
                    try:
                        step["priority"] = max(1, min(9, int(prio_in.value or mf.DEFAULT_PRIORITY)))
                    except (TypeError, ValueError):
                        step["priority"] = mf.DEFAULT_PRIORITY
                step["radiates"] = bool(rad.value)
                if rxport_in is not None or txport_in is not None:
                    _orx, _otx = ports.step_ports(step)
                    ports.set_step_ports(
                        step,
                        (rxport_in.value or None) if rxport_in is not None else _orx,
                        (txport_in.value or None) if txport_in is not None else _otx)
                # keep the fixed keys (the generator kind) and whatever the
                # file and recording pickers wrote; the form owns the rest
                step["params"] = {**dict(step.get("params") or {}), **vals}
                if loc:
                    step["location"] = loc
                else:
                    step.pop("location", None)
                audit.add("edit", f"applied {sid}: " + ("location task" if loc_task else
                                                       f"{_tl_label_s(start_s)}-{_tl_label_s(stop_s)}"))
                _revalidate(); _refresh_conflicts(); _refresh_timeline()
                _refresh_audit()
                _mark_clean()
                ui.notify("changes applied", type="positive")
            apply_btn.on_click(_apply)

            for w in time_fields + (rad,):
                w.on_value_change(_mark_dirty)
            for w in (rxport_in, txport_in):
                if w is not None:
                    w.on_value_change(_mark_dirty)

            def _on_form(e):
                _mark_dirty(e)
                _show_rows()
                _preview()
            for w in form_widgets:
                w.on_value_change(_on_form)

    def _refresh_audit():
        # rewrite the whole log cheaply (bounded)
        audit_box.clear()
        for line in audit.lines()[-200:]:
            audit_box.push(line)

    def _refresh_stop_steps():
        """One Stop this step button for each keyed step, redrawn only when that
        list changes, so a button is not replaced under a click."""
        keyed = sorted(runner.active_steps)
        if keyed == state.get("stop_steps_drawn"):
            return
        state["stop_steps_drawn"] = keyed
        stop_steps_row.clear()
        with stop_steps_row:
            for sid in keyed:
                ui.button(f"Stop this step: {sid}", icon="stop",
                          on_click=lambda s=sid: _do_stop_step(s)) \
                    .props("color=amber-10 outline dense no-caps").tooltip(
                    "stop this step only; the run and the triggers carry on. A location "
                    "task stays off until the platform leaves its area")

    def _refresh_plan_changed():
        """A run keeps the steps and times it started with, so say what differs
        from the plan on screen, on every edit path including Open file."""
        ch = run_changes(runner.started_steps, mission()) if runner.is_running() else {}
        bits = [f"{what}: {', '.join(ch[key])}." for key, what in (
            ("removed", "Removed, but still in this run"),
            ("added", "Added, not in this run"),
            ("moved", "Moved, but keeps its old time"),
            ("settings", "Settings changed, used if it has not started yet")) if ch.get(key)]
        if bits:
            plan_changed_lbl.set_text("The plan changed during this run. " + " ".join(bits)
                                      + " To change the plan, press STOP ALL TX, then load it and run again.")
        plan_changed_lbl.set_visibility(bool(bits))

    def _refresh_run_state():
        _refresh_stop_steps()
        _refresh_plan_changed()
        if runner.is_running():
            active = ", ".join(sorted(runner.active_steps)) or "(between steps)"
            el = _run_elapsed()
            total = runner.run_stop_s if runner.run_stop_s is not None else _plan_stop_s()
            at = (f" at {_tl_label(float(int(el)))} of {_tl_label(total)}"
                  if el is not None else "")
            busy = runner.init_status()
            # the timeline has begun, on a relative or a UTC plan; el is None on a UTC plan
            began = runner.elapsed_s() is not None
            keyed = (f"keyed = {', '.join(sorted(runner.active_steps))}"
                     if runner.active_steps else "nothing keyed yet")
            if busy and not began:
                run_state_lbl.set_text(f"STARTING: radio coming up, about 50 s each, {keyed} ({busy})")
            elif busy:
                run_state_lbl.set_text(f"RUNNING{at}: radio re-initializing, about 50 s ({busy})")
            elif not began:
                run_state_lbl.set_text(f"STARTING: getting the radios ready, {keyed}")
            else:
                run_state_lbl.set_text(f"RUNNING{at}: keyed = {active}"
                                       + (f"; NOT RUNNING {runner.run_error}" if runner.run_error else ""))
            run_state_lbl.classes(replace="mp-pill bg-red-600 text-white")
        elif runner.run_error:
            run_state_lbl.set_text(f"RUN FAILED {runner.run_error}")
            run_state_lbl.classes(replace="mp-pill bg-red-800 text-white")
        elif runner.active_steps:
            waiting = [w[0].get("id") for w in runner._waiting.values()]
            run_state_lbl.set_text("KEYED by location trigger: "
                                   + ", ".join(sorted(runner.active_steps))
                                   + (f"; waiting for the radio: {', '.join(waiting)}"
                                      if waiting else ""))
            run_state_lbl.classes(replace="mp-pill bg-red-600 text-white")
        else:
            run_state_lbl.set_text("idle")
            run_state_lbl.classes(replace="mp-pill bg-gray-200 text-gray-700")

    # top-bar actions

    def _do_validate():
        _revalidate()
        _refresh_conflicts()
        _refresh_timeline()
        n_err = sum(1 for c in state["conflicts"] if c.level == "error")
        if n_err:
            ui.notify(f"{n_err} conflict(s) to fix before running", type="negative")
        else:
            ui.notify("plan validates", type="positive")

    def _do_save():
        mission()["name"] = name_in.value or "untitled mission"
        text = mf.dumps(mission())
        fname = (mission()["name"].replace(" ", "_") or "mission") + ".dpmg-mission.json"
        audit.add("edit", f"saved plan '{mission()['name']}'")
        _refresh_audit()
        ui.download(text.encode("utf-8"), fname)
        ui.notify(f"saved {fname}", type="positive")

    async def _import_cfe(fname, text):
        """A CFE autoSchedule.csv or role/service XML: steps go on a radio of
        the plan, appended or replacing that radio's steps."""
        low = fname.lower()
        try:
            if low.endswith(".csv"):
                parsed = mi.parse_autoschedule(text)
                what = f"{len(parsed)} autoSchedule row(s)"
            else:
                parsed = mi.parse_cfe_xml(text)
                what = f"{len(parsed)} CFE service(s)"
        except Exception as ex:
            ui.notify(f"could not read {fname}: {ex}", type="negative")
            return
        radios = lane_radios()
        if not radios:
            ui.notify("add or connect a radio first; the imported steps need one", type="warning")
            return
        with dialog_host:
            with ui.dialog() as dlg, ui.card().classes("w-[30rem] gap-1"):
                ui.label(f"Import {fname}").classes("text-base font-bold")
                ui.label(what + ", one step after another").classes("text-xs text-gray-500")
                radio_in = ui.select(radios, value=radios[0], label="onto radio") \
                    .props("dense outlined").classes("w-full")
                how_in = ui.select({"append": "append after that radio's last step",
                                    "replace": "replace that radio's steps"}, value="append",
                                   label="how").props("dense outlined").classes("w-full")
                with ui.row().classes("justify-end w-full gap-2"):
                    ui.button("Cancel", on_click=lambda: dlg.submit(False)).props("flat")
                    ui.button("Import", on_click=lambda: dlg.submit(True)).props("color=primary")
        if not await dlg:
            return
        rid = radio_in.value
        ensure_radio(rid)
        r = _plan_radio(rid) or {}
        rtype = r.get("type")
        replace = how_in.value == "replace"
        start = 0.0 if replace else max((mf.step_stop_s(s) for s in _steps_on(rid)
                                         if not mf.is_location_task(s)), default=0.0)
        rate = None
        if low.endswith(".csv"):
            steps, notes = mi.steps_from_autoschedule(parsed, rid, rtype, start_s=start,
                                                      new_id=_new_step_id)
        else:
            steps, notes, rate = mi.steps_from_cfe_xml(parsed, rid, rtype, start_s=start,
                                                       new_id=_new_step_id)
        with dialog_host:
            n = mi.import_into(mission(), steps, rid, replace=replace, start_rate=rate)
            for note in notes:
                audit.add("edit", f"import {fname}: {note}")
            audit.add("edit", f"imported {len(steps)} step(s) from {fname} onto {rid} "
                              f"({'replaced' if replace else 'appended'}); {n} step(s) on it now")
            _revalidate(); _refresh_all(); _refresh_audit()
            ui.notify(f"imported {len(steps)} step(s) from {fname}", type="positive")

    async def _on_upload(e):
        fname = getattr(e.file, "name", "") or ""
        text = await e.file.text()
        try:
            up.reset()                           # the picker takes the next file too
        except Exception:
            pass
        if fname.lower().endswith((".csv", ".xml")):
            await _import_cfe(fname, text)
            return
        try:
            # NiceGUI 3 gives the upload as e.file (2.x had e.content)
            data = json.loads(text)
            loaded = mf.loads(data)
        except Exception as ex:
            ui.notify(f"load failed: {ex}", type="negative")
            return
        sess.mission = loaded
        state["selected"] = None
        state["sel"] = set()
        state["view_s"] = VIEW_S
        state["homed"] = False
        audit.add("edit", f"loaded plan '{loaded.get('name', '?')}'")
        _revalidate()
        _refresh_all()
        _refresh_audit()
        ui.notify(f"loaded '{loaded.get('name', '?')}'", type="positive")
        await _dial_planned()
    up.on_upload(_on_upload)

    async def _dial_planned():
        """Connect every planned radio that has an address and is not online.
        Without this a reopened or loaded plan shows those radios offline (and
        Run mission refuses to run) until each one's link button is pressed."""
        for r in list(mission().get("radios", [])):
            if r.get("address") and not _is_online(r.get("id")):
                await _connect_radio(r["id"])

    async def _do_run():
        if runner.is_running():
            ui.notify("a mission is already running: radios may still be coming up, see the banner",
                      type="warning")
            return
        _revalidate()
        _refresh_conflicts()
        _refresh_timeline()
        if has_errors():
            ui.notify("fix the conflicts before running", type="negative")
            return
        radiating = [s for s in mission()["steps"] if s.get("radiates")
                     and not mf.is_location_task(s)]
        from .mission_runner import step_commands
        # only steps the run actually drives (location tasks key from their areas)
        runnable = []
        for s in mission()["steps"]:
            if mf.is_location_task(s):
                continue
            sc, _ = step_commands(s)
            if sc is not None:
                runnable.append(s)
        n_tasks = sum(1 for s in mission()["steps"] if mf.is_location_task(s))
        # every radio the run would drive, and which of them are online now; a
        # planned radio that is offline is run without (its steps are skipped)
        run_radios = []
        for s in runnable:
            if s.get("radio") not in run_radios:
                run_radios.append(s.get("radio"))
        offline = [rid for rid in run_radios if not _is_online(rid)]
        live_radiating = [s for s in radiating if s.get("radio") not in offline]
        with ui.dialog() as dlg, ui.card().classes("w-[40rem] gap-1"):
            ui.label("Confirm Run mission: this will RADIATE").classes(
                "text-lg font-bold text-red-600")
            ui.label("Radios").classes("text-sm font-bold")
            with ui.column().classes("gap-0"):
                for rid in run_radios:
                    text, ok = _radio_status(rid)
                    n = len([s for s in runnable if s.get("radio") == rid])
                    if rid in offline:
                        line, cls = (f"{rid}: offline, its {n} step(s) will be "
                                     f"skipped", "text-amber-700")
                    elif (getattr(getattr(mgr.hosts.get(rid), "status", None),
                                  "radio_state", None) == 0 and _plan_radio(rid)):
                        # not initialized: the runner brings it up from the plan
                        how = bring_up_plan(_plan_radio(rid), plan_steps_on(mission(), rid),
                                            pm=_live_port_maps().get(rid))
                        if how.get("err"):
                            line, cls = (f"{rid}: not set up and cannot bring it up: "
                                         f"{how['err']}; its {n} step(s) will fail",
                                         "text-red-600")
                        else:
                            line, cls = (f"{rid}: not set up. Initialized first (about "
                                         f"{ports.REINIT_S.get(str(_plan_radio(rid).get('type') or '').lower(), 30)}"
                                         f" s): {how['text']}. {n} step(s)", "text-amber-700")
                            if _plan_radio(rid).get("clock_source") == "external":
                                line += ". External needs 10 MHz and 1 PPS connected"
                    elif not ok:
                        line, cls = (f"{rid}: {text}; its {n} step(s) may fail",
                                     "text-amber-700")
                    else:
                        line, cls = f"{rid}: {text}, {n} step(s)", "text-emerald-700"
                    ui.label(line).classes(f"text-xs font-mono {cls}")
                    if rid not in offline and _tx_on(rid):
                        ui.label(f"{rid}: TX is on now; Run mission stops it before the "
                                 f"plan starts").classes("text-xs font-mono text-amber-700")
                    # RX none does not stop a stream already running
                    h_rs = getattr(getattr(mgr.hosts.get(rid), "status", None), "radio_state", None)
                    if h_rs == 2 and all(ports.step_ports(s)[0] == ports.RX_NONE
                                         for s in runnable if s.get("radio") == rid):
                        ui.label(f"{rid}: RX is running and this plan does not stop it; "
                                 f"stop RX first for TX only").classes(
                            "text-xs font-mono text-amber-700")
            ui.label("All radios start from one common start and line up to "
                     "within the link and command delay.").classes(
                "text-xs text-gray-500")
            if radiating:
                when = ("at these UTC times" if _tl_clock()
                        else "at its scheduled time")
                ui.label(f"Every radiating step below will key on its radio "
                         f"{when}:").classes("text-sm mt-1")
                with ui.column().classes("gap-0 max-h-72 overflow-y-auto"):
                    for s in radiating:
                        skipped = s.get("radio") in offline
                        ui.label(
                            f"{_tl_label_s(mf.step_start_s(s))}-"
                            f"{_tl_label_s(mf.step_stop_s(s))}  "
                            f"{s['technique']}{_step_detail(s)}  on  {s['radio']}"
                            + ("  (skipped, radio offline)" if skipped else "")) \
                            .classes("text-xs font-mono "
                                     + ("text-gray-400 line-through" if skipped
                                        else "text-red-700"))
            else:
                ui.label("No radiating steps in this plan.").classes(
                    "text-sm text-gray-600")
            if n_tasks:
                ui.label(f"{n_tasks} location task(s) are not started by Run mission: they key from "
                         "their areas when the triggers are armed; priority decides a clash "
                         "with the timeline.").classes("text-xs text-amber-700")
            nothing = bool(run_radios) and len(offline) == len(run_radios)
            if nothing:
                ui.label("None of the planned radios is online, so nothing "
                         "would run.").classes("text-sm text-red-600")
            ui.label("Confirm the chamber / load is connected and safe to key.") \
                .classes("text-xs text-gray-500")
            with ui.row().classes("justify-end w-full"):
                ui.button("Cancel", on_click=lambda: dlg.submit("cancel")).props("flat")
                key_btn = ui.button("KEY THE MISSION NOW",
                                    on_click=lambda: dlg.submit("go")) \
                    .props("color=negative")
                if nothing:
                    key_btn.disable()
        choice = await dlg
        if choice != "go":
            audit.add("confirm", "run cancelled at confirm")
            _refresh_audit()
            return
        audit.add("confirm", f"run confirmed: {len(live_radiating)} radiating "
                             f"step(s)" + (f"; offline, skipped: {', '.join(offline)}"
                                           if offline else ""))
        _refresh_audit()
        ui.notify("mission started", type="warning")
        asyncio.create_task(runner.run(mission()))
        _refresh_run_state()

    async def _do_stop_all():
        hosts = list(mgr.hosts.values())
        sent = await sess.stop_all(hosts)
        if sent == len(hosts):
            ui.notify(f"STOP ALL TX sent to {sent} radio(s)", type="warning")
        else:
            ui.notify(f"STOP ALL TX reached {sent} of {len(hosts)} radio(s); not answering: "
                      + ", ".join(runner.stop_unconfirmed)
                      + f"; stop_tx is retried for {runner.STOP_RETRY_S:g} s, see the audit log",
                      type="negative", timeout=0, close_button=True)
        if runner.atten_not_reset:
            ui.notify("RX attenuator NOT back to 120 dB: " + "; ".join(runner.atten_not_reset),
                      type="negative", timeout=0, close_button=True)
        _refresh_run_state()
        _refresh_timeline()
        _refresh_audit()

    async def _do_stop_step(sid):
        step = runner._active.get(sid)
        if step is None:
            ui.notify(f"{sid} is not keyed now", type="warning")
        else:
            await sess.stop_step(step)
            ui.notify(f"{sid} stopped; the rest of the plan carries on"
                      + ("; held until the platform leaves its area"
                         if sess.triggers.is_held(sid) else ""), type="warning")
        _refresh_run_state()
        _refresh_timeline()
        _refresh_audit()

    # periodic light refresh so connected radios appear as lanes and the run
    # state stays current
    def _tick():
        sess.autosave()
        _refresh_catalog_target()
        _refresh_run_state()
        _move_run_marker()
        _refresh_clock_strip()
        # a radio coming online or dropping off: redraw its row and its lane
        sig = _radios_sig()
        if sig != state.get("radios_sig"):
            state["radios_sig"] = sig
            _revalidate()
            _refresh_radios()
            _refresh_show_bar()
            _refresh_conflicts()
            _refresh_timeline()

    def _start_timers():
        # NiceGUI runs a ui.timer only once its page has a socket. A page
        # that never gets one (fetched by curl, or a load the browser gave
        # up on) is pruned a minute later with the timer still waiting; the
        # timer then wakes on the deleted page and logs "The parent slot of
        # the element has been deleted". So the timers are made here, in the
        # connect handler, where the page is known to be live. The handler
        # runs again when the same page reconnects, hence the flag.
        if state.get("timers"):
            return
        with root:
            state["timers"] = [ui.timer(1.0, _tick), ui.timer(1.0, _trigger_tick),
                               # poll each radio's clock (gps_status) a bit slower
                               ui.timer(5.0, _poll_radio_clocks)]
    ui.context.client.on_connect(_start_timers)

    # first paint
    state["view_rev"] = sess.view_rev
    _revalidate()
    _refresh_all()
    _refresh_show_bar()
    _refresh_audit()
    background_tasks.create(_dial_planned(), name="mission-dial-planned")
    root.__exit__(None, None, None)


def _catalog_card(entry, lane_target, state, add_step, lane_radios):
    card = ui.card().classes("w-full mp-cat-card").props("draggable=true flat") \
        .style("box-shadow:none")
    card.on("dragstart", lambda e, t=entry.technique: state.update(
        drag={"type": "new", "technique": t}))

    def _click():
        target = lane_target.value
        if not target:
            radios = lane_radios()
            target = radios[0] if radios else None
        if not target:
            ui.notify("no radio to add to; connect a radio first", type="warning")
            return
        add_step(entry.technique, target)
    card.on("click", _click)
    with card:
        with ui.row().classes("items-center gap-1 w-full no-wrap"):
            ui.label(entry.label).classes("text-xs grow")
            if entry.radiates:
                ui.icon("cell_tower").classes("text-sm shrink-0") \
                    .style(f"color:{_theme.TIMELINE_KINDS['tx']['border']}") \
                    .tooltip("radiates: needs the Run mission or trigger confirm")
    return card
