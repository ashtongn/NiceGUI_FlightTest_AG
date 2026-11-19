"""External 10 MHz and 1 PPS on a Matchstiq G40: the carrier clock and the PPS line.

What the hardware does (Epiq G20/G40 External Reference and PPS Integration Guide,
and the Getting Started Guide v1.0.1, Carrier Card Clock Configuration):

  10 MHz   The REF jack goes to input 1 of the carrier's HMC7044 clock chip. Input 0 is
           the 40 MHz from the GPS module. The HMC7044 makes the 40 MHz the two NV100
           cards run on when they are set to "carrier edge". Which input it uses is a
           setting of the chip: epiq-hmc-init.service writes it at boot from
           /epiq/hmc/hmc_device_g40.txt (clkin_input_source = 0), and hmc_from_file
           writes it again at any time from a file like that one. The setting is lost
           at a reboot. epiq-gps-ref ext10M does not touch it and is not used here.
  1 PPS    Line PCC.00 picks the pulse the cards get: high the GPS module's own pulse,
           low the chassis 1PPS jack. epiq-pps-init.service sets it high at boot.
           The cards' own 1 PPS source select is written external (the carrier pin).

Measured on a bench G40 on 5 November 2026: with the HMC7044 on input 1 and the 10 MHz
in, both PLLs lock and the card clock counts 40,000,000 ticks a second against the 1 PPS
within 0.06 ppm; with the 10 MHz pulled nothing reports an error and the count runs
about 137 ppm low. That count is the check used here: a lost 10 MHz shows as a clock
error far beyond anything a working reference gives.

The five choices the service offers, and what each does:

  internal   cards on their own oscillators, HMC7044 input 0, PCC.00 high
  gpsdo      as internal, plus the card's GPSDO discipline from the GPS module's 1 PPS
  external   HMC7044 input 1 and cards on carrier edge (the 10 MHz), PCC.00 low
  ext_10mhz  as external, PCC.00 high (10 MHz in, no 1 PPS cable)
  ext_pps    as internal, PCC.00 low (the chassis 1 PPS, own oscillator: the aircraft
             gives a 1 PPS and no 10 MHz)

An X40 takes internal, external and x40_gpsdo. Its GPSDO is a hardware one inside its
u-blox LEA-M8F, run from the ZynqMP; the card has no FPGA GPSDO, so skiq_gpsdo_enable
returns -95 (ENOTSUP) on it by design (Sidekiq API 4.25.0-1 p26 and p274, the same in
4.27.0 p30). What libsidekiq does give an X40 is the host reference clock: "Host (gpsdo)
reference clock configuration at 40 MHz", with libsidekiq 4.22.1 or later and both the
Jetson and the ZynqMP on BSP v3.0.0 or later (Software Development Manual 4.25.0-1 p58
and the 4.22.1 release notes). x40_gpsdo selects that and nothing else. The GPSDO's lock
cannot be read through libsidekiq on an X40, so the only evidence the service has is the
GNSS fix from the X40's gpsd.

Locked is not the same as on GPS time. With the card 1 PPS source on external (the G40
default) the FPGA GPSDO uses the pulse unconditionally (SDM 4.25.0-1 p59), and the GPS
module gives a pulse before it has a fix. gpsdo_verdict() puts the fix next to the lock
so a lock with no fix reads amber, not green.

Nothing here writes the NV100 EEPROM.
"""
from __future__ import annotations

import json
import os
import re
import select
import shutil
import struct
import subprocess
import tempfile
import time
from typing import Callable, Optional

GPS_USB_PORT = "/dev/ttyACM0"
PPS_CHIP = "gpiochip1"
PPS_LINE = "PCC.00"

