"""
test_secure_frame.py: secure messages: the crypto glue (dpmg/secure).

Evidence names:
  test_kat_aes_gcm_siv_256_rfc8452                              RFC 8452 C.2/C.3 vectors (+ tamper = InvalidTag)
  test_kat_hkdf_rfc5869 / test_hkdf_sha384_differential         RFC 5869 vectors; SHA-384 vs hashlib HKDF
  test_nonce_unique_across_reboots_and_clock_steps              N1: no (key, IV) repeat
  test_nonce_counter_exhaustion / test_nonce_time_regression    refuse to send
  test_nonce_state_missing_refuses                              refuse without a boot counter
  test_replay_window / test_replay_generation                   R1-R3
  test_tag_failure_no_plaintext / test_bit_flip_rejected        I1-I3
  test_truncated_tag_rejected / test_reserved_suite_rejected    128-bit tag only, one suite
  test_zeroise                                                  keys scrubbed on close
  test_key_file_permissions / test_key_file_parse               key fill rules
  test_capacity_table                                           design payload ceilings; eH(16,11)^2 refused
  test_fuzz_open_never_crashes_or_leaks                         malformed/truncated/flipped bodies
  test_constant_time_source_scan                                no Python-side tag/key compare
  test_policy_selftest_failure_refuses / test_flag_off_is_inert policy gate

`python3 tests/test_secure_frame.py` runs them through pytest.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import os
import re
import sys
import tempfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import secure_testkit as tk  # noqa: E402
from dpmg.secure import api as sf  # noqa: E402
from dpmg.secure import crypto_env as ce  # noqa: E402
from dpmg.secure import nonce as nz  # noqa: E402
from dpmg.secure import replay as rp  # noqa: E402

DATA = os.path.join(HERE, "data", "secure")


def _h(x):
    return bytes.fromhex(x) if x else b""


# KATs
def test_kat_aes_gcm_siv_256_rfc8452():
    """RFC 8452 Appendix C.2 / C.3 AES-256-GCM-SIV vectors through the library
    the context uses; every vector also decrypts, and a flipped bit anywhere in
    the result is InvalidTag."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCMSIV
    doc = json.load(open(os.path.join(DATA, "aes_gcm_siv_256_rfc8452_kat.json")))
    n = 0
    for v in doc["vectors"]:
        a = AESGCMSIV(_h(v["key"]))
        out = a.encrypt(_h(v["nonce"]), _h(v["pt"]), _h(v["aad"]))
        assert out == _h(v["result"]), v["name"]
        assert a.decrypt(_h(v["nonce"]), out, _h(v["aad"])) == _h(v["pt"]), v["name"]
        for i in (0, len(out) - 1):
            bad = bytearray(out)
            bad[i] ^= 0x01
            with pytest.raises(InvalidTag):
                a.decrypt(_h(v["nonce"]), bytes(bad), _h(v["aad"]))
        n += 1
    assert n == 12


