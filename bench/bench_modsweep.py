#!/usr/bin/env python3
"""
bench_modsweep.py: over-RF modulation sweep for the two-node loopback bench.

For each modulation: build a framed "PING 1" burst at +200 kHz (card rate),
write the matching channel plan, scp the burst to the radio, load the plan on
the card-0 responder, key the burst loop from the card-1 initiator, and watch
the responder logs for the [XCV] valid-decode line. Prints a verdict table.

Assumes the two-node bench is up: primary service (card 0, RX TRx1A, single)
on :47810 with transceive wired DISARMED, initiator instance (card 1, TX TRx1B,
single) on :47812, cable TRx1B then pad then TRx1A. All TX is into the cable.
"""
from __future__ import annotations

import json
import subprocess
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
from dpmg.modem.modem import Modem, ModemConfig
from dpmg.radio.dpmg_service import _resample_iq

G40 = "sidekiq@192.168.40.101"
from dpmg.deploy_config import RADIO_CODE_DIR
ROOT = RADIO_CODE_DIR                  # the G40 code folder (DPMG_RADIO_CODE_DIR)
HOST = "192.168.40.101"
FS_CARD = 1_920_000.0
SHIFT = 200_000.0
WATCH_S = 50

# (name, ModemConfig kwargs, plan params, plan offset_hz)
CASES = [
    ("FSK", dict(modulation="FSK", symbol_rate=4800, samples_per_bit=40,
                 tone0=1200, tone1=2200),
     dict(symbol_rate=4800.0, tone_spacing_hz=1000.0),
     SHIFT + 1700.0),
    ("4FSK", dict(modulation="4FSK", symbol_rate=4800, samples_per_bit=40,
                  tone0=-1800, tone1=-600, tone2=600, tone3=1800),
     dict(symbol_rate=4800.0, tone_spacing_hz=1200.0, dibit_mapping="binary"),
     SHIFT),
    ("GFSK", dict(modulation="GFSK", symbol_rate=4800, samples_per_bit=40,
                  tone0=1200, tone1=2200, gfsk_bw_hz=2400.0),
     dict(symbol_rate=4800.0, tone_spacing_hz=1000.0, gfsk_bw_hz=2400.0),
     SHIFT + 1700.0),
    ("BPSK", dict(modulation="BPSK", symbol_rate=4800, samples_per_bit=40),
     dict(symbol_rate=4800.0), SHIFT),
    ("QPSK", dict(modulation="QPSK", symbol_rate=4800, samples_per_bit=40),
     dict(symbol_rate=4800.0), SHIFT),
    ("π/4-QPSK", dict(modulation="π/4-QPSK", symbol_rate=4800,
                           samples_per_bit=40),
     dict(symbol_rate=4800.0, plan_modulation="PI4-QPSK"), SHIFT),
    ("DQPSK", dict(modulation="DQPSK", symbol_rate=4800, samples_per_bit=40),
     dict(symbol_rate=4800.0), SHIFT),
    ("OQPSK", dict(modulation="OQPSK", symbol_rate=4800, samples_per_bit=40),
     dict(symbol_rate=4800.0), SHIFT),
    # spread spectrum: rates chosen so the 250k channel baseband gives an
    # integer samples/bit with the decode-pool default params (SF15 / SF7).
    ("DSSS", dict(modulation="DSSS", symbol_rate=2777.7778, samples_per_bit=90,
                  spread_factor=15),
     dict(symbol_rate=2777.7778), SHIFT),
    ("CSS", dict(modulation="CSS", css_sf=7, css_bw=125_000.0, css_os=1),
     dict(symbol_rate=976.5625, css_sf=7, css_bw=125_000.0, css_os=2), SHIFT),
]


def sh(cmd, timeout=60):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                          timeout=timeout).stdout.strip()


def bench(args, timeout=40):
    return sh(f"python bench_transceive.py --host {HOST} {args}", timeout)


