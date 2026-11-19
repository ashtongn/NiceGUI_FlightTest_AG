#!/usr/bin/env python3
"""
G40 same-card reverse-role rule
================================================
Each NV100 card has two shared RX/TX jacks, J1 (TRx1x) and J2 (TRx2x). Single-card
full duplex (dual chan_mode) only works in the libsidekiq FIXED RF-port mode, which
locks J1 = RX (RXA1) and J2 = TX (TXB1 / operator-console TXA2); TRX mode flips both
jacks together (TDD). So RX on the "2" jack with TX on the "1" jack of the same card
can never co-stream (test-plan rows PORT-G-06 / PORT-G-12).

  1. the shared port-name rule (sdr_core.g40_same_card_conflict) flags exactly the
     same-card TRx2x: TRx1x pairs, and nothing else (same-SMA, normal FDD,
     every cross-card pair)
  2. the handle form (sdr_core.g40_fixed_mode_conflict) flags dual chan_mode +
     RXA2 + TXA1/TXB1 on one card, leaves single chan_mode (TDD) and cross-card
     pairs alone, and says the same thing
  3. the real RadioService (kind g40, MockBackend) rejects the reverse-role dual
     init with g40_same_card=True and the rule text, before tearing down a
     running session; normal single-card FDD (RXA1+TXB1 / RXA1+TXA2), TDD
     same-port and cross-card inits are accepted
  4. an X40 is never affected
  5. XCARD_A2_DUAL (Block 3 moves 2/6/8/12 = PORT-G-03/05/08/11): the
     real EpiqBackend open paths against a recording fake bridge that enforces
     libsidekiq's rule (TX on the A2 handle streams only in dual chan_mode):
     the four cross-card "2"-jack TX pairs open the TX card in dual mode with
     dual TX packing and leave the RX card single; the four cross-card A1-TX
     pairs and the two same-card FDD pairs are unchanged; dual-unavailable is a
     hard init failure
  6. TX_START_ONCE: a refused start_tx_streaming is raised, not swallowed, the
     scheduler TX thread gives up after tx_max_consecutive_errors tries, and the
     service's start_tx ack carries the libsidekiq reason without the RT hint

Stdlib + numpy. Exit 0 = all passed.    python3 tests/test_g40_ports.py
"""
import asyncio, os, sys, threading, time
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dpmg.radio import sdr_core as sdr
from dpmg.link.proto import CmdStatus, HostType
from dpmg.radio.dpmg_service import RadioService

FAILS = []
def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)

# the operator console's port map (link_tab._PORT_MAP): port: card + handles
PORT_MAP = {
    "TRx1A": {"card": 0, "rx": "RXA1", "tx": "TXA1"},
    "TRx2A": {"card": 0, "rx": "RXA2", "tx": "TXA2"},
    "TRx1B": {"card": 1, "rx": "RXA1", "tx": "TXA1"},
    "TRx2B": {"card": 1, "rx": "RXA2", "tx": "TXA2"},
}

