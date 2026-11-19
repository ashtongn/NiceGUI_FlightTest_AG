"""
secure_testkit.py: shared fixtures for the secure-messaging tests.

Builds a SecureContext from fixed test masters (never the operator's key file),
a FixedTime clock and a temp nonce-state file, with DPMG_SECURE_MSG forced on
for the test process. Importable from tests/ and bench/ (replay tests).
"""
from __future__ import annotations

import datetime as _dt
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("DPMG_SECURE_MSG", "1")

from dpmg.secure import api as sf  # noqa: E402

# fixed, obviously-test masters (the KeyFile check rejects equal masters)
COMSEC = bytes(range(32)).hex()
TRANSEC = bytes(range(32, 64)).hex()
T0 = _dt.datetime(2026, 10, 19, 12, 0, 0, tzinfo=_dt.timezone.utc).timestamp()   # test "GPS time"


def key_doc(net_id=1, sender_id=7, kid=0, suite="gcmsiv128", day=None, days=30, slot_s=1.0):
    day = day or _dt.datetime.fromtimestamp(T0, _dt.timezone.utc).date()
    return {"version": 1, "net_id": net_id, "sender_id": sender_id, "active_kid": kid,
            "key_sets": [{"kid": kid, "label": "test", "suite": suite,
                          "comsec_master": COMSEC, "transec_master": TRANSEC,
                          "valid_from": (day - _dt.timedelta(days=1)).isoformat(),
                          "valid_to": (day + _dt.timedelta(days=days)).isoformat(),
                          "slot_s": slot_s}]}


def make_store(**kw) -> sf.KeyStore:
    st = sf.parse_key_file(key_doc(**kw), path="<test>")
    st.sha256 = "test" + kw.get("suite", "gcmsiv128")
    return st


def make_ctx(sender_id=7, suite="gcmsiv128", t=T0, tx=True, state_dir=None, policy=None,
             **kw) -> sf.SecureContext:
    """A context with its own nonce-state file in a temp dir (kept alive by the
    context: ctx._tmpdir). The nonce is the persisted counter + time-slot
    construction (the only one)."""
    store = make_store(sender_id=sender_id, suite=suite, **kw)
    d = state_dir or tempfile.mkdtemp(prefix="dpmg-sec-")
    clock = sf.FixedTime(t, "trusted")
    if policy is None:
        policy = sf.Policy()
    ctx = sf.SecureContext(store, time_source=clock, policy=policy,
                           state_path=os.path.join(d, f"nonce-{sender_id}.json"), tx=tx)
    ctx._tmpdir = d
    ctx.clock = clock
    return ctx


def pair(suite="gcmsiv128", t=T0, **kw):
    """(tx_ctx, rx_ctx) sharing masters and the same fixed clock value."""
    tx = make_ctx(sender_id=7, suite=suite, t=t, **kw)
    rx = make_ctx(sender_id=9, suite=suite, t=t, tx=False, **kw)
    rx.clock = rx.time
    return tx, rx
