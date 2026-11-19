"""Laptop side logging (dpmg/link/ground_log.py): the state and files belong to the
GUI process per radio, not to a page, decodes land in their own CSV, each start gets
new files owned by the folder's owner, and the logging line is correct. No radio,
no page, no sockets."""
import csv
import glob
import logging
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dpmg import paths                                            # noqa: E402
from dpmg.link import ground_log as gl, proto                     # noqa: E402
from dpmg.link import link_client as lc                           # noqa: E402

SER = "G40-SIM"
EST = -5 * 3600
# 2026-11-05 18:28:32 EST
T0 = 1793921312.0


def _status(state=2, flags=0):
    return SimpleNamespace(radio_state=state, flags=flags, overruns=1, underruns=2,
                           fps=30, uptime_s=99, lat_1e7=389000000, lon_1e7=-770000000,
                           alt_m=12.5)


def _det(i):
    return SimpleNamespace(t_unix=T0 + i, freq_hz=915e6 + i, snr_db=20.0, duration_ms=5.0)


def _host(**kw):
    h = SimpleNamespace(serial=SER, status=_status(), connected=True, config={},
                        detections={}, det_head=0, watch_bands=[])
    h.__dict__.update(kw)
    return h


def _logs(tmp_path, hosts):
    return gl.GroundLogs(lambda: hosts, lambda: None, lambda s: {2: "STREAMING"}.get(s, "?"),
                         out_dir=str(tmp_path))


def _files(tmp_path, cat, serial=SER):
    return sorted(glob.glob(str(tmp_path / f"{cat}_{serial}_*.csv")))


def _rows(tmp_path, cat, serial=SER):
    """The rows of the newest file."""
    with open(_files(tmp_path, cat, serial)[-1], newline="", encoding="utf-8") as fp:
        return list(csv.reader(fp))


def _data_rows(tmp_path, cat, serial=SER):
    """The rows of every file, without the headers."""
    out = []
    for path in _files(tmp_path, cat, serial):
        with open(path, newline="", encoding="utf-8") as fp:
            out += list(csv.reader(fp))[1:]
    return out


def _start(glogs, *cats, dst="ground", serial=SER, now=T0):
    return glogs.get(serial).start(dst, {c: True for c in cats}, EST, now=now)


def test_state_outlives_any_page_and_keeps_writing(tmp_path):
    # a page is only a reader: the same object comes back and rows keep coming
    host = _host()
    glogs = _logs(tmp_path, {SER: host})
    _start(glogs, "detections")
    glogs.tick()
    host.detections[0] = _det(0)
    host.detections[1] = _det(1)
    host.det_head = 2
    glogs.tick()                       # no page exists in this test at all
    assert glogs.find(SER) is glogs.get(SER)
    assert gl.label_text(glogs.find(SER), None) == "logging detections to local"
    rows = _rows(tmp_path, "detections")
    assert rows[0] == gl.header("detections", EST)
    assert [r[2] for r in rows[1:]] == ["915000000", "915000001"]  # index 0 counts too


def test_logging_is_per_radio(tmp_path):
    a, b = _host(serial="A"), _host(serial="B")
    glogs = _logs(tmp_path, {"A": a, "B": b})
    _start(glogs, "detections", serial="A")
    _start(glogs, "health", serial="B")
    a.detections[5] = _det(5)
    b.detections[5] = _det(5)
    glogs.tick(now=T0)
    assert len(_rows(tmp_path, "detections", "A")) == 2
    assert len(_rows(tmp_path, "health", "B")) == 2
    assert not _files(tmp_path, "detections", "B")
    glogs.get("A").stop()
    assert gl.label_text(glogs.find("A"), None) == "logging off"
    assert gl.label_text(glogs.find("B"), None) == "logging health to local"


