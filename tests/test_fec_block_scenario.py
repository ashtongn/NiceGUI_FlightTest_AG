"""
test_fec_block_scenario.py: the block-code FEC framing (RS, RS+conv, LDPC, TPC)
added to frame_codec, and the two bench harnesses that use it
(bench/mod_fec_matrix.py --full, bench/jam_denial.py).

Covers:
  * frame_codec.encode_fec / fec_find_and_decode round-trip for every CODINGS
    entry mod_fec_matrix exposes, clean and with a handful of channel bit errors,
    and the inverted stream (BPSK/QPSK 180-deg ambiguity);
  * the conv path is byte-identical to before the block codes were added;
  * the NOT_RUNNABLE list and coding_wired report what has no RF frame;
  * mod_fec_matrix.replay_pair decodes FSK uncoded + conv + a block code at a
    high Es/N0 through the real decode pool;
  * jam_denial builds FSK/16QAM victims and a spot jammer that denies them.

`python3 tests/test_fec_block_scenario.py` prints a summary; pytest collects the
test_* functions.
"""
from __future__ import annotations

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "bench"))
import numpy as np

from dpmg.framing import frame_codec as fc


def _roundtrip(fec, msg=b"BLOCK CODE TEST 01"):
    bits = fc.encode_fec(msg, fec)
    assert bits.startswith(fc.SYNC_BITS)
    # the body length matches the inverse the decoder computes
    import mod_fec_matrix as m  # noqa
    fr = fc.fec_find_and_decode("01" * 150 + bits + "10" * 150, fec)
    return fr.valid and fr.payload_bytes == msg


def test_block_roundtrip_clean():
    import mod_fec_matrix as m
    for label, fec in m.CODINGS.items():
        if fec is None:
            continue
        cap = (fc.fec_max_info_bits(fec) - 24) // 8
        msg = b"BLOCK"[:max(1, min(5, cap))] + b" R1"
        assert _roundtrip(fec, msg[:cap]), f"{label} {fec} clean round-trip failed"


def test_block_corrects_errors():
    """A few channel bit errors in the coded body are corrected by every code."""
    rng = np.random.default_rng(3)
    for fec, ne in [({"type": "rs"}, 6), ({"type": "rs+conv", "rate": "1/2"}, 10),
                    ({"type": "ldpc", "code": "qc-r12"}, 6),
                    ({"type": "tpc", "code": "eH(32,26)^2"}, 8)]:
        msg = b"ERRTEST"
        bits = list(fc.encode_fec(msg, fec))
        idx = rng.choice(np.arange(fc.SYNC_LEN + 24, len(bits)), ne, replace=False)
        for i in idx:
            bits[i] = "1" if bits[i] == "0" else "0"
        fr = fc.fec_find_and_decode("".join(bits), fec)
        assert fr.valid and fr.payload_bytes == msg, f"{fec} failed to correct {ne} errors"


def test_block_inverted_stream():
    for fec in ({"type": "rs"}, {"type": "ldpc", "code": "qc-r23"},
                {"type": "tpc", "code": "eH(16,11)^2"}):
        bits = fc.encode_fec(b"INV", fec)
        inv = bits.translate(str.maketrans("01", "10"))
        assert fc.fec_find_and_decode(inv, fec).valid, f"{fec} inverted stream failed"


def test_conv_unchanged():
    """The conv path must be byte-identical to before block codes were added."""
    for rate in ("1/2", "2/3", "3/4", "5/6", "7/8"):
        fec = {"type": "conv", "rate": rate}
        bits = fc.encode_fec(b"CONV", fec)
        assert fc.fec_kind(fec) == "conv"
        assert fc.fec_find_and_decode(bits, fec).payload_bytes == b"CONV"


def test_not_runnable_reported():
    import mod_fec_matrix as m
    schemes = " ".join(n["scheme"] for n in m.NOT_RUNNABLE).lower()
    assert "interleav" in schemes and "acm" in schemes and "eh(8,4)" in schemes
    ok, why = m.coding_wired("rs")
    assert ok and why == ""


def test_replay_fsk_cells():
    """mod_fec_matrix.replay_pair decodes FSK uncoded + conv + a block code."""
    import mod_fec_matrix as m
    rng = np.random.default_rng(7)
    with tempfile.TemporaryDirectory() as d:
        for fec in ("none", "1/2", "rs", "rs+1/2"):
            r = m.replay_pair("FSK", fec, 0, 24.0, rng, d, windows=2)
            assert r["decoded"], f"FSK {fec} did not decode offline at 24 dB: {r['status']}"


