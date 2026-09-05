#!/usr/bin/env bash
# dev-runpod.sh — run the control plane on this Mac against a RunPod GPU pod.
#
# The pod runs vLLM; we do not. That makes this the `remote_openai` backend
# (docs/BACKENDS.md §2.3), not the `vllm` one: `vllm.py` spawns and owns a local
# process, and there is no local process here. The control plane is a thin auth
# and admin layer in front of somebody else's engine, which is exactly what that
# backend is for. The desktop app cannot tell the difference — that is the rule
# BACKENDS.md exists to protect.
#
# What this means, and it is a real limitation rather than a bug: one pod serves
# one model, so /admin/models lists what the pod offers and switching between
# entries is instant and does nothing. Model switching on RunPod means a pod per
# model, or a control plane running *inside* the pod on the `vllm` backend.
# Neither is built; this script is the "get it answering" path.
#
# Safe to re-run: it reuses an existing key and regenerates the catalog.
#
#   COST: an H100 SXM pod is ~$3.52/hour and bills per millisecond whether or not
#   anything is talking to it. Ctrl-C here stops the control plane, NOT the pod.
#   Stop the pod in the RunPod console when you are done.
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

ENV_FILE="$REPO_ROOT/.env.local"
RUN_DIR="$REPO_ROOT/.local"
CATALOG="$RUN_DIR/models.runpod.yaml"
CONTROL_HOST="127.0.0.1"
CONTROL_PORT="${HARNESS_PORT:-8080}"
POD_PORT="${RUNPOD_POD_PORT:-8000}"

log() { printf '▸ %s\n' "$*"; }
die() {
	printf 'error: %s\n' "$*" >&2
	exit 1
}

# ------------------------------------------------------------ 1. the secrets
# .env.local is gitignored and holds both keys. They are different things:
# HARNESS_API_KEY authenticates the desktop app to us; HARNESS_REMOTE_API_KEY
# authenticates us to the pod. Never interchange them (CLAUDE.md rule 5).
if [ -f "$ENV_FILE" ]; then
	# shellcheck disable=SC1090  # runtime path, not resolvable at lint time
	set -a && . "$ENV_FILE" && set +a
fi

if [ -z "${HARNESS_API_KEY:-}" ]; then
	HARNESS_API_KEY="$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '=')"
	printf 'HARNESS_API_KEY=%s\n' "$HARNESS_API_KEY" >>"$ENV_FILE"
	chmod 600 "$ENV_FILE"
	log "generated a new control-plane API key into .env.local"
fi
export HARNESS_API_KEY

# ---------------------------------------------------------------- 2. the pod
# Either the full URL, or the pod id we build RunPod's proxy hostname from.
if [ -z "${HARNESS_REMOTE_BASE_URL:-}" ]; then
	if [ -z "${RUNPOD_POD_ID:-}" ]; then
		cat >&2 <<-'EOF'
			error: no pod configured.

			Set one of these, then re-run:

			    export RUNPOD_POD_ID=<the pod id from the RunPod console>
			    export HARNESS_REMOTE_BASE_URL=https://<host>:<port>

			If the pod was started with an API key set (it should be — the proxy
			URL is public HTTPS and a bearer token is the only thing standing in
			front of your GPU), also set:

			    export HARNESS_REMOTE_API_KEY=<that key>

			Anything exported here can go in .env.local instead; this script
			reads it.
		EOF
		exit 1
	fi
	HARNESS_REMOTE_BASE_URL="https://${RUNPOD_POD_ID}-${POD_PORT}.proxy.runpod.net"
fi
HARNESS_REMOTE_BASE_URL="${HARNESS_REMOTE_BASE_URL%/}"
export HARNESS_REMOTE_BASE_URL
export HARNESS_REMOTE_API_KEY="${HARNESS_REMOTE_API_KEY:-}"

if [ -z "$HARNESS_REMOTE_API_KEY" ]; then
	printf 'warning: %s\n' "no HARNESS_REMOTE_API_KEY — if the pod's port is
         reachable over the public proxy URL, anyone with the pod id can spend
         your GPU time. Set an API key on the pod and pass it here." >&2
fi

# ---------------------------------------------------------------- 3. the venv
# Needed here rather than just before uvicorn: the probe below parses the pod's
# JSON with it.
[ -x .venv/bin/python ] || make setup

