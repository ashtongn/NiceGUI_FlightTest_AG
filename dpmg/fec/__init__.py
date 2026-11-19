"""
dpmg.fec: CDM-625A-parity forward error correction suite.

Tiers (see docs/FEC.md):
  1 conv  - K=7 convolutional + soft Viterbi, punctured 1/2..7/8
  2 rs    - RS(255,223) + block interleaver + RS x Viterbi concatenation
  3 ldpc  - LDPC min-sum belief propagation (DVB-S2 / custom rates)
  4 tpc   - Turbo product codes (extended Hamming, Chase-Pyndiah)
  5 acm   - adaptive coding & modulation controller

All soft decoders consume LLRs with the sign convention llr > 0 means bit 1, matching
higher_order.soft_llr / demodulator.HigherOrderDemodulator.
"""
from __future__ import annotations

from . import acm, channel, conv, ldpc, rs, tpc

__all__ = ["channel", "conv", "rs", "ldpc", "tpc", "acm"]
