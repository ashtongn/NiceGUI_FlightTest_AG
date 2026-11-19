#!/usr/bin/env python3
"""
test_bitpipe_service.py: the bitpipe stream link through dpmg_service.py on
the simulated radio, no hardware.

The mock backend runs with its TX to RX loopback (extras tx_loopback), so one
simulated radio hears its own transmission. A container sits on the bit face;
bitpipe_start attaches the stream modem (TX source + RX frame hook), start_tx
keys it like any waveform, and the container hears its own frames, so bytes
sent on a stream come back out of the same stream: the whole service path
(scheduler TX source, backend send, RX drain, frame hook, continuous
demodulator, container) end to end.

  1. guards: bitpipe_start before init refused; while TX runs refused; twice refused
  2. 2-FSK, clear link: text out and back through the simulated radio
  3. QPSK, sealed link, 300 Hz carrier offset in the loopback: text out and back
  4. bitpipe_status (modem and container), bitpipe_stop stops TX and restores the hook
  5. start_tx with no waveform and no stream is still refused
  6. listen mode: the modem listens, the container dials in (the laptop
     firewall topology), text out and back
  8. a cached waveform at another rate (as left by an earlier test) does not
     block start_tx for the stream, which never plays it
  7. a backend that pads every send to a whole TX block (as the G40 bridge
     does): the modem's blocks follow the backend's block size and the text
     still makes the round trip

    python3 tests/test_bitpipe_service.py
"""
import asyncio
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
os.environ.setdefault("DPMG_SECURE_MSG", "1")

from dpmg.radio import dpmg_service as ds                              # noqa: E402
from dpmg.link import proto                                           # noqa: E402
from dpmg.link.proto import Cmd, CmdStatus, HostType             # noqa: E402
from dpmg.bitpipe import BitpipeConfig, BitpipeContainer, NullCrypto  # noqa: E402
from dpmg.bitpipe.crypto import SecureContextCrypto, make_context      # noqa: E402

FAILS = []
BASE = 48020


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


class _Ep:
    """Encodes every ack the way the real endpoint does and records any that
    would not fit one datagram (the endpoint would send a trimmed copy)."""
    oversize = []

    def send(self, msg, addr):
        try:
            proto.encode(msg, 1)
        except proto.ProtoError as e:
            _Ep.oversize.append((type(msg).__name__, str(e)))


def ack_fits(a):
    try:
        return len(proto.encode(a, 1))
    except proto.ProtoError:
        return None


def init_params(cfo=0.0, snr=None, tx_block=0):
    ex = {"card": 0, "chan_mode": "single", "tx_center_freq": 430_500_000, "tx_loopback": True,
          "loopback_cfo_hz": cfo, "mock_tx_block": tx_block}
    if snr is not None:
        ex["loopback_snr_db"] = snr
    return {"kind": "g40", "sample_rate": 1_920_000, "center_freq": 430_500_000, "rx_gain": 220,
            "tx_gain": 10.0, "clock_source": "internal", "rx_antenna": "RXA1", "tx_antenna": "TXA1",
            "extras": ex}


class Inbox:
    def __init__(self):
        self.data = {}
        self.lock = threading.Lock()

    def __call__(self, stream, data):
        with self.lock:
            self.data.setdefault(stream, bytearray()).extend(data)

    def get(self, stream):
        with self.lock:
            return bytes(self.data.get(stream, b""))


async def wait_for(pred, timeout):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        await asyncio.sleep(0.1)
    return False


