#!/usr/bin/env python3
"""
replay_chain.py: offline twin of the RF decode path, shared by the bench
harnesses that pre-screen hardware runs (mod_fec_matrix --replay,
jam_denial --replay).

The chain is the one the radio runs, minus the radio:

  gen_scenario_json.build_scenario: scenario loop IQ + plan
  plan_adapter.plan_to_watchlist: the ChannelConfig per plan entry
  worker_fs_out: channelizer_worker.set_plan's gate rate,
                                        then the client's integer decimation
  channelize_loop: gpu_channelizer2: one FFT, each channel
                                        = the n_out bins around its centre bin
                                        (a brick wall at +-fs_out/2, no filter)
  decode: decode_pool._default_decode with the
                                        DecodeSpec the worker submits

The scenario file loops on air, so the loop is processed circularly (one FFT
over the whole loop, no edge). AWGN is added at the TX sample rate. The service
detector (energy_detector.EnergyDetector + BurstTracker, one nfft-sample
periodogram per 0.2 s tick) is modelled by detect_ticks.
"""
from __future__ import annotations

import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import numpy as np                                               # noqa: E402

DET_NFFT = 32768                 # dpmg_service DET_DEFAULT_NFFT
DET_THRESHOLD_DB = 10.0          # DET_DEFAULT_THRESHOLD_DB
DET_INTERVAL_S = 0.2             # DET_INTERVAL_S


def worker_fs_out(watchlist, fs_in):
    """channelizer_worker.set_plan's gate rate: lcm of the decode symbol rates
    x the smallest multiple giving >= 10 sps, raised by SPREAD_FSOUT for DSSS
    (2 chips/sample) and CSS (chirp bw); then the client's integer decimation.
    Returns (fs_out_actual, decim)."""
    srates = [int(round(float(cc.symbol_rate))) for cc, _e in watchlist
              if cc is not None and getattr(cc, "decode", False) and cc.symbol_rate]
    srates = [s for s in srates if s > 0]
    if not srates:
        raise ValueError("plan has no decodable channel with a symbol rate")
    lcm = srates[0]
    for s in srates[1:]:
        lcm = lcm * s // math.gcd(lcm, s)
    mult = 1
    while (lcm * mult) / max(srates) < 10:
        mult += 1
    fs_req = float(lcm * mult)
    for cc, _e in watchlist:
        if cc is None or not getattr(cc, "decode", False):
            continue
        m = str(cc.demod_type).upper()
        p = cc.demod_params or {}
        sr = float(cc.symbol_rate or 0)
        if "DSSS" in m and sr > 0:
            fs_req = max(fs_req, 2.0 * float(p.get("spread_factor", 15) or 15) * sr)
        elif "CSS" in m or "LORA" in m:
            fs_req = max(fs_req, float(p.get("css_bw", 125e3)) * float(p.get("css_os", 1) or 1))
    decim = max(1, int(round(fs_in / fs_req)))
    return fs_in / decim, decim


def load_scenario(out_dir, name):
    """(loop iq complex64, ChannelPlan, watchlist) of a built scenario."""
    from dpmg.scenario.plan_adapter import load_plan, plan_to_watchlist
    plan = load_plan(os.path.join(out_dir, f"{name}.plan.json"))
    iq = np.fromfile(os.path.join(out_dir, f"{name}.iq.bin"), dtype=np.complex64)
    return iq, plan, plan_to_watchlist(plan)


def spectrum(iq):
    """One FFT of the (circular) loop, reused for every channel."""
    return np.fft.fft(np.asarray(iq, dtype=np.complex64))


def channelize_loop(X, fs_in, off_hz, decim):
    """gpu_channelizer2.process on a circular loop: the n_out = N // decim bins
    around the channel's centre bin, inverse-FFT'd. X = spectrum(loop).
    Returns (baseband complex64, fs_out_effective)."""
    N = len(X)
    n_out = N // decim
    half = n_out // 2
    cb = int(round(off_hz / (fs_in / N)))
    bins = (np.arange(-half, -half + n_out) + cb) % N
    bins = np.roll(bins, -half)
    bb = np.fft.ifft(X[bins]) * (n_out / N)
    return bb.astype(np.complex64), fs_in * n_out / N


def awgn(iq, n0_per_hz, fs, rng):
    """Complex AWGN of one-sided density n0 (power/Hz) at sample rate fs."""
    n = len(iq)
    s = math.sqrt(n0_per_hz * fs / 2.0)
    return (np.asarray(iq) + (rng.standard_normal(n) + 1j * rng.standard_normal(n)) * s
            ).astype(np.complex64)


