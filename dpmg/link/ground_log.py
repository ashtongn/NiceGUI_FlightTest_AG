"""Laptop side logging behind the operator view's Log information button.

What is being logged and the open files live here, one RadioLog per radio serial,
in the GUI process. A browser page goes away when the operator moves to the
mission view or reloads, so a page only starts, stops and reads this state, and
GroundLogs.run writes the rows from its own task with no page open. A GUI restart
still ends the logging until the next Start logging.

Each Start logging opens new files, output/<category>_<serial>_<local start>.csv.
Radio side logging (log_detections, log_telemetry) stays on the radio, whose
get_config 'log' entry is the truth for it. The radio keeps no decode log, so decodes
always come here.

The GUI container runs in UTC, so the gcs_time column and the file names use the
UTC offset of the browser that pressed Start logging. The column header names the
zone, as in gcs_time (UTC-05:00), so a reader knows it is the ground station's clock
and not the radio's. t_unix is beside it either way, for lining up with the radio's
own files.

The ref_locked column in the health file is the STATUS flag. On a G40 with an
external 10 MHz it follows the service's own check of the card clock against the 1 PPS;
the two columns after it carry that check (docs/LOGGING.md). On a GPS disciplined
clock the flag only says the GPSDO is locked to a 1 PPS, which the GPS module gives
with or without a fix; gps_fix and gps_sats carry the fix from the radio's gpsd, and
gpsdo names the verdict (locked, no_fix, acquiring, unread for the X40 GPSDO).
"""

import asyncio
import csv
import inspect
import logging
import os
import time

from dpmg.link import proto
from dpmg.link.link_client import DET_CACHE_MAX
from dpmg.radio import g40_clock as _g40_clock
from dpmg.paths import match_folder_owner, match_owners_under

log = logging.getLogger(__name__)

CATEGORIES = ("detections", "position", "health", "decodes")
HEADERS = {
    "detections": ("gcs_time", "t_unix", "freq_hz", "snr_db", "dur_ms"),
    "position": ("gcs_time", "t_unix", "lat", "lon", "alt_m", "source",
                 "heading_deg", "speed_kt", "fix_mode", "sats", "aircraft"),
    "health": ("gcs_time", "t_unix", "state", "overruns", "underruns", "fps",
               "uptime_s", "clock", "ref_locked", "clock_vs_pps_ppm", "ten_mhz",
               "gps_fix", "gps_sats", "gpsdo"),
    "decodes": ("gcs_time", "t_unix", "band", "freq_hz", "modulation", "message",
                "frames"),
}


def _csv_cell(text) -> str:
    """One hand-written cell: quoted when the text has a comma or a quote."""
    text = str(text or "")
    if any(c in text for c in ',"\n'):
        return '"' + text.replace('"', '""') + '"'
    return text


