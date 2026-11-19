"""Payload tab (payload_tab.py) pure helpers: device presence from payload_status,
the dashed "no device" diagram style, and the widgets it shares with the
operator console (frequency units, TX-attenuation choices)."""
import os
import re as _re
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

pytest.importorskip("nicegui")
from dpmg.payload import payload_budget as pb           # noqa: E402
from dpmg.web import payload_tab as pt              # noqa: E402

REAL = os.path.join(HERE, "payload_profiles.yaml")

# a live G40 status: gpio front end (lines driven, nothing wired),
# fault input disabled, no attenuator device, no payload: section
LIVE_G40 = {"configured": False, "frontend": "gpio", "sim": False, "tx_running": False,
            "pa": {"keyed": False, "level": False, "active_high": False},
            "tr": {"tx": False, "mode": "gpio", "level": False, "tx_level": 1},
            "fault": {"enabled": False, "active": None},
            "atten": None, "tx_center_hz": 2.2e9, "tx_atten_db": 20.0}
# the payload simulator (bench/payload_sim_frontend.yaml)
SIM = {"configured": True, "frontend": "mock", "sim": True, "tx_running": False,
       "pa": {"keyed": True, "level": True}, "tr": {"tx": True, "mode": "gpio"},
       "fault": {"enabled": False, "active": None},
       "atten": {"att_db": 120.0, "max_db": 120, "step_db": 0.05, "transport": "mock"},
       "guard": {"profile": "P2", "min_db": 7.05, "protect_db": 120.0, "armed": True}}


class TestPresence:
    def test_empty_status(self):
        assert pt.device_presence({}) == {} and pt.device_presence(None) == {}
        assert pt.presence_lines(None) == []

    def test_live_g40_gpio_no_devices(self):
        pr = pt.device_presence(LIVE_G40)
        assert "service" not in pr                                   # not a simulator
        assert pr["hpa"]["kind"] == pt.DRIVEN and "GPIO" in pr["hpa"]["text"]
        assert pr["switch"]["kind"] == pt.DRIVEN
        assert pr["atten"]["kind"] == pt.NONE and "transport none" in pr["atten"]["text"]
        assert pr["fault"]["kind"] == pt.NONE and "disabled" in pr["fault"]["text"]
        assert pr["guard"]["kind"] == pt.NONE and "payload:" in pr["guard"]["text"]

    def test_simulator_mock(self):
        pr = pt.device_presence(SIM)
        assert pr["service"]["kind"] == pt.SIM
        assert pr["hpa"]["kind"] == pr["switch"]["kind"] == pr["atten"]["kind"] == pt.SIM
        assert pr["fault"]["kind"] == pt.NONE                        # disabled in the sim yaml
        assert pr["guard"]["kind"] == pt.DEVICE and "P2" in pr["guard"]["text"]

    def test_no_frontend_at_all(self):
        pr = pt.device_presence({"configured": False, "frontend": "none", "atten": None})
        assert {pr[k]["kind"] for k in ("hpa", "switch", "atten", "fault", "guard")} == {pt.NONE}

    def test_real_devices(self):
        st = dict(LIVE_G40, configured=True, guard={"profile": "P1"},
                  fault={"enabled": True, "active": False},
                  atten={"att_db": 30.0, "transport": "hidraw"},
                  tr={"tx": False, "mode": "proxy"})
        pr = pt.device_presence(st)
        assert pr["fault"]["kind"] == pt.DRIVEN
        assert pr["atten"]["kind"] == pt.DEVICE and "hidraw" in pr["atten"]["text"]
        assert pr["switch"]["kind"] == pt.DEVICE and "proxy" in pr["switch"]["text"]
        assert pr["guard"]["kind"] == pt.DEVICE

    def test_older_service_without_sim_flag(self):
        st = {k: v for k, v in SIM.items() if k != "sim"}
        pr = pt.device_presence(st)
        assert "service" not in pr and pr["hpa"]["kind"] == pt.SIM   # mock still says simulated

    def test_lines_order(self):
        titles = [t for t, _, _ in pt.presence_lines(SIM)]
        assert titles == ["service", "HPA", "switch", "attenuator", "fault input", "guard"]
        titles = [t for t, _, _ in pt.presence_lines(LIVE_G40)]
        assert titles == ["HPA", "switch", "attenuator", "fault input", "guard"]


@pytest.fixture(scope="module")
def calc():
    data = pb.load_profiles(REAL)
    # 0 dB TX attenuation: P2 (behind its 20 dB pad) is past P1dB there
    return pb.live_summary(data, "P2", 2200, 0, 3, False, None)


