"""A BLUE recording is recognised by its first bytes, whatever the file is called."""
import os
import shutil
import sys
import tempfile

import numpy as np

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dpmg.dsp import cdif_reader as cr  # noqa: E402
from dpmg.dsp import iq_resample as ir  # noqa: E402

BLUE = os.path.join(REPO, "flight_test", "files", "fsk_pair_2m4.cdif")     # BLUE header, 2.4 MS/s
RAW = os.path.join(REPO, "flight_test", "files", "fsk_pair_2m4.iq.bin")    # the same samples, no header


def _copy(src, name):
    d = tempfile.mkdtemp(prefix="blue_sniff_")
    dst = os.path.join(d, name)
    shutil.copyfile(src, dst)
    return dst


def test_magic():
    assert cr.is_blue(BLUE) and not cr.is_blue(RAW)
    assert not cr.is_blue(os.path.join(REPO, "no_such_file.bin"))
    assert cr.is_blue_bytes(b"BLUEEEEIEEEI") and cr.is_blue_bytes(b"BLUEIEEEIEEE" + b"\\0" * 20)
    assert not cr.is_blue_bytes(b"BLUE") and not cr.is_blue_bytes(b"BLUExxxxEEEI") and not cr.is_blue_bytes(b"")


def test_blue_file_named_bin_reads_as_blue():
    ref, meta = cr.read_cdif(BLUE)
    for name in ("capture.bin", "capture.iq.bin", "capture.c64"):
        p = _copy(BLUE, name)
        assert abs(ir.native_rate(p) - 2.4e6) < 1.0, name
        iq = ir.load_iq(p)
        assert len(iq) == len(ref) and np.array_equal(iq, np.asarray(ref, dtype=np.complex64)), name
        shutil.rmtree(os.path.dirname(p))


def test_raw_file_is_unchanged():
    assert ir.native_rate(RAW) is None
    raw = ir.load_iq(RAW)
    assert len(raw) == os.path.getsize(RAW) // 8
    assert np.array_equal(raw, np.fromfile(RAW, dtype=np.complex64))


def test_a_hand_set_offset_or_format_is_kept():
    p = _copy(BLUE, "capture.bin")
    n_all = os.path.getsize(p) // 8
    assert len(ir.load_iq(p, offset_bytes=512)) == (os.path.getsize(p) - 512) // 8      # the caller skips the header
    forced = next(f for f in ir.RAW_FORMATS if ir.RAW_FORMATS[f][1] == "C")
    assert len(ir.load_iq(p, fmt=forced)) == n_all                                       # a forced raw format is obeyed
    shutil.rmtree(os.path.dirname(p))


def test_name_for_sending():
    head = open(BLUE, "rb").read(12)
    assert cr.blue_name("single_freq.bin", head) == "single_freq.cdif"
    assert cr.blue_name("single_freq.iq.bin", head) == "single_freq.cdif"
    assert cr.blue_name("capture.c64", head) == "capture.cdif"
    assert cr.blue_name("capture", head) == "capture.cdif"
    assert cr.blue_name("capture.CDIF", head) == "capture.CDIF" and cr.blue_name("a.blue", head) == "a.blue"
    raw = open(RAW, "rb").read(12)
    assert cr.blue_name("fsk.iq.bin", raw) == "fsk.iq.bin" and cr.blue_name("plan.json", b'{"name": "x"}') == "plan.json"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
