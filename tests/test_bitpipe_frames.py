"""
test_bitpipe_frames.py: the bitpipe frame, coding and interleaver (plan rows BP-01..BP-04).

  test_frame_roundtrip_and_crc         BP-01 fields round trip; one flipped bit fails the CRC
  test_inner_record                    the sealed inner record binds stream and seq
  test_coding_roundtrip_each_fec       BP-02 conv 1/2..7/8, rs, rs+conv, ldpc, tpc at frame size
  test_conv_corrects_random_errors     BP-03 conv 1/2 frame at 2 percent bit errors
  test_rs_corrects_sixteen_bytes       BP-03 RS corrects 16 symbol errors and flags 17
  test_interleaver_spreads_a_burst     BP-04 a 64 bit burst decodes interleaved, fails plain
  test_sizes_follow_config             derived sizes, capacity and the block code limit

python3 tests/test_bitpipe_frames.py runs them through pytest.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dpmg.bitpipe import coding, frame as fr  # noqa: E402
from dpmg.bitpipe.config import BitpipeConfig  # noqa: E402


def test_frame_roundtrip_and_crc():
    for crc in ("CRC-16-CCITT", "CRC-32"):
        hdr = fr.FrameHeader(flags=fr.FLAG_DATA | fr.FLAG_RETX, stream=2, seq=65535, ack=17, sack=0xA5A5, length=0)
        payload = bytes(range(40))
        raw = fr.pack_frame(hdr, payload, 64, crc)
        assert len(raw) == fr.HEADER_BYTES + 64 + fr.crc_bytes_of(crc)
        got = fr.unpack_frame(raw, 64, crc)
        assert got is not None
        h, p = got
        assert (h.stream, h.seq, h.ack, h.sack, h.length) == (2, 65535, 17, 0xA5A5, 40)
        assert h.data and h.retx and not h.idle and h.version == fr.VERSION
        assert p == payload
        for i in (0, 5, 30, len(raw) - 1):
            bad = bytearray(raw)
            bad[i] ^= 0x10
            assert fr.unpack_frame(bytes(bad), 64, crc) is None
        assert fr.unpack_frame(raw[:-1], 64, crc) is None
    with pytest.raises(ValueError):
        fr.pack_frame(fr.FrameHeader(), bytes(65), 64)


def test_inner_record():
    rec = fr.pack_inner(1, 40000, b"abc")
    assert fr.unpack_inner(rec) == (1, 40000, b"abc")
    assert fr.unpack_inner(b"\x01") is None
    assert fr.seq_lt(65535, 0) and not fr.seq_lt(0, 65535)
    assert fr.seq_diff(0, 65535) == 1 and fr.seq_diff(65535, 0) == -1


FECS = [{"type": "conv", "rate": r} for r in ("1/2", "2/3", "3/4", "5/6", "7/8")] + [
    {"type": "rs"}, {"type": "rs+conv", "rate": "1/2"}, {"type": "ldpc", "code": "qc-r12"},
    {"type": "ldpc", "code": "qc-r34"}, {"type": "tpc", "code": "eH(32,26)^2"}, None]


@pytest.mark.parametrize("fec", FECS, ids=[str(f) for f in FECS])
def test_coding_roundtrip_each_fec(fec):
    limit = coding.max_info_bits(fec)
    n_info = 600 if limit is None or limit >= 600 else (limit // 8) * 8
    rng = np.random.default_rng(1)
    info = rng.integers(0, 2, n_info).astype(np.uint8)
    coded = coding.encode_block(info, fec)
    assert len(coded) == coding.coded_len(n_info, fec)
    out, ok = coding.decode_block(np.where(coded == 1, 1.0, -1.0), fec, n_info)
    assert ok and np.array_equal(out, info)


def test_conv_corrects_random_errors():
    rng = np.random.default_rng(2)
    fec = {"type": "conv", "rate": "1/2"}
    fails = 0
    for _ in range(10):
        info = rng.integers(0, 2, 600).astype(np.uint8)
        llr = np.where(coding.encode_block(info, fec) == 1, 1.0, -1.0)
        flips = rng.random(len(llr)) < 0.02
        out, _ok = coding.decode_block(np.where(flips, -llr, llr), fec, 600)
        fails += int(not np.array_equal(out, info))
    assert fails == 0


def test_rs_corrects_sixteen_bytes():
    rng = np.random.default_rng(3)
    info = rng.integers(0, 2, 600).astype(np.uint8)        # 75 bytes
    coded = coding.encode_block(info, {"type": "rs"})
    hard = coded.copy()
    for k in range(16):                                      # 16 distinct bytes hit
        hard[8 * (k * 5) + 3] ^= 1
    out, ok = coding.decode_block(np.where(hard == 1, 1.0, -1.0), {"type": "rs"}, 600)
    assert ok and np.array_equal(out, info)
    hard[8 * 90 + 1] ^= 1                                    # a seventeenth
    out, ok = coding.decode_block(np.where(hard == 1, 1.0, -1.0), {"type": "rs"}, 600)
    assert not ok or not np.array_equal(out, info)


def test_interleaver_spreads_a_burst():
    rng = np.random.default_rng(4)
    fec = {"type": "conv", "rate": "1/2"}
    v = np.arange(1212, dtype=np.float32)
    assert np.array_equal(coding.deinterleave(coding.interleave(v, 8), 8, 1212), v)
    assert np.array_equal(coding.deinterleave(coding.interleave(v, 1), 1, 1212), v)
    info = rng.integers(0, 2, 600).astype(np.uint8)
    coded = coding.encode_block(info, fec)

    def run(depth, burst=64, start=300):
        air = coding.interleave(coded, depth)
        llr = np.where(air == 1, 1.0, -1.0)
        llr[start:start + burst] *= -1
        back = coding.deinterleave(llr, depth, len(coded))
        out, _ = coding.decode_block(back, fec, 600)
        return np.array_equal(out, info)

    assert run(16)
    assert not run(1)


def test_sizes_follow_config():
    s = BitpipeConfig().validate()
    assert s.frame_bytes == 75 and s.info_bits == 600 and s.coded_bits == 1212
    assert s.air_bits == 1216 and s.block_bits == 32 + 1216 and s.data_capacity == 61
    assert BitpipeConfig(crypto="secure").validate().data_capacity == 64 - 23 - 3
    assert BitpipeConfig(bind_header=False, crypto="none").validate().data_capacity == 64
    with pytest.raises(ValueError):
        BitpipeConfig(fec={"type": "ldpc", "code": "qc-r12"}, payload_bytes=64).validate()
    assert BitpipeConfig(fec={"type": "ldpc", "code": "qc-r12"}, payload_bytes=20).validate().info_bits <= 329
    for bad in (dict(window=17), dict(crc="CRC-8"), dict(bit_format="x"), dict(payload_bytes=0)):
        with pytest.raises(ValueError):
            BitpipeConfig(**bad).validate()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"] + sys.argv[1:]))
