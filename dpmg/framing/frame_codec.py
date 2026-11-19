"""
frame_codec.py: framing + validity for the channelizer test harness.

This is the adapter layer between raw bits and "a valid message". The base frame
is simple and self-contained:

    [ SYNC (16 bits) | LEN (8 bits) | PAYLOAD (LEN bytes) | CRC16 (16 bits) ]

so synthetic test signals carry real structure and the receiver can decide whether
a decode is "valid" (sync found + CRC passes) rather than just noise-driven bits.

Adapter point: a different framing (sync words, CRC, FEC) plugs in by replacing the
bodies of encode_message() / find_and_decode(). Keep the function signatures and the
FrameResult shape, and the rest of the harness (injector, UI green/red trigger,
TX-back) works as is.

Pure-Python, no deps beyond numpy. Bits are represented as a string of '0'/'1'
(matching DemodResult.bit_string) and as np.uint8 arrays where convenient.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

# 16-bit sync word (Barker-ish, distinctive). Adapter point: replace with the real sync word.
SYNC_BITS = "1011001000111011"
SYNC_LEN = len(SYNC_BITS)


# CRC-16/CCITT (poly 0x1021), a common, simple choice 
def crc16_ccitt(data: bytes, init: int = 0xFFFF) -> int:
    crc = init
    for b in data:
        crc ^= (b << 8)
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc & 0xFFFF


def _bits_to_bytes(bits: str) -> bytes:
    # pad to byte boundary
    if len(bits) % 8:
        bits = bits + "0" * (8 - len(bits) % 8)
    return bytes(int(bits[i:i+8], 2) for i in range(0, len(bits), 8))


def _bytes_to_bits(data: bytes) -> str:
    return "".join(f"{b:08b}" for b in data)


def _int_to_bits(v: int, n: int) -> str:
    return format(v & ((1 << n) - 1), f"0{n}b")


@dataclass
class FrameResult:
    valid: bool
    payload_bits: str = ""        # decoded payload (without sync/len/crc), '0'/'1'
    payload_bytes: bytes = b""
    crc_ok: bool = False
    sync_found: bool = False
    offset: int = -1              # bit offset where sync was found
    message: str = ""             # payload decoded as UTF-8 if printable, else hex
    # A sealed body LEN||HDR||CT||TAG whose CRC passed.
    # The finder never opens it (no keys in decode children); the parent calls
    # secure_frame.open_result / frame_codec.open_sealed. n_info = body bits incl.
    # LEN and CRC (the coded-length inverse the FEC finders need).
    sealed: Optional[bytes] = None
    n_info: int = 0


# encode: message bits/text: framed bit string 
def encode_message(payload: bytes) -> str:
    """Build a full framed bit string from a payload (bytes).

    Adapter point: swap this for the real encoder. Output is a '0'/'1' string ready to be
    mapped to symbols by the modulator.
    """
    if len(payload) > 255:
        raise ValueError("payload too long for 8-bit length field (<=255 bytes)")
    body = _int_to_bits(len(payload), 8) + _bytes_to_bits(payload)
    crc = crc16_ccitt(_bits_to_bytes(_int_to_bits(len(payload), 8)) + payload)
    frame = SYNC_BITS + body + _int_to_bits(crc, 16)
    return frame


def encode_text(text: str) -> str:
    return encode_message(text.encode("utf-8", errors="replace"))


# decode: search a bit string for a valid frame 
def find_and_decode(bit_string: str, max_search: int = 4096,
                    try_invert: bool = True) -> FrameResult:
    """Search bit_string for a valid frame (sync + CRC).

    Also tries the bit-inverted stream, because BPSK/QPSK demods have a 180-degree
    phase ambiguity that flips every recovered bit (including the sync word). If the
    normal pass fails to find sync and try_invert is set, retry inverted.

    Returns FrameResult(valid=True...) only if sync is found and crc passes.
    Adapter point: swap for the real framing/CRC. Keep returning a FrameResult.
    """
    res = _find_and_decode_once(bit_string, max_search)
    if res.valid:
        return res
    if try_invert and bit_string:
        inv = bit_string.translate(str.maketrans("01", "10"))
        res_inv = _find_and_decode_once(inv, max_search)
        if res_inv.valid:
            return res_inv
    # return the better of the two attempts (sync_found preferred) for diagnostics
    return res


def _find_and_decode_once(bit_string: str, max_search: int = 4096) -> FrameResult:
    if not bit_string:
        return FrameResult(valid=False)
    n = len(bit_string)
    limit = min(n - SYNC_LEN, max_search)
    # Keep scanning past CRC-failed hits (see _find_frame_once): a sync-looking
    # run in payload bits or a truncated frame at the buffer edge must not hide
    # the intact frames behind it.
    first_bad = None
    for off in range(0, max(0, limit) + 1):
        if bit_string[off:off+SYNC_LEN] == SYNC_BITS:
            # found sync; parse length
            p = off + SYNC_LEN
            if p + 8 > n:
                continue
            length = int(bit_string[p:p+8], 2)
            p += 8
            need = length * 8
            if p + need + 16 > n:
                continue
            payload_bits = bit_string[p:p+need]
            p += need
            crc_rx = int(bit_string[p:p+16], 2)
            payload = _bits_to_bytes(payload_bits)[:length]
            len_byte = _bits_to_bytes(_int_to_bits(length, 8))
            crc_calc = crc16_ccitt(len_byte + payload)
            ok = (crc_rx == crc_calc)
            try:
                msg = payload.decode("utf-8")
                if not msg.isprintable():
                    msg = payload.hex()
            except Exception:
                msg = payload.hex()
            fr = FrameResult(valid=ok, payload_bits=payload_bits,
                             payload_bytes=payload, crc_ok=ok, sync_found=True,
                             offset=off, message=msg if ok else "")
            if ok:
                return fr
            if first_bad is None:
                first_bad = fr
    return first_bad if first_bad is not None \
        else FrameResult(valid=False, sync_found=False)


# decode: find every valid frame in a bit string (not just the first)
def find_all_frames(bit_string: str, max_search: int = 4096,
                    try_invert: bool = True) -> list:
    """Scan bit_string for all valid frames (sync + CRC), in buffer order.

    find_and_decode returns only the first frame, so a demodulated buffer that
    carries several concatenated messages (e.g. a PING/PONG burst train) surfaces
    only one of them. This returns one FrameResult per valid frame.

    Handles the BPSK/QPSK 180-deg phase ambiguity like find_and_decode: if the
    normal stream yields nothing and try_invert is set, the inverted stream is
    scanned. Every frame in one buffer shares polarity, so the two are never mixed.
    Adapter point: swap for the real framing/CRC. Keep returning a list of FrameResult.
    """
    frames = _find_all_frames_once(bit_string, max_search)
    if frames:
        return frames
    if try_invert and bit_string:
        inv = bit_string.translate(str.maketrans("01", "10"))
        frames = _find_all_frames_once(inv, max_search)
    return frames


def _find_all_frames_once(bit_string: str, max_search: int = 4096) -> list:
    out: list = []
    if not bit_string:
        return out
    n = len(bit_string)
    off = 0
    # Scan the whole buffer: a multi-message buffer is longer than one frame's
    # search window, so max_search (kept for signature symmetry) does not cap the
    # scan here. After a validated frame we resume just past it, so payload bits
    # that happen to look like sync are never re-parsed.
    while off <= n - SYNC_LEN:
        if bit_string[off:off + SYNC_LEN] != SYNC_BITS:
            off += 1
            continue
        p = off + SYNC_LEN
        if p + 8 > n:
            break
        length = int(bit_string[p:p + 8], 2)
        p += 8
        need = length * 8
        if p + need + 16 > n:
            off += 1               # not enough bits left for this frame; keep scanning
            continue
        payload_bits = bit_string[p:p + need]
        p += need
        crc_rx = int(bit_string[p:p + 16], 2)
        p += 16
        payload = _bits_to_bytes(payload_bits)[:length]
        len_byte = _bits_to_bytes(_int_to_bits(length, 8))
        crc_calc = crc16_ccitt(len_byte + payload)
        if crc_rx != crc_calc:
            off += 1               # coincidental sync match inside noise; step past it
            continue
        try:
            msg = payload.decode("utf-8")
            if not msg.isprintable():
                msg = payload.hex()
        except Exception:
            msg = payload.hex()
        out.append(FrameResult(valid=True, payload_bits=payload_bits,
                               payload_bytes=payload, crc_ok=True, sync_found=True,
                               offset=off, message=msg))
        off = p                    # resume immediately after this validated frame
    return out


if __name__ == "__main__":
    # round-trip self-test
    for text in ["HELLO", "ACK-7", "test123"]:
        frame = encode_text(text)
        # simulate some leading noise bits then the frame then trailing noise
        noisy = "0101" + frame + "1100"
        res = find_and_decode(noisy)
        status = "OK" if (res.valid and res.message == text) else "FAIL"
        print(f"  '{text}': frame={len(frame)} bits, decoded='{res.message}' "
              f"crc_ok={res.crc_ok} sync@{res.offset}  {status}")
    print("frame_codec self-test done")


# frame descriptor (declarable frames)
from dataclasses import dataclass, field as _dc_field


def normalize_sync(sync) -> str:
    """Accept a sync/preamble as raw bits ('101100..'), hex ('0xB23B' or 'B23B'),
    or a list/tuple of 0/1 ints; returns the canonical bit string ('' when empty)."""
    if sync is None:
        return ""
    if isinstance(sync, (list, tuple)):
        # structured field list: [{"value":..,"width_bits":..,"role":"sync"}, ...]
        if sync and isinstance(sync[0], dict):
            out = ""
            for f in sync:
                if f.get("role", "sync") != "sync":
                    continue
                out += _int_to_bits(int(f["value"]), int(f["width_bits"]))
            return out
        return "".join("1" if int(b) else "0" for b in sync)
    t = str(sync).strip()
    if not t:
        return ""
    low = t.lower().replace("_", "").replace(" ", "")
    if low.startswith("0x"):
        low = low[2:]
        return bin(int(low, 16))[2:].zfill(len(low) * 4) if low else ""
    if set(low) <= {"0", "1"}:
        return low
    # bare hex (no 0x): only if it looks hex and not pure bits
    if set(low) <= set("0123456789abcdef"):
        return bin(int(low, 16))[2:].zfill(len(low) * 4)
    raise ValueError(f"cannot parse sync/preamble: {sync!r}")


def _hamming(a: str, b: str) -> int:
    return sum(1 for x, y in zip(a, b) if x != y)


@dataclass
class FrameSpec:
    """Declarative frame the channelizer triggers on.
    sync_bits: normalized bit string to correlate on (required to trigger).
    max_sync_errors: allow up to N mismatched sync bits (fault tolerant).
    has_length: an 8-bit length field follows sync (payload length in bytes).
    payload_len_bits: if has_length is False, a fixed payload length in bits
                      (0/None: take the rest of the buffer as payload).
    has_crc: a trailing CRC-16/CCITT over (len_byte + payload) must validate.
    invert_ok: also try the bit-inverted stream (BPSK/QPSK 180-deg ambiguity).
    sync_err_head: when > 0, the max_sync_errors mismatches must all fall in the
                   first sync_err_head bits of the sync (0 = anywhere).
    """
    sync_bits: str = ""
    max_sync_errors: int = 0
    has_length: bool = True
    payload_len_bits: int = 0
    crc: object = None          # None/'' = no CRC; preset name (e.g. 'CRC-16-CCITT') or dict of params
    invert_ok: bool = True
    sync_err_head: int = 0

    @classmethod
    def from_entry(cls, d: dict) -> "FrameSpec":
        """Build from a channel_plan Entry.frame dict (all keys optional)."""
        d = d or {}
        fields = d.get("fields")
        sync_src = d.get("sync") or d.get("sync_bits") or d.get("preamble") or ""
        payload_bits = 0
        max_err = int(d.get("max_sync_errors", d.get("sync_tolerance", 0)) or 0)
        if fields and not sync_src:
            sync_src = [f for f in fields if f.get("role") == "sync"]
            payload_bits = sum(int(f.get("width_bits", 0))
                               for f in fields if f.get("role") == "data")
            se = [int(f.get("max_errors", 0)) for f in fields if f.get("role") == "sync"]
            max_err = max(se) if se else max_err
        return cls(
            sync_bits=normalize_sync(sync_src),
            max_sync_errors=max_err,
            has_length=bool(d.get("has_length", False)),
            payload_len_bits=int(d.get("payload_len_bits", payload_bits) or payload_bits),
            crc=(d.get("crc") if d.get("crc") not in (None, "")
                 else ("CRC-16-CCITT" if d.get("has_crc", False) else None)),
            invert_ok=bool(d.get("invert_ok", True)),
        )


def _sync_hits(mism, spec: FrameSpec):
    """Offsets whose sync window (rows of mism, True = mismatched bit) is within
    the spec's tolerance: at most max_sync_errors mismatches, and with
    sync_err_head set, none past the first sync_err_head bits."""
    ok = mism.sum(axis=1) <= spec.max_sync_errors
    head = int(getattr(spec, "sync_err_head", 0) or 0)
    if head > 0 and spec.max_sync_errors > 0:
        ok &= ~mism[:, head:].any(axis=1)
    return np.nonzero(ok)[0]


def _find_frame_once(bit_string: str, spec: FrameSpec, max_search: int) -> FrameResult:
    sync = spec.sync_bits
    if not sync:
        return FrameResult(valid=False, sync_found=False)
    n = len(bit_string); slen = len(sync)
    limit = min(n - slen, max_search)
    # Vectorized sync search. Calling _hamming() (a Python genexpr over 48 chars) at
    # every bit offset costs ~32,000 interpreted comparisons per search, per channel,
    # per frame, 18% of all decode time (py-spy, 6519 samples).
    # sliding_window_view + one != does the whole search in a single C pass.
    if limit >= 0:
        _b = np.frombuffer(bit_string.encode('ascii'), dtype=np.uint8)
        _s = np.frombuffer(sync.encode('ascii'), dtype=np.uint8)
        _w = np.lib.stride_tricks.sliding_window_view(_b[:limit + slen], slen)
        _hits = _sync_hits(_w != _s, spec)
    else:
        _hits = ()
    # Keep scanning past CRC-failed hits: with fuzzy sync (max_sync_errors) a
    # false hit in payload/pad bits (or a truncated frame at the buffer's
    # leading edge) is common, and stopping at the first one hides every intact
    # frame behind it (the 4FSK/DQPSK "perfect bits, no decode" bench failure).
    first_bad = None
    for off in _hits:
        off = int(off)
        if True:
            p = off + slen
            # length
            if spec.has_length:
                if p + 8 > n:
                    continue
                length = int(bit_string[p:p + 8], 2); p += 8
                need = length * 8
            elif spec.payload_len_bits > 0:
                length = None; need = spec.payload_len_bits
            else:
                length = None; need = n - p  # rest of buffer
            if need < 0 or p + need > n:
                continue
            payload_bits = bit_string[p:p + need]; p += need
            payload = _bits_to_bytes(payload_bits)
            if length is not None:
                payload = payload[:length]
            # crc (parameterized width/poly)
            crc_ok = True
            if spec.crc:
                from dpmg.framing import crc_util as _crc
                params = _crc.get_params(spec.crc)
                cw = params.width
                if p + cw > n:
                    continue
                crc_rx = int(bit_string[p:p + cw], 2)
                lb = _bits_to_bytes(_int_to_bits(length, 8)) if length is not None else b""
                crc_ok = (crc_rx == _crc.crc_of(lb + payload, params))
            try:
                msg = payload.decode("utf-8")
                if not msg.isprintable():
                    msg = payload.hex()
            except Exception:
                msg = payload.hex()
            valid = crc_ok  # sync already matched within tolerance
            fr = FrameResult(valid=valid, payload_bits=payload_bits,
                             payload_bytes=payload, crc_ok=crc_ok, sync_found=True,
                             offset=off, message=msg if valid else "")
            if valid:
                return fr
            if first_bad is None:
                first_bad = fr           # diagnostics: first parseable-but-bad hit
    return first_bad if first_bad is not None \
        else FrameResult(valid=False, sync_found=False)


def find_frame(bit_string: str, spec: FrameSpec, max_search: int = 4096) -> FrameResult:
    """Generalized, fault-tolerant frame finder driven by a FrameSpec."""
    if not bit_string or not spec.sync_bits:
        return FrameResult(valid=False, sync_found=False)
    res = _find_frame_once(bit_string, spec, max_search)
    if res.valid:
        return res
    if spec.invert_ok:
        inv = bit_string.translate(str.maketrans("01", "10"))
        res_inv = _find_frame_once(inv, spec, max_search)
        if res_inv.valid:
            return res_inv
    return res


def find_all_frames_spec(bit_string: str, spec: FrameSpec,
                         max_search: int = 4096) -> list:
    """FrameSpec-driven multi-frame scan: the declarable-frame counterpart of
    find_frame. Returns every valid frame in the buffer, in buffer order.

    Same inversion handling as find_frame: all frames in one buffer share
    polarity, so the normal stream is scanned first and the inverted stream only
    if the normal one yields nothing."""
    if not bit_string or not spec.sync_bits:
        return []
    frames = _find_all_frames_spec_once(bit_string, spec, max_search)
    if frames:
        return frames
    if spec.invert_ok:
        inv = bit_string.translate(str.maketrans("01", "10"))
        frames = _find_all_frames_spec_once(inv, spec, max_search)
    return frames


def _find_all_frames_spec_once(bit_string: str, spec: FrameSpec,
                               max_search: int = 4096) -> list:
    out: list = []
    sync = spec.sync_bits
    if not sync or not bit_string:
        return out
    n = len(bit_string); slen = len(sync)
    if n - slen < 0:
        return out
    # Same vectorized sync correlation as _find_frame_once, but over the whole
    # buffer so every frame is found. Hits inside an already-accepted frame are
    # skipped (consumed_to), so payload bits that resemble sync don't spawn ghosts.
    _b = np.frombuffer(bit_string.encode('ascii'), dtype=np.uint8)
    _s = np.frombuffer(sync.encode('ascii'), dtype=np.uint8)
    _w = np.lib.stride_tricks.sliding_window_view(_b, slen)
    _hits = _sync_hits(_w != _s, spec)
    _crc = None; _params = None
    if spec.crc:
        from dpmg.framing import crc_util as _crc
        _params = _crc.get_params(spec.crc)
    consumed_to = -1
    for off in _hits:
        off = int(off)
        if off <= consumed_to:
            continue
        p = off + slen
        if spec.has_length:
            if p + 8 > n:
                continue
            length = int(bit_string[p:p + 8], 2); p += 8
            need = length * 8
        elif spec.payload_len_bits > 0:
            length = None; need = spec.payload_len_bits
        else:
            length = None; need = n - p
        if need < 0 or p + need > n:
            continue
        payload_bits = bit_string[p:p + need]; p += need
        payload = _bits_to_bytes(payload_bits)
        if length is not None:
            payload = payload[:length]
        crc_ok = True
        if _params is not None:
            cw = _params.width
            if p + cw > n:
                continue
            crc_rx = int(bit_string[p:p + cw], 2)
            lb = _bits_to_bytes(_int_to_bits(length, 8)) if length is not None else b""
            crc_ok = (crc_rx == _crc.crc_of(lb + payload, _params))
            p += cw
        if not crc_ok:
            continue
        try:
            msg = payload.decode("utf-8")
            if not msg.isprintable():
                msg = payload.hex()
        except Exception:
            msg = payload.hex()
        out.append(FrameResult(valid=True, payload_bits=payload_bits,
                               payload_bytes=payload, crc_ok=True, sync_found=True,
                               offset=off, message=msg))
        consumed_to = p - 1
    return out


# FEC framing
# A FEC frame is  SYNC(uncoded) + conv.encode(LEN + PAYLOAD + CRC16).  The sync
# word stays uncoded so the channelizer's existing sync correlator still finds
# frame start and recovers timing; everything after it is one zero-tail
# convolutional block. fec = {"type": "conv", "rate": "1/2".."7/8"}.
def _conv():
    from dpmg.fec import conv
    return conv


def _coded_len(n_info: int, rate: str) -> int:
    """Output length of conv.encode() for n_info info bits at the given rate."""
    return len(_conv().encode([0] * n_info, rate))


def encode_fec(payload: bytes, fec: dict) -> str:
    """SYNC + FEC-coded (LEN + PAYLOAD + CRC16) bit string."""
    if len(payload) > 255:
        raise ValueError("payload too long for 8-bit length field (<=255 bytes)")
    len_bits = _int_to_bits(len(payload), 8)
    crc = crc16_ccitt(_bits_to_bytes(len_bits) + payload)
    info = len_bits + _bytes_to_bits(payload) + _int_to_bits(crc, 16)
    kind = str((fec or {}).get("type", "conv")).lower()
    if kind == "conv":
        coded = _conv().encode(info, str(fec.get("rate", "1/2")))
        return SYNC_BITS + "".join("1" if b else "0" for b in coded)
    if kind in _BLOCK_KINDS:
        return SYNC_BITS + _block_encode(info, fec)
    raise ValueError(f"unsupported fec type: {kind!r}")


# Block-code frames
# Same layout as conv: SYNC (uncoded) + coded(LEN + PAYLOAD + CRC16). Every code
# is systematic with LEN first, so the receiver reads LEN off the coded body,
# derives the exact coded length and decodes that block. Short frames shorten
# the code (known-zero info that is never sent). fec dicts:
#   {"type": "rs"}                            shortened RS(255,223) over the bytes
#   {"type": "rs+conv", "rate": "1/2".."7/8"} RS outer, conv inner (concatenated)
#   {"type": "ldpc", "code": "qc-r12|qc-r23|qc-r34"}  zero-filled QC-LDPC
#   {"type": "tpc", "code": "eH(16,11)^2|eH(32,26)^2"}  row-shortened product code
# The RS interleaver is not used: one frame is one codeword (depth 1).
_BLOCK_KINDS = ("rs", "rs+conv", "ldpc", "tpc")
_RS_PARITY_BITS = 256                     # 32 parity bytes
_KNOWN_LLR = 50.0                         # confidence of a shortened (known-0) bit


def fec_kind(fec: dict) -> str:
    return str((fec or {}).get("type", "conv")).lower()


def fec_max_info_bits(fec: dict) -> int:
    """Largest LEN + PAYLOAD + CRC (bits) one frame of this code carries."""
    kind = fec_kind(fec)
    if kind in ("rs", "rs+conv"):
        return 223 * 8
    if kind == "ldpc":
        from dpmg.fec import ldpc
        return ldpc.get_ldpc(str(fec.get("code", "qc-r12"))).K
    if kind == "tpc":
        from dpmg.fec import tpc
        return tpc.get_tpc(str(fec.get("code", "eH(32,26)^2"))).k
    return 8 + 255 * 8 + 16                # conv: the 8-bit LEN limit


def _bits(v) -> str:
    return "".join("1" if int(b) else "0" for b in v)


def _block_encode(info: str, fec: dict) -> str:
    kind = fec_kind(fec)
    n = len(info)
    if n > fec_max_info_bits(fec):
        raise ValueError(f"frame of {n} info bits exceeds one {kind} block "
                         f"({fec_max_info_bits(fec)} bits)")
    if kind in ("rs", "rs+conv"):
        from dpmg.fec import rs
        k = n // 8
        data = list(_bits_to_bytes(info))
        cw = rs.encode([0] * (rs.K - k) + data)[rs.K - k:]      # shortened
        out = _bits(rs.bytes_to_bits(cw))
        if kind == "rs+conv":
            out = _bits(_conv().encode(out, str(fec.get("rate", "1/2"))))
        return out
    if kind == "ldpc":
        from dpmg.fec import ldpc
        code = ldpc.get_ldpc(str(fec.get("code", "qc-r12")))
        u = [int(c) for c in info] + [0] * (code.K - n)
        c = np.asarray(code.encode(u), dtype=np.int64)
        return _bits(c[code._info_cols[:n]]) + _bits(c[code._pivot_cols])
    if kind == "tpc":
        from dpmg.fec import tpc
        t = tpc.get_tpc(str(fec.get("code", "eH(32,26)^2")))
        r = -(-n // t.kc)                                       # rows used
        u = [int(c) for c in info] + [0] * (t.k - n)
        M = np.asarray(t.encode(u), dtype=np.int64).reshape(t.nr, t.nc)
        return _bits(M[:r].reshape(-1)) + _bits(M[t.kr:].reshape(-1))
    raise ValueError(f"unsupported fec type: {kind!r}")


def _block_coded_len(n_info: int, fec: dict) -> int:
    """On-air coded bits for n_info info bits (the inverse the decoder needs)."""
    kind = fec_kind(fec)
    if kind == "rs":
        return n_info + _RS_PARITY_BITS
    if kind == "rs+conv":
        return _coded_len(n_info + _RS_PARITY_BITS, str(fec.get("rate", "1/2")))
    if kind == "ldpc":
        from dpmg.fec import ldpc
        code = ldpc.get_ldpc(str(fec.get("code", "qc-r12")))
        return n_info + (code.N - code.K)
    if kind == "tpc":
        from dpmg.fec import tpc
        t = tpc.get_tpc(str(fec.get("code", "eH(32,26)^2")))
        return (-(-n_info // t.kc)) * t.nc + (t.nr - t.kr) * t.nc
    return _coded_len(n_info, str((fec or {}).get("rate", "1/2")))


def _frame_coded_len(n_info: int, fec: dict) -> int:
    if fec_kind(fec) == "conv":
        return _coded_len(n_info, str((fec or {}).get("rate", "1/2")))
    return _block_coded_len(n_info, fec)


def _len_candidates(llr8) -> list:
    """LEN byte read off the first 8 systematic bits: the hard value, then the
    single flips of the three least reliable bits (one bit error in LEN would
    otherwise lose a frame the code itself can correct)."""
    llr8 = np.asarray(llr8, dtype=np.float64)
    hard = [1 if v > 0 else 0 for v in llr8]
    out = [int(_bits(hard), 2)]
    for i in np.argsort(np.abs(llr8), kind="stable")[:3]:
        h = list(hard)
        h[int(i)] ^= 1
        v = int(_bits(h), 2)
        if v not in out:
            out.append(v)
    return out


def _block_decode(coded: str, fec: dict, llr=None, layout=None) -> Optional["FrameResult"]:
    kind = fec_kind(fec)
    lay = layout or LEGACY
    full = (np.where(np.frombuffer(coded.encode("ascii"), np.uint8) == ord("1"), 6.0, -6.0)
            if llr is None else np.asarray(llr, dtype=np.float64)[:len(coded)])
    if kind == "rs+conv":
        rate = str(fec.get("rate", "1/2"))
        conv = _conv()
        probe_n = min(len(full), 96)
        if probe_n < 24:
            return None
        try:
            probe = conv.decode_soft(full[:probe_n], rate, terminated=False)
        except Exception:
            return None
        if len(probe) < 8:
            return None
        lens = [int(_bits(probe[:8]), 2)]
        tail5 = _bits(probe[8:13]).ljust(5, "0")      # SUITE/EPOCH/HTYPE (secure layout)
        inner = lambda n_info: conv.decode_soft(full[:_block_coded_len(n_info, fec)],
                                                rate, terminated=True)
    else:
        if len(full) < 8:
            return None
        lens = _len_candidates(full[:8])
        tail5 = _bits([1 if v > 0 else 0 for v in full[8:13]]).ljust(5, "0")
        inner = None
    for length in lens:
        n_info = lay.n_info(_int_to_bits(length, 8) + tail5)
        if n_info is None:
            continue
        clen = _block_coded_len(n_info, fec)
        if clen > len(full):
            continue
        try:
            if kind in ("rs", "rs+conv"):
                from dpmg.fec import rs
                if inner is not None:
                    bits = inner(n_info)
                else:
                    bits = [1 if v > 0 else 0 for v in full[:clen]]
                rx = rs.bits_to_bytes(list(bits)[:n_info + _RS_PARITY_BITS])
                k = n_info // 8
                info_b, _nerr, ok = rs.decode([0] * (rs.K - k) + rx)
                if not ok:
                    continue
                info = _bits(rs.bytes_to_bits(info_b[rs.K - k:]))
            elif kind == "ldpc":
                from dpmg.fec import ldpc
                code = ldpc.get_ldpc(str(fec.get("code", "qc-r12")))
                L = np.full(code.N, -_KNOWN_LLR)
                L[code._info_cols[:n_info]] = full[:n_info]
                L[code._pivot_cols] = full[n_info:clen]
                info = _bits(code.decode(L)[:n_info])
            elif kind == "tpc":
                from dpmg.fec import tpc
                t = tpc.get_tpc(str(fec.get("code", "eH(32,26)^2")))
                r = -(-n_info // t.kc)
                # known zero rows: confident but on the channel's scale, so the
                # decoder's unit-mean normalisation keeps its alpha/beta schedule
                big = 2.0 * float(np.mean(np.abs(full[:clen])) or 1.0)
                R = np.full((t.nr, t.nc), -big)
                R[:r] = full[:r * t.nc].reshape(r, t.nc)
                R[t.kr:] = full[r * t.nc:clen].reshape(t.nr - t.kr, t.nc)
                info = _bits(np.asarray(t.decode(R.reshape(-1)))[:n_info])
            else:
                return None
        except Exception:
            continue
        fr = lay.parse(info)
        if fr is not None and fr.valid:
            return fr
    return None


def _parse_info(info: str) -> Optional["FrameResult"]:
    """Parse LEN + PAYLOAD + CRC16 out of decoded info bits."""
    if len(info) < 24:
        return None
    length = int(info[:8], 2)
    need = length * 8
    if 8 + need + 16 > len(info):
        return None
    payload_bits = info[8:8 + need]
    crc_rx = int(info[8 + need:8 + need + 16], 2)
    payload = _bits_to_bytes(payload_bits)[:length]
    crc_calc = crc16_ccitt(_bits_to_bytes(_int_to_bits(length, 8)) + payload)
    ok = (crc_rx == crc_calc)
    try:
        msg = payload.decode("utf-8")
        if not msg.isprintable():
            msg = payload.hex()
    except Exception:
        msg = payload.hex()
    return FrameResult(valid=ok, payload_bits=payload_bits, payload_bytes=payload,
                       crc_ok=ok, sync_found=True, offset=0, message=msg if ok else "")


class SoftBits(str):
    """A '0'/'1' hard-decision string that also carries one LLR per
    bit in .llr (llr > 0 favours '1', the dpmg/fec convention). Every str
    consumer sees plain hard bits; the FEC frame finder feeds .llr to the soft
    Viterbi instead of +-6 hard LLRs. Slicing/translate return plain str."""
    def __new__(cls, bits="", llr=None):
        o = super().__new__(cls, bits)
        o.llr = None if llr is None else np.asarray(llr, dtype=np.float64)
        return o


def _invert_bits(bits: str) -> str:
    """Bit-invert, keeping (negated) LLRs when the input carries them."""
    inv = bits.translate(str.maketrans("01", "10"))
    if getattr(bits, "llr", None) is not None:
        return SoftBits(inv, -bits.llr)
    return inv


def _fec_decode_block(coded: str, fec: dict, llr=None, layout=None) -> Optional["FrameResult"]:
    """Two-pass Viterbi on one coded segment: probe LEN with an unterminated
    decode, recompute the exact coded length, then decode that exact zero-tail
    block (this tolerates trailing pad/noise bits between looped frames). `llr`
    (one per coded bit, from a soft demod) replaces the hard +-6 LLRs.
    `layout` (None = the legacy LEN/PAYLOAD/CRC body, SECURE = the
    sealed body) sizes the block and parses the info bits."""
    lay = layout or LEGACY
    if fec_kind(fec) in _BLOCK_KINDS:                   # FEC_BLOCK
        return _block_decode(coded, fec, llr, layout)
    rate = str((fec or {}).get("rate", "1/2"))
    conv = _conv()
    # hard bits: LLR (clean loopback: generous confidence, sign = bit)
    full_llr = ([6.0 if c == "1" else -6.0 for c in coded] if llr is None
                else np.asarray(llr, dtype=np.float64)[:len(coded)])
    probe_n = min(len(full_llr), 96)
    if probe_n < 24:
        return None
    try:
        info_probe = conv.decode_soft(full_llr[:probe_n], rate, terminated=False)
    except Exception:
        return None
    if len(info_probe) < 8:
        return None
    n_info = lay.n_info("".join(str(b) for b in info_probe[:13]).ljust(13, "0"))
    if n_info is None:
        return None
    clen = _coded_len(n_info, rate)
    if clen > len(coded):
        return None
    try:
        info_bits = conv.decode_soft(full_llr[:clen], rate, terminated=True)
    except Exception:
        return None
    return lay.parse("".join(str(b) for b in info_bits))


_FEC_SOFT_MAX_MISSES = 6


def _fec_find_once(bit_string: str, fec: dict, max_sync_errors: int = 0,
                   _misses=None, first_only: bool = False, layout=None) -> list:
    out: list = []
    n = len(bit_string)
    if n < SYNC_LEN:
        return out
    llr = getattr(bit_string, "llr", None)
    if llr is None or len(llr) != n:
        syncs = [i for i in range(0, n - SYNC_LEN + 1)
                 if bit_string[i:i + SYNC_LEN] == SYNC_BITS]
        for i, a in enumerate(syncs):
            end = syncs[i + 1] if i + 1 < len(syncs) else n
            seg = bit_string[a + SYNC_LEN:end]
            fr = _fec_decode_block(seg, fec, layout=layout)
            if fr is not None and fr.valid and fr.message:
                fr.offset = a
                out.append(fr)
                if first_only:
                    break
        return out
    # Soft bits (one demod pass, not the 10-offset hard sweep, so
    # the extra Viterbi probes are affordable). The sync word is uncoded, so
    # demanding it exact throws away frames the code would still correct; allow
    # the plan's max_sync_errors like the uncoded find_frame does. A noisy hit
    # can sit inside the previous frame's coded body, so the segment runs to
    # the buffer end (the block decode reads only its exact coded length) and
    # hits inside an accepted frame are skipped. In noise every fuzzy hit costs
    # a Viterbi over a random LEN, so hits are tried best soft-sync correlation
    # first and the search stops after _FEC_SOFT_MAX_MISSES failed candidates
    # (real frames rank on top; a noise-only window stays ~0.3 s, not ~2 s).
    _b = np.frombuffer(str.encode(bit_string, "ascii"), dtype=np.uint8)
    _s = np.frombuffer(SYNC_BITS.encode("ascii"), dtype=np.uint8)
    _w = np.lib.stride_tricks.sliding_window_view(_b, SYNC_LEN)
    hits = np.nonzero((_w != _s).sum(axis=1) <= int(max_sync_errors))[0]
    _pm = np.where(_s == ord("1"), 1.0, -1.0)
    corr = np.lib.stride_tricks.sliding_window_view(llr, SYNC_LEN)[hits] @ _pm
    rate = str((fec or {}).get("rate", "1/2"))
    taken: list = []                      # (start, end) of accepted frames
    misses = _misses if _misses is not None else [0]     # shared across polarities
    for a in hits[np.argsort(-corr, kind="stable")]:
        a = int(a)
        if any(lo <= a <= hi for lo, hi in taken):
            continue
        p = a + SYNC_LEN
        fr = _fec_decode_block(str.__getitem__(bit_string, slice(p, n)), fec, llr=llr[p:],
                               layout=layout)
        if fr is not None and fr.valid and fr.message:
            fr.offset = a
            out.append(fr)
            span = fr.n_info or (8 + len(fr.payload_bytes) * 8 + 16)
            taken.append((a, p + _frame_coded_len(span, fec) - 1))
            if first_only:
                break
        else:
            misses[0] += 1
            if misses[0] >= _FEC_SOFT_MAX_MISSES:
                break
    out.sort(key=lambda f: f.offset)
    return out


def fec_find_all_frames(bit_string: str, fec: dict, try_invert: bool = True,
                        max_sync_errors: int = 0, first_only: bool = False,
                        layout=None) -> list:
    """FEC counterpart of find_all_frames: every valid FEC frame in the buffer.
    Handles the BPSK/QPSK 180-deg ambiguity by retrying the inverted stream (a
    linear conv code maps an inverted input to an inverted output).
    max_sync_errors applies only to SoftBits input (see _fec_find_once); a plain
    hard-bit string keeps the exact sync match. layout=SECURE finds
    sealed frames (FrameResult.sealed set, plaintext not released here)."""
    misses = [0]
    out = _fec_find_once(bit_string or "", fec, max_sync_errors, misses, first_only, layout)
    if out:
        return out
    if try_invert and bit_string and misses[0] < _FEC_SOFT_MAX_MISSES:
        out = _fec_find_once(_invert_bits(bit_string), fec, max_sync_errors, misses,
                             first_only, layout)
    return out


def fec_find_and_decode(bit_string: str, fec: dict, try_invert: bool = True,
                        max_sync_errors: int = 0, layout=None) -> "FrameResult":
    # The caller needs one valid frame; decoding every frame
    # in a 1.1 s window here and again in decode_pool's all-frames rescan would make a
    # coded window cost 2-3 s of Viterbi (FEC-07). The first frame is the same one
    # the all-frames search returns.
    frames = fec_find_all_frames(bit_string, fec, try_invert=try_invert,
                                 max_sync_errors=max_sync_errors, first_only=True,
                                 layout=layout)
    return frames[0] if frames else FrameResult(valid=False, sync_found=False)


# layouts
# A body layout tells the FEC finders how many info bits a frame has (from its
# first 13 systematic bits) and how to parse the decoded info bits. LEGACY is
# the LEN/PAYLOAD/CRC body above; SECURE is the sealed body
# (secure_frame.py: LEN | HDR | CT | TAG | CRC). Passing no layout keeps every
# legacy path byte-identical.
class _LegacyLayout:
    name = "legacy"

    @staticmethod
    def n_info(prefix13: str):
        return 8 + int(prefix13[:8], 2) * 8 + 16

    @staticmethod
    def parse(info: str):
        return _parse_info(info)


class _SecureLayout:
    name = "secure"

    @staticmethod
    def n_info(prefix13: str):
        from dpmg.secure import api as _sf
        r = _sf.read_prefix(prefix13)
        return None if r is None else r[3]

    @staticmethod
    def parse(info: str):
        return _parse_secure_info(info)


LEGACY = _LegacyLayout()
SECURE = _SecureLayout()


# sealed body (secure frame)
# [ SYNC (uncoded) | coded( LEN | HDR(32/48) | CIPHERTEXT | TAG(128) | CRC16 ) ]
# LEN counts ciphertext bytes; the CRC covers bytes(LEN..TAG) as a noise pre-
# check; integrity and confidentiality are the AEAD tag's (secure_frame.py).
# Encrypt then CRC then FEC on the way out; FEC then CRC then open on the way in.
def _parse_secure_info(info: str) -> Optional["FrameResult"]:
    """LEN | HDR | CT | TAG | CRC16: FrameResult carrying the sealed body
    (crc-checked, not opened: keys live only in the parent process)."""
    from dpmg.secure import api as _sf
    r = _sf.read_prefix(info[:13])
    if r is None:
        return None
    length, _suite, _long, total = r
    if total > len(info):
        return None
    body_bits = info[:total - 16]
    body = _bits_to_bytes(body_bits)
    crc_rx = int(info[total - 16:total], 2)
    ok = (crc_rx == crc16_ccitt(body))
    return FrameResult(valid=ok, payload_bits=body_bits, payload_bytes=body, crc_ok=ok,
                       sync_found=True, offset=0,
                       message=(f"[sealed {length} B]" if ok else ""),
                       sealed=(body if ok else None), n_info=total)


def secure_body_bits(body: bytes) -> str:
    """LEN||HDR||CT||TAG bytes (secure_frame.SecureContext.seal): body bits + CRC16."""
    return _bytes_to_bits(body) + _int_to_bits(crc16_ccitt(body), 16)


def encode_secure(plaintext: bytes, fec: dict = None, ctx=None, long_header=None) -> str:
    """SYNC + [FEC-coded] (LEN | HDR | AES-256-GCM-SIV(plaintext) | TAG | CRC16).
    Refuses (ValueError) a coding that cannot carry this frame instead of
    truncating; refuses (SecureError) when the context cannot seal (no key,
    policy, nonce exhaustion). The nonce is consumed only after the size check."""
    from dpmg.secure import api as _sf
    ctx = ctx or _sf.get_context()
    if ctx is None:
        raise _sf.SecureError("secure messaging not available: " + (_sf.last_error() or "no context"))
    pt = bytes(plaintext)
    suite = ctx.store.active.suite
    if long_header is None:
        # size the worst case (long header) so a frame never fails after sealing
        long_hint = True
    else:
        long_hint = bool(long_header)
    cap = _sf.capacity(fec, suite, long_hint)
    if cap is None or len(pt) > cap:
        raise ValueError(f"{len(pt)} B plaintext does not fit one {fec_kind(fec) if fec else 'uncoded'} "
                         f"frame with the {suite} suite (max {cap if cap is not None else 0} B)")
    body = ctx.seal(pt, long_header=long_header)
    info = secure_body_bits(body)
    kind = str((fec or {}).get("type", "conv")).lower() if fec else None
    if not fec:
        return SYNC_BITS + info
    if kind == "conv":
        coded = _conv().encode(info, str(fec.get("rate", "1/2")))
        return SYNC_BITS + "".join("1" if b else "0" for b in coded)
    if kind in _BLOCK_KINDS:
        return SYNC_BITS + _block_encode(info, fec)
    raise ValueError(f"unsupported fec type: {kind!r}")


def _secure_find_once(bit_string: str, max_search: int, max_sync_errors: int,
                      first_only: bool) -> list:
    """Uncoded sealed frames: vectorised sync search (as _find_frame_once), then
    the body parse sizes itself from LEN/SUITE/HTYPE and checks the CRC."""
    out: list = []
    n = len(bit_string)
    if n < SYNC_LEN + 13:
        return out
    limit = n - SYNC_LEN if max_search is None else min(n - SYNC_LEN, max_search)
    _b = np.frombuffer(str.encode(bit_string, "ascii"), dtype=np.uint8)
    _s = np.frombuffer(SYNC_BITS.encode("ascii"), dtype=np.uint8)
    _w = np.lib.stride_tricks.sliding_window_view(_b[:limit + SYNC_LEN], SYNC_LEN)
    hits = np.nonzero((_w != _s).sum(axis=1) <= int(max_sync_errors))[0]
    consumed_to = -1
    for off in hits:
        off = int(off)
        if off <= consumed_to:
            continue
        p = off + SYNC_LEN
        fr = _parse_secure_info(str.__getitem__(bit_string, slice(p, n)))
        if fr is None or not fr.valid:
            continue
        fr.offset = off
        out.append(fr)
        consumed_to = p + fr.n_info - 1
        if first_only:
            break
    return out


def secure_find_all_frames(bit_string: str, max_search: int = None, try_invert: bool = True,
                           max_sync_errors: int = 0, first_only: bool = False) -> list:
    """Every CRC-valid sealed frame in an uncoded bit string (buffer order);
    inverted-stream retry as find_all_frames. Open the .sealed bodies with
    secure_frame.open_result / open_sealed."""
    if not bit_string:
        return []
    out = _secure_find_once(bit_string, max_search, max_sync_errors, first_only)
    if out:
        return out
    if try_invert:
        out = _secure_find_once(_invert_bits(bit_string), max_search, max_sync_errors, first_only)
    return out


def secure_find_and_decode(bit_string: str, max_search: int = None, try_invert: bool = True,
                           max_sync_errors: int = 0) -> "FrameResult":
    frames = secure_find_all_frames(bit_string, max_search, try_invert, max_sync_errors,
                                    first_only=True)
    return frames[0] if frames else FrameResult(valid=False, sync_found=False)


def open_sealed(fr: "FrameResult", ctx=None) -> "FrameResult":
    """In-process open of a sealed FrameResult (modem.receive, transceive,
    tests). valid/message become the authenticated plaintext or stay empty;
    fr.secure_verdict carries the verdict string."""
    from dpmg.secure import api as _sf
    if fr is None or fr.sealed is None:
        return fr
    ctx = ctx or _sf.get_context(tx=False)
    if ctx is None:
        fr.valid, fr.message, fr.payload_bytes, fr.payload_bits = False, "", b"", ""
        fr.secure_verdict = _sf.NO_KEY
        return fr
    r = ctx.open(fr.sealed)
    fr.secure_verdict = r.verdict
    fr.secure_result = r.as_dict()
    if r.ok:
        fr.payload_bytes = r.plaintext
        fr.payload_bits = _bytes_to_bits(r.plaintext)
        try:
            m = r.plaintext.decode("utf-8")
            if not m.isprintable():
                m = r.plaintext.hex()
        except Exception:
            m = r.plaintext.hex()
        fr.message, fr.valid = m, True
    else:
        fr.valid, fr.message, fr.payload_bytes, fr.payload_bits = False, "", b"", ""
    return fr


def build_frame(payload_bits: str, spec: "FrameSpec") -> str:
    """Wrap payload bits in the declared frame: sync [+ len byte] + payload [+ CRC16].
    Inverse of find_frame. Used by compose when frame.auto_wrap is set so the
    transmitted signal contains exactly the structure the channelizer triggers on."""
    if not spec or not spec.sync_bits:
        return payload_bits
    frame = spec.sync_bits
    payload_bytes = _bits_to_bytes(payload_bits)
    length = len(payload_bytes)
    if spec.has_length:
        frame += _int_to_bits(length, 8)
    frame += payload_bits
    if spec.crc:
        from dpmg.framing import crc_util as _crc
        params = _crc.get_params(spec.crc)
        lb = _bits_to_bytes(_int_to_bits(length, 8)) if spec.has_length else b""
        crc_val = _crc.crc_of(lb + payload_bytes, params)
        frame += _int_to_bits(crc_val, params.width)
    return frame
