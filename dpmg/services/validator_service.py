"""Validator service: IQ file parsing and bit comparison against expected fields."""
from __future__ import annotations

import math
from typing import Any, Dict, Optional

import numpy as np

from dpmg.demodulation.demodulator import get_demodulator
from dpmg.encoding.ecc_decoder import encode_field_bits
from dpmg.ui.data_validation import (
    _get_4fsk_tone_to_dibit,
    _load_expected_json_for_batch,
    _normalize_4fsk_dibit_mapping,
    _resolve_4fsk_tones_domain,
    demodulate_instantaneous_frequency,
    estimate_symbol_bits,
    normalize_modulation,
    read_iq_file,
    run_batch_validate,
)


def validate_iq(
    iq_path: str,
    expected_json_path: str,
    overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run IQ validation and return a structured result dict.

    Returns dict with keys: pass, decoded_bits_len, expected_bits_len,
    field_count, matched_fields, results.
    """
    return run_batch_validate(iq_path, expected_json_path, overrides)


def _bits_per_symbol(modulation: str) -> int:
    if modulation in ("4FSK", "QPSK", "π/4-QPSK", "DQPSK", "OQPSK"):
        return 2
    return 1


def _symbol_expected_freq(bits: str, modulation: str, expected_tones: list[float], dibit_mapping: str) -> Optional[float]:
    if modulation in ("FSK", "GFSK") and len(expected_tones) >= 2 and bits in ("0", "1"):
        return float(expected_tones[0] if bits == "0" else expected_tones[1])
    if modulation == "4FSK" and len(expected_tones) >= 4 and len(bits) == 2:
        tone_to_bits = _get_4fsk_tone_to_dibit(dibit_mapping)
        bits_to_tone = {v: k for k, v in tone_to_bits.items()}
        idx = bits_to_tone.get(bits)
        if idx is not None:
            return float(expected_tones[idx])
    return None


def _symbol_freq_series(inst_freq: list[float], modulation: str, samples_per_bit: int) -> list[float]:
    if not inst_freq:
        return []
    samples_per_symbol = max(1, int(round(samples_per_bit * _bits_per_symbol(modulation))))
    out: list[float] = []
    for start in range(0, len(inst_freq), samples_per_symbol):
        window = inst_freq[start:start + samples_per_symbol]
        if window:
            out.append(float(np.mean(np.asarray(window, dtype=np.float64))))
    return out


def _build_plot_payload(
    iq_path: str,
    expected_json_path: str,
    overrides: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    settings, expected_fields = _load_expected_json_for_batch(expected_json_path)
    if overrides:
        settings.update({k: v for k, v in overrides.items() if v is not None})

    sample_rate = float(settings.get("sample_rate", 44400.0))
    symbol_rate = float(settings.get("symbol_rate", settings.get("baud", 1200.0)))
    samples_per_bit = int(settings.get("samples_per_bit", 37))
    modulation = normalize_modulation(settings.get("modulation", "FSK")) or "FSK"
    tone0 = float(settings.get("tone0", 1200.0))
    tone1 = float(settings.get("tone1", 2200.0))
    tone2 = float(settings.get("tone2", 1400.0))
    tone3 = float(settings.get("tone3", 2400.0))
    center_freq_hz = float(settings.get("center_freq_hz", 0.0))
    tone_domain = str(settings.get("tone_domain", "auto"))
    dibit_mapping = _normalize_4fsk_dibit_mapping(settings.get("dibit_mapping", "dmr"))
    gfsk_bw_hz = float(settings.get("gfsk_bw_hz", 250.0))
    spread_factor = int(settings.get("spread_factor", 15))
    pn_seed = int(settings.get("pn_seed", 1))
    css_sf = int(settings.get("css_sf", 7))
    css_bw = float(settings.get("css_bw", 125000.0))
    css_os = int(settings.get("css_os", 1))
    code_spec = settings.get("code_spec")
    dsss_modem = str(settings.get("dsss_modem", "bpsk"))

    if modulation == "4FSK":
        tone0, tone1, tone2, tone3 = _resolve_4fsk_tones_domain(
            tone0, tone1, tone2, tone3, center_freq_hz, tone_domain
        )

    if modulation == "4FSK":
        expected_tones = [tone0, tone1, tone2, tone3]
    elif modulation in ("FSK", "GFSK"):
        expected_tones = [tone0, tone1]
    else:
        expected_tones = []

    samples = read_iq_file(iq_path)
    iq = np.asarray(samples, dtype=np.complex128)
    if modulation in ("FSK", "4FSK"):
        decoded_bits = estimate_symbol_bits(
            samples,
            sample_rate,
            symbol_rate,
            modulation,
            expected_tones,
            samples_per_bit,
            dibit_mapping,
        )
    else:
        demod = get_demodulator(
            modulation,
            sample_rate=sample_rate,
            symbol_rate=symbol_rate,
            samples_per_bit=samples_per_bit,
            tone0=tone0,
            tone1=tone1,
            tone2=tone2,
            tone3=tone3,
            dibit_mapping=dibit_mapping,
            gfsk_bw_hz=gfsk_bw_hz,
            spread_factor=spread_factor,
            pn_seed=pn_seed,
            css_sf=css_sf,
            css_bw=css_bw,
            css_os=css_os,
            code_spec=code_spec,
            dsss_modem=dsss_modem,
        )
        decoded_bits = demod.demodulate(iq).bit_string

    inst_freq = demodulate_instantaneous_frequency(samples, sample_rate)
    symbol_freqs = _symbol_freq_series(inst_freq, modulation, samples_per_bit)

    expected_string = "".join(
        encode_field_bits(field["bin"], field.get("encoding", "None"))
        for field in expected_fields
    )
    bps = _bits_per_symbol(modulation)
    expected_symbols = [expected_string[i:i + bps] for i in range(0, len(expected_string), bps)]
    decoded_symbols = [decoded_bits[i:i + bps] for i in range(0, len(decoded_bits), bps)]
    symbol_count = max(len(expected_symbols), len(decoded_symbols))

    # Build a reverse mapping: dibit string: tone index (for both expected and decoded).
    _dibit_to_tone_idx: dict[str, int] = {}
    if modulation == "4FSK":
        tone_to_dibit = _get_4fsk_tone_to_dibit(dibit_mapping)
        _dibit_to_tone_idx = {v: k for k, v in tone_to_dibit.items()}
    elif modulation in ("FSK", "GFSK"):
        _dibit_to_tone_idx = {"0": 0, "1": 1}

    timing_points = []
    for idx in range(symbol_count):
        exp_bits = expected_symbols[idx] if idx < len(expected_symbols) else ""
        dec_bits = decoded_symbols[idx] if idx < len(decoded_symbols) else ""
        exp_freq = _symbol_expected_freq(exp_bits, modulation, expected_tones, dibit_mapping)

        # Derive decoded_freq_hz from the decoded dibit via the same reverse mapping so
        # that the waveform visualization is always consistent with the decoded bits.
        # The raw instantaneous-freq window (which may not be sample-aligned with the
        # demodulator) is preserved separately for diagnostics.
        dec_tone_idx = _dibit_to_tone_idx.get(dec_bits)
        dec_freq_from_bits = float(expected_tones[dec_tone_idx]) if dec_tone_idx is not None and expected_tones else None
        raw_dec_freq = symbol_freqs[idx] if idx < len(symbol_freqs) else None
        if raw_dec_freq is not None and not math.isfinite(raw_dec_freq):
            raw_dec_freq = None

        timing_points.append(
            {
                "index": idx + 1,
                "expected_bits": exp_bits,
                "decoded_bits": dec_bits,
                "expected_freq_hz": exp_freq,
                "decoded_freq_hz": dec_freq_from_bits,
                "raw_decoded_freq_hz": raw_dec_freq,
                "tone_index": dec_tone_idx,
            }
        )

    fft_freqs: list[float] = []
    fft_mags: list[float] = []
    if iq.size >= 8:
        fft_data = iq
        fft_n = int(settings.get("fft_size", 65536))
        win_name = str(settings.get("fft_window", "hamming")).lower()
        if fft_data.size > fft_n:
            start = (fft_data.size - fft_n) // 2
            fft_data = fft_data[start:start + fft_n]
        _win_map = {
            "hamming": np.hamming,
            "hann": np.hanning,
            "blackman": np.blackman,
            "rectangular": np.ones,
        }
        window = _win_map.get(win_name, np.hamming)(fft_data.size)
        if modulation == "4FSK":
            transformed = np.fft.fftshift(np.fft.fft((fft_data - np.mean(fft_data)) * window))
            freqs = np.fft.fftshift(np.fft.fftfreq(fft_data.size, d=1.0 / sample_rate))
        else:
            real_data = np.asarray(fft_data.real, dtype=np.float64)
            transformed = np.fft.rfft((real_data - np.mean(real_data)) * window)
            freqs = np.fft.rfftfreq(real_data.size, d=1.0 / sample_rate)
        mags = 20.0 * np.log10(np.abs(transformed) + 1e-12)
        max_points = 1200
        if mags.size > max_points:
            idx = np.linspace(0, mags.size - 1, max_points, dtype=int)
            freqs = freqs[idx]
            mags = mags[idx]
        fft_freqs = freqs.astype(float).tolist()
        fft_mags = mags.astype(float).tolist()

    spec_freqs: list[float] = []
    spec_power: list[float] = []
    if len(symbol_freqs) >= 2:
        freq_arr = np.asarray(symbol_freqs, dtype=np.float64)
        bins = max(32, min(256, int(math.sqrt(freq_arr.size) * 8)))
        counts, edges = np.histogram(freq_arr, bins=bins)
        centers = 0.5 * (edges[:-1] + edges[1:])
        power_db = 10.0 * np.log10(counts.astype(np.float64) + 1e-12)
        spec_freqs = centers.astype(float).tolist()
        spec_power = power_db.astype(float).tolist()

    return {
        "fft": {
            "frequencies_hz": fft_freqs,
            "magnitude_db": fft_mags,
            "expected_tones_hz": [float(x) for x in expected_tones],
        },
        "symbol_spectrum": {
            "frequencies_hz": spec_freqs,
            "power_db": spec_power,
            "expected_tones_hz": [float(x) for x in expected_tones],
        },
        "symbol_timing": {
            "modulation": modulation,
            "bits_per_symbol": bps,
            "expected_tones_hz": [float(x) for x in expected_tones],
            "points": timing_points,
        },
    }


def validate_iq_with_plots(
    iq_path: str,
    expected_json_path: str,
    overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run IQ validation and include plot-friendly signal analysis payloads."""
    base = validate_iq(iq_path, expected_json_path, overrides)
    base["plots"] = _build_plot_payload(iq_path, expected_json_path, overrides)
    return base
