#!/usr/bin/env python3
"""
J2_TX_PIN: same-card dual TX on a G40 "2" jack (Block 3 F4)
===========================================================
On the G40, RX TRx1A + TX TRx2A on card 0
(dual chan_mode, FIXED ports, TXA2 driven as handle B1, data in the B1 half) reads
the tone at TRx1A at the same level with TRx1A disconnected: on-card coupling,
J2 radiates nothing. The identical handle + packing radiates cross-card (move 2).
Only the same-card path starts a receiver on the transmitting card, and in FIXED
port config a port is RX or TX, never both, while the NV100 maps RxA2/RxB1 to J2
too. J2_TX_PIN assigns the ports explicitly with the public per-handle routing calls.

  1. same-card FDD (TRx1A+TRx2A, TRx1B+TRx2B): RX handle then J1 and TX handle B1 then J2
     after start_rx in open(), and again right before start_tx_streaming; RxB1 is
     moved off J2 when it reads back J2
  2. cross-card pairs (the working path): no routing writes, a read-only diag line
  3. single chan_mode (TDD / TX on TRx1x): untouched
  4. Opt-in: unset / DPMG_J2_TX_PIN=0 = no routing writes (on hardware the pin
     does not cure F4, tx to J2 rc=-95). With =1, a bridge without the calls, a
     fixed-port list without J2, and a refusing write are non-fatal: TX starts
  5. SkiqBridge wrappers call the right libsidekiq symbols and parse the lists

Stdlib + numpy. Exit 0 = all passed.    python3 tests/test_j2_tx_pin.py
"""
import ctypes, os, sys
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
from dpmg.radio import sdr_core as sdr
from dpmg.radio import skiq_bridge as skb
from test_g40_ports import FakeBridge, PORT_MAP

FAILS = []
def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)

J1, J2 = 0, 1
B1 = skb.TX_HDL["TXB1"]
RXA1, RXA2, RXB1 = skb.RX_HDL["RXA1"], skb.RX_HDL["RXA2"], skb.RX_HDL["RXB1"]


class PortBridge(FakeBridge):
    """FakeBridge + the NV100 per-handle port model (SW manual table: RxA2/RxB1
    and TxA2/TxB1 on J2, RxA1/TxA1 on J1) and the J2_TX_PIN bridge calls."""
    RF_PORT_J1 = J1
    RF_PORT_J2 = J2
    tx_fixed = {0: [J1], 1: [J2], 2: [J2]}
    refuse_tx_write = False

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.rx_port = {RXA1: J1, RXA2: J2, RXB1: J2}
        self.tx_port = {0: J1, 1: J2, 2: J2}

    def tx_rf_ports_avail(self, card, hdl):
        return (list(self.tx_fixed.get(hdl, [])), [J1, J2])
    def set_tx_rf_port(self, card, hdl, port):
        self._rec("set_tx_rf_port", card, hdl, port)
        if self.refuse_tx_write:
            raise sdr.SkiqError("write_tx_rf_port_for_hdl failed rc=-22 (Invalid argument)")
        self.tx_port[hdl] = port
    def set_rx_rf_port(self, card, hdl, port):
        self._rec("set_rx_rf_port", card, hdl, port); self.rx_port[hdl] = port
    def read_rx_rf_port(self, card, hdl): return self.rx_port.get(hdl, -1)
    def read_tx_rf_port(self, card, hdl): return self.tx_port.get(hdl, -1)


def _open(rx_port, tx_port, bridge_cls=PortBridge, chan_mode=None, rate=1_920_000):
    rxp, txp = PORT_MAP[rx_port], PORT_MAP[tx_port]
    same = rxp["card"] == txp["card"]
    ex = {"card": rxp["card"], "tx_card": txp["card"],
          "chan_mode": chan_mode or ("dual" if same else "single"),
          "rf_bandwidth_hz": int(rate * 0.8), "tx_center_freq": 425_025_000}
    tx_ant = txp["tx"] if chan_mode != "single" else "TXA1"
    cfg = sdr.SDRConfig(sample_rate=rate, center_freq=425_025_000, rx_gain=245,
                        tx_gain=20.0, rx_antenna=rxp["rx"], tx_antenna=tx_ant,
                        extras=ex).validate()
    sdr.SkiqBridge = lambda: bridge_cls(rate=float(rate))
    sdr._SKIQ_BRIDGE_AVAILABLE = True
    b = sdr.make_backend("g40", cfg, simulate=False)
    b.frontend = None
    b.open()
    return b, FakeBridge.instances[-1]


def _idx(br, name, *args):
    return [i for i, c in enumerate(br.calls) if c[0] == name and c[1:1 + len(args)] == args]


