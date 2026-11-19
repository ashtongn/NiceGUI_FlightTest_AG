"""Payload checkout sweep.

Builds a row of back-to-back CW tone steps that step the radio's centre across
a band, for exercising the payload RF chain and reading output power per
frequency. The tones are the transmit stimulus. Record the receive side from
the SDR tab alongside; it is not added as plan steps, because a record on the
same radio at the same time as a tone would double-book the radio in the plan.

The band normally comes from a payload profile (payload_budget.band_limits),
so the sweep runs across what the front end actually passes. Nothing here keys
a radio; it only writes plan steps that the mission planner runs.

The gap between tones is where the radio retunes and builds the next tone. The
runner does that work after the tone before has stopped, so a gap shorter than
the work makes the next tone key late and run short, and a tone whose whole
slot has passed is skipped. MIN_GAP_S is the shortest gap that leaves room for
it at any sample rate.
"""
from __future__ import annotations

import uuid
from typing import List


MIN_GAP_S = 5.0        # retune plus a tone build takes 1 to 4 s on a G40
DEFAULT_DWELL_S = 10.0


def gap_note(gap_s: float) -> str:
    """Text for the dialog when the gap is too short to retune and build in, else ""."""
    if float(gap_s) >= MIN_GAP_S:
        return ""
    return (f"gap under {MIN_GAP_S:g} s: the radio retunes and builds the next tone in the gap, "
            f"so tones key late or are skipped")


def sweep_points(lo_hz: float, hi_hz: float, n: int) -> List[float]:
    """n centre frequencies from lo to hi inclusive. One point sits in the
    middle; zero points is empty. lo and hi are swapped if handed over."""
    lo, hi = float(lo_hz), float(hi_hz)
    if lo > hi:
        lo, hi = hi, lo
    if n <= 0:
        return []
    if n == 1:
        return [(lo + hi) / 2.0]
    return [lo + (hi - lo) * i / (n - 1) for i in range(n)]


def sweep_steps(radio_id: str, lo_hz: float, hi_hz: float, n_points: int,
                dwell_s: float, *, gap_s: float = MIN_GAP_S, start_s: float = 0.0,
                tone_offset_hz: float = 100e3, tx_atten_db: float = 40.0,
                ampl: float = 0.5, seconds: float = 0.1,
                id_prefix: str = "sw", tx_port: str = None,
                rx_none: bool = False) -> List[dict]:
    """Tone steps stepping the centre across [lo, hi], one every dwell_s plus a
    gap. Each step keys a CW tone at its centre frequency on radio_id. Returns a
    list of plan step dicts in time order; the caller appends them to the
    mission. Levels default to the safe bench loopback (40 dB TX attenuation,
    half of full scale).

    tx_port names the TX jack on every step, so the plan does not depend on
    the jack the radio happens to be on; rx_none adds RX none with it, for a
    radio that may transmit with no receive jack. The centre is set once per
    step: a blank TX frequency keeps TX on the step's centre, where setting it
    as well would retune the radio twice."""
    steps: List[dict] = []
    t = float(start_s)
    for i, f in enumerate(sweep_points(lo_hz, hi_hz, n_points)):
        port_block = {}
        if tx_port:
            port_block = {"ports": ({"rx": "none", "tx": str(tx_port)} if rx_none
                                    else {"tx": str(tx_port)})}
        steps.append({
            "id": f"{id_prefix}_{i:03d}_{uuid.uuid4().hex[:6]}",
            "radio": radio_id,
            "technique": "tools.tone",
            "radiates": True,
            "params": {
                "center_hz": round(f, 1),
                "tx_center_hz": None,
                "tx_atten_db": float(tx_atten_db),
                "ampl": float(ampl),
                "freq": float(tone_offset_hz),
                "seconds": float(seconds),
            },
            "schedule": {"start_s": round(t, 1),
                         "stop_s": round(t + float(dwell_s), 1)},
            **port_block,
        })
        t += float(dwell_s) + float(gap_s)
    return steps


def sweep_span_s(n_points: int, dwell_s: float, gap_s: float = MIN_GAP_S) -> float:
    """How long the whole sweep runs, for a quick 'this is N minutes' readout."""
    n = max(int(n_points), 0)
    if n == 0:
        return 0.0
    return n * float(dwell_s) + (n - 1) * float(gap_s)