# ------------------------------------------------------- 4. what it is serving
# Ask, rather than guess. `model_ref` must be exactly the id the engine answers
# to (docs/BACKENDS.md §3): get it wrong and the proxy 404s on every request,
# and the pod is the only authority on what it loaded.
log "asking ${HARNESS_REMOTE_BASE_URL} what it serves"
auth_args=()
if [ -n "$HARNESS_REMOTE_API_KEY" ]; then
	auth_args=(-H "Authorization: Bearer ${HARNESS_REMOTE_API_KEY}")
fi

# Keep the status code. `curl -f` collapses 401, 404, 502 and "no route to host"
# into one exit code, and they have four different fixes — a wrong key reported
# as "could not reach the pod" sends you to look at the wrong thing entirely.
body_file="$(mktemp)"
trap 'rm -f "$body_file"' EXIT
http_code="$(curl -sS -o "$body_file" -w '%{http_code}' --max-time 20 \
	${auth_args[@]+"${auth_args[@]}"} \
	"${HARNESS_REMOTE_BASE_URL}/v1/models" 2>/dev/null)" || true
# curl writes "000" itself when it never got a response, so a fallback here would
# concatenate onto that rather than replace it. Only an empty capture needs one.
http_code="${http_code:-000}"
models_json="$(cat "$body_file")"

case "$http_code" in
200) ;;
000)
	die "could not reach ${HARNESS_REMOTE_BASE_URL}/v1/models at all.

  The pod is stopped, the id is wrong, or you have no route to it. RunPod bills a
  running pod either way, so check the console before assuming it is up."
	;;
401 | 403)
	die "${HARNESS_REMOTE_BASE_URL} rejected the key (HTTP ${http_code}).

  The pod is up and serving — this is only about credentials. HARNESS_REMOTE_API_KEY
  must equal the --api-key in the pod's container start command.

  It is currently $([ -n "$HARNESS_REMOTE_API_KEY" ] && echo "set, and wrong" || echo "NOT SET").
  Put it in .env.local as:

      HARNESS_REMOTE_API_KEY=<the --api-key value>

  then re-run. Check it landed with:  grep -c '^HARNESS_REMOTE_API_KEY=' .env.local"
	;;
404)
	die "${HARNESS_REMOTE_BASE_URL}/v1/models is a 404.

  Something answers, but it is not an OpenAI-compatible server on this port.
  Check the pod's exposed HTTP port really is ${POD_PORT}."
	;;
5*)
	die "${HARNESS_REMOTE_BASE_URL} returned HTTP ${http_code}.

  Usually vLLM is still starting, or it died after the port opened. The container
  logs say which: 'Uvicorn running on http://0.0.0.0:${POD_PORT}' means ready."
	;;
*)
	die "${HARNESS_REMOTE_BASE_URL}/v1/models returned HTTP ${http_code}: ${models_json:0:200}"
	;;
esac

# RunPod's proxy answers with a 200 and an HTML holding page when nothing is
# listening on the port yet — so a successful curl does not mean a serving pod,
# and without this check the failure surfaces as a wall of HTML.
case "$models_json" in
*"<!DOCTYPE html>"* | *"<html"*)
	die "the pod is not serving on port ${POD_PORT} yet.

  RunPod returned its \"waiting for service to respond\" page, which means the
  container is up but nothing is listening. Almost always this is vLLM still
  downloading or loading weights — GLM 4.7 Flash is ~32 GB on a cold volume.

  Check the container logs: 'Uvicorn running on http://0.0.0.0:8000' means ready.
  A traceback means it died, and waiting will not help."
	;;
esac

# `data[0]` and nothing else: vLLM nests a `permission` array carrying its own
# "id", so any text-level scrape picks that up instead of the model. Ask a JSON
# parser for the field we actually mean.
served="$(printf '%s' "$models_json" | .venv/bin/python -c '
import json, sys
try:
    data = json.load(sys.stdin).get("data") or []
except ValueError:
    sys.exit(1)
print(data[0]["id"] if data and isinstance(data[0].get("id"), str) else "")
')" || die "${HARNESS_REMOTE_BASE_URL}/v1/models did not return JSON: ${models_json:0:200}"
[ -n "$served" ] || die "${HARNESS_REMOTE_BASE_URL}/v1/models returned no model ids: ${models_json:0:200}"

MODEL_REF="${HARNESS_REMOTE_MODEL_REF:-$served}"

