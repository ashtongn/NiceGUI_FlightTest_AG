#!/usr/bin/env python3
"""
gen_scenario_json.py: JSON-driven FSK / 4FSK (and general) test-scenario builder.

Reads a scenario spec (.json) and writes, under the output dir:
  <name>.iq.bin    TX composite at the spec sample rate (key with the SDR-tab
                   "file on radio" TX, or bench tx-file). Deploy to X40 @ 50 Msps
                   or G40 @ 1.92-30.72: the sample_rate_hz is the TX rate.
  <name>.plan.json channelizer decode plan (push for RX decode).

FSK/4FSK: every signal is parametric: symbol_rate, tone_spacing_hz
(peak-to-peak shift for FSK / adjacent-tone spacing for 4FSK), tone_center_hz
(baseband center, default 0 = symmetric RF FSK), samples_per_bit (auto if omitted),
band offset_hz, message, and frame sync/CRC. Presets in scenarios/json/.

Spec schema
{
  "name": "fsk_4800_5k",
  "sample_rate_hz": 1920000,
  "center_freq_hz": 425025000,
  "loop_s": 0.2,
  "peak_fs": 0.45,                  # optional; peak of the whole mix as a fraction
  #                                 #   of full scale (default 0.45, allowed 0.05
  #                                 #   to 1.0, built at no more than 0.999)
  "signals": [
    {
      "modulation": "FSK",          # or "4FSK"
      "symbol_rate": 4800,
      "tone_spacing_hz": 5000,      # FSK: full shift (tones at +-spacing/2)
      "tone_center_hz": 0,          # optional baseband center (default 0)
      "samples_per_bit": null,      # optional; auto = max(8, ceil(4*spacing/symrate))
      "offset_hz": 100000,          # RF placement within the band, or instead
      # "center_freq_hz": 425125000,  # this signal's absolute RF center (needs
      #                             #   the spec's center_freq_hz; offset derived)
      "message": "PING X40",
      "amplitude": 0.8,             # level relative to the other signals; the
      #                             #   mix is then scaled to peak_fs
      # DSSS / QPSK-DSSS only (all optional; defaults = modem defaults):
      #   "spread_factor": 15,        # chips per symbol (seed family length)
      #   "samples_per_chip": 2,      # chip oversampling (samples_per_bit is
      #                               #   then spread_factor*samples_per_chip)
      #   "pn_seed": 1, "code_spec": {"family": "seed|m|gold|custom|lfsr", ...},
      #   "lfsr": {"poly": [5,2] | "0x12", "state": 1, "form": "fibonacci|galois",
      #            "length": 31, "offset": 0},   # explicit LFSR code, same
      #                               #   as code_spec {"family": "lfsr", ...}
      #   "code_spec_q" / "lfsr_q",   # second code on the Q rail
      #   "dsss_modem" | "carrier": "bpsk|qpsk|oqpsk|pi4qpsk",   # DSSS_CARRIERS
      #   "chip_rate_hz": 74400,      # instead of symbol_rate (= chips/s / code length)
      #   "user": "U01"               # user id: plan entry name / meta
      # (the plan entry always carries an explicit code_spec: a DSSS entry
      #  without one does not decode on the radio)
      # CSS only: "css_sf": 7, "css_bw": 125000, "css_os": 1
      # "decode_gate_db": 3,        # optional; plan param: decode this continuous
      #                             #   channel while its GPU slot is > N dB over
      #                             #   the floor (default: the 12 dB detection set)
      # "bits": "0110..." or [...], # optional; raw on-air bits instead of message:
      #                             #   no sync/len/CRC framing, no FEC, no pad; a
      #                             #   list = successive bursts (replica captures)
      # "burst_period_s": 0.08,     # optional; zero-pad each burst to this period
      #                             #   (TDMA slot); a longer burst runs over
      # "delay_s": 0.01,            # optional; circular time shift of this
      #                             #   signal within its own cycle (staggers
      #                             #   copies to cut the composite's PAPR)
      # {"modulation": "CW", "offset_hz": 5468.75, "amplitude": 0.08,
      #  "duration_s": 1.0}         # unmodulated carrier (pilot/LO line), IQ
      #                             #   only: no plan entry
      "frame": {                    # optional; default sync/CRC below
        "sync_bits": "1011001000111011", "max_sync_errors": 2,
        "has_length": true, "crc": "CRC-16-CCITT"
      }
    }
  ]
}

Multi-user DSSS: a spec may define a code set once
and a list of users that take codes from it; expand_users() turns them into
ordinary DSSS signals (all at one centre frequency by default, or spread by
"spacing_hz"), each with an explicit code spec, user id and carrier:
{
  "code_set": {"kind": "gold", "n": 5, "count": 4, "length": 31},   # or "code_sets": {name: {...}}
  "dsss": {"symbol_rate": 2400, "samples_per_chip": 2, "offset_hz": 300000,
           "carrier": "bpsk", "amplitude": 0.8, "stagger_chips": 5, "spacing_hz": 0},
  "users": [{"id": "U01", "code": 0, "carrier": "qpsk", "message": "U01 HELLO",
             "amplitude": 0.8, "delay_s": 0.0, "offset_hz": 300000,
             "lfsr": {...} | "code_spec": {...}, "code_q": 1, "set": "name"}, ...]
}
The plan JSON gets a "dsss_overlay" block: the set's correlation report and
the predicted multi-user interference (dpmg.modulation.lfsr_codes.set_report).

Usage:  python3 -m dpmg.scenario.gen_scenario_json <spec.json> [--out DIR] [--peak-fs 0.45]
        python3 -m dpmg.scenario.gen_scenario_json --presets            # write the preset library
        python3 -m dpmg.scenario.gen_scenario_json --dsss-presets       # write the DSSS multi-user presets
"""
from __future__ import annotations
import argparse
import json
import math
import os
import sys

