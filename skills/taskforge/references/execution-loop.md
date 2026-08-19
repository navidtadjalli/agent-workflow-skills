# Execution loop

Read this file when running a generated Taskforge initiative.

## Lead loop

Repeat until every row is done or a genuine external blocker remains:

1. Verify the ledger snapshot when one exists. On mismatch, inspect the diff and
   actual repository state. Restore only unintended changes.
2. Read the run pointer and select the lowest-numbered `todo` task whose
   dependencies are `done`.
3. Change that row to `in-progress`, increment attempts, record the runner, and seal.
4. Execute using the selected mode.
5. Change the row to `review` only when an implementation result exists.
6. Inspect the real diff or artifact and run exact acceptance checks.
7. Fix or return findings. Change to `done` only on green evidence; otherwise leave
   `in-progress` or mark `blocked` with a concrete external dependency.
8. Update the run pointer and evidence, seal, then continue to the next ready task.

Allowed normal transitions are `todo -> in-progress -> review -> done`.
`in-progress -> blocked` is allowed only for a real dependency or decision.
Rework may move `review -> in-progress`.

## Single mode

Use one current agent for every role:

1. As lead, make and seal the state transition.
2. As the task role, read `_shared.md`, the task prompt, the numbered spec,
   `readme.md`, and `acceptance.md`; then implement.
3. As reviewer, inspect from first principles and run verification.
4. As lead, record evidence and the next state.

Call the review `self-review`. Do not manufacture independence by changing role
labels inside one context.

## Team mode

Prefer native host subagents. If using the Orchestrate CLI adapter, read its runner
reference first. Give a worker the thin task prompt and repository path. Workers:

- do not edit the control plane;
- do not self-mark completion;
- do not push or commit unless policy explicitly authorizes it;
- return status, changed files, verification output, and risks.

The lead verifies repository state and acceptance independently of the report. Run
parallel tasks only in isolated worktrees or disjoint output locations.

## Resumption

Durable state supports resumption; it does not guarantee automatic resumption.

- Continue within the active session while safe work remains.
- If the host supports scheduled or goal-based wakeups and the user authorizes one,
  record its real identifier in STATE.
- If no wake mechanism exists, stop only at a natural host boundary and return the
  generated START/RESUME prompt. The next capable agent resumes from STATE.
- Never tell the user a background runner will re-enter the loop unless the host
  actually provides that behavior.

Recovery snapshots default to
`${XDG_STATE_HOME:-$HOME/.local/state}/taskforge/ledgers`.
Set `TASKFORGE_STATE_HOME` to override the store. Existing snapshots under
`$HOME/.claude/taskforge-ledger` remain readable; the next `seal` writes the new
repository-namespaced format.

## Final gate

After all rows are `done`, run initiative-wide acceptance, inspect the aggregate
diff or artifact, confirm policy compliance, update STATE with final evidence, seal,
and report completion. Do not push, merge, deploy, or send messages without explicit
authority.
