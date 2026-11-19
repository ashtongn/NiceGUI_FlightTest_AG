"""
dpmg.secure.secure_frame: seal and open DPMG frames (secure messages).

Frame body as frame_codec codes it (after the uncoded SYNC, before FEC):

    LEN(8) | HDR | CIPHERTEXT(8*LEN) | TAG(128) | CRC-16/CCITT(16)

  LEN     ciphertext bytes (= plaintext bytes, 0..255), clear, in the AAD
  HDR     clear, authenticated (AAD = LEN byte || header bytes), 32 or 48 bits:
        bits 0-1   SUITE    0 = AES-256-GCM-SIV, 128-bit tag (1-3 reserved)
        bits 2-3   EPOCH    0 current day key, 1 previous, 2 next
        bit  4     HTYPE    0 short (32-bit), 1 long (48-bit) header
        bits 5-7   GEN_LSB  low 3 bits of the sender's boot generation
        bits 8-15  SENDER   net-local sender ID
        bits 16-31 SEQ      slot LSB (6) || per-slot counter (10)
        long only: bits 32-44 GEN_HI (generation >> 3), bits 45-47 KID
      nonce (96) = sender16 || generation16 || slot32 || counter32
      (dpmg.secure.nonce: persisted boot generation + time slot + counter).

  CIPHERTEXT  AES-256-GCM-SIV(K_aead(day), nonce, plaintext, AAD)  (RFC 8452)
  TAG         the full 128-bit GCM-SIV tag (no truncated-tag suite exists)
  CRC         over bytes(LEN || HDR || CT || TAG): noise pre-check only

AES-GCM-SIV is nonce-misuse resistant: a repeated (key, nonce) only reveals
that the same message was sent twice; it does not leak the authentication key
the way a GCM nonce repeat does. The sender still never repeats a nonce (the
counter/slot state is persisted across reboots).

Order on TX: encrypt then CRC then FEC. On RX: FEC then CRC then rebuild nonce then AEAD
open then replay bookkeeping then deliver. No plaintext leaves open() unless the
tag verified. Tag comparison is inside the library; no Python-side comparison
of tags or keys exists here. Encryption and decryption both go through
pyca/cryptography (AESGCMSIV).

Where this runs: on the ground station only (GUI, bench, tests). The radio
service parses and forwards sealed bodies and never holds keys; parsing
(read_prefix, capacity) needs no crypto library.
"""
from __future__ import annotations

import datetime as _dt
import logging
import threading
from dataclasses import dataclass

from . import crypto_env as _ce
from . import keys as _keys
from . import nonce as _nonce
from . import replay as _replay

log = logging.getLogger("dpmg.secure")

SUITE_GCMSIV128 = 0
SUITE_CODE = {"gcmsiv128": SUITE_GCMSIV128}
SUITE_NAME = {v: k for k, v in SUITE_CODE.items()}
TAG_BITS = {SUITE_GCMSIV128: 128}
EPOCH_CUR, EPOCH_PREV, EPOCH_NEXT = 0, 1, 2
EPOCH_OFFSET = {EPOCH_CUR: 0, EPOCH_PREV: -1, EPOCH_NEXT: +1}
HDR_SHORT_BITS, HDR_LONG_BITS = 32, 48
LEN_BITS, CRC_BITS = 8, 16
SLOT_LSB_BITS = 6
MAX_PLAINTEXT = 255
NONCE_PROFILE = "slot"                  # counter + time slot, persisted (the only one)

# verdicts (the GUI column); AUTH_OK is the only one that releases plaintext
AUTH_OK = "AUTH_OK"
AUTH_FAIL = "AUTH_FAIL"
REPLAY = "REPLAY"
REPLAY_GEN = "REPLAY_GEN"
STALE = "STALE"
GEN_UNKNOWN = "GEN_UNKNOWN"
NO_GEN_STATE = "NO_GEN_STATE"
UNKNOWN_KEY = "UNKNOWN_KEY"
SUITE_MISMATCH = "SUITE_MISMATCH"
BAD_HEADER = "BAD_HEADER"
NO_KEY = "NO_KEY"
DISABLED = "DISABLED"
VERDICTS = (AUTH_OK, AUTH_FAIL, REPLAY, REPLAY_GEN, STALE, GEN_UNKNOWN, NO_GEN_STATE,
            UNKNOWN_KEY, SUITE_MISMATCH, BAD_HEADER, NO_KEY, DISABLED)


