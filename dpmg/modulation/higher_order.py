#!/usr/bin/env python3
"""Higher-order digital modulation for the DPMG SDR modem.

Gray-coded constellation mappers (8PSK, 8QAM, 16QAM, 16APSK, 32APSK) with
optional root-raised-cosine pulse shaping, minimum-distance hard demod, and
max-log soft LLRs. Self-contained (numpy only, no scipy). Reuses the
ModulationResult(iq_samples, sample_rate) container from modulator.py.

Convention notes:
- Every constellation is scaled to unit average symbol energy, so a caller can
  swap modulations without re-scaling gain or noise references.
- LLR sign follows the formula llr = (min_{b=0}|y-s|^2 -
  min_{b=1}|y-s|^2)/N0, hence llr > 0 favours bit 1 (matches the hard decision).
"""

from __future__ import annotations

import math
from typing import Dict, List, Tuple

import numpy as np

from dpmg.modulation.modulator import ModulationResult


def _gray(i: int) -> int:
    # Reflected binary Gray code: adjacent integers differ by exactly one bit.
    return i ^ (i >> 1)


def rrc_taps(rolloff: float, sps: int, span_syms: int) -> np.ndarray:
    """Root-raised-cosine FIR, symmetric and unit-energy.

    Closed-form (no scipy). Length is span_syms*sps + 1 (odd, so the peak sits
    on a tap). Because the taps are symmetric and unit-energy, the TX(+RX)
    cascade RRC*RRC is a raised cosine with unit peak gain and zero ISI at the
    symbol instants: the demod then needs no separate gain estimate.
    """
    beta = float(rolloff)
    sps = int(sps)
    span = int(span_syms)
    if sps <= 0 or span <= 0:
        raise ValueError("sps and span_syms must be positive")
    n_half = (span * sps) // 2
    n = np.arange(-n_half, n_half + 1)
    t = n / float(sps)  # time in symbol periods (Ts = 1)
    h = np.zeros_like(t, dtype=np.float64)
    for k, ti in enumerate(t):
        if abs(ti) < 1e-12:  # t = 0 limit
            h[k] = 1.0 - beta + 4.0 * beta / math.pi
        elif beta > 0 and abs(abs(ti) - 1.0 / (4.0 * beta)) < 1e-9:  # 0/0 limit at t = Ts/(4*beta)
            h[k] = (beta / math.sqrt(2.0)) * (
                (1.0 + 2.0 / math.pi) * math.sin(math.pi / (4.0 * beta))
                + (1.0 - 2.0 / math.pi) * math.cos(math.pi / (4.0 * beta))
            )
        else:
            num = math.sin(math.pi * ti * (1.0 - beta)) + 4.0 * beta * ti * math.cos(math.pi * ti * (1.0 + beta))
            den = math.pi * ti * (1.0 - (4.0 * beta * ti) ** 2)
            h[k] = num / den
    return h / math.sqrt(np.sum(h ** 2))  # unit energy gives unit cascade peak gain


