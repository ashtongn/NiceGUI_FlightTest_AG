"""
gpu_channelizer2.py: batched CuPy/GPU channelizer.

A Python loop over channels costs 30 small GPU ops and 30 GPU to host syncs per frame
and scales linearly (the loop, not the math, dominates). This version batches all
channels into single vectorized GPU operations:

    - stack every channel's bin indices into one (n_chan, n_out) array
    - gather all bands at once:        Xg[bins2d]: (n_chan, n_out)
    - one batched inverse FFT:         cp.fft.ifft(..., axis=1)  (all channels)
    - one host transfer + one sync     for the whole result block

Run in the L4T + CuPy container:
    python3 /work/gpu_channelizer2.py
"""

from __future__ import annotations

from dataclasses import dataclass
import time

import numpy as np

try:
    import cupy as cp
    _HAVE_CUPY = True
except Exception:
    cp = None
    _HAVE_CUPY = False


@dataclass
class ChanSpec:
    f_center_hz: float
    bw_hz: float
    name: str = ""


class GPUChannelizerBatched:
    def __init__(self, fs_in: float, fs_out: float, frame_len: int):
        if not _HAVE_CUPY:
            raise RuntimeError("CuPy not available")
        self.fs_in = float(fs_in)
        self.frame_len = int(frame_len)
        # Decimation = plain round(fs_in/fs_out). Do not snap it to a power of 2
        # (FFT-friendly n_out): the decode worker computes symbol timing (sps)
        # from round(fs_in/fs_out), a snapped decim silently shifts the channel
        # output rate, sps no longer matches and nothing decodes (detection still
        # works, it reads the shared FFT, not the channel rate). Keep the rate
        # contract the worker assumes.
        self.decim = max(1, int(round(self.fs_in / float(fs_out))))
        self.fs_out = self.fs_in / self.decim
        self.n_out = self.frame_len // self.decim
        self.specs: dict[int, ChanSpec] = {}
        self._dirty = True
        self._bins2d_gpu = None          # (n_chan, n_out) int64 on GPU
        self._order: list[int] = []      # channel idx order matching rows
        # energy-detector slot grid (computed on the GPU from the same FFT)
        self._slot_ready = False
        self._slot_edges_gpu = None      # (n_slots+1,) bin edges into the fftshifted spectrum
        self._n_slots = 0
        self._slot_bw = 20e3             # set via configure_slots()

    def set_channel(self, idx: int, spec: ChanSpec, capture_center_hz: float) -> None:
        self.specs[idx] = spec
        self._capture_center = capture_center_hz
        self._dirty = True

    def remove_channel(self, idx: int) -> None:
        self.specs.pop(idx, None)
        self._dirty = True

    def _rebuild_bins(self, capture_center_hz: float) -> None:
        binspacing = self.fs_in / self.frame_len
        half = self.n_out // 2
        rows = []
        self._order = []
        for idx, sp in self.specs.items():
            foff = sp.f_center_hz - capture_center_hz
            cb = int(round(foff / binspacing))   # bin index of the requested channel center
            bins = (np.arange(-half, -half + self.n_out) + cb) % self.frame_len
            # pre-roll so DC lands at column 0: folds the per-frame cp.roll into
            # the gather order (one fewer full (n_chan,n_out) copy per frame)
            bins = np.roll(bins, -half)
            rows.append(bins.astype(np.int64))
            self._order.append(idx)
        if rows:
            self._bins2d_gpu = cp.asarray(np.stack(rows, axis=0))  # (n_chan, n_out)
        else:
            self._bins2d_gpu = None
        self._dirty = False

    def configure_slots(self, capture_center_hz: float, slot_bw: float = 20e3,
                        edge_guard_frac: float = 0.10) -> None:
        """Precompute the 20 kHz slot grid as bin-edge indices into the fftshifted
        wideband spectrum. Called once when the frame/rate config changes. The detector
        power for each slot is then a segment-sum of |Xg_shifted|^2 between consecutive
        edges: all on the GPU, reusing the FFT process() already computes."""
        self._slot_bw = float(slot_bw)
        binwidth = self.fs_in / self.frame_len
        half = self.fs_in / 2.0
        lo = -half + edge_guard_frac * self.fs_in       # offset from capture center (Hz)
        hi =  half - edge_guard_frac * self.fs_in
        n_slots = int(np.floor((hi - lo) / self._slot_bw))
        # slot k covers [lo + k*bw, lo + (k+1)*bw); convert to fftshifted bin index.
        # fftshifted bin 0 is at -fs/2; bin i is at (-half + i*binwidth).
        edges_hz = lo + self._slot_bw * np.arange(n_slots + 1)     # (n_slots+1,)
        edges_bin = np.round((edges_hz + half) / binwidth).astype(np.int64)
        edges_bin = np.clip(edges_bin, 0, self.frame_len)
        self._slot_edges_gpu = cp.asarray(edges_bin)
        self._n_slots = int(n_slots)
        self._slot_lo_hz = lo
        self._slot_ready = True

    def _slot_powers(self, Xg) -> "cp.ndarray":
        """Segment-sum |Xg|^2 over the precomputed slot edges. Returns (n_slots,) dB on GPU.
        Uses cumulative-sum + edge-difference: cheap and fully vectorized. Shifts the
        real power array (4 B/elem) rather than the complex spectrum (8 B/elem), so the
        slot edges stay in fftshifted coordinates with half the copy cost."""
        p = Xg.real ** 2 + Xg.imag ** 2                 # |X|^2 (unshifted), no sqrt
        p = cp.fft.fftshift(p)                          # shift the real power, not Xg
        csum = cp.concatenate((cp.zeros(1, dtype=p.dtype), cp.cumsum(p)))
        edges = self._slot_edges_gpu
        slot_p = csum[edges[1:]] - csum[edges[:-1]]     # (n_slots,) power per slot
        # A near-empty slot subtracts two large nearly-equal cumsum
        # values; float rounding can make that tiny-negative then log10 then NaN, and
        # one NaN slot poisons the detector's np.median floor (all detection dies;
        # it happens at high fs_in where n_slots is large). Clamp to >= 0.
        slot_p = cp.maximum(slot_p, 0.0)
        return 10.0 * cp.log10(slot_p + 1e-20)

    def process(self, iq_host: np.ndarray, capture_center_hz: float,
                return_host: bool = True) -> dict[int, np.ndarray]:
        if self._dirty or self._bins2d_gpu is None:
            self._rebuild_bins(capture_center_hz)
        if self._bins2d_gpu is None:
            return {}

        xg = cp.asarray(iq_host.astype(np.complex64))    # host: GPU (one copy)
        Xg = cp.fft.fft(xg)                              # one shared FFT

        # energy detection: slot powers from the same FFT (no extra FFT/copy)
        self._last_slot_db = None
        if self._slot_ready and self._slot_edges_gpu is not None:
            self._last_slot_db = self._slot_powers(Xg)   # shifts the real power inside

        # gather all channels at once: (n_chan, n_out), already DC-at-column-0
        # (the roll is folded into the gather indices in _rebuild_bins)
        bands = Xg[self._bins2d_gpu]
        # one batched inverse FFT across all channels (axis=1)
        bb = cp.fft.ifft(bands, axis=1) * (1.0 / self.decim)

        if return_host:
            # transfer single-precision (no-op if cuFFT already returned complex64;
            # halves the copy if it returned complex128)
            bb = bb.astype(cp.complex64, copy=False)
            host = cp.asnumpy(bb)                        # one transfer for all channels
            self._last_slot_db_host = (cp.asnumpy(self._last_slot_db)
                                       if self._last_slot_db is not None else None)
            cp.cuda.Stream.null.synchronize()
            return {self._order[r]: host[r]
                    for r in range(len(self._order))}
        return {self._order[r]: bb[r] for r in range(len(self._order))}

    def last_slot_powers_db(self):
        """Return the most recent (n_slots,) slot-power array in dB (host), or None.
        Valid after a process(return_host=True) call with slots configured."""
        return getattr(self, "_last_slot_db_host", None)


