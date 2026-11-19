"""
dpmg.secure.pq_rekey: post-quantum rekey: design + interface stubs.

Nothing cryptographic is implemented here:
  * the primitives (ML-KEM-1024, FIPS 203; ML-DSA-87, FIPS 204) must come from a
    vetted library: pyca/cryptography >= 48.0.0 built on OpenSSL >= 3.5 (ML-KEM
    and ML-DSA since 48.0.0, 2026-05-04) or AWS-LC FIPS / liboqs for the bench;
    the x86 dev host and the G40 image run cryptography 42 on OpenSSL 3.0 today;
  * the Department of War CIO memorandum "Preparing for Migration to Post
    Quantum Cryptography" (18 Nov 2025) is reported to require CIO approval
    before PQC testing or deployment. DPMG_PQ_TEST_APPROVED=1 is the
    local record that this approval exists; every entry point refuses without it.

Design:

  RekeyMessage (ground to radio, once per key epoch or on demand, under the
  current secure-message AEAD key as well):
      version(1) | epoch(4) | valid_from(4) | valid_to(4) | recipient_id(2) |
      kem_ct(1568, ML-KEM-1024) | n_wrapped(1) | wrapped_teks(n x 40, AES-256-KWP) |
      sig(4627, ML-DSA-87 over everything before it)
  Combiner (RFC 8784 model; confirm against SP 800-56C r2 / SP 800-227 before use):
      K_master' = HKDF-SHA-384(salt = HKDF(K_psk, "DPMG/PQ-SALT"), IKM = ss_mlkem ||
                  epoch || recipient_id || SHA-384(kem_ct), info = "DPMG/PQ-REKEY")
      secure if either the pre-placed key or ML-KEM holds.
  Fragmentation: 100-byte fragments, Reed-Solomon erasure code across fragments
      (k data to n coded); the signature verifies the reassembled object, so the
      erasure code itself is non-cryptographic and DPMG may write it.
  State machine (radio): IDLE then COLLECTING(epoch) then VERIFYING then STAGED(valid_from)
      then ACTIVE then RETIRED(valid_to + grace); any verify failure returns to IDLE with an alarm;
      the old master is zeroised when the new one becomes ACTIVE and the grace
      period for in-flight frames (one freshness window) has passed.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field

from . import crypto_env as _ce

ML_KEM_1024_CT = 1568
ML_KEM_1024_PK = 1568
ML_DSA_87_SIG = 4627
ML_DSA_87_PK = 2592
KWP_256_WRAPPED = 40
FRAGMENT_PAYLOAD = 100
HEADER_BYTES = 1 + 4 + 4 + 4 + 2 + 1          # version..n_wrapped

STATES = ("IDLE", "COLLECTING", "VERIFYING", "STAGED", "ACTIVE", "RETIRED")
TRANSITIONS = {
    ("IDLE", "fragment"): "COLLECTING",
    ("COLLECTING", "fragment"): "COLLECTING",
    ("COLLECTING", "complete"): "VERIFYING",
    ("COLLECTING", "timeout"): "IDLE",
    ("VERIFYING", "sig_ok"): "STAGED",
    ("VERIFYING", "sig_fail"): "IDLE",
    ("STAGED", "valid_from"): "ACTIVE",
    ("ACTIVE", "valid_to"): "RETIRED",
    ("RETIRED", "grace_over"): "IDLE",
}


class PqGateError(RuntimeError):
    """PQ testing is gated: DPMG_PQ_TEST_APPROVED=1 records the DoW CIO approval."""


class PqNotAvailable(NotImplementedError):
    """Gate open, but no vetted ML-KEM / ML-DSA library is present (or the
    function is design-only in this build)."""


def gate_open() -> bool:
    return _ce.flag_on(_ce.FLAG_PQ_TEST_APPROVED)


def require_gate() -> None:
    if not gate_open():
        raise PqGateError(f"PQ rekey testing requires DoW CIO PQC approval: set {_ce.FLAG_PQ_TEST_APPROVED}=1 "
                          f"only once that approval is on record")


def library_prerequisite() -> dict:
    """What the running interpreter offers for the PQ rekey (no gate needed to ask)."""
    out = {"cryptography": None, "openssl": None, "ml_kem": False, "ml_dsa": False, "liboqs": False}
    try:
        import cryptography
        out["cryptography"] = cryptography.__version__
        from cryptography.hazmat.backends.openssl.backend import backend
        out["openssl"] = backend.openssl_version_text()
        try:
            from cryptography.hazmat.primitives.asymmetric import ml_kem  # noqa: F401
            out["ml_kem"] = True
        except ImportError:
            pass
        try:
            from cryptography.hazmat.primitives.asymmetric import ml_dsa  # noqa: F401
            out["ml_dsa"] = True
        except ImportError:
            pass
    except Exception:                                              # noqa: BLE001
        pass
    try:
        import oqs  # noqa: F401
        out["liboqs"] = True
    except ImportError:
        pass
    out["ready"] = bool(out["ml_kem"] and out["ml_dsa"]) or out["liboqs"]
    return out


@dataclass
class RekeyMessage:
    """Wire layout (sizes only; no content is produced in this build)."""
    version: int = 1
    epoch: int = 0
    valid_from: int = 0
    valid_to: int = 0
    recipient_id: int = 0
    kem_ct: bytes = b""
    wrapped_teks: list = field(default_factory=list)
    sig: bytes = b""

    @staticmethod
    def size(n_wrapped: int = 1) -> int:
        return HEADER_BYTES + ML_KEM_1024_CT + n_wrapped * KWP_256_WRAPPED + ML_DSA_87_SIG


def fragment_plan(n_bytes: int, fer: float, target_success: float = 0.99,
                  payload: int = FRAGMENT_PAYLOAD, max_extra: int = 64) -> dict:
    """Non-cryptographic sizing: k = ceil(n_bytes/payload)
    data fragments; the smallest n >= k such that P(at least k of n arrive) at
    frame error rate `fer` meets target_success. Airtime at 1.2 / 9.6 kbit/s with
    conv-1/2 secure-message fragments of ~1996 on-air bits."""
    k = math.ceil(n_bytes / payload)
    def p_ok(n):
        return sum(math.comb(n, i) * (1 - fer) ** i * fer ** (n - i) for i in range(k, n + 1))
    n = k
    while p_ok(n) < target_success and n < k + max_extra:
        n += 1
    return {"bytes": n_bytes, "k": k, "n": n, "p_success": p_ok(n), "p_success_uncoded": p_ok(k),
            "airtime_s_1200": round(n * 1996 / 1200.0, 1), "airtime_s_9600": round(n * 1996 / 9600.0, 1)}


class RekeyStateMachine:
    """The radio-side states; transitions are pure bookkeeping (testable without
    any crypto), the crypto steps are the gated stubs below."""

    def __init__(self):
        self.state = "IDLE"
        self.history = []

    def event(self, ev: str) -> str:
        nxt = TRANSITIONS.get((self.state, ev))
        if nxt is None:
            raise ValueError(f"no transition from {self.state} on {ev}")
        self.history.append((self.state, ev, nxt))
        self.state = nxt
        return nxt


# gated stubs
def combine_master(k_psk: bytes, ss_mlkem: bytes, epoch: int, recipient_id: int, kem_ct: bytes) -> bytes:
    """HKDF-SHA-384 combiner (RFC 8784 model). Gated; not implemented in this build."""
    require_gate()
    raise PqNotAvailable("combine_master: design only; needs the approved combiner form (SP 800-56C r2 / "
                         "SP 800-227) confirmed and ML-KEM shared secrets from a vetted library")


def encapsulate(recipient_pk: bytes) -> tuple:
    """ML-KEM-1024 encapsulation at the ground station. Gated; stub."""
    require_gate()
    raise PqNotAvailable("encapsulate: needs cryptography>=48 on OpenSSL>=3.5 (ml_kem) or liboqs; "
                         f"present: {library_prerequisite()}")


def decapsulate(radio_sk: bytes, kem_ct: bytes) -> bytes:
    require_gate()
    raise PqNotAvailable("decapsulate: needs an ML-KEM-1024 implementation from a vetted library")


def sign_rekey(ground_sk: bytes, message: bytes) -> bytes:
    """ML-DSA-87 signature over the rekey message (HSM-held key). Gated; stub."""
    require_gate()
    raise PqNotAvailable("sign_rekey: needs ML-DSA-87 (cryptography>=48 ml_dsa / HSM)")


def verify_rekey(ground_pk: bytes, message: bytes, sig: bytes) -> bool:
    require_gate()
    raise PqNotAvailable("verify_rekey: needs ML-DSA-87 from a vetted library")


def fragment(message: bytes, n: int) -> list:
    """RS erasure coding across fragments (non-cryptographic). Gated with the
    rest of the PQ rekey so no partial path can be exercised on a .mil system before
    the approval; dpmg/fec/rs.py would need an erasure-decoding entry point."""
    require_gate()
    raise PqNotAvailable("fragment: erasure coding across fragments not implemented (rs.py needs erasure decode)")


def reassemble(fragments: list, k: int, n: int) -> bytes:
    require_gate()
    raise PqNotAvailable("reassemble: not implemented")


STUBS = (combine_master, encapsulate, decapsulate, sign_rekey, verify_rekey, fragment, reassemble)
