"""
dpmg.secure.transec_sync: TRANSEC: the keyed 32-bit sync word
(implemented, tested, simulator-only hook) and the designed stubs (header
masking, size buckets, keyed time offset, keyed channel hop) that refuse to run.

Keyed sync word:

    W(slot) = Trunc32( CMAC-AES-256( K_sync(day), "DPMG/SYNC" || net_id || day || slot || k ) )

  K_sync(day) is the TRANSEC master's daily HKDF key (keys.KeySet.transec_key),
  never the COMSEC key. k is a re-derivation counter: a candidate word with poor
  acquisition properties (unbalanced, long runs, high aperiodic autocorrelation
  sidelobe) or within 2t+1 bits of a neighbouring slot's word (either polarity)
  is deterministically re-derived with k+1: both ends run the same loop, so
  no bits are exchanged. Acceptance tolerance t = 3 (t = 4 for marginal links).

Time-trust rules:
    trusted    GPS locked (3D fix, fresh): candidates {slot-1, slot, slot+1}
    degraded   holdover / 2D / stale refresh: candidates slot +- degraded_slots (alarm)
    untrusted  no fix, no recent refresh: TX inhibited; RX falls back to the
               daily RENDEZVOUS word (slot field = 0xFFFFFFFF) only when the policy
               allows it, with an operator warning
  Backward time steps and forward steps beyond the drift model are rejected by
  the nonce source; the sync layer only widens or inhibits.

False-sync model: P_fa per bit position = 2^-N * sum_{k<=t} C(N,k);
expected false hits per search = P_fa * offsets * candidates * polarities; miss
probability = binomial tail of the uncoded word at the raw BER. The functions
below reproduce the design numbers and tests/test_transec_sync.py pins them.

Flag: DPMG_TRANSEC_SIM=1 enables the frame-search hook only for a simulator
context (SecureContext.simulator = True); on a real radio the hook refuses.
TRANSEC on hardware (32-bit sync on air, masked header, buckets, hopping)
is not implemented: the stubs raise NotImplementedPhase2.
"""
from __future__ import annotations

import datetime as _dt
import math
import os
from dataclasses import dataclass

from . import crypto_env as _ce
from . import keys as _keys
from . import nonce as _nonce

SYNC_BITS_N = 32
DEFAULT_T = 3
MARGINAL_T = 4
RENDEZVOUS_SLOT = 0xFFFFFFFF
MAX_REDERIVE = 16
LABEL = b"DPMG/SYNC"


class TransecError(RuntimeError):
    """Refused: untrusted time, not a simulator, flag off."""


class NotImplementedPhase2(NotImplementedError):
    """A designed TRANSEC on-air function that has no implementation."""


# derivation
def _cmac32(key: bytes, msg: bytes) -> int:
    from cryptography.hazmat.primitives import cmac
    from cryptography.hazmat.primitives.ciphers import algorithms
    c = cmac.CMAC(algorithms.AES(key))
    c.update(msg)
    return int.from_bytes(c.finalize()[:4], "big")


def _msg(net_id: int, day: _dt.date, slot: int, k: int) -> bytes:
    return (LABEL + b"|" + bytes([net_id & 0xFF]) + day.strftime("%Y%m%d").encode()
            + (slot & 0xFFFFFFFF).to_bytes(4, "big") + bytes([k & 0xFF]))


def hamming32(a: int, b: int) -> int:
    return bin((a ^ b) & 0xFFFFFFFF).count("1")


def word_bits(w: int) -> str:
    return format(w & 0xFFFFFFFF, "032b")


def autocorr_sidelobe(w: int) -> int:
    """Max |aperiodic autocorrelation| over shifts 1..31 of the +-1 sequence."""
    s = [1 if c == "1" else -1 for c in word_bits(w)]
    best = 0
    for sh in range(1, SYNC_BITS_N):
        acc = sum(s[i] * s[i + sh] for i in range(SYNC_BITS_N - sh))
        best = max(best, abs(acc))
    return best


def longest_run(w: int) -> int:
    b = word_bits(w)
    best = cur = 1
    for i in range(1, len(b)):
        cur = cur + 1 if b[i] == b[i - 1] else 1
        best = max(best, cur)
    return best


