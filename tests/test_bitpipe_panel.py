"""
test_bitpipe_panel.py: what the link panel reads (no browser).

  test_ber_measured_before_fec      injected channel bit errors read back within 10 percent
  test_fer_window                   the frame error rate follows the last 30 s, not the whole run
  test_ping_round_trip              a ping on the control stream comes back with a round trip
  test_ctl_and_stream_clients       status / send / ping over the control face; chat over the app face
  test_modem_status_relayed         a modem link's status reaches the container's status
  test_link_state_badge             the badge logic for every link state

python3 tests/test_bitpipe_panel.py runs them through pytest. Ports 48100..48139.
"""
from __future__ import annotations

import os
import sys
import time

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dpmg.bitpipe import BitpipeConfig, BitpipeContainer, LinkEngine  # noqa: E402
from dpmg.bitpipe.ctl_client import CtlClient, StreamClient  # noqa: E402


def llr(bits):
    return np.where(np.asarray(bits) == 1, 1.0, -1.0)


def wait(pred, timeout=5.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.05)
    return False


@pytest.mark.parametrize("p", [0.005, 0.02])
def test_ber_measured_before_fec(p):
    cfg = BitpipeConfig()
    a, b = LinkEngine(cfg, name="A"), LinkEngine(cfg, name="B")
    rng = np.random.default_rng(7)
    for _ in range(40):
        x = llr(a.next_frame_bits())
        b.feed_bits(np.where(rng.random(len(x)) < p, -x, x))
    q = b.quality()
    assert abs(q["ber_total"] - p) / p < 0.1, q
    assert q["fer"] == 0.0 and q["frames_ok"] == 40


def test_fer_window():
    cfg = BitpipeConfig()
    a, b = LinkEngine(cfg, name="A"), LinkEngine(cfg, name="B")
    b.fer_window_s = 0.3
    junk = np.random.default_rng(1).integers(0, 2, cfg.sizes.block_bits * 10)
    b.feed_bits(llr(junk))                                     # 10 frames worth of noise first
    b.quality()
    assert b.quality()["fer"] > 0.5
    time.sleep(0.35)
    b.quality()
    for _ in range(20):
        b.feed_bits(llr(a.next_frame_bits()))
    time.sleep(0.35)
    for _ in range(20):
        b.feed_bits(llr(a.next_frame_bits()))
    assert b.quality()["fer"] < 0.1                            # the noise has left the window


def test_ping_round_trip():
    cfg = BitpipeConfig()
    a, b = LinkEngine(cfg, name="A"), LinkEngine(cfg, name="B")
    a.ping()
    got = []
    for _ in range(6):
        got += b.feed_bits(llr(a.next_frame_bits()))
        got += a.feed_bits(llr(b.next_frame_bits()))
    q = a.quality()
    assert q["ping_rtt_s"] is not None and q["ping_rtt_s"] < 1.0
    assert got == []                                           # control records never reach an application


def test_ctl_and_stream_clients():
    ca = BitpipeConfig(name="PA", bit_port=48100, ctl_port=48101, app_base_port=48102)
    cb = BitpipeConfig(name="PB", bit_port=48110, ctl_port=48111, app_base_port=48112, bit_connect="tcp:127.0.0.1:48100")
    A, B = BitpipeContainer(ca), BitpipeContainer(cb)
    A.start()
    B.start()
    try:
        assert wait(lambda: A.connected() and B.connected())
        # B connects to A's bit face as if it were a modem; only the control and
        # application faces are under test here, so no frames are pulled
        ctl = CtlClient(A.ctl_endpoint().describe())
        st = ctl.status()
        assert st and st["bit_face"]["connected"] and "quality" in st and st["transport"] == "tcp"
        assert ctl.send(1, b"abc") and ctl.status()["pending_bytes"] == 3
        assert ctl.ping() and ctl.status()["pending_bytes"] == 3 + 14
        chat = StreamClient(A.app_endpoint(1).describe())
        assert wait(chat.connected)
        got = bytearray()
        A._dispatch(1, b"line one\n")                           # as if delivered from the peer
        assert wait(lambda: got.extend(chat.take()) or bytes(got) == b"line one\n", 3)
        assert chat.send(b"typed\n") and wait(lambda: A.engine.pending_bytes(1) == 3 + 6)
        chat.close()
        dead = CtlClient("tcp:127.0.0.1:48139", timeout=0.3)
        assert dead.status() is None and dead.last_error
    finally:
        A.stop()
        B.stop()


def test_modem_status_relayed():
    from dpmg.bitpipe.modem_link import ModemLink
    from dpmg.bitpipe.stream_modem import WaveformConfig
    cfg = BitpipeConfig(name="PM", bit_port=48120, ctl_port=48121, app_base_port=48122)
    c = BitpipeContainer(cfg, app_sockets=False)
    c.start()
    link = ModemLink(WaveformConfig(modulation="BPSK"), c.bit_endpoint().describe(), 38400, 38400)
    link.start()
    try:
        assert wait(link.connected)
        link.on_rx(np.zeros(4000, dtype=np.complex64))
        assert wait(lambda: c.status()["modem"].get("waveform", {}).get("modulation") == "BPSK")
        m = c.status()["modem"]
        assert "demod" in m and "stats" in m and m["received_at"] > 0
    finally:
        link.stop()
        c.stop()


def test_link_state_badge():
    from dpmg.bitpipe.link_panel import link_state
    assert link_state(None)[0] == "NO CONTAINER"
    base = {"bit_face": {"connected": True}, "link_down": False, "locked": True, "peer_seen": True}
    assert link_state(base)[0] == "LINKED"
    assert link_state(dict(base, bit_face={"connected": False}))[0] == "NO MODEM"
    assert link_state(dict(base, link_down=True))[0] == "LINK DOWN"
    assert link_state(dict(base, locked=False))[0] == "SEARCHING"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"] + sys.argv[1:]))
