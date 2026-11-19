#!/usr/bin/env python3
"""Simple FSK encoder UI for hex/binary fields."""

import argparse
import json
import math
import os
import struct
import sys
import threading
import wave
import tkinter as tk
import tkinter.font as tkfont
import tkinter.ttk as ttk
from tkinter import filedialog, messagebox

from dpmg.modulation.modulator import get_modulator
from dpmg.encoding.ecc_decoder import encode_field_bits


DPMG_BIN_MAGIC = b"DPMG"
DPMG_BIN_VERSION = 1
DPMG_BIN_FORMAT_BE_IQ16 = 1
DPMG_BIN_HEADER_SIZE = 16


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


def _hex_to_bin(hex_text):
    hex_value = hex_text.replace(" ", "")
    if hex_value.lower().startswith("0x"):
        hex_value = hex_value[2:]
    if len(hex_value) == 0:
        raise ValueError("Hex value must not be empty.")
    integer = int(hex_value, 16)
    bit_width = len(hex_value) * 4
    return f"{integer:0{bit_width}b}"


def _bin_to_hex(bin_text):
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


def _load_fields_json_for_batch(path):
    with open(path, "r", encoding="utf-8") as in_file:
        loaded = json.load(in_file)

    settings = {}
    fields_raw = loaded
    if isinstance(loaded, dict):
        settings = loaded.get("settings", {}) if isinstance(loaded.get("settings", {}), dict) else {}
        fields_raw = loaded.get("fields", [])

    if not isinstance(fields_raw, list):
        raise ValueError("Input JSON must contain a 'fields' array or be an array of fields.")

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
                bin_value = _hex_to_bin(hex_value)
            elif bin_value and not hex_value:
                hex_value = _bin_to_hex(bin_value)
            else:
                expected_bin = _hex_to_bin(hex_value)
                if expected_bin != bin_value:
                    raise ValueError()
        except ValueError:
            continue
        parsed_fields.append(
            {
                "hex": hex_value,
                "bin": bin_value,
                "desc": desc_value,
                "encoding": item.get("encoding", "None"),
            }
        )

    if not parsed_fields:
        raise ValueError("No valid fields found in input JSON.")

    return settings, parsed_fields


def _build_encoded_bit_string(fields):
    chunks = []
    for field in fields:
        field_bits = field["bin"]
        encoding = field.get("encoding", "None")
        chunks.append(encode_field_bits(field_bits, encoding))
    return "".join(chunks)


def _build_lowpass_coeffs(num_taps, cutoff_freq, sample_rate):
    center = (num_taps - 1) // 2
    coeffs = []
    for n in range(num_taps):
        t = n - center
        if t == 0:
            value = 2.0 * cutoff_freq / sample_rate
        else:
            value = math.sin(2.0 * math.pi * cutoff_freq * t / sample_rate) / (math.pi * t)
        window = 0.54 - 0.46 * math.cos(2.0 * math.pi * n / (num_taps - 1))
        coeffs.append(value * window)
    total = sum(coeffs)
    return [c / total for c in coeffs]


def _build_rrc_coeffs(num_taps, rolloff, sample_rate, symbol_rate):
    center = (num_taps - 1) // 2
    T = 1.0 / symbol_rate
    dt = 1.0 / sample_rate
    coeffs = []

    for n in range(num_taps):
        t = (n - center) * dt
        if abs(t) < 1e-12:
            value = (1.0 + rolloff * (4.0 / math.pi - 1.0)) / math.sqrt(T)
        elif rolloff != 0.0 and abs(abs(t) - T / (4.0 * rolloff)) < 1e-12:
            numerator = rolloff / math.sqrt(2.0 * T)
            value = numerator * (
                (1.0 + 2.0 / math.pi) * math.sin(math.pi / (4.0 * rolloff))
                + (1.0 - 2.0 / math.pi) * math.cos(math.pi / (4.0 * rolloff))
            )
        else:
            numerator = (
                math.sin(math.pi * t * (1.0 - rolloff) / T)
                + 4.0 * rolloff * t / T * math.cos(math.pi * t * (1.0 + rolloff) / T)
            )
            denominator = math.pi * t * (1.0 - (4.0 * rolloff * t / T) ** 2) / T
            value = numerator / denominator
        coeffs.append(value)

    energy = sum(c * c for c in coeffs)
    norm = math.sqrt(energy)
    return [c / norm for c in coeffs]


def _build_filter_coeffs(filter_type, num_taps, sample_rate, symbol_rate):
    if filter_type == "Lowpass":
        cutoff = min(sample_rate * 0.45, symbol_rate * 3.0)
        return _build_lowpass_coeffs(num_taps, cutoff, sample_rate)
    if filter_type == "RRC":
        return _build_rrc_coeffs(num_taps, 0.35, sample_rate, symbol_rate)
    raise ValueError(f"Unknown filter type: {filter_type}")


def _apply_filter_real(samples, coeffs):
    n = len(coeffs)
    padded = [0.0] * (n - 1) + list(samples)
    out = []
    for i in range(len(samples)):
        out.append(sum(coeffs[j] * padded[i + n - 1 - j] for j in range(n)))
    return out


def _resolve_4fsk_tones_domain(tone0, tone1, tone2, tone3, center_freq_hz, tone_domain="auto"):
    """Resolve 4FSK tone domain and return baseband tones.

    tone_domain:
    - "absolute_rf": tones are absolute RF and must be shifted by center
    - "baseband": tones are already baseband offsets
    - "auto": infer from magnitudes relative to center
    """
    tones = [float(tone0), float(tone1), float(tone2), float(tone3)]
    center_freq_hz = float(center_freq_hz)
    domain = str(tone_domain or "auto").strip().lower()

    if domain not in ("absolute_rf", "baseband", "auto"):
        raise ValueError(f"Unknown tone_domain: {tone_domain}")

    if domain == "auto":
        # Heuristic: treat as RF if all tones are large positive values clustered around center.
        if center_freq_hz > 0 and all(t > 0 for t in tones):
            near_center = max(abs(t - center_freq_hz) for t in tones) <= 5_000_000.0
            large_freqs = max(abs(t) for t in tones) >= 1_000_000.0
            domain = "absolute_rf" if (near_center and large_freqs) else "baseband"
        else:
            domain = "baseband"

    if domain == "absolute_rf":
        if center_freq_hz <= 0:
            raise ValueError("center_freq_hz must be positive when tone_domain is absolute_rf")
        tones = [t - center_freq_hz for t in tones]

    return tones, domain


def _normalize_4fsk_dibit_mapping(value, default="dmr"):
    key = str(value or default).strip().lower().replace("-", "_")
    if key not in ("binary", "gray", "dmr", "dmr_inverted"):
        raise ValueError(f"Unknown 4FSK dibit mapping: {value}")
    return key


def _encode_fsk_batch(bit_string, settings):
    modulation = settings.get("modulation", "FSK")
    if modulation == "2FSK":
        modulation = "FSK"
    symbol_rate = float(settings.get("symbol_rate", settings.get("baud", 9600.0)))
    samples_per_bit = int(settings.get("samples_per_bit", 37))
    tone0 = float(settings.get("tone0", 1200.0))
    tone1 = float(settings.get("tone1", 2200.0))
    tone2 = float(settings.get("tone2", 1400.0))
    tone3 = float(settings.get("tone3", 2400.0))
    gfsk_bw_hz = float(settings.get("gfsk_bw_hz", 250.0))
    # If gfsk_bt is present, it overrides gfsk_bw_hz
    gfsk_bt = settings.get("gfsk_bt")
    if gfsk_bt is not None:
        try:
            symbol_rate_for_bt = float(settings.get("symbol_rate", settings.get("baud", 9600.0)))
            gfsk_bw_hz = float(gfsk_bt) * symbol_rate_for_bt
        except (ValueError, TypeError):
            pass
    filter_type = str(settings.get("filter", "None"))
    filter_taps = int(settings.get("filter_taps", 101))
    output_sample_rate = settings.get("output_sample_rate")
    if output_sample_rate is not None and str(output_sample_rate).strip() != "":
        output_sample_rate = int(round(float(output_sample_rate)))
        if output_sample_rate == 0:
            output_sample_rate = None
    else:
        output_sample_rate = None
    dibit_mapping = _normalize_4fsk_dibit_mapping(settings.get("dibit_mapping", "dmr"))
    tone_domain = str(settings.get("tone_domain", "auto"))
    center_freq_hz = float(settings.get("center_freq_hz", 0.0))
    if modulation == "4FSK":
        (tone0, tone1, tone2, tone3), _resolved_domain = _resolve_4fsk_tones_domain(
            tone0, tone1, tone2, tone3, center_freq_hz, tone_domain
        )

    if symbol_rate <= 0 or samples_per_bit <= 0:
        raise ValueError("symbol_rate and samples_per_bit must be positive.")
    if modulation in ("FSK", "GFSK") and (tone0 <= 0 or tone1 <= 0):
        raise ValueError("tone0 and tone1 must be positive for FSK/GFSK.")
    if modulation == "GFSK" and gfsk_bw_hz <= 0:
        raise ValueError("GFSK bandwidth must be positive.")
    if modulation == "4FSK" and any(t == 0 for t in (tone0, tone1, tone2, tone3)):
        raise ValueError("4FSK tones must be non-zero.")

    if modulation in ("4FSK", "QPSK", "π/4-QPSK", "DQPSK", "OQPSK") and (len(bit_string) % 2 == 1):
        bit_string += "0"

    mod = get_modulator(
        modulation,
        symbol_rate=symbol_rate,
        samples_per_bit=samples_per_bit,
        tone0=tone0,
        tone1=tone1,
        tone2=tone2,
        tone3=tone3,
        dibit_mapping=dibit_mapping,
        gfsk_bw_hz=gfsk_bw_hz,
        amplitude=0.8,
        spread_factor=int(settings.get("spread_factor", 15)),
        pn_seed=int(settings.get("pn_seed", 1)),
        code_spec=settings.get("code_spec"),
        dsss_modem=str(settings.get("dsss_modem", "bpsk")),
        css_sf=int(settings.get("css_sf", 7)),
        css_bw=float(settings.get("css_bw", 125000.0)),
        css_os=int(settings.get("css_os", 1)),
    )
    result = mod.modulate(bit_string)
    sample_rate = result.sample_rate
    waveform_data = [(float(c.real), float(c.imag)) for c in result.iq_samples]

    if filter_type != "None":
        if modulation == "4FSK":
            # DMR-style 4FSK is sensitive to post-IQ filtering; keep symbols unfiltered.
            filter_type = "None"
    if filter_type != "None":
        if filter_taps <= 0 or filter_taps % 2 == 0:
            raise ValueError("filter_taps must be an odd positive integer.")
        coeffs = _build_filter_coeffs(filter_type, filter_taps, sample_rate, symbol_rate)
        i_ch = _apply_filter_real([s[0] for s in waveform_data], coeffs)
        q_ch = _apply_filter_real([s[1] for s in waveform_data], coeffs)
        waveform_data = list(zip(i_ch, q_ch))
    if output_sample_rate is not None:
        waveform_data, sample_rate = _resample_waveform_linear(
            waveform_data, sample_rate, output_sample_rate)

    return waveform_data, sample_rate

def _resample_waveform_linear(waveform_data, input_sample_rate, output_sample_rate):
    """Resample IQ points with linear interpolation, preserving time duration."""
    in_sr = int(round(float(input_sample_rate)))
    out_sr = int(round(float(output_sample_rate)))
    if in_sr <= 0 or out_sr <= 0:
        raise ValueError("sample rates must be positive")
    if out_sr == in_sr or not waveform_data:
        return waveform_data, in_sr
    ratio = out_sr / in_sr
    output_len = max(1, int(round(len(waveform_data) * ratio)))
    last_idx = len(waveform_data) - 1
    out = []
    for k in range(output_len):
        src_pos = k / ratio
        left = int(src_pos)
        if left >= last_idx:
            out.append(waveform_data[last_idx])
            continue
        frac = src_pos - left
        i0, q0 = waveform_data[left]
        i1, q1 = waveform_data[left + 1]
        out.append((i0 + (i1 - i0) * frac, q0 + (q1 - q0) * frac))
    return out, out_sr

