"""
read_cdif tail test: the reader must stop at the HCB data_size, not at EOF.
==========================================================================
Sceptre/SignalVu .cdif files put the extended keyword block after the sample
data. A read-to-EOF returns those keyword bytes as junk samples
(scenarios/single_freq.cdif: 5,357,824 samples instead of 5,357,568, the last
256 with |x| up to 1.3e35).

  1. synthetic BLUE file, keyword block appended after the data (CF and CI)
  2. write_cdif layout (keywords before the data) still reads back exactly
  3. max_samples smaller / larger than n_samples
  4. scenarios/single_freq.cdif: 5,357,568 samples, max |x| < 1e-3

Exit 0 = all passed.   python3 tests/test_cdif_reader_tail.py
"""

from __future__ import annotations

import os
import struct
import sys
import tempfile

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from dpmg.dsp import cdif_reader
from dpmg.dsp.cdif_reader import HCB_SIZE, _kw_entry

SINGLE_FREQ = os.path.join(ROOT, "scenarios", "single_freq.cdif")


def _write_tail_layout(path, iq, rate, fmt="CF"):
    """HCB + data + keyword block (Sceptre layout: ext_start past the data)."""
    if fmt == "CI":
        scale = float(np.max(np.abs(np.concatenate([iq.real, iq.imag])))) / 32767.0
        inter = np.empty(iq.size * 2)
        inter[0::2], inter[1::2] = iq.real, iq.imag
        data = np.round(inter / scale).astype("<i2").tobytes()
    else:
        scale = 1.0
        inter = np.empty(iq.size * 2, dtype="<f4")
        inter[0::2], inter[1::2] = iq.real, iq.imag
        data = inter.tobytes()
    ext = _kw_entry("RF_FREQ", "D", struct.pack("<d", 915e6))
    ext += _kw_entry("RSA_DATA_SCALE", "D", struct.pack("<d", scale))
    # a value that decodes as a huge float32 if mistaken for samples
    ext += _kw_entry("JUNK", "D", struct.pack("<d", 1e300))
    ext += b"\x7f\x7f\x7f\x7f" * 64
    data_start = HCB_SIZE
    pad = (-(data_start + len(data))) % 512        # ext_start is in 512-B blocks
    ext_start = (data_start + len(data) + pad) // 512
    hcb = bytearray(HCB_SIZE)
    hcb[0:4] = b"BLUE"
    hcb[4:8] = b"EEEI"
    hcb[8:12] = b"EEEI"
    struct.pack_into("<ii", hcb, 24, ext_start, len(ext))
    struct.pack_into("<dd", hcb, 32, float(data_start), float(len(data)))
    struct.pack_into("<i", hcb, 48, 1001)
    hcb[52:54] = fmt.encode("latin1")
    struct.pack_into("<dd", hcb, 256, 0.0, 1.0 / rate)
    with open(path, "wb") as f:
        f.write(hcb)
        f.write(data)
        f.write(b"\x00" * pad)
        f.write(ext)


def _tone(n, rate=1e6, f0=12.5e3, amp=1e-4):
    t = np.arange(n) / rate
    return (amp * np.exp(2j * np.pi * f0 * t)).astype(np.complex64)


def test_tail_keywords_cf():
    iq = _tone(1000)
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "tail.cdif")
        _write_tail_layout(p, iq, 1e6, "CF")
        out, meta = cdif_reader.read_cdif(p)
    assert meta.n_samples == 1000, meta.n_samples
    assert out.size == 1000, out.size
    assert np.allclose(out, iq), "samples differ"
    assert float(np.max(np.abs(out))) < 1e-3
    assert abs(meta.center_freq_hz - 915e6) < 1


def test_tail_keywords_ci():
    iq = _tone(777, amp=0.5)
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "tail_ci.cdif")
        _write_tail_layout(p, iq, 1e6, "CI")
        out, meta = cdif_reader.read_cdif(p)
    assert out.size == 777, out.size
    assert float(np.max(np.abs(out - iq))) < 1e-4


def test_write_cdif_layout_roundtrip():
    iq = _tone(4096, amp=0.3)
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "w.cdif")
        cdif_reader.write_cdif(p, iq, 1e6, 915e6, keywords={"NOTE": "x"})
        out, meta = cdif_reader.read_cdif(p)
    assert out.size == 4096 and np.array_equal(out, iq)


def test_max_samples():
    iq = _tone(1000)
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "tail.cdif")
        _write_tail_layout(p, iq, 1e6, "CF")
        small, _ = cdif_reader.read_cdif(p, max_samples=100)
        big, _ = cdif_reader.read_cdif(p, max_samples=10**6)
    assert small.size == 100 and np.allclose(small, iq[:100])
    assert big.size == 1000, big.size              # capped at n_samples, no junk


def test_single_freq_cdif():
    if not os.path.exists(SINGLE_FREQ):
        print("  (skip: scenarios/single_freq.cdif not present)")
        return
    out, meta = cdif_reader.read_cdif(SINGLE_FREQ)
    assert meta.n_samples == 5_357_568, meta.n_samples
    assert out.size == 5_357_568, out.size
    peak = float(np.max(np.abs(out)))
    assert np.isfinite(peak) and peak < 1e-3, peak


def main():
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    fails = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:
            fails += 1
            print(f"FAIL {t.__name__}: {e!r}")
    print(f"{len(tests) - fails}/{len(tests)} passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
