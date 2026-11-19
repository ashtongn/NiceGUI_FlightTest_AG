"""./deploy.sh --frontend: frontend_setup.py asks by G40 socket pin, generates
rf_frontend.yaml from answers (HPA / switch presets, a custom set, interactive
prompts), the file parses with rf_frontend.py's own loader with the expected values,
bad pins / answers / files are rejected, unknown-polarity parts force an answer, and
the reload report reads bench/rf_frontend_bench.py output correctly.  Also the
pin-16 direction line in rf_frontend.py.  All offline."""
import io
import os
import subprocess
import sys

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dpmg.payload import frontend_setup as fs  # noqa: E402
from dpmg.payload import rf_frontend as rf  # noqa: E402

LIVE_G40 = """\
# Bench config: fault input disabled until something is wired to pin 18.
config_version: 1
frontend: gpio
pa_enable:   {chip: gpiochip0, line: cpu-gp-out1, fallback_line: PG.06, active_high: true, settle_ms: 2, hang_ms: 20}
tr_switch:   {mode: gpio, chip: gpiochip0, line: cpu-gp-out2, fallback_line: PN.01, tx_level: 1, settle_ms: 1}
pa_fault_in: {enabled: false, chip: gpiochip1, line: cpu-gp-in, fallback_line: PCC.01, active_low: true}
watchdog_ms: 500
atten: {transport: none, path: auto}
"""


def gen(tmp_path, *args, current=None):
    out = tmp_path / "rf_frontend.yaml"
    argv = ["generate", "--out", str(out), "--batch", *args]
    if current is not None:
        cur = tmp_path / "current.yaml"
        cur.write_text(current)
        argv += ["--current", str(cur)]
    rc = fs.main(argv)
    return rc, (rf.load_config(str(out), required=True) if rc == 0 else None), out


# HPA presets
@pytest.mark.parametrize("args,active_high,fault_enabled,fault_low", [
    (["--hpa", "empower-1189"], False, False, True),
    (["--hpa", "empower-1193"], True, True, False),
    (["--hpa", "empower-1212"], True, True, False),
    (["--hpa", "empower-1208", "--pa-active-high", "false", "--fault-pin", "none"], False, False, True),
    (["--hpa", "empower-1211", "--pa-active-high", "true", "--fault-pin", "18",
      "--fault-level", "low"], True, True, True),
])
def test_hpa_presets(tmp_path, capsys, args, active_high, fault_enabled, fault_low):
    rc, cfg, out = gen(tmp_path, *args)
    assert rc == 0
    assert cfg.frontend == "gpio"
    assert cfg.pa_enable.active_high is active_high
    assert (cfg.pa_enable.chip, cfg.pa_enable.line, cfg.pa_enable.fallback_line) == \
        ("gpiochip0", "cpu-gp-out1", "PG.06")
    assert cfg.tr_switch.mode == "gpio" and cfg.tr_switch.tx_level == 1
    assert (cfg.tr_switch.line, cfg.tr_switch.fallback_line) == ("cpu-gp-out2", "PN.01")
    assert cfg.pa_fault_in.enabled is fault_enabled
    if fault_enabled:
        assert cfg.pa_fault_in.active_low is fault_low
    assert (cfg.pa_fault_in.chip, cfg.pa_fault_in.fallback_line) == ("gpiochip1", "PCC.01")
    assert cfg.watchdog_ms == 500
    text = capsys.readouterr().out
    assert "FAIL-SAFE" in text and "control selects RX" in text
    if active_high:
        assert "pull-down (10-47 kOhm)" in text
    else:
        assert "A pull-down would turn THIS" in text
    if args[1] == "empower-1189":
        assert "5 V TTL" in text and "level shifter" in text
    if args[1] in ("empower-1208", "empower-1211"):
        assert "polarity is NOT on file" in text
    assert fs.read_meta(out.read_text())["hpa"] == args[1]


