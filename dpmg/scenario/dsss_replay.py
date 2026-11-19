"""dsss_replay.py: offline DSSS replay: build a scenario, add a G40-like channel
(arrival offset, phase, CFO + drift, AWGN), channelize like the GPU slice and decode
every user through the real decode path. Used by the DSSS builder panel's
"offline replay" and by tests/test_dsss_multiuser.py.
"""
from __future__ import annotations

import json
import math
import os

import numpy as np

from dpmg.framing import frame_codec as fc
from dpmg.scenario import gen_scenario_json as g
from dpmg.dsp.decode_pool import DecodeSpec, _default_decode
from dpmg.demodulation.demodulator import get_demodulator
from dpmg.scenario.plan_adapter import load_plan, plan_to_watchlist

FS = 1_920_000.0
SNR_DB = 25.0
CFO_HZ = 250.0              # G40-like: a few hundred Hz between undisciplined ends
DRIFT_HZ_S = 5.0            # slow LO drift over the loop
REPS = 2

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


def impair(mix, rng, cfo_hz=CFO_HZ, snr_db=SNR_DB):
    """Random arrival offset, carrier phase, a fixed CFO of random sign plus a
    slow drift, AWGN."""
    n = len(mix)
    x = np.roll(mix, int(rng.integers(0, n))) * np.exp(1j * rng.uniform(0, 2 * np.pi))
    t = np.arange(n) / FS
    f = rng.choice([-1.0, 1.0]) * cfo_hz + DRIFT_HZ_S * t
    x = x * np.exp(2j * np.pi * np.cumsum(f) / FS)
    p = float(np.mean(np.abs(x) ** 2))
    noise = rng.standard_normal(n) + 1j * rng.standard_normal(n)
    noise = noise * np.sqrt(p / 10 ** (snr_db / 10) / 2)
    return (x + noise).astype(np.complex64)


def _raw_ber(bits: str, msg: str):
    """Bit errors of the framed TX bits inside the recovered bit string: find
    the best alignment of the sync word (plain or inverted), compare the frame.
    Returns (errors, frame_bits) or (None, n) when the sync is not found."""
    ref = fc.encode_message(msg.encode())
    sync = fc.SYNC_BITS
    n = len(ref)
    best = None
    for inv in (False, True):
        b = bits if not inv else "".join("1" if c == "0" else "0" for c in bits)
        i = b.find(sync)
        while i >= 0:
            seg = b[i:i + n]
            if len(seg) == n:
                e = sum(1 for x, y in zip(seg, ref) if x != y)
                if best is None or e < best:
                    best = e
            i = b.find(sync, i + 1)
    return best, n


# The live channelizer decodes a 2-frame window: 2 x 131072 input
# samples (channelizer_worker frame_len) at 1.92 MSps = 136.5 ms
HW_WINDOW_S = 2 * 131072 / FS


