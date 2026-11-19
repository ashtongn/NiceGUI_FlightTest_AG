"""
frame_spec.py: declarative frame descriptor: build (author) + parse (ingest).

Design principles (survey-tool oriented):
  * One descriptor expresses the general case; simpler frames use fewer fields.
  * Single forward pass, backward references only: statically validatable,
    deterministic parse.
  * Strict on author (build_frame raises on any inconsistency),
    lenient on ingest (parse_frame never raises; every field gets a status).
  * CRC covered span is explicit (crc.covers) so encode/decode compute identically.
  * Bit-order pinned per field (global default, per-field override).

Roles: sync | match | length | data | rest | crc
Widths: int | {"from_field","scale","offset"} (signed offset) | (implicit for rest)
present_if: {"field","mask","op","value"}  op in eq|ne|any|all: (val & mask) op value

CRC covered region must resolve to a whole number of bytes (validated at build).
Covered bits are grouped MSB-first into bytes, then run through a standard
Rocksoft-model CRC (verified against known vectors in the test harness).
"""
from __future__ import annotations

VERSION = 1
_BIT_ORDERS = ("msb_first", "lsb_first")
_ROLES = ("sync", "match", "length", "data", "rest", "crc")
_OPS = ("eq", "ne", "any", "all")

# parse-side per-field status
OK = "ok"
MISMATCH = "mismatch"
TRUNCATED = "truncated"
ABSENT = "absent"
UNDECODED = "undecoded"


class FrameSpecError(Exception):
    """Raised only by author-side operations (validate/build). Parse never raises."""


# helpers

def _as_int(x, what="value"):
    if isinstance(x, bool):
        raise FrameSpecError(f"{what}: bool not allowed")
    if isinstance(x, int):
        return x
    if isinstance(x, str):
        s = x.strip()
        try:
            return int(s, 0)  # handles 0x.., 0b.., decimal
        except ValueError:
            raise FrameSpecError(f"{what}: cannot parse int from {x!r}")
    raise FrameSpecError(f"{what}: expected int or str, got {type(x).__name__}")


def _reflect(x, n):
    r = 0
    for i in range(n):
        if x & (1 << i):
            r |= 1 << (n - 1 - i)
    return r


# Single source of truth for CRC: delegate to the app's crc_util when present
# (verified bit-identical to the fallback below via reconcile_crc_on_g40.py).
# The built-in fallback keeps this module runnable standalone (tests, tooling)
# where crc_util isn't importable.
try:
    from dpmg.framing import crc_util as _crc_util  # app's authoritative engine
    _get_params = getattr(_crc_util, "get_params", None)
    _app_crc = getattr(_crc_util, "crc_compute", None)
    _CrcParams = getattr(_crc_util, "CrcParams", None)
except Exception:
    _crc_util = _get_params = _app_crc = _CrcParams = None


def _crc_builtin(data, width, poly, init, refin, refout, xorout):
    """Fallback Rocksoft-model CRC over whole bytes. width >= 8."""
    if width < 8:
        raise FrameSpecError("CRC width < 8 unsupported")
    mask = (1 << width) - 1
    topbit = 1 << (width - 1)
    reg = init & mask
    for byte in data:
        if refin:
            byte = _reflect(byte, 8)
        reg ^= (byte << (width - 8)) & mask
        for _ in range(8):
            if reg & topbit:
                reg = ((reg << 1) ^ poly) & mask
            else:
                reg = (reg << 1) & mask
    if refout:
        reg = _reflect(reg, width)
    return (reg ^ xorout) & mask


def crc_compute_bytes(data, width, poly, init, refin, refout, xorout):
    """CRC over whole bytes. Uses crc_util (app engine) if importable, else the
    identical built-in fallback."""
    if width < 8:
        raise FrameSpecError("CRC width < 8 unsupported")
    if _app_crc is not None:
        d = {"width": width, "poly": poly, "init": init,
             "refin": refin, "refout": refout, "xorout": xorout}
        try:
            if _get_params is not None:
                return int(_app_crc(bytes(data), _get_params(d)))
            if _CrcParams is not None:
                return int(_app_crc(bytes(data), _CrcParams(
                    width=width, poly=poly, init=init,
                    refin=refin, refout=refout, xorout=xorout)))
        except Exception:
            pass  # fall through to built-in on any signature/shape surprise
    return _crc_builtin(data, width, poly, init, refin, refout, xorout)


