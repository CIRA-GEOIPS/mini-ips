# # # This source code is subject to the license referenced at
# # # https://github.com/NRLMMD-GEOIPS.

"""The mini-ips command line entry point.

    geoips run order_based <workflow> <files...>   (aliases: ob, obp)
    geoips config create-registries [-p PKG]
    geoips list {plugins,workflows,packages,interfaces}
    geoips expand <workflow>

Every ``geoips.*`` import is deferred into the handler that needs it. Importing
``geoips`` costs a few hundred milliseconds -- ``geoips.interfaces`` eagerly
loads all 21 interfaces -- and ``--help`` should not pay for it.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from geoips_cli.workflow_arg import (
    WorkflowArgError,
    add_override_arguments,
    apply_overrides,
    resolve_workflow,
)

LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def cmd_run_order_based(args) -> int:
    """Execute an order-based workflow."""
    from geoips.interfaces import procflows

    try:
        workflow = resolve_workflow(args.workflow)
        workflow = apply_overrides(workflow, args)
    except WorkflowArgError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2

    procflow = procflows.get_plugin("order_based")
    procflow(workflow_spec=workflow, filenames=args.filenames, command_line_args=args)
    return 0


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def cmd_config_create_registries(args) -> int:
    """Build the plugin registry for each installed plugin package."""
    from pluginify.plugin_registry import PluginRegistry

    registry = PluginRegistry(args.namespace)
    registry.create_registries(args.packages, args.save_type)
    return 0


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def _installed_packages() -> list[str]:
    from importlib import metadata

    return sorted(
        {ep.value for ep in metadata.entry_points(group="geoips.plugin_packages")}
    )


def cmd_list(args) -> int:
    """Print what is registered, for debugging external plugin packages."""
    # `packages` is answered from entry points alone, so return before importing
    # geoips: `geoips.interfaces` loads all 21 interfaces and costs ~1.2 s.
    if args.what == "packages":
        for package in _installed_packages():
            print(package)
        return 0

    from geoips import interfaces

    if args.what == "interfaces":
        available = interfaces.list_available_interfaces()
        for kind, names in available.items():
            for name in names:
                print(f"{kind:12s} {name}")
        return 0

    if args.what == "workflows":
        rows = _plugin_rows(interfaces, ["workflows"])
    else:  # "plugins"
        every = interfaces.list_available_interfaces()
        rows = _plugin_rows(interfaces, [n for names in every.values() for n in names])

    if not rows:
        print(
            "no plugins registered. Install a plugin package and run "
            "`geoips config create-registries`."
        )
        return 1
    width = max(len(row[0]) for row in rows)
    for interface_name, plugin_name, package in sorted(rows):
        print(f"{interface_name:{width}s}  {plugin_name:34s}  {package}")
    return 0


def _plugin_rows(interfaces, interface_names: list[str]) -> list[tuple[str, str, str]]:
    """Collect (interface, plugin, package) triples from the registry.

    An interface with no plugins is normal here -- mini-ips derives its built-in
    set from a dependency budget and expects external packages to supply the
    rest -- so a missing registry entry is skipped rather than raised.
    """
    rows: list[tuple[str, str, str]] = []
    for name in interface_names:
        interface = getattr(interfaces, name, None)
        if interface is None:
            continue
        try:
            registered = interface.plugin_registry.registered_plugins
            entries = registered.get(interface.interface_type, {}).get(name, {})
        except Exception as exc:  # noqa: BLE001 -- registry may be absent entirely
            LOG.debug("skipping %s: %s", name, exc)
            continue
        for plugin_name, meta in (entries or {}).items():
            package = meta.get("package", "?") if isinstance(meta, dict) else "?"
            rows.append((name, str(plugin_name), package))
    return rows


# ---------------------------------------------------------------------------
# expand / validate
# ---------------------------------------------------------------------------


def cmd_expand(args) -> int:
    """Dump a fully expanded workflow as YAML.

    The single most useful thing when a workflow does not do what you expect:
    it shows the steps, arguments and defaults the OBP will actually run.
    """
    import json

    import yaml

    try:
        workflow = resolve_workflow(args.workflow)
    except WorkflowArgError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2
    # An expanded workflow carries datetimes (window_start_time) and other
    # non-primitive values that `yaml.safe_dump` refuses to represent. Round-trip
    # through JSON with `default=str` first: this is a human-readable dump, so
    # rendering those as strings is right, and it keeps the command from
    # crashing on workflows that merely use a timestamp.
    plain = json.loads(json.dumps(workflow, default=str))
    yaml.safe_dump(plain, sys.stdout, sort_keys=False, default_flow_style=False)
    return 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build the full command tree."""
    parser = argparse.ArgumentParser(
        prog="geoips",
        description=(
            "mini-geoips: the GeoIPS order-based procflow, for external plugin "
            "packages."
        ),
    )
    parser.add_argument(
        "--log-level",
        "-log",
        default="interactive",
        choices=["debug", "info", "interactive", "warning", "error", "critical"],
        help="logging verbosity",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # -- run ---------------------------------------------------------------
    run = sub.add_parser("run", help="run a processing workflow").add_subparsers(
        dest="procflow", required=True
    )
    order_based = run.add_parser(
        "order_based",
        aliases=["ob", "obp"],
        help="run the order-based procflow (OBP)",
    )
    order_based.add_argument(
        "workflow",
        help="a registered workflow name, a path to a .yaml/.json workflow, "
        "or an inline dict",
    )
    order_based.add_argument(
        "filenames",
        nargs="+",
        type=os.path.abspath,
        help="input data files (or a glob the shell has expanded)",
    )
    add_override_arguments(order_based)
    order_based.set_defaults(handler=cmd_run_order_based)

    # -- config ------------------------------------------------------------
    config = sub.add_parser("config", aliases=["cfg"], help="registry configuration")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    for name, aliases, handler in (
        ("create-registries", ["crt-reg"], cmd_config_create_registries),
    ):
        cmd = config_sub.add_parser(name, aliases=aliases, help=name.replace("-", " "))
        cmd.add_argument(
            "-p",
            "--packages",
            nargs="*",
            default=None,
            help="limit to these plugin packages (default: all installed)",
        )
        cmd.add_argument("-n", "--namespace", default="geoips.plugin_packages")
        cmd.add_argument("-s", "--save-type", default="json", choices=["json", "yaml"])
        cmd.set_defaults(handler=handler)

    # -- list --------------------------------------------------------------
    listing = sub.add_parser("list", aliases=["ls"], help="list what is registered")
    listing.add_argument(
        "what",
        nargs="?",
        default="plugins",
        choices=["plugins", "workflows", "packages", "interfaces"],
    )
    listing.add_argument("-n", "--namespace", default="geoips.plugin_packages")
    listing.set_defaults(handler=cmd_list)

    # -- expand ------------------------------------------------------------
    expand = sub.add_parser(
        "expand", aliases=["exp"], help="print a workflow with defaults filled in"
    )
    expand.add_argument("workflow")
    expand.set_defaults(handler=cmd_expand)

    # -- validate ----------------------------------------------------------
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, set up logging, and dispatch to the handler."""
    parser = build_parser()
    args = parser.parse_args(argv)

    from geoips.commandline.log_setup import setup_logging

    setup_logging(logging_level=args.log_level)

    try:
        return args.handler(args) or 0
    except KeyboardInterrupt:
        sys.stderr.write("interrupted\n")
        return 130


if __name__ == "__main__":
    sys.exit(main())
