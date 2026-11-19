"""
energy_detector.py: wideband energy detector + fixed-grid slot mapping for the DPMG channelizer.

Design (survey-receiver pattern):
  wideband frame (30.72 MSps), one FFT, power per 20 kHz slot, adaptive threshold, a
  list of active slots. The worker then set_channels() only the active slots, so the expensive
  channelize+demod runs only where energy exists. Silent slots cost nothing but one FFT bin sum.

Import-safe and hardware-free so it can be unit-tested offline (see selftest at end).

Public API:
    det = EnergyDetector(fs=30.72e6, center_hz=425.025e6, slot_bw=20e3,
                         grid_start_hz=..., grid_stop_hz=..., nfft=32768,
                         threshold_db=10.0, floor_alpha=0.05)
    active = det.process(wideband_iq)      # -> list of Detection(slot, center_hz, power_db, t)
    det.slot_centers                       # array of slot center freqs (Hz, absolute)

Burst tracking (rising/falling edge: start/stop/peak) is in BurstTracker; it clusters the
per-slot detections into one burst per emitter first (cluster_detections, DET-02).
"""
from __future__ import annotations
import numpy as np
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Detection:
    slot: int
    center_hz: float
    power_db: float
    bin_lo: int
    bin_hi: int
    # slot span of the emitter this detection stands for (cluster_detections
    # fills it; a raw per-slot detection spans just its own slot)
    slot_lo: Optional[int] = None
    slot_hi: Optional[int] = None
    # measured emitter frequency (Hz, absolute): the slot's interpolated PSD
    # peak (EnergyDetector.process); defaults to the slot centre where no PSD
    # exists (process_slots / GPU powers, merge_adjacent)
    freq_hz: Optional[float] = None
    # True when a spectral valley separates this slot's peak from the peak of
    # the active slot just below it (two emitters, not one spilling over the
    # boundary); cluster_detections then treats the pair like a gap
    valley_below: bool = False

    def __post_init__(self):
        if self.slot_lo is None:
            self.slot_lo = self.slot
        if self.slot_hi is None:
            self.slot_hi = self.slot
        if self.freq_hz is None:
            self.freq_hz = self.center_hz

    @property
    def n_slots(self) -> int:
        return int(self.slot_hi - self.slot_lo + 1)


@dataclass
class BurstEvent:
    slot: int
    center_hz: float
    t_start: float
    t_stop: Optional[float] = None
    peak_power_db: float = -999.0
    message: Optional[str] = None    # filled after decode
    slot_lo: Optional[int] = None    # emitter span (slots) at the peak observation
    slot_hi: Optional[int] = None
    freq_hz: Optional[float] = None  # measured frequency at the peak observation

    def __post_init__(self):
        if self.slot_lo is None:
            self.slot_lo = self.slot
        if self.slot_hi is None:
            self.slot_hi = self.slot
        if self.freq_hz is None:
            self.freq_hz = self.center_hz

    @property
    def n_slots(self) -> int:
        return int(self.slot_hi - self.slot_lo + 1)