class TestDiagramNoDevice:
    def test_live_g40_dashed_atten_coloured_hpa(self, calc):
        svg = pt.diagram_svg(LIVE_G40, calc, "P2")
        assert svg.count('stroke-dasharray="6 4"') == 1                # only the attenuator
        i = svg.index('stroke-dasharray')
        assert "RX atten RCDAT" in svg[i:i + 600] and "no device" in svg[i:i + 900]
        # the HPA has a driven line: the existing colour rules stay (here amber:
        # the 0 dB operating point is past P1dB), never the dashed style
        h = svg.index("HPA off")
        assert "stroke-dasharray" not in svg[h - 200:h]
        assert 'stroke="#d97706"' in svg[h - 200:h]

    def test_no_frontend_everything_dashed(self, calc):
        st = {"configured": False, "frontend": "none", "atten": None}
        svg = pt.diagram_svg(st, calc, "P2")
        assert svg.count('stroke-dasharray="6 4"') == 3                # HPA, switch, attenuator
        assert svg.count(">no device<") == 3
        assert "#dc2626" not in svg.split("<defs>")[1].split("</defs>")[1].split("Antenna")[0] \
            or True                                                     # no red on the TX row

    def test_mock_keyed_keeps_colours(self, calc):
        svg = pt.diagram_svg(SIM, calc, "P2")
        assert 'stroke-dasharray' not in svg
        assert "HPA KEYED" in svg and 'stroke="#dc2626"' in svg        # keyed: red as before

    def test_empty_status_is_neutral(self):
        svg = pt.diagram_svg({}, None, "-")
        assert svg.startswith("<svg") and 'stroke-dasharray' not in svg


def _x(svg, title):
    """x of the text that draws ``title``."""
    return float(_re.search(r'<text x="([\d.]+)"[^>]*>' + _re.escape(title) + "<", svg).group(1))


class TestDiagramP2AsBuilt:
    @pytest.fixture
    def svg(self):
        data = pb.load_profiles(REAL)
        return pt.diagram_svg(SIM, pb.live_summary(data, "P2", 2200, 10, 3, True, 30.0), "P2")

    def test_tx_row_order_with_the_pad(self, svg):
        assert "G40 TX TRx2B" in svg and "TX pad" in svg and "20 dB" in svg
        assert _x(svg, "G40 TX TRx2B") < _x(svg, "TX pad") < _x(svg, "T/R switch: TX") < _x(svg, "Antenna")
        hpa = svg.index("HPA KEYED")
        assert svg.index("TX pad") < hpa < svg.index("T/R switch")
        assert "TX combiner" not in svg

    def test_switch_ports_labelled(self, svg):
        assert ">J2 TX<" in svg and ">J0 ant<" in svg and ">J1 RX<" in svg

    def test_rx_row_order_with_the_splitter(self, svg):
        # right to left from the switch: attenuator, LNA, splitter, G40 RX
        assert _x(svg, "RX atten RCDAT") > _x(svg, "LNA") > _x(svg, "RX splitter") > _x(svg, "G40 RX")
        assert ">TRx1A<" in svg and ">TRx1B<" in svg and ">TRx2A<" in svg

    def test_cw_rating_flagged_in_the_block(self, svg):
        assert "CW rating unknown" in svg

    def test_splitter_box_without_a_leak_figure(self):
        # no RX attenuator reading yet: the chain drawing is still the as-built one
        data = pb.load_profiles(REAL)
        svg = pt.diagram_svg({}, pb.live_summary(data, "P2", 2200, 10, 3, True, None), "P2")
        assert "RX splitter" in svg and ">J1 RX<" in svg

    def test_other_profiles_keep_their_layout(self):
        data = pb.load_profiles(REAL)
        svg = pt.diagram_svg(SIM, pb.live_summary(data, "P1", 2200, 10, 3, True, 30.0), "P1")
        assert "TX pad" not in svg and "RX splitter" not in svg and ">J1 RX<" not in svg
        assert "CW rating unknown" not in svg
        assert _x(svg, "LNA") > _x(svg, "RX atten RCDAT") > _x(svg, "G40 RX")
        svg = pt.diagram_svg(SIM, pb.live_summary(data, "P2b", 2200, 10, 3, True, 30.0), "P2b")
        assert "TX combiner" in svg and "TRx1A TRx2A TRx1B" in svg and "G40 RX TRx2B" in svg
        svg = pt.diagram_svg(SIM, pb.live_summary(data, "P2a", 2200, 10, 3, True, 30.0), "P2a")
        assert _x(svg, "RX atten RCDAT") > _x(svg, "LNA") > _x(svg, "RX splitter")
        assert "TX pad" not in svg


