"""gen_hpa_bench_sheet.py: fill-in workbook for the HPA bench power sweep.

    python3 tools/gen_hpa_bench_sheet.py [--profile P2b] [--freqs 915,2200]
                                         [--attens 41.75,35,30,25,20,15,10,5,0]
                                         [--ampl 0.5] [--extra-loss 0]
                                         [--out output/bench/HPA_bench_power_sweep.xlsx]

Bench chain: G40 TX port then ZB3PD-63-S+ combiner then HPA then ZGDC30-372HP+ directional
coupler then dummy load; coupled port (30 dB down) then spectrum analyzer.

Sheets:
  Inputs   the numbers every row uses (G40 max output, tone backoff, cables, HPA
           Psat and Rapp s, extra loss on the coupled port, pass tolerance); blue
           values are model inputs from payload_profiles.yaml, yellow cells are
           yours to change
  Sweep    one row per (frequency, TX attenuation): the chain as formulas from the
           Inputs sheet and the per-frequency table values (combiner loss, HPA gain,
           coupling, all interpolated from the datasheet tables), the expected
           coupled-port level, then the yellow measured column with delta and
           PASS/FAIL formulas
  Notes    the chain, what each column means, where the numbers come from

Expected rows come from the same payload_budget maths as bench/hpa_power_sweep.py,
so a sweep CSV and this sheet agree.  --check recalculates a copy with LibreOffice
and compares the sheet's formulas with payload_budget.
"""
from __future__ import annotations

import argparse
import math
import os
import sys

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from dpmg.payload import payload_budget as pb        # noqa: E402

FONT = "Arial"
F_TITLE = Font(name=FONT, bold=True, size=13)
F_HEAD = Font(name=FONT, bold=True)
F_BODY = Font(name=FONT)
F_INPUT = Font(name=FONT, color="0000FF")          # model input (blue)
F_LINK = Font(name=FONT, color="008000")           # from another sheet (green)
FILL_USER = PatternFill("solid", fgColor="FFFF00")  # cells the user fills in
FILL_HEAD = PatternFill("solid", fgColor="DDEBF7")
THIN = Side(style="thin", color="999999")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
import datetime as _dt
DEFAULT_OUT = os.path.join(os.environ.get("DPMG_BENCH_RESULTS") or os.path.join(ROOT, "output", "bench"),
                           f"{_dt.date.today():%Y-%m-%d}_hpa_bench_p2b", "HPA_bench_power_sweep.xlsx")


def _floats(text):
    return [float(x) for x in text.replace(";", ",").split(",") if x.strip()]


def _put(ws, ref, value, font=F_BODY, fill=None, fmt=None, comment=None, bold=False, align=None):
    c = ws[ref]
    c.value = value
    c.font = Font(name=font.name, bold=bold or font.bold, color=font.color, size=font.size)
    if fill is not None:
        c.fill = fill
    if fmt:
        c.number_format = fmt
    if comment:
        c.comment = Comment(comment, "payload_budget")
    if align:
        c.alignment = Alignment(horizontal=align)
    return c


