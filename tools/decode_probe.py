#!/usr/bin/env python3
"""
decode_probe.py: run the actual worker decode on a channel dump, read-only.

This does not modify anything running. It imports the real decode function the
worker uses (_default_decode from decode_pool) and the real demod chain, and runs
them on a captured channel baseband (.npy from the Watch List "Dump baseband"
button). It prints full diagnostics: sync-match score, recovery attempts, where it
fails: numbers that offline reconstruction cannot give, taken from the actual
code path.

Because it calls the worker's own _default_decode, it reproduces exactly what the
live worker does for that channel, as a separate read-only process.

Usage (inside the dpmg container, where the real modules live):
    docker cp decode_probe.py dpmg:/app/tools/
    docker cp chan_bbFSK-06.npy dpmg:/tmp/
    docker cp chan_bb_metaFSK-06.npy dpmg:/tmp/
    docker exec dpmg python3 /app/tools/decode_probe.py /tmp/chan_bbFSK-06.npy /tmp/chan_bb_metaFSK-06.npy

Or point it at both a failing and a working channel to compare:
    docker exec dpmg python3 /app/tools/decode_probe.py \
        /tmp/chan_bbFSK-06.npy   /tmp/chan_bb_metaFSK-06.npy \
        /tmp/chan_bb4FSK-07.npy  /tmp/chan_bb_meta4FSK-07.npy
"""
import sys
import numpy as np
sys.path.insert(0, "/app")
sys.path.insert(0, ".")


def sync_match(bits, sync):
    if not bits or not sync:
        return -1, -1
    best, bestpos = -1, -1
    for p in range(max(1, len(bits) - len(sync))):
        s = sum(a == b for a, b in zip(bits[p:p+len(sync)], sync))
        if s > best:
            best, bestpos = s, p
    return best, bestpos


def probe(bb_path, meta_path):
    bb = np.load(bb_path).astype(np.complex64)
    meta = np.load(meta_path, allow_pickle=True)
    m = meta[0] if hasattr(meta, "__len__") and not isinstance(meta, dict) else meta
    m = dict(m)
    name = m.get("name", "?")
    fs_out = float(m["fs_out"])
    symrate = float(m["symbol_rate"])
    modulation = m.get("demod_type", "FSK")
    fs_in = float(m.get("fs_in", 30.72e6))

    # reproduce the worker's rate math exactly
    decim = max(1, int(round(fs_in / fs_out)))
    fs_act = fs_in / decim
    sps = fs_act / symrate

    print(f"\n{'='*60}")
    print(f"CHANNEL: {name}   modulation: {modulation}")
    print(f"{'='*60}")
    print(f"  samples={len(bb)}  fs_out(meta)={fs_out}  fs_act={fs_act:.1f}  sps={sps:.4f}")

    # burst presence
    p2 = np.abs(bb)**2
    env = np.convolve(p2, np.ones(8)/8, "same")
    on = np.nonzero(env > env.max()*0.15)[0]
    if len(on):
        nsym = (on[-1]-on[0]) / (fs_act/symrate)
        print(f"  burst: {(on[-1]-on[0])/fs_act*1000:.1f}ms = {nsym:.0f} symbols (frame needs 104)")
    pk = 10*np.log10(p2.max()+1e-20); mn = 10*np.log10(p2.mean()+1e-20)
    print(f"  power: peak={pk:.1f} mean={mn:.1f} dB (spread {pk-mn:.1f} dB, >10=burst present)")

    # run the real worker decode
    try:
        from dpmg.dsp.decode_pool import _default_decode, DecodeSpec
    except Exception as e:
        print(f"  [!] could not import worker decode: {e}")
        _default_decode = None

    from dpmg.framing import frame_codec as fc
    sync = fc.SYNC_BITS

    if _default_decode is not None:
        # build the spec exactly as the worker would, using the dump's params
        dp = {"symbol_rate": symrate}
        # provide both keys so the tone path is tested fairly
        # (scenario uses tone_deltas_hz; worker reads tone_spacing_hz)
        if modulation == "4FSK":
            dp["tone_spacing_hz"] = symrate     # 4FSK spacing
        else:
            dp["tone_spacing_hz"] = symrate     # FSK: +/-0.5*spacing
        spec = DecodeSpec(modulation=modulation, fs_out=fs_act, sps=sps,
                          demod_params=dp, lock={})
        res = _default_decode(name, bb, spec)
        print(f"\n  --- WORKER _default_decode result ---")
        print(f"    ok={getattr(res,'ok',None)} valid={getattr(res,'valid',None)} "
              f"acquired={getattr(res,'acquired',None)}")
        print(f"    n_syms={getattr(res,'n_syms',None)} snr_db={getattr(res,'snr_db',None)}")
        print(f"    bit_string len={len(getattr(res,'bit_string','') or '')}")
        print(f"    error/diag: {getattr(res,'error','')}")
        bs = getattr(res, "bit_string", "") or ""
        sm, pos = sync_match(bs, sync)
        print(f"    >>> SYNC MATCH: {sm}/{len(sync)} @ pos {pos}   (16/16 = locked)")
        if sm >= 15:
            print(f"    >>> VERDICT: MARGINAL, a small recovery fix would lock it")
        elif sm >= 11:
            print(f"    >>> VERDICT: recovery gap; signal present, timing/CFO needs work")
        elif sm >= 0:
            print(f"    >>> VERDICT: poor sync; signal or param problem")

    # also show the raw demod chain directly for cross-check
    try:
        from dpmg.dsp.demod_helper import demod_with_recovery
        from dpmg.demodulation.demodulator import get_demodulator
        d = get_demodulator(modulation, fs_act, symrate, int(round(sps)))
        bs2, fr2, ns2, _ = demod_with_recovery(bb, fs_act, sps, modulation, d,
                                               fc.find_and_decode, sync_bits=sync,
                                               frame_bits=104)
        sm2, _ = sync_match(bs2, sync)
        print(f"\n  --- direct demod_with_recovery cross-check ---")
        print(f"    valid={getattr(fr2,'valid',False)} sync={sm2}/16 nsyms={ns2}")
    except Exception as e:
        print(f"  [cross-check skipped: {e}]")


if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) < 2:
        print(__doc__); sys.exit(1)
    # pairs of (bb, meta)
    for i in range(0, len(args)-1, 2):
        probe(args[i], args[i+1])
