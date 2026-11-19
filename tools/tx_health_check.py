#!/usr/bin/env python3
"""
X40 TX health check: clean emitters, or broadband smear and replicas.

Runs on the G40 (the receiver). Analyzes a wideband IQ capture with a
long-integration averaged spectrum (not the 2-second live PSD, which misses bursts),
then reports:
  - where the real energy is vs where the 8 emitters should be
  - replica/spur peaks outside the emitter band (the fs/8 = +6.25 MHz family)
  - per-emitter occupied bandwidth (narrow = clean, wide = smeared/corrupted)
  - a verdict: clean 8-emitter spectrum, or broadband corruption

Usage
-----
Step 1: capture wideband on the G40 while the X40 is transmitting. Two options:

  (A) If the app exposes a wideband dump, use it. Otherwise use this standalone grab
      that opens the card directly (only works if the app is not holding the card;
      stop the worker/RX first, or run inside the container sharing the backend):

        docker exec dpmg python3 /app/tools/tx_health_check.py --grab /tmp/wb.npy \
            --center 1350e6 --rate 30.72e6 --secs 0.5

  (B) Simplest: capture from the running app's raw hook (if present) or copy an
      existing /tmp/raw_rx20.npy the worker dumped.

Step 2: analyze (works on any wideband .npy at a known center+rate):

        python3 tx_health_check.py --analyze /tmp/wb.npy --center 1350e6 --rate 30.72e6

You can also point --emitters at the scenario JSON to auto-load expected freqs:

        python3 tx_health_check.py --analyze /tmp/wb.npy --center 1350e6 --rate 30.72e6 \
            --emitters /app/scenarios/s1_modem_sweep_1350_crc_g40.json
"""
import argparse, json, sys
import numpy as np


