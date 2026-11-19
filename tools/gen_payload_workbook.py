#!/usr/bin/env python3
"""Generate the payload EIRP / RX-protection / RX link-budget workbook from
payload_profiles.yaml, with live Excel formulas.

    python3 tools/gen_payload_workbook.py [--profiles payload_profiles.yaml]
                                          [--out docs/Payload_EIRP_Link_Budget.xlsx]
                                          [--check]

The equations are the ones in payload_budget.py (the module the /payload page
and the service's RX-protection guard use).  ``--check`` recalculates the
written file with LibreOffice and compares the default operating points with
payload_budget's numbers (tests/test_payload_workbook.py does the same).

Sheets: README, Inputs (per-profile scalars with sources), Tables (every
frequency table with its source), EIRP (operating point + TX-attenuation sweep),
RX Protection (TX-state leakage ladder + band worst case = the service's
keying threshold), RX Link Budget (notional systems, Friis cascade, margins),
Required EsN0 (thresholds per modulation / FEC).
"""
from __future__ import annotations

import argparse
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dpmg.payload import payload_budget as pb  # noqa: E402

from openpyxl import Workbook  # noqa: E402
from openpyxl.comments import Comment  # noqa: E402
from openpyxl.formatting.rule import FormulaRule  # noqa: E402
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side  # noqa: E402
from openpyxl.utils import get_column_letter  # noqa: E402
from openpyxl.worksheet.datavalidation import DataValidation  # noqa: E402

FONT = "Arial"
F_BASE = Font(name=FONT, size=10)
F_BOLD = Font(name=FONT, size=10, bold=True)
F_TITLE = Font(name=FONT, size=14, bold=True)
F_H = Font(name=FONT, size=10, bold=True, color="FFFFFF")
F_INPUT = Font(name=FONT, size=10, color="0000FF")
F_LINK = Font(name=FONT, size=10, color="008000")
F_NOTE = Font(name=FONT, size=9, italic=True, color="555555")
FILL_IN = PatternFill("solid", fgColor="FFFF00")
FILL_H = PatternFill("solid", fgColor="334155")
FILL_SUB = PatternFill("solid", fgColor="E2E8F0")
FILL_VERIFY = PatternFill("solid", fgColor="FDE68A")
FILL_BAD = PatternFill("solid", fgColor="FECACA")
FILL_OK = PatternFill("solid", fgColor="DCFCE7")
THIN = Side(style="thin", color="CBD5E1")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

# frequency-dependent (or scalar) quantities read through the interp machinery:
# key: (role, component key, label, unit, required)
QUANTS = {
    "g40_pmax":   ("g40_tx", "max_output_dbm", "G40 TX output at 0 dB atten", "dBm", True),
    "comb_il":    ("tx_combiner", "insertion_loss_db", "TX combiner loss (split loss included)", "dB", False),
    "hpa_gain":   ("hpa", "gain_db", "HPA small-signal gain", "dB", True),
    "hpa_psat":   ("hpa", "psat_dbm", "HPA Psat", "dBm", True),
    "hpa_maxin":  ("hpa", "max_input_dbm", "HPA max RF input (no damage)", "dBm", True),
    "sw_il":      ("switch", "insertion_loss_db", "Switch insertion loss", "dB", True),
    "sw_iso":     ("switch", "isolation_db", "Switch isolation (TX port -> RX port)", "dB", True),
    "sw_rating":  ("switch", "power_rating_dbm", "Switch max CW input", "dBm", False),
    "sw_pulsed":  ("switch", "pulsed_rating_dbm", "Switch pulsed RF input rating (part with no CW rating)", "dBm", False),
    "ant_gain":   ("antenna", "gain_dbi", "Antenna gain", "dBi", True),
    "ant_max":    ("antenna", "max_input_dbm", "Antenna max input", "dBm", False),
    "lna_gain":   ("lna", "gain_db", "LNA gain", "dB", True),
    "lna_nf":     ("lna", "nf_db", "LNA noise figure", "dB", True),
    "lna_maxin":  ("lna", "max_input_dbm", "LNA max RF input (no damage)", "dBm", True),
    "lna_p1db":   ("lna", "p1db_out_dbm", "LNA output P1dB", "dBm", False),
    "att_il":     ("rx_atten", "insertion_loss_db", "RX attenuator insertion loss (0 dB setting)", "dB", True),
    "att_maxin":  ("rx_atten", "max_input_dbm", "RX attenuator max input", "dBm", False),
    "split_il":   ("rx_splitter", "insertion_loss_db", "RX splitter loss to one G40 RX port (split loss included)", "dB", False),
    "g40_maxsafe": ("g40_rx", "max_safe_input_dbm", "G40 RX max safe input", "dBm", True),
    "g40_maxop":  ("g40_rx", "max_operating_input_dbm", "G40 RX max operating input (onset of clipping)", "dBm", False),
    "g40_nf":     ("g40_rx", "noise_figure_db", "G40 RX noise figure", "dB", True),
}
CABLE_LABELS = {
    "g40_to_hpa": "Cable G40 TX -> HPA", "hpa_to_switch": "Cable HPA -> T/R switch",
    "switch_to_antenna": "Cable switch -> antenna",
    "switch_to_lna": "Cable switch -> LNA (attenuator first: switch -> RX attenuator)",
    "lna_to_atten": "Cable LNA -> RX attenuator (attenuator first: RX attenuator -> LNA)",
    "atten_to_g40": "Cable RX attenuator -> G40 RX (attenuator first: LNA -> G40 RX)",
}


def _src(leaf):
    if isinstance(leaf, dict):
        return str(leaf.get("source", "") or ""), bool(leaf.get("verify", False)), str(leaf.get("note", "") or "")
    return "", True, ""


def _leaf(prof: pb.Profile, role, key):
    # optional roles (tx_combiner, rx_splitter) are absent from profiles that do not have one
    return (prof.comps.get(role) or {}).get(key)


