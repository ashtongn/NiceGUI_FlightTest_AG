#!/usr/bin/env python3
"""List the files the radio service loads: every dpmg module reached by import from
dpmg_service.py, lazy imports inside functions included (static read, nothing is run).

    python3 radio_closure.py TREE_ROOT > files.txt
"""
import ast
import os
import sys

STARTS = ("dpmg_service.py", "dpmg/radio/dpmg_service.py")


def modfile(root, mod):
    p = os.path.join(root, *mod.split("."))
    for c in (p + ".py", os.path.join(p, "__init__.py")):
        if os.path.isfile(c):
            return c
    return None


def closure(root):
    seen = set()
    todo = [os.path.join(root, s) for s in STARTS if os.path.isfile(os.path.join(root, s))]
    while todo:
        f = todo.pop()
        if f in seen:
            continue
        seen.add(f)
        try:
            tree = ast.parse(open(f, encoding="utf-8").read())
        except (SyntaxError, UnicodeDecodeError) as e:
            print(f"cannot parse {f}: {e}", file=sys.stderr)
            continue
        pkg = os.path.dirname(os.path.relpath(f, root)).replace(os.sep, ".")
        for n in ast.walk(tree):
            mods = []
            if isinstance(n, ast.Import):
                mods = [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom):
                base = n.module or ""
                if n.level:
                    parts = pkg.split(".") if pkg else []
                    parts = parts[:len(parts) - (n.level - 1)]
                    base = ".".join(parts + ([base] if base else []))
                mods = [base] + [base + "." + a.name for a in n.names]
            for m in mods:
                ps = [x for x in m.split(".") if x]
                for i in range(1, len(ps) + 1):
                    mf = modfile(root, ".".join(ps[:i]))
                    if mf and mf not in seen:
                        todo.append(mf)
    return sorted(os.path.relpath(f, root) for f in seen)


if __name__ == "__main__":
    for rel in closure(sys.argv[1]):
        print(rel)
