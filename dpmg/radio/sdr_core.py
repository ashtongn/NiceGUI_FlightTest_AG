"""
sdr_core.py: framework-agnostic SDR backend, pipeline, scheduler and analysis.

"""

from __future__ import annotations

import enum
import math
import os
import queue
import threading
import time
import wave
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

from dpmg.radio import g40_clock as _g40_clock


X40_VALID_RATES_HZ = [
    50_000_000, 61_440_000, 100_000_000, 122_880_000, 153_600_000, 245_760_000,
]
# Sidekiq NV100 valid sample rates (the G40 is two NV100 cards). From the
# Matchstiq G-series HW manual / NV100 datasheet: 24 ksps to 61.44 Msps.
# These are the manufacturer-listed discrete rates (Msps).
G40_VALID_RATES_HZ = [
    541_667, 1_920_000, 2_457_600, 2_800_000, 3_840_000, 4_000_000,
    4_915_200, 5_600_000, 7_680_000, 9_830_400, 10_000_000, 11_200_000,
    15_360_000, 16_000_000, 20_000_000, 21_666_700, 22_000_000, 23_040_000,
    30_720_000, 40_000_000, 61_440_000,
]
# Ettus B200/B200mini (AD936x): continuous ~0.2-61.44 MSps; UHD raises the
# master clock to hit the requested rate. These discrete values were each
# verified on-device to round-trip exactly (set_rx_rate then get_rx_rate), unlike
# the Sidekiq list. 61.44 MSps is the master-clock max; sustained throughput on
# a given host is lower (USB3 + CPU), which is a host limit, not a radio limit.
B200_VALID_RATES_HZ = [
    1_000_000, 2_000_000, 5_000_000, 10_000_000, 15_360_000, 20_000_000,
    25_000_000, 30_720_000, 40_000_000, 50_000_000, 56_000_000, 61_440_000,
]
 
_X40_RX_HDL = {"RXA1": "rx_hdl_A1", "RXA2": "rx_hdl_A2", "RXB1": "rx_hdl_B1", "RXB2": "rx_hdl_B2"}
_X40_TX_HDL = {"TXA1": "tx_hdl_A1", "TXA2": "tx_hdl_A2"}

# The 18 GHz Matchstiq X40 is 4R1T. Its only transmitter (TXA1 on
# J5) sits on the A-side RFIC, which it shares with the A-side receivers RXA1 (J2)
# and RXA2 (J1): one LO, TDD RFIC, so an A-side RX cannot stream while J5 keys.
# FDD on the X40 is TX on J5 + RX on the B-side (RXB1 / J4, RXB2 / J3). Handle to
# connector wiring is crossed on the mezzanine, hence the tables (Epiq HW manual).
X40_RX_CONNECTOR = {"RXA1": "J2", "RXA2": "J1", "RXB1": "J4", "RXB2": "J3"}
X40_TX_CONNECTOR = {"TXA1": "J5"}
X40_RX_A_SIDE = ("RXA1", "RXA2")


def x40_same_side_conflict(rx_antenna, tx_antenna):
    """Return the reason an X40 RX+TX handle pair cannot co-stream, else None.
    Shared by the GUI (port note / constraint line, before init) and the service
    (start_tx & co., after init) so both say the same thing."""
    rx = str(rx_antenna or "").upper()
    tx = str(tx_antenna or "").upper()
    if rx in X40_RX_A_SIDE and tx.startswith("TXA"):
        return (f"X40: RX {rx} ({X40_RX_CONNECTOR[rx]}) and TX {tx} "
                f"({X40_TX_CONNECTOR.get(tx, 'J5')}) are both A-side: they share "
                f"one LO and cannot stream at the same time. For RX+TX receive on "
                f"RXB1 (J4) or RXB2 (J3); {rx} is receive-only.")
    return None


# Each NV100 card has two shared RX/TX jacks, J1 (TRx1x) and J2
# (TRx2x). Single-card full duplex (dual chan_mode) only works in the libsidekiq
# fixed RF-port mode, which locks J1 = RX (handle RXA1) and J2 = TX (handle TXB1;
# the operator console names it TXA2). TRX mode flips both jacks the same way
# (TDD), so RX on the "2" jack with TX on the "1" jack of the same card can never
# co-stream: the supported single-card FDD is RX TRx1x + TX TRx2x. Cross-card
# pairs (independent cards) and TDD same-port are unaffected. Shared by the
# operator console (port note / START), the engineering view (constraint line /
# INITIALIZE) and the service (_do_init) so all three say the same thing.
def g40_trx_port(handle, card=0):
    """Physical connector for a G40 handle, e.g. RXA1@card1: TRx1B. RX handles
    follow the digit; the TX side of J2/TRx2 is driven by the B-side chain (TxB1
    to TRx2, RF-verified), so TXB1 maps to TRx2 despite its trailing 1."""
    h = str(handle or "").upper()
    sfx = "B" if int(card or 0) == 1 else "A"
    n = "2" if (h in ("TXB1", "TXA2") or h.endswith("2")) else "1"
    return f"TRx{n}{sfx}"


def _g40_same_card_msg(rx_desc, tx_desc, side):
    return (f"G40: RX on {rx_desc} with TX on {tx_desc} cannot co-stream on one "
            f"card. Single-card FDD uses the NV100 FIXED RF-port mode, "
            f"which locks J1 (TRx1{side}) = RX and J2 (TRx2{side}) = TX, and TRX "
            f"mode flips both jacks together, so the \"2\" jack cannot receive "
            f"while the same card transmits. Supported single-card FDD is RX "
            f"TRx1{side} + TX TRx2{side}; otherwise use a cross-card pair.")


def g40_same_card_conflict(rx_port, tx_port):
    """Operator-console form: TRx port names ('TRx1A'..'TRx2B'). Return the
    reason the pair cannot co-stream on one card (same card, RX on the "2" jack,
    TX on the "1" jack), else None. Same-SMA and cross-card pairs return None."""
    rx = str(rx_port or "").upper()
    tx = str(tx_port or "").upper()
    if (len(rx) == 5 and len(tx) == 5 and rx.startswith("TRX") and tx.startswith("TRX")
            and rx[4] == tx[4] and rx[3] == "2" and tx[3] == "1"):
        return _g40_same_card_msg(str(rx_port), str(tx_port), rx[4])
    return None


def g40_fixed_mode_conflict(rx_antenna, tx_antenna, chan_mode, rx_card=0, tx_card=None):
    """Handle form (engineering view / service): a dual-chan_mode (fixed-port)
    init with RX on RXA2 and TX on TXA1 or TXB1 of the same card cannot stream
    RX, because fixed mode makes J2 the TX jack. Returns the same message as
    g40_same_card_conflict, else None. Single chan_mode (TDD / TRX port mode)
    and cross-card pairs are untouched."""
    rx = str(rx_antenna or "").upper()
    tx = str(tx_antenna or "").upper()
    if tx_card is None:
        tx_card = rx_card
    if (str(chan_mode or "").lower() == "dual" and int(rx_card or 0) == int(tx_card or 0)
            and rx == "RXA2" and tx in ("TXA1", "TXB1")):
        rxp = g40_trx_port(rx, rx_card)
        txp = g40_trx_port(tx, tx_card)
        # TXB1 drives the same jack RXA2 listens on (J2/TRx2x), so both sides
        # would read "(TRx2A)"; say why so it is not mistaken for the same-SMA
        # rule. TXA1 keeps the plain "(TRx1A)" that mirrors the operator console.
        tx_desc = (f"{tx} ({txp}, the FIXED-mode TX jack)" if txp == rxp
                   else f"{tx} ({txp})")
        return _g40_same_card_msg(f"{rx} ({rxp})", tx_desc, rxp[-1])
    return None
X40_TX_FULL_SCALE = 8191.0
X40_TX_BLOCK_SAMPLES = 1020
_BLUE_EXTS = (".cdif", ".tmp", ".blue", ".prm")   # Midas BLUE / Platinum recordings
 

try:
    from dpmg.radio import skiq_bridge as _skiqmod
    from dpmg.radio.skiq_bridge import SkiqBridge, RX_HDL as _SKIQ_RX_HDL, TX_HDL as _SKIQ_TX_HDL, SkiqError
    _SKIQ_BRIDGE_AVAILABLE = True
except Exception:
    _skiqmod = None
    SkiqBridge = None
    _SKIQ_RX_HDL = {"RXA1": 0, "RXA2": 1, "RXB1": 2, "RXB2": 3, "RXC1": 4, "RXD1": 5}
    _SKIQ_TX_HDL = {"TXA1": 0, "TXA2": 1}
    class SkiqError(Exception):
        pass
    _SKIQ_BRIDGE_AVAILABLE = False


# On the NV100 a card streams channel A2 only in dual mode. RX runs
# on A1 or A2 in single mode (verified), and TX on A1 is single, but TX on the
# A2 handle needs dual mode + start_tx_streaming on A2 (SW dev manual §5.4.12).
# The transceive case RX TRx1A + TX TRx2A = RXA1 + TXA2 on one card is exactly
# this. Pure decision so it can be unit-tested without hardware.
def needs_dual_channel(rx_antenna: str, tx_antenna: str, extras: dict) -> bool:
    if (extras or {}).get("chan_mode") == "dual":
        return True
    return tx_antenna == "TXA2"


# Enums  (b200_sdr_backend.py)

