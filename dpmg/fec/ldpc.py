"""
Tier 3: LDPC, normalized min-sum belief propagation soft decoder.

The CDM-625A LDPC option uses Comtech VersaFEC block codes. Those H-matrices
are Comtech proprietary and not published, so they are not reproduced here and
this module is not bit-compatible with a VersaFEC modem.

H-matrix construction used here (fully defined, reproducible):
  Regular (j,k) QC-LDPC "array codes" (Fan, "Array codes as LDPC codes", 2000).
  H is a j x k array of pxp circulant permutation blocks; the block at
  (row i, col l) is P^(i*l mod p), where P is the pxp cyclic shift permutation
  (P[a,(a+1)%p]=1) and p is prime. Variable degree = j, check degree = k, so
  the design rate is 1 - j/k. Because p is prime and (i1-i2)(l1-l2) !=0 mod p
  for distinct rows/cols, the Tanner graph has no length-4 cycles: girth >= 6.
  The true dimension K = N - rank_GF2(H) is measured (array codes are mildly
  rank-deficient), so the advertised rate and the exact code.rate can differ by
  a few tenths of a percent: code.rate is the exact value tests/channel use.

Systematic encoding: H is reduced to RREF over GF(2) with column pivoting
(_gf2_rref). The rank pivot columns carry parity, the remaining K columns carry
info, giving c with H c = 0 (GF2) and the info bits recoverable verbatim from
the info columns: a systematic round trip.

LLR convention (matches channel.awgn_llr / conv.py / rs.py):
  llr = log P(bit=1)/P(bit=0), llr > 0 favours bit 1. BPSK map b to (2b-1).
  The decoder converts to the textbook L0=log P(0)/P(1)=-llr domain internally
  (unambiguous min-sum for even-parity checks of any degree), then reports bits
  back in the package convention.
"""
from __future__ import annotations

from typing import Dict, List, Tuple

import numpy as np


# H construction
def _array_qc_H(j: int, k: int, p: int) -> np.ndarray:
    """(j,k) array QC-LDPC parity-check matrix. Block (i,l)=P^(i*l mod p),
    P the pxp cyclic shift. M=j*p checks, N=k*p bits. Girth >= 6 for prime p."""
    M, N = j * p, k * p
    H = np.zeros((M, N), dtype=np.uint8)
    for i in range(j):
        for l in range(k):
            s = (i * l) % p
            for a in range(p):
                H[i * p + a, l * p + ((a + s) % p)] = 1
    return H


def _has_4cycle(H: np.ndarray) -> bool:
    """True if the Tanner graph of H has a length-4 cycle (girth 4)."""
    # two columns sharing >=2 common check rows form a 4-cycle.
    G = (H.astype(np.int64).T @ H.astype(np.int64))   # N x N column overlaps
    np.fill_diagonal(G, 0)
    return bool(np.any(G >= 2))


# GF(2) systematic form
def _gf2_rref(H: np.ndarray) -> Tuple[np.ndarray, List[int]]:
    """Reduced row-echelon form of H over GF(2) with column pivoting. Returns
    (rref, pivot_cols). Non-pivot columns are the info coordinates."""
    A = H.copy().astype(np.uint8)
    M, N = A.shape
    pivots: List[int] = []
    r = 0
    for c in range(N):
        if r >= M:
            break
        rows = np.nonzero(A[r:, c])[0]
        if rows.size == 0:
            continue
        pr = r + int(rows[0])
        if pr != r:
            A[[r, pr]] = A[[pr, r]]
        # eliminate this column from every other row
        col = A[:, c].copy()
        col[r] = 0
        hit = np.nonzero(col)[0]
        if hit.size:
            A[hit] ^= A[r]
        pivots.append(c)
        r += 1
    return A, pivots


