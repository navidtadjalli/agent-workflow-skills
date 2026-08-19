#!/usr/bin/env bash
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=_common.sh
source "$here/_common.sh"

if [ "$#" -lt 3 ]; then
  die 'usage: spawn-agent.sh <lead|architect|builder|dev|qa|reviewer|scribe> <repo-path> "<task|@file|->" [output-file]'
fi

role="$1"
repo="$2"
task_arg="$3"
output="${4:-/tmp/orchestrate-${role}-$(date -u +%Y%m%dT%H%M%SZ)-$$.md}"

case "$role" in
  lead|architect|builder|reviewer) role_profile=strong ;;
  dev|qa) role_profile=balanced ;;
  scribe) role_profile=fast ;;
  *) die "unknown role: $role" ;;
esac

if [ -n "${PROFILE:-}" ]; then
  profile="$(normalize_profile "$PROFILE")"
elif [ -n "${TIER:-}" ]; then
  profile="$(normalize_profile "$TIER")"
else
  profile="$role_profile"
fi
effort="${EFFORT:-$(default_effort "$profile")}"
provider="${PROVIDER:-${CLI:-auto}}"

runner_available() {
  if is_true "${DRY_RUN:-0}"; then
    return 0
  fi
  command_exists "$1"
}

choose_auto_provider() {
  if [ "$role" = reviewer ]; then
    case "${PRODUCER_PROVIDER:-}" in
      claude)
        if runner_available codex; then printf '%s\n' codex; return; fi
        ;;
      codex)
        if runner_available claude; then printf '%s\n' claude; return; fi
        ;;
    esac
  fi

  case "$role" in
    lead|architect)
      if runner_available claude; then printf '%s\n' claude; return; fi
      if runner_available codex; then printf '%s\n' codex; return; fi
      ;;
    *)
      if runner_available codex; then printf '%s\n' codex; return; fi
      if runner_available claude; then printf '%s\n' claude; return; fi
      ;;
  esac
  die 'neither claude nor codex CLI is installed'
}

case "$provider" in
  auto) provider="$(choose_auto_provider)" ;;
  claude)
    runner_available claude || die 'PROVIDER=claude requested, but claude is unavailable'
    ;;
  codex)
    runner_available codex || die 'PROVIDER=codex requested, but codex is unavailable'
    ;;
  *) die "PROVIDER must be auto|claude|codex; got: $provider" ;;
esac

[ -d "$repo" ] || die "repo path is not a directory: $repo"

prompt_file="$(mktemp "${TMPDIR:-/tmp}/orchestrate-prompt.XXXXXX")"
cleanup() {
  rm -f -- "$prompt_file"
}
trap cleanup EXIT

if [ "$role" = reviewer ]; then
  role_rule='Review only. Do not edit files, commit, or change workflow state.'
else
  role_rule='Do not edit workflow control-plane files. Do not commit or push unless the task explicitly authorizes it.'
fi

{
  printf 'You are the %s role in an orchestrated workflow.\n' "$role"
  printf '%s\n' "$role_rule"
  printf '%s\n' 'Read repository instructions before acting. Stay inside task scope and report exact evidence.'
  printf '%s\n' 'Finish with:'
  printf '%s\n' 'STATUS: complete|partial|blocked'
  printf '%s\n' 'SUMMARY: concise outcome and files changed'
  printf '%s\n' 'VERIFICATION: commands and real results'
  printf '%s\n' 'RISKS: residual risks or none'
  printf '\nTASK:\n'
  load_task "$task_arg"
} > "$prompt_file"

printf 'route: role=%s provider=%s profile=%s effort=%s output=%s\n' \
  "$role" "$provider" "$profile" "$effort" "$output" >&2

case "$provider" in
  codex)
    EFFORT="$effort" "$here/spawn-codex.sh" "$profile" "$repo" "@$prompt_file" "$output"
    ;;
  claude)
    (
      cd "$repo"
      EFFORT="$effort" "$here/spawn-claude.sh" "$profile" "@$prompt_file" "$output"
    )
    ;;
esac
