# # # This source code is subject to the license referenced at
# # # https://github.com/NRLMMD-GEOIPS.

"""Workflow argument handling, ported from upstream's CLI.

``resolve_workflow`` is upstream ``GeoipsWorkflowCommand.workflow_type``
(``geoips/commandline/geoips_command.py:799-901``) and ``apply_overrides`` is
upstream ``GeoipsRunOrderBased._apply_overrides``
(``geoips/commandline/geoips_run.py:345-400``), kept behaviourally identical so
that the same invocations produce the same workflows. Only the error reporting
differs: these raise :class:`WorkflowArgError` instead of calling
``argparse.ArgumentParser.error``, so the same code is usable outside argparse.

The three accepted forms and the ``_expand=True`` on the registered-name path
are load-bearing: overrides are applied to an already-expanded workflow, so
expansion has to happen during the cast rather than later.
"""

from __future__ import annotations

import json
import os
from ast import literal_eval
from collections.abc import Mapping
from pathlib import Path

import yaml


class WorkflowArgError(Exception):
    """A workflow argument could not be resolved or overridden."""


def _load_path(value: str) -> Path | None:
    """Return the path if ``value`` names an existing .json/.yaml/.yml file."""
    path = Path(os.path.abspath(os.path.expanduser(value)))
    if path.is_file() and path.suffix.lower() in (".json", ".yaml", ".yml"):
        return path
    return None


def resolve_workflow(value: str | Mapping | None):
    """Cast a CLI workflow argument to an expanded workflow dict.

    Accepts, in this order: an inline dict (or a string that ``literal_eval``s
    to one), a path to a .json/.yaml/.yml file, or the name of a registered
    workflow plugin.
    """
    from pydantic import ValidationError

    from geoips.errors import PluginError
    from geoips.filenames.base_paths import PATHS
    from geoips.interfaces import workflows
    from geoips.pydantic_models.v1.workflows import WorkflowPluginModel

    # -- inline dict, or a string that evaluates to one ---------------------
    if isinstance(value, Mapping):
        candidate = value
    else:
        try:
            candidate = literal_eval(value)
        except (ValueError, SyntaxError, TypeError):
            candidate = None

    if isinstance(candidate, Mapping):
        try:
            # `context={"expand": True}` tells pydantic to expand the workflow
            # rather than validate only what was supplied; overrides are applied
            # to the expanded form.
            return WorkflowPluginModel(
                **candidate, is_registered=False, context={"expand": True}
            ).model_dump()
        except (ValidationError, ValueError, TypeError) as exc:
            raise WorkflowArgError(f"could not parse workflow dict: {exc}") from exc

    # -- a file on disk ------------------------------------------------------
    if isinstance(value, str):
        path = _load_path(value)
        if path is not None:
            loader = json.load if path.suffix.lower() == ".json" else yaml.safe_load
            with open(path, encoding="utf-8") as handle:
                data = loader(handle)
            try:
                return WorkflowPluginModel(
                    **data, is_registered=False, context={"expand": True}
                ).model_dump()
            except (ValidationError, ValueError, TypeError) as exc:
                raise WorkflowArgError(
                    f"could not parse workflow file '{value}': {exc}"
                ) from exc

    # -- a registered plugin name -------------------------------------------
    if isinstance(value, str):
        # Rebuilding the registry on a miss is gated on the user's own
        # GEOIPS_REBUILD_REGISTRIES setting, so a stale registry is only
        # refreshed when they opted in.
        rebuild = PATHS.get("GEOIPS_REBUILD_REGISTRIES", False)
        try:
            return workflows.get_plugin(value, _expand=True, rebuild_registries=rebuild)
        except PluginError as exc:
            raise WorkflowArgError(
                f"no workflow plugin named '{value}'. Is its package installed and "
                "are the registries built? Try: geoips config create-registries"
            ) from exc
        except ValidationError as exc:
            raise WorkflowArgError(
                f"workflow '{value}' failed validation:\n{exc}"
            ) from exc

    raise WorkflowArgError(
        f"workflow argument {value!r} is not a dict, an existing .json/.yaml file, "
        "or the name of a registered workflow plugin"
    )


def dict_override(value: str):
    """Parse a -S/-K/-G dictionary override."""
    try:
        return yaml.safe_load(value)
    except Exception as exc:  # noqa: BLE001 -- yaml raises a wide range
        raise WorkflowArgError(f"invalid dictionary override: {value}") from exc


def apply_overrides(workflow, args):
    """Apply the -S/-K/-G and -s/-k/-g overrides to an expanded workflow."""
    from geoips.interfaces import workflows
    from geoips.pydantic_models.v1.workflows import WorkflowPluginModel

    if any(
        [args.step_override_dict, args.kind_override_dict, args.global_override_dict]
    ):
        workflow = workflows._override_workflow_dict_format(
            workflow,
            goverrides=args.global_override_dict,
            koverrides=args.kind_override_dict,
            soverrides=args.step_override_dict,
        )
        # Re-validate after each override pass, as upstream does: an override
        # can produce a structurally invalid workflow and it should fail here
        # rather than midway through execution.
        WorkflowPluginModel(**workflow, is_registered=False)

    if any(
        [
            args.step_override_strings,
            args.kind_override_strings,
            args.global_override_strings,
        ]
    ):
        workflow = workflows._override_workflow_string_format(
            workflow,
            goverrides=args.global_override_strings,
            koverrides=args.kind_override_strings,
            soverrides=args.step_override_strings,
        )
        WorkflowPluginModel(**workflow, is_registered=False)

    return workflow


def add_override_arguments(parser) -> None:
    """Add the six override flags, matching upstream's names and semantics."""
    parser.add_argument(
        "-S",
        "--step-override-dict",
        type=dict_override,
        default=None,
        metavar="DICT",
        help=(
            "per-step overrides as a dict, e.g. "
            "\"{'reader': {'variables': ['B14BT']}}\""
        ),
    )
    parser.add_argument(
        "-K",
        "--kind-override-dict",
        type=dict_override,
        default=None,
        metavar="DICT",
        help="per-kind overrides as a dict, applied to every step of that kind",
    )
    parser.add_argument(
        "-G",
        "--global-override-dict",
        type=dict_override,
        default=None,
        metavar="DICT",
        help="workflow-global overrides as a dict",
    )
    parser.add_argument(
        "-s",
        "--step-override-strings",
        action="append",
        default=None,
        metavar="STR",
        help="per-step override, '<step>.<arg>=<value>'; repeatable",
    )
    parser.add_argument(
        "-k",
        "--kind-override-strings",
        action="append",
        default=None,
        metavar="STR",
        help="per-kind override, '<kind>.<arg>=<value>'; repeatable",
    )
    parser.add_argument(
        "-g",
        "--global-override-strings",
        action="append",
        default=None,
        metavar="STR",
        help="global override, '<name>=<value>'; repeatable",
    )