# the HMC7044 tool and its boot file, as the host has them. The service container
# sees them only when they are mounted in (deploy/radio/g40_up_r36.sh); these can
# be pointed elsewhere for a test or another mount point.
HMC_FROM_FILE = os.environ.get("DPMG_HMC_FROM_FILE", "/usr/bin/hmc_from_file")
HMC_STOCK_FILE = os.environ.get("DPMG_HMC_STOCK_FILE", "/epiq/hmc/hmc_device_g40.txt")
HMC_STATE_FILE = os.environ.get("DPMG_HMC_STATE_FILE", "/tmp/dpmg_hmc_state.json")
HMC_INPUT_GPS = 0           # CLKIN0, the GPS module's 40 MHz
HMC_INPUT_EXT = 1           # CLKIN1, the REF jack
HMC_TIMEOUT_S = 20.0

SYS_TS_NOMINAL_HZ = 40_000_000      # the NV100 system timestamp clock
TEN_MHZ_LOST_PPM = 10.0             # beyond this the carrier is not on the 10 MHz
PPS_STALE_S = 2.5                   # no new pulse for this long: the 1 PPS is absent


# The clock choices. Key is what goes on the wire as clock_source. ref says what the
# cards run on, pps which pulse PCC.00 selects. Order is the order the GUI lists them.
CLOCK_CHOICES = {
    "internal":  {"label": "internal", "ref": "internal", "pps": "internal",
                  "help": "the card's own oscillator; the GPS module's 1 PPS"},
    "gpsdo":     {"label": "gpsdo (GPS antenna)", "ref": "gpsdo", "pps": "internal",
                  "help": "the card's oscillator disciplined from GPS; needs the antenna and its bias"},
    "external":  {"label": "external 10 MHz and 1 PPS", "ref": "external", "pps": "external",
                  "help": "10 MHz on REF through the HMC7044, 1 PPS on the 1PPS jack"},
    "ext_10mhz": {"label": "external 10 MHz only", "ref": "external", "pps": "internal",
                  "help": "10 MHz on REF through the HMC7044; the GPS module's 1 PPS"},
    "ext_pps":   {"label": "1 PPS only (own oscillator)", "ref": "internal", "pps": "external",
                  "help": "1 PPS on the 1PPS jack for time marks; the card's own oscillator"},
    "x40_gpsdo": {"label": "X40 GPSDO (host reference)", "ref": "host", "pps": "internal",
                  "help": "the X40's own GPSDO in its GPS module, selected as the host reference "
                          "clock; needs Jetson and ZynqMP BSP v3.0.0 or later; its lock cannot be "
                          "read, only the GPS fix"},
}
G40_CHOICES = ("internal", "gpsdo", "external", "ext_10mhz", "ext_pps")
X40_CHOICES = ("internal", "external", "x40_gpsdo")
LEGACY = ("internal", "external", "gpsdo")      # the tokens every radio kind takes

X40_GPSDO_REFUSED = (
    "clock source 'gpsdo' is the card's FPGA GPSDO, which the X40 does not have "
    "(skiq_gpsdo_enable returns -95 on an X40). Pick x40_gpsdo for the X40's own GPSDO "
    "through the host reference clock (needs Jetson and ZynqMP BSP v3.0.0), external for a "
    "10 MHz on its REF jack, or internal.")


def labels(kind="g40") -> dict:
    """{token: label} for a GUI select, in list order."""
    keys = G40_CHOICES if kind in ("g40", "mock", None, "") else X40_CHOICES
    return {k: CLOCK_CHOICES[k]["label"] for k in keys}


def label(token) -> str:
    c = CLOCK_CHOICES.get(str(token or "internal").lower())
    return c["label"] if c else str(token)


