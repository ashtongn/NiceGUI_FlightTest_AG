"""
Continuous modem for the bitpipe: a modulator that never gaps and a
demodulator that keeps symbol timing and carrier phase across calls.

The bitpipe hands over bits; this module turns them into IQ at the radio's
rate and IQ back into soft bits, for 2-FSK, 4FSK, BPSK and QPSK, in that
order of maturity. Tone and constellation conventions follow
dpmg/modulation/modulator.py (FSK bit 1 = the higher tone, the 4FSK dibit
tables, BPSK 0 = +1, the Gray QPSK square), with one modem side addition:
BPSK and QPSK are differentially encoded, so the carrier tracker's 180 or 90
degree ambiguity never reaches the bits.

StreamModulator   bits in, IQ out; phase carried across calls (FSK),
                  reference symbol carried (PSK), odd bits held for the
                  next call
StreamDemodulator IQ at any rate in, LLRs out (llr > 0 means bit 1): NCO
                  mix (carried phase), FIR decimation (carried tail),
                  symbol timing from the transition energy spectral line
                  (carried accumulator), then per waveform detection with
                  carried thresholds, carrier loop and differential reference
IqChannel         AWGN, frequency offset, sample clock offset in ppm, delay,
                  for the offline proofs

Nothing here keys a radio; the service side wiring is modem_link.py.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np

QPSK_POINTS = {"00": complex(1.0, 1.0), "01": complex(-1.0, 1.0), "11": complex(-1.0, -1.0), "10": complex(1.0, -1.0)}
QPSK_GRID = np.array([QPSK_POINTS[k] for k in ("00", "01", "11", "10")]) / math.sqrt(2.0)
# differential QPSK: the dibit is the rotation between symbols, in the Gray
# order of the square (00 stays, 01 a quarter turn, 11 a half, 10 three
# quarters), so the absolute symbols stay on the 45 degree grid above
QPSK_STEP = {"00": complex(1.0, 0.0), "01": complex(0.0, 1.0), "11": complex(-1.0, 0.0), "10": complex(0.0, -1.0)}
QPSK_REF = complex(1.0, 1.0) / math.sqrt(2.0)
_ROT45 = complex(math.cos(-math.pi / 4), math.sin(-math.pi / 4))
_TWO_PI = 2.0 * math.pi


def dibit_table(mapping: str) -> dict:
    from dpmg.modulation.modulator import get_4fsk_dibit_table
    return get_4fsk_dibit_table(mapping)


@dataclass
class WaveformConfig:
    modulation: str = "FSK"               # FSK | 4FSK | BPSK | QPSK
    symbol_rate: float = 2400.0
    deviation_hz: float = 1200.0          # FSK tones at -dev, +dev; 4FSK at -3dev, -dev, +dev, +3dev
    tones: Optional[List[float]] = None   # explicit tone list overrides deviation_hz
    dibit_mapping: str = "binary"
    amplitude: float = 0.8
    rx_sps: int = 16                      # samples per symbol the demodulator works at

    @property
    def kind(self) -> str:
        m = self.modulation.upper()
        if m in ("FSK", "2FSK"):
            return "FSK"
        if m in ("4FSK", "FSK4"):
            return "4FSK"
        if m in ("BPSK", "QPSK"):
            return m
        raise ValueError(f"stream modem does not do {self.modulation!r} (FSK, 4FSK, BPSK, QPSK)")

    @property
    def bits_per_symbol(self) -> int:
        return 2 if self.kind in ("4FSK", "QPSK") else 1

    def tone_list(self) -> List[float]:
        if self.tones:
            return [float(t) for t in self.tones]
        d = float(self.deviation_hz)
        return [-d, d] if self.kind == "FSK" else [-3 * d, -d, d, 3 * d]

    def occupied_hz(self) -> float:
        """Rough two sided bandwidth, for the RX filter."""
        if self.kind in ("FSK", "4FSK"):
            t = self.tone_list()
            return (max(t) - min(t)) + 2.0 * self.symbol_rate
        return 2.0 * self.symbol_rate


def _bits(v) -> np.ndarray:
    return np.asarray(v, dtype=np.uint8).reshape(-1)


# TX
class StreamModulator:
    def __init__(self, wf: WaveformConfig, fs: float):
        self.wf = wf
        self.fs = float(fs)
        # samples a symbol, fractional when the rate does not divide (2 MS/s at
        # 2400 Bd is 833.33): symbol edges fall on a carried fractional sample
        # clock, so the air rate is the configured rate on any radio (rounding
        # to 833 would send 2400.96 Bd, 400 ppm off the receiver's clock)
        self.sps = self.fs / float(wf.symbol_rate)
        if self.sps < 2:
            raise ValueError(f"{fs} S/s is too slow for {wf.symbol_rate} Bd")
        self.symbol_rate = float(wf.symbol_rate)
        self._t = 0.0                                  # fractional start of the next symbol, in samples
        self.kind = wf.kind
        self.tones = wf.tone_list()
        self.table = dibit_table(wf.dibit_mapping) if self.kind == "4FSK" else None
        self.phase = 0.0                               # FSK phase, radians
        self.ref = QPSK_REF if wf.kind == "QPSK" else complex(1.0, 0.0)   # PSK differential reference
        self.held = np.zeros(0, dtype=np.uint8)        # odd bit waiting for its pair
        self.symbols_out = 0

    def _symbols(self, bits: np.ndarray) -> np.ndarray:
        """Bits in, one value per symbol out: tone index (FSK family) or step index (PSK)."""
        bps = self.wf.bits_per_symbol
        allb = np.concatenate([self.held, bits]) if len(self.held) else bits
        n = (len(allb) // bps) * bps
        self.held = allb[n:].copy()
        b = allb[:n]
        if bps == 1:
            return b.astype(np.int64)
        pairs = b.reshape(-1, 2)
        if self.kind == "4FSK":
            lut = np.zeros(4, dtype=np.int64)
            for k, v in self.table.items():
                lut[int(k, 2)] = v
            return lut[pairs[:, 0] * 2 + pairs[:, 1]]
        return pairs[:, 0] * 2 + pairs[:, 1]           # QPSK: index into "00","01","10","11" order below

    def _counts(self, k: int) -> np.ndarray:
        """Samples for each of the next k symbols, from the carried fractional clock."""
        edges = np.floor(self._t + np.arange(k + 1) * self.sps).astype(np.int64)
        end = self._t + k * self.sps
        self._t = end - edges[-1]                       # fraction carried to the next call
        return np.diff(edges)

    def modulate(self, bits) -> np.ndarray:
        """Whole symbols only; a dangling bit of a two bit waveform waits for the next call."""
        sym = self._symbols(_bits(bits))
        if len(sym) == 0:
            return np.zeros(0, dtype=np.complex64)
        self.symbols_out += len(sym)
        a = float(self.wf.amplitude)
        counts = self._counts(len(sym))
        if self.kind in ("FSK", "4FSK"):
            f = np.asarray(self.tones, dtype=np.float64)[sym]
            dphi = np.repeat(_TWO_PI * f / self.fs, counts)
            ph = self.phase + np.cumsum(dphi)
            self.phase = float(ph[-1] % _TWO_PI)
            ph = ph - dphi                             # phase at the start of each sample
            return (a * np.exp(1j * ph)).astype(np.complex64)
        if self.kind == "BPSK":
            step = np.where(sym == 1, -1.0, 1.0)
        else:
            idx = {0: "00", 1: "01", 2: "10", 3: "11"}
            step = np.array([QPSK_STEP[idx[int(s)]] for s in sym])
        seq = self.ref * np.cumprod(step)
        self.ref = complex(seq[-1])
        seq = seq / np.abs(seq)                        # keep the running product on the circle
        return (a * np.repeat(seq, counts)).astype(np.complex64)


# RX
def _lowpass(cutoff_hz: float, fs: float, ntaps: int) -> np.ndarray:
    n = np.arange(ntaps) - (ntaps - 1) / 2.0
    h = np.sinc(2.0 * cutoff_hz / fs * n) * np.hamming(ntaps)
    return (h / np.sum(h)).astype(np.float64)


def _convolve_valid(x: np.ndarray, h: np.ndarray) -> np.ndarray:
    """FFT overlap add when scipy is there (a 400 tap filter over a 2 MS/s
    frame is far too slow for the direct form), direct form otherwise."""
    try:
        from scipy.signal import oaconvolve
        return oaconvolve(x, h, mode="valid")
    except ImportError:
        return np.convolve(x, h, mode="valid")


class StreamDemodulator:
    def __init__(self, wf: WaveformConfig, fs_in: float, offset_hz: float = 0.0):
        self.wf = wf
        self.kind = wf.kind
        self.fs_in = float(fs_in)
        self.offset_hz = float(offset_hz)
        self.D = max(1, int(round(self.fs_in / (wf.symbol_rate * wf.rx_sps))))
        self.fs = self.fs_in / self.D
        self.sps = self.fs / wf.symbol_rate            # may be fractional
        if self.sps < 4:
            raise ValueError(f"only {self.sps:.1f} samples a symbol after decimation; raise rx_sps or fs")
        self.tones = wf.tone_list()
        # front end
        self.nco_phase = 0.0
        self.nco_freq = -self.offset_hz                # Hz applied by the mixer (minus the offset, minus CFO)
        self.cfo_hz = 0.0
        if self.D > 1:
            cutoff = min(0.45 * self.fs, 0.6 * wf.occupied_hz())
            self.taps = _lowpass(cutoff, self.fs_in, 8 * self.D + 1)
            self.tail = np.zeros(len(self.taps) - 1, dtype=np.complex128)
            self.dphase = 0                            # decimation phase carried
        # symbol slicer
        self.buf = np.zeros(0, dtype=np.complex128)
        self.n0 = 0                                    # absolute index of buf[0]
        self.next_pos = None                           # absolute position of the next symbol centre
        self.acc = 0j                                  # timing spectral line accumulator
        self.lam = math.exp(-1.0 / (256.0 * self.sps))
        self.prev_sample = 0j
        self.acquired = False
        # detection state
        self.thr = None                                # FSK threshold (Hz) or (centre, d) for 4FSK
        self.spread = None
        self.theta = 0.0                               # PSK carrier phase (rad)
        self.dtheta = 0.0                              # PSK carrier phase step (rad per symbol)
        self.ref = None                                # PSK differential reference symbol
        self._cfo_win: list = []                       # front end output gathered for the carrier estimate
        self._cfo_n = 0
        self.table = dibit_table(wf.dibit_mapping) if self.kind == "4FSK" else None
        self.stats = {"samples_in": 0, "symbols": 0, "calls": 0, "timing": 0.0, "cfo_hz": 0.0,
                      "eye": 0.0, "acquired": False, "adjust": 0.0}

    # front end
    def _frontend(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.complex128).reshape(-1)
        n = len(x)
        if n == 0:
            return x
        if self.nco_freq != 0.0 or self.nco_phase != 0.0:
            ph = self.nco_phase + _TWO_PI * self.nco_freq / self.fs_in * np.arange(1, n + 1)
            x = x * np.exp(1j * (ph - _TWO_PI * self.nco_freq / self.fs_in))
            self.nco_phase = float(ph[-1] % _TWO_PI)
        if self.D == 1:
            return x
        src = np.concatenate([self.tail, x])
        y = _convolve_valid(src, self.taps)
        self.tail = src[-len(self.tail):]
        out = y[self.dphase::self.D]
        self.dphase = (self.dphase - len(y)) % self.D
        return out

    # timing
    def _timing(self, e: np.ndarray, n_start: int) -> None:
        """Fold the transition energy of the new samples into the spectral line
        at 1/sps and steer next_pos to the symbol centres."""
        n = len(e)
        if n == 0:
            return
        idx = n_start + np.arange(n)
        w = self.lam ** (n - 1 - np.arange(n))
        self.acc = self.acc * (self.lam ** n) + np.sum(e * w * np.exp(-1j * _TWO_PI * idx / self.sps))
        if abs(self.acc) < 1e-9:
            return
        tau = (-np.angle(self.acc) / _TWO_PI) * self.sps      # transition position mod sps
        centre = (tau + self.sps / 2.0) % self.sps
        if self.next_pos is None:
            self.next_pos = self.n0 + centre
            while self.next_pos < self.n0 + self.sps:
                self.next_pos += self.sps
            self.acquired = True
            self.stats["acquired"] = True
            return
        err = (centre - (self.next_pos % self.sps) + self.sps / 2.0) % self.sps - self.sps / 2.0
        adj = float(np.clip(0.3 * err, -0.5, 0.5))
        self.next_pos += adj
        self.stats["adjust"] = adj
        self.stats["timing"] = float(self.next_pos % self.sps)

    def _slice(self, half_frac: float):
        """Symbol windows [centre - half, centre + half) available in buf, as
        (start indices into buf, window length). Advances next_pos."""
        w = max(2, int(round(2.0 * half_frac * self.sps)))
        half = w // 2
        starts = []
        while self.next_pos is not None:
            c = int(round(self.next_pos - self.n0))
            if c + (w - half) > len(self.buf):
                break
            if c - half < 0:
                self.next_pos += self.sps
                continue
            starts.append(c - half)
            self.next_pos += self.sps
        return np.asarray(starts, dtype=np.int64), w

    def _trim(self) -> None:
        keep = int(3 * self.sps) + 4
        if self.next_pos is not None:
            keep = max(keep, len(self.buf) - int(self.next_pos - self.n0 - self.sps))
        if len(self.buf) > keep:
            drop = len(self.buf) - keep
            self.buf = self.buf[drop:]
            self.n0 += drop

    # detection
    def _fsk(self, x: np.ndarray, n_start: int) -> np.ndarray:
        inst = np.angle(x[1:] * np.conj(np.concatenate([[self.prev_sample], x[:-1]])[1:]))
        inst = np.concatenate([[np.angle(x[0] * np.conj(self.prev_sample))], inst]) if len(x) else inst
        self.prev_sample = x[-1]
        f = inst * self.fs / _TWO_PI
        self.buf = np.concatenate([self.buf, f.astype(np.complex128)]) if len(self.buf) else f.astype(np.complex128)
        e = np.diff(np.concatenate([[self._last_f], f])) ** 2
        self._last_f = float(f[-1])
        self._timing(e, n_start)
        starts, w = self._slice(0.5)
        if len(starts) == 0:
            return np.zeros(0, dtype=np.float32)
        cs = np.concatenate([[0.0], np.cumsum(self.buf.real)])
        symf = (cs[starts + w] - cs[starts]) / w
        self.stats["symbols"] += len(symf)
        if self.kind == "FSK":
            if len(symf) >= 16:
                lo, hi = np.percentile(symf, 15), np.percentile(symf, 85)
                thr, spread = 0.5 * (lo + hi), max(1e-3, 0.5 * (hi - lo))
                self.thr = thr if self.thr is None else 0.8 * self.thr + 0.2 * thr
                self.spread = spread if self.spread is None else 0.8 * self.spread + 0.2 * spread
            if self.thr is None:
                self.thr, self.spread = 0.0, abs(self.tones[1] - self.tones[0]) / 2.0
            else:
                # the slicer sits midway between the two tones, so after the
                # offset mixer its threshold is the carrier offset
                self.stats["cfo_hz"] = float(self.thr)
            self.stats["eye"] = float(self.spread)
            return (2.0 * (symf - self.thr) / self.spread).astype(np.float32)
        # 4FSK: levels at centre + (-3, -1, 1, 3) * d
        if len(symf) >= 32:
            lo, hi = np.percentile(symf, 10), np.percentile(symf, 90)
            c = 0.5 * (lo + hi)
            d = max(1e-3, float(np.mean(np.abs(symf - c))) / 2.0)
            if self.thr is None:
                self.thr = (c, d)
            else:
                self.thr = (0.8 * self.thr[0] + 0.2 * c, 0.8 * self.thr[1] + 0.2 * d)
            self.stats["cfo_hz"] = float(self.thr[0])     # centre of the four tones
        if self.thr is None:
            self.thr = (0.0, abs(self.tones[2] - self.tones[1]) / 2.0)
        c, d = self.thr
        levels = c + np.array([-3.0, -1.0, 1.0, 3.0]) * d
        dist = (symf[:, None] - levels[None, :]) ** 2                  # (n, 4) by tone index
        inv = {v: k for k, v in self.table.items()}                    # tone index to dibit string
        llr = np.zeros((len(symf), 2), dtype=np.float64)
        scale = 1.0 / (2.0 * (0.35 * d) ** 2)
        for b in (0, 1):
            ones = np.array([inv[t][b] == "1" for t in range(4)])
            d1 = np.min(dist[:, ones], axis=1)
            d0 = np.min(dist[:, ~ones], axis=1)
            llr[:, b] = (d0 - d1) * scale
        self.stats["eye"] = float(d)
        return np.clip(llr.reshape(-1), -50, 50).astype(np.float32)

    def _psk(self, x: np.ndarray, n_start: int) -> np.ndarray:
        self.buf = np.concatenate([self.buf, x]) if len(self.buf) else x.copy()
        e = np.abs(np.diff(np.concatenate([[self.prev_sample], x]))) ** 2
        self.prev_sample = x[-1]
        self._timing(e, n_start)
        self._cfo_win.append(x)
        self._cfo_n += len(x)
        if self._cfo_n >= 128 * self.sps:
            # coarse carrier offset from the M-th power of the matched filtered
            # signal over the last 128 symbols or more (gathered across calls:
            # a decimated radio frame can be a handful of symbols): the moving
            # average over a symbol removes the pulse edge lines at multiples
            # of the symbol rate that would otherwise pass for a carrier line
            from dpmg.dsp.demod_helper import _mth_power_cfo
            M = 2 if self.kind == "BPSK" else 4
            seg = np.concatenate(self._cfo_win)
            self._cfo_win, self._cfo_n = [], 0
            w = max(2, int(round(self.sps)))
            cs = np.concatenate([[0j], np.cumsum(seg)])
            y = (cs[w:] - cs[:-w]) / w
            res = _mth_power_cfo(y, self.fs, M, 0.45 * self.wf.symbol_rate)
            if abs(res) < 0.45 * self.wf.symbol_rate:
                self.nco_freq -= 0.5 * res
                self.cfo_hz = -(self.nco_freq + self.offset_hz)
                self.stats["cfo_hz"] = self.cfo_hz
        starts, w = self._slice(0.25)
        if len(starts) == 0:
            return np.zeros(0, dtype=np.float32)
        cs = np.concatenate([[0j], np.cumsum(self.buf)])
        sym = (cs[starts + w] - cs[starts]) / w
        self.stats["symbols"] += len(sym)
        # decision directed carrier loop, one symbol at a time
        out = np.empty(len(sym), dtype=np.complex128)
        th, dth = self.theta, self.dtheta
        alpha, beta = 0.08, 0.004
        bpsk = self.kind == "BPSK"
        for i, s in enumerate(sym):
            r = s * np.exp(-1j * th)
            if bpsk:
                dec = 1.0 if r.real >= 0 else -1.0
            else:
                dec = QPSK_GRID[int(np.argmin(np.abs(QPSK_GRID - r / (abs(r) + 1e-12))))]
            err = float(np.angle(r * np.conj(dec)))
            dth += beta * err
            th += alpha * err + dth
            out[i] = r
        self.theta, self.dtheta = th % _TWO_PI, float(np.clip(dth, -0.3, 0.3))
        # what the loop still tracks after the coarse estimate, in Hz
        self.stats["cfo_hz"] = self.cfo_hz + self.dtheta * self.wf.symbol_rate / _TWO_PI
        ref = out[0] if self.ref is None else self.ref
        z = out * np.conj(np.concatenate([[ref], out[:-1]]))
        self.ref = out[-1]
        mag = float(np.mean(np.abs(z))) + 1e-12
        if bpsk:
            self.stats["eye"] = float(np.mean(np.abs(z.real))) / mag
            llr = -z.real / mag * 4.0
        else:
            z = z * _ROT45                     # rotations 1, j, -1, -j land in the four quadrants
            self.stats["eye"] = float(np.mean(np.minimum(np.abs(z.real), np.abs(z.imag)))) / mag * math.sqrt(2.0)
            llr = np.stack([-z.real, z.imag], axis=1).reshape(-1) / mag * 4.0
        return np.clip(llr, -50, 50).astype(np.float32)

    # entry
    def demodulate(self, iq) -> np.ndarray:
        """IQ at fs_in in, LLRs out (llr > 0 means bit 1), as many whole symbols as fit."""
        x = self._frontend(iq)
        self.stats["calls"] += 1
        self.stats["samples_in"] += len(x)
        if len(x) == 0:
            return np.zeros(0, dtype=np.float32)
        n_start = self.n0 + len(self.buf)
        if not hasattr(self, "_last_f"):
            self._last_f = 0.0
        out = self._fsk(x, n_start) if self.kind in ("FSK", "4FSK") else self._psk(x, n_start)
        self._trim()
        return out

    def status(self) -> dict:
        d = dict(self.stats)
        d.update({"fs": self.fs, "sps": round(self.sps, 3), "decimation": self.D, "kind": self.kind})
        return d


# channel
@dataclass
class IqChannel:
    """Offline channel: gain, AWGN at an SNR over the signal power, carrier
    offset, sample clock offset (ppm, by linear interpolation) and a delay."""
    fs: float
    snr_db: Optional[float] = None
    cfo_hz: float = 0.0
    ppm: float = 0.0
    delay_samples: int = 0
    seed: int = 1
    rng: np.random.Generator = field(init=False)
    phase: float = field(init=False, default=0.0)
    frac: float = field(init=False, default=0.0)
    hold: np.ndarray = field(init=False, default=None)

    def __post_init__(self):
        self.rng = np.random.default_rng(self.seed)
        self.hold = np.zeros(self.delay_samples, dtype=np.complex128)
        self._carry = np.zeros(0, dtype=np.complex128)

    def apply(self, x) -> np.ndarray:
        x = np.asarray(x, dtype=np.complex128).reshape(-1)
        if self.delay_samples:
            allx = np.concatenate([self.hold, x])
            x, self.hold = allx[:len(x)], allx[len(x):]
        if self.ppm:
            src = np.concatenate([self._carry, x])
            step = 1.0 + self.ppm * 1e-6
            pos = self.frac + np.arange(0, len(src) - 1, step)
            pos = pos[pos <= len(src) - 1 - 1e-9]
            i = np.floor(pos).astype(int)
            fr = pos - i
            y = src[i] * (1 - fr) + src[np.minimum(i + 1, len(src) - 1)] * fr
            last = pos[-1] + step if len(pos) else self.frac
            keep_from = int(math.floor(last))
            self._carry = src[keep_from:]
            self.frac = last - keep_from
            x = y
        if self.cfo_hz:
            ph = self.phase + _TWO_PI * self.cfo_hz / self.fs * np.arange(1, len(x) + 1)
            x = x * np.exp(1j * (ph - _TWO_PI * self.cfo_hz / self.fs))
            self.phase = float(ph[-1] % _TWO_PI) if len(ph) else self.phase
        if self.snr_db is not None and len(x):
            p = float(np.mean(np.abs(x) ** 2)) or 1e-12
            npow = p / (10.0 ** (self.snr_db / 10.0))
            x = x + (self.rng.standard_normal(len(x)) + 1j * self.rng.standard_normal(len(x))) * math.sqrt(npow / 2.0)
        return x.astype(np.complex64)