import numpy as np
from dpmg.modem.modem import Modem, ModemConfig
from dpmg.radio.dpmg_service import _resample_iq

# Peak of the built mix as a fraction of full scale. 1.0 is accepted; a build
# stops at PEAK_FS_CEILING, the same ceiling tools_engine.generate uses.
PEAK_FS_DEFAULT = 0.45
PEAK_FS_MIN = 0.05
PEAK_FS_MAX = 1.0
PEAK_FS_CEILING = 0.999


def resolve_peak_fs(spec: dict | None, peak_fs=None) -> float:
    """The peak to build at: an explicit peak_fs wins, then the spec's own
    "peak_fs", then PEAK_FS_DEFAULT. Raises ValueError outside the allowed range."""
    v = peak_fs
    if v is None and isinstance(spec, dict):
        v = spec.get("peak_fs")
    if v is None or v == "":
        return PEAK_FS_DEFAULT
    try:
        v = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"peak_fs {v!r} is not a number") from None
    if not (PEAK_FS_MIN <= v <= PEAK_FS_MAX):
        raise ValueError(f"peak_fs {v:g} is outside {PEAK_FS_MIN:g} to {PEAK_FS_MAX:g}")
    return min(v, PEAK_FS_CEILING)

DEFAULT_SYNC = {"sync_bits": "1011001000111011", "max_sync_errors": 2,
                "has_length": True, "crc": "CRC-16-CCITT"}


def _tones(sig: dict) -> dict:
    """FSK/GFSK/4FSK tone frequencies (Hz, baseband) from spacing + center. FSK
    and GFSK (when a spacing is given) sit at center +- spacing/2; 4FSK at
    center +- 0.5/1.5 * spacing."""
    mod = sig["modulation"].upper()
    spacing = float(sig.get("tone_spacing_hz", 1000.0))
    center = float(sig.get("tone_center_hz", 0.0))
    if mod == "FSK":
        return {"tone0": center - spacing / 2.0, "tone1": center + spacing / 2.0}
    # Honour an explicit spacing like FSK (centre +- spacing/2); with no
    # spacing given, GFSK keeps the modem default tones (1200/2200 Hz, low index)
    if mod == "GFSK" and sig.get("tone_spacing_hz") is not None:
        return {"tone0": center - spacing / 2.0, "tone1": center + spacing / 2.0}
    if mod == "4FSK":
        return {"tone0": center - 1.5 * spacing, "tone1": center - 0.5 * spacing,
                "tone2": center + 0.5 * spacing, "tone3": center + 1.5 * spacing}
    return {}


def _auto_sps(symrate: float, spacing: float) -> int:
    """samples/bit giving a modem native rate that cleanly holds the tones
    (native = symrate*sps >= 4x the full spacing, and >= 8)."""
    return max(8, int(math.ceil(4.0 * spacing / max(symrate, 1.0))))


def signal_bw_hz(sig: dict) -> float:
    """Rough occupied bandwidth (Hz) of one FSK/4FSK signal: the full tone
    excursion (spacing * (M-1)) plus a symbol-rate skirt on each side."""
    mod = str(sig.get("modulation", "FSK")).upper()
    try:
        symrate = _symbol_rate(sig) if "DSSS" in mod else float(sig.get("symbol_rate", 0.0))
    except KeyError:
        symrate = 0.0
    spacing = float(sig.get("tone_spacing_hz", 1000.0))
    if mod == "CW":
        return 0.0
    if "DSSS" in mod:
        # main lobe of the chip stream: +/- chip rate
        return 2.0 * _dsss_code_len(sig) * symrate
    if mod in ("CSS", "LORA", "LORA-CSS"):
        return float(sig.get("css_bw", 125_000.0))
    m = 4 if mod == "4FSK" else 2
    return spacing * (m - 1) + 2.0 * symrate


# spread-spectrum helpers

def _sig_code_spec(sig: dict, rail: str = "") -> dict | None:
    """The explicit spreading-code spec of a DSSS signal (never a default).
    rail '' = the I/common code: "code_spec", else "lfsr" (family lfsr),
    else the legacy seed PN written out as {"family": "seed", ...}.
    rail 'q' = the optional second (Q-rail) code ("code_spec_q" / "lfsr_q")."""
    key_cs, key_lf = ("code_spec_q", "lfsr_q") if rail == "q" else ("code_spec", "lfsr")
    if sig.get(key_cs):
        return dict(sig[key_cs])
    if sig.get(key_lf):
        return dict({"family": "lfsr"}, **sig[key_lf])
    if rail == "q":
        return None
    return {"family": "seed", "length": int(sig.get("spread_factor", 15) or 15),
            "seed": int(sig.get("pn_seed", 1) or 1)}


def _dsss_code_len(sig: dict) -> int:
    """True chips/symbol of a DSSS signal: the resolved code length (a gold/m
    code is 2^n-1 long whatever spread_factor says), else spread_factor."""
    sf = int(sig.get("spread_factor", 15) or 15)
    cs = _sig_code_spec(sig)
    if cs:
        try:
            from dpmg.modulation.spreading_codes import code_len
            return int(code_len(cs, sf))
        except Exception:
            pass
    return sf