@pytest.mark.parametrize("args,flag", [
    (["--hpa", "empower-1208"], "--pa-active-high"),                       # polarity unknown
    (["--hpa", "empower-1211", "--pa-active-high", "true", "--fault-pin", "18"],
     "--fault-level"),                                                     # alarm level unknown
    (["--switch", "pe71s2041"], "--tr-tx-level"),                          # switch level unknown
])
def test_unknown_polarity_must_be_given(tmp_path, capsys, args, flag):
    rc, _, out = gen(tmp_path, *args)
    assert rc == 2 and flag in capsys.readouterr().err and not out.exists()


def test_unknown_polarity_has_no_default_interactively():
    out = io.StringIO()
    with pytest.raises(fs.AnswerError):          # EOF on a required question: no silent default
        fs.collect_answers({"kind": "gpio", "hpa": "empower-1208", "pa_pin": 17},
                           inp=io.StringIO("\n"), out=out)
    assert "an answer is required here" in out.getvalue()


def test_switch_preset_and_level_shifter(tmp_path, capsys):
    rc, cfg, out = gen(tmp_path, "--hpa", "1189", "--switch", "pe71s2041", "--tr-tx-level", "0",
                       "--level-shifter", "pullup")
    assert rc == 0 and cfg.tr_switch.tx_level == 0 and not cfg.pa_enable.active_high
    text = capsys.readouterr().out
    assert "read HIGH whenever the G40 is off" in text
    assert "Two bidirectional translators in series" in text and "scope" in text
    assert "consistent" in text and "DANGER" not in text           # 1189 + pull-up = OK
    assert "The pull-up shifter supplies the 5 V level" in text
    rc, cfg, _ = gen(tmp_path, "--hpa", "1193", "--level-shifter", "pullup")   # active-high + pull-up
    text = capsys.readouterr().out
    assert rc == 0 and text.count("DANGER") == 2                  # HPA and switch (tx_level 1)


# pins
def test_custom_pin_answers(tmp_path, capsys):
    rc, cfg, out = gen(tmp_path, "--hpa", "custom", "--pa-pin", "15", "--pa-active-high", "false",
                       "--switch-pin", "17", "--tr-tx-level", "0", "--fault-pin", "18",
                       "--fault-level", "low", "--watchdog-ms", "250")
    assert rc == 0
    assert (cfg.pa_enable.line, cfg.pa_enable.fallback_line) == ("cpu-gp-out2", "PN.01")
    assert cfg.pa_enable.active_high is False
    assert (cfg.tr_switch.line, cfg.tr_switch.fallback_line, cfg.tr_switch.tx_level) == \
        ("cpu-gp-out1", "PG.06", 0)
    assert cfg.pa_fault_in.enabled is True and cfg.pa_fault_in.active_low is True
    assert cfg.watchdog_ms == 250
    text = capsys.readouterr().out
    assert "socket pin 15 = /dev/gpiochip0:85" in text      # HPA resolves via the pad fallback
    assert "socket pin 17 = /dev/gpiochip0:41" in text
    assert "socket pin 18 = /dev/gpiochip1:13" in text
    assert "socket pin 15\n!!   (HPA enable)" in text       # fail-safe names the real HPA pin


def test_pin16_sets_direction_line(tmp_path, capsys):
    rc, cfg, _ = gen(tmp_path, "--pa-pin", "16", "--switch-pin", "17")
    assert rc == 0
    pa = cfg.pa_enable
    assert (pa.chip, pa.line, pa.fallback_line) == ("gpiochip0", "cpu-gpio", "PP.06")
    assert (pa.dir_chip, pa.dir_line, pa.dir_fallback_line) == ("gpiochip1", "cpu-gpio-direction", "PCC.03")
    text = capsys.readouterr().out
    assert "socket pin 16 = /dev/gpiochip0:98" in text
    assert "PCC.03 direction for pin 16 = /dev/gpiochip1:15" in text
    assert "needs the rf_frontend.py from this tree" in text


