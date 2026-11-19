#!/usr/bin/env python3
"""mission_sweep_recorder.py: fill the HPA bench workbook while a mission runs.

Run it on the laptop next to the mission planner (Mission page, Run mission).
It never keys anything.  Twice a second it reads the radio's get_config; when
the TX is on at the centre and attenuation of a step in the mission file, it
waits --settle seconds, points the FieldFox at the tone, reads the marker peak,
saves a screenshot named after the step and writes the reading into the
Measured and Analyzer notes columns of a copy of the workbook (the original is
not touched), plus a CSV.  After the step's TX stops it reads the same
frequency again, so the notes show the level with the HPA off.

One radio command besides get_config: a reading more than --stop-over dB above
the expected level, or more than --stop-under dB below it, sends stop_tx (0 turns
either off).  stop_tx does not end the mission, so from then on the recorder sends
it again whenever the radio keys, until --timeout, to hold off the later steps.
Press STOP on the mission page as well.

    python3 bench/mission_sweep_recorder.py --host 192.168.0.23 --analyzer 192.168.10.3 \\
        --mission output/bench/hpa_bench_sweep.dpmg-mission.json \\
        --sheet output/bench/HPA_bench_power_sweep.xlsx

    --selftest   no radio: one analyzer reading and a screenshot as the analyzer is
                 now, written to the first row of a scratch copy of the sheet, to prove
                 the reading, the screenshot and the sheet write before keying.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from dpmg.payload import payload_budget as pb       # noqa: E402

MEASURED_COL, NOTES_COL = "N", "Q"
FREQ_TOL_HZ, ATTEN_TOL_DB = 1e3, 0.13


def load_steps(mission_path: str) -> list:
    """Tone steps of a dpmg-mission file: id, TX centre, attenuation, tone offset
    and amplitude, in run order."""
    with open(mission_path) as f:
        m = json.load(f)
    out = []
    for s in m.get("steps", []):
        p = s.get("params") or {}
        if "tx_atten_db" not in p:
            continue
        out.append({"id": s["id"],
                    "tx_center_hz": float(p.get("tx_center_hz") or p.get("center_hz")),
                    "tx_atten_db": float(p["tx_atten_db"]),
                    "tone_hz": float(p.get("freq", 0.0)),
                    "ampl": float(p.get("ampl", 0.5)),
                    "start_s": float((s.get("schedule") or {}).get("start_s", 0.0))})
    return sorted(out, key=lambda x: x["start_s"])


def match_step(steps: list, done: set, center_hz, atten_db):
    """The not yet recorded step at this TX centre and attenuation, or None."""
    if center_hz is None or atten_db is None:
        return None
    for s in steps:
        if s["id"] in done:
            continue
        if abs(s["tx_center_hz"] - float(center_hz)) <= FREQ_TOL_HZ and \
                abs(s["tx_atten_db"] - float(atten_db)) <= ATTEN_TOL_DB:
            return s
    return None


class Sheet:
    """The filled copy of the workbook: Sweep rows found by frequency (column A,
    MHz) and TX attenuation (column B), saved after every write."""

    def __init__(self, template: str, out_path: str, tx_port: str = ""):
        from openpyxl import load_workbook
        shutil.copyfile(template, out_path)
        self.path = out_path
        self.wb = load_workbook(out_path)
        self.ws = self.wb["Sweep"]
        if tx_port and "Inputs" in self.wb.sheetnames:
            wi = self.wb["Inputs"]
            for r in range(1, wi.max_row + 1):
                if str(wi[f"A{r}"].value or "").startswith("G40 TX port"):
                    wi[f"B{r}"] = tx_port
            self.wb.save(out_path)
        self.rows = {}
        for r in range(5, self.ws.max_row + 1):
            a, b = self.ws[f"A{r}"].value, self.ws[f"B{r}"].value
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                self.rows[(round(float(a), 3), round(float(b), 2))] = r

    def row_for(self, freq_mhz: float, atten_db: float):
        return self.rows.get((round(freq_mhz, 3), round(atten_db, 2)))

    def write(self, row: int, measured_dbm: float, note: str) -> None:
        self.ws[f"{MEASURED_COL}{row}"] = round(float(measured_dbm), 2)
        self.ws[f"{NOTES_COL}{row}"] = note
        self.wb.save(self.path)

    def append_note(self, row: int, extra: str) -> None:
        cur = self.ws[f"{NOTES_COL}{row}"].value or ""
        self.ws[f"{NOTES_COL}{row}"] = (cur + "; " + extra) if cur else extra
        self.wb.save(self.path)


def extra_loss_db(text, f_mhz: float) -> float:
    """--extra-loss: one number, or per frequency as 915:3.83,2200:7.51 (the nearest
    listed frequency applies)."""
    t = str(text)
    if ":" not in t:
        return float(t)
    pairs = [tuple(float(x) for x in p.split(":")) for p in t.split(",") if p.strip()]
    return min(pairs, key=lambda fv: abs(fv[0] - f_mhz))[1]


def expected_dbm(data, prof, step, extra_loss, coupler) -> float:
    backoff = -20.0 * math.log10(step["ampl"]) if step["ampl"] > 0 else 0.0
    f_mhz = step["tx_center_hz"] / 1e6
    x = pb.coupled_port_expected(data, prof, f_mhz, step["tx_atten_db"],
                                 backoff, extra_loss_db(extra_loss, f_mhz), coupler)
    return float(x["coupled_dbm"])


def measure(fx, step, a, shots_dir: str, tag: str) -> dict:
    """Analyzer on the tone: fixed input attenuation and reference level, peak
    over --sweeps sweeps, screenshot, back to continuous.  An ADC over range
    (FieldFox error 210) adds up to 10 dB of input attenuation and sweeps again,
    up to --fx-atten-max."""
    tone = step["tx_center_hz"] + step["tone_hz"]
    att = float(a.fx_atten)
    while True:
        # preamp None: the N9912A has no preamp header and shows the error on screen
        fx.setup(center_hz=tone, span_hz=a.span, rbw_hz=a.rbw, ref_dbm=a.ref, atten_db=att, preamp=None)
        try:
            dbm, hz = fx.peak(sweeps=a.sweeps)
            break
        except Exception as e:
            if "over range" not in str(e).lower() or att >= a.fx_atten_max:
                raise
            att = min(att + 10.0, float(a.fx_atten_max))
            print(f"  {step['id']}: analyzer over range, input attenuation up to {att:g} dB")
    shot = os.path.join(shots_dir, f"{a.tx_port}_{step['id']}_{tag}.png" if a.tx_port
                        else f"{step['id']}_{tag}.png")
    try:
        fx.screenshot(shot)
    except Exception as e:                     # a missing screenshot is not a lost reading
        shot = f"screenshot failed: {e}"
    try:
        fx.resume()
    except Exception:
        pass
    return {"dbm": float(dbm), "hz": float(hz), "shot": shot, "fx_atten_db": att}


def run(a, link, fx, sheet, steps, data, prof, csv_path, shots_dir, clock=time.monotonic,
        sleep=time.sleep, now=dt.datetime.now) -> list:
    """The recorder loop.  ``link`` needs cmd(name, params): (status, result)."""
    done, rows = set(), []
    pending_off = None                 # (step, row) waiting for the TX to stop
    seen = None                        # (step id, first time seen keyed)
    t_end = clock() + a.timeout
    with open(csv_path, "w", newline="") as fcsv:
        w = csv.writer(fcsv)
        w.writerow(["time", "step", "freq_mhz", "tx_atten_db", "expected_dbm", "measured_dbm",
                    "marker_hz", "delta_db", "off_dbm", "screenshot"])

        def csv_row(s_, rec_):
            off_ = rec_.get("off_dbm")
            w.writerow([rec_["time"], s_["id"], s_["tx_center_hz"] / 1e6, s_["tx_atten_db"],
                        round(rec_["expected_dbm"], 2), round(rec_["dbm"], 2), round(rec_["hz"], 1),
                        round(rec_["delta_db"], 2), "" if off_ is None else round(off_, 2), rec_["shot"]])
            fcsv.flush()

        stopped = False
        # keep going after the last reading until its off reading is in
        while clock() < t_end and (len(done) < len(steps) or pending_off is not None):
            st, cfg = link.cmd("get_config", {})
            cfg = cfg or {}
            keyed = bool(cfg.get("tx_active"))
            center, att = cfg.get("tx_center_hz"), cfg.get("tx_gain")
            if pending_off is not None and (not keyed or stopped):
                s_off, r_off, rec = pending_off
                sleep(a.off_delay)
                try:
                    tone = s_off["tx_center_hz"] + s_off["tone_hz"]
                    off = float(fx.marker_at(tone))
                    fx.resume()
                except Exception as e:
                    off = None
                    print(f"  {s_off['id']}: off reading failed: {e}")
                rec["off_dbm"] = off
                if r_off is not None and off is not None:
                    sheet.append_note(r_off, f"HPA off after unkey {off:.1f} dBm")
                csv_row(s_off, rec)
                print(f"  {s_off['id']}: off {('%.1f dBm' % off) if off is not None else 'n/a'}")
                pending_off = None
                continue
            if stopped:                          # hold the mission's later steps off
                if keyed:
                    print(f"  keyed again at {float(att or 0):g} dB after a stop: sending stop_tx")
                    link.cmd("stop_tx", {})
                sleep(a.poll)
                continue
            step = match_step(steps, done, center, att) if keyed else None
            if step is None:
                seen = None
                sleep(a.poll)
                continue
            if seen is None or seen[0] != step["id"]:
                seen = (step["id"], clock())
                print(f"{step['id']}: keyed at {center / 1e6:.3f} MHz, {float(att):g} dB; settling {a.settle:g} s")
            if clock() - seen[1] < a.settle:
                sleep(a.poll)
                continue
            exp = expected_dbm(data, prof, step, a.extra_loss, a.coupler)
            row = sheet.row_for(step["tx_center_hz"] / 1e6, step["tx_atten_db"])
            try:
                m = measure(fx, step, a, shots_dir, "on")
            except Exception as e:
                # one bad reading must not end the recording while the mission keys on
                done.add(step["id"])
                print(f"  {step['id']}: analyzer error, step not recorded: {e}")
                if row is not None:
                    sheet.append_note(row, f"analyzer error {now().strftime('%H:%M:%S')}: {e}")
                continue
            delta = m["dbm"] - exp
            note = (f"marker {m['hz'] / 1e6:.4f} MHz, RBW {a.rbw / 1e3:g} kHz, ref {a.ref:g} dBm, "
                    f"input atten {m['fx_atten_db']:g} dB, {now().strftime('%H:%M:%S')}, "
                    f"{os.path.basename(m['shot']) if os.path.isabs(str(m['shot'])) else m['shot']}")
            if row is not None:
                sheet.write(row, m["dbm"], note)
            done.add(step["id"])
            rec = {"time": now().isoformat(timespec="seconds"), "expected_dbm": exp, "delta_db": delta, **m}
            rows.append({"step": step["id"], "row": row, **rec})
            print(f"  {step['id']}: measured {m['dbm']:.1f} dBm, expected {exp:.1f}, delta {delta:+.1f} dB, "
                  f"sheet row {row if row is not None else 'none'}")
            under = getattr(a, "stop_under", 0.0)
            if a.stop_over > 0 and delta > a.stop_over:
                print(f"  {delta:+.1f} dB is over the {a.stop_over:g} dB limit: sending stop_tx")
                link.cmd("stop_tx", {})
                stopped = True
            elif under > 0 and delta < -under:
                print(f"  {delta:+.1f} dB is under the {under:g} dB limit, RF is not reaching the analyzer "
                      "as planned: sending stop_tx")
                link.cmd("stop_tx", {})
                stopped = True
            pending_off = (step, row, rec)
        if pending_off is not None:              # timed out before the TX stopped
            csv_row(pending_off[0], pending_off[2])
    return rows


class _LinkCmd:
    """checkout_common.Link with the (status, result) call the loop uses."""

    def __init__(self, host, port):
        import checkout_common as cc
        self.cc = cc
        self.link = cc.Link(host, port)

    def cmd(self, name, params, timeout=15.0):
        a = self.cc.safe_cmd(self.link, name, params, timeout=timeout)
        return getattr(a, "status", None), (getattr(a, "result", None) or {})


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=os.environ.get("DPMG_G40_HOST", "192.168.0.23"))
    ap.add_argument("--port", type=int, default=47810)
    ap.add_argument("--analyzer", default="192.168.10.3", help="FieldFox host[:port]")
    ap.add_argument("--mission", required=True, help="the dpmg-mission JSON the planner runs")
    ap.add_argument("--sheet", required=True, help="HPA_bench_power_sweep.xlsx (left untouched; a copy is filled)")
    ap.add_argument("--out-dir", default=None, help="where the filled copy, CSV and screenshots go "
                                                    "(default: beside the sheet)")
    ap.add_argument("--tx-port", default="", help="G40 TX jack of this run (TRx1A, TRx2A, TRx1B): names the "
                                                 "filled copy, CSV and screenshots and sets the sheet's TX port")
    ap.add_argument("--profiles", default=os.path.join(ROOT, "payload_profiles.yaml"))
    ap.add_argument("--profile", default="P2b")
    ap.add_argument("--coupler", default=pb.BENCH_COUPLER)
    ap.add_argument("--g40-max-dbm", type=float, default=13.6, help="calibrated G40 output for the expected level")
    ap.add_argument("--extra-loss", default="0", help="pad or cable on the coupled port, dB; one number "
                    "or per frequency, 915:3.83,2200:7.51")
    ap.add_argument("--settle", type=float, default=4.0, help="seconds keyed before the reading")
    ap.add_argument("--off-delay", type=float, default=1.0, help="seconds after unkey before the off reading")
    ap.add_argument("--poll", type=float, default=0.5)
    ap.add_argument("--timeout", type=float, default=1800.0, help="give up after this many seconds")
    ap.add_argument("--span", type=float, default=200e3)
    ap.add_argument("--rbw", type=float, default=10e3)
    ap.add_argument("--ref", type=float, default=20.0, help="analyzer reference level, dBm")
    ap.add_argument("--fx-atten", type=float, default=30.0,
                    help="analyzer input attenuation, dB (20 dB overloads the N9912A ADC near +14 dBm)")
    ap.add_argument("--fx-atten-max", type=float, default=31.0,
                    help="highest input attenuation the over range retry goes to (N9912A maximum 31 dB)")
    ap.add_argument("--sweeps", type=int, default=3)
    ap.add_argument("--stop-under", type=float, default=0.0,
                    help="send stop_tx when a reading is this far below expected (0 = never)")
    ap.add_argument("--stop-over", type=float, default=6.0,
                    help="send stop_tx when a reading is this far above expected (0 = never)")
    ap.add_argument("--selftest", action="store_true", help="no radio: one reading and screenshot into a scratch copy")
    a = ap.parse_args(argv)

    from fieldfox import FieldFox
    steps = load_steps(os.path.expanduser(a.mission))
    if not steps:
        print("no tone steps with tx_atten_db in the mission file")
        return 2
    sheet_in = os.path.expanduser(a.sheet)
    out_dir = os.path.expanduser(a.out_dir or os.path.dirname(os.path.abspath(sheet_in)))
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    shots_dir = os.path.join(out_dir, "screenshots")
    os.makedirs(shots_dir, exist_ok=True)
    os.makedirs(os.path.join(out_dir, "sweeps"), exist_ok=True)
    base = os.path.splitext(os.path.basename(sheet_in))[0]
    kind = ("selftest" if a.selftest else "filled") + (f"_{a.tx_port}" if a.tx_port else "")
    sheet = Sheet(sheet_in, os.path.join(out_dir, f"{base}_{kind}_{stamp}.xlsx"), a.tx_port)
    csv_path = os.path.join(out_dir, "sweeps", f"mission_sweep_{kind}_{stamp}.csv")
    data = pb.apply_g40_max(pb.load_profiles(a.profiles), a.g40_max_dbm)
    prof = pb.profile(data, a.profile)
    missing = [s["id"] for s in steps if sheet.row_for(s["tx_center_hz"] / 1e6, s["tx_atten_db"]) is None]
    print(f"{len(steps)} steps, sheet rows found for {len(steps) - len(missing)}"
          + (f"; no row for {', '.join(missing)}" if missing else ""))
    host, _, port = a.analyzer.partition(":")
    fx = FieldFox(host, int(port or 5025))
    print(f"analyzer: {fx.idn}")
    try:
        if a.selftest:
            s = steps[0]
            m = measure(fx, s, a, shots_dir, "selftest")
            row = sheet.row_for(s["tx_center_hz"] / 1e6, s["tx_atten_db"])
            sheet.write(row, m["dbm"], f"SELFTEST, no RF: marker {m['hz'] / 1e6:.4f} MHz, "
                                       f"{os.path.basename(str(m['shot']))}")
            print(f"selftest: {m['dbm']:.1f} dBm at {m['hz'] / 1e6:.4f} MHz, screenshot {m['shot']}, "
                  f"written to row {row} of {sheet.path}")
            return 0
        print(f"filling {sheet.path}\nCSV {csv_path}\nwaiting for the mission (Run mission on the Mission page)")
        rows = run(a, _LinkCmd(a.host, a.port), fx, sheet, steps, data, prof, csv_path, shots_dir)
        print(f"done: {len(rows)} of {len(steps)} steps recorded in {sheet.path}")
        return 0 if len(rows) == len(steps) else 1
    finally:
        fx.close()


if __name__ == "__main__":
    sys.exit(main())
