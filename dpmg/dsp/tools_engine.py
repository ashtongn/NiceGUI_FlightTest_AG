"""
tools_engine.py: test-signal generators + IQ measurements for the Tools tab.

Pure numpy so it runs anywhere (service and offline). Generators return complex64
baseband IQ at fs (the TX LO shifts it to RF); measurements take captured IQ and
return the numbers a ground checkout / signal survey needs.
"""
from __future__ import annotations

import numpy as np


# Fraction of the sample rate the NV100 passes cleanly: the outer ~20% is the
# anti-alias / reconstruction filter roll-off, so usable baseband is +/-0.4*fs
# (0.8*fs wide). The generator defaults (chirp f0/f1 = -/+0.4*fs, sweep span =
# 0.8*fs) assume this; the waveform validator guards against it.
USABLE_FRACTION = 0.8


# test-signal generators (baseband, complex64 @ fs)

def lfm_chirp(fs: float, f0: float, f1: float, dur_s: float,
              ampl: float = 0.7) -> np.ndarray:
    """Linear-FM (linear chirp): sweeps f0: f1 over dur_s. f0/f1 are baseband
    offsets from the TX LO. Instantaneous phase is the integral of the linear
    frequency ramp, so the tone is continuous at the sweep endpoints."""
    n = max(1, int(round(fs * dur_s)))
    t = np.arange(n) / fs
    k = (f1 - f0) / max(dur_s, 1e-12)          # Hz/s sweep rate
    phase = 2.0 * np.pi * (f0 * t + 0.5 * k * t * t)
    return (ampl * np.exp(1j * phase)).astype(np.complex64)


def cw_tone(fs: float, freq: float, dur_s: float,
            ampl: float = 0.7) -> np.ndarray:
    n = max(1, int(round(fs * dur_s)))
    t = np.arange(n) / fs
    return (ampl * np.exp(2j * np.pi * freq * t)).astype(np.complex64)


def two_tone(fs: float, f1: float, f2: float, dur_s: float,
             ampl: float = 0.5) -> np.ndarray:
    """Two equal tones: the classic IMD/linearity stimulus (3rd-order products
    appear at 2*f1-f2 and 2*f2-f1)."""
    n = max(1, int(round(fs * dur_s)))
    t = np.arange(n) / fs
    s = np.exp(2j * np.pi * f1 * t) + np.exp(2j * np.pi * f2 * t)
    return (ampl * s).astype(np.complex64)


def band_noise(fs: float, bw: float, dur_s: float, ampl: float = 0.3,
               seed: int = 1) -> np.ndarray:
    """Band-limited complex Gaussian noise (brick-wall in the FFT to +-bw/2)."""
    n = max(1, int(round(fs * dur_s)))
    rng = np.random.default_rng(seed)
    x = (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64)
    X = np.fft.fft(x)
    f = np.fft.fftfreq(n, 1.0 / fs)
    X[np.abs(f) > bw / 2.0] = 0
    y = np.fft.ifft(X)
    p = float(np.sqrt(np.mean(np.abs(y) ** 2)) or 1.0)
    return (ampl * y / p).astype(np.complex64)


# jamming waveforms (for in-chamber resiliency testing)

def barrage_noise(fs: float, dur_s: float, ampl: float = 0.3) -> np.ndarray:
    """Full-band barrage noise: AWGN across (nearly) the whole tuned band."""
    return band_noise(fs, fs * 0.9, dur_s, ampl)


def comb_jam(fs: float, spacing: float, n_tones: int, dur_s: float,
             ampl: float = 0.4, center: float = 0.0) -> np.ndarray:
    """A frequency comb: n_tones equal CW tones spaced `spacing` Hz, centered at
    `center` (baseband offset). Peak-normalized so it fills the DAC without
    clipping regardless of tone count."""
    n = max(1, int(round(fs * dur_s)))
    t = np.arange(n) / fs
    n_tones = max(1, int(n_tones))
    offs = (np.arange(n_tones) - (n_tones - 1) / 2.0) * spacing + center
    # tones on whole FFT bins build in one inverse FFT; a loop over tones takes
    # 12 s for 128 tones and 3 M samples, most of a mission step on the radio
    bins = offs * n / fs
    if np.all(np.abs(bins - np.round(bins)) < 1e-6) and np.all(np.abs(offs) < fs / 2.0):
        X = np.zeros(n, dtype=np.complex128)
        np.add.at(X, np.round(bins).astype(np.int64) % n, 1.0)
        s = np.fft.ifft(X) * n
    else:
        s = np.zeros(n, dtype=np.complex128)
        for f in offs:
            s += np.exp(2j * np.pi * f * t)
    s /= (np.max(np.abs(s)) or 1.0)
    return (ampl * s).astype(np.complex64)


