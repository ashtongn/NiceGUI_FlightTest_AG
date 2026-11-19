#!/usr/bin/env python3
"""ECC (Error Correction Code) decoder framework for digital messaging.

Supports soft-input decoding (from LLR values) with fallback to hard-input.
Includes DMR codes (Hamming, Golay, BPTC, CRC) plus Turbo/LDPC stubs.
"""

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np


@dataclass
class CodecResult:
    """Result from codec decoding."""

    hard_bits: str  # Decoded bits (e.g., "11001001")
    num_corrections: int  # Number of bit corrections made (0 if no ECC)
    confidence: float  # Confidence in decode [0.0, 1.0]
    syndrome_weight: int = 0  # Syndrome weight (for LDPC/iterative decoders)


class CodecBase(ABC):
    """Base class for error correction codecs."""

    def __init__(self, code_name: str, k_info_bits: int, n_coded_bits: int):
        """Initialize codec.

        Args:
            code_name: Human-readable name (e.g., "Hamming(7,4)")
            k_info_bits: Number of information bits
            n_coded_bits: Total coded bits (k + parity)
        """
        self.code_name = code_name
        self.k_info_bits = k_info_bits
        self.n_coded_bits = n_coded_bits

    @abstractmethod
    def decode_soft(self, soft_bits_llr: List[float]) -> CodecResult:
        """Decode soft bits (LLRs) from demodulator.

        Args:
            soft_bits_llr: Soft bits as log-likelihood ratios

        Returns:
            CodecResult with hard bits + corrections + confidence
        """
        pass

    @abstractmethod
    def decode_hard(self, hard_bits: str) -> CodecResult:
        """Decode hard bits (fallback when soft unavailable).

        Args:
            hard_bits: String of '0' and '1'

        Returns:
            CodecResult
        """
        pass

    def _soft_to_hard(self, soft_bits_llr: List[float]) -> str:
        """Convert LLRs to hard bits (threshold at 0).

        Args:
            soft_bits_llr: List of LLR values

        Returns:
            String of '0' and '1'
        """
        return "".join("1" if llr >= 0 else "0" for llr in soft_bits_llr)

    def _confidence_from_llr(self, soft_bits_llr: List[float]) -> float:
        """Compute confidence from LLR magnitude average.

        Args:
            soft_bits_llr: List of LLR values

        Returns:
            Confidence [0.0, 1.0]
        """
        avg_magnitude = float(np.mean(np.abs(soft_bits_llr)))
        # Map to [0, 1]: magnitude 0: 0.5, magnitude > 3: ~0.95
        return 0.5 + 0.45 * min(1.0, avg_magnitude / 6.0)


class HammingDecoder(CodecBase):
    """Hamming code soft-decision decoder (single error correction)."""

    def __init__(self, k: int):
        """Initialize Hamming decoder.

        Args:
            k: Information bits (4, 9, or 11 for standard Hamming codes)
        """
        # Standard Hamming codes: (7,4), (13,9), (15,11), (31,26)
        m = None
        if k == 4:
            m = 3
        elif k == 9:
            m = 4
        elif k == 11:
            m = 4
        else:
            raise ValueError(f"Unsupported Hamming k={k}")

        n = (1 << m) - 1  # 2^m - 1
        super().__init__(f"Hamming({n},{k})", k, n)
        self.m = m
        self._build_parity_matrix()

    def _build_parity_matrix(self) -> None:
        """Build parity check matrix H for syndrome computation."""
        # For Hamming: H is m x n matrix where columns are non-zero m-bit numbers
        self.H = np.zeros((self.m, self.n_coded_bits), dtype=int)
        for col in range(self.n_coded_bits):
            # Column: binary representation of (col + 1)
            val = col + 1
            for row in range(self.m):
                self.H[row, col] = (val >> row) & 1

    def decode_soft(self, soft_bits_llr: List[float]) -> CodecResult:
        """Decode Hamming code from soft bits using syndrome decoding.

        Algorithm:
        1. Convert LLRs to hard bits
        2. Compute syndrome: s = H * r^T (mod 2)
        3. syndrome != 0: error at position syndrome (binary value)
        4. Flip that bit; compute corrections

        Args:
            soft_bits_llr: Soft bit LLRs (length n_coded_bits)

        Returns:
            CodecResult
        """
        if len(soft_bits_llr) != self.n_coded_bits:
            raise ValueError(
                f"Expected {self.n_coded_bits} bits, got {len(soft_bits_llr)}"
            )

        # Convert to hard bits
        hard_bits = self._soft_to_hard(soft_bits_llr)
        r = np.array([int(b) for b in hard_bits], dtype=int)

        # Compute syndrome
        s = (self.H @ r) % 2

        # Syndrome is error position (1-indexed)
        error_pos = 0
        for i in range(self.m):
            error_pos = (error_pos << 1) | s[self.m - 1 - i]

        num_corrections = 0
        if error_pos > 0:
            # Flip error bit (note: 1-indexed to 0-indexed)
            r[error_pos - 1] ^= 1
            num_corrections = 1

        # Extract information bits (assume standard layout: info bits first)
        info_bits = r[: self.k_info_bits]
        hard_bits_out = "".join(str(b) for b in info_bits)

        confidence = self._confidence_from_llr(soft_bits_llr)
        return CodecResult(
            hard_bits=hard_bits_out,
            num_corrections=num_corrections,
            confidence=confidence,
        )

    def decode_hard(self, hard_bits: str) -> CodecResult:
        """Decode hard bits using syndrome decoding.

        Args:
            hard_bits: String of '0' and '1' (length n_coded_bits)

        Returns:
            CodecResult
        """
        if len(hard_bits) != self.n_coded_bits:
            raise ValueError(
                f"Expected {self.n_coded_bits} bits, got {len(hard_bits)}"
            )

        # Dummy LLRs from hard bits (high magnitude = high confidence)
        llrs = [10.0 if b == "1" else -10.0 for b in hard_bits]
        return self.decode_soft(llrs)


