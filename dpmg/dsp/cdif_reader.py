"""
cdif_reader.py: reader for .cdif I/Q recordings (Tektronix SignalVu / SCEPTRE).

.cdif is a Platinum/X-Midas *BLUE* file: a 512-byte Header Control Block (HCB)
followed by interleaved I/Q samples, with an extended keyword block (RF_FREQ,
TIME_EPOCH, RSA_DATA_SCALE, ...) usually appended at the end.

read_cdif(path): (iq complex64, meta) where meta carries sample_rate_hz,
center_freq_hz (from RF_FREQ when present) and the parsed keywords. The sample
rate is 1/xdelta from the HCB adjunct; the format code (CF/CI/CD/...) picks the
on-disk sample type, scaled by RSA_DATA_SCALE for the integer formats.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

import numpy as np

HCB_SIZE = 512

# Midas BLUE / Platinum file extensions: all share the same 512-byte HCB +
# extended keyword block and are read by read_header/read_cdif (which key off
# the "BLUE" magic, not the extension). .cdif is Tektronix SignalVu; .tmp /
# .blue / .prm are X-Midas, NeXtMidas and REDHAWK.
BLUE_EXTS = (".cdif", ".tmp", ".blue", ".prm")

# A BLUE file is known by its first twelve bytes, whatever it is called: the
# magic, then the number representation of the header and of the data. A
# recording saved as .bin or .iq.bin is still a BLUE file, and read as raw
# samples its header and keyword block turn into junk samples at each end.
_REPS = (b"EEEI", b"IEEE", b"VAX ", b"CRAY")


def is_blue_bytes(head: bytes) -> bool:
    """True when these first bytes of a file are a BLUE header."""
    head = bytes(head[:12])
    return len(head) >= 12 and head[0:4] == b"BLUE" and head[4:8] in _REPS and head[8:12] in _REPS


def is_blue(path: str) -> bool:
    """True when the file starts with a BLUE header. False for a file that cannot be read."""
    try:
        with open(path, "rb") as f:
            return is_blue_bytes(f.read(12))
    except OSError:
        return False


def blue_name(name: str, head: bytes) -> str:
    """The name to give a file when it is handed on: unchanged, unless the
    content is BLUE and the ending is not one a reader takes as BLUE. Then the
    ending becomes .cdif, so a reader that goes by the name reads the header."""
    name = str(name)
    if not is_blue_bytes(head) or name.lower().endswith(BLUE_EXTS):
        return name
    low = name.lower()
    for ext in (".iq.bin", ".bin", ".c64", ".iq", ".dat", ".raw"):
        if low.endswith(ext):
            return name[:len(name) - len(ext)] + ".cdif"
    return name + ".cdif"


# BLUE format code: (numpy base dtype, is_complex, full_scale for int types)
_TYPE = {
    "B": ("i1", 127.0), "I": ("i2", 32767.0), "L": ("i4", 2147483647.0),
    "F": ("f4", None), "D": ("f8", None),
}


@dataclass
class CdifMeta:
    sample_rate_hz: float = 0.0
    center_freq_hz: float = 0.0
    n_samples: int = 0
    duration_s: float = 0.0
    fmt: str = ""              # BLUE format code, e.g. "CF"
    complex: bool = True
    xstart: float = 0.0
    data_scale: float = 1.0
    keywords: dict = field(default_factory=dict)


def _parse_keywords(buf: bytes, en: str) -> dict:
    """Parse an X-Midas extended keyword block into {tag: value}."""
    kw, pos, n = {}, 0, len(buf)
    while pos + 8 <= n:
        lkey = struct.unpack(en + "i", buf[pos:pos + 4])[0]
        if lkey <= 0 or pos + lkey > n:
            break
        lextra = struct.unpack(en + "h", buf[pos + 4:pos + 6])[0]
        ltag = buf[pos + 6]
        vtype = chr(buf[pos + 7])
        vlen = lkey - lextra                      # value bytes = lkey - lextra
        val = buf[pos + 8:pos + 8 + vlen]
        tag = buf[pos + 8 + vlen:pos + 8 + vlen + ltag].decode("latin1").strip("\x00")
        try:
            if vtype == "A":
                kw[tag] = val.decode("latin1").strip("\x00 ")
            elif vtype == "D":
                kw[tag] = struct.unpack(en + "d", val[:8])[0]
            elif vtype == "F":
                kw[tag] = struct.unpack(en + "f", val[:4])[0]
            elif vtype in ("L", "I", "B"):
                kw[tag] = int.from_bytes(val, "little" if en == "<" else "big", signed=True)
            else:
                kw[tag] = val
        except Exception:
            kw[tag] = val
        pos += lkey
    return kw


def read_header(path: str) -> CdifMeta:
    """Parse just the HCB + keywords (no sample data): fast, for rate/metadata."""
    with open(path, "rb") as f:
        hcb = f.read(HCB_SIZE)
        if hcb[0:4] != b"BLUE":
            raise ValueError("not a BLUE/.cdif file (missing BLUE magic)")
        en = "<" if hcb[4:8].startswith(b"EEEI") else ">"
        ext_start, ext_size = struct.unpack(en + "2i", hcb[24:32])
        data_start, data_size = struct.unpack(en + "2d", hcb[32:48])
        fmt = hcb[52:54].decode("latin1")
        xstart, xdelta = struct.unpack(en + "2d", hcb[256:272])
        ext = b""
        if ext_size > 0:
            f.seek(ext_start * 512)
            ext = f.read(ext_size)
    kw = _parse_keywords(ext, en) if ext else {}
    mode = fmt[0] if fmt else "C"
    tcode = fmt[1] if len(fmt) > 1 else "F"
    base, fs_int = _TYPE.get(tcode, ("f4", None))
    is_complex = (mode == "C")
    bytes_per = np.dtype(base).itemsize * (2 if is_complex else 1)
    n = int(data_size) // bytes_per
    rate = (1.0 / xdelta) if xdelta else 0.0
    scale = float(kw.get("RSA_DATA_SCALE", kw.get("DATA_SCALE", 1.0)))
    cf = float(kw.get("RF_FREQ", kw.get("SBT_FREQ", kw.get("CENTER_FREQ", 0.0))))
    return CdifMeta(sample_rate_hz=rate, center_freq_hz=cf, n_samples=n,
                    duration_s=n * xdelta, fmt=fmt, complex=is_complex,
                    xstart=xstart, data_scale=scale, keywords=kw)


def read_cdif(path: str, max_samples: int | None = None):
    """Read a .cdif into (iq complex64, CdifMeta). Real (scalar) files come back
    as complex64 with a zero imaginary part. Integer formats are scaled by
    RSA_DATA_SCALE (else normalized by the type's full scale)."""
    meta = read_header(path)
    en = "<"  # EEEI; read_header already errored on a non-BLUE file
    with open(path, "rb") as f:
        hcb = f.read(HCB_SIZE)
        en = "<" if hcb[4:8].startswith(b"EEEI") else ">"
        data_start = struct.unpack(en + "d", hcb[32:40])[0]
        f.seek(int(data_start))
        tcode = meta.fmt[1] if len(meta.fmt) > 1 else "F"
        base, fs_int = _TYPE.get(tcode, ("f4", None))
        comp = 2 if meta.complex else 1
        # read only the HCB data_size worth of samples: Sceptre/SignalVu put the
        # keyword block AFTER the data, so a read-to-EOF returns it as junk
        # samples (|x| ~1e35). data_size 0 (unknown) falls back to EOF.
        count = meta.n_samples * comp if meta.n_samples > 0 else -1
        if max_samples is not None and (count < 0 or max_samples * comp < count):
            count = max_samples * comp
        arr = np.fromfile(f, dtype=en + base, count=count)
    if fs_int is not None:                        # integer format: float
        scale = meta.data_scale if meta.data_scale and meta.data_scale != 1.0 else (1.0 / fs_int)
        arr = arr.astype(np.float32) * np.float32(scale)
    else:
        arr = arr.astype(np.float32)
    if meta.complex:
        iq = arr[0::2].astype(np.float32) + 1j * arr[1::2].astype(np.float32)
        iq = iq.astype(np.complex64)
    else:
        iq = arr.astype(np.complex64)
    return iq, meta


# on-disk code for the numpy dtype we write for each BLUE format letter
_WRITE_TYPE = {"F": ("f4", None), "D": ("f8", None),
               "I": ("i2", 32767.0), "B": ("i1", 127.0), "L": ("i4", 2147483647.0)}


def _kw_entry(tag: str, vtype: str, value: bytes) -> bytes:
    """Build one extended-keyword TLV entry, lkey rounded up to 8 bytes.
    Layout mirrors _parse_keywords: [lkey i4][lextra i2][ltag i1][vtype c]
    [value][tag][pad]; value length = lkey - lextra."""
    tb = tag.encode("latin1")
    ltag, vlen = len(tb), len(value)
    raw = 8 + vlen + ltag
    lkey = (raw + 7) & ~7
    pad = lkey - raw
    lextra = lkey - vlen
    return (struct.pack("<ihB", lkey, lextra, ltag) + vtype.encode("latin1")
            + value + tb + b"\x00" * pad)


def write_cdif(path: str, iq: np.ndarray, sample_rate_hz: float,
               center_freq_hz: float = 0.0, fmt: str = "CF",
               keywords: dict | None = None) -> None:
    """Write a complex array as a little-endian BLUE/.cdif file that read_cdif
    reads back exactly. Layout is HCB(512) + keyword block + sample data, so a
    read-to-EOF returns only the samples. fmt is a 2-char BLUE code: CF
    (complex float32, default), CI (complex int16), CD (complex float64)."""
    iq = np.asarray(iq, dtype=np.complex64)
    tcode = fmt[1] if len(fmt) > 1 else "F"
    base, fs_int = _WRITE_TYPE.get(tcode, ("f4", None))
    xdelta = 1.0 / float(sample_rate_hz) if sample_rate_hz else 0.0

    # extended keywords: RF_FREQ + data scale, then any caller extras
    scale = 1.0
    if fs_int is not None:
        peak = float(np.max(np.abs(np.concatenate([iq.real, iq.imag])))) or 1.0
        scale = peak / fs_int                      # so int*scale recovers volts
    ext = _kw_entry("VER", "A", b"1.1")
    ext += _kw_entry("RF_FREQ", "D", struct.pack("<d", float(center_freq_hz)))
    ext += _kw_entry("RSA_DATA_SCALE", "D", struct.pack("<d", scale))
    for k, v in (keywords or {}).items():
        if isinstance(v, (int, float)):
            ext += _kw_entry(k, "D", struct.pack("<d", float(v)))
        else:
            ext += _kw_entry(k, "A", str(v).encode("latin1"))
    if len(ext) % 8:
        ext += b"\x00" * (8 - len(ext) % 8)

    # interleave to the chosen on-disk dtype
    inter = np.empty(iq.size * 2, dtype=np.float64)
    inter[0::2], inter[1::2] = iq.real, iq.imag
    if fs_int is not None:
        inter = np.round(inter / scale).astype("<" + base)
    else:
        inter = inter.astype("<" + base)
    data = inter.tobytes()

    ext_start = 1                                  # keyword block begins at 512
    data_start = HCB_SIZE + len(ext)               # byte offset of sample data
    hcb = bytearray(HCB_SIZE)
    hcb[0:4] = b"BLUE"
    hcb[4:8] = b"EEEI"
    hcb[8:12] = b"EEEI"
    struct.pack_into("<ii", hcb, 24, ext_start, len(ext))
    struct.pack_into("<dd", hcb, 32, float(data_start), float(len(data)))
    struct.pack_into("<i", hcb, 48, 1001)          # type 1001 = single data record
    hcb[52:54] = fmt.encode("latin1")[:2]
    struct.pack_into("<dd", hcb, 256, 0.0, xdelta)  # xstart, xdelta

    with open(path, "wb") as f:
        f.write(hcb)
        f.write(ext)
        f.write(data)


if __name__ == "__main__":
    import sys
    m = read_header(sys.argv[1])
    print(f"format {m.fmt}  rate {m.sample_rate_hz/1e6:g} MSps  center "
          f"{m.center_freq_hz/1e6:g} MHz  n {m.n_samples}  dur {m.duration_s:.2f}s")
    print("keywords:", {k: m.keywords[k] for k in list(m.keywords)[:12]})
