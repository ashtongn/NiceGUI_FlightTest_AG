"""payload_budget.py (shared EIRP / RX-protection / link-budget math), the real
payload_profiles.yaml, the /payload page's diagram renderer and the workbook
generator: offline.

Hand-checked numbers use a synthetic fixture so they do not move when the
datasheet file is edited; the real file is checked for structure, sources and
sane results over every profile's band.

The real payload has no coupler and no power meter, and the RX attenuator sits
between the LNA and the G40:
  TX:  G40 TX then cable then HPA then cable then switch then cable then antenna
  RX:  antenna then switch then cable then LNA then cable then RX attenuator then cable then G40 RX
"""
import math
import copy
import os
import shutil
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from dpmg.payload import payload_budget as pb  # noqa: E402

REAL = os.path.join(HERE, "payload_profiles.yaml")

FIX = {
    "components": {
        "g40tx": {"max_output_dbm": {"value": 5}},
        "g40rx": {"max_safe_input_dbm": {"value": 20}, "noise_figure_db": {"value": 5}},
        "hpa": {"gain_db": {"points": [[1000, 50], [2000, 60]]}, "p1db_dbm": {"value": 47},
                "psat_dbm": {"value": 51}, "max_input_dbm": {"value": 10}},
        "sw": {"insertion_loss_db": {"value": 0.6},
               "isolation_db": {"points": [[800, 65], [2000, 65], [2001, 60], [2700, 60]]},
               "power_rating_dbm": {"value": 50}},
        "ant": {"gain_dbi": {"value": 2}, "max_input_dbm": {"value": 50}},
        "lna": {"gain_db": {"value": 20}, "nf_db": {"value": 1.0}, "max_input_dbm": {"value": 0}},
        "att": {"insertion_loss_db": {"value": 5}, "max_db": {"value": 120}, "step_db": {"value": 0.05},
                "max_input_dbm": {"value": 23}},
        # bench fixture (not on any payload chain); used only by bench_loopback_expected
        "cpl": {"coupling_db": {"value": 30}, "mainline_loss_db": {"value": 0.1}},
    },
    "profiles": {
        "PX": {"g40_tx": "g40tx", "g40_rx": "g40rx", "hpa": "hpa", "switch": "sw", "antenna": "ant",
               "lna": "lna", "rx_atten": "att", "band_mhz": [1000, 2500],
               "cables_db": {"g40_to_hpa": 0.5, "hpa_to_switch": 0.2, "switch_to_antenna": 0.5,
                             "switch_to_lna": 0.2, "lna_to_atten": 0.2, "atten_to_g40": 0.3}},
    },
    "protection": {"margin_db": {"value": 10}},
    "bench_fixture": {"coupler": "cpl", "power_meter": "pm"},
    "required_esn0": {"rows": [{"mod": "QPSK", "fec": "1/2", "bits_per_symbol": 2, "code_rate": 0.5,
                                "esn0_db": 4.0}]},
}


@pytest.fixture
def prof():
    return pb.profile(FIX, "PX")


# primitives
class TestPrimitives:
    def test_interp_and_clamp(self):
        p = [[1000, 50], [2000, 60]]
        assert pb.interp(p, 1500) == (55.0, True)
        assert pb.interp(p, 500) == (50.0, False)
        assert pb.interp(p, 2500) == (60.0, False)
        assert pb.interp(p, 2000) == (60.0, True)

    def test_out_of_span_warns(self, prof):
        prof.get("hpa", "gain_db", 3000)
        assert any("outside the tabulated span" in w for w in prof.warnings)

    def test_missing_value_raises(self, prof):
        with pytest.raises(pb.ProfileError, match="no value"):
            prof.get("lna", "p1db_out_dbm", 1500)

    def test_rapp_s_hits_p1db(self):
        s = pb.rapp_s_from_p1db(47.0, 51.0)
        # at the 1 dB point the output is P1dB and 1 dB below the linear output
        lin = None
        for li in [i / 100 for i in range(4000, 5600)]:
            out = pb.rapp_out_dbm(li, 51.0, s)
            if li - out >= 1.0:
                lin = li
                break
        assert lin is not None and pb.rapp_out_dbm(lin, 51.0, s) == pytest.approx(47.0, abs=0.02)

    def test_rapp_limits(self):
        assert pb.rapp_out_dbm(20.0, 51.0, 1.4) == pytest.approx(20.0, abs=0.01)   # linear region
        assert pb.rapp_out_dbm(80.0, 51.0, 1.4) == pytest.approx(51.0, abs=0.01)   # saturates at Psat

    def test_rapp_rejects_p1db_above_psat(self):
        with pytest.raises(pb.ProfileError):
            pb.rapp_s_from_p1db(52, 51)

    def test_ceil_step(self):
        assert pb.ceil_step(7.01, 0.05) == 7.05
        assert pb.ceil_step(7.05, 0.05) == 7.05
        assert pb.ceil_step(-3, 0.05) == 0.0

    def test_fspl_known_value(self):
        # 1 km at 1000 MHz = 92.45 dB
        assert pb.fspl_db(1.0, 1000.0) == pytest.approx(92.45, abs=0.01)

    def test_friis_two_stage(self):
        st = [{"gain_db": 20, "nf_db": 1.0}, {"gain_db": 0, "nf_db": 10.0}]
        f = 10 ** 0.1 + (10 - 1) / 100
        assert pb.friis_nf_db(st) == pytest.approx(10 * math.log10(f), abs=1e-9)

    def test_passive_first_stage_adds_loss(self):
        st = [{"gain_db": -3, "nf_db": 3}, {"gain_db": 30, "nf_db": 1}]
        assert pb.friis_nf_db(st) == pytest.approx(4.0, abs=0.01)


