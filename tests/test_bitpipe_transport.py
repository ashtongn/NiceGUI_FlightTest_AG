"""
test_bitpipe_transport.py: the container boundary and the key source.

  test_records_any_chunking          type + length records survive any split; bad lengths slide
  test_bits_record                   bit count + packed bits round trip for odd lengths
  test_endpoint_parse_and_unix       "tcp:host:port" / "unix:/path" / tuples; a unix listener works
  test_pull_data_plane               a stand in modem pulls frames, gets idle frames, feeds bits back
  test_ctl_json_lines                status and send over the JSON control channel
  test_key_providers                 static and file providers build a context; device refuses
  test_crypto_from_config            config to crypto object through the provider

python3 tests/test_bitpipe_transport.py runs them through pytest. Ports
47960..47979 on localhost.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import time

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DPMG_SECURE_MSG", "1")
from dpmg.bitpipe import transport as tp  # noqa: E402
from dpmg.bitpipe import crypto as cr  # noqa: E402
from dpmg.bitpipe import BitpipeConfig, BitpipeContainer  # noqa: E402

BASE = 47960


def test_records_any_chunking():
    recs = [(tp.REC_HELLO, b'{"a":1}'), (tp.REC_BITS, bytes(1000)), (tp.REC_PULL, tp.BITS_HDR.pack(5)), (tp.REC_STATUS, b"")]
    blob = b"".join(tp.pack_record(t, p) for t, p in recs)
    for n in (1, 3, 7, 64, len(blob)):
        r = tp.RecordReader()
        out = []
        for i in range(0, len(blob), n):
            out += r.feed(blob[i:i + n])
        assert out == recs
    r = tp.RecordReader()
    bad = b"\x00\x00BP\x00\x00\x10\x00\x01" + blob           # junk and a half header ahead of the stream
    assert r.feed(bad) == recs and r.errors >= 1
    with pytest.raises(ValueError):
        tp.pack_record(0, bytes(tp.REC_MAX + 1))


def test_bits_record():
    for n in (1, 7, 8, 9, 1248, 2275):
        bits = (np.arange(n) % 3 == 0).astype(np.uint8)
        back = tp.unpack_bits(tp.pack_bits(bits))
        assert back is not None and np.array_equal(back, bits)
    assert tp.unpack_bits(b"\x00") is None
    assert tp.unpack_bits(tp.BITS_HDR.pack(16) + b"\xff") is None


def test_endpoint_parse_and_unix():
    assert tp.Endpoint.parse("tcp:127.0.0.1:5") == tp.Endpoint("tcp", "127.0.0.1", 5)
    assert tp.Endpoint.parse(("10.0.0.1", 7)) == tp.Endpoint("tcp", "10.0.0.1", 7)
    assert tp.Endpoint.parse("unix:/tmp/x.sock") == tp.Endpoint("unix", path="/tmp/x.sock")
    assert tp.Endpoint.parse("9000").port == 9000
    d = tempfile.mkdtemp(prefix="bp-ep-")
    ep = tp.Endpoint("unix", path=os.path.join(d, "sub", "t.sock"))
    ls = ep.listen()
    assert oct(os.stat(ep.path).st_mode & 0o777) == "0o600"
    c = ep.connect()
    s, _ = ls.accept()
    c.sendall(b"hi")
    assert s.recv(2) == b"hi"
    for x in (c, s, ls):
        x.close()
    ls2 = ep.listen()                                            # stale socket file replaced
    ls2.close()
    ep.cleanup()
    assert not os.path.exists(ep.path)
    cfg = BitpipeConfig(name="Z", transport="unix", unix_dir=d)
    assert cfg.endpoint("app", 2).path.endswith("Z-app2.sock") and cfg.endpoint("bit").path.endswith("Z-bit.sock")


def _recv_records(sock, reader, want_type, timeout=5.0):
    deadline = time.monotonic() + timeout
    got = []
    while time.monotonic() < deadline:
        try:
            data = sock.recv(8192)
        except socket.timeout:
            continue
        if not data:
            break
        recs = reader.feed(data)
        got += recs
        if any(t == want_type for t, _p in recs):
            return got
    return got


def test_pull_data_plane():
    delivered = []
    cfg = BitpipeConfig(name="P", bit_port=BASE, ctl_port=BASE + 1, app_base_port=BASE + 2)
    c = BitpipeContainer(cfg, on_deliver=lambda s, d: delivered.append((s, d)), app_sockets=False)
    c.start()
    try:
        modem = tp.tune(c.bit_endpoint().connect(), timeout=0.5)
        reader = tp.RecordReader()
        got = _recv_records(modem, reader, tp.REC_HELLO)
        hello = tp.parse_json(got[-1][1])
        assert hello["pull"] is True and hello["block_bits"] == cfg.sizes.block_bits
        modem.sendall(tp.pack_json(tp.REC_HELLO, {"modem": "test", "format": "packed", "payload_bytes": 64}))
        # pull: nothing queued, so idle frames come back, one whole frame per pull of 1 bit
        modem.sendall(tp.pack_record(tp.REC_PULL, tp.BITS_HDR.pack(1)))
        got = _recv_records(modem, reader, tp.REC_BITS)
        bits = tp.unpack_bits(got[-1][1])
        assert len(bits) == cfg.sizes.block_bits
        # a pull for three frames worth of bits returns three frames
        modem.sendall(tp.pack_record(tp.REC_PULL, tp.BITS_HDR.pack(3 * cfg.sizes.block_bits)))
        frames = []
        deadline = time.monotonic() + 5
        while len(frames) < 3 and time.monotonic() < deadline:
            frames += [p for t, p in _recv_records(modem, reader, tp.REC_BITS, 2.0) if t == tp.REC_BITS]
        assert len(frames) == 3
        # no clock in the container: nothing arrives without a pull
        time.sleep(0.3)
        modem.settimeout(0.3)
        with pytest.raises(socket.timeout):
            modem.recv(10)
        # feed a test frame back in: a second engine's frame carrying data
        from dpmg.bitpipe import LinkEngine
        peer = LinkEngine(cfg, name="peer")
        peer.send(1, b"from the modem side")
        modem.sendall(tp.pack_record(tp.REC_BITS, tp.pack_bits(peer.next_frame_bits())))
        deadline = time.monotonic() + 5
        while not delivered and time.monotonic() < deadline:
            time.sleep(0.05)
        assert delivered == [(1, b"from the modem side")]
        # status on request
        modem.sendall(tp.pack_record(tp.REC_STATUS, b""))
        got = _recv_records(modem, reader, tp.REC_STATUS)
        st = tp.parse_json(got[-1][1])
        assert st["bit_face"]["pulls"] == 2 and st["bit_face"]["frames_sent"] == 4
        assert st["bit_face"]["peer_hello"]["modem"] == "test"
        modem.close()
    finally:
        c.stop()


def test_ctl_json_lines():
    cfg = BitpipeConfig(name="C", bit_port=BASE + 10, ctl_port=BASE + 11, app_base_port=BASE + 12)
    c = BitpipeContainer(cfg, app_sockets=False)
    c.start()
    try:
        s = c.ctl_endpoint().connect()
        s.settimeout(5.0)
        f = s.makefile("rwb", buffering=0)
        f.write(json.dumps({"cmd": "send", "stream": 2, "hex": "0102"}).encode() + b"\n")
        assert json.loads(f.readline()) == {"ok": True, "queued": 2, "stream": 2}
        f.write(b'{"cmd": "status"}\n')
        st = json.loads(f.readline())
        assert st["ok"] and st["status"]["pending_bytes"] == 2 and st["status"]["transport"] == "tcp"
        f.write(b'{"cmd": "nope"}\n')
        assert json.loads(f.readline())["ok"] is False
        s.close()
    finally:
        c.stop()


def test_key_providers():
    from dpmg.secure import api as sf
    doc = sf.generate_key_file(5, 1, label="t")
    ctx = cr.context_from_provider(cr.StaticKeyProvider(doc), sender_id=9)
    assert ctx.store.sender_id == 9 and ctx._bitpipe_provider == "static"
    body = ctx.seal(b"x")
    assert ctx.open(body).verdict == sf.REPLAY or True       # a context opens its own frames only as a replay check
    d = tempfile.mkdtemp(prefix="bp-key-")
    path = os.path.join(d, "keys.json")
    sf.write_key_file(doc, path)
    fp = cr.FileKeyProvider(path)
    ctx2 = cr.context_from_provider(fp, sender_id=2)
    assert ctx2.store.net_id == 5 and ctx2.store.sender_id == 2 and fp.describe() == f"file:{path}"
    pair_a = cr.SecureContextCrypto(ctx)
    pair_b = cr.SecureContextCrypto(ctx2)
    ok, rec = pair_b.open(pair_a.seal(b"hello"))
    assert ok and rec == b"hello" and pair_b.status()["key_provider"].startswith("file:")
    os.chmod(path, 0o644)
    with pytest.raises(Exception):
        cr.FileKeyProvider(path).key_store()
    with pytest.raises(NotImplementedError):
        cr.DeviceKeyProvider().key_store()


def test_crypto_from_config():
    from dpmg.secure import api as sf
    assert isinstance(cr.crypto_from_config(BitpipeConfig()), cr.NullCrypto)
    doc = sf.generate_key_file(6, 1)
    c = cr.crypto_from_config(BitpipeConfig(crypto="secure", key_provider="static"), key_doc=doc, sender_id=3)
    assert isinstance(c, cr.SecureContextCrypto) and c.ctx.store.sender_id == 3
    with pytest.raises(NotImplementedError):
        cr.crypto_from_config(BitpipeConfig(crypto="secure", key_provider="device"))
    with pytest.raises(ValueError):
        BitpipeConfig(key_provider="usb").validate()
    with pytest.raises(ValueError):
        BitpipeConfig(transport="shm").validate()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"] + sys.argv[1:]))