def test_each_start_gets_its_own_files(tmp_path):
    host = _host()
    glogs = _logs(tmp_path, {SER: host})
    paths1 = _start(glogs, "health", "decodes")
    assert sorted(os.path.basename(p) for p in paths1) == [
        f"decodes_{SER}_2026-11-05_182832.csv", f"health_{SER}_2026-11-05_182832.csv"]
    glogs.tick(now=T0)
    glogs.get(SER).stop()
    paths2 = _start(glogs, "health", now=T0 + 60)
    assert [os.path.basename(p) for p in paths2] == [f"health_{SER}_2026-11-05_182932.csv"]
    assert len(_files(tmp_path, "health")) == 2
    assert _rows(tmp_path, "health")[0] == gl.header("health", EST)
    assert len(_rows(tmp_path, "health")) == 1         # the new file is only a header


def test_start_twice_in_a_second_keeps_one_header(tmp_path):
    glogs = _logs(tmp_path, {SER: _host()})
    _start(glogs, "decodes")
    glogs.get(SER).stop()
    glogs2 = _logs(tmp_path, {SER: _host()})
    _start(glogs2, "decodes")
    assert len(_files(tmp_path, "decodes")) == 1
    assert len(_rows(tmp_path, "decodes")) == 1


def test_local_time_column_uses_the_offset_given(tmp_path):
    # the GUI container is on UTC, the browser's offset is what makes this local
    host = _host()
    host.detections[0] = _det(0)
    host.watch_bands = [{"n": "b", "f": 1e6, "m": "FSK", "msg": "x", "dn": 1}]
    glogs = _logs(tmp_path, {SER: host})
    _start(glogs, "detections", "decodes", "health", "position")
    glogs.tick(now=T0)
    for cat in gl.CATEGORIES:
        rows = _rows(tmp_path, cat)
        assert rows[0][:2] == ["gcs_time (UTC-05:00)", "t_unix"], cat
        assert rows[1][0] == "2026-11-05 18:28:32", cat
        assert float(rows[1][1]) == T0, cat
        assert len(rows[1]) == len(rows[0]), cat


def test_header_names_the_clock_and_zone():
    # the column says whose clock it is, and the zone it is written in
    assert gl.zone_text(-18000) == "UTC-05:00"
    assert gl.zone_text(19800) == "UTC+05:30"
    assert gl.zone_text(0) == "UTC"
    assert gl.header("health", -18000)[:2] == ["gcs_time (UTC-05:00)", "t_unix"]
    own = __import__("time").localtime(T0).tm_gmtoff
    assert gl.header("health", None, T0)[0] == f"gcs_time ({gl.zone_text(own)})"


def test_local_time_without_an_offset_is_this_process(tmp_path):
    rl = gl.RadioLog(SER, str(tmp_path))
    rl.start("ground", {"decodes": True}, now=T0)
    assert rl._local(T0) == __import__("time").strftime(
        "%Y-%m-%d %H:%M:%S", __import__("time").localtime(T0))
    assert rl._local(0) == ""


def test_detections_not_written_twice_after_a_cache_clear_or_restart(tmp_path):
    host = _host()
    glogs = _logs(tmp_path, {SER: host})
    _start(glogs, "detections")
    host.detections.update({0: _det(0), 1: _det(1)})
    host.det_head = 2
    glogs.tick()
    # an UNINIT radio clears the cache and the gap fill can fetch the same rows back
    host.detections.clear()
    glogs.tick()
    host.detections.update({0: _det(0), 1: _det(1)})
    glogs.tick()
    assert len(_data_rows(tmp_path, "detections")) == 2
    # stop and start, cache still full: a row sits in one file, not both
    glogs.get(SER).stop()
    _start(glogs, "detections", now=T0 + 60)
    glogs.tick()
    assert len(_data_rows(tmp_path, "detections")) == 2
    # a late gap fill below the newest index still lands
    host.detections[7] = _det(7)
    host.detections[6] = _det(6)
    host.det_head = 8
    glogs.tick()
    host.detections[3] = _det(3)
    glogs.tick()
    assert len(_data_rows(tmp_path, "detections")) == 5
    # a service restart rewinds the index: the new index 0 is a new detection
    host.detections.clear()
    host.detections[0] = _det(50)
    host.det_head = 1
    glogs.tick()
    assert _rows(tmp_path, "detections")[-1][2] == "915000050"


