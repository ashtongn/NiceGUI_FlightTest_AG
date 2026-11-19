#!/usr/bin/env python3
"""Soft-decision demodulation framework for FSK, 4FSK, QPSK, GFSK.

Produces bit-level log-likelihood ratios (LLRs) for integration with ECC decoders.
"""

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, List, Tuple

import numpy as np

def _estimate_sps(x, sps_nom, search=0.05):
    """Non-data-aided symbol-rate estimate from the spectral line at the symbol rate.

    x        : real 'transition energy' signal (|diff(inst_freq)| for FSK).
    sps_nom  : nominal samples/symbol (starting guess).
    search   : fractional search window around the nominal rate.

    The composer resamples each emitter to the composite rate, so the true transmitted
    symbol rate is not exactly nominal; TX/RX clock offset and Doppler add more. Measuring
    it from the signal removes the residual drift that no nominal-rate calc can see.
    """
    n = len(x)
    if n < sps_nom * 8 or sps_nom <= 0:
        return sps_nom
    xz = x - np.mean(x)
    X = np.fft.rfft(xz * np.hanning(n))
    f = np.fft.rfftfreq(n, 1.0)              # cycles/sample
    ftar = 1.0 / sps_nom
    band = (f >= ftar * (1 - search)) & (f <= ftar * (1 + search))
    if not band.any():
        return sps_nom
    idx = np.where(band)[0]
    k = int(idx[np.argmax(np.abs(X[idx]))])
    mag = np.abs(X)
    # parabolic interpolation: sub-bin rate accuracy
    if 0 < k < len(mag) - 1:
        a, b, c = mag[k-1], mag[k], mag[k+1]
        denom = (a - 2*b + c)
        d = 0.5 * (a - c) / denom if abs(denom) > 1e-12 else 0.0
        d = max(-0.5, min(0.5, d))
    else:
        d = 0.0
    f_sym = f[k] + d * (f[1] - f[0])
    if f_sym <= 0:
        return sps_nom
    sps_est = 1.0 / f_sym
    # reject wild estimates
    if abs(sps_est - sps_nom) / sps_nom > search:
        return sps_nom
    return float(sps_est)

def get_4fsk_dibit_table(dibit_mapping: str) -> Dict[str, int]:
    """Return dibit to tone-index table for 4FSK demodulation."""
    key = (dibit_mapping or "binary").strip().lower()
    if key == "binary":
        return {"00": 0, "01": 1, "10": 2, "11": 3}
    if key == "gray":
        return {"00": 0, "01": 1, "11": 2, "10": 3}
    if key == "dmr":
        return {"11": 0, "10": 1, "00": 2, "01": 3}
    if key in ("dmr_inverted", "dmr-inverted"):
        return {"01": 0, "00": 1, "10": 2, "11": 3}
    raise ValueError(f"Unknown 4FSK dibit mapping: {dibit_mapping}")


@dataclass
class DemodSymbol:
    """Per-symbol demodulation output."""

    symbol_index: int
    tones_energy: List[float]  # Energy at each tone
    hard_bits: str  # Demodulated bits (e.g., "01" for 4FSK)
    soft_bits_llr: List[float]  # Log-likelihood ratios (one per bit)
    confidence: float  # Overall symbol confidence (0.0-1.0)


@dataclass
class DemodResult:
    """Complete demodulation result."""

    bit_string: str  # Hard demodulated bits
    soft_bits_llr: List[float]  # All LLRs (one per bit)
    symbols: List[DemodSymbol]  # Per-symbol details
    symbol_timing_offset: int  # Detected symbol start offset (samples)
    snr_estimate_db: float  # Estimated SNR (dB)


class SoftDemodulator(ABC):
    """Base class for soft-decision demodulators."""

    def __init__(self, sample_rate: float, symbol_rate: float, samples_per_bit: int):
        """Initialize demodulator.

        Args:
            sample_rate: ADC sample rate (Hz)
            symbol_rate: Symbol rate (Hz, baud)
            samples_per_bit: Samples per bit
        """
        self.sample_rate = float(sample_rate)
        self.symbol_rate = float(symbol_rate)
        self.samples_per_bit = int(samples_per_bit)
        self.samples_per_symbol = samples_per_bit  # Override in 4FSK

    @abstractmethod
    def demodulate(self, iq_samples: np.ndarray) -> DemodResult:
        """Demodulate IQ samples to soft bits.

        Args:
            iq_samples: Complex IQ array (shape: (N,))

        Returns:
            DemodResult with hard/soft bits, per-symbol details, timing offset, SNR estimate
        """
        pass

    def _estimate_snr_db(self, iq_samples: np.ndarray) -> float:
        """Estimate SNR from IQ signal power vs noise floor (FFT-based).

        Args:
            iq_samples: Complex IQ samples

        Returns:
            SNR estimate in dB
        """
        fft_vals = np.abs(np.fft.fft(iq_samples)) ** 2
        fft_db = 10.0 * np.log10(np.maximum(fft_vals, 1e-12))

        # Signal power: sum of peaks
        signal_bins = np.argsort(fft_db)[-3:]  # Top 3 bins
        signal_power = np.mean(fft_db[signal_bins])

        # Noise floor: median of all bins (rough estimate)
        noise_floor = np.median(fft_db)

        snr_db = float(signal_power - noise_floor)
        return max(-10.0, snr_db)  # Floor at -10 dB


