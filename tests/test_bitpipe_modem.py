"""
test_bitpipe_modem.py: the continuous stream modem and the modem link
(offline).

  test_modulator_continuity            chunked modulation equals one call; FSK phase and PSK
                                       reference carry; a dangling bit waits for its pair
  test_clean_bits_each_waveform        2-FSK, 4FSK, BPSK, QPSK: zero bit errors on a clean channel,
                                       demodulated in chunks that are not aligned to anything
  test_impaired_frames_each_waveform   bitpipe frames through SNR, carrier offset, clock offset, delay
  test_card_rate_decimation            1.92 MS/s in, 2400 Bd out (the G40's usual rate)
  test_fractional_symbol_rate          2 MS/s (833.33 samples a symbol, the B200's rate): the air
                                       rate is exactly 2400 Bd, frames decode at 2 MS/s and across
                                       radios at different rates (TX 1.92 MS/s, RX 2 MS/s)
  test_psk_rotation_invariance         a constant 90 or 180 degree carrier rotation changes no bits
  test_cfo_reported_each_waveform      the status carries the carrier offset for every waveform
                                       (FSK included), both signs, and at the radio rates with
                                       the offset mixer in use
  test_modem_link_blocks               fixed size blocks, no gaps between them, underruns only
                                       without a container, pulls answered with frames
  test_modem_listen_container_dials    the modem listens, the container dials in (laptop firewall
                                       topology); allow_from refuses others; a busy port raises
  test_sim_link_fsk                    two software radios, text and a file both ways, sealed

python3 tests/test_bitpipe_modem.py runs them through pytest. Ports 48040..48079.
"""
from __future__ import annotations

import os
import sys
import time

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DPMG_SECURE_MSG", "1")
from dpmg.bitpipe import BitpipeConfig, BitpipeContainer, LinkEngine  # noqa: E402
from dpmg.bitpipe.modem_link import ModemLink  # noqa: E402
from dpmg.bitpipe.stream_modem import IqChannel, StreamDemodulator, StreamModulator, WaveformConfig  # noqa: E402

MODS = ["FSK", "4FSK", "BPSK", "QPSK"]
FS = 38400.0


def wf_of(mod, **kw):
    return WaveformConfig(modulation=mod, symbol_rate=2400.0, deviation_hz=1200.0, **kw)


def demod_chunked(rx, iq, sizes=(1234, 777, 5000, 333)):
    out, i, k = [], 0, 0
    while i < len(iq):
        n = sizes[k % len(sizes)]
        out.append(rx.demodulate(iq[i:i + n]))
        i += n
        k += 1
    return np.concatenate(out) if out else np.zeros(0)