# EIRP
class TestEirp:
    def test_chain_by_hand(self, prof):
        e = pb.eirp_chain(prof, 1500, tx_atten_db=20, backoff_db=3)
        assert e["g40_out_dbm"] == pytest.approx(5 - 20 - 3)
        assert e["hpa_in_dbm"] == pytest.approx(-18.5)             # g40 out - cable 0.5
        assert e["hpa_gain_db"] == pytest.approx(55.0)
        lin = -18.5 + 55
        out = pb.rapp_out_dbm(lin, 51, e["rapp_s"])
        assert e["hpa_out_dbm"] == pytest.approx(out)
        assert e["hpa_compression_db"] == pytest.approx(lin - out)
        # no coupler or meter: the HPA cable runs straight to the switch
        assert e["switch_in_dbm"] == pytest.approx(out - 0.2)
        assert e["eirp_dbm"] == pytest.approx(out - 0.2 - 0.6 - 0.5 + 2)
        assert e["eirp_dbw"] == pytest.approx(e["eirp_dbm"] - 30)
        assert e["flags"] == []
        assert "meter_dbm" not in e and "coupler_in_dbm" not in e

    def test_hpa_overdrive_flag(self, prof):
        e = pb.eirp_chain(prof, 1500, tx_atten_db=0, backoff_db=0)
        assert e["hpa_in_dbm"] == pytest.approx(4.5)
        prof.comps["hpa"] = dict(prof.comps["hpa"], max_input_dbm={"value": 3})
        e = pb.eirp_chain(prof, 1500, 0, 0)
        assert any(f.startswith("HPA input") for f in e["flags"])

    def test_switch_flag_at_saturation(self, prof):
        e = pb.eirp_chain(prof, 1500, 0, 0)                 # deep saturation ~51 dBm
        assert any("switch TX input" in f for f in e["flags"])   # 51 - 0.2 = 50.8 > 50

    def test_monotonic_sweep(self, prof):
        rows = pb.eirp_sweep(prof, 1500, backoff_db=3)
        eirps = [r["eirp_dbm"] for r in rows]
        assert all(a >= b for a, b in zip(eirps, eirps[1:]))
        assert max(eirps) <= 51 - 0.2 - 0.6 - 0.5 + 2 + 1e-9

    def test_hpa_off_needs_feedthrough(self, prof):
        with pytest.raises(pb.ProfileError, match="off_gain_db"):
            pb.eirp_chain(prof, 1500, 10, 0, hpa_on=False)
        prof.comps["hpa"] = dict(prof.comps["hpa"], off_gain_db={"value": -40})
        e = pb.eirp_chain(prof, 1500, 10, 0, hpa_on=False)
        assert e["hpa_out_dbm"] == pytest.approx(5 - 10 - 0.5 - 40)


# saturation onset
class TestSaturation:
    def test_p1db_input_matches_datasheet(self, prof):
        e = pb.eirp_chain(prof, 1500, 20, 3)
        # Rapp fitted to P1dB 47 / Psat 51: input P1dB = P1dB + 1 - gain
        assert e["hpa_in_p1db_dbm"] == pytest.approx(47 + 1 - 55, abs=0.02)
        assert e["hpa_in_3db_dbm"] > e["hpa_in_p1db_dbm"]
        assert e["hpa_backoff_p1db_db"] == pytest.approx(e["hpa_in_p1db_dbm"] - e["hpa_in_dbm"])

    def test_tx_atten_at_onset_reproduces_compression(self, prof):
        e = pb.eirp_chain(prof, 1500, 20, 3)
        at1 = pb.eirp_chain(prof, 1500, e["tx_atten_p1db_db"], 3)
        at3 = pb.eirp_chain(prof, 1500, e["tx_atten_3db_db"], 3)
        assert at1["hpa_compression_db"] == pytest.approx(1.0, abs=0.01)
        assert at3["hpa_compression_db"] == pytest.approx(3.0, abs=0.01)

    def test_regions(self, prof):
        assert pb.hpa_region(0.05) == "linear" and pb.hpa_region(0.5) == "compressing"
        assert pb.hpa_region(1.5) == "past P1dB" and pb.hpa_region(4) == "SATURATED"
        assert pb.eirp_chain(prof, 1500, 40, 3)["hpa_region"] == "linear"
        e = pb.eirp_chain(prof, 1500, 0, 0)
        assert e["hpa_region"] == "SATURATED" and e["saturation_notes"]

    def test_link_headroom(self, prof):
        prof.comps["lna"] = dict(prof.comps["lna"], p1db_out_dbm={"value": 18})
        prof.comps["g40_rx"] = dict(prof.comps["g40_rx"], max_operating_input_dbm={"value": -25})
        r = pb.rx_link_budget(prof, 1500, 10, 1.0, 0, 1e5, 1.35e5, 2, 0.5, 4.0)
        assert r["lna_headroom_db"] == pytest.approx(18 - r["lna_out_dbm"])
        assert r["g40_headroom_db"] == pytest.approx(-25 - r["g40_in_dbm"])
        m = min(r["lna_headroom_db"], r["g40_headroom_db"])
        assert r["strongest_pr_ant_dbm"] == pytest.approx(r["pr_ant_dbm"] + m)
        assert r["closest_range_km"] == pytest.approx(1.0 * 10 ** (-m / 20))
        # at the closest range the first stage sits exactly at its limit
        r2 = pb.rx_link_budget(prof, 1500, 10, r["closest_range_km"], 0, 1e5, 1.35e5, 2, 0.5, 4.0)
        assert min(r2["lna_headroom_db"], r2["g40_headroom_db"]) == pytest.approx(0, abs=1e-6)

    def test_lna_saturation_note_with_attenuator_after(self):
        # with the attenuator after the LNA, a leak that drives the LNA past its
        # P1dB but stays under its damage level is a note (RX blind while keyed),
        # not a warning. P2b with a 60 dB switch is that case.
        data = copy.deepcopy(pb.load_profiles(REAL))
        sw = data["components"][data["profiles"]["P2b"]["switch"]]
        sw["isolation_db"] = {"value": 60, "unit": "dB"}
        for att in (0.0, 60.0):
            s = pb.live_summary(data, "P2b", 800, 0, 0, True, att)
            assert s["saturation"]["hpa_region"] == "SATURATED"
            assert any("LNA saturates during TX" in n for n in s["notes"])
            assert not any("LNA input in TX" in w for w in s["warnings"])


