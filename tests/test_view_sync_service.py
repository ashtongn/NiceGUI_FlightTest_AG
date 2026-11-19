#!/usr/bin/env python3
"""
VIEW_SYNC service side (dpmg_service + link_client): offline, simulated radio
===============================================================================
  1. get_config on a closed radio, then after an operator-style init + live
     set_tx_atten / set_center / set_gain: clock, ports, cards, rate, centre,
     gains, TX atten, PSD shaping, ant_bias, cfg_seq (counts state changes only),
     and view_sync.op_fields / eng_fields turn it into the right widget values
  2. TX waveform (tone) and radio logging show up; close: inited False
  3. ACK_PER_SENDER: the same cmd_id from two senders is applied twice, a
     duplicate from one sender is replayed; the ack store stays <= APPLIED_MAX
  4. CARVE_RATE: a carved c64 (sidecar) and a DPMG .bin report their own rate
     (and the c64 its band centre) through set_tx_waveform
  5. UPLOAD_NAME_FIX: wire_file_name keeps the extension within 32 bytes
  6. PSD_STALE_FIX: the PSD loop does not rebuild a PSD from the same frame twice
  7. CAP_LOCK: two concurrent raw-IQ captures both complete (no shared-buffer crash)

    python3 tests/test_view_sync_service.py
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import numpy as np                                     # noqa: E402

from dpmg.radio import dpmg_service as ds                              # noqa: E402
from dpmg.dsp import region_extract as rx                            # noqa: E402
from dpmg.link import view_sync as vs                                 # noqa: E402
from dpmg.link.link_client import wire_file_name                 # noqa: E402
from dpmg.link.proto import Cmd, CmdStatus, HostType             # noqa: E402

FAILS = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


class _Ep:
    def __init__(self):
        self.sent = []

    def send(self, msg, addr):
        self.sent.append((msg, addr))


def _svc():
    return ds.RadioService(serial="G40-SIM", host_type=HostType.G40_RX, kind="g40",
                           simulate=True, cmd_port=47894, discovery_port=47895,
                           broadcast="127.0.0.1")


INIT = {"kind": "g40", "sample_rate": 7_680_000, "center_freq": 430_500_000,
        "rx_gain": 220, "tx_gain": 10.0, "clock_source": "gpsdo",
        "rx_antenna": "RXA1", "tx_antenna": "TXA1",
        "extras": {"card": 0, "tx_card": 1, "chan_mode": "single",
                   "tx_center_freq": 430_500_000},
        "psd_nfft": 4096, "psd_window": "hann", "psd_buffer": 65536}


async def _cfg(svc, cid=900):
    a = await svc._apply(Cmd(cid, "get_config", {}))
    return a.result


async def _cases():
    svc = _svc()
    svc.loop = asyncio.get_running_loop()
    svc.ep = _Ep()
    print("[1] get_config")
    c0 = await _cfg(svc)
    check(c0["inited"] is False and c0["cfg_seq"] == 0, f"closed radio: inited False, seq 0 ({c0})")
    check("log" in c0 and c0["ant_bias"] in (False, True), "log + ant_bias reported before init")
    check(len(json.dumps(c0)) < 1400, "fits one datagram")
    a = await svc._apply(Cmd(1, "init", INIT))
    check(a.status == CmdStatus.OK, f"init OK ({a.result})")
    for cid, name, p in ((2, "set_tx_atten", {"qdb": 60}), (3, "set_center", {"hz": 426_000_000}),
                         (4, "set_gain", {"index": 230})):
        a = await svc._apply(Cmd(cid, name, p))
        check(a.status == CmdStatus.OK, f"{name} OK")
    c = await _cfg(svc)
    check(c["inited"] and c["clock_source"] == "gpsdo", "inited, clock gpsdo")
    check(c["center_hz"] == 426_000_000 and c["sample_rate"] == 7_680_000, "live centre + rate")
    check(c["rx_gain"] == 230 and c["tx_gain"] == 15.0, f"live gain 230 / TX atten 15 ({c['rx_gain']}, {c['tx_gain']})")
    check(c["card"] == 0 and c["tx_card"] == 1 and c["tx_antenna"] == "TXA1", "cards + handles")
    check(c["psd"] == {"nfft": 4096, "window": "hann", "buffer": 65536}, f"psd {c['psd']}")
    check(len(json.dumps(c)) < 1400, f"fits one datagram ({len(json.dumps(c))} B)")
    op = vs.op_fields(c)
    check(op == {"center_mhz": 426.0, "rate": 7_680_000.0, "clk": "gpsdo", "rx_port": "TRx1A",
                 "tx_port": "TRx1B", "rx_gain": "230", "tx_atten": "15"}, f"op_fields {op}")
    eng = vs.eng_fields(c)
    check(eng["cf"] == "426000000" and eng["txcard"] == 1 and eng["txatten"] == "15"
          and eng["mode"] == vs.ENG_CROSS_MODE and eng.get("txant") == "TXA1", f"eng_fields {eng}")
    # _apply is called directly here, so cfg_seq is counted in _apply_and_ack:
    # drive two commands through it the way the endpoint does
    seq0 = c["cfg_seq"]
    await svc._apply_and_ack(Cmd(10, "get_config", {}), ("127.0.0.1", 1))
    await svc._apply_and_ack(Cmd(11, "set_center", {"hz": 427_000_000}), ("127.0.0.1", 1))
    c = await _cfg(svc)
    check(c["cfg_seq"] == seq0 + 1, f"cfg_seq counts set_center, not get_config ({seq0} -> {c['cfg_seq']})")

    print("[2] TX waveform, logging, close")
    await svc._apply(Cmd(20, "set_tx_waveform", {"kind": "tone", "offset_hz": 125_000, "ampl": 0.4}))
    with tempfile.TemporaryDirectory(dir=str(ROOT)) as td:
        rel = os.path.relpath(os.path.join(td, "det.csv"), str(ROOT))
        a = await svc._apply(Cmd(21, "log_detections", {"enable": True, "path": rel}))
        check(a.status == CmdStatus.OK, f"log_detections on ({a.result})")
        c = await _cfg(svc)
        check(c["tx_wf"]["mode"] == "tone" and c["tx_wf"]["offset_hz"] == 125_000, f"tx_wf {c.get('tx_wf')}")
        check(str(c["log"]["detections"]).endswith("det.csv"), f"log {c['log']}")
        check("125" in vs.tx_wf_text(c) and "detections" in vs.log_text(c), "readout texts")
        await svc._apply(Cmd(22, "log_detections", {"enable": False}))
    await svc._apply(Cmd(23, "close", {}))
    c = await _cfg(svc)
    check(c["inited"] is False and vs.op_fields(c) == {}, "closed: inited False, no widget values")

    print("[3] ACK_PER_SENDER")
    svc2 = _svc()
    svc2.loop = asyncio.get_running_loop()
    svc2.ep = _Ep()
    a1, a2 = ("10.0.0.1", 5000), ("10.0.0.2", 5000)
    await svc2._apply_and_ack(Cmd(77, "set_gain", {"index": 200}), a1)    # rejected: not inited
    svc2._on_cmd(Cmd(77, "get_config", {}), a2)                          # same id, other sender
    await asyncio.sleep(0.05)
    got = [m for m, ad in svc2.ep.sent if ad == a2]
    check(got and "cfg_seq" in (got[-1].result or {}), "same cmd_id from another sender is applied, not replayed")
    n = len(svc2.ep.sent)
    svc2._on_cmd(Cmd(77, "get_config", {}), a2)
    check(len(svc2.ep.sent) == n + 1 and svc2.ep.sent[-1][0] is got[-1], "a duplicate from the same sender is replayed")
    for i in range(ds.APPLIED_MAX + 50):
        await svc2._apply_and_ack(Cmd(1000 + i, "get_config", {}), a1)
    check(len(svc2.applied) <= ds.APPLIED_MAX, f"ack store bounded ({len(svc2.applied)})")

    print("[4] CARVE_RATE")
    with tempfile.TemporaryDirectory(dir=str(ROOT)) as td:
        iq = (0.1 * np.exp(2j * np.pi * 0.01 * np.arange(20000))).astype(np.complex64)
        out, meta = rx.carve(iq, 1_920_000.0, 425e6, 425.1e6, 425.2e6)
        c64 = os.path.join(td, "r.c64")
        rx.write_capture(c64, out, meta, "c64")
        binp = os.path.join(td, "r.bin")
        rx.write_capture(binp, out, meta, "dpmg")
        check(svc._iq_file_rate(c64)[0] == meta.out_rate_hz, "c64 sidecar rate")
        check(svc._iq_file_rate(binp)[0] == round(meta.out_rate_hz), "DPMG header rate")
        cm = svc._carve_meta(c64)
        check(abs(cm["center"] - 425.15e6) < 1, f"c64 band centre {cm.get('center')}")
        await svc._apply(Cmd(30, "init", dict(INIT, sample_rate=1_920_000)))
        a = await svc._apply(Cmd(31, "set_tx_waveform", {"kind": "file", "path": os.path.relpath(c64, str(ROOT))}))
        r = a.result or {}
        check(a.status == CmdStatus.OK and r.get("file_rate_hz") == meta.out_rate_hz
              and abs((r.get("file_center_hz") or 0) - 425.15e6) < 1,
              f"set_tx_waveform reports the carved rate + centre ({r})")
        await svc._apply(Cmd(32, "close", {}))

    print("[5] UPLOAD_NAME_FIX")
    for name, want in (("short.c64", "short.c64"),
                       ("gui_chk_upload_2026-10-18_2157.c64", "gui_chk_upload_2026-10-18_21.c64"),
                       ("a_very_long_scenario_name_here.iq.bin", "a_very_long_scenario_name.iq.bin")):
        w = wire_file_name(name)
        check(w == want and len(w.encode()) <= 32, f"{name} -> {w}")

    print("[6] PSD_STALE_FIX")
    svc3 = _svc()
    svc3.loop = asyncio.get_running_loop()

    class _F:
        def __init__(self, seq):
            self.samples = np.ones(1024, np.complex64)
            self.sample_rate, self.center_freq, self.seq, self.rf_timestamp = 1.92e6, 425e6, seq, seq

    class _Sched:
        def __init__(self):
            self.f = _F(1)

        def latest(self):
            return self.f
    built = []
    svc3.sched, svc3.psd, svc3.gui_addrs = _Sched(), object(), {("127.0.0.1", 9)}
    svc3.ep = _Ep()
    orig = ds._build_psd_frame
    ds._build_psd_frame = lambda rec, fr, mx: built.append(fr.seq)
    try:
        t = asyncio.create_task(svc3._psd_loop())
        await asyncio.sleep(ds.PSD_INTERVAL_S * 4.5)
        n1 = len(built)
        svc3.sched.f = _F(2)
        await asyncio.sleep(ds.PSD_INTERVAL_S * 2.5)
        t.cancel()
    finally:
        ds._build_psd_frame = orig
    check(n1 == 1 and built == [1, 2], f"one PSD per new frame ({built})")


async def _cap_lock_case():
    print("[7] CAP_LOCK: two captures at once both complete")
    svc = _svc()
    svc.loop = asyncio.get_running_loop()
    svc.ep = _Ep()
    a = await svc._apply(Cmd(1, "init", dict(INIT, sample_rate=1_920_000)))
    await svc._apply(Cmd(2, "start_rx", {}))
    try:
        r1, r2 = await asyncio.gather(svc._grab_rx(0.05), svc._grab_rx(0.05))
        check(r1 is not None and r2 is not None and len(r1[0]) > 0 and len(r2[0]) > 0,
              "both captures returned IQ, no clobbered buffer")
        check(svc._cap is None and not svc._cap_lock.locked(), "capture state released")
    except Exception as e:
        check(False, f"concurrent capture raised {type(e).__name__}: {e}")
    await svc._apply(Cmd(3, "close", {}))


def main():
    asyncio.run(_cases())
    asyncio.run(_cap_lock_case())
    print(f"\n{len(FAILS)} failure(s)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
