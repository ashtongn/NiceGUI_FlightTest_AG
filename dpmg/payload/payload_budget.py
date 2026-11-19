"""payload_budget.py: payload RF chain math shared by the /payload GUI page,
the service's RX-protection guard and the EIRP / link-budget workbook generator.

Everything is driven by ``payload_profiles.yaml``: one
``components`` entry per part (datasheet values with their source and a verify
flag) and one ``profiles`` entry per payload build (which HPA / LNA it carries,
cable losses, fixed pads).  Nothing here invents a number: a value the file
does not carry raises ``ProfileError`` unless the caller supplies it.

Chains (the real payload has no coupler and no power meter):

  TX:  G40 TX then cable then [fixed pad] then HPA then cable then RF switch (TX port)
       then cable then antenna
  RX:  antenna then RF switch (RX port) then cable then LNA then cable then RCDAT
       attenuator then cable then G40 RX

With that RX order the attenuator sits between the LNA and the G40, so in the TX
state it protects the G40 RX only.  The LNA sees the leakage through the switch
isolation with nothing in between; the attenuator setting does not change it.
A profile with rx_order: atten_first (Payload 2 as built) has the attenuator
directly after the switch, so the setting protects the LNA as well.
The coupler and power meter are bench characterization equipment
(payload_profiles.yaml bench_fixture), not on any payload chain.

The workbook (tools/gen_payload_workbook.py) writes the same equations as Excel
formulas; ``tests/test_payload_budget.py`` cross-checks the two.

Conventions: powers in dBm, losses positive dB, frequencies in MHz.  Tables are
``points: [[freq_mhz, value], ...]`` and are linearly interpolated in frequency,
clamped (and flagged) outside the tabulated span.
"""
from __future__ import annotations

import copy
import math
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from dpmg.paths import REPO_DIR

DEFAULT_PROFILES_NAME = "payload_profiles.yaml"
PROFILES_ENV = "DPMG_PAYLOAD_PROFILES"

K_BOLTZ_DBM = -198.6                  # 10*log10(1.380649e-23 * 1000) dBm/K/Hz
T0_K = 290.0


class ProfileError(Exception):
    """payload_profiles.yaml is missing, malformed, or lacks a needed value."""


# loading
def default_profiles_path() -> str:
    p = os.environ.get(PROFILES_ENV)
    if p:
        return p
    return os.path.join(REPO_DIR, DEFAULT_PROFILES_NAME)


def load_profiles(path: Optional[str] = None) -> dict:
    path = path or default_profiles_path()
    if not os.path.exists(path):
        raise ProfileError(f"payload profiles not found: {path}")
    import yaml
    with open(path, "r", encoding="utf-8") as f:
        try:
            data = yaml.safe_load(f)
        except Exception as e:
            raise ProfileError(f"{path}: cannot parse: {e}")
    validate(data, source=path)
    data["_source"] = path
    return data


def validate(data: Any, source: str = "profiles") -> None:
    if not isinstance(data, dict):
        raise ProfileError(f"{source}: top level must be a mapping")
    for key in ("components", "profiles"):
        if not isinstance(data.get(key), dict) or not data[key]:
            raise ProfileError(f"{source}: '{key}' must be a non-empty mapping")
    comps = data["components"]
    for pname, prof in data["profiles"].items():
        if not isinstance(prof, dict):
            raise ProfileError(f"{source}: profile {pname!r} must be a mapping")
        for role in ROLES:
            ref = prof.get(role)
            if ref is None:
                raise ProfileError(f"{source}: profile {pname!r} has no '{role}'")
            if ref not in comps:
                raise ProfileError(f"{source}: profile {pname!r} {role} -> unknown component {ref!r}")
        for role in OPTIONAL_ROLES:
            ref = prof.get(role)
            if ref is not None and ref not in comps:
                raise ProfileError(f"{source}: profile {pname!r} {role} -> unknown component {ref!r}")
        if prof.get("rx_order", "lna_first") not in RX_ORDERS:
            raise ProfileError(f"{source}: profile {pname!r} rx_order must be one of {RX_ORDERS}")
    for cname, comp in comps.items():
        if not isinstance(comp, dict):
            raise ProfileError(f"{source}: component {cname!r} must be a mapping")
        for k, leaf in comp.items():
            if isinstance(leaf, dict) and "points" in leaf:
                pts = leaf["points"]
                if not pts or any(len(p) != 2 for p in pts):
                    raise ProfileError(f"{source}: {cname}.{k}: points must be [[f, v], ...]")
                fs = [float(p[0]) for p in pts]
                if fs != sorted(fs):
                    raise ProfileError(f"{source}: {cname}.{k}: points must be sorted by frequency")
    radios = data.get("radios")
    if radios is not None:
        if not isinstance(radios, dict):
            raise ProfileError(f"{source}: 'radios' must be a mapping")
        for rname, r in radios.items():
            if not isinstance(r, dict):
                raise ProfileError(f"{source}: radio {rname!r} must be a mapping")
            if r.get("control", "atten") not in TX_CONTROLS:
                raise ProfileError(f"{source}: radio {rname!r} control must be one of {TX_CONTROLS}")
            if r.get("tx") not in comps:
                raise ProfileError(f"{source}: radio {rname!r} tx -> unknown component {r.get('tx')!r}")


# component role in a profile: what the math reads from it
ROLES = ("g40_tx", "g40_rx", "hpa", "switch", "antenna", "lna", "rx_atten")
# roles a profile may leave out: tx_combiner sits between the radio TX port(s) and
# the HPA input (a splitter/combiner used backwards); rx_splitter sits after the RX
# attenuator and LNA and feeds several G40 RX ports.  For both, insertion_loss_db is
# the loss from the common port to one other port, split loss included
OPTIONAL_ROLES = ("tx_combiner", "rx_splitter")

TX_CONTROLS = ("atten", "gain")

# receive path order after the T/R switch.  lna_first: switch, LNA, RX attenuator,
# G40 (the attenuator protects the G40 only).  atten_first: switch, RX attenuator,
# LNA, G40 (the attenuator protects the LNA as well).  With atten_first the
# cables_db keys keep their names but mean the runs in order: switch_to_lna is
# switch to attenuator, lna_to_atten attenuator to LNA, atten_to_g40 LNA to G40.
RX_ORDERS = ("lna_first", "atten_first")

CABLE_KEYS = ("g40_to_hpa", "hpa_to_switch", "switch_to_antenna",
              "switch_to_lna", "lna_to_atten", "atten_to_g40")


# leaves
def is_table(leaf: Any) -> bool:
    return isinstance(leaf, dict) and "points" in leaf


def leaf_value(leaf: Any, default: Any = None, what: str = "value") -> Optional[float]:
    """Scalar leaf ({value: x, ...} or a bare number): float | default."""
    if leaf is None:
        return default
    if isinstance(leaf, (int, float)) and not isinstance(leaf, bool):
        return float(leaf)
    if isinstance(leaf, dict):
        v = leaf.get("value")
        if v is None:
            return default
        if isinstance(v, (list, tuple)):
            raise ProfileError(f"{what}: expected a scalar, got a range {v}")
        return float(v)
    raise ProfileError(f"{what}: cannot read {leaf!r}")


def interp(points: List[List[float]], f: float) -> Tuple[float, bool]:
    """Linear interpolation; returns (value, in_range).  Clamps outside."""
    fs = [float(p[0]) for p in points]
    vs = [float(p[1]) for p in points]
    if f <= fs[0]:
        return vs[0], f >= fs[0] - 1e-9
    if f >= fs[-1]:
        return vs[-1], f <= fs[-1] + 1e-9
    for i in range(1, len(fs)):
        if f <= fs[i]:
            t = (f - fs[i - 1]) / (fs[i] - fs[i - 1])
            return vs[i - 1] + t * (vs[i] - vs[i - 1]), True
    return vs[-1], False                          # pragma: no cover


def at(leaf: Any, f_mhz: float, what: str, default: Any = None) -> Tuple[Optional[float], bool]:
    """Table or scalar leaf evaluated at f: (value, in_range)."""
    if is_table(leaf):
        return interp(leaf["points"], f_mhz)
    v = leaf_value(leaf, default=None, what=what)
    if v is None:
        if default is not None:
            return float(default), True
        raise ProfileError(f"{what}: no value in payload_profiles.yaml")
    return v, True


