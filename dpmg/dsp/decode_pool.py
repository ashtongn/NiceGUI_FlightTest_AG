"""
decode_pool.py: bounded, swap-ready decode pool for the channelizer.

Keeps the expensive per-channel demod/decode off the detect-scan thread so the
25-channel scan stays responsive while <=N channels decode behind it.

Process backend: the cold acquisition search in _default_decode
is a Python-loop-bound CFO x timing sweep (~589 ms) that holds the GIL. With a
ThreadPoolExecutor, several concurrent cold acquires serialized on the GIL pegged
one core and starved the SCHED_FIFO RX drain thread: RXGAP splices (the RX
hardware FIFO overflowed because the drain loop's Python glue couldn't get the
GIL). ProcessPoolExecutor gives each decode its own interpreter/GIL, so cold
acquires run in parallel on the otherwise-idle cores and never contend with RX.

The per-channel `lock` cache (cfo/timing, "locked", miss counters) can't be mutated
in the parent from a child process, so it is round-tripped: the child returns the
updated lock dict in DecodeResult.lock_out, and the parent merges it back into the
channel's lock before the next submit. Tracking (warm re-sync) therefore still works
across frames; DecodePool.collect() does the merge through a registered lock
store.

Usage:
    pool = DecodePool(max_workers=5)
    pool.submit(key, baseband_np, spec)      # non-blocking; coalesces duplicate keys
    for result in pool.collect():            # non-blocking; whatever finished
        ...apply result...                   # lock cache already merged back
    pool.shutdown()
"""
from __future__ import annotations
import threading, queue, time, os
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Optional

# Uncoded clear frames: the plan's sync tolerance applies to the first this many
# sync bits only (see the find_frame path in _default_decode)
SYNC_ERR_HEAD = 2
_DEMOD_FAIL_SEEN: set = set()


@dataclass
class DecodeSpec:
    """Everything a decode needs that isn't the samples. Carries the per-channel
    lock cache so acquisition happens once and subsequent frames just track."""
    modulation: str
    fs_out: float
    sps: float  # true rate is fractional (fs_in/decim / symrate)
    demod_params: dict = field(default_factory=dict)
    lock: dict = field(default_factory=dict)   # cfo/timing cache: round-tripped for processes


@dataclass
class DecodeResult:
    key: object
    ok: bool
    valid: bool = False
    message: str = ""
    messages: list = field(default_factory=list)  # all frames in the buffer (find_all_frames); message is messages[0]
    bit_string: str = ""
    n_syms: int = 0
    snr_db: float = 0.0
    elapsed_ms: float = 0.0
    acquired: bool = False        # True if this call did a full (cold) acquisition
    error: str = ""
    lock_out: Optional[dict] = None   # updated lock cache to merge back in the parent (process backend)
    # Sealed bodies (LEN||HDR||CT||TAG, CRC-checked) found by the child;
    # the parent opens them (secure_frame.open_result): children hold no keys.
    sealed: list = field(default_factory=list)
    secure: Optional[dict] = None     # verdict summary once opened (parent side)


def _proc_init():
    # spawn children get a fresh interpreter: put the repo root of the tree this
    # module came from at the front of the path so the decode imports
    # (frame_codec, demod_helper, dpmg.*) resolve to the same tree the parent
    # runs, and nice them (+10) so they never
    # starve the latency-critical RX drain (SCHED_FIFO 15) or the parent worker.
    # (A fixed "/app" is wrong on the G40: the service runs from the /app_svc bind
    # mount while the image still carries a stale baked /app, so the children would
    # import months-old demod code and fail decodes the parent's code handles.)
    try:
        import os as _os
        import sys as _sys
        from dpmg.paths import REPO_DIR as _here
        if _here not in _sys.path:
            _sys.path.insert(0, _here)
        elif _sys.path and _sys.path[0] != _here:
            _sys.path.remove(_here)
            _sys.path.insert(0, _here)
    except Exception:
        pass
    try:
        import os as _os
        _os.nice(10)
    except Exception:
        pass