def grab_wideband(path, center, rate, secs):
    """Open the Epiq card directly and capture `secs` of wideband IQ. Card must be free."""
    sys.path.insert(0, "/app"); sys.path.insert(0, ".")
    from dpmg.radio.skiq_bridge import SkiqBridge
    br = SkiqBridge(); br.init(0)
    hdl = 0  # rx handle A1 (adjust if your rx_antenna maps elsewhere)
    bw = int(rate * 0.8)
    br.set_rx_rate_bw(0, hdl, int(rate), bw)
    br.set_rx_lo(0, hdl, int(center))
    gmin, gmax = br.rx_gain_index_range(0, hdl)
    br.set_rx_gain_index(0, hdl, int((gmin + gmax) // 2))
    try:
        br.set_iq_order(0, False)
    except Exception:
        pass
    br.start_rx(0, hdl)
    need = int(rate * secs)
    chunks, got = [], 0
    while got < need:
        iq = br.receive_raw(0)          # returns complex64 block (or None)
        if iq is None or len(iq) == 0:
            continue
        chunks.append(np.asarray(iq, dtype=np.complex64)); got += len(iq)
    br.stop_rx(0, hdl)
    wb = np.concatenate(chunks)[:need]
    np.save(path, wb)
    print(f"[grab] saved {len(wb)} samples ({len(wb)/rate*1000:.0f} ms) @ {center/1e6:.3f} MHz, {rate/1e6:g} MSps to {path}")
    return wb


def averaged_spectrum(wb, rate, nfft=4096):
    """Welch-style averaged power spectrum: integrates over the whole capture so a
    20 ms burst that only occupies 1/100 of the record still shows at full height
    (unlike the live 2 s PSD which samples one instant)."""
    n = (len(wb) // nfft) * nfft
    x = wb[:n].reshape(-1, nfft)
    win = np.hanning(nfft)
    S = np.abs(np.fft.fftshift(np.fft.fft(x * win, axis=1), axes=1)) ** 2
    psd = S.mean(axis=0)                       # average over all segments
    psd_max = np.abs(np.fft.fftshift(np.fft.fft(x * win, axis=1), axes=1)) ** 2
    peak_hold = psd_max.max(axis=0)            # max-hold: catches every burst
    f = np.fft.fftshift(np.fft.fftfreq(nfft, 1.0 / rate))
    return f, psd, peak_hold


def find_peaks(f, spec, floor_db, min_sep_hz):
    """Return [(freq_hz, level_db)] of peaks that stand `>= floor_db` over the median."""
    s_db = 10 * np.log10(spec + 1e-30)
    med = np.median(s_db)
    thr = med + floor_db
    peaks = []
    i = 1
    while i < len(s_db) - 1:
        if s_db[i] > thr and s_db[i] >= s_db[i - 1] and s_db[i] >= s_db[i + 1]:
            # local max above threshold; merge nearby
            if not peaks or abs(f[i] - peaks[-1][0]) > min_sep_hz:
                peaks.append((f[i], s_db[i] - med))
            elif s_db[i] - med > peaks[-1][1]:
                peaks[-1] = (f[i], s_db[i] - med)
        i += 1
    return peaks


def analyze(wb, center, rate, emitter_freqs=None):
    f, psd, peak = averaged_spectrum(wb, rate)
    f_mhz = (f + center) / 1e6

    print(f"\n{'='*64}\nWIDEBAND TX HEALTH  |  center {center/1e6:.3f} MHz  rate {rate/1e6:g} MSps")
    print(f"capture: {len(wb)} samples = {len(wb)/rate*1000:.0f} ms\n{'='*64}")

    # peaks on the max-hold (catches bursts)
    peaks = find_peaks(f, peak, floor_db=10.0, min_sep_hz=200e3)
    peaks.sort(key=lambda p: -p[1])
    print(f"\nPEAKS (>10 dB over floor, max-hold):")
    for pf, pdb in peaks[:20]:
        print(f"   {(pf+center)/1e6:9.3f} MHz   +{pdb:4.1f} dB")

    # expected emitters
    if emitter_freqs:
        print(f"\nEXPECTED EMITTERS ({len(emitter_freqs)}):")
        vals = list(emitter_freqs.values()) if isinstance(emitter_freqs, dict) else list(emitter_freqs)
        lo = min(vals); hi = max(vals)
        for name, ef in emitter_freqs.items() if isinstance(emitter_freqs, dict) else enumerate(emitter_freqs):
            # nearest peak
            near = min(peaks, key=lambda p: abs((p[0]+center)/1e6 - ef)) if peaks else None
            d = abs((near[0]+center)/1e6 - ef) if near else 99
            tag = f"peak +{near[1]:.0f}dB @ {(near[0]+center)/1e6:.3f}" if near and d < 0.3 else "NO CLEAN PEAK"
            print(f"   {ef:9.3f} MHz  -> {tag}")

        # energy outside the emitter band = TX splatter/replicas
        margin_mhz = 2.0
        band_lo_hz = (lo - margin_mhz) * 1e6 - center
        band_hi_hz = (hi + margin_mhz) * 1e6 - center
        out = [(pf, pdb) for pf, pdb in peaks if pf < band_lo_hz or pf > band_hi_hz]
        print(f"\nENERGY OUTSIDE EMITTER BAND ({lo:.2f}-{hi:.2f} MHz +/- 2 MHz):")
        if not out:
            print("   none significant: GOOD (no replicas/splatter outside band)")
        else:
            for pf, pdb in out[:12]:
                print(f"   {(pf+center)/1e6:9.3f} MHz  +{pdb:.1f} dB  <-- SPUR/REPLICA")
            # is it the fs/8 family?
            for ef in (emitter_freqs.values() if isinstance(emitter_freqs, dict) else emitter_freqs):
                for mult, lbl in ((rate/8/1e6, "fs/8"), (3*rate/8/1e6, "3fs/8")):
                    for pf, pdb in out:
                        if abs((pf+center)/1e6 - (ef + mult)) < 0.3:
                            print(f"      ^ {(pf+center)/1e6:.3f} = {ef:.2f} + {mult:.2f} ({lbl} replica)")

    # total in-band vs out-of-band power ratio (corruption metric)
    total = psd.sum()
    if emitter_freqs:
        lo = min(emitter_freqs.values() if isinstance(emitter_freqs, dict) else emitter_freqs)
        hi = max(emitter_freqs.values() if isinstance(emitter_freqs, dict) else emitter_freqs)
        inband = ((f + center)/1e6 >= lo-1) & ((f + center)/1e6 <= hi+1)
        frac = psd[inband].sum() / total
        print(f"\nPOWER IN EMITTER BAND: {frac*100:.1f}% of total")
        print(f"   >80%  = clean (energy where the emitters are)")
        print(f"   <50%  = CORRUPTED (most energy is splatter/replicas outside the band)")
        verdict = "CLEAN" if frac > 0.7 else ("SUSPECT" if frac > 0.5 else "CORRUPTED")
        print(f"\nVERDICT: TX looks {verdict}")

    # simple ascii spectrum
    print(f"\nAVERAGED SPECTRUM (max-hold, 1 line ~= {rate/1e6/60:.2f} MHz):")
    pk_db = 10*np.log10(peak+1e-30); pk_db -= pk_db.max()
    step = max(1, len(pk_db)//60)
    for i in range(0, len(pk_db), step):
        seg = pk_db[i:i+step].max()
        bar = '#' * max(0, int((seg+60)/2))
        print(f"   {f_mhz[i]:8.2f} {bar}")


def load_emitter_freqs(json_path, center):
    d = json.load(open(json_path))
    out = {}
    c = d.get("compose", {}).get("center_freq_hz", center)
    for e in d.get("entries", []):
        out[e.get("name", "?")] = (c + e.get("offset_hz", 0.0)) / 1e6
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--grab", metavar="OUT.npy", help="capture wideband to this file (card must be free)")
    ap.add_argument("--analyze", metavar="IN.npy", help="analyze an existing wideband capture")
    ap.add_argument("--center", type=float, default=1350e6, help="center freq Hz (e.g. 1350e6)")
    ap.add_argument("--rate", type=float, default=30.72e6, help="sample rate Hz")
    ap.add_argument("--secs", type=float, default=0.5, help="capture length (grab mode)")
    ap.add_argument("--emitters", help="scenario JSON to load expected emitter freqs from")
    a = ap.parse_args()

    if a.grab:
        wb = grab_wideband(a.grab, a.center, a.rate, a.secs)
        ef = load_emitter_freqs(a.emitters, a.center) if a.emitters else None
        analyze(wb, a.center, a.rate, ef)
    elif a.analyze:
        wb = np.load(a.analyze).astype(np.complex64)
        ef = load_emitter_freqs(a.emitters, a.center) if a.emitters else None
        analyze(wb, a.center, a.rate, ef)
    else:
        ap.print_help()