def _dsss_carrier(sig: dict) -> str:
    """Carrier family of a DSSS signal: 'carrier' / 'dsss_modem' key, else the
    modulation name (QPSK-DSSS ...), else bpsk."""
    from dpmg.modulation.spread_spectrum import canon_carrier
    c = sig.get("carrier") or sig.get("dsss_modem")
    if c:
        return canon_carrier(c)
    mod = _canon_mod(sig.get("modulation", "DSSS"))
    return {"QPSK-DSSS": "qpsk", "OQPSK-DSSS": "oqpsk", "PI4QPSK-DSSS": "pi4qpsk"}.get(mod, "bpsk")


def _symbol_rate(sig: dict) -> float:
    """symbol_rate, or chip_rate_hz / code length for a DSSS signal."""
    if sig.get("symbol_rate") is not None:
        return float(sig["symbol_rate"])
    if sig.get("chip_rate_hz") is not None and "DSSS" in _canon_mod(sig.get("modulation", "")):
        return float(sig["chip_rate_hz"]) / float(_dsss_code_len(sig))
    raise KeyError("symbol_rate")


def _spread_kwargs(sig: dict, mod: str) -> dict:
    """ModemConfig extras for the spread mods (empty for everything else)."""
    kw = {}
    if "DSSS" in mod:
        kw["spread_factor"] = int(sig.get("spread_factor", 15) or 15)
        if sig.get("pn_seed") is not None:
            kw["pn_seed"] = int(sig["pn_seed"])
        kw["code_spec"] = _sig_code_spec(sig)        # always explicit
        csq = _sig_code_spec(sig, "q")
        if csq:
            kw["code_spec_q"] = csq                   # second (Q-rail) code
        kw["dsss_modem"] = _dsss_carrier(sig)         # DSSS_CARRIERS
    elif mod in ("CSS", "LORA", "LORA-CSS"):
        for k in ("css_sf", "css_bw", "css_os"):
            if sig.get(k) is not None:
                kw[k] = sig[k]
    return kw


# multi-user DSSS

def _code_sets(spec: dict) -> dict:
    """{name: [explicit code spec, ...]} for every set the spec defines."""
    from dpmg.modulation.lfsr_codes import make_code_set
    sets = {}
    if spec.get("code_set"):
        sets["default"] = make_code_set(spec["code_set"])
    for name, sd in (spec.get("code_sets") or {}).items():
        sets[str(name)] = make_code_set(sd)
    return sets


def expand_users(spec: dict) -> dict:
    """Turn the spec's "users" list (+ "code_set"/"code_sets" + "dsss" defaults)
    into ordinary DSSS signals appended to "signals". Returns a copy; a spec
    without users comes back as-is. Every expanded signal carries an explicit
    code spec, its user id, carrier, symbol rate and samples_per_chip."""
    users = spec.get("users") or []
    if not users:
        return spec
    sets = _code_sets(spec)
    d = dict(spec.get("dsss") or {})
    base_off = float(d.get("offset_hz", 0.0))
    spacing = float(d.get("spacing_hz", 0.0) or 0.0)
    sigs = list(spec.get("signals") or [])
    for i, u in enumerate(users):
        u = dict(u)
        uid = str(u.get("id") or u.get("user") or f"U{i + 1:02d}")
        carrier = str(u.get("carrier") or d.get("carrier") or "bpsk")
        # the code: explicit on the user, else from a named / default set
        if u.get("code_spec"):
            cs = dict(u["code_spec"])
        elif u.get("lfsr"):
            cs = dict({"family": "lfsr"}, **u["lfsr"])
        else:
            sname = str(u.get("set") or ("default" if "default" in sets else next(iter(sets), "")))
            if sname not in sets:
                raise ValueError(f"user {uid}: no code set {sname!r} (define code_set / code_sets)")
            codes = sets[sname]
            k = u.get("code", i)
            if isinstance(k, str) and not k.lstrip("-").isdigit():
                raise ValueError(f"user {uid}: code must be an index into set {sname!r}")
            k = int(k)
            if k >= len(codes):
                raise ValueError(f"user {uid}: code index {k} but set {sname!r} has {len(codes)} codes")
            cs = dict(codes[k])
        sig = {"modulation": "DSSS", "name": uid, "user": uid, "dsss_modem": carrier,
               "code_spec": cs, "message": str(u.get("message", f"{uid} HELLO")),
               "amplitude": float(u.get("amplitude", d.get("amplitude", 0.8))),
               "offset_hz": float(u.get("offset_hz", base_off + i * spacing)),
               "samples_per_chip": int(u.get("samples_per_chip", d.get("samples_per_chip", 2)))}
        if u.get("code_q") is not None or u.get("code_spec_q") or u.get("lfsr_q"):
            if u.get("code_spec_q"):
                sig["code_spec_q"] = dict(u["code_spec_q"])
            elif u.get("lfsr_q"):
                sig["lfsr_q"] = dict(u["lfsr_q"])
            else:
                sname = str(u.get("set") or ("default" if "default" in sets else next(iter(sets), "")))
                sig["code_spec_q"] = dict(sets[sname][int(u["code_q"])])
        if u.get("chip_rate_hz") is not None or (d.get("chip_rate_hz") is not None
                                                 and u.get("symbol_rate") is None):
            sig["chip_rate_hz"] = float(u.get("chip_rate_hz", d.get("chip_rate_hz")))
        else:
            sig["symbol_rate"] = float(u.get("symbol_rate", d.get("symbol_rate", 2400)))
        # stagger: user i starts stagger_chips * i chips late unless delay_s given
        if u.get("delay_s") is not None:
            sig["delay_s"] = float(u["delay_s"])
        elif d.get("stagger_chips"):
            from dpmg.modulation.spreading_codes import code_len
            L = code_len(cs, 15)
            sr = sig.get("symbol_rate") or (sig["chip_rate_hz"] / L)
            sig["delay_s"] = float(d["stagger_chips"]) * i / (sr * L)
        for k in ("frame", "fec", "decode_gate_db", "burst_period_s", "bits"):
            if u.get(k) is not None:
                sig[k] = u[k]
            elif d.get(k) is not None and k != "bits":
                sig[k] = d[k]
        sigs.append(sig)
    out = dict(spec, signals=sigs)
    out.pop("users", None)
    return out