def test_rule():
    print("[1] shared port-name rule")
    for rx, tx in (("TRx2A", "TRx1A"), ("TRx2B", "TRx1B"), ("trx2a", "trx1a")):
        check(sdr.g40_same_card_conflict(rx, tx) is not None, f"{rx} <- {tx} conflicts (reverse roles)")
    for rx, tx in (("TRx1A", "TRx2A"), ("TRx1B", "TRx2B")):
        check(sdr.g40_same_card_conflict(rx, tx) is None, f"{rx} <- {tx} is the supported single-card FDD")
    for rx in PORT_MAP:
        for tx in PORT_MAP:
            if PORT_MAP[rx]["card"] != PORT_MAP[tx]["card"]:
                check(sdr.g40_same_card_conflict(rx, tx) is None, f"{rx} <- {tx} cross-card is clean")
        check(sdr.g40_same_card_conflict(rx, rx) is None, f"{rx} <- {rx} same-SMA is a different rule")
    check(sdr.g40_same_card_conflict("TRx2A", None) is None, "no TX port -> no conflict")
    check(sdr.g40_same_card_conflict("RXB1", "TXA1") is None, "X40 handles never match")
    msg = sdr.g40_same_card_conflict("TRx2B", "TRx1B")
    check("FIXED" in msg and "J1" in msg and "J2" in msg, "message names FIXED mode and the J1/J2 lock")
    check("TRx1B + TX TRx2B" in msg and "cross-card" in msg, "message names the supported FDD and the cross-card way out")

    print("[2] shared handle rule (engineering view / service)")
    for tx in ("TXA1", "TXB1", "txb1"):
        check(sdr.g40_fixed_mode_conflict("RXA2", tx, "dual", 0) is not None, f"dual RXA2+{tx} card0 conflicts")
    check(sdr.g40_fixed_mode_conflict("RXA2", "TXA1", "dual", 1, 1) is not None, "dual RXA2+TXA1 card1 conflicts")
    check(sdr.g40_fixed_mode_conflict("RXA2", "TXA1", "single", 0) is None, "single chan_mode (TDD / TRX port mode) untouched")
    check(sdr.g40_fixed_mode_conflict("RXA1", "TXB1", "dual", 0) is None, "normal FDD RXA1+TXB1 is clean")
    check(sdr.g40_fixed_mode_conflict("RXA1", "TXA2", "dual", 0) is None, "operator-console FDD RXA1+TXA2 is clean")
    check(sdr.g40_fixed_mode_conflict("RXA2", "TXA1", "dual", 0, 1) is None, "cross-card RXA2@0 + TXA1@1 is clean")
    check(sdr.g40_fixed_mode_conflict("RXA2", None, "dual", 0) is None, "no TX handle -> no conflict")
    h = sdr.g40_fixed_mode_conflict("RXA2", "TXA1", "dual", 1)
    check(h.split("cannot co-stream", 1)[1] == msg.split("cannot co-stream", 1)[1],
          "handle form says the same thing as the port-name form")
    check(sdr.g40_trx_port("RXA2", 1) == "TRx2B" and sdr.g40_trx_port("TXB1", 0) == "TRx2A"
          and sdr.g40_trx_port("TXA1", 0) == "TRx1A", "handle -> connector table")

def _svc(kind, host_type):
    return RadioService(serial=f"{kind.upper()}-SIM", host_type=host_type, kind=kind,
                        simulate=True, cmd_port=47892, discovery_port=47893,
                        broadcast="127.0.0.1")

def _init_p(rx, tx, card=0, tx_card=None, chan_mode="dual", kind="g40"):
    ex = {"card": card, "chan_mode": chan_mode, "rf_bandwidth_hz": 1_536_000,
          "tx_center_freq": 425_025_000}
    if tx_card is not None:
        ex["tx_card"] = tx_card
    p = {"kind": kind, "sample_rate": 1_920_000, "center_freq": 425_025_000,
         "rx_gain": 245, "tx_gain": 20.0, "clock_source": "internal",
         "rx_antenna": rx, "extras": ex}
    if tx is not None:
        p["tx_antenna"] = tx
    return p

