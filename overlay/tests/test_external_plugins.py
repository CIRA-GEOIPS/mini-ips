# # # This source code is subject to the license referenced at
# # # https://github.com/NRLMMD-GEOIPS.

"""The contract mini-ips exists to support: plugins from an external package.

Everything else in this suite tests the runtime with synthetic in-process
doubles. This is the only place that exercises the real path end to end --
entry-point discovery, registry creation, plugin resolution by name, family
conversion, and DataTree assembly -- against a package that lives *outside*
geoips. If this passes, "the order-based procflow runs on external plugins" is
a measured fact rather than an assumption.

Requires `tests/fixture_plugin_pkg` to be installed and the registries built:

    pip install -e tests/fixture_plugin_pkg
    geoips config create-registries

Tests skip (rather than fail) when it is absent, so the suite stays usable
without it, and `test_fixture_package_is_installed` states plainly whether the
contract was actually covered.
"""

import numpy as np
import pytest

FIXTURE_PACKAGE = "mini_ips_fixture_plugins"
FIXTURE_WORKFLOW = "fixture_workflow"
SCALE_FACTOR = 3.0  # must match fixture_workflow.yaml


def _fixture_package_installed() -> bool:
    from importlib import metadata

    return FIXTURE_PACKAGE in {
        ep.value for ep in metadata.entry_points(group="geoips.plugin_packages")
    }


needs_fixture = pytest.mark.skipif(
    not _fixture_package_installed(),
    reason=f"{FIXTURE_PACKAGE} is not installed; see this module's docstring",
)


def test_fixture_package_is_installed():
    """State whether the external-plugin contract is being covered at all.

    Not an xfail: a green suite that silently skipped the only external-plugin
    coverage is worse than a visible warning, so this reports rather than
    passes quietly.
    """
    if not _fixture_package_installed():
        pytest.skip(
            "external-plugin coverage was NOT exercised: install "
            "tests/fixture_plugin_pkg and run `geoips config create-registries`"
        )
    assert _fixture_package_installed()


@needs_fixture
def test_external_plugins_are_registered():
    """The three fixture plugins resolve, and report the external package."""
    from geoips import interfaces

    expected = {
        "readers": "fixture_reader",
        "algorithms": "fixture_scale",
        "workflows": FIXTURE_WORKFLOW,
    }
    for interface_name, plugin_name in expected.items():
        interface = getattr(interfaces, interface_name)
        registered = interface.plugin_registry.registered_plugins
        entries = registered.get(interface.interface_type, {}).get(interface_name, {})
        assert plugin_name in entries, (
            f"{plugin_name} not registered under {interface_name}. Run "
            "`geoips config create-registries`."
        )
        assert entries[plugin_name]["package"] == FIXTURE_PACKAGE


@needs_fixture
def test_external_plugin_classes_resolve():
    """`get_plugin` loads the class out of the external package."""
    from geoips import interfaces

    reader = interfaces.readers.get_plugin("fixture_reader")
    assert reader.name == "fixture_reader"
    assert reader.__class__.__module__.startswith(FIXTURE_PACKAGE)

    algorithm = interfaces.algorithms.get_plugin("fixture_scale")
    assert algorithm.family == "list_numpy_to_numpy"


@needs_fixture
def test_order_based_procflow_runs_on_external_plugins(tmp_path):
    """Run a workflow whose every step is an external plugin.

    Asserts on the values, not just on completion: `fixture_reader` emits
    `arange(20)` and the workflow scales by 3.0, so an exact comparison proves
    the family conversion (Dataset -> list[ndarray] -> Dataset) ran as well as
    the plugins themselves.
    """
    from geoips.interfaces import procflows, workflows

    workflow = workflows.get_plugin(FIXTURE_WORKFLOW, _expand=True)
    # fixture_reader fabricates its data, so this path need not exist -- which
    # is what keeps the test free of test-data downloads.
    result = procflows.get_plugin("order_based")(
        workflow_spec=workflow, filenames=[str(tmp_path / "unused.dat")]
    )

    groups = set(result.groups)
    assert "/reader" in groups
    assert "/algorithm" in groups

    dataset = result["/algorithm"].to_dataset()
    variables = list(dataset.data_vars)
    assert len(variables) == 1, f"expected one output variable, got {variables}"
    values = np.asarray(dataset[variables[0]].values).ravel()
    expected = np.arange(20, dtype="float32") * SCALE_FACTOR
    np.testing.assert_allclose(values, expected)


@needs_fixture
def test_external_workflow_expands_without_builtin_plugins():
    """The workflow validates against the registry with no built-in step.

    `_expand=True` runs the same pydantic validation the CLI does, including
    the plugin-name check against the registry, so this catches an external
    workflow that names something unregistered.
    """
    from geoips.interfaces import workflows

    expanded = workflows.get_plugin(FIXTURE_WORKFLOW, _expand=True)
    steps = expanded["spec"]["steps"]
    assert {"reader", "algorithm"} <= set(steps)
    assert steps["reader"]["name"] == "fixture_reader"
    assert steps["algorithm"]["arguments"]["factor"] == SCALE_FACTOR
