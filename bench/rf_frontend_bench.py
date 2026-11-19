"""rf_frontend_bench.py: exercise the external PA / TX-RX switch front end on a
live radio over UDP, exactly as the GUI does (docs/GPIO_FRONTEND.md).

  python bench/rf_frontend_bench.py status              frontend_status (resolved chip:offset per line)
  python bench/rf_frontend_bench.py manual [hold_s]     switch to TX, PA on, hold (meter socket pin 17 / 15), SAFE
  python bench/rf_frontend_bench.py init                init card0 RXA1+TXA2 dual, 1.92 MSps, 915 MHz, 30 dB TX atten
  python bench/rf_frontend_bench.py burst x [wave_s]    tone of wave_s, SINGLE_SHOT TX, poll PA state (radiates)
  python bench/rf_frontend_bench.py cont [secs] [wave_s] CONTINUOUS TX for secs, watchdog alive, stop (radiates)
  python bench/rf_frontend_bench.py reload              frontend_reload (re-read rf_frontend.yaml on the radio)
  python bench/rf_frontend_bench.py set tr=tx|rx pa=on|off safe
                                                        frontend_set, applied in the order given (no TX)
Any other word is refused before the radio is contacted.

Radio = DPMG_G40_HOST (deploy_config / .env), port DPMG_CMD_PORT.  On the G40,
manual hold, 3 s burst and continuous 1.5 s waveform all key and release cleanly.
"""
import json, os, socket, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dpmg.link import proto
from dpmg.link.proto import Cmd, Hello, MsgType
try:
    from dpmg.deploy_config import G40_HOST, CMD_PORT
except Exception:
    G40_HOST, CMD_PORT = "", 47810
G40 = (os.environ.get("DPMG_G40_HOST", G40_HOST), int(os.environ.get("DPMG_CMD_PORT", CMD_PORT)))
if not G40[0]:
    sys.exit("Set DPMG_G40_HOST to the radio address first.")

class Client:
    def __init__(self):
        self.s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.s.bind(("0.0.0.0", 0)); self.s.settimeout(0.2)
        self.seq = 0; self.acks = {}; self.cid = int(time.time()) % 100000 * 10; self.status = None
        self.hello_ack = None
    def _send(self, m): self.seq += 1; self.s.sendto(proto.encode(m, self.seq), G40)
    def _pump(self, deadline):
        while time.monotonic() < deadline:
            try: data, _ = self.s.recvfrom(4096)
            except socket.timeout: continue
            try: mt, _, msg = proto.decode(data)
            except proto.ProtoError: continue
            if mt == MsgType.HELLO_ACK: self.hello_ack = msg
            elif mt == MsgType.CMD_ACK: self.acks[msg.cmd_id] = msg
            elif mt == MsgType.STATUS: self.status = msg
    def hello(self):
        end = time.monotonic() + 6
        while self.hello_ack is None and time.monotonic() < end:
            self._send(Hello("GCS-FE-TEST")); self._pump(time.monotonic() + 0.4)
        if self.hello_ack is None: raise SystemExit("no HELLO_ACK from G40")
    def cmd(self, name, params=None, timeout=8.0):
        cid = self.cid; self.cid += 1; c = Cmd(cid, name, params or {})
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self._send(c); self._pump(time.monotonic() + 0.25)
            if cid in self.acks:
                a = self.acks[cid]; return a.status, (a.result or {})
        raise TimeoutError(name)

def fe(c):
    st, r = c.cmd("frontend_status"); return r.get("frontend") or {}

MODES = ("status", "init", "reload", "manual", "set", "burst", "cont")


