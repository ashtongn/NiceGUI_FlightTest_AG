#!/usr/bin/env python3
"""Ticks of the card clock per 1 PPS, from pps_tester --displayts output.

    pps_rate.py FILE [FILE ...]

A card clock locked to the same source as the 1 PPS counts a whole number of ticks each
second. A steady fraction or offset means the two are running free of each other.
Assumes pulses are one second apart, so a missed pulse is taken out by rounding.
"""
import re
import sys

for path in sys.argv[1:]:
    cards = {}
    for line in open(path):
        m = re.search(r"found PPS for card (\d+) at system timestamp (\d+)", line)
        if m:
            cards.setdefault(int(m[1]), []).append(int(m[2]))
    print(path.rsplit("/", 1)[-1])
    for c, t in sorted(cards.items()):
        if len(t) < 2:
            print(f"  card {c}: {len(t)} pulses, no rate")
            continue
        nom = 40e6
        steps = [b - a for a, b in zip(t, t[1:])]
        secs = sum(round(s / nom) for s in steps)
        rate = (t[-1] - t[0]) / secs
        per = [s / round(s / nom) for s in steps]
        print(f"  card {c}: {len(t)} pulses over {secs} s, {rate:,.3f} ticks/s, {(rate - nom) / nom * 1e9:+.1f} ppb, "
              f"second to second {min(per):,.0f} to {max(per):,.0f}")
