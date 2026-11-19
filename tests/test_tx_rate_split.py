#!/usr/bin/env python3
"""
Per-card TX sample rate: offline
==================================================
  1. view_sync.eng_fields reports "same" or the TX rate in cross-card mode only
  2. the simulator backend takes a TX rate only when TX is on the other card
  3. the service's TX rate helper falls back to the RX rate

    python3 tests/test_tx_rate_split.py      (or pytest)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dpmg.link import view_sync as vs  # noqa: E402
from dpmg.radio import sdr_core as sdr  # noqa: E402

BASE = {"inited": True, "kind": "g40", "sample_rate": 30.72e6, "center_hz": 425.025e6,
        "rx_antenna": "RXA1", "card": 0, "tx_card": 1, "tx_antenna": "TXA1",
        "chan_mode": "single", "rx_gain": 255, "tx_gain": 20}


def test_eng_fields_txrate():
    f = vs.eng_fields(dict(BASE, tx_sample_rate=1.92e6))
    assert f["mode"] == vs.ENG_CROSS_MODE and f["txrate"] == "1920000", f
    f = vs.eng_fields(dict(BASE, tx_sample_rate=None))
    assert f["txrate"] == "same", f
    f = vs.eng_fields(dict(BASE, tx_sample_rate=30.72e6))
    assert f["txrate"] == "same", f
    same_card = dict(BASE, tx_card=0, tx_antenna="TXB1", chan_mode="dual")
    assert "txrate" not in vs.eng_fields(same_card)


def _mock(extras):
    cfg = sdr.SDRConfig(sample_rate=30.72e6, center_freq=425.025e6)
    cfg.extras = extras
    b = sdr.MockBackend(cfg)
    b.open()
    return b


def test_mock_backend_tx_rate():
    assert _mock({"card": 0, "tx_card": 1, "tx_sample_rate": 1.92e6})._tx_rate == 1.92e6
    assert _mock({"card": 0, "tx_card": 0, "tx_sample_rate": 1.92e6})._tx_rate is None
    assert _mock({"card": 0, "tx_card": 1})._tx_rate is None


def test_service_tx_fs():
    from dpmg.radio import dpmg_service as svc
    cls = next(c for c in vars(svc).values()
               if isinstance(c, type) and hasattr(c, "_tx_fs"))
    o = cls.__new__(cls)
    o.backend = _mock({"card": 0, "tx_card": 1, "tx_sample_rate": 1.92e6})
    assert cls._tx_fs(o) == 1.92e6
    o.backend = _mock({"card": 0, "tx_card": 1})
    assert cls._tx_fs(o) == 30.72e6
    o.backend = None
    assert cls._tx_fs(o) == 0.0


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); n += 1
            print(f"ok {name}")
    print(f"{n} passed")
