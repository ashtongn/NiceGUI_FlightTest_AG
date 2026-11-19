"""DEVICE_WATCH: atten.transport auto attaches the Mini-Circuits attenuator when
it is plugged in and drops it when it goes away (device_watch.AttenWatch), the
switch readback ADC presence probe (AdcProbe), and the service wiring: the
guard refuses keys while the attenuator is missing and accepts them once it is
attached, nothing is written on attach, reload and close stop the watcher, and
payload_status stays inside one CMD_ACK datagram.  All offline: a fake
find_hidraw, MockTransport units, a pty standing in for /dev/hidrawN and a fake
smbus."""
import asyncio
import os
import sys
import threading

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dpmg.link.proto import CmdAck, CmdStatus, MAX_DATAGRAM  # noqa: E402
from dpmg.payload import device_watch as dw  # noqa: E402
from dpmg.payload import mc_atten as mc  # noqa: E402
from dpmg.payload import rf_frontend as rf  # noqa: E402


# fakes
class Bus:
    """What the fake find_hidraw returns, and the MockTransport behind each node."""
    def __init__(self):
        self.nodes = []
        self.units = {}
        self.open_err = {}            # path: exception raised by open
        self.opened = []

    def find(self):
        return list(self.nodes)

    def plug(self, path="/dev/hidraw3", att=37.5, serial="11901234567"):
        self.nodes.append(path)
        self.units[path] = mc.MockTransport(serial=serial, att=att)
        return self.units[path]

    def unplug(self, path="/dev/hidraw3"):
        self.nodes.remove(path)

    def open(self, path):
        self.opened.append(path)
        e = self.open_err.pop(path, None)
        if e is not None:
            raise e
        return mc.MiniCircuitsAtten(self.units[path], 120.0, 0.05)


def watch(bus, path="auto", published=None):
    cfg = rf.AttenCfg(transport="auto", path=path)
    published = published if published is not None else []
    w = dw.AttenWatch(cfg, published.append, find=bus.find, open_fn=bus.open)
    return w, published


def _writes(t):
    return [c for c in t.commands if c.startswith(":SETATT") or c.startswith(":STARTUPATT")]