async def _service_cases():
    print("[3] G40 service: reverse-role dual init is REJECTED")
    svc = _svc("g40", HostType.G40_RX)
    svc.loop = asyncio.get_running_loop()
    cid = 1
    # a good session first, so the refusal can be shown to leave it running
    ack = await svc._cmd_init(cid, _init_p("RXA1", "TXA2")); cid += 1
    check(ack.status == CmdStatus.OK, f"operator-console FDD RXA1+TXA2 (TRx1A <- TRx2A) init OK ({ack.result})")
    live = svc.backend
    for rx, tx, card, label in (("RXA2", "TXA1", 0, "operator console TRx2A <- TRx1A (PORT-G-06)"),
                                ("RXA2", "TXA1", 1, "operator console TRx2B <- TRx1B (PORT-G-12)"),
                                ("RXA2", "TXB1", 0, "engineering FDD mode with RX RXA2")):
        a = await svc._cmd_init(cid, _init_p(rx, tx, card)); cid += 1
        check(a.status == CmdStatus.REJECTED and (a.result or {}).get("g40_same_card"),
              f"{label} REJECTED with g40_same_card ({a.status})")
        check("FIXED" in str((a.result or {}).get("err")), f"{label} err names the FIXED-mode rule")
    check(svc.backend is live, "a refused init leaves the running session untouched")

    print("[4] G40 service: supported pairs are accepted")
    for rx, tx, card, tx_card, cm, label in (
            ("RXA1", "TXB1", 0, None, "dual", "engineering FDD RXA1+TXB1 card0"),
            ("RXA1", "TXA2", 1, None, "dual", "operator FDD TRx1B <- TRx2B"),
            ("RXA1", "TXA1", 0, None, "single", "TDD same-port TRx1A"),
            ("RXA2", "TXA1", 0, 1, "single", "cross-card TRx2A <- TRx1B"),
            ("RXA2", "TXA2", 0, 1, "single", "cross-card TRx2A <- TRx2B"),
            ("RXA1", "TXA1", 1, 0, "single", "cross-card TRx1B <- TRx1A"),
            ("RXA2", None, 0, None, "single", "RX only on RXA2")):
        a = await svc._cmd_init(cid, _init_p(rx, tx, card, tx_card, cm)); cid += 1
        check(a.status == CmdStatus.OK and not (a.result or {}).get("g40_same_card"),
              f"{label} init OK ({a.status}, {(a.result or {}).get('err', '')[:50]})")
    await svc._cmd_close(cid); cid += 1

    print("[5] X40 never affected")
    x = _svc("x40", HostType.X40_TX)
    x.loop = asyncio.get_running_loop()
    p = _init_p("RXA2", "TXA1", kind="x40"); p["sample_rate"] = 50_000_000
    p["extras"] = {"card": 0, "tx_center_freq": 425_025_000}
    a = await x._cmd_init(cid, p); cid += 1
    check(a.status == CmdStatus.OK and not (a.result or {}).get("g40_same_card"),
          f"X40 RXA2+TXA1 init not refused by the G40 rule ({a.status})")
    await x._cmd_close(cid)

# [6] XCARD_A2_DUAL
from dpmg.radio import skiq_bridge as _skiqmod

