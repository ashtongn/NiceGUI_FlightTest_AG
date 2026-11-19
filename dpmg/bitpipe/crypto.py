"""
What the bitpipe seals each frame payload with, and where the key comes from.

A crypto object has seal(record) then bytes and open(body) then (ok, record).
NullCrypto passes bytes through (a clear link). SecureContextCrypto wraps the
existing secure_frame SecureContext: AES-256-GCM-SIV, persisted nonce state,
replay table, the same code the single sealed messages use.

The key comes through one small interface, KeyProvider.key_store(), so the
custody scheme can change without touching the pipe: FileKeyProvider reads
the key file (DPMG_KEY_FILE, on the x86: the x86 side holds the keys),
StaticKeyProvider takes a key document already in memory
(loopback, tests), and DeviceKeyProvider is the slot for the device unlocked
key (a Jetson secure boot and fuse based scheme, an asymmetric key unlocking
a device key that unlocks the TRANSEC key); that scheme is not implemented and
the provider says so.

Because the ARQ dedups and reorders before open(), each sequence reaches the
replay table once and in order; a retransmission re-sends the original sealed
bytes and never spends a new nonce.
"""
from __future__ import annotations

import os
import tempfile
from typing import Optional, Tuple

SECURE_OVERHEAD_BYTES = 1 + 6 + 16          # LEN + long header + tag (worst case)


class NullCrypto:
    name = "none"

    def seal(self, record: bytes) -> bytes:
        return bytes(record)

    def open(self, body: bytes) -> Tuple[bool, bytes]:
        return True, bytes(body)

    def status(self) -> dict:
        return {"crypto": self.name}


class SecureContextCrypto:
    """seal/open through a secure_frame.SecureContext (tx=True so it can seal;
    opening works on the same context). verdicts counts what open() said."""
    name = "secure"

    def __init__(self, ctx):
        self.ctx = ctx
        self.verdicts: dict = {}
        self.last_error = ""

    def seal(self, record: bytes) -> bytes:
        return self.ctx.seal(bytes(record))

    def open(self, body: bytes) -> Tuple[bool, Optional[bytes]]:
        r = self.ctx.open(bytes(body))
        self.verdicts[r.verdict] = self.verdicts.get(r.verdict, 0) + 1
        if not r.ok:
            self.last_error = f"{r.verdict}: {r.detail}"
            return False, None
        return True, bytes(r.plaintext)

    def status(self) -> dict:
        try:
            st = self.ctx.status()
        except Exception:                                   # noqa: BLE001
            st = {}
        return {"crypto": self.name, "verdicts": dict(self.verdicts), "key_id": st.get("key_id"),
                "sender_id": getattr(self.ctx.store, "sender_id", None), "last_error": self.last_error,
                "key_provider": getattr(self.ctx, "_bitpipe_provider", "?")}


class KeyProvider:
    """Returns the KeyStore (parsed key file: masters, net and sender ids) the
    SecureContext derives its day keys from. One call, no caching here."""
    name = "none"

    def key_store(self, sender_id: Optional[int] = None):
        raise NotImplementedError

    def describe(self) -> str:
        return self.name


class FileKeyProvider(KeyProvider):
    """The operator key file (mode 0600, owner only), DPMG_KEY_FILE or the
    default path when `path` is None."""
    name = "file"

    def __init__(self, path: Optional[str] = None):
        self.path = path

    def key_store(self, sender_id: Optional[int] = None):
        from dpmg.secure import api as sf
        store = sf.load_key_file(self.path)
        if sender_id is not None:
            store.sender_id = int(sender_id)
        return store

    def describe(self) -> str:
        from dpmg.secure import api as sf
        return f"file:{self.path or sf.key_file_path()}"


class StaticKeyProvider(KeyProvider):
    """Keys passed in as a dict with the same fields as a key file (loopback and tests)."""
    name = "static"

    def __init__(self, key_doc: dict, label: str = "static"):
        self.doc = dict(key_doc)
        self.label = label

    def key_store(self, sender_id: Optional[int] = None):
        from dpmg.secure import api as sf
        doc = dict(self.doc)
        if sender_id is not None:
            doc["sender_id"] = int(sender_id)
        store = sf.parse_key_file(doc, path=f"<{self.label}>")
        store.sha256 = f"bitpipe-{self.label}-{doc.get('net_id', 0)}"
        return store


class DeviceKeyProvider(KeyProvider):
    """Placeholder for the device unlocked key: an asymmetric key unlocking a
    device key that in turn unlocks the TRANSEC symmetric key. The scheme is
    not designed yet; until it exists this provider refuses."""
    name = "device"

    def key_store(self, sender_id: Optional[int] = None):
        raise NotImplementedError("device unlocked key provider: the custody scheme is not designed yet; "
                                  "use the file provider on the x86")


def context_from_provider(provider: KeyProvider, sender_id: Optional[int] = None, freshness_slots: int = 300,
                          state_dir: Optional[str] = None, time_source=None):
    """A SecureContext for one end of a bitpipe. The nonce state file lives
    in state_dir (a fresh temp dir when None). Needs the DPMG_SECURE_MSG flag
    on in this process."""
    from dpmg.secure import api as sf
    store = provider.key_store(sender_id)
    d = state_dir or tempfile.mkdtemp(prefix="dpmg-bitpipe-")
    policy = sf.Policy(freshness_slots=int(freshness_slots))
    ctx = sf.SecureContext(store, time_source=time_source, policy=policy,
                           state_path=os.path.join(d, f"nonce-{store.sender_id}.json"), tx=True)
    ctx._bitpipe_state_dir = d
    ctx._bitpipe_provider = provider.describe()
    return ctx


def make_context(key_doc: dict, sender_id: int, freshness_slots: int = 300, state_dir: Optional[str] = None,
                 time_source=None):
    """A SecureContext from a key document in memory (StaticKeyProvider)."""
    return context_from_provider(StaticKeyProvider(key_doc), sender_id, freshness_slots, state_dir, time_source)


def loopback_crypto_pair(net_id: int = 1, freshness_slots: int = 300, time_source=None):
    """Two SecureContextCrypto objects sharing throw away masters (sender 1
    and sender 2), for a loopback on one host. Turns the DPMG_SECURE_MSG flag
    on for this process if it is not set, which is what the secure tests do."""
    os.environ.setdefault("DPMG_SECURE_MSG", "1")
    from dpmg.secure import api as sf
    doc = sf.generate_key_file(net_id, 1, label="bitpipe-loopback")
    a = make_context(doc, 1, freshness_slots, time_source=time_source)
    b = make_context(doc, 2, freshness_slots, time_source=time_source)
    return SecureContextCrypto(a), SecureContextCrypto(b)


def provider_from_config(cfg, key_doc: Optional[dict] = None) -> KeyProvider:
    if key_doc is not None or cfg.key_provider == "static":
        if key_doc is None:
            raise ValueError("key_provider static needs a key document")
        return StaticKeyProvider(key_doc)
    if cfg.key_provider == "device":
        return DeviceKeyProvider()
    return FileKeyProvider(cfg.key_file)


def crypto_from_config(cfg, key_doc: Optional[dict] = None, sender_id: Optional[int] = None,
                       state_dir: Optional[str] = None, provider: Optional[KeyProvider] = None):
    """The crypto object a config asks for: NullCrypto for "none", otherwise a
    SecureContext built through the key provider (file by default)."""
    if cfg.crypto == "none":
        return NullCrypto()
    provider = provider or provider_from_config(cfg, key_doc)
    return SecureContextCrypto(context_from_provider(provider, sender_id, cfg.freshness_slots, state_dir))