# code type
class LDPCCode:
    """A QC-LDPC code + normalized min-sum decoder. Attributes: name, N, K,
    rate, H (dense uint8). encode(u)->codeword; decode(llr)->info bits."""

    def __init__(self, name: str, j: int, k: int, p: int,
                 max_iter: int = 50, alpha: float = 0.85):
        self.name = name
        self.j, self.k, self.p = j, k, p
        self.max_iter = max_iter
        self.alpha = alpha
        self.H = _array_qc_H(j, k, p)
        self.M, self.N = self.H.shape

        rref, pivots = _gf2_rref(self.H)
        self._pivot_cols = np.asarray(pivots, dtype=np.int64)
        rank = len(pivots)
        info_mask = np.ones(self.N, dtype=bool)
        info_mask[self._pivot_cols] = False
        self._info_cols = np.nonzero(info_mask)[0]
        self.K = int(self._info_cols.size)
        self.rate = self.K / self.N
        # parity gen: parity[pivot i] = sum_j Pmat[i,j]*info[j]  (RREF row i)
        self._Pmat = rref[:rank][:, self._info_cols].astype(np.uint8)

        # decoder graph (check-major edge ordering; regular so reshape to M x k)
        # each check row has exactly k ones.
        cols = [np.nonzero(self.H[m])[0] for m in range(self.M)]
        self._chk_var = np.asarray(cols, dtype=np.int64)          # M x dc
        self.dc = self._chk_var.shape[1]
        self._var_idx = self._chk_var.reshape(-1)                 # E
        self._col_ar = np.arange(self.dc)[None, :]                # 1 x dc

    # encode
    def encode(self, data) -> List[int]:
        u = np.asarray([int(b) & 1 for b in data], dtype=np.uint8)
        if u.size != self.K:
            raise ValueError(f"{self.name} encode expects {self.K} bits, got {u.size}")
        c = np.zeros(self.N, dtype=np.uint8)
        c[self._info_cols] = u
        if self._Pmat.size:
            parity = (self._Pmat @ u) & 1
            c[self._pivot_cols] = parity
        return c.astype(np.int64).tolist()

    def info_bits(self, codeword) -> List[int]:
        """Extract the K systematic info bits from a full codeword."""
        c = np.asarray([int(b) & 1 for b in codeword], dtype=np.int64)
        return c[self._info_cols].tolist()

    # decode
    def _bp(self, llr, max_iter: int, trace: bool):
        """Core normalized min-sum. Returns (hard_bits N, iters, unsat, trace)."""
        llr = np.asarray(llr, dtype=np.float64)
        if llr.size != self.N:
            raise ValueError(f"{self.name} decode expects {self.N} LLRs, got {llr.size}")
        L0 = -llr                                   # log P(0)/P(1) domain
        var_idx = self._var_idx
        chk_var = self._chk_var
        M, dc = self.M, self.dc
        col_ar = self._col_ar
        alpha = self.alpha

        Mvc = L0[var_idx].reshape(M, dc).copy()     # variable to check msgs
        unsat_trace: List[int] = []
        it = 0
        hard = (L0 < 0).astype(np.int64)
        unsat = 0
        for it in range(1, max_iter + 1):
            # check update: normalized min-sum, min-excluding-self
            A = np.abs(Mvc)
            sgn = np.where(Mvc >= 0, 1.0, -1.0)
            total_sign = np.prod(sgn, axis=1)
            order = np.argsort(A, axis=1)           # ascending magnitudes
            rows = np.arange(M)
            amin = order[:, 0]
            min1 = A[rows, amin]
            min2 = A[rows, order[:, 1]]
            mag_excl = np.where(col_ar == amin[:, None], min2[:, None], min1[:, None])
            sign_excl = total_sign[:, None] * sgn
            Mcv = alpha * sign_excl * mag_excl       # M x dc

            # posterior + hard decision + syndrome (early stop)
            Sv = np.bincount(var_idx, weights=Mcv.reshape(-1), minlength=self.N)
            T = L0 + Sv
            hard = (T < 0).astype(np.int64)          # T < 0 means bit 1
            par = np.bitwise_xor.reduce(hard[chk_var], axis=1)
            unsat = int(np.count_nonzero(par))
            if trace:
                unsat_trace.append(unsat)
            if unsat == 0:
                break
            # variable update (exclude own check)
            Mvc = (L0[var_idx] + Sv[var_idx] - Mcv.reshape(-1)).reshape(M, dc)
        return hard, it, unsat, unsat_trace

    def decode(self, llr, max_iter: int | None = None) -> List[int]:
        """Decode per-bit LLRs (llr > 0 means bit 1). Returns K systematic info bits."""
        mi = self.max_iter if max_iter is None else max_iter
        hard, _, _, _ = self._bp(llr, mi, trace=False)
        return hard[self._info_cols].tolist()

    def decode_full(self, llr, max_iter: int | None = None):
        """Decode: (info_bits, full_codeword, iters, unsatisfied_checks)."""
        mi = self.max_iter if max_iter is None else max_iter
        hard, it, unsat, _ = self._bp(llr, mi, trace=False)
        return hard[self._info_cols].tolist(), hard.tolist(), it, unsat

    def decode_trace(self, llr, max_iter: int | None = None):
        """Decode: (info_bits, unsatisfied-checks-per-iteration list)."""
        mi = self.max_iter if max_iter is None else max_iter
        hard, _, _, tr = self._bp(llr, mi, trace=True)
        return hard[self._info_cols].tolist(), tr

    def parity_ok(self, codeword) -> bool:
        c = np.asarray([int(b) & 1 for b in codeword], dtype=np.uint8)
        return not np.any((self.H @ c) & 1)


# registry
# (j,k,p): design rate 1-j/k, N=k*p. p prime: girth>=6. Sizes ~640 bits so
# the min-sum decoder runs a full BER sweep in well under a minute.
_SPEC: Dict[str, Tuple[int, int, int]] = {
    "qc-r12": (3, 6, 109),      # rate ~1/2, N=654
    "qc-r23": (3, 9, 73),       # rate ~2/3, N=657
    "qc-r34": (3, 12, 53),      # rate ~3/4, N=636
}

_CACHE: Dict[str, LDPCCode] = {}


def get_ldpc(name: str) -> LDPCCode:
    if name not in _SPEC:
        raise ValueError(f"unknown ldpc code {name}; have {list(_SPEC)}")
    if name not in _CACHE:
        j, k, p = _SPEC[name]
        _CACHE[name] = LDPCCode(name, j, k, p)
    return _CACHE[name]


def names() -> List[str]:
    return list(_SPEC)