def resolve(token, kind="g40") -> dict:
    """The choice for this token on this radio kind, or a dict with "err" that says
    what to pick instead. Unknown tokens and the G40 only ones on another kind are
    refused here, before anything touches the radio."""
    t = str(token or "internal").lower()
    c = CLOCK_CHOICES.get(t)
    if c is None:
        return {"err": f"clock source {token!r} is not one of {', '.join(CLOCK_CHOICES)}"}
    if kind == "x40" and t == "gpsdo":
        return {"err": X40_GPSDO_REFUSED}
    if kind == "x40" and t not in X40_CHOICES:
        return {"err": f"clock source {t!r} is a G40 choice: the X40 has no 1 PPS switch. "
                       "Pick x40_gpsdo, external for a 10 MHz on its REF jack, or internal."}
    if kind != "x40" and t not in G40_CHOICES:
        return {"err": f"clock source {t!r} is an X40 choice. On a G40 pick gpsdo for the "
                       "GPS antenna, an external choice, or internal."}
    return dict(c, token=t)


# GPSDO lock and the GNSS fix

def has_fix(gnss) -> bool:
    """True when gps_status's gnss dict says 2D or 3D."""
    return bool(gnss) and str(gnss.get("fix") or "") in ("2D", "3D")


def fix_text(gnss) -> str:
    """'GPS fix 3D, 17 sats', 'no GPS fix' or 'GPS fix not read' (no gpsd answer)."""
    if not gnss:
        return "GPS fix not read"
    if not has_fix(gnss):
        return "no GPS fix"
    n = gnss.get("sats_used")
    return f"GPS fix {gnss['fix']}" + (f", {int(n)} sats" if n is not None else "")


def gpsdo_verdict(ref, locked, gnss) -> dict:
    """What the operator is told about a GPS disciplined clock, from the choice's ref
    ("gpsdo" on a G40, "host" for the X40's own GPSDO), the GPSDO lock (True, False or
    None) and gps_status's gnss dict (None when gpsd did not answer).

    Returns {"state", "text"}; state is
      locked    locked with a 2D or 3D fix (green)
      no_fix    locked with no fix: locked to a free running 1 PPS (amber)
      acquiring not locked yet (amber)
      unread    X40 GPSDO with a fix; libsidekiq cannot read its lock (neutral)
      None      not a GPS disciplined choice, or nothing read yet
    """
    if ref == "host":
        if has_fix(gnss):
            return {"state": "unread", "text": f"X40 GPSDO, {fix_text(gnss)}, lock not readable"}
        tail = "lock not readable" if not gnss else "not disciplined to GPS"
        return {"state": "no_fix", "text": f"X40 GPSDO, {fix_text(gnss)}, {tail}"}
    if ref != "gpsdo" or locked is None:
        return {"state": None, "text": ""}
    if not locked:
        return {"state": "acquiring", "text": f"GPSDO acquiring, {fix_text(gnss)}"}
    if has_fix(gnss):
        return {"state": "locked", "text": f"GPSDO locked, {fix_text(gnss)}"}
    return {"state": "no_fix", "text": "locked to 1 PPS, " + fix_text(gnss)}


# HMC7044

def hmc_file_input(text: str) -> Optional[int]:
    """clkin_input_source in an hmc_from_file text, or None when it has none."""
    m = re.search(r"^\s*clkin_input_source\s*=\s*(\d+)", text, re.M)
    return int(m.group(1)) if m else None


def hmc_file_with_input(text: str, inp: int) -> str:
    """The same file with clkin_input_source set to inp and print_debug_msg on: the
    tool prints its "Both PLLs locked" line only with the debug messages on
    (G40-0001, 8 November 2026), and that line is what the service reads."""
    if hmc_file_input(text) is None:
        raise ValueError("no clkin_input_source line in the HMC7044 file")
    out = re.sub(r"^(\s*clkin_input_source\s*=\s*)\d+", rf"\g<1>{int(inp)}", text, count=1, flags=re.M)
    return re.sub(r"^(\s*print_debug_msg\s*=\s*)\d+", r"\g<1>1", out, count=1, flags=re.M)


def hmc_boot_input(stock_file: str = HMC_STOCK_FILE) -> Optional[int]:
    """What epiq-hmc-init writes at boot, from the boot file; None when the file
    cannot be read (not mounted into the container)."""
    try:
        with open(stock_file) as fh:
            return hmc_file_input(fh.read())
    except OSError:
        return None


