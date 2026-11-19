"""
test_dsss_scenario_roundtrip.py: scenario-built DSSS / QPSK-DSSS through the
channelizer plan path, offline.

Covers the MOD-DSSS / MOD-QPSK-DSSS 0/5 failure: a DSSS burst built with the
FSK-derived samples_per_bit (9 at 2400 Bd), which is not a multiple of the
15-chip code, makes the modulator emit 15 samples/bit at a declared 9-samples/bit
rate. The symbols then go on air at 1440 Bd (21.6 kchip/s) against a plan that
says 2400 Bd (36 kchip/s), and a plan without spread params cannot despread them.

Each case: gen_scenario_json.build_scenario to <name>.iq.bin + .plan.json to
plan_adapter.plan_to_watchlist (exactly what channelizer_worker.set_plan
consumes): the worker's gate fs_out (symbol_rate*10 floor, raised by
SPREAD_FSOUT to 2 chips/sample) with its integer decimation: channelize the
file to that rate: decode_pool._default_decode with the DecodeSpec the worker
builds. Random arrival offset, carrier phase and 25 dB AWGN per rep. pytest
collects the test_* functions; `python3 tests/test_dsss_scenario_roundtrip.py`
prints the table (exit 0 = all decoded).
"""
from __future__ import annotations

import json
import math
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import numpy as np

from dpmg.scenario import gen_scenario_json as g
from dpmg.dsp.decode_pool import DecodeSpec, _default_decode
from dpmg.modem.modem import Modem, ModemConfig
from dpmg.scenario.plan_adapter import load_plan, plan_to_watchlist

FS = 1_920_000.0            # the bench rate (Setup A)
OFF = 300_000.0             # the bench case offset
SR = 2400                   # the bench DSSS symbol rate
SNR_DB = 25.0
REPS = 3

# (scenario modulation, extra signal keys, tag)
CASES = [
    ("DSSS", {}, "seed SF15 (modem default)"),
    ("DSSS", {"code_spec": {"family": "gold", "n": 5}}, "gold n=5 (31 chips)"),
    ("DSSS", {"code_spec": {"family": "m", "n": 5}, "samples_per_chip": 3}, "m n=5, 3 samp/chip"),
    ("DSSS", {"dsss_modem": "qpsk"}, "DSSS + dsss_modem qpsk"),
    ("QPSK-DSSS", {}, "QPSK-DSSS SF15"),
    ("QPSK-DSSS", {"code_spec": {"family": "gold", "n": 5, "index": 3}}, "QPSK-DSSS gold idx 3"),
]


def channelize(iq, center_off_hz, fs_in, fs_out):
    """Mix the band to baseband, lowpass, resample (what the GPU slice does)."""
    x = np.asarray(iq, dtype=np.complex64)
    n = np.arange(len(x))
    x = x * np.exp(-2j * np.pi * center_off_hz / fs_in * n)
    ntaps = 129
    fc = 0.5 * fs_out / fs_in
    t = np.arange(ntaps) - (ntaps - 1) / 2
    h = (2 * fc * np.sinc(2 * fc * t) * np.hamming(ntaps)).astype(np.float32)
    x = np.convolve(x, h, mode="same")
    m = int(round(len(x) * fs_out / fs_in))
    src = np.arange(m) * (fs_in / fs_out)
    left = np.minimum(np.floor(src).astype(np.int64), len(x) - 2)
    frac = (src - left).astype(np.float32)
    return (x[left] * (1 - frac) + x[left + 1] * frac).astype(np.complex64)


def worker_fs_out(watchlist, fs_in):
    """channelizer_worker.set_plan's gate rate: lcm of the decode symbol rates
    x the smallest multiple giving >= 10 sps, raised by SPREAD_FSOUT for DSSS
    (2 chips/sample) and CSS (chirp bw); then the client's integer decimation."""
    srates = [int(round(float(cc.symbol_rate))) for cc, _e in watchlist
              if getattr(cc, "decode", False) and cc.symbol_rate]
    lcm = srates[0]
    for s in srates[1:]:
        lcm = lcm * s // math.gcd(lcm, s)
    mult = 1
    while (lcm * mult) / max(srates) < 10:
        mult += 1
    fs_req = float(lcm * mult)
    for cc, _e in watchlist:
        m = str(cc.demod_type).upper()
        p = cc.demod_params or {}
        sr = float(cc.symbol_rate or 0)
        if "DSSS" in m and sr > 0:
            fs_req = max(fs_req, 2.0 * float(p.get("spread_factor", 15) or 15) * sr)
        elif "CSS" in m or "LORA" in m:
            fs_req = max(fs_req, float(p.get("css_bw", 125e3)) * float(p.get("css_os", 1) or 1))
    decim = max(1, int(round(fs_in / fs_req)))
    return fs_in / decim


def impair(mix, rng):
    """Random arrival offset (the file loops on air), carrier phase, AWGN."""
    x = np.roll(mix, int(rng.integers(0, len(mix)))) * np.exp(1j * rng.uniform(0, 2 * np.pi))
    p = float(np.mean(np.abs(x) ** 2))
    noise = (rng.standard_normal(len(x)) + 1j * rng.standard_normal(len(x)))
    noise = noise * np.sqrt(p / 10 ** (SNR_DB / 10) / 2)
    return (x + noise).astype(np.complex64)