class EnergyDetector:
    def __init__(self, fs: float, center_hz: float, slot_bw: float = 20e3,
                 grid_start_hz: Optional[float] = None, grid_stop_hz: Optional[float] = None,
                 nfft: int = 32768, threshold_db: float = 10.0, floor_alpha: float = 0.05,
                 guard_frac: float = 1.0):
        """
        fs           : wideband sample rate (Hz)
        center_hz    : RX LO / capture center (Hz, absolute)
        slot_bw      : channel slot width (Hz), e.g. 20e3
        grid_start/stop_hz : absolute freq range for the grid; default = full band minus 10% edges
        nfft         : FFT size for the periodogram (power of 2)
        threshold_db : dB above the adaptive noise floor to declare a slot active
        floor_alpha  : EWMA rate for the per-slot noise floor (0..1; smaller = slower)
        guard_frac   : fraction of slot_bw integrated (centred). 1.0 (default) tiles the
                       band: every FFT bin belongs to exactly one slot. Below 1.0 each slot
                       edge gets a dead strip of (1-guard_frac)*slot_bw in which a clean
                       tone is invisible (0.9 gives a 2 kHz blind strip, see below).

        Slot-boundary blind strip: with guard_frac 0.9 a CW
        tone on a 20 kHz boundary (all 200 kHz comb tones, the +300 kHz port
        probe, a 0 Hz emitter) falls in the 2 kHz gap between two slots' 18 kHz
        windows; a Hann-windowed tone keeps its energy within +-2 bins (+-117 Hz
        at 1.92 MSps, +-1.9 kHz at 30.72), so neither slot sees it and only a TX
        skirt makes it show, 10 kHz off. With the default the slots tile the band, a boundary
        tone lights both neighbours (cluster_detections merges them) and the
        reported frequency is the PSD peak, not the slot centre.
        """
        self.fs = float(fs)
        self.center_hz = float(center_hz)
        self.slot_bw = float(slot_bw)
        self.nfft = int(nfft)
        self.threshold_db = float(threshold_db)
        self.floor_alpha = float(floor_alpha)
        self.guard_frac = float(guard_frac)

        half = self.fs / 2.0
        lo = (center_hz - half) if grid_start_hz is None else grid_start_hz
        hi = (center_hz + half) if grid_stop_hz is None else grid_stop_hz
        # keep slots fully inside the band (10% edge guard by default via grid range)
        # build slot centers on a fixed grid spaced by slot_bw
        n_slots = int(np.floor((hi - lo) / self.slot_bw))
        first_center = lo + self.slot_bw / 2.0
        self.slot_centers = first_center + self.slot_bw * np.arange(n_slots)   # absolute Hz
        self.n_slots = n_slots

        # FFT bin frequencies (absolute Hz), fftshifted
        self.bin_hz = np.fft.fftshift(np.fft.fftfreq(self.nfft, 1.0 / self.fs)) + self.center_hz
        self.binwidth = self.fs / self.nfft
        # precompute the bin index range for each slot (integrate guard_frac of the slot)
        if self.guard_frac >= 1.0:
            # tile the band: one set of edges, slot s = bins [edge s, edge s+1),
            # so a bin exactly on a boundary goes to the upper slot and none is
            # dropped or counted twice
            edges = np.searchsorted(self.bin_hz, first_center - self.slot_bw / 2.0
                                    + self.slot_bw * np.arange(n_slots + 1))
            self._bin_lo = edges[:-1]
            self._bin_hi = edges[1:]
        else:
            halfw = (self.slot_bw * self.guard_frac) / 2.0
            self._bin_lo = np.searchsorted(self.bin_hz, self.slot_centers - halfw)
            self._bin_hi = np.searchsorted(self.bin_hz, self.slot_centers + halfw)
        # peak search: how far a slot's edge-bin peak may climb into the
        # neighbour (the same spectral line spilling over the boundary), the
        # smoothing used for the valley test, and the valley test itself
        self._climb_bins = max(3, int(np.ceil(2e3 / self.binwidth)))
        self._smooth_bins = max(5, int(round(1e3 / self.binwidth)))
        self.valley_min_sep_hz = self.slot_bw / 2.0
        self.valley_db = 10.0
        self._last_psd = None
        self._last_n = self.nfft          # samples behind the last PSD (zero-padding)
        # per-slot noise floor estimate (dB), lazily initialized
        self._floor_db = None
        self._win = np.hanning(self.nfft).astype(np.float32)
        self._win_pow = np.sum(self._win ** 2)

    def _slot_power_db(self, iq: np.ndarray) -> np.ndarray:
        # average periodogram over as many nfft chunks as the frame allows
        n = len(iq)
        if n < self.nfft:
            x = np.zeros(self.nfft, dtype=np.complex64); x[:n] = iq
            chunks = [x]
        else:
            nchunks = n // self.nfft
            chunks = [iq[k*self.nfft:(k+1)*self.nfft] for k in range(nchunks)]
        acc = np.zeros(self.nfft, dtype=np.float64)
        for c in chunks:
            X = np.fft.fftshift(np.fft.fft(c * self._win))
            acc += (np.abs(X) ** 2)
        psd = acc / (len(chunks) * self._win_pow * self.fs)   # power spectral density
        self._last_psd = psd
        self._last_n = min(n, self.nfft)
        # integrate PSD over each slot's bins: slot power (linear), then dB
        slot_p = np.empty(self.n_slots, dtype=np.float64)
        for s in range(self.n_slots):
            lo, hi = self._bin_lo[s], self._bin_hi[s]
            slot_p[s] = np.sum(psd[lo:hi]) * self.binwidth if hi > lo else 1e-20
        return 10.0 * np.log10(slot_p + 1e-20)

    def process_slots(self, slot_db, slot_lo_hz: float, slot_bw: float,
                      capture_center_hz: float, t: float = 0.0) -> list[Detection]:
        """Detect from GPU-computed slot powers instead of an on-CPU FFT.

        slot_db          : (n_slots,) slot power in dB, from the GPU channelizer
        slot_lo_hz       : offset (Hz) of slot 0's low edge from capture center
        slot_bw          : slot width (Hz) used on the GPU
        capture_center_hz: absolute RF center, to turn offsets into absolute Hz

        The expensive FFT + periodogram is gone; this is just the median-floor threshold
        the CPU path always did, over the GPU's grid. ~microseconds vs ~136 ms.
        """
        import numpy as _np
        pdb = _np.asarray(slot_db, dtype=_np.float64)
        if pdb.size == 0:
            return []
        # A single NaN slot (from a degenerate GPU slot) makes
        # np.median return NaN: the threshold compares against NaN and nothing
        # detects. nanmedian for the floor + NaN slots can never exceed it.
        pdb = _np.nan_to_num(pdb, nan=-300.0, posinf=-300.0, neginf=-300.0)
        floor = float(_np.median(pdb))          # signals are a minority of slots
        self._floor_db = floor
        active = pdb > (floor + self.threshold_db)
        dets = []
        for s in _np.nonzero(active)[0]:
            # slot s center = capture_center + slot_lo_hz + (s + 0.5)*slot_bw
            cen = float(capture_center_hz) + float(slot_lo_hz) + (int(s) + 0.5) * float(slot_bw)
            dets.append(Detection(slot=int(s), center_hz=cen,
                                   power_db=float(pdb[s]), bin_lo=0, bin_hi=0))
        return dets

    def _peak_bin(self, psd: np.ndarray, s: int) -> tuple[int, float]:
        """Strongest bin of slot s and its interpolated frequency (Hz, absolute).
        A peak on the slot's edge bin climbs (at most _climb_bins) into the
        neighbour while the PSD keeps rising: a tone on or just past the
        boundary belongs to one spectral line, whichever slot holds more of it.
        Parabolic interpolation on the dB values of the 3 bins round the peak."""
        lo, hi = int(self._bin_lo[s]), int(self._bin_hi[s])
        if hi <= lo:
            return lo, float(self.slot_centers[s])
        k = lo + int(np.argmax(psd[lo:hi]))
        n = len(psd)
        for step in (-1, 1):
            if (step < 0 and k == lo) or (step > 0 and k == hi - 1):
                for _ in range(self._climb_bins):
                    j = k + step
                    if 0 <= j < n and psd[j] > psd[k]:
                        k = j
                    else:
                        break
        f = float(self.bin_hz[k])
        if 0 < k < n - 1:
            a, b, c = 10.0 * np.log10(psd[k - 1:k + 2] + 1e-30)
            den = a - 2.0 * b + c
            if den < 0:
                f += float(np.clip(0.5 * (a - c) / den, -0.5, 0.5)) * self.binwidth
        return k, f

    def _valley_between(self, psd: np.ndarray, k1: int, k2: int) -> bool:
        """True when two adjacent active slots hold two emitters: their peak bins
        k1 < k2 are at least valley_min_sep_hz apart and the mean PSD over the
        central half of the gap sits valley_db below the weaker peak (each peak
        = mean over ~1 kHz centred on it). A tone spilling over the boundary
        (one peak), a skirt (falls away monotonically) or a wideband emitter
        (flat: the long central mean has low variance, the short peak means sit
        only a few dB above it) does not qualify. The peak window widens with
        zero-padding (frame shorter than nfft: fewer independent bins) but
        never past slot_bw/4."""
        if (k2 - k1) * self.binwidth < self.valley_min_sep_hz:
            return False
        pad = max(1.0, self.nfft / float(max(1, self._last_n)))
        w = int(min(max(self._smooth_bins, round(self._smooth_bins * pad)),
                    max(5, round(self.slot_bw / 4.0 / self.binwidth))))
        h = w // 2
        if k1 - h < 0 or k2 + h + 1 > len(psd):
            return False
        p1 = float(np.mean(psd[k1 - h:k1 + h + 1]))
        p2 = float(np.mean(psd[k2 - h:k2 + h + 1]))
        m, q = (k1 + k2) // 2, max(1, (k2 - k1) // 4)
        valley = float(np.mean(psd[m - q:m + q + 1]))
        return 10.0 * np.log10(min(p1, p2) / (valley + 1e-30)) >= self.valley_db

    def process(self, iq: np.ndarray, t: float = 0.0) -> list[Detection]:
        pdb = self._slot_power_db(iq)
        psd = self._last_psd
        # median floor: signals are a minority of slots, so they barely move it
        floor = float(np.median(pdb))
        self._floor_db = floor
        active_mask = pdb > (floor + self.threshold_db)
        dets = []
        prev = None                       # (slot, peak bin) of the previous active slot
        for s in np.nonzero(active_mask)[0]:
            k, f = self._peak_bin(psd, int(s))
            valley = (prev is not None and prev[0] == s - 1
                      and self._valley_between(psd, prev[1], k))
            dets.append(Detection(slot=int(s), center_hz=float(self.slot_centers[s]),
                                   power_db=float(pdb[s]), bin_lo=int(self._bin_lo[s]),
                                   bin_hi=int(self._bin_hi[s]), freq_hz=f,
                                   valley_below=bool(valley)))
            prev = (int(s), k)
        return dets


