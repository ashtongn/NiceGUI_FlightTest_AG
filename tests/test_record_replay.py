#!/usr/bin/env python3
"""
Record & replay: iq_record.py + the radio service commands,
offline on the simulated radio
==========================================================================
  1. limits: record cap per rate (5 s at low rates, REC_MAX_SAMPLES at high)
  2. selected band: a tone inside the box survives, a DC spur and an
     out-of-band tone are rejected; rebuilt for TX it lands back at its RF
     offset, also when the TX LO differs from the capture centre
  3. full band: replays sample-exact at the same rate / LO; a far-off TX LO
     is refused; a band outside the span is refused
  4. gain: 0 dB keeps the captured level, a large request is capped at DAC
     full scale (peak <= PEAK_LIMIT_DBFS), "match received power" is a hook
     that returns None without a calibration
  5. files: safe names (no traversal), prune keeps the newest N within the
     byte budget and never removes a protected one, compact list entries
  6. service: record_iq full + band on the simulator (file size, rate,
     sidecar, warnings), list_recordings fits one datagram, replay_recording
     arms the TX buffer (capped gain), start_tx once/loop, re-arm after the
     buffer is lost, match_rx refused, delete (refused while it transmits),
     prune on record, seconds clamped to the cap

    python3 tests/test_record_replay.py
"""
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import numpy as np                                     # noqa: E402

from dpmg.radio import dpmg_service as ds                              # noqa: E402
from dpmg.radio import iq_record as rec                                # noqa: E402
from dpmg.link import proto                                           # noqa: E402
from dpmg.link.proto import Cmd, CmdStatus, HostType             # noqa: E402

FAILS = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def _tone_power_db(x, fs, f_hz, half_bw=2_000.0):
    """Power (dB, relative to total) in +-half_bw around f_hz."""
    X = np.abs(np.fft.fft(x)) ** 2
    f = np.fft.fftfreq(len(x), 1.0 / fs)
    m = np.abs(f - f_hz) <= half_bw
    return 10 * np.log10(max(X[m].sum(), 1e-30) / X.sum())


def case_limits():
    print("[1] limits")
    check(rec.max_seconds(1.92e6) == 5.0, f"1.92 MSps cap 5 s ({rec.max_seconds(1.92e6)})")
    m30 = rec.max_seconds(30.72e6)
    check(1.0 < m30 < 1.1 and m30 * 30.72e6 <= rec.REC_MAX_SAMPLES,
          f"30.72 MSps cap {m30} s (<= {rec.REC_MAX_SAMPLES} samples)")
    check(rec.clamp_seconds(99, 30.72e6) == m30, "clamp to the cap at 30.72")
    check(rec.clamp_seconds("x", 1.92e6) == 1.0, "bad seconds -> 1 s")
    check(rec.clamp_seconds(0, 1.92e6) == rec.REC_MIN_S, "0 s -> minimum")


FS = 7_680_000.0
CENTER = 425_025_000.0


def _scene(n, seed=1):
    rng = np.random.default_rng(seed)
    k = np.arange(n)
    x = (0.30 * np.exp(2j * np.pi * 300e3 / FS * k)          # wanted, +300 kHz
         + 0.25 * np.exp(2j * np.pi * -1.2e6 / FS * k)       # other emitter
         + 0.20                                              # DC spur / LO leakage
         + 0.005 * (rng.standard_normal(n) + 1j * rng.standard_normal(n)))
    return x.astype(np.complex64)


