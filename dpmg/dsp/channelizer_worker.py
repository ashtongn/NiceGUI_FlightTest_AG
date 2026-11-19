"""
channelizer_worker.py: runs the GPU channelizer + demods on the live SDR stream.

Sits between the SDR scheduler (which produces wideband frames) and the GPU
channelizer service. Runs in its own thread so it never blocks the NiceGUI event
loop. Gets every frame (via a scheduler tap, not the lossy maxlen=4 display deque)
so no burst is dropped.

Flow:
  scheduler _rx_loop --(every frame)--> ChannelizerWorker.feed()
      -> FrameAccumulator (4096-blocks: 131072 channelizer frames)
      -> ChannelizerClient.process()  [shared memory then GPU service then back]
      -> per-channel baseband
      -> build_demod().demodulate()   [per channel]
      -> results dict (read by the UI)

Wiring into the app (minimal, opt-in):
  - SDRScheduler gets an optional `frame_hook` callable; _rx_loop calls it with each
    frame. When no channelizer is active, hook is None: zero behavior change.
  - The worker registers its feed() as that hook.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

from dpmg.dsp.channelizer_client import ChannelizerClient, FrameAccumulator

# continuous channels re-decode every CONT_PERIOD frames; 2 tiles the 2-frame
# decode window with no gap or overlap
CONT_PERIOD = 2

try:
    from dpmg.framing.frame_codec import find_and_decode
    try:
        from dpmg.dsp.demod_helper import demod_with_recovery
    except Exception:
        demod_with_recovery = None
    from dpmg.dsp.decode_log import get_log, DecodeEvent
    from dpmg.dsp.burst_history import get_history, BurstEvent
    _HAVE_CODEC = True
except Exception:
    _HAVE_CODEC = False
# wideband energy detector for gated channel selection
try:
    from dpmg.dsp.energy_detector import EnergyDetector, merge_adjacent
except Exception:
    EnergyDetector = None
    merge_adjacent = None


@dataclass
class ChannelRuntime:
    """One configured channel: its spec, demod, and latest decoded output."""
    idx: int
    f_center_hz: float
    bw_hz: float
    demod_type: str
    demod_params: dict = field(default_factory=dict)
    demod: object = None                # built demodulator instance
    last_bit_string: str = ""           # DemodResult.bit_string
    last_n_syms: int = 0                # len(DemodResult.symbols)
    last_snr_db: float = 0.0           # DemodResult SNR estimate if present
    frames_done: int = 0
    response_bits: str = ""             # UI-configured text to TX back on decode
    valid_decode: bool = False          # last frame passed sync+CRC (green trigger)
    last_message: str = ""              # decoded payload text when valid
    valid_count: int = 0                # cumulative valid decodes
    bb_buffer: object = None            # rolling baseband accumulator (np.ndarray)


try:
    from dpmg.dsp.burst_history import get_history, BurstEvent
    HAVE_HIST = True
except Exception:
    HAVE_HIST = False


class ChannelizerWorker:
    """Continuously channelize the live wideband stream and demod each channel."""

    def __init__(self, frame_len: int = 131072, fs_out: float = 250_000.0,
                 queue_max: int = 64):
        self.frame_len = int(frame_len)
        self.fs_out = float(fs_out)
        self._acc = FrameAccumulator(frame_len)
        self._client: Optional[ChannelizerClient] = None
        self._channels: list[ChannelRuntime] = []
        self._q: deque = deque(maxlen=queue_max)     # wideband blocks awaiting accumulation
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._fs_in = 50e6
        self._capture_center = 0.0
    

        self._det = None
        self._slot_bw = 20e3
        self._chan_bw = 150e3   # 150kHz: sps=10 at 15ksym/s
        self._symrate = 15e3
        self._thr_db = 12.0
        self._gated_rt = {}
        self._next_idx = 0
        # targeted mode state
        self._mode = 'blind'            # 'blind' or 'targeted'
        # declared channel plan (list of (ChannelConfig, entry)).
        # When set, _gate_frame matches detections to these bands (each with its own
        # modulation/symbol_rate/params) rather than minting hardcoded BPSK. Undeclared
        # detections are kept and tagged role='unknown' (watchlist + discovery).
        self._watchlist = []            # [(ChannelConfig, entry)]
        self._watch_tol_hz = 12e3
        self._watch_strict = True   # a loaded plan means decode only declared bands (ignore undeclared detections)
        self._targets = []              # list of absolute Hz to watch in targeted mode
        self._target_tol_hz = 30e3      # match tolerance
        self.gated_results = {}         # fkey: live result dict for the UI
        self._emit_counts = {}   # ch_name: {"det","dec","clean","last_det","last_dec"}
        self._dropped = 0
        self.backlog_frames = 16      # Max frames waiting in the accumulator
        self.backlog_drops = 0
        self._processed = 0
        self.det_slot_age = 0          # frames GPU detection is behind (GUI-visible)
        self._max_slot_age_seen = 0
        self.last_error: Optional[str] = None
        self.service_ok = False
        self._svc_miss = 0
        self.log_key: str = "x40"        # which decode_log to write to
        self.tx_callback = None          # optional: called(ch_idx, response_bits) on valid decode
        self.sealed_callback = None      # Called(ch, [bodies], f_center_hz): the radio forwards, never opens
        # optional: responder(ch, message): framed reply bits (or None to stay
        # silent). Set for modem transceive; when None, the static
        # ch.response_bits canned reply is used instead.
        self.responder = None
        # decode runs off the detect thread in a bounded pool. POOL_CORES: size
        # the pool to the box, not a fixed 5: on the 8-core Jetson a wideband
        # plan (25 channels) saturated 5 workers (drop_busy climbing) while ~60%
        # of CPU sat idle. Leave one core for the SCHED_FIFO RX drain + parent.
        import os as _os
        self._pool = None
        self._pool_max = max(5, (_os.cpu_count() or 4) - 1)
        self._probe_armed = False
        self.last_probe = ""

    @property
    def _gated(self):
        return getattr(self, '_mode', 'blind') != 'off'
    # configuration
    def set_channels(self, channels: list[ChannelRuntime],
                     build_demod_fn: Callable) -> None:
        """Configure the active channel set. build_demod_fn(ChannelRuntime)->demod."""
        with self._lock:
            self._build_demod_fn = build_demod_fn
            for ch in channels:
                if ch.demod is None and ch.demod_type:
                    try:
                        ch.demod = build_demod_fn(ch)
                    except Exception as e:
                        self.last_error = f"demod build ch{ch.idx}: {e}"
            self._channels = channels
            if self._client is not None:
                self._client.set_channels([(c.f_center_hz, c.bw_hz) for c in channels])

    # the scheduler tap: called with every frame 
    def feed(self, frame) -> None:
        """Hook for the scheduler _rx_loop. frame is an AnalysisFrame."""
        # in gated mode channels are created by the detector from incoming frames,
        # so don't bail on empty _channels when gating: let frames flow so the gate can run.
        if not self._channels and not getattr(self, "_gated", False):
            return
        self._fs_in = frame.sample_rate
        self._capture_center = frame.center_freq
        # push into the bounded queue; if full, count a drop (worker fell behind)
        if len(self._q) >= self._q.maxlen:
            self._dropped += 1
            return
        self._q.append((np.asarray(frame.samples, dtype=np.complex64),
                        getattr(frame, "rf_timestamp", None)))

    #  lifecycle 
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        try:
            self._client = ChannelizerClient(self.frame_len, self.fs_out)
            with self._lock:
                if self._channels:
                    self._client.set_channels(
                        [(c.f_center_hz, c.bw_hz) for c in self._channels])
            self.service_ok = True
        except Exception as e:
            self.last_error = f"client init: {e}"
            self.service_ok = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        if self._client:
            self._client.close()
            self._client = None

        # The pool must be shut down and the handle cleared: _ensure_pool() only rebuilds
        # `if self._pool is None`, so a dead pool left here is reused by the next start,
        # which raises RuntimeError: cannot schedule new futures after shutdown. The _run
        # thread then dies silently: the worker looks "up", processes nothing, logs nothing.
        if self._pool is not None:
            try:
                self._pool.shutdown(wait=False)
            except Exception:
                pass
            self._pool = None

    # the worker loop 
    def set_targets(self, freqs_hz, tol_hz: float = 30e3) -> None:
        """Set the list of absolute target frequencies (Hz) to watch in targeted mode."""
        self._targets = [float(x) for x in (freqs_hz or [])]
        self._target_tol_hz = float(tol_hz)

    def set_mode(self, mode: str) -> None:
        """mode: 'blind' (auto-discover) or 'targeted' (only the target list)."""
        m = str(mode).lower()
        self._mode = 'targeted' if m.startswith('t') else ('off' if m.startswith('o') else 'blind')

    def clear_results(self) -> None:
        self.gated_results = {}

    def set_plan(self, plan, tol_hz: float = 12e3, strict: bool = False) -> None:
        """Install a channel_plan as the watch-list. Detections are matched to its
        declared bands; each band supplies its own modulation/symbol_rate/params.
        strict=True ignores undeclared detections (pure watch-list, no discovery)."""
        try:
            from dpmg.scenario.plan_adapter import plan_to_watchlist
            wl = plan_to_watchlist(plan)
        except Exception as e:
            self.last_error = f"set_plan: {e}"; wl = []
        with self._lock:
            self._watchlist = [(cc, e) for (cc, e) in wl if cc is not None]
            self._watch_tol_hz = float(tol_hz)
            self._watch_strict = True   # always strict, `strict` is ignored; plan = declared-only
            self._mode = 'targeted'
            # Targeted mode: the plan declares the frequencies, so build the channels
            # directly instead of rediscovering them with a 1M-point FFT on every frame.
            # py-spy: the detector is _raw_fft 36% + _slot_power_db 24% + roll 13.5% of the
            # worker thread, and it is the only per-frame cost that does not scale with
            # channel count: which matches RXGAP being flat at 127-231 across 2/3/4/5
            # channels while RX alone measures 0.
            self._gated_rt = {}
            # Several plan entries may share one centre (DSSS
            # users at one frequency, each with its own code). _gated_rt keys
            # one runtime per slot (the gate looks slots up by frequency), so
            # the extra users ride along as siblings: they are full channels
            # (own demod params: own code in the decode pool, own baseband
            # slice from the GPU) that gate on their slot's detection.
            _siblings = []
            for _cc, _e in self._watchlist:
                _rt = self._rt_from_config(_cc, role="known")
                _k = int(round(_cc.f_center_hz / self._slot_bw))
                if _k in self._gated_rt:
                    _siblings.append((_k, _rt))
                else:
                    self._gated_rt[_k] = _rt
            _held = sorted(self._gated_rt.keys())          # same ordering as _gate_frame
            self._channels = [self._gated_rt[k] for k in _held]
            for _k, _rt in _siblings:                     # keep slot order: after their slot's first
                _pos = max(i for i, c in enumerate(self._channels)
                           if int(round(c.f_center_hz / self._slot_bw)) == _k)
                self._channels.insert(_pos + 1, _rt)
            self._overlay_siblings = [rt for _k, rt in _siblings]
            if _siblings:
                print(f"[PLAN] CDMA overlay: {len(_siblings)} extra user(s) share a slot "
                      f"({sorted(set(k for k, _ in _siblings))})", flush=True)
            self._targets = [float(cc.f_center_hz) for cc, _e in self._watchlist]
            self._target_tol_hz = float(tol_hz)
            for _i, _rt in enumerate(self._channels):
                _rt.idx = _i
                _fn = getattr(self, "_build_demod_fn", None)
                if _rt.demod is None and _rt.demod_type and _fn is not None:
                    try:
                        _rt.demod = _fn(_rt)
                    except Exception as _e:
                        self.last_error = f"demod build {_rt.ch_name}: {_e}"
            print(f"[PLAN] built {len(self._channels)} channels: "
                  f"{[(r.idx, round(r.f_center_hz/1e6,3), r.demod_type, r.decode, r.demod is not None) for r in self._channels]}",
                  flush=True)
            # Derive the gated symbol rate + fs_out from the plan's channels
            # so sps = fs_out/symrate is an integer (hardcoded 15k*10=150k gave 15.625 at
            # 9600 baud: detect-but-never-decode). Use the plan's dominant symbol rate.
            # set the gated symbol rate from the plan's dominant rate.
            # Do not null the client here: _ensure_detector rebuilds it when fs_out
            # changes, from inside the run loop; nulling it here deadlocks that loop
            # (no client then frame block skipped then _ensure_detector never runs).
            srates = [int(round(float(getattr(cc, "symbol_rate", 0) or 0)))
                      for cc, _e in self._watchlist
                      if getattr(cc, "decode", False) and getattr(cc, "symbol_rate", 0)]
            srates = [s for s in srates if s > 0]
            if srates:
                import math
                lcm = srates[0]
                for s in srates[1:]:
                    lcm = lcm * s // math.gcd(lcm, s)
                mult = 1
                while (lcm * mult) / max(srates) < 10:
                    mult += 1
                self._gate_fs_out = float(lcm * mult)
                self._symrate = float(max(srates))
                # SPREAD_FSOUT: DSSS despreads spread_factor chips per data
                # symbol and CSS occupies its full chirp bandwidth, so the
                # symbol-rate*10 floor starves them (sps=10 can't hold 15 chips;
                # 48k can't fit a 125k chirp). Raise fs_out to >= 2 chips/sample
                # for DSSS and >= the chirp bandwidth for CSS.
                _need_fs = self._gate_fs_out
                for cc, _e in self._watchlist:
                    if not getattr(cc, "decode", False):
                        continue
                    _m = str(getattr(cc, "demod_type", "")).upper()
                    _p = getattr(cc, "demod_params", None) or {}
                    _sr = float(getattr(cc, "symbol_rate", 0) or 0)
                    if "DSSS" in _m and _sr > 0:
                        _sf = float(_p.get("spread_factor", 15) or 15)
                        _need_fs = max(_need_fs, 2.0 * _sf * _sr)
                    elif "CSS" in _m or "LORA" in _m:
                        _need_fs = max(_need_fs, float(_p.get("css_bw", 125000.0))
                                       * float(_p.get("css_os", 1) or 1))
                if _need_fs > self._gate_fs_out:
                    print(f"[SPREAD_FSOUT] raising gate fs_out "
                          f"{self._gate_fs_out:.0f} -> {_need_fs:.0f} for spread",
                          flush=True)
                    self._gate_fs_out = float(_need_fs)
            if self._client is not None:
                self._client.set_channels(
                    [(c.f_center_hz, c.bw_hz) for c in self._channels])
            self._det = None

    def clear_plan(self) -> None:
        with self._lock:
            self._watchlist = []
            self._gated_rt = {}          # flush cached channels
            self._overlay_siblings = []  # CDMA overlay users
            self._mode = 'blind'

    def _rt_from_config(self, cc, role="known"):
        """Build a ChannelRuntime from a plan ChannelConfig (per-band params)."""
        rt = ChannelRuntime(idx=0, f_center_hz=cc.f_center_hz, bw_hz=cc.bw_hz,
                            demod_type=cc.demod_type,
                            demod_params={"symbol_rate": cc.symbol_rate, **(cc.demod_params or {})})
        rt.role = role
        rt.ch_name = getattr(cc, "name", "")
        rt.decode = bool(getattr(cc, "decode", False))
        rt.is_continuous = bool(getattr(cc, "is_continuous", False))
        rt.lock = {}                                       # per-channel acquire/track cache
        return rt

    def enable_gating(self, slot_bw: float = 20e3, chan_bw: float = 150e3,
                      symrate: float = 15e3, threshold_db: float = 12.0) -> None:
        self._slot_bw = float(slot_bw); self._chan_bw = float(chan_bw)
        self._symrate = float(symrate); self._thr_db = float(threshold_db)
        self._det = None

    def disable_gating(self) -> None:
        self._mode = 'off'

    def _ensure_detector(self):
        if EnergyDetector is None:
            return False
        # GATE-RATE: force fs_out so sps = fs_out/symrate is an integer (=10 at 15 ksym/s: 150k).
        # The tab may have set fs_out to a value giving non-integer sps (e.g. 105600: 7.04),
        # so the demod samples every symbol slightly off, never locks, and the CPU thrashes.
        want_fs_out = float(getattr(self, "_gate_fs_out", 0.0) or self._symrate * 10.0)
        if self._det is None and self._fs_in and self._capture_center:
            self._det = EnergyDetector(fs=self._fs_in, center_hz=self._capture_center,
                                       slot_bw=self._slot_bw, nfft=32768,
                                       threshold_db=self._thr_db)
            print("[GATE-RATE] detector built: fs_in=%.0f center=%.0f" %
                  (self._fs_in, self._capture_center), flush=True)
        return self._det is not None

    def _ensure_client_rate(self):
        """Rebuild the client when fs_out changes. Kept apart from _ensure_detector
        (blind FFT discovery): targeted mode needs this and must not pay for the
        detector, which FFTs every 1M-sample frame and is what overruns the card's
        271 ms RX FIFO (RXGAP 140/min: 0 with it off)."""
        want_fs_out = float(getattr(self, "_gate_fs_out", 0.0) or self._symrate * 10.0)
        if abs(float(getattr(self, "fs_out", 0.0)) - want_fs_out) > 1.0:
            self.fs_out = want_fs_out
            try:
                if self._client is not None:
                    self._client.close()
            except Exception:
                pass
            try:
                self._client = ChannelizerClient(self.frame_len, self.fs_out)
                print("[RATE] fs_out set to %.0f Hz (sps=%.1f at %.0f sym/s)" %
                      (self.fs_out, self.fs_out / self._symrate, self._symrate), flush=True)
                self._client.set_channels(
                    [(c.f_center_hz, c.bw_hz) for c in self._channels])
            except Exception as _e:
                self.last_error = "client rebuild: %s" % _e
        elif self._client is not None and self._mode == 'targeted' and self._channels:
            # Rate unchanged: re-assert the plan's channels every frame so targeted decode
            # never depends on _gate_frame. This is the decode-config path; _gate_frame
            # only detects, so the two cannot break each other.
            try:
                for _i, _c in enumerate(self._channels):
                    _c.idx = _i          # idx must match the client's 0..N-1 keys, not the
                                          # position in _gated_rt (which is polluted with blind
                                          # detections keyed differently: plan chans got idx 5-9)
                # Only push channels to the client when the set actually changed. Re-sending
                # the identical plan every frame made the GPU service rebuild its channelizer
                # every frame (reconfigure storm: GUI shows the GPU flapping up/down). The
                # plan is static in targeted mode, so this normally sends exactly once.
                _chan_sig = tuple((round(c.f_center_hz, 1), round(c.bw_hz, 1))
                                  for c in self._channels)
                if _chan_sig != getattr(self, "_last_chan_sig", None):
                    self._client.set_channels(
                        [(c.f_center_hz, c.bw_hz) for c in self._channels])
                    self._last_chan_sig = _chan_sig
                    print(f"[CHANS] pushed {len(self._channels)} channels to GPU "
                          f"(set changed)", flush=True)
            except Exception:
                pass

    def _hist(self):
        try:
            from dpmg.dsp.burst_history import get_history
            return get_history(getattr(self, "log_key", "g40"))
        except Exception:
            return None

    def _gate_frame(self, frame) -> None:
        if not self._ensure_detector():
            return
        try:
            dets = None
            # GPU energy detection: the channelizer already did the FFT on this frame and
            # returned per-slot power. process_slots() just thresholds: ~0ms CPU vs the
            # 136ms CPU FFT, which starves the RX drain. We always use the GPU slots
            # (never skip): a burst spans many frames, so even slightly-stale slots detect it.
            # slot_age (frames behind) is recorded for the GUI so staleness is visible.
            _cli = getattr(self, "_client", None)
            if _cli is not None and hasattr(_cli, "last_slot_powers"):
                _sdb, _meta = _cli.last_slot_powers()
                if _sdb is not None and _meta is not None:
                    _lo_hz, _bw = _meta
                    dets = self._det.process_slots(_sdb, _lo_hz, _bw, self._capture_center)
                    # expose detection staleness to the GUI (0 = fresh this frame)
                    self.det_slot_age = int(_cli.slot_age()) if hasattr(_cli, "slot_age") else 0
                    if self.det_slot_age > self._max_slot_age_seen:
                        self._max_slot_age_seen = self.det_slot_age
            if dets is None:
                # Slots have never arrived yet (cold start only, before the first GPU result).
                # Do one CPU-FFT detection so blind bring-up isn't blind; after the GPU starts
                # returning slots this branch never runs again.
                self._det_cold = getattr(self, "_det_cold", 0) + 1
                if self._det_cold <= 3:
                    dets = self._det.process(frame)
                else:
                    return   # give up on CPU FFT after cold start; GPU should be feeding by now
            if merge_adjacent is not None:
                dets = merge_adjacent(dets, max_gap=2)
        except Exception as e:
            self.last_error = f"detector: {e}"
            return
        # keep only detections within tol of a target frequency, and snap their
        # center to the matched target for stable channel identity.
        if self._mode == 'targeted' and self._targets:
            kept = []
            for d in dets:
                best = None; bestdf = 1e18
                for tf in self._targets:
                    df = abs(d.center_hz - tf)
                    if df < bestdf:
                        bestdf = df; best = tf
                if best is not None and bestdf <= self._target_tol_hz:
                    d.center_hz = best   # snap to the target center
                    kept.append(d)
            dets = kept
        active_slots = {d.slot: d for d in dets}
        # key channels by rounded frequency (nearest slot_bw) so +/-1 slot jitter
        # maps to the same persistent channel; hysteresis keeps a channel alive HOLD_FRAMES after
        # it drops out so brief gaps don't wipe the accumulated bb_buffer.
        HOLD_FRAMES = 15
        def _fkey(hz):
            # key by absolute freq/slot_bw (not offset from capture_center, which is 0.0
            # until the first RX frame: plan loaded before RX would key off 0 Hz and never
            # match detections). Absolute key matches whether computed from plan center or
            # a snapped detection, independent of load order.
            return int(round(hz / self._slot_bw))
        # if a plan is installed, match detections to declared bands so
        # each channel carries its own modulation/symbol_rate/params; keep undeclared
        # detections as role='unknown' unless strict. Otherwise fall back to blind BPSK.
        if not hasattr(self, "_hold"):
            self._hold = {}
        _wl = getattr(self, "_watchlist", [])
        # Match each detection to a plan band first, then key by the band's fixed
        # center. Emitters whose offset lands on a half-slot boundary (e.g. +750kHz
        # = 37.5 slots) otherwise jitter between two keys frame-to-frame: a new
        # channel is minted every frame, its bb_buffer never accumulates to `need`,
        # and it detects endlessly but never decodes. Keying matched channels by the
        # plan center makes the key stable regardless of detection jitter.
        active_keys = {}
        match_for = {}
        for d in dets:
            _best, _bd = None, 1e18
            if _wl:
                for cc, _e in _wl:
                    df = abs(d.center_hz - cc.f_center_hz)
                    if df < _bd:
                        _bd, _best = df, cc
            if _best is not None and _bd <= self._watch_tol_hz:
                kk = _fkey(_best.f_center_hz)      # stable: keyed by plan center
                active_keys[kk] = d
                match_for[kk] = _best
            else:
                active_keys[_fkey(d.center_hz)] = d   # blind/unmatched: raw center
                match_for[_fkey(d.center_hz)] = None
        for k, d in active_keys.items():
            rt = self._gated_rt.get(k)
            if rt is None:
                best = match_for.get(k)
                if _wl:
                    if best is not None:
                        rt = self._rt_from_config(best, role="known")
                        rt.f_center_hz = best.f_center_hz   # snap to declared center
                    elif getattr(self, "_watch_strict", False):
                        continue   # strict: ignore undeclared detection
                    else:
                        rt = ChannelRuntime(idx=0, f_center_hz=d.center_hz,
                                            bw_hz=self._chan_bw, demod_type="BPSK",
                                            demod_params={"symbol_rate": self._symrate})
                        rt.role = "unknown"
                else:
                    rt = ChannelRuntime(idx=0, f_center_hz=d.center_hz,
                                        bw_hz=self._chan_bw, demod_type="BPSK",
                                        demod_params={"symbol_rate": self._symrate})
                    rt.role = "unknown"
                self._gated_rt[k] = rt
            else:
                if getattr(rt, "role", "unknown") != "known":
                    rt.f_center_hz = d.center_hz
            _was = k in getattr(self, "_active_keys_prev", set())
            self._hold[k] = HOLD_FRAMES
            import time as _ht
            # Counter + last_det update every frame the channel is detected, so Det counts
            # ongoing detections and Present lights live. (In targeted mode the emitters are
            # continuously present: the rising-edge _h.add below fires once, but the counter
            # must keep ticking.) The DETECTED burst-history event stays rising-edge-only to
            # avoid spamming the log.
            _ec = self._emit_counts.setdefault(getattr(rt, "ch_name", ""),
                  {"det":0,"dec":0,"clean":0,"last_det":0.0,"last_dec":0.0})
            _ec["det"] += 1
            _ec["last_det"] = _ht.time()
            if not _was and HAVE_HIST:
                try:
                    _h = self._hist()
                    if _h is not None:
                        _h.add(BurstEvent(ts=_ht.time(), kind="DETECTED",
                               offset_khz=(d.center_hz - self._capture_center)/1e3,
                               freq_hz=d.center_hz, power_db=float(d.power_db), demod=rt.demod_type))
                        self._gated_rt[k]._t_detected = _ht.time()
                except Exception:
                    pass
            try:
                # scoreboard shows SNR margin over the detector's median floor, not raw
                rt.last_power_db = float(d.power_db) - float(getattr(self._det, "_floor_db", 0.0) or 0.0) 
            except Exception:
                pass
        # Users sharing a slot live outside _gated_rt (one key per
        # slot); count the slot's detection on them too so each user's row
        # shows Present and its SNR margin like the slot's first channel.
        for _sib in getattr(self, "_overlay_siblings", ()):
            _sk = _fkey(getattr(_sib, "f_center_hz", 0.0))
            if _sk in active_keys:
                import time as _st
                _ec = self._emit_counts.setdefault(getattr(_sib, "ch_name", ""),
                      {"det":0,"dec":0,"clean":0,"last_det":0.0,"last_dec":0.0})
                _ec["det"] += 1
                _ec["last_det"] = _st.time()
                try:
                    _sib.last_power_db = float(active_keys[_sk].power_db) - \
                        float(getattr(self._det, "_floor_db", 0.0) or 0.0)
                except Exception:
                    pass
        for k in list(self._hold.keys()):
            if k not in active_keys:
                self._hold[k] -= 1
                if self._hold[k] <= 0:
                    if HAVE_HIST:
                        try:
                            _h = self._hist(); _rt = self._gated_rt.get(k)
                            if _h is not None and _rt is not None:
                                import time as _ht
                                _dt = _ht.time() - getattr(_rt, "_t_detected", _ht.time())
                                _h.add(BurstEvent(ts=_ht.time(), kind="LOST",
                                       offset_khz=(_rt.f_center_hz - self._capture_center)/1e3,
                                       freq_hz=_rt.f_center_hz, duration_s=_dt, demod=rt.demod_typ))
                        except Exception:
                            pass
                    self._hold.pop(k, None)
                    self._gated_rt.pop(k, None)
        # rising-edge set: keys whose burst started this gate pass. Decode submits on
        # these only: the 2-frame window then contains the whole burst, and we stop
        # burning workers on post-burst noise while HOLD keeps the channel alive.
        self._rising_keys = set(active_keys.keys()) - getattr(self, "_active_keys_prev", set())
        self._active_keys_prev = set(active_keys.keys())
        self._active_keys = set(active_keys.keys())   # continuous gate reads this
        held_keys = sorted(self._gated_rt.keys())
        new_channels = [self._gated_rt[k] for k in held_keys]
        for i, rt in enumerate(new_channels):
            rt.idx = i
        with self._lock:
            if self._mode != 'targeted':
                self._channels = new_channels
                if self._client is not None:
                    self._client.set_channels([(c.f_center_hz, c.bw_hz) for c in new_channels])
            # targeted: plan owns _channels; _ensure_client_rate keeps the client configured.
            # The gate only detects + counts here: it touches nothing decode depends on.

    def _run(self) -> None:
        while not self._stop.is_set():
            # drain the queue into the accumulator
            got = False
            while self._q:
                try:
                    blk = self._q.popleft()
                except IndexError:
                    break
                self._acc.push(blk[0], blk[1])
                got = True
            # The queue above is bounded, but everything in it moves into
            # the accumulator every pass, and the accumulator has no bound of its own: when the GPU path
            # runs slower than the RX rate (61.44 MSps with a plan: ~32 of the 58.6 1M-sample
            # frames a second) the backlog grows without limit (about 78 MB/s of RSS on
            # the G40) until RX stops. Past backlog_frames frames, drop the backlog
            # (counted as dropped frames, like a full queue) and go on from new samples.
            if self._acc._have > self.frame_len * self.backlog_frames:
                self._dropped += self._acc._have // self.frame_len
                self.backlog_drops += 1
                self._acc._buf.clear()
                self._acc._have = 0

            if self._acc.ready() and self._client is not None:
                frame = self._acc.pop()
                self._ensure_client_rate()
                if self._gated:
                    self._det_frame = getattr(self, "_det_frame", 0) + 1
                    # Detect every frame: the GPU detector costs ~0ms, and with 20ms
                    # bursts any DET_EVERY>1 lets bursts fall between detection frames
                    # where the decode gate never sees them.
                    DET_EVERY = 1
                    if self._det_frame % DET_EVERY == 0 or not self._channels:
                        self._gate_frame(frame)
                    if not self._channels:
                        import time as _t; _t.sleep(0.005); continue
                
                try:
                
                    out = self._client.process(frame, self._fs_in,
                                               self._capture_center, timeout_s=0.05)
                except Exception as e:
                    self.last_error = f"process: {e}"
                    out = None
                except Exception as e:
                    self.last_error = f"process: {e}"
                    out = None
                if out is None:
                    # one timed-out frame is normal under load: don't flap the GUI. Only
                    # declare the service down after several consecutive misses (hysteresis).
                    self._svc_miss = getattr(self, "_svc_miss", 0) + 1
                    if self._svc_miss >= 5:
                        self.service_ok = False
                else:
                    self._svc_miss = 0
                    self.service_ok = True
                    self._processed += 1
                    if self._processed % 20 == 0:
                        _ps = self._pool.stats() if self._pool is not None else {}
                        print(f"[STAT] processed={self._processed} dropped={self._dropped} "
                              f"({100*self._dropped/max(1,self._processed+self._dropped):.0f}% dropped) "
                              f"det_age={self.det_slot_age} max_age={self._max_slot_age_seen} "
                              f"pool: sub={_ps.get('submitted',0)} done={_ps.get('completed',0)} "
                              f"drop_busy={_ps.get('dropped_busy',0)} inflight={_ps.get('inflight',0)}",
                              flush=True)
                    if getattr(self, "_probe_armed", False):
                        self._do_probe(frame, out); self._probe_armed = False
                    self._demod_channels(out)
                    self._pool_collect()          # apply finished decodes
                    # decode runs off-thread in the pool, so no long throttle
                    # is needed; a tiny yield keeps the event loop fed
                    # without falling behind the injector.
                    import time as _t
                    _t.sleep(0.003)
            else:
                # yield CPU whenever we did not process a full frame.
                # (The RX flood keeps the queue non-empty, so 'got' is almost always True;
                # a sleep only on 'not got' would never fire, the loop would spin a core
                # at 100%, starve the NiceGUI event loop and the browser would refresh
                # every ~10 s.)
                time.sleep(0.002)

    def _ensure_pool(self):
        # Rebuild if the pool is missing or has shut down / broken. A ProcessPoolExecutor
        # whose child process died marks itself shutdown permanently; every subsequent
        # submit throws "cannot schedule new futures after shutdown". Detect and rebuild so
        # decode recovers instead of silently dying for the rest of the run.
        if self._pool is not None and hasattr(self._pool, "is_healthy"):
            if not self._pool.is_healthy():
                try:
                    self._pool.shutdown()
                except Exception:
                    pass
                self._pool = None
                print("[POOL] decode pool shut down/broken -- rebuilding", flush=True)
        if self._pool is None:
            try:
                from dpmg.dsp.decode_pool import DecodePool
                self._pool = DecodePool(max_workers=self._pool_max)
            except Exception as e:
                self.last_error = f"pool init: {e}"
        return self._pool

    def _slot_margin_db(self, f_center_hz):
        """GPU slot power of the slot holding f_center_hz over the median of all
        slots (the floor process_slots thresholds against), from the client's
        last slot powers. None when no slots have arrived or the centre is off
        the slot grid."""
        _cli = getattr(self, "_client", None)
        if _cli is None or not hasattr(_cli, "last_slot_powers"):
            return None
        _sdb, _meta = _cli.last_slot_powers()
        if _sdb is None or _meta is None:
            return None
        _lo_hz, _bw = _meta
        pdb = np.nan_to_num(np.asarray(_sdb, dtype=np.float64),
                            nan=-300.0, posinf=-300.0, neginf=-300.0)
        if pdb.size == 0 or not _bw:
            return None
        s = int(np.floor((float(f_center_hz) - float(self._capture_center)
                          - float(_lo_hz)) / float(_bw)))
        if s < 0 or s >= pdb.size:
            return None
        return float(pdb[s] - np.median(pdb))

    def _targeted_gate(self, ch) -> bool:
        """Decide whether a targeted-mode channel decodes this frame. Burst
        channels submit on the rising edge only (2-frame window then holds the
        whole burst). Continuous channels never re-rise, so they submit every
        CONT_PERIOD frames while active, per-channel-phased so simultaneous
        continuous channels don't all land on the pool in the same frame."""
        ck = int(round(float(getattr(ch, 'f_center_hz', 0)) / self._slot_bw))
        if getattr(ch, 'is_continuous', False):
            # Opt-in per plan channel (params "decode_gate_db").
            # The detection set needs the slot 12 dB (_thr_db) over the median
            # floor: for a narrow FSK in a 20 kHz slot that is Es/N0 ~19 dB,
            # far above the decoder's own edge (FEC-03 / M-08 tie). With the
            # key set, the channel is gated on its own slot margin against
            # that value instead. Cost stays bounded without a new limiter: a
            # failing window counts a miss in decode_pool._default_decode and
            # 3 misses park the channel for 3 s (cooldown_until, merged back
            # via lock_out), so a channel that never decodes costs at most 3
            # windows per ~6 s (~100-140 ms each on x86), and the pool's key
            # coalescing + worker cap keep it from queueing behind itself.
            _dg = (getattr(ch, 'demod_params', None) or {}).get('decode_gate_db')
            if _dg is not None:
                _m = self._slot_margin_db(getattr(ch, 'f_center_hz', 0))
                if _m is None:
                    if ck not in getattr(self, '_active_keys', set()):
                        return False   # no slot powers yet: stock gate
                elif _m <= float(_dg):
                    return False
            elif ck not in getattr(self, '_active_keys', set()):
                return False
            phase = int(getattr(ch, 'idx', 0)) % CONT_PERIOD
            return (getattr(self, '_frame_ctr', 0) % CONT_PERIOD) == phase
        return ck in getattr(self, '_rising_keys', set())

    def _demod_channels(self, out: dict[int, np.ndarray]) -> None:
        import numpy as _np
        self._frame_ctr = getattr(self, "_frame_ctr", 0) + 1
        submissions = []
        with self._lock:
            for _i, _c in enumerate(self._channels):
                _c.idx = _i
            for ch in self._channels:
                bb_new = out.get(ch.idx)
                if bb_new is None:
                    continue
                # Cache this frame's baseband every frame, before any gating `continue`:
                # the two-frame decode window below needs the immediately-previous frame,
                # not the previous *submitted* one.
                _cur = _np.asarray(bb_new)
                _prev = getattr(ch, "_prev_bb", None)
                ch._prev_bb = _cur.copy()
                # Gate decode to currently-bursting channels. With 25 staggered-burst plan
                # channels, the channelizer produces a buffer for all 25 every frame, but only
                # ~2-3 are actually transmitting. Submitting all 25 to the decode pool saturates
                # it: most decodes drop. Only submit channels the detector fired on last frame
                # (i.e. actually have energy = actually bursting). _fkey keys by abs freq/slot_bw,
                # matching how _active_keys_prev is built.
                if self._mode == 'targeted' and not self._targeted_gate(ch):
                    continue
                # ch.demod is only needed for the dBFS readout; decode_pool builds its own
                # demodulator via get_demodulator. Targeted-mode channels come from the plan
                # with symbol_rate in demod_params, so let them through.
                if ch.demod is None and not getattr(self, '_gated', False) \
                   and not (ch.demod_params or {}).get("symbol_rate"):
                    continue
                fs_out = float(getattr(self, "fs_out", 124800) or 124800)
                symrate = ch.demod_params.get("symbol_rate") if ch.demod_params else None
                if not symrate:
                    symrate = getattr(ch.demod, "symbol_rate", None)
                if not symrate or symrate <= 0:
                    symrate = fs_out / 13.0
                # The channelizer decimates by an integer, so the real output rate is
                # fs_in/round(fs_in/fs_out), not the requested fs_out (30.72e6/102 =
                # 301176.47 Hz, 3922 ppm high). Using the requested rate makes sps wrong
                # by ~0.05, giving ~0.56 symbols of drift per 144-symbol frame: a one-symbol
                # slip mid-frame. Use the actual rate and carry sps as a float.
                _fs_in = float(getattr(self, "_fs_in", 0) or 0)
                if _fs_in > 0:
                    _decim = max(1, int(round(_fs_in / fs_out)))
                    fs_out_act = _fs_in / _decim
                else:
                    fs_out_act = fs_out
                sps = max(2.0, fs_out_act / float(symrate))
                # The FFT-slice-IFFT channelizer is a circular filter: exactly the two
                # samples at each block edge are wrap-around-contaminated. The decode
                # window is [previous frame, this frame] concatenated (built below), with
                # EDGE samples trimmed off each end so the join is interior and clean.
                EDGE = 4
                if len(bb_new) <= 2 * EDGE:
                    continue
                if _prev is not None and len(_prev) == len(_cur):
                    _win = _np.concatenate((_prev, _cur))
                else:
                    _win = _cur
                bb = _win[EDGE:-EDGE]
                _fr = (ch.demod_params or {}).get("frame") or {}
                _fb = int(_fr.get("frame_bits") or 144)
                _bps = 2 if "4FSK" in str(ch.demod_type).upper() else 1
                need = int(((_fb / _bps) + 32) * sps)
                if need > len(bb):
                    # On a channel's first frame only this frame's
                    # block exists; the decode window is [previous, this] from
                    # the next frame on. Call it "NEVER" only when that full
                    # two-frame window is short too (it printed for DSSS m/gold
                    # at 609.6 kHz on their first frame, which then decoded).
                    # The block is what the GPU service returns: at most
                    # MAX_OUT samples of the frame's n_out (gpu_service cap),
                    # so a truncated window is also two non-contiguous pieces.
                    _full = 2 * len(_cur) - 2 * EDGE
                    if need > _full and not getattr(ch, "_need_warned", False):
                        _nout = (int(self.frame_len) // max(1, int(round(_fs_in / fs_out)))
                                 if _fs_in > 0 else len(_cur))
                        _tr = (f"; GPU returns {len(_cur)} of {_nout} samples/frame"
                               if _nout > len(_cur) else "")
                        print(f"[NEED] {getattr(ch,'ch_name','?')}: need={need} > "
                              f"window={_full} (sps={sps:.2f}{_tr}) -- can NEVER decode",
                              flush=True)
                        ch._need_warned = True
                    continue
                if len(bb) < need:
                    continue
                # only flagged channels go to the (expensive) decode pool.
                # detect/characterize-only channels stop here: presence is already
                # recorded by _gate_frame; they never pay the recovery cost.
                if not getattr(ch, "decode", False):
                    continue
                submissions.append((ch.idx, _np.asarray(bb).copy(), ch.demod_type,
                                    fs_out_act, sps, dict(ch.demod_params or {}), ch))
        pool = self._ensure_pool()
        if pool is None:
            return
        from dpmg.dsp.decode_pool import DecodeSpec
        for idx, bb, mod, fs_out, sps, params, ch in submissions:
            if getattr(ch, "lock", None) is None:
                ch.lock = {}
            import os as _os
            if _os.path.exists("/tmp/xcv_dump") and int(getattr(ch, "_xcv_dumped", 0)) < 3:
                try:
                    import numpy as _dnp   # XCVDUMP: the exact buffer fed to decode
                    _cnt = int(getattr(ch, "_xcv_dumped", 0))
                    _dnp.save(f"/tmp/xcv_buf_{mod}_{_cnt}.npy", _dnp.asarray(bb))
                    _dnp.save(f"/tmp/xcv_meta_{mod}_{_cnt}.npy",
                              _dnp.asarray([fs_out, sps, len(bb)], dtype=float))
                    print(f"[XCVDUMP] {mod} #{_cnt} len={len(bb)} sps={sps:.3f} "
                          f"fs={fs_out:.1f} params={params}", flush=True)
                    ch._xcv_dumped = _cnt + 1
                except Exception as _e:
                    print(f"[XCVDUMP] failed: {_e}", flush=True)
            pool.submit(idx, bb, DecodeSpec(modulation=mod, fs_out=fs_out, sps=sps,
                                            demod_params=params, lock=ch.lock))

    def _pool_collect(self) -> None:
        pool = self._pool
        if pool is None:
            return
        results = pool.collect()
        if not results:
            return
        with self._lock:
            by_idx = {ch.idx: ch for ch in self._channels}
            for r in results:
                ch = by_idx.get(r.key)
                if ch is not None:
                    self._apply_decode_result(ch, r)

    def _apply_decode_result(self, ch, r) -> None:
        """Apply a pool DecodeResult to a channel, with every side effect of a
        decode: green trigger, burst history, gated_results, decode_log, tx_back.

        A single demodulated buffer can carry several concatenated frames
        (find_all_frames: r.messages). Each message runs the full side-effect
        chain in order, so a multi-message burst (PING/PONG train) yields every
        reply, not just the first. Falls back to the single r.message when the
        pool didn't populate the list (older results / direct callers)."""
        ch.last_bit_string = r.bit_string or ""
        ch.last_n_syms = int(r.n_syms or 0)
        ch.last_snr_db = float(r.snr_db or 0.0)
        ch.frames_done += 1
        if getattr(r, "sealed", None):
            # The child found CRC-valid sealed bodies. The radio
            # holds no keys, so nothing is opened here: the bodies go to the
            # service (sealed_callback: SEALED_FRAME to the ground station)
            # and the band shows a placeholder, never a plaintext.
            bodies = [bytes(b) for b in r.sealed]
            ch.secure_last = {"sealed": len(bodies), "t": __import__("time").time()}
            ch.sealed_count = int(getattr(ch, "sealed_count", 0)) + len(bodies)
            ch.last_message = f"[sealed {bodies[0][0]} B x{len(bodies)}]"
            ch.valid_decode = True
            _ec = self._emit_counts.setdefault(getattr(ch, "ch_name", ""),
                  {"det": 0, "dec": 0, "clean": 0, "last_det": 0.0, "last_dec": 0.0})
            _ec["dec"] += len(bodies)
            _ec["last_dec"] = __import__("time").time()
            if self.sealed_callback is not None:
                try:
                    self.sealed_callback(ch, bodies, float(getattr(ch, "f_center_hz", 0.0)))
                except Exception:                                  # noqa: BLE001
                    pass
            return
        ch.valid_decode = bool(r.valid)
        if not r.valid:
            return
        messages = list(getattr(r, "messages", None) or [])
        if not messages and r.message:
            messages = [r.message]
        for _msg in messages:
            self._apply_one_message(ch, r, _msg)

    def _apply_one_message(self, ch, r, msg) -> None:
        """Run every valid-decode side-effect for one decoded message."""
        ch.last_message = msg
        ch.valid_count += 1
        import time as _dt
        _ec = self._emit_counts.setdefault(getattr(ch, "ch_name", ""),
              {"det":0,"dec":0,"clean":0,"last_det":0.0,"last_dec":0.0})
        _ec["dec"] += 1; _ec["last_dec"] = _dt.time()
        _ec["clean"] += 1
        if HAVE_HIST:
            try:
                _h = self._hist()
                if _h is not None:
                    import time as _ht
                    _h.add(BurstEvent(ts=_ht.time(), kind="DECODED",
                           offset_khz=(ch.f_center_hz - self._capture_center)/1e3,
                           freq_hz=ch.f_center_hz,
                           power_db=float(getattr(ch, "last_power_db", 0.0)),
                           message=msg, demod=ch.demod_type))
            except Exception:
                pass
        try:
            import time as _rt
            _fk = int(round((ch.f_center_hz - self._capture_center) / self._slot_bw))
            _prev = self.gated_results.get(_fk, {})
            self.gated_results[_fk] = dict(
                freq_hz=ch.f_center_hz,
                offset_khz=(ch.f_center_hz - self._capture_center) / 1e3,
                power_db=float(getattr(ch, "last_power_db", 0.0)),
                message=msg,
                count=int(_prev.get("count", 0)) + 1,
                last_ts=_rt.time())
        except Exception:
            pass
        try:
            get_log(self.log_key).add(DecodeEvent(
                ts=__import__("time").time(), ch_idx=ch.idx, valid=True,
                message=msg, bits=(r.bit_string or "")[:128],
                snr_db=float(getattr(ch, "last_snr_db", 0.0))))
        except Exception as _e:
            self.last_error = "declog: %s" % _e
        print("[XCV] ch%d valid decode: %r (snr %.1f dB)" % (
            ch.idx, msg, float(r.snr_db or 0.0)), flush=True)
        # Reply: a modem responder computes it from the decoded message; absent
        # one, fall back to the static UI-configured canned reply.
        reply_bits = ch.response_bits
        if self.responder is not None and msg:
            try:
                reply_bits = self.responder(ch, msg)
            except Exception as e:
                self.last_error = f"responder ch{ch.idx}: {e}"
                reply_bits = None
        if reply_bits and self.tx_callback is not None:
            try:
                self.tx_callback(ch.idx, reply_bits)
            except Exception as e:
                self.last_error = f"tx_cb ch{ch.idx}: {e}"

    # per-band state for the plan UI
    def band_status(self) -> list:
        """One row per declared watch-list band: present? power? decoding? message?"""
        with self._lock:
            present = {}
            for ch in self._channels:
                nm = getattr(ch, "ch_name", "") or ""
                if nm:
                    present[nm] = ch
            rows = []
            for cc, e in getattr(self, "_watchlist", []):
                nm = getattr(cc, "name", "")
                ch = present.get(nm)
                rows.append(dict(
                    name=nm, f_center_hz=float(cc.f_center_hz),
                    modulation=cc.demod_type,
                    decode=bool(getattr(cc, "decode", False)),
                    present=(self._emit_counts.get(nm, {}).get("last_det", 0.0)
                             > __import__("time").time() - 2.0),
                    power_db=(float(getattr(ch, "last_power_db", 0.0)) if ch else None),
                    decoded=(self._emit_counts.get(nm, {}).get("last_dec", 0.0)
                             > __import__("time").time() - 2.0),
                    message=(getattr(ch, "last_message", "") if ch else ""),
                    valid_count=int(self._emit_counts.get(nm, {}).get("dec", 0)),
                    detected_n=int(self._emit_counts.get(nm, {}).get("det", 0)),
                    decoded_n=int(self._emit_counts.get(nm, {}).get("dec", 0)),
                    clean_n=int(self._emit_counts.get(nm, {}).get("clean", 0)),
                ))
            return rows

    def discovered_unknown(self) -> list:
        """Detections not matched to any declared band (blind discovery)."""
        with self._lock:
            out = []
            for ch in self._channels:
                if getattr(ch, "role", "unknown") == "unknown":
                    out.append(dict(f_center_hz=float(ch.f_center_hz),
                                    power_db=float(getattr(ch, "last_power_db", 0.0))))
            return out

    def set_band_decode(self, name: str, on: bool) -> None:
        """Flip a band's decode flag live (watch-list config + any live runtime)."""
        on = bool(on)
        with self._lock:
            for cc, e in getattr(self, "_watchlist", []):
                if getattr(cc, "name", "") == name:
                    cc.decode = on
            for ch in self._channels:
                if getattr(ch, "ch_name", "") == name:
                    ch.decode = on
            for ch in getattr(self, "_gated_rt", {}).values():
                if getattr(ch, "ch_name", "") == name:
                    ch.decode = on

    # diagnostic: save a flagged channel's baseband 
    def dump_flagged_baseband(self, out="/tmp/chan_bb.npy", only_name=None): 
        import numpy as _np
        with self._lock:
            for ch in self._channels:
                bb = getattr(ch, "_prev_bb", None)
                if only_name and getattr(ch, "ch_name", "") != only_name:
                    continue
                if getattr(ch, "decode", False) and bb is not None and len(bb) > 500:
                    arr = _np.asarray(bb, dtype=_np.complex64).copy()
                    meta = dict(name=getattr(ch, "ch_name", ""),
                                f_center=float(ch.f_center_hz),
                                fs_out=float(getattr(self, "fs_out", 0) or 0),
                                capture_center=float(getattr(self, "_capture_center", 0) or 0),
                                fs_in=float(getattr(self, "_fs_in", 0) or 0),
                                symbol_rate=(ch.demod_params or {}).get("symbol_rate", 0),
                                demod_type=ch.demod_type, n=len(arr))
                    _np.save(out, arr)
                    _np.save(out.replace(".npy", "_meta.npy"), _np.array([meta], dtype=object))
                    self.last_error = f"dumped {meta['name']} ({len(arr)} samp) -> {out}"
                    return meta
        self.last_error = "dump: no flagged channel with baseband yet"
        return None

    def reset_counters(self) -> None:
        """Zero the scoreboard: per-emitter det/dec/clean counters and per-channel
        cumulative decode state. Burst history has its own CLEAR button."""
        with self._lock:
            for _ec in self._emit_counts.values():
                _ec.update(det=0, dec=0, clean=0, last_det=0.0, last_dec=0.0)
            for ch in self._channels:
                ch.valid_count = 0
                ch.frames_done = 0
                ch.last_message = ""
                ch.last_bit_string = ""
                ch.valid_decode = False

    def probe_pipeline(self):
        """Arm a one-shot capture on the next processed frame."""
        self._probe_armed = True
        return "probe armed -- next frame"

    def _do_probe(self, frame, out):
        import numpy as _np
        def _peaks(x, fs, topn=8, thresh_db=-30):
            X = _np.abs(_np.fft.fftshift(_np.fft.fft(x[:min(len(x),131072)]))) ** 2
            if X.max() <= 0: return []
            Xdb = 10*_np.log10(X/X.max()+1e-20)
            f = _np.fft.fftshift(_np.fft.fftfreq(min(len(x),131072), 1.0/fs))
            pk = sorted(set(round(float(v)/1e6, 2) for v in f[Xdb > thresh_db]))
            return pk[:40]
        wb = _np.asarray(getattr(frame, "samples", frame))   # Frame is the popped ndarray
        # live RX rate (set from each frame in feed(); 0 only before the first
        # frame, in which case the MHz axis is meaningless so skip the peaks).
        _fs = float(getattr(self, "_fs_in", 0.0) or 0.0)
        _cen = float(getattr(self, "_capture_center", 0.0) or 0.0)
        wb_pk = _peaks(wb, _fs) if _fs > 0 else []
        wb_pw = 20*_np.log10(_np.mean(_np.abs(wb))+1e-12)
        lines = [f"[PROBE] INPUT wideband: n={len(wb)} fs={_fs/1e6:g}MHz "
                 f"center={_cen/1e6:.4f} pwr={wb_pw:.1f}dBFS peaks@MHz={wb_pk}"]
        fso = float(getattr(self, "fs_out", 96000) or 96000)
        for idx, bb in (out or {}).items():
            bb = _np.asarray(bb)
            if len(bb) < 8:
                continue
            opk = _peaks(bb, fso, thresh_db=-15)
            opw = 20*_np.log10(_np.mean(_np.abs(bb))+1e-12)
            nm = ""
            for ch in self._channels:
                if ch.idx == idx: nm = getattr(ch, "ch_name", ""); break
            lines.append(f"[PROBE]  ch{idx} {nm}: n={len(bb)} pwr={opw:.1f}dBFS "
                         f"peak_kHz={[round(p*1000,1) for p in opk]}")
        self.last_probe = "\n".join(lines)
        print(self.last_probe, flush=True)

    # status for the UI
    def status(self) -> dict:
        return dict(processed=self._processed, dropped=self._dropped,
                    service_ok=self.service_ok, last_error=self.last_error,
                    n_channels=len(self._channels))


