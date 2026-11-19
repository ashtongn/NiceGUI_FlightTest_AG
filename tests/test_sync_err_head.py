#!/usr/bin/env python3
"""
test_sync_err_head.py: chance frames from a fuzzy sync on uncoded clear frames.

On the 9 November bench one UPLINK OQPSK line came out as a 24-byte hex payload
between good frames. The plan frame allows 2 sync errors in a 16-bit sync with
a CRC-16, and the OQPSK recovery tries several hundred hypotheses per window,
each a window of random-looking bits when it is wrong: two errors anywhere in
the sync plus a chance CRC pass then come up about once in twenty failed
windows (3 in 58 noise-only windows offline). The decode pool keeps the
tolerance to the first two sync bits (the symbol a differential detector or a
settling filter gets wrong after the dead air), FrameSpec.sync_err_head.

Checks:
  1. random bit strings, the volume of a few hundred failed windows: the
     sync hits that each risk a chance CRC pass, 34 times fewer with the
     head rule;
  2. a frame whose sync has errors in its first two bits still decodes, one
     with an error further in does not, at the frame layer;
  3. through decode_pool._default_decode (BPSK channel, the plan's default
     frame with 2 sync errors): a first-symbol error decodes, a mid-sync error
     does not, a clean frame does.
Exit 0 = all passed.
"""
from __future__ import annotations

import dataclasses
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import numpy as np

from dpmg.framing import frame_codec as fc
from dpmg.dsp.decode_pool import DecodeSpec, _default_decode, SYNC_ERR_HEAD
from dpmg.scenario.gen_scenario_json import DEFAULT_SYNC

failures = []


def check(name, ok, detail=""):
    print(f"  {'OK  ' if ok else 'FAIL'} {name} {detail}")
    if not ok:
        failures.append(name)


def flip(bits, i):
    return bits[:i] + ("1" if bits[i] == "0" else "0") + bits[i + 1:]


def main() -> int:
    spec = fc.FrameSpec.from_entry(DEFAULT_SYNC)
    head = dataclasses.replace(spec, sync_err_head=SYNC_ERR_HEAD)
    check("plan default frame allows 2 sync errors", spec.max_sync_errors == 2)

    # 1. sync hits in random bits, the volume of a few hundred failed windows
    # (2000 strings of 6500 bits, one window of 9600 bit/s hard bits each).
    # Every hit costs a chance CRC-16 pass at 1 in 65536: 137 of 65536 sync
    # words are within 2 errors anywhere, 4 with the errors in the first two
    # bits. (Full frames over 20000 such strings: 12 chance frames with errors
    # anywhere, 1 with the head rule.)
    rng = np.random.default_rng(0x5EC)
    s_bits = np.frombuffer(spec.sync_bits.encode("ascii"), np.uint8)
    n_any = n_head = 0
    for _ in range(2000):
        b = rng.integers(0, 2, 6500).astype(np.uint8) + ord("0")
        mism = np.lib.stride_tricks.sliding_window_view(b, 16) != s_bits
        n_any += len(fc._sync_hits(mism, spec))
        n_head += len(fc._sync_hits(mism, head))
    ratio = n_any / max(1, n_head)
    check("random bits: sync hits with errors anywhere", 0.8 < n_any / (2000 * 6485 * 137 / 65536) < 1.2,
          f"({n_any})")
    check("random bits: head rule cuts the hits 25 to 45 times", 25 < ratio < 45,
          f"({n_head}, {ratio:.0f} times fewer)")

    # 2. frame layer
    frame = fc.encode_message(b"UPLINK OQPSK")
    pad = "0110" * 40
    for errs, want in (([], True), ([0], True), ([1], True), ([0, 1], True),
                       ([5], False), ([0, 9], False), ([15], False)):
        f = frame
        for i in errs:
            f = flip(f, i)
        r = fc.find_frame(pad + f + pad, head, max_search=10000)
        check(f"frame layer, sync errors at {errs}", bool(r.valid) == want and
              (not want or r.message == "UPLINK OQPSK"))

    # 3. through the decode pool, BPSK at 10 samples/bit
    from dpmg.modulation.modulator import get_modulator
    sps = 10
    mod = get_modulator("BPSK", 9600.0, sps)
    entry = {"symbol_rate": 9600.0, "frame": dict(DEFAULT_SYNC)}
    for errs, want in (([], True), ([0], True), ([7], False)):
        f = frame
        for i in errs:
            f = flip(f, i)
        burst = np.asarray(mod.modulate("0110" * 8 + f + "0110" * 8).iq_samples, np.complex64)
        gap = np.zeros(400, np.complex64)
        x = np.tile(np.concatenate([gap, burst]), 4)
        r2 = np.random.default_rng(len(errs) * 100 + sum(errs))
        x = (x + 0.02 * (r2.standard_normal(len(x)) + 1j * r2.standard_normal(len(x)))).astype(np.complex64)
        res = _default_decode(0, x, DecodeSpec(modulation="BPSK", fs_out=9600.0 * sps, sps=sps,
                                               demod_params=entry, lock={}))
        got = bool(res.valid and res.message == "UPLINK OQPSK")
        check(f"decode pool BPSK, sync errors at {errs}", got == want,
              f"(valid={res.valid} msg={res.message!r})")

    print(f"\n{'all passed' if not failures else str(len(failures)) + ' failed'}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
