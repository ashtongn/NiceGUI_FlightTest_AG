#!/usr/bin/env python3
"""
wedge_soak.py: long-run RX soak to measure how often the NV100 RX FIFO wedges
(RXGAP timestamp ping-pong) and how the WEDGE_WATCHDOG recovers.

Runs one condition for --minutes on the cross-card loopback (TX card1 TRx1B
through the pad into RX card0 TRx1A), with the dense 30.72 Msps plan loaded so the full
channelizer/decode pipeline is under load:
  --tx off     RX only
  --tx cycle   key the dense 8-emitter waveform --on-s seconds, off --off-s seconds

Afterwards it pulls the dpmg_svc log over ssh and reports:
  - RXGAP counts (forward/backward) and the most common jump sizes
  - wedge episodes and the recovery stage each one needed (watchdog counters)
  - precursor check: how often a forward jump of the wedge's jump size occurred
    in the PRECURSOR_S before an episode, vs. in any window of that length

Nothing radiates without --arm. Needs the waveforms the loopback suite builds
(fsk_loopback_test.py ... dense) in the radio's data/waveforms dir.

  python3 bench/wedge_soak.py --host 192.168.0.22 --arm --tx off   --minutes 60
  python3 bench/wedge_soak.py --host 192.168.0.22 --arm --tx cycle --minutes 60
"""
import argparse, json, os, re, subprocess, sys, time
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fsk_loopback_test import Link, init_loopback, WF_DIR, CENTER, DENSE_RATE

DENSE = "fsk_dense_30msps"
PRECURSOR_S = 30.0
EPISODE_GAP_S = 5.0
RXGAP_RE = re.compile(r"^(\S+) \[RXGAP\] expected ts (\d+), got (\d+) \(([+-]\d+) samples\)")
WEDGE_RE = re.compile(r"^(\S+) .*\[WEDGE\] (.*)$")


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ts(s):
    """docker --timestamps prefix (RFC3339 UTC): epoch seconds."""
    return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(
        tzinfo=timezone.utc).timestamp()


def pull_log(ssh, since, until):
    out = subprocess.run(["ssh", "-o", "BatchMode=yes", ssh, "docker", "logs",
                          "--timestamps", "--since", since, "--until", until, "dpmg_svc"],
                         capture_output=True, text=True, timeout=300)
    return (out.stdout + out.stderr).splitlines()