class Book:
    def __init__(self, data):
        self.data = data
        self.names = pb.profile_names(data)
        self.profs = {n: pb.profile(data, n) for n in self.names}
        self.wb = Workbook()
        self.tables = {}          # (quant, profile): (start_row, end_row) on Tables
        self.inputs_row = {}      # scalar key: row on Inputs (profile columns C..)
        self.pcols = [get_column_letter(3 + i) for i in range(len(self.names))]

    # styling helpers
    @staticmethod
    def put(ws, ref, value, font=F_BASE, fill=None, fmt=None, bold=False, wrap=False, align=None):
        c = ws[ref]
        c.value = value
        c.font = F_BOLD if bold and font is F_BASE else font
        if fill is not None:
            c.fill = fill
        if fmt:
            c.number_format = fmt
        if wrap or align:
            c.alignment = Alignment(wrap_text=wrap, horizontal=align, vertical="top")
        return c

    def header_row(self, ws, row, labels, col0=1):
        for i, lab in enumerate(labels):
            c = ws.cell(row=row, column=col0 + i, value=lab)
            c.font, c.fill = F_H, FILL_H
            c.alignment = Alignment(wrap_text=True, vertical="center")
            c.border = BORDER

    # Tables sheet
    def build_tables(self):
        ws = self.wb.create_sheet("Tables")
        self.put(ws, "A1", "Frequency tables (MHz -> value), one block per quantity and profile", font=F_TITLE)
        self.put(ws, "A2", "Linear interpolation in frequency, clamped at the table ends (same rule as "
                           "payload_budget.interp). A scalar datasheet value is a one-row table.", font=F_NOTE)
        r = 4
        for q, (role, key, label, unit, req) in QUANTS.items():
            for pn in self.names:
                prof = self.profs[pn]
                leaf = _leaf(prof, role, key)
                if pb.is_table(leaf):
                    pts = [[float(a), float(b)] for a, b in leaf["points"]]
                else:
                    v = pb.leaf_value(leaf, default=None) if leaf is not None else None
                    if v is None:
                        if req:
                            raise pb.ProfileError(f"{pn}: {role}.{key} required for the workbook")
                        continue
                    pts = [[0.0, float(v)]]
                src, ver, note = _src(leaf)
                comp = prof.raw[role]
                self.put(ws, f"A{r}", f"{label} [{unit}]", bold=True, fill=FILL_SUB)
                self.put(ws, f"B{r}", pn, bold=True, fill=FILL_SUB)
                self.put(ws, f"C{r}", f"component: {comp}", fill=FILL_SUB)
                measured = isinstance(leaf, dict) and leaf.get("measured")
                self.put(ws, f"D{r}", "MEASURED" if measured else ("VERIFY" if ver else "datasheet"),
                         fill=FILL_VERIFY if ver else FILL_SUB)
                self.put(ws, f"E{r}", src[:250], fill=FILL_SUB)
                self.put(ws, f"F{r}", note[:250], font=F_NOTE, fill=FILL_SUB)
                self.put(ws, f"G{r}", q, font=F_NOTE)
                s = r + 1
                for i, (f, v) in enumerate(pts):
                    self.put(ws, f"A{s + i}", f, font=F_INPUT, fmt="0.0")
                    self.put(ws, f"B{s + i}", v, font=F_INPUT, fmt="0.00")
                e = s + len(pts) - 1
                self.tables[(q, pn)] = (s, e)
                r = e + 2
        for col, w in zip("ABCDEFG", (44, 12, 26, 11, 60, 60, 12)):
            ws.column_dimensions[col].width = w

    def interp_one(self, q, pn, x):
        if (q, pn) not in self.tables:
            return None
        s, e = self.tables[(q, pn)]
        A = f"Tables!$A${s}:$A${e}"
        B = f"Tables!$B${s}:$B${e}"
        if s == e:
            return f"Tables!$B${s}"
        m = f"MATCH({x},{A},1)"
        return (f"IF({x}<=Tables!$A${s},Tables!$B${s},IF({x}>=Tables!$A${e},Tables!$B${e},"
                f"INDEX({B},{m})+({x}-INDEX({A},{m}))*(INDEX({B},{m}+1)-INDEX({B},{m}))"
                f"/(INDEX({A},{m}+1)-INDEX({A},{m}))))")

    def sel(self, q, x, pidx):
        """Formula body (no '=') for quantity q at frequency cell x, selected profile."""
        parts = [self.interp_one(q, pn, x) for pn in self.names]
        if all(p is None for p in parts):
            return '"n/a"'
        parts = [p if p is not None else '"n/a"' for p in parts]
        if len(parts) == 1:
            return parts[0]
        return f"CHOOSE({pidx},{','.join(parts)})"

    def has(self, q):
        return any((q, pn) in self.tables for pn in self.names)

    def opt0(self, q, x, pidx):
        """Formula body for an optional quantity, 0 where the profile has none."""
        if not self.has(q):
            return "0"
        v = self.sel(q, x, pidx)
        return f"IF(ISNUMBER({v}),{v},0)"

    # Inputs sheet
    def build_inputs(self):
        ws = self.wb.create_sheet("Inputs", 1)
        self.put(ws, "A1", "Payload profiles: per-profile scalar parameters", font=F_TITLE)
        self.header_row(ws, 3, ["Parameter", "Unit"] + self.names + ["Source", "Verify / note"])
        r = 4

        def row(key, label, unit, values, src="", ver=False, note="", fmt="0.00", is_text=False):
            nonlocal r
            self.put(ws, f"A{r}", label)
            self.put(ws, f"B{r}", unit)
            for i, v in enumerate(values):
                c = self.put(ws, f"{self.pcols[i]}{r}", v, font=F_BASE if is_text else F_INPUT,
                             fmt=None if is_text else fmt)
                c.border = BORDER
            ci = 3 + len(self.names)
            self.put(ws, f"{get_column_letter(ci)}{r}", src[:250])
            vc = self.put(ws, f"{get_column_letter(ci + 1)}{r}", (("VERIFY. " if ver else "") + note)[:250],
                          font=F_NOTE)
            if ver:
                vc.fill = FILL_VERIFY
            self.inputs_row[key] = r
            r += 1

        P = [self.profs[n] for n in self.names]
        row("label", "Profile", "", [p.label for p in P], is_text=True)
        row("hpa_model", "HPA", "", [str(p.comps["hpa"].get("model", p.raw["hpa"])) for p in P], is_text=True)
        row("lna_model", "LNA", "", [str(p.comps["lna"].get("model", p.raw["lna"])) for p in P], is_text=True)
        row("sw_model", "T/R switch", "", [str(p.comps["switch"].get("model", p.raw["switch"])) for p in P], is_text=True)
        chains = [pb.chain_text(p) for p in P]
        row("tx_chain", "TX chain, in signal order", "", [c["tx"] for c in chains], is_text=True)
        row("rx_chain", "RX chain, in signal order", "", [c["rx"] for c in chains], is_text=True)
        row("measured", "Bench-measured values (everything else is datasheet or estimate)", "",
            [", ".join(pb.measured_values(p)) or "none" for p in P], "payload_profiles.yaml leaves with measured: true",
            is_text=True)
        cw = [pb.leaf_value(p.comps["switch"].get("power_rating_dbm"), default=None)
              if not pb.is_table(p.comps["switch"].get("power_rating_dbm")) else None for p in P]
        row("sw_cw", "Switch CW input rating", "dBm", [("n/a" if c is None else c) for c in cw],
            "switch datasheet", any(c is None for c in cw),
            "n/a = no CW rating on the datasheet (PASR0200500600B lists 50 dBm pulsed only); ask the vendor, none is assumed")
        bands = [pb.band_mhz(self.data, p) for p in P]
        bsrc = _src(P[0].raw.get("band_mhz"))
        row("band_lo", "Operating band low edge", "MHz", [b[0] for b in bands], bsrc[0], bsrc[1], bsrc[2], "0")
        row("band_hi", "Operating band high edge", "MHz", [b[1] for b in bands], bsrc[0], bsrc[1], bsrc[2], "0")
        for k, lab in CABLE_LABELS.items():
            leaf = (P[0].raw.get("cables_db") or {}).get(k)
            s, v, n = _src(leaf)
            row(f"cab_{k}", lab, "dB", [p.cables[k] for p in P], s, v, n)
        # source of the first profile that has a pad, else of the first profile
        s, v, n = _src(next((p.raw.get("fixed_pad_db") for p in P if p.fixed_pad_db), P[0].raw.get("fixed_pad_db")))
        row("fixed_pad", "Fixed pad G40 TX -> HPA input", "dB", [p.fixed_pad_db for p in P], s, v, n)
        mid = [(b[0] + b[1]) / 2 for b in bands]
        ss = [pb.hpa_params(p, f)["rapp_s"] for p, f in zip(P, mid)]
        p1 = [p.comps["hpa"].get("p1db_dbm") for p in P]
        row("rapp_s", "HPA compression model: Rapp smoothness s (power domain)", "", ss,
            "derived from HPA P1dB and Psat (payload_budget.rapp_s_from_p1db)",
            any(x is None for x in p1),
            "Pout = x/(1+(x/Psat)^s)^(1/s), x = linear output (mW). Fallback s=2 where P1dB is not published.",
            "0.000")
        off = [pb.leaf_value(p.comps["hpa"].get("off_gain_db"), default=None) for p in P]
        row("hpa_off", "HPA gain when disabled (feed-through)", "dB",
            [("n/a" if o is None else o) for o in off], "HPA datasheet", True,
            "not published -> EIRP with the HPA disabled is not computed")
        mx = [float(pb.leaf_value(p.comps["rx_atten"].get("max_db"), default=120.0)) for p in P]
        st = [float(pb.leaf_value(p.comps["rx_atten"].get("step_db"), default=0.05)) for p in P]
        s, v, n = _src(P[0].comps["rx_atten"].get("max_db"))
        row("att_max", "RX attenuator max setting", "dB", mx, s, v, n)
        s, v, n = _src(P[0].comps["rx_atten"].get("step_db"))
        row("att_step", "RX attenuator step", "dB", st, s, v, n, "0.00")
        txm = [float(pb.leaf_value(p.comps["g40_tx"].get("tx_atten_max_db"), default=41.75)) for p in P]
        s, v, n = _src(P[0].comps["g40_tx"].get("tx_atten_max_db"))
        row("tx_att_max", "G40 TX attenuation range (max)", "dB", txm, s, v, n)
        margin = pb.protect_margin_db(self.data)
        s, v, n = _src((self.data.get("protection") or {}).get("margin_db"))
        row("margin", "RX protection margin below the G40 max safe input", "dB", [margin] * len(P), s, v, n)
        fl = []
        for p in P:
            f = p.raw.get("min_rx_atten_db")
            if f is None:
                f = (self.data.get("protection") or {}).get("min_rx_atten_db")
            fl.append(pb.leaf_value(f, default=0.0) if f is not None else 0.0)
        row("floor", "RX attenuation floor (explicit minimum, 0 = none)", "dB", fl,
            "payload_profiles.yaml protection / profile min_rx_atten_db", False, "")
        row("rxfirst", "RX attenuator before the LNA (1) or after it (0)", "", [1 if p.atten_first else 0 for p in P],
            "payload_profiles.yaml profile rx_order", False,
            "1 = switch, RX attenuator, LNA, G40: the attenuator setting protects the LNA too", "0")
        self.inputs_end = r - 1
        ws.column_dimensions["A"].width = 52
        ws.column_dimensions["B"].width = 7
        for c in self.pcols:
            ws.column_dimensions[c].width = 20
        ws.column_dimensions[get_column_letter(3 + len(self.names))].width = 60
        ws.column_dimensions[get_column_letter(4 + len(self.names))].width = 70
        ws.freeze_panes = "C4"

    def scal(self, key, pidx):
        r = self.inputs_row[key]
        return f"INDEX(Inputs!$C${r}:${self.pcols[-1]}${r},{pidx})"

    def plist(self):
        r = self.inputs_row["label"]
        return f"Inputs!$C$3:${self.pcols[-1]}$3"

    def profile_cell(self, ws, ref, default):
        c = self.put(ws, ref, default, font=F_INPUT, fill=FILL_IN)
        dv = DataValidation(type="list", formula1="=" + self.plist(), allow_blank=False)
        ws.add_data_validation(dv)
        dv.add(ref)
        return c

    def yes_no(self, ws, ref, default="YES"):
        self.put(ws, ref, default, font=F_INPUT, fill=FILL_IN)
        dv = DataValidation(type="list", formula1='"YES,NO"', allow_blank=False)
        ws.add_data_validation(dv)
        dv.add(ref)

    @staticmethod
    def flag_rule(ws, rng, formula):
        ws.conditional_formatting.add(rng, FormulaRule(formula=[formula], fill=FILL_BAD))

    # EIRP sheet
    def build_eirp(self):
        ws = self.wb.create_sheet("EIRP")
        d = self.data.get("workbook_defaults") or {}
        self.put(ws, "A1", "Expected EIRP: TX chain at one operating point", font=F_TITLE)
        self.put(ws, "A2", "Yellow cells are inputs (blue text). Everything else is a formula. Limits come "
                           "from the Inputs / Tables sheets.", font=F_NOTE)
        self.header_row(ws, 3, ["Input", "Value", "Unit / note"])
        self.put(ws, "A4", "Payload profile"); self.profile_cell(ws, "B4", d.get("profile", self.names[0]))
        self.put(ws, "A5", "Frequency"); self.put(ws, "B5", float(d.get("freq_mhz", 2200)), font=F_INPUT, fill=FILL_IN, fmt="0.0")
        self.put(ws, "C5", "MHz")
        self.put(ws, "A6", "G40 TX attenuation (operator setting)")
        self.put(ws, "B6", float(d.get("tx_atten_db", 30)), font=F_INPUT, fill=FILL_IN, fmt="0.00")
        self.put(ws, "C6", "dB, 0 .. 41.75 in 0.25 dB steps (set_tx_atten qdb/4)")
        self.put(ws, "A7", "Waveform backoff from full scale (average)")
        self.put(ws, "B7", float(d.get("backoff_db", 3)), font=F_INPUT, fill=FILL_IN, fmt="0.0")
        self.put(ws, "C7", "dB (amplitude 0.5 = 6 dB; add PAPR for peak power)")
        self.put(ws, "A8", "HPA enabled"); self.yes_no(ws, "B8", "YES")
        self.put(ws, "A9", "Profile index"); self.put(ws, "B9", f"=MATCH(B4,{self.plist()},0)")
        P, F = "$B$9", "$B$5"
        self.header_row(ws, 11, ["Stage", "Value", "Unit", "Limit", "Status"])
        rows = [
            ("G40 TX output at 0 dB atten", f"={self.sel('g40_pmax', F, P)}", "dBm", None),
            ("G40 TX output", "=B12-B6-B7", "dBm", None),
            ("Cable G40 -> HPA + fixed pad + TX combiner",
             f"={self.scal('cab_g40_to_hpa', P)}+{self.scal('fixed_pad', P)}"
             + (f"+IF(ISNUMBER({self.sel('comb_il', F, P)}),{self.sel('comb_il', F, P)},0)"
                if self.has("comb_il") else ""), "dB", None),
            ("HPA input", "=B13-B14", "dBm", f"={self.sel('hpa_maxin', F, P)}"),
            ("HPA small-signal gain", f"={self.sel('hpa_gain', F, P)}", "dB", None),
            ("HPA linear output (no compression)", "=B15+B16", "dBm", None),
            ("HPA Psat", f"={self.sel('hpa_psat', F, P)}", "dBm", None),
            ("Rapp s", f"={self.scal('rapp_s', P)}", "", None),
            ("HPA output", '=IF(B8="YES",10*LOG10(10^(B17/10)/(1+(10^(B17/10)/10^(B18/10))^B19)^(1/B19)),'
                           f'IF(ISNUMBER({self.scal("hpa_off", P)}),B15+{self.scal("hpa_off", P)},"n/a"))', "dBm", None),
            ("HPA compression", '=IF(AND(B8="YES",ISNUMBER(B20)),B17-B20,0)', "dB", None),
            ("Cable HPA -> T/R switch", f"={self.scal('cab_hpa_to_switch', P)}", "dB", None),
            ("Switch TX-port input", '=IF(ISNUMBER(B20),B20-B22,"n/a")',
             "dBm", f"={self.sel('sw_rating', F, P)}" if self.has("sw_rating") else None),
            ("Switch insertion loss", f"={self.sel('sw_il', F, P)}", "dB", None),
            ("Antenna input", f'=IF(ISNUMBER(B23),B23-B24-{self.scal("cab_switch_to_antenna", P)},"n/a")', "dBm",
             f"={self.sel('ant_max', F, P)}" if self.has("ant_max") else None),
            ("Antenna gain", f"={self.sel('ant_gain', F, P)}", "dBi", None),
            ("EIRP", '=IF(ISNUMBER(B25),B25+B26,"n/a")', "dBm", None),
            ("EIRP", '=IF(ISNUMBER(B27),B27-30,"n/a")', "dBW", None),
            ("EIRP", '=IF(ISNUMBER(B27),10^((B27-30)/10),"n/a")', "W", None),
            ("HPA output power", '=IF(ISNUMBER(B20),10^((B20-30)/10),"n/a")', "W", None),
            # saturation (rows 31-37): where the chain starts to compress
            ("HPA input at 1 dB compression (P1dB)", "=B18+10*LOG10((10^(0.1*B19)-1)^(1/B19))-B16", "dBm", None),
            ("HPA input at 3 dB compression (saturation)", "=B18+10*LOG10((10^(0.3*B19)-1)^(1/B19))-B16", "dBm", None),
            ("Input back-off from HPA P1dB (negative = past P1dB)", "=B31-B15", "dB", None),
            ("HPA output below Psat", '=IF(ISNUMBER(B20),B18-B20,"n/a")', "dB", None),
            ("HPA region", '=IF(B8<>"YES","off",IF(B21<0.1,"linear",IF(B21<1,"compressing",IF(B21<3,"past P1dB","SATURATED"))))', "", None),
            ("G40 TX attenuation where the HPA reaches P1dB", "=B12-B7-B14-B31", "dB", None),
            ("G40 TX attenuation where the HPA reaches 3 dB compression", "=B12-B7-B14-B32", "dB", None),
            ("Switch pulsed RF input rating (read when the part has no CW rating)",
             f"={self.sel('sw_pulsed', F, P)}", "dBm", None),
        ]
        for i, (lab, f, unit, lim) in enumerate(rows):
            rr = 12 + i
            self.put(ws, f"A{rr}", lab, bold=lab.startswith(("EIRP", "HPA output")))
            self.put(ws, f"B{rr}", f, fmt="0.00")
            self.put(ws, f"C{rr}", unit)
            if lim:
                self.put(ws, f"D{rr}", lim, fmt="0.00", font=F_LINK)
                self.put(ws, f"E{rr}", f'=IF(AND(ISNUMBER(B{rr}),ISNUMBER(D{rr})),IF(B{rr}>D{rr},"OVER LIMIT","ok"),"")')
                self.flag_rule(ws, f"E{rr}", f'E{rr}="OVER LIMIT"')
        self.put(ws, "E14", f'=IF({self.scal("fixed_pad", P)}>0,"of which fixed pad "&TEXT({self.scal("fixed_pad", P)},"0.0")&" dB","")')
        # no CW rating on file for the selected switch: say so instead of "ok" or blank
        self.put(ws, "E23", '=IF(ISNUMBER(B23),IF(ISNUMBER(D23),IF(B23>D23,"OVER LIMIT","ok"),'
                            'IF(AND(ISNUMBER(B38),B23>B38),"OVER PULSED RATING, NO CW RATING","NO CW RATING")),"")')
        self.flag_rule(ws, "E23", 'RIGHT(E23,12)="NO CW RATING"')
        self.eirp_rows = {"g40": 13, "hpa_in": 15, "hpa_out": 20, "switch_in": 23,
                          "eirp_dbm": 27, "eirp_dbw": 28, "eirp_w": 29}
        self.put(ws, "C36", "dB (below 0 = P1dB not reached even at 0 dB; above 41.75 = always past P1dB)")
        self.put(ws, "C37", "dB (same convention)")
        self.flag_rule(ws, "B35", 'OR(B35="past P1dB",B35="SATURATED")')
        self.put(ws, "A41", "Notes", bold=True)
        notes = [
            "The payload has no coupler and no power meter; the HPA cable runs straight to the T/R switch. "
            "The coupler and power meter are bench characterization equipment (not in this chain).",
            "HPA compression uses a Rapp AM/AM model fitted to the datasheet P1dB and Psat; real parts "
            "differ by a dB or more near saturation.",
            "The HPA max input and the switch / antenna limits come from the datasheets listed on "
            "Inputs / Tables. Values marked VERIFY are estimates or read off curves.",
            "Switch hot-switching limit is lower than its CW rating: the service always switches cold "
            "(HPA off before the switch moves).",
            "NO CW RATING in the switch row: the part's datasheet has none (PASR0200500600B, 50 dBm pulsed "
            "only, row 38). No figure is assumed; get one from the vendor before CW keying at high power.",
            "Tables marks each value MEASURED (bench), VERIFY (estimate or curve reading) or datasheet. P2 uses "
            "the measured G40 output and HPA gain and Psat of the 2026-11-02 bench.",
        ]
        for i, n in enumerate(notes):
            self.put(ws, f"A{42 + i}", n, font=F_NOTE)
        # sweep
        r0 = 52
        self.put(ws, f"A{r0 - 1}", "EIRP vs G40 TX attenuation (profile, frequency and backoff from above; HPA enabled)",
                 bold=True)
        self.header_row(ws, r0, ["TX atten (dB)", "G40 out (dBm)", "HPA in (dBm)", "HPA in status",
                                 "HPA out (dBm)", "Compression (dB)", "EIRP (dBm)", "EIRP (dBW)", "EIRP (W)",
                                 "HPA region"])
        att = [float(a) for a in range(0, 42)] + [41.75]
        for i, a in enumerate(att):
            rr = r0 + 1 + i
            self.put(ws, f"A{rr}", a, font=F_INPUT, fmt="0.00")
            self.put(ws, f"B{rr}", f"=$B$12-A{rr}-$B$7", fmt="0.00")
            self.put(ws, f"C{rr}", f"=B{rr}-$B$14", fmt="0.00")
            self.put(ws, f"D{rr}", f'=IF(C{rr}>$D$15,"OVER MAX","ok")')
            self.put(ws, f"E{rr}", f"=10*LOG10(10^((C{rr}+$B$16)/10)/(1+(10^((C{rr}+$B$16)/10)/10^($B$18/10))^$B$19)^(1/$B$19))", fmt="0.00")
            self.put(ws, f"F{rr}", f"=C{rr}+$B$16-E{rr}", fmt="0.00")
            self.put(ws, f"G{rr}", f"=E{rr}-$B$22-$B$24-"
                                   f"{self.scal('cab_switch_to_antenna', P)}+$B$26", fmt="0.00")
            self.put(ws, f"H{rr}", f"=G{rr}-30", fmt="0.00")
            self.put(ws, f"I{rr}", f"=10^((G{rr}-30)/10)", fmt="0.00")
            self.put(ws, f"J{rr}", f'=IF(F{rr}<0.1,"linear",IF(F{rr}<1,"compressing",IF(F{rr}<3,"past P1dB","SATURATED")))')
        last = r0 + len(att)
        self.flag_rule(ws, f"D{r0 + 1}:D{last}", f'D{r0 + 1}="OVER MAX"')
        ws.conditional_formatting.add(f"J{r0 + 1}:J{last}", FormulaRule(
            formula=[f'OR(J{r0 + 1}="past P1dB",J{r0 + 1}="SATURATED")'], fill=FILL_VERIFY))
        self.sweep_rows = (r0 + 1, last)
        for col, w in zip("ABCDEFGHIJ", (52, 14, 13, 13, 14, 14, 12, 12, 11, 14)):
            ws.column_dimensions[col].width = w
        ws.freeze_panes = "A12"

    # RX protection sheet
    def build_protection(self):
        ws = self.wb.create_sheet("RX Protection")
        d = self.data.get("workbook_defaults") or {}
        self.put(ws, "A1", "RX front-end protection in the TX state", font=F_TITLE)
        self.put(ws, "A2", "TX power leaks from the switch TX port to its RX port through the switch "
                           "isolation, then through the LNA (taken as linear, no compression = "
                           "conservative), the RX attenuator and on to the G40 RX input. With the attenuator "
                           "AFTER the LNA the minimum safe setting protects the G40 only, (G40 input minus "
                           "(G40 max safe minus margin)), and the LNA relies on the switch isolation alone. "
                           "With the attenuator BEFORE the LNA (Inputs: RX attenuator before the LNA = 1) the "
                           "setting protects both and the larger need sets the minimum.",
                           font=F_NOTE, wrap=False)
        self.header_row(ws, 3, ["Input", "Value", "Unit / note"])
        self.put(ws, "A4", "Payload profile"); self.profile_cell(ws, "B4", d.get("profile", self.names[0]))
        self.put(ws, "A5", "Frequency"); self.put(ws, "B5", float(d.get("freq_mhz", 2200)), font=F_INPUT, fill=FILL_IN, fmt="0.0")
        self.put(ws, "C5", "MHz")
        self.put(ws, "A6", "HPA output basis"); self.put(ws, "B6", "Psat (worst case)", font=F_INPUT, fill=FILL_IN)
        dv = DataValidation(type="list", formula1='"Psat (worst case),EIRP sheet operating point"')
        ws.add_data_validation(dv); dv.add("B6")
        self.put(ws, "C6", "the service's keying threshold always uses Psat")
        self.put(ws, "A7", "Profile index"); self.put(ws, "B7", f"=MATCH(B4,{self.plist()},0)")
        P, F = "$B$7", "$B$5"
        self.put(ws, "A8", "Margin below G40 max safe input"); self.put(ws, "B8", f"={self.scal('margin', P)}", fmt="0.0", font=F_LINK)
        self.put(ws, "C8", "dB")
        self.header_row(ws, 10, ["Quantity", "Value", "Unit", "Limit", "Status"])
        eps = "0.0000001"
        step = self.scal('att_step', P)
        psat_out = f"{self.sel('hpa_psat', F, P)}-{self.scal('cab_hpa_to_switch', P)}"
        O = self.scal("rxfirst", P)                 # 1 = RX attenuator before the LNA
        atil, spl = self.sel("att_il", F, P), self.opt0("split_il", F, P)
        rows = [
            ("Switch TX-port input", f'=IF(B6="Psat (worst case)",{psat_out},EIRP!B23)', "dBm", None),
            ("Switch isolation", f"={self.sel('sw_iso', F, P)}", "dB", None),
            ("Leak at the switch RX port", "=B11-B12", "dBm", None),
            ("Cable switch -> LNA (attenuator first: switch -> RX attenuator)",
             f"={self.scal('cab_switch_to_lna', P)}", "dB", None),
            ("LNA input (leakage, RX attenuator at 0 dB)", f"=IF({O}=1,B13-B14-({atil})-B18,B13-B14)", "dBm",
             f"={self.sel('lna_maxin', F, P)}"),
            ("LNA gain", f"={self.sel('lna_gain', F, P)}", "dB", None),
            ("LNA output (leakage, linear)", "=B15+B16", "dBm", None),
            ("Cable LNA -> RX attenuator (attenuator first: RX attenuator -> LNA)",
             f"={self.scal('cab_lna_to_atten', P)}", "dB", None),
            ("RX attenuator input (leakage)", f"=IF({O}=1,B13-B14,B17-B18)", "dBm",
             f"={self.sel('att_maxin', F, P)}" if self.has("att_maxin") else None),
            ("RX attenuator insertion loss (0 dB setting) + RX splitter loss",
             f"={self.sel('att_il', F, P)}+{self.opt0('split_il', F, P)}", "dB", None),
            ("Cable RX attenuator -> G40 RX (attenuator first: LNA -> G40 RX)",
             f"={self.scal('cab_atten_to_g40', P)}", "dB", None),
            ("G40 RX input at 0 dB attenuation", f"=IF({O}=1,B17-({spl})-B21,B19-B20-B21)", "dBm", None),
            ("G40 RX max safe input", f"={self.sel('g40_maxsafe', F, P)}", "dBm", None),
            ("LNA over its max input at 0 dB attenuation (attenuator after the LNA cannot help)",
             "=B15-D15", "dB", None),
            ("Min RX atten: G40 safe, and with the attenuator first the LNA safe (larger need)",
             f"=IF(MAX(B22-(B23-B8),IF({O}=1,B15-(D15-B8),0))<=0,0,"
             f"CEILING(MAX(B22-(B23-B8),IF({O}=1,B15-(D15-B8),0))-{eps},{step}))", "dB", None),
            ("Minimum RX attenuation here", "=B25", "dB",
             f"={self.scal('att_max', P)}"),
            ("LNA output P1dB (saturation; no damage)",
             f"={self.sel('lna_p1db', F, P)}" if self.has("lna_p1db") else '="n/a"', "dBm", None),
            ("LNA state during TX",
             '=IF(ISNUMBER(B27),IF(B17>B27,"saturated by leakage (RX blind while keyed)","linear"),"n/a")', "", None),
        ]
        for i, (lab, f, unit, lim) in enumerate(rows):
            rr = 11 + i
            self.put(ws, f"A{rr}", lab, bold=lab.startswith(("Minimum", "Min RX atten")))
            self.put(ws, f"B{rr}", f, fmt="0.00")
            self.put(ws, f"C{rr}", unit)
            if lim:
                self.put(ws, f"D{rr}", lim, fmt="0.00", font=F_LINK)
                self.put(ws, f"E{rr}", f'=IF(B{rr}>D{rr},"OVER LIMIT","ok")')
                self.flag_rule(ws, f"E{rr}", f'E{rr}="OVER LIMIT"')
        # The attenuator is after the LNA, so a leak over the LNA max is a damage
        # state whatever the setting: only more switch isolation or a limiter helps.
        self.put(ws, "E15", f'=IF({O}=1,IF(B15-B26>D15,"OVER MAX even at the minimum setting","ok at the minimum setting"),'
                            'IF(B15>D15,"OVER MAX (attenuator is after the LNA; needs isolation or a limiter)","ok"))')
        self.flag_rule(ws, "E15", 'LEFT(E15,8)="OVER MAX"')
        self.put(ws, "E24", f'=IF(IF({O}=1,B15-B26>D15,B24>0),"LNA UNPROTECTED","ok")')
        self.flag_rule(ws, "E24", 'E24="LNA UNPROTECTED"')
        # band worst case = the service threshold
        r0 = 32
        self.put(ws, f"A{r0 - 2}", "Service keying threshold: worst case over the band, HPA at Psat", bold=True)
        self.put(ws, f"A{r0 - 1}", "Minimum safe RX attenuation (service)", bold=True)
        freqs = sorted({f for p in self.profs.values() for f in pb.protection_freqs(self.data, p)})
        self.header_row(ws, r0, ["Freq (MHz)", "In band", "Switch in (dBm)", "Isolation (dB)",
                                 "Leak @ sw RX (dBm)", "LNA in @0 dB (dBm)", "LNA max (dBm)",
                                 "G40 in @0 dB (dBm)", "G40 max safe (dBm)", "Min RX atten (dB)"])
        for i, f in enumerate(freqs):
            rr = r0 + 1 + i
            X = f"$A{rr}"
            self.put(ws, f"A{rr}", f, font=F_INPUT, fmt="0.0")
            self.put(ws, f"B{rr}", f'=AND(A{rr}>={self.scal("band_lo", P)},A{rr}<={self.scal("band_hi", P)})')
            self.put(ws, f"C{rr}", f"={self.sel('hpa_psat', X, P)}-{self.scal('cab_hpa_to_switch', P)}", fmt="0.00")
            self.put(ws, f"D{rr}", f"={self.sel('sw_iso', X, P)}", fmt="0.00")
            self.put(ws, f"E{rr}", f"=C{rr}-D{rr}", fmt="0.00")
            Ob = self.scal("rxfirst", P)
            self.put(ws, f"F{rr}", f"=E{rr}-{self.scal('cab_switch_to_lna', P)}"
                                   f"-IF({Ob}=1,{self.sel('att_il', X, P)}+{self.scal('cab_lna_to_atten', P)},0)", fmt="0.00")
            self.put(ws, f"G{rr}", f"={self.sel('lna_maxin', X, P)}", fmt="0.00")
            self.put(ws, f"H{rr}", f"=E{rr}-{self.scal('cab_switch_to_lna', P)}+{self.sel('lna_gain', X, P)}-"
                                   f"{self.scal('cab_lna_to_atten', P)}-{self.sel('att_il', X, P)}-"
                                   f"{self.opt0('split_il', X, P)}-{self.scal('cab_atten_to_g40', P)}", fmt="0.00")
            self.put(ws, f"I{rr}", f"={self.sel('g40_maxsafe', X, P)}", fmt="0.00")
            need = f"MAX(H{rr}-(I{rr}-$B$8),IF({Ob}=1,F{rr}-(G{rr}-$B$8),0))"
            self.put(ws, f"J{rr}", f'=IF(B{rr},IF({need}<=0,0,CEILING({need}-{eps},{step})),"")',
                     fmt="0.00")
            self.flag_rule(ws, f"F{rr}", f"AND(B{rr},F{rr}>G{rr},{Ob}=0)")
        last = r0 + len(freqs)
        self.put(ws, f"B{r0 - 1}", f"=MAX(MAX(J{r0 + 1}:J{last}),{self.scal('floor', P)})", fmt="0.00",
                 font=Font(name=FONT, size=12, bold=True, color="B91C1C"))
        self.put(ws, f"C{r0 - 1}", "dB  (payload_status reports the same value as guard.min_db)")
        self.prot_min_cell = f"B{r0 - 1}"
        # ladder
        l0 = last + 3
        self.put(ws, f"A{l0 - 1}", "Ladder: LNA and G40 RX input in TX state per RX attenuator setting "
                                   "(G40 input at 0 dB from row 22, LNA input at 0 dB from row 15)", bold=True)
        self.header_row(ws, l0, ["RX atten (dB)", "G40 RX input (dBm)", "G40 status", "LNA status"])
        for i, a in enumerate(range(0, 125, 5)):
            rr = l0 + 1 + i
            self.put(ws, f"A{rr}", float(a), font=F_INPUT, fmt="0.00")
            self.put(ws, f"B{rr}", f"=$B$22-A{rr}", fmt="0.00")
            self.put(ws, f"C{rr}", f'=IF(B{rr}>$B$23,"DAMAGE RISK",IF(B{rr}>$B$23-$B$8,"inside margin","safe"))')
            # the setting lowers the LNA input only with the attenuator in front of it
            self.put(ws, f"D{rr}", f'=IF($B$15-IF({O}=1,A{rr},0)>$D$15,"LNA over max","ok")')
        la = l0 + 25
        ws.conditional_formatting.add(f"C{l0 + 1}:D{la}", FormulaRule(formula=[f'OR(C{l0 + 1}="DAMAGE RISK",C{l0 + 1}="LNA over max")'], fill=FILL_BAD))
        ws.conditional_formatting.add(f"C{l0 + 1}:C{la}", FormulaRule(formula=[f'C{l0 + 1}="safe"'], fill=FILL_OK))
        for col, w in zip("ABCDEFGHIJ", (48, 14, 16, 14, 16, 16, 14, 16, 16, 16)):
            ws.column_dimensions[col].width = w

    # required Es/N0
    def build_required(self):
        ws = self.wb.create_sheet("Required EsN0")
        req = self.data.get("required_esn0") or {}
        self.put(ws, "A1", "Required Es/N0 per modulation and FEC", font=F_TITLE)
        self.put(ws, "A2", str(req.get("note", ""))[:400], font=F_NOTE)
        self.header_row(ws, 4, ["Key", "Modulation", "FEC", "Bits/symbol", "Code rate", "Required Es/N0 (dB)",
                                "Required Eb/N0 (dB)", "Basis", "Source / note"])
        rows = req.get("rows") or []
        for i, rw in enumerate(rows):
            rr = 5 + i
            self.put(ws, f"A{rr}", f'=B{rr}&"|"&C{rr}')
            self.put(ws, f"B{rr}", rw["mod"], font=F_INPUT)
            self.put(ws, f"C{rr}", str(rw["fec"]), font=F_INPUT)
            self.put(ws, f"D{rr}", rw.get("bits_per_symbol", 1), font=F_INPUT)
            self.put(ws, f"E{rr}", float(rw.get("code_rate", 1.0)), font=F_INPUT, fmt="0.000")
            v = rw.get("esn0_db")
            self.put(ws, f"F{rr}", ("n/a" if v is None else float(v)), font=F_INPUT, fmt="0.00")
            self.put(ws, f"G{rr}", f'=IF(ISNUMBER(F{rr}),F{rr}-10*LOG10(D{rr}*E{rr}),"n/a")', fmt="0.00")
            self.put(ws, f"H{rr}", str(rw.get("basis", "")))
            self.put(ws, f"I{rr}", (str(rw.get("source", "")) + ("  " + str(rw.get("note")) if rw.get("note") else ""))[:300],
                     font=F_NOTE)
            if rw.get("verify"):
                ws[f"H{rr}"].fill = FILL_VERIFY
        self.req_range = (5, 4 + len(rows))
        # lists for validation
        mods = []
        for rw in rows:
            if rw["mod"] not in mods:
                mods.append(rw["mod"])
        fecs = []
        for rw in rows:
            if str(rw["fec"]) not in fecs:
                fecs.append(str(rw["fec"]))
        self.put(ws, "K4", "Modulations", bold=True)
        for i, m in enumerate(mods):
            self.put(ws, f"K{5 + i}", m)
        self.put(ws, "L4", "FEC", bold=True)
        for i, f in enumerate(fecs):
            self.put(ws, f"L{5 + i}", f)
        self.mod_list = f"'Required EsN0'!$K$5:$K${4 + len(mods)}"
        self.fec_list = f"'Required EsN0'!$L$5:$L${4 + len(fecs)}"
        for col, w in zip("ABCDEFGHIJKL", (22, 24, 7, 10, 10, 16, 16, 30, 90, 3, 24, 8)):
            ws.column_dimensions[col].width = w
        ws.freeze_panes = "A5"

    # link budget
    def build_link(self):
        ws = self.wb.create_sheet("RX Link Budget")
        lb = self.data.get("link_budget") or {}
        systems = lb.get("notional_systems") or []
        if not systems:
            raise pb.ProfileError("link_budget.notional_systems missing")
        n = len(systems)
        cols = [get_column_letter(2 + i) for i in range(n)]
        self.put(ws, "A1", "RX link budget: notional comm systems into the payload receiver", font=F_TITLE)
        self.put(ws, "A2", "Each column is one far-end transmitter (NOTIONAL example values; edit the yellow "
                           "cells). Receive chain = antenna, switch, then LNA and RX attenuator in the selected "
                           "profile's order, then the G40 RX; noise figure cascaded with Friis.", font=F_NOTE)
        self.put(ws, "A3", "Payload profile", bold=True)
        self.profile_cell(ws, "B3", (self.data.get("workbook_defaults") or {}).get("profile", self.names[0]))
        self.put(ws, "C3", f"=MATCH(B3,{self.plist()},0)")
        self.put(ws, "D3", "<- profile index")
        P = "$C$3"
        rq0, rq1 = self.req_range
        R = {}
        r = 5
        self.header_row(ws, r, ["Parameter"] + [f"System {i + 1}" for i in range(n)] + ["Unit / note"])
        r += 1
        inputs = [("name", "System (notional)", "", None), ("freq_mhz", "Frequency", "MHz", "0.0"),
                  ("eirp_dbw", "Far-end EIRP", "dBW", "0.00"), ("range_km", "Range", "km", "0.0"),
                  ("pol_loss_db", "Polarization mismatch loss", "dB", "0.0"),
                  ("pointing_loss_db", "Pointing loss (far end + payload pattern)", "dB", "0.0"),
                  ("other_loss_db", "Other losses (atmosphere, multipath fade margin)", "dB", "0.0"),
                  ("t_ant_k", "Antenna noise temperature", "K", "0"),
                  ("rx_atten_db", "RX attenuator setting (RX state)", "dB", "0.00"),
                  ("symbol_rate_hz", "Symbol rate", "sym/s", "#,##0"),
                  ("noise_bw_hz", "Noise bandwidth for SNR", "Hz", "#,##0"),
                  ("mod", "Modulation", "", None), ("fec", "FEC", "", None),
                  ("impl_loss_db", "Implementation loss", "dB", "0.0")]
        for key, lab, unit, fmt in inputs:
            self.put(ws, f"A{r}", lab)
            for i, s in enumerate(systems):
                c = self.put(ws, f"{cols[i]}{r}", s.get(key), font=F_INPUT, fill=FILL_IN, fmt=fmt)
                c.border = BORDER
            self.put(ws, f"{get_column_letter(2 + n)}{r}", unit)
            R[key] = r
            r += 1
        dvm = DataValidation(type="list", formula1="=" + self.mod_list)
        dvf = DataValidation(type="list", formula1="=" + self.fec_list)
        ws.add_data_validation(dvm); ws.add_data_validation(dvf)
        dvm.add(f"{cols[0]}{R['mod']}:{cols[-1]}{R['mod']}")
        dvf.add(f"{cols[0]}{R['fec']}:{cols[-1]}{R['fec']}")
        r += 1
        self.header_row(ws, r, ["Result"] + [f"System {i + 1}" for i in range(n)] + ["Unit / note"])
        r += 1
        calc = [
            ("bps", "Bits per symbol", lambda c: f"=IFERROR(INDEX('Required EsN0'!$D${rq0}:$D${rq1},MATCH({c}{R['mod']}&\"|\"&{c}{R['fec']},'Required EsN0'!$A${rq0}:$A${rq1},0)),1)", "", "0"),
            ("cr", "Code rate", lambda c: f"=IFERROR(INDEX('Required EsN0'!$E${rq0}:$E${rq1},MATCH({c}{R['mod']}&\"|\"&{c}{R['fec']},'Required EsN0'!$A${rq0}:$A${rq1},0)),1)", "", "0.000"),
            ("fspl", "Free-space path loss", lambda c: f"=20*LOG10({c}{R['range_km']})+20*LOG10({c}{R['freq_mhz']})+32.4478", "dB", "0.00"),
            ("gant", "Payload antenna gain", lambda c: f"={self.sel('ant_gain', f'{c}{R[chr(102)+chr(114)+chr(101)+chr(113)+chr(95)+chr(109)+chr(104)+chr(122)]}', P)}", "dBi", "0.00"),
            ("pr", "Received power at the antenna port", lambda c: f"={c}{R['eirp_dbw']}+30-{c}{{fspl}}-{c}{R['pol_loss_db']}-{c}{R['pointing_loss_db']}-{c}{R['other_loss_db']}+{c}{{gant}}", "dBm", "0.00"),
        ]
        # cascade stages: (name, gain formula, nf formula) per column
        O = self.scal("rxfirst", P)                 # 1 = RX attenuator before the LNA

        def stages(c):
            # slots 3 and 5 swap between the LNA and the RX attenuator with the
            # profile's order; the splitter goes with the attenuator (after the LNA)
            X = f"{c}{R['freq_mhz']}"
            sw = self.sel("sw_il", X, P)
            ail, spl = self.sel("att_il", X, P), self.opt0("split_il", X, P)
            setting = f"{c}{R['rx_atten_db']}"
            lg, lnf = self.sel("lna_gain", X, P), self.sel("lna_nf", X, P)
            att = f"(({ail})+{setting})"
            att_sp = f"(({ail})+({spl})+{setting})"
            c3 = self.scal("cab_atten_to_g40", P)
            return [
                ("Cable antenna->switch", f"-{self.scal('cab_switch_to_antenna', P)}", f"{self.scal('cab_switch_to_antenna', P)}"),
                ("Switch RX path", f"-({sw})", f"({sw})"),
                ("Cable switch->LNA (attenuator first: switch->RX attenuator)",
                 f"-{self.scal('cab_switch_to_lna', P)}", f"{self.scal('cab_switch_to_lna', P)}"),
                ("LNA (attenuator first: RX attenuator, IL + setting)",
                 f"IF({O}=1,-{att},{lg})", f"IF({O}=1,{att},{lnf})"),
                ("Cable LNA->atten (attenuator first: RX attenuator->LNA)",
                 f"-{self.scal('cab_lna_to_atten', P)}", f"{self.scal('cab_lna_to_atten', P)}"),
                ("RX attenuator (IL + setting) + RX splitter (attenuator first: LNA)",
                 f"IF({O}=1,{lg},-{att_sp})", f"IF({O}=1,{lnf},{att_sp})"),
                ("Cable atten->G40 (attenuator first: RX splitter + LNA->G40 cable)",
                 f"-(IF({O}=1,{spl},0)+{c3})", f"(IF({O}=1,{spl},0)+{c3})"),
                ("G40 RX", "0", f"{self.sel('g40_nf', X, P)}"),
            ]
        placed = {}
        for key, lab, fn, unit, fmt in calc:
            self.put(ws, f"A{r}", lab, bold=key == "pr")
            for c in cols:
                f = fn(c).replace("{fspl}", str(placed.get("fspl", 0))).replace("{gant}", str(placed.get("gant", 0)))
                self.put(ws, f"{c}{r}", f, fmt=fmt)
            self.put(ws, f"{get_column_letter(2 + n)}{r}", unit)
            placed[key] = r
            r += 1
        # cascade block: per stage gain (dB), NF (dB), then Friis accumulation
        r += 1
        self.put(ws, f"A{r}", "Receive cascade (gain dB / NF dB per stage; Friis)", bold=True)
        r += 1
        st_rows = []
        names = [s[0] for s in stages(cols[0])]
        for si, name in enumerate(names):
            self.put(ws, f"A{r}", f"{name}: gain")
            self.put(ws, f"A{r + 1}", f"{name}: NF")
            for c in cols:
                g, nf = stages(c)[si][1], stages(c)[si][2]
                self.put(ws, f"{c}{r}", f"={g}", fmt="0.00")
                self.put(ws, f"{c}{r + 1}", f"={nf}", fmt="0.00")
            st_rows.append((r, r + 1))
            r += 2
        # Friis: F = F1 + (F2-1)/G1 + ...
        self.put(ws, f"A{r}", "Cascaded noise figure", bold=True)
        for c in cols:
            terms = []
            gprod = []
            for si, (gr, nr) in enumerate(st_rows):
                Fi = f"10^({c}{nr}/10)"
                if si == 0:
                    terms.append(Fi)
                else:
                    terms.append(f"({Fi}-1)/10^(({'+'.join(gprod)})/10)")
                gprod.append(f"{c}{gr}")
            self.put(ws, f"{c}{r}", f"=10*LOG10({'+'.join(terms)})", fmt="0.00")
        self.put(ws, f"{get_column_letter(2 + n)}{r}", "dB, referred to the antenna port")
        placed["nf"] = r
        r += 1
        more = [
            ("te", "Receiver noise temperature", lambda c: f"=290*(10^({c}{placed['nf']}/10)-1)", "K", "0.0"),
            ("tsys", "System noise temperature", lambda c: f"={c}{R['t_ant_k']}+{c}{{te}}", "K", "0.0"),
            ("n0", "Noise density N0", lambda c: f"=-198.6+10*LOG10({c}{{tsys}})", "dBm/Hz", "0.00"),
            ("noise", "Noise floor in the noise bandwidth", lambda c: f"={c}{{n0}}+10*LOG10({c}{R['noise_bw_hz']})", "dBm", "0.00"),
            ("cn0", "C/N0", lambda c: f"={c}{placed['pr']}-{c}{{n0}}", "dB-Hz", "0.00"),
            ("snr", "SNR in the noise bandwidth", lambda c: f"={c}{placed['pr']}-{c}{{noise}}", "dB", "0.00"),
            ("esn0", "Es/N0", lambda c: f"={c}{{cn0}}-10*LOG10({c}{R['symbol_rate_hz']})", "dB", "0.00"),
            ("ebn0", "Eb/N0 (information bit)", lambda c: f"={c}{{esn0}}-10*LOG10({c}{placed['bps']}*{c}{placed['cr']})", "dB", "0.00"),
            ("req", "Required Es/N0", lambda c: f"=IFERROR(INDEX('Required EsN0'!$F${rq0}:$F${rq1},MATCH({c}{R['mod']}&\"|\"&{c}{R['fec']},'Required EsN0'!$A${rq0}:$A${rq1},0)),\"n/a\")", "dB", "0.00"),
            ("margin", "Link margin (Es/N0 - impl. loss - required)", lambda c: f"=IF(ISNUMBER({c}{{req}}),{c}{{esn0}}-{c}{R['impl_loss_db']}-{c}{{req}},\"n/a\")", "dB", "0.00"),
            ("maxr", "Max range at 0 dB margin", lambda c: f"=IF(ISNUMBER({c}{{margin}}),{c}{R['range_km']}*10^({c}{{margin}}/20),\"n/a\")", "km", "0.0"),
            ("g40in", "Signal level at the G40 RX input", lambda c: f"={c}{placed['pr']}+" + "+".join(f"{c}{gr}" for gr, _ in st_rows), "dBm", "0.00"),
        ]
        if self.has("g40_maxop"):
            more.append(("g40ok", "G40 input vs max operating input",
                         lambda c: f"=IF({c}{{g40in}}>{self.sel('g40_maxop', f'{c}{R[chr(102)+chr(114)+chr(101)+chr(113)+chr(95)+chr(109)+chr(104)+chr(122)]}', P)},\"TOO STRONG: add RX atten\",\"ok\")", "", None))
        # the LNA is cascade slot 3, or slot 5 with the RX attenuator first
        sum_to = lambda c, k: "+".join(f"{c}{gr}" for gr, _ in st_rows[:k])
        more += [
            ("lnain", "LNA input (signal)",
             lambda c: f"={c}{placed['pr']}+IF({O}=1,{sum_to(c, 5)},{sum_to(c, 3)})", "dBm", "0.00"),
            ("lnap1", "LNA output P1dB", lambda c: (f"={self.sel('lna_p1db', f'{c}{R[chr(102)+chr(114)+chr(101)+chr(113)+chr(95)+chr(109)+chr(104)+chr(122)]}', P)}"
                                                    if self.has("lna_p1db") else '="n/a"'), "dBm", "0.00"),
            ("lnahead", "LNA headroom to P1dB (strong-signal saturation)",
             lambda c: f'=IF(ISNUMBER({c}{{lnap1}}),{c}{{lnap1}}-({c}{{lnain}}+IF({O}=1,{c}{st_rows[5][0]},{c}{st_rows[3][0]})),"n/a")', "dB", "0.00"),
            ("g40fs", "G40 RX full scale (max RX gain)", lambda c: (f"={self.sel('g40_maxop', f'{c}{R[chr(102)+chr(114)+chr(101)+chr(113)+chr(95)+chr(109)+chr(104)+chr(122)]}', P)}"
                                                                     if self.has("g40_maxop") else '="n/a"'), "dBm", "0.00"),
            ("g40head", "G40 headroom to ADC full scale", lambda c: f'=IF(ISNUMBER({c}{{g40fs}}),{c}{{g40fs}}-{c}{{g40in}},"n/a")', "dB", "0.00"),
            ("first", "First stage to saturate", lambda c: f'=IF(AND(ISNUMBER({c}{{lnahead}}),ISNUMBER({c}{{g40head}})),IF({c}{{lnahead}}<={c}{{g40head}},"LNA","G40 ADC"),"n/a")', "", None),
            ("strong", "Strongest signal at the antenna port before saturation",
             lambda c: f'=IF({c}{{first}}="n/a","n/a",{c}{placed["pr"]}+MIN({c}{{lnahead}},{c}{{g40head}}))', "dBm", "0.00"),
            ("closest", "Closest range before saturation (this EIRP)",
             lambda c: f'=IF({c}{{first}}="n/a","n/a",{c}{R["range_km"]}*10^(-MIN({c}{{lnahead}},{c}{{g40head}})/20))', "km", "0.000"),
        ]
        for key, lab, fn, unit, fmt in more:
            self.put(ws, f"A{r}", lab, bold=key in ("margin", "maxr", "esn0", "ebn0"))
            for c in cols:
                f = fn(c)
                for k2, v2 in placed.items():
                    f = f.replace("{" + k2 + "}", str(v2))
                self.put(ws, f"{c}{r}", f, fmt=fmt)
            self.put(ws, f"{get_column_letter(2 + n)}{r}", unit)
            placed[key] = r
            r += 1
        for key in ("lnahead", "g40head"):
            hr = placed[key]
            self.flag_rule(ws, f"{cols[0]}{hr}:{cols[-1]}{hr}", f"AND(ISNUMBER({cols[0]}{hr}),{cols[0]}{hr}<0)")
        mr = placed["margin"]
        self.flag_rule(ws, f"{cols[0]}{mr}:{cols[-1]}{mr}", f"AND(ISNUMBER({cols[0]}{mr}),{cols[0]}{mr}<0)")
        ws.conditional_formatting.add(f"{cols[0]}{mr}:{cols[-1]}{mr}",
                                      FormulaRule(formula=[f"AND(ISNUMBER({cols[0]}{mr}),{cols[0]}{mr}>=0)"], fill=FILL_OK))
        self.link_rows = placed
        self.link_inputs = R
        self.link_cols = cols
        # modcod table for one system
        r += 2
        self.put(ws, f"A{r}", "Margin for every modulation / FEC at System", bold=True)
        self.put(ws, f"B{r}", 1, font=F_INPUT, fill=FILL_IN)
        self.put(ws, f"C{r}", "<- system number (same Es/N0, i.e. the same symbol rate)")
        sysc = f"$B${r}"
        r += 1
        self.header_row(ws, r, ["Modulation | FEC", "Required Es/N0 (dB)", "Link Es/N0 (dB)", "Margin (dB)",
                                "Max range (km)"])
        r += 1
        m0 = r
        for i in range(rq1 - rq0 + 1):
            q = rq0 + i
            self.put(ws, f"A{r}", f"='Required EsN0'!A{q}")
            self.put(ws, f"B{r}", f"='Required EsN0'!F{q}", fmt="0.00", font=F_LINK)
            self.put(ws, f"C{r}", f"=INDEX(${cols[0]}${placed['esn0']}:${cols[-1]}${placed['esn0']},{sysc})-"
                                  f"INDEX(${cols[0]}${R['impl_loss_db']}:${cols[-1]}${R['impl_loss_db']},{sysc})", fmt="0.00")
            self.put(ws, f"D{r}", f'=IF(ISNUMBER(B{r}),C{r}-B{r},"n/a")', fmt="0.00")
            self.put(ws, f"E{r}", f'=IF(ISNUMBER(D{r}),INDEX(${cols[0]}${R["range_km"]}:${cols[-1]}${R["range_km"]},{sysc})*10^(D{r}/20),"n/a")',
                     fmt="0.0")
            r += 1
        self.flag_rule(ws, f"D{m0}:D{r - 1}", f"AND(ISNUMBER(D{m0}),D{m0}<0)")
        ws.column_dimensions["A"].width = 50
        for c in cols:
            ws.column_dimensions[c].width = 22
        ws.column_dimensions[get_column_letter(2 + n)].width = 34
        ws.freeze_panes = "B6"

    # README
    def build_readme(self, src_path):
        ws = self.wb.active
        ws.title = "README"
        lines = [
            ("Payload EIRP and RX link budget", F_TITLE),
            (f"Generated {time.strftime('%Y-%m-%d')} by tools/gen_payload_workbook.py from "
             f"{os.path.basename(src_path)}. Regenerate after editing the YAML; do not hand-edit the "
             "Inputs / Tables sheets (the YAML is the master copy, the /payload GUI page and the service's "
             "RX-protection guard read it too).", F_BASE),
            ("", F_BASE),
            ("How to use", F_BOLD),
            ("Yellow cells with blue text are inputs. Pick the payload profile from the dropdown on each sheet. "
             "Every other number is a live formula.", F_BASE),
            ("The payload has no coupler and no power meter: the HPA cable runs straight to the T/R switch. The "
             "RX attenuator sits after the LNA except in the profiles with rx_order atten_first, where it comes "
             "first. The coupler and power meter are bench characterization equipment (payload_profiles.yaml "
             "bench_fixture), not on any payload chain. Inputs lists each profile's TX and RX chain in order.",
             F_BASE),
            (f"Payload 2 as built on the bench 2026-11-03. TX: {pb.chain_text(self.profs['P2'])['tx']}. "
             f"RX: {pb.chain_text(self.profs['P2'])['rx']}. Switch ports: J0 antenna, J1 RX side, J2 TX side "
             "(mounted upside down, tx_level 0). G40 output and HPA gain and Psat are bench readings.", F_BASE)
            if "P2" in self.profs else ("", F_BASE),
            ("EIRP: G40 output -> HPA (Rapp compression toward Psat) -> switch -> antenna; over-limit cells turn "
             "red. The sweep table lists EIRP against the G40 TX attenuation.", F_BASE),
            ("Saturation onset: EIRP rows 31-37 give the HPA input at its 1 dB and 3 dB compression points, the "
             "region (linear / compressing / past P1dB / SATURATED), and the G40 TX attenuation at which each is "
             "reached; the sweep's last column marks the region per attenuation step. RX Protection shows whether "
             "TX leakage saturates the LNA (RX blind while keyed). The link budget gives LNA and G40-ADC headroom, "
             "the first stage to saturate, the strongest signal the receiver takes and the closest range for "
             "that transmitter.", F_BASE),
            ("RX Protection: leakage through the switch isolation in the TX state to the LNA, the RX attenuator "
             "and the G40. The red number is the minimum safe RX attenuation the service enforces before the HPA "
             "can be keyed, worst case over the band with the HPA at Psat, plus the margin. With the attenuator "
             "after the LNA it protects the G40 only and the LNA relies on the switch isolation alone. With the "
             "attenuator first it protects both and the larger need sets it.", F_BASE),
            ("RX Link Budget: notional far-end transmitters (example values, not real systems) -> received "
             "power, Friis noise figure, noise floor, SNR, Es/N0, Eb/N0, margin against the required Es/N0, "
             "and max range.", F_BASE),
            ("", F_BASE),
            ("Data quality", F_BOLD),
            ("Every value carries its source. Cells or rows marked VERIFY (amber) are estimates, read off a "
             "datasheet curve, from a distributor, chip-level rather than card-level, or not published. "
             "Check them on the bench before relying on the numbers.", F_BASE),
            ("Key risks found in the datasheets:", F_BOLD),
        ]
        for rk in (self.data.get("risks") or []):
            lines.append(("  - " + str(rk), F_BASE))
        for i, (t, f) in enumerate(lines):
            c = ws.cell(row=1 + i, column=1, value=t)
            c.font = f
            c.alignment = Alignment(wrap_text=True, vertical="top")
        ws.column_dimensions["A"].width = 150

    def build(self, src_path):
        self.build_readme(src_path)
        self.build_tables()
        self.build_inputs()
        self.build_required()
        self.build_eirp()
        self.build_protection()
        self.build_link()
        order = ["README", "Inputs", "EIRP", "RX Protection", "RX Link Budget", "Required EsN0", "Tables"]
        self.wb._sheets = [self.wb[n] for n in order]
        for ws in self.wb.worksheets:
            for row in ws.iter_rows():
                for c in row:
                    if c.font is None or c.font.name != FONT:
                        c.font = Font(name=FONT, size=c.font.size if c.font else 10,
                                      bold=c.font.bold if c.font else False,
                                      italic=c.font.italic if c.font else False,
                                      color=c.font.color if c.font else None)
        return self.wb


