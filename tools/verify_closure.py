#!/usr/bin/env python3
"""Import-closure gate: every ``geoips.*`` import must resolve inside the tree.

Pruning a package by deleting files is only safe if nothing left behind still
imports what was removed.  GeoIPS hides a lot of coupling in function-local
imports (``sector_utils/utils.py`` defers three of them), so a check that only
looks at module-level imports misses most of the risk.  This walks the full AST
of every module instead.

Resolution rules, deliberately strict:

* ``from A.B import c``  -> ``A.B`` must be a module in the tree.
  ``c`` may be either a submodule or an attribute, so it is not checked.
* ``import A.B``         -> ``A.B`` must be a module in the tree.
* relative imports are skipped (they cannot escape the tree).

The looser rule -- "walk back until some prefix exists" -- passes
``from geoips.filenames.product_filenames import x`` just because
``geoips.filenames`` exists, which is how that upstream bug went unnoticed.

This module is a library, not a command.  ``prune/slim.py`` calls ``scan()``
twice -- once on the pristine upstream export before patching, once on the
finished tree -- and fails only on the difference.  It has to live there: the
baseline must be *unpatched* (patches 02 and 07 rewrite ``geoips.*`` imports
themselves, so a patched baseline would carry a mis-authored one too and the
diff would silently cancel), and by the time a standalone command could run,
the pristine export is gone.

Upstream is not clean -- at 1.20.0a0 ``data_manipulations/merge.py:111``
imports ``geoips.filenames.product_filenames``, which does not exist -- and a
gate that reports inherited breakage alongside our own is one people learn to
ignore.
"""

from __future__ import annotations

import ast
import os
from dataclasses import dataclass


@dataclass(frozen=True, order=True)
class Finding:
    """One unresolvable ``geoips.*`` import."""

    path: str  # tree-relative, so findings compare across trees
    lineno: int
    target: str
    kind: str  # "import" | "from-module" | "syntax"

    def __str__(self) -> str:
        return f"{self.path}:{self.lineno} [{self.kind}] -> {self.target}"


def module_name(tree_root: str, path: str) -> str:
    """Dotted module name for a .py file, dropping a trailing ``__init__``."""
    rel = os.path.relpath(path, tree_root)[: -len(".py")]
    parts = rel.split(os.sep)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def find_modules(tree_root: str, package: str) -> dict[str, str]:
    """Map dotted module name -> file path for every .py under ``package``."""
    modules: dict[str, str] = {}
    pkg_root = os.path.join(tree_root, package)
    if not os.path.isdir(pkg_root):
        # Raise rather than sys.exit: this is a library now, and the caller
        # (prune/slim.py) owns the process's exit codes.
        raise FileNotFoundError(f"no '{package}' package under {tree_root}")
    for dirpath, dirnames, filenames in os.walk(pkg_root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for filename in filenames:
            if filename.endswith(".py"):
                full = os.path.join(dirpath, filename)
                modules[module_name(tree_root, full)] = full
    return modules


def scan(tree_root: str, package: str) -> list[Finding]:
    """Collect every ``<package>.*`` import that does not resolve in-tree."""
    modules = find_modules(tree_root, package)
    known = set(modules)
    findings: list[Finding] = []

    for _, path in sorted(modules.items()):
        rel = os.path.relpath(path, tree_root)
        source = open(path, encoding="utf-8", errors="replace").read()
        try:
            parsed = ast.parse(source, filename=path)
        except SyntaxError as exc:
            findings.append(Finding(rel, exc.lineno or 0, str(exc), "syntax"))
            continue

        for node in ast.walk(parsed):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] != package:
                        continue
                    if alias.name not in known:
                        findings.append(Finding(rel, node.lineno, alias.name, "import"))
            elif isinstance(node, ast.ImportFrom):
                if node.level:  # relative; cannot leave the tree
                    continue
                base = node.module or ""
                if base.split(".")[0] != package:
                    continue
                if base not in known:
                    findings.append(Finding(rel, node.lineno, base, "from-module"))

    return findings