class FakeBridge:
    """Recording stand-in for skiq_bridge.SkiqBridge. Enforces the one libsidekiq
    rule under test: start_tx_streaming on the A2 handle (1) of a card in single
    chan_mode fails rc=-95 (EOPNOTSUPP), as libsidekiq 4.25 does on the
    G40. Everything else succeeds and is logged in .calls."""
    RF_PORT_CONFIG_TRX = 0
    RF_PORT_CONFIG_FIXED = 1
    REFUSE = "start_tx_streaming failed rc=-95 (Operation not supported)"
    instances = []

    def __init__(self, rate=1_920_000.0, dual_available=True):
        self.calls = []
        self.chan_mode = {}
        self.rate = rate
        self.dual_available = dual_available
        self.exited = False
        FakeBridge.instances.append(self)

    def _rec(self, name, *a):
        self.calls.append((name,) + tuple(a))

    def init(self, card=0): self._rec("init", card); self.chan_mode[card] = "single"
    def init_cards(self, cards):
        self._rec("init_cards", tuple(cards))
        for c in cards: self.chan_mode[c] = "single"
    def lib_version(self): return "4.25.0-fake"
    def set_ref_clock(self, card, clock): self._rec("set_ref_clock", card, clock); return clock
    def set_rx_rate_bw_multi(self, card, hdls, rates, bws):
        self._rec("set_rx_rate_bw_multi", card, tuple(hdls), tuple(rates))
    def set_chan_mode(self, card, dual):
        self._rec("set_chan_mode", card, bool(dual))
        if dual and not self.dual_available:
            raise sdr.SkiqError("write_chan_mode failed rc=-22 (Invalid argument)")
        self.chan_mode[card] = "dual" if dual else "single"
        return self.chan_mode[card]
    def read_chan_mode(self, card): return self.chan_mode.get(card, "single")
    def set_rx_rate_bw(self, card, hdl, rate, bw): self._rec("set_rx_rate_bw", card, hdl, rate)
    def set_rf_port_config(self, card, cfg): self._rec("set_rf_port_config", card, cfg)
    def set_rf_port_operation(self, card, tx): self._rec("set_rf_port_operation", card, bool(tx))
    def set_rx_lo(self, card, hdl, hz): self._rec("set_rx_lo", card, hdl, hz)
    def rx_gain_index_range(self, card, hdl): return (0, 76)
    def set_rx_gain_mode(self, card, hdl, manual): self._rec("set_rx_gain_mode", card, hdl)
    def read_rx_gain_mode(self, card, hdl): return "manual"
    def set_rx_gain_index(self, card, hdl, ix): self._rec("set_rx_gain_index", card, hdl, ix)
    def apply_rx_cal(self, card, hdl): self._rec("apply_rx_cal", card, hdl)
    def set_iq_order(self, card, iq): self._rec("set_iq_order", card)
    def read_iq_order(self, card): return "QI"
    def read_rx_rate_range(self, card, hdl): return (233_000, 61_440_000)
    def read_rx_sample_rate(self, card, hdl): return self.rate
    def set_tx_lo(self, card, hdl, hz): self._rec("set_tx_lo", card, hdl, hz)
    def set_tx_rate_bw(self, card, hdl, rate, bw): self._rec("set_tx_rate_bw", card, hdl, rate)
    def set_tx_attenuation(self, card, hdl, q): self._rec("set_tx_attenuation", card, hdl, q)
    def configure_tx(self, card, hdl, block_samples=None):
        self._rec("configure_tx", card, hdl, getattr(self, "tx_block_samples", None))
    def write_rx_block_size(self, card, hdl, words): self._rec("write_rx_block_size", card, hdl, words)
    def start_rx(self, card, hdl): self._rec("start_rx", card, hdl)
    def stop_rx(self, card, hdl): self._rec("stop_rx", card, hdl)
    def rf_port_diag(self, card, rx, tx): return "fake"
    def read_lo(self, card, rx, tx): return "fake"
    def start_tx(self, card, hdl):
        self._rec("start_tx", card, hdl)
        # libsidekiq 4.25 on the NV100: A2 (1) and B1 (2) stream only in dual
        if hdl in (_skiqmod.TX_HDL["TXA2"], _skiqmod.TX_HDL["TXB1"]) and self.chan_mode.get(card) != "dual":
            raise sdr.SkiqError(self.REFUSE)
    def stop_tx(self, card, hdl): self._rec("stop_tx", card, hdl)
    def prepare_tx(self, samples, iq_order_iq): pass
    def transmit(self, card, hdl, samples, iq_order_iq): self._rec("transmit", card, hdl, len(samples))
    def tx_underruns(self, card, hdl): return None
    def exit(self): self.exited = True; self._rec("exit")


def _open_backend(rx_port, tx_port, rate=1_920_000, **bridge_kw):
    """Open the real EpiqBackend (kind g40) with the operator console's init for
    the pair (link_tab / checkout_common.port_init_params mapping), on a fresh
    FakeBridge. Returns (backend, bridge)."""
    rxp, txp = PORT_MAP[rx_port], PORT_MAP[tx_port]
    ex = {"card": rxp["card"], "tx_card": txp["card"],
          "chan_mode": "dual" if rxp["card"] == txp["card"] else "single",
          "rf_bandwidth_hz": int(rate * 0.8), "tx_center_freq": 425_025_000}
    cfg = sdr.SDRConfig(sample_rate=rate, center_freq=425_025_000, rx_gain=245,
                        tx_gain=20.0, rx_antenna=rxp["rx"], tx_antenna=txp["tx"],
                        extras=ex).validate()
    sdr.SkiqBridge = lambda: FakeBridge(rate=float(rate), **bridge_kw)
    sdr._SKIQ_BRIDGE_AVAILABLE = True
    b = sdr.make_backend("g40", cfg, simulate=False)
    b.frontend = None
    b.open()
    return b, FakeBridge.instances[-1]


def _names(bridge, name):
    return [c for c in bridge.calls if c[0] == name]


