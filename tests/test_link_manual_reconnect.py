"""
A radio added with Connect by IP reconnects on its own after its service
restarts (link_client.LinkManager._rehello).

The service announces to a port this GUI does not listen on, the same as the
Super Nano, whose ANNOUNCE never reaches the x86 GUI. Without the re-HELLO the
link would stay stale after a service restart until Connect by IP is pressed again.

Checks:
  1. by-IP add opens the session, the host is marked manual
  2. service stops: link goes stale, the GUI starts re-sending HELLO
  3. a fresh service on the same port: session reopens with no operator action
  4. a radio found by ANNOUNCE is not marked manual (unchanged behaviour)

Usage:  python3 tests/test_link_manual_reconnect.py     (exit 0 = all pass)
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dpmg.link.proto import HostType

from dpmg.link import link_client
from dpmg.radio.dpmg_service import RadioService
from dpmg.link.link_client import LinkManager

SERIAL = "B200-MANUAL-SIM"
CMD_PORT = 47940                      # off the default to dodge stray procs
SVC_DISCO = 47941                     # where the service announces
GUI_DISCO = 47942                     # where the GUI listens: never hears it

checks = []


def check(name, ok, detail=""):
    checks.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")


async def wait_for(pred, timeout, poll=0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(poll)
    return pred()


def start_service(serial=SERIAL, disco=SVC_DISCO):
    svc = RadioService(serial=serial, host_type=HostType.OTHER, kind="mock",
                       simulate=True, cmd_port=CMD_PORT, discovery_port=disco,
                       broadcast="127.0.0.1")
    stop = asyncio.Event()
    return stop, asyncio.create_task(svc.serve(stop))


async def stop_service(stop, task):
    stop.set()
    try:
        await asyncio.wait_for(task, 5.0)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass


async def main():
    print("Connect by IP: reconnect after a service restart\n")
    stop, task = start_service()
    mgr = LinkManager(gui_id="GCS-X86-TEST", discovery_port=GUI_DISCO)
    await mgr.start()
    try:
        mgr.add_manual_host("127.0.0.1", CMD_PORT)
        ok = await wait_for(lambda: SERIAL in mgr.hosts
                            and mgr.hosts[SERIAL].connected, 6.0)
        host = mgr.hosts.get(SERIAL)
        check("by-IP add opens the session", ok, f"(hosts={list(mgr.hosts)})")
        if host is None:
            return 1
        check("host is marked manual", host.manual)
        await asyncio.sleep(link_client.STALE_AFTER_S + 0.5)
        check("a fresh session is not dropped while its first heartbeat is due",
              host.connected and not any("re-sending HELLO" in e[-1] for e in mgr.events),
              f"(events={[e[-1] for e in mgr.events][-3:]})")

        await stop_service(stop, task)
        ok = await wait_for(lambda: not host.connected,
                            link_client.STALE_AFTER_S + 2.0, poll=0.25)
        check("service stopped -> link stale", ok, f"(age={host.status_age:.1f}s)")
        ok = await wait_for(lambda: any("re-sending HELLO" in e[-1] for e in mgr.events),
                            link_client.MANUAL_REHELLO_S + 2.0, poll=0.25)
        check("GUI starts re-sending HELLO while stale", ok)

        stop, task = start_service()
        t0 = time.monotonic()
        ok = await wait_for(lambda: host.connected,
                            link_client.MANUAL_REHELLO_S + 4.0, poll=0.1)
        check("fresh service on the same port -> reconnected on its own", ok,
              f"({time.monotonic() - t0:.1f} s after restart)")
        check("same host entry, same serial", mgr.hosts.get(SERIAL) is host)
        await stop_service(stop, task)
    finally:
        await mgr.stop()

    # 4: a radio found by ANNOUNCE stays on the announce path
    stop, task = start_service(serial="ANNOUNCED-SIM", disco=GUI_DISCO)
    mgr = LinkManager(gui_id="GCS-X86-TEST2", discovery_port=GUI_DISCO)
    await mgr.start()
    try:
        ok = await wait_for(lambda: "ANNOUNCED-SIM" in mgr.hosts
                            and mgr.hosts["ANNOUNCED-SIM"].connected, 6.0)
        check("ANNOUNCE-found radio connects", ok)
        check("ANNOUNCE-found radio is not manual",
              ok and not mgr.hosts["ANNOUNCED-SIM"].manual)
    finally:
        await stop_service(stop, task)
        await mgr.stop()

    print(f"\nresult: {sum(checks)}/{len(checks)} checks passed")
    return 0 if checks and all(checks) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
