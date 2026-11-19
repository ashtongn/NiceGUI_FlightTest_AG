"""
DPMG ground-station link client  (link_client.py)
==================================================
The GUI-side half of the ground station. Runs inside the NiceGUI
process on the x86 ground station (same asyncio loop) and speaks proto.py
UDP to every radio service it can hear.

Hosts are discovered by listening for ANNOUNCE broadcasts on the discovery
port, so no topology is configured. Identity is the radio serial, not the
IP: an address change (WiFi rejoin, DHCP renew) updates the serial's current
address and a new HELLO reopens the session.

    mgr = LinkManager(gui_id="GCS-X86")
    await mgr.start()
    ...
    host = mgr.hosts.get("G40-0001")        # serial: HostLink
    await host.cmd("set_center", {"hz": 425.025e6})
    host.status, host.status_age, host.is_stale
    host.psd                                 # PsdView (freqs_hz, psd_db)
    host.detections                          # det_index: proto.Detection

Reliability model (proven in test_link_loopback.py at 30-50% loss):
  - CMD: retried every CMD_RETRY_S until CMD_ACK (idempotent setpoints).
  - STATUS: 1 Hz heartbeat; staleness = link loss even if all else drops.
  - PSD_FRAME: fire-and-forget, last-value-wins; decoded lazily on read.
  - DETECTION: cached by det_index; a background reconciler compares the
    cache against a snapshot of STATUS.det_index and requests contiguous
    gap runs (new arrivals during a round are the next round's problem).

cmd_id is seeded from wall-clock so a GUI restart never collides with the
service's stored-ack idempotency window from a previous run.

The GUI never touches sockets or proto directly: it polls these caches
from its ui.timer tick and awaits cmd() from button handlers.

Stdlib + numpy only. proto.py is the same file the radio services use.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sys
import time
from collections import deque

import numpy as np

from dpmg.paths import match_folder_owner
from dpmg.link import proto
from dpmg.link.proto import (Cmd, CmdStatus, DetectionReq, FileChunk, FileOffer, Hello,
                   HostType, MsgType, RadioState)

log = logging.getLogger("link_client")

DISCOVERY_PORT = 47811                # where services broadcast ANNOUNCE
CMD_RETRY_S = 0.20                    # command retransmit cadence (design)
CMD_TIMEOUT_S = 5.0                   # default per-command give-up
HELLO_RETRY_S = 0.20
HELLO_BURST_S = 3.0                   # one _kick_hello retries this long, then stops
STALE_AFTER_S = 3.5                   # 3 missed heartbeats + margin
WATCH_TTL_S = 3.0                     # drop a decode band this long after its last page (2 Hz updates)
RECONCILE_S = 0.5                     # detection gap-fill cadence
MANUAL_REHELLO_S = 2.0                # stale radio added by IP: one HELLO this often
# Radios added by IP are saved here and dialled again at every GUI start, so a
# radio whose service starts later (USB hot-plug on the Super Nano) shows up by
# itself. An address that has never answered stays out of `hosts` (see pending).
SAVED_RADIOS_NAME = "saved_radios.json"
# a radio added by IP that has been silent this long goes back to `pending`, so
# an unplugged radio's card goes away; it shows LINK LOST until then
MANUAL_RETIRE_S = 60.0
RECONCILE_RUNS = 4                    # contiguous runs per round
RECONCILE_RUN_MAX = 64                # matches service DET_REQ_MAX
RECONCILE_GIVE_UP = 20                # rounds before a hole is abandoned
DET_CACHE_MAX = 8192                  # matches service ring; bounds RAM
CONFIG_POLL_S = 1.0                   # Get_config cadence (one status period)
FILE_NAME_MAX = 32                    # FileOffer.name is a 32-byte field on the wire
# Commands after which the shared config snapshot is re-read at once
STATE_CMDS = frozenset({
    "init", "close", "set_center", "set_tx_center", "set_gain", "set_tx_atten",
    "start_rx", "stop_rx",
    "set_tx_waveform", "start_tx", "stop_tx", "set_plan", "gen_scenario",
    "gen_test_waveform", "resample_waveform", "log_detections", "log_telemetry",
    "gps_antenna_bias", "frontend_set", "frontend_reload", "atten_set",
    "payload_key", "payload_restore", "transceive", "ground_checkout",
    "set_role", "fpga_load"})


def wire_file_name(name: str, limit: int = FILE_NAME_MAX) -> str:
    """The name a FILE_* push is stored under on the radio: the basename cut to
    the 32-byte FileOffer field keeping the extension (.iq.bin, .c64 ...), so
    the loader still recognises the format and the caller can name the inbox
    file it really created."""
    import os as _o
    base = _o.path.basename(str(name)) or "unnamed"
    if len(base.encode("utf-8")) <= limit:
        return base
    low = base.lower()
    ext = ".iq.bin" if low.endswith(".iq.bin") else _o.path.splitext(base)[1]
    if len(ext.encode("utf-8")) >= limit:
        ext = ""
    stem = base[:len(base) - len(ext)] if ext else base
    while len((stem + ext).encode("utf-8")) > limit:
        stem = stem[:-1]
    return stem + ext


class PsdView:
    """One decoded PSD snapshot. Reconstructs the float arrays lazily so
    the datagram path stays allocation-free."""

    __slots__ = ("frame", "t_arrival", "_decoded")

    def __init__(self, frame, t_arrival):
        self.frame = frame
        self.t_arrival = t_arrival
        self._decoded = None

    @property
    def freqs_hz(self) -> np.ndarray:
        self._decode()
        return self._decoded[0]

    @property
    def psd_db(self) -> np.ndarray:
        self._decode()
        return self._decoded[1]

    def _decode(self):
        if self._decoded is None:
            f = self.frame
            bins = np.frombuffer(f.bins, dtype=np.int8)
            db = f.ref_db + bins.astype(np.float64) * (f.scale_db_x100 / 100.0)
            if len(bins) > 1:
                freqs = np.linspace(f.center_hz - f.span_hz / 2.0,
                                    f.center_hz + f.span_hz / 2.0, len(bins))
            else:
                freqs = np.full(len(bins), f.center_hz)
            self._decoded = (freqs, db)


class HostLink:
    """State for one discovered radio service, keyed by serial."""

    def __init__(self, mgr: "LinkManager", serial: str, host_type: int,
                 addr: tuple):
        self.mgr = mgr
        self.serial = serial
        self.host_type = host_type
        self.addr = addr                    # current addr: may flap
        self.svc_version = 0

        self.session_open = False
        self.status = None                  # last proto.Status
        self._status_t = 0.0                # monotonic arrival time
        self.psd: PsdView | None = None
        self.psd_frames_rx = 0
        self.detections: dict[int, object] = {}   # det_index: Detection
        self.watch_bands: list = []         # v3 WATCH_UPDATE: channelizer decode rows
        self._watch_t = 0.0                 # monotonic arrival time of last update
        self._watch_map: dict = {}          # band name: (row, monotonic): merged pages
        # Sealed bodies the radio forwarded (opened on this host by
        # secure_ground); (seq, SealedFrame) newest last, bounded
        from collections import deque as _dq
        self.sealed = _dq(maxlen=256)
        self.sealed_rx = 0
        self.det_head = 0                   # from STATUS (emission count)
        self._det_attempts: dict[int, int] = {}   # gap-fill bookkeeping
        self._det_given_up: set[int] = set()

        self._pending: dict[int, asyncio.Future] = {}  # cmd_id: ack future
        # restart-safe cmd_id seed: the service replays stored acks for ids
        # it has already applied, so a fresh GUI must not reuse old ids.
        self._next_cmd_id = int(time.time()) & 0x7FFFFFFF
        self._hello_task: asyncio.Task | None = None
        # added with Connect by IP: it may never ANNOUNCE to this GUI, so the
        # manager re-sends HELLO itself while it is stale (see _rehello)
        self.manual = False
        self._rehello_t = 0.0
        self._open_t = 0.0                  # monotonic time of the last HELLO_ACK
        # FILE_* push state: latest cumulative ack + wakeup per transfer
        self._file_hc: dict[int, int] = {}       # file_id: highest_contig
        self._file_evt: dict[int, asyncio.Event] = {}
        # The service's get_config snapshot, shared by every view and
        # browser of this GUI process (None until read / on an older service)
        self.config: dict | None = None
        self.config_t = 0.0                      # monotonic time of the last snapshot
        self.config_supported: bool | None = None
        self._cfg_task: asyncio.Task | None = None
        self._cfg_again = False
        self._state_gen = 0                      # bumped on every state-changing ack

    # GUI-facing state

    @property
    def connected(self) -> bool:
        return self.session_open and not self.is_stale

    @property
    def status_age(self) -> float:
        """Seconds since the last STATUS heartbeat (inf before the first)."""
        if self._status_t == 0.0:
            return float("inf")
        return time.monotonic() - self._status_t

    @property
    def is_stale(self) -> bool:
        return self.status_age > STALE_AFTER_S

    @property
    def radio_state(self) -> int:
        if self.status is None:
            return int(RadioState.UNINIT)
        return self.status.radio_state

    # command plane

    async def cmd(self, name: str, params: dict | None = None,
                  timeout: float = CMD_TIMEOUT_S):
        """Send an absolute setpoint and retry until CMD_ACK. Returns the
        CmdAck; raises TimeoutError if the link stays dead past `timeout`."""
        c = Cmd(self._next_cmd_id, name, params or {})
        self._next_cmd_id = (self._next_cmd_id + 1) & 0xFFFFFFFF or 1
        fut = asyncio.get_running_loop().create_future()
        self._pending[c.cmd_id] = fut
        t_end = time.monotonic() + timeout
        try:
            while time.monotonic() < t_end:
                self.mgr._send(c, self.addr)
                try:
                    ack = await asyncio.wait_for(asyncio.shield(fut), CMD_RETRY_S)
                    if name in STATE_CMDS:
                        self._state_gen += 1
                        # the cached snapshot predates this change: views hold their
                        # widgets (config_age = inf) until the fresh read lands
                        self.config_t = 0.0
                        self.kick_config()      # every view sees it within one refresh
                    return ack
                except asyncio.TimeoutError:
                    continue
            raise TimeoutError(
                f"cmd {name} to {self.serial} not acked in {timeout:.1f}s")
        finally:
            self._pending.pop(c.cmd_id, None)

    # Shared config snapshot

    def kick_config(self):
        """Re-read get_config now (coalesced: one read in flight, one queued)."""
        if self.config_supported is False:
            return
        if self._cfg_task is not None and not self._cfg_task.done():
            self._cfg_again = True
            return
        try:
            self._cfg_task = asyncio.get_running_loop().create_task(self._read_config())
        except RuntimeError:
            pass                                 # no loop (offline use)

    async def _read_config(self):
        while True:
            self._cfg_again = False
            gen = self._state_gen
            try:
                ack = await self.cmd("get_config", {}, timeout=3.0)
            except Exception:                    # link down: STATUS staleness says so
                return
            if self._state_gen != gen:
                continue                         # a state change overtook this read: re-read,
                                                 # never show the older value for a moment
            if ack.status == CmdStatus.OK:
                self.config = dict(ack.result or {})
                self.config_t = time.monotonic()
                self.config_supported = True
            elif "unknown cmd" in str((ack.result or {}).get("err", "")):
                self.config_supported = False    # older service: views fall back to STATUS
                return
            if not self._cfg_again:
                return

    @property
    def config_age(self) -> float:
        return time.monotonic() - self.config_t if self.config_t else float("inf")

    async def send_file(self, name: str, data: bytes,
                        timeout: float = 60.0, window: int = 8) -> str:
        """Push a file to the service over the FILE_* chunked transfer
        (cumulative ack, sliding window). Blocks until the service has the
        whole file in its inbox; raises TimeoutError on a dead link. Use for
        anything bigger than one datagram (plans, waveforms); afterwards
        cmd("set_plan", {"plan_inbox": name}) applies a pushed plan. Returns the
        name the radio stored it under (wire_file_name: cut to the 32-byte field,
        extension kept): use that for the follow-up inbox reference."""
        name = wire_file_name(name)
        fid = self._next_cmd_id
        self._next_cmd_id = (self._next_cmd_id + 1) & 0xFFFFFFFF or 1
        n = proto.FILE_CHUNK_PAYLOAD
        chunks = [data[i:i + n] for i in range(0, len(data), n)] or [b""]
        evt = asyncio.Event()
        self._file_evt[fid] = evt
        offer = FileOffer(fid, len(chunks), len(data), name)
        t_end = time.monotonic() + timeout
        try:
            # offer until the first ack opens the transfer
            while fid not in self._file_hc:
                if time.monotonic() > t_end:
                    raise TimeoutError(f"FILE_OFFER to {self.serial} never acked")
                self.mgr._send(offer, self.addr)
                try:
                    await asyncio.wait_for(evt.wait(), CMD_RETRY_S)
                except asyncio.TimeoutError:
                    pass
                evt.clear()
            # sliding window above highest_contig; a stalled ack (loss)
            # retransmits the window from the last acked position
            while self._file_hc.get(fid, -1) < len(chunks) - 1:
                if time.monotonic() > t_end:
                    raise TimeoutError(
                        f"file '{name}' to {self.serial} stalled at chunk "
                        f"{self._file_hc.get(fid, -1) + 1}/{len(chunks)}")
                base = self._file_hc.get(fid, -1) + 1
                for i in range(base, min(base + window, len(chunks))):
                    self.mgr._send(FileChunk(fid, i, chunks[i]), self.addr)
                try:
                    await asyncio.wait_for(evt.wait(), CMD_RETRY_S)
                except asyncio.TimeoutError:
                    pass
                evt.clear()
        finally:
            self._file_evt.pop(fid, None)
            self._file_hc.pop(fid, None)
        return name

    # session

    def _kick_hello(self):
        """(Re)open the session unless a hello loop is already running."""
        if self._hello_task is None or self._hello_task.done():
            self._hello_task = asyncio.create_task(self._hello_loop())

    async def _hello_loop(self):
        # a short burst; after it, ANNOUNCE (discovered radios) or the
        # manager's MANUAL_REHELLO_S cadence (radios added by IP) keeps trying
        end = time.monotonic() + HELLO_BURST_S
        while not self.session_open and time.monotonic() < end:
            self.mgr._send(Hello(self.mgr.gui_id), self.addr)
            await asyncio.sleep(HELLO_RETRY_S)

    # inbound (called by LinkManager)

    def _on_status(self, msg):
        # xrun fault surfacing: emit one shared event per counter jump (here,
        # not in the GUI refresh: multiple browsers would duplicate it)
        prev = self.status
        if prev is not None and (msg.overruns > prev.overruns
                                 or msg.underruns > prev.underruns):
            self.mgr._evt(f"! {self.serial} XRUN overruns +"
                          f"{msg.overruns - prev.overruns} underruns +"
                          f"{msg.underruns - prev.underruns} "
                          f"(totals {msg.overruns}/{msg.underruns})")
        self.status = msg
        self._status_t = time.monotonic()
        # an UNINIT radio has no live detection pipeline, and a service restart
        # rewinds det_index: in either case the cached detections are stale
        # (they'd otherwise linger on a closed radio), so drop them
        if (msg.radio_state == int(RadioState.UNINIT) and self.detections) \
                or msg.det_index < self.det_head:
            self.detections.clear()
            self._det_given_up.clear()
            self._det_attempts.clear()
            self.det_head = 0
        self.det_head = max(self.det_head, msg.det_index)

    def _merge_watch(self, msg):
        # WATCH_UPDATE arrives in one or more datagrams (pages) per snapshot.
        # Merge the pages by band name and age out bands that stop arriving
        # (plan changed or cleared), so a plan of any size shows in full.
        now = time.monotonic()
        for b in msg.bands:
            self._watch_map[str(b.get("n", ""))] = (b, now)
        for k in [k for k, (_, t) in self._watch_map.items()
                  if now - t > WATCH_TTL_S]:
            self._watch_map.pop(k, None)
        self.watch_bands = [row for row, _ in
                            sorted(self._watch_map.values(),
                                   key=lambda rt: rt[0].get("f", 0.0))]
        self._watch_t = now

    def _on_detection(self, msg):
        self.detections[msg.det_index] = msg
        self._det_given_up.discard(msg.det_index)
        self._det_attempts.pop(msg.det_index, None)
        # bound the cache: drop everything older than the newest window
        if len(self.detections) > DET_CACHE_MAX:
            cutoff = max(self.detections) - DET_CACHE_MAX
            for i in [k for k in self.detections if k <= cutoff]:
                del self.detections[i]

    # detection gap-fill

    def _reconcile_detections(self):
        """One reconciliation round against a snapshot of det_head. Requests
        up to RECONCILE_RUNS contiguous runs of missing indices; holes that
        stay unanswered for RECONCILE_GIVE_UP rounds (aged out of the
        service ring) are abandoned so the round stays O(window)."""
        target = self.det_head
        if target == 0:
            return
        lo = max(0, target - DET_CACHE_MAX)     # never chase pruned history
        missing = [i for i in range(lo, target)
                   if i not in self.detections and i not in self._det_given_up]
        if not missing:
            return
        runs, i = [], 0
        while i < len(missing) and len(runs) < RECONCILE_RUNS:
            start, run = missing[i], 1
            while i + run < len(missing) and missing[i + run] == start + run:
                run += 1
            runs.append((start, min(run, RECONCILE_RUN_MAX)))
            i += run
        for start, count in runs:
            for idx in range(start, start + count):
                n = self._det_attempts.get(idx, 0) + 1
                if n > RECONCILE_GIVE_UP:
                    self._det_given_up.add(idx)
                    self._det_attempts.pop(idx, None)
                else:
                    self._det_attempts[idx] = n
            self.mgr._send(DetectionReq(start, count), self.addr)


def _bind_udp_reuseaddr(host: str, port: int) -> socket.socket:
    """A bound, non-blocking UDP socket with SO_REUSEADDR for
    create_datagram_endpoint(sock=...). Any bind failure propagates as OSError
    (the socket is closed first) so the caller's retry loop sees the same
    exception type it does on Linux."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setblocking(False)
        s.bind((host, port))
    except OSError:
        s.close()
        raise
    return s