# profile view
@dataclass
class Profile:
    """Resolved view of one payload profile: component dicts by role + cables."""
    name: str
    raw: dict
    comps: Dict[str, dict]
    cables: Dict[str, float]
    fixed_pad_db: float
    label: str = ""
    warnings: List[str] = field(default_factory=list)
    rx_order: str = "lna_first"

    @property
    def atten_first(self) -> bool:
        return self.rx_order == "atten_first"

    def comp(self, role: str) -> dict:
        return self.comps[role]

    def tx_combiner_db(self, f_mhz: float) -> float:
        """Loss of the TX combiner at f (0 when the profile has none)."""
        if "tx_combiner" not in self.comps:
            return 0.0
        return float(self.get("tx_combiner", "insertion_loss_db", f_mhz))

    def rx_splitter_db(self, f_mhz: float) -> float:
        """Loss of the RX splitter to one G40 RX port at f (0 when the profile has none)."""
        if "rx_splitter" not in self.comps:
            return 0.0
        return float(self.get("rx_splitter", "insertion_loss_db", f_mhz))

    def tx_inline_db(self, f_mhz: float) -> float:
        """Everything between the radio TX cable and the HPA input: the fixed pad
        and the TX combiner."""
        return self.fixed_pad_db + self.tx_combiner_db(f_mhz)

    def get(self, role: str, key: str, f_mhz: Optional[float] = None,
            default: Any = None) -> Optional[float]:
        leaf = self.comps[role].get(key)
        what = f"{self.name}.{role}({self.raw.get(role)}).{key}"
        if f_mhz is None:
            if is_table(leaf):
                raise ProfileError(f"{what}: frequency-dependent, need a frequency")
            v = leaf_value(leaf, default=None, what=what)
            if v is None:
                if default is not None:
                    return float(default)
                raise ProfileError(f"{what}: no value in payload_profiles.yaml")
            return v
        v, ok = at(leaf, f_mhz, what, default=default)
        if not ok:
            msg = f"{what}: {f_mhz:g} MHz outside the tabulated span (clamped)"
            if msg not in self.warnings:
                self.warnings.append(msg)
        return v


def profile(data: dict, name: str) -> Profile:
    profs = data.get("profiles", {})
    if name not in profs:
        raise ProfileError(f"unknown payload profile {name!r} (have {sorted(profs)})")
    raw = profs[name]
    comps = {role: data["components"][raw[role]] for role in ROLES}
    for role in OPTIONAL_ROLES:
        if raw.get(role):
            comps[role] = data["components"][raw[role]]
    cab = raw.get("cables_db") or {}
    cables = {}
    for k in CABLE_KEYS:
        cables[k] = float(leaf_value(cab.get(k), default=0.0, what=f"{name}.cables_db.{k}"))
    pad = float(leaf_value(raw.get("fixed_pad_db"), default=0.0, what=f"{name}.fixed_pad_db"))
    return Profile(name=name, raw=raw, comps=comps, cables=cables, fixed_pad_db=pad,
                   label=str(raw.get("label", name)), rx_order=str(raw.get("rx_order", "lna_first")))


def profile_names(data: dict) -> List[str]:
    return list(data.get("profiles", {}).keys())


# Readable names for the part that sets a band edge.
ROLE_LABEL = {"g40_tx": "G40 TX", "g40_rx": "G40 RX", "hpa": "HPA", "switch": "switch",
              "antenna": "antenna", "lna": "LNA", "rx_atten": "RX attenuator",
              "tx_combiner": "TX combiner", "rx_splitter": "RX splitter"}


def band_limits(data: dict, prof: Profile) -> dict:
    """Operating band of a profile and which part sets each edge.

    The band is the overlap of every part on the chain, so the low edge is the
    highest low end of any part and the high edge is the lowest high end. For
    the current builds the switch (800-2700 MHz) or the antenna (700-3000 MHz)
    sets the low edge and the HPA the high edge. Returns {lo, hi, lo_role,
    hi_role, lo_by, hi_by} with the _by fields as readable names. An explicit band_mhz override reports the
    edge as set in the profile."""
    b = prof.raw.get("band_mhz") or data.get("band_mhz")
    if b:
        v = b.get("value") if isinstance(b, dict) else b
        return {"lo": float(v[0]), "hi": float(v[1]), "lo_role": None, "hi_role": None,
                "lo_by": "set in the profile", "hi_by": "set in the profile"}
    lo, hi = 0.0, 1e9
    lo_role = hi_role = None
    for role in ROLES + OPTIONAL_ROLES:
        comp = prof.comps.get(role) or {}
        r = comp.get("freq_range_mhz")
        v = r.get("value") if isinstance(r, dict) else r
        if v and v[0] is not None and v[1] is not None:
            if float(v[0]) > lo:
                lo, lo_role = float(v[0]), role
            if float(v[1]) < hi:
                hi, hi_role = float(v[1]), role
    return {"lo": lo, "hi": hi, "lo_role": lo_role, "hi_role": hi_role,
            "lo_by": ROLE_LABEL.get(lo_role, lo_role or "?"),
            "hi_by": ROLE_LABEL.get(hi_role, hi_role or "?")}


def band_mhz(data: dict, prof: Profile) -> Tuple[float, float]:
    """Operating band of a profile: the explicit ``band_mhz`` or the overlap of
    every component's freq_range_mhz."""
    z = band_limits(data, prof)
    return z["lo"], z["hi"]


# unit helpers
def dbm_to_mw(p: float) -> float:
    return 10.0 ** (p / 10.0)


def mw_to_dbm(p: float) -> float:
    return 10.0 * math.log10(max(p, 1e-30))


def dbm_to_w(p: float) -> float:
    return dbm_to_mw(p) / 1000.0


def ceil_step(x: float, step: float) -> float:
    if x <= 0:
        return 0.0
    return round(math.ceil(x / step - 1e-9) * step, 4)


# HPA model
OneDB = 10 ** 0.1


def rapp_s_from_p1db(p1db_dbm: float, psat_dbm: float) -> float:
    """Power-domain Rapp smoothness s such that the 1 dB compression point of
    Pout = x / (1 + (x/Psat)^s)^(1/s) sits at P1dB (both output-referred).
    Needs Psat > P1dB; returns s (larger = harder limiter)."""
    r = dbm_to_mw(p1db_dbm) / dbm_to_mw(psat_dbm)       # P1dB/Psat < 1
    if not (0 < r < 1):
        raise ProfileError(f"HPA: P1dB {p1db_dbm} dBm must be below Psat {psat_dbm} dBm")
    target = r * OneDB                                   # x/Psat at the 1 dB point

    def g(s):                                            # x/Psat at 1 dB compression for s
        return (OneDB ** s - 1.0) ** (1.0 / s)
    lo, hi = 0.05, 200.0
    if not (g(lo) <= target <= g(hi)):
        # g is increasing in s; clamp to the reachable range
        return lo if target < g(lo) else hi
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if g(mid) < target:
            lo = mid
        else:
            hi = mid
    return round(0.5 * (lo + hi), 4)


def rapp_out_dbm(lin_out_dbm: float, psat_dbm: float, s: float) -> float:
    x = dbm_to_mw(lin_out_dbm)
    ps = dbm_to_mw(psat_dbm)
    return mw_to_dbm(x / (1.0 + (x / ps) ** s) ** (1.0 / s))


def hpa_input_for_compression(gain_db: float, psat_dbm: float, s: float, comp_db: float) -> float:
    """HPA input (dBm) at which the Rapp model is ``comp_db`` below linear:
    x/Psat = (10^(comp/10 * s) - 1)^(1/s), input = Psat + 10log10(x/Psat) - gain."""
    r = (10 ** (comp_db / 10.0 * s) - 1.0) ** (1.0 / s)
    return psat_dbm + 10 * math.log10(r) - gain_db


def hpa_region(compression_db: float) -> str:
    """Saturation region label shared by the page and the workbook."""
    if compression_db < 0.1:
        return "linear"
    if compression_db < 1.0:
        return "compressing"
    if compression_db < 3.0:
        return "past P1dB"
    return "SATURATED"