def burst_power(iq, frac=0.25):
    """Mean power over the 'on' samples (> frac of the peak power)."""
    a = np.abs(np.asarray(iq)) ** 2
    if not a.size or a.max() <= 0:
        return 0.0
    return float(np.mean(a[a > frac * a.max()]))


def decode_spec(cc, fs_out, lock=None):
    from dpmg.dsp.decode_pool import DecodeSpec
    params = {"symbol_rate": cc.symbol_rate, **(cc.demod_params or {})}
    return DecodeSpec(modulation=cc.demod_type, fs_out=fs_out,
                      sps=max(2.0, fs_out / float(cc.symbol_rate)),
                      demod_params=params, lock={} if lock is None else lock)


def decode(bb, cc, fs_out, lock=None):
    """decode_pool._default_decode exactly as the worker submits it. `lock` is
    the worker's per-channel lock dict (warm QPSK/timing state, miss count);
    pass the same dict across windows of one keying, None = cold. The 3-miss
    cooldown is wall-clock in the pool; a replay window stands for ~1 s of air
    time, so it is cleared here instead of sleeping it out."""
    from dpmg.dsp.decode_pool import _default_decode
    if lock is not None:
        lock.pop("cooldown_until", None)
    return _default_decode(0, np.asarray(bb, dtype=np.complex64), decode_spec(cc, fs_out, lock))


def raw_ber(bit_string, ref_bits, sync_bits, max_sync_errors=2):
    """Pre-FEC channel bit errors of a recovered hard-bit string against the
    known on-air frame bits (sync + body). Every sync hit (either polarity) is
    scored over the body; the best frame wins. Returns (errors, n_bits) or None
    when no sync is found."""
    bs = str(bit_string or "")
    body = ref_bits[len(sync_bits):]
    nb = len(body)
    if not bs or nb == 0 or len(bs) < len(sync_bits) + nb:
        return None
    best = None
    s = np.frombuffer(sync_bits.encode("ascii"), np.uint8)
    ref = np.frombuffer(body.encode("ascii"), np.uint8)
    for inv in (False, True):
        b = bs.translate(str.maketrans("01", "10")) if inv else bs
        a = np.frombuffer(b.encode("ascii"), np.uint8)
        w = np.lib.stride_tricks.sliding_window_view(a[:len(a) - nb], len(s))
        hits = np.nonzero((w != s).sum(axis=1) <= max_sync_errors)[0]
        for h in hits[:64]:
            p = int(h) + len(s)
            e = int(np.count_nonzero(a[p:p + nb] != ref))
            if best is None or e < best:
                best = e
    return None if best is None else (best, nb)


def detect_ticks(iq_loop, fs, center_hz, rng, n_ticks=50, nfft=DET_NFFT,
                 threshold_db=DET_THRESHOLD_DB, n0_per_hz=0.0):
    """Model of the service detection plane: every DET_INTERVAL_S tick the
    detector reads the newest nfft samples of the stream; here each tick reads
    nfft samples at the stream position k*0.2 s (mod the loop, plus a random
    phase), adds receiver noise and runs EnergyDetector.process +
    BurstTracker.update with the service defaults. Returns the closed
    BurstEvents as dicts {freq_hz, n_slots, peak_db, ticks}."""
    from dpmg.dsp import energy_detector as ed
    det = ed.EnergyDetector(fs=fs, center_hz=center_hz, slot_bw=20e3,
                            threshold_db=threshold_db, nfft=nfft)
    tr = ed.BurstTracker(cluster_margin_db=20.0, cluster_gap=1, hold_ticks=1,
                         island_slots=3)
    N = len(iq_loop)
    ext = np.concatenate([iq_loop, iq_loop[:nfft]])
    pos0 = int(rng.integers(0, N))
    step = int(round(DET_INTERVAL_S * fs))
    closed = []
    for k in range(n_ticks):
        p = (pos0 + k * step) % N
        x = ext[p:p + nfft]
        if n0_per_hz > 0:
            x = awgn(x, n0_per_hz, fs, rng)
        dets = det.process(x)
        closed += tr.update(dets, k * DET_INTERVAL_S)
    closed += tr.flush(n_ticks * DET_INTERVAL_S)
    out = []
    for ev in closed:
        out.append({"freq_hz": float(getattr(ev, "freq_hz", None) or ev.center_hz),
                    "n_slots": int(getattr(ev, "n_slots", 1)),
                    "peak_db": float(ev.peak_power_db),
                    "ticks": int(round((ev.t_stop - ev.t_start) / DET_INTERVAL_S))})
    return out
