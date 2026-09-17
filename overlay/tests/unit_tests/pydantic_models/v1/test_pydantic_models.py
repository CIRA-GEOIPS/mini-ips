# # # This source code is subject to the license referenced at
# # # https://github.com/NRLMMD-GEOIPS.

"""Testing module for Pydantic PluginModels."""

# Python Standard Libraries
from copy import deepcopy

# Third-Party Libraries
import pytest

# GeoIPS imports
from geoips.pydantic_models.v1.algorithms import AlgorithmArgumentsModel
from geoips.pydantic_models.v1.colormappers import ColormapperArgumentsModel
from geoips.pydantic_models.v1.filename_formatters import (
    FilenameFormatterArgumentsModel,
)
from geoips.pydantic_models.v1.interpolators import InterpolatorArgumentsModel
from geoips.pydantic_models.v1.output_formatters import OutputFormatterArgumentsModel
from geoips.pydantic_models.v1.output_checkers import OutputCheckerArgumentsModel
from geoips.pydantic_models.v1.readers import ReaderArgumentsModel
from geoips.pydantic_models.v1.title_formatters import TitleFormatterArgumentsModel
from geoips.pydantic_models.v1.workflows import WorkflowSpecModel
from tests.unit_tests.pydantic_models.v1.utils import (
    PathDict,
    load_geoips_yaml_plugin,
    load_test_cases,
    retrieve_model,
    validate_bad_plugin,
    validate_base_plugin,
    validate_neutral_plugin,
    validate_good_plugin,
)

# A mapping of interfaces implemented in pydantic and a plugin to validate against.
models_available = {
    "feature_annotators": {
        "good_source": ("yaml", "default_oldlace"),
        "model": None,
    },
    "filename_formatters": {
        "good_source": ("fixture", "valid_filename_formatter_arguments"),
        "model": FilenameFormatterArgumentsModel,
    },
    "gridline_annotators": {
        "good_source": ("yaml", "default_palegreen"),
        "model": None,
    },
    "products": {
        "good_source": ("yaml", ("abi", "Infrared")),
        "model": None,
    },
    "product_defaults": {
        "good_source": ("yaml", "windbarbs"),
        "model": None,
    },
    "sectors": {
        "good_source": ("yaml", "korea"),
        "model": None,
    },
    # mini-ips: `dynamic_sectors` removed. Its sample plugin `tc_web` is a TC
    # template that resolves through `bdeck_parser`, which needs pandas-era
    # trackfile parsing the fork excludes, so there is no dynamic sector here to
    # validate against. Restore this entry (and the dynamic_sectors/ test-case
    # directory) if the fork ever ships one.
    "readers": {
        "good_source": ("fixture", "valid_reader_arguments_model_data"),
        "model": ReaderArgumentsModel,
    },
    "output_checkers": {
        "good_source": ("fixture", "valid_output_checker_arguments"),
        "model": OutputCheckerArgumentsModel,
    },
    "output_formatters": {
        "good_source": ("fixture", "valid_output_formatter_arguments"),
        "model": OutputFormatterArgumentsModel,
    },
    "title_formatters": {
        "good_source": ("fixture", "valid_title_formatter_arguments"),
        "model": TitleFormatterArgumentsModel,
    },
    "algorithms": {
        "good_source": ("fixture", "valid_algorithm_arguments"),
        "model": AlgorithmArgumentsModel,
    },
    "interpolators": {
        "good_source": ("fixture", "valid_interpolator_arguments"),
        "model": InterpolatorArgumentsModel,
    },
    "workflows": {
        "good_source": ("fixture", "valid_workflow_spec_model_data"),
        "model": WorkflowSpecModel,
    },
    "colormappers": {
        "good_source": ("fixture", "valid_colormapper_plugin_data"),
        "model": ColormapperArgumentsModel,
    },
}