class SDRDeviceState(enum.Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    IDLE = "IDLE"
    STREAMING = "STREAMING"
    ERROR = "ERROR"
    CLOSING = "CLOSING"


class TXMode(enum.Enum):
    STOPPED = "STOPPED"
    SINGLE_SHOT = "SINGLE_SHOT"
    CONTINUOUS = "CONTINUOUS"


class StreamDirection(enum.Enum):
    RX = "RX"
    TX = "TX"
    FULL_DUPLEX = "FULL_DUPLEX"


# Exceptions (SDRError hierarchy)

class SDRError(Exception):
    pass


class SDRConfigError(SDRError):
    pass


class SDRConnectionError(SDRError):
    pass


class SDRStreamError(SDRError):
    pass


class WaveformError(SDRError):
    pass


# Configuration  (config_manager.py :: SDRConfig)

@dataclass
class SDRConfig:
    sample_rate: float = 1_000_000.0
    center_freq: float = 900_000_000.0
    rx_gain: float = 245.0  # index range is 195-255; 30 clamped to min(195)
    tx_gain: float = 20.0
    clock_source: str = "internal"
    rx_antenna: str = "RX2"
    tx_antenna: str = "TX/RX"
    rx_chunk: int = 4096
    device_args: str = ""
    direction: StreamDirection = StreamDirection.FULL_DUPLEX

    extras: dict = field(default_factory=dict)

    def validate(self) -> "SDRConfig":
        if self.sample_rate <= 0:
            raise SDRConfigError("sample_rate must be > 0")
        if self.center_freq <= 0:
            raise SDRConfigError("center_freq must be > 0")
        if self.rx_chunk <= 0:
            raise SDRConfigError("rx_chunk must be > 0")
        if not (-10.0 <= self.rx_gain <= 255.0):
            raise SDRConfigError("rx_gain out of range")
        if not (-10.0 <= self.tx_gain <= 400.0):
            raise SDRConfigError("tx_gain out of range")
        return self

# Data models

@dataclass
class AnalysisFrame:
    samples: np.ndarray            # complex64
    sample_rate: float
    center_freq: float
    seq: int = 0
    timestamp: float = field(default_factory=time.time)
    rf_timestamp: int = 0  # SDR sample-counter


@dataclass
class StreamHealth:
    state: SDRDeviceState = SDRDeviceState.DISCONNECTED
    rx_frames: int = 0
    tx_frames: int = 0
    overruns: int = 0
    underruns: int = 0
    last_error: str = ""


@dataclass
class SchedulerMetrics:
    frames: int = 0
    repeats: int = 0
    underruns: int = 0
    overruns: int = 0
    started: float = 0.0

    @property
    def uptime(self) -> float:
        return time.time() - self.started if self.started else 0.0

    @property
    def fps(self) -> float:
        u = self.uptime
        return self.frames / u if u > 0 else 0.0

# Waveform pipeline  (waveform_pipeline.py)

@dataclass
class WaveformMetadata:
    source: str
    path: str = ""
    sample_rate: float = 0.0
    duration: float = 0.0
    peak: float = 0.0


# User-selectable byte-order interpretation for Epiq files (logged as
# [FILE_IQORDER]). "auto" = trust the extension (.g40iq/.x40iq = I in the high
# half of each LE uint32, the codebase writer convention); "iq"/"qi" force that
# interpretation.
_FILE_IQ_ORDER = "auto"

def set_file_iq_order(choice) -> None:
    global _FILE_IQ_ORDER
    c = str(choice).lower()
    _FILE_IQ_ORDER = c if c in ("auto", "iq", "qi") else "auto"
    print(f"[FILE_IQORDER] file IQ-order interpretation: {_FILE_IQ_ORDER}")

def resolve_file_iq_order(ext: str, choice: str):
    """Returns (qi_flag_for_loader, reason). 'qi' means I in the low half."""
    ext_qi = ext.endswith("qi")
    if choice == "auto":
        return ext_qi, f"extension {ext}"
    forced_qi = (choice == "qi")
    if forced_qi != ext_qi:
        return forced_qi, f"user override ({choice.upper()}) of extension {ext}"
    return forced_qi, f"user selection {choice.upper()}"


class WaveformLoader:
    """Loads .bin (complex64) and .wav (stereo IQ / mono real) waveforms."""

    @staticmethod
    def load(path: str, sample_rate: float) -> Tuple[np.ndarray, WaveformMetadata]:
        p = Path(path)
        if not p.exists():
            raise WaveformError(f"file not found: {path}")
        ext = p.suffix.lower()
        if ext in (".bin", ".c64"):
            data = WaveformLoader._load_bin(p)
        elif ext in (".g40iq", ".g40qi", ".x40iq", ".x40qi"):
            _qi, _why = resolve_file_iq_order(ext, _FILE_IQ_ORDER)
            print(f"[FILE_IQORDER] {p.name}: reading as "
                  f"{'QI (I=lo half)' if _qi else 'IQ (I=hi half)'} ({_why})")
            data = WaveformLoader._load_epiq_u32(
                p, bits=(14 if ext in (".x40iq", ".x40qi") else 16), qi=_qi)
        elif ext == ".wav":
            data = WaveformLoader._load_wav(p)
        elif ext in _BLUE_EXTS:
            from dpmg.dsp import cdif_reader                        # Midas BLUE / Platinum I/Q
            data, _cm = cdif_reader.read_cdif(str(p))
        else:
            raise WaveformError(f"unsupported waveform extension: {ext}")
        meta = WaveformMetadata(
            source="file", path=str(p), sample_rate=sample_rate,
            duration=(len(data) / sample_rate) if sample_rate else 0.0,
            peak=float(np.max(np.abs(data))) if len(data) else 0.0,
        )
        return data.astype(np.complex64), meta

    @staticmethod
    def _load_wav(p: Path) -> np.ndarray:
        with wave.open(str(p), "rb") as w:
            ch, sw, fr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            raw = np.frombuffer(w.readframes(n), dtype={1: np.int8, 2: np.int16, 4: np.int32}[sw])
        raw = raw.astype(np.float32) / float(np.iinfo(raw.dtype).max if raw.dtype != np.float32 else 1)
        if ch == 2:                       # stereo: I in L, Q in R
            return raw[0::2] + 1j * raw[1::2]
        return raw.astype(np.complex64)   # mono real

    @staticmethod
    def _load_bin(p) -> np.ndarray:
        """DPMG .bin = 16-byte header ('DPMG'+ver+fmt+pad+rate+hdrsize) then
        big-endian int16 interleaved I,Q. Headerless .bin: raw complex64."""
        raw = np.fromfile(p, dtype=np.uint8)
        if raw.size >= 16 and raw[:4].tobytes() == b"DPMG":
            body = raw[16:]
            iq16 = np.frombuffer(body.tobytes(), dtype=">i2").astype(np.float32) / 32768.0
            return (iq16[0::2] + 1j * iq16[1::2]).astype(np.complex64)
        return np.frombuffer(raw.tobytes(), dtype=np.complex64)

    @staticmethod
    def _load_epiq_u32(p, bits: int, qi: bool) -> np.ndarray:
        """Epiq .g40iq/.x40iq: each sample is a little-endian uint32 holding two
        signed ints (I,Q or Q,I). bits=16 (G40, /32767) or 14 (X40, /8191)."""
        words = np.fromfile(p, dtype="<u4")
        lo = (words & 0xFFFF).astype(np.int32)
        hi = ((words >> 16) & 0xFFFF).astype(np.int32)
        lo[lo >= 32768] -= 65536
        hi[hi >= 32768] -= 65536
        i_int = (lo if qi else hi).astype(np.float32)
        q_int = (hi if qi else lo).astype(np.float32)
        scale = 8191.0 if bits == 14 else 32767.0
        return (i_int / scale + 1j * q_int / scale).astype(np.complex64)

# Signal analysis  (signal_analysis.py)

@dataclass
class SpectrumFrame:
    freqs: np.ndarray
    mag_db: np.ndarray
    peaks: List[Tuple[float, float]]      # (freq_hz, mag_db)


class SpectrumAnalyzer:
    def __init__(self, nfft: int = 4096, window: str = "hann"):
        self.nfft = int(nfft)
        self.window = window

    def analyze(self, frame: AnalysisFrame) -> SpectrumFrame:
        x = frame.samples[:self.nfft]
        if len(x) < self.nfft:
            x = np.pad(x, (0, self.nfft - len(x)))
        if self.window == "hann":
            w = np.hanning(len(x))
        elif self.window == "hamming":
            w = np.hamming(len(x))
        elif self.window == "blackman":
            w = np.blackman(len(x))
        else:
            w = np.ones(len(x))
        spec = np.fft.fftshift(np.fft.fft(x * w))
        mag_db = 20.0 * np.log10(np.abs(spec) + 1e-12)
        freqs = np.fft.fftshift(np.fft.fftfreq(self.nfft, 1.0 / frame.sample_rate)) + frame.center_freq
        return SpectrumFrame(freqs, mag_db, self._peaks(freqs, mag_db))

    @staticmethod
    def _peaks(freqs, mag_db, top_n: int = 6, min_sep_bins: int = 16) -> List[Tuple[float, float]]:
        thr = np.median(mag_db) + 6.0
        cand = [i for i in range(1, len(mag_db) - 1)
                if mag_db[i] > thr and mag_db[i] >= mag_db[i - 1] and mag_db[i] >= mag_db[i + 1]]
        cand.sort(key=lambda i: mag_db[i], reverse=True)
        chosen: List[int] = []
        for i in cand:
            if all(abs(i - j) >= min_sep_bins for j in chosen):
                chosen.append(i)
            if len(chosen) >= top_n:
                break
        return [(float(freqs[i]), float(mag_db[i])) for i in sorted(chosen)]


@dataclass
class ConstellationData:
    i: np.ndarray
    q: np.ndarray


class ConstellationExtractor:
    def __init__(self, samples_per_symbol: int = 37):
        self.sps = max(1, int(samples_per_symbol))

    def extract(self, frame: AnalysisFrame, max_points: int = 400) -> ConstellationData:
        offset = self.sps // 2
        syms = frame.samples[offset::self.sps]
        if syms.size == 0:
            return ConstellationData(np.zeros(0, dtype=np.float32), np.zeros(0, dtype=np.float32))
        if len(syms) > max_points:
            syms = syms[:max_points]
        peak = np.max(np.abs(syms)) or 1.0
        syms = syms / peak
        return ConstellationData(np.real(syms), np.imag(syms))


class SignalEstimator:
    """First-order estimates. Good enough for monitoring, not for metrology."""

    @staticmethod
    def estimate_snr(frame: AnalysisFrame) -> float:
        p = np.abs(frame.samples) ** 2
        if len(p) < 8:
            return 0.0
        srt = np.sort(p)
        noise = np.mean(srt[: len(srt) // 4]) + 1e-12
        sig = np.mean(srt[-len(srt) // 4:])
        return float(10.0 * np.log10(sig / noise))

    @staticmethod
    def estimate_frequency_offset(frame: AnalysisFrame) -> float:
        x = frame.samples
        if len(x) < 2:
            return 0.0
        dphi = np.angle(x[1:] * np.conj(x[:-1]))
        return float(np.mean(dphi) / (2 * np.pi) * frame.sample_rate)

    @staticmethod
    def estimate_baud_rate(frame: AnalysisFrame) -> float:
        # crude: zero-crossing rate of the real part as a proxy for symbol activity
        r = np.real(frame.samples)
        if len(r) < 4:
            return 0.0
        zc = np.sum(np.abs(np.diff(np.signbit(r)))) 
        return float(zc / 2.0 / (len(r) / frame.sample_rate))


@dataclass
class ModulationClassification:
    primary: str
    confidence: float
    candidates: List[Tuple[str, float]]


class ModulationClassifier:
    """Heuristic classifier over envelope/phase/spectral features."""

    def classify(self, frame: AnalysisFrame, spectrum: Optional[SpectrumFrame] = None) -> ModulationClassification:
        x = frame.samples
        if len(x) < 16:
            return ModulationClassification("UNKNOWN", 0.0, [])
        env = np.abs(x)
        env_cv = float(np.std(env) / (np.mean(env) + 1e-12))     # ~0 means constant envelope
        dphi = np.angle(x[1:] * np.conj(x[:-1]))
        n_peaks = len(spectrum.peaks) if spectrum else 0

        scores = {"FSK": 0.0, "GFSK": 0.0, "PSK": 0.0, "QPSK": 0.0, "ASK/OOK": 0.0}
        if env_cv > 0.5:
            scores["ASK/OOK"] += 2.0
        else:
            # constant-envelope family
            if n_peaks >= 2:
                scores["FSK"] += 2.0
                scores["GFSK"] += 1.0
            # phase-jump structure for PSK family
            hist, _ = np.histogram(np.mod(dphi, 2 * np.pi), bins=8)
            concentration = float(np.max(hist) / (np.sum(hist) + 1e-12))
            if concentration > 0.35:
                scores["PSK"] += 1.0
                scores["QPSK"] += 1.0
        total = sum(scores.values()) or 1.0
        ranked = sorted(((k, v / total) for k, v in scores.items()), key=lambda kv: kv[1], reverse=True)
        primary, conf = ranked[0]
        if conf == 0.0:
            primary = "UNKNOWN"
        return ModulationClassification(primary, round(conf, 3), [(k, round(v, 3)) for k, v in ranked if v > 0])

# Backends  (b200_sdr_backend.py)

class SDRBackend:
    def __init__(self, config: SDRConfig):
        self.config = config.validate()
        self.health = StreamHealth()

    def open(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        self.health.state = SDRDeviceState.DISCONNECTED

    def recv(self) -> AnalysisFrame:
        raise NotImplementedError

    def send(self, samples: np.ndarray) -> None:
        raise NotImplementedError

    def set_frequency_live(self, hz: float) -> bool:
        """Retune center frequency without re-init. Base: not supported."""
        return False

    def set_tx_frequency_live(self, hz: float) -> bool:
        """Move only the TX LO without re-init; RX stays where it is. A later
        set_frequency_live keeps the new RX/TX gap. Base: not supported."""
        return False

    def set_gain_live(self, index: float) -> bool:
        """Change RX gain without re-init. Base: not supported."""
        return False

    def restart_rx_stream(self) -> bool:
        """Stop + restart the hardware RX stream without a full re-init (resets
        the card's RX FIFO). Base: not supported."""
        return False

    def gps_status(self) -> dict:
        """GPSDO + reference-clock health for the GUI indicator. Base: none."""
        return {"supported": False, "err": "no GPS/ref source on this backend"}

    def gpsdo_locked(self):
        """Fast GPSDO lock for the status heartbeat. None = not GPS-disciplined."""
        return None

    def card_temps(self) -> dict:
        """{card index: degrees C} for the radio cards in use. Base: none."""
        return {}

    def accel(self):
        """(x, y, z) acceleration in g from the RX card. Base: none."""
        return None

class MockBackend(SDRBackend):
    """Synthetic 4-FSK + AWGN source so the pipeline runs with no hardware."""

    def card_temps(self) -> dict:
        """Simulated card temperatures so the GUI can be checked offline."""
        return {0: 41, 1: 43}

    def set_tx_frequency_live(self, hz: float) -> bool:
        self._tx_lo = float(hz)
        return True

    def accel(self):
        """Simulated card at rest, level (1 g on z) with a little noise."""
        import random
        return (random.gauss(0.0, 0.004), random.gauss(0.0, 0.004), 1.0 + random.gauss(0.0, 0.004))

    def open(self) -> None:
        self.health.state = SDRDeviceState.IDLE
        self._seq = 0
        self._phase = 0.0
        self._rf_ts = 0
        self._rng = np.random.default_rng(0)
        self._t0 = time.monotonic()  # Wall-clock anchor
        # Mirror the G40 cross-card rule so the simulator reports it
        ex = dict(getattr(self.config, "extras", None) or {})
        _txr = float(ex.get("tx_sample_rate", 0) or 0)
        _xc = "tx_card" in ex and int(ex.get("tx_card", 0)) != int(ex.get("card", 0))
        self._tx_rate = _txr if (_xc and _txr > 0) else None
        # TX to RX loopback: what send() gets comes back out of recv() at the
        # sample rate, paced to the wall clock, with a little noise and an
        # optional carrier offset. extras tx_loopback / loopback_cfo_hz /
        # loopback_snr_db, or the DPMG_MOCK_TX_LOOPBACK environment flag.
        import os as _os
        self._lb_on = bool(ex.get("tx_loopback")) or _os.environ.get("DPMG_MOCK_TX_LOOPBACK") == "1"
        self._lb = deque()
        self._lb_have = 0
        self._lb_sent = 0
        self._lb_t0 = None
        self._lb_lock = threading.Lock()
        self._lb_phase = 0.0
        self._lb_cfo = float(ex.get("loopback_cfo_hz", 0.0) or 0.0)
        self._lb_snr = ex.get("loopback_snr_db")
        # pad every send to a whole TX block with zeros, as skiq_bridge does on
        # the G40 (extras mock_tx_block or DPMG_MOCK_TX_BLOCK, samples; 0 = off)
        self._lb_block = int(ex.get("mock_tx_block", 0) or _os.environ.get("DPMG_MOCK_TX_BLOCK", 0) or 0)
        if self._lb_block > 0:
            self.tx_block_samples = self._lb_block

    def set_tx_attenuation_qdb(self, qdb: int) -> bool:
        """Accept the live TX attenuation like EpiqBackend (quarter
        dB, 0-167) so a simulated service honours set_tx_atten; stored only."""
        q = int(qdb)
        if not 0 <= q <= 167:
            raise ValueError(f"tx attenuation {q} qdB outside 0..167")
        self.config.tx_gain = q / 4.0
        return True

    def recv(self) -> AnalysisFrame:
        n = self.config.rx_chunk
        fs = self.config.sample_rate
        # Emit samples at the configured rate, like real hardware.
        # Without this the scheduler's drain loop (which batches until the
        # card reports empty) never sees an empty card and synthesizes
        # 2048-chunk mega-batches, starving sched.latest() for seconds.
        # An empty frame = "no data yet", matching EpiqBackend.recv.
        avail = (time.monotonic() - self._t0) * fs - self._rf_ts
        if avail < n:
            return AnalysisFrame(np.empty(0, dtype=np.complex64), fs,
                                 self.config.center_freq, self._seq,
                                 rf_timestamp=self._rf_ts)
        if getattr(self, "_lb_on", False):
            return self._recv_loopback(n, fs)
        tones = np.array([-0.18, -0.06, 0.06, 0.18]) * fs
        sps = max(1, n // 40)
        sig = np.zeros(n, dtype=np.complex64)
        idx = 0
        while idx < n:
            f = self._rng.choice(tones)
            seg = min(sps, n - idx)
            t = np.arange(seg) / fs
            sig[idx:idx + seg] = np.exp(1j * (2 * np.pi * f * t + self._phase))
            self._phase = float(np.angle(sig[idx + seg - 1])) if seg else self._phase
            idx += seg
        noise = (self._rng.standard_normal(n) + 1j * self._rng.standard_normal(n)) * 0.08
        self._seq += 1
        self._rf_ts += n
        self.health.rx_frames += 1
        return AnalysisFrame((sig + noise).astype(np.complex64), fs,
                             self.config.center_freq, self._seq, rf_timestamp=self._rf_ts)


    def _recv_loopback(self, n: int, fs: float) -> AnalysisFrame:
        """One RX chunk made of what TX sent (zeros when TX is quiet)."""
        with self._lb_lock:
            if self._lb_have >= n:
                parts, got = [], 0
                while got < n:
                    blk = self._lb.popleft()
                    take = min(len(blk), n - got)
                    parts.append(blk[:take])
                    if take < len(blk):
                        self._lb.appendleft(blk[take:])
                    got += take
                self._lb_have -= n
                sig = np.concatenate(parts).astype(np.complex64)
            else:
                sig = np.zeros(n, dtype=np.complex64)
        if self._lb_cfo:
            ph = self._lb_phase + 2 * np.pi * self._lb_cfo / fs * np.arange(1, n + 1)
            sig = (sig * np.exp(1j * (ph - 2 * np.pi * self._lb_cfo / fs))).astype(np.complex64)
            self._lb_phase = float(ph[-1] % (2 * np.pi))
        if self._lb_snr is not None and np.any(sig):
            p = float(np.mean(np.abs(sig) ** 2)) or 1e-12
            amp = np.sqrt(p / (10 ** (float(self._lb_snr) / 10)) / 2)
        else:
            amp = 0.01
        noise = (self._rng.standard_normal(n) + 1j * self._rng.standard_normal(n)) * amp
        self._seq += 1
        self._rf_ts += n
        self.health.rx_frames += 1
        return AnalysisFrame((sig + noise).astype(np.complex64), fs,
                             self.config.center_freq, self._seq, rf_timestamp=self._rf_ts)

    def send(self, samples: np.ndarray) -> None:
        if not getattr(self, "_tx_streaming", False):
            _fe_tx_on(self)                         # front end (mock radio, real sequencer)
            self._tx_streaming = True
        _fe_tick(self, len(samples), self.config.sample_rate)   # before the "send" ...
        self.health.tx_frames += 1
        _fe_tick(self, len(samples), self.config.sample_rate)   # ... and after, like EpiqBackend
        if getattr(self, "_lb_on", False):
            fs = float(self._tx_rate or self.config.sample_rate)
            x = np.asarray(samples, dtype=np.complex64)
            if self._lb_block > 0 and len(x) % self._lb_block:
                x = np.concatenate([x, np.zeros(self._lb_block - len(x) % self._lb_block, dtype=np.complex64)])
            if self._tx_rate and abs(fs - self.config.sample_rate) > 0.5:
                n_out = max(1, int(round(len(x) * self.config.sample_rate / fs)))
                x = np.interp(np.arange(n_out) * fs / self.config.sample_rate, np.arange(len(x)), x.real) + \
                    1j * np.interp(np.arange(n_out) * fs / self.config.sample_rate, np.arange(len(x)), x.imag)
                x = x.astype(np.complex64)
            # pace like a DAC: never run ahead of the wall clock by more than a block
            if self._lb_t0 is None:
                self._lb_t0 = time.monotonic()
            self._lb_sent += len(x)
            ahead = self._lb_sent / float(self.config.sample_rate) - (time.monotonic() - self._lb_t0)
            if ahead > 0.05:
                time.sleep(ahead - 0.05)
            with self._lb_lock:
                self._lb.append(x)
                self._lb_have += len(x)
                cap = int(4 * self.config.sample_rate)
                while self._lb_have > cap and self._lb:
                    self._lb_have -= len(self._lb.popleft())

    def stop_tx(self) -> None:
        _fe_tx_off(self)
        self._tx_streaming = False


class _PumpBatchState:
    """The fields of rxdrain.c's rxd_state_t that the scheduler reads, for a
    backend that serves recv_batch from Python (UhdBackend's pump)."""
    __slots__ = ("n_blocks", "empty", "ts0", "n_mismatch", "first_exp", "first_got",
                 "n_back", "last_ts", "last_n")

    def __init__(self):
        self.n_blocks = self.empty = self.ts0 = self.n_mismatch = 0
        self.first_exp = self.first_got = self.n_back = self.last_ts = self.last_n = 0


class UhdBackend(SDRBackend):
    """Ettus USRP via the UHD 4.10 Python API (X410, B2xx, N3xx, ...).

    Not hardware-tested: no Ettus device on hand at build time. Written to the
    UHD 4.10 MultiUSRP API; verify antenna names, gain scale, and device_args
    against the actual unit before trusting it. device_args selects the radio
    (e.g. "type=x4xx,addr=192.168.40.2"); rx/tx gain here are dB (UHD-native),
    unlike the Epiq index scale. Single channel 0 on RX and TX.
    """

    _CHAN = 0
    # Blocks a live TX stream may have built and waiting ahead of the UHD send.
    # Once send() returns UHD holds only a few ms of samples, so building the
    # next block after the send leaves no slack: any slow block (GIL, demod, a
    # pull from the container) starves the radio and it underflows. A sender
    # thread feeds UHD from this queue while the scheduler builds ahead.
    # extras tx_queue_blocks overrides; at 50 ms blocks 3 is 150 ms of lead.
    TX_QUEUE_BLOCKS = 3
    # restrict UHD discovery to the right transport by device type, so an empty
    # device_args never triggers a hanging network-wide probe.
    # USB receive frames UHD keeps in flight (see open()); 0 = UHD default (16).
    # This is the floor; open() sizes it to RECV_BUFFER_S at the running rate,
    # capped by the kernel's usbfs memory limit (16 MB by default on the Orin).
    NUM_RECV_FRAMES = 256
    RECV_FRAME_BYTES = 8192        # B200 USB 3 frame; sc16 on the wire = 4 bytes/sample
    RECV_BUFFER_S = 0.1
    # The drain loop stalls 150-220 ms at a time on the Orin under the service's
    # worker load, longer than the USB buffer, so at 30.72 MSps the B200 overflowed
    # 2-3 times a second. A reader thread (the pump) keeps calling recv() (UHD drops
    # the GIL inside it) into a host ring this deep; the drain reads the ring.
    PUMP_QUEUE_S = 0.3
    # The scheduler drains a pumped backend through recv_batch (the interface the
    # Epiq native drain uses): one call per batch, the ring copied out and
    # converted in one pass. The per-block path cost 38 ms of every 50 ms batch
    # at 61.44 MSps on the Orin Nano (a fresh array per block, then concatenate).
    drain_engine = "pump"
    # B200mini top master clock with one channel
    MAX_MCR = 61.44e6
    # Above 30.72 MSps the AD936x does almost no decimation of its own and its
    # sigma-delta ADC noise climbs toward the band edges: at 61.44 MSps flat
    # within +-15 MHz, +7 dB at +-21 MHz, +15 dB at the edge. Only this fraction
    # of the rate is clean enough for the detector (see usable_bw_hz).
    EDGE_FREE_FRAC = 0.66
    _TYPE_BY_KIND = {"b200": "type=b200", "ettus": "type=b200",
                     "x410": "type=x4xx"}

    @classmethod
    def recv_frames_for(cls, rate: float, usbfs_mb: int = 0) -> int:
        """USB frames for RECV_BUFFER_S of samples at this rate. usbfs_mb is the
        kernel limit on all usbfs buffers (0 = none); stay under 3/4 of it so the
        TX frames still fit."""
        n = max(cls.NUM_RECV_FRAMES,
                int(math.ceil(rate * cls.RECV_BUFFER_S * 4 / cls.RECV_FRAME_BYTES)))
        if usbfs_mb > 0:
            n = min(n, int(usbfs_mb * 1024 * 1024 * 3 // 4 // cls.RECV_FRAME_BYTES))
        return n

    @staticmethod
    def recv_samps_for(rate: float, floor: int) -> int:
        """Samples per recv(): about 1 ms, a power of two, between the configured
        chunk and 65536. At 30.72 MSps a fixed 4096 would mean 7500 Python calls a second."""
        n = 1 << max(0, int(math.ceil(math.log2(max(rate * 1e-3, 1.0)))))
        return int(min(65536, max(int(floor), n)))

    @staticmethod
    def auto_lo_offset(rate: float, mcr: float) -> float:
        """LO offset that puts the LO spur just outside the received band: half the
        rate plus a guard. 0 when the master clock has no room for it (the shifted
        band has to stay inside 0.4 x the master clock, the codec's usable band)."""
        lo = rate / 2 + max(0.1 * rate, 100e3)
        return lo if mcr > 0 and lo + rate / 2 <= 0.4 * mcr else 0.0

    # the share of the band the GUI treats as usable (caps usable_bw_factor)
    USABLE_BW_FACTOR = 0.8

    @classmethod
    def offset_room(cls, rate: float, lo: float, mcr: float) -> bool:
        """True when an LO offset of lo Hz has room at this master clock: the
        usable band stays inside 0.42 x the master clock and the whole band
        below its Nyquist edge, so nothing folds over. Without room the FPGA
        shift wraps part of the band onto the wrong frequencies."""
        a = abs(float(lo))
        return (mcr > 0 and a + cls.USABLE_BW_FACTOR * rate / 2 <= 0.42 * mcr
                and a + rate / 2 < 0.5 * mcr)

    @classmethod
    def _mcr_multiples(cls, rate: float):
        """Master clocks that keep this sample rate exact: whole multiples
        k x rate (k >= 2) up to MAX_MCR, largest first (more decimation)."""
        k = int((cls.MAX_MCR + 1.0) // rate) if rate > 0 else 0
        return [k_ * rate for k_ in range(k, 1, -1)]

    @classmethod
    def mcr_with_room(cls, rate: float, lo: float) -> float:
        """A master clock from _mcr_multiples with room for lo, or 0."""
        return next((m for m in cls._mcr_multiples(rate)
                     if cls.offset_room(rate, lo, m)), 0.0)

    @classmethod
    def best_lo_offset(cls, rate: float, sign: int = 1):
        """(offset_hz, master_clock_hz, where) for the GUI's Best LO offset
        button. where is "outside" (the leak lands outside the sampled band,
        up to about 20 MSps), "guard" (in the roll-off strip just past the
        usable band, 25 to 30.72 MSps; measured flat there at 60 dB RX gain),
        or "none" (from 40 MSps the master clock cannot reach twice the rate,
        so there is no room and the offset stays 0)."""
        s = -1.0 if sign < 0 else 1.0
        lo = rate / 2 + max(0.1 * rate, 100e3)
        m = next((m for m in cls._mcr_multiples(rate) if cls.auto_lo_offset(rate, m)), 0.0)
        if m:
            return s * lo, m, "outside"
        lo = cls.USABLE_BW_FACTOR * rate / 2 + 0.5e6
        m = cls.mcr_with_room(rate, lo)
        if m:
            return s * lo, m, "guard"
        return 0.0, 0.0, "none"

    def _set_mcr(self, mcr: float, rate: float) -> bool:
        try:
            self.usrp.set_master_clock_rate(float(mcr))
            self.usrp.set_rx_rate(rate, self._CHAN)
            return True
        except Exception as e:
            print(f"[UHD] master clock {mcr / 1e6:.2f} MHz not set for the LO offset: {e}",
                  flush=True)
            return False

    @staticmethod
    def _usbfs_mb() -> int:
        try:
            with open("/sys/module/usbcore/parameters/usbfs_memory_mb") as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            return 16

    def _mcr(self) -> float:
        try:
            return float(self.usrp.get_master_clock_rate())
        except Exception:
            return 0.0

    def open(self) -> None:
        self.health.state = SDRDeviceState.CONNECTING
        # UHD reads its config via get_xdg_config_home() during device
        # discovery; under a bare systemd/service environment HOME is unset and
        # discovery raises "Unable to find $HOME or $XDG_CONFIG_HOME". Supply a
        # fallback so the backend works no matter how the service was launched.
        import os as _os
        if not _os.environ.get("HOME"):
            try:
                import pwd
                _os.environ["HOME"] = pwd.getpwuid(_os.getuid()).pw_dir
            except Exception:
                _os.environ["HOME"] = "/tmp"
        _os.environ.setdefault("XDG_CONFIG_HOME",
                               _os.path.join(_os.environ["HOME"], ".config"))
        try:
            import uhd
        except ImportError as e:
            self.health.state = SDRDeviceState.ERROR
            raise SDRConnectionError(
                "UHD Python module not installed (pip/uhd 4.10)") from e
        cfg = self.config
        try:
            self._uhd = uhd
            # Default device_args to the radio type so UHD probes only that
            # transport. An empty string makes MultiUSRP run full multi-
            # transport discovery: including UDP broadcast probes on every
            # network interface: slow/unreliable on hosts with extra
            # interfaces (e.g. a Jetson's wifi + USB-gadget bridge). GUI sends "".
            dev_args = cfg.device_args or self._TYPE_BY_KIND.get(
                getattr(self, "_kind", ""), "type=b200")
            # Host-side USB receive buffering. UHD's B200 default is 16 frames
            # of 8192 bytes (~32k samples: 4 ms at 7.68 MSps, 16 ms at 2 MSps).
            # The scheduler drain stalls recv() for 4-8 ms at the end of every
            # 50 ms batch (concatenating the batch on the Jetson), so at
            # >= ~5 MSps each stall overflowed the radio ("O", a spliced frame,
            # ~0.2/s in the scheduler probe, more under the service's worker
            # load). 256 frames buffer ~68 ms at 7.68 MSps (2 MB of USB
            # buffers). Sized to the rate by recv_frames_for. extras
            # ["num_recv_frames"] overrides (0 = UHD default); device_args that
            # already name it win.
            ex = cfg.extras or {}
            if "num_recv_frames" not in dev_args:
                nrf = int(ex.get("num_recv_frames",
                                 self.recv_frames_for(float(cfg.sample_rate), self._usbfs_mb())))
                if nrf > 0:
                    dev_args = (f"{dev_args},num_recv_frames={nrf}" if dev_args
                                else f"num_recv_frames={nrf}")
            # UHD starts its USB streaming threads when the device opens, so
            # anything new from here to the stream start is UHD's (see _start_pump)
            tids_before = self._tids()
            self.usrp = uhd.usrp.MultiUSRP(dev_args)
            # external 10 MHz / 1 PPS discipline when requested. The B200mini has
            # no onboard GPSDO (only internal/external). The 10 MHz frequency
            # reference (clock_source) and the 1 PPS time reference (time_source)
            # are independent: feed one, the other, or both. clock_source comes
            # from cfg; time_source via extras["time_source"] (default follows
            # clock_source for a combined GPSDO feed).
            ex = cfg.extras or {}
            def _set_ref(kind, val):
                val = (val or "internal").lower()
                if val in ("", "internal"):
                    return
                try:
                    avail = list(getattr(self.usrp, f"get_{kind}_sources")(self._CHAN))
                    pick = val if val in avail else ("external" if "external" in avail else None)
                    if pick:
                        getattr(self.usrp, f"set_{kind}_source")(pick)
                except Exception as e:
                    print(f"[UHD] {kind}_source {val!r} not applied: {e}", flush=True)
            _clk = (cfg.clock_source or "internal").lower()
            _set_ref("clock", _clk)                               # 10 MHz freq ref
            _set_ref("time", ex.get("time_source", _clk))         # 1 PPS time ref
            rate = float(cfg.sample_rate)
            self.usrp.set_rx_rate(rate, self._CHAN)
            self.usrp.set_rx_rate(rate, self._CHAN)
            # LO offset: tune the analog LO off-center and let the FPGA shift
            # back, which moves the LO spur (about 10 dB over the floor on the
            # B200, the G40 has none) away from center. 0, which the GUI sends
            # by default, means auto: just outside the band when a master clock
            # of a whole multiple of the rate leaves room (up to about 20 MSps),
            # else at center. A non-zero lo_offset_hz (typed, or the GUI's Best
            # button) is used as given, raising the master clock to a whole
            # multiple of the rate when the current one has no room; if no
            # clock up to 61.44 MHz has room the LO stays at center, since a
            # shift without room folds part of the band onto the wrong
            # frequencies. extras lo_offset_auto=False keeps auto at 0.
            lo_off = float(ex.get("lo_offset_hz", 0) or 0)
            mcr = self._mcr()
            own_mcr = "master_clock_rate" not in dev_args
            if lo_off == 0 and ex.get("lo_offset_auto", True):
                lo_off = self.auto_lo_offset(rate, mcr)
                if not lo_off and own_mcr:
                    _lo, _m, _where = self.best_lo_offset(rate)
                    if _where == "outside" and self._set_mcr(_m, rate):
                        mcr = self._mcr()
                        lo_off = self.auto_lo_offset(rate, mcr)
            elif lo_off and not self.offset_room(rate, lo_off, mcr):
                _m = self.mcr_with_room(rate, lo_off) if own_mcr else 0.0
                if _m and self._set_mcr(_m, rate):
                    mcr = self._mcr()
                if not self.offset_room(rate, lo_off, mcr):
                    print(f"[UHD] LO offset {lo_off / 1e6:+.3f} MHz has no room at "
                          f"{rate / 1e6:g} MSps (master clock {mcr / 1e6:.2f} MHz); "
                          f"LO left at center", flush=True)
                    lo_off = 0.0
            self._lo_off = lo_off
            tr = (uhd.types.TuneRequest(float(cfg.center_freq), lo_off) if lo_off
                  else uhd.types.TuneRequest(float(cfg.center_freq)))
            self._log_tune("RX", self.usrp.set_rx_freq(tr, self._CHAN), mcr)
            self.usrp.set_rx_gain(float(cfg.rx_gain), self._CHAN)
            self._set_antenna("rx", cfg.rx_antenna)
            # analog RX bandwidth (0 = leave UHD's rate-derived default)
            rx_bw = float(ex.get("rx_bw_hz", 0) or 0)
            if rx_bw > 0:
                try:
                    self.usrp.set_rx_bandwidth(rx_bw, self._CHAN)
                except Exception as _bwe:
                    print(f"[UHD] set_rx_bandwidth({rx_bw}) skipped: {_bwe}", flush=True)
            # DC-offset + IQ-imbalance auto-correction (default on: nulls the
            # LO/DC spur and image). Toggle off via extras to see the raw front end.
            try:
                self.usrp.set_rx_dc_offset(bool(ex.get("dc_offset", True)), self._CHAN)
                self.usrp.set_rx_iq_balance(bool(ex.get("iq_balance", True)), self._CHAN)
            except Exception as _qe:
                print(f"[UHD] DC/IQ auto-correct not applied: {_qe}", flush=True)
            # TX is best-effort; a receive-only run must still open.
            try:
                self.usrp.set_tx_rate(float(cfg.sample_rate), self._CHAN)
                # the AD9364 has its own TX synthesizer: honor an independent
                # TX center (extras tx_center_freq), else co-tune with RX
                tx_hz = float(ex.get("tx_center_freq", 0) or cfg.center_freq)
                tx_tr = (uhd.types.TuneRequest(tx_hz, lo_off) if lo_off
                         else uhd.types.TuneRequest(tx_hz))
                self.usrp.set_tx_freq(tx_tr, self._CHAN)
                self._tx_lo = tx_hz
                self.usrp.set_tx_gain(float(cfg.tx_gain), self._CHAN)
                self._set_antenna("tx", cfg.tx_antenna)
                tx_bw = float(ex.get("tx_bw_hz", 0) or 0)
                if tx_bw > 0:
                    self.usrp.set_tx_bandwidth(tx_bw, self._CHAN)
            except Exception as _txe:
                print(f"[UHD] TX config skipped: {_txe}", flush=True)

            # With the pump, RX hands over raw sc16 (UHD only copies it) and the
            # drain converts to complex64 once per batch: at 61.44 MSps UHD's
            # sc16 to fc32 conversion inside recv() was what kept the pump from
            # keeping up (82-89% of the samples, overflows at any buffer size).
            pump = bool(ex.get("rx_pump", True))
            rx_st = uhd.usrp.StreamArgs("sc16" if pump else "fc32", "sc16")
            rx_st.channels = [self._CHAN]
            self._rx_stream = self.usrp.get_rx_stream(rx_st)
            st = uhd.usrp.StreamArgs("fc32", "sc16")   # host complex64, wire sc16
            st.channels = [self._CHAN]
            self._tx_stream = self.usrp.get_tx_stream(st)
            self.tx_queue_blocks = max(1, int(ex.get("tx_queue_blocks", self.TX_QUEUE_BLOCKS)))
            self._recv_n = int(ex.get("recv_samps", self.recv_samps_for(rate, cfg.rx_chunk)))
            self._rx_buf = np.zeros((1, self._recv_n), dtype=np.complex64)
            self._rx_md = uhd.types.RXMetadata()

            cmd = uhd.types.StreamCMD(uhd.types.StreamMode.start_cont)
            cmd.stream_now = True
            self._rx_stream.issue_stream_cmd(cmd)
            self._uhd_stream_tids = self._tids() - tids_before
            self._seq = 0
            self._rf_ts = 0
            self._achieved_rate = float(self.usrp.get_rx_rate(self._CHAN))
            self.config.sample_rate = self._achieved_rate
            self._pump = None
            if pump:
                self._start_pump(self._achieved_rate)
            self.health.state = SDRDeviceState.STREAMING
        except SDRConnectionError:
            raise
        except Exception as e:
            self.health.state = SDRDeviceState.ERROR
            raise SDRConnectionError(f"UHD open failed: {e}") from e

    def _set_antenna(self, direction: str, want: str) -> None:
        """Select an antenna, tolerating labels from other radio families.

        The ground GUI offers Sidekiq port names (e.g. "TRx1A"); UHD devices
        have their own names ("RX2", "TX/RX"). If the requested name is not a
        valid UHD antenna, fall back to the device default rather than failing
        the whole open: a B200mini has one RX and one TX antenna anyway.
        """
        if not want:
            return
        getter = getattr(self.usrp, f"get_{direction}_antennas")
        setter = getattr(self.usrp, f"set_{direction}_antenna")
        try:
            valid = list(getter(self._CHAN))
        except Exception:
            valid = []
        if valid and want not in valid:
            # GUI sends Sidekiq port names (e.g. "TRx1A"); map to the sensible
            # UHD default for the direction: RX2 for receive, TX/RX for
            # transmit: which also matches a TX/RX to pad to RX2 loopback.
            prefer = "RX2" if direction == "rx" else "TX/RX"
            want = prefer if prefer in valid else valid[0]
            print(f"[UHD] {direction.upper()} antenna not in {valid}; using {want!r}",
                  flush=True)
        try:
            setter(want, self._CHAN)
        except Exception as e:
            print(f"[UHD] set_{direction}_antenna({want!r}) skipped: {e}", flush=True)

    def _log_tune(self, side: str, res, mcr: float) -> None:
        """One line per tune so the log shows where the LO really went."""
        lo = getattr(self, "_lo_off", 0.0)
        try:
            print(f"[UHD] {side} tune: lo offset {lo / 1e6:+.3f} MHz, master clock "
                  f"{mcr / 1e6:.2f} MHz, rf {res.actual_rf_freq / 1e6:.6f} MHz, "
                  f"dsp {res.actual_dsp_freq / 1e6:+.6f} MHz", flush=True)
        except Exception:
            print(f"[UHD] {side} tune: lo offset {lo / 1e6:+.3f} MHz", flush=True)

    # At 61.44 MSps the pump and UHD's own streaming thread (the one that moves
    # USB packets) each use about 75% of an Orin Nano core. Left to the
    # scheduler they kept landing on a core with each other or with the
    # detector, and the radio overflowed 1 to 4 times a second in most runs;
    # on cores of their own, 0 in 30 s soaks. extras rx_pin_cpus=False skips it.
    @staticmethod
    def plan_cores(cpus) -> Optional[dict]:
        """Which cores to give UHD's streaming threads, the pump and the drain,
        with the rest for the other threads. None with fewer than 4 cores."""
        cores = sorted(set(int(c) for c in cpus))
        if len(cores) < 4:
            return None
        return {"io": cores[-2], "pump": cores[-1], "drain": cores[-3],
                "rest": cores[:-3]}

    @staticmethod
    def _cpuset() -> list:
        """Cores this container may use. Not sched_getaffinity: after one open
        the calling thread is already limited to the 'rest' cores."""
        try:
            with open("/sys/fs/cgroup/cpuset.cpus.effective") as f:
                txt = f.read().strip()
            out = []
            for part in txt.split(","):
                lo, _, hi = part.partition("-")
                out.extend(range(int(lo), int(hi or lo) + 1))
            if out:
                return out
        except (OSError, ValueError):
            pass
        return list(range(os.cpu_count() or 1))

    @staticmethod
    def _tids() -> set:
        try:
            return {int(t) for t in os.listdir("/proc/self/task")}
        except (OSError, ValueError):
            return set()

    @staticmethod
    def _pin(tid: int, cpus) -> bool:
        try:
            os.sched_setaffinity(tid, set(cpus))
            return True
        except (AttributeError, OSError):
            return False

    def _start_pump(self, rate: float) -> None:
        n = self._recv_n
        nblk = max(8, int(math.ceil(self.PUMP_QUEUE_S * rate / n)))
        # One circular buffer, one uint32 per sample (UHD's sc16: int16 I then Q).
        # The pump writes at _wpos and only moves that counter on; the reader
        # keeps its own _rpos. Both count samples from the start of streaming, so
        # _wpos - _rpos is the backlog and a sample's count is its rf_timestamp.
        # A queue of blocks cost two lock hand-offs per 1 ms block through the
        # GIL at 61.44 MSps and the drain still fell behind in some runs; this
        # way the drain touches the ring a few times per 50 ms batch.
        self._ring = np.empty(nblk * n, dtype=np.uint32)
        self._wpos = self._rpos = 0
        self._pump_stop = threading.Event()
        self._host_drops = 0
        self._radio_ovf = 0
        self._last_pop_t = 0.0
        self._cores = None
        self._drain_pinned = False
        if (self.config.extras or {}).get("rx_pin_cpus", True):
            self._cores = self.plan_cores(self._cpuset())
        if self._cores:
            c = self._cores
            uhd_tids = getattr(self, "_uhd_stream_tids", set())
            for tid in uhd_tids:
                self._pin(tid, [c["io"]])
            for tid in self._tids() - uhd_tids:
                self._pin(tid, c["rest"])     # threads started later inherit this
            print(f"[UHD] cores: UHD streaming threads {c['io']}, pump {c['pump']}, "
                  f"drain {c['drain']}, everything else {c['rest']}", flush=True)
        self._pump = threading.Thread(target=self._pump_loop, name="uhd-rx-pump",
                                      daemon=True)
        self._pump.start()

    def _pump_loop(self) -> None:
        try:
            os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(20))
        except (AttributeError, PermissionError, OSError):
            pass
        if self._cores:
            self._pin(0, [self._cores["pump"]])
        ovf = self._uhd.types.RXMetadataErrorCode.overflow
        md = self._uhd.types.RXMetadata()
        stream, ring = self._rx_stream, self._ring
        cap, n_max = ring.size, self._recv_n
        # every 5 s: the longest gap between recv() returns (a GIL or scheduling
        # stall shows up here) and where samples were lost
        t_prev = t_rep = time.monotonic()
        max_gap, d0, o0 = 0.0, 0, 0
        while not self._pump_stop.is_set():
            off = self._wpos % cap
            try:
                n = stream.recv(ring[None, off:off + min(n_max, cap - off)], md, 0.1)
            except Exception as e:
                self.health.last_error = f"UHD RX: {e}"
                time.sleep(0.01)
                continue
            now = time.monotonic()
            max_gap = max(max_gap, now - t_prev)
            t_prev = now
            if now - t_rep >= 5.0:
                if self._radio_ovf - o0 or self._host_drops - d0 or max_gap > 0.02:
                    print(f"[PUMP] 5 s: longest recv gap {max_gap * 1e3:.0f} ms, radio "
                          f"overflows {self._radio_ovf - o0}, ring drops "
                          f"{self._host_drops - d0}", flush=True)
                t_rep, max_gap, d0, o0 = now, 0.0, self._host_drops, self._radio_ovf
            if md.error_code == ovf:
                self.health.overruns += 1
                self._radio_ovf += 1
            if n > 0:
                self._wpos += n

    def _stop_pump(self) -> None:
        p = getattr(self, "_pump", None)
        if p is not None:
            self._pump_stop.set()
            p.join(timeout=1.0)
            self._pump = None

    def native_drain_ok(self) -> bool:
        return getattr(self, "_pump", None) is not None

    def _take(self, limit: int):
        """Samples written since the last read, up to limit, as complex64 (one
        pass from the int16 ring, scaled like UHD's sc16 to fc32: 32767 is full
        scale). Returns (samples or None, first sample's count, old read count
        when a stretch had to be skipped, else None)."""
        ring, cap, n_max = self._ring, self._ring.size, self._recv_n
        jump = None
        if self._wpos - self._rpos > cap // 2:
            # the reader fell half a ring behind: skip to recent samples, well
            # clear of the part the pump is overwriting
            jump = self._rpos
            self._rpos = self._wpos - cap // 4
            self._host_drops += 1
            self.health.overruns += 1
        r = self._rpos
        m = min(self._wpos - r, int(limit))
        if m <= 0:
            return None, r, jump
        out = np.empty(2 * m, dtype=np.float32)
        i16 = ring.view(np.int16)
        a = r % cap
        k = min(m, cap - a)
        np.multiply(i16[2 * a:2 * (a + k)], 1.0 / 32767.0, out=out[:2 * k], casting="unsafe")
        if k < m:
            np.multiply(i16[:2 * (m - k)], 1.0 / 32767.0, out=out[2 * k:], casting="unsafe")
        if self._wpos + n_max - cap > r:     # lapped while copying: start is garbage
            self._host_drops += 1
            self.health.overruns += 1
        self._rpos = r + m
        return out.view(np.complex64), r, jump

    def recv_batch(self, max_blocks: int, batch_s: float):
        """About batch_s of samples as one complex64 array plus a state record
        shaped like the Epiq native drain's (rxd_state_t), so the scheduler takes
        a whole batch in one call. A skipped stretch shows as one ts mismatch."""
        now = time.time()
        if now - self._last_pop_t > 0.5:
            self._rpos = self._wpos            # stale after STOP RX or a pause
        st = _PumpBatchState()
        want = int(batch_s * self._achieved_rate)
        t_end = now + batch_s
        while self._wpos - self._rpos < want and time.time() < t_end:
            time.sleep(0.002)
        if self._cores and not self._drain_pinned:
            self._drain_pinned = self._pin(0, [self._cores["drain"]])
        chunk, r, jump = self._take(max_blocks * self._recv_n)
        self._last_pop_t = time.time()
        if chunk is None:
            st.empty = 1
            return None, st
        m, n_max = chunk.size, self._recv_n
        st.n_blocks = max(1, -(-m // n_max))
        st.ts0 = r
        st.last_n = m - (st.n_blocks - 1) * n_max
        st.last_ts = r + m - st.last_n
        if jump is not None:
            st.n_mismatch, st.first_exp, st.first_got = 1, jump, r
        self._seq += st.n_blocks
        self.health.rx_frames += st.n_blocks
        return chunk, st

    @staticmethod
    def _sc16_to_c64(raw: np.ndarray) -> np.ndarray:
        """uint32-per-sample sc16 (int16 I then Q) as complex64 scaled the way
        UHD's own sc16 to fc32 conversion does (full scale 32767 is 1.0)."""
        f = raw.view(np.int16).astype(np.float32)
        f *= 1.0 / 32767.0
        return f.view(np.complex64)

    def _recv_pumped(self) -> AnalysisFrame:
        now = time.time()
        if now - self._last_pop_t > 0.5:
            # first read after STOP RX or a long pause: start from fresh samples
            self._rpos = self._wpos
        t_end = now + 0.2
        while self._wpos <= self._rpos and time.time() < t_end:
            time.sleep(0.001)
        chunk, r, _jump = self._take(self._recv_n)
        self._last_pop_t = time.time()
        if chunk is None:
            return AnalysisFrame(np.empty(0, dtype=np.complex64), self._achieved_rate,
                                 self.config.center_freq, self._seq, rf_timestamp=0)
        self._seq += 1
        self.health.rx_frames += 1
        return AnalysisFrame(chunk, self._achieved_rate, self.config.center_freq,
                             self._seq, rf_timestamp=r)

    def recv(self) -> AnalysisFrame:
        if getattr(self, "_pump", None) is not None:
            return self._recv_pumped()
        cfg = self.config
        # An overflow makes UHD return 0 samples with error_code OVERFLOW. The
        # scheduler's drain treats an empty recv as "card idle" and sleeps
        # rx_pace_s (50 ms): longer than the B200's host buffer at low rates,
        # so that sleep guarantees the next overflow: one transient becomes a
        # self-sustaining ~40 overflows/s with every frame spliced (2 and
        # 7.68 MSps). Count it and read again instead.
        ovf = self._uhd.types.RXMetadataErrorCode.overflow
        n = 0
        for _ in range(4):
            n = self._rx_stream.recv(self._rx_buf, self._rx_md, 1.0)
            if self._rx_md.error_code == ovf:
                self.health.overruns += 1
                if n <= 0:
                    continue
            break
        if n <= 0:
            return AnalysisFrame(np.empty(0, dtype=np.complex64),
                                 self._achieved_rate, cfg.center_freq,
                                 self._seq, rf_timestamp=self._rf_ts)
        self._seq += 1
        # rf_timestamp is the start sample index of this block (== cumulative
        # count before adding n), matching the Epiq/skiq hardware-timestamp
        # semantics the drain's RXGAP check and FrameAccumulator's contiguity
        # gate both assume (ts_i == ts_{i-1} + n_{i-1}). Reporting the post-add
        # end index instead makes ts_i depend on this block's own size, so with
        # UHD's variable samples-per-recv the batched drain tags each frame
        # with a non-contiguous ts, FrameAccumulator.push() clears its buffer
        # every batch, never reaches frame_len, and the channelizer decodes
        # nothing on the B200 (detections through the separate _grab_rx path
        # still work, which hides it). Constant-size backends (Mock) are not affected.
        ts0 = self._rf_ts
        self._rf_ts += n
        self.health.rx_frames += 1
        return AnalysisFrame(self._rx_buf[0, :n].copy(), self._achieved_rate,
                             cfg.center_freq, self._seq, rf_timestamp=ts0)

    def send(self, samples: np.ndarray) -> None:
        # Each send is a whole burst (start and end), which suits a repeated
        # waveform. A live stream (tx_stream_mode, set by the scheduler while a
        # TX source feeds it) is one long burst instead: start of burst on the
        # first send only and no end of burst between sends, or every seam
        # becomes a gap; end_tx_burst() closes it when the stream stops.
        # Stream blocks go through a short queue to a sender thread (see
        # TX_QUEUE_BLOCKS), so this returns as soon as there is room.
        if bool(getattr(self, "tx_stream_mode", False)):
            self._queue_tx(samples)
            return
        if getattr(self, "_tx_sender", None) is not None or getattr(self, "_tx_burst_open", False):
            self.end_tx_burst()                 # a repeated waveform after a stream
        try:
            md = self._uhd.types.TXMetadata()
            md.start_of_burst = True
            md.end_of_burst = True
            buf = np.ascontiguousarray(samples, dtype=np.complex64).reshape(1, -1)
            self._tx_stream.send(buf, md, 1.0)
            self.health.tx_frames += 1
        except Exception as e:
            self.health.last_error = f"UHD TX: {e}"
        self._poll_tx_async()

    def _queue_tx(self, samples: np.ndarray) -> None:
        t = getattr(self, "_tx_sender", None)
        if t is None or not t.is_alive():
            n = int(getattr(self, "tx_queue_blocks", self.TX_QUEUE_BLOCKS))
            self._tx_q = queue.Queue(maxsize=max(1, n))
            t = self._tx_sender = threading.Thread(
                target=self._tx_send_loop, args=(self._tx_q,), name="uhd-tx", daemon=True)
            t.start()
        # a copy: the source may build the next block in the same buffer
        buf = np.array(samples, dtype=np.complex64, copy=True).reshape(1, -1)
        while True:                             # blocks while the queue is full,
            try:                                # which paces the source to the radio
                self._tx_q.put(buf, timeout=0.5)
                return
            except queue.Full:
                if not t.is_alive():
                    self.health.last_error = "UHD TX: sender thread stopped"
                    return

    def _tx_send_loop(self, q: "queue.Queue") -> None:
        while True:
            buf = q.get()
            if buf is None:                     # end_tx_burst
                break
            try:
                md = self._uhd.types.TXMetadata()
                md.start_of_burst = not getattr(self, "_tx_burst_open", False)
                md.end_of_burst = False
                self._tx_stream.send(buf, md, 1.0)
                self._tx_burst_open = True
                self.health.tx_frames += 1
            except Exception as e:
                self.health.last_error = f"UHD TX: {e}"
            self._poll_tx_async()
        self._send_end_of_burst()

    def _send_end_of_burst(self) -> None:
        if not getattr(self, "_tx_burst_open", False):
            return
        self._tx_burst_open = False
        try:
            md = self._uhd.types.TXMetadata()
            md.start_of_burst = False
            md.end_of_burst = True
            self._tx_stream.send(np.zeros((1, 64), dtype=np.complex64), md, 1.0)
        except Exception as e:
            self.health.last_error = f"UHD TX end of burst: {e}"

    def end_tx_burst(self) -> None:
        """Send what is queued, then close a streamed burst with a short run of
        zeros flagged end of burst."""
        t, q = getattr(self, "_tx_sender", None), getattr(self, "_tx_q", None)
        self._tx_sender = None
        if t is not None and t.is_alive() and q is not None:
            try:
                q.put(None, timeout=2.0)
                t.join(timeout=5.0)
            except queue.Full:
                self.health.last_error = "UHD TX: sender did not drain"
            if t.is_alive():
                self.health.last_error = "UHD TX: sender did not stop"
                return
        else:
            self._send_end_of_burst()
        self._poll_tx_async()

    def _poll_tx_async(self) -> None:
        """Count UHD's async TX reports. Underflows are the U marks UHD prints;
        unread, the service would report 0 underruns while the radio starves.
        Timeout 0, so this never waits."""
        st = getattr(self, "_tx_stream", None)
        if st is None or getattr(self, "_tx_async_off", False):
            return
        try:
            types_ = self._uhd.types
            codes = getattr(self, "_tx_async_codes", None)
            if codes is None:
                ev = types_.TXMetadataEventCode
                codes = self._tx_async_codes = (
                    {int(ev.underflow), int(ev.underflow_in_packet)},
                    {int(ev.seq_error), int(ev.seq_error_in_packet)},
                    {int(ev.time_error)})
            for _ in range(256):
                md = types_.TXAsyncMetadata()
                if not st.recv_async_msg(md, 0.0):
                    break
                code = int(md.event_code)
                if code in codes[0]:
                    self.tx_underflows = getattr(self, "tx_underflows", 0) + 1
                    self.health.underruns += 1
                elif code in codes[1]:
                    self.tx_seq_errors = getattr(self, "tx_seq_errors", 0) + 1
                elif code in codes[2]:
                    self.tx_time_errors = getattr(self, "tx_time_errors", 0) + 1
        except Exception as e:                  # an API without async reports
            self._tx_async_off = True
            self._tx_async_err = f"{type(e).__name__}: {e}"
            self.health.last_error = f"UHD TX async reports off: {e}"

    def set_frequency_live(self, hz: float) -> bool:
        # keep open()'s LO offset; a plain TuneRequest here puts the LO (and its
        # spur) back at center on every retune
        lo = getattr(self, "_lo_off", 0.0)
        types_ = self._uhd.types
        tr = (lambda: types_.TuneRequest(float(hz), lo)) if lo else \
             (lambda: types_.TuneRequest(float(hz)))
        old = float(self.config.center_freq)
        try:
            self._log_tune("RX", self.usrp.set_rx_freq(tr(), self._CHAN), self._mcr())
            # the B200 has separate RX/TX LOs: move TX with RX, keeping the
            # RX/TX gap from open() or set_tx_frequency_live (0 = co-tuned), or a
            # keyed waveform stays at the old frequency and vanishes from the
            # retuned view (same rule as Epiq)
            new_tx = float(hz) + float(getattr(self, "_tx_lo", old)) - old
            try:
                self.usrp.set_tx_freq(types_.TuneRequest(new_tx, lo) if lo
                                      else types_.TuneRequest(new_tx), self._CHAN)
                self._tx_lo = new_tx
            except Exception as e:
                print(f"[UHD] TX LO not moved on retune: {e}", flush=True)
            self.config.center_freq = float(hz)
            return True
        except Exception:
            return False

    def set_tx_frequency_live(self, hz: float) -> bool:
        lo = getattr(self, "_lo_off", 0.0)
        types_ = self._uhd.types
        try:
            self._log_tune("TX", self.usrp.set_tx_freq(
                types_.TuneRequest(float(hz), lo) if lo else types_.TuneRequest(float(hz)),
                self._CHAN), self._mcr())
            self._tx_lo = float(hz)
            return True
        except Exception as e:
            print(f"[UHD] TX retune to {float(hz) / 1e6:.6f} MHz failed: {e}", flush=True)
            return False

    def set_gain_live(self, index: float) -> bool:
        try:
            self.usrp.set_rx_gain(float(index), self._CHAN)
            return True
        except Exception:
            return False

    def read_sensors(self) -> dict:
        """Live front-end sensors for the GUI status row: external-ref lock,
        RX/TX LO lock, RX RSSI, die temperature. Each guarded: a B200mini
        exposes ref_locked (mboard), rx {temp,rssi,lo_locked}, tx {temp,lo_locked}."""
        u = getattr(self, "usrp", None)
        if u is None:
            return {}
        out = {}
        def _mb_bool(n):
            try: return bool(u.get_mboard_sensor(n, 0).to_bool())
            except Exception: return None
        def _rx(n, real=False):
            try:
                s = u.get_rx_sensor(n, self._CHAN)
                return float(s.to_real()) if real else bool(s.to_bool())
            except Exception: return None
        def _tx(n, real=False):
            try:
                s = u.get_tx_sensor(n, self._CHAN)
                return float(s.to_real()) if real else bool(s.to_bool())
            except Exception: return None
        vals = {"ref_locked": _mb_bool("ref_locked"),
                "rx_lo_locked": _rx("lo_locked"), "rx_rssi_dbfs": _rx("rssi", True),
                "rx_temp_c": _rx("temp", True), "tx_lo_locked": _tx("lo_locked"),
                "tx_temp_c": _tx("temp", True)}
        return {k: v for k, v in vals.items() if v is not None}

    def usable_bw_hz(self) -> float:
        """Bandwidth around center whose noise floor is flat enough to detect in.
        The whole rate up to 30.72 MSps; EDGE_FREE_FRAC of it above. At 61.44 MSps
        the full grid made 673 of 3072 slots read as signals every tick, all of
        them edge noise, and the detector's Python work starved the RX drain."""
        r = float(getattr(self, "_achieved_rate", 0.0) or self.config.sample_rate)
        return r * self.EDGE_FREE_FRAC if r > 30.72e6 * 1.001 else r

    # top of the B200's TX gain range, used when UHD cannot report it
    TX_GAIN_MAX_DB = 89.75

    def set_tx_attenuation_qdb(self, qdb: int) -> bool:
        """TX attenuation in quarter dB below full power, the convention the
        service's set_tx_atten and the mission planner use for every radio. A
        UHD radio has TX gain instead, so this sets gain = the top of its gain
        range minus the attenuation (0 dB attenuation = full power), so
        set_tx_atten works on a B200 and a mission step that sets TX
        attenuation keys."""
        q = int(qdb)
        if q < 0:
            raise ValueError(f"tx attenuation {q} qdB below 0")
        top = self.TX_GAIN_MAX_DB
        try:
            top = float(self.usrp.get_tx_gain_range(self._CHAN).stop())
        except Exception:
            pass
        gain = max(0.0, top - q / 4.0)
        self.usrp.set_tx_gain(gain, self._CHAN)
        self.config.tx_gain = gain
        return True

    def card_temps(self) -> dict:
        """{0: degrees C} from the radio's own sensor (the AD936x die on a B2xx),
        shown on the operator card like an NV100 card temperature."""
        u = getattr(self, "usrp", None)
        if u is None:
            return {}
        try:
            return {0: int(round(float(u.get_rx_sensor("temp", self._CHAN).to_real())))}
        except Exception:
            return {}

    @property
    def card_names(self) -> dict:
        """What the operator card calls card 0: the motherboard name UHD reports
        (B200mini), else the radio kind."""
        name = getattr(self, "_mb_name", None)
        if name is None:
            try:
                name = str(self.usrp.get_mboard_name()).strip()
            except Exception:
                name = ""
            name = name or {"x410": "X410"}.get(getattr(self, "_kind", ""), "B200")
            if getattr(self, "usrp", None) is not None:
                self._mb_name = name
        return {"0": name}

    def close(self) -> None:
        self._stop_pump()
        if getattr(self, "_tx_stream", None) is not None:
            self.end_tx_burst()                 # stop the sender before the stream goes
        try:
            if getattr(self, "_rx_stream", None) is not None:
                cmd = self._uhd.types.StreamCMD(
                    self._uhd.types.StreamMode.stop_cont)
                self._rx_stream.issue_stream_cmd(cmd)
        except Exception:
            pass
        self._rx_stream = self._tx_stream = self.usrp = None
        self.health.state = SDRDeviceState.DISCONNECTED


# back-compat alias: the old "b200" kind maps here too.
B200Backend = UhdBackend


class PlutoBackend(SDRBackend):
    """ADALM-PLUTO (ADI AD9363) over USB/IP via libiio + pyadi-iio.

    A different driver stack from UHD (Ettus) and libsidekiq (Epiq): the Pluto
    speaks libiio. device_args is the libiio URI selecting the unit:
    "ip:192.168.2.1" (the Pluto's default USB-ethernet gadget), "usb:1.5.5", or
    "ip:pluto.local". rx/tx gain here are dB (AD9363 hardware gain), not the
    Epiq index; RX is put in manual-gain mode so the GUI gain slider applies.
    Single channel 0 on RX and TX. Pluto.rx() blocks until rx_buffer_size
    samples are ready, which paces the scheduler drain loop like real hardware.
    """

    _CHAN = 0
    _DEFAULT_URI = "ip:192.168.2.1"
    _RX_GAIN_DB = (0.0, 71.0)      # AD9363 manual RX hardware-gain range
    _FS_NORM = float(1 << 11)      # 12-bit ADC: int16 counts to ~[-1, 1] full scale

    def open(self) -> None:
        self.health.state = SDRDeviceState.CONNECTING
        try:
            import adi
        except ImportError as e:
            self.health.state = SDRDeviceState.ERROR
            raise SDRConnectionError(
                "pyadi-iio not installed (pip install pyadi-iio; needs libiio)") from e
        cfg = self.config
        uri = cfg.device_args or self._DEFAULT_URI
        try:
            self._adi = adi
            self.sdr = adi.Pluto(uri)
            fs = int(cfg.sample_rate)
            self.sdr.sample_rate = fs
            bw = int(cfg.extras.get("rx_bw", fs))
            # RX
            self.sdr.rx_lo = int(cfg.center_freq)
            self.sdr.rx_rf_bandwidth = bw
            self.sdr.gain_control_mode_chan0 = "manual"
            lo, hi = self._RX_GAIN_DB
            self.sdr.rx_hardwaregain_chan0 = max(lo, min(hi, float(cfg.rx_gain)))
            self.sdr.rx_buffer_size = int(cfg.rx_chunk)
            # TX is best-effort; a receive-only run must still open. Pluto TX gain
            # is attenuation (0 dB = max power, down to -89.75); treat cfg.tx_gain
            # as dB of attenuation below full scale.
            try:
                # separate TX synthesizer: honor an independent TX center
                tx_hz = int(cfg.extras.get("tx_center_freq", 0) or cfg.center_freq)
                self.sdr.tx_lo = tx_hz
                self._tx_lo = float(tx_hz)
                self.sdr.tx_rf_bandwidth = int(fs)
                self.sdr.tx_cyclic_buffer = False
                self.sdr.tx_hardwaregain_chan0 = max(-89.75, min(0.0, -float(cfg.tx_gain)))
                # Read the attenuation back so a silently ignored
                # setting shows up in the log (and in sensors)
                self._tx_atten_db = -float(self.sdr.tx_hardwaregain_chan0)
                print(f"[Pluto] TX attenuation {self._tx_atten_db:.2f} dB "
                      f"(requested {float(cfg.tx_gain):.2f})", flush=True)
            except Exception as _txe:
                print(f"[Pluto] TX config skipped: {_txe}", flush=True)
            self._cyc_wave = None          # Waveform looping in HW
            self._last_wave_id = None

            self._seq = 0
            self._rf_ts = 0
            self._achieved_rate = float(self.sdr.sample_rate)
            self.config.sample_rate = self._achieved_rate
            self.health.state = SDRDeviceState.STREAMING
        except SDRConnectionError:
            raise
        except Exception as e:
            self.health.state = SDRDeviceState.ERROR
            raise SDRConnectionError(f"Pluto open failed: {e}") from e

    def recv(self) -> AnalysisFrame:
        cfg = self.config
        data = self.sdr.rx()      # blocks until rx_buffer_size samples are ready
        if data is None or len(data) == 0:
            return AnalysisFrame(np.empty(0, dtype=np.complex64),
                                 self._achieved_rate, cfg.center_freq,
                                 self._seq, rf_timestamp=self._rf_ts)
        # pyadi returns int16 ADC counts as complex; normalize to ~unit full
        # scale so PSD/detector thresholds match the other backends (fc32 +/-1).
        iq = (np.asarray(data, dtype=np.complex64) / self._FS_NORM)
        n = iq.size
        self._seq += 1
        self._rf_ts += n
        self.health.rx_frames += 1
        return AnalysisFrame(iq, self._achieved_rate, cfg.center_freq,
                             self._seq, rf_timestamp=self._rf_ts)

    def send(self, samples: np.ndarray) -> None:
        # The scheduler's CONTINUOUS loop calls send() with the same
        # waveform over and over. Pushed as separate non-cyclic buffers from
        # Python, the gaps between them chop the signal (a CW tone still looks
        # fine on a PSD, framed FSK does not decode). On the second call with the
        # same buffer, hand it to the radio as a cyclic buffer instead: the FPGA
        # repeats it gap-free and later calls just pace the loop. A single-shot
        # send (one call) stays a plain one-pass transmit.
        try:
            if getattr(self, "_reopen_tx", False):
                self._reopen_for_tx()
            if samples is self._cyc_wave:
                time.sleep(min(0.1, len(samples) / max(self._achieved_rate, 1.0)))
                return
            # scale unit-amplitude host samples back to the AD9363 int16 range
            buf = np.ascontiguousarray(samples, dtype=np.complex64) * (self._FS_NORM - 1)
            if id(samples) == self._last_wave_id:
                self._stop_cyclic()
                # pyadi refuses to change tx_cyclic_buffer while a TX buffer
                # exists (the one-pass push above created one): drop it first
                try:
                    self.sdr.tx_destroy_buffer()
                except Exception:
                    pass
                self.sdr.tx_cyclic_buffer = True
                self.sdr.tx(buf)
                self._cyc_wave = samples
                print(f"[Pluto] TX cyclic: {len(samples)} samples looping in HW",
                      flush=True)
            else:
                self._stop_cyclic()
                self.sdr.tx(buf)
                self._last_wave_id = id(samples)
            self.health.tx_frames += 1
        except Exception as e:
            self.health.last_error = f"Pluto TX: {e}"

    def _stop_cyclic(self) -> None:
        if getattr(self, "_cyc_wave", None) is None:
            return
        self._cyc_wave = None
        try:
            self.sdr.tx_destroy_buffer()
        except Exception:
            pass
        try:
            self.sdr.tx_cyclic_buffer = False
        except Exception:
            pass

    def stop_tx(self) -> None:
        """A cyclic buffer keeps radiating after the Python loop
        stops, so tear it down explicitly. The next send() reopens the context so
        every TX starts from the same state as the first one after init: on the
        Orin's Pluto, TX power differed by ~60 dB between the first and a later
        TX on one context (tone #1 +64.8 dB, #2 +4.3 dB at 60 dB attenuation):
        most likely the requested attenuation being ignored at open and only
        applied later. Attenuation was linear (0/20/40 dB: +61/+42/+22 dB) on a
        fresh service; whether it always applies at open is unproven."""
        self._last_wave_id = None
        had_cyclic = getattr(self, "_cyc_wave", None) is not None
        self._stop_cyclic()
        if had_cyclic:
            self._reopen_tx = True

    def _reopen_for_tx(self) -> None:
        self._reopen_tx = False
        old = self.sdr
        for m in ("tx_destroy_buffer", "rx_destroy_buffer"):
            try:
                getattr(old, m)()
            except Exception:
                pass
        self.sdr = None
        del old
        self.open()                    # fresh context, same config
        print("[Pluto] TX context reopened after a cyclic stop", flush=True)

    def read_sensors(self) -> dict:
        return {"tx_atten_db": getattr(self, "_tx_atten_db", None),
                "tx_cyclic": getattr(self, "_cyc_wave", None) is not None}

    def set_frequency_live(self, hz: float) -> bool:
        old = float(self.config.center_freq)
        try:
            self.sdr.rx_lo = int(hz)
            # move TX with RX, keeping the RX/TX gap (0 = co-tuned), or a keyed
            # waveform stays at the old frequency (same rule as UHD and Epiq)
            if getattr(self, "_tx_lo", None) is not None:
                new_tx = float(hz) + float(self._tx_lo) - old
                try:
                    self.sdr.tx_lo = int(round(new_tx))
                    self._tx_lo = new_tx
                except Exception as e:
                    print(f"[Pluto] TX LO not moved on retune: {e}", flush=True)
            self.config.center_freq = float(hz)
            return True
        except Exception:
            return False

    def set_tx_frequency_live(self, hz: float) -> bool:
        try:
            self.sdr.tx_lo = int(round(float(hz)))
            self._tx_lo = float(hz)
            return True
        except Exception as e:
            print(f"[Pluto] TX retune failed: {e}", flush=True)
            return False

    def set_gain_live(self, index: float) -> bool:
        try:
            lo, hi = self._RX_GAIN_DB
            self.sdr.rx_hardwaregain_chan0 = max(lo, min(hi, float(index)))
            return True
        except Exception:
            return False

    def close(self) -> None:
        try:
            # release the libiio RX/TX buffers so the next open re-claims cleanly
            sdr = getattr(self, "sdr", None)
            if sdr is not None:
                for m in ("rx_destroy_buffer", "tx_destroy_buffer"):
                    try:
                        getattr(sdr, m)()
                    except Exception:
                        pass
        except Exception:
            pass
        self.sdr = None
        self.health.state = SDRDeviceState.DISCONNECTED


class SoapyBackend(SDRBackend):
    """Any SoapySDR radio, via the SoapySDR Python bindings (python3-soapysdr).

    Written for the Wavelet Lab xSDR (M.2 PCIe, LMS7002M), whose host stack is
    usdr-lib: kernel driver usdr_pcie_uram (usdr-dkms) + libusdr + the
    soapysdr-module-usdr plugin (driver key "usdr"). libusdr has no Python
    bindings of its own, so Soapy is the Python path: and the same class
    drives any other Soapy-supported radio (kind "soapy" + device_args).

    RX on a rev4 xSDR over USB 2.0 streams 1.92/5/10 MSps 100% with no
    timestamp gaps or overruns. TX on channel 0 has not been RF-tested through
    this class (the loopback test used TX channel 1).
    Offline tests: tests/test_soapy_backend.py in the Super Nano repo. Ranges come from the usdr Soapy
    module (xsdr_ranges / lms7_gains). Gains are dB: rx_gain is the overall RX
    gain (Soapy distributes it LNA/TIA/PGA); tx_gain is dB of attenuation below
    full scale, applied digitally because the LMS7 PAD control has no RF effect
    on this build (see _apply_tx_atten). Single channel 0 on RX and TX.
    Board runs hot (64 to 81 C in minutes of streaming): needs heatsinking.
    """

    _CHAN = 0
    # device_args used when the config supplies none, per kind. A bare "" makes
    # Soapy open the first device of any installed driver: pin the driver.
    _ARGS_BY_KIND = {"xsdr": "driver=usdr", "usdr": "driver=usdr"}
    # GUI sends Sidekiq/UHD port names; fall back to the driver's AUTO port.
    _DEFAULT_ANT = {"rx": "AUTO", "tx": "AUTO"}
    # Full-duplex radios activate the TX stream at open so send() is instant.
    # A half-duplex driver (HackRF) must not: activating TX stops RX there.
    _ACTIVATE_TX_AT_OPEN = True
    # readStream wait when no block is ready. The scheduler's drain batches
    # blocks until recv() returns empty, so this must be short relative to the
    # block period or the batch never ends (see HackRFBackend).
    _RX_TIMEOUT_US = 1_000_000

    def open(self) -> None:
        self.health.state = SDRDeviceState.CONNECTING
        try:
            import SoapySDR
        except ImportError as e:
            self.health.state = SDRDeviceState.ERROR
            raise SDRConnectionError(
                "SoapySDR Python module not installed (apt python3-soapysdr; "
                "xSDR also needs soapysdr-module-usdr + usdr-dkms)") from e
        cfg = self.config
        ex = cfg.extras or {}
        self._S = S = SoapySDR
        RX, TX = S.SOAPY_SDR_RX, S.SOAPY_SDR_TX
        # device_args: config (GUI) > DPMG_SOAPY_ARGS env (launch script pins a
        # unit, e.g. "driver=hackrf,serial=...") > per-kind driver default.
        dev_args = (cfg.device_args or os.environ.get("DPMG_SOAPY_ARGS", "")
                    or self._ARGS_BY_KIND.get(getattr(self, "_kind", ""), ""))
        self._rx_stream = self._tx_stream = None
        try:
            self.dev = S.Device(dev_args)
            clk = (cfg.clock_source or "internal").lower()
            if clk not in ("", "internal"):
                try:
                    if clk in list(self.dev.listClockSources()):
                        self.dev.setClockSource(clk)
                except Exception as e:
                    print(f"[Soapy] clock_source {clk!r} not applied: {e}", flush=True)
            # usdr couples RX and TX rate (setSampleRate sets both), so set once
            # per direction with the same value.
            self.dev.setSampleRate(RX, self._CHAN, float(cfg.sample_rate))
            self.dev.setFrequency(RX, self._CHAN, float(cfg.center_freq))
            self._set_antenna(RX, "rx", cfg.rx_antenna)
            try:
                self.dev.setGainMode(RX, self._CHAN, False)    # manual gain
            except Exception:
                pass
            # clamp: a config built with the Epiq index default (245) must not
            # throw: the driver's overall RX range is about -12..61 dB on xSDR.
            self.dev.setGain(RX, self._CHAN, self._clamp_rx_gain(float(cfg.rx_gain)))
            rx_bw = float(ex.get("rx_bw_hz", 0) or 0)
            if rx_bw > 0:
                try:
                    self.dev.setBandwidth(RX, self._CHAN, rx_bw)
                except Exception as e:
                    print(f"[Soapy] RX bandwidth {rx_bw} skipped: {e}", flush=True)
            try:
                if self.dev.hasDCOffsetMode(RX, self._CHAN):
                    self.dev.setDCOffsetMode(RX, self._CHAN, bool(ex.get("dc_offset", True)))
            except Exception:
                pass
            # TX is best-effort; a receive-only run must still open.
            try:
                self.dev.setSampleRate(TX, self._CHAN, float(cfg.sample_rate))
                tx_f = float(ex.get("tx_center_freq", cfg.center_freq))
                self.dev.setFrequency(TX, self._CHAN, tx_f)
                self._tx_offset_hz = tx_f - float(cfg.center_freq)   # 0 = co-tuned
                self._set_antenna(TX, "tx", cfg.tx_antenna)
                self._tx_atten_db = self._apply_tx_atten(float(cfg.tx_gain))
                tx_bw = float(ex.get("tx_bw_hz", 0) or 0)
                if tx_bw > 0:
                    self.dev.setBandwidth(TX, self._CHAN, tx_bw)
                self._tx_stream = self.dev.setupStream(TX, S.SOAPY_SDR_CF32, [self._CHAN],
                                                       self._stream_args())
                if self._ACTIVATE_TX_AT_OPEN:
                    self.dev.activateStream(self._tx_stream)
                self._tx_mtu = max(1, int(self.dev.getStreamMTU(self._tx_stream)))
            except Exception as _txe:
                self._tx_stream = None
                print(f"[Soapy] TX config skipped: {_txe}", flush=True)

            self._rx_stream = self.dev.setupStream(RX, S.SOAPY_SDR_CF32, [self._CHAN],
                                                   self._stream_args())
            # usdr's readStream wants numElems == its configured block size and
            # otherwise injects a jitter buffer ("performance will be degraded"),
            # so read exactly one MTU per call rather than cfg.rx_chunk.
            mtu = int(self.dev.getStreamMTU(self._rx_stream) or 0)
            self._rx_n = mtu if mtu > 0 else int(cfg.rx_chunk)
            self._rx_buf = np.zeros(self._rx_n, dtype=np.complex64)
            self.dev.activateStream(self._rx_stream)
            self._seq = 0
            self._rf_ts = 0
            self._achieved_rate = float(self.dev.getSampleRate(RX, self._CHAN))
            self.config.sample_rate = self._achieved_rate
            self.health.state = SDRDeviceState.STREAMING
        except SDRConnectionError:
            raise
        except Exception as e:
            self.close()
            self.health.state = SDRDeviceState.ERROR
            raise SDRConnectionError(f"Soapy open failed ({dev_args!r}): {e}") from e

    def _stream_args(self) -> dict:
        """Wire format. USB 2.0 carries ~11.26 MSps of CS16; the LMS7002M ADC is
        12-bit, so CS12 is lossless and lifts the ceiling to ~15 MSps. Use it above 11 MSps unless extras["link_format"] says
        otherwise."""
        fmt = (self.config.extras or {}).get("link_format")
        if not fmt and self.config.sample_rate > 11e6:
            fmt = "CS12"
        return {"linkFormat": fmt} if fmt else {}

    def _set_antenna(self, d: int, name: str, want: str) -> None:
        """Select an antenna, tolerating port labels from other radio families
        (the GUI may send "TRx1A" or "RX2"); unknown: AUTO / first listed."""
        try:
            valid = list(self.dev.listAntennas(d, self._CHAN))
        except Exception:
            valid = []
        if not valid:
            return
        if want not in valid:
            prefer = self._DEFAULT_ANT[name]
            new = prefer if prefer in valid else valid[0]
            if want:
                print(f"[Soapy] {name.upper()} antenna {want!r} not in {valid}; "
                      f"using {new!r}", flush=True)
            want = new
        try:
            self.dev.setAntenna(d, self._CHAN, want)
        except Exception as e:
            print(f"[Soapy] setAntenna({name}, {want!r}) skipped: {e}", flush=True)

    def _clamp_rx_gain(self, g: float) -> float:
        try:
            r = self.dev.getGainRange(self._S.SOAPY_SDR_RX, self._CHAN)
            return max(float(r.minimum()), min(float(r.maximum()), g))
        except Exception:
            return g

    def _apply_tx_atten(self, atten_db: float) -> float:
        """tx_gain is dB attenuation below max, applied digitally in send().

        On the xSDR (usdr-lib f7e869d, USB) the LMS7 PAD gain reads back but has
        no RF effect: in an RF loopback 52 dB of PAD on either TX channel moves
        the received tone 0 dB, while digital scaling tracks exactly. So hold the analog gain at its max and scale the samples; the
        range stays the driver's (0..52 dB on LMS7). Returns the atten set."""
        TX = self._S.SOAPY_SDR_TX
        r = self.dev.getGainRange(TX, self._CHAN)
        lo, hi = float(r.minimum()), float(r.maximum())
        att = max(0.0, min(hi - lo, atten_db))
        self.dev.setGain(TX, self._CHAN, hi)
        self._tx_scale = np.float32(10.0 ** (-att / 20.0))
        return att

    def recv(self) -> AnalysisFrame:
        cfg = self.config
        S = self._S
        sr = self.dev.readStream(self._rx_stream, [self._rx_buf], self._rx_n,
                                 timeoutUs=self._RX_TIMEOUT_US)
        n = int(sr.ret)
        if n <= 0:
            if n == S.SOAPY_SDR_OVERFLOW:
                self.health.overruns += 1
            elif n != S.SOAPY_SDR_TIMEOUT:
                self.health.last_error = f"Soapy readStream: {S.errToStr(n)}"
            return AnalysisFrame(np.empty(0, dtype=np.complex64),
                                 self._achieved_rate, cfg.center_freq,
                                 self._seq, rf_timestamp=self._rf_ts)
        self._seq += 1
        # rf_timestamp = start sample index of this block (see UhdBackend.recv:
        # the drain's RXGAP check and FrameAccumulator assume ts_i == ts_{i-1} +
        # n_{i-1}). When the driver stamps hardware time, derive the index from
        # it so a dropped block shows up as a gap instead of being papered over.
        if sr.flags & S.SOAPY_SDR_HAS_TIME:
            ts0 = int(round(sr.timeNs * 1e-9 * self._achieved_rate))
        else:
            ts0 = self._rf_ts
        self._rf_ts = ts0 + n
        self.health.rx_frames += 1
        return AnalysisFrame(self._rx_buf[:n].copy(), self._achieved_rate,
                             cfg.center_freq, self._seq, rf_timestamp=ts0)

    def send(self, samples: np.ndarray) -> None:
        if self._tx_stream is None:
            self.health.last_error = "Soapy TX: no TX stream (TX config failed at open)"
            return
        S = self._S
        # usdr's writeStream sends all-or-nothing and expects exactly one packet
        # (MTU samples) per call, so zero-pad the tail to a whole MTU. The loop
        # still handles partial writes for other Soapy drivers; END_BURST rides
        # the request that reaches the end (drivers apply it only when the
        # whole request is consumed).
        buf = np.ascontiguousarray(samples, dtype=np.complex64)
        scale = getattr(self, "_tx_scale", None)
        if scale is not None and scale != 1.0:
            buf = buf * scale
        pad = (-buf.size) % self._tx_mtu
        if pad:
            buf = np.concatenate([buf, np.zeros(pad, dtype=np.complex64)])
        try:
            off, total = 0, buf.size
            while off < total:
                n = min(self._tx_mtu, total - off)
                flags = S.SOAPY_SDR_END_BURST if off + n >= total else 0
                sr = self.dev.writeStream(self._tx_stream, [buf[off:]], n,
                                          flags, timeoutUs=1_000_000)
                if sr.ret <= 0:
                    if sr.ret == S.SOAPY_SDR_UNDERFLOW:
                        self.health.underruns += 1
                        continue
                    raise RuntimeError(S.errToStr(sr.ret))
                off += sr.ret
            self.health.tx_frames += 1
        except Exception as e:
            self.health.last_error = f"Soapy TX: {e}"

    def set_frequency_live(self, hz: float) -> bool:
        try:
            self.dev.setFrequency(self._S.SOAPY_SDR_RX, self._CHAN, float(hz))
            # dual-LO radios (LimeSDR, xSDR): move the TX LO with RX, keeping
            # any TX/RX offset chosen at open (tx_center_freq). Found on the
            # LimeSDR loopback: after a GUI retune the jammer stayed at the old
            # center and was invisible on the retuned RX. Harmless on the
            # single-LO HackRF (its driver applies the TX value on switch).
            if getattr(self, "_tx_stream", None) is not None:
                try:
                    self.dev.setFrequency(self._S.SOAPY_SDR_TX, self._CHAN,
                                          float(hz) + float(getattr(self, "_tx_offset_hz", 0.0)))
                except Exception as e:
                    print(f"[Soapy] TX LO not moved on retune: {e}", flush=True)
            self.config.center_freq = float(hz)
            return True
        except Exception:
            return False

    def set_tx_frequency_live(self, hz: float) -> bool:
        # dual-LO radios only; the single-LO HackRF can't hold RX and TX apart
        if getattr(self, "_tx_stream", None) is None:
            return False
        try:
            self.dev.setFrequency(self._S.SOAPY_SDR_TX, self._CHAN, float(hz))
            self._tx_offset_hz = float(hz) - float(self.config.center_freq)
            return True
        except Exception as e:
            print(f"[Soapy] TX retune failed: {e}", flush=True)
            return False

    def set_gain_live(self, index: float) -> bool:
        try:
            g = self._clamp_rx_gain(float(index))
            self.dev.setGain(self._S.SOAPY_SDR_RX, self._CHAN, g)
            # the service's overload back-off reads config.rx_gain back to see
            # whether a step actually moved the gain (and to stop at the floor)
            self.config.rx_gain = float(g)
            return True
        except Exception:
            return False

    def set_tx_attenuation_db(self, db: float) -> bool:
        try:
            self._tx_atten_db = self._apply_tx_atten(float(db))
            return True
        except Exception:
            return False

    def set_tx_attenuation_qdb(self, qdb: int) -> bool:
        """Service `set_tx_atten` sends quarter-dB (the Epiq unit); Soapy radios
        take dB. Without this shim the GUI's live TX attenuation is rejected
        with "backend has no TX attenuation" for every Soapy-family radio."""
        return self.set_tx_attenuation_db(int(qdb) / 4.0)

    def read_sensors(self) -> dict:
        """Board temperature only. usdr's clock_locked/ref_locked/lo_locked
        sensors are hard-coded "true" in the driver, so surfacing them would
        show a lock indicator that can never go red, so they are omitted."""
        out = {}
        dev = getattr(self, "dev", None)
        if dev is None:
            return out
        try:
            if "board_temp" in list(dev.listSensors()):
                v = float(dev.readSensor("board_temp"))
                if v == v:                       # NaN = read failed
                    out["rx_temp_c"] = v
        except Exception:
            pass
        if getattr(self, "_tx_atten_db", None) is not None:
            out["tx_atten_db"] = self._tx_atten_db
        return out

    def close(self) -> None:
        dev = getattr(self, "dev", None)
        for attr in ("_rx_stream", "_tx_stream"):
            st = getattr(self, attr, None)
            if dev is not None and st is not None:
                try:
                    dev.deactivateStream(st)
                except Exception:
                    pass
                try:
                    dev.closeStream(st)
                except Exception:
                    pass
            setattr(self, attr, None)
        # Release the driver handle now. The Python wrapper only closes the
        # device when it is garbage-collected; a lingering reference (a thread
        # still unwinding a send(), an exception frame) leaves libhackrf's
        # handle open and the next open() fails with "Could not Open HackRF
        # Device" (for example after a teardown mid-TX).
        if dev is not None:
            try:
                unmake = getattr(getattr(self, "_S", None), "Device", None)
                unmake = getattr(unmake, "unmake", None)
                if callable(unmake):
                    unmake(dev)
            except Exception as e:
                print(f"[Soapy] Device.unmake skipped: {e}", flush=True)
        self.dev = None
        self.health.state = SDRDeviceState.DISCONNECTED


class HackRFBackend(SoapyBackend):
    """HackRF One / HackRF Pro over USB via SoapyHackRF (driver key "hackrf").

    The HackRF is half-duplex: one LO and one RF path, and libhackrf is in RX
    mode or TX mode, never both. SoapyHackRF auto-switches on readStream /
    writeStream, so left to itself the scheduler's RX-drain and TX threads
    would ping-pong the radio mid-burst. This class owns the switching:
      * open() activates RX only (the base also activates TX, which on this
        driver stops RX);
      * send() in burst mode (default; SINGLE_SHOT, transceive, tools): switch
        to TX with END_BURST sized for payload + flush zeros, write, then
        switch back to RX: the driver waits for the burst to finish first;
      * send() in hold mode (scheduler CONTINUOUS, e.g. jamming): switch to
        TX once and stay there until stop_tx(); recv() yields empty frames
        because RX is dead while this radio transmits.
    A lock serializes the two threads around every mode change.
    Gains: RX = LNA (0..40, 8 dB steps) + VGA (0..62, 2 dB steps) set as
    elements from one 0..102 dB number (+14 dB RF amp via extras rx_amp,
    default off); TX = dB attenuation below the 47 dB TX VGA, analog (extras
    tx_amp adds the 14 dB amp). Wire format is native CS8 (SoapyHackRF
    scales to +-1.0 CF32 and casts TX without clipping: send() clips).
    HackRF Pro needs libhackrf >= v2026.01.1 (board "Praline"); it runs in
    HackRF-One-compatible legacy mode (8-bit, 2-20 MSps). Offline tests and
    image: tests/test_hackrf_backend.py and Dockerfile.hackrf in the Super Nano repo.
    """

    _ARGS_BY_KIND = {"hackrf": "driver=hackrf", "hackrf_pro": "driver=hackrf"}
    _DEFAULT_ANT = {"rx": "TX/RX", "tx": "TX/RX"}
    _ACTIVATE_TX_AT_OPEN = False
    # Blocks are 131072 samples (65 ms at 2 MSps). With the xSDR's 1 s wait the
    # drain never sees an empty ring and batches 2048 blocks (~2 min, ~2 GB)
    # before publishing a frame: no PSD, and the host runs out of memory.
    # 5 ms: a ready block returns at once, an empty ring ends the batch.
    _RX_TIMEOUT_US = 5_000
    # Read in sub-MTU blocks (SoapyHackRF keeps the remainder of a 131072-
    # sample buffer across calls): 32768 = 16 ms at 2 MSps / 1.6 ms at 20 MSps,
    # so a PSD frame is never a 65 ms-old block and the drain's >50 ms RXLOOP
    # warning stops firing on every block at low rates. extras rx_block overrides.
    RX_BLOCK = 32768
    _RX_LNA_MAX, _RX_VGA_MAX, _TX_VGA_MAX, _AMP_DB = 40, 62, 47, 14
    RX_GAIN_MAX_DB = _RX_LNA_MAX + _RX_VGA_MAX           # 102
    # libhackrf keeps 4 USB transfers (one MTU each) in flight; a burst is
    # padded with this many MTUs of zeros so the stop after END_BURST cancels
    # only zeros, never the payload tail. extras["tx_flush_bufs"] overrides.
    TX_FLUSH_BUFS = 4

    def open(self) -> None:
        self._hd_lock = threading.RLock()
        self._tx_hold = False          # CONTINUOUS: stay in TX between sends
        self._in_tx = False
        self._rx_gain_db = None
        super().open()
        ex = self.config.extras or {}
        blk = int(ex.get("rx_block", self.RX_BLOCK))
        self._rx_mtu = int(self._rx_n)            # driver buffer = 131072 samples
        if 0 < blk < self._rx_n:
            self._rx_n = blk
            self._rx_buf = np.zeros(blk, dtype=np.complex64)
        self._rx_scratch = np.zeros(self._rx_mtu, dtype=np.complex64)
        self._rx_pos = 0                           # samples read since RX (re)start
        # libhackrf hands over one 131072-sample transfer at a time, so the
        # drain cadence is that long at low rates (65 ms at 2 MSps): tell the
        # scheduler so its >50 ms RXLOOP warning doesn't fire on every block.
        self._rx_loop_warn_s = 2.0 * self._rx_mtu / max(self._achieved_rate, 1.0)
        # >= 1: the burst countdown below needs at least one flush MTU
        self._flush_bufs = max(1, int(ex.get("tx_flush_bufs", self.TX_FLUSH_BUFS)))
        # the base applied an overall setGain (SoapyHackRF's own LNA/VGA/AMP
        # split, which engages the amp above 51 dB); re-apply as elements.
        try:
            self._apply_rx_gain(float(self.config.rx_gain))
        except Exception as e:
            print(f"[HackRF] RX gain elements skipped: {e}", flush=True)
        # antenna port bias tee (3.3 V on TX/RX for an active antenna / LNA).
        # SoapyHackRF setting "bias_tx"; off unless extras bias_tx is true.
        self._bias_tx = bool(ex.get("bias_tx", False))
        try:
            self.dev.writeSetting("bias_tx", "true" if self._bias_tx else "false")
        except Exception as e:
            if self._bias_tx:
                print(f"[HackRF] bias_tx not applied: {e}", flush=True)
        # reference: the hardware auto-selects an external 10 MHz on CLKIN;
        # SoapyHackRF reports which it found at open as "clock source".
        self._ref_source = None
        try:
            info = dict(self.dev.getHardwareInfo())
            cs = str(info.get("clock source", "")).strip().lower()
            if cs:
                self._ref_source = cs
        except Exception:
            pass

    # gains
    @classmethod
    def split_rx_gain(cls, g: float):
        """One 0..102 dB number: (LNA, VGA): LNA on its 8 dB grid, VGA on its
        2 dB grid, VGA carrying ~60% so the pair lands within 1 dB of the
        request. Shared by the backend and the GUI's dropdown labels."""
        g = max(0.0, min(float(cls.RX_GAIN_MAX_DB), float(g)))
        lna = min(cls._RX_LNA_MAX, 8 * int(round(g * 0.4 / 8.0)))
        vga = max(0, min(cls._RX_VGA_MAX, 2 * int(round((g - lna) / 2.0))))
        return lna, vga

    def _apply_rx_gain(self, g: float) -> float:
        """One 0..102 dB number: LNA (8 dB steps) + VGA (2 dB steps); the
        14 dB RF amp is only engaged by extras rx_amp (never implicitly)."""
        RX = self._S.SOAPY_SDR_RX
        lna, vga = self.split_rx_gain(g)
        amp = self._AMP_DB if (self.config.extras or {}).get("rx_amp") else 0
        self.dev.setGain(RX, self._CHAN, "LNA", float(lna))
        self.dev.setGain(RX, self._CHAN, "VGA", float(vga))
        self.dev.setGain(RX, self._CHAN, "AMP", float(amp))
        self._rx_gain_db = float(lna + vga + amp)
        return self._rx_gain_db

    def _apply_tx_atten(self, atten_db: float) -> float:
        """tx_gain is dB attenuation below the TX VGA max (47 dB), applied in
        the analog VGA (1 dB steps): the DAC is 8-bit, so digital scaling
        would cost resolution. extras tx_amp adds the +14 dB RF amp."""
        TX = self._S.SOAPY_SDR_TX
        att = max(0.0, min(float(self._TX_VGA_MAX), float(atten_db)))
        vga = int(round(self._TX_VGA_MAX - att))
        amp = self._AMP_DB if (self.config.extras or {}).get("tx_amp") else 0
        self.dev.setGain(TX, self._CHAN, "VGA", float(vga))
        self.dev.setGain(TX, self._CHAN, "AMP", float(amp))
        self._tx_scale = None
        return float(self._TX_VGA_MAX - vga)

    def set_gain_live(self, index: float) -> bool:
        try:
            g = max(0.0, min(float(self.RX_GAIN_MAX_DB), float(index)))
            self._apply_rx_gain(g)
            self.config.rx_gain = g        # LNA+VGA request (amp rides extras)
            return True
        except Exception:
            return False

    # half-duplex streaming
    def set_tx_hold(self, hold: bool) -> None:
        """Scheduler hook: CONTINUOUS TX keeps the radio in TX between sends
        (gapless jamming); clearing it while transmitting returns to RX."""
        with self._hd_lock:
            self._tx_hold = bool(hold)
            if not hold and self._in_tx:
                self._to_rx()

    def _to_rx(self) -> None:
        # activateStream(RX) in SoapyHackRF: if a burst end is pending it waits
        # for the burst to finish, then hackrf_stop_tx + hackrf_start_rx (and
        # resets the RX ring, so the read position restarts at a buffer edge).
        self.dev.activateStream(self._rx_stream)
        self._in_tx = False
        self._rx_pos = 0

    def _finish_rx_buffer(self) -> None:
        """Consume the rest of a partially read driver buffer before leaving RX.
        Sub-MTU reads leave SoapyHackRF holding a remainder of the current
        131072-sample buffer; the RX restart after TX zeroes its buffer count,
        and releasing that remainder afterwards wraps the count (uint32) so
        reads then return stale buffers at CPU speed (at 8 MSps: 65 MSps
        "effective" for 2 s after a burst). Reading up to
        the buffer edge first, those samples are blanked anyway, avoids it."""
        mtu = self._rx_mtu
        rem = self._rx_pos % mtu
        tries = 0
        while rem and tries < 60:
            tries += 1
            sr = self.dev.readStream(self._rx_stream, [self._rx_scratch], mtu - rem,
                                     timeoutUs=self._RX_TIMEOUT_US)
            if sr.ret > 0:
                self._rx_pos += int(sr.ret)
                rem = self._rx_pos % mtu
        if rem:
            print(f"[HackRF] could not finish RX buffer before TX ({rem} left)", flush=True)

    def _write_all(self, buf: np.ndarray, end_burst: bool) -> None:
        S = self._S
        off, total, idle = 0, buf.size, 0
        while off < total:
            n = min(self._tx_mtu, total - off)
            flags = S.SOAPY_SDR_END_BURST if (end_burst and off + n >= total) else 0
            sr = self.dev.writeStream(self._tx_stream, [buf[off:]], n, flags,
                                      timeoutUs=1_000_000)
            if sr.ret <= 0:
                if sr.ret == S.SOAPY_SDR_UNDERFLOW:
                    self.health.underruns += 1
                    continue
                if sr.ret == S.SOAPY_SDR_TIMEOUT:
                    # ring full = back-pressure at low rates (15 MTU ~ 1 s at
                    # 2 MSps); keep waiting, but not forever.
                    idle += 1
                    if idle > 30:
                        raise RuntimeError("writeStream timeout (ring never drained)")
                    continue
                raise RuntimeError(S.errToStr(sr.ret))
            idle = 0
            off += sr.ret

    def send(self, samples: np.ndarray) -> None:
        if self._tx_stream is None:
            self.health.last_error = "HackRF TX: no TX stream (TX config failed at open)"
            return
        S = self._S
        buf = np.ascontiguousarray(samples, dtype=np.complex64)
        # SoapyHackRF multiplies by 127 and casts to int8 without clipping, so
        # anything past +-1.0 would wrap: clip here.
        if buf.size and (np.abs(buf.real).max() > 1.0 or np.abs(buf.imag).max() > 1.0):
            buf = (np.clip(buf.real, -1.0, 1.0) + 1j * np.clip(buf.imag, -1.0, 1.0)
                   ).astype(np.complex64)
        pad = (-buf.size) % self._tx_mtu
        if pad:
            buf = np.concatenate([buf, np.zeros(pad, dtype=np.complex64)])
        with self._hd_lock:
            try:
                if self._tx_hold:
                    if not self._in_tx:
                        self._finish_rx_buffer()
                        self.dev.activateStream(self._tx_stream)
                        self._in_tx = True
                    self._write_all(buf, end_burst=False)
                else:
                    self._finish_rx_buffer()
                    flush = np.zeros(self._flush_bufs * self._tx_mtu, dtype=np.complex64)
                    # END_BURST + numElems on activate is how SoapyHackRF arms
                    # a burst: its TX callback subtracts one MTU per buffer it
                    # hands to libhackrf and stops the radio when the count
                    # goes strictly below zero: and an empty ring does not
                    # decrement it. Arming for exactly what is written therefore
                    # never ends the burst (_to_rx spins forever), so arm for one MTU
                    # less: the last flush buffer pushes the count negative
                    # after every payload buffer is out. Issued from RX mode.
                    self.dev.activateStream(self._tx_stream, S.SOAPY_SDR_END_BURST, 0,
                                            int(buf.size + flush.size - self._tx_mtu))
                    self._in_tx = True
                    self._write_all(buf, end_burst=False)
                    self._write_all(flush, end_burst=True)
                    self._to_rx()
                self.health.tx_frames += 1
            except Exception as e:
                self.health.last_error = f"HackRF TX: {e}"
                try:
                    self._to_rx()
                except Exception:
                    pass

    def stop_tx(self) -> None:
        """Scheduler hook after SINGLE_SHOT / on Stop TX: leave TX mode. In
        burst mode the radio is already back in RX; in hold mode this drops
        whatever is still queued (jammer stop) and restarts RX."""
        with self._hd_lock:
            self._tx_hold = False
            if self._in_tx:
                self._to_rx()

    def recv(self) -> AnalysisFrame:
        if self._in_tx:
            # half-duplex: nothing to receive while transmitting. Don't call
            # readStream: SoapyHackRF would switch the radio back to RX and
            # cut the burst. Pace the drain thread instead.
            time.sleep(0.02)
            return AnalysisFrame(np.empty(0, dtype=np.complex64), self._achieved_rate,
                                 self.config.center_freq, self._seq,
                                 rf_timestamp=self._rf_ts)
        with self._hd_lock:
            if self._in_tx:
                return AnalysisFrame(np.empty(0, dtype=np.complex64), self._achieved_rate,
                                     self.config.center_freq, self._seq,
                                     rf_timestamp=self._rf_ts)
            fr = super().recv()
            self._rx_pos += int(fr.samples.size)
            return fr

    def read_sensors(self) -> dict:
        out = super().read_sensors()
        ex = self.config.extras or {}
        if self._rx_gain_db is not None:
            out["rx_gain_db"] = self._rx_gain_db
        out["tx_active"] = bool(self._in_tx)
        out["rx_amp"] = bool(ex.get("rx_amp"))
        out["tx_amp"] = bool(ex.get("tx_amp"))
        out["bias_tx"] = bool(getattr(self, "_bias_tx", False))
        rs = getattr(self, "_ref_source", None)
        if rs:
            out["ref_source"] = rs
        return out

    def close(self) -> None:
        lock = getattr(self, "_hd_lock", None)
        if lock is None:
            return super().close()
        with lock:
            super().close()
            self._in_tx = False


class LimeSDRBackend(SoapyBackend):
    """LimeSDR Mini (v1.x, LMS7002M + MAX10, USB 3) via SoapyLMS7 (driver key
    "lime"), apt limesuite/soapysdr-module-lms7.

    What this radio is (LimeSuite src/API/LimeSDR_mini.cpp + lms7_device.cpp):
      * full duplex, 1 RX + 1 TX, independent RX/TX LOs (tx_center_freq works);
      * 10 MHz .. 3.5 GHz (below 30 MHz the driver tunes 30 MHz and offsets the
        NCO); 100 kSps .. 30.72 MSps, 12-bit; stream native CS16, CF32 here;
      * RX paths LNAH / LNAW only: "LNAL_NC" is not connected on the Mini;
        TX paths BAND1 / BAND2. "Auto" lets the board switch: RX LNAW below
        1.7 GHz else LNAH; TX BAND2 below 2.0 GHz else BAND1;
      * reference 40 MHz VCTCXO, internal only (external needs the R59/R62
        hardware mod, the driver just warns);
      * RX gain = LNA 0..30 + TIA 0..12 + PGA -12..19 dB, set here as elements
        from one 0..61 dB number (the driver's overall setGain caps at 49 dB
        with NEW_GAIN_BEHAVIOUR); TX gain = PAD 0..52 dB, so tx_gain is dB of
        attenuation below PAD max, analog (IAMP left at 0 dB);
      * RX DC corrector and LMS7 DC/IQ calibration ("CALIBRATE"
        per channel, needs the LPF bandwidth): extras dc_offset / iq_balance;
      * analog LPF: RX 1.4..130 MHz, TX 5..40 / 50..130 MHz: set to the
        sample rate by default so the spectrum edges are filtered;
      * sensors clock_locked (CGEN), lms7_temp (chip C), lo_locked per direction;
        hardware timestamps on readStream.
    Offline tests and image: tests/test_limesdr_backend.py and Dockerfile.limesdr
    in the Super Nano repo.
    """

    _ARGS_BY_KIND = {"limesdr": "driver=lime", "lime": "driver=lime",
                     "limesdr_mini": "driver=lime"}
    _DEFAULT_ANT = {"rx": "Auto", "tx": "Auto"}
    _BAD_ANT = {"LNAL", "LNAL_NC", "NONE"}        # not wired on the Mini / disables RX
    _RX_TIMEOUT_US = 100_000
    RX_BLOCK = 16384              # readStream serves any size from its FIFO
    _LNA_MAX, _TIA_MAX, _PGA_MIN, _PGA_MAX = 30.0, 12.0, -12.0, 19.0
    RX_GAIN_MAX_DB = _LNA_MAX + _TIA_MAX + _PGA_MAX          # 61
    _PAD_MAX = 52.0
    _RX_LPF = (1.4e6, 130e6)
    _TX_LPF = (5e6, 130e6)

    def _stream_args(self) -> dict:
        # SoapyLMS7 has no linkFormat. Size its FIFO explicitly: the default
        # (0 = auto) is small enough that a 30 ms stall at 15 MSps drops
        # samples; 250 ms of samples at the configured
        # rate costs 30 MB at 30.72 MSps.
        # skipCal: SoapyLMS7 otherwise runs its own DC/IQ calibration when a
        # stream is activated, before open() has raised the PAD, and logs
        # "Tx Calibration: MCU error 5" at any real attenuation. The backend
        # calibrates explicitly (PAD at max) when extras iq_balance is on.
        return {"bufferLength": str(int(max(65536, 0.25 * float(self.config.sample_rate)))),
                "skipCal": "true"}

    def _set_antenna(self, d: int, name: str, want: str) -> None:
        # foreign labels (TRx1A, RX2, TX/RX) and the Mini's unconnected LNAL
        # fall back to "Auto" so the board picks the path for the frequency.
        if want in self._BAD_ANT:
            print(f"[Lime] {name.upper()} path {want!r} not usable on the Mini; "
                  f"using Auto", flush=True)
            want = "Auto"
        super()._set_antenna(d, name, want)

    def open(self) -> None:
        self._rx_gain_db = None
        super().open()
        RX, TX = self._S.SOAPY_SDR_RX, self._S.SOAPY_SDR_TX
        ex = self.config.extras or {}
        rate = float(self._achieved_rate)
        # read block ~2 ms of samples, never below RX_BLOCK: 16384 at 7.68 MSps,
        # 61440 at 30.72 (the Python per-call cost showed as timestamp gaps at
        # 30.72 MSps with 16384-sample reads); extras rx_block overrides.
        blk = int(ex.get("rx_block") or max(self.RX_BLOCK, 4096 * round(rate * 0.002 / 4096)))
        if blk > 0 and blk != self._rx_n:
            self._rx_n = blk
            self._rx_buf = np.zeros(blk, dtype=np.complex64)
        # the base applied the driver's overall setGain (clamped to its 0..49
        # range): re-apply as elements so the full LNA+TIA+PGA range is usable
        try:
            self._apply_rx_gain(float(self.config.rx_gain))
        except Exception as e:
            print(f"[Lime] RX gain elements skipped: {e}", flush=True)
        # analog LPF defaults: follow the rate unless the GUI gave explicit widths
        try:
            if not ex.get("rx_bw_hz"):
                self.dev.setBandwidth(RX, self._CHAN, self._clamp(rate, *self._RX_LPF))
            if self._tx_stream is not None and not ex.get("tx_bw_hz"):
                self.dev.setBandwidth(TX, self._CHAN, self._clamp(rate, *self._TX_LPF))
        except Exception as e:
            print(f"[Lime] LPF default skipped: {e}", flush=True)
        # Narrow rates: the analog LPF floors at 1.4 MHz (RX) / 5 MHz (TX), so
        # at 100 kSps..~1.4 MSps the band would be unfiltered after decimation.
        # Enable the LMS7 GFIR digital low-pass instead (SoapyLMS7
        # "ENABLE_GFIR_LPF", must be set after the sample rate). LimeSuite's
        # ConfigureGFIR normalises the half bandwidth to the decimated rate and
        # refuses w*1.1 > 0.495, i.e. anything above ~0.9 x rate ("GFIR LPF
        # cannot be set to the requested bandwidth" on hardware at 1.0 x rate),
        # so ask for 0.8 x rate: the same usable fraction the GUI shows.
        for d, floor in ((RX, self._RX_LPF[0]), (TX, self._TX_LPF[0])):
            if d == TX and self._tx_stream is None:
                continue
            if rate < floor:
                try:
                    self.dev.writeSetting(d, self._CHAN, "ENABLE_GFIR_LPF", str(0.8 * rate))
                    self._gfir_on = True
                except Exception as e:
                    print(f"[Lime] GFIR LPF {'TX' if d == TX else 'RX'} skipped: {e}", flush=True)
        # LMS7 DC/IQ calibration (GUI "DC/IQ auto-correct"): the driver's
        # CALIBRATE setting takes the bandwidth to calibrate for. The TX
        # calibration drives the internal loopback and fails with "MCU error 5
        # (Loopback signal weak)" when the PAD is low (e.g. at 50 dB atten),
        # so run it with the PAD at max and restore the attenuation.
        if ex.get("iq_balance", True):
            for d, lo, hi in ((RX, *self._RX_LPF), (TX, *self._TX_LPF)):
                if d == TX and self._tx_stream is None:
                    continue
                try:
                    if d == TX:
                        self.dev.setGain(TX, self._CHAN, "PAD", self._PAD_MAX)
                    # LimeSuite's RX calibration floor is 2.5 MHz (it bumps and
                    # warns below that), so never ask for less.
                    self.dev.writeSetting(d, self._CHAN, "CALIBRATE",
                                          str(self._clamp(rate, max(lo, 2.5e6), hi)))
                except Exception as e:
                    print(f"[Lime] calibrate {'TX' if d == TX else 'RX'} skipped: {e}",
                          flush=True)
                finally:
                    if d == TX:
                        self._tx_atten_db = self._apply_tx_atten(float(self.config.tx_gain))

    @staticmethod
    def _clamp(v, lo, hi):
        return max(float(lo), min(float(hi), float(v)))

    @classmethod
    def split_rx_gain(cls, g: float):
        """0..61 dB: (LNA, TIA, PGA): LNA first (lowest noise), then TIA,
        then PGA. Shared by the backend and the GUI's dropdown labels."""
        g = max(cls._PGA_MIN, min(cls.RX_GAIN_MAX_DB, float(g)))
        lna = max(0.0, min(cls._LNA_MAX, g))
        tia = max(0.0, min(cls._TIA_MAX, g - lna))
        pga = max(cls._PGA_MIN, min(cls._PGA_MAX, g - lna - tia))
        return lna, tia, pga

    def _apply_rx_gain(self, g: float) -> float:
        """0..61 dB: LNA first (lowest noise), then TIA, then PGA. PGA can go
        to -12 dB, so values below 0 attenuate; the GUI range starts at 0."""
        RX = self._S.SOAPY_SDR_RX
        lna, tia, pga = self.split_rx_gain(g)
        self.dev.setGain(RX, self._CHAN, "LNA", lna)
        self.dev.setGain(RX, self._CHAN, "TIA", tia)
        self.dev.setGain(RX, self._CHAN, "PGA", pga)
        self._rx_gain_db = lna + tia + pga
        return self._rx_gain_db

    def _apply_tx_atten(self, atten_db: float) -> float:
        """tx_gain = dB attenuation below PAD max (52 dB), applied in the PAD;
        IAMP stays at 0 dB. No digital scaling (12-bit DAC, PAD works)."""
        TX = self._S.SOAPY_SDR_TX
        att = self._clamp(atten_db, 0.0, self._PAD_MAX)
        self.dev.setGain(TX, self._CHAN, "PAD", self._PAD_MAX - att)
        self.dev.setGain(TX, self._CHAN, "IAMP", 0.0)
        self._tx_scale = None
        return att

    def set_gain_live(self, index: float) -> bool:
        try:
            g = max(0.0, min(float(self.RX_GAIN_MAX_DB), float(index)))
            self._apply_rx_gain(g)
            self.config.rx_gain = g        # LNA+VGA request (amp rides extras)
            return True
        except Exception:
            return False

    def read_sensors(self) -> dict:
        out = {}
        dev = getattr(self, "dev", None)
        if dev is None:
            return out
        S = self._S
        try:
            names = list(dev.listSensors())
            if "lms7_temp" in names:
                v = float(dev.readSensor("lms7_temp"))
                if v == v:
                    out["rx_temp_c"] = v
            if "clock_locked" in names:
                out["ref_locked"] = str(dev.readSensor("clock_locked")).lower() == "true"
        except Exception:
            pass
        for d, key in ((S.SOAPY_SDR_RX, "rx_lo_locked"), (S.SOAPY_SDR_TX, "tx_lo_locked")):
            try:
                if "lo_locked" in list(dev.listSensors(d, self._CHAN)):
                    out[key] = str(dev.readSensor(d, self._CHAN, "lo_locked")).lower() == "true"
            except Exception:
                pass
        if self._rx_gain_db is not None:
            out["rx_gain_db"] = self._rx_gain_db
        if getattr(self, "_tx_atten_db", None) is not None:
            out["tx_atten_db"] = self._tx_atten_db
        return out


# Optional external PA-enable / TX-RX-switch sequencer (rf_frontend.py).
# A backend carries it as `backend.frontend` (None by default: every helper is a
# strict no-op, so an unconfigured radio is unaffected).
try:
    from dpmg.payload.rf_frontend import FrontendError as _FrontendError
except Exception:                                   # pragma: no cover
    _FrontendError = ()


def _fe_tx_on(backend) -> None:
    """Before the first send of a TX run: switch: TX, PA on, settle.  Raises
    (FrontendError / FrontendFault / InterlockError) with the PA forced LOW, so
    the caller must not start the RF datapath when this fails."""
    fe = getattr(backend, "frontend", None)
    if fe is not None:
        fe.sequence_tx_on()


def _fe_tick(backend, n_samples: int, rate_hz: float) -> None:
    """Feed the PA watchdog after each send (busy time = samples / rate)."""
    fe = getattr(backend, "frontend", None)
    if fe is not None:
        try:
            fe.tx_tick(float(n_samples) / max(1.0, float(rate_hz)))
        except Exception:
            pass


def _fe_tx_off(backend) -> None:
    """At stop_tx: PA off (after the burst hang), settle, switch: RX.  Never raises."""
    fe = getattr(backend, "frontend", None)
    if fe is not None:
        try:
            fe.sequence_tx_off()
        except Exception as e:
            backend.health.last_error = f"frontend off: {e}"


class EpiqBackend(SDRBackend):
    # Shared Epiq Sidekiq backend (ctypes bridge) for the X40 and G40/NV100.

    def open(self) -> None:
        self.health.state = SDRDeviceState.CONNECTING
        if not _SKIQ_BRIDGE_AVAILABLE:
            self.health.state = SDRDeviceState.ERROR
            raise SDRConnectionError(
                "libsidekiq bridge unavailable on this host -- ensure libsidekiq "
                "is on LD_LIBRARY_PATH for this platform.")

        cfg = self.config
        ex = cfg.extras or {}
        card = int(ex.get("card", 0))

        if cfg.rx_antenna not in _SKIQ_RX_HDL:
            self.health.state = SDRDeviceState.ERROR
            raise SDRConnectionError(f"Unknown X40 RX antenna {cfg.rx_antenna}")
        tx_ant = cfg.tx_antenna if cfg.tx_antenna in _SKIQ_TX_HDL else "TXA1"

        self._iq_order_iq = False
        self._rx_hdl = _SKIQ_RX_HDL[cfg.rx_antenna]
        self._tx_hdl = _SKIQ_TX_HDL[tx_ant]
        # On the NV100 the J2/TRx2x
        # transmitter is skiq handle B1 (2) in dual chan_mode. The operator
        # console, checkout_common and the loopback benches name that jack
        # "TXA2"; handle A2 (1) is accepted by start_tx_streaming in dual mode
        # but radiates nothing from J2 (its only "signal" was on-card coupling
        # into the same card's RX). Keep the wire name, drive the real handle.
        if getattr(self, "_kind", "g40") != "x40" and tx_ant == "TXA2":
            self._tx_hdl = _SKIQ_TX_HDL["TXB1"]
        self._card = card
        # Housekeeping reads (card temperatures, accelerometer, GPSDO lock) run in
        # the service's worker pool and can reach libsidekiq while close() is in
        # skiq_exit releasing the cards; on a two-card session that ended the
        # whole process.  One lock serialises those reads against close.
        self._hw_lock = threading.Lock()
        self._tx_card = int(ex.get("tx_card", card))
        self._tx_rate = None                # Set by the cross-card path only
        self._seq = 0
        self._prepare_g40_clock((getattr(cfg, "clock_source", "internal") or "internal").lower())
        # Cross-card FDD (G40): RX on one NV100 + TX on the other = two independent
        # RFICs/LOs: no same-chip TX to RX LO leakage. Kept apart from the
        # single-card open() below (self._tx_card == card there, so every TX call
        # site takes the single-card case).
        if self._tx_card != card and getattr(self, "_kind", "g40") != "x40":
            return self._open_cross_card(cfg, ex, card, self._tx_card, tx_ant)

        try:
            self._skiq = SkiqBridge()
            self._skiq.init(card)
            print(f"[caps] libsidekiq runtime: v{self._skiq.lib_version()}", flush=True)

            # Apply the reference-clock setpoint before any
            # rate/LO config. "internal" is always safe; an explicit
            # external/gpsdo request that the lib or card cannot honor is a
            # hard failure (silently running on the wrong reference is worse).
            clock = (getattr(cfg, "clock_source", "internal") or "internal").lower()
            try:
                applied = self._skiq.set_ref_clock(card, self._ref_source(clock))
                self._ref_clock = applied
                print(f"[REF_CLOCK] requested={clock} applied={applied}", flush=True)
            except SkiqError as e:
                if self._ref_source(clock) != "internal":
                    self._skiq.exit()
                    self.health.state = SDRDeviceState.ERROR
                    hint = ""
                    if self._ref_source(clock) == "host":
                        hint = (". The X40 host reference needs libsidekiq 4.22.1 or later and "
                                "the Jetson and ZynqMP on BSP v3.0.0 or later; pick internal "
                                "or external until then")
                    raise SDRConnectionError(f"clock source {clock!r}: {e}{hint}")
                self._ref_clock = f"internal (apply skipped: {e})"
                print(f"[REF_CLOCK] {self._ref_clock}", flush=True)
            self._write_pps_source([card])

            # Rate + channel mode. Per SW dev manual §5.8 the multi-
            # handle rate write comes before the switch to dual: both channels
            # share one RFIC profile, so the rate must be set for both at once
            # (a per-handle write under dual returns rc=-71). Rx/Tx share the
            # clock, so this also programs the Tx rate. TX on the A2 handle
            # (transceive on one card) needs dual; RX-only / TX-on-A1 stays
            # single. Required-but-unavailable dual is a hard failure: silently
            # transmitting nothing on A2 is worse.
            bw = int(ex.get("rf_bandwidth_hz", 0)) or int(cfg.sample_rate * 0.8)
            want_dual = needs_dual_channel(cfg.rx_antenna, tx_ant, ex)
            if want_dual:
                dual_hdls = [_SKIQ_RX_HDL["RXA1"], _SKIQ_RX_HDL["RXA2"]]
                self._skiq.set_rx_rate_bw_multi(
                    card, dual_hdls, [int(cfg.sample_rate)] * 2, [bw] * 2)
                try:
                    applied = self._skiq.set_chan_mode(card, True)
                    self._chan_mode = applied
                    print(f"[CHAN_MODE] dual "
                          f"(readback: {self._skiq.read_chan_mode(card)})", flush=True)
                    # dual TX packet carries both channels' samples: use the
                    # dual block size + build [A1 zeros][A2 data] packets
                    self._skiq.tx_dual = True
                    self._skiq.tx_block_samples = _skiqmod.tx_block_for_rate(
                        cfg.sample_rate, dual=True)
                except SkiqError as e:
                    self._skiq.exit()
                    self.health.state = SDRDeviceState.ERROR
                    raise SDRConnectionError(
                        f"dual channel mode required (TX on A2) but unavailable: {e}")
            else:
                self._chan_mode = "single"
                self._skiq.tx_dual = False
                self._skiq.tx_block_samples = _skiqmod.tx_block_for_rate(cfg.sample_rate)
                self._skiq.set_rx_rate_bw(card, self._rx_hdl, int(cfg.sample_rate), bw)

            # RF port config depends on the operating mode:
            #  - dual (FDD): RX on J1 (RxA1) and TX on J2 (TxB1/TxA2) run at the
            #    same time. TRX mode can't: its rf_port_operation flip points
            #    all connectors one way. Fixed mode gives each connector its own
            #    direction (J1 dedicated RX, J2 dedicated TX), so both stream.
            #  - single (TDD same-port / RX-only): TRX mode + operation flip, so
            #    one connector can be RX then TX.
            # The X40 (4R1T) has dedicated RX/TX connectors and no
            # shared TRx port, so it is always fixed: there is no operation
            # flip. Only the G40/NV100 shared-TRx ports use the TRX+flip path.
            is_x40 = getattr(self, "_kind", "g40") == "x40"
            try:
                if want_dual or is_x40:
                    self._skiq.set_rf_port_config(card, self._skiq.RF_PORT_CONFIG_FIXED)
                    print(f"[RFPORT] config=fixed "
                          f"({'X40 dedicated RX/TX' if is_x40 else 'FDD: J1 RX + J2 TX'})",
                          flush=True)
                else:
                    self._skiq.set_rf_port_config(card, self._skiq.RF_PORT_CONFIG_TRX)
                    self._skiq.set_rf_port_operation(card, False)
                    print("[RFPORT] config=trx, operation=RX", flush=True)
            except SkiqError as _e:
                print(f"[RFPORT] port config failed: {_e}", flush=True)

            self._skiq.set_rx_lo(card, self._rx_hdl, int(cfg.center_freq))

            gmin, gmax = self._skiq.rx_gain_index_range(card, self._rx_hdl)
            gain_index = int(max(gmin, min(gmax, int(round(cfg.rx_gain)))))
            self._gain_range = (int(gmin), int(gmax))   # for acks
            self._gain_index = gain_index
            # force manual gain: under the AGC default the index
            # write below "succeeds" but is silently overridden
            try:
                self._skiq.set_rx_gain_mode(card, self._rx_hdl, True)
                print(f"[RX_GAIN_MODE] manual "
                      f"(readback: {self._skiq.read_rx_gain_mode(card, self._rx_hdl)})",
                      flush=True)
            except SkiqError as _e:
                print(f"[RX_GAIN_MODE] manual set failed: {_e} "
                      f"(gain index writes may be ignored)", flush=True)
            self._skiq.set_rx_gain_index(card, self._rx_hdl, gain_index)
            self._skiq.apply_rx_cal(card, self._rx_hdl)   # DC-offset correction only

            # assert card-native QI explicitly (defends against a prior
            # process having left the card in IQ mode). Tolerant: QI is the FPGA
            # default, so a failed write is logged, not fatal.
            try:
                self._skiq.set_iq_order(card, False)
            except SkiqError as _e:
                print(f"[QI_PIN] set_iq_order(QI) failed: {_e} (continuing; QI is FPGA default)")

            # min/max achievable rate: tells if the FPGA decimator
            # build is present (min << 50 Msps) on this X40 bitstream.
            try:
                self._rate_range = self._skiq.read_rx_rate_range(card, self._rx_hdl)
                print(f"[RATE_RANGE] {self._rate_range} Hz (rx_hdl {self._rx_hdl})", flush=True)
            except Exception as _e:
                self._rate_range = None
                print(f"[RATE_RANGE] read failed: {_e}", flush=True)

            # read back the achieved rate (card may override the requested rate)
            achieved = self._skiq.read_rx_sample_rate(card, self._rx_hdl)
            self._achieved_rate = achieved if achieved > 0 else float(cfg.sample_rate)
            self.config.sample_rate = self._achieved_rate

            # TX (best-effort: configure + attenuation; safe even if unused)
            try:
                # TX LO is its own PLL: honor an independent TX
                # center when provided (RX/TX on one card share only the
                # sample rate clock, not the LO)
                tx_lo = int(ex.get("tx_center_freq", 0) or cfg.center_freq)
                self._tx_lo = tx_lo
                self._skiq.set_tx_lo(card, self._tx_hdl, tx_lo)
                if self._chan_mode != "dual":
                    # dual already set the Tx rate via the shared-clock multi Rx
                    # write; a per-handle Tx rate write under dual returns rc=-71
                    self._skiq.set_tx_rate_bw(card, self._tx_hdl, int(self._achieved_rate), bw)
                # cfg.tx_gain is dB (GUI field "TX Gain (dB)"). skiq takes quarter-dB,
                # 0-167 (0-41.75 dB). Writing dB as quarter-dB (a clamp of 400) turns
                # 25 dB into 6.25 dB, ~19 dB hot: PA compression and an IM3 comb.
                atten = int(max(0, min(167, int(round(float(cfg.tx_gain) * 4)))))
                self._skiq.set_tx_attenuation(card, self._tx_hdl, atten)
                print(f"[TXATT] wrote {atten} qdB = {atten/4.0:.2f} dB", flush=True)
                self._skiq.configure_tx(card, self._tx_hdl)
                if self._chan_mode == "dual":
                    # dual enables both TxA1 and TxA2; the card validates every
                    # enabled TX channel's block size at start_tx_streaming, so
                    # the unused A1 handle also needs a valid (mult-of-256
                    # packet) block size or start fails "packet size ... 16380".
                    a1 = _SKIQ_TX_HDL["TXA1"]
                    if a1 != self._tx_hdl:
                        self._skiq.configure_tx(card, a1)
            except SkiqError as _txe:
                # TX config is best-effort for RX-only, but a silent pass here
                # would hide a dual-mode failure that leaves the TX block size at
                # the default (breaks A2 TX). Log it and record for the ack.
                self._tx_cfg_err = str(_txe)
                print(f"[TXCFG] TX config failed: {_txe}", flush=True)

            # RX block size is fixed: 1024 words = 1018 samples per skiq_receive()
            # (libsidekiq has no skiq_write_rx_block_size). The drain copes by
            # batching (the native drain in FullDuplexScheduler._rx_loop).
            self._skiq.start_rx(card, self._rx_hdl)
            self._skiq.start_rx(card, self._rx_hdl)
            if want_dual and not is_x40:
                self._pin_fdd_ports(card, self._rx_hdl, "open")   # same-card J2 TX pin
            # log the wire order the card actually latched at stream start
            try:
                print(f"[QI_PIN] card {card} iq_order readback: "
                      f"{self._skiq.read_iq_order(card)} (expect QI)")
            except Exception as _e:
                print(f"[QI_PIN] iq_order readback failed: {_e}")
            try:
                print(f"[RFPORT] diag: "
                      f"{self._skiq.rf_port_diag(card, self._rx_hdl, self._tx_hdl)}",
                      flush=True)
                print(f"[LO] {self._skiq.read_lo(card, self._rx_hdl, self._tx_hdl)}"
                      f" (requested rx={int(cfg.center_freq)} "
                      f"tx={getattr(self, '_tx_lo', None)})", flush=True)
            except Exception as _e:
                print(f"[RFPORT] diag failed: {_e}", flush=True)
            self.health.state = SDRDeviceState.STREAMING
        except SkiqError as e:
            self.health.state = SDRDeviceState.ERROR
            self.health.last_error = str(e)
            raise SDRConnectionError(f"X40 open failed: {e}") from e

    def _open_cross_card(self, cfg, ex, rx_card, tx_card, tx_ant) -> None:
        """Cross-card FDD: RX on rx_card + TX on tx_card = two independent NV100
        RFICs/LOs. The RX card runs single mode with its TRx flipped to RX. The
        TX card runs single mode too when TX is on the A1 handle (TRx1x; TRx
        flipped to TX lazily in send()), but dual mode when TX is on the A2
        handle (TRx2x): see the TX card notes below. Because they are separate
        cards there is no shared-port conflict and no same-chip LO leakage.
        self._chan_mode reports the RX card (what the heartbeat/ack always
        meant); self._tx_chan_mode reports the TX card."""
        bw = int(ex.get("rf_bandwidth_hz", 0)) or int(cfg.sample_rate * 0.8)
        self._chan_mode = "single"
        self._tx_chan_mode = "single"
        self._skiq = SkiqBridge()
        self._skiq.init_cards([rx_card, tx_card])
        self._skiq.tx_dual = False
        self._skiq.tx_block_samples = _skiqmod.tx_block_for_rate(cfg.sample_rate)
        print(f"[caps] libsidekiq runtime: v{self._skiq.lib_version()}", flush=True)

        clock = (getattr(cfg, "clock_source", "internal") or "internal").lower()
        for c in (rx_card, tx_card):
            # GPSDO disciplines the NV100 module's shared reference: enable it
            # once, on the RX card (where gps_status reads). Enabling it on both
            # RFICs fights the discipline and keeps it from locking (the engineering
            # single-card path enables it once, and that locks). internal/external
            # still apply to both cards.
            use = self._ref_source(clock if (clock != "gpsdo" or c == rx_card) else "internal")
            try:
                self._skiq.set_ref_clock(c, use)
            except SkiqError as _e:
                print(f"[REF_CLOCK] card{c} {use}: {_e}", flush=True)
                if use != "internal":     # as on one card: not on the wrong reference
                    self._skiq.exit()
                    self.health.state = SDRDeviceState.ERROR
                    raise SDRConnectionError(f"clock source {clock!r}: {_e}")
        self._ref_clock = clock
        self._write_pps_source([rx_card, tx_card])

        # RX on rx_card (TRX port, operation = RX)
        self._skiq.set_rx_rate_bw(rx_card, self._rx_hdl, int(cfg.sample_rate), bw)
        try:
            self._skiq.set_rf_port_config(rx_card, self._skiq.RF_PORT_CONFIG_TRX)
            self._skiq.set_rf_port_operation(rx_card, False)
        except SkiqError as _e:
            print(f"[RFPORT rx card{rx_card}] {_e}", flush=True)
        self._skiq.set_rx_lo(rx_card, self._rx_hdl, int(cfg.center_freq))
        gmin, gmax = self._skiq.rx_gain_index_range(rx_card, self._rx_hdl)
        gain_index = int(max(gmin, min(gmax, int(round(cfg.rx_gain)))))
        self._gain_range = (int(gmin), int(gmax))
        self._gain_index = gain_index
        try:
            self._skiq.set_rx_gain_mode(rx_card, self._rx_hdl, True)
        except SkiqError as _e:
            print(f"[RX_GAIN_MODE] {_e}", flush=True)
        self._skiq.set_rx_gain_index(rx_card, self._rx_hdl, gain_index)
        self._skiq.apply_rx_cal(rx_card, self._rx_hdl)   # DC-offset correction only
        try:
            self._skiq.set_iq_order(rx_card, False)
        except SkiqError:
            pass
        try:
            self._rate_range = self._skiq.read_rx_rate_range(rx_card, self._rx_hdl)
        except Exception:
            self._rate_range = None
        achieved = self._skiq.read_rx_sample_rate(rx_card, self._rx_hdl)
        self._achieved_rate = achieved if achieved > 0 else float(cfg.sample_rate)
        self.config.sample_rate = self._achieved_rate

        # The TX card has its own clock, so cross-card TX may run its
        # own sample rate (extras tx_sample_rate); default = the RX rate.
        _txr = float(ex.get("tx_sample_rate", 0) or 0)
        self._tx_rate = _txr if _txr > 0 else float(self._achieved_rate)
        tx_bw = bw if abs(self._tx_rate - self._achieved_rate) < 1.0 else int(self._tx_rate * 0.8)

        # TX on tx_card
        # The A2 TX handle (operator-console TXA2 = the TRx2x jack)
        # cannot stream while its card is in single chan_mode: libsidekiq 4.25
        # refuses start_tx_streaming with rc=-95 "Cannot stream on handle A2 in
        # single channel mode (independent tx not supported)". So the TX card
        # alone goes dual, with the sequence the single-card FDD path uses:
        # multi-handle rate write before the mode switch (per-handle writes under
        # dual return rc=-71), fixed port config so J2 is the TX jack (no
        # rf_port_operation flip in send()), the dual block size on both TX
        # handles (the card validates every enabled TX channel at
        # start_tx_streaming) and the dual [data][zeros] packing in the bridge.
        # The RX card stays single/TRX, and TX on A1 (TRx1x)
        # keeps the single-mode TRX path with the lazy operation flip in send().
        tx_lo = int(ex.get("tx_center_freq", 0) or cfg.center_freq)
        self._tx_lo = tx_lo
        # J2 transmitter (console "TXA2" or engineering "TXB1" = handle B1) only;
        # TXB1 is left out of needs_dual_channel because the X40 streams its
        # J6/TXB1 in single mode, and that rule is shared with the X40 path.
        tx_dual = tx_ant == "TXB1" or needs_dual_channel(cfg.rx_antenna, tx_ant, {})
        if tx_dual:
            try:
                self._skiq.set_rx_rate_bw_multi(
                    tx_card, [_SKIQ_RX_HDL["RXA1"], _SKIQ_RX_HDL["RXA2"]],
                    [int(self._tx_rate)] * 2, [tx_bw] * 2)
                self._tx_chan_mode = self._skiq.set_chan_mode(tx_card, True)
            except SkiqError as e:
                self._skiq.exit()
                self.health.state = SDRDeviceState.ERROR
                raise SDRConnectionError(
                    f"dual channel mode required on card{tx_card} (TX {tx_ant} = "
                    f"{g40_trx_port(tx_ant, tx_card)}) but unavailable: {e}") from e
            self._skiq.tx_dual = True
            self._skiq.tx_block_samples = _skiqmod.tx_block_for_rate(
                self._tx_rate, dual=True)
            print(f"[CHAN_MODE] card{tx_card} dual for TX {tx_ant} "
                  f"(readback: {self._skiq.read_chan_mode(tx_card)}); "
                  f"RX card{rx_card} single", flush=True)
        try:
            if tx_dual:
                self._skiq.set_rf_port_config(tx_card, self._skiq.RF_PORT_CONFIG_FIXED)
                print(f"[RFPORT tx card{tx_card}] config=fixed (J2 TX)", flush=True)
            else:
                self._skiq.set_rf_port_config(tx_card, self._skiq.RF_PORT_CONFIG_TRX)
            self._skiq.set_tx_lo(tx_card, self._tx_hdl, tx_lo)
            if not tx_dual:
                # dual already set the Tx rate via the shared-clock multi write
                self._skiq.set_tx_rate_bw(tx_card, self._tx_hdl, int(self._tx_rate), tx_bw)
                self._skiq.tx_block_samples = _skiqmod.tx_block_for_rate(self._tx_rate)
            atten = int(max(0, min(167, int(round(float(cfg.tx_gain) * 4)))))
            self._skiq.set_tx_attenuation(tx_card, self._tx_hdl, atten)
            self._skiq.configure_tx(tx_card, self._tx_hdl)
            if tx_dual:
                a1 = _SKIQ_TX_HDL["TXA1"]
                if a1 != self._tx_hdl:
                    self._skiq.configure_tx(tx_card, a1)
            print(f"[TXATT] card{tx_card} wrote {atten} qdB = {atten/4.0:.2f} dB", flush=True)
        except SkiqError as _txe:
            self._tx_cfg_err = str(_txe)
            print(f"[TXCFG xcard{tx_card}] {_txe}", flush=True)

        self._skiq.start_rx(rx_card, self._rx_hdl)
        try:
            # read-only, so the working cross-card TX card's routing
            # can be compared with the same-card one in the service log
            print(f"[RFPORT tx card{tx_card}] diag: "
                  f"{self._skiq.rf_port_diag(tx_card, self._rx_hdl, self._tx_hdl)}", flush=True)
        except Exception as _e:
            print(f"[RFPORT tx card{tx_card}] diag failed: {_e}", flush=True)
        print(f"[XCARD] RX card{rx_card} {cfg.rx_antenna} (single) + TX card{tx_card} "
              f"{tx_ant} ({self._tx_chan_mode}) @ RX {self._achieved_rate/1e6:.3f} / "
              f"TX {self._tx_rate/1e6:.3f} Msps "
              f"(independent LOs)", flush=True)
        self.health.state = SDRDeviceState.STREAMING

    def recv(self) -> AnalysisFrame:
        cfg = self.config
        res = self._skiq.receive(self._card, self._iq_order_iq)
        if res is None:
            return AnalysisFrame(np.zeros(0, dtype=np.complex64),
                                 self._achieved_rate, cfg.center_freq, self._seq)
        iq, rf_ts = res
        self._seq += 1
        self.health.rx_frames += 1
        self.health.overruns = getattr(self._skiq, "rx_overruns",
                                       {}).get(self._card, 0)  # overrun count
        return AnalysisFrame(iq, self._achieved_rate, cfg.center_freq, self._seq,
                             rf_timestamp=rf_ts)
    def native_drain_ok(self) -> bool:
        """Librxdrain loaded (else the Python per-block path)."""
        nd = getattr(self._skiq, "native_drain", None)
        return bool(nd is not None and nd() is not None)

    def recv_batch(self, max_blocks, budget_s):
        """One native call drains up to max_blocks blocks.
        Returns (complex64 chunk or None, rxdrain state)."""
        chunk, st = self._skiq.receive_batch(self._card, max_blocks, budget_s,
                                             self._iq_order_iq)
        self.health.overruns = getattr(self._skiq, "rx_overruns",
                                       {}).get(self._card, 0)
        return chunk, st

    def recv_raw(self):
        """Raw int16 + timestamp, no conversion: for the batched RX path."""
        res = self._skiq.receive_raw(self._card)
        # Surface timestamp-gap overruns into health so the
        # v2 STATUS heartbeat carries the real overrun count
        self.health.overruns = getattr(self._skiq, "rx_overruns",
                                       {}).get(self._card, 0)
        return res

    def _same_card_fdd(self) -> bool:
        """G40 single-card FDD: RX and TX on one NV100 in dual chan_mode (fixed
        ports, RX J1 + TX J2). The X40 has dedicated ports and never applies."""
        return (getattr(self, "_kind", "g40") != "x40"
                and getattr(self, "_tx_card", None) == getattr(self, "_card", None)
                and getattr(self, "_chan_mode", "single") == "dual")

    def _pin_fdd_ports(self, card, rx_hdl, where: str) -> dict:
        """Same-card dual TX on a "2" jack radiates nothing from J2
        (the level at TRx1A is identical with the RX jack open, i.e. on-card
        coupling), while the same handle/packing radiates cross-card. Structural difference: only the same-card path configures and
        starts a receiver on the transmitting card. In fixed port config a port is
        RX or TX, never both (skiq_rf_port_config_fixed), and on the NV100 the
        receive handles RxA2/RxB1 also map to J2; this path never told the card
        that J2 belongs to TxB1. So assign it explicitly with the public per-handle
        routing calls: RX handle to J1, TX handle to J2 (only if J2 is in the
        handle's fixed-port list), RxB1 off J2 to J1 if it reads back J2 (the SW
        manual allows RxB1 on J1 in fixed mode only). Called after start_rx in
        open() and again right before start_tx_streaming. Non-fatal: every
        failure is logged and the radio keeps its previous routing.
        On hardware it does not cure this: RxA2/RxB1 move to J1 fine, but
        write_tx_rf_port_for_hdl(B1, J2) is rc=-95 (TX routing is fixed on the
        NV100, tx_port already reads J2) and the tone stays on-card coupling
        (+3 dB with the pin). So it is opt-in for experiments only:
        DPMG_J2_TX_PIN=1. Default off."""
        out = {"where": where}
        sk = getattr(self, "_skiq", None)
        if sk is None or os.environ.get("DPMG_J2_TX_PIN", "0").strip() != "1":
            out["skipped"] = "disabled" if sk is not None else "no bridge"
            return out
        J1 = getattr(sk, "RF_PORT_J1", 0)
        J2 = getattr(sk, "RF_PORT_J2", 1)
        rx_b1 = _SKIQ_RX_HDL.get("RXB1", 2)

        def _do(key, fn, *a):
            f = getattr(sk, fn, None)
            if f is None:
                out[key] = "n/a"
                return None
            try:
                r = f(*a)
                out[key] = "ok" if r is None else r
                return r
            except SkiqError as e:
                out[key] = f"err: {e}"
                return None

        if rx_hdl is not None:
            _do("rx->J1", "set_rx_rf_port", card, rx_hdl, J1)
        avail = _do("tx_fixed_avail", "tx_rf_ports_avail", card, self._tx_hdl)
        if isinstance(avail, (tuple, list)) and avail and J2 not in list(avail[0]):
            out["tx->J2"] = f"not offered (fixed list {list(avail[0])})"
        else:
            _do("tx->J2", "set_tx_rf_port", card, self._tx_hdl, J2)
        if rx_hdl != rx_b1 and _do("rxB1_port", "read_rx_rf_port", card, rx_b1) == J2:
            _do("rxB1->J1", "set_rx_rf_port", card, rx_b1, J1)
        _do("tx_port", "read_tx_rf_port", card, self._tx_hdl)
        if rx_hdl is not None:
            _do("rx_port", "read_rx_rf_port", card, rx_hdl)
        print(f"[J2_TX_PIN {where}] card{card} rx_hdl {rx_hdl} tx_hdl {self._tx_hdl}: {out}",
              flush=True)
        self._j2_pin = out
        return out

    def _tx_card_dual(self) -> bool:
        """chan_mode of the card that transmits: the TX card's own mode on the
        cross-card path, else the single card's mode."""
        return (getattr(self, "_tx_chan_mode", None)
                or getattr(self, "_chan_mode", "single")) == "dual"

    def _tx_start_error(self, e) -> str:
        """The libsidekiq refusal, worded for the GUI toast. The
        lib prints its own reason only to the service log, so name the known
        one (rc=-95 EOPNOTSUPP on the A2 handle = card not in dual chan_mode)."""
        msg = f"start_tx_streaming on card{self._tx_card} handle {self._tx_hdl}: {e}"
        if "rc=-95" in str(e) and self._tx_hdl in (_SKIQ_TX_HDL.get("TXA2", 1),
                                                   _SKIQ_TX_HDL.get("TXB1", 2)):
            hname = "B1" if self._tx_hdl == _SKIQ_TX_HDL.get("TXB1", 2) else "A2"
            msg += (f" -- libsidekiq: 'Cannot stream on handle {hname} in single channel "
                    "mode (independent tx not supported)'; the TX card must be in "
                    "dual chan_mode for TRx2x TX (card now "
                    f"{'dual' if self._tx_card_dual() else 'single'})")
        return msg

    def send(self, samples: np.ndarray) -> None:
        try:
            # Build the TX blocks before the PA is keyed or the watchdog fed: a 1 s
            # waveform at 61.44 MSps took longer to build than watchdog_ms and the
            # PA dropped with TX still running (RL-042)
            self._skiq.prepare_tx(samples, self._iq_order_iq)
            # TX streaming starts lazily on the first send. skiq_transmit into a
            # non-streaming TX datapath returns status 12, so start streaming here
            # rather than at open (keeps the DAC idle until the first send).
            if not getattr(self, "_tx_streaming", False):
                _fe_tx_on(self)                     # PA keyed before RF
                # Card-level rf_port_operation points the TRx connectors at the
                # transmitter. Needed for single-mode TX (TxA1 on J1), but on
                # the dual/B1 path it would also yank J1 away from a live RX:
                # so skip it there and let start_tx_streaming(B1) route J2.
                if not self._tx_card_dual():
                    try:
                        self._skiq.set_rf_port_operation(self._tx_card, True)
                        print("[RFPORT] operation -> TX", flush=True)
                    except SkiqError as _e:
                        print(f"[RFPORT] set TX op failed: {_e}", flush=True)
                elif self._same_card_fdd():
                    self._pin_fdd_ports(self._tx_card, self._rx_hdl, "start_tx")  # same-card J2 TX pin
                try:
                    self._skiq.start_tx(self._tx_card, self._tx_hdl)
                except SkiqError as e:
                    # Raise on a refused start: swallowing it makes the
                    # scheduler re-call send() flat out and libsidekiq logs the
                    # same error ~130x/s. Raising lets the TX loop back off and
                    # give up after a few tries, and the ack carries the reason.
                    msg = self._tx_start_error(e)
                    self.health.last_error = msg
                    raise SDRStreamError(msg) from e
                self._tx_streaming = True
            # Feed the PA watchdog before the blocking transmit (it covers
            # the whole send: a 3 s burst at a 500 ms watchdog would otherwise be dropped
            # mid-burst) and again after it returns.
            _rate = (getattr(self, "_tx_rate", None)          # cross-card TX may run its own rate
                     or getattr(self, "_achieved_rate", self.config.sample_rate))
            _fe_tick(self, len(samples), _rate)
            self._skiq.transmit(self._tx_card, self._tx_hdl, samples, self._iq_order_iq)
            self.health.tx_frames += 1
            _fe_tick(self, len(samples), _rate)
        except SkiqError as e:
            # Surface mid-stream failures the same way, so the
            # TX loop's backoff + give-up bound applies instead of a hot retry.
            self.health.last_error = str(e)
            raise SDRStreamError(str(e)) from e

    def stop_tx(self) -> None:
        """Halt the radio TX datapath through the bridge's stop_tx
        (skiq_stop_tx_streaming); send() starts it lazily and this is what stops
        it. Safe to call even if TX was never started."""
        _fe_tx_off(self)                            # PA off before RX routing
        try:
            if getattr(self, "_tx_streaming", False):
                self._skiq.stop_tx(self._tx_card, self._tx_hdl)
        except Exception as e:
            self.health.last_error = str(e)
        finally:
            self._tx_streaming = False
            # TDD: return the TRx ports to the receiver so RX can resume.
            try:
                self._skiq.set_rf_port_operation(self._tx_card, False)
                print("[RFPORT] operation -> RX", flush=True)
            except Exception as _e:
                print(f"[RFPORT] set RX op failed: {_e}", flush=True)

    def restart_rx_stream(self) -> bool:
        """Wedge watchdog stage 1: stop + start the card's RX stream on the RX card
        (self._card is the RX card in both the single-card and cross-card paths).
        TX on either card keeps streaming. Caller must have stopped the drain
        thread first: skiq_receive must not run across the restart."""
        if getattr(self, "_skiq", None) is None:
            return False
        self._skiq.stop_rx(self._card, self._rx_hdl)
        self._skiq.start_rx(self._card, self._rx_hdl)
        print(f"[WEDGE] card{self._card} RX stream restarted", flush=True)
        return True

    def set_frequency_live(self, hz: float) -> bool:
        if getattr(self, "_skiq", None) and self.health.state == SDRDeviceState.STREAMING:
            old = float(self.config.center_freq)
            self._skiq.set_rx_lo(self._card, self._rx_hdl, int(hz))
            # move the TX LO with it or TX keeps radiating at the old center and
            # vanishes from the retuned RX view. Preserve any intentional TX/RX
            # offset set at open (tx_center_freq); default co-tuned (offset 0).
            tx_hdl = getattr(self, "_tx_hdl", None)
            if tx_hdl is not None:
                tx_off = float(getattr(self, "_tx_lo", old)) - old
                new_tx = int(round(hz + tx_off))
                try:
                    self._skiq.set_tx_lo(getattr(self, "_tx_card", self._card),
                                         tx_hdl, new_tx)
                    self._tx_lo = float(new_tx)
                except Exception as e:
                    self.health.last_error = f"tx retune: {e}"
            self.config.center_freq = float(hz)
            return True
        return False

    def set_tx_frequency_live(self, hz: float) -> bool:
        # TX LO is its own PLL (same as tx_center_freq at open); RX stays put
        tx_hdl = getattr(self, "_tx_hdl", None)
        if getattr(self, "_skiq", None) is None or tx_hdl is None:
            return False
        try:
            self._skiq.set_tx_lo(getattr(self, "_tx_card", self._card), tx_hdl,
                                 int(round(float(hz))))
            self._tx_lo = float(int(round(float(hz))))
            return True
        except Exception as e:
            self.health.last_error = f"tx retune: {e}"
            return False

    def set_gain_live(self, index: float) -> bool:
        if getattr(self, "_skiq", None) and self.health.state == SDRDeviceState.STREAMING:
            gmin, gmax = self._skiq.rx_gain_index_range(self._card, self._rx_hdl)
            idx = int(max(gmin, min(gmax, int(round(index)))))
            self._skiq.set_rx_gain_index(self._card, self._rx_hdl, idx)
            self.config.rx_gain = float(idx)
            return True
        return False

    def set_tx_attenuation_qdb(self, qdb: int) -> bool:
        """Live TX attenuation in quarter-dB units (0-167 =
        0-41.75 dB; 0 = max power). The GUI control finds it through
        hasattr(); without it attenuation changes do nothing. Works whether
        or not TX is streaming."""
        if getattr(self, "_skiq", None) is None:
            return False
        try:
            self._skiq.set_tx_attenuation(self._tx_card, self._tx_hdl, int(qdb))
            return True
        except Exception as e:
            self.health.last_error = str(e)
            return False

    def rx_gain_table(self):
        # (index, gain_db) over the valid index range at the current LO.
        # gain_db is nominal RX gain from the documented 0.5 dB/index step (not the
        # cal offset, which is a separate power-calibration quantity).
        if not getattr(self, "_skiq", None):
            return []
        gmin, gmax = self._skiq.rx_gain_index_range(self._card, self._rx_hdl)
        lo = int(self.config.center_freq)
        table = []
        for ix in range(int(gmin), int(gmax) + 1):
            db = (ix - int(gmin)) * 0.5   # nominal RX gain dB (0.5 dB/index step, per libsidekiq docs)
            table.append((ix, db))
        return table

    def gps_status(self) -> dict:
        """GPSDO lock + external-ref (10 MHz / 1PPS) presence, read
        live off the card for the GUI indicator. Requires an inited card."""
        if getattr(self, "_skiq", None) is None:
            return {"supported": False, "err": "card not initialized"}
        try:
            st = self._skiq.read_gps_status(self._card)
            st["supported"] = True
            if getattr(self, "_kind", "g40") != "x40":
                self._add_clock_status(st)
            else:
                # the X40 has no FPGA GPSDO to ask: say which choice is in use and
                # leave the lock unknown; the GNSS fix rides along from its gpsd
                c = getattr(self, "_clock_choice", None) or {}
                st["clock"] = {"choice": c.get("token"), "label": c.get("label"), "ref": c.get("ref")}
                if c.get("ref") == "host":
                    st["gpsdo_locked"] = None
                    st["gpsdo_note"] = "the X40 GPSDO lock cannot be read through libsidekiq"
            return st
        except Exception as e:
            return {"supported": False, "err": str(e)}

    def _ref_source(self, clock):
        """What to select on the card for a clock token. On a G40 the external 10 MHz
        reaches the cards through the carrier clock (carrier edge, fed by the HMC7044
        on its input 1), not their own external input, which fails PLL lock and stops
        the process (RL-030). The 1 PPS part of the token is PCC.00, not a card select.
        On an X40 x40_gpsdo is the host reference (the GPSDO in its GPS module), never
        skiq_gpsdo_enable, which an X40 refuses with -95."""
        c = _g40_clock.CLOCK_CHOICES.get(clock)
        ref = c["ref"] if c else clock
        if getattr(self, "_kind", "g40") == "x40":
            return ref
        return "carrier" if ref == "external" else ref

    def _prepare_g40_clock(self, clock):
        """Before the cards open, in the order the Epiq integration guide gives:
        resolve the choice, set the HMC7044 input for it (input 1 for an external
        10 MHz, back to input 0 for anything else) and confirm its PLL lock, then set
        the 1 PPS line PCC.00. The card's own selects follow in open(), after
        skiq_init. Refuses the init when a step an external choice depends on fails;
        a step the choice does not depend on is logged and the init goes on."""
        self._clock_choice = None
        self._hmc = {}
        self._pps_line = None
        self._meter = None
        self._meas = {}
        kind = getattr(self, "_kind", "g40")
        choice = _g40_clock.resolve(clock, kind)
        if choice.get("err"):
            self.health.state = SDRDeviceState.ERROR
            raise SDRConnectionError(choice["err"])
        self._clock_choice = choice
        if kind == "x40":
            return
        ex = dict(getattr(self.config, "extras", None) or {})
        want = _g40_clock.HMC_INPUT_EXT if choice["ref"] == "external" else _g40_clock.HMC_INPUT_GPS
        st = _g40_clock.hmc_state()
        print(f"[REF_CLOCK] {choice['token']}: {choice['label']}; HMC7044 now "
              f"input {st.get('input')} ({st.get('source')}), want input {want}", flush=True)
        if ex.get("hmc_set_by_hand"):
            # the operator ran hmc_from_file at a shell; take their word, the clock
            # count against the 1 PPS still checks it
            _g40_clock.hmc_record_by_hand(want)
            st = _g40_clock.hmc_state()
            print("[REF_CLOCK] HMC7044 taken as set by hand", flush=True)
        elif want == _g40_clock.HMC_INPUT_EXT or st.get("input") != want:
            # write for an external 10 MHz every time, so the PLL lock is read fresh;
            # for the rest only when the chip is (or may be) on the other input
            w = _g40_clock.hmc_write_input(want)
            print(f"[REF_CLOCK] hmc_from_file input {want}: ok={w['ok']} locked={w['locked']} "
                  f"{w.get('err') or w.get('output', '')[-120:]}", flush=True)
            if want == _g40_clock.HMC_INPUT_EXT and not w["ok"]:
                if w.get("err") is None:
                    # the chip took input 1 but did not lock: no usable 10 MHz. Put it
                    # back on the GPS clock so the carrier is not left on a dead input.
                    _g40_clock.hmc_write_input(_g40_clock.HMC_INPUT_GPS)
                    why = ("the HMC7044 did not lock to the 10 MHz on REF (hmc_from_file: "
                           f"{w.get('output', '')[-160:]}). Check the 10 MHz cable and level, "
                           "then init again; or pick 1 PPS only or internal.")
                else:
                    why = w["err"]
                self.health.state = SDRDeviceState.ERROR
                raise SDRConnectionError(f"clock source {choice['token']!r}: {why}")
            st = _g40_clock.hmc_state()
        self._hmc = st
        external_pps = choice["pps"] == "external"
        try:
            _g40_clock.set_pps_line(external_pps)
            self._pps_line = "external" if external_pps else "internal"
        except Exception as e:
            print(f"[REF_CLOCK] 1 PPS line {_g40_clock.PPS_LINE}: {e}", flush=True)
            if external_pps:
                self.health.state = SDRDeviceState.ERROR
                raise SDRConnectionError(f"clock source {choice['token']!r}: 1 PPS line not set: {e}")

    def _write_pps_source(self, cards):
        """After skiq_init: the cards take their 1 PPS from the carrier pin, which the
        API calls external (the guide's step 4). Not fatal: the default on these
        cards already reads external."""
        if getattr(self, "_kind", "g40") == "x40":
            return
        for c in cards:
            try:
                r = self._skiq.set_pps_source(int(c), "external")
                print(f"[REF_CLOCK] card{c} 1 PPS source: {r}", flush=True)
            except Exception as e:
                print(f"[REF_CLOCK] card{c} 1 PPS source not written: {e}", flush=True)

    def clock_measure(self) -> dict:
        """One read of the last-1PPS timestamp, fed to the clock meter: the card
        clock against the 1 PPS and whether the pulse is there. For the service's
        clock loop (every second or two); returns the current clock dict."""
        with self._hw_guard():
            if getattr(self, "_skiq", None) is None:
                return self.clock_status()
            try:
                if self._meter is None:
                    self._meter = _g40_clock.ClockMeter(self._skiq.sys_timestamp_freq(self._card))
                ts = self._skiq.read_last_1pps(self._card)
            except Exception:
                ts = None
        if self._meter is not None:
            self._meas = self._meter.feed(None if ts is None else ts[1])
        return self.clock_status()

    def clock_status(self) -> dict:
        """The clock as the GUI and the logs see it: the choice, what the HMC7044 is
        on, the measured card clock against the 1 PPS and the 10 MHz verdict."""
        c = getattr(self, "_clock_choice", None) or {}
        hmc = getattr(self, "_hmc", None) or {}
        meter = getattr(self, "_meter", None)
        # the meter's view right now, so a pulse that stopped reads absent even
        # between two reads
        m = meter.result() if meter is not None else (getattr(self, "_meas", None) or {})
        if getattr(self, "_kind", "g40") == "x40":
            # no HMC7044 on an X40: the G40 verdict would read LOST for want of input 1
            v = {"ten_mhz": None, "why": ""}
        else:
            v = _g40_clock.ten_mhz_verdict(c, hmc, m)
        return {"choice": c.get("token"), "label": c.get("label"), "ref": c.get("ref"),
                "pps": c.get("pps"), "pps_line": getattr(self, "_pps_line", None),
                "hmc_input": hmc.get("input"), "hmc_source": hmc.get("source"),
                "hmc_locked": hmc.get("locked"),
                "pps_present": m.get("pps_present"), "pps_age_s": m.get("pps_age_s"),
                "clock_ppm": None if m.get("ppm") is None else round(m["ppm"], 3),
                "ticks_per_s": m.get("ticks_per_s"),
                "ten_mhz": v["ten_mhz"], "why": v["why"]}

    def _add_clock_status(self, st):
        """gps_status on a G40: the clock dict rides along, and the fields the older
        readers look at (ext_ref_present, pps_present, pps_line, ref_clock) follow it."""
        cs = self.clock_status()
        st["clock"] = cs
        st["pps_line"] = cs["pps_line"]
        if cs.get("pps_present") is not None:
            st["pps_present"] = cs["pps_present"]
        if cs.get("ref") == "external":
            st["ext_ref_present"] = cs["ten_mhz"] == "in use"
            if st.get("ref_clock") == "carrier":
                st["ref_clock"] = "external (carrier)"
        st["ten_mhz"] = cs["ten_mhz"]
        st["clock_ppm"] = cs["clock_ppm"]

    def _hw_guard(self):
        """The housekeeping lock, or a fresh one when open() never got as far as
        creating it (nothing can be racing in that case)."""
        lock = getattr(self, "_hw_lock", None)
        if lock is None:
            lock = self._hw_lock = threading.Lock()
        return lock

    def gpsdo_locked(self):
        """Fast GPSDO lock for the 1 Hz status heartbeat (no 1PPS probe)."""
        with self._hw_guard():
            if getattr(self, "_skiq", None) is None:
                return None
            try:
                return self._skiq.gpsdo_locked(self._card)
            except Exception:
                return None

    def ref_locked(self):
        """Reference health for the heartbeat, by clock choice: GPSDO lock on gpsdo;
        on an external 10 MHz a G40 goes by the HMC7044 lock and the measured card
        clock against the 1 PPS (None until measured, False when the 10 MHz is lost),
        an X40 by the card running with the external select; None on internal and on
        1 PPS only, where there is nothing to be locked to, and on the X40 GPSDO,
        whose lock libsidekiq cannot read."""
        clk = (getattr(self.config, "clock_source", None) or "internal").lower()
        c = _g40_clock.CLOCK_CHOICES.get(clk) or {}
        if c.get("ref", clk) == "gpsdo":
            return self.gpsdo_locked()
        if c.get("ref") == "host":
            return None
        if c.get("ref", clk) != "external":
            return None
        if getattr(self, "_kind", "g40") != "x40":
            v = self.clock_status().get("ten_mhz")
            return True if v == "in use" else False if v == "LOST" else None
        with self._hw_guard():
            if getattr(self, "_skiq", None) is None:
                return None
            try:
                return self._skiq.ext_ref_present(self._card)
            except Exception:
                return None

    def card_temps(self) -> dict:
        """On-card temperature of the RX card and, when different, the
        TX card, via skiq_read_temp. Cards that return no reading are left out."""
        with self._hw_guard():
            if getattr(self, "_skiq", None) is None:
                return {}
            cards = [int(self._card)]
            tx = getattr(self, "_tx_card", None)
            if tx is not None and int(tx) not in cards:
                cards.append(int(tx))
            out = {}
            for c in cards:
                try:
                    t = self._skiq.read_temp(c)
                except Exception:
                    t = None
                if t is not None:
                    out[c] = t
            return out

    def accel(self):
        """(x, y, z) in g from the RX card's accelerometer, or None."""
        with self._hw_guard():
            if getattr(self, "_skiq", None) is None:
                return None
            try:
                return self._skiq.read_accel(int(self._card))
            except Exception:
                return None

    def close(self) -> None:
        # Under the housekeeping lock: a temperature or accelerometer read in the
        # worker pool must not reach libsidekiq while skiq_exit is releasing the
        # cards (on a two-card session that ended the whole process, exit 0, no
        # trace).  _skiq is dropped once the lib has exited, so a read that was
        # waiting on the lock finds nothing to do.
        with self._hw_guard():
            try:
                if getattr(self, "_skiq", None) is not None:
                    # stop_rx must not be able to skip skiq_exit: if it throws (seen
                    # tearing down a gpsdo session), the card would never be released
                    # and the next skiq_init returns rc=-16 (busy), e.g. on a clock switch.
                    try:
                        if self.health.state == SDRDeviceState.STREAMING:
                            self._skiq.stop_rx(self._card, self._rx_hdl)
                    except Exception as e:
                        self.health.last_error = f"stop_rx during close: {e}"
                    self._skiq.exit()
                    self._skiq = None
            except Exception as e:
                self.health.last_error = str(e)
            finally:
                self.health.state = SDRDeviceState.DISCONNECTED
 


# USB radios: how to spot each one in /sys/bus/usb/devices. A device matches on
# vendor (and product, when listed) or on its USB product string. A B200 that
# was just plugged in is the Cypress FX3 boot loader (04b4:00f3) until UHD loads
# its firmware, then 2500:xxxx with product "Unknown", hence the vendor-only
# Ettus entry. The HackRF Pro shares the HackRF One id (1d50:6089). Keep in step
# with deploy/usb_hotplug/dpmg-usb-sync in the Super Nano repo.
_USB_RADIOS = {
    "uhd": ({("2500", None), ("3923", "7813"), ("3923", "7814"), ("04b4", "00f3")},
            ("B200", "B210", "USRP")),
    "hackrf": ({("1d50", "6089"), ("1d50", "604b"), ("1d50", "cc15")}, ("HackRF", "rad1o")),
    "lime": ({("0403", "601f"), ("1d50", "6108"), ("04b4", "00f1")}, ("LimeSDR",)),
    "pluto": ({("0456", "b673"), ("0456", "b674")}, ("PlutoSDR",)),
}
_USB_KIND = {"b200": "uhd", "uhd": "uhd", "ettus": "uhd",
             "hackrf": "hackrf", "hackrf_pro": "hackrf",
             "limesdr": "lime", "lime": "lime", "limesdr_mini": "lime",
             "pluto": "pluto", "plutosdr": "pluto", "adalm": "pluto"}
USB_SYSFS = "/sys/bus/usb/devices"


def usb_radio_present(kind: str, root: str = USB_SYSFS):
    """True/False for a USB radio kind (is one plugged in), None for kinds that
    aren't checked (G40/X40 cards, network radios, mock) or when sysfs can't be
    read. Cheap: reads two or three small files per USB device."""
    fam = _USB_KIND.get(str(kind or "").lower())
    if fam is None:
        return None
    ids, names = _USB_RADIOS[fam]
    try:
        devs = os.listdir(root)
    except OSError:
        return None

    def _rd(d, f):
        try:
            with open(os.path.join(root, d, f)) as fh:
                return fh.read().strip()
        except OSError:
            return ""
    for d in devs:
        vid = _rd(d, "idVendor").lower()
        if not vid:
            continue
        pid = _rd(d, "idProduct").lower()
        if (vid, pid) in ids or (vid, None) in ids:
            return True
        prod = _rd(d, "product")
        if prod and any(n.lower() in prod.lower() for n in names):
            return True
    return False


def make_backend(kind: str, config: SDRConfig, simulate: bool) -> SDRBackend:
    if simulate:
        return MockBackend(config)
    # UHD kinds cover any Ettus radio the UHD 4.10 API enumerates (X410, B2xx,
    # N3xx); device_args in the config picks the actual unit.
    cls = {"b200": UhdBackend, "uhd": UhdBackend, "ettus": UhdBackend,
           "x410": UhdBackend, "x40": EpiqBackend,
           "g40": EpiqBackend,
           "pluto": PlutoBackend, "plutosdr": PlutoBackend,
           "adalm": PlutoBackend,
           "xsdr": SoapyBackend, "usdr": SoapyBackend,
           "soapy": SoapyBackend,
           "hackrf": HackRFBackend, "hackrf_pro": HackRFBackend,
           "limesdr": LimeSDRBackend, "lime": LimeSDRBackend,
           "limesdr_mini": LimeSDRBackend}.get(kind, MockBackend)
    b = cls(config)
    b._kind = kind          # EpiqBackend branches on this (X40 4R1T vs G40 NV100)
    return b


def _gain_values(rng: dict, label=None) -> list:
    """Expand a caps gain range {min,max,step} into the explicit [value, label]
    list the GUI renders as a dropdown, so an operator can only pick values
    the radio supports. `label(v)` customizes the text (e.g. the LNA/VGA split
    the backend will actually apply)."""
    lo, hi = float(rng.get("min", 0)), float(rng.get("max", 0))
    step = float(rng.get("step", 1.0)) or 1.0
    n = int(round((hi - lo) / step))
    out = []
    for i in range(n + 1):
        v = round(lo + i * step, 4)
        v = int(v) if float(v).is_integer() else v
        out.append([v, label(v) if label else f"{v:g} dB"])
    return out


def _gain_labels_for(k: str, caps: dict) -> tuple:
    """Per-personality label functions (rx, tx) for the gain dropdowns."""
    tmode = (caps.get("tx_gain") or {}).get("mode", "gain")
    if k in ("hackrf", "hackrf_pro"):
        def rx(v):
            lna, vga = HackRFBackend.split_rx_gain(v)
            return f"{v:g} dB  (LNA {lna} + VGA {vga})"
        tx = lambda v: f"{v:g} dB atten  (TX VGA {HackRFBackend._TX_VGA_MAX - v:g} dB)"
        return rx, tx
    if k in ("limesdr", "lime"):
        def rx(v):
            lna, tia, pga = LimeSDRBackend.split_rx_gain(v)
            return f"{v:g} dB  (LNA {lna:g} + TIA {tia:g} + PGA {pga:g})"
        tx = lambda v: f"{v:g} dB atten  (PAD {LimeSDRBackend._PAD_MAX - v:g} dB)"
        return rx, tx
    rx = lambda v: f"{v:g} dB"
    tx = (lambda v: f"{v:g} dB atten") if tmode == "atten" else (lambda v: f"{v:g} dB")
    return rx, tx


def gain_choices(kind: str, caps: dict) -> tuple:
    """(rx_values, tx_values) for the GUI's gain dropdowns: every setting the
    personality supports, as [[value, label], ...], expanded locally from the
    caps ranges. Not embedded in the caps reply: that reply must
    fit one MAX_DATAGRAM (1400 B) and the lists run to 10 KB (B200 TX has
    360 quarter-dB steps). link_tab imports this; a GUI without sdr_core falls
    back to a plain min..max..step expansion."""
    k = (kind or "").lower()
    rxl, txl = _gain_labels_for(k, caps or {})
    return (_gain_values((caps or {}).get("rx_gain") or {}, rxl),
            _gain_values((caps or {}).get("tx_gain") or {}, txl))


def backend_caps(kind: str) -> dict:
    """Capability descriptor by kind; small enough for one UDP datagram (the
    GUI expands gain value lists itself via gain_choices)."""
    return _backend_caps_kind(kind)


def _backend_caps_kind(kind: str) -> dict:
    """Static capability descriptor by radio kind, so the GUI can render the
    right controls before the device is opened. `family` tells the GUI which
    control set to draw ("uhd" = single-port dB-gain Ettus, "epiq" = multi-port
    index-gain Sidekiq). Gain `unit`/`mode` say how the slider value is sent:
    UHD wants dB gain; Epiq wants a gain index + TX attenuation."""
    k = (kind or "mock").lower()
    if k in ("b200", "ettus", "uhd"):
        return {
            "kind": k, "family": "uhd", "label": "Ettus B200mini" if k == "b200" else "Ettus/UHD",
            # RX can use either port (RX2 default); TX is TX/RX only
            "rx_antennas": ["RX2", "TX/RX"], "tx_antennas": ["TX/RX"],
            # on-device verified ranges (get_rx/tx_gain_range, bandwidth_range, freq_range)
            "rx_gain": {"min": 0.0, "max": 76.0, "step": 1.0, "unit": "dB"},
            "tx_gain": {"min": 0.0, "max": 89.75, "step": 0.25, "unit": "dB", "mode": "gain"},
            "clock_sources": ["internal", "external"],          # 10 MHz frequency reference
            "time_sources": ["none", "internal", "external"],   # 1 PPS time reference (independent)
            "rx_bandwidth_hz": {"min": 200e3, "max": 56e6},
            "tx_bandwidth_hz": {"min": 200e3, "max": 56e6},
            "has_gpsdo": False,
            "sensors": ["ref_locked", "rx_lo_locked", "rx_rssi_dbfs",
                        "rx_temp_c", "tx_lo_locked", "tx_temp_c"],
            "sample_rates_hz": list(B200_VALID_RATES_HZ),   # on-device verified, 1-61.44 MSps
            "usable_bw_factor": 0.8,                         # ~RF bandwidth shown = rate * this
            "tune_hz": {"min": 42e6, "max": 6008e6},
            # UHD quality hooks the GUI should surface (sent back via extras)
            "quality_hooks": {"dc_offset": True, "iq_balance": True, "lo_offset": True,
                              "rx_bandwidth": True, "tx_bandwidth": True},
        }
    if k == "x410":
        return {
            "kind": k, "family": "uhd", "label": "Ettus X410",
            "rx_antennas": ["RX0", "RX1"], "tx_antennas": ["TX0", "TX1"],
            "rx_gain": {"min": 0.0, "max": 60.0, "step": 1.0, "unit": "dB"},
            "tx_gain": {"min": 0.0, "max": 60.0, "step": 1.0, "unit": "dB", "mode": "gain"},
            "clock_sources": ["internal", "external", "gpsdo"],
            "time_sources": ["internal", "external", "gpsdo"],
            "has_gpsdo": True,
            "sample_rates_hz": list(X40_VALID_RATES_HZ),
            "tune_hz": {"min": 1e6, "max": 8e9},
        }
    if k in ("pluto", "plutosdr", "adalm"):
        return {
            "kind": k, "family": "pluto", "label": "ADALM-PLUTO",
            "rx_antennas": ["RX"], "tx_antennas": ["TX"],
            "rx_gain": {"min": 0.0, "max": 71.0, "step": 1.0, "unit": "dB"},
            "tx_gain": {"min": 0.0, "max": 89.75, "step": 0.25, "unit": "dB", "mode": "atten"},
            "clock_sources": ["internal"], "time_sources": ["internal"],
            "has_gpsdo": False,
            # ADALM-PLUTO (AD9363): the chip accepts a continuous 521 kSps..61.44
            # MSps (521 kSps is the hard floor, every value latches exact). But the Pluto's USB2 link only streams clean
            # to ~5 MSps, higher rates drop samples, so the GUI dropdown is capped
            # at 5 MSps (don't offer rates the radio can't sustain). The hardware
            # still accepts higher for short bursts; raise USABLE_MAX if a faster
            # transport ever exists (it won't over USB2).
            "sample_rates_hz": [521_000, 1_000_000, 2_000_000, 3_840_000, 5_000_000],
            "rate_range_hz": {"min": 521_000, "max": 5_000_000},
            "usable_bw_factor": 0.8,                 # ~RF bandwidth shown = rate * this
            "tune_hz": {"min": 325e6, "max": 3800e6},
        }
    if k in ("xsdr", "usdr", "soapy"):
        # family "soapy" is caps-driven like "pluto": dB RX gain, TX attenuation.
        # Ranges from the usdr Soapy module (xsdr_ranges, lms7_gains); a generic
        # "soapy" radio gets the same descriptor until it is probed. On the xSDR
        # over USB 2.0, 0.2-11 MSps streams continuously, 100% delivered,
        # 0 overflows; <0.2 MSps fails (MMCM
        # timeout). CS16 caps at 11.26 MSps (silent drops) so SoapyBackend
        # switches to CS12 above 11 MSps (12 MSps 100%, ~15.0 MSps ceiling).
        return {
            "kind": k, "family": "soapy",
            "label": "Wavelet xSDR" if k in ("xsdr", "usdr") else "SoapySDR",
            "rx_antennas": ["AUTO", "LNAH", "LNAL", "LNAW"],
            "tx_antennas": ["AUTO", "TXH", "TXW"],
            # overall RX = LNA 0..30 + TIA 0..12 + PGA -12..19: -12..61 dB
            "rx_gain": {"min": 0.0, "max": 61.0, "step": 1.0, "unit": "dB"},
            "tx_gain": {"min": 0.0, "max": 52.0, "step": 1.0, "unit": "dB", "mode": "atten"},
            "clock_sources": ["internal", "external"], "time_sources": ["internal"],
            "has_gpsdo": False,
            "sensors": ["rx_temp_c", "tx_atten_db"],
            # continuous 0.2-15 MSps over USB (anything in range latches exactly);
            # these are the dropdown presets. The radio itself does 122.88 MSps
            # SISO: only over PCIe, which does not link on the Orin.
            "sample_rates_hz": [250_000, 500_000, 1_000_000, 1_536_000, 1_920_000,
                                2_000_000, 2_500_000, 3_072_000, 3_840_000,
                                4_000_000, 5_000_000, 6_144_000, 7_680_000,
                                8_000_000, 10_000_000, 12_000_000, 12_288_000],
            "rate_range_hz": {"min": 200_000, "max": 15_000_000},
            "rx_bandwidth_hz": {"min": 0.5e6, "max": 125e6},
            "tx_bandwidth_hz": {"min": 0.5e6, "max": 125e6},
            "usable_bw_factor": 0.8,
            "tune_hz": {"min": 0.1e6, "max": 3800e6},
        }
    if k in ("hackrf", "hackrf_pro"):
        # family "soapy": dB RX gain, TX attenuation, caps-driven controls.
        # HackRF One / HackRF Pro (legacy mode): 8-bit, 2-20 MSps, half-duplex,
        # one TX/RX port. RX gain = LNA 0..40 + VGA 0..62 (amp via extras
        # rx_amp); TX atten 0..47 over the TX VGA. Baseband filter auto-tracks
        # the rate (~0.75x); external 10 MHz is auto-detected by the hardware.
        return {
            "kind": k, "family": "soapy", "label": "HackRF Pro / One",
            "rx_antennas": ["TX/RX"], "tx_antennas": ["TX/RX"],
            "rx_gain": {"min": 0.0, "max": float(HackRFBackend.RX_GAIN_MAX_DB),
                        "step": 2.0, "unit": "dB"},
            "tx_gain": {"min": 0.0, "max": 47.0, "step": 1.0, "unit": "dB", "mode": "atten"},
            # one clock select (internal); an external 10 MHz on CLKIN is
            # auto-detected by the hardware and reported by the ref_source
            # sensor. No 1 PPS input: no time source to choose.
            "clock_sources": ["internal"], "time_sources": [],
            "ext_ref_auto": True,
            "has_gpsdo": False, "half_duplex": True,
            "sensors": ["rx_gain_db", "tx_atten_db", "tx_active", "rx_amp",
                        "tx_amp", "bias_tx", "ref_source"],
            # front-end switches the GUI offers (ride init via extras):
            # rx_amp / tx_amp = the +14 dB RF amps, bias_tee = 3.3 V on the
            # port (extras bias_tx), rx_bandwidth = manual baseband filter
            # (extras rx_bw_hz, 1.75-28 MHz; omitted = auto ~0.75 x rate).
            "fe_hooks": {"rx_amp": True, "tx_amp": True, "bias_tee": True,
                         "rx_bandwidth": True},
            "rate_warn_below_hz": 8_000_000,
            # operator list = specified rates only (>= 8 MSps); the engineering
            # view gets sample_rates_hz_all (2-20 MSps, low rates flagged) for
            # bench work, and rate_range_hz lets scripts request any 2-20 MSps.
            "sample_rates_hz": [8_000_000, 10_000_000, 12_288_000, 15_360_000,
                                16_000_000, 20_000_000],
            "sample_rates_hz_all": [2_000_000, 3_072_000, 3_840_000, 4_000_000,
                                    5_000_000, 6_144_000, 7_680_000, 8_000_000,
                                    10_000_000, 12_288_000, 15_360_000, 16_000_000,
                                    20_000_000],
            "rate_range_hz": {"min": 2_000_000, "max": 20_000_000},
            # hackrf.readthedocs.io/en/latest/sampling_rate.html: below 8 MSps the
            # MAX5864 is unspecified and the 1.75 MHz baseband filter cannot
            # reject aliases (only ~33 dB at +-2 MHz for a 2 MSps capture), so
            # the GUI defaults to 8 MSps; lower rates stay selectable.
            "default_rate_hz": 8_000_000,
            "rx_bandwidth_hz": {"min": 1.75e6, "max": 28e6},
            "tx_bandwidth_hz": {"min": 1.75e6, "max": 28e6},
            "usable_bw_factor": 0.75,
            "tune_hz": {"min": 1e6, "max": 6000e6},
        }
    if k in ("limesdr", "lime", "limesdr_mini"):
        # LimeSDR Mini v1 (LMS7002M), family "soapy". Figures from LimeSuite
        # src/API/LimeSDR_mini.cpp + lms7_device.cpp: 10 MHz-3.5 GHz, 100 kSps-
        # 30.72 MSps, full duplex, RX paths LNAW (<1.7 GHz) / LNAH ("Auto"
        # switches), TX BAND2 (<2 GHz) / BAND1, RX gain LNA+TIA+PGA 0..61 dB,
        # TX PAD 0..52 dB (attenuation here), internal 40 MHz VCTCXO only.
        return {
            "kind": k, "family": "soapy", "label": "LimeSDR Mini (LMS7002M)",
            "rx_antennas": ["Auto", "LNAW", "LNAH"],
            "tx_antennas": ["Auto", "BAND2", "BAND1"],
            "rx_gain": {"min": 0.0, "max": float(LimeSDRBackend.RX_GAIN_MAX_DB),
                        "step": 1.0, "unit": "dB"},
            "tx_gain": {"min": 0.0, "max": 52.0, "step": 1.0, "unit": "dB", "mode": "atten"},
            "clock_sources": ["internal"], "time_sources": ["internal"],
            "has_gpsdo": False, "half_duplex": False,
            "sensors": ["ref_locked", "rx_lo_locked", "tx_lo_locked", "rx_temp_c",
                        "rx_gain_db", "tx_atten_db"],
            # every entry latches exactly with 0 overruns;
            # below ~1.4 MSps the analog LPF cannot follow, so the backend
            # enables the LMS7 GFIR digital low-pass at the rate there.
            "sample_rates_hz": [100_000, 200_000, 250_000, 500_000, 1_000_000,
                                1_920_000, 2_000_000, 2_500_000, 3_072_000,
                                3_840_000, 4_000_000, 5_000_000, 6_144_000, 7_680_000,
                                8_000_000, 10_000_000, 12_288_000, 15_360_000,
                                20_000_000, 30_720_000],
            "rate_range_hz": {"min": 100_000, "max": 30_720_000},
            "default_rate_hz": 7_680_000,
            "rx_bandwidth_hz": {"min": 1.4e6, "max": 130e6},
            "tx_bandwidth_hz": {"min": 5e6, "max": 130e6},
            "usable_bw_factor": 0.8,
            "tune_hz": {"min": 10e6, "max": 3500e6},
            # LMS7 RX DC corrector + DC/IQ calibration; explicit LPF widths
            "quality_hooks": {"dc_offset": True, "iq_balance": True,
                              "rx_bandwidth": True, "tx_bandwidth": True},
        }
    if k in ("g40", "x40"):
        rates = G40_VALID_RATES_HZ if k == "g40" else X40_VALID_RATES_HZ
        return {
            "kind": k, "family": "epiq", "label": k.upper(),
            # Sidekiq ports are selectable per-card; the GUI keeps its existing
            # per-kind port logic for these (family "epiq").
            "rx_antennas": [], "tx_antennas": [],
            "rx_gain": {"min": 195, "max": 255, "step": 1, "unit": "idx"},
            "tx_gain": {"min": 0, "max": 60, "step": 1, "unit": "dB", "mode": "atten"},
            "clock_sources": list(_g40_clock.G40_CHOICES if k == "g40" else _g40_clock.X40_CHOICES),
            "clock_labels": _g40_clock.labels(k),
            "time_sources": ["internal", "external", "gpsdo"] if k == "g40" else ["internal", "external"],
            # the G40's NV100 cards have the FPGA GPSDO; the X40's GPSDO is in its
            # GPS module and libsidekiq cannot drive or read it (x40_gpsdo selects it)
            "has_gpsdo": k == "g40",
            "sample_rates_hz": list(rates),
            "tune_hz": {"min": 70e6, "max": 6e9},
        }
    return {"kind": k, "family": "mock", "label": "Mock",
            "rx_antennas": [], "tx_antennas": [],
            "rx_gain": {"min": 0, "max": 255, "step": 1, "unit": "idx"},
            "tx_gain": {"min": 0, "max": 60, "step": 1, "unit": "dB", "mode": "atten"},
            "clock_sources": ["internal"], "time_sources": ["internal"],
            "has_gpsdo": False, "sample_rates_hz": list(G40_VALID_RATES_HZ),
            "tune_hz": {"min": 70e6, "max": 6e9}}


# Full-duplex scheduler  (full_duplex_scheduler.py)

class RXCapture:
    # Fmt "c64" (complex64, default) | "iq16" | "qi16".
    # Epiq int16 packing matches the encoder convention: LE uint32 per pair,
    # 'iq' = I in the high half. full_scale 32767 (16-bit) or 8191 (14-bit).
    def __init__(self, path: str, fmt: str = "c64", full_scale: float = 32767.0,
                 meta: dict = None):
        self.path = Path(path)
        self.fmt = fmt if fmt in ("c64", "iq16", "qi16") else "c64"
        self.full_scale = float(full_scale)
        self.meta = dict(meta or {})
        self._fh = open(self.path, "wb")
        self.frames = 0
        self.samples = 0

    def write(self, frame: AnalysisFrame) -> None:
        s = frame.samples
        if self.fmt == "c64":
            s.tofile(self._fh)
        else:
            fsv = self.full_scale
            iv = (np.clip(np.round(s.real * fsv), -fsv - 1, fsv)
                  .astype(np.int64) & 0xFFFF)
            qv = (np.clip(np.round(s.imag * fsv), -fsv - 1, fsv)
                  .astype(np.int64) & 0xFFFF)
            if self.fmt == "qi16":
                words = ((qv << 16) | iv).astype("<u4")   # Q hi, I lo
            else:
                words = ((iv << 16) | qv).astype("<u4")   # I hi, Q lo (encoder conv)
            words.tofile(self._fh)
        self.frames += 1
        self.samples += len(s)

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass
        try:  # Sidecar metadata
            import json as _json, time as _time
            side = dict(self.meta)
            side.update({"format": self.fmt, "full_scale": self.full_scale,
                         "frames": self.frames, "samples": self.samples,
                         "written_utc": _time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                       _time.gmtime())})
            Path(str(self.path) + ".json").write_text(_json.dumps(side, indent=2))
        except Exception:
            pass


class FullDuplexScheduler:
    # Consecutive send() failures tolerated before the TX thread
    # gives up (each retry waits 0.1 s, so a refused start_tx_streaming costs 3
    # libsidekiq log lines and ~0.3 s, inside the service's 0.5 s start check).
    tx_max_consecutive_errors = 3
    def __init__(self, backend: SDRBackend, tx_mode: TXMode = TXMode.STOPPED,
                 capture: Optional[RXCapture] = None, rx_pace_s: float = 0.05):
        self.backend = backend
        self.tx_mode = tx_mode
        self.capture = capture
        self.rx_pace_s = rx_pace_s
        self.metrics = SchedulerMetrics()
        self._frames: deque = deque(maxlen=4)
        self.frame_hook = None    # optional channelizer tap
        self._lock = threading.Lock()
        self._stop = threading.Event()          # legacy: stops both
        self._rx_stop = threading.Event()       # independent RX stop
        self._tx_stop = threading.Event()       # independent TX stop
        self._rx: Optional[threading.Thread] = None
        self._tx: Optional[threading.Thread] = None
        self._tx_wave: Optional[np.ndarray] = None
        # a live TX source: a callable returning the next IQ block (the bitpipe
        # stream modem). When set, _tx_loop sends what it returns instead of
        # repeating _tx_wave. Keying still goes through start_tx.
        self.tx_source = None
        # Wedge watchdog test hook: while time < _inject_replay_until, every RX
        # block is treated as a replay (as if the FIFO had wedged). Cleared by
        # restart_rx_stream() unless _inject_survives_stream (forces a re-init).
        self._inject_replay_until = 0.0
        self._inject_survives_stream = False

    def inject_rx_replay(self, hold_s: float, survives_stream: bool = False) -> None:
        """Simulate a wedged RX FIFO for hold_s seconds (BIT/selftest only)."""
        self._inject_survives_stream = bool(survives_stream)
        self._inject_replay_until = time.time() + float(hold_s)

    def restart_rx_stream(self) -> bool:
        """Wedge watchdog stage 1: stop the drain thread, restart the card's RX
        stream (FIFO reset), restart the drain. Unlike stop_rx() this leaves any
        open RX capture file alone. Returns False if the backend can't do it."""
        was_running = self._rx is not None and self._rx.is_alive()
        self._rx_stop.set()
        if self._rx and self._rx.is_alive():
            self._rx.join(timeout=2.0)
        self._rx = None
        try:
            ok = bool(self.backend.restart_rx_stream())
        finally:
            # new stream epoch: no gap/replay against pre-restart timestamps
            self._last_blk_ts = None
            self._rx_replay_t = None
            if not self._inject_survives_stream:
                self._inject_replay_until = 0.0
            if was_running:
                self.start_rx()
        return ok

    def set_tx_source(self, source, allow_live: bool = False) -> None:
        """Install (or clear, with None) the live TX block source. Like
        set_tx_waveform it refuses to swap the source under a running TX unless
        the caller says so; clearing is always allowed and stops the stream at
        the next block (the loop then idles, nothing is repeated)."""
        if source is not None and not allow_live and self._tx is not None and self._tx.is_alive():
            raise WaveformError("TX is active -- STOP transmit before changing the TX source")
        self.tx_source = source

    def set_tx_waveform(self, samples: np.ndarray, allow_live: bool = False) -> None:
        # Loading a waveform must not radiate on its own. _tx_loop re-reads
        # self._tx_wave every iteration, so if a TX stream is already running the
        # new buffer would be picked up and transmitted immediately: without any
        # explicit Start TX. Refuse the swap while TX is live unless the caller
        # explicitly opts in (reactive/transceive paths that key TX themselves).
        if not allow_live and self._tx is not None and self._tx.is_alive():
            raise WaveformError(
                "TX is active -- STOP transmit before loading a new waveform")
        self._tx_wave = samples

    # independent RX/TX control
    def start_rx(self) -> None:
        if self._rx and self._rx.is_alive():
            return
        self._rx_stop.clear(); self._stop.clear()
        if self.metrics.started == 0:
            self.metrics = SchedulerMetrics(started=time.time())
        self.backend.health.state = SDRDeviceState.STREAMING
        self._rx = threading.Thread(target=self._rx_loop, daemon=True)
        self._rx.start()

    def stop_rx(self) -> None:
        self._rx_stop.set()
        if self._rx and self._rx.is_alive():
            self._rx.join(timeout=1.0)
        self._rx = None
        if not (self._tx and self._tx.is_alive()):
            self.backend.health.state = SDRDeviceState.IDLE
        if self.capture:
            try: self.capture.close()
            except Exception: pass

    def start_tx(self) -> None:
        if self._tx and self._tx.is_alive():
            return
        if self._tx_wave is None and self.tx_source is None:
            raise WaveformError("no TX waveform set -- load a waveform before Start TX")
        self._tx_stop.clear(); self._stop.clear()
        if self.metrics.started == 0:
            self.metrics = SchedulerMetrics(started=time.time())
        self.backend.health.state = SDRDeviceState.STREAMING
        # half-duplex backends (HackRF) hold TX between sends when CONTINUOUS
        # so a jammer is gapless; burst modes return to RX after each send.
        hold = getattr(self.backend, "set_tx_hold", None)
        if callable(hold):
            try:
                hold(self.tx_mode == TXMode.CONTINUOUS)
            except Exception as e:
                self.backend.health.last_error = str(e)
        fe = getattr(self.backend, "frontend", None)         # CONTINUOUS keeps
        if fe is not None:                                    # the PA keyed; SINGLE_SHOT keys
            try:                                              # per burst with hang time
                fe.set_hold(self.tx_mode == TXMode.CONTINUOUS)
            except Exception as e:
                self.backend.health.last_error = str(e)
        self._tx = threading.Thread(target=self._tx_loop, daemon=True)
        self._tx.start()

    def stop_tx(self) -> None:
        self._tx_stop.set()
        if self._tx and self._tx.is_alive():
            self._tx.join(timeout=1.0)
        self._tx = None
        # actually halt the radio TX datapath (not just the python loop)
        stop_fn = getattr(self.backend, "stop_tx", None)
        if callable(stop_fn):
            try: stop_fn()
            except Exception as e: self.backend.health.last_error = str(e)
        if not (self._rx and self._rx.is_alive()):
            self.backend.health.state = SDRDeviceState.IDLE

    # legacy combined API (kept for callers that still use it)
    def start(self) -> None:
        self.start_rx()
        if self.tx_mode != TXMode.STOPPED and self._tx_wave is not None:
            self.start_tx()

    def stop(self) -> None:
        self._stop.set()
        self.stop_tx()
        self.stop_rx()
        self.backend.health.state = SDRDeviceState.IDLE

    def latest(self) -> Optional[AnalysisFrame]:
        with self._lock:
            return self._frames[-1] if self._frames else None

    def rx_degraded(self, window_s: float = 0.5) -> bool:
        """True if the RX recently replayed an older buffer: the hardware
        timestamp jumped backward, which happens when the card's FIFO wedges and
        ping-pongs duplicate blocks. While degraded the frames are stale/dup
        (the PSD appears frozen), so detection/analysis should be suspended
        rather than acting on data that isn't actually new."""
        t = getattr(self, "_rx_replay_t", None)
        return t is not None and (time.time() - t) < window_s

    def _rx_loop(self) -> None:
        _rxstop = lambda: self._stop.is_set() or self._rx_stop.is_set()
        
        import numpy as _np
        import sys as _sys, os as _os, gc as _gc
        _sys.setswitchinterval(0.0005)   # GIL slice 0.5ms << 271ms FIFO (default is 5ms)
        try:
            _gc.freeze()                 # shrink the collector's scan set
            _gc.disable()                # no GC pauses mid-drain
        except Exception:
            pass
        # RX drain is the most latency-critical thread: the card hardware FIFO overflows
        # (seen as RXGAP splices) if the drain loop's Python glue can't run promptly. The worker
        # thread holds the GIL in long numpy/FFT/decode bursts, and the TX thread runs at
        # SCHED_FIFO 10: so at default priority the RX thread is starved by both. Give it
        # SCHED_FIFO 15 (above TX and worker) so the OS preempts them for RX between card
        # reads, keeping the drain fed. Same pattern as _tx_loop.
        try:
            if _os.environ.get("DPMG_RX_NO_RT"):      # diagnostic: plain priority
                raise PermissionError("DPMG_RX_NO_RT set")
            _os.sched_setscheduler(0, _os.SCHED_FIFO, _os.sched_param(15))
            print("[rx] SCHED_FIFO priority 15 set for RX drain thread", flush=True)
        except (PermissionError, OSError) as e:
            try:
                _os.nice(-15)
                print(f"[rx] SCHED_FIFO unavailable ({e}); nice -15 applied", flush=True)
            except OSError:
                print(f"[rx] no scheduling privileges ({e}); default priority", flush=True)
        # Optionally pin the skiq_receive thread to one core. Epiq's known
        # issue "Stale Sample Data on NVIDIA Jetson" is mitigated by pinning the
        # skiq_receive caller to a single core. DPMG_RX_CPU=<core> enables it (unset =
        # no pinning). sched_setaffinity(0) applies to this thread.
        _pin = _os.environ.get("DPMG_RX_CPU", "").strip()
        if _pin:
            try:
                _os.sched_setaffinity(0, {int(_pin)})
                print(f"[rx] RX drain thread pinned to CPU {int(_pin)}", flush=True)
            except (ValueError, OSError) as e:
                print(f"[rx] DPMG_RX_CPU={_pin!r} not applied ({e})", flush=True)
        # a backend whose transport delivers big fixed blocks (HackRF: 131072
        # samples per USB transfer = 65 ms at 2 MSps) legitimately drains at
        # that cadence; let it raise the warning threshold above the default.
        _batch_s = max(0.02, float(getattr(self.backend, "_rx_batch_s", 0.05)))
        _warn_s = max(0.05, 2.0 * _batch_s,
                      float(getattr(self.backend, "_rx_loop_warn_s", 0.0)))
        # On the Epiq backend with librxdrain loaded, one native
        # call drains the whole batch (skiq_receive loop, int16 to complex64,
        # QI swap, timestamp checks in C, GIL released). The NV100 block is fixed
        # at 1018 samples, so 61.44 MSps needs 60,354 receives a second; the
        # Python per-block loop below manages ~17-20 us each on the G40 against
        # a 16.57 us budget (see rxdrain.c). Python path = fallback.
        _nat_fn = None
        try:
            if getattr(self.backend, "native_drain_ok", None) and self.backend.native_drain_ok():
                _nat_fn = self.backend.recv_batch
        except Exception as e:
            print(f"[rx] native drain check failed ({e}); Python drain", flush=True)
        if getattr(self.backend, "recv_raw", None) is not None:
            _why = getattr(getattr(self.backend, "_skiq", None), "native_why", "")
            print(f"[rx] drain engine: {'native' if _nat_fn else 'python'} ({_why})", flush=True)
        _engine = getattr(self.backend, "drain_engine", "native") if _nat_fn else "py"
        while not _rxstop():
            _now = time.time()
            _prev = getattr(self, "_last_drain_t", None)
            if _prev is not None and (_now - _prev) > _warn_s:
                print(f"[RXLOOP] {1000*(_now-_prev):.0f} ms since last drain", flush=True)
            self._last_drain_t = _now
            drained = 0
            _card_empty = False
            _t0 = time.time()
            batch_samples = []
            chunk = None
            batch_ts = None
            batch_sr = None
            batch_fc = None
            last_fr = None
            _raw_fn = getattr(self.backend, "recv_raw", None)
            if _nat_fn is not None:
                try:
                    chunk, _st = _nat_fn(2048, _batch_s)
                except Exception as e:
                    self.backend.health.last_error = str(e)
                    self.backend.health.state = SDRDeviceState.ERROR
                    return
                drained = int(_st.n_blocks)
                _card_empty = bool(_st.empty)
                if drained:
                    batch_ts = int(_st.ts0)
                    batch_sr = getattr(self.backend, "_achieved_rate",
                                       self.backend.config.sample_rate)
                    batch_fc = self.backend.config.center_freq
                    if _st.n_mismatch:
                        _more = (f" (+{_st.n_mismatch - 1} more in this batch)"
                                 if _st.n_mismatch > 1 else "")
                        print(f"[RXGAP] expected ts {_st.first_exp}, got {_st.first_got} "
                              f"({int(_st.first_got) - int(_st.first_exp):+d} samples) "
                              f"-- frame will be SPLICED{_more}", flush=True)
                    if _st.n_back:                       # replaying FIFO (see below)
                        self._rx_replay_t = time.time()
                    if self._inject_replay_until and time.time() < self._inject_replay_until:
                        self._rx_replay_t = time.time()  # wedge watchdog selftest
                    self._last_blk_ts = int(_st.last_ts)
                    self._last_blk_n = int(_st.last_n)
                    self.metrics.frames += drained
            # A batch is also bounded by wall time: a backend whose recv() waits
            # for the next block instead of returning empty (UHD at low rates,
            # SoapySDR) never trips the empty-card break, and 2048 blocks at
            # 2 MSps is 4 s of samples: the end-of-batch work then stalls the
            # USB receive long enough to overflow (B200: "[RXLOOP] 4219 ms since
            # last drain" and 67 overflows/s).
            while _nat_fn is None and drained < 2048 and (time.time() - _t0) < _batch_s:
                if _raw_fn is None:                      # MockBackend / B200: per-block path
                    try:
                        fr = self.backend.recv()
                    except Exception as e:
                        self.backend.health.last_error = str(e)
                        self.backend.health.state = SDRDeviceState.ERROR
                        return
                    if fr is None or len(getattr(fr, "samples", [])) == 0:
                        _card_empty = True
                        break
                    batch_samples.append(_np.asarray(fr.samples, dtype=_np.complex64))
                    _ts = getattr(fr, "rf_timestamp", None)
                    _n_cplx = len(fr.samples)
                    if batch_ts is None:
                        batch_ts = _ts; batch_sr = fr.sample_rate; batch_fc = fr.center_freq
                    last_fr = fr
                else:                                    # Epiq: raw int16, convert once below
                    try:
                        res = _raw_fn()
                    except Exception as e:
                        self.backend.health.last_error = str(e)
                        self.backend.health.state = SDRDeviceState.ERROR
                        return
                    if res is None:
                        _card_empty = True
                        break
                    _raw, _ts = res
                    _n_cplx = len(_raw) // 2
                    batch_samples.append(_raw)
                    if batch_ts is None:
                        batch_ts = _ts
                        batch_sr = getattr(self.backend, "_achieved_rate",
                                           self.backend.config.sample_rate)
                        batch_fc = self.backend.config.center_freq
                if _ts is not None and getattr(self, "_last_blk_ts", None) is not None:
                    _exp = self._last_blk_ts + self._last_blk_n
                    if _ts != _exp:
                        print(f"[RXGAP] expected ts {_exp}, got {_ts} "
                              f"({_ts - _exp:+d} samples) -- frame will be SPLICED", flush=True)
                    # a backward jump means the card replayed an older buffer: the
                    # content is stale/duplicate (a wedged FIFO ping-ponging two
                    # blocks), not fresh RX. The PSD looks frozen; mark the stream
                    # degraded so consumers (the detector) skip it instead of
                    # emitting detections off stale data.
                    if _ts < self._last_blk_ts:
                        self._rx_replay_t = time.time()
                if self._inject_replay_until and time.time() < self._inject_replay_until:
                    self._rx_replay_t = time.time()    # wedge watchdog selftest
                self._last_blk_ts = _ts
                self._last_blk_n = _n_cplx
                self.metrics.frames += 1
                drained += 1
            _t1 = time.time()
            # after the drain: assemble one contiguous chunk and feed once
            if batch_samples:
                if _raw_fn is not None:
                    # One conversion for the whole batch. Per-block cost was 36 us against a
                    # 33 us RF budget: that is numpy's ~9 us fixed call overhead x4 on a
                    # 2036-element array, not the data volume. Batching 2048 blocks amortizes
                    # it ~2000x. batch_samples holds raw int16 on the Epiq path.
                    _r = _np.concatenate(batch_samples) if len(batch_samples) > 1 else batch_samples[0]
                    if not getattr(self.backend, "_iq_order_iq", True):
                        # QI to IQ: swap the two int16 of each sample as one uint32
                        # rotate (same result as reshape(-1, 2)[:, ::-1], which costs
                        # 3x more on the drain thread). _r is a private copy.
                        _v = _r.view(_np.uint32)
                        _v[:] = (_v >> 16) | (_v << 16)
                    _f = _r.astype(_np.float32)
                    _f *= (1.0 / 32768.0)
                    chunk = _f.view(_np.complex64)
                else:
                    chunk = _np.concatenate(batch_samples) if len(batch_samples) > 1 else batch_samples[0]
            if chunk is not None:
                try:
                    bf = AnalysisFrame(chunk, batch_sr, batch_fc, self.backend._seq,
                                       rf_timestamp=batch_ts)
                except Exception:
                    bf = last_fr  # fallback: at least feed something valid
                with self._lock:
                    self._frames.append(bf)
                hook = self.frame_hook
                if hook is not None:
                    try:
                        hook(bf)   # one feed per batch
                    except Exception:
                        pass
                if self.capture:
                    try:
                        self.capture.write(bf)
                    except Exception:
                        pass
            if drained > 0:
                _dt_ms = 1000.0 * (_t1 - _t0)
                _post_ms = 1000.0 * (time.time() - _t1)
                # RF time this batch represents, at the actual sample rate (a fixed
                # 30.72 MSps reads ~15x low at low rates and looks like a drain stall).
                _sr = float(batch_sr or 30_720_000.0)
                _rf_ms = drained * self._last_blk_n * 1000.0 / max(_sr, 1.0)
                _r = _rf_ms / max(_dt_ms, 1e-6)
                if not hasattr(self, "_drain_n"):
                    self._drain_n = 0; self._drain_rf = 0.0; self._drain_wall = 0.0
                    self._drain_post = 0.0
                self._drain_n += 1; self._drain_rf += _rf_ms; self._drain_wall += _dt_ms
                self._drain_post += _post_ms
                if self._drain_n % 50 == 0:
                    # ratio / cumulative ratio = the receive loop alone;
                    # post = convert + frame hook on this thread, during which
                    # nothing is drained; cycle ratio counts both (must stay > 1)
                    _cyc = self._drain_rf / max(self._drain_wall + self._drain_post, 1e-6)
                    print(f"[DRAIN] {drained} blks, {_rf_ms:.1f} ms RF in {_dt_ms:.1f} ms wall "
                          f"(ratio {_r:.2f})  cumulative ratio {self._drain_rf/max(self._drain_wall,1e-6):.2f}"
                          f"  post {_post_ms:.1f} ms  cycle ratio {_cyc:.2f}  [{_engine}]",
                          flush=True)

            if not _card_empty:
                time.sleep(0)
            else:
                time.sleep(self.rx_pace_s)     # idle: sleep the normal pace

    def _tx_loop(self) -> None:
    
        import sys as _sys, os as _os, gc as _gc
        _sys.setswitchinterval(0.0005)          # GIL slice << FIFO depth
        try:
            _gc.freeze()                        # shrink the collector's scan set
            _gc.disable()                       # no GC pauses mid-stream
        except Exception:
            pass
        try:
            _os.sched_setscheduler(0, _os.SCHED_FIFO, _os.sched_param(10))
            print("[tx] SCHED_FIFO priority set for TX thread", flush=True)
        except (PermissionError, OSError) as e:
            try:
                _os.nice(-10)
                print(f"[tx] SCHED_FIFO unavailable ({e}); nice -10 applied", flush=True)
            except OSError:
                print(f"[tx] no scheduling privileges ({e}); default priority", flush=True)
        
        _fails = 0
        while not (self._stop.is_set() or self._tx_stop.is_set()):
            src = self.tx_source
            # a live source is one continuous stream: backends that frame each
            # send as a burst (UHD) keep the burst open between sends
            self.backend.tx_stream_mode = src is not None
            if src is not None:
                try:
                    wave = src()                 # live stream: one block per pass
                except Exception as e:           # noqa: BLE001
                    self.backend.health.last_error = f"tx source: {e}"
                    self.metrics.underruns += 1
                    time.sleep(0.05)
                    continue
            else:
                wave = self._tx_wave
            if wave is None or len(wave) == 0:
                time.sleep(0.1)                  # idle: nothing to send yet
                continue
            try:
                self.backend.send(wave)
                self.metrics.repeats += 1
                _fails = 0
            except Exception as e:                   # pragma: no cover
                self.backend.health.last_error = str(e)
                self.metrics.underruns += 1
                if isinstance(e, _FrontendError):    # PA refused/failed (fault,
                    print(f"[tx] front-end refused TX: {e}", flush=True)  # interlock, GPIO):
                    break                            # stop, do not re-key every 100 ms
                _fails += 1
                if _fails >= int(self.tx_max_consecutive_errors):
                    print(f"[tx] giving up after {_fails} consecutive send failures: {e}",
                          flush=True)
                    break
                time.sleep(0.1)                  # backoff: don't hot-loop a failing card
                continue
            if self.tx_mode == TXMode.SINGLE_SHOT:
                break
        end_burst = getattr(self.backend, "end_tx_burst", None)
        if callable(end_burst):
            try:
                end_burst()                     # close a streamed burst (UHD)
            except Exception:
                pass
        self.backend.tx_stream_mode = False
        if self.tx_mode == TXMode.SINGLE_SHOT:
            try:
                self.backend.stop_tx()          # halt TX datapath after the single pass
            except Exception:
                pass
        try:
            _gc.enable()                        # resume GC after TX stops
        except Exception:
            pass
