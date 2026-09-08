# ollama setup — sourced by scripts/dev.sh, not executed.
#
# What configuration alone cannot express: the daemon has to be running, it has
# to have been *started* with the right concurrency limit, and the weights have
# to be pulled. `log`, `warn`, `die` and the loaded HARNESS_* values come from
# dev.sh.

# This file is *sourced* by scripts/dev.sh, never executed. Two of the linter's
# complaints are therefore structural rather than real: log/warn/die/RUN_DIR and
# the HARNESS_* values come from the sourcing script (SC2154), and
# PROFILE_SUMMARY is consumed by it after we return (SC2034). Neither direction
# is visible from this file alone.
# shellcheck shell=bash
# shellcheck disable=SC2034,SC2154

OLLAMA_PORT="${OLLAMA_PORT:-11434}"

if ! command -v ollama >/dev/null 2>&1; then
	die "ollama is not installed.

  Install it with one of:

      brew install ollama
      open https://ollama.com/download

  Then re-run. Or switch deployment with:  make profile runpod"
fi

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
	log "starting ollama serve (log: .local/ollama.log)"
	nohup ollama serve >>"$RUN_DIR/ollama.log" 2>&1 &
	for _ in $(seq 1 30); do
		if ollama_up; then break; fi
		sleep 1
	done
	ollama_up || die "ollama did not come up on :${OLLAMA_PORT} — see .local/ollama.log"
	log "ollama ready on 127.0.0.1:${OLLAMA_PORT}"
fi

# The weights. Slow once, cached after.
if [ ! -f "$HARNESS_MODELS_FILE" ]; then
	warn "no catalog at ${HARNESS_MODELS_FILE} — skipping weight pull"
else
	tags="$(sed -n 's/^[[:space:]]*model_ref:[[:space:]]*\([^[:space:]#]*\).*/\1/p' \
		"$HARNESS_MODELS_FILE")"
	if [ -z "$tags" ]; then
		warn "${HARNESS_MODELS_FILE} lists no model_ref entries — skipping weight pull"
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

export HARNESS_OLLAMA_URL="http://127.0.0.1:${OLLAMA_PORT}"
PROFILE_SUMMARY="  engine         ollama  →  http://127.0.0.1:${OLLAMA_PORT}
"
