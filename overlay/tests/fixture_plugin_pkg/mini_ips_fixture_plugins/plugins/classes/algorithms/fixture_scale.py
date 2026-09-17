"""An algorithm that scales its input by a constant."""

import logging

from geoips.interfaces.class_based.algorithms import BaseAlgorithmPlugin

LOG = logging.getLogger(__name__)


class FixtureScaleAlgorithmPlugin(BaseAlgorithmPlugin):
    """Multiply the input array by `factor`."""

    interface = "algorithms"
    # The family is a contract, not a label: it selects the conversion the OBP
    # applies between the upstream DataTree node and this signature.
    # `list_numpy_to_numpy` is what turns the reader's Dataset into the
    # list[np.ndarray] that `arrays` expects -- naming a family that does not
    # exist in ALGORITHM_FAMILY_CONVERSIONS means no conversion happens and the
    # raw Dataset arrives instead.
    family = "list_numpy_to_numpy"
    name = "fixture_scale"

    def call(self, arrays, factor=2.0):
        """Return the first input array scaled by `factor`."""
        LOG.info("fixture_scale factor=%s", factor)
        return arrays[0] * factor


PLUGIN_CLASS = FixtureScaleAlgorithmPlugin
