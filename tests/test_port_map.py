#!/usr/bin/env python3
"""
test_port_map.py: the per-radio port map (rf_frontend.yaml ports:, port_map.py).

  1. the ports section loads, validates and round-trips; bad sections are refused
     with an error naming the key; no section means no restriction
  2. the handle to jack table agrees with sdr_core.g40_trx_port
  3. a real RadioService (g40, simulate) refuses wrong-role TX and RX on every
     entry point, names the jack and the file, and leaves the running radio alone
  4. get_config reports the map; the snapshot and the full report fit one CMD_ACK
  5. frontend_reload changes the map under an open radio; a broken file refuses
  6. TX TRx2B with RX TRx1B opens with the same-card warning, cross-card does not
  7. the mission planner flags locked jacks and RX none stays off them
  8. the GUI dropdown helper greys out, relabels and moves off locked jacks
  9. writing the ports: section leaves every other line of the file as it was
 10. the set_ports command validates, refuses during TX, writes the file and applies
     at once with the PA and switch lines left alone; who changed it is reported
 11. the lockout panel follows any radio: jack lists per kind (G40, X40, from the
     capability list), the roles an X40 jack can take, unknown jacks refused by
     the service for that kind, caps carrying the jack list, the panel title and
     the edits kept per radio

Stdlib + numpy. Exit 0 = all passed.    python3 tests/test_port_map.py
"""
import asyncio
import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dpmg.link import proto                                          # noqa: E402
from dpmg.link.proto import CmdAck, CmdStatus, HostType              # noqa: E402
from dpmg.mission import mission_file as mf, ports                   # noqa: E402
from dpmg.payload import rf_frontend as rf                           # noqa: E402
from dpmg.radio import port_map as pm, sdr_core as sdr               # noqa: E402
from dpmg.radio.dpmg_service import RadioService                     # noqa: E402
from dpmg.web import port_pick_ui as ppu                             # noqa: E402

FAILS = []
RX_CHAIN = "ZB3PD-63-S+ splitter, dBWave PALNA200101000A LNA, RCDAT-6G-120H RX attenuator, T/R switch J1 and J0, antenna"
TX_CHAIN = "20 dB pad, Empower 1189 HPA, T/R switch J2 and J0, antenna"
PAYLOAD2 = {"TRx2B": {"role": "tx", "chain": TX_CHAIN},
            "TRx1A": {"role": "rx", "chain": RX_CHAIN},
            "TRx1B": {"role": "rx", "chain": RX_CHAIN},
            "TRx2A": {"role": "rx", "chain": RX_CHAIN}}
ROLES2 = {j: e["role"] for j, e in PAYLOAD2.items()}


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def _yaml_file(tmp, text, name="rf_frontend.yaml"):
    path = os.path.join(tmp, name)
    with open(path, "w") as fh:
        fh.write(text)
    return path


def _ports_yaml(ports_map=None):
    import yaml
    return yaml.safe_dump({"frontend": "none", **({"ports": ports_map} if ports_map else {})})


def test_loader():
    print("[1] ports section in rf_frontend.yaml")
    cfg = rf.config_from_dict({"ports": PAYLOAD2})
    check(cfg.ports == PAYLOAD2, "payload 2 section loads as written")
    check(rf.config_from_dict({}).ports == {}, "no section: empty map")
    check(rf.config_from_dict({"ports": None}).ports == {}, "empty section: same as none")
    check(rf.config_from_dict({"ports": {"TRx1A": "rx"}}).ports["TRx1A"]["role"] == "rx",
          "a bare role is accepted")
    again = rf.apply_overrides(cfg, {"watchdog_ms": 100})
    check(again.ports == PAYLOAD2, "the map survives CLI overrides (to_dict round trip)")
    for bad, word in (({"TRx3A": "rx"}, "TRx3A"), ({"TRX1A": "rx"}, "TRX1A"),
                      ({"TRx1A": {"role": "both"}}, "both"),
                      ({"TRx1A": {"role": "rx", "wire": "x"}}, "wire"),
                      ({"TRx1A": {"role": "rx", "chain": "x" * 200}}, "chain"),
                      ({"TRx1A": "tx"}, "RX"), ("rx", "mapping"), ({"TRx1A": 5}, "TRx1A")):
        try:
            rf.config_from_dict({"ports": bad})
            check(False, f"{bad!r} should be refused")
        except rf.ConfigError as e:
            check("ports" in str(e) and word in str(e), f"{str(bad)[:40]} refused: {str(e)[:70]}")
    check(pm.role_of(PAYLOAD2, "TRx9Z") == "none", "a jack the map does not list counts as none")
    check(pm.allows({}, "TRx1A", "tx") and pm.allows(None, "TRx1A", "rx"), "no map: everything allowed")
    check(pm.allows({"TRx1A": "txrx"}, "TRx1A", "tx") and pm.allows({"TRx1A": "txrx"}, "TRx1A", "rx"),
          "txrx allows both")
    check(not pm.allows({"TRx1A": "none", "TRx1B": "rx"}, "TRx1A", "rx"), "none allows neither")
    check(pm.jacks_for(PAYLOAD2, "tx") == ["TRx2B"]
          and pm.jacks_for(PAYLOAD2, "rx") == ["TRx1A", "TRx2A", "TRx1B"], "jacks_for in panel order")
    check(pm.compact(PAYLOAD2) == ROLES2 and pm.role_of(ROLES2, "TRx2B") == "tx",
          "compact form reads the same")


