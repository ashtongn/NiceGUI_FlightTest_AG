#!/usr/bin/env python3
"""
view_sync.py rules: offline
=========================
  1. EditGuard: focus blocks, an edit holds until the service's value for that
     field changes or HOLD_S passes unfocused; other fields never release it
  2. op_fields: service get_config: operator console values (ports reverse-
     mapped to TRx jacks, gain index, nearest TX-atten key, clock, centre MHz)
  3. eng_fields: engineering values (card/handle, mode label from tx handle +
     chan_mode, PSD shaping, TX waveform tone/file)
  4. a closed radio / an older service reports nothing: no widget is touched
  5. pending_labels flags held edits that differ from the radio

    python3 tests/test_view_sync.py      (or pytest)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dpmg.link import view_sync as vs  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


CFG = {"inited": True, "kind": "g40", "center_hz": 430_500_000.0, "tx_center_hz": 430_500_000.0,
       "sample_rate": 7_680_000.0, "achieved_rate": 7_680_000.0, "clock_source": "gpsdo",
       "rx_antenna": "RXA1", "tx_antenna": "TXA1", "card": 0, "tx_card": 1, "chan_mode": "single",
       "rx_gain": 220.0, "tx_gain": 10.0,
       "psd": {"nfft": 4096, "window": "hann", "buffer": 65536},
       "tx_wf": {"mode": "tone", "offset_hz": 250_000.0, "ampl": 0.5, "desc": "tone"},
       "log": {"detections": "/app_svc/detections.csv", "telemetry": None},
       "ant_bias": True}


def test_guard():
    c = Clock()
    g = vs.EditGuard(hold_s=20, clock=c)
    assert g.may_apply("center", 1.0)
    g.focus("center")
    assert not g.may_apply("center", 2.0), "focused field must not be overwritten"
    g.edited("center", 1.0)
    g.focus("center", False)
    assert not g.may_apply("center", 1.0), "fresh edit is held while the radio is unchanged"
    c.t += 5
    assert not g.may_apply("center", 1.0)
    assert g.held("center")
    assert g.may_apply("center", 3.0), "service value changed -> follow the radio"
    assert not g.held("center")
    # hold expiry
    g.edited("rate", 30.72e6)
    c.t += 19
    assert not g.may_apply("rate", 30.72e6)
    c.t += 2
    assert g.may_apply("rate", 30.72e6), "hold expires after HOLD_S unfocused"
    # an edit on one field is never released by another field's change
    g.edited("rate", 30.72e6)
    assert g.may_apply("gain", 230)
    assert not g.may_apply("rate", 30.72e6)
    # the hold window restarts when focus leaves
    g.focus("atten")
    g.edited("atten", "20")
    c.t += 100
    assert not g.may_apply("atten", "20"), "still focused"
    g.focus("atten", False)
    assert not g.may_apply("atten", "20"), "hold restarts at blur"


def test_op_fields():
    f = vs.op_fields(CFG)
    assert f == {"center_mhz": 430.5, "rate": 7_680_000.0, "clk": "gpsdo", "rx_port": "TRx1A",
                 "tx_port": "TRx1B", "rx_gain": "220", "tx_atten": "10"}, f
    # J2 TX handles map to TRx2, card 1: B
    f = vs.op_fields(dict(CFG, rx_antenna="RXA1", card=1, tx_antenna="TXB1", tx_card=0))
    assert f["rx_port"] == "TRx1B" and f["tx_port"] == "TRx2A", f
    f = vs.op_fields(dict(CFG, tx_antenna="TXA2", tx_card=None, card=1))
    assert f["tx_port"] == "TRx2B", f
    # nearest atten key, quarter-dB and the 41.75 max
    assert vs.atten_key(41.75) == "41.75" and vs.atten_key(19.5) == "20" and vs.atten_key(0) == "0"
    # x40 ports are the handles themselves
    f = vs.op_fields(dict(CFG, kind="x40", rx_antenna="RXB1", tx_antenna="TXA1"))
    assert f["rx_port"] == "RXB1" and f["tx_port"] == "TXA1"


def test_eng_fields():
    f = vs.eng_fields(CFG)
    assert f["clk"] == "gpsdo" and f["rate"] == 7_680_000.0 and f["cf"] == "430500000"
    assert f["card"] == 0 and f["txcard"] == 1 and f["rxant"] == "RXA1"
    # RX card0 + TX card1 is the cross-card transceive mode, TX port TRx1B
    assert f["mode"] == vs.ENG_CROSS_MODE and f["txant"] == "TXA1"
    assert f["gain"] == "220" and f["txatten"] == "10"
    assert f["nfft"] == "4096" and f["win"] == "hann" and f["buf"] == "65536"
    assert f["tx_kind"] == "tone" and f["tx_off_khz"] == 250.0 and f["tx_ampl"] == 0.5
    # current label kept when it maps to the same params; FDD for TXB1 dual
    assert vs.eng_mode(dict(CFG, tx_card=0), "Transceive TDD same-port (TRx1)") == "Transceive TDD same-port (TRx1)"
    assert vs.eng_mode(dict(CFG, tx_card=0, tx_antenna="TXB1", chan_mode="dual")) == \
        "Transceive FDD (RX TRx1 + TX TRx2)"
    assert vs.eng_mode(dict(CFG, tx_antenna=None)) == "RX only"
    assert vs.eng_mode(dict(CFG, tx_antenna="TXA2", chan_mode="single")) in vs.ENG_MODE_PARAMS
    # same card again: the single-card labels
    assert vs.eng_mode(dict(CFG, tx_card=0)) == "TX only: TRx1 (TxA1)"
    assert vs.eng_fields(dict(CFG, tx_antenna="TXA2"))["txant"] == "TXA2"   # TRx2B
    f = vs.eng_fields(dict(CFG, tx_wf={"mode": "loader", "path": "data/waveforms/x.iq.bin"}))
    assert f["tx_kind"] == "file" and f["tx_file"] == "data/waveforms/x.iq.bin"


def test_closed_and_old():
    assert vs.op_fields({}) == {} and vs.eng_fields(None or {}) == {}
    assert vs.op_fields({"inited": False, "clock_source": "gpsdo"}) == {"clk": "gpsdo"}
    assert "off" in vs.log_text({}) and "detections" in vs.log_text(CFG)
    assert "none" in vs.tx_wf_text({})


def test_pending():
    c = Clock()
    g = vs.EditGuard(clock=c)
    g.edited("rate", 7.68e6)
    lbl = vs.pending_labels(g, {"rate": 30_720_000, "center_mhz": 430.5},
                            {"rate": 7_680_000.0, "center_mhz": 430.5},
                            {"rate": "Sample rate", "center_mhz": "Center"})
    assert lbl == ["Sample rate"], lbl
    assert vs.pending_labels(g, {"rate": 7680000}, {"rate": 7.68e6}, {"rate": "x"}) == []


def main():
    fails = 0
    for fn in (test_guard, test_op_fields, test_eng_fields, test_closed_and_old, test_pending):
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            fails += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{fails} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