def _save_waveform(path, waveform_data, sample_rate):
    path_lower = path.lower()

    if path_lower.endswith(".wav"):
        packed = bytearray()
        for i_samp, _q in waveform_data:
            v = int(round(i_samp * 32767.0))
            packed += struct.pack("<h", max(-32768, min(32767, v)))
        with wave.open(path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(bytes(packed))
        return "wav"

    if path_lower.endswith(".bin"):
        packed = bytearray()
        for i_samp, q_samp in waveform_data:
            iv = int(round(i_samp * 32767.0))
            qv = int(round(q_samp * 32767.0))
            packed += struct.pack(">h", max(-32768, min(32767, iv)))
            packed += struct.pack(">h", max(-32768, min(32767, qv)))
        header = struct.pack(
            ">4sBBHII",
            DPMG_BIN_MAGIC,
            DPMG_BIN_VERSION,
            DPMG_BIN_FORMAT_BE_IQ16,
            0,
            int(sample_rate),
            DPMG_BIN_HEADER_SIZE,
        )
        with open(path, "wb") as out_file:
            out_file.write(header)
            out_file.write(packed)
        return "bin"

    if path_lower.endswith((".x40iq", ".x40qi")):
        # X40 (Sidekiq): each IQ/QI pair is a single little-endian uint32.
        # 14-bit signed, sign-extended to 16 bits, right-justified.
        qi = path_lower.endswith(".x40qi")
        packed = bytearray()
        for i_samp, q_samp in waveform_data:
            iv = max(-8192, min(8191, int(round(i_samp * 8191.0)))) & 0xFFFF
            qv = max(-8192, min(8191, int(round(q_samp * 8191.0)))) & 0xFFFF
            word = (qv << 16) | iv if qi else (iv << 16) | qv
            packed += struct.pack("<I", word)
        with open(path, "wb") as out_file:
            out_file.write(packed)
        return "x40-qi-14bit-u32" if qi else "x40-iq-14bit-u32"

    if path_lower.endswith((".g40iq", ".g40qi")):
        # G40: 16-bit signed samples, two per uint32 word, little-endian.
        qi = path_lower.endswith(".g40qi")
        packed = bytearray()
        for i_samp, q_samp in waveform_data:
            iv = max(-32768, min(32767, int(round(i_samp * 32767.0)))) & 0xFFFF
            qv = max(-32768, min(32767, int(round(q_samp * 32767.0)))) & 0xFFFF
            word = (qv << 16) | iv if qi else (iv << 16) | qv
            packed += struct.pack("<I", word)
        with open(path, "wb") as out_file:
            out_file.write(packed)
        return "g40-qi-16bit-u32" if qi else "g40-iq-16bit-u32"

    # raw uint16 fallback: catch-all, must be last
    packed = bytearray()
    for i_samp, _q in waveform_data:
        v = int(round((i_samp + 1.0) * 0.5 * 65535.0))
        packed += struct.pack("<H", max(0, min(65535, v)))
    with open(path, "wb") as out_file:
        out_file.write(packed)
    return "raw"
    
    


def run_batch_encode(fields_json_path, output_path, overrides=None):
    settings, fields = _load_fields_json_for_batch(fields_json_path)
    if overrides:
        settings.update({k: v for k, v in overrides.items() if v is not None})

    bit_string = _build_encoded_bit_string(fields)
    waveform_data, sample_rate = _encode_fsk_batch(bit_string, settings)
    fmt = _save_waveform(output_path, waveform_data, sample_rate)

    return {
        "output_path": output_path,
        "format": fmt,
        "sample_rate": sample_rate,
        "bit_count": len(bit_string),
        "field_count": len(fields),
    }


class FskEncoderApp:
    def __init__(self, root, parent=None):
        self.root = root
        if parent is None:
            self.root.title("Data Packet Message Generator")
        self.parent = parent if parent is not None else root
        self.fields = []
        self.encode_worker = None
        self.encode_busy = False

        self.create_widgets()

    def create_widgets(self):
        self.base_font = tkfont.Font(size=13)
        self.button_font = tkfont.Font(size=13, weight="bold")
        style = ttk.Style(self.root)
        ui_scale = float(self.root.tk.call("tk", "scaling"))
        tree_rowheight = max(
            int(32 * ui_scale),
            self.base_font.metrics("linespace") + int(14 * ui_scale),
        )
        style.configure("Custom.Treeview", rowheight=tree_rowheight, font=self.base_font)
        style.configure("Custom.Treeview.Heading", font=self.button_font)

        frame = tk.Frame(self.parent, padx=16, pady=16)
        frame.pack(fill=tk.BOTH, expand=True)
        for c in range(5):
            frame.grid_columnconfigure(c, weight=1)
        frame.grid_rowconfigure(1, weight=1)

        input_frame = tk.LabelFrame(frame, text="Field entry", font=self.button_font, padx=12, pady=12)
        input_frame.grid(row=0, column=0, columnspan=5, sticky="ew")
        for c in range(5):
            input_frame.grid_columnconfigure(c, weight=1)

        tk.Label(input_frame, text="Value:", font=self.base_font).grid(row=0, column=0, sticky="w")
        self.value_entry = tk.Entry(input_frame, width=42, font=self.base_font)
        self.value_entry.grid(row=0, column=1, columnspan=2, sticky="ew")

        tk.Label(input_frame, text="Format:", font=self.base_font).grid(row=0, column=3, sticky="w", padx=(16, 0))
        self.value_format_var = tk.StringVar(value="Hex")
        self.value_format_entry = ttk.Combobox(
            input_frame,
            textvariable=self.value_format_var,
            values=["Hex", "Binary"],
            font=self.base_font,
            width=20,
            state="readonly",
        )
        self.value_format_entry.grid(row=0, column=4, sticky="ew", padx=(8, 0))

        tk.Label(input_frame, text="Description:", font=self.base_font).grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.desc_entry = ttk.Combobox(
            input_frame,
            values=["Preamble", "Sync", "Header", "Delimiter", "Payload", "Telemetry", "Control", "Other"],
            font=self.base_font,
            width=20,
            
        )
        style.configure('TCombobox', arrowsize=30)
        self.desc_entry.set("Payload")
        self.desc_entry.grid(row=1, column=1, columnspan=2, sticky="ew", pady=(8, 0))

        tk.Label(input_frame, text="Encoding:", font=self.base_font).grid(row=1, column=3, sticky="w", padx=(16, 0), pady=(8, 0))
        self.encoding_types = [
            "None",
            "Convolutional Rate 1/2",
            "Convolutional Rate 1/3",
            "Hamming(7,4)",
            "Hamming(13,9)",
            "Hamming(15,11)",
            "BPTC(196,96)",
            "Golay(20,8)",
            "Rate 3/4 Trellis",
            "CRC-CCITT",
            "CRC-9",
            "CRC-32",
            "DMR CACH (CRC-9 over 15b)",
            "DMR Slot Type (Golay(20,8))",
            "DMR Data Block (CRC-CCITT + BPTC196/96)",
        ]
        self.encoding_entry = ttk.Combobox(
            input_frame,
            values=self.encoding_types,
            font=self.base_font,
            width=20,
            state="readonly",
        )
        self.encoding_entry.set("None")
        self.encoding_entry.grid(row=1, column=4, sticky="ew", padx=(8, 0), pady=(8, 0))

        tk.Button(input_frame, text="Add Field", command=self.add_field, font=self.button_font, padx=12, pady=6).grid(row=2, column=0, pady=(14, 0), sticky="ew", padx=(0, 4))
        tk.Button(input_frame, text="Delete Selected", command=self.delete_selected_field, font=self.button_font, padx=12, pady=6).grid(row=2, column=1, pady=(14, 0), sticky="ew", padx=(2, 4))
        tk.Button(input_frame, text="Clear Fields", command=self.clear_fields, font=self.button_font, padx=12, pady=6).grid(row=2, column=2, pady=(14, 0), sticky="ew", padx=(2, 4))

        self.message_option_var = tk.StringVar(value="Message Options")
        message_menu = tk.OptionMenu(
            input_frame,
            self.message_option_var,
            "Save Message",
            "Load Message",
            "Load DMR Template",
            "Edit DMR Template",
            command=self.on_message_option_selected,
        )
        message_menu.config(font=self.button_font, width=20)
        message_menu.grid(row=2, column=3, columnspan=2, pady=(14, 0), sticky="ew", padx=(2, 0))

        fields_frame = tk.LabelFrame(frame, text="Fields", font=self.button_font, padx=12, pady=12)
        fields_frame.grid(row=1, column=0, columnspan=5, sticky="nsew", pady=(12, 0))
        fields_frame.grid_rowconfigure(0, weight=1)
        fields_frame.grid_columnconfigure(0, weight=1)

        self.field_table = ttk.Treeview(
            fields_frame,
            columns=("bit_range", "hex", "bin", "desc", "encoding"),
            show="headings",
            height=12,
            style="Custom.Treeview",
            selectmode="browse",
        )
        self.field_table.heading("bit_range", text="Bit Field")
        self.field_table.heading("hex", text="Value (Hex)")
        self.field_table.heading("bin", text="Value (Bin)")
        self.field_table.heading("desc", text="Description")
        self.field_table.heading("encoding", text="Encoding")
        self.field_table.column("bit_range", width=110, minwidth=70, anchor="center", stretch=True)
        self.field_table.column("hex", width=140, minwidth=80, anchor="center", stretch=True)
        self.field_table.column("bin", width=200, minwidth=100, anchor="center", stretch=True)
        self.field_table.column("desc", width=150, minwidth=90, anchor="w", stretch=True)
        self.field_table.column("encoding", width=140, minwidth=90, anchor="center", stretch=True)
        self.field_table.grid(row=0, column=0, sticky="nsew")
        self.field_table.bind("<Delete>", self.delete_selected_field)
        self.field_table.bind("<Configure>", lambda _event: self._refresh_tree_rowheight())

        v_scroll = tk.Scrollbar(fields_frame, orient="vertical", command=self.field_table.yview)
        self.field_table.configure(yscrollcommand=v_scroll.set)
        v_scroll.grid(row=0, column=1, sticky="ns")

        h_scroll = tk.Scrollbar(fields_frame, orient="horizontal", command=self.field_table.xview)
        self.field_table.configure(xscrollcommand=h_scroll.set)
        h_scroll.grid(row=1, column=0, sticky="ew", columnspan=2)

        options_frame = tk.Frame(frame, pady=12)
        options_frame.grid(row=2, column=0, columnspan=5, sticky="ew")
        for c in range(3):
            options_frame.grid_columnconfigure(c, weight=1)

        modem_frame = tk.LabelFrame(options_frame, text="Modulation", font=self.button_font, padx=12, pady=12)
        modem_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        modem_frame.grid_columnconfigure(1, weight=1)

        tk.Label(modem_frame, text="Modulation:", font=self.base_font).grid(row=0, column=0, sticky="w")
        self.modulation_var = tk.StringVar(value="FSK")
        modulation_menu = tk.OptionMenu(
            modem_frame, self.modulation_var,
            "FSK", "4FSK", "BPSK", "QPSK", "π/4-QPSK", "DQPSK", "OQPSK", "GFSK",
            "DSSS", "CSS"
        )
        modulation_menu.config(font=self.base_font)
        modulation_menu.grid(row=0, column=1, sticky="w")
        self.modulation_var.trace_add("write", self.on_modulation_change)

        tk.Label(modem_frame, text="Symbol rate:", font=self.base_font).grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.baud_entry = tk.Entry(modem_frame, width=12, font=self.base_font)
        self.baud_entry.insert(0, "9600")
        self.baud_entry.grid(row=1, column=1, sticky="w", pady=(8, 0))
        self.baud_entry.bind("<KeyRelease>", self._update_sample_rate)
        self.baud_entry.bind("<FocusOut>", self._update_sample_rate)

        tk.Label(modem_frame, text="MXG sample rate:", font=self.base_font).grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.sample_rate_label = tk.Label(modem_frame, text="--", font=self.base_font, anchor="w", relief="sunken", width=14, bg="white")
        self.sample_rate_label.grid(row=2, column=1, sticky="w", pady=(8, 0))
        self.auto_spb_button = tk.Button(
            modem_frame,
            text="Set SR",
            command=self.auto_set_samples_per_bit,
            font=self.button_font,
            padx=10,
            pady=4,
        )
        self.auto_spb_button.grid(row=2, column=2, sticky="w", padx=(8, 0), pady=(8, 0))

        self.encoding_hint = tk.Label(modem_frame, text="Encoding is selected per field.", font=self.base_font)
        self.encoding_hint.grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))

        tone_frame = tk.LabelFrame(options_frame, text="Tone settings", font=self.button_font, padx=12, pady=12)
        tone_frame.grid(row=0, column=1, sticky="nsew", padx=(8, 8))
        tone_frame.grid_columnconfigure(1, weight=1)
        tone_frame.grid_columnconfigure(3, weight=1)
        tone_frame.grid_columnconfigure(5, weight=1)

        self.tone0_label = tk.Label(tone_frame, text="Tone 0:", font=self.base_font)
        self.mark_entry = tk.Entry(tone_frame, width=12, font=self.base_font)
        self.mark_entry.insert(0, "0")
        self.tone0_unit_var = tk.StringVar(value="kHz")
        self.tone0_unit_entry = ttk.Combobox(
            tone_frame,
            textvariable=self.tone0_unit_var,
            values=["Hz", "kHz", "MHz"],
            font=self.base_font,
            width=8,
            state="readonly",
        )

        self.tone1_label = tk.Label(tone_frame, text="Tone 1:", font=self.base_font)
        self.space_entry = tk.Entry(tone_frame, width=12, font=self.base_font)
        self.space_entry.insert(0, "0")
        self.tone1_unit_var = tk.StringVar(value="kHz")
        self.tone1_unit_entry = ttk.Combobox(
            tone_frame,
            textvariable=self.tone1_unit_var,
            values=["Hz", "kHz", "MHz"],
            font=self.base_font,
            width=8,
            state="readonly",
        )

        self.samples_per_bit_label = tk.Label(tone_frame, text="Samples per bit:", font=self.base_font)
        self.samples_per_bit_entry = tk.Entry(tone_frame, width=12, font=self.base_font)
        self.samples_per_bit_entry.insert(0, "37")
        self.samples_per_bit_entry.bind("<KeyRelease>", self._update_sample_rate)
        self.samples_per_bit_entry.bind("<FocusOut>", self._update_sample_rate)

        self.center_label = tk.Label(tone_frame, text="Center freq:", font=self.base_font)
        self.center_entry = tk.Entry(tone_frame, width=12, font=self.base_font)
        self.center_entry.insert(0, "425025")
        self.center_unit_var = tk.StringVar(value="kHz")
        self.center_unit_entry = ttk.Combobox(
            tone_frame,
            textvariable=self.center_unit_var,
            values=["Hz", "kHz", "MHz"],
            font=self.base_font,
            width=8,
            state="readonly",
        )

        self.delta_label = tk.Label(tone_frame, text="4FSK deviation:", font=self.base_font)
        self.delta_entry = tk.Entry(tone_frame, width=12, font=self.base_font)
        self.delta_entry.insert(0, "0.648")
        self.delta_unit_var = tk.StringVar(value="kHz")
        self.delta_unit_entry = ttk.Combobox(
            tone_frame,
            textvariable=self.delta_unit_var,
            values=["Hz", "kHz", "MHz"],
            font=self.base_font,
            width=8,
            state="readonly",
        )

        self.generate_button = tk.Button(tone_frame, text="Generate 4FSK tones", command=self.generate_4fsk_tones, font=self.button_font, padx=12, pady=6)

        self.dibit_map_label = tk.Label(tone_frame, text="4FSK mapping:", font=self.base_font)
        self.dibit_map_var = tk.StringVar(value="dmr")
        self.dibit_map_entry = ttk.Combobox(
            tone_frame,
            textvariable=self.dibit_map_var,
            values=["dmr", "dmr_inverted", "binary", "gray"],
            font=self.base_font,
            width=12,
            state="readonly",
        )
        self.dibit_map_info_button = tk.Button(
            tone_frame,
            text="Info",
            command=self.show_4fsk_mapping_info,
            font=self.button_font,
            padx=8,
            pady=2,
        )

        self.tone2_label = tk.Label(tone_frame, text="Tone 2:", font=self.base_font)
        self.tone2_entry = tk.Entry(tone_frame, width=12, font=self.base_font)
        self.tone2_entry.insert(0, "0")
        self.tone2_unit_var = tk.StringVar(value="kHz")
        self.tone2_unit_entry = ttk.Combobox(
            tone_frame,
            textvariable=self.tone2_unit_var,
            values=["Hz", "kHz", "MHz"],
            font=self.base_font,
            width=8,
            state="readonly",
        )

        self.tone3_label = tk.Label(tone_frame, text="Tone 3:", font=self.base_font)
        self.tone3_entry = tk.Entry(tone_frame, width=12, font=self.base_font)
        self.tone3_entry.insert(0, "0")
        self.tone3_unit_var = tk.StringVar(value="kHz")
        self.tone3_unit_entry = ttk.Combobox(
            tone_frame,
            textvariable=self.tone3_unit_var,
            values=["Hz", "kHz", "MHz"],
            font=self.base_font,
            width=8,
            state="readonly",
        )

        # GFSK BT product controls (in place of raw gfsk_bw_hz)
        self.gfsk_bt_label = tk.Label(tone_frame, text="BT product:", font=self.base_font)
        self.gfsk_bt_entry = tk.Entry(tone_frame, width=8, font=self.base_font)
        self.gfsk_bt_entry.insert(0, "0.3")
        _BT_PRESETS = {
            "Custom": None,
            "Bluetooth (BT=0.3)": "0.3",
            "BLE (BT=0.5)": "0.5",
            "DECT (BT=0.5)": "0.5",
            "ZigBee / 802.15.4 (BT=0.5)": "0.5",
            "DMR GFSK (BT=0.5)": "0.5",
            "P25 C4FM (BT=0.4)": "0.4",
            "TETRA π/4-DQPSK (BT=0.35)": "0.35",
        }
        self._gfsk_bt_presets = _BT_PRESETS
        self.gfsk_bt_preset_var = tk.StringVar(value="Custom")
        self.gfsk_bt_preset_label = tk.Label(tone_frame, text="Preset:", font=self.base_font)
        self.gfsk_bt_preset_menu = tk.OptionMenu(
            tone_frame, self.gfsk_bt_preset_var, *list(_BT_PRESETS.keys()),
            command=self._on_gfsk_bt_preset
        )
        self.gfsk_bt_preset_menu.config(font=self.base_font)
        # Keep legacy entry for JSON backward compatibility (hidden; not shown in UI)
        self.gfsk_bw_entry = tk.Entry(tone_frame, width=12, font=self.base_font)
        self.gfsk_bw_entry.insert(0, "0")
        self.gfsk_bw_label = tk.Label(tone_frame, text="", font=self.base_font)  # hidden legacy

        filter_frame = tk.LabelFrame(options_frame, text="Filter settings", font=self.button_font, padx=12, pady=12)
        filter_frame.grid(row=0, column=2, sticky="nsew", padx=(8, 0))
        filter_frame.grid_columnconfigure(1, weight=1)

        tk.Label(filter_frame, text="Filter:", font=self.base_font).grid(row=0, column=0, sticky="w")
        self.filter_var = tk.StringVar(value="None")
        filter_menu = tk.OptionMenu(filter_frame, self.filter_var, "None", "Lowpass", "RRC")
        filter_menu.config(font=self.base_font)
        filter_menu.grid(row=0, column=1, sticky="w")

        tk.Label(filter_frame, text="Filter taps:", font=self.base_font).grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.filter_taps_entry = tk.Entry(filter_frame, width=12, font=self.base_font)
        self.filter_taps_entry.insert(0, "31")
        self.filter_taps_entry.grid(row=1, column=1, sticky="w", pady=(8, 0))
        self.calc_taps_button = tk.Button(
            filter_frame,
            text="Calc",
            command=self._calc_filter_taps,
            font=self.button_font,
            padx=8,
            pady=2,
        )
        self.calc_taps_button.grid(row=1, column=2, sticky="w", padx=(6, 0), pady=(8, 0))

        action_frame = tk.Frame(frame)
        action_frame.grid(row=3, column=0, columnspan=5, sticky="ew", pady=(16, 0))
        action_frame.grid_columnconfigure(0, weight=1)
        action_frame.grid_columnconfigure(1, weight=1)

        self.encode_button = tk.Button(
            action_frame,
            text="Save ",
            command=self.encode_and_save,
            font=self.button_font,
            padx=12,
            pady=6,
        )
        self.encode_button.grid(row=0, column=0, sticky="e")

        self.show_bits_button = tk.Button(
            action_frame,
            text="Show Bit String",
            command=self.show_bit_string,
            font=self.button_font,
            padx=12,
            pady=6,
        )
        self.show_bits_button.grid(row=0, column=1, sticky="w")

        self.encode_progress = ttk.Progressbar(action_frame, mode="indeterminate", length=260)
        self.encode_progress.grid(row=1, column=0, columnspan=2, pady=(10, 0))
        self.encode_progress.grid_remove()

        self.status_label = tk.Label(frame, text="Enter fields and choose modulation, symbol rate, samples per bit, and filter.", anchor="w", font=self.base_font)
        self.status_label.grid(row=4, column=0, columnspan=5, sticky="we", pady=(16, 0))

        self.update_modulation_fields()
        self._update_sample_rate()
        self.root.after(0, self._refresh_tree_rowheight)

    def _update_sample_rate(self, *_):
        try:
            symbol_rate = float(self.baud_entry.get())
            spb = float(self.samples_per_bit_entry.get())
            sr = symbol_rate * spb
            text = f"{int(sr):,} Sa/s" if sr == int(sr) else f"{sr:,.1f} Sa/s"
            self.sample_rate_label.config(text=text, fg="black")
        except ValueError:
            self.sample_rate_label.config(text="--", fg="gray")

    def update_modulation_fields(self):
        modulation = self.modulation_var.get()
        is_4fsk = modulation == "4FSK"
        is_gfsk = modulation == "GFSK"
        is_fsk_family = modulation in ("FSK", "4FSK", "GFSK")  # modulations that use tones
        is_psk = modulation in ("BPSK", "QPSK", "\u03c0/4-QPSK", "DQPSK", "OQPSK")

        # All conditionally-shown widgets
        widgets_to_hide = [
            self.tone0_label,
            self.mark_entry,
            self.tone0_unit_entry,
            self.tone1_label,
            self.space_entry,
            self.tone1_unit_entry,
            self.samples_per_bit_label,
            self.samples_per_bit_entry,
            self.center_label,
            self.center_entry,
            self.center_unit_entry,
            self.delta_label,
            self.delta_entry,
            self.delta_unit_entry,
            self.generate_button,
            self.dibit_map_label,
            self.dibit_map_entry,
            self.dibit_map_info_button,
            self.tone2_label,
            self.tone2_entry,
            self.tone2_unit_entry,
            self.tone3_label,
            self.tone3_entry,
            self.tone3_unit_entry,
            self.gfsk_bt_label,
            self.gfsk_bt_entry,
            self.gfsk_bt_preset_label,
            self.gfsk_bt_preset_menu,
        ]
        for widget in widgets_to_hide:
            widget.grid_remove()

        if is_4fsk:
            # Row 0: Tone 0 and Tone 1
            self.tone0_label.grid(row=0, column=0, sticky="w")
            self.mark_entry.grid(row=0, column=1, sticky="w")
            self.tone0_unit_entry.grid(row=0, column=2, sticky="w", padx=(8, 0))
            self.tone1_label.grid(row=0, column=3, sticky="w", padx=(16, 0))
            self.space_entry.grid(row=0, column=4, sticky="w")
            self.tone1_unit_entry.grid(row=0, column=5, sticky="w", padx=(8, 0))
            # Row 1: Tone 2 and Tone 3
            self.tone2_label.grid(row=1, column=0, sticky="w", pady=(8, 0))
            self.tone2_entry.grid(row=1, column=1, sticky="w", pady=(8, 0))
            self.tone2_unit_entry.grid(row=1, column=2, sticky="w", padx=(8, 0), pady=(8, 0))
            self.tone3_label.grid(row=1, column=3, sticky="w", padx=(16, 0), pady=(8, 0))
            self.tone3_entry.grid(row=1, column=4, sticky="w", pady=(8, 0))
            self.tone3_unit_entry.grid(row=1, column=5, sticky="w", padx=(8, 0), pady=(8, 0))
            # Row 2: Samples per bit
            self.samples_per_bit_label.grid(row=2, column=0, sticky="w", pady=(8, 0))
            self.samples_per_bit_entry.grid(row=2, column=1, sticky="w", pady=(8, 0))
            # Row 3: Center freq and deviation
            self.center_label.grid(row=3, column=0, sticky="w", pady=(8, 0))
            self.center_entry.grid(row=3, column=1, sticky="w", pady=(8, 0))
            self.center_unit_entry.grid(row=3, column=2, sticky="w", padx=(8, 0), pady=(8, 0))
            self.delta_label.grid(row=3, column=3, sticky="w", padx=(16, 0), pady=(8, 0))
            self.delta_entry.grid(row=3, column=4, sticky="w", pady=(8, 0))
            self.delta_unit_entry.grid(row=3, column=5, sticky="w", padx=(8, 0), pady=(8, 0))
            # Row 4: Generate button + dibit mapping
            self.generate_button.grid(row=4, column=0, columnspan=2, pady=(12, 0), sticky="w")
            self.dibit_map_label.grid(row=4, column=3, sticky="w", padx=(16, 0), pady=(12, 0))
            self.dibit_map_entry.grid(row=4, column=4, sticky="w", pady=(12, 0))
            self.dibit_map_info_button.grid(row=4, column=5, sticky="w", padx=(8, 0), pady=(12, 0))

        elif is_gfsk:
            # Row 0: Tone 0 and Tone 1 (mark/space frequencies)
            self.tone0_label.grid(row=0, column=0, sticky="w")
            self.mark_entry.grid(row=0, column=1, sticky="w")
            self.tone0_unit_entry.grid(row=0, column=2, sticky="w", padx=(8, 0))
            self.tone1_label.grid(row=0, column=3, sticky="w", padx=(16, 0))
            self.space_entry.grid(row=0, column=4, sticky="w")
            self.tone1_unit_entry.grid(row=0, column=5, sticky="w", padx=(8, 0))
            # Row 1: Samples per bit
            self.samples_per_bit_label.grid(row=1, column=0, sticky="w", pady=(8, 0))
            self.samples_per_bit_entry.grid(row=1, column=1, sticky="w", pady=(8, 0))
            # Row 2: BT product + preset
            self.gfsk_bt_label.grid(row=2, column=0, sticky="w", pady=(8, 0))
            self.gfsk_bt_entry.grid(row=2, column=1, sticky="w", pady=(8, 0))
            self.gfsk_bt_preset_label.grid(row=2, column=3, sticky="w", padx=(16, 0), pady=(8, 0))
            self.gfsk_bt_preset_menu.grid(row=2, column=4, columnspan=2, sticky="w", pady=(8, 0))

        elif modulation == "FSK":
            # Row 0: Tone 0 and Tone 1
            self.tone0_label.grid(row=0, column=0, sticky="w")
            self.mark_entry.grid(row=0, column=1, sticky="w")
            self.tone0_unit_entry.grid(row=0, column=2, sticky="w", padx=(8, 0))
            self.tone1_label.grid(row=0, column=3, sticky="w", padx=(16, 0))
            self.space_entry.grid(row=0, column=4, sticky="w")
            self.tone1_unit_entry.grid(row=0, column=5, sticky="w", padx=(8, 0))
            # Row 1: Samples per bit
            self.samples_per_bit_label.grid(row=1, column=0, sticky="w", pady=(8, 0))
            self.samples_per_bit_entry.grid(row=1, column=1, sticky="w", pady=(8, 0))

        elif is_psk:
            # PSK modes: no tones, only samples per bit
            self.samples_per_bit_label.grid(row=0, column=0, sticky="w")
            self.samples_per_bit_entry.grid(row=0, column=1, sticky="w")

    def on_modulation_change(self, *_args):
        self.update_modulation_fields()

    def _on_gfsk_bt_preset(self, selection):
        """Auto-fill BT product entry when a preset is chosen."""
        val = self._gfsk_bt_presets.get(selection)
        if val is not None:
            self.gfsk_bt_entry.delete(0, tk.END)
            self.gfsk_bt_entry.insert(0, val)

    def _get_gfsk_bw_hz(self):
        """Return GFSK bandwidth in Hz from BT product * symbol_rate."""
        try:
            bt = float(self.gfsk_bt_entry.get())
            symbol_rate = float(self.baud_entry.get())
            if bt > 0 and symbol_rate > 0:
                return bt * symbol_rate
        except ValueError:
            pass
        return 250.0  # safe fallback

    def show_4fsk_mapping_info(self):
        messagebox.showinfo(
            "4FSK Mapping Options",
            "4FSK mapping controls which dibit (2-bit symbol) each tone index represents.\n\n"
            "binary\n"
            "T0->00, T1->01, T2->10, T3->11\n"
            "Straight binary ordering.\n\n"
            "gray\n"
            "T0->00, T1->01, T2->11, T3->10\n"
            "Gray coding minimizes bit changes between adjacent tones.\n\n"
            "dmr\n"
            "T0->11, T1->10, T2->00, T3->01\n"
            "DMR-style dibit mapping used by this project's default 4FSK path.\n\n"
            "dmr_inverted\n"
            "T0->01, T1->00, T2->10, T3->11\n"
            "Inverted variant for interoperability/debugging with opposite symbol polarity.",
        )

    def _refresh_tree_rowheight(self):
        style = ttk.Style(self.root)
        ui_scale = float(self.root.tk.call("tk", "scaling"))
        tree_rowheight = max(
            int(32 * ui_scale),
            self.base_font.metrics("linespace") + int(14 * ui_scale),
        )
        style.configure("Custom.Treeview", rowheight=tree_rowheight, font=self.base_font)

    def on_message_option_selected(self, option):
        if option == "Save Message":
            self.save_fields_json()
        elif option == "Load Message":
            self.load_fields_json()
        elif option == "Load DMR Template":
            self.load_tier3_min_template()
        elif option == "Edit DMR Template":
            self.open_tier3_template_editor()
        # Reset menu to default state
        self.message_option_var.set("Message Options")

    def _frequency_to_hz(self, value_text, unit_text):
        unit = unit_text.strip()
        scale_to_hz = {
            "Hz": 1.0,
            "kHz": 1000.0,
            "MHz": 1000000.0,
        }
        return float(value_text) * scale_to_hz.get(unit, 1000.0)

    def _hz_to_frequency_text(self, value_hz, unit_text):
        unit = unit_text.strip()
        scale_from_hz = {
            "Hz": 1.0,
            "kHz": 1.0 / 1000.0,
            "MHz": 1.0 / 1000000.0,
        }
        return f"{float(value_hz) * scale_from_hz.get(unit, 1.0 / 1000.0):.6f}"

    def _repo_root_dir(self):
        return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

    def _repo_output_dir(self):
        out = os.path.join(self._repo_root_dir(), "output")
        os.makedirs(out, exist_ok=True)
        return out

    def generate_4fsk_tones(self):
        try:
            center_hz = self._frequency_to_hz(self.center_entry.get(), self.center_unit_var.get())
            delta_value = float(self.delta_entry.get())
        except ValueError:
            messagebox.showerror("Invalid center/delta", "Center frequency and delta must be numeric.")
            return
        delta_unit = self.delta_unit_var.get().strip()
        unit_scale_to_hz = {
            "Hz": 1.0,
            "kHz": 1000.0,
            "MHz": 1000000.0,
        }
        delta_hz = delta_value * unit_scale_to_hz.get(delta_unit, 1000.0)

        if center_hz <= 0 or delta_hz <= 0:
            messagebox.showerror("Invalid values", "Center frequency and delta must be positive.")
            return

        # Interpret delta as the requested center deviation for the inner tones.
        # This places tones at -3d, -d, +d, +3d around center.
        tone0_hz = center_hz - 3.0 * delta_hz
        tone1_hz = center_hz - 1.0 * delta_hz
        tone2_hz = center_hz + 1.0 * delta_hz
        tone3_hz = center_hz + 3.0 * delta_hz

        def fmt_tone_value(value_hz, unit_var):
            scale_from_hz = {
                "Hz": 1.0,
                "kHz": 1.0 / 1000.0,
                "MHz": 1.0 / 1000000.0,
            }
            unit_value = float(value_hz) * scale_from_hz.get(unit_var.get().strip(), 1.0 / 1000.0)
            return f"{unit_value:.6f}"

        self.mark_entry.delete(0, tk.END)
        self.mark_entry.insert(0, fmt_tone_value(tone0_hz, self.tone0_unit_var))
        self.space_entry.delete(0, tk.END)
        self.space_entry.insert(0, fmt_tone_value(tone1_hz, self.tone1_unit_var))
        self.tone2_entry.delete(0, tk.END)
        self.tone2_entry.insert(0, fmt_tone_value(tone2_hz, self.tone2_unit_var))
        self.tone3_entry.delete(0, tk.END)
        self.tone3_entry.insert(0, fmt_tone_value(tone3_hz, self.tone3_unit_var))
        self.status_label.config(text="4FSK tones generated: center ±delta (inner), center ±3delta (outer).")

    def _recommended_spb_for_modulation(self, modulation, symbol_rate, tones_hz, gfsk_bw_hz):
        if symbol_rate <= 0:
            raise ValueError("Symbol rate must be positive.")

        tones_abs = [abs(float(t)) for t in tones_hz]
        max_tone_hz = max(tones_abs) if tones_abs else 0.0

        if modulation == "FSK":
            tone_sep = abs(tones_hz[1] - tones_hz[0]) if len(tones_hz) >= 2 else 0.0
            freq_dev = 0.5 * tone_sep
            occupied_bw_hz = 2.0 * (freq_dev + symbol_rate)
            highest_component_hz = max(max_tone_hz, 0.5 * occupied_bw_hz)
            practical_margin = 1.25
            min_spb = 6
        elif modulation == "4FSK":
            if len(tones_hz) < 4:
                raise ValueError("4FSK requires 4 tones.")
            ordered = sorted(float(t) for t in tones_hz)
            deltas = [ordered[i + 1] - ordered[i] for i in range(len(ordered) - 1)]
            spacing_hz = min((d for d in deltas if d > 0), default=0.0)
            occupied_bw_hz = 3.0 * spacing_hz + 2.0 * symbol_rate
            highest_component_hz = max(max_tone_hz, 0.5 * occupied_bw_hz)
            practical_margin = 1.25
            min_spb = 6
        elif modulation == "GFSK":
            tone_sep = abs(tones_hz[1] - tones_hz[0]) if len(tones_hz) >= 2 else 0.0
            freq_dev = 0.5 * tone_sep
            shaping_bw_hz = max(float(gfsk_bw_hz), 0.5 * symbol_rate)
            occupied_bw_hz = 2.0 * (freq_dev + shaping_bw_hz)
            highest_component_hz = max(max_tone_hz, 0.5 * occupied_bw_hz)
            practical_margin = 1.5
            min_spb = 8
        elif modulation == "QPSK":
            # Root-raised-cosine practical default roll-off for sizing bandwidth.
            rolloff = 0.35
            occupied_bw_hz = symbol_rate * (1.0 + rolloff)
            highest_component_hz = max(max_tone_hz, 0.5 * occupied_bw_hz)
            practical_margin = 2.0
            # In this codebase QPSK uses samples_per_symbol = 2 * samples_per_bit,
            # so min_spb=2 yields 4 samples/symbol.
            min_spb = 2
        elif modulation == "BPSK":
            # BPSK: 1 bit/symbol; RRC roll-off 0.5 default
            occupied_bw_hz = symbol_rate * 1.5
            highest_component_hz = max(max_tone_hz, 0.5 * occupied_bw_hz)
            practical_margin = 2.0
            min_spb = 4
        elif modulation in ("π/4-QPSK", "DQPSK", "OQPSK"):
            # pi/4-QPSK, DQPSK, OQPSK: similar bandwidth to QPSK
            rolloff = 0.35
            occupied_bw_hz = symbol_rate * (1.0 + rolloff)
            highest_component_hz = max(max_tone_hz, 0.5 * occupied_bw_hz)
            practical_margin = 2.0
            min_spb = 2
        else:
            raise ValueError(f"Unsupported modulation: {modulation}")

        nyquist_sample_rate_hz = max(2.0 * highest_component_hz, 2.0 * symbol_rate)
        target_sample_rate_hz = max(practical_margin * nyquist_sample_rate_hz, min_spb * symbol_rate)
        samples_per_bit = max(min_spb, int(math.ceil(target_sample_rate_hz / symbol_rate)))
        return samples_per_bit, nyquist_sample_rate_hz, target_sample_rate_hz

    def auto_set_samples_per_bit(self):
        modulation = self.modulation_var.get()
        try:
            symbol_rate = float(self.baud_entry.get())
            if symbol_rate <= 0:
                raise ValueError("Symbol rate must be positive.")

            is_psk_mode = modulation in ("BPSK", "QPSK", "\u03c0/4-QPSK", "DQPSK", "OQPSK")
            tones = [0.0, 0.0]  # PSK modes don't use tones
            if not is_psk_mode:
                tone0 = self._frequency_to_hz(self.mark_entry.get(), self.tone0_unit_var.get())
                tone1 = self._frequency_to_hz(self.space_entry.get(), self.tone1_unit_var.get())
                tones = [tone0, tone1]
            gfsk_bw_hz = self._get_gfsk_bw_hz()

            if modulation == "4FSK":
                tone2 = self._frequency_to_hz(self.tone2_entry.get(), self.tone2_unit_var.get())
                tone3 = self._frequency_to_hz(self.tone3_entry.get(), self.tone3_unit_var.get())
                tones = [tone0, tone1, tone2, tone3]
                try:
                    center_hz = self._frequency_to_hz(self.center_entry.get(), self.center_unit_var.get())
                except ValueError:
                    center_hz = 0.0
                if center_hz > 0:
                    tones = [t - center_hz for t in tones]

            spb, nyquist_hz, target_hz = self._recommended_spb_for_modulation(
                modulation=modulation,
                symbol_rate=symbol_rate,
                tones_hz=tones,
                gfsk_bw_hz=gfsk_bw_hz,
            )
        except ValueError as exc:
            messagebox.showerror("Invalid settings", str(exc))
            return

        self.samples_per_bit_entry.delete(0, tk.END)
        self.samples_per_bit_entry.insert(0, str(spb))
        self._update_sample_rate()
        self.status_label.config(
            text=(
                f"Samples per bit set to {spb} for {modulation} "
                f"(Nyquist >= {nyquist_hz:,.0f} Sa/s, target {target_hz:,.0f} Sa/s)."
            )
        )

    def _calc_filter_taps(self):
        """Calculate filter taps as (sample_rate / symbol_rate) * 10 + 1."""
        try:
            symbol_rate = float(self.baud_entry.get())
            spb = float(self.samples_per_bit_entry.get())
            if symbol_rate <= 0 or spb <= 0:
                raise ValueError("Symbol rate and samples per bit must be positive.")
            calc_taps = int(spb * 10 + 1)
            if calc_taps % 2 == 0:
                calc_taps += 1
            self.filter_taps_entry.delete(0, tk.END)
            self.filter_taps_entry.insert(0, str(calc_taps))
            self.status_label.config(text=f"Filter taps set to {calc_taps} ((SR/SymRate)*10+1).")
        except ValueError as exc:
            messagebox.showerror("Invalid settings", str(exc))

    def hex_to_bin(self, hex_text):
        hex_value = hex_text.replace(" ", "")
        if hex_value.lower().startswith("0x"):
            hex_value = hex_value[2:]
        if len(hex_value) == 0:
            raise ValueError("Hex value must not be empty.")
        try:
            integer = int(hex_value, 16)
        except ValueError:
            raise ValueError("Hex field contains invalid characters.")
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

    def add_field(self):
        value_text = self.value_entry.get().strip()
        value_format = self.value_format_var.get().strip()
        desc = self.desc_entry.get().strip()

        if not value_text:
            messagebox.showwarning("Missing value", "Enter a field value first.")
            return

        if value_format == "Hex":
            try:
                bin_text = self.hex_to_bin(value_text)
                hex_text = self.bin_to_hex(bin_text)
            except ValueError as exc:
                messagebox.showerror("Invalid hex", str(exc))
                return
        else:
            normalized_bin = value_text.replace(" ", "")
            try:
                hex_text = self.bin_to_hex(normalized_bin)
            except ValueError as exc:
                messagebox.showerror("Invalid binary", str(exc))
                return
            bin_text = normalized_bin

        bit_count = len(bin_text)
        if bit_count == 0:
            messagebox.showwarning("Invalid value", "Field must contain at least one bit.")
            return

        encoding = self.encoding_entry.get()
        try:
            if encoding != "None":
                self.process_bit_stream(bin_text, encoding)
        except ValueError as exc:
            messagebox.showerror("Invalid encoding input", str(exc))
            return

        start_bit = self.total_bits()
        end_bit = start_bit + bit_count - 1
        bit_range = f"{start_bit}-{end_bit}" if bit_count > 1 else f"{start_bit}"

        self.fields.append({
            "bit_range": bit_range,
            "hex": hex_text.upper(),
            "bin": bin_text,
            "desc": desc,
            "encoding": encoding,
        })
        self.refresh_field_table()

        self.value_entry.delete(0, tk.END)
        self.desc_entry.delete(0, tk.END)
        self.encoding_entry.set("None")
        self.status_label.config(text=f"Field added: {bit_range} {hex_text.upper()}")

    def delete_selected_field(self, _event=None):
        selection = self.field_table.selection()
        if not selection:
            return

        selected_iid = selection[0]
        try:
            idx = int(selected_iid)
        except ValueError:
            return

        if 0 <= idx < len(self.fields):
            self.fields.pop(idx)
            self.refresh_field_table()
            self.status_label.config(text="Removed selected field.")

    def clear_fields(self):
        self.fields.clear()
        self.refresh_field_table()
        self.status_label.config(text="All fields cleared.")

    def show_bit_string(self):
        try:
            bit_string = self.build_bit_string()
        except ValueError as exc:
            messagebox.showerror("Error", str(exc))
            return
        messagebox.showinfo("Bit string", bit_string)

    def save_fields_json(self):
        if not self.fields:
            messagebox.showwarning("No fields", "There are no fields to save.")
            return

        project_dir = self._repo_root_dir()
        initial_dir = self._repo_output_dir()

        path = filedialog.asksaveasfilename(
            title="Save field definitions",
            initialdir=initial_dir,
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*")],
        )
        if not path:
            return
        payload = self._build_message_payload()

        try:
            with open(path, "w", encoding="utf-8") as out_file:
                json.dump(payload, out_file, indent=2)
        except Exception as exc:
            messagebox.showerror("Save failed", f"Failed to save field definitions: {exc}")
            return

        self.status_label.config(text=f"Saved field definitions to {os.path.relpath(path, project_dir)}")
        messagebox.showinfo("Saved", f"Field definitions saved to:\n{path}")

    def load_fields_json(self):
        project_dir = self._repo_root_dir()
        initial_dir = self._repo_output_dir()

        path = filedialog.askopenfilename(
            title="Load field definitions",
            initialdir=initial_dir,
            filetypes=[("JSON files", "*.json"), ("All files", "*")],
        )
        if not path:
            return

        try:
            with open(path, "r", encoding="utf-8") as in_file:
                loaded = json.load(in_file)
        except Exception as exc:
            messagebox.showerror("Load failed", f"Failed to load field definitions: {exc}")
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
            messagebox.showwarning("No valid fields", "No valid field definitions were loaded.")
            return

        self.fields = parsed_fields
        self.refresh_field_table()
        self.status_label.config(text=f"Loaded field definitions from {os.path.relpath(path, project_dir)}")
        messagebox.showinfo("Loaded", f"Field definitions loaded from:\n{path}")

    def apply_settings(self, settings):
        def set_if_present(widget, key, transform=str):
            value = settings.get(key)
            if value is not None:
                widget.delete(0, tk.END)
                widget.insert(0, transform(value))

        if not isinstance(settings, dict):
            return

        modulation = settings.get("modulation")
        if modulation in ("FSK", "4FSK", "BPSK", "QPSK", "π/4-QPSK", "DQPSK", "OQPSK", "GFSK"):
            self.modulation_var.set(modulation)
        symbol_rate = settings.get("symbol_rate")
        if symbol_rate is None:
            symbol_rate = settings.get("baud")
        if symbol_rate is not None:
            self.baud_entry.delete(0, tk.END)
            self.baud_entry.insert(0, str(symbol_rate))
        set_if_present(self.samples_per_bit_entry, "samples_per_bit")
        tone0_unit = settings.get("tone0_unit", "kHz")
        tone1_unit = settings.get("tone1_unit", "kHz")
        tone2_unit = settings.get("tone2_unit", "kHz")
        tone3_unit = settings.get("tone3_unit", "kHz")
        center_unit = settings.get("center_unit", "kHz")
        delta_unit = settings.get("delta_unit", "kHz")

        if tone0_unit in ("Hz", "kHz", "MHz"):
            self.tone0_unit_var.set(tone0_unit)
        if tone1_unit in ("Hz", "kHz", "MHz"):
            self.tone1_unit_var.set(tone1_unit)
        if tone2_unit in ("Hz", "kHz", "MHz"):
            self.tone2_unit_var.set(tone2_unit)
        if tone3_unit in ("Hz", "kHz", "MHz"):
            self.tone3_unit_var.set(tone3_unit)
        if center_unit in ("Hz", "kHz", "MHz"):
            self.center_unit_var.set(center_unit)
        if delta_unit in ("Hz", "kHz", "MHz"):
            self.delta_unit_var.set(delta_unit)

        set_if_present(self.mark_entry, "tone0", lambda v: self._hz_to_frequency_text(v, self.tone0_unit_var.get()))
        set_if_present(self.space_entry, "tone1", lambda v: self._hz_to_frequency_text(v, self.tone1_unit_var.get()))
        set_if_present(self.tone2_entry, "tone2", lambda v: self._hz_to_frequency_text(v, self.tone2_unit_var.get()))
        set_if_present(self.tone3_entry, "tone3", lambda v: self._hz_to_frequency_text(v, self.tone3_unit_var.get()))
        set_if_present(self.center_entry, "center_freq_hz", lambda v: self._hz_to_frequency_text(v, self.center_unit_var.get()))
        set_if_present(self.delta_entry, "delta_hz", lambda v: self._hz_to_frequency_text(v, self.delta_unit_var.get()))
        # GFSK BT product, or fall back from legacy gfsk_bw_hz
        gfsk_bt = settings.get("gfsk_bt")
        if gfsk_bt is not None:
            self.gfsk_bt_entry.delete(0, tk.END)
            self.gfsk_bt_entry.insert(0, str(gfsk_bt))
        elif settings.get("gfsk_bw_hz") and settings.get("symbol_rate"):
            try:
                bt_derived = float(settings["gfsk_bw_hz"]) / float(settings["symbol_rate"])
                self.gfsk_bt_entry.delete(0, tk.END)
                self.gfsk_bt_entry.insert(0, f"{bt_derived:.4f}")
            except (ValueError, ZeroDivisionError):
                pass
        set_if_present(self.filter_taps_entry, "filter_taps")
        dibit_mapping = settings.get("dibit_mapping")
        if dibit_mapping in ("dmr", "dmr_inverted", "binary", "gray"):
            self.dibit_map_var.set(dibit_mapping)
        filter_type = settings.get("filter")
        if filter_type in ("None", "Lowpass", "RRC"):
            self.filter_var.set(filter_type)
        self.update_modulation_fields()
        self._refresh_tree_rowheight()

    def build_bit_string(self):
        if not self.fields:
            raise ValueError("Add at least one field before encoding.")

        bit_string = []
        for field in self.fields:
            field_bits = field["bin"]
            encoding = field.get("encoding", "None")
            if encoding != "None":
                field_bits = self.process_bit_stream(field_bits, encoding)
            bit_string.append(field_bits)
        return "".join(bit_string)

    def total_bits(self):
        return sum(len(field["bin"]) for field in self.fields)

    def process_bit_stream(self, bit_string, encoding_mode):
        return encode_field_bits(bit_string, encoding_mode)

    def repeated_bits(self, length, pattern="01"):
        if length <= 0:
            return ""
        out = []
        for i in range(length):
            out.append(pattern[i % len(pattern)])
        return "".join(out)

    def fixed_width_bits(self, value, width):
        if value < 0 or value >= (1 << width):
            raise ValueError(f"Value {value} does not fit in {width} bits.")
        return f"{value:0{width}b}"

    def parse_int_field(self, text, label, max_value):
        text = text.strip()
        if not text:
            raise ValueError(f"{label} must not be empty.")
        try:
            value = int(text, 0)
        except ValueError:
            raise ValueError(f"{label} must be a valid integer (decimal or 0x hex).")
        if value < 0 or value > max_value:
            raise ValueError(f"{label} must be in range 0..{max_value}.")
        return value

    def parse_payload_bytes_bits(self, text):
        """Parse hex bytes text into an 80-bit string (padded with trailing zeros)."""
        cleaned = text.strip().replace("0x", "").replace("0X", "")
        for sep in [",", ":", "-", ".", "_"]:
            cleaned = cleaned.replace(sep, " ")
        parts = [p for p in cleaned.split() if p]

        if not parts:
            return None

        if len(parts) == 1:
            token = parts[0]
            if len(token) % 2 == 1:
                raise ValueError("Payload bytes hex must contain an even number of nibbles.")
            parts = [token[i:i + 2] for i in range(0, len(token), 2)]

        if len(parts) > 10:
            raise ValueError("Payload bytes accepts up to 10 bytes (80 bits).")

        bytes_out = []
        for part in parts:
            if len(part) > 2:
                raise ValueError("Each payload byte must be 1 or 2 hex digits.")
            try:
                value = int(part, 16)
            except ValueError:
                raise ValueError("Payload bytes must contain only hex byte values.")
            if value < 0 or value > 0xFF:
                raise ValueError("Payload byte out of range (00-FF).")
            bytes_out.append(value)

        bits = "".join(f"{b:08b}" for b in bytes_out)
        return bits.ljust(80, "0")

    def build_tier3_template_fields(self, values):
        color_code = values["color_code"]
        slot_flag = values["slot_flag"]
        cach_reserved = values["cach_reserved"]
        burst_type = values["burst_type"]
        tier3_mode = values.get("tier3_mode", "structured80")
        dest_id = values["dest_id"]
        src_id = values["src_id"]
        app_id = values["app_id"]
        msg_type = values["msg_type"]
        payload_word = values["payload_word"]
        payload_bytes = values.get("payload_bytes", "")
        csbk_header = values.get("csbk_header", 0x1020)
        csbk_burst_identifier = values.get("csbk_burst_identifier", 0x1)
        csbk_constant = values.get("csbk_constant", 0x3F)
        csbk_counter = values.get("csbk_counter", 0x1)
        csbk_data_crc = values.get("csbk_data_crc", 0x000000111111)
        csbk_crc = values.get("csbk_crc", 0x1111)
        csbk_sync = values.get("csbk_sync", 0xD5D7F77FD757)
        csbk_slot_type = values.get("csbk_slot_type", ((color_code & 0xF) << 4) | 0x3)

        cach_bits = (
            self.fixed_width_bits(color_code, 4)
            + self.fixed_width_bits(slot_flag, 1)
            + self.fixed_width_bits(cach_reserved, 10)
        )
        slot_type_bits = self.fixed_width_bits(color_code, 4) + self.fixed_width_bits(burst_type, 4)
        sync_bits = self.repeated_bits(48, "110100")
        payload_bytes_bits = self.parse_payload_bytes_bits(payload_bytes)

        if tier3_mode == "csbk96":
            data_bits = (
                self.fixed_width_bits(csbk_header, 16)
                + self.fixed_width_bits(csbk_burst_identifier, 4)
                + self.fixed_width_bits(csbk_constant, 8)
                + self.fixed_width_bits(csbk_counter, 4)
                + self.fixed_width_bits(csbk_data_crc, 48)
                + self.fixed_width_bits(csbk_crc, 16)
            )
            data_desc = "Data/Application:CSBK Raw (96b)"
            data_encoding = "BPTC(196,96)"
        elif tier3_mode == "csbk96burst":
            csbk_raw_bits = (
                self.fixed_width_bits(csbk_header, 16)
                + self.fixed_width_bits(csbk_burst_identifier, 4)
                + self.fixed_width_bits(csbk_constant, 8)
                + self.fixed_width_bits(csbk_counter, 4)
                + self.fixed_width_bits(csbk_data_crc, 48)
                + self.fixed_width_bits(csbk_crc, 16)
            )
            payload_bits = self.process_bit_stream(csbk_raw_bits, "BPTC(196,96)")
            slot_type_raw = self.fixed_width_bits(csbk_slot_type, 8)
            slot_bits_20 = self.process_bit_stream(slot_type_raw, "DMR Slot Type (Golay(20,8))")
            sync_bits_48 = self.fixed_width_bits(csbk_sync, 48)

            # Full DMR burst: 98 payload + 10 slot + 48 sync + 10 slot + 98 payload = 264 bits.
            data_bits = (
                payload_bits[:98]
                + slot_bits_20[:10]
                + sync_bits_48
                + slot_bits_20[10:]
                + payload_bits[98:]
            )
            data_desc = "Data/Application:CSBK Full Burst Encoded (264b)"
            data_encoding = "None"
        elif payload_bytes_bits is None:
            data_bits = (
                self.fixed_width_bits(dest_id, 24)
                + self.fixed_width_bits(src_id, 24)
                + self.fixed_width_bits(app_id, 8)
                + self.fixed_width_bits(msg_type, 8)
                + self.fixed_width_bits(payload_word, 16)
            )
            data_desc = "Data/Application:Telemetry Header+Payload Raw (80b)"
            data_encoding = "DMR Data Block (CRC-CCITT + BPTC196/96)"
        else:
            data_bits = payload_bytes_bits
            data_desc = "Data/Application:Payload Bytes Raw (80b)"
            data_encoding = "DMR Data Block (CRC-CCITT + BPTC196/96)"

        return [
            {
                "desc": "Physical:CACH Raw (15b)",
                "bin": cach_bits,
                "encoding": "DMR CACH (CRC-9 over 15b)",
            },
            {
                "desc": "Physical:Sync (48b)",
                "bin": sync_bits,
                "encoding": "None",
            },
            {
                "desc": "Physical:Slot Type Raw (8b)",
                "bin": slot_type_bits,
                "encoding": "DMR Slot Type (Golay(20,8))",
            },
            {
                "desc": data_desc,
                "bin": data_bits,
                "encoding": data_encoding,
            },
        ]

    def tier3_default_values(self):
        return {
            "tier3_mode": "structured80",
            "color_code": 1,
            "slot_flag": 0,
            "cach_reserved": 0,
            "burst_type": 2,
            "dest_id": 0x100001,
            "src_id": 0x100002,
            "app_id": 0x01,
            "msg_type": 0x01,
            "payload_word": 0x1234,
            "payload_bytes": "",
            "csbk_header": 0x1020,
            "csbk_burst_identifier": 0x1,
            "csbk_constant": 0x3F,
            "csbk_counter": 0x1,
            "csbk_data_crc": 0x000000111111,
            "csbk_crc": 0x1111,
            "csbk_sync": 0xD5D7F77FD757,
            "csbk_slot_type": 0x13,
        }

    def open_tier3_template_editor(self):
        current = dict(self.tier3_default_values())
        if hasattr(self, "tier3_template_values") and isinstance(self.tier3_template_values, dict):
            current.update(self.tier3_template_values)

        top = tk.Toplevel(self.root)
        top.title("DMR Template Editor")
        top.transient(self.root)

        editor = tk.Frame(top, padx=12, pady=12)
        editor.pack(fill=tk.BOTH, expand=True)

        spec = [
            ("color_code", "Color Code (0-15)", 15),
            ("slot_flag", "Slot Flag (0/1)", 1),
            ("cach_reserved", "CACH Reserved (0-1023)", 1023),
            ("burst_type", "Burst Type (0-15)", 15),
            ("dest_id", "Destination ID (0-16777215)", 0xFFFFFF),
            ("src_id", "Source ID (0-16777215)", 0xFFFFFF),
            ("app_id", "Application ID (0-255)", 0xFF),
            ("msg_type", "Message Type (0-255)", 0xFF),
            ("payload_word", "Payload Word (0-65535)", 0xFFFF),
            ("csbk_header", "CSBK Header (0-65535)", 0xFFFF),
            ("csbk_burst_identifier", "CSBK Burst Identifier (0-15)", 0xF),
            ("csbk_constant", "CSBK Constant (0-255)", 0xFF),
            ("csbk_counter", "CSBK Counter (0-15)", 0xF),
            ("csbk_data_crc", "CSBK Data+DataCRC (0-2^48-1)", 0xFFFFFFFFFFFF),
            ("csbk_crc", "CSBK CRC (0-65535)", 0xFFFF),
            ("csbk_sync", "CSBK Sync (48-bit, 0-2^48-1)", 0xFFFFFFFFFFFF),
            ("csbk_slot_type", "CSBK Slot Type Raw (8-bit, 0-255)", 0xFF),
        ]

        entries = {}
        for row, (key, label, _max_value) in enumerate(spec):
            tk.Label(editor, text=label, font=self.base_font).grid(row=row, column=0, sticky="w", pady=(0, 6))
            ent = tk.Entry(editor, width=18, font=self.base_font)
            ent.insert(0, hex(current[key]))
            ent.grid(row=row, column=1, sticky="w", padx=(8, 0), pady=(0, 6))
            entries[key] = ent

        payload_row = len(spec)
        tk.Label(editor, text="Template Mode", font=self.base_font).grid(row=payload_row, column=0, sticky="w", pady=(2, 6))
        mode_var = tk.StringVar(value=current.get("tier3_mode", "structured80"))
        mode_menu = ttk.Combobox(
            editor,
            textvariable=mode_var,
            values=["structured80", "payload80", "csbk96", "csbk96burst"],
            state="readonly",
            width=22,
            font=self.base_font,
        )
        mode_menu.grid(row=payload_row, column=1, sticky="w", padx=(8, 0), pady=(2, 6))

        payload_row += 1
        tk.Label(editor, text="Payload Bytes Hex (optional, up to 10 bytes)", font=self.base_font).grid(row=payload_row, column=0, sticky="w", pady=(2, 6))
        payload_entry = tk.Entry(editor, width=40, font=self.base_font)
        payload_entry.insert(0, current.get("payload_bytes", ""))
        payload_entry.grid(row=payload_row, column=1, sticky="w", padx=(8, 0), pady=(2, 6))
        tk.Label(editor, text="Example: 01 02 03 04 or 01020304", font=self.base_font).grid(row=payload_row + 1, column=0, columnspan=2, sticky="w", pady=(0, 6))

        preview_mode_var = tk.StringVar(value="Mode: Structured fields")
        preview_bits_var = tk.StringVar(value="Data Raw Bits (80):")
        preview_hex_var = tk.StringVar(value="Data Raw Hex (20):")
        preview_err_var = tk.StringVar(value="")

        tk.Label(editor, textvariable=preview_mode_var, font=self.base_font).grid(row=payload_row + 2, column=0, columnspan=2, sticky="w", pady=(2, 2))
        tk.Label(editor, textvariable=preview_bits_var, font=("Courier", 10), wraplength=850, justify="left").grid(row=payload_row + 3, column=0, columnspan=2, sticky="w", pady=(0, 2))
        tk.Label(editor, textvariable=preview_hex_var, font=("Courier", 10), wraplength=850, justify="left").grid(row=payload_row + 4, column=0, columnspan=2, sticky="w", pady=(0, 2))
        tk.Label(editor, textvariable=preview_err_var, font=self.base_font, fg="#aa0000").grid(row=payload_row + 5, column=0, columnspan=2, sticky="w", pady=(0, 4))

        action_row = payload_row + 6

        def collect_editor_values():
            values = {}
            for key, label, max_value in spec:
                values[key] = self.parse_int_field(entries[key].get(), label, max_value)
            values["tier3_mode"] = mode_var.get().strip() or "structured80"
            values["payload_bytes"] = payload_entry.get().strip()
            if values["tier3_mode"] == "payload80":
                self.parse_payload_bytes_bits(values["payload_bytes"])
            return values

        def refresh_preview(*_args):
            try:
                values = collect_editor_values()
                template = self.build_tier3_template_fields(values)
                data_bits = template[3]["bin"]
                data_hex = self.bin_to_hex(data_bits)
                if values["tier3_mode"] == "csbk96":
                    mode = "CSBK-96 raw"
                elif values["tier3_mode"] == "csbk96burst":
                    mode = "CSBK full burst (264-bit)"
                elif values["tier3_mode"] == "payload80":
                    mode = "Payload bytes"
                else:
                    mode = "Structured fields"
                preview_mode_var.set(f"Mode: {mode}")
                preview_bits_var.set(f"Data Raw Bits ({len(data_bits)}): {data_bits}")
                preview_hex_var.set(f"Data Raw Hex ({len(data_hex)}): {data_hex}")
                preview_err_var.set("")
                apply_btn.config(state="normal")
            except ValueError as exc:
                preview_mode_var.set("Mode: Invalid input")
                preview_bits_var.set("Data Raw Bits (80):")
                preview_hex_var.set("Data Raw Hex (20):")
                preview_err_var.set(f"Preview error: {exc}")
                apply_btn.config(state="disabled")

        def apply_template():
            try:
                values = collect_editor_values()
            except ValueError as exc:
                messagebox.showerror("Invalid template value", str(exc), parent=top)
                return

            self.load_tier3_min_template(values)
            top.destroy()

        apply_btn = tk.Button(editor, text="Apply Template", command=apply_template, font=self.button_font, padx=10, pady=4)
        apply_btn.grid(row=action_row, column=0, sticky="w", pady=(8, 0))
        tk.Button(editor, text="Cancel", command=top.destroy, font=self.button_font, padx=10, pady=4).grid(row=action_row, column=1, sticky="w", padx=(8, 0), pady=(8, 0))

        for ent in entries.values():
            ent.bind("<KeyRelease>", refresh_preview)
            ent.bind("<FocusOut>", refresh_preview)
        payload_entry.bind("<KeyRelease>", refresh_preview)
        payload_entry.bind("<FocusOut>", refresh_preview)
        mode_menu.bind("<<ComboboxSelected>>", refresh_preview)
        refresh_preview()

    def load_tier3_min_template(self, values=None):
        """Load a reproducible minimum DMR reference template.

        This template is structured for lab/reference use and includes:
        - Physical: CACH (15b + CRC9), Sync (48b), Slot Type (8b + Golay)
        - Data/Application: 80-bit telemetry/header block with CRC16+BPTC
        """
        self.clear_fields()
        final_values = dict(self.tier3_default_values())
        if values:
            final_values.update(values)
        self.tier3_template_values = final_values

        template = self.build_tier3_template_fields(final_values)

        for item in template:
            self.fields.append({
                "bit_range": "",
                "hex": self.bin_to_hex(item["bin"]),
                "bin": item["bin"],
                "desc": item["desc"],
                "encoding": item["encoding"],
            })

        self.refresh_field_table()

        # Slot-body reference sizing: 48 sync + 20 slot type + 196 data block = 264 bits.
        sync_air = len(template[1]["bin"])
        slot_air = len(self.process_bit_stream(template[2]["bin"], template[2]["encoding"]))
        data_air = len(self.process_bit_stream(template[3]["bin"], template[3]["encoding"]))
        cach_air = len(self.process_bit_stream(template[0]["bin"], template[0]["encoding"]))
        self.status_label.config(
            text=(
                f"Loaded DMR template. Slot body={sync_air + slot_air + data_air} bits "
                f"(sync {sync_air} + slot type {slot_air} + data {data_air}); CACH={cach_air} bits."
            )
        )

    # Hamming(7,4) per ETSI TS 102 361-1 B.3.8 
    def build_filter_coeffs(self, filter_type, num_taps, sample_rate, symbol_rate):
        if filter_type == "Lowpass":
            cutoff = min(sample_rate * 0.45, symbol_rate * 3.0)
            return self.build_lowpass_coeffs(num_taps, cutoff, sample_rate)
        if filter_type == "RRC":
            return self.build_rrc_coeffs(num_taps, 0.35, sample_rate, symbol_rate)
        raise ValueError(f"Unknown filter type: {filter_type}")

    def apply_filter_real(self, samples, coeffs):
        """FIR convolution on a list of floats; returns same-length list."""
        n = len(coeffs)
        padded = [0.0] * (n - 1) + list(samples)
        out = []
        for i in range(len(samples)):
            out.append(sum(coeffs[j] * padded[i + n - 1 - j] for j in range(n)))
        return out

    def build_lowpass_coeffs(self, num_taps, cutoff_freq, sample_rate):
        center = (num_taps - 1) // 2
        coeffs = []
        for n in range(num_taps):
            t = n - center
            if t == 0:
                value = 2.0 * cutoff_freq / sample_rate
            else:
                value = math.sin(2.0 * math.pi * cutoff_freq * t / sample_rate) / (math.pi * t)
            window = 0.54 - 0.46 * math.cos(2.0 * math.pi * n / (num_taps - 1))
            coeffs.append(value * window)
        total = sum(coeffs)
        return [c / total for c in coeffs]

    def build_rrc_coeffs(self, num_taps, rolloff, sample_rate, symbol_rate):
        center = (num_taps - 1) // 2
        T = 1.0 / symbol_rate
        dt = 1.0 / sample_rate
        coeffs = []

        for n in range(num_taps):
            t = (n - center) * dt
            if abs(t) < 1e-12:
                value = (1.0 + rolloff * (4.0 / math.pi - 1.0)) / math.sqrt(T)
            elif rolloff != 0.0 and abs(abs(t) - T / (4.0 * rolloff)) < 1e-12:
                numerator = rolloff / math.sqrt(2.0 * T)
                value = numerator * (
                    (1.0 + 2.0 / math.pi) * math.sin(math.pi / (4.0 * rolloff))
                    + (1.0 - 2.0 / math.pi) * math.cos(math.pi / (4.0 * rolloff))
                )
            else:
                numerator = (
                    math.sin(math.pi * t * (1.0 - rolloff) / T)
                    + 4.0 * rolloff * t / T * math.cos(math.pi * t * (1.0 + rolloff) / T)
                )
                denominator = math.pi * t * (1.0 - (4.0 * rolloff * t / T) ** 2) / T
                value = numerator / denominator
            coeffs.append(value)

        energy = sum(c * c for c in coeffs)
        norm = math.sqrt(energy)
        return [c / norm for c in coeffs]

    def refresh_field_table(self):
        for row in self.field_table.get_children():
            self.field_table.delete(row)

        offset = 0
        for idx, field in enumerate(self.fields):
            bit_count = len(field["bin"])
            field["bit_range"] = f"{offset}-{offset + bit_count - 1}" if bit_count > 1 else f"{offset}"
            offset += bit_count
            encoding = field.get("encoding", "None")
            self.field_table.insert("", "end", iid=str(idx), values=(
                field["bit_range"],
                field["hex"],
                field["bin"],
                field["desc"],
                encoding,
            ))

    def _to_num(self, text):
        text = text.strip()
        try:
            return int(text)
        except ValueError:
            return float(text)

    def _build_message_payload(self):
        symbol_rate = self._to_num(self.baud_entry.get())
        tone0_hz = self._frequency_to_hz(self.mark_entry.get(), self.tone0_unit_var.get())
        tone1_hz = self._frequency_to_hz(self.space_entry.get(), self.tone1_unit_var.get())
        tone2_hz = self._frequency_to_hz(self.tone2_entry.get(), self.tone2_unit_var.get())
        tone3_hz = self._frequency_to_hz(self.tone3_entry.get(), self.tone3_unit_var.get())
        center_hz = self._frequency_to_hz(self.center_entry.get(), self.center_unit_var.get())
        _, resolved_domain = _resolve_4fsk_tones_domain(
            tone0_hz, tone1_hz, tone2_hz, tone3_hz, center_hz, "auto"
        )
        settings = {
            "modulation": self.modulation_var.get(),
            "symbol_rate": symbol_rate,
            "baud": symbol_rate,
            "samples_per_bit": self._to_num(self.samples_per_bit_entry.get()),
            "tone0": tone0_hz,
            "tone1": tone1_hz,
            "tone2": tone2_hz,
            "tone3": tone3_hz,
            "center_freq_hz": center_hz,
            "delta_hz": self._frequency_to_hz(self.delta_entry.get(), self.delta_unit_var.get()),
            "tone0_unit": self.tone0_unit_var.get(),
            "tone1_unit": self.tone1_unit_var.get(),
            "tone2_unit": self.tone2_unit_var.get(),
            "tone3_unit": self.tone3_unit_var.get(),
            "center_unit": self.center_unit_var.get(),
            "delta_unit": self.delta_unit_var.get(),
            "tone_domain": resolved_domain,
            "dibit_mapping": _normalize_4fsk_dibit_mapping(self.dibit_map_var.get(), "dmr"),
            "gfsk_bt": self._to_num(self.gfsk_bt_entry.get()),
            "gfsk_bw_hz": self._get_gfsk_bw_hz(),
            "filter": self.filter_var.get(),
            "filter_taps": self._to_num(self.filter_taps_entry.get()),
        }
        try:
            settings["sample_rate"] = int(round(settings["symbol_rate"] * settings["samples_per_bit"]))
        except Exception:
            settings["sample_rate"] = None

        return {
            "settings": settings,
            "fields": [dict(field) for field in self.fields],
        }

    def _set_encode_busy(self, busy, status_text=None):
        self.encode_busy = busy
        state = "disabled" if busy else "normal"
        self.encode_button.config(state=state)
        self.show_bits_button.config(state=state)
        if busy:
            self.encode_progress.grid()
            self.encode_progress.start(12)
        else:
            self.encode_progress.stop()
            self.encode_progress.grid_remove()
        if status_text is not None:
            self.status_label.config(text=status_text)

    def _set_status_async(self, text):
        self.root.after(0, lambda: self.status_label.config(text=text))

    def _on_encode_save_success(self, path, project_dir, fmt, json_path):
        self._set_encode_busy(False, f"Saved: {os.path.relpath(path, project_dir)}")
        messagebox.showinfo(
            "Saved",
            f"FSK file saved:\n{path}\n\n{fmt}\n\nMessage JSON saved:\n{json_path}",
        )

    def _on_encode_save_error(self, message):
        self._set_encode_busy(False, "Encode/save failed.")
        messagebox.showerror("Save failed", message)

    def _encode_and_save_worker(self, bit_string, path, project_dir, message_payload):
        try:
            self._set_status_async("Generating waveform...")
            waveform_data, sample_rate = self.encode_fsk(bit_string, update_status=False)

            self._set_status_async("Saving waveform...")
            if path.lower().endswith(".wav"):
                packed = bytearray()
                for i_samp, _q in waveform_data:
                    v = int(round(i_samp * 32767.0))
                    packed += struct.pack("<h", max(-32768, min(32767, v)))
                with wave.open(path, "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(sample_rate)
                    wf.writeframes(bytes(packed))
                fmt = f"WAV PCM signed 16-bit, {sample_rate} Hz"
            elif path.lower().endswith(".bin"):
                packed = bytearray()
                for i_samp, q_samp in waveform_data:
                    iv = int(round(i_samp * 32767.0))
                    qv = int(round(q_samp * 32767.0))
                    packed += struct.pack(">h", max(-32768, min(32767, iv)))
                    packed += struct.pack(">h", max(-32768, min(32767, qv)))
                header = struct.pack(
                    ">4sBBHII",
                    DPMG_BIN_MAGIC,
                    DPMG_BIN_VERSION,
                    DPMG_BIN_FORMAT_BE_IQ16,
                    0,
                    int(sample_rate),
                    DPMG_BIN_HEADER_SIZE,
                )
                with open(path, "wb") as out_file:
                    out_file.write(header)
                    out_file.write(packed)
                fmt = (
                    f"DPMG BIN (header + big-endian int16 IQ), "
                    f"set ARB sample rate to {sample_rate} Hz on instrument"
                )
            else:
                packed = bytearray()
                for i_samp, _q in waveform_data:
                    v = int(round((i_samp + 1.0) * 0.5 * 65535.0))
                    packed += struct.pack("<H", max(0, min(65535, v)))
                with open(path, "wb") as out_file:
                    out_file.write(packed)
                fmt = f"raw uint16 binary, {sample_rate} Hz"

            json_path = f"{os.path.splitext(path)[0]}.json"
            with open(json_path, "w", encoding="utf-8") as out_file:
                json.dump(message_payload, out_file, indent=2)
        except Exception as exc:
            err_msg = f"Failed to write file: {exc}"
            self.root.after(0, lambda msg=err_msg: self._on_encode_save_error(msg))
            return

        self.root.after(0, lambda: self._on_encode_save_success(path, project_dir, fmt, json_path))

    def encode_and_save(self):
        if self.encode_busy:
            return

        try:
            bit_string = self.build_bit_string()
        except ValueError as exc:
            messagebox.showerror("Error", str(exc))
            return

        project_dir = self._repo_root_dir()
        output_dir = self._repo_output_dir()

        selected = filedialog.asksaveasfilename(
            title="Choose output filename",
            initialdir=output_dir,
            defaultextension=".bin",
            filetypes=[
                ("Keysight MXG ARB IQ - signed 16-bit big-endian", "*.bin"),
                ("WAV audio - signed 16-bit PCM", "*.wav"),
                ("Raw uint16 binary (legacy)", "*.raw"),
                ("All files", "*"),
            ],
        )
        if not selected:
            return

        file_name = os.path.basename(selected)
        path = os.path.join(output_dir, file_name)
        try:
            message_payload = self._build_message_payload()
        except Exception as exc:
            messagebox.showerror("Error", f"Failed to build message JSON payload: {exc}")
            return

        self._set_encode_busy(True, "Generating waveform...")
        self.encode_worker = threading.Thread(
            target=self._encode_and_save_worker,
            args=(bit_string, path, project_dir, message_payload),
            daemon=True,
        )
        self.encode_worker.start()

    def encode_fsk(self, bit_string, update_status=True):
        try:
            symbol_rate = float(self.baud_entry.get())
            samples_per_bit = int(self.samples_per_bit_entry.get())
            tone0 = self._frequency_to_hz(self.mark_entry.get(), self.tone0_unit_var.get())
            tone1 = self._frequency_to_hz(self.space_entry.get(), self.tone1_unit_var.get())
            filter_type = self.filter_var.get()
            filter_taps = int(self.filter_taps_entry.get())
            modulation = self.modulation_var.get()
            gfsk_bw_hz = self._get_gfsk_bw_hz()
            dibit_mapping = _normalize_4fsk_dibit_mapping(self.dibit_map_var.get(), "dmr")
        except ValueError:
            raise ValueError("Symbol rate, samples-per-bit, tone frequencies, or filter taps must be numeric.")

        tone2 = 0.0
        tone3 = 0.0
        delta = 0.0
        if modulation == "4FSK":
            try:
                tone2 = self._frequency_to_hz(self.tone2_entry.get(), self.tone2_unit_var.get())
                tone3 = self._frequency_to_hz(self.tone3_entry.get(), self.tone3_unit_var.get())
                delta = self._frequency_to_hz(self.delta_entry.get(), self.delta_unit_var.get())
            except ValueError:
                raise ValueError("For 4FSK, Tone 2, Tone 3, and delta must be numeric.")

        if modulation == "4FSK":
            try:
                center_hz = self._frequency_to_hz(self.center_entry.get(), self.center_unit_var.get())
            except ValueError:
                center_hz = 0.0
            (tone0, tone1, tone2, tone3), _resolved_domain = _resolve_4fsk_tones_domain(
                tone0, tone1, tone2, tone3, center_hz, "auto"
            )

        if symbol_rate <= 0 or samples_per_bit <= 0:
            raise ValueError("Symbol rate and samples per bit must be positive numbers.")
        if modulation in ("FSK", "GFSK") and (tone0 <= 0 or tone1 <= 0):
            raise ValueError("Tone 0 and Tone 1 must be positive for FSK/GFSK.")
        if modulation == "GFSK" and gfsk_bw_hz <= 0:
            raise ValueError("GFSK BW must be positive.")
        if modulation == "4FSK":
            if any(t == 0 for t in (tone0, tone1, tone2, tone3)):
                raise ValueError("For 4FSK, no tone may be zero.")
            if delta <= 0:
                raise ValueError("4FSK delta must be a positive number.")

        if modulation in ("4FSK", "QPSK", "π/4-QPSK", "DQPSK", "OQPSK") and (len(bit_string) % 2 == 1):
            bit_string += "0"
            if update_status:
                self.status_label.config(text=f"Odd bit length padded with 0 for {modulation} encoding.")

        mod = get_modulator(
            modulation,
            symbol_rate=symbol_rate,
            samples_per_bit=samples_per_bit,
            tone0=tone0,
            tone1=tone1,
            tone2=tone2,
            tone3=tone3,
            dibit_mapping=dibit_mapping,
            gfsk_bw_hz=gfsk_bw_hz,
            amplitude=0.8,
            spread_factor=int(getattr(self, "spread_factor", 15)),
            pn_seed=int(getattr(self, "pn_seed", 1)),
            css_sf=int(getattr(self, "css_sf", 7)),
            css_bw=float(getattr(self, "css_bw", 125000.0)),
            css_os=int(getattr(self, "css_os", 1)),
        )
        result = mod.modulate(bit_string)
        sample_rate = result.sample_rate
        waveform_data = [(float(c.real), float(c.imag)) for c in result.iq_samples]

        if filter_type != "None":
            if modulation in ("4FSK", "DSSS", "CSS"):
                filter_type = "None"
        if filter_type != "None":
            if filter_taps <= 0 or filter_taps % 2 == 0:
                raise ValueError("Filter taps must be an odd positive integer.")
            coeffs = self.build_filter_coeffs(filter_type, filter_taps, sample_rate, symbol_rate)
            i_ch = self.apply_filter_real([s[0] for s in waveform_data], coeffs)
            q_ch = self.apply_filter_real([s[1] for s in waveform_data], coeffs)
            waveform_data = list(zip(i_ch, q_ch))

        return waveform_data, sample_rate


def main(argv=None):
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--batch", action="store_true", help="Run headless JSON->waveform encoding.")
    parser.add_argument("--fields-json", help="Path to input field-definition JSON.")
    parser.add_argument("--output", help="Output waveform path (.bin/.wav/.raw).")
    parser.add_argument("--modulation", choices=["FSK", "4FSK", "QPSK", "GFSK"])
    parser.add_argument("--symbol-rate", type=float)
    parser.add_argument("--samples-per-bit", type=int)
    parser.add_argument("--tone0", type=float)
    parser.add_argument("--tone1", type=float)
    parser.add_argument("--tone2", type=float)
    parser.add_argument("--tone3", type=float)
    parser.add_argument("--dibit-mapping", choices=["dmr", "dmr_inverted", "binary", "gray"])
    parser.add_argument("--tone-domain", choices=["auto", "absolute_rf", "baseband"])
    parser.add_argument("--gfsk-bw-hz", type=float)
    parser.add_argument("--filter", choices=["None", "Lowpass", "RRC"])
    parser.add_argument("--filter-taps", type=int)
    args = parser.parse_args(argv)

    if args.batch:
        if not args.fields_json or not args.output:
            parser.error("--batch requires --fields-json and --output")
        overrides = {
            "modulation": args.modulation,
            "symbol_rate": args.symbol_rate,
            "samples_per_bit": args.samples_per_bit,
            "tone0": args.tone0,
            "tone1": args.tone1,
            "tone2": args.tone2,
            "tone3": args.tone3,
            "dibit_mapping": args.dibit_mapping,
            "tone_domain": args.tone_domain,
            "gfsk_bw_hz": args.gfsk_bw_hz,
            "filter": args.filter,
            "filter_taps": args.filter_taps,
        }
        summary = run_batch_encode(args.fields_json, args.output, overrides)
        print(
            "Encoded {field_count} fields ({bit_count} bits) -> {output_path} "
            "[{format}, {sample_rate} Hz]".format(**summary)
        )
        return

    root = tk.Tk()
    configure_window(root)
    app = FskEncoderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main(sys.argv[1:])
