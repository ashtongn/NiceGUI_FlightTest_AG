"""
lfsr_codes.py: general LFSR spreading-code library for DSSS.

What it gives the scenario builder and the DSSS builder panel:

  LFSR            user-defined polynomial (taps as exponent list, int mask or
                  hex) + initial state, Fibonacci or Galois form, any degree.
  m_sequence()    maximal-length sequence from a primitive polynomial; a table
                  of known primitive polynomials by degree (PRIMITIVE) so an
                  operator only has to pick n.
  gold_code()     Gold code from a preferred pair (GOLD_PAIRS by degree, or two
                  explicit polynomials) with a selectable offset; offset -1 / -2
                  return the two base m-sequences themselves.
  truncation      every generator takes `length` to cut a period down to the
                  chips/symbol the link wants (e.g. 64 of 127).
  code sets       make_code_set(): N codes for N users: distinct offsets of
                  one Gold generator, distinct seeds (phases) of one
                  m-sequence, distinct polynomials, or an explicit list.
  analysis        period(), balance(), autocorr(), crosscorr(), set_report():
                  peak off-zero auto-correlation and the pairwise peak
                  cross-correlation of a set, plus the equal-power multi-user
                  interference the set predicts, so an operator can see how
                  orthogonal the set is before keying it.

Conventions (shared with spreading_codes.py so the legacy seed / m / gold
presets are exactly expressible here):
  * a polynomial is x^n + sum c_k x^k + 1; `poly` accepts an exponent list
    [5, 2] (x^5 + x^2 + 1; the 0 term is implied), an int mask whose bit k-1
    stands for x^k (so x^5 + x^2 + 1: 0b10010 = 0x12), or that mask as a hex
    string "0x12".
  * the register state is an n-bit int; the first output chip is the state's
    LSB; state 0 is replaced by 1 (all-zero never leaves the LFSR).
  * chips are 0/1 bits internally; to_pm() maps 0 to +1, 1 to -1 like the rest
    of the modem (bit '0' is +1 in BPSK).

Fibonacci form (legacy spreading_codes._lfsr_mseq is exactly this):
    out = state & 1
    fb  = parity(state & FMASK)          # FMASK bit k set iff term x^k, k < n
    state = (state >> 1) | (fb << (n-1))
Galois form (same polynomial, same sequence up to a phase):
    out = state & 1
    state >>= 1
    if out: state ^= GMASK               # GMASK bit (n-1-k) set iff term x^k, 0 < k < n ... plus bit n-1
"""
from __future__ import annotations

from typing import Iterable, Optional, Sequence
import math
import numpy as np

# known primitive polynomials (exponent lists, x^n first, 0 implied)
# One minimal-weight primitive polynomial per degree 2..16 (Peterson / Stahnke
# tables). Any of these gives an m-sequence of period 2^n - 1.
PRIMITIVE = {
    2: [2, 1], 3: [3, 1], 4: [4, 1], 5: [5, 2], 6: [6, 1], 7: [7, 1],
    8: [8, 4, 3, 2], 9: [9, 4], 10: [10, 3], 11: [11, 2], 12: [12, 6, 4, 1],
    13: [13, 4, 3, 1], 14: [14, 10, 6, 1], 15: [15, 1], 16: [16, 12, 3, 1],
}
# A few alternates by degree (so "distinct polynomial" code sets have material).
PRIMITIVE_ALT = {
    5: [[5, 3], [5, 4, 3, 2], [5, 4, 2, 1], [5, 3, 2, 1]],
    6: [[6, 5], [6, 5, 2, 1], [6, 5, 3, 2], [6, 4, 3, 1]],
    7: [[7, 3], [7, 3, 2, 1], [7, 4], [7, 6], [7, 5, 2, 1], [7, 6, 4, 2]],
    8: [[8, 6, 5, 3], [8, 7, 6, 1], [8, 5, 3, 1], [8, 6, 5, 2]],
    9: [[9, 5], [9, 6, 4, 3], [9, 8, 5, 4], [9, 7, 2, 1]],
    10: [[10, 7], [10, 9, 8, 6, 3, 2], [10, 6, 5, 3, 2, 1], [10, 9, 4, 1]],
    11: [[11, 9], [11, 8, 5, 2], [11, 10, 3, 2], [11, 7, 3, 2]],
}
# Preferred pairs for Gold codes (three-valued cross-correlation). Gold codes
# exist for every n not divisible by 4; the pair (polynomial a, polynomial b).
# n = 10 is the GPS C/A pair (G1 = x^10+x^3+1, G2 = x^10+x^9+x^8+x^6+x^3+x^2+1).
GOLD_PAIRS = {
    5: ([5, 2], [5, 4, 3, 2]),
    6: ([6, 1], [6, 5, 2, 1]),
    7: ([7, 3], [7, 3, 2, 1]),
    9: ([9, 4], [9, 6, 4, 3]),
    10: ([10, 3], [10, 9, 8, 6, 3, 2]),
    11: ([11, 2], [11, 8, 5, 2]),
}