def merge_adjacent(dets, max_gap: int = 1):
    if not dets:
        return []
    dets = sorted(dets, key=lambda d: d.slot)
    merged = []; run=[dets[0]]
    for d in dets[1:]:
        if d.slot - run[-1].slot <= max_gap: run.append(d)
        else: merged.append(_merge_run(run)); run=[d]
    merged.append(_merge_run(run)); return merged

def _merge_run(run):
    p=np.array([10**(d.power_db/10) for d in run])
    cen=float(np.sum([d.center_hz for d in run]*p)/np.sum(p))
    pk=max(run,key=lambda d:d.power_db)
    return Detection(slot=pk.slot,center_hz=cen,power_db=pk.power_db,
                     bin_lo=run[0].bin_lo,bin_hi=run[-1].bin_hi)

DEFAULT_CLUSTER_MARGIN_DB = 20.0
DEFAULT_CLUSTER_GAP = 1


def cluster_detections(dets, margin_db: float = DEFAULT_CLUSTER_MARGIN_DB,
                       gap: int = DEFAULT_CLUSTER_GAP) -> list:
    """Collapse per-slot Detections into one Detection per emitter.

    A CW tone (or any emitter narrower than a slot) lights its own slot plus a
    skirt of neighbouring slots 25-50 dB down (TX phase noise / leakage), so a
    per-slot tracker reports one burst per skirt slot: on the G40 an 8-tone
    comb gave 48-118 bursts instead of 8 (DET-02). This groups
    each contiguous run of active slots into emitters:

      * a run is a sequence of active slots with at most `gap` inactive slots
        between neighbours (gap=1 bridges one inactive slot, so the far tail
        of a skirt that flickers above threshold one slot beyond the run is
        still judged against the tone; gap=0 joins adjacent slots only);
      * within a run, a slot is an emitter if it is a local maximum and its
        level is within `margin_db` of the strongest emitter already accepted
        next to it (on either side). A weaker bump is treated as skirt. Two
        comparable emitters therefore stay separate whether they touch or sit
        one inactive slot apart;
      * every other slot joins the emitter it descends from: the valley
        between two accepted emitters is split at its lowest slot.

    Returns one Detection per emitter carrying the peak slot's slot/center_hz/
    power_db/freq_hz (measured PSD peak) and slot_lo/slot_hi = the cluster's span.
    Two adjacent active slots whose PSD peaks are >= half a slot apart with a
    >= 10 dB spectral valley between them (Detection.valley_below, set by
    EnergyDetector.process) are split like slots with a gap, so two tones
    either side of one boundary stay two; a boundary tone (one peak spilling
    into both slots) and a skirt (falls away monotonically) never set it. An isolated active slot
    passes through unchanged. The known limit: an emitter more than margin_db
    weaker than a neighbour within the bridged run (no more than `gap`
    inactive slots between them) is reported inside that neighbour's burst,
    whose span widens accordingly. The IQ image of a tone (40-48 dB down on
    the G40) is not touched: it sits at the mirror frequency, outside the run,
    and is reported as its own emitter whenever it clears the threshold.
    margin_db None or <= 0 disables clustering (legacy per-slot output).
    """
    if not dets:
        return []
    if margin_db is None or float(margin_db) <= 0.0:
        return list(dets)
    margin_db = float(margin_db)
    gap = max(0, int(gap))
    dets = sorted(dets, key=lambda d: d.slot)
    runs = []
    run = [dets[0]]
    for d in dets[1:]:
        if d.slot - run[-1].slot <= gap + 1:
            run.append(d)
        else:
            runs.append(run)
            run = [d]
    runs.append(run)

    out = []
    for run in runs:
        n = len(run)
        if n == 1:
            out.append(run[0])
            continue
        lvl = [float(d.power_db) for d in run]
        # local maxima (plateau: leftmost slot wins). A bridged inactive slot
        # is a valley, so the slot on either side of it counts as a maximum
        # on that side.
        # A spectral valley between two adjacent active slots (EnergyDetector
        # .process: two tones either side of a boundary) splits them the same way.
        gap_before = [i > 0 and (run[i].slot - run[i - 1].slot > 1
                                 or bool(getattr(run[i], "valley_below", False)))
                      for i in range(n)]
        cands = [i for i in range(n)
                 if (i == 0 or gap_before[i] or lvl[i] >= lvl[i - 1])
                 and (i == n - 1 or gap_before[i + 1] or lvl[i] > lvl[i + 1])]
        cands.sort(key=lambda i: -lvl[i])
        accepted: list[int] = []
        for i in cands:
            left = [j for j in accepted if j < i]
            right = [j for j in accepted if j > i]
            neigh = []
            if left:
                neigh.append(lvl[max(left)])
            if right:
                neigh.append(lvl[min(right)])
            if not neigh or lvl[i] >= max(neigh) - margin_db:
                accepted.append(i)
        accepted.sort()
        # split the run between consecutive emitters at the valley slot
        bounds = []          # (lo_idx, hi_idx) per accepted emitter, inclusive
        lo = 0
        for k, pk in enumerate(accepted):
            if k + 1 < len(accepted):
                nxt = accepted[k + 1]
                gaps = [j for j in range(pk + 1, nxt + 1) if gap_before[j]]
                if gaps:                      # an inactive slot between them: split there
                    hi = gaps[0] - 1
                else:
                    hi = min(range(pk + 1, nxt), key=lambda j: lvl[j])
            else:
                hi = n - 1
            bounds.append((lo, hi))
            lo = hi + 1
        for pk, (lo_i, hi_i) in zip(accepted, bounds):
            p = run[pk]
            out.append(Detection(slot=p.slot, center_hz=p.center_hz, power_db=p.power_db,
                                 bin_lo=run[lo_i].bin_lo, bin_hi=run[hi_i].bin_hi,
                                 slot_lo=run[lo_i].slot, slot_hi=run[hi_i].slot,
                                 freq_hz=p.freq_hz, valley_below=run[lo_i].valley_below))
    return out


