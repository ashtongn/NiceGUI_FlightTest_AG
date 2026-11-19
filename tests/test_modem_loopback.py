"""
End-to-end modem loopback: a full preset conversation runs through
frame then modulate then (channel) then demodulate then de-frame then respond, for FSK,
DSSS, and CSS/LoRa. Every message must round-trip; the noisy pass proves the
spread waveforms hold their link below the noise floor. Deterministic.
Exit 0 = all pass.
"""
from __future__ import annotations

import sys
import os
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dpmg.modem.modem import Modem, ModemConfig, ack_responder, run_link, PRESET_SCRIPTS

rng = np.random.default_rng(0xC0FFEE)
checks = []


def chk(name, ok, detail=""):
    checks.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")


def awgn(snr_db):
    def ch(iq):
        x = np.asarray(iq, dtype=np.complex64)
        p = np.mean(np.abs(x) ** 2) + 1e-12
        npow = p / (10 ** (snr_db / 10.0))
        n = (rng.standard_normal(len(x)) + 1j * rng.standard_normal(len(x)))
        return (x + (n * np.sqrt(npow / 2.0)).astype(np.complex64))
    return ch


def report(hops):
    for h in hops:
        arrow = "->" if h.kind == "tx" else "<-"
        flag = "" if h.ok else "   <-- MISMATCH"
        print(f"       {h.node} {arrow} {h.kind:2s}  {h.text!r}{flag}")


def run_case(name, cfg, script, channel=None):
    a = Modem(cfg)
    b = Modem(cfg)
    hops, ok = run_link(a, b, ack_responder(), script, channel=channel)
    chk(name, ok)
    if not ok:
        report(hops)
    return ok


def main():
    print("Modem loopback -- preset communication stream\n")
    script = PRESET_SCRIPTS["linkcheck"]

    print(" FSK (start here):")
    run_case("FSK noiseless full script", ModemConfig(
        modulation="FSK", symbol_rate=4800, samples_per_bit=40,
        tone0=1200, tone1=2200, rx_port="TRx2A", tx_port="TRx1A"), script)
    run_case("FSK @ 18 dB SNR (uncoded frame)", ModemConfig(
        modulation="FSK", symbol_rate=1000, samples_per_bit=48,
        tone0=1000, tone1=2000), script, channel=awgn(18))

    print(" 4FSK:")
    run_case("4FSK noiseless full script", ModemConfig(
        modulation="4FSK", symbol_rate=4800, samples_per_bit=40,
        tone0=600, tone1=1800, tone2=-600, tone3=-1800), script)

    print(" DSSS (custom message, spread):")
    run_case("DSSS SF=31 noiseless", ModemConfig(
        modulation="DSSS", symbol_rate=2400, samples_per_bit=31 * 4,
        spread_factor=31, rx_port="TRx1A", tx_port="TRx2B"), script)
    run_case("DSSS SF=31 @ -3 dB SNR (spread gain)", ModemConfig(
        modulation="DSSS", symbol_rate=2400, samples_per_bit=31 * 4,
        spread_factor=31), script, channel=awgn(-3))

    print(" CSS/LoRa (custom message, chirp):")
    run_case("CSS/LoRa SF7 BW125k noiseless", ModemConfig(
        modulation="CSS", css_sf=7, css_bw=125_000, css_os=1,
        rx_port="TRx1A", tx_port="TRx2A"), script)
    run_case("CSS/LoRa SF9 @ 0 dB SNR", ModemConfig(
        modulation="CSS", css_sf=9, css_bw=125_000, css_os=1),
        PRESET_SCRIPTS["short"], channel=awgn(0))

    print(f"\nresult: {sum(checks)}/{len(checks)} checks passed")
    return 0 if checks and all(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