class FSKDemodulator(SoftDemodulator):
    """2-tone FSK soft-decision demodulator."""

    def __init__(
        self,
        sample_rate: float,
        symbol_rate: float,
        samples_per_bit: int,
        tone0: float,
        tone1: float,
    ):
        """Initialize FSK demodulator.

        Args:
            sample_rate: ADC sample rate (Hz)
            symbol_rate: Symbol rate (Hz)
            samples_per_bit: Samples per bit
            tone0: Tone frequency for bit 0 (Hz)
            tone1: Tone frequency for bit 1 (Hz)
        """
        super().__init__(sample_rate, symbol_rate, samples_per_bit)
        self.tone0 = float(tone0)
        self.tone1 = float(tone1)
        self.samples_per_symbol = samples_per_bit  # 1 bit per symbol

    def demodulate(self, iq_samples: np.ndarray) -> DemodResult:
        """Demodulate FSK IQ samples to bit stream with soft bits.

        Data-driven decision: the transmitted tones are rarely exactly at the
        authored tone0/tone1 (residual CFO, offset-correction error, hardware).
        Correlating against fixed tone phasors then misses the energy. Instead this
        measures each symbol's mean instantaneous frequency and splits against the
        data-derived median (the midpoint between the two tone clusters), which is
        robust to any common frequency offset.

        Args:
            iq_samples: Complex IQ array (shape: (N,))

        Returns:
            DemodResult
        """
        iq_samples = np.asarray(iq_samples, dtype=np.complex64)
        if len(iq_samples) == 0:
            raise ValueError("IQ samples array is empty.")

        symbols: List[DemodSymbol] = []
        hard_bits_list: List[str] = []
        soft_bits_llr_list: List[float] = []

        # Per-sample instantaneous frequency (rad/sample).
        if len(iq_samples) >= 2:
            inst = np.angle(iq_samples[1:] * np.conj(iq_samples[:-1]))
        else:
            inst = np.zeros(0)

        sps_f = float(self.samples_per_symbol)
        num_symbols = int(len(inst) / sps_f) if sps_f > 0 else 0
        # Fractional-endpoint window average, vectorized. A per-symbol Python loop
        # costs 249 ms/channel/block on the G40; at 5 channels x 3 blocks/s that holds
        # the GIL 3.7 s per second of wall time, so _rx_loop never gets scheduled, the
        # card's 271 ms RX FIFO (8192 x 1018 samples) overflows, ~61% of the RF
        # timeline is lost and FrameAccumulator splices across the holes.
        # cumsum + interpolated edges is the same integral (verified to 4e-14): for a
        # piecewise-constant signal, integral(t0..t1) = interp(cumsum)@t1 - interp(cumsum)@t0,
        # and the window width is exactly t1-t0 = sps_f, equal to sum(w).
        num_symbols = int(len(inst) / sps_f) if sps_f > 0 else 0
        if num_symbols > 0 and int(np.ceil(num_symbols * sps_f)) > len(inst):
            num_symbols -= 1
        if num_symbols > 0:
            _cs = np.concatenate(([0.0], np.cumsum(inst)))
            _e = np.arange(num_symbols + 1) * sps_f
            _v = np.interp(_e, np.arange(len(_cs)), _cs)
            symfreq = np.diff(_v) / sps_f
        else:
            symfreq = np.zeros(0)

        # Decision threshold = midpoint of the two tone clusters; robust to bit imbalance.
        if symfreq.size:
            lo = float(np.percentile(symfreq, 15))
            hi = float(np.percentile(symfreq, 85))
            thr = 0.5 * (lo + hi)
        else:
            thr = 0.0
        spread = float(np.std(symfreq)) + 1e-9  # LLR scale

        # Vectorized. A per-symbol loop builds 682 DemodSymbol objects + 682 string appends +
        # 682 float appends per call. demod_with_recovery calls demodulate() up to 15x per
        # frame (early-exit ~7.5 avg) and discards all but one, so that is ~41,000 object
        # builds per drain pass, ~9.5 ms/channel, which is the decode budget. Nothing consumes
        # DemodResult.symbols except `last_n_syms = len(...)`, so build a length-only stub.
        hard = symfreq > thr
        bit_string = bytes(hard.astype(np.uint8) + 48).decode('ascii')
        llr = (symfreq - thr) / spread
        soft_bits_llr_list = llr.tolist()
        symbols = [None] * num_symbols          # len() is the only thing ever read
        snr_db = self._estimate_snr_db(iq_samples)

        return DemodResult(
            bit_string=bit_string,
            soft_bits_llr=soft_bits_llr_list,
            symbols=symbols,
            symbol_timing_offset=0,
            snr_estimate_db=snr_db,
        )