def test_jack_table():
    print("[2] handle to jack")
    for h in ("RXA1", "RXA2"):
        for c in (0, 1):
            check(pm.rx_jack("g40", h, c) == sdr.g40_trx_port(h, c), f"{h}@{c} = {pm.rx_jack('g40', h, c)}")
    for h in ("TXA1", "TXA2", "TXB1"):
        for c in (0, 1):
            check(pm.tx_jack("g40", h, c) == sdr.g40_trx_port(h, c), f"{h}@{c} = {pm.tx_jack('g40', h, c)}")
    check(pm.rx_jack("g40", "RXB1", 0) is None and pm.tx_jack("g40", "TXB2", 0) is None,
          "a G40 handle no GUI uses is not guessed at")
    check(pm.rx_jack("x40", "RXB1") == "RXB1" and pm.tx_jack("b200", "TX/RX") == "TX/RX",
          "other radios use the handle name")
    for side, cmds in (("tx", ("set_tx_waveform", "start_tx", "replay_recording")),
                       ("rx", ("start_rx", "record_iq", "capture_iq"))):
        for c in cmds:
            check(pm.sides_used(c) == {side}, f"{c} uses {side}")
    check(pm.sides_used("stop_tx") == set() and pm.sides_used("close") == set()
          and pm.sides_used("get_config") == set(), "stops and status use nothing")
    check(pm.sides_used("transceive", {"enabled": False}) == set(), "transceive off uses nothing")
    check(pm.sides_used("transponder", {"armed": True}) == {"rx"}, "emulated transponder only listens")


def _svc(cfg_path, kind="g40", host_type=HostType.G40_RX):
    return RadioService(serial=f"{kind.upper()}-SIM", host_type=host_type, kind=kind, simulate=True,
                        cmd_port=47896, discovery_port=47897, broadcast="127.0.0.1",
                        frontend_config=cfg_path)


def _init(rx, tx, card=0, tx_card=None):
    ex = {"card": card, "tx_center_freq": 425_025_000, "rf_bandwidth_hz": 1_536_000}
    if tx_card is not None:
        ex["tx_card"] = tx_card
    ex["chan_mode"] = "single" if tx_card is not None and tx_card != card else "dual"
    p = {"kind": "g40", "sample_rate": 1_920_000, "center_freq": 425_025_000, "rx_gain": 245,
         "tx_gain": 20.0, "clock_source": "internal", "rx_antenna": rx, "extras": ex}
    if tx:
        p["tx_antenna"] = tx
    return p


class _Run:
    def __init__(self, svc):
        self.svc, self.cid = svc, 100

    async def __call__(self, name, params=None):
        self.cid += 1
        return await self.svc._apply(SimpleNamespace(name=name, params=params or {}, cmd_id=self.cid))


def _refused(ack, jack=None, word=None):
    err = str((ack.result or {}).get("err"))
    return (ack.status == CmdStatus.REJECTED and (ack.result or {}).get("port_role") is True
            and (jack is None or jack in err) and (word is None or word in err))