# RX protection
class TestProtection:
    def test_leak_by_hand(self, prof):
        # order: switch RX port then cable 0.2 then LNA (+20) then cable 0.2 then atten (IL 5 + setting) then cable 0.3 then G40
        z = pb.rx_leak_chain(prof, 2200, switch_in_dbm=50.0, rx_atten_db=10)
        assert z["switch_isolation_db"] == 60                      # iso above 2 GHz
        assert z["lna_in_dbm"] == pytest.approx(50 - 60 - 0.2)
        assert z["atten_in_dbm"] == pytest.approx(50 - 60 - 0.2 + 20 - 0.2)
        assert z["g40_in_dbm"] == pytest.approx(50 - 60 - 0.2 + 20 - 0.2 - 5 - 10 - 0.3)
        assert z["lna_ok"] is True                                 # well under 0 dBm
        assert z["atten_ok"] and z["g40_ok"]

    def test_only_the_g40_sets_the_minimum(self, prof):
        m = pb.min_rx_atten_at(prof, 2200, 50.0, margin_db=10)
        lna_in = 50 - 60 - 0.2                                     # no attenuator ahead of it
        g40_in0 = lna_in + 20 - 0.2 - 5 - 0.3                      # at 0 dB setting
        need_g40 = g40_in0 - (20 - 10)                             # G40 max safe - margin
        assert m["need_lna_db"] == pytest.approx(lna_in - 0)       # LNA max 0 dBm
        assert m["need_g40_db"] == pytest.approx(need_g40)
        assert m["min_rx_atten_db"] == pb.ceil_step(max(need_g40, 0), 0.05)
        assert m["binding"] == "G40"

    def test_attenuator_cannot_protect_the_lna(self, prof):
        # a very low LNA max input: the leak is over it at any setting, so the
        # minimum still follows the G40 and the LNA is reported unprotected
        prof.comps["lna"] = dict(prof.comps["lna"], max_input_dbm={"value": -30})
        m = pb.min_rx_atten_at(prof, 2200, 60.0, margin_db=10)
        assert m["binding"] == "G40" and m["need_lna_db"] > 0
        assert m["lna_ok"] is False
        assert m["min_rx_atten_db"] == pb.ceil_step(max(m["need_g40_db"], 0), 0.05)
        z = pb.rx_leak_chain(prof, 2200, 60.0, 120.0)
        assert z["lna_ok"] is False and z["g40_ok"] is True

    def test_worst_case_over_band(self, prof):
        prof.comps["lna"] = dict(prof.comps["lna"], gain_db={"value": 40})
        ms = pb.min_safe_rx_atten(FIX, prof)
        # worst = lowest isolation (60 dB above 2 GHz); HPA Psat 51, no coupler
        sw = 51 - 0.2
        g40 = sw - 60 - 0.2 + 40 - 0.2 - 5 - 0.3
        assert ms["min_rx_atten_db"] == pytest.approx(pb.ceil_step(g40 - (20 - 10), 0.05))
        assert 2001 <= ms["worst_freq_mhz"] <= 2500
        assert ms["achievable"] and ms["binding"] == "G40"

    def test_floor_applies(self, prof):
        d = dict(FIX, protection={"margin_db": {"value": 10}, "min_rx_atten_db": {"value": 30}})
        assert pb.min_safe_rx_atten(d, prof)["min_rx_atten_db"] == 30

    def test_lna_unprotected_whatever_the_range(self, prof):
        # LNA leak over its max: the attenuator is after it, so even its full
        # 120 dB range does not help; the band check reports it and the page warns
        prof.comps["lna"] = dict(prof.comps["lna"], max_input_dbm={"value": -30})
        ms = pb.min_safe_rx_atten(FIX, prof)
        assert ms["lna_unprotected_mhz"] and ms["achievable"]

    def test_lna_unprotected_warning_on_the_page(self):
        data = {**FIX, "components": {**FIX["components"],
                                      "lna": dict(FIX["components"]["lna"], max_input_dbm={"value": -30})}}
        s = pb.live_summary(data, "PX", 2200, 0, 0, True, 120.0)
        assert any("LNA input in TX" in w and "after the LNA" in w for w in s["warnings"])
        assert any("only more switch isolation or a limiter" in w for w in s["warnings"])

    def test_protection_freqs_include_breakpoints(self, prof):
        fs = pb.protection_freqs(FIX, prof)
        assert 1000 in fs and 2500 in fs and 2000 in fs and 2001 in fs and 800 not in fs


# bench loopback
class TestBenchLoopback:
    def test_expected_levels(self, prof):
        bl = pb.bench_loopback_expected(FIX, prof, 1500, tx_atten_db=40, backoff_db=6, pad_db=20)
        e = pb.eirp_chain(prof, 1500, 40, 6, hpa_on=True)
        assert bl["hpa_out_dbm"] == pytest.approx(e["hpa_out_dbm"])
        assert bl["meter_dbm"] == pytest.approx(e["hpa_out_dbm"] - 30)       # coupler coupling 30 dB
        assert bl["rx_level_dbm"] == pytest.approx(e["hpa_out_dbm"] - 0.1 - 20)  # mainline 0.1 + pad 20

    def test_no_fixture_gives_no_meter(self, prof):
        d = {k: v for k, v in FIX.items() if k != "bench_fixture"}
        bl = pb.bench_loopback_expected(d, prof, 1500)
        assert bl["meter_dbm"] is None


# link budget
class TestLink:
    def test_budget_by_hand(self, prof):
        r = pb.rx_link_budget(prof, 1500, far_eirp_dbw=10, range_km=100, rx_atten_db=0,
                              symbol_rate_hz=1e5, noise_bw_hz=1.35e5, bits_per_symbol=2,
                              code_rate=0.5, required_esn0_db=4.0, pol_loss_db=3, impl_loss_db=2)
        L = pb.fspl_db(100, 1500)
        assert r["pr_ant_dbm"] == pytest.approx(40 - L - 3 + 2)
        stages = pb.rx_cascade(prof, 1500, 0)
        assert [s["stage"] for s in stages][:4] == [
            "cable antenna->switch", "switch RX path", "cable switch->LNA", "LNA"]
        nf = pb.friis_nf_db(stages)
        tsys = 290 + 290 * (10 ** (nf / 10) - 1)
        n0 = -198.6 + 10 * math.log10(tsys)
        assert r["cn0_dbhz"] == pytest.approx(r["pr_ant_dbm"] - n0)
        assert r["esn0_db"] == pytest.approx(r["cn0_dbhz"] - 50)
        assert r["ebn0_db"] == pytest.approx(r["esn0_db"])            # 2 * 1/2 = 1
        assert r["margin_db"] == pytest.approx(r["esn0_db"] - 2 - 4)
        assert r["max_range_km"] == pytest.approx(100 * 10 ** (r["margin_db"] / 20))

    def test_rx_atten_raises_nf(self, prof):
        a = pb.friis_nf_db(pb.rx_cascade(prof, 1500, 0))
        b = pb.friis_nf_db(pb.rx_cascade(prof, 1500, 30))
        assert b > a + 5

    def test_required_lookup(self):
        assert pb.required_esn0(FIX, "QPSK", "1/2")["esn0_db"] == 4.0
        assert pb.required_esn0(FIX, "QPSK", "7/8") is None