# vLLM reports the context window it was actually started with (`--max-model-len`)
# in the same payload. Take it from there rather than asking anyone to keep a
# number in sync by hand: the pod is the only authority, and a catalog that
# advertises a window the engine will not honour makes the app promise something
# the engine then refuses. Absent (a provider that does not report it) means the
# field is simply omitted, which the schema allows.
CONTEXT_LEN="${HARNESS_REMOTE_CONTEXT_LEN:-$(printf '%s' "$models_json" | .venv/bin/python -c '
import json, sys
try:
    data = json.load(sys.stdin).get("data") or []
except ValueError:
    data = []
value = data[0].get("max_model_len") if data else None
print(value if isinstance(value, int) else "")
')}"
# The catalog id is what the desktop app shows and what clients send; it must be
# URL- and dropdown-friendly, so derive it from the last path segment of the HF
# repo rather than using "org/Model-Name" verbatim.
MODEL_ID="$(printf '%s' "$MODEL_REF" | tr '[:upper:]' '[:lower:]' | sed 's#.*/##; s/[^a-z0-9._-]/-/g')"
# The display name keeps the upstream's own casing: "GLM-4.7-Flash" reads better
# in the app's dropdown than the lowercased id, and the id is not shown there.
MODEL_NAME="$(printf '%s' "$MODEL_REF" | sed 's#.*/##')"

# ------------------------------------------------------------ 5. the catalog
# Generated, not committed: it describes one pod serving one model, and both
# change every time you start a pod. Regenerated on every run so it cannot go
# stale against the pod that is actually up.
mkdir -p "$RUN_DIR"
CONTEXT_LINE=""
[ -n "$CONTEXT_LEN" ] && CONTEXT_LINE="    context_length: ${CONTEXT_LEN}"
cat >"$CATALOG" <<EOF
# Generated by scripts/dev-runpod.sh — do not edit; re-run the script.
# One pod, one model, on the remote_openai backend (docs/BACKENDS.md §2.3).
defaults:
  backend: remote_openai

models:
  - id: ${MODEL_ID}
    display_name: ${MODEL_NAME}
    backend: remote_openai
    model_ref: ${MODEL_REF}
${CONTEXT_LINE}
    # Nothing loads on activation — the pod already has the weights resident.
    # The honest number is 0; the load happened when the pod started.
    estimated_load_seconds: 0
EOF

# ------------------------------------------------------- 6. the control plane
if curl -fsS -o /dev/null --max-time 2 "http://${CONTROL_HOST}:${CONTROL_PORT}/healthz"; then
	die "something is already serving ${CONTROL_HOST}:${CONTROL_PORT} — stop it first"
fi

export HARNESS_DEFAULT_BACKEND=remote_openai
export HARNESS_MODELS_FILE="$CATALOG"
export HARNESS_STATE_DIR="$RUN_DIR"
export HARNESS_AUTOLOAD_MODEL="${HARNESS_AUTOLOAD_MODEL:-$MODEL_ID}"

cat <<EOF

  control plane  http://${CONTROL_HOST}:${CONTROL_PORT}
  backend        remote_openai  →  ${HARNESS_REMOTE_BASE_URL}
  catalog        ${CATALOG}  (generated)
  model          ${MODEL_ID}   → served upstream as ${MODEL_REF}
  context        ${CONTEXT_LEN:-not reported by the upstream}
  upstream key   $([ -n "$HARNESS_REMOTE_API_KEY" ] && echo "set" || echo "NOT SET — the pod is unauthenticated")
  API key        ${HARNESS_API_KEY}   (also in .env.local)

  Streaming smoke test (SPEC §7.1 L3) — paste into another terminal:

    curl -N --trace-time -s http://${CONTROL_HOST}:${CONTROL_PORT}/v1/chat/completions \\
      -H 'Authorization: Bearer ${HARNESS_API_KEY}' \\
      -H 'Content-Type: application/json' \\
      -d '{"model":"${MODEL_ID}","stream":true,"messages":[{"role":"user","content":"Count to twenty slowly."}]}'

  In the desktop app: Server URL http://${CONTROL_HOST}:${CONTROL_PORT}, and the API key above.

  Ctrl-C stops the control plane. It does NOT stop the pod — that keeps billing
  at ~\$3.52/hour until you stop it in the RunPod console.

EOF

exec .venv/bin/uvicorn harness_control.app:app \
	--host "$CONTROL_HOST" --port "$CONTROL_PORT" --reload