def host_boot_time() -> float:
    """The kernel's boot time as a unix time. Containers share the host kernel, so
    this is the host's boot, the moment the HMC7044 went back to its boot file."""
    try:
        with open("/proc/stat") as fh:
            for line in fh:
                if line.startswith("btime "):
                    return float(line.split()[1])
    except (OSError, ValueError):
        pass
    return 0.0


def hmc_state(state_file: str = HMC_STATE_FILE, stock_file: str = HMC_STOCK_FILE) -> dict:
    """The HMC7044 input as far as the service can know it without reading the chip
    (nothing on the unit reads it back; hmc_from_file reconfigures it).

    input:   0 or 1, or None when unknown
    source:  "written" (this service wrote it since the host booted), "boot" (the
             boot file, nothing written since the boot) or "unknown"
    locked:  the PLL lock hmc_from_file reported at the last write, else None
    """
    st = {"input": None, "source": "unknown", "locked": None, "written_at": None}
    try:
        with open(state_file) as fh:
            d = json.load(fh) or {}
        if float(d.get("t", 0)) >= host_boot_time() and d.get("input") in (0, 1):
            st.update(input=int(d["input"]), source="written", locked=d.get("locked"),
                      written_at=d.get("t"))
            return st
    except (OSError, ValueError, TypeError):
        pass
    b = hmc_boot_input(stock_file)
    if b is not None:
        st.update(input=b, source="boot")
    return st


def parse_hmc_output(text: str) -> dict:
    """What hmc_from_file printed: 'Both PLLs locked: 1, SPI interface works: 1' and
    'Return Code: 0' on a bench G40. Missing lines read as None."""
    out = {"locked": None, "spi": None, "rc": None}
    m = re.search(r"Both PLLs locked:\s*(\d)", text)
    if m:
        out["locked"] = m.group(1) == "1"
    m = re.search(r"SPI interface works:\s*(\d)", text)
    if m:
        out["spi"] = m.group(1) == "1"
    m = re.search(r"Return Code:\s*(-?\d+)", text)
    if m:
        out["rc"] = int(m.group(1))
    return out


def _save_state(state_file, d):
    try:
        with open(state_file + ".tmp", "w") as fh:
            json.dump(d, fh)
        os.replace(state_file + ".tmp", state_file)
    except OSError:
        pass


def hmc_write_input(inp: int, run: Optional[Callable] = None, tool: str = HMC_FROM_FILE,
                    stock_file: str = HMC_STOCK_FILE, state_file: str = HMC_STATE_FILE,
                    timeout_s: float = HMC_TIMEOUT_S) -> dict:
    """Set the HMC7044 input by running hmc_from_file on a copy of the boot file with
    clkin_input_source changed. The boot file itself is never written. Returns

      ok:      the tool ran and reported both PLLs locked
      locked:  the lock it reported (None when it did not say)
      input:   the input asked for
      output:  the tool's last lines
      err:     why it could not run, else absent

    Never raises. The cards must be closed: the write restarts the clock they run on."""
    res = {"ok": False, "locked": None, "input": int(inp), "output": ""}
    if run is None and not (os.path.isfile(tool) and os.access(tool, os.X_OK)):
        res["err"] = (f"{tool} is not reachable from the service: mount it and /epiq into the "
                      "container (deploy/radio/g40_up_r36.sh), or set the HMC7044 by hand and "
                      "send the init with extras hmc_set_by_hand")
        return res
    try:
        with open(stock_file) as fh:
            text = fh.read()
        copy = hmc_file_with_input(text, inp)
    except (OSError, ValueError) as e:
        res["err"] = f"HMC7044 boot file {stock_file}: {e}"
        return res
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix="hmc_input%d_" % int(inp), suffix=".txt")
        with os.fdopen(fd, "w") as fh:
            fh.write(copy)
        if run is None:
            def run(cmd, timeout):
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
                return p.returncode, (p.stdout or "") + (p.stderr or "")
        rc, out = run([tool, tmp], timeout_s)
    except subprocess.TimeoutExpired:
        res["err"] = f"{tool} did not finish in {timeout_s:.0f} s"
        return res
    except Exception as e:
        res["err"] = f"{tool}: {e}"
        return res
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
    p = parse_hmc_output(out)
    res.update(locked=p["locked"], output=out.strip()[-400:], tool_rc=rc)
    res["ok"] = rc == 0 and p["locked"] is True
    if rc != 0 and p["locked"] is None:
        res["err"] = f"{tool} returned {rc}: {out.strip()[-200:]}"
    elif p["locked"] is None:
        res["err"] = f"{tool} gave no PLL lock report (return code {rc}): {out.strip()[-160:]}"
    _save_state(state_file, {"t": time.time(), "input": int(inp), "locked": p["locked"], "rc": rc})
    return res


