# # # This source code is subject to the license referenced at
# # # https://github.com/NRLMMD-GEOIPS.

"""Every shipped YAML plugin must resolve and validate through the registry.

Ported from upstream (`tests/unit_tests/plugins/yaml/test_all_yaml_plugins.py`)
because a generated fork needs it more than upstream does. mini-ips derives its
YAML plugin set: a product is kept only if every plugin it references is also
kept, iterated to a fixed point. That derivation is static analysis over YAML,
and the failure it can produce -- shipping a product whose product_default was
excluded -- is invisible to the import-closure gate and to every other test
here, because nothing else loads all 74 of them.

It also covers external packages, since it walks every entry point in
`geoips.plugin_packages` rather than the geoips tree.
"""

from importlib import metadata, resources

import pytest
import yaml

from geoips import interfaces


def yield_plugins():
    """Yield every YAML plugin file in every installed plugin package."""
    for package in metadata.entry_points(group="geoips.plugin_packages"):
        root = resources.files(package.value) / "plugins/yaml"
        if not root.is_dir():
            continue
        yield from sorted(root.rglob("*.yaml"))


def gen_label(value):
    """Use the file name as the pytest id."""
    return value.name


@pytest.mark.parametrize("plugin", yield_plugins(), ids=gen_label)
def test_is_plugin_valid(plugin):
    """Resolve the plugin through its interface, which validates it."""
    with open(plugin, "r", encoding="utf-8") as handle:
        docs = list(yaml.safe_load_all(handle))

    for doc in docs:
        interface = getattr(interfaces, doc["interface"])
        # Every `get_plugin` call validates; there is no need to invoke the
        # validator separately.
        if doc["interface"] == "products":
            # Products are looked up by (source_name, product_name), not by the
            # top-level plugin name.
            for product in doc["spec"]["products"]:
                for source_name in product["source_names"]:
                    interface.get_plugin(source_name, product["name"])
        else:
            interface.get_plugin(doc["name"])
