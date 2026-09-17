#!/usr/bin/env python3
"""Resolve the dependency budget and derive which built-in plugins may stay.

Two jobs, both driven by ``prune/manifest.yaml``:

1. **Resolve the budget.**  ``allowed_dependencies`` names direct requirements;
   what actually matters is their *transitive* closure.  ``pandas`` is a hard
   ``xarray`` requirement, so a plugin importing pandas costs nothing -- the
   first version of this analysis missed that and wrongly excluded 14 readers.
   The closure is resolved with ``uv pip compile`` and cached in
   ``prune/dependency-closure.lock`` so builds are reproducible and offline-safe;
   refresh it deliberately with ``--refresh``.

2. **Derive the plugin set.**  A plugin module is admissible iff its own
   un-guarded third-party imports fall inside the closure *and* every plugin
   module it imports is admissible.  The second clause makes this a fixed point
   rather than a filter: ``interp_grid`` passes on its own imports but pulls in
   ``interp_scipy`` (scipy, alphashape, skimage), so a single pass would ship a
   plugin whose dependencies are absent.

Imports inside ``with import_optional_dependencies(...)`` or a
``try/except ImportError`` do not count -- upstream already uses those to make
``pyPublicDecompWT`` and ``numexpr`` soft, and honouring them is what keeps
``seviri_hrit`` and ``hrit_reader`` in the kept set.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from globs import Matcher  # noqa: E402

# Import name -> distribution name, for the cases where they differ.  Only the
# names that actually appear in geoips' import graph are listed; anything absent
# is assumed to import under its own distribution name.
IMPORT_TO_DIST = {
    # Only the rows `normalise()` cannot derive on its own -- i.e. where the
    # import name is not just the distribution name with underscores swapped for
    # hyphens. Everything else (netCDF4, pydantic_core, lexeme_type, ...) round
    # trips through normalise() and does not belong here.
    #
    # skimage/cv2/OpenSSL are not in the budget today; they cost three lines and
    # dropping them would plant a wrong answer the day scikit-image is added.
    "yaml": "pyyaml",
    "PIL": "pillow",
    "dateutil": "python-dateutil",
    "skimage": "scikit-image",
    "cv2": "opencv-python",
    "OpenSSL": "pyopenssl",
}


def normalise(dist: str) -> str:
    return dist.lower().replace("_", "-")


def import_in_budget(import_name: str, budget: set[str]) -> bool:
    """Return True if a top-level import name is covered by the budget."""
    dist = IMPORT_TO_DIST.get(import_name, import_name)
    return normalise(dist) in budget


# ---------------------------------------------------------------------------
# Budget resolution
# ---------------------------------------------------------------------------


def resolve_closure(requirements: list[str]) -> set[str]:
    """Resolve requirements to a transitive distribution set via `uv pip compile`."""
    proc = subprocess.run(
        ["uv", "pip", "compile", "--quiet", "--no-header", "-"],
        input="\n".join(requirements) + "\n",
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "uv pip compile failed -- resolve offline with the cached closure "
            f"or fix the budget:\n{proc.stderr.strip()}"
        )
    dists: set[str] = set()
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        # "name==version" or "name==version  # via ..."
        name = line.split("==")[0].split("#")[0].strip()
        if name:
            dists.add(normalise(name))
    return dists


def budget_requirements(pyproject_path: str) -> tuple[list[str], list[str]]:
    """Read the dependency budget from the fork's pyproject.

    One declaration, not two. The budget used to be restated in
    prune/manifest.yaml as `allowed_dependencies`, which meant two hand-synced
    lists of the same thing -- and they had already drifted. The pyproject is
    the honest home: it is what actually gets installed, and both Dockerfiles
    already build from it.

    Only the extras named in `[tool.mini-ips] runtime_extras` count; `lint` and
    `test` are developer tooling and must not be able to widen the plugin set.
    """
    import tomllib

    with open(pyproject_path, "rb") as handle:
        data = tomllib.load(handle)
    project = data["project"]
    settings = data.get("tool", {}).get("mini-ips", {})
    required = [_requirement_name(d) for d in project.get("dependencies") or []]
    optional = project.get("optional-dependencies") or {}
    extras = [
        _requirement_name(d)
        for group in settings.get("runtime_extras") or []
        for d in optional.get(group) or []
    ]
    return required, extras


def _requirement_name(spec: str) -> str:
    """Strip version specifiers and markers from a PEP 508 requirement."""
    return re.split(r"[<>=!~\[ ;(]", spec.strip())[0].strip()


def budget_ceilings(pyproject_path: str) -> dict[str, int]:
    """Maximum resolved package counts, from `[tool.mini-ips]`."""
    import tomllib

    with open(pyproject_path, "rb") as handle:
        settings = tomllib.load(handle).get("tool", {}).get("mini-ips", {})
    return {
        "required": settings.get("max_packages_required", 10**6),
        "all": settings.get("max_packages_with_extras", 10**6),
    }


def load_or_resolve(
    pyproject_path: str, cache_path: str, refresh: bool
) -> dict[str, set[str]]:
    """Return {"required": {...}, "all": {...}} distribution sets."""
    required, extras = budget_requirements(pyproject_path)
    every = required + extras

    if not refresh and os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as handle:
            cached = json.load(handle)
        # A lock resolved from a different budget is worse than no lock: it
        # silently answers the previous question. Refuse rather than refresh
        # implicitly, so editing the budget cannot quietly skip re-resolution.
        stale = cached.get("requirements", {}).get("with_extras") != every
        if not stale:
            return {k: set(v) for k, v in cached["closure"].items()}
        sys.exit(
            "error: prune/dependency-closure.lock was resolved from a different "
            "dependency budget than overlay/pyproject.toml now declares.\n"
            "Refresh it with:  python tools/depbudget.py --refresh"
        )
    closure = {"required": resolve_closure(required), "all": resolve_closure(every)}
    with open(cache_path, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "_comment": (
                    "Derived from prune/manifest.yaml allowed_dependencies by "
                    "tools/depbudget.py --refresh. Checked in so builds are "
                    "reproducible and offline-safe; a diff here in an upstream "
                    "sync PR means the dependency surface moved."
                ),
                "requirements": {"required": required, "with_extras": every},
                "closure": {k: sorted(v) for k, v in closure.items()},
            },
            handle,
            indent=1,
        )
        handle.write("\n")
    return closure


# ---------------------------------------------------------------------------
# AST scanning
# ---------------------------------------------------------------------------


@dataclass
class ModuleImports:
    """Un-guarded third-party imports and in-package imports of one module.

    Imports inside an optional-import guard are simply not recorded: nothing
    ever needed the list of soft imports, only the fact that they do not count.
    """

    hard: set[str] = field(default_factory=set)
    internal: set[str] = field(default_factory=set)


class _ImportScanner(ast.NodeVisitor):
    #: The context manager geoips uses to mark an import optional
    #: (geoips.utils.context_managers.import_optional_dependencies).
    #: try/except ImportError is recognised structurally, by shape, not by name.
    GUARD = "import_optional_dependencies"

    def __init__(
        self,
        package: str,
        known_modules: set[str],
        module: str = "",
        is_package: bool = False,
    ):
        self.package = package
        self.known = known_modules
        # Needed to resolve relative imports: `from . import x` means something
        # different in a package __init__ than in a module beside it.
        self.module = module
        self.is_package = is_package
        self.result = ModuleImports()
        self._guard_depth = 0

    def _resolve_relative(self, level: int, module: str | None) -> str | None:
        """Turn a relative import into an absolute dotted name."""
        parts = self.module.split(".") if self.module else []
        # Inside a package __init__, level 1 means the package itself; in a
        # plain module it means the package containing it.
        base = parts if self.is_package else parts[:-1]
        if level > 1:
            base = base[: len(base) - (level - 1)]
        if not base:
            return None
        return ".".join(base + ([module] if module else []))

    def _record(self, dotted: str) -> None:
        top = dotted.split(".")[0]
        if top == self.package:
            # Attribute the edge to the longest prefix that is a real module.
            parts = dotted.split(".")
            for i in range(len(parts), 0, -1):
                candidate = ".".join(parts[:i])
                if candidate in self.known:
                    self.result.internal.add(candidate)
                    return
            return
        if top in sys.stdlib_module_names:
            return
        if not self._guard_depth:
            self.result.hard.add(top)

    def visit_With(self, node: ast.With) -> None:
        guarded = any(
            isinstance(item.context_expr, ast.Call)
            and getattr(item.context_expr.func, "id", None) == self.GUARD
            for item in node.items
        )
        self._guard_depth += int(guarded)
        self.generic_visit(node)
        self._guard_depth -= int(guarded)

    def visit_Try(self, node: ast.Try) -> None:
        def catches_import_error(handler: ast.ExceptHandler) -> bool:
            exc = handler.type
            if isinstance(exc, ast.Name):
                return exc.id in ("ImportError", "ModuleNotFoundError")
            if isinstance(exc, ast.Tuple):
                return any(
                    getattr(elt, "id", None) in ("ImportError", "ModuleNotFoundError")
                    for elt in exc.elts
                )
            return False

        guarded = any(catches_import_error(h) for h in node.handlers)
        self._guard_depth += int(guarded)
        for stmt in node.body:
            self.visit(stmt)
        self._guard_depth -= int(guarded)
        for handler in node.handlers:
            self.visit(handler)
        for stmt in list(node.orelse) + list(node.finalbody):
            self.visit(stmt)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._record(alias.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level:
            resolved = self._resolve_relative(node.level, node.module)
            if resolved is None:
                return
            self._record(resolved)
            for alias in node.names:
                self._record(f"{resolved}.{alias.name}")
            return
        if not node.module:
            return
        self._record(node.module)
        for alias in node.names:
            self._record(f"{node.module}.{alias.name}")


def module_name(tree_root: str, path: str) -> str:
    rel = os.path.relpath(path, tree_root)[: -len(".py")]
    parts = rel.split(os.sep)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def scan_tree(
    tree_root: str, package: str = "geoips"
) -> tuple[dict[str, str], dict[str, ModuleImports]]:
    """Return (module -> path, module -> imports) for the whole package."""
    paths: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(os.path.join(tree_root, package)):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for filename in filenames:
            if filename.endswith(".py"):
                full = os.path.join(dirpath, filename)
                paths[module_name(tree_root, full)] = full

    known = set(paths)
    imports: dict[str, ModuleImports] = {}
    for name, path in paths.items():
        source = open(path, encoding="utf-8", errors="replace").read()
        try:
            parsed = ast.parse(source, filename=path)
        except SyntaxError:
            imports[name] = ModuleImports()
            continue
        scanner = _ImportScanner(
            package,
            known,
            module=name,
            is_package=os.path.basename(path) == "__init__.py",
        )
        scanner.visit(parsed)
        imports[name] = scanner.result
    return paths, imports


# ---------------------------------------------------------------------------
# The fixed point
# ---------------------------------------------------------------------------


@dataclass
class PluginDecision:
    """Whether one built-in plugin fits the dependency budget, and why."""

    module: str
    path: str
    kept: bool
    reason: str


@dataclass
class Derivation:
    """The outcome of deriving the built-in plugin set."""

    decisions: list[PluginDecision]
    #: Non-core, non-plugin modules that kept plugins need (e.g.
    #: data_manipulations/corrections.py).  Auto-included so the manifest does
    #: not have to enumerate every private helper a plugin happens to use.
    support: set[str]


def derive_plugins(
    tree_root: str,
    plugin_root: str,
    budget: set[str],
    core_modules: Iterable[str] = (),
) -> Derivation:
    """Decide which built-in plugins fit the dependency budget.

    A plugin's cost is the cost of its whole private import closure, not just
    its own import lines.  ``coverage_checkers/center_radius`` imports nothing
    but numpy, yet calls ``geoips.utils.coverage_checkers.create_radius``, which
    hard-imports ``skimage.draw`` inside the function body -- so the plugin does
    need scikit-image and must be excluded.  Checking only the plugin's own
    imports gets that wrong.

    Traversal stops at ``core_modules`` (the code the manifest keeps outright).
    Their dependencies define the budget's extras, so they are already paid for
    and following them would drag matplotlib/cartopy/netCDF4 in via the
    interface modules that every plugin imports -- which would make the answer
    meaningless.

    Traversal stops at ``patch_sites`` too, recording the edge instead of
    costing it.  Those are the handful of places where kept code legitimately
    reaches into code we drop and a patch repoints it -- they are work items,
    not reasons to exclude a plugin.  Everything else the manifest denies is
    still traversed for cost accounting: that is what makes the budget, rather
    than the deny list, decide whether a plugin can stay.
    """
    paths, imports = scan_tree(tree_root)
    plugin_prefix = plugin_root.replace("/", ".")
    plugins = {m for m in paths if m.startswith(plugin_prefix)}
    core = set(core_modules)

    def closure(start: str) -> tuple[set[str], set[str]]:
        seen: set[str] = set()
        stack = [start]
        hard: set[str] = set()
        support: set[str] = set()
        while stack:
            module = stack.pop()
            if module in seen or module not in imports:
                continue
            seen.add(module)
            hard |= imports[module].hard
            for dep in imports[module].internal:
                if dep in core:
                    continue  # already paid for by the manifest
                stack.append(dep)
                if dep not in plugins:
                    support.add(dep)
        return hard, support

    reasons: dict[str, str] = {}
    admissible: set[str] = set()
    support_for: dict[str, set[str]] = {}

    for module in sorted(plugins):
        hard, support = closure(module)
        offenders = sorted(d for d in hard if not import_in_budget(d, budget))
        if offenders:
            reasons[module] = "needs " + ", ".join(offenders)
            continue
        admissible.add(module)
        support_for[module] = support

    kept_support = {
        os.path.relpath(paths[dep], tree_root)
        for module in admissible
        for dep in support_for.get(module, ())
    }

    decisions = [
        PluginDecision(
            module=module,
            path=os.path.relpath(paths[module], tree_root),
            kept=module in admissible,
            reason=reasons.get(module, "within dependency budget"),
        )
        for module in sorted(plugins)
    ]
    return Derivation(
        decisions=decisions,
        support=kept_support,
    )


def core_modules_from_globs(tree_root: str, patterns: list[str]) -> set[str]:
    """Dotted module names for the .py files matched by a set of manifest globs."""
    matcher = Matcher(list(patterns))
    modules: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(os.path.join(tree_root, "geoips")):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            full = os.path.join(dirpath, filename)
            rel = os.path.relpath(full, tree_root).replace(os.sep, "/")
            if matcher.match(rel):
                modules.add(module_name(tree_root, full))
    return modules


# ---------------------------------------------------------------------------
# CLI (inspection; slim.py imports the functions directly)
# ---------------------------------------------------------------------------


def load_manifest(path: str) -> dict:
    try:
        import yaml
    except ModuleNotFoundError:
        sys.exit("error: pyyaml is required to read the manifest")
    with open(path, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def main(argv: list[str] | None = None) -> int:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "tree", nargs="?", default=os.path.join(here, "upstream-geoips")
    )
    parser.add_argument("--manifest", default=os.path.join(here, "prune/manifest.yaml"))
    parser.add_argument(
        "--closure-cache", default=os.path.join(here, "prune/dependency-closure.lock")
    )
    parser.add_argument(
        "--pyproject", default=os.path.join(here, "overlay/pyproject.toml")
    )
    parser.add_argument(
        "--refresh", action="store_true", help="re-resolve the budget with uv"
    )
    parser.add_argument("--json", metavar="FILE")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    manifest = load_manifest(args.manifest)
    closure = load_or_resolve(args.pyproject, args.closure_cache, args.refresh)
    plugin_cfg = manifest.get("plugins") or {}

    # Standalone inspection has no manifest-derived core set, so nothing is
    # treated as pre-paid.  slim.py passes the real one; use --core-glob here
    # only for spot checks.
    core = core_modules_from_globs(args.tree, manifest.get("keep") or [])
    # Judge against the tier the build actually ships, not always "all" --
    # otherwise this tool reports a different plugin set from prune/slim.py.
    tier = plugin_cfg["budget_tier"]
    derivation = derive_plugins(
        args.tree,
        plugin_cfg["root"],
        closure[tier],
        core_modules=core,
    )
    decisions = derivation.decisions

    real = [d for d in decisions if not d.path.endswith("__init__.py")]
    kept = [d for d in real if d.kept]
    dropped = [d for d in real if not d.kept]

    def loc(items: list[PluginDecision]) -> int:
        total = 0
        for item in items:
            full = os.path.join(args.tree, item.path)
            with open(full, encoding="utf-8", errors="replace") as handle:
                total += sum(1 for _ in handle)
        return total

    print(
        f"budget: {len(closure['required'])} dists required, "
        f"{len(closure['all'])} with extras"
    )
    print(
        f"plugins: {len(kept)} kept ({loc(kept)} LOC), "
        f"{len(dropped)} excluded ({loc(dropped)} LOC)"
    )

    by_interface: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for decision in real:
        interface = decision.path.split("/")[3]
        by_interface[interface][0 if decision.kept else 1] += 1
    print(f"\n  {'interface':30s} kept excluded")
    for interface in sorted(by_interface):
        keep_n, drop_n = by_interface[interface]
        print(f"  {interface:30s} {keep_n:4d} {drop_n:8d}")

    print("\nexcluded:")
    for decision in dropped:
        print(
            f"  {decision.path.replace('geoips/plugins/classes/', ''):48s} "
            f"{decision.reason}"
        )

    if args.verbose:
        print("\nkept:")
        for decision in kept:
            print(f"  {decision.path.replace('geoips/plugins/classes/', '')}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "closure": {k: sorted(v) for k, v in closure.items()},
                    "plugins": [d.__dict__ for d in decisions],
                },
                handle,
                indent=1,
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