async def _service_cases(tmp):
    print("[3] service refuses wrong-role use")
    path = _yaml_file(tmp, _ports_yaml(PAYLOAD2))
    svc = _svc(path)
    svc.loop = asyncio.get_running_loop()
    svc._frontend_start()
    run = _Run(svc)

    # init: the proven Payload 2 pair first, so a refusal can be shown to leave it up
    a = await run("init", _init("RXA1", "TXA2", 0, 1))
    check(a.status == CmdStatus.OK and not a.result.get("same_card_tx"),
          f"RX TRx1A + TX TRx2B (cross-card) init OK ({a.result.get('err')})")
    live = svc.backend
    for label, p, jack, word in (
            ("TX TRx1B", _init("RXA1", "TXA1", 0, 1), "TRx1B", "TX on TRx1B"),
            ("TX TRx1A", _init("RXA1", "TXA1", 1, 0), "TRx1A", "RX only"),
            ("TX TRx2A", _init("RXA1", "TXA2", 1, 0), "TRx2A", "TX on"),
            ("RX TRx2B", _init("RXA2", "TXA2", 1, None), "TRx2B", "RX on TRx2B"),
            ("TDD on TRx1A", _init("RXA1", "TXA1", 0), "TRx1A", "TX on"),
            ("unknown TX handle", _init("RXA1", "TXB2", 0, 1), "TXB2", "can check"),
            ("unknown RX handle", _init("RXB1", "TXA2", 0, 1), "RXB1", "can check")):
        a = await run("init", p)
        check(_refused(a, jack, word), f"init {label} refused: {str(a.result.get('err'))[:80]}")
    a = await run("init", _init("RXA1", "TXA1", 0, 1))
    err = a.result.get("err", "")
    check(path in err and RX_CHAIN in err and "TRx2B" in err,
          "the refusal names the config file, the chain and the jack that would work")
    check(svc.backend is live, "a refused init leaves the running session alone")
    a = await run("init", _init("RXA1", "TXA2", 0, 1))
    check(a.status == CmdStatus.OK and a.result.get("noop"), "the same good init is still a no-op")

    # a legal session: nothing below is refused by the map (other guards may answer)
    for name, params in (("set_tx_waveform", {}), ("start_rx", {}), ("record_iq", {"seconds": 0.0}),
                         ("transponder", {"seconds": 0.0})):
        a = await run(name, params)
        check(not (a.result or {}).get("port_role"), f"{name} on TRx1A / TRx2B not refused by the map")

    print("    RX only init then key: TX falls on the default jack")
    await run("stop_rx")
    a = await run("init", _init("RXA1", None, 0))
    check(a.status == CmdStatus.OK, "RX only init on TRx1A OK (names no TX)")
    for name, params, what in (
            ("set_tx_waveform", {"path": "x"}, "scenario, jammer and IQ playback load"),
            ("start_tx", {}, "start_tx"),
            ("replay_recording", {"name": "x"}, "record and replay"),
            ("bitpipe_start", {}, "bitpipe"),
            ("transceive", {"armed": True}, "armed transceive"),
            ("transponder", {"armed": True, "emulate": False}, "armed transponder"),
            ("ground_checkout", {"armed": True}, "armed checkout")):
        a = await run(name, params)
        check(_refused(a, "TRx1A", "TX on"), f"{what} refused on the RX only jack")
    a = await run("transceive", {"enabled": False})
    check(not (a.result or {}).get("port_role"), "transceive off is not refused")
    a = await run("stop_tx")
    check(a.status == CmdStatus.OK, "stop_tx always works")

    print("    payload_key is about the HPA, not a jack: not refused by the map")
    a = await run("payload_key", {"on": False})
    check(not (a.result or {}).get("port_role"), "payload_key not refused by the map")

    print("[4] get_config reports the map")
    a = await run("get_config")
    check(a.result.get("ports") == ROLES2 and a.result.get("ports_v") == pm.version(PAYLOAD2),
          "snapshot has roles and version")
    size = len(proto.encode(CmdAck(1, CmdStatus.OK, a.result), 1))
    check(size < proto.MAX_DATAGRAM - 100, f"snapshot is {size} bytes, room left in one CMD_ACK")
    a = await run("get_config", {"ports": True})
    check(a.result.get("ports_full") == PAYLOAD2 and a.result.get("ports_source", "") in path,
          "ports=true gives the chains and the file")
    size = len(proto.encode(CmdAck(1, CmdStatus.OK, a.result), 1))
    check(size < proto.MAX_DATAGRAM, f"full report is {size} bytes, fits one CMD_ACK")
    big = {f"J{i}": {"role": "txrx", "chain": "x" * pm.CHAIN_MAX} for i in range(pm.JACKS_MAX)}
    size = len(proto.encode(CmdAck(1, CmdStatus.OK, {
        "ports_full": big, "ports_v": 4294967295, "ports_source": "x" * 80,
        "ports_err": "x" * 200}), 1))
    check(size < proto.MAX_DATAGRAM, f"the largest map the loader allows is {size} bytes")

    print("[5] frontend_reload")
    with open(path, "w") as fh:
        fh.write(_ports_yaml({"TRx1A": "tx", "TRx2B": "rx"}))
    a = await run("frontend_reload")
    check(a.status == CmdStatus.OK and "ports_conflict" in a.result,
          f"reload onto a map the open RX jack breaks says so: {a.result.get('ports_conflict', '')[:50]}")
    a = await run("start_rx")
    check(_refused(a, "TRx1A", "RX on"), "start_rx then refused on the jack the new map locks")
    with open(path, "w") as fh:
        fh.write("ports: {TRx1A: wrong}\nfrontend: none\n")
    a = await run("frontend_reload")
    check(a.status == CmdStatus.FAILED and "wrong" in str(a.result.get("err")), "a bad map fails the reload")
    a = await run("init", _init("RXA1", "TXA2", 0, 1))
    check(_refused(a, None, "did not load"), "init refused while the map is unknown")
    a = await run("start_tx")
    check(_refused(a, None, "did not load"), "start_tx refused while the map is unknown")
    a = await run("get_config")
    check("wrong" in str(a.result.get("ports_err")), "get_config reports the load error")
    a = await run("stop_tx")
    check(a.status == CmdStatus.OK, "stop_tx still works")
    with open(path, "w") as fh:
        fh.write(_ports_yaml())
    a = await run("frontend_reload")
    check(a.status == CmdStatus.OK and "ports" not in (await run("get_config")).result,
          "section removed: no map reported")
    a = await run("start_rx")
    check(not (a.result or {}).get("port_role"), "and nothing is refused any more")
    await run("close")

    print("    no file at all: a radio with no map works as before")
    plain = _svc(os.path.join(tmp, "absent.yaml"))
    plain.loop = asyncio.get_running_loop()
    plain._frontend_start()
    prun = _Run(plain)
    a = await prun("init", _init("RXA1", "TXA1", 0, 1))
    check(a.status == CmdStatus.OK, "no ports section: TX TRx1B allowed")
    await prun("close")

    print("    other radios: the map uses the handle names")
    x_path = _yaml_file(tmp, _ports_yaml({"RXB1": "rx", "TXA1": "none"}), "x40.yaml")
    x = _svc(x_path, "x40", HostType.X40_TX)
    x.loop = asyncio.get_running_loop()
    x._frontend_start()
    xrun = _Run(x)
    p = _init("RXB1", "TXA1", 0)
    p.update(kind="x40", sample_rate=50_000_000)
    a = await xrun("init", p)
    check(_refused(a, "TXA1"), "X40 with TXA1 locked refuses the init")
    p.pop("tx_antenna")
    a = await xrun("init", p)
    check(a.status == CmdStatus.OK, "X40 RX only init on RXB1 OK")
    a = await xrun("start_tx")
    check(_refused(a, "TXA1"), "X40 key refused on the locked default TX")
    await xrun("close")

    print("[6] the same-card TX pair")
    path2 = _yaml_file(tmp, _ports_yaml(PAYLOAD2), "p2.yaml")
    s2 = _svc(path2)
    s2.loop = asyncio.get_running_loop()
    s2._frontend_start()
    r2 = _Run(s2)
    a = await r2("init", _init("RXA1", "TXA2", 1))
    check(a.status == CmdStatus.OK and a.result.get("same_card_tx") is True
          and "coupling" in a.result.get("warning", ""),
          "RX TRx1B + TX TRx2B is not refused by the map and carries the warning")
    a = await r2("init", _init("RXA2", "TXA2", 0, 1))
    check(a.status == CmdStatus.OK and not a.result.get("same_card_tx"), "RX TRx2A + TX TRx2B: no warning")
    await r2("close")
    check(pm.same_card_tx_note("TRx1A", "TRx2A") is not None, "same note on card A")
    for rx, tx in (("TRx1A", "TRx2B"), ("TRx2A", "TRx2B"), ("TRx1A", "TRx1B"), ("RXB1", "TXA1")):
        check(pm.same_card_tx_note(rx, tx) is None, f"no note for RX {rx} + TX {tx}")


