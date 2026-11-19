"""
dpmg.secure.crypto_env: start-up check for the secure-messaging stack.

What it answers, once per process:
  * which crypto library is loaded (pyca/cryptography) and its version,
  * which OpenSSL (or other backend) it is linked against,
  * whether the installed version meets the pin in requirements-crypto.txt,
  * whether the AES-256-GCM-SIV (RFC 8452) / HKDF known-answer self-tests pass
    (AES-GCM-SIV needs OpenSSL >= 3.2 under pyca; an older backend fails the
    self-test and encryption stays off).

Flag:
  DPMG_SECURE_MSG=1      the feature flag for secure messages. Off by
                         default; nothing in the TX/RX paths changes while it
                         is off.

Nothing here implements a primitive. The self-test vectors are RFC published
values (tests/data/secure/ carries the full sets); the ones inlined below are
the start-up KAT subset.
"""
from __future__ import annotations

import logging
import os
import platform
from dataclasses import asdict, dataclass, field

log = logging.getLogger("dpmg.secure")

# the pin (kept in step with requirements-crypto.txt; the check below reads the
# file when it is present so the two cannot drift silently)
MIN_CRYPTOGRAPHY = (48, 0, 0)
REQUIREMENTS_FILE = "requirements-crypto.txt"
from dpmg.paths import REPO_ROOT  # noqa: E402

AEAD_NAME = "AES-256-GCM-SIV"                    # RFC 8452, 128-bit tag, 96-bit nonce
NONCE_NAME = "counter+slot"                      # dpmg.secure.nonce, persisted

FLAG_SECURE_MSG = "DPMG_SECURE_MSG"
FLAG_TRANSEC_SIM = "DPMG_TRANSEC_SIM"          # TRANSEC simulator-only hook
FLAG_PQ_TEST_APPROVED = "DPMG_PQ_TEST_APPROVED"  # PQ rekey gate (DoW CIO approval)


def flag_on(name: str, default: str = "0") -> bool:
    return str(os.environ.get(name, default)).strip().lower() in ("1", "true", "yes", "on")


def secure_msg_enabled() -> bool:
    """The secure-messages feature flag. Off: every secure path is inert."""
    return flag_on(FLAG_SECURE_MSG)


@dataclass
class CryptoEnv:
    library: str = "cryptography"
    version: str = ""
    version_tuple: tuple = ()
    backend: str = ""                 # e.g. "OpenSSL 4.0.3 29 Sep 2026"
    backend_number: int = 0
    aead: str = AEAD_NAME
    nonce: str = NONCE_NAME
    meets_pin: bool = False
    pin: str = ""
    python: str = platform.python_version()
    machine: str = platform.machine()
    selftest_ok: bool | None = None
    selftest_detail: str = ""
    errors: list = field(default_factory=list)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["version_tuple"] = list(self.version_tuple)
        return d

    def summary(self) -> str:
        pin = "meets pin" if self.meets_pin else f"BELOW pin {self.pin}"
        st = {True: "self-test OK", False: "SELF-TEST FAILED", None: "self-test not run"}[self.selftest_ok]
        return (f"{self.library} {self.version} ({pin}) on {self.backend or '?'}; {self.aead}; {st}; "
                f"nonce: {self.nonce} (persisted); python {self.python} {self.machine}")


