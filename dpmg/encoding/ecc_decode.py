"""
Decode/correct side for the DMR FEC in ecc_decoder.py. Each function inverts
its encoder exactly (matrices/genpolys taken from the encoder, not a textbook)
and reports how many bit errors it corrected. decode_field_bits() mirrors
encode_field_bits() so the RX path has one dispatch point.

Correctness is pinned by test_ecc_roundtrip.py: encode, inject <= t errors,
then decode must return the original data and the right correction count.
"""
from __future__ import annotations

from typing import List, Tuple

from .ecc_decoder import (
    _H1393_PARITY, _H15113_PARITY,
    crc_ccitt, crc_9, crc_32,
    convolutional_rate12_encode, convolutional_rate13_encode,
)

# Hamming(7,4) parity columns implied by hamming_7_4_encode's layout
# [p1,p2,d0,p3,d1,d2,d3] with p1=d0^d1^d3, p2=d0^d2^d3, p3=d1^d2^d3.
_H74_P = [[1, 1, 0], [1, 0, 1], [0, 1, 1], [1, 1, 1]]   # rows d0..d3, cols p1,p2,p3

# BPTC column code is its own (13,9); derived from bptc_196_96_encode's
# explicit column formulas (not _H1393_PARITY).
_BPTC_COL_P = [
    [1, 1, 1, 1], [1, 1, 1, 0], [0, 1, 1, 1], [1, 0, 1, 0], [0, 1, 0, 1],
    [1, 0, 1, 1], [1, 1, 0, 0], [0, 1, 1, 0], [0, 0, 1, 1],
]


def _b2l(s: str) -> List[int]:
    return [int(c) for c in s]


def _l2b(x: List[int]) -> str:
    return "".join(str(b & 1) for b in x)


# systematic single-error-correcting Hamming: codeword = data(k) || parity(4)
# with parity[c] = XOR_j data[j]*P[j][c]. H columns: data j: P[j], parity c
# gives a unit vector. syndrome picks the flipped column.

def _ham_decode_block(code: List[int], P: List[List[int]], k: int,
                      np_: int) -> Tuple[List[int], int]:
    n = k + np_
    data = code[:k]
    par = code[k:n]
    syn = [0] * np_
    for c in range(np_):
        s = par[c]
        for j in range(k):
            s ^= data[j] * P[j][c]
        syn[c] = s & 1
    if not any(syn):
        return data[:], 0
    # match syndrome against every column of H
    for j in range(k):
        if P[j] == syn:
            code[j] ^= 1
            return code[:k], 1
    for c in range(np_):
        if syn == [1 if i == c else 0 for i in range(np_)]:
            return code[:k], 1        # parity-bit error, data intact
    return code[:k], -1               # uncorrectable (>1 error)


def _ham_decode(bits: str, P: List[List[int]], k: int, np_: int
                ) -> Tuple[str, int]:
    n = k + np_
    x = _b2l(bits)
    out, corr = [], 0
    for i in range(0, len(x) - n + 1, n):
        d, c = _ham_decode_block(x[i:i + n], P, k, np_)
        out.extend(d)
        corr += max(c, 0)
    return _l2b(out), corr


def hamming_7_4_decode(bits: str) -> Tuple[str, int]:
    # de-interleave the [p1,p2,d0,p3,d1,d2,d3] wire order to [d|p] then decode
    x = _b2l(bits)
    out, corr = [], 0
    for i in range(0, len(x) - 6, 7):
        b = x[i:i + 7]
        code = [b[2], b[4], b[5], b[6], b[0], b[1], b[3]]   # d0..d3, p1,p2,p3
        d, c = _ham_decode_block(code, _H74_P, 4, 3)
        out.extend(d)
        corr += max(c, 0)
    return _l2b(out), corr


def hamming_13_9_decode(bits: str) -> Tuple[str, int]:
    return _ham_decode(bits, _H1393_PARITY, 9, 4)


