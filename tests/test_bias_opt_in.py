"""test_bias_opt_in.py: the bench harness switches the 3.3 V GPS antenna bias on
only with --ant-bias, and every GUI shows who last switched it.

Covers: ensure_ant_bias refuses without consent and names the way out, passes
when the bias is already on, sends the enable (with the script as "by") when
--ant-bias was given; the --ant-bias flag sets the environment child stages
inherit; the service records the sender and publishes it in get_config; the GUI
text for every case.
"""
import argparse
import asyncio
import os
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "bench"))

import checkout_common as cc  # noqa: E402
from dpmg.link.proto import CmdStatus  # noqa: E402


class _Ack:
    def __init__(self, status, result):
        self.status, self.result = status, result


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(cc.ANT_BIAS_ENV, raising=False)


def _fake_radio(monkeypatch, bias_initial):
    state = {"bias": bias_initial, "sent": []}
    monkeypatch.setattr(cc, "gps_status", lambda link, timeout=10.0: (CmdStatus.OK, {"ant_bias": state["bias"]}))

    def safe_cmd(link, name, params, timeout=60.0):
        state["sent"].append((name, dict(params)))
        if name == "gps_antenna_bias":
            state["bias"] = params["enable"]
            return _Ack(CmdStatus.OK, {"ant_bias": state["bias"]})
        return _Ack(CmdStatus.OK, {})
    monkeypatch.setattr(cc, "safe_cmd", safe_cmd)
    return state


def test_refuses_without_consent(monkeypatch):
    st = _fake_radio(monkeypatch, False)
    with pytest.raises(cc.GpsLockError) as e:
        cc.ensure_ant_bias(None)
    assert "--ant-bias" in str(e.value) and "ACTIVE" in str(e.value)
    assert st["sent"] == []                              # nothing was switched


def test_already_on_needs_no_consent(monkeypatch):
    st = _fake_radio(monkeypatch, True)
    assert cc.ensure_ant_bias(None) == {"ant_bias": True, "enabled_now": False}
    assert st["sent"] == []


def test_enables_with_flag(monkeypatch):
    st = _fake_radio(monkeypatch, False)
    ap = argparse.ArgumentParser()
    cc.add_clock_args(ap)
    args = ap.parse_args(["--clock", "gpsdo", "--ant-bias"])
    assert args.ant_bias is True and os.environ.get(cc.ANT_BIAS_ENV) == "1"
    assert cc.ensure_ant_bias(None) == {"ant_bias": True, "enabled_now": True}
    name, params = st["sent"][0]
    assert name == "gps_antenna_bias" and params["enable"] is True
    assert params["by"].startswith("bench ")


def test_flag_absent_leaves_env_unset():
    ap = argparse.ArgumentParser()
    cc.add_clock_args(ap)
    args = ap.parse_args(["--clock", "gpsdo"])
    assert args.ant_bias is False and not cc.ant_bias_allowed()


def test_service_records_sender():
    from dpmg.radio import dpmg_service

    class _Sim:
        def set_bias(self, en):
            return {"ok": True, "ant_bias": en}

    svc = dpmg_service.RadioService.__new__(dpmg_service.RadioService)
    svc._gps_sim, svc._gps_real = _Sim(), False
    ack = asyncio.run(svc._cmd_gps_antenna_bias(1, {"enable": True, "by": "GUI GCS-X86"}))
    assert ack.status == CmdStatus.OK
    assert svc._ant_bias_by["by"] == "GUI GCS-X86" and svc._ant_bias_by["enable"] is True
    asyncio.run(svc._cmd_gps_antenna_bias(2, {"enable": False}))
    assert svc._ant_bias_by["by"] == "unnamed client" and svc._ant_bias_by["enable"] is False


def test_gui_who_text():
    pytest.importorskip("nicegui")
    from dpmg.web import link_tab
    assert link_tab._bias_who_text(True, None) == ""                    # no config yet
    assert link_tab._bias_who_text(True, {}) == ""                      # older service
    assert "before this radio service started" in link_tab._bias_who_text(True, {"ant_bias_by": None})
    assert link_tab._bias_who_text(False, {"ant_bias_by": None}) == ""
    t = link_tab._bias_who_text(True, {"ant_bias_by": {"by": "bench first_checkout.py",
                                                       "enable": True, "at": "2026-10-19 22:40"}})
    assert t == "turned ON by bench first_checkout.py, 2026-10-19 22:40"


if __name__ == "__main__":  # OFF-01 loop runs `python3 tests/<file>.py`; delegate to pytest
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