def test_xcard_a2_dual():
    print("[6] XCARD_A2_DUAL: EpiqBackend open paths on a fake bridge")
    dual_blk = _skiqmod.tx_block_for_rate(1_920_000, dual=True)
    single_blk = _skiqmod.tx_block_for_rate(1_920_000)
    check(dual_blk == 6142 and single_blk == 8188, f"block sizes dual {dual_blk} / single {single_blk}")
    wave = np.zeros(4096, dtype=np.complex64)

    # the four cross-card "2"-jack TX pairs: moves 2, 6, 8, 12
    a2_pairs = (("TRx1A", "TRx2B", "PORT-G-03 / move 2"), ("TRx2A", "TRx2B", "PORT-G-05 / move 6"),
                ("TRx1B", "TRx2A", "PORT-G-08 / move 8"), ("TRx2B", "TRx2A", "PORT-G-11 / move 12"))
    for rx, tx, label in a2_pairs:
        b, br = _open_backend(rx, tx)
        rxc, txc = PORT_MAP[rx]["card"], PORT_MAP[tx]["card"]
        cm = _names(br, "set_chan_mode")
        check(cm == [("set_chan_mode", txc, True)], f"{label} {rx} <- {tx}: TX card{txc} set dual, RX card{rxc} untouched ({cm})")
        check(br.chan_mode == {rxc: "single", txc: "dual"}, f"{label}: bridge modes {br.chan_mode}")
        check(b._chan_mode == "single" and b._tx_chan_mode == "dual", f"{label}: backend reports RX single / TX dual")
        check(br.tx_dual is True and br.tx_block_samples == dual_blk, f"{label}: dual TX packing, block {br.tx_block_samples}")
        multi = _names(br, "set_rx_rate_bw_multi")
        check(multi == [("set_rx_rate_bw_multi", txc, (0, 1), (1_920_000, 1_920_000))], f"{label}: multi-handle rate write on the TX card only ({multi})")
        order = [c[0] for c in br.calls if c[0] in ("set_rx_rate_bw_multi", "set_chan_mode")]
        check(order == ["set_rx_rate_bw_multi", "set_chan_mode"], f"{label}: rate written before the mode switch ({order})")
        check(("set_rf_port_config", txc, FakeBridge.RF_PORT_CONFIG_FIXED) in br.calls, f"{label}: TX card in FIXED port config (J2 = TX)")
        check(("set_rf_port_config", rxc, FakeBridge.RF_PORT_CONFIG_TRX) in br.calls and ("set_rf_port_operation", rxc, False) in br.calls, f"{label}: RX card TRX + operation RX as before")
        check(not [c for c in _names(br, "set_tx_rate_bw") if c[1] == txc], f"{label}: no per-handle TX rate write under dual (rc=-71)")
        ctx = sorted((c[1], c[2], c[3]) for c in _names(br, "configure_tx"))
        check(ctx == [(txc, 0, dual_blk), (txc, 2, dual_blk)], f"{label}: dual block size on A1 and B1 ({ctx})")
        check(b._tx_hdl == 2, f"{label}: console TXA2 drives skiq handle B1 (G40_J2_TXB1), got {b._tx_hdl}")
        check(_names(br, "start_rx") == [("start_rx", rxc, PORT_MAP[rx]["rx"] == "RXA2" and 1 or 0)], f"{label}: RX streaming on card{rxc}")
        # key it: the fake lib accepts A2 only in dual, so this is the rc=-95 case
        b.send(wave)
        check(_names(br, "start_tx") == [("start_tx", txc, 2)] and b._tx_streaming and not b.health.last_error,
              f"{label}: start_tx_streaming(card{txc}, B1) accepted, no error ({b.health.last_error!r})")
        check(not [c for c in _names(br, "set_rf_port_operation") if c[1] == txc], f"{label}: no operation flip on the FIXED-mode TX card")
        check(_names(br, "transmit") == [("transmit", txc, 2, 4096)], f"{label}: samples went to card{txc} handle B1")

    # the four cross-card A1-TX pairs: unchanged single-mode TRX path
    a1_pairs = (("TRx1A", "TRx1B", "PORT-G-01 / move 1"), ("TRx2A", "TRx1B", "PORT-G-04 / move 4"),
                ("TRx1B", "TRx1A", "PORT-G-07 / move 7"), ("TRx2B", "TRx1A", "PORT-G-10 / move 10"))
    for rx, tx, label in a1_pairs:
        b, br = _open_backend(rx, tx)
        rxc, txc = PORT_MAP[rx]["card"], PORT_MAP[tx]["card"]
        check(not _names(br, "set_chan_mode") and not _names(br, "set_rx_rate_bw_multi"), f"{label} {rx} <- {tx}: both cards stay single, no multi write")
        check(b._chan_mode == "single" and b._tx_chan_mode == "single", f"{label}: backend reports single / single")
        check(br.tx_dual is False and br.tx_block_samples == single_blk, f"{label}: single TX packing, block {br.tx_block_samples}")
        check(("set_rf_port_config", txc, FakeBridge.RF_PORT_CONFIG_TRX) in br.calls, f"{label}: TX card TRX port config")
        check(("set_tx_rate_bw", txc, 0, 1_920_000) in br.calls, f"{label}: per-handle TX rate write kept")
        check([(c[1], c[2]) for c in _names(br, "configure_tx")] == [(txc, 0)], f"{label}: configure_tx on A1 only")
        b.send(wave)
        check(("set_rf_port_operation", txc, True) in br.calls and _names(br, "start_tx") == [("start_tx", txc, 0)] and not b.health.last_error,
              f"{label}: lazy operation flip -> TX then start_tx(card{txc}, A1)")

    # the two same-card FDD pairs: the RF-verified single-card dual path, untouched
    for rx, tx, label in (("TRx1A", "TRx2A", "PORT-G-02 / move 3"), ("TRx1B", "TRx2B", "PORT-G-09 / move 9")):
        b, br = _open_backend(rx, tx)
        c = PORT_MAP[rx]["card"]
        check(_names(br, "init") == [("init", c)] and not _names(br, "init_cards"), f"{label} {rx} <- {tx}: single-card init")
        check(_names(br, "set_chan_mode") == [("set_chan_mode", c, True)] and b._chan_mode == "dual", f"{label}: card{c} dual")
        check(getattr(b, "_tx_chan_mode", None) is None and b._tx_card_dual(), f"{label}: TX follows the card's mode (no separate TX mode)")
        check(br.tx_dual is True and br.tx_block_samples == dual_blk, f"{label}: dual TX packing")
        check(("set_rf_port_config", c, FakeBridge.RF_PORT_CONFIG_FIXED) in br.calls, f"{label}: FIXED port config")
        b.send(wave)
        check(_names(br, "start_tx") == [("start_tx", c, 2)] and not [x for x in _names(br, "set_rf_port_operation") if x[2]] and not b.health.last_error,
              f"{label}: start_tx(card{c}, B1 via console TXA2), no operation flip")

    # engineering-view name TXB1 cross-card (not reachable from the console, but the service accepts it)
    rxp = PORT_MAP["TRx1A"]
    ex = {"card": 0, "tx_card": 1, "chan_mode": "single", "rf_bandwidth_hz": 1_536_000, "tx_center_freq": 425_025_000}
    cfg = sdr.SDRConfig(sample_rate=1_920_000, center_freq=425_025_000, rx_gain=245, tx_gain=20.0,
                        rx_antenna="RXA1", tx_antenna="TXB1", extras=ex).validate()
    sdr.SkiqBridge = lambda: FakeBridge(rate=1_920_000.0)
    b = sdr.make_backend("g40", cfg, simulate=False); b.frontend = None; b.open(); br = FakeBridge.instances[-1]
    check(_names(br, "set_chan_mode") == [("set_chan_mode", 1, True)] and b._tx_hdl == 2 and br.tx_dual,
          "cross-card TXB1 by name also opens card1 dual on handle B1")

    # X40 keeps TXB1 = J6 in single mode (the alias and the cross-card rule are G40-only)
    xcfg = sdr.SDRConfig(sample_rate=50_000_000, center_freq=425_025_000, rx_gain=10, tx_gain=20.0,
                         rx_antenna="RXB1", tx_antenna="TXB1", extras={"card": 0}).validate()
    sdr.SkiqBridge = lambda: FakeBridge(rate=50_000_000.0)
    xb = sdr.make_backend("x40", xcfg, simulate=False); xb.frontend = None; xb.open(); xbr = FakeBridge.instances[-1]
    check(not _names(xbr, "set_chan_mode") and xb._tx_hdl == 2 and xbr.tx_dual is False, "X40 RXB1+TXB1 stays single on handle B1")
    xcfg2 = sdr.SDRConfig(sample_rate=50_000_000, center_freq=425_025_000, rx_gain=10, tx_gain=20.0,
                          rx_antenna="RXB1", tx_antenna="TXA2", extras={"card": 0}).validate()
    sdr.SkiqBridge = lambda: FakeBridge(rate=50_000_000.0)
    xb2 = sdr.make_backend("x40", xcfg2, simulate=False); xb2.frontend = None; xb2.open()
    check(xb2._tx_hdl == 1, "X40 TXA2 is not aliased (G40_J2_TXB1 is NV100-only)")

    # dual unavailable on the TX card = hard init failure (not a silent single-mode TX)
    try:
        _open_backend("TRx1A", "TRx2B", dual_available=False)
        check(False, "dual unavailable: open must raise")
    except sdr.SDRConnectionError as e:
        br = FakeBridge.instances[-1]
        check("dual channel mode required on card1" in str(e) and "TRx2B" in str(e) and br.exited,
              f"dual unavailable: SDRConnectionError names card1/TRx2B and the lib is released ({e})")


