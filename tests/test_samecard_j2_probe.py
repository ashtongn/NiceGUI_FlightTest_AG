#!/usr/bin/env python3
"""
bench/samecard_j2_probe.py offline: the four start-order variants drive the real
EpiqBackend open + send path (kind g40, same card, dual) against a recording fake
bridge, and each produces exactly the libsidekiq call order it claims:

  baseline     start_rx before start_tx (product order)
  tx_first     RX config in open, start_rx only after start_tx
  rx_after_tx  set_rx_lo / gain / DC cal / start_rx all after start_tx,
               rate + chan_mode + FIXED + TX config still before it
  rx_cycle     start_rx, stop_rx, start_tx, start_rx
plus --hold bisect, the unarmed path (held calls still replayed so the radio is
left consistent), teardown, the verdict thresholds and the JSON record.

Stdlib + numpy. Exit 0 = all passed.    python3 tests/test_samecard_j2_probe.py
"""
import json, os, sys, tempfile
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "bench"))
from dpmg.radio import sdr_core as sdr
from dpmg.radio import skiq_bridge as skb
from test_g40_ports import FakeBridge
import samecard_j2_probe as probe

FAILS = []
def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)

B1 = skb.TX_HDL["TXB1"]


class RxBridge(FakeBridge):
    """FakeBridge that delivers RX blocks once start_rx ran: a tone at
    tone_hz `tone_db` over a unit noise floor while TX streams, else noise."""
    tone_db = 60.0
    tone_hz = 310e3

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.rx_on = False
        self.tx_on = False
        self.t = 0
        self.rng = np.random.default_rng(1)

    def start_rx(self, card, hdl): super().start_rx(card, hdl); self.rx_on = True
    def stop_rx(self, card, hdl): super().stop_rx(card, hdl); self.rx_on = False
    def start_tx(self, card, hdl): super().start_tx(card, hdl); self.tx_on = True
    def stop_tx(self, card, hdl): super().stop_tx(card, hdl); self.tx_on = False
    def exit(self): super().exit(); self.rx_on = False

    def receive(self, card, iq_order_iq):
        if not self.rx_on:
            return None
        n = 8186
        x = (self.rng.standard_normal(n) + 1j * self.rng.standard_normal(n)).astype(np.complex64) * 0.01
        if self.tx_on:
            k = np.arange(self.t, self.t + n)
            x += (0.01 * 10 ** (self.tone_db / 20) / 64
                  * np.exp(2j * np.pi * self.tone_hz * k / self.rate)).astype(np.complex64)
        self.t += n
        return x, self.t


def _run(variant, *extra, bridge=RxBridge):
    out = tempfile.mktemp(suffix=".json")
    FakeBridge.instances.clear()
    sdr.SkiqBridge = lambda: bridge(rate=1_920_000.0)
    sdr._SKIQ_BRIDGE_AVAILABLE = True
    rc = probe.main(["--card", "1", "--variant", variant, "--tx-s", "0.3", "--early-s", "0.2",
                     "--settle-s", "0.0", "--out", out] + list(extra))
    with open(out) as fh:
        res = json.load(fh)
    os.remove(out)
    return rc, res, FakeBridge.instances[-1]


def _i(br, name, *args):
    return [k for k, c in enumerate(br.calls) if c[0] == name and c[1:1 + len(args)] == args]


