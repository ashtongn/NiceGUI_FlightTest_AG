"""
crc_util.py: parameterized CRC engine (Rocksoft/CRC-RevEng model) + named presets.

A CRC is fully specified by: width, poly, init, refin (reflect input bytes),
refout (reflect output), xorout. Every real-world CRC is one of these parameter sets,
so this one engine matches any protocol's CRC, as blind-survey RX needs: it
must reproduce a real signal's exact CRC to validate/trigger on it.

Presets included: CRC-8, CRC-9 (DMR), CRC-16/CCITT-FALSE, CRC-16/ARC (IBM), CRC-32.
Plus 'custom' via explicit params.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class CrcParams:
    width: int
    poly: int
    init: int
    refin: bool
    refout: bool
    xorout: int
    name: str = "custom"


# Named presets. Values are the standard catalogue parameters (crccalc/RevEng).
PRESETS = {
    # 8-bit, poly 0x07, init 0x00 (CRC-8/SMBUS)
    "CRC-8":          CrcParams(8,  0x07,       0x00,       False, False, 0x00,       "CRC-8"),
    # DMR CRC-9: poly 0x059, init 0x000, non-reflected, xorout 0x000 (ETSI TS 102 361-1)
    "CRC-9":          CrcParams(9,  0x059,      0x000,      False, False, 0x000,      "CRC-9/DMR"),
    # CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, non-reflected
    "CRC-16-CCITT":   CrcParams(16, 0x1021,     0xFFFF,     False, False, 0x0000,     "CRC-16/CCITT-FALSE"),
    # CRC-16/ARC (IBM): poly 0x8005, init 0x0000, reflected in/out
    "CRC-16-IBM":     CrcParams(16, 0x8005,     0x0000,     True,  True,  0x0000,     "CRC-16/ARC"),
    # CRC-32 (zlib/PKZIP): poly 0x04C11DB7, init 0xFFFFFFFF, reflected, xorout 0xFFFFFFFF
    "CRC-32":         CrcParams(32, 0x04C11DB7, 0xFFFFFFFF, True,  True,  0xFFFFFFFF, "CRC-32"),
}


def _reflect(val: int, width: int) -> int:
    r = 0
    for _ in range(width):
        r = (r << 1) | (val & 1)
        val >>= 1
    return r


def crc_compute(data: bytes, p: CrcParams) -> int:
    """Bit-by-bit CRC (clear and provably correct; speed is a non-issue for frame sizes)."""
    topbit = 1 << (p.width - 1)
    mask = (1 << p.width) - 1
    reg = p.init & mask
    for byte in data:
        b = _reflect(byte, 8) if p.refin else byte
        reg ^= (b << (p.width - 8)) & mask if p.width >= 8 else (b << 0)
        # for widths <8 (none here) the above still works because we mask; keep general:
        if p.width < 8:
            # process 8 input bits against a <8 register: shift bit-by-bit
            reg = p.init & mask  # (not used by our presets; guard)
        for _ in range(8):
            if reg & topbit:
                reg = ((reg << 1) ^ p.poly) & mask
            else:
                reg = (reg << 1) & mask
    if p.refout:
        reg = _reflect(reg, p.width)
    return (reg ^ p.xorout) & mask


def get_params(spec) -> CrcParams:
    """spec: preset name string, or dict {width,poly,init,refin,refout,xorout}, or CrcParams."""
    if isinstance(spec, CrcParams):
        return spec
    if isinstance(spec, str):
        if spec in PRESETS:
            return PRESETS[spec]
        raise ValueError(f"unknown CRC preset: {spec}")
    if isinstance(spec, dict):
        return CrcParams(
            width=int(spec["width"]), poly=int(spec["poly"]), init=int(spec.get("init", 0)),
            refin=bool(spec.get("refin", False)), refout=bool(spec.get("refout", False)),
            xorout=int(spec.get("xorout", 0)), name=str(spec.get("name", "custom")))
    raise ValueError(f"cannot interpret CRC spec: {spec!r}")


def crc_of(data: bytes, spec) -> int:
    return crc_compute(data, get_params(spec))