def hmc_record_by_hand(inp: int, state_file: str = HMC_STATE_FILE) -> None:
    """The operator set the HMC7044 by hand (hmc_from_file at a shell): remember it
    the same way, with no lock word since the service did not see the tool's output."""
    _save_state(state_file, {"t": time.time(), "input": int(inp), "locked": None, "by_hand": True})


def hmc_tool_present(tool: str = HMC_FROM_FILE, stock_file: str = HMC_STOCK_FILE) -> dict:
    """Whether the service can run the HMC7044 tool from where it is."""
    return {"tool": tool, "tool_ok": bool(shutil.which(tool) or (os.path.isfile(tool) and os.access(tool, os.X_OK))),
            "stock_file": stock_file, "stock_ok": os.path.isfile(stock_file)}


# The card clock against the 1 PPS

class ClockMeter:
    """Ticks of the card's system timestamp clock between 1 PPS pulses, from
    skiq_read_last_1pps_timestamp read every second or two. The FPGA latches the
    timestamp at the pulse, so the count does not depend on when it is read.

    feed(sys_ts, now) takes the last-1PPS system timestamp and the wall time of the
    read; result() gives

      pps_present   a new pulse was seen within PPS_STALE_S
      ppm           the clock error against the 1 PPS, from the last two distinct
                    pulses seen (None until two are in, or when the pulse is gone)
      ticks_per_s   the count per second behind ppm
      seconds       how many pulses apart those two reads were
    """

    def __init__(self, nominal_hz: float = SYS_TS_NOMINAL_HZ):
        self.nominal = float(nominal_hz or SYS_TS_NOMINAL_HZ)
        self.last_ts = None          # the last distinct timestamp
        self.last_t = None           # wall time it was first seen
        self.ppm = None
        self.ticks_per_s = None
        self.seconds = None

    def feed(self, sys_ts: Optional[int], now: Optional[float] = None) -> dict:
        now = time.monotonic() if now is None else now
        if sys_ts is not None and sys_ts > 0 and sys_ts != self.last_ts:
            if self.last_ts is not None and sys_ts > self.last_ts:
                d = sys_ts - self.last_ts
                n = max(1, int(round(d / self.nominal)))
                self.ticks_per_s = d / n
                self.ppm = (self.ticks_per_s - self.nominal) / self.nominal * 1e6
                self.seconds = n
            elif self.last_ts is not None:
                # the timestamp went backwards: the card was re-timed; start over
                self.ppm = self.ticks_per_s = self.seconds = None
            self.last_ts, self.last_t = sys_ts, now
        return self.result(now)

    def result(self, now: Optional[float] = None) -> dict:
        now = time.monotonic() if now is None else now
        if self.last_t is None:
            return {"pps_present": None, "ppm": None, "ticks_per_s": None, "seconds": None,
                    "pps_age_s": None}
        present = now - self.last_t <= PPS_STALE_S
        return {"pps_present": present,
                "ppm": self.ppm if present else None,
                "ticks_per_s": self.ticks_per_s if present else None,
                "seconds": self.seconds if present else None,
                "pps_age_s": round(now - self.last_t, 1)}

    def reset(self):
        self.__init__(self.nominal)