def load_good_plugins(models_available):
    """Generate a dictionary of valid GeoIPS plugins.

    Parameters
    ----------
    models_available: dict
        - A dictionary of interfaces and plugin names. Formatted:
          {interface_name: plugin_name}

    Returns
    -------
    good_plugins: dict
        - A dictionary of interfaces and their corresponding valid plugin. Formatted:
          {interface_name: plugin[dict]}
    """
    # mini-ips change: skip interfaces whose sample plugin is not registered
    # here, instead of raising at import time.
    #
    # This file names six specific built-in YAML plugins as its fixtures, and
    # mini-ips does not ship all of them -- `tc_web` is a dynamic sector that
    # needs the excluded `bdeck_parser`. More importantly, WHICH plugins ship is
    # derived from a dependency budget, so it moves whenever upstream changes a
    # plugin's imports. Hard-coding the list here would turn every upstream sync
    # into a test-fixture edit; skipping the absent ones keeps the suite honest
    # about what it actually covered. `missing_plugins` is asserted on in
    # test_good_plugins_cover_available_models below.
    good_plugins = {}
    missing = []
    for interface, cfg in models_available.items():
        source_type, plugin_name = cfg["good_source"]
        lookup = "sectors" if interface == "dynamic_sectors" else interface
        if source_type != "yaml":
            continue
        try:
            good_plugins[interface] = load_geoips_yaml_plugin(lookup, plugin_name)
        except KeyError:
            missing.append(f"{interface}:{plugin_name}")

    load_good_plugins.missing_plugins = missing
    return good_plugins


good_plugins = load_good_plugins(models_available)


# Generate separate test cases for each interface
def generate_test_cases(test_type):
    """Generate test cases used for pydantic validation.

    Where each case is a pytest parameter set, Formatted:
    param(argval=(interface_name, test_case[dict]), id={interface_name}_{test_id})

    Parameters
    ----------
    test_type: str
        - The type of test being ran. Must be one of ["good", "bad", "neutral"].

    Returns
    -------
    cases: list[pytest.param]
        - A list of pytest parameters representing cases to test.

    Raises
    ------
    ValueError:
        - Raised if test_type isn't one of 'bad' or 'neutral'. (Raised via
          load_test_cases).
    """
    cases = []
    for interface in models_available.keys():
        test_cases = load_test_cases(interface, test_type)
        for key, value in test_cases.items():
            cases.append(pytest.param((interface, key, value), id=f"{interface}_{key}"))
    return cases


def get_good_plugin(interface, request):
    """
    Return a "known-good" plugin instance or config for the given interface.

    Source of the good plugin is defined in `models_available[interface]["good_source"]`
    as a tuple: (source_type, source_value), where:

    - source_type == "yaml": return `good_plugins[interface]`
    - source_type == "fixture" return `request.getfixturevalue(source_value)`

    Parameters
    ----------
    interface : str
        GeoIPS interface name such as readers.
    request : pytest.FixtureRequest
        Pytest request object used to resolve fixture-based sources.

    Returns
    -------
    Any
        The good plugin object or config for the given interface.

    """
    cfg = models_available.get(interface)
    if cfg is None:
        raise KeyError(
            f"Unknown interface '{interface}' provided."
            f" Available: {sorted(models_available)}"
        )

    try:
        source_type, source_value = cfg["good_source"]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            f"Invalid good_source for interface '{interface}'"
            f"(source_type, source_value), got {cfg.get('good_source')!r}"
        ) from exc

    if source_type == "yaml":
        if interface not in good_plugins:
            # mini-ips: the fork's dependency budget can exclude the plugin an
            # interface's fixture is built from -- every product and almost
            # every product_default is dropped once the matplotlib colormappers
            # go. The bad/neutral cases mutate the good plugin, so without it
            # there is nothing to test rather than something failing. Skip, and
            # let test_good_plugins_cover_available_models report the gap.
            pytest.skip(
                f"no '{interface}' plugin ships in this build "
                f"(missing: {getattr(load_good_plugins, 'missing_plugins', [])})"
            )
        try:
            return good_plugins[interface]
        except KeyError as exc:
            raise KeyError(
                f"Missing good_plugins entry for interface '{interface}'"
            ) from exc

    if source_type == "fixture":
        if not source_value:
            raise ValueError(
                f"Fixture for the interface '{interface}' is empty or None:"
                f"{source_value!r}"
            )
        return request.getfixturevalue(source_value)

    raise ValueError(
        f"unknown good_source type: {source_type} for interface '{interface}': "
        f"{source_type!r}. should be 'yaml' or 'fixture'"
    )