# AttenWatch
class TestAttenWatch:
    def test_none_appear_gone_appear(self):
        bus = Bus()
        w, pub = watch(bus)
        assert w.scan() == dw.AT_NONE and pub == [] and w.atten is None
        t = bus.plug(att=42.25)
        assert w.scan() == dw.AT_CONNECTED
        assert pub[-1] is w.atten and w.atten is not None
        st = w.status()
        assert st["model"] == "RCDAT-6G-120H" and st["serial"] == "11901234567"
        assert st["path"] == "/dev/hidraw3" and st["db"] == 42.25 and st["since"] is not None
        assert _writes(t) == []                           # read only on attach
        assert w.scan() == dw.AT_CONNECTED                 # still there: liveness read only
        assert _writes(t) == []
        bus.unplug()
        assert w.scan() == dw.AT_LOST and pub[-1] is None and w.atten is None
        assert w.scan() == dw.AT_LOST                      # stays "lost" until it is back
        t2 = bus.plug(att=10.0, serial="11900000002")
        assert w.scan() == dw.AT_CONNECTED and w.status()["serial"] == "11900000002"
        assert w.attaches == 2 and w.detaches == 1 and _writes(t2) == []

    def test_several_opens_none(self):
        bus = Bus()
        bus.plug("/dev/hidraw2"); bus.plug("/dev/hidraw5")
        w, pub = watch(bus)
        assert w.scan() == dw.AT_SEVERAL and pub == [] and bus.opened == []
        assert "set atten.path" in w.status()["state"]
        bus.unplug("/dev/hidraw2")                         # one left: attaches
        assert w.scan() == dw.AT_CONNECTED and w.path == "/dev/hidraw5"

    def test_explicit_path_picks_one(self):
        bus = Bus()
        bus.plug("/dev/hidraw2"); bus.plug("/dev/hidraw5")
        w, pub = watch(bus, path="/dev/hidraw5")
        assert w.scan() == dw.AT_CONNECTED and w.path == "/dev/hidraw5"

    def test_open_error_then_retry(self, caplog):
        bus = Bus()
        bus.plug()
        w, pub = watch(bus)
        bus.open_err["/dev/hidraw3"] = mc.AttenError("open /dev/hidraw3: Permission denied")
        with caplog.at_level("WARNING", logger="device_watch"):
            assert w.scan() == dw.AT_ERROR
            assert w.status()["state"].startswith("error: open /dev/hidraw3: Permission denied")
            bus.open_err["/dev/hidraw3"] = mc.AttenError("open /dev/hidraw3: Permission denied")
            w.scan()                                       # same reason: logged once
        assert sum("not opened" in r.message for r in caplog.records) == 1
        assert pub == []
        assert w.scan() == dw.AT_CONNECTED and pub[-1] is w.atten   # retry succeeds
        assert w.err is None

    def test_failed_ident_closes_unit(self):
        bus = Bus()
        t = bus.plug()
        t.fail_next = mc.AttenError("timeout waiting for reply to ':MN?'")
        w, pub = watch(bus)
        assert w.scan() == dw.AT_ERROR and pub == []
        assert w.scan() == dw.AT_CONNECTED

    def test_io_error_on_read_is_lost(self):
        bus = Bus()
        t = bus.plug()
        w, pub = watch(bus)
        w.scan()
        t.fail_next = OSError(19, "No such device")
        assert w.scan() == dw.AT_LOST and pub[-1] is None

    def test_one_timeout_is_tolerated(self):
        bus = Bus()
        t = bus.plug()
        w, pub = watch(bus)
        w.scan()
        t.fail_next = mc.AttenError("timeout")
        assert w.scan() == dw.AT_CONNECTED
        assert w.scan() == dw.AT_CONNECTED                 # good read resets the count
        t.fail_next = mc.AttenError("timeout")
        w.scan()
        t.fail_next = mc.AttenError("timeout")
        assert w.scan() == dw.AT_LOST

    def test_busy_lock_skips_liveness_read(self):
        bus = Bus()
        t = bus.plug()
        w, pub = watch(bus)
        w.scan()
        n = len(t.commands)
        got = threading.Event(); done = threading.Event()

        def hold():
            with w.lock:
                got.set(); done.wait(2)
        th = threading.Thread(target=hold); th.start(); got.wait(2)
        assert w.scan() == dw.AT_CONNECTED and len(t.commands) == n   # did not wait or read
        done.set(); th.join()

    def test_close_unpublishes(self):
        bus = Bus()
        bus.plug()
        w, pub = watch(bus)
        w.scan()
        w.close()
        assert pub[-1] is None and w.atten is None
        w.close()                                          # twice is harmless


class TestHidrawPty:
    """The real HidrawTransport on a pty: attach, read, then the 'device' goes away."""

    @pytest.fixture
    def fake_dev(self):
        import pty
        import tty
        master, slave = pty.openpty()
        tty.setraw(master); tty.setraw(slave)
        path = os.ttyname(slave)
        emu = mc.MockTransport(att=55.0)
        stop = threading.Event()

        def serve():
            buf = b""
            while not stop.is_set():
                try:
                    chunk = os.read(master, 65)
                except OSError:
                    return
                if not chunk:
                    return
                buf += chunk
                while len(buf) >= 65:
                    rep, buf = buf[:65], buf[65:]
                    cmd = rep[2:].split(b"\0", 1)[0].decode()
                    out = emu.scpi(cmd).encode()
                    try:
                        os.write(master, bytes([rep[1]]) + out + b"\0" * (63 - len(out)))
                    except OSError:
                        return
        th = threading.Thread(target=serve, daemon=True); th.start()
        yield path, emu, stop, (master, slave)
        stop.set()
        for fd in (slave, master):
            try: os.close(fd)
            except OSError: pass
        th.join(timeout=1.0)

    def test_attach_read_only_then_closed_fd_refuses(self, fake_dev):
        path, emu, stop, fds = fake_dev
        pub = []
        w = dw.AttenWatch(rf.AttenCfg(transport="auto", path="auto"), pub.append,
                          find=lambda: [path])
        assert w.scan() == dw.AT_CONNECTED and w.db == 55.0
        assert not any(c.startswith(":SETATT") or c.startswith(":STARTUP") for c in emu.commands)
        at = w.atten
        w._candidates = lambda: []                 # node gone
        assert w.scan() == dw.AT_LOST
        assert at.t.fd == -1
        with pytest.raises(mc.AttenError):         # never writes into a reused fd number
            at.t.scpi(":ATT?")


