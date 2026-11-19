"""
amc_eval.py: offline validation of the AMC classifier against the project's
own modulators. Generates each modulation, applies a carrier impairment (CFO,
Wiener phase noise, linear drift, random start phase), adds AWGN at a set SNR,
runs amc.classify, and prints a confusion matrix + per-class accuracy. No SDR
needed.

    python3 tools/amc_eval.py [snr_db] [trials]                 # one matrix, zero CFO
    python3 tools/amc_eval.py 15 8 --cfo 300 --linewidth 10     # one impaired matrix
    python3 tools/amc_eval.py --legacy ...                      # the legacy classifier
    python3 tools/amc_eval.py --sweep [--trials N] [--json out.json]
        # every modulation x SNR {20,15,10} x carrier {none, G40 cross-card,
        # harsh}, for the current classifier and the legacy one

SNR is per sample over the generated sample rate (8 samples/symbol for most
mods), the same definition as the reference baseline (100/94/80 %).
"""
import argparse
import json
import os
import random
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # repo root, one up from tools/
sys.path.insert(0, _ROOT)

import numpy as np

from dpmg.dsp import amc
from dpmg.modulation.modulator import get_modulator

SYM_RATE = 20_000
SPB = 8                      # samples per symbol (bit for the 1-bit/2-bit mods)

# Original 7-class set (the baseline) and the full registry.
BASE_MODS = ["FSK", "4FSK", "BPSK", "QPSK", "8PSK", "16QAM", "CSS"]
ALL_MODS = ["FSK", "GFSK", "4FSK", "BPSK", "QPSK", "pi/4-QPSK", "DQPSK", "OQPSK",
            "8PSK", "8QAM", "16QAM", "16APSK", "32APSK", "DSSS", "QPSK-DSSS", "CSS"]

# realistic FSK tone plans (h ~ 0.7, spread across the baseband), keyed by mod
_TONES = {
    "FSK": dict(tone0=-7000.0, tone1=7000.0),
    "2FSK": dict(tone0=-7000.0, tone1=7000.0),
    "GFSK": dict(tone0=-7000.0, tone1=7000.0, gfsk_bw_hz=10_000.0),   # BT 0.5
    "4FSK": dict(tone0=-9000.0, tone1=-3000.0, tone2=3000.0, tone3=9000.0),
}

# Carrier impairment profiles. "g40" = the cross-card link: the TX card stays
# on its internal reference (~0.026 ppm vs the GPSDO card, i.e. tens of Hz at
# UHF and a few hundred Hz worst case, measured -11 Hz at +300 kHz),
# two PLL synthesizers on independent references (slow close-in
# wander, modelled as a 1 Hz Wiener linewidth), thermal drift, ~1 deg rms
# synthesizer jitter. "harsh" is 3x the offset, 10x the wander and 3x the jitter.
PROFILES = {
    "none": dict(cfo=0.0, linewidth=0.0, drift=0.0, jitter_deg=0.0),
    "g40": dict(cfo=300.0, linewidth=1.0, drift=5.0, jitter_deg=1.0),
    "harsh": dict(cfo=1000.0, linewidth=10.0, drift=20.0, jitter_deg=3.0),
}


def _bits(n):
    return "".join(random.choice("01") for _ in range(n))


OPTS = {"rrc": False}      # set from the command line


def gen(mod, nbits=4000, spb=None):
    """IQ + sample rate for one burst of `mod` from the real TX modulators
    (spb overrides the samples per symbol, e.g. to build at the card rate)."""
    kw = dict(_TONES.get(mod, {}))
    rate, spb = SYM_RATE, (spb or SPB)
    base_spb = spb
    if OPTS["rrc"] and mod in ("8PSK", "8QAM", "16QAM", "16APSK", "32APSK"):
        kw.update(use_rrc=True, rolloff=0.35)
    if mod in _TONES and SYM_RATE != 20_000:
        kw = {k: (v * SYM_RATE / 20_000 if k.startswith("tone") or k == "gfsk_bw_hz" else v)
              for k, v in kw.items()}
    if mod in ("DSSS", "QPSK-DSSS"):
        rate, spb = SYM_RATE / 15.0, base_spb * 15   # SYM_RATE chip/s, spb samples/chip
        kw["spread_factor"] = 15
        nbits = 300
    if mod == "CSS" and base_spb != SPB:
        kw["css_os"] = 16                            # 2 MSps for the 125 kHz chirp
    m = get_modulator(mod, rate, spb, **kw)
    r = m.modulate(_bits(nbits))
    return np.asarray(r.iq_samples, dtype=np.complex64), float(r.sample_rate)


