#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
tmp_dir="$(mktemp -d)"

cleanup() {
  if [ -n "${tmp_dir:-}" ] && [ -d "$tmp_dir" ]; then
    rm -rf -- "$tmp_dir"
  fi
}
trap cleanup EXIT

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

pass() {
  printf 'ok: %s\n' "$*"
}

shell_files=()
while IFS= read -r shell_file; do
  shell_files+=("$shell_file")
done < <(find "$repo_dir" -type f -name '*.sh' -print)

for shell_file in "${shell_files[@]}"; do
  bash -n "$shell_file"
done
pass 'shell syntax'

if command -v shellcheck >/dev/null 2>&1; then
  shellcheck "${shell_files[@]}"
  pass 'shellcheck'
fi

for skill_name in taskforge orchestrate dispatch; do
  skill_dir="$repo_dir/skills/$skill_name"
  skill_file="$skill_dir/SKILL.md"
  metadata_file="$skill_dir/agents/openai.yaml"

  [ -f "$skill_file" ] || fail "missing $skill_file"
  [ -f "$metadata_file" ] || fail "missing $metadata_file"
  [ "$(sed -n '1p' "$skill_file")" = '---' ] || fail "$skill_name frontmatter start"
  grep -Eq "^name: $skill_name$" "$skill_file" || fail "$skill_name name"
  grep -Eq '^description: .+' "$skill_file" || fail "$skill_name description"
  expected_prompt='default_prompt: "Use $'"$skill_name"
  grep -Fq "$expected_prompt" "$metadata_file" ||
    fail "$skill_name default prompt"
done
pass 'skill metadata'

if grep -RIE '(gpt-[0-9]|claude-(opus|sonnet|haiku)-[0-9])' "$repo_dir/skills" >/dev/null; then
  fail 'version-pinned model found'
fi
pass 'model defaults are unpinned'

orchestrate="$repo_dir/skills/orchestrate/scripts/spawn-agent.sh"
runner_repo="$tmp_dir/runner-repo"
mkdir -p "$runner_repo"

DRY_RUN=1 PROVIDER=codex "$orchestrate" dev "$runner_repo" 'test task' "$tmp_dir/codex.md" >/dev/null 2>&1
grep -Fq 'dry-run provider=codex profile=balanced effort=medium' "$tmp_dir/codex.md.status" ||
  fail 'codex dry-run routing'

DRY_RUN=1 PROVIDER=claude "$orchestrate" architect "$runner_repo" 'test task' "$tmp_dir/claude.md" >/dev/null 2>&1
grep -Fq 'dry-run provider=claude profile=strong effort=high' "$tmp_dir/claude.md.status" ||
  fail 'claude dry-run routing'

DRY_RUN=1 PROVIDER=codex ORCHESTRATE_CODEX_MODEL_BALANCED=test-model \
  "$orchestrate" dev "$runner_repo" 'test task' "$tmp_dir/profile-model.md" >/dev/null 2>&1
grep -Fq 'model=test-model' "$tmp_dir/profile-model.md.status" ||
  fail 'profile model override'

DRY_RUN=1 PRODUCER_PROVIDER=codex "$orchestrate" reviewer "$runner_repo" 'review task' "$tmp_dir/reviewer.md" >/dev/null 2>&1
grep -Fq 'dry-run provider=claude' "$tmp_dir/reviewer.md.status" ||
  fail 'opposite-provider reviewer routing'
pass 'runner routing'

install_home="$tmp_dir/install-home"
mkdir -p "$install_home"
HOME="$install_home" "$repo_dir/install.sh" --all >/dev/null

for skill_name in taskforge orchestrate dispatch; do
  [ -L "$install_home/.claude/skills/$skill_name" ] ||
    fail "Claude install missing $skill_name"
  [ -L "$install_home/.agents/skills/$skill_name" ] ||
    fail "Agent Skills install missing $skill_name"
done

HOME="$install_home" "$repo_dir/install.sh" --all >/dev/null
pass 'installer and idempotency'

conflict_home="$tmp_dir/conflict-home"
mkdir -p "$conflict_home/.claude/skills/taskforge"
printf '%s\n' legacy > "$conflict_home/.claude/skills/taskforge/legacy.txt"

if HOME="$conflict_home" "$repo_dir/install.sh" --claude >/dev/null 2>&1; then
  fail 'installer overwrote a conflict without --force'
fi
[ ! -e "$conflict_home/.claude/skills/orchestrate" ] ||
  fail 'installer made partial changes after a preflight conflict'

HOME="$conflict_home" "$repo_dir/install.sh" --claude --force >/dev/null
[ -L "$conflict_home/.claude/skills/taskforge" ] ||
  fail 'forced install did not create taskforge link'
legacy_backup="$(find "$conflict_home/.local/state/agent-workflow-skills/backups" -type f -name legacy.txt -print -quit)"
[ -n "$legacy_backup" ] || fail 'forced install did not retain backup'
pass 'installer conflict backup'

fixture_repo="$tmp_dir/fixture-repo"
initiative="$fixture_repo/.tasks/01-sample"
mkdir -p "$initiative/prompts"

