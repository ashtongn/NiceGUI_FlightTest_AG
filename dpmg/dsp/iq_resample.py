"""
iq_resample.py: load / resample / save IQ across the supported file formats.

Formats (by extension):
  .c64 .iq.bin .bin    complex64 (float32 I,Q interleaved)
  .g40iq .g40qi        G40 16-bit packed (LE uint32, I/Q in the two halves)
  .x40iq .x40qi        X40 14-bit packed (same layout, 14-bit full scale)
  .wav                 stereo = I/Q, mono = real

A raw IQ file carries neither its format nor its rate, so both are given: the
format comes from the extension, the native rate is supplied by the caller.
resample() uses the exact rational fs_out/fs_in (scipy resample_poly), so a file
captured at any native rate replays correctly at any supported radio rate.
"""
from __future__ import annotations

import os
from fractions import Fraction

import numpy as np

C64_EXT = (".c64", ".iq.bin", ".bin")
G40_EXT = (".g40iq", ".g40qi")
X40_EXT = (".x40iq", ".x40qi")
BLUE_EXTS = (".cdif", ".tmp", ".blue", ".prm")   # Midas BLUE / Platinum (same HCB)
IQ_EXTS = C64_EXT + G40_EXT + X40_EXT + (".wav",) + BLUE_EXTS


def _ext(path: str) -> str:
    name = os.path.basename(path).lower()
    return ".iq.bin" if name.endswith(".iq.bin") else os.path.splitext(name)[1]


# Explicit raw-sample formats the caller can force (name: dtype, order, divisor).
# order "C" = already-complex dtype; "IQ"/"QI" = two reals per sample. Ints are
# normalised to ~[-1, 1] by the divisor; floats are assumed already scaled.
RAW_FORMATS = {
    "complex64 (float32 I,Q interleaved)": ("<c8", "C", 1.0),
    "float32 I,Q interleaved (LE)":        ("<f4", "IQ", 1.0),
    "int16 I,Q interleaved (LE)":          ("<i2", "IQ", 32768.0),
    "int16 Q,I interleaved (LE)":          ("<i2", "QI", 32768.0),
    "int16 I,Q interleaved (BE)":          (">i2", "IQ", 32768.0),
    "int8  I,Q interleaved":               ("<i1", "IQ", 128.0),
    "int32 I,Q interleaved (LE)":          ("<i4", "IQ", 2147483648.0),
}
# formats read by the packed/wav/extension readers
SPECIAL_FORMATS = ["auto (by extension)",
                   "G40 16-bit packed (.g40iq)", "G40 16-bit packed Q,I (.g40qi)",
                   "X40 14-bit packed (.x40iq)", "WAV (stereo I/Q)",
                   "CDIF (BLUE / SignalVu)"]
FORMAT_NAMES = SPECIAL_FORMATS + list(RAW_FORMATS)
AUTO = "auto (by extension)"


def _read_raw(path: str, dtype, offset_bytes: int = 0) -> np.ndarray:
    """np.fromfile from byte `offset_bytes` (skips a leading header)."""
    with open(path, "rb") as f:
        if offset_bytes:
            f.seek(int(offset_bytes))
        return np.fromfile(f, dtype=dtype)


def _load_packed(path: str, bits: int, qi: bool, offset_bytes: int = 0) -> np.ndarray:
    fsv = float(1 << (bits - 1))
    words = _read_raw(path, "<u4", offset_bytes)
    hi = (words >> 16).astype(np.int32)
    lo = (words & 0xFFFF).astype(np.int32)
    hi = (hi ^ 0x8000) - 0x8000          # sign-extend the 16-bit halves
    lo = (lo ^ 0x8000) - 0x8000
    # G40/X40 pack I in the high half, Q in the low half (".g40qi" swaps them)
    i, q = (lo, hi) if qi else (hi, lo)
    return ((i + 1j * q).astype(np.complex64) / fsv)


def _is_blue(path: str) -> bool:
    """A Midas BLUE file by its ending or by its first bytes: a BLUE recording
    is often saved as .bin."""
    if _ext(path) in BLUE_EXTS:
        return True
    from dpmg.dsp import cdif_reader
    return cdif_reader.is_blue(path)


def _load_by_ext(path: str, offset_bytes: int = 0) -> np.ndarray:
    ext = _ext(path)
    # the content decides before the name does, unless the caller skips a header by hand
    if not offset_bytes and ext not in BLUE_EXTS and ext != ".wav" and _is_blue(path):
        from dpmg.dsp import cdif_reader
        iq, _cm = cdif_reader.read_cdif(path)
        return np.asarray(iq, dtype=np.complex64)
    if ext in (".c64", ".iq.bin", ".bin"):
        return _read_raw(path, np.complex64, offset_bytes)
    if ext in G40_EXT or ext in X40_EXT:
        return _load_packed(path, 14 if ext in X40_EXT else 16,
                            qi=ext in (".g40qi", ".x40qi"), offset_bytes=offset_bytes)
    if ext == ".wav":                       # wav header is handled by the reader
        from dpmg.radio.sdr_core import WaveformLoader
        wave, _meta = WaveformLoader.load(path, 1.0)
        return np.asarray(wave, dtype=np.complex64)
    if ext in BLUE_EXTS:                    # Midas BLUE / Platinum (.cdif/.tmp/.blue/.prm)
        from dpmg.dsp import cdif_reader
        iq, _cm = cdif_reader.read_cdif(path)
        return np.asarray(iq, dtype=np.complex64)
    raise ValueError(f"unsupported IQ extension: {ext} (set the input format)")