# real file
class TestRealProfiles:
    @pytest.fixture
    def data(self):
        return pb.load_profiles(REAL)

    def test_profiles_present(self, data):
        assert set(pb.profile_names(data)) == {"P2", "P2a", "P2b", "P1", "P3"}
        p = {n: pb.profile(data, n) for n in pb.profile_names(data)}
        assert p["P2"].raw["hpa"] == "hpa_1189_bench" and p["P1"].raw["hpa"] == "hpa_1208"
        assert p["P3"].raw["hpa"] == "hpa_1211"
        # P2 as built 2026-11-03 (TestP2AsBuilt has the detail): dBWave B switch and
        # LNA, the 3-way ZB3PD on the RX side
        assert p["P2"].raw["lna"] == "lna_palna200101000a" and p["P1"].raw["lna"] == "lna_zkl_33uln"
        assert p["P2"].raw["switch"] == "switch_pasr0200500600b"
        assert p["P2"].raw["rx_splitter"] == "splitter_zb3pd_63"
        # P2b: the same parts with the ZB3PD as a TX combiner on three G40 TX ports
        # (TRx1A, TRx2A, TRx1B) and TRx2B the only RX port (bench setup 2026-11-02)
        assert p["P2b"].raw["hpa"] == "hpa_1189" and p["P2b"].raw["lna"] == "lna_palna200101000a"
        assert p["P2b"].raw["tx_combiner"] == "splitter_zb3pd_63"
        assert p["P2b"].raw["g40_ports"] == {"tx": ["TRx1A", "TRx2A", "TRx1B"], "rx": ["TRx2B"]}
        assert all("tx_combiner" not in p[n].comps for n in p if n != "P2b")
        assert all("rx_splitter" not in p[n].comps for n in p if n not in ("P2", "P2a"))
        # P2a: the 02Nov26 parts with the RX attenuator before the LNA.  Not the same
        # as P2: the A switch, no TX pad, datasheet G40 and HPA
        assert p["P2a"].raw["rx_order"] == "atten_first"
        assert p["P2a"].raw["switch"] == "switch_pasr0200500600a" and p["P2a"].fixed_pad_db == 0
        assert p["P2a"].raw["g40_tx"] == "g40_tx" and p["P2a"].raw["hpa"] == "hpa_1189"
        assert p["P2b"].raw["switch"] == "switch_pasr0200500600a" and p["P2b"].fixed_pad_db == 0
        # no profile references the bench coupler / power meter
        for n in pb.profile_names(data):
            raw = data["profiles"][n]
            assert "coupler" not in raw and "power_meter" not in raw
            assert set(raw["cables_db"]) == set(pb.CABLE_KEYS)
        # the coupler and power meter stay in components as the bench fixture
        assert data["bench_fixture"]["coupler"] in data["components"]
        assert data["bench_fixture"]["power_meter"] in data["components"]

    def test_every_number_has_a_source(self, data):
        missing = []
        for cname, comp in data["components"].items():
            for k, leaf in comp.items():
                if isinstance(leaf, dict) and ("value" in leaf or "points" in leaf):
                    if leaf.get("value") is None and "points" not in leaf:
                        continue
                    if not leaf.get("source"):
                        missing.append(f"{cname}.{k}")
                    if "verify" not in leaf:
                        missing.append(f"{cname}.{k} (no verify flag)")
        assert missing == []

    def test_bands_and_math_over_band(self, data):
        for n in pb.profile_names(data):
            prof = pb.profile(data, n)
            lo, hi = pb.band_mhz(data, prof)
            assert 700 <= lo < hi <= 3000
            for f in (lo, (lo + hi) / 2, hi):
                e = pb.eirp_chain(prof, f, 20, 3)
                assert 20 < e["eirp_dbm"] < 55
                assert e["hpa_out_dbm"] <= e["hpa_psat_dbm"] + 1e-9
            ms = pb.min_safe_rx_atten(data, prof)
            assert ms["achievable"] and 0 <= ms["min_rx_atten_db"] <= 120
            if n == "P2b":
                continue           # known: see test_p2b_lna_unprotected
            assert not ms["lna_unprotected_mhz"], (n, ms["lna_worst_in_dbm"])

    def test_p2b_lna_unprotected(self, data):
        # Known design problem (LNA before the attenuator with the 40 dB switch): the
        # PASR0200500600A isolates only 40 dB and the PALNA200101000A survives -5 dBm,
        # so at the HPA's Psat the LNA sees about +10.6 dBm in TX. The budget must keep
        # reporting it for that order; P2 as built moved the attenuator in front.
        ms = pb.min_safe_rx_atten(data, pb.profile(data, "P2b"))
        assert ms["lna_unprotected_mhz"] and ms["lna_worst_in_dbm"] > ms["lna_max_input_dbm"]
        s = pb.live_summary(data, "P2b", 800, 0, 0, True, 0.0)
        assert any("LNA input in TX" in w for w in s["warnings"])

    def test_p2a_switch_rating_flag(self, data):
        # the A switch takes 47 dBm CW; the 1189 can drive it past that
        e = pb.eirp_chain(pb.profile(data, "P2a"), 2200, 0, 0)
        assert any("switch TX input" in f for f in e["flags"])
        e = pb.eirp_chain(pb.profile(data, "P2a"), 2200, 10, 3)
        assert not e["flags"]
        # and it has a rating on file, so no warning about a missing one
        s = pb.live_summary(data, "P2a", 2200, 10, 3, True, 30.0)
        assert not any("CW rating" in w for w in s["warnings"])

    def test_p2_one_tx_port_no_combiner(self, data):
        prof = pb.profile(data, "P2")
        e = pb.eirp_chain(prof, 2200, 10, 3)
        # G40 +20 dBm (measured) - 10 atten - 3 backoff - 0.2 cable - 20 dB pad
        assert e["tx_combiner_db"] is None and e["fixed_pad_db"] == 20
        assert e["hpa_in_dbm"] == pytest.approx(20 - 10 - 3 - 0.2 - 20, abs=1e-3)

    def test_p2_rx_splitter_in_rx_path(self, data):
        prof = pb.profile(data, "P2")
        # splitter loss 5.15 + (5.26-5.15)*0.4 at 2200 MHz, after the LNA
        assert prof.rx_splitter_db(2200) == pytest.approx(5.194, abs=1e-3)
        z = pb.rx_leak_chain(prof, 2200, 50.0, 10.0)
        assert z["rx_splitter_db"] == pytest.approx(5.194, abs=1e-3)
        assert z["g40_in_dbm"] == pytest.approx(z["lna_out_dbm"] - 5.194 - prof.cables["atten_to_g40"], abs=1e-3)
        names = [st["stage"] for st in pb.rx_cascade(prof, 2200, 10.0)]
        assert names.index("RX attenuator") < names.index("LNA") < names.index("RX splitter")
        assert names.index("RX splitter") == names.index("LNA") + 1
        assert "RX splitter" not in [st["stage"] for st in pb.rx_cascade(pb.profile(data, "P1"), 2200, 10.0)]

    def test_p2b_combiner_in_tx_path(self, data):
        prof = pb.profile(data, "P2b")
        # combiner loss 5.15 + (5.26-5.15)*0.4 at 2200 MHz, between the G40 TX and the HPA
        assert prof.tx_combiner_db(2200) == pytest.approx(5.194, abs=1e-3)
        e = pb.eirp_chain(prof, 2200, 10, 3)
        assert e["tx_combiner_db"] == pytest.approx(5.194, abs=1e-3)
        assert e["hpa_in_dbm"] == pytest.approx(5 - 10 - 3 - 0.4 - 5.194, abs=1e-3)
        # no splitter on the RX side: the attenuator feeds TRx2B directly
        z = pb.rx_leak_chain(prof, 2200, 50.0, 10.0)
        assert z["rx_splitter_db"] is None
        assert "RX splitter" not in [st["stage"] for st in pb.rx_cascade(prof, 2200, 10.0)]
        # the LNA ahead of the attenuator, so it is unprotected in TX here
        ms = pb.min_safe_rx_atten(data, prof)
        assert ms["lna_unprotected_mhz"]
        assert pb.min_lna_safe_tx_atten(data, prof)["needed"]

    def test_coupled_port_expected(self, data):
        # bench: HPA into the ZGDC30-372HP+ and a dummy load, analyzer on the coupled port
        prof = pb.profile(data, "P2b")
        r = pb.coupled_port_expected(data, prof, 2200, 10, 0)
        e = pb.eirp_chain(prof, 2200, 10, 0)
        # coupling 30.5 + (30.3-30.5)*0.4 at 2200 MHz
        assert r["coupling_db"] == pytest.approx(30.42, abs=1e-3)
        assert r["coupled_dbm"] == pytest.approx(e["hpa_out_dbm"] - 30.42, abs=1e-3)
        assert r["hpa_in_ok"] and r["hpa_in_dbm"] == e["hpa_in_dbm"]
        r2 = pb.coupled_port_expected(data, prof, 2200, 10, 0, extra_loss_db=10.0)
        assert r2["coupled_dbm"] == pytest.approx(r["coupled_dbm"] - 10.0, abs=1e-9)
        with pytest.raises(pb.ProfileError):
            pb.coupled_port_expected(data, prof, 2200, 10, 0, coupler="no_such_part")

    def test_lna_tx_floor(self, data):
        # P2b: 40 dB switch isolation against a -5 dBm LNA, so only TX attenuation
        # keeps the leak under the LNA max less the 10 dB margin
        r = pb.min_lna_safe_tx_atten(data, pb.profile(data, "P2b"))
        assert r["needed"] and r["achievable"]
        assert r["min_tx_atten_db"] == 31.5 and r["worst_freq_mhz"] == 1600.0
        prof = pb.profile(data, "P2b")
        e = pb.eirp_chain(prof, 1600, 31.5, 0.0)
        assert pb.rx_leak_chain(prof, 1600, e["switch_in_dbm"], 0.0)["lna_in_dbm"] <= -15.0
        e = pb.eirp_chain(prof, 1600, 31.25, 0.0)
        assert pb.rx_leak_chain(prof, 1600, e["switch_in_dbm"], 0.0)["lna_in_dbm"] > -15.0
        # P3's switch isolation is enough on its own
        assert not pb.min_lna_safe_tx_atten(data, pb.profile(data, "P3"))["needed"]

    def test_known_datasheet_values(self, data):
        c = data["components"]
        assert c["hpa_1211"]["max_input_dbm"]["value"] == 3
        assert c["hpa_1189"]["max_input_dbm"]["value"] == 10
        assert c["switch_pe71s2041"]["power_rating_dbm"]["value"] == pytest.approx(50.0)
        assert c["hpa_1211"]["p1db_dbm"] is None                       # not published: null

    def test_required_rows_have_fec_rates(self, data):
        rows = data["required_esn0"]["rows"]
        fsk = {r["fec"]: r["esn0_db"] for r in rows if r["mod"] == "FSK (DPMG measured edge)"}
        assert fsk == {"none": 8.5, "1/2": 4.0, "2/3": 5.5, "3/4": 5.0, "5/6": 6.5, "7/8": 6.5}

    def test_live_summary(self, data):
        s = pb.live_summary(data, "P2", 2200, 10, 3, False, 120.0)
        assert s["eirp"]["eirp_dbm"] > 30 and s["leak"]["g40_ok"] and "warnings" in s
        s = pb.live_summary(data, "P3", 2200, 0, 0, True, 0.0)
        assert any("switch" in w for w in s["warnings"])
        assert any("HPA input" in w for w in s["warnings"])            # 1211 max +3 dBm