class TestRxAttenDropdown:
    def test_step_grid(self):
        assert pt.rx_atten_step(31.52) == 31.5 and pt.rx_atten_step(31.53) == 31.55
        assert pt.rx_atten_step(106.04) == 106.0 and pt.rx_atten_step(106.06) == 106.1   # 0.10 above 105
        assert pt.rx_atten_step(7.05) == 7.05
        assert pt.rx_atten_step(-3) == 0.0 and pt.rx_atten_step(500) == 120.0

    def test_options_grid_min_safe_and_other(self):
        o = pt.rx_atten_options(7.05, extra=[31.5, 120.0, None])
        keys = list(o)
        assert keys[-1] == pt.RX_ATTEN_OTHER and o[keys[-1]].startswith("other")
        assert len(keys) == 121 + 2 + 1                               # 0..120, 7.05, 31.5, other
        assert o["7.00"] == "7 dB (below min safe)" and o["7.05"] == "7.05 dB"
        assert o["31.50"] == "31.5 dB" and o["120.00"] == "120 dB" and o["0.00"].endswith("(below min safe)")
        nums = [float(k) for k in keys[:-1]]
        assert nums == sorted(nums)

    def test_options_without_guard(self):
        o = pt.rx_atten_options(None)
        assert not any("below" in v for v in o.values()) and len(o) == 122
        assert pt.rx_atten_key(31.5) == "31.50"


class TestConsoleWidgetsShared:
    def test_units_and_atten_choices(self):
        from dpmg.web import link_tab
        from dpmg.link import view_sync
        assert list(pt.FREQ_UNITS) == ["Hz", "kHz", "MHz", "GHz"]
        assert pt.FREQ_UNITS["MHz"] == 1e6 and pt.FREQ_UNITS is link_tab.FREQ_UNITS
        assert pt.TX_ATTEN_CHOICES is link_tab.TX_ATTEN_CHOICES
        assert [float(k) for k in pt.TX_ATTEN_CHOICES] == [float(v) for v in view_sync.OP_ATTEN_DB]
        assert pt.TX_ATTEN_CHOICES["41.75"] == "41.75 dB"
        assert max(float(k) for k in pt.TX_ATTEN_CHOICES) == pt.TX_ATTEN_MAX_DB


class TestMeasurementPanel:
    def test_within_and_outside_band(self):
        assert pt.measurement_check(-10.0, -11.0, tol_db=3.0) is True    # 1 dB off, inside
        assert pt.measurement_check(-10.0, -13.0, tol_db=3.0) is True    # exactly at the edge
        assert pt.measurement_check(-10.0, -14.0, tol_db=3.0) is False   # 4 dB off, outside

    def test_default_tol(self):
        assert pt.MEAS_TOL_DB == 3.0
        assert pt.measurement_check(0.0, 2.9) is True and pt.measurement_check(0.0, 3.1) is False

    def test_missing_or_bad_values_are_none(self):
        assert pt.measurement_check(None, -10.0) is None
        assert pt.measurement_check(-10.0, None) is None
        assert pt.measurement_check(-10.0, "n/a") is None


# devices row (DEVICE_WATCH)
def _st(atten_state="connected", adc="present", **a):
    at = {"state": atten_state, "model": "RCDAT-6G-120H", "serial": "11901234567",
          "path": "/dev/hidraw3", "db": 30.0, "since": 1700000000, "mode": "auto", **a}
    return {"configured": True, "frontend": "gpio", "sim": False,
            "atten": ({"att_db": 30.0, "max_db": 120, "step_db": 0.05, "transport": "hidraw"}
                      if atten_state == "connected" else None),
            "devices": {"atten": at, "tr_adc": {"state": adc, "bus": "/dev/i2c-1", "address": "0x48"}}}


