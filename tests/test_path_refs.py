#!/usr/bin/env python3
"""
test_path_refs.py: every repo file that a script, launcher or Dockerfile names exists.

Scans *.sh, *.bat, *.ps1 and Dockerfile* (repo root, deploy/, bench/, tools/,
tests/) for references to repo files and checks each one:

  python[3] [flags] X.py        python -m pkg.mod        bash|sh X.sh   ./X.sh
  -f Dockerfile.X               COPY src... dst          docker cp src dst
  bench/...                     the import self-check lists in launch_gui.sh,
                                deploy/push_update.sh and dpmg_doctor.sh

A relative path is accepted when it exists under the repo root or beside the
script (the scripts cd to the repo root, or run from the radio's code folder,
which has the same layout). Paths built from variables are skipped.
Runs under pytest or as a script (exit 0 = all pass).
"""
from __future__ import annotations

import os
import re
import shlex
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

SCAN_DIRS = ("deploy", "bench", "tools", "tests")
SKIP_DIRS = {".git", "__pycache__", "data", "output", "results", "dcfe", "wheels"}

# (script, reference) pairs that are not repo files on purpose.
ALLOWED = {
    ("deploy/g40/guarded_flash.sh", "epiq-flash.sh"),   # in the Epiq BSP folder
}

SKIP_CHARS = set("$%*<>~{}`")
BENCH_RE = re.compile(r"(?<![\w/$.])bench/[\w./-]+\.(?:py|sh|yaml|json)")
SELFCHECK_TUPLE_RE = re.compile(r"for m in \(([^)]*)\)", re.S)
IMPORT_C_RE = re.compile(r"python3? (?:-[A-Za-z] \S+ )*-c ['\"]import ([\w., ]+?)[;'\"]")


def rel(p):
    return os.path.relpath(p, ROOT).replace(os.sep, "/")


def scripts():
    out = []

    def want(f):
        return (f.endswith((".sh", ".bat", ".ps1")) or f.startswith("Dockerfile"))

    out += [os.path.join(ROOT, f) for f in sorted(os.listdir(ROOT))
            if os.path.isfile(os.path.join(ROOT, f)) and want(f)]
    for d in SCAN_DIRS:
        for dp, dn, fn in os.walk(os.path.join(ROOT, d)):
            dn[:] = sorted(x for x in dn if x not in SKIP_DIRS)
            out += [os.path.join(dp, f) for f in sorted(fn) if want(f)]
    return out


def clean(tok):
    tok = tok.strip().strip("\"'").rstrip(";),\\").strip("\"'")
    if not tok or tok.startswith(("-", "/")) or SKIP_CHARS & set(tok):
        return None
    if tok.startswith("./"):
        tok = tok[2:]
    return tok


def tokens(line):
    try:
        return shlex.split(line, comments=False, posix=True)
    except ValueError:
        return line.split()


def refs_in(path):
    """(lineno, kind, ref) for every file reference in one script."""
    out = []
    name = os.path.basename(path)
    is_docker = name.startswith("Dockerfile")
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    for i, line in enumerate(lines, 1):
        text = line.lstrip("#").strip() if line.lstrip().startswith("#") else line
        toks = tokens(text.replace("\\", " "))
        if is_docker and toks and toks[0].upper() == "COPY":
            srcs = [t for t in toks[1:-1] if not t.startswith("--")]
            for s in srcs:
                c = clean(s)
                if c and c != ".":
                    out.append((i, "COPY", c))
        for j, t in enumerate(toks):
            base = os.path.basename(t.strip("\"'"))
            if re.fullmatch(r"python[0-9.]*(\.exe)?", base):
                k = j + 1
                while k < len(toks) and toks[k].startswith("-") and toks[k] not in ("-m", "-c"):
                    k += 1
                if k < len(toks) and toks[k] == "-m" and k + 1 < len(toks):
                    out.append((i, "-m", toks[k + 1]))
                elif k < len(toks) and toks[k].endswith(".py"):
                    c = clean(toks[k])
                    if c:
                        out.append((i, "python", c))
            elif base in ("bash", "sh") and j + 1 < len(toks) and toks[j + 1].endswith(".sh"):
                c = clean(toks[j + 1])
                if c:
                    out.append((i, "bash", c))
            elif t.startswith("./") and t.endswith(".sh"):
                c = clean(t)
                if c:
                    out.append((i, "run", c))
            elif t == "-f" and j + 1 < len(toks) and "Dockerfile" in toks[j + 1]:
                c = clean(toks[j + 1])
                if c:
                    out.append((i, "-f", c))
            elif t == "cp" and j > 0 and toks[j - 1] == "docker" and j + 1 < len(toks):
                c = clean(toks[j + 1])
                if c and ":" not in c:
                    out.append((i, "docker cp", c))
        for m in BENCH_RE.finditer(text):
            out.append((i, "bench", m.group(0)))
    src = "\n".join(lines)
    for m in SELFCHECK_TUPLE_RE.finditer(src):
        for mod in re.findall(r"[\"']([\w.]+)[\"']", m.group(1)):
            out.append((src[:m.start()].count("\n") + 1, "self-check", mod))
    joined = re.sub(r"\\\n\s*", " ", src)
    for m in IMPORT_C_RE.finditer(joined):
        for mod in m.group(1).split(","):
            mod = mod.strip().split(" as ")[0].strip()
            if mod:
                out.append((joined[:m.start()].count("\n") + 1, "import -c", mod))
    return out


def module_exists(dotted):
    p = os.path.join(ROOT, *dotted.split("."))
    return os.path.isfile(p + ".py") or os.path.isfile(os.path.join(p, "__init__.py"))


def first_party_top(dotted):
    top = dotted.split(".")[0]
    return os.path.isfile(os.path.join(ROOT, top + ".py")) or os.path.isdir(os.path.join(ROOT, top))


def check():
    missing = []
    n = 0
    for path in scripts():
        sdir = os.path.dirname(path)
        for lineno, kind, ref in refs_in(path):
            if (rel(path), ref) in ALLOWED:
                continue
            if kind in ("-m", "self-check", "import -c"):
                if kind != "self-check" and not first_party_top(ref):
                    continue
                n += 1
                if not module_exists(ref):
                    missing.append(f"{rel(path)}:{lineno}: {kind} {ref}")
                continue
            n += 1
            if not (os.path.exists(os.path.join(ROOT, ref)) or os.path.exists(os.path.join(sdir, ref))):
                missing.append(f"{rel(path)}:{lineno}: {kind} {ref}")
    return missing, n


def test_script_file_references_exist():
    missing, n = check()
    print(f"  {n} references checked")
    assert not missing, "references to files that do not exist:\n  " + "\n  ".join(missing)


def test_selfcheck_lists_found():
    """The launch_gui.sh import self-check list is parsed (guards the parser)."""
    kinds = [k for _l, k, _r in refs_in(os.path.join(ROOT, "launch_gui.sh"))]
    assert kinds.count("self-check") >= 3, kinds


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                fails += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if fails else 0)
