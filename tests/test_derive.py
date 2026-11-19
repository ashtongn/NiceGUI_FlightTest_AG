"""
Tests frame_integration.descriptor_from_encoder_fields: turning the encoder tab's
field list (bin/hex + desc + encoding) into a frame descriptor. Covers sync/preamble
detection, CRC-CCITT mapping, hex-only width derivation, and the empty/None/no-sync
fallbacks (returns None instead of raising). This is the only coverage of that
function. Exit 0 = all checks pass.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root
from dpmg.framing import frame_spec as fs
from dpmg.framing import frame_integration as fi
P=F=0
def chk(c,l):
    global P,F
    if c: P+=1; print("  [ok]  ",l)
    else: F+=1; print("  [FAIL]",l)

print("=== derive descriptor from existing encoder fields ===")
# realistic encoder output: a Sync field + data + a CRC-CCITT field
svc = [
    {"bin":"1011001000111011","desc":"Sync","encoding":"None"},        # 0xB23B preamble
    {"bin":"01001010","desc":"Header","encoding":"None"},              # 0x4A
    {"bin":"1010101011001100","desc":"Payload","encoding":"None"},
    {"bin":"0000000000000000","desc":"Payload","encoding":"CRC-CCITT"},# CRC field
]
settings={"modulation":"BPSK","symbol_rate":9600.0}
d = fi.descriptor_from_encoder_fields(svc, settings)
chk(d is not None, "descriptor derived from Sync+CRC fields")
chk(any(f["role"]=="sync" for f in d["fields"]), "Sync desc -> sync field")
chk(d["crc"] is not None and d["crc"]["poly"]==0x1021, "CRC-CCITT enc -> CCITT crc")
chk(d["fields"][0]["value"]==0xB23B, "sync value = field bits (0xB23B)")

# no sync field: None (safe fallback)
svc2=[{"bin":"01001010","desc":"Payload","encoding":"None"}]
chk(fi.descriptor_from_encoder_fields(svc2, settings) is None, "no sync -> None (fallback)")

# empty: None, no crash
chk(fi.descriptor_from_encoder_fields([], settings) is None, "empty fields -> None")
chk(fi.descriptor_from_encoder_fields(None, settings) is None, "None fields -> None")

# 'Preamble' desc also counts as sync
svc3=[{"bin":"10101010","desc":"Preamble","encoding":"None"},
      {"bin":"11110000","desc":"Payload","encoding":"None"}]
d3=fi.descriptor_from_encoder_fields(svc3, settings)
chk(d3 is not None and d3["fields"][0]["role"]=="sync", "Preamble desc -> sync")

# hex-only field (no bin) still derives width
svc4=[{"hex":"AA","desc":"Sync","encoding":"None"},
      {"hex":"BB","desc":"Payload","encoding":"None"}]
d4=fi.descriptor_from_encoder_fields(svc4, settings)
chk(d4 is not None and d4["fields"][0]["width_bits"]==8, "hex-only sync -> 8 bits")

print(f"\nRESULT: {P} passed, {F} failed")
import sys; sys.exit(1 if F else 0)