class FSK4Demodulator(SoftDemodulator):
    """4-tone FSK (4FSK) soft-decision demodulator."""

    def __init__(
        self,
        sample_rate: float,
        symbol_rate: float,
        samples_per_bit: int,
        tone0: float,
        tone1: float,
        tone2: float,
        tone3: float,
        dibit_mapping: str = "binary",
    ):
        """Initialize 4FSK demodulator.

        Args:
            sample_rate: ADC sample rate (Hz)
            symbol_rate: Symbol rate (Hz)
            samples_per_bit: Samples per bit (each symbol is 2 bits)
            tone0-3: Tone frequencies (Hz)
        """
        super().__init__(sample_rate, symbol_rate, samples_per_bit)
        self.tones = [float(tone0), float(tone1), float(tone2), float(tone3)]
        self.samples_per_symbol = samples_per_bit  # Each symbol carries 2 bits but occupies samples_per_bit time samples
        self.dibit_mapping = dibit_mapping
        self.dibit_to_tone = get_4fsk_dibit_table(dibit_mapping)
        self.tone_to_dibit = {tone_idx: dibit for dibit, tone_idx in self.dibit_to_tone.items()}

    def demodulate(self, iq_samples: np.ndarray) -> DemodResult:
        """Demodulate 4FSK IQ samples to bit stream with soft bits.

        Algorithm:
        1. For each symbol (2 bits), correlate with 4 tone phasors
        2. Find nearest tone index: dibit = argmax(energies)
        3. Map dibit to 2 hard bits (00/01/10/11)
        4. Compute 2 LLRs: L_bit0 = f(E00,E01,E10,E11), L_bit1 = g(...)
        5. Return hard bits + LLRs

        Args:
            iq_samples: Complex IQ array (shape: (N,))

        Returns:
            DemodResult
        """
        iq_samples = np.asarray(iq_samples, dtype=np.complex128)
        if len(iq_samples) == 0:
            raise ValueError("IQ samples array is empty.")

        # Demodulate symbols
        symbols: List[DemodSymbol] = []
        hard_bits_list: List[str] = []
        soft_bits_llr_list: List[float] = []

        sps_f = float(self.samples_per_symbol)
        num_symbols = int(len(iq_samples) / sps_f) if sps_f > 0 else 0

        for sym_idx in range(num_symbols):
            t0 = sym_idx * sps_f; t1 = (sym_idx + 1) * sps_f
            a = int(np.floor(t0)); b = int(np.ceil(t1))
            if b > len(iq_samples):
                break
            w = np.ones(b - a)
            if len(w) >= 2:
                w[0] = 1.0 - (t0 - a)
                w[-1] = 1.0 - (b - t1)
            symbol_samples = iq_samples[a:b] * w    # fractional-endpoint weighting
            if len(symbol_samples) < 2:
                continue

            # Correlate with all 4 tone phasors
            n = np.arange(len(symbol_samples))
            energies = []
            for i in range(4):
                phasor = np.exp(-2j * np.pi * self.tones[i] * n / self.sample_rate)
                corr = np.sum(symbol_samples * phasor)
                energy = float(np.abs(corr) ** 2)
                energies.append(energy)

            # Hard decision: nearest tone
            best_idx = int(np.argmax(energies))
            hard_bits = self.tone_to_dibit[best_idx]
            hard_bits_list.append(hard_bits)

            # Soft bits (2 LLRs for 2 bits)
            tones_bit0_1 = [tone_idx for tone_idx, dibit in self.tone_to_dibit.items() if dibit[0] == "1"]
            tones_bit0_0 = [tone_idx for tone_idx, dibit in self.tone_to_dibit.items() if dibit[0] == "0"]
            tones_bit1_1 = [tone_idx for tone_idx, dibit in self.tone_to_dibit.items() if dibit[1] == "1"]
            tones_bit1_0 = [tone_idx for tone_idx, dibit in self.tone_to_dibit.items() if dibit[1] == "0"]

            e_bit0_1 = sum(energies[i] for i in tones_bit0_1)
            e_bit0_0 = sum(energies[i] for i in tones_bit0_0)
            denom0 = e_bit0_0 + e_bit0_1 + 1e-12
            llr_bit0 = (e_bit0_1 - e_bit0_0) / denom0

            e_bit1_1 = sum(energies[i] for i in tones_bit1_1)
            e_bit1_0 = sum(energies[i] for i in tones_bit1_0)
            denom1 = e_bit1_0 + e_bit1_1 + 1e-12
            llr_bit1 = (e_bit1_1 - e_bit1_0) / denom1

            soft_bits_llr_list.extend([float(llr_bit0), float(llr_bit1)])

            # Confidence
            sum_energy = sum(energies)
            confidence = float(max(energies) / (sum_energy + 1e-12))

            symbols.append(
                DemodSymbol(
                    symbol_index=sym_idx,
                    tones_energy=energies,
                    hard_bits=hard_bits,
                    soft_bits_llr=[float(llr_bit0), float(llr_bit1)],
                    confidence=confidence,
                )
            )

        bit_string = "".join(hard_bits_list)
        snr_db = self._estimate_snr_db(iq_samples)

        return DemodResult(
            bit_string=bit_string,
            soft_bits_llr=soft_bits_llr_list,
            symbols=symbols,
            symbol_timing_offset=0,
            snr_estimate_db=snr_db,
        )