class GolaySystDecoder(CodecBase):
    """Golay(20,8) systematic code decoder (single error correction, double detection)."""

    def __init__(self):
        """Initialize Golay(20,8) decoder."""
        super().__init__("Golay(20,8)", 8, 20)
        self._build_golay_table()

    def _build_golay_table(self) -> None:
        """Build lookup table for Golay syndrome: error vector (for small code)."""
        # For Golay(20,8), build all 2^8 = 256 info words and their syndromes
        # This is feasible for small code size
        # Simplified approach: store identity + known error patterns
        self.syndrome_table = {}
        # Build table: syndrome: error vector (exhaustive for small code)
        for info in range(256):  # 2^8 info bits
            codeword = self._encode_golay(info)
            syndrome = self._compute_golay_syndrome(codeword)
            # syndrome as integral key
            s_key = int("".join(str(b) for b in syndrome), 2)
            if s_key not in self.syndrome_table:
                self.syndrome_table[s_key] = codeword

    def _encode_golay(self, info_bits: int) -> List[int]:
        """Encode 8 info bits using Golay(20,8) systematic code.

        Golay generator matrix G = [I_8 | P] where P is 8x12 parity matrix.
        Uses standard Golay polynomial.

        Args:
            info_bits: 8-bit integer

        Returns:
            List of 20 bits [info | parity]
        """
        # Standard Golay parity polynomial (simplified)
        # In real implementation, use full Golay matrix from ETSI TS 102 361-1
        # This is a stub that adds simple parity bits
        codeword = []
        for i in range(8):
            codeword.append((info_bits >> i) & 1)

        # Compute 12 parity bits (simplified XOR parity for each subset)
        parity_masks = [
            0x0FF,  # XOR all 8 bits
            0x0F0,  # XOR bits 4-7
            0x00F,  # XOR bits 0-3
            0x0CC,  # XOR bits 2,3,6,7
            0x0AA,  # XOR bits 1,3,5,7
            0x0BB,  # XOR bits 0,1,3,4,5,7
            0x0DD,  # XOR bits 0,2,3,4,6,7
            0x0F3,  # XOR bits 0,1,4,5,6,7
            0x07E,  # XOR bits 1,2,3,4,5,6
            0x0CF,  # XOR bits 0,1,2,3,6,7
            0x03F,  # XOR bits 0,1,2,3,4,5
            0x0BA,  # XOR bits 1,3,4,5,7
            0x0D5,  # XOR bits 0,2,4,6,7
        ]
        for mask in parity_masks[:12]:
            parity_bit = 0
            for i in range(8):
                if (mask >> i) & 1:
                    parity_bit ^= (info_bits >> i) & 1
            codeword.append(parity_bit)

        return codeword

    def _compute_golay_syndrome(self, codeword: List[int]) -> List[int]:
        """Compute syndrome for Golay codeword (H * c^T mod 2).

        Args:
            codeword: 20-bit codeword

        Returns:
            12-bit syndrome
        """
        # Simplified syndrome computation
        syndrome = []
        for _ in range(12):
            # XOR subset of codeword bits
            syndrome.append(sum(codeword) % 2)
        return syndrome

    def decode_soft(self, soft_bits_llr: List[float]) -> CodecResult:
        """Decode Golay from soft bits using syndrome lookup.

        Args:
            soft_bits_llr: 20 soft bits as LLRs

        Returns:
            CodecResult
        """
        if len(soft_bits_llr) != self.n_coded_bits:
            raise ValueError(f"Expected {self.n_coded_bits} bits, got {len(soft_bits_llr)}")

        hard_bits = self._soft_to_hard(soft_bits_llr)
        codeword = [int(b) for b in hard_bits]

        # Compute syndrome
        syndrome = self._compute_golay_syndrome(codeword)
        s_key = int("".join(str(b) for b in syndrome), 2)

        # Lookup error vector (or assume error-free if syndrome = 0)
        num_corrections = 0
        if s_key in self.syndrome_table:
            # Correct using lookup
            corrected_cw = self.syndrome_table[s_key]
            num_corrections = sum(1 for i in range(20) if codeword[i] != corrected_cw[i])
            codeword = corrected_cw
        elif s_key != 0:
            # Syndrome non-zero but not in table: uncorrectable error
            num_corrections = 99  # Flag uncorrectable

        # Extract info bits (first 8)
        hard_bits_out = "".join(str(codeword[i]) for i in range(8))

        confidence = self._confidence_from_llr(soft_bits_llr)
        return CodecResult(
            hard_bits=hard_bits_out,
            num_corrections=min(num_corrections, 3),  # Cap corrections count
            confidence=confidence,
        )

    def decode_hard(self, hard_bits: str) -> CodecResult:
        """Decode hard bits using syndrome lookup.

        Args:
            hard_bits: 20 bits as string

        Returns:
            CodecResult
        """
        llrs = [10.0 if b == "1" else -10.0 for b in hard_bits]
        return self.decode_soft(llrs)


class BPTCDecoder(CodecBase):
    """BPTC(196,96) block interleaver + Hamming rows/cols decoder."""

    def __init__(self):
        """Initialize BPTC(196,96) decoder.

        BPTC = 96 data bits + 100 parity bits in 196-bit block.
        Uses 13x15 bit matrix with Hamming codes on rows and columns.
        """
        super().__init__("BPTC(196,96)", 96, 196)

    def decode_soft(self, soft_bits_llr: List[float]) -> CodecResult:
        """Decode BPTC block from soft bits.

        BPTC decoding:
        1. Arrange 196 bits into 13x15 matrix (de-interleave)
        2. Compute row syndromes (13 Hamming(15,11) decoders)
        3. Compute column syndromes (15 Hamming(13,9) decoders)
        4. Iterative correction using syndrome info
        5. Extract first 96 bits as data

        Args:
            soft_bits_llr: 196 soft bits as LLRs

        Returns:
            CodecResult
        """
        if len(soft_bits_llr) != self.n_coded_bits:
            raise ValueError(f"Expected {self.n_coded_bits} bits, got {len(soft_bits_llr)}")

        # Convert soft to hard
        hard_bits = self._soft_to_hard(soft_bits_llr)

        # Arrange in 13x15 matrix
        # BPTC(196,96): 196 = 14x14, not 13x15 (which is 195)
        matrix = np.array([int(b) for b in hard_bits], dtype=int).reshape(14, 14)

        # Perform BPTC iterative decoding (simplified: 1 iteration)
        num_corrections = 0

        # Row decoding: apply Hamming(15,11) to each of 13 rows
        # Row decoding: Hamming per row (14 rows of 14 bits each)
        for row_idx in range(14):
            row_bits = matrix[row_idx, :]
            # Compute syndrome for this row (simplified Hamming)
            parity = sum(row_bits) % 2
            if parity != 0:
                # Single error in row: flip bit at highest-magnitude LLR position in this row
                row_llrs = soft_bits_llr[row_idx * 14 : (row_idx + 1) * 14]
                min_llr_idx = np.argmin(np.abs(row_llrs))
                matrix[row_idx, min_llr_idx] ^= 1
                num_corrections += 1

        # Column decoding: similar (simplified)
        for col_idx in range(14):
            col_bits = matrix[:, col_idx]
            parity = sum(col_bits) % 2
            if parity != 0:
                # Single error in column: flip bit
                col_llrs = soft_bits_llr[col_idx :: 14]  # Every 14th element
                min_llr_idx = np.argmin(np.abs(col_llrs))
                matrix[min_llr_idx, col_idx] ^= 1
                num_corrections += 1

        # Extract data bits (first 96 from flattened matrix)
        flattened = matrix.flatten()
        hard_bits_out = "".join(str(flattened[i]) for i in range(96))

        confidence = self._confidence_from_llr(soft_bits_llr)
        return CodecResult(
            hard_bits=hard_bits_out,
            num_corrections=min(num_corrections, 3),
            confidence=confidence,
        )

    def decode_hard(self, hard_bits: str) -> CodecResult:
        """Decode hard bits.

        Args:
            hard_bits: 196 bits

        Returns:
            CodecResult
        """
        llrs = [10.0 if b == "1" else -10.0 for b in hard_bits]
        return self.decode_soft(llrs)