def expected(data) -> dict:
    """payload_budget numbers for the workbook's default inputs (cross-check)."""
    d = data.get("workbook_defaults") or {}
    pn = d.get("profile", pb.profile_names(data)[0])
    prof = pb.profile(data, pn)
    e = pb.eirp_chain(prof, float(d.get("freq_mhz", 2200)), float(d.get("tx_atten_db", 30)),
                      float(d.get("backoff_db", 3)), True)
    ms = pb.min_safe_rx_atten(data, prof)
    out = {"eirp_dbm": e["eirp_dbm"], "hpa_out_dbm": e["hpa_out_dbm"], "hpa_in_dbm": e["hpa_in_dbm"],
           "tx_atten_p1db_db": e["tx_atten_p1db_db"], "tx_atten_3db_db": e["tx_atten_3db_db"],
           "switch_in_dbm": e["switch_in_dbm"], "min_safe_db": ms["min_rx_atten_db"]}
    lbs = []
    for s in (data.get("link_budget") or {}).get("notional_systems", []):
        rq = pb.required_esn0(data, s["mod"], s["fec"]) or {}
        lbs.append(pb.rx_link_budget(prof, float(s["freq_mhz"]), float(s["eirp_dbw"]), float(s["range_km"]),
                                     float(s["rx_atten_db"]), float(s["symbol_rate_hz"]), float(s["noise_bw_hz"]),
                                     int(rq.get("bits_per_symbol", 1)), float(rq.get("code_rate", 1.0)),
                                     float(rq["esn0_db"]) if rq.get("esn0_db") is not None else float("nan"),
                                     float(s.get("pol_loss_db", 0)), float(s.get("pointing_loss_db", 0)),
                                     float(s.get("other_loss_db", 0)), float(s.get("impl_loss_db", 0)),
                                     float(s.get("t_ant_k", 290))))
    out["link"] = lbs
    return out


