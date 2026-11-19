#!/usr/bin/env python3
"""
test_decode_pool_spawn.py: the process decode pool runs the same code as its parent.

DecodePool(backend="process") spawns fresh interpreters. Each child must import
the decode modules from the same files the parent uses (an older copy of the
tree on the path once made children decode with stale code), must not load a
module twice under two names, and must decode a real burst.

  (a) the pool comes up with the process backend
  (b) one on-radio DSSS capture (tests/data/dsss_live) decodes in a child
  (c) every first-party module the child loaded comes from the same file as in
      the parent, and no file is loaded as two module objects in the child

Runs under pytest or as a script (exit 0 = all pass).
"""
from __future__ import annotations

import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import numpy as np                                   # noqa: E402

DATA = os.path.join(HERE, "data", "dsss_live")
CAPTURE = "g40_dsss_none_r1"

# evaluated inside the child: module name to (file, id) for first-party modules
CHILD_PROBE = ("{k: (__import__('os').path.realpath(m.__file__), id(m)) "
               "for k, m in list(__import__('sys').modules.items()) "
               "if getattr(m, '__file__', None) and "
               "__import__('os').path.realpath(m.__file__).startswith(%r)}")


def _spec():
    from dpmg.scenario.channel_plan import ChannelPlan
    from dpmg.dsp.decode_pool import DecodeSpec
    from dpmg.scenario.plan_adapter import plan_to_watchlist
    meta = json.load(open(os.path.join(DATA, CAPTURE + ".json")))
    bb = np.load(os.path.join(DATA, CAPTURE + ".npy"))
    cc = plan_to_watchlist(ChannelPlan.from_dict(meta["plan"]))[0][0]
    fs = float(meta["fs_out"])
    spec = DecodeSpec(modulation=cc.demod_type, fs_out=fs, sps=fs / cc.symbol_rate,
                      demod_params={"symbol_rate": cc.symbol_rate, **cc.demod_params},
                      lock={})
    return meta, bb, spec


def test_spawned_child_decodes_with_parent_code():
    from dpmg.dsp import decode_pool as dp
    meta, bb, spec = _spec()
    pool = dp.DecodePool(max_workers=1, backend="process")
    try:
        assert pool.stats()["backend"] == "process", pool.stats()
        assert pool.submit("k0", bb, spec)
        t0 = time.time()
        res = []
        while not res and time.time() - t0 < 120:
            res = pool.collect()
            time.sleep(0.05)
        assert res, "no decode result from the child within 120 s"
        r = res[0]
        assert r.ok and r.valid, f"child decode failed: ok={r.ok} valid={r.valid} err={r.error}"
        assert meta["message"] in (r.messages or [r.message]), r.messages
        child = pool._ex.submit(eval, CHILD_PROBE % os.path.realpath(ROOT)).result(timeout=60)
    finally:
        pool.shutdown()

    assert child, "child reported no first-party modules"
    by_file = {}
    for name, (f, ident) in child.items():
        by_file.setdefault(f, set()).add(ident)
    twice = {f: ids for f, ids in by_file.items() if len(ids) > 1}
    assert not twice, f"child loaded a file as two modules: {sorted(twice)}"

    differ = []
    for name, (f, _ident) in child.items():
        m = sys.modules.get(name)
        pf = getattr(m, "__file__", None)
        if pf and os.path.realpath(pf) != f:
            differ.append((name, pf, f))
    assert not differ, f"child and parent load different files: {differ}"
    names = set(child)
    for want in ("demod_helper", "frame_codec"):
        assert any(n == want or n.endswith("." + want) for n in names), \
            f"child did not load {want}: {sorted(names)}"
    print(f"  child loaded {len(child)} first-party modules, all from the parent's files")


if __name__ == "__main__":
    try:
        test_spawned_child_decodes_with_parent_code()
        print("PASS test_spawned_child_decodes_with_parent_code")
    except AssertionError as e:
        print(f"FAIL test_spawned_child_decodes_with_parent_code: {e}")
        sys.exit(1)
