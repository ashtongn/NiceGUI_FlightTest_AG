"""
The laptop peer: one bitpipe container on the ground station, optionally with
its link panel and an offline far end.

    python3 -m dpmg.bitpipe.peer --name ground --bit-host 0.0.0.0 --crypto secure
        a container whose bit face a radio's modem connects to (bitpipe_start
        container=tcp:<laptop ip>:47899); keys from the key file on this host
    python3 -m dpmg.bitpipe.peer --panel 8095
        the same with the link panel served at http://127.0.0.1:8095
    python3 -m dpmg.bitpipe.peer --sim --panel 8095
        no radio: a far container, two modem links and a simulated channel
        (QPSK, 10 dB, 200 Hz, 10 ppm by default), both panels side by side

The container's faces: chat on app_base_port+1, files on +2, the control
face on ctl_port, the bit face on bit_port (or Unix sockets with --transport
unix). Stop with Ctrl-C.

Key custody: with --crypto secure the key comes through the file provider
(DPMG_KEY_FILE or --key-file), which belongs on the x86. A container on a
radio host runs clear: the device key scheme is not implemented. The bit face only
ever carries sealed frames when the link is secure, but on a LAN it carries
them in the clear when it is not.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import threading
import time

from .config import BitpipeConfig
from .container import BitpipeContainer
from .crypto import FileKeyProvider, NullCrypto, SecureContextCrypto, context_from_provider, loopback_crypto_pair

log = logging.getLogger("dpmg.bitpipe.peer")


def make_cfg(a, name=None, port_shift=0) -> BitpipeConfig:
    fec = None if a.fec == "none" else {"type": a.fec}
    if fec and a.fec in ("conv", "rs+conv"):
        fec["rate"] = a.rate
    return BitpipeConfig(name=name or a.name, payload_bytes=a.payload, fec=fec, transport=a.transport,
                         bit_host=a.bit_host, bit_port=a.bit_port + port_shift, ctl_port=a.ctl_port + port_shift,
                         app_base_port=a.app_port + port_shift, unix_dir=a.unix_dir,
                         crypto="secure" if a.crypto == "secure" else "none")


class SimFarEnd:
    """A second container, two modem links and the channels: the far radio in software."""

    def __init__(self, a, near: BitpipeContainer, crypto_far):
        from .modem_link import ModemLink
        from .sim_link import SimRadioPair
        from .stream_modem import IqChannel, WaveformConfig
        cfg_b = make_cfg(a, name=f"{a.name}-far", port_shift=20)
        cfg_b.bit_host = "127.0.0.1"
        self.far = BitpipeContainer(cfg_b, crypto_far)
        wf = WaveformConfig(modulation=a.modulation, symbol_rate=a.symbol_rate, deviation_hz=a.deviation)
        fs = a.sim_fs
        host = "127.0.0.1" if near.cfg.bit_host in ("0.0.0.0", "") else near.cfg.bit_host
        near_ep = near.bit_endpoint()
        if near_ep.kind == "tcp":
            near_ep.host = host
        self.ma = ModemLink(wf, near_ep.describe(), fs, fs, block_s=0.05, name="sim-near")
        self.mb = ModemLink(wf, self.far.bit_endpoint().describe(), fs, fs, block_s=0.05, name="sim-far")
        self.pair = SimRadioPair(self.ma, self.mb, IqChannel(fs, a.snr, a.cfo, a.ppm, 7, seed=1),
                                 IqChannel(fs, a.snr, -a.cfo, -a.ppm, 3, seed=2), realtime=True)

    def start(self):
        self.far.start()
        self.ma.start()
        self.mb.start()
        t0 = time.monotonic()
        while not (self.ma.connected() and self.mb.connected()) and time.monotonic() - t0 < 10:
            time.sleep(0.05)
        self.pair.start()

    def stop(self):
        self.pair.stop()
        self.ma.stop()
        self.mb.stop()
        self.far.stop()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="bitpipe laptop peer")
    ap.add_argument("--name", default="ground")
    ap.add_argument("--transport", default="tcp", choices=("tcp", "unix"))
    ap.add_argument("--bit-host", default="127.0.0.1", help="0.0.0.0 to let a radio's modem connect over the LAN")
    ap.add_argument("--bit-port", type=int, default=47899)
    ap.add_argument("--ctl-port", type=int, default=47898)
    ap.add_argument("--app-port", type=int, default=47900, help="application faces at this + stream id")
    ap.add_argument("--unix-dir", default="/tmp/dpmg-bitpipe")
    ap.add_argument("--payload", type=int, default=64)
    ap.add_argument("--fec", default="conv")
    ap.add_argument("--rate", default="1/2")
    ap.add_argument("--crypto", default="none", choices=("none", "secure"))
    ap.add_argument("--key-file", default=None, help="key file for --crypto secure (default DPMG_KEY_FILE)")
    ap.add_argument("--panel", type=int, default=0, help="serve the link panel on this port")
    ap.add_argument("--sim", action="store_true", help="add an offline far end (no radio)")
    ap.add_argument("--modulation", default="QPSK")
    ap.add_argument("--symbol-rate", type=float, default=2400.0)
    ap.add_argument("--deviation", type=float, default=1200.0)
    ap.add_argument("--sim-fs", type=float, default=38400.0)
    ap.add_argument("--snr", type=float, default=10.0)
    ap.add_argument("--cfo", type=float, default=200.0)
    ap.add_argument("--ppm", type=float, default=10.0)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    cfg = make_cfg(a)
    far_crypto = None
    if a.crypto == "secure":
        if a.sim and not a.key_file and not os.environ.get("DPMG_KEY_FILE"):
            near_crypto, far_crypto = loopback_crypto_pair()        # throw away keys for the offline demo
        else:
            os.environ.setdefault("DPMG_SECURE_MSG", "1")
            near_crypto = SecureContextCrypto(context_from_provider(FileKeyProvider(a.key_file), None,
                                                                    cfg.freshness_slots))
            if a.sim:
                far_crypto = SecureContextCrypto(context_from_provider(
                    FileKeyProvider(a.key_file), (near_crypto.ctx.store.sender_id + 1) & 0xFF, cfg.freshness_slots))
    else:
        near_crypto, far_crypto = NullCrypto(), NullCrypto()
    near = BitpipeContainer(cfg, near_crypto)
    near.start()
    print(f"bitpipe peer {cfg.name}: bit face {near.bit_endpoint().describe()}, control "
          f"{near.ctl_endpoint().describe()}, chat {near.app_endpoint(1).describe()}, "
          f"files {near.app_endpoint(2).describe()}, crypto {near_crypto.name}", flush=True)
    far = None
    if a.sim:
        far = SimFarEnd(a, near, far_crypto)
        far.start()
        print(f"offline far end {far.far.cfg.name}: {a.modulation} {a.symbol_rate:g} Bd, snr {a.snr} dB, "
              f"cfo {a.cfo} Hz, {a.ppm} ppm; its control {far.far.ctl_endpoint().describe()}", flush=True)

    if a.panel:
        from nicegui import ui
        from .link_panel import panel_for_config

        @ui.page("/")
        def _page():
            ui.page_title("Bitpipe link")
            with ui.row().classes("w-full items-start"):
                with ui.column().classes("grow min-w-[380px]"):
                    panel_for_config(cfg, f"Bitpipe {cfg.name}")
                if far is not None:
                    with ui.column().classes("grow min-w-[380px]"):
                        panel_for_config(far.far.cfg, f"Bitpipe {far.far.cfg.name} (simulated)",
                                         inbox=os.path.join(os.path.expanduser("~"), ".dpmg", "bitpipe", "inbox-far"))
        try:
            ui.run(host="127.0.0.1", port=a.panel, reload=False, show=False, title="Bitpipe link")
        finally:
            if far is not None:
                far.stop()
            near.stop()
        return 0

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    while not stop.is_set():
        stop.wait(1.0)
    if far is not None:
        far.stop()
    near.stop()
    return 0


if __name__ in ("__main__", "__mp_main__"):
    raise SystemExit(main())