def test_decodes_one_row_per_change_and_quoting(tmp_path):
    host = _host()
    glogs = _logs(tmp_path, {SER: host})
    _start(glogs, "decodes")
    msg = 'HELLO, "WORLD"\nline2'
    host.watch_bands = [
        {"n": "b1", "f": 915.5e6, "m": "QPSK", "p": True, "d": True, "msg": msg, "dn": 3, "tn": 3},
        {"n": "quiet", "f": 916e6, "m": "FSK", "p": True, "d": False, "msg": "", "dn": 0, "tn": 9},
    ]
    glogs.tick(now=T0)
    glogs.tick(now=T0 + 0.25)          # unchanged snapshot, no new row
    rows = _rows(tmp_path, "decodes")
    assert rows[0] == ["gcs_time (UTC-05:00)", "t_unix", "band", "freq_hz", "modulation",
                       "message", "frames"]
    assert rows[1:] == [["2026-11-05 18:28:32", f"{T0:.3f}", "b1", "915500000", "QPSK", msg, "3"]]
    host.watch_bands[0]["dn"] = 5      # same text, more frames
    glogs.tick(now=T0 + 1)
    host.watch_bands[0]["msg"] = "NEXT"
    glogs.tick(now=T0 + 2)
    assert [(r[5], r[6]) for r in _rows(tmp_path, "decodes")[2:]] == [(msg, "5"), ("NEXT", "5")]
    # a band that ages out of the snapshot and comes back is not news
    bands, host.watch_bands = host.watch_bands, []
    glogs.tick(now=T0 + 3)
    host.watch_bands = bands
    glogs.tick(now=T0 + 4)
    assert len(_rows(tmp_path, "decodes")) == 4


def test_decodes_from_real_watch_update_pages(tmp_path):
    # the wire format through HostLink's page merge, as the GUI gets it
    h = lc.HostLink.__new__(lc.HostLink)
    h.watch_bands, h._watch_t, h._watch_map = [], 0.0, {}
    bands = [{"n": f"band_{i:02d}", "f": 900e6 + i * 1e6, "m": "FSK", "p": True, "d": True,
              "msg": f"msg {i}, " + "x" * 40, "dn": i + 1, "tn": i + 1} for i in range(24)]
    for pg in proto.WatchUpdate.paginate(SER, bands):
        h._merge_watch(proto.WatchUpdate.unpack(pg.pack()))
    host = _host(watch_bands=h.watch_bands)
    glogs = _logs(tmp_path, {SER: host})
    _start(glogs, "decodes")
    glogs.tick()
    rows = _rows(tmp_path, "decodes")[1:]
    assert len(rows) == 24
    assert rows[23][2:4] == ["band_23", "923000000"] and rows[23][6] == "24"
    assert rows[23][5] == bands[23]["msg"]


def test_sealed_message_stays_sealed(tmp_path):
    host = _host(watch_bands=[{"n": "s", "f": 1e6, "m": "QPSK", "msg": "[sealed 64B]", "dn": 1}])
    glogs = _logs(tmp_path, {SER: host})
    _start(glogs, "decodes")
    glogs.tick()
    assert _rows(tmp_path, "decodes")[1][5] == "[sealed 64B]"


def test_health_and_position_rows(tmp_path):
    host = _host(config={"clock_source": "gpsdo"},
                 status=_status(flags=proto.Status.FLAG_REF_LOCKED | proto.Status.FLAG_POS_VALID))
    glogs = _logs(tmp_path, {SER: host})
    _start(glogs, "health", "position")
    glogs.tick(now=T0)
    glogs.tick(now=T0 + 0.5)           # about 1 Hz only
    glogs.tick(now=T0 + 1)
    assert _rows(tmp_path, "health")[1][2:] == ["STREAMING", "1", "2", "30", "99", "gpsdo", "1", "", "",
                                                "", "", ""]
    assert len(_rows(tmp_path, "health")) == 3
    row = _rows(tmp_path, "position")[1]
    assert row[2:6] == ["38.900000", "-77.000000", "12.5", "status"]
    assert row[6:] == ["", "", "", "", ""]      # the radio's STATUS has no heading, speed or aircraft
    assert len(row) == len(gl.HEADERS["position"])


