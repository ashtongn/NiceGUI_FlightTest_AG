#!/usr/bin/env python3
"""OFF-11: build every generator (jammer and test signal kinds) and every bundled
scenario, and report the largest IQ magnitude. Pass is 0.999 or less. No radio."""
import glob
import json
import os
import sys
import tempfile

import numpy as np

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))
from dpmg.dsp import tools_engine as te  # noqa: E402

from dpmg.scenario import gen_scenario_json as gsj  # noqa: E402

LIMIT = 0.999
TOL = 2e-7   # the generators clamp to 0.999 in complex64, which rounds to about 0.99900001
FS = 30.72e6
worst = 0.0
fails = 0


def report(name, pk):
    global worst, fails
    worst = max(worst, pk)
    bad = pk > LIMIT + TOL
    fails += bad
    print(f"{'FAIL' if bad else 'PASS'} {name:48s} peak {pk:.8f}")


print(f"generators: {sorted(te.GENERATORS)}")
for kind in sorted(te.GENERATORS):
    for label, kw in [("default", {}), ("ampl 1.0", {"ampl": 1.0}), ("ampl 2.0", {"ampl": 2.0}),
                      ("pulsed", {"prf": 1000.0, "duty": 0.5})]:
        y = te.generate(kind, FS, 0.02, **kw)
        report(f"{kind} {label}", float(np.max(np.abs(y))))

for path in sorted(glob.glob(os.path.join(REPO, "scenarios", "json", "*.json"))):
    spec = json.load(open(path))
    with tempfile.TemporaryDirectory() as out:
        for label, peak in [("default", None), ("peak 1.0 (builder maximum)", 1.0)]:
            try:
                gsj.build_scenario(spec, out, peak)
            except Exception as e:
                fails += 1
                print(f"FAIL {os.path.basename(path)} {label}: build error {e}")
                continue
            for iq in glob.glob(os.path.join(out, "*.iq.bin")):
                d = np.fromfile(iq, dtype=np.complex64)
                report(f"{os.path.basename(path)} {label}", float(np.max(np.abs(d))))
                os.remove(iq)

print(f"worst peak {worst:.4f}, limit {LIMIT}, failures {fails}")
sys.exit(1 if fails else 0)
