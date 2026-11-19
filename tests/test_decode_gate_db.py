"""
test_decode_gate_db.py: opt-in per-plan-channel decode gate.

The FEC-03 / M-08 sweep scenario (FSK 4800 Bd / 5 kHz at +300 kHz, 1.92 MSps,
see tests/test_fsk_fec_snr_replay.py) runs through the real
channelizer_worker._gate_frame + _targeted_gate with GPU slot powers modelled
as in that test's gate_open (one FFT over a 1M frame, 20 kHz slots from
-fs/2 + 10 %), fed through a stand-in for ChannelizerClient.last_slot_powers.

  * plumbing: signal "decode_gate_db" then plan params then cc.demod_params
    then the worker's ChannelRuntime; absent when the signal does not set it;
  * default (key absent): the 12 dB detection gate stays shut at Es/N0 12 dB
    (it opens from ~19 dB): the stock behaviour;
  * opt-in 3 dB: the same channel is admitted at Es/N0 12 dB, and the
    admitted window decodes conv 1/2 there;
  * opt-in on noise only (signal absent) stays shut; with no slot powers yet
    the opt-in channel falls back to the stock gate.

pytest collects test_*; `python3 tests/test_decode_gate_db.py` exits 0 on pass.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import numpy as np

from dpmg.scenario import gen_scenario_json as g
from dpmg.dsp.channelizer_worker import ChannelizerWorker, CONT_PERIOD
from dpmg.scenario.plan_adapter import load_plan, plan_to_watchlist
from test_fsk_fec_snr_replay import (FS, OFF, SR, _burst_power, decode_rate, impair,
                                     worker_fs_out)

SLOT_BW = 20e3


def build(fec, out_dir, gate_db=None):
    """The sweep's scenario (optionally with decode_gate_db): (msg, iq, cc, plan, fs_out)."""
    msg = f"SNR FSK {fec}"
    sig = {"modulation": "FSK", "symbol_rate": SR, "tone_spacing_hz": 5000,
           "offset_hz": OFF, "message": msg, "amplitude": 0.9}
    if fec != "none":
        sig["fec"] = {"type": "conv", "rate": fec}
    if gate_db is not None:
        sig["decode_gate_db"] = gate_db
    name = "gate_" + fec.replace("/", "") + ("" if gate_db is None else f"_g{gate_db:g}")
    g.build_scenario({"name": name, "sample_rate_hz": FS, "center_freq_hz": 425_025_000.0,
                      "loop_s": 0.2, "signals": [sig]}, out_dir)
    plan = load_plan(os.path.join(out_dir, f"{name}.plan.json"))
    iq = np.fromfile(os.path.join(out_dir, f"{name}.iq.bin"), dtype=np.complex64)
    wl = plan_to_watchlist(plan)
    return msg, iq, wl[0][0], plan, worker_fs_out(wl, FS)


