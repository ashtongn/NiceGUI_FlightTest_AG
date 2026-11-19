"""A get_config too long for one datagram during TX must keep the safety and health
fields: the old fallback dropped every structured field, ports and hpa_monitor with them."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dpmg.link import proto
from dpmg.link.proto import CmdAck, CmdStatus
from dpmg.radio import dpmg_service as svc


def snapshot(**extra):
    d = {"state": 2, "tx_active": True, "tx_gain": 25.0, "center_hz": 915000000.0,
         "ports": {"TRx2B": "tx", "TRx1A": "rx", "TRx1B": "rx", "TRx2A": "rx"},
         "hpa_monitor": {"state": "ok", "hpa_temp_c": 35.0, "hpa_current_a": 1.5, "hot": False, "quality": "ok"},
         "baro": {"state": "ok", "pressure_hpa": 930.1, "temp_c": 24.0, "alt_m": 730.0},
         "secure": {"on": False, "note": "x" * 300},
         "log": ["y" * 60] * 5,
         "tx_wf": {"kind": "modulated", "mod": "qpsk", "note": "z" * 200},
         "psd": {"nfft": 4096, "window": "hann"}}
    d.update(extra)
    return d


def fits(result):
    try:
        proto.encode(CmdAck(1, CmdStatus.OK, result), 0)
        return True
    except proto.ProtoError:
        return False


def test_drops_extras_first_and_keeps_safety_fields():
    full = snapshot(padding={"a": "w" * 900})
    assert not fits(full)
    small = svc.trim_ack_result(1, CmdStatus.OK, full)
    assert fits(small) and small["truncated"] is True
    assert "secure" in small["dropped_keys"]
    for k in ("ports", "hpa_monitor", "baro", "state", "tx_active", "tx_gain"):
        assert k in small and small[k] == full[k]


def test_only_as_much_as_needed_is_dropped():
    full = snapshot(padding={"a": "w" * 500})          # dropping secure and log is enough
    small = svc.trim_ack_result(1, CmdStatus.OK, full)
    assert fits(small) and "tx_wf" in small and "padding" in small


def test_safety_fields_go_last_and_it_always_fits():
    full = {"ports": {"j": "p" * 900}, "guard": {"k": "q" * 900}, "state": 2}
    small = svc.trim_ack_result(1, CmdStatus.OK, full)
    assert fits(small) and small["truncated"] is True and "guard" in small["dropped_keys"] + list(small)


def test_a_repeated_trim_logs_once(caplog):
    svc._ack_trim_logged = None
    full = snapshot(padding={"a": "w" * 500})
    for _ in range(3):
        svc.trim_ack_result(1, CmdStatus.OK, full)
    assert sum("ack trimmed" in r.message for r in caplog.records) == 1


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
