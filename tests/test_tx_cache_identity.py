#!/usr/bin/env python3
"""
TX block cache: offline
=============================================
  1. a new waveform of the same length is always prepared fresh, even when it
     lands at the memory address the previous one used (otherwise
     4 MHz partial-band noise goes out as the barrage loaded before it)
  2. re-sending the same waveform object reuses the prepared blocks
  3. a change of IQ order re-prepares
  4. a stream of new waveforms (the bitpipe TX source): replaced prepared
     buffers stay alive until newer sends cover TX_RETAIN_BLOCKS blocks (the
     async send queue still points into them), and no longer

    python3 tests/test_tx_cache_identity.py      (or pytest)
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dpmg.radio import skiq_bridge as sb  # noqa: E402


class _Lib:
    def __init__(self):
        self.sent = []

    def skiq_transmit(self, card, hdl, ptr, _ts):
        self.sent.append(ptr.value)
        return 0


def _bridge():
    b = sb.SkiqBridge.__new__(sb.SkiqBridge)
    b.lib = _Lib()
    b.prepared = []
    real = sb.SkiqBridge._prepare_tx_blocks

    def prep(samples, iq):
        b.prepared.append(np.asarray(samples).copy())
        return np.zeros((2, 4), dtype=np.int16)    # two dummy blocks
    b._prepare_tx_blocks = prep
    b.tx_underruns = lambda card, hdl: None
    return b, real


def test_same_length_new_object_reprepares():
    b, _ = _bridge()
    buf = np.zeros(1000, dtype=np.complex64)
    a = buf[:]                     # view: same data address
    a[:] = 1.0                     # "barrage"
    b.transmit(0, 0, a, False)
    c = buf[:]                     # new object, same address and length ("noise")
    c[:] = 2.0
    assert c.ctypes.data == a.ctypes.data and len(c) == len(a)
    b.transmit(0, 0, c, False)
    assert len(b.prepared) == 2, "new waveform at a reused address must be re-prepared"
    assert b.prepared[1][0] == 2.0


def test_same_object_reuses_blocks():
    b, _ = _bridge()
    w = np.ones(500, dtype=np.complex64)
    for _ in range(5):
        b.transmit(0, 0, w, False)
    assert len(b.prepared) == 1


def test_iq_order_change_reprepares():
    b, _ = _bridge()
    w = np.ones(500, dtype=np.complex64)
    b.transmit(0, 0, w, False)
    b.transmit(0, 0, w, True)
    assert len(b.prepared) == 2


def test_stream_keeps_replaced_buffers_for_the_async_queue():
    import weakref
    b = sb.SkiqBridge.__new__(sb.SkiqBridge)
    b.lib = _Lib()
    b.tx_underruns = lambda card, hdl: None
    blocks_per_send = 12
    refs = []

    def prep(samples, iq):
        arr = np.zeros((blocks_per_send, 4), dtype=np.int16)
        refs.append(weakref.ref(arr))
        return arr
    b._prepare_tx_blocks = prep
    sends = 30
    for _ in range(sends):
        b.transmit(0, 0, np.ones(100, dtype=np.complex64), False)
    alive = [r() is not None for r in refs]
    # the current buffer plus enough replaced ones to cover the queue twice over
    need = -(-sb.TX_RETAIN_BLOCKS // blocks_per_send)
    assert all(alive[-(need + 1):]), "buffers the async queue may still read were freed"
    assert sum(alive) <= need + 2, f"kept {sum(alive)} buffers, more than the queue needs"
    assert not alive[0], "old buffers are never released"


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); n += 1
            print(f"ok {name}")
    print(f"{n} passed")