def word_quality_ok(w: int, neighbours=(), t: int = DEFAULT_T,
                    max_sidelobe: int = 9, max_run: int = 7) -> bool:
    """Acquisition quality: balanced (12..20 ones), no run > max_run, autocorrelation
    sidelobe <= max_sidelobe, and at least 2t+2 bits from every neighbour word and
    from every neighbour's inverse (the finder retries the inverted stream)."""
    ones = bin(w & 0xFFFFFFFF).count("1")
    if not (12 <= ones <= 20):
        return False
    if longest_run(w) > max_run or autocorr_sidelobe(w) > max_sidelobe:
        return False
    for nb in neighbours:
        if hamming32(w, nb) <= 2 * t + 1 or hamming32(w, ~nb & 0xFFFFFFFF) <= 2 * t + 1:
            return False
    return True


def derive_sync_word(k_sync: bytes, net_id: int, day: _dt.date, slot: int, t: int = DEFAULT_T,
                     neighbours=()) -> tuple:
    """(word, k) for the slot: the first re-derivation k whose word passes
    word_quality_ok against `neighbours`. Deterministic on both ends."""
    for k in range(MAX_REDERIVE):
        w = _cmac32(bytes(k_sync), _msg(net_id, day, slot, k))
        if word_quality_ok(w, neighbours, t):
            return w, k
    # extremely unlikely (p < 1e-9); fall back to k=0 so both ends still agree
    return _cmac32(bytes(k_sync), _msg(net_id, day, slot, 0)), 0


def sync_words_for(k_sync: bytes, net_id: int, day: _dt.date, slots, t: int = DEFAULT_T) -> dict:
    """{slot: word} for a set of consecutive slots, each derived against the
    words of the slots already placed (neighbour separation)."""
    out = {}
    for s in sorted(slots):
        nbs = [out[x] for x in (s - 1, s + 1) if x in out]
        out[s], _k = derive_sync_word(k_sync, net_id, day, s, t, nbs)
    return out


def rendezvous_word(k_sync: bytes, net_id: int, day: _dt.date, t: int = DEFAULT_T) -> int:
    """The daily fallback word for receivers with untrusted time (policy-gated)."""
    return derive_sync_word(k_sync, net_id, day, RENDEZVOUS_SLOT, t)[0]


# time trust
@dataclass
class SyncPolicy:
    t: int = DEFAULT_T
    degraded_slots: int = 8               # +-N candidate slots when degraded
    allow_rendezvous_rx: bool = True      # untrusted receivers may listen for the daily word
    tx_requires_trusted: bool = True      # untrusted or degraded time: TX inhibited


def trust_state(fix: str | None, locked: bool | None, refresh_age_s: float | None,
                holdover_s: float = 300.0) -> str:
    """The three time-trust states from what the radio knows."""
    if fix == "3D" and (locked is None or locked) and refresh_age_s is not None and refresh_age_s <= 10.0:
        return "trusted"
    if refresh_age_s is not None and refresh_age_s <= holdover_s and fix in ("2D", "3D"):
        return "degraded"
    if locked and refresh_age_s is not None and refresh_age_s <= holdover_s:
        return "degraded"                 # GPSDO holdover within the drift model
    return "untrusted"


def candidate_slots(local_slot: int, trust: str, policy: SyncPolicy | None = None) -> list:
    policy = policy or SyncPolicy()
    if trust == "trusted":
        return [local_slot - 1, local_slot, local_slot + 1]
    if trust == "degraded":
        n = int(policy.degraded_slots)
        return list(range(local_slot - n, local_slot + n + 1))
    return []


def tx_allowed(trust: str, policy: SyncPolicy | None = None) -> tuple:
    policy = policy or SyncPolicy()
    if trust == "trusted":
        return True, ""
    if trust == "degraded" and not policy.tx_requires_trusted:
        return True, "degraded time: widened RX window on the far end"
    return False, f"TRANSEC TX inhibited: time is {trust}"


# false-sync model
def p_fa_per_position(n: int = SYNC_BITS_N, t: int = DEFAULT_T) -> float:
    return sum(math.comb(n, k) for k in range(t + 1)) / 2.0 ** n


def expected_false_hits(n: int = SYNC_BITS_N, t: int = DEFAULT_T, offsets: int = 4096,
                        candidates: int = 3, polarities: int = 2) -> float:
    return p_fa_per_position(n, t) * offsets * candidates * polarities


