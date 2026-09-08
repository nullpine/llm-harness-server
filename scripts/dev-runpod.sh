#!/usr/bin/env bash
# dev-runpod.sh — kept as the name people already type and the docs already cite.
#
# The real entry point is scripts/dev.sh, which runs whichever deployment profile
# is selected. This is the `runpod` one: vLLM on a rented GPU pod, reached over
# HTTPS on the remote_openai backend.
#
#     make dev                  # the active profile
#     make profile runpod       # make this one the active profile
set -euo pipefail
exec "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/dev.sh" runpod "$@"
