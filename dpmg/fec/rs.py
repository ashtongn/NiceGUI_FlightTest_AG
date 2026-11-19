"""
Tier 2: Reed-Solomon RS(255,223) over GF(2^8) + block interleaver +
RS-over-Viterbi concatenation (the classic CCSDS/satcom stack).

Matches the CDM-625A "Reed-Solomon" and "Concatenated Reed-Solomon / Viterbi"
options.

RS(255,223): n=255, k=223, 2t=32 parity symbols, corrects t=16 symbol errors.
Field GF(2^8) with primitive polynomial 0x11D (x^8+x^4+x^3+x^2+1). Generator
g(x)=prod_{i=1}^{32}(x - alpha^i), first-consecutive-root FCR=1, alpha=0x02.
This is the conventional (Berlekamp) representation, not the CCSDS dual-basis
(0x187) variant, so a CCSDS RS receiver is not bit-compatible with it.

Decoder: syndromes, Berlekamp-Massey (error locator), Chien search (roots give
error positions), Forney (error magnitudes). Detects/flags uncorrectable
(> t errors) instead of silently mis-correcting.
"""
from __future__ import annotations

from typing import List, Sequence, Tuple

import numpy as np

PRIM = 0x11D
ALPHA = 0x02
N = 255
K = 223
NROOTS = N - K          # 32
FCR = 1                 # first consecutive root exponent

# GF(2^8) log/antilog tables.
_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= PRIM
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def gmul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def gdiv(a: int, b: int) -> int:
    if b == 0:
        raise ZeroDivisionError
    if a == 0:
        return 0
    return _EXP[(_LOG[a] - _LOG[b]) % 255]


def gpow(a: int, e: int) -> int:
    if a == 0:
        return 0
    return _EXP[(_LOG[a] * e) % 255]


def ginv(a: int) -> int:
    return _EXP[(255 - _LOG[a]) % 255]


def _gen_poly(nroots: int) -> List[int]:
    g = [1]
    for i in range(nroots):
        root = gpow(ALPHA, FCR + i)
        # multiply g(x) by (x - root)
        ng = [0] * (len(g) + 1)
        for j in range(len(g)):
            ng[j] ^= gmul(g[j], root)
            ng[j + 1] ^= g[j]
        g = ng
    return g            # length nroots+1, highest degree last


_GENPOLY = _gen_poly(NROOTS)


# genpoly high-degree first for schoolbook division; monic leading coeff = 1.
_GENHI = _GENPOLY[::-1]


def encode(data: Sequence[int]) -> List[int]:
    """Systematic RS(255,223). `data` = 223 symbols (0..255). Returns 255
    symbols = data || 32 parity. c(x) = data(x)*x^32 mod g(x)."""
    data = list(int(d) & 0xFF for d in data)
    if len(data) != K:
        raise ValueError(f"RS encode expects {K} symbols, got {len(data)}")
    rem = data + [0] * NROOTS
    for i in range(K):
        coef = rem[i]
        if coef != 0:
            for j in range(1, NROOTS + 1):
                if _GENHI[j]:
                    rem[i + j] ^= gmul(_GENHI[j], coef)
    parity = rem[K:]
    return data + parity


def _syndromes(rx: Sequence[int]) -> List[int]:
    syn = [0] * NROOTS
    for i in range(NROOTS):
        s = 0
        xi = gpow(ALPHA, FCR + i)
        for c in rx:                       # Horner
            s = gmul(s, xi) ^ c
        syn[i] = s
    return syn


def _berlekamp_massey(syn: List[int]) -> List[int]:
    """Return error-locator polynomial Lambda(x), low-degree first."""
    L = [1]
    B = [1]
    m = 1
    b = 1
    n = 0
    Ln = 0
    for n in range(NROOTS):
        # discrepancy
        delta = syn[n]
        for i in range(1, Ln + 1):
            delta ^= gmul(L[i], syn[n - i])
        if delta == 0:
            m += 1
        elif 2 * Ln <= n:
            T = list(L)
            coef = gdiv(delta, b)
            # L = L - coef * x^m * B
            xB = [0] * m + [gmul(coef, bb) for bb in B]
            L = _poly_add(L, xB)
            Ln = n + 1 - Ln
            B = T
            b = delta
            m = 1
        else:
            coef = gdiv(delta, b)
            xB = [0] * m + [gmul(coef, bb) for bb in B]
            L = _poly_add(L, xB)
            m += 1
    return L


def _poly_add(a: List[int], b: List[int]) -> List[int]:
    if len(a) < len(b):
        a, b = b, a
    out = list(a)
    for i in range(len(b)):
        out[i] ^= b[i]
    return out


