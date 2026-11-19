"""
DPMG link loopback harness  (test_link_loopback.py)
===================================================
Runs a fake radio service and a fake GUI client on 127.0.0.1 with
simulated packet loss injected in both directions, and asserts that
every reliability mechanism in proto.py actually survives it:

  1. HELLO/HELLO_ACK session open (retried until acked)
  2. CMD retry-until-CMD_ACK, idempotent under duplicate delivery
  3. STATUS heartbeat + staleness detection
  4. DETECTION stream gap detection + DETECTION_REQ gap-fill
  5. FILE_OFFER/FILE_CHUNK/FILE_ACK cumulative-ack transfer

Usage:
    python3 test_link_loopback.py [loss_fraction]      # default 0.20

Stdlib only. Exit code 0 = all checks passed.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root
from dpmg.link import proto
from dpmg.link.proto import (Announce, Cmd, CmdAck, CmdStatus, Detection,
                   DetectionReq, FileAck, FileChunk, FileOffer, Hello,
                   HelloAck, HostType, MsgType, PsdFrame, RadioState,
                   Status)

LOSS = float(sys.argv[1]) if len(sys.argv) > 1 else 0.20
SVC_PORT = 47810
rng = random.Random(1337)


class LossyEndpoint(asyncio.DatagramProtocol):
    """Datagram endpoint that randomly drops outbound packets and hands
    inbound ones to a callback. Tracks per-direction stats."""

    def __init__(self, name, on_msg):
        self.name = name
        self.on_msg = on_msg
        self.transport = None
        self.seq = 0
        self.sent = self.dropped = self.rx = self.garbage = 0

    def connection_made(self, transport):
        self.transport = transport

    def send(self, msg, addr):
        self.seq += 1
        data = proto.encode(msg, self.seq)
        self.sent += 1
        if rng.random() < LOSS:
            self.dropped += 1
            return                      # simulated loss
        self.transport.sendto(data, addr)

    def datagram_received(self, data, addr):
        try:
            mtype, seq, msg = proto.decode(data)
        except proto.ProtoError:
            self.garbage += 1
            return
        self.rx += 1
        self.on_msg(mtype, seq, msg, addr)


# Fake radio service (stands in for dpmg_service.py)

class FakeService:
    def __init__(self):
        self.ep = LossyEndpoint("svc", self.on_msg)
        self.serial = "G40-0001-SIM"
        self.gui_addr = None
        self.applied = {}               # cmd_id: CmdAck (idempotency store)
        self.center_hz = 425.025e6
        self.state = RadioState.INIT_OK
        self.det_log = []               # ring buffer stand-in
        self.det_head = 0
        self.t0 = time.time()

    # inbound
    def on_msg(self, mtype, seq, msg, addr):
        if mtype == MsgType.HELLO:
            ok = 1 if msg.proto_version == proto.VERSION else 0
            self.gui_addr = addr if ok else None
            self.ep.send(HelloAck(self.serial, ok), addr)

        elif mtype == MsgType.CMD:
            # Idempotency: duplicate cmd_id: resend stored ack, apply once.
            if msg.cmd_id in self.applied:
                self.ep.send(self.applied[msg.cmd_id], addr)
                return
            ack = self.apply(msg)
            self.applied[msg.cmd_id] = ack
            self.ep.send(ack, addr)

        elif mtype == MsgType.DETECTION_REQ:
            lo = msg.start_index
            hi = min(lo + msg.count, len(self.det_log))
            for det in self.det_log[lo:hi]:
                self.ep.send(det, addr)

        elif mtype == MsgType.FILE_OFFER:
            self.rx_file = {"id": msg.file_id, "total": msg.total_chunks,
                            "chunks": {}, "name": msg.name}
            self.ep.send(FileAck(msg.file_id, -1), addr)

        elif mtype == MsgType.FILE_CHUNK:
            f = getattr(self, "rx_file", None)
            if f is None or msg.file_id != f["id"]:
                return
            f["chunks"][msg.chunk_idx] = msg.payload
            hc = -1
            while hc + 1 in f["chunks"]:
                hc += 1
            self.ep.send(FileAck(f["id"], hc), addr)

    def apply(self, cmd: Cmd) -> CmdAck:
        # absolute setpoints only
        if cmd.name == "set_center":
            self.center_hz = float(cmd.params["hz"])
            return CmdAck(cmd.cmd_id, CmdStatus.OK,
                          {"center_hz": self.center_hz})
        if cmd.name == "start_task":
            self.state = RadioState.STREAMING
            return CmdAck(cmd.cmd_id, CmdStatus.OK, {})
        if cmd.name == "stop":
            self.state = RadioState.INIT_OK
            return CmdAck(cmd.cmd_id, CmdStatus.OK, {})
        return CmdAck(cmd.cmd_id, CmdStatus.REJECTED,
                      {"err": f"unknown cmd {cmd.name}"})

    # outbound telemetry (accelerated rates for the test)
    async def run(self):
        n = 0
        while True:
            await asyncio.sleep(0.02)           # "1 Hz", accelerated
            n += 1
            if self.gui_addr is None:
                continue
            self.ep.send(Status(
                serial=self.serial, host_type=HostType.G40_RX,
                radio_state=self.state,
                flags=Status.FLAG_POS_VALID,
                temp_c_x10=612,
                lat_1e7=proto.deg_to_1e7(39.826),
                lon_1e7=proto.deg_to_1e7(-84.048),
                alt_m=1524.0 + n,
                uptime_s=int(time.time() - self.t0),
                last_cmd_id=max(self.applied, default=0),
                det_index=self.det_head), self.gui_addr)
            # PSD every other tick
            if n % 2 == 0:
                bins = bytes((i * 7 + n) % 200 for i in range(1024))
                self.ep.send(PsdFrame(self.center_hz, 30.72e6, -110.0,
                                      50, bins), self.gui_addr)
            # a detection most ticks
            if n % 3 != 0:
                det = Detection(self.det_head, time.time(),
                                self.center_hz + rng.uniform(-10e6, 10e6),
                                20e3, rng.uniform(8, 30), 12.5)
                self.det_log.append(det)
                self.det_head += 1
                self.ep.send(det, self.gui_addr)


# Fake GUI client (stands in for the x86 side's UDP layer)

class FakeGui:
    def __init__(self):
        self.ep = LossyEndpoint("gui", self.on_msg)
        self.svc = ("127.0.0.1", SVC_PORT)
        self.session = None
        self.next_cmd_id = 1
        self.pending = {}               # cmd_id: Future
        self.last_status = None
        self.last_status_t = 0.0
        self.psd_frames = 0
        self.dets = {}                  # det_index: Detection
        self.svc_det_head = 0
        self.file_ack = {}              # file_id: highest_contig

    def on_msg(self, mtype, seq, msg, addr):
        if mtype == MsgType.HELLO_ACK:
            self.session = msg
        elif mtype == MsgType.STATUS:
            self.last_status = msg
            self.last_status_t = time.monotonic()
            self.svc_det_head = max(self.svc_det_head, msg.det_index)
        elif mtype == MsgType.PSD_FRAME:
            self.psd_frames += 1
        elif mtype == MsgType.DETECTION:
            self.dets[msg.det_index] = msg
        elif mtype == MsgType.CMD_ACK:
            fut = self.pending.pop(msg.cmd_id, None)
            if fut and not fut.done():
                fut.set_result(msg)
        elif mtype == MsgType.FILE_ACK:
            self.file_ack[msg.file_id] = msg.highest_contig

    # reliability: retry until acked
    async def hello(self, timeout=5.0):
        t_end = time.monotonic() + timeout
        while time.monotonic() < t_end:
            self.ep.send(Hello("GCS-X86-SIM"), self.svc)
            await asyncio.sleep(0.05)
            if self.session:
                return self.session
        raise TimeoutError("HELLO never acked")

    async def cmd(self, name, params=None, timeout=3.0, retry=0.05):
        cmd = Cmd(self.next_cmd_id, name, params or {})
        self.next_cmd_id += 1
        fut = asyncio.get_running_loop().create_future()
        self.pending[cmd.cmd_id] = fut
        t_end = time.monotonic() + timeout
        while time.monotonic() < t_end:
            self.ep.send(cmd, self.svc)
            try:
                return await asyncio.wait_for(asyncio.shield(fut), retry)
            except asyncio.TimeoutError:
                continue
        self.pending.pop(cmd.cmd_id, None)
        raise TimeoutError(f"cmd {cmd.cmd_id} ({name}) never acked")

    # gap-fill
    async def fill_det_gaps(self, timeout=10.0, max_runs_per_round=4):
        """Reconcile against a snapshot of the head. New detections that
        arrive during reconciliation are the next round's problem: this
        must not race a moving target."""
        target = self.svc_det_head
        t_end = time.monotonic() + timeout
        while time.monotonic() < t_end:
            missing = [i for i in range(target) if i not in self.dets]
            if not missing:
                return
            # request several contiguous runs per round
            runs, i = [], 0
            while i < len(missing) and len(runs) < max_runs_per_round:
                start, run = missing[i], 1
                while (i + run < len(missing)
                       and missing[i + run] == start + run):
                    run += 1
                runs.append((start, min(run, 64)))
                i += run
            for start, count in runs:
                self.ep.send(DetectionReq(start, count), self.svc)
            await asyncio.sleep(0.05)
        raise TimeoutError(f"gap-fill incomplete: "
                           f"{len(missing)} still missing")

    # chunked file push with cumulative ack
    async def push_file(self, file_id, data: bytes, timeout=30.0):
        n = proto.FILE_CHUNK_PAYLOAD
        chunks = [data[i:i + n] for i in range(0, len(data), n)]
        self.file_ack.pop(file_id, None)
        # offer until first ack
        t_end = time.monotonic() + timeout
        while file_id not in self.file_ack:
            if time.monotonic() > t_end:
                raise TimeoutError("FILE_OFFER never acked")
            self.ep.send(FileOffer(file_id, len(chunks), len(data),
                                   "scenario.g40iq"), self.svc)
            await asyncio.sleep(0.05)
        # sliding window of 8 above highest_contig
        while self.file_ack.get(file_id, -1) < len(chunks) - 1:
            if time.monotonic() > t_end:
                raise TimeoutError("file transfer stalled")
            base = self.file_ack.get(file_id, -1) + 1
            for i in range(base, min(base + 8, len(chunks))):
                self.ep.send(FileChunk(file_id, i, chunks[i]), self.svc)
            await asyncio.sleep(0.03)


# test sequence

async def main():
    loop = asyncio.get_running_loop()
    svc = FakeService()
    gui = FakeGui()

    await loop.create_datagram_endpoint(lambda: svc.ep,
                                        local_addr=("127.0.0.1", SVC_PORT))
    await loop.create_datagram_endpoint(lambda: gui.ep,
                                        local_addr=("127.0.0.1", 0))
    svc_task = asyncio.create_task(svc.run())
    checks = []

    def check(name, ok, detail=""):
        checks.append(ok)
        print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")

    print(f"DPMG loopback harness: simulated loss {LOSS:.0%} each direction\n")

    # 1. session open
    ack = await gui.hello()
    check("HELLO/HELLO_ACK under loss", ack.accepted == 1,
          f"(serial={ack.serial})")

    # 2. commands: 20 setpoints, all must ack, final state must match last
    for i in range(20):
        hz = 425.025e6 + i * 1e5
        r = await gui.cmd("set_center", {"hz": hz})
        assert r.status == CmdStatus.OK
    check("20 commands retried to completion", True)
    check("idempotent final state", svc.center_hz == 425.025e6 + 19 * 1e5,
          f"(center={svc.center_hz/1e6:.4f} MHz)")
    r = await gui.cmd("start_task")
    check("start_task applied", svc.state == RadioState.STREAMING)

    # 3. let telemetry flow, confirm heartbeat freshness
    await asyncio.sleep(1.5)
    age = time.monotonic() - gui.last_status_t
    check("STATUS heartbeat fresh", gui.last_status is not None and age < 0.5,
          f"(age={age*1000:.0f} ms, psd_frames={gui.psd_frames})")
    check("position decodes", gui.last_status is not None and
          abs(proto.deg_from_1e7(gui.last_status.lat_1e7) - 39.826) < 1e-6)

    # 4. detection gap-fill: loss guarantees gaps; fill them.
    # Stop production first so the completeness check has a fixed target
    # (in the live system this is a continuous background reconciliation).
    svc_task.cancel()
    gui.svc_det_head = svc.det_head          # head sync (STATUS in real sys)
    lost_before = sum(1 for i in range(svc.det_head)
                      if i not in gui.dets)
    await gui.fill_det_gaps()
    complete = all(i in gui.dets for i in range(gui.svc_det_head))
    check("DETECTION gap-fill", complete,
          f"({lost_before} lost in stream, {gui.svc_det_head} recovered total)")

    # 5. file push: 200 KB with cumulative-ack window
    payload = os.urandom(200 * 1024)
    t0 = time.monotonic()
    await gui.push_file(1, payload)
    dt = time.monotonic() - t0
    rx = b"".join(svc.rx_file["chunks"][i]
                  for i in range(svc.rx_file["total"]))
    check("200 KB FILE transfer integrity",
          hashlib.sha256(rx).digest() == hashlib.sha256(payload).digest(),
          f"({dt:.1f} s, {len(payload)/dt/1024:.0f} KB/s at {LOSS:.0%} loss)")

    svc_task.cancel()
    print(f"\nlink stats: gui sent={gui.ep.sent} dropped={gui.ep.dropped} | "
          f"svc sent={svc.ep.sent} dropped={svc.ep.dropped}")
    print(f"result: {sum(checks)}/{len(checks)} checks passed")
    return 0 if all(checks) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