async def run_case(svc, n, label, modulation, crypto, cfo, port, text, listen=False, tx_block=0):
    inbox = Inbox()
    cfg = BitpipeConfig(name=label, bit_port=port, ctl_port=port + 1, app_base_port=port + 2,
                        crypto=crypto.name if crypto.name != "none" else "none",
                        bit_connect=f"tcp:127.0.0.1:{port + 5}" if listen else None)
    cont = BitpipeContainer(cfg, crypto, on_deliver=inbox, app_sockets=False)
    cont.start()
    try:
        a = await svc._apply(Cmd(n, "init", init_params(cfo=cfo, snr=20, tx_block=tx_block)))
        check(a.status == CmdStatus.OK, f"{label}: init on the simulator ({(a.result or {}).get('err', 'ok')})")
        a = await svc._apply(Cmd(n + 1, "start_rx", {}))
        check(a.status == CmdStatus.OK, f"{label}: start_rx")
        where = ({"listen": f"tcp:127.0.0.1:{port + 5}", "allow_from": "127.0.0.1"} if listen
                 else {"container": f"tcp:127.0.0.1:{port}"})
        a = await svc._apply(Cmd(n + 2, "bitpipe_start", dict(where, modulation=modulation, symbol_rate=2400,
                                                              deviation_hz=1200, amplitude=0.5)))
        check(a.status == CmdStatus.OK, f"{label}: bitpipe_start {modulation} ({(a.result or {}).get('err', 'ok')})")
        check(ack_fits(a) is not None, f"{label}: bitpipe_start ack fits one datagram ({ack_fits(a)} B)")
        if tx_block:
            blk = (a.result or {}).get("bitpipe", {}).get("tx_block", 0)
            check(blk and blk % tx_block == 0, f"{label}: modem TX block {blk} is a whole number of {tx_block}")
        a2 = await svc._apply(Cmd(n + 3, "bitpipe_start", {"container": f"tcp:127.0.0.1:{port}"}))
        check(a2.status == CmdStatus.REJECTED, f"{label}: a second bitpipe_start refused")
        check(await wait_for(cont.connected, 10), f"{label}: modem connected to the container's bit face")
        a = await svc._apply(Cmd(n + 4, "start_tx", {"mode": "CONTINUOUS"}))
        check(a.status == CmdStatus.OK, f"{label}: start_tx keys the stream ({(a.result or {}).get('err', 'ok')})")
        cont.send(1, text)
        ok = await wait_for(lambda: inbox.get(1) == text, 40)
        check(ok, f"{label}: {len(text)} B out and back through the simulated radio "
                  f"(got {len(inbox.get(1))} B)")
        st = cont.status()
        check(st["locked"] and st["frames_rx"] > 0, f"{label}: container locked, {st['frames_rx']} frames heard")
        a = await svc._apply(Cmd(n + 5, "bitpipe_status", {"container": True}))
        r = a.result or {}
        m = r.get("modem", {})
        check(a.status == CmdStatus.OK and r.get("running") and r.get("tx_running"),
              f"{label}: bitpipe_status running with TX on")
        check(ack_fits(a) is not None, f"{label}: bitpipe_status ack with the container fits one datagram "
                                       f"({ack_fits(a)} B)")
        check(m.get("underruns", 99) == 0 and m.get("pulls", 0) > 0,
              f"{label}: modem pulled {m.get('pulls')} times, {m.get('underruns')} underruns")
        cb = r.get("container") or {}
        check(cb.get("locked") is True and (cb.get("frames_rx") or 0) > 0,
              f"{label}: container brief relayed (locked, {cb.get('frames_rx')} frames heard)")
        if cfo:
            est = m.get("cfo_hz", 0.0)
            check(abs(est - cfo) < 20, f"{label}: demodulator tracked the {cfo:g} Hz offset ({est:.1f} Hz)")
        a = await svc._apply(Cmd(n + 6, "bitpipe_stop", {}))
        check(ack_fits(a) is not None, f"{label}: bitpipe_stop ack fits one datagram ({ack_fits(a)} B)")
        check(a.status == CmdStatus.OK and (a.result or {}).get("stopped_tx") is True,
              f"{label}: bitpipe_stop stopped TX")
        check(svc.sched.tx_source is None and not svc._tx_running(), f"{label}: TX source cleared, TX idle")
        check(svc.sched.frame_hook is None or getattr(svc.sched.frame_hook, "__name__", "") != "_hook",
              f"{label}: RX frame hook restored")
        await svc._apply(Cmd(n + 7, "stop_rx", {}))
        await svc._apply(Cmd(n + 8, "close", {}))
    finally:
        cont.stop()