def analyze(lines, t0, t1):
    gaps, wedges = [], []
    for ln in lines:
        m = RXGAP_RE.match(ln)
        if m:
            gaps.append((ts(m.group(1)), int(m.group(4))))
            continue
        m = WEDGE_RE.match(ln)
        if m:
            wedges.append((ts(m.group(1)), m.group(2)))
    fwd = [g for g in gaps if g[1] > 0]
    back = [g for g in gaps if g[1] < 0]
    hours = max((t1 - t0) / 3600.0, 1e-9)
    rep = {"hours": round(hours, 3), "rxgap_total": len(gaps),
           "rxgap_forward": len(fwd), "rxgap_backward": len(back),
           "top_jump_sizes": Counter(abs(g[1]) for g in gaps).most_common(5)}
    # wedge episodes straight from the data: backward jumps more than
    # EPISODE_GAP_S apart start a new episode (independent of the watchdog)
    starts = []
    for t, _n in back:
        if not starts or t - last_back > EPISODE_GAP_S:
            starts.append(t)
        last_back = t
    cleared = [msg for _t, msg in wedges if msg.startswith("cleared")]
    stages = Counter(re.search(r"stage (\d)", m).group(1) for m in cleared if "stage" in m)
    rep.update({"wedge_episodes": len(starts),
                "episodes_per_hour": round(len(starts) / hours, 2),
                "watchdog_episodes": sum(1 for _t, m in wedges if m.startswith("RX replaying")),
                "recovery_stage_counts": dict(stages),
                "giveups": sum(1 for _t, m in wedges if "power-cycle" in m)})
    # precursor: forward jumps of the wedge's own jump size before each episode
    wsize = Counter(abs(g[1]) for g in back).most_common(1)
    if wsize and starts:
        size = wsize[0][0]
        same = [t for t, n in fwd if n == size]
        hit = sum(1 for s in starts if any(s - PRECURSOR_S <= t < s for t in same))
        # base rate: fraction of PRECURSOR_S windows (sliding, 1 s step) with such a jump
        wins = max(int((t1 - t0) - PRECURSOR_S), 1)
        base = sum(1 for k in range(wins)
                   if any(t0 + k <= t < t0 + k + PRECURSOR_S for t in same)) / wins
        rep["precursor"] = {"wedge_jump_size": size, "same_size_forward_jumps": len(same),
                            "episodes_with_precursor": f"{hit}/{len(starts)}",
                            "base_rate_any_window": round(base, 3)}
    return rep


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", required=True)
    ap.add_argument("--ssh", default=None, help="ssh target for logs (default sidekiq@HOST)")
    ap.add_argument("--port", type=int, default=47810)
    ap.add_argument("--tx", choices=["off", "cycle"], default="off")
    ap.add_argument("--on-s", type=float, default=20.0)
    ap.add_argument("--off-s", type=float, default=10.0)
    ap.add_argument("--minutes", type=float, default=60.0)
    ap.add_argument("--tx-atten", type=float, default=30)
    ap.add_argument("--rx-gain", type=float, default=255)
    ap.add_argument("--arm", action="store_true", help="allow TX to radiate (cycle mode)")
    ap.add_argument("--out", default=None, help="write the JSON report here")
    a = ap.parse_args()
    if a.tx == "cycle" and not a.arm:
        raise SystemExit("--tx cycle radiates; add --arm")
    ssh = a.ssh or f"sidekiq@{a.host}"

    L = Link(a.host, a.port)
    init_loopback(L, DENSE_RATE, CENTER, a.tx_atten, a.rx_gain, tx_card=1, kind="g40")
    L.cmd("set_plan", {"plan_path": f"{WF_DIR}/{DENSE}.plan.json"})
    if a.tx == "cycle":
        L.cmd("set_tx_waveform", {"kind": "file", "path": f"{WF_DIR}/{DENSE}.iq.bin"})
    w0 = L.cmd("wedge_status", {}).result
    since, t0 = utc_now(), time.time()
    end = t0 + a.minutes * 60.0
    print(f"[soak] tx={a.tx}  {a.minutes:g} min  start {since}  watchdog={w0.get('enabled')}",
          flush=True)
    keyed, next_flip, last_poll = False, t0, 0.0
    try:
        while time.time() < end:
            now = time.time()
            if a.tx == "cycle" and now >= next_flip:
                L.cmd("stop_tx" if keyed else "start_tx",
                      {} if keyed else {"mode": "CONTINUOUS"})
                keyed = not keyed
                next_flip = now + (a.on_s if keyed else a.off_s)
            if now - last_poll >= 60.0:
                w = L.cmd("wedge_status", {}, timeout=60, soft=True)
                if w is not None:
                    r = w.result
                    print(f"[soak] {int(now - t0):5d}s  episodes={r['episodes']} "
                          f"stream_restarts={r['stream_restarts']} reinits={r['reinits']} "
                          f"giveups={r['giveups']}", flush=True)
                last_poll = now
            time.sleep(0.5)
    finally:
        if keyed:
            L.cmd("stop_tx", {}, soft=True)
        L.cmd("stop_rx", {}, soft=True)
    until, t1 = utc_now(), time.time()
    w1 = L.cmd("wedge_status", {}).result
    rep = analyze(pull_log(ssh, since, until), t0, t1)
    rep.update({"condition": a.tx, "start": since, "end": until,
                "watchdog_delta": {k: w1[k] - w0.get(k, 0) for k in
                                   ("episodes", "self_cleared", "stream_restarts",
                                    "reinits", "giveups")}})
    print(json.dumps(rep, indent=1), flush=True)
    if a.out:
        with open(a.out, "w") as f:
            json.dump(rep, f, indent=1)


if __name__ == "__main__":
    main()