# AdcProbe
class FakeSMBus:
    present = {0x48}
    opened = []

    def __init__(self, n):
        FakeSMBus.opened.append(n)
        self.closed = False

    def read_i2c_block_data(self, addr, reg, n):
        assert reg == 0x01 and n == 2                # config register, read only
        if addr not in self.present:
            raise OSError(121, "Remote I/O error")
        return [0x85, 0x83]

    def write_i2c_block_data(self, *a):              # pragma: no cover
        raise AssertionError("the probe must not write")

    def close(self):
        self.closed = True


class TestAdcProbe:
    def test_present_absent(self):
        p = dw.AdcProbe("/dev/i2c-1", 0x48, smbus_cls=FakeSMBus, exists=lambda b: True)
        assert p.probe() == dw.ADC_PRESENT and FakeSMBus.opened[-1] == 1
        p2 = dw.AdcProbe("/dev/i2c-1", 0x49, smbus_cls=FakeSMBus, exists=lambda b: True)
        assert p2.probe() == dw.ADC_ABSENT
        assert p.status() == {"state": "present", "bus": "/dev/i2c-1", "address": "0x48"}

    def test_bus_missing(self):
        p = dw.AdcProbe("/dev/i2c-7", 0x48, smbus_cls=FakeSMBus, exists=lambda b: False)
        assert p.probe() == dw.ADC_NO_BUS

    def test_smbus_missing(self, monkeypatch):
        monkeypatch.setattr(dw, "_import_smbus", lambda: None)
        p = dw.AdcProbe("/dev/i2c-1", 0x48, exists=lambda b: True)
        assert p.probe() == dw.ADC_NO_SMBUS

    def test_open_error(self):
        class Denied(FakeSMBus):
            def __init__(self, n):
                raise PermissionError(13, "Permission denied")
        p = dw.AdcProbe("/dev/i2c-1", 0x48, smbus_cls=Denied, exists=lambda b: True)
        assert p.probe().startswith("error:")

    def test_watcher_skips_probe_when_not_allowed(self):
        p = dw.AdcProbe("/dev/i2c-1", 0x48, smbus_cls=FakeSMBus, exists=lambda b: True)
        allowed = {"v": False}
        d = dw.DeviceWatcher(None, p, adc_allowed=lambda: allowed["v"], adc_s=5.0)
        d.step(100.0)
        assert p.probes == 0 and p.state == dw.ADC_OFF
        allowed["v"] = True
        d.step(100.5)                                    # retried at the next tick
        assert p.probes == 1 and p.state == dw.ADC_PRESENT
        d.step(103.0)
        assert p.probes == 1                             # 5 s period
        d.step(105.6)
        assert p.probes == 2


# service
FIX = {
    "components": {
        "g40tx": {"max_output_dbm": {"value": 5}},
        "g40rx": {"max_safe_input_dbm": {"value": 0}, "noise_figure_db": {"value": 8}},
        "hpa": {"gain_db": {"value": 45}, "p1db_dbm": {"value": 48}, "psat_dbm": {"value": 50},
                "max_input_dbm": {"value": 10}},
        "sw": {"insertion_loss_db": {"value": 0.5},
               "isolation_db": {"points": [[1000, 50], [3000, 40]]},
               "power_rating_dbm": {"value": 50}},
        "ant": {"gain_dbi": {"value": 3}},
        "lna": {"gain_db": {"value": 30}, "nf_db": {"value": 1.0}, "max_input_dbm": {"value": 15}},
        "att": {"insertion_loss_db": {"value": 4}, "max_db": {"value": 120}, "step_db": {"value": 0.05}},
    },
    "profiles": {
        "PX": {"g40_tx": "g40tx", "g40_rx": "g40rx", "hpa": "hpa",
               "switch": "sw", "antenna": "ant", "lna": "lna",
               "rx_atten": "att", "band_mhz": [1000, 3000]},
    },
    "protection": {"margin_db": 10},
}
MIN_DB = 46.0