def build(data, prof, coupler_name, freqs, attens, ampl, extra_loss, tol, tx_port, out):
    comp = data["components"]
    cpl = comp[coupler_name]
    backoff = -20.0 * math.log10(ampl)
    wb = Workbook()

    # Inputs
    wi = wb.active
    wi.title = "Inputs"
    _put(wi, "A1", f"HPA bench power sweep: inputs (profile {prof.name})", F_TITLE)
    _put(wi, "A2", "Blue = model input from payload_profiles.yaml (source in the comment). "
                   "Yellow = change to match the bench. Every Sweep row reads these cells.", F_BODY)
    rows = [
        ("Profile", prof.name, None, "payload_profiles.yaml profile used for the chain"),
        ("G40 TX port driving the combiner", tx_port, None, "one port; terminate the other combiner inputs"),
        ("G40 max output (dBm)", prof.get("g40_tx", "max_output_dbm", None), "0.0",
         str(data["components"]["g40_tx"]["max_output_dbm"].get("note", ""))[:200]
         if "calibration" in str(data["components"]["g40_tx"]["max_output_dbm"].get("note", ""))
         else "payload_profiles.yaml g40_tx.max_output_dbm, full-scale tone, 0 dB attenuation (datasheet typical)"),
        ("Tone amplitude (full scale 1.0)", ampl, "0.00", "hpa_power_sweep.py --ampl; 0.5 = 6 dB under full scale"),
        ("Backoff from amplitude (dB)", "=-20*LOG10(B{amp_row})", "0.00", "formula: -20 log10(amplitude)"),
        ("Cable G40 to combiner to HPA (dB)", prof.cables["g40_to_hpa"], "0.00",
         "payload_profiles.yaml cables_db.g40_to_hpa (estimate, not measured)"),
        ("Fixed pad before the HPA (dB)", prof.fixed_pad_db, "0.00", "payload_profiles.yaml fixed_pad_db"),
        ("HPA Psat (dBm)", prof.get("hpa", "psat_dbm", None), "0.0",
         "payload_profiles.yaml hpa.psat_dbm (Empower 1189 datasheet, 125 W typ)"),
        ("HPA max input (dBm)", prof.get("hpa", "max_input_dbm", None), "0.0",
         "payload_profiles.yaml hpa.max_input_dbm; a row whose HPA in exceeds this is flagged"),
        ("HPA Rapp knee s", pb.hpa_params(prof, freqs[0])["rapp_s"], "0.000",
         "payload_budget.hpa_params: Rapp sharpness from P1dB and Psat (same at every frequency)"),
        ("Extra loss on the coupled port (dB)", extra_loss, "0.00",
         "pad or cable between the coupler's coupled port and the analyzer; 0 = analyzer straight on"),
        ("Pass tolerance (dB)", tol, "0.0", "a row passes when |measured - expected| is at or under this"),
    ]
    user_rows = {"Tone amplitude (full scale 1.0)", "Extra loss on the coupled port (dB)", "Pass tolerance (dB)",
                 "Cable G40 to combiner to HPA (dB)", "G40 TX port driving the combiner"}
    r0 = 4
    _put(wi, f"A{r0 - 1}", "Input", F_HEAD, FILL_HEAD)
    _put(wi, f"B{r0 - 1}", "Value", F_HEAD, FILL_HEAD)
    _put(wi, f"C{r0 - 1}", "Where it comes from", F_HEAD, FILL_HEAD)
    ref = {}
    amp_row = r0 + [lbl for lbl, *_ in rows].index("Tone amplitude (full scale 1.0)")
    for i, (label, val, fmt, src) in enumerate(rows):
        r = r0 + i
        _put(wi, f"A{r}", label)
        is_formula = isinstance(val, str) and val.startswith("=")
        if is_formula:
            val = val.format(amp_row=amp_row)
        font = F_BODY if is_formula else F_INPUT
        fill = FILL_USER if label in user_rows else None
        _put(wi, f"B{r}", val, font, fill, fmt, comment=src)
        _put(wi, f"C{r}", src)
        ref[label] = f"Inputs!$B${r}"
    wi.column_dimensions["A"].width = 38
    wi.column_dimensions["B"].width = 14
    wi.column_dimensions["C"].width = 90

    # Sweep
    ws = wb.create_sheet("Sweep")
    _put(ws, "A1", f"HPA bench power sweep: expected against measured at the coupled port ({coupler_name})", F_TITLE)
    _put(ws, "A2", "Fill the yellow Measured column from the analyzer marker (dBm). Delta and Verdict update. "
                   "Blue per-row values are interpolated datasheet tables; the rest are formulas.", F_BODY)
    heads = ["Freq (MHz)", "TX atten (dB)", "G40 out (dBm)", "Combiner loss (dB)", "HPA in (dBm)",
             "HPA in OK", "HPA gain (dB)", "HPA linear out (dBm)", "HPA out (dBm)", "HPA out (W)",
             "Compression (dB)", "Coupling (dB)", "Expected coupled (dBm)", "Measured (dBm)",
             "Delta (dB)", "Verdict", "Analyzer notes"]
    hr = 4
    for j, h in enumerate(heads, start=1):
        c = _put(ws, f"{get_column_letter(j)}{hr}", h, F_HEAD, FILL_HEAD, align="center")
        c.border = BOX
        c.alignment = Alignment(horizontal="center", wrap_text=True)
    ws.row_dimensions[hr].height = 32
    G40MAX, BACK = ref["G40 max output (dBm)"], ref["Backoff from amplitude (dB)"]
    CABLE, PAD = ref["Cable G40 to combiner to HPA (dB)"], ref["Fixed pad before the HPA (dB)"]
    PSAT, HMAX, S = ref["HPA Psat (dBm)"], ref["HPA max input (dBm)"], ref["HPA Rapp knee s"]
    XLOSS, TOL = ref["Extra loss on the coupled port (dB)"], ref["Pass tolerance (dB)"]
    r = hr + 1
    first = r
    checks = []
    for f in freqs:
        comb = prof.tx_combiner_db(f)
        gain = pb.hpa_params(prof, f)["gain_db"]
        coupling, _ = pb.interp(cpl["coupling_db"]["points"], f)
        for att in sorted(attens, reverse=True):
            x = pb.coupled_port_expected(data, prof, f, att, backoff, extra_loss, coupler_name)
            checks.append((r, x))
            _put(ws, f"A{r}", f, F_INPUT, fmt="0.0")
            _put(ws, f"B{r}", att, F_INPUT, fmt="0.00")
            _put(ws, f"C{r}", f"={G40MAX}-B{r}-{BACK}", fmt="0.0")
            _put(ws, f"D{r}", round(comb, 3), F_INPUT, fmt="0.00",
                 comment=f"ZB3PD-63-S+ loss common port to one port at {f:g} MHz, split loss included "
                         "(payload_profiles.yaml splitter_zb3pd_63.insertion_loss_db, interpolated)")
            _put(ws, f"E{r}", f"=C{r}-{CABLE}-{PAD}-D{r}", fmt="0.0")
            _put(ws, f"F{r}", f'=IF(E{r}<={HMAX},"ok","OVER MAX")', align="center")
            _put(ws, f"G{r}", round(gain, 2), F_INPUT, fmt="0.0",
                 comment=f"Empower 1189 gain at {f:g} MHz (payload_profiles.yaml hpa_1189.gain_db, interpolated)")
            _put(ws, f"H{r}", f"=E{r}+G{r}", fmt="0.0")
            # Rapp: out = lin / (1 + (lin/Psat)^s)^(1/s), in mW
            _put(ws, f"I{r}", f"=10*LOG10(10^(H{r}/10)/(1+(10^(H{r}/10)/10^({PSAT}/10))^{S})^(1/{S}))", fmt="0.0")
            _put(ws, f"J{r}", f"=10^(I{r}/10)/1000", fmt="0.0")
            _put(ws, f"K{r}", f"=H{r}-I{r}", fmt="0.0")
            _put(ws, f"L{r}", round(coupling, 2), F_INPUT, fmt="0.00",
                 comment=f"ZGDC30-372HP+ coupling at {f:g} MHz (payload_profiles.yaml "
                         f"{coupler_name}.coupling_db, Mini-Circuits datasheet, interpolated)")
            _put(ws, f"M{r}", f"=I{r}-L{r}-{XLOSS}", fmt="0.0", bold=True)
            _put(ws, f"N{r}", None, F_BODY, FILL_USER, "0.0")
            _put(ws, f"O{r}", f'=IF(N{r}="","",N{r}-M{r})', fmt="+0.0;-0.0;0.0")
            _put(ws, f"P{r}", f'=IF(N{r}="","",IF(ABS(O{r})<={TOL},"PASS","FAIL"))', align="center")
            _put(ws, f"Q{r}", None, F_BODY, FILL_USER)
            for j in range(1, len(heads) + 1):
                ws[f"{get_column_letter(j)}{r}"].border = BOX
            r += 1
    last = r - 1
    # one example row so the fill-in format is clear (clearly marked, not a measurement)
    ex = checks[0][1]
    _put(ws, f"A{r + 1}", "Example of a filled row (not a measurement):", F_HEAD)
    _put(ws, f"M{r + 1}", round(ex["coupled_dbm"], 1), fmt="0.0")
    _put(ws, f"N{r + 1}", round(ex["coupled_dbm"] - 0.8, 1), F_BODY, FILL_USER, "0.0")
    _put(ws, f"O{r + 1}", f"=N{r + 1}-M{r + 1}", fmt="+0.0;-0.0;0.0")
    _put(ws, f"P{r + 1}", f'=IF(ABS(O{r + 1})<={TOL},"PASS","FAIL")', align="center")
    _put(ws, f"Q{r + 1}", "marker on the tone peak, RBW 10 kHz", F_BODY, FILL_USER)
    _put(ws, f"A{r + 3}", "Rows measured", F_HEAD)
    _put(ws, f"B{r + 3}", f'=COUNT(N{first}:N{last})')
    _put(ws, f"A{r + 4}", "Rows PASS", F_HEAD)
    _put(ws, f"B{r + 4}", f'=COUNTIF(P{first}:P{last},"PASS")')
    _put(ws, f"A{r + 5}", "Rows FAIL", F_HEAD)
    _put(ws, f"B{r + 5}", f'=COUNTIF(P{first}:P{last},"FAIL")')
    _put(ws, f"A{r + 6}", "Largest |delta| (dB)", F_HEAD)
    _put(ws, f"B{r + 6}", f'=IF(COUNT(O{first}:O{last})=0,"",MAX(MAX(O{first}:O{last}),-MIN(O{first}:O{last})))', fmt="0.0")
    widths = [10, 12, 12, 13, 11, 10, 11, 13, 12, 11, 13, 12, 15, 14, 10, 9, 36]
    for j, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(j)].width = w
    ws.freeze_panes = f"A{hr + 1}"

    # Notes
    wn = wb.create_sheet("Notes")
    notes = [
        "Bench chain: G40 TX port, ZB3PD-63-S+ 3-way combiner (one input driven, the others terminated), "
        "Empower 1189 HPA, ZGDC30-372HP+ 30 dB directional coupler, dummy load on the coupler output, "
        "spectrum analyzer on the coupled port. No antenna, no T/R switch, no LNA, nothing on the RX jacks.",
        "G40 out = G40 max output - TX attenuation - backoff. Backoff follows the tone amplitude: a 0.5 amplitude "
        "tone is 6 dB under full scale. The G40 max output is the datasheet typical; the first measured row "
        "calibrates it (adjust Inputs B5 to the measurement if every row is off by the same amount).",
        "HPA in = G40 out - cable - fixed pad - combiner loss. It must stay under the HPA max input (the HPA in OK column).",
        "HPA out follows the Rapp model: linear gain until the knee, then compression towards Psat. The Rapp s "
        "comes from the datasheet P1dB and Psat. Compression (dB) is linear out minus modelled out.",
        "Expected coupled = HPA out - coupling - extra loss on the coupled port. Coupling is the datasheet "
        "table for the ZGDC30-372HP+, about 30.3 to 30.6 dB from 1.5 to 2.7 GHz.",
        "Measured: the analyzer marker on the tone, in dBm, with the analyzer's reference level and input "
        "attenuation set so the marker is inside its linear range (the coupled port reaches about +20 dBm at "
        "full drive). Note RBW and any pad in the Analyzer notes column.",
        "Delta = measured - expected. Verdict PASS when |delta| is within the pass tolerance on the Inputs sheet.",
        "Run order: highest attenuation first (lowest power). Stop if a reading comes out more than a few dB "
        "above expected: the amplifier is then giving more than the model and the analyzer input is at risk.",
        "bench/hpa_power_sweep.py drives the radio through the same rows and writes a CSV with these expected "
        "values; copy its measured column here, or type the readings in as you go.",
        "Built by tools/gen_hpa_bench_sheet.py from payload_profiles.yaml; rebuild after a profile edit.",
    ]
    _put(wn, "A1", "Notes", F_TITLE)
    for i, t in enumerate(notes, start=3):
        c = _put(wn, f"A{i}", t)
        c.alignment = Alignment(wrap_text=True, vertical="top")
        wn.row_dimensions[i].height = 48
    wn.column_dimensions["A"].width = 120

    os.makedirs(os.path.dirname(out), exist_ok=True)
    wb.properties.creator = ""
    wb.save(out)
    return checks


