"""
FILE_* chunked transfer end-to-end test  (test_file_transfer.py)
================================================================
Runs the real RadioService (simulate mode) and the real LinkManager over
localhost and exercises the whole large-plan path:

  1. discovery + session open
  2. HostLink.send_file() pushes a multi-chunk plan (sliding-window,
     cumulative ack) into the service inbox
  3. inbox file bytes match what was sent
  4. cmd set_plan {"plan_inbox": name} loads the pushed plan
  5. single-chunk small file works
  6. set_plan with an unknown inbox name is REJECTED

Uses ports 48810/48811 so it can run beside a live GUI/service pair.
Points the service's data tree (DPMG_DATA_DIR, read at import) at a temp dir so
the inbox lands under it, not in the repo's data/inbox, and chdirs there too.

Usage:  python3 test_file_transfer.py        (exit 0 = all passed)
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root
sys.path.insert(0, REPO)

# the service resolves DATA_DIR once at import (env or <cwd>/data): redirect
# it before importing so uploads never land in the repo's data/inbox
TMP = tempfile.mkdtemp(prefix="dpmg_ft_")
os.environ["DPMG_DATA_DIR"] = os.path.join(TMP, "data")

from dpmg.radio import dpmg_service
from dpmg.radio.dpmg_service import RadioService
from dpmg.link.link_client import LinkManager
from dpmg.link.proto import CmdStatus, HostType

CMD_PORT = 48810
DISCO_PORT = 48811


def big_plan(entries: int = 60) -> dict:
    """A plan comfortably bigger than one 1400-byte datagram."""
    return {
        "schema_version": 1,
        "compose": {"tx_radio": "g40", "sample_rate_hz": 1_920_000.0,
                    "center_freq_hz": 425_025_000.0,
                    "rf_bandwidth_hz": 1_536_000.0},
        "entries": [{
            "name": f"CH-{i:03d}", "modulation": "FSK",
            "params": {"symbol_rate": 4800.0, "tone_spacing_hz": 1000.0},
            "offset_hz": -400_000.0 + i * 12_500.0,
            "power_db": 0.0, "role": "known", "decode": True,
            "frame": {"sync_bits": "1011001000111011", "max_sync_errors": 2,
                      "has_length": True, "crc": "CRC-16-CCITT"},
        } for i in range(entries)],
    }


async def main() -> int:
    tmp = TMP
    os.chdir(tmp)                       # cwd under tmp as well (list_waveforms)
    checks = []

    def check(name, ok, detail=""):
        checks.append(ok)
        print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")

    # where the service really writes pushed files (data tree, "inbox" subdir)
    inbox = dpmg_service._data_dir("inbox")
    check("service inbox lives under the temp data tree (not the repo)",
          os.path.realpath(inbox).startswith(os.path.realpath(tmp)), f"({inbox})")

    # set_plan's channelizer guard would reject before touching the inbox on a
    # box without the GPU worker; the inbox lookup is what's under test here.
    dpmg_service._HAVE_CHAN = True

    svc = RadioService(serial="G40-FT-SIM", host_type=HostType.G40_RX,
                       kind="mock", simulate=True, cmd_port=CMD_PORT,
                       discovery_port=DISCO_PORT, broadcast="127.0.0.1")
    stop = asyncio.Event()
    svc_task = asyncio.create_task(svc.serve(stop))

    mgr = LinkManager(gui_id="GCS-FT-TEST", discovery_port=DISCO_PORT)
    await mgr.start()

    # 1. discovery + session
    host = None
    for _ in range(100):                # ANNOUNCE every 2 s; allow ~10 s
        await asyncio.sleep(0.1)
        host = mgr.hosts.get("G40-FT-SIM")
        if host is not None and host.session_open:
            break
    check("discovery + session open", host is not None and host.session_open)
    if host is None or not host.session_open:
        stop.set(); await asyncio.sleep(0.2); return 1

    # 2. multi-chunk push (60 entries ~ 12 KB ~ 12 chunks)
    plan = big_plan(60)
    blob = json.dumps(plan, separators=(",", ":")).encode()
    await host.send_file("bigplan.json", blob, timeout=20.0)
    check("multi-chunk send_file completes", True,
          f"({len(blob)} B, {(len(blob) + 1023) // 1024} chunks)")

    # 3. inbox integrity
    path = os.path.join(inbox, "bigplan.json")
    ok = os.path.exists(path) and open(path, "rb").read() == blob
    check("inbox file integrity", ok, f"({path})")

    # 4. set_plan from the inbox
    ack = await host.cmd("set_plan", {"plan_inbox": "bigplan.json"},
                         timeout=10.0)
    check("set_plan plan_inbox applies", ack.status == CmdStatus.OK
          and ack.result.get("entries") == 60, f"({ack.result})")

    # 5. single-chunk small file
    small = b'{"compose": {}, "entries": []}'
    await host.send_file("tiny.json", small, timeout=10.0)
    ok = open(os.path.join(inbox, "tiny.json"), "rb").read() == small
    check("single-chunk file", ok)

    # 6. unknown inbox name rejected
    ack = await host.cmd("set_plan", {"plan_inbox": "nope.json"}, timeout=10.0)
    check("unknown inbox name rejected", ack.status == CmdStatus.REJECTED,
          f"({ack.result})")

    # 7. loss survival: drop 30% of datagrams in both directions and push
    # again: the cumulative-ack window must retransmit through it
    import random
    rng = random.Random(1337)
    real_mgr_send, real_svc_send = mgr._send, svc.ep.send
    mgr._send = lambda m, a: (None if rng.random() < 0.30
                              else real_mgr_send(m, a))
    svc.ep.send = lambda m, a: (None if rng.random() < 0.30
                                else real_svc_send(m, a))
    try:
        await host.send_file("lossy.json", blob, timeout=30.0)
        ok = open(os.path.join(inbox, "lossy.json"), "rb").read() == blob
        check("multi-chunk push under 30% loss", ok)
    except TimeoutError as e:
        check("multi-chunk push under 30% loss", False, f"({e})")
    finally:
        mgr._send, svc.ep.send = real_mgr_send, real_svc_send

    stop.set()
    await mgr.stop()
    try:
        await asyncio.wait_for(svc_task, 3.0)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        svc_task.cancel()
    print(f"\nresult: {sum(checks)}/{len(checks)} checks passed")
    return 0 if all(checks) else 1


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING)
    sys.exit(asyncio.run(main()))