def hpa_params(prof: Profile, f_mhz: float) -> dict:
    """Gain (at f), P1dB, Psat, Rapp s, max input: from the HPA component."""
    gain = prof.get("hpa", "gain_db", f_mhz)
    psat = prof.get("hpa", "psat_dbm", f_mhz)
    p1db = prof.get("hpa", "p1db_dbm", f_mhz, default=None) \
        if prof.comp("hpa").get("p1db_dbm") is not None else None
    s_leaf = prof.comp("hpa").get("rapp_s")
    if s_leaf is not None:
        s = float(leaf_value(s_leaf))
    elif p1db is not None and p1db < psat:
        s = rapp_s_from_p1db(p1db, psat)
    else:
        s = 2.0                                          # documented fallback: soft knee
    maxin = prof.get("hpa", "max_input_dbm", f_mhz)
    off = prof.get("hpa", "off_gain_db", f_mhz, default=None) \
        if prof.comp("hpa").get("off_gain_db") is not None else None
    return {"gain_db": gain, "psat_dbm": psat, "p1db_dbm": p1db, "rapp_s": s,
            "max_input_dbm": maxin, "off_gain_db": off}


# TX / EIRP
def g40_tx_out_dbm(prof: Profile, f_mhz: float, tx_atten_db: float, backoff_db: float) -> float:
    pmax = prof.get("g40_tx", "max_output_dbm", f_mhz)
    return pmax - float(tx_atten_db) - float(backoff_db)


def eirp_chain(prof: Profile, f_mhz: float, tx_atten_db: float, backoff_db: float = 0.0,
               hpa_on: bool = True, hpa_out_override_dbm: Optional[float] = None) -> dict:
    """Stage-by-stage TX chain at one operating point.  ``hpa_out_override_dbm``
    forces the HPA output (used for the worst-case Psat protection numbers)."""
    c = prof.cables
    hp = hpa_params(prof, f_mhz)
    g40 = g40_tx_out_dbm(prof, f_mhz, tx_atten_db, backoff_db)
    comb = prof.tx_combiner_db(f_mhz)
    inline = prof.fixed_pad_db + comb
    hpa_in = g40 - c["g40_to_hpa"] - inline
    lin = hpa_in + hp["gain_db"]
    if hpa_out_override_dbm is not None:
        hpa_out = float(hpa_out_override_dbm)
    elif hpa_on:
        hpa_out = rapp_out_dbm(lin, hp["psat_dbm"], hp["rapp_s"])
    else:
        if hp["off_gain_db"] is None:
            raise ProfileError(f"{prof.name}: HPA off_gain_db unknown, cannot compute disabled leakage")
        hpa_out = hpa_in + hp["off_gain_db"]
    compression = (lin - hpa_out) if (hpa_on and hpa_out_override_dbm is None) else 0.0
    switch_in = hpa_out - c["hpa_to_switch"]
    sw_il = prof.get("switch", "insertion_loss_db", f_mhz)
    ant_in = switch_in - sw_il - c["switch_to_antenna"]
    gant = prof.get("antenna", "gain_dbi", f_mhz)
    eirp = ant_in + gant
    flags = []

    def lim(role, key, value, label):
        leaf = prof.comp(role).get(key)
        if leaf is None:
            return None
        v = prof.get(role, key, f_mhz) if is_table(leaf) else leaf_value(leaf)
        if v is None:
            return None
        if value > v:
            flags.append(f"{label} {value:.1f} dBm > limit {v:.1f} dBm")
        return v
    # saturation: where the HPA starts to compress, and the TX attenuation that gets there
    in_p1 = hpa_input_for_compression(hp["gain_db"], hp["psat_dbm"], hp["rapp_s"], 1.0)
    in_p3 = hpa_input_for_compression(hp["gain_db"], hp["psat_dbm"], hp["rapp_s"], 3.0)
    base = g40 + float(tx_atten_db)                      # G40 output at 0 dB TX attenuation
    att_p1 = base - c["g40_to_hpa"] - inline - in_p1
    att_p3 = base - c["g40_to_hpa"] - inline - in_p3
    region = hpa_region(compression) if hpa_on and hpa_out_override_dbm is None else "off"
    notes = []
    if region in ("past P1dB", "SATURATED"):
        notes.append(f"HPA {compression:.1f} dB into compression ({region}); output {hp['psat_dbm'] - hpa_out:.1f} dB "
                     f"below Psat -- P1dB is reached at G40 TX atten {att_p1:.1f} dB")
    hpa_max_in = hp["max_input_dbm"]
    if hpa_in > hpa_max_in:
        flags.append(f"HPA input {hpa_in:.1f} dBm > max input {hpa_max_in:.1f} dBm")
    switch_max = lim("switch", "power_rating_dbm", switch_in, "switch TX input")
    # a switch with no CW rating on file: only its pulsed rating can be checked here
    # (live_summary warns that the CW rating is missing)
    switch_pulsed = None if switch_max is not None else \
        lim("switch", "pulsed_rating_dbm", switch_in, "switch TX input, pulsed rating")
    ant_max = lim("antenna", "max_input_dbm", ant_in, "antenna input")
    return {
        "profile": prof.name, "freq_mhz": f_mhz, "tx_atten_db": float(tx_atten_db),
        "backoff_db": float(backoff_db), "hpa_on": bool(hpa_on),
        "g40_out_dbm": g40, "tx_combiner_db": (comb if "tx_combiner" in prof.comps else None),
        "fixed_pad_db": prof.fixed_pad_db,
        "hpa_in_dbm": hpa_in, "hpa_gain_db": hp["gain_db"],
        "hpa_lin_out_dbm": lin, "hpa_out_dbm": hpa_out, "hpa_compression_db": compression,
        "hpa_psat_dbm": hp["psat_dbm"], "hpa_p1db_dbm": hp["p1db_dbm"], "rapp_s": hp["rapp_s"],
        "hpa_max_input_dbm": hpa_max_in,
        "switch_in_dbm": switch_in, "switch_il_db": sw_il, "switch_max_dbm": switch_max,
        "switch_pulsed_max_dbm": switch_pulsed,
        "ant_in_dbm": ant_in, "ant_max_dbm": ant_max, "ant_gain_dbi": gant,
        "eirp_dbm": eirp, "eirp_dbw": eirp - 30.0, "eirp_w": dbm_to_w(eirp),
        "hpa_out_w": dbm_to_w(hpa_out),
        "hpa_in_p1db_dbm": in_p1, "hpa_in_3db_dbm": in_p3, "hpa_backoff_p1db_db": in_p1 - hpa_in,
        "hpa_below_psat_db": hp["psat_dbm"] - hpa_out, "hpa_region": region,
        "tx_atten_p1db_db": att_p1, "tx_atten_3db_db": att_p3,
        "saturation_notes": notes,
        "flags": flags, "warnings": list(prof.warnings),
    }


def eirp_sweep(prof: Profile, f_mhz: float, backoff_db: float = 0.0, hpa_on: bool = True,
               atten_values: Optional[Iterable[float]] = None) -> List[dict]:
    vals = list(atten_values) if atten_values is not None else [x * 1.0 for x in range(0, 42)]
    return [eirp_chain(prof, f_mhz, a, backoff_db, hpa_on) for a in vals]


# other TX radios
# The profile's g40_tx part is the default radio.  payload_profiles.yaml radios:
# can name another TX part (the B200).  A gain-controlled radio maps onto the
# same chain math: its output at gain g is max_output_dbm - (gain_max - g), which
# is the G40 formula with "attenuation" = gain_max - g.  So these build a profile
# copy with that part in the g40_tx role and convert the control value; the G40
# functions above are not touched.
@dataclass
class RadioTx:
    key: str
    tx: str                    # component name in components:
    control: str               # atten | gain
    min_db: float
    max_db: float
    step_db: float
    label: str
    bench_max_gain_db: Optional[float] = None


def radio_names(data: dict) -> List[str]:
    return list((data.get("radios") or {}).keys()) or ["g40"]


