"""
test_dsss_live_decode.py: live MOD-DSSS / MOD-QPSK-DSSS decode under the
Setup A carrier offset (G40), reproduced offline through the live decode path.

A DSSSDemodulator that estimates one carrier phase per decode window and has
no carrier-offset correction decodes every matrix DSSS / QPSK-DSSS key 0/5 on
Setup A, which shows a -8.5..-11.3 Hz offset (measured on every capture). A
0.9 s window rotates ~9 turns; a frame decodes only when its own rotation
stays inside the +-90 deg (BPSK) / +-45 deg (QPSK) slice, i.e. short frames
at small offsets. That gives "DSSS seed" 3/3 vs "DSSS none R1" 0/5, coded
rates worse than uncoded, and passes at smaller offsets. The channelizer rate
is not the cause: the worker rebuilds its client at 72 kHz whenever the
plan's gate rate changes (a DSSS plan after a DSSS plan logs no [RATE] line
because the rate is already 72 kHz). The de-sloping despreader
(spread_spectrum, M-th-power slope removal) decodes the same captures.

This test drives the real ChannelizerWorker the way the service's _run loop
does (set_plan per key, _ensure_client_rate, _gate_frame, _demod_channels,
the decode pool), with a numpy stand-in for the GPU channelizer service
(gpu_channelizer2 slice math, gpu_service's MAX_OUT=32768 per-channel result
cap, GPU slot powers), over the live plan sequences at a -10 Hz carrier
offset:

  * QPSK 2400 then DSSS (matrix spec, no code_spec) then DSSS again then
    QPSK-DSSS: DSSS seed preset (code_spec, the mod_variants cell); every
    spread plan decodes its message and the client runs at the plan's rate;
  * two on-radio captures (tests/data/dsss_live, channelized at the worker's
    72 kHz rate) decode through decode_pool._default_decode with the
    DecodeSpec the worker builds;
  * [NEED]: a DSSS m-sequence channel (127 chips, gate rate 609.6 kHz) is not
    reported as "can NEVER decode" on its first frame when the two-frame
    window holds the frame (the frame does decode); a FSK 2400 channel at
    30.72 MSps whose two-frame window is too short still is.

Run against another tree (e.g. one whose despreader has no CFO correction,
where the first two checks fail):  DPMG_TEST_TREE=/path/to/tree python3 tests/test_dsss_live_decode.py
pytest collects the test_* functions; `python3 tests/test_dsss_live_decode.py`
exits 0 when all pass.
"""
from __future__ import annotations

import contextlib
import io
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("DPMG_TEST_TREE") or os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import numpy as np

FS = 1_920_000.0             # Setup A rate
CENTER = 425_025_000.0       # Setup A centre
OFF = 300_000.0              # the bench case offset
FRAME = 1_048_576            # dpmg_service: ChannelizerWorker(frame_len=1048576)
MAX_OUT = 32768              # gpu_service.py / channelizer_client.py result cap
SLOT_BW = 20e3
CFO_HZ = -10.0               # measured on the Setup A captures
ESN0_DB = 30.0               # Es/N0 at the channel symbol rate
DATA = os.path.join(HERE, "data", "dsss_live")