class CRCValidator(CodecBase):
    """CRC validator (no error correction, only validation)."""

    def __init__(self, crc_type: str = "CRC-16"):
        """Initialize CRC validator.

        Args:
            crc_type: "CRC-16", "CRC-9", or "CRC-32"
        """
        self.crc_type = crc_type
        if crc_type == "CRC-16":
            # CRC-CCITT polynomial 0x1021
            self.poly = 0x1021
            self.init_val = 0xFFFF
            self.final_xor = 0xFFFF
            n = 16
        elif crc_type == "CRC-9":
            # DMR CACH CRC polynomial 0x059
            self.poly = 0x059
            self.init_val = 0x0
            self.final_xor = 0x0
            n = 9
        elif crc_type == "CRC-32":
            # CRC-32 polynomial 0x04C11DB7
            self.poly = 0x04C11DB7
            self.init_val = 0xFFFFFFFF
            self.final_xor = 0xFFFFFFFF
            n = 32
        else:
            raise ValueError(f"Unsupported CRC type: {crc_type}")

        super().__init__(crc_type, 0, n)  # k=0 because CRC is validation-only

    def _compute_crc(self, bit_string: str) -> int:
        """Compute CRC for bit string.

        Args:
            bit_string: String of '0' and '1'

        Returns:
            CRC value (integer)
        """
        crc = self.init_val
        for bit_char in bit_string:
            bit = int(bit_char)
            crc = ((crc << 1) | bit) & ((1 << len(bin(self.poly)) - 2) - 1)
            if crc & (1 << (len(bin(self.poly)) - 2 - 1)):
                msb_pos = len(bin(self.poly)) - 2
                crc ^= (self.poly & ((1 << msb_pos) - 1))
        return crc ^ self.final_xor

    def decode_soft(self, soft_bits_llr: List[float]) -> CodecResult:
        """Validate CRC from soft bits.

        Args:
            soft_bits_llr: Soft bits (message + CRC)

        Returns:
            CodecResult with num_corrections = 0 if match, else mismatch count
        """
        hard_bits = self._soft_to_hard(soft_bits_llr)
        return self.decode_hard(hard_bits)

    def decode_hard(self, hard_bits: str) -> CodecResult:
        """Validate CRC from hard bits.

        Args:
            hard_bits: Message bits (includes CRC)

        Returns:
            CodecResult (num_corrections = 0 if match, else indicates mismatch)
        """
        computed_crc = self._compute_crc(hard_bits)
        # If CRC is valid, computed CRC should be 0
        is_valid = computed_crc == 0

        confidence = 1.0 if is_valid else 0.0
        num_corrections = 0 if is_valid else 1  # Flag mismatch

        # Return original bits (no correction)
        return CodecResult(
            hard_bits=hard_bits,
            num_corrections=num_corrections,
            confidence=confidence,
        )


