#!/usr/bin/env python3
"""Generate the mini-ips tree from a pinned upstream geoips revision.

Five stages, each gated.  Idempotent: the same ``upstream.lock`` produces a
byte-identical ``build/``.

    RESOLVE   export the upstream tree at the locked SHA, then apply
              prune/patches/*.patch to it
    CLASSIFY  match every upstream path against the manifest; derive the
              plugin set; refuse to continue if anything is unclassified
    PRUNE     copy the kept paths
    OVERLAY   lay the fork-owned files over the result
    VERIFY    closure gate, import, registry round-trip, tests, benchmark

Patching happens BEFORE classification on purpose. The plugin derivation reads
import statements, and two of the patches change exactly that: patch 04 moves
`zarr` into an optional-import guard and patch 08 defers `cartopy`. Deriving
from unpatched sources judges 9 readers and the whole imagery chain against
dependencies the patched code does not actually require.

Why generate instead of maintaining a normal fork: deleting ~200 files means a
*deleted by us / modified by them* conflict on every upstream commit that
touches one of them, and upstream's release process rewrites
``.github/versions/tagged_version``, ``environments/`` and
``docs/source/releases/latest/`` every cycle.  Regenerating from a pinned SHA
has no merge step, so it has no conflicts.

Exit codes: 0 ok, 2 usage/environment, 3 unclassified paths, 4 patch failure,
5 verification failure.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field

import yaml as yaml_module

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools")
)

import depbudget  # noqa: E402  (path juggling above is deliberate)
import verify_closure  # noqa: E402
from globs import Matcher  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Deliberately not "geoips": a sibling directory of that name shadows the
# installed package as a namespace package, so every tool that runs from the
# repo root would import the unpruned upstream tree instead of the build.
UPSTREAM_CLONE = "upstream-geoips"


# ---------------------------------------------------------------------------
# stage 1: resolve
# ---------------------------------------------------------------------------


def read_lock(path: str) -> dict:
    """Load and sanity-check upstream.lock."""
    with open(path, encoding="utf-8") as handle:
        lock = yaml_module.safe_load(handle)
    for key in ("repo", "sha", "ref"):
        if key not in lock:
            sys.exit(f"error: {path} is missing required key '{key}'")
    return lock


def resolve_upstream(lock: dict, dest: str, upstream_path: str | None) -> None:
    """Export the upstream tree at the locked SHA into ``dest``."""
    sha = lock["sha"]
    if upstream_path:
        source = upstream_path
        have = subprocess.run(
            ["git", "-C", source, "cat-file", "-e", f"{sha}^{{commit}}"],
            capture_output=True,
        )
        if have.returncode != 0:
            sys.exit(
                f"error: {source} does not contain {sha[:9]}; fetch it or drop "
                "--upstream-path to clone from the remote"
            )
    else:
        cache = os.path.join(ROOT, ".upstream-cache.git")
        if not os.path.isdir(cache):
            run(["git", "clone", "--bare", "--filter=blob:none", lock["repo"], cache])
        run(
            [
                "git",
                "-C",
                cache,
                "fetch",
                "--tags",
                "origin",
                "+refs/heads/*:refs/heads/*",
            ]
        )
        source = cache

    os.makedirs(dest, exist_ok=True)
    archive = subprocess.Popen(
        ["git", "-C", source, "archive", "--format=tar", sha], stdout=subprocess.PIPE
    )
    extract = subprocess.Popen(["tar", "-x", "-C", dest], stdin=archive.stdout)
    assert archive.stdout is not None
    archive.stdout.close()
    extract.communicate()
    if extract.returncode != 0 or archive.wait() != 0:
        sys.exit("error: failed to export the upstream tree")


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a command, exiting with its output if it fails."""
    proc = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout + proc.stderr)
        sys.exit(f"error: command failed: {' '.join(cmd)}")
    return proc


# ---------------------------------------------------------------------------
# stage 2: classify
# ---------------------------------------------------------------------------