def miss_probability(n: int = SYNC_BITS_N, t: int = DEFAULT_T, ber: float = 1e-2) -> float:
    """P(more than t of n uncoded sync bits in error)."""
    return 1.0 - sum(math.comb(n, k) * ber ** k * (1 - ber) ** (n - k) for k in range(t + 1))


def blind_forger_hit(n: int = SYNC_BITS_N, t: int = DEFAULT_T) -> float:
    return p_fa_per_position(n, t)


# simulator hook
def sim_enabled() -> bool:
    return _ce.flag_on(_ce.FLAG_TRANSEC_SIM)


def sync_spec_for(ctx, now: float | None = None, policy: SyncPolicy | None = None, simulator: bool = False):
    """Simulator-only frame-search hook: a frame_codec.FrameSpec whose sync is the
    keyed 32-bit word for the current slot (t = policy.t), plus the neighbour
    candidates the finder should also try. Refuses on a real radio, with the
    flag off, or with untrusted time (RX rendezvous only when allowed)."""
    from dpmg.framing import frame_codec as fc
    policy = policy or SyncPolicy()
    if not sim_enabled():
        raise TransecError(f"{_ce.FLAG_TRANSEC_SIM} is off")
    if not (simulator or getattr(ctx, "simulator", False)):
        raise TransecError("Keyed sync runs only against a simulator context in this build")
    t, trust = ctx.time.now() if now is None else (now, ctx.time.now()[1])
    day = _nonce.utc_day(t)
    ks = ctx.store.active
    k_sync = ks.transec_key(_keys.LABEL_SYNC, day, ctx.store.net_id)
    local = _nonce.slot_of(t, day, ks.slot_s)
    slots = candidate_slots(local, trust, policy)
    if not slots:
        if not policy.allow_rendezvous_rx:
            raise TransecError("time untrusted and rendezvous listening not allowed")
        words = {RENDEZVOUS_SLOT: rendezvous_word(bytes(k_sync), ctx.store.net_id, day, policy.t)}
    else:
        words = sync_words_for(bytes(k_sync), ctx.store.net_id, day, slots, policy.t)
    primary = words.get(local, next(iter(words.values())))
    spec = fc.FrameSpec(sync_bits=word_bits(primary), max_sync_errors=policy.t, has_length=True,
                        crc="CRC-16-CCITT", invert_ok=True)
    spec.transec_candidates = [word_bits(w) for w in words.values()]     # extra correlators
    spec.transec_slot = local
    spec.transec_trust = trust
    return spec


# designed stubs
def mask_header(header: bytes, k_hp: bytes, sample: bytes) -> bytes:
    """Design (QUIC RFC 9001 section 5.4 model): mask = AES-CTR(K_hp,
    nonce = first 16 B of ciphertext||tag) truncated to the header length;
    header XOR mask. The 128-bit GCM-SIV tag guarantees the 16-byte sample.
    Not implemented in this build."""
    raise NotImplementedPhase2("header masking: not implemented")


def unmask_header(masked: bytes, k_hp: bytes, sample: bytes) -> bytes:
    raise NotImplementedPhase2("header unmasking: not implemented")


def pad_to_bucket(plaintext: bytes, buckets=(16, 32, 64, 128, 200)) -> bytes:
    """Design: pad to the next fixed size bucket (length then masked
    with the header) so LEN leaks nothing. Not implemented in this build."""
    raise NotImplementedPhase2("size buckets: not implemented")


def tx_time_offset(k_time: bytes, net_id: int, day: _dt.date, slot: int, slot_s: float,
                   burst_s: float, guard_s: float) -> float:
    """Design: offset = PRF(K_time, slot) mod (slot_s - burst_s - guard_s);
    guard >= 2 x worst-case clock error + propagation (<= 1 ms at 300 km).
    Not implemented in this build."""
    raise NotImplementedPhase2("keyed time offset: not implemented")


def hop_channel(k_hop: bytes, net_id: int, day: _dt.date, slot: int, channels: list) -> float:
    """Design: per-burst channel = channels[PRF(K_hop, slot) mod len];
    the GPU channelizer already sees the band so RX is nearly free; TX retune
    latency on the NV100 is unmeasured. Not implemented in this build."""
    raise NotImplementedPhase2("keyed channel hop: not implemented")


STUBS = (mask_header, unmask_header, pad_to_bucket, tx_time_offset, hop_channel)
