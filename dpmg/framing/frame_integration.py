"""
frame_integration.py: the glue layer between frame_spec and the DPMG app.

Keeps the frame logic in one place so the encoder tab, scenario_tab and the
channelizer only need small call sites.

Provides:
  * CRC_PRESETS + crc_params(): map the encoder's CRC dropdown to params
  * descriptor_from_ui(...): encoder field-editor rows: validated descriptor
  * make_frame_bits(...): author bits for a descriptor (scenario/encoder side)
  * detect_and_tag(...): channelizer side: multi-hypothesis detect + policy
 

Interfaces are plain data (dicts/lists/ints) so this module does not depend on
the app's object types.
"""
from __future__ import annotations
from dpmg.framing import frame_spec as fs


# Best-known standard parameters. Reconcile against the app's crc_util before
# trusting on-air (see reconcile_crc); the app's "CCITT"/"IBM" may differ.
CRC_PRESETS = {
    "None":         None,
    "CRC-8":        {"width": 8,  "poly": 0x07,       "init": 0x00,       "refin": False, "refout": False, "xorout": 0x00},
    "CRC-9":        {"width": 9,  "poly": 0x059,      "init": 0x000,      "refin": False, "refout": False, "xorout": 0x000},   # CRC-9/DMR-ish; verify
    "CRC-16-CCITT": {"width": 16, "poly": 0x1021,     "init": 0xFFFF,     "refin": False, "refout": False, "xorout": 0x0000},  # CCITT-FALSE
    "CRC-16-IBM":   {"width": 16, "poly": 0x8005,     "init": 0x0000,     "refin": True,  "refout": True,  "xorout": 0x0000},  # ARC
    "CRC-32":       {"width": 32, "poly": 0x04C11DB7, "init": 0xFFFFFFFF, "refin": True,  "refout": True,  "xorout": 0xFFFFFFFF},
}


def crc_params(choice, custom=None):
    """Resolve a CRC dropdown choice to a param dict (or None).
    choice: preset name, or 'Custom...' with `custom` = {width,poly,init,refin,refout,xorout}.
    """
    if choice in (None, "None"):
        return None
    if choice == "Custom...":
        if not custom:
            raise fs.FrameSpecError("Custom CRC selected but no params given")
        return {
            "width": fs._as_int(custom.get("width", 16), "crc.width"),
            "poly": fs._as_int(custom.get("poly", 0x1021), "crc.poly"),
            "init": fs._as_int(custom.get("init", 0), "crc.init"),
            "refin": bool(custom.get("refin", False)),
            "refout": bool(custom.get("refout", False)),
            "xorout": fs._as_int(custom.get("xorout", 0), "crc.xorout"),
        }
    if choice not in CRC_PRESETS:
        raise fs.FrameSpecError(f"unknown CRC preset {choice!r}")
    return dict(CRC_PRESETS[choice]) if CRC_PRESETS[choice] else None


def descriptor_from_ui(modulation, symbol_rate_hz, field_rows,
                       crc_choice="None", crc_covers=None, crc_custom=None,
                       bit_order="msb_first", crc_bit_order="msb_first"):
    """Assemble a validated descriptor from the encoder field-editor rows.

    field_rows: ordered list of dicts, each like a frame_spec field, e.g.
        {"name":"sync","role":"sync","width_bits":16,"value":"0xB23B","max_errors":2}
        {"name":"payload","role":"data","width_bits":{"from_field":"length","scale":8}}
    A crc-role field is appended automatically when a CRC is selected (unless the
    rows already include one). Raises FrameSpecError on any inconsistency.
    """
    fields = [dict(r) for r in field_rows]
    cp = crc_params(crc_choice, crc_custom)

    if cp is not None:
        if not any(f.get("role") == "crc" for f in fields):
            fields.append({"name": "crc", "role": "crc", "width_bits": cp["width"]})
        covers = list(crc_covers or [f["name"] for f in fields
                                      if f.get("role") in ("match", "length", "data", "rest")])
        crc = {**cp, "covers": covers, "crc_bit_order": crc_bit_order}
    else:
        crc = None

    desc = {
        "version": fs.VERSION,
        "modulation": modulation,
        "symbol_rate_hz": symbol_rate_hz,
        "bit_order": bit_order,
        "fields": fields,
        "crc": crc,
    }
    # validate now so authoring errors surface at author time, not on-air
    fs.validate_descriptor(desc)
    return desc


def make_frame_bits(descriptor, values=None, field_widths=None):
    """Author the concrete bit list for a descriptor (thin wrapper on build_frame)."""
    return fs.build_frame(descriptor, values=values, field_widths=field_widths)