class FakeGpuClient:
    """ChannelizerClient stand-in: gpu_channelizer2.GPUChannelizerBatched on
    numpy (one FFT, n_out = frame_len // round(fs_in/fs_out) bins around each
    channel, inverse FFT / decim), gpu_service's MAX_OUT cap on what goes back
    per channel, and the 20 kHz slot powers from the same FFT."""
    built: list = []

    def __init__(self, frame_len: int = 131072, fs_out: float = 250_000.0):
        self.frame_len = int(frame_len)
        self.fs_out = float(fs_out)
        self._chans = []
        self._slots = (None, None)
        FakeGpuClient.built.append(self.fs_out)

    def set_channels(self, channels):
        self._chans = list(channels)[:30]

    def process(self, wideband, fs_in, capture_center, timeout_s=1.0):
        if not self._chans:
            return {}
        N = self.frame_len
        X = np.fft.fft(np.asarray(wideband[:N], dtype=np.complex64))
        decim = max(1, int(round(float(fs_in) / self.fs_out)))
        n_out = N // decim
        half = n_out // 2
        out = {}
        for i, (fcen, _bw) in enumerate(self._chans):
            cb = int(round((fcen - capture_center) / (float(fs_in) / N)))
            bins = np.roll((np.arange(-half, -half + n_out) + cb) % N, -half)
            bb = (np.fft.ifft(X[bins]) / decim).astype(np.complex64)
            out[i] = bb[:MAX_OUT].copy()
        p = np.fft.fftshift(X.real ** 2 + X.imag ** 2)
        lo = -float(fs_in) / 2 + 0.10 * float(fs_in)
        n_slots = int(np.floor(0.8 * float(fs_in) / SLOT_BW))
        e = np.clip(np.round((lo + SLOT_BW * np.arange(n_slots + 1) + float(fs_in) / 2)
                             / (float(fs_in) / N)).astype(np.int64), 0, N)
        cs = np.concatenate(([0.0], np.cumsum(p)))
        sp = np.maximum(cs[e[1:]] - cs[e[:-1]], 0.0)
        self._slots = ((10 * np.log10(sp + 1e-20)).astype(np.float32), (lo, SLOT_BW))
        return out

    def last_slot_powers(self):
        return self._slots

    def slot_age(self):
        return 0

    def close(self):
        pass


def _build(specs, out_dir):
    """gen_scenario_json in a child (the scenario TX side is not under test and
    must build the same loop whichever tree DPMG_TEST_TREE points at)."""
    repo = os.path.dirname(HERE)
    path = os.path.join(out_dir, "specs.json")
    json.dump(specs, open(path, "w"))
    code = ("import sys, json; sys.path.insert(0, sys.argv[1]); from dpmg.scenario import gen_scenario_json as g; "
            "[g.build_scenario(s, sys.argv[2]) for s in json.load(open(sys.argv[3]))]")
    subprocess.run([sys.executable, "-c", code, repo, out_dir, path], check=True,
                   stdout=subprocess.DEVNULL)


def _spec(name, mod, msg, extra=None, loop_s=0.2):
    sig = {"modulation": mod, "symbol_rate": 2400, "tone_spacing_hz": 5000,
           "offset_hz": OFF, "message": msg, "amplitude": 0.9}
    sig.update(extra or {})
    return {"name": name, "center_freq_hz": CENTER, "loop_s": loop_s,
            "sample_rate_hz": FS, "signals": [sig]}


class _Rx:
    """The RX stream of one key: the TX loop from a random arrival offset,
    carrier offset, AWGN at Es/N0 (channel symbol rate)."""

    def __init__(self, loop, rng, cfo_hz=CFO_HZ, esn0_db=ESN0_DB, sr=2400.0):
        self.loop = np.asarray(loop, dtype=np.complex64)
        self.pos = int(rng.integers(0, len(self.loop)))
        a = np.abs(self.loop) ** 2
        pb = float(np.mean(a[a > 0.25 * a.max()]))
        self.ns = math.sqrt(pb / sr / 10 ** (esn0_db / 10) * FS / 2)
        self.cfo, self.t, self.rng = float(cfo_hz), 0, rng

    def frame(self):
        idx = (self.pos + np.arange(FRAME)) % len(self.loop)
        self.pos = int((self.pos + FRAME) % len(self.loop))
        x = self.loop[idx] * np.exp(2j * np.pi * self.cfo * (self.t + np.arange(FRAME)) / FS)
        self.t += FRAME
        n = (self.rng.standard_normal(FRAME) + 1j * self.rng.standard_normal(FRAME)) * self.ns
        return (x + n).astype(np.complex64)


