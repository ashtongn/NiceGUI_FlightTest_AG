"""mc_atten.py: Mini-Circuits programmable attenuator (RCDAT-6G-120H and family).

Two transports, no third-party library needed:

* ``hidraw``: the attenuator is a USB HID device (VID 0x20CE, PID 0x0023).  The
  G40 kernel has usbhid/hid-generic/hidraw built in, so plugging it in creates
  ``/dev/hidrawN``; a udev rule (deploy/g40/60-minicircuits.rules) gives group
  ``periph`` access.  Protocol (Mini-Circuits Programming Manual C9, section 5):
  64-byte interrupt reports, byte 0 = command code; code 1 = "send SCPI string"
  (ASCII in bytes 1..n, zero terminated reply).  On hidraw the write is prefixed
  with a 0 report-id byte (65 bytes total).
* ``http``: Ethernet models answer ``GET http://<ip>/:SETATT=12.75`` with the
  same SCPI reply text.  (The unit still needs USB for power.)

SCPI used: ``:SETATT=<dB>`` (reply 1 = ok, 2 = out of range), ``:ATT?``, ``:MN?``,
``:SN?``, ``:FIRMWARE?``, ``:STARTUPATT:VALUE:<dB>``.  The RCDAT-6G-120H has
0.05 dB steps (0.10 above 105 dB), which the legacy quarter-dB binary command
(code 19) cannot express, so everything goes through SCPI.
"""
from __future__ import annotations

import glob
import os
import re
import select
import stat
import time
import urllib.parse
import urllib.request
from typing import List, Optional

MC_VID = 0x20CE
MC_PID_ATTEN = 0x0023
REPORT_LEN = 64
CMD_SCPI = 1
CMD_MODEL = 40
CMD_SERIAL = 41
CMD_FIRMWARE = 99


class AttenError(Exception):
    pass


# transports
class Transport:
    name = "base"

    def scpi(self, cmd: str, timeout: float = 1.0) -> str:
        raise NotImplementedError

    def close(self) -> None:
        pass

    def describe(self) -> str:
        return self.name


def frame_scpi(cmd: str, with_report_id: bool = True) -> bytes:
    """Build the HID report for a SCPI command: [report-id 0] [code 1] [ascii] [0 pad]."""
    body = cmd.encode("ascii")
    if len(body) > REPORT_LEN - 2:
        raise AttenError(f"SCPI command too long ({len(body)} bytes)")
    rep = bytes([CMD_SCPI]) + body
    rep = rep + b"\0" * (REPORT_LEN - len(rep))
    return (b"\0" + rep) if with_report_id else rep


def parse_reply(buf: bytes, expect_code: int = CMD_SCPI) -> str:
    """Decode a 64-byte reply: byte 0 echoes the command code, then ASCII to NUL."""
    if not buf:
        raise AttenError("empty reply")
    if buf[0] != expect_code:
        raise AttenError(f"reply code {buf[0]} != {expect_code}")
    payload = buf[1:]
    end = payload.find(b"\0")
    if end >= 0:
        payload = payload[:end]
    return payload.decode("ascii", errors="replace").strip()


def find_hidraw(vid: int = MC_VID, pid: int = MC_PID_ATTEN) -> List[str]:
    """/dev/hidrawN nodes whose HID_ID matches the Mini-Circuits attenuator."""
    want = f":{vid:08X}:{pid:08X}".upper()
    out = []
    for dev in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            with open(os.path.join(dev, "device", "uevent"), "r") as f:
                txt = f.read().upper()
        except OSError:
            continue
        for line in txt.splitlines():
            if line.startswith("HID_ID=") and line.strip().endswith(want):
                out.append("/dev/" + os.path.basename(dev))
    return out


def ensure_dev_node(path: str, sysfs: str = "/sys/class/hidraw") -> bool:
    """Make /dev/hidrawN when the kernel has the device but this /dev has no node.
    A container's /dev is a copy taken when the container started, so a unit
    plugged in later shows in /sys but not in /dev; the radio service runs as
    root in a privileged container and may create the node from the kernel's
    device number. True when the node exists afterwards."""
    if os.path.exists(path):
        return True
    try:
        with open(os.path.join(sysfs, os.path.basename(path), "dev")) as f:
            major, minor = (int(x) for x in f.read().strip().split(":"))
        os.mknod(path, 0o660 | stat.S_IFCHR, os.makedev(major, minor))
        return True
    except (OSError, ValueError):
        return False