def _key(b):
    b.send(np.zeros(4096, dtype=np.complex64))


def test_same_card():
    print("\n[1] same-card FDD: RX -> J1, TX B1 -> J2, at open and before start_tx")
    for rx, tx, card in (("TRx1A", "TRx2A", 0), ("TRx1B", "TRx2B", 1)):
        b, br = _open(rx, tx)
        check(b._tx_hdl == B1 and b._chan_mode == "dual",
              f"{rx}+{tx}: TX handle B1, card{card} dual")
        start_rx = _idx(br, "start_rx", card)
        tx_pins = _idx(br, "set_tx_rf_port", card, B1, J2)
        rx_pins = _idx(br, "set_rx_rf_port", card, RXA1, J1)
        check(start_rx and tx_pins and tx_pins[0] > start_rx[-1],
              f"{rx}+{tx}: set_tx_rf_port(card{card}, B1, J2) after start_rx in open()")
        check(bool(rx_pins), f"{rx}+{tx}: set_rx_rf_port(card{card}, RXA1, J1)")
        check(bool(_idx(br, "set_rx_rf_port", card, RXB1, J1)),
              f"{rx}+{tx}: RxB1 read back J2 -> moved to J1")
        check(br.tx_port[B1] == J2 and br.rx_port[RXA1] == J1 and br.rx_port[RXB1] == J1,
              f"{rx}+{tx}: final routing RXA1=J1, RXB1=J1, TXB1=J2")
        check(not _idx(br, "set_rf_port_operation", card, True),
              f"{rx}+{tx}: no rf_port_operation flip on the dual path")
        n_open = len(_idx(br, "set_tx_rf_port", card, B1, J2))
        _key(b)
        st = _idx(br, "start_tx", card, B1)
        pins = _idx(br, "set_tx_rf_port", card, B1, J2)
        check(len(pins) == n_open + 1 and st and pins[-1] < st[0],
              f"{rx}+{tx}: re-pinned J2 right before start_tx_streaming")
        check(b._j2_pin.get("where") == "start_tx" and b._j2_pin.get("tx->J2") == "ok",
              f"{rx}+{tx}: pin record {b._j2_pin.get('where')} / tx->J2 {b._j2_pin.get('tx->J2')}")
        _key(b)
        check(len(_idx(br, "set_tx_rf_port", card, B1, J2)) == n_open + 1,
              f"{rx}+{tx}: no re-pin per block while streaming")
        b.stop_tx(); _key(b)
        check(len(_idx(br, "set_tx_rf_port", card, B1, J2)) == n_open + 2,
              f"{rx}+{tx}: re-pinned again on the next key after stop_tx")


def test_cross_card_untouched():
    print("\n[2] cross-card pairs: no routing writes (working path), diag only")
    for rx, tx in (("TRx1A", "TRx2B"), ("TRx1A", "TRx1B"), ("TRx1B", "TRx2A"), ("TRx2B", "TRx2A")):
        b, br = _open(rx, tx)
        _key(b)
        check(not _idx(br, "set_tx_rf_port") and not _idx(br, "set_rx_rf_port"),
              f"{rx} <- {tx}: no set_tx/rx_rf_port calls")
        check(bool(_idx(br, "start_tx")), f"{rx} <- {tx}: start_tx ran")


def test_single_mode_untouched():
    print("\n[3] single chan_mode on one card (TX TRx1x): untouched")
    b, br = _open("TRx1A", "TRx1A", chan_mode="single")
    _key(b)
    check(b._chan_mode == "single", "TRx1A TDD: single chan_mode")
    check(not _idx(br, "set_tx_rf_port") and not _idx(br, "set_rx_rf_port"),
          "TRx1A TDD: no routing writes")
    check(bool(_idx(br, "set_rf_port_operation", 0, True)), "TRx1A TDD: operation flip kept")
    b._kind = "x40"
    check(not b._same_card_fdd(), "X40 never takes the J2_TX_PIN path")


