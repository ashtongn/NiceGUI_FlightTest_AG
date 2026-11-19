"""barometer.py: Bosch BMP581 / BMP585 barometer on the radio host's I2C bus
(the DFRobot Gravity SEN0664 on the payload, powered from the G40 3.3 V).

Plain Linux i2c-dev access (ioctl I2C_SLAVE, then write the register and read
the bytes), so no smbus2 is needed on the radio.  find() looks for the chip on
every /dev/i2c-* at 0x47 then 0x46 (DPMG_BARO_BUS / DPMG_BARO_ADDR pin it).

Registers (BMP581 datasheet): CHIP_ID 0x01 (0x50 BMP581, 0x51 BMP585),
TEMP_DATA 0x1D..0x1F (signed 24 bit, /65536 = deg C), PRESS_DATA 0x20..0x22
(unsigned 24 bit, /64 = Pa), OSR_CONFIG 0x36 (bit 6 press_en, bits 5:3 osr_p,
bits 2:0 osr_t), ODR_CONFIG 0x37 (bits 1:0 power mode, 01 = normal; bits 6:2
data rate; bit 7 deep standby off), CMD 0x7E.
"""
from __future__ import annotations

import fcntl
import glob
import os
import time
from typing import Optional

I2C_SLAVE = 0x0703
REG_CHIP_ID = 0x01
REG_TEMP = 0x1D
REG_OSR = 0x36
REG_ODR = 0x37
CHIP_IDS = {0x50: "BMP581", 0x51: "BMP585"}
ADDRS = (0x47, 0x46)
OSR_CONFIG = 0x40 | (3 << 3) | 1        # pressure on, pressure x8, temperature x2
ODR_CONFIG = 0x80 | (0x1C << 2) | 0x01  # deep standby off, 1 Hz, normal mode
POLL_S = 5.0
RETRY_S = 30.0
SEA_LEVEL_HPA = 1013.25


class Bmp58x:
    """One BMP581/585 on /dev/i2c-<bus> at <addr>."""

    def __init__(self, bus: int, addr: int, opener=os.open):
        self.bus, self.addr = int(bus), int(addr)
        self.fd = opener(f"/dev/i2c-{self.bus}", os.O_RDWR)
        fcntl.ioctl(self.fd, I2C_SLAVE, self.addr)
        self.chip = None

    def _read(self, reg: int, n: int) -> bytes:
        os.write(self.fd, bytes([reg]))
        data = os.read(self.fd, n)
        if len(data) != n:
            raise OSError(f"short read {len(data)} of {n} at 0x{reg:02x}")
        return data

    def _write(self, reg: int, val: int) -> None:
        os.write(self.fd, bytes([reg, val & 0xFF]))

    def probe(self) -> str:
        cid = self._read(REG_CHIP_ID, 1)[0]
        if cid not in CHIP_IDS:
            raise OSError(f"chip id 0x{cid:02x} at 0x{self.addr:02x} is not a BMP581/585")
        self.chip = CHIP_IDS[cid]
        return self.chip

    def configure(self) -> None:
        self._write(REG_OSR, OSR_CONFIG)
        self._write(REG_ODR, ODR_CONFIG)

    def read(self) -> tuple:
        """(pressure hPa, temperature deg C)."""
        d = self._read(REG_TEMP, 6)
        t_raw = d[0] | (d[1] << 8) | (d[2] << 16)
        if t_raw & 0x800000:
            t_raw -= 1 << 24
        p_raw = d[3] | (d[4] << 8) | (d[5] << 16)
        return p_raw / 64.0 / 100.0, t_raw / 65536.0

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass


def _buses() -> list:
    env = os.environ.get("DPMG_BARO_BUS")
    if env:
        return [int(env)]
    out = []
    for p in sorted(glob.glob("/dev/i2c-*")):
        try:
            out.append(int(p.rsplit("-", 1)[1]))
        except ValueError:
            pass
    return out


def find(opener=os.open, buses=None) -> Optional[Bmp58x]:
    """The first BMP581/585 found, configured and ready, or None."""
    env_addr = os.environ.get("DPMG_BARO_ADDR")
    addrs = (int(env_addr, 0),) if env_addr else ADDRS
    for bus in (buses if buses is not None else _buses()):
        for addr in addrs:
            dev = None
            try:
                dev = Bmp58x(bus, addr, opener)
                dev.probe()
                dev.configure()
                return dev
            except OSError:
                if dev is not None:
                    dev.close()
    return None


def altitude_m(p_hpa: float, sea_level_hpa: float = SEA_LEVEL_HPA) -> float:
    """Pressure altitude, standard atmosphere."""
    return 44330.0 * (1.0 - (p_hpa / sea_level_hpa) ** (1.0 / 5.255))


class BaroTracker:
    """Last reading and state for get_config 'baro'."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self.dev = None
        self.state = "not found"
        self.err: Optional[str] = None
        self.last: Optional[tuple] = None        # (hPa, deg C, monotonic time)
        self.next_find = 0.0

    def poll(self, finder=find) -> None:
        """One poll: find the chip if needed (every RETRY_S), then read it."""
        now = self._clock()
        if self.dev is None:
            if now < self.next_find:
                return
            self.next_find = now + RETRY_S
            self.dev = finder()
            if self.dev is None:
                self.state = "not found"
                return
        try:
            p, t = self.dev.read()
            if not 300.0 <= p <= 1250.0:         # a powering-up chip reads 0 first
                raise OSError(f"pressure {p:.1f} hPa out of range")
            self.last = (p, t, now)
            self.state, self.err = "ok", None
        except OSError as e:
            self.err = str(e)[:80]
            self.state = "error"
            self.dev.close()
            self.dev = None

    def set_sim(self, p_hpa: float = 1013.2, t_c: float = 24.0) -> None:
        self.last = (p_hpa, t_c, self._clock())
        self.state, self.err = "ok", None

    def summary(self) -> dict:
        d = {"state": self.state}
        if self.dev is not None:
            d.update(bus=self.dev.bus, addr=f"0x{self.dev.addr:02x}", chip=self.dev.chip)
        if self.last is not None:
            p, t, at = self.last
            d.update(pressure_hpa=round(p, 2), temp_c=round(t, 2),
                     alt_m=round(altitude_m(p), 1), age_s=round(self._clock() - at, 1))
        if self.err:
            d["err"] = self.err
        return d


def card_text(baro) -> tuple:
    """(text, level) for the radio cards from get_config 'baro'.  Level None
    (grey) when there is nothing current, 'warm' (amber) for a stale reading."""
    if not baro:
        return "baro: not reported", None
    if baro.get("pressure_hpa") is None:
        return ("baro: not found" if baro.get("state") == "not found"
                else f"baro: {baro.get('state', '?')}"), None
    txt = (f"baro {baro['pressure_hpa']:.1f} hPa, {baro['temp_c']:.1f} °C, "
           f"alt {baro.get('alt_m', 0):.0f} m")
    if baro.get("state") != "ok" or baro.get("age_s", 0) > 3 * POLL_S:
        return txt + " (stale)", "warm"
    return txt, "ok"
