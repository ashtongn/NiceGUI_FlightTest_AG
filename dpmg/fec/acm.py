"""
Tier 5: Adaptive Coding & Modulation (ACM).

Matches the CDM-625A "ACM" option, which swaps MODCOD (modulation + FEC rate)
on the fly from a measured link SNR to hold a target BER while maximising
throughput. This layer does the same job for the DPMG chain: a MODCOD ladder
(monotone in both Es/N0 threshold and spectral efficiency), an SNR to MODCOD
selector with hysteresis (anti-flap), and encode/decode dispatch that wires the
selected FEC tier (Tier 1 Viterbi, optional Tier 3 LDPC) to the modem chain.

Modulations reuse the already-integrated set (BPSK/QPSK/8PSK/16APSK/32APSK via
higher_order + get_modulator/get_demodulator). FEC reuses dpmg.fec.conv (Tier 1)
and, when present, dpmg.fec.ldpc (Tier 3).

Es/N0 thresholds are nominal reference values for the switch ladder (DVB-S2-style
ordering), not per-implementation measured QEF points. The controller only
needs the order to be right; the measured-SNR margin does the rest. Tune req_esn0_db from a real BER campaign before flying it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

BITS_PER_SYMBOL = {
    "BPSK": 1, "QPSK": 2, "8PSK": 3, "8QAM": 3,
    "16QAM": 4, "16APSK": 4, "32APSK": 5,
}


@dataclass(frozen=True)
class Modcod:
    name: str
    modulation: str          # get_modulator name
    fec_family: str          # "conv" | "ldpc"
    rate: str                # e.g. "1/2", "3/4", "7/8"
    req_esn0_db: float       # nominal Es/N0 switch threshold
    idx: int = 0

    @property
    def bits_per_symbol(self) -> int:
        return BITS_PER_SYMBOL[self.modulation]

    @property
    def code_rate(self) -> float:
        a, b = self.rate.split("/")
        return int(a) / int(b)

    @property
    def spectral_eff(self) -> float:
        # info bits per channel symbol
        return self.bits_per_symbol * self.code_rate


# ACM ladder: strictly increasing req_esn0_db and spectral_eff. Tier-1 Viterbi
# across the higher-order mods. fec_family="ldpc" rows can replace these once
# Tier 3 covers the same rungs; see acm_encode dispatch.
_LADDER = [
    ("BPSK",   "conv", "1/2",  1.0),
    ("QPSK",   "conv", "1/2",  3.0),
    ("QPSK",   "conv", "3/4",  5.0),
    ("8PSK",   "conv", "2/3",  7.0),
    ("8PSK",   "conv", "3/4",  8.5),
    ("16APSK", "conv", "3/4", 10.5),
    ("16APSK", "conv", "5/6", 11.5),
    ("32APSK", "conv", "3/4", 12.5),
    ("32APSK", "conv", "7/8", 14.0),
]

ACM_TABLE: List[Modcod] = [
    Modcod(f"{m}-{fam}{r.replace('/', '')}", m, fam, r, e, i)
    for i, (m, fam, r, e) in enumerate(_LADDER)
]


# measured option
# Es/N0 (dB, per on-air symbol) at which each rung's frames decode with
# FER <= 1e-2 through DPMG's real decode path (gen_scenario_json: AWGN,
# GPU-model channelizer, decode_pool._default_decode, hard-decision Viterbi),
# plus 1.0 dB margin. Source: bench/acm_threshold_sweep.py --replay,
# tests/data/acm_thresholds_replay.json (docs/FEC.md). None =
# the rung never reached FER 1e-2 on the ladder (unusable as measured).
# These sit far above the nominal table because the decode path, not the
# code, sets the edge (acquisition misses, hard decisions).
MEASURED_ESN0_DB = {
    "BPSK-conv12": None,
    "QPSK-conv12": 33.0,
    "QPSK-conv34": 27.0,
    "8PSK-conv23": 17.0,
    "8PSK-conv34": 17.0,
    "16APSK-conv34": 22.0,
    "16APSK-conv56": 21.0,
    "32APSK-conv34": 23.0,
    "32APSK-conv78": 22.5,
}
MEASURED_SOURCE = "tests/data/acm_thresholds_replay.json"

# Which thresholds the controller uses: "nominal" (default, the table above)
# or "measured". Per call via table=acm_table("measured"), or process-wide
# with DPMG_ACM_THRESHOLDS=measured.
THRESHOLD_SOURCES = ("nominal", "measured")


def acm_table(source: Optional[str] = None) -> List[Modcod]:
    """The MODCOD ladder for a threshold source. 'measured' replaces
    req_esn0_db with MEASURED_ESN0_DB, drops rungs that never met the FER
    target and rungs dominated by a more efficient rung that needs no more
    Es/N0, and re-indexes (still strictly monotone in both). Falls back to
    the nominal table when no measurement is loaded."""
    import os
    src = (source or os.environ.get("DPMG_ACM_THRESHOLDS") or "nominal").lower()
    if src not in THRESHOLD_SOURCES:
        raise ValueError(f"unknown ACM threshold source {src!r}; one of {THRESHOLD_SOURCES}")
    if src == "nominal" or not any(v is not None for v in MEASURED_ESN0_DB.values()):
        return ACM_TABLE
    rows = [(mc, MEASURED_ESN0_DB.get(mc.name)) for mc in ACM_TABLE]
    rows = [(mc, e) for mc, e in rows if e is not None]
    keep = []
    for mc, e in rows:
        if any(o.spectral_eff > mc.spectral_eff and oe <= e for o, oe in rows):
            continue                       # a faster rung decodes at no more Es/N0
        keep.append((mc, e))
    keep.sort(key=lambda t: t[1])
    out, last = [], None
    for mc, e in keep:
        if last is not None and (e <= last[1] or mc.spectral_eff <= last[0].spectral_eff):
            continue
        out.append(Modcod(mc.name, mc.modulation, mc.fec_family, mc.rate, float(e), len(out)))
        last = (mc, e)
    return out or ACM_TABLE


def select_modcod(esn0_db: float, margin_db: float = 1.0,
                  table: Optional[List[Modcod]] = None) -> Modcod:
    """Highest-efficiency MODCOD whose req_esn0 + margin <= measured Es/N0.
    Falls back to the most robust rung (index 0) below the floor. `table`
    defaults to acm_table() (nominal unless DPMG_ACM_THRESHOLDS=measured)."""
    tab = table if table is not None else acm_table()
    best = tab[0]
    for mc in tab:
        if mc.req_esn0_db + margin_db <= esn0_db:
            best = mc
        else:
            break
    return best


@dataclass
class AcmController:
    """Stateful ACM with hysteresis so noise on the SNR estimate cannot flap the
    MODCOD every frame. Up-shift needs the higher rung's threshold + margin +
    hyst; down-shift is immediate once the current rung loses its margin (fail
    safe toward robustness)."""
    margin_db: float = 1.0
    hysteresis_db: float = 1.0
    table: Optional[List[Modcod]] = None      # None: acm_table() at construction
    current: Optional[Modcod] = None
    switches: int = 0

    def __post_init__(self):
        if self.table is None:
            self.table = acm_table()
        if self.current is None:
            self.current = self.table[0]

    def update(self, esn0_db: float) -> Modcod:
        tab = self.table
        cur = self.current
        # down-shift: drop rungs while current no longer meets margin
        while cur.idx > 0 and esn0_db < cur.req_esn0_db + self.margin_db:
            cur = tab[cur.idx - 1]
        # up-shift: climb while the next rung clears threshold + margin + hyst
        while cur.idx < len(tab) - 1:
            nxt = tab[cur.idx + 1]
            if esn0_db >= nxt.req_esn0_db + self.margin_db + self.hysteresis_db:
                cur = nxt
            else:
                break
        if cur.name != self.current.name:
            self.switches += 1
            self.current = cur
        return self.current


# chain dispatch
def acm_encode(bits, modcod: Modcod) -> List[int]:
    """Apply the MODCOD's FEC to info bits. Returns coded bit list ready for the
    modulator (get_modulator(modcod.modulation))."""
    if modcod.fec_family == "conv":
        from dpmg.fec import conv
        return conv.encode(bits, modcod.rate)
    if modcod.fec_family == "ldpc":
        from dpmg.fec import ldpc
        return ldpc.get_ldpc(_ldpc_name(modcod)).encode(bits)
    raise ValueError(f"unknown fec_family {modcod.fec_family}")


def acm_decode(llr, modcod: Modcod, n_info: Optional[int] = None) -> List[int]:
    """Soft-decode the MODCOD's FEC from per-coded-bit LLRs (llr > 0 means bit 1)."""
    if modcod.fec_family == "conv":
        from dpmg.fec import conv
        return conv.decode_soft(llr, modcod.rate, n_info=n_info)
    if modcod.fec_family == "ldpc":
        from dpmg.fec import ldpc
        return ldpc.get_ldpc(_ldpc_name(modcod)).decode(llr)
    raise ValueError(f"unknown fec_family {modcod.fec_family}")


def _ldpc_name(modcod: Modcod) -> str:
    # maps to the dpmg.fec.ldpc array-code registry (qc-r12 / qc-r23 / qc-r34)
    return {"1/2": "qc-r12", "2/3": "qc-r23", "3/4": "qc-r34"}[modcod.rate]


def to_plan_config(modcod: Modcod) -> dict:
    """MODCOD: the fields the plan/modem chain consumes (modulation name +
    a fec descriptor). Kept string-only so it serialises into plan JSON."""
    return {
        "modcod": modcod.name,
        "modulation": modcod.modulation,
        "fec": f"{modcod.fec_family}:{modcod.rate}",
        "code_rate": modcod.code_rate,
        "bits_per_symbol": modcod.bits_per_symbol,
        "spectral_eff": modcod.spectral_eff,
        "req_esn0_db": modcod.req_esn0_db,
    }
