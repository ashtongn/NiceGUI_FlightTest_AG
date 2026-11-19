#!/usr/bin/env python3
"""
bench_transceive.py: direct proto client to drive the G40 transceive bench.

Owns one session (HELLO then CMD then CMD_ACK). Non-radiating stages run freely;
every stage that keys TX refuses to run without --arm, so nothing radiates by
accident. Point a spectrum analyzer at the TX port before passing --arm.

Stages:
  init-dual   RX TRx1A + TX TRx2A on one card: verifies chan_mode=dual. No TX.
  status      print one STATUS heartbeat.
  start-rx    start RX streaming. No TX.
  stop-rx     stop RX.
  tx-tone     [--arm] CW tone burst on the TX port (LO offset), dwell, stop.
  transceive  [--arm] enable the modem responder (armed=--arm).
  stop-tx     stop TX.

Usage:
  python3 bench_transceive.py --host 192.168.40.101 init-dual
  python3 bench_transceive.py --host 192.168.40.101 tx-tone --arm --dwell 3
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import time

# this script lives in bench/; put the repo root on the path so `import proto`
# (and the other top-level modules) resolves no matter the current directory.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dpmg.link import proto
from dpmg.link.proto import Cmd, Hello, MsgType


def _sock():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(1.0)
    return s


def hello(s, addr, gui_id="BENCH-CLI", retries=10):
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


def send_cmd(s, addr, name, params, cmd_id, timeout=15.0, resend=5.0):
    """Send a CMD and wait for its CMD_ACK. Resends at `resend` cadence (the
    service dedups by cmd_id, so a resend during a long-running init is safe and
    never re-applies). A slow cadence avoids interrupting a card cal in flight."""
    pkt = proto.encode(Cmd(cmd_id, name, params), 0)
    s.sendto(pkt, addr)
    t0 = last = time.time()
    while time.time() - t0 < timeout:
        now = time.time()
        if now - last >= resend:
            s.sendto(pkt, addr)
            last = now
        try:
            data, _ = s.recvfrom(65535)
        except socket.timeout:
            continue
        try:
            mt, _seq, msg = proto.decode(data)
        except proto.ProtoError:
            continue
        if mt == MsgType.CMD_ACK and msg.cmd_id == cmd_id:
            return msg
    raise SystemExit(f"no CMD_ACK for {name} (cmd_id={cmd_id})")


def one_status(s, addr, timeout=3.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            data, _ = s.recvfrom(65535)
            mt, _seq, msg = proto.decode(data)
            if mt == MsgType.STATUS:
                return msg
        except (socket.timeout, proto.ProtoError):
            continue
    return None


def init_dual_params(a):
    extras = {"card": a.card,
              "rf_bandwidth_hz": int(a.rf_bw if getattr(a, "rf_bw", 0) else a.rate * 0.8),
              "tx_center_freq": a.tx_freq}
    if a.tx_ant != "TXA1":
        extras["chan_mode"] = "dual"    # A2 and B1 TX both need dual mode
    return {
        "kind": getattr(a, "radio_kind", "g40"),
        "sample_rate": a.rate, "center_freq": a.rx_freq,
        "rx_gain": a.rx_gain, "tx_gain": a.tx_atten,
        "clock_source": "internal",
        "rx_antenna": a.rx_ant, "tx_antenna": a.tx_ant,  # A2/B1 TX exit J2/TRx2A
        "extras": extras,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="192.168.40.101",
                    help="radio service host (default 192.168.40.101; the G40 is 192.168.0.22)")
    ap.add_argument("--port", type=int, default=47810)
    ap.add_argument("stage", choices=["init-dual", "status", "psd", "start-rx",
                                      "stop-rx", "tx-tone", "tx-file",
                                      "set-plan", "transceive", "stop-tx",
                                      "extract-region", "gen-test", "checkout",
                                      "chan-test", "transponder", "gps-status",
                                      "gen-scenario", "list-waveforms"])
    ap.add_argument("--resp-json", help="response message spec JSON (transponder)")
    ap.add_argument("--trigger-snr", type=float, default=15.0)
    ap.add_argument("--kind", default="chirp",
                    choices=["chirp", "tone", "two_tone", "noise"])
    ap.add_argument("--f0", type=float, help="gen chirp start / tone freq (Hz)")
    ap.add_argument("--f1", type=float, help="gen chirp stop / two_tone f2 (Hz)")
    ap.add_argument("--bw", type=float, help="gen noise bandwidth (Hz)")
    ap.add_argument("--ampl", type=float, default=0.5, help="gen amplitude")
    ap.add_argument("--load-tx", action="store_true", help="load gen wf as TX")
    ap.add_argument("--f-lo", type=float, help="region low freq Hz (extract-region)")
    ap.add_argument("--f-hi", type=float, help="region high freq Hz (extract-region)")
    ap.add_argument("--filter", default="hann", help="carve filter window")
    ap.add_argument("--seconds", type=float, default=1.0, help="carve capture seconds")
    ap.add_argument("--name", default="region", help="output basename on the radio")
    ap.add_argument("--fmt", default="c64", choices=["c64", "dpmg"])
    ap.add_argument("--plan-file", help="local plan JSON, sent inline (set-plan)")
    ap.add_argument("--plan-path", help="plan JSON path on the radio host (set-plan)")
    ap.add_argument("--path", help="waveform file path on the radio host (tx-file)")
    ap.add_argument("--arm", action="store_true", help="permit TX to radiate")
    ap.add_argument("--card", type=int, default=0)
    ap.add_argument("--rx-freq", type=float, default=425.025e6)
    ap.add_argument("--tx-freq", type=float, default=425.025e6)
    ap.add_argument("--rate", type=float, default=1_920_000.0)  # G40 known-good profile
    ap.add_argument("--rx-gain", type=float, default=200.0)
    ap.add_argument("--tx-atten", type=float, default=30.0, help="TX attenuation dB (default 30)")
    ap.add_argument("--tx-ant", default="TXA2", choices=["TXA1", "TXA2", "TXB1"])
    ap.add_argument("--rx-ant", default="RXA1", choices=["RXA1", "RXA2", "RXB1", "RXB2"])
    ap.add_argument("--radio-kind", default="g40", choices=["g40", "x40"])
    ap.add_argument("--rf-bw", type=float, default=0.0,
                    help="explicit RF bandwidth Hz (0=auto 0.8*rate); lower bw lets "
                         "the X40 pick a decimated sub-50 Msps profile")
    ap.add_argument("--offset", type=float, default=100_000.0, help="tone offset Hz")
    ap.add_argument("--dwell", type=float, default=3.0, help="tx-tone seconds")
    ap.add_argument("--modulation", default="FSK")
    a = ap.parse_args()
    addr = (a.host, a.port)

    radiating = a.stage in ("tx-tone", "transceive")
    if radiating and a.stage == "transceive" and not a.arm:
        pass  # transceive without --arm is allowed: configures DISARMED
    if a.stage == "tx-tone" and not a.arm:
        raise SystemExit("tx-tone RADIATES -- refusing without --arm "
                         "(connect a spectrum analyzer first)")

    s = _sock()
    # unique per invocation: whole-second seeds collide across rapid runs and
    # the service replays a stale cached ack (looks like "no waveform set")
    cid = (int(time.time() * 1000) ^ (os.getpid() << 12)) & 0x7FFFFFFF
    serial = hello(s, addr)
    print(f"session open with {serial}")

    if a.stage == "init-dual":
        print("init (dual-mode cal can take ~30s, waiting patiently)...")
        ack = send_cmd(s, addr, "init", init_dual_params(a), cid, timeout=90.0)
        r = ack.result
        print(f"init ack: status={ack.status} result={r}")
        cm = r.get("chan_mode")
        print(f"\n  chan_mode = {cm!r}  ->  "
              f"{'DUAL OK' if cm == 'dual' else 'NOT DUAL (check)'}")
        print(f"  tx_center_hz = {r.get('tx_center_hz')}  "
              f"rx_gain_db = {r.get('rx_gain_db')}")
        return 0

    if a.stage == "status":
        st = one_status(s, addr)
        print(f"status: {st}")
        return 0

    if a.stage == "psd":
        # collect PSD frames for a few seconds; report the max-hold peaks so a
        # low-duty burst still shows. Prints top bins as offset-from-center.
        import numpy as _np
        hold = None
        meta = None
        t0 = time.time()
        while time.time() - t0 < 5.0:
            try:
                data, _ = s.recvfrom(65535)
                mt, _seq, msg = proto.decode(data)
            except (socket.timeout, proto.ProtoError):
                continue
            if mt != MsgType.PSD_FRAME:
                continue
            arr = _np.frombuffer(msg.bins, dtype=_np.int8).astype(_np.float32)
            db = msg.ref_db + arr * (msg.scale_db_x100 / 100.0)
            hold = db if hold is None else _np.maximum(hold, db)
            meta = msg
        if hold is None:
            print("no PSD frames received")
            return 1
        n = len(hold)
        freqs = (_np.arange(n) - n / 2) * (meta.span_hz / n)
        med = float(_np.median(hold))
        print(f"PSD max-hold over 5s: {n} bins, center {meta.center_hz/1e6:.3f} MHz, "
              f"span {meta.span_hz/1e3:.0f} kHz, median floor {med:.1f} dB")
        order = _np.argsort(hold)[::-1][:8]
        for i in sorted(order):
            if hold[i] - med > 6:
                print(f"  peak {freqs[i]/1e3:+9.1f} kHz : {hold[i]:6.1f} dB "
                      f"({hold[i]-med:+.1f} vs floor)")
        return 0

    if a.stage == "start-rx":
        print(send_cmd(s, addr, "start_rx", {}, cid).result)
        return 0
    if a.stage == "stop-rx":
        print(send_cmd(s, addr, "stop_rx", {}, cid).result)
        return 0
    if a.stage == "stop-tx":
        print(send_cmd(s, addr, "stop_tx", {}, cid).result)
        return 0

    if a.stage == "tx-tone":
        print("set_tx_waveform:",
              send_cmd(s, addr, "set_tx_waveform",
                       {"kind": "tone", "offset_hz": a.offset, "ampl": 0.5}, cid).result)
        cid += 1
        if a.dwell <= 0:
            print(f"start_tx (RADIATING @ LO+{a.offset/1e3:g}kHz, HELD)...")
            print("start_tx:", send_cmd(s, addr, "start_tx", {"mode": "CONTINUOUS"}, cid).result)
            print("\n  TX is radiating and HELD. Run  stop-tx  to stop it.")
            return 0
        print(f"start_tx (RADIATING {a.dwell}s @ LO+{a.offset/1e3:g}kHz)...")
        print("start_tx:", send_cmd(s, addr, "start_tx", {"mode": "CONTINUOUS"}, cid).result)
        time.sleep(a.dwell)
        cid += 1
        print("stop_tx:", send_cmd(s, addr, "stop_tx", {}, cid).result)
        return 0

    if a.stage == "set-plan":
        # multi-entry plans exceed MAX_DATAGRAM inline: use --plan-path to
        # point the service at a JSON already on the radio host instead
        if a.plan_path:
            ack = send_cmd(s, addr, "set_plan", {"plan_path": a.plan_path}, cid)
        else:
            import json as _json
            plan = _json.load(open(a.plan_file))
            ack = send_cmd(s, addr, "set_plan", {"plan": plan}, cid)
        print(f"set_plan ack: status={ack.status} {ack.result}")
        return 0

    if a.stage == "tx-file":
        if not a.arm:
            raise SystemExit("tx-file RADIATES -- refusing without --arm")
        print("set_tx_waveform:",
              send_cmd(s, addr, "set_tx_waveform",
                       {"kind": "file", "path": a.path}, cid).result)
        cid += 1
        print("start_tx (RADIATING, looping file, HELD)...")
        print("start_tx:", send_cmd(s, addr, "start_tx", {"mode": "CONTINUOUS"}, cid).result)
        print("\n  TX is radiating and HELD. Run  stop-tx  to stop it.")
        return 0

    if a.stage == "transceive":
        p = {"enabled": True, "armed": bool(a.arm), "modulation": a.modulation}
        ack = send_cmd(s, addr, "transceive", p, cid)
        print(f"transceive ack: {ack.result}  (armed={a.arm})")
        return 0

    if a.stage == "extract-region":
        if a.f_lo is None or a.f_hi is None:
            raise SystemExit("extract-region needs --f-lo and --f-hi (Hz)")
        p = {"f_lo_hz": a.f_lo, "f_hi_hz": a.f_hi, "filter": a.filter,
             "seconds": a.seconds, "name": a.name, "fmt": a.fmt}
        ack = send_cmd(s, addr, "extract_region", p, cid, timeout=30.0)
        print(f"extract_region ack: status={ack.status} {ack.result}")
        return 0

    if a.stage == "gen-test":
        p = {"kind": a.kind, "seconds": a.seconds, "ampl": a.ampl,
             "name": a.name if a.name != "region" else a.kind,
             "load_tx": a.load_tx}
        if a.kind == "chirp":
            if a.f0 is not None: p["f0"] = a.f0
            if a.f1 is not None: p["f1"] = a.f1
        elif a.kind == "tone":
            if a.f0 is not None: p["freq"] = a.f0
        elif a.kind == "two_tone":
            if a.f0 is not None: p["f1"] = a.f0
            if a.f1 is not None: p["f2"] = a.f1
        elif a.kind == "noise":
            if a.bw is not None: p["bw"] = a.bw
        ack = send_cmd(s, addr, "gen_test_waveform", p, cid, timeout=20.0)
        print(f"gen_test_waveform ack: status={ack.status} {ack.result}")
        return 0

    if a.stage == "list-waveforms":
        ack = send_cmd(s, addr, "list_waveforms", {}, cid, timeout=10.0)
        for fdesc in (ack.result or {}).get("files", []):
            print(f"  {fdesc['bytes']/1e6:7.1f} MB  {fdesc['path']}")
        return

    if a.stage == "gen-scenario":
        # build a scenario waveform + plan ON the radio from a JSON spec (--plan-file)
        import json as _json
        spec = _json.load(open(a.plan_file))
        ack = send_cmd(s, addr, "gen_scenario", {"spec": spec, "load_tx": a.arm},
                       cid, timeout=30.0)
        print(f"gen_scenario ack: status={ack.status} {ack.result}")
        return

    if a.stage == "gps-status":
        ack = send_cmd(s, addr, "gps_status", {}, cid, timeout=10.0)
        r = ack.result or {}
        print(f"gps_status ack: status={ack.status}")
        print(f"  ref_clock={r.get('ref_clock')}  gpsdo: supported={r.get('gpsdo_supported')} "
              f"enabled={r.get('gpsdo_enabled')} locked={r.get('gpsdo_locked')} "
              f"acc={r.get('freq_accuracy_ppm')} ppm")
        print(f"  ext_ref: present={r.get('ext_ref_present')} freq={r.get('ext_ref_freq_hz')} Hz   "
              f"1pps: present={r.get('pps_present')} source={r.get('pps_source')}")
        return

    if a.stage == "checkout":
        # --arm gates radiation exactly like tx-tone: the loopback leg keys TX
        p = {"seconds": a.seconds, "armed": a.arm, "tone_offset_hz": a.offset}
        ack = send_cmd(s, addr, "ground_checkout", p, cid, timeout=30.0)
        print(f"ground_checkout ack: status={ack.status} pass={ack.result.get('pass')}")
        for t in (ack.result or {}).get("report", []):
            print(f"  [{'PASS' if t.get('pass') else 'FAIL'}] {t}")
        return 0

    if a.stage == "chan-test":
        ack = send_cmd(s, addr, "channelizer_test", {"seconds": a.seconds}, cid, timeout=20.0)
        r = ack.result or {}
        print(f"channelizer_test: {r.get('n')} signals, floor {r.get('floor_db')} dB "
              f"@ {r.get('fs',0)/1e6:.2f} MSps")
        for sg in r.get("signals", []):
            print(f"  {sg['freq_hz']/1e3:+8.1f} kHz  SNR {sg['snr_db']:5.1f} dB  "
                  f"(RF {sg.get('rf_hz',0)/1e6:.4f} MHz)")
        return 0

    if a.stage == "transponder":
        import json as _json
        resp = _json.load(open(a.resp_json)) if a.resp_json else \
            {"message": "ACK X40", "modulation": "FSK", "symbol_rate": 4800,
             "tone_spacing_hz": 5000, "offset_hz": 0}
        p = {"seconds": a.seconds, "trigger_snr_db": a.trigger_snr,
             "response": resp, "emulate": not a.arm, "armed": a.arm}
        ack = send_cmd(s, addr, "transponder", p, cid, timeout=max(30.0, a.seconds + 10))
        r = ack.result or {}
        print(f"transponder: {len(r.get('events',[]))} events, response={r.get('response')}, "
              f"emulated={r.get('emulated')}")
        for e in r.get("events", [])[:8]:
            print(f"  detect {e['detect_khz']:+.1f} kHz SNR {e['snr_db']:.1f} dB | "
                  f"proc {e['proc_latency_ms']:.2f} ms  total {e['total_latency_ms']:.2f} ms")
        st = r.get("stats", {})
        if st:
            print(f"  LATENCY: proc mean {st['proc_latency_ms']['mean']} ms "
                  f"(min {st['proc_latency_ms']['min']}/max {st['proc_latency_ms']['max']}), "
                  f"total mean {st['total_latency_ms']['mean']} ms")
        return 0


if __name__ == "__main__":
    sys.exit(main())