@pytest.mark.parametrize("pin", [7, 8, 9, 10])
def test_proxy_switch_pins(tmp_path, capsys, pin):
    rc, cfg, out = gen(tmp_path, "--switch-pin", str(pin))
    assert rc == 0 and cfg.tr_switch.mode == "proxy"
    assert "NOT YET SCOPED" in capsys.readouterr().out
    assert fs.read_meta(out.read_text())["switch_pin"] == str(pin)


@pytest.mark.parametrize("args,why", [
    (["--pa-pin", "1"], "power"), (["--pa-pin", "26"], "power"),
    (["--pa-pin", "3"], "ground"), (["--switch-pin", "20"], "ground"),
    (["--pa-pin", "21"], "SPI0/I2C-1"), (["--switch-pin", "27"], "SPI0/I2C-1"),
    (["--pa-pin", "5"], "NV100 FPGA GPIO"), (["--switch-pin", "12"], "NV100 FPGA GPIO"),
    (["--pa-pin", "9"], "only be the switch's proxy"),
    (["--pa-pin", "18"], "input-only"), (["--switch-pin", "18"], "input-only"),
    (["--fault-pin", "17"], "used here as an output"),
    (["--fault-pin", "8"], "TR_N output, not an input"),
    (["--fault-pin", "16"], "used here as an output"),
    (["--fault-pin", "3"], "ground"),
    (["--pa-pin", "28"], "does not exist"), (["--pa-pin", "x"], "socket pin number"),
    (["--pa-pin", "15", "--switch-pin", "15"], "one pin cannot have two roles"),
    (["--pa-pin", "16", "--switch-pin", "16"], "one pin cannot have two roles"),
    (["--tr-tx-level", "2"], "TX level must be 1 or 0"),
    (["--watchdog-ms", "abc"], "number of ms"),
    (["--watchdog-ms", "10"], "0 (off) or 50"),
    (["--kind", "real"], "HPA connection must be"),
    (["--hpa", "empower-9999"], "HPA must be"),
    (["--switch", "foo"], "switch must be"),
    (["--level-shifter", "maybe"], "level shifter must be"),
    (["--hpa", "empower-1193", "--pa-active-high", "false"], "contradicts the preset"),
    (["--hpa", "empower-1189", "--fault-pin", "18"], "has no alarm output"),
    (["--hpa", "empower-1212", "--fault-level", "low"], "contradicts the preset"),
    (["--tr-mode", "proxy", "--switch-pin", "15"], "needs a TR_N pin"),
    (["--tr-mode", "gpio", "--switch-pin", "9"], "cannot use TR_N pin"),
])
def test_bad_answers_rejected(tmp_path, capsys, args, why):
    rc, _, out = gen(tmp_path, *args)
    assert rc == 2
    assert why in capsys.readouterr().err
    assert not out.exists()                                  # nothing written


def test_shorthands(tmp_path):
    rc, cfg, _ = gen(tmp_path, "--tr-mode", "proxy", "--pa-fault", "low", "--tr-pin", "8")
    assert rc == 0 and cfg.tr_switch.mode == "proxy"
    assert cfg.pa_fault_in.enabled and cfg.pa_fault_in.active_low
    rc, cfg, _ = gen(tmp_path, "--preset", "1193", "--pa-fault", "none")
    assert rc == 0 and cfg.pa_fault_in.enabled is False and cfg.pa_enable.active_high


def test_mock_and_none(tmp_path, capsys):
    rc, cfg, _ = gen(tmp_path, "--kind", "mock")
    assert rc == 0 and cfg.frontend == "mock"
    assert "frontend mock" in capsys.readouterr().out
    rc, cfg, _ = gen(tmp_path, "--kind", "none")
    assert rc == 0 and cfg.frontend == "none"