def _chien(locator: List[int]) -> List[int]:
    """Roots of locator: error positions (0-based from the first rx symbol)."""
    deg = len(locator) - 1
    positions = []
    for i in range(N):
        # evaluate locator at alpha^-i
        x = gpow(ALPHA, (255 - i) % 255)
        acc = 0
        xp = 1
        for coef in locator:
            acc ^= gmul(coef, xp)
            xp = gmul(xp, x)
        if acc == 0:
            # root alpha^-i: error at position i (locator uses X_l=alpha^pos)
            positions.append(i)
    return positions


def _forney(syn: List[int], locator: List[int], positions: List[int]) -> List[int]:
    """Error magnitudes at each position (aligned with `positions`)."""
    # Omega(x) = S(x)*Lambda(x) mod x^(2t)
    S = list(syn)
    omega = [0] * NROOTS
    for i in range(NROOTS):
        s = 0
        for j in range(len(locator)):
            if i - j >= 0:
                s ^= gmul(locator[j], S[i - j])
        omega[i] = s
    omega = omega[:NROOTS]
    # Lambda'(x): formal derivative (odd-index terms survive in GF(2))
    lam_prime = [0] * (len(locator) - 1)
    for i in range(1, len(locator)):
        if i % 2 == 1:
            lam_prime[i - 1] = locator[i]
    mags = []
    for pos in positions:
        Xi = gpow(ALPHA, pos)          # error locator value
        Xi_inv = ginv(Xi)
        # eval omega(Xi_inv)
        o = 0
        xp = 1
        for c in omega:
            o ^= gmul(c, xp)
            xp = gmul(xp, Xi_inv)
        # eval lam_prime(Xi_inv)
        lp = 0
        xp = 1
        for c in lam_prime:
            lp ^= gmul(c, xp)
            xp = gmul(xp, Xi_inv)
        if lp == 0:
            mags.append(None)
            continue
        # Forney: e = Xi^(1-FCR) * omega/lam' ; FCR=1: Xi^0=1
        mag = gmul(gdiv(o, lp), gpow(Xi, 1 - FCR))
        mags.append(mag)
    return mags


def decode(rx: Sequence[int]) -> Tuple[List[int], int, bool]:
    """Decode a 255-symbol RS word. Returns (223 info symbols, n_errs, ok).
    ok=False when uncorrectable (> t errors detected)."""
    rx = list(int(c) & 0xFF for c in rx)
    if len(rx) != N:
        raise ValueError(f"RS decode expects {N} symbols, got {len(rx)}")
    syn = _syndromes(rx)
    if not any(syn):
        return rx[:K], 0, True
    locator = _berlekamp_massey(syn)
    nerr = len(locator) - 1
    positions = _chien(locator)
    if len(positions) != nerr or nerr == 0:
        return rx[:K], 0, False        # locator degree != #roots: uncorrectable
    mags = _forney(syn, locator, positions)
    corr = list(rx)
    fixed = 0
    for pos, mag in zip(positions, mags):
        if mag is None:
            return rx[:K], 0, False
        # _chien returns i for the root alpha^-i, i.e. X = alpha^i, with i counted
        # from the highest power. rx is stored [c_{n-1}..c_0] (rx[0] is the highest
        # degree), so the symbol index is N-1-i.
        idx = N - 1 - pos
        if 0 <= idx < N:
            corr[idx] ^= mag
            fixed += 1
    # re-check syndromes
    if any(_syndromes(corr)):
        return rx[:K], 0, False
    return corr[:K], fixed, True


# interleave
def block_interleave(symbols: Sequence[int], depth: int) -> List[int]:
    """Write `depth` rows column-by-column (classic block interleaver). Pads to
    a multiple of depth with zeros; de-interleave undoes the transpose."""
    s = list(symbols)
    ncol = (len(s) + depth - 1) // depth
    pad = ncol * depth - len(s)
    s = s + [0] * pad
    arr = np.array(s, dtype=np.int64).reshape(depth, ncol)
    return arr.T.reshape(-1).tolist()


def block_deinterleave(symbols: Sequence[int], depth: int, orig_len: int) -> List[int]:
    s = list(symbols)
    ncol = len(s) // depth
    arr = np.array(s, dtype=np.int64).reshape(ncol, depth)
    return arr.T.reshape(-1).tolist()[:orig_len]


# RS x Viterbi concatenation
def bytes_to_bits(symbols: Sequence[int]) -> List[int]:
    out: List[int] = []
    for s in symbols:
        for k in range(7, -1, -1):
            out.append((int(s) >> k) & 1)
    return out


def bits_to_bytes(bits: Sequence[int]) -> List[int]:
    out: List[int] = []
    for i in range(0, len(bits) - 7, 8):
        v = 0
        for k in range(8):
            v = (v << 1) | (int(bits[i + k]) & 1)
        out.append(v)
    return out
