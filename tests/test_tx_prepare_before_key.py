"""The TX blocks are built before the PA is keyed, so a long waveform does not run
out the PA watchdog before its first block goes out (RL-042)."""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dpmg.radio import sdr_core                     # noqa: E402
from dpmg.radio.skiq_bridge import SkiqBridge       # noqa: E402


class FakeFrontend:
    def __init__(self, order):
        self.order = order

    def sequence_tx_on(self):
        self.order.append("key")

    def tx_tick(self, busy_s=0.0):
        self.order.append("tick")


class FakeSkiq:
    def __init__(self, order):
        self.order = order

    def prepare_tx(self, samples, iq_order_iq):
        self.order.append("prepare")

    def set_rf_port_operation(self, card, tx):
        pass

    def start_tx(self, card, hdl):
        self.order.append("start")

    def transmit(self, card, hdl, samples, iq_order_iq):
        self.order.append("transmit")


def _backend(order):
    be = sdr_core.EpiqBackend.__new__(sdr_core.EpiqBackend)
    be._skiq = FakeSkiq(order)
    be.frontend = FakeFrontend(order)
    be._iq_order_iq = True
    be._tx_card = 1
    be._tx_hdl = 0
    be._tx_streaming = False
    be._achieved_rate = 61_440_000.0
    be.config = type("C", (), {"sample_rate": 61_440_000.0})()
    be._chan_mode = "single"
    be.health = type("H", (), {"tx_frames": 0, "last_error": ""})()
    return be


def test_blocks_are_built_before_the_pa_is_keyed():
    order = []
    _backend(order).send(np.zeros(1000, dtype=np.complex64))
    assert order == ["prepare", "key", "start", "tick", "transmit", "tick"]


def test_each_later_send_prepares_before_it_feeds_the_watchdog():
    order = []
    be = _backend(order)
    be._tx_streaming = True
    be.send(np.zeros(1000, dtype=np.complex64))
    assert order == ["prepare", "tick", "transmit", "tick"]


def _bridge():
    b = SkiqBridge.__new__(SkiqBridge)
    b.tx_block_samples = 1024
    b.tx_dual = False
    return b


def test_prepare_tx_caches_by_waveform_object():
    b, w = _bridge(), np.ones(2048, dtype=np.complex64) * 0.5
    first = b.prepare_tx(w, True)
    assert b.prepare_tx(w, True) is first
    assert b.prepare_tx(w.copy(), True) is not first