def test_current_file_is_reproduced_and_atten_kept(tmp_path):
    cur = LIVE_G40.replace("settle_ms: 2, hang_ms: 20", "settle_ms: 3, hang_ms: 40")
    rc, cfg, out = gen(tmp_path, current=cur)
    assert rc == 0
    want = rf.config_from_dict(yaml.safe_load(cur)).to_dict()
    got = cfg.to_dict()
    want.pop("source"), got.pop("source")
    assert got == want                                       # batch + --current = same config
    assert cfg.atten.path == "auto" and cfg.pa_enable.hang_ms == 40
    rc, cfg, _ = gen(tmp_path, "--tr-tx-level", "0", current=cur)   # change one answer only
    assert rc == 0 and cfg.tr_switch.tx_level == 0 and cfg.pa_enable.settle_ms == 3


def test_regenerate_keeps_payload_guard(tmp_path):
    # the payload guard section is added by hand; rerunning the setup must not drop
    # it, or the HPA keys with no RX protection check
    cur = LIVE_G40 + ("payload:\n  profile: P1\n  auto_protect: true\n  restore_rx_atten: true\n"
                      "  min_rx_atten_db: 20\n")
    rc, cfg, out = gen(tmp_path, current=cur)
    assert rc == 0
    assert cfg.payload is not None
    assert cfg.payload["profile"] == "P1" and cfg.payload["min_rx_atten_db"] == 20
    assert cfg.payload["auto_protect"] is True and cfg.payload["restore_rx_atten"] is True
    rc, cfg, _ = gen(tmp_path, current=LIVE_G40)             # no section stays no section
    assert rc == 0 and cfg.payload is None and "payload" not in out.read_text()


def test_regenerate_keeps_the_port_map(tmp_path):
    # the lockout is set by hand or from the GUI; a rerun must not unlock the jacks
    cur = LIVE_G40 + ("ports:\n"
                      "  # last set by GCS-X86 engineering view, 2026-11-03\n"
                      "  TRx2B: {role: tx, chain: \"20 dB pad, HPA, switch, antenna\"}\n"
                      "  TRx1A: {role: rx, chain: \"splitter, LNA\"}\n")
    rc, cfg, out = gen(tmp_path, current=cur)
    assert rc == 0 and cfg.ports["TRx2B"]["role"] == "tx" and cfg.ports["TRx1A"]["role"] == "rx"
    assert cfg.ports["TRx2B"]["chain"] == "20 dB pad, HPA, switch, antenna"
    assert "# last set by GCS-X86 engineering view, 2026-11-03" in out.read_text()
    rc, cfg, out = gen(tmp_path, current=LIVE_G40)           # no map stays no map
    assert rc == 0 and not cfg.ports and "ports:" not in out.read_text()


def test_regenerate_keeps_adc_sections(tmp_path):
    # tr_readback and hpa_monitor are added by hand too; a rerun must keep them,
    # values and all, and a disabled or absent section stays out
    cur = LIVE_G40 + ("tr_readback:\n  enabled: true\n  transport: i2c\n  address: 0x48\n"
                      "  channel: 0\n  divider: 2\n  tx_window_v: [2.7, 5.5]\n  rx_window_v: [-0.5, 0.8]\n"
                      "hpa_monitor:\n  enabled: true\n  address: 0x49\n  temp_channel: 3\n"
                      "  current_channel: 1\n  warn_c: 65\n  poll_s: 1.5\n")
    rc, cfg, out = gen(tmp_path, current=cur)
    assert rc == 0
    rb, hm = cfg.tr_readback, cfg.hpa_monitor
    assert rb.enabled and rb.transport == "i2c" and rb.address == 0x48 and rb.divider == 2.0
    assert rb.tx_window_v == [2.7, 5.5] and rb.rx_window_v == [-0.5, 0.8]
    assert hm.enabled and hm.address == 0x49 and (hm.temp_channel, hm.current_channel) == (3, 1)
    assert hm.warn_c == 65.0 and hm.poll_s == 1.5 and hm.temp_v_per_c == 0.01
    txt = out.read_text()
    assert "address: 0x48" in txt and "address: 0x49" in txt
    rc, cfg, out = gen(tmp_path, current=cur.replace("hpa_monitor:\n  enabled: true",
                                                     "hpa_monitor:\n  enabled: false"))
    assert rc == 0 and cfg.tr_readback.enabled and not cfg.hpa_monitor.enabled
    assert "hpa_monitor" not in out.read_text()
    rc, cfg, out = gen(tmp_path, current=LIVE_G40)
    assert rc == 0 and not cfg.tr_readback.enabled and "tr_readback" not in out.read_text()


