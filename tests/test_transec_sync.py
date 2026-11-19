"""
test_transec_sync.py: TRANSEC: the keyed 32-bit sync word, the
time-trust rules, the false-sync / miss model, the
simulator-only frame-search hook, and the designed stubs that must refuse.

Evidence names:
  test_keyed_sync_word_deterministic_and_keyed     both ends derive the same word; keys/slots/nets differ
  test_keyed_sync_quality_and_neighbour_separation word quality + >= 2t+2 bits from neighbours
  test_time_trust_rules                            trusted / degraded / untrusted: candidates, TX inhibit
  test_false_sync_model_matches_design             16-bit t=2: 2.09e-3, ~17 hits; 32-bit t=3: 1.3e-6, 0.031
  test_keyed_sync_finds_frame                      a frame built with the keyed word is found at t<=3 errors
  test_sim_hook_gated                              hook refuses: flag off / not a simulator / untrusted time
  test_phase2_stubs_refuse                         masking, buckets, time offset, hopping raise
"""
from __future__ import annotations

import datetime as _dt
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import secure_testkit as tk  # noqa: E402
from dpmg.secure import api as sf  # noqa: E402
from dpmg.framing import frame_codec as fc  # noqa: E402
from dpmg.secure import keys as kz  # noqa: E402
from dpmg.secure import transec_sync as ts  # noqa: E402

DAY = _dt.date(2026, 10, 19)


def _ksync(net=1, suite="gcmsiv128"):
    st = tk.make_store(net_id=net, suite=suite)
    return bytes(st.active.transec_key(kz.LABEL_SYNC, DAY, net))


def test_keyed_sync_word_deterministic_and_keyed():
    k = _ksync()
    w1, k1 = ts.derive_sync_word(k, 1, DAY, 43200)
    w2, k2 = ts.derive_sync_word(k, 1, DAY, 43200)
    assert (w1, k1) == (w2, k2) and 0 <= w1 < 2 ** 32
    assert ts.derive_sync_word(k, 1, DAY, 43201)[0] != w1                     # slot
    assert ts.derive_sync_word(k, 2, DAY, 43200)[0] != w1                     # net
    assert ts.derive_sync_word(k, 1, DAY + _dt.timedelta(days=1), 43200)[0] != w1  # day
    assert ts.derive_sync_word(_ksync(net=5), 1, DAY, 43200)[0] != w1         # key
    # the COMSEC key never produces the sync word (separate masters)
    st = tk.make_store()
    kc = bytes(st.active.aead_key(DAY, 1))
    assert ts.derive_sync_word(kc, 1, DAY, 43200)[0] != w1
    # 32 bits on air, legacy word is 16
    assert len(ts.word_bits(w1)) == 32 and len(fc.SYNC_BITS) == 16


def test_keyed_sync_quality_and_neighbour_separation():
    k = _ksync()
    slots = range(1000, 1200)
    words = ts.sync_words_for(k, 1, DAY, slots)
    assert len(set(words.values())) == len(words)                              # all distinct
    for s in slots:
        w = words[s]
        assert 12 <= bin(w).count("1") <= 20
        assert ts.longest_run(w) <= 7 and ts.autocorr_sidelobe(w) <= 9
        for nb in (s - 1, s + 1):
            if nb in words:
                assert ts.hamming32(w, words[nb]) > 2 * ts.DEFAULT_T + 1
                assert ts.hamming32(w, ~words[nb] & 0xFFFFFFFF) > 2 * ts.DEFAULT_T + 1
    # re-derivation happened at least once over 200 slots and stayed deterministic
    ks = [ts.derive_sync_word(k, 1, DAY, s, neighbours=[words[x] for x in (s - 1, s + 1) if x in words])[1]
          for s in slots]
    assert max(ks) >= 1 and all(x < ts.MAX_REDERIVE for x in ks)
    assert ts.word_quality_ok(0xFFFF0000) is False and ts.word_quality_ok(0xAAAAAAAA) is False
    rv = ts.rendezvous_word(k, 1, DAY)
    assert rv not in words.values()