class HigherOrderModulator:
    """Base for Gray-coded higher-order constellations.

    Subclasses provide `_build()` returning (symbols, labels, bits_per_symbol) where
    `symbols` is the unit-average-power complex table and `labels[i]` is the
    Gray bit pattern for symbols[i]. All map/demod/LLR machinery is generic and
    driven purely by that table, so it is agnostic to ring geometry.
    """

    name = "BASE"

    def __init__(
        self,
        symbol_rate: float,
        samples_per_symbol: int,
        amplitude: float = 1.0,
        use_rrc: bool = False,
        rolloff: float = 0.35,
        rrc_span: int = 10,
    ):
        if symbol_rate <= 0:
            raise ValueError("symbol_rate must be positive")
        if samples_per_symbol <= 0:
            raise ValueError("samples_per_symbol must be positive")
        if amplitude <= 0:
            raise ValueError("amplitude must be positive")

        self.symbol_rate = float(symbol_rate)
        self.sps = int(samples_per_symbol)
        self.amplitude = float(amplitude)
        self.use_rrc = bool(use_rrc)
        self.rolloff = float(rolloff)
        self.rrc_span = int(rrc_span)
        self.sample_rate = int(round(self.symbol_rate * self.sps))

        self.symbols, self.labels, self.bits_per_symbol = self._build()
        self.symbols = np.asarray(self.symbols, dtype=complex)
        # Unit average symbol energy: normalize here so subclasses only place points.
        self.symbols = self.symbols / math.sqrt(float(np.mean(np.abs(self.symbols) ** 2)))

        if len(self.labels) != len(self.symbols):
            raise ValueError("labels/symbols length mismatch")
        self.bits_to_index: Dict[str, int] = {lab: i for i, lab in enumerate(self.labels)}
        if len(self.bits_to_index) != len(self.labels):
            raise ValueError("Gray labels are not a bijection")

        # Per-bit symbol partitions for the LLR min-search.
        b = self.bits_per_symbol
        M = len(self.symbols)
        self._bit0 = [np.array([i for i in range(M) if self.labels[i][k] == "0"]) for k in range(b)]
        self._bit1 = [np.array([i for i in range(M) if self.labels[i][k] == "1"]) for k in range(b)]

        self._h = rrc_taps(self.rolloff, self.sps, self.rrc_span) if self.use_rrc else None

    def _build(self) -> Tuple[np.ndarray, List[str], int]:
        raise NotImplementedError

    # TX
    def modulate(self, bit_string: str) -> ModulationResult:
        b = self.bits_per_symbol
        rem = len(bit_string) % b
        if rem:
            bit_string = bit_string + "0" * (b - rem)  # zero-pad final partial symbol

        idx = [self.bits_to_index[bit_string[i : i + b]] for i in range(0, len(bit_string), b)]
        syms = self.symbols[idx] * self.amplitude

        if self.use_rrc:
            x = np.zeros(len(syms) * self.sps, dtype=complex)
            x[:: self.sps] = syms  # impulse train, first symbol at index 0
            iq = np.convolve(x, self._h, "full")
        else:
            iq = np.repeat(syms, self.sps)  # rectangular hold

        return ModulationResult(iq_samples=iq.tolist(), sample_rate=self.sample_rate)

    # RX front-end
    def _symbol_estimates(self, iq) -> np.ndarray:
        # Matched-filter (RRC) or rectangular-average down to one sample/symbol,
        # then de-scale by amplitude so estimates live on the unit-power table.
        y = np.asarray(iq, dtype=complex)
        if self.use_rrc:
            L = len(self._h)
            z = np.convolve(y, self._h, "full")
            nsym = int(round((len(y) - (L - 1)) / self.sps))
            nsym = max(nsym, 0)
            off = L - 1  # cascade delay: (L-1)/2 from each RRC pass
            est = z[off : off + nsym * self.sps : self.sps][:nsym]
        else:
            nsym = len(y) // self.sps
            est = y[: nsym * self.sps].reshape(nsym, self.sps).mean(axis=1)
        return est / self.amplitude

    def demodulate(self, iq) -> str:
        est = self._symbol_estimates(iq)
        if len(est) == 0:
            return ""
        # Nearest constellation point (minimum Euclidean distance).
        d = np.abs(est[:, None] - self.symbols[None, :])
        nearest = np.argmin(d, axis=1)
        return "".join(self.labels[i] for i in nearest)

    def soft_llr(self, iq, n0: float = 1.0) -> np.ndarray:
        # Max-log LLR per bit: llr = (min_{b=0}|y-s|^2 - min_{b=1}|y-s|^2)/N0.
        est = self._symbol_estimates(iq)
        b = self.bits_per_symbol
        out = np.empty(len(est) * b, dtype=np.float64)
        if len(est) == 0:
            return out
        d2 = np.abs(est[:, None] - self.symbols[None, :]) ** 2  # nsym x M
        for s in range(len(est)):
            row = d2[s]
            base = s * b
            for k in range(b):
                out[base + k] = (row[self._bit0[k]].min() - row[self._bit1[k]].min()) / n0
        return out