def test_dual_packing():
    print("[6b] DUAL_TX_SECOND_HALF: dual TX blocks carry the data in the B1/A2 half")
    br = _skiqmod.SkiqBridge.__new__(_skiqmod.SkiqBridge)      # no lib needed for packing
    hdr16 = _skiqmod.TX_HEADER_BYTES // 2
    n = 6142 * 2 + 100                                           # 3 dual blocks, last one padded
    s = (np.arange(n, dtype=np.float32) / n * 0.5 + 0.1j * np.ones(n, dtype=np.float32)).astype(np.complex64)
    br.tx_dual, br.tx_block_samples = True, 6142
    blk = br._prepare_tx_blocks(s, True)
    check(blk.shape == (3, hdr16 + 4 * 6142) and blk.dtype == np.int16, f"3 dual blocks of {hdr16}+4*6142 int16 ({blk.shape})")
    first = blk[:, hdr16:hdr16 + 2 * 6142]; second = blk[:, hdr16 + 2 * 6142:]
    check(not np.any(first), "A1 half (first 2*step int16) is all zeros")
    i16 = np.clip(np.round(s.real * _skiqmod.TX_FULL_SCALE), -32768, 32767).astype(np.int16)
    q16 = np.clip(np.round(s.imag * _skiqmod.TX_FULL_SCALE), -32768, 32767).astype(np.int16)
    flat = second.reshape(-1)
    check(np.array_equal(flat[0:2 * n:2], i16) and np.array_equal(flat[1:2 * n:2], q16),
          "B1 half holds every sample ONCE, in order, I/Q interleaved (no [s0,s0,s1,s1] doubling)")
    check(not np.any(flat[2 * n:]), "zero padding after the last sample")
    check(not np.any(blk[:, :hdr16]), "header words zeroed")
    br.tx_dual, br.tx_block_samples = False, 8188
    sb = br._prepare_tx_blocks(s, True)
    check(sb.shape == (2, hdr16 + 2 * 8188) and np.array_equal(sb[:, hdr16:].reshape(-1)[0:2 * n:2], i16),
          "single-mode packing unchanged (data right after the header)")