# page renderer
class TestP2AsBuilt:
    """Payload 2 as rewired 2026-11-03: TRx2B through a 20 dB pad to the 1189, switch
    J2 and J0 to the antenna; J0 and J1 to the attenuator, LNA and ZB3PD into TRx1A,
    TRx1B and TRx2A; dBWave B switch; bench numbers for the G40 and the HPA."""

    @pytest.fixture
    def data(self):
        return pb.load_profiles(REAL)

    @pytest.fixture
    def p2(self, data):
        return pb.profile(data, "P2")

    def test_cabling_record(self, p2):
        assert p2.raw["g40_ports"] == {"tx": ["TRx2B"], "rx": ["TRx1A", "TRx1B", "TRx2A"]}
        assert p2.raw["switch_ports"] == {"antenna": "J0", "rx": "J1", "tx": "J2"}
        assert p2.atten_first and p2.fixed_pad_db == 20
        assert "tx_combiner" not in p2.comps

    def test_chain_in_words(self, p2):
        c = pb.chain_text(p2)
        assert c["tx"].startswith("G40 TRx2B, 20 dB pad, Empower RF 1189")
        assert c["tx"].endswith("switch J2, switch J0, antenna")
        assert c["rx"].startswith("antenna, switch J0, switch J1, Mini-Circuits RCDAT-6G-120H")
        assert c["rx"].index("RCDAT") < c["rx"].index("PALNA") < c["rx"].index("ZB3PD")
        assert c["rx"].endswith("G40 TRx1A / TRx1B / TRx2A")

    def test_pad_sets_the_hpa_input(self, p2):
        e = pb.eirp_chain(p2, 2200, 10, 3)
        assert e["hpa_in_dbm"] == pytest.approx(e["g40_out_dbm"] - 0.2 - 20.0)
        # no overdrive even at full scale and 0 dB, thanks to the pad
        e0 = pb.eirp_chain(p2, 2200, 0, 0)
        assert e0["hpa_in_dbm"] == pytest.approx(-0.2) and e0["hpa_in_dbm"] < e0["hpa_max_input_dbm"]
        assert not any("HPA input" in f for f in e0["flags"])

    def test_measured_values_labelled(self, data, p2):
        assert pb.measured_values(p2) == ["G40 TX output", "HPA gain", "HPA Psat"]
        assert p2.comp("g40_tx")["max_output_dbm"]["value"] == 20
        assert p2.get("hpa", "gain_db", 915) == pytest.approx(58.9)
        assert p2.get("hpa", "gain_db", 2200) == pytest.approx(55.3)
        assert p2.get("hpa", "psat_dbm") == pytest.approx(51.6)
        for n in ("P1", "P2a", "P2b", "P3"):
            assert pb.measured_values(pb.profile(data, n)) == []
        s = pb.live_summary(data, "P2", 2200, 10, 3, True, 30.0)
        assert any("bench values" in n and "HPA Psat" in n for n in s["notes"])
        # the datasheet parts the other profiles use are untouched
        assert data["components"]["g40_tx"]["max_output_dbm"]["value"] == 5
        assert data["components"]["hpa_1189"]["psat_dbm"]["value"] == 51.0

    def test_apply_g40_max_reaches_the_bench_part(self, data):
        d = pb.apply_g40_max(copy.deepcopy(data), 13.6)
        assert pb.eirp_chain(pb.profile(d, "P2"), 2200, 0, 0)["g40_out_dbm"] == pytest.approx(13.6)
        assert pb.eirp_chain(pb.profile(d, "P2b"), 2200, 0, 0)["g40_out_dbm"] == pytest.approx(13.6)

    def test_switch_is_the_b_part(self, data, p2):
        sw = data["components"]["switch_pasr0200500600b"]
        assert sw["datasheet"] == "docs/datasheets/PASR0200500600B.pdf"
        assert os.path.exists(os.path.join(HERE, sw["datasheet"]))
        assert p2.get("switch", "insertion_loss_db", 2200) == pytest.approx(1.0)
        assert p2.get("switch", "isolation_db", 2200) == pytest.approx(40.0)
        assert p2.get("switch", "isolation_db", 4000) == pytest.approx(35.0)
        assert sw["freq_range_mhz"]["value"] == [500, 6000]
        # no CW rating invented: null, with the pulsed figure on its own key
        assert sw["power_rating_dbm"] is None and sw["hot_switch_rating_dbm"] is None
        assert sw["pulsed_rating_dbm"]["value"] == 50

    def test_missing_cw_rating_flagged(self, data, p2):
        e = pb.eirp_chain(p2, 2200, 20, 3)
        assert e["switch_max_dbm"] is None and e["switch_pulsed_max_dbm"] == 50 and not e["flags"]
        s = pb.live_summary(data, "P2", 2200, 20, 3, True, 30.0)
        assert any("switch CW rating unknown" in w and "50 dBm pulsed" in w for w in s["warnings"])
        # past the only rating there is, it is an over-limit flag as well
        e0 = pb.eirp_chain(p2, 2200, 0, 0)
        assert any("pulsed rating" in f for f in e0["flags"])

    def test_guard_numbers(self, data, p2):
        ms = pb.min_safe_rx_atten(data, p2)
        assert ms["min_rx_atten_db"] == 23.95 and ms["worst_freq_mhz"] == 700.0
        assert ms["binding"] == "G40" and not ms["lna_unprotected_mhz"]
        lf = pb.min_lna_safe_tx_atten(data, p2)
        assert not lf["needed"] and lf["min_tx_atten_db"] == 0.0
        # at the minimum setting the LNA, the attenuator and the G40 all clear with
        # the margin, across the band, with the HPA at the measured Psat
        for r in ms["rows"]:
            z = pb.rx_leak_chain(p2, r["freq_mhz"], r["switch_in_dbm"], ms["min_rx_atten_db"])
            assert z["lna_in_dbm"] <= z["lna_max_input_dbm"] - ms["margin_db"] + 1e-6
            assert z["g40_in_dbm"] <= z["g40_max_safe_dbm"] - ms["margin_db"] + 1e-6
            assert z["atten_ok"]

    def test_other_profiles_keep_their_guard_numbers(self, data):
        got = {n: pb.min_safe_rx_atten(data, pb.profile(data, n))["min_rx_atten_db"] for n in ("P1", "P2a", "P2b", "P3")}
        assert got == {"P1": 6.2, "P2a": 23.35, "P2b": 28.3, "P3": 1.25}