def swept_jam(fs: float, span: float, sweep_period_s: float, dur_s: float,
              ampl: float = 0.6, noise_bw: float = 0.0,
              center: float = 0.0) -> np.ndarray:
    """Sweep jammer: a CW tone (noise_bw<=0) or a narrow noise patch (noise_bw>0)
    whose center ramps linearly across `span` Hz about `center`, retracing every
    sweep_period_s (sawtooth). The carrier phase is the integral of the swept
    frequency, so it is continuous within each ramp."""
    n = max(1, int(round(fs * dur_s)))
    t = np.arange(n) / fs
    ramp = (t / max(sweep_period_s, 1e-9)) % 1.0           # 0..1 sawtooth
    finst = center + (ramp - 0.5) * span
    phase = 2.0 * np.pi * np.cumsum(finst) / fs
    carrier = np.exp(1j * phase)
    if noise_bw and noise_bw > 0:
        carrier = carrier * band_noise(fs, noise_bw, dur_s, ampl=1.0)
    pk = float(np.max(np.abs(carrier)) or 1.0)            # peak-normalize (no clip)
    return (ampl * carrier / pk).astype(np.complex64)


# Unit-average-energy constellations for the modulated jammer (and the BER
# sweep's victim). Gray-mapped so bit index b of symbol k is ((k >> (m-1-b)) & 1).
def constellation(name: str) -> np.ndarray:
    name = str(name).lower().replace("-", "")
    if name == "bpsk":
        c = np.array([1, -1], dtype=np.complex128)
    elif name == "qpsk":                             # 00 01 11 10 Gray around
        c = np.array([1 + 1j, -1 + 1j, 1 - 1j, -1 - 1j]) / np.sqrt(2)
    elif name == "8psk":
        gray = [0, 1, 3, 2, 6, 7, 5, 4]              # index on circle: label
        c = np.zeros(8, dtype=np.complex128)
        for pos, lab in enumerate(gray):
            c[lab] = np.exp(1j * (np.pi / 4) * pos)
    elif name in ("16qam", "64qam"):
        m = 4 if name == "16qam" else 8
        lv = np.arange(m) * 2 - (m - 1)              # -3 -1 1 3 (16QAM)
        g = np.arange(m) ^ (np.arange(m) >> 1)       # Gray code per axis
        ax = np.zeros(m)
        ax[g] = lv                                   # label: level
        k = int(np.log2(m))
        c = np.array([complex(ax[i >> k], ax[i & (m - 1)])
                      for i in range(m * m)])
        c /= np.sqrt(np.mean(np.abs(c) ** 2))
    else:
        raise ValueError(f"unknown constellation {name}")
    return c.astype(np.complex128)


MOD_JAM_CONSTELLATIONS = ("bpsk", "qpsk", "8psk", "16qam", "64qam")


def rrc_taps(sps: float, rolloff: float = 0.35, span: int = 10) -> np.ndarray:
    """Root-raised-cosine taps at `sps` samples/symbol (may be fractional),
    `span` symbols long, normalized to unit energy so a matched filter returns
    the symbol value at the symbol instant."""
    n = int(np.ceil(span * sps)) | 1
    t = (np.arange(n) - (n - 1) / 2.0) / sps
    b = float(rolloff)
    h = np.zeros(n)
    for i, x in enumerate(t):
        if abs(x) < 1e-9:
            h[i] = 1.0 - b + 4 * b / np.pi
        elif b > 0 and abs(abs(x) - 1.0 / (4 * b)) < 1e-9:
            h[i] = (b / np.sqrt(2)) * ((1 + 2 / np.pi) * np.sin(np.pi / (4 * b))
                                       + (1 - 2 / np.pi) * np.cos(np.pi / (4 * b)))
        else:
            h[i] = ((np.sin(np.pi * x * (1 - b)) + 4 * b * x * np.cos(np.pi * x * (1 + b)))
                    / (np.pi * x * (1 - (4 * b * x) ** 2)))
    return h / np.sqrt(np.sum(h ** 2))