def _mission(rx=None, tx=None, technique="rx_dwell"):
    step = {"id": "s1", "radio": "R1", "technique": technique, "t_start_s": 0, "duration_s": 10}
    ports.set_step_ports(step, rx, tx)
    return {"radios": [{"id": "R1", "type": "g40"}], "steps": [step]}


def test_mission():
    print("[7] mission planner")
    maps = {"R1": ROLES2}
    check(ports.invalid("g40", "TRx2B", "TRx1A", ROLES2) and
          len(ports.invalid("g40", "TRx2B", "TRx1A", ROLES2)) == 2, "RX on the TX jack, TX on an RX jack: both flagged")
    check(ports.invalid("g40", "TRx1A", "TRx2B", ROLES2) == [], "the proven pair is clean")
    check(ports.invalid("g40", "TRx2B", "TRx1A") == [], "no map: no flag")
    txt = ports.invalid("g40", "TRx1A", "TRx1B", PAYLOAD2)[0]
    check("TRx1B" in txt and "RX only" in txt and RX_CHAIN in txt, "the message names the jack and chain")
    for rx, tx, want in (("TRx1A", "TRx2B", False), ("TRx2B", None, True), (None, "TRx1A", True)):
        msgs = [c for c in mf.validate(_mission(rx, tx), port_maps=maps)
                if c.level == "error" and "locked" in c.message]
        check(bool(msgs) == want, f"validate RX {rx} TX {tx}: {'error' if want else 'clean'}")
    check(not [c for c in mf.validate(_mission("TRx2B", "TRx1A")) if "locked" in c.message],
          "validate with no map: no port map errors")
    warn = [c for c in mf.validate(_mission("TRx1B", "TRx2B"), port_maps=maps) if "coupling" in c.message]
    check(warn and warn[0].level == "warning", "RX TRx1B + TX TRx2B gets a warning, not an error")
    check(ports.rx_for_none("g40", None, "TRx2B", ROLES2) == "TRx1A", "RX none with TX TRx2B goes to TRx1A")
    check(ports.rx_for_none("g40", "TRx1B", "TRx2B", ROLES2) == "TRx1A", "and off the same-card jack")
    check(ports.rx_for_none("g40", "TRx2A", "TRx2B", ROLES2) == "TRx2A", "a good RX stays")
    only = {"TRx2B": "tx", "TRx2A": "none", "TRx1A": "none", "TRx1B": "rx"}
    check(ports.rx_for_none("g40", None, "TRx2B", only) == "TRx1B", "nothing on the other card: any RX jack")
    locked = {"TRx1A": "tx", "TRx2A": "rx", "TRx2B": "rx", "TRx1B": "tx"}
    check(ports.rx_for_none("g40", "TRx1B", "TRx1A", locked) == "TRx2B",
          "never picks a jack the map locks for RX")
    snap = {"card": 0, "rx_antenna": "RXA1", "tx_antenna": "TXA2", "tx_card": 1, "ports": ROLES2}
    check(ports.resolve(snap, "g40", ports.RX_NONE, "TRx2B") == ("TRx1A", "TRx2B"), "resolve follows the snapshot's map")
    check(not ports.needs_reinit(snap, "g40", ports.RX_NONE, "TRx2B"), "already there: no re-init")
    check(ports.rx_fitting("g40", "TRx2B", ROLES2) == ["TRx1A", "TRx2A"], "rx_fitting lists the card A jacks")


