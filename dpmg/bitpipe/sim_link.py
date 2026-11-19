"""
Two radios in software: container A, modem A, an IQ channel each way,
modem B, container B. No sockets other than the containers' own faces, no
radio, no clock but the loop itself, which runs as fast as the decoders go.

    python3 -m dpmg.bitpipe.sim_link --modulation QPSK --snr 10 --cfo 300 --ppm 10
    python3 -m dpmg.bitpipe.sim_link --all          one run per waveform

Each step pulls one TX block from each modem (which pulls frames from its
container), runs the block through that direction's channel and hands it to
the other modem's receiver, whose soft bits go back to its container. Text
both ways and a file each way, the same acceptance as the socket loopback,
but across modulation and demodulation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import threading
import time
from typing import Optional

import numpy as np

from . import streams as st
from .config import BitpipeConfig
from .container import BitpipeContainer
from .crypto import NullCrypto, loopback_crypto_pair
from .loopback import _AppClient, _wait, default_configs
from .modem_link import ModemLink
from .stream_modem import IqChannel, WaveformConfig


class SimRadioPair:
    """Drives the two modems and channels in a thread until stopped."""

    def __init__(self, ma: ModemLink, mb: ModemLink, ch_ab: IqChannel, ch_ba: IqChannel, realtime: bool = False):
        self.ma, self.mb, self.ch_ab, self.ch_ba = ma, mb, ch_ab, ch_ba
        self.realtime = realtime
        self._stop = threading.Event()
        self._t: Optional[threading.Thread] = None
        self.steps = 0

    def start(self) -> None:
        self._t = threading.Thread(target=self._run, daemon=True, name="sim-radio-pair")
        self._t.start()

    def _run(self) -> None:
        t_next = time.monotonic()
        while not self._stop.is_set():
            blk_a = self.ma.next_tx_block()
            blk_b = self.mb.next_tx_block()
            self.mb.on_rx(self.ch_ab.apply(blk_a))
            self.ma.on_rx(self.ch_ba.apply(blk_b))
            self.steps += 1
            if self.realtime:
                t_next += self.ma.block_s
                dt = t_next - time.monotonic()
                if dt > 0:
                    time.sleep(dt)

    def stop(self) -> None:
        self._stop.set()
        if self._t is not None:
            self._t.join(timeout=5.0)


def run_sim(cfg_a: BitpipeConfig, cfg_b: BitpipeConfig, wf: WaveformConfig, fs: float = 38400.0,
            secure: bool = True, snr_db: Optional[float] = None, cfo_hz: float = 0.0, ppm: float = 0.0,
            offset_hz: float = 0.0, lines: int = 10, file_kb: int = 4, timeout: float = 120.0, seed: int = 1,
            verbose: bool = True, block_s: float = 0.05) -> dict:
    if secure:
        ca, cb = loopback_crypto_pair()
        cfg_a.crypto = cfg_b.crypto = "secure"
    else:
        ca, cb = NullCrypto(), NullCrypto()
    A, B = BitpipeContainer(cfg_a, ca), BitpipeContainer(cfg_b, cb)
    A.start()
    B.start()
    ma = ModemLink(wf, A.bit_endpoint().describe(), fs, fs, offset_hz=offset_hz, block_s=block_s, name="modemA")
    mb = ModemLink(wf, B.bit_endpoint().describe(), fs, fs, offset_hz=offset_hz, block_s=block_s, name="modemB")
    ma.start()
    mb.start()
    pair = SimRadioPair(ma, mb, IqChannel(fs, snr_db, cfo_hz, ppm, delay_samples=7, seed=seed),
                        IqChannel(fs, snr_db, -cfo_hz, -ppm, delay_samples=3, seed=seed + 1))
    rng = np.random.default_rng(seed)
    rep = {"ok": False, "modulation": wf.kind, "symbol_rate": wf.symbol_rate, "snr_db": snr_db, "cfo_hz": cfo_hz,
           "ppm": ppm, "secure": secure}
    t0 = time.monotonic()
    try:
        if not _wait(lambda: ma.connected() and mb.connected(), 10.0):
            rep["error"] = "modems did not connect to the containers"
            return rep
        pair.start()
        ca_, cb_ = _AppClient(A.app_endpoint(st.STREAM_CHAT)), _AppClient(B.app_endpoint(st.STREAM_CHAT))
        fa, fb = _AppClient(A.app_endpoint(st.STREAM_FILE)), _AppClient(B.app_endpoint(st.STREAM_FILE))
        time.sleep(0.2)
        la = [f"A>B line {i} {rng.integers(0, 1 << 30)}\n".encode() for i in range(lines)]
        lb = [f"B>A line {i} {rng.integers(0, 1 << 30)}\n".encode() for i in range(lines)]
        for x, y in zip(la, lb):
            ca_.send(x)
            cb_.send(y)
        want_a, want_b = b"".join(la), b"".join(lb)
        rep["text_ok"] = _wait(lambda: cb_.received() == want_a and ca_.received() == want_b, timeout / 2)
        rep["text_time_s"] = round(time.monotonic() - t0, 2)
        if verbose:
            print(f"text both ways: {'PASS' if rep['text_ok'] else 'FAIL'} ({rep['text_time_s']} s)")
        data_a, data_b = rng.bytes(file_kb * 1024), rng.bytes(file_kb * 1024)
        t1 = time.monotonic()
        fa.send(st.file_record("a_to_b.bin", data_a))
        fb.send(st.file_record("b_to_a.bin", data_b))

        def done():
            return bool(st.FileReceiver().feed(fb.received())) and bool(st.FileReceiver().feed(fa.received()))
        _wait(done, timeout)
        gb, ga = st.FileReceiver().feed(fb.received()), st.FileReceiver().feed(fa.received())
        rep["file_ok"] = bool(gb and ga and gb[0].ok and ga[0].ok and gb[0].data == data_a and ga[0].data == data_b)
        rep["file_time_s"] = round(time.monotonic() - t1, 2)
        if verbose:
            print(f"file both ways: {'PASS' if rep['file_ok'] else 'FAIL'} ({file_kb} kB each, {rep['file_time_s']} s; "
                  f"at B {len(fb.received())} B, at A {len(fa.received())} B)")
        for c in (ca_, cb_, fa, fb):
            c.close()
        rep["ok"] = bool(rep["text_ok"] and rep["file_ok"])
    finally:
        pair.stop()
        rep["steps"] = pair.steps
        rep["air_seconds"] = round(pair.steps * block_s, 1)
        rep["elapsed_s"] = round(time.monotonic() - t0, 2)
        rep["modem_a"], rep["modem_b"] = ma.status(), mb.status()
        rep["status_a"], rep["status_b"] = A.status(), B.status()
        ma.stop()
        mb.stop()
        A.stop()
        B.stop()
    return rep


def summary(rep: dict) -> str:
    def one(s, m):
        d = m["demod"]
        return (f"frames rx {s['frames_rx']} bad {s['deframer']['frames_bad']} retx {s['tx']['retx']} "
                f"open_failed {s['open_failed']} locked {s['locked']} | demod cfo {d['cfo_hz']:.0f} Hz "
                f"eye {d['eye']:.2f} timing {d['timing']:.2f} | pulls {m['stats']['pulls']} underruns {m['stats']['underruns']}")
    return (f"A: {one(rep['status_a'], rep['modem_a'])}\nB: {one(rep['status_b'], rep['modem_b'])}\n"
            f"{rep['steps']} blocks = {rep['air_seconds']} s of air in {rep['elapsed_s']} s")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="bitpipe over two software radios")
    ap.add_argument("--modulation", default="FSK")
    ap.add_argument("--all", action="store_true", help="FSK, 4FSK, BPSK, QPSK in turn")
    ap.add_argument("--symbol-rate", type=float, default=2400.0)
    ap.add_argument("--deviation", type=float, default=1200.0)
    ap.add_argument("--fs", type=float, default=38400.0)
    ap.add_argument("--snr", type=float, default=None)
    ap.add_argument("--cfo", type=float, default=0.0)
    ap.add_argument("--ppm", type=float, default=0.0)
    ap.add_argument("--offset", type=float, default=0.0, help="TX offset from the LO, Hz")
    ap.add_argument("--payload", type=int, default=64)
    ap.add_argument("--clear", action="store_true", help="no encryption")
    ap.add_argument("--lines", type=int, default=10)
    ap.add_argument("--file-kb", type=int, default=4)
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--base-port", type=int, default=47980)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    mods = ["FSK", "4FSK", "BPSK", "QPSK"] if a.all else [a.modulation]
    allok = True
    for m in mods:
        wf = WaveformConfig(modulation=m, symbol_rate=a.symbol_rate, deviation_hz=a.deviation)
        cfg_a, cfg_b = default_configs(a.base_port, payload_bytes=a.payload, crypto="none" if a.clear else "secure")
        print(f"sim link {m} {a.symbol_rate:g} Bd fs {a.fs:g} snr {a.snr} cfo {a.cfo} ppm {a.ppm} offset {a.offset}")
        rep = run_sim(cfg_a, cfg_b, wf, fs=a.fs, secure=not a.clear, snr_db=a.snr, cfo_hz=a.cfo, ppm=a.ppm,
                      offset_hz=a.offset, lines=a.lines, file_kb=a.file_kb, timeout=a.timeout)
        print(summary(rep))
        if a.json:
            print(json.dumps(rep, indent=1, default=str))
        print(f"SIM LINK {m}:", "PASS" if rep["ok"] else "FAIL")
        allok = allok and rep["ok"]
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