class SecureError(RuntimeError):
    """seal() refused (no key, policy, nonce exhaustion, oversize)."""


def header_bits(suite: int, long_header: bool = False) -> int:
    return HDR_LONG_BITS if long_header else HDR_SHORT_BITS


def body_bits(length: int, suite: int, long_header: bool) -> int:
    """Total body bits LEN..CRC for a frame of `length` ciphertext bytes."""
    return LEN_BITS + header_bits(suite, long_header) + 8 * int(length) + TAG_BITS[suite] + CRC_BITS


def overhead_bits(suite: str | int = "gcmsiv128", long_header: bool = False) -> int:
    """Added info bits vs the legacy frame (header + tag)."""
    s = SUITE_CODE[suite] if isinstance(suite, str) else int(suite)
    return header_bits(s, long_header) + TAG_BITS[s]


def read_prefix(bits: str) -> tuple | None:
    """From the first 13 body bits (LEN + SUITE + EPOCH [+ HTYPE]): (length,
    suite, long_header, total_body_bits) or None when the prefix is not a legal
    secure frame prefix. The FEC decoders use it to size the block."""
    if len(bits) < 13:
        return None
    try:
        length = int(bits[:8], 2)
        suite = int(bits[8:10], 2)
        epoch = int(bits[10:12], 2)
    except ValueError:
        return None
    if suite not in TAG_BITS or epoch not in EPOCH_OFFSET:
        return None
    long_header = bits[12] == "1"
    return length, suite, long_header, body_bits(length, suite, long_header)


@dataclass
class Header:
    suite: int
    epoch: int
    long: bool = False
    gen_lsb: int = 0
    sender: int = 0
    slot_lsb: int = 0
    counter: int = 0
    gen_hi: int | None = None       # long header only (generation >> 3)
    kid: int | None = None          # long header only

    @property
    def generation(self) -> int | None:
        return None if self.gen_hi is None else ((self.gen_hi << 3) | self.gen_lsb)

    def pack(self) -> bytes:
        w = ((self.suite & 3) << 30) | ((self.epoch & 3) << 28) | ((1 if self.long else 0) << 27) \
            | ((self.gen_lsb & 7) << 24) | ((self.sender & 0xFF) << 16) \
            | ((self.slot_lsb & 0x3F) << 10) | (self.counter & 0x3FF)
        out = w.to_bytes(4, "big")
        if self.long:
            ext = (((self.gen_hi or 0) & 0x1FFF) << 3) | ((self.kid or 0) & 7)
            out += ext.to_bytes(2, "big")
        return out

    @classmethod
    def unpack(cls, b: bytes) -> "Header":
        if len(b) < 4:
            raise ValueError("header too short")
        w = int.from_bytes(b[:4], "big")
        h = cls(suite=(w >> 30) & 3, epoch=(w >> 28) & 3, long=bool((w >> 27) & 1),
                gen_lsb=(w >> 24) & 7, sender=(w >> 16) & 0xFF, slot_lsb=(w >> 10) & 0x3F,
                counter=w & 0x3FF)
        if h.long:
            if len(b) < 6:
                raise ValueError("long header too short")
            ext = int.from_bytes(b[4:6], "big")
            h.gen_hi, h.kid = (ext >> 3) & 0x1FFF, ext & 7
        return h

    @property
    def nbytes(self) -> int:
        return header_bits(self.suite, self.long) // 8


@dataclass
class OpenResult:
    verdict: str
    plaintext: bytes | None = None          # only with AUTH_OK
    header: Header | None = None
    key_id: str = ""
    sender16: int | None = None
    slot: int | None = None
    counter: int | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.verdict == AUTH_OK

    def as_dict(self) -> dict:
        h = self.header
        return {"verdict": self.verdict, "key_id": self.key_id, "sender16": self.sender16,
                "slot": self.slot, "counter": self.counter, "detail": self.detail,
                "header": None if h is None else {
                    "suite": SUITE_NAME.get(h.suite, h.suite), "epoch": h.epoch, "long": h.long,
                    "gen_lsb": h.gen_lsb, "sender": h.sender, "slot_lsb": h.slot_lsb,
                    "counter": h.counter, "generation": h.generation, "kid": h.kid}}


