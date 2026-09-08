#!/usr/bin/env bash
# dev.sh — run the control plane against whichever deployment is selected.
#
# One entry point for all three backends. Which one you get is a profile:
#
#     make dev                  # the active profile (.harness-profile)
#     make dev PROFILE=runpod   # override for one run
#     make profile runpod       # change the active profile
#
# A profile is two things:
#
#   deploy/profiles/<name>.env   values — literal KEY=value, no secrets
#   scripts/profiles/<name>.sh   the setup that config alone cannot express
#
# The setup half exists because the three deployments differ in *work*, not just
# in values: ollama needs its daemon started with OLLAMA_MAX_LOADED_MODELS=1 and
# tags pulled, runpod needs the pod probed and a catalog generated, and vllm
# needs a provisioned GPU box. A hook is optional — a profile that is pure
# configuration simply has none.
#
# Precedence, strongest first: your shell environment, then .env.local, then the
# profile. So `HARNESS_PORT=8188 make dev` wins over both files, and .env.local
# (your machine, your secrets) wins over the committed profile.
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PROFILE_DIR="$REPO_ROOT/deploy/profiles"
HOOK_DIR="$REPO_ROOT/scripts/profiles"
ACTIVE_FILE="$REPO_ROOT/.harness-profile"
ENV_FILE="$REPO_ROOT/.env.local"
RUN_DIR="$REPO_ROOT/.local"
DEFAULT_PROFILE=ollama

log() { printf '▸ %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() {
	printf 'error: %s\n' "$*" >&2
	exit 1
}

profiles_available() {
	local path
	for path in "$PROFILE_DIR"/*.env; do
		[ -e "$path" ] || continue
		basename "$path" .env
	done
}

# ------------------------------------------------------------- the env loader
# Literal KEY=value only — no expansion, no command substitution. That keeps a
# profile file valid as a systemd `EnvironmentFile=` on a real VM, which is why
# the precedence lives here rather than as `${VAR:-default}` in the files.
load_env_file() {
	local path="$1" line key value name
	[ -f "$path" ] || return 0
	name="$(basename "$path")"
	while IFS= read -r line || [ -n "$line" ]; do
		case "$line" in '' | '#'*) continue ;; esac
		key="${line%%=*}"
		value="${line#*=}"
		# Trim surrounding whitespace on the key; reject anything that is not a
		# plain variable name rather than eval'ing whatever the file contains.
		key="$(printf '%s' "$key" | tr -d '[:space:]')"
		case "$key" in
		'' | *[!A-Za-z0-9_]* | [0-9]*)
			warn "${name}: ignoring unparseable line: ${line:0:40}"
			continue
			;;
		esac
		# Strip one layer of matching quotes, the way an env file usually means.
		case "$value" in
		\"*\") value="${value#\"}" && value="${value%\"}" ;;
		\'*\') value="${value#\'}" && value="${value%\'}" ;;
		esac
		# Only if unset: the caller's environment always wins.
		if eval "[ -z \"\${$key+x}\" ]"; then
			eval "export $key=\"\$value\""
		fi
	done <"$path"
}

# ---------------------------------------------------------- which profile?
PROFILE="${PROFILE:-${1:-}}"
if [ -z "$PROFILE" ] && [ -f "$ACTIVE_FILE" ]; then
	PROFILE="$(tr -d '[:space:]' <"$ACTIVE_FILE")"
fi
PROFILE="${PROFILE:-$DEFAULT_PROFILE}"

PROFILE_FILE="$PROFILE_DIR/${PROFILE}.env"
[ -f "$PROFILE_FILE" ] || die "no such profile: '${PROFILE}'.

  Available: $(profiles_available | tr '\n' ' ')

  Select one with:  make profile <name>
  Or for one run:   make dev PROFILE=<name>"

log "profile: ${PROFILE}"

# ----------------------------------------------------------------- the config
# .env.local first so your machine's values beat the committed profile's.
load_env_file "$ENV_FILE"
load_env_file "$PROFILE_FILE"

CONTROL_HOST="${HARNESS_HOST:-127.0.0.1}"
CONTROL_PORT="${HARNESS_PORT:-8080}"
mkdir -p "$RUN_DIR"

# ---------------------------------------------------------------- the API key
# Generated once into .env.local, which is gitignored. Never committed, never
# logged — logging_config.py's redaction filter scrubs it from every record.
if [ -z "${HARNESS_API_KEY:-}" ]; then
	HARNESS_API_KEY="$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '=')"
	printf 'HARNESS_API_KEY=%s\n' "$HARNESS_API_KEY" >>"$ENV_FILE"
	chmod 600 "$ENV_FILE"
	log "generated a new API key into .env.local"
fi
export HARNESS_API_KEY

# ------------------------------------------------------------------ the venv
[ -x .venv/bin/python ] || make setup

# ------------------------------------------------------------------ the hook
# Sourced, not executed, so what it exports reaches uvicorn and so it can use
# log/warn/die above. It may set HARNESS_* values the profile could not know —
# a discovered model id, a generated catalog path, a probed base URL.
PROFILE_SUMMARY=""
HOOK="$HOOK_DIR/${PROFILE}.sh"
if [ -f "$HOOK" ]; then
	# shellcheck source=/dev/null  # chosen at runtime
	. "$HOOK"
else
	log "no setup hook for '${PROFILE}' — configuration only"
fi

# --------------------------------------------------------- the control plane
[ -n "${HARNESS_MODELS_FILE:-}" ] || die "the '${PROFILE}' profile set no HARNESS_MODELS_FILE"
[ -f "$HARNESS_MODELS_FILE" ] || die "catalog not found: ${HARNESS_MODELS_FILE}
  The '${PROFILE}' profile points at it, but nothing has created it."

if curl -fsS -o /dev/null --max-time 2 "http://${CONTROL_HOST}:${CONTROL_PORT}/healthz"; then
	die "something is already serving ${CONTROL_HOST}:${CONTROL_PORT} — stop it first"
fi

first_model="$(sed -n 's/^[[:space:]]*-[[:space:]]*id:[[:space:]]*\([^[:space:]#]*\).*/\1/p' \
	"$HARNESS_MODELS_FILE" 2>/dev/null | head -n1)"
export HARNESS_AUTOLOAD_MODEL="${HARNESS_AUTOLOAD_MODEL:-$first_model}"

cat <<EOF

  profile        ${PROFILE}
  control plane  http://${CONTROL_HOST}:${CONTROL_PORT}
  backend        ${HARNESS_DEFAULT_BACKEND:-?}
  catalog        ${HARNESS_MODELS_FILE}
  autoload       ${HARNESS_AUTOLOAD_MODEL:-(nothing)}
${PROFILE_SUMMARY}  API key        ${HARNESS_API_KEY}   (also in .env.local)

  Streaming smoke test (SPEC §7.1 L3) — paste into another terminal:

    curl -N --trace-time -s http://${CONTROL_HOST}:${CONTROL_PORT}/v1/chat/completions \\
      -H 'Authorization: Bearer ${HARNESS_API_KEY}' \\
      -H 'Content-Type: application/json' \\
      -d '{"model":"${HARNESS_AUTOLOAD_MODEL}","stream":true,"messages":[{"role":"user","content":"Count to twenty slowly."}]}'

  Or check everything at once:  ./scripts/smoke.sh

EOF

exec .venv/bin/uvicorn harness_control.app:app \
	--host "$CONTROL_HOST" --port "$CONTROL_PORT" --reload