def radio_tx(data: dict, key: str = "g40") -> RadioTx:
    """The TX radio entry.  A file without radios: has the G40 only, built from
    the g40_tx part."""
    radios = data.get("radios") or {}
    if key not in radios:
        if key == "g40":
            comp = (data.get("components") or {}).get("g40_tx") or {}
            mx = float(leaf_value(comp.get("tx_atten_max_db"), default=41.75))
            return RadioTx("g40", "g40_tx", "atten", 0.0, mx, 0.25, "G40 TX atten")
        raise ProfileError(f"unknown TX radio {key!r} (have {radio_names(data)})")
    r = radios[key]
    comp = data["components"][r["tx"]]
    control = r.get("control", "atten")
    mx_leaf = comp.get("tx_gain_max_db" if control == "gain" else "tx_atten_max_db")
    mx = float(r["max_db"]) if r.get("max_db") is not None else \
        float(leaf_value(mx_leaf, default=41.75 if control == "atten" else None, what=f"{r['tx']}.max"))
    bench = leaf_value(comp.get("bench_max_gain_db"), default=None) if control == "gain" else None
    return RadioTx(key, r["tx"], control, float(r.get("min_db", 0.0)), mx,
                   float(r.get("step_db", 0.25)), str(r.get("label", key)),
                   None if bench is None else float(bench))


def radio_profile(data: dict, prof: Profile, rt: RadioTx) -> Profile:
    """The profile with the radio's TX part in the g40_tx role (the same object
    for the G40, so its numbers are exactly the profile's, whichever G40 part the
    profile names)."""
    if rt.key == "g40" or rt.tx == prof.raw.get("g40_tx"):
        return prof
    comps = dict(prof.comps)
    comps["g40_tx"] = data["components"][rt.tx]
    raw = dict(prof.raw, g40_tx=rt.tx)
    return Profile(name=prof.name, raw=raw, comps=comps, cables=dict(prof.cables),
                   fixed_pad_db=prof.fixed_pad_db, label=prof.label, rx_order=prof.rx_order)


def tx_atten_equiv(rt: RadioTx, setting_db: float) -> float:
    """The control value as the attenuation below full output the chain math uses."""
    v = float(setting_db)
    return v if rt.control == "atten" else rt.max_db - v


def eirp_chain_radio(data: dict, prof: Profile, rt: RadioTx, f_mhz: float, setting_db: float,
                     backoff_db: float = 0.0, hpa_on: bool = True) -> dict:
    """eirp_chain for any TX radio.  ``setting_db`` is the radio's own control
    (attenuation or gain).  A gain radio also gets tx_gain_db and the gains at the
    HPA P1dB / 3 dB points (tx_gain_p1db_db, tx_gain_3db_db), and its notes name
    the gain.  tx_out_dbm is the radio's output (g40_out_dbm for the G40)."""
    rp = radio_profile(data, prof, rt)
    e = eirp_chain(rp, f_mhz, tx_atten_equiv(rt, setting_db), backoff_db, hpa_on)
    e["tx_radio"], e["tx_control"], e["tx_out_dbm"] = rt.key, rt.control, e["g40_out_dbm"]
    if rt.control == "gain":
        g1 = rt.max_db - e["tx_atten_p1db_db"]
        g3 = rt.max_db - e["tx_atten_3db_db"]
        e["tx_gain_db"] = float(setting_db)
        e["tx_gain_p1db_db"], e["tx_gain_3db_db"] = g1, g3
        e["saturation_notes"] = [n.replace(f"G40 TX atten {e['tx_atten_p1db_db']:.1f} dB",
                                           f"{rt.label} {g1:.1f} dB")
                                 for n in e["saturation_notes"]]
    prof.warnings.extend(w for w in rp.warnings if w not in prof.warnings)
    return e


def max_safe_tx_gain(data: dict, prof: Profile, rt: RadioTx, load_power_dbm: float,
                     f_mhz: Optional[float] = None, margin_db: Optional[float] = None,
                     point: str = "antenna", backoff_db: float = 0.0) -> dict:
    """Gain-mode counterpart of min_safe_tx_atten: the largest TX gain that keeps
    the power delivered to a load rated ``load_power_dbm`` under its rating, worst
    case over the band.  ``achievable`` false = even the minimum gain is too much."""
    if rt.control != "gain":
        raise ProfileError(f"{rt.key}: max_safe_tx_gain is for a gain-controlled radio")
    rp = radio_profile(data, prof, rt)
    r = min_safe_tx_atten(data, rp, load_power_dbm, f_mhz=f_mhz, margin_db=margin_db, point=point,
                          backoff_db=backoff_db, atten_max_db=rt.max_db - rt.min_db, step_db=rt.step_db)
    out = {k: v for k, v in r.items() if k not in ("min_tx_atten_db", "atten_max_db", "rows")}
    out["max_tx_gain_db"] = rt.max_db - r["min_tx_atten_db"]
    out["radio"] = rt.key
    return out


# load protection
# The bench keys into a dummy load or pad where the antenna would go, and a small
# pad (the 2 W / +33 dBm ones) is good only for G40-level power, not for the HPA's
# saturated output.  These turn a load power rating into the smallest G40 TX
# attenuation that keeps the power delivered to the load under its rating, so the
# guard can refuse to key below it (and refuse outright when no TX attenuation in
# range is enough, which is the HPA-into-a-small-pad case).

LOAD_POINTS = ("antenna", "switch", "hpa")


def tx_delivered_dbm(e: dict, point: str = "antenna") -> float:
    """Power delivered to a load at ``point`` in a TX chain result from eirp_chain.
    antenna = the antenna port (load replaces the antenna, the real bench case);
    switch = the switch TX-port output; hpa = the HPA output (most conservative)."""
    if point == "hpa":
        return e["hpa_out_dbm"]
    if point == "switch":
        return e["switch_in_dbm"] - e["switch_il_db"]     # out of the switch TX port
    return e["ant_in_dbm"]                                 # antenna port


def min_tx_atten_at(prof: Profile, f_mhz: float, load_power_dbm: float, margin_db: float,
                    point: str = "antenna", backoff_db: float = 0.0,
                    atten_max_db: float = 41.75, step_db: float = 0.25) -> dict:
    """Smallest G40 TX attenuation at one frequency that keeps the power delivered
    to the load at or below ``load_power_dbm`` minus ``margin_db``, with the HPA
    keyed at full drive (backoff defaults to 0).  Delivered power falls as the TX
    attenuation rises, so this walks up from 0 dB until it is under the target.
    ``achievable`` is false when even ``atten_max_db`` is not enough."""
    target = float(load_power_dbm) - float(margin_db)
    steps = int(round(float(atten_max_db) / float(step_db)))
    chosen = None
    for i in range(steps + 1):
        a = round(i * float(step_db), 4)
        e = eirp_chain(prof, f_mhz, a, backoff_db, hpa_on=True)
        if tx_delivered_dbm(e, point) <= target:
            chosen = a
            break
    at0 = tx_delivered_dbm(eirp_chain(prof, f_mhz, 0.0, backoff_db, hpa_on=True), point)
    atmax = tx_delivered_dbm(eirp_chain(prof, f_mhz, float(atten_max_db), backoff_db, hpa_on=True), point)
    return {"freq_mhz": f_mhz, "point": point, "load_power_dbm": float(load_power_dbm),
            "target_dbm": target, "margin_db": float(margin_db),
            "min_tx_atten_db": (chosen if chosen is not None else float(atten_max_db)),
            "achievable": chosen is not None,
            "delivered_at0_dbm": at0, "delivered_atmax_dbm": atmax}


def min_safe_tx_atten(data: dict, prof: Profile, load_power_dbm: float,
                      f_mhz: Optional[float] = None, margin_db: Optional[float] = None,
                      point: str = "antenna", backoff_db: float = 0.0,
                      atten_max_db: float = 41.75, step_db: float = 0.25) -> dict:
    """Worst case over the band (or at ``f_mhz``) of min_tx_atten_at: the TX
    attenuation the guard enforces before keying the HPA into a load rated
    ``load_power_dbm``.  ``achievable`` is false if any checked frequency cannot be
    brought under the rating within ``atten_max_db``: the guard then refuses to
    key at all (use a higher-power load)."""
    margin = protect_margin_db(data) if margin_db is None else float(margin_db)
    if point not in LOAD_POINTS:
        raise ProfileError(f"load point {point!r} unknown (have {list(LOAD_POINTS)})")
    freqs = [float(f_mhz)] if f_mhz is not None else protection_freqs(data, prof)
    rows = [min_tx_atten_at(prof, f, load_power_dbm, margin, point, backoff_db,
                            atten_max_db, step_db) for f in freqs]
    worst = max(rows, key=lambda r: r["min_tx_atten_db"])
    achievable = all(r["achievable"] for r in rows)
    return {"profile": prof.name, "load_power_dbm": float(load_power_dbm), "point": point,
            "min_tx_atten_db": worst["min_tx_atten_db"], "worst_freq_mhz": worst["freq_mhz"],
            "margin_db": margin, "achievable": achievable, "atten_max_db": float(atten_max_db),
            "unprotected_mhz": [r["freq_mhz"] for r in rows if not r["achievable"]],
            "rows": rows}