def hamming_15_11_decode(bits: str) -> Tuple[str, int]:
    return _ham_decode(bits, _H15113_PARITY, 11, 4)


# Golay(20,8): 8 data << 11 reduced by genpoly 0xC75: 19-bit cyclic word,
# then an overall parity bit. Syndrome = remainder mod genpoly; a precomputed
# weight<=3 error table corrects the 19-bit word.

_GOLAY_GENPOL = 0xC75          # degree 11
_GOLAY_N = 19


def _poly_rem(word: int, genpol: int, deg: int, nbits: int) -> int:
    for i in range(nbits - 1, deg - 1, -1):
        if word & (1 << i):
            word ^= genpol << (i - deg)
    return word & ((1 << deg) - 1)


def _build_golay_table():
    tbl = {}
    n = _GOLAY_N
    idx = list(range(n))
    from itertools import combinations
    for w in range(0, 4):                       # weight 0..3
        for combo in combinations(idx, w):
            e = 0
            for p in combo:
                e |= (1 << p)
            syn = _poly_rem(e, _GOLAY_GENPOL, 11, n)
            tbl.setdefault(syn, e)              # lowest weight wins
    return tbl


_GOLAY_TABLE = _build_golay_table()


def golay_20_8_decode(bits: str) -> Tuple[str, int]:
    if len(bits) < 20:
        return "", -1
    code20 = int(bits[:20], 2)
    word19 = code20 >> 1
    syn = _poly_rem(word19, _GOLAY_GENPOL, 11, _GOLAY_N)
    corr = 0
    if syn:
        e = _GOLAY_TABLE.get(syn)
        if e is None:
            return f"{(word19 >> 11) & 0xFF:08b}", -1
        word19 ^= e
        corr = bin(e).count("1")
    data = (word19 >> 11) & 0xFF
    return f"{data:08b}", corr


# BPTC(196,96): inverse interleave (a*181)%196, then iterate row Hamming
# (15,11) and column Hamming (13,9) single-error correction, extract payload.

_BPTC_PERM = [(a * 181) % 196 for a in range(196)]     # de[a]: raw[perm[a]]
_BPTC_PAYLOAD = [(4, 11), (16, 26), (31, 41), (46, 56), (61, 71),
                 (76, 86), (91, 101), (106, 116), (121, 131)]


def bptc_196_96_decode(bits: str) -> Tuple[str, int]:
    if len(bits) < 196:
        return "", -1
    raw = _b2l(bits[:196])
    de = [0] * 196
    for a in range(196):
        de[a] = raw[_BPTC_PERM[a]]

    corr = 0
    for _ in range(5):
        changed = False
        for row in range(9):
            base = row * 15 + 1
            code = de[base:base + 15]
            _, c = _ham_decode_block(code, _H15113_PARITY, 11, 4)
            if c > 0:
                de[base:base + 15] = code
                corr += c
                changed = True
        for col in range(15):
            code = [de[r * 15 + 1 + col] for r in range(13)]
            _, c = _ham_decode_block(code, _BPTC_COL_P, 9, 4)
            if c > 0:
                for r in range(13):
                    de[r * 15 + 1 + col] = code[r]
                corr += c
                changed = True
        if not changed:
            break

    out = []
    for lo, hi in _BPTC_PAYLOAD:
        out.extend(de[lo:hi + 1])
    return _l2b(out), corr


# Viterbi for the K=7 rate-1/2 and rate-1/3 convolutional codes and the
# 16-state rate-3/4 trellis. Hard-decision, Hamming-metric.