def get_model(interface, plugin):
    """Return the Pydantic model used to validate a plugin for a given interface."""
    cfg = models_available[interface]
    if cfg["model"] is not None:
        return cfg["model"]
    return retrieve_model(plugin)


@pytest.mark.parametrize("interface", list(models_available.keys()))
def test_base_plugin(interface, request):
    """Assert that a well formatted GeoIPS plugin is valid.

    Parameters
    ----------
    good_plugin: dict
        - A dictionary representing a valid GeoIPS plugin.
    """
    good_plugin = get_good_plugin(interface, request)
    model = get_model(interface, good_plugin)
    validate_base_plugin(good_plugin, model)


@pytest.mark.parametrize("test_case", generate_test_cases("good"))
def test_good_plugins(test_case, request):
    """Perform validation on GeoIPS plugins, including passing 'good' cases."""
    interface, _key, case_value = test_case

    good_plugin = get_good_plugin(interface, request)
    plugin = PathDict(deepcopy(good_plugin))
    model = get_model(interface, plugin)

    validate_good_plugin(plugin, case_value, model)


@pytest.mark.parametrize("test_case", generate_test_cases("bad"))
def test_bad_plugins(test_case, request):
    """Perform validation on GeoIPS plugins, including failing cases.

    Parameters
    ----------
    test_tup:
        - A tuple formatted (interface_name, (key, value, class, err_str)), Formatted:
          (str, (str, any, str, str)) used to run and validate tests.
    """
    interface, _key, case_value = test_case

    good_plugin = get_good_plugin(interface, request)
    plugin = PathDict(deepcopy(good_plugin))
    model = get_model(interface, plugin)

    validate_bad_plugin(plugin, case_value, model)


@pytest.mark.parametrize("test_case", generate_test_cases("neutral"))
def test_neutral_plugins(test_case, request):
    """Perform validation on GeoIPS plugins, including neutral cases.

    Parameters
    ----------
    test_tup:
        - A tuple formatted (interface_name, (key, value, class, err_str)), Formatted:
          (str, (str, any, str, str)) used to run and validate tests.
    """
    interface, _key, case_value = test_case

    good_plugin = get_good_plugin(interface, request)
    plugin = PathDict(deepcopy(good_plugin))
    model = get_model(interface, plugin)

    validate_neutral_plugin(plugin, case_value, model)


def test_good_plugins_cover_available_models():
    """Report which fixture plugins mini-ips does not ship.

    `load_good_plugins` skips absent ones so collection cannot fail, which
    would otherwise let coverage silently erode as the derived plugin set
    changes. This makes what was skipped visible in the test output, and fails
    if the fixtures have eroded to the point where the suite is not testing
    much any more.
    """
    missing = getattr(load_good_plugins, "missing_plugins", [])
    expected_yaml = sorted(
        interface
        for interface, cfg in models_available.items()
        if cfg["good_source"][0] == "yaml"
    )
    resolved = sorted(good_plugins)
    print(
        f"\nYAML model fixtures: {len(resolved)}/{len(expected_yaml)} resolve "
        f"in this build.\n  resolved: {resolved}\n  missing:  {missing}"
    )

    # Assert on what the dependency budget actually guarantees rather than on a
    # fraction. These three are built from plugins that ship under every tier --
    # the two `default` annotators are required by image_utils' fallback lookup,
    # and static sectors carry no plugin dependencies at all -- so if any of
    # them stops resolving, something is genuinely wrong with the build rather
    # than merely narrower than upstream.
    guaranteed = {"feature_annotators", "gridline_annotators", "sectors"}
    assert guaranteed <= set(resolved), (
        f"these fixtures should resolve under any budget tier but did not: "
        f"{sorted(guaranteed - set(resolved))}"
    )