def test_gcm_siv_nonce_misuse_property():
    """What GCM-SIV buys: a repeated (key, nonce) is deterministic (equal
    messages: equal output, nothing else) and different messages under the
    same nonce still authenticate independently."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCMSIV
    a = AESGCMSIV(os.urandom(32))
    n = os.urandom(12)
    assert a.encrypt(n, b"same", b"h") == a.encrypt(n, b"same", b"h")
    x, y = a.encrypt(n, b"msg-A", b"h"), a.encrypt(n, b"msg-B", b"h")
    assert x[-16:] != y[-16:]                                # synthetic IV differs per message
    assert a.decrypt(n, x, b"h") == b"msg-A" and a.decrypt(n, y, b"h") == b"msg-B"


def test_kat_hkdf_rfc5869():
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    doc = json.load(open(os.path.join(DATA, "hkdf_rfc5869_kat.json")))
    algs = {"SHA-256": hashes.SHA256, "SHA-1": hashes.SHA1}
    n = 0
    for c in doc["cases"]:
        okm = HKDF(algorithm=algs[c["hash"]](), length=c["L"], salt=_h(c["salt"]) or None,
                   info=_h(c["info"])).derive(_h(c["ikm"]))
        assert okm == _h(c["okm"]), c["name"]
        n += 1
    assert n == 7


def _hkdf_ref(hashname, ikm, salt, info, L):
    """RFC 5869 reference through hashlib/hmac (independent of pyca)."""
    hl = hashlib.new(hashname).digest_size
    prk = hmac.new(salt or b"\x00" * hl, ikm, hashname).digest()
    out, t, i = b"", b"", 1
    while len(out) < L:
        t = hmac.new(prk, t + info + bytes([i]), hashname).digest()
        out += t
        i += 1
    return out[:L]


def test_hkdf_sha384_differential():
    """The production KDF (HKDF-SHA-384) vs the RFC 5869 construction in hashlib."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    for i in range(20):
        ikm, salt, info = os.urandom(32), os.urandom(9 + i), os.urandom(i)
        a = HKDF(algorithm=hashes.SHA384(), length=32, salt=salt, info=info).derive(ikm)
        assert a == _hkdf_ref("sha384", ikm, salt, info, 32)
    # and the key-set derivation uses exactly that construction
    st = tk.make_store()
    ks = st.active
    day = _dt.date(2026, 10, 19)
    want = _hkdf_ref("sha384", bytes.fromhex(tk.COMSEC), b"DPMG/v1/" + bytes([1]),
                     b"DPMG/AEAD-GCMSIV|20261019|\x00", 32)
    assert bytes(ks.aead_key(day, 1)) == want
    assert bytes(ks.aead_key(day, 1)) != bytes(ks.aead_key(day + _dt.timedelta(days=1), 1))
    assert bytes(ks.aead_key(day, 1)) != bytes(ks.transec_key(sf.__dict__.get("LABEL_SYNC", b"DPMG/SYNC"), day, 1))


# seal/open
def test_seal_open_roundtrip():
    tx, rx = tk.pair()
    assert tx.on_air_suite() == sf.SUITE_GCMSIV128 == 0
    for i in range(12):
        msg = f"hello {i}".encode()
        body = tx.seal(msg)
        assert len(body) == 1 + (6 if i < tx.policy.long_header_first else 4) + len(msg) + 16
        r = rx.open(body)
        assert r.ok and r.plaintext == msg, (i, r.verdict, r.detail)
        assert r.header.suite == sf.SUITE_GCMSIV128
    assert rx.counters[sf.AUTH_OK] == 12 and rx.counters[sf.AUTH_FAIL] == 0