def build_case(name, mkw, params, offset):
    cfg = ModemConfig(**mkw)
    m = Modem(cfg)
    burst = m.transmit("PING 1")
    up = _resample_iq(burst, m.sample_rate, FS_CARD)
    n = np.arange(len(up))
    up = (up * np.exp(2j * np.pi * SHIFT / FS_CARD * n)).astype(np.complex64)
    slot = int(FS_CARD * 0.100)
    nslots = max(1, int(np.ceil(len(up) / slot)) + 1)   # burst + dead air
    out = np.zeros(slot * nslots, dtype=np.complex64)
    out[:len(up)] = up
    fn = f"sweep_{name.replace('/', '').replace('π', 'pi')}.bin"
    out.tofile(fn)
    plan = {
        "schema_version": 1,
        "compose": {"tx_radio": "g40", "sample_rate_hz": FS_CARD,
                    "center_freq_hz": 425_025_000.0,
                    "rf_bandwidth_hz": 1_536_000.0},
        "entries": [{
            "name": f"SWEEP-{name}", "modulation": params.get("plan_modulation", mkw["modulation"]),
            "params": {k: v for k, v in params.items() if k != "plan_modulation"}, "offset_hz": offset,
            "power_db": 0.0, "role": "known", "decode": True,
            "frame": {"sync_bits": "1011001000111011", "max_sync_errors": 2,
                      "has_length": True, "crc": "CRC-16-CCITT"},
        }],
    }
    pf = fn.replace(".bin", "_plan.json")
    json.dump(plan, open(pf, "w"), indent=1)
    return fn, pf, len(up)


def main():
    only = sys.argv[1:] or None
    results = []
    for name, mkw, params, offset in CASES:
        if only and name not in only:
            continue
        print(f"\n=== {name} ===")
        try:
            fn, pf, nsamp = build_case(name, mkw, params, offset)
        except Exception as e:
            print(f"  build failed: {e}")
            results.append((name, f"BUILD FAIL: {e}"))
            continue
        print(f"  burst {nsamp} samp ({nsamp/FS_CARD*1e3:.1f} ms) -> {fn}")
        sh(f"scp -o ConnectTimeout=4 {fn} {G40}:{ROOT}/", 40)
        # clean-room per case: the chan worker doesn't survive live re-plans
        sh(f"ssh -o ConnectTimeout=4 {G40} 'docker restart dpmg_svc >/dev/null 2>&1'", 70)
        time.sleep(8)
        bench("init-dual --card 0 --tx-ant TXA1 --rx-ant RXA1 --tx-freq 425025000",
              timeout=110)
        print("  " + bench(f"set-plan --plan-file {pf}").splitlines()[-1])
        bench("start-rx")
        bench("transceive")          # wired, DISARMED
        sh(f"ssh -o ConnectTimeout=4 {G40} 'docker exec -d -w /app_svc dpmg_svc "
           f"python dpmg_service.py --kind g40 --host-type g40 --serial G40-0001-C1 "
           f"--cmd-port 47812 --discovery-port 47811 --broadcast 192.168.40.255'", 30)
        time.sleep(3)
        bench("--port 47812 init-dual --card 1 --tx-ant TXA1 --rx-ant RXA1 "
              "--tx-freq 425025000", timeout=110)
        t_mark = time.strftime("%H:%M:%S")
        bench(f"--port 47812 tx-file --arm --path /app_svc/{fn}")
        print(f"  TX keyed at {t_mark}, watching {WATCH_S}s ...")
        verdict = "no decode"
        t0 = time.time()
        while time.time() - t0 < WATCH_S:
            time.sleep(5)
            log = sh(f"ssh -o ConnectTimeout=4 {G40} "
                     f"'docker logs --since 40s dpmg_svc 2>&1 | grep XCV | tail -3'", 30)
            if "PING 1" in log:
                verdict = "DECODED"
                print(f"  {log.splitlines()[-1]}")
                break
        bench("--port 47812 stop-tx")
        print(f"  -> {verdict}")
        results.append((name, verdict))
        time.sleep(2)

    print("\n===== SWEEP RESULTS =====")
    for name, v in results:
        print(f"  {name:10s} {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
