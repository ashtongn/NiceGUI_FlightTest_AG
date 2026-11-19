"""
test_dsss_builder_validate.py: the DSSS builder's input checks
(dsss_builder_validate.validate_builder, DSSS_VALIDATE).

The three DSSS presets (4-user BPSK, mixed carriers, 16-user) produce no
errors; every rule fires on a case built to break it, with the right level
and the input (field) it names, and its message says what to change. Also the
panel-shaped spec (raw widget values: empty fields, poly text) and the
single-code check used by "Add custom LFSR".

pytest collects the test_* functions; `python3 tests/test_dsss_builder_validate.py`
runs them all (exit 0 = pass).
"""
from __future__ import annotations

import copy
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from dpmg.scenario import gen_scenario_json as g
from dpmg.scenario import dsss_builder_validate as V

PRESETS = {s["name"]: s for s in g.dsss_preset_specs()}


def _spec(name="dsss_4user_bpsk", **top):
    s = copy.deepcopy(PRESETS[name])
    s.update(top)
    return s


def _users(n, **kw):
    return [dict({"id": f"U{k + 1:02d}", "code": k, "message": f"U{k + 1:02d}"}, **kw) for k in range(n)]


def _find(issues, level, field=None, text=None):
    return [i for i in issues if i[0] == level and (field is None or i[1] == field)
            and (text is None or text.lower() in i[2].lower())]


def _expect(spec, level, field, text, **kw):
    issues = V.validate_builder(spec, **kw)
    hit = _find(issues, level, field, text)
    assert hit, f"expected {level} on {field!r} containing {text!r}; got {issues}"
    return hit[0][2]


# valid presets

def test_presets_have_no_errors():
    for name, spec in PRESETS.items():
        issues = V.validate_builder(spec, with_info=True)
        assert not V.errors(issues), (name, V.errors(issues))
        assert _find(issues, V.INFO, "degree", "-- OK"), (name, issues)


def test_16user_preset_warns_capacity_not_error():
    issues = V.validate_builder(PRESETS["dsss_16user"])
    msg = _find(issues, V.WARNING, "count", "16 equal-power users on 127 chips")
    assert msg and "255" in msg[0][2] and "13 users" in msg[0][2], issues
    assert not V.errors(issues)


def test_4user_capacity_ok_and_amplitude_sum_explained():
    issues = V.validate_builder(PRESETS["dsss_4user_bpsk"])
    assert not _find(issues, V.WARNING, "count")
    w = _find(issues, V.WARNING, "amplitude", "add up to 3.20")
    assert w and "0.45 of full scale" in w[0][2] and "nothing clips" in w[0][2]


# degree / code family

def test_degree_out_of_library_range():
    s = _spec(); s["code_set"]["n"] = 20
    _expect(s, V.ERROR, "degree", "2..16")
    s = _spec(); s["code_set"]["n"] = 1
    _expect(s, V.ERROR, "degree", "2..16")


def test_degree_too_low_for_users_gold():
    s = _spec(); s["code_set"].update(n=5, count=40); s["users"] = _users(40)
    m = _expect(s, V.ERROR, "degree", "Gold n=5 gives 33 codes")
    assert "raise the degree to 6" in m


def test_gold_no_pair_for_small_n_suggests_degree():
    s = _spec(); s["code_set"].update(n=3, count=16); s["users"] = _users(16)
    m = _expect(s, V.ERROR, "degree", "no Gold preferred pair for n=3")
    assert "n=5 or more" in m and "16 users" in m


def test_gold_none_for_multiple_of_4():
    s = _spec(); s["code_set"]["n"] = 8; s["code_set"].pop("length")
    _expect(s, V.ERROR, "degree", "divisible by 4")


def test_mseq_phases_capacity_and_shift_warning():
    s = _spec(); s["code_set"] = {"kind": "mseq_phases", "poly": [3, 1], "count": 9}
    s["users"] = _users(9)
    _expect(s, V.ERROR, "degree", "only 7 distinct phases")
    s = _spec(); s["code_set"] = {"kind": "mseq_phases", "poly": [5, 2], "count": 4}
    _expect(s, V.WARNING, "set_kind", "shifts of each other")


def test_mseq_degree_ignored_warning():
    s = _spec(); s["code_set"] = {"kind": "mseq_phases", "poly": [5, 2], "n": 7, "count": 4}
    _expect(s, V.WARNING, "degree", "degree n=7 is ignored")


def test_polys_capacity():
    s = _spec(); s["code_set"] = {"kind": "polys", "n": 5, "count": 8}; s["users"] = _users(8)
    _expect(s, V.ERROR, "degree", "5 primitive polynomial(s) of degree 5")


