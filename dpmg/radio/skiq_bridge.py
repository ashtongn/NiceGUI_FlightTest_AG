"""ctypes bridge to libsidekiq (Epiq Sidekiq X40 and G40/NV100).

The Epiq SDK is pure C; there is no Python binding. Loads libsidekiq.so
through ctypes and exposes the subset of skiq_* functions the
DPMG app needs: init/exit, LO/sample-rate/gain config, IQ order, RX/TX streaming,
and the receive path (skiq_rx_block_t). The same bridge runs on both radios; the
.so is discovered at runtime, so it works against 4.21 (X40 host) and 4.19.1
(G40 host) without a rebuild.

Validated on the X40 (libsidekiq 4.21, rev C):
  - Handles: rx A1=0 A2=1 B1=2 B2=3 C1=4 D1=5 ; tx A1=0 A2=1 (X40: 1 TX)
  - skiq_receive(card, *p_hdl, **pp_block, *p_data_len): status (0 = success)
  - skiq_rx_block_t header = 6 words / 24 bytes:
        rf_timestamp u64, sys_timestamp u64, 1 packed metadata word, user_meta u32
    then int16 data[] with Q0=data[0], I0=data[1] (QI order)
  - RX gain is an index (skiq_write_rx_gain); TX is attenuation in 0.25 dB steps
  - Library default IQ order is QI; skiq_write_iq_order_mode can select IQ.

G40/NV100 (libsidekiq 4.19.1) shares the skiq_* API above; it is QI-native like
the X40. Card/handle counts and gain ranges differ from the X40 (two NV100 cards,
NV100 RX gain index and TX attenuation ranges per the NV100 docs; check the
ranges there before relying on them).

Only the C library is required at runtime (libsidekiq.so on LD_LIBRARY_PATH).
"""
from __future__ import annotations

import ctypes
import os
import numpy as np
from typing import Optional, Tuple

# enums (from v4.21 headers)
XPORT_TYPE_AUTO = 0
XPORT_INIT_LEVEL_FULL = 1

RX_HDL = {"RXA1": 0, "RXA2": 1, "RXB1": 2, "RXB2": 3, "RXC1": 4, "RXD1": 5}
TX_HDL = {"TXA1": 0, "TXA2": 1, "TXB1": 2, "TXB2": 3}  # X40 J6 = TXB1 (skiq_tx_hdl_b1)

# these match sidekiq_types.h (they were once inverted here): do not flip
# enum skiq_iq_order_t { skiq_iq_order_qi = 0, skiq_iq_order_iq = 1 }
IQ_ORDER_QI = 0     # skiq_iq_order_qi  (Q0=data[0], I0=data[1]):  library/FPGA default
IQ_ORDER_IQ = 1     # skiq_iq_order_iq  (I0=data[0], Q0=data[1])

RX_STATUS_SUCCESS = 0
RX_STATUS_NO_DATA = -1

RX_HEADER_WORDS = 6          # SKIQ_RX_HEADER_SIZE_IN_WORDS
RX_HEADER_BYTES = RX_HEADER_WORDS * 4   # 24 bytes
TX_HEADER_WORDS = 4          # SKIQ_TX_HEADER_SIZE_IN_WORDS
TX_HEADER_BYTES = TX_HEADER_WORDS * 4   # 16 bytes

TX_FLOW_IMMEDIATE = 0        # skiq_tx_immediate_data_flow_mode
TX_TRANSFER_SYNC = 0         # skiq_tx_transfer_mode_sync
TX_TRANSFER_ASYNC = 1        # skiq_tx_transfer_mode_async
TX_QUEUE_FULL = 100          # SKIQ_TX_ASYNC_SEND_QUEUE_FULL
TX_ASYNC_THREADS = 2         # libsidekiq send threads (min 2, max 10)
TX_ASYNC_PRIORITY = 15       # RT priority for those threads
TX_ASYNC_QUEUE_PKTS = 50     # libsidekiq async send queue depth (packets)
# In async mode skiq_transmit queues a pointer into the prepared buffer and
# returns; the send threads read it later. A replaced buffer must stay alive
# until every packet that points into it has gone out, so the last replaced
# buffers are kept until newer sends cover this many blocks (twice the queue).
TX_RETAIN_BLOCKS = 2 * TX_ASYNC_QUEUE_PKTS
TX_FULL_SCALE = 32767.0      # 16-bit full scale; X40 packs its 14-bit DAC into 16 bits (validated via skiq_read_rx_iq_resolution)
TX_BLOCK_SAMPLES = 8188      # single: 8188 + 4 header = 8192 words (mult of 256)
TX_BLOCK_SAMPLES_DUAL = 6142  # dual: 2*6142 + 4 header = 12288 words (mult of 256,
                              # and <= FPGA FIFO 16384 - 2k = 14336, the dual TX cap).
                              # Bigger blocks mean fewer Python loop iterations a
                              # second and fewer FIFO underruns than 1020-sample ones.


def tx_block_for_rate(rate_hz, dual=False):
    """Pick the TX block size for a sample rate. The async send queue is a fixed
    50 packets, so the host tolerance before a FIFO underrun is 50*block/rate.
    At a few MSps 8188 samples is plenty (~70 ms) and stays small so a short
    transponder response isn't zero-padded into latency; at tens of MSps that
    window shrinks (6.7 ms at 61.44), so step up to a bigger block: same queue
    buffers 2-4x longer and the send loop makes 2-4x fewer skiq_transmit calls.
    Sizes keep the words-per-block (step*(2 if dual else 1) + header) a multiple
    of 256. For dual the packet is 2*step+4 words and the card caps a dual-mode
    Tx packet at FIFO-2k = 16384-2048 = 14336 words (enforced from libsidekiq
    4.23.0; older libs let an oversized write slide), so every dual step must
    satisfy 2*step+4 <= 14336: step <= 7166."""
    # DPMG_TX_BLOCK=<samples> overrides the pick (single mode only;
    # keep samples+4 a multiple of 256): for the TX-induced RX wedge tests
    _ov = _os.environ.get("DPMG_TX_BLOCK", "").strip()
    if _ov and not dual:
        return int(_ov)
    single = (8188, 16380, 32764)     # 256*{32,64,128} - 4 words
    dual_s = (6142, 7166, 7166)       # 2*step+4 = 12288 / 14336 words (<= 14336 cap)
    sizes = dual_s if dual else single
    if rate_hz >= 50e6:
        return sizes[2]
    if rate_hz >= 20e6:
        return sizes[1]
    return sizes[0]

import glob as _glob
import os as _os
# Discover libsidekiq at runtime so the same bridge works across versions
# (X40 host has 4.21.0; G40 host has 4.19.1). Prefer an explicit unversioned
# symlink, then any versioned .so.* found, then the bare soname.
def _discover_lib_paths():
    paths = ["/usr/lib/epiq/libsidekiq.so"]
    # versioned libs, newest-sorted last so the highest version is tried first
    vers = sorted(_glob.glob("/usr/lib/epiq/libsidekiq.so.*"), reverse=True)
    paths.extend(vers)
    paths.append("libsidekiq.so")
    return paths
_LIB_PATHS = _discover_lib_paths()


class SkiqError(RuntimeError):
    pass


# skiq_rx_block_t header layout (the 6-word / 24-byte metadata)
# rf_timestamp(u64) + sys_timestamp(u64) + 1 packed word(u32) + user_meta(u32)
class _RxBlockHeader(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("rf_timestamp", ctypes.c_uint64),
        ("sys_timestamp", ctypes.c_uint64),
        ("meta_word", ctypes.c_uint32),     # hdl/overload/rfic_control/id/system_meta/version packed
        ("user_meta", ctypes.c_uint32),
    ]


# Rxdrain.c (librxdrain.so, see rxdrain_candidates) loops
# skiq_receive in C, one ctypes call per batch, so the drain does not need
# one Python iteration (and one GIL hand-off) per 1018-sample block; the
# header of rxdrain.c has the details. DPMG_RX_NATIVE=0 keeps the Python path;
# DPMG_RXDRAIN_LIB overrides the library path.
RXDRAIN_ABI = 1


