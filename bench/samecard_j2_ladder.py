#!/usr/bin/env python3
"""
x86 driver for the same-card J2 start-order ladder (Block 3 F4)
===============================================================
Runs bench/samecard_j2_probe.py inside the G40 dpmg_svc container from the
staging folder /app_svc/staging_j2pin (host <radio code folder>/
staging_j2pin), one key per `run`, and pulls each JSON back to
output/bench/port_matrix/samecard_j2/.

The probe imports sdr_core / skiq_bridge from the staging folder and only falls
back to the live /app_svc tree for modules staging does not carry (tools_engine,
rf_frontend). It never imports energy_detector or dpmg_service, so a service
deploy into /app_svc does not change what the probe runs; `stage` refreshes the
staged files (an older staged sdr_core had J2_TX_PIN on by default).

  python3 bench/samecard_j2_ladder.py stage          # copy + hash-check the 3 files
  python3 bench/samecard_j2_ladder.py uninit         # stop_tx/stop_rx/close + STATUS
  python3 bench/samecard_j2_ladder.py run tx_first connected --arm
  python3 bench/samecard_j2_ladder.py summary        # table of every JSON pulled
Every `run` first makes the service UNINIT (the probe claims the card itself).
"""
from __future__ import annotations
import argparse, hashlib, json, os, subprocess, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
# results go outside the source tree (output/ is not tracked); DPMG_BENCH_RESULTS overrides
RESULTS_DIR = os.environ.get("DPMG_BENCH_RESULTS") or os.path.join(os.path.dirname(HERE), "output", "bench")
os.makedirs(RESULTS_DIR, exist_ok=True)
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

HOST = os.environ.get("DPMG_G40_HOST", "")
if not HOST:
    sys.exit("Set DPMG_G40_HOST to the radio address first.")
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", f"sidekiq@{HOST}"]
from dpmg.deploy_config import RADIO_CODE_DIR  # noqa: E402
HOST_STAGE = RADIO_CODE_DIR + "/staging_j2pin"
CT_STAGE = "/app_svc/staging_j2pin"
# staged name: source in this tree. The two modules are staged flat at the
# staging root, so the probe's `import sdr_core` finds them before the live tree.
FILES = {"sdr_core.py": "dpmg/radio/sdr_core.py",
         "skiq_bridge.py": "dpmg/radio/skiq_bridge.py",
         "bench/samecard_j2_probe.py": "bench/samecard_j2_probe.py"}
OUT_DIR = os.path.join(RESULTS_DIR, "port_matrix", "samecard_j2")


def sh(cmd, timeout=120, check=True):
    print("$", " ".join(cmd), flush=True)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.stdout:
        print(r.stdout.rstrip(), flush=True)
    if r.stderr:
        print(r.stderr.rstrip(), file=sys.stderr, flush=True)
    if check and r.returncode != 0:
        raise SystemExit(f"command failed rc={r.returncode}")
    return r


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def stage(_a):
    sh(SSH + [f"mkdir -p {HOST_STAGE}/bench"])
    for f in FILES:
        sh(["scp", "-q", os.path.join(ROOT, FILES[f]), f"sidekiq@{HOST}:{HOST_STAGE}/{f}"])
    r = sh(SSH + [f"cd {HOST_STAGE} && sha256sum {' '.join(FILES)}"])
    remote = {ln.split()[1]: ln.split()[0] for ln in r.stdout.splitlines() if ln.strip()}
    bad = [f for f in FILES if remote.get(f) != sha(os.path.join(ROOT, FILES[f]))]
    for f in FILES:
        print(f"  {f}: {sha(os.path.join(ROOT, FILES[f]))[:8]} {'OK' if f not in bad else 'MISMATCH'}")
    if bad:
        raise SystemExit(f"staged hash mismatch: {bad}")


def uninit(_a=None):
    from fsk_loopback_test import Link
    import checkout_common as cc
    link = Link(HOST, 47810)
    cc.teardown(link)
    time.sleep(1.0)
    st = cc.status_snapshot(link, 3.0) or {}
    rs = st.get("radio_state")
    print(f"[uninit] radio_state={rs} flags={st.get('flags')} fps={st.get('fps')}", flush=True)
    if rs not in (0, None):
        raise SystemExit("service radio not UNINIT -- not running the probe")
    return rs


def run(a):
    uninit()
    name = f"m{9 if a.card == 1 else 3}_{a.variant}_{a.cable}.json"
    cmd = (f"docker exec -w {CT_STAGE} dpmg_svc python3 bench/samecard_j2_probe.py "
           f"--card {a.card} --variant {a.variant} --out {name}"
           + (" --arm" if a.arm else "") + (f" --hold {a.hold}" if a.hold else ""))
    sh(SSH + [cmd], timeout=180, check=False)
    os.makedirs(OUT_DIR, exist_ok=True)
    dst = os.path.join(OUT_DIR, name)
    sh(["scp", "-q", f"sidekiq@{HOST}:{HOST_STAGE}/{name}", dst])
    res = json.load(open(dst))
    res["cable"] = a.cable
    json.dump(res, open(dst, "w"), indent=1)
    print(f"[run] {name}: {res.get('verdict')}  early {(res.get('early') or {}).get('tone_snr_db')}  "
          f"late {(res.get('late') or {}).get('tone_snr_db')}  off {(res.get('tx_off') or {}).get('tone_snr_db')}  "
          f"(coupling {res.get('coupling_db')})  modules {res.get('modules')}", flush=True)
    uninit()


def summary(_a):
    rows = []
    for fn in sorted(os.listdir(OUT_DIR)) if os.path.isdir(OUT_DIR) else []:
        if fn.endswith(".json"):
            r = json.load(open(os.path.join(OUT_DIR, fn)))
            rows.append((fn, r.get("variant"), r.get("cable"), (r.get("early") or {}).get("tone_snr_db"),
                         (r.get("late") or {}).get("tone_snr_db"), (r.get("tx_off") or {}).get("tone_snr_db"),
                         r.get("verdict")))
    print("| file | variant | cable | early dB | late dB | TX off dB | verdict |\n|---|---|---|---|---|---|---|")
    for row in rows:
        print("| " + " | ".join(str(x) for x in row) + " |")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("stage").set_defaults(fn=stage)
    sp.add_parser("uninit").set_defaults(fn=uninit)
    sp.add_parser("summary").set_defaults(fn=summary)
    r = sp.add_parser("run")
    r.add_argument("variant", choices=("baseline", "tx_first", "rx_after_tx", "rx_cycle"))
    r.add_argument("cable", choices=("connected", "open"), help="RX jack state (record only)")
    r.add_argument("--arm", action="store_true")
    r.add_argument("--card", type=int, default=1)
    r.add_argument("--hold", default=None)
    r.set_defaults(fn=run)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