DEFAULT_HOLD_TICKS = 1
DEFAULT_ISLAND_SLOTS = 3


class BurstTracker:
    """Turn per-frame Detections into start/stop burst events with peak power.

    With cluster_margin_db set (the default) the per-slot detections are first
    collapsed to one per emitter (see cluster_detections) and an open burst is
    continued by any cluster whose slot span overlaps the span it was last
    seen with: so a tone straddling a slot boundary, whose peak slot flips
    between frames, stays one burst. cluster_margin_db=None restores the
    legacy one-burst-per-slot behaviour.

    Time splits (DET-02 at 1.92 MSps): a continuously keyed comb
    can drop out of the detector for one tick ~1-1.4 s into the key, so every
    tone closes and reopens (8 tones: 16 bursts). hold_ticks keeps a burst
    open through that many consecutive ticks with no overlapping cluster; it
    continues if the emitter comes back, and otherwise closes with t_stop =
    the first missed tick (durations are unchanged; the detection is only
    emitted hold_ticks ticks later). An emitter that is off for more than
    hold_ticks ticks still gives two bursts. hold_ticks=0 = close on the first
    miss.

    Islands: the same comb shows one-tick clusters 1-3 inactive slots from a
    tone, 7-14 dB below it, on the tick the tone drops out. A new cluster
    within island_slots inactive slots of a burst that was already open (or
    held) before this tick, and weaker than that burst's peak, is an island:
      * more than cluster_margin_db below the peak: skirt, never opens a
        burst (the same margin cluster_detections folds skirt with);
      * within the margin: pending: it opens only if an overlapping cluster
        is seen again on the next tick (t_start back-dated to the first
        sighting), so a real weaker neighbour still gets its burst one tick
        late and a one-tick island gets none.
    A new cluster at least as strong as the burst, or farther away, opens at
    once. island_slots=-1 (or clustering off) disables the rule.
    """
    def __init__(self, cluster_margin_db: Optional[float] = DEFAULT_CLUSTER_MARGIN_DB,
                 cluster_gap: int = DEFAULT_CLUSTER_GAP,
                 hold_ticks: int = DEFAULT_HOLD_TICKS,
                 island_slots: int = DEFAULT_ISLAND_SLOTS):
        self.cluster_margin_db = cluster_margin_db
        self.cluster_gap = int(cluster_gap)
        self.hold_ticks = max(0, int(hold_ticks))
        self.island_slots = int(island_slots)
        self._open: dict[int, BurstEvent] = {}   # burst id: open event
        self._span: dict[int, tuple[int, int]] = {}   # burst id: last seen span
        self._miss: dict[int, int] = {}          # burst id: consecutive missed ticks
        self._t_miss: dict[int, float] = {}      # burst id: time of the first miss
        self._pending: list[tuple[Detection, float]] = []   # islands awaiting a 2nd tick
        self._next_id = 0
        self.completed: list[BurstEvent] = []

    def _clustering(self) -> bool:
        return self.cluster_margin_db is not None and float(self.cluster_margin_db) > 0.0

    def _close(self, bid: int, t_stop: float) -> BurstEvent:
        ev = self._open.pop(bid)
        self._span.pop(bid, None)
        self._miss.pop(bid, None)
        self._t_miss.pop(bid, None)
        ev.t_stop = t_stop
        self.completed.append(ev)
        return ev

    def _open_burst(self, d: Detection, t_start: float, peak_db: float) -> None:
        ev = BurstEvent(slot=d.slot, center_hz=d.center_hz, t_start=t_start,
                        peak_power_db=peak_db, slot_lo=d.slot_lo, slot_hi=d.slot_hi,
                        freq_hz=d.freq_hz)
        bid = self._next_id
        self._next_id += 1
        self._open[bid] = ev
        self._span[bid] = (d.slot_lo, d.slot_hi)

    def _island_ref(self, d: Detection, refs) -> Optional[float]:
        """Peak (dB) of the nearest pre-existing burst that makes `d` an
        island, or None when `d` may open a burst now."""
        if self.island_slots < 0 or not self._clustering():
            return None
        best = None
        for (lo, hi), peak in refs:
            between = max(d.slot_lo - hi, lo - d.slot_hi) - 1   # inactive slots between
            if between <= self.island_slots and d.power_db < peak:
                if best is None or peak > best:
                    best = peak
        return best

    def update(self, dets: list[Detection], t: float) -> list[BurstEvent]:
        if self._clustering():
            dets = cluster_detections(dets, self.cluster_margin_db, self.cluster_gap)
        refs = [(self._span[b], ev.peak_power_db) for b, ev in self._open.items()]
        unmatched = list(dets)
        closed = []
        for bid in list(self._open.keys()):
            ev = self._open[bid]
            lo, hi = self._span[bid]
            best = None
            for d in dets:
                if d.slot_lo <= hi and d.slot_hi >= lo:       # spans overlap
                    if best is None or abs(d.slot - ev.slot) < abs(best.slot - ev.slot):
                        best = d
            if best is None:
                n = self._miss.get(bid, 0) + 1
                if n == 1:
                    self._t_miss[bid] = t
                if n > self.hold_ticks:
                    closed.append(self._close(bid, self._t_miss.get(bid, t)))
                else:
                    self._miss[bid] = n                     # held: may come back
                continue
            self._miss.pop(bid, None)
            self._t_miss.pop(bid, None)
            if best.power_db > ev.peak_power_db:
                ev.peak_power_db = best.power_db
                ev.slot = best.slot
                ev.center_hz = best.center_hz
                ev.freq_hz = best.freq_hz
                ev.slot_lo, ev.slot_hi = best.slot_lo, best.slot_hi
            self._span[bid] = (best.slot_lo, best.slot_hi)
            if best in unmatched:
                unmatched.remove(best)
        pending, self._pending = self._pending, []
        for d in unmatched:
            prev = next((p for p in pending
                         if d.slot_lo <= p[0].slot_hi and d.slot_hi >= p[0].slot_lo), None)
            if prev is not None:                            # island seen twice: real
                pending.remove(prev)
                self._open_burst(d if d.power_db >= prev[0].power_db else prev[0],
                                 prev[1], max(d.power_db, prev[0].power_db))
                continue
            ref = self._island_ref(d, refs)
            if ref is None:
                self._open_burst(d, t, d.power_db)
            elif d.power_db > ref - float(self.cluster_margin_db):
                self._pending.append((d, t))
            # else: skirt island > margin below an open burst: no burst
        return closed

    def flush(self, t: float) -> list[BurstEvent]:
        """Close every open (and held) burst now: end of capture or a retune.
        A held burst keeps t_stop = its first missed tick; pending islands are
        dropped."""
        self._pending = []
        return [self._close(bid, self._t_miss.get(bid, t)) for bid in list(self._open.keys())]