class QPSKDemodulator(SoftDemodulator):
    """QPSK soft-decision demodulator (Gray-coded)."""

    def __init__(self, sample_rate: float, symbol_rate: float, samples_per_bit: int):
        super().__init__(sample_rate, symbol_rate, samples_per_bit)
        self.samples_per_symbol = 2 * samples_per_bit  # 2 bits per symbol

    def demodulate(self, iq_samples: np.ndarray) -> DemodResult:
        """Demodulate Gray-coded QPSK symbols.

        Constellation mapping used:
        00: (+I, +Q)
        01: (-I, +Q)
        11: (-I, -Q)
        10: (+I, -Q)
        """
        x = np.asarray(iq_samples, dtype=np.complex128)
        if x.size == 0:
            raise ValueError("IQ samples array is empty.")

        num_symbols = x.size // self.samples_per_symbol
        symbols: List[DemodSymbol] = []
        hard_bits_list: List[str] = []
        llrs: List[float] = []

        for sym_idx in range(num_symbols):
            start = sym_idx * self.samples_per_symbol
            end = start + self.samples_per_symbol
            s = np.mean(x[start:end])
            i_val = float(np.real(s))
            q_val = float(np.imag(s))

            if i_val >= 0.0 and q_val >= 0.0:
                hard_bits = "00"
            elif i_val < 0.0 and q_val >= 0.0:
                hard_bits = "01"
            elif i_val < 0.0 and q_val < 0.0:
                hard_bits = "11"
            else:
                hard_bits = "10"

            # Bit 0 is tied to Q sign; bit 1 is tied to I sign for this mapping.
            denom = abs(i_val) + abs(q_val) + 1e-12
            llr_b0 = (-q_val) / denom
            llr_b1 = (-i_val) / denom

            hard_bits_list.append(hard_bits)
            llrs.extend([llr_b0, llr_b1])

            conf = float(min(1.0, np.abs(s) / (np.mean(np.abs(x[start:end])) + 1e-12)))
            symbols.append(
                DemodSymbol(
                    symbol_index=sym_idx,
                    tones_energy=[i_val * i_val + q_val * q_val],
                    hard_bits=hard_bits,
                    soft_bits_llr=[llr_b0, llr_b1],
                    confidence=conf,
                )
            )

        return DemodResult(
            bit_string="".join(hard_bits_list),
            soft_bits_llr=llrs,
            symbols=symbols,
            symbol_timing_offset=0,
            snr_estimate_db=self._estimate_snr_db(x),
        )


