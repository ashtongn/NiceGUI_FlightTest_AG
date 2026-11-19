#!/usr/bin/env python3
"""
Generate DPMG flight-test scenario JSONs (X40 + G40 pairs).

Frame model: frames match frame_codec's simple decode path exactly:
    SYNC(16) | LEN(8, payload byte count) | PAYLOAD(len bytes) | CRC16(over len_byte+payload)
with SYNC = "1011001000111011" (frame_codec.SYNC_BITS) and CRC = CRC-16/CCITT (poly
0x1021, init 0xFFFF, no reflection). id and seq live in the first two payload bytes,
so they survive as data without needing separate structural fields the decoder doesn't
model.

CRC: computed here at build time over (len_byte + payload) and written into the
frame bits, so the transmit path, which plays declared bits verbatim, puts a
correct CRC on the air and the receiver's crc_ok gate passes clean / fails
corrupted. The transmit encode path (_build_encoded_bit_string) does not compute
a CRC itself, so other scenarios only carry one if their author writes it in.

Run (from anywhere; default --out is the repo's scenarios/ dir):
    python3 tools/gen_flight_scenarios.py --both                    # both sets, 1350 MHz
    python3 tools/gen_flight_scenarios.py --both --center 2200e6    # retune the whole set
    python3 tools/gen_flight_scenarios.py --crc --out /tmp/sc       # CRC set only, custom dir

Flags:
    --both     write a sync-only set and a CRC-enforced set (*_crc filename suffix)
    --crc      CRC-enforced set only;  (default, neither given) sync-only placeholder
    --center   center frequency in Hz (default 1350e6). Sets the band for s1/s2/s3
               and the base for the s4 agility hops (fixed offsets -35/-5/+25/+45
               MHz), and is written into each output filename's MHz tag.
    --out      output directory (default: the repo's scenarios/ dir next to tools/)
    --power-db / --psk-boost-db   per-emitter power tweaks (see --help)

Each run writes s1..s4 as {tag}_g40.json (30.72 MSps) and {tag}_x40.json (50 MSps).
"""
import json, copy, os, argparse

SYNC_BITS = "1011001000111011"
POWER_DB = 0.0   # per-emitter power (dB). Set <0 to test TX DAC overdrive (e.g. -12).
PSK_BOOST_DB = 0.0   # extra power for PSK-family emitters (QPSK/DQPSK/OQPSK/PI4QPSK): they read ~26dB weaker; use +20 to test

def crc16_ccitt(data: bytes, init: int = 0xFFFF) -> int:   # byte-identical to frame_codec
    crc = init
    for b in data:
        crc ^= (b << 8)
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if (crc & 0x8000) else (crc << 1) & 0xFFFF
    return crc & 0xFFFF

def _int_to_bits(v, n): return format(v & ((1 << n) - 1), f"0{n}b")
def _bits_to_bytes(bits):
    if len(bits) % 8: bits = bits + "0"*(8 - len(bits) % 8)
    return bytes(int(bits[i:i+8], 2) for i in range(0, len(bits), 8))

# build one frame's bit string, matching find_frame's simple path
def build_frame_bits(emitter_id, seq, has_crc, payload_tail=b"SPECAT"):
    """SYNC | LEN | PAYLOAD | CRC.  payload = id(1) + seq(1) + tail bytes."""
    payload = bytes([emitter_id & 0xFF, seq & 0xFF]) + payload_tail   # id, seq, then label
    length  = len(payload)                                            # bytes
    len_bits     = _int_to_bits(length, 8)
    payload_bits = "".join(f"{b:08b}" for b in payload)
    if has_crc:
        crc = crc16_ccitt(_bits_to_bytes(len_bits) + payload)         # over len_byte+payload
    else:
        crc = 0x0033                                                  # legacy placeholder
    return SYNC_BITS + len_bits + payload_bits + _int_to_bits(crc, 16)

# frame spec block written into the JSON (documents the layout)
def frame_block(mod, symrate, has_crc):
    # roles the simple decode path understands: sync + length + data(payload) + crc.
    return {"version":1,"modulation":mod,"symbol_rate_hz":float(symrate),"bit_order":"msb_first",
        "sync_bits":SYNC_BITS, "has_length":True,
        "fields":[
            {"name":"sync","role":"sync","width_bits":16,"value":int(SYNC_BITS,2),"max_errors":3},
            {"name":"len","role":"length","width_bits":8},
            {"name":"payload","role":"data","width_bits":None},   # variable (id+seq+label)
            {"name":"crc","role":"crc","width_bits":16}],
        "crc": ("CRC-16-CCITT" if has_crc else None)}

