#!/usr/bin/env bash
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=_common.sh
source "$here/_common.sh"

if [ "$#" -lt 2 ]; then
  die 'usage: spawn-claude.sh <strong|balanced|fast|t1|t2|t3> "<task|@file|->" [output-file]'
fi

profile="$(normalize_profile "$1")"
task_arg="$2"
output="${3:-/tmp/orchestrate-claude-$(date -u +%Y%m%dT%H%M%SZ)-$$.md}"
effort="${EFFORT:-$(default_effort "$profile")}"
case "$profile" in
  strong) profile_model="${ORCHESTRATE_CLAUDE_MODEL_STRONG:-}" ;;
  balanced) profile_model="${ORCHESTRATE_CLAUDE_MODEL_BALANCED:-}" ;;
  fast) profile_model="${ORCHESTRATE_CLAUDE_MODEL_FAST:-}" ;;
esac
model="${MODEL:-$profile_model}"
repo="$(pwd -P)"

prepare_output "$output"

args=(-p --effort "$effort")

if [ -n "$model" ]; then
  args+=(--model "$model")
fi

if is_true "${UNSAFE:-0}"; then
  args+=(--dangerously-skip-permissions)
elif [ -n "${CLAUDE_PERMISSION_MODE:-}" ]; then
  args+=(--permission-mode "$CLAUDE_PERMISSION_MODE")
fi

if is_true "${DRY_RUN:-0}"; then
  write_status "$output" "dry-run provider=claude profile=$profile effort=$effort model=${model:-configured-default}"
  printf 'dry-run: provider=claude profile=%s effort=%s model=%s repo=%s output=%s\n' \
    "$profile" "$effort" "${model:-configured-default}" "$repo" "$output"
  exit 0
fi

command_exists claude || die 'claude CLI is not installed or not on PATH'
write_status "$output" "running provider=claude profile=$profile effort=$effort pid=$$"
printf 'runner: provider=claude profile=%s effort=%s model=%s output=%s\n' \
  "$profile" "$effort" "${model:-configured-default}" "$output" >&2

if load_task "$task_arg" | claude "${args[@]}" | tee "$output"; then
  write_status "$output" "ok provider=claude profile=$profile effort=$effort"
else
  rc="$?"
  write_status "$output" "failed:$rc provider=claude profile=$profile effort=$effort"
  exit "$rc"
fi