def decode_users(iq, plan, rng, reps=REPS, cfo_hz=CFO_HZ, snr_db=SNR_DB, window_s=None, mui=True):
    """Channelize once per rep (all users share the band), decode every user
    from the same baseband with its own DecodeSpec. Returns rows per user.
    window_s cuts each rep's baseband into consecutive windows of
    that length (HW_WINDOW_S = the live 2-frame window) decoded separately:
    rows[i]["win_ok"] then lists the per-window hits and "ok" counts reps whose
    every window decoded; mui=False drops the overlay codes (plain despread,
    no multi-user cancellation) for a before/after comparison."""
    wl = plan_to_watchlist(plan)
    fs_out = worker_fs_out(wl, FS)
    rows = []
    for cc, e in wl:
        rows.append({"user": e.name, "carrier": (e.params or {}).get("dsss_modem"),
                     "code": (e.meta or {}).get("code", ""), "ok": 0, "bit_err": [],
                     "sinr_db": [], "sinr_mui_db": [], "win_ok": [], "msg": ""})
    for _ in range(reps):
        x = impair(iq, rng, cfo_hz, snr_db)
        bbs = {}
        for i, (cc, e) in enumerate(wl):
            off = float(cc.f_center_hz - plan.center_freq_hz)
            if off not in bbs:
                bbs[off] = channelize(x, off, FS, fs_out)
            bb_all = bbs[off]
            params = {"symbol_rate": cc.symbol_rate, **(cc.demod_params or {})}
            if not mui:
                params.pop("overlay_codes", None)
            if window_s:
                nw = max(1, int(round(window_s * fs_out)))
                wins = [bb_all[k:k + nw] for k in range(0, len(bb_all) - nw + 1, nw)] or [bb_all]
            else:
                wins = [bb_all]
            want = _expected_message(plan, e)
            hits, errs_w = [], []
            for bb in wins:
                r = _default_decode(0, bb, DecodeSpec(modulation=cc.demod_type, fs_out=fs_out,
                                                       sps=fs_out / cc.symbol_rate,
                                                       demod_params=params, lock={}))
                hit = bool(r.valid and want in (r.messages or [r.message]))
                hits.append(hit)
                rows[i]["msg"] = r.message if r.valid else rows[i]["msg"]
                errs, n = _raw_ber(r.bit_string or "", want)
                errs_w.append(errs if errs is not None else n)
            rows[i]["ok"] += all(hits)
            rows[i]["win_ok"].append(hits)
            rows[i]["bit_err"].append(max(errs_w))
            rows[i]["sinr_db"].append(_despread_sinr(bb_all, fs_out, cc, params, mui=False))
            rows[i]["sinr_mui_db"].append(_despread_sinr(bb_all, fs_out, cc, params, mui=True)
                                          if params.get("overlay_codes") else None)
    return rows, fs_out


def _expected_message(plan, e):
    rep = getattr(plan, "_dsss_overlay", None) or {}
    for u in rep.get("users", []):
        if u.get("user") == e.name:
            return u.get("message", "")
    return e.name


def _despread_sinr(bb, fs_out, cc, params, mui=False):
    """Despread SINR seen by this user's code: the demod's own symbol
    correlations (other users + noise are the interference). With
    mui=True, after the overlay cancellation the decode path applies."""
    try:
        sr = float(cc.symbol_rate)
        kw = {k: params[k] for k in ("spread_factor", "pn_seed", "code_spec", "code_spec_q", "dsss_modem")
              if params.get(k) is not None}
        if mui and params.get("overlay_codes"):
            kw["overlay_codes"] = params["overlay_codes"]
        d = get_demodulator("DSSS", fs_out, sr, fs_out / sr, **kw)
        nom = float(d.sf * d.spc) * sr
        from dpmg.dsp.decode_pool import _resample_to
        x = _resample_to(np.asarray(bb), fs_out, nom) if abs(nom - fs_out) > 1e-9 else np.asarray(bb)
        x = np.asarray(x, dtype=np.complex128)
        if mui and getattr(d, "mui_pns", None):
            x = d._mui_cancel(x)
        off = d._timing(x)
        c = np.abs(d._despread(x, off))
        c = c[c > 0.3 * np.max(c)]                     # the burst, not the dead air
        if len(c) < 8:
            return None
        return float(10 * np.log10(np.mean(c) ** 2 / (np.var(c) + 1e-12)))
    except Exception:
        return None


def build(spec, out_dir):
    g.build_scenario(spec, out_dir)
    pj = os.path.join(out_dir, f"{spec['name']}.plan.json")
    plan = load_plan(pj)
    raw = json.load(open(pj))
    # expected message per user: the plan's dsss_overlay block (multi-user), else
    # straight from the spec's (expanded) signals: single-signal scenarios too
    ex = g.expand_users(spec)
    plan._dsss_overlay = raw.get("dsss_overlay") or {
        "users": [{"user": str(sg.get("user") or sg.get("name") or ""), "message": str(sg.get("message", ""))}
                  for sg in ex.get("signals", []) if "DSSS" in g._canon_mod(sg.get("modulation", ""))]}
    for e in plan.entries:
        e.meta = next((x.get("meta", {}) for x in raw["entries"] if x["name"] == e.name), {})
    iq = np.fromfile(os.path.join(out_dir, f"{spec['name']}.iq.bin"), dtype=np.complex64)
    return iq, plan, raw
