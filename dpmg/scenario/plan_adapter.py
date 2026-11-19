"""
plan_adapter.py: bridge the channel_plan schema (file format) to channels.ChannelConfig
(the channelizer worker's internal per-channel model). Keeps the two decoupled:
the schema can evolve without touching DSP; the worker keeps its validated model.
"""
from __future__ import annotations
import json
from dpmg.scenario.channel_plan import ChannelPlan


def load_plan(path: str) -> ChannelPlan:
    return ChannelPlan.from_dict(json.load(open(path)))


def entry_to_channelconfig(entry, plan):
    """Convert one inline plan Entry into a channels.ChannelConfig.

    offset_hz: absolute f_center via plan.center_freq_hz + offset
    tone_deltas_hz: tone_spacing_hz (channels.py places tones symmetrically around DC)
    symbol_rate/samples_per_bit: straight across
    band width: params bw or derived from occupied width
    """
    from dpmg.dsp.channels import ChannelConfig
    p = entry.params or {}
    f_center = plan.center_freq_hz + float(entry.offset_hz)
    sr = float(p.get("symbol_rate", 0.0) or 0.0)

    # tone spacing: channels.py wants a single spacing; derive from deltas if present.
    # deltas [-3d,-d,+d,+3d]: spacing = 2d (adjacent tone separation).
    demod_params = {}
    if getattr(entry, "frame", None):        # carry declared frame through
        demod_params["frame"] = dict(entry.frame)
    deltas = p.get("tone_deltas_hz")
    if deltas and len(deltas) >= 2:
        srt = sorted(float(x) for x in deltas)
        adj = min(srt[i+1]-srt[i] for i in range(len(srt)-1))
        demod_params["tone_spacing_hz"] = float(adj)
    elif p.get("tone_spacing_hz"):
        demod_params["tone_spacing_hz"] = float(p["tone_spacing_hz"])
    if p.get("gfsk_bw_hz"):
        demod_params["gfsk_bw_hz"] = float(p["gfsk_bw_hz"])
    if p.get("dibit_mapping"):
        demod_params["dibit_mapping"] = p["dibit_mapping"]
    if p.get("fec"):                         # carry FEC spec to the decode pool
        demod_params["fec"] = dict(p["fec"])
    if p.get("secure"):                      # Sealed-body plan entry
        demod_params["secure"] = dict(p["secure"]) if isinstance(p["secure"], dict) else {"on": True}
    # The despreader / dechirper must be built from the same
    # code and chirp the scenario used, and the worker sizes its gate rate from
    # spread_factor (SPREAD_FSOUT). Without these a DSSS band would run the
    # SF15/seed-1 defaults whatever the plan said.
    for _k in ("spread_factor", "pn_seed", "css_sf", "css_bw", "css_os"):
        if p.get(_k) is not None:
            demod_params[_k] = p[_k]
    if p.get("code_spec"):
        demod_params["code_spec"] = dict(p["code_spec"])
    if p.get("code_spec_q"):                 # Q-rail code (qpsk / oqpsk DSSS)
        demod_params["code_spec_q"] = dict(p["code_spec_q"])
    if p.get("dsss_modem"):
        demod_params["dsss_modem"] = str(p["dsss_modem"])
    if p.get("decode_gate_db") is not None:   # decode_gate_db (channelizer_worker)
        demod_params["decode_gate_db"] = float(p["decode_gate_db"])

    # channel bandwidth: explicit band_hz, else occupied estimate
    _mod_u = str(entry.modulation or "").upper()
    if entry.band_hz:
        bw = float(entry.band_hz)
    elif deltas:
        bw = (max(deltas) - min(deltas)) + sr
    elif "DSSS" in _mod_u and sr:
        bw = 2.0 * float(p.get("spread_factor", 15) or 15) * sr   # chip main lobe
    elif _mod_u in ("CSS", "LORA", "LORA-CSS"):
        bw = float(p.get("css_bw", 125e3) or 125e3)
    else:
        bw = sr * 1.5 if sr else 100e3

    cc = ChannelConfig(
        f_center_hz=f_center, bw_hz=float(bw),
        demod_type=entry.modulation, symbol_rate=sr or 1.0,
        demod_params=demod_params, enabled=True, name=entry.name,
    )
    # carry the plan's decode flag as an attribute (ChannelConfig doesn't model it)
    try:
        cc.decode = bool(entry.decode)
    except Exception:
        pass
    # burst is None in the schema means continuous transmission: the worker
    # gates continuous channels on a periodic cadence, not a rising edge
    try:
        cc.is_continuous = (getattr(entry, "burst", None) is None)
    except Exception:
        cc.is_continuous = False
    return cc


def _attach_overlay_codes(out):
    """DSSS users that share a centre, symbol rate and code length
    are one CDMA overlay; give each the other users' codes (overlay_codes) so
    its despreader can cancel them (DSSSDemodulator._mui_cancel)."""
    groups = {}
    for cc, _e in out:
        if cc is None or "DSSS" not in str(cc.demod_type or "").upper():
            continue
        p = cc.demod_params or {}
        if not p.get("code_spec"):
            continue
        k = (int(round(float(cc.f_center_hz))), float(cc.symbol_rate or 0.0),
             int(p.get("spread_factor") or 0))
        groups.setdefault(k, []).append(cc)
    for users in groups.values():
        if len(users) < 2:
            continue
        for cc in users:
            cc.demod_params["overlay_codes"] = [
                {k: dict(o.demod_params[k]) if isinstance(o.demod_params[k], dict) else o.demod_params[k]
                 for k in ("code_spec", "code_spec_q", "dsss_modem") if o.demod_params.get(k)}
                for o in users if o is not cc]


def plan_to_watchlist(plan):
    """Return list of (ChannelConfig, entry) for all known entries with params."""
    out = []
    for e in plan.entries:
        if e.role == "known" and e.modulation:
            try:
                out.append((entry_to_channelconfig(e, plan), e))
            except Exception as ex:
                out.append((None, e))
    try:
        _attach_overlay_codes(out)                       # overlay cancellation
    except Exception:
        pass
    return out
