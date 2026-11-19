"""
Integration test: dpmg_service (MockBackend) + link_client in one
asyncio process over the loopback, exercising the real discovery path.

Checks:
  1. ANNOUNCE discovery: LinkManager finds the service by serial, no
     address configuration anywhere (topology-blind)
  2. session auto-opens (HELLO retried until HELLO_ACK), connected=True
  3. init/set_center/start_rx round-trip through HostLink.cmd retry loop
  4. STATUS cache: fresh heartbeat, STREAMING state, task-running flag
  5. PSD cache: PsdView decodes to the commanded center/rate axis and
     shows the mock 4-FSK band (tones over noise floor)
  6. stop_rx closes the mock's continuous bursts: detections appear in
     the cache with contiguous indices
  7. background reconciler refills a wiped detection cache from the
     service ring (gap-fill without any manual DETECTION_REQ)
  8. staleness: service stops then status_age grows then connected False

Usage:  python3 test_link_client.py     (exit 0 = all pass)
"""

from __future__ import annotations

import asyncio
import sys
import time

import numpy as np

import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root
from dpmg.link import proto
from dpmg.link.proto import CmdStatus, HostType, RadioState

from dpmg.radio import dpmg_service
from dpmg.link import link_client
from dpmg.radio.dpmg_service import RadioService
from dpmg.link.link_client import LinkManager

SERIAL = "G40-0001-SIM"
CMD_PORT = 47910                      # off the default to dodge stray procs
DISCO_PORT = 47911
CENTER = 426_900_000.0
RATE = 1_000_000.0

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


async def main():
    print("link_client integration test (service + client, loopback)\n")

    svc = RadioService(serial=SERIAL, host_type=HostType.G40_RX, kind="mock",
                       simulate=True, cmd_port=CMD_PORT,
                       discovery_port=DISCO_PORT, broadcast="127.0.0.1")
    stop_svc = asyncio.Event()
    svc_task = asyncio.create_task(svc.serve(stop_svc))

    mgr = LinkManager(gui_id="GCS-X86-TEST", discovery_port=DISCO_PORT)
    await mgr.start()

    # 1-2: discovery + auto session
    await wait_for(lambda: SERIAL in mgr.hosts, 5.0)
    host = mgr.hosts.get(SERIAL)
    check("ANNOUNCE discovery by serial", host is not None,
          f"(hosts={list(mgr.hosts)})")
    if host is None:
        return finish(mgr, stop_svc, svc_task)
    ok = await wait_for(lambda: host.session_open, 5.0)
    check("session auto-open (HELLO/HELLO_ACK)", ok)
    check("host identity fields", host.host_type == HostType.G40_RX
          and host.addr[1] == CMD_PORT)

    # 3: command plane through the retry loop
    ack = await host.cmd("init", {
        "sample_rate": RATE, "center_freq": CENTER, "rx_gain": 30.0,
        "rx_antenna": "RX2", "kind": "mock"})
    check("init acked OK", ack.status == CmdStatus.OK,
          f"(achieved_rate={ack.result.get('achieved_rate')})")
    ack = await host.cmd("set_center", {"hz": CENTER})
    check("set_center acked OK", ack.status == CmdStatus.OK)
    ack = await host.cmd("start_rx")
    check("start_rx acked OK", ack.status == CmdStatus.OK)

    # 4: STATUS cache
    ok = await wait_for(lambda: host.status is not None and
                        host.radio_state == RadioState.STREAMING, 4.0)
    st = host.status
    check("STATUS cache shows STREAMING", ok,
          f"(age={host.status_age:.2f}s)")
    check("connected + task-running flag", host.connected and st is not None
          and st.flags & proto.Status.FLAG_TASK_RUNNING)

    # 5: PSD cache: let a few frames land, then decode
    await wait_for(lambda: host.psd_frames_rx >= 4, 4.0)
    view = host.psd
    ok_axis = ok_band = False
    if view is not None:
        f, p = view.freqs_hz, view.psd_db
        ok_axis = (len(f) == len(p) > 0
                   and abs(f[len(f) // 2] - CENTER) < 2e3
                   and (f[-1] - f[0]) < RATE)
        if ok_axis:
            band = (np.abs(f - CENTER) < 250e3)
            ok_band = (float(np.median(p[band]) - np.median(p[~band])) > 6.0)
    check("PsdView axis matches center/rate", ok_axis,
          f"(frames_rx={host.psd_frames_rx})")
    check("PsdView shows mock tone band", ok_band)

    # 6: detections on burst close
    ack = await host.cmd("stop_rx")
    check("stop_rx acked OK", ack.status == CmdStatus.OK)
    await wait_for(lambda: len(host.detections) >= 2, 4.0)
    nd = len(host.detections)
    check("detections cached on burst close", nd >= 2, f"({nd} bursts)")
    check("det indices contiguous", sorted(host.detections) == list(range(nd)))

    # 7: reconciler refills a wiped cache (no manual DETECTION_REQ)
    host.detections.clear()
    host._det_attempts.clear()
    host._det_given_up.clear()
    ok = await wait_for(lambda: len(host.detections) == nd, 6.0)
    check("background gap-fill refills wiped cache", ok,
          f"({len(host.detections)}/{nd} recovered)")

    # 7b: TX command plane (mock backend: no real RF). Prove set_tx_waveform
    # then start_tx then FLAG_TX_ACTIVE in STATUS then stop_tx clears it.
    await host.cmd("start_rx")            # re-arm streaming for the mock
    ack = await host.cmd("set_tx_waveform",
                         {"kind": "tone", "offset_hz": 1e6, "ampl": 0.5})
    check("set_tx_waveform acked OK", ack.status == CmdStatus.OK,
          f"({ack.result.get('waveform')})")
    ack = await host.cmd("start_tx", {"mode": "CONTINUOUS"})
    check("start_tx acked OK", ack.status == CmdStatus.OK)
    ok = await wait_for(lambda: host.status is not None and
                        host.status.flags & proto.Status.FLAG_TX_ACTIVE, 3.0)
    check("STATUS shows FLAG_TX_ACTIVE while transmitting", ok)
    ack = await host.cmd("stop_tx")
    ok = await wait_for(lambda: host.status is not None and
                        not (host.status.flags & proto.Status.FLAG_TX_ACTIVE),
                        3.0)
    check("stop_tx clears FLAG_TX_ACTIVE", ack.status == CmdStatus.OK and ok)

    # 8: staleness on service death
    stop_svc.set()
    await asyncio.sleep(0)
    ok = await wait_for(lambda: not host.connected,
                        link_client.STALE_AFTER_S + 2.0, poll=0.25)
    check("stale link drops connected", ok,
          f"(age={host.status_age:.1f}s)")

    return await finish(mgr, stop_svc, svc_task)


async def finish(mgr, stop_svc, svc_task):
    stop_svc.set()
    try:
        await asyncio.wait_for(svc_task, 5.0)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass
    await mgr.stop()
    print(f"\nresult: {sum(checks)}/{len(checks)} checks passed")
    return 0 if checks and all(checks) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
