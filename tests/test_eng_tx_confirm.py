"""Engineering TX confirm text (link_tab.eng_tx_confirm_text).

A B200 is a caps-driven radio: the confirm names its own TX port and the TX
gain in dB it inits with, not the hidden G40 attenuation field or a TRx jack.
The G40 text (handle, jack, attenuation) is unchanged."""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

pytest.importorskip("nicegui")
from dpmg.web import link_tab as lt                 # noqa: E402


def test_g40_text_unchanged():
    t = lt.eng_tx_confirm_text("G40-1", "425.0250 MHz", "CONTINUOUS", family="epiq",
                               kind="g40", handle="TXA1", jack="TRx1B", atten_db="30",
                               tx_port="TXA1", tx_gain_db=50)
    assert t == ("Host G40-1: transmit on TXA1 (TRx1B) at 425.0250 MHz, atten 30 dB, "
                 "mode CONTINUOUS.")


def test_no_caps_yet_is_the_g40_text():
    t = lt.eng_tx_confirm_text("G40-1", "the TX LO", "BURST", family=None, kind="g40",
                               handle="TXB1", jack="TRx2B", atten_db="35")
    assert t == "Host G40-1: transmit on TXB1 (TRx2B) at the TX LO, atten 35 dB, mode BURST."


def test_b200_shows_port_and_tx_gain():
    t = lt.eng_tx_confirm_text("B200-1", "425.0250 MHz", "CONTINUOUS", family="uhd",
                               kind="b200", handle="TX/RX", jack="TRx1A", atten_db="20",
                               tx_port="TX/RX", tx_gain_db=40.0, gain_mode="gain")
    assert t == ("Host B200-1: transmit on B200 port TX/RX at 425.0250 MHz, TX gain 40 dB, "
                 "mode CONTINUOUS.")
    assert "atten" not in t and "TRx" not in t


def test_b200_fractional_gain_and_missing_value():
    t = lt.eng_tx_confirm_text("B200-1", "x", "CONTINUOUS", family="uhd", kind="b200",
                               tx_port="TX/RX", tx_gain_db=44.75)
    assert "TX gain 44.75 dB" in t
    t = lt.eng_tx_confirm_text("B200-1", "x", "CONTINUOUS", family="uhd", kind="b200",
                               tx_port="TX/RX", tx_gain_db=None)
    assert "TX gain ? dB" in t


def test_soapy_atten_mode_says_atten():
    t = lt.eng_tx_confirm_text("XSDR-1", "x", "CONTINUOUS", family="soapy", kind="xsdr",
                               tx_port="TX", tx_gain_db=12, gain_mode="atten")
    assert "XSDR port TX" in t and "TX atten 12 dB" in t


if __name__ == "__main__":  # OFF-01 loop runs `python3 tests/<file>.py`; delegate to pytest
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
