"""DMR (Tier 3) reference template: pure bit-string generator.

Lifted from fsk_encoder.py's build_tier3_template_fields / tier3_default_values.
"""
from __future__ import annotations
from typing import Dict, List, Optional

from dpmg.encoding.ecc_decoder import encode_field_bits


def fixed_width_bits(value: int, width: int) -> str:
    if value < 0 or value >= (1 << width):
        raise ValueError(f"Value {value} does not fit in {width} bits.")
    return f"{value:0{width}b}"


def repeated_bits(length: int, pattern: str = "01") -> str:
    if length <= 0:
        return ""
    return "".join(pattern[i % len(pattern)] for i in range(length))


def bin_to_hex(bin_text: str) -> str:
    bin_value = bin_text.replace(" ", "")
    if not bin_value:
        return ""
    if any(ch not in "01" for ch in bin_value):
        raise ValueError("Binary field must contain only 0 and 1.")
    pad = (-len(bin_value)) % 4
    bin_value = "0" * pad + bin_value
    return f"{int(bin_value, 2):0{len(bin_value)//4}X}"


def parse_payload_bytes_bits(text: str) -> Optional[str]:
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
        value = int(part, 16)
        if not (0 <= value <= 0xFF):
            raise ValueError("Payload byte out of range (00-FF).")
        bytes_out.append(value)
    return "".join(f"{b:08b}" for b in bytes_out).ljust(80, "0")


def tier3_default_values() -> Dict:
    return {
        "tier3_mode": "structured80",
        "color_code": 1, "slot_flag": 0, "cach_reserved": 0, "burst_type": 2,
        "dest_id": 0x100001, "src_id": 0x100002, "app_id": 0x01, "msg_type": 0x01,
        "payload_word": 0x1234, "payload_bytes": "",
        "csbk_header": 0x1020, "csbk_burst_identifier": 0x1, "csbk_constant": 0x3F,
        "csbk_counter": 0x1, "csbk_data_crc": 0x000000111111, "csbk_crc": 0x1111,
        "csbk_sync": 0xD5D7F77FD757, "csbk_slot_type": 0x13,
    }


def build_tier3_template_fields(values: Dict) -> List[Dict]:
    color_code = values["color_code"]
    slot_flag = values["slot_flag"]
    cach_reserved = values["cach_reserved"]
    burst_type = values["burst_type"]
    tier3_mode = values.get("tier3_mode", "structured80")
    dest_id = values["dest_id"]; src_id = values["src_id"]
    app_id = values["app_id"]; msg_type = values["msg_type"]
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

    cach_bits = (fixed_width_bits(color_code, 4) + fixed_width_bits(slot_flag, 1)
                 + fixed_width_bits(cach_reserved, 10))
    slot_type_bits = fixed_width_bits(color_code, 4) + fixed_width_bits(burst_type, 4)
    sync_bits = repeated_bits(48, "110100")
    payload_bytes_bits = parse_payload_bytes_bits(payload_bytes)

    if tier3_mode == "csbk96":
        data_bits = (fixed_width_bits(csbk_header, 16) + fixed_width_bits(csbk_burst_identifier, 4)
                     + fixed_width_bits(csbk_constant, 8) + fixed_width_bits(csbk_counter, 4)
                     + fixed_width_bits(csbk_data_crc, 48) + fixed_width_bits(csbk_crc, 16))
        data_desc = "Data/Application:CSBK Raw (96b)"
        data_encoding = "BPTC(196,96)"
    elif tier3_mode == "csbk96burst":
        csbk_raw_bits = (fixed_width_bits(csbk_header, 16) + fixed_width_bits(csbk_burst_identifier, 4)
                         + fixed_width_bits(csbk_constant, 8) + fixed_width_bits(csbk_counter, 4)
                         + fixed_width_bits(csbk_data_crc, 48) + fixed_width_bits(csbk_crc, 16))
        payload_bits = encode_field_bits(csbk_raw_bits, "BPTC(196,96)")
        slot_bits_20 = encode_field_bits(fixed_width_bits(csbk_slot_type, 8),
                                         "DMR Slot Type (Golay(20,8))")
        sync_bits_48 = fixed_width_bits(csbk_sync, 48)
        data_bits = (payload_bits[:98] + slot_bits_20[:10] + sync_bits_48
                     + slot_bits_20[10:] + payload_bits[98:])
        data_desc = "Data/Application:CSBK Full Burst Encoded (264b)"
        data_encoding = "None"
    elif payload_bytes_bits is None:
        data_bits = (fixed_width_bits(dest_id, 24) + fixed_width_bits(src_id, 24)
                     + fixed_width_bits(app_id, 8) + fixed_width_bits(msg_type, 8)
                     + fixed_width_bits(payload_word, 16))
        data_desc = "Data/Application:Telemetry Header+Payload Raw (80b)"
        data_encoding = "DMR Data Block (CRC-CCITT + BPTC196/96)"
    else:
        data_bits = payload_bytes_bits
        data_desc = "Data/Application:Payload Bytes Raw (80b)"
        data_encoding = "DMR Data Block (CRC-CCITT + BPTC196/96)"
        
    return [
        {"desc": "Physical:CACH Raw (15b)", "bin": cach_bits, "encoding": "DMR CACH (CRC-9 over 15b)"},
        {"desc": "Physical:Sync (48b)", "bin": sync_bits, "encoding": "None"},
        {"desc": "Physical:Slot Type Raw (8b)", "bin": slot_type_bits, "encoding": "DMR Slot Type (Golay(20,8))"},
        {"desc": data_desc, "bin": data_bits, "encoding": data_encoding},
    ]
        
def parse_int_field(text: str, label: str, max_value: int) -> int:
    text = (text or "").strip()
    if not text:
        raise ValueError(f"{label} must not be empty.")
    try:
        value = int(text, 0)          # base 0 accepts 0x.. and decimal
    except ValueError:
        raise ValueError(f"{label} must be a valid integer (decimal or 0x hex).")
    if value < 0 or value > max_value:
        raise ValueError(f"{label} must be in range 0..{max_value}.")
    return value

    return [
        {"desc": "Physical:CACH Raw (15b)", "bin": cach_bits, "encoding": "DMR CACH (CRC-9 over 15b)"},
        {"desc": "Physical:Sync (48b)", "bin": sync_bits, "encoding": "None"},
        {"desc": "Physical:Slot Type Raw (8b)", "bin": slot_type_bits, "encoding": "DMR Slot Type (Golay(20,8))"},
        {"desc": data_desc, "bin": data_bits, "encoding": data_encoding},
    ] 