def int_to_bits(value, width, bit_order):
    if width <= 0:
        return []
    bits = [(value >> (width - 1 - i)) & 1 for i in range(width)]  # msb_first
    if bit_order == "lsb_first":
        bits.reverse()
    return bits


def bits_to_int(bits, bit_order):
    seq = list(reversed(bits)) if bit_order == "lsb_first" else bits
    v = 0
    for b in seq:
        v = (v << 1) | (b & 1)
    return v


def bits_to_bytes_msb(bits):
    """Group a bit list (len % 8 == 0) into bytes, MSB-first within each byte."""
    if len(bits) % 8 != 0:
        raise FrameSpecError("bit region not byte-aligned")
    out = bytearray()
    for i in range(0, len(bits), 8):
        b = 0
        for j in range(8):
            b = (b << 1) | (bits[i + j] & 1)
        out.append(b)
    return bytes(out)


# validation

def validate_descriptor(d):
    """Author-side structural validation. Raises FrameSpecError. Returns a
    normalized copy (hex strings: ints, defaults filled)."""
    if not isinstance(d, dict):
        raise FrameSpecError("descriptor must be a dict")
    if d.get("version", VERSION) != VERSION:
        # forward-compat: warn-and-proceed is the ingest stance, but author-side
        # we hard-check so we don't silently author against a schema we can't honor
        raise FrameSpecError(f"unsupported descriptor version {d.get('version')}")
    gbo = d.get("bit_order", "msb_first")
    if gbo not in _BIT_ORDERS:
        raise FrameSpecError(f"bad global bit_order {gbo!r}")
    fields = d.get("fields")
    if not isinstance(fields, list) or not fields:
        raise FrameSpecError("descriptor.fields must be a non-empty list")

    norm_fields = []
    seen = set()
    rest_count = 0
    rest_index = None
    for idx, f in enumerate(fields):
        if not isinstance(f, dict):
            raise FrameSpecError(f"field[{idx}] must be a dict")
        name = f.get("name")
        if not name or not isinstance(name, str):
            raise FrameSpecError(f"field[{idx}] missing name")
        if name in seen:
            raise FrameSpecError(f"duplicate field name {name!r}")
        seen.add(name)
        role = f.get("role")
        if role not in _ROLES:
            raise FrameSpecError(f"{name}: bad role {role!r}")
        bo = f.get("bit_order", gbo)
        if bo not in _BIT_ORDERS:
            raise FrameSpecError(f"{name}: bad bit_order {bo!r}")

        nf = {"name": name, "role": role, "bit_order": bo}

        # width
        if role == "rest":
            rest_count += 1
            rest_index = idx
            nf["width_bits"] = "rest"
        else:
            w = f.get("width_bits")
            if isinstance(w, dict):
                ref = w.get("from_field")
                if ref not in seen or ref == name:
                    raise FrameSpecError(f"{name}: width from_field {ref!r} must be an EARLIER field")
                # the referenced field must be a length role
                prev = next(p for p in norm_fields if p["name"] == ref)
                if prev["role"] != "length":
                    raise FrameSpecError(f"{name}: width from_field {ref!r} must have role 'length'")
                nf["width_bits"] = {
                    "from_field": ref,
                    "scale": _as_int(w.get("scale", 1), f"{name}.scale"),
                    "offset": _as_int(w.get("offset", 0), f"{name}.offset"),
                }
            else:
                wi = _as_int(w, f"{name}.width_bits")
                if wi <= 0:
                    raise FrameSpecError(f"{name}: width_bits must be > 0")
                nf["width_bits"] = wi

        # value / mask / max_errors
        if role in ("sync", "match"):
            if "value" not in f:
                raise FrameSpecError(f"{name}: role {role} requires 'value'")
            nf["value"] = _as_int(f["value"], f"{name}.value")
            if role == "match":
                nf["mask"] = _as_int(f.get("mask", -1), f"{name}.mask")  # -1 means all ones
            if role == "sync":
                nf["max_errors"] = _as_int(f.get("max_errors", 0), f"{name}.max_errors")
            # strict: value must fit fixed width
            if isinstance(nf["width_bits"], int):
                if nf["value"] >= (1 << nf["width_bits"]) or nf["value"] < 0:
                    raise FrameSpecError(
                        f"{name}: value 0x{nf['value']:X} overflows width {nf['width_bits']}")
        elif "value" in f:
            nf["value"] = _as_int(f["value"], f"{name}.value")  # optional constant for data

        # present_if
        pi = f.get("present_if")
        if pi is not None:
            pf = pi.get("field")
            if pf not in seen or pf == name:
                raise FrameSpecError(f"{name}: present_if.field {pf!r} must be an EARLIER field")
            op = pi.get("op")
            if op not in _OPS:
                raise FrameSpecError(f"{name}: present_if.op {op!r} invalid")
            nf["present_if"] = {
                "field": pf,
                "mask": _as_int(pi.get("mask", -1), f"{name}.present_if.mask"),
                "op": op,
                "value": _as_int(pi.get("value", 0), f"{name}.present_if.value"),
            }
        norm_fields.append(nf)

    if rest_count > 1:
        raise FrameSpecError("at most one 'rest' field allowed")
    if rest_index is not None:
        for f in norm_fields[rest_index + 1:]:
            if not isinstance(f["width_bits"], int):
                raise FrameSpecError(
                    f"field {f['name']!r} after 'rest' must be fixed-width (needed as backstop)")

    # crc
    crc = d.get("crc")
    norm_crc = None
    if crc is not None:
        covers = crc.get("covers")
        if not isinstance(covers, list) or not covers:
            raise FrameSpecError("crc.covers must be a non-empty list")
        for c in covers:
            if c not in seen:
                raise FrameSpecError(f"crc.covers references unknown field {c!r}")
        norm_crc = {
            "algo": crc.get("algo", "custom"),
            "width": _as_int(crc.get("width", 16), "crc.width"),
            "poly": _as_int(crc.get("poly", 0x1021), "crc.poly"),
            "init": _as_int(crc.get("init", 0), "crc.init"),
            "refin": bool(crc.get("refin", False)),
            "refout": bool(crc.get("refout", False)),
            "xorout": _as_int(crc.get("xorout", 0), "crc.xorout"),
            "covers": list(covers),
            "crc_bit_order": crc.get("crc_bit_order", "msb_first"),
        }
        if norm_crc["crc_bit_order"] not in _BIT_ORDERS:
            raise FrameSpecError("bad crc_bit_order")
        # there must be exactly one crc-role field, matching crc.width
        crc_fields = [f for f in norm_fields if f["role"] == "crc"]
        if len(crc_fields) != 1:
            raise FrameSpecError("exactly one field with role 'crc' required when crc present")
        cf = crc_fields[0]
        if not isinstance(cf["width_bits"], int) or cf["width_bits"] != norm_crc["width"]:
            raise FrameSpecError("crc field width must equal crc.width")

    return {"version": VERSION, "bit_order": gbo, "fields": norm_fields, "crc": norm_crc}