class TestAttenFirst:
    """P2a: the 02Nov26 parts with the RX attenuator before the LNA."""

    @pytest.fixture
    def data(self):
        return pb.load_profiles(REAL)

    def test_order_flag(self, data):
        assert pb.profile(data, "P2a").atten_first and not pb.profile(data, "P2b").atten_first
        bad = copy.deepcopy(data)
        bad["profiles"]["P2a"]["rx_order"] = "sideways"
        with pytest.raises(pb.ProfileError, match="rx_order"):
            pb.validate(bad)

    def test_leak_path_math(self, data):
        p = pb.profile(data, "P2a")
        c = p.cables
        z = pb.rx_leak_chain(p, 1600, 50.0, 20.0)
        il = p.get("rx_atten", "insertion_loss_db", 1600)
        assert z["atten_in_dbm"] == pytest.approx(z["leak_switch_rx_dbm"] - c["switch_to_lna"])
        assert z["lna_in_dbm"] == pytest.approx(z["atten_in_dbm"] - il - 20.0 - c["lna_to_atten"])
        assert z["g40_in_dbm"] == pytest.approx(z["lna_out_dbm"] - p.rx_splitter_db(1600) - c["atten_to_g40"])
        # here the attenuator lowers the LNA input one for one
        z0 = pb.rx_leak_chain(p, 1600, 50.0, 0.0)
        assert z0["lna_in_dbm"] - z["lna_in_dbm"] == pytest.approx(20.0)

    def test_attenuator_protects_the_lna(self, data):
        ms2 = pb.min_safe_rx_atten(data, pb.profile(data, "P2b"))
        ms2a = pb.min_safe_rx_atten(data, pb.profile(data, "P2a"))
        assert ms2["lna_unprotected_mhz"] and not ms2a["lna_unprotected_mhz"]
        p = pb.profile(data, "P2a")
        for r in ms2a["rows"]:
            z = pb.rx_leak_chain(p, r["freq_mhz"], r["switch_in_dbm"], r["min_rx_atten_db"])
            assert z["lna_in_dbm"] <= z["lna_max_input_dbm"] - ms2a["margin_db"] + 1e-6
            assert z["g40_ok"] and z["atten_ok"]

    def test_no_tx_floor(self, data):
        assert pb.min_lna_safe_tx_atten(data, pb.profile(data, "P2b"))["needed"]
        assert not pb.min_lna_safe_tx_atten(data, pb.profile(data, "P2a"))["needed"]

    def test_cascade_order_and_nf_cost(self, data):
        st = pb.rx_cascade(pb.profile(data, "P2a"), 1600, 0)
        names = [x["stage"] for x in st]
        assert names.index("RX attenuator") < names.index("LNA") < names.index("RX splitter")
        after = copy.deepcopy(data)
        del after["profiles"]["P2a"]["rx_order"]         # the same parts, LNA first
        nf2 = pb.friis_nf_db(pb.rx_cascade(pb.profile(after, "P2a"), 1600, 0))
        nf2a = pb.friis_nf_db(st)
        assert 4.0 < nf2a - nf2 < 7.0          # the attenuator loss sits in front of the LNA here

    def test_summary_carries_the_order(self, data):
        s = pb.live_summary(data, "P2a", 1600, 30.0, 0.0, True, 30.0)
        assert s["rx_order"] == "atten_first" and not s["lna_tx_floor"]["needed"]


