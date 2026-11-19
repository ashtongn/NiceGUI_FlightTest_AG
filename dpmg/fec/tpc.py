"""
Tier 4: Turbo Product Codes (2D block product) + iterative Chase-Pyndiah SISO.

Matches the CDM-625A "Turbo Product Code" FEC option.

Component code: extended Hamming (2^m, 2^m-1-m), SEC-DED, d_min=4 (an overall
parity bit appended to Hamming(2^m-1, 2^m-1-m)). A 2D product of two extended
Hamming codes has d_min = 4*4 = 16 and rate (k/n)^2. Configs exposed:
  eH(8,4)^2    k=16   n=64    rate 0.250   (fast/small)
  eH(16,11)^2  k=121  n=256   rate 0.473
  eH(32,26)^2  k=676  n=1024  rate 0.660

Systematic 2D encode: k_r x k_c info block: encode each info row with the row
code: encode all columns (info + row-parity cols) with the column code. Info
stays in the top-left k_r x k_c block (both component codes systematic).

Hard component decode: syndrome s = H r^T over the extended code. A precomputed
coset-leader table maps every syndrome to its min-weight error (true nearest-
codeword / ML-on-BSC). weight <= 1 is corrected (SEC); weight 2 is flagged
uncorrectable (DED). d_min=4 guarantees single- and double-error syndromes never
collide, so detection is exact.

Chase-Pyndiah SISO (per line):
  * Chase-II: flip the p least-reliable bits (all 2^p subsets), hard-decode each
    perturbed word to the nearest codeword: candidate set.
  * ML pick: decision D = candidate maximising correlation sum_i r_i*(2c_i-1)
    (== min Euclidean |r - d|^2 with d in {-1,+1}).
  * Pyndiah soft out per bit j: competitor C = candidate with the opposite bit j
    and best correlation; soft_j = dsign_j*(corrD - corrC)/2. No competitor
    (decision very reliable): add confidence: soft_j = r_in_j + dsign_j*beta,
    i.e. extrinsic = dsign_j*beta.
  * extrinsic w = soft - r_in; next half-iter input = R + alpha*W (alpha ramps).
Rows and columns alternate; several full iterations. The channel matrix is
normalised to unit average |LLR| first so the alpha/beta schedule (calibrated
for ~unit-scale inputs) holds across SNR; sign-based decisions are scale-free.

LLR convention (matches conv/rs/channel): llr = log P(1)/P(0), llr > 0 means bit 1;
BPSK map bit b: 2b-1. channel.awgn_llr already returns this sign, consumed
directly as the Chase soft input (correlation with +-1 symbols is sign-correct).
"""
from __future__ import annotations

from itertools import combinations
from typing import Dict, List, Sequence, Tuple

import numpy as np

# alpha (extrinsic feedback weight) and beta (soft-output fallback) ramp across
# half-iterations, per Pyndiah. Indexed by half-iteration, clamped to the tail.
ALPHA_SCHED = [0.2, 0.3, 0.5, 0.7, 0.9, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0]
BETA_SCHED = [0.2, 0.4, 0.6, 0.8, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0]


def _sched(tab: List[float], h: int) -> float:
    return tab[h] if h < len(tab) else tab[-1]


