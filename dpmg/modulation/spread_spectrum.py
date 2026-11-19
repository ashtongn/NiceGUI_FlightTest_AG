"""
Spread-spectrum waveforms: DSSS (direct-sequence spreading over a BPSK, QPSK,
OQPSK or pi/4-QPSK carrier, one or two codes) and CSS/LoRa (chirp spread
spectrum). TX modulators + RX demodulators, framework-compatible
(ModulationResult / DemodResult). Both carry processing gain, so they decode
well below the noise floor. Round-trip pinned by test_spread_roundtrip.py.

BPSK convention matches the rest of the tool: bit '0' is +1, bit '1' is -1.
"""
from __future__ import annotations

import numpy as np

from dpmg.modulation.modulator import Modulator, ModulationResult
from dpmg.demodulation.demodulator import SoftDemodulator, DemodResult, DemodSymbol


# Per-process cache of the overlay cancellation of a buffer (the K
# users of one window decode from the same baseband)
_MUI_CACHE: dict = {}


def pn_code(length: int, seed: int = 1) -> np.ndarray:
    """Deterministic +/-1 PN chip sequence. Same seed on TX and RX despread."""
    rng = np.random.default_rng(seed)
    return (rng.integers(0, 2, length) * 2 - 1).astype(np.float64)


# DSSS: each data bit: spread_factor BPSK chips (bit XOR PN), despread by
# correlation. Processing gain ~ 10*log10(spread_factor) dB.

# QPSK-DSSS gray dibit <-> unit-circle symbol (matches QPSKModulator packing).
_QPSK_SYM = {"00": (1 + 1j), "01": (-1 + 1j), "11": (-1 - 1j), "10": (1 - 1j)}
_QPSK_INV = {0: "00", 1: "01", 2: "11", 3: "10"}   # quadrant (angle//90): dibit

# DSSS_CARRIERS: the carrier families a DSSS chip stream can ride. The same
# spreading code is applied per chip in every family; what differs is the
# symbol constellation the chips multiply (and, for OQPSK, the half-chip
# stagger of the Q rail). MSK / GMSK-shaped chips are not offered: the modem
# has no MSK chip shaper (its GFSK is a frequency modulation, not a chip
# filter), so the families stop at the four linear PSK carriers.
DSSS_CARRIERS = ("bpsk", "qpsk", "oqpsk", "pi4qpsk")
_CARRIER_ALIAS = {"bpsk": "bpsk", "qpsk": "qpsk", "oqpsk": "oqpsk", "pi4qpsk": "pi4qpsk",
                  "pi4": "pi4qpsk", "pi4-qpsk": "pi4qpsk", "pi/4-qpsk": "pi4qpsk",
                  "π/4-qpsk": "pi4qpsk", "π/4qpsk": "pi4qpsk"}


def canon_carrier(name) -> str:
    """'QPSK' / 'pi/4-QPSK' / 'OQPSK' / 'BPSK': the DSSS_CARRIERS key."""
    m = str(name or "bpsk").strip().lower().replace("_", "").replace("-dsss", "")
    m = m.replace("dsss-", "")
    if m in _CARRIER_ALIAS:
        return _CARRIER_ALIAS[m]
    m2 = m.replace("-", "").replace("/", "")
    if m2 in _CARRIER_ALIAS:
        return _CARRIER_ALIAS[m2]
    raise ValueError(f"unknown DSSS carrier {name!r} (have {DSSS_CARRIERS})")


def _dsss_code(spread_factor, pn_seed, code_spec):
    """Resolve the spreading code: a code_spec dict (family selector) wins;
    otherwise the legacy seed PN of length spread_factor."""
    from dpmg.modulation.spreading_codes import resolve_code
    if code_spec:
        return resolve_code(code_spec, spread_factor)
    return resolve_code({"family": "seed", "length": int(spread_factor),
                         "seed": int(pn_seed)}, spread_factor)


def _dsss_symbols(bit_string: str, carrier: str):
    """Bits: complex unit symbols for the carrier (QPSK map shared by qpsk /
    oqpsk / pi4qpsk grid A; pi4qpsk grid B = grid A rotated by -45 deg on the
    odd symbols). BPSK: '0' is +1, '1' is -1."""
    if carrier == "bpsk":
        return np.array([1.0 if b == "0" else -1.0 for b in bit_string], dtype=np.complex128)
    bits = bit_string + ("0" if len(bit_string) % 2 else "")
    syms = np.array([_QPSK_SYM[bits[i:i + 2]] for i in range(0, len(bits), 2)],
                    dtype=np.complex128) / np.sqrt(2.0)
    if carrier == "pi4qpsk":
        syms[1::2] *= np.exp(-1j * np.pi / 4)          # grid B on the odd symbols
    return syms


