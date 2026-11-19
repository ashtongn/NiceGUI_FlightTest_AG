#!/usr/bin/env python3
"""Move one group of root modules into the package and leave a stub at each old path.

    python3 tools/reorg/move.py STEP [--log FILE] [--dry-run]
    python3 tools/reorg/move.py STEP --undo [--log FILE]

The groups are in tools/reorg/moves.tsv (step, old path, new path). A move is a
plain rename plus a stub at the old path that replaces itself in sys.modules
with the moved module, so `import name` and `python3 name.py` keep working and
both names give the same module object. Targets under tools/ get no stub.
dpmg_service.py gets the permanent service entry instead of a stub.
Missing package folders are created with an empty __init__.py.
--log appends "old<TAB>new<TAB>step" rows (or "undo" rows) to FILE.
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
MOVES = os.path.join(HERE, "moves.tsv")

STUB_TAG = "kept so old imports and commands still work"

STUB = '''# Moved to {new}; {tag}.
import importlib as _il, sys as _sys
if __name__ == "__main__":
    import runpy
    runpy.run_module("{dotted}", run_name="__main__", alter_sys=True)
else:
    _sys.modules[__name__] = _il.import_module("{dotted}")
'''

SERVICE_ENTRY = '''"""DPMG radio service entry. The code is in dpmg/radio/dpmg_service.py.

Every radio container runs `python dpmg_service.py ...` from the root of the
mounted tree, so this file stays here. `import dpmg_service` gives the real
module, and `python3 -m dpmg.radio.dpmg_service` works as well.
"""
import importlib
import sys

if __name__ == "__main__":
    from dpmg.radio.dpmg_service import main
    main()
elif __name__ == "dpmg_service":
    sys.modules[__name__] = importlib.import_module("dpmg.radio.dpmg_service")
# __mp_main__ (a spawned worker re-running this file): nothing to do
'''


def load_moves():
    rows = []
    with open(MOVES, encoding="utf-8") as f:
        for line in f:
            if not line.strip() or line.startswith("#"):
                continue
            step, old, new = line.rstrip("\n").split("\t")
            rows.append((step, old, new))
    return rows


def is_stub(path):
    try:
        with open(path, encoding="utf-8") as f:
            head = f.read(4096)
    except OSError:
        return False
    return STUB_TAG in head or head.startswith('"""DPMG radio service entry.')


def stub_text(old, new):
    if old == "dpmg_service.py":
        return SERVICE_ENTRY
    dotted = new[:-3].replace("/", ".")
    return STUB.format(new=new, tag=STUB_TAG, dotted=dotted)


def ensure_package(new, dry):
    d = os.path.dirname(new)
    parts = d.split("/")
    if parts[0] != "dpmg":
        return
    for i in range(1, len(parts) + 1):
        pkg = os.path.join(ROOT, *parts[:i])
        init = os.path.join(pkg, "__init__.py")
        if not os.path.isfile(init):
            print(f"  new package {'/'.join(parts[:i])}/__init__.py")
            if not dry:
                os.makedirs(pkg, exist_ok=True)
                open(init, "w").close()


def do_move(rows, dry, log):
    for _step, old, new in rows:
        src, dst = os.path.join(ROOT, old), os.path.join(ROOT, new)
        if not os.path.isfile(src) or is_stub(src):
            sys.exit(f"{old}: missing or already a stub")
        if os.path.exists(dst):
            sys.exit(f"{new}: already exists")
    for step, old, new in rows:
        src, dst = os.path.join(ROOT, old), os.path.join(ROOT, new)
        ensure_package(new, dry)
        print(f"  {old} to {new}")
        if dry:
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.rename(src, dst)
        if new.startswith("dpmg/"):
            with open(src, "w", encoding="utf-8") as f:
                f.write(stub_text(old, new))
        if log:
            with open(log, "a", encoding="utf-8") as f:
                f.write(f"{old}\t{new}\t{step}\n")


def do_undo(rows, log):
    for step, old, new in rows:
        src, dst = os.path.join(ROOT, old), os.path.join(ROOT, new)
        if not os.path.isfile(dst):
            sys.exit(f"{new}: not there, nothing to undo")
        if os.path.exists(src) and not is_stub(src):
            sys.exit(f"{old}: exists and is not a stub")
    for step, old, new in rows:
        src, dst = os.path.join(ROOT, old), os.path.join(ROOT, new)
        print(f"  {new} back to {old}")
        if os.path.exists(src):
            os.remove(src)
        os.rename(dst, src)
        if log:
            with open(log, "a", encoding="utf-8") as f:
                f.write(f"{new}\t{old}\tundo {step}\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("step")
    ap.add_argument("--undo", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--log", default=os.environ.get("DPMG_REORG_LOG"))
    a = ap.parse_args()
    rows = [r for r in load_moves() if r[0] == a.step]
    if not rows:
        sys.exit(f"no rows for step {a.step} in {MOVES}")
    if a.undo:
        do_undo(rows, a.log)
    else:
        do_move(rows, a.dry_run, a.log)


if __name__ == "__main__":
    main()
