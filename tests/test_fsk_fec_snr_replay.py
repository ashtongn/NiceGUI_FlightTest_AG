"""
test_fsk_fec_snr_replay.py: offline replay of the FEC-03 / M-08 SNR sweep
(FSK 4800 Bd / 5 kHz at +300 kHz, 1.92 MSps, uncoded + conv 1/2..7/8) through
the plan/channelizer decode path, with AWGN set as Es/N0 at the channel symbol
rate.

Why: the G40 sweep through a 50 dB pad shows every rate decoding down to the
same edge (bench est SNR 10.1-10.8 dB), with no coding gain. This
replay separates the two limits behind that tie (FEC-03 / M-08):

  * the channelizer only submits a continuous plan channel for decode while
    the GPU slot gate sees its 20 kHz slot 12 dB over the median floor
    (channelizer_worker._targeted_gate). For this signal that is Es/N0 ~18 dB,
    the hardware edge for every rate (gate_open below models it);
  * below the gate the demod limits: the FSKDemodulator discriminator needs
    ~15 dB Es/N0 for BER 1e-2 and feeds the Viterbi hard bits, and FEC frames
    demanded an exact 16-bit sync. FSK_NONCOHERENT (demod_helper) + SoftBits /
    FEC_SOFT_SYNC (frame_codec) fix that offline.

Each case: gen_scenario_json.build_scenario then plan_adapter then the worker's
gate fs_out then channelize then decode_pool._default_decode (exactly as
tests/test_dsss_scenario_roundtrip.py). pytest collects the test_* functions;
`python3 tests/test_fsk_fec_snr_replay.py [--full]` prints the per-rate table
(before = noncoherent fallback disabled, i.e. the old decode behaviour).
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

from dpmg.dsp import demod_helper
from dpmg.framing import frame_codec as fc
from dpmg.scenario import gen_scenario_json as g
from dpmg.dsp.decode_pool import DecodeSpec, _default_decode
from dpmg.fec import conv
from dpmg.scenario.plan_adapter import load_plan, plan_to_watchlist
from test_dsss_scenario_roundtrip import channelize, worker_fs_out

FS = 1_920_000.0            # Setup A rate
OFF = 300_000.0             # the sweep's signal offset
SR = 4800                   # FSK 4800 Bd, 5 kHz spacing (bench/snr_sweep.py defaults)
RATES = ["none", "1/2", "2/3", "3/4", "5/6", "7/8"]
GATE_DB = 12.0              # channelizer_worker._thr_db (targeted decode gate)


def build(fec, out_dir):
    """The sweep's scenario for one rate: (message, iq, plan channel, plan, fs_out)."""
    msg = f"SNR FSK {fec}"
    sig = {"modulation": "FSK", "symbol_rate": SR, "tone_spacing_hz": 5000,
           "offset_hz": OFF, "message": msg, "amplitude": 0.9}
    if fec != "none":
        sig["fec"] = {"type": "conv", "rate": fec}
    name = "fsk_snr_" + fec.replace("/", "")
    g.build_scenario({"name": name, "sample_rate_hz": FS, "center_freq_hz": 425_025_000.0,
                      "loop_s": 0.2, "signals": [sig]}, out_dir)
    plan = load_plan(os.path.join(out_dir, f"{name}.plan.json"))
    iq = np.fromfile(os.path.join(out_dir, f"{name}.iq.bin"), dtype=np.complex64)
    wl = plan_to_watchlist(plan)
    return msg, iq, wl[0][0], plan, worker_fs_out(wl, FS)


def _burst_power(iq):
    a = np.abs(iq) ** 2
    return float(np.mean(a[a > 0.25 * a.max()]))      # the loop has ~8 % silence