class _RxdState(ctypes.Structure):
    """rxd_state_t in rxdrain.c (field order and types must match)."""
    _fields_ = [
        ("last_ts", ctypes.c_uint64), ("last_n", ctypes.c_uint64),
        ("ts0", ctypes.c_uint64), ("n_floats", ctypes.c_uint64),
        ("n_blocks", ctypes.c_uint32), ("n_mismatch", ctypes.c_uint32),
        ("n_fwd", ctypes.c_uint32), ("n_back", ctypes.c_uint32),
        ("first_exp", ctypes.c_uint64), ("first_got", ctypes.c_uint64),
        ("empty", ctypes.c_int32), ("full", ctypes.c_int32),
    ]


def rxdrain_candidates():
    """Where librxdrain.so is looked for, in order: DPMG_RXDRAIN_LIB alone if
    set, else native/lib/, the repo root, then beside this file."""
    env = _os.environ.get("DPMG_RXDRAIN_LIB")
    if env:
        return [env]
    from dpmg.paths import native_lib_candidates
    return native_lib_candidates("librxdrain.so", __file__)


def load_rxdrain(path=None):
    """(lib, why): the native drain library, or (None, reason) to use the
    Python path."""
    if _os.environ.get("DPMG_RX_NATIVE", "1").strip() == "0":
        return None, "DPMG_RX_NATIVE=0"
    if not path:
        cands = rxdrain_candidates()
        path = next((p for p in cands if _os.path.exists(p)), None)
        if path is None:
            return None, "librxdrain.so not found (" + ", ".join(cands) + ")"
    if not _os.path.exists(path):
        return None, f"{path} not found"
    try:
        lib = ctypes.CDLL(path)
        lib.rxd_abi.restype = ctypes.c_int32
        if lib.rxd_abi() != RXDRAIN_ABI:
            return None, f"{path}: ABI {lib.rxd_abi()} != {RXDRAIN_ABI}"
        f = lib.skiq_drain_batch
        f.restype = ctypes.c_int32
        f.argtypes = [ctypes.c_void_p, ctypes.c_uint8, ctypes.c_void_p, ctypes.c_uint64,
                      ctypes.c_uint32, ctypes.c_double, ctypes.c_int32,
                      ctypes.POINTER(_RxdState)]
    except (OSError, AttributeError) as e:
        return None, f"{path}: {e}"
    return lib, path


def _sig(fn, restype, argtypes):
    """Set a ctypes signature; a plain Python stand-in (tests) has no such slots."""
    try:
        fn.restype = restype
        fn.argtypes = argtypes
    except (AttributeError, TypeError):
        pass