def test_polynomial_not_primitive():
    s = _spec(); s["code_set"] = {"kind": "mseq_phases", "poly": [5, 3, 1], "count": 4}
    m = _expect(s, V.ERROR, "poly", "not primitive")
    assert "15 chips instead of 31" in m


def test_polynomial_unreadable():
    s = _spec(); s["code_set"] = {"kind": "mseq_phases", "poly": "x^5+", "count": 4}
    s["code_set"]["poly"] = "zz"
    _expect(s, V.ERROR, "poly", "cannot be read")


def test_gold_explicit_pair_degree_taps_and_preference():
    s = _spec(); s["code_set"] = {"kind": "gold", "n": 5, "count": 4, "polys": [[7, 3], [5, 4, 3, 2]]}
    _expect(s, V.ERROR, "poly", "beyond degree n=5")
    s = _spec(); s["code_set"] = {"kind": "gold", "n": 7, "count": 4, "polys": [[5, 2], [5, 4, 3, 2]]}
    _expect(s, V.ERROR, "poly", "is degree 5 but n=7")
    s = _spec(); s["code_set"] = {"kind": "gold", "n": 5, "count": 4, "polys": [[5, 2], [5, 3]]}
    _expect(s, V.WARNING, "poly2", "not a preferred pair")
    s = _spec(); s["code_set"] = {"kind": "gold", "n": 5, "count": 4, "polys": [[5, 2], [5, 2]]}
    _expect(s, V.ERROR, "poly2", "equals polynomial A")
    s = _spec(); s["code_set"] = {"kind": "gold", "n": 5, "count": 4, "polys": [[5, 2], [5, 4, 3, 2]]}
    assert not V.errors(V.validate_builder(s))


def test_seed_all_zero_and_too_wide():
    s = _spec(); s["code_set"]["state"] = 0
    _expect(s, V.ERROR, "seed", "all-zero")
    s = _spec(); s["code_set"]["state"] = 40
    _expect(s, V.ERROR, "seed", "does not fit a 5-bit register")


def test_length_longer_than_period_or_too_short():
    s = _spec(); s["code_set"]["length"] = 40
    _expect(s, V.ERROR, "length", "longer than the 31-chip period")
    s = _spec(); s["code_set"]["length"] = 5
    _expect(s, V.ERROR, "length", "too short to spread")


def test_custom_list_checks():
    s = _spec(); s["code_set"] = {"kind": "list", "codes": []}
    _expect(s, V.ERROR, "custom", "custom set is empty")
    s = _spec(); s["code_set"] = {"kind": "list", "codes": [{"family": "lfsr", "poly": [5, 3, 1]},
                                                            {"family": "lfsr", "poly": [5, 2]}]}
    s["users"] = _users(2)
    _expect(s, V.ERROR, "custom", "not primitive")
    s = _spec(); s["code_set"] = {"kind": "list", "codes": [{"family": "lfsr", "poly": [5, 2]},
                                                            {"family": "lfsr", "poly": [5, 2]}]}
    s["users"] = _users(2)
    _expect(s, V.ERROR, "custom", "identical")


def test_validate_code_spec_single():
    assert not V.errors(V.validate_code_spec({"family": "lfsr", "poly": "0x12", "state": 3}))
    assert V.errors(V.validate_code_spec({"family": "lfsr", "poly": [5, 3, 1]}))
    assert V.errors(V.validate_code_spec({"family": "lfsr", "poly": [5, 2], "state": 0}))
    assert V.errors(V.validate_code_spec({"family": "lfsr", "poly": [5, 2], "length": 3}))


# users / codes

def test_duplicate_code_index():
    s = _spec(); s["users"][1]["code"] = 0
    _expect(s, V.ERROR, "user:U02", "both use code #0")


def test_same_code_at_separated_centres_is_fine():
    s = _spec(); s["users"][1]["code"] = 0
    s["users"][1]["offset_hz"] = -300_000.0
    assert not _find(V.validate_builder(s), V.ERROR, "user:U02", "both use")


def test_code_index_beyond_set():
    s = _spec(); s["users"][1]["code"] = 7
    m = _expect(s, V.ERROR, "count", "uses code #7 but the set has 4")
    assert "raise 'codes (N users)' to 8" in m


