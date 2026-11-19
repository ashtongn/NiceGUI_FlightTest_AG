"""
test_encoder_validator_roundtrip.py: Encoder tab: IQ Validator tab.

Drives the headless cores behind both GUI tabs: run_batch_encode() writes a
.bin waveform from a fields/settings JSON, then run_batch_validate() decodes it
back and compares to the same expected fields. A pass means the Encoder tab and
the IQ Validator tab agree end to end, per modulation.

Exit 0 = all passed.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dpmg.ui.fsk_encoder import run_batch_encode
from dpmg.ui.data_validation import run_batch_validate

# (name, encoder+validator settings). Payload is byte-aligned so symbol counts
# stay even for the 2-bit modulations.
CASES = [
    ("FSK",  dict(modulation="FSK", symbol_rate=4800, samples_per_bit=40,
                  tone0=1200, tone1=2200)),
    ("GFSK", dict(modulation="GFSK", symbol_rate=4800, samples_per_bit=40,
                  tone0=1200, tone1=2200, gfsk_bw_hz=2400)),
    ("4FSK", dict(modulation="4FSK", symbol_rate=4800, samples_per_bit=40,
                  tone0=-1800, tone1=-600, tone2=600, tone3=1800,
                  dibit_mapping="binary")),
    ("BPSK", dict(modulation="BPSK", symbol_rate=4800, samples_per_bit=40)),
    ("QPSK", dict(modulation="QPSK", symbol_rate=4800, samples_per_bit=40)),
]

PAYLOAD_HEX = "A5C30F"


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="encval_")
    checks = []

    for name, settings in CASES:
        spec = {"settings": settings,
                "fields": [{"hex": PAYLOAD_HEX, "desc": "payload"}]}
        jf = os.path.join(tmp, f"{name}.json")
        json.dump(spec, open(jf, "w"))
        wf = os.path.join(tmp, f"{name}.bin")
        try:
            enc = run_batch_encode(jf, wf)
        except Exception as e:
            checks.append(False)
            print(f"  [FAIL] {name:6s} encode raised: {e}")
            continue
        # the validator reads sample_rate from the settings, not the file header,
        # so feed it the rate the encoder actually produced
        vspec = {"settings": dict(settings, sample_rate=enc["sample_rate"]),
                 "fields": [{"hex": PAYLOAD_HEX, "desc": "payload"}]}
        vjf = os.path.join(tmp, f"{name}_v.json")
        json.dump(vspec, open(vjf, "w"))
        try:
            res = run_batch_validate(wf, vjf)
        except Exception as e:
            checks.append(False)
            print(f"  [FAIL] {name:6s} validate raised: {e}")
            continue
        ok = bool(res["pass"])
        checks.append(ok)
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:6s} "
              f"encode {enc['bit_count']}b @ {enc['sample_rate']} Hz -> "
              f"validate matched {res['matched_fields']}/{res['field_count']} "
              f"fields ({res['decoded_bits_len']} bits)")

    print(f"\nresult: {sum(checks)}/{len(checks)} modulations round-tripped")
    return 0 if all(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
