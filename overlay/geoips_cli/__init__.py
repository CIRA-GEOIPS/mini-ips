# # # This source code is subject to the license referenced at
# # # https://github.com/NRLMMD-GEOIPS.

"""Minimal command line interface for the mini-ips order-based procflow.

Fork-owned, deliberately. Upstream's CLI framework is ~2,000 lines plus a 28 KB
``cmd_instructions.yaml`` whose missing keys are fatal, and
``commandline_interface.py`` eagerly imports all eight command modules -- so
``geoips run order_based`` currently pays for ``requests``, ``rich``,
``tabulate`` and ``pygments`` on every invocation. Trimming that would mean
patching three actively-developed files on every upstream sync; this is ~250
lines with no patch surface at all.

Command names and flags match upstream so its documentation still applies.
"""

__all__ = ["main"]
