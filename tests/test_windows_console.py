"""Windows console branches of the shared link modules, run on Linux with
sys.platform patched and the socket faked. No network, no radio.

Covers acft_pos_multicast: the bind address rule (Linux binds all interfaces,
Windows binds the chosen adapter address), the socket options in order
(SO_REUSEADDR, bind, IP_ADD_MEMBERSHIP with the interface), the local address
list, and link_client's hand-bound discovery socket for Windows.

    python3 -m pytest tests/test_windows_console.py
"""
import os
import socket
import struct
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from dpmg.link import acft_pos_multicast as apm      # noqa: E402
from dpmg.link import link_client as lc              # noqa: E402


class _FakeSock:
    def __init__(self):
        self.calls = []

    def setsockopt(self, level, opt, val):
        self.calls.append(("setsockopt", level, opt, val))

    def bind(self, addr):
        self.calls.append(("bind", addr))


def test_bind_address_linux_always_all_interfaces():
    assert apm._bind_address("192.168.55.100", "linux") == ""
    assert apm._bind_address("0.0.0.0", "linux") == ""
    assert apm._bind_address("", "linux") == ""


def test_bind_address_windows_binds_the_adapter():
    assert apm._bind_address("192.168.55.100", "win32") == "192.168.55.100"
    assert apm._bind_address(" 192.168.56.100 ", "win32") == "192.168.56.100"
    assert apm._bind_address("0.0.0.0", "win32") == ""
    assert apm._bind_address("", "win32") == ""


def test_bind_address_defaults_to_this_platform(monkeypatch):
    monkeypatch.setattr(apm.sys, "platform", "win32")
    assert apm._bind_address("10.1.2.3") == "10.1.2.3"
    monkeypatch.setattr(apm.sys, "platform", "linux")
    assert apm._bind_address("10.1.2.3") == ""


def _mreq(group, iface):
    return struct.pack("4s4s", socket.inet_aton(group), socket.inet_aton(iface))


def test_socket_setup_windows_order_and_membership():
    s = _FakeSock()
    apm._setup_multicast_socket(s, "239.2.1.1", 5005, "192.168.55.100", platform="win32")
    assert s.calls == [
        ("setsockopt", socket.SOL_SOCKET, socket.SO_REUSEADDR, 1),
        ("bind", ("192.168.55.100", 5005)),
        ("setsockopt", socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
         _mreq("239.2.1.1", "192.168.55.100")),
    ]


def test_socket_setup_linux_unchanged():
    s = _FakeSock()
    apm._setup_multicast_socket(s, "239.2.1.1", 5005, "192.168.55.100", platform="linux")
    assert s.calls[1] == ("bind", ("", 5005))
    assert s.calls[2][3] == _mreq("239.2.1.1", "192.168.55.100")


def test_socket_setup_windows_default_adapter():
    s = _FakeSock()
    apm._setup_multicast_socket(s, "239.2.1.1", 5005, "0.0.0.0", platform="win32")
    assert s.calls[1] == ("bind", ("", 5005))
    assert s.calls[2][3] == _mreq("239.2.1.1", "0.0.0.0")


def test_listener_start_uses_setup_with_platform(monkeypatch):
    """The listener goes through the shared setup with its own iface on win32
    (bind to it) and no thread is left running."""
    seen = {}

    def fake_setup(s, group, port, iface_ip, platform=None):
        seen.update(group=group, port=port, iface=iface_ip, platform=platform or sys.platform)

    monkeypatch.setattr(apm, "_setup_multicast_socket", fake_setup)
    monkeypatch.setattr(apm.sys, "platform", "win32")
    lst = apm.MulticastPosListener("239.2.1.1", 5005, iface_ip="192.168.56.100")
    lst.start()
    try:
        assert seen == {"group": "239.2.1.1", "port": 5005, "iface": "192.168.56.100",
                        "platform": "win32"}
    finally:
        lst.stop()
    assert not lst.running


def test_local_ipv4_addresses_shape():
    ips = apm.local_ipv4_addresses()
    assert ips and ips[-1] == "127.0.0.1"
    assert len(ips) == len(set(ips))
    for ip in ips:
        socket.inet_aton(ip)
    assert all(not ip.startswith("127.") for ip in ips[:-1])


def test_local_ipv4_addresses_never_raises(monkeypatch):
    monkeypatch.setattr(apm.socket, "getaddrinfo",
                        lambda *a, **k: (_ for _ in ()).throw(socket.gaierror("no name")))
    monkeypatch.setattr(apm.sys, "platform", "win32")   # skips the ip(8) call too
    assert apm.local_ipv4_addresses()[-1] == "127.0.0.1"


def test_link_client_windows_discovery_socket_reuseaddr():
    s = lc._bind_udp_reuseaddr("127.0.0.1", 0)
    try:
        assert s.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR)
        assert s.getsockname()[1] > 0
        assert not s.getblocking()
    finally:
        s.close()


def test_link_manager_start_win32_branch(monkeypatch):
    """With sys.platform patched the manager binds the discovery socket itself
    (sock=) instead of asking asyncio for reuse_port."""
    import asyncio
    seen = []

    async def go():
        m = lc.LinkManager(discovery_port=0)
        loop = asyncio.get_running_loop()
        real = loop.create_datagram_endpoint

        async def spy(factory, **kw):
            seen.append(sorted(kw))
            return await real(factory, **kw)

        monkeypatch.setattr(loop, "create_datagram_endpoint", spy)
        monkeypatch.setattr(lc.sys, "platform", "win32")
        await m.start()
        await m.stop()

    asyncio.run(go())
    assert seen[0] == ["sock"]
    assert seen[1] == ["local_addr"]