def modulated_jam(fs: float, sym_rate: float, dur_s: float, ampl: float = 0.5,
                  mod: str = "qpsk", center: float = 0.0, sym_duty: float = 1.0,
                  rolloff: float = 0.35, seed=None) -> np.ndarray:
    """Modulated (symbol) jammer: random symbols from `mod` at `sym_rate`,
    RRC-shaped, offset to `center`. Per Amuru & Buehrer (GLOBE'14) a QPSK symbol
    jammer beats equal-power AWGN against M-QAM, and at high SNR the optimum is
    on-off keyed QPSK: each symbol is on with probability `sym_duty` (lambda)
    at amplitude 1/sqrt(lambda), so the average power is unchanged while the on
    symbols land with more energy. sym_duty=1 is continuous. Symbol instants are
    placed at round(k*fs/sym_rate), so a non-integer samples/symbol works (sub-
    sample jitter only). Returns RMS-normalized * ampl (callers peak-normalize)."""
    n = max(1, int(round(fs * dur_s)))
    sps = fs / max(float(sym_rate), 1.0)
    rng = np.random.default_rng(seed)
    c = constellation(mod)
    nsym = int(np.floor(n / sps)) + 1
    syms = c[rng.integers(0, len(c), nsym)]
    lam = min(1.0, max(1e-3, float(sym_duty)))
    if lam < 1.0:
        on = rng.random(nsym) < lam
        syms = syms * on / np.sqrt(lam)
    return symbol_waveform(fs, sym_rate, syms, rolloff, center, ampl, n_out=n)


def symbol_waveform(fs: float, sym_rate: float, syms: np.ndarray,
                    rolloff: float = 0.35, center: float = 0.0,
                    ampl: float = 0.5, n_out: int = None) -> np.ndarray:
    """RRC-shaped baseband from an explicit symbol sequence `syms` at `sym_rate`,
    offset to `center`, RMS-normalized * ampl. Shared by the modulated jammer
    (random symbols) and the BER sweep's victim (a known sequence). Symbols are
    placed at round(k*fs/sym_rate) so a fractional samples/symbol works."""
    syms = np.asarray(syms, dtype=np.complex128)
    sps = fs / max(float(sym_rate), 1.0)
    n = int(n_out) if n_out else int(np.round(len(syms) * sps)) + 1
    imp = np.zeros(n, dtype=np.complex128)
    idx = np.round(np.arange(len(syms)) * sps).astype(int)
    keep = idx < n
    imp[idx[keep]] = syms[keep]
    # FFT convolution: at 30.72 MSps and 100 kBd the RRC is about 3700 taps, and
    # a direct np.convolve of a 15 M sample loop takes the G40 about a minute.
    # oaconvolve gives the same 'same' output in a few seconds; numpy's direct
    # form stays as the fallback where scipy is missing
    taps = rrc_taps(sps, rolloff)
    try:
        from scipy.signal import oaconvolve as _oac
        y = _oac(imp, taps, mode="same")
    except ImportError:
        y = np.convolve(imp, taps, mode="same")
    y = y[:n] if len(y) >= n else np.concatenate([y, np.zeros(n - len(y))])
    if center:
        y = y * np.exp(2j * np.pi * (center / fs) * np.arange(n))
    p = float(np.sqrt(np.mean(np.abs(y) ** 2)) or 1.0)
    return (ampl * y / p).astype(np.complex64)


def apply_pulse(iq: np.ndarray, fs: float, prf_hz: float,
                duty: float) -> np.ndarray:
    """Gate IQ into pulses at prf_hz with the given duty cycle (0..1). prf<=0 or
    duty>=1 returns the waveform unchanged (CW)."""
    if prf_hz <= 0 or duty >= 1.0:
        return iq
    duty = max(0.0, min(1.0, duty))
    period = max(1, int(round(fs / prf_hz)))
    on = int(round(period * duty))
    gate = np.zeros(len(iq), dtype=np.float32)
    for start in range(0, len(iq), period):
        gate[start:start + on] = 1.0
    return (iq * gate).astype(np.complex64)


