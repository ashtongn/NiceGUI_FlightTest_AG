"""
Tests for frame_integration: UI to descriptor assembly and the channelizer-side
detect_and_tag / match_gated policy (multi-hypothesis detection).
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root
from dpmg.framing import frame_spec as fs
from dpmg.framing import frame_integration as fi

PASS = FAIL = 0


def check(cond, label):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  [ok]   {label}")
    else:
        FAIL += 1; print(f"  [FAIL] {label}")


def section(t):
    print(f"\n=== {t} ===")


# descriptor_from_ui
section("1. descriptor_from_ui: rows + CRC preset -> validated descriptor")
rows = [
    {"name": "sync", "role": "sync", "width_bits": 16, "value": "0xB23B", "max_errors": 2},
    {"name": "device_id", "role": "match", "width_bits": 8, "value": "0x4A", "mask": "0xFF"},
    {"name": "length", "role": "length", "width_bits": 8},
    {"name": "payload", "role": "data", "width_bits": {"from_field": "length", "scale": 8}},
]
desc = fi.descriptor_from_ui("BPSK", 9600, rows,
                             crc_choice="CRC-16-CCITT",
                             crc_covers=["device_id", "length", "payload"])
check(desc["crc"]["poly"] == 0x1021, "CCITT preset poly wired")
check(any(f["role"] == "crc" for f in desc["fields"]), "crc field auto-appended")
check(desc["modulation"] == "BPSK" and desc["symbol_rate_hz"] == 9600, "mod/rate bound")

# round-trip the assembled descriptor to be sure it's real
bits = fi.make_frame_bits(desc, values={"payload": 0xAABBCC}, field_widths={"payload": 24})
r = fs.parse_frame(desc, bits)
check(r.crc_ok is True and r.fields["device_id"]["value"] == 0x4A,
      "assembled descriptor round-trips")

# custom CRC path
descc = fi.descriptor_from_ui("FSK", 4800,
                              [{"name": "s", "role": "sync", "width_bits": 8, "value": "0xAA"},
                               {"name": "d", "role": "data", "width_bits": 8}],
                              crc_choice="Custom...",
                              crc_custom={"width": 16, "poly": "0x1021", "init": "0xFFFF"},
                              crc_covers=["d"])
check(descc["crc"]["init"] == 0xFFFF, "custom CRC params flow through")


# detect_and_tag
section("2. detect_and_tag: multi-hypothesis, tag everything synced")
# two descriptors as competing hypotheses
descA = fi.descriptor_from_ui("BPSK", 9600, rows,
                              crc_choice="CRC-16-CCITT",
                              crc_covers=["device_id", "length", "payload"])
descB = fi.descriptor_from_ui("GFSK", 38400,
                              [{"name": "sync", "role": "sync", "width_bits": 16, "value": "0xAAAA"},
                               {"name": "id", "role": "match", "width_bits": 8, "value": "0x11", "mask": "0xFF"},
                               {"name": "body", "role": "rest"}],
                              crc_choice="CRC-16-IBM", crc_covers=["id", "body"])

# a burst that is an A-frame with device_id 0x4A
burstA = fi.make_frame_bits(descA, values={"payload": 0x010203}, field_widths={"payload": 24})
hyps = {"A": descA, "B": descB}
res = fi.detect_and_tag(burstA, hyps, mode="detect_and_tag")
names = {r["descriptor"] for r in res}
check("A" in names, "A-frame detected by hypothesis A")
check("B" not in names, "hypothesis B (sync 0xAAAA) does NOT falsely lock on A-frame")
recA = next(r for r in res if r["descriptor"] == "A")
check(recA["all_match"] is True and recA["crc_ok"] is True, "A tagged match+crc_ok")

# a burst that is an A-frame but with a non-matching device_id
burstA2 = fi.make_frame_bits(descA, values={"device_id": 0x99, "payload": 0x010203},
                             field_widths={"payload": 24})
res = fi.detect_and_tag(burstA2, hyps, mode="detect_and_tag")
recA = next(r for r in res if r["descriptor"] == "A")
check(recA["detected"] is True, "detect_and_tag STILL surfaces non-matching frame (synced)")
check(recA["all_match"] is False, "non-matching frame tagged all_match False")
check(recA["crc_ok"] is True, "frame body/CRC still valid despite ID mismatch")


# match_gated
section("3. match_gated: suppress non-matching frames")
# gated on the matching frame: surfaced
res = fi.detect_and_tag(burstA, hyps, mode="match_gated")
check(any(r["descriptor"] == "A" for r in res), "match_gated surfaces the matching frame")
# gated on the non-matching frame: suppressed
res = fi.detect_and_tag(burstA2, hyps, mode="match_gated")
check(all(r["descriptor"] != "A" for r in res), "match_gated suppresses ID mismatch")

# gated + bad CRC: suppressed when gate_requires_crc
bad = list(burstA); bad[-1] ^= 1
res = fi.detect_and_tag(bad, hyps, mode="match_gated", gate_requires_crc=True)
check(all(r["descriptor"] != "A" for r in res), "match_gated+crc suppresses bad-CRC frame")
# same bad-CRC frame in detect_and_tag: surfaced but flagged
res = fi.detect_and_tag(bad, hyps, mode="detect_and_tag")
recA = next((r for r in res if r["descriptor"] == "A"), None)
check(recA is not None and recA["crc_ok"] is False, "detect_and_tag shows bad-CRC frame flagged")


# summarize
section("4. summarize: human one-liners")
s = fi.summarize(recA)
check("A" in s and "CRC_FAIL" in s, f"summary reflects state: {s!r}")
recA_ok = next(r for r in fi.detect_and_tag(burstA, hyps, "detect_and_tag")
               if r["descriptor"] == "A")
s2 = fi.summarize(recA_ok)
check("device_id=0x4A" in s2, f"summary lists matched id: {s2!r}")


print(f"\n{'='*44}\nRESULT: {PASS} passed, {FAIL} failed\n{'='*44}")
import sys
sys.exit(1 if FAIL else 0)