class ExtHamming:
    """Systematic extended Hamming (2^m, 2^m-1-m). G=[I|P], H=[P^T|I]."""

    def __init__(self, m: int):
        if m < 3:
            raise ValueError("extended Hamming needs m>=3")
        self.m = m
        self.n = 1 << m
        self.k = (1 << m) - m - 1
        self.r = self.n - self.k          # m+1 parity bits

        # base Hamming A (m x k): columns are the nonzero m-bit vectors of
        # weight>=2 (all nonzero except the m unit vectors). Info cols.
        units = {1 << i for i in range(m)}
        info_cols = [v for v in range(1, (1 << m)) if v not in units]
        assert len(info_cols) == self.k
        A = np.zeros((m, self.k), dtype=np.int8)
        for j, v in enumerate(info_cols):
            for i in range(m):
                A[i, j] = (v >> i) & 1

        # base Hamming G_ham = [I_k | A^T]  (k x n0), n0 = k+m
        Ik = np.eye(self.k, dtype=np.int8)
        G_ham = np.concatenate([Ik, A.T], axis=1)          # k x (k+m)
        # extended overall-parity column: row parity of G_ham
        s_col = (G_ham.sum(axis=1) & 1).astype(np.int8).reshape(-1, 1)
        G = np.concatenate([G_ham, s_col], axis=1)         # k x n
        self.G = G
        self.P = G[:, self.k:].copy()                      # k x r
        # H = [P^T | I_r]  (r x n)
        self.H = np.concatenate([self.P.T, np.eye(self.r, dtype=np.int8)], axis=1)
        self.Ht = self.H.T.copy()                          # n x r
        self._w = (1 << np.arange(self.r))                 # syndrome bit weights

        # column-of-H integer (single-error syndrome for each position)
        self._colint = (self.Ht * self._w).sum(axis=1).astype(np.int64)  # (n,)

        self._build_coset_leaders()

    # construction of the coset-leader (nearest-codeword) table
    def _build_coset_leaders(self) -> None:
        size = 1 << self.r
        leader = np.full(size, -1, dtype=np.int64)         # bitmask of error
        lwt = np.zeros(size, dtype=np.int8)                # weight of leader
        leader[0] = 0
        lwt[0] = 0
        filled = 1
        w = 1
        n = self.n
        col = self._colint
        while filled < size and w <= n:
            for combo in combinations(range(n), w):
                syn = 0
                mask = 0
                for j in combo:
                    syn ^= int(col[j])
                    mask |= (1 << j)
                if leader[syn] < 0:
                    leader[syn] = mask
                    lwt[syn] = w
                    filled += 1
            w += 1
        self._leader = leader
        self._lwt = lwt
        self._bitpos = np.arange(n)

    # encode / hard decode
    def encode(self, u: Sequence[int]) -> np.ndarray:
        u = np.asarray(u, dtype=np.int8).reshape(-1)
        if len(u) != self.k:
            raise ValueError(f"encode expects {self.k} bits, got {len(u)}")
        par = (u @ self.P) & 1
        return np.concatenate([u, par]).astype(np.int8)

    def _syn_int(self, Z: np.ndarray) -> np.ndarray:
        """Z: (...,n) hard bits: syndrome integers (...,)."""
        S = (Z @ self.Ht) & 1
        return (S * self._w).sum(axis=-1).astype(np.int64)

    def nearest(self, Z: np.ndarray) -> np.ndarray:
        """Min-weight (nearest-codeword) hard decode for a batch (T,n)->(T,n)."""
        Z = np.atleast_2d(Z).astype(np.int8)
        syn = self._syn_int(Z)
        err = self._leader[syn]
        errbits = ((err[:, None] >> self._bitpos[None, :]) & 1).astype(np.int8)
        return (Z ^ errbits).astype(np.int8)

    def decode_hard(self, r: Sequence[int]) -> Tuple[np.ndarray, int, bool]:
        """SEC-DED. Returns (codeword-info k bits, n_corrected, ok). ok=False on a
        detected double error (weight-2 syndrome coset)."""
        z = np.asarray(r, dtype=np.int8).reshape(1, -1)
        syn = int(self._syn_int(z)[0])
        w = int(self._lwt[syn])
        if w == 0:
            return z[0, :self.k].copy(), 0, True
        if w == 1:
            c = self.nearest(z)[0]
            return c[:self.k].copy(), 1, True
        # w>=2 : double (or more) error detected, uncorrectable
        return z[0, :self.k].copy(), 0, False


# Chase SISO
def _flip_patterns(p: int) -> np.ndarray:
    T = 1 << p
    bits = ((np.arange(T)[:, None] >> np.arange(p)[None, :]) & 1)
    return bits.astype(np.int8)                            # (T,p)