def zone_text(utc_offset_s, t=None):
    """The zone for the header, UTC-05:00 style: from the browser's offset, or
    with None this process's own zone at time t."""
    if utc_offset_s is None:
        utc_offset_s = time.localtime(time.time() if not t else t).tm_gmtoff
    off = int(round(utc_offset_s))
    if off == 0:
        return "UTC"
    h, m = divmod(abs(off) // 60, 60)
    return f"UTC{'+' if off > 0 else '-'}{h:02d}:{m:02d}"


def header(cat, utc_offset_s=None, t=None):
    """One file's header row: the first column is named for the ground station
    clock and its zone, so a reader knows whose time it is."""
    cols = list(HEADERS[cat])
    cols[0] = f"gcs_time ({zone_text(utc_offset_s, t)})"
    return cols
TICK_S = 0.25                  # WATCH_UPDATE comes at 2 Hz, don't miss a snapshot
PERIODIC_S = 1.0               # position and health rows


class RadioLog:
    """What one radio is logging, and its open files."""

    def __init__(self, serial, out_dir="output"):
        self.serial = serial
        self.out_dir = out_dir
        self.dst = "ground"
        self.cats = {c: False for c in CATEGORIES}     # as ticked in the dialog
        self.radio_on = False          # this GUI asked the radio to log
        self.note = ""                 # the dialog's status line
        self.error = ""                # why logging stopped by itself
        self.fp = {}                   # category: open file
        self.last_periodic = 0.0
        self.utc_offset_s = None       # None: this process's own time zone
        # Kept over a stop and start, so a row the radio still holds is in one file
        # and not in the next one as well.
        self.det_seen = set()          # det_index values already written
        self.det_head = 0
        self.band_seen = {}            # band name: (frames, message) last written
        self._tick_warned = False

    @property
    def on(self):
        return bool(self.fp) or self.radio_on

    def local_cats(self):
        return [c for c in CATEGORIES if c in self.fp]

    def start(self, dst, cats, utc_offset_s=None, now=None):
        """Open new local files for this choice and return their paths. On the
        radio only the decodes are local. OSError if a file will not open."""
        self.stop()
        self.utc_offset_s = utc_offset_s
        self._start_t = time.time() if now is None else now
        self.dst = dst
        self.cats = {c: bool(cats.get(c)) for c in CATEGORIES}
        self.last_periodic = 0.0
        local = [c for c in CATEGORIES if self.cats[c]
                 and (dst == "ground" or c == "decodes")]
        stamp = self._local(time.time() if now is None else now, "%Y-%m-%d_%H%M%S")
        paths = []
        try:
            os.makedirs(self.out_dir, exist_ok=True)
            for c in local:
                paths.append(self._open(c, stamp))
        except OSError:
            self.stop()
            raise
        match_owners_under(self.out_dir)   # earlier files are not opened again
        return paths

    def stop(self):
        for fp in self.fp.values():
            try:
                fp.close()
            except OSError:
                pass
        self.fp = {}
        self.radio_on = False
        self.note = "logging off"
        self.error = ""
        self._tick_warned = False

    def _local(self, t, fmt="%Y-%m-%d %H:%M:%S"):
        if not t:
            return ""
        return time.strftime(fmt, time.localtime(t) if self.utc_offset_s is None
                             else time.gmtime(t + self.utc_offset_s))

    def _open(self, cat, stamp):
        path = os.path.join(self.out_dir, f"{cat}_{self.serial}_{stamp}.csv")
        fp = open(path, "a", newline="", encoding="utf-8", errors="replace")
        self.fp[cat] = fp
        if fp.tell() == 0:
            csv.writer(fp, lineterminator="\n").writerow(
                header(cat, self.utc_offset_s, getattr(self, "_start_t", None)))
            fp.flush()
        match_folder_owner(path, self.out_dir)
        return path

    def tick(self, host, acft_latest, state_text, now=None):
        """Append what is new from this host to the open files."""
        now = time.time() if now is None else now
        if "detections" in self.fp:
            self._log_detections(host)
        if "decodes" in self.fp:
            self._log_decodes(host, now)
        if now - self.last_periodic >= PERIODIC_S:
            self.last_periodic = now
            # the last STATUS of a lost link is not news, so no health row and no
            # position from it: a gap in the file is the link being down
            st = host.status if host.connected else None
            if "position" in self.fp:
                self._log_position(st, acft_latest, now)
            if "health" in self.fp and st is not None:
                self._log_health(host, st, state_text, now)

    def _log_detections(self, host):
        head = host.det_head
        if head < self.det_head:       # the service restarted, its index rewound
            self.det_seen.clear()
        self.det_head = head
        fp, wrote = self.fp["detections"], False
        for i in sorted(host.detections):
            if i not in self.det_seen:
                d = host.detections[i]
                t = getattr(d, "t_unix", 0)
                fp.write(f"{self._local(t)},{t:.3f},{d.freq_hz:.0f},"
                         f"{d.snr_db:.1f},{d.duration_ms:.0f}\n")
                self.det_seen.add(i)
                wrote = True
        if wrote:
            fp.flush()
        # the link client never asks for anything older than one cache behind head
        if len(self.det_seen) > 3 * DET_CACHE_MAX:
            floor = head - 2 * DET_CACHE_MAX
            self.det_seen = {i for i in self.det_seen if i >= floor}

    def _log_decodes(self, host, now):
        """One row each time a band's frame count or message changes. A WATCH_UPDATE
        row holds only the band's latest message, so frames can go up by more than
        one between rows. A sealed message stays sealed here: the plain text is not
        written to disk."""
        fp, wrote = self.fp["decodes"], False
        writer = csv.writer(fp, lineterminator="\n")
        for b in getattr(host, "watch_bands", None) or []:
            name, msg = str(b.get("n", "")), str(b.get("msg", ""))
            frames = int(b.get("dn", 0) or 0)
            if (frames <= 0 and not msg) or self.band_seen.get(name) == (frames, msg):
                continue
            self.band_seen[name] = (frames, msg)
            writer.writerow((self._local(now), f"{now:.3f}", name,
                             f"{float(b.get('f', 0.0)):.0f}",
                             b.get("m", ""), msg, frames))
            wrote = True
        if wrote:
            fp.flush()

    def _log_position(self, st, acft_latest, now):
        """The radio's STATUS position when it has one, else the aircraft feed's
        last fix with its heading (degrees true), ground speed (knots), fix mode
        and satellites used when the feed carried them; the radio's STATUS has
        none of those, so they stay blank on a status row. acft_latest is the
        feed's AircraftPos, or (aircraft name, AircraftPos) when this radio is
        bound to a named aircraft; the name goes in the last column."""
        fp = self.fp["position"]
        stamp = f"{self._local(now)},{now:.1f}"
        name = ""
        if isinstance(acft_latest, tuple):
            name, acft_latest = (acft_latest + (None, None))[:2]
            name = str(name or "")
        if st is not None and (st.flags & proto.Status.FLAG_POS_VALID):
            fp.write(f"{stamp},{st.lat_1e7/1e7:.6f},"
                     f"{st.lon_1e7/1e7:.6f},{st.alt_m:.1f},status,,,,,\n")
        elif acft_latest is not None:
            a = acft_latest
            hdg = getattr(a, "heading_deg", None)
            spd = getattr(a, "speed_kt", None)
            mode = getattr(a, "fix_mode", None)
            sats = getattr(a, "sats_used", None)
            fp.write(f"{stamp},{a.lat_deg:.6f},{a.lon_deg:.6f},"
                     f"{(a.alt_m or 0):.1f},acft_mcast,"
                     f"{'' if hdg is None else f'{hdg:.1f}'},"
                     f"{'' if spd is None else f'{spd:.1f}'},"
                     f"{mode or ''},{'' if sats is None else int(sats)},{_csv_cell(name)}\n")
        else:
            fp.write(f"{stamp},,,,no_fix,,,,,{_csv_cell(name)}\n")
        fp.flush()

    def _log_health(self, host, st, state_text, now):
        rl = 1 if (st.flags & proto.Status.FLAG_REF_LOCKED) else 0
        cfg = getattr(host, "config", None) or {}
        clock = cfg.get("clock_source", "")
        # the G40 service's clock dict: the card clock counted against the 1 PPS and
        # the 10 MHz verdict, so a lost 10 MHz is in this file within a few seconds
        ck = cfg.get("clock") or {}
        ppm = ck.get("clock_ppm")
        # the GNSS fix beside the lock (empty when the radio sends none)
        g = cfg.get("gnss") if "gnss" in cfg else None
        fix = (g or {}).get("fix") or ("" if "gnss" not in cfg else "none")
        sats = (g or {}).get("sats_used")
        ref = (_g40_clock.CLOCK_CHOICES.get(str(ck.get("choice") or clock)) or {}).get("ref")
        gv = _g40_clock.gpsdo_verdict(ref, bool(rl), g)["state"] if "gnss" in cfg else None
        fp = self.fp["health"]
        fp.write(f"{self._local(now)},{now:.1f},{state_text(st.radio_state)},"
                 f"{st.overruns},{st.underruns},"
                 f"{st.fps},{st.uptime_s},{clock},{rl},"
                 f"{'' if ppm is None else f'{ppm:.3f}'},{ck.get('ten_mhz') or ''},"
                 f"{fix},{'' if sats is None else int(sats)},{gv or ''}\n")
        fp.flush()


def label_text(rlog, cfg):
    """The operator's logging line for one radio: the local files from its RadioLog
    (None if it never logged), the radio side from its get_config (None when stale
    or an older service, then what this GUI last asked for)."""
    parts = []
    local = rlog.local_cats() if rlog is not None else []
    if local:
        parts.append(f"{', '.join(local)} to local")
    if cfg is not None and "log" in cfg:
        radio = [k for k, v in (cfg.get("log") or {}).items() if v]
    elif rlog is not None and rlog.radio_on:
        radio = [c for c in CATEGORIES if rlog.cats.get(c) and c != "decodes"]
    else:
        radio = []
    if radio:
        parts.append(f"{', '.join(radio)} to G40")
    if parts:
        return "logging " + ", ".join(parts)
    return (rlog.error if rlog is not None and rlog.error else "") or "logging off"


class GroundLogs:
    """Every radio's RadioLog, and the task that writes them."""

    def __init__(self, get_hosts, get_acft=lambda: None, state_text=str,
                 out_dir="output"):
        self.get_hosts = get_hosts
        # get_acft() gives the feed's last fix; get_acft(serial) may instead
        # give (aircraft name, fix) for the aircraft that radio is bound to
        self.get_acft = get_acft
        self.state_text = state_text
        self.out_dir = out_dir
        self.logs = {}                 # serial: RadioLog
        self.task = None               # the run() task, held so it is not collected

    def find(self, serial):
        return self.logs.get(serial)

    def get(self, serial):
        rl = self.logs.get(serial)
        if rl is None:
            rl = self.logs[serial] = RadioLog(serial, self.out_dir)
        return rl

    def tick(self, now=None):
        hosts = self.get_hosts()
        for serial, rl in list(self.logs.items()):
            host = hosts.get(serial)
            if not rl.fp or host is None:
                continue
            try:
                rl.tick(host, self._acft_for(serial), self.state_text, now)
            except OSError as e:       # disk full, say: stop and say so on the label
                log.warning("ground log for %s stopped: %s", serial, e)
                rl.stop()
                rl.error = rl.note = f"logging stopped, write failed: {e}"
            except Exception:          # noqa: BLE001 - one bad row must not end the task
                if not rl._tick_warned:
                    rl._tick_warned = True
                    log.exception("ground log tick for %s", serial)

    def _acft_for(self, serial):
        fn = self.get_acft
        try:
            n = len(inspect.signature(fn).parameters)
        except (TypeError, ValueError):
            n = 0
        return fn(serial) if n >= 1 else fn()

    async def run(self):
        match_owners_under(self.out_dir)   # what an earlier run left behind as root
        while True:
            await asyncio.sleep(TICK_S)
            self.tick()