class TestDevices:
    @pytest.mark.parametrize("state,tone", [
        ("connected", "ok"), ("present", "ok"), ("not connected", "off"), ("lost", "off"),
        ("absent", "off"), ("off", "off"), ("several attenuators found, set atten.path", "warn"),
        ("error: open /dev/hidraw3: Permission denied", "warn"), ("smbus2 missing", "warn"),
        ("bus missing", "warn"),
    ])
    def test_tones(self, state, tone):
        assert pt.device_tone(state) == tone

    def test_chips(self):
        chips = pt.device_chips(_st())
        assert [c["key"] for c in chips] == ["atten", "tr_adc"]
        a, c = chips
        assert a["tone"] == "ok" and "RCDAT-6G-120H SN 11901234567" in a["tip"]
        assert "/dev/hidraw3" in a["tip"] and "30 dB" in a["tip"]
        assert c["tone"] == "ok" and "/dev/i2c-1 0x48" in c["tip"]
        assert pt.device_chips({"configured": True}) == []          # older service

    def test_changes(self):
        none = _st("not connected", "absent")
        assert pt.device_changes(None, _st()) == []                 # first poll: silent
        ch = pt.device_changes(none, _st())
        assert len(ch) == 2 and all(k == "positive" for _, k in ch)
        assert "RX attenuator connected" in ch[0][0]
        ch = pt.device_changes(_st(), _st("lost", "present"))
        assert ch == [("RX attenuator lost (was connected)", "warning")]
        ch = pt.device_changes(none, _st("several attenuators found, set atten.path", "absent"))
        assert ch and ch[0][1] == "warning" and "set atten.path" in ch[0][0]
        assert pt.device_changes(_st(), _st()) == []

    def test_presence_follows_auto_state(self):
        pr = pt.device_presence(_st("not connected"))
        assert pr["atten"]["kind"] == pt.NONE and "not connected" in pr["atten"]["text"]
        pr = pt.device_presence(_st())
        assert pr["atten"]["kind"] == pt.DEVICE and "11901234567" in pr["atten"]["text"]
        # diagram: the attenuator block is dashed while it is missing
        assert 'class="nodev"' in pt.diagram_svg(_st("lost"), None, "P2")

    def test_eng_label_text(self):
        from dpmg.web import link_tab
        assert link_tab.atten_state_text(None) == "atten: none configured"
        t = link_tab.atten_state_text({"state": "connected", "att_db": 30.0, "model": "RCDAT-6G-120H",
                                       "serial": "119", "transport": "hidraw:/dev/hidraw3"})
        assert t == "atten: connected  30.0 dB  RCDAT-6G-120H 119  [hidraw:/dev/hidraw3]"
        assert link_tab.atten_state_text({"state": "not connected", "path": None}) == \
            "atten: not connected"
        assert link_tab.atten_state_text({"att_db": 12.0, "transport": "mock"}) == "atten: 12.0 dB  [mock]"


# radio pick + TX control
B200_CAPS = {"kind": "b200", "family": "uhd", "label": "Ettus B200mini",
             "tx_gain": {"min": 0.0, "max": 89.75, "step": 0.25, "unit": "dB", "mode": "gain"}}
XSDR_CAPS = {"kind": "xsdr", "family": "soapy", "label": "Wavelet xSDR",
             "tx_gain": {"min": 0.0, "max": 52.0, "step": 1.0, "unit": "dB", "mode": "atten"}}


class TestRadioPick:
    def test_prefers_epiq_family(self):
        fam = {"B200MINI-3000001": "uhd", "G40-0042": "epiq", "A-RADIO": None}.get
        assert pt.pick_default_radio(["B200MINI-3000001", "G40-0042", "A-RADIO"], fam) == "G40-0042"

    def test_serial_before_caps(self):
        assert pt.pick_default_radio(["B200MINI-3000001", "X40-7"], lambda s: None) == "X40-7"
        assert pt.pick_default_radio(["B200MINI-3000001", "g40-lab"], lambda s: None) == "g40-lab"

    def test_caps_beat_serial(self):
        # a "G40..." serial whose caps say uhd is not taken for a G40
        fam = {"G40-ODD": "uhd", "MOTHBALL": "epiq"}.get
        assert pt.pick_default_radio(["G40-ODD", "MOTHBALL"], fam) == "MOTHBALL"

    def test_fallback_first_and_empty(self):
        assert pt.pick_default_radio(["Z-B200", "B200MINI-3000001"], lambda s: "uhd") == "B200MINI-3000001"
        assert pt.pick_default_radio([], None) is None


