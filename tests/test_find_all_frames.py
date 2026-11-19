"""
find_all_frames: a bit buffer holding several concatenated framed messages
(a PING/PONG burst train) must surface every valid frame, not just the first.
Covers the simple frame path and the declarative FrameSpec path, plus the
inverted-stream (BPSK 180-deg) case. Deterministic. Exit 0 = all pass.
"""
from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dpmg.framing import frame_codec as fc

checks = []


def chk(name, ok, detail=""):
    checks.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")


def main():
    print("find_all_frames\n")
    msgs = ["PING 1", "PING 2", "TEXT HELLO", "BYE"]
    buf = "0101" + "".join(fc.encode_text(m) for m in msgs) + "11"

    got = [f.message for f in fc.find_all_frames(buf) if f.valid]
    chk("all four messages surfaced", got == msgs, f"({got})")

    # single message still works (back-compat with find_and_decode)
    one = [f.message for f in fc.find_all_frames(fc.encode_text("ACK 7")) if f.valid]
    chk("single frame", one == ["ACK 7"])

    # inverted stream (BPSK/QPSK 180-deg ambiguity)
    inv = buf.translate(str.maketrans("01", "10"))
    goti = [f.message for f in fc.find_all_frames(inv) if f.valid]
    chk("inverted stream recovers all", goti == msgs, f"({goti})")

    # declarative FrameSpec path
    spec = fc.FrameSpec(sync_bits=fc.SYNC_BITS, has_length=True,
                        crc="CRC-16-CCITT")
    gots = [f.message for f in fc.find_all_frames_spec(buf, spec) if f.valid]
    chk("FrameSpec path finds all", gots == msgs, f"({gots})")

    # noise-only buffer: nothing
    chk("noise yields no frames",
        [f for f in fc.find_all_frames("0" * 400) if f.valid] == [])

    print(f"\nresult: {sum(checks)}/{len(checks)} checks passed")
    return 0 if checks and all(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