def test_lost_link_leaves_a_gap_in_health(tmp_path):
    host = _host()
    glogs = _logs(tmp_path, {SER: host})
    _start(glogs, "health", "position")
    glogs.tick(now=T0)
    host.connected = False
    glogs.tick(now=T0 + 1)
    assert len(_rows(tmp_path, "health")) == 2
    assert _rows(tmp_path, "position")[-1][5] == "no_fix"
    assert len(_rows(tmp_path, "position")[-1]) == len(gl.HEADERS["position"])
    # the host going from the manager's list must not break the task either
    glogs.get_hosts = lambda: {}
    glogs.tick(now=T0 + 2)
    assert glogs.get(SER).on


def test_decodes_are_local_when_logging_on_the_radio(tmp_path):
    glogs = _logs(tmp_path, {SER: _host()})
    rl = glogs.get(SER)
    new = rl.start("g40", {"detections": True, "decodes": True})
    rl.radio_on = True
    assert [os.path.basename(p).split("_2")[0] for p in new] == [f"decodes_{SER}"]
    assert not _files(tmp_path, "detections")
    cfg = {"log": {"detections": "/app_svc/detections.csv", "telemetry": None}}
    assert gl.label_text(rl, cfg) == "logging decodes to local, detections to G40"
    # the radio's report wins over what this GUI asked for
    assert gl.label_text(rl, {"log": {"detections": None, "telemetry": None}}) \
        == "logging decodes to local"
    # stale or older service: what this GUI last asked for
    assert gl.label_text(rl, None) == "logging decodes to local, detections to G40"


def test_label_with_no_laptop_state_reads_the_radio(tmp_path):
    # a fresh GUI process, or logging started elsewhere: the existing G40 path
    cfg = {"log": {"detections": "/app_svc/detections.csv", "telemetry": "/app_svc/t.csv"}}
    assert gl.label_text(None, cfg) == "logging detections, telemetry to G40"
    assert gl.label_text(None, {"log": {}}) == "logging off"
    assert gl.label_text(None, None) == "logging off"


def test_write_failure_stops_and_says_so(tmp_path):
    host = _host()
    glogs = _logs(tmp_path, {SER: host})
    _start(glogs, "detections")
    rl = glogs.get(SER)

    class Full:
        def write(self, _s):
            raise OSError(28, "No space left on device")

        def flush(self):
            pass

        def close(self):
            pass
    rl.fp["detections"] = Full()
    host.detections[0] = _det(0)
    glogs.tick()
    assert not rl.on
    assert "write failed" in gl.label_text(rl, None)


def test_bad_row_does_not_end_the_task(tmp_path):
    host = _host()
    glogs = _logs(tmp_path, {SER: host})
    _start(glogs, "detections")
    host.detections[0] = SimpleNamespace(t_unix=1.0)       # no freq_hz
    glogs.tick()
    assert glogs.get(SER).on


# File ownership. The mocked tests run anywhere. The real ones need root, so they are
# skipped as a user and run in a throwaway container from the GUI image.
def _foreign_stat(monkeypatch, *suffixes):
    real = os.lstat

    def fake(path, *a, **k):
        st = real(path, *a, **k)
        if str(path).endswith(suffixes):
            return SimpleNamespace(st_uid=-1, st_gid=-1, st_mode=st.st_mode)
        return st
    monkeypatch.setattr(paths.os, "lstat", fake)


def test_new_file_goes_to_the_folder_owner_mocked(tmp_path, monkeypatch):
    calls = []
    _foreign_stat(monkeypatch, ".csv")
    monkeypatch.setattr(paths.os, "chown",
                        lambda p, u, g, **k: calls.append((os.path.basename(p), u, g)))
    glogs = _logs(tmp_path, {SER: _host()})
    _start(glogs, "decodes")
    folder = os.stat(tmp_path)
    assert (f"decodes_{SER}_2026-11-05_182832.csv", folder.st_uid, folder.st_gid) in calls


