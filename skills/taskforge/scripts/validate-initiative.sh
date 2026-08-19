#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 1 ]; then
  printf '%s\n' 'usage: validate-initiative.sh <initiative-dir>' >&2
  exit 2
fi

initiative_arg="$1"
[ -d "$initiative_arg" ] || {
  printf 'error: not a directory: %s\n' "$initiative_arg" >&2
  exit 2
}

initiative="$(cd "$initiative_arg" && pwd -P)"
state="$initiative/prompts/STATE.md"
errors=0

report_error() {
  printf 'invalid: %s\n' "$*" >&2
  errors=$((errors + 1))
}

required=(
  readme.md
  acceptance.md
  prompts/00-ORCHESTRATOR.md
  prompts/STATE.md
  prompts/_shared.md
)

for relative in "${required[@]}"; do
  [ -f "$initiative/$relative" ] || report_error "missing $relative"
done

while IFS= read -r markdown; do
  if matches="$(grep -nE '\{\{[A-Z0-9_]+\}\}' "$markdown" 2>/dev/null)"; then
    report_error "unresolved placeholder(s) in ${markdown#"$initiative/"}"
    printf '%s\n' "$matches" >&2
  fi
done < <(find "$initiative" -type f -name '*.md' -print)

shopt -s nullglob
specs=("$initiative"/[0-9][0-9]-*.md)
prompts=("$initiative"/prompts/task-[0-9][0-9].md)

if [ "${#specs[@]}" -eq 0 ]; then
  report_error 'no numbered task specs found'
fi

for spec in "${specs[@]}"; do
  base="$(basename "$spec")"
  task_id="${base:0:2}"
  prompt="$initiative/prompts/task-$task_id.md"
  [ -f "$prompt" ] || report_error "missing prompts/task-$task_id.md for $base"
  if [ -f "$state" ] && ! grep -Eq "^\|[[:space:]]*$task_id[[:space:]]*\|" "$state"; then
    report_error "STATE ledger has no row for task $task_id"
  fi
done

for prompt in "${prompts[@]}"; do
  base="$(basename "$prompt")"
  task_id="${base#task-}"
  task_id="${task_id%.md}"
  matches=("$initiative"/"$task_id"-*.md)
  [ "${#matches[@]}" -eq 1 ] || report_error "task prompt $base does not map to exactly one spec"
  if [ "${#matches[@]}" -eq 1 ]; then
    expected_link="../$(basename "${matches[0]}")"
    grep -Fq "$expected_link" "$prompt" ||
      report_error "task prompt $base does not link to $expected_link"
  fi

  mutation_lines="$(grep -Ein '(update|write|modify|set|mark).*(STATE\.md|ledger row|row.*(done|blocked|in-progress))' "$prompt" 2>/dev/null || true)"
  if [ -n "$mutation_lines" ]; then
    unsafe_lines="$(printf '%s\n' "$mutation_lines" | grep -Eiv '(do not|never|leave .* lead)' || true)"
    if [ -n "$unsafe_lines" ]; then
      report_error "worker prompt $base instructs control-plane mutation"
      printf '%s\n' "$unsafe_lines" >&2
    fi
  fi
done

if [ -f "$state" ]; then
  duplicate_ids="$(awk -F'|' '
    $2 ~ /^[[:space:]]*[0-9][0-9][[:space:]]*$/ {
      id = $2
      gsub(/[[:space:]]/, "", id)
      seen[id]++
    }
    END {
      for (id in seen) if (seen[id] > 1) print id
    }
  ' "$state")"
  [ -z "$duplicate_ids" ] || report_error "duplicate STATE task id(s): $duplicate_ids"

  invalid_rows="$(awk -F'|' '
    function trim(value) {
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
      return value
    }
    $2 ~ /^[[:space:]]*[0-9][0-9][[:space:]]*$/ {
      id = trim($2)
      role = trim($4)
      profile = trim($5)
      status = trim($8)
      if (role !~ /^(architect|builder|dev|qa|scribe)$/)
        print id ": invalid role " role
      if (profile !~ /^(strong|balanced|fast)$/)
        print id ": invalid profile " profile
      if (status !~ /^(todo|in-progress|review|done|blocked)$/)
        print id ": invalid status " status
    }
  ' "$state")"
  if [ -n "$invalid_rows" ]; then
    report_error "invalid STATE row values"
    printf '%s\n' "$invalid_rows" >&2
  fi

  while IFS= read -r ledger_id; do
    ledger_specs=("$initiative"/"$ledger_id"-*.md)
    [ "${#ledger_specs[@]}" -eq 1 ] ||
      report_error "STATE task $ledger_id does not map to exactly one spec"
    [ -f "$initiative/prompts/task-$ledger_id.md" ] ||
      report_error "STATE task $ledger_id has no worker prompt"
  done < <(awk -F'|' '
    $2 ~ /^[[:space:]]*[0-9][0-9][[:space:]]*$/ {
      id = $2
      gsub(/[[:space:]]/, "", id)
      print id
    }
  ' "$state")

  grep -Eqi 'only the lead (changes|writes)' "$state" ||
    report_error 'STATE does not declare lead-only ownership'
fi

if [ "$errors" -ne 0 ]; then
  printf 'validation failed: %d issue(s)\n' "$errors" >&2
  exit 1
fi

printf 'valid initiative: %s (%d tasks)\n' "$initiative" "${#specs[@]}"