def min_lna_tx_atten_at(prof: Profile, f_mhz: float, margin_db: float,
                        atten_max_db: float = 41.75, step_db: float = 0.25) -> dict:
    """Smallest G40 TX attenuation at one frequency that keeps the leak through the
    switch isolation at or below the LNA max input minus ``margin_db``, with the
    HPA keyed at full drive.  This is the only protection the LNA has in the TX
    state when the HPA at Psat would leak more than that: the RX attenuator sits
    after the LNA."""
    target = prof.get("lna", "max_input_dbm", f_mhz) - float(margin_db)
    steps = int(round(float(atten_max_db) / float(step_db)))
    chosen = None
    for i in range(steps + 1):
        a = round(i * float(step_db), 4)
        e = eirp_chain(prof, f_mhz, a, 0.0, hpa_on=True)
        if rx_leak_chain(prof, f_mhz, e["switch_in_dbm"], 0.0)["lna_in_dbm"] <= target:
            chosen = a
            break
    return {"freq_mhz": f_mhz, "target_dbm": target,
            "min_tx_atten_db": (chosen if chosen is not None else float(atten_max_db)),
            "achievable": chosen is not None}


def min_lna_safe_tx_atten(data: dict, prof: Profile, margin_db: Optional[float] = None,
                          atten_max_db: float = 41.75, step_db: float = 0.25) -> dict:
    """Worst case over the band of min_lna_tx_atten_at: the TX attenuation floor
    the guard enforces when the switch isolation alone cannot protect the LNA from
    the HPA at Psat.  ``needed`` is false (floor 0) when it can."""
    margin = protect_margin_db(data) if margin_db is None else float(margin_db)
    if prof.atten_first:
        # the RX attenuator in front of the LNA protects it (the guard holds it at
        # min_safe_rx_atten before keying), so no TX attenuation floor is needed
        return {"profile": prof.name, "needed": False, "min_tx_atten_db": 0.0,
                "worst_freq_mhz": None, "margin_db": margin, "achievable": True, "rows": []}
    freqs = protection_freqs(data, prof)
    needed = [f for f in freqs
              if rx_leak_chain(prof, f, worst_case_switch_in_dbm(prof, f), 0.0)["lna_in_dbm"]
              > prof.get("lna", "max_input_dbm", f) - margin]
    if not needed:
        return {"profile": prof.name, "needed": False, "min_tx_atten_db": 0.0,
                "worst_freq_mhz": None, "margin_db": margin, "achievable": True, "rows": []}
    rows = [min_lna_tx_atten_at(prof, f, margin, atten_max_db, step_db) for f in needed]
    worst = max(rows, key=lambda r: r["min_tx_atten_db"])
    return {"profile": prof.name, "needed": True, "min_tx_atten_db": worst["min_tx_atten_db"],
            "worst_freq_mhz": worst["freq_mhz"], "margin_db": margin,
            "achievable": all(r["achievable"] for r in rows), "rows": rows}


# RX protection
def rx_leak_chain(prof: Profile, f_mhz: float, switch_in_dbm: float, rx_atten_db: float) -> dict:
    """TX state: power leaking from the switch TX port to its RX port (through the
    isolation), then through the LNA (linear, no compression, the conservative
    choice), the RX attenuator and on to the G40 RX input.  lna_first: the
    attenuator sits after the LNA, so raising it lowers the G40 input but not the
    LNA input.  atten_first: the attenuator comes first and lowers both."""
    c = prof.cables
    iso = prof.get("switch", "isolation_db", f_mhz)
    il = prof.get("rx_atten", "insertion_loss_db", f_mhz)
    leak_switch_rx = switch_in_dbm - iso                     # at the switch RX port
    g = prof.get("lna", "gain_db", f_mhz)
    split = prof.rx_splitter_db(f_mhz)
    if prof.atten_first:
        att_in = leak_switch_rx - c["switch_to_lna"]         # switch to attenuator run
        lna_in = att_in - il - float(rx_atten_db) - c["lna_to_atten"]
        lna_out = lna_in + g
        g40_in = lna_out - split - c["atten_to_g40"]         # LNA to G40 run
    else:
        lna_in = leak_switch_rx - c["switch_to_lna"]         # at the LNA input
        lna_out = lna_in + g
        att_in = lna_out - c["lna_to_atten"]                 # at the RX attenuator input
        g40_in = att_in - il - float(rx_atten_db) - split - c["atten_to_g40"]
    lna_max = prof.get("lna", "max_input_dbm", f_mhz)
    g40_max = prof.get("g40_rx", "max_safe_input_dbm", f_mhz)
    am = prof.comp("rx_atten").get("max_input_dbm")
    att_max = None if am is None else prof.get("rx_atten", "max_input_dbm", f_mhz)
    return {"switch_isolation_db": iso, "leak_switch_rx_dbm": leak_switch_rx,
            "atten_in_dbm": att_in, "atten_il_db": il, "rx_atten_db": float(rx_atten_db),
            "rx_splitter_db": (split if "rx_splitter" in prof.comps else None),
            "lna_in_dbm": lna_in, "lna_gain_db": g, "lna_out_dbm": lna_out,
            "g40_in_dbm": g40_in, "lna_max_input_dbm": lna_max, "g40_max_safe_dbm": g40_max,
            "atten_max_input_dbm": att_max,
            "atten_ok": att_max is None or att_in <= att_max,
            "lna_p1db_out_dbm": _opt(prof, "lna", "p1db_out_dbm", f_mhz),
            "g40_full_scale_dbm": _opt(prof, "g40_rx", "max_operating_input_dbm", f_mhz),
            "lna_ok": lna_in <= lna_max, "g40_ok": g40_in <= g40_max}


def _opt(prof: Profile, role: str, key: str, f_mhz: float) -> Optional[float]:
    """Optional leaf (None when the file does not carry it)."""
    leaf = prof.comp(role).get(key)
    if leaf is None or (isinstance(leaf, dict) and "points" not in leaf and leaf.get("value") is None):
        return None
    return prof.get(role, key, f_mhz)


def protect_margin_db(data: dict) -> float:
    p = data.get("protection") or {}
    return float(leaf_value(p.get("margin_db"), default=10.0, what="protection.margin_db"))


def min_rx_atten_at(prof: Profile, f_mhz: float, switch_in_dbm: float, margin_db: float,
                    step_db: Optional[float] = None) -> dict:
    """Smallest RX attenuator setting that keeps the G40 safe at a given switch
    TX-port power.  The leak at the switch RX port is the HPA output minus the
    switch guaranteed-minimum isolation minus the cable to the switch (folded into
    ``switch_in_dbm``).  The attenuator sits after the LNA, so only the G40 limit
    is relieved by the setting:

      G40 safe:  rx_atten >= (G40 input at 0 dB) - (G40 max safe - margin)

    The LNA input does not depend on the setting.  ``need_lna_db`` is how far the
    leak sits over the LNA max input (positive = LNA over its max whatever the
    setting, which only more switch isolation or a limiter can fix);
    ``lna_ok`` reports it.

    With rx_order atten_first the attenuator is in front of the LNA, so the
    setting protects the LNA too and the larger of the two needs wins:

      LNA safe:  rx_atten >= (LNA input at 0 dB) - (LNA max input - margin)

    ``need_lna_db`` then means the LNA excess at the 0 dB setting."""
    z = rx_leak_chain(prof, f_mhz, switch_in_dbm, 0.0)       # leak at 0 dB setting
    need_lna = z["lna_in_dbm"] - z["lna_max_input_dbm"]
    need_g40 = z["g40_in_dbm"] - (z["g40_max_safe_dbm"] - margin_db)
    binding = "G40"
    need = need_g40
    if prof.atten_first:
        need_lna_m = z["lna_in_dbm"] - (z["lna_max_input_dbm"] - margin_db)
        if need_lna_m > need:
            need, binding = need_lna_m, "LNA"
    step = step_db or float(leaf_value(prof.comp("rx_atten").get("step_db"), default=0.05))
    v = ceil_step(need, step)
    atten_max = float(leaf_value(prof.comp("rx_atten").get("max_db"), default=120.0))
    zc = rx_leak_chain(prof, f_mhz, switch_in_dbm, v)        # at the chosen setting
    return {"freq_mhz": f_mhz, "switch_in_dbm": switch_in_dbm, "min_rx_atten_db": v,
            "achievable": v <= atten_max, "atten_max_db": atten_max, "binding": binding,
            "need_lna_db": need_lna, "need_g40_db": need_g40,
            "lna_in_at0_dbm": z["lna_in_dbm"], "lna_max_input_dbm": z["lna_max_input_dbm"],
            "lna_ok": zc["lna_ok"], "g40_ok": zc["g40_ok"], "margin_db": margin_db}