def dsss_rotations(carrier) -> list:
    """Input rotations the frame layer must try for a DSSS carrier: BPSK has
    none (the frame search covers inversion), the QPSK family four 90-deg
    steps, pi/4-QPSK eight 45-deg steps (grid parity)."""
    c = canon_carrier(carrier)
    if c == "bpsk":
        return [0.0]
    if c == "pi4qpsk":
        return [k * np.pi / 4 for k in range(8)]
    return [k * np.pi / 2 for k in range(4)]


class DSSSModulator(Modulator):
    """Direct-sequence spread spectrum over a BPSK, QPSK, OQPSK or pi/4-QPSK
    carrier (modem=...). One data symbol: len(code) chips, each chip held
    samples_per_chip samples. The chip code comes from code_spec (seed / m /
    gold / custom / lfsr) or the legacy seed. Dual code: code_spec_q spreads
    the Q rail with a second code (complex spreading, qpsk / oqpsk only);
    without it the one real code multiplies the complex symbol."""

    def __init__(self, symbol_rate: float, samples_per_bit: int,
                 spread_factor: int = 15, amplitude: float = 0.8,
                 pn_seed: int = 1, code_spec: dict = None, modem: str = "bpsk",
                 code_spec_q: dict = None):
        super().__init__(symbol_rate, samples_per_bit, amplitude)
        self.pn = _dsss_code(spread_factor, pn_seed, code_spec)
        self.sf = int(len(self.pn))                 # true chips/symbol
        self.spc = max(1, self.samples_per_bit // self.sf)
        self.modem = canon_carrier(modem)
        self.pn_q = None
        if code_spec_q:
            if self.modem not in ("qpsk", "oqpsk"):
                raise ValueError("code_spec_q (dual-code spreading) needs a qpsk or oqpsk carrier")
            self.pn_q = _dsss_code(spread_factor, pn_seed, code_spec_q)
            if len(self.pn_q) != self.sf:
                raise ValueError("code_spec_q must have the same length as code_spec")
        if self.modem == "oqpsk" and self.spc % 2:
            raise ValueError("OQPSK-DSSS needs an even samples_per_chip (half-chip Q stagger)")
        # Modulate() emits sf*spc samples per symbol, not
        # samples_per_bit. When the caller's samples_per_bit is not a multiple
        # of the code length (gen_scenario's FSK-derived 9 at SF15: 15
        # emitted), the base-class rate (symbol_rate*samples_per_bit) is wrong:
        # resampling the burst by it puts the symbols on air at
        # symbol_rate*samples_per_bit/(sf*spc) (1440 Bd for a 2400 Bd plan) and
        # no despreader built from the plan ever locks. Declare the real rate.
        self.sample_rate = int(round(self.symbol_rate * self.sf * self.spc))

    def modulate(self, bit_string: str) -> ModulationResult:
        syms = _dsss_symbols(bit_string, self.modem) * self.amplitude
        pn_i = self.pn
        pn_q = self.pn_q if self.pn_q is not None else self.pn
        # chips: Re(s) * code_I + j Im(s) * code_Q  (== s * code for one code)
        ci = np.outer(syms.real, pn_i)
        cq = np.outer(syms.imag, pn_q)
        if self.modem == "oqpsk":
            # Q rail delayed half a chip. The burst keeps exactly nsym*sf*spc
            # samples (the last Q chip loses its trailing half): a burst one
            # sample longer than a symbol multiple drifts its chip timing by
            # one sample per copy when the file loops (gen_scenario tiles it,
            # the radio replays it), so no single despread timing fits more
            # than one copy, and OQPSK users in an overlay are missed.
            half = self.spc // 2
            i_s = np.repeat(ci.ravel(), self.spc)
            q_s = np.concatenate([np.zeros(half), np.repeat(cq.ravel(), self.spc)])[:len(i_s)]
            out = i_s + 1j * q_s
        else:
            out = np.repeat((ci + 1j * cq).ravel(), self.spc)
        return ModulationResult(iq_samples=out.astype(np.complex128).tolist(),
                                sample_rate=self.sample_rate)


class DSSSDemodulator(SoftDemodulator):
    """Self-acquiring DSSS despreader for every carrier family: searches the
    chip-timing offset (RF arrival is arbitrary), removes a residual carrier
    offset from the per-symbol correlations (M-th power slope; up to
    +/- symbol_rate / 2M, i.e. +/-300 Hz at 2400 Bd QPSK: the G40-like CFO
    profile) and recovers the common carrier phase (2nd power for BPSK, 4th
    for the QPSK family). OQPSK despreads the I rail at the chip timing and
    the Q rail half a chip later. Residual 90-deg ambiguities (45-deg for
    pi/4-QPSK, whose grid parity is unobservable blind) are the frame layer's:
    Modem.receive / demod_with_recovery try dsss_rotations() of the input."""

    def __init__(self, sample_rate: float, symbol_rate: float,
                 samples_per_bit: int, spread_factor: int = 15,
                 pn_seed: int = 1, code_spec: dict = None, modem: str = "bpsk",
                 code_spec_q: dict = None, overlay_codes: list = None):
        super().__init__(sample_rate, symbol_rate, samples_per_bit)
        self.pn = _dsss_code(spread_factor, pn_seed, code_spec)
        self.sf = int(len(self.pn))
        self.spc = max(1, int(round(float(samples_per_bit))) // self.sf)
        self.modem = canon_carrier(modem)
        self.pn_q = _dsss_code(spread_factor, pn_seed, code_spec_q) if code_spec_q else None
        if self.pn_q is not None and len(self.pn_q) != self.sf:
            raise ValueError("code_spec_q must have the same length as code_spec")
        # The other users of a CDMA overlay (same centre, symbol rate
        # and code length: plan_adapter forwards them as overlay_codes) are
        # cancelled before this user is despread (_mui_cancel). Only BPSK
        # single-code users take part, as target and as cancelled sibling;
        # every other carrier keeps the plain despreader.
        self.mui_pns = []
        self.mui_enabled = True
        if overlay_codes and self.modem == "bpsk" and self.pn_q is None:
            for oc in overlay_codes:
                try:
                    oc = dict(oc or {})
                    if oc.get("code_spec_q") or canon_carrier(oc.get("dsss_modem", "bpsk")) != "bpsk":
                        continue
                    pn = _dsss_code(spread_factor, pn_seed, oc.get("code_spec") or oc)
                except Exception:
                    continue
                if len(pn) == self.sf and not np.array_equal(pn, self.pn):
                    self.mui_pns.append(np.asarray(pn, dtype=np.float64))

    def _despread(self, x: np.ndarray, off: int, pn=None) -> np.ndarray:
        per_sym = self.sf * self.spc
        seg = x[off:]
        nsym = len(seg) // per_sym
        if nsym < 1:
            return np.zeros(0, dtype=np.complex128)
        m = seg[:nsym * per_sym].reshape(nsym, self.sf, self.spc).mean(axis=2)
        return m @ (self.pn if pn is None else pn)   # complex correlation per symbol

    @staticmethod
    def _deslope(c: np.ndarray, M: int) -> np.ndarray:
        """Remove a per-symbol phase ramp (residual CFO) via the M-th power:
        the peak of the zero-padded spectrum of c^M (coherent over the whole
        buffer, parabolic refinement). A per-symbol differential average
        is noisy enough under multi-user interference that its
        error, accumulated over a frame, rotates the OQPSK rails apart.
        Range +/- symbol_rate / (2 M)."""
        n = len(c)
        if n < 8:
            return c
        z = c ** M
        z = z / (np.sqrt(np.mean(np.abs(z) ** 2)) + 1e-12)
        nfft = 1 << int(np.ceil(np.log2(n * 8)))
        S = np.abs(np.fft.fft(z, nfft))
        k = int(np.argmax(S))
        a, b, cc = S[k - 1], S[k], S[(k + 1) % nfft]
        den = a - 2 * b + cc
        d = 0.5 * (a - cc) / den if abs(den) > 1e-12 else 0.0
        f = (k + d) / nfft
        if f > 0.5:
            f -= 1.0
        slope = 2 * np.pi * f / M
        return c * np.exp(-1j * slope * np.arange(n))

    def _timing(self, x: np.ndarray):
        """Best chip/symbol timing offset by despread energy (and, for OQPSK,
        the Q-rail timing half a chip later)."""
        per_sym = self.sf * self.spc
        pn_q = self.pn_q if self.pn_q is not None else self.pn
        if self.modem == "oqpsk":
            return self._oqpsk_acquire(x)[0]
        best_off, best_met = 0, -1.0
        for off in range(per_sym):
            c = self._despread(x, off)
            if not len(c):
                continue
            met = float(np.mean(np.abs(c)))
            if self.pn_q is not None:
                cq = self._despread(x, off, pn_q)
                if len(cq):
                    met = 0.5 * (met + float(np.mean(np.abs(cq))))
            if met > best_met:
                best_met, best_off = met, off
        return best_off

    def _oqpsk_acquire(self, x: np.ndarray):
        """Joint chip-timing x carrier-quadrant search for the
        staggered rails. A magnitude metric cannot tell the I-rail timing from
        the Q-rail timing half a chip later (both despread to ~1.1 of a rail),
        and the 4th-power phase of ca+cb is biased by non-circular
        interference (BPSK users of an overlay share the carrier phase), so
        score each (offset, phi + k*90) on what the slicer will read: the
        I rail's real part at `off` plus the Q rail's imaginary part at
        `off + half`. Returns (off, phi, ca, cb) with ca / cb de-sloped."""
        per_sym = self.sf * self.spc
        half = self.spc // 2
        pn_q = self.pn_q if self.pn_q is not None else self.pn
        best = None
        for off in range(per_sym):
            ca = self._despread(x, off)
            cb = self._despread(x, off + half, pn_q)
            n = min(len(ca), len(cb))
            if n < 2:
                continue
            ca, cb = ca[:n], cb[:n]
            s = self._deslope(ca + cb, 4)
            ramp = s / (ca + cb + 1e-12)
            ca, cb = ca * ramp, cb * ramp
            phi4 = 0.25 * np.angle(np.sum(s ** 4)) - np.pi / 4
            for k in range(4):
                ph = phi4 + k * np.pi / 2
                rot = np.exp(-1j * ph)
                # rail energy (squares): right timing+quadrant ~ 1 + 1, the
                # Q-rail timing with a 90-deg error ~ 1 + 1/4: wider margin
                # than |.| sums under multi-user interference
                met = float(np.mean((ca * rot).real ** 2) + np.mean((cb * rot).imag ** 2))
                if best is None or met > best[0]:
                    best = (met, off, ph, ca, cb)
        if best is None:
            return 0, 0.0, np.zeros(0, dtype=np.complex128), np.zeros(0, dtype=np.complex128)
        _m, off, ph, ca, cb = best
        # one decision-directed refinement: sum(ca * a_hat) and sum(cb * -j b_hat)
        # both point at e^{j phi} with the half-chip leakage averaged out
        rot = np.exp(-1j * ph)
        a_hat = np.sign((ca * rot).real); b_hat = np.sign((cb * rot).imag)
        a_hat[a_hat == 0] = 1; b_hat[b_hat == 0] = 1
        v = np.sum(ca * a_hat) + np.sum(cb * (-1j) * b_hat)
        if abs(v) > 0:
            ph = float(np.angle(v))
        return off, ph, ca, cb

    # Multi-user interference cancellation for CDMA overlays
    # An equal-power overlay of K BPSK users that all leave one transmitter
    # (the scenario composite) shares carrier phase, frame layout and data, so
    # the despread interference is not noise-like: it is the same real bias on
    # every repeat of a frame (U07 of the 16-user preset acquires
    # the wrong chip timing at 60 dB SNR and loses 6 bits on every copy;
    # combining repeats cannot help). With every code known, estimate every
    # user (timing, CFO slope, phase, amplitude, hard symbols), rebuild the
    # chip streams, fit the common chip pulse (channelizer filter + resampler)
    # by least squares, and run parallel interference cancellation: each pass
    # re-acquires every user (timing included) on x minus all the other users.
    # The target is then despread by the unchanged path from x minus the other
    # users' reconstruction.
    _MUI_ITERS = 3
    _MUI_NTAP = 7
    _MUI_MIN_Q = 2.0          # mean/std of |v| on active symbols: BPSK ~2.8 at
    #                           9 dB SINR, a code that is not on air ~1.3

    @staticmethod
    def _mui_fft_corr(x: np.ndarray, w: np.ndarray) -> np.ndarray:
        nf = 1 << int(np.ceil(np.log2(len(x) + len(w))))
        return np.fft.ifft(np.fft.fft(x, nf) * np.conj(np.fft.fft(w, nf)))[:len(x)]

    def _mui_estimate(self, x: np.ndarray, pn: np.ndarray) -> dict | None:
        """Acquire one BPSK user on x: chip timing (despread energy over every
        offset, FFT correlator == _despread), CFO slope + phase (2nd power),
        amplitude, active-symbol mask and hard symbols."""
        spc, per = self.spc, self.sf * self.spc
        n = len(x)
        r = self._mui_fft_corr(x, np.repeat(pn, spc).astype(np.complex128)) / spc
        ns = (n - 2 * per + 1) // per + 1
        if ns < 8:
            return None
        off = int(np.argmax(np.mean(np.abs(r[:ns * per].reshape(ns, per)), axis=0)))
        nsym = (n - per - off) // per + 1
        c = r[off + np.arange(nsym) * per]
        cd = self._deslope(c, 2)
        # the ramp _deslope removed: cd_k conj(c_k) = |c_k|^2 e^{-j slope k}
        slope = -float(np.angle(np.sum(cd[1:] * np.conj(c[1:]) * np.conj(cd[:-1] * np.conj(c[:-1])))))
        phi = 0.5 * float(np.angle(np.sum(cd ** 2)))
        v = (cd * np.exp(-1j * phi)).real
        av = np.abs(v)
        p90 = float(np.percentile(av, 90))
        big = av[av > 0.5 * p90]
        A = float(np.median(big)) if len(big) else 0.0
        if A <= 0:
            return None
        # active symbols: a 3-symbol run (left or right) above 0.6 A, so the
        # first / last symbol of a burst is kept and dead air is not rebuilt
        pad = np.concatenate([np.zeros(2), av, np.zeros(2)])
        sl = (pad[0:-4] + pad[1:-3] + pad[2:-2]) / 3.0
        sr = (pad[2:-2] + pad[3:-1] + pad[4:]) / 3.0
        act = (np.maximum(sl, sr) > 0.6 * A) & (av > 0.3 * A)
        q = float(np.mean(av[act]) / (np.std(av[act]) + 1e-12)) if np.sum(act) >= 8 else 0.0
        return {"off": off, "slope": slope, "phi": phi, "A": A, "q": q,
                "dec": np.where(act, np.sign(v) * A, 0.0)}

    def _mui_rebuild(self, n: int, pn: np.ndarray, e: dict) -> np.ndarray:
        """Rectangular-chip waveform of an estimated user (chip amplitude A/L,
        the symbol phase ramp continued inside each symbol)."""
        spc, per = self.spc, self.sf * self.spc
        out = np.zeros(n, dtype=np.complex128)
        s = np.repeat((e["dec"][:, None] * pn[None, :]).ravel() / self.sf, spc)
        nn = np.arange(len(s))
        s = s * np.exp(1j * (e["phi"] + e["slope"] * (nn - 0.5 * (per - 1)) / per))
        o = int(e["off"])
        m = max(0, min(len(s), n - o))
        out[o:o + m] = s[:m]
        return out

    def _mui_cancel(self, x: np.ndarray) -> np.ndarray:
        """x minus every other overlay user. Cached per buffer, so
        the K decodes of one window in a pool worker estimate the set once."""
        spc, per = self.spc, self.sf * self.spc
        n = len(x)
        if not self.mui_pns or n < 10 * per:
            return x
        import hashlib
        pns = [self.pn] + list(self.mui_pns)
        keys = [p.tobytes() for p in pns]
        order = sorted(range(len(pns)), key=lambda i: keys[i])
        pns = [pns[i] for i in order]
        own = order.index(0)
        ck = (hashlib.sha1(np.ascontiguousarray(x).tobytes()).hexdigest(), n, spc,
              hashlib.sha1(b"".join(keys[i] for i in order)).hexdigest())
        hit = _MUI_CACHE.get(ck)
        half = self._MUI_NTAP // 2
        lags = range(-half, half + 1)

        def filt(z, h):
            return sum(h[i] * np.roll(z, k) for i, k in enumerate(lags))

        if hit is None:
            est = [self._mui_estimate(x, p) for p in pns]
            h = np.zeros(len(lags), dtype=np.complex128)
            h[half] = 1.0
            tot = np.zeros(n, dtype=np.complex128)
            fy = [None] * len(pns)
            for _it in range(self._MUI_ITERS):
                use = [i for i, e in enumerate(est) if e is not None and e["q"] >= self._MUI_MIN_Q]
                if not use:
                    break
                rb = {i: self._mui_rebuild(n, pns[i], est[i]) for i in use}
                y = sum(rb.values())
                cols = np.stack([np.roll(y, k) for k in lags], 1)[self._MUI_NTAP:-self._MUI_NTAP]
                h, *_ = np.linalg.lstsq(cols, x[self._MUI_NTAP:-self._MUI_NTAP], rcond=None)
                fy = [filt(rb[i], h) if i in rb else None for i in range(len(pns))]
                tot = sum(f for f in fy if f is not None)
                est = [self._mui_estimate(x - (tot - fy[i]) if fy[i] is not None else x - tot, p)
                       for i, p in enumerate(pns)]
            use = [i for i, e in enumerate(est) if e is not None and e["q"] >= self._MUI_MIN_Q]
            fy = {i: filt(self._mui_rebuild(n, pns[i], est[i]), h) for i in use}
            hit = {"fy": fy}
            if len(_MUI_CACHE) >= 4:
                _MUI_CACHE.pop(next(iter(_MUI_CACHE)))
            _MUI_CACHE[ck] = hit
        others = [f for i, f in hit["fy"].items() if i != own]
        if not others:
            return x
        return x - sum(others)

    def demodulate(self, iq_samples: np.ndarray) -> DemodResult:
        x = np.asarray(iq_samples, dtype=np.complex128)
        per_sym = self.sf * self.spc
        if len(x) < 2 * per_sym + self.spc:
            return DemodResult("", [], [], 0, 0.0)
        if self.mui_pns and self.mui_enabled and self.modem == "bpsk":   # overlay cancellation
            try:
                x = self._mui_cancel(x)
            except Exception:
                pass
        off = self._timing(x) if self.modem != "oqpsk" else 0
        pn_q = self.pn_q if self.pn_q is not None else self.pn
        bits, llrs, syms = [], [], []

        if self.modem == "bpsk":
            corr = self._deslope(self._despread(x, off), 2)
            phi = 0.5 * np.angle(np.sum(corr ** 2)) if len(corr) else 0.0
            vals = (corr * np.exp(-1j * phi)).real
            for k, v in enumerate(vals):
                bit = "0" if v >= 0 else "1"
                bits.append(bit)
                llrs.append(float(v))
                syms.append(DemodSymbol(k, [abs(float(v))], bit, [float(v)],
                                        min(1.0, abs(float(v)))))
            return DemodResult("".join(bits), llrs, syms, 0,
                               self._estimate_snr_db(np.asarray(iq_samples)))

        if self.modem == "oqpsk":
            off, phi, ca, cb = self._oqpsk_acquire(x)
            n = len(ca)
            ra = ca * np.exp(-1j * phi); rb = cb * np.exp(-1j * phi)
            for k in range(n):
                a, b = float(ra[k].real), float(rb[k].imag)
                db = ("0" if b >= 0 else "1") + ("0" if a >= 0 else "1")   # bit0 is Q, bit1 is I
                bits.append(db)
                llrs.extend([a, b])
                mag = 0.5 * (abs(a) + abs(b))
                syms.append(DemodSymbol(k, [mag], db, [a, b], min(1.0, mag)))
            return DemodResult("".join(bits), llrs, syms, 0,
                               self._estimate_snr_db(np.asarray(iq_samples)))

        if self.pn_q is not None:                           # dual-code QPSK
            ci = self._despread(x, off)
            cq = self._despread(x, off, pn_q)
            n = min(len(ci), len(cq))
            ci, cq = ci[:n], cq[:n]
            s = self._deslope(ci + cq, 4)
            ramp = s / (ci + cq + 1e-12)
            ci, cq = ci * ramp, cq * ramp
            phi = 0.5 * np.angle(np.sum(ci ** 2)) if n else 0.0
            ri = ci * np.exp(-1j * phi); rq = cq * np.exp(-1j * phi)
            for k in range(n):
                a, b = float(ri[k].real), float(rq[k].imag)
                db = ("0" if b >= 0 else "1") + ("0" if a >= 0 else "1")   # bit0 is Q, bit1 is I
                bits.append(db)
                llrs.extend([a, b])
                mag = 0.5 * (abs(a) + abs(b))
                syms.append(DemodSymbol(k, [mag], db, [a, b], min(1.0, mag)))
            return DemodResult("".join(bits), llrs, syms, 0,
                               self._estimate_snr_db(np.asarray(iq_samples)))

        corr = self._despread(x, off)
        if self.modem == "pi4qpsk":
            # The two grids alternate per symbol, and a burst that
            # repeats in the loop with an odd symbol period flips which demod
            # index is "odd" from copy to copy: so no single parity rotation
            # holds across the buffer (the 4th-power sums of grid A and grid B
            # cancel). Use grid-agnostic estimators instead:
            #  * CFO: consecutive pi/4 symbols differ by an odd multiple of
            #    45 deg, so (c_k conj c_k-1)^4 = -e^{4j dphi} whatever the data
            #    giving a slope range +/- Rs/8 (+/-300 Hz at 2400 Bd)
            #  * phase: the 8th power is the same on both grids: phi mod 45
            #  * slice to the nearest of the 8 points; an odd point is grid A
            #    (45+90m), an even one grid B (90m), both map m: dibit
            # The 45-deg ambiguity is the frame layer's (8 rotations).
            if len(corr) > 2:
                dz = (corr[1:] * np.conj(corr[:-1])) ** 4
                sl = float(np.angle(np.sum(dz))) - np.pi
                sl = (sl + np.pi) % (2 * np.pi) - np.pi
                corr = corr * np.exp(-1j * (sl / 4.0) * np.arange(len(corr)))
            phi8 = np.angle(np.sum(corr ** 8)) / 8.0 if len(corr) else 0.0
            r = corr * np.exp(-1j * phi8)
            for k, cv in enumerate(r):
                kk = int(np.round(np.angle(cv) / (np.pi / 4))) % 8
                m = (kk - 1) // 2 if kk % 2 else kk // 2
                db = _QPSK_INV[m]
                bits.append(db)
                mag = abs(cv)
                llrs.extend([mag, mag])
                syms.append(DemodSymbol(k, [mag], db, [mag], min(1.0, mag)))
            return DemodResult("".join(bits), llrs, syms, 0,
                               self._estimate_snr_db(np.asarray(iq_samples)))
        corr = self._deslope(corr, 4)
        # 4th-power common-phase estimate; the frame layer resolves the
        # residual 90-deg ambiguity by trying rotations via inversion+bits.
        phi = 0.25 * np.angle(np.sum(corr ** 4)) if len(corr) else 0.0
        r = corr * np.exp(-1j * (phi + np.pi / 4))   # align to quadrant edges
        for k, cv in enumerate(r):
            q = int(np.angle(cv) // (np.pi / 2)) % 4
            db = _QPSK_INV[q]
            bits.append(db)
            mag = abs(cv)
            llrs.extend([mag, mag])
            syms.append(DemodSymbol(k, [mag], db, [mag], min(1.0, mag)))
        return DemodResult("".join(bits), llrs, syms, 0,
                           self._estimate_snr_db(np.asarray(iq_samples)))


# CSS / LoRa: sf bits per symbol; symbol s (0..2^sf-1) is a base up-chirp
# cyclically shifted by s. Demod = multiply by the down-chirp then FFT; the
# peak bin is s. Processing gain ~ 10*log10(2^sf) dB.

def _upchirp(n: int, os: int) -> np.ndarray:
    # Sweeps the chirp bandwidth bw = fs/os (-bw/2..+bw/2) over n*os samples.
    # Scaling the sweep with the sample count instead would give the dechirp
    # template double the slope of an os=1-native signal at os>1, and the
    # dechirped "tone" would stay a chirp: smeared bins, garbage symbols.
    N = n * os
    k = np.arange(N)
    return np.exp(1j * 2 * np.pi * (k * k / (2.0 * N * os) - k / (2.0 * os))
                  ).astype(np.complex64)


class LoRaCSSModulator:
    def __init__(self, sf: int = 7, bw: float = 125_000.0, os: int = 1,
                 amplitude: float = 0.8, preamble_syms: int = 4):
        self.sf = int(sf)
        self.N = 1 << self.sf
        self.bw = float(bw)
        self.os = int(os)
        self.amplitude = float(amplitude)
        self.preamble_syms = int(preamble_syms)
        self.sample_rate = int(round(self.bw * self.os))
        self.symbol_rate = self.bw / self.N          # symbols/s
        self._base = _upchirp(self.N, self.os)

    def modulate(self, bit_string: str) -> ModulationResult:
        pad = (-len(bit_string)) % self.sf
        bits = bit_string + "0" * pad
        Nos = self.N * self.os
        k = np.arange(Nos)
        out = []
        # preamble: raw base upchirps (symbol 0). The demod locks on them and
        # their common decoded value is the integer-chip timing offset, which
        # it subtracts from every data symbol: RF arrival needs no other sync.
        for _ in range(self.preamble_syms):
            out.extend((self.amplitude * self._base).astype(np.complex64).tolist())
        for i in range(0, len(bits), self.sf):
            s = int(bits[i:i + self.sf], 2)
            shift = np.exp(1j * 2 * np.pi * s * k / (self.N * self.os))
            sym = self.amplitude * self._base * shift
            out.extend(sym.astype(np.complex64).tolist())
        return ModulationResult(iq_samples=out, sample_rate=self.sample_rate)


class LoRaCSSDemodulator:
    def __init__(self, sf: int = 7, bw: float = 125_000.0, os: int = 1,
                 preamble_syms: int = 4):
        self.sf = int(sf)
        self.N = 1 << self.sf
        self.bw = float(bw)
        self.os = int(os)
        self.preamble_syms = int(preamble_syms)
        self.sample_rate = int(round(self.bw * self.os))
        self.symbol_rate = self.bw / self.N
        self.samples_per_symbol = self.N * self.os
        self._down = np.conj(_upchirp(self.N, self.os))

    def _peak_metric(self, x: np.ndarray, off: int) -> float:
        Nos = self.N * self.os
        seg = x[off:]
        nsym = len(seg) // Nos
        if nsym < 1:
            return -1.0
        tot = 0.0
        for k in range(min(nsym, 8)):
            sp = np.abs(np.fft.fft(seg[k * Nos:(k + 1) * Nos] * self._down, Nos))
            tot += float(sp.max() / (sp.sum() + 1e-9))
        return tot

    def _symbols_at(self, x: np.ndarray, off: int):
        """Dechirp+FFT every symbol at sample offset `off`; (values, confs)."""
        Nos = self.N * self.os
        seg = x[off:]
        nsym = len(seg) // Nos
        vals, confs = [], []
        for k in range(nsym):
            sp = np.abs(np.fft.fft(seg[k * Nos:(k + 1) * Nos] * self._down, Nos))
            if self.os > 1:
                # a wrapped chirp's post-wrap segment dechirps to bin s - N,
                # i.e. FFT index N*(os-1)+s: fold it straight onto bin s.
                # (A reversed fold would send it to bin N-1-s, so every
                # symbol above N/2 would decode as its bit-complement.)
                sp = sp[:self.N] + sp[self.N * (self.os - 1):self.N * self.os]
            s = int(np.argmax(sp[:self.N]))
            vals.append(s)
            confs.append(float(sp[s] / (sp[:self.N].sum() + 1e-9)))
        return np.asarray(vals), np.asarray(confs)

    def demodulate(self, iq_samples: np.ndarray) -> DemodResult:
        xf = np.asarray(iq_samples, dtype=np.complex64)
        Nos = self.N * self.os
        npre = max(1, getattr(self, "preamble_syms", 4))
        if len(xf) < (npre + 1) * Nos:
            return DemodResult("", [], [], 0, 0.0)
        # Sample-accurate burst sync: matched-filter the known preamble
        # (npre base upchirps). A window offset over a stream of different
        # symbols straddles two chirps and splits the dechirped energy, so
        # symbol-metric searches cannot align data: only a preamble can.
        tmpl = np.tile(_upchirp(self.N, self.os), npre)
        n_fft = 1 << int(np.ceil(np.log2(len(xf) + len(tmpl))))
        c = np.fft.ifft(np.fft.fft(xf, n_fft) *
                        np.conj(np.fft.fft(tmpl, n_fft)))[:len(xf)]
        coarse = int(np.argmax(np.abs(c)))
        # The chirp delay/frequency ambiguity ridge (plus channel filtering)
        # can land the correlation peak a few samples off; refine on the
        # preamble itself: all npre symbols equal, sharpest peaks.
        def _score(o):
            if o < 0 or o + npre * Nos > len(xf):
                return -1.0
            v, cf = self._symbols_at(xf[o:o + npre * Nos], 0)
            if len(v) < npre or not np.all(v == v[0]):
                return -1.0
            return float(np.sum(cf))
        cands = range(max(0, coarse - 8), coarse + 9)
        start_samp = max(cands, key=_score)
        if _score(start_samp) < 0:
            start_samp = coarse
        vals, confs = self._symbols_at(xf, start_samp)
        delta = 0
        if len(vals) >= npre and np.all(vals[:npre] == vals[0]):
            delta = int(vals[0])           # residual integer-chip offset
            vals, confs = vals[npre:], confs[npre:]
        if len(vals) < 1:
            return DemodResult("", [], [], 0, 0.0)
        data = (vals - delta) % self.N
        bits, llrs, syms = [], [], []
        for k, (s, conf) in enumerate(zip(data, confs)):
            b = format(int(s), f"0{self.sf}b")
            bits.append(b)
            llrs.extend([float(conf)] * self.sf)
            syms.append(DemodSymbol(k, [float(conf)], b,
                                    [float(conf)] * self.sf, float(conf)))
        return DemodResult("".join(bits), llrs, syms, 0, 0.0)