def case_band():
    print("[2] selected band")
    x = _scene(int(0.1 * FS))
    out, meta = rec.band_record(x, FS, CENTER, CENTER + 260e3, CENTER + 340e3)
    check(meta.decim > 1 and meta.out_rate_hz >= 2 * 80e3,
          f"decimated to {meta.out_rate_hz/1e3:.1f} kHz (decim {meta.decim})")
    p_want = _tone_power_db(out, meta.out_rate_hz, 300e3 - 300e3)   # at DC of the band
    check(p_want > -0.5, f"wanted tone holds the band power ({p_want:.2f} dB)")
    md = dict(vars(meta), mode="band")
    w, info = rec.rebuild_for_tx(out, md, FS, CENTER)
    check(info["resampled"] and abs(info["offset_hz"] - 300e3) < 1,
          f"rebuild: resampled, offset {info['offset_hz']:.0f} Hz")
    check(abs(len(w) - len(x)) <= meta.decim, f"length back at the radio rate ({len(w)} vs {len(x)})")
    pw = _tone_power_db(w, FS, 300e3)
    pdc = _tone_power_db(w, FS, 0.0)
    pother = _tone_power_db(w, FS, -1.2e6)
    check(pw > -0.5, f"tone back at +300 kHz ({pw:.2f} dB)")
    check(pdc < -60, f"DC spur left out ({pdc:.1f} dB)")
    check(pother < -60, f"other emitter left out ({pother:.1f} dB)")
    # FDD-style: TX LO 100 kHz above the capture centre: mix to +200 kHz from the LO
    w2, info2 = rec.rebuild_for_tx(out, md, FS, CENTER + 100e3)
    check(abs(info2["offset_hz"] - 200e3) < 1 and _tone_power_db(w2, FS, 200e3) > -0.5,
          "TX LO +100 kHz: tone placed at +200 kHz from the LO (same RF)")
    # replay at another radio rate (1.92 MSps): still at +300 kHz
    w3, info3 = rec.rebuild_for_tx(out, md, 1.92e6, CENTER)
    check(abs(len(w3) - 0.1 * 1.92e6) < 100 and _tone_power_db(w3, 1.92e6, 300e3) > -0.5,
          f"replay at 1.92 MSps: {len(w3)} samples, tone at +300 kHz")
    try:
        rec.rebuild_for_tx(out, md, FS, CENTER + 4.2e6)
        check(False, "band beyond the TX span refused")
    except ValueError:
        check(True, "band beyond the TX span refused")
    try:
        rec.band_record(x, FS, CENTER, CENTER + 3.9e6, CENTER + 4.1e6)
        check(False, "band outside the received span refused")
    except ValueError:
        check(True, "band outside the received span refused")
    try:
        rec.band_record(x, FS, CENTER, CENTER + 1e3, CENTER + 1e3)
        check(False, "empty band refused")
    except ValueError:
        check(True, "empty band refused")


def case_full():
    print("[3] full band")
    x = _scene(20000)
    meta = dict(vars(rec.full_meta(len(x), FS, CENTER)), mode="full")
    w, info = rec.rebuild_for_tx(x, meta, FS, CENTER)
    check(np.array_equal(w, x) and not info["resampled"] and info["offset_hz"] == 0,
          "same rate + LO: sample-exact")
    w, info = rec.rebuild_for_tx(x, meta, FS, CENTER + 10e3)
    check(info["warnings"] and abs(info["offset_hz"] + 10e3) < 1,
          "TX LO 10 kHz off: shifted with a warning")
    try:
        rec.rebuild_for_tx(x, meta, FS, CENTER + 1e6)
        check(False, "full band with a far-off TX LO refused")
    except ValueError:
        check(True, "full band with a far-off TX LO refused")


def case_gain():
    print("[4] gain")
    x = _scene(20000)
    pk, _ = rec.level_dbfs(x)
    y, g = rec.apply_gain(x, 0.0)
    check(abs(g["peak_dbfs"] - pk) < 0.01 and not g["capped"], f"0 dB keeps the peak ({pk:.2f} dBFS)")
    y, g = rec.apply_gain(x, -6.0)
    check(abs(g["peak_dbfs"] - (pk - 6)) < 0.02, "-6 dB lowers the peak 6 dB")
    y, g = rec.apply_gain(x, 30.0)
    check(g["capped"] and g["peak_dbfs"] <= rec.PEAK_LIMIT_DBFS + 0.01
          and max(np.max(np.abs(y.real)), np.max(np.abs(y.imag))) < 1.0,
          f"+30 dB capped at {g['gain_db']:+.2f} dB, peak {g['peak_dbfs']} dBFS")
    y, g = rec.apply_gain(x, float("nan"))
    check(g["gain_db"] == 0.0, "NaN gain -> 0 dB")
    check(rec.match_rx_gain_db({"peak_dbfs": -10}) is None,
          "match received power: None without a calibration")