def protection_freqs(data: dict, prof: Profile) -> List[float]:
    """Frequencies checked for the worst case: the band edges plus every table
    point inside the band of the components on the leak path."""
    lo, hi = band_mhz(data, prof)
    fs = {lo, hi}
    # Both leak-path constraints are frequency dependent: the LNA max input is a
    # table (very low at UHF), so its breakpoints matter as much as the isolation.
    for role, key in (("switch", "isolation_db"), ("lna", "gain_db"), ("lna", "max_input_dbm"),
                      ("rx_atten", "insertion_loss_db"), ("hpa", "psat_dbm"),
                      ("g40_rx", "max_safe_input_dbm")):
        leaf = prof.comp(role).get(key)
        if is_table(leaf):
            fs.update(float(p[0]) for p in leaf["points"] if lo <= float(p[0]) <= hi)
    return sorted(fs)


def worst_case_switch_in_dbm(prof: Profile, f_mhz: float) -> float:
    """Switch TX-port power with the HPA at Psat (any drive, any TX attenuation).
    There is no coupler: the HPA cable runs straight to the switch."""
    psat = prof.get("hpa", "psat_dbm", f_mhz)
    return psat - prof.cables["hpa_to_switch"]


def min_safe_rx_atten(data: dict, prof: Profile, f_mhz: Optional[float] = None,
                      margin_db: Optional[float] = None) -> dict:
    """The service's keying threshold.  Worst case = HPA at Psat; over the whole
    band unless ``f_mhz`` is given.  An explicit ``protection.min_rx_atten_db``
    (or per-profile ``min_rx_atten_db``) acts as a floor."""
    margin = protect_margin_db(data) if margin_db is None else float(margin_db)
    freqs = [float(f_mhz)] if f_mhz is not None else protection_freqs(data, prof)
    rows = [min_rx_atten_at(prof, f, worst_case_switch_in_dbm(prof, f), margin) for f in freqs]
    worst = max(rows, key=lambda r: r["min_rx_atten_db"])
    floor = leaf_value(prof.raw.get("min_rx_atten_db"), default=None) \
        if prof.raw.get("min_rx_atten_db") is not None else \
        leaf_value((data.get("protection") or {}).get("min_rx_atten_db"), default=None)
    value = worst["min_rx_atten_db"]
    if floor is not None:
        value = max(value, float(floor))
    atten_max = worst["atten_max_db"]
    # lna_first: the attenuator is after the LNA and cannot help it, so the LNA is
    # unprotected wherever the leak through the switch isolation alone is over its
    # max input.  atten_first: only where even the full attenuator range is not
    # enough.
    if prof.atten_first:
        lna_bad = [r for r in rows if r["need_lna_db"] > r["atten_max_db"]]
    else:
        lna_bad = [r for r in rows if r["need_lna_db"] > 0]
    return {"profile": prof.name, "min_rx_atten_db": value, "computed_db": worst["min_rx_atten_db"],
            "floor_db": floor, "worst_freq_mhz": worst["freq_mhz"], "margin_db": margin,
            "binding": worst["binding"],
            "achievable": value <= atten_max, "atten_max_db": atten_max,
            "lna_unprotected_mhz": [r["freq_mhz"] for r in lna_bad],
            "lna_worst_in_dbm": max(r["lna_in_at0_dbm"] for r in rows),
            "lna_max_input_dbm": worst["lna_max_input_dbm"], "rows": rows}


def rx_protection_table(prof: Profile, f_mhz: float, switch_in_dbm: float,
                        atten_values: Iterable[float]) -> List[dict]:
    return [rx_leak_chain(prof, f_mhz, switch_in_dbm, a) for a in atten_values]


# bench loopback
def bench_fixture_coupler(data: dict) -> Optional[dict]:
    """The directional coupler component used for the bench power-meter reading
    (payload_profiles.yaml bench_fixture), or None when the file does not list it."""
    bf = data.get("bench_fixture") or {}
    name = bf.get("coupler")
    return (data.get("components") or {}).get(name) if name else None


def bench_loopback_expected(data: dict, prof: Profile, f_mhz: float, tx_atten_db: float = 40.0,
                            backoff_db: float = 6.0, pad_db: float = 20.0) -> dict:
    """Expected levels for the safe bench loopback checkout at one frequency.

    The bench setup: antenna removed, the directional coupler and power meter
    inline for a per-frequency output-power reading, then a straight pad loop
    from the coupler mainline into the RX side.  Defaults are the safe bench
    loopback (40 dB TX attenuation, 0.5 of full scale ~ 6 dB backoff, 20 dB pad).
    Returns the HPA output, the expected power-meter reading (HPA output less the
    coupler coupling at this frequency) and the level at the loopback point (HPA
    output less the coupler mainline loss and the pad)."""
    e = eirp_chain(prof, f_mhz, tx_atten_db, backoff_db, hpa_on=True)
    hpa_out = e["hpa_out_dbm"]
    cpl = bench_fixture_coupler(data)
    coupling = mainline = None
    if cpl is not None:
        cl = cpl.get("coupling_db")
        if cl is not None:
            coupling = interp(cl["points"], f_mhz)[0] if is_table(cl) else leaf_value(cl)
        ml = cpl.get("mainline_loss_db")
        if ml is not None:
            mainline = interp(ml["points"], f_mhz)[0] if is_table(ml) else leaf_value(ml)
    meter_dbm = None if coupling is None else hpa_out - coupling
    rx_level_dbm = hpa_out - (mainline or 0.0) - float(pad_db)
    return {"freq_mhz": f_mhz, "tx_atten_db": float(tx_atten_db), "backoff_db": float(backoff_db),
            "pad_db": float(pad_db), "hpa_out_dbm": hpa_out, "coupling_db": coupling,
            "mainline_loss_db": mainline, "meter_dbm": meter_dbm, "rx_level_dbm": rx_level_dbm}


# RX link budget
def fspl_db(range_km: float, f_mhz: float) -> float:
    return 20 * math.log10(max(range_km, 1e-9)) + 20 * math.log10(f_mhz) + 32.4478


def rx_cascade(prof: Profile, f_mhz: float, rx_atten_db: float) -> List[dict]:
    """Receive cascade from the antenna port, each stage (name, gain dB, NF dB).
    Passive stages: gain = -loss, NF = loss (at 290 K)."""
    c = prof.cables
    il = prof.get("rx_atten", "insertion_loss_db", f_mhz)
    sw_il = prof.get("switch", "insertion_loss_db", f_mhz)
    lna = ("LNA", prof.get("lna", "gain_db", f_mhz), prof.get("lna", "nf_db", f_mhz))
    att = ("RX attenuator", -(il + float(rx_atten_db)), il + float(rx_atten_db))
    split = []
    if "rx_splitter" in prof.comps:
        sp = prof.rx_splitter_db(f_mhz)
        split = [("RX splitter", -sp, sp)]
    st = [("cable antenna->switch", -c["switch_to_antenna"], c["switch_to_antenna"]),
          ("switch RX path", -sw_il, sw_il)]
    if prof.atten_first:
        st += [("cable switch->atten", -c["switch_to_lna"], c["switch_to_lna"]), att,
               ("cable atten->LNA", -c["lna_to_atten"], c["lna_to_atten"]), lna] + split + [
               ("cable LNA->G40", -c["atten_to_g40"], c["atten_to_g40"])]
    else:
        st += [("cable switch->LNA", -c["switch_to_lna"], c["switch_to_lna"]), lna,
               ("cable LNA->atten", -c["lna_to_atten"], c["lna_to_atten"]), att] + split + [
               ("cable atten->G40", -c["atten_to_g40"], c["atten_to_g40"])]
    st.append(("G40 RX", 0.0, prof.get("g40_rx", "noise_figure_db", f_mhz)))
    return [{"stage": n, "gain_db": g, "nf_db": nf} for n, g, nf in st]