def run_case(mod, extra, rng, out_dir):
    """Build the scenario, decode REPS impaired copies through the plan path.
    Returns (n_ok, entry, modem_fs_ok)."""
    msg = f"{mod} none R1"
    sig = {"modulation": mod, "symbol_rate": SR, "tone_spacing_hz": 5000,
           "offset_hz": OFF, "message": msg, "amplitude": 0.9}
    sig.update(extra)
    name = f"dsss_rt_{abs(hash((mod, json.dumps(extra, sort_keys=True)))) % 10**8}"
    spec = {"name": name, "sample_rate_hz": FS, "center_freq_hz": 425_025_000.0,
            "loop_s": 0.2, "signals": [sig]}
    g.build_scenario(spec, out_dir)
    plan = load_plan(os.path.join(out_dir, f"{name}.plan.json"))
    entry = plan.entries[0]
    iq = np.fromfile(os.path.join(out_dir, f"{name}.iq.bin"), dtype=np.complex64)

    # the modem's declared rate must match the samples/bit the plan records,
    # i.e. the symbols really are on air at symbol_rate
    sps = int(entry.params.get("samples_per_bit") or 0) or int(
        json.load(open(os.path.join(out_dir, f"{name}.plan.json")))["entries"][0]["meta"]["samples_per_bit"])
    mkw = dict(modulation=g._canon_mod(mod), symbol_rate=float(SR), samples_per_bit=sps,
               **g._spread_kwargs(sig, g._canon_mod(mod)))
    modem_fs_ok = Modem(ModemConfig(**mkw)).sample_rate == SR * sps

    wl = plan_to_watchlist(plan)
    cc, _e = wl[0]
    fs_out = worker_fs_out(wl, FS)
    params = {"symbol_rate": cc.symbol_rate, **(cc.demod_params or {})}   # _rt_from_config
    n_ok = 0
    for _ in range(REPS):
        bb = channelize(impair(iq, rng), float(cc.f_center_hz - plan.center_freq_hz), FS, fs_out)
        r = _default_decode(0, bb, DecodeSpec(modulation=cc.demod_type, fs_out=fs_out,
                                               sps=fs_out / cc.symbol_rate,
                                               demod_params=params, lock={}))
        n_ok += bool(r.valid and r.message == msg)
    return n_ok, entry, modem_fs_ok, fs_out


# pytest entry points

def test_plan_entry_carries_spread_params(tmp_path):
    """The plan entry names the code (spread_factor = true code length, family,
    seed, modem) and a chip-wide band, and plan_adapter forwards all of it."""
    sig = {"modulation": "DSSS", "symbol_rate": SR, "tone_spacing_hz": 5000,
           "offset_hz": OFF, "message": "x", "code_spec": {"family": "gold", "n": 5},
           "pn_seed": 7, "dsss_modem": "qpsk"}
    spec = {"name": "p", "sample_rate_hz": FS, "signals": [sig]}
    g.build_scenario(spec, str(tmp_path))
    plan = load_plan(str(tmp_path / "p.plan.json"))
    e = plan.entries[0]
    assert e.params["spread_factor"] == 31            # gold n=5: 2^5-1 chips
    assert e.params["code_spec"] == {"family": "gold", "n": 5}
    assert e.params["pn_seed"] == 7 and e.params["dsss_modem"] == "qpsk"
    assert e.band_hz == 2.0 * 31 * SR
    cc, _ = plan_to_watchlist(plan)[0]
    for k in ("spread_factor", "code_spec", "pn_seed", "dsss_modem"):
        assert k in cc.demod_params, k
    assert cc.bw_hz == e.band_hz
    # default: 2 samples/chip, so samples_per_bit = 62 and the modem rate matches
    meta = json.load(open(tmp_path / "p.plan.json"))["entries"][0]["meta"]
    assert meta["samples_per_bit"] == 62


def test_dsss_modulator_declares_true_rate():
    """A samples_per_bit that is not a multiple of the code length must not
    mis-declare the sample rate (the 1440-Bd-on-air bug)."""
    from dpmg.modulation.spread_spectrum import DSSSModulator
    m = DSSSModulator(2400.0, 9, spread_factor=15)
    n = len(m.modulate("0" * 10).iq_samples)
    assert n == 10 * m.sf * m.spc
    assert m.sample_rate == 2400 * m.sf * m.spc


def test_scenario_dsss_decodes_through_plan_path():
    rng = np.random.default_rng(0xD555)
    tmp = tempfile.mkdtemp(prefix="dsss_rt_")
    try:
        for mod, extra, tag in CASES:
            n_ok, entry, fs_ok, _ = run_case(mod, extra, rng, tmp)
            assert fs_ok, f"{tag}: modem sample rate != symbol_rate * samples_per_bit"
            assert n_ok == REPS, f"{tag}: {n_ok}/{REPS} decoded"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    rng = np.random.default_rng(0xD555)
    tmp = tempfile.mkdtemp(prefix="dsss_rt_")
    oks = []
    print(f"scenario DSSS through the plan path ({FS/1e6:g} MSps, +{OFF/1e3:.0f} kHz, "
          f"{SR} Bd, {SNR_DB:.0f} dB, {REPS} reps):")
    try:
        for mod, extra, tag in CASES:
            n_ok, entry, fs_ok, fs_out = run_case(mod, extra, rng, tmp)
            ok = fs_ok and n_ok == REPS
            oks.append(ok)
            print(f"  [{'PASS' if ok else 'FAIL'}] {tag:26s} {n_ok}/{REPS}  "
                  f"sf={entry.params.get('spread_factor')} band={entry.band_hz/1e3:.1f}k "
                  f"fs_out={fs_out:.0f}{'' if fs_ok else '  MODEM RATE MISMATCH'}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{sum(oks)}/{len(oks)} cases decoded")
    return 0 if oks and all(oks) else 1


if __name__ == "__main__":
    sys.exit(main())