class HidrawTransport(Transport):
    name = "hidraw"

    def __init__(self, path: Optional[str] = None):
        if not path or path == "auto":
            found = find_hidraw()
            if not found:
                raise AttenError("no Mini-Circuits attenuator (20ce:0023) on hidraw")
            path = found[0]
        self.path = path
        ensure_dev_node(path)                 # hot-plugged into a running container
        try:
            self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        except OSError as e:
            raise AttenError(f"open {path}: {e} (udev rule / periph group?)") from e

    def _drain(self) -> None:
        while True:
            r, _, _ = select.select([self.fd], [], [], 0)
            if not r:
                return
            try:
                if not os.read(self.fd, REPORT_LEN + 1):
                    return
            except BlockingIOError:
                return

    def scpi(self, cmd: str, timeout: float = 1.0) -> str:
        if self.fd < 0:
            # closed (unplugged and detached): never write a HID frame into an fd
            # number the process may have reused for something else
            raise AttenError(f"{self.path} is closed")
        self._drain()
        n = os.write(self.fd, frame_scpi(cmd))
        if n < REPORT_LEN:
            raise AttenError(f"short HID write ({n} bytes)")
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            raise AttenError(f"timeout waiting for reply to {cmd!r}")
        buf = os.read(self.fd, REPORT_LEN + 1)
        return parse_reply(buf)

    def close(self) -> None:
        fd, self.fd = self.fd, -1
        if fd < 0:
            return
        try:
            os.close(fd)
        except OSError:
            pass

    def describe(self) -> str:
        return f"hidraw:{self.path}"


class HttpTransport(Transport):
    name = "http"

    def __init__(self, url: str, opener=None):
        if not url.startswith("http"):
            url = "http://" + url
        self.url = url.rstrip("/")
        self._open = opener or urllib.request.urlopen

    def scpi(self, cmd: str, timeout: float = 2.0) -> str:
        # Mini-Circuits accepts the raw SCPI text as the path; keep ':' '=' '?' as-is.
        q = urllib.parse.quote(cmd, safe=":=?,;")
        with self._open(f"{self.url}/{q}", timeout=timeout) as resp:
            return resp.read().decode("ascii", errors="replace").strip()

    def describe(self) -> str:
        return f"http:{self.url}"


class MockTransport(Transport):
    """Emulates the attenuator's SCPI behaviour; records every command."""
    name = "mock"

    def __init__(self, model: str = "RCDAT-6G-120H", serial: str = "11901234567",
                 max_db: float = 120.0, step: float = 0.05, att: float = 120.0):
        self.model, self.serial_no = model, serial
        self.max_db, self.step = max_db, step
        self.att = att
        self.startup = att
        self.commands: List[str] = []
        self.fail_next: Optional[Exception] = None

    def scpi(self, cmd: str, timeout: float = 1.0) -> str:
        self.commands.append(cmd)
        if self.fail_next is not None:
            e, self.fail_next = self.fail_next, None
            raise e
        if cmd == ":MN?":
            return f"MN={self.model}"
        if cmd == ":SN?":
            return f"SN={self.serial_no}"
        if cmd == ":FIRMWARE?":
            return "C9"
        if cmd == ":ATT?":
            return f"{self.att:.2f}"
        m = re.fullmatch(r":SETATT=([0-9.]+)", cmd)
        if m:
            v = float(m.group(1))
            if v < 0 or v > self.max_db:
                return "2"
            self.att = round(v / self.step) * self.step
            return "1"
        m = re.fullmatch(r":STARTUPATT:VALUE:([0-9.]+)", cmd)
        if m:
            self.startup = float(m.group(1))
            return "1"
        return "0"