def friis_nf_db(stages: List[dict]) -> float:
    f_tot, g_acc = 0.0, 1.0
    for i, s in enumerate(stages):
        f = 10 ** (s["nf_db"] / 10.0)
        f_tot += f if i == 0 else (f - 1.0) / g_acc
        g_acc *= 10 ** (s["gain_db"] / 10.0)
    return 10 * math.log10(f_tot)


def rx_link_budget(prof: Profile, f_mhz: float, far_eirp_dbw: float, range_km: float,
                   rx_atten_db: float, symbol_rate_hz: float, noise_bw_hz: float,
                   bits_per_symbol: int, code_rate: float, required_esn0_db: float,
                   pol_loss_db: float = 0.0, pointing_loss_db: float = 0.0,
                   other_loss_db: float = 0.0, impl_loss_db: float = 0.0,
                   t_ant_k: float = T0_K) -> dict:
    """Received level, cascaded NF, noise floor, SNR, Es/N0, Eb/N0, margin, max range."""
    L = fspl_db(range_km, f_mhz)
    gant = prof.get("antenna", "gain_dbi", f_mhz)
    pr_ant = far_eirp_dbw + 30.0 - L - pol_loss_db - pointing_loss_db - other_loss_db + gant
    stages = rx_cascade(prof, f_mhz, rx_atten_db)
    nf = friis_nf_db(stages)
    te = T0_K * (10 ** (nf / 10.0) - 1.0)
    tsys = float(t_ant_k) + te
    n0_dbm_hz = K_BOLTZ_DBM + 10 * math.log10(tsys)
    noise_dbm = n0_dbm_hz + 10 * math.log10(noise_bw_hz)
    cn0 = pr_ant - n0_dbm_hz
    snr = pr_ant - noise_dbm
    esn0 = cn0 - 10 * math.log10(symbol_rate_hz)
    ebn0 = esn0 - 10 * math.log10(max(bits_per_symbol * code_rate, 1e-9))
    margin = esn0 - impl_loss_db - required_esn0_db
    gain_total = sum(s["gain_db"] for s in stages)
    g40_in = pr_ant + gain_total
    # saturation: headroom of the strong-signal stages for this received level.
    # Find the LNA by name rather than index, so a change of stage order is safe.
    lna_idx = next(i for i, s in enumerate(stages) if s["stage"] == "LNA")
    lna_in = pr_ant + sum(s["gain_db"] for s in stages[:lna_idx])
    lna_out = lna_in + stages[lna_idx]["gain_db"]
    lna_p1 = _opt(prof, "lna", "p1db_out_dbm", f_mhz)
    g40_fs = _opt(prof, "g40_rx", "max_operating_input_dbm", f_mhz)
    lna_head = None if lna_p1 is None else lna_p1 - lna_out
    g40_head = None if g40_fs is None else g40_fs - g40_in
    heads = [h for h in (lna_head, g40_head) if h is not None]
    min_head = min(heads) if heads else None
    sat = {"lna_in_dbm": lna_in, "lna_out_dbm": lna_out, "lna_p1db_out_dbm": lna_p1,
           "lna_headroom_db": lna_head, "g40_full_scale_dbm": g40_fs, "g40_headroom_db": g40_head,
           "first_to_saturate": (None if min_head is None else
                                 ("LNA" if lna_head is not None and lna_head == min_head else "G40 ADC")),
           "strongest_pr_ant_dbm": (None if min_head is None else pr_ant + min_head),
           "closest_range_km": (None if min_head is None else range_km * 10 ** (-min_head / 20.0))}
    return {"fspl_db": L, "ant_gain_dbi": gant, "pr_ant_dbm": pr_ant, "nf_db": nf,
            "te_k": te, "tsys_k": tsys, "n0_dbm_hz": n0_dbm_hz, "noise_dbm": noise_dbm,
            "cn0_dbhz": cn0, "snr_db": snr, "esn0_db": esn0, "ebn0_db": ebn0,
            "required_esn0_db": required_esn0_db, "margin_db": margin,
            "max_range_km": range_km * 10 ** (margin / 20.0),
            "g40_in_dbm": g40_in, "rx_gain_total_db": gain_total, "stages": stages, **sat}


def required_esn0(data: dict, mod: str, fec: str = "none") -> Optional[dict]:
    for row in data.get("required_esn0", {}).get("rows", []):
        if row.get("mod") == mod and str(row.get("fec")) == str(fec):
            return row
    return None


# GUI helper
MEASURED_LABEL = {"max_output_dbm": "output", "gain_db": "gain", "psat_dbm": "Psat"}


def measured_values(prof: Profile) -> List[str]:
    """Readable names of the profile's values that are bench readings (a leaf with
    measured: true) rather than datasheet figures."""
    out = []
    for role in ROLES + OPTIONAL_ROLES:
        for key, leaf in (prof.comps.get(role) or {}).items():
            if isinstance(leaf, dict) and leaf.get("measured"):
                out.append(f"{ROLE_LABEL.get(role, role)} {MEASURED_LABEL.get(key, key)}")
    return out


def chain_text(prof: Profile) -> Dict[str, str]:
    """The TX and RX chains of a profile in words, in signal order, for the workbook."""
    ports = prof.raw.get("g40_ports") or {}
    sw = prof.raw.get("switch_ports") or {}

    def model(role):
        text = str(prof.comps[role].get("model", prof.raw[role]))
        return text.split(" (")[0].split(",")[0].split(";")[0]

    def jacks(side, label):
        names = ports.get(side) or []
        return f"{label} {' / '.join(names)}" if names else label

    def port(side):
        return f"switch {sw[side]}" if side in sw else f"switch {side} port"
    tx = [jacks("tx", "G40")]
    if "tx_combiner" in prof.comps:
        tx.append(model("tx_combiner"))
    if prof.fixed_pad_db:
        tx.append(f"{prof.fixed_pad_db:g} dB pad")
    tx += [model("hpa"), port("tx"), port("antenna"), "antenna"]
    rx = ["antenna", port("antenna"), port("rx")]
    atten, lna = model("rx_atten"), model("lna")
    rx += [atten, lna] if prof.atten_first else [lna, atten]
    if "rx_splitter" in prof.comps:
        rx.append(model("rx_splitter"))
    rx.append(jacks("rx", "G40"))
    return {"tx": ", ".join(tx), "rx": ", ".join(rx)}