class TestDiagram:
    def test_svg_renders_states(self):
        pytest.importorskip("nicegui")
        from dpmg.web import payload_tab
        data = pb.load_profiles(REAL)
        calc = pb.live_summary(data, "P2", 2200, 10, 3, True, 120.0)
        st = {"pa": {"keyed": True, "level": True}, "tr": {"tx": True, "mode": "gpio"},
              "atten": {"att_db": 120.0}, "guard": {"min_db": 7.3, "saved_db": 20.0},
              "fault": {"enabled": False}}
        svg = payload_tab.diagram_svg(st, calc, "P2 <x>")
        assert svg.startswith("<svg") and svg.endswith("</svg>")
        assert "HPA KEYED" in svg and "T/R switch: TX" in svg and "radiating" in svg
        assert "P2 &lt;x&gt;" in svg                                   # escaped
        assert "saved 20.00 dB" in svg
        assert "coupler" not in svg.lower() and "power meter" not in svg.lower()
        svg2 = payload_tab.diagram_svg({}, None, "-")
        assert "HPA off" in svg2 and "EIRP if keyed" in svg2


# workbook
class TestWorkbook:
    def test_generate_structure(self, tmp_path):
        pytest.importorskip("openpyxl")
        sys.path.insert(0, os.path.join(HERE, "tools"))
        import gen_payload_workbook as gw
        out = str(tmp_path / "wb.xlsx")
        book = gw.generate(REAL, out)
        from openpyxl import load_workbook
        wb = load_workbook(out)
        assert wb.sheetnames == ["README", "Inputs", "EIRP", "RX Protection", "RX Link Budget",
                                 "Required EsN0", "Tables"]
        e = wb["EIRP"]
        assert str(e["B27"].value).startswith("=IF(ISNUMBER(B25)")       # EIRP dBm formula
        assert e["A36"].value.startswith("G40 TX attenuation where the HPA reaches P1dB")
        assert str(wb["RX Protection"][book.prot_min_cell].value).startswith("=MAX(")

    @pytest.mark.skipif(not (shutil.which("soffice") or shutil.which("libreoffice")),
                        reason="LibreOffice not installed")
    def test_formulas_match_python(self, tmp_path):
        pytest.importorskip("openpyxl")
        sys.path.insert(0, os.path.join(HERE, "tools"))
        import gen_payload_workbook as gw
        out = str(tmp_path / "test_wb.xlsx")
        book = gw.generate(REAL, out)
        assert gw.check(out, book.data, book) == []


class TestLoadAtten:
    def test_delivered_points(self, prof):
        e = pb.eirp_chain(prof, 1500, 10.0, 0.0, hpa_on=True)
        assert pb.tx_delivered_dbm(e, "hpa") == e["hpa_out_dbm"]
        assert pb.tx_delivered_dbm(e, "antenna") == e["ant_in_dbm"]
        assert pb.tx_delivered_dbm(e, "switch") == pytest.approx(e["switch_in_dbm"] - e["switch_il_db"])

    def test_min_tx_atten_keeps_load_under_rating(self, prof):
        r = pb.min_safe_tx_atten(FIX, prof, 33.0, margin_db=3)
        assert r["achievable"] and r["point"] == "antenna"
        a, f = r["min_tx_atten_db"], r["worst_freq_mhz"]
        # the chosen attenuation is the smallest that holds the load under rating-margin
        e = pb.eirp_chain(prof, f, a, 0.0, hpa_on=True)
        assert pb.tx_delivered_dbm(e, "antenna") <= 33.0 - 3 + 1e-6
        if a >= 0.25:
            e0 = pb.eirp_chain(prof, f, a - 0.25, 0.0, hpa_on=True)
            assert pb.tx_delivered_dbm(e0, "antenna") > 33.0 - 3

    def test_low_load_not_achievable_band_wide(self, prof):
        # a +20 dBm pad cannot hold the high-gain top of the band (HPA gain 60 dB
        # at 2000 MHz), so the guard would refuse to key: use a higher-power load
        r = pb.min_safe_tx_atten(FIX, prof, 20.0, margin_db=3)
        assert not r["achievable"] and 2000.0 in r["unprotected_mhz"]

    def test_unachievable_at_hpa_output(self, prof):
        # the HPA output barely moves near Psat, so a +10 dBm load is not reachable
        r = pb.min_safe_tx_atten(FIX, prof, 10.0, margin_db=3, point="hpa", atten_max_db=10.0)
        assert not r["achievable"] and r["min_tx_atten_db"] == 10.0 and r["unprotected_mhz"]

    def test_bad_point_raises(self, prof):
        with pytest.raises(pb.ProfileError, match="load point"):
            pb.min_safe_tx_atten(FIX, prof, 20.0, point="coupler")


