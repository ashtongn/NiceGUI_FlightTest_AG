"""
Transceive controller over a simulated two-node link: node A initiates the
preset script, node B is the responder. A.tx then channel then B.on_rx then reply then
channel then A.on_rx. Verifies the RX to decode to TX-response loop, the ARM gate
(disarmed decodes but never keys TX), and dedup (a repeated burst replies once).
Deterministic. Exit 0 = all pass.
"""
from __future__ import annotations

import sys
import os
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dpmg.modem.modem import Modem, ModemConfig, ack_responder, PRESET_SCRIPTS
from dpmg.modem.transceive import TransceiveController, TransceiveConfig

rng = np.random.default_rng(0xBEEF)
checks = []


def chk(name, ok, detail=""):
    checks.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")


def awgn(snr_db):
    def ch(iq):
        x = np.asarray(iq, dtype=np.complex64)
        p = np.mean(np.abs(x) ** 2) + 1e-12
        npow = p / (10 ** (snr_db / 10.0))
        n = (rng.standard_normal(len(x)) + 1j * rng.standard_normal(len(x)))
        return x + (n * np.sqrt(npow / 2.0)).astype(np.complex64)
    return ch


def fsk_cfg(rx_port, tx_port):
    base = dict(modulation="FSK", symbol_rate=1000, samples_per_bit=48,
                tone0=1000, tone1=2000)
    # NV100 card = the trailing letter (A=card0, B=card1); the digit is the
    # channel within the card. TRx1A + TRx2A share one RFIC: dual-channel.
    return TransceiveConfig(
        rx=ModemConfig(rx_port=rx_port, tx_port=tx_port, **base),
        tx=ModemConfig(rx_port=rx_port, tx_port=tx_port, **base),
        same_card=(rx_port[-1] == tx_port[-1]))


def run_two_node(cfg_b, channel, arm=True):
    """A initiates the script; B responds. Returns (b_decoded, a_decoded, B)."""
    a = Modem(cfg_b.rx)                        # node A uses same waveform
    tx_capture = {}

    def b_tx(iq):
        tx_capture["iq"] = iq

    B = TransceiveController(cfg_b, responder=ack_responder(), tx_send=b_tx)
    B.arm(arm)
    b_decoded, a_decoded = [], []
    for msg in PRESET_SCRIPTS["linkcheck"]:
        tx_capture.clear()
        ev = B.on_rx(channel(a.transmit(msg)))
        if ev and ev.valid:
            b_decoded.append(ev.decoded)
        if "iq" in tx_capture:                 # B transmitted a reply
            ra = a.receive(channel(tx_capture["iq"]))
            if ra.valid:
                a_decoded.append(ra.message)
    return b_decoded, a_decoded, B


def main():
    print("Transceive controller -- two-node link\n")
    script = PRESET_SCRIPTS["linkcheck"]

    # 1) same-card different-port routing (needs dual-channel), noiseless
    cfg = fsk_cfg("TRx1A", "TRx2A")
    chk("routing: same card, two ports -> dual-channel required",
        cfg.needs_dual_channel is True)
    bd, ad, B = run_two_node(cfg, lambda x: x, arm=True)
    chk("B decoded every A message", bd == script, f"({len(bd)}/{len(script)})")
    exp_replies = [ack_responder().respond(m) for m in script]
    chk("A decoded every B reply", ad == exp_replies, f"({len(ad)}/{len(exp_replies)})")
    chk("B transmitted when armed", B.stats["transmitted"] == len(script))

    # 2) other-card routing, through a noisy channel
    cfg2 = fsk_cfg("TRx1A", "TRx2B")
    chk("routing: different cards -> no dual-channel",
        cfg2.needs_dual_channel is False)
    bd2, ad2, _ = run_two_node(cfg2, awgn(20), arm=True)
    chk("noisy link: B decoded all", bd2 == script)
    chk("noisy link: A decoded all replies", ad2 == exp_replies)

    # 3) ARM gate: disarmed decodes but never keys TX
    bd3, ad3, B3 = run_two_node(cfg, lambda x: x, arm=False)
    chk("disarmed: B still decodes", bd3 == script)
    chk("disarmed: B never transmitted", B3.stats["transmitted"] == 0)
    chk("disarmed: A got nothing back", ad3 == [])
    chk("disarmed: replies were still computed", B3.stats["replied"] == len(script))

    # 4) dedup: same burst fed twice replies once
    a = Modem(cfg.rx)
    tx_hits = []
    B4 = TransceiveController(cfg, tx_send=lambda iq: tx_hits.append(iq))
    B4.arm(True)
    burst = a.transmit("PING 1")
    B4.on_rx(burst)
    B4.on_rx(burst)
    chk("dedup: repeated burst transmits once", len(tx_hits) == 1,
        f"(hits={len(tx_hits)}, dups={B4.stats['duplicates']})")

    print(f"\nresult: {sum(checks)}/{len(checks)} checks passed")
    return 0 if checks and all(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