def impair(x, fs, cfo=0.0, linewidth=0.0, drift=0.0, jitter_deg=0.0, rng=np.random):
    """Carrier impairment: CFO drawn uniform in +/-cfo, Wiener phase noise with
    3-dB linewidth `linewidth` Hz, linear frequency drift `drift` Hz/s, white
    phase jitter, random start phase."""
    n = np.arange(x.size)
    f0 = rng.uniform(-cfo, cfo) if cfo else 0.0
    ph = 2 * np.pi * (f0 * n / fs + 0.5 * drift * (n / fs) ** 2) + rng.uniform(0, 2 * np.pi)
    if linewidth:
        ph = ph + np.cumsum(rng.normal(0.0, np.sqrt(2 * np.pi * linewidth / fs), x.size))
    if jitter_deg:
        ph = ph + rng.normal(0.0, np.radians(jitter_deg), x.size)
    return (x * np.exp(1j * ph)).astype(np.complex64), f0


def awgn(x, snr_db):
    p = np.mean(np.abs(x) ** 2)
    npw = p / (10 ** (snr_db / 10.0))
    noise = np.sqrt(npw / 2) * (np.random.randn(len(x)) + 1j * np.random.randn(len(x)))
    return (x + noise).astype(np.complex64)


CARD_RATE = 1.92e6


def _trim(x):
    """Drop leading/trailing silence only (interior zeros are real samples:
    a BPSK flip interpolates through exactly 0)."""
    nz = np.flatnonzero(np.abs(x) > 1e-6)
    return x[nz[0]:nz[-1] + 1] if nz.size else x


def gen_radio(mod, offset=300e3):
    """The burst exactly as the radio transmits it: gen_scenario_json.build_signal
    at the card rate (native-rate modulator + linear-interpolation lift, as on
    the G40), at +offset. Symbol rates follow the bench matrix (4800 / 2400 sym/s,
    bench/mod_fec_matrix.MOD_PARAMS) unless --sym-rate is given."""
    from dpmg.scenario import gen_scenario_json as g
    sys.path.insert(0, os.path.join(_ROOT, "bench"))
    from mod_fec_matrix import MOD_PARAMS
    from b200_comm_loopback import mod_spec
    sr, sp, extra = MOD_PARAMS[mod]
    if SYM_RATE != 20_000 and mod not in ("CSS",):
        scale = SYM_RATE / sr
        sr, sp = SYM_RATE, sp * scale
    spec = mod_spec(mod, sr, sp, _bits(64)[:8], 425_025_000, offset=offset, extra=extra or None)
    sig = dict(spec["signals"][0])
    sig["message"] = "".join(random.choice("ABCDEFGHJKLMNPQRSTUVWXYZ0123456789") for _ in range(200))
    iq, _e = g.build_signal(sig, CARD_RATE)
    x = np.asarray(iq, dtype=np.complex64)
    # the TX loop as bench/amc_hw_check.py sizes it (200-char frame + 5 % gap),
    # played CONTINUOUS; the capture is 0.2 s from a random point of the loop
    x = _trim(x)
    loop = int(np.ceil(1.05 * x.size))
    x = np.concatenate([x, np.zeros(loop - x.size, np.complex64)])
    n = int(0.2 * CARD_RATE)
    st = random.randrange(loop)
    x = np.tile(x, int(np.ceil((st + n) / loop)) + 1)[st:st + n]
    return x, CARD_RATE, float(sr)


