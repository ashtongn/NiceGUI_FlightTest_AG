"""
test_bitpipe_loopback.py: two containers back to back over their sockets, no
radio (plan rows BP-08 and BP-09). FEC and encryption on, bit flips and
dropped segments in the channel; text both ways, then a file both ways.

  test_loopback_secure_impaired     the acceptance run over TCP (about 10 s)
  test_loopback_clear_clean_unix    a clear link, clean channel, Unix socket transport
  test_file_record                  the file record parser alone

The relay plays both modems: it pulls one frame per period at its clock and
the containers keep no clock (the modem is the only clock in the chain).

python3 tests/test_bitpipe_loopback.py runs them through pytest. Ports
47940..47959 on localhost.
"""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DPMG_SECURE_MSG", "1")
from dpmg.bitpipe import streams as st  # noqa: E402
from dpmg.bitpipe.loopback import default_configs, run_loopback  # noqa: E402

BASE = 47940


def test_loopback_secure_impaired():
    cfg_a, cfg_b = default_configs(BASE, payload_bytes=128)
    rep = run_loopback(cfg_a, cfg_b, secure=True, ber=0.005, drop=0.05, lines=20, file_kb=6,
                       timeout=90.0, seed=3, verbose=False, bit_rate=48000.0)
    assert rep["text_ok"] and rep["file_ok"], rep.get("error")
    for s in (rep["status_a"], rep["status_b"]):
        assert s["open_failed"] == 0 and s["bind_mismatch"] == 0 and not s["link_down"]
        assert s["crypto"]["crypto"] == "secure"
    # losses hit both directions (a lost idle frame costs no retransmit, so count the pair)
    assert rep["status_a"]["tx"]["retx"] + rep["status_b"]["tx"]["retx"] > 0
    lost = lambda c: rep[c]["dropped"] + rep[c]["cut"]
    assert rep["channel_ab"]["flipped"] > 0 and lost("channel_ab") + lost("channel_ba") > 0
    assert rep["status_a"]["bit_face"]["pulls"] > 0 and rep["relay"]["hellos"]["a>b"]["pull"] is True


def test_loopback_clear_clean_unix():
    cfg_a, cfg_b = default_configs(BASE + 20, transport="unix", payload_bytes=64,
                                   unix_dir=os.path.join(tempfile.mkdtemp(prefix="bp-"), "s"))
    rep = run_loopback(cfg_a, cfg_b, secure=False, lines=10, file_kb=4, timeout=60.0, seed=4, verbose=False,
                       bit_rate=24000.0)
    assert rep["ok"], rep.get("error")
    assert rep["status_a"]["transport"] == "unix" and rep["status_a"]["bit_face"]["endpoint"].startswith("unix:")
    assert rep["status_a"]["tx"]["retx"] == 0 and rep["status_b"]["deframer"]["frames_bad"] == 0


def test_file_record():
    data = os.urandom(5000)
    rec = st.file_record("x.bin", data)
    r = st.FileReceiver()
    assert r.feed(rec[:100]) == []
    assert r.progress() == ("x.bin", 100 - 45 - 5, 5000)
    out = r.feed(rec[100:] + b"junk" + st.file_record("y", b"z"))
    assert [f.name for f in out] == ["x.bin", "y"] and all(f.ok for f in out)
    assert out[0].data == data and out[0].sha256 == hashlib.sha256(data).hexdigest()
    assert r.errors == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"] + sys.argv[1:]))