class LinkManager:
    """Owns the UDP endpoints and the per-serial HostLink registry."""

    def __init__(self, gui_id: str = "GCS-X86",
                 discovery_port: int = DISCOVERY_PORT, saved_path: str | None = None):
        self.gui_id = gui_id
        self.discovery_port = discovery_port
        self.hosts: dict[str, HostLink] = {}       # serial: HostLink
        # added by IP but never answered: kept out of `hosts` (so no radio card)
        # and re-sent HELLO until the service starts; placeholder: HostLink
        self.pending: dict[str, HostLink] = {}
        self._by_addr: dict[tuple, HostLink] = {}  # current addr: HostLink
        self.saved_path = saved_path               # None: nothing is saved
        self.saved: list[tuple] = []               # (ip, port) in the order added
        self._session_ep = None
        self._disco_transport = None
        self._session_transport = None
        self._reconcile_task: asyncio.Task | None = None
        self._seq = 0
        self.garbage_rx = 0
        # link event feed for the GUI log: (monotonic counter, text).
        # Consumers keep their own cursor and drain entries with n > cursor.
        self.events: deque = deque(maxlen=300)
        self._evt_n = 0

    def _evt(self, msg: str):
        self._evt_n += 1
        self.events.append((self._evt_n, time.strftime("%H:%M:%S"), msg))
        log.info("%s", msg)

    # lifecycle

    async def start(self):
        loop = asyncio.get_running_loop()
        # discovery socket: receives ANNOUNCE broadcasts on a fixed port.
        # SO_REUSEPORT (so a native dev GUI and the docker GUI
        # can overlap during a redeploy) exists only on POSIX; asyncio raises
        # ValueError, not OSError, for reuse_port on Windows, which would
        # escape the caller's bind-failure retry. On Windows bind a socket
        # ourselves with SO_REUSEADDR, the Windows equivalent for UDP.
        if sys.platform == "win32":
            self._disco_transport, _ = await loop.create_datagram_endpoint(
                lambda: _ClientEndpoint(self, "disco"),
                sock=_bind_udp_reuseaddr("0.0.0.0", self.discovery_port))
        else:
            self._disco_transport, _ = await loop.create_datagram_endpoint(
                lambda: _ClientEndpoint(self, "disco"),
                local_addr=("0.0.0.0", self.discovery_port),
                reuse_port=True)
        # session socket: ephemeral port; all cmd/telemetry flows here once
        # HELLO goes out from it (the service replies to the sender addr)
        self._session_transport, self._session_ep = \
            await loop.create_datagram_endpoint(
                lambda: _ClientEndpoint(self, "session"),
                local_addr=("0.0.0.0", 0))
        # the session socket carries the telemetry/PSD flood and cmd acks; grow
        # its receive buffer so a multi-fragment ack (e.g. a long file listing)
        # isn't evicted by a burst of PSD frames while RX is streaming
        try:
            _ssock = self._session_transport.get_extra_info("socket")
            if _ssock is not None:
                import socket as _sk
                _ssock.setsockopt(_sk.SOL_SOCKET, _sk.SO_RCVBUF, 4 * 1024 * 1024)
        except OSError:
            pass
        self._reconcile_task = asyncio.create_task(self._reconcile_loop())
        log.info("link manager up (gui_id=%s, discovery port %d)",
                 self.gui_id, self.discovery_port)
        for ip, port in self._load_saved():
            self.add_manual_host(ip, port)

    async def stop(self):
        if self._reconcile_task is not None:
            self._reconcile_task.cancel()
        for host in list(self.hosts.values()) + list(self.pending.values()):
            if host._hello_task is not None:
                host._hello_task.cancel()
        for tr in (self._disco_transport, self._session_transport):
            if tr is not None:
                tr.close()

    def _send(self, msg, addr):
        self._seq = (self._seq + 1) & 0xFFFFFFFF
        try:
            self._session_transport.sendto(proto.encode(msg, self._seq), addr)
        except (OSError, proto.ProtoError) as e:
            log.warning("send %s -> %s failed: %s",
                        type(msg).__name__, addr, e)

    # inbound dispatch

    def _on_msg(self, mtype, seq, msg, addr):
        if mtype == MsgType.ANNOUNCE:
            self._on_announce(msg, addr)
            return

        host = self._by_addr.get(addr)
        if host is None and hasattr(msg, "serial"):
            # HELLO_ACK / STATUS carry the serial: rebind an addr flap even
            # if the ANNOUNCE that would normally do it was lost
            host = self.hosts.get(msg.serial)
            if host is not None:
                self._rebind(host, addr)
        if host is None:
            log.debug("drop %s from unknown %s", mtype.name, addr)
            return
        # A host added by IP starts with a placeholder serial; the
        # first HELLO_ACK/STATUS carries the real one: adopt + re-key.
        if getattr(msg, "serial", "") and msg.serial != host.serial:
            self._adopt_serial(host, msg.serial)

        if mtype == MsgType.HELLO_ACK:
            if msg.accepted:
                if self.pending.get(host.serial) is host:      # retired radio is back
                    self.pending.pop(host.serial)
                    self.hosts[host.serial] = host
                host.session_open = True
                host._open_t = time.monotonic()
                self._evt(f"session open with {host.serial} at {addr[0]}:{addr[1]}")
            else:
                self._evt(f"service {host.serial} REFUSED session "
                          f"(proto {msg.proto_version} != ours {proto.VERSION})")
        elif mtype == MsgType.STATUS:
            host._on_status(msg)
        elif mtype == MsgType.PSD_FRAME:
            host.psd = PsdView(msg, time.monotonic())
            host.psd_frames_rx += 1
        elif mtype == MsgType.DETECTION:
            host._on_detection(msg)
        elif mtype == MsgType.WATCH_UPDATE:      # v3 channelizer decode telemetry
            host._merge_watch(msg)
        elif mtype == MsgType.SEALED_FRAME:      # Open on the ground
            host.sealed.append(msg)
            host.sealed_rx += 1
        elif mtype == MsgType.CMD_ACK:
            fut = host._pending.get(msg.cmd_id)
            if fut is not None and not fut.done():
                fut.set_result(msg)
        elif mtype == MsgType.FILE_ACK:
            evt = host._file_evt.get(msg.file_id)
            if evt is not None:                  # transfer still in flight
                prev = host._file_hc.get(msg.file_id, -2)
                host._file_hc[msg.file_id] = max(prev, msg.highest_contig)
                evt.set()

    def _on_announce(self, msg, addr):
        svc_addr = (addr[0], msg.cmd_port)   # ANNOUNCE src port != cmd port
        host = self.hosts.get(msg.serial)
        if host is None:
            host = HostLink(self, msg.serial, msg.host_type, svc_addr)
            host.svc_version = msg.svc_version
            self.hosts[msg.serial] = host
            self._by_addr[svc_addr] = host
            self._evt(f"discovered {msg.serial} "
                      f"({HostType(msg.host_type).name}) at "
                      f"{svc_addr[0]}:{svc_addr[1]}")
            host._kick_hello()
            return
        if host.addr != svc_addr:
            self._rebind(host, svc_addr)
        # service restarted (fresh gui_addrs) or link died: STATUS goes
        # stale while ANNOUNCE still arrives: reopen the session
        if host.is_stale and host.session_open:
            host.session_open = False
        if not host.session_open:
            host._kick_hello()

    def _rebind(self, host, new_addr):
        self._evt(f"{host.serial} address flap {host.addr} -> {new_addr} "
                  f"(session re-open)")
        self._by_addr.pop(host.addr, None)
        host.addr = new_addr
        self._by_addr[new_addr] = host
        host.session_open = False
        host._kick_hello()

    def add_manual_host(self, ip: str, port: int = 47810, save: bool = False) -> str:
        """Register a radio by IP and unicast HELLO to it, bypassing
        ANNOUNCE discovery (for when broadcast is firewalled or routed away). The
        GUI initiates, so the reply + telemetry ride the allowed established flow.
        The real serial is adopted from the first HELLO_ACK; until then the
        host waits in `pending`, and keeps getting a HELLO every
        MANUAL_REHELLO_S. save=True also adds the address to the saved list.
        Returns a status."""
        addr = (str(ip).strip(), int(port))
        if save:
            self._save_addr(addr)
        existing = self._by_addr.get(addr)
        if existing is not None:
            existing.manual = True
            existing.session_open = False
            existing._kick_hello()
            return f"re-connecting to {existing.serial} at {addr[0]}:{addr[1]}"
        placeholder = f"@{addr[0]}:{addr[1]}"          # until HELLO_ACK brings serial
        host = HostLink(self, placeholder, HostType.OTHER, addr)
        host.manual = True
        self.pending[placeholder] = host
        self._by_addr[addr] = host
        self._evt(f"manual add {addr[0]}:{addr[1]} -> HELLO (awaiting serial)")
        host._kick_hello()
        return f"connecting to {addr[0]}:{addr[1]} ..."

    def forget_host(self, ip: str, port: int) -> str:
        """Drop a radio added by IP: off the saved list, out of hosts/pending,
        no more HELLOs. A radio that also ANNOUNCEs will be discovered again."""
        addr = (str(ip).strip(), int(port))
        was_saved = addr in self.saved
        if was_saved:
            self.saved.remove(addr)
            self._write_saved()
        host = self._by_addr.pop(addr, None)
        if host is not None:
            self.hosts.pop(host.serial, None)
            self.pending.pop(host.serial, None)
            host.manual = False
            host.session_open = False
            if host._hello_task is not None:
                host._hello_task.cancel()
        if host is None and not was_saved:
            return f"{addr[0]}:{addr[1]} was not saved"
        self._evt(f"forgot {addr[0]}:{addr[1]}")
        return f"forgot {addr[0]}:{addr[1]}"

    def saved_status(self) -> list[tuple]:
        """[(ip, port, serial or None)] for the saved list; serial is set once
        the service there has answered."""
        out = []
        for addr in self.saved:
            h = self._by_addr.get(addr)
            serial = h.serial if h is not None and h.serial in self.hosts else None
            out.append((addr[0], addr[1], serial))
        return out

    # saved list (JSON: {"radios": [[ip, port], ...]})

    def _load_saved(self) -> list[tuple]:
        if not self.saved_path:
            return []
        try:
            with open(self.saved_path) as fh:
                rows = json.load(fh).get("radios", [])
            self.saved = [(str(ip), int(port)) for ip, port in rows]
        except FileNotFoundError:
            self.saved = []
        except (OSError, ValueError, TypeError, AttributeError) as e:
            log.warning("saved radios %s unreadable (%s); starting empty",
                        self.saved_path, e)
            self.saved = []
        if self.saved:
            self._evt("saved radios: " + ", ".join(f"{i}:{p}" for i, p in self.saved))
        return list(self.saved)

    def _save_addr(self, addr: tuple):
        if addr not in self.saved:
            self.saved.append(addr)
            self._write_saved()

    def _write_saved(self):
        if not self.saved_path:
            return
        tmp = self.saved_path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.saved_path) or ".", exist_ok=True)
            with open(tmp, "w") as fh:
                json.dump({"radios": [list(a) for a in self.saved]}, fh, indent=1)
            os.replace(tmp, self.saved_path)
            match_folder_owner(self.saved_path, os.path.dirname(self.saved_path) or ".")
        except OSError as e:
            log.warning("could not save radios to %s: %s", self.saved_path, e)

    def _retire(self, host):
        """A radio added by IP has been silent MANUAL_RETIRE_S (unplugged, its
        service stopped): drop its card but keep calling it from `pending`."""
        self.hosts.pop(host.serial, None)
        self.pending[host.serial] = host
        host.session_open = False
        self._evt(f"{host.serial} silent {MANUAL_RETIRE_S:g} s: hidden until its "
                  f"service at {host.addr[0]}:{host.addr[1]} answers again")

    def _adopt_serial(self, host, serial: str):
        """Re-key a host from its manual-add placeholder to the real serial the
        service reported (HELLO_ACK/STATUS). No-op when they already match."""
        if host.serial == serial:
            return
        self.hosts.pop(host.serial, None)
        self.pending.pop(host.serial, None)
        host.serial = serial
        self.hosts[serial] = host                      # overwrites any stale entry
        self._evt(f"adopted serial {serial} at {host.addr[0]}:{host.addr[1]}")

    def _rehello(self, host):
        """A radio added by IP may never ANNOUNCE to this GUI (firewall, another
        subnet), and ANNOUNCE is the only other thing that reopens a stale
        session. So after its service restarts it would stay stale until
        someone pressed Connect by IP again. While it is stale, send it one
        HELLO every MANUAL_REHELLO_S; its HELLO_ACK reopens the session."""
        now = time.monotonic()
        if host.session_open and now - host._open_t < STALE_AFTER_S:
            return                  # just opened: the first heartbeat is on its way
        if now - host._rehello_t < MANUAL_REHELLO_S:
            return
        host._rehello_t = now
        if host.session_open:
            host.session_open = False
            age = host.status_age
            quiet = (f"silent {age:.0f} s" if age != float("inf")
                     else "no heartbeat since the session opened")
            self._evt(f"{host.serial} {quiet}, re-sending HELLO to "
                      f"{host.addr[0]}:{host.addr[1]} every {MANUAL_REHELLO_S:g} s")
        self._send(Hello(self.gui_id), host.addr)

    def _tick_manual(self):
        """Radios added by IP, once per RECONCILE_S: keep calling the ones that
        never answered, re-HELLO stale ones, hide ones silent MANUAL_RETIRE_S."""
        for host in list(self.pending.values()):
            self._rehello(host)
        for host in list(self.hosts.values()):
            if not host.manual:
                continue
            if host.status_age > MANUAL_RETIRE_S \
                    and time.monotonic() - host._open_t > MANUAL_RETIRE_S:
                self._retire(host)
            elif host.is_stale:
                self._rehello(host)

    async def _reconcile_loop(self):
        n = 0
        while True:
            await asyncio.sleep(RECONCILE_S)
            n += 1
            self._tick_manual()
            for host in list(self.hosts.values()):
                if host.session_open:
                    host._reconcile_detections()
                    # One get_config per radio per status period, shared
                    # by all views/browsers of this process
                    if n * RECONCILE_S >= CONFIG_POLL_S and host.connected:
                        host.kick_config()
            if n * RECONCILE_S >= CONFIG_POLL_S:
                n = 0


class _ClientEndpoint(asyncio.DatagramProtocol):
    def __init__(self, mgr: LinkManager, name: str):
        self.mgr = mgr
        self.name = name

    def datagram_received(self, data, addr):
        try:
            mtype, seq, msg = proto.decode(data)
        except proto.ProtoError as e:
            self.mgr.garbage_rx += 1
            log.debug("drop garbage from %s: %s", addr, e)
            return
        self.mgr._on_msg(mtype, seq, msg, addr)