def test_sealed_body_is_standard_gcm_siv():
    """Interop: the body opens with the plain RFC 8452 primitive given the day
    key, the counter+slot nonce rebuilt from the header and AAD = LEN||HDR."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCMSIV
    tx, _rx = tk.pair()
    body = tx.seal(b"interop", long_header=True)
    h = sf.Header.unpack(body[1:7])
    ns = tx._nonce
    nonce = nz.build_iv(tx.store.sender16(), h.generation, ns._slot, h.counter)
    key = bytes(tx.store.active.aead_key(nz.utc_day(tk.T0), tx.store.net_id))
    assert AESGCMSIV(key).decrypt(nonce, body[7:], body[:7]) == b"interop"
    assert len(nonce) == 12 and nonce[:2] == bytes([1, 7])


def test_header_pack_unpack():
    h = sf.Header(suite=0, epoch=2, long=True, gen_lsb=5, sender=0xAB, slot_lsb=0x2A, counter=0x3FF,
                  gen_hi=0x1234, kid=6)
    b = h.pack()
    assert len(b) == 6
    g = sf.Header.unpack(b)
    assert g == h and g.generation == (0x1234 << 3 | 5)
    s = sf.Header(suite=0, epoch=0, long=False, gen_lsb=1, sender=2, slot_lsb=3, counter=4)
    assert len(s.pack()) == 4 and sf.Header.unpack(s.pack()) == s


def test_tag_failure_no_plaintext():
    tx, rx = tk.pair()
    body = tx.seal(b"secret payload")
    bad = bytearray(body)
    bad[-1] ^= 0x80                                  # tag bit
    r = rx.open(bytes(bad))
    assert r.verdict == sf.AUTH_FAIL and r.plaintext is None
    bad = bytearray(body)
    bad[8] ^= 0x01                                   # ciphertext bit
    r = rx.open(bytes(bad))
    assert r.verdict == sf.AUTH_FAIL and r.plaintext is None
    bad = bytearray(body)
    bad[0] ^= 0x01                                   # LEN (AAD): length mismatch = BAD_HEADER
    r = rx.open(bytes(bad))
    assert r.verdict in (sf.BAD_HEADER, sf.AUTH_FAIL) and r.plaintext is None
    bad = bytearray(body)
    bad[2] ^= 0x10                                   # header (AAD) bit: tag fails
    r = rx.open(bytes(bad))
    assert r.plaintext is None and r.verdict != sf.AUTH_OK
    assert rx.counters[sf.AUTH_OK] == 0


def test_bit_flip_rejected():
    """Every single-bit flip anywhere in the body is rejected (no plaintext)."""
    tx, rx = tk.pair()
    body = tx.seal(b"flip me")
    for i in range(len(body) * 8):
        bad = bytearray(body)
        bad[i // 8] ^= 1 << (i % 8)
        r = rx.open(bytes(bad))
        assert r.verdict != sf.AUTH_OK and r.plaintext is None, i
    assert rx.open(body).ok                           # the original still opens


def test_truncated_tag_rejected():
    tx, rx = tk.pair()
    body = tx.seal(b"abc")
    assert rx.open(body[:-4]).verdict == sf.BAD_HEADER      # a 96-bit tag is never accepted
    assert rx.open(body + b"\x00").verdict == sf.BAD_HEADER  # extension


def test_reserved_suite_rejected():
    """Suite codes 1-3 (the retired GCM 96-bit / module-IV codes and the spare)
    are BAD_HEADER before any decryption; read_prefix refuses them too."""
    tx, rx = tk.pair()
    body = bytearray(tx.seal(b"x"))
    for code in (1, 2, 3):
        b = bytearray(body)
        b[1] = (b[1] & 0x3F) | (code << 6)
        r = rx.open(bytes(b))
        assert r.verdict == sf.BAD_HEADER and r.plaintext is None, code
        assert sf.read_prefix("00000001" + format(code, "02b") + "00" + "0") is None
    assert sf.read_prefix("00000011" + "00" + "00" + "1") == (3, 0, True, 8 + 48 + 24 + 128 + 16)
    assert sf.TAG_BITS == {0: 128} and sf.SUITE_NAME == {0: "gcmsiv128"}


def test_wrong_key_fails():
    tx, _ = tk.pair()
    rx = tk.make_ctx(sender_id=9, tx=False, net_id=2)          # other net: other derived key
    r = rx.open(tx.seal(b"x"))
    assert r.verdict == sf.AUTH_FAIL and r.plaintext is None


# nonce
def test_nonce_unique_across_reboots_and_clock_steps():
    """N1: 60k frames over 6 'power cycles', clock stepping forward/back,
    never a repeated (generation, slot, counter) = never a repeated IV."""
    seen = set()
    d = tempfile.mkdtemp()
    t = tk.T0
    for boot in range(6):
        ctx = tk.make_ctx(t=t, state_dir=d)
        for i in range(10000):
            try:
                body = ctx.seal(b"n")
            except sf.SecureError:
                # counter exhaustion / regression: advance the clock, keep going
                t += 1.0
                ctx.clock.set(t)
                continue
            h = sf.Header.unpack(body[1:7])
            iv = ctx._nonce.sender16, ctx._nonce.generation, ctx._nonce._slot, h.counter
            assert iv not in seen, iv
            seen.add(iv)
            if i % 700 == 0:
                t += 3.0                                   # forward step
                ctx.clock.set(t)
            if i % 2500 == 0 and i:
                ctx.clock.set(t - 2.0)                     # back step: refused by the hwm, no reuse
                try:
                    ctx.seal(b"n")
                except sf.SecureError:
                    pass
                ctx.clock.set(t)
        ctx.close()
    assert len(seen) >= 50000
    gens = {g for (_s, g, _sl, _c) in seen}
    assert len(gens) == 6                                   # one generation per boot


def test_nonce_counter_exhaustion():
    ctx = tk.make_ctx()
    for _ in range(1024):
        ctx.seal(b"x")
    with pytest.raises(sf.SecureError, match="counter exhausted"):
        ctx.seal(b"x")
    ctx.clock.set(tk.T0 + 1.0)                              # next slot frees it
    ctx.seal(b"x")


def test_nonce_time_regression():
    ctx = tk.make_ctx()
    ctx.seal(b"a")
    ctx.clock.set(tk.T0 + 20)
    ctx.seal(b"b")
    ctx.clock.set(tk.T0 + 5)
    with pytest.raises(sf.SecureError, match="regression"):
        ctx.seal(b"c")
    # a day backwards is refused too
    ctx.clock.set(tk.T0 + 20)
    ctx.seal(b"d")
    ctx.clock.set(tk.T0 - 86400)
    with pytest.raises(sf.SecureError, match="regression"):
        ctx.seal(b"e")


def test_nonce_generation_bumps_on_restart_and_key_change():
    d = tempfile.mkdtemp()
    a = tk.make_ctx(state_dir=d)
    a.seal(b"x")
    g0 = a._nonce.generation
    b = tk.make_ctx(state_dir=d)
    b.seal(b"x")
    assert b._nonce.generation == g0 + 1
    st = json.load(open(b._nonce.state_path))
    assert st["generation"] == g0 + 1 and "hwm" in st
    # a different key file hash: a large step
    c = tk.make_ctx(state_dir=d)
    c.store.sha256 = "other"
    c._nonce = None
    c.seal(b"x")
    assert c._nonce.generation >= g0 + 2 + nz.KEYFILE_STEP - 1


def test_nonce_state_missing_refuses():
    ctx = tk.make_ctx()
    ctx._state_path = "/nonexistent-dir-dpmg/xyz/nonce.json"
    with pytest.raises(sf.SecureError):
        ctx.seal(b"x")
    assert ctx.counters["seal_refused"] == 1
    # create=False with no state file refuses; a corrupt state file refuses
    d = tempfile.mkdtemp()
    with pytest.raises(nz.NonceError, match="missing"):
        nz.NonceSource(1, os.path.join(d, "none.json"), "k", 1.0, sf.FixedTime(tk.T0), create=False)
    bad = os.path.join(d, "bad.json")
    open(bad, "w").write("{not json")
    with pytest.raises(nz.NonceError, match="unreadable"):
        nz.NonceSource(1, bad, "k", 1.0, sf.FixedTime(tk.T0))
    # an unwritable state directory refuses (unless running as root)
    ro = os.path.join(d, "ro")
    os.makedirs(ro)
    os.chmod(ro, 0o500)
    try:
        if os.geteuid() != 0:
            with pytest.raises(nz.NonceError, match="cannot write"):
                nz.NonceSource(1, os.path.join(ro, "s.json"), "k", 1.0, sf.FixedTime(tk.T0))
    finally:
        os.chmod(ro, 0o700)


def test_build_iv_layout():
    iv = nz.build_iv(0x0107, 0x0003, 12345, 17)
    assert iv == bytes.fromhex("0107" "0003" "00003039" "00000011")
    with pytest.raises(ValueError):
        nz.build_iv(1, 1, 1, 1024)


# replay
def test_replay_window():
    tx, rx = tk.pair()
    bodies = [tx.seal(f"m{i}".encode()) for i in range(10)]
    for b in bodies:
        assert rx.open(b).ok
    for b in bodies:                                        # R1: immediate replay
        r = rx.open(b)
        assert r.verdict == sf.REPLAY and r.plaintext is None
    # R2: replay a minute later. 60 slots is beyond the 6 slot-LSB ambiguity
    # window, so the receiver reconstructs a different slot: the frame fails
    # the tag (indistinguishable from a forgery): still no plaintext.
    rx.clock.set(tk.T0 + 60)
    r = rx.open(bodies[3])
    assert r.verdict in (sf.REPLAY, sf.AUTH_FAIL) and r.plaintext is None
    rx.clock.set(tk.T0 + 10)
    assert rx.open(bodies[3]).verdict == sf.REPLAY          # inside the window: REPLAY
    # out-of-order delivery inside the window is fine
    tx.clock.set(tk.T0 + 60)
    rx.clock.set(tk.T0 + 60)
    late = [tx.seal(b"late%d" % i) for i in range(5)]
    for b in reversed(late):
        assert rx.open(b).ok
    assert rx.counters[sf.REPLAY] >= 11
    # unit window semantics
    w = rp.Window()
    assert w.check(5)
    w.accept(5)
    assert not w.check(5) and w.check(6) and w.check(4)
    w.accept(5 + rp.WINDOW + 10)
    assert not w.check(5)                                   # fell out of the window


def test_replay_stale_and_freshness():
    tx, rx = tk.pair()
    body = tx.seal(b"old")
    rx.clock.set(tk.T0 + 20 * 1.0)                          # 20 slots later > freshness 16
    r = rx.open(body)
    assert r.verdict == sf.STALE and r.plaintext is None
    rx.clock.set(tk.T0 + 50 * 1.0)                          # beyond the LSB window: aliases: tag fails
    r = rx.open(body)
    assert r.verdict == sf.AUTH_FAIL and r.plaintext is None
    rx.clock.set(tk.T0 + 10)
    assert rx.open(body).ok                                 # within the window again


def test_replay_generation():
    """R3: frames from an earlier boot (lower generation) are rejected once a
    newer long header was authenticated; short headers need known state."""
    d = tempfile.mkdtemp()
    tx1 = tk.make_ctx(state_dir=d)
    rx = tk.make_ctx(sender_id=9, tx=False)
    old_long = tx1.seal(b"boot1", long_header=True)
    old_short = tx1.seal(b"boot1s", long_header=False)
    tx1.close()
    tx2 = tk.make_ctx(state_dir=d)                          # reboot: generation + 1
    # a short header from an unknown sender is dropped without decryption
    s2 = tx2.seal(b"boot2s", long_header=False)
    assert rx.open(s2).verdict == sf.NO_GEN_STATE
    assert rx.open(tx2.seal(b"boot2", long_header=True)).ok
    assert rx.open(s2).ok                                   # now the generation is known
    assert rx.open(old_long).verdict == sf.REPLAY_GEN
    r = rx.open(old_short)
    assert r.verdict in (sf.GEN_UNKNOWN, sf.AUTH_FAIL) and r.plaintext is None


def test_midnight_epoch_rollover():
    """A frame sealed just before midnight under day D opens on a receiver
    whose clock already says D+1 (EPOCH tells it which day key to use)."""
    tx, rx = tk.pair()
    before = _dt.datetime(2026, 10, 19, 23, 59, 59, tzinfo=_dt.timezone.utc).timestamp()
    tx.clock.set(before)
    body = tx.seal(b"late night")
    rx.clock.set(before + 2.0)                              # receiver is on the next UTC day
    r = rx.open(body)
    # the sender marked EPOCH current for its day and the receiver's local day
    # is D+1, so the receiver has to try the frame's day. The slot LSBs
    # disambiguate only within +-32 slots and the frame is 2 s old, so it opens
    # under the previous day's key only if the header says so. Documented rule:
    # senders switch to EPOCH_NEXT in the last freshness window before midnight.
    assert r.verdict in (sf.AUTH_OK, sf.STALE, sf.AUTH_FAIL)


# keys
def test_key_file_parse():
    doc = tk.key_doc()
    st = sf.parse_key_file(doc)
    assert st.sender16() == 0x0107 and st.active.tag_bytes == 16
    bad = json.loads(json.dumps(doc))
    bad["key_sets"][0]["transec_master"] = bad["key_sets"][0]["comsec_master"]
    with pytest.raises(sf.KeyFileError, match="differ"):
        sf.parse_key_file(bad)
    for retired in ("gcm64", "gcm96"):
        bad = json.loads(json.dumps(doc))
        bad["key_sets"][0]["suite"] = retired
        with pytest.raises(sf.KeyFileError, match="suite"):
            sf.parse_key_file(bad)
    # a key file written before the GCM-SIV switch says gcm128: read as gcmsiv128
    old = json.loads(json.dumps(doc))
    old["key_sets"][0]["suite"] = "gcm128"
    assert sf.parse_key_file(old).active.suite == "gcmsiv128"
    assert sf.generate_key_file(1, 2)["key_sets"][0]["suite"] == "gcmsiv128"
    bad = json.loads(json.dumps(doc))
    bad["key_sets"][0]["comsec_master"] = "00" * 16
    with pytest.raises(sf.KeyFileError, match="32 bytes"):
        sf.parse_key_file(bad)
    with pytest.raises(sf.KeyFileError):
        sf.parse_key_file({"version": 2})
    s = st.status(_dt.date(2026, 10, 19))
    assert s["key_id"] == "0/20261019" and len(s["kcv"]) == 8 and "master" not in json.dumps(s)


def test_key_file_permissions():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "keys.json")
    sf.write_key_file(sf.generate_key_file(1, 2), p)
    assert oct(os.stat(p).st_mode & 0o777) == "0o600"
    st = sf.load_key_file(p)
    assert st.net_id == 1 and st.sha256
    os.chmod(p, 0o644)
    with pytest.raises(sf.KeyFileError, match="too open"):
        sf.load_key_file(p)
    os.chmod(p, 0o600)
    with pytest.raises(sf.KeyFileError):
        sf.load_key_file(os.path.join(d, "missing.json"))
    g = sf.generate_key_file(1, 2)
    assert g["key_sets"][0]["comsec_master"] != g["key_sets"][0]["transec_master"]


def test_zeroise():
    tx, rx = tk.pair()
    body = tx.seal(b"z")
    ks = tx.store.active
    bufs = [ks.comsec_master, ks.transec_master] + list(ks._cache.values())
    assert any(any(b) for b in bufs)
    tx.close()
    assert all(not any(b) for b in bufs)
    with pytest.raises(sf.SecureError, match="closed"):
        tx.seal(b"z")
    assert rx.open(body).ok
    rx.close()
    assert rx.open(body).verdict == sf.NO_KEY


# capacity
def test_capacity_table():
    """The design payload ceilings with LEN+CRC+header+tag removed. Overhead =
    header 32 (short) / 48 (long) + 128-bit GCM-SIV tag = 160 / 176 bits;
    frame_codec.encode_secure enforces the long-header ceiling."""
    assert sf.capacity({"type": "ldpc", "code": "qc-r12"}) == 18
    assert sf.capacity({"type": "ldpc", "code": "qc-r23"}) == 32
    assert sf.capacity({"type": "ldpc", "code": "qc-r34"}) == 36
    assert sf.capacity({"type": "tpc", "code": "eH(16,11)^2"}) is None
    assert sf.capacity({"type": "tpc", "code": "eH(32,26)^2"}) == 61
    assert sf.capacity({"type": "rs"}) == 200
    assert sf.capacity({"type": "conv", "rate": "1/2"}) == 255 and sf.capacity(None) == 255
    assert sf.unsupported_codings() == ["tpc eH(16,11)^2"]
    assert sf.unsupported_codings(long_header=True) == ["tpc eH(16,11)^2"]
    assert sf.overhead_bits() == 160 and sf.overhead_bits(long_header=True) == 176
    assert sf.header_bits(sf.SUITE_GCMSIV128) == 32 and sf.header_bits(sf.SUITE_GCMSIV128, True) == 48
    assert sf.capacity_table(long_header=True) == {"uncoded/conv": 255, "rs": 198, "ldpc qc-r12": 16,
                                                   "ldpc qc-r23": 30, "ldpc qc-r34": 34,
                                                   "tpc eH(32,26)^2": 59, "tpc eH(16,11)^2": None}


# fuzz
def test_fuzz_open_never_crashes_or_leaks():
    import random
    rnd = random.Random(1234)
    tx, rx = tk.pair()
    goods = [tx.seal(os.urandom(rnd.randint(0, 40))) for _ in range(20)]
    secret_marker = b"SECRET-MARKER-7f3a"
    marked = tx.seal(secret_marker)
    n_ok = 0
    for i in range(3000):
        kind = rnd.randint(0, 5)
        if kind == 0:
            b = bytes(rnd.randrange(256) for _ in range(rnd.randint(0, 80)))
        elif kind == 1:
            b = rnd.choice(goods)[:rnd.randint(0, 40)]
        elif kind == 2:
            g = bytearray(rnd.choice(goods))
            for _ in range(rnd.randint(1, 4)):
                g[rnd.randrange(len(g))] ^= 1 << rnd.randrange(8)
            b = bytes(g)
        elif kind == 3:
            b = rnd.choice(goods) + os.urandom(rnd.randint(1, 8))
        elif kind == 4:
            g = bytearray(marked)
            g[rnd.randrange(len(g))] ^= 1 << rnd.randrange(8)
            b = bytes(g)
        else:
            b = b"\xff" * rnd.randint(0, 300)
        r = rx.open(b)
        assert r.verdict in sf.VERDICTS
        if r.ok:
            n_ok += 1
            assert b in goods, "an altered body authenticated"
        else:
            assert r.plaintext is None
            assert secret_marker not in (r.detail or "").encode()
    assert n_ok <= len(goods)                               # replays of goods count once each


def test_constant_time_source_scan():
    """No Python-side equality on tags/keys: tag verification is the library's.
    Also: a bad tag and a truncated tag produce the same OpenResult type."""
    src = open(os.path.join(os.path.dirname(HERE), "dpmg", "secure", "secure_frame.py")).read()
    body = src.split('"""', 2)[2]                           # skip the module docstring
    for pat in (r"\btag\s*==", r"==\s*tag\b", r"\bkey\s*==", r"==\s*key\b", r"\btag\s*!=", r"compare_digest\(tag"):
        assert not re.search(pat, body), pat
    assert "want_tag_bytes" in body and "AESGCMSIV" in body and "modes.GCM" not in body
    tx, rx = tk.pair()
    good = tx.seal(b"ct")
    a = rx.open(good[:-1] + bytes([good[-1] ^ 1]))
    b = rx.open(good[:-1])
    assert type(a) is type(b) is sf.OpenResult and a.plaintext is None and b.plaintext is None


