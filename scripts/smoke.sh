#!/usr/bin/env bash
# smoke.sh — exercise SPEC §7.1 L1–L11 against a *running* deployment.
#
# This is what you run after touching the deployment, and before tagging a
# release. It talks to the control plane over HTTP only: nothing here imports the
# package, reads the repo, or assumes it is on the same machine — except the two
# checks that genuinely need the Ollama daemon, which say so and skip.
#
#     ./scripts/smoke.sh                          # localhost, key from .env.local
#     ./scripts/smoke.sh https://harness.example.com KEY
#     ./scripts/smoke.sh --disruptive             # also run L8 (kills ollama)
#
# Exits non-zero if any criterion fails. A SKIP is not a failure, but it is
# printed loudly: a silent omission would read as coverage it does not have.
set -euo pipefail

HOST=""
KEY=""
OLLAMA_URL="${OLLAMA_URL:-http://127.0.0.1:11434}"
DISRUPTIVE=0

while [ $# -gt 0 ]; do
	case "$1" in
	--disruptive) DISRUPTIVE=1 ;;
	--ollama)
		OLLAMA_URL="$2"
		shift
		;;
	-h | --help)
		sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
		exit 0
		;;
	-*)
		printf 'error: unknown option %s\n' "$1" >&2
		exit 2
		;;
	*)
		if [ -z "$HOST" ]; then HOST="$1"; else KEY="$1"; fi
		;;
	esac
	shift
done

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="${HOST:-${HARNESS_HOST:-http://127.0.0.1:8080}}"
HOST="${HOST%/}"

if [ -z "$KEY" ] && [ -z "${HARNESS_API_KEY:-}" ] && [ -f "$REPO_ROOT/.env.local" ]; then
	# shellcheck disable=SC1091  # runtime path, not resolvable at lint time
	set -a && . "$REPO_ROOT/.env.local" && set +a
fi
KEY="${KEY:-${HARNESS_API_KEY:-}}"
if [ -z "$KEY" ]; then
	printf 'error: no API key. Pass it as the second argument or put HARNESS_API_KEY in .env.local\n' >&2
	exit 2
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

FAILURES=0
SKIPS=0

pass() { printf '  \033[32mPASS\033[0m  %-4s %s\n' "$1" "$2"; }
fail() {
	printf '  \033[31mFAIL\033[0m  %-4s %s\n' "$1" "$2"
	FAILURES=$((FAILURES + 1))
}
skip() {
	printf '  \033[33mSKIP\033[0m  %-4s %s\n' "$1" "$2"
	SKIPS=$((SKIPS + 1))
}
note() { printf '        %s\n' "$*"; }

# ---------------------------------------------------------------- helpers
api() { curl -fsS --max-time 15 -H "Authorization: Bearer $KEY" "$@"; }
status_of() { curl -s -o "$1" -w '%{http_code}' --max-time 15 "${@:2}"; }

jqp() { python3 -c 'import json,sys;d=json.load(sys.stdin);print(eval(sys.argv[1],{"d":d,"json":json}))' "$1"; }

state_now() { api "$HOST/admin/state" | jqp 'd["state"]'; }
active_now() { api "$HOST/admin/state" | jqp 'd["active_model_id"] or ""'; }

wait_for_state() {
	local want="$1" deadline=$((SECONDS + ${2:-120}))
	while [ "$SECONDS" -lt "$deadline" ]; do
		[ "$(state_now)" = "$want" ] && return 0
		sleep 1
	done
	return 1
}

# Number of models the Ollama daemon currently holds. Prints "?" if unreachable.
resident_count() {
	curl -fsS --max-time 3 "$OLLAMA_URL/api/ps" 2>/dev/null |
		python3 -c 'import json,sys;print(len(json.load(sys.stdin).get("models",[])))' 2>/dev/null || printf '?'
}

ollama_reachable() { curl -fsS -o /dev/null --max-time 3 "$OLLAMA_URL/api/version" 2>/dev/null; }

completion_body() {
	printf '{"model":"%s","stream":%s,"max_tokens":%s,"messages":[{"role":"user","content":"Count to twenty slowly."}]}' \
		"$1" "$2" "${3:-64}"
}

printf '\nharness smoke  →  %s\n\n' "$HOST"

# ------------------------------------------------------------------- L1
if [ "$(status_of "$TMP/health" "$HOST/healthz")" = "200" ]; then
	pass L1 "control plane is up; /healthz returns 200"