def check(out, checks, tol=0.05):
    """Recalculate a copy with LibreOffice (headless convert, as
    gen_payload_workbook does) and compare the sheet's Expected column and the
    chain columns with payload_budget."""
    import shutil
    import subprocess
    import tempfile
    from openpyxl import load_workbook
    exe = shutil.which("soffice") or shutil.which("libreoffice")
    if exe is None:
        print("check: LibreOffice not found; skipped")
        return False
    # snap LibreOffice cannot see hidden dirs or /tmp: work under the output's folder
    work = tempfile.mkdtemp(prefix="wb_check_", dir=os.path.dirname(os.path.abspath(out)))
    src = os.path.join(work, "in.xlsx")
    shutil.copy(out, src)
    subprocess.run([exe, "--headless", "--calc", "--convert-to", "xlsx", "--outdir",
                    os.path.join(work, "out"), src], check=True, capture_output=True, timeout=180)
    calc = os.path.join(work, "out", "in.xlsx")
    ws = load_workbook(calc, data_only=True)["Sweep"]
    errors = 0
    worst = 0.0
    for r, x in checks:
        for col, key in (("E", "hpa_in_dbm"), ("I", "hpa_out_dbm"), ("M", "coupled_dbm")):
            v = ws[f"{col}{r}"].value
            if isinstance(v, str) and v.startswith("#"):
                errors += 1
                print(f"{col}{r}: {v}")
                continue
            d = abs(float(v) - x[key])
            worst = max(worst, d)
            if d > tol:
                print(f"{col}{r}: sheet {v} vs budget {x[key]:.3f}")
        if ws[f"F{r}"].value not in ("ok", "OVER MAX"):
            errors += 1
            print(f"F{r}: {ws[f'F{r}'].value!r}")
    for row in ws.iter_rows():
        for c in row:
            if isinstance(c.value, str) and c.value.startswith("#") and c.value.endswith(("!", "?")):
                errors += 1
                print(f"{c.coordinate}: {c.value}")
    shutil.rmtree(work, ignore_errors=True)
    print(f"check: {len(checks)} rows, {errors} formula errors, largest difference {worst:.3f} dB -> "
          f"{'OK' if errors == 0 and worst <= tol else 'MISMATCH'}")
    return errors == 0 and worst <= tol


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profiles", default=os.path.join(ROOT, "payload_profiles.yaml"))
    ap.add_argument("--profile", default="P2b")
    ap.add_argument("--coupler", default=pb.BENCH_COUPLER)
    ap.add_argument("--tx-port", default="TRx1A")
    ap.add_argument("--freqs", default="915,2200")
    ap.add_argument("--attens", default="41.75,35,30,25,20,15,10,5,0")
    ap.add_argument("--ampl", type=float, default=0.5)
    ap.add_argument("--extra-loss", type=float, default=0.0)
    ap.add_argument("--tol", type=float, default=3.0)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--check", action="store_true", help="recalculate with LibreOffice and compare with payload_budget")
    ap.add_argument("--g40-max-dbm", type=float, default=None, help="calibrated G40 max output (dBm) instead of the datasheet typical")
    a = ap.parse_args(argv)
    data = pb.apply_g40_max(pb.load_profiles(a.profiles), a.g40_max_dbm)
    prof = pb.profile(data, a.profile)
    checks = build(data, prof, a.coupler, _floats(a.freqs), _floats(a.attens), a.ampl, a.extra_loss, a.tol,
                   a.tx_port, a.out)
    print(f"wrote {a.out} ({len(checks)} sweep rows)")
    if a.check:
        return 0 if check(a.out, checks) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