def test_regenerate_from_own_output_keeps_presets_and_entered_polarity(tmp_path):
    rc, _, out = gen(tmp_path, "--hpa", "1208", "--pa-active-high", "false", "--switch", "pe71s2041",
                     "--tr-tx-level", "0", "--switch-pin", "17", "--pa-pin", "15",
                     "--level-shifter", "pullup")
    assert rc == 0
    first = out.read_text()
    out2 = tmp_path / "again.yaml"
    cur = tmp_path / "cur.yaml"
    cur.write_text(first)
    # batch re-run with no answers: the entered polarity of the same part is the default now
    assert fs.main(["generate", "--out", str(out2), "--batch", "--current", str(cur)]) == 0
    a = rf.load_config(str(out2), required=True)
    assert (a.pa_enable.active_high, a.tr_switch.tx_level, a.pa_enable.line) == (False, 0, "cpu-gp-out2")
    assert fs.read_meta(out2.read_text()) == fs.read_meta(first)


def test_interactive_pinout_questions():
    # kind, HPA, HPA pin (ground then re-ask), polarity (none then re-ask, then y), switch model,
    # switch pin (same as HPA then re-ask, then SPI then re-ask, then 15), TX level, alarm pin
    # (input-only check: 17: re-ask, then 18), alarm level, shifter, watchdog
    answers = ("gpio\nempower-1211\n3\n17\n\ny\npe71s2041\n17\n22\n15\n0\n17\n18\nlow\npullup\n300\n")
    out = io.StringIO()
    a = fs.collect_answers({}, inp=io.StringIO(answers), out=out)
    assert {k: a[k] for k in fs.QUESTIONS} == {
        "kind": "gpio", "hpa": "empower-1211", "pa_pin": 17, "pa_active_high": True,
        "switch": "pe71s2041", "switch_pin": 15, "tr_tx_level": 0, "fault_pin": 18,
        "fault_level": "low", "level_shifter": "pullup", "watchdog_ms": 300}
    log = out.getvalue()
    assert "CPU_GP_OUT_1" in log and "NVA_TR_N_1" in log and "+3.3 V" in log   # socket table shown
    assert "socket pin 3 is ground" in log
    assert "an answer is required here" in log
    assert "already the HPA enable" in log
    assert "SPI0/I2C-1 bus pin" in log
    assert "used here as an output" in log
    cfg, resolved = fs.validate_text(fs.build_yaml(a, stamp="test"))
    assert cfg.pa_fault_in.enabled and cfg.pa_fault_in.active_low is True
    assert [r[:2] for r in resolved] == [("HPA enable", 17), ("TX/RX switch", 15), ("HPA alarm", 18)]


def test_pins_table_command(capsys):
    assert fs.main(["pins"]) == 0
    t = capsys.readouterr().out
    assert len([ln for ln in t.splitlines() if ln[:6].replace("*", "").strip().isdigit()]) == 27