def _conv_viterbi(bits: str, gens: List[int], tail: int = 6) -> Tuple[str, int]:
    r = gens and len(gens)
    x = _b2l(bits)
    nsteps = len(x) // r
    if nsteps == 0:
        return "", -1
    NS = 128
    INF = 1 << 30

    def trans(reg, b):
        return ((reg >> 1) | (b << 6)) & 0x7F

    def outp(reg):
        return [bin(reg & g).count("1") & 1 for g in gens]

    pm = [INF] * NS
    pm[0] = 0
    prev = [[-1] * NS for _ in range(nsteps)]
    for t in range(nsteps):
        rx = x[t * r:t * r + r]
        npm = [INF] * NS
        for s in range(NS):
            if pm[s] == INF:
                continue
            for b in (0, 1):
                ns = trans(s, b)
                o = outp(ns)
                m = pm[s] + sum(a ^ c for a, c in zip(o, rx))
                if m < npm[ns]:
                    npm[ns] = m
                    prev[t][ns] = (s << 1) | b
        pm = npm
    # the 6-bit zero tail does not fully terminate this 7-bit register, so
    # the end state is not fixed at 0: traceback from the ML (min-metric)
    # state instead of forcing it
    end = min(range(NS), key=lambda s: pm[s])
    metric = pm[end]
    dec = []
    s = end
    for t in range(nsteps - 1, -1, -1):
        p = prev[t][s]
        dec.append(p & 1)
        s = p >> 1
    dec.reverse()
    if tail:
        dec = dec[:-tail]
    return _l2b(dec), metric


def convolutional_rate12_decode(bits: str) -> Tuple[str, int]:
    return _conv_viterbi(bits, [0b1011011, 0b1111001], tail=6)


def convolutional_rate13_decode(bits: str) -> Tuple[str, int]:
    return _conv_viterbi(bits, [0b1011011, 0b1111001, 0b1110101], tail=6)


def rate_34_trellis_decode(bits: str) -> Tuple[str, int]:
    x = _b2l(bits)
    nsteps = len(x) // 4
    if nsteps == 0:
        return "", -1
    NS = 16
    INF = 1 << 30

    def outp(state):
        b3 = (state >> 3) & 1
        b2 = (state >> 2) & 1
        b1 = (state >> 1) & 1
        b0 = state & 1
        return [b0 ^ b1 ^ b3, b0 ^ b2 ^ b3, b0 ^ b1 ^ b2 ^ b3, b0 ^ b1 ^ b2]

    pm = [INF] * NS
    pm[0] = 0
    prev = [[-1] * NS for _ in range(nsteps)]
    for t in range(nsteps):
        rx = x[t * 4:t * 4 + 4]
        npm = [INF] * NS
        for s in range(NS):
            if pm[s] == INF:
                continue
            for b in (0, 1):
                ns = ((s << 1) | b) & 0xF
                o = outp(ns)
                m = pm[s] + sum(a ^ c for a, c in zip(o, rx))
                if m < npm[ns]:
                    npm[ns] = m
                    prev[t][ns] = (s << 1) | b
        pm = npm
    end = min(range(NS), key=lambda s: pm[s])
    metric = pm[end]
    dec = []
    s = end
    for t in range(nsteps - 1, -1, -1):
        p = prev[t][s]
        dec.append(p & 1)
        s = p >> 1
    dec.reverse()
    return _l2b(dec), metric


# CRC verify through the authoritative encoders (recompute + compare).

def _crc_check(bits: str, ncrc: int, fn) -> Tuple[str, bool]:
    if len(bits) < ncrc:
        return "", False
    data, rx = bits[:-ncrc], bits[-ncrc:]
    return data, (fn(data) == rx)


# dispatch mirroring encode_field_bits. Returns (data_bits, n_corrected, ok).
# ok is False only when the code can affirmatively flag failure (CRC mismatch
# or Viterbi/Golay/Hamming reporting an uncorrectable block).

