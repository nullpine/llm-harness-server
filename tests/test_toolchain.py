"""M0: the package installs, imports, and reports the version /healthz will serve.

There is nothing else to test yet. This exists so `make test` exercises the
installed package rather than exiting "no tests collected", which would let a
broken src-layout install pass CI unnoticed.
"""

import tomllib
from pathlib import Path

import harness_control


def test_package_imports_from_the_installed_distribution() -> None:
    assert Path(harness_control.__file__).name == "__init__.py"


def test_version_matches_pyproject() -> None:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    declared = tomllib.loads(pyproject.read_text())["project"]["version"]
    assert harness_control.__version__ == declared
