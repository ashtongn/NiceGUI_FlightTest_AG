#!/usr/bin/env python3
"""
test_acm_thresholds.py: the measured-threshold option of dpmg/fec/acm.py.

  (a) the default (no option) is still the nominal table, unchanged
  (b) acm_table("measured") is strictly monotone in Es/N0 and efficiency,
      drops unmeasured and dominated rungs, and re-indexes
  (c) DPMG_ACM_THRESHOLDS=measured switches select_modcod / AcmController
  (d) an unknown source is refused; no measurement loaded: nominal fallback
  (e) the shipped MEASURED_ESN0_DB matches the replay result file it cites

Runs under pytest or as a script (exit 0 = all pass).
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from dpmg.fec import acm                                            # noqa: E402

NOMINAL = [1.0, 3.0, 5.0, 7.0, 8.5, 10.5, 11.5, 12.5, 14.0]


def _with_measured(values, fn):
    saved = dict(acm.MEASURED_ESN0_DB)
    acm.MEASURED_ESN0_DB.clear()
    acm.MEASURED_ESN0_DB.update(values)
    try:
        return fn()
    finally:
        acm.MEASURED_ESN0_DB.clear()
        acm.MEASURED_ESN0_DB.update(saved)


def test_default_is_nominal():
    os.environ.pop("DPMG_ACM_THRESHOLDS", None)
    assert [mc.req_esn0_db for mc in acm.ACM_TABLE] == NOMINAL
    assert acm.acm_table() is acm.ACM_TABLE
    assert acm.select_modcod(30.0).name == acm.ACM_TABLE[-1].name
    assert acm.AcmController().table is acm.ACM_TABLE


def test_measured_table_monotone_and_pruned():
    vals = {mc.name: 20.0 + i for i, mc in enumerate(acm.ACM_TABLE)}
    vals["BPSK-conv12"] = None                 # never met the FER target
    vals["8PSK-conv23"] = 30.0                 # dominated by 8PSK 3/4 at 24 dB
    tab = _with_measured(vals, lambda: acm.acm_table("measured"))
    names = [mc.name for mc in tab]
    assert "BPSK-conv12" not in names and "8PSK-conv23" not in names, names
    es = [mc.req_esn0_db for mc in tab]
    ef = [mc.spectral_eff for mc in tab]
    assert all(b > a for a, b in zip(es, es[1:])), es
    assert all(b > a for a, b in zip(ef, ef[1:])), ef
    assert [mc.idx for mc in tab] == list(range(len(tab)))


def test_env_switch():
    vals = {mc.name: 20.0 + i for i, mc in enumerate(acm.ACM_TABLE)}

    def run():
        os.environ["DPMG_ACM_THRESHOLDS"] = "measured"
        try:
            low = acm.select_modcod(15.0)          # below every measured rung
            ctl = acm.AcmController()
            mc = ctl.update(40.0)
            return low, mc, ctl.table
        finally:
            os.environ.pop("DPMG_ACM_THRESHOLDS", None)
    low, mc, tab = _with_measured(vals, run)
    assert low.name == tab[0].name
    assert mc.name == tab[-1].name and mc.req_esn0_db == 28.0


def test_bad_source_and_fallback():
    try:
        acm.acm_table("guess")
        raise AssertionError("unknown source accepted")
    except ValueError:
        pass
    tab = _with_measured({}, lambda: acm.acm_table("measured"))
    assert tab is acm.ACM_TABLE


def test_shipped_values_match_result_file():
    path = os.path.join(ROOT, acm.MEASURED_SOURCE)
    if not acm.MEASURED_ESN0_DB:
        return                                  # nothing shipped yet
    res = {r["modcod"]: r.get("proposed_db") for r in json.load(open(path))["results"]}
    for name, v in acm.MEASURED_ESN0_DB.items():
        assert res.get(name) == v, (name, v, res.get(name))
    tab = acm.acm_table("measured")
    assert len(tab) >= 2


def _main():
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    bad = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  [PASS] {name}")
        except AssertionError as e:
            bad += 1
            print(f"  [FAIL] {name}: {e}")
    print(f"{len(tests) - bad}/{len(tests)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(_main())