@dataclass
class Classification:
    """The keep/drop decision for every upstream path."""

    keep: set[str] = field(default_factory=set)
    drop: set[str] = field(default_factory=set)
    unclassified: list[str] = field(default_factory=list)
    plugin_decisions: list[depbudget.PluginDecision] = field(default_factory=list)
    #: per-YAML-plugin keep/drop reason (workflows, products, sectors, ...)
    yaml_reasons: dict[str, str] = field(default_factory=dict)
    closure: dict[str, set[str]] = field(default_factory=dict)
    #: auto-included private helpers of kept plugins
    support: set[str] = field(default_factory=set)
    #: which budget tier built-in plugins were judged against
    tier: str = "required"


class _TagTolerantLoader(yaml_module.SafeLoader):
    """SafeLoader that does not choke on geoips' custom YAML tags.

    geoips plugin YAML uses `!ENV ${GEOIPS_TESTDATA_DIR}/...` (resolved at load
    time by geoips-yaml-utils). Plain `yaml.safe_load` raises ConstructorError
    on it. That mattered a lot: with the error swallowed, every workflow
    carrying an `!ENV` path parsed as "references no plugins" and was kept --
    including eight that reference excluded readers and sectors, which then
    failed pydantic validation at run time instead of being pruned.

    We only need the structure, never the resolved value, so unknown tags
    collapse to their raw scalar/sequence/mapping.
    """


def _passthrough_scalar(loader, node):
    return str(node.value)


def _passthrough_multi(loader, tag_suffix, node):
    if isinstance(node, yaml_module.ScalarNode):
        return str(node.value)
    if isinstance(node, yaml_module.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


_TagTolerantLoader.add_constructor("!ENV", _passthrough_scalar)
_TagTolerantLoader.add_multi_constructor("!", _passthrough_multi)


def load_yaml_docs(path: str) -> list:
    """Load every document in a plugin YAML file.

    Raises on malformed YAML. Callers must decide what an unparseable plugin
    means rather than treating it as an empty one.
    """
    with open(path, encoding="utf-8", errors="replace") as handle:
        return [doc for doc in yaml_module.load_all(handle, Loader=_TagTolerantLoader)]


def singular(kind: str) -> str:
    """Normalise an interface/kind name for comparison.

    Workflow steps name a `kind` in the singular ("colormapper") while
    interfaces are plural ("colormappers"). geoips uses a Lexeme type for this;
    slim.py runs outside the built package, so a trailing-s strip is enough for
    every interface name geoips actually has.
    """
    return kind[:-1] if kind.endswith("s") else kind


def plugin_names(tree: str, kept_paths: set[str]) -> set[tuple[str, str]]:
    """(kind, name) pairs registered by the kept class plugins.

    The kind matters: `Infrared` exists as a workflow, a product AND a
    colormapper upstream, so a name-only check happily keeps a workflow whose
    `colormapper: Infrared` step was excluded, and the failure only surfaces
    later as a pydantic validation error at run time.

    Plugins declare `interface` and `name` either at module level or on the
    plugin class, so walk all assignments and pair them up per module.
    """
    pairs: set[tuple[str, str]] = set()
    for rel in kept_paths:
        if not rel.startswith("geoips/plugins/classes/") or not rel.endswith(".py"):
            continue
        try:
            parsed = ast.parse(open(os.path.join(tree, rel), errors="replace").read())
        except SyntaxError:
            continue
        found: dict[str, str] = {}
        for node in ast.walk(parsed):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id in ("name", "interface")
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                ):
                    # Class-level assignments come after module-level ones and
                    # are the authoritative values for class-based plugins.
                    found[target.id] = node.value.value
        if "name" in found and "interface" in found:
            pairs.add((singular(found["interface"]), found["name"]))
    return pairs


def declared_plugins(docs: list) -> set[tuple[str, str]]:
    """(kind, name) pairs that these parsed YAML documents declare."""
    pairs: set[tuple[str, str]] = set()
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        name = doc.get("name")
        interface = doc.get("interface")
        if isinstance(name, str) and isinstance(interface, str):
            pairs.add((singular(interface), name))
        if interface == "products" and isinstance(doc.get("spec"), dict):
            # `family: list` products declare their members under
            # spec.products, each with its own name.
            for entry in doc["spec"].get("products") or []:
                if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                    pairs.add(("product", entry["name"]))
    return pairs


