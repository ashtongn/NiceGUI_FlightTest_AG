"""
iq_record.py: record a stretch of live RX IQ and rebuild it for TX replay.

Used by the radio service's record_iq / replay_recording commands (operator
console "Record & replay" panel) and testable offline.

Record:
  * full band: the RX samples as received, at the radio rate.
  * selected band: the band [f_lo, f_hi] mixed to DC, filtered and decimated
    (two stages: scipy resample_poly with a Kaiser window, then a lowpass at the
    band's own rate), so the radio's DC spur / LO leakage and anything else
    outside the box stays out of the file. Same file layout as region_extract
    (raw complex64 + <file>.json sidecar holding the CarveMeta fields), so a
    recording also opens in IQ playback.

Replay:
  * back to the radio rate (rational resample when the rate changed), mixed to
    the band's RF offset from the TX LO, so it radiates where it was received;
  * a gain offset in dB relative to as captured (0 dB = same digital level, RX
    dBFS equals TX dBFS), with the digital gain capped so the peak stays below DAC
    full scale. Larger level changes go through TX attenuation.

"Match received power" is not available: it needs the TX output power at
0 dBFS per attenuation step and frequency, and an RX dBFS to dBm calibration
per gain index and port. Neither has been measured. match_rx_gain_db() is the
hook: it returns None until such a calibration is supplied.

Memory/disk: 30.72 MSps complex64 is ~245 MB/s, so the record length is capped
by REC_MAX_SAMPLES (and REC_MAX_S at low rates), and the recordings directory
keeps the newest REC_KEEP files within REC_MAX_TOTAL_BYTES.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from dataclasses import asdict
from fractions import Fraction

import numpy as np

from dpmg.dsp.region_extract import CarveMeta

REC_MAX_S = 5.0                      # longest recording at low rates
REC_MAX_SAMPLES = 1 << 25            # 33.5 M samples = 268 MB c64 (~1.09 s at 30.72 MSps)
REC_MIN_S = 0.01
REC_KEEP = 6                         # newest recordings kept on the radio
REC_MAX_TOTAL_BYTES = 1_500_000_000  # and at most this much disk between them
REC_DISK_MARGIN = 1_000_000_000      # free space left on the radio after a recording
REC_SUBDIR = "recordings"
REC_EXT = ".c64"
BAND_OVERSAMPLE = 2.0                # band file rate >= 2 x the band width
PEAK_LIMIT_DBFS = -0.1               # replay peak never above this (DAC full scale = 0 dBFS)
GAIN_MIN_DB = -60.0
GAIN_MAX_DB = 40.0                   # absolute ceiling, before the peak cap
_MIX_CHUNK = 1 << 20

_NAME_OK = re.compile(r"[^A-Za-z0-9_.-]+")


# limits

def max_seconds(fs: float) -> float:
    """Longest recording allowed at sample rate fs (s), rounded down to 10 ms."""
    if not fs or fs <= 0:
        return REC_MAX_S
    s = min(REC_MAX_S, REC_MAX_SAMPLES / float(fs))
    return max(REC_MIN_S, math.floor(s * 100.0) / 100.0)


def clamp_seconds(seconds, fs: float) -> float:
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        s = 1.0
    if not math.isfinite(s):
        s = 1.0
    return min(max(REC_MIN_S, s), max_seconds(fs))


def level_dbfs(x) -> tuple:
    """(peak dBFS, rms dBFS) of complex samples. Full scale is 1.0 per
    component: the ADC and DAC (skiq_bridge TX_FULL_SCALE) clip I and Q
    separately, so the peak is the largest |I| or |Q|, and RMS is of |x|."""
    x = np.asarray(x)
    if x.size == 0:
        return -200.0, -200.0
    pk = float(max(np.max(np.abs(x.real)), np.max(np.abs(x.imag))))
    rms = float(np.sqrt(np.mean(np.abs(x).astype(np.float64) ** 2)))
    return (20.0 * math.log10(max(pk, 1e-10)), 20.0 * math.log10(max(rms, 1e-10)))


# DSP

def _mix(x, f_norm: float) -> np.ndarray:
    """x * exp(j 2 pi f_norm n), chunked (no full-length float64 index array),
    phase continuous across chunks. f_norm = Hz / fs."""
    x = np.asarray(x, dtype=np.complex64)
    if f_norm == 0.0:
        return x.copy()
    out = np.empty_like(x)
    w = 2.0 * np.pi * float(f_norm)
    k = np.arange(_MIX_CHUNK, dtype=np.float64)
    for i in range(0, len(x), _MIX_CHUNK):
        n = min(_MIX_CHUNK, len(x) - i)
        ph = w * (i + k[:n])
        ph = np.mod(ph, 2.0 * np.pi)
        out[i:i + n] = x[i:i + n] * np.exp(1j * ph).astype(np.complex64)
    return out


def _resample(x, fs_in: float, fs_out: float) -> np.ndarray:
    import scipy.signal as ss
    fr = Fraction(float(fs_out) / float(fs_in)).limit_denominator(20000)
    if fr.numerator == fr.denominator:
        return np.asarray(x, dtype=np.complex64)
    return ss.resample_poly(np.asarray(x, dtype=np.complex64), fr.numerator,
                            fr.denominator, window=("kaiser", 8.0)).astype(np.complex64)


def full_meta(n: int, fs: float, center_hz: float) -> CarveMeta:
    return CarveMeta(src_center_hz=float(center_hz), band_center_hz=float(center_hz),
                     band_bw_hz=float(fs), in_rate_hz=float(fs), out_rate_hz=float(fs),
                     decim=1, filter="none", n_out=int(n))


def band_record(iq, fs: float, center_hz: float, f_lo_hz: float, f_hi_hz: float):
    """Selected-band recording: mix [f_lo, f_hi] (absolute Hz) to DC, decimate
    to >= BAND_OVERSAMPLE x its width, lowpass at the band edge. Returns
    (out complex64, CarveMeta). Raises ValueError if the band is empty or not
    inside the captured span."""
    import scipy.signal as ss
    f_lo, f_hi = float(min(f_lo_hz, f_hi_hz)), float(max(f_lo_hz, f_hi_hz))
    bw = f_hi - f_lo
    if bw <= 0:
        raise ValueError("selected band is empty (f_hi must exceed f_lo)")
    half = 0.5 * float(fs)
    if f_lo < center_hz - half - 1.0 or f_hi > center_hz + half + 1.0:
        raise ValueError(f"band {f_lo/1e6:.4f}-{f_hi/1e6:.4f} MHz is outside the "
                         f"received span {(center_hz-half)/1e6:.4f}-"
                         f"{(center_hz+half)/1e6:.4f} MHz")
    band_center = 0.5 * (f_lo + f_hi)
    x = _mix(iq, -(band_center - center_hz) / float(fs))
    decim = max(1, int(fs / (bw * BAND_OVERSAMPLE)))
    if decim > 1:
        x = ss.resample_poly(x, 1, decim, window=("kaiser", 8.0)).astype(np.complex64)
    out_rate = float(fs) / decim
    # second stage at the band's own rate: passband to bw/2, stop by ~0.3 x rate
    if bw < 0.95 * out_rate:
        h = ss.firwin(127, 0.5 * bw, fs=out_rate, window=("kaiser", 8.0))
        x = np.convolve(x, h.astype(np.float32), mode="same").astype(np.complex64)
    meta = CarveMeta(src_center_hz=float(center_hz), band_center_hz=float(band_center),
                     band_bw_hz=float(bw), in_rate_hz=float(fs), out_rate_hz=out_rate,
                     decim=int(decim), filter="kaiser2", n_out=int(len(x)))
    return x, meta


def rebuild_for_tx(x, meta: dict, fs_tx: float, tx_lo_hz: float):
    """Turn a recording back into a TX buffer at the radio rate fs_tx, placed at
    the band's RF centre relative to the TX LO. Returns (wave complex64, info).
    Raises ValueError when the recording does not fit the TX span."""
    rate = float(meta.get("out_rate_hz") or meta.get("rate_hz") or 0)
    if rate <= 0:
        raise ValueError("recording has no sample rate")
    band_center = float(meta.get("band_center_hz") or meta.get("src_center_hz") or tx_lo_hz)
    full = meta.get("mode") == "full"
    bw = rate if full else float(meta.get("band_bw_hz") or rate)
    offset = band_center - float(tx_lo_hz)
    warnings = []
    half = 0.5 * float(fs_tx)
    if full:
        # a full-band recording only lines up when the TX LO is the capture centre;
        # a small offset rotates the band edges round (they are the RX filter skirt)
        if abs(offset) > 0.05 * float(fs_tx):
            raise ValueError(f"full-band recording was taken at "
                             f"{band_center/1e6:.6f} MHz but the TX LO is "
                             f"{tx_lo_hz/1e6:.6f} MHz; retune, or record a selected band")
        if abs(offset) > 1.0:
            warnings.append(f"TX LO is {offset/1e3:+.1f} kHz from the capture centre; "
                            "the band edges wrap round")
        if abs(rate - fs_tx) > 1.0:
            warnings.append(f"recorded at {rate/1e6:g} MSps, replayed at "
                            f"{fs_tx/1e6:g} MSps (resampled)")
    elif abs(offset) + 0.5 * bw > half:
        raise ValueError(f"band {band_center/1e6:.4f} MHz ({bw/1e3:.1f} kHz) does not "
                         f"fit the TX span {(tx_lo_hz-half)/1e6:.4f}-"
                         f"{(tx_lo_hz+half)/1e6:.4f} MHz")
    resampled = abs(rate - fs_tx) > 1.0
    y = _resample(x, rate, fs_tx) if resampled else np.asarray(x, dtype=np.complex64)
    if offset and not (full and abs(offset) <= 1.0):
        y = _mix(y, offset / float(fs_tx))
    info = {"offset_hz": float(offset), "resampled": bool(resampled),
            "n": int(len(y)), "seconds": float(len(y) / float(fs_tx)),
            "warnings": warnings}
    return y.astype(np.complex64, copy=False), info


def gain_limits(peak_dbfs: float) -> float:
    """Largest digital gain (dB) that keeps the replay peak at PEAK_LIMIT_DBFS."""
    return min(GAIN_MAX_DB, PEAK_LIMIT_DBFS - float(peak_dbfs))


def apply_gain(wave, gain_db: float):
    """Scale wave by gain_db, capped so the peak stays below DAC full scale.
    Returns (scaled complex64, info)."""
    try:
        g = float(gain_db)
    except (TypeError, ValueError):
        g = 0.0
    if not math.isfinite(g):
        g = 0.0
    pk_in, _ = level_dbfs(wave)
    gmax = gain_limits(pk_in)
    applied = max(GAIN_MIN_DB, min(g, gmax))
    capped = applied < g - 1e-9
    y = (np.asarray(wave, dtype=np.complex64) * np.float32(10.0 ** (applied / 20.0)))
    pk_out, rms_out = level_dbfs(y)
    return y.astype(np.complex64, copy=False), {
        "gain_req_db": round(g, 2), "gain_db": round(applied, 2),
        "gain_max_db": round(gmax, 2), "capped": bool(capped),
        "peak_in_dbfs": round(pk_in, 2), "peak_dbfs": round(pk_out, 2),
        "rms_dbfs": round(rms_out, 2)}


def match_rx_gain_db(meta: dict, cal=None):
    """Hook for a later "match received power" mode: the gain offset (dB) that
    makes the radiated level equal the level received at capture time.

    It needs, per frequency: the TX output power at 0 dBFS for the TX
    attenuation in use, and the RX dBFS to dBm offset for the RX gain index and
    port in the recording's sidecar (rx_gain_index, rx_port). Those have not
    been measured, so with cal=None this returns None and the service refuses
    the mode. Do not fill this in with nominal numbers."""
    if not cal:
        return None
    raise NotImplementedError("match received power: calibration format not defined yet")


# files

def safe_label(label) -> str:
    s = _NAME_OK.sub("_", str(label or "")).strip("._")
    return s[:16]


def new_name(label="", now=None) -> str:
    t = time.strftime("%Y%m%d_%H%M%S", time.gmtime(now or time.time()))
    lab = safe_label(label)
    return f"rec_{t}" + (f"_{lab}" if lab else "")


def rec_path(dirpath: str, name: str) -> str:
    base = str(name)
    if os.path.basename(base) != base or "/" in base or "\\" in base:
        raise ValueError(f"bad recording name '{name}'")
    if base.endswith(REC_EXT):
        base = base[:-len(REC_EXT)]
    if not base or base.startswith(".") or base != safe_name(base):
        raise ValueError(f"bad recording name '{name}'")
    return os.path.join(dirpath, base + REC_EXT)


def safe_name(name: str) -> str:
    return _NAME_OK.sub("_", str(name))


def write_recording(dirpath: str, name: str, iq, meta: CarveMeta, extra: dict) -> dict:
    """Write <name>.c64 + <name>.c64.json (CarveMeta fields + extra). Returns
    the sidecar dict."""
    path = rec_path(dirpath, name)
    iq = np.asarray(iq, dtype=np.complex64)
    iq.tofile(path)
    pk, rms = level_dbfs(iq)
    side = asdict(meta)
    side.update(extra or {})
    side.update({"name": os.path.basename(path)[:-len(REC_EXT)], "kind": "record",
                 "rate_hz": float(meta.out_rate_hz), "n": int(len(iq)),
                 "seconds": float(len(iq) / float(meta.out_rate_hz)),
                 "size_bytes": int(os.path.getsize(path)),
                 "peak_dbfs": round(pk, 2), "rms_dbfs": round(rms, 2),
                 "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    with open(path + ".json", "w") as f:
        json.dump(side, f, indent=1)
    return side


def read_meta(dirpath: str, name: str) -> dict:
    path = rec_path(dirpath, name)
    if not os.path.exists(path):
        raise FileNotFoundError(f"no recording '{name}'")
    with open(path + ".json") as f:
        return json.load(f)


def load_recording(dirpath: str, name: str):
    meta = read_meta(dirpath, name)
    x = np.fromfile(rec_path(dirpath, name), dtype=np.complex64)
    return x, meta


def list_recordings(dirpath: str) -> list:
    """Sidecars of every recording, newest first."""
    out = []
    try:
        names = os.listdir(dirpath)
    except FileNotFoundError:
        return out
    for fn in names:
        if not fn.endswith(REC_EXT):
            continue
        p = os.path.join(dirpath, fn)
        try:
            with open(p + ".json") as f:
                m = json.load(f)
            m["size_bytes"] = int(os.path.getsize(p))
            m["_mtime"] = os.path.getmtime(p)
        except (OSError, ValueError):
            m = {"name": fn[:-len(REC_EXT)], "size_bytes": int(os.path.getsize(p)),
                 "_mtime": os.path.getmtime(p), "broken": True}
        out.append(m)
    out.sort(key=lambda m: m.get("_mtime", 0), reverse=True)
    return out


def delete_recording(dirpath: str, name: str) -> bool:
    path = rec_path(dirpath, name)
    found = False
    for p in (path, path + ".json"):
        try:
            os.remove(p)
            found = True
        except FileNotFoundError:
            pass
    return found


def prune(dirpath: str, keep=None, max_bytes=None, protect=()) -> list:
    """Remove the oldest recordings beyond `keep` files or `max_bytes` total.
    Names in `protect` (the new one, the one armed for TX) are never removed.
    Returns the removed names."""
    keep = REC_KEEP if keep is None else int(keep)
    max_bytes = REC_MAX_TOTAL_BYTES if max_bytes is None else int(max_bytes)
    protect = {str(p) for p in protect if p}
    recs = list_recordings(dirpath)
    removed, total, kept = [], 0, 0
    for m in recs:                                   # newest first
        nm = m.get("name")
        sz = int(m.get("size_bytes") or 0)
        if nm in protect or (kept < keep and total + sz <= max_bytes):
            kept += 1
            total += sz
            continue
        if delete_recording(dirpath, nm):
            removed.append(nm)
    return removed


def brief(m: dict) -> dict:
    """Compact form for a CMD_ACK (the whole ack must stay < 1400 B)."""
    return {"n": m.get("name"), "s": round(float(m.get("seconds") or 0), 3),
            "r": float(m.get("rate_hz") or 0), "b": int(m.get("size_bytes") or 0),
            "p": m.get("peak_dbfs"), "m": m.get("mode", "?"),
            "c": m.get("band_center_hz"),
            "w": (None if m.get("mode") == "full" else m.get("band_bw_hz"))}
