#!/usr/bin/env python3
"""
test_fpga_image.py: FPGA image readback and loading through the service,
with a simulated X410 standing in for the UHD tools. No hardware.

  1. parsers: uhd_find_devices blocks, uhd_usrp_probe version, device args
  2. image choices from an images folder (bitstream and device tree needed)
  3. x4xx: fpga_status reports the image, the choices and loadable
  4. x4xx load: refused without confirm, with an unknown image, while TX
     runs; a good load closes the radio, waits for it to come back, reads the
     image back and probes; the radio refuses init during the load
  5. x4xx load failures: the loader fails, the radio comes back with the wrong
     image, the probe fails; each is reported as failed with the reason
  6. b2xx: the chosen file rides the device args at the next open, UHD
     default removes it
  7. epiq (G40): read only, fpga_load refused; get_config carries the brief
  8. the X410 personality follows the loaded image: caps per image, init
     refused for an image with no table, for an image that changed under the
     controls, and for a rate the link cannot carry; caps refresh after a load

    python3 tests/test_fpga_image.py
"""
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
_TMP = tempfile.mkdtemp(prefix="dpmg_fpga_")
os.environ["DPMG_DATA_DIR"] = os.path.join(_TMP, "data")
IMG = os.path.join(_TMP, "images")
os.makedirs(IMG)
for n in ("X4_200", "CG_400", "UC_200"):
    for ext in (".bin", ".dts"):
        Path(IMG, f"usrp_x410_fpga_{n}{ext}").write_bytes(b"x")
Path(IMG, "usrp_x410_fpga_X4_400.bin").write_bytes(b"x")     # no .dts: not offered
for n in ("usrp_b200mini_fpga.bin", "usrp_b205mini_fpga.bin"):
    Path(IMG, n).write_bytes(b"x")
os.environ["DPMG_UHD_IMAGES"] = IMG
os.environ["DPMG_UHD_ARGS"] = "type=x4xx,addr=192.168.8.25"

from dpmg.radio import dpmg_service as ds                              # noqa: E402
from dpmg.radio import fpga_image as fi                                # noqa: E402
from dpmg.radio import radio_role as rr                                # noqa: E402
from dpmg.link.proto import Cmd, CmdStatus, HostType             # noqa: E402
from dpmg.link import proto                                           # noqa: E402

FAILS = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def fits(a):
    try:
        return len(proto.encode(a, 1)) <= proto.MAX_DATAGRAM
    except proto.ProtoError:
        return False


FIND = """[INFO] [UHD] linux; GNU C++ version 11.4.0; UHD_4.11.0.0
--------------------------------------------------
-- UHD Device 0
--------------------------------------------------
Device Address:
    serial: 1234ABC
    addr: 192.168.8.25
    claimed: False
    fpga: X4_200
    mgmt_addr: 192.168.8.25
    name: ni-x4xx-1234ABC
    product: x410
    type: x4xx
"""


def parsers():
    print("[1] parsers")
    devs = fi.parse_find(FIND)
    check(len(devs) == 1 and devs[0]["fpga"] == "X4_200", "find: one device, fpga X4_200")
    check(fi.find_device(devs, addr="192.168.8.25") is devs[0], "find by address")
    check(fi.find_device(devs, addr="10.0.0.1") is None, "a different address is not matched")
    check(fi.parse_find("No UHD Devices Found") == [], "no devices")
    pr = fi.parse_probe("|   |   FPGA Version: 11.0\n|   |   FPGA git hash: 881f19d\n")
    check(pr == {"version": "11.0", "git_hash": "881f19d"}, f"probe version and hash ({pr})")
    a = fi.args_set("type=x4xx,addr=1.2.3.4", "fpga", "CG_400")
    check(fi.args_get(a, "fpga") == "CG_400" and fi.args_get(a, "addr") == "1.2.3.4", "args set/get")
    check(fi.args_get(fi.args_set(a, "fpga", None), "fpga") is None, "args remove")
    check(fi.load_cmd("type=x4xx,addr=1.2.3.4", "UC_200") ==
          ["uhd_image_loader", "--args", "type=x4xx,addr=1.2.3.4,fpga=UC_200"], "loader command")
    check(fi.family("x410") == "x4xx" and fi.family("b200") == "b2xx"
          and fi.family("g40") == "epiq" and fi.family("hackrf") is None, "families by kind")

    print("[2] choices")
    check(fi.x4xx_choices(IMG) == ["CG_400", "UC_200", "X4_200"],
          f"x410 images with a device tree ({fi.x4xx_choices(IMG)})")
    check(fi.b2xx_choices(IMG) == ["usrp_b200mini_fpga.bin", "usrp_b205mini_fpga.bin"],
          "b200 family bitstreams")