def entry(name, mod, off_hz, symrate, idb, start_s, has_crc, seq=0):
    spb = int(round(5.0e6 / symrate))
    # PSK family reads ~26 dB weaker than FSK/BPSK on the scoreboard; allow a boost.
    _pwr = float(POWER_DB) + (PSK_BOOST_DB if mod in ("QPSK","DQPSK","OQPSK","PI4QPSK") else 0.0)
    e = {"name":name,"modulation":mod,
         "params":{"symbol_rate":float(symrate),"samples_per_bit":spb},
         "offset_hz":float(off_hz),"power_db":_pwr,"role":"known","decode":True,
         # duration_s must be >= 2x the frame length so the burst window always holds
         # a complete frame (frames are tiled in compose_plan, so a window of two frame
         # lengths spans at least one whole frame at any alignment). Frame = 104 bits.
         # At 1 bit/symbol (BPSK/FSK/GFSK) and QPSK's 2x-sps the frame is 20.8 ms at
         # 5 ksym/s, so 41.6 ms or more; 45 ms covers every mod here with margin
         # (4FSK's frame is 10.4 ms). A window shorter than the frame cuts the CRC off.
         "burst":{"start_s":round(start_s,4),"duration_s":0.045,"period_s":0.25}}
    if mod == "4FSK":
        e["params"]["tone_deltas_hz"] = [-1.5*symrate,-0.5*symrate,0.5*symrate,1.5*symrate]
        e["params"]["tone_spacing_hz"] = symrate   # decode_pool reads this key to build tones
    elif mod in ("FSK","GFSK"):
        e["params"]["tone_deltas_hz"] = [-0.5*symrate,0.5*symrate]
        e["params"]["tone_spacing_hz"] = symrate   # decode_pool reads this key: tones +/-0.5*spacing = +/-2500
    e["frame"] = frame_block(mod, symrate, has_crc)
    e["bits"]  = build_frame_bits(idb, seq, has_crc)
    return e

def write_pair(out, tag, center, entries):
    for radio, fs in (("g40",30.72e6),("x40",50e6)):
        d = {"schema_version":1,
             "compose":{"tx_radio":radio,"sample_rate_hz":float(fs),
                        "center_freq_hz":float(center),"rf_bandwidth_hz":float(fs*0.8)},
             "entries":copy.deepcopy(entries)}
        json.dump(d, open(f"{out}/{tag}_{radio}.json","w"), indent=2)

def build(out, has_crc, center=1350e6):
    sfx = "_crc" if has_crc else ""
    cmhz = int(round(center / 1e6))     # center in MHz, for the filename tags
    mods=["BPSK","QPSK","PI4QPSK","DQPSK","OQPSK","FSK","4FSK","GFSK"]
    e1=[entry(f"{m}-{i+1:02d}",m,(-5.25+1.5*i)*1e6,5000,i+1,round(0.02+0.026*i,4),has_crc) for i,m in enumerate(mods)]
    write_pair(out, f"s1_modem_sweep_{cmhz}{sfx}",center,e1)
    rates=[2500,5000,10000,20000]; offs=[-6e6,-2e6,2e6,7e6]; st=[0.02,0.08,0.14,0.20]
    e2=[entry(f"4FSK-{r//1000}k","4FSK",offs[i],r,i+1,st[i],has_crc) for i,r in enumerate(rates)]
    write_pair(out, f"s2_rate_sweep_{cmhz}{sfx}",center,e2)
    e3=[entry(f"4FSK-{i+1:02d}","4FSK",-10.0e6+i*0.8333e6,5000,(i%254)+1,round(0.01+0.0072*(i%14),4),has_crc) for i in range(25)]
    write_pair(out, f"s3_density25_{cmhz}{sfx}",center,e3)
    # agility hops as fixed offsets from center, so the whole set moves with
    # --center (the defaults reproduce 1315/1345/1375/1395 at center 1350 MHz).
    for k,dC in enumerate([-35e6,-5e6,25e6,45e6]):
        C = center + dC
        ek=[entry(f"4FSK-{k+1}{j+1}","4FSK",(-4+4*j)*1e6,5000,k*3+j+1,round(0.03+0.08*j,4),has_crc) for j in range(3)]
        write_pair(out, f"s4_agility_step{k+1}_{int(round(C/1e6))}{sfx}",C,ek)

if __name__ == "__main__":
    # Default output = the bind-mounted scenarios dir next to the project root,
    # derived from this script's location (tools/), so the current directory does
    # not matter. tools/../scenarios is NiceGui_test/scenarios on the radio.
    _here = os.path.dirname(os.path.abspath(__file__))
    _default_out = os.path.normpath(os.path.join(_here, "..", "scenarios"))
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=_default_out)
    ap.add_argument("--center", type=float, default=1350e6,
                    help="center frequency in Hz (default 1350e6); sets s1/s2/s3 and the s4 agility base")
    ap.add_argument("--power-db", type=float, default=0.0, help="per-emitter power in dB (use -12 to test TX overdrive)")
    ap.add_argument("--psk-boost-db", type=float, default=0.0, help="extra power for PSK-family emitters (use +20 to test QPSK SNR)")
    ap.add_argument("--crc", action="store_true")
    ap.add_argument("--both", action="store_true")
    a = ap.parse_args(); os.makedirs(a.out, exist_ok=True)
    globals()["POWER_DB"] = a.power_db
    globals()["PSK_BOOST_DB"] = a.psk_boost_db
    if a.both:
        build(a.out, False, a.center); build(a.out, True, a.center)
        print(f"wrote both sets to {a.out} @ {a.center/1e6:g} MHz (*_crc = CRC enforced)")
    else:
        build(a.out, a.crc, a.center)
        print(f"wrote {'CRC' if a.crc else 'placeholder'} set to {a.out} @ {a.center/1e6:g} MHz")