async def case_service(tmp):
    ds.DATA_DIR = tmp
    svc = ds.RadioService(serial="G40-SIM-BP", host_type=HostType.G40_RX, kind="g40", simulate=True,
                          cmd_port=BASE - 2, discovery_port=BASE - 1, broadcast="127.0.0.1")
    svc.loop = asyncio.get_running_loop()
    svc.ep = _Ep()
    print("[1] guards")
    a = await svc._apply(Cmd(1, "bitpipe_start", {}))
    check(a.status == CmdStatus.REJECTED, "bitpipe_start before init refused")
    a = await svc._apply(Cmd(2, "bitpipe_status", {}))
    check(a.status == CmdStatus.OK and a.result.get("running") is False, "bitpipe_status idle")

    print("[2] 2-FSK, clear link")
    await run_case(svc, 10, "fsk", "FSK", NullCrypto(), 0.0, BASE, b"hello over the simulated G40\n")

    print("[3] QPSK, sealed link, 300 Hz offset")
    from dpmg.secure import api as sf
    ctx = make_context(sf.generate_key_file(3, 4, label="svc-test"), 4)
    await run_case(svc, 30, "qpsk", "QPSK", SecureContextCrypto(ctx), 300.0, BASE + 10,
                   b"sealed QPSK through the service path\n")

    print("[6] listen mode, container dials in")
    await run_case(svc, 80, "listen", "FSK", NullCrypto(), 0.0, BASE + 30, b"container dialled the radio\n",
                   listen=True)

    print("[8] stale cached waveform at another rate")
    a = await svc._apply(Cmd(100, "init", init_params()))
    svc._tx_src = {"mode": "fromfile", "path": "/nonexistent", "rate": 30.72e6}   # as after a 30.72 MS/s load
    await svc._apply(Cmd(101, "start_rx", {}))
    cfg8 = BitpipeConfig(name="stale", bit_port=BASE + 50, ctl_port=BASE + 51, app_base_port=BASE + 52)
    c8 = BitpipeContainer(cfg8, NullCrypto(), app_sockets=False)
    c8.start()
    try:
        a = await svc._apply(Cmd(102, "bitpipe_start", {"container": f"tcp:127.0.0.1:{BASE + 50}"}))
        a = await svc._apply(Cmd(103, "start_tx", {"mode": "CONTINUOUS"}))
        check(a.status == CmdStatus.OK, f"start_tx keys the stream despite a 30.72 MS/s cached waveform "
                                        f"({(a.result or {}).get('err', 'ok')})")
        await svc._apply(Cmd(104, "bitpipe_stop", {}))
        a = await svc._apply(Cmd(105, "start_tx", {"mode": "CONTINUOUS"}))
        check(a.status == CmdStatus.REJECTED and (a.result or {}).get("rate_mismatch"),
              "without the stream the rate guard still refuses the mismatched waveform")
    finally:
        c8.stop()
        await svc._apply(Cmd(106, "stop_tx", {}))
        await svc._apply(Cmd(107, "close", {}))
    svc._tx_src = None

    print("[7] backend pads sends to a whole TX block")
    await run_case(svc, 90, "padded", "FSK", NullCrypto(), 0.0, BASE + 40, b"no holes between blocks\n",
                   tx_block=8188)

    print("[5] start_tx with nothing loaded")
    a = await svc._apply(Cmd(60, "init", init_params()))
    a = await svc._apply(Cmd(61, "start_tx", {}))
    check(a.status != CmdStatus.OK, f"start_tx with no waveform and no stream refused ({(a.result or {}).get('err')})")
    await svc._apply(Cmd(62, "close", {}))

    print("[guard] TX running blocks bitpipe_start")
    a = await svc._apply(Cmd(70, "init", init_params()))
    a = await svc._apply(Cmd(71, "set_tx_waveform", {"kind": "tone", "offset_hz": 10000, "ampl": 0.3}))
    a = await svc._apply(Cmd(72, "start_tx", {}))
    if a.status == CmdStatus.OK:
        b = await svc._apply(Cmd(73, "bitpipe_start", {}))
        check(b.status == CmdStatus.REJECTED, "bitpipe_start while a tone transmits refused")
        await svc._apply(Cmd(74, "stop_tx", {}))
    else:
        check(False, f"tone start_tx on the simulator ({(a.result or {}).get('err')})")
    await svc._apply(Cmd(75, "close", {}))


def main():
    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(case_service(tmp))
    check(not _Ep.oversize, f"no message the service sent needed trimming ({_Ep.oversize[:3]})")
    print(f"\n{'PASS' if not FAILS else 'FAIL'}: {len(FAILS)} failure(s)")
    for f in FAILS:
        print("  -", f)
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