render_template() {
  sed \
    -e 's/{{NN}}/01/g' \
    -e 's/{{TASK_SLUG}}/sample/g' \
    -e 's/{{TASK_TITLE}}/Sample task/g' \
    -e 's/{{ROLE}}/dev/g' \
    -e 's/{{PROFILE}}/balanced/g' \
    -e 's/{{INITIATIVE_TITLE}}/Sample/g' \
    -e 's/{{INITIATIVE_SLUG}}/sample/g' \
    -e 's/{{INITIATIVE_DIR}}/01-sample/g' \
    -e 's/{{EXECUTION_MODE}}/single/g' \
    -e 's/{{RUNNER_KIND}}/same-agent/g' \
    -e 's/{{FIRST_TASK}}/01/g' \
    -e 's~{{LEDGER_ROWS}}~| 01 | Sample | dev | balanced | — | P1 | todo | 0 | — | — |~g' \
    "$1" | sed -E 's/\{\{[A-Z0-9_]+\}\}/resolved/g' > "$2"
}

templates="$repo_dir/skills/taskforge/templates"
render_template "$templates/readme.template.md" "$initiative/readme.md"
render_template "$templates/acceptance.template.md" "$initiative/acceptance.md"
render_template "$templates/orchestrator.template.md" "$initiative/prompts/00-ORCHESTRATOR.md"
render_template "$templates/shared.template.md" "$initiative/prompts/_shared.md"
render_template "$templates/state.template.md" "$initiative/prompts/STATE.md"
render_template "$templates/spec.template.md" "$initiative/01-sample.md"
render_template "$templates/task.template.md" "$initiative/prompts/task-01.md"

validator="$repo_dir/skills/taskforge/scripts/validate-initiative.sh"
ledger="$repo_dir/skills/taskforge/scripts/ledger.sh"

"$validator" "$initiative" >/dev/null
pass 'initiative validation'

TASKFORGE_STATE_HOME="$tmp_dir/taskforge-state" "$ledger" seal "$initiative" >/dev/null
TASKFORGE_STATE_HOME="$tmp_dir/taskforge-state" "$ledger" verify "$initiative" >/dev/null
printf '\nintentional drift\n' >> "$initiative/prompts/STATE.md"

if TASKFORGE_STATE_HOME="$tmp_dir/taskforge-state" "$ledger" verify "$initiative" >/dev/null 2>&1; then
  fail 'ledger failed to detect drift'
fi

TASKFORGE_STATE_HOME="$tmp_dir/taskforge-state" "$ledger" restore "$initiative" >/dev/null
TASKFORGE_STATE_HOME="$tmp_dir/taskforge-state" "$ledger" verify "$initiative" >/dev/null
pass 'ledger seal, drift detection, and restore'

same_slug="$tmp_dir/another-repo/.tasks/01-sample"
mkdir -p "$same_slug/prompts"
cp "$initiative/prompts/STATE.md" "$same_slug/prompts/STATE.md"
first_vault="$(TASKFORGE_STATE_HOME="$tmp_dir/taskforge-state" "$ledger" path "$initiative")"
second_vault="$(TASKFORGE_STATE_HOME="$tmp_dir/taskforge-state" "$ledger" path "$same_slug")"
[ "$first_vault" != "$second_vault" ] || fail 'ledger vault collides across repositories'
pass 'ledger repository namespacing'

legacy_home="$tmp_dir/legacy-home"
legacy_vault="$legacy_home/.claude/taskforge-ledger/01-sample"
mkdir -p "$legacy_vault"
cp "$initiative/prompts/STATE.md" "$legacy_vault/current.md"
if command -v sha256sum >/dev/null 2>&1; then
  sha256sum "$legacy_vault/current.md" | awk '{print $1}' > "$legacy_vault/current.sha256"
else
  shasum -a 256 "$legacy_vault/current.md" | awk '{print $1}' > "$legacy_vault/current.sha256"
fi
legacy_result="$(HOME="$legacy_home" XDG_STATE_HOME="$legacy_home/xdg" "$ledger" verify "$initiative")"
printf '%s\n' "$legacy_result" | grep -Fq 'legacy baseline; seal to migrate' ||
  fail 'legacy ledger baseline was not recognized'
pass 'legacy ledger compatibility'

state_copy="$tmp_dir/state-before-extra-row.md"
cp "$initiative/prompts/STATE.md" "$state_copy"
printf '%s\n' '| 02 | Extra | dev | balanced | — | P1 | todo | 0 | — | — |' >> "$initiative/prompts/STATE.md"
if "$validator" "$initiative" >/dev/null 2>&1; then
  fail 'validator accepted a STATE row without a spec and prompt'
fi
mv "$state_copy" "$initiative/prompts/STATE.md"
pass 'STATE-to-task mapping guard'

printf '%s\n' 'Update STATE.md and mark the row done.' >> "$initiative/prompts/task-01.md"
if "$validator" "$initiative" >/dev/null 2>&1; then
  fail 'validator accepted worker state mutation'
fi
pass 'worker state-mutation guard'

run_python_suite() {
  local label="$1"
  local script="$2"
  local log="$tmp_dir/$(basename "$script").log"

  # -W error: the suites are held to pristine output, and a warning nobody
  # sees is how that constraint quietly stops being true. The log is only
  # printed on failure, so a warning that is not an error is invisible here.
  if ! python3 -W error "$script" >"$log" 2>&1; then
    cat "$log" >&2
    fail "$label"
  fi
  pass "$label"
}

run_python_suite 'dispatch unit tests' "$repo_dir/tests/dispatch_test.py"
run_python_suite 'dispatch stub-agent integration' "$repo_dir/tests/dispatch_integration.py"

printf '%s\n' 'all tests passed'
