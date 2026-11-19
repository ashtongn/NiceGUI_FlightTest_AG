"""
Offline replay of the over-RF decode chain: load each sweep burst file (what
the initiator radiates), channelize it down to the 250k channel baseband the
GPU would produce (mix + lowpass + resample), and run decode_pool's
_default_decode with the exact DecodeSpec the worker would build from the plan.

Reproduces the bench sweep failures on the desk so fixes iterate in seconds.
Optional --rf adds a random arrival offset, phase rotation, CFO, and noise to
approximate real RF arrival. Exit 0 when every case decodes.
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bench"))   # bench_modsweep moved to bench/
import numpy as np

from dpmg.dsp.decode_pool import DecodeSpec, _default_decode
from bench_modsweep import CASES, build_case, FS_CARD, SHIFT

FS_OUT = 250_000.0
PROD_SPS = 10          # the channelizer's real samples/symbol floor
rng = np.random.default_rng(0xBEEF)


def channelize(iq, center_off_hz, fs_in=FS_CARD, fs_out=FS_OUT):
    """Mix the channel to baseband, lowpass, resample to fs_out (linear)."""
    x = np.asarray(iq, dtype=np.complex64)
    n = np.arange(len(x))
    x = x * np.exp(-2j * np.pi * center_off_hz / fs_in * n)
    # windowed-sinc lowpass at ~fs_out/2
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


def rf_impair(bb, snr_db=30.0):
    """Random arrival offset + phase + tiny CFO + AWGN."""
    off = int(rng.integers(0, 400))
    bb = np.concatenate([np.zeros(off, dtype=np.complex64), bb])
    ph = np.exp(1j * rng.uniform(0, 2 * np.pi))
    cfo = rng.uniform(-5.0, 5.0)                     # Hz (shared-ref loopback)
    n = np.arange(len(bb))
    bb = bb * ph * np.exp(2j * np.pi * cfo / FS_OUT * n)
    p = np.mean(np.abs(bb[bb != 0]) ** 2) if np.any(bb != 0) else 1.0
    npow = p / (10 ** (snr_db / 10.0))
    bb = bb + ((rng.standard_normal(len(bb)) + 1j * rng.standard_normal(len(bb)))
               * np.sqrt(npow / 2)).astype(np.complex64)
    return bb.astype(np.complex64)


def run_case(name, mkw, params, offset, rf=False):
    fn, pf, _ = build_case(name, mkw, params, offset)
    iq = np.fromfile(fn, dtype=np.complex64)
    bb = channelize(iq, offset)
    if rf:
        bb = rf_impair(bb)
    prod = "--sps10" in sys.argv
    fs_out = params["symbol_rate"] * PROD_SPS if prod else FS_OUT
    if prod:
        # mirror the worker's SPREAD_FSOUT: the symbol-rate*10 floor starves the
        # spread mods (a 125 kHz chirp cannot live in a 9.8 kHz channel), so the
        # service raises the gate rate: 2 chips/sample for DSSS, bw*os for CSS
        _m = str(params.get("plan_modulation", mkw["modulation"])).upper()
        if "DSSS" in _m:
            fs_out = max(fs_out, 2.0 * float(params.get("spread_factor",
                         mkw.get("spread_factor", 15))) * params["symbol_rate"])
        elif "CSS" in _m or "LORA" in _m:
            fs_out = max(fs_out, float(params.get("css_bw", 125_000.0))
                         * float(params.get("css_os", 1) or 1))
    bb = channelize(iq, offset, fs_out=fs_out) if prod else bb
    demod_params = {"symbol_rate": params["symbol_rate"]}
    if "tone_spacing_hz" in params:
        demod_params["tone_spacing_hz"] = params["tone_spacing_hz"]
    if "dibit_mapping" in params:
        demod_params["dibit_mapping"] = params["dibit_mapping"]
    if "gfsk_bw_hz" in params:
        demod_params["gfsk_bw_hz"] = params["gfsk_bw_hz"]
    for k in ("spread_factor", "pn_seed", "css_sf", "css_bw", "css_os"):
        if k in params:
            demod_params[k] = params[k]
    demod_params["frame"] = {"sync_bits": "1011001000111011",
                             "max_sync_errors": 2, "has_length": True,
                             "crc": "CRC-16-CCITT"}
    plan_mod = params.get("plan_modulation", mkw["modulation"])
    spec = DecodeSpec(modulation=plan_mod, fs_out=fs_out,
                      sps=fs_out / params["symbol_rate"],
                      demod_params=demod_params, lock={})
    r = _default_decode(0, bb, spec)
    ok = bool(r.valid and r.message == "PING 1")
    print(f"  {name:10s} valid={r.valid!s:5} msg={r.message!r:12} "
          f"bits={len(r.bit_string or '')}  {'OK' if ok else '-- FAIL'}"
          f"{('  err=' + r.error) if getattr(r, 'error', None) else ''}")
    return ok


# The preset PING/PONG conversation, all four framed messages in one buffer.
MULTIFRAME_MSGS = ["PING 1", "PING 2", "TEXT HELLO", "BYE"]


def build_multiframe_iq(messages, mkw):
    """Concatenate framed bursts for each message into one card-rate signal at
    +SHIFT, exactly as build_case does for a single burst. Each FSK burst is an
    integer number of bit periods (frame + lead/tail pad), so the bursts stay on
    one symbol grid when concatenated: the demod's fixed stride aligns all
    four."""
    from dpmg.modem.modem import Modem, ModemConfig
    from dpmg.radio.dpmg_service import _resample_iq
    m = Modem(ModemConfig(**mkw))
    burst = np.concatenate([m.transmit(msg) for msg in messages]).astype(np.complex64)
    up = _resample_iq(burst, m.sample_rate, FS_CARD)
    n = np.arange(len(up))
    return (up * np.exp(2j * np.pi * SHIFT / FS_CARD * n)).astype(np.complex64)


def run_multiframe(rf=False):
    """Decode a buffer holding all four preset messages and assert find_all_frames
    surfaces every one (not just the first)."""
    mkw = dict(modulation="FSK", symbol_rate=4800, samples_per_bit=40,
               tone0=1200, tone1=2200)
    params = dict(symbol_rate=4800.0, tone_spacing_hz=1000.0)
    offset = SHIFT + 1700.0
    # Channelize at the production integer-sps rate (symbol_rate * PROD_SPS). The
    # real channelizer picks fs_out so sps is an integer (set_plan._gate_fs_out);
    # a fractional sps (e.g. 250k/4800=52.08) drifts the demod's fixed stride
    # ~0.08 samp/symbol, so the first frame decodes but timing slips by later
    # frames: which would mask find_all_frames behind a demod timing bug.
    fs_out = params["symbol_rate"] * PROD_SPS
    up = build_multiframe_iq(MULTIFRAME_MSGS, mkw)
    bb = channelize(up, offset, fs_out=fs_out)
    if rf:
        bb = rf_impair(bb)
    demod_params = {"symbol_rate": params["symbol_rate"],
                    "tone_spacing_hz": params["tone_spacing_hz"],
                    "frame": {"sync_bits": "1011001000111011",
                              "max_sync_errors": 2, "has_length": True,
                              "crc": "CRC-16-CCITT"}}
    spec = DecodeSpec(modulation="FSK", fs_out=fs_out,
                      sps=fs_out / params["symbol_rate"],
                      demod_params=demod_params, lock={})
    r = _default_decode(0, bb, spec)
    got = list(getattr(r, "messages", None) or ([r.message] if r.message else []))
    ok = got == MULTIFRAME_MSGS
    print(f"  {'multiframe':10s} valid={r.valid!s:5} got={got} "
          f"want={MULTIFRAME_MSGS}  {'OK' if ok else '-- FAIL'}"
          f"{('  err=' + r.error) if getattr(r, 'error', None) else ''}")
    return ok


def main():
    import shutil
    import tempfile
    rf = "--rf" in sys.argv
    only = [a for a in sys.argv[1:] if not a.startswith("-")]
    print(f"offline decode replay ({'RF-impaired' if rf else 'clean'}):")
    oks = []
    # build_case() writes sweep_<MOD>.bin + _plan.json relative to cwd; run the
    # whole replay inside a temp dir so the suite never litters the repo root.
    cwd, tmp = os.getcwd(), tempfile.mkdtemp(prefix="rfdecode_test_")
    os.chdir(tmp)
    try:
        for name, mkw, params, offset in CASES:
            if only and name not in only:
                continue
            try:
                oks.append(run_case(name, mkw, params, offset, rf=rf))
            except Exception as e:
                print(f"  {name:10s} EXC: {e}")
                oks.append(False)
        if not only or "multiframe" in only:
            print("\nmulti-frame buffer (find_all_frames):")
            try:
                oks.append(run_multiframe(rf=rf))
            except Exception as e:
                print(f"  multiframe EXC: {e}")
                oks.append(False)
    finally:
        os.chdir(cwd)
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{sum(oks)}/{len(oks)} decoded")
    return 0 if all(oks) else 1


if __name__ == "__main__":
    sys.exit(main())
