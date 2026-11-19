#!/usr/bin/env python3
"""One simulated radio service whose mock TX goes to another simulated
service instead of back to itself, for dry runs of the two radio harness.

Run two of these, each pointing its --air-out at the other's --air-in:

    python3 bench/bitpipe_pair_sim.py --serial SIM-G40 --cmd-port 48210 --rate-hint 1.92e6 \\
        --air-in 48401 --air-out 48402 --peer-rate 2e6 --cfo 224
    python3 bench/bitpipe_pair_sim.py --serial SIM-B200 --kind b200 --cmd-port 48212 --rate-hint 2e6 \\
        --air-in 48402 --air-out 48401 --peer-rate 1.92e6 --cfo -223

What one service sends is resampled to the peer's rate (linear, state carried
between blocks), shifted by --cfo and written to the peer over TCP; the peer
queues it as received samples. The service's own rate comes from its init.
"""
import argparse, os, socket, struct, sys, threading, time
NG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, NG)
os.environ["DPMG_MOCK_TX_LOOPBACK"] = "1"          # recv() serves the sample queue
import numpy as np
from dpmg.radio import sdr_core as sdr
from dpmg.radio import dpmg_service as svcmod

ap = argparse.ArgumentParser()
ap.add_argument("--serial", required=True)
ap.add_argument("--kind", default="mock")
ap.add_argument("--cmd-port", type=int, required=True)
ap.add_argument("--discovery-port", type=int, default=48311)
ap.add_argument("--air-in", type=int, required=True)
ap.add_argument("--air-out", type=int, required=True)
ap.add_argument("--peer-rate", type=float, required=True)
ap.add_argument("--rate-hint", type=float, default=0.0)
ap.add_argument("--cfo", type=float, default=0.0)
a = ap.parse_args()

CURRENT = {"be": None}
_orig_init = sdr.MockBackend.__init__


def _init(self, *k, **kw):
    _orig_init(self, *k, **kw)
    CURRENT["be"] = self


sdr.MockBackend.__init__ = _init


class Resampler:
    def __init__(self, fin, fout, cfo):
        self.step = fin / fout
        self.pos = 0.0
        self.prev = 0j
        self.cfo, self.fout, self.ph = cfo, fout, 0.0

    def run(self, x):
        buf = np.concatenate([[self.prev], x.astype(np.complex128)])
        last = len(buf) - 1
        n = int(np.floor((last - self.pos) / self.step)) + 1 if last >= self.pos else 0
        p = self.pos + self.step * np.arange(n)
        i = np.minimum(np.floor(p).astype(int), last - 1)
        fr = p - i
        y = buf[i] * (1 - fr) + buf[i + 1] * fr
        self.pos = (self.pos + self.step * n) - last
        self.prev = buf[-1]
        if self.cfo:
            ph = self.ph + 2 * np.pi * self.cfo / self.fout * np.arange(1, n + 1)
            y = y * np.exp(1j * (ph - 2 * np.pi * self.cfo / self.fout))
            self.ph = float(ph[-1] % (2 * np.pi)) if n else self.ph
        return y.astype(np.complex64)


OUT = {"sock": None, "rs": None, "fs": None}
LOCK = threading.Lock()


def _send(self, samples):
    if not getattr(self, "_tx_streaming", False):
        self._tx_streaming = True
    self.health.tx_frames += 1
    fs = float(self.config.sample_rate)
    x = np.asarray(samples, dtype=np.complex64)
    if self._lb_block > 0 and len(x) % self._lb_block:
        x = np.concatenate([x, np.zeros(self._lb_block - len(x) % self._lb_block, dtype=np.complex64)])
    if self._lb_t0 is None:
        self._lb_t0 = time.monotonic()
    self._lb_sent += len(x)
    ahead = self._lb_sent / fs - (time.monotonic() - self._lb_t0)
    if ahead > 0.05:
        time.sleep(ahead - 0.05)
    with LOCK:
        if OUT["rs"] is None or OUT["fs"] != fs:
            OUT["rs"], OUT["fs"] = Resampler(fs, a.peer_rate, a.cfo), fs
        y = OUT["rs"].run(x)
        if OUT["sock"] is None:
            try:
                OUT["sock"] = socket.create_connection(("127.0.0.1", a.air_out), timeout=2)
            except OSError:
                return
        try:
            b = y.tobytes()
            OUT["sock"].sendall(struct.pack("<I", len(b)) + b)
        except OSError:
            OUT["sock"] = None


def _stop_tx(self):
    self._tx_streaming = False
    self._lb_t0, self._lb_sent = None, 0


sdr.MockBackend.send = _send
sdr.MockBackend.stop_tx = _stop_tx


def air_in():
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", a.air_in))
    srv.listen(1)
    while True:
        c, _ = srv.accept()
        f = c.makefile("rb")
        while True:
            h = f.read(4)
            if len(h) < 4:
                break
            n = struct.unpack("<I", h)[0]
            y = np.frombuffer(f.read(n), dtype=np.complex64)
            be = CURRENT["be"]
            if be is None or not getattr(be, "_lb_on", False):
                continue
            with be._lb_lock:
                be._lb.append(y.copy())
                be._lb_have += len(y)
                cap = int(4 * be.config.sample_rate)
                while be._lb_have > cap and be._lb:
                    be._lb_have -= len(be._lb.popleft())


threading.Thread(target=air_in, daemon=True).start()
svcmod.main(["--serial", a.serial, "--kind", a.kind, "--simulate", "--cmd-port", str(a.cmd_port),
             "--discovery-port", str(a.discovery_port), "--broadcast", "127.0.0.1"])