def _worker():
    """The service's worker (ChannelizerWorker(frame_len=1048576, fs_out=250e3),
    started: client at 250 kHz), no thread; a thread-backend decode pool."""
    from dpmg.dsp import channelizer_worker as cw
    from dpmg.dsp.decode_pool import DecodePool
    cw.ChannelizerClient = FakeGpuClient
    w = cw.ChannelizerWorker(frame_len=FRAME, fs_out=250_000.0)
    w.log_key = "test_dsss_live"
    w._client = FakeGpuClient(w.frame_len, w.fs_out)
    w._pool = DecodePool(max_workers=2, backend="thread")
    return w


def _key(w, plan, rx, n_frames=6):
    """One key: set_plan (as _cmd_set_plan: _chan_start), then n_frames
    frames through the _run loop body. Returns the messages decoded."""
    w.set_plan(plan)
    got = []
    for _ in range(n_frames):
        frame = rx.frame()
        w._fs_in, w._capture_center = FS, CENTER
        w._ensure_client_rate()
        w._gate_frame(frame)
        if not w._channels:
            continue
        out = w._client.process(frame, w._fs_in, w._capture_center)
        w._demod_channels(out)
        t_end = time.time() + 120
        while w._pool.busy() and time.time() < t_end:
            time.sleep(0.005)
        for r in w._pool.collect():
            got += list(r.messages or ([r.message] if r.valid else []))
        # the pool's 3-miss cooldown is wall-clock (3 s of air); frames run
        # faster than air here, so clear it like bench/replay_chain.decode
        for lk in w._pool._locks.values():
            lk.pop("cooldown_until", None)
    return got


# pytest entry points

def test_live_plan_sequence_decodes_dsss_at_link_cfo():
    """QPSK then DSSS then DSSS then QPSK-DSSS then DSSS seed preset, -10 Hz offset:
    each spread plan decodes, and the client runs at the plan's gate rate."""
    from dpmg.scenario.plan_adapter import load_plan
    tmp = tempfile.mkdtemp(prefix="dsss_live_")
    try:
        seq = [("lv_qpsk", "QPSK", "MOD17 QPSK2400", None, 0.2),
               ("lv_dsss", "DSSS", "DSSS none R1", None, 0.2),          # matrix cell
               ("lv_dsss2", "DSSS", "DSSS none R2", None, 0.2),         # DSSS after DSSS
               ("lv_qdsss", "QPSK-DSSS", "QPSK-DSSS none R1", None, 0.2),
               ("lv_seed", "DSSS", "DSSS seed", {"code_spec": {"family": "seed"}}, 0.25)]
        _build([_spec(n, m, msg, ex, ls) for n, m, msg, ex, ls in seq], tmp)
        rng = np.random.default_rng(0xD555)
        w = _worker()
        fails = []
        for n, m, msg, _ex, _ls in seq:
            plan = load_plan(os.path.join(tmp, f"{n}.plan.json"))
            loop = np.fromfile(os.path.join(tmp, f"{n}.iq.bin"), dtype=np.complex64)
            got = _key(w, plan, _Rx(loop, rng))
            want_fs = 24000.0 if m == "QPSK" else 72000.0
            ok = msg in got and abs(w._client.fs_out - want_fs) < 1.0
            print(f"  {n:10s} {m:10s} client {w._client.fs_out:7.0f} Hz  decoded "
                  f"{sum(1 for g in got if g == msg)}  {'ok' if ok else 'MISS'}")
            if not ok:
                fails.append(n)
        w._pool.shutdown()
        assert not fails, f"live-path DSSS keys missed at {CFO_HZ} Hz: {fails}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_onradio_captures_decode_through_worker_spec():
    """The on-radio rep-1 / probe captures (channelized at 72 kHz) decode with
    the DecodeSpec the worker submits (fs_out_act, sps = fs_out_act / Rs)."""
    from dpmg.scenario.channel_plan import ChannelPlan
    from dpmg.dsp.decode_pool import DecodeSpec, _default_decode
    from dpmg.scenario.plan_adapter import plan_to_watchlist
    fails = []
    for name in ("g40_dsss_none_r1", "g40_qpsk_dsss_probe"):
        meta = json.load(open(os.path.join(DATA, name + ".json")))
        bb = np.load(os.path.join(DATA, name + ".npy"))
        cc = plan_to_watchlist(ChannelPlan.from_dict(meta["plan"]))[0][0]
        fs = float(meta["fs_out"])
        spec = DecodeSpec(modulation=cc.demod_type, fs_out=fs, sps=fs / cc.symbol_rate,
                          demod_params={"symbol_rate": cc.symbol_rate, **cc.demod_params},
                          lock={})
        r = _default_decode(0, bb, spec)
        ok = bool(r.valid) and meta["message"] in (r.messages or [r.message])
        print(f"  {name:22s} {cc.demod_type:9s} valid={r.valid} frames={len(r.messages or [])}")
        if not ok:
            fails.append(name)
    assert not fails, f"on-radio captures did not decode: {fails}"