def encoded_len(encoding: str, orig_len: int) -> int:
    """Wire length encode_field_bits produces for orig_len info bits. Needed to
    split a concatenated multi-field message back into fields before decode."""
    def ceil_mul(k):
        return ((orig_len + k - 1) // k) * k
    if not encoding or encoding == "None":
        return orig_len
    return {
        "Hamming(7,4)": ceil_mul(4) // 4 * 7,
        "Hamming": ceil_mul(4) // 4 * 7,
        "DMR Telemetry": ceil_mul(4) // 4 * 7,
        "Hamming(13,9)": ceil_mul(9) // 9 * 13,
        "Hamming(15,11)": ceil_mul(11) // 11 * 15,
        "BPTC(196,96)": 196,
        "Golay(20,8)": 20,
        "DMR Slot Type (Golay(20,8))": 20,
        "Rate 3/4 Trellis": ceil_mul(3) * 4,
        "CRC-CCITT": orig_len + 16,
        "CRC-9": orig_len + 9,
        "CRC-32": orig_len + 32,
        "DMR CACH (CRC-9 over 15b)": 24,
        "DMR Data Block (CRC-CCITT + BPTC196/96)": 196,
        "Convolutional Rate 1/2": (orig_len + 6) * 2,
        "Convolutional Rate 1/3": (orig_len + 6) * 3,
    }[encoding]


def decode_message_fields(bits: str, fields):
    """Inverse of fsk_encoder._build_encoded_bit_string. fields is a list of
    (orig_len, encoding); returns (list of per-field data bits trimmed to
    orig_len, total_corrected, all_ok)."""
    out, corr, ok_all, pos = [], 0, True, 0
    for orig_len, enc in fields:
        elen = encoded_len(enc, orig_len)
        chunk = bits[pos:pos + elen]
        pos += elen
        d, c, ok = decode_field_bits(chunk, enc)
        out.append(d[:orig_len])
        corr += c
        ok_all = ok_all and ok
    return out, corr, ok_all


def decode_field_bits(bits: str, encoding: str) -> Tuple[str, int, bool]:
    if not encoding or encoding == "None":
        return bits, 0, True
    if encoding in ("Hamming(7,4)", "Hamming", "DMR Telemetry"):
        d, c = hamming_7_4_decode(bits)
        return d, max(c, 0), c >= 0
    if encoding == "Hamming(13,9)":
        d, c = hamming_13_9_decode(bits)
        return d, max(c, 0), c >= 0
    if encoding == "Hamming(15,11)":
        d, c = hamming_15_11_decode(bits)
        return d, max(c, 0), c >= 0
    if encoding in ("Golay(20,8)", "DMR Slot Type (Golay(20,8))"):
        d, c = golay_20_8_decode(bits)
        return d, max(c, 0), c >= 0
    if encoding == "BPTC(196,96)":
        d, c = bptc_196_96_decode(bits)
        return d, max(c, 0), c >= 0
    if encoding == "Rate 3/4 Trellis":
        d, m = rate_34_trellis_decode(bits)
        return d, 0, m >= 0
    if encoding == "Convolutional Rate 1/2":
        d, m = convolutional_rate12_decode(bits)
        return d, 0, m >= 0
    if encoding == "Convolutional Rate 1/3":
        d, m = convolutional_rate13_decode(bits)
        return d, 0, m >= 0
    if encoding == "CRC-CCITT":
        d, ok = _crc_check(bits, 16, crc_ccitt)
        return d, 0, ok
    if encoding == "CRC-9":
        d, ok = _crc_check(bits, 9, crc_9)
        return d, 0, ok
    if encoding == "CRC-32":
        d, ok = _crc_check(bits, 32, crc_32)
        return d, 0, ok
    if encoding == "DMR CACH (CRC-9 over 15b)":
        d, ok = _crc_check(bits, 9, crc_9)
        return d, 0, ok
    if encoding == "DMR Data Block (CRC-CCITT + BPTC196/96)":
        block96, c = bptc_196_96_decode(bits)
        data80, ok = _crc_check(block96, 16, crc_ccitt)
        return data80, max(c, 0), ok and c >= 0
    raise ValueError(f"Unknown encoding: {encoding}")