def _validate() -> bool:
    if not _HAVE_CUPY:
        print("CuPy not available")
        return False
    rng = np.random.default_rng(0)
    fs_in, fc, fs_out = 50e6, 425e6, 250e3
    n = 65536
    t = np.arange(n) / fs_in
    offs = [-10e6, 5e6, 15e6]
    wb = (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64) * 0.02
    for fo in offs:
        wb += np.exp(2j * np.pi * fo * t).astype(np.complex64)

    ch = GPUChannelizerBatched(fs_in, fs_out, frame_len=n)
    for i, fo in enumerate(offs):
        ch.set_channel(i, ChanSpec(fc + fo, 100e3), fc)
    out = ch.process(wb, fc)
    ok = True
    for i, fo in enumerate(offs):
        bb = out[i]
        sp = np.fft.fftshift(np.fft.fft(bb * np.hanning(len(bb))))
        fax = np.fft.fftshift(np.fft.fftfreq(len(bb), 1.0 / ch.fs_out))
        peak = fax[np.argmax(np.abs(sp) ** 2)]
        good = abs(peak) < 3 * (ch.fs_out / len(bb))
        ok = ok and good
        print(f"  ch{i} off={fo/1e6:+g}MHz peak@{peak/1e3:+.2f}kHz {'OK' if good else 'FAIL'}")
    return ok


def _benchmark():
    if not _HAVE_CUPY:
        return
    rng = np.random.default_rng(0)
    fs_in, fc, fs_out = 50e6, 425e6, 250e3
    print("\nBATCHED GPU channelizer benchmark (all channels in one op)")
    print("=" * 72)
    for buf in (65536, 131072, 262144):
        frame_ms = buf / fs_in * 1e3
        wb = (rng.standard_normal(buf) + 1j * rng.standard_normal(buf)).astype(np.complex64)
        print(f"\nbuffer={buf}  frame period={frame_ms:.3f} ms")
        print(f"  {'chans':>5} | {'total ms':>9} | {'budget':>7} | verdict")
        print("  " + "-" * 52)
        for nch in (1, 5, 10, 20, 30):
            ch = GPUChannelizerBatched(fs_in, fs_out, frame_len=buf)
            offsets = np.linspace(-0.4 * fs_in, 0.4 * fs_in, nch)
            for i, fo in enumerate(offsets):
                ch.set_channel(i, ChanSpec(fc + fo, 100e3), fc)
            ch.process(wb, fc)
            cp.cuda.Stream.null.synchronize()
            iters = 50
            t0 = time.perf_counter()
            for _ in range(iters):
                ch.process(wb, fc)
            cp.cuda.Stream.null.synchronize()
            ms = (time.perf_counter() - t0) / iters * 1e3
            frac = ms / frame_ms
            verdict = "OK every-frame" if frac < 0.8 else ("tight" if frac < 1.0 else "TOO SLOW")
            print(f"  {nch:>5} | {ms:>9.3f} | {frac*100:>6.0f}% | {verdict}")
    print("\n" + "=" * 72)
    print("If 30 channels is now well under 80%, batching fixed the loop overhead")
    print("and every-frame 50 MHz channelization is proven on the GPU.")


if __name__ == "__main__":
    if _HAVE_CUPY:
        print("VALIDATE:", "PASS" if _validate() else "FAIL")
        _benchmark()
    else:
        print("Needs CuPy + GPU; run in the L4T container.")
