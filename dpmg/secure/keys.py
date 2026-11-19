"""
dpmg.secure.keys: key file, key hierarchy and daily key derivation.

Key hierarchy:

  key file (outside the repo, mode 0600, owner = service user)
    net_id (8 bit)            the net this radio belongs to
    sender_id (8 bit)         this radio's net-local short ID (header SENDER)
    key_sets[]                one entry per pre-placed master pair
      kid (0-7)               key-set ID (3 bits in a long header)
      suite                   "gcmsiv128" (AES-256-GCM-SIV, 128-bit tag): fixed
                              for the key set's lifetime (never negotiated on
                              air). Key files written before the GCM-SIV switch
                              say "gcm128": read as "gcmsiv128" (same masters,
                              new AEAD label below, so no day key is shared
                              with the old GCM suite). "gcm96" is refused:
                              GCM-SIV has no truncated-tag form.
      comsec_master (32 B)    AES-256 master for message confidentiality/integrity
      transec_master (32 B)   separate master for TRANSEC (sync word, hop, timing)
      valid_from / valid_to   UTC dates (inclusive)
      slot_s                  nonce slot length in seconds (default 1.0)

  daily keys (per key set, per UTC date D, via HKDF-SHA-384, RFC 5869):
    K_aead(D)  = HKDF(ikm=comsec_master,  salt="DPMG/v1/"||net_id, info="DPMG/AEAD-GCMSIV|"||D||"|"||dir, 32 B)
    K_sync(D)  = HKDF(ikm=transec_master, salt="DPMG/v1/"||net_id, info="DPMG/SYNC|"||D,          32 B)
    K_hop(D), K_time(D), K_hp(D) likewise with labels DPMG/HOP, DPMG/TIME, DPMG/HP.

  key ID as shown in the GUI / logs: "<kid>/<YYYYMMDD>" plus a 4-byte key check
  value KCV = HMAC-SHA-384(K_aead, "DPMG/KCV")[:4]: never key bytes.

Rotation / rollover: the day key changes at 00:00 UTC. The header's 2-bit EPOCH
field says whether a frame used the current (0), previous (1) or next (2) day's
key, so both sides tolerate clock skew around midnight; a receiver accepts all
three for the active key set. Masters rotate by key fill (a new key set with a
new kid and a valid_from date); the old set stays loadable until valid_to so
in-flight traffic still opens, then it is removed from the file.

Zeroisation: masters and derived keys live in bytearrays and KeyStore.close()
overwrites them. CPython cannot scrub the immutable bytes copies the library
takes internally.

Nothing here implements a primitive: HKDF and HMAC come from pyca/cryptography.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac as _hmac
import json
import os
import stat
from dataclasses import dataclass, field

DEFAULT_KEY_FILE = os.path.join(os.path.expanduser("~"), ".config", "dpmg", "keys.json")
ENV_KEY_FILE = "DPMG_KEY_FILE"
SUITES = {"gcmsiv128": 16}                    # tag bytes (AES-256-GCM-SIV, RFC 8452)
SUITE_ALIASES = {"gcm128": "gcmsiv128"}       # key files written before the GCM-SIV switch
DEFAULT_SUITE = "gcmsiv128"
LABEL_AEAD, LABEL_SYNC, LABEL_HOP, LABEL_TIME, LABEL_HP = (
    b"DPMG/AEAD-GCMSIV", b"DPMG/SYNC", b"DPMG/HOP", b"DPMG/TIME", b"DPMG/HP")
SALT_PREFIX = b"DPMG/v1/"
MASTER_BYTES = 32


class KeyError_(Exception):
    """Key file missing, unreadable, badly protected or malformed."""


KeyFileError = KeyError_


def key_file_path() -> str:
    return os.environ.get(ENV_KEY_FILE) or DEFAULT_KEY_FILE


def check_permissions(path: str) -> None:
    """Refuse a key file that is not a regular file owned by us (or root) with
    no group/other permission bits."""
    try:
        st = os.stat(path)
    except OSError as e:
        raise KeyFileError(f"key file {path}: {e.strerror}") from e
    if not stat.S_ISREG(st.st_mode):
        raise KeyFileError(f"key file {path}: not a regular file")
    if st.st_mode & 0o077:
        raise KeyFileError(f"key file {path}: mode {oct(st.st_mode & 0o777)} is too open "
                           f"(need 0600 or 0400)")
    if hasattr(os, "geteuid") and st.st_uid not in (os.geteuid(), 0):
        raise KeyFileError(f"key file {path}: owned by uid {st.st_uid}, not us")


def _date(s) -> _dt.date:
    return _dt.date.fromisoformat(str(s))


def _hex32(v, what) -> bytearray:
    try:
        b = bytes.fromhex(str(v))
    except ValueError as e:
        raise KeyFileError(f"{what}: not hex") from e
    if len(b) != MASTER_BYTES:
        raise KeyFileError(f"{what}: need {MASTER_BYTES} bytes, got {len(b)}")
    return bytearray(b)


@dataclass
class KeySet:
    kid: int
    suite: str
    comsec_master: bytearray = field(repr=False)
    transec_master: bytearray = field(repr=False)
    valid_from: _dt.date
    valid_to: _dt.date
    slot_s: float = 1.0
    label: str = ""
    _cache: dict = field(default_factory=dict, repr=False)

    @property
    def tag_bytes(self) -> int:
        return SUITES[self.suite]

    def valid_on(self, day: _dt.date) -> bool:
        return self.valid_from <= day <= self.valid_to

    def derive(self, label: bytes, day: _dt.date, net_id: int, direction: int = 0,
               master: str = "comsec") -> bytearray:
        """HKDF-SHA-384 daily key (cached per label/day/direction)."""
        key = (label, day.isoformat(), direction, master)
        if key in self._cache:
            return self._cache[key]
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
        ikm = self.comsec_master if master == "comsec" else self.transec_master
        if not any(ikm):
            raise KeyFileError("key set is zeroised")
        info = label + b"|" + day.strftime("%Y%m%d").encode() + b"|" + bytes([direction & 0xFF])
        okm = HKDF(algorithm=hashes.SHA384(), length=MASTER_BYTES,
                   salt=SALT_PREFIX + bytes([net_id & 0xFF]), info=info).derive(bytes(ikm))
        out = bytearray(okm)
        self._cache[key] = out
        return out

    def aead_key(self, day: _dt.date, net_id: int, direction: int = 0) -> bytearray:
        return self.derive(LABEL_AEAD, day, net_id, direction, "comsec")

    def transec_key(self, label: bytes, day: _dt.date, net_id: int) -> bytearray:
        return self.derive(label, day, net_id, 0, "transec")

    def kcv(self, day: _dt.date, net_id: int, direction: int = 0) -> str:
        """4-byte key check value of the day's AEAD key (display only)."""
        k = self.aead_key(day, net_id, direction)
        return _hmac.new(bytes(k), b"DPMG/KCV", hashlib.sha384).digest()[:4].hex()

    def key_id(self, day: _dt.date) -> str:
        return f"{self.kid}/{day.strftime('%Y%m%d')}"

    def zeroise(self) -> None:
        for buf in (self.comsec_master, self.transec_master, *self._cache.values()):
            for i in range(len(buf)):
                buf[i] = 0
        self._cache.clear()


