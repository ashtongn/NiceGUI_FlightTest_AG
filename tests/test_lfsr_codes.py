"""
test_lfsr_codes.py: the LFSR spreading-code library (dpmg/modulation/lfsr_codes.py).

Known sequences: m-sequences of degree 5 / 7 / 10 against an independent
bit-by-bit shift-register reference and their textbook properties (period
2^n-1, balance 2^(n-1) ones, run-length halving, two-level autocorrelation);
the GPS C/A code (Gold code of degree 10, G1 = x^10+x^3+1, G2 =
x^10+x^9+x^8+x^6+x^3+x^2+1, ICD-GPS-200 Table 3-I: PRN 1 first ten chips
1440 octal, PRN 2 1620, PRN 3 1710) built from the library's LFSRs; Gold-pair
properties (three-valued cross-correlation {-1, -t, t-2}) for every tabled
pair; Galois == Fibonacci up to a phase; truncation; code sets; the
correlation helpers; and the legacy seed / m / gold presets reproduced exactly
by the LFSR builder. pytest collects the test_* functions; `python3
tests/test_lfsr_codes.py` runs them all (exit 0 = pass).
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import numpy as np

from dpmg.modulation import lfsr_codes as L
from dpmg.modulation.spreading_codes import resolve_code, code_len, describe_code


def _ref_mseq(exps, n, state):
    """Independent reference: explicit list-of-bits Fibonacci register, LSB out."""
    bits = [(state >> i) & 1 for i in range(n)]       # bits[0] = output end
    taps = [k for k in exps if 0 <= k < n]
    out = []
    for _ in range((1 << n) - 1):
        out.append(bits[0])
        fb = 0
        for k in taps:
            fb ^= bits[k]
        bits = bits[1:] + [fb]
    return np.array(out, dtype=np.int8)


def test_msequence_known_degrees_5_7_10():
    for n, poly in ((5, [5, 2]), (7, [7, 3]), (10, [10, 3])):
        seq = L.m_sequence(poly, n=n, state=1)
        ref = _ref_mseq(poly + [0], n, 1)
        assert np.array_equal(seq, ref), f"n={n} differs from the reference register"
        assert len(seq) == (1 << n) - 1 and L.period(seq) == (1 << n) - 1
        b = L.balance(seq)
        assert b["ones"] == 1 << (n - 1) and b["zeros"] == (1 << (n - 1)) - 1
        ac = L.autocorr(seq)
        assert ac[0] == (1 << n) - 1 and set(int(v) for v in ac[1:]) == {-1}, "two-level autocorr"
        runs = L.run_lengths(seq)
        # half the runs have length 1, a quarter length 2 ... (one run of n, one of n-1)
        total = sum(runs.values())
        assert runs[1] == total // 2 and runs[2] == total // 4
        assert runs.get(n) == 1 and runs.get(n - 1) == 1


def test_gps_ca_prn_1_2_3_from_library_lfsrs():
    """ICD-GPS-200: G1 / G2 are 10-stage registers, all-ones start, output from
    stage 10 (the shift direction makes them the reciprocal polynomials in this
    library's LSB-out convention: x^10+x^7+1 and x^10+x^8+x^7+x^4+x^2+x+1).
    PRN i = G1 xor G2 delayed by its chip delay (PRN1 5, PRN2 6, PRN3 7)."""
    g1 = L.LFSR([10, 7], state=1023).run()
    g2 = L.LFSR([10, 8, 7, 4, 2, 1], state=1023).run()
    assert L.LFSR([10, 7]).is_maximal() and L.LFSR([10, 8, 7, 4, 2, 1]).is_maximal()
    want = {5: "1100100000", 6: "1110010000", 7: "1111001000"}   # 1440 / 1620 / 1710 octal
    for delay, first10 in want.items():
        prn = g1 ^ np.roll(g2, delay)
        got = "".join(str(int(b)) for b in prn[:10])
        assert got == first10, f"PRN delay {delay}: {got} != {first10}"
        assert int(got, 2) == int({5: "1440", 6: "1620", 7: "1710"}[delay], 8)


def test_gold_pairs_three_valued():
    for n, (pa, pb) in L.GOLD_PAIRS.items():
        assert L.is_preferred_pair(pa, pb, n), f"n={n} is not a preferred pair"
        t = L.gold_t(n)
        a, b = L.m_sequence(pa, n=n), L.m_sequence(pb, n=n)
        vals = set(int(v) for v in L.crosscorr(a, b))
        assert vals <= {-1, -t, t - 2} and len(vals) == 3
        # every Gold code of the family keeps the bound against every other
        fam = [L.gold_code(n, off) for off in range(0, 6)]
        rep = L.set_report(fam)
        assert rep["worst_pair"][2] <= t, f"n={n}: peak xcorr {rep['worst_pair']} > t={t}"
        assert all(p <= t for p in rep["auto_peak"])


def test_galois_equals_fibonacci_up_to_phase_and_primitive_table():
    for n, poly in L.PRIMITIVE.items():
        f = L.LFSR(poly, 1, "fibonacci")
        g = L.LFSR(poly, 1, "galois")
        assert f.is_maximal() and g.is_maximal(), f"n={n} table polynomial not primitive"
        sf, sg = f.run(), g.run()
        assert any(np.array_equal(sf, np.roll(sg, k)) for k in range(len(sf))), f"n={n} forms differ"
    for n, alts in L.PRIMITIVE_ALT.items():
        for p in alts:
            assert L.LFSR(p).is_maximal(), f"alt {p} not primitive"
    # a reducible polynomial is refused by m_sequence
    try:
        L.m_sequence([4, 2])           # x^4 + x^2 + 1 = (x^2+x+1)^2
        assert False, "non-primitive polynomial accepted"
    except ValueError:
        pass


def test_polynomial_forms_taps_mask_hex_string():
    n, exps = L.parse_poly([5, 2])
    assert (n, exps) == (5, [0, 2, 5])
    assert L.parse_poly(0x12) == (5, [0, 2, 5])
    assert L.parse_poly("0x12") == (5, [0, 2, 5])
    assert L.parse_poly("x^5+x^2+1") == (5, [0, 2, 5])
    assert L.parse_poly("5,2") == (5, [0, 2, 5])
    assert L.poly_mask([5, 2]) == 0x12 and L.poly_str([5, 2]) == "x^5+x^2+1"
    assert L.parse_poly([2], degree=5) == (5, [0, 2, 5])        # degree override
    s1 = L.LFSR([5, 2], 7).run(); s2 = L.LFSR("0x12", 7).run(); s3 = L.LFSR("x^5+x^2+1", 7).run()
    assert np.array_equal(s1, s2) and np.array_equal(s1, s3)


def test_truncation_offset_and_state():
    full = L.m_sequence([6, 1])
    cut = L.m_sequence([6, 1], length=40)
    assert len(cut) == 40 and np.array_equal(cut, full[:40])
    spec = {"family": "lfsr", "poly": [6, 1], "state": 1, "offset": 10, "length": 63}
    pm = L.lfsr_spec_to_pm(spec)
    assert np.array_equal(pm, L.to_pm(np.roll(full, -10)))
    # a different start state is a phase of the same sequence
    other = L.m_sequence([6, 1], state=37)
    assert any(np.array_equal(full, np.roll(other, k)) for k in range(63))
    # state 0 never leaves the register: replaced by 1
    assert np.array_equal(L.LFSR([5, 2], 0).run(), L.LFSR([5, 2], 1).run())


def test_code_sets_kinds():
    g = L.make_code_set({"kind": "gold", "n": 5, "count": 4})
    assert len(g) == 4 and all(c["family"] == "gold" and c["polys"] for c in g)
    assert [c["offset"] for c in g] == [0, 1, 2, 3]
    ph = L.make_code_set({"kind": "mseq_phases", "poly": [6, 1], "count": 3})
    assert len(ph) == 3 and len({c["state"] for c in ph}) == 3
    seqs = [resolve_code(c) for c in ph]
    assert all(len(s) == 63 for s in seqs)
    # phases of one m-sequence: cross-correlation of shifts is exactly -1 off the peak
    rep = L.set_report(seqs)
    assert rep["worst_pair"][2] == 63 or rep["xcorr_peak"][0][1] == 63   # a shift hits full peak at one lag
    po = L.make_code_set({"kind": "polys", "n": 7, "count": 3})
    assert len(po) == 3 and len({tuple(c["poly"]) for c in po}) == 3
    li = L.make_code_set({"kind": "list", "codes": [{"family": "m", "n": 5}, {"family": "seed", "length": 31}]})
    assert len(li) == 2
    for bad in ({"kind": "gold", "n": 8, "count": 2}, {"kind": "nope"}, {"kind": "gold", "n": 5}):
        try:
            L.make_code_set(bad)
            assert False, f"accepted {bad}"
        except ValueError:
            pass


def test_set_report_fields_and_mui():
    codes = [L.to_pm(L.gold_code(6, k)) for k in range(4)]
    rep = L.set_report(codes, ["a", "b", "c", "d"])
    assert rep["n"] == 4 and rep["length"] == 63 and rep["names"] == ["a", "b", "c", "d"]
    assert len(rep["xcorr_peak"]) == 4 and rep["xcorr_peak"][0][0] == 63
    assert rep["worst_pair"][2] <= L.gold_t(6) and rep["worst_db"] < 0
    # equal-power MUI: L^2 / sum of rms^2 over 3 interferers, each rms ~ sqrt(L)
    assert all(10.0 < m < 16.0 for m in rep["mui_snr_db"]), rep["mui_snr_db"]
    assert L.set_report([])["n"] == 0


def test_legacy_presets_expressible_in_lfsr_form():
    for spec in ({"family": "m", "n": 5}, {"family": "m", "n": 7, "seed": 5},
                 {"family": "gold", "n": 5, "index": 0}, {"family": "gold", "n": 6, "index": 3},
                 {"family": "seed", "length": 15, "seed": 3}):
        new = L.legacy_to_lfsr(spec)
        assert np.array_equal(resolve_code(spec), resolve_code(new)), spec
    # the LFSR families resolve through spreading_codes too
    assert code_len({"family": "lfsr", "poly": [7, 1]}) == 127
    assert code_len({"family": "gold", "polys": [[5, 2], [5, 4, 3, 2]], "offset": 4}) == 31
    assert code_len({"family": "lfsr", "poly": "0x12", "length": 20}) == 20
    assert "lfsr" in describe_code({"family": "lfsr", "poly": [5, 2]})
    # the legacy gold table: only n=5 is a true preferred pair (recorded fact)
    from dpmg.modulation.spreading_codes import _MSEQ_TAPS
    t0, t1 = _MSEQ_TAPS[5]
    pa = [5] + [k for k in range(5) if (t0 >> k) & 1]
    pb = [5] + [k for k in range(5) if (t1 >> k) & 1]
    assert L.is_preferred_pair(pa, pb, 5)


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    bad = 0
    for t in tests:
        try:
            t()
            print(f"  [PASS] {t.__name__}")
        except Exception as e:                                   # noqa: BLE001
            bad += 1
            print(f"  [FAIL] {t.__name__}: {e!r}")
    print(f"\n{len(tests) - bad}/{len(tests)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