def test_earlier_files_are_corrected_at_start_and_at_gui_start_mocked(tmp_path, monkeypatch):
    # with a new file per start the old ones are never opened again, so a sweep fixes them
    (tmp_path / "health_OLD.csv").write_text("old\n")
    (tmp_path / "bench").mkdir()
    (tmp_path / "bench" / "nested.csv").write_text("old\n")
    calls = []
    _foreign_stat(monkeypatch, ".csv")
    monkeypatch.setattr(paths.os, "chown",
                        lambda p, u, g, **k: calls.append(os.path.basename(p)))
    glogs = _logs(tmp_path, {SER: _host()})
    _start(glogs, "decodes")
    assert "health_OLD.csv" in calls and "nested.csv" in calls
    calls.clear()
    paths.match_owners_under(str(tmp_path))                # what GroundLogs.run does first
    assert "health_OLD.csv" in calls


def test_failed_chown_warns_once_and_logging_goes_on(tmp_path, monkeypatch, caplog):
    def refuse(*_a, **_k):
        raise PermissionError(1, "not allowed")
    _foreign_stat(monkeypatch, ".csv")
    monkeypatch.setattr(paths.os, "chown", refuse)
    paths._owner_warned.clear()
    host = _host(watch_bands=[{"n": "b", "f": 1e6, "m": "FSK", "msg": "x", "dn": 1}])
    glogs = _logs(tmp_path, {SER: host})
    with caplog.at_level(logging.WARNING, logger=paths.__name__):
        _start(glogs, "decodes", "health")
        glogs.get(SER).stop()
        _start(glogs, "decodes", now=T0 + 60)
    assert glogs.get(SER).on
    glogs.tick()
    assert len(_data_rows(tmp_path, "decodes")) == 1
    assert len([r for r in caplog.records if "cannot give" in r.getMessage()]) == 1


def test_symlink_is_left_alone_mocked(tmp_path, monkeypatch):
    target = tmp_path.parent / "elsewhere.txt"
    target.write_text("x")
    (tmp_path / "link.csv").symlink_to(target)
    calls = []
    monkeypatch.setattr(paths.os, "chown", lambda *a, **k: calls.append(a))
    paths.match_owners_under(str(tmp_path))
    assert calls == []


need_root = pytest.mark.skipif(os.geteuid() != 0, reason="needs root: run in a throwaway container")


@need_root
def test_root_created_file_ends_up_with_the_folder_owner(tmp_path):
    os.chown(tmp_path, 1000, 1000)
    old = tmp_path / "health_OLD.csv"
    old.write_text("old\n")
    os.chown(old, 0, 0)
    target = tmp_path.parent / "must_stay_root.txt"
    target.write_text("x")
    os.chown(target, 0, 0)
    (tmp_path / "link.csv").symlink_to(target)
    glogs = _logs(tmp_path, {SER: _host()})
    new = _start(glogs, "decodes")
    for p in [old, *new]:
        st = os.stat(p)
        assert (st.st_uid, st.st_gid) == (1000, 1000), p
    assert os.stat(target).st_uid == 0


@need_root
def test_saved_radios_rewrite_stays_with_the_folder_owner(tmp_path):
    os.chown(tmp_path, 1000, 1000)
    mgr = lc.LinkManager.__new__(lc.LinkManager)
    mgr.saved_path, mgr.saved = str(tmp_path / "saved_radios.json"), [("1.2.3.4", 47810)]
    mgr._write_saved()
    assert os.stat(mgr.saved_path).st_uid == 1000


def test_health_row_carries_the_clock_check(tmp_path):
    # a G40 on an external 10 MHz sends its clock dict in get_config: the card clock
    # against the 1 PPS and the 10 MHz verdict land in the two last columns
    host = _host(config={"clock_source": "external",
                         "clock": {"choice": "external", "clock_ppm": -136.711, "ten_mhz": "LOST"}},
                 status=_status(flags=proto.Status.FLAG_POS_VALID))
    glogs = _logs(tmp_path, {SER: host})
    _start(glogs, "health")
    glogs.tick(now=T0)
    row = _rows(tmp_path, "health")[1]
    assert row[-7:-3] == ["external", "0", "-136.711", "LOST"]
    assert gl.header("health", -18000)[-5:-3] == ["clock_vs_pps_ppm", "ten_mhz"]
