"""
Tier 1: K=7 convolutional code + soft-decision Viterbi.

Matches the CDM-625A "Sequential" / Viterbi FEC option. Mother code is the CCSDS/NASA
standard rate-1/2 K=7, generators (octal) X=171, Y=133: identical polynomials
to ecc_decoder.convolutional_rate12_encode (that copy lists them 133/171; here X
is the 171 tap first so the DVB-S puncture tables below line up). Punctured to
rate 2/3, 3/4, 5/6, 7/8 with the DVB-S / DVB-DSNG puncturing patterns (ETSI
EN 300 421), the same rate set the CDM-625A Viterbi option exposes.

LLR convention (matches higher_order.soft_llr and demodulator.HigherOrderDemodulator):
llr = log P(bit=1)/P(bit=0), so llr > 0 favours bit 1. BPSK map used in the AWGN
harness is bit b: symbol (2b-1): 0 to -1, 1 to +1, hence channel llr = 2*y/sigma^2.

Zero-tail terminated: m=6 flush bits appended, traceback forced from state 0.
"""
from __future__ import annotations

from typing import List, Sequence, Tuple

import numpy as np

K = 7                       # constraint length
M = K - 1                   # memory (register) bits
NS = 1 << M                 # 64 trellis states
G_X = 0o171                 # 1111001
G_Y = 0o133                 # 1011011

# DVB-S puncturing maps (period p input bits). '1' = transmit, '0' = puncture.
# Row 0 applies to the X (g=171) stream, row 1 to the Y (g=133) stream. Rate
# r = p / (count of transmitted bits). 1/1 patterns give the rate 1/2 mother code.
PUNCTURE = {
    "1/2": ([1], [1]),
    "2/3": ([1, 0], [1, 1]),
    "3/4": ([1, 0, 1], [1, 1, 0]),
    "5/6": ([1, 0, 1, 0, 1], [1, 1, 0, 1, 0]),
    "7/8": ([1, 0, 0, 0, 1, 0, 1], [1, 1, 1, 1, 0, 1, 0]),
}


def _parity(x: int) -> int:
    return bin(x).count("1") & 1


# Precompute the trellis: for each (state, input): (next_state, x, y).
_NEXT = np.empty((NS, 2), dtype=np.int64)
_OUT = np.empty((NS, 2, 2), dtype=np.int64)   # [state][input]: (x, y)
for _s in range(NS):
    for _u in (0, 1):
        _r = (_u << M) | _s                    # 7-bit register, new bit at MSB
        _OUT[_s, _u, 0] = _parity(_r & G_X)
        _OUT[_s, _u, 1] = _parity(_r & G_Y)
        _NEXT[_s, _u] = (_r >> 1) & (NS - 1)


# Predecessor view of the same trellis, for the vectorised add-compare-select
# in decode_soft: ns = (u << (M-1)) | (s >> 1), so ns is entered from
# s = ((ns << 1) & (NS-1)) | b, b in {0,1}, with input u = ns >> (M-1).
_NSI = np.arange(NS)
_PRED = np.stack([((_NSI << 1) & (NS - 1)) | _b for _b in (0, 1)])   # (2, NS)
_PU = _NSI >> (M - 1)
_POX = _OUT[_PRED, _PU, 0]                     # (2, NS) coded x on each entry
_POY = _OUT[_PRED, _PU, 1]
assert np.array_equal(_NEXT[_PRED[0], _PU], _NSI) and np.array_equal(_NEXT[_PRED[1], _PU], _NSI)


def _as_bits(data) -> List[int]:
    if isinstance(data, str):
        return [int(c) for c in data]
    return [int(b) & 1 for b in data]


def encode(data, rate: str = "1/2") -> List[int]:
    """Encode info bits (str '0/1' or iterable) at the given rate. Returns the
    punctured coded bit list. Zero-tail terminated (6 flush bits)."""
    if rate not in PUNCTURE:
        raise ValueError(f"unsupported rate {rate}")
    u = _as_bits(data) + [0] * M
    px, py = PUNCTURE[rate]
    p = len(px)
    s = 0
    out: List[int] = []
    for i, bit in enumerate(u):
        x = _OUT[s, bit, 0]
        y = _OUT[s, bit, 1]
        s = _NEXT[s, bit]
        j = i % p
        if px[j]:
            out.append(int(x))
        if py[j]:
            out.append(int(y))
    return out