def overlay_report(spec: dict) -> dict | None:
    """Correlation / interference report for the spec's DSSS users (after
    expand_users): one row per user with its code label, plus the set report
    (peak off-zero autocorr, pairwise peak cross-corr, predicted MUI SNR)."""
    from dpmg.modulation.lfsr_codes import set_report
    from dpmg.modulation.spreading_codes import resolve_code, describe_code
    ex = expand_users(spec)
    dsss = [s for s in ex.get("signals") or [] if "DSSS" in _canon_mod(s.get("modulation", ""))]
    if not dsss:
        return None
    codes, names, rows = [], [], []
    for s in dsss:
        cs = _sig_code_spec(s)
        try:
            c = resolve_code(cs, int(s.get("spread_factor", 15) or 15))
        except Exception as e:                                       # noqa: BLE001
            rows.append({"user": s.get("user", s.get("name")), "error": str(e)})
            continue
        codes.append(c); names.append(str(s.get("user") or s.get("name")))
        rows.append({"user": names[-1], "carrier": _dsss_carrier(s), "length": int(len(c)),
                     "code": describe_code(cs, len(c)), "offset_hz": float(s.get("offset_hz", 0.0)),
                     "message": s.get("message", "")})
    same_len = len({len(c) for c in codes}) == 1
    rep = set_report(codes, names) if (codes and same_len) else None
    by_off = {}
    for r in rows:
        by_off.setdefault(r.get("offset_hz"), []).append(r["user"])
    return {"users": rows, "set": rep, "same_length": same_len,
            "overlay_groups": [{"offset_hz": k, "users": v} for k, v in by_off.items()],
            "expected_mui_snr_db": (min(rep["mui_snr_db"]) if rep else None)}


def resolve_signal_offsets(spec: dict) -> dict:
    """A signal may give its absolute RF center (center_freq_hz)
    instead of offset_hz; it is resolved against the spec's center_freq_hz (the
    radio tuning). Returns a copy; a spec without signal centers comes back
    as-is."""
    sigs = spec.get("signals") or []
    if not any(isinstance(s, dict) and s.get("center_freq_hz") is not None for s in sigs):
        return spec
    if spec.get("center_freq_hz") is None:
        raise ValueError("signals give center_freq_hz but the spec has no "
                         "center_freq_hz (radio tuning) to place them against")
    lo = float(spec["center_freq_hz"])
    out = [dict(s, offset_hz=float(s["center_freq_hz"]) - lo)
           if isinstance(s, dict) and s.get("center_freq_hz") is not None else s
           for s in sigs]
    return dict(spec, signals=out)


def validate_spec(spec: dict, radio_rate: float | None = None,
                  guard: float = 0.95, peak_fs=None) -> dict:
    """Checks + bounds for a scenario spec against a radio rate.

    Returns {ok, errors[], warnings[], build_rate, radio_rate, scale,
    usable_hz, signals[], peak_fs}. 'errors' are hard problems (signal outside
    the usable band: it would alias/clip, or peak_fs out of range);
    'warnings' are soft (the scenario's own rate differs from the radio's, so
    playing it as-is scales every frequency by 'scale'). build_rate is the
    rate the IQ would be built at:
    the spec's sample_rate_hz, else the radio rate.
    """
    errors: list[str] = []
    warnings: list[str] = []
    if isinstance(spec, dict) and spec.get("users"):
        try:
            spec = expand_users(spec)                  # multi-user DSSS
        except Exception as e:                         # noqa: BLE001
            return {"ok": False, "errors": [f"users: {e}"], "warnings": [],
                    "build_rate": 0.0, "radio_rate": radio_rate, "scale": None,
                    "usable_hz": 0.0, "signals": []}
    if not isinstance(spec, dict) or not spec.get("signals"):
        return {"ok": False, "errors": ["spec has no 'signals' list"],
                "warnings": [], "build_rate": 0.0, "radio_rate": radio_rate,
                "scale": None, "usable_hz": 0.0, "signals": []}
    spec_rate = float(spec.get("sample_rate_hz") or 0.0)
    build_rate = spec_rate or float(radio_rate or 0.0)
    if build_rate <= 0:
        return {"ok": False,
                "errors": ["no sample_rate_hz in spec and radio rate unknown"],
                "warnings": [], "build_rate": 0.0, "radio_rate": radio_rate,
                "scale": None, "usable_hz": 0.0, "signals": []}
    usable = 0.5 * build_rate * guard

    peak = None
    try:
        peak = resolve_peak_fs(spec, peak_fs)
    except ValueError as e:
        errors.append(str(e))

    scale = None
    if radio_rate and spec_rate and abs(radio_rate - spec_rate) > spec_rate * 1e-3:
        scale = radio_rate / spec_rate
        warnings.append(
            f"scenario is built for {spec_rate/1e6:g} MSps but the radio is at "
            f"{radio_rate/1e6:g} MSps; played as-is, every frequency scales "
            f"x{scale:.4g} (offsets/spacings {scale:.4g}x wider)")

    try:
        spec = resolve_signal_offsets(spec)
    except ValueError as e:
        errors.append(str(e))
    sig_info = []
    for i, sig in enumerate(spec["signals"]):
        try:
            off = float(sig.get("offset_hz", 0.0))
            bw = signal_bw_hz(sig)
            edge = abs(off) + bw / 2.0
            inband = edge <= usable
            sig_info.append({"i": i, "offset_hz": off, "bw_hz": bw,
                             "edge_hz": edge, "in_band": inband})
            if not inband:
                errors.append(
                    f"signal {i} ({sig.get('modulation', '?')} @ "
                    f"{off/1e6:+.3f} MHz, ~{bw/1e3:.0f} kHz wide) reaches "
                    f"{edge/1e6:.3f} MHz, beyond the usable +/-{usable/1e6:.3f} MHz "
                    f"at {build_rate/1e6:g} MSps")
        except (KeyError, ValueError, TypeError) as e:
            errors.append(f"signal {i} invalid: {e}")

    # smallest sample rate at which every signal fits the usable band: so a
    # caller can recommend a rate/re-tune when the scenario is too wide for the
    # current rate
    max_edge = max((s["edge_hz"] for s in sig_info), default=0.0)
    min_rate = (2.0 * max_edge / guard) if max_edge > 0 else 0.0
    return {"ok": not errors, "errors": errors, "warnings": warnings,
            "build_rate": build_rate, "radio_rate": radio_rate, "scale": scale,
            "usable_hz": usable, "min_rate_hz": min_rate, "signals": sig_info,
            "peak_fs": peak}


