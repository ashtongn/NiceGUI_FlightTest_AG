"""
Service transceive plane (dpmg_service): the reply-burst callback and IQ
resampler. Proves the arm gate at the service boundary: disarmed touches no
scheduler and radiates nothing; armed modulates the framed reply, resamples to
the card rate, and keys exactly one burst (set_tx_waveform: start_tx then
stop_tx) with the waveform loaded via allow_live=True. Mock scheduler,
no hardware. Exit 0 = all pass.
"""
from __future__ import annotations

import sys
import os
import types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from dpmg.radio import dpmg_service as S
from dpmg.framing import frame_codec

checks = []


def chk(name, ok, detail=""):
    checks.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")


def mock_session(card_rate=48000.0):
    sess = S.RadioService(serial="G40-SIM", host_type=0, kind="mock",
                          simulate=True, cmd_port=0, discovery_port=0,
                          broadcast="127.0.0.1")
    calls = []
    sess.sched = types.SimpleNamespace(
        # the service loads the reply with allow_live=True (the scheduler
        # refuses a waveform swap while TX is live unless the caller opts in);
        # record the flag so the armed path can prove it opts in explicitly.
        set_tx_waveform=lambda w, allow_live=False:
            calls.append(("wf", len(w), bool(allow_live))),
        start_tx=lambda: calls.append(("start",)),
        stop_tx=lambda: calls.append(("stop",)), tx_mode=None)
    sess.backend = types.SimpleNamespace(
        config=types.SimpleNamespace(sample_rate=card_rate),
        health=types.SimpleNamespace(last_error=""))
    sess._modem_cfg = {"modulation": "FSK", "symbol_rate": 1000,
                       "samples_per_bit": 48, "tone0": 1000, "tone1": 2000,
                       "ampl": 0.5, "spread_factor": 15, "pn_seed": 1,
                       "css_sf": 7, "css_bw": 125000.0, "css_os": 1}
    return sess, calls


def main():
    print("Service transceive plane\n")

    # resampler: length scales with rate, duration preserved, dtype complex64
    x = np.exp(1j * np.linspace(0, 10, 1000)).astype(np.complex64)
    up = S._resample_iq(x, 48000, 192000)
    down = S._resample_iq(x, 192000, 48000)
    chk("resample up 4x length", abs(up.size - 4000) <= 1, f"({up.size})")
    chk("resample down 4x length", abs(down.size - 250) <= 1, f"({down.size})")
    chk("resample keeps complex64", up.dtype == np.complex64)
    chk("resample no-op on equal rates", S._resample_iq(x, 1e6, 1e6).size == 1000)

    reply = frame_codec.encode_text("PONG 1")

    # disarmed: nothing keyed
    sess, calls = mock_session()
    sess._tx_armed = False
    sess._modem_tx_burst(0, reply)
    chk("disarmed: scheduler untouched (no radiation)", calls == [])

    # armed: modulate then resample then one burst
    sess, calls = mock_session()
    sess._tx_armed = True
    sess._modem_tx_burst(0, reply)
    chk("armed: burst sequence wf->start->stop",
        [c[0] for c in calls] == ["wf", "start", "stop"])
    wf = [c[1] for c in calls if c[0] == "wf"]
    chk("armed: non-empty reply waveform", bool(wf) and wf[0] > 0, f"({wf})")
    live = [c[2] for c in calls if c[0] == "wf"]
    chk("armed: waveform load opts in with allow_live=True",
        live == [True], f"({live})")

    # armed but no config: safe no-op
    sess, calls = mock_session()
    sess._tx_armed = True
    sess._modem_cfg = None
    sess._modem_tx_burst(0, reply)
    chk("armed but unconfigured: no-op", calls == [])

    # busy guard: a second burst while one is in flight is dropped
    sess, calls = mock_session()
    sess._tx_armed = True
    sess._tx_busy = True
    sess._modem_tx_burst(0, reply)
    chk("busy guard drops overlapping burst", calls == [])

    print(f"\nresult: {sum(checks)}/{len(checks)} checks passed")
    return 0 if checks and all(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