def svc_for(kind, serial, runner=None, simulate=True):
    s = ds.RadioService(serial=serial, host_type=HostType.OTHER, kind=kind, simulate=simulate,
                        cmd_port=48130, discovery_port=48131, broadcast="127.0.0.1",
                        fpga_runner=runner)
    s.loop = asyncio.get_running_loop()
    return s


async def wait_job(s, timeout=20):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if not s._fpga_busy():
            return s._fpga_job
        await asyncio.sleep(0.05)
    return s._fpga_job


INIT_X = {"kind": "x410", "sample_rate": 1_920_000, "center_freq": 915e6, "rx_gain": 30,
          "tx_gain": 0.0, "clock_source": "internal", "rx_antenna": "RX0", "tx_antenna": "TX0",
          "device_args": "type=x4xx,addr=192.168.8.25", "extras": {}}


async def x4xx():
    print("[3] x4xx status")
    sim = fi.SimX4xx(down_s=0.3, load_s=0.1)
    s = svc_for("x410", "X410-T1", sim)
    a = await s._apply(Cmd(1, "fpga_status", {}))
    r = a.result
    check(a.status == CmdStatus.OK and r["family"] == "x4xx" and r["image"] == "X4_200",
          f"status: x4xx running X4_200 ({r.get('image')})")
    check(r["choices"] == ["CG_400", "UC_200", "X4_200"] and r["loadable"], "choices and loadable")
    check(fits(a), "fpga_status fits one datagram")
    a = await s._apply(Cmd(2, "fpga_status", {"probe": True}))
    check(a.result.get("version") == "11.0", "probe with the radio closed reads the version")

    print("[4] x4xx load")
    a = await s._apply(Cmd(10, "fpga_load", {"image": "CG_400"}))
    check(a.status == CmdStatus.REJECTED and "confirm" in a.result["err"], "refused without confirm")
    a = await s._apply(Cmd(11, "fpga_load", {"image": "X4_400", "confirm": "X4_400"}))
    check(a.status == CmdStatus.REJECTED, "an image without its device tree is refused")
    a = await s._apply(Cmd(12, "init", INIT_X))
    check(a.status == CmdStatus.OK, f"init the simulated X410 ({(a.result or {}).get('err', 'ok')})")
    await s._apply(Cmd(13, "start_rx", {}))
    a = await s._apply(Cmd(14, "set_tx_waveform", {"mode": "tone", "offset_hz": 1e5, "ampl": 0.1}))
    a = await s._apply(Cmd(15, "start_tx", {"mode": "CONTINUOUS"}))
    check(a.status == CmdStatus.OK, f"TX keyed ({(a.result or {}).get('err', 'ok')})")
    a = await s._apply(Cmd(16, "fpga_load", {"image": "CG_400", "confirm": "CG_400"}))
    check(a.status == CmdStatus.REJECTED and "TX" in a.result["err"], "refused while TX runs")
    await s._apply(Cmd(17, "stop_tx", {}))
    a = await s._apply(Cmd(18, "fpga_load", {"image": "CG_400", "confirm": "CG_400"}))
    check(a.status == CmdStatus.OK and a.result.get("prev") == "X4_200", "load CG_400 starts")
    await asyncio.sleep(0.02)
    a = await s._apply(Cmd(19, "init", INIT_X))
    check(a.status == CmdStatus.REJECTED and a.result.get("fpga_busy"), "init refused during the load")
    a = await s._apply(Cmd(20, "fpga_load", {"image": "UC_200", "confirm": "UC_200"}))
    check(a.status == CmdStatus.REJECTED, "a second load is refused")
    a = await s._apply(Cmd(21, "stop_tx", {}))
    check(a.status == CmdStatus.OK, "stop_tx still passes during the load")
    j = await wait_job(s)
    check(j["state"] == "done", f"load done ({j.get('err')})")
    check(j["result"]["image"] == "CG_400" and j["result"]["probe"] == "ok",
          f"readback CG_400 and probe ok ({j['result']})")
    check(s.backend is None, "the radio was closed for the load and left closed")
    tools = [c[0] for c in sim.calls]
    check("uhd_image_loader" in tools and tools.index("uhd_image_loader") < len(tools) - 2
          and tools[-1] == "uhd_usrp_probe", f"order: load, find until back, probe ({tools[-4:]})")
    a = await s._apply(Cmd(22, "fpga_status", {"refresh": True}))
    check(a.result["image"] == "CG_400" and a.result["job"]["state"] == "done", "status after the load")
    check(fits(a), "fpga_status with a job fits one datagram")
    a = await s._apply(Cmd(23, "get_config", {}))
    check((a.result.get("fpga") or {}).get("img") == "CG_400", "get_config brief shows CG_400")
    a = await s._apply(Cmd(24, "fpga_load", {"image": "X4_200", "confirm": "X4_200"}))
    j = await wait_job(s)
    check(j["state"] == "done" and j["result"]["image"] == "X4_200", "load X4_200 back")

    print("[5] x4xx load failures")
    for label, sim2, want in (
            ("loader fails", fi.SimX4xx(fail_load=True, load_s=0.0), "uhd_image_loader"),
            ("wrong image after load", fi.SimX4xx(land_on="X4_200", down_s=0.1, load_s=0.0), "expected"),
            ("probe fails", fi.SimX4xx(probe_rc=1, down_s=0.1, load_s=0.0), "probe")):
        s2 = svc_for("x410", "X410-F", sim2)
        a = await s2._apply(Cmd(1, "fpga_load", {"image": "UC_200", "confirm": "UC_200"}))
        j = await wait_job(s2)
        check(j["state"] == "failed" and want in (j.get("err") or ""), f"{label}: failed ({j.get('err')})")
    s3 = svc_for("x410", "X410-NOADDR", fi.SimX4xx(), simulate=False)
    os.environ.pop("DPMG_UHD_ARGS")
    a = await s3._apply(Cmd(1, "fpga_load", {"image": "UC_200", "confirm": "UC_200"}))
    check(a.status == CmdStatus.REJECTED and "address" in a.result["err"], "no address: refused")
    os.environ["DPMG_UHD_ARGS"] = "type=x4xx,addr=192.168.8.25"

    print("[6] b2xx")
    s = svc_for("b200", "B200-T")
    a = await s._apply(Cmd(1, "fpga_status", {}))
    r = a.result
    check(r["family"] == "b2xx" and r["image"] == "UHD default"
          and "usrp_b205mini_fpga.bin" in r["choices"], "b200: UHD default and the files")
    a = await s._apply(Cmd(2, "fpga_load", {"image": "usrp_b205mini_fpga.bin",
                                           "confirm": "usrp_b205mini_fpga.bin"}))
    j = await wait_job(s)
    check(j["state"] == "done" and j["result"]["probe"] == "pending",
          "closed radio: chosen for the next start")
    seen = {}
    real = ds.sdr.make_backend

    def spy(kind, cfg, simulate):
        seen["args"] = cfg.device_args
        return real(kind, cfg, simulate)
    ds.sdr.make_backend = spy
    try:
        a = await s._apply(Cmd(3, "init", dict(INIT_X, kind="b200", device_args="",
                                                rx_antenna="RX2", tx_antenna="TX/RX")))
        check(a.status == CmdStatus.OK, "init the simulated B200")
        check(fi.args_get(seen.get("args"), "fpga") == os.path.join(IMG, "usrp_b205mini_fpga.bin")
              and fi.args_get(seen.get("args"), "type") == "b200",
              f"the chosen file rides the device args ({seen.get('args')})")
        a = await s._apply(Cmd(4, "fpga_load", {"image": "UHD default", "confirm": "UHD default"}))
        j = await wait_job(s)
        check(j["state"] == "done" and j["result"]["probe"] == "ok" and s.backend is not None,
              f"open radio: reopened with UHD default ({j.get('err')})")
        check(fi.args_get(seen.get("args"), "fpga") is None, f"no fpga arg for UHD default ({seen.get('args')})")
    finally:
        ds.sdr.make_backend = real
        await s._teardown()

    print("[7] epiq read only")
    s = svc_for("g40", "G40-FPGA")
    a = await s._apply(Cmd(1, "fpga_status", {}))
    check(a.result["family"] == "epiq" and not a.result["loadable"], "g40: read only")
    a = await s._apply(Cmd(2, "fpga_load", {"image": "x", "confirm": "x"}))
    check(a.status == CmdStatus.REJECTED and "Epiq" in a.result["err"], "g40: fpga_load refused")
    a = await s._apply(Cmd(3, "fpga_status", {"probe": True}))
    check(a.result["version"] is None and "start the radio once" in a.result["note"],
          f"g40 closed, nothing saved yet: {a.result['note']}")
    s._fpga_save(epiq={"cards": {"0": {"version": "3.21.0", "git_hash": "abcd1234"},
                                 "1": {"version": "3.21.0"}},
                       "lib": "4.25.0", "read_t": 1793500000.0})
    a = await s._apply(Cmd(4, "fpga_status", {"probe": True}))
    check(a.result["version"] == "3.21.0" and a.result["cards"]["0"]["git_hash"] == "abcd1234"
          and a.result["lib"] == "4.25.0" and "as read at" in a.result["note"]
          and a.result.get("read_at") == 1793500000.0,
          f"g40 closed: saved versions returned with their date ({a.result})")
    s = svc_for("hackrf", "HACKRF-FPGA")
    a = await s._apply(Cmd(1, "fpga_status", {}))
    check(a.result["family"] is None and not a.result["loadable"], "hackrf: no FPGA control")
    check(rr.refusal("monitor", "fpga_status") is None, "fpga_status passes in every role")