def detect_and_tag(bits, descriptors, mode="detect_and_tag", total_len=None,
                   gate_requires_crc=True):
    """Multi-hypothesis frame detection over one demodulated burst.

    descriptors: {name: descriptor}. Each is a hypothesis; a burst may match one,
        several, or none. This is how you hunt multiple candidate preambles at once.
    mode:
        'detect_and_tag': surface every hypothesis whose sync locked, annotated
                            with match/crc status. (Research default: see everything.)
        'match_gated': surface only hypotheses where all declared match fields
                            equal their values (and, if gate_requires_crc, crc_ok).
    Returns a list of result dicts (possibly empty). Never raises.
    """
    out = []
    for name, desc in descriptors.items():
        r = fs.parse_frame(desc, bits, total_len=total_len)
        any_sync = r.get("any_sync")
        # a descriptor with sync fields must lock to count as a detection;
        # a descriptor with no sync fields is always a candidate (any_sync is None)
        detected = True if any_sync is None else bool(any_sync)
        if not detected:
            continue

        all_match = r.get("all_match")   # None when no match fields
        crc_ok = r.get("crc_ok")         # None when no CRC
        rec = {
            "descriptor": name,
            "detected": True,
            "sync_results": r.get("sync_results"),
            "match_results": r.get("match_results"),
            "all_match": all_match,
            "crc_ok": crc_ok,
            "fields": r.get("fields"),
            "consumed_bits": r.get("consumed_bits"),
        }

        if mode == "match_gated":
            if all_match is False:
                continue
            if gate_requires_crc and crc_ok is False:
                continue

        out.append(rec)
    return out


def summarize(rec):
    """One-line human summary of a detect_and_tag record (for logs/GUI)."""
    parts = [rec["descriptor"]]
    if rec.get("all_match") is not None:
        parts.append("match" if rec["all_match"] else "NOMATCH")
    if rec.get("crc_ok") is not None:
        parts.append("crc_ok" if rec["crc_ok"] else "CRC_FAIL")
    ids = []
    for m in (rec.get("match_results") or []):
        ids.append("%s=0x%X%s" % (m["field"], m["got"], "" if m["ok"] else "!"))
    if ids:
        parts.append("[" + " ".join(ids) + "]")
    return " ".join(parts)


# Encoder 'desc' values that mean "this is the correlation preamble"
_SYNC_DESCS = {"sync", "preamble"}
# Map encoder 'enc' CRC labels: CRC_PRESETS keys
_ENC_CRC_MAP = {
    "crc-ccitt": "CRC-16-CCITT",
    "crc-9": "CRC-9",
    "crc-32": "CRC-32",
    # DMR composite encodings carry a CRC too; map the CRC portion best-effort
    "dmr cach (crc-9 over 15b)": "CRC-9",
    "dmr data block (crc-ccitt + bptc196/96": "CRC-16-CCITT",
}


def _enc_to_crc_choice(enc):
    if not enc:
        return None
    e = enc.strip().lower()
    if not e.startswith("crc") and "crc" not in e:
        return None
    for key, choice in _ENC_CRC_MAP.items():
        if key in e:
            return choice
    if e.startswith("crc"):
        return "CRC-16-CCITT"  # generic CRC fallback
    return None


def descriptor_from_encoder_fields(svc_fields, settings):
    """Best-effort descriptor from the encoder's existing per-field metadata:
    no new UI. Reads each field's desc (Sync/Preamble/... ) + enc (CRC-*) + bin.

    Returns a validated descriptor, or None if there's no sync field to detect on
    (the 'if available' fallback: nothing breaks downstream when None).
    Never raises.
    """
    try:
        rows = []
        counts = {}
        covers = []
        crc_choice = "None"

        def _name(role):
            n = counts.get(role, 0)
            counts[role] = n + 1
            return role if n == 0 else f"{role}{n}"

        for f in (svc_fields or []):
            binv = (f.get("bin") or "").strip()
            if not binv:
                # hex-only field: derive bits from hex length
                hexv = (f.get("hex") or "").strip()
                if hexv:
                    binv = bin(int(hexv, 16))[2:].zfill(len(hexv) * 4)
            width = len(binv)
            if width <= 0:
                continue
            val = int(binv, 2)
            desc = (f.get("desc") or "").strip().lower()
            enc = f.get("encoding") or f.get("enc") or ""

            if desc in _SYNC_DESCS:
                rows.append({"name": _name("sync"), "role": "sync",
                             "width_bits": width, "value": val, "max_errors": 0})
                continue

            cc = _enc_to_crc_choice(enc)
            if cc:
                # this field is a CRC: add preceding data to covers, mark crc
                crc_choice = cc
                rows.append({"name": _name("crc"), "role": "crc",
                             "width_bits": width})
                continue

            # ordinary data field
            nm = _name("data")
            rows.append({"name": nm, "role": "data", "width_bits": width})
            covers.append(nm)

        if not any(r["role"] == "sync" for r in rows):
            return None  # nothing to correlate on: no descriptor

        return descriptor_from_ui(
            settings.get("modulation", "BPSK"),
            float(settings.get("symbol_rate", settings.get("baud", 0)) or 0),
            rows,
            crc_choice=crc_choice,
            crc_covers=(covers or None))
    except Exception:
        return None
