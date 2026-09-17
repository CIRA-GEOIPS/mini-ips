# Fork-owned tests

Copied from upstream `geoips/tests/`, which `prune/manifest.yaml` denies. These
are the tests that exercise the machinery mini-ips keeps and that run with **no
satellite data, no network and no `GEOIPS_TESTDATA_DIR`**.

Two deliberate deviations from upstream, both because a fixture named a plugin
the fork does not ship:

- `abi_netcdf` -> `geoips_netcdf` throughout. `abi_netcdf` imports scipy, so the
  dependency budget excludes it. These are structural validation tests, so the
  reader name only has to resolve to a registered reader; `geoips_netcdf` is
  dependency-neutral and always present.
- `dynamic_sectors` fixtures removed. The sample plugin `tc_web` resolves
  through `bdeck_parser`, which the budget excludes.

`test_pydantic_models.load_good_plugins` skips fixture plugins that are not
registered rather than failing at import, and
`test_good_plugins_cover_available_models` fails if more than a third of them
go missing -- so the derived plugin set can move with upstream without breaking
collection, while real erosion of coverage still surfaces.

Because these files live in `overlay/`, upstream changes to them never conflict;
they are simply not picked up. Re-copy from upstream deliberately when you want
their new cases.
