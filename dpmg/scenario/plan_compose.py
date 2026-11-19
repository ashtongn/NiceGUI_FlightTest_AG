"""
plan_compose.py: render a loaded channel_plan into one wideband IQ block for the
loopback self-test. Each entry's bits are modulated by the encoder's get_modulator
(same code the encoder tab uses: no drift), rendered at the entry's native rate,
resampled to the compose rate, shifted to its offset, and summed. The result is fed
to loopback_source, injecting the plan's own signal into the worker that's watching it.
"""
from __future__ import annotations
import numpy as np


def _resample(iq, fs_in, fs_out):
    if abs(fs_in - fs_out) < 1.0:
        return iq
    from math import gcd
    g = gcd(int(round(fs_in)), int(round(fs_out)))
    up = int(round(fs_out)) // g
    down = int(round(fs_in)) // g
    try:
        from scipy.signal import resample_poly
        return resample_poly(iq, up, down).astype(np.complex64)
    except Exception:
        # fallback: linear interp (fine for a loopback self-test)
        n_out = int(round(len(iq) * fs_out / fs_in))
        xi = np.linspace(0, len(iq) - 1, n_out)
        i0 = xi.astype(int); mu = xi - i0
        i0 = np.clip(i0, 0, len(iq) - 2)
        return (iq[i0] * (1 - mu) + iq[i0 + 1] * mu).astype(np.complex64)


def render_entry(entry):
    """Modulate one entry's bits at its native rate via the encoder's modulator.
    Returns (iq_native, fs_native) or (None, 0) if not renderable."""
    from dpmg.modulation.modulator import get_modulator
    p = entry.params or {}
    sr = float(p.get("symbol_rate", 0) or 0)
    spb = int(p.get("samples_per_bit", 8) or 8)
    if sr <= 0 or not entry.bits:
        return None, 0.0
    fs_native = sr * spb
    kw = {}
    if p.get("dibit_mapping"): kw["dibit_mapping"] = p["dibit_mapping"]
    # get_modulator takes tone0..tone3 as separate baseband-Hz kwargs (not a "tones" list).
    # tone_deltas_hz are already baseband deltas around DC: map straight to tone0..3.
    deltas = p.get("tone_deltas_hz")
    if deltas:
        for i, t in enumerate(deltas[:4]):
            kw[f"tone{i}"] = float(t)
    if p.get("gfsk_bw_hz"): kw["gfsk_bw_hz"] = float(p["gfsk_bw_hz"])
    # Frame auto-wrap: if the entry declares a frame with auto_wrap, wrap the
    # payload bits in sync[+len]+payload[+crc] so the transmitted signal contains exactly
    # what the channelizer will trigger on. Otherwise modulate the bits verbatim.
    tx_bits = entry.bits
    fr = getattr(entry, "frame", None) or {}
    if fr.get("sync") and fr.get("auto_wrap", True):
        try:
            from dpmg.framing import frame_codec as _fc
            spec = _fc.FrameSpec.from_entry(fr)
            tx_bits = _fc.build_frame(entry.bits, spec)
        except Exception:
            tx_bits = entry.bits
    mod = get_modulator(entry.modulation, symbol_rate=sr, samples_per_bit=spb, **kw)
    res = mod.modulate(tx_bits)
    # ModulationResult carries the IQ + the achieved sample rate (authoritative)
    iq = np.asarray(res.iq_samples, dtype=np.complex64)
    fs_native = float(getattr(res, "sample_rate", fs_native) or fs_native)
    return iq, fs_native


def occupied_bw(iq, fs, frac=0.99, max_fft=262144):
    """Measured occupied bandwidth of a complex baseband signal.
    PSD via FFT, greedy expansion from the peak bin until `frac` of the
    total power is enclosed. Returns (bw_hz, centroid_hz)."""
    N = int(min(max_fft, len(iq)))
    if N < 8:
        return 0.0, 0.0
    X = np.abs(np.fft.fftshift(np.fft.fft(iq[:N]))) ** 2
    tp = float(X.sum())
    if tp <= 0.0:
        return 0.0, 0.0
    lo = hi = int(np.argmax(X))
    p = float(X[lo])
    while p < frac * tp and (lo > 0 or hi < N - 1):
        left = X[lo - 1] if lo > 0 else -1.0
        right = X[hi + 1] if hi < N - 1 else -1.0
        if right >= left:
            hi += 1; p += float(X[hi])
        else:
            lo -= 1; p += float(X[lo])
    f = np.fft.fftshift(np.fft.fftfreq(N, 1.0 / fs))
    bw = float((hi - lo + 1) * fs / N)
    fc = float((f[lo:hi + 1] * X[lo:hi + 1]).sum() / p)
    return bw, fc

