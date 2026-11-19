#!/usr/bin/env python3
"""DMR telemetry IQ validator UI."""

import argparse
import json
import math
import os
import struct
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
import tkinter.ttk as ttk
from tkinter import filedialog, messagebox

import numpy as np

from dpmg.demodulation.channel_simulator import ChannelParameters, ChannelSimulator
from dpmg.demodulation.demodulator import get_demodulator
from dpmg.encoding.ecc_decoder import encode_field_bits

SDR_IQ_EXTS = {".x40iq", ".x40qi", ".g40iq", ".g40qi"}
DPMG_BIN_MAGIC = b"DPMG"
DPMG_BIN_HEADER_SIZE = 16
SUPPORTED_MODULATIONS = (
    "FSK",
    "4FSK",
    "BPSK",
    "QPSK",
    "π/4-QPSK",
    "DQPSK",
    "OQPSK",
    "GFSK",
    "DSSS",
    "QPSK-DSSS",
    "CSS",
)

_MODULATION_ALIASES = {
    "FSK": "FSK",
    "4FSK": "4FSK",
    "FSK4": "4FSK",
    "BPSK": "BPSK",
    "QPSK": "QPSK",
    "PI4QPSK": "π/4-QPSK",
    "PIBY4QPSK": "π/4-QPSK",
    "DQPSK": "DQPSK",
    "OQPSK": "OQPSK",
    "GFSK": "GFSK",
    "DSSS": "DSSS",
    "BPSKDSSS": "BPSK-DSSS",
    "DSSSBPSK": "BPSK-DSSS",
    "QPSKDSSS": "QPSK-DSSS",
    "DSSSQPSK": "QPSK-DSSS",
    "QPSKDSSS": "QPSK-DSSS",
    "DSSSQPSK": "QPSK-DSSS",
    "CSS": "CSS",
    "LORA": "CSS",
    "LORACSS": "CSS",
}


_FALLBACK_ICON_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAABnUlEQVR42u2ZQUoFMQyGv4O4cC"
    "mC4AE8g0fwCCIiiAgigorgWTyC6yeKoiiKeJfI25XSN07bydSSLP5d6fz5krZph59bxLL4vkEs"
    "i69rxLL4vEQsi48LxLJ4P0csi7czxLJ4PUUsi5cTxLJ4PkYsi6cjxLJ4PEQsi4cDxLJY7COW"
    "xdr6tliWA3AADsABNPv43u5WUiYALAO9u9pMak4ItAxe7jeSmhMCLYNfVQFzQqgGIIudrPFx"
    "gCmFY/4tgGXgscZkf0x2QwjaVTAbgHDTGxNU7vhZAaSC/wtCSUbDilEDUEo3J/iaQLSXAqV0"
    "c7NfWsrhUlABUEt3zNqv/cYUcwxWgNZGM2X2tHwy9miqaXimmFfLJ3FzMtXEvcxJT9nS8ElP"
    "61XDJz3t2Bo+6enM1vBJT12bhk966tvjBiz0OfZaHvukp5tbDGAKn/R0d0/dQ2p90tPrzSoI"
    "NT7p6f1uqApKfZLTfbV+wR26jZb6JPfcbPmGr+GTnMlb/8XR8On/Bh2AA3AADsABOAAH4AAc"
    "gANwAA7AATgAB+AArOkXvRkZnKyreYYAAAAASUVORK5CYII="
)