# BER-vs-jamming-power sweep (symbol-level; reproduces Amuru & Buehrer GLOBE'14)

def _hard_decide(rx: np.ndarray, const: np.ndarray) -> np.ndarray:
    """Nearest-constellation-point decision. Returns symbol indices."""
    # |rx - c|^2 = |rx|^2 - 2 Re(rx conj(c)) + |c|^2; drop the rx term (constant
    # per sample): maximize Re(rx conj(c)) - |c|^2/2.
    metric = (rx[:, None] * np.conj(const)[None, :]).real - 0.5 * (np.abs(const) ** 2)[None, :]
    return np.argmax(metric, axis=1)


def ber_point(victim: str, js_db: float, snr_db: float, jammer: str = "qpsk",
              n_sym: int = 200_000, seed=0) -> float:
    """Symbol-synchronous bit-error rate for a `victim` constellation at `snr_db`
    (Es/N0) under a jammer `js_db` dB above the victim, decided by an ML receiver
    whose regions ignore the jammer (the paper's assumption). `jammer` is a
    constellation name for a modulated jammer, or "awgn"/"barrage" for Gaussian
    noise of the same average power: which is the comparison the sweep makes.
    All constellations are unit average energy, so J/S is a pure amplitude ratio.
    """
    rng = np.random.default_rng(seed)
    cv = constellation(victim)
    m = len(cv)
    bits_per = int(round(np.log2(m)))
    tx_idx = rng.integers(0, m, n_sym)
    rx = cv[tx_idx].astype(np.complex128)

    es = 1.0                                         # victim average symbol energy
    n0 = es * 10.0 ** (-snr_db / 10.0)               # AWGN (thermal)
    pj = es * 10.0 ** (js_db / 10.0)                 # jammer average power
    # thermal noise (complex, variance n0, so n0/2 per axis)
    rx = rx + np.sqrt(n0 / 2.0) * (rng.standard_normal(n_sym) + 1j * rng.standard_normal(n_sym))
    if str(jammer).lower() in ("awgn", "barrage", "noise", "gaussian"):
        rx = rx + np.sqrt(pj / 2.0) * (rng.standard_normal(n_sym) + 1j * rng.standard_normal(n_sym))
    else:                                            # modulated (symbol) jammer
        cj = constellation(jammer)
        jsym = cj[rng.integers(0, len(cj), n_sym)]
        phi = rng.uniform(0, 2 * np.pi, n_sym)       # random phase: non-coherent jammer
        rx = rx + np.sqrt(pj) * jsym * np.exp(1j * phi)

    rx_idx = _hard_decide(rx, cv)
    # bit errors via Gray-code labels (constellation() is Gray-mapped)
    xor = tx_idx ^ rx_idx
    biterr = np.unpackbits(xor.astype(np.uint8)[:, None], axis=1)[:, -bits_per:].sum()
    return float(biterr) / (n_sym * bits_per)


def ber_vs_js(victim: str = "16qam", snr_db: float = 20.0,
              js_db_list=None, n_sym: int = 200_000, seed=0,
              jammers=("qpsk", "barrage")) -> dict:
    """Sweep J/S for each jammer in `jammers` against `victim` at `snr_db`.
    Returns {"victim","snr_db","js_db":[...], "<jammer>":[ber,...], ...}. The
    headline comparison is a modulated (e.g. qpsk) jammer vs "barrage" (AWGN) at
    equal average power: the paper's result is that qpsk gives higher BER."""
    if js_db_list is None:
        js_db_list = list(range(-10, 21, 2))
    out = {"victim": victim, "snr_db": snr_db, "js_db": list(js_db_list)}
    for j in jammers:
        out[j] = [ber_point(victim, js, snr_db, jammer=j, n_sym=n_sym,
                            seed=seed + i) for i, js in enumerate(js_db_list)]
    return out


GENERATORS = {"chirp", "tone", "two_tone", "noise", "barrage", "comb", "swept",
              "modulated"}