class BPSKDemodulator(SoftDemodulator):
    """BPSK soft-decision demodulator."""

    def __init__(self, sample_rate: float, symbol_rate: float, samples_per_bit: int):
        super().__init__(sample_rate, symbol_rate, samples_per_bit)
        self.samples_per_symbol = samples_per_bit

    def demodulate(self, iq_samples: np.ndarray) -> DemodResult:
        x = np.asarray(iq_samples, dtype=np.complex128)
        if x.size == 0:
            raise ValueError("IQ samples array is empty.")

        num_symbols = x.size // self.samples_per_symbol
        symbols: List[DemodSymbol] = []
        hard_bits_list: List[str] = []
        llrs: List[float] = []

        for sym_idx in range(num_symbols):
            start = sym_idx * self.samples_per_symbol
            end = start + self.samples_per_symbol
            s = np.mean(x[start:end])
            i_val = float(np.real(s))
            q_val = float(np.imag(s))

            hard_bit = "0" if i_val >= 0.0 else "1"
            denom = abs(i_val) + abs(q_val) + 1e-12
            llr = (-i_val) / denom

            hard_bits_list.append(hard_bit)
            llrs.append(llr)
            symbols.append(
                DemodSymbol(
                    symbol_index=sym_idx,
                    tones_energy=[i_val * i_val + q_val * q_val],
                    hard_bits=hard_bit,
                    soft_bits_llr=[llr],
                    confidence=float(min(1.0, abs(i_val) / (abs(i_val) + abs(q_val) + 1e-12))),
                )
            )

        return DemodResult(
            bit_string="".join(hard_bits_list),
            soft_bits_llr=llrs,
            symbols=symbols,
            symbol_timing_offset=0,
            snr_estimate_db=self._estimate_snr_db(x),
        )


class PiQuarterQPSKDemodulator(SoftDemodulator):
    """pi/4-QPSK demodulator for alternating-grid Gray mapping."""

    _GRAY_BITS = ("00", "01", "11", "10")

    def __init__(self, sample_rate: float, symbol_rate: float, samples_per_bit: int):
        super().__init__(sample_rate, symbol_rate, samples_per_bit)
        self.samples_per_symbol = 2 * samples_per_bit

        self._grid_a = [
            complex(math.cos(math.pi * (1 + 2 * i) / 4), math.sin(math.pi * (1 + 2 * i) / 4))
            for i in range(4)
        ]
        self._grid_b = [
            complex(math.cos(math.pi * i / 2), math.sin(math.pi * i / 2))
            for i in range(4)
        ]

    def demodulate(self, iq_samples: np.ndarray) -> DemodResult:
        x = np.asarray(iq_samples, dtype=np.complex128)
        if x.size == 0:
            raise ValueError("IQ samples array is empty.")

        num_symbols = x.size // self.samples_per_symbol
        symbols: List[DemodSymbol] = []
        hard_bits_list: List[str] = []
        llrs: List[float] = []

        for sym_idx in range(num_symbols):
            start = sym_idx * self.samples_per_symbol
            end = start + self.samples_per_symbol
            s = np.mean(x[start:end])

            grid = self._grid_a if (sym_idx % 2 == 0) else self._grid_b
            energies = [float(abs(s - ref) ** 2) for ref in grid]
            best_idx = int(np.argmin(energies))
            hard_bits = self._GRAY_BITS[best_idx]

            denom = sum(energies) + 1e-12
            e00, e01, e11, e10 = energies
            llr_b0 = ((e11 + e10) - (e00 + e01)) / denom
            llr_b1 = ((e01 + e11) - (e00 + e10)) / denom

            hard_bits_list.append(hard_bits)
            llrs.extend([llr_b0, llr_b1])
            symbols.append(
                DemodSymbol(
                    symbol_index=sym_idx,
                    tones_energy=energies,
                    hard_bits=hard_bits,
                    soft_bits_llr=[llr_b0, llr_b1],
                    confidence=float(max(0.0, min(1.0, 1.0 - min(energies) / (denom + 1e-12)))),
                )
            )

        return DemodResult(
            bit_string="".join(hard_bits_list),
            soft_bits_llr=llrs,
            symbols=symbols,
            symbol_timing_offset=0,
            snr_estimate_db=self._estimate_snr_db(x),
        )