class _FakeSel:
    """The parts of ui.select the helper touches."""

    def __init__(self, options, value):
        self.options, self.value = options, value
        self.props_on = {}
        self.events, self.hooks = [], []
        self.id = 7
        self.client = SimpleNamespace(sent=[])
        self.client.run_javascript = self.client.sent.append

    def on(self, name, js_handler=None):
        self.hooks.append((name, js_handler))
        return self

    def props(self, add=None, *, remove=None):
        if add:
            k, v = add.split("=", 1)
            self.props_on[k.strip()] = v
        if remove:
            self.props_on.pop(remove, None)
        return self

    def set_options(self, options, *, value=...):
        self.options = options
        if value is not ...:
            self.value = value

    def update(self):
        pass

    def on_value_change(self, fn):
        self.events.append(fn)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeTip:
    def __init__(self, *_a):
        self.text, self.visible = "", True

    def set_text(self, t):
        self.text = t

    def set_visibility(self, v):
        self.visible = v


def test_gui_helper():
    print("[8] GUI dropdown helper")
    ppu.ui = SimpleNamespace(tooltip=_FakeTip)
    view = {"pm": PAYLOAD2, "v": 1, "err": None, "sig": (1, True, None)}
    ports_opts = {p: p for p in pm.G40_JACKS}
    tx = _FakeSel(dict(ports_opts), "TRx1B")
    ppu.apply(tx, view, "tx")
    check(tx.value == "TRx2B", "TX steps off the locked default TRx1B onto TRx2B")
    check(tx.options["TRx1B"] == "TRx1B (RX only)" and tx.options["TRx2B"] == "TRx2B",
          "locked rows are relabelled, allowed ones are not")
    js = tx.props_on.get(":option-disable", "")
    check("TRx1B (RX only)" in js and "'TRx2B'" not in js, "locked rows are disabled by label")
    check(TX_CHAIN in tx.client.sent[-1] and len(tx.hooks) == 1, "the row tooltips have the chain")
    ppu.apply(tx, view, "tx")
    check(len(tx.hooks) == 1, "the menu handler goes on once, however often the list is drawn")
    check(tx._port_tip.text == TX_CHAIN, "the closed dropdown's tooltip is the picked jack's chain")
    rx = _FakeSel(dict(ports_opts), "TRx1A")
    ppu.apply(rx, view, "rx")
    check(rx.value == "TRx1A" and rx.options["TRx2B"] == "TRx2B (TX only)", "RX keeps TRx1A, TRx2B greyed")
    ppu.apply(rx, view, "rx")
    check(rx.options["TRx2B"] == "TRx2B (TX only)", "drawing twice does not stack the label")
    ppu.apply(rx, None, "rx")
    check(rx.options == ports_opts and ":option-disable" not in rx.props_on and rx.client.sent[-1].endswith("= {}"),
          "map gone: the dropdown goes back to how it was")
    other = _FakeSel(dict(ports_opts), "TRx1B")
    ppu.apply(other, None, "tx")
    check(other.options == ports_opts and other.value == "TRx1B" and not other.props_on,
          "no map at all: nothing touched")
    plan = _FakeSel({"": "radio's current", "none": "none (TX only)", **ports_opts}, "TRx2B")
    ppu.apply(plan, {"pm": ROLES2, "v": 1, "err": None, "sig": ()}, "rx", skip=("", "none"), move=False)
    check(plan.value == "TRx2B" and plan.options[""] == "radio's current"
          and plan.options["TRx2B"] == "TRx2B (TX only)", "planner: stored choice kept, blank and none left alone")
    check(ppu.lock_note(view, "TRx1A", "TRx1B")[1] is True, "note: locked pair is a refusal")
    txt, refused = ppu.lock_note(view, "TRx1B", "TRx2B")
    check("coupling" in txt and refused is False, "note: same-card pair is a warning")
    check(ppu.lock_note(view, "TRx1A", "TRx2B") == (None, False), "note: the proven pair says nothing")
    check(ppu.lock_note(None, "TRx1B", "TRx2B")[1] is False and "coupling" in ppu.lock_note(None, "TRx1B", "TRx2B")[0],
          "note: the same-card warning does not need a map")
    check(ppu._clean("a'b\"c<d>{e}") == "abcde", "chain text cannot break out of the template")


LIVE_FILE = """# rf_frontend.yaml -- generated by ./deploy.sh --frontend 2026-11-01.
# FAIL-SAFE: a hard kill leaves the HPA enable pin at its last level.
config_version: 1
frontend: mock
# tx_level 0: the HPA is on the switch engraved J2 (switch mounted upside down)
tr_switch:   {mode: gpio, chip: gpiochip0, line: cpu-gp-out2, fallback_line: PN.01, tx_level: 0, settle_ms: 1}
watchdog_ms: 500
atten:       {transport: "none", path: auto, url: http://192.168.0.40, startup_db: 120, max_db: 120, step_db: 0.05}
"""


def test_file_round_trip(tmp_path):
    _file_round_trip(str(tmp_path))