def _set_args(words):
    """'tr=tx pa=on safe' to frontend_set parameter dicts, one per word, in order."""
    out = []
    for w in words:
        k, _, v = w.partition("=")
        if k == "safe" and not v:
            out.append({"safe": True})
        elif k == "tr" and v in ("tx", "rx"):
            out.append({"tr": v})
        elif k == "pa" and v in ("on", "off"):
            out.append({"pa": v == "on"})
        else:
            raise SystemExit(f"set: {w!r} not understood (use tr=tx, tr=rx, pa=on, pa=off, safe)")
    if not out:
        raise SystemExit("set: give at least one of tr=tx, tr=rx, pa=on, pa=off, safe")
    return out


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "status"
    if mode not in MODES:
        raise SystemExit(f"unknown mode {mode!r}: use one of {', '.join(MODES)} "
                         "(see the top of this file). Nothing was sent to the radio.")
    sets = _set_args(sys.argv[2:]) if mode == "set" else []
    c = Client(); c.hello()
    st, r = c.cmd("frontend_status")
    f = r.get("frontend") or {}
    c._pump(time.monotonic() + 1.2); print("radio_state:", getattr(c.status, "radio_state", None), "| frontend:", json.dumps(f)[:700])
    if mode == "status":
        return
    if mode == "init":
        rate, center = 1.92e6, 915e6
        p = {"kind": "g40", "sample_rate": rate, "center_freq": center, "rx_gain": 230, "tx_gain": 30,
             "clock_source": "internal", "rx_antenna": "RXA1", "tx_antenna": "TXA2",
             "extras": {"card": 0, "chan_mode": "dual", "rf_bandwidth_hz": int(rate * 0.8), "tx_center_freq": center},
             "psd_nfft": 4096, "psd_window": "hann", "psd_buffer": 65536}
        st, r = c.cmd("init", p, timeout=120)
        print("init:", st, {k: r.get(k) for k in ("chan_mode", "achieved_rate", "err")}); return
    if mode == "reload":
        print("reload:", c.cmd("frontend_reload", {}, timeout=15)); return
    if mode == "set":
        for prm in sets:
            st, r = c.cmd("frontend_set", prm)
            x = r.get("frontend") or {}
            print(f"set {prm}: status {st}", r.get("err") or "",
                  {"pa_keyed": (x.get("pa") or {}).get("keyed"), "pa_level": (x.get("pa") or {}).get("level"),
                   "tr_tx": (x.get("tr") or {}).get("tx")})
        return
    if mode == "manual":
        hold = float(sys.argv[2]) if len(sys.argv) > 2 else 8.0
        print("switch -> TX:", c.cmd("frontend_set", {"tr": "tx"})[0])
        st, r = c.cmd("frontend_set", {"pa": True})
        print("PA ON:", st, r.get("err") or {"keyed": r["frontend"]["pa"]["keyed"], "level": r["frontend"]["pa"]["level"]})
        print(f"  >>> METER NOW: socket pin 17 (PG.06) should read 3.3 V, pin 15 (PN.01) 3.3 V, for {hold:.0f} s")
        t0 = time.time()
        while time.time() - t0 < hold:
            time.sleep(1.0); x = fe(c)
            print(f"  t+{time.time()-t0:4.1f}s keyed={x['pa'].get('keyed')} pa_level={x['pa'].get('level')} tr_level={x['tr'].get('level')} wd_remaining={x.get('watchdog_remaining_ms')} trips={x.get('watchdog_trips')}")
        st, r = c.cmd("frontend_set", {"safe": True})
        x = r["frontend"]; print("SAFE:", st, {"keyed": x["pa"]["keyed"], "pa_level": x["pa"]["level"], "tr_level": x["tr"].get("level")})
        return
    # TX modes need an initialized radio + a loaded waveform
    rs = getattr(c.status, "radio_state", None) if c.status else None
    print("radio_state:", rs)
    wsec = float(sys.argv[3]) if len(sys.argv) > 3 else 0.5
    st, r = c.cmd("gen_test_waveform", {"kind": "tone", "freq": 100000.0, "seconds": wsec,
                                        "ampl": 0.3, "name": "fe_tone", "load_tx": True}, timeout=20)
    print("gen tone 0.5 s:", st, {k: r.get(k) for k in ("path", "n", "fs", "loaded_tx", "err")})
    if st != 0:
        return
    if mode == "burst":
        samples = []
        st, r = c.cmd("start_tx", {"mode": "SINGLE_SHOT"}, timeout=15)
        print("start_tx SINGLE_SHOT:", st, r.get("err", ""))
        t0 = time.monotonic()
        while time.monotonic() - t0 < wsec + 2.0:
            x = fe(c); samples.append((round(time.monotonic() - t0, 3), x["pa"].get("keyed"), x["pa"].get("level"), x["tr"].get("level")))
            time.sleep(0.02)
        keyed = [s for s in samples if s[1]]
        print(f"samples={len(samples)} keyed_samples={len(keyed)} first_keyed={keyed[0][0] if keyed else None} last_keyed={keyed[-1][0] if keyed else None}")
        print("tail:", samples[-1])
        print("stop_tx:", c.cmd("stop_tx")[0]); x = fe(c)
        print("after:", {"keyed": x["pa"]["keyed"], "pa_level": x["pa"]["level"], "tr_level": x["tr"].get("level"), "trips": x["watchdog_trips"], "err": x["last_error"]})
        return
    if mode == "cont":
        secs = float(sys.argv[2]) if len(sys.argv) > 2 else 4.0
        st, r = c.cmd("start_tx", {"mode": "CONTINUOUS"}, timeout=15)
        print("start_tx CONTINUOUS:", st, r.get("err", ""))
        t0 = time.monotonic()
        while time.monotonic() - t0 < secs:
            x = fe(c)
            print(f"  t+{time.monotonic()-t0:4.1f}s keyed={x['pa']['keyed']} hold={x['hold']} pa_level={x['pa'].get('level')} tr_level={x['tr'].get('level')} wd_remaining={x.get('watchdog_remaining_ms')} trips={x['watchdog_trips']} err={x['last_error']}")
            time.sleep(0.5)
        print("stop_tx:", c.cmd("stop_tx")[0]); time.sleep(0.1); x = fe(c)
        print("after:", {"keyed": x["pa"]["keyed"], "pa_level": x["pa"].get("level"), "tr_level": x["tr"].get("level"), "trips": x["watchdog_trips"], "err": x["last_error"]})

main()
