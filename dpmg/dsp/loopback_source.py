"""
loopback_source.py: synthetic RX frame source for the g40 channelizer worker.

The receive pipeline has one entry point: worker.feed(frame). The live SDR
scheduler calls it; this module calls it too, from a background thread, streaming
a composed IQ buffer as frame_len-sized frames. That makes loopback a drop-in
replacement for the RX SDR: same worker, same gated panel, same channelizer-tab
display. When you cable the two radios, you stop this and start live RX: the view
downstream is identical.

    generator emitter table --compose--> LoopbackSource.set_buffer()
                                          LoopbackSource.start(worker)
                                              -> worker.feed(_Frame) [loop]
                                              -> gate / channelize / demod / decode
                                              -> Channelizer tab shows the decodes

Usage:
    from loopback_source import get_source
    src = get_source()
    src.set_buffer(iq_complex64, fs=30.72e6, center=425.025e6)
    src.start(worker)      # begins streaming; loops the buffer to keep channels alive
    ...
    src.stop()
"""

from __future__ import annotations

import threading
import time

import numpy as np


class _Frame:
    __slots__ = ("samples", "sample_rate", "center_freq", "seq")

    def __init__(self, samples, sample_rate, center_freq, seq=0):
        self.samples = samples
        self.sample_rate = sample_rate
        self.center_freq = center_freq
        self.seq = seq


class LoopbackSource:
    def __init__(self):
        self._iq = None
        self._fs = 30_720_000.0
        self._center = 425_025_000.0
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.loop = True
        self.info = "no buffer"

    # buffer (live-updatable while streaming) 
    def set_buffer(self, iq, fs, center):
        with self._lock:
            self._iq = np.ascontiguousarray(np.asarray(iq, dtype=np.complex64))
            self._fs = float(fs)
            self._center = float(center)
            self.info = (f"{len(self._iq):,} samp @ {self._fs/1e6:g} MHz, "
                         f"center {self._center/1e6:.4f} MHz")

    def has_buffer(self):
        with self._lock:
            return self._iq is not None and len(self._iq) > 0

    # lifecycle
    def start(self, worker, pace=True):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(worker, pace),
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        self._thread = None

    def running(self):
        return bool(self._thread and self._thread.is_alive())

    # the injector loop 
    def _run(self, worker, pace):
        flen = int(getattr(worker, "frame_len", 262144))
        q = getattr(worker, "_q", None)
        wth = getattr(worker, "_thread", None)
        seq = 0
        while not self._stop.is_set():
            # stop if the worker was stopped out from under us
            if wth is not None and not wth.is_alive():
                break
            with self._lock:
                iq = self._iq
                fs = self._fs
                center = self._center
            if iq is None or len(iq) < flen:
                time.sleep(0.05)
                continue
            n = (len(iq) // flen) * flen
            for i in range(0, n, flen):
                if self._stop.is_set():
                    break
                if wth is not None and not wth.is_alive():
                    self._stop.set()
                    break
                chunk = np.ascontiguousarray(iq[i:i + flen], dtype=np.complex64)
                # backpressure so the worker's bounded queue never drops
                if q is not None:
                    while len(q) > 8 and not self._stop.is_set():
                        time.sleep(0.005)
                worker.feed(_Frame(chunk, fs, center, seq))
                seq += 1
                if pace:
                    time.sleep(flen / fs)      # ~real-time cadence, like the live SDR
            if not self.loop:
                break


_SOURCE = LoopbackSource()


def get_source() -> LoopbackSource:
    return _SOURCE
