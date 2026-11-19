"""
test_bitpipe_link.py: deframer, ARQ, seal/open and idle continuity through the
pure engine, no sockets (plan rows BP-05, BP-06, BP-07, BP-10).

  test_deframer_lock_and_relock          BP-05 random bit offset, 200 frames, a 500 bit gap
  test_arq_lossy_both_ways               BP-06 20 percent frame loss + bit errors, in order, once
  test_arq_window_and_sack               TxWindow / RxReorder bookkeeping and wrap at 65535
  test_secure_tamper_replay_bind         BP-07 tampered, replayed and header swapped frames dropped
  test_idle_frames_keep_lock             BP-10 nothing to send, the far end stays locked
  test_mux_round_robin                   two streams share the link and arrive apart
  test_soft8_face                        the soft bit input format decodes
  test_station_drops_own_frames          two radio link: an end that hears its own TX leaking into
                                         its RX drops those frames (no self delivery, no self acks)
  test_no_station_hears_itself           one radio loopback: with no station set an end still
                                         talks to itself
  test_outage_with_self_hearing          BP-33 as the radios did it: one end silent for 20 slots,
                                         the frames in its TX queue lost, the other end hearing only
                                         its own TX meanwhile; every byte arrives once TX is back

python3 tests/test_bitpipe_link.py runs them through pytest.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DPMG_SECURE_MSG", "1")
from dpmg.bitpipe import BitpipeConfig, LinkEngine, loopback_crypto_pair  # noqa: E402
from dpmg.bitpipe import frame as fr  # noqa: E402
from dpmg.bitpipe.arq import RxReorder, TxWindow  # noqa: E402
from dpmg.bitpipe.deframer import Deframer  # noqa: E402


def llr_of(bits):
    return np.where(np.asarray(bits) == 1, 1.0, -1.0).astype(np.float32)


def pump(a, b, n, chan=None, got_a=None, got_b=None):
    """n frame slots each way through an optional channel (llr in, llr or None out)."""
    chan = chan or (lambda x: x)
    for _ in range(n):
        x = chan(llr_of(a.next_frame_bits()))
        if x is not None:
            for s, d in b.feed_bits(x):
                if got_b is not None:
                    got_b.setdefault(s, bytearray()).extend(d)
        y = chan(llr_of(b.next_frame_bits()))
        if y is not None:
            for s, d in a.feed_bits(y):
                if got_a is not None:
                    got_a.setdefault(s, bytearray()).extend(d)


def test_deframer_lock_and_relock():
    cfg = BitpipeConfig()
    a = LinkEngine(cfg, name="A")
    seen = []
    df = Deframer(cfg.sync_hex, cfg.sizes.air_bits, cfg.sync_max_errors, cfg.lock_miss_limit)
    b = LinkEngine(cfg, name="B")

    def try_block(block):
        ok = b._try_block(block)
        seen.append(ok)
        return ok

    rng = np.random.default_rng(7)
    stream = np.concatenate([rng.integers(0, 2, 777)] + [a.next_frame_bits() for _ in range(200)])
    df.feed(llr_of(stream[:5000]), try_block)
    df.feed(llr_of(stream[5000:]), try_block)
    assert df.locked and df.stats["frames_ok"] == 200 and df.stats["locks"] == 1
    # a 500 bit gap (a dropped segment): a few frames lost, then back in lock
    more = [a.next_frame_bits() for _ in range(30)]
    cut = np.concatenate(more)
    cut = np.concatenate([cut[:cfg.sizes.block_bits * 3 + 100], cut[cfg.sizes.block_bits * 3 + 600:]])
    before = df.stats["frames_ok"]
    df.feed(llr_of(cut), try_block)
    assert df.locked
    assert df.stats["frames_ok"] - before >= 27


def test_arq_lossy_both_ways():
    cfg = BitpipeConfig(payload_bytes=32)
    a, b = LinkEngine(cfg, name="A"), LinkEngine(cfg, name="B")
    rng = np.random.default_rng(11)
    msg_a, msg_b = rng.bytes(4000), rng.bytes(2500)
    a.send(1, msg_a)
    b.send(1, msg_b)

    def chan(llr):
        if rng.random() < 0.2:
            return None
        flips = rng.random(len(llr)) < 0.01
        return np.where(flips, -llr, llr)

    got_a, got_b = {}, {}
    for _ in range(60):
        pump(a, b, 20, chan, got_a, got_b)
        if bytes(got_b.get(1, b"")) == msg_a and bytes(got_a.get(1, b"")) == msg_b:
            break
    assert bytes(got_b[1]) == msg_a and bytes(got_a[1]) == msg_b
    sa, sb = a.status(), b.status()
    assert sa["tx"]["retx"] > 0 and sb["tx"]["retx"] > 0
    assert sa["open_failed"] == 0 and sb["bind_mismatch"] == 0
    pump(a, b, 20, None)
    assert a.idle() and b.idle() and not a.status()["link_down"]


def test_arq_window_and_sack():
    tx = TxWindow(window=4, rto_frames=3)
    for i in range(4):
        tx.sent(tx.add(i, 1, bytes([i])), retransmit=False)
        tx.advance()
    assert not tx.can_send_new()
    # peer has 0, 2 and 3 but not 1: ack 1, sack bits for 2 and 3
    tx.on_ack(1, 0b11)
    assert list(tx.pending) == [1] and tx.pending[1].nack_hint
    tx.advance(); tx.advance()
    e = tx.pick_retransmit()
    assert e is not None and e.seq == 1
    tx.sent(e, retransmit=True)
    assert tx.pick_retransmit() is None
    tx.on_ack(4, 0)
    assert tx.outstanding() == 0 and tx.stats["acked"] == 4
    rx = RxReorder(window=8)
    rx.expected = 65534
    assert rx.offer(65535, 1, b"b") == []
    assert rx.ack_fields() == (65534, 0b1)
    out = rx.offer(65534, 1, b"a")
    assert [o[0] for o in out] == [65534, 65535] and rx.expected == 0
    assert rx.offer(65535, 1, b"b") == [] and rx.stats["dups"] == 1
    assert rx.offer(9, 1, b"x") == [] and rx.stats["out_of_window"] == 1


def test_secure_tamper_replay_bind():
    """One receiver, because the replay table lives in the shared context and
    every sealed body may open once: tamper (AUTH_FAIL, no state change), then
    the valid frame, its exact replay (ARQ dup), a header swap on a fresh
    body (bind mismatch) and the old body under a new sequence (REPLAY)."""
    ca, cb = loopback_crypto_pair()
    cfg = BitpipeConfig(crypto="secure", payload_bytes=80)
    a, b = LinkEngine(cfg, ca, "A"), LinkEngine(cfg, cb, "B")
    text = b"first message that will be tampered with"
    a.send(1, text)
    raw = a.next_frame()                                    # data frame seq 0 (before FEC)
    hdr, body = fr.unpack_frame(raw, 80, cfg.crc)
    assert hdr.data and hdr.seq == 0
    # 1. tampered ciphertext with a refreshed CRC: only the tag can catch it
    bad = bytearray(body)
    bad[10] ^= 0x01
    tampered = fr.pack_frame(hdr, bytes(bad), 80, cfg.crc)
    assert b.feed_bits(llr_of(a.frame_to_bits(tampered))) == []
    assert b.status()["open_failed"] == 1
    assert b.status()["crypto"]["verdicts"].get("AUTH_FAIL") == 1
    assert b.status()["rx"]["delivered"] == 1               # the ARQ took seq 0; open refused it
    # 2. the valid copy is now a duplicate for the ARQ (seq 0 is done) and never reaches open
    assert b.feed_bits(llr_of(a.frame_to_bits(raw))) == []
    assert b.status()["rx"]["dups"] == 1 and b.status()["open_failed"] == 1
    # 3. a fresh frame (seq 1) opens and delivers; its exact replay is an ARQ dup
    a.send(1, b"second")
    raw2 = a.next_frame()
    assert b.feed_bits(llr_of(a.frame_to_bits(raw2))) == [(1, b"second")]
    assert b.feed_bits(llr_of(a.frame_to_bits(raw2))) == []
    assert b.status()["rx"]["dups"] == 2 and b.status()["open_failed"] == 1
    # 4. header swap: a fresh sealed body (seq 2) sent under stream 2 is refused by the binding
    a.send(1, b"third")
    hdr3, body3 = fr.unpack_frame(a.next_frame(), 80, cfg.crc)
    swapped = fr.pack_frame(fr.FrameHeader(flags=hdr3.flags, stream=2, seq=hdr3.seq, ack=hdr3.ack,
                                           sack=hdr3.sack), body3, 80, cfg.crc)
    assert b.feed_bits(llr_of(a.frame_to_bits(swapped))) == []
    assert b.status()["bind_mismatch"] == 1
    # 5. an old sealed body under the next sequence number reaches open() and is a REPLAY
    forged = fr.pack_frame(fr.FrameHeader(flags=fr.FLAG_DATA, stream=1, seq=3), body3, 80, cfg.crc)
    assert b.feed_bits(llr_of(a.frame_to_bits(forged))) == []
    st = b.status()
    assert st["open_failed"] == 2 and st["crypto"]["verdicts"].get("REPLAY") == 1
    assert st["bytes_out"] == len(b"second")


def test_idle_frames_keep_lock():
    cfg = BitpipeConfig()
    a, b = LinkEngine(cfg, name="A"), LinkEngine(cfg, name="B")
    pump(a, b, 50)
    sa, sb = a.status(), b.status()
    assert sa["idle_tx"] == 50 and sb["frames_rx"] == 50 and sb["locked"] and sa["locked"]
    assert sb["deframer"]["frames_bad"] == 0
    b.send(2, b"late data")
    got = {}
    pump(a, b, 2, None, got, {})
    assert bytes(got[2]) == b"late data"


def test_mux_round_robin():
    cfg = BitpipeConfig(payload_bytes=16)
    a, b = LinkEngine(cfg, name="A"), LinkEngine(cfg, name="B")
    a.send(1, b"chat " * 10)
    a.send(2, b"file " * 10)
    got = {}
    pump(a, b, 12, None, {}, got)
    assert bytes(got[1]) == b"chat " * 10 and bytes(got[2]) == b"file " * 10
    frames = a.status()["data_tx"]
    assert frames == 8                                    # 50 bytes each at 13 a frame


def test_soft8_face():
    cfg = BitpipeConfig()
    a, b = LinkEngine(cfg, name="A"), LinkEngine(cfg, name="B")
    a.send(1, b"soft bits")
    bits = a.next_frame_bits()
    soft = np.where(bits == 1, 100, -100).astype(np.int8)
    soft[40:60] = 3                                        # weak, wrong-ish region
    out = b.feed_soft8(soft.tobytes())
    assert out == [(1, b"soft bits")]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"] + sys.argv[1:]))


def _file_ok(got, data):
    from dpmg.bitpipe import streams as st
    return any(x.ok and x.data == data for x in st.FileReceiver().feed(bytes(got)))


def test_station_drops_own_frames():
    a = LinkEngine(BitpipeConfig(payload_bytes=64, station=0), name="A")
    a.send(1, b"hello from A")
    bits = a.next_frame_bits()
    assert a.tx.outstanding() == 1
    out = a.feed_hard(bits)                      # A hears its own frame
    st = a.status()
    assert out == [] and st["own_rx"] == 1 and st["frames_rx"] == 0
    assert a.tx.outstanding() == 1, "its own ack field must not acknowledge its own frame"
    assert st["rx_expected"] == 0 and st["peer_seen"] is False
    b = LinkEngine(BitpipeConfig(payload_bytes=64, station=1), name="B")
    got = b.feed_hard(bits)
    assert got == [(1, b"hello from A")] and b.status()["own_rx"] == 0


def test_no_station_hears_itself():
    a = LinkEngine(BitpipeConfig(payload_bytes=64), name="A")
    a.send(1, b"loop")
    got = a.feed_hard(a.next_frame_bits())
    assert got == [(1, b"loop")] and a.status()["own_rx"] == 0


@pytest.mark.parametrize("qdepth", [3, 8])
def test_outage_with_self_hearing(qdepth):
    from dpmg.bitpipe import streams as st
    rng = np.random.default_rng(2)
    A = LinkEngine(BitpipeConfig(payload_bytes=64, station=0), name="A")
    B = LinkEngine(BitpipeConfig(payload_bytes=64, station=1), name="B")
    fa, fb = rng.bytes(4096), rng.bytes(4096)
    A.send(st.STREAM_FILE, st.file_record("a.bin", fa))
    B.send(st.STREAM_FILE, st.file_record("b.bin", fb))
    got_a, got_b = bytearray(), bytearray()
    qa = [None] * qdepth                         # what A sends now is on the air qdepth slots later
    silent = range(8, 28)
    for t in range(1500):
        if t == silent.start:
            qa = [None] * qdepth                 # stop_tx: the queued frames never reach the air
        b_bits = B.next_frame_bits()
        if t in silent:
            heard_by_b = b_bits                  # B's own TX leaking into B's RX, nothing from A
        else:
            qa.append(A.next_frame_bits())
            heard_by_b = qa.pop(0)
        if heard_by_b is not None:
            for s, d in B.feed_hard(heard_by_b):
                if s == st.STREAM_FILE:
                    got_b += d
        for s, d in A.feed_hard(b_bits):
            if s == st.STREAM_FILE:
                got_a += d
        if _file_ok(got_a, fb) and _file_ok(got_b, fa):
            break
    assert _file_ok(got_b, fa), f"A to B stalled: A pending {list(A.tx.pending)}, B expects {B.rx.expected}"
    assert _file_ok(got_a, fb), "B to A stalled"
    assert B.status()["own_rx"] >= len(silent) - 2
    assert not any(x.name == "b.bin" for x in st.FileReceiver().feed(bytes(got_b))), "B delivered its own file"
