#!/usr/bin/env bash
# profile.sh — show or change the active deployment profile.
#
#     make profile              # show which is active, and what is available
#     make profile runpod       # switch
#
# The choice is one word in .harness-profile, which is gitignored: which
# deployment you happen to be pointed at is a property of your machine, not of
# the repository. `make dev PROFILE=x` overrides it for a single run without
# changing it.
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PROFILE_DIR="deploy/profiles"
ACTIVE_FILE=".harness-profile"
DEFAULT_PROFILE=ollama

available() {
	local path
	for path in "$PROFILE_DIR"/*.env; do
		[ -e "$path" ] || continue
		basename "$path" .env
	done
}

active() {
	if [ -f "$ACTIVE_FILE" ]; then
		tr -d '[:space:]' <"$ACTIVE_FILE"
	else
		printf '%s' "$DEFAULT_PROFILE"
	fi
}

# The one-line summary comes from each profile file's first comment line, so it
# cannot drift from the profile it describes.
describe() {
	# Strip the leading "<name> — " too: the name is already the column beside it,
	# and repeating it reads as a stutter.
	sed -n "1s/^# *//p" "$PROFILE_DIR/$1.env" 2>/dev/null |
		sed "s/^$1 *[—-] *//"
}

want="${1:-}"

if [ -z "$want" ]; then
	current="$(active)"
	printf '\n  active profile: %s%s\n\n' "$current" \
		"$([ -f "$ACTIVE_FILE" ] || printf ' (default — nothing selected yet)')"
	while IFS= read -r name; do
		marker="  "
		[ "$name" = "$current" ] && marker="→ "
		printf '  %s%-8s %s\n' "$marker" "$name" "$(describe "$name")"
	done < <(available)
	printf '\n  switch with:  make profile <name>\n\n'
	exit 0
fi

if [ ! -f "$PROFILE_DIR/${want}.env" ]; then
	printf 'error: no such profile: %s\n\n  Available: %s\n\n' \
		"$want" "$(available | tr '\n' ' ')" >&2
	exit 1
fi

printf '%s\n' "$want" >"$ACTIVE_FILE"
printf '\n  active profile: %s — %s\n\n  Start it with:  make dev\n\n' \
	"$want" "$(describe "$want")"