def test_variants():
    print("\n[1] call order per variant (card 1, RXA1 + TXA2->B1, dual)")
    rc, res, br = _run("baseline", "--arm")
    check(rc == 0 and _i(br, "start_rx", 1)[0] < _i(br, "start_tx", 1, B1)[0],
          f"baseline: start_rx before start_tx (verdict {res['verdict']})")
    check(res["held"] == [] and res["open"]["same_card_fdd"] is True, "baseline: nothing held, same-card FDD")

    rc, res, br = _run("tx_first", "--arm")
    st, sr = _i(br, "start_tx", 1, B1), _i(br, "start_rx", 1)
    check(rc == 0 and st and sr and sr[0] > st[0], "tx_first: every start_rx after start_tx")
    check(_i(br, "set_rx_lo", 1)[0] < st[0], "tx_first: RX LO still set in open()")
    check(res["open"]["held_calls"] == ["start_rx", "start_rx"],
          f"tx_first: held {res['open']['held_calls']}")

    rc, res, br = _run("rx_after_tx", "--arm")
    st = _i(br, "start_tx", 1, B1)[0]
    after = all(_i(br, n, 1) and min(_i(br, n, 1)) > st
                for n in ("set_rx_lo", "set_rx_gain_mode", "set_rx_gain_index", "apply_rx_cal",
                          "start_rx"))
    check(rc == 0 and after, "rx_after_tx: LO, gain mode/index, DC cal, start_rx all after start_tx")
    before = all(_i(br, n, 1) and min(_i(br, n, 1)) < st
                 for n in ("set_rx_rate_bw_multi", "set_chan_mode", "set_rf_port_config",
                           "set_tx_lo", "configure_tx"))
    check(before, "rx_after_tx: rate multi-write, dual, FIXED, TX LO, configure_tx before start_tx")
    check(not _i(br, "set_rf_port_operation", 1, True), "rx_after_tx: no port-operation flip (dual)")

    rc, res, br = _run("rx_cycle", "--arm")
    sr, sp, st = _i(br, "start_rx", 1), _i(br, "stop_rx", 1), _i(br, "start_tx", 1, B1)
    check(rc == 0 and sr[0] < sp[0] < st[0] < sr[-1], "rx_cycle: start_rx, stop_rx, start_tx, start_rx")

    rc, res, br = _run("rx_after_tx", "--arm", "--hold", "apply_rx_cal,start_rx")
    st = _i(br, "start_tx", 1, B1)[0]
    check(min(_i(br, "set_rx_lo", 1)) < st and min(_i(br, "apply_rx_cal", 1)) > st
          and res["held"] == ["apply_rx_cal", "start_rx"], "--hold bisect: only the named calls held")


def test_unarmed_and_teardown():
    print("\n[2] unarmed + teardown")
    rc, res, br = _run("rx_after_tx")
    check(rc == 0 and res["verdict"] == "INIT-OK (unarmed)" and not _i(br, "start_tx"),
          "unarmed: no start_tx")
    check(bool(_i(br, "start_rx", 1)) and len(res["replayed"]) >= 6,
          f"unarmed: held calls replayed ({len(res['replayed'])})")
    check(br.exited, "unarmed: skiq_exit at teardown")
    rc, res, br = _run("tx_first", "--arm")
    check(br.exited and _i(br, "stop_tx", 1, B1), "armed: stop_tx + skiq_exit at teardown")
    check(sdr.SkiqBridge is not probe.holding_bridge_factory, "SkiqBridge restored after open")


def test_verdicts():
    print("\n[3] verdicts against the card's coupling level (card 1 = 43 dB)")
    for db, want in ((80.0, "ABOVE-COUPLING"), (45.0, "COUPLING-LEVEL"), (5.0, "BELOW-COUPLING")):
        cls = type("B%d" % int(db), (RxBridge,), {"tone_db": db})
        rc, res, br = _run("tx_first", "--arm", bridge=cls)
        check(res["verdict"] == want,
              f"tone {db:.0f} dB -> {res['verdict']} (late {res['late']['tone_snr_db']}, "
              f"off {res['tx_off']['tone_snr_db']})")
    check(res["tx_off"]["tone_snr_db"] < 6.0, "TX-off reference collected after stop_tx")


def main():
    os.environ.pop("DPMG_J2_TX_PIN", None)
    test_variants()
    test_unarmed_and_teardown()
    test_verdicts()
    print(f"\n{len(FAILS)} failure(s)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
