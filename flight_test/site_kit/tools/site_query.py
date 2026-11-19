#!/usr/bin/env python3
"""Read the payload G40's service state over UDP, read only, for the site deploy.

    python3 site_query.py read OUT.json              caps, get_config, payload_status,
                                                     frontend_status, gps_status, heartbeat
    python3 site_query.py compare BEFORE.json AFTER.json

Only the read commands below are ever sent; nothing initializes, receives, keys or changes
a setting. The radio is DPMG_G40_HOST (default 192.168.55.1), port DPMG_CMD_PORT (47810).
DPMG_SRC is the code tree that holds dpmg/link/proto.py (the unpacked bundle).
"""
import json
import os
import socket
import sys
import time

SRC = os.environ.get("DPMG_SRC")
if SRC:
    sys.path.insert(0, SRC)
from dpmg.link import proto                     # noqa: E402
from dpmg.link.proto import Cmd, Hello, MsgType  # noqa: E402

RADIO = (os.environ.get("DPMG_G40_HOST", "192.168.55.1"), int(os.environ.get("DPMG_CMD_PORT", "47810")))
READS = (("caps", {}), ("get_config", {"ports": True}), ("payload_status", {}),
         ("frontend_status", {}), ("gps_status", {}))
STATE = {0: "UNINIT (closed)", 1: "INIT_OK (open, idle)", 2: "STREAMING", 3: "FAULT"}


class Client:
    def __init__(self):
        self.s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.s.bind(("0.0.0.0", 0))
        self.s.settimeout(0.2)
        self.seq = 0
        self.cid = int(time.time()) % 100000 * 10 + 7
        self.acks, self.status, self.hello_ack = {}, None, None

    def _send(self, m):
        self.seq += 1
        self.s.sendto(proto.encode(m, self.seq), RADIO)

    def _pump(self, until):
        while time.monotonic() < until:
            try:
                data, _ = self.s.recvfrom(65535)
            except socket.timeout:
                continue
            try:
                mt, _, msg = proto.decode(data)
            except proto.ProtoError:
                continue
            if mt == MsgType.HELLO_ACK:
                self.hello_ack = msg
            elif mt == MsgType.CMD_ACK:
                self.acks[msg.cmd_id] = msg
            elif mt == MsgType.STATUS:
                self.status = msg

    def hello(self):
        end = time.monotonic() + 8
        while self.hello_ack is None and time.monotonic() < end:
            self._send(Hello("GCS-SITE95-READ"))
            self._pump(time.monotonic() + 0.4)
        if self.hello_ack is None:
            raise SystemExit(f"no answer from the service at {RADIO[0]}:{RADIO[1]}")

    def cmd(self, name, params, timeout=8.0):
        assert name in dict(READS), name           # read commands only
        cid = self.cid
        self.cid += 1
        c = Cmd(cid, name, params)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self._send(c)
            self._pump(time.monotonic() + 0.3)
            if cid in self.acks:
                a = self.acks[cid]
                return {"status": int(a.status), "result": a.result or {}}
        return {"status": None, "result": {}, "err": "no reply"}


def read(out):
    c = Client()
    c.hello()
    d = {"radio": f"{RADIO[0]}:{RADIO[1]}", "read_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
         "serial": c.hello_ack.serial}
    for name, params in READS:
        d[name] = c.cmd(name, params)
    end = time.monotonic() + 3
    while c.status is None and time.monotonic() < end:
        c._pump(time.monotonic() + 0.3)
    st = c.status
    d["heartbeat"] = None if st is None else {
        "radio_state": int(st.radio_state), "flags": int(st.flags), "uptime_s": int(st.uptime_s),
        "rx_frames": int(getattr(st, "rx_frames", 0) or 0)}
    with open(out, "w") as f:
        json.dump(d, f, indent=1, default=str)
    for line in summary(d):
        print(line)


def _r(d, name):
    return (d.get(name) or {}).get("result") or {}