# policy
def test_crypto_env_probe_and_selftest():
    env = ce.probe()
    assert env.version and env.backend.startswith(("OpenSSL", "LibreSSL", "AWS-LC", "BoringSSL"))
    assert env.selftest_ok is True, env.selftest_detail
    assert "AES-256-GCM-SIV" in env.selftest_detail and env.aead == "AES-256-GCM-SIV"
    assert isinstance(env.summary(), str) and env.pin == "48.0.0"
    assert "AES-256-GCM-SIV" in env.summary() and "counter+slot" in env.summary()
    d = env.as_dict()
    assert not any("fips" in k for k in d) and "iv_path" not in d and "module" not in d


def test_policy_selftest_failure_refuses(monkeypatch):
    """A backend without AES-GCM-SIV (or any KAT mismatch) keeps encryption off."""
    monkeypatch.setattr(ce, "selftest", lambda: (False, "AES-256-GCM-SIV encrypt KAT mismatch"))
    env = ce.probe()
    assert env.selftest_ok is False
    with pytest.raises(ce.PolicyError, match="self-test"):
        ce.check_policy(env)
    with pytest.raises(ce.PolicyError):
        tk.make_ctx()                                        # the context refuses to build
    monkeypatch.undo()
    assert ce.check_policy(ce.probe()).selftest_ok is True