def _burst_mask(total, fs, burst):
    """0/1 time-gate for an emitter. burst = {"start_s","duration_s","period_s"} or None.
    None gives all ones (continuous, backward-compatible); period_s <= 0 gives a single burst (no repeat)."""
    if not burst:
        return None
    start = float(burst.get("start_s", 0.0))
    dur = float(burst.get("duration_s", 0.0))
    period = float(burst.get("period_s", 0.0))
    if dur <= 0.0:
        return None
    m = np.zeros(total, dtype=np.float32)
    t = np.arange(total) / fs
    if period > 0.0:
        phase = np.mod(t - start, period)          # repeating burst every period_s
        m[(phase >= 0.0) & (phase < dur)] = 1.0
    else:
        m[(t >= start) & (t < start + dur)] = 1.0  # single burst
    return m

def compose_plan(plan, headroom=0.85, loop_len_s=0.10):
    """Render + place + sum all known entries into one wideband block at plan rate.
    Returns (iq_complex64, fs, center, report)."""
    fs = float(plan.sample_rate_hz)
    center = float(plan.center_freq_hz)
    total = int(fs * loop_len_s)
    total = ((total + 8188 - 1) // 8188) * 8188  # align to TX_BLOCK_SAMPLES
    wb = np.zeros(total, dtype=np.complex64)
    report = {"rendered": [], "skipped": []}
    contribs = []
    for e in plan.entries:
        if e.role != "known":
            report["skipped"].append((e.name, "unknown role")); continue
        try:
            iq_n, fs_n = render_entry(e)
        except Exception as ex:
            report["skipped"].append((e.name, f"render: {ex}")); continue
        if iq_n is None:
            report["skipped"].append((e.name, "no bits/rate")); continue
        iq = _resample(iq_n, fs_n, fs)
        # tile/trim to the block length
        if len(iq) < total:
            iq = np.tile(iq, total // len(iq) + 1)
        iq = iq[:total]
        try:  # measure baseband BW before the offset shift
            _bw, _fc = occupied_bw(iq, fs)
        except Exception:
            _bw, _fc = 0.0, 0.0
        # shift to the entry's placement offset
        t = np.arange(total) / fs
        iq = iq * np.exp(2j * np.pi * float(e.offset_hz) * t).astype(np.complex64)
        amp = 10.0 ** (float(e.power_db) / 20.0)
        _mask = _burst_mask(total, fs, getattr(e, "burst", None))
        if _mask is not None:
            iq = iq * _mask
        contribs.append(amp * iq)
        report["rendered"].append(e.name)
        report.setdefault("occ_bw", []).append({  # measured occupied bandwidth
            "name": e.name, "offset_hz": float(e.offset_hz),
            "bw_hz": _bw, "lo_hz": float(e.offset_hz) + _fc - _bw / 2.0,
            "hi_hz": float(e.offset_hz) + _fc + _bw / 2.0})
    if contribs:
        for c in contribs:
            wb += c
        peak = np.max(np.abs(wb)) + 1e-12
        wb = (wb / peak * headroom).astype(np.complex64)
    _ob = report.get("occ_bw", [])  #  edge + overlap lint
    _warns = report.setdefault("warnings", [])
    for _d in _ob:
        if _d["lo_hz"] < -fs / 2.0 or _d["hi_hz"] > fs / 2.0:
            _warns.append("%s: band %.4f..%.4f MHz exceeds +/-fs/2 -- aliasing"
                          % (_d["name"], _d["lo_hz"] / 1e6, _d["hi_hz"] / 1e6))
    for _ai in range(len(_ob)):
        for _bi in range(_ai + 1, len(_ob)):
            _a, _b = _ob[_ai], _ob[_bi]
            if _a["lo_hz"] < _b["hi_hz"] and _b["lo_hz"] < _a["hi_hz"]:
                _warns.append("%s overlaps %s (occupied bands intersect)"
                              % (_a["name"], _b["name"]))
    return wb, fs, center, report


def spectrum_report(wb, fs, center, plan=None):
    import numpy as np
    N=min(65536,len(wb))
    X=np.abs(np.fft.fftshift(np.fft.fft(wb[:N])))**2
    Xdb=10*np.log10(X/X.max()+1e-20)
    f=np.fft.fftshift(np.fft.fftfreq(N,1/fs))
    peaks=f[Xdb>-30]
    if len(peaks)==0: return "spectrum: no peaks>-30dB"
    clusters=sorted(set(round(p/1e6,1) for p in peaks))
    declared=sorted(round(e.offset_hz/1e6,1) for e in plan.entries) if plan else []
    return f"spectrum: energy@MHz {clusters[:15]}" + (f" | declared {declared}" if declared else "")