@dataclass
class KeyStore:
    net_id: int
    sender_id: int
    key_sets: dict                      # kid: KeySet
    active_kid: int
    path: str = ""
    sha256: str = ""                    # of the file (nonce state ties generations to it)
    closed: bool = False

    @property
    def active(self) -> KeySet:
        return self.key_sets[self.active_kid]

    def for_kid(self, kid: int) -> KeySet | None:
        return self.key_sets.get(int(kid))

    def sender16(self) -> int:
        """The 16-bit IV sender field: net_id || sender_id."""
        return ((self.net_id & 0xFF) << 8) | (self.sender_id & 0xFF)

    def status(self, day: _dt.date | None = None) -> dict:
        day = day or _dt.datetime.now(_dt.timezone.utc).date()
        ks = self.active
        return {"path": self.path, "net_id": self.net_id, "sender_id": self.sender_id,
                "active_kid": self.active_kid, "suite": ks.suite,
                "key_id": ks.key_id(day), "kcv": ks.kcv(day, self.net_id),
                "valid_from": ks.valid_from.isoformat(), "valid_to": ks.valid_to.isoformat(),
                "valid_today": ks.valid_on(day), "slot_s": ks.slot_s,
                "key_sets": sorted(self.key_sets), "closed": self.closed}

    def close(self) -> None:
        for ks in self.key_sets.values():
            ks.zeroise()
        self.closed = True