else
	fail L1 "/healthz did not return 200 — nothing else can be trusted"
	printf '\nAborting: the control plane is not answering at %s\n' "$HOST" >&2
	exit 1
fi

# ------------------------------------------------------------------- L2
unauth_admin="$(status_of /dev/null "$HOST/admin/state")"
unauth_v1="$(status_of /dev/null -X POST "$HOST/v1/chat/completions" \
	-H 'Content-Type: application/json' -d "$(completion_body x false 1)")"
if [ "$unauth_admin" = "401" ] && [ "$unauth_v1" = "401" ]; then
	pass L2 "unauthenticated /admin and /v1 both return 401"
else
	fail L2 "expected 401/401, got admin=$unauth_admin v1=$unauth_v1"
fi

# --------------------------------------------------------- catalog + a target
api "$HOST/admin/models" >"$TMP/catalog.json"
MODEL_IDS=()
while IFS= read -r id; do
	[ -n "$id" ] && MODEL_IDS+=("$id")
done < <(jqp '"\n".join(m["id"] for m in d["models"])' <"$TMP/catalog.json")
if [ "${#MODEL_IDS[@]}" -eq 0 ]; then
	fail L4 "the catalog is empty — no model to activate"
	printf '\nAborting: /admin/models lists nothing\n' >&2
	exit 1
fi

# A baseline: everything after this assumes exactly one model is ready. A plane
# that has just started is usually mid-autoload, so settle before deciding.
case "$(state_now)" in
stopping | loading)
	note "an activation is already in flight; waiting for it"
	wait_for_state ready 300 || {
		fail L4 "the in-flight activation never reached ready (state: $(state_now))"
		exit 1
	}
	;;
esac

ACTIVE="$(active_now)"
if [ -z "$ACTIVE" ]; then
	note "nothing is active; activating ${MODEL_IDS[0]} to get a baseline"
	baseline_code="$(status_of /dev/null -X POST -H "Authorization: Bearer $KEY" \
		"$HOST/admin/models/${MODEL_IDS[0]}/activate")"
	case "$baseline_code" in
	202 | 409) : ;; # 409 = someone else got there first; waiting is still right
	*)
		fail L4 "could not start a baseline activation (HTTP $baseline_code)"
		exit 1
		;;
	esac
	wait_for_state ready 300 || {
		fail L4 "could not reach a ready baseline (state: $(state_now))"
		exit 1
	}
	ACTIVE="$(active_now)"
fi

# The switch target: any catalogued model that is not the active one.
TARGET=""
for id in "${MODEL_IDS[@]}"; do
	[ "$id" != "$ACTIVE" ] && TARGET="$id" && break
done

# ------------------------------------------------------------------- L3
# Streaming has to be checked by *arrival time*, not by final text: a fully
# buffered response produces byte-identical output. --trace-time stamps each read.
curl -N --trace-time --no-buffer -sS --max-time 60 \
	-H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
	-o "$TMP/stream.out" --trace-ascii "$TMP/stream.trace" \
	-X POST "$HOST/v1/chat/completions" \
	-d "$(completion_body "$ACTIVE" true 96)" || true

python3 - "$TMP/stream.trace" >"$TMP/l3" <<'PY' || true
import re, sys
from datetime import datetime

stamps = []
for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
    # curl --trace-time prefixes each trace line with HH:MM:SS.microseconds.
    m = re.match(r"^(\d\d:\d\d:\d\d\.\d+) <= Recv data", line)
    if m:
        stamps.append(datetime.strptime(m.group(1), "%H:%M:%S.%f"))
if len(stamps) < 2:
    print(f"chunks={len(stamps)} span=0")
else:
    print(f"chunks={len(stamps)} span={(stamps[-1] - stamps[0]).total_seconds():.3f}")
PY
read -r l3_chunks l3_span <"$TMP/l3" || true
chunks="${l3_chunks#chunks=}"
span="${l3_span#span=}"
frames="$(grep -c '^data: ' "$TMP/stream.out" || true)"

if [ "${chunks:-0}" -lt 2 ]; then
	fail L3 "the whole reply arrived in one read — something is buffering ($frames frames, $chunks reads)"
	note "check: httpx aiter_raw, no compression middleware on /v1, Caddy flush_interval -1"
elif python3 -c "import sys;sys.exit(0 if float('$span') > 0.05 else 1)"; then
	pass L3 "streaming is incremental: $frames frames over $chunks reads spanning ${span}s"
else
	fail L3 "$chunks reads but they span only ${span}s — effectively one blob"
fi

