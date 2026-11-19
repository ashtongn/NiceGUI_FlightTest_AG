"""
One link frame is one FEC block, coded with dpmg.fec, then bit interleaved.

The frame is a fixed size so every block has the same info length and the
receiver never has to read a length field off the coded bits (frame_codec
does that for variable length bursts; a stream does not need it).

fec dicts are the ones frame_codec and modem.py already use:
    None or {"type": "none"}
    {"type": "conv", "rate": "1/2".."7/8"}      K=7 soft Viterbi, zero tail
    {"type": "rs"}                               shortened RS(255,223)
    {"type": "rs+conv", "rate": ...}             RS outer, conv inner
    {"type": "ldpc", "code": "qc-r12|qc-r23|qc-r34"}
    {"type": "tpc", "code": "eH(16,11)^2|eH(32,26)^2"}
LDPC and TPC are shortened the way frame_codec._block_encode does it: the
info part is zero filled to K and only the used info bits plus the parity go
on the air, the decoder puts the known zeros back as confident LLRs.

LLR convention follows dpmg.fec: llr > 0 means bit 1. Hard bits are fed as
+1.0 / -1.0.
"""
from __future__ import annotations

from typing import Optional, Tuple

import numpy as np

RS_PARITY_BITS = 256
KNOWN_LLR = 50.0


def kind(fec: Optional[dict]) -> str:
    return str((fec or {}).get("type", "none")).lower()


def _conv():
    from dpmg.fec import conv
    return conv


def _rate(fec: dict) -> str:
    return str(fec.get("rate", "1/2"))


def max_info_bits(fec: Optional[dict]) -> Optional[int]:
    """Largest info block the code takes (None = unbounded)."""
    k = kind(fec)
    if k in ("rs", "rs+conv"):
        return 223 * 8
    if k == "ldpc":
        from dpmg.fec import ldpc
        return int(ldpc.get_ldpc(str(fec.get("code", "qc-r12"))).K)
    if k == "tpc":
        from dpmg.fec import tpc
        return int(tpc.get_tpc(str(fec.get("code", "eH(32,26)^2"))).k)
    return None