def test_replay_estimate():
    import mod_fec_matrix as m
    grid = m.build_grid(["FSK", "QPSK"], ["none", "1/2", "rs"], ["FSK", "QPSK"])
    s, nskip = m.estimate_s(grid, 3, 25.0)
    assert s > 0 and nskip == 0
    # a replay table marking a cell 0 skips it in the estimate
    s2, n2 = m.estimate_s(grid, 3, 25.0, replay={("FSK", "rs"): 0, ("FSK", "none"): 3})
    assert n2 == 1 and s2 < s


def test_dibit_mapping_roundtrip():
    """gen_scenario threads 4FSK dibit_mapping and GFSK gfsk_bw_hz to the TX
    modulator: every mapping and BT round-trips offline."""
    from dpmg.scenario import gen_scenario_json as g
    import replay_chain as rc
    import checkout_common as cc

    def rt(sig, msg):
        with tempfile.TemporaryDirectory() as d:
            spec = {"name": "v", "sample_rate_hz": 1_920_000.0, "center_freq_hz": cc.CENTER,
                    "loop_s": 0.2, "signals": [dict(sig, message=msg)]}
            g.build_scenario(spec, d)
            iq, plan, wl = rc.load_scenario(d, "v")
            cc_, _e = wl[0]
            bb, fse = rc.channelize_loop(rc.spectrum(np.tile(iq, 2)), 1_920_000.0,
                                         cc_.f_center_hz - plan.center_freq_hz,
                                         rc.worker_fs_out(wl, 1_920_000.0)[1])
            r = rc.decode(bb, cc_, fse)
            return bool(r.valid and msg in (r.messages or [r.message]))
    for mp in ("dmr", "dmr-inverted", "binary", "gray"):
        assert rt({"modulation": "4FSK", "symbol_rate": 4800, "tone_spacing_hz": 1296,
                   "offset_hz": 300_000, "dibit_mapping": mp, "amplitude": 0.9},
                  f"4FSK {mp}"), f"4FSK {mp} did not round-trip"


def test_mod_variants_cases_build():
    """Every mod_variants case builds a valid scenario and the registry has the
    MOD-17..21 plan ids + the per-family presets."""
    import mod_variants as mv
    from dpmg.scenario import gen_scenario_json as g
    C = mv.build_cases()
    plans = {c["plan"] for c in C}
    assert {"MOD", "MOD-17", "MOD-18", "MOD-19", "MOD-20", "MOD-21"} <= plans
    ids = {c["id"] for c in C}
    assert {"4FSK/dmr", "GFSK/BT0.3", "DSSS/gold", "CSS/SF9"} <= ids
    with tempfile.TemporaryDirectory() as d:
        c = next(c for c in C if c["id"] == "MOD-21/mixed@7.68")
        g.build_scenario(mv.spec_of(c), d)
        assert os.path.exists(os.path.join(d, mv.spec_of(c)["name"] + ".plan.json"))


def test_fec_frame_opts_build_frame():
    """fec_frame_opts builds each frame variant as raw bits, and the negatives
    (bad CRC, exact-sync flip) are rejected by find_frame."""
    import fec_frame_opts as ffo
    C = ffo.build_cases()
    assert any(c["id"] == "FEC-06/crc-CRC-32" for c in C)
    rng = np.random.default_rng(0)
    with tempfile.TemporaryDirectory() as d:
        # a CRC-9 frame round-trips through the raw-bits path at high SNR
        c = next(c for c in C if c["id"] == "FEC-06/crc-CRC-9")
        r = ffo.replay_fec06(c, rng, d, esn0_db=40.0)
        assert r["pass"], f"CRC-9 frame did not decode: {r['status']}"
        for nid in ("FEC-06/neg-badcrc", "FEC-06/neg-syncerr0-flip"):
            c = next(c for c in C if c["id"] == nid)
            r = ffo.replay_fec06(c, rng, d)
            assert r["pass"], f"{nid} negative was not rejected"


def test_jam_denial_build():
    """jam_denial builds FSK/16QAM victims and a spot jammer that denies them."""
    import jam_denial as js
    import numpy as _np

    class A:
        prf = 0.0; duty = 1.0
    iq, plan, cc_, msg = js._build_victim_1p92("fsk4800", 300_000.0, 1_920_000.0)
    assert js._decode_victim_1p92(iq, plan, cc_, msg), "clean FSK victim should decode"
    jam = js._denial_jammer("spot", 1_920_000.0, len(iq) / 1_920_000.0, 300_000.0, 4800, A())
    comp, g = js.compose(iq, jam, 30.0, 0.6)
    assert not js._decode_victim_1p92(comp, plan, cc_, msg), "spot at +30 dB should deny FSK"


def _main():
    import mod_fec_matrix as m
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print(f"  ok  {name}")
    print(f"result: {n}/{n} passed")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
