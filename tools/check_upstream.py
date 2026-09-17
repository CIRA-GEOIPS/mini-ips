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


def newest(tags: dict[str, str], channel: str, behind_pin=None):
    """Newest usable (tag, sha, Version, skipped) for the channel, or None.

    Upstream's tags are not monotonic with history: `2.0.0a0` sorts above
    `1.20.0a0` but its commit is an ANCESTOR of it. Taking the highest version
    and only then checking ancestry made the watcher refuse on every run and
    never sync -- it would have filed a refusal issue nightly, forever.

    So walk candidates in descending version order and take the first that is
    not already behind the pin. `skipped` carries the ones passed over, which
    the report prints: silently ignoring a higher tag is the kind of thing a
    human needs told.
    """
    candidates = []
    for tag, sha in tags.items():
        version = parse(tag)
        if version is None:
            continue
        if channel == "stable" and version.is_prerelease:
            continue
        candidates.append((version, tag, sha))

    skipped: list[str] = []
    for version, tag, sha in sorted(candidates, reverse=True):
        if behind_pin is not None and behind_pin(sha):
            skipped.append(tag)
            continue
        return tag, sha, version, skipped
    return None


def behind_pin_check(repo_path: str | None, pinned_sha: str):
    """Build a predicate: is this commit already an ancestor of the pin.

    Returns None when there is no local clone to ask, in which case selection
    cannot skip ancestors and `verdict` falls back to refusing.
    """
    if not repo_path or not os.path.isdir(repo_path):
        return None

    def behind(sha: str) -> bool:
        # The pinned commit is its own ancestor, so compare first: without this
        # the currently pinned tag skips itself, every candidate is rejected,
        # and the watcher reports "no usable tag" instead of "already current".
        if sha == pinned_sha:
            return False
        return (
            subprocess.run(
                # fmt: off
                ["git", "-C", repo_path, "merge-base",
                 "--is-ancestor", sha, pinned_sha],
                # fmt: on
                capture_output=True,
            ).returncode
            == 0
        )

    return behind


def verdict(lock: dict, channel: str, tag: str, sha: str, version, checked_ancestry):
    """(exit code, message) for the candidate tag. No side effects."""
    current_ref = str(lock["ref"])
    current = parse(current_ref)

    # Without a local clone, ancestry could not be checked during selection, so
    # a higher-sorting ancestor may still be sitting here. Refuse rather than
    # walk the pin backwards through history and silently drop months of work.
    if not checked_ancestry and current is not None and version > current:
        return 3, (
            f"REFUSING: {tag} sorts newer than {current_ref}, but with no local "
            "clone (--repo-path) its ancestry cannot be checked, and upstream's "
            "tags are not monotonic with history. Re-run with --repo-path."
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
    behind_pin = behind_pin_check(args.repo_path, lock["sha"])
    found = newest(tags, channel, behind_pin)
    if found is None:
        sys.exit(
            f"error: no usable {channel} tag in {lock['repo']} "
            "(every candidate is already an ancestor of the pinned commit)"
        )
    tag, sha, version, skipped = found

    code, message = verdict(
        lock, channel, tag, sha, version, checked_ancestry=behind_pin is not None
    )
    print(message)
    if skipped:
        print(
            f"  skipped {len(skipped)} higher-sorting tag(s) already behind the "
            f"pin: {', '.join(skipped)}"
        )

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "channel": channel,
                    "current_ref": str(lock["ref"]),
                    "current_sha": lock["sha"],
                    "latest_ref": tag,
                    "latest_sha": sha,
                    "skipped_behind_pin": skipped,
                    "refused": message if code == 3 else None,
                },
                handle,
                indent=1,
            )
    return code


if __name__ == "__main__":
    sys.exit(main())
