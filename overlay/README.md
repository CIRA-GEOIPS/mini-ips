# mini-geoips

The [GeoIPS](https://github.com/NRLMMD-GEOIPS/geoips) order-based procflow,
slimmed to run against external plugin packages.

Generated from upstream geoips by
[CIRA-GEOIPS/mini-ips](https://github.com/CIRA-GEOIPS/mini-ips); see that
repository for how the tree is derived and what was removed.

```bash
pip install mini-geoips
geoips config create-registries
geoips list plugins
geoips run order_based <workflow-name> <input-files...>
```

The import package is still `geoips` and still reads the
`geoips.plugin_packages` entry-point group, so existing plugin packages work
unmodified. It is a drop-in replacement: `mini-geoips` and upstream `geoips`
cannot be installed into the same environment.

## What is different

45 required dependencies instead of 96, and 147 MB installed instead of 542 MB.
The built-in plugin set is derived from that dependency budget: a plugin ships
only if its imports fall inside the budget and every plugin it imports does
too. That leaves 20 readers, 13 output formatters, 11 algorithms, 10 filename
formatters, 57 static sectors and 2 interpolators, and excludes anything
needing `scipy`, `h5py`, `rasterio`, `pygrib`, `pyhdf` or `satpy`.

Imagery is not built in. Install `mini-geoips[imagery]` for the matplotlib and
cartopy helpers in `geoips.image_utils`, `[netcdf]` for netCDF4, or
`[geostationary]` for the zarr geolocation cache.

## Licence

Upstream's NRL Open License Agreement applies; the `LICENSE` and
`DISTRIBUTION` notices are carried through unchanged.