def generate(kind: str, fs: float, dur_s: float, **kw) -> np.ndarray:
    if kind == "chirp":
        y = lfm_chirp(fs, kw.get("f0", -fs * 0.4), kw.get("f1", fs * 0.4),
                      dur_s, kw.get("ampl", 0.7))
    elif kind == "tone":                             # spot/CW: a single coherent line
        y = cw_tone(fs, kw.get("freq", 100e3), dur_s, kw.get("ampl", 0.7))
    elif kind == "two_tone":
        y = two_tone(fs, kw.get("f1", -100e3), kw.get("f2", 100e3), dur_s,
                     kw.get("ampl", 0.5))
    elif kind == "noise":                            # partial-band noise patch
        center = float(kw.get("center", 0.0) or 0.0)
        y = band_noise(fs, kw.get("bw", fs * 0.5), dur_s, kw.get("ampl", 0.3))
        if center:                                   # shift the patch off DC
            n = np.arange(len(y), dtype=np.float64)
            y = (y * np.exp(2j * np.pi * (center / fs) * n)).astype(np.complex64)
    elif kind == "barrage":
        y = barrage_noise(fs, dur_s, kw.get("ampl", 0.3))
    elif kind == "comb":
        y = comb_jam(fs, kw.get("spacing", 500e3), kw.get("n_tones", 8), dur_s,
                     kw.get("ampl", 0.4), kw.get("center", 0.0))
    elif kind == "swept":
        y = swept_jam(fs, kw.get("span", fs * 0.8), kw.get("sweep_period_s", 0.01),
                      dur_s, kw.get("ampl", 0.6), kw.get("noise_bw", 0.0),
                      kw.get("center", 0.0))
    elif kind == "modulated":
        y = modulated_jam(fs, kw.get("sym_rate", 100e3), dur_s, kw.get("ampl", 0.5),
                          kw.get("mod", "qpsk"), kw.get("center", 0.0),
                          kw.get("sym_duty", 1.0), kw.get("rolloff", 0.35))
    else:
        raise ValueError(f"unknown generator {kind}")
    y = apply_pulse(y, fs, float(kw.get("prf", 0.0)), float(kw.get("duty", 1.0)))
    pk = float(np.max(np.abs(y)))                          # never exceed DAC full scale
    if pk > 0.999:
        y = (y * (0.999 / pk)).astype(np.complex64)
    return y