# file checks
@pytest.mark.parametrize("text,why", [
    (LIVE_G40.replace("watchdog_ms: 500", "watchdog_ms: 500\npa_enabel: {}"), "unknown key"),
    (LIVE_G40.replace("tx_level: 1", "tx_level: 1, polarity: 1"), "unknown key"),
    # R36 has no logical names: a line with no pad fallback cannot resolve on the G40
    (LIVE_G40.replace("fallback_line: PG.06", 'fallback_line: ""'), "cannot resolve line"),
    (LIVE_G40.replace("fallback_line: PG.06", "fallback_line: PZ.99"), "cannot resolve line"),
    (LIVE_G40.replace("line: cpu-gp-out1, fallback_line: PG.06", "line: 100"), "not a G40 socket line"),
    (LIVE_G40.replace("fallback_line: PN.01", "fallback_line: PG.06"), "already requested"),
    (LIVE_G40.replace("chip: gpiochip0, line: cpu-gp-out1, fallback_line: PG.06",
                      "chip: gpiochip1, line: cpu-gp-in, fallback_line: PCC.01"), "cannot be used as an output"),
    # pin 16 without its direction line
    (LIVE_G40.replace("line: cpu-gp-out1, fallback_line: PG.06", "line: cpu-gpio, fallback_line: PP.06"),
     "needs dir_line"),
])
def test_check_rejects_bad_files(tmp_path, capsys, text, why):
    p = tmp_path / "bad.yaml"
    p.write_text(text)
    assert fs.main(["check", str(p)]) == 2
    assert why in capsys.readouterr().err


def test_check_accepts_live_file(tmp_path, capsys):
    p = tmp_path / "ok.yaml"
    p.write_text(LIVE_G40)
    assert fs.main(["check", str(p)]) == 0
    assert "socket pin 17 = /dev/gpiochip0:41 (resolves by fallback" in capsys.readouterr().out


# rf_frontend dir line
def test_rf_frontend_direction_line_held_high_and_released():
    cfg = rf.config_from_dict(yaml.safe_load(LIVE_G40.replace(
        "line: cpu-gp-out1, fallback_line: PG.06",
        "line: cpu-gpio, fallback_line: PP.06, dir_chip: gpiochip1, dir_line: cpu-gpio-direction, "
        "dir_fallback_line: PCC.03")))
    gpio = rf.MemoryGpio()
    fe = rf.RfFrontend(cfg, gpio=gpio, watchdog_poll_s=3600).open()
    try:
        assert gpio.level("gpiochip1", 15) is True          # translator set to output
        assert gpio.level("gpiochip0", 118) is False         # cpu-gpio (mock table) idle, PA off
        st = fe.status()
        assert st["pa"]["dir"] == "gpiochip1:15" and len(str(st)) < 1400
        fe.tr_switch(True); fe.pa_enable(True); fe.safe("t")
        fe.reload(cfg)                                       # re-request must not collide
        assert gpio.level("gpiochip1", 15) is True
    finally:
        fe.close()
    assert gpio.level("gpiochip1", 15) is None and not gpio.lines   # everything released


def test_rf_frontend_without_dir_keys_is_unchanged():
    cfg = rf.config_from_dict(yaml.safe_load(LIVE_G40))
    gpio = rf.MemoryGpio()
    fe = rf.RfFrontend(cfg, gpio=gpio, watchdog_poll_s=3600).open()
    try:
        assert fe._dirs == [] and "dir" not in fe.status()["pa"]
        assert sorted(k[1] for k in gpio.lines) == [41, 97]
    finally:
        fe.close()


