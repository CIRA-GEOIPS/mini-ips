"""A reader that fabricates data instead of reading a file.

Deliberately synthetic: the point of this package is to prove that an external
package can register plugins and be driven by the order-based procflow, not to
test file parsing. Fabricating the array keeps the test independent of
satellite data, of `$GEOIPS_TESTDATA_DIR`, and of any I/O library outside the
required dependency set.
"""

import logging
from datetime import datetime, timezone

import numpy as np
import xarray as xr

from geoips.interfaces.class_based.readers import BaseReaderPlugin

LOG = logging.getLogger(__name__)


class FixtureReaderPlugin(BaseReaderPlugin):
    """Produce a small, deterministic dataset for any filename given."""

    interface = "readers"
    family = "standard"
    name = "fixture_reader"

    source_names = ["fixture_source"]

    def call(
        self,
        fnames,
        metadata_only=False,
        chans=None,
        area_def=None,
        self_register=False,
    ):
        """Return a dict of xarray Datasets, as the reader contract requires.

        `fnames` is accepted and logged but not opened -- the values are
        generated, so the test needs no data files on disk.
        """
        LOG.info("fixture_reader invoked with %s file(s)", len(list(fnames)))
        rows, cols = 4, 5
        lats, lons = np.meshgrid(
            np.linspace(10.0, 13.0, rows), np.linspace(-100.0, -96.0, cols),
            indexing="ij",
        )
        # A fixed ramp, so assertions can be exact rather than approximate.
        values = np.arange(rows * cols, dtype="float32").reshape(rows, cols)

        start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc).replace(tzinfo=None)
        attrs = {
            # The five attributes geoips requires on every dataset.
            "source_name": "fixture_source",
            "platform_name": "fixture_platform",
            "data_provider": "mini_ips_tests",
            "start_datetime": start,
            "end_datetime": start,
        }
        dataset = xr.Dataset(
            {
                "TEMP": (("y", "x"), values),
                "latitude": (("y", "x"), lats),
                "longitude": (("y", "x"), lons),
            },
            attrs=attrs,
        )
        metadata = xr.Dataset(attrs=dict(attrs))
        return {"FIXTURE": dataset, "METADATA": metadata}


PLUGIN_CLASS = FixtureReaderPlugin