class SkiqBridge:
    """Thin ctypes wrapper around libsidekiq. One instance per process."""

    def __init__(self):
        self.lib = self._load()
        self._declare()
        self._inited_card: Optional[int] = None

    # library load / prototypes
    def _load(self):
        last = None
        for p in _LIB_PATHS:
            try:
                return ctypes.CDLL(p)
            except OSError as e:
                last = e
        raise SkiqError(f"Could not load libsidekiq.so ({last}). "
                        f"Set LD_LIBRARY_PATH=/usr/lib/epiq")

    def _declare(self):
        L = self.lib
        c = ctypes
        L.skiq_init.restype = c.c_int32
        L.skiq_init.argtypes = [c.c_int, c.c_int, c.POINTER(c.c_uint8), c.c_uint8]
        L.skiq_exit.restype = c.c_int32

        L.skiq_read_serial_string.restype = c.c_int32
        L.skiq_read_serial_string.argtypes = [c.c_uint8, c.POINTER(c.c_char_p)]

        L.skiq_write_rx_LO_freq.restype = c.c_int32
        L.skiq_write_rx_LO_freq.argtypes = [c.c_uint8, c.c_int, c.c_uint64]
        L.skiq_write_tx_LO_freq.restype = c.c_int32
        L.skiq_write_tx_LO_freq.argtypes = [c.c_uint8, c.c_int, c.c_uint64]

        L.skiq_write_rx_sample_rate_and_bandwidth.restype = c.c_int32
        L.skiq_write_rx_sample_rate_and_bandwidth.argtypes = [
            c.c_uint8, c.c_int, c.c_uint32, c.c_uint32]
        L.skiq_write_tx_sample_rate_and_bandwidth.restype = c.c_int32
        L.skiq_write_tx_sample_rate_and_bandwidth.argtypes = [
            c.c_uint8, c.c_int, c.c_uint32, c.c_uint32]

        L.skiq_write_rx_gain.restype = c.c_int32
        L.skiq_write_rx_gain.argtypes = [c.c_uint8, c.c_int, c.c_uint8]
        L.skiq_read_rx_gain_index_range.restype = c.c_int32
        L.skiq_read_rx_gain_index_range.argtypes = [
            c.c_uint8, c.c_int, c.POINTER(c.c_uint8), c.POINTER(c.c_uint8)]
        # cal offset (dB) for a gain index at an LO freq: real RX gain in dB
        L.skiq_read_rx_cal_offset_by_LO_freq_and_gain_index.restype = c.c_int32
        L.skiq_read_rx_cal_offset_by_LO_freq_and_gain_index.argtypes = [
            c.c_uint8, c.c_int, c.c_uint64, c.c_uint8, c.POINTER(c.c_double)]
        # RX front-end calibration: DC-offset correction (kills the LO/DC spur)
        # and run_rx_cal (DC + IQ-imbalance, reduces the mirror image). Both are
        # simple (card, hdl[, bool]): no pointers, declared only if present.
        for _nm, _at in (("skiq_write_rx_dc_offset_corr", [c.c_uint8, c.c_int, c.c_bool]),
                         ("skiq_run_rx_cal", [c.c_uint8, c.c_int])):
            _fn = getattr(L, _nm, None)
            if _fn is not None:
                _fn.restype = c.c_int32
                _fn.argtypes = _at

        L.skiq_write_tx_attenuation.restype = c.c_int32
        L.skiq_write_tx_attenuation.argtypes = [c.c_uint8, c.c_int, c.c_uint16]

        L.skiq_write_iq_order_mode.restype = c.c_int32
        L.skiq_write_iq_order_mode.argtypes = [c.c_uint8, c.c_int]

        L.skiq_start_rx_streaming.restype = c.c_int32
        L.skiq_start_rx_streaming.argtypes = [c.c_uint8, c.c_int]
        L.skiq_stop_rx_streaming.restype = c.c_int32
        L.skiq_stop_rx_streaming.argtypes = [c.c_uint8, c.c_int]
        L.skiq_start_tx_streaming.restype = c.c_int32
        L.skiq_start_tx_streaming.argtypes = [c.c_uint8, c.c_int]
        L.skiq_stop_tx_streaming.restype = c.c_int32
        L.skiq_stop_tx_streaming.argtypes = [c.c_uint8, c.c_int]

        # skiq_rx_status_t skiq_receive(uint8_t, skiq_rx_hdl_t*, skiq_rx_block_t**, uint32_t*)
        L.skiq_receive.restype = c.c_int32
        L.skiq_receive.argtypes = [
            c.c_uint8, c.POINTER(c.c_int), c.POINTER(c.c_void_p), c.POINTER(c.c_uint32)]

        # read back the achieved sample rate (card may override the requested rate)
        L.skiq_read_rx_sample_rate.restype = c.c_int32
        L.skiq_read_rx_sample_rate.argtypes = [
            c.c_uint8, c.c_int, c.POINTER(c.c_uint32), c.POINTER(c.c_double)]
        # min/max achievable RX rate for this handle+bitstream (logged as
        # [RATE_RANGE]). A min well below the 50 Msps base profile means the FPGA
        # decimator build is present (X40); min equal to base means no decimation.
        _rr = getattr(L, "skiq_read_rx_sample_rate_range", None)
        if _rr is not None:
            _rr.restype = c.c_int32
            _rr.argtypes = [c.c_uint8, c.c_int,
                            c.POINTER(c.c_uint32), c.POINTER(c.c_uint32)]

        # TX path
        L.skiq_write_tx_data_flow_mode.restype = c.c_int32
        L.skiq_write_tx_data_flow_mode.argtypes = [c.c_uint8, c.c_int, c.c_int]
        L.skiq_write_tx_transfer_mode.restype = c.c_int32
        L.skiq_write_tx_transfer_mode.argtypes = [c.c_uint8, c.c_int, c.c_int]
        try:  # async-mode config (present in 4.19+; guard for older libs)
            L.skiq_write_num_tx_threads.restype = c.c_int32
            L.skiq_write_num_tx_threads.argtypes = [c.c_uint8, c.c_uint8]
            L.skiq_write_tx_thread_priority.restype = c.c_int32
            L.skiq_write_tx_thread_priority.argtypes = [c.c_uint8, c.c_int32]
        except AttributeError:
            pass
        L.skiq_write_tx_block_size.restype = c.c_int32
        L.skiq_write_tx_block_size.argtypes = [c.c_uint8, c.c_int, c.c_uint32]
        # RX block size: libsidekiq has no skiq_write_rx_block_size (4.19 to 4.25,
        # only skiq_read_rx_block_size); the block is 1024 words = 1018 samples.
        # skiq_transmit(card, hdl, skiq_tx_block_t*, p_user): pass the bridge's own buffer
        L.skiq_transmit.restype = c.c_int32
        L.skiq_transmit.argtypes = [c.c_uint8, c.c_int, c.c_void_p, c.c_void_p]

    # lifecycle
    def init(self, card: int = 0) -> None:
        # defensively release any prior lock held by this process (libsidekiq is
        # process-global; a second skiq_init without skiq_exit returns EBUSY)
        try:
            self.lib.skiq_exit()
        except Exception:
            pass
        arr = (ctypes.c_uint8 * 1)(card)
        rc = self.lib.skiq_init(XPORT_TYPE_AUTO, XPORT_INIT_LEVEL_FULL, arr, 1)
        if rc == -16:
            # EBUSY: a claim was stranded (a crashed process, or a failed teardown).
            # skiq_exit above only frees this process's claim, so clear the stale
            # card-manager shm and retry once. Safe here because dpmg_svc is the sole
            # card owner (the GPU channelizer never skiq_inits: it reads shm baseband).
            self._clear_stale_claim()
            rc = self.lib.skiq_init(XPORT_TYPE_AUTO, XPORT_INIT_LEVEL_FULL, arr, 1)
        if rc != 0:
            raise SkiqError(f"skiq_init(card={card}) failed rc={rc} "
                            f"(card attached? driver loaded? permissions?)")
        self._inited_card = card

    def init_cards(self, cards) -> None:
        """Claim several cards in one skiq_init call: cross-card FDD (RX on one
        NV100, TX on the other) needs both cards up, and libsidekiq wants them
        all in a single init."""
        cards = [int(c) for c in cards]
        try:
            self.lib.skiq_exit()
        except Exception:
            pass
        n = len(cards)
        arr = (ctypes.c_uint8 * n)(*cards)
        rc = self.lib.skiq_init(XPORT_TYPE_AUTO, XPORT_INIT_LEVEL_FULL, arr, n)
        if rc == -16:
            self._clear_stale_claim()
            rc = self.lib.skiq_init(XPORT_TYPE_AUTO, XPORT_INIT_LEVEL_FULL, arr, n)
        if rc != 0:
            raise SkiqError(f"skiq_init(cards={cards}) failed rc={rc}")
        self._inited_card = cards[0]      # exit() only needs a non-None marker

    @staticmethod
    def _clear_stale_claim():
        import glob, os
        for f in glob.glob("/dev/shm/skiq_card_mgr*"):
            try:
                os.remove(f)
            except OSError:
                pass

    def exit(self) -> None:
        if self._inited_card is not None:
            self.lib.skiq_exit()
            self._inited_card = None

    def serial(self, card: int = 0) -> str:
        s = ctypes.c_char_p()
        rc = self.lib.skiq_read_serial_string(card, ctypes.byref(s))
        return s.value.decode() if rc == 0 and s.value else "?"

    # configuration 
    def set_rx_lo(self, card, hdl, hz):
        self._chk(self.lib.skiq_write_rx_LO_freq(card, hdl, int(hz)), "write_rx_LO_freq")

    def set_tx_lo(self, card, hdl, hz):
        self._chk(self.lib.skiq_write_tx_LO_freq(card, hdl, int(hz)), "write_tx_LO_freq")

    def read_lo(self, card, rx_hdl, tx_hdl) -> dict:
        # Signature is (card, hdl, uint64_t *p_freq, double *p_actual_freq):
        # Both pointers are required; omitting the 4th arg writes through a
        # garbage pointer and corrupts the process (stranded the card lock once).
        out = {}
        for name, fn, hdl in (("rx", "skiq_read_rx_LO_freq", rx_hdl),
                              ("tx", "skiq_read_tx_LO_freq", tx_hdl)):
            f = getattr(self.lib, fn, None)
            if f is None:
                out[name] = "n/a"
                continue
            try:
                f.restype = ctypes.c_int32
                f.argtypes = [ctypes.c_uint8, ctypes.c_int,
                              ctypes.POINTER(ctypes.c_uint64),
                              ctypes.POINTER(ctypes.c_double)]
                v = ctypes.c_uint64(); act = ctypes.c_double()
                rc = f(card, hdl, ctypes.byref(v), ctypes.byref(act))
                out[name] = int(v.value) if rc == 0 else f"rc={rc}"
                out[name + "_actual"] = round(act.value, 1)
            except Exception as e:
                out[name] = f"err={e}"
        return out

    def set_rx_rate_bw(self, card, hdl, rate, bw):
        self._chk(self.lib.skiq_write_rx_sample_rate_and_bandwidth(card, hdl, int(rate), int(bw)),
                  "write_rx_sample_rate_and_bandwidth")

    RF_PORT_CONFIG_FIXED = 0
    RF_PORT_CONFIG_TDD = 1
    RF_PORT_CONFIG_TRX = 2

    def set_rf_port_config(self, card, config: int) -> None:
        # The NV100 dual-use TRx connectors only switch between Rx and Tx in TRx mode; in
        # fixed mode the direction is locked (and rf_port_operation is a no-op),
        # so TxA2 never reaches J2. SW dev manual §5.4.7.
        fn = getattr(self.lib, "skiq_write_rf_port_config", None)
        if fn is None:
            raise SkiqError("no skiq_write_rf_port_config in this libsidekiq")
        self._chk(fn(card, int(config)), "write_rf_port_config")

    def set_rf_port_operation(self, card, transmit: bool) -> None:
        # NV100 TRx ports are card-level TDD: skiq_write_rf_port_operation(card,
        # true) points every TRx port at the transmitter, false at the receiver
        # (SW dev manual §5.4.7, tdd_rx_tx_samples.c). Without this the J2/TRx2A
        # port stays in RX and TxA2 never reaches the connector.
        fn = getattr(self.lib, "skiq_write_rf_port_operation", None)
        if fn is None:
            raise SkiqError("no skiq_write_rf_port_operation in this libsidekiq")
        self._chk(fn(card, bool(transmit)), "write_rf_port_operation")

    def rf_port_diag(self, card, rx_hdl, tx_hdl) -> dict:
        # Read the actual RF-port routing to see whether TxA2 is on J2
        # and whether the port config is fixed vs trx (operation switching only
        # applies in trx/tdd). Best-effort; missing symbols are skipped.
        out = {}
        strfn = getattr(self.lib, "skiq_rf_port_string", None)
        if strfn is not None:
            strfn.restype = ctypes.c_char_p

        def _name(v):
            if strfn is not None:
                try:
                    return strfn(v).decode()
                except Exception:
                    pass
            return v

        try:
            fx = ctypes.c_bool(); tx = ctypes.c_bool()
            self.lib.skiq_read_rf_port_config_avail(card, ctypes.byref(fx), ctypes.byref(tx))
            out["avail"] = {"fixed": fx.value, "trx": tx.value}
        except Exception as e:
            out["avail_err"] = str(e)
        try:
            cfg = ctypes.c_int()
            self.lib.skiq_read_rf_port_config(card, ctypes.byref(cfg))
            out["config"] = {0: "fixed", 1: "tdd", 2: "trx"}.get(cfg.value, cfg.value)
        except Exception as e:
            out["config_err"] = str(e)
        try:
            p = ctypes.c_int()
            self.lib.skiq_read_rx_rf_port_for_hdl(card, rx_hdl, ctypes.byref(p))
            out["rx_port"] = _name(p.value)
        except Exception as e:
            out["rx_port_err"] = str(e)
        try:
            p = ctypes.c_int()
            self.lib.skiq_read_tx_rf_port_for_hdl(card, tx_hdl, ctypes.byref(p))
            out["tx_port"] = _name(p.value)
        except Exception as e:
            out["tx_port_err"] = str(e)
        # The other receive handles that the NV100 maps to J2 (RxA2,
        # RxB1). In fixed mode a port is RX or TX, never both, so a receiver
        # holding J2 on the TX card is what to look for when J2 stays silent.
        for nm, h in (("rxA2_port", RX_HDL["RXA2"]), ("rxB1_port", RX_HDL["RXB1"])):
            try:
                p = ctypes.c_int()
                rc = self.lib.skiq_read_rx_rf_port_for_hdl(card, h, ctypes.byref(p))
                out[nm] = _name(p.value) if rc == 0 else f"rc={rc}"
            except Exception as e:
                out[nm + "_err"] = str(e)
        try:
            out["tx_fixed_avail"] = [_name(v) for v in self.tx_rf_ports_avail(card, tx_hdl)[0]]
        except Exception as e:
            out["tx_fixed_avail_err"] = str(e)
        return out

    # Explicit per-handle RF port routing for same-card J2 TX (Block 3 F4)
    # (public API since v4.5.0, SW dev manual "Detailed RF Port Configuration").
    RF_PORT_J1 = 0          # skiq_rf_port_J1
    RF_PORT_J2 = 1          # skiq_rf_port_J2

    def _ports_avail(self, sym, card, hdl):
        fn = getattr(self.lib, sym, None)
        if fn is None:
            raise SkiqError(f"no {sym} in this libsidekiq")
        nmax = 16                                   # >= skiq_rf_port_max
        nf = ctypes.c_uint8(0); nt = ctypes.c_uint8(0)
        fl = (ctypes.c_int * nmax)(); tl = (ctypes.c_int * nmax)()
        self._chk(fn(card, int(hdl), ctypes.byref(nf), fl, ctypes.byref(nt), tl), sym)
        return list(fl[:nf.value]), list(tl[:nt.value])

    def tx_rf_ports_avail(self, card, hdl):
        """(fixed_ports, trx_ports) as skiq_rf_port_t ints for a TX handle."""
        return self._ports_avail("skiq_read_tx_rf_ports_avail_for_hdl", card, hdl)

    def rx_rf_ports_avail(self, card, hdl):
        """(fixed_ports, trx_ports) as skiq_rf_port_t ints for an RX handle."""
        return self._ports_avail("skiq_read_rx_rf_ports_avail_for_hdl", card, hdl)

    def set_tx_rf_port(self, card, hdl, port: int) -> None:
        fn = getattr(self.lib, "skiq_write_tx_rf_port_for_hdl", None)
        if fn is None:
            raise SkiqError("no skiq_write_tx_rf_port_for_hdl in this libsidekiq")
        self._chk(fn(card, int(hdl), int(port)), "write_tx_rf_port_for_hdl")

    def set_rx_rf_port(self, card, hdl, port: int) -> None:
        fn = getattr(self.lib, "skiq_write_rx_rf_port_for_hdl", None)
        if fn is None:
            raise SkiqError("no skiq_write_rx_rf_port_for_hdl in this libsidekiq")
        self._chk(fn(card, int(hdl), int(port)), "write_rx_rf_port_for_hdl")

    def read_rx_rf_port(self, card, hdl) -> int:
        p = ctypes.c_int(-1)
        self._chk(self.lib.skiq_read_rx_rf_port_for_hdl(card, int(hdl), ctypes.byref(p)),
                  "read_rx_rf_port_for_hdl")
        return int(p.value)

    def read_tx_rf_port(self, card, hdl) -> int:
        p = ctypes.c_int(-1)
        self._chk(self.lib.skiq_read_tx_rf_port_for_hdl(card, int(hdl), ctypes.byref(p)),
                  "read_tx_rf_port_for_hdl")
        return int(p.value)

    def set_rx_rate_bw_multi(self, card, handles, rates, bws):
        # Dual channel mode shares one RFIC profile across both channels, so the
        # rate must be set for all handles at once (per-handle write returns
        # rc=-71). SW dev manual §5.8 dual-channel sequence. Rx/Tx rates derive
        # from the same clock, so this also sets the Tx rate.
        fn = getattr(self.lib, "skiq_write_rx_sample_rate_and_bandwidth_multi", None)
        if fn is None:
            raise SkiqError("no skiq_write_rx_sample_rate_and_bandwidth_multi in this libsidekiq")
        n = len(handles)
        h = (ctypes.c_int * n)(*[int(x) for x in handles])
        r = (ctypes.c_uint32 * n)(*[int(x) for x in rates])
        b = (ctypes.c_uint32 * n)(*[int(x) for x in bws])
        self._chk(fn(card, h, ctypes.c_uint8(n), r, b),
                  "write_rx_sample_rate_and_bandwidth_multi")

    def set_tx_rate_bw(self, card, hdl, rate, bw):
        self._chk(self.lib.skiq_write_tx_sample_rate_and_bandwidth(card, hdl, int(rate), int(bw)),
                  "write_tx_sample_rate_and_bandwidth")

    def rx_gain_index_range(self, card, hdl) -> Tuple[int, int]:
        lo = ctypes.c_uint8(0)
        hi = ctypes.c_uint8(0)
        rc = self.lib.skiq_read_rx_gain_index_range(card, hdl, ctypes.byref(lo), ctypes.byref(hi))
        if rc != 0:
            return (195, 255)
        return (lo.value, hi.value)

    def rx_gain_db(self, card, hdl, lo_freq_hz, index):
        # Cal offset (dB) for a gain index at the given LO freq. This is the
        # factory digital-power to absolute-power conversion factor,
        # not the front-end gain: its absolute value (~90-120 dB on NV100)
        # folds in full-chain scaling. Differences between indices are true
        # gain steps (~0.5 dB/index). Returns None without cal data.
        import ctypes as _ct
        off = _ct.c_double(0.0)
        rc = self.lib.skiq_read_rx_cal_offset_by_LO_freq_and_gain_index(
            card, hdl, int(lo_freq_hz), int(index), _ct.byref(off))
        return float(off.value) if rc == 0 else None

    def set_rx_gain_index(self, card, hdl, index):
        self._chk(self.lib.skiq_write_rx_gain(card, hdl, int(index)), "write_rx_gain")

    def apply_rx_cal(self, card, hdl):
        """Enable RX DC-offset correction (reduces the LO/DC spur at band center).
        DC-offset only: skiq_run_rx_cal is not called here: on this
        card it left the receiver outputting no signal. Best effort: a missing
        function or non-zero return is logged, never fatal."""
        if os.environ.get("DPMG_RXCAL", "1") == "0" or os.path.exists("/app_svc/.rxcal_off"):
            print("[RXCAL] disabled (DPMG_RXCAL=0 or .rxcal_off present)", flush=True)
            return
        dc = getattr(self.lib, "skiq_write_rx_dc_offset_corr", None)
        if dc is None:
            print("[RXCAL] skiq_write_rx_dc_offset_corr not in lib -- skipped", flush=True)
            return
        try:
            rc = dc(card, hdl, True)
            print(f"[RXCAL] dc_offset_corr enable card{card} hdl{hdl} -> rc={rc}", flush=True)
        except Exception as e:
            print(f"[RXCAL] dc_offset_corr failed: {e}", flush=True)

    # Under AGC (the NV100 power-up default) manual index writes return
    # success but the AGC immediately overrides them (seen live on the G40:
    # floor identical at index 0/30/60). Manual mode must be
    # set explicitly before index writes mean anything.
    def set_rx_gain_mode(self, card, hdl, manual: bool):
        fn = getattr(self.lib, "skiq_write_rx_gain_mode", None)
        if fn is None:
            raise SkiqError("no skiq_write_rx_gain_mode in this libsidekiq")
        self._chk(fn(card, hdl, 0 if manual else 1), "write_rx_gain_mode")

    def read_rx_gain_mode(self, card, hdl) -> str:
        fn = getattr(self.lib, "skiq_read_rx_gain_mode", None)
        if fn is None:
            return "unknown (read API absent)"
        import ctypes as _c
        m = _c.c_int(-1)
        if fn(card, hdl, _c.byref(m)) != 0:
            return "unknown (read failed)"
        return {0: "manual", 1: "auto"}.get(m.value, f"enum{m.value}")

    def set_tx_attenuation(self, card, hdl, quarter_db):
        self._chk(self.lib.skiq_write_tx_attenuation(card, hdl, int(quarter_db)),
                  "write_tx_attenuation")

    def lib_version(self) -> str:
        """Runtime libsidekiq version, tolerant of signature drift."""
        try:
            import ctypes as _c
            ma, mi, pa = _c.c_uint8(0), _c.c_uint8(0), _c.c_uint8(0)
            lab = _c.c_char_p()
            if self.lib.skiq_read_libsidekiq_version(
                    _c.byref(ma), _c.byref(mi), _c.byref(pa), _c.byref(lab)) == 0:
                s = f"{ma.value}.{mi.value}.{pa.value}"
                if lab.value:
                    s += lab.value.decode(errors="ignore")
                return s
        except Exception:
            pass
        return "unknown"

    # Single vs dual channel. Per the SW dev manual (NV100,
    # 5.4.12): channel A1 TX runs in single mode; to transmit (or receive)
    # on channel A2 the card must be in dual mode, and start_tx_streaming is
    # called with the A2 handle (which enables both A1 and A2). Probed, not
    # assumed: 4.19.1 has it; older libs may not.
    CHAN_MODE_SINGLE = 0
    CHAN_MODE_DUAL = 1

    def set_chan_mode(self, card, dual: bool) -> str:
        fn = getattr(self.lib, "skiq_write_chan_mode", None)
        if fn is None:
            raise SkiqError("no skiq_write_chan_mode in this libsidekiq")
        self._chk(fn(card, self.CHAN_MODE_DUAL if dual
                     else self.CHAN_MODE_SINGLE), "write_chan_mode")
        return "dual" if dual else "single"

    def read_chan_mode(self, card) -> str:
        fn = getattr(self.lib, "skiq_read_chan_mode", None)
        if fn is None:
            return "unknown (read API absent)"
        import ctypes as _c
        m = _c.c_int(-1)
        if fn(card, _c.byref(m)) != 0:
            return "unknown (read failed)"
        return {0: "single", 1: "dual"}.get(m.value, f"enum{m.value}")

    # Reference-clock selection (internal / external / gpsdo).
    # Symbols are probed, never assumed: 4.19.1 and 4.21.0 differ, and the
    # bridge must fail with a clear message rather than a ctypes crash.
    _REF_CLOCK_ENUM = {"internal": 0, "external": 1, "host": 2, "carrier": 3}

    def set_ref_clock(self, card, source: str,
                      ext_freq_hz: int = 10_000_000) -> str:
        """Apply a reference-clock setpoint. Returns the readback string.
        'gpsdo' = internal ref + GPSDO discipline enabled (NV100 module).
        'carrier' = the carrier's 40 MHz clock, the G40 path for an external
        10 MHz (g40_clock); left alone when the card already reads it."""
        source = (source or "internal").lower()
        wr = getattr(self.lib, "skiq_write_ref_clock_select", None)
        if source == "gpsdo":
            en = getattr(self.lib, "skiq_gpsdo_enable", None)
            if en is None:
                raise SkiqError("gpsdo requested but this libsidekiq has no "
                                "skiq_gpsdo_enable")
            if wr is not None:
                self._chk(wr(card, self._REF_CLOCK_ENUM["internal"]),
                          "write_ref_clock_select(internal for gpsdo)")
            self._chk(en(card), "gpsdo_enable")
            return self.read_ref_clock(card) + "+gpsdo"
        if source not in self._REF_CLOCK_ENUM:
            raise SkiqError(f"unknown clock source {source!r}")
        # leaving gpsdo must turn the discipline off, else it stays enabled+locked
        # and "internal" is still GPS-disciplined. Best-effort (harmless if already off).
        dis = getattr(self.lib, "skiq_gpsdo_disable", None)
        if dis is not None:
            try:
                dis(card)
            except Exception:
                pass
        if source == "carrier" and self.read_ref_clock(card) == "carrier":
            return "carrier"
        if wr is None:
            if source == "internal":
                return "internal (select API absent; card default)"
            raise SkiqError(f"{source} requested but this libsidekiq has no "
                            "skiq_write_ref_clock_select")
        if source == "external":
            wf = getattr(self.lib, "skiq_write_ext_ref_clock_freq", None)
            if wf is not None:
                self._chk(wf(card, int(ext_freq_hz)), "write_ext_ref_clock_freq")
        self._chk(wr(card, self._REF_CLOCK_ENUM[source]),
                  "write_ref_clock_select")
        return self.read_ref_clock(card)

    def read_ref_clock(self, card) -> str:
        rd = getattr(self.lib, "skiq_read_ref_clock_select", None)
        if rd is None:
            return "unknown (read API absent)"
        import ctypes as _c
        sel = _c.c_int(-1)
        if rd(card, _c.byref(sel)) != 0:
            return "unknown (read failed)"
        names = {v: k for k, v in self._REF_CLOCK_ENUM.items()}
        return names.get(sel.value, f"enum{sel.value}")

    # GPSDO lock + ref-clock presence for the SDR-tab indicator.
    # Best-effort: a missing symbol or failed read gives None, never raises
    # (4.19.1 and 4.21.0 differ, and a status poll mustn't throw).
    def gpsdo_locked(self, card):
        """Fast GPSDO lock read (no 1PPS probe, safe to poll). Returns True/False
        when GPSDO is enabled, else None (timing ref isn't GPS-disciplined)."""
        import ctypes as _c

        def _bc(sym):
            fn = getattr(self.lib, sym, None)
            if fn is None:
                return None
            fn.restype = _c.c_int32
            fn.argtypes = [_c.c_uint8, _c.POINTER(_c.c_bool)]
            b = _c.c_bool(False)
            return bool(b.value) if fn(card, _c.byref(b)) == 0 else None

        if _bc("skiq_gpsdo_is_enabled") is not True:
            return None
        return _bc("skiq_gpsdo_is_locked")

    # 1 PPS: the pulse the card sees and the card clock counted against it. Reads
    # of the last-1PPS timestamp are one FPGA register pair each, safe to poll.
    _PPS_SOURCE_ENUM = {"external": 0, "host": 1}      # skiq_1pps_source_t

    def set_pps_source(self, card, source: str = "external") -> str:
        """skiq_write_1pps_source. On the G40 the cards take their 1 PPS from the
        carrier pin, which the API calls external; the carrier's PCC.00 line then
        picks the GPS module's pulse or the chassis jack (g40_clock). Returns the
        readback name, or a note when the API is absent."""
        import ctypes as _c
        wr = getattr(self.lib, "skiq_write_1pps_source", None)
        if wr is None:
            return "unknown (write API absent)"
        _sig(wr, _c.c_int32, [_c.c_uint8, _c.c_int])
        self._chk(wr(card, self._PPS_SOURCE_ENUM[source]), "write_1pps_source")
        return self.read_pps_source(card)

    def read_pps_source(self, card) -> str:
        import ctypes as _c
        rd = getattr(self.lib, "skiq_read_1pps_source", None)
        if rd is None:
            return "unknown (read API absent)"
        _sig(rd, _c.c_int32, [_c.c_uint8, _c.POINTER(_c.c_int)])
        s = _c.c_int(-1)
        if rd(card, _c.byref(s)) != 0:
            return "unknown (read failed)"
        names = {v: k for k, v in self._PPS_SOURCE_ENUM.items()}
        return names.get(s.value, f"enum{s.value}")

    def sys_timestamp_freq(self, card) -> int:
        """skiq_read_sys_timestamp_freq: the rate the system timestamp counts at
        (40 MHz on the NV100). The nominal when the API is absent or fails."""
        import ctypes as _c
        fn = getattr(self.lib, "skiq_read_sys_timestamp_freq", None)
        if fn is None:
            return 40_000_000
        _sig(fn, _c.c_int32, [_c.c_uint8, _c.POINTER(_c.c_uint64)])
        f = _c.c_uint64(0)
        if fn(card, _c.byref(f)) != 0 or f.value <= 0:
            return 40_000_000
        return int(f.value)

    def read_last_1pps(self, card):
        """(rf_timestamp, sys_timestamp) latched at the last 1 PPS, or None when the
        API is absent or the read fails (a -ERANGE read straddled a pulse: try again
        next time). Both are zero until the first pulse after init."""
        import ctypes as _c
        fn = getattr(self.lib, "skiq_read_last_1pps_timestamp", None)
        if fn is None:
            return None
        _sig(fn, _c.c_int32, [_c.c_uint8, _c.POINTER(_c.c_uint64), _c.POINTER(_c.c_uint64)])
        rf, sy = _c.c_uint64(0), _c.c_uint64(0)
        try:
            rc = fn(card, _c.byref(rf), _c.byref(sy))
        except Exception:
            return None
        if rc != 0:
            return None
        return int(rf.value), int(sy.value)

    def ext_ref_present(self, card):
        """Is the card running on the external reference: the reference select
        reads external. libsidekiq has no reference-present read
        (skiq_read_ext_ref_clock_freq is the configured frequency, 40 MHz on
        the carrier), but the card does not come up on external without a
        reference on REF IN, so an inited card with that select is on it.
        Fast, no 1PPS probe, safe to poll."""
        try:
            return str(self.read_ref_clock(card) or "").lower().startswith("external")
        except Exception:
            return None

    # NV100 / Sidekiq on-card temperature. Best-effort like the GPS
    # reads: a missing symbol or a failed read gives None, never raises. The
    # card must be initialised at full level (-ENODEV otherwise); with the
    # FPGA GPSDO enabled the FPGA owns the sensor and a read can return -EAGAIN.
    def read_temp(self, card):
        """On-card temperature in whole degrees C, or None when unavailable."""
        import ctypes as _c
        fn = getattr(self.lib, "skiq_read_temp", None)
        if fn is None:
            return None
        fn.restype = _c.c_int32
        fn.argtypes = [_c.c_uint8, _c.POINTER(_c.c_int8)]
        t = _c.c_int8(0)
        try:
            rc = fn(card, _c.byref(t))
        except Exception:
            return None
        return int(t.value) if rc == 0 else None

    # NV100 on-card accelerometer (skiq_read_accel, thousandths of g).
    # Best-effort: unsupported, disabled-and-cannot-enable or a failed read gives
    # None. Needs an initialised card. Enabling only powers the sensor.
    def read_accel(self, card):
        """(x, y, z) in g, or None when unavailable."""
        import ctypes as _c
        L = self.lib
        rd = getattr(L, "skiq_read_accel", None)
        if rd is None:
            return None
        ready = getattr(self, "_accel_ready", None)
        if ready is None:
            ready = self._accel_ready = {}
        if card not in ready:
            ok = False
            try:
                sup = getattr(L, "skiq_is_accel_supported", None)
                if sup is not None:
                    sup.restype = _c.c_int32
                    sup.argtypes = [_c.c_uint8, _c.POINTER(_c.c_bool)]
                    b = _c.c_bool(False)
                    ok = sup(card, _c.byref(b)) == 0 and bool(b.value)
                if ok:
                    st = getattr(L, "skiq_read_accel_state", None)
                    on = _c.c_uint8(0)
                    if st is not None:
                        st.restype = _c.c_int32
                        st.argtypes = [_c.c_uint8, _c.POINTER(_c.c_uint8)]
                        st(card, _c.byref(on))
                    if not on.value:
                        wr = getattr(L, "skiq_write_accel_state", None)
                        if wr is not None:
                            wr.restype = _c.c_int32
                            wr.argtypes = [_c.c_uint8, _c.c_uint8]
                            ok = wr(card, 1) == 0
            except Exception:
                ok = False
            ready[card] = ok
        if not ready[card]:
            return None
        rd.restype = _c.c_int32
        rd.argtypes = [_c.c_uint8] + [_c.POINTER(_c.c_int16)] * 3
        x, y, z = _c.c_int16(0), _c.c_int16(0), _c.c_int16(0)
        try:
            rc = rd(card, _c.byref(x), _c.byref(y), _c.byref(z))
        except Exception:
            return None
        if rc != 0:
            return None
        return (x.value / 1000.0, y.value / 1000.0, z.value / 1000.0)

    def read_gps_status(self, card, pps_probe_s: float = 1.1) -> dict:
        """GPSDO supported/enabled/locked, freq accuracy, and external-ref health.
        ext_ref_present = a plausible (>1 MHz) measured clock; pps_present = the
        last-1pps timestamp advanced across a >1 s window (a live 1 Hz edge)."""
        import ctypes as _c
        out = {"gpsdo_supported": None, "gpsdo_enabled": None, "gpsdo_locked": None,
               "freq_accuracy_ppm": None, "ref_clock": self.read_ref_clock(card),
               "ext_ref_freq_hz": None, "ext_ref_present": None,
               "pps_present": None, "pps_source": None}

        def _bool_call(sym):
            fn = getattr(self.lib, sym, None)
            if fn is None:
                return None
            fn.restype = _c.c_int32
            fn.argtypes = [_c.c_uint8, _c.POINTER(_c.c_bool)]
            b = _c.c_bool(False)
            return bool(b.value) if fn(card, _c.byref(b)) == 0 else None

        out["gpsdo_supported"] = _bool_call("skiq_is_gpsdo_supported")
        out["gpsdo_enabled"] = _bool_call("skiq_gpsdo_is_enabled")
        out["gpsdo_locked"] = _bool_call("skiq_gpsdo_is_locked")

        fa = getattr(self.lib, "skiq_gpsdo_read_freq_accuracy", None)
        if fa is not None:
            fa.restype = _c.c_int32
            fa.argtypes = [_c.c_uint8, _c.POINTER(_c.c_double)]
            d = _c.c_double(0.0)
            if fa(card, _c.byref(d)) == 0:
                out["freq_accuracy_ppm"] = float(d.value)

        ef = getattr(self.lib, "skiq_read_ext_ref_clock_freq", None)
        if ef is not None:
            ef.restype = _c.c_int32
            ef.argtypes = [_c.c_uint8, _c.POINTER(_c.c_uint32)]
            f = _c.c_uint32(0)
            if ef(card, _c.byref(f)) == 0:
                # the frequency the card is configured to expect on REF IN (the
                # carrier reads 40 MHz here on internal); libsidekiq has no
                # "reference present" read
                out["ext_ref_freq_hz"] = int(f.value)
        # ext_ref_present: the reference select reads external. The card does not
        # finish its init on external without a reference on REF IN, so a card
        # that is up with that select is running on it. The 1 PPS below is the
        # live measurement.
        out["ext_ref_present"] = str(out.get("ref_clock") or "").lower().startswith("external")

        ps = getattr(self.lib, "skiq_read_1pps_source", None)
        if ps is not None:
            ps.restype = _c.c_int32
            ps.argtypes = [_c.c_uint8, _c.POINTER(_c.c_int)]
            s = _c.c_int(-1)
            if ps(card, _c.byref(s)) == 0:
                out["pps_source"] = int(s.value)

        # 1PPS presence: a live 1 Hz edge advances the last-1pps timestamp within 1 s
        tsf = getattr(self.lib, "skiq_read_last_1pps_timestamp", None)
        if tsf is not None:
            tsf.restype = _c.c_int32
            tsf.argtypes = [_c.c_uint8, _c.POINTER(_c.c_uint64), _c.POINTER(_c.c_uint64)]
            rf1, sy1 = _c.c_uint64(0), _c.c_uint64(0)
            if tsf(card, _c.byref(rf1), _c.byref(sy1)) == 0:
                import time as _t
                _t.sleep(max(0.0, pps_probe_s))
                rf2, sy2 = _c.c_uint64(0), _c.c_uint64(0)
                if tsf(card, _c.byref(rf2), _c.byref(sy2)) == 0:
                    out["pps_present"] = bool(rf2.value != rf1.value and rf2.value != 0)
        return out

    def read_iq_order(self, card) -> str:
        """Read back the skiq_iq_order_t mode actually latched on the card."""
        import ctypes as _c
        mode = _c.c_int(-1)
        rc = self.lib.skiq_read_iq_order_mode(card, _c.byref(mode))
        if rc != 0:
            return f"unavailable (rc={rc})"
        return {IQ_ORDER_QI: "QI", IQ_ORDER_IQ: "IQ"}.get(mode.value, f"unknown({mode.value})")

    def set_iq_order(self, card, iq_order_iq: bool):
        self._chk(self.lib.skiq_write_iq_order_mode(card, IQ_ORDER_IQ if iq_order_iq else IQ_ORDER_QI),
                  "write_iq_order_mode")

    def read_rx_sample_rate(self, card, hdl) -> float:
        """Return the achieved RX sample rate (Hz). The card may override the
        requested rate (e.g. force a higher RFIC profile), so the analysis /
        PSD frequency axis must use this, not the requested value."""
        rate = ctypes.c_uint32(0)
        actual = ctypes.c_double(0.0)
        rc = self.lib.skiq_read_rx_sample_rate(card, hdl, ctypes.byref(rate), ctypes.byref(actual))
        if rc != 0:
            return 0.0
        # prefer the precise double 'actual'; fall back to the uint32 if needed
        return float(actual.value) if actual.value > 0 else float(rate.value)

    def read_rx_rate_range(self, card, hdl):
        """(min_hz, max_hz) achievable RX sample rate for this handle. Returns
        None if libsidekiq lacks the call. A min far below 50 Msps means the
        FPGA sample-decimator build is loaded on this X40."""
        fn = getattr(self.lib, "skiq_read_rx_sample_rate_range", None)
        if fn is None:
            return None
        lo = ctypes.c_uint32(0)
        hi = ctypes.c_uint32(0)
        rc = fn(card, hdl, ctypes.byref(hi), ctypes.byref(lo))  # (p_max, p_min)
        if rc != 0:
            return None
        return (int(lo.value), int(hi.value))

    # TX configuration 
    def configure_tx(self, card, hdl, block_samples: int = None):
        if block_samples is None:
            block_samples = getattr(self, "tx_block_samples", TX_BLOCK_SAMPLES)
        self._chk(self.lib.skiq_write_tx_data_flow_mode(card, hdl, TX_FLOW_IMMEDIATE),
                  "write_tx_data_flow_mode")
        # prefer async transfer (libsidekiq RT C threads pace the FPGA
        # 50-packet queue ~= 13 ms host tolerance vs 266 us in sync). Must be set
        # before start_tx_streaming. Unsupported product/bitstream: sync fallback.
        self._tx_async = getattr(self, "_tx_async", {})
        # DPMG_TX_MODE=sync forces synchronous TX transfer (default async)
        want_sync = _os.environ.get("DPMG_TX_MODE", "").strip().lower() == "sync"
        rc = (-1 if want_sync else
              self.lib.skiq_write_tx_transfer_mode(card, hdl, TX_TRANSFER_ASYNC))
        if rc == 0:
            try:
                self.lib.skiq_write_num_tx_threads(card, TX_ASYNC_THREADS)
                # DPMG_TX_ASYNC_PRIO overrides the send threads' RT priority
                _pr = int(_os.environ.get("DPMG_TX_ASYNC_PRIO", "") or TX_ASYNC_PRIORITY)
                self.lib.skiq_write_tx_thread_priority(card, _pr)
            except AttributeError:
                pass
            self._tx_async[(card, hdl)] = True
            print(f"[skiq] ASYNC_TX enabled: {TX_ASYNC_THREADS} send threads, prio "
                  f"{_os.environ.get('DPMG_TX_ASYNC_PRIO', '') or TX_ASYNC_PRIORITY}, "
                  f"queue 50 pkts", flush=True)
        else:
            self._chk(self.lib.skiq_write_tx_transfer_mode(card, hdl, TX_TRANSFER_SYNC),
                      "write_tx_transfer_mode")
            self._tx_async[(card, hdl)] = False
            print(f"[skiq] ASYNC_TX unsupported here (rc={rc}); using SYNC transfer",
                  flush=True)
        self._chk(self.lib.skiq_write_tx_block_size(card, hdl, block_samples),
                  "write_tx_block_size")

    def _prepare_tx_blocks(self, samples: np.ndarray, iq_order_iq: bool):
        """Build all tx blocks (header + interleaved int16 data)
        in one contiguous numpy allocation, zero-padding the waveform to a full
        block multiple. Done once per waveform and cached, so the realtime send
        loop does no allocation/interleave work at all."""
        s = np.asarray(samples, dtype=np.complex64)
        n = len(s)
        step = getattr(self, "tx_block_samples", TX_BLOCK_SAMPLES)
        dual = getattr(self, "tx_dual", False)
        nblocks = (n + step - 1) // step
        pad = nblocks * step - n
        i16 = np.clip(np.round(s.real * TX_FULL_SCALE), -32768, 32767).astype(np.int16)
        q16 = np.clip(np.round(s.imag * TX_FULL_SCALE), -32768, 32767).astype(np.int16)
        if pad:
            i16 = np.concatenate([i16, np.zeros(pad, dtype=np.int16)])
            q16 = np.concatenate([q16, np.zeros(pad, dtype=np.int16)])
        inter = np.empty(2 * nblocks * step, dtype=np.int16)
        if iq_order_iq:
            inter[0::2] = i16; inter[1::2] = q16
        else:
            inter[0::2] = q16; inter[1::2] = i16
        hdr16 = TX_HEADER_BYTES // 2                       # header size in int16 words
        if dual:
            # Second half (see bench/crosscard_txb1_probe.py): a dual-mode
            # TX block carries both channels, laid out
            # as [A1: step samples][B1/A2: step samples] after the header. The
            # J2/TRx2x jack is driven by the second channel, so the waveform goes
            # in the second 2*step int16 and the A1 half stays zero. Data in the
            # first half has the right rate (one
            # contiguous channel of `step` unique samples, no [s0,s0,s1,s1]
            # doubling) but the wrong channel: it plays on A1, whose
            # DAC in fixed port mode reaches the same card's J1 receiver only by
            # on-card coupling (so a same-card "FDD" loopback cannot tell the two
            # layouts apart) and radiates nothing from J2. Cross-card, with
            # no coupling, first-half data gives 0 dB at the far RX on both
            # handles; second-half data on handle B1 gives the tone at +90 dB
            # (TX card1 J2/TRx2B through a 50 dB pad into RX TRx1A). Handle A2 (1)
            # radiates nothing from J2 with either layout, so the console's TXA2
            # is sent as handle B1.
            blocks = np.zeros((nblocks, hdr16 + 4 * step), dtype=np.int16)
            blocks[:, hdr16 + 2 * step:hdr16 + 4 * step] = inter.reshape(nblocks, 2 * step)
        else:
            # one matrix: each row = one tx block = header (zeroed) + data
            blocks = np.zeros((nblocks, hdr16 + 2 * step), dtype=np.int16)
            blocks[:, hdr16:] = inter.reshape(nblocks, 2 * step)
        blocks = np.ascontiguousarray(blocks)
        return blocks

    def prepare_tx(self, samples: np.ndarray, iq_order_iq: bool):
        """The prepared block buffer for this waveform, built now if it is not the
        cached one. A long waveform takes a while to build, so the backend calls
        this before keying the PA and the build does not run on the PA watchdog."""
        # The cache holds the waveform object itself and compares
        # identity. Keying on the data address would let a new waveform of the same length
        # that lands at the freed address of the old one reuse the old blocks (a
        # 4 MHz noise patch would go out as the barrage loaded before it).
        # Holding the reference keeps the old array alive, so its address cannot be
        # reused while it is cached.
        # A live stream (a new array on every send) replaces the buffer each
        # time while the async queue may still hold packets pointing into the
        # old one: keep replaced buffers until newer ones cover TX_RETAIN_BLOCKS
        # blocks, or the queued packets read freed memory and the card sends
        # garbage in the middle of the stream.
        cached = getattr(self, "_tx_prep", None)
        if (cached is None or cached[0] is not samples
                or cached[1] != (len(samples), bool(iq_order_iq))):
            if cached is not None:
                kept = getattr(self, "_tx_retired", None)
                if kept is None:
                    kept = self._tx_retired = []
                kept.append(cached)
                newer = 0
                for i in range(len(kept) - 1, -1, -1):          # newest first
                    newer += int(kept[i][2].shape[0])
                    if newer >= TX_RETAIN_BLOCKS:
                        del kept[:i]
                        break
            self._tx_prep = (samples, (len(samples), bool(iq_order_iq)),
                             self._prepare_tx_blocks(samples, iq_order_iq))
        return self._tx_prep[2]

    def transmit(self, card, hdl, samples: np.ndarray, iq_order_iq: bool) -> None:
        """Transmit complex IQ from a prepared, cached, block-aligned buffer. The
        loop below is only skiq_transmit calls (ctypes releases the GIL during
        each call), with no per-block Python allocation (a cause of underruns)
        and no partial final block."""
        blocks = self.prepare_tx(samples, iq_order_iq)
        base = blocks.ctypes.data
        stride = blocks.strides[0]
        tx_fn = self.lib.skiq_transmit
        import time as _time
        for k in range(blocks.shape[0]):
            while True:
                rc = tx_fn(card, hdl, ctypes.c_void_p(base + k * stride), None)
                if rc == 0:
                    break
                if rc == TX_QUEUE_FULL:      # async queue full: let it drain
                    _time.sleep(0.0005)      # ~2 blocks' worth
                    continue
                raise SkiqError(f"skiq_transmit failed rc={rc} (block {k}/{blocks.shape[0]})")
        # read the card's own underrun counter once per waveform
        # send (cheap) and print deltas: shows up in `docker logs dpmg`.
        n = self.tx_underruns(card, hdl)
        if n is not None:
            prev = getattr(self, "_urun_last", 0)
            if n < prev:
                prev = 0            # counter reset (stream restarted)
            if n > prev:
                print(f"[skiq] TX underruns +{n - prev} this waveform (card total {n})", flush=True)
            self._urun_last = n

    def tx_underruns(self, card, hdl):
        """Read the card's TX underrun counter, if this libsidekiq
        exports it. Returns int count, or None if unavailable."""
        fn = getattr(self.lib, "skiq_read_tx_num_underruns", None)
        if fn is None:
            return None
        try:
            n = ctypes.c_uint32(0)
            if fn(card, hdl, ctypes.byref(n)) == 0:
                return int(n.value)
        except Exception:
            pass
        return None
    # Libsidekiq exposes no RX overrun counter in this API
    # surface, but a FIFO overflow always shows up as an rf_timestamp gap
    # between consecutive RX blocks. Track expectation per card; each gap
    # event counts one overrun. Reset on start_rx so a stop/start cycle's
    # timestamp jump is not a false positive.
    def _rx_gap_track(self, card, rf_ts, n_samp):
        if not hasattr(self, "_rx_expect"):
            self._rx_expect = {}
            self.rx_overruns = {}
        exp = self._rx_expect.get(card)
        if exp is not None and rf_ts > exp:
            self.rx_overruns[card] = self.rx_overruns.get(card, 0) + 1
        self._rx_expect[card] = rf_ts + n_samp

    def receive_raw(self, card):
        """Return (int16 copy, rf_timestamp) with no conversion. The caller batches many
        blocks and converts once. Per-block conversion measured 36 us on the Jetson against
        a 33 us RF budget: not the data volume (4 KB), but numpy's ~9 us fixed call
        overhead x4 on a 2036-element array. Batching 2048 blocks amortizes it away."""
        hdl = ctypes.c_int(0); pblock = ctypes.c_void_p(); dlen = ctypes.c_uint32(0)
        status = self.lib.skiq_receive(card, ctypes.byref(hdl),
                                       ctypes.byref(pblock), ctypes.byref(dlen))
        if status != RX_STATUS_SUCCESS or not pblock.value:
            return None
        hdr = _RxBlockHeader.from_address(pblock.value)
        rf_ts = int(hdr.rf_timestamp)
        n_int16 = (dlen.value - RX_HEADER_BYTES) // 2
        if n_int16 <= 1:
            return None
        self._rx_gap_track(card, rf_ts, n_int16 // 2)   # overrun count
        if getattr(self, "_blk_n", None) != n_int16:
            self._blk_type = ctypes.c_int16 * n_int16
            self._blk_n = n_int16
        raw = np.ctypeslib.as_array(
            self._blk_type.from_address(pblock.value + RX_HEADER_BYTES))
        return raw.copy(), rf_ts

    # native RX drain
    def native_drain(self):
        """The loaded librxdrain, or None (reason in self.native_why)."""
        if not hasattr(self, "_rxd"):
            self._rxd, self.native_why = load_rxdrain()
            self._rxd_states = {}
            if self._rxd is not None:
                self._rxd_fn = ctypes.cast(self.lib.skiq_receive, ctypes.c_void_p)
        return self._rxd

    def receive_batch(self, card, max_blocks, budget_s, iq_order_iq):
        """Drain up to max_blocks RX blocks in one native call. Returns
        (complex64 chunk, state) with state an _RxdState (ts0, n_blocks, gap
        counts, empty); chunk is None when no block came. Same samples and
        timestamp checks as receive_raw + the scheduler's batch convert."""
        lib = self.native_drain()
        st = self._rxd_states.get(card)
        if st is None:
            st = self._rxd_states[card] = _RxdState()
        cap = int(max_blocks) * 2036           # floats: max_blocks largest blocks (1018 samples)
        buf = np.empty(cap, dtype=np.float32)  # new buffer per batch: consumers keep old frames
        n = lib.skiq_drain_batch(self._rxd_fn, card, buf.ctypes.data, cap, int(max_blocks),
                                 float(budget_s), 0 if iq_order_iq else 1, ctypes.byref(st))
        if st.n_fwd:                           # Same count as _rx_gap_track
            if not hasattr(self, "rx_overruns"):
                self._rx_expect = {}
                self.rx_overruns = {}
            self.rx_overruns[card] = self.rx_overruns.get(card, 0) + int(st.n_fwd)
        if n <= 0:
            return None, st
        return buf[: st.n_floats].view(np.complex64), st

    # streaming
    def start_rx(self, card, hdl):
        if hasattr(self, "_rx_expect"):
            self._rx_expect.pop(card, None)   # Fresh epoch
        if getattr(self, "_rxd_states", None) is not None:
            self._rxd_states.pop(card, None)  # Fresh epoch
        self._chk(self.lib.skiq_start_rx_streaming(card, hdl), "start_rx_streaming")

    def stop_rx(self, card, hdl):
        self.lib.skiq_stop_rx_streaming(card, hdl)

    def start_tx(self, card, hdl):
        self._chk(self.lib.skiq_start_tx_streaming(card, hdl), "start_tx_streaming")

    def stop_tx(self, card, hdl):
        self.lib.skiq_stop_tx_streaming(card, hdl)

    # receive
    def receive(self, card, iq_order_iq):
        import time as _t
        _a = _t.perf_counter()
        hdl = ctypes.c_int(0); pblock = ctypes.c_void_p(); dlen = ctypes.c_uint32(0)
        status = self.lib.skiq_receive(card, ctypes.byref(hdl),
                                       ctypes.byref(pblock), ctypes.byref(dlen))
        _b = _t.perf_counter()
        if status == RX_STATUS_NO_DATA: return None
        if status != RX_STATUS_SUCCESS or not pblock.value: return None
        hdr = _RxBlockHeader.from_address(pblock.value)
        rf_ts = int(hdr.rf_timestamp)
        n_int16 = (dlen.value - RX_HEADER_BYTES) // 2
        if n_int16 <= 1: return None
        self._rx_gap_track(card, rf_ts, n_int16 // 2)   # overrun count
        data_addr = pblock.value + RX_HEADER_BYTES
        _c = _t.perf_counter()
        if getattr(self, "_blk_n", None) != n_int16:
            self._blk_type = ctypes.c_int16 * n_int16
            self._blk_n = n_int16
        raw = np.ctypeslib.as_array(self._blk_type.from_address(data_addr))
        _d = _t.perf_counter()
        f = raw.astype(np.float32)
        f *= (1.0 / 32768.0)
        if not iq_order_iq:
            f = f.reshape(-1, 2)[:, ::-1].reshape(-1)
        iq = f.view(np.complex64)
        _e = _t.perf_counter()
        if not hasattr(self, "_pt"):
            self._pt = [0.0, 0.0, 0.0, 0.0]; self._pn = 0
        self._pt[0] += _b-_a; self._pt[1] += _c-_b; self._pt[2] += _d-_c; self._pt[3] += _e-_d
        self._pn += 1
        if self._pn % 20000 == 0:
            t = [1e6*x/self._pn for x in self._pt]
            print(f"[RECV] skiq_receive={t[0]:.1f}us hdr={t[1]:.1f}us as_array={t[2]:.1f}us "
                  f"convert={t[3]:.1f}us total={sum(t):.1f}us (budget 33us)", flush=True)
        return iq, rf_ts

    # helpers
    def _chk(self, rc, name):
        if rc != 0:
            # libsidekiq returns -errno; name it (rc=-95 = EOPNOTSUPP, the
            # "Cannot stream on handle A2 in single channel mode" refusal)
            why = ""
            try:
                if -200 < rc < 0:
                    why = f" ({_os.strerror(-rc)})"
            except Exception:
                pass
            raise SkiqError(f"{name} failed rc={rc}{why}")
