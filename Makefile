# llm-harness-server — dev entrypoints. See CLAUDE.md § Commands.
# Everything here runs without a GPU; the engine is stood in for by tests/fake_upstream.py.

SHELL := /usr/bin/env bash
VENV  := .venv
BIN   := $(VENV)/bin
UV    ?= uv
PYTHON_VERSION := 3.12

.DEFAULT_GOAL := help
.PHONY: help setup dev test lint fmt smoke lock clean

help: ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-8s\033[0m %s\n", $$1, $$2}'

setup: $(BIN)/pytest ## Create .venv and install the package with dev extras

$(BIN)/pytest: pyproject.toml
	$(UV) venv --python $(PYTHON_VERSION) $(VENV)
	$(UV) pip install --python $(VENV) --editable ".[dev]"
	@touch $(BIN)/pytest

dev: setup ## the local stack: ollama + control plane with reload (scripts/dev-local.sh)
	./scripts/dev-local.sh

test: setup ## pytest, no GPU required
	$(BIN)/pytest

lint: setup ## ruff check + ruff format --check + mypy --strict
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .
	$(BIN)/mypy

fmt: setup ## ruff format + ruff's safe autofixes
	$(BIN)/ruff format .
	$(BIN)/ruff check --fix .

smoke: ## End-to-end curl check against a real deployment: make smoke HOST=https://...
	@[ -n "$(HOST)" ] || { echo "usage: make smoke HOST=https://harness.example.com" >&2; exit 2; }
	./scripts/smoke.sh "$(HOST)"

lock: setup ## Re-resolve requirements.lock for the VM (linux, includes vLLM)
	$(UV) pip compile pyproject.toml \
		--extra vllm \
		--python-version $(PYTHON_VERSION) \
		--python-platform x86_64-manylinux_2_28 \
		--output-file requirements.lock

clean: ## Remove the venv and tool caches
	rm -rf $(VENV) .mypy_cache .pytest_cache .ruff_cache
