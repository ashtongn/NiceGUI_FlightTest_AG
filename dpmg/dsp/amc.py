"""
amc.py: blind automatic modulation classification for a captured IQ slice.

classify(iq, fs) returns a dict with the predicted modulation, a confidence,
the family, an estimated symbol rate and carrier offset, and the raw feature
vector it decided on. The classifier is feature-based (no training data): it
leans on constant-envelope vs amplitude modulation, M-th power phase lines for
PSK order, instantaneous-frequency modality for FSK order, a dechirp score for
chirp spread-spectrum (CSS/LoRa), and higher-order cumulants to separate QAM
from PSK. Thresholds were tuned against the project's own modulators.
"""
from __future__ import annotations

import numpy as np


def _norm(iq: np.ndarray) -> np.ndarray:
    """Zero-mean, unit-power complex baseband."""
    x = np.asarray(iq, dtype=np.complex128)
    if x.size:
        x = x - x.mean()
        p = np.sqrt(np.mean(np.abs(x) ** 2))
        if p > 0:
            x = x / p
    return x


def env_var(x: np.ndarray) -> float:
    """Normalized envelope variation std|x|/mean|x|: ~0 for constant-envelope
    (FSK/PSK/CSS), larger for amplitude modulations (QAM/APSK/ASK)."""
    a = np.abs(x)
    m = a.mean()
    return float(a.std() / m) if m > 0 else 0.0


def mth_power_line(x: np.ndarray, m: int) -> float:
    """Prominence (dB) of the dominant spectral line of (x/|x|)^m. An M-PSK
    signal, de-rotated to the M-th power, collapses to a single carrier line, giving
    a tall peak at the correct M; FSK/QAM stay spread."""
    xn = x / np.maximum(np.abs(x), 1e-12)
    y = xn ** m
    y = y - y.mean()
    Y = np.abs(np.fft.fft(y))
    if Y.size == 0:
        return 0.0
    peak = Y.max()
    med = np.median(Y) + 1e-12
    return float(20.0 * np.log10(max(peak, 1e-12) / med))


def _occupied_bw(x: np.ndarray, fs: float) -> float:
    """-12 dB occupied bandwidth of the signal (~its symbol rate)."""
    n = min(x.size, 8192)
    if n < 64:
        return fs
    X = np.abs(np.fft.fftshift(np.fft.fft(x[:n] * np.hanning(n)))) ** 2
    f = np.fft.fftshift(np.fft.fftfreq(n, 1.0 / fs))
    band = f[X > X.max() * 10 ** (-1.2)]
    return float(band.max() - band.min()) if band.size else fs


