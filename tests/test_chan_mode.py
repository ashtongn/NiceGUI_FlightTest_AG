"""
NV100 dual-channel decision (sdr_core.needs_dual_channel). Dual mode is required
only when TX uses the A2 handle (transceive on one card: RX TRx1A + TX TRx2A) or
when an explicit extras override asks for it. RX on A2 and TX on A1 stay single.
Deterministic. Exit 0 = all pass.
"""
from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dpmg.radio.sdr_core import needs_dual_channel as nd

checks = []


def chk(name, ok):
    checks.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")


def main():
    print("needs_dual_channel decision\n")
    # RX-only / TX on A1: single
    chk("RX A1 + TX A1 -> single", nd("RXA1", "TXA1", {}) is False)
    chk("RX A2 + TX A1 -> single (RX A2 ok single)", nd("RXA2", "TXA1", {}) is False)
    chk("RX-only default (tx TXA1) -> single", nd("RXA1", "TXA1", {}) is False)
    # transceive on one card: TX on A2: dual
    chk("RX A1 + TX A2 (TRx1A rx + TRx2A tx) -> dual", nd("RXA1", "TXA2", {}) is True)
    chk("RX A2 + TX A2 -> dual", nd("RXA2", "TXA2", {}) is True)
    # explicit override
    chk("extras chan_mode=dual forces dual", nd("RXA1", "TXA1", {"chan_mode": "dual"}) is True)
    chk("extras chan_mode=single leaves TX A1 single",
        nd("RXA1", "TXA1", {"chan_mode": "single"}) is False)

    print(f"\nresult: {sum(checks)}/{len(checks)} checks passed")
    return 0 if checks and all(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
