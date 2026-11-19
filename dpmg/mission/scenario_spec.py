"""Scenario JSON helpers for the mission planner's comm waveform step.

A comm waveform step (technique scenario_json) carries a scenario spec, the
same JSON gen_scenario_json.py and the service's gen_scenario command read: a
dict with a "signals" list (modulation, symbol rate, offset, FEC and so on)
or a DSSS "users" list. The spec either sits inline in the step's params
("spec", uploaded on the console) or is a path on the radio ("spec_path").

Nothing here touches a radio. The tab uses these to accept an upload and to
write the one-line summary under the step; the runner uses the size check to
decide whether a spec rides inline in the gen_scenario command or has to be
pushed first over the FILE_* transfer (spec_inbox).
"""

from __future__ import annotations

import json
import os

# a command datagram is 1400 bytes on the wire; the playback panel sends a spec
# inline up to 200 bytes under that and pushes anything bigger as a file
SPEC_INLINE_MAX = 1200

try:
    from dpmg.scenario import gen_scenario_json as _gsj
except Exception:          # the generator is beside the GUI in the repo and image
    _gsj = None


def is_scenario_spec(obj) -> bool:
    """True for a dict with a non-empty signals list of dicts, or a non-empty
    DSSS users list. The service applies the same rule before building."""
    if not isinstance(obj, dict):
        return False
    sigs = obj.get("signals")
    if isinstance(sigs, list) and sigs and all(isinstance(s, dict) for s in sigs):
        return True
    users = obj.get("users")
    return isinstance(users, list) and bool(users)


def parse_upload(text: str, name: str = "") -> dict:
    """Parse the text of an uploaded .json and check it is a scenario spec.
    Raises ValueError with a message meant for the operator."""
    try:
        obj = json.loads(text)
    except Exception as e:
        raise ValueError(f"{name or 'file'} is not valid JSON: {e}")
    if not is_scenario_spec(obj):
        raise ValueError(f"{name or 'file'} is not a scenario JSON: it needs a "
                         f"'signals' list (or DSSS 'users')")
    return obj


def spec_blob(spec: dict) -> bytes:
    """The spec as compact JSON, the form it crosses the link in."""
    return json.dumps(spec, separators=(",", ":")).encode()


def needs_inbox(spec: dict) -> bool:
    """True when the spec is too big to ride inline in one command datagram
    and has to go over the FILE_* transfer as spec_inbox."""
    return len(spec_blob(spec)) > SPEC_INLINE_MAX


def _signals(spec: dict) -> list:
    """The spec's signals, with DSSS users expanded and absolute signal
    centers turned into offsets when the generator is importable."""
    s = spec
    if _gsj is not None:
        try:
            s = _gsj.expand_users(s)
        except Exception:
            pass
        try:
            s = _gsj.resolve_signal_offsets(s)
        except Exception:
            pass
    return [x for x in (s.get("signals") or []) if isinstance(x, dict)]


def _bw_hz(sig: dict) -> float:
    if _gsj is not None:
        try:
            return float(_gsj.signal_bw_hz(sig))
        except Exception:
            pass
    # without the generator: a symbol-rate skirt either side of the tone span
    try:
        sr = float(sig.get("symbol_rate") or 0.0)
    except (TypeError, ValueError):
        sr = 0.0
    try:
        sp = float(sig.get("tone_spacing_hz") or 0.0)
    except (TypeError, ValueError):
        sp = 0.0
    return sp + 2.0 * sr


def summarize(spec: dict) -> dict:
    """What the step shows about its spec: signal count, the modulations in
    order of first use, the band the signals occupy around the TX center
    (lo_hz, hi_hz, span_hz) and the spec's own sample rate and name."""
    sigs = _signals(spec)
    mods = []
    lo = hi = None
    for s in sigs:
        m = str(s.get("modulation") or "?").upper()
        if m not in mods:
            mods.append(m)
        try:
            off = float(s.get("offset_hz") or 0.0)
        except (TypeError, ValueError):
            off = 0.0
        bw = _bw_hz(s)
        a, b = off - bw / 2.0, off + bw / 2.0
        lo = a if lo is None else min(lo, a)
        hi = b if hi is None else max(hi, b)
    try:
        rate = float(spec.get("sample_rate_hz") or 0.0) or None
    except (TypeError, ValueError):
        rate = None
    return {"signals": len(sigs), "modulations": mods,
            "lo_hz": lo or 0.0, "hi_hz": hi or 0.0,
            "span_hz": (hi - lo) if (lo is not None and hi is not None) else 0.0,
            "sample_rate_hz": rate, "name": str(spec.get("name") or "")}


def _hz_text(hz: float) -> str:
    hz = float(hz)
    if abs(hz) >= 1e6:
        return f"{hz / 1e6:.6g} MHz"
    if abs(hz) >= 1e3:
        return f"{hz / 1e3:.6g} kHz"
    return f"{hz:.6g} Hz"


def summary_text(summary: dict, check: dict | None = None) -> str:
    """One line for the step editor. summary is summarize()'s dict (or the
    service's, from a check_only gen_scenario); check is the check_only
    result when the radio was asked, else None."""
    n = int(summary.get("signals") or 0)
    mods = ", ".join(summary.get("modulations") or []) or "?"
    bits = [f"{n} signal{'s' if n != 1 else ''}: {mods}"]
    span = float(summary.get("span_hz") or 0.0)
    if span > 0:
        bits.append(f"about {_hz_text(span)} wide ({_hz_text(summary.get('lo_hz', 0))} "
                    f"to {_hz_text(summary.get('hi_hz', 0))} of the TX center)")
    if check is None:
        if summary.get("sample_rate_hz"):
            bits.append(f"written for {float(summary['sample_rate_hz']) / 1e6:g} MSps")
        bits.append("radio offline, not checked against its rate")
        return "; ".join(bits)
    v = check.get("validation") or {}
    errs, warns = v.get("errors") or [], v.get("warnings") or []
    rate = v.get("build_rate") or check.get("sample_rate")
    if errs:
        bits.append("does not fit: " + "; ".join(str(e) for e in errs))
    else:
        if rate:
            bits.append(f"builds at {float(rate) / 1e6:g} MSps on the radio")
        if warns:
            bits.append("note: " + "; ".join(str(w) for w in warns))
        else:
            bits.append("fits")
    return "; ".join(bits)


def step_spec_source(params: dict) -> str:
    """'console' when the step holds its spec inline, 'radio' when it names a
    file on the radio, '' when it has neither (needs input)."""
    if is_scenario_spec(params.get("spec")):
        return "console"
    if str(params.get("spec_path") or "").strip():
        return "radio"
    return ""


def step_spec_label(params: dict) -> str:
    """The file name the step shows: the uploaded file's name, or the radio
    path's base name."""
    src = step_spec_source(params)
    if src == "console":
        return str(params.get("spec_name") or (params["spec"].get("name") if
                   isinstance(params.get("spec"), dict) else "") or "uploaded spec")
    if src == "radio":
        return os.path.basename(str(params.get("spec_path")))
    return ""