# Module-level worker entry so it's picklable for ProcessPoolExecutor.
# Returns (DecodeResult) with lock_out populated so the parent can persist tracking state.
def _proc_run_one(key, bb, spec: "DecodeSpec") -> "DecodeResult":
    t0 = time.perf_counter()
    try:
        res = _default_decode(key, bb, spec)
    except Exception as e:
        res = DecodeResult(key=key, ok=False, error=str(e))
    res.elapsed_ms = (time.perf_counter() - t0) * 1000.0
    # ship the (mutated in place) lock cache back to the parent
    try:
        res.lock_out = dict(spec.lock)
    except Exception:
        res.lock_out = None
    return res


class DecodePool:
    def __init__(self, max_workers: int = 5, decode_fn=None, backend: str = "process"):
        self.max_workers = int(max_workers)
        # Process backend by default: cold acquires run parallel on idle cores, no GIL
        # contention with the RX drain. Fall back to threads only if processes can't start
        # (e.g. spawn restrictions) so the app still runs.
        self._backend = backend
        # Leave a core free for the SCHED_FIFO RX drain thread + parent worker thread.
        # On a 4-core Jetson, capping decode at 3 processes means RX always has CPU.
        import os as _os
        _ncpu = _os.cpu_count() or 4
        n = min(max(1, _ncpu - 1), max(1, self.max_workers))
        self._ex = None
        if backend == "process":
            try:
                import multiprocessing as _mp
                # spawn (not fork): the parent has CUDA + posix_ipc + numpy thread state that
                # a fork inherits into a broken child (immediate death: pool rebuild loop).
                # spawn gives each child a clean interpreter. Costs a bit more at startup only.
                ctx = _mp.get_context("spawn")
                import sys as _sys, types as _types
                _real_main = _sys.modules.get("__main__")
                _fake_main = _types.ModuleType("__main__")
                _fake_main.__spec__ = None
                _sys.modules["__main__"] = _fake_main
                try:
                    self._ex = ProcessPoolExecutor(max_workers=n, mp_context=ctx,
                                                   initializer=_proc_init)
                    # Force all n workers to spawn now, while masked (executor
                    # spawns one worker per submit until max_workers reached).
                    _warm = [self._ex.submit(int, 1) for _ in range(n)]
                    for _f in _warm:
                        _f.result(timeout=60)
                    print(f"[decode_pool] {n} spawn workers up", flush=True)
                finally:
                    if _real_main is not None:
                        _sys.modules["__main__"] = _real_main
            except Exception as e:
                print(f"[decode_pool] process backend unavailable ({e}); using threads", flush=True)
                if self._ex is not None:
                    try:
                        self._ex.shutdown(wait=False, cancel_futures=True)
                    except Exception:
                        pass
                self._ex = None
        if self._ex is None:
            self._backend = "thread"
            self._ex = ThreadPoolExecutor(max_workers=min(2, self.max_workers))
        print(f"[decode_pool] backend={self._backend} workers={n}", flush=True)
        self._out: "queue.Queue[DecodeResult]" = queue.Queue()
        self._inflight = set()
        self._lock = threading.Lock()
        self._decode_fn = decode_fn or _default_decode
        # parent-side lock store: key: lock dict, so tracking state persists across frames
        # even though the decode ran in a child process.
        self._locks: dict = {}
        self._submitted = 0
        self._completed = 0
        self._dropped_busy = 0
        self._broken = False

    def submit(self, key, baseband, spec: DecodeSpec) -> bool:
        """Non-blocking. Coalesces duplicate keys and caps concurrency so a slow
        channel never backs up the scan."""
        with self._lock:
            if key in self._inflight:
                self._dropped_busy += 1
                return False
            if len(self._inflight) >= self.max_workers:
                self._dropped_busy += 1
                return False
            self._inflight.add(key)
            self._submitted += 1
            # seed spec.lock from the parent-side store so the child sees prior tracking state
            prev = self._locks.get(key)
            if prev:
                spec.lock = dict(prev)
        import numpy as np
        bb = np.asarray(baseband, dtype=np.complex64).copy()
        try:
            fut = self._ex.submit(_proc_run_one if self._backend == "process" else self._thread_run_one,
                                  key, bb, spec)
        except RuntimeError as e:
            print(f"[decode_pool] submit failed, marking broken: {type(e).__name__}: {e}", flush=True)
            # pool shut down/broken (e.g. a child died). Release the key and signal broken so
            # the worker's _ensure_pool rebuilds us. Don't throw: one broken moment shouldn't
            # spam tracebacks or permanently lose this channel.
            with self._lock:
                self._inflight.discard(key)
            self._broken = True
            return False
        fut.add_done_callback(self._on_done)
        return True

    def _thread_run_one(self, key, bb, spec: DecodeSpec) -> DecodeResult:
        # thread fallback path: same entry, lock is shared in-process (no round-trip needed
        # but we still populate lock_out so collect() is uniform).
        t0 = time.perf_counter()
        try:
            res = self._decode_fn(key, bb, spec)
        except Exception as e:
            res = DecodeResult(key=key, ok=False, error=str(e))
        res.elapsed_ms = (time.perf_counter() - t0) * 1000.0
        try:
            res.lock_out = dict(spec.lock)
        except Exception:
            res.lock_out = None
        return res

    def _on_done(self, fut):
        try:
            res = fut.result()
        except Exception as e:
            # we lost the key here; best effort: clear all is unsafe, so skip
            res = DecodeResult(key=None, ok=False, error=f"future: {e}")
        key = getattr(res, "key", None)
        with self._lock:
            if key is not None:
                self._inflight.discard(key)
                # merge the child's updated lock cache back so the next frame tracks.
                # Only on success: a failed decode's lock state is garbage, and merging
                # it back poisons the cache: next attempt on this key goes cold again.
                if res.ok and getattr(res, "lock_out", None) is not None:
                    self._locks[key] = res.lock_out
            self._completed += 1
        if not res.ok:
            print(f"[decode_pool] error key={key}: {str(getattr(res,'error',''))[:80]}", flush=True)
        self._out.put(res)

    def collect(self):
        """Non-blocking drain of finished results. Lock caches are already merged."""
        out = []
        while True:
            try:
                out.append(self._out.get_nowait())
            except queue.Empty:
                break
        return out

    def busy(self) -> int:
        with self._lock:
            return len(self._inflight)

    def stats(self) -> dict:
        with self._lock:
            return dict(submitted=self._submitted, completed=self._completed,
                        dropped_busy=self._dropped_busy, inflight=len(self._inflight),
                        max_workers=self.max_workers, backend=self._backend)

    def is_healthy(self) -> bool:
        """False if the executor has shut down or a child died: worker should rebuild."""
        if self._broken:
            return False
        _ex = self._ex
        return not (bool(getattr(_ex, "_broken", False)) or bool(getattr(_ex, "_shutdown", False))
                    or bool(getattr(_ex, "_shutdown_thread", False)))

    def shutdown(self):
        try:
            self._ex.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            self._ex.shutdown(wait=False)


