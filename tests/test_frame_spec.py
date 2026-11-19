"""
Round-trip test harness for frame_spec.

Coverage:
  1. CRC engine vs known vectors (proves it's a real CRC, not just self-consistent)
  2. Round-trip: build: parse, assert every field value + crc_ok survive
     - fixed fields, length-driven payload, rest role
     - match with mask, per-field bit_order (msb/lsb mix)
     - conditional field present and absent (present_if)
     - signed offset on width-ref (length includes CRC convention)
     - multi (repeated) sync fields
  3. Author-side strictness: build/validate raise on bad descriptors
  4. Ingest-side leniency: parse never raises; statuses fire correctly
     (mismatch, truncated, absent, undecoded)
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root
from dpmg.framing import frame_spec as fs

PASS = 0
FAIL = 0


def check(cond, label):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok]   {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}")


def section(t):
    print(f"\n=== {t} ===")


# 1. CRC vectors
section("1. CRC engine vs known vectors (input '123456789')")
msg = b"123456789"
# CRC-16-CCITT/FALSE
v = fs.crc_compute_bytes(msg, 16, 0x1021, 0xFFFF, False, False, 0x0000)
check(v == 0x29B1, f"CRC-16/CCITT-FALSE = 0x{v:04X} (want 0x29B1)")
# CRC-16/ARC (IBM)
v = fs.crc_compute_bytes(msg, 16, 0x8005, 0x0000, True, True, 0x0000)
check(v == 0xBB3D, f"CRC-16/ARC = 0x{v:04X} (want 0xBB3D)")
# CRC-32
v = fs.crc_compute_bytes(msg, 32, 0x04C11DB7, 0xFFFFFFFF, True, True, 0xFFFFFFFF)
check(v == 0xCBF43926, f"CRC-32 = 0x{v:08X} (want 0xCBF43926)")
# CRC-8/SMBUS
v = fs.crc_compute_bytes(msg, 8, 0x07, 0x00, False, False, 0x00)
check(v == 0xF4, f"CRC-8 = 0x{v:02X} (want 0xF4)")


# 2a. fixed + length + match + crc
section("2a. Round-trip: sync + match(id) + length + payload + CRC")
desc = {
    "version": 1, "modulation": "BPSK", "symbol_rate_hz": 9600, "bit_order": "msb_first",
    "fields": [
        {"name": "sync", "role": "sync", "width_bits": 16, "value": "0xB23B", "max_errors": 2},
        {"name": "device_id", "role": "match", "width_bits": 8, "value": "0x4A", "mask": "0xFF"},
        {"name": "length", "role": "length", "width_bits": 8},
        {"name": "payload", "role": "data",
         "width_bits": {"from_field": "length", "scale": 8}},
        {"name": "crc", "role": "crc", "width_bits": 16},
    ],
    "crc": {"algo": "CRC-16-CCITT", "width": 16, "poly": "0x1021", "init": "0xFFFF",
            "refin": False, "refout": False, "xorout": "0x0000",
            "covers": ["device_id", "length", "payload"]},
}
# payload = 3 bytes (0xAABBCC), length auto-derived from payload width
bits = fs.build_frame(desc, values={"payload": 0xAABBCC}, field_widths={"payload": 24})
r = fs.parse_frame(desc, bits)
check(r.fields["device_id"]["value"] == 0x4A, "device_id round-trips")
check(r.fields["device_id"]["status"] == fs.OK, "device_id match OK")
check(r.fields["length"]["value"] == 3, "length auto-derived = 3 bytes")
check(r.fields["payload"]["value"] == 0xAABBCC, "payload round-trips")
check(r.fields["sync"]["status"] == fs.OK, "sync OK")
check(r.crc_ok is True, "CRC validates")
check(r["all_match"] is True, "all_match True")


# 2b. mixed bit_order
section("2b. Round-trip: per-field lsb_first override")
desc2 = {
    "version": 1, "bit_order": "msb_first",
    "fields": [
        {"name": "sync", "role": "sync", "width_bits": 8, "value": "0xAA"},
        {"name": "id_le", "role": "match", "width_bits": 16, "value": "0x1234",
         "mask": "0xFFFF", "bit_order": "lsb_first"},
        {"name": "crc", "role": "crc", "width_bits": 16},
    ],
    "crc": {"width": 16, "poly": "0x1021", "init": "0xFFFF", "refin": False,
            "refout": False, "xorout": 0, "covers": ["id_le"]},
}
bits = fs.build_frame(desc2)
r = fs.parse_frame(desc2, bits)
check(r.fields["id_le"]["value"] == 0x1234, "lsb_first field round-trips to same int")
check(r.fields["id_le"]["status"] == fs.OK, "lsb_first match OK")
check(r.crc_ok is True, "CRC over lsb_first field validates")


# 2c. conditional field
section("2c. Round-trip: present_if (ext_addr only when flags bit3 set)")
desc3 = {
    "version": 1, "bit_order": "msb_first",
    "fields": [
        {"name": "sync", "role": "sync", "width_bits": 24, "value": "0xAAAAAA", "max_errors": 3},
        {"name": "flags", "role": "data", "width_bits": 8},
        {"name": "short_addr", "role": "match", "width_bits": 16, "value": "0x1234", "mask": "0xFFFF"},
        {"name": "ext_addr", "role": "match", "width_bits": 32, "value": "0xDEADBEEF",
         "mask": "0xFFFFFFFF",
         "present_if": {"field": "flags", "mask": "0x08", "op": "all", "value": "0x08"}},
        {"name": "length", "role": "length", "width_bits": 8},
        {"name": "payload", "role": "data", "width_bits": {"from_field": "length", "scale": 8}},
        {"name": "crc", "role": "crc", "width_bits": 16},
    ],
    "crc": {"width": 16, "poly": "0x8005", "init": 0, "refin": True, "refout": True,
            "xorout": 0, "covers": ["flags", "short_addr", "ext_addr", "length", "payload"]},
}
# flags bit3 set: ext_addr present
bits = fs.build_frame(desc3, values={"flags": 0x08, "payload": 0x1122},
                      field_widths={"payload": 16})
r = fs.parse_frame(desc3, bits)
check(r.fields["ext_addr"]["status"] == fs.OK, "ext_addr present & matches when flag set")
check(r.fields["ext_addr"]["value"] == 0xDEADBEEF, "ext_addr value round-trips")
check(r.fields["payload"]["value"] == 0x1122, "payload after conditional field ok")
check(r.crc_ok is True, "CRC (incl. conditional) validates when present")

# flags bit3 clear: ext_addr absent
bits = fs.build_frame(desc3, values={"flags": 0x00, "payload": 0x1122},
                      field_widths={"payload": 16})
r = fs.parse_frame(desc3, bits)
check(r.fields["ext_addr"]["status"] == fs.ABSENT, "ext_addr absent when flag clear")
check(r.fields["payload"]["value"] == 0x1122, "payload parses with conditional absent")
check(r.crc_ok is True, "CRC (excl. absent conditional) validates")


# 2d. signed offset (length incl CRC)
section("2d. Round-trip: signed offset (length counts whole frame incl 16-bit CRC)")
desc4 = {
    "version": 1, "bit_order": "msb_first",
    "fields": [
        {"name": "sync", "role": "sync", "width_bits": 8, "value": "0x7E"},
        {"name": "length", "role": "length", "width_bits": 8},
        # payload bits = length*8 - 16  (length counts payload+crc bytes)
        {"name": "payload", "role": "data",
         "width_bits": {"from_field": "length", "scale": 8, "offset": -16}},
        {"name": "crc", "role": "crc", "width_bits": 16},
    ],
    "crc": {"width": 16, "poly": "0x1021", "init": 0xFFFF, "refin": False,
            "refout": False, "xorout": 0, "covers": ["payload"]},
}
# payload 2 bytes: length must be 4 (2 payload + 2 crc)
bits = fs.build_frame(desc4, values={"length": 4, "payload": 0xBEEF},
                      field_widths={"payload": 16})
r = fs.parse_frame(desc4, bits)
check(r.fields["length"]["value"] == 4, "length = 4 (payload+crc bytes)")
check(r.fields["payload"]["value"] == 0xBEEF, "signed-offset payload round-trips")
check(r.crc_ok is True, "CRC validates with signed-offset width")


# 2e. rest role + multi sync
section("2e. Round-trip: two sync words + 'rest' payload")
desc5 = {
    "version": 1, "bit_order": "msb_first",
    "fields": [
        {"name": "sync1", "role": "sync", "width_bits": 8, "value": "0xAA"},
        {"name": "sync2", "role": "sync", "width_bits": 8, "value": "0x55", "max_errors": 1},
        {"name": "body", "role": "rest"},
        {"name": "crc", "role": "crc", "width_bits": 16},
    ],
    "crc": {"width": 16, "poly": "0x1021", "init": 0xFFFF, "refin": False,
            "refout": False, "xorout": 0, "covers": ["body"]},
}
bits = fs.build_frame(desc5, values={"body": 0xCAFEBABE}, field_widths={"body": 32})
r = fs.parse_frame(desc5, bits)   # total_len defaults to len(bits)
check(r.fields["sync1"]["status"] == fs.OK and r.fields["sync2"]["status"] == fs.OK,
      "both sync words OK")
check(r.fields["body"]["value"] == 0xCAFEBABE, "'rest' body round-trips")
check(r.crc_ok is True, "CRC over rest body validates")
check(r["any_sync"] is True, "any_sync True")


# 3. author strictness
section("3. Author-side STRICTNESS (build/validate must raise)")

def expect_raise(fn, label):
    try:
        fn()
        check(False, label + " (did NOT raise)")
    except fs.FrameSpecError:
        check(True, label)

expect_raise(lambda: fs.validate_descriptor(
    {"version": 1, "fields": [
        {"name": "a", "role": "match", "width_bits": 8, "value": "0x1FF", "mask": "0xFF"}]}),
    "value overflowing width rejected")

expect_raise(lambda: fs.validate_descriptor(
    {"version": 1, "fields": [
        {"name": "p", "role": "data",
         "width_bits": {"from_field": "len", "scale": 8}},
        {"name": "len", "role": "length", "width_bits": 8}]}),
    "forward width reference rejected")

expect_raise(lambda: fs.validate_descriptor(
    {"version": 1, "fields": [
        {"name": "r1", "role": "rest"}, {"name": "r2", "role": "rest"},
        {"name": "crc", "role": "crc", "width_bits": 16}]}),
    "two 'rest' fields rejected")

expect_raise(lambda: fs.validate_descriptor(
    {"version": 1, "fields": [
        {"name": "sync", "role": "sync", "width_bits": 8, "value": "0xAA"}],
     "crc": {"width": 16, "poly": 1, "covers": ["nope"]}}),
    "crc.covers referencing unknown field rejected")

expect_raise(lambda: fs.build_frame(
    {"version": 1, "fields": [
        {"name": "a", "role": "data", "width_bits": 4},
        {"name": "crc", "role": "crc", "width_bits": 16}],
     "crc": {"width": 16, "poly": 0x1021, "init": 0xFFFF, "covers": ["a"]}},
    values={"a": 0xF}),
    "non-byte-aligned CRC coverage rejected at build")

expect_raise(lambda: fs.validate_descriptor({"version": 99, "fields": []}),
             "unsupported version rejected")


# 4. ingest leniency
section("4. Ingest-side LENIENCY (parse never raises; statuses fire)")

# 4a mismatch: wrong device id
bits = fs.build_frame(desc, values={"device_id": 0x99, "payload": 0xAABBCC},
                      field_widths={"payload": 24})
r = fs.parse_frame(desc, bits)
check(r.fields["device_id"]["status"] == fs.MISMATCH, "wrong device_id -> MISMATCH")
check(r["all_match"] is False, "all_match False on mismatch")
check(r.crc_ok is True, "frame still parses & CRC ok despite ID mismatch (tag, not drop)")

# 4b truncated: chop the frame mid-payload
good = fs.build_frame(desc, values={"payload": 0xAABBCC}, field_widths={"payload": 24})
r = fs.parse_frame(desc, good[:-20])
statuses = {n: v["status"] for n, v in r.fields.items()}
check(fs.TRUNCATED in statuses.values(), "chopped frame yields a TRUNCATED field")
check("error" not in r, "parse did not raise/error on truncation")

# 4c corrupt CRC: crc_ok False but fields still read
bad = list(good)
bad[-1] ^= 1
r = fs.parse_frame(desc, bad)
check(r.crc_ok is False, "flipped CRC bit -> crc_ok False")
check(r.fields["payload"]["value"] == 0xAABBCC, "payload still recovered under bad CRC")

# 4d garbage in: no exception
r = fs.parse_frame(desc, [0, 1] * 5)
check("fields" in r, "garbage input returns a result, no raise")


# summary
print(f"\n{'='*44}\nRESULT: {PASS} passed, {FAIL} failed\n{'='*44}")
import sys
sys.exit(1 if FAIL else 0)