# Minimal self-check of the worker plumbing (no GPU/service needed: stubs the client).
if __name__ == "__main__":
    import types, sys

    # stub ChannelizerClient so we can test the worker loop without the service
    class _StubClient:
        def __init__(self, *a, **k): pass
        def set_channels(self, ch): self._ch = ch
        def process(self, frame, fs_in, cap, timeout_s=1.0):
            return {i: np.zeros(frame_len // 200, dtype=np.complex64)
                    for i in range(len(self._ch))}
        def close(self): pass

    from dpmg.dsp import channelizer_worker as W
    W.ChannelizerClient = _StubClient
    frame_len = 131072

    class _Frame:
        sample_rate = 50e6; center_freq = 425e6
        samples = np.zeros(4096, dtype=np.complex64)

    class _Res:
        bit_string = "0101"; soft_bits_llr = [0.1]; symbols = [1,2,3]; snr_db = 12.0
    class _Demod:
        def demodulate(self, bb): return _Res()

    w = W.ChannelizerWorker(frame_len=frame_len)
    chans = [W.ChannelRuntime(idx=i, f_center_hz=425e6 + i*1e6, bw_hz=100e3,
                              demod_type="BPSK") for i in range(3)]
    w.set_channels(chans, build_demod_fn=lambda c: _Demod())
    w.start()
    for _ in range(40 * 33):       # ~33 blocks/frame * 40 frames
        w.feed(_Frame())
        time.sleep(0.0005)
    time.sleep(0.5)
    w.stop()
    print("worker status:", w.status())
    print("WORKER PLUMBING:", "PASS" if w.status()["processed"] > 0 else "FAIL")