def test_dual_code_rules():
    s = _spec(); s["users"][0].update(carrier="qpsk", code_q=0)
    _expect(s, V.ERROR, "user:U01", "equals the I-rail code")
    s = _spec(); s["users"][0].update(code_q=2)          # bpsk default carrier
    _expect(s, V.ERROR, "user:U01", "needs a qpsk or oqpsk carrier")
    s = _spec(); s["users"][0].update(carrier="qpsk", code_q=3); s["users"] = s["users"][:3]
    assert not V.errors(V.validate_builder(s))


def test_no_users_and_duplicate_ids():
    s = _spec(users=[])
    _expect(s, V.ERROR, "users", "no users")
    s = _spec(); s["users"][1]["id"] = "U01"
    _expect(s, V.ERROR, "user:U01", "used twice")


# rates and bandwidth

def test_symbol_rate_nonpositive():
    s = _spec(); s["dsss"]["symbol_rate"] = 0
    m = _expect(s, V.ERROR, "symbol_rate", "must be > 0")
    assert m.startswith("all 4 users")                  # one message, not four


def test_chip_rate_beyond_nyquist():
    s = _spec(); s["dsss"]["symbol_rate"] = 40_000      # 31 chips: 1.24 Mchip/s at 1.92 MSps
    m = _expect(s, V.ERROR, "symbol_rate", "2 samples per chip")
    assert "30968 Bd or less" in m


def test_chip_rate_hz_form():
    s = _spec(); s["dsss"].pop("symbol_rate"); s["dsss"]["chip_rate_hz"] = 1_500_000
    _expect(s, V.ERROR, "symbol_rate", "2 samples per chip")
    s = _spec(); s["dsss"].pop("symbol_rate"); s["dsss"]["chip_rate_hz"] = 74_400
    assert not V.errors(V.validate_builder(s))


def test_samples_per_chip():
    s = _spec(); s["dsss"]["samples_per_chip"] = 1
    _expect(s, V.ERROR, "samples_per_chip", ">= 2")
    s = _spec(); s["dsss"].update(samples_per_chip=3, carrier="oqpsk")
    _expect(s, V.ERROR, "samples_per_chip", "OQPSK needs an even")
    s = _spec(); s["dsss"]["samples_per_chip"] = 16; s["dsss"]["symbol_rate"] = 9600
    _expect(s, V.WARNING, "samples_per_chip", "resamples it down")


def test_offset_pushes_user_out_of_band():
    s = _spec(); s["dsss"]["offset_hz"] = 880_000
    m = _expect(s, V.ERROR, "offset_hz", "beyond the usable")
    assert "837.6 kHz" in m


def test_overlapping_centres_warn():
    s = _spec(); s["dsss"]["spacing_hz"] = 50_000
    _expect(s, V.WARNING, "spacing_hz", "spectra overlap")
    s = _spec(); s["dsss"]["spacing_hz"] = 160_000; s["dsss"]["offset_hz"] = -240_000
    assert not _find(V.validate_builder(s), V.WARNING, "spacing_hz")


def test_sample_rate_and_radio_mismatch():
    s = _spec(sample_rate_hz=0)
    _expect(s, V.ERROR, "sample_rate", "must be > 0")
    _expect(_spec(), V.WARNING, "sample_rate", "radio runs 7.68 MSps", radio_rate=7.68e6)


def test_live_decode_window_warning():
    s = _spec("dsss_16user", sample_rate_hz=30.72e6)
    _expect(s, V.WARNING, "symbol_rate", "never decode live")
    assert not _find(V.validate_builder(PRESETS["dsss_16user"]), V.WARNING, "symbol_rate")


# multi-user interference

def test_mui_below_threshold_warns_with_number():
    s = _spec(); s["code_set"]["count"] = 20; s["users"] = _users(20)
    m = _expect(s, V.WARNING, "users", "predicted despread SINR")
    assert " dB " in m and "below the ~8 dB" in m
    _expect(s, V.WARNING, "count", "20 equal-power users on 31 chips")


def test_near_far_weights_mui():
    s = _spec(); s["users"][0]["amplitude"] = 0.1
    _expect(s, V.WARNING, "amplitude", "near-far")
    _expect(s, V.WARNING, "users", "for U01")


# messages / frames

def test_message_empty_and_too_long():
    s = _spec(); s["users"][0]["message"] = ""
    _expect(s, V.ERROR, "user:U01", "message is empty")
    s = _spec(); s["users"][0]["message"] = "X" * 300
    _expect(s, V.ERROR, "user:U01", "at most 255")


