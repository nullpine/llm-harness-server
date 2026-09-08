# runpod setup — sourced by scripts/dev.sh, not executed.
#
# What configuration alone cannot express: which pod, and what it is serving.
# The pod id changes every deployment, and `model_ref` must be exactly the name
# the engine answers to (docs/BACKENDS.md §3) — get it wrong and the proxy 404s
# on every request. The pod is the only authority on that, so we ask it rather
# than guess, and generate the catalog from the answer.

# This file is *sourced* by scripts/dev.sh, never executed. Two of the linter's
# complaints are therefore structural rather than real: log/warn/die/RUN_DIR and
# the HARNESS_* values come from the sourcing script (SC2154), and
# PROFILE_SUMMARY is consumed by it after we return (SC2034). Neither direction
# is visible from this file alone.
# shellcheck shell=bash
# shellcheck disable=SC2034,SC2154

POD_PORT="${RUNPOD_POD_PORT:-8000}"

# ---------------------------------------------------------------- the pod URL
if [ -z "${HARNESS_REMOTE_BASE_URL:-}" ]; then
	if [ -z "${RUNPOD_POD_ID:-}" ]; then
		die "no pod configured.

  Set one of these, then re-run:

      export RUNPOD_POD_ID=<the pod id from the RunPod console>
      export HARNESS_REMOTE_BASE_URL=https://<host>:<port>

  If the pod was started with an API key set (it should be — the proxy URL is
  public HTTPS and a bearer token is the only thing standing in front of your
  GPU), also set:

      export HARNESS_REMOTE_API_KEY=<that key>

  Anything exported here can go in .env.local instead; dev.sh reads it."
	fi
	HARNESS_REMOTE_BASE_URL="https://${RUNPOD_POD_ID}-${POD_PORT}.proxy.runpod.net"
fi
HARNESS_REMOTE_BASE_URL="${HARNESS_REMOTE_BASE_URL%/}"
export HARNESS_REMOTE_BASE_URL
export HARNESS_REMOTE_API_KEY="${HARNESS_REMOTE_API_KEY:-}"

if [ -z "$HARNESS_REMOTE_API_KEY" ]; then
	warn "no HARNESS_REMOTE_API_KEY — if the pod's port is reachable over the
         public proxy URL, anyone with the pod id can spend your GPU time. Set
         an API key on the pod and put it in .env.local."
fi

# ------------------------------------------------------- what it is serving
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

  Most likely the pod is STOPPED — RunPod's proxy 404s for a pod that is not
  running, rather than refusing the connection. Check the console first; a
  stopped pod costs only its storage, so it is easy to leave stopped and forget.

  Failing that, something answers on this port but is not an OpenAI-compatible
  server: check the pod's exposed HTTP port really is ${POD_PORT}."
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
  downloading or loading weights — a 30B model is tens of GB on a cold volume.

  Check the container logs: 'Uvicorn running on http://0.0.0.0:${POD_PORT}' means
  ready. A traceback means it died, and waiting will not help."
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
# the engine then refuses.
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

# ------------------------------------------------------------------ the catalog
# Generated, not committed: it describes one pod serving one model, and both
# change every time you start a pod. Regenerated on every run so it cannot go
# stale against the pod that is actually up.
mkdir -p "$(dirname "$HARNESS_MODELS_FILE")"
CONTEXT_LINE=""
[ -n "$CONTEXT_LEN" ] && CONTEXT_LINE="    context_length: ${CONTEXT_LEN}"
cat >"$HARNESS_MODELS_FILE" <<EOF
# Generated by scripts/profiles/runpod.sh — do not edit; re-run \`make dev\`.
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

PROFILE_SUMMARY="  pod            ${HARNESS_REMOTE_BASE_URL}
  model          ${MODEL_ID}   → served upstream as ${MODEL_REF}
  context        ${CONTEXT_LEN:-not reported by the upstream}
  upstream key   $([ -n "$HARNESS_REMOTE_API_KEY" ] && echo "set" || echo "NOT SET — the pod is unauthenticated")
  cost           the pod bills whether or not anything is talking to it;
                 Ctrl-C here does NOT stop it — use the RunPod console
"