def case_files():
    print("[5] files")
    with tempfile.TemporaryDirectory() as d:
        for bad in ("../x", "..", ".hidden", "a b", "x/../../y", "a\\b", ""):
            try:
                p = rec.rec_path(d, bad)
                check(os.path.dirname(p) == d and False, f"bad name '{bad}' refused")
            except ValueError:
                check(True, f"bad name '{bad}' refused")
        check(rec.safe_label("a/b c..") == "a_b_c", "label sanitised")
        names = []
        for i in range(5):
            nm = f"rec_t{i}"
            x = np.full(1000 * (i + 1), 0.1 + 0j, dtype=np.complex64)
            rec.write_recording(d, nm, x, rec.full_meta(len(x), 1e6, CENTER), {"mode": "full"})
            t = time.time() - 100 + i                     # oldest first
            os.utime(rec.rec_path(d, nm), (t, t))
            names.append(nm)
        lst = rec.list_recordings(d)
        check([m["name"] for m in lst] == names[::-1], "list newest first")
        m0 = rec.read_meta(d, "rec_t2")
        check(m0["n"] == 3000 and m0["size_bytes"] == 24000 and m0["rate_hz"] == 1e6
              and abs(m0["peak_dbfs"] + 20) < 0.01 and m0["out_rate_hz"] == 1e6,
              "sidecar: n, size, rate, peak, CarveMeta fields")
        removed = rec.prune(d, keep=3, max_bytes=10**9, protect=("rec_t0",))
        check(sorted(removed) == ["rec_t1"] and os.path.exists(rec.rec_path(d, "rec_t0")),
              f"keep 3 + protected oldest: removed {removed}")
        removed = rec.prune(d, keep=10, max_bytes=40000 + 32000)
        left = [m["name"] for m in rec.list_recordings(d)]
        check(left == ["rec_t4", "rec_t3"], f"byte budget keeps the newest that fit ({left})")
        b = rec.brief(rec.read_meta(d, "rec_t4"))
        check(set(b) == {"n", "s", "r", "b", "p", "m", "c", "w"} and b["w"] is None,
              f"brief: {b}")
        check(rec.delete_recording(d, "rec_t3") and not rec.delete_recording(d, "rec_t3"),
              "delete once, then gone")


# service

class _Ep:
    def send(self, msg, addr):
        pass


INIT = {"kind": "g40", "sample_rate": 1_920_000, "center_freq": 430_500_000,
        "rx_gain": 220, "tx_gain": 10.0, "clock_source": "internal",
        "rx_antenna": "RXA1", "tx_antenna": "TXA1",
        "extras": {"card": 0, "chan_mode": "single", "tx_center_freq": 430_500_000}}


def _ack_size(a):
    return len(proto.encode(a, 1))