def test_message_vs_fec_block():
    s = _spec(); s["users"][0].update(message="X" * 40, fec={"type": "tpc", "code": "eH(16,11)^2"})
    m = _expect(s, V.ERROR, "user:U01", "one tpc")
    assert "12 bytes" in m
    s = _spec(); s["users"][0].update(message="U01 PING", fec={"type": "tpc", "code": "eH(16,11)^2"})
    assert not _find(V.validate_builder(s), V.ERROR, "user:U01")
    s = _spec(); s["users"][0].update(fec={"type": "turbo"})
    _expect(s, V.ERROR, "user:U01", "unknown fec type")


def test_frame_longer_than_loop():
    s = _spec(loop_s=0.02)
    _expect(s, V.ERROR, "loop_s", "loop would cut the frame")


# amplitudes

def test_amplitude_bounds():
    s = _spec(); s["users"][0]["amplitude"] = 0
    _expect(s, V.ERROR, "user:U01", "(0, 1]")
    s = _spec(); s["users"][0]["amplitude"] = 1.5
    _expect(s, V.ERROR, "user:U01", "(0, 1]")
    s = _spec(); s["dsss"]["amplitude"] = -1
    _expect(s, V.ERROR, "amplitude", "(0, 1]")


# the panel's raw spec

def _panel_spec(**over):
    """What dsss_builder_tab._collect_spec sends for the default panel state."""
    s = {"name": "dsss_builder", "sample_rate_hz": 1_920_000, "center_freq_hz": 425_025_000,
         "loop_s": 0.25, "signals": [],
         "code_set": {"kind": "gold", "n": 5, "count": 4, "form": "fibonacci", "state": 1},
         "dsss": {"symbol_rate": 2400, "samples_per_chip": 2, "offset_hz": 300000.0, "carrier": "bpsk",
                  "amplitude": 0.8, "stagger_chips": 7, "spacing_hz": 0},
         "users": _users(4, amplitude=0.8, carrier="bpsk")}
    for k, v in over.items():
        if k in s["code_set"] or k in ("n", "count", "poly", "polys", "kind", "length"):
            s["code_set"][k] = v
        elif k in s["dsss"]:
            s["dsss"][k] = v
        else:
            s[k] = v
    return s


def test_panel_default_ok_and_empty_inputs():
    assert not V.errors(V.validate_builder(_panel_spec()))
    _expect(_panel_spec(n=None), V.ERROR, "degree", "empty")
    _expect(_panel_spec(count=None), V.ERROR, "count", "empty")
    _expect(_panel_spec(symbol_rate=None), V.ERROR, "symbol_rate", "(empty)")
    _expect(_panel_spec(name=""), V.ERROR, "name", "empty")
    _expect(_panel_spec(name="a/b"), V.ERROR, "name", "letters, digits")


def test_panel_hex_poly_mseq():
    s = _panel_spec(kind="mseq_phases", poly="0x12", count=3)
    s["users"] = _users(3)
    issues = V.validate_builder(s)
    assert not V.errors(issues), issues
    s = _panel_spec(kind="mseq_phases", poly="0x1", count=3)
    _expect(s, V.ERROR, "poly", "cannot be read")


def test_code_set_errors_scope():
    issues = V.validate_builder(_panel_spec(users=[]))
    assert V.errors(issues) and not V.code_set_errors(issues)      # Build codes stays allowed
    issues = V.validate_builder(_panel_spec(n=3))
    assert V.code_set_errors(issues)


def test_preview_papr_matches_built_iq():
    """The preview's PAPR is the composite's own (16 users ~11 dB, not 31.6)."""
    import numpy as np
    from dpmg.web import dsss_builder_tab as T
    spec = PRESETS["dsss_16user"]
    ex = g.expand_users(spec)
    fs = float(spec["sample_rate_hz"]); n = int(fs * float(spec["loop_s"]))
    mix = np.zeros(n, dtype=np.complex64)
    for sig in ex["signals"]:
        iq, _e = g.build_signal(sig, fs)
        mix += np.tile(iq, int(np.ceil(n / len(iq))))[:n]
    papr = T._papr_db(mix)
    assert abs(papr - T._papr_db(mix * (0.9 / np.max(np.abs(mix))))) < 1e-3   # scale-free
    assert 8.0 < papr < 14.0, papr


def test_fast_enough_for_live_use():
    import time
    V.validate_builder(PRESETS["dsss_16user"])
    t = time.time()
    for _ in range(5):
        V.validate_builder(PRESETS["dsss_16user"])
    assert (time.time() - t) / 5 < 0.25


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:                           # noqa: BLE001
                fails += 1
                print(f"FAIL {name}: {e}")
    print(f"{fails} failure(s)")
    sys.exit(1 if fails else 0)
