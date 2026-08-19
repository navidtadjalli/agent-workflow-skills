#!/usr/bin/env bash
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=_common.sh
source "$here/_common.sh"

if [ "$#" -lt 3 ]; then
  die 'usage: spawn-codex.sh <strong|balanced|fast|t1|t2|t3> <repo-path> "<task|@file|->" [output-file]'
fi

profile="$(normalize_profile "$1")"
repo="$2"
task_arg="$3"
output="${4:-/tmp/orchestrate-codex-$(date -u +%Y%m%dT%H%M%SZ)-$$.md}"
effort="${EFFORT:-$(default_effort "$profile")}"
case "$profile" in
  strong) profile_model="${ORCHESTRATE_CODEX_MODEL_STRONG:-}" ;;
  balanced) profile_model="${ORCHESTRATE_CODEX_MODEL_BALANCED:-}" ;;
  fast) profile_model="${ORCHESTRATE_CODEX_MODEL_FAST:-}" ;;
esac
model="${MODEL:-$profile_model}"

[ -d "$repo" ] || die "repo path is not a directory: $repo"
prepare_output "$output"

args=(exec)

if is_true "${UNSAFE:-0}"; then
  args+=(--dangerously-bypass-approvals-and-sandbox)
elif [ -n "${CODEX_SANDBOX:-}" ]; then
  args+=(-s "$CODEX_SANDBOX")
fi

if [ -n "$model" ]; then
  args+=(-m "$model")
fi

args+=(-c "model_reasoning_effort=$effort" -C "$repo" -o "$output" -)

if is_true "${DRY_RUN:-0}"; then
  write_status "$output" "dry-run provider=codex profile=$profile effort=$effort model=${model:-configured-default}"
  printf 'dry-run: provider=codex profile=%s effort=%s model=%s repo=%s output=%s\n' \
    "$profile" "$effort" "${model:-configured-default}" "$repo" "$output"
  exit 0
fi

command_exists codex || die 'codex CLI is not installed or not on PATH'
write_status "$output" "running provider=codex profile=$profile effort=$effort pid=$$"
printf 'runner: provider=codex profile=%s effort=%s model=%s output=%s\n' \
  "$profile" "$effort" "${model:-configured-default}" "$output" >&2

if load_task "$task_arg" | codex "${args[@]}"; then
  write_status "$output" "ok provider=codex profile=$profile effort=$effort"
else
  rc="$?"
  write_status "$output" "failed:$rc provider=codex profile=$profile effort=$effort"
  exit "$rc"
fi