class TurboDecoder(CodecBase):
    """Turbo code decoder: systematic PCCC rate-1/2 with Log-MAP BCJR.

    Code parameters
    ---------------
    * Info bits  k = 128
    * Coded bits n = 256  (rate 1/2 after puncturing)
    * Component RSC: generator (1, 5/7) in octal: constraint length 3
    * Interleaver: S-random permutation seeded deterministically for reproducibility
    * Iterations: 6  (configurable via ``_ITERATIONS``)

    Encoding layout (per bit position in the 256-bit codeword)
    ----------------------------------------------------------
    sys[0..127]: systematic bits (unchanged info bits)
    par[0..127]: parity bits from encoder-1, alternating punctured pattern:
                   even positions then parity1, odd positions then parity2
    """

    _N = 128
    _ITERATIONS = 6
    _POLY_G0 = 0b101   # feedback  (D^0 + D^2)   octal 5
    _POLY_G1 = 0b111   # feedforward (D^0+D^1+D^2) octal 7
    _CONSTRAINT = 3    # K=3: 4 states

    def __init__(self):
        super().__init__("Turbo(256,128)", 128, 256)
        self._interleaver = self._build_interleaver(self._N)

    # Helpers

    @staticmethod
    def _build_interleaver(n: int) -> List[int]:
        """Deterministic S-random interleaver (S = floor(sqrt(n)))."""
        s_val = int(math.sqrt(n))
        perm = list(range(n))
        rng_state = 42
        for i in range(n - 1, 0, -1):
            # LCG with fixed parameters for reproducibility
            rng_state = (rng_state * 1664525 + 1013904223) & 0xFFFFFFFF
            j = rng_state % (i + 1)
            # Enforce S-random property (best-effort for speed)
            attempts = 0
            while attempts < 32:
                ok = True
                for prev in range(max(0, i - s_val), i):
                    if abs(perm[j] - perm[prev]) < s_val:
                        ok = False
                        break
                if ok:
                    break
                rng_state = (rng_state * 1664525 + 1013904223) & 0xFFFFFFFF
                j = rng_state % (i + 1)
                attempts += 1
            perm[i], perm[j] = perm[j], perm[i]
        return perm

    @staticmethod
    def _rsc_trellis(
        input_llr: List[float],
        g0: int,
        g1: int,
        constraint: int,
        apriori_llr: List[float],
        terminate: bool = False,
    ) -> List[float]:
        """Log-MAP BCJR on a single RSC component code.

        Args:
            input_llr:   Channel LLRs for systematic bits (length n).
            g0:          Feedback polynomial (binary, MSB = highest degree).
            g1:          Feedforward (parity) polynomial.
            constraint:  Constraint length K (register depth = K-1).
            apriori_llr: A-priori LLRs from other decoder (length n).
            terminate:   If True, assume trellis terminates to state 0.

        Returns:
            Extrinsic LLRs (length n), positive = bit-1 more likely.
        """
        n = len(input_llr)
        num_states = 1 << (constraint - 1)
        NEG_INF = -1e30

        def log_sum_exp(a: float, b: float) -> float:
            """Jacobian logarithm approximation: log(e^a + e^b)."""
            diff = abs(a - b)
            return max(a, b) + math.log1p(math.exp(-diff)) if diff < 50 else max(a, b)

        # Build trellis transitions: dict (state, bit): (next_state, parity_bit)
        transitions: dict = {}
        for state in range(num_states):
            for bit in range(2):
                # Feedback shift register
                reg = (state << 1) | bit
                feedback = 0
                masked = reg & g0
                for _ in range(constraint):
                    feedback ^= masked & 1
                    masked >>= 1
                # Next state
                next_state = (reg ^ (feedback << (constraint - 1))) & (num_states - 1)
                # Parity output
                par_out = 0
                reg2 = (state << 1) | bit
                masked2 = reg2 & g1
                for _ in range(constraint):
                    par_out ^= masked2 & 1
                    masked2 >>= 1
                transitions[(state, bit)] = (next_state, par_out)

        # Forward alpha (log-domain)
        alpha = [[NEG_INF] * num_states for _ in range(n + 1)]
        alpha[0][0] = 0.0

        for t in range(n):
            ch = input_llr[t]
            ap = apriori_llr[t]
            for state in range(num_states):
                if alpha[t][state] == NEG_INF:
                    continue
                for bit in range(2):
                    ns, par = transitions[(state, bit)]
                    # Branch metric: systematic LLR + a-priori LLR + parity channel LLR
                    # Parity channel LLR is 0 when punctured (soft zero)
                    bm = (0.5 * ch + 0.5 * ap) * (2 * bit - 1)
                    val = alpha[t][state] + bm
                    if val > alpha[t + 1][ns]:
                        alpha[t + 1][ns] = val
                    else:
                        alpha[t + 1][ns] = log_sum_exp(alpha[t + 1][ns], val)

        # Backward beta (log-domain)
        beta = [[NEG_INF] * num_states for _ in range(n + 1)]
        if terminate:
            beta[n][0] = 0.0
        else:
            for s in range(num_states):
                beta[n][s] = 0.0

        for t in range(n - 1, -1, -1):
            ch = input_llr[t]
            ap = apriori_llr[t]
            for state in range(num_states):
                for bit in range(2):
                    ns, par = transitions[(state, bit)]
                    if beta[t + 1][ns] == NEG_INF:
                        continue
                    bm = (0.5 * ch + 0.5 * ap) * (2 * bit - 1)
                    val = beta[t + 1][ns] + bm
                    if val > beta[t][state]:
                        beta[t][state] = val
                    else:
                        beta[t][state] = log_sum_exp(beta[t][state], val)

        # Extrinsic LLR computation
        extrinsic = []
        for t in range(n):
            L1 = NEG_INF  # accumulator for bit=1
            L0 = NEG_INF  # accumulator for bit=0
            ch = input_llr[t]
            ap = apriori_llr[t]
            for state in range(num_states):
                if alpha[t][state] == NEG_INF:
                    continue
                for bit in range(2):
                    ns, par = transitions[(state, bit)]
                    if beta[t + 1][ns] == NEG_INF:
                        continue
                    bm = (0.5 * ch + 0.5 * ap) * (2 * bit - 1)
                    val = alpha[t][state] + bm + beta[t + 1][ns]
                    if bit == 1:
                        L1 = log_sum_exp(L1, val)
                    else:
                        L0 = log_sum_exp(L0, val)
            # Extrinsic = total LLR minus channel and a-priori contributions
            total = (L1 - L0) if (L1 != NEG_INF and L0 != NEG_INF) else 0.0
            extrinsic.append(total - ch - ap)
        return extrinsic

    # Public interface

    def decode_soft(self, soft_bits_llr: List[float]) -> CodecResult:
        """Iterative Log-MAP Turbo decoding.

        The 256-bit codeword layout:
            positions 0..127:   systematic bits
            positions 128..255: parity/punctured bits
                even index in parity block: parity from encoder-1
                odd  index in parity block: parity from encoder-2

        Args:
            soft_bits_llr: 256 soft bits as LLRs.

        Returns:
            CodecResult with decoded 128 info bits.
        """
        if len(soft_bits_llr) != self.n_coded_bits:
            raise ValueError(f"Expected {self.n_coded_bits} LLRs, got {len(soft_bits_llr)}")

        k = self._N
        sys_llr = list(soft_bits_llr[:k])
        par_llr = list(soft_bits_llr[k:])

        # Split punctured parity: even to enc1, odd to enc2 (0-value when punctured)
        par1 = [par_llr[i] if i % 2 == 0 else 0.0 for i in range(k)]
        par2 = [par_llr[i] if i % 2 == 1 else 0.0 for i in range(k)]

        apriori = [0.0] * k  # initial a-priori LLR = 0 (no prior)

        for _ in range(self._ITERATIONS):
            # Decoder 1: uses sys_llr + par1 + apriori
            combined1 = [sys_llr[i] + par1[i] for i in range(k)]
            ext1 = self._rsc_trellis(
                combined1, self._POLY_G0, self._POLY_G1,
                self._CONSTRAINT, apriori,
            )
            # Decoder 2: interleaved systematic + par2, a-priori = permuted ext1
            sys_interleaved = [sys_llr[self._interleaver[i]] for i in range(k)]
            apriori2 = [ext1[self._interleaver[i]] for i in range(k)]
            combined2 = [sys_interleaved[i] + par2[i] for i in range(k)]
            ext2_perm = self._rsc_trellis(
                combined2, self._POLY_G0, self._POLY_G1,
                self._CONSTRAINT, apriori2,
            )
            # De-interleave ext2
            ext2 = [0.0] * k
            for i in range(k):
                ext2[self._interleaver[i]] = ext2_perm[i]
            # Update a-priori for next iteration
            apriori = ext2

        # Final soft output = systematic + extrinsic from both decoders
        final_llr = [sys_llr[i] + ext1[i] + ext2[i] for i in range(k)]
        hard_bits_out = "".join("1" if l >= 0 else "0" for l in final_llr)

        # Count corrections
        hard_sys = self._soft_to_hard(sys_llr)
        num_corrections = sum(1 for a, b in zip(hard_sys, hard_bits_out) if a != b)

        confidence = self._confidence_from_llr(final_llr)
        return CodecResult(
            hard_bits=hard_bits_out,
            num_corrections=num_corrections,
            confidence=confidence,
        )

    def decode_hard(self, hard_bits: str) -> CodecResult:
        llrs = [10.0 if b == "1" else -10.0 for b in hard_bits]
        return self.decode_soft(llrs)

    # Encoder (for test generation and integration pipeline)

    def encode(self, info_bits: str) -> str:
        """Encode 128 info bits into 256-bit Turbo codeword.

        Layout: [sys(128)] [par1_0, par2_0, par1_1, par2_1, ..., par1_127, par2_127]

        Args:
            info_bits: String of 128 '0'/'1' characters.

        Returns:
            256-bit codeword as string.
        """
        if len(info_bits) != self._N:
            raise ValueError(f"Turbo encoder expects exactly {self._N} info bits.")

        def rsc_encode(bits: List[int], g0: int, g1: int, constraint: int) -> List[int]:
            state = 0
            parity = []
            for bit in bits:
                reg = (state << 1) | bit
                fb = 0
                masked = reg & g0
                for _ in range(constraint):
                    fb ^= masked & 1
                    masked >>= 1
                state = (reg ^ (fb << (constraint - 1))) & ((1 << (constraint - 1)) - 1)
                par = 0
                reg2 = (state << 1) | bit
                masked2 = reg2 & g1
                for _ in range(constraint):
                    par ^= masked2 & 1
                    masked2 >>= 1
                parity.append(par)
            return parity

        d = [int(b) for b in info_bits]
        par1 = rsc_encode(d, self._POLY_G0, self._POLY_G1, self._CONSTRAINT)
        d_int = [d[self._interleaver[i]] for i in range(self._N)]
        par2 = rsc_encode(d_int, self._POLY_G0, self._POLY_G1, self._CONSTRAINT)

        # Puncture to rate 1/2: interleave par1/par2: even slots then par1, odd then par2.
        # Decoder reads positions 0..127 as parity; even-indexed then enc1, odd then enc2.
        # Interleaved layout: [par1[0], par2[0], par1[1], par2[1], ...] gives 256 items,
        # then we drop every other pair to keep exactly 128.  Simplest consistent scheme:
        # alternate one par1 and one par2 until 128 slots are filled (64 each).
        parity_punctured = []
        for i in range(self._N // 2):
            parity_punctured.append(par1[i])  # even position
            parity_punctured.append(par2[i])  # odd position

        return info_bits + "".join(str(b) for b in parity_punctured)


class LDPCDecoder(CodecBase):
    """LDPC(512,256) decoder using sum-product (belief propagation) algorithm.

    Code construction
    -----------------
    * n = 512  coded bits,  k = 256  info bits,  rate = 1/2
    * Parity-check matrix H (256 x 512): quasi-cyclic construction with
      column weight 3 and row weight 6 (regular LDPC).
    * H is built deterministically from a circulant base matrix so that
      the same parity matrix is used for both encoding and decoding.
    * Max iterations: 50 (early-termination when syndrome = 0).
    """

    _N = 512
    _K = 256
    _M = 256   # number of parity-check equations = N - K
    _COL_WEIGHT = 3
    _MAX_ITER = 50

    def __init__(self):
        super().__init__("LDPC(512,256)", 256, 512)
        self._H, self._var_nodes, self._check_nodes = self._build_parity_matrix()

    # Matrix construction

    def _build_parity_matrix(self):
        """Build quasi-cyclic LDPC parity-check matrix H (M x N).

        Uses a base matrix B of size (M/Z) x (N/Z) with circulant shifts.
        Z = 32 (circulant block size), base matrix is 8 x 16.

        Returns:
            H (np.ndarray, uint8, shape MxN),
            var_nodes  (list of sets: var_node i: set of check indices),
            check_nodes (list of sets: check_node j: set of var indices).
        """
        M, N, Z = self._M, self._N, 32
        mb, nb = M // Z, N // Z   # 8, 16

        # Base matrix shifts: -1 means zero block, >=0 is circulant shift
        # Designed to give column weight 3, row weight 6
        BASE = [
            [0, -1, -1, 0, -1, -1, 0, -1, -1, 0, -1, -1, 0, -1, -1, 0],
            [-1, 1, -1, -1, 2, -1, -1, 3, -1, -1, 4, -1, -1, 5, -1, -1],
            [-1, -1, 6, -1, -1, 7, -1, -1, 8, -1, -1, 9, -1, -1, 10, -1],
            [11, -1, -1, 12, -1, -1, 13, -1, -1, 14, -1, -1, 15, -1, -1, 16],
            [-1, 17, -1, -1, 18, -1, -1, 19, -1, -1, 20, -1, -1, 21, -1, -1],
            [-1, -1, 22, -1, -1, 23, -1, -1, 24, -1, -1, 25, -1, -1, 26, -1],
            [27, -1, -1, 28, -1, -1, 29, -1, -1, 30, -1, -1, 31, -1, -1, 0],
            [-1, 1, -1, -1, 2, -1, -1, 3, -1, -1, 4, -1, -1, 5, -1, 6],
        ]

        H = np.zeros((M, N), dtype=np.uint8)
        for bi in range(mb):
            for bj in range(nb):
                shift = BASE[bi][bj]
                if shift < 0:
                    continue
                # Add circulant block of size Z with cyclic shift
                for r in range(Z):
                    c = (r + shift) % Z
                    H[bi * Z + r, bj * Z + c] = 1

        # Build adjacency lists
        var_nodes: List[set] = [set() for _ in range(N)]
        check_nodes: List[set] = [set() for _ in range(M)]
        rows, cols = np.nonzero(H)
        for r, c in zip(rows.tolist(), cols.tolist()):
            check_nodes[r].add(c)
            var_nodes[c].add(r)

        return H, var_nodes, check_nodes

    # Sum-product decoder

    def decode_soft(self, soft_bits_llr: List[float]) -> CodecResult:
        """Sum-product (belief propagation) LDPC decoding.

        Args:
            soft_bits_llr: 512 channel LLRs (positive = bit-1 more likely).

        Returns:
            CodecResult with decoded 256 info bits.
        """
        if len(soft_bits_llr) != self.n_coded_bits:
            raise ValueError(f"Expected {self.n_coded_bits} LLRs, got {len(soft_bits_llr)}")

        N = self._N
        # Internally the sum-product algorithm uses the convention:
        # LLR = log(P(bit=0) / P(bit=1)), so positive LLR: bit=0.
        # Callers pass LLR = log(P(bit=1)/P(bit=0)) (positive: bit=1),
        # so negate once on entry and once on exit for hard decisions.
        ch = -np.array(soft_bits_llr, dtype=float)   # flip to internal convention
        # v2c[v][c] = message from variable v to check c
        v2c: dict = {v: {c: ch[v] for c in self._var_nodes[v]} for v in range(N)}

        hard_bits = None
        syndrome_weight = 0
        num_corrections = 0
        initial_hard = (ch < 0).astype(np.uint8)   # internal: positive to bit=0, negative to bit=1

        for iteration in range(self._MAX_ITER):
            # Check-to-variable update (min-sum approximation for speed)
            c2v: dict = {}
            for c_idx, vars_in_check in enumerate(self._check_nodes):
                c2v[c_idx] = {}
                for v in vars_in_check:
                    others = [v2c[u][c_idx] for u in vars_in_check if u != v]
                    if not others:
                        c2v[c_idx][v] = 0.0
                        continue
                    sign = 1
                    for o in others:
                        if o < 0:
                            sign = -sign
                    min_mag = min(abs(o) for o in others)
                    # Scale factor 0.875 for offset min-sum
                    c2v[c_idx][v] = sign * max(0.0, min_mag - 0.875)

            # Variable-to-check update
            for v in range(N):
                total = ch[v] + sum(c2v[c][v] for c in self._var_nodes[v])
                for c in self._var_nodes[v]:
                    v2c[v][c] = total - c2v[c][v]

            # Hard decision (internal convention: positive: bit=0)
            total_llr = np.array([
                ch[v] + sum(c2v[c][v] for c in self._var_nodes[v])
                for v in range(N)
            ])
            hard = (total_llr < 0).astype(np.uint8)

            # Syndrome check
            syndrome = (self._H @ hard) % 2
            syndrome_weight = int(np.sum(syndrome))
            if syndrome_weight == 0:
                hard_bits = "".join(str(b) for b in hard[:self._K])
                num_corrections = int(np.sum(hard != initial_hard))
                break
        else:
            # Did not converge: return best estimate
            total_llr_final = np.array([
                ch[v] + sum(c2v[c][v] for c in self._var_nodes[v])
                for v in range(N)
            ])
            hard_final = (total_llr_final < 0).astype(np.uint8)
            hard_bits = "".join(str(b) for b in hard_final[:self._K])
            num_corrections = int(np.sum(hard_final != initial_hard))

        confidence = self._confidence_from_llr(soft_bits_llr)
        return CodecResult(
            hard_bits=hard_bits,
            num_corrections=num_corrections,
            confidence=confidence,
            syndrome_weight=syndrome_weight,
        )

    def decode_hard(self, hard_bits: str) -> CodecResult:
        llrs = [10.0 if b == "1" else -10.0 for b in hard_bits]
        return self.decode_soft(llrs)

    # Encoder

    def encode(self, info_bits: str) -> str:
        """Systematic LDPC encoding via back-substitution.

        Partitions H = [H_info | H_parity] and solves for parity bits p:
          H_parity * p^T = H_info * d^T  (mod 2)

        For speed, uses the lower-triangular structure of the quasi-cyclic
        parity sub-matrix.  Falls back to Gaussian elimination if needed.

        Args:
            info_bits: 256-bit string.

        Returns:
            512-bit codeword string [info | parity].
        """
        if len(info_bits) != self._K:
            raise ValueError(f"LDPC encoder expects exactly {self._K} info bits.")

        d = np.array([int(b) for b in info_bits], dtype=np.uint8)
        H_info = self._H[:, :self._K]
        H_par = self._H[:, self._K:]

        # Right-hand side
        rhs = (H_info @ d) % 2

        # Back-substitution through H_par (upper-triangular after column pivoting)
        # Use Gaussian elimination with partial pivoting
        M = self._M
        A = np.copy(H_par).astype(np.uint8)
        b = np.copy(rhs)
        pivot_cols: List[int] = []

        for col in range(M):
            # Find pivot row
            pivot = -1
            for row in range(col, M):
                if A[row, col] == 1:
                    pivot = row
                    break
            if pivot < 0:
                continue
            if pivot != col:
                A[[col, pivot]] = A[[pivot, col]]
                b[col], b[pivot] = b[pivot], b[col]
            pivot_cols.append(col)
            for row in range(M):
                if row != col and A[row, col] == 1:
                    A[row] = (A[row] + A[col]) % 2
                    b[row] = (b[row] + b[col]) % 2

        parity = np.zeros(M, dtype=np.uint8)
        for col in pivot_cols:
            parity[col] = b[col]

        codeword = np.concatenate([d, parity])
        return "".join(str(b) for b in codeword)


def get_codec(codec_name: str) -> CodecBase:
    """Factory function to get codec by name.

    Args:
        codec_name: Codec name (e.g., "Hamming(7,4)", "CRC-16", "BPTC(196,96)")

    Returns:
        CodecBase instance

    Raises:
        ValueError: If codec not supported
    """
    if codec_name == "None":
        return DummyCodec()

    if codec_name.startswith("Hamming"):
        # Extract k from "Hamming(n,k)"
        k = int(codec_name.split(",")[1].rstrip(")"))
        return HammingDecoder(k)

    if codec_name == "Golay(20,8)":
        return GolaySystDecoder()

    if codec_name == "BPTC(196,96)":
        return BPTCDecoder()

    if codec_name.startswith("CRC"):
        return CRCValidator(codec_name)

    if codec_name == "Turbo(256,128)":
        return TurboDecoder()

    if codec_name == "LDPC(512,256)":
        return LDPCDecoder()

    raise ValueError(f"Unknown codec: {codec_name}")


class DummyCodec(CodecBase):
    """Dummy codec that returns bits unchanged (for 'None' encoding)."""

    def __init__(self):
        """Initialize dummy codec."""
        super().__init__("None", 0, 0)

    def decode_soft(self, soft_bits_llr: List[float]) -> CodecResult:
        """Return bits unchanged."""
        hard_bits = self._soft_to_hard(soft_bits_llr)
        return CodecResult(hard_bits=hard_bits, num_corrections=0, confidence=1.0)

    def decode_hard(self, hard_bits: str) -> CodecResult:
        """Return bits unchanged."""
        return CodecResult(hard_bits=hard_bits, num_corrections=0, confidence=1.0)


# Module-level encoding functions (shared by encoder and validator UIs)
# All functions accept and return Python bit-strings ("0" and "1" characters).


# Generator matrix parity columns (shared constants)

_H1393_PARITY: List[List[int]] = [
    [1, 1, 0, 0],
    [1, 0, 1, 0],
    [1, 0, 0, 1],
    [0, 1, 1, 0],
    [0, 1, 0, 1],
    [0, 0, 1, 1],
    [1, 1, 1, 0],
    [1, 1, 0, 1],
    [1, 0, 1, 1],
]

_H15113_PARITY: List[List[int]] = [
    [1, 0, 0, 1],
    [1, 1, 0, 1],
    [1, 1, 1, 1],
    [1, 1, 1, 0],
    [0, 1, 1, 1],
    [1, 0, 1, 0],
    [0, 1, 0, 1],
    [1, 0, 1, 1],
    [1, 1, 0, 0],
    [0, 1, 1, 0],
    [0, 0, 1, 1],
]

_GOLAY_GEN: List[List[int]] = [
    [1, 0, 0, 1, 0, 1, 1, 0, 1, 1, 1, 0],
    [0, 0, 1, 0, 1, 1, 0, 1, 1, 1, 0, 1],
    [1, 0, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0],
    [0, 1, 0, 1, 1, 1, 1, 1, 1, 0, 0, 0],
    [1, 0, 0, 0, 1, 0, 1, 1, 1, 1, 0, 1],
    [1, 1, 0, 0, 0, 1, 0, 1, 1, 1, 1, 0],
    [0, 1, 1, 0, 0, 0, 1, 0, 1, 1, 1, 1],
    [1, 1, 1, 1, 0, 1, 0, 0, 0, 1, 1, 0],
]


def hamming_7_4_encode(bit_string: str) -> str:
    """Hamming(7,4) encoder per ETSI TS 102 361-1 B.3.8.

    Pads input to a multiple of 4 bits then encodes each nibble.
    """
    pad = (-len(bit_string)) % 4
    if pad:
        bit_string += "0" * pad
    encoded = []
    for i in range(0, len(bit_string), 4):
        d = [int(bit_string[i + j]) for j in range(4)]
        p1 = d[0] ^ d[1] ^ d[3]
        p2 = d[0] ^ d[2] ^ d[3]
        p3 = d[1] ^ d[2] ^ d[3]
        encoded.extend([p1, p2, d[0], p3, d[1], d[2], d[3]])
    return "".join(str(b) for b in encoded)


def hamming_13_9_encode(bit_string: str) -> str:
    """Hamming(13,9) encoder per ETSI TS 102 361-1 B.3.9."""
    pad = (-len(bit_string)) % 9
    if pad:
        bit_string += "0" * pad
    encoded = []
    for i in range(0, len(bit_string), 9):
        d = [int(bit_string[i + j]) for j in range(9)]
        parity = [0, 0, 0, 0]
        for ri, row in enumerate(_H1393_PARITY):
            for c in range(4):
                parity[c] ^= d[ri] * row[c]
        encoded.extend(d)
        encoded.extend([p % 2 for p in parity])
    return "".join(str(b) for b in encoded)


def hamming_15_11_encode(bit_string: str) -> str:
    """Hamming(15,11) encoder per ETSI TS 102 361-1 B.3.10."""
    pad = (-len(bit_string)) % 11
    if pad:
        bit_string += "0" * pad
    encoded = []
    for i in range(0, len(bit_string), 11):
        d = [int(bit_string[i + j]) for j in range(11)]
        parity = [0, 0, 0, 0]
        for ri, row in enumerate(_H15113_PARITY):
            for c in range(4):
                parity[c] ^= d[ri] * row[c]
        encoded.extend(d)
        encoded.extend([p % 2 for p in parity])
    return "".join(str(b) for b in encoded)


def bptc_196_96_encode(bit_string: str) -> str:
    """BPTC(196,96) encoder per ETSI TS 102 361-1 B.2.1.

    This implementation matches DMR on-air slot framing conventions used by
    OP25/MMDVM style stacks:
    - Payload extraction/insertion map is aligned to the 196-bit slot payload
      deinterleave layout.
    - Row FEC uses Hamming(15,11,3) variant 2.
    - Column FEC uses Hamming(13,9,3).
    - Interleave mapping uses (a * 181) % 196.
    """
    if len(bit_string) > 96:
        raise ValueError("BPTC(196,96) accepts a maximum of 96 data bits.")
    bit_string = bit_string.ljust(96, "0")

    # Deinterleaved working buffer (196 bits). Bit 0 is reserved (R(3)).
    de = [0] * 196

    # Inverse of decode extraction ranges used by DMR slot decoders.
    payload_ranges = [
        (4, 11),
        (16, 26),
        (31, 41),
        (46, 56),
        (61, 71),
        (76, 86),
        (91, 101),
        (106, 116),
        (121, 131),
    ]
    bit_idx = 0
    for lo, hi in payload_ranges:
        for pos in range(lo, hi + 1):
            de[pos] = int(bit_string[bit_idx])
            bit_idx += 1

    # Reserved bits R(0..3): set to zero for data payloads.
    de[0] = 0
    de[1] = 0
    de[2] = 0
    de[3] = 0

    # Row parity: Hamming(15,11,3) variant 2 across rows 0..8.
    for row in range(9):
        base = row * 15 + 1
        d = [de[base + c] for c in range(11)]
        de[base + 11] = d[0] ^ d[1] ^ d[2] ^ d[3] ^ d[5] ^ d[7] ^ d[8]
        de[base + 12] = d[1] ^ d[2] ^ d[3] ^ d[4] ^ d[6] ^ d[8] ^ d[9]
        de[base + 13] = d[2] ^ d[3] ^ d[4] ^ d[5] ^ d[7] ^ d[9] ^ d[10]
        de[base + 14] = d[0] ^ d[1] ^ d[2] ^ d[4] ^ d[6] ^ d[7] ^ d[10]

    # Column parity: Hamming(13,9,3) across each of the 15 columns.
    for col in range(15):
        d = [de[row * 15 + 1 + col] for row in range(9)]
        de[(9 * 15) + 1 + col] = d[0] ^ d[1] ^ d[3] ^ d[5] ^ d[6]
        de[(10 * 15) + 1 + col] = d[0] ^ d[1] ^ d[2] ^ d[4] ^ d[6] ^ d[7]
        de[(11 * 15) + 1 + col] = d[0] ^ d[1] ^ d[2] ^ d[3] ^ d[5] ^ d[7] ^ d[8]
        de[(12 * 15) + 1 + col] = d[0] ^ d[2] ^ d[4] ^ d[5] ^ d[8]

    # Interleave for on-air 196-bit payload segment.
    raw = [0] * 196
    for a in range(196):
        raw[(a * 181) % 196] = de[a]
    return "".join(str(b) for b in raw)


def golay_20_8_encode(bit_string: str) -> str:
    """Golay(20,8) systematic encoder per ETSI TS 102 361-1 B.3.4.

    Uses the cyclic (20,8,7) construction compatible with DMR slot-type coding:
    - First 8 bits are information bits.
    - Next 11 bits are BCH/Golay parity from generator polynomial 0xC75.
    - Final bit is overall parity (even parity across first 19 bits).
    """
    if len(bit_string) > 8:
        raise ValueError("Golay(20,8) accepts a maximum of 8 data bits.")
    bit_string = bit_string.ljust(8, "0")

    value = int(bit_string, 2)

    # 19-bit cyclic code: 8 data bits + 11 parity bits.
    GENPOL_GL = 0x00000C75
    X11 = 0x00000800
    X18 = 0x00040000
    MASK8 = 0xFFFFF800

    pattern = value << 11
    aux = X18
    if pattern >= X11:
        while pattern & MASK8:
            while not (aux & pattern):
                aux >>= 1
            pattern ^= (aux // X11) * GENPOL_GL

    code19 = (value << 11) | pattern
    parity_bit = bin(code19).count("1") & 0x1
    code20 = (code19 << 1) | parity_bit
    return f"{code20:020b}"


def rate_34_trellis_encode(bit_string: str) -> str:
    """Rate 3/4 trellis coded modulation encoder per ETSI TS 102 361-1 B.2.2."""
    pad = (-len(bit_string)) % 3
    if pad:
        bit_string += "0" * pad
    state = 0
    output = []
    for i in range(0, len(bit_string), 3):
        tribits = [int(bit_string[i + j]) for j in range(3)]
        for tribit in tribits:
            state = ((state << 1) | tribit) & 0x0F
            b3 = (state >> 3) & 1
            b2 = (state >> 2) & 1
            b1 = (state >> 1) & 1
            b0 = state & 1
            g0 = b0 ^ b1 ^ b3
            g1 = b0 ^ b2 ^ b3
            g2 = b0 ^ b1 ^ b2 ^ b3
            g3 = b0 ^ b1 ^ b2
            output.extend([g0, g1, g2, g3])
    return "".join(str(b) for b in output)


def crc_ccitt(bit_string: str) -> str:
    """CRC-CCITT (CRC-16/CCITT-FALSE) per ETSI TS 102 361-1 B.3.12.

    Polynomial 0x1021, init 0x0000, final XOR 0xFFFF.
    Returns 16-bit CRC as bit string.
    """
    crc = 0x0000
    for bit_ch in bit_string:
        bit = int(bit_ch)
        msb = (crc >> 15) & 1
        crc = (crc << 1) & 0xFFFF
        if bit ^ msb:
            crc ^= 0x1021
    crc ^= 0xFFFF
    return f"{crc:016b}"


def crc_9(bit_string: str) -> str:
    """CRC-9 per ETSI TS 102 361-1 B.3.11.

    Polynomial 0x059, init 0x000, final XOR 0x000.
    Returns 9-bit CRC as bit string.
    """
    crc = 0x000
    for bit_ch in bit_string:
        bit = int(bit_ch)
        msb = (crc >> 8) & 1
        crc = (crc << 1) & 0x1FF
        if bit ^ msb:
            crc ^= 0x059
    return f"{crc:09b}"


def crc_32(bit_string: str) -> str:
    """CRC-32 per ETSI TS 102 361-1 B.3.13.

    Polynomial 0x04C11DB7, init 0xFFFFFFFF, final XOR 0xFFFFFFFF.
    Returns 32-bit CRC as bit string.
    """
    crc = 0xFFFFFFFF
    for bit_ch in bit_string:
        bit = int(bit_ch)
        msb = (crc >> 31) & 1
        crc = (crc << 1) & 0xFFFFFFFF
        if bit ^ msb:
            crc ^= 0x04C11DB7
    crc ^= 0xFFFFFFFF
    return f"{crc:032b}"


def csbk_burst_264bit_encode(raw_bits: str) -> str:
    """Encode raw 96-bit CSBK into full 264-bit DMR burst.
    
    Structure: 98 payload + 10 slot + 48 sync + 10 slot + 98 payload
    The 96-bit CSBK is BPTC(196,96) encoded. Payload bits are interleaved
    according to DMR standard, slot bits are Golay-encoded, sync is fixed pattern.
    
    Args:
        raw_bits: 96-bit CSBK data
        
    Returns:
        264-bit burst
    """
    if len(raw_bits) != 96:
        raise ValueError(f"CSBK Burst expects 96 input bits, got {len(raw_bits)}")
    
    # Step 1: BPTC encode the raw CSBK to 196 bits
    bptc = bptc_196_96_encode(raw_bits)
    
    # Step 2: Arrange into payloads using DMR interleaving
    # Simple 13x15 matrix interleaving read out column-wise
    # Write BPTC row-major into 13x15 matrix, read column-major alternating
    
    # Create interleaving indices based on standard DMR pattern
    # Place BPTC bits into 13x15 matrix (195 bits) + 1 parity
    matrix_bits = list(bptc[:195]) + [int(bptc[195])]
    
    # Reorganize into two payloads
    # Distribute columns alternately: columns 0-7 to p1, 8-14 to p2
    payload1_bits = []
    payload2_bits = []
    
    bit_idx = 0
    for col in range(15):
        for row in range(13):
            if bit_idx < 195:
                if col < 7:
                    payload1_bits.append(str(matrix_bits[bit_idx]))
                else:
                    payload2_bits.append(str(matrix_bits[bit_idx]))
                bit_idx += 1
    
    # Handle parity bit
    payload1_bits.append(str(matrix_bits[195]))
    
    # Pad to 98 bits
    while len(payload1_bits) < 98:
        payload1_bits.append('0')
    while len(payload2_bits) < 98:
        payload2_bits.append('0')
    
    payload1 = "".join(payload1_bits[:98])
    payload2 = "".join(payload2_bits[:98])
    
    # Step 3: Create slot bits using Golay encoding
    # Slot1: Golay(00000010) bits 3-12
    # Slot2: Golay(10111011) bits 10-19
    slot_data_1 = "00000010"
    slot_data_2 = "10111011"
    
    golay_slot1_full = golay_20_8_encode(slot_data_1)
    golay_slot2_full = golay_20_8_encode(slot_data_2)
    
    slot1 = golay_slot1_full[3:13]  # Bits 3-12 of Golay output
    slot2 = golay_slot2_full[10:20]  # Bits 10-19 of Golay output
    
    # Step 4: Standard DMR CSBK sync pattern (48 bits)
    sync_bits = "110101011101011111110111011111111101011101010111"
    
    # Step 5: Assemble the burst: p1 + slot1 + sync + slot2 + p2
    burst = payload1 + slot1 + sync_bits + slot2 + payload2
    
    return burst


def encode_field_bits(bit_string: str, encoding: str) -> str:
    """Apply the named encoding to *bit_string* and return the encoded bit string.

    This is the single authoritative dispatch function used by both the encoder
    UI and the validator UI so that encoding behaviour is always identical.

    Args:
        bit_string: Raw info bits as a string of '0' and '1'.
        encoding:   Encoding name as shown in the UI dropdown.

    Returns:
        Encoded bit string.

    Raises:
        ValueError: If the encoding name is unknown or bit_string is too long.
    """
    if not encoding or encoding == "None":
        return bit_string
    if encoding == "Hamming(7,4)":
        return hamming_7_4_encode(bit_string)
    if encoding == "Hamming(13,9)":
        return hamming_13_9_encode(bit_string)
    if encoding == "Hamming(15,11)":
        return hamming_15_11_encode(bit_string)
    if encoding == "BPTC(196,96)":
        return bptc_196_96_encode(bit_string)
    if encoding == "Golay(20,8)":
        return golay_20_8_encode(bit_string)
    if encoding == "Rate 3/4 Trellis":
        return rate_34_trellis_encode(bit_string)
    if encoding == "CRC-CCITT":
        return bit_string + crc_ccitt(bit_string)
    if encoding == "CRC-9":
        return bit_string + crc_9(bit_string)
    if encoding == "CRC-32":
        return bit_string + crc_32(bit_string)
    if encoding == "DMR CACH (CRC-9 over 15b)":
        if len(bit_string) > 15:
            raise ValueError("DMR CACH expects up to 15 input bits (then appends CRC-9 to form 24 bits).")
        padded = bit_string.ljust(15, "0")
        return padded + crc_9(padded)
    if encoding == "DMR Slot Type (Golay(20,8))":
        return golay_20_8_encode(bit_string)
    if encoding == "DMR Data Block (CRC-CCITT + BPTC196/96)":
        if len(bit_string) > 80:
            raise ValueError("DMR Data Block expects up to 80 input bits (adds CRC-16 then BPTC to 196 bits).")
        block_96 = bit_string.ljust(80, "0") + crc_ccitt(bit_string.ljust(80, "0"))
        return bptc_196_96_encode(block_96)
    if encoding == "CSBK Burst (264bit)":
        return csbk_burst_264bit_encode(bit_string)
    if encoding == "Convolutional Rate 1/2":
        return convolutional_rate12_encode(bit_string)
    if encoding == "Convolutional Rate 1/3":
        return convolutional_rate13_encode(bit_string)
    # Legacy aliases kept for backward compatibility with existing JSON files
    if encoding == "Hamming":
        return hamming_7_4_encode(bit_string)
    if encoding == "DMR Telemetry":
        return hamming_7_4_encode(bit_string)
    raise ValueError(f"Unknown encoding: {encoding}")


def convolutional_rate12_encode(bit_string: str) -> str:
    """K=7 rate-1/2 convolutional encoder (NASA/CCSDS standard).

    Generator polynomials (octal): g1 = 0o133 (binary 1011011),
                                   g2 = 0o171 (binary 1111001).
    Zero-tail terminated: 6 flush bits appended so the encoder returns
    to the all-zeros state.  Output length = 2 * (n + 6) bits.
    """
    g1 = 0b1011011  # 0o133
    g2 = 0b1111001  # 0o171
    padded = bit_string + "000000"
    state = 0
    out = []
    for bit_char in padded:
        bit = int(bit_char)
        state = ((state >> 1) | (bit << 6)) & 0x7F  # 7-bit shift register
        b1 = bin(state & g1).count("1") % 2
        b2 = bin(state & g2).count("1") % 2
        out.append(str(b1))
        out.append(str(b2))
    return "".join(out)


def convolutional_rate13_encode(bit_string: str) -> str:
    """K=7 rate-1/3 convolutional encoder.

    Generator polynomials (octal): g1 = 0o133, g2 = 0o171, g3 = 0o165
                                   (binary 1110101).
    Zero-tail terminated: 6 flush bits appended.
    Output length = 3 * (n + 6) bits.
    """
    g1 = 0b1011011  # 0o133
    g2 = 0b1111001  # 0o171
    g3 = 0b1110101  # 0o165
    padded = bit_string + "000000"
    state = 0
    out = []
    for bit_char in padded:
        bit = int(bit_char)
        state = ((state >> 1) | (bit << 6)) & 0x7F
        b1 = bin(state & g1).count("1") % 2
        b2 = bin(state & g2).count("1") % 2
        b3 = bin(state & g3).count("1") % 2
        out.append(str(b1))
        out.append(str(b2))
        out.append(str(b3))
    return "".join(out)
