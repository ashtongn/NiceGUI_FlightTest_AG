"""
dsss_builder_validate.py: input checks for the DSSS builder.

One pure function, validate_builder(spec), looks at the users-form scenario
JSON the engineering-view DSSS builder panel describes (the same dict
dsss_builder_tab._spec_from_state returns and gen_scenario_json.expand_users
reads: "code_set" / "code_sets" + "dsss" + "users" + sample_rate_hz /
center_freq_hz / loop_s / name) and returns a list of

    (level, field, message)

level "error": invalid or physically impossible; the panel blocks Build
                   codes (code-set fields only), Preview, Scenario JSON, Save,
                   Build on radio and Replay until it is fixed.
level "warning": allowed, but the result will probably not be what the
                   operator expects (shown amber).
level "info": only with with_info=True: what was checked and passed.

`field` names the panel input the message is about (degree, count, length,
poly, poly2, seed, form, set_kind, symbol_rate, samples_per_chip, offset_hz,
spacing_hz, carrier, amplitude, stagger, sample_rate, center, loop_s, name,
custom, users, code_set) or "user:<id>" for one user's row.

Messages say what is wrong and what to change. Nothing here builds IQ or
talks to the radio; code chips are generated (cached) only for the
correlation / interference estimate, within a size budget.

    from dsss_builder_validate import validate_builder, has_errors
    issues = validate_builder(spec, radio_rate=host_rate)
"""
from __future__ import annotations

import json
import math
import re
from functools import lru_cache
from typing import Optional

import numpy as np

from dpmg.modulation import lfsr_codes as L

ERROR, WARNING, INFO = "error", "warning", "info"

# limits (each one names where it comes from)
MIN_DEGREE, MAX_DEGREE = min(L.PRIMITIVE), max(L.PRIMITIVE)   # library table 2..16
MIN_CHIPS = 7                     # below this a code barely spreads (processing gain < 8.5 dB)
MIN_SPC = 2                       # DSSSDemodulator / SPREAD_FSOUT floor: 2 samples per chip
USABLE_GUARD = 0.95               # gen_scenario_json.validate_spec: usable band = 0.95 * fs/2
MAX_PAYLOAD_BYTES = 255           # frame_codec: 8-bit LEN field
SYNC_BITS = 16                    # frame_codec.SYNC_BITS (uncoded)
LEAD_TAIL_SYMBOLS = 8 + 4         # modem.ModemConfig lead_pad_bits + tail_pad_bits (in symbol strides)
CARRIERS = ("bpsk", "qpsk", "oqpsk", "pi4qpsk")
DUAL_CODE_CARRIERS = ("qpsk", "oqpsk")            # spread_spectrum.DSSSModulator
# despread SINR a user needs to decode (docs/DSSS_BUILDER.md "rules of thumb")
DECODE_SINR_DB = {"bpsk": 8.0, "qpsk": 11.0, "oqpsk": 11.0, "pi4qpsk": 11.0}
CAPACITY_MARGIN_DB = 2.0          # equal-power rule of thumb L/(N-1) must clear the need by this
# live channelizer decode window (channelizer_worker [NEED] check)
LIVE_FRAME_LEN = 1_048_576        # dpmg_service: ChannelizerWorker(frame_len=1048576)
GPU_MAX_OUT = 32_768              # gpu_service.MAX_OUT: samples returned per channel per frame
WORKER_FRAME_BITS = 144           # channelizer_worker default frame_bits (plans carry none)
NEED_MARGIN_SYMBOLS = 32
EDGE = 4
# analysis budget: chips generated for the MUI estimate on every input change
MAX_ANALYSIS_LEN = 4095
MAX_ANALYSIS_CHIPS = 300_000      # users x code length
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


# small helpers

