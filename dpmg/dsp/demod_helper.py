"""
demod_helper.py: demod wrapper: carrier recovery + symbol-timing search.

The dpmg demods have no carrier recovery and no timing search. The channelizer
leaves a small residual frequency offset and arbitrary symbol alignment, which
breaks those demods (the constellation rotates, signs flip, no frame validates).
This wrapper fixes both before/around the real demod.
"""
from __future__ import annotations
import numpy as np


def _derotate_psk(iq: np.ndarray, fs: float, order: int) -> np.ndarray:
    """Estimate + remove residual carrier via Mth-power (2=BPSK, 4=QPSK)."""
    p = iq ** order
    sp = np.fft.fft(p)
    f = np.fft.fftfreq(len(iq), 1.0 / fs)
    foff = f[np.argmax(np.abs(sp))] / order
    t = np.arange(len(iq)) / fs
    return (iq * np.exp(-2j * np.pi * foff * t)).astype(np.complex64)


def _correct_fsk_offset(iq: np.ndarray, fs: float) -> np.ndarray:
    """Remove a DC/centering offset from an FSK signal by centering its spectrum.

    A residual frequency offset shifts the FSK tones away from the demod's expected
    tone0/tone1 positions. Estimate the spectral centroid and shift it to DC.
    """
    sp = np.abs(np.fft.fft(iq)) ** 2
    f = np.fft.fftfreq(len(iq), 1.0 / fs)
    centroid = np.sum(f * sp) / (np.sum(sp) + 1e-12)
    t = np.arange(len(iq)) / fs
    return (iq * np.exp(-2j * np.pi * centroid * t)).astype(np.complex64)


def _mth_power_cfo(iq: np.ndarray, fs: float, M: int, fmax: float) -> float:
    """Coarse blind carrier-offset estimate: the M-th power of an M-fold
    symmetric linear mod (BPSK 2, QPSK/16QAM 4, pi/4-QPSK/8PSK 8, 16APSK 12)
    collapses the data to a line at M*CFO. Zero-padded FFT + parabolic peak,
    restricted to |CFO| <= fmax (the detector band is centred by the plan, so
    anything past that is a wrong channel, not an offset). Returns Hz.

    Needed for two-radio links: undisciplined TCXOs leave ~200 Hz between TX
    and RX (e.g. a B200mini to HackRF Pro link), far past what the per-frame
    estimators tolerate at 4800 sym/s."""
    x = np.asarray(iq, dtype=np.complex128)
    if len(x) < 256:
        return 0.0
    x = x / (np.sqrt(np.mean(np.abs(x) ** 2)) + 1e-12)
    p = x ** M
    n = 1 << int(np.ceil(np.log2(len(p) * 4)))
    S = np.abs(np.fft.fft(p, n))
    f = np.fft.fftfreq(n, 1.0 / fs)
    S[np.abs(f) > M * fmax] = 0.0
    k = int(np.argmax(S))
    if S[k] <= 0:
        return 0.0
    # parabolic refinement between neighbouring bins
    a, b, c = S[k - 1], S[k], S[(k + 1) % n]
    den = (a - 2 * b + c)
    d = 0.5 * (a - c) / den if abs(den) > 1e-12 else 0.0
    fpk = f[k] + d * (fs / n)
    return float(fpk / M)


def _interp_run(xd: np.ndarray, start: float, step: float, count: int):
    """Vectorised linear interpolation of `count` samples at start + k*step.
    Replaces the per-sample python `itp()` loops (the data-aided searches ran
    ~1e5-1e6 of those per frame, which is why QPSK/BPSK took seconds to lock).
    Returns the array, or None when the run leaves the buffer."""
    if start < 0 or count <= 0:
        return None
    last = start + step * (count - 1)
    if last >= len(xd) - 1:
        return None
    pos = start + step * np.arange(count)
    i = np.floor(pos).astype(np.int64)
    mu = pos - i
    return xd[i] * (1.0 - mu) + xd[i + 1] * mu


_LINEAR_MODS = ("BPSK", "QPSK", "PI4QPSK", "PI4-QPSK", "DQPSK", "OQPSK")
_HIGHER_ORDER_MODS = ("8PSK", "8QAM", "16QAM", "16APSK", "32APSK")