def coded_len(n_info: int, fec: Optional[dict]) -> int:
    """On air bits (before interleaving) for n_info info bits."""
    k = kind(fec)
    if k == "none":
        return int(n_info)
    if k == "conv":
        return len(_conv().encode([0] * n_info, _rate(fec)))
    if k == "rs":
        return int(n_info) + RS_PARITY_BITS
    if k == "rs+conv":
        return len(_conv().encode([0] * (n_info + RS_PARITY_BITS), _rate(fec)))
    if k == "ldpc":
        from dpmg.fec import ldpc
        code = ldpc.get_ldpc(str(fec.get("code", "qc-r12")))
        return int(n_info) + (code.N - code.K)
    if k == "tpc":
        from dpmg.fec import tpc
        t = tpc.get_tpc(str(fec.get("code", "eH(32,26)^2")))
        return (-(-int(n_info) // t.kc)) * t.nc + (t.nr - t.kr) * t.nc
    raise ValueError(f"unsupported fec type {k!r}")


def _as_bits(v) -> np.ndarray:
    return (np.asarray(v).reshape(-1) > 0).astype(np.uint8) if np.asarray(v).dtype != np.uint8 \
        else np.asarray(v).reshape(-1)


def bytes_to_bits(data: bytes) -> np.ndarray:
    return np.unpackbits(np.frombuffer(bytes(data), dtype=np.uint8))


def bits_to_bytes(bits: np.ndarray) -> bytes:
    b = np.asarray(bits, dtype=np.uint8).reshape(-1)
    return np.packbits(b).tobytes()


def _rs_encode_bits(info_bits: np.ndarray):
    from dpmg.fec import rs
    data = list(bits_to_bytes(info_bits))
    cw = rs.encode([0] * (rs.K - len(data)) + data)[rs.K - len(data):]
    return np.asarray(rs.bytes_to_bits(cw), dtype=np.uint8)


def _rs_decode_bits(hard: np.ndarray, n_info: int) -> Tuple[np.ndarray, bool]:
    from dpmg.fec import rs
    k = n_info // 8
    rx = rs.bits_to_bytes(list(hard[:n_info + RS_PARITY_BITS]))
    info_b, _n, ok = rs.decode([0] * (rs.K - k) + rx)
    return np.asarray(rs.bytes_to_bits(info_b[rs.K - k:]), dtype=np.uint8), bool(ok)


def encode_block(info_bits: np.ndarray, fec: Optional[dict]) -> np.ndarray:
    """info bits (uint8 0/1): coded bits (uint8 0/1)."""
    u = np.asarray(info_bits, dtype=np.uint8).reshape(-1)
    k = kind(fec)
    if k == "none":
        return u.copy()
    if k == "conv":
        return np.asarray(_conv().encode(u.tolist(), _rate(fec)), dtype=np.uint8)
    if k == "rs":
        return _rs_encode_bits(u)
    if k == "rs+conv":
        return np.asarray(_conv().encode(_rs_encode_bits(u).tolist(), _rate(fec)), dtype=np.uint8)
    if k == "ldpc":
        from dpmg.fec import ldpc
        code = ldpc.get_ldpc(str(fec.get("code", "qc-r12")))
        n = len(u)
        c = np.asarray(code.encode(u.tolist() + [0] * (code.K - n)), dtype=np.uint8)
        return np.concatenate([c[code._info_cols[:n]], c[code._pivot_cols]]).astype(np.uint8)
    if k == "tpc":
        from dpmg.fec import tpc
        t = tpc.get_tpc(str(fec.get("code", "eH(32,26)^2")))
        n = len(u)
        r = -(-n // t.kc)
        M = np.asarray(t.encode(u.tolist() + [0] * (t.k - n)), dtype=np.uint8).reshape(t.nr, t.nc)
        return np.concatenate([M[:r].reshape(-1), M[t.kr:].reshape(-1)]).astype(np.uint8)
    raise ValueError(f"unsupported fec type {k!r}")


def decode_block(llr: np.ndarray, fec: Optional[dict], n_info: int) -> Tuple[np.ndarray, bool]:
    """coded LLRs in, (info bits uint8, decoder_ok) out. decoder_ok is False when a
    block code reports an uncorrectable word; conv and LDPC always return their
    best guess and leave the verdict to the CRC."""
    L = np.asarray(llr, dtype=np.float64).reshape(-1)
    k = kind(fec)
    try:
        if k == "none":
            return (L[:n_info] > 0).astype(np.uint8), True
        if k == "conv":
            out = _conv().decode_soft(L, _rate(fec), n_info=n_info, terminated=True)
            return np.asarray(out, dtype=np.uint8), True
        if k == "rs":
            return _rs_decode_bits((L > 0).astype(np.uint8), n_info)
        if k == "rs+conv":
            inner = _conv().decode_soft(L, _rate(fec), n_info=n_info + RS_PARITY_BITS, terminated=True)
            return _rs_decode_bits(np.asarray(inner, dtype=np.uint8), n_info)
        if k == "ldpc":
            from dpmg.fec import ldpc
            code = ldpc.get_ldpc(str(fec.get("code", "qc-r12")))
            full = np.full(code.N, -KNOWN_LLR)
            full[code._info_cols[:n_info]] = L[:n_info]
            full[code._pivot_cols] = L[n_info:n_info + (code.N - code.K)]
            return np.asarray(code.decode(full)[:n_info], dtype=np.uint8), True
        if k == "tpc":
            from dpmg.fec import tpc
            t = tpc.get_tpc(str(fec.get("code", "eH(32,26)^2")))
            r = -(-n_info // t.kc)
            clen = r * t.nc + (t.nr - t.kr) * t.nc
            big = 2.0 * float(np.mean(np.abs(L[:clen])) or 1.0)
            R = np.full((t.nr, t.nc), -big)
            R[:r] = L[:r * t.nc].reshape(r, t.nc)
            R[t.kr:] = L[r * t.nc:clen].reshape(t.nr - t.kr, t.nc)
            return np.asarray(t.decode(R.reshape(-1)), dtype=np.uint8)[:n_info], True
    except Exception:
        return np.zeros(n_info, dtype=np.uint8), False
    raise ValueError(f"unsupported fec type {k!r}")


# interleaver
# Same row and column convention as dpmg.fec.rs.block_interleave (write depth
# rows, read columns), but over bits or soft values, so a burst of b adjacent
# channel errors lands as b/depth errors per row of the code.
def interleave(bits: np.ndarray, depth: int) -> np.ndarray:
    v = np.asarray(bits).reshape(-1)
    d = max(1, int(depth))
    if d == 1:
        return v.copy()
    ncol = -(-len(v) // d)
    padded = np.concatenate([v, np.zeros(ncol * d - len(v), dtype=v.dtype)])
    return padded.reshape(d, ncol).T.reshape(-1)


def deinterleave(vals: np.ndarray, depth: int, orig_len: int) -> np.ndarray:
    v = np.asarray(vals).reshape(-1)
    d = max(1, int(depth))
    if d == 1:
        return v[:orig_len].copy()
    ncol = len(v) // d
    return v[:ncol * d].reshape(ncol, d).T.reshape(-1)[:orig_len]