# TX radios (G40 / B200)
class TestTxRadios:
    @pytest.fixture
    def data(self):
        return pb.load_profiles(REAL)

    # live_summary outputs taken before the radios: section was added
    # (profile, MHz, TX atten, backoff): eirp, HPA out, HPA in, atten at P1dB, min safe RX, G40 in
    G40_GOLDEN = [
        # P2 rows recaptured 2026-11-03: as built (20 dB pad, measured G40 and HPA, B
        # switch, attenuator first)
        (("P2", 2200, 10, 3), (40.0029, 41.9029, -13.2, 4.1, 23.95, -7.4511)),
        # P1 rows recaptured 2026-11-02: top drawing (ZKL-33ULN-S+ LNA, coupler in the TX path)
        (("P1", 1500, 30, 6), (26.4691, 28.9991, -31.2, 10.9999, 6.2, -45.7109)),
        (("P3", 900, 0, 0), (53.0194, 50.9994, 4.8, 18.9646, 1.25, -19.1206)),
        (("P2", 800, 41.75, 3), (14.95, 13.95, -44.95, 7.7, 23.95, -33.876)),
    ]

    @pytest.mark.parametrize("args,want", G40_GOLDEN)
    @pytest.mark.parametrize("radio", [None, "g40"])
    def test_g40_numbers_unchanged(self, data, args, want, radio):
        s = pb.live_summary(data, *args, True, 30.0, radio=radio)
        e = s["eirp"]
        got = (e["eirp_dbm"], e["hpa_out_dbm"], e["hpa_in_dbm"], e["tx_atten_p1db_db"],
               s["min_safe"]["min_rx_atten_db"], s["leak"]["g40_in_dbm"])
        assert got == pytest.approx(want, abs=1e-3)

    def test_g40_load_limit_unchanged(self, data):
        assert pb.min_safe_tx_atten(data, pb.profile(data, "P2"), 33.0)["min_tx_atten_db"] == 34.0

    def test_file_without_radios_is_g40_only(self, data):
        d = {k: v for k, v in data.items() if k != "radios"}
        pb.validate(d)
        assert pb.radio_names(d) == ["g40"]
        rt = pb.radio_tx(d, "g40")
        assert (rt.control, rt.max_db, rt.step_db, rt.tx) == ("atten", 41.75, 0.25, "g40_tx")
        s = pb.live_summary(d, "P2", 2200, 10, 3, True, 30.0)
        assert s["eirp"]["eirp_dbm"] == pytest.approx(40.0029, abs=1e-3)    # P2 2200 MHz, 10 dB, 3 dB
        with pytest.raises(pb.ProfileError):
            pb.radio_tx(d, "b200")

    def test_bad_radios_section(self, data):
        d = dict(data, radios={"x": {"tx": "nope"}})
        with pytest.raises(pb.ProfileError):
            pb.validate(d)
        d = dict(data, radios={"x": {"tx": "g40_tx", "control": "volume"}})
        with pytest.raises(pb.ProfileError):
            pb.validate(d)

    def test_b200_entry(self, data):
        rt = pb.radio_tx(data, "b200")
        assert (rt.control, rt.min_db, rt.max_db, rt.step_db) == ("gain", 0.0, 89.75, 0.25)
        assert rt.bench_max_gain_db == 45.0 and rt.label == "B200 TX gain"
        comp = data["components"]["b200_tx"]
        assert comp["max_output_dbm"]["value"] == 10 and comp["max_output_dbm"]["verify"] is True
        assert "ettus.com" in comp["max_output_dbm"]["source"]
        assert "MEASURE" in comp["max_output_dbm"]["note"]

    def test_b200_output_model(self, data):
        prof = pb.profile(data, "P2")
        rt = pb.radio_tx(data, "b200")
        for gain in (0.0, 45.0, 89.75):
            e = pb.eirp_chain_radio(data, prof, rt, 2200, gain, backoff_db=3.0)
            assert e["tx_out_dbm"] == pytest.approx(10.0 - (89.75 - gain) - 3.0)
            assert e["g40_out_dbm"] == e["tx_out_dbm"] and e["tx_gain_db"] == gain
            # the rest of the chain is the G40 chain fed the same power
            g = pb.eirp_chain(prof, 2200, 20.0 - 3.0 - e["tx_out_dbm"], 3.0)
            assert e["hpa_out_dbm"] == pytest.approx(g["hpa_out_dbm"])
            assert e["eirp_dbm"] == pytest.approx(g["eirp_dbm"])
        # gain at the HPA P1dB point drives the HPA input to its P1dB input
        e = pb.eirp_chain_radio(data, prof, rt, 2200, 0.0, backoff_db=3.0)
        e1 = pb.eirp_chain_radio(data, prof, rt, 2200, e["tx_gain_p1db_db"], backoff_db=3.0)
        assert e1["hpa_in_dbm"] == pytest.approx(e1["hpa_in_p1db_dbm"], abs=1e-6)
        assert e["tx_gain_3db_db"] > e["tx_gain_p1db_db"]

    def test_b200_live_summary(self, data):
        s = pb.live_summary(data, "P2", 2200, 40.0, 3, True, 30.0, radio="b200")
        assert s["tx"]["control"] == "gain" and s["eirp"]["tx_radio"] == "b200"
        assert "tx_gain_p1db_db" in s["saturation"]
        assert not any("bench limit" in w for w in s["warnings"])
        # the minimum safe RX attenuation is the profile's own (HPA at Psat)
        g = pb.live_summary(data, "P2", 2200, 10, 3, True, 30.0)
        assert s["min_safe"]["min_rx_atten_db"] == g["min_safe"]["min_rx_atten_db"]
        s = pb.live_summary(data, "P2", 2200, 45.25, 3, True, 30.0, radio="b200")
        assert any("above the bench limit 45 dB" in w for w in s["warnings"])
        s = pb.live_summary(data, "P2", 2200, 45.0, 3, True, 30.0, radio="b200")
        assert not any("bench limit" in w for w in s["warnings"])

    def test_max_safe_tx_gain(self, data):
        prof = pb.profile(data, "P2")
        rt = pb.radio_tx(data, "b200")
        r = pb.max_safe_tx_gain(data, prof, rt, 33.0)
        assert r["achievable"] and 0 <= r["max_tx_gain_db"] <= 89.75
        f = r["worst_freq_mhz"]
        target = 33.0 - r["margin_db"]
        ok = pb.eirp_chain_radio(data, prof, rt, f, r["max_tx_gain_db"])
        over = pb.eirp_chain_radio(data, prof, rt, f, r["max_tx_gain_db"] + 0.25)
        assert pb.tx_delivered_dbm(ok) <= target < pb.tx_delivered_dbm(over)
        with pytest.raises(pb.ProfileError):
            pb.max_safe_tx_gain(data, prof, pb.radio_tx(data, "g40"), 33.0)

    def test_g40_entry_keeps_the_profile_g40_part(self, data):
        # P2 names the bench G40 part, the radios entry the datasheet one: selecting
        # the G40 must not swap the part, and another radio must keep the RX order
        prof = pb.profile(data, "P2")
        assert pb.radio_profile(data, prof, pb.radio_tx(data, "g40")) is prof
        assert pb.radio_profile(data, prof, pb.radio_tx(data, "b200")).atten_first


if __name__ == "__main__":  # OFF-01 loop runs `python3 tests/<file>.py`; delegate to pytest
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