def _depuncture(llr: Sequence[float], rate: str, n_steps: int) -> np.ndarray:
    """Re-insert 0.0 LLR (erasures) at punctured positions: full 2*n_steps
    stream the mother-code Viterbi consumes."""
    px, py = PUNCTURE[rate]
    p = len(px)
    full = np.zeros(2 * n_steps, dtype=np.float64)
    k = 0
    llr = np.asarray(llr, dtype=np.float64)
    for i in range(n_steps):
        j = i % p
        if px[j]:
            full[2 * i] = llr[k]; k += 1
        if py[j]:
            full[2 * i + 1] = llr[k]; k += 1
    return full


def decode_soft(llr: Sequence[float], rate: str = "1/2",
                n_info: int | None = None, terminated: bool = True) -> List[int]:
    """Soft-decision Viterbi. `llr` are per-coded-bit LLRs (llr > 0 means bit 1),
    length matches encode() output. Returns decoded info bits (tail stripped
    when `terminated`).

    Path metric maximises sum(c_i * llr_i): for a hypothesised coded bit c in
    {0,1}, log P(c) is proportional to c*llr up to a c-independent constant, so
    accumulating c*llr and keeping the max path is the ML soft rule. Punctured
    positions carry llr=0 and contribute nothing.

    `terminated` (default): the block is zero-tail terminated, so the best final
    state is 0 and the M tail bits are stripped. Set False to decode an arbitrary
    (unterminated) window: traceback starts from the max-metric state and no
    tail is stripped; the early info bits are reliable, which is enough to read a
    length field before re-decoding the exact block.
    """
    llr = np.asarray(llr, dtype=np.float64)
    px, py = PUNCTURE[rate]
    p = len(px)
    # recover n_steps by walking the puncture pattern until transmitted bits
    # match len(llr).
    n_coded = len(llr)
    # count how many steps produce n_coded transmitted bits
    n_steps = 0
    acc = 0
    while acc < n_coded:
        j = n_steps % p
        acc += px[j] + py[j]
        n_steps += 1
    if acc != n_coded:
        raise ValueError(f"coded length {n_coded} not consistent with rate {rate}")

    full = _depuncture(llr, rate, n_steps)

    NEG = -1e18
    pm = np.full(NS, NEG, dtype=np.float64)
    pm[0] = 0.0
    # survivor predecessor state into each state at each step
    back = np.zeros((n_steps, NS), dtype=np.int64)

    # Add-compare-select over all 64 states at once (a Python loop over
    # states x inputs costs ~90 us/step, 2-3 s per coded 1.1 s window,
    # FEC-07). Gives the same bits as that loop: same metric arithmetic order,
    # the lower predecessor wins ties (states visited ascending, replaced only
    # on a strictly larger metric), unreachable states stay NEG with
    # survivor 0.
    p0, p1 = _PRED
    ox0, ox1 = _POX
    oy0, oy1 = _POY
    for t in range(n_steps):
        lx = full[2 * t]
        ly = full[2 * t + 1]
        b0 = pm[p0]
        b1 = pm[p1]
        # branch metric = x*lx + y*ly  (c*llr accumulation)
        m0 = b0 + ox0 * lx + oy0 * ly
        m1 = b1 + ox1 * lx + oy1 * ly
        dead0 = b0 == NEG
        m0[dead0] = NEG
        m1[b1 == NEG] = NEG
        take1 = m1 > m0
        pm = np.where(take1, m1, m0)
        back[t] = np.where(take1, p1, np.where(dead0, 0, p0))

    # Info bit into state ns is its top memory bit (u = ns >> (M-1)); the
    # survivor gives the predecessor. Terminated blocks end at state 0; an
    # unterminated window tracebacks from the surviving max-metric state.
    s = 0 if terminated else int(np.argmax(pm))
    dec = [0] * n_steps
    for t in range(n_steps - 1, -1, -1):
        dec[t] = int(s >> (M - 1))
        s = int(back[t, s])
    info = (dec[:-M] if M else dec) if terminated else dec
    if n_info is not None:
        info = info[:n_info]
    return info


def rate_of(name: str) -> float:
    a, b = name.split("/")
    return int(a) / int(b)
