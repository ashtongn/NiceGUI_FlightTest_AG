"""PSD display: batched compute_psd matches the old per-segment loop, the plot
y-axis holds a 40 dB minimum span, and the B200 gets the bigger PSD buffer."""
import os
import sys

import numpy as np
import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from dpmg.dsp import psd_trace as pt                 # noqa: E402


def _loop_psd(iq, fs, cf=0.0, nfft=4096, window="hann", overlap=0.5):
    # the old per-segment compute_psd, one segment per loop, complex128
    x = np.asarray(iq, dtype=np.complex128)
    nfft = int(min(nfft, x.size))
    if nfft < 8:
        nfft = x.size
    w = {"hann": np.hanning, "hamming": np.hamming, "blackman": np.blackman,
         "rectangular": np.ones}[window](nfft)
    step = max(1, int(nfft * (1.0 - overlap)))
    acc = np.zeros(nfft)
    count = 0
    for s in range(0, x.size - nfft + 1, step):
        acc += np.abs(np.fft.fftshift(np.fft.fft(x[s:s + nfft] * w))) ** 2
        count += 1
    psd = acc / count / (fs * np.sum(w ** 2))
    f = np.fft.fftshift(np.fft.fftfreq(nfft, d=1.0 / fs)) + cf
    return f, 10 * np.log10(psd + 1e-20)


@pytest.mark.parametrize("n,nfft,window", [
    (65536, 4096, "hann"), (262144, 4096, "hann"), (70001, 4096, "blackman"),
    (5000, 4096, "hamming"), (3000, 4096, "hann"), (6, 4096, "rectangular"),
    (65536, 1024, "rectangular"), (100000, 1000, "hann"),
])
def test_batched_psd_matches_loop(n, nfft, window):
    rng = np.random.default_rng(n)
    x = (0.01 * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
         + 0.1 * np.exp(2j * np.pi * 0.1 * np.arange(n))).astype(np.complex64)
    f0, p0 = _loop_psd(x, 30.72e6, 425e6, nfft, window)
    f1, p1 = pt.compute_psd(x, 30.72e6, 425e6, nfft=nfft, window=window)
    assert np.array_equal(f0, f1)
    assert np.max(np.abs(p0 - p1)) < 1e-3


def test_batched_psd_empty():
    f, p = pt.compute_psd(np.zeros(0, np.complex64), 1e6)
    assert f.size == 0 and p.size == 0


try:
    import nicegui  # noqa: F401
    from dpmg.web import link_tab as lt
except ImportError:                    # service images have no nicegui
    lt = None
needs_gui = pytest.mark.skipif(lt is None, reason="nicegui not installed")


@needs_gui
def test_flat_trace_gets_40_db_window():
    lo, hi = lt.psd_y_range([-130.0, -128.0, -122.0])
    assert hi - lo == 40 and lo <= -130 and hi >= -122
    assert lo % 5 == 0
    # centred: the trace sits in the middle of the window, not at an edge
    assert lo < -140 and hi > -112


@needs_gui
def test_wide_trace_auto_scales():
    assert lt.psd_y_range([-140.0, -95.0]) == (None, None)


@needs_gui
def test_window_kept_while_trace_fits():
    first = lt.psd_y_range([-130.0, -120.0])
    assert lt.psd_y_range([-131.0, -119.0], prev=first) == first
    moved = lt.psd_y_range([-100.0, -95.0], prev=first)
    assert moved != first and moved[0] <= -100 and moved[1] >= -95


@needs_gui
def test_top_never_cuts_the_data():
    for top in np.arange(-100.0, -90.0, 0.5):
        lo, hi = lt.psd_y_range([top - 39.5, top])
        assert lo <= top - 39.5 and hi >= top and 40 <= hi - lo <= 45


@needs_gui
def test_bad_or_empty_values():
    assert lt.psd_y_range([]) == (None, None)
    assert lt.psd_y_range([np.nan, np.inf]) == (None, None)
    assert lt.psd_y_range(None) == (None, None)


@needs_gui
def test_set_psd_y_writes_axis():
    class C:
        options = {"yAxis": {}}
    c = C()
    lt._set_psd_y(c, [-130.0, -125.0])
    assert c.options["yAxis"]["max"] - c.options["yAxis"]["min"] == 40
    lt._set_psd_y(c, [-128.0, -126.0])          # fits: axis does not move
    assert c.options["yAxis"]["max"] - c.options["yAxis"]["min"] == 40
    lt._set_psd_y(c, [-140.0, -60.0])
    assert c.options["yAxis"]["min"] is None and c.options["yAxis"]["max"] is None


@needs_gui
def test_psd_buffer_for():
    assert lt.psd_buffer_for("b200") == 262144
    for k in ("g40", "x40", "hackrf", "limesdr", None):
        assert lt.psd_buffer_for(k) == 65536


@needs_gui
def test_y_zoom_buttons_scale_window():
    class C:
        options = {"yAxis": {}}
    c = C()
    lt._set_psd_y(c, [-130.0, -125.0])                      # auto: 40 dB window
    assert c.options["yAxis"]["max"] - c.options["yAxis"]["min"] == 40
    c._dpmg_yzoom = 0.8                                     # zoom in
    lt._set_psd_y(c, [-130.0, -125.0])
    assert abs((c.options["yAxis"]["max"] - c.options["yAxis"]["min"]) - 32.0) < 1e-6
    c._dpmg_yzoom = 1.25                                    # zoom out
    lt._set_psd_y(c, [-130.0, -125.0])
    assert abs((c.options["yAxis"]["max"] - c.options["yAxis"]["min"]) - 50.0) < 1e-6
    c._dpmg_yzoom = 1.0                                     # reset to auto
    lt._set_psd_y(c, [-130.0, -125.0])
    assert c.options["yAxis"]["max"] - c.options["yAxis"]["min"] == 40


@needs_gui
def test_y_zoom_on_wide_trace_derives_window():
    # a wide trace auto-scales to (None, None); the buttons still get a window
    lo, hi = lt._zoom_y_range((None, None), [-140.0, -60.0], 0.8)
    assert lo is not None and hi is not None and hi > lo


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