class PSK8(HigherOrderModulator):
    """8PSK: 8 equal-power phases, Gray-coded around the circle."""

    name = "8PSK"

    def _build(self):
        M = 8
        syms = np.array([np.exp(1j * 2.0 * math.pi * i / M) for i in range(M)])
        labels = [format(_gray(i), "03b") for i in range(M)]  # phase step i: Gray label
        return syms, labels, 3


class QAM8(HigherOrderModulator):
    """8QAM quasi-circular (1+sqrt3) two-ring set.

    Four inner corners at radius sqrt2 and four axis points at radius (1+sqrt3);
    every nearest-neighbour distance equals 2. Points are listed in angular
    order and carry a cyclic Gray code (wrap-around 100 to 000 differs by 1 bit).
    """

    name = "8QAM"

    def _build(self):
        r = 1.0 + math.sqrt(3.0)
        pts = [(r, 0.0), (1.0, 1.0), (0.0, r), (-1.0, 1.0), (-r, 0.0), (-1.0, -1.0), (0.0, -r), (1.0, -1.0)]
        syms = np.array([complex(x, y) for x, y in pts])
        labels = ["000", "001", "011", "010", "110", "111", "101", "100"]  # cyclic Gray
        return syms, labels, 3


class QAM16(HigherOrderModulator):
    """Square 16QAM: 4x4 grid, independent Gray coding on I and Q."""

    name = "16QAM"

    def _build(self):
        levels = [-3.0, -1.0, 1.0, 3.0]
        glab = ["00", "01", "11", "10"]  # Gray labels for the 4 amplitude levels
        syms: List[complex] = []
        labels: List[str] = []
        for a in range(4):
            for c in range(4):
                syms.append(complex(levels[a], levels[c]))
                labels.append(glab[a] + glab[c])  # high dibit = I, low dibit = Q
        return np.array(syms), labels, 4


class APSK16(HigherOrderModulator):
    """16APSK: DVB-S2-style 4+12 rings, outer/inner radius ratio ~2.85."""

    name = "16APSK"

    def _build(self):
        r1, r2 = 1.0, 2.85
        inner = [r1 * np.exp(1j * (math.pi / 4.0 + k * math.pi / 2.0)) for k in range(4)]
        outer = [r2 * np.exp(1j * (math.pi / 12.0 + k * math.pi / 6.0)) for k in range(12)]
        syms = np.array(inner + outer)
        labels = [format(_gray(i), "04b") for i in range(16)]  # Gray-ordered index labels
        return syms, labels, 4


class APSK32(HigherOrderModulator):
    """32APSK: DVB-S2-style 4+12+16 rings, radius ratios ~2.84 and ~5.27."""

    name = "32APSK"

    def _build(self):
        r1, r2, r3 = 1.0, 2.84, 5.27
        inner = [r1 * np.exp(1j * (math.pi / 4.0 + k * math.pi / 2.0)) for k in range(4)]
        mid = [r2 * np.exp(1j * (math.pi / 12.0 + k * math.pi / 6.0)) for k in range(12)]
        outer = [r3 * np.exp(1j * (math.pi / 16.0 + k * math.pi / 8.0)) for k in range(16)]
        syms = np.array(inner + mid + outer)
        labels = [format(_gray(i), "05b") for i in range(32)]  # Gray-ordered index labels
        return syms, labels, 5


_REGISTRY = {
    "8PSK": PSK8,
    "8QAM": QAM8,
    "16QAM": QAM16,
    "16APSK": APSK16,
    "32APSK": APSK32,
}


def get_higher_order_modulator(
    name: str,
    symbol_rate: float,
    samples_per_symbol: int,
    **kw,
) -> HigherOrderModulator:
    """Factory: name in {8PSK, 8QAM, 16QAM, 16APSK, 32APSK}.

    kw forwards to the constructor: amplitude, use_rrc, rolloff, rrc_span.
    """
    key = (name or "").strip().upper()
    if key not in _REGISTRY:
        raise ValueError(f"Unknown higher-order modulation: {name}")
    return _REGISTRY[key](symbol_rate, samples_per_symbol, **kw)