@pytest.mark.parametrize("mod", MODS)
def test_modulator_continuity(mod):
    rng = np.random.default_rng(1)
    bits = rng.integers(0, 2, 2001).astype(np.uint8)
    one = StreamModulator(wf_of(mod), FS).modulate(bits)
    m = StreamModulator(wf_of(mod), FS)
    parts = [m.modulate(bits[i:i + n]) for i, n in ((0, 7), (7, 500), (507, 1), (508, 1493))]
    chunked = np.concatenate(parts)
    assert np.allclose(one, chunked, atol=1e-5)
    if mod in ("4FSK", "QPSK"):
        assert len(one) == (2000 // 2) * 16 and len(m.held) == 1
    else:
        assert len(one) == 2001 * 16
    if mod in ("FSK", "4FSK"):
        dphi = np.angle(one[1:] * np.conj(one[:-1]))
        tones = np.asarray(wf_of(mod).tone_list()) * 2 * np.pi / FS
        assert np.min(np.abs(dphi[:, None] - tones[None, :]), axis=1).max() < 1e-4      # no phase steps
    assert np.allclose(np.abs(one), 0.8, atol=1e-4)


@pytest.mark.parametrize("mod", MODS)
def test_clean_bits_each_waveform(mod):
    rng = np.random.default_rng(2)
    bits = rng.integers(0, 2, 6000).astype(np.uint8)
    iq = StreamModulator(wf_of(mod), FS).modulate(bits)
    rx = StreamDemodulator(wf_of(mod), FS)
    llr = demod_chunked(rx, iq)
    hard = (llr > 0).astype(np.uint8)
    # timing acquires within the first chunk; compare after a short settle, aligned by search
    best = min(range(0, 8), key=lambda d: np.count_nonzero(hard[200:3000] != bits[200 + d:3000 + d]))
    n = min(len(hard), len(bits) - best) - 40
    errs = np.count_nonzero(hard[200:n] != bits[200 + best:n + best])
    assert errs == 0, f"{mod}: {errs} bit errors, offset {best}"


IMPAIR = [(12, 0, 0, 5), (12, 150, 20, 3), (8, 300, 10, 11)]


@pytest.mark.parametrize("mod", MODS)
@pytest.mark.parametrize("snr,cfo,ppm,delay", IMPAIR)
def test_impaired_frames_each_waveform(mod, snr, cfo, ppm, delay):
    if mod == "4FSK" and snr < 10:
        snr = 12                     # 4FSK needs about 4 dB more than 2-FSK for the same eye
    cfg = BitpipeConfig(payload_bytes=64)
    a, b = LinkEngine(cfg, name="A"), LinkEngine(cfg, name="B")
    tx, rx = StreamModulator(wf_of(mod), FS), StreamDemodulator(wf_of(mod), FS)
    ch = IqChannel(FS, snr_db=snr, cfo_hz=cfo, ppm=ppm, delay_samples=delay, seed=3)
    n = 40
    iq = np.concatenate([ch.apply(tx.modulate(a.next_frame_bits())) for _ in range(n)])
    b.feed_bits(demod_chunked(rx, iq, (7000, 3001)))
    st = b.status()["deframer"]
    assert st["frames_ok"] >= n - 3 and st["frames_bad"] <= 3, st


@pytest.mark.parametrize("mod", ["FSK", "QPSK"])
def test_card_rate_decimation(mod):
    fs = 1_920_000.0
    cfg = BitpipeConfig(payload_bytes=64)
    a, b = LinkEngine(cfg, name="A"), LinkEngine(cfg, name="B")
    tx, rx = StreamModulator(wf_of(mod), fs), StreamDemodulator(wf_of(mod), fs)
    assert rx.D == 50 and abs(rx.sps - 16.0) < 1e-9
    ch = IqChannel(fs, snr_db=10, cfo_hz=250, ppm=10, delay_samples=999, seed=4)
    t0 = time.monotonic()
    n = 12
    for _ in range(n):
        b.feed_bits(rx.demodulate(ch.apply(tx.modulate(a.next_frame_bits()))))
    dt = time.monotonic() - t0
    air = n * cfg.sizes.block_bits / tx.symbol_rate / (2 if mod == "QPSK" else 1)
    assert b.status()["deframer"]["frames_ok"] >= n - 2
    assert dt < air, f"slower than real time: {dt:.1f} s for {air:.1f} s of air"


@pytest.mark.parametrize("mod", ["FSK", "QPSK"])
def test_fractional_symbol_rate(mod):
    m = StreamModulator(wf_of(mod), 2e6)
    bits = np.random.default_rng(9).integers(0, 2, 2400 * m.wf.bits_per_symbol).astype(np.uint8)
    n = sum(len(m.modulate(bits[i:i + 200])) for i in range(0, len(bits), 200))
    assert abs(n - 2e6) <= 1, f"{n} samples for one second of symbols"          # 2400 symbols = 1 s
    for fs_tx, fs_rx in ((2e6, 2e6), (1.92e6, 2e6), (2e6, 1.92e6)):
        cfg = BitpipeConfig(payload_bytes=64)
        a, b = LinkEngine(cfg, name="A"), LinkEngine(cfg, name="B")
        tx, rx = StreamModulator(wf_of(mod), fs_tx), StreamDemodulator(wf_of(mod), fs_rx)
        nfr = 12
        iq = np.concatenate([tx.modulate(a.next_frame_bits()) for _ in range(nfr)])
        if fs_rx != fs_tx:                                       # the other radio's clock
            t = np.arange(int(len(iq) * fs_rx / fs_tx)) * (fs_tx / fs_rx)
            iq = (np.interp(t, np.arange(len(iq)), iq.real) + 1j * np.interp(t, np.arange(len(iq)), iq.imag))
        for i in range(0, len(iq), 100000):
            b.feed_bits(rx.demodulate(iq[i:i + 100000].astype(np.complex64)))
        st = b.status()["deframer"]
        assert st["frames_ok"] >= nfr - 2 and st["frames_bad"] == 0, (fs_tx, fs_rx, st)


@pytest.mark.parametrize("mod,rot", [("BPSK", np.pi), ("QPSK", np.pi / 2), ("QPSK", np.pi)])
def test_psk_rotation_invariance(mod, rot):
    rng = np.random.default_rng(5)
    bits = rng.integers(0, 2, 4000).astype(np.uint8)
    iq = StreamModulator(wf_of(mod), FS).modulate(bits)
    a = (demod_chunked(StreamDemodulator(wf_of(mod), FS), iq) > 0)
    b = (demod_chunked(StreamDemodulator(wf_of(mod), FS), (iq * np.exp(1j * rot)).astype(np.complex64)) > 0)
    n = min(len(a), len(b))
    assert np.count_nonzero(a[100:n] != b[100:n]) == 0


@pytest.mark.parametrize("mod", MODS)
@pytest.mark.parametrize("cfo", [224.0, -223.0])
def test_cfo_reported_each_waveform(mod, cfo):
    rng = np.random.default_rng(5)
    bits = rng.integers(0, 2, 2400 * 4).astype(np.uint8)
    tx, rx = StreamModulator(wf_of(mod), FS), StreamDemodulator(wf_of(mod), FS)
    ch = IqChannel(FS, snr_db=20, cfo_hz=cfo, seed=6)
    demod_chunked(rx, ch.apply(tx.modulate(bits)))
    got = rx.status()["cfo_hz"]
    assert abs(got - cfo) < 25.0, f"{mod}: reported {got:.1f} Hz for a {cfo:g} Hz offset"


@pytest.mark.parametrize("mod", ["FSK", "BPSK"])
def test_cfo_reported_with_offset_mixer(mod):
    # the radio case: 2 MS/s, the signal 200 kHz up plus the clock offset
    fs, off, cfo = 2e6, 200e3, 224.0
    bits = np.random.default_rng(8).integers(0, 2, 2400 * 3).astype(np.uint8)
    iq = StreamModulator(wf_of(mod), fs).modulate(bits)
    t = np.arange(len(iq)) / fs
    iq = (iq * np.exp(2j * np.pi * (off + cfo) * t)).astype(np.complex64)
    rx = StreamDemodulator(wf_of(mod), fs, offset_hz=off)
    for i in range(0, len(iq), 100000):
        rx.demodulate(iq[i:i + 100000])
    got = rx.status()["cfo_hz"]
    assert abs(got - cfo) < 25.0, f"{mod}: reported {got:.1f} Hz for a {cfo:g} Hz offset"


def test_modem_link_blocks():
    wf = wf_of("FSK")
    lonely = ModemLink(wf, "tcp:127.0.0.1:48041", FS, FS, block_s=0.05, pull_timeout_s=0.1)
    blk = lonely.next_tx_block()                     # never started, no container
    assert len(blk) == 1920 and not np.any(blk) and lonely.stats["underruns"] == 1
    cfg = BitpipeConfig(name="M", bit_port=48040, ctl_port=48042, app_base_port=48043)
    cont = BitpipeContainer(cfg, app_sockets=False)
    cont.start()
    link = ModemLink(wf, "tcp:127.0.0.1:48040", FS, FS, block_s=0.05, offset_hz=3000.0)
    link.start()
    try:
        t0 = time.monotonic()
        while not link.connected() and time.monotonic() - t0 < 5:
            time.sleep(0.05)
        assert link.connected()
        blocks = [link.next_tx_block() for _ in range(60)]
        assert all(len(b) == 1920 for b in blocks)
        x = np.concatenate(blocks)
        assert link.stats["underruns"] == 0 and np.all(np.abs(x) > 0.5)
        f = np.angle(x[1:] * np.conj(x[:-1])) * FS / (2 * np.pi) - 3000.0
        assert np.max(np.abs(np.abs(f) - 1200.0)) < 1.0                     # tones only, no seams
        assert link.stats["frames_in"] >= 60 * 1920 / 16 / cfg.sizes.block_bits
        assert cont.status()["bit_face"]["pulls"] == link.stats["pulls"]
        assert link.hello.get("block_bits") == cfg.sizes.block_bits
    finally:
        link.stop()
        cont.stop()


def test_modem_listen_container_dials():
    import socket as _s
    wf = wf_of("FSK")
    link = ModemLink(wf, None, FS, FS, block_s=0.05, listen="tcp:127.0.0.1:48050", allow_from="127.0.0.1")
    link.start()
    cfg = BitpipeConfig(name="D", bit_port=48051, ctl_port=48052, app_base_port=48053,
                        bit_connect="tcp:127.0.0.1:48050")
    cont = BitpipeContainer(cfg, app_sockets=False)
    cont.start()
    try:
        assert wait_for(lambda: link.connected() and cont.connected())
        blocks = [link.next_tx_block() for _ in range(20)]
        assert link.stats["underruns"] == 0 and all(len(b) == 1920 for b in blocks)
        assert link.brief()["container"] == "listen:tcp:127.0.0.1:48050" and link.brief()["peer"] == "127.0.0.1"
        with pytest.raises(OSError):                                   # the port is taken
            ModemLink(wf, None, FS, FS, listen="tcp:127.0.0.1:48050").start()
    finally:
        cont.stop()
        link.stop()
    picky = ModemLink(wf, None, FS, FS, listen="tcp:127.0.0.1:48054", allow_from="10.9.9.9")
    picky.start()
    try:
        c = _s.create_connection(("127.0.0.1", 48054), timeout=2)
        assert wait_for(lambda: picky.stats["refused_peers"] == 1) and not picky.connected()
        c.close()
    finally:
        picky.stop()


def wait_for(pred, timeout=5.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.05)
    return False


def test_sim_link_fsk():
    from dpmg.bitpipe.loopback import default_configs
    from dpmg.bitpipe.sim_link import run_sim
    cfg_a, cfg_b = default_configs(48060, payload_bytes=64)
    rep = run_sim(cfg_a, cfg_b, wf_of("FSK"), fs=FS, secure=True, snr_db=10, cfo_hz=200, ppm=10,
                  lines=6, file_kb=2, timeout=90, verbose=False)
    assert rep["ok"], rep.get("error")
    for m in (rep["modem_a"], rep["modem_b"]):
        assert m["stats"]["underruns"] == 0
    for s in (rep["status_a"], rep["status_b"]):
        assert s["open_failed"] == 0 and s["locked"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"] + sys.argv[1:]))