def _write_fe(tmp_path, payload=True, atten=None):
    import yaml
    prof = tmp_path / "payload_profiles.yaml"
    prof.write_text(yaml.safe_dump(FIX))
    d = {"frontend": "mock",
         "pa_enable": {"line": "cpu-gp-out1", "settle_ms": 0, "hang_ms": 0},
         "tr_switch": {"mode": "gpio", "line": "cpu-gp-out2", "tx_level": 1, "settle_ms": 0},
         "atten": atten or {"transport": "auto"}, "watchdog_ms": 500}
    if payload:
        d["payload"] = {"profile": "PX", "profiles_file": str(prof)}
    p = tmp_path / "rf_frontend.yaml"
    p.write_text(yaml.safe_dump(d))
    return str(p)


def _svc(cfg_path):
    from dpmg.radio.dpmg_service import RadioService
    from dpmg.link.proto import HostType
    return RadioService(serial="G40-DEVWATCH-TEST", host_type=HostType.G40_RX, kind="g40",
                        simulate=True, cmd_port=0, discovery_port=0, broadcast="127.0.0.1",
                        frontend_config=cfg_path)


@pytest.fixture
def bus(monkeypatch):
    b = Bus()
    monkeypatch.setattr(mc, "find_hidraw", b.find)
    monkeypatch.setattr(dw.AttenWatch, "_open_hidraw", lambda self, path: b.open(path))
    return b


def run(coro_fn):
    async def main():
        return await coro_fn()
    return asyncio.run(main())