def test_non_fatal():
    print("\n[4] default off / missing calls / J2 not offered / refused write: TX still starts")
    for val in (None, "0"):
        if val is None:
            os.environ.pop("DPMG_J2_TX_PIN", None)
        else:
            os.environ["DPMG_J2_TX_PIN"] = val
        try:
            b, br = _open("TRx1A", "TRx2A"); _key(b)
            tag = "default (unset)" if val is None else f"DPMG_J2_TX_PIN={val}"
            check(not _idx(br, "set_tx_rf_port") and not _idx(br, "set_rx_rf_port")
                  and _idx(br, "start_tx", 0, B1),
                  f"{tag}: opt-in off -> no routing writes, start_tx on B1")
            check(not hasattr(b, "_j2_pin"), f"{tag}: no pin record")
        finally:
            os.environ["DPMG_J2_TX_PIN"] = "1"

    b, br = _open("TRx1A", "TRx2A", bridge_cls=FakeBridge); _key(b)
    check(bool(_idx(br, "start_tx", 0, B1)) and b._j2_pin.get("tx->J2") == "n/a",
          "bridge without the routing calls: n/a, start_tx on B1")

    class NoJ2(PortBridge):
        tx_fixed = {0: [J1], 1: [J2], 2: [J1]}
    b, br = _open("TRx1A", "TRx2A", bridge_cls=NoJ2); _key(b)
    check(not _idx(br, "set_tx_rf_port") and "not offered" in str(b._j2_pin.get("tx->J2")),
          f"J2 not in the fixed list: no write, noted ({b._j2_pin.get('tx->J2')})")
    check(bool(_idx(br, "start_tx", 0, B1)), "J2 not in the fixed list: start_tx on B1")

    class Refuse(PortBridge):
        refuse_tx_write = True
    b, br = _open("TRx1A", "TRx2A", bridge_cls=Refuse); _key(b)
    check(str(b._j2_pin.get("tx->J2", "")).startswith("err:") and _idx(br, "start_tx", 0, B1),
          "refused set_tx_rf_port: logged, start_tx on B1")


class _FakeLib:
    """ctypes-level stand-in for libsidekiq's port calls."""
    def __init__(self):
        self.calls = []
    def skiq_write_tx_rf_port_for_hdl(self, card, hdl, port):
        self.calls.append(("w_tx", card, hdl, port)); return 0
    def skiq_write_rx_rf_port_for_hdl(self, card, hdl, port):
        self.calls.append(("w_rx", card, hdl, port)); return 0
    def skiq_read_tx_rf_ports_avail_for_hdl(self, card, hdl, pnf, fl, pnt, tl):
        pnf._obj.value = 1; fl[0] = J2
        pnt._obj.value = 1; tl[0] = J2
        return 0
    def skiq_read_rx_rf_ports_avail_for_hdl(self, card, hdl, pnf, fl, pnt, tl):
        pnf._obj.value = 2; fl[0] = J2; fl[1] = J1
        pnt._obj.value = 1; tl[0] = J2
        return 0
    def skiq_read_rx_rf_port_for_hdl(self, card, hdl, pp):
        pp._obj.value = J1 if hdl == RXA1 else J2; return 0
    def skiq_read_tx_rf_port_for_hdl(self, card, hdl, pp):
        pp._obj.value = J2; return 0


def test_bridge_wrappers():
    print("\n[5] SkiqBridge port wrappers")
    br = object.__new__(skb.SkiqBridge)
    br.lib = _FakeLib()
    br.set_tx_rf_port(0, B1, br.RF_PORT_J2)
    br.set_rx_rf_port(1, RXA1, br.RF_PORT_J1)
    check(br.lib.calls == [("w_tx", 0, B1, J2), ("w_rx", 1, RXA1, J1)],
          f"write symbols + args {br.lib.calls}")
    check(br.tx_rf_ports_avail(0, B1) == ([J2], [J2]), "tx_rf_ports_avail parses (fixed, trx)")
    check(br.rx_rf_ports_avail(0, RXB1) == ([J2, J1], [J2]), "rx_rf_ports_avail parses (fixed, trx)")
    check(br.read_rx_rf_port(0, RXB1) == J2 and br.read_tx_rf_port(0, B1) == J2, "port readbacks")
    br.lib.skiq_write_tx_rf_port_for_hdl = lambda c, h, p: -22
    try:
        br.set_tx_rf_port(0, B1, J2); ok = False
    except skb.SkiqError as e:
        ok = "rc=-22" in str(e)
    check(ok, "a refused write raises SkiqError with rc")
    del br.lib.skiq_write_tx_rf_port_for_hdl
    br.lib.skiq_write_tx_rf_port_for_hdl = None
    try:
        br.set_tx_rf_port(0, B1, J2); ok = False
    except skb.SkiqError as e:
        ok = "no skiq_write_tx_rf_port_for_hdl" in str(e)
    check(ok, "a lib without the symbol raises a clear SkiqError")


def main():
    # J2_TX_PIN is opt-in (on hardware it does not cure F4);
    # sections 1-4 exercise the opt-in path, section 4 also the default-off one
    os.environ["DPMG_J2_TX_PIN"] = "1"
    test_same_card()
    test_cross_card_untouched()
    test_single_mode_untouched()
    test_non_fatal()
    test_bridge_wrappers()
    print(f"\n{len(FAILS)} failure(s)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
