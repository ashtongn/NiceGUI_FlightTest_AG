"""
test_iqv02_status_agreement.py: IQ Validator status line vs Field Validation table.

IQV-02: the Validator tab must not show every
field row PASS while the status line reads "FAIL - 1/1 fields matched". The verdict the NiceGUI tab renders must be the same one the field
rows imply: PASS exactly when every row matched, FAIL otherwise, and a decoded
bit string shorter than the expected fields must show up as a FAIL row
(reason "len a!=b"), never as a hidden extra condition on the status line.

Three checks, Encoder: Validator round trip like test_encoder_validator_roundtrip:
  1. full file: every row PASS: verdict PASS (both the "pass" key and the
     "passed" alias)
  2. truncated file: decoded length < expected: the field row is FAIL with a
     length reason, and the verdict is FAIL (agreement in the failing direction)
  3. the NiceGUI tab reads the key the service returns (static check on the
     Validator tab source), so the two cannot drift apart again

Exit 0 = all passed.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from dpmg.ui.fsk_encoder import run_batch_encode                  # noqa: E402
from dpmg.ui.data_validation import read_iq_file                  # noqa: E402
from dpmg.services.validator_service import validate_iq_with_plots  # noqa: E402

SETTINGS = dict(modulation="FSK", symbol_rate=4800, samples_per_bit=40,
                tone0=1200, tone1=2200)
PAYLOAD_HEX = "A5C30F"


def gui_status_line(res):
    """Exactly what dpmg/web/nicegui_app.py validate_iq() renders."""
    passed = bool(res.get("pass", res.get("passed", False)))
    return f"{'PASS' if passed else 'FAIL'} - {res.get('matched_fields', 0)}/{res.get('field_count', 0)} fields matched"


def gui_rows(res):
    """The Field Validation table's status column, as the tab builds it."""
    return ["PASS" if f.get("match", False) else "FAIL" for f in res.get("results", [])]


def main() -> int:
    checks = []

    def chk(name, ok, detail=""):
        checks.append(bool(ok))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))

    tmp = tempfile.mkdtemp(prefix="iqv02_")
    spec = {"settings": SETTINGS, "fields": [{"hex": PAYLOAD_HEX, "desc": "payload"}]}
    jf = os.path.join(tmp, "enc.json")
    json.dump(spec, open(jf, "w"))
    wf = os.path.join(tmp, "full.bin")
    enc = run_batch_encode(jf, wf)
    vspec = {"settings": dict(SETTINGS, sample_rate=enc["sample_rate"]),
             "fields": spec["fields"]}
    vjf = os.path.join(tmp, "val.json")
    json.dump(vspec, open(vjf, "w"))

    # 1. full file: all rows PASS: status line PASS
    res = validate_iq_with_plots(wf, vjf, {})
    rows = gui_rows(res)
    line = gui_status_line(res)
    chk("full file: every field row PASS", rows and all(r == "PASS" for r in rows), f"rows={rows}")
    chk("full file: status line PASS", line.startswith("PASS"), line)
    chk("full file: 'passed' alias equals 'pass'", res.get("passed") is res.get("pass") is True)
    chk("full file: verdict == all rows matched", bool(res["pass"]) == all(r == "PASS" for r in rows))

    # 2. truncated file: decoded shorter than expected: FAIL row with a length
    #    reason, and the verdict FAILs for the same reason the operator can see
    raw = np.fromfile(wf, dtype=np.uint8)
    keep = (int(len(raw) * 0.6) // 4) * 4
    cut = os.path.join(tmp, "cut.bin")
    raw[:keep].tofile(cut)
    res_cut = validate_iq_with_plots(cut, vjf, {})
    rows_cut = gui_rows(res_cut)
    line_cut = gui_status_line(res_cut)
    f0 = res_cut["results"][0]
    chk("truncated: decoded bits shorter than expected",
        res_cut["decoded_bits_len"] < res_cut["expected_bits_len"],
        f"{res_cut['decoded_bits_len']} < {res_cut['expected_bits_len']} "
        f"({len(read_iq_file(cut))} of {len(read_iq_file(wf))} samples)")
    chk("truncated: field row is FAIL with a length reason",
        rows_cut == ["FAIL"] and f0["decoded_len"] != f0["expected_len"],
        f"decoded_len={f0['decoded_len']} expected_len={f0['expected_len']}")
    chk("truncated: status line FAIL", line_cut.startswith("FAIL"), line_cut)
    chk("truncated: verdict == all rows matched",
        bool(res_cut["pass"]) == all(r == "PASS" for r in rows_cut))

    # 3. the Validator tab reads the key the service returns
    src = open(os.path.join(ROOT, "dpmg", "web", "nicegui_app.py")).read()
    m = re.search(r'passed = bool\(result\.get\("pass"', src)
    chk("nicegui_app.py status line reads result['pass']", m is not None)

    n_ok = sum(checks)
    print(f"\nresult: {n_ok}/{len(checks)} checks passed")
    return 0 if all(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