def facts(d):
    caps, cfg, pay = _r(d, "caps"), _r(d, "get_config"), _r(d, "payload_status")
    pa = pay.get("pa") or {}
    tr = pay.get("tr") or {}
    g = pay.get("guard") or {}
    hb = d.get("heartbeat") or {}
    return {
        "serial": d.get("serial"),
        "radio_state": hb.get("radio_state"),
        "tx_running": pay.get("tx_running"),
        "clock_sources": caps.get("clock_sources"),
        "port_jacks": caps.get("port_jacks"),
        "ports": cfg.get("ports_full") if cfg.get("ports_full") is not None else cfg.get("ports"),
        "ports_by": cfg.get("ports_by"),
        "ports_err": cfg.get("ports_err"),
        "guard_configured": pay.get("configured"),
        "guard_profile": g.get("profile"),
        "guard_protect_db": g.get("protect_db"),
        "guard_min_db": g.get("min_db"),
        "guard_load_min_tx_atten_db": (g.get("load") or {}).get("min_tx_atten_db"),
        "guard_config_error": g.get("config_error"),
        "frontend": pay.get("frontend"),
        "pa_keyed": pa.get("keyed"),
        "pa_level": pa.get("level"),
        "pa_active_high": pa.get("active_high"),
        "tr_tx": tr.get("tx"),
        "tr_tx_level": tr.get("tx_level"),
        "rx_atten_db": (pay.get("atten") or {}).get("att_db"),
        "fe_err": pay.get("fe_err"),
    }


def summary(d):
    f = facts(d)
    out = [f"service {d.get('radio')} serial {f['serial']}, read {d.get('read_utc')} UTC"]
    out.append(f"radio state {f['radio_state']} {STATE.get(f['radio_state'], '')}, tx_running {f['tx_running']}")
    cs = f["clock_sources"] or []
    out.append(f"clock choices in caps ({len(cs)}): {', '.join(cs)}")
    out.append(f"port jacks in caps: {f['port_jacks']}")
    out.append(f"port map: {json.dumps(f['ports'], sort_keys=True)}  last set by {f['ports_by']}"
               + (f"  ERROR {f['ports_err']}" if f["ports_err"] else ""))
    out.append(f"payload guard: configured {f['guard_configured']}, profile {f['guard_profile']}, "
               f"RX protect {f['guard_protect_db']} dB, TX floor {f['guard_load_min_tx_atten_db']} dB"
               + (f", config error {f['guard_config_error']}" if f["guard_config_error"] else ""))
    out.append(f"front end {f['frontend']}: HPA keyed {f['pa_keyed']}, HPA enable line active {f['pa_level']} "
               f"(active high {f['pa_active_high']}), switch tx {f['tr_tx']} (tx level {f['tr_tx_level']}), "
               f"RX attenuator {f['rx_atten_db']} dB" + (f", error {f['fe_err']}" if f["fe_err"] else ""))
    for name, _ in READS:
        if (d.get(name) or {}).get("status") not in (0,):
            out.append(f"note: {name} answered {d.get(name)}")
    return out


def compare(before, after):
    a, b = facts(json.load(open(before))), facts(json.load(open(after)))
    bad = 0
    for k in ("ports", "guard_configured", "guard_profile", "guard_protect_db",
              "guard_load_min_tx_atten_db", "frontend", "pa_active_high", "tr_tx_level"):
        same = json.dumps(a[k], sort_keys=True) == json.dumps(b[k], sort_keys=True)
        bad += not same
        print(f"{'same   ' if same else 'CHANGED'} {k}: before {json.dumps(a[k], sort_keys=True)}"
              + ("" if same else f"  after {json.dumps(b[k], sort_keys=True)}"))
    n = len(b["clock_sources"] or [])
    want = int(os.environ.get("SITE_EXPECT_CLOCKS", "5"))     # 5 on the flight code, 3 on the old
    print(f"{'ok     ' if n == want else 'NOT OK '} clock choices now: {n}, expected {want} ({', '.join(b['clock_sources'] or [])})")
    off = b["pa_keyed"] is False and b["pa_level"] is False and b["tr_tx"] is False
    print(f"{'ok     ' if off else 'NOT OK '} HPA after: keyed {b['pa_keyed']}, enable line active {b['pa_level']}, switch tx {b['tr_tx']}")
    return 1 if bad or n != want or not off else 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "read":
        read(sys.argv[2])
    elif len(sys.argv) == 4 and sys.argv[1] == "compare":
        sys.exit(compare(sys.argv[2], sys.argv[3]))
    else:
        print(__doc__)
        sys.exit(2)
