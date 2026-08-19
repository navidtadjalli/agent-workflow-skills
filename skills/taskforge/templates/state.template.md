# STATE — {{INITIATIVE_TITLE}}

This is the only progress ledger for `{{INITIATIVE_SLUG}}`.

## Policy

- Execution mode: **{{EXECUTION_MODE}}**
- Runner: **{{RUNNER_KIND}}**
- Control plane: {{CONTROL_PLANE_POLICY}}
- Commit policy: {{COMMIT_POLICY}}
- Push policy: {{PUSH_POLICY}}
- Protected targets: {{PROTECTED_TARGETS}}
- Review policy: {{REVIEW_POLICY}}
- Taskforge skill: `{{TASKFORGE_SKILL_DIR}}`
- Orchestrate skill: `{{ORCHESTRATE_SKILL_DIR}}`

Only the lead changes this file. Team workers never edit it. In single mode, the
current agent changes it only while acting as lead after checking real evidence.

Statuses: `todo`, `in-progress`, `review`, `done`, `blocked`.

## Run pointer

- Current phase: **{{CURRENT_PHASE}}**
- Last completed task: **{{LAST_DONE}}**
- Next ready task: **{{NEXT_TASK}}**
- Active runner/output: **{{ACTIVE_RUNNER}}**
- Last checkpoint: **{{LAST_CHECKPOINT}}**
- Wake mechanism: **{{WAKE_MECHANISM}}**

## Ledger

| # | task | role | profile | deps | group | status | attempts | evidence | note |
|---|---|---|---|---|---|---|---:|---|---|
{{LEDGER_ROWS}}

## Evidence log

{{EVIDENCE_LOG}}

## Resume anchor

- Completed: {{DONE_LIST}}
- Next action: {{NEXT_ACTION}}
- Verification environment: {{ENV_SETUP}}
- Open decisions or blockers: {{OPEN_FLAGS}}
- Final gate: `{{FINAL_GATE_SUMMARY}}`