# default decode fn (runs in the child process)

def _resample_to(x, fs_in: float, fs_out: float):
    """Rate-match a channel baseband to a demod's nominal rate. Polyphase when
    scipy is present (the spread demods run near Nyquist, where linear interp
    distorts); linear-interp fallback keeps the decode path dependency-free."""
    import numpy as np
    x = np.asarray(x, dtype=np.complex64)
    if len(x) < 2 or fs_in <= 0 or fs_out <= 0:
        return x
    try:
        from fractions import Fraction
        from scipy.signal import resample_poly
        fr = Fraction(float(fs_out) / float(fs_in)).limit_denominator(10000)
        return resample_poly(x, fr.numerator, fr.denominator).astype(np.complex64)
    except Exception:
        n_out = int(round(len(x) * fs_out / fs_in))
        src = np.arange(n_out) * (fs_in / fs_out)
        left = np.minimum(np.floor(src).astype(np.int64), len(x) - 2)
        frac = (src - left).astype(np.float32)
        return (x[left] * (1 - frac) + x[left + 1] * frac).astype(np.complex64)


def _transec_sim_spec(frame: dict):
    """TRANSEC_SIM (simulator only): a plan entry whose frame carries
    "transec_sim": {"simulator": true} is searched with the keyed 32-bit sync
    word of the current GPS slot instead of its fixed sync. Returns None (legacy
    spec) unless DPMG_TRANSEC_SIM=1, a secure context is loaded and the entry
    declares itself a simulator; a real radio can never take this path."""
    ts = (frame or {}).get("transec_sim")
    if not ts:
        return None
    try:
        from dpmg.secure import api as _sf
        from dpmg.secure import transec_sync as _tsy
        if not _tsy.sim_enabled():
            return None
        ctx = _sf.get_context(create=True, tx=False)
        if ctx is None:
            return None
        return _tsy.sync_spec_for(ctx, simulator=bool(isinstance(ts, dict) and ts.get("simulator")))
    except Exception:
        return None