def recalc(path: str) -> str:
    """LibreOffice headless round trip: path of a recalculated copy.  Works in
    a temporary folder under $HOME (the snap LibreOffice can see neither a
    private /tmp nor hidden directories under $HOME); check() removes it."""
    exe = shutil.which("soffice") or shutil.which("libreoffice")
    if not exe:
        raise RuntimeError("LibreOffice not found")
    import tempfile
    work = tempfile.mkdtemp(prefix="dpmg_wb_check_", dir=os.path.expanduser("~"))
    src = os.path.join(work, "in_" + os.path.basename(path))
    shutil.copy(path, src)
    outdir = os.path.join(work, "out")
    os.makedirs(outdir, exist_ok=True)
    subprocess.run([exe, "--headless", "--calc", "--convert-to", "xlsx", "--outdir", outdir, src],
                   check=True, capture_output=True, timeout=180)
    return os.path.join(outdir, os.path.basename(src))


def check(path: str, data: dict, book: "Book", tol: float = 0.02) -> list:
    from openpyxl import load_workbook
    rc = recalc(path)
    try:
        wb = load_workbook(rc, data_only=True)
    finally:
        shutil.rmtree(os.path.dirname(os.path.dirname(rc)), ignore_errors=True)
    exp = expected(data)
    errs = []
    errors = []
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for c in row:
                if isinstance(c.value, str) and c.value.startswith("#") and c.value.rstrip("!?0/").isupper():
                    errors.append(f"{ws.title}!{c.coordinate}={c.value}")
    if errors:
        errs.append(f"formula errors: {errors[:20]}")
    E = wb["EIRP"]
    for k, row in (("eirp_dbm", book.eirp_rows["eirp_dbm"]), ("hpa_out_dbm", 20), ("hpa_in_dbm", 15),
                   ("switch_in_dbm", 23)):
        v = E[f"B{row}"].value
        if v is None or abs(float(v) - exp[k]) > tol:
            errs.append(f"EIRP {k}: sheet {v} vs payload_budget {exp[k]:.3f}")
    for k, row in (("tx_atten_p1db_db", 36), ("tx_atten_3db_db", 37)):
        v = E[f"B{row}"].value
        if v is None or abs(float(v) - exp[k]) > tol:
            errs.append(f"EIRP {k}: sheet {v} vs payload_budget {exp[k]:.3f}")
    v = wb["RX Protection"][book.prot_min_cell].value
    if v is None or abs(float(v) - exp["min_safe_db"]) > tol:
        errs.append(f"min safe RX atten: sheet {v} vs payload_budget {exp['min_safe_db']}")
    L = wb["RX Link Budget"]
    for i, lb in enumerate(exp["link"]):
        c = book.link_cols[i]
        for k, key in (("nf_db", "nf"), ("pr_ant_dbm", "pr"), ("esn0_db", "esn0"), ("cn0_dbhz", "cn0"),
                       ("g40_in_dbm", "g40in"), ("lna_headroom_db", "lnahead"),
                       ("g40_headroom_db", "g40head"), ("closest_range_km", "closest")):
            v = L[f"{c}{book.link_rows[key]}"].value
            if v is None or abs(float(v) - lb[k]) > tol:
                errs.append(f"link system {i + 1} {k}: sheet {v} vs payload_budget {lb[k]:.3f}")
        if not math.isnan(lb["margin_db"]):
            v = L[f"{c}{book.link_rows['margin']}"].value
            if v is None or abs(float(v) - lb["margin_db"]) > tol:
                errs.append(f"link system {i + 1} margin: sheet {v} vs {lb['margin_db']:.3f}")
    return errs