def parse_key_file(doc: dict, path: str = "") -> KeyStore:
    if not isinstance(doc, dict) or int(doc.get("version", 0)) != 1:
        raise KeyFileError("key file: unsupported or missing version (need 1)")
    try:
        net_id = int(doc["net_id"])
        sender_id = int(doc["sender_id"])
    except (KeyError, TypeError, ValueError) as e:
        raise KeyFileError("key file: net_id and sender_id (0-255) are required") from e
    if not (0 <= net_id <= 255 and 0 <= sender_id <= 255):
        raise KeyFileError("key file: net_id / sender_id must be 0-255")
    sets = {}
    for i, ks in enumerate(doc.get("key_sets") or []):
        try:
            kid = int(ks["kid"])
            suite = str(ks.get("suite", DEFAULT_SUITE)).lower()
            suite = SUITE_ALIASES.get(suite, suite)
            if suite not in SUITES:
                raise KeyFileError(f"key set {i}: suite must be one of {sorted(SUITES)} "
                                   f"(got {suite!r}; AES-256-GCM-SIV has only the 128-bit tag)")
            if not (0 <= kid <= 7):
                raise KeyFileError(f"key set {i}: kid must be 0-7")
            if kid in sets:
                raise KeyFileError(f"key set {i}: duplicate kid {kid}")
            cm = _hex32(ks["comsec_master"], f"key set {kid} comsec_master")
            tm = _hex32(ks["transec_master"], f"key set {kid} transec_master")
            if bytes(cm) == bytes(tm):
                raise KeyFileError(f"key set {kid}: comsec and transec masters must differ")
            sets[kid] = KeySet(kid=kid, suite=suite, comsec_master=cm, transec_master=tm,
                               valid_from=_date(ks["valid_from"]), valid_to=_date(ks["valid_to"]),
                               slot_s=float(ks.get("slot_s", 1.0)), label=str(ks.get("label", "")))
            if sets[kid].slot_s <= 0:
                raise KeyFileError(f"key set {kid}: slot_s must be > 0")
        except KeyFileError:
            raise
        except (KeyError, TypeError, ValueError) as e:
            raise KeyFileError(f"key set {i}: {e}") from e
    if not sets:
        raise KeyFileError("key file: no key_sets")
    active = int(doc.get("active_kid", min(sets)))
    if active not in sets:
        raise KeyFileError(f"key file: active_kid {active} not present")
    return KeyStore(net_id=net_id, sender_id=sender_id, key_sets=sets, active_kid=active, path=path)


def load_key_file(path: str | None = None) -> KeyStore:
    path = path or key_file_path()
    check_permissions(path)
    with open(path, "rb") as f:
        raw = f.read()
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise KeyFileError(f"key file {path}: not JSON ({e})") from e
    ks = parse_key_file(doc, path)
    ks.sha256 = hashlib.sha256(raw).hexdigest()
    return ks


def generate_key_file(net_id: int, sender_id: int, kid: int = 0, suite: str = DEFAULT_SUITE,
                      valid_days: int = 365, label: str = "", slot_s: float = 1.0,
                      today: _dt.date | None = None) -> dict:
    """A fresh key-file document with masters from the OS CSPRNG (os.urandom,
    the kernel RBG). Key fill: generate at the ground station, copy to every
    member of the net over an out-of-band channel (never over the UDP plane or
    the air), chmod 0600."""
    if suite not in SUITES:
        raise ValueError(f"suite must be one of {sorted(SUITES)}")
    today = today or _dt.datetime.now(_dt.timezone.utc).date()
    return {"version": 1, "net_id": int(net_id), "sender_id": int(sender_id), "active_kid": int(kid),
            "key_sets": [{"kid": int(kid), "label": label, "suite": suite,
                          "comsec_master": os.urandom(MASTER_BYTES).hex(),
                          "transec_master": os.urandom(MASTER_BYTES).hex(),
                          "valid_from": today.isoformat(),
                          "valid_to": (today + _dt.timedelta(days=int(valid_days) - 1)).isoformat(),
                          "slot_s": float(slot_s)}]}


def write_key_file(doc: dict, path: str) -> None:
    """Write with 0600 from the first byte (open with O_EXCL-less but mode-set
    create, then fsync)."""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        try:
            os.unlink(tmp)
        finally:
            raise
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