def referenced_plugins(docs: list, known_kinds: set[str]) -> set[tuple[str, str]]:
    """(kind, name) pairs that these parsed YAML documents reference.

    Two shapes are in use and both have to be understood:

    * workflow steps -- ``{kind: reader, name: abi_netcdf}``
    * product / product_default specs --
      ``colormapper: {plugin: {name: matplotlib_linear_norm}}``, where the
      *parent key* carries the kind.
    * bare scalars -- ``product_defaults: color89``, which is how a `family:
      list` product names the product_default each member inherits from.

    ``known_kinds`` gates the last two shapes. Without it, any mapping with a
    nested ``plugin.name`` -- or any string value under any key -- would read as
    a plugin reference and produce phantom dependencies.
    """
    referenced: set[tuple[str, str]] = set()

    def walk(node, parent_key: str | None = None) -> None:
        if isinstance(node, dict):
            kind, name = node.get("kind"), node.get("name")
            if isinstance(kind, str) and isinstance(name, str):
                referenced.add((singular(kind), name))
            # `<kind>: {plugin: {name: ...}}`
            plugin = node.get("plugin")
            if (
                parent_key
                and singular(parent_key) in known_kinds
                and isinstance(plugin, dict)
                and isinstance(plugin.get("name"), str)
            ):
                referenced.add((singular(parent_key), plugin["name"]))
            for key, value in node.items():
                # `<kind>: <name>` as a plain scalar.
                if (
                    isinstance(key, str)
                    and isinstance(value, str)
                    and singular(key) in known_kinds
                ):
                    referenced.add((singular(key), value))
                walk(value, key if isinstance(key, str) else parent_key)
        elif isinstance(node, list):
            for item in node:
                walk(item, parent_key)

    for doc in docs:
        walk(doc)
    return referenced


def _skip_dir(dirnames: list[str]) -> bool:
    """Prune VCS/cache dirs from an os.walk in place; never skips the walk."""
    dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__")]
    return False


