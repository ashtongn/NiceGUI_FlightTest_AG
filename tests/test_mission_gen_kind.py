"""The runner fills in the generator kind from the technique when a hand-built
plan leaves it out.  Without this the service defaults to a chirp, so a tone
step radiates a chirp; this showed up on the bench with the demo plan."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dpmg.mission import mission_runner as mr  # noqa: E402
from dpmg.mission.mission_runner import _gen_params  # noqa: E402


def test_transponder_timeout_covers_its_dwell():
    # the service acks the transponder only after its RX dwell, so the wait
    # has to outlast the dwell; everything else keeps the generic timeout
    assert mr._cmd_timeout_s("transponder", {"seconds": 30.0}) >= 40.0
    assert mr._cmd_timeout_s("transponder", {"seconds": 1.0}) == mr.CMD_TIMEOUT_S
    assert mr._cmd_timeout_s("transponder", {}) == max(mr.CMD_TIMEOUT_S, 15.0)
    assert mr._cmd_timeout_s("set_center", {"hz": 1.0}) == mr.CMD_TIMEOUT_S
    assert mr._cmd_timeout_s("gen_scenario", {}) == mr.GEN_SCENARIO_TIMEOUT_S


def test_kind_from_technique_when_absent():
    cases = (("tools.tone", "tone"), ("tools.noise", "noise"), ("tools.chirp", "chirp"),
             ("tools.two_tone", "two_tone"), ("jammer.barrage", "barrage"),
             ("jammer.noise", "noise"), ("jammer.tone", "tone"), ("jammer.comb", "comb"),
             ("jammer.swept", "swept"), ("jammer.chirp", "chirp"),
             ("jammer.modulated", "modulated"))
    for tech, kind in cases:
        p = _gen_params({"id": "s1", "technique": tech,
                         "params": {"ampl": 0.5, "seconds": 0.1, "center_hz": 915e6}})
        assert p["kind"] == kind, tech
        assert "center_hz" not in p          # RF keys still stripped


def test_explicit_kind_is_kept():
    p = _gen_params({"id": "s2", "technique": "tools.tone",
                     "params": {"kind": "two_tone", "ampl": 0.5}})
    assert p["kind"] == "two_tone"


def test_technique_without_a_dot_adds_no_kind():
    p = _gen_params({"id": "s3", "technique": "record", "params": {"ampl": 0.5}})
    assert "kind" not in p


def test_pulsed_keeps_prf_and_duty():
    p = _gen_params({"id": "s4", "technique": "jammer.tone",
                     "params": {"ampl": 0.5, "prf": 100.0, "duty": 0.5}})
    assert p["kind"] == "tone" and p["prf"] == 100.0 and p["duty"] == 0.5
