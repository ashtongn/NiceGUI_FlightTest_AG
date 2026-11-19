"""
dpmg.secure: DPMG secure messaging and TRANSEC.

  crypto_env    library / OpenSSL check, KAT self-test (AES-256-GCM-SIV, HKDF)
  keys          key file, key hierarchy, HKDF-SHA-384 daily keys, zeroisation
  nonce         persisted boot generation + time-slot/counter nonce source (TX)
  replay        per-sender anti-replay windows (RX)
  secure_frame  AES-256-GCM-SIV (RFC 8452) seal/open of the frame body, header, verdicts
  transec_sync  TRANSEC: keyed 32-bit sync word per GPS slot (+ designed stubs)
  pq_rekey      ML-KEM-1024 / ML-DSA-87 rekey design, gated stubs

Everything is behind DPMG_SECURE_MSG (off by default); the primitives come from
pyca/cryptography (requirements-crypto.txt), never from this package.
"""
from . import crypto_env  # noqa: F401
