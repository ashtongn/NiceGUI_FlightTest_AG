"""
test_secure_roundtrip.py: the full secure round trip through the repo's
replay path, for every modulation and every coding that can carry the tag:

  encode (seal then CRC then FEC, gen_scenario_json) then scenario IQ then AWGN +
  random arrival / phase (replay_chain): GPU-model channelizer then
  decode_pool._default_decode (sealed bodies): secure_frame.open_result
  (parent open: tag, replay window, freshness): plaintext == message.

Default pytest run (OFF-01 loop): every modulation uncoded + conv 1/2 + RS,
one rep each at 30 dB Es/N0 (~1-2 min). `python3 tests/test_secure_roundtrip.py
--full` runs every modulation x every coding and prints the matrix.

Replay-model limits inherited from bench/mod_fec_matrix.py (hardware is
authoritative): GFSK does not decode in the replay chain (the FSK
discriminator at 10 sps), and 8QAM / 32APSK are "record only" in the plan.
Those cells are reported, not asserted. Evidence name:
  test_secure_roundtrip_every_modulation   (SEC-11 / SEC-12 offline half)
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

import numpy as np
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "bench"))
import secure_testkit as tk  # noqa: E402
from dpmg.secure import api as sf  # noqa: E402
from dpmg.framing import frame_codec as fc  # noqa: E402

REPLAY_LIMITED = {"GFSK", "8QAM", "32APSK"}          # reported, not asserted
ESN0_DB = 30.0


def codings(full: bool):
    import mod_fec_matrix as m
    if full:
        return {k: v for k, v in m.CODINGS.items()}
    return {"none": None, "1/2": {"type": "conv", "rate": "1/2"}, "rs": {"type": "rs"}}


def run_cell(mod, label, fec, rng, work_dir, tx, rx, windows=3, keyings=3):
    """One (modulation, coding) cell: dict(decoded, verdicts, t_s, status)."""
    from dpmg.scenario import gen_scenario_json as g
    import mod_fec_matrix as m
    import replay_chain as rc
    cap = tx.capacity(fec, long_header=True)
    # Every return carries the full key set (run_matrix prints
    # t_s/verdicts); the tag-incapable coding is reported as not applicable.
    na = {"message": "", "decoded": False, "verdicts": [], "t_s": None, "na": False}
    if cap is None:
        return {**na, "status": "cannot_carry_tag", "na": True}
    message = f"SEC {mod} {label}"[:min(cap, 40)]
    spec = m.cell_spec(mod, "none", message, rc.__dict__.get("CENTER", 425_025_000), name=f"sec_{m._slug(mod)}_{m._slug(label)}")
    # Ground side: seal here, ship the on-air bits; the radio never seals
    spec["signals"][0]["bits"] = fc.encode_secure(message.encode(), fec, ctx=tx)
    spec["signals"][0]["secure"] = True
    if fec is not None:
        spec["signals"][0]["fec"] = dict(fec)
    spec["sample_rate_hz"] = 1_920_000.0
    # size the loop to the (longer) sealed frame
    try:
        iq0, _e = g.build_signal(spec["signals"][0], 1_920_000.0)
        spec["loop_s"] = max(0.2, float(np.ceil(len(iq0) / 1_920_000.0 * 1.1 / 0.05) * 0.05))
    except Exception as e:                                           # noqa: BLE001
        return {**na, "message": message, "status": f"build_fail: {e}"}
    res = {**na, "message": message, "status": ""}
    try:
        g.build_scenario(spec, work_dir)
        iq, plan, wl = rc.load_scenario(work_dir, spec["name"])
    except Exception as e:                                           # noqa: BLE001
        res["status"] = f"build_fail: {e}"
        return res
    ch = wl[0][0]
    fs = float(plan.sample_rate_hz)
    _fo, decim = rc.worker_fs_out(wl, fs)
    ps = rc.burst_power(iq)
    n0 = ps / (float(ch.symbol_rate) * 10 ** (ESN0_DB / 10.0))
    for _key in range(keyings):
        lock = {}
        base = (np.roll(np.tile(iq, 2), int(rng.integers(0, len(iq))))
                * np.exp(1j * rng.uniform(0, 2 * np.pi))).astype(np.complex64)
        bb0, fse0 = rc.channelize_loop(rc.spectrum(base), fs, ch.f_center_hz - plan.center_freq_hz, decim)
        rc.decode(bb0, ch, fse0, lock)                              # warm seed (not scored)
        for _w in range(windows):
            x = rc.awgn(base, n0, fs, rng)
            bb, fse = rc.channelize_loop(rc.spectrum(x), fs, ch.f_center_hz - plan.center_freq_hz, decim)
            t0 = time.time()
            r = rc.decode(bb, ch, fse, lock)
            if r.valid and r.sealed:
                r = sf.open_result(r, rx)
                res["t_s"] = round(time.time() - t0, 3)
                res["verdicts"] += [v["verdict"] for v in (r.secure or {}).get("verdicts", [])]
                if r.valid and message in r.messages:
                    res["decoded"] = True
                    res["status"] = "decoded"
                    return res
            else:
                res["status"] = "no_sealed_frame" if not r.error else f"error: {r.error}"
    if not res["status"]:
        res["status"] = "opened_but_no_message"
    return res


def run_matrix(full=False, mods=None, verbose=True):
    import mod_fec_matrix as m
    tx, rx = tk.pair()                      # the production context (AES-256-GCM-SIV, counter+slot nonce)
    rng = np.random.default_rng(2026)
    out = {}
    with tempfile.TemporaryDirectory() as d:
        for mod in (mods or m.MODS_ALL):
            for label, fec in codings(full).items():
                r = run_cell(mod, label, fec, rng, d, tx, rx)
                out[(mod, label)] = r
                if verbose:
                    flag = "ok  " if r["decoded"] else ("n/a " if r.get("na") else "MISS")
                    print(f"  {mod:<10} {label:<8} {flag} {r['status']:<22} "
                          f"{'' if r['t_s'] is None else str(r['t_s']) + ' s'} {','.join(r['verdicts'][:3])}",
                          flush=True)
    return out


def test_secure_roundtrip_every_modulation():
    import mod_fec_matrix as m
    out = run_matrix(full=False, verbose=True)
    misses = [(mod, lab) for (mod, lab), r in out.items() if not r["decoded"] and mod not in REPLAY_LIMITED]
    assert not misses, f"secure round trip failed: {misses}"
    # every cell that reached the opener authenticated exactly (no AUTH_FAIL on a clean body)
    for (mod, lab), r in out.items():
        if r["decoded"]:
            assert sf.AUTH_OK in r["verdicts"]
    assert len(out) == len(m.MODS_ALL) * 3


def test_secure_roundtrip_all_codings_fsk():
    """Every coding that can carry the tag, on FSK (fast), plus the refusal."""
    import mod_fec_matrix as m
    tx, rx = tk.pair()
    rng = np.random.default_rng(7)
    fails = []
    with tempfile.TemporaryDirectory() as d:
        for label, fec in m.CODINGS.items():
            r = run_cell("FSK", label, fec, rng, d, tx, rx, windows=2)
            if label == "tpc-16":
                assert r["status"] == "cannot_carry_tag"
                continue
            if not r["decoded"]:
                fails.append((label, r["status"]))
    assert not fails, fails


if __name__ == "__main__":  # OFF-01 loop runs `python3 tests/<file>.py`; delegate to pytest
    if "--full" in sys.argv:
        out = run_matrix(full=True)
        n_ok = sum(1 for r in out.values() if r["decoded"])
        n_na = sum(1 for r in out.values() if r.get("na"))   # Not applicable
        print(f"\nsecure full matrix: {n_ok}/{len(out) - n_na} decoded "
              f"({n_na} not applicable: coding cannot carry the tag)")
        sys.exit(0)
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
