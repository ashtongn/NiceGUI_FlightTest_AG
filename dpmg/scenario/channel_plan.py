"""
channel_plan.py: the multi-waveform scenario/channel-plan schema shared by the
Composer (TX) and Channelizer (RX). One file describes a whole cabled test: a band
header + a list of inline, bit-level waveform entries, each with a placement and a
role. Authored by the encoder; consumed by both other tabs.

Design: the schema does not enumerate valid modulations. `modulation` is a free
string and `params` is an open bag. Whether an entry can be rendered or decoded is
decided at load time against the code registries (modulator/demodulator factories),
never by the file. So a scenario can describe a modulation you haven't implemented
yet: it stays structurally valid, transmits, and characterizes: decode fills in
later when the demodulator exists. Bits are carried explicitly (fields[].bin), so a
waveform is pinned to the bit level regardless of whether the code understands its
framing.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
import json

SCHEMA_VERSION = 1


@dataclass
class Entry:
    name: str
    modulation: str                     # free string; capability checked at load
    params: dict                        # open bag: symbol_rate, samples_per_bit, tone_deltas_hz, dibit_mapping, filter, ...
    offset_hz: float                    # placement relative to compose center
    power_db: float = 0.0
    bits: str = ""                      # concatenated fields[].bin (the framed bitstream)
    fields: list = field(default_factory=list)   # [{desc,bin,encoding}] provenance; bits is the source of truth
    role: str = "known"                 # "known" (has params: decodable) or "unknown" (watch/characterize only)
    decode: bool = False                # RX: run full demod/decode on this band
    band_hz: float = 0.0                # for unknown entries: watch width (known entries derive from params)
    frame: dict = field(default_factory=dict)   # declared trigger frame
    #   {sync: bits|hex, max_sync_errors:int, has_length:bool,
    #    payload_len_bits:int, has_crc:bool, auto_wrap:bool}
    response: Optional[dict] = None     # optional marco/polo reply recipe
    burst: Optional[dict] = None        # {"start_s","duration_s","period_s"}; None = continuous

@dataclass
class ChannelPlan:
    center_freq_hz: float
    sample_rate_hz: float
    rf_bandwidth_hz: float
    tx_radio: str = "g40"
    schema_version: int = SCHEMA_VERSION
    entries: list = field(default_factory=list)

    def to_dict(self):
        return {
            "schema_version": self.schema_version,
            "compose": {"tx_radio": self.tx_radio, "sample_rate_hz": self.sample_rate_hz,
                        "center_freq_hz": self.center_freq_hz, "rf_bandwidth_hz": self.rf_bandwidth_hz},
            "entries": [
                {k: v for k, v in {
                    "name": e.name, "modulation": e.modulation, "params": e.params,
                    "offset_hz": e.offset_hz, "power_db": e.power_db,
                    "role": e.role, "decode": e.decode, "band_hz": e.band_hz,
                    "bits": e.bits, "fields": e.fields, "response": e.response,
                    "frame": e.frame,
                    "burst": e.burst,
                }.items() if v not in (None, [], "", 0.0, {}) or k in ("offset_hz", "power_db", "decode")}
                for e in self.entries
            ],
        }

    @staticmethod
    def from_dict(d: dict) -> "ChannelPlan":
        c = d.get("compose", {})
        p = ChannelPlan(
            center_freq_hz=float(c.get("center_freq_hz", 0.0)),
            sample_rate_hz=float(c.get("sample_rate_hz", 0.0)),
            rf_bandwidth_hz=float(c.get("rf_bandwidth_hz", 0.0)),
            tx_radio=str(c.get("tx_radio", "g40")),
            schema_version=int(d.get("schema_version", SCHEMA_VERSION)),
        )
        for e in d.get("entries", []):
            p.entries.append(Entry(
                name=str(e.get("name", "")), modulation=str(e.get("modulation", "")),
                params=dict(e.get("params", {})), offset_hz=float(e.get("offset_hz", 0.0)),
                power_db=float(e.get("power_db", 0.0)), bits=str(e.get("bits", "")),
                fields=list(e.get("fields", [])), role=str(e.get("role", "known")),
                decode=bool(e.get("decode", False)), band_hz=float(e.get("band_hz", 0.0)),
                frame=dict(e.get("frame", {}) or {}),
                response=e.get("response"),
                burst=e.get("burst"),
            ))
        return p


# capability probes (against the code registries, not the schema)
def can_render(modulation: str) -> bool:
    try:
        from dpmg.modulation.modulator import get_modulator
        get_modulator(modulation, symbol_rate=9600, samples_per_bit=8)
        return True
    except Exception:
        return False

def can_decode(modulation: str) -> bool:
    try:
        from dpmg.demodulation.demodulator import get_demodulator
        get_demodulator(modulation, 96000.0, 9600.0, 10)
        return True
    except Exception:
        return False


# structural + placement + capability lint 
def lint(plan: ChannelPlan) -> dict:
    warnings, errors, per_entry = [], [], []
    usable = 0.80 * plan.sample_rate_hz
    half_plan = plan.rf_bandwidth_hz / 2.0
    half_usable = usable / 2.0
    if plan.rf_bandwidth_hz > usable:
        errors.append(f"rf_bandwidth {plan.rf_bandwidth_hz/1e6:g} MHz exceeds usable "
                      f"{usable/1e6:.2f} MHz at {plan.sample_rate_hz/1e6:g} MSps")
    occ = []
    for e in plan.entries:
        info = {"name": e.name, "modulation": e.modulation, "role": e.role,
                "renderable": None, "decodable": None, "notes": []}
        # structure
        if not e.name or not e.modulation:
            errors.append(f"entry missing name/modulation: {e}")
        # bandwidth of this entry
        sr = float(e.params.get("symbol_rate", 0) or 0)
        if e.role == "known":
            if not e.bits:
                errors.append(f"{e.name}: known entry has no bits")
            if sr <= 0:
                errors.append(f"{e.name}: known entry missing symbol_rate")
            # occupied width estimate
            deltas = e.params.get("tone_deltas_hz")
            _mu = str(e.modulation or "").upper()
            if e.band_hz:
                width = float(e.band_hz)
            elif deltas:
                width = (max(deltas) - min(deltas)) + sr
            elif "DSSS" in _mu:
                width = 2.0 * float(e.params.get("spread_factor", 15) or 15) * sr
            elif _mu in ("CSS", "LORA", "LORA-CSS"):
                width = float(e.params.get("css_bw", 125e3) or 125e3)
            else:
                width = sr * 1.5
            info["renderable"] = can_render(e.modulation)
            info["decodable"] = can_decode(e.modulation)
            if not info["renderable"]:
                info["notes"].append("no renderer -> cannot transmit")
                errors.append(f"{e.name}: modulation '{e.modulation}' has no renderer")
            if e.decode and not info["decodable"]:
                info["notes"].append("decode requested but no demodulator -> will characterize only")
                warnings.append(f"{e.name}: decode set but '{e.modulation}' not decodable yet")
        else:
            width = e.band_hz or (sr * 1.5 if sr else 0.0)
            if width <= 0:
                warnings.append(f"{e.name}: unknown entry has no band_hz; defaulting width")
                width = 100e3
        # placement fits the declared plan band and the physical usable band
        lo, hi = e.offset_hz - width / 2, e.offset_hz + width / 2
        if lo < -half_plan or hi > half_plan:
            errors.append(f"{e.name}: [{lo/1e6:+.3f},{hi/1e6:+.3f}] MHz outside plan band "
                          f"+/-{half_plan/1e6:g} MHz")
        elif lo < -half_usable or hi > half_usable:
            warnings.append(f"{e.name}: outside usable +/-{half_usable/1e6:.2f} MHz "
                            f"(inside plan band but not capturable at this rate)")
        occ.append((lo, hi, e.name))
        per_entry.append(info)
    # pairwise overlap
    occ.sort()
    for i in range(1, len(occ)):
        if occ[i][0] < occ[i-1][1]:
            warnings.append(f"overlap: {occ[i-1][2]} and {occ[i][2]}")
    return {"ok": not errors, "errors": errors, "warnings": warnings, "entries": per_entry,
            "usable_bw_hz": usable, "n_entries": len(plan.entries)}


if __name__ == "__main__":
    import sys
    for path in sys.argv[1:]:
        plan = ChannelPlan.from_dict(json.load(open(path)))
        rep = lint(plan)
        dec = sum(1 for e in rep["entries"] if e["decodable"])
        ren = sum(1 for e in rep["entries"] if e["renderable"])
        print(f"\n{path}: {rep['n_entries']} entries, ok={rep['ok']}  "
              f"renderable={ren} decodable={dec}  usable={rep['usable_bw_hz']/1e6:g} MHz")
        for e in rep["errors"][:8]:   print("  ERROR:", e)
        for w in rep["warnings"][:8]: print("  warn :", w)
