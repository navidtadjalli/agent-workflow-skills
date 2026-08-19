# 00 — Execution runbook: {{INITIATIVE_TITLE}}

Use this runbook to execute or resume `{{INITIATIVE_SLUG}}` from
`{{REPO_PATH}}`. Any capable current agent may act as lead.

## Operating contract

- Mode: **{{EXECUTION_MODE}}**
- Runner: **{{RUNNER_KIND}}**
- Taskforge skill: `{{TASKFORGE_SKILL_DIR}}`
- Orchestrate skill: `{{ORCHESTRATE_SKILL_DIR}}`
- Control plane: {{CONTROL_PLANE_POLICY}}
- Commit policy: {{COMMIT_POLICY}}
- Push policy: {{PUSH_POLICY}}
- Protected targets: {{PROTECTED_TARGETS}}
- Isolation: {{ISOLATION_POLICY}}
- Review: {{REVIEW_POLICY}}

The lead owns `prompts/STATE.md`. Team workers never edit any file under
`prompts/`. In single mode, the same agent may implement, but it changes STATE only
after explicitly returning to the lead role and verifying evidence.

## Recovery ledger

The snapshot ledger detects accidental or out-of-role STATE drift and keeps recovery
copies. It is not a security boundary against processes with the same filesystem
access.

```bash
{{TASKFORGE_SKILL_DIR}}/scripts/ledger.sh verify  .tasks/{{INITIATIVE_DIR}}
{{TASKFORGE_SKILL_DIR}}/scripts/ledger.sh seal    .tasks/{{INITIATIVE_DIR}}
{{TASKFORGE_SKILL_DIR}}/scripts/ledger.sh restore .tasks/{{INITIATIVE_DIR}}
{{TASKFORGE_SKILL_DIR}}/scripts/ledger.sh log     .tasks/{{INITIATIVE_DIR}}
```

On first execution, seal the fully generated and validated STATE baseline. On resume,
verify before trusting STATE. A mismatch requires inspection of the snapshot diff and
real repository evidence; restore only unintended changes.

## State machine

Normal transitions:

```text
todo -> in-progress -> review -> done
                      -> in-progress
in-progress -> blocked
```

Use `blocked` only for a concrete external dependency or decision. Store real
evidence in the row: a commit SHA only when policy authorizes commits, otherwise a
diff range, artifact path, command result, or output file.

## Execution loop

1. Verify the recovery ledger when a baseline exists.
2. Read STATE and reconcile any `in-progress` or `review` row with the actual
   worktree, artifact, runner output, and status sidecar.
3. If all rows are `done`, run the final gate, record evidence, seal, and report.
4. Select the lowest-numbered `todo` row whose dependencies are `done`.
5. Set it to `in-progress`, increment attempts, record runner/output, and seal.
6. Execute:
   - **single:** adopt the row's role and perform the task in this session;
   - **team/native:** spawn one authorized native worker with `prompts/task-NN.md`;
   - **team/cli:** run
     `{{ORCHESTRATE_SKILL_DIR}}/scripts/spawn-agent.sh <role> "{{REPO_PATH}}" @<absolute-task-prompt> <output-file>`.
7. Inspect the actual result. Move to `review`, then apply {{REVIEW_POLICY}}.
8. Run task acceptance exactly. Fix findings and rerun until green or genuinely
   blocked.
9. Record evidence, move to `done`, update the run pointer, seal, and continue.

Do not run two repository writers in one worktree. Parallel rows require isolated
worktrees and isolated databases, ports, caches, and generated outputs.

## Review gate

For every row:

- compare the result with the numbered spec and non-goals;
- inspect the real diff or artifact;
- check correctness, security, error paths, scope, and repository conventions;
- run the exact acceptance commands;
- confirm control-plane files and unrelated changes are absent from deliverables.

In single mode, label this `self-review`. In team mode, use an independent reviewer
where risk warrants it, but the lead still owns acceptance. A worker report is a
claim, not evidence.

## Resumption

Continue in the active session while safe progress remains. Record a scheduled or
goal-based wake only if the host actually supplies it and the user authorized it.
Otherwise, durable STATE plus the prompt below is the resume mechanism.

Do not claim automatic wake, background re-entry, independent review, tests, commits,
or pushes unless they occurred.

## Final gate

Run all of:

```bash
{{FINAL_GATE_COMMANDS}}
```

Then validate the control plane:

```bash
{{TASKFORGE_SKILL_DIR}}/scripts/validate-initiative.sh .tasks/{{INITIATIVE_DIR}}
```

Confirm aggregate scope and policies before reporting completion.

## START / RESUME PROMPT

> Act as lead for `{{INITIATIVE_SLUG}}` in `{{REPO_PATH}}`. Read
> `.tasks/{{INITIATIVE_DIR}}/prompts/00-ORCHESTRATOR.md` and
> `.tasks/{{INITIATIVE_DIR}}/prompts/STATE.md`, verify the recovery ledger if a
> baseline exists, and {{START_INSTRUCTION}}. Use mode `{{EXECUTION_MODE}}` with
> runner `{{RUNNER_KIND}}`. Enforce acceptance and recorded mutation policies.
> Start or resume at task {{FIRST_TASK}}.