def gold_t(n: int) -> int:
    """The Gold bound t(n): cross-correlation of a preferred pair takes only
    the values -1, -t(n), t(n) - 2."""
    return (1 << ((n + 1) // 2)) + 1 if n % 2 else (1 << ((n + 2) // 2)) + 1


# polynomial parsing

def parse_poly(poly, degree: Optional[int] = None) -> tuple[int, list[int]]:
    """Normalise a polynomial spec to (degree n, sorted exponent list incl. n
    and 0). Accepts an exponent list / tuple, an int mask (bit k-1 stands for x^k),
    a hex / binary string of that mask, or a 'x^5+x^2+1' style string."""
    if isinstance(poly, str):
        s = poly.strip().lower().replace(" ", "")
        if s.startswith("0x"):
            mask = int(s, 16)
            return parse_poly(mask, degree)
        if s.startswith("0b"):
            return parse_poly(int(s, 2), degree)
        if "x" in s:
            exps = []
            for term in s.replace("-", "+").split("+"):
                if not term:
                    continue
                if term == "1":
                    exps.append(0)
                elif term == "x":
                    exps.append(1)
                else:
                    exps.append(int(term.split("^")[1]))
            return parse_poly(exps, degree)
        # "5,2" or "5 2"
        exps = [int(t) for t in s.replace(";", ",").split(",") if t]
        return parse_poly(exps, degree)
    if isinstance(poly, (int, np.integer)):
        mask = int(poly)
        if mask <= 0:
            raise ValueError("polynomial mask must be > 0")
        exps = [k + 1 for k in range(mask.bit_length()) if (mask >> k) & 1]
        n = int(degree) if degree else max(exps)
        if n < max(exps):
            raise ValueError(f"degree {n} below the highest tap x^{max(exps)}")
        exps = sorted(set(exps + [n, 0]))
        return n, exps
    exps = [int(e) for e in poly]
    if not exps:
        raise ValueError("empty polynomial")
    n = int(degree) if degree else max(exps)
    if n < 2:
        raise ValueError("LFSR degree must be >= 2")
    if max(exps) > n:
        raise ValueError(f"tap x^{max(exps)} exceeds degree {n}")
    exps = sorted(set(exps + [n, 0]))
    return n, exps


def poly_mask(poly, degree: Optional[int] = None) -> int:
    """Int mask form (bit k-1 stands for x^k) of a polynomial, for display as hex."""
    n, exps = parse_poly(poly, degree)
    return sum(1 << (k - 1) for k in exps if k >= 1)


def poly_str(poly, degree: Optional[int] = None) -> str:
    n, exps = parse_poly(poly, degree)
    terms = []
    for k in sorted(exps, reverse=True):
        terms.append("1" if k == 0 else ("x" if k == 1 else f"x^{k}"))
    return "+".join(terms)


def _fib_mask(n: int, exps: Sequence[int]) -> int:
    # legacy convention: bit k stands for term x^k for k < n (x^n implicit)
    return sum(1 << k for k in exps if k < n)


def _galois_mask(n: int, exps: Sequence[int]) -> int:
    # mirror of the Fibonacci taps: term x^k (0 < k < n): bit n-1-k; plus the
    # x^0 term lands on bit n-1 (the injected feedback)
    m = 1 << (n - 1)
    for k in exps:
        if 0 < k < n:
            m |= 1 << (n - 1 - k)
    return m


class LFSR:
    """A linear-feedback shift register. Iterate chips with step() / run()."""

    def __init__(self, poly, state: int = 1, form: str = "fibonacci",
                 degree: Optional[int] = None):
        self.n, self.exps = parse_poly(poly, degree)
        self.form = str(form).lower()
        if self.form not in ("fibonacci", "galois"):
            raise ValueError("form must be 'fibonacci' or 'galois'")
        self.period = (1 << self.n) - 1
        self.fmask = _fib_mask(self.n, self.exps)
        self.gmask = _galois_mask(self.n, self.exps)
        st = int(state) & self.period
        self.state = st or 1
        self.initial_state = self.state

    def reset(self) -> None:
        self.state = self.initial_state

    def step(self) -> int:
        s = self.state
        out = s & 1
        if self.form == "fibonacci":
            fb = bin(s & self.fmask).count("1") & 1
            self.state = (s >> 1) | (fb << (self.n - 1))
        else:
            s >>= 1
            if out:
                s ^= self.gmask
            self.state = s
        return out

    def run(self, length: Optional[int] = None) -> np.ndarray:
        """`length` chips (default one full period) as 0/1 int8."""
        L = int(length) if length else self.period
        out = np.empty(L, dtype=np.int8)
        for i in range(L):
            out[i] = self.step()
        return out

    def is_maximal(self) -> bool:
        """True when the register visits every non-zero state (primitive poly)."""
        self.reset()
        seen = {self.state}
        for _ in range(self.period):
            self.step()
            if self.state in seen:
                break
            seen.add(self.state)
        self.reset()
        return len(seen) == self.period


# sequences

def m_sequence(poly=None, n: Optional[int] = None, state: int = 1,
               form: str = "fibonacci", length: Optional[int] = None) -> np.ndarray:
    """One (truncated) period of an m-sequence. Give `poly`, or just `n` to use
    PRIMITIVE[n]. Raises if the polynomial is not primitive."""
    if poly is None:
        if n is None or int(n) not in PRIMITIVE:
            raise ValueError(f"no known primitive polynomial for n={n} (have {sorted(PRIMITIVE)})")
        poly = PRIMITIVE[int(n)]
    reg = LFSR(poly, state, form, degree=n)
    if not reg.is_maximal():
        raise ValueError(f"{poly_str(reg.exps)} is not primitive (period < 2^{reg.n}-1)")
    return reg.run(length)


def gold_code(n: Optional[int] = None, offset: int = 0, polys=None, state: int = 1,
              state2: Optional[int] = None, form: str = "fibonacci",
              length: Optional[int] = None) -> np.ndarray:
    """Gold code a XOR roll(b, -offset); offset -1 gives a itself, -2 gives b itself.
    `polys` = (poly_a, poly_b) overrides the GOLD_PAIRS table."""
    if polys is None:
        if n is None or int(n) not in GOLD_PAIRS:
            raise ValueError(f"no preferred pair for n={n} (have {sorted(GOLD_PAIRS)}; "
                             "n divisible by 4 has no Gold codes)")
        polys = GOLD_PAIRS[int(n)]
    pa, pb = polys
    a = LFSR(pa, state, form, degree=n)
    b = LFSR(pb, state if state2 is None else state2, form, degree=n)
    if a.n != b.n:
        raise ValueError("Gold pair polynomials must have the same degree")
    sa, sb = a.run(), b.run()
    off = int(offset)
    if off == -1:
        g = sa
    elif off == -2:
        g = sb
    else:
        g = sa ^ np.roll(sb, -(off % a.period))
    if length:
        g = g[: int(length)]
    return g.astype(np.int8)


def to_pm(bits) -> np.ndarray:
    """0/1 chips to +1/-1 (bit 0 is +1, like the BPSK convention)."""
    return (1 - 2 * np.asarray(bits, dtype=np.int8)).astype(np.float64)


def from_pm(chips) -> np.ndarray:
    return (np.asarray(chips, dtype=np.float64) < 0).astype(np.int8)


# code specs (the dict form carried by plans)
# {"family": "lfsr", "poly": [5,2] | 0x12 | "0x12", "state": 1, "form": "fibonacci",
#  "length": 31, "degree": 5, "offset": 0}
#     one LFSR; "offset" discards that many chips first (a phase shift)
# {"family": "gold", "n": 6, "index": 3}                   (legacy, kept as is)
# {"family": "gold", "polys": [[6,1],[6,5,2,1]], "offset": 3, "state": 1,
#  "state2": 1, "form": "fibonacci", "length": 63}         (explicit pair)

def lfsr_spec_to_pm(spec: dict) -> np.ndarray:
    fam = str(spec.get("family", "lfsr")).lower()
    form = str(spec.get("form", "fibonacci"))
    length = spec.get("length")
    if fam == "lfsr":
        poly = spec.get("poly", spec.get("taps"))
        if poly is None:
            raise ValueError("lfsr code spec needs 'poly' (taps)")
        reg = LFSR(poly, int(spec.get("state", spec.get("seed", 1)) or 1), form,
                   degree=spec.get("degree"))
        off = int(spec.get("offset", 0) or 0)
        if off:
            reg.run(off % reg.period)
        return to_pm(reg.run(length))
    if fam == "gold" and (spec.get("polys") or spec.get("poly_a")):
        polys = spec.get("polys") or (spec["poly_a"], spec["poly_b"])
        return to_pm(gold_code(n=spec.get("degree") or spec.get("n"),
                               offset=int(spec.get("offset", spec.get("index", 0)) or 0),
                               polys=polys, state=int(spec.get("state", spec.get("seed", 1)) or 1),
                               state2=spec.get("state2", spec.get("seed2")), form=form,
                               length=length))
    raise ValueError(f"not an lfsr-form code spec: {spec}")


def legacy_to_lfsr(spec: dict) -> dict:
    """Express a legacy spreading_codes spec (seed / m / gold presets) in the
    explicit LFSR form that reproduces its chips exactly. 'seed' (numpy PN)
    has no LFSR behind it, so it is returned as an explicit custom chip list."""
    from dpmg.modulation.spreading_codes import resolve_code, _MSEQ_TAPS, _lfsr_mseq
    fam = str(spec.get("family", "seed")).lower()
    if fam in ("m", "mseq", "m-sequence"):
        n = int(spec.get("n", 7))
        exps = [n] + [k for k in range(n) if (_MSEQ_TAPS[n][0] >> k) & 1]
        return {"family": "lfsr", "poly": sorted(exps, reverse=True), "degree": n,
                "state": int(spec.get("seed", 1)), "form": "fibonacci", "length": (1 << n) - 1}
    if fam == "gold":
        n = int(spec.get("n", 7))
        t0, t1 = _MSEQ_TAPS[n]
        pa = sorted([n] + [k for k in range(n) if (t0 >> k) & 1], reverse=True)
        pb = sorted([n] + [k for k in range(n) if (t1 >> k) & 1], reverse=True)
        idx = int(spec.get("index", 0))
        return {"family": "gold", "polys": [pa, pb], "degree": n, "offset": max(0, idx),
                "state": int(spec.get("seed", 1)), "state2": int(spec.get("seed2", spec.get("seed", 1))),
                "form": "fibonacci", "length": (1 << n) - 1}
    chips = resolve_code(spec, int(spec.get("length", 15)))
    return {"family": "custom", "chips": "".join("+" if c > 0 else "-" for c in chips)}


# code sets

def make_code_set(setdef: dict) -> list[dict]:
    """Expand a code-set definition into N explicit code specs (one per user).

    {"kind": "gold", "n": 6, "count": 4, "offsets": [0,1,2,3], "length": 63,
     "polys": [...optional...]}                      distinct Gold offsets
    {"kind": "mseq_phases", "poly": [6,1], "count": 4, "step": 16, "states": [...],
     "length": 63}                                   distinct phases of one m-seq
    {"kind": "polys", "polys": [[7,1],[7,3],[7,4]], "state": 1, "length": 127}
                                                     distinct primitive polynomials
    {"kind": "list", "codes": [{...}, {...}]}        explicit specs
    Every returned spec is self-contained (family lfsr / gold with polys):
    nothing downstream has to know the set existed."""
    kind = str(setdef.get("kind", "gold")).lower()
    count = int(setdef.get("count") or 0)
    length = setdef.get("length")
    form = str(setdef.get("form", "fibonacci"))
    state = int(setdef.get("state", setdef.get("seed", 1)) or 1)
    out = []
    if kind == "gold":
        n = int(setdef.get("n") or setdef.get("degree") or 6)
        polys = setdef.get("polys") or list(GOLD_PAIRS.get(n, ()))
        if not polys:
            raise ValueError(f"no Gold preferred pair for n={n}")
        offsets = setdef.get("offsets")
        if offsets is None:
            if count <= 0:
                raise ValueError("gold code set needs 'count' or 'offsets'")
            offsets = list(range(count))
        for off in offsets:
            out.append({"family": "gold", "polys": [list(polys[0]), list(polys[1])],
                        "degree": n, "offset": int(off), "state": state,
                        "state2": int(setdef.get("state2", state)), "form": form,
                        "length": int(length) if length else (1 << n) - 1})
    elif kind in ("mseq_phases", "seeds", "phases"):
        poly = setdef.get("poly") or PRIMITIVE[int(setdef.get("n") or 6)]
        n, _ = parse_poly(poly, setdef.get("degree"))
        reg = LFSR(poly, state, form, degree=n)
        if not reg.is_maximal():
            raise ValueError(f"{poly_str(reg.exps)} is not primitive")
        states = setdef.get("states")
        if states is None:
            if count <= 0:
                raise ValueError("mseq_phases set needs 'count' or 'states'")
            step = int(setdef.get("step") or max(1, reg.period // count))
            states = []
            reg.reset()
            for k in range(count):
                states.append(reg.state)
                reg.run(step)
        for st in states:
            out.append({"family": "lfsr", "poly": list(reg.exps)[::-1], "degree": n,
                        "state": int(st), "form": form,
                        "length": int(length) if length else reg.period})
    elif kind == "polys":
        polys = setdef.get("polys")
        if not polys:
            n = int(setdef.get("n") or 7)
            polys = [PRIMITIVE[n]] + list(PRIMITIVE_ALT.get(n, []))
            if count:
                polys = polys[:count]
        for p in polys:
            n, exps = parse_poly(p, setdef.get("degree"))
            out.append({"family": "lfsr", "poly": sorted(exps, reverse=True), "degree": n,
                        "state": state, "form": form,
                        "length": int(length) if length else (1 << n) - 1})
    elif kind == "list":
        for c in setdef.get("codes") or []:
            out.append(dict(c))
    else:
        raise ValueError(f"unknown code set kind {kind!r}")
    if count and len(out) < count:
        raise ValueError(f"code set gives {len(out)} codes, {count} wanted")
    return out


# analysis

def period(seq) -> int:
    """Smallest cyclic period of a sequence (len(seq) if it has none)."""
    s = np.asarray(seq)
    L = len(s)
    for p in range(1, L):
        if L % p == 0 and np.array_equal(s, np.roll(s, p)):
            return p
    return L


def balance(seq) -> dict:
    """Ones / zeros count and the +/-1 sum (0 = balanced; an m-sequence has one
    more 1 than 0, sum -1 in the +/-1 domain)."""
    b = np.asarray(seq)
    if b.dtype.kind == "f":
        b = from_pm(b)
    ones = int(np.sum(b))
    return {"ones": ones, "zeros": int(len(b) - ones), "sum_pm": int(len(b) - 2 * ones)}


def _pm(seq) -> np.ndarray:
    s = np.asarray(seq)
    return s.astype(np.float64) if s.dtype.kind == "f" else to_pm(s)


def autocorr(seq) -> np.ndarray:
    """Periodic auto-correlation at lags 0..L-1 (chip units, unnormalised)."""
    x = _pm(seq)
    X = np.fft.fft(x)
    return np.round(np.real(np.fft.ifft(X * np.conj(X)))).astype(np.int64)


def crosscorr(a, b) -> np.ndarray:
    """Periodic cross-correlation sum_k a[k] b[k+lag] at lags 0..L-1."""
    x, y = _pm(a), _pm(b)
    if len(x) != len(y):
        raise ValueError("cross-correlation needs equal lengths")
    return np.round(np.real(np.fft.ifft(np.conj(np.fft.fft(x)) * np.fft.fft(y)))).astype(np.int64)


def peak_offzero(seq) -> int:
    ac = autocorr(seq)
    return int(np.max(np.abs(ac[1:]))) if len(ac) > 1 else 0


def run_lengths(seq) -> dict:
    """Histogram of run lengths of equal chips (an m-sequence halves per length)."""
    b = np.asarray(seq)
    if b.dtype.kind == "f":
        b = from_pm(b)
    out: dict = {}
    run = 1
    for i in range(1, len(b)):
        if b[i] == b[i - 1]:
            run += 1
        else:
            out[run] = out.get(run, 0) + 1
            run = 1
    out[run] = out.get(run, 0) + 1
    return dict(sorted(out.items()))


def set_report(codes: Sequence, names: Optional[Sequence[str]] = None) -> dict:
    """Orthogonality report for a code set (list of +/-1 arrays or 0/1 arrays).

    Returns {"n": N, "length": L, "names": [...],
             "auto_peak": [peak off-zero |autocorr| per code],
             "balance": [sum_pm per code],
             "xcorr_peak": NxN matrix of max |crosscorr| over all lags (diag = L),
             "xcorr_rms": NxN rms crosscorr over lags,
             "worst_pair": (i, j, peak), "worst_db": 20log10(peak/L),
             "mui_snr_db": [per code: 10log10(L^2 / sum_j rms_ij^2)]: the
             despread SNR an equal-power, chip-asynchronous overlay of the whole
             set leaves this user, before noise}"""
    pm = [_pm(c) for c in codes]
    N = len(pm)
    if N == 0:
        return {"n": 0, "length": 0, "names": [], "auto_peak": [], "balance": [],
                "xcorr_peak": [], "xcorr_rms": [], "worst_pair": None, "worst_db": None,
                "mui_snr_db": []}
    L = len(pm[0])
    names = list(names) if names else [f"C{i}" for i in range(N)]
    peak = np.zeros((N, N)); rms = np.zeros((N, N))
    for i in range(N):
        peak[i, i] = L
        rms[i, i] = L
        for j in range(i + 1, N):
            if len(pm[j]) != L:
                raise ValueError("all codes in a set must share one length")
            xc = crosscorr(pm[i], pm[j]).astype(np.float64)
            peak[i, j] = peak[j, i] = float(np.max(np.abs(xc)))
            rms[i, j] = rms[j, i] = float(np.sqrt(np.mean(xc ** 2)))
    worst = None
    for i in range(N):
        for j in range(i + 1, N):
            if worst is None or peak[i, j] > worst[2]:
                worst = (i, j, float(peak[i, j]))
    mui = []
    for i in range(N):
        it = sum(rms[i, j] ** 2 for j in range(N) if j != i)
        mui.append(float(10 * math.log10(L * L / it)) if it > 0 else float("inf"))
    return {"n": N, "length": L, "names": names,
            "auto_peak": [peak_offzero(c) for c in pm],
            "balance": [balance(c)["sum_pm"] for c in pm],
            "xcorr_peak": peak.astype(int).tolist(), "xcorr_rms": np.round(rms, 2).tolist(),
            "worst_pair": worst,
            "worst_db": (float(20 * math.log10(max(worst[2], 1e-9) / L)) if worst else None),
            "mui_snr_db": mui}


def is_preferred_pair(poly_a, poly_b, n: Optional[int] = None) -> bool:
    """True when the two m-sequences have a three-valued cross-correlation
    {-1, -t(n), t(n)-2} (the Gold condition)."""
    a = m_sequence(poly_a, n=n); b = m_sequence(poly_b, n=n)
    deg = parse_poly(poly_a, n)[0]
    t = gold_t(deg)
    vals = set(int(v) for v in crosscorr(a, b))
    return vals <= {-1, -t, t - 2}