X4_RATES = [480_000, 960_000, 1_228_800, 1_920_000, 2_457_600, 3_840_000,
            4_915_200, 6_144_000, 7_680_000, 9_830_400]


async def personality():
    print("[8] X410 personality follows the loaded image")
    check(fi.x4xx_rates("X4_200") == X4_RATES, "X4_200: 245.76 MHz / n up to the 1 GbE cap")
    check(fi.x4xx_rates("UC_200") == X4_RATES, "UC_200 measured the same as X4_200")
    check(fi.x4xx_rates("CG_400") == X4_RATES,
          "CG_400 (491.52 MHz) makes the same link rates exactly")
    check(fi.X4XX_IMAGES["CG_400"]["mcr_hz"] == 491_520_000, "CG_400 clock as measured")
    check(fi.x4xx_rates("XX_1") == [], "an image with no table offers no rates")
    check(fi.x4xx_rates("X4_200", link_max_hz=40e6)[-3:] == [15_360_000, 24_576_000, 30_720_000],
          "a faster link would offer more of the 245.76 MHz family")
    check(all(r <= fi.X4XX_LINK_MAX_HZ for r in fi.x4xx_rates("X4_200")), "rates capped by the link")
    c = fi.x4xx_caps("UC_200", {"label": "Ettus X410", "kind": "x410", "rx_gain": {"max": 60}})
    check(c["fpga_image"] == "UC_200" and c["label"] == "Ettus X410 (UC_200)"
          and c["mcr_hz"] == 245_760_000 and c["rx_channels"] == 4 and c["rx_gain"] == {"max": 60},
          "caps carry the image, clock and channels on top of the static caps")
    u = fi.x4xx_caps("XX_1", {"label": "Ettus X410 (X4_200)"})
    check(u["image_known"] is False and u["sample_rates_hz"] == [] and "XX_1" in u["note"]
          and u["label"] == "Ettus X410 (XX_1)", "unknown image: no rates, a note, image in the label")
    c4 = fi.x4xx_caps("CG_400", {"label": "Ettus X410"})
    check(c4["image_known"] and c4["mcr_hz"] == 491_520_000 and c4["label"] == "Ettus X410 (CG_400)",
          "CG_400 caps: 491.52 MHz clock")

    sim = fi.SimX4xx(image="X4_200", down_s=0.1, load_s=0.0)
    s = svc_for("x410", "X410-P", sim)
    a = await s._apply(Cmd(1, "caps", {}))
    check(a.status == CmdStatus.OK and a.result.get("fpga_image") == "X4_200"
          and a.result.get("sample_rates_hz") == X4_RATES, "caps report X4_200 and its rates")
    check(fits(a), "caps with the image fit one datagram")
    import json as _json
    n = len(_json.dumps(a.result).encode())
    check(n < 1200, f"caps with the image stay under the 1200 B budget ({n} B)")
    a = await s._apply(Cmd(2, "init", dict(INIT_X, sample_rate=30_720_000)))
    check(a.status == CmdStatus.REJECTED and "rate" in a.result["err"],
          f"init at 30.72 MS/s refused over this link ({a.result.get('err')})")
    a = await s._apply(Cmd(3, "init", INIT_X))
    check(a.status == CmdStatus.OK, "init at 1.92 MS/s with X4_200")
    await s._apply(Cmd(4, "close", {}))
    sim.image = "UC_200"                               # loaded by someone else
    s._fpga_find = (0.0, None)
    a = await s._apply(Cmd(5, "init", INIT_X))
    check(a.status == CmdStatus.REJECTED and "changed from X4_200 to UC_200" in a.result["err"]
          and a.result.get("caps_changed"), f"image changed under it: refused ({a.result.get('err')})")
    a = await s._apply(Cmd(6, "caps", {}))
    check(a.result.get("fpga_image") == "UC_200", "caps re-read report UC_200")
    a = await s._apply(Cmd(7, "init", INIT_X))
    check(a.status == CmdStatus.OK, "init again after the caps refresh")
    await s._apply(Cmd(8, "close", {}))
    sim.image = "XX_1"
    s._fpga_find = (0.0, None)
    a = await s._apply(Cmd(9, "caps", {}))
    check(a.result.get("image_known") is False, "an image with no table: caps say unknown")
    a = await s._apply(Cmd(10, "init", INIT_X))
    check(a.status == CmdStatus.REJECTED and "not in DPMG's X410 table" in a.result["err"],
          "init refused for an image with no table")
    sim.image = "CG_400"
    s._fpga_find = (0.0, None)
    a = await s._apply(Cmd(30, "caps", {}))
    check(a.result.get("fpga_image") == "CG_400" and a.result.get("mcr_hz") == 491_520_000,
          "CG_400 caps: 491.52 MHz clock")
    a = await s._apply(Cmd(31, "init", INIT_X))
    ex = (s.backend.config.extras or {}) if s.backend is not None else {}
    check(a.status == CmdStatus.OK and ex.get("fpga_mcr_hz") == 491_520_000
          and ex.get("fpga_image") == "CG_400",
          "init on CG_400 hands the backend the 491.52 MHz clock to check")
    await s._apply(Cmd(32, "close", {}))
    a = await s._apply(Cmd(11, "fpga_load", {"image": "X4_200", "confirm": "X4_200"}))
    j = await wait_job(s)
    check(j["state"] == "done" and s._caps_image == "X4_200", "after fpga_load the caps image is X4_200")
    a = await s._apply(Cmd(12, "get_config", {}))
    check((a.result.get("fpga") or {}).get("img") == "X4_200", "get_config shows the new image")
    a = await s._apply(Cmd(13, "caps", {}))
    check(a.result.get("fpga_image") == "X4_200" and a.result.get("image_known"),
          "caps follow the load without a service restart")
    a = await s._apply(Cmd(14, "init", INIT_X))
    check(a.status == CmdStatus.OK, "init with X4_200 back")
    await s._teardown()
    g = svc_for("g40", "G40-P")
    a = await g._apply(Cmd(1, "caps", {}))
    check("fpga_image" not in a.result, "a G40's caps are unchanged")


def main():
    parsers()
    asyncio.run(x4xx())
    asyncio.run(personality())
    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}")
        sys.exit(1)
    print("all passed")


if __name__ == "__main__":
    main()
