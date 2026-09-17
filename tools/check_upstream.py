#!/usr/bin/env python3
"""Detect a newer upstream geoips release than the one `upstream.lock` pins.

Keyed off git tags rather than PyPI or the GitHub Releases API. Upstream's
release flow is: a version-release PR bumps `.github/versions/tagged_version`,
then their CI creates the tag and only afterwards publishes a Release and a
wheel. The tag is therefore the earliest reliable signal, and it carries the
commit SHA directly, which is what `slim.py` actually needs.

Version comparison uses `packaging.version`, never a string or `sort -V`:
upstream ships pre-releases (`2.0.0a0`, `1.20.0a0`) and the order-based procflow
is the 2.0 line, so `1.9.0` vs `1.20.0a0` has to compare correctly.

Release notes are deliberately NOT looked up. An earlier version called
`gh api .../releases/tags/<tag>` to fill a `release_notes_url` and a
`has_github_release` tri-state, which cost a network round trip, a `gh`
dependency and three error branches -- to produce a URL that is
`<repo>/releases/tag/<tag>` by construction, for a PR body that no longer
prints it. The tag is authoritative regardless: it is what carries the SHA, and
upstream's CI creates it before publishing the Release.

Exit codes: 0 a newer release exists, 1 already current, 2 error, 3 refusing to
move the pin (a retag, a moved tag, or the ancestry trap below).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

TAG_REF = re.compile(r"^([0-9a-f]{40})\srefs/tags/(.+)$")


def read_lock(path: str) -> dict:
    import yaml

    with open(path, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def list_remote_tags(repo: str) -> dict[str, str]:
    """Return {tag: sha} for the remote, without cloning it."""
    proc = subprocess.run(
        ["git", "ls-remote", "--tags", "--refs", repo],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        sys.exit(f"error: git ls-remote failed:\n{proc.stderr.strip()}")
    tags: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        match = TAG_REF.match(line.strip().replace("\t", " "))
        if match:
            tags[match.group(2)] = match.group(1)
    return tags


def parse(tag: str):
    from packaging.version import InvalidVersion, Version

    try:
        return Version(tag)
    except InvalidVersion:
        return None


def newest(tags: dict[str, str], channel: str):
    """Newest (tag, sha, Version) for the channel, or None."""
    candidates = []
    for tag, sha in tags.items():
        version = parse(tag)
        if version is None:
            continue
        if channel == "stable" and version.is_prerelease:
            continue
        candidates.append((version, tag, sha))
    if not candidates:
        return None
    version, tag, sha = max(candidates)
    return tag, sha, version


def verdict(lock: dict, channel: str, tag: str, sha: str, version, repo_path):
    """(exit code, message) for the candidate tag. No side effects."""
    current_ref = str(lock["ref"])
    current = parse(current_ref)

    # Version order is not history order in this repository. Upstream tagged
    # `2.0.0a0` at 2d3d2aa7d and then carried on to `1.20.0a0` at 751e6533e, so
    # 2.0.0a0 sorts NEWER while being an ANCESTOR. Trusting the version alone
    # would walk the pin backwards through history and silently drop months of
    # commits, which is much worse than not syncing.
    if repo_path and os.path.isdir(repo_path):
        ancestry = subprocess.run(
            ["git", "-C", repo_path, "merge-base", "--is-ancestor", sha, lock["sha"]],
            capture_output=True,
        )
        if ancestry.returncode == 0:
            return 3, (
                f"REFUSING: {tag} ({sha[:9]}) sorts newer than {current_ref} but is "
                f"an ANCESTOR of the pinned {str(lock['sha'])[:9]} -- upstream's tag "
                "numbering is not monotonic with history. Moving the pin here would "
                "go backwards. Pick the ref by hand."
            )

    if current is not None and version < current:
        return 3, (
            f"REFUSING: newest {channel} tag {tag} is OLDER than the pinned "
            f"{current_ref}. Upstream may have retagged or deleted a tag. "
            "Resolve by hand rather than moving the pin backwards."
        )

    if tag == current_ref:
        if sha == lock["sha"]:
            return 1, f"current: {current_ref} ({sha[:9]})"
        return 3, (
            f"REFUSING: tag {tag} now points at {sha[:9]} but the lock pins "
            f"{str(lock['sha'])[:9]}. A moved tag is not a new release; "
            "confirm upstream's intent before changing the pin."
        )

    return 0, f"new {channel} release: {current_ref} -> {tag} ({sha[:9]})"


def main(argv: list[str] | None = None) -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--lock", default=os.path.join(root, "upstream.lock"))
    parser.add_argument("--json", metavar="FILE")
    parser.add_argument(
        "--repo-path",
        default=(
            os.path.join(root, "upstream-geoips")
            if os.path.isdir(os.path.join(root, "upstream-geoips", ".git"))
            else None
        ),
        help="local clone used to check that the candidate is not an ancestor "
        "of the pinned commit (upstream's tags are not monotonic)",
    )
    args = parser.parse_args(argv)

    lock = read_lock(args.lock)
    channel = lock.get("channel", "prerelease")
    if channel == "main":
        print("channel is 'main'; release watching does not apply")
        return 1

    tags = list_remote_tags(lock["repo"])
    found = newest(tags, channel)
    if found is None:
        sys.exit(f"error: no {channel} tags found in {lock['repo']}")
    tag, sha, version = found

    code, message = verdict(lock, channel, tag, sha, version, args.repo_path)
    print(message)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "channel": channel,
                    "current_ref": str(lock["ref"]),
                    "current_sha": lock["sha"],
                    "latest_ref": tag,
                    "latest_sha": sha,
                    "refused": message if code == 3 else None,
                },
                handle,
                indent=1,
            )
    return code


if __name__ == "__main__":
    sys.exit(main())