def _to_symbol_rate(x: np.ndarray, fs: float, factor: float = 2.5):
    """Decimate to ~factor x the occupied bandwidth (2.5 = ~2.5 samples/symbol)
    so the phase statistics aren't washed out by oversampling and the CFO line
    is sharp. Returns (x2, fs2)."""
    bw = _occupied_bw(x, fs)
    if bw <= 0:
        return x, fs
    cur = float(fs)
    target = max(bw * factor, 1.0)
    try:
        import scipy.signal as _ss
        while cur / target >= 2.0 and x.size > 512:
            q = int(min(8, cur // target))
            if q < 2:
                break
            x = _ss.decimate(x, q, ftype="fir", zero_phase=True)
            cur /= q
    except Exception:
        pass
    return x, cur


def _estimate_cfo(x: np.ndarray, fs: float) -> float:
    """Residual carrier offset via the 4th-power method: (x/|x|)^4 strips the
    PSK/QAM data and leaves a sharp line at 4*CFO. Precise enough (sub-kHz) to
    de-spin the constellation, which the spectral centroid is not."""
    n = x.size
    if n < 64:
        return 0.0
    y = (x / np.maximum(np.abs(x), 1e-12)) ** 4
    Y = np.abs(np.fft.fft(y))
    k = int(np.argmax(Y))
    # parabolic interpolation around the peak bin for sub-bin precision
    km1, kp1 = (k - 1) % n, (k + 1) % n
    a, b, c = Y[km1], Y[k], Y[kp1]
    denom = (a - 2 * b + c)
    delta = 0.5 * (a - c) / denom if denom != 0 else 0.0
    f4 = (k + delta) / n * fs
    if f4 > fs / 2:
        f4 -= fs
    return float(f4 / 4.0)


def _derotate(x: np.ndarray, fs: float) -> np.ndarray:
    """Remove the residual carrier offset so the phase cumulants survive. A
    spinning constellation drives E[x^4] (hence C40) to zero and collapses
    BPSK/QPSK down to the 8PSK end of the ladder, as on a loopback
    where the TX and RX LOs differ by a few kHz."""
    cfo = _estimate_cfo(x, fs)
    if abs(cfo) < 1.0:
        return x
    return x * np.exp(-2j * np.pi * cfo / fs * np.arange(x.size))


def _inst_freq(x: np.ndarray) -> np.ndarray:
    return np.diff(np.unwrap(np.angle(x)))


def fsk_modality(x: np.ndarray) -> tuple[int, float]:
    """Count discrete tones in the instantaneous-frequency histogram. Returns
    (n_modes, separation_score). Constant-envelope MFSK shows n_modes distinct
    clusters; PSK/QAM give a single broad cluster."""
    f = _inst_freq(x)
    if f.size < 128:
        return 1, 0.0
    lo, hi = np.percentile(f, [1, 99])
    f = f[(f >= lo) & (f <= hi)]
    if f.size < 128 or hi <= lo:
        return 1, 0.0
    hist, edges = np.histogram(f, bins=80)
    hist = hist.astype(float)
    # smooth a touch so a single tone doesn't split into neighbours
    k = np.array([1.0, 2.0, 3.0, 2.0, 1.0])
    hist = np.convolve(hist, k / k.sum(), mode="same")
    thr = 0.25 * hist.max()
    # count peaks above threshold, separated by a valley
    modes = 0
    i = 1
    n = len(hist)
    peaks = []
    while i < n - 1:
        if hist[i] > thr and hist[i] >= hist[i - 1] and hist[i] >= hist[i + 1]:
            peaks.append(i)
            i += 3
        else:
            i += 1
    modes = len(peaks)
    # separation: spread of the mode centres over the inst-freq std
    if modes >= 2:
        centres = edges[peaks] if len(edges) > max(peaks) else np.array(peaks, float)
        sep = float((centres.max() - centres.min()) / (f.std() + 1e-12))
    else:
        sep = 0.0
    return modes, sep


def spec_peaks(x: np.ndarray, fs: float) -> tuple[int, float]:
    """Count well-separated narrow peaks in the smoothed power spectrum, and the
    fraction of the band they occupy. MFSK shows M comparable peaks; PSK/QAM a
    single main lobe; a chirp spreads into a wide flat shelf."""
    n = x.size
    if n < 256:
        return 1, 0.0
    nf = 1 << int(np.floor(np.log2(min(n, 8192))))
    acc = np.zeros(nf)
    w = np.hanning(nf)
    cnt = 0
    for i in range(0, n - nf + 1, nf // 2):
        acc += np.abs(np.fft.fftshift(np.fft.fft(x[i:i + nf] * w))) ** 2
        cnt += 1
    if cnt == 0:
        return 1, 0.0
    P = 10.0 * np.log10(acc / cnt + 1e-20)
    k = np.ones(max(3, nf // 128))
    P = np.convolve(P, k / k.sum(), mode="same")
    thr = P.max() - 6.0
    above = P > thr
    # count contiguous runs above -6 dB = resolvable peaks
    runs = np.diff(np.concatenate([[0], above.view(np.int8), [0]]))
    npk = int((runs == 1).sum())
    occ = float(above.mean())
    return npk, occ


def cumulants(x: np.ndarray) -> dict:
    """Normalized higher-order cumulants (unit-power input). |C42| and |C40|
    separate the PSK/QAM families; |C21| is the reference power."""
    x = _norm(x)
    c21 = np.mean(np.abs(x) ** 2)
    c20 = np.mean(x ** 2)
    c40 = np.mean(x ** 4) - 3.0 * c20 ** 2
    c42 = np.mean(np.abs(x) ** 4) - np.abs(c20) ** 2 - 2.0 * c21 ** 2
    c63 = (np.mean(np.abs(x) ** 6) - 9.0 * c21 * np.mean(np.abs(x) ** 4)
           + 12.0 * c21 ** 3)
    denom = (c21 ** 2) + 1e-12
    return {"c20": float(np.abs(c20) / (c21 + 1e-12)),
            "c40": float(np.abs(c40) / denom),
            "c42": float(np.abs(c42) / denom),
            "c63": float(np.abs(c63) / ((c21 ** 3) + 1e-12))}


def chirp_score(x: np.ndarray, fs: float) -> float:
    """Dechirp concentration for CSS/LoRa. A linear up-chirp spanning the signal
    bandwidth, multiplied by a matched down-chirp, collapses to tones: the
    dechirped spectrum becomes far peakier than the raw spectrum. Returns the
    peakiness gain in dB (high: chirp modulation)."""
    n = x.size
    if n < 256:
        return 0.0
    # occupied bandwidth from the raw spectrum (-10 dB)
    X = np.abs(np.fft.fftshift(np.fft.fft(x * np.hanning(n))))
    f = np.fft.fftshift(np.fft.fftfreq(n, 1.0 / fs))
    pk = X.max()
    band = f[X > pk * 10 ** (-10 / 20)]
    bw = (band.max() - band.min()) if band.size else fs / 4
    bw = min(bw, fs)
    t = np.arange(n) / fs
    best = 0.0
    for rate in (bw / (n / fs), -bw / (n / fs)):   # try both sweep directions
        ref = np.exp(-1j * np.pi * rate * t ** 2)
        Y = np.abs(np.fft.fft(x * ref))
        peaky = Y.max() / (np.median(Y) + 1e-12)
        best = max(best, peaky)
    raw_peaky = X.max() / (np.median(X) + 1e-12)
    return float(20.0 * np.log10(best / (raw_peaky + 1e-12)))


def symbol_rate(x: np.ndarray, fs: float) -> float:
    """Rough symbol-rate estimate from the cyclostationary line in |x|^2 (works
    for amplitude transitions); falls back to the -20 dB occupied bandwidth for
    constant-envelope signals. Approximate: for display, not timing."""
    n = x.size
    if n < 256:
        return 0.0
    mag = np.abs(x) ** 2
    mag = mag - mag.mean()
    M = np.abs(np.fft.rfft(mag * np.hanning(n)))
    fr = np.fft.rfftfreq(n, 1.0 / fs)
    M[0] = 0.0
    lo = fr > fs * 1e-4
    if lo.any() and M[lo].max() > 6.0 * np.median(M[lo] + 1e-12):
        return float(fr[lo][np.argmax(M[lo])])
    X = np.abs(np.fft.fftshift(np.fft.fft(x * np.hanning(n))))
    f = np.fft.fftshift(np.fft.fftfreq(n, 1.0 / fs))
    band = f[X > X.max() * 10 ** (-20 / 20)]
    return float(band.max() - band.min()) if band.size else 0.0


def features(iq: np.ndarray, fs: float) -> dict:
    """Full feature vector for a captured slice."""
    x = _norm(iq)
    # phase features (cumulants, PSK lines) run on a symbol-rate, CFO-corrected
    # copy: oversampling and a spinning carrier both wash out E[x^4] otherwise
    xs, fs_s = _to_symbol_rate(x, fs)
    xd = _derotate(xs, fs_s)
    ev = env_var(x)
    p2, p4, p8 = (mth_power_line(xd, m) for m in (2, 4, 8))
    nmodes, sep = fsk_modality(x)
    npk, occ = spec_peaks(x, fs)
    cum = cumulants(xd)
    cs = chirp_score(x, fs)
    sr = symbol_rate(x, fs)
    return {"env_var": ev, "psk2": p2, "psk4": p4, "psk8": p8,
            "fsk_modes": nmodes, "fsk_sep": sep, "spec_peaks": npk,
            "spec_occ": occ, "chirp": cs,
            "sym_rate_hz": sr, **cum, "n": int(x.size)}


def classify_legacy(iq: np.ndarray, fs: float) -> dict:
    """The pre-carrier-recovery decision tree (global cumulants, 4th-power
    de-rotation only). Kept so amc_eval.py can show the before/after matrix and
    as a fallback; classify() below is the default."""
    f = features(iq, fs)
    if f["n"] < 256:
        return {"modulation": "unknown", "family": "unknown",
                "confidence": 0.0, "sym_rate_hz": 0.0, "features": f}

    # Decision tree on noise-tolerant features. env_var and fsk_sep are reliable
    # only at high SNR, so the order leans on the cumulant ladder (c40 ~2/1/0
    # for BPSK/QPSK/8PSK) and c63 (~1.5 for QAM vs ~3.0 constant-envelope),
    # with spec_occ for the chirp and fsk_modes for the FSK order.
    c40, c63 = f["c40"], f["c63"]

    # 1) chirp spread-spectrum (CSS/LoRa): the chirp sweeps the whole band
    if f["spec_occ"] > 0.5 and f["fsk_sep"] > 1.5:
        label, fam, conf = "CSS", "chirp", min(1.0, 0.5 + f["spec_occ"] / 2.0)
    # 2) BPSK: top of the C40 ladder (checked before QAM: its c63 overlaps QAM)
    elif c40 > 1.3:
        label, fam, conf = "BPSK", "psk", min(1.0, 0.5 + (c40 - 1.3) / 1.4)
    # 3) QAM / APSK: amplitude rings pull c63 well below the constant-envelope 3.0
    elif c63 < 2.3:
        fam = "qam"
        label, conf = ("16QAM", 0.6) if c40 > 0.35 else ("APSK", 0.5)
    # 4) QPSK: middle of the C40 ladder
    elif c40 > 0.4:
        label, fam, conf = "QPSK", "psk", min(1.0, 0.5 + (1.0 - abs(c40 - 0.9)) / 1.2)
    # 5) FSK family: constant envelope, c40 ~0, inst-freq hops between tones
    elif f["fsk_modes"] >= 2 and f["fsk_sep"] > 0.4:
        label = "4FSK" if f["fsk_modes"] >= 2.8 else "FSK"
        fam, conf = "fsk", min(1.0, 0.45 + f["fsk_sep"] / 6.0)
    # 6) 8PSK: bottom of the ladder, single spectral lobe (no FSK modality)
    elif f["psk8"] > 10.0 or f["psk2"] > 10.0:
        label, fam, conf = "8PSK", "psk", 0.5
    else:
        label, fam, conf = "unknown", "unknown", 0.2

    return {"modulation": label, "family": fam, "confidence": round(float(conf), 2),
            "sym_rate_hz": round(f["sym_rate_hz"], 1), "features": f}


# Carrier recovery + symbol-level classification (the default path)
#
# The global cumulants above need a carrier that is static over the whole
# capture. Across two G40 cards the LOs are not coherent, so a residual offset
# of a few Hz (or slow phase wander) spins the constellation, c20/c40 average
# toward zero and BPSK reads as QPSK. The path below makes every feature
# carrier-insensitive:
#   1. coarse CFO from the M-th power spectral line, M picked per candidate
#      order (2/4/8, phase-only or amplitude-weighted), Welch-averaged so phase
#      noise between segments does not smear the line;
#   2. symbol rate from the cyclic line of the transition energy, then an
#      integrate-and-dump matched filter with block timing recovery;
#   3. fine CFO from the differential M-th power (y[k]*conj(y[k-1]) cancels any
#      static or slow phase), then block-wise feed-forward phase tracking;
#   4. a constellation likelihood fit (Gaussian mixture with a free complex gain)
#      over every linear constellation DPMG transmits, plus differential checks
#      for pi/4-QPSK and OQPSK and a code-periodicity test for DSSS;
#   5. FSK/GFSK/4FSK from per-symbol phase advance (needs no carrier at all) and
#      CSS from the legacy chirp features, both only when no linear carrier is
#      found.

_ORDERS = (2, 4, 8)


def _unit_power(c) -> np.ndarray:
    c = np.asarray(c, dtype=np.complex128)
    return c / np.sqrt(np.mean(np.abs(c) ** 2))


def _psk(m: int, off: float = 0.0) -> np.ndarray:
    return np.exp(1j * (off + 2.0 * np.pi * np.arange(m) / m))


def _rings(spec) -> np.ndarray:
    pts = []
    for n, r, off in spec:
        pts.extend(r * _psk(n, off))
    return np.array(pts)


_R3 = 1.0 + np.sqrt(3.0)
# Same point sets as dpmg/modulation (modulator.py, higher_order.py); kept
# here so the classifier has no import dependency on the TX package.
CONSTELLATIONS = {
    "BPSK": _unit_power([1.0, -1.0]),
    "QPSK": _unit_power(_psk(4, np.pi / 4)),
    "8PSK": _unit_power(_psk(8)),
    "8QAM": _unit_power([_R3, 1 + 1j, 1j * _R3, -1 + 1j, -_R3, -1 - 1j, -1j * _R3, 1 - 1j]),
    "16QAM": _unit_power([complex(a, b) for a in (-3, -1, 1, 3) for b in (-3, -1, 1, 3)]),
    "16APSK": _unit_power(_rings([(4, 1.0, np.pi / 4), (12, 2.85, np.pi / 12)])),
    "32APSK": _unit_power(_rings([(4, 1.0, np.pi / 4), (12, 2.84, np.pi / 12),
                                  (16, 5.27, np.pi / 16)])),
}
# rotational symmetry: the phase search only has to cover 2*pi/order
_SYMMETRY = {"BPSK": 2, "QPSK": 4, "8PSK": 8, "8QAM": 4, "16QAM": 4,
             "16APSK": 4, "32APSK": 4}

FAMILY = {"FSK": "fsk", "GFSK": "fsk", "4FSK": "fsk",
          "BPSK": "psk", "QPSK": "psk", "pi/4-QPSK": "psk", "OQPSK": "psk",
          "8PSK": "psk", "8QAM": "qam", "16QAM": "qam", "16APSK": "qam",
          "32APSK": "qam", "DSSS": "dsss", "QPSK-DSSS": "dsss", "CSS": "chirp",
          "unknown": "unknown"}

# Modulations that random data makes indistinguishable to a blind receiver.
# DQPSK carries its data in the phase change but sits on the plain QPSK points,
# so with random bits its symbol stream is statistically identical to QPSK.
EQUIVALENT = {"DQPSK": "QPSK", "2FSK": "FSK", "BPSK-DSSS": "DSSS",
              "DSSS-BPSK": "DSSS", "DSSS-QPSK": "QPSK-DSSS", "LoRa": "CSS",
              "LoRa-CSS": "CSS", "PI4QPSK": "pi/4-QPSK", "π/4-QPSK": "pi/4-QPSK"}


def canonical(mod: str) -> str:
    """The label classify() can return for a registry modulation name."""
    return EQUIVALENT.get(mod, mod)


def occupied_bw(x: np.ndarray, fs: float, db: float = 12.0) -> float:
    """-db occupied bandwidth from a Welch-averaged, lightly smoothed spectrum,
    taken as the contiguous region around the peak. Unlike _occupied_bw (one
    FFT, every bin above the threshold) a wide capture's noise bins cannot
    stretch it to the whole band."""
    return _occupied_span(x, fs, db)[1]


def _occupied_span(x: np.ndarray, fs: float, db: float = 12.0) -> tuple[float, float]:
    """(centre_hz, width_hz) of the occupied_bw region (centre 0 when the
    capture is too short for the Welch estimate)."""
    # Split out of occupied_bw so the FSK branch can centre its cut
    n = x.size
    L = int(min(4096, max(256, 1 << int(np.floor(np.log2(max(n // 8, 256)))))))
    if n < L:
        return 0.0, _occupied_bw(x, fs)
    seg = _segments(x, L) * np.hanning(L)
    S = np.fft.fftshift(np.mean(np.abs(np.fft.fft(seg, axis=1)) ** 2, axis=0))
    k = max(1, L // 128)
    S = np.convolve(S, np.ones(k) / k, mode="same")
    f = np.fft.fftshift(np.fft.fftfreq(L, 1.0 / fs))
    pk = int(np.argmax(S))
    thr = S[pk] * 10 ** (-db / 10)
    lo = pk
    while lo > 0 and S[lo - 1] > thr:
        lo -= 1
    hi = pk
    while hi < L - 1 and S[hi + 1] > thr:
        hi += 1
    # a two-tone FSK spectrum has a dip between its tones: lobes within 6 dB of
    # the peak (the other tones) widen the span; weaker lobes (hold images,
    # sidelobes, a neighbour) do not
    above = np.concatenate([[False], S > thr, [False]])
    edges = np.flatnonzero(np.diff(above.astype(np.int8)))
    for a, b in zip(edges[::2], edges[1::2]):
        if S[a:b].max() >= S[pk] * 10 ** (-0.6):
            lo, hi = min(lo, a), max(hi, b - 1)
    return 0.5 * float(f[hi] + f[lo]), float(max(f[hi] - f[lo], fs / L))


def power_bw(x: np.ndarray, fs: float, frac: float = 0.98) -> float:
    """Bandwidth holding `frac` of the signal power above the noise floor
    (Welch PSD; floor = 20th percentile, i.e. the capture must be at least ~20 %
    noise-only band, which a brushed selection at 2.5x its width is). Unlike a
    -dB width it is not fooled by a line spectrum (CPFSK with h ~ 1 puts most
    of its power in two narrow tone lines plus a wide skirt)."""
    n = x.size
    L = int(min(4096, max(256, 1 << int(np.floor(np.log2(max(n // 8, 256)))))))
    if n < L:
        return occupied_bw(x, fs)
    seg = _segments(x, L) * np.hanning(L)
    S = np.fft.fftshift(np.mean(np.abs(np.fft.fft(seg, axis=1)) ** 2, axis=0))
    S = np.maximum(S - np.percentile(S, 20), 0.0)
    c = np.cumsum(S)
    if c[-1] <= 0:
        return fs
    c /= c[-1]
    f = np.fft.fftshift(np.fft.fftfreq(L, 1.0 / fs))
    lo = int(np.searchsorted(c, (1 - frac) / 2))
    hi = int(np.searchsorted(c, (1 + frac) / 2))
    return float(max(f[min(hi, L - 1)] - f[lo], fs / L))


def _band_limit_to(x: np.ndarray, fs: float, target: float):
    """Integer-factor FIR decimation while fs stays >= 2x target."""
    cur = float(fs)
    try:
        import scipy.signal as _ss
        while cur / max(target, 1.0) >= 2.0 and x.size > 512:
            q = int(min(8, cur // max(target, 1.0)))
            if q < 2:
                break
            x = _ss.decimate(x, q, ftype="fir", zero_phase=True)
            cur /= q
    except Exception:
        pass
    return x, cur


def _band_limit(x: np.ndarray, fs: float, factor: float = 2.5):
    """Decimate to ~factor x the signal's width: the larger of the -12 dB
    width (occupied_bw) and half the 98 % power width (power_bw), so a line
    spectrum is not cut down to its lines."""
    bw = max(occupied_bw(x, fs), 0.5 * power_bw(x, fs))
    cur = float(fs)
    target = max(bw * factor, 1.0)
    try:
        import scipy.signal as _ss
        while cur / target >= 2.0 and x.size > 512:
            q = int(min(8, cur // target))
            if q < 2:
                break
            x = _ss.decimate(x, q, ftype="fir", zero_phase=True)
            cur /= q
    except Exception:
        pass
    return x, cur


def _segments(y: np.ndarray, L: int) -> np.ndarray:
    hop = max(1, L // 2)
    n = max(1, (y.size - L) // hop + 1)
    idx = np.arange(L)[None, :] + hop * np.arange(n)[:, None]
    return y[idx]


def _line(y: np.ndarray, fs: float, L: int) -> tuple[float, float, float]:
    """Strongest spectral line of y, power-averaged over L-sample segments.
    Returns (coherence, freq_hz, prominence). coherence = |line amplitude| /
    rms(y): ~1 for a pure tone. prominence = peak power over the local median
    of the spectrum around it (main lobe excluded): a true line is narrow,
    while the continuous spectrum of x^M for rectangular symbols is a broad
    hump that can hold the maximum without being a line."""
    L = int(min(L, y.size))
    if L < 32:
        return 0.0, 0.0, 0.0
    seg = _segments(y, L)
    P = 4 * (1 << int(np.ceil(np.log2(L))))
    S = np.mean(np.abs(np.fft.fft(seg, P, axis=1)) ** 2, axis=0)
    k = int(np.argmax(S))
    a, b, c = np.sqrt(S[(k - 1) % P]), np.sqrt(S[k]), np.sqrt(S[(k + 1) % P])
    den = a - 2 * b + c
    delta = 0.5 * (a - c) / den if den != 0 else 0.0
    f = (k + delta) / P * fs
    f = (f + fs / 2) % fs - fs / 2
    rms = np.sqrt(np.mean(np.abs(y) ** 2)) + 1e-30
    near = np.r_[np.arange(k - 96, k - 12), np.arange(k + 13, k + 97)] % P
    prom = float(S[k] / (np.median(S[near]) + 1e-30))
    return float(b / (L * rms)), float(f), prom


def mth_power_lines(x: np.ndarray, fs: float, L: int = 1024) -> dict:
    """{M: (coherence, line_freq_hz, weighting, prominence)} for M in 2/4/8.
    Two weightings per order: phase-only (x/|x|)^M (best for PSK/APSK) and raw
    x^M (needed for 8QAM, whose two rings cancel in the phase-only 4th power).
    Only weightings whose maximum is a real line (prominence) compete."""
    xn = x / np.maximum(np.abs(x), 1e-12)
    out = {}
    for m in _ORDERS:
        best = (0.0, 0.0, "phase", 0.0)
        for w, y in (("phase", xn ** m), ("raw", x ** m)):
            coh, f, prom = _line(y, fs, L)
            if prom < LINE_PROM:
                coh = 0.0
            if coh > best[0] or (best[0] == 0.0 and prom > best[3]):
                best = (coh, f, w, prom)
        out[m] = best
    return out


LINE_PROM = 8.0       # peak / local median (power) for a spectral line


def pick_order(lines: dict, fs: float, L: int) -> int | None:
    """Lowest M whose line is near the strongest one and agrees with every
    detected higher-order line (f_M2 = f_M * M2/M). The agreement check is what
    stops pi/4-QPSK (4th-power lines at 4*CFO +/- Rs/2) from being taken as
    QPSK with a wrong CFO."""
    best = max(v[0] for v in lines.values())
    if best <= 0.0:
        return None
    tol = 4.0 * fs / L
    for m in _ORDERS:
        coh, f = lines[m][0], lines[m][1]
        if coh <= 0.0 or coh < 0.6 * best:
            continue
        ok = True
        for m2 in _ORDERS:
            if m2 <= m or lines[m2][0] <= 0.0:
                continue
            d = (lines[m2][1] - f * m2 / m + fs / 2) % fs - fs / 2
            if abs(d) > tol * m2 / m:
                ok = False
                break
        if ok:
            return m
    return max(lines, key=lambda k: lines[k][0])


def _cyclic_line(g: np.ndarray, fs: float, fmin: float, fmax: float, walk: bool = True):
    """Fundamental of the strongest periodic component of a real sequence g.
    Rectangular transitions put equal lines at every harmonic, so after the
    peak the sub-harmonics f/2..f/6 (up to f/16) are checked and the lowest one whose full harmonic series
    is present wins.
    Returns (freq_hz, prominence)."""
    n = g.size
    if n < 256:
        return 0.0, 0.0
    g = (g - g.mean()) * np.hanning(n)
    P = 2 * (1 << int(np.ceil(np.log2(n))))
    G = np.abs(np.fft.rfft(g, P))
    fr = np.fft.rfftfreq(P, 1.0 / fs)
    ok = (fr >= fmin) & (fr <= fmax)
    if ok.sum() < 8:
        return 0.0, 0.0
    floor = float(np.median(G[ok])) + 1e-30
    idx = np.flatnonzero(ok)
    k = int(idx[np.argmax(G[idx])])
    peak = G[k]
    if peak < 8.0 * floor:
        return 0.0, float(peak / floor)
    df = fs / P

    def _near(f0):
        j0 = int(round(f0 / df))
        lo, hi = max(1, j0 - 3), min(G.size - 2, j0 + 3)
        j = lo + int(np.argmax(G[lo:hi + 1]))
        return j, G[j]

    for h in (range(16, 1, -1) if walk else ()):
        fh = fr[k] / h
        if fh < fmin:
            continue
        # accept a sub-harmonic only if the whole series fh, 2fh .. h*fh is
        # present (rectangular transitions); a DSSS code repeating every L
        # chips puts isolated lines at multiples of Rc/L that must not win
        j, gj = _near(fh)
        if all(_near(i * fh)[1] > 0.35 * peak for i in range(1, h + 1)) \
                and gj > 8.0 * floor:
            k = j
            break
    a, b, c = G[k - 1], G[k], G[k + 1]
    den = a - 2 * b + c
    delta = 0.5 * (a - c) / den if den != 0 else 0.0
    return float((k + delta) * df), float(b / floor)


def _cyclic_peaks(g: np.ndarray, fs: float, fmin: float, fmax: float, n: int = 3):
    """Up to n strongest local maxima of the spectrum of a real sequence g
    within fmin..fmax, at least 8x the median there and 3 % apart. Returns
    [(prominence, freq_hz)], strongest first."""
    m = g.size
    if m < 256:
        return []
    g = (g - g.mean()) * np.hanning(m)
    P = 2 * (1 << int(np.ceil(np.log2(m))))
    G = np.abs(np.fft.rfft(g, P))
    fr = np.fft.rfftfreq(P, 1.0 / fs)
    idx = np.flatnonzero((fr >= fmin) & (fr <= fmax))
    if idx.size < 8:
        return []
    floor = float(np.median(G[idx])) + 1e-30
    k = idx[(idx > 0) & (idx < G.size - 1)]
    k = k[(G[k] >= G[k - 1]) & (G[k] >= G[k + 1]) & (G[k] > 8.0 * floor)]
    out = []
    for j in k[np.argsort(G[k])[::-1]]:
        a, b, c = G[j - 1], G[j], G[j + 1]
        den = a - 2 * b + c
        f0 = (j + (0.5 * (a - c) / den if den != 0 else 0.0)) * fs / P
        if all(abs(f0 - f1) > 0.03 * f1 for _p, f1 in out):
            out.append((float(b / floor), float(f0)))
        if len(out) >= n:
            break
    return out


def baud_rate(x: np.ndarray, fs: float) -> tuple[float, float]:
    """Symbol (transition) rate of a linear modulation: cyclic line of the
    sample-to-sample change energy (rectangular hold) or of |x|^2 (shaped
    pulses), whichever is more prominent. Phase-insensitive by construction.
    Searched within 0.3..2.6 x the occupied bandwidth (a linear modulation's
    -12 dB width is about one symbol rate; OQPSK's transitions run at twice
    it), which keeps out the frame-rate comb of a looped TX burst."""
    bw = occupied_bw(x, fs)
    fmin, fmax = max(fs / 400.0, 0.3 * bw), min(0.45 * fs, 2.6 * bw)
    if fmax <= fmin:
        return 0.0, 0.0
    best = (0.0, 0.0)
    for g in (np.abs(np.diff(x)) ** 2, np.abs(x) ** 2):
        f, prom = _cyclic_line(g, fs, fmin, fmax)
        if f > 0 and prom > best[1]:
            best = (f, prom)
    return best


def _resample_to(x: np.ndarray, fs: float, fs_new: float) -> np.ndarray:
    import scipy.signal as _ss
    m = int(round(x.size * fs_new / fs))
    if m < 16:
        return x
    return _ss.resample(x, m)


def _block_timing(e: np.ndarray, K: int, B: int) -> np.ndarray:
    """Per-sample timing phase (0..K) from the K-periodic component of an
    energy sequence e, estimated per block of B symbols and unwrapped so a
    small symbol-rate error becomes a slow drift instead of a slip."""
    n = e.size
    blk = B * K
    nb = max(1, n // blk)
    rot = np.exp(-2j * np.pi * np.arange(n) / K)
    S = np.array([np.sum(e[b * blk:(b + 1) * blk] * rot[b * blk:(b + 1) * blk])
                  for b in range(nb)])
    ph = np.unwrap(np.angle(S))
    if nb >= 3:
        ph = np.convolve(np.pad(ph, 1, mode="edge"), np.ones(3) / 3, mode="valid")
    tau = (-ph * K / (2 * np.pi))
    centres = (np.arange(nb) + 0.5) * blk
    return np.interp(np.arange(n), centres, tau)


def _sample_at(y: np.ndarray, pos: np.ndarray) -> np.ndarray:
    pos = pos[(pos >= 0) & (pos <= y.size - 1)]
    i = np.floor(pos).astype(int)
    i = np.minimum(i, y.size - 2)
    fr = pos - i
    return y[i] * (1 - fr) + y[i + 1] * fr


def symbols(x: np.ndarray, fs: float, rs: float, K: int = 8,
            max_sym: int = 2000, B: int = 64) -> np.ndarray:
    """Symbol-spaced samples: resample to K samples/symbol, integrate-and-dump
    over one symbol, sample at the block-tracked energy peak."""
    nmax = int(max_sym * fs / rs) + 8 * K
    x = x[:nmax]
    xr = _resample_to(x, fs, K * rs)
    if xr.size < 4 * K:
        return np.zeros(0, complex)
    c = np.concatenate([[0.0], np.cumsum(xr)])
    y = (c[K:] - c[:-K]) / K               # y[n] = mean(xr[n:n+K])
    edge = 2 * K
    y = y[edge:-edge] if y.size > 4 * edge else y
    tau = _block_timing(np.abs(y) ** 2, K, B)
    k = np.arange(0, y.size, K, dtype=float)
    pos = k + np.interp(k, np.arange(y.size), tau) % K
    return _sample_at(y, pos)


def carrier_track(y: np.ndarray, m: int, weighting: str = "phase",
                  B: int | None = None) -> tuple[np.ndarray, float, float]:
    """Fine carrier recovery on symbol-spaced samples for an order-m
    constellation. (a) If the differential m-th power y[k]*conj(y[k-1]) is
    strong (PSK), it gives the residual frequency directly and is immune to
    any static or slowly wandering phase. (b) Feed-forward Viterbi & Viterbi
    phase: the m-th power summed over a sliding window of B symbols,
    unwrapped modulo 2*pi/m. B trades estimation noise (short) against phase
    wander (long); it is picked per capture by leave-one-out coherence (each
    symbol is scored against a window that excludes it, so a short window
    cannot win by fitting the noise). Returns (y_tracked,
    residual_rad_per_symbol_from_(a), rms_phase_step_rad_per_B)."""
    n = y.size
    if n < 32:
        return y, 0.0, 0.0
    k = np.arange(n)
    yn = y / np.maximum(np.abs(y), 1e-12)
    zm = np.mean((yn[1:] * np.conj(yn[:-1])) ** m)
    w = 0.0
    if abs(zm) > 0.3 and abs(np.angle(zm) / m) <= np.pi / 8:
        # (|w| > pi/8 per symbol is outside the small-offset prior; for pi/4-QPSK
        # under a QPSK hypothesis it is the 45-degree turn itself)
        w = float(np.angle(zm) / m)
        y = y * np.exp(-1j * w * k)
    src = (y / np.maximum(np.abs(y), 1e-12)) if weighting == "phase" else \
        y / np.sqrt(np.mean(np.abs(y) ** 2))
    p = src ** m
    c = np.concatenate([[0.0], np.cumsum(p)])

    def window_sum(b):
        lo = np.clip(k - b // 2, 0, n)
        hi = np.clip(k - b // 2 + b, 0, n)
        return c[hi] - c[lo]

    if B is None:
        best = (-np.inf, 16)
        ap = np.mean(np.abs(p)) + 1e-12
        for b in (4, 8, 16, 32, 64, 128, 256, 512, 1024):
            if b > n:
                break
            T = window_sum(b) - p
            sc = float(np.mean(np.real(p * np.conj(T) / np.maximum(np.abs(T), 1e-12)))) / ap
            if sc > best[0]:
                best = (sc, b)
        B = best[1]
    S = window_sum(B)
    phi = np.unwrap(np.angle(S)) / m
    step = phi[B::B] - phi[:-B:B] if n > 2 * B else np.zeros(1)
    return y * np.exp(-1j * phi), w, float(np.sqrt(np.mean(step ** 2)))


def _gmm_fit(y: np.ndarray, C: np.ndarray, sym: int, n_init: int = 6):
    """Log-likelihood per symbol of y under 'g * C[i] + complex AWGN' with equal
    point weights, maximised over the complex gain g (amplitude and residual
    phase) and the noise variance by EM, from n_init starting phases across
    the constellation's symmetry period. Returns (ll_per_sym, g, s2)."""
    N = y.size
    CC = C[:, None]
    absC2 = np.abs(CC) ** 2

    def run(g, s2, iters):
        for _ in range(iters):
            d = np.abs(y[None, :] - g * CC) ** 2
            l = -d / s2
            r = np.exp(l - l.max(axis=0))
            r /= r.sum(axis=0)
            g = np.sum(r * y[None, :] * np.conj(CC)) / np.sum(r * absC2)
            s2 = max(float(np.sum(r * d) / N), 1e-4)
        d = np.abs(y[None, :] - g * CC) ** 2
        l = -d / s2
        lm = l.max(axis=0)
        ll = np.sum(lm + np.log(np.exp(l - lm).sum(axis=0)))
        ll = ll / N - np.log(C.size) - np.log(np.pi * s2)
        return float(ll), g, s2

    starts = []
    for k in range(n_init):
        g0 = 0.9 * np.exp(1j * 2 * np.pi * k / (sym * n_init))
        starts.append(run(g0, 0.1, 3))
    starts.sort(key=lambda t: t[0], reverse=True)
    return max((run(g, s2, 8) for _, g, s2 in starts[:2]), key=lambda t: t[0])


def dsss_period(y: np.ndarray, lmax: int = 128) -> tuple[int, float]:
    """Short-code DSSS test on chip-spaced samples. With a code of L chips
    repeating every data symbol, s[k]*conj(s[k+L]) is constant over each symbol
    (d[m]*conj(d[m+1]) * |c|^2), so its L-sample sliding mean keeps a large
    magnitude; for unspread data it averages down to ~1/sqrt(L). Returns
    (L, score 0..1) for the shortest strong lag."""
    s = y / np.maximum(np.abs(y), 1e-12)
    N = s.size
    if N < 32 or abs(np.mean(s[1:] * np.conj(s[:-1]))) > 0.6:
        return 0, 0.0      # neighbours alike: oversampled symbols, not chips
    scores = {}
    for L in range(4, min(lmax, N // 6) + 1):
        p = s[:-L] * np.conj(s[L:])
        c = np.concatenate([[0.0], np.cumsum(p)])
        w = (c[L:] - c[:-L]) / L
        R = float(np.mean(np.abs(w) ** 2))
        scores[L] = (R - 1.0 / L) / (1.0 - 1.0 / L)
    if not scores:
        return 0, 0.0
    for L in list(scores):
        # Real spreading carries data: s[k]*conj(s[k+L]) = d[m]*conj(d[m+1]) has
        # a random sign per symbol, so its mean at every code phase is ~0. A
        # looped frame (p = 1 everywhere) or a text payload (the fixed high
        # bits of ASCII bytes repeat every 8 bits: p = 1 at those phases) has
        # code phases whose mean stays near 1: that is not DSSS.
        p = s[:-L] * np.conj(s[L:])
        nb = p.size // L
        if nb >= 4:
            ph = np.abs(p[:nb * L].reshape(nb, L).mean(axis=0))
            if ph.max() > 0.6 or abs(np.mean(p)) > 0.5:
                scores[L] = 0.0
    top = max(scores.values())
    if top < 0.2:
        return 0, float(top)
    for L in sorted(scores):
        if scores[L] >= 0.6 * top:
            return L, float(scores[L])
    return 0, float(top)


def _psk_transitions(y: np.ndarray, C: np.ndarray, g: complex) -> dict:
    """Fractions of 0/90/180-degree steps between consecutive hard decisions on
    a QPSK grid (QPSK: .25/.5/.25; OQPSK at its half-symbol rate: .5/.5/0)."""
    q = np.argmin(np.abs(y[None, :] - g * C[:, None]), axis=0)
    a = np.round(np.angle(C[q] / C[0]) / (np.pi / 2)).astype(int)
    d = np.diff(a) % 4
    if d.size == 0:
        return {"p0": 0.0, "p90": 0.0, "p180": 0.0}
    return {"p0": float(np.mean(d == 0)), "p90": float(np.mean((d == 1) | (d == 3))),
            "p180": float(np.mean(d == 2))}


# carrier-recovery order and M-th power weighting per hypothesis
_HYP_CARRIER = {"BPSK": (2, "phase"), "QPSK": (4, "phase"), "8PSK": (8, "phase"),
                "8QAM": (4, "raw"), "16QAM": (4, "raw"), "16APSK": (4, "phase"),
                "32APSK": (4, "phase")}


def _window_line(p: np.ndarray, rs: float, fmax: float, L: int):
    """Peak of |FFT(p)|^2 within |f| <= fmax, power-averaged over L-sample
    segments. Returns (freq_hz, prominence = peak / median over the window)."""
    seg = _segments(p, L)
    P = 8 * (1 << int(np.ceil(np.log2(max(L, 16)))))
    S = np.mean(np.abs(np.fft.fft(seg, P, axis=1)) ** 2, axis=0)
    f = np.fft.fftfreq(P, 1.0 / rs)
    win = np.flatnonzero(np.abs(f) <= fmax)
    k = int(win[np.argmax(S[win])])
    a, b, c = np.sqrt(S[(k - 1) % P]), np.sqrt(S[k]), np.sqrt(S[(k + 1) % P])
    den = a - 2 * b + c
    delta = 0.5 * (a - c) / den if den != 0 else 0.0
    return float(f[k] + delta * rs / P), float(S[k] / (np.median(S[win]) + 1e-30))


def carrier_for(y: np.ndarray, rs: float, m: int, weighting: str):
    """Carrier recovery of symbol-spaced samples for one hypothesis order m:
    the M-th power line searched within +/- Rs/16 of zero offset, then
    carrier_track. The line is taken from one full-length FFT (coherent gain:
    a weak line such as 32APSK's rises above the white data spectrum) or from
    256-symbol segments (survives phase noise that smears the full-length
    line), whichever is more prominent. Returns (y_recovered, cfo_hz,
    block_phase_std_rad, line_prominence)."""
    n = y.size
    src = (y / np.maximum(np.abs(y), 1e-12)) if weighting == "phase" else y
    p = src ** m
    fmax = m * rs / 16.0
    best = max((_window_line(p, rs, fmax, L) for L in {n, min(n, 256)}),
               key=lambda t: t[1])
    cfo = best[0] / m
    y = y * np.exp(-2j * np.pi * cfo / rs * np.arange(n))
    y, w, pstd = carrier_track(y, m, weighting)
    return y, float(cfo + w * rs / (2 * np.pi)), pstd, best[1]


# Constant-envelope hypotheses must show their own M-th power line: a ring of
# random phases (FSK, GFSK) otherwise fits 8PSK at ~13 dB.
_NEEDS_LINE = ("BPSK", "QPSK", "8PSK")
PSK_LINE_PROM = 150.0
M4_TOL = 0.15         # observed E|y|^4 may sit this far below the model (unit
                      # power): flatter than the constellation = constant envelope
M4_TOL_UP = 0.4       # ... and this far above it (burst joins and ramps add
                      # envelope outliers, never remove them)


def _model_m4(C: np.ndarray, g: complex, s2: float) -> float:
    """E|y|^4 / (E|y|^2)^2 of g*C + complex AWGN of variance s2."""
    a2 = abs(g) ** 2
    p2 = a2 * np.mean(np.abs(C) ** 2) + s2
    p4 = a2 ** 2 * np.mean(np.abs(C) ** 4) + 4 * a2 * s2 * np.mean(np.abs(C) ** 2) + 2 * s2 ** 2
    return float(p4 / p2 ** 2)


def _linear_classify(x: np.ndarray, fs: float, info: dict):
    """Linear-modulation branch. Each constellation hypothesis gets its own
    carrier recovery (its own symmetry order and weighting), then a likelihood
    fit; the best fit wins. Returns (label, confidence) or None when no
    constellation fits well enough (then CSS/FSK are tried).

    It works on a copy band-limited to ~2.5x the occupied width: a TX chain's
    interpolation filter leaves Gibbs ringing at every rectangular transition,
    which would otherwise put the strongest transition line at a high harmonic
    of the symbol rate. If that finds nothing it retries at 5x: OQPSK's
    half-symbol transitions sit at twice its -12 dB width, above the 0.45*fs
    search limit of the 2.5x copy."""
    for factor in (2.5, 5.0):
        xb, fsb = _band_limit(x, fs, factor)
        rs, prom = baud_rate(xb, fsb)
        info["baud_hz"], info["baud_prom"] = rs, prom
        if rs > 0:
            # a frame's periodic preamble can put the strongest transition line
            # at Rs/2 (or a harmonic at 2 Rs): fit at the neighbours too and
            # keep the cleanest constellation
            best, best_sc, best_m, cands = None, -99.0, 1.0, {}
            for mult in (1.0, 2.0, 0.5):
                r = rs * mult
                if not (fsb / 400.0 <= r <= 0.45 * fsb):
                    continue
                sub = {}
                res = _linear_at(xb, fsb, sub, r)
                if res is not None:
                    cands[mult] = (res, sub)
            for mult, (res, sub) in cands.items():
                if sub.get("decisive"):
                    if mult != 1.0:
                        continue       # chips at a multiple of the line: oversampled data
                    half = cands.get(0.5)
                    if half and half[1].get("esn0_db", -99) >= LIN_SURE_DB:
                        continue       # it is plain PSK at half that rate, sampled twice
                    best = (res, sub)
                    break
                # the same constellation sampled at 2x the rate fits ~3 dB worse
                # (half the energy per window): compare per-unit-time, and keep
                # the lower rate unless the higher one is clearly better
                sc = sub.get("esn0_db", -99) + 10 * np.log10(mult)
                if best is None:
                    best, best_sc, best_m = (res, sub), sc, mult
                elif (sc > best_sc + 1.5) or (abs(sc - best_sc) <= 1.5 and mult < best_m):
                    best, best_sc, best_m = (res, sub), sc, mult
            if best is not None:
                info.update(best[1])
                info["baud_prom"] = prom
                return best[0]
        if fsb >= fs:
            break
    return None


def _linear_at(x: np.ndarray, fs: float, info: dict, rs: float):
    """_linear_classify on one band-limited copy at symbol rate rs."""
    info["baud_hz"] = rs
    y0 = symbols(x, fs, rs)
    if y0.size < 64:
        return None
    # Short-code DSSS first: the code-periodicity test and the chip type are
    # both read from products s[k]*conj(s[k+L]), which cancel any carrier
    # phase, so no hypothesis tracking is needed (and a BPSK tracker would
    # otherwise follow QPSK-DSSS data flips that last a whole code period).
    L, sc = dsss_period(y0)
    info["dsss_L"], info["dsss_score"] = L, sc
    if L:
        s0 = y0 / np.maximum(np.abs(y0), 1e-12)
        pr = s0[:-L] * np.conj(s0[L:])
        b2 = float(abs(np.mean(pr ** 2)))
        b4 = float(abs(np.mean(pr ** 4)))
        info["dsss_b2"], info["dsss_b4"] = b2, b4
        if b4 < 0.5:
            L = 0        # the products are not on a BPSK/QPSK grid: not DSSS chips
    if L:
        info["chip_rate_hz"] = rs
        info["sym_rate_hz"] = info["dsss_symbol_rate_hz"] = rs / L
        info.update(order=2 if b2 > 0.5 else 4, weighting="phase", fine_cfo_hz=0.0,
                    decisive=True)       # code periodicity: no FSK comparison needed
        return ("DSSS" if b2 > 0.5 else "QPSK-DSSS"), min(1.0, 0.5 + sc)
    rec, fits = {}, {}
    cache = {}
    for key in set(_HYP_CARRIER.values()):
        cache[key] = carrier_for(y0, rs, *key)
    prom = {key[0]: max(v[3] for k2, v in cache.items() if k2[0] == key[0])
            for key in cache}
    info["sym_line_prom"] = {m_: round(v, 1) for m_, v in prom.items()}
    m4_obs = float(np.mean(np.abs(y0) ** 4) / np.mean(np.abs(y0) ** 2) ** 2)
    info["m4"] = m4_obs
    for name, C in CONSTELLATIONS.items():
        key = _HYP_CARRIER[name]
        yh, cfo, pstd, lp = cache[key]
        if name in _NEEDS_LINE and lp < PSK_LINE_PROM:
            continue     # a PSK without its M-th power line is not that PSK
        yh = yh / np.sqrt(np.mean(np.abs(yh) ** 2))
        ft = _gmm_fit(yh, C, _SYMMETRY[name])
        d4 = m4_obs - _model_m4(C, ft[1], ft[2])
        if d4 < -M4_TOL or d4 > M4_TOL_UP:
            continue     # envelope statistics do not match this constellation
        rec[name] = (yh, cfo, pstd, key)
        fits[name] = ft

    def _fits_well(names):
        return any(n in fits and 10 * np.log10(abs(fits[n][1]) ** 2 / fits[n][2]) >= LIN_SURE_DB - 5
                   for n in names)
    # Symmetry exclusions, applied only when the lower-order hypothesis itself
    # fits: tracking a 2-fold signal at order 4/8 (or a 4-fold one at order 8)
    # can slip by 2*pi/M and manufacture the extra points of the denser
    # constellation. A structured payload (repeated text, a preamble) can also
    # raise a 2nd-power line on a 4-fold signal; then BPSK does not fit and
    # nothing is excluded.
    if prom[2] >= PSK_LINE_PROM and _fits_well(["BPSK"]):
        for n in [n for n in fits if _SYMMETRY[n] > 2]:
            fits.pop(n); rec.pop(n)
    if prom[4] >= PSK_LINE_PROM and "8PSK" in fits and \
            _fits_well([n for n in fits if _SYMMETRY[n] == 4]):
        fits.pop("8PSK"); rec.pop("8PSK")
    # pi/4-QPSK = QPSK whose grid turns by 45 degrees every symbol
    k = np.arange(y0.size)
    best_pi4 = None
    for sg in (1, -1):
        yh, cfo, pstd, lp = carrier_for(y0 * np.exp(sg * 1j * np.pi / 4 * k), rs, 4, "phase")
        if lp < PSK_LINE_PROM or abs(1.0 - m4_obs) > M4_TOL + 0.3 \
                or (prom[2] >= PSK_LINE_PROM and _fits_well(["BPSK"])) \
                or (prom[4] >= PSK_LINE_PROM and _fits_well(["QPSK"])):
            continue
        yh = yh / np.sqrt(np.mean(np.abs(yh) ** 2))
        ft = _gmm_fit(yh, CONSTELLATIONS["QPSK"], 4)
        if best_pi4 is None or ft[0] > best_pi4[0][0]:
            best_pi4 = (ft, (yh, cfo, pstd, (4, "phase")))
    if best_pi4 is not None:
        fits["pi/4-QPSK"], rec["pi/4-QPSK"] = best_pi4
    if not fits:
        return None
    if len(fits) == 1:
        fits["_none"] = (-np.inf, 0j, 1.0)
    ranked = sorted(fits.items(), key=lambda kv: kv[1][0], reverse=True)
    (best, (ll, g, s2)), (_second, (ll2, _, _)) = ranked[0], ranked[1]
    y, cfo, pstd, (m, wgt) = rec[best]
    esn0 = 10 * np.log10(abs(g) ** 2 / s2)
    info.update(order=m, weighting=wgt, fine_cfo_hz=cfo,
                phase_track_std_deg=float(np.degrees(pstd)), esn0_db=float(esn0))
    info["ll"] = {k2: round(v[0], 3) for k2, v in fits.items() if np.isfinite(v[0])}
    if esn0 < MIN_ESN0_DB:
        return None
    conf = min(1.0, 0.5 + (ll - ll2)) if np.isfinite(ll2) else 0.9
    label = best
    if label == "QPSK":
        t = _psk_transitions(y, CONSTELLATIONS["QPSK"], g)
        info.update({"step_" + k2: v for k2, v in t.items()})
        if t["p180"] < 0.08 and t["p0"] > 0.35:
            label = "OQPSK"       # never a 180-degree step at the half-symbol rate
            info["oqpsk_symbol_rate_hz"] = rs / 2
    info["sym_rate_hz"] = info.get("oqpsk_symbol_rate_hz") or rs
    return label, conf


MIN_ESN0_DB = 8.0     # constellation fit below this = no linear modulation
LIN_SURE_DB = 15.0    # constellation fit this clean is certain (rate/order choices)
STRONG_LINE_PROM = 1000.0   # symbol-level 4th/8th-power line of a phase constellation
                            # (an MFSK with h >> 1 shows one too: _fsk_beats_linear)


def _fit_fsk(F: np.ndarray, offs: np.ndarray, iters: int = 30):
    """1-D equal-weight, equally-spaced Gaussian mixture (MFSK tones): means
    c + d*offs. EM over centre c, spacing d, variance. Returns (ll/sym, c, d, s2).
    Two starting centres: the median and the middle of the 3..97 % range (a
    short looped payload can use the tones unevenly, which moves the median
    a whole tone off centre and leaves EM in a wrong local optimum)."""
    lo, hi = np.percentile(F, [3, 97])
    fits = [_fit_fsk_from(F, offs, c0, lo, hi, iters)
            for c0 in {float(np.median(F)), 0.5 * float(lo + hi)}]
    return max(fits, key=lambda t: t[0])


def _fit_fsk_from(F, offs, c0, lo, hi, iters):
    c, d = c0, float(max(hi - lo, 1e-9) / (offs.max() - offs.min()))
    s2 = (d / 4) ** 2 + 1e-12
    A = np.stack([np.ones_like(offs), offs], axis=1)
    for _ in range(iters):
        mu = c + d * offs
        l = -(F[None, :] - mu[:, None]) ** 2 / (2 * s2)
        r = np.exp(l - l.max(axis=0))
        r /= r.sum(axis=0)
        wts = r.sum(axis=1)
        # weighted least squares for (c, d)
        M = (A * wts[:, None]).T @ A
        v = A.T @ (r @ F)
        try:
            c, d = np.linalg.solve(M, v)
        except np.linalg.LinAlgError:
            break
        s2 = max(float(np.sum(r * (F[None, :] - (c + d * offs)[:, None]) ** 2) / F.size),
                 (1e-2 * abs(d)) ** 2, 1e-12)     # caps the tone separation at ~34 dB
    mu = c + d * offs
    l = -(F[None, :] - mu[:, None]) ** 2 / (2 * s2)
    lm = l.max(axis=0)
    ll = np.mean(lm + np.log(np.exp(l - lm).sum(axis=0))) - np.log(offs.size) \
        - 0.5 * np.log(2 * np.pi * s2)
    return float(ll), float(c), float(abs(d)), s2


def _fsk_classify(x: np.ndarray, fs: float, info: dict, spans=None):
    """FSK branch, carrier-free: the per-symbol phase advance is the tone
    frequency, so a CFO only shifts every tone equally and phase noise does not
    matter. Returns (label, confidence) or None.

    Only a light band-limit (to ~8x the 98 % power width): a low-index GFSK
    (h ~0.2, BT 0.3) keeps 98 % of its power well inside one symbol rate, and
    the 2.5x cut the linear branch uses would remove its symbol transitions."""
    pw = power_bw(x, fs)
    # power_bw's floor (20th-percentile PSD) leaves the noise of a
    # wide selection in the 98 % width: the bench GFSK (h ~0.2, BT 0.3, ~1.4 kHz
    # at -12 dB) in its 60 kHz selection read ~12 kHz at Es/N0 20 dB, so the
    # cut did nothing and inst-freq noise buried every symbol-rate line. Use
    # the smaller of it and the -12 dB width (contiguous around the peak, not
    # stretched by noise), and centre the cut on the occupied band (this branch
    # is carrier-free: a frequency shift moves every tone equally).
    fc, ob = _occupied_span(x, fs)
    w = min(pw, ob)
    if fc:
        x = x * np.exp(-2j * np.pi * fc / fs * np.arange(x.size))
    xs, fss = _band_limit_to(x, fs, 8.0 * w) if fs > 16.0 * w else (x, fs)
    # Run spans of a looped burst, rescaled to fss (None when the
    # capture is one continuous run)
    runs = None
    if spans is not None and len(spans) > 1:
        q = fss / fs
        runs = [(int(np.ceil(a * q)), int(np.floor(b * q))) for a, b in spans]
    dphi = np.angle(xs[1:] * np.conj(xs[:-1]))
    inst = dphi * fss / (2 * np.pi)
    sm = np.convolve(inst, np.ones(3) / 3, mode="same")
    # tone changes: |d f/dt|^2 peaks at a sharp (rectangular) transition, while
    # (f - centre)^2 dips through zero at a smooth (Gaussian) one; the symbol
    # rate is searched up to 4x the 98 % power width (a low-index GFSK keeps
    # its power inside a fraction of one symbol rate)
    bw = power_bw(xs, fss)
    fmin, fmax = max(fss / 400.0, bw / 60.0), min(0.45 * fss, 4.0 * bw)
    rs, prom = 0.0, 0.0
    xband = None
    gs = [np.abs(np.diff(sm)) ** 2, (sm - np.median(sm)) ** 2]
    # Gaussian shaping (BT 0.3) leaves almost no symbol-rate line
    # in either statistic above, but the frequency still crosses its centre
    # only at symbol boundaries. The crossing train of the inst freq of a copy
    # filtered to +/-1.5x the -12 dB width (most of the noise gone, crossings
    # kept in place) carries that line.
    if 3.0 * ob < 0.9 * fss and xs.size > 512:
        try:
            import scipy.signal as _ss
            xf = np.convolve(xs, _ss.firwin(129, 1.5 * ob, fs=fss), mode="same")
            fz = np.convolve(np.angle(xf[1:] * np.conj(xf[:-1])), np.ones(3) / 3, mode="same")
            sb = np.signbit(fz - np.median(fz))
            gs.append((sb[1:] != sb[:-1]).astype(float))
            # A low-index MFSK (DMR-class 4FSK, h ~0.27) at 15-20 dB
            # has no symbol line in the statistics of the raw inst freq: the
            # noise of a selection several times wider than the signal swamps
            # tones only ~1.3 kHz apart. The same two statistics from a copy cut
            # to the signal band (+/-0.6x its width) and an inst freq smoothed
            # over ~1/width (the inst-freq noise rises with frequency) keep it.
            wn = min(pw, 1.5 * ob)
            xn = np.convolve(xs, _ss.firwin(129, min(0.6 * wn, 0.45 * fss), fs=fss), mode="same")
            xband = xn
            ln = max(3, int(fss / wn))
            fn = np.convolve(np.angle(xn[1:] * np.conj(xn[:-1])), np.ones(ln) / ln, mode="same")
            gs += [np.abs(np.diff(fn)) ** 2, (fn - np.median(fn)) ** 2]
        except Exception:                                            # noqa: BLE001
            pass
    if runs is not None:
        # Each join (and the filters' transients around it) puts a
        # spike into every statistic once per burst, a frame-rate comb whose
        # harmonics can outrank the symbol line; keep only the run interiors
        # (zero-mean there, so the mask adds no lines of its own); 70 samples
        # covers half the 129-tap band filters above
        ok = np.zeros(max(g.size for g in gs), bool)
        for a, b in runs:
            ok[max(0, a + 70):max(0, b - 70)] = True
        if ok.sum() >= 256:
            gs = [np.where(ok[:g.size], g - np.mean(g[ok[:g.size]]), 0.0) for g in gs]
    lines = []
    for g in gs:
        f_, p_ = _cyclic_line(g, fss, fmin, fmax, walk=False) if fmax > fmin else (0.0, 0.0)
        if f_ > 0:
            lines.append((p_, f_))
            if runs is not None:
                lines += _cyclic_peaks(g, fss, fmin, fmax, 3)[1:]
        if f_ > 0 and p_ > prom:
            rs, prom = f_, p_
    info["fsk_baud_hz"], info["fsk_baud_prom"] = rs, prom
    if rs <= 0:
        return _fsk_rate_scan(xs, fss, fmin, fmax, sm, info, runs)
    # The strongest line may be a harmonic of the symbol rate, or a sub-multiple
    # put there by a periodic preamble (alternating tones: Rs/2). Per-symbol
    # tone clusters are tightest at the true rate (a longer window averages
    # two tones into extra levels, a shorter one is noisier), so try the
    # neighbours and keep the tightest.
    # A looped burst repeats the same few dozen symbols, so the
    # data itself is periodic and every statistic carries a comb at the burst
    # rate; its strongest tooth can beat the symbol line in one statistic
    # (the tone deviation (f - c)^2 above all) while another still has the
    # symbol line on top. Every statistic's line is a candidate, and with
    # bursts the next two lines of each as well.
    cands = []
    for _p, f0 in sorted(lines, reverse=True):
        if all(abs(f0 - c0) > 0.02 * c0 for c0 in cands):
            cands.append(f0)
    best = None
    tried = []
    for f0 in cands:
        for mult in (1.0, 0.5, 2.0, 1 / 3, 3.0, 0.25, 4.0):
            r = f0 * mult
            if not (fmin <= r <= 0.45 * fss) or any(abs(r - t) <= 0.003 * t for t in tried):
                continue
            tried.append(r)
            sub = {}
            res = _fsk_at(xs, fss, r, sub, runs)
            if res is not None and (best is None or sub.get("fsk_sep_db", -99) > best[2].get("fsk_sep_db", -99)):
                best = (res, r, sub)
    if best is None:
        return _fsk_rate_scan(xs, fss, fmin, fmax, sm, info, runs)
    if runs is not None:
        # the run mask splits every line into teeth at the burst rate, so the
        # line read-out can be off by up to half that; timing drifts across a
        # run at the wrong rate, so refine on the tone separation
        r0 = best[1]
        for r in r0 * (1.0 + np.linspace(-0.03, 0.03, 13)):
            sub = {}
            res = _fsk_at(xs, fss, float(r), sub, runs)
            if res is not None and sub.get("fsk_sep_db", -99) > best[2].get("fsk_sep_db", -99):
                best = (res, float(r), sub)
    if best[0][0] != "4FSK" and xband is not None:
        # A low-index 4FSK at Es/N0 ~15 dB (DMR-class, tones
        # 1.3 kHz apart) has its four per-symbol levels merged into two by
        # the noise of a capture several times its width; on the copy cut to
        # its band they separate. Only the order is taken from that copy, and
        # only when the levels do not follow the neighbouring symbols: band
        # cutting (or Gaussian shaping) also gives a 2FSK inner levels, set
        # by its neighbours (fsk_pr4, ~0 for independent 4FSK symbols).
        sub = {}
        res = _fsk_at(xband, fss, best[1], sub, runs)
        if res is not None and res[0] == "4FSK" and abs(sub.get("fsk_pr4", 1.0)) < 0.1:
            sub["fsk_order_from_band"] = True
            best = (res, best[1], sub)
    info.update(best[2])
    info["fsk_baud_hz"] = best[1]
    return best[0]


def _fsk_rate_scan(xs, fss, fmin, fmax, sm, info, runs=None):
    """No usable cyclic line (a low-index GFSK, h ~0.2 with BT 0.3, has almost
    none): scan candidate symbol rates and keep the one whose per-symbol tones
    separate best; the inst-freq histogram is the last resort."""
    best = None
    if fmax > fmin:
        for r in np.geomspace(max(fmin, 1.0), fmax, 28):
            sub = {}
            res = _fsk_at(xs, fss, float(r), sub, runs)
            if res is not None and (best is None or sub["fsk_sep_db"] > best[2]["fsk_sep_db"]):
                best = (res, float(r), sub)
    if best is None:
        return _fsk_no_baud(sm, info)
    # refine around the best grid point
    r0 = best[1]
    for r in np.linspace(0.92 * r0, 1.08 * r0, 9):
        sub = {}
        res = _fsk_at(xs, fss, float(r), sub, runs)
        if res is not None and sub["fsk_sep_db"] > best[2]["fsk_sep_db"]:
            best = (res, float(r), sub)
    info.update(best[2])
    info["fsk_baud_hz"], info["fsk_rate_scan"] = best[1], True
    return best[0]


def _fsk_no_baud(sm: np.ndarray, info: dict):
    """No symbol rate (heavily Gaussian-smoothed GFSK, BT ~0.15, has almost no
    cyclic line): read the order off the inst-freq histogram. Smooth
    transitions leave much of the time between the tones: GFSK."""
    f = sm[np.isfinite(sm)]
    if f.size < 512:
        return None
    lo, hi = np.percentile(f, [5, 95])
    if hi <= lo:
        return None
    h, e = np.histogram(np.clip(f, lo, hi), bins=40)
    h = np.convolve(h.astype(float), np.ones(3) / 3, mode="same")
    pk = [i for i in range(1, 39) if h[i] >= h[i - 1] and h[i] >= h[i + 1] and h[i] > 0.4 * h.max()]
    peaks = [pk[0]] if pk else []
    for i in pk[1:]:
        if i - peaks[-1] >= 4:
            peaks.append(i)
    info["fsk_hist_modes"] = len(peaks)
    if len(peaks) < 2:
        return None
    if len(peaks) >= 3:
        return "4FSK", 0.4
    a, b = e[peaks[0]], e[peaks[-1]]
    mid = float(np.mean((f > a + 0.25 * (b - a)) & (f < b - 0.25 * (b - a))))
    info["fsk_mid_frac"] = mid
    return ("GFSK" if mid > 0.25 else "FSK"), 0.4


def _fsk_at(xs: np.ndarray, fss: float, rs: float, info: dict, runs=None):
    """Per-symbol tone fit at symbol rate rs (see _fsk_classify). runs: the
    burst spans (samples at fss) of a looped burst; timing is recovered per
    run, since it restarts at every join."""
    K = 8
    nmax = int(4000 * fss / rs)
    xr = _resample_to(xs[:nmax], fss, K * rs)
    a = np.angle(xr[1:] * np.conj(xr[:-1]))
    c = np.concatenate([[0.0], np.cumsum(a)])
    Fw = (c[K:] - c[:-K]) * rs / (2 * np.pi)          # mean freq over one symbol
    edge = 2 * K
    Fw = Fw[edge:-edge] if Fw.size > 4 * edge else Fw
    if Fw.size < 32 * K:
        return None
    med = np.median(Fw)
    if runs is None:
        tau = _block_timing((Fw - med) ** 2, K, 64)
        k = np.arange(0, Fw.size, K, dtype=float)
        F = _sample_at(Fw, k + np.interp(k, np.arange(Fw.size), tau) % K)
    else:
        # Timing per run, a symbol of margin at each end (filter
        # and resampler transients across the join), tracked over 16-symbol
        # blocks so a rate read off a line comb tooth (~0.5 % out) still lines
        # up across a whole run
        q = K * rs / fss
        parts = []
        for a0, b0 in runs:
            lo = int(np.ceil(a0 * q)) - edge + 2 * K
            hi = int(np.floor(min(b0, nmax) * q)) - edge - 2 * K
            lo, hi = max(lo, 0), min(hi, Fw.size)
            if hi - lo < 8 * K:
                continue
            seg = Fw[lo:hi]
            tau = _block_timing((seg - med) ** 2, K, 16)
            k = np.arange(0, seg.size - K, K, dtype=float)
            parts.append(_sample_at(seg, k + np.interp(k, np.arange(seg.size), tau) % K))
        if sum(p.size for p in parts) < 32:
            return None
        F = np.concatenate(parts)
    ll2, c2, d2, s22 = _fit_fsk(F, np.array([-0.5, 0.5]))
    ll4, c4, d4, s24 = _fit_fsk(F, np.array([-1.5, -0.5, 0.5, 1.5]))
    info["fsk_ll2"], info["fsk_ll4"] = ll2, ll4
    info["sym_rate_hz"] = rs
    if ll4 > ll2 + 0.1 and d4 > 4 * np.sqrt(s24):
        if F.size > 64:
            # level vs the neighbours' signs (partial response; ~0 for 4FSK)
            sg = np.where(F > c4, 1.0, -1.0)
            z = F[1:-1] - c4
            a_, b_ = np.linalg.lstsq(np.stack([np.sign(z) * np.abs(z).mean(),
                                               sg[:-2] + sg[2:]], axis=1), z, rcond=None)[0]
            info["fsk_pr4"] = float(b_ / (a_ * np.abs(z).mean())) if a_ > 0 else 1.0
        info["tone_spacing_hz"] = d4
        info["fsk_sep_db"] = float(20 * np.log10(d4 / (2 * np.sqrt(s24))))
        return "4FSK", min(1.0, 0.5 + (ll4 - ll2))
    if d2 < 3 * np.sqrt(s22) and not _pr_resolvable(F, c2, info):
        return None                      # no resolvable tones
    info["tone_spacing_hz"] = d2
    # tone separation in the same terms as a constellation Es/N0: half the
    # spacing over the per-symbol frequency noise
    info["fsk_sep_db"] = float(20 * np.log10(d2 / (2 * np.sqrt(s22))))
    # GFSK: Gaussian shaping pulls a symbol whose neighbours differ toward the
    # centre (ISI); rectangular FSK reaches the full deviation either way.
    b = F > c2
    amp = np.abs(F - c2) / (d2 / 2)
    same = (b[1:-1] == b[:-2]) & (b[1:-1] == b[2:])
    diff = ~same
    if same.sum() > 16 and diff.sum() > 16:
        isi = float(np.mean(amp[1:-1][same]) - np.mean(amp[1:-1][diff]))
        info["fsk_isi"] = isi
        if isi > 0.1:
            return "GFSK", min(1.0, 0.5 + isi)
    return "FSK", min(1.0, 0.5 + (ll2 - ll4) + 0.2)


def _pr_resolvable(F: np.ndarray, c: float, info: dict) -> bool:
    """Gaussian ISI (partial response) spreads each tone into
    sub-levels set by the neighbours (s_k + b*(s_k-1 + s_k+1)), so a GFSK whose
    two tones are clear can still fail the plain 2-level fit. Fit b
    (decision-directed, b ~0.2 for h ~0.2 BT 0.3, ~0 for rectangular FSK);
    when the ISI is clear, take it out and re-test the tones."""
    if F.size < 64:
        return False
    z = F - c
    sg = np.where(z > 0, 1.0, -1.0)
    nb = sg[:-2] + sg[2:]
    a, b = np.linalg.lstsq(np.stack([sg[1:-1], nb], axis=1), z[1:-1], rcond=None)[0]
    info["fsk_pr_b"] = float(b / a) if a > 0 else 0.0
    if a <= 0 or b < 0.1 * a:
        return False
    _ll, _c, de, se = _fit_fsk(F[1:-1] - b * nb, np.array([-0.5, 0.5]))
    return bool(de >= 3 * np.sqrt(se))


def _top_frac(blocks: np.ndarray) -> float:
    """Mean fraction of each block's energy in its four strongest FFT bins (a
    block straddling a symbol edge, or a chirp that wraps at the band edge,
    dechirps into up to four tones)."""
    P = np.abs(np.fft.fft(blocks, axis=1)) ** 2
    top = np.sort(P, axis=1)[:, -4:].sum(axis=1)
    return float(np.mean(top / (P.sum(axis=1) + 1e-30)))


CSS_MIN = 0.15      # no linear/FSK capture has scored above 0.02


def css_score(x: np.ndarray, fs: float) -> tuple[float, int, float]:
    """Chirp spread spectrum (LoRa) test. For a symbol time Ts = 2^SF / bw,
    multiplying by a down-chirp of rate bw/Ts turns every CSS symbol into a
    single tone, so Ts-long blocks become peaky; an FSK tone turns into a
    full-band sweep instead, and PSK stays spread. Score = best (over SF 5..12
    and both sweep directions) dechirped top-4-bin energy fraction minus the
    raw one, over the -3/-6/-12 dB bandwidth readings. Carrier offset only
    moves the tone. Returns (score, sf, bw_hz)."""
    t = np.arange(x.size) / fs
    best = (0.0, 0, 0.0)
    # the chirp sweeps exactly bw; a flat CSS spectrum reads that at -3/-6 dB,
    # while interpolation images can widen the -12 dB reading
    for bw in sorted({round(occupied_bw(x, fs, db), 0) for db in (3.0, 6.0, 12.0)}):
        if bw <= 0:
            continue
        for sf in range(5, 13):
            ts = 2 ** sf / bw
            n = int(round(ts * fs))
            if n < 16 or x.size < 4 * n:
                continue
            nb = min(x.size // n, 64)
            raw = _top_frac(x[:nb * n].reshape(nb, n))
            for sg in (1.0, -1.0):
                d = x[:nb * n] * np.exp(-1j * np.pi * sg * (bw / ts) * t[:nb * n] ** 2)
                sc = _top_frac(d.reshape(nb, n)) - raw
                if sc > best[0]:
                    best = (sc, sf, bw)
    return float(best[0]), int(best[1]), float(best[2])


def _seg_power(y: np.ndarray, fs: float, L: int, f: float) -> float:
    """Welch power of y at one frequency (same segmentation as _line)."""
    seg = _segments(y, int(min(L, y.size)))
    e = np.exp(-2j * np.pi * f / fs * np.arange(seg.shape[1]))
    return float(np.mean(np.abs(seg @ e) ** 2))


def _pi4_twin(xs: np.ndarray, fss: float, L: int, f4: float, weighting: str) -> float:
    """pi/4-QPSK's 4th power is +/-1 alternating every symbol, so instead of
    one line at 4*CFO it shows two equal lines at 4*CFO +/- Rs/2. If the
    strongest 4th-power line has such a twin one symbol rate away, return the
    midpoint (the true 4*CFO); otherwise return f4 unchanged."""
    rs, _ = baud_rate(xs, fss)
    if rs <= 0:
        return f4
    xn = xs / np.maximum(np.abs(xs), 1e-12)
    y = xn ** 4 if weighting == "phase" else xs ** 4
    p0 = _seg_power(y, fss, L, f4)
    for sg in (-1.0, 1.0):
        # search a few bins around the twin position for its peak
        cand = [f4 + sg * rs + d * fss / (4 * L) for d in range(-3, 4)]
        pw = [_seg_power(y, fss, L, f) for f in cand]
        if max(pw) > 0.4 * p0:
            return 0.5 * (f4 + cand[int(np.argmax(pw))])
    return f4


def _active(x: np.ndarray, fs: float) -> np.ndarray:
    """The part of the capture that holds the signal. A capture of a looped TX
    burst (or a brushed span) has silent gaps; symbols taken from a gap are
    noise. Returns the burst runs (each at least half the longest) joined."""
    n = x.size
    if n < 4096:
        return x
    w = max(64, n // 400)
    p = np.abs(x) ** 2
    env = np.convolve(p, np.ones(w) / w, mode="same")
    hi = np.percentile(env, 90)
    on = env > 0.25 * hi
    # a gap = at least one window-length stretch well below the burst level
    # (it may be only a few % of the capture: a looped frame + short gap)
    off = np.convolve((~on).astype(float), np.ones(w), mode="same") >= w
    if not off.any():
        return x                 # continuous signal (or all noise)
    on = np.concatenate([[False], on, [False]])
    edges = np.flatnonzero(np.diff(on.astype(np.int8)))
    runs = [(a + w, b - w) for a, b in zip(edges[::2], edges[1::2]) if b - a > 2 * w]
    if not runs:
        return x
    longest = max(b - a for a, b in runs)
    if longest < 512:
        return x
    # a looped burst gives several equal runs: keep every run at least half
    # the longest (more symbols; the trackers re-lock at each join), in order
    keep = [x[a:b] for a, b in runs if b - a >= 0.5 * longest]
    return np.concatenate(keep)


def _active_runs(x: np.ndarray, fs: float):
    """The FSK branch's version of _active. Returns the joined
    runs and the (start, end) span of each inside the joined array: symbol
    timing restarts at every join (the gaps are not a whole number of
    symbols), so per-symbol statistics are taken per run. It also finds the
    short gaps of narrow survey cuts that _active misses; the linear branch
    keeps _active (a summed CDMA overlay is exactly zero for a few chips
    often enough to look gapped here, and the code test needs it whole)."""
    n = x.size
    whole = [(0, n)]
    if n < 1024:          # (a survey cut of a 1200 Bd burst: 2000 samples in 0.2 s)
        return x, whole
    # Gaps are looked for on a copy cut to the occupied band: a
    # selection 5x the signal width at 15 dB in-band SNR is only ~3 dB SNR
    # across the capture, too little for the on/off envelope. That copy needs
    # only ~4/width of smoothing (w), and a gap need only last w: a 9600 Bd
    # burst's ~1 ms gap is ~36 samples at a 30 kHz survey cut, ~20 of them
    # below threshold after smoothing. Runs lie between such gaps, so a short
    # dip inside a burst (a few inner 16QAM/APSK symbols in a row) never
    # splits it.
    fc, ob = _occupied_span(x, fs)
    w = int(np.clip(4.0 * fs / max(ob, 1.0), 16, max(64, n // 400)))
    xe = x
    if 1.5 * ob < 0.45 * fs:
        try:
            import scipy.signal as _ss
            taps = int(min(511, 2 * int(4.0 * fs / ob) + 1))
            xe = np.convolve(x * np.exp(-2j * np.pi * fc / fs * np.arange(n)),
                             _ss.firwin(taps, 0.75 * ob, fs=fs), mode="same")
        except Exception:                                            # noqa: BLE001
            xe = x
    p = np.abs(xe) ** 2
    env = np.convolve(p, np.ones(w) / w, mode="same")
    hi = np.percentile(env, 90)
    low = np.concatenate([[False], env <= 0.25 * hi, [False]])
    e = np.flatnonzero(np.diff(low.astype(np.int8)))
    # a gap = a stretch at least w long well below the burst level (it may be
    # only a few % of the capture: a looped frame + short gap)
    gaps = [(a, b) for a, b in zip(e[::2], e[1::2]) if b - a >= w]
    if not gaps:
        return x, whole          # continuous signal (or all noise)
    bounds = [0] + [v for gp in gaps for v in gp] + [n]
    runs = [(a + w, b - w) for a, b in zip(bounds[::2], bounds[1::2]) if b - a > 2 * w]
    if not runs:
        return x, whole
    longest = max(b - a for a, b in runs)
    if longest < max(128, 4 * w):     # (a DMR-class burst is ~320 samples at 30 kHz)
        return x, whole
    # a looped burst gives several equal runs: keep every run at least half
    # the longest (more symbols; the trackers re-lock at each join), in order
    keep = [x[a:b] for a, b in runs if b - a >= 0.5 * longest]
    ends = np.cumsum([k.size for k in keep])
    return np.concatenate(keep), list(zip(np.r_[0, ends[:-1]].tolist(), ends.tolist()))


DSSS_FSK_SEP_DB = 18.0     # FSK tone separation no DSSS capture reaches (<= ~11 dB)


def _fsk_beats_linear(fsk_info: dict, lin_info: dict, lin_label: str = "",
                      fsk_label: str = "") -> bool:
    """Which explanation wins when both branches found one.
    A constant-envelope MFSK can fit a QAM/APSK ring (or, h >> 1, show strong
    M-th power lines from its tones) well enough to pass the constellation
    branch, so the FSK tone separation is always compared, not only when the
    constellation fit is poor:
      - a DSSS code-periodicity hit stands unless the tones are far cleaner
        than any spread signal gives (real DSSS: <= ~11 dB);
      - otherwise FSK wins unless its separation is more than 3 dB below the
        constellation Es/N0 (pi/4-QPSK, whose 45-degree turns look like 4
        tones, sits 3-9 dB below), and, when a strong symbol-level 4th/8th
        power line backs the constellation, only if it is at least as good;
      - pi/4-QPSK steps by +-45/+-135 degrees, which per symbol are four
        tones at h = 0.25 (at half its rate, 0/+-90/180 degrees: four again),
        so a 4FSK read at its rate or half of it needs a 3 dB better fit."""
    sep = fsk_info.get("fsk_sep_db", -99.0)
    if lin_info.get("decisive"):
        return sep >= DSSS_FSK_SEP_DB
    esn0 = lin_info.get("esn0_db", 0.0)
    if sep < esn0 - 3.0:
        return False
    rl, rf = lin_info.get("sym_rate_hz", 0.0), fsk_info.get("fsk_baud_hz", 0.0)
    if lin_label == "pi/4-QPSK" and fsk_label == "4FSK" and rl > 0 and rf > 0 \
            and min(abs(rf / rl - 1.0), abs(2.0 * rf / rl - 1.0)) < 0.05 and sep < esn0 + 3.0:
        return False
    lines = lin_info.get("sym_line_prom") or {}
    if max(lines.get(4, 0.0), lines.get(8, 0.0)) >= STRONG_LINE_PROM and sep < esn0:
        return False
    return True


def _box_guard(x: np.ndarray, fs: float, band_hz: float) -> tuple[np.ndarray, bool]:
    """Keep the classifier on the brushed box. classify_selection
    shifts the box to DC and decimates by whole factors, which can leave the
    capture up to ~5x the box wide, so a neighbouring channel just outside the
    box (mod_coding_card: 100 kHz apart, as strong as the target) sits in the
    capture and takes over every spectral estimate. Only when the capture
    holds a lobe wholly outside the box more than 10 dB over the in-box noise
    floor is it filtered (to the box, widened to keep any lobe of the target
    that crosses the box edge); otherwise it is returned untouched."""
    n = x.size
    if not band_hz or band_hz <= 0 or band_hz >= 0.8 * fs or n < 4096:
        return x, False
    L = 1024
    S = np.fft.fftshift(np.mean(np.abs(np.fft.fft(_segments(x, L) * np.hanning(L), axis=1)) ** 2, axis=0))
    S = np.convolve(S, np.ones(5) / 5, mode="same")
    f = np.fft.fftshift(np.fft.fftfreq(L, 1.0 / fs))
    inside = np.abs(f) <= 0.5 * band_hz
    if inside.sum() < 8:
        return x, False
    # noise floor: the box is mostly noise when drawn wider than the signal,
    # the whole capture (>= 2.5x the box) when it is drawn too tight
    floor = min(float(np.percentile(S[inside], 20)), float(np.median(S)))
    # lobes = runs > 6 dB over the floor; one that reaches into the box is the
    # target (or spills over the box edge: it is kept whole), one that lies
    # wholly outside it is a neighbour
    above = np.concatenate([[False], S > 4.0 * floor, [False]])
    edges = np.flatnonzero(np.diff(above.astype(np.int8)))
    keep_hz, neighbour = 0.5 * band_hz, False
    for a, b in zip(edges[::2], edges[1::2]):
        if inside[a:b].any():
            keep_hz = max(keep_hz, float(np.abs(f[a:b]).max()))
        elif S[a:b].max() > 10.0 * floor:
            neighbour = True
    if not neighbour or keep_hz >= 0.45 * fs:
        return x, False
    import scipy.signal as _ss
    taps = int(min(1023, 2 * int(20.0 * fs / band_hz) + 1))
    h = _ss.firwin(taps, min(1.1 * keep_hz, 0.45 * fs), fs=fs)
    return np.convolve(x, h, mode="same"), True


def classify(iq: np.ndarray, fs: float, band_hz: float | None = None) -> dict:
    """Blind modulation classification of an IQ slice captured at fs, with
    carrier recovery (see the block comment above). Returns {modulation,
    family, confidence (0-1), sym_rate_hz, carrier, features}. The legacy
    feature vector stays under 'features' for the GUI/service read-outs.
    band_hz: width of the brushed box centred on DC, when the caller has one
    (classify_selection); energy outside it is then kept out (_box_guard)."""
    iq = np.asarray(iq)
    boxed = False
    if band_hz:
        iq, boxed = _box_guard(iq, fs, float(band_hz))
    x = _norm(_active(iq, fs))
    if x.size > 131072:
        x = x[:131072]
    # FSK branch input: DC removed on the whole capture, before the burst runs
    # are joined. The runs of a looped burst carry the same symbols, so the
    # joined signal is periodic and can hold a real spectral line at 0 Hz (35 %
    # of the rms for DMR 4FSK brushed 800 Hz off centre); subtracting the
    # joined mean took that line out and smeared every per-symbol tone.
    xk = np.asarray(iq, dtype=np.complex128)
    if xk.size:
        xk = xk - xk.mean()
    xk, spans = _active_runs(xk, fs)
    if xk.size:
        xk = xk / (np.sqrt(np.mean(np.abs(xk) ** 2)) or 1.0)
    if xk.size > 131072:
        xk = xk[:131072]
        spans = [(a, min(b, xk.size)) for a, b in spans if a < xk.size]
    f = features(x, fs)
    carrier = {"cfo_hz": 0.0, "order": None}
    if f["n"] < 256:
        return {"modulation": "unknown", "family": "unknown", "confidence": 0.0,
                "sym_rate_hz": 0.0, "carrier": carrier, "features": f}
    info: dict = {"box_filtered": boxed} if boxed else {}
    # 1) coarse carrier: M-th power line on a band-limited copy. A strong line
    #    here (PSK) is unambiguous over +/- fs/2M; weaker constellations are
    #    picked up again at the symbol level inside _linear_classify.
    xs, fss = _band_limit(x, fs)
    L = int(min(1024, max(64, xs.size // 4)))
    lines = mth_power_lines(xs, fss, L)
    m = pick_order(lines, fss, L)
    cfo = 0.0
    if m is not None:
        fl = lines[m][1]
        if m == 4:
            fl = _pi4_twin(xs, fss, L, fl, lines[4][2])
        cfo = fl / m
        carrier.update(coarse_order=m, coarse_cfo_hz=round(float(cfo), 2))
    xc = x * np.exp(-2j * np.pi * cfo / fs * np.arange(x.size)) if cfo else x
    label = None
    # 2) CSS: dechirping concentrates each symbol into one tone (checked
    #    first: a chirp train has enough structure to look periodic to the
    #    linear branch, while no linear modulation dechirps into tones)
    xcs, fcs = _band_limit(x, fs, 2.0)
    cs, sf, bw = css_score(xcs, fcs)
    info.update(css_score=cs, css_sf=sf, css_bw_hz=bw)
    if cs > CSS_MIN:
        label, conf = "CSS", min(1.0, 0.5 + cs)
        info["sym_rate_hz"] = bw / 2 ** sf
    # 3) linear constellations vs the FSK family: both explanations are built
    #    and _fsk_beats_linear picks one (a constant-envelope MFSK can fit an
    #    APSK ring at 12-20 dB, a PSK never shows separated tones). If a fit
    #    at Es/N0 >= LIN_SURE_DB won without the FSK check, looped FSK presets
    #    would read as 16QAM/32APSK.
    lin_info, fsk_info = {}, {}
    lin = _linear_classify(xc, fs, lin_info) if label is None else None
    fsk = None
    if label is None:
        fsk = _fsk_classify(xk, fs, fsk_info, spans)
        if fsk is not None and lin is not None and \
                not _fsk_beats_linear(fsk_info, lin_info, lin[0], fsk[0]):
            fsk = None
    if fsk is not None:
        label, conf = fsk
        info.update(fsk_info)
        info.update({k: v for k, v in lin_info.items() if k not in info})
    elif lin is not None:
        label, conf = lin
        info.update(lin_info)
        info.update({k: v for k, v in fsk_info.items() if k not in info})
        carrier.update(cfo_hz=round(float(cfo + info.get("fine_cfo_hz", 0.0)), 2),
                       order=info["order"], weighting=info["weighting"],
                       phase_track_std_deg=round(info.get("phase_track_std_deg", 0.0), 2),
                       esn0_db=round(info.get("esn0_db", 0.0), 1))
    else:
        info.update(lin_info)
        info.update(fsk_info)
    if label is None:
        legacy = classify_legacy(iq, fs)
        label, conf = legacy["modulation"], min(0.3, legacy["confidence"])
        if label == "APSK":
            label = "16APSK"
        info["fallback"] = "legacy"
    # 'features' rides the service's classify_selection ack (one <1400 B
    # datagram), so it carries the legacy vector plus a few carrier fields;
    # everything else is under 'detail' (not sent over the link)
    feats = dict(f)
    for k in _WIRE_KEYS:
        v = carrier.get(k[4:]) if k.startswith("car_") else info.get(k)
        if v is not None:
            feats[k] = round(v, 2) if isinstance(v, float) else v
    return {"modulation": label, "family": FAMILY.get(label, "unknown"),
            "confidence": round(float(conf), 2),
            "sym_rate_hz": round(float(info.get("sym_rate_hz", f["sym_rate_hz"])), 1),
            "carrier": carrier, "features": feats,
            "detail": {k: v for k, v in info.items()}}


_WIRE_KEYS = ("car_cfo_hz", "car_order", "esn0_db", "baud_hz", "css_score", "dsss_L")