def classify(tree: str, manifest: dict, closure: dict[str, set[str]]) -> Classification:
    result = Classification(closure=closure)

    keep_matcher = Matcher(
        list(manifest.get("keep") or []) + list(manifest.get("keep_optional") or [])
    )
    deny_matcher = Matcher(list(manifest.get("deny") or []))

    plugin_root = manifest["plugins"]["root"]
    yaml_root = manifest["yaml_plugins"]["root"]

    # --- derived class plugins ---------------------------------------------
    # The derivation needs to know which modules the manifest keeps outright
    # ("core", already paid for -- stop traversal there), so resolve those globs
    # to module names before deriving.
    # NOTE: `keep_optional` is deliberately absent here. Those modules ship,
    # but their dependencies must be charged to whoever imports them.
    #
    # There is no "patch site" concept because patches are applied BEFORE
    # classification: by the time the derivation runs, patches 02 and 07 have
    # already repointed the only two imports that reached dropped code, so no
    # kept module references geoips.dev.output_config or single_source at all.
    # If patching ever moves after classification, that stops being true and a
    # traversal-stop mechanism has to come back.
    core_modules = depbudget.core_modules_from_globs(tree, manifest.get("keep") or [])
    # Which tier of the budget a built-in plugin must fit inside.
    # `required` is the strict reading of the fork's rule: a plugin may not
    # increase the dependencies of a default `pip install mini-geoips`.
    # The extras exist so EXTERNAL plugins can reuse geoips' helpers (e.g.
    # image_utils.mpl_utils), not to widen what we ship ourselves -- and
    # registry creation exec_module()s every plugin, so a plugin needing an
    # uninstalled extra breaks `geoips config create-registries` outright.
    tier = manifest["plugins"]["budget_tier"]
    if tier not in closure:
        sys.exit(f"error: plugins.budget_tier must be one of {sorted(closure)}")
    derivation = depbudget.derive_plugins(
        tree,
        plugin_root,
        closure[tier],
        core_modules=core_modules,
    )
    result.tier = tier
    result.plugin_decisions = derivation.decisions
    result.support = set(derivation.support)
    plugin_keep = {d.path for d in result.plugin_decisions if d.kept}
    plugin_drop = {d.path for d in result.plugin_decisions if not d.kept}
    # Private helpers the kept plugins reach for (data_manipulations/corrections,
    # sector_utils/yaml_utils, ...).  Auto-including these is what stops the
    # manifest from having to enumerate every module a plugin happens to use.
    result.keep.update(result.support)

    # Walk the filesystem rather than asking git. The export is patched in
    # place, which makes it a git repo, and `git ls-files` there would honour
    # upstream's .gitignore -- which ignores `_version.py` and
    # `docs/source/releases/*.rst` even though upstream tracks them. Those
    # files would then never be classified, never be copied, and `import
    # geoips` would fail on the missing `_version`.
    all_paths = sorted(
        os.path.relpath(os.path.join(dirpath, name), tree).replace(os.sep, "/")
        for dirpath, dirnames, names in os.walk(tree)
        if not _skip_dir(dirnames)
        for name in names
    )

    for path in all_paths:
        # Auto-included plugin support modules are already decided.
        if path in result.keep:
            continue
        # Python under the plugin root is decided by the derivation; anything
        # else there (data files sitting beside a plugin) falls through to the
        # normal keep/deny matchers, so the manifest still governs it.
        if path.startswith(plugin_root + "/") and path.endswith(".py"):
            if path in plugin_keep:
                result.keep.add(path)
            elif path in plugin_drop:
                result.drop.add(path)
            else:
                result.unclassified.append(path)
            continue

        if keep_matcher.match(path):
            result.keep.add(path)
            continue
        if deny_matcher.match(path):
            result.drop.add(path)
            continue
        result.unclassified.append(path)

    # --- derived YAML plugins ----------------------------------------------
    # A YAML plugin is only worth shipping if every plugin it names resolves.
    # This is a fixed point, not a single pass: products reference
    # product_defaults, which reference colormappers and algorithms, so
    # excluding a colormapper can strand a product_default and in turn a
    # product. Iterating means one tightened dependency budget prunes the whole
    # chain instead of leaving YAML that fails pydantic validation at run time.
    class_plugin_names = plugin_names(tree, result.keep)
    candidates = sorted(
        p
        for p in result.keep
        if p.startswith(yaml_root + "/") and p.endswith((".yaml", ".yml"))
    )
    result.keep -= set(candidates)

    known_kinds = {kind for kind, _ in class_plugin_names}

    parse_failures: dict[str, str] = {}
    parsed: dict[str, list] = {}
    for path in candidates:
        try:
            parsed[path] = load_yaml_docs(os.path.join(tree, path))
        except Exception as exc:  # noqa: BLE001 -- yaml raises several types
            parse_failures[path] = f"could not parse: {exc}"

    # Declarations before references, and the order matters: two of the three
    # reference shapes are gated on `known_kinds`. Extracting references first
    # meant `product_defaults: color89` was invisible (product_default was not
    # yet a known kind), so 27 products survived while the product_defaults
    # they inherit from had all been excluded.
    declares = {path: declared_plugins(docs) for path, docs in parsed.items()}
    known_kinds |= {kind for pairs in declares.values() for kind, _ in pairs}
    references = {
        path: referenced_plugins(docs, known_kinds) for path, docs in parsed.items()
    }

    for path, reason in parse_failures.items():
        result.drop.add(path)
        result.yaml_reasons[path] = reason

    admissible = [p for p in candidates if p not in parse_failures]
    while True:
        available = class_plugin_names | {
            pair for path in admissible for pair in declares[path]
        }
        rejected = []
        for path in admissible:
            missing = sorted(
                f"{kind}:{name}"
                for kind, name in references[path]
                # Only judge kinds we actually know about: an unknown kind means
                # the extractor mis-read the shape, and guessing would prune
                # good plugins.
                if kind in known_kinds and (kind, name) not in available
            )
            if missing:
                rejected.append((path, missing))
        if not rejected:
            break
        for path, missing in rejected:
            result.drop.add(path)
            result.yaml_reasons[path] = (
                "references excluded plugin(s): "
                + ", ".join(missing[:4])
                + ("..." if len(missing) > 4 else "")
            )
        admissible = [p for p in admissible if p not in result.drop]

    for path in admissible:
        result.keep.add(path)
        result.yaml_reasons[path] = "all referenced plugins resolve"

    # --- ancestor package markers ------------------------------------------
    # A kept module is unimportable without the __init__.py files above it.
    for path in sorted(result.keep):
        if not path.endswith(".py"):
            continue
        parts = path.split("/")[:-1]
        while parts:
            marker = "/".join(parts + ["__init__.py"])
            if os.path.exists(os.path.join(tree, marker)):
                result.keep.add(marker)
                result.drop.discard(marker)
            parts.pop()

    return result


