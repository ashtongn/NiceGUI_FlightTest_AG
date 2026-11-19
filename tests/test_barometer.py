"""dpmg/radio/barometer.py: BMP581/585 register decode, the tracker's states and
retry timing, and the radio card text."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dpmg.radio import barometer as b  # noqa: E402


class FakeDev(b.Bmp58x):
    """Register file instead of /dev/i2c."""

    def __init__(self, regs, bus=1, addr=0x47):
        self.bus, self.addr, self.fd, self.chip = bus, addr, -1, None
        self.regs = dict(regs)
        self.writes = []

    def _read(self, reg, n):
        return bytes(self.regs.get(reg + i, 0) for i in range(n))

    def _write(self, reg, val):
        self.writes.append((reg, val))

    def close(self):
        pass


def _regs(p_hpa, t_c, chip=0x50):
    p_raw = int(round(p_hpa * 100 * 64))
    t_raw = int(round(t_c * 65536)) & 0xFFFFFF
    return {0x01: chip, 0x1D: t_raw & 0xFF, 0x1E: (t_raw >> 8) & 0xFF, 0x1F: t_raw >> 16,
            0x20: p_raw & 0xFF, 0x21: (p_raw >> 8) & 0xFF, 0x22: p_raw >> 16}


def test_decode_and_configure():
    d = FakeDev(_regs(1009.37, 23.5))
    assert d.probe() == "BMP581"
    d.configure()
    assert d.writes == [(0x36, b.OSR_CONFIG), (0x37, b.ODR_CONFIG)]
    p, t = d.read()
    assert p == pytest.approx(1009.37, abs=0.01) and t == pytest.approx(23.5, abs=1e-4)


def test_negative_temperature_and_bmp585():
    d = FakeDev(_regs(850.0, -12.25, chip=0x51))
    assert d.probe() == "BMP585"
    p, t = d.read()
    assert t == pytest.approx(-12.25, abs=1e-4) and p == pytest.approx(850.0, abs=0.01)


def test_wrong_chip_id_rejected():
    with pytest.raises(OSError, match="not a BMP581"):
        FakeDev({0x01: 0x60}).probe()


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def test_tracker_not_found_retries_every_30_s():
    clk, calls = Clock(), []

    def finder():
        calls.append(clk.t)
        return None
    tr = b.BaroTracker(clock=clk)
    tr.poll(finder)
    clk.t += 5
    tr.poll(finder)
    assert calls == [100.0] and tr.summary()["state"] == "not found"
    clk.t += b.RETRY_S
    tr.poll(finder)
    assert len(calls) == 2


def test_tracker_reads_and_reports():
    clk = Clock()
    dev = FakeDev(_regs(1013.25, 20.0))
    dev.probe()
    tr = b.BaroTracker(clock=clk)
    tr.poll(lambda: dev)
    s = tr.summary()
    assert s["state"] == "ok" and s["chip"] == "BMP581" and s["addr"] == "0x47" and s["bus"] == 1
    assert s["pressure_hpa"] == pytest.approx(1013.25, abs=0.01) and abs(s["alt_m"]) < 0.5
    txt, lvl = b.card_text(s)
    assert lvl == "ok" and "1013.2 hPa" in txt and "20.0 °C" in txt


def test_tracker_read_error_drops_device_and_keeps_last():
    clk = Clock()
    dev = FakeDev(_regs(1000.0, 21.0))
    dev.probe()
    tr = b.BaroTracker(clock=clk)
    tr.poll(lambda: dev)
    dev.regs.update(_regs(0.0, 0.0))                 # out of range: treated as a read error
    clk.t += 20
    tr.poll(lambda: dev)
    s = tr.summary()
    assert s["state"] == "error" and tr.dev is None and s["pressure_hpa"] == pytest.approx(1000.0, abs=0.01)
    txt, lvl = b.card_text(s)
    assert lvl == "warm" and txt.endswith("(stale)")


def test_card_text_without_reading():
    assert b.card_text(None) == ("baro: not reported", None)
    assert b.card_text({"state": "not found"}) == ("baro: not found", None)
    assert b.card_text({"state": "error", "err": "x"}) == ("baro: error", None)


def test_sim_reading():
    tr = b.BaroTracker(clock=Clock())
    tr.set_sim()
    txt, lvl = b.card_text(tr.summary())
    assert lvl == "ok" and txt.startswith("baro 1013.2 hPa")


# INA3221 module power
from dpmg.radio import host_thermal as ht  # noqa: E402


def _hwmon(tmp_path, name, rails):
    d = tmp_path / "hwmon3"
    d.mkdir()
    (d / "name").write_text(name + "\n")
    for i, (label, mv, ma) in enumerate(rails, 1):
        (d / f"in{i}_label").write_text(label + "\n")
        (d / f"in{i}_input").write_text(f"{mv}\n")
        (d / f"curr{i}_input").write_text(f"{ma}\n")
    return str(tmp_path)


def test_ina3221_rails_and_text(tmp_path):
    root = _hwmon(tmp_path, "ina3221", [("VDD_IN", 12920, 376), ("VDD_CPU_GPU_CV", 12912, 48),
                                        ("VDD_SOC", 12912, 120)])
    p = ht.read_ina3221(root)
    assert [r["label"] for r in p["rails"]] == ["VDD_IN", "VDD_CPU_GPU_CV", "VDD_SOC"]
    assert p["in_w"] == pytest.approx(4.86, abs=0.01)
    assert ht.power_text(p) == ", module 4.9 W"


def test_ina3221_absent(tmp_path):
    assert ht.read_ina3221(_hwmon(tmp_path, "nvme", [])) is None
    assert ht.power_text(None) == "" and ht.power_text({"in_w": None}) == ""
