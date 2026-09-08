#!/usr/bin/env bash
# dev-local.sh — kept as the name people already type and the docs already cite.
#
# The real entry point is scripts/dev.sh, which runs whichever deployment profile
# is selected. This is the `ollama` one: local Apple Silicon, no cloud, no cost.
#
#     make dev                  # the active profile
#     make profile ollama       # make this one the active profile
set -euo pipefail
exec "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/dev.sh" ollama "$@"