def _read_pin() -> tuple:
    """The version floor from requirements-crypto.txt (repo root), else the constant."""
    path = os.path.join(str(REPO_ROOT), REQUIREMENTS_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                s = line.split("#", 1)[0].strip()
                if s.lower().startswith("cryptography"):
                    spec = s[len("cryptography"):].strip()
                    for op in (">=", "=="):
                        if spec.startswith(op):
                            v = spec[len(op):].split(",")[0].strip()
                            return tuple(int(x) for x in v.split(".")[:3])
    except OSError:
        pass
    return MIN_CRYPTOGRAPHY


def probe(run_selftest: bool = True) -> CryptoEnv:
    env = CryptoEnv()
    env.pin = ".".join(str(x) for x in _read_pin())
    try:
        import cryptography
        env.version = cryptography.__version__
        env.version_tuple = tuple(int(x) for x in env.version.split(".")[:3])
        env.meets_pin = env.version_tuple >= _read_pin()
    except Exception as e:                                        # noqa: BLE001
        env.errors.append(f"cryptography import failed: {e}")
        return env
    try:
        from cryptography.hazmat.backends.openssl.backend import backend
        env.backend = backend.openssl_version_text()
        try:
            env.backend_number = int(backend.openssl_version_number())
        except Exception:                                         # noqa: BLE001
            env.backend_number = 0
    except Exception as e:                                        # noqa: BLE001
        env.errors.append(f"backend probe failed: {e}")
    if run_selftest:
        ok, detail = selftest()
        env.selftest_ok, env.selftest_detail = ok, detail
        if not ok:
            env.errors.append(f"self-test: {detail}")
    return env


# self-tests
# AES-256-GCM-SIV: RFC 8452 Appendix C.2 (the vendored set in
# tests/data/secure/aes_gcm_siv_256_rfc8452_kat.json holds these and more).
_KAT_SIV = (
    {"key": "0100000000000000000000000000000000000000000000000000000000000000",
     "nonce": "030000000000000000000000", "aad": "01", "pt": "02000000000000000000000000000000",
     "out": "c91545823cc24f17dbb0e9e807d5ec17b292d28ff61189e8e49f3875ef91aff7"},
    {"key": "bae8e37fc83441b16034566b7a806c46bb91c3c5aedb64a6c590bc84d1a5e269",
     "nonce": "e4b47801afc0577e34699b9e", "aad": "4fbdc66f14", "pt": "671fdd",
     "out": "0eaccb93da9bb81333aee0c785b240d319719d"},
)
# HKDF-SHA-256, RFC 5869 A.1 (there is no RFC vector for SHA-384; the
# production KDF is checked against an independent hashlib construction in
# tests/test_secure_frame.py).
_KAT_HKDF = {
    "ikm": "0b" * 22, "salt": "000102030405060708090a0b0c", "info": "f0f1f2f3f4f5f6f7f8f9", "L": 42,
    "okm": ("3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf"
            "34007208d5b887185865"),
}


def selftest() -> tuple:
    """Known-answer tests through the library (never a DPMG re-implementation).
    Returns (ok, detail)."""
    try:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.ciphers.aead import AESGCMSIV
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
        for k in _KAT_SIV:
            a = AESGCMSIV(bytes.fromhex(k["key"]))
            out = a.encrypt(bytes.fromhex(k["nonce"]), bytes.fromhex(k["pt"]), bytes.fromhex(k["aad"]))
            if out != bytes.fromhex(k["out"]):
                return False, f"{AEAD_NAME} encrypt KAT mismatch"
            if a.decrypt(bytes.fromhex(k["nonce"]), out, bytes.fromhex(k["aad"])) != bytes.fromhex(k["pt"]):
                return False, f"{AEAD_NAME} decrypt KAT mismatch"
            # the tag check must reject a flipped bit
            bad = bytearray(out)
            bad[-1] ^= 0x01
            try:
                a.decrypt(bytes.fromhex(k["nonce"]), bytes(bad), bytes.fromhex(k["aad"]))
                return False, f"{AEAD_NAME} accepted a corrupted tag"
            except InvalidTag:
                pass
        h = _KAT_HKDF
        okm = HKDF(algorithm=hashes.SHA256(), length=h["L"], salt=bytes.fromhex(h["salt"]),
                   info=bytes.fromhex(h["info"])).derive(bytes.fromhex(h["ikm"]))
        if okm != bytes.fromhex(h["okm"]):
            return False, "HKDF-SHA-256 RFC 5869 A.1 mismatch"
        # SHA-384 must be available (the hash of the production KDF)
        HKDF(algorithm=hashes.SHA384(), length=32, salt=b"x", info=b"y").derive(b"z")
        return True, f"{AEAD_NAME} (RFC 8452) + HKDF KATs pass"
    except Exception as e:                                        # noqa: BLE001
        return False, f"self-test raised {type(e).__name__}: {e}"


class PolicyError(RuntimeError):
    """Encryption may not be enabled in the current environment."""


def check_policy(env: CryptoEnv | None = None) -> CryptoEnv:
    """Refuse to enable encryption when the library is missing or the
    known-answer self-test failed (e.g. a backend without AES-GCM-SIV).
    Returns the env."""
    env = env or probe()
    if env.errors and env.selftest_ok is False:
        raise PolicyError("crypto self-test failed: " + env.selftest_detail)
    if env.errors and not env.version:
        raise PolicyError("; ".join(env.errors))
    return env


_LOGGED = False


def log_startup(logger=None, env: CryptoEnv | None = None) -> CryptoEnv:
    """Log the library / OpenSSL / self-test line once per process."""
    global _LOGGED
    env = env or probe()
    lg = logger or log
    if not _LOGGED:
        lg.info("secure-msg crypto: %s", env.summary())
        if not env.meets_pin:
            lg.warning("cryptography %s is below the %s pin %s (functional, not the pinned build)",
                       env.version, REQUIREMENTS_FILE, env.pin)
        for e in env.errors:
            lg.error("secure-msg crypto: %s", e)
        _LOGGED = True
    return env


if __name__ == "__main__":
    e = probe()
    print(e.summary())
    for k, v in e.as_dict().items():
        print(f"  {k}: {v}")