def ten_mhz_verdict(choice: dict, hmc: dict, meas: dict) -> dict:
    """What the operator is told about the 10 MHz. Only a choice with ref external
    has one. Returns {"ten_mhz": "in use" | "LOST" | "not measured" | None, "why": ...}.

    in use        the HMC7044 is on input 1 and the card clock agrees with the 1 PPS
                  within TEN_MHZ_LOST_PPM
    LOST          the card clock is off by more than that: the 10 MHz is not there
                  (about -137 ppm on a bench G40) or the HMC7044 is not locked to it
    not measured  no 1 PPS to count against, or no two pulses yet
    """
    if not choice or choice.get("ref") != "external":
        return {"ten_mhz": None, "why": ""}
    if hmc.get("input") != HMC_INPUT_EXT:
        return {"ten_mhz": "LOST", "why": f"HMC7044 on input {hmc.get('input')}, not the REF jack"}
    if hmc.get("locked") is False:
        return {"ten_mhz": "LOST", "why": "HMC7044 PLLs not locked at the last write"}
    ppm = meas.get("ppm")
    if ppm is None:
        if meas.get("pps_present") is False:
            return {"ten_mhz": "not measured", "why": "no 1 PPS to count the clock against"}
        return {"ten_mhz": "not measured", "why": "waiting for two 1 PPS pulses"}
    if abs(ppm) > TEN_MHZ_LOST_PPM:
        return {"ten_mhz": "LOST", "why": f"card clock {ppm:+.1f} ppm against the 1 PPS"}
    return {"ten_mhz": "in use", "why": f"card clock {ppm:+.3f} ppm against the 1 PPS"}


def clock_text(st: dict) -> str:
    """One short line for a GUI chip from the service's clock dict."""
    if not st:
        return ""
    parts = [label(st.get("choice"))]
    tm = st.get("ten_mhz")
    ppm = st.get("clock_ppm")
    if tm == "in use":
        parts.append(f"10 MHz in use ({ppm:+.2f} ppm)" if ppm is not None else "10 MHz in use")
    elif tm == "LOST":
        parts.append(f"10 MHz LOST ({ppm:+.0f} ppm)" if ppm is not None else "10 MHz LOST")
    elif tm == "not measured":
        parts.append("10 MHz not measured" + (" (no 1 PPS)" if st.get("pps_present") is False else ""))
    elif ppm is not None:
        parts.append(f"clock {ppm:+.2f} ppm vs 1 PPS")
    pp = st.get("pps_present")
    if pp is not None:
        parts.append(f"1 PPS {'present' if pp else 'ABSENT'} ({st.get('pps') or '?'})")
    return ", ".join(parts)


# The 1 PPS line

def set_pps_line(external: bool, backend=None) -> None:
    """PCC.00 low for the external 1 PPS, high for the internal one. Written once and
    released, as Epiq's boot service does: the driver keeps the pin state."""
    if backend is None:
        from dpmg.payload.rf_frontend import GpiodBackend
        backend = GpiodBackend()
    h = backend.open_line(PPS_CHIP, PPS_LINE, None, "out", True, initial_active=not external)
    h.release()


# The GPS module over USB: what epiq-gps-ref set. Kept for tools/read_smgr.py and the
# engineering line; it says nothing about the 10 MHz path above.

CFG_SMGR = (0x06, 0x62)
MON_SMGR = (0x0A, 0x2E)

SMGR_FLAGS_EXT10M = 0x1B0E0
SMGR_FLAGS_INTERNAL = 0x1B0E9

_DISC_SRC = {0: "internal", 1: "GNSS", 2: "EXTINT0", 3: "EXTINT1",
             4: "internal, host measured", 5: "external, host measured"}


