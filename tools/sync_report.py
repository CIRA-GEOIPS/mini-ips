#!/usr/bin/env python3
"""Render the upstream-sync PR body from a regeneration run.

Status first, then what needs a human, then the derived deltas -- a reviewer
should see in one screen whether the sync is mergeable and, if not, what blocks
it.

**No verdict is grepped out of a log.** An earlier version derived each one that
way, which was both a second source of truth and a wrong one: a pytest log
reading "3 failed, 1270 passed" contains "passed", so a failing run rendered as
PASS. Logs are uploaded as workflow artifacts instead of summarised here.

The first three stages take their verdict from ``slim.json`` -- not as a guess,
but by re-applying the same predicate ``slim.py`` failed on (any unclassified
path, any failed patch, any newly dangling import). The stages that run outside
slim.py have no such artifact, so those pass their exit code in as
``--status stage=N``.

Reads ``slim.json`` and ``upstream.json`` if present, so it still says something
useful when a stage died before writing one.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Run order, not alphabetical: the first FAIL is the one that caused the rest.
# The first three are decided by slim.json, the rest by --status.
SLIM_STAGES = ("classify", "patches", "closure")
EXIT_STAGES = ("registry", "tests", "import")
STAGES = SLIM_STAGES + EXIT_STAGES


def load_json(path: str) -> dict:
    """Parse a JSON file, or return {} if it is missing or truncated."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError:
        return {}


def tail(path: str) -> str:
    """Last non-empty line of a log -- used as table detail, never as verdict."""
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8", errors="replace") as handle:
        lines = [line.strip() for line in handle if line.strip()]
    return lines[-1] if lines else ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", default="pr-body.md")
    parser.add_argument("--dir", default=".")
    parser.add_argument(
        "--status",
        action="append",
        default=[],
        metavar="STAGE=CODE",
        help="exit code for a stage, e.g. --status tests=1. Repeatable. "
        "A stage given no --status renders as 'not reached'.",
    )
    args = parser.parse_args(argv)

    status = dict(item.split("=", 1) for item in args.status)
    unknown = set(status) - set(EXIT_STAGES)
    if unknown:
        parser.error(
            f"unknown stage(s): {', '.join(sorted(unknown))}. "
            f"--status accepts only {', '.join(EXIT_STAGES)}; "
            f"{', '.join(SLIM_STAGES)} are read from slim.json."
        )

    here = args.dir
    upstream = load_json(os.path.join(here, "upstream.json"))
    slim = load_json(os.path.join(here, "slim.json"))
    ref = upstream.get("latest_ref", "?")
    patches = slim.get("patches") or []
    unclassified = slim.get("unclassified") or []
    introduced = (slim.get("imports") or {}).get("introduced", [])

    counted: dict[str, int] = {}
    for patch in patches:
        counted[patch["status"]] = counted.get(patch["status"], 0) + 1

    # None means "slim.py did not get this far", which is not the same as a
    # pass. Exit 3 (unclassified) returns before the closure gate ever runs, so
    # `imports` is absent from the report and closure reads "not reached".
    reached = {
        "classify": bool(slim),
        "patches": bool(patches),
        "closure": bool(slim.get("imports")),
    }
    failed = {
        "classify": bool(unclassified),
        "patches": any(p["status"] == "failed" for p in patches),
        "closure": bool(introduced),
    }

    detail = {
        "classify": f"{len(unclassified)} unclassified path(s)",
        "patches": ", ".join(f"{n} {k}" for k, n in sorted(counted.items())),
        "closure": f"{len(introduced)} new dangling import(s)",
        "registry": tail(os.path.join(here, "registry.log")),
        "tests": tail(os.path.join(here, "tests.log")),
        "import": tail(os.path.join(here, "import.log")),
    }

    out = [
        f"## Upstream release `{ref}` (was `{upstream.get('current_ref', '?')}`)",
        f"`{str(upstream.get('current_sha', ''))[:9]}` -> "
        f"`{str(upstream.get('latest_sha', ''))[:9]}`",
        "",
        "| stage | result | detail |",
        "|---|---|---|",
    ]
    for stage in STAGES:
        if stage in SLIM_STAGES:
            ok = None if not reached[stage] else not failed[stage]
        else:
            code = status.get(stage)
            ok = None if code is None else code == "0"
        # A stage that never ran shows no detail: a leftover log from an
        # earlier run in the same workspace would otherwise sit beside
        # "not reached" and read as a result.
        verdict = "not reached" if ok is None else ("PASS" if ok else "FAIL")
        shown = "—" if ok is None else (detail[stage] or "—")
        out.append(f"| {stage} | {verdict} | {shown} |")

    # The checklist. This is what makes the PR a start rather than a ping.
    decisions = []
    for item in unclassified[:40]:
        decisions.append(
            f"- [ ] `{item}` — new upstream path, unclassified. "
            "Add it to `keep:` or `deny:` in `prune/manifest.yaml`."
        )
    if len(unclassified) > 40:
        decisions.append(f"- [ ] ... and {len(unclassified) - 40} more, see `slim.log`")
    for item in introduced:
        decisions.append(
            f"- [ ] `{item}` — pruning broke this import. Widen `keep:` or patch it."
        )
    for patch in patches:
        # slim.py emits exactly three statuses: applied, noop, failed. Only two
        # of them need a human.
        note = {
            "failed": f"**did not apply**. Rebase it against `{ref}`:\n"
            f"      ```\n      {patch.get('detail', '')}\n      ```",
            "noop": "is now a **no-op**: upstream fixed what it worked around. "
            "Delete it — this is how the patch set shrinks.",
        }.get(patch["status"])
        if note:
            decisions.append(f"- [ ] patch `{patch['name']}` {note}")
    if decisions:
        out += ["", "### Needs a decision"] + decisions

    # Why each plugin was excluded. Recomputed every sync, never hand-edited.
    plugins = [
        p for p in (slim.get("plugins") or []) if not p["path"].endswith("__init__.py")
    ]
    if plugins:
        dropped = [p for p in plugins if not p["kept"]]
        by_reason: dict[str, list[str]] = {}
        for item in dropped:
            short = item["path"].replace("geoips/plugins/classes/", "")
            by_reason.setdefault(item["reason"], []).append(short)
        out += [
            "",
            f"### Derived plugin set: {len(plugins) - len(dropped)} kept, "
            f"{len(dropped)} excluded",
            "Recomputed from the dependency budget, so a plugin whose imports "
            "changed needs no manifest edit. Exclusions, with cause:",
            "",
        ]
        for reason, paths in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
            out.append(f"- **{reason}** — {', '.join(sorted(paths))}")

    out += ["", "Full logs are attached to the workflow run as `sync-logs`."]
    body = "\n".join(out) + "\n"
    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write(body)
    print(f"wrote {args.out} ({len(body)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