def service_frontend(x, fs, offset=300e3, rng=np.random, clean=None):
    """What the radio path does to a burst before amc.classify: the burst is
    built at the card rate (gen_scenario: rectangular symbols at
    card_rate/sym_rate samples each), sits at `offset` in the capture, and
    classify_selection shifts the brushed span to DC and decimates to ~2.5x its
    width (same loop as dpmg_service._cmd_classify_selection). The brush is the
    -20 dB occupied width x1.3, so the classifier sees ~3 samples/symbol."""
    import scipy.signal as ss
    card_rate = fs
    y = np.asarray(x, dtype=np.complex128)
    n = np.arange(y.size)
    y = y * np.exp(2j * np.pi * offset / card_rate * n)
    # selection width from the burst's own (noise-free) spectrum: the span an
    # operator brushes around the visible signal
    ref = x if clean is None else clean
    if clean is not None:
        ref = _trim(np.roll(ref, -int(np.argmin(np.abs(ref) > 1e-6))))  # the burst, not the gap
    nf = min(1 << 16, ref.size)
    X = np.abs(np.fft.fftshift(np.fft.fft(ref[:nf] * np.hanning(nf)))) ** 2
    f = np.fft.fftshift(np.fft.fftfreq(nf, 1.0 / fs))
    band = f[X > X.max() * 1e-2]
    sel_bw = 1.3 * float(band.max() - band.min()) if band.size else fs / 2
    sel_bw = max(sel_bw, 20e3)          # same 10 kHz half-width floor as amc_hw_check
    sel_c = offset
    y = y * np.exp(-2j * np.pi * sel_c / card_rate * n)
    cur = float(card_rate)
    target = max(sel_bw * 2.5, 1.0)
    while cur / target >= 2.0:
        q = int(min(8, cur // target))
        if q < 2:
            break
        y = ss.decimate(y, q, ftype="fir", zero_phase=True)
        cur /= q
    return y.astype(np.complex64), cur


def expected(mod, legacy=False):
    """Label a correct classifier returns for `mod`. The legacy tree only knew
    7 labels (and 'APSK'); the current one returns registry names, with blind
    equivalences (DQPSK == QPSK) from amc.EQUIVALENT."""
    if legacy:
        return {"16APSK": "APSK", "32APSK": "APSK"}.get(mod, mod)
    return amc.canonical(mod)


def run_matrix(mods, snr, trials, prof, legacy=False, seed=0, cfo_fixed=None):
    random.seed(seed)
    np.random.seed(seed)
    rng = np.random.RandomState(seed + 1)
    fn = amc.classify_legacy if legacy else amc.classify
    conf = {m: {} for m in mods}
    cfo_err = {m: [] for m in mods}
    t0 = time.time()
    svc = OPTS.get("frontend") == "service"
    for mod in mods:
        for _ in range(trials):
            if svc:
                iq, fs, sr = gen_radio(mod)
            else:
                iq, fs = gen(mod)
            p = dict(prof)
            if cfo_fixed is not None:
                p["cfo"] = 0.0
            iq, f0 = impair(iq, fs, rng=rng, **p)
            if cfo_fixed is not None:
                iq = (iq * np.exp(2j * np.pi * cfo_fixed / fs * np.arange(iq.size))).astype(np.complex64)
                f0 = cfo_fixed
            fs_c = fs
            if svc:
                # same in-band SNR as the default mode: SNR over SPB x the symbol
                # rate, or over 2x the occupied width for the spread waveforms
                # (DSSS chips, CSS sweep) so their chip-level SNR is comparable
                clean = iq * np.exp(-2j * np.pi * 300e3 / fs * np.arange(iq.size))
                ref_bw = max(SPB * sr, 2.0 * amc.occupied_bw(_trim(clean), fs)) \
                    if mod in ("DSSS", "QPSK-DSSS", "CSS") else SPB * sr
                iq = awgn(iq, snr - 10 * np.log10(fs / ref_bw))
                iq = (iq * np.exp(-2j * np.pi * 300e3 / fs * np.arange(iq.size))).astype(np.complex64)
                iq, fs_c = service_frontend(iq, fs, rng=rng, clean=clean)
            else:
                iq = awgn(iq, snr)
            res = fn(iq, fs_c)
            pred = res["modulation"]
            conf[mod][pred] = conf[mod].get(pred, 0) + 1
            if not legacy and res.get("carrier", {}).get("order"):
                cfo_err[mod].append(res["carrier"]["cfo_hz"] - f0)
    dt = time.time() - t0
    acc = {m: conf[m].get(expected(m, legacy), 0) / trials for m in mods}
    overall = float(np.mean(list(acc.values())))
    return {"snr": snr, "trials": trials, "profile": prof, "legacy": legacy,
            "confusion": conf, "acc": acc, "overall": overall,
            "cfo_err_rms_hz": {m: float(np.sqrt(np.mean(np.square(v)))) if v else None
                               for m, v in cfo_err.items()},
            "sec_per_classify": dt / (len(mods) * trials)}


def print_matrix(r, mods):
    preds = []
    for m in mods:
        for p in r["confusion"][m]:
            if p not in preds:
                preds.append(p)
    order = [expected(m, r["legacy"]) for m in mods]
    cols = [p for p in dict.fromkeys(order) if p in preds] + \
           [p for p in preds if p not in order]
    w = max(6, max(len(c) for c in cols) + 1)
    hdr = "exp\\pred".ljust(10) + "".join(c[:w - 1].rjust(w) for c in cols) + "    acc"
    print(hdr)
    for m in mods:
        row = r["confusion"][m]
        cells = "".join((str(row.get(c, 0)) if row.get(c, 0) else ".").rjust(w) for c in cols)
        print(m[:9].ljust(10) + cells + f"  {100 * r['acc'][m]:4.0f}%")
    print(f"overall accuracy: {100 * r['overall']:.1f}%   "
          f"({r['sec_per_classify'] * 1e3:.0f} ms/classify)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("snr", nargs="?", type=float, default=20.0)
    ap.add_argument("trials_pos", nargs="?", type=int, default=None)
    ap.add_argument("--trials", type=int, default=8)
    ap.add_argument("--legacy", action="store_true", help="old decision tree")
    ap.add_argument("--base", action="store_true", help="only the original 7 classes")
    ap.add_argument("--profile", choices=sorted(PROFILES), default="none")
    ap.add_argument("--cfo", type=float, help="override: CFO range +/- Hz")
    ap.add_argument("--linewidth", type=float, help="override: Wiener linewidth Hz")
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--cfo-sweep", action="store_true",
                    help="fixed CFO steps 0..1000 Hz at one SNR (accuracy vs CFO)")
    ap.add_argument("--json", help="write results here")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--sym-rate", type=float, default=20_000.0,
                    help="symbol rate of the generated bursts (default 20 k)")
    ap.add_argument("--rrc", action="store_true",
                    help="RRC pulse shaping (alpha 0.35) on 8PSK/8QAM/16QAM/APSK")
    ap.add_argument("--frontend", choices=["none", "service"], default="none",
                    help="service = the radio's own burst (gen_scenario_json.build_signal at "
                         "1.92 MSps, bench symbol rates) + classify_selection decimation")
    a = ap.parse_args()
    global SYM_RATE
    SYM_RATE = a.sym_rate
    OPTS.update(rrc=a.rrc, frontend=a.frontend)
    trials = a.trials_pos or a.trials
    mods = BASE_MODS if a.base else ALL_MODS
    out = []
    if a.sweep:
        for prof in ("none", "g40", "harsh"):
            for legacy in (True, False):
                for snr in (20.0, 15.0, 10.0):
                    ms = [m for m in mods] if not legacy else mods
                    r = run_matrix(ms, snr, trials, PROFILES[prof], legacy, a.seed)
                    r["profile_name"] = prof
                    out.append(r)
                    tag = "legacy" if legacy else "carrier-recovery"
                    print(f"\n=== {tag}  profile={prof}  SNR {snr:g} dB  {trials}/class ===")
                    print_matrix(r, ms)
    elif a.cfo_sweep:
        for cfo in (0, 30, 100, 300, 600, 1000):
            for legacy in (True, False):
                prof = dict(PROFILES["none"])
                r = run_matrix(mods, a.snr, trials, prof, legacy, a.seed, cfo_fixed=float(cfo))
                r["cfo_fixed_hz"] = cfo
                out.append(r)
                print(f"CFO {cfo:5d} Hz  {'legacy ' if legacy else 'new    '} "
                      f"overall {100 * r['overall']:5.1f}%  " +
                      " ".join(f"{m}:{100 * r['acc'][m]:.0f}" for m in mods))
    else:
        prof = dict(PROFILES[a.profile])
        if a.cfo is not None:
            prof["cfo"] = a.cfo
        if a.linewidth is not None:
            prof["linewidth"] = a.linewidth
        r = run_matrix(mods, a.snr, trials, prof, a.legacy, a.seed)
        print(f"AMC eval @ {a.snr:g} dB SNR, {trials} trials/class, carrier {prof}"
              f"{' [legacy]' if a.legacy else ''}\n")
        print_matrix(r, mods)
        out.append(r)
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(out, fh, indent=1, default=float)


if __name__ == "__main__":
    main()