# device
class MiniCircuitsAtten:
    """High-level control.  ``set_db`` snaps to the step and bounds-checks before
    anything reaches the device, so an out-of-range request never un-attenuates."""

    def __init__(self, transport: Transport, max_db: float = 120.0, step_db: float = 0.05):
        self.t = transport
        self.max_db = float(max_db)
        self.step = float(step_db)
        self.last_set: Optional[float] = None

    # helpers
    def snap(self, db: float) -> float:
        db = float(db)
        if db != db:                                 # NaN
            raise AttenError("attenuation is NaN")
        if db < 0.0 or db > self.max_db:
            raise AttenError(f"attenuation {db} dB outside 0..{self.max_db}")
        return round(round(db / self.step) * self.step, 2)

    @staticmethod
    def _fmt(db: float) -> str:
        return f"{db:.2f}"

    @staticmethod
    def _value(reply: str) -> str:
        # replies may be "MN=RCDAT-6G-120H" or bare numbers; strip a KEY= prefix
        return reply.split("=", 1)[1].strip() if "=" in reply else reply.strip()

    # commands
    def set_db(self, db: float) -> float:
        v = self.snap(db)
        r = self.t.scpi(f":SETATT={self._fmt(v)}")
        if r.strip() != "1":
            raise AttenError(f"SETATT {v} rejected by device (reply {r!r})")
        self.last_set = v
        return v

    def get_db(self) -> float:
        r = self._value(self.t.scpi(":ATT?"))
        try:
            return float(r.split()[0])
        except (ValueError, IndexError):
            raise AttenError(f"unparseable ATT? reply {r!r}")

    def model(self) -> str:
        return self._value(self.t.scpi(":MN?"))

    def serial(self) -> str:
        return self._value(self.t.scpi(":SN?"))

    def firmware(self) -> str:
        return self._value(self.t.scpi(":FIRMWARE?"))

    def startup_att(self, db: float) -> float:
        """Power-on attenuation (persisted on the unit).  Set to max for PA work."""
        v = self.snap(db)
        r = self.t.scpi(f":STARTUPATT:VALUE:{self._fmt(v)}")
        if r.strip() != "1":
            raise AttenError(f"STARTUPATT {v} rejected (reply {r!r})")
        return v

    def status(self) -> dict:
        d = {"transport": self.t.describe(), "max_db": self.max_db, "step_db": self.step,
             "last_set": self.last_set}
        try:
            d["att_db"] = self.get_db()
        except Exception as e:
            d["att_db"] = None; d["err"] = str(e)
        return d

    def close(self) -> None:
        self.t.close()


def make_atten(spec, **kw) -> Optional[MiniCircuitsAtten]:
    """spec: 'none' | 'mock' | 'hidraw' | 'hidraw:/dev/hidraw0' | 'http://ip' | an
    rf_frontend AttenCfg.  Returns None for 'none' and for 'auto' (with auto the
    service's device_watch.AttenWatch opens the unit when it is plugged in)."""
    max_db = kw.pop("max_db", 120.0)
    step = kw.pop("step_db", 0.05)
    if spec is None:
        return None
    if not isinstance(spec, str):            # AttenCfg-like
        tr = getattr(spec, "transport", "none")
        max_db = getattr(spec, "max_db", max_db)
        step = getattr(spec, "step_db", step)
        if tr in ("none", "auto"):
            return None
        if tr == "mock":
            return MiniCircuitsAtten(MockTransport(max_db=max_db, step=step), max_db, step)
        if tr == "hidraw":
            return MiniCircuitsAtten(HidrawTransport(getattr(spec, "path", None)), max_db, step)
        if tr == "http":
            return MiniCircuitsAtten(HttpTransport(getattr(spec, "url", "")), max_db, step)
        raise AttenError(f"unknown atten transport {tr!r}")
    s = spec.strip()
    if s in ("", "none", "auto"):
        return None
    if s == "mock":
        return MiniCircuitsAtten(MockTransport(max_db=max_db, step=step), max_db, step)
    if s.startswith("hidraw"):
        path = s.split(":", 1)[1] if ":" in s else None
        return MiniCircuitsAtten(HidrawTransport(path), max_db, step)
    if s.startswith("http"):
        return MiniCircuitsAtten(HttpTransport(s), max_db, step)
    raise AttenError(f"unknown atten spec {spec!r}")


if __name__ == "__main__":                   # tiny bench helper: python3 -m dpmg.payload.mc_atten [spec] [dB]
    import sys
    a = make_atten(sys.argv[1] if len(sys.argv) > 1 else "hidraw")
    if a is None:
        print("no attenuator"); sys.exit(1)
    print("model", a.model(), "serial", a.serial(), "fw", a.firmware(), "att", a.get_db())
    if len(sys.argv) > 2:
        print("set ->", a.set_db(float(sys.argv[2])), "readback", a.get_db())