def check_waveform_params(kind: str, fs: float, dur_s: float, **kw) -> list:
    """Flag operator-entered values that would silently produce a wrong, empty,
    or degraded waveform. Returns a list of human-readable warning strings
    (empty = nothing suspect). Mirrors the generate()/apply_pulse() math so the
    GUI can warn before transmitting instead of showing a CW/blank result."""
    w = []
    nyq = fs / 2.0
    prf = float(kw.get("prf", 0.0) or 0.0)
    duty = float(kw.get("duty", 1.0) or 1.0)

    # pulse gating: PRF/duty vs loop length and sample rate
    if prf > 0:
        period_s = 1.0 / prf
        on_samples = int(round((fs / prf) * duty))
        if on_samples < 1:
            w.append(f"pulse ON-time rounds to 0 samples (PRF {prf:g} Hz x duty "
                     f"{duty:g} at {fs/1e6:.3f} MSps) -- NOTHING will transmit. "
                     f"Lower PRF (<= {2*fs*duty:,.0f} Hz) or raise duty.")
        if dur_s < period_s:
            w.append(f"loop {dur_s:g} s is shorter than one pulse period "
                     f"{period_s:g} s (PRF {prf:g} Hz) -- the buffer can't hold an "
                     f"on+off cycle, so it transmits CONTINUOUS (CW), not pulsed. "
                     f"Set loop >= {period_s:g} s.")
        elif abs((dur_s * prf) - round(dur_s * prf)) > 0.01:
            w.append(f"loop {dur_s:g} s is not a whole number of pulse periods "
                     f"({period_s:g} s) -- the on/off pattern will seam at each "
                     f"loop. Use a loop that is a multiple of {period_s:g} s.")
    elif duty < 1.0:
        w.append("duty < 1 has no effect without a PRF -- set pulse PRF > 0 to "
                 "actually pulse (otherwise it stays CW).")

    # Usable band is ~80% of the sample rate; the outer ~20% is filter roll-off /
    # alias-protection, so energy there is attenuated/distorted (and beyond +/-fs/2
    # it folds). Guard against usable, not just Nyquist.
    usable = USABLE_FRACTION * fs
    usable_half = usable / 2.0

    def _edge(name, extent):                         # extent = peak |offset| reached
        if extent > nyq:
            w.append(f"{name} reaches +/-{extent/1e6:.3f} MHz -- beyond +/-Nyquist "
                     f"({nyq/1e6:.3f} MHz at {fs/1e6:.3f} MSps). It will ALIAS/fold "
                     f"back into band. Keep within +/-{usable_half/1e6:.3f} MHz.")
        elif extent > usable_half:
            w.append(f"{name} reaches +/-{extent/1e6:.3f} MHz -- outside the usable "
                     f"band +/-{usable_half/1e6:.3f} MHz (~{USABLE_FRACTION:.0%} of "
                     f"{fs/1e6:.3f} MSps). It lands in the filter roll-off and will "
                     f"be attenuated/distorted. Keep within +/-{usable_half/1e6:.3f} MHz.")

    def _bw(name, bw):                               # occupied width vs usable band
        if bw > fs:
            w.append(f"{name} {bw/1e6:.3f} MHz > sample rate {fs/1e6:.3f} MHz -- "
                     f"impossible; it clamps to the full band.")
        elif bw > usable:
            w.append(f"{name} {bw/1e6:.3f} MHz exceeds the usable band "
                     f"~{usable/1e6:.3f} MHz (~{USABLE_FRACTION:.0%} of {fs/1e6:.3f} "
                     f"MSps) -- the edges fall in the filter roll-off. Keep it "
                     f"<= {usable/1e6:.3f} MHz.")

    if kind == "noise":
        bw = float(kw.get("bw", fs * 0.5))
        center = float(kw.get("center", 0.0) or 0.0)
        _bw("bandwidth", bw)
        _edge("noise edge (offset + bw/2)", abs(center) + bw / 2.0)
    elif kind == "tone":                             # pure CW line
        _edge("tone offset", abs(float(kw.get("freq", 100e3))))
    elif kind == "two_tone":
        _edge("tone f1", abs(float(kw.get("f1", -100e3))))
        _edge("tone f2", abs(float(kw.get("f2", 100e3))))
    elif kind == "comb":
        spacing = float(kw.get("spacing", 500e3))
        n = int(float(kw.get("n_tones", 8)))
        center = float(kw.get("center", 0.0))
        _edge(f"comb extent (center + {n} tones x {spacing/1e6:.3f} MHz)",
              abs(center) + (n / 2.0) * spacing)
    elif kind == "swept":
        span = float(kw.get("span", fs * 0.8))
        center = float(kw.get("center", 0.0))
        _edge("sweep edge (center + span/2)", abs(center) + span / 2.0)
        sp = float(kw.get("sweep_period_s", 0.01))
        if sp > dur_s:
            w.append(f"sweep period {sp:g} s > loop {dur_s:g} s -- only part of "
                     f"the sweep is in the buffer. Set loop >= {sp:g} s.")
    elif kind == "modulated":
        rs = float(kw.get("sym_rate", 100e3))
        occ = rs * (1.0 + float(kw.get("rolloff", 0.35)))
        center = float(kw.get("center", 0.0))
        if rs <= 0:
            w.append("symbol rate must be > 0.")
        else:
            if fs / rs < 2.0:
                w.append(f"symbol rate {rs:,.0f} Hz gives {fs/rs:.2f} samples/"
                         f"symbol at {fs/1e6:.3f} MSps -- need >= 2. Lower the "
                         f"symbol rate (<= {fs/2:,.0f} Hz).")
            _bw("occupied bandwidth (Rs x (1+rolloff))", occ)
            _edge("modulated edge (center + occ/2)", abs(center) + occ / 2.0)
        mod = str(kw.get("mod", "qpsk")).lower()
        if mod not in MOD_JAM_CONSTELLATIONS:
            w.append(f"constellation {mod!r} unknown -- use one of "
                     f"{', '.join(MOD_JAM_CONSTELLATIONS)}.")
        sd = float(kw.get("sym_duty", 1.0))
        if sd < 1.0:
            w.append(f"symbol on-off keying (lambda={sd:g}): ON symbols are "
                     f"{-10*np.log10(max(sd,1e-3)):.1f} dB hotter than average, so "
                     f"after peak-normalization the AVERAGE TX power drops by that "
                     f"much. Compare jammers at equal average power, not equal "
                     f"peak (the BER sweep does this).")
    elif kind == "chirp":
        _edge("chirp f0", abs(float(kw.get("f0", -fs * 0.4))))
        _edge("chirp f1", abs(float(kw.get("f1", fs * 0.4))))
    return w