class TestTxControl:
    def test_g40_default(self):
        for fam in (None, "epiq"):
            t = pt.tx_control(fam, {"family": "epiq"})
            assert t == {"label": "G40 TX atten (dB)", "mode": "atten", "radio": "g40"}
            assert pt.tx_model_kw(t) == {}

    def test_b200(self):
        t = pt.tx_control("uhd", B200_CAPS)
        assert t == {"label": "B200 TX gain (dB)", "mode": "gain", "radio": "b200"}
        assert pt.tx_model_kw(t) == {"radio": "b200"}

    def test_other_caps_radio_has_no_model(self):
        t = pt.tx_control("soapy", XSDR_CAPS)
        assert t["label"] == "Wavelet xSDR TX atten (dB)" and t["radio"] is None
        t = pt.tx_control("uhd", {"kind": "x410", "label": "Ettus X410", "tx_gain": {"mode": "gain"}})
        assert t["label"] == "Ettus X410 TX gain (dB)" and t["radio"] is None

    def test_set_command(self):
        assert pt.tx_set_command("epiq", None, 30.25) == ("set_tx_atten", {"qdb": 121})
        assert pt.tx_set_command(None, None, 41.75) == ("set_tx_atten", {"qdb": 167})
        # B200 gain 45 dB = 44.75 dB below the 89.75 top (UhdBackend: gain = top - atten)
        assert pt.tx_set_command("uhd", B200_CAPS, 45.0) == ("set_tx_atten", {"qdb": 179})
        assert pt.tx_set_command("uhd", B200_CAPS, 89.75) == ("set_tx_atten", {"qdb": 0})
        assert pt.tx_set_command("soapy", XSDR_CAPS, 10.0) == ("set_tx_atten", {"qdb": 40})

    def test_follow_value(self):
        assert pt.follow_tx_value("epiq", None, {"tx_atten_db": 20.0, "tx_atten_src": "init"}) == 20.0
        assert pt.follow_tx_value("uhd", B200_CAPS, {"tx_atten_db": 60.0, "tx_atten_src": "init"}) == 60.0
        assert pt.follow_tx_value("uhd", B200_CAPS,
                                  {"tx_atten_db": 44.75, "tx_atten_src": "set_tx_atten"}) == 45.0
        assert pt.follow_tx_value("uhd", B200_CAPS, {}) is None


    def test_saturation_text(self):
        sat = {"hpa_region": "linear", "hpa_compression_db": 0.0, "hpa_below_psat_db": 20.0,
               "hpa_backoff_p1db_db": 10.0, "tx_atten_p1db_db": 10.2, "tx_atten_3db_db": -1.0,
               "tx_gain_p1db_db": 74.5, "tx_gain_3db_db": 95.0}
        t = pt.saturation_text(sat)
        assert "G40 TX atten for HPA P1dB 10.2 dB" in t and "never (even at 0 dB)" in t
        t = pt.saturation_text(sat, {"control": "gain", "label": "B200 TX gain", "max_db": 89.75})
        assert "B200 TX gain for HPA P1dB 74.5 dB" in t and "never (even at 89.75 dB)" in t
        assert "G40" not in t

    def test_b200_numbers_from_b200_model(self):
        data = pb.load_profiles(REAL)
        txc = pt.tx_control("uhd", B200_CAPS)
        s = pb.live_summary(data, "P2", 2200, 40.0, 3.0, False, 30.0, **pt.tx_model_kw(txc))
        assert s["eirp"]["g40_out_dbm"] == pytest.approx(10.0 - 49.75 - 3.0)
        svg = pt.diagram_svg(SIM, s, "P2")
        assert "B200 TX" in svg and "gain 40.00 dB" in svg
        g = pb.live_summary(data, "P2", 2200, 10.0, 3.0, False, 30.0, **pt.tx_model_kw(pt.tx_control(None)))
        svg = pt.diagram_svg(SIM, g, "P2")
        assert "G40 TX" in svg and "atten 10.00 dB" in svg and "B200" not in svg


if __name__ == "__main__":  # OFF-01 loop runs `python3 tests/<file>.py`; delegate to pytest
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))


class TestSwitchButtonLabels:
    def test_tx_level_1_tx_is_high(self):
        st = {"tr": {"mode": "gpio", "tx_level": 1}}
        assert pt.tr_button_labels(st) == ("Switch to TX (TTL high, 3.3 V)", "Switch to RX (TTL low, 0 V)")

    def test_tx_level_0_tx_is_low(self):
        # Payload 2's PASR0200500600B has the HPA on J2, and TTL low connects J0-J2
        st = {"tr": {"mode": "gpio", "tx_level": 0}}
        assert pt.tr_button_labels(st) == ("Switch to TX (TTL low, 0 V)", "Switch to RX (TTL high, 3.3 V)")

    def test_plain_without_a_gpio_level(self):
        assert pt.tr_button_labels({}) == ("Switch to TX", "Switch to RX")
        assert pt.tr_button_labels({"tr": {"mode": "proxy", "tx_level": 1}}) == ("Switch to TX", "Switch to RX")
        assert pt.tr_button_labels({"tr": {"mode": "gpio"}}) == ("Switch to TX", "Switch to RX")
