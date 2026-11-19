#!/usr/bin/env python3
"""
test_import_graph.py: static and runtime checks on how the code imports itself.

  (a) every first-party import resolves, including imports inside functions and
      try blocks (static: files on disk plus the names each module defines)
  (b) no import cycles between product modules at module level
  (c) no product module puts its own folder on sys.path
  (d) after importing the service and the GUI, no file is loaded as two
      different module objects (a module loaded twice under two names)

Product modules are the .py files at the repo root and everything under dpmg/.
Tests, bench and tools are checked for (a) only. dcfe/ is never scanned.
Runs under pytest or as a script (exit 0 = all pass).
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "data", "output", "dcfe",
             "scenarios", "mission_plans", "docs", "node_modules", "results"}
SCAN_DIRS = ("dpmg", "tests", "bench", "tools")

# dpmg/gpu is copied flat into the GPU image (/app), so its two files import
# each other by plain name from their own folder.
OWN_DIR_IMPORTS = ("dpmg/gpu/",)

# a forwarding file: replaces itself in sys.modules with the real module
STUB_RE = re.compile(r"sys\.modules\[__name__\]\s*=\s*\w+\.import_module\(\s*[\"']([\w.]+)[\"']\s*\)")


def rel(p):
    return os.path.relpath(p, ROOT).replace(os.sep, "/")


def py_files():
    out = [os.path.join(ROOT, f) for f in sorted(os.listdir(ROOT)) if f.endswith(".py")]
    for d in SCAN_DIRS:
        base = os.path.join(ROOT, d)
        for dp, dn, fn in os.walk(base):
            dn[:] = sorted(x for x in dn if x not in SKIP_DIRS)
            out += [os.path.join(dp, f) for f in sorted(fn) if f.endswith(".py")]
    return out


def is_product(path):
    r = rel(path)
    return "/" not in r or r.startswith("dpmg/")


_parsed = {}


def parse(path):
    if path not in _parsed:
        with open(path, encoding="utf-8") as f:
            src = f.read()
        _parsed[path] = (src, ast.parse(src, path))
    return _parsed[path]


def stub_target(path):
    """Dotted target of a forwarding file (move stub or service entry), else None."""
    if not path.endswith(".py") or not os.path.isfile(path):
        return None
    src = parse(path)[0]
    m = STUB_RE.search(src)
    return m.group(1) if m else None


def search_dirs(path):
    r = rel(path)
    d = os.path.dirname(path)
    if r.startswith("dpmg/"):
        dirs = [ROOT]
        if r.startswith(OWN_DIR_IMPORTS):
            dirs.insert(0, d)
        return dirs
    return [d, ROOT] + [os.path.join(ROOT, x) for x in ("bench", "tools", "tests")]


def find_module(dotted, dirs):
    """File (module .py or package dir) for a dotted name, or None."""
    parts = dotted.split(".")
    for base in dirs:
        cur = base
        hit = None
        for i, part in enumerate(parts):
            pkg = os.path.join(cur, part)
            mod = pkg + ".py"
            if os.path.isdir(pkg) and not os.path.isfile(mod):
                cur = pkg
                hit = pkg
            elif os.path.isfile(mod) and i == len(parts) - 1:
                hit = mod
            else:
                hit = None
                break
        if hit:
            return hit
    return None


_known = None


def known_module_names():
    """Base names of every first-party module and of every module that has been
    moved (tools/reorg/moves.tsv), so a plain import of a name that no longer
    resolves is reported instead of being taken for a third-party package."""
    global _known
    if _known is None:
        _known = {os.path.basename(p)[:-3] for p in py_files()} - {"__init__"}
        moves = os.path.join(ROOT, "tools", "reorg", "moves.tsv")
        if os.path.isfile(moves):
            for line in open(moves, encoding="utf-8"):
                f = line.rstrip("\n").split("\t")
                if len(f) >= 2 and f[1].endswith(".py") and not line.startswith("#"):
                    _known.add(os.path.basename(f[1])[:-3])
        _known.add("dpmg")
    return _known


def top_is_first_party(top, dirs):
    if any(os.path.isfile(os.path.join(d, top + ".py")) or
           os.path.isdir(os.path.join(d, top)) for d in dirs):
        return True
    return top in known_module_names()


def follow(target):
    """Resolve a module file through move stubs to the real file."""
    seen = set()
    while target and target.endswith(".py") and target not in seen:
        seen.add(target)
        t = stub_target(target)
        if not t:
            break
        nxt = find_module(t, [ROOT])
        if not nxt:
            return None
        target = nxt
    return target


def module_names(path, depth=0):
    """Names a module binds at top level, or None for 'anything goes'."""
    if os.path.isdir(path):
        init = os.path.join(path, "__init__.py")
        if not os.path.isfile(init):
            return set()
        path = init
    src, tree = parse(path)
    names = set()
    if "globals()" in src:
        return None

    def bind_target(t):
        if isinstance(t, ast.Name):
            names.add(t.id)
        elif isinstance(t, (ast.Tuple, ast.List)):
            for e in t.elts:
                bind_target(e)
        elif isinstance(t, ast.Starred):
            bind_target(t.value)

    def walk(body):
        for n in body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(n.name)
                if n.name == "__getattr__":
                    raise LookupError
            elif isinstance(n, ast.Assign):
                for t in n.targets:
                    bind_target(t)
            elif isinstance(n, (ast.AnnAssign, ast.AugAssign)):
                bind_target(n.target)
            elif isinstance(n, ast.Import):
                for a in n.names:
                    names.add(a.asname or a.name.split(".")[0])
            elif isinstance(n, ast.ImportFrom):
                for a in n.names:
                    if a.name == "*":
                        sub = None
                        if n.level == 0 and n.module:
                            sub = find_module(n.module, search_dirs(path))
                        if sub is None or depth > 4:
                            raise LookupError
                        more = module_names(follow(sub) or sub, depth + 1)
                        if more is None:
                            raise LookupError
                        names.update(more)
                    else:
                        names.add(a.asname or a.name)
            elif isinstance(n, (ast.For, ast.AsyncFor)):
                bind_target(n.target)
                walk(n.body)
                walk(n.orelse)
            elif isinstance(n, (ast.If, ast.While)):
                walk(n.body)
                walk(n.orelse)
            elif isinstance(n, (ast.With, ast.AsyncWith)):
                for it in n.items:
                    if it.optional_vars is not None:
                        bind_target(it.optional_vars)
                walk(n.body)
            elif isinstance(n, ast.Try) or type(n).__name__ == "TryStar":
                walk(n.body)
                for h in n.handlers:
                    if h.name:
                        names.add(h.name)
                    walk(h.body)
                walk(n.orelse)
                walk(n.finalbody)
            elif isinstance(n, ast.Expr) and isinstance(n.value, ast.NamedExpr):
                bind_target(n.value.target)

    try:
        walk(tree.body)
    except LookupError:
        return None
    for n in ast.walk(tree):
        if isinstance(n, ast.Global):
            names.update(n.names)
    return names


def package_of(path):
    r = rel(path)
    parts = r[:-3].split("/")
    if parts[-1] == "__init__":
        return parts[:-1]
    return parts[:-1]


def iter_imports(tree, module_level_only=False):
    """(node, in_function) for every import statement."""
    out = []

    def visit(node, in_fn):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                out.append((child, in_fn))
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                if not module_level_only:
                    visit(child, True)
            else:
                visit(child, in_fn)

    visit(tree, False)
    return out


def resolve_import(path, node):
    """List of (dotted, file_or_None, first_party, names) for one statement."""
    dirs = search_dirs(path)
    res = []
    if isinstance(node, ast.Import):
        for a in node.names:
            top = a.name.split(".")[0]
            fp = top_is_first_party(top, dirs)
            res.append((a.name, find_module(a.name, dirs) if fp else None, fp, None))
        return res
    if node.level:
        pkg = package_of(path)
        if node.level > 1:
            pkg = pkg[:len(pkg) - (node.level - 1)]
        dotted = ".".join(pkg + ([node.module] if node.module else []))
        dirs = [ROOT]
        fp = True
    else:
        dotted = node.module
        fp = top_is_first_party(dotted.split(".")[0], dirs)
    target = find_module(dotted, dirs) if fp else None
    res.append((dotted, target, fp, [a.name for a in node.names]))
    return res


def check_resolution():
    problems = []
    n_fp = 0
    for path in py_files():
        _src, tree = parse(path)
        for node, _in_fn in iter_imports(tree):
            for dotted, target, fp, names in resolve_import(path, node):
                if not fp:
                    continue
                n_fp += 1
                where = f"{rel(path)}:{node.lineno}"
                if target is None:
                    problems.append(f"{where}: {dotted} not found")
                    continue
                real = follow(target)
                if real is None:
                    problems.append(f"{where}: {dotted} is a stub to a missing module")
                    continue
                if not names or names == ["*"]:
                    continue
                pkg_dir = real if os.path.isdir(real) else None
                defined = module_names(real)
                for nm in names:
                    if pkg_dir and (os.path.isfile(os.path.join(pkg_dir, nm + ".py"))
                                    or os.path.isdir(os.path.join(pkg_dir, nm))):
                        continue
                    if real.endswith("__init__.py") and (
                            os.path.isfile(os.path.join(os.path.dirname(real), nm + ".py"))
                            or os.path.isdir(os.path.join(os.path.dirname(real), nm))):
                        continue
                    if defined is None or nm in defined:
                        continue
                    problems.append(f"{where}: {dotted} has no name {nm!r}")
    return problems, n_fp


def product_graph():
    graph = {}
    for path in py_files():
        if not is_product(path) or stub_target(path):
            continue
        _src, tree = parse(path)
        edges = set()
        for node, _ in iter_imports(tree, module_level_only=True):
            for dotted, target, fp, names in resolve_import(path, node):
                if not fp or target is None:
                    continue
                real = follow(target)
                if real is None:
                    continue
                if os.path.isdir(real) or real.endswith("__init__.py"):
                    base = real if os.path.isdir(real) else os.path.dirname(real)
                    for nm in names or []:
                        sub = os.path.join(base, nm + ".py")
                        if os.path.isfile(sub):
                            edges.add(follow(sub) or sub)
                    continue
                if is_product(real) and real != path:
                    edges.add(real)
        graph[path] = edges
    return graph


def find_cycles(graph):
    index, low, stack, on, out = {}, {}, [], set(), []
    counter = [0]
    sys.setrecursionlimit(10000)

    def strong(v):
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on.add(v)
        for w in graph.get(v, ()):
            if w not in index:
                strong(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop()
                on.discard(w)
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1:
                out.append(sorted(rel(c) for c in comp))

    for v in list(graph):
        if v not in index:
            strong(v)
    return out


def self_path_inserts():
    bad = []
    for path in py_files():
        if not is_product(path):
            continue
        src, tree = parse(path)
        # names assigned from something derived from __file__
        from_file = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Assign) and "__file__" in ast.unparse(n.value):
                for t in n.targets:
                    if isinstance(t, ast.Name):
                        from_file.add(t.id)
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call) or not isinstance(n.func, ast.Attribute):
                continue
            if n.func.attr not in ("insert", "append"):
                continue
            base = ast.unparse(n.func.value)
            if not base.endswith("sys.path"):
                continue
            args = " ".join(ast.unparse(a) for a in n.args)
            names = {x.id for a in n.args for x in ast.walk(a) if isinstance(x, ast.Name)}
            if "__file__" in args or names & from_file:
                bad.append(f"{rel(path)}:{n.lineno}: {ast.unparse(n)}")
    return bad


def moved_names():
    """Old flat names of the modules that moved into dpmg/ (tools/ scripts keep
    plain names: tools/ is a folder of scripts, not a package)."""
    out = set()
    moves = os.path.join(ROOT, "tools", "reorg", "moves.tsv")
    for line in open(moves, encoding="utf-8"):
        f = line.rstrip("\n").split("\t")
        if line.startswith("#") or len(f) < 3 or "/" in f[1] or not f[1].endswith(".py"):
            continue
        if not f[2].startswith("dpmg/"):
            continue
        out.add(f[1][:-3])
    return out


def flat_imports():
    """Imports that still use an old flat module name."""
    names = moved_names()
    out = []
    for path in py_files():
        r = rel(path)
        _src, tree = parse(path)
        for node, _ in iter_imports(tree):
            if isinstance(node, ast.Import):
                tops = [a.name.split(".")[0] for a in node.names]
            elif node.level == 0 and node.module:
                tops = [node.module.split(".")[0]]
            else:
                continue
            for top in tops:
                if top not in names:
                    continue
                if r.startswith(OWN_DIR_IMPORTS) and os.path.isfile(
                        os.path.join(os.path.dirname(path), top + ".py")):
                    continue
                out.append(f"{r}:{node.lineno}: {top}")
    return out


# tests

def test_no_flat_imports_of_moved_modules():
    left = flat_imports()
    assert not left, "old flat imports left (use the dpmg package name):\n  " + "\n  ".join(left)


def test_first_party_imports_resolve():
    problems, n = check_resolution()
    print(f"  {n} first-party import statements checked")
    assert not problems, "unresolved imports:\n  " + "\n  ".join(problems)


def test_no_module_level_cycles():
    cycles = find_cycles(product_graph())
    assert not cycles, f"module-level import cycles: {cycles}"


def test_no_self_path_inserts():
    bad = self_path_inserts()
    assert not bad, "product modules adding their own folder to sys.path:\n  " + "\n  ".join(bad)


DUP_PROBE = r"""
import os, sys
root = sys.argv[1]
sys.path.insert(0, root)
import dpmg_service
import dpmg.web.nicegui_app
seen = {}
for name, m in list(sys.modules.items()):
    f = getattr(m, "__file__", None)
    if not f:
        continue
    f = os.path.realpath(f)
    if not f.startswith(root + os.sep):
        continue
    seen.setdefault(f, {})[id(m)] = name
dups = {f: sorted(v.values()) for f, v in seen.items() if len(v) > 1}
print("MODULES", len(seen))
print("DUPS", dups)
sys.exit(1 if dups else 0)
"""


def test_no_module_loaded_twice():
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", DPMG_DISCOVERY_PORT="47939",
               DPMG_SAVED_RADIOS="", DPMG_GUI_ID="GCS-IMPORT-TEST")
    r = subprocess.run([sys.executable, "-c", DUP_PROBE, os.path.realpath(ROOT)],
                       cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
    tail = (r.stdout + r.stderr)[-3000:]
    assert r.returncode == 0, f"duplicate modules or import failure:\n{tail}"
    print("  " + " ".join(l for l in r.stdout.splitlines() if l.startswith("MODULES")))


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
