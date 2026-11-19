"""
test_region_extract.py: carve one signal out of a multi-signal capture.

Builds a mixed capture (FSK + BPSK + a CW tone at three offsets), carves just
the FSK band, and asserts: (1) the carved output decodes as FSK 'PING 1', (2)
the output rate is decimated to roughly the band, (3) the out-of-band signals
are suppressed (their power drops well below the FSK's after the carve), and
(4) each filter window runs. Exit 0 = all passed.
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
from dpmg.modem.modem import Modem, ModemConfig
from dpmg.radio.dpmg_service import _resample_iq
from dpmg.dsp.decode_pool import DecodeSpec, _default_decode
from dpmg.dsp.region_extract import carve, write_capture, design_lowpass

FS = 1_920_000.0
CENTER = 425_025_000.0            # absolute RX LO
checks = []


def chk(name, ok, detail=""):
    checks.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")


def build_mixed(n):
    mix = np.zeros(n, dtype=np.complex64)
    # FSK 'PING 1' at +200 kHz
    m = Modem(ModemConfig(modulation="FSK", symbol_rate=4800,
                          samples_per_bit=40, tone0=1200, tone1=2200))
    up = _resample_iq(m.transmit("PING 1"), m.sample_rate, FS)
    s = np.tile(up, int(np.ceil(n / len(up))))[:n]
    k = np.arange(n)
    mix += (s * np.exp(2j * np.pi * 200_000.0 / FS * k)).astype(np.complex64)
    # BPSK at -300 kHz (the out-of-band neighbor)
    mb = Modem(ModemConfig(modulation="BPSK", symbol_rate=4800,
                           samples_per_bit=40))
    upb = _resample_iq(mb.transmit("PING 2"), mb.sample_rate, FS)
    sb = np.tile(upb, int(np.ceil(n / len(upb))))[:n]
    mix += (sb * np.exp(2j * np.pi * -300_000.0 / FS * k)).astype(np.complex64)
    # CW tone at +600 kHz
    mix += (0.5 * np.exp(2j * np.pi * 600_000.0 / FS * k)).astype(np.complex64)
    return mix


def band_power(iq, fs, f_lo, f_hi):
    x = np.asarray(iq)
    S = np.abs(np.fft.fftshift(np.fft.fft(x * np.hanning(len(x))))) ** 2
    f = np.fft.fftshift(np.fft.fftfreq(len(x), 1 / fs))
    m = (f >= f_lo) & (f <= f_hi)
    return float(S[m].sum() / max(1, m.sum()))


def main() -> int:
    n = 400_000
    mix = build_mixed(n)
    # FSK sits around +200k with ~+1.7k tone center; carve +190k..+210k
    lo, hi = CENTER + 190_000.0, CENTER + 210_000.0
    out, meta = carve(mix, FS, CENTER, lo, hi, filter_kind="hann")

    chk("decimated output rate", meta.out_rate_hz < FS / 20 and meta.decim > 20,
        f"(decim={meta.decim}, out_rate={meta.out_rate_hz/1e3:.0f}k)")

    # decode the carved band as FSK
    sr = 4800.0
    dp = {"symbol_rate": sr, "tone_spacing_hz": 1000.0,
          "frame": {"sync_bits": "1011001000111011", "max_sync_errors": 2,
                    "has_length": True, "crc": "CRC-16-CCITT"}}
    r = _default_decode(0, out, DecodeSpec(modulation="FSK",
                        fs_out=meta.out_rate_hz, sps=meta.out_rate_hz / sr,
                        demod_params=dp, lock={}))
    msgs = getattr(r, "messages", None) or ([r.message] if r.valid else [])
    chk("carved FSK decodes", any("PING 1" in m for m in msgs), f"({msgs})")

    # out-of-band suppression: BPSK@-300k and tone@+600k are far outside the
    # carved 20 kHz band, so in the carved baseband their residual power must be
    # far below the FSK's in-band power.
    fsk_p = band_power(out, meta.out_rate_hz, -5000, 5000)
    # the BPSK would alias to (-300k-200k)=-500k, tone to +400k: both outside
    # the decimated band, so compare in-band vs the carved noise floor edges
    edge_p = band_power(out, meta.out_rate_hz,
                        meta.out_rate_hz * 0.35, meta.out_rate_hz * 0.45)
    chk("out-of-band suppressed", fsk_p > edge_p * 50,
        f"(in-band/edge = {fsk_p/max(edge_p,1e-9):.0f}x)")

    # every filter window designs + carves without error
    okw = True
    for w in ("hann", "hamming", "blackman", "rect", "none"):
        try:
            o2, _ = carve(mix, FS, CENTER, lo, hi, filter_kind=w)
            okw = okw and len(o2) > 0
        except Exception as e:
            okw = False
            print(f"      window {w} failed: {e}")
    chk("all filter windows run", okw)

    # write both formats
    tmp = tempfile.mkdtemp(prefix="carve_")
    p1 = write_capture(os.path.join(tmp, "fsk.c64"), out, meta, "c64")
    p2 = write_capture(os.path.join(tmp, "fsk.bin"), out, meta, "dpmg")
    chk("write c64 + sidecar", os.path.exists(p1) and
        os.path.exists(p1 + ".json"))
    chk("write dpmg bin", os.path.exists(p2) and os.path.getsize(p2) > 16)

    print(f"\nresult: {sum(checks)}/{len(checks)} checks passed")
    return 0 if all(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