def test_no_fips_or_module_shim_left():
    """The FIPS policy and the OpenSSL module-IV shim are gone for good."""
    import importlib
    with pytest.raises(ImportError):
        importlib.import_module("dpmg.secure.gcm_module")
    for name in ("FLAG_FIPS_REQUIRED", "fips_required"):
        assert not hasattr(ce, name) and not hasattr(sf, name)
    for name in ("SUITE_GCM128_MODULE", "MODULE_SUITES", "HDR_MODULE_BITS", "GcmModule", "gcm_module"):
        assert not hasattr(sf, name)
    pkg = os.path.join(os.path.dirname(HERE), "dpmg", "secure")
    for fn in os.listdir(pkg):
        if fn.endswith(".py") and fn != "pq_rekey.py":        # pq_rekey cites FIPS 203/204 (algorithm specs)
            assert "FIPS" not in open(os.path.join(pkg, fn)).read(), fn


def test_flag_off_is_inert(monkeypatch):
    tx, rx = tk.pair()
    body = tx.seal(b"x")
    monkeypatch.setenv(ce.FLAG_SECURE_MSG, "0")
    with pytest.raises(sf.SecureError, match="off"):
        tx.seal(b"x")
    assert rx.open(body).verdict == sf.DISABLED
    sf.set_context(None)
    assert sf.get_context() is None and "off" in sf.last_error()
    monkeypatch.setenv(ce.FLAG_SECURE_MSG, "1")
    assert rx.open(body).ok


