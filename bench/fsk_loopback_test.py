#!/usr/bin/env python3
"""
FSK/4FSK RX+TX loopback test for one G40.

Builds FSK/4FSK scenarios on the radio, transmits them into an RF loopback
cable, and checks the channelizer decodes them back. Two cabling options (see
the `instructions` phase): same-card (RX+TX on one NV100) or cross-card (RX on
card0, TX on card1, so the LOs are independent). Run `instructions` for the
wiring.

Phases (default `all`): library, sweep, latency, exchange, dense, cleanup. The
per-case sweep/latency/exchange run at 1.92 Msps; the dense 8-emitter scenario
runs at 30.72 Msps. Scenarios are left on the radio for GUI replay.

Nothing transmits without --arm.

  python3 bench/fsk_loopback_test.py --host 192.168.0.22 instructions
  python3 bench/fsk_loopback_test.py --host 192.168.0.22 --arm all
  python3 bench/fsk_loopback_test.py --host 192.168.0.22 --arm --tx-card 1 all
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dpmg.link import proto
from dpmg.link.proto import Cmd, Hello, MsgType, CmdStatus

# 2400 bit/s FSK only decodes at the lower rate (its ~83 ms frame outlives the
# 30.72 Msps channelizer block), so per-case work runs at 1.92 Msps; the dense
# scenario needs the wide 30.72 MHz span.
SWEEP_RATE = 1_920_000
DENSE_RATE = 30_720_000
CENTER = 425_025_000
# service's _data_dir("waveforms"), container path. DPMG_WF_DIR overrides it so
# the same phases run against a simulated service whose data dir is elsewhere
# (bench/first_checkout.py --wf-dir); unset = the G40 container path as before.
WF_DIR = os.environ.get("DPMG_WF_DIR", "/app_svc/data/waveforms")

# (mod, symbol_rate, tone_spacing_hz, name, message); each message self-identifies
CASES = [
    ("FSK",  2400,  5000, "fsk_2400_5000",  "FSK 2400/5000 GRID-QF44"),
    ("FSK",  4800,  5000, "fsk_4800_5000",  "FSK 4800/5000 CALL-VK3ABC"),
    ("FSK",  4800, 10000, "fsk_4800_10000", "FSK 4800/10k ALT-12000"),
    ("FSK",  9600, 12000, "fsk_9600_12000", "FSK 9600/12k HDG-270"),
    ("4FSK", 2400,  1296, "fsk4_2400_1296", "4FSK 2400/1296 SPD-450"),
    ("4FSK", 4800,  1296, "fsk4_4800_1296", "4FSK 4800/1296 DMR-POSN"),
    ("4FSK", 4800,  4000, "fsk4_4800_4000", "4FSK 4800/4000 FUEL-68"),
    ("4FSK", 9600,  4000, "fsk4_9600_4000", "4FSK 9600/4000 SQ-7421"),
]
CASE_OFFSET = 300_000          # +300 kHz: away from DC leakage, inside the 1.92 MHz band

# dense scenario, 8 emitters within +-7 MHz (band edge rolls off past that), all
# >=4800 bit/s. (mod, symbol_rate, tone_spacing_hz, offset_hz, message)
DENSE = [
    ("FSK",  4800,  8000, -7_000_000, "D1 FSK4800 -7M"),
    ("FSK",  9600, 12000, -5_000_000, "D2 FSK9600 -5M"),
    ("4FSK", 4800,  1296, -3_500_000, "D3 DMR4800 -3.5M"),
    ("FSK",  9600, 12000, -1_500_000, "D4 FSK9600 -1.5M"),
    ("4FSK", 2400,  1296,  1_500_000, "D5 4FSK2400 +1.5M"),
    ("FSK",  4800,  5000,  3_500_000, "D6 FSK4800 +3.5M"),
    ("4FSK", 9600,  4000,  5_000_000, "D7 4FSK9600 +5M"),
    ("FSK",  4800, 10000,  7_000_000, "D8 FSK4800 +7M"),
]
DENSE_NAME = "fsk_dense_30msps"


# link plumbing
def _sock():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(1.0)
    return s


def hello(s, addr, gui_id="LOOPTEST", retries=12):
    for _ in range(retries):
        s.sendto(proto.encode(Hello(gui_id), 0), addr)
        try:
            data, _ = s.recvfrom(65535)
            mt, _seq, msg = proto.decode(data)
            if mt == MsgType.HELLO_ACK and msg.accepted:
                return msg.serial
        except (socket.timeout, proto.ProtoError):
            continue
    raise SystemExit("no HELLO_ACK -- service unreachable or version mismatch")


class Link:
    def __init__(self, host, port):
        self.s = _sock()
        self.addr = (host, port)
        # The service dedups commands by cmd_id (globally, not per-session), so a
        # reused cmd_id returns a stale cached ack without re-applying the command.
        # Seed uniquely per run (time+pid) so successive invocations never collide.
        self.n = ((int(time.time()) & 0xFFFF) * 20000 + (os.getpid() % 20000)) & 0x7FFFFFFF
        self.rx_conn = "RX"          # physical connector labels, set by init_loopback
        self.tx_conn = "TX"
        self.serial = hello(self.s, self.addr)

    def cmd(self, name, params, timeout=120.0, resend=5.0, soft=False):
        if name == "gen_scenario" and os.environ.get("DPMG_BENCH_PEAK_FS") \
                and "peak_fs" not in (params or {}):
            # the run's --peak-fs (checkout_common.PEAK_FS_ENV)
            params = dict(params or {}, peak_fs=float(os.environ["DPMG_BENCH_PEAK_FS"]))
        self.n += 1
        cmd_id = self.n
        pkt = proto.encode(Cmd(cmd_id, name, params), 0)
        self.s.sendto(pkt, self.addr)
        t0 = last = time.time()
        while time.time() - t0 < timeout:
            if time.time() - last >= resend:
                self.s.sendto(pkt, self.addr)
                last = time.time()
            try:
                data, _ = self.s.recvfrom(65535)
            except socket.timeout:
                continue
            try:
                mt, _seq, msg = proto.decode(data)
            except proto.ProtoError:
                continue
            if mt == MsgType.CMD_ACK and msg.cmd_id == cmd_id:
                # A rejected waveform/plan leaves the previous one active, so every
                # later case would silently test the wrong thing. Stop instead.
                if name in ("set_tx_waveform", "set_plan") and msg.status != CmdStatus.OK:
                    raise SystemExit(f"{name} rejected: {msg.result} (params={params})")
                return msg
        if soft:
            return None
        raise SystemExit(f"no CMD_ACK for {name} (cmd_id={cmd_id})")

    def _drain(self):
        """Discard any queued datagrams so a subsequent wait starts on fresh
        WATCH_UPDATEs. Every sweep case transmits at the same offset: the same
        channelizer band, whose last message lingers; a socket backlog of stale
        updates otherwise burns the whole timeout and reports the previous case's
        message (the 'saw=<prev>' lag seen in the Phase-2 mod sweep)."""
        self.s.setblocking(False)
        try:
            while True:
                self.s.recvfrom(65535)
        except (BlockingIOError, OSError):
            pass
        finally:
            self.s.settimeout(1.0)

    def wait_decode(self, expected, timeout=25.0):
        """Wait until a channelizer band's last message == expected. Returns
        (ok, seconds_to_decode, observed, frame_count)."""
        self._drain()                 # start on fresh updates, not a stale backlog
        t0 = time.time()
        seen = ""
        last_n = 0
        while time.time() - t0 < timeout:
            try:
                data, _ = self.s.recvfrom(65535)
            except socket.timeout:
                continue
            try:
                mt, _seq, msg = proto.decode(data)
            except proto.ProtoError:
                continue
            if mt != MsgType.WATCH_UPDATE:
                continue
            for b in msg.bands:
                m = str(b.get("msg", ""))
                if m:
                    seen = m
                    last_n = int(b.get("dn", 0))
                if m == expected:
                    return True, round(time.time() - t0, 1), m, int(b.get("dn", 0))
        return False, timeout, seen, last_n

    def collect_decodes(self, seconds=20.0):
        """Listen for `seconds` and return {band_name: (message, decoded_n)} of
        everything the channelizer decodes."""
        self._drain()                 # start on fresh updates, not a stale backlog
        t0 = time.time()
        out = {}
        while time.time() - t0 < seconds:
            try:
                data, _ = self.s.recvfrom(65535)
            except socket.timeout:
                continue
            try:
                mt, _seq, msg = proto.decode(data)
            except proto.ProtoError:
                continue
            if mt != MsgType.WATCH_UPDATE:
                continue
            for b in msg.bands:
                m = str(b.get("msg", ""))
                if m:
                    out[str(b.get("n"))] = (m, int(b.get("dn", 0)))
        return out


# scenario specs
def case_spec(mod, sr, sp, message, offset=CASE_OFFSET, name=None, ampl=0.9):
    sig = {"modulation": mod, "symbol_rate": sr, "tone_spacing_hz": sp,
           "offset_hz": offset, "message": message, "amplitude": ampl}
    if mod == "4FSK":
        sig["dibit_mapping"] = "binary"
    # loop_s 0.2 so even the slowest case (FSK 2400 bit/s, ~83 ms/frame) tiles
    # several complete frames per loop
    return {"name": name or "case", "center_freq_hz": CENTER, "loop_s": 0.2,
            "signals": [sig]}


def dense_spec():
    sigs = []
    for (mod, sr, sp, off, msg) in DENSE:
        sig = {"modulation": mod, "symbol_rate": sr, "tone_spacing_hz": sp,
               "offset_hz": off, "message": msg, "amplitude": 0.20}
        if mod == "4FSK":
            sig["dibit_mapping"] = "binary"
        sigs.append(sig)
    return {"name": DENSE_NAME, "center_freq_hz": CENTER, "loop_s": 0.2,
            "signals": sigs}


# phases
def print_instructions():
    print("""