@dataclass
class Policy:
    require_trusted_time: bool = False      # on for TRANSEC on air
    freshness_slots: int = 16               # |frame slot - local slot| accepted
    long_header_first: int = 8              # frames after boot with a long header
    long_header_every: int = 64
    direction: int = 0                      # KDF direction byte (0 = net broadcast key)
    auth_fail_alarm: int = 64               # per key day: alarm above this many tag failures


def _aead_encrypt(key: bytes, nonce: bytes, pt: bytes, aad: bytes) -> tuple:
    """AES-256-GCM-SIV (RFC 8452) through pyca: (ciphertext, 128-bit tag)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCMSIV
    out = AESGCMSIV(key).encrypt(nonce, pt, aad)
    return out[:-16], out[-16:]


def _aead_decrypt(key: bytes, nonce: bytes, ct: bytes, tag: bytes, aad: bytes, suite: int) -> bytes:
    """Raises cryptography.exceptions.InvalidTag on any mismatch. The compare
    happens inside OpenSSL (constant time); a tag of any length other than the
    suite's 128 bits is refused before the library is called."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCMSIV
    want_tag_bytes = TAG_BITS[suite] // 8
    if len(tag) != want_tag_bytes:
        raise InvalidTag()
    return AESGCMSIV(key).decrypt(nonce, ct + tag, aad)


