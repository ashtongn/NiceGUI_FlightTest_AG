"""
region_extract.py: carve a frequency band out of a wideband IQ capture.

A PSD region-select on the GUI names a [f_lo, f_hi] band; this mixes that band
to baseband, filters it with a selectable lowpass, and decimates to the band's
own rate: so the saved file holds one signal, not the whole span. Used by the
service's extract_region command and reusable offline (tests, replay).

Stdlib + numpy. Filter is a windowed-sinc FIR (window picks the sidelobe/roll
tradeoff); "none" passes the mixed band through undecimated-lowpass-free.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from typing import Optional

import numpy as np

_WINDOWS = {
    "hann": np.hanning,
    "hamming": np.hamming,
    "blackman": np.blackman,
    "rect": (lambda n: np.ones(n)),
}


def design_lowpass(fs: float, cutoff_hz: float, kind: str = "hann",
                   ntaps: int = 129) -> Optional[np.ndarray]:
    """Windowed-sinc lowpass FIR at `cutoff_hz`. kind='none': None (no filter)."""
    if kind == "none":
        return None
    win = _WINDOWS.get(kind, np.hanning)
    ntaps = int(ntaps) | 1                       # force odd for a symmetric FIR
    fc = max(1e-6, min(0.499, cutoff_hz / fs))   # normalized cutoff
    t = np.arange(ntaps) - (ntaps - 1) / 2.0
    h = 2 * fc * np.sinc(2 * fc * t) * win(ntaps)
    return (h / np.sum(h)).astype(np.float64)


@dataclass
class CarveMeta:
    src_center_hz: float           # RX LO the capture was taken at
    band_center_hz: float          # absolute center of the carved band
    band_bw_hz: float              # requested passband width
    in_rate_hz: float
    out_rate_hz: float
    decim: int
    filter: str
    n_out: int


def carve(iq: np.ndarray, fs: float, src_center_hz: float,
          band_lo_hz: float, band_hi_hz: float, filter_kind: str = "hann",
          oversample: float = 1.25, ntaps: int = 129):
    """Extract the absolute band [band_lo_hz, band_hi_hz] from `iq` (captured at
    RX LO src_center_hz, rate fs). Returns (out_iq complex64, CarveMeta)."""
    x = np.asarray(iq, dtype=np.complex64)
    bw = float(band_hi_hz - band_lo_hz)
    if bw <= 0:
        raise ValueError("band_hi must exceed band_lo")
    band_center = 0.5 * (band_lo_hz + band_hi_hz)
    off = band_center - src_center_hz            # shift the band to DC
    n = np.arange(len(x))
    mixed = x * np.exp(-2j * np.pi * off / fs * n).astype(np.complex64)

    h = design_lowpass(fs, 0.5 * bw, filter_kind, ntaps)
    if h is not None:
        mixed = np.convolve(mixed, h, mode="same").astype(np.complex64)

    # decimate so the output keeps ~oversample x the band; >=1 always
    decim = max(1, int(fs / (bw * float(oversample)))) if bw > 0 else 1
    out = mixed[::decim].astype(np.complex64)
    meta = CarveMeta(src_center_hz=float(src_center_hz),
                     band_center_hz=float(band_center), band_bw_hz=bw,
                     in_rate_hz=float(fs), out_rate_hz=float(fs / decim),
                     decim=int(decim), filter=filter_kind, n_out=int(len(out)))
    return out, meta


def write_capture(path: str, iq: np.ndarray, meta: CarveMeta,
                  fmt: str = "c64") -> str:
    """Write the carved IQ. fmt 'c64': raw complex64 + .json sidecar;
    'dpmg': the DPMG BE-IQ16 .bin the encoder/IQ-Validator read."""
    iq = np.asarray(iq, dtype=np.complex64)
    if fmt == "dpmg":
        import struct
        i16 = np.clip(np.round(iq.real * 32767.0), -32768, 32767).astype(">i2")
        q16 = np.clip(np.round(iq.imag * 32767.0), -32768, 32767).astype(">i2")
        inter = np.empty(2 * len(iq), dtype=">i2")
        inter[0::2] = i16
        inter[1::2] = q16
        # magic 'DPMG', ver 1, fmt BE_IQ16(2), reserved, rate, header size 16
        header = struct.pack(">4sBBHII", b"DPMG", 1, 2, 0,
                             int(round(meta.out_rate_hz)), 16)
        with open(path, "wb") as f:
            f.write(header)
            f.write(inter.tobytes())
    else:
        iq.tofile(path)
        with open(path + ".json", "w") as f:
            json.dump(asdict(meta), f, indent=1)
    return path