def test_tx_start_once():
    print("[7] TX_START_ONCE: refused start_tx is raised once per try and bounded")
    # (a) EpiqBackend.send on a card libsidekiq refuses: raise with the lib's reason
    b, br = _open_backend("TRx1A", "TRx2B")
    br.chan_mode[1] = "single"              # emulate the pre-fix card state
    wave = np.zeros(4096, dtype=np.complex64)
    try:
        b.send(wave); check(False, "refused start_tx must raise")
    except sdr.SDRStreamError as e:
        check("rc=-95" in str(e) and "Cannot stream on handle B1" in str(e) and "dual chan_mode" in str(e),
              f"send raises SDRStreamError with the libsidekiq reason ({str(e)[:90]}...)")
        check(b.health.last_error == str(e) and not getattr(b, "_tx_streaming", False), "health.last_error carries it; TX not marked streaming")
    check(len(_names(br, "start_tx")) == 1 and not _names(br, "transmit"), "exactly one start_tx attempt per send, no transmit")

    # (b) the scheduler TX thread: bounded retries, then it gives up
    cfg = sdr.SDRConfig(sample_rate=1_920_000, center_freq=425e6, rx_gain=10, tx_gain=20.0,
                        rx_antenna="RXA1", tx_antenna="TXA1", extras={}).validate()
    mock = sdr.MockBackend(cfg)
    calls = []
    def refusing_send(samples):
        calls.append(time.time())
        raise sdr.SDRStreamError(FakeBridge.REFUSE)
    mock.send = refusing_send
    sched = sdr.FullDuplexScheduler(mock, tx_mode=sdr.TXMode.CONTINUOUS)
    sched.set_tx_waveform(wave, allow_live=True)
    sched.start_tx()
    t0 = time.time()
    while sched._tx.is_alive() and time.time() - t0 < 3.0:
        time.sleep(0.02)
    n = int(sched.tx_max_consecutive_errors)
    check(not sched._tx.is_alive(), "TX thread exits on its own")
    check(len(calls) == n, f"{len(calls)} send attempts (tx_max_consecutive_errors={n}); the G40 logged 31783 in 4 min before")
    check(len(calls) >= 2 and all(0.08 <= (calls[i+1] - calls[i]) <= 0.5 for i in range(len(calls)-1)), "0.1 s backoff between attempts")
    check(mock.health.last_error == FakeBridge.REFUSE and sched.metrics.underruns == n, "last_error holds the reason; underruns counted")
    sched.stop_tx()


