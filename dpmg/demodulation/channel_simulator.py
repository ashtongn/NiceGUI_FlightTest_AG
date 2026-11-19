#!/usr/bin/env python3
"""Channel simulator for DPMG.

Applies common impairments to complex baseband IQ samples:
- AWGN
- Carrier frequency offset
- Phase noise
- Multipath echo
- Symbol timing error (sample-rate mismatch via resampling)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np


@dataclass
class ChannelParameters:
    """Parameter set for channel impairments."""

    enabled: bool = False
    awgn_snr_db: Optional[float] = None
    freq_offset_hz: float = 0.0
    phase_noise_deg_rms: float = 0.0
    multipath_delay_us: float = 0.0
    multipath_attenuation_db: float = 0.0
    symbol_timing_error_ppm: float = 0.0
    random_seed: Optional[int] = None


class ChannelSimulator:
    """Applies channel impairments to complex IQ sample vectors."""

    def __init__(self, sample_rate_hz: float, random_seed: Optional[int] = None):
        if sample_rate_hz <= 0:
            raise ValueError("sample_rate_hz must be positive")
        self.sample_rate_hz = float(sample_rate_hz)
        self.rng = np.random.default_rng(random_seed)

    def apply_awgn(self, iq_samples: np.ndarray, snr_db: float) -> np.ndarray:
        """Add complex AWGN to achieve target SNR in dB.

        SNR is measured as ratio of average signal power to average noise power.
        """
        if not np.isfinite(snr_db):
            return np.asarray(iq_samples, dtype=np.complex128).copy()

        x = np.asarray(iq_samples, dtype=np.complex128)
        if x.size == 0:
            return x.copy()

        signal_power = float(np.mean(np.abs(x) ** 2))
        if signal_power <= 0:
            return x.copy()

        snr_linear = 10.0 ** (snr_db / 10.0)
        noise_power = signal_power / max(snr_linear, 1e-15)
        sigma = np.sqrt(noise_power / 2.0)

        noise = sigma * (self.rng.standard_normal(x.size) + 1j * self.rng.standard_normal(x.size))
        return x + noise

    def apply_frequency_offset(self, iq_samples: np.ndarray, freq_offset_hz: float) -> np.ndarray:
        """Apply carrier frequency offset by complex rotation."""
        x = np.asarray(iq_samples, dtype=np.complex128)
        if x.size == 0 or abs(freq_offset_hz) < 1e-15:
            return x.copy()

        n = np.arange(x.size, dtype=np.float64)
        phase = 2.0 * np.pi * float(freq_offset_hz) * n / self.sample_rate_hz
        return x * np.exp(1j * phase)

    def apply_phase_noise(self, iq_samples: np.ndarray, phase_noise_deg_rms: float) -> np.ndarray:
        """Apply white phase jitter with RMS in degrees."""
        x = np.asarray(iq_samples, dtype=np.complex128)
        if x.size == 0 or phase_noise_deg_rms <= 0.0:
            return x.copy()

        sigma_rad = np.deg2rad(float(phase_noise_deg_rms))
        jitter = self.rng.normal(0.0, sigma_rad, size=x.size)
        return x * np.exp(1j * jitter)

    def apply_multipath(
        self,
        iq_samples: np.ndarray,
        delay_us: float,
        attenuation_db: float,
    ) -> np.ndarray:
        """Apply a single delayed echo path.

        Delay is rounded to nearest sample. If delay rounds to 0, input is unchanged.
        """
        x = np.asarray(iq_samples, dtype=np.complex128)
        if x.size == 0:
            return x.copy()
        if delay_us <= 0.0:
            return x.copy()

        delay_samples = int(round(delay_us * 1e-6 * self.sample_rate_hz))
        if delay_samples <= 0 or delay_samples >= x.size:
            return x.copy()

        # Convert attenuation in dB to amplitude ratio.
        echo_gain = 10.0 ** (-float(attenuation_db) / 20.0)
        y = x.copy()
        y[delay_samples:] += echo_gain * x[:-delay_samples]
        return y

    def apply_timing_error(self, iq_samples: np.ndarray, timing_error_ppm: float) -> np.ndarray:
        """Apply timing error by fractional-time resampling back to original length.

        Positive ppm means receiver clock faster than ideal, equivalent to slight
        time compression of the waveform when mapped onto a fixed sample grid.
        """
        x = np.asarray(iq_samples, dtype=np.complex128)
        n = x.size
        if n <= 1 or abs(timing_error_ppm) < 1e-15:
            return x.copy()

        eps = float(timing_error_ppm) * 1e-6
        src_index = np.arange(n, dtype=np.float64) * (1.0 + eps)
        src_index = np.clip(src_index, 0.0, n - 1.0)

        i_interp = np.interp(src_index, np.arange(n, dtype=np.float64), x.real)
        q_interp = np.interp(src_index, np.arange(n, dtype=np.float64), x.imag)
        return i_interp + 1j * q_interp

    def apply(self, iq_samples: np.ndarray, params: ChannelParameters) -> np.ndarray:
        """Apply all configured impairments in deterministic order."""
        x = np.asarray(iq_samples, dtype=np.complex128)
        if x.size == 0:
            return x.copy()

        if not params.enabled:
            return x.copy()

        if params.random_seed is not None:
            self.rng = np.random.default_rng(params.random_seed)

        y = self.apply_frequency_offset(x, params.freq_offset_hz)
        y = self.apply_phase_noise(y, params.phase_noise_deg_rms)
        y = self.apply_multipath(y, params.multipath_delay_us, params.multipath_attenuation_db)
        y = self.apply_timing_error(y, params.symbol_timing_error_ppm)

        if params.awgn_snr_db is not None:
            y = self.apply_awgn(y, params.awgn_snr_db)

        return y