# ------------------------------------------------------------------- L7
# Ask for a model that exists in the catalog but is not the active one.
if [ -n "$TARGET" ]; then
	code="$(status_of "$TMP/409.json" -X POST "$HOST/v1/chat/completions" \
		-H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
		-d "$(completion_body "$TARGET" false 8)")"
	err="$(jqp 'd["error"]["code"]' <"$TMP/409.json" 2>/dev/null || printf '?')"
	detail="$(jqp 'str(d["error"].get("details",{}))' <"$TMP/409.json" 2>/dev/null || printf '')"
	if [ "$code" = "409" ] && [ "$err" = "model_not_active" ] && [[ "$detail" == *"$ACTIVE"* ]]; then
		pass L7 "an inactive model returns 409 model_not_active naming '$ACTIVE'"
	else
		fail L7 "expected 409/model_not_active with the active id, got $code/$err $detail"
	fi
else
	skip L7 "the catalog has only one model, so nothing is 'not active'"
fi

# ------------------------------------------------------------------- L11
gpu="$(api "$HOST/admin/state" | jqp 'json.dumps(d["gpu"])')"
if printf '%s' "$gpu" | python3 -c 'import json,sys;sys.exit(0 if isinstance(json.load(sys.stdin),list) else 1)'; then
	pass L11 "/admin/state.gpu is a list and is handled: $gpu"
else
	fail L11 "/admin/state.gpu is not a list: $gpu"
fi

# ------------------------------------- L4 / L5 / L6 / L10: one observed switch
if [ -z "$TARGET" ]; then
	skip L4 "only one model in the catalog — nothing to switch to"
	skip L5 "no switch to observe"
	skip L6 "no switch to observe"
	skip L10 "no switch to observe"
else
	advertised="$(jqp "[m['estimated_load_seconds'] for m in d['models'] if m['id']=='$TARGET'][0]" <"$TMP/catalog.json")"
	before_resident="$(resident_count)"

	switch_code="$(status_of "$TMP/activate.json" -X POST -H "Authorization: Bearer $KEY" \
		"$HOST/admin/models/$TARGET/activate")"
	started_at="$SECONDS"


	# --- everything below happens *while the switch is in flight* -------------
	max_resident=0
	l6_code="" l6_err="" l6_retry_after=""
	l10_code="" l10_err=""

	while [ "$((SECONDS - started_at))" -lt $((advertised + 120)) ]; do
		st="$(state_now)"

		n="$(resident_count)"
		if [ "$n" != "?" ] && [ "$n" -gt "$max_resident" ]; then max_resident="$n"; fi

		# L6: a completion aimed at the model being loaded, mid-switch.
		if [ -z "$l6_code" ] && [ "$st" = "loading" ]; then
			l6_code="$(curl -s -o "$TMP/503.json" -D "$TMP/503.head" -w '%{http_code}' --max-time 10 \
				-X POST "$HOST/v1/chat/completions" -H "Authorization: Bearer $KEY" \
				-H 'Content-Type: application/json' -d "$(completion_body "$TARGET" false 8)")"
			l6_err="$(jqp 'd["error"]["code"]' <"$TMP/503.json" 2>/dev/null || printf '?')"
			l6_retry_after="$(grep -i '^retry-after:' "$TMP/503.head" | tr -d '\r' | awk '{print $2}')"
		fi

		# L10: a second activation while the first is still running.
		if [ -z "$l10_code" ] && { [ "$st" = "loading" ] || [ "$st" = "stopping" ]; }; then
			l10_code="$(status_of "$TMP/l10.json" -X POST -H "Authorization: Bearer $KEY" \
				"$HOST/admin/models/${MODEL_IDS[0]}/activate")"
			l10_err="$(jqp 'd["error"]["code"]' <"$TMP/l10.json" 2>/dev/null || printf '?')"
		fi

		case "$st" in ready | error) break ;; esac
	done

	elapsed=$((SECONDS - started_at))
	final_state="$(state_now)"

	if [ "$switch_code" != "202" ]; then
		fail L4 "activate returned $switch_code, expected 202"
	elif [ "$final_state" = "ready" ] && [ "$(active_now)" = "$TARGET" ]; then
		if [ "$elapsed" -le $((advertised * 2 + 30)) ]; then
			pass L4 "202, then $ACTIVE → $TARGET in ${elapsed}s (advertised ${advertised}s)"
		else
			fail L4 "switched, but took ${elapsed}s against an advertised ${advertised}s"
		fi
	else
		fail L4 "switch ended in '$final_state' (active: $(active_now))"
		note "run: curl -H 'Authorization: Bearer \$KEY' $HOST/admin/logs?source=control"
	fi

	# --- L5 -------------------------------------------------------------------
	after_resident="$(resident_count)"
	if [ "$after_resident" = "?" ]; then
		skip L5 "the Ollama daemon at $OLLAMA_URL is not reachable from here"
		note "L5 is the only check that catches two models silently coexisting; run it on the host"
	elif [ "$max_resident" -le 1 ] && [ "$after_resident" = "1" ]; then
		pass L5 "exactly one model resident during the switch (peak $max_resident) and after"
	elif [ "$max_resident" -gt 1 ]; then
		fail L5 "$max_resident models were resident at once during the switch"
		note "the machine has room for both, so this fails silently: check OLLAMA_MAX_LOADED_MODELS=1"
	else
		fail L5 "$after_resident models resident after the switch, expected exactly 1 (was $before_resident before)"
	fi

	# --- L6 -------------------------------------------------------------------
	if [ -z "$l6_code" ]; then
		skip L6 "never caught the switch in 'loading' — too fast to sample"
	elif [ "$l6_code" = "503" ] && [ "$l6_err" = "model_loading" ] && [ -n "$l6_retry_after" ]; then
		pass L6 "mid-switch completion returned 503 model_loading, Retry-After: $l6_retry_after"
	else
		fail L6 "expected 503/model_loading with Retry-After, got $l6_code/$l6_err retry-after='$l6_retry_after'"
	fi

	# --- L10 ------------------------------------------------------------------
	if [ -z "$l10_code" ]; then
		skip L10 "never caught the switch in flight — too fast to sample"
	elif [ "$l10_code" = "409" ] && [ "$l10_err" = "activation_in_progress" ]; then
		pass L10 "a concurrent activation returned 409 activation_in_progress"
	else
		fail L10 "expected 409/activation_in_progress, got $l10_code/$l10_err"
	fi
