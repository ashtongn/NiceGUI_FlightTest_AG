#!/usr/bin/env python3
"""Rewrite plain imports of moved root modules to package imports.

    python3 tools/reorg/codemod.py --package dsp [--dry-run]
    python3 tools/reorg/codemod.py --names proto,link_client [--dry-run]
    python3 tools/reorg/codemod.py --all [--dry-run]

The old-name to new-module map comes from tools/reorg/moves.tsv (rows whose new
path is under dpmg/). Every import statement is found with the AST, including
ones inside functions and try blocks, and only that statement's text is
replaced, so comments, layout and line numbers stay as they were. Old form,
then new form:

    import sdr_core as sdr       from dpmg.radio import sdr_core as sdr
    import proto                 from dpmg.link import proto
    import secure_frame as sf    from dpmg.secure import api as sf
    from frame_codec import X    from dpmg.framing.frame_codec import X
    import numpy, proto          import numpy; from dpmg.link import proto

dcfe/ and dpmg/gpu/ (copied flat into the GPU image) are never edited.
"""
from __future__ import annotations

import argparse
import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
MOVES = os.path.join(HERE, "moves.tsv")
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "data", "output", "dcfe",
             "scenarios", "mission_plans", "docs", "results"}
SKIP_PREFIXES = ("dpmg/gpu/",)
SCAN_DIRS = ("dpmg", "tests", "bench", "tools")


def load_map():
    m = {}
    with open(MOVES, encoding="utf-8") as f:
        for line in f:
            if not line.strip() or line.startswith("#"):
                continue
            _step, old, new = line.rstrip("\n").split("\t")
            if new.startswith("dpmg/") and old.endswith(".py") and "/" not in old:
                m[old[:-3]] = new[:-3].replace("/", ".")
    return m


def py_files():
    out = [os.path.join(ROOT, f) for f in sorted(os.listdir(ROOT)) if f.endswith(".py")]
    for d in SCAN_DIRS:
        for dp, dn, fn in os.walk(os.path.join(ROOT, d)):
            dn[:] = sorted(x for x in dn if x not in SKIP_DIRS)
            out += [os.path.join(dp, f) for f in sorted(fn) if f.endswith(".py")]
    rel = lambda p: os.path.relpath(p, ROOT).replace(os.sep, "/")
    return [p for p in out if not rel(p).startswith(SKIP_PREFIXES)]


def line_offsets(src):
    offs, pos = [0], 0
    for line in src.splitlines(keepends=True):
        pos += len(line)
        offs.append(pos)
    return offs


def from_form(new, alias, old):
    pkg, _, base = new.rpartition(".")
    name = alias or (None if base == old else old)
    return f"from {pkg} import {base}" + (f" as {name}" if name else "")


def rewrite_import(node, mapping):
    parts, changed = [], False
    plain = []
    for a in node.names:
        top = a.name.split(".")[0]
        if top in mapping and "." not in a.name:
            if plain:
                parts.append("import " + ", ".join(plain))
                plain = []
            parts.append(from_form(mapping[top], a.asname, a.name))
            changed = True
        else:
            plain.append(a.name + (f" as {a.asname}" if a.asname else ""))
    if plain:
        parts.append("import " + ", ".join(plain))
    return ("; ".join(parts) if changed else None)


def edit_file(path, mapping, dry):
    with open(path, encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src, path)
    offs = line_offsets(src)
    lines = src.splitlines(keepends=True)

    def pos(ln, col):
        # ast columns are UTF-8 byte offsets
        return offs[ln - 1] + len(lines[ln - 1].encode("utf-8")[:col].decode("utf-8"))

    edits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            new = rewrite_import(node, mapping)
            if new is None:
                continue
            a, b = pos(node.lineno, node.col_offset), pos(node.end_lineno, node.end_col_offset)
            edits.append((a, b, new, node.lineno))
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            top = node.module.split(".")[0]
            if top not in mapping or "." in node.module:
                continue
            a, b = pos(node.lineno, node.col_offset), pos(node.end_lineno, node.end_col_offset)
            text = src[a:b]
            i = text.find(node.module)
            if not text.startswith("from") or i < 0:
                raise SystemExit(f"{path}:{node.lineno}: cannot place {node.module!r}")
            new_text = text[:i] + mapping[top] + text[i + len(node.module):]
            edits.append((a, b, new_text, node.lineno))
    if not edits:
        return 0
    edits.sort(reverse=True)
    out = src
    for a, b, new, ln in edits:
        rel = os.path.relpath(path, ROOT)
        print(f"  {rel}:{ln}: {out[a:b].strip()[:70]}  became  {new.strip()[:80]}")
        out = out[:a] + new + out[b:]
    ast.parse(out, path)
    if not dry:
        with open(path, "w", encoding="utf-8") as f:
            f.write(out)
    return len(edits)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--package", help="dpmg subpackage (or 'dpmg' for top-level modules)")
    g.add_argument("--names", help="comma-separated old module names")
    g.add_argument("--all", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    full = load_map()
    if a.all:
        mapping = full
    elif a.names:
        names = [n.strip() for n in a.names.split(",") if n.strip()]
        missing = [n for n in names if n not in full]
        if missing:
            sys.exit(f"not in moves.tsv: {missing}")
        mapping = {n: full[n] for n in names}
    else:
        want = "dpmg" if a.package == "dpmg" else "dpmg." + a.package
        mapping = {k: v for k, v in full.items() if v.rpartition(".")[0] == want}
    if not mapping:
        sys.exit("nothing to rewrite")
    total = sum(edit_file(p, mapping, a.dry_run) for p in py_files())
    print(f"{total} import statements {'would be ' if a.dry_run else ''}rewritten "
          f"for {', '.join(sorted(mapping))}")


if __name__ == "__main__":
    main()