G40 FSK/4FSK RX+TX loopback test: setup

Cabling (RF loopback on the G40, SMA, no antennas; pick one):

  same-card    TRx2A (TX) --[40 dB]-- TRx1A (RX)     run without --tx-card
  cross-card   TRx1B (TX) --[20 dB]-- TRx1A (RX)     run with  --tx-card 1

  Same-card runs RX and TX on one NV100 (shared LO gives some DC leakage, so keep
  signals off the band center). Cross-card puts TX on the second NV100, so the
  two LOs are independent and signals decode right down to the center. The pad
  values above are what these tests were tuned with.

GUI (http://<host>:8080): SDR tab, Host dropdown, G40-0001, then watch the
live PSD and the "Channelizer decode" table.

Run (nothing radiates without --arm):

  same-card    python3 bench/fsk_loopback_test.py --host 192.168.0.22 --arm all
  cross-card   python3 bench/fsk_loopback_test.py --host 192.168.0.22 --arm --tx-card 1 all

The per-case sweep, latency, and exchange run at 1.92 Msps (signal at +300 kHz);
the dense 8-emitter scenario runs at 30.72 Msps (emitters spread +-7 MHz). Every
scenario is left in the radio's waveforms/ dir as .iq.bin / .plan.json /
.spec.json. To replay one by hand in the GUI: Initialize at the matching rate
(1.92 for an fsk_* case, 30.72 for fsk_dense_30msps), Browse waveforms, load,
START TX, then Push plan.
""", flush=True)


RADIOS = {"1": ("192.168.0.22", "g40", "G40"),
          "2": ("192.168.0.26", "x40", "X40")}


def prompt_for_host():
    """Ask which radio to target. Returns (ip, kind)."""
    print("Which radio?")
    for k, (ip, _kind, name) in RADIOS.items():
        print(f"  {k}) {name:<4} {ip}")
    print("  or type an IP address")
    ans = input("radio [1/2/IP]: ").strip()
    if ans in RADIOS:
        ip, kind, _ = RADIOS[ans]
        return ip, kind
    ip = ans or RADIOS["1"][0]
    for _k, (known_ip, kind, _n) in RADIOS.items():
        if ip == known_ip:
            return ip, kind
    return ip, "g40"


def connector(handle, card, kind):
    """Physical SMA connector for a handle+card. G40: TRx<1|2><A|B> (port from the
    handle's digit, A/B from the card). Other radios: just the handle name."""
    if kind != "g40":
        return handle
    port = "2" if handle.endswith("2") else "1"
    return f"TRx{port}{'B' if int(card) == 1 else 'A'}"


def browser_reminder(pause=6):
    print("""
------------------------------------------------------------------
 Open the GUI in a browser now to watch it live:
   http://localhost:8080, SDR tab, Host dropdown: G40-0001
   watch the live PSD and the "Channelizer decode" table while this runs.
   (this console also prints every TX message and RX decode below)
------------------------------------------------------------------""", flush=True)
    for s in range(pause, 0, -1):
        print(f"  starting in {s}s ...", end="\r", flush=True)
        time.sleep(1)
    print(" " * 40, end="\r", flush=True)


def init_loopback(link, rate, center, atten, gain, tx_card=None, kind="g40",
                  clock="internal", lock_timeout=None):
    """Single-card FDD (tx_card=None): RX RXA1 + TX TXA2 on card0 (dual mode).
    Cross-card FDD (tx_card=1): RX RXA1@card0 + TX TXA1@card1 = two independent
    NV100 RFICs/LOs, single mode each. clock = init clock_source; on gpsdo the
    init returns only after checkout_common.gpsdo_gate (GPS antenna bias on +
    GPSDO lock), else GpsLockError (a SystemExit) before anything keys."""
    if tx_card is None:
        tx_ant, tx_c = "TXA2", 0
        extras = {"card": 0, "chan_mode": "dual",
                  "rf_bandwidth_hz": int(rate * 0.8), "tx_center_freq": center}
    else:
        tx_ant, tx_c = "TXA1", int(tx_card)
        extras = {"card": 0, "tx_card": int(tx_card),
                  "rf_bandwidth_hz": int(rate * 0.8), "tx_center_freq": center}
    print(f"[init] {rate/1e6:.3f} Msps  |  center {center/1e6:.4f} MHz  |  "
          f"tx_atten {atten} dB  rx_gain {gain}  clock {clock}", flush=True)
    p = {"kind": kind, "sample_rate": rate, "center_freq": center,
         "rx_gain": gain, "tx_gain": atten, "clock_source": clock,
         "rx_antenna": "RXA1", "tx_antenna": tx_ant, "extras": extras,
         "psd_nfft": 4096, "psd_window": "hann", "psd_buffer": 65536}
    a = link.cmd("init", p, timeout=120)
    if a.status != CmdStatus.OK:
        raise SystemExit(f"init failed: {a.result}")
    print(f"[init] OK: chan_mode={a.result.get('chan_mode')} "
          f"achieved={a.result.get('achieved_rate')} Hz", flush=True)
    import checkout_common as cc        # lazy: checkout_common imports this module
    cc.gpsdo_gate(link, clock, cc.LOCK_TIMEOUT_S if lock_timeout is None else lock_timeout,
                  label=f"{rate/1e6:.2f} MSps")
    rx_conn = connector("RXA1", 0, kind)
    tx_conn = connector(tx_ant, tx_c, kind)
    link.rx_conn, link.tx_conn = rx_conn, tx_conn
    print("[ports] which SDR connector is doing what this run (FDD, both live):", flush=True)
    print(f"          TX  out -->  {tx_conn}  (card{tx_c} / {tx_ant}), radiates only "
          f"while a message is keyed (watch for [TX {tx_conn} ON]/[OFF] below)", flush=True)
    print(f"          RX  in  <--  {rx_conn}  (card0 / RXA1), streams continuously", flush=True)
    print(f"          loopback cable: {tx_conn} through the pad to {rx_conn}", flush=True)
    link.cmd("start_rx", {})
    print(f"[RX {rx_conn}] streaming (on until cleanup)", flush=True)


def warmup(link):
    """Key a throwaway signal until the decode pipeline is hot (first decode),
    so the real sweep never eats the cold-start penalty."""
    print("[warmup] heating the decode pipeline...", flush=True)
    spec = case_spec("FSK", 4800, 5000, "WARMUP", name="warmup")
    link.cmd("gen_scenario", {"spec": spec, "load_tx": True})
    link.cmd("set_plan", {"plan_path": f"{WF_DIR}/warmup.plan.json"})
    time.sleep(2.0)
    link.cmd("start_tx", {"mode": "CONTINUOUS"})
    ok, dt, obs, _ = link.wait_decode("WARMUP", timeout=30)
    link.cmd("stop_tx", {})
    print(f"[warmup] {'hot' if ok else 'NO DECODE'} (t={dt}s, saw {obs!r})", flush=True)
    return ok


def phase_library(link, save_dir):
    """Build + persist every scenario on the radio; save spec JSON locally."""
    print("\n=== LIBRARY: build + persist scenarios on the G40 ===", flush=True)
    built = []
    for (mod, sr, sp, name, message) in CASES:
        spec = case_spec(mod, sr, sp, message, name=name)
        a = link.cmd("gen_scenario", {"spec": spec, "load_tx": False})
        st = "OK" if a.status == CmdStatus.OK else f"FAIL {a.result}"
        iqp = a.result.get("iq_path"); pp = a.result.get("plan_path")
        print(f"  {name:<16} {st}  {iqp}", flush=True)
        _save_spec(save_dir, name, spec)
        built.append((name, iqp, pp))
    return built


def _save_spec(save_dir, name, spec):
    if not save_dir:
        return
    os.makedirs(save_dir, exist_ok=True)
    with open(os.path.join(save_dir, f"{name}.spec.json"), "w") as f:
        json.dump(spec, f, indent=1)


def phase_sweep(link):
    """Transmit each per-case scenario over the loopback and confirm RX decodes
    it, using the persisted files exactly like the GUI would (load then plan then TX)."""
    print("\n=== SWEEP: transmit each FSK case, confirm RX decodes it ===", flush=True)
    results = []
    for (mod, sr, sp, name, message) in CASES:
        iq = f"{WF_DIR}/{name}.iq.bin"
        plan = f"{WF_DIR}/{name}.plan.json"
        link.cmd("set_tx_waveform", {"kind": "file", "path": iq})
        link.cmd("set_plan", {"plan_path": plan})
        time.sleep(2.0)                                   # channel worker settle
        link.cmd("start_tx", {"mode": "CONTINUOUS"})
        print(f"[TX {link.tx_conn} ON]  \"{message}\"  ({mod} {sr} sym/s, {sp} Hz "
              f"spacing, +{CASE_OFFSET // 1000} kHz)", flush=True)
        ok, dt, obs, cnt = link.wait_decode(message, timeout=25)
        if not ok:                                        # one retry
            link.cmd("stop_tx", {}); time.sleep(1.0)
            link.cmd("start_tx", {"mode": "CONTINUOUS"})
            ok, dt, obs, cnt = link.wait_decode(message, timeout=25)
        link.cmd("stop_tx", {})
        print(f"[TX {link.tx_conn} OFF]", flush=True)
        if ok:
            print(f"[RX {link.rx_conn}]  decoded \"{obs}\" in {dt}s  ({cnt} frames)", flush=True)
        else:
            print(f"[RX {link.rx_conn}]  MISS after {dt}s (last saw \"{obs}\")", flush=True)
        results.append((name, ok, dt))
        time.sleep(0.5)
    good = sum(1 for _, ok, _ in results if ok)
    print(f"[sweep] {good}/{len(results)} cases decoded", flush=True)
    return results


def phase_latency(link):
    """Measure RX to TX (detect to response) latency with the transponder while a
    signal loops back continuously. emulate=True instruments the processing
    latency without a TX collision."""
    print("\n=== LATENCY: RX to TX (detect to response) via transponder ===", flush=True)
    out = []
    for (mod, sr, sp) in [("FSK", 4800, 5000), ("4FSK", 4800, 1296)]:
        spec = case_spec(mod, sr, sp, f"LAT {mod} {sr}", name="lat")
        link.cmd("gen_scenario", {"spec": spec, "load_tx": True})
        link.cmd("start_tx", {"mode": "CONTINUOUS"})       # interrogation loops back
        time.sleep(1.5)
        resp = {"message": "ACK", "modulation": mod, "symbol_rate": sr,
                "tone_spacing_hz": sp, "offset_hz": CASE_OFFSET}
        # max_events small so the ack (events list) stays under one datagram
        a = link.cmd("transponder",
                     {"seconds": 6.0, "trigger_snr_db": 10.0, "emulate": True,
                      "response": resp, "min_sep_hz": 20000, "max_events": 5},
                     timeout=25, soft=True)
        link.cmd("stop_tx", {})
        st = a.result.get("stats", {}) if a else {}
        if st:
            pl = st["proc_latency_ms"]; tl = st["total_latency_ms"]
            print(f"  {mod} {sr}/{sp}: {st['events']} events  "
                  f"proc={pl['mean']}ms (min {pl['min']}/max {pl['max']})  "
                  f"total={tl['mean']}ms", flush=True)
            out.append((f"{mod} {sr}/{sp}", st))
        else:
            print(f"  {mod} {sr}/{sp}: no detections (level/SNR?)", flush=True)
        time.sleep(0.5)
    return out


# transponder exchange: RX a request, TX a computed reply
_EX_TOKENS = ["GRID QF44", "CALL VK3XYZ", "BRG 090", "ALT FL350",
              "POSN S37E145", "FUEL 72PCT"]


def _ex_reply(seq, token):
    # reply derived from the request (reversed token + checksum), so it is never
    # the same string the request was
    cs = sum(token.encode()) % 256
    return f"RSP{seq:03d} {token[::-1]} CS{cs:02X}"


def phase_exchange(link, rounds=5, mod="FSK", sr=4800, sp=5000):
    """Each round the G40 keys a request, decodes it on its own RX, computes a
    reply, keys that, and decodes it too: the request and reply are different
    messages."""
    print("\n=== EXCHANGE: RX request, compute, TX a different reply ===", flush=True)
    link.cmd("gen_scenario",
             {"spec": case_spec(mod, sr, sp, "SEED", name="ex"), "load_tx": True})
    link.cmd("set_plan", {"plan_path": f"{WF_DIR}/ex.plan.json"})
    time.sleep(2.0)
    # prime the channel on the seed so round 1 doesn't catch a stale decode
    link.cmd("start_tx", {"mode": "CONTINUOUS"})
    link.wait_decode("SEED", timeout=15)
    link.cmd("stop_tx", {})

    def key_and_read(msg, label):
        link.cmd("gen_scenario",
                 {"spec": case_spec(mod, sr, sp, msg, name="ex"), "load_tx": True})
        link.cmd("start_tx", {"mode": "CONTINUOUS"})
        print(f"           [TX {link.tx_conn} ON]  {label}: \"{msg}\"", flush=True)
        ok, _dt, obs, _n = link.wait_decode(msg, timeout=20)
        link.cmd("stop_tx", {})
        print(f"           [TX {link.tx_conn} OFF]  [RX {link.rx_conn}] decoded \"{obs}\"",
              flush=True)
        return ok

    out = []
    for i in range(rounds):
        seq = i + 1
        token = _EX_TOKENS[i % len(_EX_TOKENS)]
        req = f"REQ{seq:03d} {token}"
        rep = _ex_reply(seq, token)
        print(f"[round {seq}]", flush=True)
        okq = key_and_read(req, "request")
        okp = key_and_read(rep, "reply  (computed from request, different)")
        out.append((req, rep, okq and okp))
    ok = sum(1 for _, _, g in out if g)
    print(f"[exchange] {ok}/{len(out)} rounds decoded both request and reply", flush=True)
    return out


def phase_dense(link, save_dir=None):
    """Build + TX the dense 8-emitter scenario at 30.72 Msps (must be inited at
    DENSE_RATE first) and confirm simultaneous multi-band decode."""
    print("\n=== DENSE: 8 FSK/4FSK transmitters @ 30.72 Msps, simultaneous decode ===",
          flush=True)
    spec = dense_spec()
    a = link.cmd("gen_scenario", {"spec": spec, "load_tx": True})
    _save_spec(save_dir, DENSE_NAME, spec)
    iq = f"{WF_DIR}/{DENSE_NAME}.iq.bin"
    plan = f"{WF_DIR}/{DENSE_NAME}.plan.json"
    link.cmd("set_tx_waveform", {"kind": "file", "path": iq})
    link.cmd("set_plan", {"plan_path": plan})
    time.sleep(3.0)
    link.cmd("start_tx", {"mode": "CONTINUOUS"})
    print(f"[TX {link.tx_conn} ON]  8 emitters keyed at once "
          f"({'built' if a.status == CmdStatus.OK else a.result}):", flush=True)
    for (mod, sr, sp, off, msg) in DENSE:
        print(f"       {off/1e6:+5.1f} MHz  {mod:<4} {sr} sym/s  \"{msg}\"", flush=True)
    print(f"[RX {link.rx_conn}]  collecting decodes for 25 s (TX still ON)...", flush=True)
    got = link.collect_decodes(seconds=25.0)
    link.cmd("stop_tx", {})
    print(f"[TX {link.tx_conn} OFF]", flush=True)
    want = {msg for (_, _, _, _, msg) in DENSE}
    got_msgs = {m for (m, _dn) in got.values()}
    hit = sum(1 for w in want if any(w[:6] in gm for gm in got_msgs))
    for (m, dn) in sorted(got.values()):
        print(f"[RX {link.rx_conn}]  decoded \"{m}\"  ({dn} frames)", flush=True)
    print(f"[dense] {len(got)} bands decoding, ~{hit}/{len(DENSE)} emitters recovered",
          flush=True)
    return got


def cleanup(link):
    print("\n=== CLEANUP: stop TX + stop RX + close card ===", flush=True)
    for c in ("stop_tx", "stop_rx", "close"):
        try:
            a = link.cmd(c, {}, timeout=30)
            print(f"  {c}: {a.status}", flush=True)
        except SystemExit as e:
            print(f"  {c}: {e}", flush=True)


# main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", nargs="?", default="all",
                    choices=["instructions", "library", "sweep", "latency",
                             "exchange", "dense", "cleanup", "all"])
    ap.add_argument("--host", default=None, help="radio IP (prompted if omitted)")
    ap.add_argument("--kind", choices=["g40", "x40"], default=None,
                    help="radio kind (inferred from the choice/IP if omitted)")
    ap.add_argument("--port", type=int, default=47810)
    ap.add_argument("--tx-atten", type=float, default=30)
    ap.add_argument("--rx-gain", type=float, default=255)
    ap.add_argument("--tx-card", type=int, default=None,
                    help="cross-card FDD: put TX on this card (e.g. 1) = separate "
                         "NV100 RFIC/LO. Omit for same-card loopback.")
    ap.add_argument("--arm", action="store_true", help="allow TX to radiate")
    ap.add_argument("--no-init", action="store_true", help="skip init (already up)")
    ap.add_argument("--no-cleanup", action="store_true", help="leave card streaming")
    ap.add_argument("--no-wait", action="store_true",
                    help="skip the open-the-browser pause at start")
    ap.add_argument("--save-dir", default=None,
                    help="if given, also write local copies of the scenario spec "
                         "JSON here (they always land on the radio regardless)")
    import checkout_common as cc
    cc.add_clock_args(ap)
    ap.add_argument("--json", default=None, metavar="PATH",
                    help="also write the phase results as JSON here "
                         "(bench/first_checkout.py reads it)")
    args = ap.parse_args()

    if args.phase == "instructions":
        print_instructions()
        return

    print_instructions()
    if not args.arm:
        print("*** --arm not given: refusing to radiate. Re-run with --arm once "
              "the loopback cable is in. ***", flush=True)
        return

    if args.host is None:
        if not sys.stdin.isatty():
            raise SystemExit("no --host given and no terminal to prompt; pass --host <ip>")
        args.host, inferred = prompt_for_host()
        args.kind = args.kind or inferred
    if args.kind is None:
        args.kind = "x40" if str(args.host).endswith(".26") else "g40"
    if args.kind == "x40":
        print("Note: this test is tuned for the G40 (1.92 / 30.72 Msps). The X40 has "
              "a 50 Msps floor, so these rates will be clamped and may not decode.",
              flush=True)

    browser_reminder(pause=0 if args.no_wait else 6)

    link = Link(args.host, args.port)
    print(f"[link] session up with {link.serial} @ {args.host}:{args.port}", flush=True)
    args.clock = cc.resolve_clock(args.clock, link.serial, args.kind)
    print(f"[link] reference clock {args.clock}", flush=True)

    report = {}

    # Phase A @ 1.92 Msps: per-case library + sweep + latency
    if args.phase in ("all", "library", "sweep", "latency", "exchange"):
        if not args.no_init:
            init_loopback(link, SWEEP_RATE, CENTER, args.tx_atten, args.rx_gain,
                          tx_card=args.tx_card, kind=args.kind,
                          clock=args.clock, lock_timeout=args.lock_timeout)
        else:                           # radio already up: still no keying before lock
            cc.gpsdo_gate(link, args.clock, args.lock_timeout, label="--no-init")
        if args.phase in ("all", "library"):
            report["library"] = phase_library(link, args.save_dir)
        if args.phase in ("all", "sweep"):
            warmup(link)
            report["sweep"] = phase_sweep(link)
        if args.phase in ("all", "latency"):
            report["latency"] = phase_latency(link)
        if args.phase in ("all", "exchange"):
            if args.phase == "exchange":
                warmup(link)
            report["exchange"] = phase_exchange(link)

    # Phase B @ 30.72 Msps: dense multi-emitter scenario
    if args.phase in ("all", "dense"):
        if not args.no_init:
            init_loopback(link, DENSE_RATE, CENTER, args.tx_atten, args.rx_gain,
                          tx_card=args.tx_card, kind=args.kind,
                          clock=args.clock, lock_timeout=args.lock_timeout)
        report["dense"] = phase_dense(link, args.save_dir)

    if args.phase == "cleanup" or (args.phase == "all" and not args.no_cleanup):
        cleanup(link)

    # compact summary
    if "sweep" in report:
        g = sum(1 for _, ok, _ in report["sweep"] if ok)
        print(f"\n[SUMMARY] sweep {g}/{len(report['sweep'])} cases decoded", flush=True)
    if "dense" in report:
        print(f"[SUMMARY] dense {len(report['dense'])} bands decoding", flush=True)
    if args.json:
        out = {"host": args.host, "serial": link.serial, "phase": args.phase,
               "tx_card": args.tx_card, "tx_atten": args.tx_atten,
               "rx_gain": args.rx_gain, "sweep_rate_hz": SWEEP_RATE,
               "dense_rate_hz": DENSE_RATE, "center_hz": CENTER,
               "dense_emitters": [m for (_, _, _, _, m) in DENSE],
               "gps": cc.gps_summary(link, args.clock)}
        out.update(report)
        try:
            os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
            with open(args.json, "w") as f:
                json.dump(out, f, indent=1, default=str)
            print(f"[saved] {args.json}", flush=True)
        except OSError as e:
            print(f"[save failed] {e}", flush=True)


if __name__ == "__main__":
    main()