def native_rate(path: str):
    """Sample rate embedded in the file (Hz), or None if the format carries none.
    Midas BLUE/Platinum (.cdif/.tmp/.blue/.prm) record it in the HCB; a .wav in
    its header. Raw IQ carries none. A BLUE file is known by its first bytes
    as well as by its ending."""
    ext = _ext(path)
    if ext != ".wav" and _is_blue(path):
        try:
            from dpmg.dsp import cdif_reader
            return cdif_reader.read_header(path).sample_rate_hz or None
        except Exception:
            return None
    if ext == ".wav":
        try:
            import wave
            with wave.open(path, "rb") as w:
                return float(w.getframerate()) or None
        except Exception:
            return None
    return None


def load_iq(path: str, fmt: str = AUTO, offset_bytes: int = 0) -> np.ndarray:
    """Read an IQ file: complex64 (~[-1, 1]). `fmt` forces the sample format
    (AUTO infers from the extension); `offset_bytes` skips a leading header."""
    if fmt in (None, "auto", AUTO):
        return _load_by_ext(path, offset_bytes)
    if fmt in RAW_FORMATS:
        dtype, order, div = RAW_FORMATS[fmt]
        if order == "C":
            return _read_raw(path, dtype, offset_bytes).astype(np.complex64)
        raw = _read_raw(path, dtype, offset_bytes).astype(np.float32)
        if raw.size % 2:
            raw = raw[:-1]
        raw = raw.reshape(-1, 2) / float(div)
        i, q = (raw[:, 0], raw[:, 1]) if order == "IQ" else (raw[:, 1], raw[:, 0])
        return (i + 1j * q).astype(np.complex64)
    if "packed" in fmt:
        bits = 14 if "X40" in fmt else 16
        return _load_packed(path, bits, qi="Q,I" in fmt, offset_bytes=offset_bytes)
    if fmt.startswith("WAV"):
        return _load_by_ext(path, offset_bytes)
    if fmt.startswith("CDIF"):
        from dpmg.dsp import cdif_reader
        iq, _cm = cdif_reader.read_cdif(path)
        return np.asarray(iq, dtype=np.complex64)
    raise ValueError(f"unknown input format: {fmt}")


def resample(iq, fs_in: float, fs_out: float) -> np.ndarray:
    """Resample complex IQ from fs_in to fs_out (exact rational ratio)."""
    iq = np.asarray(iq, dtype=np.complex64)
    if fs_in <= 0 or fs_out <= 0:
        raise ValueError("rates must be positive")
    if abs(fs_in - fs_out) < 1.0 or len(iq) == 0:
        return iq
    from scipy.signal import resample_poly
    fr = Fraction(int(round(fs_out)), int(round(fs_in))).limit_denominator(20000)
    return resample_poly(iq, fr.numerator, fr.denominator).astype(np.complex64)


def save_iq(iq, path: str) -> None:
    """Write complex64 IQ to a file; packing chosen by the extension."""
    ext = _ext(path)
    iq = np.asarray(iq, dtype=np.complex64)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if ext in C64_EXT:
        iq.astype(np.complex64).tofile(path)
        return
    if ext in G40_EXT or ext in X40_EXT:
        bits = 14 if ext in X40_EXT else 16
        fsv = float((1 << (bits - 1)) - 1)
        iv = (np.clip(np.round(iq.real * fsv), -fsv - 1, fsv).astype(np.int64) & 0xFFFF)
        qv = (np.clip(np.round(iq.imag * fsv), -fsv - 1, fsv).astype(np.int64) & 0xFFFF)
        words = ((qv << 16) | iv) if ext in (".g40qi", ".x40qi") else ((iv << 16) | qv)
        words.astype("<u4").tofile(path)
        return
    if ext == ".wav":
        import wave as _w
        ival = np.clip(np.round(iq.real * 32767.0), -32768, 32767).astype("<i2")
        qval = np.clip(np.round(iq.imag * 32767.0), -32768, 32767).astype("<i2")
        inter = np.empty(ival.size * 2, dtype="<i2")
        inter[0::2] = ival
        inter[1::2] = qval
        with _w.open(path, "wb") as f:
            f.setnchannels(2); f.setsampwidth(2); f.setframerate(1)
            f.writeframes(inter.tobytes())
        return
    raise ValueError(f"unsupported IQ extension: {ext}")


def resample_file(src: str, dst: str, fs_in: float, fs_out: float,
                  fmt: str = AUTO, offset_bytes: int = 0) -> dict:
    """Load src (@fs_in, format `fmt`, skipping `offset_bytes`), resample, write dst."""
    iq = load_iq(src, fmt, offset_bytes)
    out = resample(iq, fs_in, fs_out)
    save_iq(out, dst)
    return {"src": src, "dst": dst, "fs_in": fs_in, "fs_out": fs_out,
            "n_in": int(len(iq)), "n_out": int(len(out)),
            "ratio": (len(out) / len(iq)) if len(iq) else 0.0}
