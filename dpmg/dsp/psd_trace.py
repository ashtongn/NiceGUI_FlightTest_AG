"""PSD computation and trace recording for the SDR RX path.

Three capabilities:
  1. Live PSD: Welch-averaged power spectral density from a rolling IQ buffer.
  2. Raw IQ capture (optional): the complex samples themselves (large).
  3. Trace recording: the computed PSD (freq bins + power dB) per frame, each
     tagged with the radio's  hardware timestamp, saved compactly and replayable to
     redraw the PSD-over-time without keeping the raw IQ.

Trace vs raw IQ
---------------
A "trace" is the spectral picture (freqs[], psd_db[]) at one instant plus its
hardware time. It is tiny compared to the raw IQ it was computed from, and it is
exactly what you replay to redraw the PSD plot. Raw IQ lets you recompute
anything later but is heavy; traces are the lightweight, replayable product.

Hardware timestamps
-------------------
Each RX block carries ``rf_timestamp`` (a 64-bit counter clocked at the
sample rate). time_seconds = rf_timestamp / sample_rate gives the precise time
of the frame from the radio's own clock (not wall-clock). The backend should
populate ``AnalysisFrame.rf_timestamp``; if absent, fall back to seq*chunk.

"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional

import numpy as np

try:                                   # complex64 FFTs, about 2x numpy on the Orin
    from scipy.fft import fft as _fft
except ImportError:                    # pragma: no cover
    _fft = np.fft.fft

# segments per batched FFT: 32 x 4096 complex64 is 1 MB of scratch
PSD_FFT_BATCH = 32


# PSD computation

def compute_psd(iq: np.ndarray, sample_rate: float, center_freq: float = 0.0,
                nfft: int = 4096, window: str = "hann",
                overlap: float = 0.5) -> tuple[np.ndarray, np.ndarray]:
    """Welch-averaged PSD of complex IQ.

    Returns (freqs_hz, psd_db) where freqs are absolute (center_freq + baseband)
    and psd_db is 10*log10 of power. Two-sided (fftshifted) for complex input.
    """
    x = np.asarray(iq, dtype=np.complex64)
    n = x.size
    if n == 0:
        return np.array([]), np.array([])
    nfft = int(min(nfft, n))
    if nfft < 8:
        nfft = n

    win_map = {
        "hann": np.hanning, "hamming": np.hamming, "blackman": np.blackman,
        "rectangular": np.ones,
    }
    w = win_map.get(window, np.hanning)(nfft)
    win_power = np.sum(w ** 2)
    w32 = w.astype(np.float32)

    # all segments at once as a strided view (no copy), FFT'd in batches; a
    # one-segment-per-loop form costs 142 ms for 262144 samples on the Orin
    step = max(1, int(nfft * (1.0 - overlap)))
    segs = np.lib.stride_tricks.sliding_window_view(x, nfft)[::step]
    acc = np.zeros(nfft, dtype=np.float64)
    for i in range(0, len(segs), PSD_FFT_BATCH):
        sp = _fft(segs[i:i + PSD_FFT_BATCH] * w32, axis=1)
        acc += (sp.real.astype(np.float64) ** 2 + sp.imag.astype(np.float64) ** 2).sum(axis=0)
    count = len(segs)
    acc = np.fft.fftshift(acc)

    psd = acc / count
    # normalize: power per Hz
    psd = psd / (sample_rate * win_power)
    psd_db = 10.0 * np.log10(psd + 1e-20)

    freqs = np.fft.fftshift(np.fft.fftfreq(nfft, d=1.0 / sample_rate)) + center_freq
    return freqs.astype(np.float64), psd_db.astype(np.float64)


# Trace data model

@dataclass
class PSDTrace:
    """One PSD snapshot with its hardware time."""
    timestamp_sec: float          # from SDR rf_timestamp / sample_rate
    sample_rate: float
    center_freq: float
    freqs_hz: List[float]
    psd_db: List[float]
    seq: int = 0


@dataclass
class TraceRecording:
    """A sequence of PSD traces, savable to / loadable from JSON, replayable."""
    sample_rate: float
    center_freq: float
    nfft: int
    window: str
    created: float = field(default_factory=time.time)
    traces: List[PSDTrace] = field(default_factory=list)

    def add(self, tr: PSDTrace) -> None:
        self.traces.append(tr)

    def save(self, path: str) -> str:
        """Save traces compactly. Freq axis is stored once (it is constant across
        traces for fixed nfft/rate); each trace stores only psd_db + timestamp."""
        p = Path(path)
        if not self.traces:
            raise ValueError("No traces to save.")
        freqs = self.traces[0].freqs_hz
        doc = {
            "format": "dpmg-psd-trace-v1",
            "sample_rate": self.sample_rate,
            "center_freq": self.center_freq,
            "nfft": self.nfft,
            "window": self.window,
            "created": self.created,
            "freqs_hz": freqs,                  # stored once
            "traces": [
                {"t": tr.timestamp_sec, "seq": tr.seq, "psd_db": tr.psd_db}
                for tr in self.traces
            ],
        }
        p.write_text(json.dumps(doc))
        return str(p)

    @staticmethod
    def load(path: str) -> "TraceRecording":
        doc = json.loads(Path(path).read_text())
        if doc.get("format") != "dpmg-psd-trace-v1":
            raise ValueError("Not a DPMG PSD trace file.")
        freqs = doc["freqs_hz"]
        rec = TraceRecording(
            sample_rate=doc["sample_rate"], center_freq=doc["center_freq"],
            nfft=doc["nfft"], window=doc["window"], created=doc.get("created", 0.0),
        )
        for t in doc["traces"]:
            rec.traces.append(PSDTrace(
                timestamp_sec=t["t"], sample_rate=doc["sample_rate"],
                center_freq=doc["center_freq"], freqs_hz=freqs,
                psd_db=t["psd_db"], seq=t.get("seq", 0),
            ))
        return rec


# live recorder: maintains a rolling IQ buffer + optional raw save + trace log

class PSDRecorder:
    """Consumes RX frames: keeps a rolling IQ buffer for live PSD, optionally
    writes raw IQ, and optionally logs PSD traces with hardware timestamps."""

    def __init__(self, nfft: int = 4096, window: str = "hann",
                 buffer_samples: int = 65536, max_traces: int = 2000):
        self.nfft = int(nfft)
        self.window = window
        self.buffer_samples = int(buffer_samples)
        self.max_traces = int(max_traces)
        self._buf = np.zeros(0, dtype=np.complex64)
        self.recording: Optional[TraceRecording] = None
        self._raw_fh = None
        self._raw_path: Optional[str] = None
        self.last_psd: Optional[tuple] = None      # (freqs, psd_db)

    # rolling IQ buffer 
    def push_frame(self, iq: np.ndarray, sample_rate: float, center_freq: float,
                   rf_timestamp: Optional[int] = None, seq: int = 0) -> None:
        iq = np.asarray(iq, dtype=np.complex64)
        if iq.size == 0:
            return
        # append + cap the rolling buffer (only the tail of a long frame can
        # survive the cap, so don't copy the rest: a 61.44 MSps batch is 2 M samples)
        self._buf = np.concatenate([self._buf, iq[-self.buffer_samples:]])[-self.buffer_samples:]

        # optional raw IQ save (interleaved float32 I,Q)
        if self._raw_fh is not None:
            inter = np.empty(2 * iq.size, dtype=np.float32)
            inter[0::2] = iq.real
            inter[1::2] = iq.imag
            self._raw_fh.write(inter.tobytes())

        # hardware time: prefer rf_timestamp / fs; else derive from seq * chunk
        if rf_timestamp is not None and sample_rate > 0:
            t_sec = float(rf_timestamp) / float(sample_rate)
        else:
            t_sec = float(seq) * (iq.size / sample_rate if sample_rate else 0.0)

        # optional trace logging
        if self.recording is not None:
            freqs, psd_db = compute_psd(self._buf, sample_rate, center_freq,
                                        nfft=self.nfft, window=self.window)
            self.last_psd = (freqs, psd_db)
            if len(self.recording.traces) < self.max_traces:
                self.recording.add(PSDTrace(
                    timestamp_sec=t_sec, sample_rate=sample_rate, center_freq=center_freq,
                    freqs_hz=freqs.tolist(), psd_db=psd_db.tolist(), seq=seq,
                ))

    # live PSD (always available from the rolling buffer) 
    def live_psd(self, sample_rate: float, center_freq: float) -> tuple[np.ndarray, np.ndarray]:
        if self._buf.size == 0:
            return np.array([]), np.array([])
        freqs, psd_db = compute_psd(self._buf, sample_rate, center_freq,
                                    nfft=self.nfft, window=self.window)
        self.last_psd = (freqs, psd_db)
        return freqs, psd_db

    # raw IQ capture control 
    def start_raw_capture(self, path: str) -> None:
        self.stop_raw_capture()
        self._raw_fh = open(path, "wb")
        self._raw_path = path

    def stop_raw_capture(self) -> Optional[str]:
        if self._raw_fh is not None:
            self._raw_fh.close()
            self._raw_fh = None
            p = self._raw_path
            self._raw_path = None
            return p
        return None

    # trace recording control 
    def start_trace_recording(self, sample_rate: float, center_freq: float) -> None:
        self.recording = TraceRecording(
            sample_rate=sample_rate, center_freq=center_freq,
            nfft=self.nfft, window=self.window,
        )

    def stop_trace_recording(self) -> Optional[TraceRecording]:
        rec = self.recording
        self.recording = None
        return rec

    def clear_buffer(self) -> None:
        self._buf = np.zeros(0, dtype=np.complex64)
