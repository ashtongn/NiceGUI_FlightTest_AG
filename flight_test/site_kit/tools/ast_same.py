#!/usr/bin/env python3
"""Say whether two Python files hold the same code once comments and docstrings are
left out: prints "comments only" or "CODE DIFFERS".

    python3 ast_same.py OLD.py NEW.py
"""
import ast
import sys


def norm(src):
    t = ast.parse(src)
    for n in ast.walk(t):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)) and n.body:
            first = n.body[0]
            if isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant) \
                    and isinstance(first.value.value, str):
                n.body = n.body[1:] or [ast.Pass()]
    return ast.dump(t, include_attributes=False)


if __name__ == "__main__":
    a = open(sys.argv[1], encoding="utf-8").read()
    b = open(sys.argv[2], encoding="utf-8").read()
    print("comments only" if norm(a) == norm(b) else "CODE DIFFERS")
