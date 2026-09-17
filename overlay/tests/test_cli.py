# # # This source code is subject to the license referenced at
# # # https://github.com/NRLMMD-GEOIPS.

"""Cover the fork-owned CLI.

`geoips_cli/` is the one substantial piece of runtime code mini-ips writes
itself rather than inherits, and it was the only such piece with no tests. It
is also where the fork's deferred-import discipline lives, which nothing else
checks: see `test_help_does_not_import_geoips`.

Commands are driven through `main(argv)` in-process rather than by spawning the
console script, so a failure surfaces as a traceback instead of an exit code.
The one exception is the import-discipline test, which needs a clean
interpreter by definition.
"""

import subprocess
import sys

import pytest

from geoips_cli.main import main


def run(capsys, argv):
    """Invoke the CLI and return (exit code, stdout)."""
    code = main(argv)
    return code, capsys.readouterr().out


def test_help_exits_zero():
    """`--help` is argparse's SystemExit(0), not a return value."""
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == 0


def test_help_does_not_import_geoips():
    """`--help` must not pay for the geoips import.

    Every command body imports geoips lazily, and it is worth 0.04 s versus
    0.50 s on every invocation that does not need it -- including tab
    completion and `--help`. A single top-level `from geoips... import` in
    geoips_cli/main.py silently undoes that, and no other test would notice.
    """
    probe = (
        "import sys\n"
        "import geoips_cli.main as cli\n"
        "try:\n"
        "    cli.main(['--help'])\n"
        "except SystemExit:\n"
        "    pass\n"
        "leaked = sorted(m for m in sys.modules if m.split('.')[0] == 'geoips')\n"
        "assert not leaked, leaked\n"
    )
    # cwd='/' so a sibling directory named `geoips` cannot be picked up as a
    # namespace package and make the assertion pass for the wrong reason.
    proc = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, cwd="/"
    )
    assert proc.returncode == 0, proc.stderr


def test_list_packages_includes_geoips_and_the_fixture(capsys):
    code, out = run(capsys, ["list", "packages"])
    assert code == 0
    assert "geoips" in out.split()
    assert "mini_ips_fixture_plugins" in out.split()


def test_list_interfaces_names_order_based_procflow(capsys):
    code, out = run(capsys, ["list", "interfaces"])
    assert code == 0
    assert "procflows" in out


def test_list_plugins_reports_a_registry(capsys):
    """The registry must be populated, not merely readable.

    pluginify wipes *every* registry if any single plugin fails to import, so
    "zero plugins" is the shape a broken build takes -- not an exception.
    """
    code, out = run(capsys, ["list", "plugins"])
    assert code == 0
    rows = [line for line in out.splitlines() if line.strip()]
    assert len(rows) > 100, f"only {len(rows)} plugins registered"
    assert any("order_based" in row for row in rows)


def test_list_workflows_includes_the_external_one(capsys):
    code, out = run(capsys, ["list", "workflows"])
    assert code == 0
    assert "mini_ips_reference" in out


def test_expand_renders_the_external_workflow(capsys):
    """`expand` is the debugging command; it must survive non-YAML values.

    An expanded workflow carries datetimes, which `yaml.safe_dump` refuses --
    hence the JSON round-trip in cmd_expand. This asserts that path works on a
    real workflow rather than a hand-built dict.
    """
    code, out = run(capsys, ["expand", "mini_ips_reference"])
    assert code == 0
    assert "name: mini_ips_reference" in out
    assert "steps" in out


def test_expand_rejects_an_unknown_workflow(capsys):
    code = main(["expand", "no_such_workflow_exists"])
    assert code == 2
    assert "error:" in capsys.readouterr().err


def test_unknown_command_is_a_usage_error():
    with pytest.raises(SystemExit) as excinfo:
        main(["frobnicate"])
    assert excinfo.value.code == 2
