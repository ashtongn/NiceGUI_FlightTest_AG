"""Saved Connect by IP addresses (link_client LinkManager.saved / pending).

A radio added by IP is saved, dialled again at every GUI start, and kept out of
`hosts` (no radio card) until its service answers. Every MANUAL_REHELLO_S it
gets one more HELLO, so a service that starts later (USB hot-plug on the Super
Nano) shows up by itself. No sockets here: _send is recorded."""
import asyncio
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from dpmg.link import link_client as lc               # noqa: E402
from dpmg.link.proto import HelloAck             # noqa: E402


def _mgr(path):
    m = lc.LinkManager(saved_path=str(path) if path else None)
    m.sent = []
    m._send = lambda msg, addr: m.sent.append((type(msg).__name__, addr))
    return m


def _ack(m, addr, serial):
    m._on_msg(lc.MsgType.HELLO_ACK, 1,
              HelloAck(serial=serial, accepted=True, proto_version=lc.proto.VERSION), addr)


def test_connect_by_ip_saves_and_waits_in_pending(tmp_path):
    p = tmp_path / "saved.json"

    async def go():
        m = _mgr(p)
        m.add_manual_host("192.168.55.1", 47814, save=True)
        m.add_manual_host("192.168.55.1", 47814, save=True)       # no duplicate
        assert m.saved == [("192.168.55.1", 47814)]
        assert m.hosts == {} and list(m.pending) == ["@192.168.55.1:47814"]
        assert m.saved_status() == [("192.168.55.1", 47814, None)]
        for h in m.pending.values():
            h._hello_task.cancel()
    asyncio.run(go())
    assert json.loads(p.read_text()) == {"radios": [["192.168.55.1", 47814]]}


def test_answer_moves_it_to_hosts(tmp_path):
    async def go():
        m = _mgr(tmp_path / "s.json")
        m.add_manual_host("192.168.55.1", 47814, save=True)
        _ack(m, ("192.168.55.1", 47814), "LIMESDR-MINI")
        assert m.pending == {} and "LIMESDR-MINI" in m.hosts
        assert m.hosts["LIMESDR-MINI"].session_open
        assert m.saved_status() == [("192.168.55.1", 47814, "LIMESDR-MINI")]
    asyncio.run(go())


def test_saved_list_comes_back_after_restart(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"radios": [["192.168.55.1", 47812], ["192.168.55.1", 47810]]}))

    async def go():
        m = _mgr(p)
        for ip, port in m._load_saved():
            m.add_manual_host(ip, port)
        assert sorted(m.pending) == ["@192.168.55.1:47810", "@192.168.55.1:47812"]
        assert m.saved == [("192.168.55.1", 47812), ("192.168.55.1", 47810)]
        for h in m.pending.values():
            h._hello_task.cancel()
    asyncio.run(go())


def test_pending_gets_a_hello_every_rehello_period(tmp_path, monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(lc.time, "monotonic", lambda: t[0])

    async def go():
        m = _mgr(None)
        m.add_manual_host("192.168.55.1", 47810)
        h = m.pending["@192.168.55.1:47810"]
        h._hello_task.cancel()
        m.sent.clear()
        m._rehello(h)
        m._rehello(h)                                  # same instant: no second one
        t[0] += lc.MANUAL_REHELLO_S
        m._rehello(h)
        assert [s[0] for s in m.sent] == ["Hello", "Hello"]
    asyncio.run(go())


def test_hello_burst_stops(monkeypatch):
    monkeypatch.setattr(lc, "HELLO_BURST_S", 0.3)
    monkeypatch.setattr(lc, "HELLO_RETRY_S", 0.05)

    async def go():
        m = _mgr(None)
        m.add_manual_host("10.0.0.9", 47810)
        h = m.pending["@10.0.0.9:47810"]
        await asyncio.sleep(0.6)
        assert h._hello_task.done()
        n = len(m.sent)
        assert 3 <= n <= 10                            # a burst, then nothing
        await asyncio.sleep(0.3)
        assert len(m.sent) == n
    asyncio.run(go())


def test_forget(tmp_path):
    p = tmp_path / "s.json"

    async def go():
        m = _mgr(p)
        m.add_manual_host("192.168.55.1", 47810, save=True)
        m.add_manual_host("192.168.55.1", 47812, save=True)
        _ack(m, ("192.168.55.1", 47812), "B200MINI-3000001")
        assert m.forget_host("192.168.55.1", 47810) == "forgot 192.168.55.1:47810"
        assert m.forget_host("192.168.55.1", 47812) == "forgot 192.168.55.1:47812"
        assert m.pending == {} and m.hosts == {} and m.saved == []
        assert m.forget_host("192.168.55.1", 47812).endswith("was not saved")
    asyncio.run(go())
    assert json.loads(p.read_text()) == {"radios": []}


def test_unplugged_radio_card_retires_then_returns(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(lc.time, "monotonic", lambda: t[0])

    async def go():
        m = _mgr(None)
        m.add_manual_host("192.168.55.1", 47814)
        m.pending["@192.168.55.1:47814"]._hello_task.cancel()
        _ack(m, ("192.168.55.1", 47814), "LIMESDR-MINI")
        h = m.hosts["LIMESDR-MINI"]
        h._status_t = t[0]                             # one heartbeat, then silence
        t[0] += 10.0                                   # stale: LINK LOST, card kept
        m._tick_manual()
        assert "LIMESDR-MINI" in m.hosts
        t[0] += lc.MANUAL_RETIRE_S                     # silent past the limit: hidden
        m._tick_manual()
        assert "LIMESDR-MINI" not in m.hosts and m.pending["LIMESDR-MINI"] is h
        _ack(m, ("192.168.55.1", 47814), "LIMESDR-MINI")   # plugged in again
        assert m.hosts["LIMESDR-MINI"] is h and m.pending == {}
        assert m.forget_host("192.168.55.1", 47814).startswith("forgot")
    asyncio.run(go())


def test_bad_file_and_no_path(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("{not json")
    m = _mgr(p)
    assert m._load_saved() == [] and m.saved == []
    m2 = _mgr(None)
    assert m2._load_saved() == []
    m2._save_addr(("1.2.3.4", 1))                     # no path: kept in memory only
    assert m2.saved == [("1.2.3.4", 1)]


try:
    import nicegui  # noqa: F401
    from dpmg.web import link_tab as lt
except ImportError:
    lt = None


@pytest.mark.skipif(lt is None, reason="nicegui not installed")
def test_saved_radios_path():
    assert lt.saved_radios_path({}, "/app", 47811) == "/app/output/saved_radios.json"
    assert lt.saved_radios_path({}, "/app", 47900) is None           # dev GUI
    assert lt.saved_radios_path({"DPMG_SAVED_RADIOS": "/x.json"}, "/app", 47900) == "/x.json"
    assert lt.saved_radios_path({"DPMG_SAVED_RADIOS": ""}, "/app", 47811) is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