def _default_decode(key, bb, spec: DecodeSpec) -> DecodeResult:
    import numpy as np
    from dpmg.framing.frame_codec import find_and_decode
    # Search the whole bit string, not the first max_search=4096 bits: the
    # decode window is [prev frame, cur frame], so a signal that starts
    # mid-window (TX keyed moments ago) packs all its frames into the tail of
    # the buffer: past 4096 bits at production sps (the 4FSK bench failure).
    _decode = lambda bits: find_and_decode(bits, max_search=len(bits))
    _real_sync = None
    _frame = (spec.demod_params or {}).get("frame")
    _fec = (spec.demod_params or {}).get("fec")
    _spec = None
    # A plan entry whose frame (or params) says "secure" carries the
    # sealed body (frame_codec.SECURE layout). Only honoured while the
    # DPMG_SECURE_MSG flag is on; otherwise the legacy parse runs (and such a
    # frame never validates).
    _secure = bool((_frame or {}).get("secure") or (spec.demod_params or {}).get("secure"))
    if _secure:
        try:
            from dpmg.secure import api as _sf
            _secure = _sf.secure_msg_enabled()
        except Exception:
            _secure = False
    if _frame:
        try:
            from dpmg.framing import frame_codec as _fc
            _tspec = _transec_sim_spec(_frame)
            _spec = _tspec or _fc.FrameSpec.from_entry(_frame)
            if _spec.sync_bits:
                # FEC channels: sync stays uncoded (so timing recovery still
                # correlates on it) but the body is a conv block: Viterbi-decode
                # it rather than parsing len/payload/crc straight off the bits.
                if _secure and _fec:
                    _decode = lambda bits, f=_fec, k=_spec.max_sync_errors: \
                        _fc.fec_find_and_decode(bits, f, max_sync_errors=k, layout=_fc.SECURE)
                    _real_sync = _fc.SYNC_BITS
                elif _secure:
                    _decode = lambda bits, k=_spec.max_sync_errors: \
                        _fc.secure_find_and_decode(bits, max_search=None, max_sync_errors=k)
                    _real_sync = _fc.SYNC_BITS
                elif _fec:
                    # The tolerance only bites on SoftBits (the
                    # noncoherent FSK fallback); hard bit strings keep exact sync.
                    _decode = lambda bits, f=_fec, k=_spec.max_sync_errors: \
                        _fc.fec_find_and_decode(bits, f, max_sync_errors=k)
                    _real_sync = _spec.sync_bits
                else:
                    # Hard bits with a fuzzy sync: the recovery paths try
                    # hundreds of hypotheses per window (OQPSK: 2 offsets x 10
                    # timings x 2 slopes x 16 phases), each a window of
                    # random-looking bits when it is wrong. Two sync errors
                    # anywhere in 16 bits plus CRC-16 then pass about once in
                    # twenty failed windows: the hex OQPSK line on the 9 November
                    # bench, and 3 in 58 noise-only windows offline. The
                    # tolerance is there for the first symbol, which a
                    # differential detector or a filter settling out of the dead
                    # air before the burst gets wrong, so keep it to those bits
                    # (34 times fewer chance hits). The rescan for every frame
                    # below uses the same spec. A TRANSEC keyed sync keeps its
                    # own policy.
                    if _tspec is None and _spec.max_sync_errors > 0 and not _spec.sync_err_head:
                        import dataclasses as _dc
                        _spec = _dc.replace(_spec, sync_err_head=SYNC_ERR_HEAD)
                    _decode = lambda bits, _s=_spec: _fc.find_frame(bits, _s,
                                                                    max_search=len(bits))
                    _real_sync = _spec.sync_bits
        except Exception:
            _decode = lambda bits: find_and_decode(bits, max_search=len(bits))
    elif _secure:
        from dpmg.framing import frame_codec as _fc
        if _fec:
            _decode = lambda bits, f=_fec: _fc.fec_find_and_decode(bits, f, layout=_fc.SECURE)
        else:
            _decode = lambda bits: _fc.secure_find_and_decode(bits, max_search=None)
        _real_sync = _fc.SYNC_BITS
    try:
        from dpmg.dsp.demod_helper import demod_with_recovery, prep_baseband, _centersample_bits, _est_snr_db, _CSRes
    except Exception as e:
        return DecodeResult(key=key, ok=False, error=f"import: {e}")

    m = (spec.modulation or "").upper()
    fsps = max(2.0, float(spec.sps))
    isps = max(2, int(round(fsps)))
    lock = spec.lock

    demod_obj = None
    if m not in ("BPSK", "QPSK", "PI4QPSK", "PI4-QPSK", "DQPSK", "OQPSK"):
        try:
            from dpmg.demodulation.demodulator import get_demodulator
            _p = spec.demod_params or {}
            _sr = float(_p.get("symbol_rate") or (spec.fs_out / max(1, isps)))
            _kw = {}
            _ts = _p.get("tone_spacing_hz")
            if _ts:
                s = float(_ts)
                if m == "4FSK":
                    _kw = dict(tone0=-1.5*s, tone1=-0.5*s, tone2=0.5*s, tone3=1.5*s)
                else:
                    _kw = dict(tone0=-0.5*s, tone1=0.5*s)
            if _p.get("dibit_mapping"):
                _kw["dibit_mapping"] = _p["dibit_mapping"]
            for _k in ("gfsk_bw_hz", "spread_factor", "pn_seed",
                       "css_sf", "css_bw", "css_os", "code_spec", "dsss_modem",
                       "code_spec_q",                       # second (Q-rail) code
                       "overlay_codes"):                    # Co-channel users' codes
                if _p.get(_k) is not None:
                    _kw[_k] = _p[_k]
            demod_obj = get_demodulator(m, float(spec.fs_out), _sr, fsps, **_kw)
        except Exception as e:
            demod_obj = None
            # Without a demod object 8PSK and the other table mods never
            # decode and nothing said why: say it once per modulation and
            # decode process, so the service log shows it.
            if m not in _DEMOD_FAIL_SEEN:
                _DEMOD_FAIL_SEEN.add(m)
                print(f"[decode_pool] no demodulator for {m}: {type(e).__name__}: {e}", flush=True)

    # The channelizer's integer-decimation fs_out never equals
    # the spread demods' nominal rate (DSSS despreads with an integer samples/chip
    # grid; CSS dechirps on an exact bw*os grid). Even 0.2% rate error slips chips
    # mid-frame, so resample the channel baseband to the demod's nominal rate and
    # decode there. (Real 83.478k channel vs 83.333k DSSS nominal = half-chip slip
    # every ~38 bits; CSS got a 128k buffer against a 250k dechirp grid.)
    fs_use = float(spec.fs_out)
    if demod_obj is not None:
        _nom = 0.0
        if m in ("CSS", "LORA", "LORA-CSS"):
            _nom = float(getattr(demod_obj, "sample_rate", 0) or 0)
        elif "DSSS" in m:
            _spc = int(getattr(demod_obj, "spc", 0) or 0)
            _sfv = int(getattr(demod_obj, "sf", 0) or 0)
            if _spc and _sfv and _sr > 0:
                _nom = float(_sfv * _spc) * _sr
        if _nom > 0 and abs(_nom - fs_use) > 1e-9:
            bb = _resample_to(np.asarray(bb), fs_use, _nom)
            fs_use = _nom
            if _sr > 0:
                fsps = max(2.0, _nom / _sr)
                isps = max(2, int(round(fsps)))

    import time as _t
    if lock.get("cooldown_until", 0.0) > _t.time():
        return DecodeResult(key=key, ok=True, valid=False, acquired=False)
    _frame_bits = None
    if _frame and _spec is not None and int(getattr(_spec, "payload_len_bits", 0) or 0) > 0:
        try:
            _frame_bits = len(_spec.sync_bits) + int(_spec.payload_len_bits)
        except Exception:
            _frame_bits = None

    bs, fr, nsyms, res = demod_with_recovery(bb, fs_use, fsps, m, demod_obj, _decode,
                                             sync_bits=_real_sync, frame_bits=_frame_bits,
                                             lock=lock)
    valid = bool(fr is not None and getattr(fr, "valid", False))
    # A single demodulated buffer can carry several concatenated frames (a burst
    # train). find_and_decode/find_frame return only the first; rescan the same
    # recovered bit string for every frame so a multi-message burst yields all of
    # them. messages[0] == fr.message; message stays the first for back-compat.
    messages = []
    sealed = []
    if valid:
        from dpmg.framing import frame_codec as _fc2
        try:
            _k = _spec.max_sync_errors if _spec is not None else 0
            if _secure and _fec:
                _allf = _fc2.fec_find_all_frames(bs or "", _fec, max_sync_errors=_k,
                                                 layout=_fc2.SECURE)
            elif _secure:
                _allf = _fc2.secure_find_all_frames(bs or "", max_sync_errors=_k)
            elif _fec:
                _allf = _fc2.fec_find_all_frames(bs or "", _fec, max_sync_errors=_k)
            elif _spec is not None and getattr(_spec, "sync_bits", ""):
                _allf = _fc2.find_all_frames_spec(bs or "", _spec)
            else:
                _allf = _fc2.find_all_frames(bs or "")
            messages = [f.message for f in _allf if f.valid and f.message]
            if _secure:
                sealed = [bytes(f.sealed) for f in _allf if f.valid and f.sealed]
        except Exception:
            messages = []
        if not messages and getattr(fr, "message", ""):
            messages = [fr.message]
            if _secure and getattr(fr, "sealed", None) and not sealed:
                sealed = [bytes(fr.sealed)]
    if valid:
        lock["locked"] = True
        lock.setdefault("cfo", 0.0)
        lock.setdefault("timing_off", 0.0)
        lock["misses"] = 0
        lock.pop("cooldown_until", None)
    else:
        lock["misses"] = int(lock.get("misses", 0)) + 1
        if lock["misses"] >= 3:
            lock["cooldown_until"] = _t.time() + 3.0
            lock["misses"] = 0
    return DecodeResult(key=key, ok=True, valid=valid,
                        message=(fr.message if valid else ""),
                        messages=messages,
                        bit_string=str(bs or ""), n_syms=int(nsyms or 0),
                        snr_db=float(getattr(res, "snr_db", 0.0) or 0.0),
                        acquired=True, sealed=sealed)
