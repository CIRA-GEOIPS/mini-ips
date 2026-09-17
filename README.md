# mini-ips

A generated fork of [NRLMMD-GEOIPS/geoips](https://github.com/NRLMMD-GEOIPS/geoips)
that runs the order-based procflow (OBP) against external plugin packages.

This repository holds the recipe rather than the code: a pinned upstream commit,
a keep/drop manifest, eight patches, and an overlay of fork-owned files.
`python prune/slim.py` regenerates the slimmed tree into `build/`, running every
gate as it goes. Upstream stays the source of truth.

## What is removed, and what it buys

Measured against upstream `1.20.0a0` (`751e6533e`), on one machine in one run.
Sizes are the sum of file bytes, excluding `__pycache__`.

### Source tree: 123.0 MB → 1.6 MB (1.3% of upstream)

Upstream ships 2,105 files. 1,772 are dropped, 333 kept, and one
(`geoips/utils/types/gridline_spacing.py`) is created by a patch.

| removed | files | Python LOC | MB | % of tree |
|---|---:|---:|---:|---:|
| golden test imagery (`tests/outputs/`) | 136 | 0 | 88.0 | 71.6% |
| `docs/` | 759 | 1,722 | 27.7 | 22.5% |
| rest of `tests/` (shell scripts, fixtures) | 386 | 19,836 | 3.4 | 2.8% |
| class plugins outside the dependency budget | 79 | 18,241 | 0.7 | 0.5% |
| installer and environment provenance | 94 | 322 | 0.5 | 0.4% |
| YAML plugins with unresolvable references | 234 | 0 | 0.3 | 0.3% |
| upstream CLI (`geoips/commandline/`) | 21 | 6,872 | 0.3 | 0.2% |
| other `geoips/` modules | 28 | 5,430 | 0.2 | 0.2% |
| legacy procflows (`single_source`, `config_based`) | 2 | 4,984 | 0.2 | 0.2% |
| upstream CI, packaging, repo metadata | 33 | 522 | 0.1 | 0.1% |
| **total** | **1,772** | **57,929** | **121.5** | **98.7%** |

The megabytes are test imagery and documentation. The code is the plugin, CLI
and procflow removal: `geoips/` goes from 309 modules and 77,844 lines to 199
modules and 42,468 lines, 55% of upstream's Python.

### Installed dependencies: 542.3 MB → 146.7 MB (27% of upstream)

| | packages | installed | % of upstream |
|---|---:|---:|---:|
| upstream geoips | 96 | 542.3 MB | 100% |
| **mini-ips** | **45** | **146.7 MB** | **27%** |
| mini-ips with all runtime extras | 59 | 271.6 MB | 50% |

51 packages go and none are added:

| removed | packages | MB | % of upstream |
|---|---:|---:|---:|
| `scipy` and `scikit-image` stack | 8 | 102.9 | 19.0% |
| GRIB/HDF readers: `pygrib`, `pyhdf`, `h5py`, `hdf5plugin`, `pypublicdecompwt` | 5 | 85.8 | 15.8% |
| raster and GIS output: `rasterio`, `rio-cogeo`, `rtree`, … | 6 | 69.3 | 12.8% |
| `matplotlib` and `cartopy` (now the `imagery` extra) | 8 | 60.8 | 11.2% |
| `netCDF4` (now the `netcdf` extra) | 1 | 56.0 | 10.3% |
| `satpy` stack | 7 | 9.3 | 1.7% |
| `setuptools`, `ephem`, `isodate`, `psutil`, `tqdm`, `requests`, … | 13 | 8.2 | 1.5% |
| `zarr` and `numexpr` (now the `geostationary` extra) | 3 | 4.9 | 0.9% |
| **total** | **51** | **397.2** | **73.3%** |

Most of that bulk is vendored native code: GDAL inside `rasterio`, eccodes
inside `pygrib`, HDF4 inside `pyhdf`, a bundled BLAS inside `scipy`. On glibc
these do install from wheels without system libraries. On musl the wheels are
much scarcer, which is why the Alpine image is only practical under this budget.

What remains is what the runtime cannot do without: `numpy`, `xarray`, `pandas`
(a hard `xarray` requirement), `pyresample`, `pyproj`, `shapely`, `dask`,
`pydantic`, `pluginify`.

### Everything else

| | upstream | mini-ips | |
|---|---|---|---|
| class plugins (modules declaring an `interface`) | 139 | 71 | 51% |
| YAML plugins (files declaring an `interface`) | 263 | 74 | 28% |
| `import geoips` (best of 5) | 1.12 s | 0.66 s | 59% |
| test suite | needs `$GEOIPS_TESTDATA_DIR` | 1,358 tests in 6 s, no data | |
| container image | none published | 226 MB Alpine, 284 MB Debian | |

The installed Python package itself only halves, 3.2 MB to 1.6 MB. The large
numbers above are dependencies and repository content.

### What you lose

The default budget admits no plugin that adds a dependency. That excludes every
colormapper but `cmap_rgb`, the six matplotlib/cartopy output formatters
(`imagery_annotated`, `imagery_clean`, `imagery_windbarbs`,
`imagery_windbarbs_multi_level`, `full_disk_image`, `unprojected_image`),
`cogeotiff`, and all of upstream's workflows, since every one names an excluded
reader or colormapper.

What registers is a read/regrid/compute/write runtime: 20 readers, 13 output
formatters (netCDF, GeoTIFF, text, and the clean windbarb images), 11
algorithms, 10 filename formatters, 57 static sectors, 3 coverage checkers, 2
interpolators, 2 output checkers.

Imagery comes from external plugin packages, or from
`pip install mini-geoips[imagery]` for the matplotlib and cartopy helpers in
`geoips.image_utils`. Setting `plugins.budget_tier: all` in `prune/manifest.yaml`
builds upstream's imagery plugins in instead; `slim.py` then keeps 125 class
plugin files rather than 77 and 206 YAML plugins rather than 74, including 16
workflows. Those need the extras installed to run. (`slim.py` counts files under
`plugins/`, so its totals run a little above the table above, which counts only
modules that declare an `interface`.)

## Layout

```
upstream.lock            the pinned upstream commit; the one file you edit to move
prune/manifest.yaml      what to keep and what to drop (292 lines)
prune/slim.py            the build, and every gate that can fail it
prune/patches/           8 patches, 304 lines
overlay/                 files this fork owns: pyproject, CLI, tests, reference workflow
overlay/pyproject.toml   the dependency budget and the ceilings CI asserts
tools/                   dependency budget, closure scan, orphan check, import
                         timing, release watch, sync report
docker/                  Debian and Alpine images, built from build/
build/                   generated, gitignored
```

The maintenance surface is 2,763 lines of fork-authored Python, the manifest and
the patches. The 12,514 lines under `overlay/tests/` are mostly upstream's own
tests, kept.

## Build

```bash
git clone <this repo> && cd mini-ips

# Not `geoips`: a sibling directory of that name shadows the installed package
# as a namespace package.
git clone --filter=blob:none --no-checkout \
  https://github.com/NRLMMD-GEOIPS/geoips.git upstream-geoips

pip install pyyaml packaging
python prune/slim.py            # -> build/
pip install -e ./build
geoips config create-registries
geoips list plugins
```

Needs Python >=3.11,<3.14, `git`, `pyyaml` and `packaging`. No system libraries.
`uv` is only needed for `tools/depbudget.py --refresh`; the resolved closure is
checked in at `prune/dependency-closure.lock`, so a normal build is offline.

`GEOIPS_OUTDIRS` sets the output directory and defaults to `$HOME/geoips_outdirs`
with a warning. `slim.py` exits 3 on an upstream path the manifest does not
classify, 4 on a patch that no longer applies, and 5 on an import the pruning
broke; the nightly sync triages on those codes.

```bash
geoips run order_based <workflow-name> <input-files...>
geoips expand <workflow-name>       # what the OBP will actually run

# tests: pip install -e './build[test]' first
cd build && pytest                  # 1,358 tests, no test data needed
```

To move to a different upstream commit, edit `ref` and `sha` in `upstream.lock`
and re-run `slim.py`. Everything else is derived.

## Your own plugins

The import package is still `geoips` and still reads the
`geoips.plugin_packages` entry-point group, so existing plugin packages work
unmodified:

```toml
[project.entry-points."geoips.plugin_packages"]
my_plugins = "my_plugins"
```

plus a `my_plugins/plugins/` directory, then `geoips config create-registries`.

`overlay/tests/fixture_plugin_pkg/` is a worked example with a reader, an
algorithm and a workflow, and `tests/test_external_plugins.py` runs it through
the OBP in CI. `mini_ips_reference` is a workflow built only from plugins the
budget guarantees; copy it when writing your first one.

## The dependency budget

`overlay/pyproject.toml` is the budget. It lives there because it is the same
list the wheel ships. Fifteen distributions are declared:

    pydantic  xarray  numpy  pluginify  platformdirs  pyyaml
    geoips-yaml-utils  pyresample  jsonschema  referencing  tabulate
    typing-extensions  cftime  dask  lexeme-type

Those plus the extras named in `[tool.mini-ips] runtime_extras` (`imagery`,
`netcdf`, `geostationary`) are the allowed distributions. They resolve to 45 and
59 packages against ceilings of 60 and 65, which `slim.py` fails on.

`slim.py` resolves the budget to its transitive closure and derives the plugin
set from it: a plugin is admissible if its own unguarded third-party imports
fall inside the closure and every plugin it imports is admissible, iterated to a
fixed point. The same rule runs over YAML plugins, where products reference
product_defaults, which reference colormappers. `tools/depbudget.py` documents
the two ways this goes wrong.

The plugin set is therefore derived, so an upstream algorithm needing only numpy
is picked up on the next sync with no manifest edit, and a reader that starts
importing `h5py` drops itself. `tools/find_orphans.py` reports modules left
unreachable when the budget tightens, which the closure gate cannot see.

## Keeping up with upstream

| workflow | when | what |
|---|---|---|
| `verify` | every PR | regenerate, build a registry, run the tests, check import time |
| `upstream-release-watch` | nightly | new release, regenerate, open or update one PR with a triage report |
| `upstream-main-drift` | weekly | report-only early warning on a tracking issue |
| `images` | docker/prune PRs, main, tags | build both images, smoke-test, push to GHCR |
| `release` | tag | build and publish the wheel as `mini-geoips` |

The release watcher bumps the lock, refreshes the closure and recomputes the
derived plugin sets, then reports what it could not do. It opens the PR even
when the build fails, so a failed sync arrives as a diff and a triage report. A
stale patch is reported for a human to rebase rather than merged three-way.

It will not move the pin backwards. Upstream's tags are not monotonic with
history: `2.0.0a0` sorts above `1.20.0a0` but its commit is an ancestor of it.
The watcher checks ancestry and files an issue.

## Patches

Eight, 304 lines, in `prune/patches/`. Four make heavy imports lazy or optional
(`netCDF4`, `matplotlib`, `cartopy`, `zarr`). One relocates `set_lonlat_spacing`
off the legacy `geoips.dev` package, which otherwise pulls cartopy onto the OBP
hot path. One repoints an import at code the fork keeps.

Two fix upstream bugs: a dead import of the non-existent
`geoips.filenames.product_filenames`, and a registry lookup that raises
`KeyError` instead of `PluginError` on an empty interface. When upstream fixes
one, `slim.py` reports that patch as a no-op so it can be deleted.

Two more upstream bugs are reported rather than patched, because nothing here
hits them: `order_based.py:119` passes `fnames=` to a parameter named
`filenames`, and `dask`, `lexeme_type` and `pygments` are imported but
undeclared while `tqdm` is declared but never imported.

## Containers

```bash
python prune/slim.py
docker build -f docker/Dockerfile.debian -t mini-geoips:debian .
docker run --rm mini-geoips:debian list plugins
docker run --rm -v "$PWD/out:/data/outdirs" mini-geoips:debian \
  run order_based <workflow> <files...>        # GEOIPS_OUTDIRS=/data/outdirs
```

CI builds and publishes both for linux/amd64 to
`ghcr.io/cira-geoips/mini-ips`: Alpine 226 MB, Debian 284 MB. The same trees
build larger on darwin/arm64 (292 and 447 MB), so calibrate from CI rather than
a local `docker images`.

Alpine builds numpy, pyproj and shapely from source against system libraries,
saving 70 MB of vendored native code; Debian keeps the wheels, because its
`libproj25` pulls in a 23 MB `proj-data`. Each Dockerfile carries the
measurements. Both run as a non-root user, bake the registry in, and assert in
the runtime stage that BLAS, PROJ, GEOS and a geoips sector all work.

## Licence

Upstream's NRL Open License Agreement applies. `LICENSE`, `DISTRIBUTION` and the
per-file licence headers are carried through unchanged.