# reload report
def _reload_line(status=0, pa_level=False, tr_level=False, tx_level=1, active_high=True,
                 keyed=False, kind="gpio", pa_offset=41):
    fe = {"frontend": kind, "open": True, "config_source": "/app_svc/rf_frontend.yaml",
          "pa": {"keyed": keyed, "active_high": active_high, "settle_ms": 2.0, "hang_ms": 20.0,
                 "line": "cpu-gp-out1", "chip": "/dev/gpiochip0", "offset": pa_offset, "by": "fallback",
                 "level": pa_level},
          "tr": {"mode": "gpio", "tx": False, "tx_level": tx_level, "settle_ms": 1.0,
                 "line": "cpu-gp-out2", "chip": "/dev/gpiochip0", "offset": 85, "by": "fallback",
                 "level": tr_level},
          "fault": {"enabled": False, "active": None, "active_low": True},
          "hold": False, "watchdog_ms": 500.0, "watchdog_trips": 0, "fault_trips": 0,
          "watchdog_remaining_ms": None, "last_error": None}
    res = {"loaded": "/app_svc/rf_frontend.yaml", "frontend_kind": kind,
           "status": {"configured": kind, "frontend": fe, "atten": None}}
    if status != 0:
        res = {"err": "pa_enable.line is required", "status": res["status"]}
    return f'radio_state: 1 | frontend: {{}}\nreload: {(status, res)!r}\n'


def _cfg(text=LIVE_G40):
    return rf.config_from_dict(yaml.safe_load(text))


def test_report_ok(capsys):
    assert fs.report(_reload_line(), _cfg()) is True
    out = capsys.readouterr().out
    assert "/dev/gpiochip0:41 (socket pin 17, by fallback)" in out
    assert "[OK] HPA enable reads level false" in out and "[OK] switch reads level false (RX)" in out


def test_report_tx_level0_rx_reads_true(capsys):
    cfg = _cfg(LIVE_G40.replace("tx_level: 1", "tx_level: 0"))
    assert fs.report(_reload_line(tx_level=0, tr_level=True), cfg) is True
    assert "switch reads level true (RX)" in capsys.readouterr().out


@pytest.mark.parametrize("text", [
    _reload_line(status=2),                      # radio rejected the file
    _reload_line(pa_level=True),                 # HPA not idle
    _reload_line(tr_level=True),                 # switch at TX
    _reload_line(active_high=False),             # radio runs something else than the file
    _reload_line(pa_offset=85),                  # HPA on a different pin than the file
    "Traceback ...\nTimeoutError: frontend_reload\n",
    "",
])
def test_report_failures(text):
    assert fs.report(text, _cfg(), out=io.StringIO()) is False


# --atten (DEVICE_WATCH)
BASE = ["--hpa", "empower-1189", "--switch", "pe71s2041", "--pa-pin", "17", "--switch-pin", "15",
        "--tr-tx-level", "1", "--fault-pin", "none"]


@pytest.mark.parametrize("spec,transport,path,url", [
    ("auto", "auto", "auto", None),
    ("none", "none", None, None),
    ("hidraw", "hidraw", "auto", None),
    ("hidraw:/dev/hidraw2", "hidraw", "/dev/hidraw2", None),
    ("http://192.168.0.40", "http", None, "http://192.168.0.40"),
])
def test_atten_option(tmp_path, capsys, spec, transport, path, url):
    rc, cfg, out = gen(tmp_path, *BASE, "--atten", spec, current=LIVE_G40)
    assert rc == 0
    assert cfg.atten.transport == transport
    if path is not None:
        assert cfg.atten.path == path
    if url is not None:
        assert cfg.atten.url == url
    assert cfg.atten.max_db == 120 and cfg.atten.startup_db == 120     # kept from the current file


def test_atten_default_keeps_current_or_auto(tmp_path):
    rc, cfg, _ = gen(tmp_path, *BASE, current=LIVE_G40)                 # current says none
    assert rc == 0 and cfg.atten.transport == "none" and cfg.atten.path == "auto"
    rc, cfg, out = gen(tmp_path, *BASE)                                  # new file: auto
    assert rc == 0 and cfg.atten.transport == "auto" and cfg.atten.path == "auto"
    assert "read only on attach" in out.read_text()
    rc, cfg, _ = gen(tmp_path, *BASE, current="")                       # empty current = new file
    assert rc == 0 and cfg.atten.transport == "auto"