def _load_icon(root, icon_path=None):
    if icon_path and os.path.exists(icon_path):
        try:
            raw = tk.PhotoImage(file=icon_path)
            factor = max(1, raw.width() // 64)
            img = raw.subsample(factor, factor) if factor > 1 else raw
            root.iconphoto(True, img)
            root._dpmg_icon_image = img
            return
        except tk.TclError:
            pass
    try:
        img = tk.PhotoImage(data=_FALLBACK_ICON_B64)
        root.iconphoto(True, img)
        root._dpmg_icon_image = img
    except Exception:
        pass


def configure_window(root, min_width=1000, min_height=700):
    root.minsize(min_width, min_height)
    base_dir = os.path.dirname(os.path.abspath(__file__))
    assets_dir = os.path.join(base_dir, "assets")
    icon_path = None
    for icon_name in ("app_icon.png", "icon.png", "app_icon.gif", "icon.gif"):
        candidate = os.path.join(assets_dir, icon_name)
        if os.path.exists(candidate):
            icon_path = candidate
            break
    _load_icon(root, icon_path)


def normalize_phase_delta(delta):
    while delta > math.pi:
        delta -= 2.0 * math.pi
    while delta < -math.pi:
        delta += 2.0 * math.pi
    return delta


def normalize_modulation(value):
    text = str(value or "").strip()
    if not text:
        return None
    if text in SUPPORTED_MODULATIONS:
        return text

    key = (
        text.replace("π", "pi")
        .replace("Π", "pi")
        .upper()
        .replace(" ", "")
        .replace("-", "")
        .replace("_", "")
        .replace("/", "")
    )
    return _MODULATION_ALIASES.get(key)


def read_iq_file(path):
    with open(path, "rb") as in_file:
        raw = in_file.read()

    # SDR DAC formats (X40 14-bit / G40 16-bit, headerless LE uint32-packed)
    import os
    ext = os.path.splitext(path)[1].lower()
    if ext in SDR_IQ_EXTS:
        if len(raw) % 4 != 0:
            raise ValueError("SDR IQ file length must be a multiple of 4 bytes (one uint32 per IQ pair).")
        n_words = len(raw) // 4
        words = struct.unpack(f"<{n_words}I", raw)        # little-endian uint32 per pair
        full_scale = 8191.0 if ext in (".x40iq", ".x40qi") else 32767.0
        qi_order = ext in (".x40qi", ".g40qi")            # QI: Q high, I low
        def _s16(v):
            return v - 0x10000 if v & 0x8000 else v
        complex_samples = []
        for w in words:
            hi = _s16((w >> 16) & 0xFFFF)
            lo = _s16(w & 0xFFFF)
            if qi_order:
                q_sample, i_sample = hi / full_scale, lo / full_scale
            else:
                i_sample, q_sample = hi / full_scale, lo / full_scale
            complex_samples.append(complex(i_sample, q_sample))
        return complex_samples

    # DPMG .bin (header + BE int16) or legacy raw (LE int16)
    is_dpmg = len(raw) >= DPMG_BIN_HEADER_SIZE and raw[:4] == DPMG_BIN_MAGIC
    payload = raw
    unpack_endian = "<"
    if is_dpmg:
        _magic, _version, data_format, _reserved, _sample_rate, data_offset = struct.unpack(
            ">4sBBHII", raw[:DPMG_BIN_HEADER_SIZE]
        )
        if data_offset < DPMG_BIN_HEADER_SIZE or data_offset > len(raw):
            raise ValueError("Invalid DPMG BIN header: data offset is out of range.")
        if data_format != 1:
            raise ValueError(f"Unsupported DPMG BIN payload format: {data_format}")
        payload = raw[data_offset:]
        unpack_endian = ">"

    if len(payload) % 4 != 0:
        raise ValueError("IQ file length must be a multiple of 4 bytes (interleaved int16 I/Q).")

    count = len(payload) // 2
    values = struct.unpack(f"{unpack_endian}{count}h", payload)
    complex_samples = []
    for i in range(0, len(values), 2):
        i_sample = values[i] / 32768.0
        q_sample = values[i + 1] / 32768.0
        complex_samples.append(complex(i_sample, q_sample))
    return complex_samples


def unwrap_phase(phases):
    unwrapped = [phases[0]]
    for i in range(1, len(phases)):
        delta = normalize_phase_delta(phases[i] - phases[i - 1])
        unwrapped.append(unwrapped[-1] + delta)
    return unwrapped


def demodulate_instantaneous_frequency(samples, sample_rate):
    phases = [math.atan2(c.imag, c.real) for c in samples]
    if len(phases) < 2:
        return []
    unwrapped = unwrap_phase(phases)
    inst_freq = []
    for i in range(1, len(unwrapped)):
        delta_phase = unwrapped[i] - unwrapped[i - 1]
        freq_hz = delta_phase * sample_rate / (2.0 * math.pi)
        inst_freq.append(freq_hz)
    return inst_freq


def _normalize_4fsk_dibit_mapping(value, default="dmr"):
    key = str(value or default).strip().lower().replace("-", "_")
    if key not in ("binary", "gray", "dmr", "dmr_inverted"):
        raise ValueError(f"Unknown 4FSK dibit mapping: {value}")
    return key


def _get_4fsk_tone_to_dibit(mapping):
    key = _normalize_4fsk_dibit_mapping(mapping)
    if key == "binary":
        return {0: "00", 1: "01", 2: "10", 3: "11"}
    if key == "gray":
        return {0: "00", 1: "01", 2: "11", 3: "10"}
    if key == "dmr":
        return {0: "11", 1: "10", 2: "00", 3: "01"}
    return {0: "01", 1: "00", 2: "10", 3: "11"}


def _resolve_4fsk_tones_domain(tone0, tone1, tone2, tone3, center_freq_hz, tone_domain="auto"):
    tones = [float(tone0), float(tone1), float(tone2), float(tone3)]
    center_freq_hz = float(center_freq_hz)
    domain = str(tone_domain or "auto").strip().lower()
    if domain not in ("absolute_rf", "baseband", "auto"):
        raise ValueError(f"Unknown tone_domain: {tone_domain}")
    if domain == "auto":
        if center_freq_hz > 0 and all(t > 0 for t in tones):
            near_center = max(abs(t - center_freq_hz) for t in tones) <= 5_000_000.0
            large_freqs = max(abs(t) for t in tones) >= 1_000_000.0
            domain = "absolute_rf" if (near_center and large_freqs) else "baseband"
        else:
            domain = "baseband"
    if domain == "absolute_rf":
        tones = [t - center_freq_hz for t in tones]
    return tones


def _bits_to_hex(bits):
    bit_text = str(bits or "").strip().replace(" ", "")
    if len(bit_text) == 0:
        return ""
    if any(ch not in "01" for ch in bit_text):
        return ""
    pad = (-len(bit_text)) % 4
    if pad:
        bit_text = "0" * pad + bit_text
    value = int(bit_text, 2)
    width = len(bit_text) // 4
    return f"{value:0{width}X}"


def estimate_symbol_bits(samples, sample_rate, symbol_rate, modulation, tones, samples_per_bit, dibit_mapping="dmr"):
    inst_freq = demodulate_instantaneous_frequency(samples, sample_rate)

    if len(inst_freq) == 0:
        return ""

    # Compute the true (possibly fractional) samples-per-symbol from the actual
    # sample rate / symbol rate, not the rounded samples_per_bit. Resampled files
    # (e.g. 541667 Sa/s at 9600 baud: 56.42 samples/sym) have a non-integer
    # symbol period; stepping by a rounded integer accumulates drift and corrupts
    # everything past the first ~dozen symbols. Tracking the boundary as a float
    # and rounding fresh each symbol keeps alignment locked with no accumulation.
    if symbol_rate and symbol_rate > 0:
        samples_per_symbol_f = (sample_rate / symbol_rate)
        if modulation not in ("FSK", "4FSK"):
            samples_per_symbol_f *= 2.0
    else:
        # fallback to the provided samples_per_bit if symbol_rate is unusable
        samples_per_symbol_f = float(samples_per_bit) * (1.0 if modulation in ("FSK", "4FSK") else 2.0)

    if samples_per_symbol_f < 1.0:
        raise ValueError("Samples per symbol must be at least 1.")

    n_symbols = int(round(len(inst_freq) / samples_per_symbol_f))
    decoded_bits = []
    for k in range(n_symbols):
        start = int(round(k * samples_per_symbol_f))
        end = int(round((k + 1) * samples_per_symbol_f))
        window = inst_freq[start:end]
        if not window:                          # past the end of data
            break
        # require at least half a symbol's worth of samples to classify
        if len(window) < samples_per_symbol_f * 0.5:
            break
        avg_freq = sum(window) / len(window)
        nearest = min(tones, key=lambda f: abs(f - avg_freq))
        if modulation == "FSK":
            decoded_bits.append("0" if nearest == tones[0] else "1")
        else:
            idx = tones.index(nearest)
            symbol = _get_4fsk_tone_to_dibit(dibit_mapping)[idx]
            decoded_bits.append(symbol)
    return "".join(decoded_bits)


def _load_expected_json_for_batch(path):
    with open(path, "r", encoding="utf-8") as in_file:
        loaded = json.load(in_file)

    settings = {}
    fields_raw = loaded
    if isinstance(loaded, dict):
        settings = loaded.get("settings", {}) if isinstance(loaded.get("settings", {}), dict) else {}
        fields_raw = loaded.get("fields", [])

    if not isinstance(fields_raw, list):
        raise ValueError("Expected JSON must contain a 'fields' array or be an array of fields.")

    parsed_fields = []
    for item in fields_raw:
        if not isinstance(item, dict):
            continue
        hex_value = str(item.get("hex", "")).strip().upper()
        bin_value = str(item.get("bin", "")).strip().replace(" ", "")
        desc_value = str(item.get("desc", "")).strip()
        if not hex_value and not bin_value:
            continue
        try:
            if hex_value and not bin_value:
                integer = int(hex_value, 16)
                bin_value = f"{integer:0{len(hex_value) * 4}b}"
            elif bin_value and not hex_value:
                if any(ch not in "01" for ch in bin_value):
                    raise ValueError()
                padded = bin_value
                pad = (-len(padded)) % 4
                if pad:
                    padded = "0" * pad + padded
                hex_value = f"{int(padded, 2):0{len(padded) // 4}X}"
            else:
                expected_bin = f"{int(hex_value, 16):0{len(hex_value) * 4}b}"
                if expected_bin != bin_value:
                    raise ValueError()
        except ValueError:
            continue
        parsed_fields.append({
            "hex": hex_value,
            "bin": bin_value,
            "desc": desc_value,
            "encoding": item.get("encoding", "None"),
        })

    if not parsed_fields:
        raise ValueError("No valid expected fields found in JSON.")

    return settings, parsed_fields


def _compare_decoded_to_expected(decoded_bits, expected_fields):
    expected_bits = []
    field_results = []
    offset = 0
    for field in expected_fields:
        desc = field["desc"]
        field_bits = encode_field_bits(field["bin"], field.get("encoding", "None"))
        expected_bits.append(field_bits)
        length = len(field_bits)
        decoded_segment = decoded_bits[offset:offset + length]
        match = decoded_segment == field_bits
        field_results.append(
            {
                "desc": desc,
                "expected_len": length,
                "decoded_len": len(decoded_segment),
                "match": match,
                "decoded_bits": decoded_segment,
                "expected_bits": field_bits,
            }
        )
        offset += length

    return "".join(expected_bits), field_results


def run_batch_validate(iq_path, expected_json_path, overrides=None):
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

    samples = read_iq_file(iq_path)
    if modulation == "4FSK":
        expected_tones = [tone0, tone1, tone2, tone3]
    elif modulation in ("FSK", "GFSK"):
        expected_tones = [tone0, tone1]
    else:
        expected_tones = []

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
        _iq = np.asarray(samples, dtype=np.complex128)
        # QPSK-DSSS carries a 4-fold phase ambiguity (no frame sync in the plain
        # validation path to resolve it), so try all four 90-deg rotations and
        # keep the one that matches the expected fields best.
        if getattr(demod, "modem", "") == "qpsk":
            _best = None
            for _r in range(4):
                _bits = demod.demodulate(
                    (_iq * np.exp(1j * np.pi / 2 * _r))).bit_string
                _es, _fr = _compare_decoded_to_expected(_bits, expected_fields)
                _mm = sum(1 for r in _fr if r["match"])
                if _best is None or _mm > _best[0]:
                    _best = (_mm, _bits)
            decoded_bits = _best[1]
        else:
            decoded_bits = demod.demodulate(_iq).bit_string

    expected_string, field_results = _compare_decoded_to_expected(decoded_bits, expected_fields)
    matched_fields = sum(1 for r in field_results if r["match"])
    # IQV-02: the overall verdict is derived from the per-field rows alone so
    # the GUI status line and the Field Validation table agree by construction.
    # A decoded string shorter than the expected fields already shows up as a
    # FAIL row (decoded_len != expected_len), so no separate length check.
    passed = len(field_results) > 0 and matched_fields == len(field_results)

    return {
        "pass": passed,
        "passed": passed,   # alias for callers that read "passed" (older GUI builds)
        "decoded_bits_len": len(decoded_bits),
        "expected_bits_len": len(expected_string),
        "field_count": len(field_results),
        "matched_fields": matched_fields,
        "results": field_results,
    }


class DmrTelemetryTesterApp:
    def __init__(self, root, parent=None):
        self.root = root
        if parent is None:
            self.root.title("DMR Telemetry IQ Validator")
        self.parent = parent if parent is not None else root
        self.iq_path = None
        self.expected_path = None
        self.expected_fields = []
        self.last_inst_freq = []
        self.channel_params = ChannelParameters(enabled=False)
        self.fsk4_dibit_mapping = "dmr"
        self.fsk4_tone_domain = "auto"
        self.fsk4_center_freq_hz = 0.0
        self._fft_job_id = 0
        self._fft_zoom = 1.0
        self._fft_zoom_center_hz = None
        self._fft_last_payload = None
        self._symbol_zoom = 1.0
        self._symbol_zoom_center_hz = None
        self._symbol_last_payload = None
        self._timing_zoom = 1.0
        self.fft_window_type = "hamming"
        self.fft_segment_size = 65536
        self.create_widgets()

    def create_widgets(self):
        self.base_font = tkfont.Font(size=12)
        self.button_font = tkfont.Font(size=12, weight="bold")
        style = ttk.Style(self.root)
        style.configure("Custom.Treeview", rowheight=30, font=self.base_font)
        style.configure("Custom.Treeview.Heading", font=self.button_font)

        frame = tk.Frame(self.parent, padx=14, pady=14)
        frame.pack(fill=tk.BOTH, expand=True)
        for c in range(4):
            frame.grid_columnconfigure(c, weight=1)
        frame.grid_rowconfigure(3, weight=1)
        frame.grid_rowconfigure(5, weight=1)

        file_frame = tk.LabelFrame(frame, text="Input files", font=self.button_font, padx=12, pady=12)
        file_frame.grid(row=0, column=0, columnspan=4, sticky="ew")
        for c in range(4):
            file_frame.grid_columnconfigure(c, weight=1)

        tk.Label(file_frame, text="IQ file:", font=self.base_font).grid(row=0, column=0, sticky="w")
        self.iq_label = tk.Label(file_frame, text="(not loaded)", font=self.base_font, anchor="w")
        self.iq_label.grid(row=0, column=1, sticky="ew")
        tk.Button(file_frame, text="Load BIN", command=self.browse_iq_file, font=self.button_font, padx=10, pady=4).grid(row=0, column=2, sticky="ew")

        tk.Label(file_frame, text="Expected JSON:", font=self.base_font).grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.json_label = tk.Label(file_frame, text="(not loaded)", font=self.base_font, anchor="w")
        self.json_label.grid(row=1, column=1, sticky="ew", pady=(8, 0))
        tk.Button(file_frame, text="Load JSON", command=self.browse_expected_json, font=self.button_font, padx=10, pady=4).grid(row=1, column=2, sticky="ew", pady=(8, 0))

        parm_frame = tk.LabelFrame(frame, text="Demodulation settings", font=self.button_font, padx=12, pady=12)
        parm_frame.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(12, 0))
        for c in range(4):
            parm_frame.grid_columnconfigure(c, weight=1)

        tk.Label(parm_frame, text="Modulation:", font=self.base_font).grid(row=0, column=0, sticky="w")
        self.modulation_var = tk.StringVar(value="FSK")
        modulation_menu = tk.OptionMenu(parm_frame, self.modulation_var, *SUPPORTED_MODULATIONS)
        modulation_menu.config(font=self.base_font)
        modulation_menu.grid(row=0, column=1, sticky="ew")

        tk.Label(parm_frame, text="Sample rate (Hz):", font=self.base_font).grid(row=0, column=2, sticky="w", padx=(16, 0))
        self.sample_rate_entry = tk.Entry(parm_frame, width=12, font=self.base_font)
        self.sample_rate_entry.insert(0, "44400")
        self.sample_rate_entry.grid(row=0, column=3, sticky="ew")
        self.sample_rate_entry.bind("<FocusOut>", lambda _e: self.refresh_signal_time_length())
        self.sample_rate_entry.bind("<Return>", lambda _e: self.refresh_signal_time_length())

        tk.Label(parm_frame, text="Symbol rate:", font=self.base_font).grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.baud_entry = tk.Entry(parm_frame, width=12, font=self.base_font)
        self.baud_entry.insert(0, "1200")
        self.baud_entry.grid(row=1, column=1, sticky="ew", pady=(8, 0))

        tk.Label(parm_frame, text="Samples/bit:", font=self.base_font).grid(row=1, column=2, sticky="w", padx=(16, 0), pady=(8, 0))
        self.samples_per_bit_entry = tk.Entry(parm_frame, width=12, font=self.base_font)
        self.samples_per_bit_entry.insert(0, "37")
        self.samples_per_bit_entry.grid(row=1, column=3, sticky="ew", pady=(8, 0))

        tk.Label(parm_frame, text="Tone 0 (kHz):", font=self.base_font).grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.tone0_entry = tk.Entry(parm_frame, width=12, font=self.base_font)
        self.tone0_entry.insert(0, "1.2")
        self.tone0_entry.grid(row=2, column=1, sticky="ew", pady=(8, 0))

        tk.Label(parm_frame, text="Tone 1 (kHz):", font=self.base_font).grid(row=2, column=2, sticky="w", padx=(16, 0), pady=(8, 0))
        self.tone1_entry = tk.Entry(parm_frame, width=12, font=self.base_font)
        self.tone1_entry.insert(0, "2.2")
        self.tone1_entry.grid(row=2, column=3, sticky="ew", pady=(8, 0))

        tk.Label(parm_frame, text="Tone 2 (kHz):", font=self.base_font).grid(row=3, column=0, sticky="w", pady=(8, 0))
        self.tone2_entry = tk.Entry(parm_frame, width=12, font=self.base_font)
        self.tone2_entry.insert(0, "1.4")
        self.tone2_entry.grid(row=3, column=1, sticky="ew", pady=(8, 0))

        tk.Label(parm_frame, text="Tone 3 (kHz):", font=self.base_font).grid(row=3, column=2, sticky="w", padx=(16, 0), pady=(8, 0))
        self.tone3_entry = tk.Entry(parm_frame, width=12, font=self.base_font)
        self.tone3_entry.insert(0, "2.6")
        self.tone3_entry.grid(row=3, column=3, sticky="ew", pady=(8, 0))

        tk.Label(parm_frame, text="GFSK BW (Hz):", font=self.base_font).grid(row=4, column=2, sticky="w", padx=(16, 0), pady=(8, 0))
        self.gfsk_bw_entry = tk.Entry(parm_frame, width=12, font=self.base_font)
        self.gfsk_bw_entry.insert(0, "250")
        self.gfsk_bw_entry.grid(row=4, column=3, sticky="ew", pady=(8, 0))

        tk.Label(parm_frame, text="Signal time length:", font=self.base_font).grid(row=4, column=0, sticky="w", pady=(8, 0))
        self.signal_time_length_entry = tk.Entry(parm_frame, width=12, font=self.base_font, state="readonly")
        self.signal_time_length_entry.grid(row=4, column=1, sticky="ew", pady=(8, 0))
        self._set_signal_time_length_text("--")

        action_frame = tk.Frame(frame)
        action_frame.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(12, 0))
        action_frame.grid_columnconfigure(0, weight=1)
        action_frame.grid_columnconfigure(1, weight=1)

        tk.Button(action_frame, text="Validate IQ", command=self.validate_iq, font=self.button_font, padx=12, pady=6).grid(row=0, column=0, sticky="ew")
        tk.Button(action_frame, text="Show decoded bits", command=self.show_decoded_bits, font=self.button_font, padx=12, pady=6).grid(row=0, column=1, sticky="ew")

        result_frame = tk.LabelFrame(frame, text="Field validation results", font=self.button_font, padx=12, pady=12)
        result_frame.grid(row=3, column=0, columnspan=4, sticky="nsew", pady=(12, 0))
        result_frame.grid_rowconfigure(0, weight=1)
        result_frame.grid_columnconfigure(0, weight=1)

        self.result_notebook = ttk.Notebook(result_frame)
        self.result_notebook.grid(row=0, column=0, sticky="nsew")

        fields_tab = tk.Frame(self.result_notebook)
        fields_tab.grid_rowconfigure(0, weight=1)
        fields_tab.grid_columnconfigure(0, weight=1)
        self.result_notebook.add(fields_tab, text="Field Validation")

        self.result_table = ttk.Treeview(
            fields_tab,
            columns=("index", "desc", "expected_hex", "decoded_hex", "decoded_value", "expected_len", "decoded_len", "status", "reason"),
            show="headings",
            style="Custom.Treeview",
        )
        self.result_table.heading("index", text="#")
        self.result_table.heading("desc", text="Description")
        self.result_table.heading("expected_hex", text="Expected Hex")
        self.result_table.heading("decoded_hex", text="Decoded Hex")
        self.result_table.heading("decoded_value", text="Decoded Value (Hex)")
        self.result_table.heading("expected_len", text="Expected Bits")
        self.result_table.heading("decoded_len", text="Decoded Bits")
        self.result_table.heading("status", text="Status")
        self.result_table.heading("reason", text="Reason")
        self.result_table.column("index", width=50, minwidth=40, anchor="center")
        self.result_table.column("desc", width=180, minwidth=100, anchor="w", stretch=True)
        self.result_table.column("expected_hex", width=170, minwidth=100, anchor="center", stretch=True)
        self.result_table.column("decoded_hex", width=170, minwidth=100, anchor="center", stretch=True)
        self.result_table.column("decoded_value", width=260, minwidth=140, anchor="w", stretch=True)
        self.result_table.column("expected_len", width=100, minwidth=70, anchor="center")
        self.result_table.column("decoded_len", width=100, minwidth=70, anchor="center")
        self.result_table.column("status", width=100, minwidth=70, anchor="center")
        self.result_table.column("reason", width=220, minwidth=120, anchor="w", stretch=True)
        self.result_table.grid(row=0, column=0, sticky="nsew")

        scroll = tk.Scrollbar(fields_tab, orient="vertical", command=self.result_table.yview)
        self.result_table.configure(yscrollcommand=scroll.set)
        scroll.grid(row=0, column=1, sticky="ns")

        h_scroll = tk.Scrollbar(fields_tab, orient="horizontal", command=self.result_table.xview)
        self.result_table.configure(xscrollcommand=h_scroll.set)
        h_scroll.grid(row=1, column=0, sticky="ew")

        symbols_tab = tk.Frame(self.result_notebook)
        symbols_tab.grid_rowconfigure(0, weight=1)
        symbols_tab.grid_columnconfigure(0, weight=1)
        self.result_notebook.add(symbols_tab, text="Encoded Symbols")

        self.symbol_table = ttk.Treeview(
            symbols_tab,
            columns=("symbol", "expected_bits", "decoded_bits", "expected_freq", "decoded_freq", "status"),
            show="headings",
            style="Custom.Treeview",
        )
        self.symbol_table.heading("symbol", text="#")
        self.symbol_table.heading("expected_bits", text="Expected Bits")
        self.symbol_table.heading("decoded_bits", text="Decoded Bits")
        self.symbol_table.heading("expected_freq", text="Expected Freq (Hz)")
        self.symbol_table.heading("decoded_freq", text="Decoded Freq (Hz)")
        self.symbol_table.heading("status", text="Status")
        self.symbol_table.column("symbol", width=70, minwidth=50, anchor="center")
        self.symbol_table.column("expected_bits", width=130, minwidth=90, anchor="center")
        self.symbol_table.column("decoded_bits", width=130, minwidth=90, anchor="center")
        self.symbol_table.column("expected_freq", width=150, minwidth=100, anchor="center")
        self.symbol_table.column("decoded_freq", width=150, minwidth=100, anchor="center")
        self.symbol_table.column("status", width=100, minwidth=70, anchor="center")
        self.symbol_table.grid(row=0, column=0, sticky="nsew")

        symbol_scroll = tk.Scrollbar(symbols_tab, orient="vertical", command=self.symbol_table.yview)
        self.symbol_table.configure(yscrollcommand=symbol_scroll.set)
        symbol_scroll.grid(row=0, column=1, sticky="ns")

        symbol_h_scroll = tk.Scrollbar(symbols_tab, orient="horizontal", command=self.symbol_table.xview)
        self.symbol_table.configure(xscrollcommand=symbol_h_scroll.set)
        symbol_h_scroll.grid(row=1, column=0, sticky="ew")

        self.summary_label = tk.Label(frame, text="Load an IQ file and expected JSON, then validate.", anchor="w", font=self.base_font)
        self.summary_label.grid(row=4, column=0, columnspan=4, sticky="we", pady=(12, 0))

        fft_frame = tk.LabelFrame(frame, text="Signal Analysis", font=self.button_font, padx=12, pady=12)
        fft_frame.grid(row=5, column=0, columnspan=4, sticky="nsew", pady=(12, 0))
        fft_frame.grid_rowconfigure(0, weight=1)
        fft_frame.grid_columnconfigure(0, weight=1)

        self.plot_notebook = ttk.Notebook(fft_frame)
        self.plot_notebook.grid(row=0, column=0, sticky="nsew")

        fft_tab = tk.Frame(self.plot_notebook)
        fft_tab.grid_rowconfigure(0, weight=1)
        fft_tab.grid_columnconfigure(0, weight=1)
        self.plot_notebook.add(fft_tab, text="FFT")

        spectrum_tab = tk.Frame(self.plot_notebook)
        spectrum_tab.grid_rowconfigure(0, weight=1)
        spectrum_tab.grid_columnconfigure(0, weight=1)
        self.plot_notebook.add(spectrum_tab, text="Symbol Spectrum")

        timing_tab = tk.Frame(self.plot_notebook)
        timing_tab.grid_rowconfigure(0, weight=1)
        timing_tab.grid_columnconfigure(0, weight=1)
        self.plot_notebook.add(timing_tab, text="Symbol Timing")

        self.fft_canvas = tk.Canvas(fft_tab, width=10, height=240, bg="white", highlightthickness=1, highlightbackground="#cccccc")
        self.fft_canvas.grid(row=0, column=0, sticky="nsew")

        self.symbol_canvas = tk.Canvas(spectrum_tab, width=10, height=240, bg="white", highlightthickness=1, highlightbackground="#cccccc")
        self.symbol_canvas.grid(row=0, column=0, sticky="nsew")

        self.symbol_info_label = tk.Label(spectrum_tab, text="Run validation to compute symbol spectrum.", anchor="w", font=self.base_font)
        self.symbol_info_label.grid(row=1, column=0, sticky="we", pady=(8, 0))
        self.symbol_legend_frame = tk.Frame(spectrum_tab)
        self.symbol_legend_frame.grid(row=2, column=0, sticky="we", pady=(4, 0))

        self.symbol_timing_canvas = tk.Canvas(timing_tab, width=10, height=240, bg="white", highlightthickness=1, highlightbackground="#cccccc")
        self.symbol_timing_canvas.grid(row=0, column=0, sticky="nsew")

        self.symbol_timing_h_scroll = tk.Scrollbar(timing_tab, orient="horizontal", command=self.symbol_timing_canvas.xview)
        self.symbol_timing_h_scroll.grid(row=1, column=0, sticky="ew")
        self.symbol_timing_canvas.configure(xscrollcommand=self.symbol_timing_h_scroll.set)

        self.symbol_timing_info_label = tk.Label(timing_tab, text="Run validation to view symbol timing.", anchor="w", font=self.base_font)
        self.symbol_timing_info_label.grid(row=2, column=0, sticky="we", pady=(8, 0))
        self.symbol_timing_legend_frame = tk.Frame(timing_tab)
        self.symbol_timing_legend_frame.grid(row=3, column=0, sticky="we", pady=(4, 0))

        self.fft_canvas.bind("<MouseWheel>", lambda e: self._on_plot_zoom("fft", e))
        self.symbol_canvas.bind("<MouseWheel>", lambda e: self._on_plot_zoom("symbol", e))
        self.symbol_timing_canvas.bind("<MouseWheel>", lambda e: self._on_plot_zoom("timing", e))
        self.fft_canvas.bind("<Button-4>", lambda e: self._on_plot_zoom_button("fft", e, 1))
        self.fft_canvas.bind("<Button-5>", lambda e: self._on_plot_zoom_button("fft", e, -1))
        self.symbol_canvas.bind("<Button-4>", lambda e: self._on_plot_zoom_button("symbol", e, 1))
        self.symbol_canvas.bind("<Button-5>", lambda e: self._on_plot_zoom_button("symbol", e, -1))
        self.symbol_timing_canvas.bind("<Button-4>", lambda e: self._on_plot_zoom_button("timing", e, 1))
        self.symbol_timing_canvas.bind("<Button-5>", lambda e: self._on_plot_zoom_button("timing", e, -1))
        self.fft_canvas.bind("<Double-Button-1>", lambda _e: self._reset_plot_zoom("fft"))
        self.symbol_canvas.bind("<Double-Button-1>", lambda _e: self._reset_plot_zoom("symbol"))
        self.symbol_timing_canvas.bind("<Double-Button-1>", lambda _e: self._reset_plot_zoom("timing"))

        fft_control_frame = tk.Frame(fft_frame)
        fft_control_frame.grid(row=1, column=0, sticky="ew", pady=(8, 0))

        self.strict_tone_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            fft_control_frame,
            text="Strict tone check",
            variable=self.strict_tone_var,
            font=self.base_font,
        ).grid(row=0, column=0, sticky="w")

        tk.Label(fft_control_frame, text="Top N peaks:", font=self.base_font).grid(row=0, column=1, sticky="w", padx=(14, 4))
        self.top_n_entry = tk.Entry(fft_control_frame, width=6, font=self.base_font)
        self.top_n_entry.insert(0, "8")
        self.top_n_entry.grid(row=0, column=2, sticky="w")

        tk.Label(fft_control_frame, text="Tolerance override (Hz):", font=self.base_font).grid(row=0, column=3, sticky="w", padx=(14, 4))
        self.tone_tolerance_entry = tk.Entry(fft_control_frame, width=8, font=self.base_font)
        self.tone_tolerance_entry.grid(row=0, column=4, sticky="w")

        tk.Label(fft_control_frame, text="Window:", font=self.base_font).grid(row=0, column=5, sticky="w", padx=(14, 4))
        self.window_type_var = tk.StringVar(value="hamming")
        window_menu = tk.OptionMenu(fft_control_frame, self.window_type_var, "hamming", "hanning", "blackman", "bartlett", "rect")
        window_menu.config(font=self.base_font, width=10)
        window_menu.grid(row=0, column=6, sticky="ew")

        tk.Label(fft_control_frame, text="FFT Size:", font=self.base_font).grid(row=0, column=7, sticky="w", padx=(14, 4))
        self.fft_size_var = tk.StringVar(value="65536")
        size_menu = tk.OptionMenu(fft_control_frame, self.fft_size_var, "4096", "8192", "16384", "32768", "65536", "131072")
        size_menu.config(font=self.base_font, width=8)
        size_menu.grid(row=0, column=8, sticky="ew")

        self.fft_progress = ttk.Progressbar(fft_control_frame, mode="indeterminate", length=100)
        self.fft_progress.grid(row=0, column=9, sticky="w", padx=(14, 4))
        self.fft_progress.grid_remove()
        self.fft_busy_label = tk.Label(fft_control_frame, text="", font=self.base_font, anchor="w")
        self.fft_busy_label.grid(row=0, column=10, sticky="w")
        
        # Second row of controls
        tk.Label(fft_control_frame, text="FFT sample rate (Hz):", font=self.base_font).grid(row=1, column=0, sticky="w", pady=(8, 0), padx=(0, 4))
        self.fft_sample_rate_entry = tk.Entry(fft_control_frame, width=10, font=self.base_font)
        self.fft_sample_rate_entry.insert(0, "192000")
        self.fft_sample_rate_entry.grid(row=1, column=1, sticky="w", pady=(8, 0))

        tk.Label(fft_control_frame, text="Mouse wheel: zoom | Double-click: reset", font=self.base_font).grid(row=1, column=2, columnspan=8, sticky="w", padx=(14, 0), pady=(8, 0))


        self.fft_info_label = tk.Label(fft_frame, text="Run validation to compute FFT from the demodulated signal.", anchor="w", font=self.base_font)
        self.fft_info_label.grid(row=2, column=0, sticky="we", pady=(8, 0))

        self.fft_tone_label = tk.Label(fft_frame, text="", anchor="w", justify="left", font=self.base_font)
        self.fft_tone_label.grid(row=3, column=0, sticky="we", pady=(4, 0))

        self.decoded_bits = ""

    def _tone_hz_to_khz_text(self, value):
        return f"{float(value) / 1000.0:.6f}"

    def _set_signal_time_length_text(self, text):
        self.signal_time_length_entry.config(state="normal")
        self.signal_time_length_entry.delete(0, tk.END)
        self.signal_time_length_entry.insert(0, str(text))
        self.signal_time_length_entry.config(state="readonly")

    def _estimate_iq_duration_seconds(self, iq_path, sample_rate):
        if not iq_path or not os.path.exists(iq_path):
            return None
        try:
            rate = float(sample_rate)
        except Exception:
            return None
        if rate <= 0:
            return None

        try:
            file_size = os.path.getsize(iq_path)
            data_offset = 0
            if file_size >= DPMG_BIN_HEADER_SIZE:
                with open(iq_path, "rb") as in_file:
                    header = in_file.read(DPMG_BIN_HEADER_SIZE)
                if len(header) >= DPMG_BIN_HEADER_SIZE and header[:4] == DPMG_BIN_MAGIC:
                    _magic, _version, data_format, _reserved, _sample_rate, data_offset = struct.unpack(
                        ">4sBBHII", header
                    )
                    if data_format != 1 or data_offset < DPMG_BIN_HEADER_SIZE:
                        return None
            payload_bytes = max(0, file_size - data_offset)
            complex_sample_count = payload_bytes // 4  # int16 I + int16 Q
            if complex_sample_count <= 0:
                return None
            return complex_sample_count / rate
        except Exception:
            return None

    def refresh_signal_time_length(self):
        sample_rate_text = self.sample_rate_entry.get().strip()
        try:
            sample_rate = float(sample_rate_text)
        except Exception:
            self._set_signal_time_length_text("--")
            return

        duration_s = self._estimate_iq_duration_seconds(self.iq_path, sample_rate)
        if duration_s is None:
            self._set_signal_time_length_text("--")
            return
        self._set_signal_time_length_text(f"{duration_s:.6f} s")

    def browse_iq_file(self):
        path = filedialog.askopenfilename(
            title="Select IQ file",
            defaultextension=".bin",
            filetypes=[("IQ files", "*.bin"), ("All files", "*")],
        )
        if not path:
            return
        self.iq_path = path
        self.iq_label.config(text=os.path.basename(path))
        self.refresh_signal_time_length()

    def browse_expected_json(self):
        path = filedialog.askopenfilename(
            title="Select expected field JSON",
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*")],
        )
        if not path:
            return
        self.expected_path = path
        self.json_label.config(text=os.path.basename(path))

        # Convenience: auto-load waveform BIN with the same basename as the JSON.
        base, _ = os.path.splitext(path)
        iq_candidate = f"{base}.bin"
        if os.path.exists(iq_candidate):
            self.iq_path = iq_candidate
            self.iq_label.config(text=os.path.basename(iq_candidate))
            self.refresh_signal_time_length()

        self.load_expected_json(path)

    def load_expected_json(self, path):
        try:
            with open(path, "r", encoding="utf-8") as in_file:
                loaded = json.load(in_file)
        except Exception as exc:
            messagebox.showerror("Load failed", f"Failed to load expected JSON: {exc}")
            return

        settings = None
        if isinstance(loaded, dict):
            settings = loaded.get("settings")
            loaded = loaded.get("fields", [])

        if not isinstance(loaded, list):
            messagebox.showerror("Invalid file", "Loaded JSON does not contain a list of fields.")
            return

        if settings and isinstance(settings, dict):
            self.apply_settings(settings)

        parsed_fields = []
        for item in loaded:
            if not isinstance(item, dict):
                continue
            hex_value = item.get("hex", "").strip().upper()
            bin_value = item.get("bin", "").strip().replace(" ", "")
            desc_value = item.get("desc", "").strip()
            if not hex_value and not bin_value:
                continue
            try:
                if hex_value and not bin_value:
                    bin_value = self.hex_to_bin(hex_value)
                elif bin_value and not hex_value:
                    hex_value = self.bin_to_hex(bin_value)
                else:
                    expected_bin = self.hex_to_bin(hex_value)
                    if expected_bin != bin_value:
                        raise ValueError()
            except ValueError:
                continue
            parsed_fields.append({"hex": hex_value, "bin": bin_value, "desc": desc_value, "encoding": item.get("encoding", "None")})

        if not parsed_fields:
            messagebox.showwarning("No valid fields", "No valid expected fields were found.")
            return

        self.expected_fields = parsed_fields
        self.summary_label.config(text=f"Loaded {len(parsed_fields)} expected fields from {os.path.basename(path)}")

    def apply_settings(self, settings):
        def set_if_present(widget, key, transform=str):
            value = settings.get(key)
            if value is not None:
                widget.delete(0, tk.END)
                widget.insert(0, transform(value))

        if not isinstance(settings, dict):
            return

        set_if_present(self.sample_rate_entry, "sample_rate")
        symbol_rate = settings.get("symbol_rate")
        if symbol_rate is None:
            symbol_rate = settings.get("baud")
        if symbol_rate is not None:
            self.baud_entry.delete(0, tk.END)
            self.baud_entry.insert(0, str(symbol_rate))
        set_if_present(self.samples_per_bit_entry, "samples_per_bit")
        set_if_present(self.tone0_entry, "tone0", self._tone_hz_to_khz_text)
        set_if_present(self.tone1_entry, "tone1", self._tone_hz_to_khz_text)
        set_if_present(self.tone2_entry, "tone2", self._tone_hz_to_khz_text)
        set_if_present(self.tone3_entry, "tone3", self._tone_hz_to_khz_text)
        set_if_present(self.gfsk_bw_entry, "gfsk_bw_hz")
        self.fsk4_dibit_mapping = _normalize_4fsk_dibit_mapping(settings.get("dibit_mapping", "dmr"))
        self.fsk4_tone_domain = str(settings.get("tone_domain", "auto"))
        self.fsk4_center_freq_hz = float(settings.get("center_freq_hz", 0.0))

        modulation = normalize_modulation(settings.get("modulation"))
        if modulation is not None:
            self.modulation_var.set(modulation)

        # Optional channel simulation block
        channel_cfg = settings.get("channel_simulation")
        if isinstance(channel_cfg, dict):
            self.channel_params = ChannelParameters(
                enabled=bool(channel_cfg.get("enabled", False)),
                awgn_snr_db=channel_cfg.get("awgn_snr_db"),
                freq_offset_hz=float(channel_cfg.get("freq_offset_hz", 0.0)),
                phase_noise_deg_rms=float(channel_cfg.get("phase_noise_deg_rms", 0.0)),
                multipath_delay_us=float(channel_cfg.get("multipath_delay_us", 0.0)),
                multipath_attenuation_db=float(channel_cfg.get("multipath_attenuation_db", 0.0)),
                symbol_timing_error_ppm=float(channel_cfg.get("symbol_timing_error_ppm", 0.0)),
            )

        self.summary_label.config(text="Loaded JSON settings and expected fields.")

    def validate_iq(self):
        if not self.iq_path:
            messagebox.showwarning("Missing IQ file", "Please select an IQ file first.")
            return
        if not self.expected_fields:
            messagebox.showwarning("Missing expected fields", "Please load an expected field JSON first.")
            return

        try:
            sample_rate = float(self.sample_rate_entry.get())
            symbol_rate = float(self.baud_entry.get())
            samples_per_bit = int(self.samples_per_bit_entry.get())
            modulation = self.modulation_var.get()
            tone0 = float(self.tone0_entry.get()) * 1000.0
            tone1 = float(self.tone1_entry.get()) * 1000.0
            tone2 = float(self.tone2_entry.get()) * 1000.0
            tone3 = float(self.tone3_entry.get()) * 1000.0
            gfsk_bw_hz = float(self.gfsk_bw_entry.get())
        except ValueError:
            messagebox.showerror("Invalid parameters", "Check sample rate, symbol rate, samples/bit, and tone frequencies (kHz).")
            return

        if sample_rate <= 0 or symbol_rate <= 0 or samples_per_bit <= 0:
            messagebox.showerror("Invalid parameters", "Sample rate, symbol rate, and samples/bit must be positive.")
            return
        if modulation == "GFSK" and gfsk_bw_hz <= 0:
            messagebox.showerror("Invalid parameters", "GFSK bandwidth must be positive.")
            return

        try:
            top_n_peaks = int(self.top_n_entry.get())
        except ValueError:
            messagebox.showerror("Invalid FFT setting", "Top N peaks must be an integer.")
            return
        if top_n_peaks <= 0:
            messagebox.showerror("Invalid FFT setting", "Top N peaks must be greater than zero.")
            return
        strict_mode = bool(self.strict_tone_var.get())

        tolerance_text = self.tone_tolerance_entry.get().strip()
        tolerance_override_hz = None
        if tolerance_text:
            try:
                tolerance_override_hz = float(tolerance_text)
            except ValueError:
                messagebox.showerror("Invalid FFT setting", "Tolerance override must be numeric (Hz).")
                return
            if tolerance_override_hz <= 0:
                messagebox.showerror("Invalid FFT setting", "Tolerance override must be greater than zero.")
                return

        fft_sample_rate_text = self.fft_sample_rate_entry.get().strip()
        try:
            fft_sample_rate = float(fft_sample_rate_text) if fft_sample_rate_text else sample_rate
        except ValueError:
            messagebox.showerror("Invalid FFT setting", "FFT sample rate must be numeric (Hz).")
            return
        if fft_sample_rate <= 0:
            messagebox.showerror("Invalid FFT setting", "FFT sample rate must be greater than zero.")
            return

        try:
            samples = read_iq_file(self.iq_path)
        except Exception as exc:
            messagebox.showerror("Load failed", str(exc))
            return
        self._set_signal_time_length_text(f"{(len(samples) / sample_rate):.6f} s")

        # Apply optional channel simulation before demodulation.
        if self.channel_params.enabled:
            try:
                simulator = ChannelSimulator(sample_rate)
                sim_samples = simulator.apply(np.asarray(samples, dtype=np.complex128), self.channel_params)
                samples = sim_samples.tolist()
            except Exception as exc:
                messagebox.showerror("Channel simulation failed", str(exc))
                return

        self.last_inst_freq = demodulate_instantaneous_frequency(samples, sample_rate)
        if modulation == "4FSK":
            tone0, tone1, tone2, tone3 = _resolve_4fsk_tones_domain(
                tone0,
                tone1,
                tone2,
                tone3,
                self.fsk4_center_freq_hz,
                self.fsk4_tone_domain,
            )
            expected_tones = [tone0, tone1, tone2, tone3]
        elif modulation in ("FSK", "GFSK"):
            expected_tones = [tone0, tone1]
        else:
            expected_tones = []
        self.update_fft_plot(
            samples,
            sample_rate,
            fft_sample_rate,
            modulation,
            expected_tones,
            symbol_rate,
            gfsk_bw_hz,
            strict_mode,
            top_n_peaks,
            tolerance_override_hz,
        )

        if modulation in ("FSK", "4FSK"):
            self.decoded_bits = estimate_symbol_bits(
                samples,
                sample_rate,
                symbol_rate,
                modulation,
                expected_tones,
                samples_per_bit,
                self.fsk4_dibit_mapping,
            )
        else:
            try:
                demod = get_demodulator(
                    modulation,
                    sample_rate=sample_rate,
                    symbol_rate=symbol_rate,
                    samples_per_bit=samples_per_bit,
                    tone0=tone0,
                    tone1=tone1,
                    tone2=tone2,
                    tone3=tone3,
                    dibit_mapping=self.fsk4_dibit_mapping,
                    gfsk_bw_hz=gfsk_bw_hz,
                )
                demod_result = demod.demodulate(np.asarray(samples, dtype=np.complex128))
                self.decoded_bits = demod_result.bit_string
            except Exception as exc:
                messagebox.showerror("Demodulation failed", str(exc))
                return

        expected_string, field_results = self.compare_to_expected(self.decoded_bits)
        self.show_results(field_results)
        symbol_rows = self.build_symbol_rows(
            expected_string,
            self.decoded_bits,
            modulation,
            expected_tones,
            self.last_inst_freq,
            samples_per_bit,
        )
        self.show_symbol_results(symbol_rows)
        self.update_symbol_spectrum_plot(
            self.last_inst_freq,
            modulation,
            sample_rate,
            symbol_rate,
            samples_per_bit,
            expected_tones,
        )
        self.update_symbol_timing_plot(symbol_rows, modulation, expected_tones)

        passed = all(field["match"] for field in field_results) and len(self.decoded_bits) >= len(expected_string)
        if passed:
            self.summary_label.config(text="PASS: All fields accounted for and matched.")
        else:
            self.summary_label.config(text="FAIL: Some fields did not match or were missing.")

    def get_top_n_peak_bins(self, mags, top_n):
        if mags.size < 3:
            n = max(1, min(int(top_n), int(mags.size)))
            indices = np.argpartition(mags, -n)[-n:]
            return sorted((int(i) for i in indices), key=lambda i: mags[i], reverse=True)

        local_max = np.where((mags[1:-1] >= mags[:-2]) & (mags[1:-1] >= mags[2:]))[0] + 1
        if local_max.size == 0:
            local_max = np.arange(mags.size)

        n = max(1, min(int(top_n), int(local_max.size)))
        peak_mags = mags[local_max]
        top_rel = np.argpartition(peak_mags, -n)[-n:]
        indices = local_max[top_rel]
        return sorted((int(i) for i in indices), key=lambda i: mags[i], reverse=True)

    def analyze_expected_tones(self, freqs, mags, expected_tones, strict_mode, top_n, tolerance_override_hz):
        if len(expected_tones) < 1:
            return [], 0.0, []

        bin_res = float(freqs[1] - freqs[0]) if len(freqs) > 1 else 1.0
        sorted_tones = sorted(expected_tones)
        if len(sorted_tones) > 1:
            min_spacing = min(sorted_tones[i + 1] - sorted_tones[i] for i in range(len(sorted_tones) - 1))
        else:
            min_spacing = max(100.0, abs(sorted_tones[0]) * 0.1)
        auto_tolerance_hz = max(2.0 * bin_res, 0.2 * min_spacing, 25.0)
        tolerance_hz = float(tolerance_override_hz) if tolerance_override_hz is not None else auto_tolerance_hz

        top_peak_bins = self.get_top_n_peak_bins(mags, top_n)
        peak_rank = {idx: rank + 1 for rank, idx in enumerate(top_peak_bins)}

        results = []
        for idx, tone in enumerate(expected_tones):
            nearest_idx = int(np.argmin(np.abs(freqs - tone)))
            half_bins = max(1, int(round(tolerance_hz / max(bin_res, 1e-12))))
            start = max(0, nearest_idx - half_bins)
            end = min(mags.size - 1, nearest_idx + half_bins)
            local_idx = start + int(np.argmax(mags[start:end + 1]))

            detected_freq = float(freqs[local_idx])
            detected_mag = float(mags[local_idx])
            err_hz = detected_freq - tone
            in_top_n = local_idx in peak_rank
            alignment_ok = abs(err_hz) <= tolerance_hz
            strict_ok = in_top_n if strict_mode else True
            results.append({
                "idx": idx,
                "expected": float(tone),
                "detected": detected_freq,
                "mag": detected_mag,
                "err": err_hz,
                "alignment_ok": alignment_ok,
                "in_top_n": in_top_n,
                "rank": peak_rank.get(local_idx),
                "pass": alignment_ok and strict_ok,
            })
        return results, tolerance_hz, top_peak_bins

    def analyze_expected_tones_from_inst_freq(self, inst_freq, expected_tones, strict_mode, top_n, tolerance_override_hz):
        if len(expected_tones) < 1 or len(inst_freq) < 1:
            return [], 0.0

        sorted_tones = sorted(float(t) for t in expected_tones)
        if len(sorted_tones) > 1:
            min_spacing = min(sorted_tones[i + 1] - sorted_tones[i] for i in range(len(sorted_tones) - 1))
        else:
            min_spacing = max(100.0, abs(sorted_tones[0]) * 0.1)

        auto_tolerance_hz = max(25.0, 0.05 * min_spacing)
        tolerance_hz = float(tolerance_override_hz) if tolerance_override_hz is not None else auto_tolerance_hz
        assignment_limit_hz = max(0.45 * min_spacing, 2.0 * tolerance_hz)

        buckets = {idx: [] for idx in range(len(expected_tones))}
        for freq in inst_freq:
            nearest_idx = min(range(len(expected_tones)), key=lambda idx: abs(freq - expected_tones[idx]))
            if abs(freq - expected_tones[nearest_idx]) <= assignment_limit_hz:
                buckets[nearest_idx].append(float(freq))

        ranked = sorted(
            range(len(expected_tones)),
            key=lambda idx: (len(buckets[idx]), -abs(expected_tones[idx])),
            reverse=True,
        )
        rank_map = {idx: rank + 1 for rank, idx in enumerate(ranked) if buckets[idx]}

        results = []
        for idx, tone in enumerate(expected_tones):
            measurements = buckets[idx]
            if measurements:
                detected_freq = float(np.median(np.asarray(measurements, dtype=np.float64)))
                err_hz = detected_freq - float(tone)
                alignment_ok = abs(err_hz) <= tolerance_hz
                rank = rank_map.get(idx)
                in_top_n = rank is not None and rank <= max(1, int(top_n))
                strict_ok = in_top_n if strict_mode else True
                mag = float(len(measurements))
            else:
                detected_freq = float("nan")
                err_hz = float("inf")
                alignment_ok = False
                rank = None
                in_top_n = False
                strict_ok = not strict_mode
                mag = 0.0

            results.append({
                "idx": idx,
                "expected": float(tone),
                "detected": detected_freq,
                "mag": mag,
                "err": err_hz,
                "alignment_ok": alignment_ok,
                "in_top_n": in_top_n,
                "rank": rank,
                "pass": alignment_ok and strict_ok,
            })

        return results, tolerance_hz

    def _estimate_encoded_bandwidth_hz(self, modulation, symbol_rate, expected_tones, gfsk_bw_hz):
        modulation = str(modulation)
        try:
            symbol_rate = float(symbol_rate)
        except Exception:
            symbol_rate = 0.0

        tones = [float(t) for t in expected_tones] if expected_tones else []
        if modulation in ("FSK", "GFSK") and len(tones) >= 2:
            tone_sep = abs(tones[1] - tones[0])
            freq_dev = 0.5 * tone_sep
            if modulation == "GFSK":
                shaping_bw_hz = max(float(gfsk_bw_hz), 0.5 * symbol_rate)
                return max(2.0 * (freq_dev + shaping_bw_hz), symbol_rate)
            return max(2.0 * (freq_dev + symbol_rate), symbol_rate)

        if modulation == "4FSK" and len(tones) >= 4:
            ordered = sorted(tones)
            deltas = [ordered[i + 1] - ordered[i] for i in range(len(ordered) - 1)]
            spacing_hz = min((d for d in deltas if d > 0), default=0.0)
            return max(3.0 * spacing_hz + 2.0 * symbol_rate, symbol_rate)

        if modulation in ("QPSK", "π/4-QPSK", "DQPSK", "OQPSK"):
            return max(symbol_rate * 1.35, symbol_rate)

        if modulation == "BPSK":
            return max(symbol_rate * 1.5, symbol_rate)

        if symbol_rate > 0:
            return 2.0 * symbol_rate
        return 0.0

    def _set_fft_busy(self, busy, message=""):
        if busy:
            self.fft_progress.grid()
            self.fft_progress.start(12)
            self.fft_busy_label.config(text=message or "Computing FFT...")
        else:
            self.fft_progress.stop()
            self.fft_progress.grid_remove()
            self.fft_busy_label.config(text=message)

    def _get_tone_color_map(self, expected_tones):
        palette = ["#d62728", "#1f77b4", "#2ca02c", "#ff7f0e", "#9467bd", "#17becf"]
        return {idx: palette[idx % len(palette)] for idx in range(len(expected_tones))}

    def _render_tone_legend(self, container, expected_tones, tone_colors):
        for child in container.winfo_children():
            child.destroy()

        if not expected_tones:
            return

        tk.Label(container, text="Legend:", font=self.base_font).pack(side=tk.LEFT, padx=(0, 8))
        for idx, tone in enumerate(expected_tones):
            item = tk.Frame(container)
            item.pack(side=tk.LEFT, padx=(0, 10))
            tk.Label(item, width=2, bg=tone_colors.get(idx, "#666666"), relief="solid", borderwidth=1).pack(side=tk.LEFT, padx=(0, 4))
            tk.Label(item, text=f"T{idx}={tone:.1f} Hz", font=self.base_font).pack(side=tk.LEFT)

    def _on_plot_zoom(self, plot_name, event):
        direction = 1 if getattr(event, "delta", 0) > 0 else -1
        self._on_plot_zoom_direction(plot_name, direction)

    def _on_plot_zoom_button(self, plot_name, _event, direction):
        self._on_plot_zoom_direction(plot_name, direction)

    def _on_plot_zoom_direction(self, plot_name, direction):
        if direction > 0:
            factor = 1.2
        else:
            factor = 1.0 / 1.2

        if plot_name == "fft":
            self._fft_zoom = min(20.0, max(1.0, self._fft_zoom * factor))
            if self._fft_last_payload is not None:
                self._render_fft_plot(self._fft_last_payload)
            return

        if plot_name == "symbol":
            self._symbol_zoom = min(20.0, max(1.0, self._symbol_zoom * factor))
            if self._symbol_last_payload is not None:
                self._render_symbol_spectrum_plot(self._symbol_last_payload)
            return

        if plot_name == "timing":
            self._timing_zoom = min(20.0, max(1.0, self._timing_zoom * factor))
            if hasattr(self, "_timing_last_data") and self._timing_last_data is not None:
                rows, modulation, expected_tones = self._timing_last_data
                self.update_symbol_timing_plot(rows, modulation, expected_tones)

    def _reset_plot_zoom(self, plot_name):
        if plot_name == "fft":
            self._fft_zoom = 1.0
            if self._fft_last_payload is not None:
                self._render_fft_plot(self._fft_last_payload)
            return
        if plot_name == "symbol":
            self._symbol_zoom = 1.0
            if self._symbol_last_payload is not None:
                self._render_symbol_spectrum_plot(self._symbol_last_payload)
            return
        if plot_name == "timing":
            self._timing_zoom = 1.0
            if hasattr(self, "_timing_last_data") and self._timing_last_data is not None:
                rows, modulation, expected_tones = self._timing_last_data
                self.update_symbol_timing_plot(rows, modulation, expected_tones)

    def _apply_center_zoom(self, x_min, x_max, zoom, center=None):
        if x_max <= x_min:
            return x_min, x_max
        zoom = max(1.0, float(zoom))
        if zoom <= 1.000001:
            return x_min, x_max
        current_span = x_max - x_min
        if center is None:
            center = x_min + 0.5 * current_span
        span = current_span / zoom
        return center - 0.5 * span, center + 0.5 * span

    def _compute_fft_payload(
        self,
        iq_samples,
        sample_rate,
        fft_sample_rate,
        modulation,
        expected_tones,
        symbol_rate,
        gfsk_bw_hz,
        strict_mode,
        top_n,
        tolerance_override_hz,
        inst_freq,
        plot_w,
        window_type="hamming",
        fft_segment_size=65536,
    ):
        if len(iq_samples) < 8:
            return {
                "kind": "not_enough",
                "info": "FFT unavailable: capture more IQ samples.",
                "tone": "",
                "canvas": "Not enough IQ samples for FFT",
            }

        use_centered_complex_fft = modulation == "4FSK"
        fft_data = np.asarray(iq_samples, dtype=np.complex128)
        fft_rate = float(sample_rate)
        
        # Configurable FFT segment size; larger segments give higher resolution
        # Take middle segment to avoid startup/shutdown transients
        segment_size = max(4096, min(int(fft_segment_size), fft_data.size // 2))
        if fft_data.size > segment_size:
            start_idx = (fft_data.size - segment_size) // 2
            fft_data = fft_data[start_idx:start_idx + segment_size]

        # Select window function based on user selection
        window_type = str(window_type or "hamming").lower()
        if window_type == "hanning":
            window = np.hanning(fft_data.size)
        elif window_type == "blackman":
            window = np.blackman(fft_data.size)
        elif window_type == "bartlett":
            window = np.bartlett(fft_data.size)
        elif window_type == "rect":
            window = np.ones(fft_data.size)  # Rectangular window (no windowing)
        else:
            window = np.hamming(fft_data.size)  # Default to Hamming

        if use_centered_complex_fft:
            data = np.asarray(fft_data, dtype=np.complex128)
            data = data - np.mean(data)
            spectrum = np.fft.fftshift(np.fft.fft(data * window))
            freqs = np.fft.fftshift(np.fft.fftfreq(data.size, d=1.0 / fft_rate))
        else:
            data = np.asarray(fft_data.real, dtype=np.float64)
            data = data - np.mean(data)
            spectrum = np.fft.rfft(data * window)
            freqs = np.fft.rfftfreq(data.size, d=1.0 / fft_rate)
        mags = 20.0 * np.log10(np.abs(spectrum) + 1e-12)

        if mags.size < 2:
            return {
                "kind": "empty",
                "info": "FFT unavailable.",
                "tone": "",
                "canvas": "FFT result empty",
            }

        y_min = float(np.min(mags))
        y_max = float(np.max(mags))
        if abs(y_max - y_min) < 1e-9:
            y_max = y_min + 1.0

        x_min_full = float(freqs[0])
        x_max_full = float(freqs[-1])
        x_min = x_min_full
        x_max = x_max_full
        if x_max <= x_min:
            x_min = 0.0
            x_max = 1.0

        encoded_bw_hz = self._estimate_encoded_bandwidth_hz(modulation, symbol_rate, expected_tones, gfsk_bw_hz)
        if encoded_bw_hz > 0.0:
            if expected_tones:
                center_hz = 0.5 * (min(expected_tones) + max(expected_tones))
            elif modulation in ("QPSK", "BPSK", "π/4-QPSK", "DQPSK", "OQPSK"):
                center_hz = 0.0
            else:
                center_hz = 0.5 * (x_min_full + x_max_full)

            span_hz = max(encoded_bw_hz * 1.25, 100.0)
            zoom_min = center_hz - 0.5 * span_hz
            zoom_max = center_hz + 0.5 * span_hz

            if not use_centered_complex_fft:
                zoom_min = max(0.0, zoom_min)

            zoom_min = max(x_min_full, zoom_min)
            zoom_max = min(x_max_full, zoom_max)

            if zoom_max - zoom_min > 1.0:
                x_min = zoom_min
                x_max = zoom_max

        max_pts = max(2, int(plot_w))
        if mags.size > max_pts:
            idx = np.linspace(0, mags.size - 1, max_pts, dtype=int)
            ds_freqs = freqs[idx]
            ds_mags = mags[idx]
        else:
            ds_freqs = freqs
            ds_mags = mags

        tone_results, tolerance_hz, top_peak_bins = self.analyze_expected_tones(
            freqs,
            mags,
            expected_tones,
            strict_mode,
            top_n,
            tolerance_override_hz,
        )
        tone_measurement_label = "fft"
        if modulation == "4FSK" and inst_freq:
            tone_results, tolerance_hz = self.analyze_expected_tones_from_inst_freq(
                inst_freq,
                expected_tones,
                strict_mode,
                top_n,
                tolerance_override_hz,
            )
            tone_measurement_label = "inst-freq"

        peak_freq_mag = [(float(freqs[i]), float(mags[i])) for i in top_peak_bins]

        peak_idx = int(np.argmax(mags))
        peak_freq = float(freqs[peak_idx])
        peak_mag = float(mags[peak_idx])
        fft_label = "Centered FFT of complex IQ" if use_centered_complex_fft else "FFT of IQ real component"
        info_text = (
            f"{fft_label}: N={data.size}, peak={peak_freq:.1f} Hz at {peak_mag:.1f} dB, "
            f"zoom {x_min:.0f}..{x_max:.0f} Hz, FFT Fs={fft_rate:.0f} Hz"
        )

        if tone_results:
            status = "PASS" if all(t["pass"] for t in tone_results) else "CHECK"
            tone_lines = []
            for t in tone_results:
                marker = "OK" if t["pass"] else "OFF"
                rank_text = f"rank #{t['rank']}" if t["rank"] is not None else "not top-N"
                tone_lines.append(
                    f"T{t['idx']}: exp {t['expected']:.1f} Hz, {tone_measurement_label} {t['detected']:.1f} Hz, err {t['err']:+.1f} Hz, {rank_text} ({marker})"
                )
            details = " | ".join(tone_lines)
            mode_text = f"strict top-{max(1, top_n)}" if strict_mode else "alignment-only"
            tol_mode = "override" if tolerance_override_hz is not None else "auto"
            tone_text = f"{modulation} tone check ({status}, {mode_text}, tol {tol_mode} +/-{tolerance_hz:.1f} Hz): {details}"
        else:
            tone_text = ""

        return {
            "kind": "ok",
            "x_min": x_min,
            "x_max": x_max,
            "y_min": y_min,
            "y_max": y_max,
            "ds_freqs": ds_freqs,
            "ds_mags": ds_mags,
            "peaks": peak_freq_mag,
            "tone_results": tone_results,
            "info": info_text,
            "tone": tone_text,
        }

    def _on_fft_payload_ready(self, job_id, payload):
        if job_id != self._fft_job_id:
            return
        self._set_fft_busy(False, "FFT ready")
        self._fft_last_payload = payload
        self._render_fft_plot(payload)

    def _render_fft_plot(self, payload):
        canvas = self.fft_canvas
        canvas.delete("all")
        width = int(canvas.winfo_width())
        height = int(canvas.winfo_height())
        if width < 50:
            width = int(canvas.cget("width"))
        if height < 50:
            height = int(canvas.cget("height"))

        if payload["kind"] != "ok":
            canvas.create_text(width // 2, height // 2, text=payload.get("canvas", "FFT unavailable"), fill="#666666", font=("TkDefaultFont", 11))
            self.fft_info_label.config(text=payload.get("info", "FFT unavailable."))
            self.fft_tone_label.config(text=payload.get("tone", ""))
            return

        margin_l = 58
        margin_r = 18
        margin_t = 16
        margin_b = 32
        plot_w = max(1, width - margin_l - margin_r)
        plot_h = max(1, height - margin_t - margin_b)

        x_min = float(payload["x_min"])
        x_max = float(payload["x_max"])
        y_min = float(payload["y_min"])
        y_max = float(payload["y_max"])
        ds_freqs = payload["ds_freqs"]
        ds_mags = payload["ds_mags"]

        x_min, x_max = self._apply_center_zoom(x_min, x_max, self._fft_zoom, self._fft_zoom_center_hz)

        points = []
        for i in range(ds_mags.size):
            x = margin_l + ((ds_freqs[i] - x_min) / (x_max - x_min)) * plot_w
            y = margin_t + (1.0 - (ds_mags[i] - y_min) / (y_max - y_min)) * plot_h
            points.extend((x, y))

        canvas.create_rectangle(margin_l, margin_t, margin_l + plot_w, margin_t + plot_h, outline="#bbbbbb")
        canvas.create_line(margin_l, margin_t + plot_h, margin_l + plot_w, margin_t + plot_h, fill="#999999")
        canvas.create_line(margin_l, margin_t, margin_l, margin_t + plot_h, fill="#999999")

        if len(points) >= 4:
            canvas.create_line(points, fill="#1f77b4", width=2, smooth=True)

        for peak_freq, peak_mag in payload["peaks"]:
            x_peak = margin_l + ((peak_freq - x_min) / (x_max - x_min)) * plot_w
            y_peak = margin_t + (1.0 - (peak_mag - y_min) / (y_max - y_min)) * plot_h
            if margin_l <= x_peak <= margin_l + plot_w:
                canvas.create_oval(x_peak - 2, y_peak - 2, x_peak + 2, y_peak + 2, fill="#ff7f0e", outline="#ff7f0e")

        for tone in payload["tone_results"]:
            x = margin_l + ((tone["expected"] - x_min) / (x_max - x_min)) * plot_w
            if x < margin_l or x > margin_l + plot_w:
                continue
            marker_color = "#2ca02c" if tone["pass"] else "#d62728"
            canvas.create_line(x, margin_t, x, margin_t + plot_h, fill=marker_color, dash=(4, 3), width=1)
            canvas.create_text(x + 4, margin_t + 8 + 12 * (tone["idx"] % 2), text=f"T{tone['idx']}", anchor="w", fill=marker_color, font=("TkDefaultFont", 9))

        canvas.create_text(margin_l - 8, margin_t, text=f"{y_max:.1f} dB", anchor="e", fill="#555555", font=("TkDefaultFont", 9))
        canvas.create_text(margin_l - 8, margin_t + plot_h, text=f"{y_min:.1f} dB", anchor="e", fill="#555555", font=("TkDefaultFont", 9))
        canvas.create_text(margin_l, margin_t + plot_h + 14, text=f"{x_min:.0f} Hz", anchor="w", fill="#555555", font=("TkDefaultFont", 9))
        canvas.create_text(margin_l + plot_w, margin_t + plot_h + 14, text=f"{x_max:.0f} Hz", anchor="e", fill="#555555", font=("TkDefaultFont", 9))

        self.fft_info_label.config(text=payload["info"])
        self.fft_tone_label.config(text=payload["tone"])

    def update_fft_plot(
        self,
        iq_samples,
        sample_rate,
        fft_sample_rate,
        modulation,
        expected_tones,
        symbol_rate,
        gfsk_bw_hz,
        strict_mode,
        top_n,
        tolerance_override_hz,
    ):
        canvas = self.fft_canvas
        canvas.delete("all")
        width = int(canvas.winfo_width())
        height = int(canvas.winfo_height())
        if width < 50:
            width = int(canvas.cget("width"))
        if height < 50:
            height = int(canvas.cget("height"))

        margin_l = 58
        margin_r = 18
        plot_w = max(1, width - margin_l - margin_r)

        self._fft_job_id += 1
        current_job_id = self._fft_job_id
        self._set_fft_busy(True, "Computing FFT...")
        canvas.create_text(width // 2, height // 2, text="Computing FFT...", fill="#666666", font=("TkDefaultFont", 11))
        self.fft_info_label.config(text="FFT processing in worker thread...")
        self.fft_tone_label.config(text="")

        inst_freq_snapshot = list(self.last_inst_freq)

        def worker():
            try:
                window_type = self.window_type_var.get()
                fft_segment_size = int(self.fft_size_var.get())
                payload = self._compute_fft_payload(
                    iq_samples,
                    sample_rate,
                    fft_sample_rate,
                    modulation,
                    expected_tones,
                    symbol_rate,
                    gfsk_bw_hz,
                    strict_mode,
                    top_n,
                    tolerance_override_hz,
                    inst_freq_snapshot,
                    plot_w,
                    window_type,
                    fft_segment_size,
                )
            except Exception as exc:
                payload = {
                    "kind": "error",
                    "info": f"FFT failed: {exc}",
                    "tone": "",
                    "canvas": "FFT computation failed",
                }
            self.root.after(0, lambda: self._on_fft_payload_ready(current_job_id, payload))

        threading.Thread(target=worker, daemon=True).start()

    def compare_to_expected(self, decoded_bits):
        expected_bits = []
        field_results = []
        offset = 0
        for field in self.expected_fields:
            desc = field["desc"]
            field_bits = field["bin"]
            encoding = field.get("encoding", "None")
            field_bits = encode_field_bits(field_bits, encoding)
            expected_bits.append(field_bits)
            length = len(field_bits)
            decoded_segment = decoded_bits[offset:offset + length]
            match = decoded_segment == field_bits
            if not match:
                if len(decoded_segment) < len(field_bits):
                    reason = "Too few decoded bits for this field."
                else:
                    reason = "Decoded bits differ from expected field bits."
            else:
                reason = ""
            field_results.append({
                "index": len(field_results) + 1,
                "desc": desc,
                "expected_hex": _bits_to_hex(field_bits),
                "decoded_hex": _bits_to_hex(decoded_segment),
                "decoded_value": _bits_to_hex(decoded_segment),
                "expected_len": length,
                "decoded_len": len(decoded_segment),
                "status": "PASS" if match else "FAIL",
                "reason": reason,
                "match": match,
                "decoded_bits": decoded_segment,
                "expected_bits": field_bits,
            })
            offset += length

        return "".join(expected_bits), field_results

    def show_results(self, field_results):
        for row in self.result_table.get_children():
            self.result_table.delete(row)

        for field in field_results:
            self.result_table.insert(
                "",
                "end",
                values=(
                    field["index"],
                    field["desc"],
                    field["expected_hex"],
                    field["decoded_hex"],
                    field["decoded_value"],
                    field["expected_len"],
                    field["decoded_len"],
                    field["status"],
                    field["reason"],
                ),
                tags=("pass",) if field["status"] == "PASS" else ("fail",),
            )

        self.result_table.tag_configure("pass", background="#ddffdd")
        self.result_table.tag_configure("fail", background="#ffdddd")

    def _symbol_bits_per_symbol(self, modulation):
        if modulation in ("4FSK", "QPSK", "π/4-QPSK", "DQPSK", "OQPSK"):
            return 2
        return 1

    def _symbol_expected_freq(self, bits, modulation, expected_tones):
        if modulation in ("FSK", "GFSK") and len(expected_tones) >= 2 and bits in ("0", "1"):
            return float(expected_tones[0] if bits == "0" else expected_tones[1])
        if modulation == "4FSK" and len(expected_tones) >= 4 and len(bits) == 2:
            tone_to_bits = _get_4fsk_tone_to_dibit(self.fsk4_dibit_mapping)
            bits_to_tone_idx = {v: k for k, v in tone_to_bits.items()}
            idx = bits_to_tone_idx.get(bits)
            if idx is not None:
                return float(expected_tones[idx])
        return float("nan")

    def _symbol_freq_series(self, inst_freq, modulation, samples_per_bit):
        if not inst_freq:
            return []
        bits_per_symbol = self._symbol_bits_per_symbol(modulation)
        samples_per_symbol = max(1, int(round(samples_per_bit * bits_per_symbol)))
        out = []
        for start in range(0, len(inst_freq), samples_per_symbol):
            window = inst_freq[start:start + samples_per_symbol]
            if window:
                out.append(float(np.mean(np.asarray(window, dtype=np.float64))))
        return out

    def build_symbol_rows(self, expected_bits, decoded_bits, modulation, expected_tones, inst_freq, samples_per_bit):
        bits_per_symbol = self._symbol_bits_per_symbol(modulation)
        expected_symbols = [expected_bits[i:i + bits_per_symbol] for i in range(0, len(expected_bits), bits_per_symbol)]
        decoded_symbols = [decoded_bits[i:i + bits_per_symbol] for i in range(0, len(decoded_bits), bits_per_symbol)]
        decoded_freqs = self._symbol_freq_series(inst_freq, modulation, samples_per_bit)
        count = max(len(expected_symbols), len(decoded_symbols), len(decoded_freqs))
        rows = []
        for idx in range(count):
            exp_bits = expected_symbols[idx] if idx < len(expected_symbols) else ""
            dec_bits = decoded_symbols[idx] if idx < len(decoded_symbols) else ""
            exp_freq = self._symbol_expected_freq(exp_bits, modulation, expected_tones)
            dec_freq = decoded_freqs[idx] if idx < len(decoded_freqs) else float("nan")
            status = "PASS" if exp_bits and dec_bits and exp_bits == dec_bits else "FAIL"
            if not exp_bits and not dec_bits:
                status = ""
            rows.append(
                {
                    "symbol": idx + 1,
                    "expected_bits": exp_bits,
                    "decoded_bits": dec_bits,
                    "expected_freq": exp_freq,
                    "decoded_freq": dec_freq,
                    "status": status,
                }
            )
        return rows

    def show_symbol_results(self, symbol_rows):
        for row in self.symbol_table.get_children():
            self.symbol_table.delete(row)

        for row in symbol_rows:
            exp_freq_text = "" if not np.isfinite(row["expected_freq"]) else f"{row['expected_freq']:.1f}"
            dec_freq_text = "" if not np.isfinite(row["decoded_freq"]) else f"{row['decoded_freq']:.1f}"
            self.symbol_table.insert(
                "",
                "end",
                values=(
                    row["symbol"],
                    row["expected_bits"],
                    row["decoded_bits"],
                    exp_freq_text,
                    dec_freq_text,
                    row["status"],
                ),
                tags=("pass",) if row["status"] == "PASS" else (("fail",) if row["status"] == "FAIL" else ()),
            )

        self.symbol_table.tag_configure("pass", background="#ddffdd")
        self.symbol_table.tag_configure("fail", background="#ffdddd")

    def _nearest_tone_idx(self, freq, expected_tones):
        if not expected_tones or not np.isfinite(freq):
            return None
        return min(range(len(expected_tones)), key=lambda i: abs(float(freq) - float(expected_tones[i])))

    def update_symbol_spectrum_plot(self, inst_freq, modulation, sample_rate, symbol_rate, samples_per_bit, expected_tones):
        canvas = self.symbol_canvas
        canvas.delete("all")
        width = int(canvas.winfo_width())
        height = int(canvas.winfo_height())
        if width < 50:
            width = int(canvas.cget("width"))
        if height < 50:
            height = int(canvas.cget("height"))

        symbol_freqs = self._symbol_freq_series(inst_freq, modulation, samples_per_bit)
        if len(symbol_freqs) < 2:
            canvas.create_text(width // 2, height // 2, text="Not enough symbols for spectrum", fill="#666666", font=("TkDefaultFont", 11))
            self.symbol_info_label.config(text="Symbol spectrum unavailable: run validation with more samples.")
            self._render_tone_legend(self.symbol_legend_frame, [], {})
            return

        freq_arr = np.asarray(symbol_freqs, dtype=np.float64)
        bins = max(32, min(256, int(math.sqrt(freq_arr.size) * 8)))
        counts, edges = np.histogram(freq_arr, bins=bins)
        centers = 0.5 * (edges[:-1] + edges[1:])
        power_db = 10.0 * np.log10(counts.astype(np.float64) + 1e-12)

        margin_l = 58
        margin_r = 18
        margin_t = 16
        margin_b = 32
        plot_w = max(1, width - margin_l - margin_r)
        plot_h = max(1, height - margin_t - margin_b)

        y_min = float(np.min(power_db))
        y_max = float(np.max(power_db))
        if abs(y_max - y_min) < 1e-9:
            y_max = y_min + 1.0

        x_min = float(np.min(centers))
        x_max = float(np.max(centers))
        if expected_tones:
            tone_min = float(min(expected_tones))
            tone_max = float(max(expected_tones))
            tone_span = max(100.0, tone_max - tone_min)
            x_min = min(x_min, tone_min - 0.2 * tone_span)
            x_max = max(x_max, tone_max + 0.2 * tone_span)
        if x_max <= x_min:
            x_max = x_min + 1.0

        payload = {
            "centers": centers,
            "power_db": power_db,
            "x_min": x_min,
            "x_max": x_max,
            "y_min": y_min,
            "y_max": y_max,
            "expected_tones": list(expected_tones),
            "sample_rate": float(sample_rate),
            "symbol_rate": float(symbol_rate),
            "symbol_count": len(symbol_freqs),
        }
        self._symbol_last_payload = payload
        self._render_symbol_spectrum_plot(payload)

    def _render_symbol_spectrum_plot(self, payload):
        canvas = self.symbol_canvas
        canvas.delete("all")
        width = int(canvas.winfo_width())
        height = int(canvas.winfo_height())
        if width < 50:
            width = int(canvas.cget("width"))
        if height < 50:
            height = int(canvas.cget("height"))

        margin_l = 58
        margin_r = 18
        margin_t = 16
        margin_b = 32
        plot_w = max(1, width - margin_l - margin_r)
        plot_h = max(1, height - margin_t - margin_b)

        centers = payload["centers"]
        power_db = payload["power_db"]
        expected_tones = payload["expected_tones"]
        y_min = float(payload["y_min"])
        y_max = float(payload["y_max"])
        x_min = float(payload["x_min"])
        x_max = float(payload["x_max"])

        x_min, x_max = self._apply_center_zoom(x_min, x_max, self._symbol_zoom, self._symbol_zoom_center_hz)

        points = []
        for i in range(power_db.size):
            x = margin_l + ((centers[i] - x_min) / (x_max - x_min)) * plot_w
            y = margin_t + (1.0 - (power_db[i] - y_min) / (y_max - y_min)) * plot_h
            points.extend((x, y))

        canvas.create_rectangle(margin_l, margin_t, margin_l + plot_w, margin_t + plot_h, outline="#bbbbbb")
        canvas.create_line(margin_l, margin_t + plot_h, margin_l + plot_w, margin_t + plot_h, fill="#999999")
        canvas.create_line(margin_l, margin_t, margin_l, margin_t + plot_h, fill="#999999")
        if len(points) >= 4:
            canvas.create_line(points, fill="#9467bd", width=2, smooth=True)

        tone_colors = self._get_tone_color_map(expected_tones)
        for idx, tone in enumerate(expected_tones):
            x = margin_l + ((tone - x_min) / (x_max - x_min)) * plot_w
            if margin_l <= x <= margin_l + plot_w:
                color = tone_colors.get(idx, "#2ca02c")
                canvas.create_line(x, margin_t, x, margin_t + plot_h, fill=color, dash=(3, 3), width=1)
                canvas.create_text(x + 3, margin_t + 10 + 12 * (idx % 2), text=f"T{idx}", anchor="w", fill=color, font=("TkDefaultFont", 9))

        canvas.create_text(margin_l - 8, margin_t, text=f"{y_max:.1f} dB", anchor="e", fill="#555555", font=("TkDefaultFont", 9))
        canvas.create_text(margin_l - 8, margin_t + plot_h, text=f"{y_min:.1f} dB", anchor="e", fill="#555555", font=("TkDefaultFont", 9))
        canvas.create_text(margin_l, margin_t + plot_h + 14, text=f"{x_min:.0f} Hz", anchor="w", fill="#555555", font=("TkDefaultFont", 9))
        canvas.create_text(margin_l + plot_w, margin_t + plot_h + 14, text=f"{x_max:.0f} Hz", anchor="e", fill="#555555", font=("TkDefaultFont", 9))

        peak_idx = int(np.argmax(power_db))
        self.symbol_info_label.config(
            text=(
                f"Symbol spectrum: {payload['symbol_count']} symbols, peak {centers[peak_idx]:.1f} Hz at {power_db[peak_idx]:.1f} dB, "
                f"Fs={payload['sample_rate']:.0f} Hz, Rs={payload['symbol_rate']:.1f}"
            )
        )
        self._render_tone_legend(self.symbol_legend_frame, expected_tones, tone_colors)

    def update_symbol_timing_plot(self, symbol_rows, modulation, expected_tones):
        self._timing_last_data = (list(symbol_rows), modulation, list(expected_tones))
        canvas = self.symbol_timing_canvas
        canvas.delete("all")
        width = int(canvas.winfo_width())
        height = int(canvas.winfo_height())
        if width < 50:
            width = int(canvas.cget("width"))
        if height < 50:
            height = int(canvas.cget("height"))

        usable_rows = [r for r in symbol_rows if r["decoded_bits"] or r["expected_bits"]]
        if not usable_rows:
            canvas.create_text(width // 2, height // 2, text="No symbol timing data", fill="#666666", font=("TkDefaultFont", 11))
            self.symbol_timing_info_label.config(text="Symbol timing unavailable: run validation first.")
            self._render_tone_legend(self.symbol_timing_legend_frame, [], {})
            canvas.configure(scrollregion=(0, 0, width, height))
            return

        rows = usable_rows

        margin_l = 58
        margin_r = 18
        margin_t = 14
        margin_b = 28
        visible_w = max(1, width - margin_l - margin_r)
        base_symbol_px = 20
        plot_w = max(visible_w, int(len(rows) * base_symbol_px * self._timing_zoom))
        plot_h = max(1, height - margin_t - margin_b)

        bits_top_h = int(plot_h * 0.34)
        wave_h = plot_h - bits_top_h - 10
        bits_y0 = margin_t
        bits_y1 = bits_y0 + bits_top_h
        wave_y0 = bits_y1 + 10
        wave_y1 = wave_y0 + wave_h
        wave_mid = 0.5 * (wave_y0 + wave_y1)

        symbol_count = len(rows)
        symbol_w = plot_w / max(1, symbol_count)

        bits_per_symbol = self._symbol_bits_per_symbol(modulation)
        bit_tokens = []
        for row in rows:
            bits = row["decoded_bits"] or row["expected_bits"]
            if not bits:
                bits = "?" * bits_per_symbol
            if len(bits) < bits_per_symbol:
                bits = bits.ljust(bits_per_symbol, "?")
            bit_tokens.extend(list(bits[:bits_per_symbol]))

        bit_count = len(bit_tokens)
        bit_w = plot_w / max(1, bit_count)

        canvas.configure(scrollregion=(0, 0, margin_l + plot_w + margin_r, height))

        canvas.create_rectangle(margin_l, bits_y0, margin_l + plot_w, bits_y1, outline="#bbbbbb")
        canvas.create_rectangle(margin_l, wave_y0, margin_l + plot_w, wave_y1, outline="#bbbbbb")

        bit_high = bits_y0 + int(bits_top_h * 0.28)
        bit_low = bits_y0 + int(bits_top_h * 0.72)
        canvas.create_line(margin_l, bit_low, margin_l + plot_w, bit_low, fill="#999999", dash=(2, 3))

        if bit_tokens:
            x0 = margin_l
            first = bit_tokens[0]
            y = bit_high if first == "1" else bit_low
            canvas.create_line(x0, y, x0 + bit_w, y, fill="#222222", width=2)
            for i in range(1, bit_count):
                prev = bit_tokens[i - 1]
                cur = bit_tokens[i]
                x = margin_l + i * bit_w
                y_prev = bit_high if prev == "1" else bit_low
                y_cur = bit_high if cur == "1" else bit_low
                if y_prev != y_cur:
                    canvas.create_line(x, y_prev, x, y_cur, fill="#222222", width=2)
                canvas.create_line(x, y_cur, x + bit_w, y_cur, fill="#222222", width=2)
            for i, bit in enumerate(bit_tokens):
                cx = margin_l + (i + 0.5) * bit_w
                canvas.create_text(cx, bits_y0 + 10, text=bit, fill="#333333", font=("TkDefaultFont", 9))

        for i in range(symbol_count + 1):
            x = margin_l + i * symbol_w
            canvas.create_line(x, bits_y0, x, wave_y1, fill="#e0e0e0")

        tone_colors = self._get_tone_color_map(expected_tones)
        max_abs_tone = max((abs(float(t)) for t in expected_tones), default=1.0)
        max_abs_tone = max(max_abs_tone, 1.0)
        amp = max(8.0, 0.35 * wave_h)

        phase = 0.0
        pts_per_symbol = 30
        for i, row in enumerate(rows):
            x_start = margin_l + i * symbol_w
            x_end = x_start + symbol_w
            if modulation == "BPSK":
                # Render BPSK as constant-frequency carrier with 180 deg phase flips.
                bits = row["decoded_bits"] or row["expected_bits"] or "0"
                bit = bits[0] if bits else "0"
                symbol_phase = 0.0 if bit == "0" else math.pi
                cycles = 3.0
                color = "#555555"
            else:
                freq = row["decoded_freq"] if np.isfinite(row["decoded_freq"]) else row["expected_freq"]
                if not np.isfinite(freq):
                    freq = 0.0
                norm = min(1.0, abs(float(freq)) / max_abs_tone)
                cycles = 1.5 + 4.5 * norm
                tone_idx = self._nearest_tone_idx(freq, expected_tones)
                if tone_idx is None:
                    color = "#777777"
                else:
                    color = tone_colors.get(tone_idx, "#777777")

            pts = []
            for k in range(pts_per_symbol + 1):
                frac = k / float(pts_per_symbol)
                x = x_start + frac * (x_end - x_start)
                if modulation == "BPSK":
                    y = wave_mid - amp * math.sin(phase + symbol_phase + 2.0 * math.pi * cycles * frac)
                else:
                    y = wave_mid - amp * math.sin(phase + 2.0 * math.pi * cycles * frac)
                pts.extend((x, y))
            canvas.create_line(pts, fill=color, width=2, smooth=True)
            phase = phase + 2.0 * math.pi * cycles

        canvas.create_text(margin_l - 8, 0.5 * (bit_high + bit_low), text="bits", anchor="e", fill="#555555", font=("TkDefaultFont", 9))
        canvas.create_text(margin_l - 8, wave_mid, text=modulation, anchor="e", fill="#555555", font=("TkDefaultFont", 9))
        canvas.create_text(margin_l, wave_y1 + 14, text="Time", anchor="w", fill="#555555", font=("TkDefaultFont", 9))
        canvas.create_line(margin_l, wave_y1 + 8, margin_l + plot_w, wave_y1 + 8, fill="#555555", arrow=tk.LAST)

        self.symbol_timing_info_label.config(
            text=f"Symbol timing view: showing {len(rows)} symbols ({bits_per_symbol} bit/symbol for {modulation}). Zoom={self._timing_zoom:.2f}x"
        )
        self._render_tone_legend(self.symbol_timing_legend_frame, expected_tones, tone_colors)

    def show_decoded_bits(self):
        if not self.decoded_bits:
            messagebox.showinfo("No decoded bits", "Run validation first to decode bits.")
            return
        top = tk.Toplevel(self.root)
        top.title("Decoded Bit Stream")
        text = tk.Text(top, wrap="none", font=("Courier", 10), width=100, height=20)
        text.insert("1.0", self.decoded_bits)
        text.config(state="disabled")
        text.pack(fill=tk.BOTH, expand=True)

    def hex_to_bin(self, hex_text):
        hex_value = hex_text.replace(" ", "")
        if hex_value.lower().startswith("0x"):
            hex_value = hex_value[2:]
        if len(hex_value) == 0:
            raise ValueError("Hex value must not be empty.")
        integer = int(hex_value, 16)
        bit_width = len(hex_value) * 4
        return f"{integer:0{bit_width}b}"

    def bin_to_hex(self, bin_text):
        bin_value = bin_text.replace(" ", "")
        if len(bin_value) == 0:
            raise ValueError("Binary value must not be empty.")
        if any(ch not in "01" for ch in bin_value):
            raise ValueError("Binary field must contain only 0 and 1.")
        pad = (-len(bin_value)) % 4
        if pad:
            bin_value = "0" * pad + bin_value
        integer = int(bin_value, 2)
        hex_width = len(bin_value) // 4
        return f"{integer:0{hex_width}X}"


def main(argv=None):
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--batch", action="store_true", help="Run headless IQ validation.")
    parser.add_argument("--iq-file", help="Path to IQ file (.bin/.raw).")
    parser.add_argument("--expected-json", help="Path to expected field JSON.")
    parser.add_argument("--modulation", choices=list(SUPPORTED_MODULATIONS))
    parser.add_argument("--sample-rate", type=float)
    parser.add_argument("--symbol-rate", type=float)
    parser.add_argument("--samples-per-bit", type=int)
    parser.add_argument("--tone0", type=float)
    parser.add_argument("--tone1", type=float)
    parser.add_argument("--tone2", type=float)
    parser.add_argument("--tone3", type=float)
    parser.add_argument("--gfsk-bw-hz", type=float)
    args = parser.parse_args(argv)

    if args.batch:
        if not args.iq_file or not args.expected_json:
            parser.error("--batch requires --iq-file and --expected-json")
        overrides = {
            "modulation": args.modulation,
            "sample_rate": args.sample_rate,
            "symbol_rate": args.symbol_rate,
            "samples_per_bit": args.samples_per_bit,
            "tone0": args.tone0,
            "tone1": args.tone1,
            "tone2": args.tone2,
            "tone3": args.tone3,
            "gfsk_bw_hz": args.gfsk_bw_hz,
        }
        summary = run_batch_validate(args.iq_file, args.expected_json, overrides)
        status = "PASS" if summary["pass"] else "FAIL"
        print(
            "{status}: matched {matched_fields}/{field_count} fields, decoded_bits={decoded_bits_len}, expected_bits={expected_bits_len}".format(
                status=status,
                **summary,
            )
        )
        return

    root = tk.Tk()
    configure_window(root)
    app = DmrTelemetryTesterApp(root)
    root.mainloop()


if __name__ == "__main__":
    main(sys.argv[1:])
