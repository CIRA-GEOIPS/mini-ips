"""Glob matching for manifest path rules.

``pathlib.PurePath.full_match`` would do, but it only arrived in 3.13 and the
fork targets 3.11+, so translate to a regex instead.
"""

from __future__ import annotations

import re


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a manifest glob to a regex.

    ``**`` spans directory separators; ``*`` and ``?`` do not.
    """
    out = ["^"]
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif char == "*":
            out.append("[^/]*")
            i += 1
        elif char == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(char))
            i += 1
    out.append("$")
    return re.compile("".join(out))


class Matcher:
    """An ordered set of globs; ``match`` returns the pattern that hit."""

    def __init__(self, patterns: list[str]):
        self.patterns = list(patterns)
        self._compiled = [(p, glob_to_regex(p)) for p in self.patterns]

    def match(self, path: str) -> str | None:
        """Return the first pattern matching ``path``, or None."""
        for pattern, regex in self._compiled:
            if regex.match(path):
                return pattern
            # `foo/**` should also match `foo` itself.
            if pattern.endswith("/**") and path == pattern[:-3]:
                return pattern
        return None

    def __bool__(self) -> bool:
        return bool(self._compiled)
