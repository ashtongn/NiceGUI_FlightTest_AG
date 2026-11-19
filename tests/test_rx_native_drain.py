#!/usr/bin/env python3
"""
RX native drain: offline
============================================
rxdrain.c must give the scheduler exactly what the Python per-block path gives.
A scripted stand-in for libsidekiq's skiq_receive (built here with gcc) serves
6000 blocks of known int16 data with a forward timestamp gap, a backward jump
(replaying FIFO) and an empty read every 700 blocks. The real
FullDuplexScheduler._rx_loop runs once per engine and IQ order; checks:

  1. same frames (rf_timestamp, length) and bit-identical samples, both engines
  2. samples equal the ground truth (QI on the wire: I + jQ, scale 1/32768)
  3. same overrun count (forward gaps), same [RXGAP] mismatches, replay flagged
  4. receive_batch: a block cap never drops a block; the next call goes on
  5. DPMG_RX_NATIVE=0 or a missing library falls back to the Python path

    python3 tests/test_rx_native_drain.py      (or pytest)
SKIP (exit 0) when gcc is not available.
"""
import contextlib
import ctypes
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

N_BLOCKS = 6000
GAP_AT, GAP_SAMPLES = 2500, 5000          # forward jump (overrun)
BACK_AT, BACK_SAMPLES = 4200, 10 * 1018   # backward jump (replay)
HOLE_EVERY = 700                          # one empty read before every 700th block

FAKE_C = r"""
#include <stdint.h>
#include <string.h>
static uint32_t blk[2][1024];
static uint64_t idx, ts, n_total; static int k, hole_done;
static uint64_t gap_at, gap_n, back_at, back_n, hole_every;
void fake_setup(uint64_t n, uint64_t ga, uint64_t gn, uint64_t ba, uint64_t bn, uint64_t he) {
    idx = 0; ts = 1000; n_total = n; gap_at = ga; gap_n = gn; back_at = ba; back_n = bn;
    hole_every = he; hole_done = 0; k = 0;
}
static int16_t val(uint64_t b, int i) { uint32_t x = (uint32_t)(b * 2654435761u + i * 40503u);
    x ^= x >> 13; x *= 0x5bd1e995u; x ^= x >> 15; return (int16_t)(x & 0xffff); }
int32_t skiq_receive(uint8_t card, int *hdl, void **pb, uint32_t *len) {
    (void)card;
    if (idx >= n_total) { *pb = 0; return -1; }
    if (hole_every && idx && idx % hole_every == 0 && !hole_done) { hole_done = 1; *pb = 0; return -1; }
    hole_done = 0;
    if (idx == gap_at) ts += gap_n;
    if (idx == back_at) ts -= back_n;
    uint32_t *b = blk[k ^= 1];
    memcpy(b, &ts, 8); memset(b + 2, 0, 16);
    int16_t *d = (int16_t *)(b + 6);
    for (int i = 0; i < 2036; i++) d[i] = val(idx, i);
    ts += 1018; idx++;
    *hdl = 0; *pb = b; *len = 4096; return 0;
}
"""

_checks = []


def check(ok, what):
    _checks.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + what)


def truth_block(b):
    """The stub's int16 payload for block b, as the card's QI words."""
    i = np.arange(2036, dtype=np.uint64)
    x = (np.uint64(b) * np.uint64(2654435761) + i * np.uint64(40503)) & np.uint64(0xFFFFFFFF)
    x = x.astype(np.uint32)
    x ^= x >> np.uint32(13)
    x = (x.astype(np.uint64) * np.uint64(0x5BD1E995) & np.uint64(0xFFFFFFFF)).astype(np.uint32)
    x ^= x >> np.uint32(15)
    return (x & np.uint32(0xFFFF)).astype(np.uint16).view(np.int16)


def build(tmp):
    cc = shutil.which("gcc") or shutil.which("cc")
    if cc is None:
        return None
    (tmp / "fake.c").write_text(FAKE_C)
    subprocess.check_call([cc, "-O2", "-shared", "-fPIC", "-o", str(tmp / "libfake.so"),
                           str(tmp / "fake.c")])
    subprocess.check_call([cc, "-O2", "-shared", "-fPIC", "-o", str(tmp / "librxdrain.so"),
                           str(ROOT / "native" / "rxdrain.c")])
    return tmp


def make_backend(sc, sb, fake, iq_order_iq):
    lib = ctypes.CDLL(str(fake))
    c = ctypes
    lib.skiq_receive.restype = c.c_int32
    lib.skiq_receive.argtypes = [c.c_uint8, c.POINTER(c.c_int), c.POINTER(c.c_void_p),
                                 c.POINTER(c.c_uint32)]
    lib.fake_setup.argtypes = [c.c_uint64] * 6
    lib.fake_setup(N_BLOCKS, GAP_AT, GAP_SAMPLES, BACK_AT, BACK_SAMPLES, HOLE_EVERY)
    br = object.__new__(sb.SkiqBridge)
    br.lib = lib
    E = sc.EpiqBackend

    class Cfg:
        sample_rate = 61.44e6
        center_freq = 2.4e9

    class Be:
        _rx_batch_s = 0.05
        native_drain_ok = E.native_drain_ok
        recv_batch = E.recv_batch
        recv_raw = E.recv_raw
        restart_rx_stream = lambda self: True

        def __init__(self):
            self._skiq = br; self._card = 0; self.health = sc.StreamHealth()
            self.config = Cfg(); self._achieved_rate = 61.44e6; self._seq = 0
            self._iq_order_iq = iq_order_iq
    return Be(), br