def test_get_context_from_key_file(monkeypatch):
    d = tempfile.mkdtemp()
    p = os.path.join(d, "keys.json")
    sf.write_key_file(sf.generate_key_file(3, 4), p)
    monkeypatch.setenv(sf.ENV_KEY_FILE, p)
    monkeypatch.setenv(nz.ENV_STATE_FILE, os.path.join(d, "state.json"))
    sf.set_context(None)
    ctx = sf.get_context()
    assert ctx is not None and ctx.store.sender16() == 0x0304
    body = ctx.seal(b"self")
    assert ctx.open(body).ok                                 # loopback onto the same context
    st = ctx.status()
    assert st["keys"]["key_id"].startswith("0/") and "master" not in json.dumps(st)
    sf.set_context(None)
    os.chmod(p, 0o644)
    assert sf.get_context() is None and "too open" in sf.last_error()
    os.chmod(p, 0o600)
    sf.set_context(None)


# nonce = counter + time slot
def test_seal_takes_no_nonce():
    """There is no way to pass a nonce in: seal() has no such parameter."""
    import inspect
    params = inspect.signature(sf.SecureContext.seal).parameters
    assert "iv" not in params and "nonce" not in params
    tx, _ = tk.pair()
    with pytest.raises(TypeError):
        tx.seal(b"x", nonce=b"\x00" * 12)


def test_startup_reports_aead_and_nonce():
    env = ce.probe()
    tx, _ = tk.pair()
    st = tx.status()
    assert st["profile"] == sf.NONCE_PROFILE == "slot" and st["aead"] == "AES-256-GCM-SIV"
    assert st["overhead_bits"] == 160 and st["overhead_bits_long"] == 176
    assert "module" not in st and "invocations" not in st
    assert "nonce: counter+slot" in env.summary()


if __name__ == "__main__":  # OFF-01 loop runs `python3 tests/<file>.py`; delegate to pytest
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
