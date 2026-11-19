#!/usr/bin/env python3
"""Flexible modulation framework for the DPMG modem.

Supports FSK, 4FSK, QPSK, and GFSK waveform generation.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, List

import numpy as np


def get_4fsk_dibit_table(dibit_mapping: str) -> Dict[str, int]:
    """Return dibit to tone-index table for 4FSK.

    Supported mappings:
    - "binary": 00,01,10,11: tone0,tone1,tone2,tone3
    - "gray":   00,01,11,10: tone0,tone1,tone2,tone3
    - "dmr":    11,10,00,01: tone0,tone1,tone2,tone3
      (when tones are ordered low to high as [-3d,-d,+d,+3d], this is a
      commonly used DMR-style C4FM ordering)
    - "dmr_inverted": 01,00,10,11: tone0,tone1,tone2,tone3
    """
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
class ModulationResult:
    """Generated IQ waveform and sample rate."""

    iq_samples: List[complex]
    sample_rate: int


class Modulator(ABC):
    """Base modulator interface."""

    def __init__(self, symbol_rate: float, samples_per_bit: int, amplitude: float = 0.8):
        if symbol_rate <= 0:
            raise ValueError("symbol_rate must be positive")
        if samples_per_bit <= 0:
            raise ValueError("samples_per_bit must be positive")
        if amplitude <= 0:
            raise ValueError("amplitude must be positive")

        self.symbol_rate = float(symbol_rate)
        self.samples_per_bit = int(samples_per_bit)
        self.amplitude = float(amplitude)
        self.sample_rate = int(round(self.symbol_rate * self.samples_per_bit))

    @abstractmethod
    def modulate(self, bit_string: str) -> ModulationResult:
        pass


class FSKModulator(Modulator):
    def __init__(self, symbol_rate: float, samples_per_bit: int, tone0: float, tone1: float, amplitude: float = 0.8):
        super().__init__(symbol_rate, samples_per_bit, amplitude)
        if tone0 == tone1:  # Signed baseband deltas OK; only reject identical tones
            raise ValueError("tone0 and tone1 must differ")
        self.tone0 = float(tone0)
        self.tone1 = float(tone1)

    def modulate(self, bit_string: str) -> ModulationResult:
        phase = 0.0
        out = []
        for bit in bit_string:
            freq = self.tone1 if bit == "1" else self.tone0
            dphi = 2.0 * math.pi * freq / self.sample_rate
            for _ in range(self.samples_per_bit):
                out.append(complex(self.amplitude * math.cos(phase), self.amplitude * math.sin(phase)))
                phase = (phase + dphi) % (2.0 * math.pi)
        return ModulationResult(iq_samples=out, sample_rate=self.sample_rate)


class FSK4Modulator(Modulator):
    def __init__(
        self,
        symbol_rate: float,
        samples_per_bit: int,
        tone0: float,
        tone1: float,
        tone2: float,
        tone3: float,
        dibit_mapping: str = "binary",
        amplitude: float = 0.8,
    ):
        super().__init__(symbol_rate, samples_per_bit, amplitude)
        self.tones = [float(tone0), float(tone1), float(tone2), float(tone3)]
        if any(t == 0 for t in self.tones):
            raise ValueError("all 4FSK tones must be non-zero")
        self.dibit_mapping = dibit_mapping
        self.table = get_4fsk_dibit_table(dibit_mapping)

    def modulate(self, bit_string: str) -> ModulationResult:
        if len(bit_string) % 2 == 1:
            bit_string += "0"

        phase = 0.0
        out = []
        # samples_per_bit == samples per symbol for 4FSK: the base class
        # defines sample_rate = symbol_rate * samples_per_bit, so using
        # samples_per_bit here gives exactly the configured symbol rate.
        samples_per_symbol = self.samples_per_bit

        for i in range(0, len(bit_string), 2):
            dibit = bit_string[i : i + 2]
            freq = self.tones[self.table[dibit]]
            dphi = 2.0 * math.pi * freq / self.sample_rate
            for _ in range(samples_per_symbol):
                out.append(complex(self.amplitude * math.cos(phase), self.amplitude * math.sin(phase)))
                phase = (phase + dphi) % (2.0 * math.pi)

        return ModulationResult(iq_samples=out, sample_rate=self.sample_rate)


class QPSKModulator(Modulator):
    """Gray-coded QPSK: 00,01,11,10 around the unit circle."""

    def modulate(self, bit_string: str) -> ModulationResult:
        if len(bit_string) % 2 == 1:
            bit_string += "0"

        symbols = {
            "00": complex(1.0, 1.0),
            "01": complex(-1.0, 1.0),
            "11": complex(-1.0, -1.0),
            "10": complex(1.0, -1.0),
        }
        norm = 1.0 / math.sqrt(2.0)
        samples_per_symbol = 2 * self.samples_per_bit

        out = []
        for i in range(0, len(bit_string), 2):
            s = symbols[bit_string[i : i + 2]] * (self.amplitude * norm)
            out.extend([s] * samples_per_symbol)

        return ModulationResult(iq_samples=out, sample_rate=self.sample_rate)


class GFSKModulator(Modulator):
    def __init__(
        self,
        symbol_rate: float,
        samples_per_bit: int,
        tone0: float,
        tone1: float,
        gfsk_bw_hz: float = 200.0,
        amplitude: float = 0.8,
    ):
        super().__init__(symbol_rate, samples_per_bit, amplitude)
        if tone0 == tone1:  # Signed baseband deltas OK; only reject identical tones
            raise ValueError("tone0 and tone1 must differ")
        if gfsk_bw_hz <= 0:
            raise ValueError("gfsk_bw_hz must be positive")
        self.tone0 = float(tone0)
        self.tone1 = float(tone1)
        self.gfsk_bw_hz = float(gfsk_bw_hz)

    def _gaussian_kernel(self) -> np.ndarray:
        # BT-derived sigma: for a Gaussian with 3-dB bandwidth B,
        # sigma_t = sqrt(ln2)/(2*pi*B); in samples that is
        # spb * sqrt(ln2)/(2*pi) / BT with BT = gfsk_bw_hz/symbol_rate.
        # A fixed 0.5*spb would ignore gfsk_bw_hz (equivalent BT~0.26),
        # and its ISI flips isolated bits at any deviation.
        bt = max(0.15, min(2.0, self.gfsk_bw_hz / max(1.0, self.symbol_rate)))
        sigma = max(1.0, 0.1325 * self.samples_per_bit / bt)
        half = max(3, int(round(3.0 * sigma)))
        n = np.arange(-half, half + 1, dtype=np.float64)
        k = np.exp(-0.5 * (n / sigma) ** 2)
        return k / np.sum(k)

    def modulate(self, bit_string: str) -> ModulationResult:
        if len(bit_string) == 0:
            return ModulationResult(iq_samples=[], sample_rate=self.sample_rate)

        base = 0.5 * (self.tone0 + self.tone1)
        dev = 0.5 * (self.tone1 - self.tone0)

        nrz = np.concatenate([
            np.full(self.samples_per_bit, 1.0 if b == "1" else -1.0, dtype=np.float64)
            for b in bit_string
        ])

        shaped = np.convolve(nrz, self._gaussian_kernel(), mode="same")
        shaped = np.clip(shaped, -1.2, 1.2)
        inst_freq = base + dev * shaped

        phase = np.cumsum(2.0 * math.pi * inst_freq / self.sample_rate)
        iq = self.amplitude * (np.cos(phase) + 1j * np.sin(phase))

        return ModulationResult(iq_samples=list(iq), sample_rate=self.sample_rate)


class BPSKModulator(Modulator):
    """BPSK: 1 bit/symbol: constellation {0 to +1+0j, 1 to -1+0j} on I axis."""

    def modulate(self, bit_string: str) -> ModulationResult:
        samples_per_symbol = self.samples_per_bit
        out = []
        for bit in bit_string:
            s = complex(self.amplitude, 0.0) if bit == "0" else complex(-self.amplitude, 0.0)
            out.extend([s] * samples_per_symbol)
        return ModulationResult(iq_samples=out, sample_rate=self.sample_rate)


class PiQuarterQPSKModulator(Modulator):
    """pi/4-QPSK: alternates between two QPSK grids offset by 45 deg.

    Grid A (even symbols): 45, 135, 225, 315 deg
    Grid B (odd symbols):   0,  90, 180, 270 deg

    Gray-coded dibit mapping: 00, 01, 11, 10: tone indices 0-3
    within each grid (same angular order as standard QPSK).
    """

    _GRAY = {"00": 0, "01": 1, "11": 2, "10": 3}

    def modulate(self, bit_string: str) -> ModulationResult:
        if len(bit_string) % 2 == 1:
            bit_string += "0"

        norm = self.amplitude
        # Grid A: starting at 45 deg
        grid_a = [
            complex(norm * math.cos(math.pi * (1 + 2 * i) / 4), norm * math.sin(math.pi * (1 + 2 * i) / 4))
            for i in range(4)
        ]
        # Grid B: starting at 0 deg
        grid_b = [
            complex(norm * math.cos(math.pi * i / 2), norm * math.sin(math.pi * i / 2))
            for i in range(4)
        ]

        samples_per_symbol = 2 * self.samples_per_bit
        out = []
        for sym_idx, i in enumerate(range(0, len(bit_string), 2)):
            dibit = bit_string[i : i + 2]
            grid = grid_a if sym_idx % 2 == 0 else grid_b
            s = grid[self._GRAY[dibit]]
            out.extend([s] * samples_per_symbol)

        return ModulationResult(iq_samples=out, sample_rate=self.sample_rate)


class DQPSKModulator(Modulator):
    """Differentially encoded QPSK.

    The dibit encodes the phase change from the previous symbol:
      00: +0 deg   (no change)
      01: +90 deg
      11: +180 deg
      10: +270 deg (i.e. -90 deg)

    Gray-coded mapping. Initial phase is 45 deg.
    """

    _DELTA = {"00": 0.0, "01": math.pi / 2, "11": math.pi, "10": 3 * math.pi / 2}

    def modulate(self, bit_string: str) -> ModulationResult:
        if len(bit_string) % 2 == 1:
            bit_string += "0"

        samples_per_symbol = 2 * self.samples_per_bit
        phase = math.pi / 4  # initial reference phase
        out = []
        for i in range(0, len(bit_string), 2):
            dibit = bit_string[i : i + 2]
            phase = (phase + self._DELTA[dibit]) % (2 * math.pi)
            s = complex(self.amplitude * math.cos(phase), self.amplitude * math.sin(phase))
            out.extend([s] * samples_per_symbol)

        return ModulationResult(iq_samples=out, sample_rate=self.sample_rate)


class OQPSKModulator(Modulator):
    """Offset QPSK (OQPSK).

    Same as standard Gray-coded QPSK but the Q channel is delayed by
    half a symbol period (samples_per_bit samples) to prevent 180 deg phase
    transitions.
    """

    _SYMBOLS = {
        "00": (complex(1.0, 0.0),  complex(0.0, 1.0)),   # I=+1, Q=+1  (split)
        "01": (complex(-1.0, 0.0), complex(0.0, 1.0)),   # I=-1, Q=+1
        "11": (complex(-1.0, 0.0), complex(0.0, -1.0)),  # I=-1, Q=-1
        "10": (complex(1.0, 0.0),  complex(0.0, -1.0)),  # I=+1, Q=-1
    }

    def modulate(self, bit_string: str) -> ModulationResult:
        if len(bit_string) % 2 == 1:
            bit_string += "0"

        spb = self.samples_per_bit  # half-symbol delay = spb samples
        norm = self.amplitude / math.sqrt(2.0)
        i_stream: List[complex] = []
        q_stream: List[complex] = []

        for idx in range(0, len(bit_string), 2):
            dibit = bit_string[idx : idx + 2]
            i_sym, q_sym = self._SYMBOLS[dibit]
            i_stream.extend([i_sym * norm] * (2 * spb))
            q_stream.extend([q_sym * norm] * (2 * spb))

        # Delay Q by half symbol (spb samples)
        q_delayed = [complex(0.0, 0.0)] * spb + q_stream
        total = len(i_stream)
        out = [i_stream[k] + q_delayed[k] for k in range(total)]

        return ModulationResult(iq_samples=out, sample_rate=self.sample_rate)


def get_modulator(
    modulation_type: str,
    symbol_rate: float,
    samples_per_bit: int,
    tone0: float = 200.0,
    tone1: float = 1000.0,
    tone2: float = 1800.0,
    tone3: float = 2600.0,
    dibit_mapping: str = "binary",
    amplitude: float = 0.8,
    gfsk_bw_hz: float = 200.0,
    **kwargs,
) -> Modulator:
    """Factory for supported modulation types."""
    if modulation_type in ("DSSS", "BPSK-DSSS", "DSSS-BPSK", "QPSK-DSSS", "DSSS-QPSK",
                           "OQPSK-DSSS", "PI4QPSK-DSSS"):          # DSSS_CARRIERS
        from dpmg.modulation.spread_spectrum import DSSSModulator
        _dm = ("qpsk" if modulation_type in ("QPSK-DSSS", "DSSS-QPSK")
               else "bpsk" if modulation_type in ("BPSK-DSSS", "DSSS-BPSK")
               else "oqpsk" if modulation_type == "OQPSK-DSSS"
               else "pi4qpsk" if modulation_type == "PI4QPSK-DSSS"
               else str(kwargs.get("dsss_modem", "bpsk")))
        return DSSSModulator(symbol_rate, samples_per_bit,
                             spread_factor=int(kwargs.get("spread_factor", 15)),
                             amplitude=amplitude,
                             pn_seed=int(kwargs.get("pn_seed", 1)),
                             code_spec=kwargs.get("code_spec"),
                             modem=_dm,
                             code_spec_q=kwargs.get("code_spec_q"))
    if modulation_type in ("LoRa", "CSS", "LoRa-CSS"):
        from dpmg.modulation.spread_spectrum import LoRaCSSModulator
        return LoRaCSSModulator(sf=int(kwargs.get("css_sf", 7)),
                                bw=float(kwargs.get("css_bw", 125_000.0)),
                                os=int(kwargs.get("css_os", 1)),
                                amplitude=amplitude)
    if modulation_type in ("FSK", "2FSK"):
        return FSKModulator(symbol_rate, samples_per_bit, tone0, tone1, amplitude)
    if modulation_type == "4FSK":
        return FSK4Modulator(symbol_rate, samples_per_bit, tone0, tone1, tone2, tone3, dibit_mapping, amplitude)
    if modulation_type == "BPSK":
        return BPSKModulator(symbol_rate, samples_per_bit, amplitude)
    if modulation_type == "QPSK":
        return QPSKModulator(symbol_rate, samples_per_bit, amplitude)
    if modulation_type in ("\u03c0/4-QPSK", "pi/4-QPSK", "PI4QPSK"):
        return PiQuarterQPSKModulator(symbol_rate, samples_per_bit, amplitude)
    if modulation_type == "DQPSK":
        return DQPSKModulator(symbol_rate, samples_per_bit, amplitude)
    if modulation_type == "OQPSK":
        return OQPSKModulator(symbol_rate, samples_per_bit, amplitude)
    if modulation_type == "GFSK":
        return GFSKModulator(symbol_rate, samples_per_bit, tone0, tone1, gfsk_bw_hz, amplitude)
    if modulation_type in ("8PSK", "8QAM", "16QAM", "16APSK", "32APSK"):
        # higher-order (CDM-625A class): samples_per_bit is samples per symbol here.
        # RRC off by default to match the rectangular-hold TX chain the other
        # mods use; enable via use_rrc=True in the plan for pulse shaping.
        from dpmg.modulation.higher_order import get_higher_order_modulator
        return get_higher_order_modulator(
            modulation_type, symbol_rate, samples_per_bit,
            amplitude=amplitude,
            use_rrc=bool(kwargs.get("use_rrc", False)),
            rolloff=float(kwargs.get("rolloff", 0.35)),
            rrc_span=int(kwargs.get("rrc_span", 10)))
    raise ValueError(f"Unknown modulation type: {modulation_type}")