def _file_round_trip(tmp):
    print("[9] ports: section on disk")
    path = _yaml_file(tmp, LIVE_FILE, "live.yaml")
    os.chmod(path, 0o640)
    cfg = rf.write_ports(path, PAYLOAD2, "GCS-X86 engineering view, 2026-11-03 19:40 UTC")
    text = open(path).read()
    check(text.startswith(LIVE_FILE), "added at the end: every earlier line is as it was, comments too")
    check(cfg.ports == PAYLOAD2 and rf.load_config(path).ports == PAYLOAD2, "reads back as written")
    check(rf.load_config(path).tr_switch.tx_level == 0, "tr_switch tx_level 0 kept")
    check(rf.read_ports_stamp(path) == "GCS-X86 engineering view, 2026-11-03 19:40 UTC", "who set it is in the file")
    check(os.stat(path).st_mode & 0o777 == 0o640, "file mode kept")
    check(not [f for f in os.listdir(tmp) if ".tmp" in f], "no temporary file left")

    rf.write_ports(path, {"TRx1A": {"role": "rx", "chain": 'a "quoted" chain: 1, 2'}}, "second")
    t2 = open(path).read()
    check(t2.startswith(LIVE_FILE) and t2.count("ports:") == 1 and t2.count("TRx2B") == 0,
          "a second write replaces the section, it does not add another")
    check(rf.load_config(path).ports["TRx1A"]["chain"] == 'a "quoted" chain: 1, 2', "quotes in chain text survive")
    check(rf.read_ports_stamp(path) == "second", "stamp replaced")
    rf.write_ports(path, {}, "third")
    t3 = open(path).read()
    check(t3.rstrip() == LIVE_FILE.rstrip() and rf.load_config(path).ports == {}, "clear removes the section and only that")
    check(rf.read_ports_stamp(path) is None, "no section, no stamp")

    mid = LIVE_FILE.replace("watchdog_ms: 500\n",
                            "ports:\n  # by hand\n  TRx1A: rx\n\n# above the watchdog\nwatchdog_ms: 500\n")
    path2 = _yaml_file(tmp, mid, "mid.yaml")
    rf.write_ports(path2, PAYLOAD2, "x")
    t = open(path2).read()
    check("\n\n# above the watchdog\nwatchdog_ms: 500\n" in t and "by hand" not in t
          and t.index("TRx2B") < t.index("watchdog_ms") and rf.load_config(path2).watchdog_ms == 500,
          "a section in mid file is swapped in place, the next key's comment stays with it")
    one = _yaml_file(tmp, LIVE_FILE + "ports: {TRx1A: rx}\n", "flow.yaml")
    rf.write_ports(one, PAYLOAD2)
    check(rf.load_config(one).ports == PAYLOAD2, "a one line flow section is replaced")

    new = os.path.join(tmp, "new.yaml")
    cfg = rf.write_ports(new, PAYLOAD2, "x")
    check(cfg.frontend == "none" and rf.load_config(new).ports == PAYLOAD2, "no file yet: created, front end still none")

    for name, bad_text, why in (("broken.yaml", "frontend: [none\n", "cannot parse"),
                                ("json.yaml", '{"frontend": "none", "watchdog_ms": 100}\n', "cannot parse"),
                                ("badkey.yaml", "frontend: none\nnosuchkey: 1\n", "nosuchkey")):
        p = _yaml_file(tmp, bad_text, name)
        try:
            rf.write_ports(p, PAYLOAD2)
            check(False, f"{name} should be refused")
        except rf.ConfigError as e:
            check(why in str(e) and open(p).read() == bad_text, f"{name}: refused, file not touched ({str(e)[:50]})")
    try:
        rf.write_ports(path, {"TRx1A": {"role": "both"}})
        check(False, "a bad role should be refused")
    except rf.ConfigError:
        check(open(path).read() == t3, "a bad role never reaches the file")


