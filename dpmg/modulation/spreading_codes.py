"""
spreading_codes.py: DSSS spreading-code library.

Produces a +/-1 chip sequence from a user-chosen family so both ends despread
with the same code. Families:
  seed     : the legacy numpy-rng PN (back-compat; length = spread_factor).
  m        : maximal-length LFSR sequence (m-sequence), length 2^n - 1.
  gold     : Gold code from a preferred pair of m-sequences, length 2^n - 1
             (a family index selects which shifted sum). Good cross-correlation.
  custom   : an explicit chip string ('+-+...' or '10...' or a list of 0/1).
  lfsr     : explicit polynomial (taps) / initial state / form
             (fibonacci | galois) / truncation length / phase offset, and
             "gold" with explicit "polys" + "offset" (dpmg.modulation.lfsr_codes).
             The legacy m / gold presets are reproduced exactly by
             lfsr_codes.legacy_to_lfsr(spec). The legacy gold table's
             second polynomial is not primitive for n = 6, 7, 9, 10 (only the
             n = 5 pair is a preferred pair); its chips are kept unchanged for
             deployed-plan compatibility, new work should use the lfsr form.

resolve_code(spec): np.ndarray of +/-1 is the single entry point used by the
modulator and demodulator, so a plan/GUI only has to carry a small dict.
"""
from __future__ import annotations

from typing import Optional
import numpy as np

# Preferred-pair LFSR tap sets (feedback polynomials, high bit = stage n) that
# generate maximal-length sequences and form Gold pairs. Keyed by register
# length n; values are the two tap masks over stages 1..n.
_MSEQ_TAPS = {
    5:  (0b00101, 0b01111),
    6:  (0b000011, 0b010011),
    7:  (0b0000011, 0b0011011),
    8:  (0b00011101, 0b00101011),
    9:  (0b000010001, 0b000110111),
    10: (0b0000001001, 0b0010011001),
    11: (0b00000000101, 0b00000101101),
}


def _lfsr_mseq(n: int, taps: int, seed: int = 1) -> np.ndarray:
    """One period (2^n - 1) of an m-sequence as 0/1 bits (Fibonacci LFSR)."""
    length = (1 << n) - 1
    state = seed & length or 1
    out = np.empty(length, dtype=np.int8)
    for i in range(length):
        out[i] = state & 1
        fb = bin(state & taps).count("1") & 1
        state = (state >> 1) | (fb << (n - 1))
    return out


def _bits_to_pm(bits: np.ndarray) -> np.ndarray:
    return (1 - 2 * np.asarray(bits, dtype=np.int8)).astype(np.float64)  # 0 to +1,1 to -1


def _parse_custom(chips) -> np.ndarray:
    if isinstance(chips, (list, tuple, np.ndarray)):
        arr = np.asarray(chips)
        if arr.dtype.kind in "fi":
            return np.where(arr > 0, 1.0, -1.0).astype(np.float64)
        chips = "".join(str(c) for c in chips)
    s = str(chips).strip()
    out = []
    for ch in s:
        if ch in "1+":
            out.append(1.0 if ch == "+" else -1.0)   # '1' to -1 (bit), '+' to +1
        elif ch in "0-":
            out.append(1.0 if ch == "0" else -1.0)    # '0' to +1 (bit), '-' to -1
    if not out:
        raise ValueError(f"empty/invalid custom chip string: {chips!r}")
    return np.asarray(out, dtype=np.float64)


def resolve_code(spec: Optional[dict], length_hint: int = 15) -> np.ndarray:
    """Return a +/-1 chip array from a code spec dict.

    spec = None or {}: legacy seed PN of length `length_hint`.
    {"family":"seed","seed":int,"length":int}
    {"family":"m","n":int,"seed":int}                  # length 2^n-1
    {"family":"gold","n":int,"index":int,"seed":int}   # length 2^n-1
    {"family":"custom","chips":"+-+.." | "101.." | [..]}
    """
    spec = spec or {}
    fam = str(spec.get("family", "seed")).lower()

    if fam in ("seed", "pn", ""):
        n = int(spec.get("length", length_hint))
        seed = int(spec.get("seed", 1))
        rng = np.random.default_rng(seed)
        return (rng.integers(0, 2, n) * 2 - 1).astype(np.float64)

    if fam in ("m", "mseq", "m-sequence"):
        n = int(spec.get("n", 7))
        if n not in _MSEQ_TAPS:
            raise ValueError(f"m-sequence n={n} unsupported (have {sorted(_MSEQ_TAPS)})")
        return _bits_to_pm(_lfsr_mseq(n, _MSEQ_TAPS[n][0], int(spec.get("seed", 1))))

    if fam in ("gold",):
        if spec.get("polys") or spec.get("poly_a"):           # explicit Gold pair (lfsr_codes)
            from dpmg.modulation.lfsr_codes import lfsr_spec_to_pm
            return lfsr_spec_to_pm(spec)
        n = int(spec.get("n", 7))
        if n not in _MSEQ_TAPS:
            raise ValueError(f"gold n={n} unsupported (have {sorted(_MSEQ_TAPS)})")
        t0, t1 = _MSEQ_TAPS[n]
        a = _lfsr_mseq(n, t0, int(spec.get("seed", 1)))
        b = _lfsr_mseq(n, t1, int(spec.get("seed2", spec.get("seed", 1))))
        length = (1 << n) - 1
        idx = int(spec.get("index", 0))
        if idx <= 0:
            g = a ^ b
        else:
            g = a ^ np.roll(b, -(idx % length))
        return _bits_to_pm(g)

    if fam in ("custom", "chips"):
        return _parse_custom(spec.get("chips", ""))

    if fam == "lfsr":
        # Explicit polynomial / state / form / length / offset
        from dpmg.modulation.lfsr_codes import lfsr_spec_to_pm
        return lfsr_spec_to_pm(spec)

    raise ValueError(f"unknown spreading-code family {fam!r}")


def describe_code(spec: Optional[dict], length_hint: int = 15) -> str:
    """One-line human label for a code spec (GUI tables, plan names)."""
    spec = spec or {}
    fam = str(spec.get("family", "seed")).lower()
    try:
        n = code_len(spec, length_hint)
    except Exception as e:                                   # noqa: BLE001
        return f"{fam}: invalid ({e})"
    if fam == "lfsr":
        from dpmg.modulation.lfsr_codes import poly_str, poly_mask
        return (f"lfsr {poly_str(spec.get('poly', spec.get('taps')), spec.get('degree'))} "
                f"(0x{poly_mask(spec.get('poly', spec.get('taps')), spec.get('degree')):X}) "
                f"state {spec.get('state', spec.get('seed', 1))} {spec.get('form', 'fibonacci')}"
                f"{' off ' + str(spec['offset']) if spec.get('offset') else ''} L={n}")
    if fam == "gold":
        if spec.get("polys"):
            from dpmg.modulation.lfsr_codes import poly_str
            pa, pb = spec["polys"]
            return (f"gold {poly_str(pa)} x {poly_str(pb)} offset "
                    f"{spec.get('offset', spec.get('index', 0))} L={n}")
        return f"gold n={spec.get('n', 7)} index {spec.get('index', 0)} L={n} (legacy table)"
    if fam in ("m", "mseq", "m-sequence"):
        return f"m-sequence n={spec.get('n', 7)} seed {spec.get('seed', 1)} L={n}"
    if fam in ("custom", "chips"):
        return f"custom chips L={n}"
    return f"seed PN seed {spec.get('seed', 1)} L={n}"


def code_len(spec: Optional[dict], length_hint: int = 15) -> int:
    return int(len(resolve_code(spec, length_hint)))