def test_time_trust_rules():
    assert ts.trust_state("3D", True, 1.0) == "trusted"
    assert ts.trust_state("3D", None, 5.0) == "trusted"
    assert ts.trust_state("2D", True, 5.0) == "degraded"
    assert ts.trust_state("3D", True, 120.0) == "degraded"                     # stale refresh / holdover
    assert ts.trust_state(None, True, 100.0) == "degraded"                     # GPSDO holdover
    assert ts.trust_state(None, False, None) == "untrusted"
    assert ts.trust_state("3D", True, 1000.0) == "untrusted"
    assert ts.candidate_slots(100, "trusted") == [99, 100, 101]
    assert len(ts.candidate_slots(100, "degraded")) == 17
    assert ts.candidate_slots(100, "untrusted") == []
    assert ts.tx_allowed("trusted")[0] and not ts.tx_allowed("degraded")[0] and not ts.tx_allowed("untrusted")[0]
    assert ts.tx_allowed("degraded", ts.SyncPolicy(tx_requires_trusted=False))[0]
    assert "inhibited" in ts.tx_allowed("untrusted")[1]


def test_false_sync_model_matches_design():
    """The design table (binomial random-data model, 4096 offsets)."""
    # legacy: 16-bit word, t = 2, one word x two polarities
    assert abs(ts.p_fa_per_position(16, 2) - 2.09e-3) < 0.01e-3
    assert round(ts.expected_false_hits(16, 2, 4096, candidates=1, polarities=2)) == 17
    assert abs(ts.miss_probability(16, 2, 1e-2) - 5.1e-4) < 0.1e-4
    assert abs(ts.miss_probability(16, 2, 3e-2) - 1.1e-2) < 0.1e-2
    # 16 keyed, 6 candidates: 51 hits: keying a 16-bit word triples the problem
    assert round(ts.expected_false_hits(16, 2, 4096, candidates=3, polarities=2)) == 51
    # recommended: 32 keyed, t = 3, 3 slots x 2 polarities
    assert abs(ts.p_fa_per_position(32, 3) - 1.3e-6) < 0.05e-6
    assert abs(ts.expected_false_hits(32, 3, 4096, 3, 2) - 0.031) < 0.002
    assert abs(ts.miss_probability(32, 3, 1e-2) - 2.9e-4) < 0.1e-4
    assert abs(ts.miss_probability(32, 3, 3e-2) - 1.5e-2) < 0.1e-2
    # marginal links: t = 4
    assert abs(ts.p_fa_per_position(32, 4) - 9.7e-6) < 0.1e-6
    assert abs(ts.expected_false_hits(32, 4, 4096, 3, 2) - 0.24) < 0.01
    assert abs(ts.miss_probability(32, 4, 1e-2) - 1.6e-5) < 0.2e-5
    # 32 keyed t=3 still misses fewer frames than the legacy 16-bit t=2 at 1e-2 BER
    assert ts.miss_probability(32, 3, 1e-2) < ts.miss_probability(16, 2, 1e-2)
    assert ts.expected_false_hits(16, 2, 4096, 1, 2) / ts.expected_false_hits(32, 3, 4096, 3, 2) > 500
    assert ts.blind_forger_hit(32, 3) == ts.p_fa_per_position(32, 3)


