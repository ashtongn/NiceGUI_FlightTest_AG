"""mc_atten: SCPI framing/parsing, bounds and 0.05 dB snapping, mock device
behaviour, hidraw transport over a pipe-backed fake device, HTTP transport with a
fake opener, make_atten spec parsing."""
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dpmg.payload import mc_atten as mc  # noqa: E402


class TestFraming:
    def test_frame_layout(self):
        b = mc.frame_scpi(":SETATT=30.00")
        assert len(b) == 65 and b[0] == 0 and b[1] == mc.CMD_SCPI
        assert b[2:15] == b":SETATT=30.00" and b[15:] == b"\0" * 50
        raw = mc.frame_scpi(":MN?", with_report_id=False)
        assert len(raw) == 64 and raw[0] == 1 and raw[1:5] == b":MN?"

    def test_frame_too_long(self):
        with pytest.raises(mc.AttenError):
            mc.frame_scpi("x" * 70)

    def test_parse_reply(self):
        buf = bytes([1]) + b"MN=RCDAT-6G-120H\0" + b"\xff" * 40
        assert mc.parse_reply(buf) == "MN=RCDAT-6G-120H"
        assert mc.parse_reply(bytes([1]) + b"1" + b"\0" * 62) == "1"
        with pytest.raises(mc.AttenError):
            mc.parse_reply(bytes([40]) + b"x\0")
        with pytest.raises(mc.AttenError):
            mc.parse_reply(b"")


class TestDevice:
    def mk(self, **kw):
        t = mc.MockTransport(**kw)
        return mc.MiniCircuitsAtten(t), t

    def test_set_snaps_and_bounds(self):
        a, t = self.mk()
        assert a.set_db(30.049) == 30.05
        assert t.commands[-1] == ":SETATT=30.05"
        assert a.set_db(0) == 0.0 and a.set_db(120) == 120.0
        assert a.set_db(12.724) == 12.70 and a.set_db(12.726) == 12.75
        for bad in (-0.01, 120.01, float("nan")):
            with pytest.raises(mc.AttenError):
                a.set_db(bad)
        assert a.last_set == 12.75                          # bad requests never reached set

    def test_readback_and_ident(self):
        a, t = self.mk()
        a.set_db(45.5)
        assert a.get_db() == 45.5
        assert a.model() == "RCDAT-6G-120H" and a.serial() == "11901234567"
        assert a.firmware() == "C9"
        st = a.status()
        assert st["att_db"] == 45.5 and st["transport"] == "mock" and st["last_set"] == 45.5

    def test_device_reject_raises(self):
        a, t = self.mk(max_db=60)                           # device smaller than driver thinks
        with pytest.raises(mc.AttenError) as ei:
            a.set_db(90)
        assert "rejected" in str(ei.value)

    def test_startup_att(self):
        a, t = self.mk()
        assert a.startup_att(120) == 120.0 and t.startup == 120.0
        assert t.commands[-1] == ":STARTUPATT:VALUE:120.00"

    def test_transport_error_propagates(self):
        a, t = self.mk()
        t.fail_next = OSError("usb gone")
        with pytest.raises(OSError):
            a.get_db()
        t.fail_next = OSError("usb gone")
        st = a.status()
        assert st["att_db"] is None and "usb gone" in st["err"]

    def test_custom_step(self):
        a = mc.MiniCircuitsAtten(mc.MockTransport(step=0.25), max_db=90, step_db=0.25)
        assert a.set_db(43.8) == 43.75
        with pytest.raises(mc.AttenError):
            a.set_db(95)


