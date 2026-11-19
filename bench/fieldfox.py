"""fieldfox.py: Keysight FieldFox (N9912A and family) spectrum analyzer over SCPI
on LAN, port 5025, for the bench scripts.

    from bench.fieldfox import FieldFox
    fx = FieldFox("192.168.10.3")
    fx.setup(center_hz=915.1e6, span_hz=200e3, rbw_hz=10e3, ref_dbm=20, atten_db=20)
    dbm, hz = fx.peak()           # one sweep, marker to the peak, (dBm, Hz)

    python3 bench/fieldfox.py --host 192.168.10.3                 # identity and settings
    python3 bench/fieldfox.py --host 192.168.10.3 --peak          # one peak reading as is
    python3 bench/fieldfox.py --host 192.168.10.3 --center 915.1e6 --span 200e3 --peak

Every command is followed by a SYST:ERR? check, so a command the firmware does
not take raises instead of silently doing nothing.  The analyzer's input
attenuation is set, never left on auto, because the sweep plans the level it
expects at the input.
"""
from __future__ import annotations

import argparse
import socket
import time
from typing import Optional, Tuple


class FieldFoxError(RuntimeError):
    pass


class FieldFox:
    def __init__(self, host: str, port: int = 5025, timeout_s: float = 10.0):
        self.host, self.port = host, int(port)
        self.s = socket.create_connection((host, self.port), timeout=timeout_s)
        self.s.settimeout(timeout_s)
        self.s.sendall(b"*CLS\n")                  # a stale error from an earlier client must not fail us
        self.idn = self.query("*IDN?")
        self._peak_cmd: Optional[str] = None
        if "N99" not in self.idn and "FieldFox" not in self.idn and "Keysight" not in self.idn \
                and "Agilent" not in self.idn:
            raise FieldFoxError(f"not a FieldFox at {host}: {self.idn!r}")

    # transport
    def write(self, cmd: str, check: bool = True) -> None:
        self.s.sendall((cmd + "\n").encode())
        if check:
            self.check(cmd)

    def query(self, cmd: str) -> str:
        self.s.sendall((cmd + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = self.s.recv(4096)
            if not chunk:
                raise FieldFoxError(f"connection closed during {cmd}")
            buf += chunk
        return buf.decode(errors="replace").strip()

    def check(self, what: str = "") -> None:
        """Raise on the first queued error, draining the rest so a later check
        does not report this one again."""
        err = self.query("SYST:ERR?")
        if err.startswith("0,") or err.startswith("+0,"):
            return
        for _ in range(8):
            more = self.query("SYST:ERR?")
            if more.startswith("0,") or more.startswith("+0,"):
                break
        raise FieldFoxError(f"{what}: {err}")

    def close(self) -> None:
        try:
            self.s.close()
        except Exception:
            pass

    # spectrum analyzer
    def sa_mode(self) -> None:
        if self.query("INST:SEL?").strip('"') != "SA":
            self.write('INST:SEL "SA"')
            self.query("*OPC?")

    def settings(self) -> dict:
        qs = {"center_hz": "SENS:FREQ:CENT?", "span_hz": "SENS:FREQ:SPAN?", "rbw_hz": "SENS:BAND:RES?",
              "vbw_hz": "SENS:BAND:VID?", "atten_db": "SENS:POW:RF:ATT?", "atten_auto": "SENS:POW:RF:ATT:AUTO?",
              "ref_dbm": "DISP:WIND:TRAC1:Y:RLEV?",
              "sweep_s": "SENS:SWE:TIME?", "continuous": "INIT:CONT?"}
        out = {}
        for k, c in qs.items():
            try:
                v = self.query(c)
                out[k] = float(v) if k not in ("atten_auto", "continuous") else int(float(v))
            except Exception as e:
                out[k] = f"? ({e})"
        return out

    def setup(self, center_hz: float, span_hz: float = 200e3, rbw_hz: Optional[float] = 10e3,
              ref_dbm: Optional[float] = 20.0, atten_db: Optional[float] = 20.0,
              preamp: Optional[bool] = False) -> None:
        """Tune for a marker reading: span around the tone, fixed input attenuation,
        preamp off, single-sweep mode.  ref_dbm None keeps the reference level."""
        self.sa_mode()
        self.write("INIT:CONT 0")
        # clear-write trace: an averaged or held trace keeps an old tone on screen
        # for many sweeps and reads as if it were still there
        self.write("TRAC1:TYPE CLRW")
        # a channel measurement (Channel Power, ACPR, occupied BW) sets its own
        # span and reads a band power, not the tone: back to the plain swept view
        if self.query("SENS:MEAS:CHAN?").strip().strip('"').upper() not in ("NONE", ""):
            self.write("SENS:MEAS:CHAN NONE")
        self.write(f"SENS:FREQ:CENT {float(center_hz):.1f}")
        self.write(f"SENS:FREQ:SPAN {float(span_hz):.1f}")
        if rbw_hz:
            self.write(f"SENS:BAND:RES {float(rbw_hz):.1f}")
        if preamp is not None:
            # not every FieldFox has a preamp (the N9912A firmware rejects the header)
            try:
                self.write(f"SENS:POW:RF:GAIN:STAT {1 if preamp else 0}")
            except FieldFoxError:
                pass
        if atten_db is not None:
            self.write("SENS:POW:RF:ATT:AUTO 0")
            self.write(f"SENS:POW:RF:ATT {float(atten_db):g}")
        if ref_dbm is not None:
            self.write(f"DISP:WIND:TRAC1:Y:RLEV {float(ref_dbm):g}")
        self.write("CALC:MARK1:ACT")

    def sweep(self) -> None:
        """One complete sweep.  Continuous mode is switched off first (INIT:IMM
        is refused while it is on) and put back by resume()."""
        if self.query("INIT:CONT?").strip() not in ("0", "+0"):
            self.write("INIT:CONT 0")
        self.write("INIT:IMM", check=False)
        self.query("*OPC?")
        self.check("INIT:IMM")

    def peak(self, sweeps: int = 1) -> Tuple[float, float]:
        """Sweep, marker to the highest point, return (dBm, Hz).  With sweeps > 1
        the highest of the readings is returned (a keyed tone is steady, the
        extra sweeps only cover a sweep that started before the key)."""
        best = None
        for _ in range(max(1, int(sweeps))):
            self.sweep()
            self._peak_search()
            y = float(self.query("CALC:MARK1:Y?"))
            x = float(self.query("CALC:MARK1:X?"))
            if best is None or y > best[0]:
                best = (y, x)
        return best

    def _peak_search(self) -> None:
        """Marker 1 to the trace maximum; the header differs between FieldFox
        firmware generations, so the first one the unit accepts is kept."""
        if self._peak_cmd:
            self.write(self._peak_cmd)
            return
        last = None
        for cmd in ("CALC:MARK1:FUNC:MAX", "CALC:MARK1:FUNC:PEAK", "CALC:MARK1:MAX", "CALC:MARK1:FUNC:PEAK:SEAR"):
            try:
                self.write(cmd)
                self._peak_cmd = cmd
                return
            except FieldFoxError as e:
                last = e
        raise FieldFoxError(f"no peak-search command accepted: {last}")

    def marker_at(self, hz: float) -> float:
        """Sweep and read the marker at one frequency (dBm)."""
        self.sweep()
        self.write(f"CALC:MARK1:X {float(hz):.1f}")
        return float(self.query("CALC:MARK1:Y?"))

    def screenshot(self, path: str, name: str = "dpmg_shot.png") -> int:
        """Save the analyzer screen to its internal memory, pull the file back
        over SCPI (definite-length block) to ``path`` and delete it on the
        instrument.  Returns the number of bytes written."""
        remote = name                      # bare name in the current directory ([INTERNAL]:)
        self.write(f'MMEM:STOR:IMAG "{remote}"', check=False)
        self.query("*OPC?")
        self.check("MMEM:STOR:IMAG")
        self.s.sendall(f'MMEM:DATA? "{remote}"\n'.encode())
        head = self.s.recv(2)
        if not head.startswith(b"#"):
            raise FieldFoxError(f"MMEM:DATA? did not return a block: {head!r}")
        nd = int(head[1:2])
        ln = int(self.s.recv(nd))
        buf = b""
        while len(buf) < ln:
            chunk = self.s.recv(min(65536, ln - len(buf)))
            if not chunk:
                raise FieldFoxError("connection closed during the screenshot transfer")
            buf += chunk
        try:
            self.s.recv(2)          # trailing newline
        except socket.timeout:
            pass
        with open(path, "wb") as f:
            f.write(buf)
        try:
            self.write(f'MMEM:DEL "{remote}"')
        except FieldFoxError:
            pass
        return len(buf)

    def resume(self) -> None:
        """Back to continuous sweeps for the operator."""
        self.write("INIT:CONT 1")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, default=5025)
    ap.add_argument("--center", type=float, default=None, help="Hz; with --span tunes the analyzer first")
    ap.add_argument("--span", type=float, default=200e3)
    ap.add_argument("--rbw", type=float, default=10e3)
    ap.add_argument("--ref", type=float, default=None, help="reference level dBm (default: keep)")
    ap.add_argument("--atten", type=float, default=None, help="input attenuation dB (default: keep)")
    ap.add_argument("--peak", action="store_true", help="one sweep and a peak reading")
    ap.add_argument("--sweeps", type=int, default=1)
    ap.add_argument("--screenshot", default=None, help="save the screen to this PNG path")
    a = ap.parse_args(argv)
    fx = FieldFox(a.host, a.port)
    print("IDN:", fx.idn)
    if a.center is not None:
        fx.setup(a.center, a.span, a.rbw, a.ref, a.atten)
        print(f"tuned: centre {a.center/1e6:.6f} MHz span {a.span/1e3:g} kHz rbw {a.rbw/1e3:g} kHz")
    for k, v in fx.settings().items():
        print(f"  {k:12s} {v}")
    if a.peak:
        t0 = time.time()
        y, x = fx.peak(a.sweeps)
        print(f"peak: {y:+.2f} dBm at {x/1e6:.6f} MHz ({time.time()-t0:.1f} s)")
        fx.resume()
    if a.screenshot:
        n = fx.screenshot(a.screenshot)
        print(f"screenshot: {a.screenshot} ({n} bytes)")
    fx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
