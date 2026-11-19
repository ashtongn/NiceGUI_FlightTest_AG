"""
E2E: QPSK-DSSS + spreading-code families through the validation/decode path.

Exercises the wiring added for the NiceGUI validation tab:
  validator_service.validate_iq(overrides): data_validation.run_batch_validate
  then get_demodulator(code_spec, dsss_modem): DSSSDemodulator.

For each modem (bpsk / qpsk) and each code family (seed / m / gold / custom) the
test modulates known bits with DSSSModulator, writes an interleaved-int16 .iq file
plus an expected JSON, then confirms validate_iq threads code_spec + dsss_modem through
and the field decodes cleanly (pass == True). Also checks normalize_modulation /
SUPPORTED_MODULATIONS accept the QPSK-DSSS / DSSS-QPSK names. Deterministic.
Exit 0 = all pass.
"""
from __future__ import annotations

import json
import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np

from dpmg.modulation.spreading_codes import code_len
from dpmg.modulation.spread_spectrum import DSSSModulator
from dpmg.ui.data_validation import normalize_modulation, SUPPORTED_MODULATIONS
from dpmg.services.validator_service import validate_iq, validate_iq_with_plots

checks = []


def chk(name, ok, detail=""):
    checks.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")


def _write_iq(path, iq):
    """Interleaved little-endian int16 I/Q: the raw path in read_iq_file."""
    peak = float(np.max(np.abs(iq))) or 1.0
    scaled = (np.asarray(iq) / peak) * 30000.0
    inter = np.empty(scaled.size * 2, dtype=np.int16)
    inter[0::2] = np.round(scaled.real).astype(np.int16)
    inter[1::2] = np.round(scaled.imag).astype(np.int16)
    with open(path, "wb") as fh:
        fh.write(struct.pack(f"<{inter.size}h", *inter.tolist()))


def _run_case(tmp, bits, modem, code_spec, sf):
    """Modulate, then validate through the service path.

    BPSK decodes at absolute phase, so a single call must pass. QPSK-DSSS carries
    a 4-fold rotation ambiguity resolved at the frame layer, not by this raw-bit
    decode tool, so all 4 absolute-phase rotations are tried and one must
    decode cleanly. That still proves code_spec + dsss_modem threaded through
    and the despreading is correct. Returns the best result dict."""
    spc = 4
    symbol_rate = 1200.0
    m = DSSSModulator(symbol_rate, sf * spc, spread_factor=15,
                      code_spec=code_spec, modem=modem)
    base = np.asarray(m.modulate(bits).iq_samples)

    json_path = os.path.join(tmp, f"{modem}_{code_spec['family']}.json")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump({"fields": [{"bin": bits, "desc": "payload", "encoding": "None"}]}, fh)

    modname = "QPSK-DSSS" if modem == "qpsk" else "DSSS"
    rotations = range(4) if modem == "qpsk" else range(1)
    best = None
    for r in rotations:
        iq = base * np.exp(1j * (np.pi / 2) * r)
        iq_path = os.path.join(tmp, f"{modem}_{code_spec['family']}_r{r}.iq")
        _write_iq(iq_path, iq)
        overrides = {
            "modulation": modname,
            "sample_rate": float(m.sample_rate),
            "symbol_rate": symbol_rate,
            "samples_per_bit": sf * spc,
            "spread_factor": 15,
            "pn_seed": 1,
            "code_spec": code_spec,
            "dsss_modem": modem,
        }
        res = validate_iq(iq_path, json_path, overrides)
        if best is None or res["matched_fields"] > best["matched_fields"]:
            best = res
        if res["pass"]:
            break
    return best


def main():
    print("QPSK-DSSS + code families through validation path\n")

    # name resolution
    chk("QPSK-DSSS in SUPPORTED_MODULATIONS", "QPSK-DSSS" in SUPPORTED_MODULATIONS)
    chk("normalize QPSK-DSSS", normalize_modulation("QPSK-DSSS") == "QPSK-DSSS")
    chk("normalize DSSS-QPSK alias", normalize_modulation("DSSS-QPSK") == "QPSK-DSSS")
    chk("normalize qpsk_dsss lower/underscore",
        normalize_modulation("qpsk_dsss") == "QPSK-DSSS")

    rng = np.random.default_rng(7)
    families = (
        {"family": "seed", "length": 15},
        {"family": "m", "n": 5},
        {"family": "gold", "n": 6, "index": 3},
        {"family": "custom", "chips": "+-+--++-"},
    )

    with tempfile.TemporaryDirectory() as tmp:
        for modem in ("bpsk", "qpsk"):
            print(f" {modem.upper()}-DSSS:")
            for cs in families:
                sf = code_len(cs, 15)
                bits = "".join(rng.integers(0, 2, 240).astype(str))
                res = _run_case(tmp, bits, modem, cs, sf)
                fam = cs["family"]
                chk(f"{modem} {fam} sf={sf} pass={res['pass']}",
                    res["pass"] and res["matched_fields"] == res["field_count"],
                    f"(matched {res['matched_fields']}/{res['field_count']})")

        # plots payload must also survive the QPSK-DSSS path (no crash, has keys)
        bits = "".join(rng.integers(0, 2, 64).astype(str))
        cs = {"family": "gold", "n": 6, "index": 3}
        sf = code_len(cs, 15)
        spc = 4
        m = DSSSModulator(1200.0, sf * spc, spread_factor=15, code_spec=cs, modem="qpsk")
        base = np.asarray(m.modulate(bits).iq_samples)
        json_path = os.path.join(tmp, "plots.json")
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump({"fields": [{"bin": bits, "desc": "p", "encoding": "None"}]}, fh)
        plots_ok = False
        for r in range(4):
            iq_path = os.path.join(tmp, f"plots_r{r}.iq")
            _write_iq(iq_path, base * np.exp(1j * (np.pi / 2) * r))
            res = validate_iq_with_plots(iq_path, json_path, {
                "modulation": "QPSK-DSSS", "sample_rate": float(m.sample_rate),
                "symbol_rate": 1200.0, "samples_per_bit": sf * spc,
                "code_spec": cs, "dsss_modem": "qpsk"})
            if res["pass"] and "plots" in res and "fft" in res["plots"]:
                plots_ok = True
                break
        chk("validate_iq_with_plots QPSK-DSSS ok", plots_ok)

    print(f"\nresult: {sum(checks)}/{len(checks)} checks passed")
    return 0 if checks and all(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