class TestHidrawTransport:
    """A pty pair stands in for /dev/hidrawN: the 'device' thread answers each
    65-byte write with a 64-byte SCPI reply."""

    @pytest.fixture
    def fake_dev(self):
        import pty
        master, slave = pty.openpty()
        import tty
        tty.setraw(master); tty.setraw(slave)
        path = os.ttyname(slave)
        emu = mc.MockTransport()
        stop = threading.Event()
        seen = []

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
                    seen.append(rep)
                    cmd = rep[2:].split(b"\0", 1)[0].decode()
                    out = emu.scpi(cmd).encode()
                    try:
                        os.write(master, bytes([rep[1]]) + out + b"\0" * (63 - len(out)))
                    except OSError:              # the test closed the pty mid-reply
                        return
        th = threading.Thread(target=serve, daemon=True); th.start()
        yield path, emu, seen
        stop.set(); os.close(slave); os.close(master)
        th.join(timeout=1.0)

    def test_roundtrip(self, fake_dev):
        path, emu, seen = fake_dev
        t = mc.HidrawTransport(path)
        a = mc.MiniCircuitsAtten(t)
        assert a.model() == "RCDAT-6G-120H"
        assert a.set_db(17.35) == 17.35 and emu.att == 17.35
        assert a.get_db() == 17.35
        assert seen[0][0] == 0 and seen[0][1] == 1           # report id + SCPI code on the wire
        assert t.describe() == f"hidraw:{path}"
        t.close()

    def test_timeout(self, fake_dev, monkeypatch):
        path, emu, seen = fake_dev
        t = mc.HidrawTransport(path)
        # device thread still answers, so force a timeout by not giving it time
        monkeypatch.setattr(mc.select, "select", lambda *a, **k: ([], [], []))
        with pytest.raises(mc.AttenError) as ei:
            t.scpi(":ATT?")
        assert "timeout" in str(ei.value)
        t.close()

    def test_missing_node(self):
        with pytest.raises(mc.AttenError):
            mc.HidrawTransport("/dev/hidraw-does-not-exist")

    def test_find_hidraw_parses_uevent(self, tmp_path, monkeypatch):
        d = tmp_path / "hidraw3" / "device"; d.mkdir(parents=True)
        (d / "uevent").write_text("DRIVER=hid-generic\nHID_ID=0003:000020CE:00000023\nHID_NAME=Mini-Circuits\n")
        e = tmp_path / "hidraw4" / "device"; e.mkdir(parents=True)
        (e / "uevent").write_text("HID_ID=0003:0000046D:0000C52B\n")
        monkeypatch.setattr(mc.glob, "glob", lambda pat: [str(tmp_path / "hidraw3"), str(tmp_path / "hidraw4")])
        assert mc.find_hidraw() == ["/dev/hidraw3"]


class TestHttpTransport:
    def test_get_url_and_reply(self):
        calls = []

        class R:
            def __init__(self, body): self.b = body
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self): return self.b

        def opener(url, timeout):
            calls.append(url)
            return R(b"1\r\n" if "SETATT" in url else b"12.75")
        t = mc.HttpTransport("192.168.0.40", opener=opener)
        a = mc.MiniCircuitsAtten(t)
        assert a.set_db(12.75) == 12.75
        assert calls[-1] == "http://192.168.0.40/:SETATT=12.75"
        assert a.get_db() == 12.75 and calls[-1].endswith("/:ATT?")
        assert t.describe() == "http:http://192.168.0.40"


class TestMakeAtten:
    def test_specs(self):
        assert mc.make_atten("none") is None and mc.make_atten("") is None and mc.make_atten(None) is None
        assert isinstance(mc.make_atten("mock").t, mc.MockTransport)
        assert isinstance(mc.make_atten("http://1.2.3.4").t, mc.HttpTransport)
        with pytest.raises(mc.AttenError):
            mc.make_atten("serial:/dev/ttyUSB0")
        with pytest.raises(mc.AttenError):
            mc.make_atten("hidraw:/dev/hidraw-nope")

    def test_from_cfg_object(self):
        from dpmg.payload import rf_frontend as rf
        cfg = rf.config_from_dict({"atten": {"transport": "mock", "max_db": 90, "step_db": 0.25}})
        a = mc.make_atten(cfg.atten)
        assert a.max_db == 90 and a.step == 0.25
        assert mc.make_atten(rf.AttenCfg()) is None


class TestDevNode:
    """A unit plugged into a running container: /sys has it, /dev does not."""

    def test_existing_node_untouched(self, tmp_path):
        node = tmp_path / "hidraw7"; node.write_text("")
        assert mc.ensure_dev_node(str(node), sysfs=str(tmp_path / "sys")) is True

    def test_missing_node_made_from_sysfs(self, tmp_path, monkeypatch):
        sysd = tmp_path / "sys" / "hidraw7"; sysd.mkdir(parents=True)
        (sysd / "dev").write_text("241:7\n")
        made = {}
        monkeypatch.setattr(mc.os, "mknod", lambda path, mode, dev: made.update(
            path=path, mode=mode, dev=dev))
        target = str(tmp_path / "dev" / "hidraw7")
        assert mc.ensure_dev_node(target, sysfs=str(tmp_path / "sys")) is True
        assert made["path"] == target and made["dev"] == mc.os.makedev(241, 7)
        assert made["mode"] & mc.stat.S_IFCHR

    def test_no_sysfs_entry(self, tmp_path):
        assert mc.ensure_dev_node(str(tmp_path / "hidraw9"), sysfs=str(tmp_path / "sys")) is False


if __name__ == "__main__":  # OFF-01 loop runs `python3 tests/<file>.py`; delegate to pytest
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
