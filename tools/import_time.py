#!/usr/bin/env python3
"""Measure `import geoips` and show what dominates it.

All this tool ever uniquely measured. The dependency-count ceilings moved to
`[tool.mini-ips]` in overlay/pyproject.toml and are asserted by prune/slim.py at
the point where the closure is known and the build can still be stopped; the
file and LOC counts were a second, slightly disagreeing implementation of what
slim.py already prints.

Import time is worth watching on its own: the patches that make matplotlib,
cartopy, netCDF4 and zarr optional are only load-bearing while nothing imports
them at package-import time, and a regression there is invisible to every other
gate -- the build still succeeds, the tests still pass, the image just gets
slower to start.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time

# Best-of-3 wall seconds. Measured 0.49 s at upstream 1.20.0a0; the ceiling
# leaves room for a slower runner without hiding a re-eagered heavy import.
DEFAULT_CEILING = 3.0


def measure(python: str, runs: int = 3) -> float | None:
    """Best-of-N wall time for `import geoips`, or None if it cannot import."""
    best = None
    for _ in range(runs):
        started = time.perf_counter()
        proc = subprocess.run(
            [python, "-c", "import geoips"],
            capture_output=True,
            text=True,
            # Never from a directory with a `geoips` sibling: it would shadow
            # the installed package as a namespace package and the number would
            # be meaningless. (This is why the upstream clone is called
            # `upstream-geoips`.)
            cwd="/",
        )
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr)
            return None
        best = min(best or 1e9, time.perf_counter() - started)
    return best


def breakdown(python: str, threshold_us: int = 75_000) -> list[tuple[int, str]]:
    """Top-level modules whose cumulative import cost exceeds the threshold."""
    proc = subprocess.run(
        [python, "-X", "importtime", "-c", "import geoips"],
        capture_output=True,
        text=True,
        cwd="/",
    )
    rows = []
    for line in proc.stderr.splitlines():
        parts = line.split("|")
        if len(parts) != 3:
            continue
        try:
            cumulative = int(parts[1].strip())
        except ValueError:
            continue
        name = parts[2].strip()
        if cumulative >= threshold_us and "." not in name.lstrip("_"):
            rows.append((cumulative, name))
    return sorted(rows, reverse=True)[:10]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--ceiling", type=float, default=DEFAULT_CEILING)
    parser.add_argument("--assert-budget", action="store_true")
    args = parser.parse_args(argv)

    seconds = measure(args.python)
    if seconds is None:
        print("import geoips FAILED -- is it installed in --python?")
        return 1

    print(f"import geoips  {seconds:.2f}s (best of 3)")
    for cumulative, name in breakdown(args.python):
        print(f"  {cumulative / 1000:7.0f} ms  {name}")

    for heavy in ("matplotlib", "cartopy", "netCDF4", "zarr"):
        if any(name == heavy for _, name in breakdown(args.python)):
            print(f"\nWARNING: {heavy} is imported at package-import time.")
            print("It is meant to be optional; a patch that deferred it has regressed.")
            return 1

    if args.assert_budget and seconds > args.ceiling:
        print(f"\nBUDGET EXCEEDED: {seconds:.2f}s over the {args.ceiling}s ceiling")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
