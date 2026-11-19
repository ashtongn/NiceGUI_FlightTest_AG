"""
encode then inject errors then decode, per DMR code. Passes only if the decoder
returns the exact input data for every error pattern within the code's
correction power, and flags (ok=False) at least the clean CRC-mismatch case.
Deterministic. Exit 0 = all pass.
"""
from __future__ import annotations

import sys
import os
import random

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dpmg.encoding.ecc_decoder import encode_field_bits
from dpmg.encoding.ecc_decode import decode_field_bits

rng = random.Random(0xECC)
checks = []


def chk(name, ok, detail=""):
    checks.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")


def flip(bits, positions):
    b = list(bits)
    for p in positions:
        b[p] = "1" if b[p] == "0" else "0"
    return "".join(b)


def rand_bits(n):
    return "".join(rng.choice("01") for _ in range(n))


def test_correcting(enc, kbits, t_per, trials, label, blocks=1):
    """t_per errors spread so each correcting unit sees <= its power."""
    all_ok = True
    for _ in range(trials):
        data = rand_bits(kbits)
        code = encode_field_bits(data, enc)
        n = len(code)
        # inject t_per random distinct errors
        if t_per > 0:
            pos = rng.sample(range(n), min(t_per, n))
        else:
            pos = []
        d, c, ok = decode_field_bits(flip(code, pos), enc)
        if d[:kbits] != data or not ok:
            all_ok = False
            break
    chk(f"{label}: recover data + correct {t_per} err", all_ok)


def test_hamming_per_block(enc, kblk, label):
    # one error per block is correctable; verify across several blocks
    ok_all = True
    for _ in range(50):
        nb = rng.randint(1, 4)
        data = rand_bits(kblk * nb)
        code = encode_field_bits(data, enc)
        n = len(code) // nb
        cc = list(code)
        for bi in range(nb):
            p = rng.randrange(n)
            idx = bi * n + p
            cc[idx] = "1" if cc[idx] == "0" else "0"
        d, c, ok = decode_field_bits("".join(cc), enc)
        if d[:len(data)] != data or c != nb:
            ok_all = False
            break
    chk(f"{label}: 1 err/block over N blocks", ok_all)


def main():
    print("DMR FEC round-trip (encode -> corrupt -> decode)\n")

    # Hamming: single error per block corrected, count exact
    test_hamming_per_block("Hamming(7,4)", 4, "Hamming(7,4)")
    test_hamming_per_block("Hamming(13,9)", 9, "Hamming(13,9)")
    test_hamming_per_block("Hamming(15,11)", 11, "Hamming(15,11)")
    # clean pass-through
    test_correcting("Hamming(13,9)", 9, 0, 50, "Hamming(13,9) noiseless")

    # Golay(20,8): corrects up to 3 errors
    for t in (0, 1, 2, 3):
        test_correcting("Golay(20,8)", 8, t, 80, f"Golay(20,8)")

    # BPTC(196,96): 1 err/row + 1 err/col correctable; test 0 and 1
    test_correcting("BPTC(196,96)", 96, 0, 30, "BPTC(196,96) noiseless")
    # every single-bit error must recover the data (row or column corrects
    # it; an error in the unused reserved bit leaves the payload intact)
    ok_bptc = True
    for _ in range(20):
        data = rand_bits(96)
        code = encode_field_bits(data, "BPTC(196,96)")
        if any(decode_field_bits(flip(code, [p]), "BPTC(196,96)")[0] != data
               for p in range(196)):
            ok_bptc = False
            break
    chk("BPTC(196,96): every single-bit error recovers data", ok_bptc)

    # Convolutional / trellis: exhaustive single-bit correction (the
    # deterministic guarantee) plus a modest multi-error sample
    for enc, t in (("Convolutional Rate 1/2", 2),
                   ("Convolutional Rate 1/3", 3),
                   ("Rate 3/4 Trellis", 1)):
        data = rand_bits(24)
        code = encode_field_bits(data, enc)
        n = len(code)
        single = all(decode_field_bits(flip(code, [p]), enc)[0][:len(data)]
                     == data for p in range(n))
        multi = all(decode_field_bits(
            flip(code, sorted(rng.sample(range(n), t))), enc)[0][:len(data)]
            == data for _ in range(30))
        chk(f"{enc}: all 1-bit + sampled {t}-bit errors recover",
            single and multi)

    # CRC: detect corruption, recover clean data
    for enc, ncrc in (("CRC-CCITT", 16), ("CRC-9", 9), ("CRC-32", 32)):
        data = rand_bits(40)
        code = encode_field_bits(data, enc)
        d0, _c0, ok0 = decode_field_bits(code, enc)
        _d1, _c1, ok1 = decode_field_bits(flip(code, [rng.randrange(len(code))]),
                                          enc)
        chk(f"{enc}: clean ok + 1-bit flip flagged",
            d0 == data and ok0 and not ok1)

    # full DMR data block: BPTC + CRC-CCITT over 80 bits
    data = rand_bits(80)
    code = encode_field_bits(data, "DMR Data Block (CRC-CCITT + BPTC196/96)")
    d, c, ok = decode_field_bits(flip(code, [rng.randrange(196)]),
                                 "DMR Data Block (CRC-CCITT + BPTC196/96)")
    chk("DMR Data Block: BPTC corrects + CRC validates",
        d[:80] == data and ok)

    # message level: multi-field encode then corrupt one bit/field then decode
    from dpmg.encoding.ecc_decode import decode_message_fields
    spec = [(9, "Hamming(13,9)"), (8, "Golay(20,8)"), (40, "CRC-CCITT"),
            (24, "Convolutional Rate 1/2"), (96, "BPTC(196,96)")]
    fdata = [rand_bits(n) for n, _ in spec]
    msg = "".join(encode_field_bits(fd, enc) for fd, (n, enc) in zip(fdata, spec))
    # one correctable error inside each error-correcting field
    from dpmg.encoding.ecc_decode import encoded_len
    corrupt = list(msg)
    off = 0
    for (n, enc), fd in zip(spec, fdata):
        el = encoded_len(enc, n)
        if enc != "CRC-CCITT":       # leave the CRC field clean so ok stays True
            corrupt[off] = "1" if corrupt[off] == "0" else "0"
        off += el
    dec, c, ok = decode_message_fields("".join(corrupt), spec)
    good = all(dec[i][:spec[i][0]] == fdata[i] for i in range(len(spec)))
    chk("multi-field message: per-field FEC decode recovers all", good and ok,
        f"({c} bits corrected)")

    print(f"\nresult: {sum(checks)}/{len(checks)} checks passed")
    return 0 if checks and all(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
