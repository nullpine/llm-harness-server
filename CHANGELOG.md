# Changelog

All notable changes to this project. The version reported by `GET /healthz` is
the contract version in `docs/API-CONTRACT.md`.

## [Unreleased]

### Added

- M0 toolchain: `pyproject.toml` (ruff, mypy `--strict`, pytest), `Makefile`
  targets `setup/dev/test/lint/fmt/smoke/lock/clean`, and a `.venv` built by
  `make` so CI runs exactly what a developer runs.
- `requirements.lock` — the VM's install, resolved for `x86_64-manylinux_2_28`
  and Python 3.12, with vLLM pinned to 0.27.1.
- GitHub Actions: `ci.yml` (lint → typecheck → test, no GPU) and
  `shellcheck.yml` (warnings fail).
- Issue and pull request templates carrying the repo's guard rails.