class _Pool:
    def __init__(self):
        self.n = 0

    def is_healthy(self):
        return True

    def submit(self, *_a, **_k):
        self.n += 1
        return True


def _need_lines(plan, fs_in, n_frames=2):
    """[NEED] lines the worker prints over n_frames of _demod_channels for the
    plan's channel (gate forced open), and how many decodes it submitted."""
    from dpmg.dsp import channelizer_worker as cw
    w = cw.ChannelizerWorker(frame_len=FRAME, fs_out=250_000.0)
    w._fs_in, w._capture_center = fs_in, CENTER
    w.set_plan(plan)
    w.fs_out = float(w._gate_fs_out)
    w._targeted_gate = lambda ch: True
    w._pool = _Pool()
    n_out = FRAME // max(1, int(round(fs_in / w.fs_out)))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        for _ in range(n_frames):
            w._demod_channels({0: np.zeros(min(n_out, MAX_OUT), dtype=np.complex64)})
    return [ln for ln in buf.getvalue().splitlines() if ln.startswith("[NEED]")], w._pool.n


def test_need_warning_uses_the_two_frame_window():
    """DSSS m (127 chips, 609.6 kHz, 32768 of 349525 samples back per frame):
    the first frame alone is short, the two-frame window is not: no
    'can NEVER decode', and it is submitted from the second frame. FSK 2400 at
    30.72 MSps (819 samples per frame, window 1630 < need 1760) still warns."""
    from dpmg.scenario.plan_adapter import load_plan
    tmp = tempfile.mkdtemp(prefix="dsss_need_")
    try:
        sp_fsk = _spec("nd_fsk", "FSK", "MOD17 FSK2400")
        sp_fsk["sample_rate_hz"] = 30.72e6
        _build([_spec("nd_m", "DSSS", "DSSS m", {"code_spec": {"family": "m"}}, 0.25), sp_fsk], tmp)
        pm = load_plan(os.path.join(tmp, "nd_m.plan.json"))
        assert int(pm.entries[0].params.get("spread_factor")) == 127
        lines, n = _need_lines(pm, FS)
        print(f"  DSSS m @1.92: [NEED] lines {lines}  submitted {n}")
        assert not any("NEVER" in ln for ln in lines), lines
        assert n >= 1, "two-frame window never submitted"
        lines, n = _need_lines(load_plan(os.path.join(tmp, "nd_fsk.plan.json")), 30.72e6)
        print(f"  FSK 2400 @30.72: [NEED] lines {lines}  submitted {n}")
        assert any("NEVER" in ln for ln in lines) and n == 0, lines
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    bad = 0
    for fn in (test_live_plan_sequence_decodes_dsss_at_link_cfo,
               test_onradio_captures_decode_through_worker_spec,
               test_need_warning_uses_the_two_frame_window):
        print(fn.__name__)
        try:
            fn()
            print("  PASS")
        except AssertionError as e:
            bad += 1
            print(f"  FAIL {e}")
    print("ALL PASS" if not bad else f"{bad} FAILED")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