def generate(profiles_path: str, out_path: str) -> "Book":
    data = pb.load_profiles(profiles_path)
    book = Book(data)
    wb = book.build(profiles_path)
    wb.properties.creator = ""          # openpyxl writes its own name here otherwise
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    wb.save(out_path)
    _plain_app_name(out_path)
    return book


def _plain_app_name(path):
    """openpyxl always names itself as the application in docProps/app.xml; write
    plain "Microsoft Excel" there instead (Excel recalculates on open either way)."""
    import re
    import zipfile
    with zipfile.ZipFile(path) as z:
        parts = [(i, z.read(i.filename)) for i in z.infolist()]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for info, data in parts:
            if info.filename == "docProps/app.xml":
                data = re.sub(rb"<Application>[^<]*</Application>",
                              b"<Application>Microsoft Excel</Application>", data)
            z.writestr(info, data)


def main(argv=None):
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--profiles", default=os.path.join(here, "payload_profiles.yaml"))
    ap.add_argument("--out", default=os.path.join(here, "docs", "Payload_EIRP_Link_Budget.xlsx"))
    ap.add_argument("--check", action="store_true", help="recalculate with LibreOffice and compare")
    a = ap.parse_args(argv)
    book = generate(a.profiles, a.out)
    print(f"wrote {a.out}")
    if a.check:
        errs = check(a.out, book.data, book)
        if errs:
            print("CHECK FAILED:\n  " + "\n  ".join(errs))
            return 1
        print("check: workbook formulas match payload_budget (EIRP, min safe RX atten, link budgets)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
