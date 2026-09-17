"""Assert a built image can actually do the work, from the runtime stage.

Both Dockerfiles run this as the final, non-root user against exactly what is
published. That placement is the point: an earlier version checked the Alpine
source builds in the *builder* stage, where proj-dev was still installed, so it
passed while the shipped image could not project at all.

It is one file rather than a `python -c` in each Dockerfile because the two had
already drifted -- the Alpine copy imported pyresample and the Debian copy did
not, so the check that mattered most on the source-building image was the one
running in fewer places.

A broken image that only fails at run time is worse than one that fails here.
"""

import numpy as np
import pyproj
import pyresample
import shapely
from numpy.linalg import det, inv

# BLAS/LAPACK. Alpine links numpy against the system OpenBLAS instead of the
# wheel's vendored copy, so this is a real integration check there.
assert abs(det([[1.0, 2.0], [3.0, 4.0]]) + 2.0) < 1e-9, "BLAS/LAPACK broken"
assert inv(np.eye(64)).shape == (64, 64), "BLAS/LAPACK broken"

# PROJ. Built from source, pyproj has no bundled proj_dir to fall back on, and
# a missing PROJ_DATA is invisible until something actually projects.
x, y = pyproj.Transformer.from_crs(4326, 3857).transform(10, 10)
assert x > 1e6 and y > 1e6, (x, y)

# GEOS.
assert abs(shapely.Point(0, 0).buffer(1).area - 3.1365) < 1e-3

print(
    f"numpy {np.__version__} blas ok | pyproj {pyproj.__version__} "
    f"data {pyproj.datadir.get_data_dir()} | shapely {shapely.__version__} "
    f"| pyresample {pyresample.__version__}"
)

# The integration that depends on all of the above at once: a geoips sector
# resolving to a pyresample AreaDefinition and computing its lon/lats. Imported
# last so a native-library failure above reports as itself, not as a geoips
# import error.
from geoips.interfaces import procflows, sectors  # noqa: E402

procflow = procflows.get_plugin("order_based")
assert procflow.name == "order_based", procflow

area = sectors.get_plugin("global_cylindrical").area_definition
lons, lats = area.get_lonlats()
assert lons.shape == area.shape, (lons.shape, area.shape)
print(f"order_based OK | sector {area.shape} {area.proj_dict.get('proj')}")