def impair(iq, esn0_db, rng, cfo_hz=0.0):
    """Random loop phase/offset, optional CFO, AWGN at Es/N0 (symbol rate SR)."""
    x = np.roll(iq, int(rng.integers(0, len(iq)))) * np.exp(1j * rng.uniform(0, 2 * np.pi))
    if cfo_hz:
        x = x * np.exp(2j * np.pi * cfo_hz / FS * np.arange(len(x)))
    n0 = _burst_power(iq) / SR / 10 ** (esn0_db / 10)
    n = (rng.standard_normal(len(x)) + 1j * rng.standard_normal(len(x))) * np.sqrt(n0 * FS / 2)
    return (x + n).astype(np.complex64)


def gate_open(iq, esn0_db, rng):
    """GPU slot gate model (gpu_channelizer2._slot_powers + process_slots):
    one FFT over a 1M-sample frame, 20 kHz slots from -fs/2 + 10 %, slot power
    over the median of all slots must exceed GATE_DB."""
    x = impair(np.tile(iq, 1048576 // len(iq) + 1)[:1048576], esn0_db, rng)
    N = len(x)
    P = np.fft.fftshift(np.abs(np.fft.fft(x)) ** 2)
    cs = np.concatenate(([0.0], np.cumsum(P)))
    lo = -FS / 2 + 0.1 * FS
    e = lo + 20e3 * np.arange(int(np.floor(0.8 * FS / 20e3)) + 1)
    eb = np.clip(np.round((e + FS / 2) / (FS / N)).astype(int), 0, N)
    sdb = 10 * np.log10(cs[eb[1:]] - cs[eb[:-1]] + 1e-20)
    return float(sdb[int((OFF - lo) // 20e3)] - np.median(sdb)) > GATE_DB


def decode_rate(case, esn0_db, reps, rng, cfo_hz=0.0):
    """Windows (one 0.2 s scenario loop each) that decode the right message."""
    msg, iq, cc, plan, fs_out = case
    params = {"symbol_rate": cc.symbol_rate, **(cc.demod_params or {})}
    ok = 0
    for _ in range(reps):
        bb = channelize(impair(iq, esn0_db, rng, cfo_hz),
                        float(cc.f_center_hz - plan.center_freq_hz), FS, fs_out)
        r = _default_decode(0, bb, DecodeSpec(modulation=cc.demod_type, fs_out=fs_out,
                                               sps=fs_out / cc.symbol_rate,
                                               demod_params=params, lock={}))
        ok += bool(r.valid and r.message == msg)
    return ok


def edge_db(row, reps):
    """Lowest Es/N0 from which at least half the windows decode at every
    higher step of the ladder (None if the top step already fails)."""
    edge = None
    for s in sorted(row, reverse=True):
        if row[s] * 2 < reps:
            break
        edge = s
    return edge


class _no_fallback:
    """Context: disable FSK_NONCOHERENT: the pre-fix decode behaviour."""
    def __enter__(self):
        self._f = demod_helper._fsk_noncoherent
        demod_helper._fsk_noncoherent = lambda *a, **k: None

    def __exit__(self, *exc):
        demod_helper._fsk_noncoherent = self._f


# pytest entry points

def test_conv12_decodes_2db_below_uncoded():
    """M-08 offline: conv 1/2 reaches at least 2 dB lower Es/N0 than uncoded."""
    rng = np.random.default_rng(0xFEC3)
    tmp = tempfile.mkdtemp(prefix="fsk_snr_")
    reps = 6
    ladder = [3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 12.0]
    try:
        edges = {}
        for fec in ("none", "1/2"):
            case = build(fec, tmp)
            row = {s: decode_rate(case, s, reps, rng) for s in ladder}
            edges[fec] = edge_db(row, reps)
            assert edges[fec] is not None, f"{fec}: {row}"
        assert edges["none"] - edges["1/2"] >= 2.0, edges
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_high_snr_keeps_discriminator_path():
    """Clean frames still decode on the existing discriminator path (the
    fallback only runs when that fails), for uncoded and coded."""
    rng = np.random.default_rng(7)
    tmp = tempfile.mkdtemp(prefix="fsk_snr_")
    try:
        for fec in ("none", "1/2", "7/8"):
            msg, iq, cc, plan, fs_out = build(fec, tmp)
            from dpmg.demodulation.demodulator import get_demodulator
            bb = channelize(impair(iq, 25.0, rng), OFF, FS, fs_out)
            spec = fc.FrameSpec.from_entry(cc.demod_params["frame"])
            fdec = ((lambda b: fc.fec_find_and_decode(b, cc.demod_params["fec"],
                                                      max_sync_errors=spec.max_sync_errors))
                    if fec != "none" else (lambda b: fc.find_frame(b, spec, max_search=len(b))))
            d = get_demodulator("FSK", fs_out, SR, fs_out / SR, tone0=-2500.0, tone1=2500.0)
            bs, fr, _n, _r = demod_helper.demod_with_recovery(bb, fs_out, fs_out / SR, "FSK", d, fdec,
                                                             sync_bits=spec.sync_bits)
            assert fr.valid and fr.message == msg, fec
            assert not isinstance(bs, fc.SoftBits), f"{fec}: fell through to the fallback"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_hard_bits_keep_exact_fec_sync():
    """A plain hard-bit string keeps the exact FEC sync match (cost bound on the
    10-offset hard sweep); SoftBits accept the plan's max_sync_errors."""
    frame = fc.encode_fec(b"SYNC TEST", {"type": "conv", "rate": "1/2"})
    bad = ("0" if frame[3] == "1" else "1")
    bits = "0101" * 8 + frame[:3] + bad + frame[4:] + "0110" * 8      # 1 sync bit wrong
    fec = {"type": "conv", "rate": "1/2"}
    assert not fc.fec_find_and_decode(bits, fec, max_sync_errors=2).valid
    llr = np.where(np.frombuffer(bits.encode(), np.uint8) == ord("1"), 4.0, -4.0)
    soft = fc.SoftBits(bits, llr)
    assert not fc.fec_find_and_decode(soft, fec, max_sync_errors=0).valid
    r = fc.fec_find_and_decode(soft, fec, max_sync_errors=2)
    assert r.valid and r.message == "SYNC TEST"
    # inverted soft stream (tone swap) still decodes, LLRs negated with the bits
    r = fc.fec_find_and_decode(fc._invert_bits(soft), fec, max_sync_errors=2)
    assert r.valid and r.message == "SYNC TEST"
    # all-frames: two frames back to back, both found once, in buffer order
    two = fc.SoftBits(soft + soft, np.concatenate((llr, llr)))
    assert [f.message for f in fc.fec_find_all_frames(two, fec, max_sync_errors=2)] == ["SYNC TEST"] * 2


def _decode_soft_loop(llr, rate, terminated=True):
    """The pre-VITERBI_VEC state loop, kept as the reference."""
    llr = np.asarray(llr, dtype=np.float64)
    px, py = conv.PUNCTURE[rate]
    p = len(px)
    n_steps = acc = 0
    while acc < len(llr):
        acc += px[n_steps % p] + py[n_steps % p]
        n_steps += 1
    full = conv._depuncture(llr, rate, n_steps)
    NEG = -1e18
    pm = np.full(conv.NS, NEG)
    pm[0] = 0.0
    back = np.zeros((n_steps, conv.NS), dtype=np.int64)
    for t in range(n_steps):
        lx, ly = full[2 * t], full[2 * t + 1]
        npm = np.full(conv.NS, NEG)
        bk = np.zeros(conv.NS, dtype=np.int64)
        for s in range(conv.NS):
            base = pm[s]
            if base == NEG:
                continue
            for u in (0, 1):
                ns = conv._NEXT[s, u]
                bm = base + conv._OUT[s, u, 0] * lx + conv._OUT[s, u, 1] * ly
                if bm > npm[ns]:
                    npm[ns] = bm
                    bk[ns] = s
        pm = npm
        back[t] = bk
    s = 0 if terminated else int(np.argmax(pm))
    dec = [0] * n_steps
    for t in range(n_steps - 1, -1, -1):
        dec[t] = int(s >> (conv.M - 1))
        s = int(back[t, s])
    return (dec[:-conv.M] if terminated else dec)


def test_vectorised_viterbi_is_bit_identical():
    rng = np.random.default_rng(3)
    for rate in conv.PUNCTURE:
        for trial in range(6):
            c = np.array(conv.encode(list(rng.integers(0, 2, int(rng.integers(8, 120)))), rate), float)
            llr = (2 * c - 1) * 1.5 + rng.standard_normal(len(c)) * 1.5
            if trial % 2:
                llr = np.round(llr)                     # integer metrics: ties
            for term in (True, False):
                assert conv.decode_soft(llr, rate, terminated=term) == \
                    _decode_soft_loop(llr, rate, terminated=term), (rate, trial, term)


def test_grid_cfo_sorted_matches_bruteforce():
    rng = np.random.default_rng(5)
    for sp, k, fs in ((5000.0, 2, 48000.0), (12000.0, 2, 96000.0), (1296.0, 4, 48000.0),
                      (500.0, 2, 48000.0)):
        tones = (np.arange(k) - (k - 1) / 2) * sp
        f = np.repeat(tones[rng.integers(0, k, 600)], 10) + rng.uniform(-0.4, 0.4) * sp
        x = np.exp(1j * np.cumsum(2 * np.pi * f / fs))
        x = (x + (rng.standard_normal(len(x)) + 1j * rng.standard_normal(len(x))) * 0.5).astype(np.complex64)
        ph = np.angle(x[1:] * np.conj(x[:-1])) * fs / (2 * np.pi)
        p = np.abs(x[1:]) ** 2
        gate = p > 4.0 * np.median(p)
        fig = ph[gate] if int(gate.sum()) >= 50 else ph
        spacing = float(np.min(np.diff(np.sort(tones))))
        lim, tol = 0.45 * spacing, max(300.0, 0.35 * spacing)
        best, bo = -1, 0.0
        for off in np.arange(-lim, lim + 1, 50.0):
            sc = int(np.sum(np.min(np.abs(fig[:, None] - (tones + off)[None, :]), axis=1) < tol))
            if sc > best:
                best, bo = sc, off
        assert demod_helper._fsk_grid_cfo(x, fs, tones) == float(bo), (sp, k)


def main() -> int:
    full = "--full" in sys.argv
    ladder = np.arange(2.0, 14.01, 0.5) if full else np.arange(3.0, 13.01, 1.0)
    reps = 8 if full else 4
    rng = np.random.default_rng(0xFEC3)
    tmp = tempfile.mkdtemp(prefix="fsk_snr_")
    gates = {float(s): gate_open(build("none", tmp)[1], float(s), rng)
             for s in list(ladder) + [16.0, 17.0, 18.0, 19.0, 20.0]}
    print(f"FSK {SR} Bd / 5 kHz, +{OFF/1e3:.0f} kHz, {FS/1e6:g} MSps; windows decoded of {reps} "
          f"per Es/N0 step (one 0.2 s loop each); before = noncoherent fallback off")
    print("GPU decode gate (12 dB slot over median) opens from Es/N0 "
          f"{min(s for s, o in gates.items() if o and all(gates[t] for t in gates if t > s)):g} dB")
    edges = {}
    try:
        for fec in RATES:
            case = build(fec, tmp)
            with _no_fallback():
                before = {float(s): decode_rate(case, float(s), reps, rng) for s in ladder}
            after = {float(s): decode_rate(case, float(s), reps, rng) for s in ladder}
            edges[fec] = (edge_db(before, reps), edge_db(after, reps))
            print(f"{fec:>4}  before " + " ".join(f"{s:g}:{before[s]}" for s in before))
            print(f"{'':>4}  after  " + " ".join(f"{s:g}:{after[s]}" for s in after))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n| fec | edge before (Es/N0 dB) | edge after (Es/N0 dB) |\n|---|---|---|")
    for fec, (b, a) in edges.items():
        print(f"| {fec} | {b} | {a} |")
    ok = edges["none"][1] is not None and edges["1/2"][1] is not None \
        and edges["none"][1] - edges["1/2"][1] >= 2.0
    print("conv 1/2 >= 2 dB below uncoded:", ok)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
