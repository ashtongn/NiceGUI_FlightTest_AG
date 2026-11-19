#!/usr/bin/env python3
"""Update file paths and commands in Markdown docs after a move.

    python3 tools/reorg/docpaths.py FILE.md ...

Uses tools/reorg/moves.tsv. For a moved module: `python3 name.py` becomes
`python3 -m dpmg.pkg.name` (or `python3 tools/name.py` for tools), and a bare
`name.py` becomes its new path. Radio scripts, the GPU Dockerfiles and
rxdrain.c get their new folders. dpmg_service.py is left alone (the root entry
still runs it).
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))

EXTRA = {
    "g40_up_r36.sh": "deploy/radio/g40_up_r36.sh",
    "x40_tx_up.sh": "deploy/radio/x40_tx_up.sh",
    "dpmg_up.sh": "deploy/radio/dpmg_up.sh",
    "dpmg_doctor.sh": "deploy/radio/dpmg_doctor.sh",
    "Dockerfile.gpu.r36": "deploy/docker/Dockerfile.gpu.r36",
    "Dockerfile.gpu": "deploy/docker/Dockerfile.gpu",
    "rxdrain.c": "native/rxdrain.c",
}


def load():
    m = {}
    for line in open(os.path.join(HERE, "moves.tsv"), encoding="utf-8"):
        if line.startswith("#") or not line.strip():
            continue
        _s, old, new = line.rstrip("\n").split("\t")
        if old != "dpmg_service.py":
            m[old] = new
    return m


def rewrite(text, moves):
    # commands first: python[3] [-B] name.py becomes the -m form or the tools path
    def cmd(mt):
        py, flags, name = mt.group(1), mt.group(2) or "", mt.group(3)
        new = moves[name]
        if new.startswith("dpmg/"):
            return f"{py} {flags}-m {new[:-3].replace('/', '.')}"
        return f"{py} {flags}{new}"
    names = "|".join(re.escape(k) for k in sorted(moves, key=len, reverse=True))
    text = re.sub(r"\b(python3?) ((?:-[A-Za-z] )*)(?:\./)?(" + names + r")\b", cmd, text)
    # ./script.sh becomes bash deploy/radio/script.sh
    for old, new in EXTRA.items():
        if old.endswith(".sh"):
            text = re.sub(r"(?<![\w/.])\./" + re.escape(old), "bash " + new, text)
    allmap = dict(moves)
    allmap.update(EXTRA)
    names = "|".join(re.escape(k) for k in sorted(allmap, key=len, reverse=True))
    return re.sub(r"(?<![\w/.-])(" + names + r")(?![\w])", lambda mt: allmap[mt.group(1)], text)


def main():
    moves = load()
    for p in sys.argv[1:]:
        src = open(p, encoding="utf-8").read()
        out = rewrite(src, moves)
        if out != src:
            open(p, "w", encoding="utf-8").write(out)
            print(f"updated {p}")


if __name__ == "__main__":
    main()
