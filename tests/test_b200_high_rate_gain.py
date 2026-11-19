"""B200 at 50 MSps and up gets 60 dB RX gain (link_tab.b200_rate_gain).

The AD9364's own ADC noise lifts the band edges from 50 MSps up (a U in the
PSD); 60 dB of RX gain puts the front-end noise above it. A rate pick at or
above 50 MSps raises a lower gain to 60 and remembers the old one; going back
below 50 MSps restores it unless the operator changed the gain in between.
Other radios are never touched."""
import os
import sys
import types

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

pytest.importorskip("nicegui")
from dpmg.web import link_tab as lt                 # noqa: E402

HI = lt.B200_HIGH_RATE_RX_GAIN_DB


def test_other_radios_untouched():
    assert lt.b200_rate_gain("g40", 61.44e6, 40, None) == (None, None, False)
    assert lt.b200_rate_gain(None, 61.44e6, 40, None) == (None, None, False)


def test_below_50_msps_no_change():
    assert lt.b200_rate_gain("b200", 30.72e6, 40, None) == (None, None, False)


def test_high_rate_raises_low_gain_and_remembers_it():
    assert lt.b200_rate_gain("b200", 61.44e6, 40, None) == (HI, 40.0, True)
    assert lt.b200_rate_gain("b200", 50e6, 20, None) == (HI, 20.0, True)


def test_high_rate_leaves_a_higher_gain_alone():
    assert lt.b200_rate_gain("b200", 56e6, 70, None) == (None, None, True)
    assert lt.b200_rate_gain("b200", 61.44e6, HI, None) == (None, None, True)


def test_back_below_restores_unless_operator_changed_it():
    assert lt.b200_rate_gain("b200", 30.72e6, HI, 40.0) == (40.0, None, False)
    assert lt.b200_rate_gain("b200", 30.72e6, 45, 40.0) == (None, None, False)


def test_two_high_picks_keep_the_original_gain():
    new, bumped, _ = lt.b200_rate_gain("b200", 61.44e6, 40, None)
    new2, bumped2, _ = lt.b200_rate_gain("b200", 56e6, new, bumped)
    assert new2 is None and bumped2 == 40.0
    assert lt.b200_rate_gain("b200", 15.36e6, HI, bumped2)[0] == 40.0


def test_operator_lowers_gain_at_high_rate_is_kept():
    # turned down for a strong signal while at 61.44: a later pick of another
    # high rate raises it again only because it is below 60 (stated in the note)
    new, bumped, show = lt.b200_rate_gain("b200", 56e6, 30, 40.0)
    assert new == HI and bumped == 40.0 and show


def test_bad_values_are_ignored():
    assert lt.b200_rate_gain("b200", "x", 40, None) == (None, None, False)


def test_nearest_gain_key():
    sel = types.SimpleNamespace(options={"0": "0 dB", "40": "40 dB", "59.5": "59.5", "62": "62"})
    assert lt._nearest_gain_key(sel, 60.0) == "59.5"
    assert lt._nearest_gain_key(types.SimpleNamespace(options={}), 60.0) is None


def test_lo_best_choice_outside_band_up_to_20_msps():
    lo, raise_gain, msg, kind = lt.lo_best_choice(15.36e6, +1, 40)
    assert lo == 9.216 and not raise_gain and kind == "info"
    assert "outside the band" in msg and "START" in msg


def test_lo_best_choice_guard_strip_raises_low_gain():
    lo, raise_gain, msg, _ = lt.lo_best_choice(30.72e6, -1, 40)
    assert lo == -12.788 and raise_gain and "60 dB" in msg and "roll-off strip" in msg
    lo, raise_gain, msg, _ = lt.lo_best_choice(25e6, +1, 70)
    assert lo == 10.5 and not raise_gain and "60 dB" not in msg


def test_lo_best_choice_no_room_from_40_msps():
    lo, raise_gain, msg, kind = lt.lo_best_choice(61.44e6, +1, 60)
    assert lo == 0.0 and not raise_gain and kind == "warning" and "no room" in msg.lower()


def test_lo_best_choice_bad_rate():
    assert lt.lo_best_choice(None, +1, 40)[3] == "warning"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