# self-test
if __name__ == "__main__":
    # synthesize a 30.72 MHz band with 3 tones (stand-ins for bursts) at known offsets
    fs = 30.72e6; center = 425.025e6; N = 262144
    t = np.arange(N) / fs
    x = (0.01 * (np.random.randn(N) + 1j*np.random.randn(N))).astype(np.complex64)  # noise floor
    for off_khz, amp in [(-5000, 0.3), (0, 0.5), (7000, 0.4)]:
        x += (amp * np.exp(2j*np.pi*(off_khz*1e3)*t)).astype(np.complex64)
    det = EnergyDetector(fs=fs, center_hz=center, slot_bw=20e3, nfft=32768, threshold_db=12.0)
    print("grid: %d slots, %.3f..%.3f MHz, binwidth %.0f Hz" %
          (det.n_slots, det.slot_centers[0]/1e6, det.slot_centers[-1]/1e6, det.binwidth))
    # warm up the floor on noise-only, then detect
    noise = (0.01*(np.random.randn(N)+1j*np.random.randn(N))).astype(np.complex64)
    for _ in range(5): det.process(noise)
    dets = det.process(x)
    print("detections:")
    for d in sorted(dets, key=lambda d:-d.power_db)[:10]:
        print("  slot %d  center %.3f MHz  peak %+.2f kHz  power %.1f dB" %
              (d.slot, d.center_hz/1e6, (d.freq_hz-center)/1e3, d.power_db))
    tr = BurstTracker()
    closed = tr.update(dets, t=0.0)
    closed2 = tr.flush(t=0.05)        # all off next frame: bursts close
    print("closed bursts:", [(e.slot, round((e.freq_hz-center)/1e3, 2), round(e.t_start,3), round(e.t_stop,3), round(e.peak_power_db,1)) for e in closed2])