# present_if eval

def _predicate(pi, resolved):
    if pi is None:
        return True
    if pi["field"] not in resolved:
        return False  # referenced field absent: treat as not-present
    v = resolved[pi["field"]] & pi["mask"]
    t = pi["value"] & pi["mask"]
    op = pi["op"]
    if op == "eq":
        return v == t
    if op == "ne":
        return v != t
    if op == "any":
        return v != 0
    if op == "all":
        return v == pi["mask"] if pi["value"] == pi["mask"] else (v == t)
    return False


def _resolve_width(f, resolved):
    w = f["width_bits"]
    if isinstance(w, int):
        return w
    if w == "rest":
        return None  # computed by caller
    ref = w["from_field"]
    if ref not in resolved:
        raise FrameSpecError(f"{f['name']}: length field {ref!r} not resolved")
    return resolved[ref] * w["scale"] + w["offset"]


# build (author)

def build_frame(descriptor, values=None, field_widths=None):
    """Assemble a concrete frame. Strict: raises FrameSpecError on any problem.

    values:  {name: int}  concrete values for data/length/match/sync overrides.
             sync/match default to descriptor 'value' if not given.
    field_widths: {name: int}  explicit bit width for a 'rest' field (its payload
             size). Also lets a length field be auto-derived: if a length field L
             is unset in `values` and some field F derives width from L, and F's
             width is given in field_widths, L is back-computed.
    Returns: list[int] bits (msb-first stream order).
    """
    d = validate_descriptor(descriptor)
    values = dict(values or {})
    field_widths = dict(field_widths or {})

    # pre-pass: auto-derive length values from field_widths where possible
    for f in d["fields"]:
        w = f["width_bits"]
        if isinstance(w, dict):
            ref = w["from_field"]
            fw = field_widths.get(f["name"])
            if fw is not None:
                derived = (fw - w["offset"])
                if derived % w["scale"] != 0:
                    raise FrameSpecError(
                        f"{f['name']}: width {fw} not consistent with scale/offset of {ref}")
                length_val = derived // w["scale"]
                if ref in values and values[ref] != length_val:
                    raise FrameSpecError(
                        f"{ref}: given value {values[ref]} conflicts with derived {length_val}")
                values[ref] = length_val

    bits = []
    resolved = {}          # name: int value (present fields only)
    field_bits = {}        # name: list[int] emitted
    crc = d["crc"]
    crc_field_name = None
    if crc:
        crc_field_name = next(f["name"] for f in d["fields"] if f["role"] == "crc")

    for f in d["fields"]:
        name = f["role"], f["name"]
        if not _predicate(f.get("present_if"), resolved):
            continue

        if f["role"] == "crc":
            # defer: assemble after all covered fields exist
            resolved[f["name"]] = None
            field_bits[f["name"]] = None
            continue

        # determine value
        if f["role"] in ("sync", "match"):
            val = values.get(f["name"], f["value"])
        elif f["name"] in values:
            val = _as_int(values[f["name"]], f["name"])
        elif "value" in f:
            val = f["value"]
        else:
            raise FrameSpecError(f"{f['name']}: no value provided (role {f['role']})")

        # determine width
        if f["width_bits"] == "rest":
            width = field_widths.get(f["name"])
            if width is None:
                raise FrameSpecError(f"{f['name']}: 'rest' field needs an explicit width in field_widths")
        else:
            width = _resolve_width(f, resolved)
            if f["name"] in field_widths and field_widths[f["name"]] != width:
                raise FrameSpecError(
                    f"{f['name']}: field_widths says {field_widths[f['name']]} but resolves to {width}")

        if val < 0 or val >= (1 << width):
            raise FrameSpecError(f"{f['name']}: value 0x{val:X} overflows width {width}")

        fb = int_to_bits(val, width, f["bit_order"])
        field_bits[f["name"]] = fb
        resolved[f["name"]] = val
        bits.extend(fb)

    # CRC assembly
    if crc:
        covered = []
        for cname in crc["covers"]:
            fb = field_bits.get(cname)
            if fb:  # present + non-crc
                covered.extend(fb)
        if len(covered) % 8 != 0:
            raise FrameSpecError(
                f"CRC covered region is {len(covered)} bits, not byte-aligned")
        data = bits_to_bytes_msb(covered)
        crc_val = crc_compute_bytes(
            data, crc["width"], crc["poly"], crc["init"],
            crc["refin"], crc["refout"], crc["xorout"])
        cf = next(f for f in d["fields"] if f["role"] == "crc")
        cb = int_to_bits(crc_val, crc["width"], crc["crc_bit_order"])
        # place CRC bits at the crc field's position: rebuild stream in field order
        # (simplest correct approach: re-emit in order, substituting crc)
        bits = []
        for f in d["fields"]:
            if f["name"] not in field_bits:
                continue  # was absent
            if f["role"] == "crc":
                bits.extend(cb)
            else:
                fb = field_bits[f["name"]]
                if fb:
                    bits.extend(fb)
        resolved[cf["name"]] = crc_val

    return bits