@pytest.mark.parametrize("spec", ["serial", "hidraw:/tmp/x", "http://"])
def test_atten_option_rejects(tmp_path, capsys, spec):
    rc, cfg, out = gen(tmp_path, *BASE, "--atten", spec)
    assert rc == 2 and "--atten" in capsys.readouterr().err
    assert not out.exists()


def test_atten_in_help():
    assert "--atten none|auto|hidraw" in fs.__doc__


# deploy.sh wiring
def test_deploy_sh_option_parsing():
    sh = os.path.join(ROOT, "deploy.sh")
    if not os.path.exists(sh):                    # the Jetson repo has no deploy.sh
        pytest.skip("no deploy.sh in this repo")
    assert subprocess.run(["bash", "-n", sh]).returncode == 0
    h = subprocess.run([sh, "--help"], capture_output=True, text=True)
    assert h.returncode == 0 and "--frontend" in h.stdout and "--switch-pin" in h.stdout
    assert "empower-1208/1211" in h.stdout and "pe71s2041" in h.stdout
    assert "--atten none|auto|hidraw" in h.stdout
    r = subprocess.run([sh, "--frontend-config"], capture_output=True, text=True)
    assert r.returncode == 2 and "needs a FILE" in r.stdout
    r = subprocess.run([sh, "--no-push", "--frontend-config", "x.yaml"], capture_output=True, text=True)
    assert r.returncode == 2 and "cannot be combined" in r.stdout
    r = subprocess.run([sh, "--yes"], capture_output=True, text=True)
    assert r.returncode == 2 and "unknown option" in r.stdout


if __name__ == "__main__":  # OFF-01 loop runs `python3 tests/<file>.py`; delegate to pytest
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))


# port map from flags
def test_port_flags_write_the_map(tmp_path):
    rc, cfg, out = gen(tmp_path, "--hpa", "empower-1189",
                       "--port", "TRx2B=tx:20 dB pad, HPA, T/R switch, antenna",
                       "--port", "trx1a=rx", "--port", "TRx2A=none")
    assert rc == 0
    assert cfg.ports == {"TRx2B": {"role": "tx", "chain": "20 dB pad, HPA, T/R switch, antenna"},
                         "TRx1A": {"role": "rx", "chain": ""},
                         "TRx2A": {"role": "none", "chain": ""}}
    assert "deploy.sh --frontend" in out.read_text()


def test_port_flags_replace_the_current_map_and_clear_removes_it(tmp_path):
    cur = LIVE_G40 + "ports:\n  TRx1A: {role: rx, chain: old}\n  TRx2B: {role: tx, chain: old}\n"
    rc, cfg, out = gen(tmp_path, "--port", "TRx1B=rx", "--port", "TRx2B=tx", current=cur)
    assert rc == 0 and set(cfg.ports) == {"TRx1B", "TRx2B"}
    rc, cfg, out = gen(tmp_path, "--ports-clear", current=cur)
    assert rc == 0 and cfg.ports == {}
    assert "ports:" not in out.read_text()
    rc, cfg, out = gen(tmp_path, current=cur)          # neither flag: the map is kept
    assert rc == 0 and cfg.ports["TRx1A"]["chain"] == "old"


@pytest.mark.parametrize("args,why", [
    (["--port", "TRx9A=tx"], "a G40 has"),
    (["--port", "TRx1A=sideways"], "role"),
    (["--port", "TRx1A"], "JACK=ROLE"),
    (["--port", "TRx2B=tx"], "no jack allows RX"),
    (["--port", "TRx1A=rx", "--ports-clear"], "contradict"),
])
def test_bad_port_flags_rejected(tmp_path, capsys, args, why):
    rc, cfg, out = gen(tmp_path, "--hpa", "empower-1189", *args)
    assert rc == 2
    assert why in capsys.readouterr().err
