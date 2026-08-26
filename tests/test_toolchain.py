"""The package installs and imports, and the two versions /healthz serves are right.

`/healthz` reports both: `version` is the API contract version, `service_version`
is this build. They move independently and mean different things, so each has its
own guard here.
"""

import re
import tomllib
from pathlib import Path

import harness_control
from harness_control.routes.health import CONTRACT_VERSION

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT = REPO_ROOT / "docs" / "API-CONTRACT.md"


def test_package_imports_from_the_installed_distribution() -> None:
    assert Path(harness_control.__file__).name == "__init__.py"


def test_service_version_matches_pyproject() -> None:
    """`service_version` on /healthz is the build — i.e. the packaged version."""
    declared = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert harness_control.__version__ == declared


def test_contract_version_matches_the_contract_title() -> None:
    """The doc and the code cannot drift apart again.

    `docs/API-CONTRACT.md` is normative and byte-identical to the desktop repo's
    copy. Bumping its title without bumping `CONTRACT_VERSION` would leave the
    server claiming conformance to a version it does not implement — which is
    exactly what happened between v1 and v1.1, silently, because nothing checked.
    """
    title = CONTRACT.read_text(encoding="utf-8").splitlines()[0]
    match = re.search(r"API Contract v(\d+(?:\.\d+)*)", title)
    assert match is not None, f"cannot find a version in the contract title: {title!r}"
    assert match.group(1) == CONTRACT_VERSION, (
        f"CONTRACT_VERSION is {CONTRACT_VERSION!r} but the contract title says "
        f"{match.group(1)!r} — bump both, in both repos"
    )


def test_the_two_versions_are_not_the_same_field() -> None:
    """A copy-paste that made `version` the build would pass every other test."""
    assert harness_control.__version__ != CONTRACT_VERSION


def test_the_contract_hash_sidecar_is_current() -> None:
    """`docs/.api-contract.sha256` must match the contract it stamps.

    The desktop repo guards its copy with `scripts/check-contract-hash.mjs`; this
    is the server-side equivalent, riding on `make test` so CI already runs it. A
    stamped hash that nothing verifies is worse than no hash — it looks like a
    guard while silently going stale.

    If this fails and the contract change is deliberate, the change needs a PR in
    *both* repos and a version bump (CLAUDE.md ground rule 3). Re-stamp with:

        shasum -a 256 docs/API-CONTRACT.md > docs/.api-contract.sha256   # from the repo root
    """
    import hashlib

    sidecar = REPO_ROOT / "docs" / ".api-contract.sha256"
    recorded = sidecar.read_text(encoding="utf-8").split()[0]
    actual = hashlib.sha256(CONTRACT.read_bytes()).hexdigest()
    assert recorded == actual, (
        "docs/API-CONTRACT.md changed without re-stamping docs/.api-contract.sha256"
    )
