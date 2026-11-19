"""
test_pq_rekey_stub.py: PQ rekey: design artefacts are consistent and
every cryptographic entry point refuses without the DoW PQC-testing gate flag
(and, with the gate open, reports the missing library instead of running).

Evidence names:
  test_pq_stubs_refuse_without_gate     DPMG_PQ_TEST_APPROVED unset: PqGateError, nothing runs
  test_pq_stubs_report_library_with_gate gate open: PqNotAvailable naming the prerequisite
  test_pq_message_sizes_and_fragment_plan design arithmetic (63: 79 fragments, 131 s)
  test_pq_state_machine                 the designed transitions, no others
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from dpmg.secure import pq_rekey as pq  # noqa: E402


def test_pq_stubs_refuse_without_gate(monkeypatch):
    monkeypatch.delenv(pq._ce.FLAG_PQ_TEST_APPROVED, raising=False)
    assert not pq.gate_open()
    for fn, args in ((pq.combine_master, (b"k", b"s", 1, 1, b"c")), (pq.encapsulate, (b"pk",)),
                     (pq.decapsulate, (b"sk", b"ct")), (pq.sign_rekey, (b"sk", b"m")),
                     (pq.verify_rekey, (b"pk", b"m", b"s")), (pq.fragment, (b"m", 10)),
                     (pq.reassemble, ([], 1, 2))):
        with pytest.raises(pq.PqGateError, match="approval"):
            fn(*args)
    assert len(pq.STUBS) == 7


def test_pq_stubs_report_library_with_gate(monkeypatch):
    monkeypatch.setenv(pq._ce.FLAG_PQ_TEST_APPROVED, "1")
    assert pq.gate_open()
    with pytest.raises(pq.PqNotAvailable, match="ML-KEM|liboqs|cryptography"):
        pq.encapsulate(b"pk")
    with pytest.raises(pq.PqNotAvailable):
        pq.combine_master(b"k", b"s", 1, 1, b"c")
    pre = pq.library_prerequisite()
    assert pre["cryptography"] and pre["openssl"]
    assert isinstance(pre["ready"], bool)
    # cryptography 41..47 (the dev host has 42 / OpenSSL 3.0) has no ML-KEM
    if pre["cryptography"].startswith(("41.", "42.", "43.", "44.", "45.", "46.", "47.")):
        assert not pre["ml_kem"] and not pre["ml_dsa"]


def test_pq_message_sizes_and_fragment_plan():
    assert pq.ML_KEM_1024_CT == 1568 and pq.ML_DSA_87_SIG == 4627          # FIPS 203 / 204
    size = pq.RekeyMessage.size(n_wrapped=1)
    assert 6200 <= size <= 6300                                                # design: 6,259 B class
    plan = pq.fragment_plan(6259, fer=0.01)
    assert plan["k"] == 63 and plan["p_success"] >= 0.99 and plan["n"] < 79
    assert plan["p_success_uncoded"] < 0.6                                     # bare 63 fragments: ~0.53
    # the design sized n = 79 for 10 % FER (P ~ 0.998), so ~131 s at 1.2 kbit/s
    hard = pq.fragment_plan(6259, fer=0.10, target_success=0.998)
    assert 76 <= hard["n"] <= 82 and hard["n"] > plan["n"]
    assert 120 <= hard["airtime_s_1200"] <= 140
    assert pq.fragment_plan(1632, fer=0.01)["k"] == 17                         # ct-only message


def test_pq_state_machine():
    m = pq.RekeyStateMachine()
    for ev, st in (("fragment", "COLLECTING"), ("fragment", "COLLECTING"), ("complete", "VERIFYING"),
                   ("sig_fail", "IDLE"), ("fragment", "COLLECTING"), ("complete", "VERIFYING"),
                   ("sig_ok", "STAGED"), ("valid_from", "ACTIVE"), ("valid_to", "RETIRED"),
                   ("grace_over", "IDLE")):
        assert m.event(ev) == st
    with pytest.raises(ValueError):
        m.event("sig_ok")                                                      # IDLE has no such edge
    assert set(s for s, _e in pq.TRANSITIONS) <= set(pq.STATES)


if __name__ == "__main__":  # OFF-01 loop runs `python3 tests/<file>.py`; delegate to pytest
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