def _rrc_taps(beta: float, sps: int, span: int = 8) -> np.ndarray:
    """Root-raised-cosine taps (matches the generator's TX RRC for a matched pair)."""
    span = min(span, max(2, 8192 // max(1, sps)))   # cap kernel like the generator
    N = span * sps
    t = np.arange(-N / 2, N / 2 + 1) / sps
    taps = np.zeros_like(t)
    for i, ti in enumerate(t):
        if abs(ti) < 1e-8:
            taps[i] = 1.0 - beta + 4 * beta / np.pi
        elif beta > 0 and abs(abs(ti) - 1 / (4 * beta)) < 1e-8:
            taps[i] = (beta / np.sqrt(2)) * ((1 + 2 / np.pi) * np.sin(np.pi / (4 * beta)) +
                                             (1 - 2 / np.pi) * np.cos(np.pi / (4 * beta)))
        else:
            num = np.sin(np.pi * ti * (1 - beta)) + 4 * beta * ti * np.cos(np.pi * ti * (1 + beta))
            den = np.pi * ti * (1 - (4 * beta * ti) ** 2)
            taps[i] = num / den if abs(den) > 1e-12 else 0.0
    return taps / np.sqrt(np.sum(taps ** 2))


def _matched_filter(iq: np.ndarray, sps: int, beta: float = 0.35) -> np.ndarray:
    """Apply the RX RRC matched filter (FFT convolution, 'same' length).

    The generator shapes each symbol with a TX RRC; the receiver needs the matching
    RX RRC so the cascade is a raised cosine with zero ISI at the symbol instants.
    Without this, the dpmg demods (which just average symbol windows) see ISI and
    decode at ~chance. This is the receive-side complement of the generator's _shape.
    """
    taps = _rrc_taps(beta, sps).astype(np.complex64)
    n = len(iq) + len(taps) - 1
    nfft = 1 << int(np.ceil(np.log2(n)))
    conv = np.fft.ifft(np.fft.fft(iq, nfft) * np.fft.fft(taps, nfft))[:n]
    start = (len(taps) - 1) // 2
    return conv[start:start + len(iq)].astype(np.complex64)


def prep_baseband(iq: np.ndarray, fs: float, modulation: str,
                  sps: int = 0, beta: float = 0.35) -> np.ndarray:
    """Carrier/offset recovery + (for linear mods) RX matched filter.

    Linear mods (BPSK/QPSK/...) are RRC-shaped at TX, so they get carrier recovery
    then the RX matched filter. Frequency mods (FSK/4FSK/GFSK) get spectral centering
    only: they use tone correlation, not a matched filter.
    """
    m = (modulation or "").upper()
    iq = np.asarray(iq)
    if m in _LINEAR_MODS:
        order = 2 if m == "BPSK" else 4
        rec = _derotate_psk(iq, fs, order)
        if sps and sps >= 2:
            rec = _matched_filter(rec, int(sps), beta)
        return rec
    if m in ("FSK", "2FSK", "4FSK", "GFSK"):
        return _correct_fsk_offset(iq, fs)
    return iq


def _centersample_bits(base, sps, modulation):
    """Sample at symbol centers (not averaging the window) and map to bits using the
    generator's convention. Returns (bit_string, n_syms, symbols_complex).
    BPSK: real>=0: '0' (composer maps bit0 to +1, bit1 to -1).
    QPSK: gray-ish per-quadrant; matches the composer's _bits_to_symbols_qpsk packing.
    """
    import numpy as _np
    m = (modulation or "").upper()
    syms = _np.asarray(base)
    if len(syms) == 0:
        return "", 0, syms
    if m == "BPSK":
        bits = "".join("0" if _np.real(s) >= 0 else "1" for s in syms)
        return bits, len(syms), syms
    if m in ("QPSK", "PI4QPSK", "PI4-QPSK", "DQPSK", "OQPSK"):
        # composer qpsk: bit pair: (I,Q). Decode each quadrant back to 2 bits.
        # I >= 0 gives first bit 0; Q >= 0 gives second bit 0 (matches (1-2b) mapping per axis)
        out = []
        for s in syms:
            out.append("0" if _np.real(s) >= 0 else "1")
            out.append("0" if _np.imag(s) >= 0 else "1")
        return "".join(out), len(syms), syms
    return "", 0, syms


class _CSRes:
    """Minimal DemodResult-like shim so callers reading .bit_string/.symbols/.snr_db work."""
    def __init__(self, bit_string, symbols, snr_db):
        self.bit_string = bit_string
        self.symbols = symbols
        self.snr_db = snr_db
        self.snr_estimate = snr_db


def _est_snr_db(syms, modulation):
    import numpy as _np
    if len(syms) == 0:
        return 0.0
    m = (modulation or "").upper()
    if m == "BPSK":
        d = _np.real(syms)
        sig = _np.mean(_np.abs(d)) ** 2
        noise = _np.var(_np.abs(d)) + 1e-12
    else:
        a = _np.abs(syms)
        sig = _np.mean(a) ** 2
        noise = _np.var(a) + 1e-12
    import math
    return float(10.0 * math.log10(max(sig / noise, 1e-6)))

def _qpsk_sync_syms(sync_bits):
    """Map sync bit-string to QPSK reference symbols.
    Composer convention (verified vs modulator): bit0 is the Q sign (0:+), bit1 the I sign (0:+)."""
    import numpy as _np
    out = []
    for i in range(0, len(sync_bits) - 1, 2):
        Q = 1.0 if sync_bits[i] == '0' else -1.0
        I = 1.0 if sync_bits[i + 1] == '0' else -1.0
        out.append((I + 1j * Q) / _np.sqrt(2.0))
    return _np.array(out, dtype=_np.complex128)

def _data_aided_qpsk(base, isps, find_and_decode, sync_bits=None, fsps=None, frame_bits=None, lock=None):
    """Data-aided carrier+timing recovery for QPSK. Complex sync
    reference, 4th-power residual-CFO removal, (Q,I) demap. Runs on raw iq (see caller).
    Returns (bits, frame, nsyms, res) or None.

    Warm lock: QPSK's cold acquire is a ~51-point CFO sweep (each a full-buffer
    correlation) + a wide frac x dsps timing sweep: so slow that, with the pool
    coalescing one inflight decode per channel, QPSK completes only every several
    seconds in continuous mode and mostly misses the decode window (BPSK's cheaper
    2-fold acquire did not). When `lock` carries cfo/frac/dsps from a prior decode
    of this channel, retry first on a tight grid around them (a handful of points),
    falling back to the full cold search only if the warm attempt finds no valid
    frame. Once locked, repeat frames re-sync cheaply: QPSK decodes every frame."""
    import numpy as _np
    fsps = float(fsps) if fsps else float(isps)
    fsps = fsps * 2.0        # Modulator emits 2*spb per symbol: true sps is 2x
    isps = max(2, int(round(fsps)))
    if sync_bits:
        SYNC = sync_bits
    else:
        try:
            from dpmg.framing.frame_codec import SYNC_BITS as SYNC
        except Exception:
            return None
    sync_syms = _qpsk_sync_syms(SYNC)
    nss = len(sync_syms)
    x = _np.asarray(base, dtype=_np.complex128); N = len(x)
    if N < (nss + 4) * isps:
        return None
    # Symbols demapped per candidate start. A fixed 104-bit (52-symbol)
    # window would cap the decodable frame: any message past ~9 bytes
    # (e.g. the bench's "QPSK TEST-002", 144 bits) could never validate, on any
    # link. With a variable-length frame (frame_bits None) demap a generous
    # run and let the sync/CRC search find the frame inside it.
    FRAME_BITS = int(frame_bits) if frame_bits else 0
    FS = max(FRAME_BITS // 2, 52) if FRAME_BITS else 512
    # Sealed (and long variable-length) frames carry no fixed
    # frame_bits, so FS=512 symbols (1024 bits) cut them off: a sealed QPSK
    # rs+1/2 frame is 1244 bits and never validated. The (frac, dsps) grid
    # search keeps the 512-symbol window (for speed, with the coherence
    # tie-break); only when the winner's run hit that cap and the frame did
    # not validate is it re-demapped once at the winning timing/phase over the
    # rest of the buffer (<= FS_LONG symbols) and re-checked. Fixed-length
    # frames (frame_bits given) never take this path.
    FS_LONG = 4096          # 8192 bits: > a 255 B legacy/sealed frame at rate 1/2
    up = _np.zeros(int(_np.ceil(nss * fsps)) + 1, dtype=complex)
    for k in range(nss):
        up[int(round(k * fsps))] = sync_syms[k]
    taps = _rrc_taps(0.35, isps).astype(_np.complex64)
    ref = _np.convolve(up, taps, 'same'); ref = ref / (_np.sqrt(_np.sum(_np.abs(ref) ** 2)) + 1e-12)
    refc = _np.conj(ref[::-1])

    def itp(sig, pos):
        i = int(_np.floor(pos)); mu = pos - i
        if i < 0 or i >= len(sig) - 1: return None
        return sig[i] * (1 - mu) + sig[i + 1] * mu

    def _try_lock(xd, lag, frac_grid, dsps_grid):
        """Demap one frame at `lag`, sweeping the given frac/dsps grids. Returns
        (bits, symbols, winning_frac, winning_dsps) or None.

        Tie-break: the 16-bit sync only spans 8 symbols, so ~1300 of the cold
        grid's ~1400 (frac, dsps) points agree with it perfectly. Taking
        the first of those in grid order gives frac at the -fsps/2 edge with an
        arbitrary dsps up to +-0.2 (1 % rate error), which slips symbols across
        a long frame (342-symbol sealed QPSK 1/2 frame: 140-240 bit errors on a
        noiseless channel, ~1 keying in 2 lost). Ties on sync agreement are
        broken by the QPSK 4th-power coherence of the whole demapped run
        (|sum s^4| / sum |s|^4: 1 = every symbol on the constellation), which
        peaks at the eye centre and the true symbol rate. The bit string is
        built once, for the winner, not per grid point."""
        bkey = None; bb = None; bfd = (0.0, 0.0); brp = (0.0, 0.0)   # (res, phi)
        _sref = _np.frombuffer(SYNC[:2 * nss].encode("ascii"), _np.uint8) == ord("1")
        for frac in frac_grid:
            for dsps in dsps_grid:
                # as many symbols as the buffer holds from this start, up to FS
                _n = min(FS, int((N - 2 - (lag + frac)) / (fsps + dsps)))
                if _n < nss + 8: continue
                syms = _interp_run(xd, lag + frac, fsps + dsps, _n)
                if syms is None: continue
                z = syms ** 4
                ang = _np.angle(_np.sum(z[1:] * _np.conj(z[:-1]))); res = ang / 4.0
                syms = syms * _np.exp(-1j * res * _np.arange(len(syms)))
                phi = _np.angle(_np.sum(syms[:nss] * _np.conj(sync_syms)))
                s2 = syms * _np.exp(-1j * phi)
                # sync agreement: bit 2k = Q sign, bit 2k+1 = I sign ('1' = negative)
                hb = _np.empty(2 * nss, dtype=bool)
                hb[0::2] = s2[:nss].imag < 0; hb[1::2] = s2[:nss].real < 0
                ag = int(_np.count_nonzero(hb == _sref))
                z4 = s2 ** 4
                coh = float(_np.abs(_np.sum(z4)) / (_np.sum(_np.abs(z4)) + 1e-30))
                key = (ag, coh)
                if bkey is None or key > bkey:
                    bkey = key; bb = s2; bfd = (float(frac), float(dsps))
                    brp = (float(res), float(phi))           # long sealed frames
        if bb is None: return None
        b = ''.join(('0' if v.imag >= 0 else '1') + ('0' if v.real >= 0 else '1') for v in bb)
        return b, bb, bfd[0], bfd[1], brp

    def _extend(xd, lag, wfrac, wdsps, rp, n0):
        """Re-demap the winning run past the FS cap, same timing,
        residual-CFO slope and phase as the grid winner. None when the buffer
        holds no more symbols than the capped run already had."""
        _n = min(FS_LONG, int((N - 2 - (lag + wfrac)) / (fsps + wdsps)))
        if FRAME_BITS or n0 < FS or _n <= n0:
            return None
        syms = _interp_run(xd, lag + wfrac, fsps + wdsps, _n)
        if syms is None: return None
        s2 = syms * _np.exp(-1j * (rp[0] * _np.arange(_n) + rp[1]))
        b = ''.join(('0' if v.imag >= 0 else '1') + ('0' if v.real >= 0 else '1') for v in s2)
        return b, s2

    warm = bool(lock) and lock.get("qpsk_locked") and ("qcfo" in lock)
    fallback = None
    for mode in (("warm", "cold") if warm else ("cold",)):
        if mode == "warm":                       # tight grid around the cached lock
            cfo_grid = float(lock["qcfo"]) + _np.arange(-0.002, 0.00201, 0.001)
            _fc = float(lock.get("qfrac", 0.0)); _dc = float(lock.get("qdsps", 0.0))
            frac_grid = _fc + _np.arange(-0.4, 0.401, 0.2)
            dsps_grid = _dc + _np.arange(-0.06, 0.0601, 0.03)
        else:                                    # full cold search
            cfo_grid = _np.arange(-0.03, 0.0301, 0.0012)
            frac_grid = _np.arange(-fsps / 2, fsps / 2, 0.2)
            dsps_grid = _np.arange(-0.2, 0.2001, 0.03)
        best = (0.0, 0, 0.0)
        for cfo in cfo_grid:
            xd = x * _np.exp(-2j * _np.pi * cfo * _np.arange(N))
            c = _np.abs(_np.convolve(xd, refc, mode='valid')); pk = int(_np.argmax(c))
            if c[pk] > best[2]: best = (float(cfo), pk, float(c[pk]))
        cfo, gpk, _ = best
        xd = x * _np.exp(-2j * _np.pi * cfo * _np.arange(N))
        corr = _np.convolve(xd, refc, mode='valid')
        # Candidate frame starts: every sync-correlation peak within 6 dB of
        # the strongest, strongest first (at most 8). Stepping from the first
        # peak by FS*fsps would assume FS is the frame length, which it is not,
        # and miss every later frame.
        ca = _np.abs(corr); starts = []
        minsep = int(nss * fsps)
        order = _np.argsort(ca)[::-1]
        for pk in order:
            if ca[pk] < 0.5 * ca[gpk] or len(starts) >= 8:
                break
            if all(abs(int(pk) - s0) >= minsep for s0 in starts):
                starts.append(int(pk))
        if not starts: starts = [max(0, gpk)]
        for lag in starts:
            got = _try_lock(xd, lag, frac_grid, dsps_grid)
            if got is None: continue
            bs, s2, wfrac, wdsps, wrp = got
            res = _CSRes(bs, s2, _est_snr_db(s2, "QPSK")); fr = find_and_decode(bs)
            if not (fr is not None and getattr(fr, "valid", False)):
                ext = _extend(xd, lag, wfrac, wdsps, wrp, len(s2))   # long sealed frames
                if ext is not None:
                    fr2 = find_and_decode(ext[0])
                    if fr2 is not None and getattr(fr2, "valid", False):
                        bs, s2, fr = ext[0], ext[1], fr2
                        res = _CSRes(bs, s2, _est_snr_db(s2, "QPSK"))
            if fr is not None and getattr(fr, "valid", False):
                if lock is not None:            # remember for the next frame (warm)
                    lock["qpsk_locked"] = True; lock["qcfo"] = float(cfo)
                    lock["qfrac"] = float(wfrac); lock["qdsps"] = float(wdsps)
                return (bs, fr, len(s2), res)
            if fallback is None: fallback = (bs, fr, len(s2), res)
    return fallback

def _data_aided_linear(base, isps, m, find_and_decode, sync_bits=None, fsps=None, frame_bits=None):
    """Data-aided recovery; uses the channel's real sync when provided."""
    import numpy as _np
    fsps = float(fsps) if fsps else float(isps)   # true fractional sps; isps is for RRC taps only
    if sync_bits:
        SYNC = sync_bits
    else:
        try:
            from dpmg.framing.frame_codec import SYNC_BITS as SYNC
        except Exception:
            return None
    if m != "BPSK":     
        return None
    sync_pm = _np.array([1.0 if b == '0' else -1.0 for b in SYNC], dtype=_np.complex128)
    nsync = len(SYNC)
    # Frame length must come from the plan. A fixed `max(nsync+96, 144)` assumes a
    # 96-bit payload (FRAME=144). BPSK-05's frame is 152 bits (48 sync + 104 data), so
    # period = FRAME*fsps would be 1735 instead of 1831: 96 samples of error per frame against a
    # +/-25 search window: the first frame locks, every later one is missed, and lock_frame
    # truncates 152-bit frames to 144.
    FRAME = int(frame_bits) if frame_bits else max(nsync + 96, 144)
    x = _np.asarray(base, dtype=_np.complex128); N = len(x)
    if N < (nsync + 4) * isps:
        return None
    up = _np.zeros(int(_np.ceil(nsync * fsps)) + 1, dtype=complex)
    for k in range(nsync):
        up[int(round(k * fsps))] = sync_pm[k]
    taps = _rrc_taps(0.35, isps).astype(_np.complex64)
    ref = _np.convolve(up, taps, 'same'); ref = ref / (_np.sqrt(_np.sum(_np.abs(ref) ** 2)) + 1e-12)
    refc = _np.conj(ref[::-1])
    best = (0.0, 0, 0.0)
    for cfo in _np.arange(-0.02, 0.0201, 0.001):
        xd = x * _np.exp(-2j * _np.pi * cfo * _np.arange(N))
        c = _np.abs(_np.convolve(xd, refc, mode='valid')); pk = int(_np.argmax(c))
        if c[pk] > best[2]: best = (float(cfo), pk, float(c[pk]))
    cfo, gpk, _ = best
    xd = x * _np.exp(-2j * _np.pi * cfo * _np.arange(N))
    corr = _np.abs(_np.convolve(xd, refc, mode='valid'))
    def itp(sig, pos):
        i = int(_np.floor(pos)); mu = pos - i
        if i < 0 or i >= len(sig) - 1: return None
        return sig[i] * (1 - mu) + sig[i + 1] * mu
    period = int(round(FRAME * fsps)); starts = []; s = gpk % period
    while s + FRAME * fsps <= N:
        lo = max(0, s - 25); hi = min(len(corr), s + 25)
        if hi > lo: starts.append(lo + int(_np.argmax(corr[lo:hi])))
        s += period
    if not starts: starts = [gpk]
    def lock_frame(lag):
        if lag + FRAME * isps > N: return None
        bscore = -1e9; bsyms = None
        for frac in _np.arange(-fsps / 2, fsps / 2, 0.25):
            for dsps in _np.arange(-0.20, 0.2001, 0.02):
                syms = _interp_run(xd, lag + frac, fsps + dsps, FRAME)
                if syms is None: continue
                phi = _np.angle(_np.sum(syms[:nsync] * _np.conj(sync_pm))); syms = syms * _np.exp(-1j * phi)
                prod = syms[:nsync] * _np.conj(sync_pm); ph = _np.unwrap(_np.angle(prod)); kk = _np.arange(nsync)
                aa, bb = _np.linalg.lstsq(_np.vstack([kk, _np.ones(nsync)]).T, ph, rcond=None)[0]
                syms = syms * _np.exp(-1j * (aa * _np.arange(FRAME) + bb))
                bits16 = ''.join('0' if sv.real >= 0 else '1' for sv in syms[:nsync])
                inv = ''.join('1' if b == '0' else '0' for b in bits16)
                ag = max(sum(a == b for a, b in zip(bits16, SYNC)), sum(a == b for a, b in zip(inv, SYNC)))
                margin = float(_np.mean(_np.abs(_np.real(syms))) - _np.mean(_np.abs(_np.imag(syms))))
                score = ag * 1000 + margin
                if score > bscore: bscore = score; bsyms = syms
        return bsyms
    fallback = None
    for lag in starts:
        syms = lock_frame(lag)
        if syms is None: continue
        bs, nsyms, scx = _centersample_bits(syms, isps, m)
        res = _CSRes(bs, scx, _est_snr_db(scx, m)); fr = find_and_decode(bs)
        if fr is not None and getattr(fr, "valid", False):
            return (bs, fr, nsyms, res)
        if fallback is None: fallback = (bs, fr, nsyms, res)
    return fallback

def _fsk_grid_cfo(iq: np.ndarray, fs: float, tones: np.ndarray) -> float:
    """Estimate FSK carrier offset by aligning the power-gated
    instantaneous frequency to the demodulator's tone grid. It holds up where
    the spectral centroid does not: out-of-burst noise and payload tone imbalance
    (measured: -2445 Hz bogus centroid ~= half the tone spacing on a clean
    burst) do not bias it."""
    x = np.asarray(iq)
    if len(x) < 64:
        return 0.0
    ph = np.angle(x[1:] * np.conj(x[:-1]))
    fi = ph * fs / (2 * np.pi)
    p = np.abs(x[1:]) ** 2
    gate = p > 4.0 * np.median(p)
    fig = fi[gate] if int(gate.sum()) >= 50 else fi
    span = float(tones.max() - tones.min())
    # Only search within a fraction of the adjacent-tone
    # spacing. A CFO beyond half-spacing is unresolvable for a tone grid
    # (shifted grid == reassigned tones), and a wide +/-span/2 search lets
    # transition-sample clusters pull the estimate a full slot off: every
    # dibit then slices to a neighbouring tone (4FSK fails under any CFO).
    st = np.sort(tones)
    spacing = float(np.min(np.diff(st))) if len(st) > 1 else span
    lim = 0.45 * spacing   # just shy of half-spacing (adjacent-tone ambiguity)
    tol = max(300.0, 0.35 * spacing)
    offs = np.arange(-lim, lim + 1, 50.0)
    eps = 1e-6
    if len(st) > 1 and float(np.min(np.diff(st))) <= 2.0 * tol + 4.0 * eps:
        # tone windows overlap: brute-force "within tol of any tone"
        best_score, best_off = -1, 0.0
        for off in offs:
            d = np.min(np.abs(fig[:, None] - (tones + off)[None, :]), axis=1)
            score = int(np.sum(d < tol))
            if score > best_score:
                best_score, best_off = score, off
        return float(best_off)
    # Disjoint windows, so "within tol of any tone" is the sum
    # of per-tone counts: binary searches on the sorted frequencies instead
    # of an (N x tones) distance matrix per offset (0.84 s/call at 9600 Bd /
    # 12 kHz, FEC-07). Same counts as the brute force: samples inside the
    # windows by an eps margin come from searchsorted, the few within eps of an
    # edge are re-tested with the exact |f - c| < tol; argmax keeps the first
    # (lowest-offset) maximum, as a strict '>' loop would.
    sf = np.sort(fig)
    score = np.zeros(len(offs), dtype=np.int64)
    for tn in tones:
        c = tn + offs
        a_in = np.searchsorted(sf, c - tol + eps, "left")
        b_in = np.searchsorted(sf, c + tol - eps, "right")
        a_lo = np.searchsorted(sf, c - tol - eps, "left")
        b_hi = np.searchsorted(sf, c + tol + eps, "right")
        score += np.maximum(b_in - a_in, 0)
        for i in np.nonzero((a_in > a_lo) | (b_hi > b_in))[0]:
            edge = np.concatenate((sf[a_lo[i]:a_in[i]], sf[b_in[i]:b_hi[i]]))
            score[i] += int(np.count_nonzero(np.abs(edge - c[i]) < tol))
    return float(offs[int(np.argmax(score))])


def _fsk_noncoherent(iq, fs, fsps, tones, find_and_decode):
    """Soft noncoherent 2FSK (tone-energy detector) fallback.

    The FSKDemodulator is a phase-difference discriminator over the whole
    channel band (fs_out = 10x the symbol rate): it hits the FM click threshold
    and needs ~15 dB Es/N0 for BER 1e-2 where integrate-and-dump tone
    correlators need ~9 dB (offline FEC-03 study),
    and it hands the Viterbi hard bits only. This path correlates each symbol
    against both tones after a CFO fit on the integrated spectrum, picks the
    symbol timing that maximises | |c1| - |c0| |, and returns SoftBits with
    llr = |c1| - |c0| so the FEC frame finder runs a soft Viterbi.
    Returns (SoftBits, FrameResult, n_syms, _CSRes) or None."""
    from dpmg.framing.frame_codec import SoftBits
    x = np.asarray(iq, dtype=np.complex64)
    t0, t1 = float(tones[0]), float(tones[1])
    sr = fs / float(fsps)
    if len(x) < 8 * fsps or t0 == t1:
        return None
    # CFO: slide the tone pair (each +-sr/2 wide) over the periodogram. Uses the
    # whole buffer's energy, so it holds far below the discriminator-based grid fit.
    nfft = 1 << int(np.ceil(np.log2(len(x))))
    P = np.fft.fftshift(np.abs(np.fft.fft(x, nfft)) ** 2)
    f = np.fft.fftshift(np.fft.fftfreq(nfft, 1.0 / fs))
    cs = np.concatenate(([0.0], np.cumsum(P)))
    lim = 0.45 * abs(t1 - t0)
    d = np.arange(-lim, lim + 1e-9, 25.0)

    def _band(fc):
        return cs[np.searchsorted(f, fc + sr / 2)] - cs[np.searchsorted(f, fc - sr / 2)]
    cfo = float(d[int(np.argmax(_band(t0 + d) + _band(t1 + d)))])
    n = np.arange(len(x))
    c0 = np.concatenate(([0j], np.cumsum(x * np.exp(-2j * np.pi * (t0 + cfo) / fs * n))))
    c1 = np.concatenate(([0j], np.cumsum(x * np.exp(-2j * np.pi * (t1 + cfo) / fs * n))))
    best = None
    for off in np.arange(8) * (float(fsps) / 8.0):
        ns = int((len(x) - off) / float(fsps))
        e = np.round(off + np.arange(ns + 1) * float(fsps)).astype(np.int64)
        a0 = np.abs(c0[e[1:]] - c0[e[:-1]])
        a1 = np.abs(c1[e[1:]] - c1[e[:-1]])
        score = float(np.mean(np.abs(a1 - a0)))
        if best is None or score > best[0]:
            best = (score, a0, a1)
    _s, a0, a1 = best
    llr = (a1 - a0) / (float(np.median(a0 + a1)) + 1e-12)
    bs = SoftBits(bytes((llr > 0).astype(np.uint8) + 48).decode("ascii"), llr)
    hi, lo = np.maximum(a0, a1) ** 2, np.minimum(a0, a1) ** 2
    snr = 10.0 * np.log10(max(float(np.mean(hi) - np.mean(lo)), 1e-12)
                          / (float(np.mean(lo)) + 1e-12))
    return bs, find_and_decode(bs), len(llr), _CSRes(bs, [None] * len(llr), snr)


_G4 = {0: "00", 1: "01", 2: "11", 3: "10"}      # Gray index: dibit


def _diffpsk_decode(iq, fsps, m, find_and_decode):
    """PI4-QPSK / DQPSK / OQPSK detector: boxcar integrate-and-dump symbols
    (TX pulses are rectangular), 4th-power common-phase recovery, and a small
    hypothesis search (timing x rotation x parity); sync+CRC arbitrates.
    fsps is the BIT sps: these mods carry 2 bits/symbol, so the symbol
    stride is 2*fsps (same convention as the QPSK path)."""
    x0 = np.asarray(iq, dtype=np.complex128)
    fsym = 2.0 * float(fsps)
    w = max(1, int(fsym / 4))
    n_sym = int((len(x0) - fsym) / fsym)
    if n_sym < 12:
        return None
    best = None
    # Coarse carrier offset first (two-radio links: ~200 Hz between
    # undisciplined TCXOs). pi/4-QPSK alternates grids, so its 4th power flips
    # sign every symbol (line at 4*CFO +/- Rs/2): use the 8th power.
    # Try the uncorrected buffer first so single-radio decodes take the plain path.
    cands = [0.0]
    if m in ("PI4-QPSK", "PI4QPSK", "DQPSK"):
        try:
            _M = 8 if m != "DQPSK" else 4
            _c = _mth_power_cfo(x0, 1.0, _M, 0.3 / fsym)     # fs=1: cycles/sample
            if abs(_c) * fsym > 0.002:                        # > 0.2 % of the symbol rate
                cands.append(float(_c))
        except Exception:
            pass
    if m == "OQPSK":
        # The raw samples of rectangular-pulse OQPSK sit on the four QPSK
        # points at every instant but the rail transitions, so their 4th power
        # is a clean line at 4*CFO. (It is the symbol-centre samples, where the
        # other rail is mid-transition, that the 4th power cannot use.) Without
        # this the only carrier step is the per-symbol slope below, which wraps
        # past Rs/8 (600 Hz at 9600 bit/s) and loses every uplink OQPSK frame
        # between two free-running radios. Search up to the 4th-power alias
        # limit; the measured offset goes first because it is the link.
        try:
            _c = _mth_power_cfo(x0, 1.0, 4, 0.45 / 4)
            if abs(_c) * fsym > 0.002:
                cands.insert(0, float(_c))
        except Exception:
            pass
    for _cfo in cands:
        x = x0 if _cfo == 0.0 else x0 * np.exp(-2j * np.pi * _cfo * np.arange(len(x0)))
        for off in range(0, int(round(fsym)), 2):
            centers = (off + (np.arange(n_sym) + 0.5) * fsym).astype(int)
            centers = centers[centers < len(x) - w]
            if len(centers) < 12:
                continue
            syms = np.array([np.mean(x[c - w:c + w]) for c in centers])

            if m == "DQPSK":
                z = syms[1:] * np.conj(syms[:-1])
                d = np.angle(z)
                bias = np.angle(np.sum(np.exp(4j * d))) / 4.0   # common mod-90 bias (CFO)
                idx = np.round((d - bias) / (np.pi / 2)).astype(int) % 4
                bits = "".join({0: "00", 1: "01", 2: "11", 3: "10"}[i] for i in idx)
                fr = find_and_decode(bits)
                if fr.valid:
                    return bits, fr, len(syms), _CSRes(bits, syms, _est_snr_db(syms, "QPSK"))
                if best is None:
                    best = (bits, fr, len(syms), _CSRes(bits, syms, _est_snr_db(syms, "QPSK")))
                continue

            if m in ("PI4-QPSK", "PI4QPSK"):
                for parity in (0, 1):
                    r = syms.copy()
                    r[(np.arange(len(r)) + parity) % 2 == 1] *= np.exp(1j * np.pi / 4)
                    # grid-A angles are 45+90k: 4th power: common angle 180+4*phi.
                    # A residual carrier offset rotates that angle 4x per symbol
                    # and smears the sum (20 Hz at 2400 sym/s is enough to lose it):
                    # take the per-symbol slope off first, as OQPSK does.
                    z4 = r ** 4
                    slope = np.angle(np.sum(z4[1:] * np.conj(z4[:-1]))) / 4.0
                    r = r * np.exp(-1j * slope * np.arange(len(r)))
                    phi = (np.angle(np.sum(r ** 4)) - np.pi) / 4.0
                    for k4 in range(4):
                        s = r * np.exp(-1j * (phi + k4 * np.pi / 2))
                        gi = np.round((np.angle(s) - np.pi / 4) / (np.pi / 2)).astype(int) % 4
                        bits = "".join(_G4[i] for i in gi)
                        fr = find_and_decode(bits)
                        if fr.valid:
                            return bits, fr, len(s), _CSRes(bits, s, _est_snr_db(s, "QPSK"))
                        if best is None:
                            best = (bits, fr, len(s), _CSRes(bits, s, _est_snr_db(s, "QPSK")))
                continue

            if m == "OQPSK":
                half = int(round(fsym / 2))
                # The staggered constellation poisons 4th-power estimates, so
                # search a small hypothesis grid instead: {no-CFO, estimated-CFO}
                # x 16 phases. Rails are extracted once per (timing, slope);
                # rotations are scalar multiplies. A sync-hamming prescreen keeps
                # full frame parsing off the losing hypotheses.
                z = syms ** 4
                slope = (np.angle(np.sum(z[1:] * np.conj(z[:-1]))) / 4.0) / fsym
                _SYNC = "1011001000111011"
                _gmap = {"00": "00", "10": "01", "11": "11", "01": "10"}
                for sl in (0.0, slope):
                    xc = x * np.exp(-1j * sl * np.arange(len(x))) if sl else x
                    ci = centers
                    cq = centers + half
                    cq = cq[cq < len(xc) - w]
                    nn = min(len(ci), len(cq))
                    cI = np.array([np.mean(xc[c - w:c + w]) for c in ci[:nn]])
                    cQ = np.array([np.mean(xc[c - w:c + w]) for c in cq[:nn]])
                    for ph in np.arange(0.0, 2 * np.pi, np.pi / 8):
                        rot = np.exp(-1j * ph)
                        iv = (cI * rot).real
                        qv = (cQ * rot).imag
                        raw = "".join(("0" if a >= 0 else "1") +
                                      ("0" if b >= 0 else "1")
                                      for a, b in zip(iv, qv))
                        bits = "".join(_gmap[raw[i:i + 2]]
                                       for i in range(0, len(raw), 2))
                        hs = min((sum(a != b for a, b in
                                      zip(bits[i:i + 16], _SYNC))
                                  for i in range(0, max(1, len(bits) - 16))),
                                 default=99)
                        hi = min((sum(a == b for a, b in
                                      zip(bits[i:i + 16], _SYNC))
                                  for i in range(0, max(1, len(bits) - 16))),
                                 default=99)
                        if hs > 3 and (16 - hi) > 3:
                            continue                     # sync nowhere near: skip
                        fr = find_and_decode(bits)
                        if fr.valid:
                            sc = iv + 1j * qv
                            return bits, fr, nn, _CSRes(bits, sc,
                                                        _est_snr_db(sc, "QPSK"))
                        if best is None:
                            sc = iv + 1j * qv
                            best = (bits, fr, nn, _CSRes(bits, sc,
                                                         _est_snr_db(sc, "QPSK")))
                continue
    return best


def _higher_order_decode(iq, fs, fsps, m, homod, find_and_decode):
    """8PSK / 8QAM / 16QAM / 16APSK / 32APSK blind recovery. The
    HigherOrderDemodulator is a bare nearest-point slicer (no AGC, no carrier
    or phase recovery), so over any real link (even a single radio with an
    arbitrary channel phase) it decodes only by luck. This wrapper does, in
    order: coarse carrier offset (M-th power FFT, M chosen from the
    constellation's own symmetry: 8PSK 8, 16QAM 4, 16APSK 12, 32APSK 16);
    boxcar integrate-and-dump over every symbol-timing offset; power-gated AGC
    to the unit-energy table; residual-CFO slope + windowed M-th-power phase
    tracking (TCXO drift / mixer phase noise); then the M rotation hypotheses,
    ranked by slicer error, with sync+CRC arbitrating. Returns
    (bits, frame, nsyms, res) or None."""
    x0 = np.asarray(iq, dtype=np.complex128)
    N = len(x0)
    fsym = float(fsps)
    # Integrate the middle ~60 % of each (rectangular) symbol: the channel
    # lowpass rounds the edges and a full-symbol boxcar folds that ISI into the
    # estimate, which the dense 16QAM / 16APSK tables cannot afford.
    w = max(2, int(round(0.6 * fsym)))
    lead = int(round((fsym - w) / 2.0))
    tab = np.asarray(homod.symbols, dtype=np.complex128)
    labels = list(homod.labels)
    n_sym = int((N - fsym) / fsym)
    if n_sym < 16 or len(tab) == 0:
        return None
    # M from the table's own M-fold symmetry (strongest normalised M-th moment)
    M = max((4, 8, 12, 16),
            key=lambda k: abs(np.mean(tab ** k)) / (np.mean(np.abs(tab) ** k) + 1e-12))
    # The table's own M-th-power angle: 0 for 8PSK (points at 2*pi*k/M), pi for
    # 16QAM / 16APSK (points at pi/M + 2*pi*k/M). Dividing the raw angle by M
    # without it rotates the slicer by pi/M (15 deg at M=12: every 16APSK
    # symbol lands between two outer-ring points).
    ref = float(np.angle(np.mean(tab ** M)))
    x0 = x0 / (np.sqrt(np.mean(np.abs(x0) ** 2)) + 1e-12)
    cands = [0.0]
    try:
        _near = 0.3 * fs / fsym
        _c = _mth_power_cfo(x0, fs, M, _near)
        if abs(_c) > 0.002 * fs / fsym:
            cands.insert(0, float(_c))          # measured offset first: it is the link
        # Then out to the M-th-power alias limit (0.45 fs/M: 5.4 kHz for 8PSK
        # in a 96 kHz channel). 0.3 Rs is 2880 Hz at 9600 Bd, and two
        # free-running radios at 915 MHz are often 3 ppm apart: an offset just
        # past it would be cut out of the search, and the decode would come and go with
        # the radios' temperature. A wide pick inside the near range is the
        # same peak; one outside it goes first and the near pick stays behind it.
        _wide = 0.45 * fs / M
        if _wide > _near:
            _cw = _mth_power_cfo(x0, fs, M, _wide)
            if abs(_cw) > _near:
                cands.insert(0, float(_cw))
    except Exception:
        pass
    WIN = 24                                     # symbols of phase smoothing
    best = None
    for cfo in cands:
        x = x0 if cfo == 0.0 else x0 * np.exp(-2j * np.pi * cfo / fs * np.arange(N))
        C = np.concatenate(([0.0], np.cumsum(x)))
        hyps = []
        for off in range(int(round(fsym))):
            i0 = (off + lead + np.arange(n_sym) * fsym).round().astype(np.int64)
            i0 = i0[i0 + w <= N]
            if len(i0) < 16:
                continue
            est = (C[i0 + w] - C[i0]) / w
            p = np.abs(est) ** 2
            # burst gate: 16QAM's inner corners and 16APSK's inner ring sit
            # ~9 dB under the peak symbols, so gate well below that (-17 dB)
            act = p > 0.02 * p.max()
            if int(act.sum()) < 8:
                continue
            est = est / (np.sqrt(np.mean(p[act])) + 1e-12)
            z = est ** M
            za = z * act
            slope = np.angle(np.sum(za[1:] * np.conj(za[:-1]))) / M
            est = est * np.exp(-1j * slope * np.arange(len(est)))
            z = (est ** M) * act
            zs = np.convolve(z, np.ones(WIN), mode="same")
            phi = (np.unwrap(np.angle(zs + 1e-12)) - ref) / M
            est = est * np.exp(-1j * phi)
            for r in range(M):
                s = est * np.exp(-2j * np.pi * r / M)
                d = np.abs(s[:, None] - tab[None, :])
                near = np.argmin(d, axis=1)
                err = float(np.mean(d[np.arange(len(s)), near][act] ** 2))
                hyps.append((err, off, r, s, near))
        hyps.sort(key=lambda h: h[0])
        for err, off, r, s, near in hyps[: max(M, 8)]:
            bits = "".join(labels[i] for i in near)
            fr = find_and_decode(bits)
            res = _CSRes(bits, s, _est_snr_db(s, m))
            if fr is not None and getattr(fr, "valid", False):
                return bits, fr, len(s), res
            if best is None:
                best = (bits, fr, len(s), res)
    return best


def demod_with_recovery(iq, fs, sps, modulation, demod_obj, find_and_decode, sync_bits=None, frame_bits=None, lock=None,
                        coarse_cfo=True):
    """Carrier-recover, then try each symbol-timing offset; return first valid decode.

    Returns (bit_string, FrameResult, n_syms, raw_DemodResult). If nothing validates,
    returns the offset-0 attempt so the UI still shows bits/syms.
    """
    isps = max(2, int(round(sps)))
    fsps = max(2.0, float(sps))       # true fractional sps; isps is for RRC taps only
    m = (modulation or "").upper()
    if coarse_cfo and m in ("BPSK", "QPSK"):
        # The data-aided searches below reach +-0.02 (BPSK) and +-0.03 (QPSK)
        # cycles/sample: 1920 and 2880 Hz in the 96 kHz channel of a 9600 Bd
        # plan, short of the 3 ppm two free-running radios can be apart at
        # 915 MHz. Past that reach, take the M-th-power estimate (2 for BPSK,
        # 4 for QPSK) off first and run the same chain on the result; inside
        # it, or if that does not validate, the chain runs as it always did.
        try:
            _M = 2 if m == "BPSK" else 4
            _reach = (0.02 if m == "BPSK" else 0.03) * fs
            _c = _mth_power_cfo(np.asarray(iq), fs, _M, 0.45 * fs / _M)
            if abs(_c) > 0.8 * _reach:
                _xd = (np.asarray(iq) * np.exp(-2j * np.pi * _c / fs * np.arange(len(iq)))
                       ).astype(np.complex64)
                got = demod_with_recovery(_xd, fs, sps, modulation, demod_obj, find_and_decode,
                                          sync_bits=sync_bits, frame_bits=frame_bits, lock=lock,
                                          coarse_cfo=False)
                if got is not None and got[1] is not None and getattr(got[1], "valid", False):
                    return got
        except Exception:
            pass
    base = prep_baseband(np.asarray(iq), fs, modulation, sps=isps)
    best = None

    if m in _LINEAR_MODS:
        if m in ("PI4-QPSK", "PI4QPSK", "DQPSK", "OQPSK"):
            try:
                da = _diffpsk_decode(np.asarray(iq), fsps, m, find_and_decode)
            except Exception:
                da = None
            if da is not None and da[1] is not None and getattr(da[1], "valid", False):
                return da
            if da is not None:
                best = da
        try:
            if m == "QPSK":                       # QPSK data-aided path
                # QPSK recovery runs on raw iq, not the matched-filtered `base`
                # (prep_baseband's RRC filter breaks the sync correlation).
                da = _data_aided_qpsk(np.asarray(iq), isps, find_and_decode,
                                      sync_bits=sync_bits, fsps=fsps, frame_bits=frame_bits,
                                      lock=lock)
            else:
                da = _data_aided_linear(base, isps, m, find_and_decode,
                                        sync_bits=sync_bits, fsps=fsps, frame_bits=frame_bits)
        except Exception:
            da = None
        if da is not None and da[1] is not None and getattr(da[1], 'valid', False):
            return da
        if da is not None and best is None:
            best = da
        for off in range(isps):
            _n = int((len(base) - off) / fsps)
            if _n < 16:
                continue
            syms = base[(off + np.arange(_n) * fsps).round().astype(int)]
            if len(syms) < 16:
                continue
            bs, nsyms, scx = _centersample_bits(syms, isps, m)
            res = _CSRes(bs, scx, _est_snr_db(scx, m))
            fr = find_and_decode(bs)   # find_and_decode tries inverted internally
            if fr.valid:
                return bs, fr, nsyms, res
            if best is None:
                best = (bs, fr, nsyms, res)
        return best if best else ("", None, 0, None)

    if m in _HIGHER_ORDER_MODS and demod_obj is not None and hasattr(demod_obj, "_mod"):
        try:
            da = _higher_order_decode(np.asarray(iq), fs, fsps, m, demod_obj._mod,
                                      find_and_decode)
        except Exception:
            da = None
        if da is not None and da[1] is not None and getattr(da[1], "valid", False):
            return da
        if da is not None:
            best = da

    # Non-linear (FSK/etc): fall back to the existing demod object over timing offsets.
    # Replace the centroid-corrected base with a grid-fit CFO
    # correction derived from the raw iq and the demod's own tone grid.
    _tn = getattr(demod_obj, "tones", None)
    if _tn is None and hasattr(demod_obj, "tone0"):
        _t1 = getattr(demod_obj, "tone1", None)
        _tn = [demod_obj.tone0, _t1 if _t1 is not None else -demod_obj.tone0]
    if demod_obj is not None and _tn:
        try:
            _cfo = _fsk_grid_cfo(np.asarray(iq), fs, np.asarray(_tn, dtype=float))
            _tt = np.arange(len(iq)) / fs
            base = (np.asarray(iq) * np.exp(-2j * np.pi * _cfo * _tt)).astype(np.complex64)
        except Exception:
            pass  # keep the centroid-corrected base as fallback
    # The self-acquiring despreader searches every chip-timing
    # offset itself, so the outer timing sweep is redundant for it (the same
    # answer isps times over). QPSK-DSSS leaves a 4-fold carrier ambiguity
    # after its 4th-power phase estimate; find_and_decode's normal+inverted
    # pair covers only two of the four quadrants, so try all four 90-deg
    # rotations like Modem.receive does (offline: 6/8 without, 8/8 with); pi/4-QPSK-DSSS
    # adds a 45-deg grid-parity ambiguity: eight rotations (DSSS_CARRIERS).
    _self_acq = hasattr(demod_obj, "_despread")
    _rots = [0.0]
    if _self_acq:
        try:
            from dpmg.modulation.spread_spectrum import dsss_rotations
            _rots = dsss_rotations(getattr(demod_obj, "modem", "bpsk"))   # 1 / 4 / 8 (pi/4) steps
        except Exception:
            _rots = [0.0]
    _offs = (0,) if _self_acq else range(isps)
    # A CDMA-overlay user is despread after cancelling the other
    # users first; if that does not validate, the plain despread is tried too
    _mui = (True, False) if (_self_acq and getattr(demod_obj, "mui_pns", None)) else (None,)
    for _mm in _mui:
        if _mm is not None:
            demod_obj.mui_enabled = _mm
        for off in _offs:
            shifted = base[off:]
            if len(shifted) < isps:
                continue
            for _rot in _rots:
                _x = shifted if _rot == 0.0 else (shifted * np.exp(1j * _rot)).astype(np.complex64)
                try:
                    res = demod_obj.demodulate(_x)
                except Exception:
                    continue
                bs = getattr(res, "bit_string", "") or ""
                nsyms = len(getattr(res, "symbols", []) or [])
                fr = find_and_decode(bs)
                if fr.valid:
                    if _mm is not None:
                        demod_obj.mui_enabled = True
                    return bs, fr, nsyms, res
                if best is None:
                    best = (bs, fr, nsyms, res)
    if _mui[0] is not None:
        demod_obj.mui_enabled = True
    # Only reached when every discriminator attempt above failed,
    # so frames the discriminator decodes never reach it; this buys ~5 dB uncoded and
    # passes soft bits to the Viterbi on FEC channels.
    if m in ("FSK", "2FSK") and _tn is not None and len(_tn) == 2 and not _self_acq:
        try:
            nc = _fsk_noncoherent(np.asarray(iq), fs, fsps, _tn, find_and_decode)
        except Exception:
            nc = None
        if nc is not None and nc[1] is not None and getattr(nc[1], "valid", False):
            return nc
    return best if best else ("", None, 0, None)