def _num(v) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, str):
        v = v.strip()
        if not v:
            return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _show(v) -> str:
    """A value for a message: '(empty)' for a blank input, 5 for 5.0."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return "(empty)"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return repr(v) if isinstance(v, str) else str(v)


def _int(v) -> Optional[int]:
    """An integral value (5 or 5.0) as int, else None."""
    f = _num(v)
    if f is None or abs(f - round(f)) > 1e-9:
        return None
    return int(round(f))


def _hz(f: float) -> str:
    a = abs(f)
    if a >= 1e6:
        return f"{f / 1e6:.4g} MHz"
    if a >= 1e3:
        return f"{f / 1e3:.4g} kHz"
    return f"{f:.4g} Hz"


def _chips(f: float) -> str:
    return f"{f / 1e6:.4g} Mchip/s" if f >= 1e6 else f"{f / 1e3:.4g} kchip/s"


def _sps(f: float) -> str:
    return f"{f / 1e6:.4g} MSps" if f >= 1e6 else f"{f / 1e3:.4g} kSps"


def _gold_ns() -> str:
    ns = sorted(L.GOLD_PAIRS)
    return ", ".join(str(n) for n in ns[:-1]) + f" or {ns[-1]}"


def _poly_text(p) -> str:
    return p if isinstance(p, str) else json.dumps(p)


@lru_cache(maxsize=512)
def _poly_info(key: str, degree: Optional[int]):
    """(n, exps, period of state 1) for a polynomial spec (json key); raises
    ValueError when it cannot be parsed. Primitive iff period == 2^n - 1."""
    poly = json.loads(key)
    n, exps = L.parse_poly(poly, degree)
    if n < 2:
        raise ValueError("degree below 2")
    if n > MAX_DEGREE:
        return n, tuple(exps), None              # too long to walk live
    reg = L.LFSR(exps, 1, "fibonacci", degree=n)
    start = reg.state
    full = (1 << n) - 1
    p = 0
    for p in range(1, full + 1):
        reg.step()
        if reg.state == start:
            break
    return n, tuple(exps), p


def poly_info(poly, degree: Optional[int] = None):
    """Public wrapper: (n, exps, period) or raises ValueError with a clear text."""
    try:
        return _poly_info(json.dumps(poly), degree)
    except (ValueError, TypeError, IndexError, json.JSONDecodeError) as e:
        raise ValueError(str(e)) from None


@lru_cache(maxsize=4096)
def _code_chips(key: str) -> np.ndarray:
    from dpmg.modulation.spreading_codes import resolve_code
    return np.asarray(resolve_code(json.loads(key)), dtype=np.float64)


def code_chips(spec: dict) -> np.ndarray:
    return _code_chips(json.dumps(spec, sort_keys=True))


@lru_cache(maxsize=64)
def _preferred(ka: str, kb: str, n: int) -> bool:
    return bool(L.is_preferred_pair(json.loads(ka), json.loads(kb), n))


# the checker

class _G(tuple):
    """Placeholder for a per-user message on a shared input: identical
    messages for several users are merged into one ("{who}": the users)."""


class _Issues(list):
    def __init__(self):
        super().__init__()
        self._grp = {}

    def user(self, level, field, uid, tmpl):
        key = (level, field, tmpl)
        if key in self._grp:
            self._grp[key].append(uid)
        else:
            self._grp[key] = [uid]
            self.append(_G(key))

    def finish(self, n_users: int) -> list:
        res = []
        for it in self:
            if isinstance(it, _G):
                who = self._grp[tuple(it)]
                if len(who) == 1:
                    w = who[0]
                elif len(who) == n_users and n_users > 2:
                    w = f"all {n_users} users"
                elif len(who) <= 4:
                    w = ", ".join(who)
                else:
                    w = f"{who[0]}..{who[-1]} ({len(who)} users)"
                res.append((it[0], it[1], it[2].replace("{who}", w)))
            else:
                res.append(tuple(it))
        return res

    def err(self, field, msg):
        self.append((ERROR, field, msg))

    def warn(self, field, msg):
        self.append((WARNING, field, msg))

    def info(self, field, msg):
        self.append((INFO, field, msg))

    def n_err(self):
        return sum(1 for lv, _f, _m in self if lv == ERROR)


def _check_state(st, n: int, field: str, out: _Issues, what="state / seed") -> bool:
    if st is None:
        return True
    s = _int(st)
    top = (1 << n) - 1
    if s is None:
        out.err(field, f"{what} {_show(st)} is not a whole number: enter 1..{top}")
        return False
    if s == 0:
        out.err(field, f"{what} 0 is the all-zero register, which an LFSR never leaves: "
                       f"use any value 1..{top}")
        return False
    if s < 0 or s > top:
        out.err(field, f"{what} {s} does not fit a {n}-bit register: use 1..{top}")
        return False
    return True


def _check_length(length, period: Optional[int], field: str, out: _Issues, label="") -> Optional[int]:
    """Truncated length check. Returns the effective code length (or None)."""
    if length in (None, "", 0, 0.0):
        return period
    ln = _int(length)
    if ln is None or ln < 0:
        out.err(field, f"{label}length {_show(length)} must be a whole number >= {MIN_CHIPS} "
                       "(0 = the full period)")
        return None
    if ln == 0:
        return period
    if period is not None and ln > period:
        out.err(field, f"{label}length {ln} is longer than the {period}-chip period: the code would "
                       f"repeat inside one symbol; use 0 (full period) or at most {period}")
        return None
    if ln < MIN_CHIPS:
        out.err(field, f"{label}length {ln} chips is too short to spread (processing gain "
                       f"{10 * math.log10(max(ln, 1)):.1f} dB): use at least {MIN_CHIPS}")
        return None
    return ln


def _check_poly(poly, field: str, out: _Issues, degree: Optional[int] = None,
                label="polynomial") -> Optional[tuple]:
    """Parse + primitive check. Returns (n, exps, period) or None (error added)."""
    if poly is None or (isinstance(poly, str) and not poly.strip()):
        out.err(field, f"{label} is empty: enter taps like 5,2, a hex mask like 0x12 or x^5+x^2+1")
        return None
    try:
        n0, _e0, _p0 = poly_info(poly, None)
    except ValueError as e:
        out.err(field, f"{label} {_show(_poly_text(poly))} cannot be read ({e}): enter taps like 5,2, "
                       "a hex mask like 0x12 or x^5+x^2+1")
        return None
    if degree is not None and n0 > degree:
        out.err(field, f"{label} {_poly_text(poly)} has a tap x^{n0} beyond degree n={degree}: "
                       f"set n={n0} or drop the taps above x^{degree}")
        return None
    if degree is not None and n0 != degree:
        out.err(field, f"{label} {L.poly_str(poly)} is degree {n0} but n={degree}: set n={n0} "
                       f"or enter a degree-{degree} polynomial (e.g. "
                       f"{','.join(str(k) for k in L.PRIMITIVE.get(degree, []))})")
        return None
    if n0 < MIN_DEGREE or n0 > MAX_DEGREE:
        out.err(field, f"{label} degree {n0} is outside the supported {MIN_DEGREE}..{MAX_DEGREE}")
        return None
    n, exps, per = poly_info(poly, None)
    full = (1 << n) - 1
    if per is not None and per != full:
        alt = ",".join(str(k) for k in L.PRIMITIVE[n])
        out.err(field, f"{label} {L.poly_str(exps)} is not primitive: the register repeats after "
                       f"{per} chips instead of {full}, so it is not an m-sequence; use a primitive "
                       f"polynomial such as {alt}")
        return None
    return n, exps, full


def _check_form(form, field: str, out: _Issues) -> None:
    if form is not None and str(form).lower() not in ("fibonacci", "galois"):
        out.err(field, f"form {_show(form)} must be fibonacci or galois")


def _check_code_spec(cs: dict, field: str, out: _Issues, label: str) -> Optional[int]:
    """One explicit code spec (custom list entry, user code_spec / lfsr).
    Returns its chip length or None."""
    if not isinstance(cs, dict):
        out.err(field, f"{label}: code spec must be an object, got {_show(cs)}")
        return None
    fam = str(cs.get("family", "lfsr")).lower()
    nerr = out.n_err()
    if fam == "lfsr":
        poly = cs.get("poly", cs.get("taps"))
        deg = _int(cs.get("degree")) if cs.get("degree") is not None else None
        r = _check_poly(poly, field, out, degree=deg, label=f"{label} polynomial")
        if r is None:
            return None
        n, _exps, per = r
        _check_state(cs.get("state", cs.get("seed")), n, field, out, f"{label} state")
        _check_form(cs.get("form"), field, out)
        off = cs.get("offset")
        if off is not None and (_int(off) is None or _int(off) < 0):
            out.err(field, f"{label} offset {_show(off)} must be a whole number >= 0 (chips skipped)")
        ln = _check_length(cs.get("length"), per, field, out, f"{label} ")
        return ln if out.n_err() == nerr else None
    if fam == "gold" and (cs.get("polys") or cs.get("poly_a")):
        polys = cs.get("polys") or [cs.get("poly_a"), cs.get("poly_b")]
        n = _int(cs.get("degree") or cs.get("n"))
        per = _check_gold_pair(polys, n, field, field, out, label)
        if per is None:
            return None
        nn = int(math.log2(per + 1))
        _check_state(cs.get("state", cs.get("seed")), nn, field, out, f"{label} state")
        _check_state(cs.get("state2", cs.get("seed2")), nn, field, out, f"{label} state2")
        ln = _check_length(cs.get("length"), per, field, out, f"{label} ")
        return ln if out.n_err() == nerr else None
    # legacy families (seed / m / gold n+index / custom chips): resolve them
    try:
        c = code_chips(cs)
    except Exception as e:                                   # noqa: BLE001
        out.err(field, f"{label}: code spec {cs} cannot be built ({e})")
        return None
    if len(c) < MIN_CHIPS:
        out.err(field, f"{label}: {len(c)} chips is too short to spread: use at least {MIN_CHIPS}")
        return None
    return len(c)


def _check_gold_pair(polys, n: Optional[int], f1: str, f2: str, out: _Issues,
                     label: str = "Gold pair") -> Optional[int]:
    """Explicit Gold pair: both parse at degree n, are primitive, differ; a
    non-preferred pair is a warning. Returns the period or None."""
    if not isinstance(polys, (list, tuple)) or len(polys) != 2:
        out.err(f1, f"{label}: give exactly two polynomials")
        return None
    ra = _check_poly(polys[0], f1, out, degree=n, label=f"{label} polynomial A")
    rb = _check_poly(polys[1], f2, out, degree=n if n else (ra[0] if ra else None),
                     label=f"{label} polynomial B")
    if ra is None or rb is None:
        return None
    if ra[0] != rb[0]:
        out.err(f2, f"{label}: both polynomials need the same degree (A is {ra[0]}, B is {rb[0]})")
        return None
    if tuple(ra[1]) == tuple(rb[1]):
        out.err(f2, f"{label}: polynomial B equals polynomial A, so every 'Gold' code is a shift of "
                    "one m-sequence; enter a different primitive polynomial (or leave B blank to "
                    "use the library's preferred pair)")
        return None
    nn = ra[0]
    if nn <= 12:
        if not _preferred(json.dumps(list(ra[1])), json.dumps(list(rb[1])), nn):
            tbl = L.GOLD_PAIRS.get(nn)
            out.warn(f2, f"{label}: {L.poly_str(ra[1])} / {L.poly_str(rb[1])} is not a preferred pair, "
                         f"so the cross-correlation is not held to the Gold bound t={L.gold_t(nn)}"
                         + (f"; leave the 2nd polynomial blank to use the library pair "
                            f"{L.poly_str(tbl[0])} / {L.poly_str(tbl[1])}" if tbl else ""))
    return ra[2]


def _suggest_gold_n(need: int) -> Optional[int]:
    for n in sorted(L.GOLD_PAIRS):
        if (1 << n) - 1 >= need:
            return n
    return None


def _check_set(sd: dict, sname: str, n_users: int, out: _Issues, panel: bool) -> Optional[dict]:
    """Validate one code-set definition. Returns {"count", "length", "n",
    "kind", "capacity"} when it can be built, else None."""
    pre = "" if sname == "default" else f"code set {_show(sname)}: "
    F = (lambda k: k) if panel else (lambda k: "code_set")
    if not isinstance(sd, dict):
        out.err(F("set_kind"), f"{pre}code set must be an object")
        return None
    kind = str(sd.get("kind", "gold")).lower()
    nerr = out.n_err()
    _check_form(sd.get("form"), F("form"), out)
    count = None
    if "count" in sd and sd.get("count") is None:
        out.err(F("count"), f"{pre}codes (N users) is empty: enter how many codes the set should have")
        return None
    if "n" in sd and sd.get("n") is None and kind != "list" and not (kind == "mseq_phases" and sd.get("poly")):
        out.err(F("degree"), f"{pre}degree n is empty: enter {MIN_DEGREE}..{MAX_DEGREE}")
        return None
    if sd.get("count") is not None:
        count = _int(sd.get("count"))
        if count is None or count < 1:
            out.err(F("count"), f"{pre}codes (N users) {_show(sd.get('count'))} must be a whole number >= 1")
            count = None
    if kind == "list":
        codes = sd.get("codes") or []
        if not codes:
            out.err(F("custom"), f"{pre}the custom set is empty: add at least one custom LFSR "
                                 "(Add custom LFSR) or pick another set kind")
            return None
        lens = []
        for i, c in enumerate(codes):
            lens.append(_check_code_spec(c, F("custom"), out, f"{pre}custom code #{i}"))
        if out.n_err() > nerr:
            return None
        seen = {}
        for i, c in enumerate(codes):
            k = json.dumps(c, sort_keys=True)
            if k in seen:
                out.err(F("custom"), f"{pre}custom code #{i} is identical to #{seen[k]}: two users on one "
                                     "code cannot be told apart; change its state, offset or polynomial")
            seen.setdefault(k, i)
        if out.n_err() > nerr:
            return None
        return {"kind": kind, "count": len(codes), "capacity": len(codes),
                "length": lens[0] if len(set(lens)) == 1 else None, "lengths": lens, "n": None}

    if kind in ("gold", "mseq_phases", "seeds", "phases", "polys"):
        n_raw = sd.get("n", sd.get("degree"))
        n = _int(n_raw) if n_raw is not None else None
        if n_raw is not None and (n is None or n < MIN_DEGREE or n > MAX_DEGREE):
            out.err(F("degree"), f"{pre}degree n={n_raw} is outside what the library supports: "
                                 f"use {MIN_DEGREE}..{MAX_DEGREE}")
            return None
    else:
        out.err(F("set_kind"), f"{pre}unknown set kind {_show(kind)}: use gold, mseq_phases, polys or a "
                               "custom list")
        return None

    if kind == "gold":
        n = n if n is not None else 6
        polys = sd.get("polys")
        if polys:
            per = _check_gold_pair(polys, n, F("poly"), F("poly2"), out)
            if per is None:
                return None
        else:
            if n % 4 == 0:
                sug = _suggest_gold_n(max(count or 1, n_users))
                out.err(F("degree"), f"{pre}no Gold codes exist for n={n} (n divisible by 4 has no "
                                     f"preferred pair): use n = {_gold_ns()}"
                                     + (f" (n={sug} or more for {max(count or 1, n_users)} users)"
                                        if sug else ""))
                return None
            if n not in L.GOLD_PAIRS:
                sug = _suggest_gold_n(max(count or 1, n_users))
                out.err(F("degree"), f"{pre}the library has no Gold preferred pair for n={n}: use "
                                     f"n = {_gold_ns()}"
                                     + (f"; n={sug} or more gives enough codes for "
                                        f"{max(count or 1, n_users)} users" if sug else "")
                                     + ", or enter both polynomials of a preferred pair")
                return None
            per = (1 << n) - 1
        _check_state(sd.get("state", sd.get("seed")), n, F("seed"), out)
        _check_state(sd.get("state2"), n, F("seed"), out, "state2")
        ln = _check_length(sd.get("length"), per, F("length"), out)
        offsets = sd.get("offsets")
        if offsets is not None:
            bad = [o for o in offsets if _int(o) is None or _int(o) < -2]
            if bad:
                out.err(F("count"), f"{pre}Gold offsets {bad} invalid: use 0..{per - 1}, or -1 / -2 for "
                                    "the two base m-sequences")
                return None
            norm = [(_int(o) if _int(o) < 0 else _int(o) % per) for o in offsets]
            dup = sorted({o for o in norm if norm.count(o) > 1})
            if dup:
                out.err(F("count"), f"{pre}Gold offsets repeat (mod {per}): {dup}; each user needs a "
                                    "distinct offset")
                return None
            count = len(offsets)
            capacity = per + 2
        else:
            capacity = per                       # set builder uses offsets 0..N-1
        need = max(count or 0, n_users)
        if need > capacity:
            sug = _suggest_gold_n(need)
            out.err(F("degree"), f"{pre}Gold n={n} gives {per + 2} codes ({per} offsets + the 2 base "
                                 f"m-sequences) and this set takes offsets 0..N-1, so at most {capacity}; "
                                 f"{need} users need more: "
                                 + (f"raise the degree to {sug} or more" if sug else "use fewer users"))
            return None
        if out.n_err() > nerr:
            return None
        return {"kind": kind, "count": count, "capacity": capacity, "length": ln, "n": n}

    if kind in ("mseq_phases", "seeds", "phases"):
        poly = sd.get("poly")
        if poly is None:
            if n is None:
                n = 6
            poly = L.PRIMITIVE[n]
            r = poly_info(poly)
        else:
            r = _check_poly(poly, F("poly"), out)
            if r is None:
                return None
            if n is not None and r[0] != n and sd.get("degree") is None:
                out.warn(F("degree"), f"{pre}degree n={n} is ignored for m-sequence phases: the "
                                      f"polynomial {L.poly_str(r[1])} sets n={r[0]} "
                                      f"({(1 << r[0]) - 1} chips); set n={r[0]} or enter a "
                                      f"degree-{n} polynomial")
        nn = r[0]
        per = (1 << nn) - 1
        _check_state(sd.get("state", sd.get("seed")), nn, F("seed"), out)
        ln = _check_length(sd.get("length"), per, F("length"), out)
        states = sd.get("states")
        if states is not None:
            for s in states:
                _check_state(s, nn, F("seed"), out, "phase state")
            norm = [_int(s) for s in states]
            dup = sorted({s for s in norm if s is not None and norm.count(s) > 1})
            if dup:
                out.err(F("seed"), f"{pre}phase states repeat: {dup}; each user needs its own state")
            count = len(states)
        need = max(count or 0, n_users)
        if need > per:
            out.err(F("degree"), f"{pre}an n={nn} m-sequence has only {per} distinct phases; {need} users "
                                 f"need more: raise the degree to {math.ceil(math.log2(need + 1))} or more "
                                 "(a longer primitive polynomial)")
            return None
        if out.n_err() > nerr:
            return None
        return {"kind": "mseq_phases", "count": count, "capacity": per, "length": ln, "n": nn}

    # polys
    n = n if n is not None else 7
    polys = sd.get("polys")
    if polys:
        rs = []
        for i, p in enumerate(polys):
            r = _check_poly(p, F("poly"), out, label=f"{pre}polynomial #{i}")
            if r:
                rs.append(r)
        if out.n_err() > nerr:
            return None
        if len({r[0] for r in rs}) > 1:
            out.err(F("poly"), f"{pre}the polynomials have different degrees "
                               f"{sorted({r[0] for r in rs})}: their codes would differ in length; "
                               "use one degree")
            return None
        keys = [tuple(r[1]) for r in rs]
        if len(set(keys)) != len(keys):
            out.err(F("poly"), f"{pre}a polynomial is listed twice: each user needs a distinct one")
            return None
        capacity = len(rs)
        per = rs[0][2]
        n = rs[0][0]
    else:
        capacity = 1 + len(L.PRIMITIVE_ALT.get(n, []))
        per = (1 << n) - 1
    _check_state(sd.get("state", sd.get("seed")), n, F("seed"), out)
    ln = _check_length(sd.get("length"), per, F("length"), out)
    need = max(count or 0, n_users)
    if need > capacity:
        best = max(range(MIN_DEGREE, MAX_DEGREE + 1), key=lambda k: 1 + len(L.PRIMITIVE_ALT.get(k, [])))
        bcap = 1 + len(L.PRIMITIVE_ALT.get(best, []))
        out.err(F("degree" if not polys else "poly"),
                f"{pre}the library has {capacity} primitive polynomial(s) of degree {n}; {need} users "
                "need one each: "
                + (f"use n={best} ({bcap} polynomials) or " if need <= bcap and best != n else "")
                + "use a Gold set (2^n-1 codes per degree)")
        return None
    if out.n_err() > nerr:
        return None
    return {"kind": "polys", "count": count if count else capacity, "capacity": capacity,
            "length": ln, "n": n}


def _frame_bits(message: str, fec, out: _Issues, field: str, uid: str) -> Optional[int]:
    """On-air bits of one frame (sync + coded LEN/payload/CRC), or None on error."""
    from dpmg.framing import frame_codec as fc
    nb = len(message.encode("utf-8", errors="replace"))
    if nb > MAX_PAYLOAD_BYTES:
        out.err(field, f"{uid}: message is {nb} bytes; a frame carries at most {MAX_PAYLOAD_BYTES} "
                       "(8-bit length field): shorten it")
        return None
    info = 8 + 8 * nb + 16
    if not fec:
        return SYNC_BITS + info
    if not isinstance(fec, dict):
        out.err(field, f"{uid}: fec {_show(fec)} must be an object like {{\"type\": \"conv\", \"rate\": \"1/2\"}}")
        return None
    kind = fc.fec_kind(fec)
    if kind not in ("conv",) + tuple(fc._BLOCK_KINDS):
        out.err(field, f"{uid}: unknown fec type {_show(kind)}: use conv, rs, rs+conv, ldpc or tpc")
        return None
    try:
        cap = fc.fec_max_info_bits(fec)
    except Exception as e:                                   # noqa: BLE001
        out.err(field, f"{uid}: fec {fec} is not available ({e})")
        return None
    if info > cap:
        max_b = max(0, (cap - 24) // 8)
        out.err(field, f"{uid}: message of {nb} bytes needs {info} frame bits (LEN + payload + CRC) but "
                       f"one {kind} {fec.get('code', fec.get('rate', ''))} block carries {cap}: shorten "
                       f"the message to {max_b} bytes or less, or pick a larger code")
        return None
    try:
        return SYNC_BITS + int(fc._frame_coded_len(info, fec))
    except Exception as e:                                   # noqa: BLE001
        out.err(field, f"{uid}: fec {fec} cannot encode this frame ({e})")
        return None


def _worker_fs_out(rates: list, spread: list) -> float:
    """channelizer_worker gate rate: lcm of the plan's symbol rates x mult (>= 10
    samples/symbol), raised to 2 samples/chip for DSSS (SPREAD_FSOUT)."""
    srates = [int(round(r)) for r in rates if r and r > 0]
    srates = [s for s in srates if s > 0]
    if not srates:
        return 0.0
    lcm = srates[0]
    for s in srates[1:]:
        lcm = lcm * s // math.gcd(lcm, s)
    mult = 1
    while (lcm * mult) / max(srates) < 10:
        mult += 1
    fs_out = float(lcm * mult)
    for sr, sf in spread:
        fs_out = max(fs_out, 2.0 * sf * sr)
    return fs_out


def validate_builder(spec: dict, radio_rate: Optional[float] = None,
                     with_info: bool = False, panel: bool = True) -> list:
    """Check a DSSS-builder users-form spec. Returns [(level, field, message)].
    panel=True: code-set issues name the panel's code-set inputs (degree,
    count, poly ...); False: everything about a set is reported as 'code_set'."""
    out = _Issues()
    if not isinstance(spec, dict):
        out.err("spec", "the scenario must be a JSON object")
        return out.finish(0)

    # scenario-level inputs
    name = spec.get("name")
    if name is None or not str(name).strip():
        out.err("name", "scenario name is empty: it names the IQ / plan files; enter one")
    elif not _NAME_RE.match(str(name).strip()):
        out.err("name", f"scenario name {_show(name)} may only use letters, digits, '_', '-' and '.' "
                        "(it becomes a file name on the radio)")
    fs = _num(spec.get("sample_rate_hz"))
    if fs is None or fs <= 0:
        out.err("sample_rate", f"sample rate is {_show(spec.get('sample_rate_hz'))}; it must be > 0 Hz "
                               "(use 'rate/center from radio')")
        fs = None
    if spec.get("center_freq_hz") is not None:
        cf = _num(spec.get("center_freq_hz"))
        if cf is None or cf <= 0:
            out.err("center", f"centre frequency {_show(spec.get('center_freq_hz'))} must be > 0 Hz")
    loop_s = _num(spec.get("loop_s", 0.2))
    if loop_s is None or loop_s <= 0:
        out.err("loop_s", f"loop is {_show(spec.get('loop_s'))}; it must be > 0 s")
        loop_s = None
    rr = _num(radio_rate)
    if fs and rr and abs(rr - fs) > fs * 1e-3:
        out.warn("sample_rate", f"the scenario is set for {_sps(fs)} but the radio runs {_sps(rr)}: "
                                "Build on radio rebuilds at the radio rate, so the checks below use "
                                f"{_sps(fs)}; press 'rate/center from radio' to check at the radio rate")

    d = spec.get("dsss") or {}
    users = spec.get("users") or []
    if not isinstance(users, list):
        out.err("users", "users must be a list")
        users = []

    # dsss defaults
    if d.get("amplitude") is not None:
        a = _num(d.get("amplitude"))
        if a is None or a <= 0 or a > 1:
            out.err("amplitude", f"default amplitude is {_show(d.get('amplitude'))}; it must be in (0, 1] (full scale = 1)")
    if d.get("stagger_chips") is not None:
        s = _int(d.get("stagger_chips"))
        if s is None or s < 0:
            out.err("stagger", f"stagger {_show(d.get('stagger_chips'))} must be a whole number of chips >= 0")
    if d.get("spacing_hz") is not None and _num(d.get("spacing_hz")) is None:
        out.err("spacing_hz", f"spacing {_show(d.get('spacing_hz'))} is not a number")
    if d.get("offset_hz") is not None and _num(d.get("offset_hz")) is None:
        out.err("offset_hz", f"offset {_show(d.get('offset_hz'))} is not a number")
    if d.get("carrier") is not None and str(d.get("carrier")).lower() not in CARRIERS:
        out.err("carrier", f"default carrier {_show(d.get('carrier'))}: use {', '.join(CARRIERS)}")

    # code sets
    sets_def = {}
    if spec.get("code_set"):
        sets_def["default"] = spec["code_set"]
    for nm, sd in (spec.get("code_sets") or {}).items():
        sets_def[str(nm)] = sd
    default_name = "default" if "default" in sets_def else next(iter(sets_def), "")

    def _uset(u):
        return str(u.get("set") or default_name)

    sets_info = {}
    for nm, sd in sets_def.items():
        n_on = sum(1 for u in users if isinstance(u, dict) and not u.get("code_spec")
                   and not u.get("lfsr") and _uset(u) == nm)
        sets_info[nm] = _check_set(sd, nm, n_on, out, panel and nm == "default")
    if with_info:
        for nm, si in sets_info.items():
            if si:
                k = si["kind"]
                n_users = sum(1 for u in users if isinstance(u, dict) and _uset(u) == nm)
                cap = si["capacity"]
                fam = {"gold": f"Gold n={si['n']} gives {cap + 2} codes ({cap} as offsets 0..{cap - 1})"
                               if not sets_def[nm].get("offsets") else f"Gold n={si['n']} gives {cap}",
                       "mseq_phases": f"an n={si['n']} m-sequence gives {cap} phases",
                       "polys": f"the library has {cap} degree-{si['n']} polynomials",
                       "list": f"the custom list has {cap}"}[k]
                need = max(n_users, si['count'] or 0)
                out.info("degree" if nm == "default" else "code_set",
                         f"{need} users need at least {need} codes; {fam} -- OK")

    # users
    if not users:
        out.err("users", "no users: add users (Add user) or press 'Auto-fill N users'")
    ids_seen = set()
    rows = []                               # resolved per-user records for the rate checks
    for i, u in enumerate(users):
        if not isinstance(u, dict):
            out.err("users", f"user #{i} is not an object")
            continue
        uid = str(u.get("id") or u.get("user") or f"U{i + 1:02d}")
        uf = f"user:{uid}"
        if uid in ids_seen:
            out.err(uf, f"user id {uid} is used twice: give every user its own id (it names the "
                        "decode channel)")
        ids_seen.add(uid)
        carrier = str(u.get("carrier") or d.get("carrier") or "bpsk").lower()
        if carrier not in CARRIERS:
            out.err(uf, f"{uid}: carrier {_show(carrier)} is not offered: use {', '.join(CARRIERS)}")
        rec = {"id": uid, "field": uf, "carrier": carrier, "L": None, "code_key": None,
               "code_spec": None, "set": None, "code": None}
        # the code
        if u.get("code_spec") or u.get("lfsr"):
            cs = dict(u["code_spec"]) if u.get("code_spec") else dict({"family": "lfsr"}, **u["lfsr"])
            rec["L"] = _check_code_spec(cs, uf, out, uid)
            rec["code_spec"] = cs
            rec["code_key"] = ("spec", json.dumps(cs, sort_keys=True))
        else:
            sname = _uset(u)
            if sname not in sets_def:
                out.err(uf, f"{uid}: no code set {_show(sname)}: define the code set (section 1)")
            else:
                si = sets_info.get(sname)
                k = u.get("code", i)
                ki = _int(k)
                if ki is None or ki < 0:
                    out.err(uf, f"{uid}: code # {_show(k)} must be a whole number >= 0 (an index into the set)")
                elif si is not None:
                    size = si["count"] if si["count"] else si["capacity"]
                    if ki >= size:
                        out.err("count" if (panel and sname == "default") else uf,
                                f"{uid} uses code #{ki} but the set has {size} code(s) (#0..#{size - 1}): "
                                f"raise 'codes (N users)' to {ki + 1} or give {uid} another code")
                    else:
                        rec.update(set=sname, code=ki,
                                   L=(si["lengths"][ki] if si.get("lengths") else si["length"]),
                                   code_key=("set", sname, ki))
            # dual code
            if u.get("code_q") is not None:
                kq = _int(u.get("code_q"))
                si = sets_info.get(sname)
                if carrier not in DUAL_CODE_CARRIERS:
                    out.err(uf, f"{uid}: a dual (I/Q) code needs a qpsk or oqpsk carrier, not {carrier}: "
                                "change the carrier or drop code_q")
                if kq is None or kq < 0:
                    out.err(uf, f"{uid}: code_q {_show(u.get('code_q'))} must be a code index >= 0")
                elif kq == rec["code"]:
                    out.err(uf, f"{uid}: the Q-rail code #{kq} equals the I-rail code, which is just "
                                "single-code spreading; pick a different code_q or drop it")
                elif si is not None:
                    size = si["count"] if si["count"] else si["capacity"]
                    if kq >= size:
                        out.err(uf, f"{uid}: code_q #{kq} but the set has {size} code(s): pick #0..#{size - 1}")
        if u.get("code_spec_q") or u.get("lfsr_q"):
            csq = dict(u["code_spec_q"]) if u.get("code_spec_q") else dict({"family": "lfsr"}, **u["lfsr_q"])
            if carrier not in DUAL_CODE_CARRIERS:
                out.err(uf, f"{uid}: a dual (I/Q) code needs a qpsk or oqpsk carrier, not {carrier}")
            lq = _check_code_spec(csq, uf, out, f"{uid} Q code")
            if lq is not None and rec["code_spec"] is not None:
                if json.dumps(csq, sort_keys=True) == json.dumps(rec["code_spec"], sort_keys=True):
                    out.err(uf, f"{uid}: the Q-rail code equals the I-rail code; use a different code "
                                "or drop the dual code")
                elif rec["L"] and lq != rec["L"]:
                    out.err(uf, f"{uid}: the Q-rail code is {lq} chips but the I-rail code is {rec['L']}: "
                                "dual codes must share one length")
        # amplitude
        amp = _num(u.get("amplitude", d.get("amplitude", 0.8)))
        if amp is None or amp <= 0 or amp > 1:
            out.err(uf, f"{uid}: amplitude is {_show(u.get('amplitude', d.get('amplitude')))}; it must be in (0, 1] "
                        "(full scale = 1)")
            amp = None
        rec["amp"] = amp
        # rates / placement
        spc = _int(u.get("samples_per_chip", d.get("samples_per_chip", 2)))
        if spc is None or spc < MIN_SPC:
            out.user(ERROR, "samples_per_chip" if u.get("samples_per_chip") is None else uf, uid,
                     "{who}: samples/chip is " f"{_show(u.get('samples_per_chip', d.get('samples_per_chip')))}; it "
                     f"must be a whole number >= {MIN_SPC} (the despreader's floor)")
            spc = None
        elif carrier == "oqpsk" and spc % 2:
            out.user(ERROR, "samples_per_chip" if u.get("samples_per_chip") is None else uf, uid,
                     "{who}: OQPSK needs an even samples/chip (the Q rail is delayed half a chip); "
                     f"{spc} is odd: use {spc + 1}")
        rec["spc"] = spc
        use_chip = (u.get("chip_rate_hz") is not None
                    or (d.get("chip_rate_hz") is not None and u.get("symbol_rate") is None))
        rec["Rs"] = None
        rec["chip"] = None
        if use_chip:
            cr = _num(u.get("chip_rate_hz", d.get("chip_rate_hz")))
            if cr is None or cr <= 0:
                out.err(uf, f"{uid}: chip rate {_show(u.get('chip_rate_hz', d.get('chip_rate_hz')))} must be > 0")
            else:
                rec["chip"] = cr
                if rec["L"]:
                    rec["Rs"] = cr / rec["L"]
        else:
            raw = u.get("symbol_rate", d.get("symbol_rate", 2400))
            sr = _num(raw)
            if sr is None or sr <= 0:
                out.user(ERROR, "symbol_rate" if u.get("symbol_rate") is None else uf, uid,
                         "{who}: symbol rate is " f"{_show(raw)}; it must be > 0 Bd")
            else:
                rec["Rs"] = sr
                if rec["L"]:
                    rec["chip"] = sr * rec["L"]
        off_raw = u.get("offset_hz")
        if off_raw is not None:
            off = _num(off_raw)
            if off is None:
                out.err(uf, f"{uid}: offset {_show(off_raw)} is not a number")
        else:
            base = _num(d.get("offset_hz", 0.0)) or 0.0
            spacing = _num(d.get("spacing_hz", 0.0)) or 0.0
            off = base + i * spacing
        rec["offset"] = off
        # message / frame
        rec["frame_bits"] = None
        if u.get("bits"):
            b = u["bits"]
            rec["frame_bits"] = len(b) if isinstance(b, str) else max(len(x) for x in b)
        elif not u.get("secure"):
            msg = u.get("message", f"{uid} HELLO")
            if msg is None or str(msg) == "":
                out.err(uf, f"{uid}: message is empty: type the text this user should send")
            else:
                rec["frame_bits"] = _frame_bits(str(msg), u.get("fec", d.get("fec")), out, uf, uid)
        rows.append(rec)

    # duplicate codes at one centre / overlapping centres
    for a_i in range(len(rows)):
        for b_i in range(a_i + 1, len(rows)):
            a, b = rows[a_i], rows[b_i]
            if a["code_key"] is None or a["code_key"] != b["code_key"]:
                continue
            if a["offset"] is None or b["offset"] is None:
                continue
            ca = max(a["chip"] or 0, b["chip"] or 0)
            if abs(a["offset"] - b["offset"]) < 2 * ca or ca == 0:
                what = (f"code #{a['code']}" if a["code_key"][0] == "set" else "the same code spec")
                out.err(b["field"], f"{a['id']} and {b['id']} both use {what} on overlapping spectrum: "
                                    "two users on one code cannot be told apart; give each user its own code")

    # rates, bandwidth, placement
    if fs:
        usable = 0.5 * fs * USABLE_GUARD
        for r in rows:
            if r["chip"] is None:
                continue
            chip = r["chip"]
            if 2 * chip > fs:
                max_sr = fs / 2 / r["L"] if r["L"] else 0
                out.user(ERROR, "symbol_rate", r["id"],
                         "{who}: chip rate " f"{_chips(chip)} needs at least {_sps(2 * chip)} (2 samples "
                        f"per chip) but the sample rate is {_sps(fs)}: lower the symbol rate to "
                        f"{max_sr:.0f} Bd or less, use a shorter code, or raise the sample rate")
                continue
            if r["offset"] is not None:
                edge = abs(r["offset"]) + chip
                if edge > usable:
                    room = usable - chip
                    out.user(ERROR, "offset_hz", r["id"],
                             "{who}: at " f"{r['offset'] / 1e3:+.1f} kHz with a main lobe of +/-{_hz(chip)} "
                            f"the signal reaches {_hz(edge)}, beyond the usable +/-{_hz(usable)} at {_sps(fs)}: "
                            + (f"keep the offset within +/-{_hz(room)}" if room > 0 else
                               "the signal is wider than the band")
                            + " or lower the symbol rate / spacing")
            if r["spc"] and chip * r["spc"] > fs:
                out.user(WARNING, "samples_per_chip", r["id"],
                         "{who}: " f"{r['spc']} samples/chip x {_chips(chip)} = {_sps(chip * r['spc'])} "
                         f"native, above the {_sps(fs)} sample rate: the generator resamples it down "
                         "(the extra samples are thrown away); 2 samples/chip is enough")
        # overlapping (not identical) centres
        cen = {}
        for r in rows:
            if r["offset"] is not None and r["chip"]:
                cen.setdefault(round(r["offset"], 3), []).append(r)
        keys = sorted(cen)
        clash = []
        for i in range(len(keys) - 1):
            a, b = cen[keys[i]][0], cen[keys[i + 1]][0]
            if keys[i + 1] - keys[i] < a["chip"] + b["chip"]:
                clash.append((keys[i], keys[i + 1], a, b))
        if clash:
            k0, k1, a, b = clash[0]
            need = a["chip"] + b["chip"]
            out.warn("spacing_hz",
                     (f"{len(clash)} neighbouring centres overlap, e.g. " if len(clash) > 1 else "")
                     + f"{a['id']} ({k0 / 1e3:+.1f} kHz) and {b['id']} ({k1 / 1e3:+.1f} kHz) are "
                     f"{_hz(k1 - k0)} apart but their main lobes span +/-{_hz(a['chip'])} and "
                     f"+/-{_hz(b['chip'])}: the spectra overlap; space centres at least {_hz(need)} apart, "
                     "or set spacing 0 for an intentional one-centre overlay")

    # frame vs TX loop, frame vs the live decode window
    for r in rows:
        if not r["frame_bits"] or not r["Rs"]:
            continue
        bps = 1 if r["carrier"] == "bpsk" else 2
        nsym = math.ceil(r["frame_bits"] / bps) + LEAD_TAIL_SYMBOLS
        dur = nsym / r["Rs"]
        if loop_s and dur > loop_s:
            out.user(ERROR, "loop_s", r["id"], "{who}: the burst lasts " f"{dur * 1e3:.0f} ms ({r['frame_bits']} frame bits at "
                              f"{r['Rs']:.0f} Bd) but the TX loop is {loop_s * 1e3:.0f} ms: the loop would cut "
                              f"the frame; raise loop to {math.ceil(dur * 20) / 20:.2f} s or more, shorten "
                              "the message or raise the symbol rate")
    if fs:
        rates = [r["Rs"] for r in rows if r["Rs"]]
        spread = [(r["Rs"], r["L"]) for r in rows if r["Rs"] and r["L"]]
        fs_out = _worker_fs_out(rates, spread)
        if fs_out > 0:
            decim = max(1, int(round(fs / fs_out)))
            fs_act = fs / decim
            cur = min(GPU_MAX_OUT, LIVE_FRAME_LEN // decim)
            window = 2 * cur - 2 * EDGE
            warned = set()
            for r in rows:
                if not r["Rs"] or not r["frame_bits"] or r["id"] in warned:
                    continue
                sps = max(2.0, fs_act / r["Rs"])
                fb = max(WORKER_FRAME_BITS, r["frame_bits"])
                need = (fb + NEED_MARGIN_SYMBOLS) * sps
                if need > window:
                    warned.add(r["id"])
                    win_sym = window / sps
                    out.user(WARNING, "symbol_rate", r["id"],
                             "{who}: the live channelizer decode window at " f"{_sps(fs)} holds about "
                             f"{win_sym:.0f} symbols at {r['Rs']:.0f} Bd but a frame needs {fb + 32:.0f} "
                             f"({r['frame_bits']} frame bits + margin): it will transmit but never decode "
                             "live; raise the symbol rate, shorten the message or use a lower sample rate")

    # amplitudes in the composite
    amps = [r["amp"] for r in rows if r.get("amp")]
    if len(amps) > 1 and sum(amps) > 1.0:
        tot = sum(amps)
        try:
            from dpmg.scenario.gen_scenario_json import resolve_peak_fs
            peak = resolve_peak_fs(spec)
        except (ImportError, ValueError):
            peak = 0.45
        out.warn("amplitude", f"user amplitudes add up to {tot:.2f} (> 1): the generator scales the whole "
                              f"composite so its peak is {peak:g} of full scale, so nothing clips, but every "
                              f"user ends up to {20 * math.log10(tot / peak):.1f} dB below its set amplitude "
                              "(only the ratios between users matter)")
    if len(amps) > 1:
        lo, hi = min(amps), max(amps)
        if hi / lo > 2.0:
            out.warn("amplitude", f"user amplitudes range {lo:.2f}..{hi:.2f} ({20 * math.log10(hi / lo):.1f} dB): "
                                  "the weaker users see the stronger ones as extra interference "
                                  "(near-far); see the interference estimate")

    # multi-user interference (needs buildable codes)
    if out.n_err() == 0:
        _check_mui(spec, rows, sets_def, out, with_info)
    return out.finish(len(rows))


def _check_mui(spec, rows, sets_def, out: _Issues, with_info: bool) -> None:
    groups = {}
    for r in rows:
        if r["offset"] is not None and r["L"]:
            groups.setdefault((round(r["offset"], 3)), []).append(r)
    set_codes = {}
    for off, grp in groups.items():
        if len(grp) < 2:
            continue
        lens = {r["L"] for r in grp}
        if len(lens) > 1:
            out.warn("length", f"users at {off / 1e3:+.1f} kHz use codes of different lengths "
                               f"{sorted(lens)}: they still overlay, but the interference cannot be "
                               "predicted (no cross-correlation table); use one code length per centre")
            continue
        Lc = lens.pop()
        if Lc > MAX_ANALYSIS_LEN or Lc * len(grp) > MAX_ANALYSIS_CHIPS:
            if with_info:
                out.info("users", f"{len(grp)} users x {Lc} chips: too large to estimate the interference "
                                  "live; press Build codes for the full report")
            continue
        # chips per user
        chips = []
        try:
            for r in grp:
                if r["code_key"][0] == "set":
                    sn = r["code_key"][1]
                    if sn not in set_codes:
                        set_codes[sn] = L.make_code_set(sets_def[sn])
                    cs = set_codes[sn][r["code_key"][2]]
                else:
                    cs = r["code_spec"]
                chips.append(code_chips(cs))
        except Exception as e:                               # noqa: BLE001
            out.err("code_set", f"the codes cannot be built: {e}")
            return
        if len({len(c) for c in chips}) > 1:
            continue
        rep = L.set_report(chips, [r["id"] for r in grp])
        N = len(grp)
        Lc = rep["length"]
        # shifted copies: a full-height cross-correlation peak at some lag
        wp = rep["worst_pair"]
        if wp and wp[2] >= Lc:
            out.warn("set_kind", f"{grp[wp[0]]['id']} and {grp[wp[1]]['id']} use codes that are shifts of "
                                 f"each other (peak cross-correlation {wp[2]:.0f} of {Lc}): with "
                                 "chip-asynchronous users the despreader can lock onto the wrong user; "
                                 "prefer a Gold set or distinct polynomials")
        rms = np.asarray(rep["xcorr_rms"], dtype=np.float64)
        amps = np.array([r["amp"] or 0.8 for r in grp], dtype=np.float64)
        worst = None
        for i, r in enumerate(grp):
            it = sum((amps[j] ** 2) * rms[i, j] ** 2 for j in range(N) if j != i)
            sinr = 10 * math.log10((Lc * Lc * amps[i] ** 2) / it) if it > 0 else float("inf")
            need = DECODE_SINR_DB.get(r["carrier"], 8.0)
            if worst is None or sinr - need < worst[1] - worst[2]:
                worst = (r, sinr, need)
        r, sinr, need = worst
        if sinr < need:
            out.warn("users", f"predicted despread SINR for {r['id']} is {sinr:.1f} dB ({N} users at "
                              f"{off / 1e3:+.1f} kHz, {Lc} chips), below the ~{need:.0f} dB a {r['carrier']} "
                              "user needs: expect missed frames; use longer codes (raise the degree), fewer "
                              "users, FEC or more amplitude for the weak user"
                              + (" (the BPSK overlay canceller may still recover it)"
                                 if r["carrier"] == "bpsk" else ""))
        elif with_info:
            out.info("users", f"predicted despread SINR >= {sinr:.1f} dB for {N} users at "
                              f"{off / 1e3:+.1f} kHz (need ~{need:.0f} dB) -- OK")
        # equal-power rule of thumb: L / (N-1)
        need_c = max(DECODE_SINR_DB.get(x["carrier"], 8.0) for x in grp) + CAPACITY_MARGIN_DB
        thumb = 10 * math.log10(Lc / (N - 1))
        if thumb < need_c:
            max_n = 1 + int(math.floor(Lc / 10 ** (need_c / 10)))
            min_L = (N - 1) * 10 ** (need_c / 10)
            nxt = next((2 ** k - 1 for k in range(2, 17) if 2 ** k - 1 >= min_L), None)
            out.warn("count", f"{N} equal-power users on {Lc} chips: the rule of thumb L/(N-1) gives "
                              f"{thumb:.1f} dB despread SINR, under the {need_c:.0f} dB comfort level "
                              f"({need_c - CAPACITY_MARGIN_DB:.0f} dB decode + {CAPACITY_MARGIN_DB:.0f} dB "
                              f"margin): {Lc} chips comfortably carry about {max_n} users"
                              + (f"; use {nxt}+ chip codes for {N}" if nxt else "")
                              + " (or expect occasional misses)")


def validate_code_spec(cs: dict, label: str = "custom LFSR") -> list:
    """Checks for one explicit code spec (the panel's 'Add custom LFSR' row)."""
    out = _Issues()
    _check_code_spec(cs, "custom", out, label)
    return out.finish(0)


# convenience

def errors(issues) -> list:
    return [i for i in issues if i[0] == ERROR]


def warnings(issues) -> list:
    return [i for i in issues if i[0] == WARNING]


def has_errors(issues) -> bool:
    return any(i[0] == ERROR for i in issues)


def first_error(issues) -> Optional[str]:
    e = errors(issues)
    return e[0][2] if e else None


CODE_SET_FIELDS = ("set_kind", "degree", "count", "length", "form", "poly", "poly2", "seed",
                   "custom", "code_set")


def code_set_errors(issues) -> list:
    """Errors that make 'Build codes' itself fail (code-set inputs only)."""
    return [i for i in issues if i[0] == ERROR and i[1] in CODE_SET_FIELDS]