def run(sc, sb, fake, native, iq_order_iq):
    os.environ["DPMG_RX_NATIVE"] = "1" if native else "0"
    be, br = make_backend(sc, sb, fake, iq_order_iq)
    s = sc.FullDuplexScheduler(be, rx_pace_s=0.005)
    frames = []
    s.frame_hook = lambda f: frames.append((int(f.rf_timestamp), np.array(f.samples)))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        s.start_rx()
        t_end = time.time() + 30
        while s.metrics.frames < N_BLOCKS and time.time() < t_end:
            time.sleep(0.01)
        time.sleep(0.05)
        s.stop_rx()
    log = out.getvalue()
    return dict(frames=frames, log=log, blocks=s.metrics.frames,
                overruns=getattr(br, "rx_overruns", {}).get(0, 0),
                health_over=be.health.overruns,
                replay=getattr(s, "_rx_replay_t", None) is not None,
                post_timed=hasattr(s, "_drain_post"),
                engine="native" if "drain engine: native" in log else "python")


def mismatches(log):
    n = 0
    for line in log.splitlines():
        if line.startswith("[RXGAP]"):
            n += 1
            if "more in this batch" in line:
                n += int(line.split("(+")[1].split()[0])
    return n


def main():
    os.environ.setdefault("DPMG_RX_NO_RT", "1")
    with tempfile.TemporaryDirectory() as td:
        tmp = build(Path(td))
        if tmp is None:
            print("SKIP no C compiler")
            return 0
        os.environ["DPMG_RXDRAIN_LIB"] = str(tmp / "librxdrain.so")
        from dpmg.radio import sdr_core as sc
        from dpmg.radio import skiq_bridge as sb
        fake = tmp / "libfake.so"

        truth = np.concatenate([truth_block(b) for b in range(N_BLOCKS)]).astype(np.float32)
        truth *= 1.0 / 32768.0
        for iq in (False, True):
            want = (truth[1::2] + 1j * truth[0::2] if not iq else
                    truth[0::2] + 1j * truth[1::2]).astype(np.complex64)
            py = run(sc, sb, fake, native=False, iq_order_iq=iq)
            nat = run(sc, sb, fake, native=True, iq_order_iq=iq)
            tag = f"iq_order={'IQ' if iq else 'QI'}"
            check(py["engine"] == "python" and nat["engine"] == "native",
                  f"{tag}: engines python / native ({py['engine']} / {nat['engine']})")
            check(py["blocks"] == nat["blocks"] == N_BLOCKS,
                  f"{tag}: {N_BLOCKS} blocks each ({py['blocks']} / {nat['blocks']})")
            fp = [(t, len(x)) for t, x in py["frames"]]
            fn = [(t, len(x)) for t, x in nat["frames"]]
            check(fp == fn, f"{tag}: same frames (ts, length): {len(fp)} / {len(fn)}")
            sp = np.concatenate([x for _, x in py["frames"]])
            sn = np.concatenate([x for _, x in nat["frames"]])
            check(sp.tobytes() == sn.tobytes(), f"{tag}: samples bit-identical")
            check(sn.tobytes() == want.tobytes(), f"{tag}: samples equal the ground truth")
            check(py["overruns"] == nat["overruns"] == 1 and nat["health_over"] == 1,
                  f"{tag}: overruns {py['overruns']} / {nat['overruns']} (want 1)")
            check(mismatches(py["log"]) == mismatches(nat["log"]) == 2,
                  f"{tag}: [RXGAP] mismatches {mismatches(py['log'])} / {mismatches(nat['log'])} (want 2)")
            check(py["replay"] and nat["replay"], f"{tag}: backward jump flags replay")
            check(py["post_timed"] and nat["post_timed"], f"{tag}: post-batch time measured")

        # 4. block cap: no block lost between calls
        os.environ["DPMG_RX_NATIVE"] = "1"
        be, br = make_backend(sc, sb, fake, False)
        ts = []
        for _ in range(4):
            chunk, st = br.receive_batch(0, 5, 0.05, False)
            ts.append((int(st.ts0), int(st.n_blocks), len(chunk)))
        check([t[1] for t in ts] == [5] * 4 and all(t[2] == 5 * 1018 for t in ts),
              f"cap 5: four calls of 5 blocks ({[t[1] for t in ts]})")
        check([t[0] for t in ts] == [1000 + 5 * 1018 * k for k in range(4)],
              "cap 5: each call starts at the next block's timestamp")

        # 5. fallbacks
        os.environ["DPMG_RX_NATIVE"] = "0"
        check(sb.load_rxdrain()[0] is None, "DPMG_RX_NATIVE=0 -> no native drain")
        os.environ["DPMG_RX_NATIVE"] = "1"
        check(sb.load_rxdrain(str(tmp / "missing.so"))[0] is None,
              "missing library -> no native drain")
        check(sb.load_rxdrain()[0] is not None, "built library loads (ABI ok)")
    n_ok = sum(_checks)
    print(f"{n_ok}/{len(_checks)} checks passed")
    return 0 if n_ok == len(_checks) else 1


def test_rx_native_drain():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