async def _set_ports_cases(tmp):
    print("[10] set_ports")
    path = _yaml_file(tmp, LIVE_FILE, "svc.yaml")
    svc = _svc(path)
    svc.loop = asyncio.get_running_loop()
    svc._frontend_start()
    run = _Run(svc)
    fe = svc.frontend
    check(fe is not None and not svc._port_map, "starts with a mock front end and no map")
    a = await run("set_ports", {"by": "t"})
    check(a.status == CmdStatus.REJECTED and "clear" in a.result["err"], "no ports and no clear: refused")
    for bad, word in (({"TRx1A": "both"}, "both"), ({"TRx1A": "tx"}, "RX"), ({"RXB1": "rx"}, "unknown jack")):
        a = await run("set_ports", {"ports": bad, "by": "t"})
        check(a.status == CmdStatus.REJECTED and word in a.result["err"], f"{bad} refused: {a.result['err'][:50]}")
    check(open(path).read() == LIVE_FILE, "a refused change never reaches the file")
    svc._tx_running = lambda: True
    a = await run("set_ports", {"ports": PAYLOAD2, "by": "t"})
    check(a.status == CmdStatus.REJECTED and "stop TX" in a.result["err"] and open(path).read() == LIVE_FILE,
          "refused while TX runs")
    svc._tx_running = lambda: False

    a = await run("set_ports", {"ports": PAYLOAD2, "by": "GCS-X86 engineering view"})
    check(a.status == CmdStatus.OK and a.result["ports_full"] == PAYLOAD2, "accepted")
    check(len(proto.encode(a, 1)) < proto.MAX_DATAGRAM, "the ack fits one datagram")
    check(rf.load_config(path).ports == PAYLOAD2 and open(path).read().startswith(LIVE_FILE),
          "written to the radio's own file, the rest unchanged")
    check(svc.frontend is fe, "applied with no front-end reload: the PA and switch lines are left alone")
    a = await run("get_config")
    check(a.result.get("ports") == ROLES2 and "GCS-X86 engineering view" in a.result.get("ports_by", ""),
          "get_config follows at once and says who")
    size = len(proto.encode(a, 1))
    check(size < proto.MAX_DATAGRAM - 100, f"snapshot with ports_by is {size} bytes")
    a = await run("init", _init("RXA1", "TXA1", 0, 1))
    check(_refused(a, "TRx1B", "TX on"), "the new map is enforced straight away")
    a = await run("init", _init("RXA1", "TXA2", 0, 1))
    check(a.status == CmdStatus.OK, "the allowed pair still inits")
    a = await run("set_ports", {"ports": PAYLOAD2, "by": "someone else"})
    check(a.status == CmdStatus.OK and a.result.get("noop") and "someone else" not in open(path).read(),
          "the same map again is a no-op, the stamp stays")

    changed = {**PAYLOAD2, "TRx1A": {"role": "none", "chain": ""}}
    a = await run("set_ports", {"ports": changed, "by": "t"})
    check(a.status == CmdStatus.OK and "TRx1A" in a.result.get("ports_conflict", ""),
          "locking the open RX jack is allowed and the ack says the radio is on it")
    await run("close")

    svc2 = _svc(path)
    svc2.loop = asyncio.get_running_loop()
    svc2._frontend_start()
    check(svc2._port_map == changed and svc2._port_by and svc2._port_by.startswith("t, "),
          "a restarted service reads the map and who set it from the file")

    with open(path, "a") as fh:
        fh.write("  TRx2A: {role: none, chain: by hand}\n")   # inside the block, last wins
    a = await _Run(svc2)("frontend_reload")
    a = await _Run(svc2)("get_config")
    check(str(a.result.get("ports_by", "")).startswith("config file reload"),
          "a hand edit picked up by frontend_reload says so")

    a = await run("set_ports", {"clear": True, "by": "t2"})
    check(a.status == CmdStatus.OK and rf.load_config(path).ports == {} and not svc._port_map,
          "clear removes the map")
    a = await run("get_config")
    check("ports" not in a.result and a.result.get("ports_by", "").startswith("t2"),
          "no map reported, and who removed it is")
    a = await run("init", _init("RXA1", "TXA1", 0, 1))
    check(a.status == CmdStatus.OK, "nothing refused any more")
    await run("close")

    broke = _yaml_file(tmp, "frontend: [none\n", "broke.yaml")
    svc3 = _svc(path)
    svc3.loop = asyncio.get_running_loop()
    svc3._frontend_start()
    svc3._fe_path = broke
    a = await _Run(svc3)("set_ports", {"ports": PAYLOAD2, "by": "t"})
    check(a.status == CmdStatus.FAILED and "cannot parse" in a.result["err"]
          and open(broke).read() == "frontend: [none\n", "a file that does not parse: failed, not touched")

    svc4 = _svc(path)
    svc4.loop = asyncio.get_running_loop()
    svc4._frontend_start()
    svc4._role = "dcfe"
    a = await _Run(svc4)("set_ports", {"ports": PAYLOAD2, "by": "t"})
    check(a.status == CmdStatus.REJECTED and "dCFE" in a.result["err"], "the dCFE role refuses it, like any other command")

    big = {f"J{i}": {"role": "txrx", "chain": "x" * pm.CHAIN_MAX} for i in range(pm.JACKS_MAX)}
    check(len(proto.encode(proto.Cmd(1, "set_ports", {"ports": big, "by": "x" * 40}), 1)) <= proto.MAX_DATAGRAM,
          "the largest map fits one command datagram")
    check(pm.locks_gained(ROLES2, {**ROLES2, "TRx1A": "txrx"}, "tx") == ["TRx1A"]
          and pm.locks_gained(None, ROLES2, "tx") == [], "locks_gained names what a change opens up")
    check(ppu.change_lines(ROLES2, {**ROLES2, "TRx1A": "none"}) == ["TRx1A: RX only to not connected"],
          "the confirm dialog says what changes")
    seen = []
    ppu.set_event_log(seen.append)
    h = SimpleNamespace(serial="R1")
    ppu.port_view(h, {"ports_by": "a"})
    ppu.port_view(h, {"ports_by": "a"})
    check(seen == [], "first sight of a radio logs nothing")
    ppu.port_view(h, {"ports_by": "b"})
    check(seen == ["R1 port map removed, by b"], f"a change is logged once, with who: {seen}")
    ppu.set_event_log(None)


