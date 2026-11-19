"""
DPMG headless radio service  (dpmg_service.py)
===============================================
The radio-side half of the ground station. Runs on a radio host
(G40 RX / X40 TX) with no GUI. Wraps the sdr_core backend + scheduler and
speaks proto.py over UDP to a topology-blind ground GUI.

Identity is the radio serial, never the IP. The GUI discovers services by
listening for ANNOUNCE broadcasts; it never needs to be told a host address.

Message planes:
  - ANNOUNCE broadcast (every 2 s)            service to discovery port
  - HELLO / HELLO_ACK session open            gui and service
  - STATUS heartbeat (1 Hz)                    service to gui
  - PSD_FRAME telemetry (4 Hz)                 service to gui
        scheduler.latest() IQ, PSDRecorder.live_psd, decimate to
        <=1024 bins, int8 quantize at 0.5 dB/LSB. Fire-and-forget,
        last-value-wins.
  - DETECTION events + DETECTION_REQ gap-fill  service and gui
        EnergyDetector slot grid over the live IQ, BurstTracker
        rising/falling edges, closed bursts emitted with monotonically
        increasing det_index, kept in a bounded ring for gap-fill replay.
        Open bursts are force-closed on stop_rx / retune / teardown.
  - CMD plane, idempotent absolute setpoints:  gui to service
        init, set_center, set_gain, set_tx_atten,
        start_rx, stop_rx, close
TODO(gps): STATUS sends lat/lon as zeros with FLAG_POS_VALID unset; wire a
GPS position source into _status() for the airborne G40. Temperature is
already sent (host junction temperature).

Runs hardware-free with --simulate (MockBackend), so it can be developed and
smoke-tested on the x86 dev box. On a Jetson use ThreadPoolExecutor only
(ProcessPoolExecutor crashes under CUDA/posix_ipc): this file uses threads.

Stdlib + numpy (numpy only reaches in via sdr_core). proto.py is verbatim.

Manual run:
    python3 dpmg_service.py --simulate --kind mock --serial G40-0001-SIM \
        --host-type g40 --cmd-port 47810 --discovery-port 47811 \
        --broadcast 127.0.0.1
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os as _os
import ipaddress as _ipaddress
import re as _re
import signal
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from dpmg.link import proto
from dpmg.payload import rf_frontend as _rf          # External PA / TX-RX switch (opt-in)
from dpmg.payload import mc_atten as _mc             # Mini-Circuits USB attenuator (opt-in)
from dpmg.payload import payload_guard as _pg        # RX protection on every PA key (opt-in)
from dpmg.payload import device_watch as _dw        # Attenuator plug-in + switch ADC presence
from dpmg.radio import gps_antenna as _ga          # G40 3.3 V GPS antenna bias + gpsd sky
from dpmg.secure import api as _sf         # Secure messages (flag DPMG_SECURE_MSG)
from dpmg.radio import host_thermal as _ht         # Junction temperature in the status heartbeat
from dpmg.radio import card_imu as _imu            # Tilt / shock / motion from the NV100 accelerometer
from dpmg.radio import barometer as _baro          # BMP581/585 barometer on the host I2C bus
from dpmg.radio import iq_record as _rec           # Record live RX, replay it on TX
from dpmg.radio import radio_role as _role         # What this radio is acting as (refuses what the role does not use)
from dpmg.radio import port_map as _ports          # What each SMA jack may be used for (rf_frontend.yaml ports:)
from dpmg.radio import fpga_image as _fpga         # FPGA image readback and loading
from dpmg.link.proto import (Announce, CmdAck, CmdStatus, FileAck, HelloAck, HostType, SealedFrame,
                   MsgType, PsdFrame, RadioState, Status, WatchUpdate)
from dpmg.link.proto import Detection as WireDetection   # energy_detector has its own

from dpmg.radio import sdr_core as sdr
from dpmg.radio import g40_clock as _g40_clock   # the clock choices, for the simulator
from dpmg.dsp import psd_trace as pt
from dpmg.dsp import energy_detector as ed

try:
    from dpmg.dsp.channelizer_worker import ChannelizerWorker
    from dpmg.scenario.channel_plan import ChannelPlan
    _HAVE_CHAN = True
except Exception:                       # GPU/channelizer deps absent (dev box)
    _HAVE_CHAN = False

from dpmg.paths import REPO_DIR as _REPO_DIR  # noqa: E402

log = logging.getLogger("dpmg_service")

SVC_VERSION = 1                       # informational; bump on service changes


def _resample_iq(iq, in_rate, out_rate):
    """Linear-interpolation resample of complex IQ, preserving duration. Used to
    lift a modem reply burst from the modem rate up to the card rate before TX."""
    in_rate = float(in_rate)
    out_rate = float(out_rate)
    x = np.asarray(iq, dtype=np.complex64)
    if in_rate <= 0 or out_rate <= 0 or x.size == 0 or in_rate == out_rate:
        return x
    n_out = max(1, int(round(x.size * out_rate / in_rate)))
    src = np.arange(n_out) * (in_rate / out_rate)
    left = np.floor(src).astype(np.int64)
    frac = (src - left).astype(np.float32)
    right = np.minimum(left + 1, x.size - 1)
    left = np.minimum(left, x.size - 1)
    return (x[left] * (1.0 - frac) + x[right] * frac).astype(np.complex64)

# PSD telemetry
PSD_INTERVAL_S = 0.25                 # 4 Hz, within the 2-5 Hz design band
PSD_MAX_BINS = 1024                   # <= PsdFrame.MAX_BINS (~1372); design cap
_BLUE_EXTS = (".cdif", ".tmp", ".blue", ".prm")   # Midas BLUE / Platinum recordings
PSD_SCALE_DB_X100 = 50                # 0.5 dB per int8 LSB (design spec)
PSD_DEFAULT_NFFT = 4096
PSD_DEFAULT_WINDOW = "hann"
PSD_DEFAULT_BUFFER = 65536

# detection telemetry
DET_INTERVAL_S = 0.2                  # detector tick (skips stale frames)
# An init that takes the whole service down (libsidekiq exits on a critical error,
# for example a G40 init on an external clock with no 10 MHz on REF IN) leaves a
# marker behind. For this long after the restart the same init is refused, so a
# client that keeps resending it cannot restart the service over and over.
INIT_CRASH_HOLD_S = 600.0
DET_RING_MAX = 8192                   # detections kept for gap-fill replay
DET_REQ_MAX = 64                      # cap on one DETECTION_REQ answer
APPLIED_MAX = 4096                    # stored acks kept for duplicate replay (oldest dropped;
                                      # a GUI retries for <= its cmd timeout, so 4096 is ample)
ANT_BIAS_CACHE_S = 5.0                # Get_config re-reads the G40 bias at most this often
GNSS_CACHE_S = 5.0                    # and gpsd for the GNSS fix at most this often
# the X40's gpsd runs on its ZynqMP, reached from the Jetson (Getting Started Guide p20)
X40_GPSD = _os.environ.get("DPMG_X40_GPSD", "192.168.55.100:2947")
X40_BIAS_NOTE = "the X40's GPS antenna bias is set on its ZynqMP (on at boot), not by DPMG"
# Commands that change what the radio is set to; get_config's cfg_seq
# counts them so a GUI (and the checkout) can tell a navigation sent nothing
_STATE_CMDS = frozenset({
    "init", "close", "set_center", "set_tx_center", "set_gain", "set_tx_atten",
    "start_rx", "stop_rx",
    "set_tx_waveform", "start_tx", "stop_tx", "set_plan", "gen_scenario",
    "gen_test_waveform", "resample_waveform", "log_detections", "log_telemetry",
    "gps_antenna_bias", "frontend_set", "frontend_reload", "atten_set",
    "payload_key", "payload_restore", "transceive", "ground_checkout",
    "set_role", "fpga_load", "set_ports",
    "replay_recording", "bitpipe_start", "bitpipe_stop"})
DET_DEFAULT_SLOT_BW = 20e3
DET_DEFAULT_THRESHOLD_DB = 10.0
DET_DEFAULT_NFFT = 32768
# slot clustering (DET-02): per-slot detections are collapsed to one burst per
# emitter: a local maximum within CLUSTER_MARGIN dB of its stronger neighbour
# in a run of active slots is a separate emitter, weaker bumps are skirt; a run
# bridges up to CLUSTER_GAP inactive slots. Margin 0 disables (legacy one
# burst per slot). See energy_detector.cluster_detections (DET-02).
DET_DEFAULT_CLUSTER_MARGIN_DB = 20.0
DET_DEFAULT_CLUSTER_GAP = 1
# burst hold-over + island suppression (DET-02 time splits at 1.92 MSps): an
# open burst survives HOLD_TICKS detector ticks with no cluster before it
# closes (t_stop stays the first missed tick); a new cluster within
# ISLAND_SLOTS inactive slots of an open burst and weaker than it must be seen
# on two ticks to open (more than CLUSTER_MARGIN below: never). 0 / -1 disable.
DET_DEFAULT_HOLD_TICKS = 1
DET_DEFAULT_ISLAND_SLOTS = 3

# WEDGE_WATCHDOG: automatic recovery when the NV100 RX FIFO wedges (replays old
# buffers; see sdr_core rx_degraded). Timings in seconds; DPMG_WEDGE_WATCHDOG=0
# disables it (detection + FLAG_RX_STALE still work).
WEDGE_WATCHDOG = _os.environ.get("DPMG_WEDGE_WATCHDOG", "1") != "0"
WEDGE_POLL_S = 0.25
WEDGE_STREAM_RESTART_S = float(_os.environ.get("DPMG_WEDGE_STREAM_RESTART_S", 2.0))
WEDGE_REINIT_AFTER_S = float(_os.environ.get("DPMG_WEDGE_REINIT_AFTER_S", 3.0))
WEDGE_GIVEUP_AFTER_S = float(_os.environ.get("DPMG_WEDGE_GIVEUP_AFTER_S", 10.0))
WEDGE_CLEAR_S = 2.0          # this long with no replay ends an episode

# RX ADC overload protection. Samples are normalized to +/-1.0 (16-bit full
# scale, see skiq_bridge.receive); a cluster of samples near full scale means
# the front end is clipping: wideband distortion + bogus detections.
CLIP_LEVEL = 0.92                     # |I| or |Q| above this = near full-scale
# High-rate consumer load with the native drain:
# above HIGH_RATE_HZ a 2048-block batch is 2.08 M samples every 34 ms, too much
# for the detector tick's FFTs. There the detector takes the newest
# DET_MAX_SAMPLES_HIGH_RATE samples (17 ms at 61.44 MSps; DPMG_DET_MAX_SAMPLES
# overrides, 0 = whole frame) and the clip check every CLIP_STRIDE_HIGH_RATE-th
# sample. At 30.72 MSps and below nothing changes.
HIGH_RATE_HZ = 40e6
DET_MAX_SAMPLES_HIGH_RATE = int(_os.environ.get("DPMG_DET_MAX_SAMPLES", 1 << 20))
CLIP_STRIDE_HIGH_RATE = 8
CLIP_FRAC_OVERLOAD = 1.0e-3           # >0.1% of samples clipping: overloaded
OVERLOAD_GAIN_STEP = 8                # rx-gain index decrement per back-off step
OVERLOAD_MAX_STEPS = 10               # cap on auto back-off steps per episode
OVERLOAD_BACKOFF_COOLDOWN_S = 0.6     # wait between steps so the clip metric catches up
OVERLOAD_GRACE_S = 1.0                # ignore clipping this long after RX (re)starts:
                                      # some front ends emit a full-scale burst at
                                      # stream start (HackRF) which is not an overload

_HOST_TYPE = {
    "g40": HostType.G40_RX,
    "x40": HostType.X40_TX,
    "mock": HostType.OTHER,
}

# sdr_core device state: wire RadioState
_STATE_MAP = {
    sdr.SDRDeviceState.DISCONNECTED: RadioState.UNINIT,
    sdr.SDRDeviceState.CONNECTING:   RadioState.UNINIT,
    sdr.SDRDeviceState.CLOSING:      RadioState.UNINIT,
    sdr.SDRDeviceState.IDLE:         RadioState.INIT_OK,
    sdr.SDRDeviceState.STREAMING:    RadioState.STREAMING,
    sdr.SDRDeviceState.ERROR:        RadioState.FAULT,
}

# init params that map directly onto SDRConfig fields (extras handled apart)
_CFG_FIELDS = ("sample_rate", "center_freq", "rx_gain", "tx_gain",
               "clock_source", "rx_antenna", "tx_antenna", "rx_chunk",
               "device_args")


def quantize_psd(freqs_hz, psd_db, max_bins=PSD_MAX_BINS,
                 scale_db_x100=PSD_SCALE_DB_X100):
    """Pack a float PSD (absolute-Hz freq axis + dB magnitudes) into a wire
    PsdFrame: decimate to <= max_bins, then int8-quantize the dB values at
    scale_db_x100/100 dB per LSB about a centered ref. The GUI reconstructs
    the freq axis linearly from center_hz/span_hz/nbins, so the decimated axis
    must stay linear (live_psd's axis is). Returns None for empty input.

    Reconstruction on the GUI: db = ref_db + bin * (scale_db_x100/100).
    Round-to-nearest gives <= half-LSB (0.25 dB at 0.5 dB/LSB) error for any
    value inside the ~127 dB int8 window; values outside clip.
    """
    f = np.asarray(freqs_hz, dtype=float)
    p = np.asarray(psd_db, dtype=float)
    if f.size == 0 or p.size == 0:
        return None
    # replace any -inf/nan (e.g. log10 of a zero bin) with the finite floor
    finite = np.isfinite(p)
    if not finite.all():
        floor = float(p[finite].min()) if finite.any() else -200.0
        p = np.where(finite, p, floor)
    step = max(1, p.size // max_bins)
    if step > 1:
        n = (p.size // step) * step       # trim to a whole number of groups
        # max over each group of `step` bins instead of a p[::step] subsample:
        # same output size + wire cost, but a narrow tone that lands in a
        # dropped bin does not scallop down ~18 dB. f keeps the group-start
        # freq so the (linear) axis the GUI reconstructs is unchanged.
        p = p[:n].reshape(-1, step).max(axis=1)
        f = f[:n].reshape(-1, step)[:, 0]
    if p.size > max_bins:                 # safety: still over the cap
        f = f[:max_bins]
        p = p[:max_bins]
    lsb = scale_db_x100 / 100.0
    ref_db = float(round((float(p.min()) + float(p.max())) / 2.0))
    bins = np.clip(np.round((p - ref_db) / lsb), -128, 127).astype(np.int8)
    if f.size > 1:
        center_hz = float((f[0] + f[-1]) / 2.0)
        span_hz = float(f[-1] - f[0])
    else:
        center_hz = float(f[0])
        span_hz = 0.0
    return PsdFrame(center_hz, span_hz, ref_db, scale_db_x100, bins.tobytes())


def _build_psd_frame(recorder, frame, max_bins):
    """Feed one RX frame into the recorder and return a wire PsdFrame from the
    current rolling buffer. Runs in the hw thread pool (FFT is CPU work) so it
    never stalls the event loop."""
    recorder.push_frame(frame.samples, frame.sample_rate, frame.center_freq,
                        rf_timestamp=getattr(frame, "rf_timestamp", 0),
                        seq=frame.seq)
    freqs, psd_db = recorder.live_psd(frame.sample_rate, frame.center_freq)
    return quantize_psd(freqs, psd_db, max_bins)


def _within_base(path, base=None):
    """Resolve `path` (relative paths are taken under `base`) and require the
    result to stay inside `base`: the service's working dir (/app_svc), which
    holds the waveform/scenario/capture tree. Blocks '..' traversal so a command
    can't read or write outside that tree. Returns the safe absolute path;
    raises ValueError on escape. Legitimate GUI paths are all relative under
    /app_svc, so this changes nothing for normal use."""
    base = _os.path.realpath(base or _os.getcwd())
    rp = _os.path.realpath(_os.path.join(base, str(path)))
    if rp == base or rp.startswith(base + _os.sep):
        return rp
    raise ValueError(f"path '{path}' escapes {base}")


# All generated/recorded/uploaded IQ lives under one tree instead of scattered at
# the service root. Override with DPMG_DATA_DIR; keep it under the service dir so
# _within_base still permits it. Subdirs: waveforms (generated), inbox (uploaded),
# captures (carved regions), iq (recordings).
DATA_DIR = _os.environ.get("DPMG_DATA_DIR") or _os.path.join(_os.getcwd(), "data")


def _data_dir(sub=""):
    d = _os.path.join(DATA_DIR, sub) if sub else DATA_DIR
    _os.makedirs(d, exist_ok=True)
    return d


class _Endpoint(asyncio.DatagramProtocol):
    """One UDP socket. Decodes inbound datagrams to the service and stamps a
    per-sender seq on outbound ones. Garbage in is counted and dropped, never
    fatal (the wire is lossy and may carry stray packets)."""

    def __init__(self, service: "RadioService"):
        self.service = service
        self.transport = None
        self.seq = 0
        self.sock = None

    def connection_made(self, transport):
        self.transport = transport
        self.sock = transport.get_extra_info("socket")
        # allow ANNOUNCE to a broadcast address
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except OSError:
            pass

    def datagram_received(self, data, addr):
        try:
            mtype, seq, msg = proto.decode(data)
        except proto.ProtoError as e:
            log.debug("drop garbage from %s: %s", addr, e)
            return
        self.service.on_msg(mtype, seq, msg, addr)

    def send(self, msg, addr):
        self.seq = (self.seq + 1) & 0xFFFFFFFF
        try:
            self.transport.sendto(proto.encode(msg, self.seq), addr)
        except (OSError, proto.ProtoError) as e:
            # an oversized ack is not a failure: the trimmed one below goes out and
            # logs what it dropped, and status polls would repeat this every second
            if not (isinstance(msg, CmdAck) and isinstance(e, proto.ProtoError)):
                log.warning("send %s -> %s failed: %s", type(msg).__name__, addr, e)
            # a command ack that does not fit one datagram still has to reach
            # the console, or the command reads as never acked and is retried
            # until it times out: send the status with the short fields only
            if isinstance(msg, CmdAck) and isinstance(e, proto.ProtoError):
                small = trim_ack_result(msg.cmd_id, msg.status, msg.result or {})
                try:
                    self.seq = (self.seq + 1) & 0xFFFFFFFF
                    self.transport.sendto(
                        proto.encode(CmdAck(msg.cmd_id, msg.status, small), self.seq), addr)
                except (OSError, proto.ProtoError) as e2:
                    log.warning("trimmed ack -> %s failed too: %s", addr, e2)


# Safety and health fields of an ack: the last to go when it has to be trimmed
ACK_KEEP_LONGEST = ("hpa_monitor", "ports", "guard", "pa", "tr", "atten", "devices", "baro",
                    "host_power", "card_temps", "imu", "fault")
# Display extras nothing keys on: the first to go
ACK_DROP_FIRST = ("secure", "log", "fpga")
_ack_trim_logged = None


def _utc_text(t) -> str:
    """The radio's own clock as text for its log files, always UTC."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t))


def trim_ack_result(cmd_id, status, full: dict) -> dict:
    """A result cut down to fit one datagram: the longest fields go first, one at a
    time, starting with ACK_DROP_FIRST, the ACK_KEEP_LONGEST ones only after every
    other long field has gone."""
    small = dict(full)
    dropped = []

    def size(k):
        return len(json.dumps(small[k], separators=(",", ":"), default=str))

    def fits():
        note = dict(small, truncated=True, dropped_keys=sorted(dropped)[:20])
        try:
            proto.encode(CmdAck(cmd_id, status, note), 0)
            return True
        except proto.ProtoError:
            return False
    while small and not fits():
        first = [k for k in ACK_DROP_FIRST if k in small]
        spare = [k for k in small if k not in ACK_KEEP_LONGEST] or list(small)
        k = first[0] if first else max(spare, key=size)
        del small[k]
        dropped.append(k)
    small["truncated"] = True
    small["dropped_keys"] = sorted(dropped)[:20]
    global _ack_trim_logged
    what = ",".join(sorted(dropped))[:200]
    if what != _ack_trim_logged:         # status polls repeat this every second during TX
        log.warning("ack trimmed to fit: dropped %s", what)
        _ack_trim_logged = what
    return small


class RadioService:
    def __init__(self, *, serial, host_type, kind, simulate,
                 cmd_port, discovery_port, broadcast,
                 frontend_config=None, frontend_overrides=None, role=None,
                 fpga_runner=None):
        self.serial = serial
        # radio role: kept per radio across restarts; --role only seeds it
        _sfile = _re.sub(r"[^A-Za-z0-9_.-]", "_", serial or "radio") + ".json"
        self._role_path = _os.path.join(_data_dir("role"), _sfile)
        _r = _role.load(self._role_path, default=role or _role.DEFAULT)
        self._role, self._role_by = _r["role"], _r["by"]
        # FPGA image: last probe (x4xx) or chosen file (b2xx), and the load job
        self._fpga_path = _os.path.join(_data_dir("fpga"), _sfile)
        # a marker left by an init that never returned
        self._init_mark_path = _os.path.join(_data_dir("state"), "init_" + _sfile)
        self._crashed_init = self._read_init_marker()
        self._fpga_job = None
        self._fpga_find = (0.0, None)          # (monotonic, find result) cache
        self._fpga_runner = fpga_runner        # None: real tools, or the sim X410
        self._caps_image = None                # x4xx image the last caps reply was built for
        # Pin map comes from rf_frontend.yaml (path overridable), CLI
        # flags only override it; nothing is touched until _frontend_start().
        self._fe_path = frontend_config
        self._fe_overrides = dict(frontend_overrides or {})
        self._fe_cfg = None
        # port map {jack: {role, chain}} from the file; _port_err = the file did
        # not load, so the map is unknown and the radio is refused until it does
        self._port_map = {}
        self._port_src = None
        self._port_err = None
        self._port_by = None             # who last changed the map, for the views' logs
        self.frontend = None             # rf_frontend.RfFrontend | None
        self.atten = None                # mc_atten.MiniCircuitsAtten | None
        self.guard = None                # Payload_guard.RxGuard | None
        self._tx_atten_db = None         # Last set_tx_atten (dB) for payload_status
        # atten.transport auto attaches the attenuator when it is
        # plugged in; the switch readback ADC is probed for presence only.
        # _atten_lock serialises attenuator access when there is no guard (the
        # guard's own lock does it when there is one, see _at_lock).
        self._atten_lock = threading.RLock()
        self._devwatch = None            # device_watch.DeviceWatcher | None
        self._atten_watch = None         # device_watch.AttenWatch | None
        self._adc_probe = None           # device_watch.AdcProbe | None
        self.host_type = int(host_type)
        self.kind = kind
        self.simulate = simulate
        self.cmd_port = cmd_port
        self.discovery_port = discovery_port
        self.broadcast = broadcast

        self.ep: _Endpoint | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.executor = ThreadPoolExecutor(max_workers=2,
                                            thread_name_prefix="svc-hw")

        # radio state
        self.backend = None
        self.sched = None
        self.psd = None                  # pt.PSDRecorder, created on init
        self._init_sig = None            # json signature of last applied init
        # last-loaded TX source, so start_tx can re-arm the scheduler if the
        # waveform was loaded before init or lost across a re-init
        self._tx_src = None
        self._tx_wf_desc = None
        # Reason TX is locked out by the current RX port, else None
        self._x40_tx_lock = None
        # wedge watchdog counters (reported by the wedge_status command)
        self.wedge_stats = {"episodes": 0, "self_cleared": 0, "stream_restarts": 0,
                            "reinits": 0, "giveups": 0, "last": None}

        # command plane
        # (sender addr, cmd_id): ack (idempotency, APPLIED_MAX newest). Keyed per
        # sender: each GUI numbers its own commands, so two GUIs (or a GUI and a
        # bench script) can use the same cmd_id: a bare cmd_id key would replay one
        # client's ack to the other without applying its command
        self.applied: dict[tuple, CmdAck] = {}
        self.in_progress: set[tuple] = set()   # (addr, cmd_id) being applied now
        self.last_cmd_id = 0
        # Get_config state the GUIs read back
        self._cfg_seq = 0                      # applied state-changing commands
        self._ant_bias_last = None             # last G40 GPS antenna bias readback
        self._ant_bias_t = 0.0                 # monotonic time of that readback
        self._atten_db_last = None             # last attenuator set/readback (dB)
        self._psd_last_seq = None              # last RX frame turned into a PSD

        # telemetry fan-out: everyone who has HELLO'd
        self.gui_addrs: set[tuple] = set()

        # detection plane: EnergyDetector + BurstTracker feed a
        # bounded ring keyed by monotonically increasing det_index. The ring
        # answers DETECTION_REQ gap-fills; entries older than DET_RING_MAX
        # ago are gone (the GUI's reconciliation just leaves that gap).
        self.det = None                        # ed.EnergyDetector
        self._det_log_fp = None                # open CSV when log_detections is on
        self._telem_log_fp = None              # open CSV when log_telemetry is on
        self._ref_locked = None                # cached GPSDO lock (None=N/A) for STATUS
        # G40 antenna bias (PCA9685 ant-bias-en) + gpsd sky status.
        # Real only on kind g40; --simulate (and the mock kind) fakes both.
        self._gps_sim = (_ga.SimGpsAntenna()
                         if (kind == "mock" or (kind == "g40" and simulate)) else None)
        self._gps_real = (kind == "g40" and not simulate)
        # X40: its GNSS receiver is on the ZynqMP, gpsd reachable from the Jetson;
        # the bias is the ZynqMP's (on at boot) and not the service's to switch or read
        if kind == "x40" and simulate:
            self._gps_sim = _ga.SimGpsAntenna(bias_on=True)
        self._gnss_addr = (_ga.GPSD_DEFAULT if self._gps_real else
                           X40_GPSD if (kind == "x40" and not simulate) else None)
        self._gnss_last = None                 # compact gnss for get_config
        self._gnss_t = -1e9
        self.tracker = None                    # ed.BurstTracker
        self.chan = None                       # ChannelizerWorker (GPU), when a plan is loaded
        self.plan = None                       # channel_plan dict; targeted decode
        self._plan_anchor = None               # "tx" = follow this radio's TX LO
        self.det_head = 0                      # next det_index to assign
        self.det_ring: dict[int, WireDetection] = {}
        self._det_params = {}                  # from init cmd
        # RX overload protection
        self._clip_ewma = 0.0                  # smoothed clip fraction
        self._rx_overload = False              # surfaced in STATUS (FLAG_RX_OVERLOAD)
        self._overload_steps = 0               # auto back-off steps this episode
        self._last_backoff_t = 0.0             # monotonic time of last gain step
        self._auto_gain_backoff = True         # step gain down on sustained overload
        self._det_last_ts = None               # frame dedupe (rf_timestamp)
        self._det_max_ts = -1                  # newest rf_timestamp processed (monotonic gate)
        self._det_suspended = False            # True while detection paused on degraded RX
        self._det_lock = asyncio.Lock()        # process vs flush/retune

        # transceive plane (modem RX to decode to TX-response over the channelizer).
        # Disarmed by default: the tx_callback logs the would-be reply and does
        # not key TX until an explicit armed transceive cmd (spectrum analyzer
        # on the TX port: standing specA rule).
        self._tx_armed = False
        self._modem_cfg = None                 # reply waveform config
        # bitpipe stream link (dpmg/bitpipe/modem_link.py): a ModemLink between a
        # container's bit face and this radio's TX source / RX frame hook
        self._bitpipe = None
        self._bitpipe_prev_hook = None
        self._tx_busy = False                  # guard overlapping reply bursts

        # FILE_* chunked transfer (large plans / waveforms > MAX_DATAGRAM).
        # _file_rx holds in-flight transfers; _file_done maps completed
        # transfer names: written path (set_plan {plan_inbox: name} uses it).
        self._file_rx: dict[tuple, dict] = {}  # (addr, file_id): {name,total,bytes,chunks}
        self._file_done: dict[str, str] = {}   # name: absolute written path

        # One-shot raw-IQ grab for extract_region. Filled by a
        # chained frame_hook while active, then carved to one band + written.
        self._cap = None                       # dict while a carve is capturing
        # One raw-IQ capture at a time. Concurrent capture commands (armed
        # checkout + channelizer test, or two views) would share self._cap: the second
        # would overwrite / clear the first's buffer mid-capture (TypeError, TX left keyed)
        self._cap_lock = asyncio.Lock()

        self.t0 = time.monotonic()

    # lifecycle

    async def serve(self, stop: asyncio.Event | None = None):
        """Bind the UDP socket and run the telemetry loops until `stop` is set
        (or forever if no stop event is given). create_datagram_endpoint hands
        back the protocol instance, which is the bound _Endpoint."""
        self.loop = asyncio.get_running_loop()
        transport, protocol = await self.loop.create_datagram_endpoint(
            lambda: _Endpoint(self),
            local_addr=("0.0.0.0", self.cmd_port),
            allow_broadcast=True)
        self.ep = protocol
        log.info("service %s (%s) up: cmd_port=%d announce->%s:%d simulate=%s",
                 self.serial, self.kind, self.cmd_port,
                 self.broadcast, self.discovery_port, self.simulate)
        await self._run(self._frontend_start)          # front end (no-op when none)
        self._secure_start()                           # secure messages (no keys on the radio)
        tasks = [asyncio.create_task(self._announce_loop()),
                 asyncio.create_task(self._status_loop()),
                 asyncio.create_task(self._psd_loop()),
                 asyncio.create_task(self._det_loop()),
                 asyncio.create_task(self._ref_loop()),
                 asyncio.create_task(self._clock_loop()),
                 asyncio.create_task(self._imu_loop()),
                 asyncio.create_task(self._baro_loop()),
                 asyncio.create_task(self._watch_loop()),
                 asyncio.create_task(self._wedge_loop())]
        if self._fpga_family() == "x4xx":                   # FPGA image: read it once at start
            tasks.append(asyncio.create_task(self._fpga_find_start()))
        try:
            if stop is not None:
                await stop.wait()
            else:
                await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            await self._teardown()
            await self._run(self._frontend_stop)       # PA LOW, lines released
            transport.close()

    # inbound dispatch

    def _origin_ok(self, addr):
        """Opt-in source-IP allowlist for the UDP command plane. Set env
        DPMG_ALLOW to a comma-separated list of IPs/CIDRs (e.g. the GUI host or
        the VPN subnet) to reject datagrams from anywhere else. Unset = no
        restriction. This is defense-in-depth on top of the
        network controls (VPN/firewall), not a replacement for them."""
        nets = getattr(self, "_allow_src", None)
        if nets is None:
            nets = []
            for a in _os.environ.get("DPMG_ALLOW", "").split(","):
                a = a.strip()
                if a:
                    try:
                        nets.append(_ipaddress.ip_network(a, strict=False))
                    except ValueError:
                        log.warning("DPMG_ALLOW: bad entry %r ignored", a)
            self._allow_src = nets
        if not nets:
            return True
        try:
            ip = _ipaddress.ip_address(addr[0])
        except ValueError:
            return False
        return any(ip in n for n in nets)

    def on_msg(self, mtype, seq, msg, addr):
        if not self._origin_ok(addr):
            return                       # not an allowed control source: drop
        if mtype == MsgType.HELLO:
            ok = 1 if msg.proto_version == proto.VERSION else 0
            if ok:
                self.gui_addrs.add(addr)
                log.info("session open with gui %s (id=%s)", addr, msg.gui_id)
            self.ep.send(HelloAck(self.serial, ok), addr)

        elif mtype == MsgType.CMD:
            self._on_cmd(msg, addr)

        elif mtype == MsgType.DETECTION_REQ:
            # gap-fill replay from the ring: contiguous run [start, start+n).
            # Indices that have aged out of the ring are silently absent:
            # the GUI reconciles against a snapshot and tolerates holes.
            n = min(msg.count, DET_REQ_MAX)
            for i in range(msg.start_index, msg.start_index + n):
                det = self.det_ring.get(i)
                if det is not None:
                    self.ep.send(det, addr)

        elif mtype == MsgType.FILE_OFFER:
            self._on_file_offer(msg, addr)

        elif mtype == MsgType.FILE_CHUNK:
            self._on_file_chunk(msg, addr)

    # FILE_* chunked transfer (gui: service, cumulative ack)

    def _on_file_offer(self, msg, addr):
        key = (tuple(addr), msg.file_id)       # file ids are per GUI
        f = self._file_rx.get(key)
        if f is not None and f.get("hc", -1) >= f["total"] - 1:
            # duplicate offer for a completed transfer: replay the final ack
            self.ep.send(FileAck(msg.file_id, f["hc"]), addr)
            return
        # basename only: a pushed name must never escape the inbox dir
        name = _os.path.basename(msg.name) or "unnamed"
        self._file_rx[key] = {"name": name, "total": msg.total_chunks,
                                      "bytes": msg.total_bytes,
                                      "chunks": {}, "hc": -1}
        log.info("file offer %d: %s (%d chunks, %d bytes)",
                 msg.file_id, name, msg.total_chunks, msg.total_bytes)
        self.ep.send(FileAck(msg.file_id, -1), addr)

    def _on_file_chunk(self, msg, addr):
        key = (tuple(addr), msg.file_id)
        f = self._file_rx.get(key)
        if f is None:
            return                             # no offer seen; sender re-offers
        done_before = f["hc"] >= f["total"] - 1
        f["chunks"][msg.chunk_idx] = msg.payload
        while f["hc"] + 1 in f["chunks"]:
            f["hc"] += 1
        self.ep.send(FileAck(msg.file_id, f["hc"]), addr)
        if not done_before and f["hc"] >= f["total"] - 1:
            self._file_complete(key, f)

    def _file_complete(self, key, f):
        fid = key[1]
        data = b"".join(f["chunks"][i] for i in range(f["total"]))
        if len(data) != f["bytes"]:
            log.warning("file %d (%s): size mismatch %d != %d, discarding",
                        fid, f["name"], len(data), f["bytes"])
            self._file_rx.pop(key, None)
            return
        inbox = _data_dir("inbox")
        path = _os.path.join(inbox, f["name"])
        with open(path, "wb") as fh:
            fh.write(data)
        f["chunks"].clear()                    # keep hc for ack replay, drop data
        self._file_done[f["name"]] = path
        log.info("file %d complete: %s (%d bytes) -> %s",
                 fid, f["name"], len(data), path)

    def _on_cmd(self, cmd, addr):
        # Duplicate of a completed command: replay the stored ack.
        key = (tuple(addr), cmd.cmd_id)
        if key in self.applied:
            self.ep.send(self.applied[key], addr)
            return
        # Duplicate of an in-flight command: ignore; sender will retry and
        # get the real ack once apply() finishes. Prevents double-open.
        if key in self.in_progress:
            return
        self.in_progress.add(key)
        asyncio.create_task(self._apply_and_ack(cmd, addr))

    async def _apply_and_ack(self, cmd, addr):
        try:
            ack = await self._apply(cmd)
        except Exception as e:                     # never let one cmd kill svc
            log.exception("cmd %s (%s) failed", cmd.cmd_id, cmd.name)
            ack = CmdAck(cmd.cmd_id, CmdStatus.FAILED, {"err": str(e)})
        key = (tuple(addr), cmd.cmd_id)
        self.applied[key] = ack
        while len(self.applied) > APPLIED_MAX:     # insertion order: drop the oldest
            self.applied.pop(next(iter(self.applied)))
        self.in_progress.discard(key)
        if ack.status == CmdStatus.OK:
            self.last_cmd_id = max(self.last_cmd_id, cmd.cmd_id)
            if cmd.name in _STATE_CMDS:
                self._cfg_seq += 1
        self.ep.send(ack, addr)

    # command application (absolute setpoints, idempotent)

    async def _apply(self, cmd) -> CmdAck:
        name, p = cmd.name, cmd.params
        cid = cmd.cmd_id

        # the role refuses what it does not use, for every client
        why = _role.refusal(self._role, name,
                            live_tx_source=getattr(self.sched, "tx_source", None) is not None)
        if why:
            return CmdAck(cid, CmdStatus.REJECTED, {"err": why, "role": self._role})
        # the radio is off limits while an image loads
        if self._fpga_busy() and name not in _role.ALWAYS and name != "fpga_load":
            return CmdAck(cid, CmdStatus.REJECTED,
                          {"err": "FPGA image load in progress", "fpga_busy": True})
        # a jack is used only for what its config says, for every client
        why = self._port_refusal(name, p)
        if why:
            return CmdAck(cid, CmdStatus.REJECTED, {"err": why, "port_role": True})
        if name == "set_role":
            return await self._cmd_set_role(cid, p)
        if name == "fpga_status":
            return await self._cmd_fpga_status(cid, p)
        if name == "fpga_load":
            return await self._cmd_fpga_load(cid, p)

        if name == "init":
            return await self._cmd_init(cid, p)
        if name == "caps":
            return await self._cmd_caps(cid, p)
        if name == "sensors":
            return await self._cmd_sensors(cid, p)
        if name == "wedge_status":
            return CmdAck(cid, CmdStatus.OK, dict(self.wedge_stats, enabled=WEDGE_WATCHDOG))
        if name == "selftest_rx_wedge":
            return await self._cmd_selftest_rx_wedge(cid, p)
        if name == "set_center":
            return await self._cmd_set_center(cid, p)
        if name == "set_tx_center":
            return await self._cmd_set_tx_center(cid, p)
        if name == "set_gain":
            return await self._cmd_set_gain(cid, p)
        if name == "set_tx_atten":
            return await self._cmd_set_tx_atten(cid, p)
        if name == "start_rx":
            return await self._cmd_start_rx(cid)
        if name == "stop_rx":
            return await self._cmd_stop_rx(cid)
        if name == "set_plan":
            return await self._cmd_set_plan(cid, p)
        if name == "set_tx_waveform":
            return await self._cmd_set_tx_waveform(cid, p)
        if name == "start_tx":
            return await self._cmd_start_tx(cid, p)
        if name == "stop_tx":
            return await self._cmd_stop_tx(cid)
        if name == "transceive":
            return await self._cmd_transceive(cid, p)
        if name == "bitpipe_start":
            return await self._cmd_bitpipe_start(cid, p)
        if name == "bitpipe_stop":
            return await self._cmd_bitpipe_stop(cid, p)
        if name == "bitpipe_status":
            return await self._cmd_bitpipe_status(cid, p)
        if name == "extract_region":
            return await self._cmd_extract_region(cid, p)
        if name == "gen_test_waveform":
            return await self._cmd_gen_test_waveform(cid, p)
        if name == "gen_scenario":
            return await self._cmd_gen_scenario(cid, p)
        if name == "waveform_rate":
            return await self._cmd_waveform_rate(cid, p)
        if name == "resample_waveform":
            return await self._cmd_resample_waveform(cid, p)
        if name == "list_waveforms":
            return await self._cmd_list_waveforms(cid, p)
        if name == "list_dir":
            return await self._cmd_list_dir(cid, p)
        if name == "log_detections":
            return await self._cmd_log_detections(cid, p)
        if name == "log_telemetry":
            return await self._cmd_log_telemetry(cid, p)
        if name == "ground_checkout":
            return await self._cmd_ground_checkout(cid, p)
        if name == "frontend_status":
            return await self._cmd_frontend_status(cid, p)
        if name == "frontend_set":
            return await self._cmd_frontend_set(cid, p)
        if name == "frontend_reload":
            return await self._cmd_frontend_reload(cid, p)
        if name == "set_ports":
            return await self._cmd_set_ports(cid, p)
        if name == "atten_set":
            return await self._cmd_atten_set(cid, p)
        if name == "atten_get":
            return await self._cmd_atten_get(cid, p)
        if name == "payload_status":
            return await self._cmd_payload_status(cid, p)
        if name == "payload_key":
            return await self._cmd_payload_key(cid, p)
        if name == "payload_restore":
            return await self._cmd_payload_restore(cid, p)
        if name == "channelizer_test":
            return await self._cmd_channelizer_test(cid, p)
        if name == "capture_iq":
            return await self._cmd_capture_iq(cid, p)
        if name == "record_iq":
            return await self._cmd_record_iq(cid, p)
        if name == "list_recordings":
            return await self._cmd_list_recordings(cid, p)
        if name == "delete_recording":
            return await self._cmd_delete_recording(cid, p)
        if name == "replay_recording":
            return await self._cmd_replay_recording(cid, p)
        if name == "classify_selection":
            return await self._cmd_classify_selection(cid, p)
        if name == "survey_classify":
            return await self._cmd_survey_classify(cid, p)
        if name == "transponder":
            return await self._cmd_transponder(cid, p)
        if name == "gps_status":
            return await self._cmd_gps_status(cid, p)
        if name == "set_time":
            return self._cmd_set_time(cid, p)
        if name == "gps_antenna_bias":
            return await self._cmd_gps_antenna_bias(cid, p)
        if name == "imu_set_level":
            return self._cmd_imu_set_level(cid, p)
        if name == "secure_status":
            return await self._cmd_secure_status(cid, p)
        if name == "secure_enable":
            return await self._cmd_secure_enable(cid, p)
        if name == "secure_keys_reload":
            return await self._cmd_secure_keys_reload(cid, p)
        if name == "secure_sim_inject":                     # simulator aid
            return await self._cmd_secure_sim_inject(cid, p)
        if name == "get_config":
            return await self._cmd_get_config(cid, p)
        if name == "close":
            return await self._cmd_close(cid)

        return CmdAck(cid, CmdStatus.REJECTED, {"err": f"unknown cmd {name}"})

    # port map

    def _kind_for(self, init) -> str:
        """The radio kind an init runs as: the service's own, or the GUI's hint
        when this service has no fixed kind."""
        return (init or {}).get("kind", self.kind) if self.kind == "mock" else self.kind

    def _session_init(self) -> dict:
        try:
            return json.loads(self._init_sig) if self._init_sig else {}
        except ValueError:
            return {}

    def _port_refusal(self, name, p):
        """Why this command would use a jack against the port map, or None. The
        init is checked as sent. Anything that keys or listens later is checked
        against the jacks the radio is on, because frontend_reload can change the
        map under an open radio and an RX only init leaves TX on the default jack."""
        if not self._port_map and not self._port_err:
            return None
        if name == "init":
            sides = {"rx", "tx"}
        else:
            sides = _ports.sides_used(name, p)
            if not sides or self.backend is None:
                return None
        if self._port_err:
            return (f"the port map is unknown because the front-end config did not "
                    f"load: {self._port_err}")
        if name == "init":
            return _ports.init_refusal(self._port_map, self._kind_for(p), p, self._port_src)
        init = self._session_init()
        return _ports.session_refusal(self._port_map, self._kind_for(init), init, sides,
                                      self._port_src,
                                      getattr(self.backend.config, "tx_antenna", None))

    def _ports_report(self) -> dict:
        """The whole map, chain text included, for get_config with ports=true.
        Too big for the snapshot a GUI polls every second, which carries only the
        roles and a version number. Fits one CMD_ACK (port_map.CHAIN_MAX)."""
        return {"ports_full": self._port_map, "ports_v": _ports.version(self._port_map),
                "ports_err": (self._port_err or "")[:200] or None,
                "ports_source": str(self._port_src or "")[-80:] or None,
                "ports_by": self._port_by}

    def _port_conflict_now(self):
        """Why the open radio's own jacks break the map, or None."""
        if self.backend is None:
            return None
        init = self._session_init()
        why = _ports.session_refusal(self._port_map, self._kind_for(init), init,
                                     {"rx", "tx"} if init.get("tx_antenna") else {"rx"})
        return why[:200] if why else None

    async def _cmd_set_ports(self, cid, p):
        """Change the port map from a GUI: {ports: {jack: {role, chain}}, by} or
        {clear: true, by}. Writes the ports: section of this radio's own
        rf_frontend.yaml, the rest of the file untouched, and applies it at once with
        no front-end reload, so the PA and switch lines are left alone. The file stays
        the one source of truth. Refused while TX runs, so a lock never changes under
        a transmitter."""
        clear = bool(p.get("clear"))
        if not clear and not p.get("ports"):
            return CmdAck(cid, CmdStatus.REJECTED,
                          {"err": "give ports, or clear to remove the port map"})
        try:
            pm = {} if clear else _ports.parse(p.get("ports"))
        except ValueError as e:
            return CmdAck(cid, CmdStatus.REJECTED, {"err": f"ports: {e}"})
        bad = _ports.unknown_jack(self.kind, pm, sdr.backend_caps(self.kind))
        if bad is not None:
            have = _ports.jacks_of(self.kind, sdr.backend_caps(self.kind))
            return CmdAck(cid, CmdStatus.REJECTED,
                          {"err": f"unknown jack {bad!r}: this {_ports.kind_text(self.kind)} has "
                                  f"{', '.join(have)}"})
        if pm == self._port_map and not self._port_err:
            return CmdAck(cid, CmdStatus.OK, dict(self._ports_report(), noop=True))
        if self._tx_running():
            return CmdAck(cid, CmdStatus.REJECTED, {"err": "stop TX before changing the port map"})
        by = str(p.get("by") or "")[:40] or "?"
        stamp = f"{by}, {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}"
        path = self._fe_path or _rf.default_config_path()
        try:
            cfg = await self._run(_rf.write_ports, path, pm, stamp)
        except (_rf.ConfigError, OSError) as e:
            return CmdAck(cid, CmdStatus.FAILED, {"err": str(e)[:300]})
        prev = _ports.compact(self._port_map)
        self._port_map, self._port_src, self._port_err = cfg.ports, path, None
        self._port_by = stamp
        if self._fe_cfg is not None:
            self._fe_cfg.ports = cfg.ports
        log.warning("port map %s changed to %s by %s (%s)", prev or "none",
                    _ports.compact(cfg.ports) or "none", by, path)
        res = self._ports_report()
        why = self._port_conflict_now()
        if why:
            res["ports_conflict"] = why
        return CmdAck(cid, CmdStatus.OK, res)

    async def _run(self, fn, *a):
        """Offload a blocking backend call to the hw thread pool."""
        return await self.loop.run_in_executor(self.executor, fn, *a)

    async def _cmd_caps(self, cid, p):
        """Report this radio's capability descriptor so the GUI renders the
        controls appropriate to the connected SDR (ports, gain units, clock
        sources) instead of assuming a Sidekiq."""
        caps = sdr.backend_caps(self.kind)
        caps["serial"] = self.serial
        caps["host_type"] = int(self.host_type)
        # the jacks the lockout panel lists for this radio; empty
        # means the kind has no port map support
        caps["port_jacks"] = _ports.jacks_of(self.kind, caps)
        if self._fpga_family() == "x4xx" and not self._fpga_busy():
            # an X410's rates and channels follow the image it runs
            dev = await self._fpga_find_now(5.0)
            img = (dev or {}).get("fpga")
            caps = _fpga.x4xx_caps(img, caps)
            self._caps_image = img
        return CmdAck(cid, CmdStatus.OK, caps)

    async def _cmd_sensors(self, cid, p):
        """Live front-end sensors (ref/LO lock, RSSI, temp) for the GUI status
        row. Empty dict if the backend has no sensors or isn't open."""
        b = self.backend
        fn = getattr(b, "read_sensors", None) if b is not None else None
        try:
            data = fn() if fn else {}
        except Exception as e:
            data = {"err": str(e)}
        return CmdAck(cid, CmdStatus.OK, data)

    async def _cmd_init(self, cid, p):
        sig = json.dumps(p, sort_keys=True)
        # idempotent: identical init while already open is a no-op
        if sig == self._init_sig and self.backend is not None:
            return CmdAck(cid, CmdStatus.OK,
                          {"serial": self.serial, "noop": True,
                           "achieved_rate": self.backend.config.sample_rate})
        # one init at a time. a slow open (external clock with no reference blocks
        # the driver for a while) must not let the client's init resends stack up.
        if getattr(self, "_init_busy", False):
            return CmdAck(cid, CmdStatus.REJECTED, {"err": "init already in progress"})
        refusal = self._crashed_init_refusal(p)
        if refusal is not None:
            return CmdAck(cid, CmdStatus.REJECTED, refusal)
        self._init_busy = True
        self._write_init_marker(cid, p)
        try:
            ack = await self._do_init(cid, p, sig)
        finally:
            self._init_busy = False
            self._clear_init_marker()
        if ack.status == CmdStatus.OK:
            self._crashed_init = None           # a good init ends the hold
        return ack

    # init crash guard

    def _read_init_marker(self):
        """The marker of an init that was running when the last process died, or
        None. Read once at start and removed, so it counts one crash only."""
        try:
            with open(self._init_mark_path) as fh:
                d = json.load(fh) or {}
        except (OSError, ValueError):
            return None
        try:
            _os.remove(self._init_mark_path)
        except OSError:
            pass
        clk = d.get("clock_source") or "default"
        log.warning("the last run stopped during an init (clock %s, %s): that init is "
                    "refused for %d s unless it is sent with force_after_crash",
                    clk, time.strftime("%H:%M:%S UTC", time.gmtime(d.get("t", 0))),
                    int(INIT_CRASH_HOLD_S))
        return d

    def _write_init_marker(self, cid, p):
        try:
            tmp = self._init_mark_path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump({"t": time.time(), "cmd_id": cid, "kind": p.get("kind"),
                           "clock_source": p.get("clock_source")}, fh)
            _os.replace(tmp, self._init_mark_path)
        except OSError:
            log.exception("init marker not written")
        if str(p.get("clock_source")) == "external":
            log.info("init on clock external: if the service stops now, the card did "
                     "not lock to its reference")

    def _clear_init_marker(self):
        try:
            _os.remove(self._init_mark_path)
        except OSError:
            pass

    def _crashed_init_refusal(self, p):
        """Refusal for an init like the one that stopped the last process, within
        INIT_CRASH_HOLD_S of it; None to go ahead. Another clock source, or
        force_after_crash, goes ahead."""
        ci = self._crashed_init
        if not ci or p.get("force_after_crash"):
            return None
        if time.time() - float(ci.get("t", 0)) > INIT_CRASH_HOLD_S:
            return None
        if (p.get("clock_source") or "default") != (ci.get("clock_source") or "default"):
            return None
        clk = ci.get("clock_source") or "default"
        hint = (": check the 10 MHz on REF and the 1 PPS, or init on 1 PPS only, internal "
                "or gpsdo" if clk in ("external", "ext_10mhz") else "")
        return {"err": f"the last init with clock {clk} stopped the radio service{hint}. "
                       "Send it again with force_after_crash to retry it.",
                "crashed_init": {"clock_source": clk, "t": ci.get("t")}}

    async def _do_init(self, cid, p, sig):
        # A separate TX rate needs TX on the other card (own clock)
        _exr = p.get("extras") or {}
        _txr = float(_exr.get("tx_sample_rate", 0) or 0)
        if _txr > 0 and abs(_txr - float(p.get("sample_rate") or 0)) >= 1.0:
            _tc = _exr.get("tx_card")
            if _tc is None or int(_tc) == int(_exr.get("card", 0)) or not p.get("tx_antenna"):
                return CmdAck(cid, CmdStatus.REJECTED,
                              {"err": "a TX sample rate different from RX needs TX on "
                                      "the other card (cross-card mode)"})
        # A dual-chan_mode (fixed-port) init with RX on RXA2 and TX
        # on TXA1/TXB1 of the same card can never stream RX (J2 is the TX jack in
        # fixed mode). Refuse it before tearing anything down, with the same text
        # the GUI shows. Single chan_mode (TDD) and cross-card pairs pass through.
        _kind0 = p.get("kind", self.kind) if self.kind == "mock" else self.kind
        # the clock choice is one of g40_clock's; an X40 takes three of them, a G40
        # (and the simulator standing in for one) all five. Refused here, before
        # the running radio is torn down, with the text that says what to pick.
        if _kind0 in ("g40", "x40", "mock") and p.get("clock_source") is not None:
            _ck = _g40_clock.resolve(p.get("clock_source"), "x40" if _kind0 == "x40" else "g40")
            if _ck.get("err"):
                return CmdAck(cid, CmdStatus.REJECTED, {"err": _ck["err"]})
        if _kind0 == "g40":
            _ex0 = p.get("extras") or {}
            _c = sdr.g40_fixed_mode_conflict(
                p.get("rx_antenna"), p.get("tx_antenna"),
                "dual" if sdr.needs_dual_channel(str(p.get("rx_antenna") or ""),
                                                 str(p.get("tx_antenna") or ""), _ex0)
                else "single",
                _ex0.get("card", 0), _ex0.get("tx_card"))
            if _c:
                return CmdAck(cid, CmdStatus.REJECTED, {"err": _c, "g40_same_card": True})
        # an X410 must run an image DPMG has a table for, the same
        # image the GUI built its controls from, at one of that image's rates
        if _fpga.family(_kind0) == "x4xx":
            dev = await self._fpga_find_now(0.0)
            img = (dev or {}).get("fpga")
            why = _fpga.x4xx_init_refusal(img, self._caps_image, p.get("sample_rate"))
            if why:
                if img and self._caps_image and img != self._caps_image:
                    self._caps_image = img
                    self._cfg_seq += 1          # GUIs re-read get_config, then caps
                return CmdAck(cid, CmdStatus.REJECTED,
                              {"err": why, "fpga_image": img, "caps_changed": True})
            # the backend checks the radio opens on the clock this image implies
            p = dict(p, extras=dict(p.get("extras") or {}, fpga_image=img,
                                    fpga_mcr_hz=_fpga.X4XX_IMAGES[img]["mcr_hz"]))
        # re-init: tear down any existing session first
        if self.backend is not None:
            await self._teardown()

        kw = {k: p[k] for k in _CFG_FIELDS if k in p}
        # a B200 opens with the image chosen in the FPGA panel
        if _fpga.family(_kind0) == "b2xx" and not _fpga.args_get(kw.get("device_args"), "fpga"):
            _img = self._fpga_state().get("image")
            if _img:
                kw["device_args"] = _fpga.args_set(kw.get("device_args") or "type=b200", "fpga",
                                                   _os.path.join(_fpga.uhd_images_dir(), _img))
        extras = dict(p.get("extras", {}))
        try:
            cfg = sdr.SDRConfig(**kw, extras=extras).validate()
        except (TypeError, sdr.SDRConfigError) as e:
            return CmdAck(cid, CmdStatus.REJECTED, {"err": f"bad config: {e}"})

        # the radio service knows its own hardware (set via --kind at launch);
        # the GUI's kind is only a hint, honored when this service was started
        # without a fixed kind (mock). Otherwise a GUI default (e.g. g40) must
        # not force the wrong backend onto a b200/x40 radio.
        kind = p.get("kind", self.kind) if self.kind == "mock" else self.kind
        # An A-side RX (RXA1/J2, RXA2/J1) shares its LO with the only
        # transmitter (TXA1/J5). Init is allowed (receive-only on those ports is a
        # legitimate use) but TX stays locked until a re-init on a B-side RX port;
        # start_tx, transceive, armed checkout and armed transponder all check it.
        self._x40_tx_lock = (sdr.x40_same_side_conflict(cfg.rx_antenna, cfg.tx_antenna)
                             if kind == "x40" else None)
        backend = sdr.make_backend(kind, cfg, simulate=self.simulate)
        try:
            await self._run(backend.open)
        except sdr.SDRConnectionError as e:
            return CmdAck(cid, CmdStatus.FAILED, {"err": f"open failed: {e}"})

        # libsidekiq runtime version, for cross-version bug triage on the
        # ground GUI (G40 runs 4.19.1, X40 4.21: behavior differs)
        lib = None
        try:
            skiq = getattr(backend, "_skiq", None)
            if skiq is not None:
                lib = skiq.lib_version()
        except Exception:
            pass

        self.backend = backend
        self._tx_atten_db = None                  # the new init's tx_gain is in force now
        backend.frontend = self.frontend          # None: strict no-op
        self.sched = sdr.FullDuplexScheduler(backend, tx_mode=sdr.TXMode.STOPPED)
        self.psd = pt.PSDRecorder(
            nfft=int(p.get("psd_nfft", PSD_DEFAULT_NFFT)),
            window=p.get("psd_window", PSD_DEFAULT_WINDOW),
            buffer_samples=int(p.get("psd_buffer", PSD_DEFAULT_BUFFER)))
        self._det_params = {
            "slot_bw": float(p.get("det_slot_bw", DET_DEFAULT_SLOT_BW)),
            "threshold_db": float(p.get("det_threshold_db",
                                        DET_DEFAULT_THRESHOLD_DB)),
            "nfft": int(p.get("det_nfft", DET_DEFAULT_NFFT)),
            "cluster_margin_db": float(p.get("det_cluster_margin_db",
                                             DET_DEFAULT_CLUSTER_MARGIN_DB)),
            "cluster_gap": int(p.get("det_cluster_gap", DET_DEFAULT_CLUSTER_GAP)),
            "hold_ticks": int(p.get("det_hold_ticks", DET_DEFAULT_HOLD_TICKS)),
            "island_slots": int(p.get("det_island_slots", DET_DEFAULT_ISLAND_SLOTS)),
        }
        self._init_sig = sig
        if self._fpga_family() == "epiq":
            await self._epiq_fpga_capture()     # FPGA panel shows these while closed
        result = {"serial": self.serial,
                  "achieved_rate": backend.config.sample_rate,
                  "center_hz": backend.config.center_freq}
        if lib:
            result["libsidekiq"] = lib
        # cross-check data: versions installed at the canonical device path
        # (run the service with -v /usr/lib/epiq:/usr/lib/epiq:ro so this is
        # the host's tested lib set, not whatever the image bundles)
        import glob as _glob
        inst = sorted(p.rsplit(".so.", 1)[-1]
                      for p in _glob.glob("/usr/lib/epiq/libsidekiq.so.*"))
        if inst:
            result["libsidekiq_installed"] = inst
        if self._x40_tx_lock:
            result["tx_locked"] = True       # GUI shows the lock
            result["warning"] = self._x40_tx_lock
        elif p.get("tx_antenna"):            # allowed, but nothing reaches the jack
            _note = _ports.same_card_tx_note(*_ports.session_jacks(kind, p))
            if _note:
                result["same_card_tx"] = True
                result["warning"] = _note
                log.warning("init: %s", _note)
        rc = getattr(backend, "_ref_clock", None)
        if rc:
            result["ref_clock"] = rc
        txlo = getattr(backend, "_tx_lo", None)
        if txlo:
            result["tx_center_hz"] = txlo      # Config-only echo
        cm = getattr(backend, "_chan_mode", None)
        if cm:
            result["chan_mode"] = cm           # single/dual echo (RX card)
        tcm = getattr(backend, "_tx_chan_mode", None)
        if tcm and tcm != cm:
            result["tx_chan_mode"] = tcm       # TX card's own mode
        # applied gain: index actually latched (after range clamp) + its
        # calibrated dB + the valid index range, so the GUI can show real
        # units (the index is the hardware setting, dB is what the operator reads)
        gi = getattr(backend, "_gain_index", None)
        if gi is not None:
            result["rx_gain_index"] = gi
            result["rx_gain_range"] = list(getattr(backend, "_gain_range",
                                                   ()) or [])
            db = self._gain_db(gi)
            if db is not None:
                result["rx_gain_db"] = db
        rr = getattr(backend, "_rate_range", None)
        if rr:
            result["rx_rate_range_hz"] = list(rr)   # decimator probe
        return CmdAck(cid, CmdStatus.OK, result)

    async def _cmd_set_center(self, cid, p):
        if self.backend is None:
            return CmdAck(cid, CmdStatus.REJECTED, {"err": "not initialized"})
        hz = float(p["hz"])
        live = await self._run(self.backend.set_frequency_live, hz)
        # record the setpoint regardless: it is truthful and takes effect on
        # the next open even when a live retune is unsupported (mock/idle).
        self.backend.config.center_freq = hz
        # a retune moves the slot grid: close open bursts, rebuild detector
        async with self._det_lock:
            if self.det is not None:
                self._flush_bursts()
                self._make_detector()
        if self._plan_anchor == "tx" and self.chan is not None:
            await self._run(self._chan_replan)          # Bands follow TX
        return CmdAck(cid, CmdStatus.OK, {"center_hz": hz, "live": bool(live)})

    async def _cmd_set_tx_center(self, cid, p):
        """Move only the TX frequency of an open radio; RX, the detector and
        the PSD stay where they are. A later set_center keeps this RX/TX gap."""
        if self.backend is None:
            return CmdAck(cid, CmdStatus.REJECTED, {"err": "not initialized"})
        hz = float(p["hz"])
        if not await self._run(self.backend.set_tx_frequency_live, hz):
            return CmdAck(cid, CmdStatus.REJECTED,
                          {"err": f"this radio ({self.kind}) can't move its TX "
                                  f"frequency on its own"})
        if self._plan_anchor == "tx" and self.chan is not None:
            await self._run(self._chan_replan)          # Bands follow TX
        return CmdAck(cid, CmdStatus.OK, {"tx_center_hz": hz,
                                          "center_hz": float(self.backend.config.center_freq)})

    def _gain_db(self, idx):
        """Relative gain in dB above the card's minimum index, from factory
        cal (cal-offset differences are true gain; the absolute offset is a
        power-conversion factor, not front-end gain). None without cal."""
        try:
            be = self.backend
            cal = be._skiq.rx_gain_db(be._card, be._rx_hdl,
                                      int(be.config.center_freq), int(idx))
            gmin = getattr(be, "_gain_range", (None,))[0]
            if cal is None or gmin is None:
                return None
            base = be._skiq.rx_gain_db(be._card, be._rx_hdl,
                                       int(be.config.center_freq), int(gmin))
            if base is None:
                return None
            return round(float(cal) - float(base), 2)
        except Exception:
            return None

    async def _cmd_set_gain(self, cid, p):
        if self.backend is None:
            return CmdAck(cid, CmdStatus.REJECTED, {"err": "not initialized"})
        idx = float(p["index"])
        live = await self._run(self.backend.set_gain_live, idx)
        self.backend.config.rx_gain = idx
        # operator set the gain explicitly: fresh overload episode (don't let
        # an auto back-off immediately walk their choice back down)
        self._overload_steps = 0
        self._clip_ewma = 0.0
        result = {"index": idx, "live": bool(live)}
        db = await self._run(self._gain_db, idx)
        if db is not None:
            result["gain_db"] = db
        return CmdAck(cid, CmdStatus.OK, result)

    def _live_tx_atten_db(self):
        """G40 TX attenuation in force: the last set_tx_atten, else the init
        value (Epiq tx_gain is the attenuation), None before an init."""
        if self._tx_atten_db is not None:
            return self._tx_atten_db
        b = self.backend
        if b is None or getattr(b, "config", None) is None:
            return None
        try:
            return float(b.config.tx_gain)
        except (TypeError, ValueError):
            return None

    async def _cmd_set_tx_atten(self, cid, p):
        if self.backend is None:
            return CmdAck(cid, CmdStatus.REJECTED, {"err": "not initialized"})
        qdb = int(p["qdb"])
        fn = getattr(self.backend, "set_tx_attenuation_qdb", None)
        if fn is None:
            return CmdAck(cid, CmdStatus.REJECTED,
                          {"err": "backend has no TX attenuation"})
        if self.guard is not None:               # payload guard: TX floor while keyed
            why = self.guard.tx_atten_request_ok(qdb / 4.0, self.frontend)
            if why:
                return CmdAck(cid, CmdStatus.REJECTED, {"err": why})
        await self._run(fn, qdb)
        self._tx_atten_db = qdb / 4.0            # Echoed by payload_status
        if self.kind in ("g40", "x40", "mock"):
            self.backend.config.tx_gain = qdb / 4.0   # Epiq tx_gain is the atten
        return CmdAck(cid, CmdStatus.OK, {"qdb": qdb})

    async def _cmd_start_rx(self, cid):
        if self.sched is None:
            return CmdAck(cid, CmdStatus.REJECTED, {"err": "not initialized"})
        # Always start the scheduler drain thread; it guards duplicates
        # itself. Do not gate on health.state: on Epiq hardware
        # EpiqBackend.open() starts the card's RX and reports STREAMING, but
        # the scheduler thread that feeds sched.latest() is separate, and
        # skipping it starves telemetry.
        self._rx_started_t = time.monotonic()
        await self._run(self.sched.start_rx)
        async with self._det_lock:
            if self.det is None:
                self._make_detector()
        if self.plan is not None:
            await self._run(self._chan_start)      # GPU channelizer taps live RX
        return CmdAck(cid, CmdStatus.OK, {})

    async def _cmd_stop_rx(self, cid):
        if self.sched is None:
            return CmdAck(cid, CmdStatus.OK, {})   # nothing to stop
        await self._run(self._chan_stop)
        await self._run(self.sched.stop_rx)
        async with self._det_lock:
            self._flush_bursts()                   # end of capture closes bursts
            self.det = None
            self.tracker = None
        return CmdAck(cid, CmdStatus.OK, {})

    # GPU channelizer (targeted decode; feeds dpmg-gpu over shared IPC)

    def _chan_start(self):
        """Attach the channelizer worker to the live RX frame tap and load the
        pending plan. Blocking (GPU IPC + thread): runs in the hw pool."""
        if not _HAVE_CHAN or self.plan is None or self.sched is None:
            return
        if self.chan is None:
            self.chan = ChannelizerWorker(frame_len=1048576, fs_out=250_000.0)
            self.chan.sealed_callback = self._on_sealed          # Forward, never open
            self.chan.log_key = "g40"
            self.chan.start()
        try:
            self.chan.set_plan(ChannelPlan.from_dict(self._plan_for_chan()))
        except Exception:
            log.exception("channelizer set_plan failed")
        self.sched.frame_hook = self.chan.feed
        self.sched._saved_rx_pace_s = getattr(self.sched, "rx_pace_s", 0.05)
        self.sched.rx_pace_s = 0.02
        self._wire_transceive()      # attach modem responder if configured

    def _plan_tx_lo(self):
        be = self.backend
        if be is None:
            return None
        try:                                               # no TX on this init: RX centre
            _tx = (json.loads(self._init_sig) if self._init_sig else {}).get("tx_antenna")
        except ValueError:
            _tx = None
        if not _tx:
            return float(be.config.center_freq)
        return float(getattr(be, "_tx_lo", None) or be.config.center_freq)

    def _plan_for_chan(self):
        """The plan as loaded, or (anchor "tx") re-centred on the TX LO,
        so a scenario keyed from this radio lands on its bands at any tuning."""
        plan = self.plan
        lo = self._plan_tx_lo() if self._plan_anchor == "tx" else None
        if plan is None or lo is None:
            return plan
        plan = dict(plan)
        plan["compose"] = dict(plan.get("compose") or {}, center_freq_hz=lo)
        return plan

    def _chan_replan(self):
        """Re-apply the plan after a retune (anchored plans move with the TX LO)."""
        if self.chan is None or self.plan is None:
            return
        try:
            self.chan.set_plan(ChannelPlan.from_dict(self._plan_for_chan()))
        except Exception:
            log.exception("channelizer re-plan failed")

    def _chan_stop(self):
        if self.sched is not None:
            self.sched.frame_hook = None
            self.sched.rx_pace_s = getattr(self.sched, "_saved_rx_pace_s", 0.05)
        if self.chan is not None:
            try:
                self.chan.stop()
            except Exception:
                log.exception("channelizer stop")
            self.chan = None

    async def _cmd_set_plan(self, cid, p):
        """Load a channel plan for targeted decode. plan_path: local JSON on
        the radio host (checkpoint C); plan: inline dict (small plans). The
        GUI's over-link push (FILE_* transfer + plan_path) lands here too."""
        if not _HAVE_CHAN:
            return CmdAck(cid, CmdStatus.REJECTED,
                          {"err": "channelizer unavailable on this host"})
        plan = p.get("plan")
        if plan is None and p.get("plan_inbox"):
            # a plan previously pushed over the link via FILE_* transfer
            path = self._file_done.get(p["plan_inbox"])
            if path is None:
                return CmdAck(cid, CmdStatus.REJECTED,
                              {"err": f"no inbox file '{p['plan_inbox']}'"})
            try:
                plan = json.load(open(path))
            except (OSError, ValueError) as e:
                return CmdAck(cid, CmdStatus.FAILED, {"err": f"plan read: {e}"})
        if plan is None and p.get("plan_path"):
            try:
                plan = json.load(open(_within_base(p["plan_path"])))
            except OSError as e:
                return CmdAck(cid, CmdStatus.FAILED, {"err": f"plan read: {e}"})
        if plan is None:
            return CmdAck(cid, CmdStatus.REJECTED, {"err": "no plan/plan_path"})
        self.plan = plan
        self._plan_anchor = "tx" if p.get("anchor") == "tx" else None
        # apply live if already streaming
        streaming = (self.backend is not None and
                     self.backend.health.state == sdr.SDRDeviceState.STREAMING)
        if streaming:
            await self._run(self._chan_start)
        return CmdAck(cid, CmdStatus.OK,
                      {"entries": len(plan.get("entries", [])),
                       "applied": streaming})

    # TX plane (stage T1; TX radiates: GUI gates on specA)

    async def _cmd_set_tx_waveform(self, cid, p):
        """Set the TX waveform. kind=tone: synthesize a CW tone at
        offset_hz from the TX LO; kind=file: load a waveform present on the
        radio host. Idempotent: only replaces the scheduler's TX buffer, no
        radiation (start_tx does that)."""
        if self.sched is None:
            return CmdAck(cid, CmdStatus.REJECTED, {"err": "not initialized"})
        fs = self._tx_fs()
        kind = p.get("kind", "tone")
        try:
            if kind == "tone":
                off = float(p.get("offset_hz", 0.0))
                ampl = float(p.get("ampl", 0.5))
                # cap length (legit tones are ~65k samples) so a huge len_samples
                # can't OOM the service: 2^24 is ~0.5 s at 30.72 MSps
                nlen = max(1, min(int(p.get("len_samples", 8188 * 8)), 1 << 24))
                n = np.arange(nlen)
                wave = (ampl * np.exp(2j * np.pi * off / fs * n)
                        ).astype(np.complex64)
                desc = f"tone {off/1e3:g}kHz off, ampl {ampl:g}, {nlen} samp"
                self._tx_src = {"mode": "tone", "off": off, "ampl": ampl,
                                "nlen": nlen}
            elif kind == "file":
                # inbox: a file uploaded from the GUI via send_file lands here
                path = p.get("path")
                if path is not None:
                    path = _within_base(path)      # confine to /app_svc
                if not path and p.get("inbox"):
                    path = self._file_done.get(p["inbox"])
                    if path is None:
                        return CmdAck(cid, CmdStatus.REJECTED,
                                      {"err": f"no inbox file '{p['inbox']}'"})
                wave, _meta = await self._run(sdr.WaveformLoader.load, path, fs)
                desc = f"file {path} ({len(wave)} samp)"
                # raw IQ carries no rate; a scenario .iq.bin has a sibling plan
                self._tx_src = {"mode": "loader", "path": path,
                                "rate": self._iq_file_rate(path)[0]}
                # BLUE/Platinum headers carry the capture center + rate: let
                # the GUI offer to retune the SDR to the recording's frequency
                file_center = file_rate = None
                if str(path).lower().endswith(_BLUE_EXTS):
                    try:
                        from dpmg.dsp import cdif_reader
                        _ch = cdif_reader.read_header(path)
                        file_center = _ch.center_freq_hz or None
                        file_rate = _ch.sample_rate_hz or None
                    except Exception:
                        pass
                else:                              # carved regions say it too
                    _cm = self._carve_meta(path)
                    file_center = _cm.get("center")
                    file_rate = _cm.get("rate")
            else:
                return CmdAck(cid, CmdStatus.REJECTED,
                              {"err": f"unknown waveform kind {kind}"})
        except (KeyError, sdr.WaveformError) as e:
            return CmdAck(cid, CmdStatus.FAILED, {"err": str(e)})
        self.sched.set_tx_waveform(wave)
        self._tx_wf_desc = desc
        result = {"waveform": desc}
        if kind == "file":
            result["file_center_hz"] = file_center
            result["file_rate_hz"] = file_rate
            result["n_samples"] = int(len(wave))
            result["radio_center_hz"] = float(self.backend.config.center_freq)
            result["radio_rate_hz"] = self._tx_fs()
        return CmdAck(cid, CmdStatus.OK, result)

    async def _ensure_tx_armed(self):
        """Re-arm the scheduler from the last-loaded TX source if nothing is
        armed. Covers Load-before-START and a waveform lost across a re-init
        (a fresh scheduler starts with no TX buffer) so PLAY works."""
        if self.sched is None or self._tx_src is None:
            return
        if getattr(self.sched, "_tx_wave", None) is not None:
            return
        src = self._tx_src
        fs = self._tx_fs()
        try:
            if src["mode"] == "fromfile":
                wave = np.fromfile(src["path"], dtype=np.complex64)
            elif src["mode"] == "loader":
                wave, _ = await self._run(sdr.WaveformLoader.load, src["path"], fs)
            elif src["mode"] == "tone":
                n = np.arange(src["nlen"])
                wave = (src["ampl"] * np.exp(2j * np.pi * src["off"] / fs * n)
                        ).astype(np.complex64)
            elif src["mode"] == "record":              # Rebuild at the new rate
                wave, _info = await self._run(self._rec_build, src["name"],
                                              src.get("gain_db", 0.0))
            else:
                return
        except Exception as e:
            log.warning("re-arm TX from %s failed: %s", src, e)
            return
        if wave.size:
            self.sched.set_tx_waveform(wave, allow_live=True)
            log.info("re-armed TX from %s (%d samp)", src.get("path", src["mode"]),
                     wave.size)

    async def _cmd_start_tx(self, cid, p):
        """Radiates: starts the scheduler TX thread with the loaded waveform.
        The GUI is responsible for the specA-connected confirmation; the
        service does not ask again."""
        if self.sched is None:
            return CmdAck(cid, CmdStatus.REJECTED, {"err": "not initialized"})
        # A backend that can't transmit refuses up front instead of acking OK
        # while the TX loop fails in the background
        if getattr(self.backend, "tx_supported", True) is False:
            return CmdAck(cid, CmdStatus.REJECTED,
                          {"err": "TX not available on this backend "
                                  f"({getattr(self.backend, '_kind', '?')})"})
        if self._x40_tx_lock:            # X40 same-side rule
            return CmdAck(cid, CmdStatus.REJECTED,
                          {"err": self._x40_tx_lock, "x40_same_side": True})
        await self._ensure_tx_armed()
        # rate-mismatch guard: a waveform built at a different rate than the radio
        # plays with every frequency scaled. Caught at LOAD when the radio is up,
        # but a load-before-START slips through: so re-check here, where the
        # radio rate is known. allow_rate_mismatch lets the operator play anyway.
        wf_rate = (self._tx_src or {}).get("rate")
        radio_rate = self._tx_fs()
        # a live TX source (the bitpipe stream modem) builds its IQ at the
        # radio's rate and the cached waveform is never played, so its rate
        # does not matter then
        live_source = getattr(self.sched, "tx_source", None) is not None
        if wf_rate and radio_rate and abs(wf_rate - radio_rate) > wf_rate * 1e-3 \
                and not p.get("allow_rate_mismatch") and not live_source:
            scale = radio_rate / wf_rate
            return CmdAck(cid, CmdStatus.REJECTED,
                          {"err": f"waveform is {wf_rate/1e6:g} MSps but the radio "
                                  f"is {radio_rate/1e6:g} MSps, so every frequency "
                                  f"would scale x{scale:.4g}",
                           "rate_mismatch": True, "wf_rate": wf_rate,
                           "radio_rate": radio_rate, "scale": scale})
        mode = p.get("mode", "CONTINUOUS")
        try:
            self.sched.tx_mode = sdr.TXMode[mode]
        except KeyError:
            return CmdAck(cid, CmdStatus.REJECTED, {"err": f"bad tx mode {mode}"})
        # TX threads spin up inside the send loop after start; a silent
        # failure (e.g. SCHED_FIFO denied without the Docker RT budget)
        # would otherwise ack OK while nothing radiates. Clear the error
        # slot, start, then verify the datapath actually came up.
        self.backend.health.last_error = ""
        m0 = self.sched.metrics
        r0 = getattr(m0, "repeats", 0)
        try:
            await self._run(self.sched.start_tx)
        except sdr.WaveformError as e:
            return CmdAck(cid, CmdStatus.REJECTED, {"err": str(e)})
        await asyncio.sleep(0.5)
        err = self.backend.health.last_error
        alive = getattr(self.sched, "_tx", None) is not None \
            and self.sched._tx.is_alive()
        # A SINGLE_SHOT shorter than the 0.5 s check has already sent its
        # one pass and stopped: that is success, not a dead thread
        m1 = self.sched.metrics
        sent_once = (self.sched.tx_mode == sdr.TXMode.SINGLE_SHOT and not err
                     and getattr(m1, "repeats", 0) > (r0 if m1 is m0 else 0))
        if sent_once and not alive:
            log.warning("TX SINGLE_SHOT sent (wf=%s)", getattr(self, "_tx_wf_desc", None))
            return CmdAck(cid, CmdStatus.OK,
                          {"tx": "sent", "mode": mode,
                           "waveform": getattr(self, "_tx_wf_desc", None),
                           "tx_center_hz": getattr(self.backend, "_tx_lo", None)})
        if err or not alive:
            await self._run(self.sched.stop_tx)
            # A libsidekiq refusal names itself ("... rc=-95");
            # the RT-budget hint only fits the silent thread-died case
            hint = "" if "rc=" in str(err) else " (RT budget? see --cpu-rt-runtime)"
            return CmdAck(cid, CmdStatus.FAILED,
                          {"err": f"TX did not start: {err or 'thread died'}{hint}"})
        log.warning("TX STREAMING started (mode=%s, wf=%s)", mode,
                    getattr(self, "_tx_wf_desc", None))
        return CmdAck(cid, CmdStatus.OK,
                      {"tx": "streaming", "mode": mode,
                       "waveform": getattr(self, "_tx_wf_desc", None),
                       "tx_center_hz": getattr(self.backend, "_tx_lo", None)})

    async def _cmd_stop_tx(self, cid):
        if self.sched is None:
            return CmdAck(cid, CmdStatus.OK, {})
        await self._run(self.sched.stop_tx)
        log.warning("TX streaming stopped")
        return CmdAck(cid, CmdStatus.OK, {"tx": "stopped"})

    # transceive plane (modem RX to decode to TX-response)

    async def _cmd_transceive(self, cid, p):
        """Configure the modem responder over the channelizer. The RX channel
        decodes to a framed message; a reply is computed and (only when armed)
        radiated as a burst on the TX handle. Disarmed unless armed=true is sent
        explicitly (spectrum analyzer on the TX port: standing specA rule)."""
        if self._x40_tx_lock and True:       # X40 same-side rule
            return CmdAck(cid, CmdStatus.REJECTED,
                          {"err": self._x40_tx_lock, "x40_same_side": True})
        if not _HAVE_CHAN:
            return CmdAck(cid, CmdStatus.REJECTED,
                          {"err": "channelizer unavailable on this host"})
        if not bool(p.get("enabled", True)):
            self._modem_cfg = None
            self._tx_armed = False
            if self.chan is not None:
                self.chan.responder = None
            return CmdAck(cid, CmdStatus.OK, {"transceive": "off"})
        self._modem_cfg = {
            "modulation": p.get("modulation", "FSK"),
            "symbol_rate": float(p.get("symbol_rate", 4800.0)),
            "samples_per_bit": int(p.get("samples_per_bit", 40)),
            "tone0": float(p.get("tone0", 1200.0)),
            "tone1": float(p.get("tone1", 2200.0)),
            "ampl": float(p.get("ampl", 0.5)),
            "spread_factor": int(p.get("spread_factor", 15)),
            "pn_seed": int(p.get("pn_seed", 1)),
            "css_sf": int(p.get("css_sf", 7)),
            "css_bw": float(p.get("css_bw", 125000.0)),
            "css_os": int(p.get("css_os", 1)),
            "reply_offset_hz": float(p.get("reply_offset_hz", 200_000.0)),
        }
        self._tx_armed = bool(p.get("armed", False))
        self._wire_transceive()
        if self._tx_armed:
            log.warning("transceive ARMED (%s) -- replies will RADIATE",
                        self._modem_cfg["modulation"])
        return CmdAck(cid, CmdStatus.OK,
                      {"transceive": "on", "armed": self._tx_armed,
                       "wired": self.chan is not None,
                       "modulation": self._modem_cfg["modulation"]})

    def _wire_transceive(self):
        """Attach the modem responder + reply-burst callback to the live
        channelizer worker. No-op until both a worker and a modem config exist."""
        if self.chan is None or self._modem_cfg is None:
            return
        from dpmg.modem.transceive import make_channelizer_responder
        self.chan.responder = make_channelizer_responder()
        self.chan.tx_callback = self._modem_tx_burst

    def _modem_tx_burst(self, ch_idx, reply_bits):
        """Channelizer fires this on a valid decode with computed reply bits.
        Runs on the decode-pool thread. Disarmed = log only, no radiation."""
        cfg = self._modem_cfg
        if cfg is None or not reply_bits:
            return
        if not self._tx_armed:
            log.info("transceive ch%d: DISARMED -- would TX %d bits (%s)",
                     ch_idx, len(reply_bits), cfg["modulation"])
            return
        if self._tx_busy:
            return                       # drop overlapping burst
        self._tx_busy = True
        try:
            self._radiate_reply(cfg, reply_bits)
        except Exception as e:
            if self.backend is not None:
                self.backend.health.last_error = f"transceive TX: {e}"
            log.exception("transceive reply burst failed")
        finally:
            self._tx_busy = False

    def _radiate_reply(self, cfg, reply_bits):
        """Armed path: modulate the reply at the modem rate, resample to the
        card rate, and key one TX burst on the configured TX handle. Reuses the
        hardware-tested scheduler TX path. Not RF-verified end to end."""
        from dpmg.modulation.modulator import get_modulator
        mod = get_modulator(
            cfg["modulation"], cfg["symbol_rate"], cfg["samples_per_bit"],
            tone0=cfg["tone0"], tone1=cfg["tone1"], amplitude=cfg["ampl"],
            spread_factor=cfg["spread_factor"], pn_seed=cfg["pn_seed"],
            css_sf=cfg["css_sf"], css_bw=cfg["css_bw"], css_os=cfg["css_os"])
        iq = np.asarray(mod.modulate(reply_bits).iq_samples, dtype=np.complex64)
        card_rate = self._tx_fs()
        wave = _resample_iq(iq, float(getattr(mod, "sample_rate", card_rate)),
                            card_rate)
        off = float(cfg.get("reply_offset_hz", 0.0))
        if off:
            # place the reply away from the DC hump so the far side can decode
            n = np.arange(len(wave))
            wave = (wave * np.exp(2j * np.pi * off / card_rate * n)
                    ).astype(np.complex64)
        self.sched.set_tx_waveform(wave, allow_live=True)
        self.sched.tx_mode = sdr.TXMode.CONTINUOUS
        self.sched.start_tx()
        time.sleep(len(wave) / card_rate + 0.02)      # one burst duration
        self.sched.stop_tx()
        log.warning("transceive: radiated %d-sample reply burst (%s)",
                    len(wave), cfg["modulation"])

    # bitpipe stream link (dpmg/bitpipe)
    async def _cmd_bitpipe_start(self, cid, p):
        """Attach a stream modem between a bitpipe container's bit face and this
        radio. TX: the scheduler's TX source becomes the modem's block feed
        (keyed by the usual start_tx, so nothing radiates here). RX: the RX
        frame hook feeds the continuous demodulator, whose soft bits go back
        to the container. Params: container ("tcp:host:port" or "unix:/path"),
        or listen ("tcp:0.0.0.0:47899") to have the container dial in instead
        (the ground host's firewall drops new inbound connections) with an optional
        allow_from address; modulation (FSK, 4FSK, BPSK, QPSK), symbol_rate,
        deviation_hz or tones, offset_hz, amplitude, rx_sps, block_s."""
        if self.backend is None or self.sched is None:
            return CmdAck(cid, CmdStatus.REJECTED, {"err": "not initialized"})
        if self._bitpipe is not None:
            return CmdAck(cid, CmdStatus.REJECTED, {"err": "bitpipe already running; bitpipe_stop first"})
        if self._tx_running():
            return CmdAck(cid, CmdStatus.REJECTED, {"err": "TX is active; stop_tx before bitpipe_start"})
        from dpmg.bitpipe.modem_link import ModemLink
        from dpmg.bitpipe.stream_modem import WaveformConfig
        try:
            wf = WaveformConfig(modulation=str(p.get("modulation", "FSK")),
                                symbol_rate=float(p.get("symbol_rate", 2400.0)),
                                deviation_hz=float(p.get("deviation_hz", 1200.0)), tones=p.get("tones"),
                                dibit_mapping=str(p.get("dibit_mapping", "binary")),
                                amplitude=float(p.get("amplitude", 0.5)), rx_sps=int(p.get("rx_sps", 16)))
            link = ModemLink(wf, str(p.get("container", "tcp:127.0.0.1:47899")), self._tx_fs(),
                             float(self.backend.config.sample_rate), offset_hz=float(p.get("offset_hz", 0.0)),
                             block_s=float(p.get("block_s", 0.05)), name=f"{self.serial}-modem",
                             listen=p.get("listen") or None, allow_from=p.get("allow_from") or None,
                             block_multiple=int(p.get("block_multiple") or self._tx_block_multiple()))
        except Exception as e:
            return CmdAck(cid, CmdStatus.REJECTED, {"err": f"bitpipe config: {e}"})
        try:
            link.start()
        except OSError as e:
            return CmdAck(cid, CmdStatus.REJECTED, {"err": f"bitpipe listen {p.get('listen')}: {e}"})
        self._bitpipe = link
        prev = self.sched.frame_hook
        self._bitpipe_prev_hook = prev

        def _hook(frame, _prev=prev, _link=link):
            try:
                _link.on_rx(frame.samples, frame.sample_rate)
            except Exception as e:                                    # noqa: BLE001
                _link.stats["errors"] += 1
                log.warning("bitpipe rx: %s", e)
            if _prev is not None:
                _prev(frame)
        self.sched.frame_hook = _hook
        self.sched.set_tx_source(link.next_tx_block)
        log.info("bitpipe: %s %g Bd offset %g Hz, container %s (TX keys on start_tx)",
                 wf.kind, link.mod.symbol_rate, link.offset_hz, link.ep.describe())
        # acks carry the briefs: one datagram (< 1400 B); the container's JSON
        # control face has the full status
        return CmdAck(cid, CmdStatus.OK, {"bitpipe": link.brief(), "tx_running": False})

    def _tx_block_multiple(self) -> int:
        """The backend's own TX block in samples (the G40 bridge pads every send
        up to a whole one), or 1 when the backend takes any length."""
        be = self.backend
        for obj in (be, getattr(be, "_skiq", None)):
            n = getattr(obj, "tx_block_samples", None) if obj is not None else None
            if n:
                return int(n)
        return 1

    async def _cmd_bitpipe_stop(self, cid, p):
        link = self._bitpipe
        if link is None:
            return CmdAck(cid, CmdStatus.OK, {"bitpipe": "off"})
        stopped_tx = False
        if self._tx_running():
            await self._run(self.sched.stop_tx)
            stopped_tx = True
        try:
            self.sched.set_tx_source(None)
        except Exception:                                             # noqa: BLE001
            self.sched.tx_source = None
        if self.sched.frame_hook is not None and getattr(self.sched.frame_hook, "__name__", "") == "_hook":
            self.sched.frame_hook = self._bitpipe_prev_hook
        self._bitpipe = None
        self._bitpipe_prev_hook = None
        link.stop()
        log.info("bitpipe: stopped (tx stopped: %s)", stopped_tx)
        return CmdAck(cid, CmdStatus.OK, {"bitpipe": "off", "stopped_tx": stopped_tx, "final": link.brief()})

    async def _cmd_bitpipe_status(self, cid, p):
        link = self._bitpipe
        if link is None:
            return CmdAck(cid, CmdStatus.OK, {"running": False})
        from dpmg.bitpipe.modem_link import container_brief
        out = {"running": True, "tx_running": self._tx_running(), "modem": link.brief()}
        if p.get("container"):
            out["container"] = container_brief(
                await self.loop.run_in_executor(None, link.request_container_status, 1.0))
        return CmdAck(cid, CmdStatus.OK, out)

    # region carve (PSD box-select: save one band's IQ on the radio)

    def _cap_feed(self, frame):
        """Chained onto sched.frame_hook while a carve is capturing. Appends raw
        RX samples until the target count is reached, then stops accepting."""
        cap = self._cap
        if cap is None or cap["got"] >= cap["target"]:
            return
        s = getattr(frame, "samples", None)
        if s is None or len(s) == 0:
            return
        cap["buf"].append(np.asarray(s, dtype=np.complex64))
        cap["got"] += len(s)
        if cap["got"] == 0:
            return
        cap["center"] = float(getattr(frame, "center_freq", cap["center"]))
        cap["fs"] = float(getattr(frame, "sample_rate", cap["fs"]))

    async def _cmd_extract_region(self, cid, p):
        """Carve the band [f_lo_hz, f_hi_hz] out of the live RX and write it to a
        file on the radio. Params: f_lo_hz, f_hi_hz (absolute), filter
        (hann/hamming/blackman/rect/none), seconds, name, fmt (c64|dpmg)."""
        if self.sched is None or self.backend is None:
            return CmdAck(cid, CmdStatus.REJECTED, {"err": "not initialized"})
        if self._cap is not None or self._cap_lock.locked():
            return CmdAck(cid, CmdStatus.REJECTED, {"err": "capture in progress"})
        try:
            f_lo = float(p["f_lo_hz"]); f_hi = float(p["f_hi_hz"])
        except (KeyError, TypeError, ValueError):
            return CmdAck(cid, CmdStatus.REJECTED, {"err": "need f_lo_hz/f_hi_hz"})
        if f_hi <= f_lo:
            return CmdAck(cid, CmdStatus.REJECTED, {"err": "f_hi must exceed f_lo"})
        filt = str(p.get("filter", "hann"))
        seconds = max(0.02, min(10.0, float(p.get("seconds", 1.0))))
        fmt = "dpmg" if str(p.get("fmt", "c64")) == "dpmg" else "c64"
        fs = float(getattr(self.backend, "_achieved_rate",
                           self.backend.config.sample_rate))
        center = float(self.backend.config.center_freq)
        target = int(seconds * fs)

        # chain the capture onto whatever hook is live (channelizer or none)
        async with self._cap_lock:                 # one capture at a time
            prev_hook = self.sched.frame_hook
            cap = {"buf": [], "got": 0, "target": target, "fs": fs, "center": center}
            self._cap = cap

            def _chain(frame):
                self._cap_feed(frame)
                if prev_hook is not None:
                    prev_hook(frame)
            self.sched.frame_hook = _chain
            try:
                t_end = time.monotonic() + seconds + 3.0
                while cap["got"] < target and time.monotonic() < t_end:
                    await asyncio.sleep(0.05)
            finally:
                if self.sched is not None:
                    self.sched.frame_hook = prev_hook
                self._cap = None
        if cap["got"] < target * 0.5:
            return CmdAck(cid, CmdStatus.FAILED,
                          {"err": f"RX starved ({cap['got']}/{target} samp) "
                                  "-- is start_rx running?"})

        # carve + write in the hw pool (numpy convolve can be heavy)
        def _do():
            from dpmg.dsp import region_extract as _re
            # Frames overshoot the target by up to one frame: trim, so
            # the file holds exactly the requested seconds (or less, if RX starved)
            iq = np.concatenate(cap["buf"])[:cap["target"]]
            out, meta = _re.carve(iq, cap["fs"], cap["center"], f_lo, f_hi,
                                  filter_kind=filt)
            outdir = _data_dir("captures")
            base = _os.path.basename(str(p.get("name") or "region"))
            ext = ".bin" if fmt == "dpmg" else ".c64"
            path = _os.path.join(outdir, base + ext)
            _re.write_capture(path, out, meta, fmt)
            return path, meta
        try:
            path, meta = await self._run(_do)
        except Exception as e:
            return CmdAck(cid, CmdStatus.FAILED, {"err": f"carve: {e}"})
        log.info("extract_region: %.3f-%.3f MHz -> %s (%d samp @ %.0f Hz)",
                 f_lo / 1e6, f_hi / 1e6, path, meta.n_out, meta.out_rate_hz)
        return CmdAck(cid, CmdStatus.OK,
                      {"path": path, "n_out": meta.n_out,
                       # The capture may stop at >= 50 % of the request
                       "requested_s": round(seconds, 3),
                       "captured_s": round(min(cap["got"], cap["target"]) / fs, 3),
                       "out_rate_hz": meta.out_rate_hz,
                       "band_center_hz": meta.band_center_hz,
                       "bw_hz": meta.band_bw_hz, "decim": meta.decim,
                       "filter": filt})

    # Tools tab: test-signal gen + automated ground checkout

    async def _grab_rx(self, seconds: float):
        """Capture `seconds` of raw live RX IQ via a chained frame_hook.
        Returns (iq complex64, fs, center_hz) or None if RX is starved."""
        if self.sched is None or self.backend is None:
            return None
        fs = float(getattr(self.backend, "_achieved_rate",
                           self.backend.config.sample_rate))
        # cap the capture length so a bad/hostile `seconds` can't grow the buffer
        # into an OOM (10 s at 61.44 MSps is already ~5 GB)
        target = int(min(max(0.02, seconds), 10.0) * fs)
        async with self._cap_lock:                 # Wait for a running capture
            if self.sched is None:
                return None
            prev = self.sched.frame_hook
            cap = {"buf": [], "got": 0, "target": target, "fs": fs,
                   "center": float(self.backend.config.center_freq)}
            self._cap = cap

            def _chain(frame):
                self._cap_feed(frame)
                if prev is not None:
                    prev(frame)
            self.sched.frame_hook = _chain
            try:
                t_end = time.monotonic() + seconds + 3.0
                while cap["got"] < target and time.monotonic() < t_end:
                    await asyncio.sleep(0.05)
            finally:
                if self.sched is not None:
                    self.sched.frame_hook = prev
                self._cap = None
        if cap["got"] < target * 0.5:
            return None
        return np.concatenate(cap["buf"]), cap["fs"], cap["center"]

    async def _cmd_gen_test_waveform(self, cid, p):
        """Generate a test or jamming signal (chirp/tone/two_tone/noise/barrage/
        comb/swept/modulated) and write it to a file on the radio; optionally load it as the
        TX waveform (does not radiate: start_tx is still required)."""
        from dpmg.dsp import tools_engine as _te
        if self.backend is None:
            return CmdAck(cid, CmdStatus.REJECTED, {"err": "not initialized"})
        kind = str(p.get("kind", "chirp"))
        if kind not in _te.GENERATORS:
            return CmdAck(cid, CmdStatus.REJECTED,
                          {"err": f"kind must be one of {sorted(_te.GENERATORS)}"})
        fs = float(p.get("fs") or self._tx_fs())
        seconds = max(0.001, min(5.0, float(p.get("seconds", 0.1))))
        # "power" (ampl) is the TX peak as a fraction of DAC full scale: peak-
        # normalize the generated waveform so max|iq| == power. This maps 0..1
        # onto the usable TX range for every waveform shape with no DAC clipping
        # (noise, whose peaks run 3-4x its RMS, does not saturate); it is a
        # pure TX-chain scaling and says nothing about the RX front end.
        power = max(0.0, min(1.0, float(p.get("ampl", 0.7))))
        kw = {k: float(p[k]) for k in
              ("f0", "f1", "f2", "freq", "bw",                   # test signals
               "spacing", "n_tones", "center", "span",           # jamming
               "sweep_period_s", "noise_bw", "prf", "duty",
               "sym_rate", "sym_duty", "rolloff")                # modulated
              if p.get(k) is not None}
        if p.get("mod") is not None:                             # constellation
            kw["mod"] = str(p["mod"]).lower()
        outdir = _data_dir("waveforms")
        path = _os.path.join(outdir, _os.path.basename(str(p.get("name")
                             or f"{kind}")) + ".c64")

        def _build():
            # a long loop at a high rate takes seconds to build and write; on
            # the event loop that stalls STATUS and the GUI marks the radio
            # stale, so the build runs on the default executor (not the 2-thread
            # hw pool the card pollers use)
            y = np.asarray(_te.generate(kind, fs, seconds, **kw), dtype=np.complex64)
            pk = float(np.max(np.abs(y))) if y.size else 0.0
            if pk > 0:
                y = (y * np.float32(power / pk)).astype(np.complex64)
            y.tofile(path)
            return y
        try:
            iq = await self.loop.run_in_executor(None, _build)
        except Exception as e:
            return CmdAck(cid, CmdStatus.FAILED, {"err": f"gen: {e}"})
        if p.get("load_tx"):
            # built at the radio's current rate, so it always matches
            self._tx_src = {"mode": "fromfile", "path": path, "rate": fs}
            self._tx_wf_desc = f"tools {kind}"
            if self.sched is not None:
                self.sched.set_tx_waveform(np.asarray(iq, dtype=np.complex64))
                self._tx_wf_desc = f"tools {kind} ({len(iq)} samp)"
        warnings = _te.check_waveform_params(kind, fs, seconds, **kw)
        for _wmsg in warnings:
            log.warning("gen_test_waveform: %s", _wmsg)
        log.info("gen_test_waveform: %s %d samp @ %.0f Hz -> %s",
                 kind, len(iq), fs, path)
        return CmdAck(cid, CmdStatus.OK,
                      {"path": path, "n": int(len(iq)), "fs": fs, "kind": kind,
                       "loaded_tx": bool(p.get("load_tx")), "warnings": warnings})

    @staticmethod
    def _carve_meta(path) -> dict:
        """{"rate", "center", "src"} of a carved region file, else {}.
        c64: region_extract's <file>.json sidecar; .bin: the DPMG BE-IQ16 header
        (rate only)."""
        path = str(path or "")
        try:
            side = path + ".json"
            if _os.path.exists(side):
                m = json.load(open(side))
                if m.get("out_rate_hz"):
                    return {"rate": float(m["out_rate_hz"]),
                            "center": float(m.get("band_center_hz") or 0) or None,
                            "src": "carve sidecar"}
            if path.lower().endswith(".bin") and not path.lower().endswith(".iq.bin"):
                import struct
                with open(path, "rb") as fh:
                    hdr = fh.read(16)
                if len(hdr) == 16 and hdr[:4] == b"DPMG":
                    _m, _v, _f, _r, rate, hsz = struct.unpack(">4sBBHII", hdr)
                    if rate and hsz == 16:
                        return {"rate": float(rate), "center": None, "src": "DPMG header"}
        except Exception:
            pass
        return {}

    def _iq_file_rate(self, path):
        """Best-effort native sample rate (Hz) of an IQ file: (rate, source).
        Scenario-built .iq.bin carries it in a sibling .plan.json; a <name>.rate
        sidecar or a trailing _<rate>k/m in the filename also count. Else None."""
        path = str(path or "")
        try:
            if path.lower().endswith(_BLUE_EXTS):     # BLUE/Platinum HCB carries 1/xdelta
                from dpmg.dsp import cdif_reader
                m = cdif_reader.read_header(path)
                if m.sample_rate_hz:
                    return float(m.sample_rate_hz), "BLUE header"
            if path.lower().endswith(".wav"):          # WAV header carries the rate
                import wave
                with wave.open(path, "rb") as _w:
                    r = float(_w.getframerate())
                if r:
                    return r, "wav header"
            # a carved region (region_extract) carries its band rate in a
            # <file>.json sidecar (c64) or the DPMG header (.bin)
            car = self._carve_meta(path)
            if car.get("rate"):
                return float(car["rate"]), car["src"]
            base = path[:-7] if path.endswith(".iq.bin") else _os.path.splitext(path)[0]
            plan = base + ".plan.json"
            side = base + ".rate"
            if _os.path.exists(plan):
                pj = json.load(open(plan))
                rate = float(pj.get("compose", {}).get("sample_rate_hz") or 0) or None
                if rate:
                    return rate, "plan.json"
            if _os.path.exists(side):
                rate = float(open(side).read().strip()) or None
                if rate:
                    return rate, "rate sidecar"
            m = _re.search(r"_(\d+(?:\.\d+)?)([kKmM])(?:sps)?\.", _os.path.basename(path))
            if m:
                return float(m.group(1)) * (1e3 if m.group(2) in "kK" else 1e6), "filename"
        except Exception:
            pass
        return None, None

    async def _cmd_resample_waveform(self, cid, p):
        """Resample an IQ waveform on the radio to the SDR's rate (scipy
        resample_poly) and write a new .iq.bin, so a file/scenario authored at a
        different rate plays at the correct frequencies. Defaults the source to
        the currently-armed TX waveform and the target to the radio's rate;
        load_tx arms the result. This is a true resample, not a rebuild."""
        if self.backend is None:
            return CmdAck(cid, CmdStatus.REJECTED, {"err": "not initialized"})
        src = str(p.get("src_path") or (self._tx_src or {}).get("path") or "")
        if src:
            try:
                src = _within_base(src)        # confine source + its derived dst to /app_svc
            except ValueError as e:
                return CmdAck(cid, CmdStatus.REJECTED, {"err": str(e)})
        if not src or not _os.path.exists(src):
            return CmdAck(cid, CmdStatus.REJECTED, {"err": "no source waveform to resample"})
        fs_in = float(p.get("fs_in") or (self._tx_src or {}).get("rate")
                      or (self._iq_file_rate(src)[0] or 0))
        fs_out = float(p.get("fs_out") or self._tx_fs())
        if fs_in <= 0:
            return CmdAck(cid, CmdStatus.REJECTED,
                          {"err": "unknown source rate, specify fs_in"})

        # already at the target rate: don't resample. resample_poly at ratio 1
        # still runs an FIR (refilters, clips band edges, rewrites the file), so
        # a file played at its own native SDR rate is loaded as-is instead.
        if abs(fs_out - fs_in) <= max(1.0, fs_in * 1e-3):
            try:
                wave, _m = await self._run(sdr.WaveformLoader.load, src, fs_out)
            except Exception as e:
                return CmdAck(cid, CmdStatus.FAILED, {"err": f"load: {e}"})
            result = {"path": src, "fs_in": fs_in, "fs_out": fs_out,
                      "n_in": int(len(wave)), "n_out": int(len(wave)),
                      "sample_rate": fs_out, "resampled": False}
            if p.get("load_tx"):
                self._tx_src = {"mode": "loader", "path": src, "rate": fs_in}
                self._tx_wf_desc = f"native {_os.path.basename(src)}"
                if self.sched is not None:
                    self.sched.set_tx_waveform(wave)
                    result["armed"] = True
                else:
                    result["armed"] = False
            log.info("resample_waveform: %s already %.4g MSps -> loaded as-is (%d samp)",
                     _os.path.basename(src), fs_out / 1e6, len(wave))
            return CmdAck(cid, CmdStatus.OK, result)

        def _do():
            import scipy.signal as _ss
            from fractions import Fraction as _Fr
            if src.lower().endswith(_BLUE_EXTS):  # Midas BLUE / Platinum I/Q
                from dpmg.dsp import cdif_reader
                iq, _cm = cdif_reader.read_cdif(src)
                iq = np.asarray(iq, dtype=np.complex64)
            else:
                iq = np.fromfile(src, dtype=np.complex64)
            fr = _Fr(fs_out / fs_in).limit_denominator(20000)
            out = _ss.resample_poly(iq, fr.numerator, fr.denominator).astype(np.complex64)
            base = src[:-7] if src.endswith(".iq.bin") else _os.path.splitext(src)[0]
            dst = f"{base}_rs{int(round(fs_out/1e3))}k.iq.bin"   # rate encoded in name
            out.tofile(dst)
            return dst, len(iq), len(out)
        try:
            dst, nin, nout = await self._run(_do)
        except Exception as e:
            return CmdAck(cid, CmdStatus.FAILED, {"err": f"resample: {e}"})
        result = {"path": dst, "fs_in": fs_in, "fs_out": fs_out,
                  "n_in": nin, "n_out": nout, "sample_rate": fs_out}
        if p.get("load_tx"):
            self._tx_src = {"mode": "fromfile", "path": dst, "rate": fs_out}
            self._tx_wf_desc = f"resampled {_os.path.basename(dst)}"
            if self.sched is not None:
                self.sched.set_tx_waveform(np.fromfile(dst, dtype=np.complex64))
                result["armed"] = True
            else:
                result["armed"] = False
        log.info("resample_waveform: %s  %d->%d samp  %.4g->%.4g MSps -> %s",
                 _os.path.basename(src), nin, nout, fs_in/1e6, fs_out/1e6, _os.path.basename(dst))
        return CmdAck(cid, CmdStatus.OK, result)

    async def _cmd_waveform_rate(self, cid, p):
        """Native sample rate of an IQ file on the radio (via _iq_file_rate), so
        the GUI can warn before playing one at a mismatched SDR rate."""
        radio_rate = None
        if self.backend is not None:
            radio_rate = self._tx_fs()
        _wr = p.get("path")
        rate, source = self._iq_file_rate(_within_base(_wr) if _wr else None)
        return CmdAck(cid, CmdStatus.OK,
                      {"rate": rate, "source": source, "radio_rate": radio_rate,
                       "match": (rate is None or radio_rate is None
                                 or abs(rate - radio_rate) <= rate * 1e-3)})

    async def _cmd_gen_scenario(self, cid, p):
        """Build a scenario waveform (+ matched decode plan) from an inline JSON
        spec on the radio, so a tiny spec crosses the link instead of a big
        .iq.bin. load_tx also arms it as the TX source (start_tx still required;
        does not radiate). Returns the iq/plan paths."""
        spec = p.get("spec")
        # spec_path: build from a scenario JSON already on the radio
        if spec is None and p.get("spec_path"):
            try:
                with open(_within_base(p["spec_path"])) as _f:
                    spec = json.load(_f)
            except Exception as e:
                return CmdAck(cid, CmdStatus.REJECTED, {"err": f"spec_path: {e}"})
        # spec_inbox: a spec JSON pushed over the link (FILE_* transfer): for
        # specs too big for one command datagram (16-user DSSS overlays)
        if spec is None and p.get("spec_inbox"):
            path = self._file_done.get(p["spec_inbox"])
            if path is None:
                return CmdAck(cid, CmdStatus.REJECTED, {"err": f"no inbox file '{p['spec_inbox']}'"})
            try:
                with open(path) as _f:
                    spec = json.load(_f)
            except Exception as e:
                return CmdAck(cid, CmdStatus.FAILED, {"err": f"spec_inbox: {e}"})
        if not isinstance(spec, dict) or not (spec.get("signals") or spec.get("users")):
            return CmdAck(cid, CmdStatus.REJECTED,
                          {"err": "spec must be a scenario JSON with a 'signals' list (or DSSS 'users')"})
        from dpmg.scenario import gen_scenario_json as _gsj
        spec_rate = float(spec.get("sample_rate_hz") or 0) or None   # scenario's own rate
        # the scenario's intended RF center (baked into its decode plan) vs the
        # radio's current tuning: the GUI offers a retune when they differ so
        # the plan + spectrum line up with where the signal actually lands.
        scenario_center = float(spec.get("center_freq_hz") or 0) or None
        radio_center = (float(self.backend.config.center_freq)
                        if self.backend is not None else None)
        # the radio's current RF rate (if up): what the waveform will play at
        radio_rate = None
        if self.backend is not None:
            radio_rate = self._tx_fs()      # Built at the TX card's rate
        # default the sample rate to this radio's own if the spec leaves it out;
        # match_radio_rate forces a rebuild at the radio's rate even if the spec
        # pins its own (no frequency scaling from a scenario built at another rate)
        if (not spec.get("sample_rate_hz") or p.get("match_radio_rate")) and radio_rate:
            spec = dict(spec, sample_rate_hz=radio_rate)

        # checks + bounds: signals must fit the usable band at the build rate,
        # and the build rate should match the radio's rate (else frequencies
        # scale). check_only returns the verdict without building.
        # peak_fs: the command's own value wins over the spec's; out of range
        # is a validation error like an out-of-band signal
        peak_fs = p.get("peak_fs")
        v = _gsj.validate_spec(spec, radio_rate, peak_fs=peak_fs)

        def _wire_v(val):
            # wire-safe validation: drop the big per-signal list and cap the
            # strings so the ack always fits in one datagram (proto caps at 1400B)
            return {"ok": val.get("ok"),
                    "errors": [str(e)[:160] for e in (val.get("errors") or [])[:4]],
                    "warnings": [str(w)[:200] for w in (val.get("warnings") or [])[:2]],
                    "build_rate": val.get("build_rate"), "scale": val.get("scale"),
                    "radio_rate": val.get("radio_rate"), "usable_hz": val.get("usable_hz"),
                    "min_rate_hz": val.get("min_rate_hz"), "peak_fs": val.get("peak_fs")}

        def _summary(val):
            # a short description of the spec for the mission planner's step
            # editor (a file on the radio is never sent back whole): signal
            # count, the modulations in order of first use and the band the
            # signals occupy around the TX center, from the validator's list
            sigs = val.get("signals") or []
            try:
                exp = _gsj.expand_users(spec)
            except Exception:
                exp = spec
            mods = []
            for sg in (exp.get("signals") or []):
                m = str(sg.get("modulation") or "?").upper() if isinstance(sg, dict) else "?"
                if m not in mods:
                    mods.append(m)
            lo = min((s["offset_hz"] - s["bw_hz"] / 2.0 for s in sigs), default=0.0)
            hi = max((s["offset_hz"] + s["bw_hz"] / 2.0 for s in sigs), default=0.0)
            return {"signals": len(sigs) or len(exp.get("signals") or []),
                    "modulations": mods[:8], "lo_hz": lo, "hi_hz": hi,
                    "span_hz": max(0.0, hi - lo)}

        if p.get("check_only"):
            return CmdAck(cid, CmdStatus.OK,
                          {"validation": _wire_v(v), "checked": True,
                           "sample_rate": v["build_rate"], "spec_rate_hz": spec_rate,
                           "radio_rate": radio_rate,
                           "scenario_center_hz": scenario_center,
                           "radio_center_hz": radio_center,
                           "can_rebuild": radio_rate is not None,
                           "summary": _summary(v)})
        if v["errors"] and not p.get("force"):
            return CmdAck(cid, CmdStatus.REJECTED,
                          {"err": "; ".join(v["errors"])[:700],
                           "validation": _wire_v(v),
                           "can_rebuild": radio_rate is not None})

        outdir = _data_dir("waveforms")
        try:
            nsig, nbytes = await self._run(lambda: _gsj.build_scenario(spec, outdir, peak_fs))
        except Exception as e:
            return CmdAck(cid, CmdStatus.FAILED, {"err": f"gen: {e}"})
        name = _os.path.basename(str(spec.get("name", "scenario")))
        iq_path = _os.path.join(outdir, f"{name}.iq.bin")
        plan_path = _os.path.join(outdir, f"{name}.plan.json")
        result = {"iq_path": iq_path, "plan_path": plan_path, "signals": nsig,
                  "bytes": nbytes, "sample_rate": float(spec.get("sample_rate_hz", 0)),
                  "radio_rate": radio_rate, "validation": _wire_v(v),
                  "scenario_center_hz": scenario_center,
                  "radio_center_hz": radio_center, "peak_fs": v.get("peak_fs")}
        if p.get("load_tx"):
            # remember the built IQ as the TX source (+ the rate it was built at)
            # so start_tx can (re-)arm it and flag a rate mismatch even when the
            # scenario was loaded before the radio was initialized
            self._tx_src = {"mode": "fromfile", "path": iq_path,
                            "rate": float(spec.get("sample_rate_hz") or 0) or None}
            self._tx_wf_desc = f"scenario {name}"
            if self.sched is not None:
                iq = np.fromfile(iq_path, dtype=np.complex64)
                self.sched.set_tx_waveform(iq)
                self._tx_wf_desc = f"scenario {name} ({len(iq)} samp)"
                result["armed"] = True
            else:
                # built on disk; will arm itself on START/PLAY
                result["armed"] = False
            result["loaded_tx"] = True
        log.info("gen_scenario: %s %d sig %d bytes -> %s", name, nsig, nbytes, iq_path)
        return CmdAck(cid, CmdStatus.OK, result)

    async def _cmd_list_waveforms(self, cid, p):
        # list the IQ files on the radio so the GUI can offer a picker instead of
        # a typed path: data/waveforms, data/inbox, data/iq, and cwd.
        import glob as _glob
        # kind="json" lists scenario spec JSON; default lists IQ waveforms
        exts = ("*.json",) if str(p.get("kind", "iq")) == "json" else ("*.iq.bin", "*.c64")
        dirs = [_data_dir("waveforms"), _data_dir("inbox"),
                _data_dir("iq"), _os.getcwd()]
        seen = {}
        for d in dirs:
            for ext in exts:
                for f in _glob.glob(_os.path.join(d, ext)):
                    try:
                        seen[f] = _os.path.getsize(f)
                    except OSError:
                        pass
        # newest first, truncated to fit one datagram: proto.encode refuses
        # anything over MAX_DATAGRAM (1400 B), which would silently drop the ack
        # when many files are present. Pack entries until the budget is hit.
        ordered = sorted(seen.items(),
                         key=lambda kv: _os.path.getmtime(kv[0]) if _os.path.exists(kv[0]) else 0,
                         reverse=True)
        budget = proto.MAX_DATAGRAM - proto.HEADER_SIZE - 64   # header + envelope slack
        files, acc, truncated = [], 0, False
        for f, s in ordered:
            e = {"path": f, "name": _os.path.basename(f), "bytes": s}
            j = len(json.dumps(e, separators=(",", ":"))) + 1
            if acc + j > budget:
                truncated = True
                break
            files.append(e); acc += j
        return CmdAck(cid, CmdStatus.OK,
                      {"files": files, "truncated": truncated,
                       "total": len(ordered)})

    async def _cmd_list_dir(self, cid, p):
        # browse the radio filesystem: list one directory (dirs + files). Entries
        # are capped so the ack fits one datagram; optional ext filter for files.
        base = _os.path.realpath(_os.getcwd())
        try:
            path = _within_base(p.get("path") or base)   # confine to /app_svc
        except ValueError as e:
            return CmdAck(cid, CmdStatus.REJECTED, {"err": str(e)})
        exts = tuple(p.get("exts") or ())
        try:
            names = sorted(_os.listdir(path))
        except OSError as e:
            return CmdAck(cid, CmdStatus.REJECTED, {"err": str(e)})
        # compact result: the ack is one ~1400 B datagram, so send names only
        # (+ KB size) and let the client rebuild full paths from `path`. Cap counts.
        CAP = 30
        dirs, files = [], []
        for nm in names:
            full = _os.path.join(path, nm)
            try:
                if _os.path.isdir(full):
                    dirs.append(nm)
                elif not exts or nm.lower().endswith(exts):
                    # KB, rounded up so a non-empty sub-KB file isn't shown as 0 KB
                    _sz = _os.path.getsize(full)
                    files.append([nm, (_sz + 1023) // 1024 if _sz else 0])  # [name, KB]
            except OSError:
                pass
        trunc = len(dirs) > CAP or len(files) > CAP
        return CmdAck(cid, CmdStatus.OK, {
            "path": path, "parent": _os.path.dirname(path.rstrip("/")) or "/",
            "dirs": dirs[:CAP], "files": files[:CAP], "truncated": trunc})

    async def _cmd_log_detections(self, cid, p):
        # tee detection events to a CSV on the radio. enable=False closes it.
        enable = bool(p.get("enable", True))
        fp = getattr(self, "_det_log_fp", None)
        if fp is not None:
            try:
                fp.close()
            except Exception:
                pass
            self._det_log_fp = None
        if not enable:
            return CmdAck(cid, CmdStatus.OK, {"enabled": False})
        try:
            path = _within_base(p.get("path") or "detections.csv")   # confine to /app_svc
        except ValueError as e:
            return CmdAck(cid, CmdStatus.REJECTED, {"err": str(e)})
        try:
            _os.makedirs(_os.path.dirname(path), exist_ok=True)
            new = not _os.path.exists(path) or _os.path.getsize(path) == 0
            self._det_log_fp = open(path, "a")
            if new:
                self._det_log_fp.write(f"{self._log_time_col()},t_unix,freq_hz,bw_hz,snr_db,dur_ms\n")
                self._det_log_fp.flush()
        except OSError as e:
            return CmdAck(cid, CmdStatus.REJECTED, {"err": str(e)})
        return CmdAck(cid, CmdStatus.OK, {"enabled": True, "path": path})

    def _log_time_col(self) -> str:
        """The first column of this radio's log files, named for its clock: g40_time_utc
        on a G40. The GUI's own files say gcs_time instead."""
        return f"{str(self.kind or 'radio').lower()}_time_utc"

    async def _cmd_log_telemetry(self, cid, p):
        # tee the 1 Hz STATUS (position + SDR health) to a CSV on the radio.
        enable = bool(p.get("enable", True))
        fp = getattr(self, "_telem_log_fp", None)
        if fp is not None:
            try:
                fp.close()
            except Exception:
                pass
            self._telem_log_fp = None
        if not enable:
            return CmdAck(cid, CmdStatus.OK, {"enabled": False})
        try:
            path = _within_base(p.get("path") or "telemetry.csv")   # confine to /app_svc
        except ValueError as e:
            return CmdAck(cid, CmdStatus.REJECTED, {"err": str(e)})
        try:
            _os.makedirs(_os.path.dirname(path), exist_ok=True)
            new = not _os.path.exists(path) or _os.path.getsize(path) == 0
            self._telem_log_fp = open(path, "a")
            if new:
                self._telem_log_fp.write(
                    f"{self._log_time_col()},t_unix,state,lat,lon,alt_m,pos_valid,"
                    "overruns,underruns,fps,uptime_s,tx_active,ref_locked,clock_ppm,ten_mhz\n")
                self._telem_log_fp.flush()
        except OSError as e:
            return CmdAck(cid, CmdStatus.REJECTED, {"err": str(e)})
        return CmdAck(cid, CmdStatus.OK, {"enabled": True, "path": path})

    # External PA enable / TX-RX switch / attenuator
    def _frontend_start(self, path=None):
        """(executor) Load rf_frontend.yaml (+CLI overrides), open the GPIO lines
        (PA forced LOW) and the attenuator. frontend: none: nothing happens."""
        try:
            cfg = _rf.load_config(path or self._fe_path, self._fe_overrides)
        except _rf.ConfigError as e:
            self._port_err = str(e)                  # port map unknown, so refuse
            raise
        self._fe_cfg = cfg
        # the file's own line says who set it, unless a hand edit has changed it since
        changed = self._port_src is not None and cfg.ports != self._port_map
        self._port_by = (f"config file reload, {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}"
                         if changed else _rf.read_ports_stamp(path or self._fe_path
                                                              or _rf.default_config_path()))
        self._port_map, self._port_src, self._port_err = cfg.ports, cfg.source, None
        if cfg.frontend != "none":
            self.frontend = _rf.make_frontend(cfg)
            log.info("rf_frontend: %s open: %s", cfg.frontend,
                     json.dumps({k: v for k, v in self.frontend.status().items()
                                 if k in ("pa", "tr", "fault")}, default=str))
        else:
            self.frontend = None
        self.atten = _mc.make_atten(cfg.atten)       # None for auto: the watcher opens it
        if self.atten is not None:
            log.info("rf_frontend: attenuator %s", self.atten.status())
        self.guard = None
        if cfg.payload is not None:
            from dpmg.payload import payload_budget as _pb
            pl = dict(cfg.payload)
            if pl.get("profiles_file") and not _os.path.isabs(pl["profiles_file"]):
                base = _os.path.dirname(_os.path.abspath(cfg.source)) \
                    if _os.path.exists(str(cfg.source)) else _REPO_DIR
                pl["profiles_file"] = _os.path.join(base, pl["profiles_file"])
            self.guard = _pg.build_guard(pl, self.atten, _pb.load_profiles,
                                         _pb.min_safe_rx_atten, _pb.profile,
                                         _pb.min_safe_tx_atten,
                                         atten_max_db=cfg.atten.max_db,
                                         lna_tx_floor=_pb.min_lna_safe_tx_atten)
            if self.frontend is not None:
                self.frontend.guard = self.guard
                # let the load power and LNA checks read the live G40 TX
                # attenuation (the last set_tx_atten, else the init value, as
                # payload_status reports it). Only an Epiq radio's value is the G40
                # attenuation the floors were worked out for; any other radio reads
                # as unknown, so a configured floor refuses (fail closed) instead of
                # comparing the wrong number
                self.frontend.tx_atten_getter = (
                    self._live_tx_atten_db if self.kind in ("g40", "x40", "mock")
                    else (lambda: None))
            log.info("payload guard: %s", json.dumps(self.guard.status(), default=str))
        self._devices_start(cfg)
        return cfg

    # attenuator auto-attach + switch ADC presence
    def _at_lock(self):
        """The lock every attenuator access takes: the guard's when there is one
        (keying reads under it), else the service's own."""
        g = self.guard
        return g.lock if g is not None else self._atten_lock

    def _atten_publish(self, at):
        """(watcher thread, lock held) An attached unit, or None when it went away."""
        self.atten = at
        if self.guard is not None:
            self.guard.set_atten(at)
        self._atten_db_last = None
        if at is None:
            fe = self.frontend
            hot = bool(fe is not None and (getattr(fe, "_keyed", False) or getattr(fe, "_tr_tx", False)))
            if hot or self._tx_running():
                self._atten_trip("RX attenuator lost during TX")

    def _atten_trip(self, reason: str):
        """The RX chain may be unprotected during TX: HPA off, switch to RX.  On its own
        thread, since the watcher holds the guard lock here and a key takes the front-end
        lock first.  The RF path stays up into the disabled HPA until the run is stopped."""
        fe = self.frontend
        log.error("payload guard: %s: dropping the HPA", reason)
        if fe is not None and self.guard is not None:
            threading.Thread(target=fe.safe, args=(reason,), name="atten-trip", daemon=True).start()

    def _atten_read(self, db: float):
        """(watcher thread) Each attenuator reading: while keyed it must still be at the
        protect value, or the HPA is dropped."""
        fe, g = self.frontend, self.guard
        if fe is not None and g is not None and getattr(fe, "_keyed", False) and not g.protected(db):
            self._atten_trip(f"RX attenuator reads {db:g} dB while keyed")

    def _adc_probe_ok(self) -> bool:
        """Probe the switch ADC only with the front end open and idle: nothing
        keyed, switch in RX, no TX run, the front-end lock free (no key sequence)."""
        fe = self.frontend
        if fe is None or getattr(fe, "_keyed", False) or getattr(fe, "_tr_tx", False):
            return False
        if self._tx_running():
            return False
        lk = getattr(fe, "_lock", None)
        if lk is not None:
            if not lk.acquire(blocking=False):
                return False
            lk.release()
        return True

    def _devices_start(self, cfg):
        aw = adc = None
        if cfg.atten.transport == "auto":
            aw = _dw.AttenWatch(cfg.atten, self._atten_publish, lock=self._at_lock(),
                                on_read=self._atten_read)
            aw.scan()                                # a unit already plugged in is there at start
        if self.frontend is not None:
            rb = cfg.tr_readback
            adc = _dw.AdcProbe(rb.bus, rb.address)
            if cfg.frontend == "mock":
                adc.off(_dw.ADC_SIM)                 # mock front end: no I2C to probe
        self._atten_watch, self._adc_probe = aw, adc
        if aw is not None or (adc is not None and adc.state != _dw.ADC_SIM):
            self._devwatch = _dw.DeviceWatcher(
                aw, adc if (adc is not None and adc.state != _dw.ADC_SIM) else None,
                adc_allowed=self._adc_probe_ok,
                atten_delay_s=_dw.ATTEN_SCAN_S).start()

    def _devices_stop(self):
        dw, self._devwatch = self._devwatch, None
        if dw is not None:
            dw.stop()                                # joins, then unpublishes + closes the unit
        elif self._atten_watch is not None:
            self._atten_watch.close()
        self._atten_watch = None
        self._adc_probe = None

    def _devices_status(self, db=None, hpa=None) -> dict:
        """Compact devices block for payload_status (one CMD_ACK datagram).
        ``hpa`` is the front end's hpa_monitor block (its own read decides
        present / not fitted; no separate probe for that chip)."""
        aw, cfg = self._atten_watch, self._fe_cfg
        if aw is not None:
            a = aw.status()
            if db is not None and self.atten is not None:
                a["db"] = db
        else:
            tr = cfg.atten.transport if cfg is not None else "none"
            if tr == "none":
                a = {"state": _dw.AT_OFF}
            else:
                at = self.atten
                a = {"state": _dw.AT_CONNECTED if at is not None else _dw.AT_NONE,
                     "path": (at.t.describe()[:40] if at is not None else None), "db": db}
        a["mode"] = cfg.atten.transport if cfg is not None else "none"
        adc = self._adc_probe
        hm = cfg.hpa_monitor if cfg is not None else None
        if hm is None or not hm.enabled or hm.transport == "none" or self.frontend is None:
            h = {"state": _dw.ADC_OFF}
        elif cfg.frontend == "mock":
            h = {"state": _dw.ADC_SIM}
        elif not hpa:
            h = {"state": _dw.ADC_OFF}
        else:
            h = {"state": (_dw.ADC_PRESENT if hpa.get("state") == _rf.HPA_MON_OK
                           else str(hpa.get("state") or _dw.ADC_OFF))}
        if hm is not None and hm.enabled:
            h.update({"bus": hm.bus, "address": hex(hm.address),
                      "channels": [hm.temp_channel, hm.current_channel]})
        return {"atten": a, "tr_adc": adc.status() if adc is not None else {"state": _dw.ADC_OFF},
                "hpa_adc": h}

    def _frontend_stop(self):
        self._devices_stop()                         # device watcher first: no scan during teardown
        fe, self.frontend = self.frontend, None
        if fe is not None:
            try: fe.close()
            except Exception: log.exception("frontend close")
        if self.backend is not None:
            self.backend.frontend = None
        self.guard = None
        at, self.atten = self.atten, None
        if at is not None:
            try: at.close()
            except Exception: pass

    def _frontend_status_dict(self) -> dict:
        d = {"configured": self._fe_cfg.frontend if self._fe_cfg else "none",
             "config_source": self._fe_cfg.source if self._fe_cfg else None,
             "config_path": self._fe_path or _rf.default_config_path(),
             "frontend": self.frontend.status() if self.frontend is not None else None,
             "atten": None}
        with self._at_lock():
            at = self.atten
            if at is not None:
                try: d["atten"] = at.status()
                except Exception as e: d["atten"] = {"err": str(e)}
        if self._atten_watch is not None:            # auto attach state
            ws = self._atten_watch.status()
            d["atten"] = dict(d["atten"] or {}, state=ws["state"], path=ws["path"],
                              model=ws["model"], serial=ws["serial"], since=ws["since"])
        return d

    def _tx_running(self) -> bool:
        s = self.sched
        return bool(s is not None and getattr(s, "_tx", None) is not None and s._tx.is_alive())

    async def _cmd_frontend_status(self, cid, p):
        return CmdAck(cid, CmdStatus.OK, await self._run(self._frontend_status_dict))

    async def _cmd_frontend_set(self, cid, p):
        """Manual control for bench testing: {pa: bool, tr: "tx"|"rx", safe: true}.
        Refused while a TX run is active (the sequencer owns the lines then)."""
        if self.frontend is None:
            return CmdAck(cid, CmdStatus.REJECTED, {"err": "no front-end configured (frontend: none)"})
        if self._tx_running() and not p.get("safe"):
            return CmdAck(cid, CmdStatus.REJECTED, {"err": "TX running: stop TX first (or safe=true)"})
        fe = self.frontend

        def _apply():
            if p.get("safe"):
                fe.safe("operator")
            if p.get("tr") is not None:
                fe.tr_switch(str(p["tr"]).lower() == "tx")
            if p.get("pa") is not None:
                fe.pa_enable(bool(p["pa"]), manual=True)   # operator hold: no TX watchdog
            return fe.status()
        try:
            st = await self._run(_apply)
        except _rf.FrontendError as e:
            return CmdAck(cid, CmdStatus.FAILED, {"err": str(e), "frontend": fe.status()})
        return CmdAck(cid, CmdStatus.OK, {"frontend": st})

    async def _cmd_frontend_reload(self, cid, p):
        """Re-read rf_frontend.yaml (or {path}) and re-map the lines without a service
        restart: PA forced LOW and every line released before the new map is applied."""
        if self._tx_running():
            return CmdAck(cid, CmdStatus.REJECTED, {"err": "TX running: stop TX first"})
        path = p.get("path") or self._fe_path

        def _reload():
            self._frontend_stop()
            cfg = self._frontend_start(path)
            if self.backend is not None:
                self.backend.frontend = self.frontend
            return cfg
        try:
            cfg = await self._run(_reload)
        except (_rf.ConfigError, _rf.FrontendError, _mc.AttenError) as e:
            return CmdAck(cid, CmdStatus.FAILED, {"err": str(e),
                                                  "status": self._frontend_status_dict()})
        if p.get("path"):
            self._fe_path = p["path"]
        res = {"loaded": cfg.source, "frontend_kind": cfg.frontend,
               "status": self._frontend_status_dict()}
        why = self._port_conflict_now()              # say so if the open radio broke the port map
        if why:
            res["ports_conflict"] = why
        return CmdAck(cid, CmdStatus.OK, res)

    def _no_atten_err(self) -> str:
        if self._atten_watch is not None:
            return f"attenuator not connected (atten.transport: auto, {self._atten_watch.status()['state']})"
        return "no attenuator configured (atten.transport: none)"

    async def _cmd_atten_set(self, cid, p):
        """{db: float, startup: bool=false}. Bounds/step-checked before the device sees it."""
        if self.atten is None:
            return CmdAck(cid, CmdStatus.REJECTED, {"err": self._no_atten_err()})
        try:
            db = float(p.get("db"))
        except (TypeError, ValueError):
            return CmdAck(cid, CmdStatus.REJECTED, {"err": "db (float) required"})
        g, fe = self.guard, self.frontend

        def _set():
            if g is None:
                with self._atten_lock:             # the watcher may detach it
                    at = self.atten
                    if at is None:
                        raise _mc.AttenError(self._no_atten_err())
                    v = at.set_db(db)
                    s = None
                    if p.get("startup"):
                        s = at.startup_att(db)
                    return {"set_db": v, "startup_db": s, "readback_db": at.get_db()}
            with g.lock:                           # same lock the keying check reads under
                at = g.atten
                if at is None:
                    raise _mc.AttenError(self._no_atten_err())
                why = g.atten_request_ok(db, fe)
                if why is None and p.get("startup") and not g.ok_db(db):
                    why = (f"power-on attenuation {db:g} dB below the minimum safe "
                           f"{g.min_db:.2f} dB (payload guard)")
                if why is not None:
                    raise _pg.GuardRefused(why)
                v = at.set_db(db)
                s = None
                if p.get("startup"):
                    s = at.startup_att(db)
                if not g.armed:
                    g.saved_db = None              # an explicit RX setting replaces a pending restore
                rb = at.get_db()
                g.last_readback = rb
                return {"set_db": v, "startup_db": s, "readback_db": rb}
        try:
            r = await self._run(_set)
            self._atten_db_last = r.get("readback_db", r.get("set_db"))
        except _pg.GuardRefused as e:
            return CmdAck(cid, CmdStatus.REJECTED, {"err": str(e), "guard": True})
        except _mc.AttenError as e:
            return CmdAck(cid, CmdStatus.FAILED, {"err": str(e)})
        except Exception as e:
            return CmdAck(cid, CmdStatus.FAILED, {"err": f"atten: {e}"})
        return CmdAck(cid, CmdStatus.OK, r)

    async def _cmd_atten_get(self, cid, p):
        if self.atten is None:
            return CmdAck(cid, CmdStatus.REJECTED, {"err": self._no_atten_err()})
        g = self.guard

        def _get():
            with self._at_lock():
                at = self.atten
                if at is None:
                    raise _mc.AttenError(self._no_atten_err())
                d = at.status()
                if g is not None:
                    g.last_readback = d.get("att_db")
            if p.get("ident"):
                try:
                    d["model"] = at.model(); d["serial"] = at.serial(); d["firmware"] = at.firmware()
                except Exception as e:
                    d["ident_err"] = str(e)
            return d
        try:
            d = await self._run(_get)
            if d.get("att_db") is not None:
                self._atten_db_last = d["att_db"]
            return CmdAck(cid, CmdStatus.OK, d)
        except Exception as e:
            return CmdAck(cid, CmdStatus.FAILED, {"err": f"atten: {e}"})

    # Payload-level keying with RX front-end protection
    def _payload_status_dict(self) -> dict:
        """Compact (one CMD_ACK datagram): guard + PA/switch/fault + attenuator +
        the TX settings the /payload page needs for its EIRP readout."""
        fe, g = self.frontend, self.guard
        d = {"configured": bool(g is not None),
             "frontend": self._fe_cfg.frontend if self._fe_cfg else "none",
             "sim": bool(self.simulate),          # The tab marks simulated blocks
             "tx_running": self._tx_running()}
        if fe is not None:
            st = fe.status()
            pa, tr, fl = st.get("pa", {}), st.get("tr", {}), st.get("fault", {})
            d["pa"] = {"keyed": pa.get("keyed"), "level": pa.get("level"),
                       "active_high": pa.get("active_high")}
            d["tr"] = {"tx": tr.get("tx"), "mode": tr.get("mode"), "level": tr.get("level"),
                       "tx_level": tr.get("tx_level")}
            d["fault"] = {"enabled": fl.get("enabled"), "active": fl.get("active")}
            d["wd"] = {"ms": st.get("watchdog_ms"), "trips": st.get("watchdog_trips"),
                       "fault_trips": st.get("fault_trips"),
                       "remaining_ms": st.get("watchdog_remaining_ms")}
            d["hold"] = st.get("hold")
            d["fe_err"] = st.get("last_error")
            if st.get("hpa_monitor"):            # HPA temperature + supply current
                d["hpa_monitor"] = {k: v for k, v in st["hpa_monitor"].items()
                                    if k != "enabled" and not (k == "reason" and v is None)}
        v = None
        with self._at_lock():                      # same lock as attach/detach
            at = self.atten
            if at is not None:
                try:
                    v, err = round(float(at.get_db()), 2), None
                except Exception as e:
                    v, err = None, str(e)[:80]
                if g is not None:
                    g.last_readback = v
        if at is not None:
            d["atten"] = {"att_db": v, "max_db": at.max_db, "step_db": at.step,
                          "transport": at.t.name}
            if err:
                d["atten"]["err"] = err
        else:
            d["atten"] = None
        d["devices"] = self._devices_status(v, d.get("hpa_monitor"))
        if g is not None:
            d["guard"] = g.status()
        b = self.backend
        if b is not None:
            d["center_hz"] = b.config.center_freq
            d["tx_center_hz"] = getattr(b, "_tx_lo", None) or b.config.center_freq
            d["tx_atten_db"] = (self._tx_atten_db if self._tx_atten_db is not None
                                else float(b.config.tx_gain))
            d["tx_atten_src"] = "set_tx_atten" if self._tx_atten_db is not None else "init"
        return d

    async def _cmd_payload_status(self, cid, p):
        return CmdAck(cid, CmdStatus.OK, await self._run(self._payload_status_dict))

    async def _cmd_payload_key(self, cid, p):
        """{on: bool}.  Manual payload key: RX attenuator to its protect value, switch to
        TX, settle, HPA on (the guard confirms the switch and the attenuator first);
        off = HPA off, settle, switch to RX, then the guard restores
        the operator's RX attenuation.  Keying is refused without a payload guard
        and while a TX run owns the lines; unkeying is the safe direction and is
        always carried out (HPA off, switch to RX), guard or not."""
        fe, g = self.frontend, self.guard
        if fe is None:
            return CmdAck(cid, CmdStatus.REJECTED, {"err": "no front-end configured (frontend: none)"})
        on = bool(p.get("on"))
        if on and g is None:
            return CmdAck(cid, CmdStatus.REJECTED,
                          {"err": "payload protection not configured (add a payload: section "
                                  "to rf_frontend.yaml)"})
        if on and self._tx_running():
            return CmdAck(cid, CmdStatus.REJECTED,
                          {"err": "TX running: the TX run keys the HPA itself (through the same guard)"})

        def _key():
            if on:
                try:
                    fe.tr_switch(True)
                    time.sleep(fe.cfg.tr_switch.settle_ms / 1000.0)
                    fe.pa_enable(True, manual=True)
                    time.sleep(fe.cfg.pa_enable.settle_ms / 1000.0)
                except Exception:
                    fe.safe("payload_key failed")
                    raise
            else:
                if fe._keyed:
                    fe.pa_enable(False)
                    time.sleep(fe.cfg.pa_enable.settle_ms / 1000.0)
                fe.tr_switch(False)                  # guard restores RX atten after this
            return self._payload_status_dict()
        try:
            st = await self._run(_key)
        except _rf.FrontendError as e:
            return CmdAck(cid, CmdStatus.FAILED,
                          {"err": str(e)[:300], "status": await self._run(self._payload_status_dict)})
        return CmdAck(cid, CmdStatus.OK, {"status": st})

    async def _cmd_payload_restore(self, cid, p):
        """Restore the operator's RX attenuation held back after a SAFE / watchdog /
        fault drop.  Refused while keyed or with the switch in TX."""
        g = self.guard
        if g is None:
            return CmdAck(cid, CmdStatus.REJECTED, {"err": "payload protection not configured"})
        err = await self._run(g.restore_now, self.frontend)
        if err:
            return CmdAck(cid, CmdStatus.REJECTED, {"err": err})
        return CmdAck(cid, CmdStatus.OK, {"status": await self._run(self._payload_status_dict)})

    # secure messages
    # The radio holds no key material. With DPMG_SECURE_MSG=1 the decode pool
    # parses sealed bodies (CRC-checked, never opened) and the service forwards
    # each one to the ground station as a SEALED_FRAME datagram; the GUI / bench
    # opens it there. Sealed TX frames arrive pre-built from the ground as raw
    # `bits` in the scenario spec. With the flag off nothing here runs.
    def _secure_start(self):
        """Log the crypto line for the record; wire the worker callback."""
        self._sealed_seq = 0
        self._sealed_seen = 0
        self._sealed_fwd = 0
        self._sealed_t = 0.0
        if not _sf.secure_msg_enabled():
            log.info("secure-msg: off (%s unset)", _sf.FLAG_SECURE_MSG)
            return
        # the radio never encrypts: record the library line without the AEAD
        # self-test (the radio needs no crypto library at all)
        env = _sf.log_startup(log, _sf.probe(run_selftest=False))
        log.info("secure-msg: radio mode = forward sealed frames to the ground (no keys on the radio); %s",
                 env.summary())

    def _on_sealed(self, ch, bodies, f_center_hz=0.0):
        """ChannelizerWorker callback (service thread): forward every sealed
        body the decode pool found. Fire-and-forget like WATCH_UPDATE."""
        self._sealed_seen += len(bodies)
        self._sealed_t = time.time()
        if not self.gui_addrs or self.ep is None:
            return
        band = str(getattr(ch, "ch_name", "") or f"ch{getattr(ch, 'idx', 0)}")
        for body in bodies:
            self._sealed_seq = (self._sealed_seq + 1) & 0xFFFFFFFF
            wire = SealedFrame(self.serial, band[:16], float(f_center_hz or getattr(ch, "f_center_hz", 0.0)),
                               self._sealed_seq, time.time(), bytes(body)[:300])
            for addr in list(self.gui_addrs):
                try:
                    self.loop.call_soon_threadsafe(self.ep.send, wire, addr)
                except Exception:                                  # noqa: BLE001
                    pass
            self._sealed_fwd += 1

    def _plan_secure_entries(self) -> int:
        try:
            return sum(1 for e in (self.plan or {}).get("entries", [])
                       if (e.get("frame") or {}).get("secure") or (e.get("params") or {}).get("secure"))
        except Exception:                                          # noqa: BLE001
            return 0

    def _secure_brief(self) -> dict:
        """Compact radio-side state for get_config and the STATUS flags: no
        keys, no verdicts (those are the ground station's)."""
        on = _sf.secure_msg_enabled()
        n_secure = self._plan_secure_entries()
        d = {"flag": on, "keys_on_radio": False, "plan_secure_entries": n_secure,
             "sealed_seen": int(getattr(self, "_sealed_seen", 0)),
             "sealed_forwarded": int(getattr(self, "_sealed_fwd", 0)),
             "last_sealed_t": float(getattr(self, "_sealed_t", 0.0) or 0.0)}
        d["active"] = bool(on and n_secure > 0)          # sealed frames expected and forwarded
        d["fault"] = bool((not on) and n_secure > 0)     # a secure plan entry can never decode
        if d["fault"]:
            d["err"] = f"plan has {n_secure} secure entr{'y' if n_secure == 1 else 'ies'} but {_sf.FLAG_SECURE_MSG} is off"
        return d

    async def _cmd_secure_status(self, cid, p):
        """Radio-side secure-messaging status: the flag, the crypto environment of
        this host (record only; the radio does not encrypt), sealed-frame counts.
        Keys, verdicts and counters live on the ground station."""
        env = _sf.probe(run_selftest=False)
        d = {"flag": _sf.secure_msg_enabled(), "crypto": env.as_dict(), "brief": self._secure_brief(),
             "keys_on_radio": False, "aead": _sf.AEAD_NAME,
             "unsupported_codings": _sf.unsupported_codings(long_header=True)}
        return CmdAck(cid, CmdStatus.OK, d)

    async def _cmd_secure_enable(self, cid, p):
        return CmdAck(cid, CmdStatus.REJECTED,
                      {"err": "keys live on the ground station; the radio only forwards sealed frames "
                              f"(set {_sf.FLAG_SECURE_MSG} in the service environment to parse them)"})

    async def _cmd_secure_sim_inject(self, cid, p):
        """Simulator only (--simulate): forward the given sealed bodies (hex
        list) as if the decode pool had found them, so the ground station's
        open path can be exercised without RF. Refused on a real radio."""
        if not self.simulate:
            return CmdAck(cid, CmdStatus.REJECTED, {"err": "secure_sim_inject is a simulator-only aid"})
        if not _sf.secure_msg_enabled():
            return CmdAck(cid, CmdStatus.REJECTED, {"err": f"{_sf.FLAG_SECURE_MSG} is off"})
        try:
            bodies = [bytes.fromhex(h) for h in (p.get("bodies") or [])][:16]
        except ValueError:
            return CmdAck(cid, CmdStatus.REJECTED, {"err": "bodies must be hex strings"})

        class _Ch:
            ch_name, idx, f_center_hz = str(p.get("band", "SIM-00"))[:16], 0, float(p.get("f_center_hz", 0.0))
        self._on_sealed(_Ch(), bodies)
        return CmdAck(cid, CmdStatus.OK, {"forwarded": len(bodies), "brief": self._secure_brief()})

    async def _cmd_secure_keys_reload(self, cid, p):
        return CmdAck(cid, CmdStatus.REJECTED, {"err": "no keys on the radio: reload them on the ground station"})

    async def _cmd_gps_status(self, cid, p):
        # GPSDO lock + ext-ref (10 MHz / 1PPS) presence for the SDR-tab indicator.
        # Ant_bias + gnss ride along (host GPIO / gpsd, so they are
        # there before init too; null on non-G40 kinds).  Default executor, not
        # the 2-thread hw pool: the gpsd read can take up to ~1.5 s.
        ant = await self.loop.run_in_executor(None, self._gps_antenna_info)
        # this host's OS clock rides along (also before init), so a
        # console can show the skew against its own clock and offer a sync.
        ant["host_time"] = time.time()
        if self.backend is None:
            return CmdAck(cid, CmdStatus.REJECTED, {"err": "not initialized", **ant})
        st = await self._run(self.backend.gps_status)
        if self._gps_sim is not None and not st.get("supported"):
            st = self._sim_ref_status()
        st.update(ant)
        return CmdAck(cid, CmdStatus.OK, st)

    def _cmd_set_time(self, cid, p):
        """Step this host's OS clock to the console's UTC
        ({"utc": epoch seconds}). Needs CAP_SYS_TIME, which the privileged radio
        containers have; a plain process gets a clear refusal instead. The
        mission itself is keyed from the console's clock, so this is for the
        radio's own logs, recordings and the GPS-time compare, not for timing.
        Refuses a time before 2024 or more than a year ahead of this host."""
        try:
            want = float(p.get("utc"))
        except (TypeError, ValueError):
            return CmdAck(cid, CmdStatus.REJECTED, {"err": "utc (epoch seconds) required"})
        before = time.time()
        if want < 1_704_067_200.0 or want - before > 366 * 86400:
            return CmdAck(cid, CmdStatus.REJECTED,
                          {"err": f"refusing to set the clock to {want:.0f}",
                           "host_time": before})
        try:
            time.clock_settime(time.CLOCK_REALTIME, want)
        except PermissionError:
            return CmdAck(cid, CmdStatus.REJECTED,
                          {"err": "no permission to set the clock (needs CAP_SYS_TIME)",
                           "host_time": time.time(), "offset_s": before - want})
        except OSError as e:
            return CmdAck(cid, CmdStatus.REJECTED,
                          {"err": f"clock not set: {e}", "host_time": time.time()})
        log.info("set_time: OS clock stepped by %+.3f s", want - before)
        return CmdAck(cid, CmdStatus.OK,
                      {"host_time": time.time(), "stepped_s": want - before})

    def _gps_antenna_info(self) -> dict:
        """Antenna bias readback: {"ant_bias": bool|None, "gnss": dict|None}.
        Blocking (i2c + gpsd socket), never raises, compact for the 1400 B ack."""
        if self._gps_sim is not None:
            g = self._gps_sim.gnss()
            self._keep_gnss(g)
            if getattr(self, "kind", None) == "x40":
                return {"ant_bias": None, "ant_bias_err": X40_BIAS_NOTE, "gnss": g}
            return {"ant_bias": self._gps_sim.bias, "gnss": g}
        if not self._gps_real:
            if self._gnss_addr is None:
                return {"ant_bias": None, "gnss": None}
            g = _ga.query_gpsd(self._gnss_addr)
            self._keep_gnss(g)
            return {"ant_bias": None, "ant_bias_err": X40_BIAS_NOTE, "gnss": g}
        b = _ga.read_bias()
        self._ant_bias_last, self._ant_bias_t = b.get("ant_bias"), time.monotonic()
        out = {"ant_bias": b.get("ant_bias"), "gnss": _ga.query_gpsd(self._gnss_addr)}
        self._keep_gnss(out["gnss"])
        if b.get("err"):
            out["ant_bias_err"] = b["err"]
        elif b.get("ant_bias") is None:
            out["ant_bias_err"] = f"ch4 regs {b.get('raw')} (PWM, not a GPIO level)"
        return out

    def _keep_gnss(self, g):
        """The fix part of a gnss read, kept for get_config (the status line and
        the clock colour need the fix next to the lock, at 1 Hz, without a gpsd
        round trip each time)."""
        self._gnss_last = None if not g else {"fix": g.get("fix"), "sats_used": g.get("sats_used")}
        self._gnss_t = time.monotonic()

    async def _gnss_refresh(self):
        """Read gpsd for the cache when it is older than GNSS_CACHE_S. Default
        executor: the read can take up to 1.5 s."""
        if self._gnss_addr is None and self._gps_sim is None:
            return
        if time.monotonic() - self._gnss_t < GNSS_CACHE_S:
            return
        if self._gps_sim is not None:
            self._keep_gnss(self._gps_sim.gnss())
            return
        self._gnss_busy = True
        try:
            g = await self.loop.run_in_executor(None, _ga.query_gpsd, self._gnss_addr)
            self._keep_gnss(g)
        finally:
            self._gnss_busy = False

    def _sim_ref_status(self) -> dict:
        """--simulate stand-in for EpiqBackend.gps_status(), driven by the sim
        antenna: GPSDO locks ~45 s after the bias goes on (gpsdo clock only)."""
        be = self.backend
        clk = (getattr(getattr(be, "config", None), "clock_source", None) or "internal").lower()
        choice = _g40_clock.CLOCK_CHOICES.get(clk) or _g40_clock.CLOCK_CHOICES["internal"]
        locked = clk == "gpsdo" and self._gps_sim.gpsdo_locked()
        ext = choice["ref"] == "external"
        if choice["ref"] == "host":
            # the X40 GPSDO: selected as the host reference, lock not readable
            return {"supported": True, "sim": True, "ref_clock": "host",
                    "gpsdo_enabled": False, "gpsdo_locked": None,
                    "gpsdo_note": "the X40 GPSDO lock cannot be read through libsidekiq",
                    "pps_present": True, "pps_source": 0,
                    "clock": {"choice": clk, "label": choice["label"], "ref": "host",
                              "pps": choice["pps"], "ten_mhz": None, "clock_ppm": None, "sim": True},
                    "ten_mhz": None, "clock_ppm": None}
        # the simulated 10 MHz is there unless the sim antenna says to lose it
        # (DPMG_SIM_LOSE_10MHZ=1 for a GUI check of the LOST display)
        lost = ext and _os.environ.get("DPMG_SIM_LOSE_10MHZ") == "1"
        ppm = -136.7 if lost else (-0.056 if ext else 0.06)
        clock = {"choice": clk, "label": choice["label"], "ref": choice["ref"],
                 "pps": choice["pps"], "pps_line": choice["pps"],
                 "hmc_input": 1 if ext else 0, "hmc_source": "written", "hmc_locked": True if ext else None,
                 "pps_present": True, "pps_age_s": 0.4, "clock_ppm": ppm,
                 "ticks_per_s": 40e6 * (1 + ppm * 1e-6),
                 "ten_mhz": ("LOST" if lost else "in use") if ext else None,
                 "why": f"card clock {ppm:+.3f} ppm against the 1 PPS", "sim": True}
        st = {"supported": True, "sim": True, "ref_clock": "external (carrier)" if ext else clk,
              "gpsdo_enabled": clk == "gpsdo", "gpsdo_locked": locked,
              "freq_accuracy_ppm": 0.004 if locked else None,
              "pps_present": True, "pps_source": 0, "pps_line": choice["pps"],
              "clock": clock, "ten_mhz": clock["ten_mhz"], "clock_ppm": ppm}
        if ext:
            st["ext_ref_present"] = not lost
        return st

    async def _cmd_gps_antenna_bias(self, cid, p):
        """{"enable": bool} switches the G40 3.3 V GPS antenna bias
        (ant-bias-en, active-low PCA9685 ch4) and returns the register readback.
        Only for active antennas (some passive antennas are a DC short).  Not
        persistent across a G40 reboot unless the boot unit is installed."""
        if "enable" not in p or not isinstance(p.get("enable"), bool):
            return CmdAck(cid, CmdStatus.REJECTED, {"err": "need {\"enable\": true|false}"})
        en = p["enable"]
        if getattr(self, "kind", None) == "x40":
            return CmdAck(cid, CmdStatus.REJECTED, {"err": X40_BIAS_NOTE, "ant_bias": None})
        if self._gps_sim is not None:
            r = self._gps_sim.set_bias(en)
        elif self._gps_real:
            r = await self.loop.run_in_executor(None, _ga.set_bias, en)
        else:
            return CmdAck(cid, CmdStatus.REJECTED,
                          {"err": f"GPS antenna bias is G40-only (kind {self.kind})",
                           "ant_bias": None})
        who = str(p.get("by") or "unnamed client")[:48]     # Shown in every GUI
        log.info("gps_antenna_bias enable=%s by %s -> %s", en, who, r)
        self._ant_bias_last, self._ant_bias_t = r.get("ant_bias"), time.monotonic()
        if r.get("ok"):
            self._ant_bias_by = {"by": who, "enable": en,
                                 "at": time.strftime("%Y-%m-%d %H:%M", time.localtime())}
        res = {k: r.get(k) for k in ("ant_bias", "raw", "err", "sim") if r.get(k) is not None}
        res["ant_bias"] = r.get("ant_bias")
        return CmdAck(cid, CmdStatus.OK if r.get("ok") else CmdStatus.FAILED, res)

    # The full current configuration, for every GUI view

    # radio role

    async def _cmd_set_role(self, cid, p):
        """What this radio is acting as. Kept across service restarts. Refused
        while TX runs, so a role change never leaves a transmitter keyed that
        the new role would not have allowed."""
        role = str(p.get("role") or "")
        if not _role.known(role):
            return CmdAck(cid, CmdStatus.REJECTED,
                          {"err": f"unknown role {role!r}", "roles": list(_role.ORDER)})
        if role == self._role:
            return CmdAck(cid, CmdStatus.OK, {"role": role, "noop": True})
        if self._tx_running():
            return CmdAck(cid, CmdStatus.REJECTED,
                          {"err": "stop TX before changing the radio role", "role": self._role})
        if self._fpga_busy():
            return CmdAck(cid, CmdStatus.REJECTED, {"err": "FPGA image load in progress"})
        if self._bitpipe is not None and role not in ("any", "bitpipe"):
            return CmdAck(cid, CmdStatus.REJECTED,
                          {"err": "the bitpipe modem is attached: bitpipe_stop first",
                           "role": self._role})
        if role == "dcfe" and self.backend is not None:
            return CmdAck(cid, CmdStatus.REJECTED,
                          {"err": "close the radio first: dCFE needs it free", "role": self._role})
        by = str(p.get("by") or "")[:40] or None
        try:
            await self.loop.run_in_executor(None, _role.save, self._role_path, role, by)
        except OSError as e:
            log.warning("role %s not saved to %s: %s", role, self._role_path, e)
        prev, self._role, self._role_by = self._role, role, by
        log.warning("radio role %s changed to %s (by %s)", prev, role, by or "?")
        return CmdAck(cid, CmdStatus.OK, {"role": role, "prev": prev,
                                          "label": _role.label(role)})

    # FPGA image

    def _fpga_family(self):
        if self.kind == "mock":
            try:
                k = json.loads(self._init_sig).get("kind") if self._init_sig else None
            except ValueError:
                k = None
            # a simulated radio stays the kind it was last opened as
            if k:
                self._fpga_mock_kind = k
            return _fpga.family(k or getattr(self, "_fpga_mock_kind", None))
        return _fpga.family(self.kind)

    def _fpga_busy(self) -> bool:
        j = self._fpga_job
        return bool(j and j.get("state") == "running")

    def _fpga_run(self, argv, timeout):
        """One tool call, through the sim X410 when the service is simulated."""
        run = self._fpga_runner
        if run is None and self.simulate:
            run = self._fpga_runner = _fpga.SimX4xx()
        return (run or _fpga.run_tool)(
            argv, {"UHD_IMAGES_DIR": _fpga.x4xx_images_dir()}, timeout)

    def _fpga_args(self) -> str:
        """Device args that name this radio: the ones it was opened with, else
        DPMG_UHD_ARGS from the launch, else the bare type."""
        try:
            a = (json.loads(self._init_sig) or {}).get("device_args") if self._init_sig else None
        except ValueError:
            a = None
        a = a or _os.environ.get("DPMG_UHD_ARGS", "")
        if self._fpga_family() == "x4xx":
            a = _fpga.x4xx_args(a)
            if self.simulate and not _fpga.args_get(a, "addr"):
                a = _fpga.args_set(a, "addr", "192.168.8.25")
        return a

    def _fpga_state(self) -> dict:
        try:
            with open(self._fpga_path) as fh:
                return json.load(fh) or {}
        except (OSError, ValueError):
            return {}

    def _fpga_save(self, **kw):
        d = self._fpga_state()
        d.update(kw, t=time.time())
        tmp = self._fpga_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(d, fh)
        _os.replace(tmp, self._fpga_path)

    async def _epiq_fpga_capture(self) -> dict:
        """Read the NV100 FPGA versions from the open cards and keep a copy, so the
        FPGA panel can show them while the radio is closed. {} when nothing read."""
        try:
            rb = await self._run(_fpga.epiq_readback, self.backend)
        except Exception:
            log.exception("NV100 FPGA version read")
            return {}
        cards = {k: v for k, v in rb.items() if k != "lib" and isinstance(v, dict)}
        if any(c.get("version") for c in cards.values()):
            try:
                self._fpga_save(epiq={"cards": cards, "lib": rb.get("lib"),
                                      "read_t": time.time()})
            except OSError:
                log.exception("saving the NV100 FPGA versions")
            return rb
        return {}

    async def _fpga_find_now(self, max_age=5.0):
        """uhd_find_devices for this radio (x4xx), cached a few seconds."""
        t, dev = self._fpga_find
        if time.monotonic() - t < max_age:
            return dev
        args = self._fpga_args()
        rc, out = await self.loop.run_in_executor(
            None, self._fpga_run, _fpga.find_cmd(args), _fpga.FIND_TIMEOUT_S)
        dev = _fpga.find_device(_fpga.parse_find(out), addr=_fpga.args_get(args, "addr"))
        self._fpga_find = (time.monotonic(), dev)
        return dev

    async def _fpga_find_start(self):
        try:
            dev = await self._fpga_find_now(0.0)
            log.info("X410 FPGA image at start: %s", (dev or {}).get("fpga") or "not found")
        except Exception:
            log.exception("FPGA image read at start")

    def _fpga_brief(self) -> dict | None:
        """For get_config: image, version and any load in progress."""
        fam = self._fpga_family()
        if fam is None:
            return None
        st = self._fpga_state()
        d = {"fam": fam}
        if fam == "x4xx":
            dev = self._fpga_find[1]
            d["img"] = (dev or {}).get("fpga") or (st.get("probe") or {}).get("image")
            d["ver"] = (st.get("probe") or {}).get("version")
        elif fam == "b2xx":
            d["img"] = st.get("image") or "UHD default"
        j = self._fpga_job
        if j:
            d["job"] = j.get("state")
            d["step"] = str(j.get("step") or "")[:60]
        return d

    async def _cmd_fpga_status(self, cid, p):
        """Which FPGA image this radio runs, what else it can load, and the
        last load. probe: also run uhd_usrp_probe (x4xx, radio closed only)."""
        fam = self._fpga_family()
        st = self._fpga_state()
        r = {"family": fam, "how": _fpga.HOW.get(fam), "loadable": fam in ("x4xx", "b2xx"),
             "tx_active": bool(self._tx_running()), "open": self.backend is not None}
        if fam is None:
            r["note"] = "no FPGA image control for this radio"
        elif fam == "x4xx":
            args = self._fpga_args()
            r["args"] = args
            r["choices"] = _fpga.x4xx_choices(_fpga.x4xx_images_dir()) or (
                list(_fpga.SimX4xx.VERSIONS) if self.simulate else [])
            if not self._fpga_busy():
                dev = await self._fpga_find_now(0.0 if p.get("refresh") else 5.0)
                r["image"] = (dev or {}).get("fpga")
                r["found"] = dev is not None
            probe = dict(st.get("probe") or {})
            if p.get("probe") and not self._fpga_busy():
                if self.backend is not None:
                    probe = dict(_fpga.uhd_readback(self.backend), image=r.get("image"))
                else:
                    rc, out = await self.loop.run_in_executor(
                        None, self._fpga_run, _fpga.probe_cmd(args), _fpga.PROBE_TIMEOUT_S)
                    probe = dict(_fpga.parse_probe(out), image=r.get("image"), rc=rc)
                    if rc == 0:
                        self._fpga_save(probe=probe)
            r["version"] = probe.get("version")
            r["git_hash"] = probe.get("git_hash")
            r["probed_image"] = probe.get("image")
            if not _fpga.args_get(args, "addr"):
                r["loadable"] = False
                r["note"] = "no radio address: init once or set DPMG_UHD_ARGS"
        elif fam == "b2xx":
            folder = _fpga.uhd_images_dir()
            r["choices"] = ["UHD default"] + _fpga.b2xx_choices(folder)
            r["image"] = st.get("image") or "UHD default"
            r["folder"] = folder
            if self.backend is not None:
                r.update({k: v for k, v in _fpga.uhd_readback(self.backend).items() if v})
            r["note"] = "UHD loads the image each time the radio opens"
        elif fam == "epiq":
            r["note"] = "read only here: NV100 flashing uses the Epiq procedure"
            rb = None
            if self.backend is not None:
                rb = await self._epiq_fpga_capture()
                if not rb:
                    r["note"] += "; the open cards did not answer the version read"
            else:
                # the versions can only be read while the service holds the cards;
                # a closed radio answers with the copy saved at the last read
                saved = st.get("epiq") or {}
                if saved.get("cards"):
                    rb = dict(saved["cards"], lib=saved.get("lib"))
                    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(saved.get("read_t", 0)))
                    r["read_at"] = saved.get("read_t")
                    r["note"] += (f"; versions as read at {when}, the last time the radio was "
                                  "started (start it to read them again)")
                else:
                    r["note"] += "; start the radio once to read the versions"
            r["cards"] = {k: v for k, v in (rb or {}).items() if k != "lib"}
            r["lib"] = (rb or {}).get("lib")
            v = sorted({c.get("version") for c in r["cards"].values() if c.get("version")})
            r["version"] = ", ".join(v) or None
        j = self._fpga_job
        if j:
            r["job"] = {k: j.get(k) for k in ("state", "image", "prev", "step", "result", "err")}
            r["job"]["log"] = [str(x)[:100] for x in j.get("log", [])[-5:]]
        return CmdAck(cid, CmdStatus.OK, r)

    async def _cmd_fpga_load(self, cid, p):
        """Load another FPGA image. confirm must repeat the image name (the GUI
        confirm dialog sends it). Refused while TX runs. Runs in the
        background; fpga_status reports each step and the readback."""
        fam = self._fpga_family()
        image = str(p.get("image") or "")
        if fam not in ("x4xx", "b2xx"):
            return CmdAck(cid, CmdStatus.REJECTED,
                          {"err": "this radio's FPGA image is read only here"
                                  + (" (NV100 flashing uses the Epiq procedure)" if fam == "epiq" else "")})
        if self._fpga_busy():
            return CmdAck(cid, CmdStatus.REJECTED, {"err": "an FPGA image load is already running"})
        if self._tx_running():
            return CmdAck(cid, CmdStatus.REJECTED, {"err": "stop TX before loading an FPGA image"})
        if p.get("confirm") != image:
            return CmdAck(cid, CmdStatus.REJECTED, {"err": "confirm must name the image to load"})
        if fam == "x4xx":
            choices = _fpga.x4xx_choices(_fpga.x4xx_images_dir()) or (
                list(_fpga.SimX4xx.VERSIONS) if self.simulate else [])
            args = self._fpga_args()
            if not _fpga.args_get(args, "addr"):
                return CmdAck(cid, CmdStatus.REJECTED,
                              {"err": "no radio address: init once or set DPMG_UHD_ARGS"})
        else:
            choices = ["UHD default"] + _fpga.b2xx_choices(_fpga.uhd_images_dir())
            args = None
        if image not in choices:
            return CmdAck(cid, CmdStatus.REJECTED,
                          {"err": f"{image!r} is not one of the images on the radio host",
                           "choices": choices})
        prev = (self._fpga_find[1] or {}).get("fpga") if fam == "x4xx" else (
            self._fpga_state().get("image") or "UHD default")
        self._fpga_job = {"state": "running", "image": image, "prev": prev, "fam": fam,
                          "step": "starting", "log": [], "result": None, "err": None,
                          "t0": time.time()}
        log.warning("FPGA load requested: %s to %s (%s)", prev, image, fam)
        asyncio.create_task(self._fpga_job_run(fam, image, args))
        return CmdAck(cid, CmdStatus.OK, {"started": True, "image": image, "prev": prev})

    async def _fpga_job_run(self, fam, image, args):
        job = self._fpga_job

        def step(s):
            job["step"] = s
            job["log"].append(time.strftime("%H:%M:%S ") + s)
            log.warning("FPGA load: %s", s)

        def fail(msg):
            job["state"], job["err"] = "failed", msg
            step("FAILED: " + msg)

        try:
            init_p = json.loads(self._init_sig) if self._init_sig else None
            if self.backend is not None:
                step("stopping and closing the radio")
                await self._teardown()
            if fam == "x4xx":
                addr = _fpga.args_get(args, "addr")
                step(f"loading {image}; the radio drops off the network")
                rc, out = await self.loop.run_in_executor(
                    None, self._fpga_run, _fpga.load_cmd(args, image), _fpga.LOAD_TIMEOUT_S)
                for line in [x for x in out.splitlines() if x.strip()][-3:]:
                    job["log"].append(line.strip()[:100])
                if rc != 0:
                    return fail(f"uhd_image_loader exit {rc}")
                step("waiting for the radio to come back")
                t_end = time.monotonic() + _fpga.COMEBACK_TIMEOUT_S
                dev = None
                await asyncio.sleep(1.0)
                while time.monotonic() < t_end:
                    self._fpga_find = (0.0, None)
                    dev = await self._fpga_find_now(0.0)
                    if dev and dev.get("fpga"):
                        break
                    await asyncio.sleep(3.0 if not self.simulate else 0.2)
                if not dev:
                    return fail(f"radio at {addr} did not come back")
                if dev.get("fpga") != image:
                    return fail(f"radio reports {dev.get('fpga')}, expected {image}")
                step(f"radio back with {dev.get('fpga')}; probing")
                rc, out = await self.loop.run_in_executor(
                    None, self._fpga_run, _fpga.probe_cmd(args), _fpga.PROBE_TIMEOUT_S)
                info = _fpga.parse_probe(out)
                if rc != 0:
                    return fail(f"uhd_usrp_probe exit {rc}")
                probe = dict(info, image=dev.get("fpga"), rc=rc)
                await self.loop.run_in_executor(None, lambda: self._fpga_save(probe=probe))
                # caps follow the new image; get_config shows it, the GUI refetches
                self._caps_image = dev.get("fpga")
                self._cfg_seq += 1
                job["result"] = {"image": dev.get("fpga"), "version": info.get("version"),
                                 "git_hash": info.get("git_hash"), "probe": "ok"}
                step(f"done: {dev.get('fpga')} v{info.get('version') or '?'}, probe ok; "
                     "radio left closed, start it again")
            else:
                folder = _fpga.uhd_images_dir()
                path = "" if image == "UHD default" else _os.path.join(folder, image)
                await self.loop.run_in_executor(None, lambda: self._fpga_save(image=path and image))
                if init_p is None:
                    job["result"] = {"image": image, "probe": "pending",
                                     "note": "loads at the next start"}
                    step(f"{image} chosen; UHD loads it at the next start")
                else:
                    step(f"reopening the radio with {image}")
                    init_p = dict(init_p)
                    init_p["device_args"] = _fpga.args_set(init_p.get("device_args") or "type=b200",
                                                           "fpga", path or None)
                    ack = await self._cmd_init(0, init_p)
                    if ack.status != CmdStatus.OK:
                        return fail("reopen failed: " + str((ack.result or {}).get("err")))
                    rb = _fpga.uhd_readback(self.backend) if self.backend is not None else {}
                    job["result"] = dict(rb, image=image, probe="ok")
                    step(f"done: radio open with {image}"
                         + (f" (FPGA v{rb['version']})" if rb.get("version") else ""))
            if job["state"] == "running":
                job["state"] = "done"
        except Exception as e:
            log.exception("FPGA load")
            fail(str(e))

    async def _cmd_get_config(self, cid, p):
        """What the radio is set to right now, so the operator console, the
        engineering view and every other GUI show the same truth (polled ~1 Hz and
        after each state change). Read-only; service state only, plus a cached G40
        bias readback (i2c, at most every ANT_BIAS_CACHE_S)."""
        if (self._gps_real and self._gps_sim is None
                and time.monotonic() - self._ant_bias_t > ANT_BIAS_CACHE_S):
            b = await self.loop.run_in_executor(None, _ga.read_bias)
            self._ant_bias_last, self._ant_bias_t = b.get("ant_bias"), time.monotonic()
        if p.get("ports"):                           # the port map chain text too
            return CmdAck(cid, CmdStatus.OK, self._ports_report())
        if not getattr(self, "_gnss_busy", False):   # fix for the next poll, never waited on
            asyncio.ensure_future(self._gnss_refresh())
        return CmdAck(cid, CmdStatus.OK, self._config_snapshot())

    def _card_fields(self) -> dict:
        """Card temperatures for get_config: card_temps (live, deg C),
        card_temps_last (a card with no live reading, e.g. after close:
        [deg C, seconds since it was read]) and card_names (kept from the last
        open radio, so a closed B200 still reads "B200mini")."""
        live = getattr(self, "_card_temps", {})
        now_w = time.time()
        backend = getattr(self, "backend", None)
        names = getattr(backend, "card_names", None) if backend is not None else None
        return {
            "card_temps": {str(c): v[0] for c, v in sorted(live.items())},
            "card_temps_last": {str(c): [v[0], int(now_w - v[1])] for c, v in
                                sorted(getattr(self, "_card_last", {}).items())
                                if c not in live},
            "card_names": dict(names or getattr(self, "_card_names_last", None) or {}),
        }

    def _config_snapshot(self) -> dict:
        """Compact (one CMD_ACK): keys are additive, a GUI uses what it knows."""
        be = self.backend
        try:
            init = json.loads(self._init_sig) if self._init_sig else {}
        except ValueError:
            init = {}
        ex = init.get("extras") or {}
        d = {"v": 1, "cfg_seq": self._cfg_seq, "inited": be is not None,
             "state": int(self._radio_state()), "tx_active": bool(self._tx_running()),
             "log": {"detections": getattr(self._det_log_fp, "name", None),
                     "telemetry": getattr(self._telem_log_fp, "name", None)}}
        if be is not None:
            cfg = be.config
            d.update({
                "kind": init.get("kind", self.kind) if self.kind == "mock" else self.kind,
                "center_hz": float(cfg.center_freq),
                "tx_center_hz": float(getattr(be, "_tx_lo", None)
                                      or ex.get("tx_center_freq") or cfg.center_freq),
                "sample_rate": float(init.get("sample_rate") or cfg.sample_rate),
                "achieved_rate": float(getattr(be, "_achieved_rate", cfg.sample_rate)),
                "tx_sample_rate": float(getattr(be, "_tx_rate", None) or 0) or None,
                "rx_gain": float(cfg.rx_gain), "tx_gain": float(cfg.tx_gain),
                "clock_source": str(cfg.clock_source or "internal"),
                "rx_antenna": init.get("rx_antenna", cfg.rx_antenna),
                "tx_antenna": init.get("tx_antenna"),
                "card": ex.get("card", 0), "tx_card": ex.get("tx_card"),
                "chan_mode": getattr(be, "_chan_mode", None) or ex.get("chan_mode"),
                "psd": {"nfft": init.get("psd_nfft"), "window": init.get("psd_window"),
                        "buffer": init.get("psd_buffer")}})
        src = self._tx_src or {}
        if src:
            wf = {"mode": src.get("mode"), "desc": str(self._tx_wf_desc or "")[:120]}
            if src.get("mode") == "tone":
                wf.update(offset_hz=src.get("off"), ampl=src.get("ampl"))
            elif src.get("path"):
                wf.update(path=str(src["path"])[-160:], rate=src.get("rate"))
            d["tx_wf"] = wf
        d["plan_entries"] = len((self.plan or {}).get("entries", [])) if self.plan else None
        d["plan_anchor"] = self._plan_anchor
        if getattr(self, "kind", None) == "x40":
            d["ant_bias"] = None                     # the ZynqMP's, not read or set here
        elif self._gps_sim is not None:
            d["ant_bias"] = self._gps_sim.bias
        elif self._gps_real:
            d["ant_bias"] = self._ant_bias_last
        else:
            d["ant_bias"] = None
        d["ref_locked"] = self._ref_locked
        # the GNSS fix next to the lock: a GPSDO locked to the 1 PPS with no fix is
        # not on GPS time. None = no GPS receiver read here (or gpsd not answering)
        if self._gnss_addr is not None or self._gps_sim is not None:
            d["gnss"] = self._gnss_last
        # G40: the clock check, short form (the ack is one datagram); gps_status
        # carries the whole dict. The ground health log reads clock_ppm and ten_mhz.
        ck = getattr(self, "_clock", None)
        if ck is None and self._gps_sim is not None:
            ck = self._sim_ref_status().get("clock")
        d["clock"] = ck and {k: ck.get(k) for k in ("choice", "ten_mhz", "clock_ppm", "pps_present",
                                                   "hmc_input", "hmc_locked")}
        d["ant_bias_by"] = getattr(self, "_ant_bias_by", None)  # Last switch, this service run
        d["imu"] = self._imu_tracker().summary()
        d["baro"] = self._baro_tracker().summary()
        try:                                                         # HPA monitor on the cards
            hm = self.frontend.hpa_monitor_state() if self.frontend is not None else None
            d["hpa_monitor"] = hm and {k: hm.get(k) for k in ("state", "hpa_temp_c", "hpa_current_a", "warn_c",
                                                              "hot", "quality", "reason") if hm.get(k) is not None}
        except Exception:
            d["hpa_monitor"] = None
        d["host_power"] = None if self.simulate else _ht.read_ina3221()
        # USB radio plugged in? An open radio is present by definition; None =
        # not checked (G40/X40 cards, network radios, simulate)
        d["hw_present"] = (True if be is not None else
                           None if self.simulate else sdr.usb_radio_present(self.kind))
        d.update(self._card_fields())
        if be is not None and hasattr(be, "_poll_tx_async"):     # UHD TX async reports
            d["tx_async"] = {"underflows": int(getattr(be, "tx_underflows", 0)),
                             "seq_errors": int(getattr(be, "tx_seq_errors", 0)),
                             "time_errors": int(getattr(be, "tx_time_errors", 0)),
                             "off": getattr(be, "_tx_async_err", None)}
        d["secure"] = self._secure_brief()                 # compact
        d["role"] = self._role
        d["role_by"] = self._role_by
        d["fpga"] = self._fpga_brief()                     # compact
        if self._port_map:                                 # roles only
            d["ports"] = _ports.compact(self._port_map)
            d["ports_v"] = _ports.version(self._port_map)
        if self._port_err:
            d["ports_err"] = self._port_err[:120]
        if self._port_by:
            d["ports_by"] = self._port_by[:64]
        if self.frontend is not None:
            try:
                fs = self.frontend.status()
                d["fe"] = {"kind": self._fe_cfg.frontend if self._fe_cfg else None,
                           "pa": bool((fs.get("pa") or {}).get("keyed")),
                           "tx": bool((fs.get("tr") or {}).get("tx")),
                           "drop": fs.get("last_drop")}
            except Exception:
                pass
        if self.atten is not None:
            d["atten_db"] = self._atten_db_last
        if self._atten_watch is not None:            # plug-in state for the views
            d["atten_state"] = self._atten_watch.status()["state"][:48]
        return d

    async def _cmd_ground_checkout(self, cid, p):
        """Automated ground checkout: RX noise floor, then (if armed) a TX to RX
        loopback + isolation measurement. armed=true radiates on the inited TX
        handle: the GUI gates this behind a specA/load confirm. Returns a
        pass/fail report the Tools tab renders."""
        if self._x40_tx_lock and bool(p.get("armed", False)):       # X40 same-side rule
            return CmdAck(cid, CmdStatus.REJECTED,
                          {"err": self._x40_tx_lock, "x40_same_side": True})
        from dpmg.dsp import tools_engine as _te
        if self.sched is None or self.backend is None:
            return CmdAck(cid, CmdStatus.REJECTED, {"err": "not initialized"})
        if self.backend.health.state != sdr.SDRDeviceState.STREAMING:
            self._rx_started_t = time.monotonic()
        await self._run(self.sched.start_rx)
        report = []

        # 1. RX noise floor (TX off)
        base = await self._grab_rx(float(p.get("seconds", 0.1)))
        if base is None:
            return CmdAck(cid, CmdStatus.FAILED, {"err": "RX starved (start_rx?)"})
        biq, fs, center = base
        floor0 = _te.noise_floor_db(biq, fs)
        report.append({"test": "rx_noise_floor", "pass": bool(np.isfinite(floor0)),
                       "floor_db": round(floor0, 1)})

        # 2. TX to RX loopback + isolation (armed only; radiates)
        if bool(p.get("armed", False)):
            off = float(p.get("tone_offset_hz", 100e3))
            self.sched.set_tx_waveform(_te.cw_tone(self._tx_fs() or fs, off, 0.2, 0.5),
                                       allow_live=True)
            self.sched.tx_mode = sdr.TXMode.CONTINUOUS
            if getattr(self.backend, "_chan_mode", "single") != "dual":
                try:
                    self.backend._skiq.set_rf_port_operation(self.backend._card, True)
                except Exception:
                    pass
            await self._run(self.sched.start_tx)
            await asyncio.sleep(0.2)
            live = await self._grab_rx(float(p.get("seconds", 0.1)))
            await self._run(self.sched.stop_tx)
            if getattr(self.backend, "_chan_mode", "single") != "dual":
                try:
                    self.backend._skiq.set_rf_port_operation(self.backend._card, False)
                except Exception:
                    pass
            if live is not None:
                liq, _, _ = live
                # look for the tone at the commanded offset, skipping the DC/LO
                # leakage spike a blind global-max would grab (recv_freq=0).
                fpk, ppk, snr = _te.peak_tone_near(liq, fs, off, search_hz=15e3)
                fdc, pdc, _ = _te.peak_tone_near(liq, fs, 0.0, search_hz=1e3,
                                                 dc_guard_hz=0.0)
                fl1 = _te.noise_floor_db(liq, fs)
                ferr = fpk - off
                report.append({"test": "tx_rx_loopback",
                               "pass": bool(snr > 20 and abs(ferr) < 5e3),
                               "recv_snr_db": round(snr, 1),
                               "freq_err_hz": round(ferr, 1),
                               "recv_freq_khz": round(fpk / 1e3, 1),
                               "dc_leak_db": round(pdc - fl1, 1)})
                report.append({"test": "tx_rx_isolation",
                               "pass": True,
                               "desense_db": round(fl1 - floor0, 1),
                               "note": "floor rise while TX keyed = leakage"})
            else:
                report.append({"test": "tx_rx_loopback", "pass": False,
                               "err": "RX starved during TX"})

        allpass = all(r.get("pass", False) for r in report)
        log.info("ground_checkout: %d tests, %s",
                 len(report), "PASS" if allpass else "FAIL")
        return CmdAck(cid, CmdStatus.OK, {"pass": allpass, "report": report})

    async def _cmd_channelizer_test(self, cid, p):
        """Read-IQ checkout: grab live RX IQ and run the energy detector on it,
        returning the signals found (freq, SNR) + noise floor. An offline-style
        channelizer probe pointed at the SDR to check what it hears."""
        from dpmg.dsp import tools_engine as _te
        grab = await self._grab_rx(float(p.get("seconds", 0.1)))
        if grab is None:
            return CmdAck(cid, CmdStatus.FAILED, {"err": "RX starved (start_rx?)"})
        iq, fs, center = grab
        sigs, floor = _te.detect_signals(
            iq, fs, thr_db=float(p.get("thr_db", 12.0)),
            min_sep_hz=float(p.get("min_sep_hz", 20_000.0)))
        for s in sigs:                                   # absolute RF freq too
            s["rf_hz"] = round(center + s["freq_hz"], 1)
        return CmdAck(cid, CmdStatus.OK,
                      {"n": len(sigs), "floor_db": floor, "fs": fs,
                       "center_hz": center, "signals": sigs,
                       "samples": int(len(iq))})

    async def _cmd_capture_iq(self, cid, p):
        """Grab a short window of live RX IQ and write it to a .c64 file on the
        radio, returning the path + fs + center. A bench scp's it back and runs
        offline measurements (e.g. the BER-vs-jamming sweep): same file-on-radio
        pattern as gen_test_waveform. `seconds` is bounded by _grab_rx (<=10 s)."""
        grab = await self._grab_rx(float(p.get("seconds", 0.1)))
        if grab is None:
            return CmdAck(cid, CmdStatus.FAILED, {"err": "RX starved (start_rx?)"})
        iq, fs, center = grab
        outdir = _data_dir("captures")
        base = _os.path.basename(str(p.get("name") or "capture"))
        path = _os.path.join(outdir, base + ".c64")
        np.asarray(iq, dtype=np.complex64).tofile(path)
        log.info("capture_iq: %d samp @ %.0f Hz -> %s", len(iq), fs, path)
        return CmdAck(cid, CmdStatus.OK,
                      {"path": path, "n": int(len(iq)), "fs": float(fs),
                       "center_hz": float(center)})

    # Record live RX on the radio, replay it on TX

    def _rec_dir(self):
        return _data_dir(_rec.REC_SUBDIR)

    def _rec_armed_name(self):
        src = self._tx_src or {}
        return src.get("name") if src.get("mode") == "record" else None

    def _rec_rate(self):
        be = self.backend
        return float(getattr(be, "_achieved_rate", be.config.sample_rate)) if be else 0.0

    def _tx_fs(self):
        """The TX card's sample rate. Cross-card TX may run its own
        rate; everywhere else TX shares the RX rate."""
        be = self.backend
        if be is None:
            return 0.0
        return float(getattr(be, "_tx_rate", None)
                     or getattr(be, "_achieved_rate", None) or be.config.sample_rate)

    def _rec_tx_lo(self):
        be = self.backend
        return float(getattr(be, "_tx_lo", None) or be.config.center_freq)

    async def _cmd_record_iq(self, cid, p):
        """Record live RX to <data>/recordings/<name>.c64 + .json sidecar. Params:
        seconds (capped by iq_record.max_seconds at the radio rate), band
        [f_lo_hz, f_hi_hz] absolute (omit for full band), and either name (the exact
        recording name, replaces one of that name) or label (added to a generated
        rec_<UTC> name). Keeps the newest REC_KEEP recordings within
        REC_MAX_TOTAL_BYTES. Does not radiate."""
        if self.sched is None or self.backend is None:
            return CmdAck(cid, CmdStatus.REJECTED, {"err": "not initialized"})
        if self._cap is not None or self._cap_lock.locked():
            return CmdAck(cid, CmdStatus.REJECTED, {"err": "capture in progress"})
        # An explicit name is used as is (replay/delete look it up by it)
        if p.get("name"):
            name = str(p["name"])
            if name.endswith(_rec.REC_EXT):
                name = name[:-len(_rec.REC_EXT)]
            try:
                _rec.rec_path(self._rec_dir(), name)
            except ValueError as e:
                return CmdAck(cid, CmdStatus.REJECTED, {"err": str(e)})
            if len(name) > 40:
                return CmdAck(cid, CmdStatus.REJECTED, {"err": "name longer than 40 characters"})
            if name == self._rec_armed_name() and self._tx_running():
                return CmdAck(cid, CmdStatus.REJECTED,
                              {"err": f"'{name}' is transmitting -- STOP first"})
        else:
            name = _rec.new_name(p.get("label"))
        fs = self._rec_rate()
        seconds = _rec.clamp_seconds(p.get("seconds", 1.0), fs)
        band = p.get("band")
        if band is not None:
            try:
                f_lo, f_hi = float(band[0]), float(band[1])
            except (TypeError, ValueError, IndexError):
                return CmdAck(cid, CmdStatus.REJECTED, {"err": "band must be [f_lo_hz, f_hi_hz]"})
            if not f_hi > f_lo:
                return CmdAck(cid, CmdStatus.REJECTED, {"err": "band: f_hi must exceed f_lo"})
        outdir = self._rec_dir()
        need = int(seconds * fs) * 8
        try:
            import shutil
            free = shutil.disk_usage(outdir).free
        except OSError:
            free = None
        if free is not None and free < need + _rec.REC_DISK_MARGIN:
            return CmdAck(cid, CmdStatus.REJECTED,
                          {"err": f"not enough disk on the radio ({free/1e9:.2f} GB free, "
                                  f"{need/1e6:.0f} MB needed + "
                                  f"{_rec.REC_DISK_MARGIN/1e9:.0f} GB margin)"})
        tx_live = self._tx_running()
        grab = await self._grab_rx(seconds)
        if grab is None:
            return CmdAck(cid, CmdStatus.FAILED, {"err": "RX starved (is the radio started?)"})
        iq, gfs, center = grab
        target = int(round(seconds * gfs))
        be = self.backend
        try:
            init = json.loads(self._init_sig) if self._init_sig else {}
        except ValueError:
            init = {}
        extra = {"mode": "full" if band is None else "band",
                 "band_lo_hz": None if band is None else min(f_lo, f_hi),
                 "band_hi_hz": None if band is None else max(f_lo, f_hi),
                 "requested_s": round(seconds, 3),
                 # kept for a "match received power" mode (needs calibration)
                 "rx_gain_index": float(be.config.rx_gain),
                 "rx_port": init.get("rx_antenna", be.config.rx_antenna),
                 "rx_card": (init.get("extras") or {}).get("card", 0),
                 "tx_atten_db": self._tx_atten_db,
                 "radio": self.serial, "tx_live_during": bool(tx_live)}

        def _do():
            x = np.asarray(iq[:target], dtype=np.complex64)
            if band is None:
                out, meta = x, _rec.full_meta(len(x), gfs, center)
            else:
                out, meta = _rec.band_record(x, gfs, center, f_lo, f_hi)
            side = _rec.write_recording(outdir, name, out, meta, extra)
            removed = _rec.prune(outdir, protect=(name, self._rec_armed_name()))
            return side, removed
        try:
            side, removed = await self._run(_do)
        except ValueError as e:
            return CmdAck(cid, CmdStatus.REJECTED, {"err": str(e)})
        except Exception as e:
            return CmdAck(cid, CmdStatus.FAILED, {"err": f"record: {e}"})
        warnings = []
        if tx_live:
            warnings.append("TX was keyed during the recording; it may hold the "
                            "radio's own transmission")
        if band is None:
            warnings.append("full band includes the radio's DC spur / LO leakage; "
                            "record a selected band to leave it out")
        if len(iq) < target * 0.95:
            warnings.append(f"short: {len(iq)/gfs:.3f} of {seconds:g} s captured")
        log.info("record_iq: %s %s %.3f s @ %.0f Hz -> %s (%d B), pruned %s",
                 side["mode"], name, side["seconds"], side["rate_hz"], outdir,
                 side["size_bytes"], removed)
        # plain top-level fields for scripts; "rec" is the compact list entry the GUI uses
        return CmdAck(cid, CmdStatus.OK,
                      {"name": name, "path": _rec.rec_path(outdir, name),
                       "mode": side["mode"], "n": side["n"], "rate_hz": side["rate_hz"],
                       "seconds": round(side["seconds"], 4),
                       "size_bytes": side["size_bytes"], "peak_dbfs": side["peak_dbfs"],
                       "rms_dbfs": side["rms_dbfs"],
                       "band_center_hz": side.get("band_center_hz"),
                       "rec": _rec.brief(side), "removed": removed[:6],
                       "max_s": _rec.max_seconds(fs), "warnings": warnings[:3]})

    async def _cmd_list_recordings(self, cid, p):
        """Recordings on the radio, newest first (compact, one ack), plus the
        record cap at the current radio rate."""
        recs = await self._run(_rec.list_recordings, self._rec_dir())
        fs = self._rec_rate()
        out = {"max_s": _rec.max_seconds(fs) if fs else _rec.REC_MAX_S,
               "rate": fs or None, "keep": _rec.REC_KEEP,
               "max_total": _rec.REC_MAX_TOTAL_BYTES,
               "total": int(sum(int(m.get("size_bytes") or 0) for m in recs)),
               "armed": self._rec_armed_name(), "recs": []}
        for m in recs:
            out["recs"].append(_rec.brief(m))
            if len(json.dumps(out)) > 1100:            # keep the ack under one datagram
                out["recs"].pop()
                out["more"] = True
                break
        return CmdAck(cid, CmdStatus.OK, out)

    async def _cmd_delete_recording(self, cid, p):
        name = str(p.get("name") or "")
        if name and name == self._rec_armed_name() and self._tx_running():
            return CmdAck(cid, CmdStatus.REJECTED,
                          {"err": "that recording is transmitting -- STOP first"})
        try:
            ok = await self._run(_rec.delete_recording, self._rec_dir(), name)
        except ValueError as e:
            return CmdAck(cid, CmdStatus.REJECTED, {"err": str(e)})
        if not ok:
            return CmdAck(cid, CmdStatus.REJECTED, {"err": f"no recording '{name}'"})
        return CmdAck(cid, CmdStatus.OK, {"deleted": name})

    def _rec_build(self, name, gain_db):
        """Load a recording and rebuild it as a TX buffer at the radio rate and
        TX LO, gain applied (capped). Blocking; runs in the hw pool."""
        x, meta = _rec.load_recording(self._rec_dir(), name)
        wave, info = _rec.rebuild_for_tx(x, meta, self._tx_fs(), self._rec_tx_lo())
        if info["n"] > _rec.REC_MAX_SAMPLES * 1.01:
            raise ValueError(f"replay at {self._tx_fs()/1e6:g} MSps would be "
                             f"{info['n']/1e6:.1f} M samples (limit "
                             f"{_rec.REC_MAX_SAMPLES/1e6:.1f} M); lower the radio "
                             "rate or record a shorter stretch")
        wave, g = _rec.apply_gain(wave, gain_db)
        info.update(g)
        info["peak_rec_dbfs"] = meta.get("peak_dbfs")
        return wave, info

    async def _cmd_replay_recording(self, cid, p):
        """Load a recording as the TX waveform (does not radiate: start_tx does,
        mode SINGLE_SHOT = once, CONTINUOUS = loop). Params: name, gain_db
        (relative to as captured, capped at DAC full scale), level_mode
        "relative" (default) | "match_rx" (refused: needs calibration)."""
        if self.sched is None or self.backend is None:
            return CmdAck(cid, CmdStatus.REJECTED, {"err": "not initialized"})
        mode = str(p.get("level_mode") or "relative")
        if mode == "match_rx":
            gm = _rec.match_rx_gain_db({}, None)
            if gm is None:
                return CmdAck(cid, CmdStatus.REJECTED,
                              {"err": "match received power needs measured TX output "
                                      "power and RX calibration; not available yet",
                               "needs_cal": True})
        elif mode != "relative":
            return CmdAck(cid, CmdStatus.REJECTED, {"err": f"bad level_mode {mode}"})
        if self._tx_running():
            return CmdAck(cid, CmdStatus.REJECTED,
                          {"err": "TX is active -- STOP transmit before loading a new waveform"})
        name = str(p.get("name") or "")
        gain_db = p.get("gain_db", 0.0)
        try:
            wave, info = await self._run(self._rec_build, name, gain_db)
        except (ValueError, FileNotFoundError) as e:
            return CmdAck(cid, CmdStatus.REJECTED, {"err": str(e)})
        except Exception as e:
            return CmdAck(cid, CmdStatus.FAILED, {"err": f"replay: {e}"})
        try:
            self.sched.set_tx_waveform(wave)
        except sdr.WaveformError as e:
            return CmdAck(cid, CmdStatus.REJECTED, {"err": str(e)})
        # rate None: the buffer is always rebuilt at the radio rate (also on re-arm)
        self._tx_src = {"mode": "record", "name": name, "gain_db": info["gain_db"],
                        "rate": None}
        self._tx_wf_desc = (f"recording {name} ({info['seconds']:.3f} s, "
                            f"{info['gain_db']:+.1f} dB)")
        log.info("replay_recording: %s armed, %d samp, gain %+.1f dB (req %+.1f, max %+.1f), "
                 "peak %.1f dBFS, offset %.0f Hz", name, info["n"], info["gain_db"],
                 info["gain_req_db"], info["gain_max_db"], info["peak_dbfs"],
                 info["offset_hz"])
        res = {k: info[k] for k in ("n", "offset_hz", "resampled", "gain_req_db", "gain_db",
                                    "gain_max_db", "capped", "peak_dbfs", "rms_dbfs",
                                    "peak_rec_dbfs")}
        res["seconds"] = round(info["seconds"], 4)
        res["warnings"] = info["warnings"][:3]
        res["tx_lo_hz"] = self._rec_tx_lo()
        return CmdAck(cid, CmdStatus.OK, res)

    async def _cmd_classify_selection(self, cid, p):
        """Blind modulation classification of a highlighted span. Grabs live RX
        IQ, frequency-shifts the selection to DC, decimates to ~4x its bandwidth,
        and runs the feature-based classifier (amc.py). f_lo/f_hi are absolute RF
        Hz (the GUI brush axis); seconds is the capture length."""
        from dpmg.dsp import amc
        grab = await self._grab_rx(float(p.get("seconds", 0.2)))
        if grab is None:
            return CmdAck(cid, CmdStatus.FAILED, {"err": "RX starved (start_rx?)"})
        iq, fs, center = grab
        f_lo = float(p.get("f_lo", center - fs / 4))
        f_hi = float(p.get("f_hi", center + fs / 4))
        if f_hi < f_lo:
            f_lo, f_hi = f_hi, f_lo
        sel_c = 0.5 * (f_lo + f_hi)
        sel_bw = max(f_hi - f_lo, fs * 1e-3)       # floor so a click still works

        def _do():
            import numpy as _np
            import scipy.signal as _ss
            x = _np.asarray(iq, dtype=_np.complex64)
            n = _np.arange(x.size)
            x = x * _np.exp(-2j * _np.pi * (sel_c - center) / fs * n)  # selection: DC
            cur = float(fs)
            target = max(sel_bw * 2.5, 1.0)        # fill the band: good for cumulants and chirp occupancy
            while cur / target >= 2.0:
                q = int(min(8, cur // target))
                if q < 2:
                    break
                x = _ss.decimate(x, q, ftype="fir", zero_phase=True)
                cur /= q
            return x, cur

        try:
            x, fs2 = await self._run(_do)
            # The whole-factor decimation can leave the capture up to
            # ~5x the box wide; with the box width amc keeps a neighbouring
            # channel outside the box out of the classification
            res = await self._run(amc.classify, x, fs2, sel_bw)
        except Exception as e:
            return CmdAck(cid, CmdStatus.FAILED, {"err": f"classify: {e}"})
        # round the feature floats so the result fits a datagram comfortably
        feats = {k: (round(v, 4) if isinstance(v, float) else v)
                 for k, v in res.get("features", {}).items()}
        return CmdAck(cid, CmdStatus.OK, {
            "modulation": res["modulation"], "family": res["family"],
            "confidence": res["confidence"], "sym_rate_hz": res["sym_rate_hz"],
            "rf_center_hz": round(sel_c, 1), "sel_bw_hz": round(sel_bw, 1),
            "capture_rate_hz": round(float(fs2), 1), "n": feats.get("n", 0),
            "features": feats})

    async def _cmd_survey_classify(self, cid, p):
        """Auto-AMC: detect every signal in the live RX and classify each. Energy-
        detects peaks, estimates each one's -10 dB bandwidth, extracts it (shift to
        DC + decimate to ~2.5x its bandwidth) and classifies it. Returns a list of
        {rf_hz, snr_db, bw_hz, modulation, family, confidence}."""
        from dpmg.dsp import amc
        from dpmg.dsp import tools_engine as _te
        grab = await self._grab_rx(float(p.get("seconds", 0.3)))
        if grab is None:
            return CmdAck(cid, CmdStatus.FAILED, {"err": "RX starved (start_rx?)"})
        iq, fs, center = grab
        thr = float(p.get("thr_db", 12.0))
        min_sep = float(p.get("min_sep_hz", 200_000.0))
        max_n = int(p.get("max_sigs", 8))
        dc_guard = float(p.get("dc_guard_hz", 150_000.0))   # skip the LO/DC spur

        def _do():
            import numpy as _np
            import scipy.signal as _ss
            # cap the working buffer: time-domain decimation per signal is O(N), so
            # a few M samples keeps the survey fast even at 40-61 MSps (plenty of
            # symbols for detection + classification)
            work = iq[:3_000_000] if iq.size > 3_000_000 else iq
            sigs, floor = _te.detect_signals(work, fs, thr_db=thr, min_sep_hz=min_sep,
                                             max_sigs=max_n + 6)
            f, psd = _te.psd_db(work, fs)
            # cheap pass: estimate each detection's bandwidth, drop narrow DC spurs
            cand = []
            for s in sigs:
                foff = s["freq_hz"]
                ki = int(_np.argmin(_np.abs(f - foff)))
                thr_bw = psd[ki] - 10.0
                lo = ki
                while lo > 0 and psd[lo] > thr_bw:
                    lo -= 1
                hi = ki
                while hi < len(psd) - 1 and psd[hi] > thr_bw:
                    hi += 1
                bw = max(float(f[hi] - f[lo]), fs * 2e-3)
                if abs(foff) < dc_guard and bw < 40e3:   # narrow LO/DC spur
                    continue
                cand.append({"foff": foff, "snr_db": s["snr_db"], "bw": bw})
            # suppress sideband shoulders before the expensive classify: a detection
            # much weaker than a nearby stronger one is that signal's spectral skirt
            cand.sort(key=lambda d: d["snr_db"], reverse=True)
            keep = []
            for d in cand:
                if any(abs(d["foff"] - k["foff"]) < max(2.0e6, 2.5 * k["bw"])
                       and d["snr_db"] < k["snr_db"] - 12.0 for k in keep):
                    continue
                keep.append(d)
            keep = keep[:max_n]
            out = []
            for d in keep:
                foff, bw = d["foff"], d["bw"]
                x = work * _np.exp(-2j * _np.pi * foff / fs * _np.arange(work.size))
                cur = float(fs)
                target = max(bw * 2.5, 1.0)
                while cur / target >= 2.0 and x.size > 512:
                    q = int(min(8, cur // target))
                    if q < 2:
                        break
                    x = _ss.decimate(x, q, ftype="fir", zero_phase=True)
                    cur /= q
                res = amc.classify(x, cur)
                out.append({"rf_hz": round(center + foff, 1),
                            "offset_hz": round(foff, 1), "snr_db": d["snr_db"],
                            "bw_hz": round(bw, 1), "modulation": res["modulation"],
                            "family": res["family"], "confidence": res["confidence"]})
            out.sort(key=lambda d: d["offset_hz"])
            return out, floor

        try:
            out, floor = await self._run(_do)
        except Exception as e:
            return CmdAck(cid, CmdStatus.FAILED, {"err": f"survey: {e}"})
        return CmdAck(cid, CmdStatus.OK, {"n": len(out), "floor_db": floor,
                                         "center_hz": center, "signals": out})

    async def _cmd_transponder(self, cid, p):
        """Detect, ring, respond, time it. Watches RX for a signal above
        trigger_snr_db, buffers each detection into a ring, builds a response
        waveform from the JSON `response` spec, and (emulated) 'transmits' it:
        instrumenting the detect/gen/total latency. emulate=true does not radiate
        (loopback the response over the wire once the 2nd RF cable is in). Real TX
        keys the loaded response when emulate=false and armed."""
        if self._x40_tx_lock and bool(p.get("armed", False)) and not bool(p.get("emulate", True)):       # X40 same-side rule
            return CmdAck(cid, CmdStatus.REJECTED,
                          {"err": self._x40_tx_lock, "x40_same_side": True})
        from dpmg.dsp import tools_engine as _te
        from collections import deque
        if self.sched is None or self.backend is None:
            return CmdAck(cid, CmdStatus.REJECTED, {"err": "not initialized"})
        if self.backend.health.state != sdr.SDRDeviceState.STREAMING:
            self._rx_started_t = time.monotonic()
        await self._run(self.sched.start_rx)
        seconds = float(p.get("seconds", 5.0))
        dwell = float(p.get("dwell", 0.05))
        thr = float(p.get("trigger_snr_db", 15.0))
        emulate = bool(p.get("emulate", True))
        armed = bool(p.get("armed", False))
        resp = dict(p.get("response") or {})
        ring = deque(maxlen=int(p.get("ring", 16)))      # buffer detections
        events = []

        # Pre-render the response waveform once from the JSON spec.
        rwave = None
        rdesc = "none"
        try:
            fs0 = self._tx_fs()
            rwave, rdesc = self._build_response_wave(resp, fs0)
        except Exception as e:
            return CmdAck(cid, CmdStatus.REJECTED,
                          {"err": f"response spec: {e}"})

        log.info("transponder: start (%.1fs, thr=%.0f, resp=%s, emulate=%s)",
                 seconds, thr, rdesc, emulate)
        t_end = time.monotonic() + seconds
        _iters = 0
        while time.monotonic() < t_end and len(events) < int(p.get("max_events", 50)):
            _iters += 1
            if _iters > 2000:
                break
            grab = await self._grab_rx(dwell)
            if grab is None:
                await asyncio.sleep(0.02)
                continue
            iq, fs, center = grab
            t_have = time.monotonic()                    # RX data in hand
            sigs, _floor = _te.detect_signals(iq, fs, thr_db=thr,
                                              min_sep_hz=float(p.get("min_sep_hz", 20e3)))
            if not sigs:
                continue
            top = max(sigs, key=lambda s: s["snr_db"])
            t_det = time.monotonic()                     # detection done
            ring.append({"t": t_det, "freq_hz": top["freq_hz"], "iq_len": len(iq)})
            # Emulated (or real if armed+not-emulate). Response is prebuilt.
            t_txstart = time.monotonic()
            if not emulate and armed and rwave is not None:
                self.sched.set_tx_waveform(rwave, allow_live=True)
                self.sched.tx_mode = sdr.TXMode.SINGLE_SHOT
                await self._run(self.sched.start_tx)
            t_tx = time.monotonic()                      # response emitted
            events.append({
                "detect_khz": round(top["freq_hz"] / 1e3, 1),
                "detect_rf_hz": round(center + top["freq_hz"], 1),
                "snr_db": top["snr_db"],
                "detect_ms": round((t_det - t_have) * 1000, 2),   # detection compute
                "resp_ms": round((t_tx - t_txstart) * 1000, 2),   # response emit
                "proc_latency_ms": round((t_tx - t_have) * 1000, 2),  # data to response
                "total_latency_ms": round((t_tx - (t_have - dwell)) * 1000, 2),  # +buffering
                "emulated": emulate})
            await asyncio.sleep(float(p.get("debounce", 0.1)))
        if not emulate and armed:
            await self._run(self.sched.stop_tx)

        lat = [e["proc_latency_ms"] for e in events]
        tot = [e["total_latency_ms"] for e in events]
        stats = {}
        if lat:
            stats = {"events": len(events),
                     "proc_latency_ms": {"min": round(min(lat), 2), "mean": round(sum(lat) / len(lat), 2),
                                         "max": round(max(lat), 2)},
                     "total_latency_ms": {"min": round(min(tot), 2), "mean": round(sum(tot) / len(tot), 2),
                                          "max": round(max(tot), 2)},
                     "note": "total includes the %.0f ms capture dwell (buffering)" % (dwell * 1000)}
        log.info("transponder: %d events, response=%s, emulate=%s", len(events), rdesc, emulate)
        # the ack is one 1400 byte datagram: a busy band (the G40 at 30.72 MSps
        # sees dozens of detections in a 30 s dwell) makes the full event list
        # 8 kB; the ack would never leave the radio and the step reads as not acked.
        # The ack carries the stats and the first few events; the whole list
        # goes to a file next to the waveforms.
        events_path = None
        if len(events) > 3:
            try:
                _os.makedirs("data", exist_ok=True)
                events_path = _os.path.join("data", f"transponder_{int(time.time())}.json")
                with open(events_path, "w") as fh:
                    json.dump({"events": events, "stats": stats, "response": rdesc,
                               "emulated": emulate}, fh)
            except Exception as e:          # the ack still goes out without the file
                log.warning("transponder: event file not written: %s", e)
                events_path = None
        return CmdAck(cid, CmdStatus.OK,
                      {"events": events[:3], "events_total": len(events),
                       "events_file": events_path, "stats": stats, "response": rdesc,
                       "emulated": emulate, "ring_depth": len(ring)})

    def _build_response_wave(self, resp: dict, fs: float):
        """Build a response TX waveform (complex64 @ fs) from a JSON response spec:
        {message, modulation, symbol_rate, tone_spacing_hz, offset_hz, ...}. Reuses
        the same modem/scenario path as the JSON scenario generator."""
        from dpmg.scenario import gen_scenario_json as _g
        sig = dict(resp)
        sig.setdefault("modulation", "FSK")
        sig.setdefault("symbol_rate", 4800)
        sig.setdefault("message", "ACK")
        iq, _entry = _g.build_signal(sig, fs)
        desc = (f"{sig['modulation']} '{sig['message']}' "
                f"{sig['symbol_rate']}bd @ {float(sig.get('offset_hz', 0))/1e3:+.0f}kHz "
                f"({len(iq)} samp)")
        return np.asarray(iq, dtype=np.complex64), desc

    async def _cmd_close(self, cid):
        await self._teardown()
        # a decode plan belongs to the session that set it; kept, a later start_rx
        # with no new plan would decode against it
        self.plan = None
        self._plan_anchor = None
        return CmdAck(cid, CmdStatus.OK, {})

    # wedge watchdog: automatic recovery from a wedged RX FIFO

    async def _wedge_loop(self):
        """Detection alone (rx_degraded: detector paused + FLAG_RX_STALE) leaves
        the radio stuck until someone re-inits it. Escalate instead:
          stage 1  stale for WEDGE_STREAM_RESTART_S: restart the card RX stream
          stage 2  stale again WEDGE_REINIT_AFTER_S later: full re-init with the
                   last init params, restart RX, resume TX if it was on
          stage 3  stale WEDGE_GIVEUP_AFTER_S after the re-init: stop trying and
                   log that the radio needs a power-cycle
        Short replays that clear by themselves are left alone. An episode ends
        after WEDGE_CLEAR_S with no replay."""
        since = last_bad = act_t = None
        stage = 0
        while True:
            await asyncio.sleep(WEDGE_POLL_S)
            if not WEDGE_WATCHDOG or getattr(self, "_init_busy", False):
                continue
            now = time.time()
            sc = self.sched
            bad = sc is not None and sc.rx_degraded()
            if bad:
                last_bad = now
                if since is None:
                    since, stage, act_t = now, 0, now
                    log.warning("[WEDGE] RX replaying stale buffers -- watching")
            elif since is not None and now - last_bad >= WEDGE_CLEAR_S:
                dur = last_bad - since
                self.wedge_stats["episodes"] += 1
                if stage == 0:
                    self.wedge_stats["self_cleared"] += 1
                self.wedge_stats["last"] = {"t": since, "dur_s": round(dur, 2),
                                            "stage": stage}
                log.warning("[WEDGE] cleared (stale %.1f s, recovery stage %d)", dur, stage)
                since = None
                stage = 0
                continue
            if not bad:
                continue
            if stage == 0 and now - since >= WEDGE_STREAM_RESTART_S:
                stage = 1
                await self._wedge_restart_stream()
                act_t = time.time()
            elif stage == 1 and now - act_t >= WEDGE_REINIT_AFTER_S:
                stage = 2
                await self._wedge_reinit()
                act_t = time.time()
            elif stage == 2 and now - act_t >= WEDGE_GIVEUP_AFTER_S:
                stage = 3
                self.wedge_stats["giveups"] += 1
                log.error("[WEDGE] still stale after RX stream restart and re-init "
                          "-- power-cycle the radio")

    async def _wedge_restart_stream(self):
        sc = self.sched
        if sc is None:
            return
        log.warning("[WEDGE] stage 1: restarting the card RX stream")
        try:
            ok = await self._run(sc.restart_rx_stream)
        except Exception:
            log.exception("[WEDGE] RX stream restart failed")
            ok = False
        if ok:
            self.wedge_stats["stream_restarts"] += 1
        else:
            log.warning("[WEDGE] RX stream restart not available -- will re-init")

    async def _wedge_reinit(self):
        sig = self._init_sig
        sc = self.sched
        if not sig or sc is None:
            return
        rx_on = sc._rx is not None and sc._rx.is_alive()
        tx_on = sc._tx is not None and sc._tx.is_alive()
        tx_mode = sc.tx_mode.name
        log.warning("[WEDGE] stage 2: re-init with the last init params "
                    "(RX %s, TX %s)", "on" if rx_on else "off", "on" if tx_on else "off")
        self._init_busy = True
        try:
            ack = await self._do_init(0, json.loads(sig), sig)
        except Exception:
            log.exception("[WEDGE] re-init failed")
            return
        finally:
            self._init_busy = False
        if ack.status != CmdStatus.OK:
            log.error("[WEDGE] re-init failed: %s", ack.result)
            return
        self.wedge_stats["reinits"] += 1
        if rx_on:
            await self._cmd_start_rx(0)
        if tx_on:
            a = await self._cmd_start_tx(0, {"mode": tx_mode})
            log.warning("[WEDGE] TX resumed after re-init: %s", a.status.name)

    async def _cmd_selftest_rx_wedge(self, cid, p):
        """BIT: make the RX look wedged for `hold` s so the watchdog can be
        exercised. survives_stream=true keeps it wedged through the stage-1
        stream restart, forcing the stage-2 re-init. No RF effect."""
        sc = self.sched
        if sc is None or sc._rx is None or not sc._rx.is_alive():
            return CmdAck(cid, CmdStatus.REJECTED, {"err": "RX not streaming"})
        hold = float(p.get("hold", 8.0))
        sc.inject_rx_replay(hold, bool(p.get("survives_stream", False)))
        log.warning("[WEDGE] selftest: simulating a wedged RX FIFO for %.1f s", hold)
        return CmdAck(cid, CmdStatus.OK, {"hold": hold,
                                         "survives_stream": bool(p.get("survives_stream", False))})

    async def _teardown(self):
        link, self._bitpipe, self._bitpipe_prev_hook = self._bitpipe, None, None
        if link is not None:                          # stream modem goes with the radio
            try:
                await self._run(link.stop)
            except Exception:
                log.exception("bitpipe stop during teardown")
        await self._run(self._chan_stop)
        async with self._det_lock:
            self._flush_bursts()
            self.det = None
            self.tracker = None
        if self.sched is not None:
            try:
                await self._run(self.sched.stop)
            except Exception:
                log.exception("scheduler stop during teardown")
            self.sched = None
        if self.backend is not None:
            # detach first, so the pollers (card temperatures, accelerometer,
            # GPSDO lock) stop issuing reads while the cards are being released
            be, self.backend = self.backend, None
            try:
                await self._run(be.close)
            except Exception:
                log.exception("backend close during teardown")
        self.psd = None
        self._init_sig = None
        # reset the detection plane: a closed radio has no live detections, so
        # STATUS should report det_index 0 and keep nothing to gap-fill:
        # otherwise a previous radio session's detections backfill into the GUI and
        # appear on an UNINIT radio
        async with self._det_lock:
            self.det_head = 0
            self.det_ring.clear()
        self._clip_ewma = 0.0
        self._rx_overload = False
        self._overload_steps = 0

    # detection plane

    def _make_detector(self):
        """(Re)build the slot grid for the current achieved rate + center.
        Caller holds _det_lock."""
        cfg = self.backend.config
        # A backend whose band edges are only radio noise (the B200 above
        # 30.72 MSps) says how much of the band is clean; the slot grid stays
        # inside it so those edges are not read as hundreds of signals.
        grid = {}
        usable = getattr(self.backend, "usable_bw_hz", None)
        bw = float(usable()) if callable(usable) else 0.0
        if 0 < bw < float(cfg.sample_rate):
            grid = {"grid_start_hz": cfg.center_freq - bw / 2.0,
                    "grid_stop_hz": cfg.center_freq + bw / 2.0}
        self.det = ed.EnergyDetector(
            fs=cfg.sample_rate, center_hz=cfg.center_freq, **grid,
            slot_bw=self._det_params.get("slot_bw", DET_DEFAULT_SLOT_BW),
            threshold_db=self._det_params.get("threshold_db",
                                              DET_DEFAULT_THRESHOLD_DB),
            nfft=self._det_params.get("nfft", DET_DEFAULT_NFFT))
        self.tracker = ed.BurstTracker(
            cluster_margin_db=self._det_params.get("cluster_margin_db",
                                                   DET_DEFAULT_CLUSTER_MARGIN_DB),
            cluster_gap=self._det_params.get("cluster_gap", DET_DEFAULT_CLUSTER_GAP),
            hold_ticks=self._det_params.get("hold_ticks", DET_DEFAULT_HOLD_TICKS),
            island_slots=self._det_params.get("island_slots", DET_DEFAULT_ISLAND_SLOTS))
        self._det_last_ts = None
        self._det_max_ts = -1              # new session: hardware ts counter restarts

    def _emit_burst(self, ev):
        """Map a closed BurstEvent onto the wire and fan out. snr_db is peak
        slot power over the detector's median floor: relative, not a calibrated
        absolute SNR. bw_hz is the emitter's clustered slot span (one slot
        for a clean tone; wider when its skirt or a touching emitter was
        folded in: see energy_detector.cluster_detections)."""
        floor = self.det._floor_db if (self.det and
                                       self.det._floor_db is not None) else None
        snr = (ev.peak_power_db - floor) if floor is not None else 0.0
        det = WireDetection(
            det_index=self.det_head,
            t_unix=ev.t_start,
            # measured PSD peak (energy_detector slot-boundary fix), not the
            # 20 kHz slot centre
            freq_hz=float(getattr(ev, "freq_hz", None) or ev.center_hz),
            bw_hz=(self._det_params.get("slot_bw", DET_DEFAULT_SLOT_BW)
                   * max(1, int(getattr(ev, "n_slots", 1)))),
            snr_db=float(snr),
            duration_ms=float((ev.t_stop - ev.t_start) * 1e3),
            flags=0)
        self.det_ring[self.det_head] = det
        self.det_head += 1
        self.det_ring.pop(self.det_head - DET_RING_MAX, None)   # bound ring
        fp = getattr(self, "_det_log_fp", None)
        if fp is not None:
            try:
                fp.write(f"{_utc_text(det.t_unix)},{det.t_unix:.3f},{det.freq_hz:.0f},{det.bw_hz:.0f},"
                         f"{det.snr_db:.1f},{det.duration_ms:.0f}\n")
                fp.flush()
            except Exception:
                pass
        for addr in list(self.gui_addrs):
            self.ep.send(det, addr)

    def _flush_bursts(self):
        """Close every open burst now (end of capture / retune moves the
        grid). Caller holds _det_lock."""
        if self.tracker is None:
            return
        closed = self.tracker.flush(time.time())     # held bursts too
        for ev in closed:
            self._emit_burst(ev)

    async def _det_loop(self):
        """Detection tick: run the energy detector over the newest RX frame
        (dedup by rf_timestamp), track rising/falling edges, emit closed
        bursts. FFT work runs in the hw thread pool."""
        while True:
            await asyncio.sleep(DET_INTERVAL_S)
            if self.sched is None or self.det is None:
                continue
            frame = self.sched.latest()
            if frame is None or len(frame.samples) == 0:
                continue
            # RX stream health: when the card wedges and replays old buffers, the
            # timestamp bounces backward and the PSD freezes. Those frames are
            # stale/duplicate: don't run the detector on them (it would emit
            # detections off data that isn't actually new).
            if self.sched.rx_degraded():
                if not self._det_suspended:
                    log.warning("detector suspended: RX stream degraded "
                                "(replaying stale buffers) -- no new data")
                    self._det_suspended = True
                continue
            if self._det_suspended:
                log.info("detector resumed: RX stream recovered")
                self._det_suspended = False
            ts = getattr(frame, "rf_timestamp", None)
            # skip repeats (same ts) and out-of-order replays (ts <= newest seen);
            # a bare "== last tick" check misses the latter when the hardware
            # timestamp oscillates between two values during a wedge.
            if ts is not None and ts <= self._det_max_ts:
                continue
            if ts is not None:
                self._det_max_ts = ts
            self._det_last_ts = ts
            async with self._det_lock:
                if self.det is None or self.tracker is None:
                    continue
                try:
                    closed, clip_frac = await self._run(self._det_step, frame)
                except Exception:
                    log.exception("detector step failed")
                    continue
                for ev in closed:
                    self._emit_burst(ev)
            await self._handle_overload(clip_frac)

    def _det_step(self, frame):
        """Blocking half of the detection tick (thread pool). Also measures the
        fraction of RX samples near ADC full-scale, for overload protection."""
        s = frame.samples
        c = s
        if float(getattr(frame, "sample_rate", 0.0) or 0.0) > HIGH_RATE_HZ:
            c = s[::CLIP_STRIDE_HIGH_RATE]
            if 0 < DET_MAX_SAMPLES_HIGH_RATE < len(s):
                s = s[-DET_MAX_SAMPLES_HIGH_RATE:]
        clip_frac = float(((np.abs(c.real) >= CLIP_LEVEL) |
                           (np.abs(c.imag) >= CLIP_LEVEL)).mean()) if len(c) else 0.0
        dets = self.det.process(s)
        return self.tracker.update(dets, time.time()), clip_frac

    async def _handle_overload(self, clip_frac):
        """Track RX clipping; on sustained overload surface a STATUS flag and
        (if enabled) step the RX gain down so the front end stops clipping."""
        if (time.monotonic() - getattr(self, "_rx_started_t", 0.0)) < OVERLOAD_GRACE_S:
            self._clip_ewma = 0.0            # start-up transient: not an overload
            return
        self._clip_ewma = 0.6 * self._clip_ewma + 0.4 * clip_frac
        was = self._rx_overload
        self._rx_overload = self._clip_ewma > CLIP_FRAC_OVERLOAD
        if self._rx_overload and not was:
            log.warning("RX OVERLOAD: ~%.2f%% of samples at ADC full-scale",
                        self._clip_ewma * 100.0)
        elif was and not self._rx_overload:
            self._overload_steps = 0          # episode cleared; allow future back-off
        if self._rx_overload and self._auto_gain_backoff \
                and self._overload_steps < OVERLOAD_MAX_STEPS \
                and (time.monotonic() - self._last_backoff_t) > OVERLOAD_BACKOFF_COOLDOWN_S:
            self._last_backoff_t = time.monotonic()
            await self._backoff_gain()

    async def _backoff_gain(self):
        """Lower the RX gain one step (lower index = less gain) to clear an
        overload. set_gain_live clamps to the driver's range and updates
        config.rx_gain, so reading that back tells whether the gain moved
        (and stops the steps at the minimum)."""
        gi = float(self.backend.config.rx_gain)
        await self._run(self.backend.set_gain_live, gi - OVERLOAD_GAIN_STEP)
        now = float(self.backend.config.rx_gain)
        if now < gi:
            self._overload_steps += 1
            log.warning("auto gain back-off: RX gain index %d -> %d (overload)",
                        int(gi), int(now))
        else:
            # already at minimum gain and still clipping: stop stepping
            self._overload_steps = OVERLOAD_MAX_STEPS
            log.warning("RX overload persists at minimum gain (index %d) -- "
                        "raise TX attenuation / add pad", int(now))

    # outbound telemetry

    def _rx_running(self) -> bool:
        """True while the scheduler's RX drain thread (the one that feeds
        frames/fps/PSD/detection) is alive."""
        s = self.sched
        return bool(s is not None and getattr(s, "_rx", None) is not None
                    and s._rx.is_alive())

    def _radio_state(self) -> int:
        if self.backend is None:
            return int(RadioState.UNINIT)
        rs = _STATE_MAP.get(self.backend.health.state, RadioState.UNINIT)
        # STREAMING on the wire means the RX drain thread is
        # running. backend.health.state is a card-level flag: EpiqBackend.open()
        # starts the card's RX and reports STREAMING before any Start RX, and the
        # scheduler leaves it STREAMING after stop_rx while a TX thread is alive;
        # both would read "STREAMING / frames 0 / fps 0" in the GUI.
        # MockBackend.open() reports IDLE, so the simulator does not show
        # this. TX is reported on its own via FLAG_TX_ACTIVE.
        if rs == RadioState.STREAMING and not self._rx_running():
            rs = RadioState.INIT_OK
        return int(rs)

    def _status(self) -> Status:
        rs = self._radio_state()
        flags = 0
        if rs == int(RadioState.STREAMING):
            flags |= Status.FLAG_TASK_RUNNING
        sc = self.sched
        if sc is not None and getattr(sc, "_tx", None) is not None \
                and sc._tx.is_alive():
            flags |= Status.FLAG_TX_ACTIVE
        if self._ref_locked:                 # GPSDO disciplined + locked
            flags |= Status.FLAG_REF_LOCKED
        if self._rx_overload:                 # RX ADC clipping (front-end hot)
            flags |= Status.FLAG_RX_OVERLOAD
        if sc is not None and sc.rx_degraded():  # wedged FIFO: PSD frozen, det paused
            flags |= Status.FLAG_RX_STALE
        _sb = self._secure_brief()
        if _sb.get("active"):
            flags |= Status.FLAG_SECURE_ON
        if _sb.get("fault"):
            flags |= Status.FLAG_SECURE_FAULT
        # No GPS position accessor exists in sdr_core yet, so lat/lon stay zero
        # and FLAG_POS_VALID stays unset. Temperature is the host's junction
        # temperature, read at most every 5 s.
        sched, be = self.sched, self.backend
        return Status(
            serial=self.serial,
            host_type=self.host_type,
            radio_state=rs,
            flags=flags,
            temp_c_x10=self._temp_x10(),
            uptime_s=int(time.monotonic() - self.t0),
            last_cmd_id=self.last_cmd_id,
            det_index=self.det_head,
            # v2 stream health: scheduler drain metrics + card xruns
            rx_frames=sched.metrics.frames if sched else 0,
            fps=int(sched.metrics.fps) if sched else 0,
            overruns=be.health.overruns if be else 0,
            underruns=be.health.underruns if be else 0,
            rx_gain_idx=int(be.config.rx_gain) if be else 0,
            center_hz=float(be.config.center_freq) if be else 0.0,
            sample_rate_hz=float(getattr(be, "_achieved_rate",
                                         be.config.sample_rate)) if be else 0.0)

    def _temp_x10(self) -> int:
        """Junction temperature for Status.temp_c_x10, re-read from
        /sys/class/thermal at most every 5 s."""
        now = time.monotonic()
        if now - getattr(self, "_tj_t", -1e9) >= 5.0:
            self._tj_t = now
            try:
                self._tj_x10 = _ht.to_wire(_ht.junction_c(_ht.read_zones()))
            except Exception:
                self._tj_x10 = _ht.NO_READING
        return self._tj_x10

    def _imu_tracker(self):
        t = getattr(self, "_imu", None)
        if t is None:
            t = self._imu = _imu.ImuTracker()
        return t

    async def _imu_loop(self):
        """Sample the RX card's accelerometer every SAMPLE_S while the
        radio is started (read in the hw pool, like the GPSDO lock). Stopped
        radio: the tracker forgets its samples and level reference."""
        tr = self._imu_tracker()
        while True:
            await asyncio.sleep(_imu.SAMPLE_S)
            if self.backend is None:
                if tr.last is not None:
                    tr.reset()
                continue
            try:
                tr.add(await self._run(self.backend.accel))
            except Exception:
                pass

    def _baro_tracker(self):
        t = getattr(self, "_baro", None)
        if t is None:
            t = self._baro = _baro.BaroTracker()
        return t

    async def _baro_loop(self):
        """Read the host barometer every POLL_S whatever the radio is doing (it
        is on the host I2C bus, not on a card).  Simulate: a fixed reading."""
        tr = self._baro_tracker()
        loop = asyncio.get_running_loop()
        while True:
            if self.simulate:
                tr.set_sim()
            else:
                try:
                    await loop.run_in_executor(None, tr.poll)
                except Exception as e:
                    tr.err, tr.state = str(e)[:80], "error"
            await asyncio.sleep(_baro.POLL_S)

    def _cmd_imu_set_level(self, cid, p):
        """Take the current card orientation as level."""
        tr = self._imu_tracker()
        if tr.last is None:
            return CmdAck(cid, CmdStatus.REJECTED,
                          {"err": "no accelerometer reading (radio not started)"})
        tr.set_level()
        return CmdAck(cid, CmdStatus.OK, {"imu": tr.summary()})

    async def _ref_loop(self):
        """Cache the GPSDO lock every 5 s (read in the hw pool so it never races
        the streaming thread). _status() reflects it as FLAG_REF_LOCKED."""
        while True:
            await asyncio.sleep(5.0)
            if self.backend is None:
                self._ref_locked = None
                self._card_temps = {}
                continue
            try:                      # Radio card temperatures, same 5 s cadence
                now = time.monotonic()
                got = await self._run(self.backend.card_temps)
                kept = getattr(self, "_card_temps", {})
                if not hasattr(self, "_card_last"):
                    self._card_last = {}        # card: (deg C, wall time), kept after close
                for c, t in (got or {}).items():
                    kept[int(c)] = (int(t), now)
                    self._card_last[int(c)] = (int(t), time.time())
                names = getattr(self.backend, "card_names", None)
                if got and names:
                    self._card_names_last = dict(names)
                # keep a reading through a few -EAGAIN polls, drop it after 30 s
                self._card_temps = {c: v for c, v in kept.items() if now - v[1] <= 30.0}
            except Exception:
                pass
            try:
                # ref_locked knows the clock source (external 10 MHz counts as
                # locked when a reference is measured); older backends only
                # have the GPSDO read
                _rl = getattr(self.backend, "ref_locked", None) or self.backend.gpsdo_locked
                self._ref_locked = await self._run(_rl)
                if self._ref_locked is None and self._gps_sim is not None:  # simulated GPSDO
                    _sim = self._sim_ref_status()
                    self._ref_locked = (_sim["gpsdo_locked"] or _sim.get("ext_ref_present")
                                        or (False if _sim.get("ten_mhz") == "LOST" else None))
            except Exception:
                self._ref_locked = None

    async def _clock_loop(self):
        """G40: every 2 s read the last-1PPS timestamp and keep the clock dict
        (card clock against the 1 PPS, 10 MHz in use or LOST). On an external
        10 MHz the reference flag follows it here, so a lost 10 MHz shows in the
        STATUS heartbeat within a few seconds and not only at the 5 s poll."""
        while True:
            await asyncio.sleep(2.0)
            be = self.backend
            meas = getattr(be, "clock_measure", None)
            if be is None or meas is None:
                self._clock = None
                continue
            try:
                self._clock = await self._run(meas)
                # an X40 gives no 10 MHz verdict (None): its flag stays with ref_locked
                if (self._clock or {}).get("ref") == "external" and self._clock.get("ten_mhz") is not None:
                    v = self._clock.get("ten_mhz")
                    self._ref_locked = True if v == "in use" else False if v == "LOST" else None
            except Exception:
                self._clock = None

    async def _status_loop(self):
        while True:
            await asyncio.sleep(1.0)
            if not self.gui_addrs:
                continue
            st = self._status()
            fp = getattr(self, "_telem_log_fp", None)
            if fp is not None:
                try:
                    pv = 1 if (st.flags & Status.FLAG_POS_VALID) else 0
                    tx = 1 if (st.flags & Status.FLAG_TX_ACTIVE) else 0
                    rl = 1 if (st.flags & Status.FLAG_REF_LOCKED) else 0
                    t = time.time()
                    ck = getattr(self, "_clock", None) or {}
                    ppm = ck.get("clock_ppm")
                    fp.write(f"{_utc_text(t)},{t:.1f},{st.radio_state},"
                             f"{st.lat_1e7/1e7:.6f},{st.lon_1e7/1e7:.6f},{st.alt_m:.1f},"
                             f"{pv},{st.overruns},{st.underruns},{st.fps},"
                             f"{st.uptime_s},{tx},{rl},"
                             f"{'' if ppm is None else f'{ppm:.3f}'},{ck.get('ten_mhz') or ''}\n")
                    fp.flush()
                except Exception:
                    pass
            for addr in list(self.gui_addrs):
                self.ep.send(st, addr)

    async def _psd_loop(self):
        """PSD_FRAME telemetry at PSD_INTERVAL_S (4 Hz). Fire-and-forget,
        last-value-wins: each frame is a complete snapshot, so a lost datagram
        costs nothing but staleness. FFT work runs in the hw thread pool."""
        while True:
            await asyncio.sleep(PSD_INTERVAL_S)
            if not self.gui_addrs or self.sched is None or self.psd is None:
                continue
            frame = self.sched.latest()
            if frame is None or len(frame.samples) == 0:
                continue
            # latest() keeps returning the last frame after Stop RX; re-sending
            # it would make a stopped radio's PSD look live (never stale)
            fkey = (id(self.sched), getattr(frame, "seq", None), getattr(frame, "rf_timestamp", None))
            if fkey == self._psd_last_seq:
                continue
            self._psd_last_seq = fkey
            try:
                wire = await self._run(_build_psd_frame, self.psd, frame,
                                       PSD_MAX_BINS)
            except Exception:
                log.exception("psd frame build failed")
                continue
            if wire is None:
                continue
            for addr in list(self.gui_addrs):
                self.ep.send(wire, addr)

    async def _watch_loop(self):
        """v3 WATCH_UPDATE telemetry: stream the channelizer's per-band decode
        summary (name/freq/mod/present/decoded/message/counts) to the GUI at
        2 Hz. Only runs while a plan is loaded; fire-and-forget, last-value-wins."""
        while True:
            await asyncio.sleep(0.5)
            if not self.gui_addrs or self.chan is None:
                continue
            try:
                rows = self.chan.band_status()
            except Exception:
                continue
            if not rows:
                continue
            bands = [{
                "n": r.get("name", ""),
                "f": float(r.get("f_center_hz", 0.0)),
                "m": r.get("modulation", ""),
                "p": bool(r.get("present", False)),
                "d": bool(r.get("decoded", False)),
                "msg": str(r.get("message", ""))[:48],
                "dn": int(r.get("decoded_n", 0)),
                "tn": int(r.get("detected_n", 0)),
            } for r in rows]
            # send the full set in as many datagrams as it takes; the GUI
            # merges the pages by band name, so no band is dropped.
            for page in WatchUpdate.paginate(self.serial, bands):
                for addr in list(self.gui_addrs):
                    self.ep.send(page, addr)

    async def _announce_loop(self):
        ann = Announce(serial=self.serial, host_type=self.host_type,
                       svc_version=SVC_VERSION, cmd_port=self.cmd_port)
        target = (self.broadcast, self.discovery_port)
        while True:
            self.ep.send(ann, target)
            await asyncio.sleep(2.0)


def main(argv=None):
    ap = argparse.ArgumentParser(description="DPMG headless radio service")
    ap.add_argument("--serial", required=True,
                    help="radio serial = permanent identity (never the IP)")
    ap.add_argument("--kind", default="mock",
                    choices=["g40", "x40", "b200", "pluto", "xsdr", "soapy", "hackrf", "limesdr", "mock"])
    ap.add_argument("--host-type", default="g40",
                    choices=["g40", "x40", "mock"])
    ap.add_argument("--simulate", action="store_true",
                    help="use MockBackend (no radio hardware)")
    ap.add_argument("--cmd-port", type=int, default=47810)
    ap.add_argument("--discovery-port", type=int, default=47811)
    ap.add_argument("--broadcast", default="255.255.255.255",
                    help="ANNOUNCE target (use 127.0.0.1 for local testing)")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--role", default=None, choices=list(_role.ORDER),
                    help="radio role to start with when none is stored yet "
                         "(the stored role, set from the GUI, wins)")
    # The pin map lives in rf_frontend.yaml; these only override it.
    fe = ap.add_argument_group("RF front-end (external PA / TX-RX switch / attenuator)")
    fe.add_argument("--frontend-config", default=None, metavar="PATH",
                    help="rf_frontend.yaml path (default: next to this file, or $DPMG_FRONTEND_CONFIG)")
    fe.add_argument("--frontend", default=None, choices=list(_rf.FRONTEND_KINDS),
                    help="override 'frontend:' (none | gpio | mock)")
    fe.add_argument("--pa-line", default=None, help="override pa_enable.line (name, pad or offset)")
    fe.add_argument("--tr-line", default=None, help="override tr_switch.line")
    fe.add_argument("--tr-mode", default=None, choices=list(_rf.TR_MODES),
                    help="override tr_switch.mode (proxy = NV100 TR_N drives the switch)")
    fe.add_argument("--pa-fault-line", default=None,
                    help="override pa_fault_in.line (also enables the fault input)")
    fe.add_argument("--atten", default=None, metavar="SPEC",
                    help="override attenuator: none | auto | mock | hidraw[:/dev/hidrawN] | http://ip")
    args = ap.parse_args(argv)
    fe_over = {"frontend": args.frontend, "pa_enable.line": args.pa_line,
               "tr_switch.line": args.tr_line, "tr_switch.mode": args.tr_mode,
               "pa_fault_in.line": args.pa_fault_line,
               "pa_fault_in.enabled": (True if args.pa_fault_line else None)}
    if args.atten:
        a = args.atten
        if a.startswith("hidraw"):
            fe_over["atten.transport"] = "hidraw"
            if ":" in a:
                fe_over["atten.path"] = a.split(":", 1)[1]
        elif a.startswith("http"):
            fe_over["atten.transport"] = "http"; fe_over["atten.url"] = a
        elif a == "auto":
            fe_over["atten.transport"] = "auto"; fe_over["atten.path"] = "auto"
        else:
            fe_over["atten.transport"] = a

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    svc = RadioService(
        serial=args.serial,
        host_type=_HOST_TYPE[args.host_type],
        kind=args.kind,
        simulate=args.simulate,
        cmd_port=args.cmd_port,
        discovery_port=args.discovery_port,
        broadcast=args.broadcast,
        frontend_config=args.frontend_config,
        frontend_overrides=fe_over,
        role=args.role)
    async def _run_svc():
        # SIGTERM-graceful: docker stop/restart must reach _teardown so the
        # card is released via skiq exit: a hard kill leaves a stale claim
        # in /dev/shm/skiq_card_mgr* and the next init fails rc=-16 (busy).
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:
                pass
        await svc.serve(stop)

    try:
        asyncio.run(_run_svc())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
