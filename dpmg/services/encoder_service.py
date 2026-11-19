"""Encoder service: build bit strings, modulate, and persist waveforms."""
from __future__ import annotations

import os
import tempfile
from typing import Any, Dict, List, Optional

from dpmg.ui.fsk_encoder import (
    _build_encoded_bit_string,
    _encode_fsk_batch,
    _save_waveform,
    run_batch_encode,
)


def _hex_to_bin(hex_text: str) -> str:
    hex_value = str(hex_text or "").strip().replace(" ", "")
    if hex_value.lower().startswith("0x"):
        hex_value = hex_value[2:]
    if not hex_value:
        raise ValueError("Hex value must not be empty.")
    integer = int(hex_value, 16)
    bit_width = len(hex_value) * 4
    return f"{integer:0{bit_width}b}"


def _bin_to_hex(bin_text: str) -> str:
    bin_value = str(bin_text or "").strip().replace(" ", "")
    if not bin_value:
        raise ValueError("Binary value must not be empty.")
    if any(ch not in "01" for ch in bin_value):
        raise ValueError("Binary field must contain only 0 and 1.")
    padded = bin_value
    pad = (-len(padded)) % 4
    if pad:
        padded = "0" * pad + padded
    integer = int(padded, 2)
    hex_width = len(padded) // 4
    return f"{integer:0{hex_width}X}"


def _normalize_fields(fields: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Normalize incoming fields so web hex/bin input matches desktop behavior."""
    normalized: List[Dict[str, Any]] = []
    for item in fields:
        hex_value = str(item.get("hex", "")).strip().upper()
        bin_value = str(item.get("bin", "")).strip().replace(" ", "")
        desc_value = str(item.get("desc", "")).strip()
        encoding = str(item.get("encoding", "None"))

        if not hex_value and not bin_value:
            continue

        if hex_value and not bin_value:
            bin_value = _hex_to_bin(hex_value)
        elif bin_value and not hex_value:
            hex_value = _bin_to_hex(bin_value)
        else:
            expected_bin = _hex_to_bin(hex_value)
            if expected_bin != bin_value:
                raise ValueError(f"Hex/bin mismatch for field '{desc_value or 'unnamed'}'.")

        normalized.append(
            {
                "hex": hex_value,
                "bin": bin_value,
                "desc": desc_value,
                "encoding": encoding,
            }
        )

    if not normalized:
        raise ValueError("No valid fields provided. Enter hex or binary for at least one field.")

    return normalized


def encode_fields_to_bin(
    fields: List[Dict[str, Any]],
    settings: Dict[str, Any],
    output_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Encode fields and write a .bin file.  Returns path and metadata.

    If *output_path* is ``None``, a temporary file is created.
    """
    normalized_fields = _normalize_fields(fields)
    bit_string = _build_encoded_bit_string(normalized_fields)
    waveform_data, sample_rate = _encode_fsk_batch(bit_string, settings)
    if output_path is None:
        fd, output_path = tempfile.mkstemp(suffix=".bin")
        os.close(fd)
    fmt = _save_waveform(output_path, waveform_data, sample_rate)
    return {
        "output_path": output_path,
        "format": fmt,
        "sample_rate": sample_rate,
        "bit_count": len(bit_string),
        "field_count": len(fields),
    }


def build_bit_string(fields, settings=None):
    """Return the assembled encoded bit string (no modulation)."""
    normalized = _normalize_fields(fields)
    return _build_encoded_bit_string(normalized)
    
  