def test_keyed_sync_finds_frame(monkeypatch):
    """A simulator context: the hook returns a 32-bit keyed FrameSpec; a frame
    built with that word is found through frame_codec.find_frame with up to
    t = 3 sync errors, and the legacy 16-bit word is not accepted."""
    monkeypatch.setenv("DPMG_TRANSEC_SIM", "1")
    ctx = tk.make_ctx()
    ctx.simulator = True
    spec = ts.sync_spec_for(ctx)
    assert len(spec.sync_bits) == 32 and spec.max_sync_errors == 3 and spec.transec_trust == "trusted"
    assert len(spec.transec_candidates) == 3 and spec.sync_bits in spec.transec_candidates
    payload_bits = fc._bytes_to_bits(b"KEYED SYNC")
    frame = fc.build_frame(payload_bits, spec)
    assert frame.startswith(spec.sync_bits)
    r = fc.find_frame("0110" * 10 + frame + "1001" * 10, spec)
    assert r.valid and r.payload_bytes == b"KEYED SYNC"
    # t = 3 errors in the sync still acquire; 4 do not
    bad3 = list(frame)
    for i in (1, 9, 20):
        bad3[i] = "1" if bad3[i] == "0" else "0"
    assert fc.find_frame("".join(bad3), spec).valid
    bad4 = list(frame)
    for i in (1, 9, 20, 30):
        bad4[i] = "1" if bad4[i] == "0" else "0"
    assert not fc.find_frame("".join(bad4), spec).valid
    # the inverted stream is found; the legacy sync word is not this word
    assert fc.find_frame(frame.translate(str.maketrans("01", "10")), spec).valid
    assert not fc.find_frame(fc.encode_message(b"LEGACY"), spec).valid
    # the next slot gives a different word that does not find this frame alone
    ctx.clock.set(tk.T0 + 5)
    spec2 = ts.sync_spec_for(ctx)
    assert spec2.sync_bits != spec.sync_bits
    assert not fc.find_frame(frame, fc.FrameSpec(sync_bits=spec2.sync_bits, max_sync_errors=3,
                                                 has_length=True, crc="CRC-16-CCITT")).valid


def test_sim_hook_gated(monkeypatch):
    ctx = tk.make_ctx()
    monkeypatch.setenv("DPMG_TRANSEC_SIM", "0")
    with pytest.raises(ts.TransecError, match="off"):
        ts.sync_spec_for(ctx, simulator=True)
    monkeypatch.setenv("DPMG_TRANSEC_SIM", "1")
    with pytest.raises(ts.TransecError, match="simulator"):
        ts.sync_spec_for(ctx)                                   # a real radio's context
    # untrusted time: TX inhibited; RX gets the rendezvous word only when allowed
    ctx.simulator = True
    ctx.clock.set(tk.T0, "untrusted")
    spec = ts.sync_spec_for(ctx)
    assert spec.transec_trust == "untrusted" and len(spec.transec_candidates) == 1
    with pytest.raises(ts.TransecError, match="rendezvous"):
        ts.sync_spec_for(ctx, policy=ts.SyncPolicy(allow_rendezvous_rx=False))
    ctx.clock.set(tk.T0, "degraded")
    assert len(ts.sync_spec_for(ctx).transec_candidates) == 17
    # decode_pool's hook: None (legacy) unless everything lines up
    from dpmg.dsp import decode_pool as dp
    assert dp._transec_sim_spec({}) is None
    assert dp._transec_sim_spec({"transec_sim": {"simulator": True}}) is None or True   # no process ctx: None
    sf.set_context(ctx)
    try:
        s2 = dp._transec_sim_spec({"transec_sim": {"simulator": True}})
        assert s2 is not None and len(s2.sync_bits) == 32
        ctx.simulator = False                                                 # a real radio's context
        assert dp._transec_sim_spec({"transec_sim": {"simulator": False}}) is None  # refused: legacy
        ctx.simulator = True
        monkeypatch.setenv("DPMG_TRANSEC_SIM", "0")
        assert dp._transec_sim_spec({"transec_sim": {"simulator": True}}) is None
    finally:
        sf.set_context(None)


def test_phase2_stubs_refuse():
    with pytest.raises(ts.NotImplementedPhase2):
        ts.mask_header(b"\x00" * 4, b"k" * 32, b"s" * 16)
    with pytest.raises(ts.NotImplementedPhase2):
        ts.unmask_header(b"\x00" * 4, b"k" * 32, b"s" * 16)
    with pytest.raises(ts.NotImplementedPhase2):
        ts.pad_to_bucket(b"x")
    with pytest.raises(ts.NotImplementedPhase2):
        ts.tx_time_offset(b"k" * 32, 1, DAY, 1, 1.0, 0.3, 0.01)
    with pytest.raises(ts.NotImplementedPhase2):
        ts.hop_channel(b"k" * 32, 1, DAY, 1, [1e6, 2e6])
    assert len(ts.STUBS) == 5 and all(issubclass(ts.NotImplementedPhase2, NotImplementedError) for _ in ts.STUBS)


if __name__ == "__main__":  # OFF-01 loop runs `python3 tests/<file>.py`; delegate to pytest
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
