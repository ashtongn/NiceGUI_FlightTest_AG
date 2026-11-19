"""
Shared AWGN BPSK channel + LLR helper for the FEC BER simulations.

BPSK map: bit b: symbol (2b-1), i.e. 0 to -1, 1 to +1. This is the sign
convention that makes the channel LLR (2*y/sigma^2) positive for bit 1,
matching higher_order.soft_llr / demodulator soft output.

Es/N0 vs Eb/N0: symbol energy Es=1 here (unit BPSK). For a code of rate R the
info-bit energy Eb = Es / R over the coded stream, so coding gain is reported
against Eb/N0 (energy per information bit): the fair yardstick.
"""
from __future__ import annotations

import numpy as np


def bpsk_mod(bits) -> np.ndarray:
    b = np.asarray([int(x) & 1 for x in bits], dtype=np.float64)
    return 2.0 * b - 1.0


def awgn_llr(bits, ebn0_db: float, rate: float = 1.0,
             rng: np.random.Generator | None = None):
    """Modulate bits, pass through AWGN at the given info-bit Eb/N0, return
    (rx_llr, hard_bits). `rate` R scales coded-bit energy Es = R*Eb.

    N0 = 1. Es = R * Eb. sigma^2 = N0/2 = 1/2 in coded-symbol units after
    normalising Es; the noise is scaled instead: sigma^2 = 1/(2*R*ebn0_lin).
    """
    if rng is None:
        rng = np.random.default_rng()
    x = bpsk_mod(bits)
    ebn0 = 10.0 ** (ebn0_db / 10.0)
    esn0 = rate * ebn0                      # coded-symbol Es/N0
    sigma2 = 1.0 / (2.0 * esn0)
    sigma = np.sqrt(sigma2)
    y = x + sigma * rng.standard_normal(len(x))
    llr = 2.0 * y / sigma2                  # > 0 favours bit 1
    hard = (y > 0).astype(np.int64)
    return llr, hard


def qfunc(x: float) -> float:
    from math import erfc, sqrt
    return 0.5 * erfc(x / sqrt(2.0))


def uncoded_ber(ebn0_db: float) -> float:
    """Theoretical uncoded BPSK BER = Q(sqrt(2 Eb/N0))."""
    ebn0 = 10.0 ** (ebn0_db / 10.0)
    return qfunc(np.sqrt(2.0 * ebn0))


def ber(a, b) -> float:
    a = np.asarray([int(x) & 1 for x in a], dtype=np.int64)
    b = np.asarray([int(x) & 1 for x in b], dtype=np.int64)
    n = min(len(a), len(b))
    if n == 0:
        return 0.0
    return float(np.count_nonzero(a[:n] ^ b[:n])) / n
