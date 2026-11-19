"""
test_dsss_multiuser.py: DSSS carriers and multi-user (CDMA overlay) scenarios
through the channelizer plan path, offline.

Same chain as test_dsss_scenario_roundtrip: gen_scenario_json.build_scenario
to .iq.bin + .plan.json then plan_adapter.plan_to_watchlist then the worker's gate
fs_out then channelize then decode_pool._default_decode with the DecodeSpec the
worker builds (one per user, from the same band): with a random arrival
offset, carrier phase, AWGN and the G40-like carrier offset (a fixed +/- CFO
up to 300 Hz plus a slow drift) per rep.

Cases:
  * every carrier family (BPSK / QPSK / OQPSK / pi4-QPSK) as a plain DSSS
    signal with an lfsr code, plus the dual-code QPSK / OQPSK option and the
    legacy-name aliases (OQPSK-DSSS, PI4QPSK-DSSS), and chip_rate_hz
  * the 4-user BPSK overlay, the mixed-carrier overlay and the 16-user overlay
    presets: per-user decode, per-user raw bit errors (sync-aligned against the
    framed TX bits) and the despread interference level, against the
    prediction recorded in the plan's dsss_overlay block
  * the JSON / plan schema: users expand to signals with explicit code specs
    and user meta, plan_adapter forwards code_spec / code_spec_q / carrier,
    and bad user specs are refused with a clear error

pytest collects the test_* functions; `python3 tests/test_dsss_multiuser.py`
prints the per-user table (exit 0 = all pass).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import numpy as np

from dpmg.framing import frame_codec as fc
from dpmg.scenario import gen_scenario_json as g
from dpmg.dsp.decode_pool import DecodeSpec, _default_decode
from dpmg.demodulation.demodulator import get_demodulator
from dpmg.scenario.plan_adapter import load_plan, plan_to_watchlist
from test_dsss_scenario_roundtrip import channelize, worker_fs_out
# the replay helpers live in the product module the DSSS builder panel also uses
from dpmg.scenario.dsss_replay import impair, _raw_ber, decode_users, _expected_message, _despread_sinr, build, HW_WINDOW_S  # noqa: E402,F401

FS = 1_920_000.0
OFF = 300_000.0
SNR_DB = 25.0
CFO_HZ = 250.0              # G40-like: a few hundred Hz between undisciplined ends
DRIFT_HZ_S = 5.0            # slow LO drift over the loop
REPS = 2

CARRIER_CASES = [
    ("DSSS", {"carrier": "bpsk", "lfsr": {"poly": [5, 2], "state": 1}}, "bpsk lfsr x^5+x^2+1"),
    ("DSSS", {"carrier": "qpsk", "lfsr": {"poly": "0x12", "state": 9}}, "qpsk lfsr hex 0x12"),
    ("DSSS", {"carrier": "oqpsk", "lfsr": {"poly": [6, 1], "form": "galois"}}, "oqpsk lfsr galois n=6"),
    ("DSSS", {"carrier": "pi4qpsk", "code_spec": {"family": "gold", "polys": [[5, 2], [5, 4, 3, 2]],
                                                 "offset": 3}}, "pi4qpsk gold explicit pair"),
    ("DSSS", {"carrier": "qpsk", "code_spec": {"family": "gold", "polys": [[5, 2], [5, 4, 3, 2]], "offset": 1},
              "code_spec_q": {"family": "gold", "polys": [[5, 2], [5, 4, 3, 2]], "offset": 6}},
     "qpsk DUAL code (I/Q)"),
    ("DSSS", {"carrier": "oqpsk", "code_spec": {"family": "gold", "polys": [[5, 2], [5, 4, 3, 2]], "offset": 2},
              "code_spec_q": {"family": "gold", "polys": [[5, 2], [5, 4, 3, 2]], "offset": 9}},
     "oqpsk DUAL code (I/Q)"),
    ("OQPSK-DSSS", {"lfsr": {"poly": [5, 2]}}, "alias OQPSK-DSSS"),
    ("PI4QPSK-DSSS", {"lfsr": {"poly": [5, 2]}}, "alias PI4QPSK-DSSS"),
    ("DSSS", {"carrier": "bpsk", "lfsr": {"poly": [5, 2]}, "chip_rate_hz": 74_400, "symbol_rate": None},
     "chip_rate_hz 74.4k (= 2400 Bd x 31)"),
    ("DSSS", {"carrier": "bpsk", "lfsr": {"poly": [6, 1], "length": 48}}, "truncated 48 of 63 chips"),
]


def carrier_spec(mod, extra, k):
    sig = {"modulation": mod, "symbol_rate": 2400, "offset_hz": OFF, "message": f"{mod} C{k}",
           "amplitude": 0.9, "name": f"C{k}", "user": f"C{k}"}
    sig.update(extra)
    sig = {kk: v for kk, v in sig.items() if v is not None}
    return {"name": f"dsss_car_{k}", "sample_rate_hz": FS, "center_freq_hz": 425_025_000.0,
            "loop_s": 0.25, "signals": [sig]}


# tests

def test_every_carrier_family_round_trips_through_plan_path():
    rng = np.random.default_rng(0xDC55)
    tmp = tempfile.mkdtemp(prefix="dsss_mu_")
    try:
        for k, (mod, extra, tag) in enumerate(CARRIER_CASES):
            spec = carrier_spec(mod, extra, k)
            iq, plan, raw = build(spec, tmp)
            e = raw["entries"][0]
            assert e["params"].get("code_spec"), f"{tag}: plan entry has no explicit code_spec"
            assert e["params"].get("dsss_modem") in ("bpsk", "qpsk", "oqpsk", "pi4qpsk"), tag
            plan._dsss_overlay = {"users": [{"user": e["name"], "message": spec["signals"][0]["message"]}]}
            rows, _ = decode_users(iq, plan, rng)
            assert rows[0]["ok"] == REPS, f"{tag}: {rows[0]['ok']}/{REPS} decoded, bit_err {rows[0]['bit_err']}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _overlay_case(name, min_ok_users=None, reps=REPS, window_s=None):
    spec = next(s for s in g.dsss_preset_specs() if s["name"] == name)
    rng = np.random.default_rng(abs(hash(name)) % (1 << 31))
    tmp = tempfile.mkdtemp(prefix="dsss_mu_")
    try:
        iq, plan, raw = build(spec, tmp)
        rep = raw["dsss_overlay"]
        n_users = len(spec["users"])
        assert len(plan.entries) == n_users and len(rep["users"]) == n_users
        assert all(e.params.get("code_spec") for e in plan.entries), "every user needs an explicit code"
        assert len({e.offset_hz for e in plan.entries}) == 1, "overlay: one centre frequency"
        assert len({json.dumps(e.params["code_spec"], sort_keys=True) for e in plan.entries}) == n_users, \
            "every user needs a distinct code"
        rows, fs_out = decode_users(iq, plan, rng, reps=reps, window_s=window_s)
        return spec, rep, rows, fs_out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _summ(rows, reps):
    ok_users = sum(1 for r in rows if r["ok"] == reps)
    err = sum(min(r["bit_err"]) for r in rows)
    sinr = [np.median([v for v in r["sinr_db"] if v is not None]) for r in rows
            if any(v is not None for v in r["sinr_db"])]
    return ok_users, err, (float(np.median(sinr)) if sinr else None)


def test_overlay_4user_bpsk():
    spec, rep, rows, _ = _overlay_case("dsss_4user_bpsk")
    ok_users, err, sinr = _summ(rows, REPS)
    assert ok_users == 4, [(r["user"], r["ok"], min(r["bit_err"])) for r in rows]
    assert rep["expected_mui_snr_db"] > 6.0
    assert sinr is None or sinr > 4.0, f"despread SINR {sinr} dB"


def test_overlay_mixed_carriers():
    spec, rep, rows, _ = _overlay_case("dsss_mixed_carriers")
    ok_users, err, sinr = _summ(rows, REPS)
    assert {r["carrier"] for r in rows} == {"bpsk", "qpsk", "oqpsk", "pi4qpsk"}
    assert ok_users == 4, [(r["user"], r["carrier"], r["ok"], min(r["bit_err"])) for r in rows]


def test_overlay_16user():
    # Decoded per live-size window (2 x 131072 samples at 1.92 MSps)
    # with the overlay cancellation; the hardware bar: >= 13/16 per window and
    # every user over the run. Before the cancellation (plain despread) U07
    # acquired the wrong chip timing on every window and U13 missed some.
    spec, rep, rows, _ = _overlay_case("dsss_16user", reps=1, window_s=HW_WINDOW_S)
    nwin = len(rows[0]["win_ok"][0])
    per_win = [sum(r["win_ok"][0][w] for r in rows) for w in range(nwin)]
    assert nwin >= 2 and min(per_win) >= 13, (per_win, [(r["user"], r["win_ok"][0]) for r in rows])
    assert all(any(r["win_ok"][0]) for r in rows), [(r["user"], r["win_ok"][0]) for r in rows]
    assert rep["set"]["n"] == 16 and rep["set"]["length"] == 127
    assert rep["set"]["worst_pair"][2] <= 17                  # Gold bound t(7)


def test_overlay_codes_reach_the_despreader():
    """Plan_to_watchlist gives every overlay user the other users'
    codes; the despreader keeps the BPSK single-code ones only."""
    tmp = tempfile.mkdtemp(prefix="dsss_mu_")
    try:
        for name, n_bpsk_sib in (("dsss_16user", 15), ("dsss_4user_bpsk", 3), ("dsss_mixed_carriers", 0)):
            spec = next(s for s in g.dsss_preset_specs() if s["name"] == name)
            g.build_scenario(spec, tmp)
            wl = plan_to_watchlist(load_plan(os.path.join(tmp, f"{name}.plan.json")))
            cc = wl[0][0]
            assert len(cc.demod_params["overlay_codes"]) == len(wl) - 1, name
            kw = {k: cc.demod_params[k] for k in ("spread_factor", "code_spec", "dsss_modem", "overlay_codes")}
            d = get_demodulator("DSSS", 320000.0, cc.symbol_rate, 320000.0 / cc.symbol_rate, **kw)
            assert len(d.mui_pns) == n_bpsk_sib, (name, len(d.mui_pns))
        # a lone DSSS carrier gets no overlay codes
        g.build_scenario(carrier_spec("DSSS", {"carrier": "bpsk", "lfsr": {"poly": [5, 2]}}, 0), tmp)
        wl = plan_to_watchlist(load_plan(os.path.join(tmp, "dsss_car_0.plan.json")))
        assert "overlay_codes" not in wl[0][0].demod_params
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_json_schema_users_expand_and_plan_forwards():
    spec = {"name": "s", "sample_rate_hz": FS,
            "code_set": {"kind": "gold", "n": 5, "count": 3},
            "code_sets": {"alt": {"kind": "mseq_phases", "poly": [5, 2], "count": 2}},
            "dsss": {"symbol_rate": 2400, "offset_hz": OFF, "stagger_chips": 3},
            "users": [{"id": "A", "code": 0, "carrier": "qpsk", "message": "a"},
                      {"id": "B", "code": 1, "code_q": 2, "carrier": "oqpsk", "message": "b"},
                      {"id": "C", "set": "alt", "code": 1, "message": "c", "offset_hz": OFF + 200e3},
                      {"id": "D", "lfsr": {"poly": [6, 1], "state": 5}, "message": "d",
                       "chip_rate_hz": 63 * 1200}],
            "signals": [{"modulation": "FSK", "symbol_rate": 1200, "tone_spacing_hz": 2000,
                         "offset_hz": -200e3, "message": "fsk"}]}
    ex = g.expand_users(spec)
    assert "users" not in ex and len(ex["signals"]) == 5
    a, b, c, d = ex["signals"][1:]
    assert a["code_spec"]["family"] == "gold" and a["code_spec"]["offset"] == 0 and a["dsss_modem"] == "qpsk"
    assert b["code_spec_q"]["offset"] == 2 and b["dsss_modem"] == "oqpsk"
    assert c["code_spec"]["family"] == "lfsr" and c["offset_hz"] == OFF + 200e3
    assert d["code_spec"] == {"family": "lfsr", "poly": [6, 1], "state": 5} and d["chip_rate_hz"] == 63 * 1200
    assert a["delay_s"] == 0.0 and abs(b["delay_s"] - 3 / (2400 * 31)) < 1e-12
    v = g.validate_spec(spec, FS)
    assert v["ok"], v["errors"]
    tmp = tempfile.mkdtemp(prefix="dsss_mu_")
    try:
        g.build_scenario(spec, tmp)
        raw = json.load(open(os.path.join(tmp, "s.plan.json")))
        names = [e["name"] for e in raw["entries"]]
        assert names == ["FSK@-200k", "A", "B", "C", "D"]
        eb = next(e for e in raw["entries"] if e["name"] == "B")
        assert eb["params"]["code_spec_q"] and eb["meta"]["user"] == "B" and eb["meta"]["carrier"] == "oqpsk"
        assert "code_q" in eb["meta"]
        ed = next(e for e in raw["entries"] if e["name"] == "D")
        assert ed["params"]["symbol_rate"] == 1200 and ed["params"]["spread_factor"] == 63
        assert raw["dsss_overlay"]["users"][1]["user"] == "B"
        assert raw["dsss_overlay"]["same_length"] is False      # 31- and 63-chip users mixed
        plan = load_plan(os.path.join(tmp, "s.plan.json"))
        wl = plan_to_watchlist(plan)
        cb = next(cc for cc, e in wl if e.name == "B")
        for k in ("code_spec", "code_spec_q", "dsss_modem", "spread_factor"):
            assert k in cb.demod_params, k
        assert cb.demod_params["dsss_modem"] == "oqpsk"
        # plain DSSS signal without any code keys: explicit seed spec in the plan
        g.build_scenario({"name": "p", "sample_rate_hz": FS, "signals": [
            {"modulation": "DSSS", "symbol_rate": 2400, "offset_hz": OFF, "message": "x"}]}, tmp)
        rp = json.load(open(os.path.join(tmp, "p.plan.json")))
        assert rp["entries"][0]["params"]["code_spec"] == {"family": "seed", "length": 15, "seed": 1}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    # refusals
    for bad in ({"users": [{"id": "x", "code": 0}]},                         # no set
                {"code_set": {"kind": "gold", "n": 5, "count": 2}, "users": [{"id": "x", "code": 5}]},
                {"code_set": {"kind": "gold", "n": 5, "count": 2},
                 "users": [{"id": "x", "code": 0, "carrier": "msk"}]}):
        sp = dict({"name": "bad", "sample_rate_hz": FS, "dsss": {"symbol_rate": 2400}}, **bad)
        v = g.validate_spec(sp, FS)
        try:
            ok = v["ok"] and bool(g.build_scenario(sp, tempfile.mkdtemp(prefix="dsss_mu_")))
        except Exception:
            ok = False
        assert not ok, f"accepted {bad}"


def test_dsss_overlay_siblings_in_channelizer_set_plan():
    """channelizer_worker.set_plan keeps every user of an overlay as its own
    channel (same slot, own code) and clear_plan drops them."""
    from dpmg.dsp import channelizer_worker as cw
    spec = next(s for s in g.dsss_preset_specs() if s["name"] == "dsss_4user_bpsk")
    tmp = tempfile.mkdtemp(prefix="dsss_mu_")
    try:
        g.build_scenario(spec, tmp)
        plan = load_plan(os.path.join(tmp, "dsss_4user_bpsk.plan.json"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    w = cw.ChannelizerWorker.__new__(cw.ChannelizerWorker)
    import threading
    w._lock = threading.RLock(); w._slot_bw = 20e3; w._client = None; w._watchlist = []
    w._gated_rt = {}; w._channels = []; w._emit_counts = {}; w._hold = {}
    w.last_error = ""; w._build_demod_fn = None
    cw.ChannelizerWorker.set_plan(w, plan)
    assert len(w._channels) == 4 and len(w._gated_rt) == 1
    assert len(w._overlay_siblings) == 3
    names = [c.ch_name for c in w._channels]
    assert names == ["U01", "U02", "U03", "U04"]
    codes = {json.dumps(c.demod_params["code_spec"], sort_keys=True) for c in w._channels}
    assert len(codes) == 4
    cw.ChannelizerWorker.clear_plan(w)
    assert w._overlay_siblings == [] and w._gated_rt == {}


def main() -> int:
    rng = np.random.default_rng(0xDC55)
    tmp = tempfile.mkdtemp(prefix="dsss_mu_")
    bad = 0
    try:
        print(f"DSSS carriers through the plan path ({FS/1e6:g} MSps, +{OFF/1e3:.0f} kHz, "
              f"{SNR_DB:.0f} dB, CFO +/-{CFO_HZ:.0f} Hz + {DRIFT_HZ_S:g} Hz/s, {REPS} reps):")
        for k, (mod, extra, tag) in enumerate(CARRIER_CASES):
            spec = carrier_spec(mod, extra, k)
            iq, plan, raw = build(spec, tmp)
            e = raw["entries"][0]
            plan._dsss_overlay = {"users": [{"user": e["name"], "message": spec["signals"][0]["message"]}]}
            rows, fs_out = decode_users(iq, plan, rng)
            r = rows[0]
            ok = r["ok"] == REPS
            bad += not ok
            print(f"  [{'PASS' if ok else 'FAIL'}] {tag:36s} {r['ok']}/{REPS}  bit_err {r['bit_err']}  "
                  f"sf={e['params']['spread_factor']} fs_out={fs_out:.0f}")
        for name, reps in (("dsss_4user_bpsk", REPS), ("dsss_mixed_carriers", REPS), ("dsss_16user", 1)):
            spec, rep, rows, fs_out = _overlay_case(name, reps=reps,
                                                    window_s=HW_WINDOW_S if name == "dsss_16user" else None)
            ok_users, err, sinr = _summ(rows, reps)
            s = rep["set"] or {}
            print(f"\n{name}: {len(rows)} users, L={s.get('length')}, set peak xcorr "
                  f"{(s.get('worst_pair') or [0, 0, 0])[2]} ({s.get('worst_db', 0):.1f} dB), predicted "
                  f"MUI SNR >= {rep['expected_mui_snr_db']:.1f} dB, measured despread SINR median "
                  f"{sinr if sinr is None else round(sinr, 1)} dB, fs_out {fs_out:.0f}")
            for r in rows:
                sin = [v for v in r["sinr_db"] if v is not None]
                smu = [v for v in r.get("sinr_mui_db", []) if v is not None]    # DSSS_MUI
                mtxt = f" -> {np.median(smu):.1f} dB cancelled" if smu else ""
                print(f"  {r['user']:5s} {r['carrier']:8s} {r['ok']}/{reps}  bit_err {r['bit_err']}  "
                      f"sinr {np.median(sin):.1f} dB{mtxt}  {r['code'][:48]}" if sin else
                      f"  {r['user']:5s} {r['carrier']:8s} {r['ok']}/{reps}  bit_err {r['bit_err']}")
            if name == "dsss_16user":                        # Per live window
                nwin = len(rows[0]["win_ok"][0])
                per_win = [sum(r["win_ok"][0][w] for r in rows) for w in range(nwin)]
                okc = min(per_win) >= 13 and all(any(r["win_ok"][0]) for r in rows)
                bad += not okc
                print(f"  [{'PASS' if okc else 'FAIL'}] per-window {per_win}/{len(rows)} "
                      f"(need >= 13 every window, every user over the run)")
                continue
            need = len(rows)
            okc = ok_users >= need
            bad += not okc
            print(f"  [{'PASS' if okc else 'FAIL'}] {ok_users}/{len(rows)} users decoded every rep "
                  f"(need >= {need}), total bit errors {err}")
        for t in (test_json_schema_users_expand_and_plan_forwards, test_dsss_overlay_siblings_in_channelizer_set_plan,
                  test_overlay_codes_reach_the_despreader):
            try:
                t(); print(f"  [PASS] {t.__name__}")
            except Exception as e:                               # noqa: BLE001
                bad += 1; print(f"  [FAIL] {t.__name__}: {e!r}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