# ---------------------------------------------------------------------------
# stage 3: prune + patch
# ---------------------------------------------------------------------------


def prune(tree: str, out: str, classification: Classification) -> None:
    if os.path.exists(out):
        shutil.rmtree(out)
    for rel in sorted(classification.keep):
        src = os.path.join(tree, rel)
        if not os.path.isfile(src):
            continue
        dst = os.path.join(out, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)


@dataclass
class PatchResult:
    """What happened when one patch was applied."""

    name: str
    status: str  # "applied" | "noop" | "failed"
    detail: str = ""


def apply_patches(out: str, patch_dir: str) -> list[PatchResult]:
    """Apply every patch in ``patch_dir`` to the exported tree.

    ``git apply`` needs no repository -- it patches a plain directory -- and is
    all-or-nothing per patch, so a failure leaves nothing half-applied and
    there is nothing to roll back. An earlier version made the export a git
    repo and committed after each patch so that ``git apply -3`` could
    three-way merge a stale one; that never worked, because a freshly
    initialised export has no blobs to merge against and ``-3`` fails with
    "repository lacks the necessary blob" every time.

    A patch that reverse-applies cleanly is already present upstream. That is
    worth reporting: it is how the patch set shrinks instead of accumulating.
    """
    if not os.path.isdir(patch_dir):
        return []
    results: list[PatchResult] = []
    for name in sorted(os.listdir(patch_dir)):
        if not name.endswith(".patch"):
            continue
        patch = os.path.join(patch_dir, name)
        if _git_apply(out, "--reverse", "--check", patch).returncode == 0:
            results.append(
                PatchResult(
                    name, "noop", "already present upstream -- delete this patch"
                )
            )
            continue
        applied = _git_apply(out, patch)
        if applied.returncode == 0:
            results.append(PatchResult(name, "applied"))
        else:
            results.append(PatchResult(name, "failed", applied.stderr.strip()))
    return results


