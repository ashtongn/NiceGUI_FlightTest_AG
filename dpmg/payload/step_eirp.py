"""Expected EIRP of each keyed step of a mission plan, for the engineering
Payload page.

The page's own EIRP figure takes one frequency, one TX attenuation and a
waveform backoff typed by hand. A plan changes all three from step to step.
This module works them out per step: the frequency and TX attenuation are on
the step, and the backoff comes from the step's waveform, built here the way
the radio builds it.

Backoff. The radio scales every waveform so its peak is a set fraction of full
scale (ampl for a jammer or test signal, peak_fs for a comm scenario). The
average power is below that peak by the waveform's peak to average ratio, and
by more when it is pulsed. So for each step:

  peak_db  how far the peak is under full scale, 20 log10(1 / fraction)
  avg_db   how far the average power is under full scale; this is the backoff
           the EIRP model takes
  how      "built": the waveform was built and measured; "file": a played
           file, whose level is not known here, taken as full scale;
           "none": nothing to work from

Nothing here talks to a radio. The numbers are the model's (payload_budget and
payload_profiles.yaml), not a measurement.
"""
from __future__ import annotations

import functools
import json
import math
from typing import List, Optional

import numpy as np

from dpmg.payload import payload_budget as pb

BUILD_MAX_S = 0.05            # longest waveform built for the measurement
BUILD_MAX_SAMPLES = 1_000_000
_GEN_KEYS = ("f0", "f1", "f2", "freq", "bw", "spacing", "n_tones", "center", "span",
             "sweep_period_s", "noise_bw", "prf", "duty", "sym_rate", "sym_duty", "rolloff")


def _db(power_ratio: float) -> float:
    return 10.0 * math.log10(max(power_ratio, 1e-30))


def _measure(y, fraction: float) -> dict:
    """Backoff of a waveform whose peak the radio sets to fraction of full scale."""
    y = np.asarray(y)
    pk = float(np.max(np.abs(y))) if y.size else 0.0
    if pk <= 0 or fraction <= 0:
        return {"peak_db": None, "avg_db": None, "papr_db": None, "how": "none"}
    mean = float(np.mean(np.abs(y / pk) ** 2))
    peak_db = -_db(fraction ** 2)
    papr_db = -_db(mean)
    return {"peak_db": peak_db, "papr_db": papr_db, "avg_db": peak_db + papr_db, "how": "built"}


@functools.lru_cache(maxsize=256)
def _jam_backoff(kind: str, fs: float, seconds: float, ampl: float, kw_json: str) -> tuple:
    from dpmg.dsp import tools_engine as te
    kw = json.loads(kw_json)
    # long enough for a whole pulse period, short enough to build at once
    dur = min(max(seconds, 0.001), BUILD_MAX_S)
    prf = float(kw.get("prf") or 0)
    if prf > 0:
        dur = max(dur, min(2.0 / prf, 0.2))
    dur = min(dur, BUILD_MAX_SAMPLES / fs)
    r = _measure(te.generate(kind, fs, dur, **kw), ampl)
    return tuple(sorted(r.items()))


@functools.lru_cache(maxsize=256)
def _scenario_backoff(spec_json: str, fs: float, peak_fs: float) -> tuple:
    from dpmg.scenario import gen_scenario_json as gs
    spec = gs.resolve_signal_offsets(gs.expand_users(json.loads(spec_json)))
    n = int(min(fs * float(spec.get("loop_s", 0.2)), BUILD_MAX_SAMPLES))
    mix = np.zeros(n, dtype=np.complex64)
    for sig in spec["signals"]:
        iq, _entry = gs.build_signal(sig, fs)
        mix += np.tile(iq, int(np.ceil(n / len(iq))))[:n]
    return tuple(sorted(_measure(mix, peak_fs).items()))