def _chase_line(r_in: np.ndarray, comp: ExtHamming, p: int,
                beta: float) -> np.ndarray:
    """Chase-Pyndiah soft output for one line. r_in: (n,) LLR. Returns (n,)
    soft output in the same LLR-ish units (sign > 0 means bit 1)."""
    n = comp.n
    y = (r_in > 0).astype(np.int8)
    rel = np.abs(r_in)
    p = min(p, n)
    least = np.argsort(rel)[:p]                            # least-reliable idx
    pats = _flip_patterns(p)                               # (T,p)
    Z = np.tile(y, (pats.shape[0], 1))
    Z[:, least] ^= pats
    C = comp.nearest(Z)                                    # (T,n) candidate cws
    Dsym = (2 * C - 1).astype(np.float64)                  # +-1
    corr = Dsym @ r_in                                     # (T,) correlation
    best = int(np.argmax(corr))
    Dbits = C[best]
    dsign = (2 * Dbits - 1).astype(np.float64)
    corrD = corr[best]
    soft = np.empty(n, dtype=np.float64)
    for j in range(n):
        opp = C[:, j] != Dbits[j]
        if opp.any():
            corrC = corr[opp].max()
            soft[j] = dsign[j] * (corrD - corrC) * 0.5
        else:
            # no competitor means the decision is very reliable: add beta confidence
            # (extrinsic dsign*beta). Replacing r_in with a small constant here
            # would invert the extrinsic sign and corrupt converged bits.
            soft[j] = r_in[j] + dsign[j] * beta
    return soft


class TPC:
    """2D product of two extended Hamming component codes."""

    def __init__(self, comp_row: ExtHamming, comp_col: ExtHamming, name: str):
        self.name = name
        self.rc = comp_row                # decodes each row   (length n_c)
        self.cc = comp_col                # decodes each column(length n_r)
        self.kr, self.kc = comp_col.k, comp_row.k
        self.nr, self.nc = comp_col.n, comp_row.n
        self.k = self.kr * self.kc
        self.n = self.nr * self.nc
        self.rate = self.k / self.n

    # encode
    def encode(self, bits: Sequence[int]) -> np.ndarray:
        U = np.asarray(bits, dtype=np.int8).reshape(self.kr, self.kc)
        M1 = np.zeros((self.kr, self.nc), dtype=np.int8)
        for i in range(self.kr):
            M1[i] = self.rc.encode(U[i])
        M = np.zeros((self.nr, self.nc), dtype=np.int8)
        for j in range(self.nc):
            M[:, j] = self.cc.encode(M1[:, j])
        return M.reshape(-1)

    # Chase-Pyndiah iterative decode
    def decode(self, llr: Sequence[float], n_iter: int = 4, p: int = 4,
               return_matrix: bool = False):
        R = np.asarray(llr, dtype=np.float64).reshape(self.nr, self.nc)
        # normalise to unit average reliability so the alpha/beta schedule
        # (tuned for ~unit-scale inputs) is calibrated; decisions are sign-based
        # so the global scale is irrelevant to the result.
        scale = np.mean(np.abs(R))
        if scale > 0:
            R = R / scale
        W = np.zeros_like(R)
        last = R.copy()
        half = 0
        for _ in range(n_iter):
            # rows
            alpha = _sched(ALPHA_SCHED, half)
            beta = _sched(BETA_SCHED, half)
            Rin = R + alpha * W
            out = np.empty_like(R)
            for i in range(self.nr):
                out[i] = _chase_line(Rin[i], self.rc, p, beta)
            W = out - Rin
            last = out
            half += 1
            # columns
            alpha = _sched(ALPHA_SCHED, half)
            beta = _sched(BETA_SCHED, half)
            Rin = R + alpha * W
            out = np.empty_like(R)
            for j in range(self.nc):
                out[:, j] = _chase_line(Rin[:, j], self.cc, p, beta)
            W = out - Rin
            last = out
            half += 1
        hard = (last > 0).astype(np.int8)
        if return_matrix:
            return hard
        return hard[:self.kr, :self.kc].reshape(-1)


# registry
_M = {3: ExtHamming(3), 4: ExtHamming(4), 5: ExtHamming(5)}

_CONFIGS: Dict[str, Tuple[int, int]] = {
    "eH(8,4)^2": (3, 3),
    "eH(16,11)^2": (4, 4),
    "eH(32,26)^2": (5, 5),
}


def get_tpc(name: str) -> TPC:
    if name not in _CONFIGS:
        raise ValueError(f"unknown TPC config {name}; have {list(_CONFIGS)}")
    mr, mc = _CONFIGS[name]
    return TPC(_M[mc], _M[mr], name)


def configs() -> List[str]:
    return list(_CONFIGS)
