"""
Gate + flag-plumbing checks for continuous-signal handling. The full GPU
channelizer needs hardware; this pins the decision logic that decides which
channels decode each frame, and that entry.burst=None: is_continuous.
Exit 0 = pass.
"""
from __future__ import annotations

import sys
import os
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

checks = []


def chk(name, ok, detail=""):
    checks.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")


def test_plan_flag():
    from dpmg.scenario.channel_plan import ChannelPlan, Entry
    from dpmg.scenario import plan_adapter as pa
    plan = ChannelPlan(center_freq_hz=425_000_000, sample_rate_hz=30.72e6,
                       rf_bandwidth_hz=24.58e6)
    cont = Entry(name="cont", modulation="FSK",
                 params={"symbol_rate": 4800, "tone_deltas_hz": [-2400, 2400]},
                 offset_hz=0.0, role="known", burst=None)
    brst = Entry(name="brst", modulation="4FSK",
                 params={"symbol_rate": 4800, "tone_deltas_hz": [-3600, -1200, 1200, 3600]},
                 offset_hz=1e6, role="known", burst={"period_s": 1.0})
    cc_c = pa.entry_to_channelconfig(cont, plan)
    cc_b = pa.entry_to_channelconfig(brst, plan)
    chk("entry.burst=None -> is_continuous True", getattr(cc_c, "is_continuous", None) is True)
    chk("entry.burst set  -> is_continuous False", getattr(cc_b, "is_continuous", None) is False)


def test_gate():
    from dpmg.dsp.channelizer_worker import ChannelizerWorker, CONT_PERIOD
    w = ChannelizerWorker.__new__(ChannelizerWorker)   # no __init__ / no threads
    w._slot_bw = 20e3

    def mk(fc, cont, idx):
        ch = types.SimpleNamespace(f_center_hz=fc, is_continuous=cont, idx=idx)
        return ch

    key = lambda fc: int(round(fc / w._slot_bw))
    fc0, fc1 = 425_020_000.0, 425_040_000.0

    # burst channel: only on the rising edge
    w._active_keys = {key(fc0)}
    w._rising_keys = {key(fc0)}
    w._frame_ctr = 0
    chk("burst channel submits on rising edge", w._targeted_gate(mk(fc0, False, 0)))
    w._rising_keys = set()
    chk("burst channel skipped when not rising", not w._targeted_gate(mk(fc0, False, 0)))

    # continuous channel: active but never rising: must still decode, on cadence
    w._active_keys = {key(fc0)}
    w._rising_keys = set()
    submits = [w._targeted_gate(mk(fc0, True, 0)) for w._frame_ctr in range(6)]
    chk("continuous decodes every CONT_PERIOD frames",
        submits == [(f % CONT_PERIOD == 0) for f in range(6)],
        f"({submits})")

    # continuous but inactive (signal gone): no decode
    w._active_keys = set()
    w._frame_ctr = 0
    chk("continuous skipped when inactive", not w._targeted_gate(mk(fc0, True, 0)))

    # two continuous channels are phase-staggered (not same frame)
    w._active_keys = {key(fc0), key(fc1)}
    clash = 0
    for w._frame_ctr in range(20):
        a = w._targeted_gate(mk(fc0, True, 0))
        b = w._targeted_gate(mk(fc1, True, 1))
        if a and b:
            clash += 1
    chk("two continuous channels never submit same frame (period 2)",
        clash == 0, f"(clashes={clash})")


def main():
    print("continuous-signal gate + flag plumbing\n")
    test_plan_flag()
    test_gate()
    print(f"\nresult: {sum(checks)}/{len(checks)} checks passed")
    return 0 if checks and all(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
