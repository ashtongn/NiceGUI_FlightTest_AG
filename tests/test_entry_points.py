#!/usr/bin/env python3
"""
test_entry_points.py: the radio service entry works the way every container runs it.

  (a) `python3 dpmg_service.py --help` from the repo root exits 0
  (b) the simulator started with that same command line answers `status`
      (bench/bench_transceive.py, the bench CLI)
  (c) with posix_ipc installed, the optional imports that fall back silently
      are really there: dpmg_service._HAVE_CHAN (channelizer) and
      sdr_core._SKIQ_BRIDGE_AVAILABLE (libsidekiq bridge). A broken import
      there looks like a dev box without the GPU or the radio.

Ports 47932/47933, clear of the live GUI (47811) and the other tests.
Runs under pytest or as a script (exit 0 = all pass).
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CMD_PORT = 47932
DISCO_PORT = 47933
ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")


def test_service_help():
    r = subprocess.run([sys.executable, "-B", "dpmg_service.py", "--help"], cwd=ROOT,
                       env=ENV, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    assert "--kind" in r.stdout and "--simulate" in r.stdout, r.stdout[-1000:]


def test_simulator_answers_status():
    with tempfile.TemporaryDirectory() as work:
        # the service keeps its data/ in the working directory; run it from a
        # scratch folder so the repo's data/ is not touched
        svc = subprocess.Popen(
            [sys.executable, "-B", os.path.join(ROOT, "dpmg_service.py"), "--simulate",
             "--kind", "mock", "--serial", "G40-0001-SIM", "--host-type", "g40",
             "--cmd-port", str(CMD_PORT), "--discovery-port", str(DISCO_PORT),
             "--broadcast", "127.0.0.1"],
            cwd=work, env=ENV, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            out = ""
            for _ in range(30):
                time.sleep(1.0)
                if svc.poll() is not None:
                    break
                r = subprocess.run([sys.executable, "-B", "bench/bench_transceive.py",
                                    "--host", "127.0.0.1", "--port", str(CMD_PORT), "status"],
                                   cwd=ROOT, env=ENV, capture_output=True, text=True, timeout=60)
                out = r.stdout + r.stderr
                if r.returncode == 0 and "G40-0001-SIM" in out:
                    break
            else:
                r = None
            assert svc.poll() is None, f"simulator exited rc={svc.returncode}"
            assert r is not None and r.returncode == 0, out[-2000:]
            assert "G40-0001-SIM" in out, out[-2000:]
        finally:
            svc.terminate()
            try:
                svc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                svc.kill()
                svc.wait()


def test_optional_imports_present():
    try:
        import posix_ipc  # noqa: F401
    except ImportError:
        print("  posix_ipc not installed: skipped")
        return
    code = ("import dpmg_service; "
            "print(dpmg_service._HAVE_CHAN, dpmg_service.sdr._SKIQ_BRIDGE_AVAILABLE)")
    r = subprocess.run([sys.executable, "-B", "-c", code], cwd=ROOT, env=ENV,
                       capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stderr[-2000:]
    last = r.stdout.strip().splitlines()[-1]
    assert last == "True True", f"_HAVE_CHAN, _SKIQ_BRIDGE_AVAILABLE = {last}"


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                fails += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if fails else 0)