def live_summary(data: dict, prof_name: str, f_mhz: float, tx_atten_db: float,
                 backoff_db: float, hpa_on: bool, rx_atten_db: Optional[float],
                 radio: Optional[str] = None) -> dict:
    """Everything the /payload page shows for the current settings, plus the
    warnings list (computed HPA input / meter input / LNA input / G40 input).
    ``radio`` (a payload_profiles.yaml radios: key) other than None / g40: the
    TX side is that radio and ``tx_atten_db`` is its own control value (the gain
    for a B200).  The minimum safe RX attenuation is always the profile's own."""
    prof = profile(data, prof_name)
    rt = None
    if radio in (None, "g40"):
        e = eirp_chain(prof, f_mhz, tx_atten_db, backoff_db, hpa_on=True)
    else:
        rt = radio_tx(data, radio)
        e = eirp_chain_radio(data, prof, rt, f_mhz, tx_atten_db, backoff_db, hpa_on=True)
    out = {"eirp": e, "hpa_on": bool(hpa_on), "rx_order": prof.rx_order,
           "rx_splitter_db": (prof.rx_splitter_db(f_mhz) if "rx_splitter" in prof.comps else None),
           "g40_ports": prof.raw.get("g40_ports"), "switch_ports": prof.raw.get("switch_ports")}
    warns = list(e["flags"])
    if e["switch_max_dbm"] is None:
        warns.append("switch CW rating unknown: not on its datasheet"
                     + (f" (only {e['switch_pulsed_max_dbm']:g} dBm pulsed)"
                        if e["switch_pulsed_max_dbm"] is not None else "")
                     + "; get it from the vendor before CW keying at high power")
    out["saturation"] = {k: e[k] for k in ("hpa_region", "hpa_compression_db", "hpa_below_psat_db",
                                           "hpa_backoff_p1db_db", "tx_atten_p1db_db", "tx_atten_3db_db")}
    if rt is not None:
        out["tx"] = {"radio": rt.key, "control": rt.control, "label": rt.label,
                     "min_db": rt.min_db, "max_db": rt.max_db, "step_db": rt.step_db}
        if rt.control == "gain":
            out["saturation"].update(tx_gain_p1db_db=e["tx_gain_p1db_db"],
                                     tx_gain_3db_db=e["tx_gain_3db_db"])
            if rt.bench_max_gain_db is not None and float(tx_atten_db) > rt.bench_max_gain_db + 1e-9:
                warns.append(f"{rt.label} {float(tx_atten_db):g} dB is above the bench limit "
                             f"{rt.bench_max_gain_db:g} dB (into the 20 dB pad)")
    out["notes"] = list(e["saturation_notes"])
    measured = measured_values(prof)
    if measured:
        out["notes"].append("bench values, not datasheet: " + ", ".join(measured))
    ms = min_safe_rx_atten(data, prof)
    out["min_safe"] = {k: v for k, v in ms.items() if k != "rows"}
    here = min_rx_atten_at(prof, f_mhz, e["switch_in_dbm"], ms["margin_db"])
    out["min_rx_atten_here_db"] = here["min_rx_atten_db"]
    if rx_atten_db is not None:
        z = rx_leak_chain(prof, f_mhz, e["switch_in_dbm"], rx_atten_db)
        out["leak"] = z
        if not z["lna_ok"]:
            warns.append(f"LNA input in TX {z['lna_in_dbm']:.1f} dBm > max {z['lna_max_input_dbm']:.1f} dBm "
                         + ("at RX atten {:g} dB (raise the RX attenuator: it is in front of the LNA)"
                            .format(rx_atten_db) if prof.atten_first else
                            "(the RX attenuator is after the LNA and cannot protect it; needs more switch "
                            "isolation or a limiter)"))
        if z["lna_p1db_out_dbm"] is not None and z["lna_out_dbm"] > z["lna_p1db_out_dbm"]:
            out["notes"].append(f"LNA saturates during TX (leakage out {z['lna_out_dbm']:.1f} dBm > P1dB "
                                f"{z['lna_p1db_out_dbm']:.1f} dBm): receiver blind while keyed, no damage")
        if not z["atten_ok"]:
            warns.append(f"RX attenuator input in TX {z['atten_in_dbm']:.1f} dBm > max {z['atten_max_input_dbm']:.1f} dBm "
                         "(LNA output in TX, linear estimate)")
        if not z["g40_ok"]:
            warns.append(f"G40 RX input in TX {z['g40_in_dbm']:.1f} dBm > max safe "
                         f"{z['g40_max_safe_dbm']:.1f} dBm at RX atten {rx_atten_db:g} dB")
    # the G40 TX attenuation floor the guard enforces for the LNA (G40 radio only:
    # the floor is in G40 attenuation)
    lf = min_lna_safe_tx_atten(data, prof, margin_db=ms["margin_db"]) if rt is None else None
    out["lna_tx_floor"] = None if lf is None else {k: v for k, v in lf.items() if k != "rows"}
    if lf is not None and lf["needed"] and float(tx_atten_db) < lf["min_tx_atten_db"] - 1e-9:
        warns.append(f"G40 TX attenuation {float(tx_atten_db):g} dB is below the "
                     f"{lf['min_tx_atten_db']:g} dB LNA floor: the payload guard refuses to key")
    if ms["lna_unprotected_mhz"]:
        fl = (f", or the G40 TX attenuation at or above {lf['min_tx_atten_db']:g} dB"
              if lf is not None and lf["needed"] else "")
        warns.append(f"worst case (HPA at Psat): the LNA input {ms['lna_worst_in_dbm']:.1f} dBm exceeds "
                     f"its max {ms['lna_max_input_dbm']:.1f} dBm"
                     + (" even with the RX attenuator at its maximum" if prof.atten_first else
                        f". The RX attenuator is after the LNA, so only more switch isolation or a "
                        f"limiter can protect it{fl}"))
    lo, hi = band_mhz(data, prof)
    if not lo <= f_mhz <= hi:
        warns.insert(0, f"{f_mhz:g} MHz is outside the {prof.name} band {lo:g}-{hi:g} MHz "
                        "(datasheet tables clamped; numbers are not valid here)")
    out["band_mhz"] = [lo, hi]
    out["warnings"] = warns + [w for w in prof.warnings if w not in warns]
    return out


# bench coupler
BENCH_COUPLER = "coupler_zgdc30_372hp"


def apply_g40_max(data: dict, g40_max_dbm):
    """Override the G40 TX max output in the loaded profile data (bench calibration:
    the measured chain sits above the datasheet typical).  Every G40 part a profile
    names takes the override.  None keeps the file value."""
    if g40_max_dbm is None:
        return data
    parts = {"g40_tx"} | {p.get("g40_tx") for p in data.get("profiles", {}).values()}
    for name in parts:
        leaf = data["components"][name]["max_output_dbm"]
        leaf["value"] = float(g40_max_dbm)
        leaf["note"] = f"bench calibration override {float(g40_max_dbm):g} dBm; " + str(leaf.get("note", ""))
    return data


def coupled_port_expected(data: dict, prof: Profile, f_mhz: float, tx_atten_db: float,
                          backoff_db: float = 0.0, extra_loss_db: float = 0.0,
                          coupler: Optional[str] = None) -> dict:
    """Expected level at the coupled port of the bench directional coupler when the
    HPA output goes into the coupler and a dummy load (antenna, switch and LNA out
    of the chain).  coupled = HPA out - coupling(f) - extra_loss (pad or cable on the
    coupled port).  Also hands back the HPA input against its maximum so a sweep
    can refuse a step that would overdrive it."""
    comp = data["components"].get(coupler or BENCH_COUPLER)
    if comp is None:
        raise ProfileError(f"bench coupler {coupler or BENCH_COUPLER!r} not in components")
    cpl = comp.get("coupling_db")
    if is_table(cpl):
        coupling, _ = interp(cpl["points"], f_mhz)
    else:
        coupling = leaf_value(cpl, what=f"{coupler or BENCH_COUPLER}.coupling_db")
    if coupling is None:
        raise ProfileError(f"{coupler or BENCH_COUPLER}: coupling_db unknown")
    e = eirp_chain(prof, f_mhz, tx_atten_db, backoff_db)
    hpa_max = prof.get("hpa", "max_input_dbm", f_mhz, default=None)
    hp = hpa_params(prof, f_mhz)
    return {
        "freq_mhz": float(f_mhz), "tx_atten_db": float(tx_atten_db), "backoff_db": float(backoff_db),
        "g40_out_dbm": e["g40_out_dbm"], "tx_combiner_db": e.get("tx_combiner_db"),
        "hpa_in_dbm": e["hpa_in_dbm"], "hpa_max_input_dbm": hpa_max,
        "hpa_in_ok": (hpa_max is None) or (e["hpa_in_dbm"] <= hpa_max),
        "hpa_gain_db": hp["gain_db"], "hpa_psat_dbm": hp["psat_dbm"], "rapp_s": hp["rapp_s"],
        "hpa_out_dbm": e["hpa_out_dbm"], "hpa_out_w": e["hpa_out_w"],
        "compression_db": e.get("hpa_compression_db"), "region": e.get("hpa_region"),
        "coupling_db": float(coupling), "extra_loss_db": float(extra_loss_db),
        "coupled_dbm": e["hpa_out_dbm"] - float(coupling) - float(extra_loss_db),
    }