# parse (ingest)

class ParseResult(dict):
    @property
    def crc_ok(self):
        return self.get("crc_ok")

    @property
    def fields(self):
        return self.get("fields", {})


def parse_frame(descriptor, bits, total_len=None):
    """Lenient: never raises. Best-effort forward pass. Returns ParseResult with
    per-field {value, status, offset, width} plus crc_ok and sync/match summaries.
    total_len defaults to len(bits) (used to size a 'rest' field)."""
    try:
        d = validate_descriptor(descriptor)
    except FrameSpecError as e:
        return ParseResult(error=str(e), fields={}, crc_ok=None)

    bits = list(bits)
    N = len(bits) if total_len is None else int(total_len)
    cursor = 0
    resolved = {}
    field_bits = {}
    out_fields = {}
    sync_results = []
    match_results = []

    fdefs = d["fields"]
    # precompute trailing fixed bits after a rest field (for backstop)
    rest_idx = next((i for i, f in enumerate(fdefs) if f["width_bits"] == "rest"), None)
    trailing_after_rest = 0
    if rest_idx is not None:
        for f in fdefs[rest_idx + 1:]:
            trailing_after_rest += f["width_bits"] if isinstance(f["width_bits"], int) else 0

    for i, f in enumerate(fdefs):
        name = f["name"]
        # presence
        if not _predicate(f.get("present_if"), resolved):
            out_fields[name] = {"value": None, "status": ABSENT, "offset": None, "width": 0}
            continue

        # width
        if f["width_bits"] == "rest":
            width = N - cursor - trailing_after_rest
            if width < 0:
                width = 0
        else:
            try:
                width = _resolve_width(f, resolved)
            except FrameSpecError:
                out_fields[name] = {"value": None, "status": UNDECODED, "offset": cursor, "width": 0}
                continue

        if cursor + width > len(bits):
            avail = bits[cursor:]
            out_fields[name] = {
                "value": (bits_to_int(avail, f["bit_order"]) if avail else None),
                "status": TRUNCATED, "offset": cursor, "width": len(avail)}
            # can't reliably continue past a truncation
            break

        raw = bits[cursor:cursor + width]
        val = bits_to_int(raw, f["bit_order"])
        field_bits[name] = raw
        resolved[name] = val
        entry = {"value": val, "status": OK, "offset": cursor, "width": width}

        if f["role"] == "sync":
            # hamming distance to declared, over msb-first value bits
            declared = int_to_bits(f["value"], width, "msb_first")
            actual = int_to_bits(val, width, "msb_first")
            dist = sum(a != b for a, b in zip(declared, actual))
            ok = dist <= f.get("max_errors", 0)
            entry["status"] = OK if ok else MISMATCH
            entry["sync_errors"] = dist
            sync_results.append({"field": name, "ok": ok, "errors": dist})
        elif f["role"] == "match":
            mask = f["mask"]
            ok = (val & mask) == (f["value"] & mask)
            entry["status"] = OK if ok else MISMATCH
            entry["expected"] = f["value"]
            entry["mask"] = mask
            match_results.append({"field": name, "ok": ok,
                                  "got": val & mask, "expected": f["value"] & mask})

        out_fields[name] = entry
        cursor += width

    # crc check
    crc_ok = None
    if d["crc"]:
        crc = d["crc"]
        cf = next(f for f in fdefs if f["role"] == "crc")
        parsed_crc = out_fields.get(cf["name"], {}).get("value")
        covered = []
        aligned = True
        for cname in crc["covers"]:
            fb = field_bits.get(cname)
            if fb:
                covered.extend(fb)
        if parsed_crc is None or len(covered) % 8 != 0:
            crc_ok = None if parsed_crc is None else False
        else:
            data = bits_to_bytes_msb(covered)
            calc = crc_compute_bytes(data, crc["width"], crc["poly"], crc["init"],
                                     crc["refin"], crc["refout"], crc["xorout"])
            crc_ok = (calc == parsed_crc)

    all_match = all(m["ok"] for m in match_results) if match_results else None
    any_sync = any(s["ok"] for s in sync_results) if sync_results else None

    return ParseResult(
        fields=out_fields, crc_ok=crc_ok,
        sync_results=sync_results, match_results=match_results,
        all_match=all_match, any_sync=any_sync,
        consumed_bits=cursor)