def _git_apply(out: str, *args: str) -> subprocess.CompletedProcess:
    """Run `git apply` against a plain directory, outside any repository."""
    return subprocess.run(
        ["git", "apply", *args],
        cwd=out,
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# stage 4: overlay
# ---------------------------------------------------------------------------


def write_provenance(out: str, lock: dict) -> None:
    """Record which upstream revision this tree was generated from.

    The distribution version (``mini-geoips 0.1.0``) deliberately does not
    encode the upstream ref: PEP 440 local version segments such as
    ``1.20.0a0+mini.1`` cannot be uploaded to PyPI.  So the provenance lives
    here instead, where an installed copy can be asked directly.
    """
    path = os.path.join(out, "geoips", "_mini_ips.py")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(
            '"""Generated by prune/slim.py -- do not edit.\n\n'
            "Provenance for this mini-geoips build.\n"
            '"""\n\n'
            f"UPSTREAM_REPO = {lock['repo']!r}\n"
            f"UPSTREAM_REF = {lock['ref']!r}\n"
            f"UPSTREAM_SHA = {lock['sha']!r}\n"
            f"UPSTREAM_CHANNEL = {lock.get('channel', 'unknown')!r}\n"
        )


def overlay(out: str, overlay_dir: str) -> None:
    """Copy the fork-owned files over the pruned tree."""
    if not os.path.isdir(overlay_dir):
        return
    for dirpath, dirnames, filenames in os.walk(overlay_dir):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in filenames:
            src = os.path.join(dirpath, name)
            dst = os.path.join(out, os.path.relpath(src, overlay_dir))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------


def assert_budget_ceilings(closure: dict[str, set[str]]) -> None:
    """Fail the build if the resolved dependency surface exceeded its ceiling.

    Asserted here rather than in a separate benchmarking script, because this
    is the point where the closure is known and the build can still be stopped.
    The ceilings live beside the budget in overlay/pyproject.toml.
    """
    ceilings = depbudget.budget_ceilings(os.path.join(ROOT, "overlay/pyproject.toml"))
    for tier, limit in ceilings.items():
        count = len(closure.get(tier) or ())
        if count > limit:
            sys.exit(
                f"error: the '{tier}' dependency closure resolves to {count} "
                f"packages, over the ceiling of {limit} declared in "
                "overlay/pyproject.toml [tool.mini-ips].\n"
                "Either the change is wrong, or the ceiling should move -- if "
                "the latter, raise it in a commit that says why."
            )


def write_report(
    path: str,
    lock: dict,
    classification: Classification,
    patches: list[PatchResult],
    imports: dict | None = None,
) -> None:
    """Write the machine-readable build report.

    One shape from one place. Previously the exit-3 path wrote a report with no
    `patches` key, so an unclassified-path run rendered "patches 0/0 applied" in
    the sync PR -- exactly the run where a reviewer needs to know the patches
    were fine and only the manifest is behind.
    """
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "lock": {k: lock[k] for k in ("ref", "sha", "channel") if k in lock},
                "unclassified": classification.unclassified,
                "kept": sorted(classification.keep),
                "dropped": sorted(classification.drop),
                "plugins": [d.__dict__ for d in classification.plugin_decisions],
                "yaml_plugins": classification.yaml_reasons,
                "patches": [p.__dict__ for p in patches],
                "closure": {k: sorted(v) for k, v in classification.closure.items()},
                "imports": imports or {},
            },
            handle,
            indent=1,
        )


