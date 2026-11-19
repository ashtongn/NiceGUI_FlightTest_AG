"""USB radio presence (sdr_core.usb_radio_present) and the radio card's link
line (link_tab.link_text): LINK OK only means the service answers, so a
service whose USB radio is unplugged shows 'LINK OK, no radio found'."""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from dpmg.radio import sdr_core as sdr                 # noqa: E402


def _dev(root, name, vid, pid, product=None):
    d = root / name
    d.mkdir()
    (d / "idVendor").write_text(vid + "\n")
    (d / "idProduct").write_text(pid + "\n")
    if product is not None:
        (d / "product").write_text(product + "\n")


@pytest.fixture
def supernano(tmp_path):
    # what the Super Nano shows: two hubs, root hubs, a B200mini
    _dev(tmp_path, "usb1", "1d6b", "0002", "xHCI Host Controller")
    _dev(tmp_path, "usb2", "1d6b", "0003", "xHCI Host Controller")
    _dev(tmp_path, "1-1", "0bda", "5489", "4-Port USB 2.0 Hub")
    _dev(tmp_path, "2-1", "0bda", "0489", "4-Port USB 3.0 Hub")
    _dev(tmp_path, "2-1.1", "2500", "0021", "Unknown")
    (tmp_path / "1-0:1.0").mkdir()          # interface dirs have no idVendor
    return str(tmp_path)


def test_b200_found_hackrf_and_lime_not(supernano):
    assert sdr.usb_radio_present("b200", supernano) is True
    assert sdr.usb_radio_present("hackrf", supernano) is False
    assert sdr.usb_radio_present("hackrf_pro", supernano) is False
    assert sdr.usb_radio_present("limesdr", supernano) is False
    assert sdr.usb_radio_present("pluto", supernano) is False


def test_found_by_id_or_product_string(tmp_path):
    _dev(tmp_path, "1-2", "1d50", "6089", "HackRF One")
    _dev(tmp_path, "2-2", "0403", "601f")                    # LimeSDR Mini (FT601), no string
    _dev(tmp_path, "1-3", "1234", "5678", "PlutoSDR (ADALM-PLUTO)")
    root = str(tmp_path)
    assert sdr.usb_radio_present("hackrf", root) is True
    assert sdr.usb_radio_present("limesdr_mini", root) is True
    assert sdr.usb_radio_present("pluto", root) is True
    assert sdr.usb_radio_present("b200", root) is False


def test_b200_before_firmware_load(tmp_path):
    # just plugged in: the FX3 boot loader id until UHD loads the firmware
    _dev(tmp_path, "2-1.3", "04b4", "00f3", "WestBridge")
    assert sdr.usb_radio_present("b200", str(tmp_path)) is True


def test_other_openmoko_ids_are_not_a_hackrf(tmp_path):
    _dev(tmp_path, "1-2", "1d50", "6108", "LimeSDR-USB")     # same vendor, other radio
    root = str(tmp_path)
    assert sdr.usb_radio_present("hackrf", root) is False
    assert sdr.usb_radio_present("lime", root) is True


def test_unchecked_kinds_and_bad_sysfs(supernano, tmp_path):
    for k in ("g40", "x40", "mock", "x410", "xsdr", None, ""):
        assert sdr.usb_radio_present(k, supernano) is None
    assert sdr.usb_radio_present("b200", str(tmp_path / "missing")) is None


try:
    import nicegui  # noqa: F401
    from dpmg.web import link_tab as lt
except ImportError:                    # service images have no nicegui
    lt = None


@pytest.mark.skipif(lt is None, reason="nicegui not installed")
def test_link_text():
    assert lt.link_text(True, {"hw_present": True}) == ("LINK OK", "text-green-600")
    assert lt.link_text(True, {"hw_present": None}) == ("LINK OK", "text-green-600")
    assert lt.link_text(True, None) == ("LINK OK", "text-green-600")
    assert lt.link_text(True, {"hw_present": False}) == ("LINK OK, no radio found",
                                                          "text-amber-600")
    assert lt.link_text(False, {"hw_present": False}) == ("LINK LOST", "text-red-600")
    assert lt.link_text(False, None, lost="STALE")[0] == "STALE"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
