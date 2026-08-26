#!/usr/bin/env bash
# dev-local.sh — run the harness on this Mac against the `ollama` backend.
#
# This is the MVP path (SPEC §4.3). No Azure, no Caddy, no systemd, no cost.
# Safe to re-run: it reuses a running daemon, an existing key, and pulled weights.
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

MODELS_FILE="${HARNESS_MODELS_FILE:-deploy/config/models.yaml}"
ENV_FILE="$REPO_ROOT/.env"
RUN_DIR="$REPO_ROOT/.local"
OLLAMA_PORT="${OLLAMA_PORT:-11434}"
CONTROL_HOST="127.0.0.1"
CONTROL_PORT="${HARNESS_PORT:-8080}"

log() { printf '▸ %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() {
	printf 'error: %s\n' "$*" >&2
	exit 1
}

# ---------------------------------------------------------------- 1. ollama
if ! command -v ollama >/dev/null 2>&1; then
	cat >&2 <<-'EOF'
		error: ollama is not installed.

		Install it with one of:

		    brew install ollama
		    open https://ollama.com/download

		Then re-run scripts/dev-local.sh.
	EOF
	exit 1
fi

# ------------------------------------------------------------- 2. the daemon
# Ollama defaults to three concurrent models, which would silently break the
# single-active-model invariant. It is read at daemon start, so it has to be in
# the environment *before* `ollama serve` — see docs/BACKENDS.md §2.1.
export OLLAMA_MAX_LOADED_MODELS=1

ollama_up() {
	curl -fsS -o /dev/null --max-time 2 "http://127.0.0.1:${OLLAMA_PORT}/api/version"
}

if ollama_up; then
	log "ollama already listening on 127.0.0.1:${OLLAMA_PORT}"
	warn "this script did not start that daemon, so it cannot guarantee
         OLLAMA_MAX_LOADED_MODELS=1. If you started it by hand or via Ollama.app,
         stop it and re-run this script, or run:
             launchctl setenv OLLAMA_MAX_LOADED_MODELS 1"
else
	mkdir -p "$RUN_DIR"
	log "starting ollama serve (log: .local/ollama.log)"
	nohup ollama serve >>"$RUN_DIR/ollama.log" 2>&1 &
	for _ in $(seq 1 30); do
		if ollama_up; then break; fi
		sleep 1
	done
	ollama_up || die "ollama did not come up on :${OLLAMA_PORT} — see .local/ollama.log"
	log "ollama ready on 127.0.0.1:${OLLAMA_PORT}"
fi

# ------------------------------------------------------------ 3. the weights
if [ ! -f "$MODELS_FILE" ]; then
	warn "no catalog at ${MODELS_FILE} — skipping weight pull"
else
	tags="$(sed -n 's/^[[:space:]]*model_ref:[[:space:]]*\([^[:space:]#]*\).*/\1/p' "$MODELS_FILE")"
	if [ -z "$tags" ]; then
		warn "${MODELS_FILE} lists no model_ref entries — skipping weight pull"
	fi
	while IFS= read -r tag; do
		[ -n "$tag" ] || continue
		if ollama show "$tag" >/dev/null 2>&1; then
			log "have ${tag}"
		else
			log "pulling ${tag} (this is the slow step; weights are cached after)"
			ollama pull "$tag"
		fi
	done <<<"$tags"
fi

# ---------------------------------------------------------------- 4. the key
# Generated once into .env, which is gitignored. Never committed, never logged.
if [ -f "$ENV_FILE" ]; then
	# shellcheck disable=SC1090  # runtime path, not resolvable at lint time
	set -a && . "$ENV_FILE" && set +a
fi
if [ -z "${HARNESS_API_KEY:-}" ]; then
	HARNESS_API_KEY="$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '=')"
	printf 'HARNESS_API_KEY=%s\n' "$HARNESS_API_KEY" >>"$ENV_FILE"
	chmod 600 "$ENV_FILE"
	log "generated a new API key into .env"
fi
export HARNESS_API_KEY

# -------------------------------------------------------- 5. the control plane
[ -x .venv/bin/uvicorn ] || make setup

if curl -fsS -o /dev/null --max-time 2 "http://${CONTROL_HOST}:${CONTROL_PORT}/healthz"; then
	die "something is already serving ${CONTROL_HOST}:${CONTROL_PORT} — stop it first"
fi

export HARNESS_DEFAULT_BACKEND=ollama
export HARNESS_MODELS_FILE="$MODELS_FILE"
export HARNESS_OLLAMA_BASE_URL="http://127.0.0.1:${OLLAMA_PORT}"

first_model="$(sed -n 's/^[[:space:]]*-[[:space:]]*id:[[:space:]]*\([^[:space:]#]*\).*/\1/p' \
	"$MODELS_FILE" 2>/dev/null | head -n1)"
first_model="${first_model:-glm-4.7-flash}"

cat <<EOF

  control plane  http://${CONTROL_HOST}:${CONTROL_PORT}
  backend        ollama  →  http://127.0.0.1:${OLLAMA_PORT}
  catalog        ${MODELS_FILE}
  API key        ${HARNESS_API_KEY}

  Streaming smoke test (SPEC §7.1 L3) — paste into another terminal:

    curl -N --trace-time -s http://${CONTROL_HOST}:${CONTROL_PORT}/v1/chat/completions \\
      -H 'Authorization: Bearer ${HARNESS_API_KEY}' \\
      -H 'Content-Type: application/json' \\
      -d '{"model":"${first_model}","stream":true,"messages":[{"role":"user","content":"Count to twenty slowly."}]}'

  Ctrl-C stops the control plane. The ollama daemon keeps running; stop it with
  \`pkill -f "ollama serve"\` if you want the memory back.

EOF

exec .venv/bin/uvicorn harness_control.app:app \
	--host "$CONTROL_HOST" --port "$CONTROL_PORT" --reload