async def case_service(tmp):
    print("[6] service on the simulator")
    ds.DATA_DIR = tmp
    svc = ds.RadioService(serial="G40-SIM", host_type=HostType.G40_RX, kind="g40",
                          simulate=True, cmd_port=47896, discovery_port=47897,
                          broadcast="127.0.0.1")
    svc.loop = asyncio.get_running_loop()
    svc.ep = _Ep()
    a = await svc._apply(Cmd(1, "record_iq", {"seconds": 0.1}))
    check(a.status == CmdStatus.REJECTED, "record before init refused")
    a = await svc._apply(Cmd(2, "init", INIT))
    check(a.status == CmdStatus.OK, f"init ({a.result.get('err') if a.status else 'ok'})")
    a = await svc._apply(Cmd(3, "start_rx", {}))
    fs = 1_920_000.0
    a = await svc._apply(Cmd(4, "record_iq", {"seconds": 0.1, "label": "full"}))
    r = a.result or {}
    m = r.get("rec") or {}
    check(a.status == CmdStatus.OK and m.get("m") == "full",
          f"record full band ({r.get('err') or m.get('n')})")
    n_exp = int(round(0.1 * fs))
    check(m.get("r") == fs and m.get("b") == n_exp * 8 and abs(m.get("s", 0) - 0.1) < 1e-6,
          f"full: {m.get('b')} B at {m.get('r')} Hz, {m.get('s')} s")
    check(any("DC spur" in w for w in r.get("warnings") or []), "full band warns about the DC spur")
    check(_ack_size(a) < proto.MAX_DATAGRAM, f"record ack {_ack_size(a)} B")
    full_name = m.get("n")
    rd = os.path.join(tmp, rec.REC_SUBDIR)
    side = rec.read_meta(rd, full_name)
    check(side.get("rx_gain_index") == 220 and side.get("rx_port") == "RXA1"
          and side.get("src_center_hz") == 430_500_000,
          "sidecar keeps RX gain index, port, centre (for a later power match)")
    # the mock's +0.18 fs tone = +345.6 kHz
    f_t = 0.18 * fs
    band = [INIT["center_freq"] + f_t - 40e3, INIT["center_freq"] + f_t + 40e3]
    a = await svc._apply(Cmd(5, "record_iq", {"seconds": 0.2, "band": band, "label": "band"}))
    r = a.result or {}
    m = r.get("rec") or {}
    check(a.status == CmdStatus.OK and m.get("m") == "band" and m.get("r") < 300e3
          and abs(m.get("c") - (INIT["center_freq"] + f_t)) < 1,
          f"record band: {m.get('r', 0)/1e3:.1f} kSps at {m.get('c')}")
    band_name = m.get("n")
    # The live spot check: record by name, then replay by that name
    a = await svc._apply(Cmd(50, "record_iq", {"seconds": 0.05, "name": "spotcheck"}))
    r = a.result or {}
    check(a.status == CmdStatus.OK and r.get("name") == "spotcheck"
          and r.get("path", "").endswith("/spotcheck.c64") and os.path.exists(r["path"])
          and r.get("n") == int(round(0.05 * fs)) and r.get("rate_hz") == fs
          and r.get("size_bytes") == r.get("n") * 8 and r.get("peak_dbfs") is not None,
          f"record by name: top-level fields {sorted(k for k in r if k != 'rec')}")
    check(_ack_size(a) < proto.MAX_DATAGRAM, f"named record ack {_ack_size(a)} B")
    a = await svc._apply(Cmd(51, "replay_recording", {"name": "spotcheck", "gain_db": 0.0}))
    check(a.status == CmdStatus.OK, f"replay by that name ({a.result.get('err', 'ok')})")
    a = await svc._apply(Cmd(52, "record_iq", {"seconds": 0.1, "name": "spotcheck"}))
    check(a.status == CmdStatus.OK and a.result.get("n") == int(round(0.1 * fs))
          and len([m for m in rec.list_recordings(rd) if m["name"] == "spotcheck"]) == 1,
          "same name again replaces it")
    for bad in ("../x", "a b", "x" * 41):
        a = await svc._apply(Cmd(53, "record_iq", {"seconds": 0.05, "name": bad}))
        check(a.status == CmdStatus.REJECTED, f"bad name '{bad[:12]}' refused before capture")
    await svc._apply(Cmd(54, "delete_recording", {"name": "spotcheck"}))
    a = await svc._apply(Cmd(6, "record_iq", {"seconds": 0.1, "band": [5, 1]}))
    check(a.status == CmdStatus.REJECTED, "inverted band refused")
    a = await svc._apply(Cmd(7, "record_iq", {"seconds": 0.1,
                                              "band": [INIT["center_freq"] + 2e6,
                                                       INIT["center_freq"] + 2.1e6]}))
    check(a.status == CmdStatus.REJECTED, f"band outside the span refused ({a.result.get('err')})")

    a = await svc._apply(Cmd(8, "list_recordings", {}))
    r = a.result or {}
    check(a.status == CmdStatus.OK and [x["n"] for x in r["recs"]] == [band_name, full_name]
          and r["max_s"] == 5.0 and r["rate"] == fs,
          f"list: {[x['n'] for x in r.get('recs', [])]}, max {r.get('max_s')} s")
    check(_ack_size(a) < proto.MAX_DATAGRAM, f"list ack {_ack_size(a)} B")

    a = await svc._apply(Cmd(9, "replay_recording", {"name": band_name, "gain_db": 40}))
    r = a.result or {}
    check(a.status == CmdStatus.OK and r.get("capped") and r["peak_dbfs"] <= rec.PEAK_LIMIT_DBFS + 0.01,
          f"replay band +40 dB: capped at {r.get('gain_db')} dB, peak {r.get('peak_dbfs')}")
    wave = svc.sched._tx_wave
    check(wave is not None and abs(len(wave) - 0.2 * fs) < 300
          and _tone_power_db(wave, fs, f_t, 5e3) > -3,
          f"TX buffer {len(wave) if wave is not None else None} samp, tone at +{f_t/1e3:.1f} kHz")
    check(_tone_power_db(wave, fs, -f_t, 5e3) < -40 and _tone_power_db(wave, fs, 0.06 * fs, 5e3) < -40,
          "other mock tones left out of the band replay")
    check(_ack_size(a) < proto.MAX_DATAGRAM, f"replay ack {_ack_size(a)} B")
    a = await svc._apply(Cmd(10, "replay_recording", {"name": full_name, "gain_db": 0}))
    r = a.result or {}
    raw = np.fromfile(rec.rec_path(rd, full_name), dtype=np.complex64)
    # the mock's tones are at |x| 1.0 + noise, so its I/Q peak sits above full
    # scale: 0 dB is capped there; a real ADC capture never exceeds 0 dBFS
    pk_raw, _ = rec.level_dbfs(raw)
    exp = raw if pk_raw <= rec.PEAK_LIMIT_DBFS else raw * np.float32(10 ** (r["gain_db"] / 20))
    check(a.status == CmdStatus.OK and r["capped"] == (pk_raw > rec.PEAK_LIMIT_DBFS)
          and np.allclose(svc.sched._tx_wave, exp, rtol=2e-3, atol=1e-5),
          f"replay full 0 dB: TX buffer == recording (peak {pk_raw:.2f} dBFS, gain {r['gain_db']})")
    # a real-ADC-like capture (|I|,|Q| <= 1) replays at 0 dB unchanged
    with open(rec.rec_path(rd, full_name), "wb") as fh:
        (np.clip(raw.real, -0.9, 0.9) + 1j * np.clip(raw.imag, -0.9, 0.9)) \
            .astype(np.complex64).tofile(fh)
    raw = np.fromfile(rec.rec_path(rd, full_name), dtype=np.complex64)
    a = await svc._apply(Cmd(110, "replay_recording", {"name": full_name, "gain_db": 0}))
    check(a.status == CmdStatus.OK and not a.result["capped"]
          and np.array_equal(svc.sched._tx_wave, raw),
          "in-range capture at 0 dB: TX buffer == recording, not capped")
    a = await svc._apply(Cmd(11, "replay_recording", {"name": full_name, "level_mode": "match_rx"}))
    check(a.status == CmdStatus.REJECTED and a.result.get("needs_cal"),
          "match received power refused (needs calibration)")
    a = await svc._apply(Cmd(12, "replay_recording", {"name": "../../etc/passwd"}))
    check(a.status == CmdStatus.REJECTED, "traversal name refused")
    a = await svc._apply(Cmd(13, "replay_recording", {"name": "rec_nope"}))
    check(a.status == CmdStatus.REJECTED, "unknown recording refused")

    # re-arm: buffer lost (as after a re-init): start_tx rebuilds from the recording
    await svc._apply(Cmd(14, "replay_recording", {"name": full_name, "gain_db": -6}))
    svc.sched._tx_wave = None
    a = await svc._apply(Cmd(15, "start_tx", {"mode": "CONTINUOUS"}))
    check(a.status == CmdStatus.OK and svc.sched._tx_wave is not None
          and abs(np.max(np.abs(svc.sched._tx_wave)) - np.max(np.abs(raw)) * 10 ** (-6 / 20)) < 1e-4,
          f"start_tx loop re-armed from the recording at -6 dB ({a.result.get('err')})")
    a = await svc._apply(Cmd(16, "delete_recording", {"name": full_name}))
    check(a.status == CmdStatus.REJECTED, "delete refused while it transmits")
    a = await svc._apply(Cmd(17, "replay_recording", {"name": band_name}))
    check(a.status == CmdStatus.REJECTED, "load refused while TX is live")
    c = (await svc._apply(Cmd(18, "get_config", {}))).result
    check((c.get("tx_wf") or {}).get("mode") == "record" and c.get("tx_active"),
          f"get_config shows the recording keyed ({(c.get('tx_wf') or {}).get('desc')})")
    await svc._apply(Cmd(19, "stop_tx", {}))
    a = await svc._apply(Cmd(20, "start_tx", {"mode": "SINGLE_SHOT"}))
    check(a.status == CmdStatus.OK and a.result.get("tx") in ("sent", "streaming"),
          f"start_tx once: short buffer reported sent, not a dead thread "
          f"({a.result.get('tx') or a.result.get('err')})")
    await svc._apply(Cmd(21, "stop_tx", {}))
    a = await svc._apply(Cmd(22, "delete_recording", {"name": full_name}))
    check(a.status == CmdStatus.OK and not os.path.exists(rec.rec_path(rd, full_name)),
          "delete after STOP")

    # prune on record: keep 2, the armed band recording is protected
    keep0 = rec.REC_KEEP
    rec.REC_KEEP = 2
    try:
        await svc._apply(Cmd(23, "replay_recording", {"name": band_name}))
        got = []
        for i in range(3):
            a = await svc._apply(Cmd(30 + i, "record_iq", {"seconds": 0.02, "label": f"p{i}"}))
            got.append((a.result.get("rec") or {}).get("n"))
            time.sleep(1.05)                              # names carry the second
        left = [m["name"] for m in rec.list_recordings(rd)]
        check(band_name in left and len(left) <= 3 and got[-1] in left,
              f"prune keeps the newest + the armed one ({left})")
    finally:
        rec.REC_KEEP = keep0

    # seconds clamp at a high rate (sim at 30.72 is heavy: only check the cap echo)
    a = await svc._apply(Cmd(40, "init", dict(INIT, sample_rate=30_720_000)))
    a = await svc._apply(Cmd(41, "list_recordings", {}))
    check(a.result.get("max_s") == rec.max_seconds(30.72e6),
          f"30.72 MSps: max {a.result.get('max_s')} s")
    a = await svc._apply(Cmd(42, "replay_recording", {"name": band_name}))
    r = a.result or {}
    check(a.status == CmdStatus.OK and abs(r["n"] - 0.2 * 30.72e6) < 3000,
          f"band recording replays at 30.72 MSps ({r.get('n')} samp)")
    await svc._apply(Cmd(43, "close", {}))


def main():
    case_limits()
    case_band()
    case_full()
    case_gain()
    case_files()
    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(case_service(tmp))
    print(f"\n{'PASS' if not FAILS else 'FAIL'}: {len(FAILS)} failure(s)")
    for f in FAILS:
        print("  -", f)
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