async def _per_radio_cases(tmp):
    print("[11] per-radio lockout")
    check(pm.jacks_of("g40") == list(pm.G40_JACKS) and pm.jacks_of("mock") == list(pm.G40_JACKS),
          "a G40 (and the mock) lists the four TRx jacks")
    check(pm.jacks_of("x40") == ["RXA1", "RXA2", "RXB1", "RXB2", "TXA1"], "an X40 lists its four RX jacks and TXA1")
    check(pm.jacks_of("b200", sdr.backend_caps("b200")) == ["RX2", "TX/RX"],
          "another kind takes its jacks from the capability list, a shared jack once")
    check(pm.jacks_of("x410", {"rx_antennas": ["Auto", "RX0"], "tx_antennas": ["TX0"]}) == ["RX0", "TX0"],
          "Auto is not a jack")
    check(pm.jacks_of("something") == [] and pm.jacks_of(None) == [], "unknown kind: no jacks, no support")
    check(pm.jacks_of("b200", {"port_jacks": ["J1", "J2"]}) == ["J1", "J2"], "the service's own list wins")
    check(pm.roles_for("x40", "RXB1") == ("rx", "none") and pm.roles_for("x40", "TXA1") == ("tx", "none")
          and pm.roles_for("g40", "TRx1A") == pm.ROLES and pm.roles_for("b200", "RX2") == pm.ROLES,
          "an X40 receive jack is rx or none, its transmitter tx or none, others all four")
    check(pm.unknown_jack("x40", {"TRx1A": "rx"}) == "TRx1A" and pm.unknown_jack("g40", {"RXB1": "rx"}) == "RXB1"
          and pm.unknown_jack("x40", {"RXB1": "rx", "TXA1": "tx"}) is None
          and pm.unknown_jack("b200", {"anything": "rx"}) is None, "unknown_jack checks against the kind's list")
    check(pm.kind_text("x40") == "X40" and pm.kind_text(None) == "unknown kind", "kind text")
    x40map = {"RXB1": {"role": "rx", "chain": "LNA, antenna"}, "RXB2": {"role": "none", "chain": ""},
              "RXA1": {"role": "none", "chain": ""}, "RXA2": {"role": "none", "chain": ""},
              "TXA1": {"role": "tx", "chain": "pad, HPA"}}
    check(pm.ordered(x40map) == ["RXA1", "RXA2", "RXB1", "RXB2", "TXA1"], "X40 jacks in panel order")
    check(pm.jacks_for(x40map, "rx") == ["RXB1"] and pm.jacks_for(x40map, "tx") == ["TXA1"], "X40 map sides")

    path = _yaml_file(tmp, "frontend: mock\n", "x40.yaml")
    svc = _svc(path, kind="x40", host_type=HostType.X40_TX)
    svc.loop = asyncio.get_running_loop()
    svc._frontend_start()
    run = _Run(svc)
    a = await run("caps")
    check(a.status == CmdStatus.OK and a.result.get("port_jacks") == pm.jacks_of("x40"),
          "caps carries the X40 jack list")
    a = await run("set_ports", {"ports": {"TRx1A": "rx", "TRx1B": "tx"}, "by": "t"})
    check(a.status == CmdStatus.REJECTED and "X40 has" in a.result["err"] and "TRx1A" in a.result["err"],
          f"an X40 refuses G40 jack names: {a.result.get('err')}")
    a = await run("set_ports", {"ports": x40map, "by": "t"})
    check(a.status == CmdStatus.OK and a.result["ports_full"] == x40map, "an X40 map is written")
    a = await run("get_config")
    check(a.result.get("ports") == pm.compact(x40map), "and reported in get_config")
    g = _svc(path if False else _yaml_file(tmp, "frontend: mock\n", "g40b.yaml"))
    g.loop = asyncio.get_running_loop()
    g._frontend_start()
    a = await _Run(g)("set_ports", {"ports": {"RXB1": "rx"}, "by": "t"})
    check(a.status == CmdStatus.REJECTED and "G40 has" in a.result["err"], "a G40 still refuses X40 names")
    a = await _Run(g)("caps")
    check(a.result.get("port_jacks") == list(pm.G40_JACKS), "caps carries the G40 jack list")

    view_on = {"pm": x40map, "v": 1, "err": None, "sig": (1, True, None)}
    check(ppu.lockout_title("X40-SIM", "x40", pm.jacks_of("x40"), view_on) == "X40-SIM (X40): lockout on"
          and ppu.lockout_title("G40-SIM", "g40", pm.jacks_of("g40"), None)
          == "G40-SIM (G40): no lockout, every jack can do TX and RX"
          and "no port map support" in ppu.lockout_title("R", "odd", [], None)
          and ppu.lockout_title(None, None, [], None) == "no radio selected"
          and "did not load: boom" in ppu.lockout_title("R", "g40", ["TRx1A"], {"err": "boom", "pm": None}),
          "the panel title names the radio, its kind and the map state")
    ed = ppu.LockoutEdits()
    ed.keep("G40-SIM", {"TRx1A": {"role": "rx", "chain": "x"}})
    check(ed.take("G40-SIM") == {"TRx1A": {"role": "rx", "chain": "x"}} and ed.take("X40-SIM") is None
          and ed.others("X40-SIM") == ["G40-SIM"] and ed.others("G40-SIM") == [],
          "an unapplied edit is kept for its radio and named on the others")
    ed.drop("G40-SIM")
    check(ed.take("G40-SIM") is None and ed.others("X40-SIM") == [], "applied or reverted: dropped")


def main():
    test_loader()
    test_jack_table()
    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(_service_cases(tmp))
    test_mission()
    test_gui_helper()
    with tempfile.TemporaryDirectory() as tmp:
        _file_round_trip(tmp)
    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(_set_ports_cases(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(_per_radio_cases(tmp))
    print(f"\n{'FAILED ' + str(len(FAILS)) if FAILS else 'all passed'}")
    for f in FAILS:
        print("  - " + f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