class TestService:
    def test_auto_config_parses(self, tmp_path):
        cfg = rf.load_config(_write_fe(tmp_path), required=True)
        assert cfg.atten.transport == "auto" and cfg.atten.path == "auto"
        assert mc.make_atten(cfg.atten) is None and mc.make_atten("auto") is None

    def test_guard_refuses_until_attached(self, tmp_path, bus):
        svc = _svc(_write_fe(tmp_path))

        async def go():
            svc.loop = asyncio.get_running_loop()
            await svc._run(svc._frontend_start)
            svc._tx_atten_db = 20.0      # past the fixture's LNA TX floor, so the attenuator decides
            try:
                assert svc._devwatch is not None and svc._devwatch.running
                assert svc.atten is None and svc.guard.atten is None
                assert svc.guard.protect_db == 120.0
                st = (await svc._cmd_payload_status(1, {})).result
                assert st["devices"]["atten"]["state"] == "not connected"
                assert st["devices"]["tr_adc"]["state"] == "sim"
                r = await svc._cmd_atten_set(2, {"db": 60.0})
                assert r.status == CmdStatus.REJECTED and "not connected" in r.result["err"]
                r = await svc._cmd_payload_key(3, {"on": True})
                assert r.status == CmdStatus.FAILED and "attenuator" in r.result["err"]
                assert r.result["status"]["pa"]["keyed"] is False
                # plug it in: attached on the next scan, nothing written
                t = bus.plug(att=80.0)
                await svc._run(svc._atten_watch.scan)
                assert svc.atten is not None and svc.guard.atten is svc.atten
                assert _writes(t) == []
                st = (await svc._cmd_payload_status(4, {})).result
                a = st["devices"]["atten"]
                assert a["state"] == "connected" and a["db"] == 80.0 and a["serial"] == "11901234567"
                assert st["atten"]["att_db"] == 80.0
                fs = (await svc._cmd_frontend_status(5, {})).result
                assert fs["atten"]["state"] == "connected" and fs["atten"]["att_db"] == 80.0
                r = await svc._cmd_payload_key(6, {"on": True})
                assert r.status == CmdStatus.OK, r.result
                assert r.result["status"]["pa"]["keyed"]
                # pulled while keyed: the HPA drops and the switch goes to RX, later keys are refused
                bus.unplug()
                await svc._run(svc._atten_watch.scan)
                assert svc.atten is None and svc.guard.atten is None
                for _ in range(100):
                    if not svc.frontend._keyed:
                        break
                    await asyncio.sleep(0.01)
                assert svc.frontend._keyed is False and svc.frontend._tr_tx is False
                r = await svc._cmd_payload_key(7, {"on": False})
                assert r.status == CmdStatus.OK
                r = await svc._cmd_payload_key(8, {"on": True})
                assert r.status == CmdStatus.FAILED
                st = (await svc._cmd_payload_status(9, {})).result
                assert st["devices"]["atten"]["state"] == "lost"
                # a reading below the maximum while keyed drops the HPA too
                bus.plug(att=120.0)
                await svc._run(svc._atten_watch.scan)
                r = await svc._cmd_payload_key(10, {"on": True})
                assert r.status == CmdStatus.OK, r.result
                svc._atten_read(50.0)
                for _ in range(100):
                    if not svc.frontend._keyed:
                        break
                    await asyncio.sleep(0.01)
                assert svc.frontend._keyed is False
            finally:
                await svc._run(svc._frontend_stop)
            assert svc._devwatch is None and svc.atten is None
        run(go)

    def test_present_at_start_and_reload_stops_watcher(self, tmp_path, bus):
        t = bus.plug(att=33.0)
        svc = _svc(_write_fe(tmp_path))

        async def go():
            svc.loop = asyncio.get_running_loop()
            await svc._run(svc._frontend_start)
            try:
                assert svc.atten is not None and svc.guard.atten is svc.atten  # there at start
                dw1, at1 = svc._devwatch, svc.atten
                r = await svc._cmd_frontend_reload(1, {})
                assert r.status == CmdStatus.OK
                assert not dw1.running and at1.t is t      # old watcher stopped
                assert svc._devwatch is not dw1 and svc._devwatch.running
                assert svc.atten is not None and svc.atten is not at1
                assert _writes(t) == []
            finally:
                await svc._run(svc._frontend_stop)
            assert svc._devwatch is None and svc._atten_watch is None
        run(go)

    def test_no_guard_auto(self, tmp_path, bus):
        svc = _svc(_write_fe(tmp_path, payload=False))

        async def go():
            svc.loop = asyncio.get_running_loop()
            await svc._run(svc._frontend_start)
            try:
                bus.plug(att=20.0)
                await svc._run(svc._atten_watch.scan)
                r = await svc._cmd_atten_set(1, {"db": 25.5})
                assert r.status == CmdStatus.OK and r.result["readback_db"] == 25.5
                r = await svc._cmd_atten_get(2, {})
                assert r.status == CmdStatus.OK and r.result["att_db"] == 25.5
            finally:
                await svc._run(svc._frontend_stop)
        run(go)

    @pytest.mark.parametrize("atten,state", [
        ({"transport": "none"}, "off"),
        ({"transport": "mock"}, "connected"),
    ])
    def test_other_transports_unchanged(self, tmp_path, bus, atten, state):
        svc = _svc(_write_fe(tmp_path, atten=atten))

        async def go():
            svc.loop = asyncio.get_running_loop()
            await svc._run(svc._frontend_start)
            try:
                assert svc._atten_watch is None              # no watcher for these
                st = (await svc._cmd_payload_status(1, {})).result
                assert st["devices"]["atten"]["state"] == state
            finally:
                await svc._run(svc._frontend_stop)
        run(go)

    def test_payload_status_size(self, tmp_path, bus):
        """Everything filled in, the longest values: the whole ack under 1400 B."""
        from dpmg.radio import sdr_core as sdr
        bus.plug("/dev/hidraw12", att=105.55, serial="12345678901234")
        svc = _svc(_write_fe(tmp_path))

        async def go():
            svc.loop = asyncio.get_running_loop()
            await svc._run(svc._frontend_start)
            try:
                svc.backend = sdr.MockBackend(sdr.SDRConfig(sample_rate=1e6, center_freq=2.2e9))
                svc.backend.open()
                await svc._cmd_set_tx_atten(1, {"qdb": 167})
                g = svc.guard
                g.saved_db, g.last_refusal = 12.35, "x" * 200
                g.config_error = "y" * 200
                svc.frontend.last_error = "z" * 200
                svc._adc_probe.state = "error: " + "w" * 40
                st = (await svc._cmd_payload_status(2, {})).result
                n = len(CmdAck(4294967295, CmdStatus.OK, st).pack())
                print(f"payload_status ack {n} B, devices block "
                      f"{len(CmdAck(0, 0, st['devices']).pack()) - 5} B")
                assert n < MAX_DATAGRAM - 64, n
                fs = (await svc._cmd_frontend_status(3, {})).result
                print(f"frontend_status ack {len(CmdAck(0, 0, fs).pack())} B")
            finally:
                await svc._run(svc._frontend_stop)
        run(go)


if __name__ == "__main__":  # OFF-01 loop runs `python3 tests/<file>.py`; delegate to pytest
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