class DQPSKDemodulator(SoftDemodulator):
    """Differential QPSK demodulator matching DQPSKModulator mapping."""

    _DELTA_BITS = {
        0: "00",
        1: "01",
        2: "11",
        3: "10",
    }

    def __init__(self, sample_rate: float, symbol_rate: float, samples_per_bit: int):
        super().__init__(sample_rate, symbol_rate, samples_per_bit)
        self.samples_per_symbol = 2 * samples_per_bit

    @staticmethod
    def _wrap_phase_delta(delta: float) -> float:
        while delta > math.pi:
            delta -= 2.0 * math.pi
        while delta < -math.pi:
            delta += 2.0 * math.pi
        return delta

    def demodulate(self, iq_samples: np.ndarray) -> DemodResult:
        x = np.asarray(iq_samples, dtype=np.complex128)
        if x.size == 0:
            raise ValueError("IQ samples array is empty.")

        num_symbols = x.size // self.samples_per_symbol
        symbols: List[DemodSymbol] = []
        hard_bits_list: List[str] = []
        llrs: List[float] = []

        prev_phase = math.pi / 4.0
        ideal_deltas = [0.0, math.pi / 2.0, math.pi, -math.pi / 2.0]

        for sym_idx in range(num_symbols):
            start = sym_idx * self.samples_per_symbol
            end = start + self.samples_per_symbol
            s = np.mean(x[start:end])
            phase = float(np.angle(s))
            delta = self._wrap_phase_delta(phase - prev_phase)

            errors = [abs(self._wrap_phase_delta(delta - ref)) for ref in ideal_deltas]
            best_idx = int(np.argmin(errors))
            hard_bits = self._DELTA_BITS[best_idx]

            denom = sum(errors) + 1e-12
            d0, d1, d2, d3 = errors
            llr_b0 = ((d2 + d3) - (d0 + d1)) / denom
            llr_b1 = ((d1 + d2) - (d0 + d3)) / denom

            hard_bits_list.append(hard_bits)
            llrs.extend([llr_b0, llr_b1])
            symbols.append(
                DemodSymbol(
                    symbol_index=sym_idx,
                    tones_energy=errors,
                    hard_bits=hard_bits,
                    soft_bits_llr=[llr_b0, llr_b1],
                    confidence=float(max(0.0, min(1.0, 1.0 - min(errors) / (math.pi + 1e-12)))),
                )
            )

            prev_phase = phase

        return DemodResult(
            bit_string="".join(hard_bits_list),
            soft_bits_llr=llrs,
            symbols=symbols,
            symbol_timing_offset=0,
            snr_estimate_db=self._estimate_snr_db(x),
        )


class OQPSKDemodulator(SoftDemodulator):
    """OQPSK demodulator using half-symbol offset-aware I/Q sampling."""

    def __init__(self, sample_rate: float, symbol_rate: float, samples_per_bit: int):
        super().__init__(sample_rate, symbol_rate, samples_per_bit)
        self.samples_per_symbol = 2 * samples_per_bit

    def demodulate(self, iq_samples: np.ndarray) -> DemodResult:
        x = np.asarray(iq_samples, dtype=np.complex128)
        if x.size == 0:
            raise ValueError("IQ samples array is empty.")

        num_symbols = x.size // self.samples_per_symbol
        symbols: List[DemodSymbol] = []
        hard_bits_list: List[str] = []
        llrs: List[float] = []

        for sym_idx in range(num_symbols):
            start = sym_idx * self.samples_per_symbol
            end = start + self.samples_per_symbol
            mid = start + self.samples_per_bit

            i_val = float(np.mean(np.real(x[start:end])))
            q_window = x[mid:end] if end > mid else x[start:end]
            q_val = float(np.mean(np.imag(q_window)))

            if i_val >= 0.0 and q_val >= 0.0:
                hard_bits = "00"
            elif i_val < 0.0 and q_val >= 0.0:
                hard_bits = "01"
            elif i_val < 0.0 and q_val < 0.0:
                hard_bits = "11"
            else:
                hard_bits = "10"

            denom = abs(i_val) + abs(q_val) + 1e-12
            llr_b0 = (-q_val) / denom
            llr_b1 = (-i_val) / denom

            hard_bits_list.append(hard_bits)
            llrs.extend([llr_b0, llr_b1])
            symbols.append(
                DemodSymbol(
                    symbol_index=sym_idx,
                    tones_energy=[i_val * i_val + q_val * q_val],
                    hard_bits=hard_bits,
                    soft_bits_llr=[llr_b0, llr_b1],
                    confidence=float(min(1.0, (abs(i_val) + abs(q_val)) / (abs(i_val) + abs(q_val) + 1e-12))),
                )
            )

        return DemodResult(
            bit_string="".join(hard_bits_list),
            soft_bits_llr=llrs,
            symbols=symbols,
            symbol_timing_offset=0,
            snr_estimate_db=self._estimate_snr_db(x),
        )