async def _tx_start_once_service():
    print("[8] TX_START_ONCE: service start_tx ack carries the reason")
    svc = _svc("g40", HostType.G40_RX)
    svc.loop = asyncio.get_running_loop()
    ack = await svc._cmd_init(1, _init_p("RXA1", "TXA2", 0, 1, "single"))
    check(ack.status == CmdStatus.OK, f"cross-card TRx1A <- TRx2B init on the simulator ({ack.result})")
    refuse = ("start_tx_streaming on card1 handle 1: " + FakeBridge.REFUSE +
              " -- libsidekiq: 'Cannot stream on handle A2 in single channel mode'")
    def refusing_send(samples):
        raise sdr.SDRStreamError(refuse)
    svc.backend.send = refusing_send
    svc.sched.set_tx_waveform(np.zeros(4096, dtype=np.complex64), allow_live=True)
    a = await svc._cmd_start_tx(2, {"mode": "CONTINUOUS"})
    err = str((a.result or {}).get("err"))
    check(a.status == CmdStatus.FAILED, f"start_tx acks FAILED ({a.status})")
    check("Cannot stream on handle A2" in err and "rc=-95" in err, f"ack err carries the libsidekiq reason: {err[:100]}")
    check("RT budget" not in err, "no misleading RT-budget hint on a libsidekiq refusal")
    check(not svc._tx_running(), "TX thread stopped after the failed start")
    # the silent thread-died case keeps its RT hint
    svc.backend.send = lambda samples: None
    svc.sched.tx_max_consecutive_errors = 3
    await svc._cmd_close(3)
    ack = await svc._cmd_init(4, _init_p("RXA1", "TXA1", 0, 1, "single"))
    check(ack.status == CmdStatus.OK and (ack.result or {}).get("chan_mode") in (None, "single")
          and "tx_chan_mode" not in (ack.result or {}),
          f"simulator init ack: chan_mode echo unchanged, no tx_chan_mode on a single/single pair ({ack.result})")
    await svc._cmd_close(5)


def main():
    test_rule()
    asyncio.run(_service_cases())
    test_xcard_a2_dual()
    test_dual_packing()
    test_tx_start_once()
    asyncio.run(_tx_start_once_service())
    print(f"\n{len(FAILS)} failure(s)")
    return 1 if FAILS else 0

if __name__ == "__main__":
    sys.exit(main())
