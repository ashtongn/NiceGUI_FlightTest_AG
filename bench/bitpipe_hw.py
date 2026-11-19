#!/usr/bin/env python3
"""
bitpipe_hw.py: the bitpipe stream link on a real radio (test cases HW-1, HW-2).

One radio in FDD loopback (Setup A: TX TRx1B through the pad into RX TRx1A).
The bitpipe container runs here on the laptop, clear. By default the radio's
stream modem listens (bitpipe_start listen=, allow_from=this laptop) and the
container dials out to it, because the laptop's firewall drops new inbound
connections; --dial-in has the modem connect to the laptop instead (needs an
inbound rule for the bit port). On a
loopback the container hears its own frames, so what goes in on a stream
comes back out of it: chat lines, a file, then a soak with a line every few
seconds. Every number comes from the container (lock, frames, BER before
FEC, frame errors, round trip) and the radio (modem brief, STATUS overruns
and underruns, RX overload, gain index).

    python3 bench/bitpipe_hw.py --host 192.168.0.22 probe
        hello, get_config, bitpipe_status; no init, no TX
    python3 bench/bitpipe_hw.py --host 192.168.0.22 path
        init + start_rx + bitpipe_start, check the modem reaches the
        container, bitpipe_stop; no TX
    python3 bench/bitpipe_hw.py --host 192.168.0.22 --arm run --mods FSK
        HW-1 (FSK) or HW-2 (--mods 4FSK BPSK QPSK); keys TX
    python3 bench/bitpipe_hw.py --host 192.168.0.22 restore --rx-port TRx1B --tx-port TRx1A \\
        --rate 30.72e6 --clock gpsdo --center 915e6 --atten 30 --gain 250
    python3 bench/bitpipe_hw.py --kind b200 --host 192.168.55.1 --port 47812 --arm run --mods FSK QPSK
        the B200 on its own TX/RX to RX2 loopback through 20 dB (HW-3): 2 MS/s,
        RX gain 30 dB, TX gain 40 dB (refused above 45), no card block rule
    python3 bench/bitpipe_hw.py --kind b200 --host 192.168.55.1 --port 47812 restore \\
        --rate 2e6 --center 915e6 --b200-rx-gain 30 --b200-tx-gain 45
        the B200 demo state: RX2, TX/RX, internal clock, RX running
        re-init to a given state and start RX; no TX

    python3 bench/bitpipe_hw.py pair
        the two radio link (HW-4): G40 TX TRx1A through 50 dB into B200 RX2,
        B200 TX/RX through 20 dB into G40 TRx1B. One container per radio on
        the laptop, each dialling out to its radio's modem, so the G40 end
        talks to the B200 end. No TX; checks both bit faces come up.
    python3 bench/bitpipe_hw.py --arm pair --mods FSK --soak-s 60
    python3 bench/bitpipe_hw.py --arm pair --mods FSK QPSK --crypto secure --key-file KEYS.json
    python3 bench/bitpipe_hw.py --arm pair --mods FSK --soak-s 10 --fault-s 10 --fault-radio B200
        BP-33: one radio's TX off for 10 s in the middle of a file each way;
        the other end must lose lock, re-lock and then get every byte
        keys both radios: lock, chat and a file each way, a soak, every
        figure per direction. G40 at 1.92 MS/s, B200 at 2 MS/s (the modems
        need no common sample rate). Ends with TX and RX stopped on both,
        the B200 closed and the G40 inited and idle.

Stops TX at once on an underrun burst (modem or card), a front-end refusal,
TX dropping by itself, or a lost lock; always ends with stop_tx, bitpipe_stop
and stop_rx. Writes a JSON and a Markdown report to --out.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import socket
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import numpy as np                                                  # noqa: E402
from dpmg.link import proto                                                        # noqa: E402
from dpmg.link.proto import CmdStatus, MsgType, Status                        # noqa: E402
from checkout_common import Link, ack_err, init_ports, safe_cmd     # noqa: E402
from dpmg.bitpipe import BitpipeConfig, BitpipeContainer, NullCrypto  # noqa: E402
from dpmg.bitpipe import streams as st                              # noqa: E402

OUT_DEFAULT = os.path.join(HERE, "results", "bitpipe")


def now_s() -> str:
    return _dt.datetime.now().strftime("%H:%M:%S")


def log(msg: str) -> None:
    print(f"[{now_s()}] {msg}", flush=True)


class Radio:
    """The command link plus the latest STATUS the service pushed."""

    def __init__(self, host: str, port: int):
        self.link = Link(host, port)
        self.status = None

    def pump(self, seconds: float = 0.0) -> None:
        """Read whatever is queued (and keep reading for `seconds`), keeping the last STATUS."""
        s = self.link.s
        t_end = time.time() + seconds
        while True:
            s.settimeout(max(0.01, t_end - time.time()) if seconds else 0.01)
            try:
                data, _ = s.recvfrom(65535)
            except (socket.timeout, BlockingIOError):
                if time.time() >= t_end:
                    break
                continue
            except OSError:
                break
            try:
                mt, _seq, msg = proto.decode(data)
            except proto.ProtoError:
                continue
            if mt == MsgType.STATUS:
                self.status = msg
        s.settimeout(1.0)

    def cmd(self, name: str, params: dict, timeout: float = 30.0):
        self.pump()
        return safe_cmd(self.link, name, params, timeout=timeout)

    def health(self) -> dict:
        m = self.status
        if m is None:
            return {}
        return {"overruns": int(m.overruns), "underruns": int(m.underruns), "rx_gain_idx": int(m.rx_gain_idx),
                "tx_active": bool(m.flags & Status.FLAG_TX_ACTIVE),
                "rx_overload": bool(m.flags & Status.FLAG_RX_OVERLOAD),
                "rx_stale": bool(m.flags & Status.FLAG_RX_STALE), "fps": int(m.fps)}


class Inbox:
    def __init__(self):
        self.lock = threading.Lock()
        self.data = {}

    def __call__(self, stream, data):
        with self.lock:
            self.data.setdefault(stream, bytearray()).extend(data)

    def get(self, stream) -> bytes:
        with self.lock:
            return bytes(self.data.get(stream, b""))

    def clear(self):
        with self.lock:
            self.data.clear()


class Abort(Exception):
    pass


B200_TX_GAIN_CAP = 45.0          # dB, TX/RX into RX2 through 20 dB (RX2 takes at most -15 dBm)


def b200_init(radio, a, rx_gain: float, tx_gain: float):
    """Init the B200 the way bench/b200_comm_loopback does, then start RX."""
    tx_gain = min(float(tx_gain), B200_TX_GAIN_CAP)
    p = {"kind": "b200", "sample_rate": float(a.rate), "center_freq": float(a.center), "rx_gain": float(rx_gain),
         "tx_gain": tx_gain, "clock_source": a.clock, "rx_antenna": "RX2", "tx_antenna": "TX/RX",
         "extras": {"rf_bandwidth_hz": int(a.rate * 0.8), "tx_center_freq": float(a.center),
                    "dc_offset": True, "iq_balance": True},
         "psd_nfft": 4096, "psd_window": "hann", "psd_buffer": 65536}
    log(f"[init] B200 {a.rate/1e6:g} MS/s, {a.center/1e6:g} MHz, RX2 gain {rx_gain} dB, TX/RX gain {tx_gain} dB, "
        f"clock {a.clock}")
    t0 = time.time()
    ack = radio.cmd("init", p, timeout=120)
    dt = round(time.time() - t0, 2)
    if ack.status == CmdStatus.OK:
        radio.cmd("start_rx", {}, timeout=30)
    return ack, dt


def tx_block_s(a) -> float:
    """The modem's TX block length: a whole number of the card's TX blocks
    near 50 ms. The bridge pads every send up to a whole card block with
    zeros, so any other length puts a hole in the stream on every block."""
    if a.tx_block <= 0 or a.kind == "b200":
        return 0.05
    n = max(1, int(np.ceil(0.05 * a.rate / a.tx_block)))
    return n * a.tx_block / a.rate


def lan_ip_towards(host: str) -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 9))
        return s.getsockname()[0]
    finally:
        s.close()


def wait(pred, timeout: float, step: float = 0.2, guard=None) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if guard is not None:
            guard()
        if pred():
            return True
        time.sleep(step)
    return False


# one waveform
def run_mod(a, radio: Radio, cont: BitpipeContainer, inbox: Inbox, mod: str, laptop_ip: str) -> dict:
    rec = {"modulation": mod, "symbol_rate": a.baud, "deviation_hz": a.deviation, "offset_hz": a.offset,
           "amplitude": a.amplitude, "t_start": now_s(), "pass": False, "steps": {}}
    inbox.clear()
    params = {"modulation": mod, "symbol_rate": a.baud, "deviation_hz": a.deviation, "offset_hz": a.offset,
              "amplitude": a.amplitude, "block_s": tx_block_s(a)}
    if a.dial_in:
        params["container"] = f"tcp:{laptop_ip}:{a.bit_port}"
    else:
        params["listen"] = f"tcp:0.0.0.0:{a.bit_port}"
        params["allow_from"] = laptop_ip
    ack = radio.cmd("bitpipe_start", params)
    rec["bitpipe_start"] = {"status": CmdStatus(ack.status).name, "result": ack.result}
    if ack.status != CmdStatus.OK:
        rec["error"] = f"bitpipe_start {ack_err(ack)}"
        return rec
    if not wait(cont.connected, 15):
        if a.dial_in:
            why = (f"the radio's modem did not reach the container at {laptop_ip}:{a.bit_port} in 15 s "
                   f"(the laptop firewall drops new inbound connections without a rule for the port)")
        else:
            why = f"the container could not reach the radio's modem at {a.host}:{a.bit_port} in 15 s"
        rec["error"] = why + "; nothing was keyed"
        radio.cmd("bitpipe_stop", {})
        return rec
    log(f"{mod}: bit face up between the laptop container and the radio's modem "
        f"({cont.peer_hello.get('modem', '?')}, {'modem dialled in' if a.dial_in else 'container dialled out'})")
    if not a.arm:
        rec["note"] = "path only, not keyed (no --arm)"
        rec["pass"] = True
        radio.cmd("bitpipe_stop", {})
        return rec

    hist = []
    base = {"modem_underruns": 0, "card_underruns": None}

    def snapshot(tag=""):
        radio.pump(0.2)
        b = radio.cmd("bitpipe_status", {}, timeout=10)
        r = b.result if isinstance(b.result, dict) else {}
        cs = cont.status()
        q = cs.get("quality", {})
        snap = {"t": now_s(), "tag": tag, "tx_running": r.get("tx_running"), "modem": r.get("modem", {}),
                "radio": radio.health(), "locked": cs.get("locked"), "frames_rx": cs.get("frames_rx"),
                "frames_bad": cs["deframer"]["frames_bad"], "frames_tx": cs.get("frames_tx"),
                "retx": cs["tx"]["retx"], "data_tx": cs.get("data_tx"), "ber": q.get("ber"), "fer": q.get("fer"),
                "rtt_s": q.get("rtt_s"), "unacked": cs.get("unacked"), "pending": cs.get("pending_bytes")}
        hist.append(snap)
        return snap

    def guard():
        s = snapshot()
        m = s["modem"]
        u = int(m.get("underruns", 0) or 0)
        if u - base["modem_underruns"] >= 3:
            raise Abort(f"modem underrun burst ({u - base['modem_underruns']} in one poll)")
        base["modem_underruns"] = u
        cu = s["radio"].get("underruns")
        if cu is not None:
            if base["card_underruns"] is not None and cu - base["card_underruns"] >= 3:
                raise Abort(f"card underrun burst ({cu - base['card_underruns']})")
            base["card_underruns"] = cu
        if s["tx_running"] is False:
            raise Abort("TX stopped by itself (front end or send failures)")

    try:
        # the service's rate guard compares the radio rate with the last cached
        # waveform, which the stream never plays (the modem builds IQ at the
        # radio's own rate); services from before the guard learned that need
        # the override
        ack = radio.cmd("start_tx", {"mode": "CONTINUOUS", "allow_rate_mismatch": True}, timeout=30)
        rec["start_tx"] = {"status": CmdStatus(ack.status).name, "err": ack_err(ack) if ack.status else ""}
        if ack.status != CmdStatus.OK:
            raise Abort(f"start_tx refused: {ack_err(ack)}")
        t_key = time.time()
        lvl = f"TX gain {min(a.b200_tx_gain, B200_TX_GAIN_CAP)} dB" if a.kind == "b200" else f"atten {a.atten} dB"
        log(f"{mod}: TX keyed ({a.offset/1e3:g} kHz offset, amplitude {a.amplitude}, {lvl}, "
            f"TX block {tx_block_s(a) * a.rate:.0f} samples)")
        snapshot("keyed")
        base["modem_underruns"] = int(hist[-1]["modem"].get("underruns", 0) or 0)
        # 1. lock on the radio's own looped-back frames
        ok = wait(lambda: cont.status().get("locked") and cont.status().get("frames_rx", 0) >= 3, a.lock_s,
                  step=1.0, guard=guard)
        rec["steps"]["lock_s"] = round(time.time() - t_key, 1) if ok else None
        if not ok:
            raise Abort(f"no lock in {a.lock_s:g} s")
        log(f"{mod}: locked after {rec['steps']['lock_s']} s")
        # 2. chat lines out and back
        lines = [f"{mod} hw line {i} {now_s()}\n".encode() for i in range(a.lines)]
        t0 = time.time()
        for ln in lines:
            cont.send(st.STREAM_CHAT, ln)
        want = b"".join(lines)
        ok = wait(lambda: inbox.get(st.STREAM_CHAT) == want, a.chat_s, step=1.0, guard=guard)
        rec["steps"]["chat"] = {"ok": ok, "bytes": len(want), "got": len(inbox.get(st.STREAM_CHAT)),
                                "seconds": round(time.time() - t0, 1)}
        log(f"{mod}: chat {'PASS' if ok else 'FAIL'} {len(inbox.get(st.STREAM_CHAT))}/{len(want)} B in "
            f"{rec['steps']['chat']['seconds']} s")
        # 3. a file out and back
        data = np.random.default_rng(7).bytes(a.file_kb * 1024)
        t0 = time.time()
        cont.send(st.STREAM_FILE, st.file_record(f"hw_{mod}.bin", data))
        def file_back():
            got = st.FileReceiver().feed(inbox.get(st.STREAM_FILE))
            return bool(got) and got[0].ok and got[0].data == data
        ok = wait(file_back, a.file_s, step=1.0, guard=guard)
        rec["steps"]["file"] = {"ok": ok, "bytes": len(data), "got": len(inbox.get(st.STREAM_FILE)),
                                "seconds": round(time.time() - t0, 1),
                                "payload_bit_s": round(8 * len(data) / max(1e-3, time.time() - t0)) if ok else None}
        log(f"{mod}: file {'PASS' if ok else 'FAIL'} {a.file_kb} kB in {rec['steps']['file']['seconds']} s")
        # 4. soak: a line every few seconds, all must come back
        sent = bytearray()
        before = len(inbox.get(st.STREAM_CHAT))
        t0 = time.time()
        k = 0
        while time.time() - t0 < a.soak_s:
            ln = f"{mod} soak {k} {now_s()}\n".encode()
            cont.send(st.STREAM_CHAT, ln)
            sent += ln
            k += 1
            end = time.time() + a.soak_every
            while time.time() < end:
                guard()
                time.sleep(1.0)
        ok = wait(lambda: inbox.get(st.STREAM_CHAT)[before:] == bytes(sent), a.chat_s, step=1.0, guard=guard)
        got = len(inbox.get(st.STREAM_CHAT)) - before
        rec["steps"]["soak"] = {"ok": ok, "seconds": round(a.soak_s), "lines": k, "bytes": len(sent),
                                "undelivered": max(0, len(sent) - got)}
        log(f"{mod}: soak {'PASS' if ok else 'FAIL'} {k} lines, {max(0, len(sent) - got)} B undelivered")
        rec["pass"] = all(v.get("ok") for v in rec["steps"].values() if isinstance(v, dict))
    except Abort as e:
        rec["error"] = f"stopped: {e}"
        log(f"{mod}: STOP {e}")
    finally:
        snapshot("final")
        radio.cmd("stop_tx", {}, timeout=20)
        rec["tx_stopped_at"] = now_s()
        radio.cmd("bitpipe_stop", {}, timeout=20)
        rec["keyed_s"] = round(time.time() - t_key, 1) if "t_key" in locals() else 0
        log(f"{mod}: TX stopped, stream link detached")
    rec["final"] = hist[-1] if hist else {}
    rec["history"] = hist[::max(1, len(hist) // 60)]
    return rec


# two radios (HW-4)
def card_block_s(rate: float, card_block: int) -> float:
    """A G40 modem block: a whole number of card TX blocks near 50 ms (see tx_block_s)."""
    if card_block <= 0:
        return 0.05
    n = max(1, int(np.ceil(0.05 * rate / card_block)))
    return n * card_block / rate


class End:
    """One radio of the pair, its stream modem settings and the laptop
    container that dials out to that modem."""

    def __init__(self, label, radio, host, bit_port, block_s, rate, ctl_base, crypto=None, station=None):
        self.label, self.radio, self.host, self.bit_port = label, radio, host, bit_port
        self.block_s, self.rate = block_s, rate
        self.laptop_ip = lan_ip_towards(host)
        self.inbox = Inbox()
        cfg = BitpipeConfig(name=f"laptop-{label.lower()}", bit_port=bit_port, ctl_port=ctl_base,
                            app_base_port=ctl_base + 2, crypto="none" if crypto is None else "secure",
                            bit_connect=f"tcp:{host}:{bit_port}", station=station)
        self.cont = BitpipeContainer(cfg, crypto or NullCrypto(), on_deliver=self.inbox, app_sockets=False)

    def modem_params(self, a, mod) -> dict:
        return {"modulation": mod, "symbol_rate": a.baud, "deviation_hz": a.deviation, "offset_hz": a.offset,
                "amplitude": a.amplitude, "block_s": self.block_s,
                "listen": f"tcp:0.0.0.0:{self.bit_port}", "allow_from": self.laptop_ip}


def pair_crypto(a) -> tuple:
    """Clear, or one SecureContext per end from the same key file with sender
    ids 1 (the G40 end) and 2 (the B200 end). The containers run here, so the
    keys never leave the laptop; the radios carry sealed frames only."""
    if a.crypto != "secure":
        return None, None
    if not a.key_file:
        raise SystemExit("--crypto secure needs --key-file (keep the operator key file for operations)")
    os.environ.setdefault("DPMG_SECURE_MSG", "1")
    from dpmg.bitpipe.crypto import FileKeyProvider, SecureContextCrypto, context_from_provider
    return tuple(SecureContextCrypto(context_from_provider(FileKeyProvider(a.key_file), sid)) for sid in (1, 2))


def run_pair_mod(a, g: End, b: End, mod: str) -> dict:
    """One waveform over the two radio link. g's container talks to b's
    container: g's modem keys the G40, b's modem hears it on the B200, and
    the other way round. Every check runs both ways at once and is timed
    per direction."""
    dirs = {"G40 to B200": (g, b), "B200 to G40": (b, g)}
    rec = {"modulation": mod, "symbol_rate": a.baud, "deviation_hz": a.deviation, "offset_hz": a.offset,
           "amplitude": a.amplitude, "t_start": now_s(), "pass": False,
           "steps": {d: {} for d in dirs}}
    for e in (g, b):
        e.inbox.clear()
        ack = e.radio.cmd("bitpipe_start", e.modem_params(a, mod))
        rec[f"bitpipe_start_{e.label}"] = {"status": CmdStatus(ack.status).name, "result": ack.result}
        if ack.status != CmdStatus.OK:
            rec["error"] = f"bitpipe_start on the {e.label} {ack_err(ack)}"
            for x in (g, b):
                x.radio.cmd("bitpipe_stop", {})
            return rec
    if not wait(lambda: g.cont.connected() and b.cont.connected(), 15):
        down = [e.label for e in (g, b) if not e.cont.connected()]
        rec["error"] = f"no bit face to the {' and '.join(down)} modem in 15 s; nothing was keyed"
        for e in (g, b):
            e.radio.cmd("bitpipe_stop", {})
        return rec
    log(f"{mod}: both bit faces up (G40 modem at {g.host}:{g.bit_port}, B200 modem at {b.host}:{b.bit_port}, "
        f"containers dialled out)")
    if not a.arm:
        rec["note"] = "path only, not keyed (no --arm)"
        rec["pass"] = True
        for e in (g, b):
            e.radio.cmd("bitpipe_stop", {})
        return rec

    hist = []
    base = {e.label: {"modem": 0, "card": None} for e in (g, b)}
    paused = set()                       # radios whose TX the fault step has stopped on purpose

    def snapshot(tag=""):
        snap = {"t": now_s(), "tag": tag}
        for e in (g, b):
            e.radio.pump(0.2)
            r = e.radio.cmd("bitpipe_status", {}, timeout=10).result
            r = r if isinstance(r, dict) else {}
            cs = e.cont.status()
            q = cs.get("quality", {})
            snap[e.label] = {"tx_running": r.get("tx_running"), "modem": r.get("modem", {}),
                             "radio": e.radio.health(), "locked": cs.get("locked"),
                             "frames_rx": cs.get("frames_rx"), "frames_bad": cs["deframer"]["frames_bad"],
                             "frames_tx": cs.get("frames_tx"), "retx": cs["tx"]["retx"], "ber": q.get("ber"),
                             "fer": q.get("fer"), "rtt_s": q.get("rtt_s"), "unacked": cs.get("unacked"),
                             "verdicts": dict((cs.get("crypto") or {}).get("verdicts") or {}),
                             "own_rx": cs.get("own_rx"), "peer_age_s": cs.get("peer_age_s")}
        hist.append(snap)
        return snap

    def guard():
        s = snapshot()
        for e in (g, b):
            m, bs = s[e.label], base[e.label]
            u = int(m["modem"].get("underruns", 0) or 0)
            if e.label in paused:
                bs["modem"], bs["card"] = u, m["radio"].get("underruns", bs["card"])
                continue
            if u - bs["modem"] >= 3:
                raise Abort(f"{e.label} modem underrun burst ({u - bs['modem']} in one poll)")
            bs["modem"] = u
            cu = m["radio"].get("underruns")
            if cu is not None:
                if bs["card"] is not None and cu - bs["card"] >= 3:
                    raise Abort(f"{e.label} card underrun burst ({cu - bs['card']})")
                bs["card"] = cu
            if m["tx_running"] is False:
                raise Abort(f"{e.label} TX stopped by itself (front end or send failures)")

    def wait_each(preds: dict, timeout: float) -> dict:
        """Poll every direction's condition until all hold or time runs out;
        seconds to each, None for the ones that never held."""
        t0, done = time.time(), {}
        while time.time() - t0 < timeout:
            guard()
            for d, p in preds.items():
                if d not in done and p():
                    done[d] = round(time.time() - t0, 1)
            if len(done) == len(preds):
                break
            time.sleep(1.0)
        return {d: done.get(d) for d in preds}

    start = {}
    try:
        for e in (g, b):
            ack = e.radio.cmd("start_tx", {"mode": "CONTINUOUS", "allow_rate_mismatch": True}, timeout=30)
            rec[f"start_tx_{e.label}"] = {"status": CmdStatus(ack.status).name,
                                          "err": ack_err(ack) if ack.status else ""}
            if ack.status != CmdStatus.OK:
                raise Abort(f"start_tx refused on the {e.label}: {ack_err(ack)}")
        t_key = time.time()
        log(f"{mod}: both keyed ({a.offset/1e3:g} kHz offset, amplitude {a.amplitude}; G40 atten {a.atten} dB "
            f"at {g.rate/1e6:g} MS/s, block {g.block_s * g.rate:.0f} samples; B200 TX gain "
            f"{min(a.b200_tx_gain, B200_TX_GAIN_CAP)} dB at {b.rate/1e6:g} MS/s)")
        s0 = snapshot("keyed")
        for e in (g, b):
            base[e.label]["modem"] = int(s0[e.label]["modem"].get("underruns", 0) or 0)
            start[e.label] = s0[e.label]
        # 1. each receiving container locks on the other radio's stream
        lk = wait_each({d: (lambda rx=rx: rx.cont.status().get("locked") and rx.cont.status().get("frames_rx", 0) >= 3)
                        for d, (_tx, rx) in dirs.items()}, a.lock_s)
        for d in dirs:
            rec["steps"][d]["lock_s"] = lk[d]
        log(f"{mod}: lock " + ", ".join(f"{d} {lk[d]} s" for d in dirs))
        if any(v is None for v in lk.values()):
            raise Abort("no lock in " + " and ".join(d for d, v in lk.items() if v is None))
        # 2. chat lines each way
        want = {}
        for d, (tx, rx) in dirs.items():
            lines = [f"{mod} {d} line {i} {now_s()}\n".encode() for i in range(a.lines)]
            for ln in lines:
                tx.cont.send(st.STREAM_CHAT, ln)
            want[d] = b"".join(lines)
        ch = wait_each({d: (lambda d=d, rx=rx: rx.inbox.get(st.STREAM_CHAT) == want[d])
                        for d, (_tx, rx) in dirs.items()}, a.chat_s)
        for d, (_tx, rx) in dirs.items():
            rec["steps"][d]["chat"] = {"ok": ch[d] is not None, "bytes": len(want[d]),
                                       "got": len(rx.inbox.get(st.STREAM_CHAT)), "seconds": ch[d]}
        log(f"{mod}: chat " + ", ".join(f"{d} {'PASS' if ch[d] is not None else 'FAIL'} {ch[d]} s" for d in dirs))
        # 3. a file each way
        data = {}
        for k, (d, (tx, _rx)) in enumerate(dirs.items()):
            data[d] = np.random.default_rng(11 + k).bytes(a.file_kb * 1024)
            tx.cont.send(st.STREAM_FILE, st.file_record(f"hw4_{mod}_{k}.bin", data[d]))

        def file_in(d, rx):
            got = st.FileReceiver().feed(rx.inbox.get(st.STREAM_FILE))
            return bool(got) and got[0].ok and got[0].data == data[d]
        fl = wait_each({d: (lambda d=d, rx=rx: file_in(d, rx)) for d, (_tx, rx) in dirs.items()}, a.file_s)
        for d, (_tx, rx) in dirs.items():
            rec["steps"][d]["file"] = {"ok": fl[d] is not None, "bytes": len(data[d]),
                                       "got": len(rx.inbox.get(st.STREAM_FILE)), "seconds": fl[d],
                                       "payload_bit_s": round(8 * len(data[d]) / max(1e-3, fl[d])) if fl[d] else None}
        log(f"{mod}: file " + ", ".join(f"{d} {'PASS' if fl[d] is not None else 'FAIL'} {fl[d]} s" for d in dirs))
        # 4. soak: a line each way every few seconds, all must arrive
        sent = {d: bytearray() for d in dirs}
        before = {d: len(rx.inbox.get(st.STREAM_CHAT)) for d, (_tx, rx) in dirs.items()}
        t0, k = time.time(), 0
        while time.time() - t0 < a.soak_s:
            for d, (tx, _rx) in dirs.items():
                ln = f"{mod} {d} soak {k} {now_s()}\n".encode()
                tx.cont.send(st.STREAM_CHAT, ln)
                sent[d] += ln
            k += 1
            end = time.time() + a.soak_every
            while time.time() < end:
                guard()
                time.sleep(1.0)
        sk = wait_each({d: (lambda d=d, rx=rx: rx.inbox.get(st.STREAM_CHAT)[before[d]:] == bytes(sent[d]))
                        for d, (_tx, rx) in dirs.items()}, a.chat_s)
        for d, (_tx, rx) in dirs.items():
            got = len(rx.inbox.get(st.STREAM_CHAT)) - before[d]
            rec["steps"][d]["soak"] = {"ok": sk[d] is not None, "seconds": round(a.soak_s), "lines": k,
                                       "bytes": len(sent[d]), "undelivered": max(0, len(sent[d]) - got)}
        log(f"{mod}: soak " + ", ".join(f"{d} {'PASS' if sk[d] is not None else 'FAIL'} "
                                        f"{rec['steps'][d]['soak']['undelivered']} B undelivered" for d in dirs))
        # 5. fault: one radio stops transmitting for a while in the middle of a
        # file each way and a chat line each way every second. The container
        # that hears that radio must lose lock, re-lock once TX is back, and
        # every byte must then arrive once and in order (the ARQ catches up).
        if a.fault_s > 0:
            fr = g if a.fault_radio == "G40" else b
            deaf = b if fr is g else g
            f = {"radio": fr.label, "seconds": a.fault_s, "file_bytes": a.fault_file_kb * 1024,
                 "down_after_s": a.down_after_s, "down_detected_s": None, "lock_lost": False,
                 "link_down_seen": False, "max_unacked": 0, "peer_back_s": None}
            rec["fault"] = f
            fdata = {}
            for k, (d, (tx, _rx)) in enumerate(dirs.items()):
                fdata[d] = np.random.default_rng(31 + k).bytes(a.fault_file_kb * 1024)
                tx.cont.send(st.STREAM_FILE, st.file_record(f"hw4_fault_{mod}_{k}.bin", fdata[d]))
            fsent = {d: bytearray() for d in dirs}
            cbefore = {d: len(rx.inbox.get(st.STREAM_CHAT)) for d, (_tx, rx) in dirs.items()}
            nline = [0]

            def line_each_way():
                for d, (tx, _rx) in dirs.items():
                    ln = f"{mod} {d} fault line {nline[0]} {now_s()}\n".encode()
                    tx.cont.send(st.STREAM_CHAT, ln)
                    fsent[d] += ln
                nline[0] += 1

            end = time.time() + 4.0                    # the transfers get going first
            while time.time() < end:
                line_each_way()
                guard()
                time.sleep(0.5)
            retx0 = {e.label: e.cont.status()["tx"]["retx"] for e in (g, b)}
            own0 = deaf.cont.status().get("own_rx") or 0
            paused.add(fr.label)
            fr.radio.cmd("stop_tx", {}, timeout=20)
            t_off = time.time()
            f["tx_off_at"] = now_s()
            log(f"{mod}: fault, {fr.label} TX off for {a.fault_s:g} s")
            while time.time() - t_off < a.fault_s:
                line_each_way()
                guard()
                cs = deaf.cont.status()
                age = cs.get("peer_age_s")
                f["lock_lost"] = f["lock_lost"] or not cs.get("locked")
                # down: nothing from the peer for down_after_s (the deaf end may
                # stay locked on its own leaked TX, which it drops)
                if f["down_detected_s"] is None and age is not None and age >= a.down_after_s:
                    f["down_detected_s"] = round(time.time() - t_off, 1)
                for e in (g, b):
                    es = e.cont.status()
                    f["link_down_seen"] = f["link_down_seen"] or bool(es.get("link_down"))
                    f["max_unacked"] = max(f["max_unacked"], int(es.get("unacked") or 0))
                time.sleep(0.5)
            ack = fr.radio.cmd("start_tx", {"mode": "CONTINUOUS", "allow_rate_mismatch": True}, timeout=30)
            t_on = time.time()
            f["tx_on_at"] = now_s()
            f["restart"] = CmdStatus(ack.status).name
            if ack.status != CmdStatus.OK:
                raise Abort(f"start_tx after the fault refused on the {fr.label}: {ack_err(ack)}")
            paused.discard(fr.label)
            ok = wait(lambda: (deaf.cont.status().get("peer_age_s") or 99) < 1.0, 20, step=0.25, guard=guard)
            f["peer_back_s"] = round(time.time() - t_on, 1) if ok else None
            log(f"{mod}: fault, {deaf.label} end reported the peer down {f['down_detected_s']} s after TX off "
                f"(lock {'lost' if f['lock_lost'] else 'held on its own leaked TX'}), heard the peer again "
                f"{f['peer_back_s']} s after TX back")

            def file_in(d, rx):
                return any(x.ok and x.data == fdata[d] for x in st.FileReceiver().feed(rx.inbox.get(st.STREAM_FILE)))
            dv = wait_each({d: (lambda d=d, rx=rx: file_in(d, rx) and
                                rx.inbox.get(st.STREAM_CHAT)[cbefore[d]:] == bytes(fsent[d]))
                            for d, (_tx, rx) in dirs.items()}, a.file_s)
            f["delivered_after_tx_back_s"] = dv
            f["lines"] = nline[0]
            f["undelivered"] = {d: max(0, len(fsent[d]) - (len(rx.inbox.get(st.STREAM_CHAT)) - cbefore[d]))
                                for d, (_tx, rx) in dirs.items()}
            f["files_ok"] = {d: file_in(d, rx) for d, (_tx, rx) in dirs.items()}
            f["retx"] = {e.label: e.cont.status()["tx"]["retx"] - retx0[e.label] for e in (g, b)}
            f["own_frames_dropped"] = (deaf.cont.status().get("own_rx") or 0) - own0
            f["ok"] = (f["down_detected_s"] is not None and f["peer_back_s"] is not None
                       and all(v is not None for v in dv.values())
                       and not any(f["undelivered"].values()))
            log(f"{mod}: fault {'PASS' if f['ok'] else 'FAIL'}: files and {nline[0]} lines each way in "
                + ", ".join(f"{d} {dv[d]} s" for d in dirs) + " after TX came back")
        rec["pass"] = all(v.get("ok") for d in dirs for v in rec["steps"][d].values() if isinstance(v, dict)) \
            and rec.get("fault", {}).get("ok", True)
    except Abort as e:
        rec["error"] = f"stopped: {e}"
        log(f"{mod}: STOP {e}")
    finally:
        last = snapshot("final")
        for e in (g, b):
            e.radio.cmd("stop_tx", {}, timeout=20)
        for e in (g, b):
            e.radio.cmd("bitpipe_stop", {}, timeout=20)
        rec["tx_stopped_at"] = now_s()
        rec["keyed_s"] = round(time.time() - t_key, 1) if "t_key" in locals() else 0
        log(f"{mod}: TX stopped on both, stream links detached")
    # per direction figures: the receiving container and modem, the sending modem and card
    rec["per_direction"] = {}
    for d, (tx, rx) in dirs.items():
        R, T = last[rx.label], last[tx.label]
        # the containers live across waveforms, so their counters are taken
        # from the moment this waveform was keyed
        k0r = start.get(rx.label, {})
        s0t, s0r = start.get(tx.label, {}).get("radio", {}), k0r.get("radio", {})
        rec["per_direction"][d] = {
            "locked": R["locked"], "frames_rx": (R["frames_rx"] or 0) - (k0r.get("frames_rx") or 0),
            "frames_bad": (R["frames_bad"] or 0) - (k0r.get("frames_bad") or 0),
            "ber": R["ber"], "fer": R["fer"], "rtt_s": T["rtt_s"], "retx": T["retx"],
            "cfo_hz": R["modem"].get("cfo_hz"), "eye": R["modem"].get("eye"),
            "tx_modem_underruns": int(T["modem"].get("underruns", 0) or 0),
            "tx_card_underruns": (None if T["radio"].get("underruns") is None
                                  else T["radio"]["underruns"] - (s0t.get("underruns") or 0)),
            "rx_card_overruns": (None if R["radio"].get("overruns") is None
                                 else R["radio"]["overruns"] - (s0r.get("overruns") or 0)),
            "verdicts": {k: v - (k0r.get("verdicts") or {}).get(k, 0) for k, v in (R.get("verdicts") or {}).items()
                         if v - (k0r.get("verdicts") or {}).get(k, 0)}}
    rec["final"] = last
    rec["history"] = hist[::max(1, len(hist) // 60)]
    return rec


def confirm_idle(g: End, b: End) -> dict:
    """After the run: the G40 inited but not streaming (state 1, RX frames
    not rising), the B200 closed (state 0), neither transmitting."""
    out = {}
    for e in (g, b):
        e.radio.pump(1.5)
        c = e.radio.cmd("get_config", {}, timeout=15).result or {}
        f0 = e.radio.status.rx_frames if e.radio.status is not None else None
        e.radio.pump(2.5)
        f1 = e.radio.status.rx_frames if e.radio.status is not None else None
        out[e.label] = {"state": c.get("state"), "tx_active": c.get("tx_active"),
                        "rx_frames_rising": (None if f0 is None or f1 is None else f1 > f0),
                        "health": e.radio.health()}
    return out


def write_pair_report(a, report: dict) -> tuple:
    os.makedirs(a.out, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    jp = os.path.join(a.out, f"bitpipe_hw4_{stamp}.json")
    mp = os.path.join(a.out, f"bitpipe_hw4_{stamp}.md")
    with open(jp, "w") as f:
        json.dump(report, f, indent=1, default=str)

    def fmt(x, p="{:.1e}"):
        return "-" if x is None else p.format(x)
    lines = [f"# Bitpipe two radio run {stamp}", "",
             f"G40 {report.get('g40_serial')} at {a.g40_host}: TX {a.g40_tx_port} attenuation {a.atten} dB, "
             f"RX {a.g40_rx_port} gain {a.gain}, {a.g40_rate/1e6:g} MS/s. "
             f"B200 {report.get('b200_serial')} at {a.b200_host}: TX TX/RX gain "
             f"{min(a.b200_tx_gain, B200_TX_GAIN_CAP)} dB, RX RX2 gain {a.b200_rx_gain} dB, {a.b200_rate/1e6:g} MS/s. "
             f"{a.center/1e6:g} MHz, internal clocks, {a.baud:g} Bd, offset {a.offset/1e3:g} kHz, "
             f"amplitude {a.amplitude}. One container per radio on the laptop, each dialling out, "
             + ("clear." if a.crypto != "secure" else
                "encrypted (AES-256-GCM-SIV, test key file on the laptop, sender ids 1 and 2)."), "",
             "| Waveform | Direction | Result | Lock s | Chat | File | Soak undelivered | Frames heard | Frames bad "
             "| BER before FEC | FER | Round trip | CFO Hz | Eye | TX modem underruns | TX card underruns | RX card overruns "
             "| Decrypt verdicts |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in report.get("runs", []):
        for d, pd in (r.get("per_direction") or {}).items():
            s = r["steps"].get(d, {})
            ok = all(v.get("ok") for v in s.values() if isinstance(v, dict)) and s.get("lock_s") is not None
            lines.append(
                f"| {r['modulation']} | {d} | {'PASS' if ok and not r.get('error') else 'FAIL'}"
                f"{(': ' + r['error']) if r.get('error') else ''} | {s.get('lock_s', '-')} "
                f"| {'ok' if s.get('chat', {}).get('ok') else '-'} {s.get('chat', {}).get('seconds', '')} s "
                f"| {'ok' if s.get('file', {}).get('ok') else '-'} {s.get('file', {}).get('seconds', '')} s "
                f"| {s.get('soak', {}).get('undelivered', '-')} | {pd.get('frames_rx')} | {pd.get('frames_bad')} "
                f"| {fmt(pd.get('ber'))} | {fmt(pd.get('fer'))} | {fmt(pd.get('rtt_s'), '{:.2f} s')} "
                f"| {pd.get('cfo_hz', '-')} | {fmt(pd.get('eye'), '{:.3g}')} | {pd.get('tx_modem_underruns')} "
                f"| {pd.get('tx_card_underruns')} | {pd.get('rx_card_overruns')} "
                f"| {', '.join(f'{k} {v}' for k, v in sorted((pd.get('verdicts') or {}).items())) or '-'} |")
        if not r.get("per_direction"):
            lines.append(f"| {r['modulation']} | both | {'PASS' if r.get('pass') else 'FAIL'}"
                         f"{(': ' + r.get('error', '')) if r.get('error') else ''} |" + " - |" * 15)
    for r in report.get("runs", []):
        f = r.get("fault")
        if f:
            dv = f.get("delivered_after_tx_back_s") or {}
            lines += ["", f"Fault, {r['modulation']}: {f['radio']} TX off for {f['seconds']:g} s "
                      f"({f.get('tx_off_at')} to {f.get('tx_on_at')}) in the middle of a "
                      f"{f['file_bytes'] // 1024} kB file each way and a chat line each way every second "
                      f"({f.get('lines')} lines). The other end reported the peer down "
                      f"{f.get('down_detected_s')} s after TX went off (no peer frame for {f.get('down_after_s')} s; "
                      f"lock {'lost' if f.get('lock_lost') else 'held on its own leaked TX'}, "
                      f"{f.get('own_frames_dropped')} own frames dropped) and heard it again {f.get('peer_back_s')} s "
                      f"after TX came back. At most {f.get('max_unacked')} frames unacknowledged, retransmits "
                      f"{f.get('retx')}. Everything delivered "
                      + ", ".join(f"{d} {v} s" for d, v in dv.items())
                      + f" after TX came back; undelivered {f.get('undelivered')}. "
                      + ("PASS" if f.get("ok") else "FAIL")]
    if report.get("idle"):
        lines += ["", "After the run: " + "; ".join(
            f"{k} state {v.get('state')}, TX {'on' if v.get('tx_active') else 'off'}, RX frames "
            f"{'rising' if v.get('rx_frames_rising') else 'not rising'}" for k, v in report["idle"].items())]
    with open(mp, "w") as f:
        f.write("\n".join(lines) + "\n")
    return jp, mp


def main_pair(a) -> int:
    g_radio = Radio(a.g40_host, a.g40_port)
    b_radio = Radio(a.b200_host, a.b200_port)
    log(f"connected to {g_radio.link.serial} at {a.g40_host}:{a.g40_port} and "
        f"{b_radio.link.serial} at {a.b200_host}:{a.b200_port}")
    cg, cb = pair_crypto(a)
    # station 0 and 1: each end drops its own frames, which its radio's RX
    # hears whenever the other radio is silent
    g = End("G40", g_radio, a.g40_host, a.g40_bit_port, card_block_s(a.g40_rate, a.tx_block), a.g40_rate, 47898, cg,
            station=0)
    b = End("B200", b_radio, a.b200_host, a.b200_bit_port, 0.05, a.b200_rate, 47998, cb, station=1)
    report = {"g40_serial": g_radio.link.serial, "b200_serial": b_radio.link.serial, "action": a.action,
              "args": vars(a), "runs": []}
    for e in (g, b):
        e.cont.start()
    log(f"containers up, dialling the G40 modem at {a.g40_host}:{a.g40_bit_port} and the B200 modem at "
        f"{a.b200_host}:{a.b200_bit_port}")
    rc = 1
    try:
        ack, dt = init_ports(g_radio.link, a.g40_rx_port, a.g40_tx_port, a.g40_rate, a.center, a.gain, a.atten,
                             clock="internal")
        report["init_G40"] = {"status": CmdStatus(ack.status).name, "seconds": dt, "err": ack_err(ack) if ack.status else ""}
        if ack.status != CmdStatus.OK:
            log(f"G40 init failed: {ack_err(ack)}")
            return 1
        bns = argparse.Namespace(rate=a.b200_rate, center=a.center, clock="internal")
        ack, dt = b200_init(b_radio, bns, a.b200_rx_gain, a.b200_tx_gain)
        report["init_B200"] = {"status": CmdStatus(ack.status).name, "seconds": dt, "err": ack_err(ack) if ack.status else ""}
        if ack.status != CmdStatus.OK:
            log(f"B200 init failed: {ack_err(ack)}")
            return 1
        for e in (g, b):
            e.radio.pump(2.0)
        report["health_after_init"] = {"G40": g_radio.health(), "B200": b_radio.health()}
        for mod in a.mods:
            r = run_pair_mod(a, g, b, mod)
            report["runs"].append(r)
            err = r.get("error") or ""
            if "no bit face" in err or "underrun" in err or "TX stopped by itself" in err:
                log("stopping the sequence")
                break
        rc = 0 if report["runs"] and all(r.get("pass") for r in report["runs"]) else 1
    finally:
        for e in (g, b):
            e.radio.cmd("stop_tx", {}, timeout=20)
            e.radio.cmd("bitpipe_stop", {}, timeout=20)
            e.radio.cmd("stop_rx", {}, timeout=20)
        b_radio.cmd("close", {}, timeout=30)          # the B200 is left closed, the G40 inited and idle
        report["idle"] = confirm_idle(g, b)
        for e in (g, b):
            e.cont.stop()
        jp, mp = write_pair_report(a, report)
        log(f"TX and RX stopped on both, B200 closed: {report['idle']}. Report: {mp}")
    return rc


# report
def write_report(a, report: dict) -> tuple:
    os.makedirs(a.out, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    jp = os.path.join(a.out, f"bitpipe_hw_{stamp}.json")
    mp = os.path.join(a.out, f"bitpipe_hw_{stamp}.md")
    with open(jp, "w") as f:
        json.dump(report, f, indent=1, default=str)
    where = (f"RX RX2 gain {a.b200_rx_gain} dB, TX TX/RX gain {min(a.b200_tx_gain, B200_TX_GAIN_CAP)} dB"
             if a.kind == "b200" else f"RX {a.rx_port}, TX {a.tx_port}, TX attenuation {a.atten} dB, RX gain {a.gain}")
    lines = [f"# Bitpipe hardware run {stamp}", "",
             f"Radio {report.get('serial')} at {a.host}. {where}, {a.rate/1e6:g} MS/s, "
             f"{a.center/1e6:g} MHz, clock {a.clock}. "
             f"Container on the laptop ({report.get('laptop_ip')}:{a.bit_port}), clear.", "",
             "| Waveform | Result | Lock s | Chat | File | Soak undelivered | BER before FEC | FER | Round trip | CFO Hz | Eye | Modem underruns | Card over/under |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in report.get("runs", []):
        f = r.get("final", {})
        m = f.get("modem", {})
        rd = f.get("radio", {})
        s = r.get("steps", {})
        def fmt(x, p="{:.1e}"):
            return "-" if x is None else p.format(x)
        lines.append(f"| {r['modulation']} | {'PASS' if r.get('pass') else 'FAIL'}{(': ' + r['error']) if r.get('error') else ''} "
                     f"| {s.get('lock_s', '-')} | {'ok' if s.get('chat', {}).get('ok') else '-'} {s.get('chat', {}).get('seconds', '')} s "
                     f"| {'ok' if s.get('file', {}).get('ok') else '-'} {s.get('file', {}).get('seconds', '')} s "
                     f"| {s.get('soak', {}).get('undelivered', '-')} | {fmt(f.get('ber'))} | {fmt(f.get('fer'))} "
                     f"| {fmt(f.get('rtt_s'), '{:.2f} s')} | {m.get('cfo_hz', '-')} | {m.get('eye', '-')} "
                     f"| {m.get('underruns', '-')} | {rd.get('overruns', '-')}/{rd.get('underruns', '-')} |")
    if report.get("restore"):
        lines += ["", f"Restore: {report['restore']}"]
    with open(mp, "w") as f:
        f.write("\n".join(lines) + "\n")
    return jp, mp


# main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="bitpipe on a real radio (HW-1 / HW-2)")
    ap.add_argument("action", choices=("probe", "path", "run", "restore", "pair"))
    ap.add_argument("--kind", default="g40", choices=("g40", "b200"))
    ap.add_argument("--host", default=os.environ.get("DPMG_G40_HOST"), required=not os.environ.get("DPMG_G40_HOST"))
    ap.add_argument("--port", type=int, default=47810)
    ap.add_argument("--arm", action="store_true", help="allow TX (run only)")
    ap.add_argument("--rx-port", default="TRx1A")
    ap.add_argument("--tx-port", default="TRx1B")
    ap.add_argument("--rate", type=float, default=1.92e6)
    ap.add_argument("--center", type=float, default=915e6)
    ap.add_argument("--clock", default="internal")
    ap.add_argument("--atten", type=float, default=30.0, help="TX attenuation, dB (at least 30 on Setup A)")
    ap.add_argument("--gain", type=int, default=250, help="RX gain index (250 = checkout and demo bring-up)")
    ap.add_argument("--b200-rx-gain", type=float, default=30.0, help="B200 RX gain, dB")
    ap.add_argument("--b200-tx-gain", type=float, default=40.0, help=f"B200 TX gain, dB (at most {B200_TX_GAIN_CAP:g})")
    ap.add_argument("--mods", nargs="+", default=["FSK"])
    ap.add_argument("--baud", type=float, default=2400.0)
    ap.add_argument("--deviation", type=float, default=1200.0)
    ap.add_argument("--offset", type=float, default=200e3)
    ap.add_argument("--amplitude", type=float, default=0.3)
    ap.add_argument("--tx-block", type=int, default=8188,
                    help="the card's TX block in samples (G40 single mode below 20 MS/s: 8188); the modem's "
                         "blocks are a whole number of these so the bridge never zero pads; 0 = plain 50 ms")
    ap.add_argument("--bit-port", type=int, default=47899)
    ap.add_argument("--dial-in", action="store_true",
                    help="the radio's modem connects to the laptop (needs an inbound firewall rule); "
                         "default: the laptop container dials out to the modem")
    ap.add_argument("--lines", type=int, default=5)
    ap.add_argument("--file-kb", type=int, default=3)
    ap.add_argument("--lock-s", type=float, default=20.0)
    ap.add_argument("--chat-s", type=float, default=45.0)
    ap.add_argument("--file-s", type=float, default=150.0)
    ap.add_argument("--soak-s", type=float, default=120.0)
    ap.add_argument("--soak-every", type=float, default=5.0)
    ap.add_argument("--out", default=OUT_DEFAULT)
    pr = ap.add_argument_group("pair: the G40 and the B200 cabled to each other (HW-4)")
    pr.add_argument("--g40-host", default=os.environ.get("DPMG_G40_HOST"))
    pr.add_argument("--g40-port", type=int, default=47810)
    pr.add_argument("--g40-bit-port", type=int, default=47899)
    pr.add_argument("--g40-rx-port", default="TRx1B")
    pr.add_argument("--g40-tx-port", default="TRx1A")
    pr.add_argument("--g40-rate", type=float, default=1.92e6,
                    help="the stream modem runs at this rate; it does not keep up at 30.72 MS/s")
    pr.add_argument("--b200-host", default="192.168.55.1")
    pr.add_argument("--b200-port", type=int, default=47812)
    pr.add_argument("--b200-bit-port", type=int, default=47899)
    pr.add_argument("--b200-rate", type=float, default=2e6)
    pr.add_argument("--fault-s", type=float, default=0.0,
                    help="after the soak, stop one radio's TX this long in the middle of a file each way (BP-33)")
    pr.add_argument("--fault-radio", default="B200", choices=("B200", "G40"))
    pr.add_argument("--fault-file-kb", type=int, default=6)
    pr.add_argument("--down-after-s", type=float, default=2.0,
                    help="the fault step counts the link down after this long with no frame from the peer")
    pr.add_argument("--crypto", default="none", choices=("none", "secure"),
                    help="secure: both containers seal with AES-256-GCM-SIV from --key-file")
    pr.add_argument("--key-file", default=None,
                    help="key file for --crypto secure (a test key file, mode 0600, on this laptop)")
    a = ap.parse_args(argv)
    if a.kind == "g40" and a.action == "run" and a.atten < 30 and a.tx_port:
        raise SystemExit("TX attenuation below 30 dB refused on this bench")
    if a.kind == "b200" and a.b200_tx_gain > B200_TX_GAIN_CAP:
        raise SystemExit(f"B200 TX gain above {B200_TX_GAIN_CAP:g} dB refused (20 dB pad into RX2)")
    if a.kind == "b200" and a.rate == 1.92e6:
        a.rate = 2e6                                  # the B200 demo rate
    if a.amplitude > 0.5:
        raise SystemExit("amplitude above 0.5 refused")
    if a.action == "pair":
        if not a.g40_host:
            ap.error("pair needs --g40-host (or DPMG_G40_HOST)")
        if a.atten < 30:
            raise SystemExit("G40 TX attenuation below 30 dB refused on this bench")
        if a.b200_tx_gain > B200_TX_GAIN_CAP:
            raise SystemExit(f"B200 TX gain above {B200_TX_GAIN_CAP:g} dB refused")
        return main_pair(a)

    radio = Radio(a.host, a.port)
    report = {"serial": radio.link.serial, "host": a.host, "action": a.action, "args": vars(a), "runs": []}
    log(f"connected to {radio.link.serial} at {a.host}:{a.port}")

    if a.action == "probe":
        g = radio.cmd("get_config", {})
        b = radio.cmd("bitpipe_status", {})
        radio.pump(1.5)
        print(json.dumps({"get_config": g.result, "bitpipe_status": b.result, "health": radio.health()},
                         indent=1, default=str)[:3000])
        return 0

    if a.action == "restore":
        tx = None if a.tx_port in ("", "none", "-") else a.tx_port
        if a.kind == "b200":
            ack, dt = b200_init(radio, a, a.b200_rx_gain, a.b200_tx_gain)
        else:
            ack, dt = init_ports(radio.link, a.rx_port, tx, a.rate, a.center, a.gain, a.atten, clock=a.clock)
        g = radio.cmd("get_config", {})
        r = g.result or {}
        radio.pump(2.0)
        print(json.dumps({k: r.get(k) for k in ("inited", "state", "sample_rate", "center_hz", "rx_antenna",
                                                "tx_antenna", "card", "tx_card", "clock_source", "rx_gain",
                                                "tx_gain", "tx_active")}, indent=1, default=str))
        log(f"restore {'OK' if ack.status == CmdStatus.OK else 'FAILED ' + ack_err(ack)}; health {radio.health()}")
        return 0 if ack.status == CmdStatus.OK else 1

    laptop_ip = lan_ip_towards(a.host)
    report["laptop_ip"] = laptop_ip
    inbox = Inbox()
    if a.dial_in:
        cfg = BitpipeConfig(name="laptop-hw", bit_host="0.0.0.0", bit_port=a.bit_port, ctl_port=a.bit_port - 1,
                            app_base_port=a.bit_port + 1, crypto="none")
    else:
        cfg = BitpipeConfig(name="laptop-hw", bit_port=a.bit_port, ctl_port=a.bit_port - 1,
                            app_base_port=a.bit_port + 1, crypto="none", bit_connect=f"tcp:{a.host}:{a.bit_port}")
    cont = BitpipeContainer(cfg, NullCrypto(), on_deliver=inbox, app_sockets=False)
    cont.start()
    log(f"container up: " + (f"bit face 0.0.0.0:{a.bit_port}, the radio dials {laptop_ip}:{a.bit_port}"
                             if a.dial_in else f"dials out to the radio's modem at {a.host}:{a.bit_port}"))
    rc = 1
    try:
        if a.kind == "b200":
            ack, dt = b200_init(radio, a, a.b200_rx_gain, a.b200_tx_gain)
        else:
            ack, dt = init_ports(radio.link, a.rx_port, a.tx_port, a.rate, a.center, a.gain, a.atten, clock=a.clock)
        report["init"] = {"status": CmdStatus(ack.status).name, "seconds": dt, "err": ack_err(ack) if ack.status else ""}
        if ack.status != CmdStatus.OK:
            log(f"init failed: {ack_err(ack)}")
            return 1
        radio.pump(2.0)
        report["health_after_init"] = radio.health()
        for mod in a.mods:
            if a.action == "path":
                r = run_mod(argparse.Namespace(**{**vars(a), "arm": False}), radio, cont, inbox, mod, laptop_ip)
            else:
                r = run_mod(a, radio, cont, inbox, mod, laptop_ip)
            report["runs"].append(r)
            if r.get("error") and "did not reach" in r["error"]:
                break
            if r.get("error") and r["error"].startswith("stopped: modem underrun") or \
                    (r.get("error") or "").startswith("stopped: card underrun") or \
                    "TX stopped by itself" in (r.get("error") or ""):
                log("stopping the sequence after a TX fault")
                break
        rc = 0 if report["runs"] and all(r.get("pass") for r in report["runs"]) else 1
    finally:
        radio.cmd("stop_tx", {}, timeout=20)
        radio.cmd("bitpipe_stop", {}, timeout=20)
        radio.cmd("stop_rx", {}, timeout=20)
        radio.pump(1.5)
        report["health_end"] = radio.health()
        cont.stop()
        jp, mp = write_report(a, report)
        log(f"TX stopped, stream detached, RX stopped. Report: {mp}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