def ubx_frame(cls: int, mid: int, payload: bytes = b"") -> bytes:
    body = bytes([cls, mid]) + struct.pack("<H", len(payload)) + payload
    a = b = 0
    for x in body:
        a = (a + x) & 0xFF
        b = (b + a) & 0xFF
    return b"\xb5\x62" + body + bytes([a, b])


def find_ubx(buf: bytes, cls: int, mid: int) -> Optional[bytes]:
    """The payload of the first good frame of this class and id in buf, or None.
    The port also carries NMEA, so frames can sit anywhere in it."""
    i = 0
    while True:
        i = buf.find(b"\xb5\x62", i)
        if i < 0 or i + 6 > len(buf):
            return None
        n = struct.unpack_from("<H", buf, i + 4)[0]
        end = i + 6 + n + 2
        if end <= len(buf) and buf[i + 2] == cls and buf[i + 3] == mid:
            frame = buf[i:end]
            if ubx_frame(cls, mid, frame[6:-2]) == frame:
                return frame[6:-2]
        i += 2


def smgr_mode(flags: int) -> str:
    if flags == SMGR_FLAGS_EXT10M:
        return "ext10M"
    if flags == SMGR_FLAGS_INTERNAL:
        return "internal"
    return f"flags x{flags:x}"


def parse_cfg_smgr(p: bytes) -> dict:
    if len(p) < 20:
        return {}
    flags = struct.unpack_from("<I", p, 16)[0]
    return {"mode": smgr_mode(flags), "flags": flags}


def parse_mon_smgr(p: bytes) -> dict:
    if len(p) < 16:
        return {}
    ext_osc = struct.unpack_from("<H", p, 10)[0]
    return {"disc_src": _DISC_SRC.get(p[12], f"source {p[12]}"),
            "ext_osc_state": ext_osc & 0x0F, "ext_int1": p[15]}


def _serial_poll(port: str, frames: list, want: list, timeout_s: float) -> bytes:
    """Send the poll frames and read until every wanted reply is in or the time is up."""
    import termios
    fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        attrs = termios.tcgetattr(fd)
        attrs[0] = attrs[1] = attrs[3] = 0
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        for f in frames:
            os.write(fd, f)
        buf, end = b"", time.monotonic() + timeout_s
        while time.monotonic() < end:
            r, _, _ = select.select([fd], [], [], 0.05)
            if r:
                buf += os.read(fd, 4096)
                if all(find_ubx(buf, *w) is not None for w in want):
                    break
        return buf
    finally:
        os.close(fd)


def read_carrier(port: str = GPS_USB_PORT, timeout_s: float = 1.0,
                 poll: Optional[Callable] = None) -> dict:
    """What the GPS module says about its own setting. Never raises. Do not call
    while epiq-gps-ref runs: two readers on the port split its bytes.

    mode:     "ext10M", "internal", other flags, or None when it could not be read
    disc_src: what is disciplining the module's oscillator now, None when not read
    locked:   True when set to ext10M and EXTINT1 is the source, False when it is
              read and that is not so, None when it could not be read
    """
    want = [CFG_SMGR, MON_SMGR]
    try:
        buf = (poll or _serial_poll)(port, [ubx_frame(*w) for w in want], want, timeout_s)
    except Exception as e:
        return {"mode": None, "disc_src": None, "locked": None, "err": str(e)[:80]}
    cfg = parse_cfg_smgr(find_ubx(buf, *CFG_SMGR) or b"")
    mon = parse_mon_smgr(find_ubx(buf, *MON_SMGR) or b"")
    out = {"mode": cfg.get("mode"), "disc_src": mon.get("disc_src"), "locked": None}
    if out["mode"] is None and out["disc_src"] is None:
        out["err"] = "no reply from the GPS module"
    elif out["mode"] is not None and out["mode"] != "ext10M":
        out["locked"] = False
    elif out["disc_src"] is not None:
        out["locked"] = out["mode"] == "ext10M" and out["disc_src"] == "EXTINT1"
    return out
