"""
test_secure_integration.py: secure messages wired into the stack
with the keys on the ground station only: the radio parses sealed bodies and
forwards them (SEALED_FRAME), never opens; the ground opens (secure_ground).

Evidence names:
  test_flag_off_no_behaviour_change        legacy paths untouched, secure requests refused
  test_gen_scenario_needs_ground_sealed_bits  the radio builds only from ground-sealed bits
  test_decode_pool_returns_sealed_only     children never release plaintext
  test_worker_forwards_never_opens         the worker hands bodies to the callback, no plaintext on the radio
  test_service_forwards_sealed_no_keys     SEALED_FRAME datagrams; no key file on the radio; enable refused
  test_service_has_no_key_loading_path     source scan: the service never loads keys
  test_ground_opener                       the x86 opens, replay/verdicts/counters on the ground
  test_gui_modules_import                  Encoder / engineering panels import; ground-side strings present
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile

import numpy as np
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "bench"))
import secure_testkit as tk  # noqa: E402
from dpmg.secure import api as sf  # noqa: E402
from dpmg.framing import frame_codec as fc  # noqa: E402
from dpmg.scenario import gen_scenario_json as g  # noqa: E402
from dpmg.link import proto  # noqa: E402
from dpmg.scenario.plan_adapter import load_plan, plan_to_watchlist  # noqa: E402
from dpmg.link.proto import Cmd, CmdStatus, HostType, Status  # noqa: E402

FS = 1_920_000.0
OFF = 300_000.0
CENTER = 425_025_000.0


def _ground():
    """The ground-station context (seals and opens its own net's frames)."""
    tx, _rx = tk.pair()
    sf.set_context(tx)
    return tx


def _secure_sig(ctx, msg, fec=None, mod="FSK"):
    sig = {"modulation": mod, "symbol_rate": 4800, "tone_spacing_hz": 5000, "offset_hz": OFF,
           "amplitude": 0.9, "secure": True,
           "bits": fc.encode_secure(msg.encode(), fec, ctx=ctx)}
    if fec:
        sig["fec"] = fec
    return sig


def _build(sig, out_dir, name):
    spec = {"name": name, "sample_rate_hz": FS, "center_freq_hz": CENTER, "loop_s": 0.3, "signals": [sig]}
    g.build_scenario(spec, out_dir)
    plan = load_plan(os.path.join(out_dir, f"{name}.plan.json"))
    iq = np.fromfile(os.path.join(out_dir, f"{name}.iq.bin"), dtype=np.complex64)
    return iq, plan


def _decode(iq, plan, rng, snr_db=30.0):
    import replay_chain as rc
    wl = plan_to_watchlist(plan)
    cc, _e = wl[0]
    fs = float(plan.sample_rate_hz)
    _fo, decim = rc.worker_fs_out(wl, fs)
    ps = rc.burst_power(iq)
    n0 = ps / (float(cc.symbol_rate) * 10 ** (snr_db / 10.0))
    x = np.roll(np.tile(iq, 2), int(rng.integers(0, len(iq)))) * np.exp(1j * rng.uniform(0, 2 * np.pi))
    x = rc.awgn(x.astype(np.complex64), n0, fs, rng)
    bb, fse = rc.channelize_loop(rc.spectrum(x), fs, cc.f_center_hz - plan.center_freq_hz, decim)
    return rc.decode(bb, cc, fse)


def _apply(svc, cmd):
    async def main():
        svc.loop = asyncio.get_running_loop()
        return await svc._apply(cmd)
    return asyncio.run(main())


def _svc(name="SEC-SVC"):
    from dpmg.radio.dpmg_service import RadioService
    return RadioService(serial=name, host_type=HostType.G40_RX, kind="mock", simulate=True,
                        cmd_port=0, discovery_port=0, broadcast="127.0.0.1")


# flag off
def test_flag_off_no_behaviour_change(monkeypatch):
    ctx = _ground()
    sig = _secure_sig(ctx, "X")
    monkeypatch.setenv(sf.FLAG_SECURE_MSG, "0")
    bits = fc.encode_fec(b"LEGACY", {"type": "conv", "rate": "1/2"})
    assert fc.fec_find_and_decode(bits, {"type": "conv", "rate": "1/2"}).payload_bytes == b"LEGACY"
    with tempfile.TemporaryDirectory() as d:
        with pytest.raises(ValueError, match="DPMG_SECURE_MSG is off"):
            _build(sig, d, "off")
    from dpmg.dsp.decode_pool import DecodeSpec, _default_decode
    spec = DecodeSpec(modulation="FSK", fs_out=48000.0, sps=10.0,
                      demod_params={"symbol_rate": 4800, "tone_spacing_hz": 5000,
                                    "frame": {"sync_bits": fc.SYNC_BITS, "secure": {"on": True}}})
    r = _default_decode(0, np.zeros(4800, np.complex64), spec)
    assert not r.valid and not r.sealed
    svc = _svc("SEC-OFF")
    svc._secure_start()
    ack = _apply(svc, Cmd(1, "secure_enable", {"enable": True}))
    assert ack.status == CmdStatus.REJECTED and "ground" in ack.result["err"]
    ack = _apply(svc, Cmd(2, "secure_status", {}))
    assert ack.status == CmdStatus.OK and ack.result["flag"] is False and ack.result["keys_on_radio"] is False
    assert svc._config_snapshot()["secure"]["flag"] is False
    assert not (svc._status().flags & (Status.FLAG_SECURE_ON | Status.FLAG_SECURE_FAULT))
    monkeypatch.setenv(sf.FLAG_SECURE_MSG, "1")
    sf.set_context(None)


# scenario / plan
def test_gen_scenario_needs_ground_sealed_bits():
    ctx = _ground()
    with tempfile.TemporaryDirectory() as d:
        # the radio never seals: a secure signal without bits is refused
        with pytest.raises(ValueError, match="ground station"):
            _build({"modulation": "FSK", "symbol_rate": 4800, "tone_spacing_hz": 5000, "offset_hz": OFF,
                    "message": "SECURE", "secure": True}, d, "nobits")
        sig = _secure_sig(ctx, "SECURE PING", fec={"type": "conv", "rate": "1/2"})
        iq, plan = _build(sig, d, "sec")
        e = plan.entries[0]
        assert e.frame.get("secure") == {"on": True} and e.params.get("secure") == {"on": True}
        assert e.params.get("fec") == {"type": "conv", "rate": "1/2"}
        cc, _ = plan_to_watchlist(plan)[0]
        assert cc.demod_params["secure"] == {"on": True} and cc.demod_params["frame"]["secure"]
        assert len(iq) > 0 and ctx.counters["sealed"] >= 1
        with pytest.raises(ValueError, match="cannot carry"):
            _build(dict(sig, fec={"type": "tpc", "code": "eH(16,11)^2"}), d, "sec16")
        # legacy unchanged
        _iq, plan = _build({"modulation": "FSK", "symbol_rate": 4800, "tone_spacing_hz": 5000,
                            "offset_hz": OFF, "message": "CLEAR"}, d, "clear")
        assert "secure" not in plan.entries[0].frame and "secure" not in plan.entries[0].params
    sf.set_context(None)


# decode pool / worker
def test_decode_pool_returns_sealed_only():
    ctx = _ground()
    rng = np.random.default_rng(3)
    with tempfile.TemporaryDirectory() as d:
        for fec in (None, {"type": "conv", "rate": "1/2"}, {"type": "rs"}):
            msg = "POOL " + ("none" if fec is None else fec["type"])
            iq, plan = _build(_secure_sig(ctx, msg, fec), d, "pool_" + ("none" if fec is None else fec["type"]))
            r = _decode(iq, plan, rng)
            assert r.valid and r.sealed, (fec, r.error, r.message)
            assert all(m.startswith("[sealed") for m in r.messages) and msg not in r.messages
            assert msg.encode() not in b"".join(r.sealed)                 # ciphertext only
            opened = sf.open_result(r, ctx)                                # ground side
            assert opened.valid and opened.messages == [msg] * len(opened.messages), (fec, opened.secure)
            opened2 = sf.open_result(_decode(iq, plan, rng), ctx)          # same frame again
            assert not opened2.valid and {v["verdict"] for v in opened2.secure["verdicts"]} <= {sf.REPLAY, sf.AUTH_FAIL}
    sf.set_context(None)


def test_worker_forwards_never_opens():
    from dpmg.dsp.channelizer_worker import ChannelRuntime, ChannelizerWorker
    from dpmg.dsp.decode_pool import DecodeResult
    ctx = _ground()
    w = ChannelizerWorker()
    got = []
    w.sealed_callback = lambda ch, bodies, f: got.append((getattr(ch, "ch_name", ""), bodies, f))
    ch = ChannelRuntime(idx=0, f_center_hz=425.3e6, bw_hz=25e3, demod_type="FSK")
    body = ctx.seal(b"WORKER OK")
    r = DecodeResult(key=0, ok=True, valid=True, message="[sealed 9 B]", messages=["[sealed 9 B]"],
                     bit_string="0" * 100, sealed=[body])
    w._apply_decode_result(ch, r)
    assert ch.valid_decode and ch.last_message.startswith("[sealed 9 B") and ch.sealed_count == 1
    assert got and got[0][1] == [body] and got[0][2] == 425.3e6
    assert "WORKER OK" not in ch.last_message and ctx.counters[sf.AUTH_OK] == 0   # nothing opened on the radio
    sf.set_context(None)


# service
def test_service_forwards_sealed_no_keys(monkeypatch):
    """No key file on the radio; the flag only makes it parse + forward."""
    monkeypatch.setenv(sf.ENV_KEY_FILE, "/nonexistent/radio-has-no-keys.json")
    sf.set_context(None)
    svc = _svc()
    svc._secure_start()
    sent = []

    class _Ep:
        def send(self, msg, addr):
            sent.append((msg, addr))
    svc.ep = _Ep()
    svc.gui_addrs = {("127.0.0.1", 50000)}

    class _Loop:
        def call_soon_threadsafe(self, fn, *a):
            fn(*a)
    svc.loop = _Loop()
    ctx_ground = tk.make_ctx()                      # the ground's keys, not the radio's
    b1, b2 = ctx_ground.seal(b"one"), ctx_ground.seal(b"two")

    class _Ch:
        ch_name, idx, f_center_hz = "FSK-00", 0, 425.3e6
    svc._on_sealed(_Ch(), [b1, b2])
    assert len(sent) == 2 and all(isinstance(m, proto.SealedFrame) for m, _a in sent)
    assert [m.body for m, _a in sent] == [b1, b2] and [m.seq for m, _a in sent] == [1, 2]
    wire = proto.encode(sent[0][0], 5)
    mt, _seq, back = proto.decode(wire)
    assert mt == proto.MsgType.SEALED_FRAME and back.band == "FSK-00" and back.body == b1
    # the ground opens what the wire carried; the radio never could
    assert ctx_ground.open(back.body).ok
    assert sf.get_context(create=False) is None
    brief = svc._secure_brief()
    assert brief["keys_on_radio"] is False and brief["sealed_seen"] == 2 and brief["sealed_forwarded"] == 2
    assert "master" not in json.dumps(svc._config_snapshot()) and "key_id" not in json.dumps(brief)
    ack = _apply(svc, Cmd(1, "secure_status", {}))
    assert ack.status == CmdStatus.OK and ack.result["keys_on_radio"] is False
    assert ack.result["brief"]["sealed_forwarded"] == 2 and len(json.dumps(ack.result)) < 2500
    for name in ("secure_enable", "secure_keys_reload"):
        ack = _apply(svc, Cmd(2, name, {"enable": True}))
        assert ack.status == CmdStatus.REJECTED and "ground" in ack.result["err"]
    # STATUS flags: ON once a secure plan entry is loaded; FAULT when the flag is off with one
    assert not (svc._status().flags & Status.FLAG_SECURE_ON)
    svc.plan = {"entries": [{"name": "FSK-00", "frame": {"secure": {"on": True}}, "params": {}}]}
    assert svc._status().flags & Status.FLAG_SECURE_ON
    monkeypatch.setenv(sf.FLAG_SECURE_MSG, "0")
    assert svc._status().flags & Status.FLAG_SECURE_FAULT and "off" in svc._secure_brief()["err"]
    monkeypatch.setenv(sf.FLAG_SECURE_MSG, "1")
    sf.set_context(None)


def test_service_sim_inject_is_simulator_only(monkeypatch):
    """secure_sim_inject forwards given bodies on a --simulate service (the GUI
    checkout aid) and is refused on a real radio."""
    svc = _svc("SEC-INJ")
    svc._secure_start()
    sent = []

    class _Ep:
        def send(self, msg, addr):
            sent.append(msg)
    svc.ep = _Ep()
    svc.gui_addrs = {("127.0.0.1", 50001)}

    class _Loop:
        def call_soon_threadsafe(self, fn, *a):
            fn(*a)
    svc.loop = _Loop()
    body = tk.make_ctx().seal(b"inject")
    ack = _apply(svc, Cmd(1, "secure_sim_inject", {"bodies": [body.hex()], "band": "SIM-00"}))
    assert ack.status == CmdStatus.OK and ack.result["forwarded"] == 1 and sent[0].body == body
    ack = _apply(svc, Cmd(2, "secure_sim_inject", {"bodies": ["zz"]}))
    assert ack.status == CmdStatus.REJECTED
    svc.simulate = False
    ack = _apply(svc, Cmd(3, "secure_sim_inject", {"bodies": [body.hex()]}))
    assert ack.status == CmdStatus.REJECTED and "simulator-only" in ack.result["err"]


def test_service_has_no_key_loading_path():
    from dpmg.radio import dpmg_service
    src = open(dpmg_service.__file__).read()
    assert "load_key_file" not in src and "_sf.get_context(" not in src and "GpsdTime" not in src
    assert "keys_on_radio" in src and "SealedFrame" in src


# ground
def test_ground_opener():
    from dpmg.secure import secure_ground as sg
    ctx = _ground()
    g = sg.GroundOpener(ctx)

    class _Host:
        serial = "G40-X"
        sealed = []
    h = _Host()
    bodies = [ctx.seal(b"g%d" % i) for i in range(5)]
    h.sealed = [proto.SealedFrame("G40-X", "FSK-00", 425.3e6, i + 1, 0.0, b) for i, b in enumerate(bodies)]
    out = g.poll(h)
    assert [o["msg"] for o in out] == ["g0", "g1", "g2", "g3", "g4"] and all(o["verdict"] == sf.AUTH_OK for o in out)
    assert g.poll(h) == [] and g.opened == 5                      # nothing new
    bad = bytearray(bodies[0]); bad[-2] ^= 1
    h.sealed.append(proto.SealedFrame("G40-X", "FSK-00", 425.3e6, 8, 0.0, bytes(bad)))   # seq gap 6,7
    out = g.poll(h)
    assert len(out) == 1 and out[0]["verdict"] in (sf.REPLAY, sf.AUTH_FAIL) and out[0]["msg"] == "" and g.dropped == 2
    assert g.last("G40-X", "FSK-00")["verdict"] != sf.AUTH_OK
    st = g.status()
    assert st["active"] and st["profile"] == "slot" and st["counters"]["AUTH_OK"] == 5
    assert st["aead"] == "AES-256-GCM-SIV" and st["overhead_bits"] == 160 and st["overhead_bits_long"] == 176
    assert "module" not in st and "invocations" not in st
    assert "master" not in json.dumps(st)
    # no keys on this host: NO_KEY, nothing released
    g2 = sg.GroundOpener(None)
    sf.set_context(None)
    os.environ[sf.ENV_KEY_FILE] = "/nonexistent/keys.json"
    try:
        r = g2.open_one("G40-X", h.sealed[0])
        assert r["verdict"] == sf.NO_KEY and r["msg"] == ""
    finally:
        os.environ.pop(sf.ENV_KEY_FILE, None)
        sf.set_context(None)


def test_gui_modules_import():
    from dpmg.web import link_tab
    from dpmg.web import scenario_tab
    assert callable(link_tab._build_secure_panel)
    lt = open(link_tab.__file__).read()
    assert "keys on this ground station" in lt and "secure_enable" not in lt and "secure_ground" in lt
    src = open(os.path.join(ROOT, "dpmg", "web", "nicegui_app.py")).read()
    assert "Secure messaging (AES-256-GCM-SIV" in src and "inspect_bits" in src and "NONCE_NAME" in src
    for gui in (lt, src):                     # operator-visible text: no FIPS, no "Phase 1"
        shown = [ln for ln in gui.splitlines()
                 if any(k in ln for k in ("ui.", "section(", "set_text(", "notify("))]
        assert not [ln for ln in shown if "FIPS" in ln or "Phase 1" in ln]
    assert "encode_secure" in open(scenario_tab.__file__).read()
    ctx = _ground()
    bits = fc.encode_secure(b"inspect me", None, ctx=ctx)
    found = sf.inspect_bits("0101" + bits + "1010")
    assert len(found) == 1 and found[0]["verdict"] == sf.AUTH_OK and found[0]["message"] == "inspect me"
    assert found[0]["header"]["sender"] == 7 and found[0]["ciphertext_bytes"] == 10
    assert sf.inspect_bits("") == [] and sf.inspect_bits("1" * 50) == []
    sf.set_context(None)


if __name__ == "__main__":  # OFF-01 loop runs `python3 tests/<file>.py`; delegate to pytest
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