def _canon_mod(name: str) -> str:
    """Scenario modulation name: a name get_modulator and decode_pool accept.
    Upper-casing is fine for every mod except pi/4-QPSK, whose canonical forms
    are 'pi/4-QPSK' / 'PI4QPSK': the all-caps 'PI/4-QPSK' that a blind .upper()
    produced is rejected by get_modulator (the Phase-2 build_fail)."""
    m = str(name).strip().upper()
    if m in ("PI/4-QPSK", "PI4-QPSK", "PI4QPSK", "π/4-QPSK"):
        return "PI4QPSK"
    if m in ("PI/4-QPSK-DSSS", "PI4-QPSK-DSSS", "PI4QPSK-DSSS", "π/4-QPSK-DSSS", "DSSS-PI4QPSK"):
        return "PI4QPSK-DSSS"                            # DSSS_CARRIERS
    if m in ("DSSS-OQPSK",):
        return "OQPSK-DSSS"
    return m


def build_signal(sig: dict, fs: float):
    """Return (iq_at_fs complex64, plan_entry dict) for one signal (entry is
    None for a CW carrier, which has nothing to decode)."""
    mod = _canon_mod(sig["modulation"])
    if mod == "CW":
        n = int(round(fs * float(sig.get("duration_s", 1.0))))
        off = float(sig.get("offset_hz", 0.0))
        iq = float(sig.get("amplitude", 0.8)) * np.exp(2j * np.pi * off / fs * np.arange(n))
        iq = np.roll(iq, int(round(float(sig.get("delay_s") or 0.0) * fs)))  # delay_s
        return iq.astype(np.complex64), None
    symrate = _symbol_rate(sig)                       # or chip_rate_hz / code length
    spacing = float(sig.get("tone_spacing_hz", 1000.0))
    spread = _spread_kwargs(sig, mod)
    if "DSSS" in mod:
        # Samples_per_bit must be a whole number of samples per chip
        # times the code length: the FSK-derived default (9 at 2400 Bd) is
        # not, so the modulator would emit 15 samples/bit at a 9-samples/bit
        # rate and put the symbols on air at 1440 Bd against a 2400 Bd plan.
        # 2 samples/chip is the despreader's floor.
        code_len = _dsss_code_len(sig)
        spc = int(sig.get("samples_per_chip") or 0)
        if spc <= 0:
            _req = int(sig.get("samples_per_bit") or 0)
            spc = max(2, _req // code_len) if _req else 2
        sps = code_len * spc
    else:
        sps = int(sig.get("samples_per_bit") or _auto_sps(symrate, spacing))
    tones = _tones(sig)
    fec = sig.get("fec") or None
    # "secure": true | {...} seals the message body
    # (frame_codec.encode_secure) and marks the decode plan entry. Refused
    # with a clear error while the DPMG_SECURE_MSG flag is off or no key is
    # loaded, never silently built as a clear frame.
    secure = sig.get("secure") or None
    if secure:
        from dpmg.secure import api as _sf
        if not _sf.secure_msg_enabled():
            raise ValueError("signal asks for secure messaging but DPMG_SECURE_MSG is off")
        if not sig.get("bits"):
            # the radio never seals: the ground station seals (frame_codec.encode_secure)
            # and ships the on-air bits in the spec (gen_scenario's bits path)
            raise ValueError("secure signal needs sealed frame `bits` from the ground station "
                             "(sealing never happens on the radio)")
        secure = dict(secure) if isinstance(secure, dict) else {"on": True}
        if fec and _sf.capacity(fec, long_header=True) is None:
            raise ValueError(f"fec {fec} cannot carry the AEAD tag (see secure_frame.unsupported_codings)")
    mkw = dict(modulation=mod, symbol_rate=symrate, samples_per_bit=sps,
               amplitude=float(sig.get("amplitude", 0.8)), **tones, **spread)
    if fec:
        mkw["fec"] = fec
    if sig.get("gfsk_bw_hz") is not None:
        mkw["gfsk_bw_hz"] = float(sig["gfsk_bw_hz"])     # GFSK BT variants
    if mod == "4FSK" and sig.get("dibit_mapping"):
        # The TX modulator must use the same dibit to tone mapping the
        # plan hands the demod, or only 'binary' round-trips. ModemConfig
        # carries it to get_modulator.
        mkw["dibit_mapping"] = str(sig["dibit_mapping"])
    m = Modem(ModemConfig(**mkw))
    raw = sig.get("bits")
    if raw:
        # On-air bits as given (replica of a capture), no framing/pad.
        # A ground-sealed frame arrives as bits too, but it is a
        # framed burst, not a capture replica: give it the modem's lead/tail
        # dead air (as the message path does) so acquisition has settling room.
        bursts = [raw] if isinstance(raw, str) else list(raw)
        parts = [np.asarray(m._mod.modulate(b).iq_samples, dtype=np.complex64)
                 for b in bursts]
        if secure or sig.get("pad"):
            parts = [m._pad(p) for p in parts]
    else:
        parts = [m.transmit(sig.get("message", "PING"))]
    nper = int(round(float(sig.get("burst_period_s") or 0.0) * m.sample_rate))
    if nper > 0:
        parts = [np.concatenate([p, np.zeros(max(0, nper - len(p)), np.complex64)])
                 for p in parts]
    up = _resample_iq(np.concatenate(parts), m.sample_rate, fs)
    # Circular shift within the signal's own cycle (staggers copies)
    up = np.roll(up, int(round(float(sig.get("delay_s") or 0.0) * fs)))
    off = float(sig.get("offset_hz", 0.0))
    n = np.arange(len(up))
    iq = (up * np.exp(2j * np.pi * off / fs * n)).astype(np.complex64)
    plan_params = {"symbol_rate": symrate, "tone_spacing_hz": spacing}
    if mod == "4FSK":
        plan_params["dibit_mapping"] = sig.get("dibit_mapping", "binary")
    if fec:
        plan_params["fec"] = fec
    if secure:
        plan_params["secure"] = secure
    if sig.get("decode_gate_db") is not None:
        # Opt-in per-channel decode gate (slot margin, dB) for
        # the channelizer's continuous-channel gate; absent = stock 12 dB set
        plan_params["decode_gate_db"] = float(sig["decode_gate_db"])
    band_hz = 0.0
    if "DSSS" in mod:
        # the despreader needs the same code, and the channelizer sizes its
        # gate rate from spread_factor (SPREAD_FSOUT): so the plan carries
        # the true code length plus the family/seed that reproduces it
        plan_params.update(spread)
        plan_params["spread_factor"] = _dsss_code_len(sig)
        band_hz = signal_bw_hz(sig)
    elif spread:
        plan_params.update(spread)
        band_hz = signal_bw_hz(sig)
    entry = {"name": sig.get("name") or sig.get("user") or f"{mod}@{off/1e3:+.0f}k",
             "modulation": mod, "params": plan_params,
             "offset_hz": off, "power_db": 0.0, "role": "known", "decode": True,
             "frame": dict(sig.get("frame", DEFAULT_SYNC), **({"secure": secure} if secure else {})),
             "meta": {"samples_per_bit": sps, "tone_center_hz":
                      float(sig.get("tone_center_hz", 0.0))}}
    if band_hz > 0:
        entry["band_hz"] = float(band_hz)
    if "DSSS" in mod:
        # Who this channel is and what despreads it, for the
        # per-user decode view (plan name = user id)
        from dpmg.modulation.spreading_codes import describe_code
        entry["meta"]["user"] = str(sig.get("user") or entry["name"])
        entry["meta"]["carrier"] = spread.get("dsss_modem", "bpsk")
        entry["meta"]["code"] = describe_code(spread.get("code_spec"), plan_params["spread_factor"])
        if spread.get("code_spec_q"):
            entry["meta"]["code_q"] = describe_code(spread["code_spec_q"], plan_params["spread_factor"])
    if sig.get("center_freq_hz") is not None:
        entry["meta"]["center_freq_hz"] = float(sig["center_freq_hz"])
    return iq, entry


def build_scenario(spec: dict, out_dir: str, peak_fs=None):
    """Build <name>.iq.bin and <name>.plan.json. The signals are summed at
    their relative amplitudes and the mix is scaled so its peak is peak_fs
    (argument, else the spec's "peak_fs", else PEAK_FS_DEFAULT)."""
    peak_target = resolve_peak_fs(spec, peak_fs)
    spec = resolve_signal_offsets(expand_users(spec))   # multi-user DSSS
    fs = float(spec["sample_rate_hz"])
    loop_s = float(spec.get("loop_s", 0.2))
    nloop = int(fs * loop_s)
    mix = np.zeros(nloop, dtype=np.complex64)
    entries = []
    for sig in spec["signals"]:
        iq, entry = build_signal(sig, fs)
        reps = int(np.ceil(nloop / len(iq)))
        mix += np.tile(iq, reps)[:nloop]
        if entry is not None:
            entries.append(entry)
    peak = float(np.max(np.abs(mix)) or 1.0)
    mix = (mix * (peak_target / peak)).astype(np.complex64)
    os.makedirs(out_dir, exist_ok=True)
    name = spec["name"]
    mix.tofile(os.path.join(out_dir, f"{name}.iq.bin"))
    plan = {"schema_version": 1, "description": spec.get("description", name),
            "compose": {"tx_radio": spec.get("tx_radio", "g40"),
                        "sample_rate_hz": fs,
                        "center_freq_hz": float(spec.get("center_freq_hz", 425_025_000.0)),
                        "rf_bandwidth_hz": fs * 0.8,
                        "peak_fs": peak_target},
            "entries": entries}
    try:
        rep = overlay_report(spec)
        if rep and len(rep["users"]) > 1:
            plan["dsss_overlay"] = rep                  # Code-set report
    except Exception as e:                              # noqa: BLE001
        plan["dsss_overlay"] = {"error": str(e)}
    json.dump(plan, open(os.path.join(out_dir, f"{name}.plan.json"), "w"), indent=1)
    return len(entries), mix.nbytes


# representative "open-signal" presets + parameter sweep

def preset_specs():
    specs = []
    # realistic open-signal presets (baseband/RF-agnostic; sample_rate set per host)
    specs.append({"name": "fsk_afsk_bell202", "description": "Bell-202 AFSK 1200 baud, 1200/2200 Hz",
                  "sample_rate_hz": 1_920_000, "signals": [
        {"modulation": "FSK", "symbol_rate": 1200, "tone_spacing_hz": 1000,
         "tone_center_hz": 1700, "offset_hz": 150_000, "message": "AFSK BELL202"}]})
    specs.append({"name": "fsk_pocsag_1200", "description": "POCSAG-class FSK 1200 baud, 9 kHz shift",
                  "sample_rate_hz": 1_920_000, "signals": [
        {"modulation": "FSK", "symbol_rate": 1200, "tone_spacing_hz": 9000,
         "offset_hz": 150_000, "message": "POCSAG 1200"}]})
    specs.append({"name": "fsk_pocsag_2400", "description": "POCSAG-class FSK 2400 baud, 9 kHz shift",
                  "sample_rate_hz": 1_920_000, "signals": [
        {"modulation": "FSK", "symbol_rate": 2400, "tone_spacing_hz": 9000,
         "offset_hz": 150_000, "message": "POCSAG 2400"}]})
    specs.append({"name": "fsk_9600_gmsk", "description": "9600-baud narrow FSK (GMSK-ish, 4.8 kHz)",
                  "sample_rate_hz": 1_920_000, "signals": [
        {"modulation": "FSK", "symbol_rate": 9600, "tone_spacing_hz": 4800,
         "offset_hz": 200_000, "message": "9600 FSK"}]})
    specs.append({"name": "4fsk_dmr", "description": "DMR-class 4FSK 4800 sym/s, +-1.944/0.648 kHz (spacing 1296)",
                  "sample_rate_hz": 1_920_000, "signals": [
        {"modulation": "4FSK", "symbol_rate": 4800, "tone_spacing_hz": 1296,
         "offset_hz": 200_000, "message": "DMR 4FSK"}]})
    specs.append({"name": "4fsk_wide", "description": "wide 4FSK 4800 sym/s, 6 kHz outer spacing",
                  "sample_rate_hz": 1_920_000, "signals": [
        {"modulation": "4FSK", "symbol_rate": 4800, "tone_spacing_hz": 2000,
         "offset_hz": 200_000, "message": "4FSK WIDE"}]})
    # parameter sweeps: symbol rate x tone spacing, several signals per scenario
    sr_sweep = []
    offs = np.linspace(-700_000, 700_000, 6)
    for i, (sr, sp) in enumerate([(1200, 2000), (2400, 3000), (4800, 5000),
                                  (4800, 10000), (9600, 8000), (2400, 12000)]):
        sr_sweep.append({"modulation": "FSK", "symbol_rate": sr, "tone_spacing_hz": sp,
                         "offset_hz": float(offs[i]), "message": f"FSK {sr}/{sp}"})
    specs.append({"name": "fsk_sweep", "description": "6 FSK: symbol-rate x tone-spacing sweep across 1.4 MHz",
                  "sample_rate_hz": 1_920_000, "signals": sr_sweep})
    f4_sweep = []
    for i, (sr, sp) in enumerate([(2400, 1296), (4800, 1296), (4800, 2000),
                                  (9600, 2000), (4800, 4000), (2400, 3000)]):
        f4_sweep.append({"modulation": "4FSK", "symbol_rate": sr, "tone_spacing_hz": sp,
                         "offset_hz": float(offs[i]), "message": f"4FSK {sr}/{sp}"})
    specs.append({"name": "fsk4_sweep", "description": "6 4FSK: symbol-rate x tone-spacing sweep across 1.4 MHz",
                  "sample_rate_hz": 1_920_000, "signals": f4_sweep})
    return specs


def dsss_preset_specs():
    """DSSS builder presets: multi-user overlays (one centre frequency, one
    code per user). The predicted equal-power MUI of each set is recorded in
    the description and in the plan's dsss_overlay block."""
    from dpmg.modulation.lfsr_codes import make_code_set, set_report
    from dpmg.modulation.spreading_codes import resolve_code

    def _mui(setdef):
        codes = [resolve_code(c) for c in make_code_set(setdef)]
        r = set_report(codes)
        return r["worst_pair"][2], min(r["mui_snr_db"])

    specs = []
    # 4 BPSK users, Gold n=5 (31 chips) 2400 Bd overlay at +300 kHz
    cs4 = {"kind": "gold", "n": 5, "count": 4, "length": 31}
    pk, mui = _mui(cs4)
    specs.append({"name": "dsss_4user_bpsk",
                  "description": f"4-user BPSK-DSSS CDMA overlay, Gold n=5 (31 chips) at 2400 Bd, "
                                 f"+300 kHz; set peak xcorr {pk:.0f}/31, predicted MUI SNR "
                                 f">= {mui:.1f} dB (equal power)",
                  "sample_rate_hz": 1_920_000, "loop_s": 0.25,
                  "code_set": cs4,
                  "dsss": {"symbol_rate": 2400, "samples_per_chip": 2, "offset_hz": 300_000,
                           "carrier": "bpsk", "amplitude": 0.8, "stagger_chips": 7},
                  "users": [{"id": f"U{k + 1:02d}", "code": k, "message": f"U{k + 1:02d} PING"}
                            for k in range(4)],
                  "signals": []})
    # mixed carriers: BPSK / QPSK / OQPSK / pi4 users, Gold n=6 (63 chips)
    cs6 = {"kind": "gold", "n": 6, "count": 4, "length": 63}
    pk, mui = _mui(cs6)
    specs.append({"name": "dsss_mixed_carriers",
                  "description": f"4 users on BPSK / QPSK / OQPSK / pi4-QPSK carriers, one Gold n=6 "
                                 f"set (63 chips) at 2400 Bd, +300 kHz; set peak xcorr {pk:.0f}/63, "
                                 f"predicted MUI SNR >= {mui:.1f} dB",
                  "sample_rate_hz": 1_920_000, "loop_s": 0.3,
                  "code_set": cs6,
                  "dsss": {"symbol_rate": 2400, "samples_per_chip": 2, "offset_hz": 300_000,
                           "amplitude": 0.8, "stagger_chips": 11},
                  "users": [{"id": "U01", "code": 0, "carrier": "bpsk", "message": "U01 BPSK"},
                            {"id": "U02", "code": 1, "carrier": "qpsk", "message": "U02 QPSK"},
                            {"id": "U03", "code": 2, "carrier": "oqpsk", "message": "U03 OQPSK"},
                            {"id": "U04", "code": 3, "carrier": "pi4qpsk", "message": "U04 PI4"}],
                  "signals": []})
    # 16 users, Gold n=7 (127 chips), 1200 Bd, short messages (frame fits the
    # channelizer's 2-frame decode window: frame_bits * 127 * 2 < 32760 samples)
    cs7 = {"kind": "gold", "n": 7, "count": 16, "length": 127}
    pk, mui = _mui(cs7)
    specs.append({"name": "dsss_16user",
                  "description": f"16-user BPSK-DSSS CDMA overlay, Gold n=7 (127 chips) at 1200 Bd, "
                                 f"+300 kHz, short messages; set peak xcorr {pk:.0f}/127, predicted "
                                 f"MUI SNR >= {mui:.1f} dB (equal power, 15 interferers)",
                  "sample_rate_hz": 1_920_000, "loop_s": 0.4,
                  "code_set": cs7,
                  "dsss": {"symbol_rate": 1200, "samples_per_chip": 2, "offset_hz": 300_000,
                           "carrier": "bpsk", "amplitude": 0.8, "stagger_chips": 9},
                  "users": [{"id": f"U{k + 1:02d}", "code": k, "message": f"U{k + 1:02d}"}
                            for k in range(16)],
                  "signals": []})
    return specs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("spec", nargs="?", help="scenario .json spec")
    ap.add_argument("--out", default="scenarios", help="output dir")
    ap.add_argument("--presets", action="store_true",
                    help="write the preset FSK/4FSK library to scenarios/json/ + build them")
    ap.add_argument("--dsss-presets", action="store_true",
                    help="write the multi-user DSSS presets (DSSS_BUILDER) to scenarios/json/ + build them")
    ap.add_argument("--peak-fs", type=float, default=None,
                    help=f"peak of the mix as a fraction of full scale (default: the spec's "
                         f"peak_fs, else {PEAK_FS_DEFAULT:g})")
    a = ap.parse_args()
    if a.dsss_presets:
        jdir = os.path.join(a.out, "json")
        os.makedirs(jdir, exist_ok=True)
        for spec in dsss_preset_specs():
            json.dump(spec, open(os.path.join(jdir, f"{spec['name']}.json"), "w"), indent=1)
            nsig, nb = build_scenario(spec, a.out, a.peak_fs)
            print(f"  {spec['name']:22} {nsig} sig  {nb/1e6:.1f} MB  (json: {jdir}/{spec['name']}.json)")
        return 0
    if a.presets:
        jdir = os.path.join(a.out, "json")
        os.makedirs(jdir, exist_ok=True)
        for spec in preset_specs():
            json.dump(spec, open(os.path.join(jdir, f"{spec['name']}.json"), "w"), indent=1)
            nsig, nb = build_scenario(spec, a.out, a.peak_fs)
            print(f"  {spec['name']:22} {nsig} sig  {nb/1e6:.1f} MB  (json: {jdir}/{spec['name']}.json)")
        return 0
    if not a.spec:
        ap.error("give a spec .json or --presets")
    spec = json.load(open(a.spec))
    nsig, nb = build_scenario(spec, a.out, a.peak_fs)
    print(f"built {spec['name']}: {nsig} signals, {nb/1e6:.1f} MB -> {a.out}/{spec['name']}.iq.bin + .plan.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