# measurements on captured IQ

def psd_db(iq: np.ndarray, fs: float, nfft: int = 4096):
    """Welch-ish averaged PSD. Returns (freqs_hz, psd_db) fftshifted."""
    x = np.asarray(iq, dtype=np.complex64)
    if len(x) < nfft:
        nfft = 1 << int(np.floor(np.log2(max(2, len(x)))))
    win = np.hanning(nfft)
    acc = np.zeros(nfft)
    nseg = 0
    for i in range(0, len(x) - nfft + 1, nfft // 2):
        seg = x[i:i + nfft] * win
        acc += np.abs(np.fft.fftshift(np.fft.fft(seg))) ** 2
        nseg += 1
    if nseg == 0:
        acc = np.abs(np.fft.fftshift(np.fft.fft(x[:nfft] * win[:len(x)]))) ** 2
        nseg = 1
    psd = 10.0 * np.log10(acc / nseg + 1e-20)
    freqs = np.fft.fftshift(np.fft.fftfreq(nfft, 1.0 / fs))
    return freqs, psd


def noise_floor_db(iq: np.ndarray, fs: float) -> float:
    _, p = psd_db(iq, fs)
    return float(np.median(p))


def peak_tone(iq: np.ndarray, fs: float):
    """Return (freq_hz, power_db, snr_db) of the strongest tone vs median floor."""
    f, p = psd_db(iq, fs)
    k = int(np.argmax(p))
    return float(f[k]), float(p[k]), float(p[k] - np.median(p))


def peak_tone_near(iq: np.ndarray, fs: float, f_expected: float,
                   search_hz: float = 15_000.0, dc_guard_hz: float = 3_000.0):
    """Strongest tone within +-search_hz of f_expected, ignoring the +-dc_guard_hz
    DC bin. A direct-conversion RX always parks an LO/DC-offset spike at 0 Hz that
    a blind global argmax locks onto; a loopback/isolation check must look where the
    commanded tone actually is. Returns (freq_hz, power_db, snr_db) vs median floor;
    freq_hz = f_expected with snr 0 if nothing clears the window."""
    f, p = psd_db(iq, fs)
    med = float(np.median(p))
    mask = (np.abs(f - f_expected) <= search_hz) & (np.abs(f) > dc_guard_hz)
    if not mask.any():
        return float(f_expected), med, 0.0
    idx = np.where(mask)[0]
    k = int(idx[int(np.argmax(p[idx]))])
    return float(f[k]), float(p[k]), float(p[k] - med)


def detect_signals(iq: np.ndarray, fs: float, thr_db: float = 12.0,
                   min_sep_hz: float = 20_000.0, max_sigs: int = 16):
    """Simple multi-signal energy detector for read-IQ checkouts + the transponder.
    Returns a list of {"freq_hz", "snr_db", "power_db"} for PSD peaks that clear
    `thr_db` above the median floor, greedily de-duplicated to `min_sep_hz`."""
    f, p = psd_db(iq, fs)
    floor = float(np.median(p))
    df = float(f[1] - f[0]) if len(f) > 1 else 1.0
    guard = max(1, int(round(min_sep_hz / max(df, 1.0))))
    order = np.argsort(p)[::-1]
    taken = np.zeros(len(p), dtype=bool)
    out = []
    for k in order:
        if p[k] - floor < thr_db:
            break
        if taken[max(0, k - guard):k + guard + 1].any():
            continue
        taken[max(0, k - guard):k + guard + 1] = True
        out.append({"freq_hz": float(f[k]), "snr_db": round(float(p[k] - floor), 1),
                    "power_db": round(float(p[k]), 1)})
        if len(out) >= max_sigs:
            break
    out.sort(key=lambda d: d["freq_hz"])
    return out, round(floor, 1)


def band_power_db(iq: np.ndarray, fs: float, f_lo: float, f_hi: float) -> float:
    f, p = psd_db(iq, fs)
    m = (f >= f_lo) & (f <= f_hi)
    if not m.any():
        return -300.0
    return float(10.0 * np.log10(np.mean(10.0 ** (p[m] / 10.0)) + 1e-20))