class GFSKDemodulator(SoftDemodulator):
    """GFSK soft-decision demodulator via instantaneous-frequency slicing."""

    def __init__(
        self,
        sample_rate: float,
        symbol_rate: float,
        samples_per_bit: int,
        tone0: float,
        tone1: float,
        gfsk_bw_hz: float = 200.0,
    ):
        super().__init__(sample_rate, symbol_rate, samples_per_bit)
        self.tone0 = float(tone0)
        self.tone1 = float(tone1)
        self.gfsk_bw_hz = float(gfsk_bw_hz)
        # The decode path passes a fractional sps (fs_out/symrate).
        # This class slices with samples_per_symbol directly, and a float raises
        # TypeError, which the caller's offset loop swallows (GFSK would give
        # zero bits on every live block). Coerce to int.
        self.samples_per_symbol = max(1, int(round(float(samples_per_bit))))  # 1 bit per symbol

    def demodulate(self, iq_samples: np.ndarray) -> DemodResult:
        """Demodulate GFSK as smoothed 2-level FSK."""
        x = np.asarray(iq_samples, dtype=np.complex128)
        if x.size < 2:
            raise ValueError("Need at least 2 IQ samples for GFSK demodulation.")

        phase = np.unwrap(np.angle(x))
        inst_freq = np.diff(phase) * self.sample_rate / (2.0 * np.pi)
        # np.diff drops one sample, so on a buffer that is an
        # exact multiple of sps the last symbol floors out ((N-1)//sps, one short
        # of N//sps). Edge-extend by one to recover the final symbol; harmless
        # on continuous blocks (yields one extra symbol the frame layer ignores).
        inst_freq = np.append(inst_freq, inst_freq[-1])

        num_symbols = inst_freq.size // self.samples_per_symbol
        # CFO slides the whole inst-freq cloud off the authored
        # midpoint; use the data-derived median of per-symbol means instead
        # (same split FSKDemodulator uses).
        _sps_i = self.samples_per_symbol
        _means = np.array([
            float(np.mean(inst_freq[k * _sps_i:(k + 1) * _sps_i]))
            for k in range(num_symbols)
        ]) if num_symbols else np.zeros(0)
        threshold = float(np.median(_means)) if _means.size else 0.5 * (self.tone0 + self.tone1)
        half_sep = max(1e-9, abs(self.tone1 - self.tone0) * 0.5)

        symbols: List[DemodSymbol] = []
        hard_bits_list: List[str] = []
        llrs: List[float] = []

        for sym_idx in range(num_symbols):
            start = sym_idx * self.samples_per_symbol
            end = start + self.samples_per_symbol

            # GFSK has intentional Gaussian transition smoothing; center samples
            # of each symbol carry stronger symbol evidence than edges.
            mid = start + (self.samples_per_symbol // 2)
            win_half = max(1, self.samples_per_symbol // 8)
            w0 = max(start, mid - win_half)
            w1 = min(end, mid + win_half + 1)
            avg_f = float(np.mean(inst_freq[w0:w1]))

            hard_bit = "1" if avg_f >= threshold else "0"
            llr = (avg_f - threshold) / half_sep
            llr = max(-5.0, min(5.0, llr))

            hard_bits_list.append(hard_bit)
            llrs.append(llr)
            symbols.append(
                DemodSymbol(
                    symbol_index=sym_idx,
                    tones_energy=[abs(avg_f - self.tone0), abs(avg_f - self.tone1)],
                    hard_bits=hard_bit,
                    soft_bits_llr=[llr],
                    confidence=float(min(1.0, abs(llr) / 3.0)),
                )
            )

        return DemodResult(
            bit_string="".join(hard_bits_list),
            soft_bits_llr=llrs,
            symbols=symbols,
            symbol_timing_offset=0,
            snr_estimate_db=self._estimate_snr_db(x),
        )


class HigherOrderDemodulator(SoftDemodulator):
    """Adapter: wraps a higher-order constellation modulator (8PSK/8QAM/16QAM/
    16/32APSK) as a SoftDemodulator. The map/demod/LLR math lives in
    dpmg.modulation.higher_order; this shapes its output into a DemodResult so
    the frame/FEC chain treats it like any other mod."""

    def __init__(self, sample_rate, symbol_rate, samples_per_bit,
                 modulation_type, use_rrc=False, rolloff=0.35, rrc_span=10):
        super().__init__(sample_rate, symbol_rate, samples_per_bit)
        from dpmg.modulation.higher_order import get_higher_order_modulator
        self._mod = get_higher_order_modulator(
            modulation_type, symbol_rate, samples_per_bit,
            use_rrc=use_rrc, rolloff=rolloff, rrc_span=rrc_span)
        self.bits_per_symbol = self._mod.bits_per_symbol
        self.samples_per_symbol = samples_per_bit

    def demodulate(self, iq_samples: np.ndarray) -> DemodResult:
        iq = np.asarray(iq_samples, dtype=complex)
        if len(iq) == 0:
            raise ValueError("IQ samples array is empty.")
        hard = self._mod.demodulate(iq)
        # n0 from the SNR estimate so LLR magnitudes track the channel.
        snr_db = self._estimate_snr_db(iq)
        n0 = float(10.0 ** (-snr_db / 10.0)) or 1.0
        llr = self._mod.soft_llr(iq, n0=n0)
        b = self.bits_per_symbol
        symbols: List[DemodSymbol] = []
        for s in range(len(hard) // b):
            hb = hard[s * b:(s + 1) * b]
            sl = [float(x) for x in llr[s * b:(s + 1) * b]]
            conf = float(np.tanh(np.mean(np.abs(sl)) / 4.0)) if sl else 0.0
            symbols.append(DemodSymbol(symbol_index=s, tones_energy=[],
                                       hard_bits=hb, soft_bits_llr=sl,
                                       confidence=conf))
        return DemodResult(bit_string=hard, soft_bits_llr=[float(x) for x in llr],
                           symbols=symbols, symbol_timing_offset=0,
                           snr_estimate_db=snr_db)


def get_demodulator(
    modulation_type: str,
    sample_rate: float,
    symbol_rate: float,
    samples_per_bit: int,
    tone0: float = 200.0,
    tone1: float = 1000.0,
    tone2: float = 1800.0,
    tone3: float = 2600.0,
    dibit_mapping: str = "binary",
    **kwargs,
) -> SoftDemodulator:
    """Factory function to create demodulator by type.

    Args:
        modulation_type: "FSK", "4FSK", "BPSK", "QPSK", "π/4-QPSK", "DQPSK", "OQPSK", "GFSK"
        sample_rate: ADC sample rate (Hz)
        symbol_rate: Symbol rate (Hz)
        samples_per_bit: Samples per bit
        tone0-3: Tone frequencies for FSK/4FSK (Hz)
        **kwargs: Additional parameters (e.g., gfsk_bw_hz)

    Returns:
        SoftDemodulator instance
    """
    if modulation_type in ("FSK", "2FSK"):
        return FSKDemodulator(sample_rate, symbol_rate, samples_per_bit, tone0, tone1)
    if modulation_type == "4FSK":
        return FSK4Demodulator(
            sample_rate, symbol_rate, samples_per_bit, tone0, tone1, tone2, tone3, dibit_mapping
        )
    if modulation_type == "QPSK":
        return QPSKDemodulator(sample_rate, symbol_rate, samples_per_bit)
    if modulation_type == "BPSK":
        return BPSKDemodulator(sample_rate, symbol_rate, samples_per_bit)
    if modulation_type in ("π/4-QPSK", "pi/4-QPSK", "PI4QPSK"):
        return PiQuarterQPSKDemodulator(sample_rate, symbol_rate, samples_per_bit)
    if modulation_type == "DQPSK":
        return DQPSKDemodulator(sample_rate, symbol_rate, samples_per_bit)
    if modulation_type == "OQPSK":
        return OQPSKDemodulator(sample_rate, symbol_rate, samples_per_bit)
    if modulation_type == "GFSK":
        gfsk_bw = kwargs.get("gfsk_bw_hz", 200.0)
        return GFSKDemodulator(sample_rate, symbol_rate, samples_per_bit, tone0, tone1, gfsk_bw)
    if modulation_type in ("DSSS", "BPSK-DSSS", "DSSS-BPSK", "QPSK-DSSS", "DSSS-QPSK",
                           "OQPSK-DSSS", "PI4QPSK-DSSS"):          # DSSS_CARRIERS
        from dpmg.modulation.spread_spectrum import DSSSDemodulator
        _dm = ("qpsk" if modulation_type in ("QPSK-DSSS", "DSSS-QPSK")
               else "bpsk" if modulation_type in ("BPSK-DSSS", "DSSS-BPSK")
               else "oqpsk" if modulation_type == "OQPSK-DSSS"
               else "pi4qpsk" if modulation_type == "PI4QPSK-DSSS"
               else str(kwargs.get("dsss_modem", "bpsk")))
        return DSSSDemodulator(sample_rate, symbol_rate, samples_per_bit,
                               spread_factor=int(kwargs.get("spread_factor", 15)),
                               pn_seed=int(kwargs.get("pn_seed", 1)),
                               code_spec=kwargs.get("code_spec"), modem=_dm,
                               code_spec_q=kwargs.get("code_spec_q"),
                               overlay_codes=kwargs.get("overlay_codes"))   # overlay cancellation
    if modulation_type in ("LoRa", "CSS", "LoRa-CSS"):
        from dpmg.modulation.spread_spectrum import LoRaCSSDemodulator
        return LoRaCSSDemodulator(sf=int(kwargs.get("css_sf", 7)),
                                  bw=float(kwargs.get("css_bw", 125_000.0)),
                                  os=int(kwargs.get("css_os", 1)))
    if modulation_type in ("8PSK", "8QAM", "16QAM", "16APSK", "32APSK"):
        return HigherOrderDemodulator(
            sample_rate, symbol_rate, samples_per_bit, modulation_type,
            use_rrc=bool(kwargs.get("use_rrc", False)),
            rolloff=float(kwargs.get("rolloff", 0.35)),
            rrc_span=int(kwargs.get("rrc_span", 10)))
    raise ValueError(f"Unknown modulation type: {modulation_type}")