class SecureContext:
    """Keys + nonce state + replay table + counters for one ground process.

    seal(plaintext): body bytes (LEN || HDR || CT || TAG)   [TX]
    open(body): OpenResult                              [RX]
    """

    def __init__(self, store: _keys.KeyStore, time_source: _nonce.TimeSource | None = None,
                 policy: Policy | None = None, state_path: str | None = None, tx: bool = True,
                 env: _ce.CryptoEnv | None = None):
        self.store = store
        self.time = time_source or _nonce.TimeSource()
        self.policy = policy or Policy()
        self.env = env or _ce.check_policy(_ce.probe())
        self.replay = _replay.ReplayTable()
        self.lock = threading.RLock()
        self._nonce = None
        self._state_path = state_path or _nonce.default_state_path(store.path or "dpmg-keys.json")
        self._tx_enabled = tx
        self.counters = {v: 0 for v in VERDICTS}
        self.counters.update(sealed=0, seal_refused=0, crc_pass_total=0)
        self.alarms: list = []
        self.closed = False
        self.profile = NONCE_PROFILE

    def on_air_suite(self) -> int:
        return SUITE_CODE[self.store.active.suite]

    # TX
    def _nonce_source(self) -> _nonce.NonceSource:
        if self._nonce is None:
            if not self._tx_enabled:
                raise SecureError("this context is receive-only")
            self._nonce = _nonce.NonceSource(self.store.sender16(), self._state_path, self.store.sha256,
                                             self.store.active.slot_s, self.time)
        return self._nonce

    def seal(self, plaintext: bytes, long_header: bool | None = None) -> bytes:
        """Encrypt-then-header: returns LEN || HDR || CT || TAG (the CRC and FEC
        are frame_codec's). Raises SecureError instead of ever reusing a nonce.
        There is no way to pass a nonce in: the persisted generation/slot/counter
        state decides it."""
        with self.lock:
            if self.closed:
                raise SecureError("context closed (keys zeroised)")
            if not _ce.secure_msg_enabled():
                raise SecureError(f"{_ce.FLAG_SECURE_MSG} is off")
            if not self._tx_enabled:
                raise SecureError("this context is receive-only")
            pt = bytes(plaintext)
            if len(pt) > MAX_PLAINTEXT:
                raise SecureError(f"plaintext {len(pt)} B exceeds {MAX_PLAINTEXT} B")
            ks = self.store.active
            t, _trust = self.time.now()
            day = _nonce.utc_day(t)
            if not ks.valid_on(day):
                self.counters["seal_refused"] += 1
                raise SecureError(f"key set {ks.kid} is not valid on {day} ({ks.valid_from}..{ks.valid_to})")
            return self._seal(pt, ks, long_header)

    def _seal(self, pt: bytes, ks: _keys.KeySet, long_header: bool | None) -> bytes:
        try:
            ns = self._nonce_source()
            nonce, gen, slot, ctr, day = ns.next(require_trusted=self.policy.require_trusted_time)
        except _nonce.NonceError as e:
            self.counters["seal_refused"] += 1
            raise SecureError(str(e)) from e
        if long_header is None:
            long_header = ns.frames <= self.policy.long_header_first or \
                (self.policy.long_header_every and ns.frames % self.policy.long_header_every == 0)
        hdr = Header(suite=SUITE_CODE[ks.suite], epoch=EPOCH_CUR, long=bool(long_header),
                     gen_lsb=gen & 7, sender=self.store.sender_id, slot_lsb=slot & 0x3F,
                     counter=ctr, gen_hi=(gen >> 3) if long_header else None,
                     kid=ks.kid if long_header else None)
        hb = hdr.pack()
        aad = bytes([len(pt)]) + hb
        key = bytes(ks.aead_key(day, self.store.net_id, self.policy.direction))
        ct, tag = _aead_encrypt(key, nonce, pt, aad)
        self.counters["sealed"] += 1
        return bytes([len(pt)]) + hb + ct + tag

    # RX
    def open(self, body: bytes, now: float | None = None) -> OpenResult:
        """body = LEN || HDR || CT || TAG (CRC already checked by the caller).
        Never raises on malformed input; never returns plaintext unless AUTH_OK."""
        with self.lock:
            try:
                return self._open(bytes(body), now)
            except Exception as e:                                 # noqa: BLE001
                self.counters[BAD_HEADER] += 1
                return OpenResult(BAD_HEADER, detail=f"open() internal: {type(e).__name__}")

    def _verdict(self, v: str, **kw) -> OpenResult:
        self.counters[v] += 1
        return OpenResult(v, **kw)

    def _open(self, body: bytes, now: float | None) -> OpenResult:
        if self.closed:
            return self._verdict(NO_KEY, detail="context closed")
        if not _ce.secure_msg_enabled():
            return self._verdict(DISABLED, detail=f"{_ce.FLAG_SECURE_MSG} is off")
        if len(body) < 1 + HDR_SHORT_BITS // 8:
            return self._verdict(BAD_HEADER, detail="body shorter than LEN+header")
        length = body[0]
        try:
            hdr = Header.unpack(body[1:1 + HDR_LONG_BITS // 8])
        except ValueError as e:
            return self._verdict(BAD_HEADER, detail=str(e))
        if hdr.suite not in TAG_BITS or hdr.epoch not in EPOCH_OFFSET:
            return self._verdict(BAD_HEADER, header=hdr, detail="reserved suite/epoch code")
        tag_len = TAG_BITS[hdr.suite] // 8
        need = 1 + hdr.nbytes + length + tag_len
        if len(body) != need:
            return self._verdict(BAD_HEADER, header=hdr, detail=f"body {len(body)} B, header says {need} B")
        self.counters["crc_pass_total"] += 1
        # key set: long headers name it; short ones use the active set
        ks = self.store.for_kid(hdr.kid) if hdr.kid is not None else self.store.active
        if ks is None:
            return self._verdict(UNKNOWN_KEY, header=hdr, detail=f"kid {hdr.kid} not loaded")
        if SUITE_CODE[ks.suite] != hdr.suite:
            return self._verdict(SUITE_MISMATCH, header=hdr,
                                 detail=f"frame suite {SUITE_NAME[hdr.suite]} vs key set {ks.suite}")
        t = self.time.now()[0] if now is None else float(now)
        local_day = _nonce.utc_day(t)
        day = local_day + _dt.timedelta(days=EPOCH_OFFSET[hdr.epoch])
        if not ks.valid_on(day):
            return self._verdict(UNKNOWN_KEY, header=hdr, detail=f"key set {ks.kid} not valid on {day}")
        key = bytes(ks.aead_key(day, self.store.net_id, self.policy.direction))
        aad = body[:1 + hdr.nbytes]
        ct = body[1 + hdr.nbytes:1 + hdr.nbytes + length]
        tag = body[1 + hdr.nbytes + length:]
        sender16 = ((self.store.net_id & 0xFF) << 8) | hdr.sender
        day_iso = day.isoformat()
        return self._open_slot(hdr, ks, key, aad, ct, tag, sender16, t, day, day_iso)

    def _open_slot(self, hdr, ks, key, aad, ct, tag, sender16, t, day, day_iso) -> OpenResult:
        from cryptography.exceptions import InvalidTag
        local_slot = _nonce.slot_of(t, day, ks.slot_s)
        slot = local_slot + ((hdr.slot_lsb - (local_slot & 0x3F) + 32) % 64) - 32
        if abs(slot - local_slot) > self.policy.freshness_slots:
            return self._verdict(STALE, header=hdr, slot=slot, detail=f"slot {slot} vs local {local_slot}")
        st = self.replay.sender(sender16)
        if hdr.long:
            gen = hdr.generation
            if st.generation is not None and gen < st.generation:
                return self._verdict(REPLAY_GEN, header=hdr, sender16=sender16, slot=slot,
                                     detail=f"generation {gen} < tracked {st.generation}")
            candidates = [gen]
        else:
            if st.generation is None:
                return self._verdict(NO_GEN_STATE, header=hdr, sender16=sender16, slot=slot,
                                     detail="short header from an unknown sender; need a long header")
            candidates = [g for g in (st.generation, st.generation + 1) if (g & 7) == hdr.gen_lsb]
            if not candidates:
                return self._verdict(GEN_UNKNOWN, header=hdr, sender16=sender16, slot=slot,
                                     detail=f"GEN_LSB {hdr.gen_lsb} matches neither {st.generation} "
                                            f"nor {st.generation + 1}")
        ordv = _replay.ordinal(slot, hdr.counter)
        last_detail = ""
        for gen in candidates:
            win = st.peek(gen, day_iso)
            if win is not None and not win.check(ordv):
                win.rejected += 1
                return self._verdict(REPLAY, header=hdr, sender16=sender16, slot=slot, counter=hdr.counter,
                                     key_id=ks.key_id(day), detail=f"ordinal {ordv} already seen / behind window")
            nonce = _nonce.build_iv(sender16, gen, slot, hdr.counter)
            try:
                pt = _aead_decrypt(key, nonce, ct, tag, aad, hdr.suite)
            except InvalidTag:
                last_detail = f"tag mismatch (generation {gen})"
                continue
            st.generation = gen
            if hdr.long:
                st.long_headers += 1
            st.window(gen, day_iso).accept(ordv)
            self.counters[AUTH_OK] += 1
            return OpenResult(AUTH_OK, plaintext=pt, header=hdr, key_id=ks.key_id(day), sender16=sender16,
                              slot=slot, counter=hdr.counter)
        return self._auth_fail(hdr, sender16, ks.key_id(day), last_detail, slot=slot, counter=hdr.counter)

    def _auth_fail(self, hdr, sender16, key_id, detail, slot=None, counter=None) -> OpenResult:
        r = self._verdict(AUTH_FAIL, header=hdr, sender16=sender16, slot=slot, counter=counter,
                          key_id=key_id, detail=detail)
        if self.counters[AUTH_FAIL] == self.policy.auth_fail_alarm:
            msg = f"{self.counters[AUTH_FAIL]} authentication failures (CRC passed): possible injection"
            self.alarms.append(msg)
            log.warning("secure-msg: %s", msg)
        return r

    # misc
    def capacity(self, fec: dict | None, long_header: bool = False) -> int | None:
        """Largest plaintext (bytes) one frame of this coding carries with the
        suite this context puts on air, or None when the coding cannot carry a tag."""
        return capacity(fec, self.on_air_suite(), long_header)

    def status(self) -> dict:
        day = _nonce.utc_day(self.time.now()[0])
        d = {"enabled": _ce.secure_msg_enabled(), "closed": self.closed,
             "profile": self.profile, "aead": _ce.AEAD_NAME,
             "on_air_suite": SUITE_NAME[self.on_air_suite()],
             "overhead_bits": overhead_bits(self.on_air_suite()),
             "overhead_bits_long": overhead_bits(self.on_air_suite(), long_header=True),
             "keys": self.store.status(day) if not self.closed else {"closed": True},
             "policy": self.policy.__dict__, "counters": dict(self.counters),
             "alarms": list(self.alarms[-5:]), "replay": self.replay.status(),
             "time_trust": self.time.now()[1], "crypto": self.env.summary()}
        if self._nonce is not None:
            d["nonce"] = self._nonce.status()
        return d

    def close(self) -> None:
        with self.lock:
            self.store.close()
            self.closed = True


def capacity(fec: dict | None, suite: str | int = "gcmsiv128", long_header: bool = False) -> int | None:
    """Max plaintext bytes per frame for a frame_codec fec dict (None = uncoded)."""
    from dpmg.framing import frame_codec as fc
    kind = fc.fec_kind(fec) if fec else "conv"
    max_info = fc.fec_max_info_bits(fec) if fec else (8 + 255 * 8 + 16)
    ovh = overhead_bits(suite, long_header)
    if kind == "conv" or not fec:
        return MAX_PLAINTEXT                   # no block limit: LEN (8 bits) is the cap
    cap = (max_info - LEN_BITS - CRC_BITS - ovh) // 8
    return cap if cap >= 1 else None


def unsupported_codings(suite: str | int = "gcmsiv128", long_header: bool = False) -> list:
    """The codings that cannot carry a tag even with an empty payload (listed,
    never silently truncated)."""
    out = []
    for label, fec in (("tpc eH(16,11)^2", {"type": "tpc", "code": "eH(16,11)^2"}),
                       ("tpc eH(32,26)^2", {"type": "tpc", "code": "eH(32,26)^2"}),
                       ("ldpc qc-r12", {"type": "ldpc", "code": "qc-r12"}),
                       ("ldpc qc-r23", {"type": "ldpc", "code": "qc-r23"}),
                       ("ldpc qc-r34", {"type": "ldpc", "code": "qc-r34"}),
                       ("rs", {"type": "rs"})):
        if capacity(fec, suite, long_header) is None:
            out.append(label)
    return out


def capacity_table(suite: str | int = "gcmsiv128", long_header: bool = False) -> dict:
    """Max plaintext per frame for each coding. long_header=True is the ceiling
    frame_codec.encode_secure enforces (a frame may need the long header)."""
    return {label: capacity(fec, suite, long_header) for label, fec in (
        ("uncoded/conv", None), ("rs", {"type": "rs"}), ("ldpc qc-r12", {"type": "ldpc", "code": "qc-r12"}),
        ("ldpc qc-r23", {"type": "ldpc", "code": "qc-r23"}), ("ldpc qc-r34", {"type": "ldpc", "code": "qc-r34"}),
        ("tpc eH(32,26)^2", {"type": "tpc", "code": "eH(32,26)^2"}),
        ("tpc eH(16,11)^2", {"type": "tpc", "code": "eH(16,11)^2"}))}


# process context
_CTX: SecureContext | None = None
_CTX_ERR: str = ""
_CTX_LOCK = threading.Lock()


def get_context(create: bool = True, tx: bool = True) -> SecureContext | None:
    """The process-wide (ground-station) context, built from DPMG_KEY_FILE on
    first use. None (with last_error() set) when the flag is off, the key file
    is missing or the policy refuses. Never raises."""
    global _CTX, _CTX_ERR
    with _CTX_LOCK:
        if _CTX is not None and not _CTX.closed:
            return _CTX
        if not create:
            return None
        if not _ce.secure_msg_enabled():
            _CTX_ERR = f"{_ce.FLAG_SECURE_MSG} is off"
            return None
        try:
            env = _ce.check_policy(_ce.log_startup())
            store = _keys.load_key_file()
            _CTX = SecureContext(store, env=env, tx=tx)
            _CTX_ERR = ""
            log.info("secure-msg: keys loaded from %s (%s), %s, nonce %s", store.path,
                     store.status()["key_id"], _ce.AEAD_NAME, _ce.NONCE_NAME)
            return _CTX
        except (_ce.PolicyError, _keys.KeyFileError, OSError) as e:
            _CTX_ERR = str(e)
            log.error("secure-msg: not enabled: %s", e)
            return None


def last_error() -> str:
    return _CTX_ERR


def set_context(ctx: SecureContext | None) -> None:
    """Tests / simulators: install a prepared context (or clear it)."""
    global _CTX, _CTX_ERR
    with _CTX_LOCK:
        if _CTX is not None and ctx is not _CTX:
            try:
                _CTX.close()
            except Exception:                                      # noqa: BLE001
                pass
        _CTX = ctx
        _CTX_ERR = ""


def rx_stats() -> dict:
    ctx = _CTX
    if ctx is None:
        return {v: 0 for v in VERDICTS}
    return dict(ctx.counters)


def _msg_of(pt: bytes) -> str:
    try:
        m = pt.decode("utf-8")
        return m if m.isprintable() else pt.hex()
    except Exception:                                              # noqa: BLE001
        return pt.hex()


def open_result(res, ctx: SecureContext | None = None):
    """Open the sealed bodies a decode_pool.DecodeResult carries (field
    `sealed`: CRC-valid LEN||HDR||CT||TAG bodies the decoder found). Runs on
    the ground (bench, tests, the GUI): the radio never opens. Rewrites
    res.messages / res.message / res.valid to the authenticated plaintexts and
    records the verdicts in res.secure. Returns res."""
    sealed = list(getattr(res, "sealed", None) or [])
    if not sealed:
        return res
    ctx = ctx or get_context(create=True, tx=False)
    verdicts, msgs = [], []
    for body in sealed:
        if ctx is None:
            verdicts.append({"verdict": NO_KEY, "detail": last_error()})
            continue
        r = ctx.open(bytes(body))
        verdicts.append(r.as_dict())
        if r.ok:
            msgs.append(_msg_of(r.plaintext))
    res.messages = msgs
    res.message = msgs[0] if msgs else ""
    res.valid = bool(msgs)
    res.secure = {"verdicts": verdicts,
                  "auth_ok": sum(1 for v in verdicts if v["verdict"] == AUTH_OK),
                  "failed": sum(1 for v in verdicts if v["verdict"] != AUTH_OK)}
    return res


def inspect_bits(bits: str, ctx: SecureContext | None = None, fec: dict | None = None) -> list:
    """IQ Validator / Encoder inspection: every CRC-valid sealed frame in a bit
    string: header fields + verdict (opened with ctx or the process context
    when one is loaded; otherwise verdict NO_KEY and no plaintext). Never raises."""
    from dpmg.framing import frame_codec as fc
    out = []
    try:
        if fec:
            frames = fc.fec_find_all_frames(bits or "", fec, layout=fc.SECURE)
        else:
            frames = fc.secure_find_all_frames(bits or "")
    except Exception as e:                                         # noqa: BLE001
        return [{"error": f"search failed: {type(e).__name__}"}]
    ctx = ctx or get_context(create=True, tx=False)
    for fr in frames:
        if not fr.sealed:
            continue
        d = {"offset": fr.offset, "body_bytes": len(fr.sealed), "ciphertext_bytes": fr.sealed[0]}
        try:
            h = Header.unpack(fr.sealed[1:1 + HDR_LONG_BITS // 8])
            d["header"] = {"suite": SUITE_NAME.get(h.suite, h.suite), "epoch": h.epoch, "long": h.long,
                           "gen_lsb": h.gen_lsb, "sender": h.sender, "slot_lsb": h.slot_lsb,
                           "counter": h.counter, "generation": h.generation, "kid": h.kid}
        except ValueError as e:
            d["header"] = {"error": str(e)}
        if ctx is None:
            d["verdict"], d["detail"] = NO_KEY, last_error() or "no key loaded on this host"
        else:
            r = ctx.open(fr.sealed)
            d["verdict"], d["detail"], d["key_id"] = r.verdict, r.detail, r.key_id
            if r.ok:
                d["message"] = _msg_of(r.plaintext)
        out.append(d)
    return out