fi

# ------------------------------------------------------------------- L9
# A disconnect is only interesting if the *upstream* request goes with it. The
# control plane logs that at INFO, so the log is the observable.
now_model="$(active_now)"

# Count first: the ring buffer is shared, and an abort logged earlier in this
# same run (or by the desktop app) would make a broken proxy look correct.
aborts_before="$(api "$HOST/admin/logs?lines=200&source=control" | jqp 'sum("aborted by the client" in l for l in d["lines"])')"

curl -N -sS --max-time 30 -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
	-X POST "$HOST/v1/chat/completions" -d "$(completion_body "$now_model" true 512)" \
	>"$TMP/l9.out" 2>/dev/null &
l9_pid=$!
sleep 2
kill "$l9_pid" 2>/dev/null || true
wait "$l9_pid" 2>/dev/null || true
sleep 2 # L9's budget: the cancel must have happened inside this window

aborts_after="$(api "$HOST/admin/logs?lines=200&source=control" | jqp 'sum("aborted by the client" in l for l in d["lines"])')"
if [ "$aborts_after" -gt "$aborts_before" ]; then
	pass L9 "a client disconnect cancelled the upstream request within 2s"
else
	fail L9 "no 'aborted by the client' in the control-plane log after a disconnect"
	note "the upstream request may still be generating — check proxy.py's finally: resp.aclose()"
fi

# ------------------------------------------------------------------- L8
if [ "$DISRUPTIVE" -ne 1 ]; then
	skip L8 "kills the Ollama daemon — re-run with --disruptive to include it"
elif ! ollama_reachable; then
	skip L8 "the Ollama daemon at $OLLAMA_URL is not reachable from here"
else
	pkill -f 'ollama serve' 2>/dev/null || true
	if wait_for_state error 15; then
		if [ "$(status_of /dev/null "$HOST/healthz")" = "200" ]; then
			pass L8 "the daemon died, state went to 'error', the control plane stayed up"
		else
			fail L8 "state reached 'error' but the control plane went down with the daemon"
		fi
	else
		fail L8 "the daemon is gone but /admin/state is still '$(state_now)'"
	fi
	note "ADR-0005: there is no automatic restart. Bring ollama back, then activate a model."
fi

# ------------------------------------------------------------------- verdict
printf '\n'
if [ "$FAILURES" -eq 0 ]; then
	printf '  all green (%d skipped)\n\n' "$SKIPS"
	exit 0
fi
printf '  %d failed, %d skipped\n\n' "$FAILURES" "$SKIPS"
exit 1