def _tile(iq):
    return np.tile(iq, 1048576 // len(iq) + 1)[:1048576]


def slot_powers_of(x):
    """GPU slot powers for one 1M frame, as test_fsk_fec_snr_replay.gate_open:
    one FFT, 20 kHz slots from -fs/2 + 10 %: (slot_db, slot_lo_hz)."""
    N = len(x)
    P = np.fft.fftshift(np.abs(np.fft.fft(x)) ** 2)
    cs = np.concatenate(([0.0], np.cumsum(P)))
    lo = -FS / 2 + 0.1 * FS
    e = lo + SLOT_BW * np.arange(int(np.floor(0.8 * FS / SLOT_BW)) + 1)
    eb = np.clip(np.round((e + FS / 2) / (FS / N)).astype(int), 0, N)
    return 10 * np.log10(cs[eb[1:]] - cs[eb[:-1]] + 1e-20), lo


def slot_powers(iq, esn0_db, rng):
    """Signal + AWGN at Es/N0 (impair): slot powers."""
    return slot_powers_of(impair(_tile(iq), esn0_db, rng))


def noise_powers(iq, esn0_db, rng):
    """The AWGN alone, at the level impair() adds for esn0_db: slot powers."""
    n0 = _burst_power(iq) / SR / 10 ** (esn0_db / 10)
    n = (rng.standard_normal(1048576) + 1j * rng.standard_normal(1048576)) * np.sqrt(n0 * FS / 2)
    return slot_powers_of(n.astype(np.complex64))


class _Slots:
    """Stand-in for ChannelizerClient: only the slot-power accessors."""
    def __init__(self, sdb, lo):
        self._s = (sdb, (lo, SLOT_BW)) if sdb is not None else (None, None)

    def last_slot_powers(self):
        return self._s

    def slot_age(self):
        return 0

    def set_channels(self, *_a, **_k):
        pass


def admitted(plan, client):
    """Run the worker's own gate on one frame: _gate_frame (detection set) then
    _targeted_gate for the plan's single continuous channel on its phase."""
    w = ChannelizerWorker(frame_len=1048576, fs_out=250_000.0)   # no thread started
    w._fs_in = FS
    w._capture_center = float(plan.center_freq_hz)
    w.set_plan(plan)
    w._client = client
    w._gate_frame(None)
    ch = w._channels[0]
    assert ch.is_continuous, "sweep channel must be continuous"
    w._frame_ctr = int(ch.idx) % CONT_PERIOD
    return w._targeted_gate(ch), ch


# pytest entry points

def test_plumbing():
    tmp = tempfile.mkdtemp(prefix="dgate_")
    try:
        _m, _iq, cc0, plan0, _f = build("1/2", tmp)
        _m, _iq, cc3, plan3, _f = build("1/2", tmp, gate_db=3)
        assert "decode_gate_db" not in (cc0.demod_params or {})
        assert cc3.demod_params.get("decode_gate_db") == 3.0
        assert plan3.entries[0].params.get("decode_gate_db") == 3.0
        _ok, ch = admitted(plan3, _Slots(None, None))
        assert ch.demod_params.get("decode_gate_db") == 3.0     # reaches the runtime
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_default_shut_optin_open_at_12db():
    rng = np.random.default_rng(0xD6A7)
    tmp = tempfile.mkdtemp(prefix="dgate_")
    try:
        case0 = build("1/2", tmp)
        case3 = build("1/2", tmp, gate_db=3)
        for _ in range(3):
            sdb, lo = slot_powers(case0[1], 12.0, rng)
            cli = _Slots(sdb, lo)
            ok0, _ = admitted(case0[3], cli)
            ok3, _ = admitted(case3[3], cli)
            assert not ok0, "stock 12 dB gate must stay shut at Es/N0 12 dB"
            assert ok3, "decode_gate_db=3 must admit the channel at Es/N0 12 dB"
        # the stock gate still opens where it always did (Es/N0 ~19 dB+)
        sdb, lo = slot_powers(case0[1], 22.0, rng)
        assert admitted(case0[3], _Slots(sdb, lo))[0]
        # an admitted window at 12 dB actually decodes (conv 1/2 edge ~4 dB)
        assert decode_rate(case3[:5], 12.0, 2, rng) >= 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_optin_shut_on_noise_and_fallback():
    rng = np.random.default_rng(0xD6A8)
    tmp = tempfile.mkdtemp(prefix="dgate_")
    try:
        case3 = build("1/2", tmp, gate_db=3)
        for _ in range(3):
            sdb, lo = noise_powers(case3[1], 12.0, rng)
            assert not admitted(case3[3], _Slots(sdb, lo))[0], "noise-only slot admitted"
        # no slot powers yet (cold start): the opt-in channel uses the stock gate
        assert not admitted(case3[3], _Slots(None, None))[0]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    fails = 0
    for t in (test_plumbing, test_default_shut_optin_open_at_12db,
              test_optin_shut_on_noise_and_fallback):
        try:
            t()
            print(f"  [PASS] {t.__name__}")
        except AssertionError as e:
            fails += 1
            print(f"  [FAIL] {t.__name__}: {e}")
    print(f"result: {3 - fails}/3 passed")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