def step_backoff(step: dict, radio_rate: float) -> dict:
    """{"peak_db", "papr_db", "avg_db", "how"} for one step at the radio's rate."""
    tech = str(step.get("technique") or "")
    p = step.get("params") or {}
    fs = float(radio_rate or 0) or 1_920_000.0
    try:
        if tech.startswith("jammer.") or tech.startswith("tools."):
            kind = str(p.get("kind") or tech.split(".", 1)[1])
            kw = {k: float(p[k]) for k in _GEN_KEYS if p.get(k) is not None}
            if p.get("mod") is not None:
                kw["mod"] = str(p["mod"]).lower()
            if float(kw.get("prf") or 0) <= 0:
                kw.pop("prf", None)
                kw.pop("duty", None)
            ampl = max(0.0, min(1.0, float(p.get("ampl", 0.7))))
            return dict(_jam_backoff(kind, fs, float(p.get("seconds", 0.1)), ampl,
                                     json.dumps(kw, sort_keys=True)))
        if tech == "scenario_json" and isinstance(p.get("spec"), dict):
            from dpmg.scenario import gen_scenario_json as gs
            spec = p["spec"]
            rate = fs if p.get("match_radio_rate", True) else float(spec.get("sample_rate_hz") or fs)
            return dict(_scenario_backoff(json.dumps(spec, sort_keys=True), rate,
                                          float(gs.resolve_peak_fs(spec, p.get("peak_fs")))))
    except Exception:
        return {"peak_db": None, "papr_db": None, "avg_db": None, "how": "none"}
    if tech in ("iq_playback", "replay", "scenario_json"):
        return {"peak_db": 0.0, "papr_db": 0.0, "avg_db": 0.0, "how": "file"}
    return {"peak_db": None, "papr_db": None, "avg_db": None, "how": "none"}


def _when(step: dict) -> str:
    sch = step.get("schedule") or {}
    if "start_s" in sch:
        s = int(round(float(sch["start_s"])))
        return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"
    return "area" if step.get("location") or step.get("trigger") else "-"


def plan_rows(data: dict, prof_name: str, mission: Optional[dict], radio_id: str,
              default_tx_atten_db: Optional[float] = None, default_freq_mhz: Optional[float] = None,
              keyed=()) -> List[dict]:
    """One row for each keyed (radiating) step of the plan on radio_id, in plan
    order. A step that leaves the TX attenuation or the frequency blank takes
    the defaults (the radio's live values) and says so. Rows carry numbers, not
    text, so the page formats them."""
    if not mission or not radio_id:
        return []
    prof = pb.profile(data, prof_name)
    rate = next((float(r.get("sample_rate") or 0) for r in mission.get("radios", [])
                 if r.get("id") == radio_id), 0.0)
    rows = []
    for step in mission.get("steps", []):
        if step.get("radio") != radio_id or not step.get("radiates"):
            continue
        p = step.get("params") or {}
        hz = p.get("tx_center_hz") or p.get("center_hz") or p.get("retune_hz")
        f_mhz = float(hz) / 1e6 if hz else default_freq_mhz
        att = p.get("tx_atten_db")
        att_from_radio = att in (None, "")
        att = default_tx_atten_db if att_from_radio else float(att)
        bo = step_backoff(step, rate)
        tech = str(step.get("technique") or "")
        row = {"id": step.get("id"), "when": _when(step),
               "what": str(p.get("kind") or (tech.split(".", 1)[1] if "." in tech else tech)),
               "freq_mhz": f_mhz, "tx_atten_db": att, "atten_from_radio": att_from_radio,
               "backoff_db": bo["avg_db"], "peak_backoff_db": bo["peak_db"], "how": bo["how"],
               "keyed": step.get("id") in set(keyed or ()),
               "eirp_dbm": None, "eirp_w": None, "peak_eirp_dbm": None, "hpa_out_dbm": None,
               "region": "", "warnings": []}
        if f_mhz is not None and att is not None and bo["avg_db"] is not None:
            try:
                e = pb.eirp_chain(prof, f_mhz, att, bo["avg_db"])
                pk = pb.eirp_chain(prof, f_mhz, att, bo["peak_db"])
                row.update(eirp_dbm=e["eirp_dbm"], eirp_w=e["eirp_w"], hpa_out_dbm=e["hpa_out_dbm"],
                           peak_eirp_dbm=pk["eirp_dbm"], region=str(pk.get("hpa_region") or ""),
                           warnings=list(pk.get("warnings") or []))
            except Exception as ex:
                row["warnings"] = [str(ex)]
        rows.append(row)
    return rows


def plan_signature(mission: Optional[dict], radio_id: str) -> str:
    """Changes when a keyed step of radio_id changes, so the page knows to work the rows out again."""
    if not mission:
        return ""
    keep = [[s.get("id"), s.get("technique"), s.get("params"), s.get("schedule")]
            for s in mission.get("steps", []) if s.get("radio") == radio_id and s.get("radiates")]
    rate = [r.get("sample_rate") for r in mission.get("radios", []) if r.get("id") == radio_id]
    return json.dumps([mission.get("name"), rate, keep], sort_keys=True, default=str)
