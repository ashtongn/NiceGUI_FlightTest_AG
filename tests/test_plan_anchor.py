#!/usr/bin/env python3
"""
Plan anchor: offline
====================================
  1. a plan set with anchor "tx" is re-centred on the TX LO (scenario keyed here
     decodes at any tuning); without the anchor the plan keeps its own center
  2. an RX-only init falls back to the RX centre
  3. the channel configs land at TX LO + offset (plan_adapter)

    python3 tests/test_plan_anchor.py      (or pytest)
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dpmg.radio import sdr_core as sdr  # noqa: E402
from dpmg.radio import dpmg_service as svc  # noqa: E402
from dpmg.scenario.channel_plan import ChannelPlan  # noqa: E402

PLAN = {"schema_version": 1,
        "compose": {"tx_radio": "g40", "sample_rate_hz": 1.92e6, "center_freq_hz": 425.025e6},
        "entries": [{"name": "C01", "modulation": "FSK", "offset_hz": -650e3,
                     "params": {"symbol_rate": 4800}, "role": "known", "decode": True}]}


def _svc(center, tx_lo=None, tx_antenna="TXA1"):
    cls = next(c for c in vars(svc).values() if isinstance(c, type) and hasattr(c, "_plan_for_chan"))
    o = cls.__new__(cls)
    cfg = sdr.SDRConfig(sample_rate=1.92e6, center_freq=center)
    be = sdr.MockBackend(cfg)
    if tx_lo is not None:
        be._tx_lo = tx_lo
    o.backend = be
    o._init_sig = json.dumps({"tx_antenna": tx_antenna} if tx_antenna else {})
    o.plan = PLAN
    o._plan_anchor = None
    return o


def test_unanchored_keeps_center():
    o = _svc(900e6, tx_lo=900e6)
    assert o._plan_for_chan()["compose"]["center_freq_hz"] == 425.025e6


def test_anchor_follows_tx_lo():
    o = _svc(900e6, tx_lo=900e6)
    o._plan_anchor = "tx"
    p = o._plan_for_chan()
    assert p["compose"]["center_freq_hz"] == 900e6
    assert PLAN["compose"]["center_freq_hz"] == 425.025e6       # original untouched
    cp = ChannelPlan.from_dict(p)
    assert cp.center_freq_hz + cp.entries[0].offset_hz == 900e6 - 650e3


def test_anchor_tx_offset_lo():
    o = _svc(900e6, tx_lo=900.1e6)
    o._plan_anchor = "tx"
    assert o._plan_for_chan()["compose"]["center_freq_hz"] == 900.1e6


def test_rx_only_uses_rx_center():
    o = _svc(433e6, tx_lo=900e6, tx_antenna=None)
    o._plan_anchor = "tx"
    assert o._plan_for_chan()["compose"]["center_freq_hz"] == 433e6


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); n += 1
            print(f"ok {name}")
    print(f"{n} passed")