def summarise(
    classification: Classification, patches: list[PatchResult], out: str
) -> None:
    kept_py = [p for p in classification.keep if p.endswith(".py")]
    loc = 0
    for rel in kept_py:
        full = os.path.join(out, rel)
        if os.path.isfile(full):
            loc += sum(1 for _ in open(full, errors="replace"))
    plugins_kept = sum(
        1
        for d in classification.plugin_decisions
        if d.kept and not d.path.endswith("__init__.py")
    )
    plugins_dropped = sum(
        1
        for d in classification.plugin_decisions
        if not d.kept and not d.path.endswith("__init__.py")
    )
    yaml_kept = sum(1 for p in classification.yaml_reasons if p in classification.keep)
    yaml_dropped = sum(
        1 for p in classification.yaml_reasons if p not in classification.keep
    )
    print(
        f"\n  kept      {len(classification.keep):5d} files "
        f"({len(kept_py)} python, {loc} LOC)"
    )
    print(f"  dropped   {len(classification.drop):5d} files")
    print(f"  plugins   {plugins_kept:5d} kept / {plugins_dropped} excluded")
    print(f"  yaml plgs {yaml_kept:5d} kept / {yaml_dropped} excluded")
    print(
        f"  budget    {len(classification.closure['required']):5d} dists required / "
        f"{len(classification.closure['all'])} with extras"
        f"  (plugins judged against '{classification.tier}')"
    )
    for patch in patches:
        marker = {"applied": "ok", "noop": "NOOP", "failed": "FAIL"}[patch.status]
        print(
            f"  patch     {marker:>5s}  {patch.name}"
            + (f"  -- {patch.detail}" if patch.detail else "")
        )


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--lock", default=os.path.join(ROOT, "upstream.lock"))
    parser.add_argument("--manifest", default=os.path.join(ROOT, "prune/manifest.yaml"))
    parser.add_argument("--out", default=os.path.join(ROOT, "build"))
    parser.add_argument(
        "--upstream-path",
        default=(
            os.path.join(ROOT, UPSTREAM_CLONE)
            if os.path.isdir(os.path.join(ROOT, UPSTREAM_CLONE, ".git"))
            else None
        ),
        help=f"local geoips clone to export from instead of fetching "
        f"(defaults to ./{UPSTREAM_CLONE} when it exists)",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="stop after CLASSIFY; do not write build/",
    )
    parser.add_argument("--refresh-closure", action="store_true")
    parser.add_argument("--json", metavar="FILE", help="machine-readable report")
    args = parser.parse_args(argv)

    lock = read_lock(args.lock)
    manifest = depbudget.load_manifest(args.manifest)

    print(f"[1/5] RESOLVE  {lock['ref']} ({lock['sha'][:9]})")
    workdir = tempfile.mkdtemp(prefix="mini-ips-upstream-")
    try:
        resolve_upstream(lock, workdir, args.upstream_path)

        # Scan BEFORE patching. The baseline has to be pristine upstream:
        # patches 02 and 07 rewrite geoips.* imports themselves, so a patched
        # baseline would contain a mis-authored one too, the diff would cancel
        # and the gate would report nothing.
        baseline_findings = set(verify_closure.scan(workdir, "geoips"))

        patches = apply_patches(workdir, os.path.join(ROOT, "prune/patches"))
        failed = [p for p in patches if p.status == "failed"]
        for patch in patches:
            if patch.status == "failed":
                print(f"  patch FAILED  {patch.name}: {patch.detail}")
        if failed:
            print(f"\nFAIL: {len(failed)} patch(es) did not apply to {lock['ref']}")
            return 4

        print("[2/5] CLASSIFY")
        closure = depbudget.load_or_resolve(
            os.path.join(ROOT, "overlay/pyproject.toml"),
            os.path.join(ROOT, "prune/dependency-closure.lock"),
            args.refresh_closure,
        )
        assert_budget_ceilings(closure)
        classification = classify(workdir, manifest, closure)

        if classification.unclassified:
            print(
                f"\nFAIL: {len(classification.unclassified)} upstream path(s) that the "
                "manifest does not classify.\nAdd each to `keep:` or `deny:` in "
                "prune/manifest.yaml, then re-run:\n"
            )
            for path in classification.unclassified[:60]:
                print(f"  {path}")
            if len(classification.unclassified) > 60:
                print(f"  ... and {len(classification.unclassified) - 60} more")
            if args.json:
                write_report(args.json, lock, classification, patches)
            return 3

        if args.check_only:
            print("  all upstream paths classified")
            summarise(classification, patches, workdir)
            return 0

        print("[3/5] PRUNE")
        prune(workdir, args.out, classification)

        print("[4/5] OVERLAY")
        overlay(args.out, os.path.join(ROOT, "overlay"))
        write_provenance(args.out, lock)

        print("[5/5] VERIFY")
        found = set(verify_closure.scan(args.out, "geoips"))
        introduced = sorted(found - baseline_findings)
        inherited = sorted(found & baseline_findings)
        # A baseline finding absent from the output: the file was pruned, or a
        # patch removed the import. The two look identical from here, which is
        # the point -- once upstream fixes one itself, our patch for it is dead
        # weight, and this line is the only prompt to go and check.
        resolved = sorted(baseline_findings - found)

        summarise(classification, patches, args.out)
        print(
            f"  closure   {len(introduced)} introduced, {len(inherited)} inherited"
            f", {len(resolved)} not present here"
        )
        for finding in resolved:
            print(f"    gone       {finding}  (is a patch still needed?)")
        for finding in inherited:
            print(f"    inherited  {finding}")

        if args.json:
            write_report(
                args.json,
                lock,
                classification,
                patches,
                {
                    "introduced": [str(f) for f in introduced],
                    "inherited": [str(f) for f in inherited],
                    "resolved": [str(f) for f in resolved],
                },
            )

        if introduced:
            print(
                f"\nFAIL: pruning introduced {len(introduced)} unresolvable "
                "geoips.* import(s):"
            )
            for finding in introduced:
                print(f"  {finding}")
            return 5
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
