---
name: orchestrate
description: Coordinate complex engineering or knowledge work through an explicit plan, role-based execution, verification, and review. Use for orchestration, delegation, subagents, parallel work, cross-review, role routing, or long multi-step tasks. Support both single-agent mode, where one capable agent performs each role sequentially, and team mode, where independent agents are available and authorized. Do not invoke for quick answers or small edits whose coordination cost exceeds the work.
---

# Orchestrate

Own the outcome as the lead. Treat delegation as an execution option, not a
requirement. Never stall because another agent, model family, CLI, background
runner, or wake mechanism is unavailable.

## Choose a mode

Choose one mode before planning and state it briefly:

- **Single-agent:** Use when the user requests one agent, delegation is unavailable
  or unauthorized, shared-worktree risk is high, or one agent can finish efficiently.
  Perform needed roles sequentially in the current session.
- **Team:** Use when the user requests or authorizes delegation, the host exposes
  suitable agents or CLIs, and independent work or review will materially help.
- **Auto:** Default to single-agent. Upgrade to team only when the task has a clean
  split and the host's rules permit delegation.

Mode may change during execution. Record the reason and preserve current state.
Never describe a self-review as independent review.

## Use roles as responsibilities

Assign responsibilities even in single-agent mode:

| Role | Responsibility | Default profile |
|---|---|---|
| Lead | Define outcome, sequence work, own state, accept evidence | strong |
| Architect | Resolve material ambiguity and cross-cutting design | strong |
| Builder | Implement complex or high-risk changes | strong |
| Dev | Implement well-scoped changes and tests | balanced |
| QA | Characterize behavior and run acceptance | balanced |
| Reviewer | Review only; find correctness, security, and scope defects | strong |
| Scribe | Apply mechanical documentation, naming, or config changes | fast |

Roles are lenses, not required processes or model brands. Keep the lead capable of
implementing in single-agent mode. Use `builder`, `dev`, and `qa` aliases when
consuming Taskforge plans.

## Plan the work

1. Read repository instructions and inspect relevant state.
2. Define the deliverable, non-goals, acceptance evidence, and mutation authority.
3. Split only where each unit has a concrete output and verification.
4. Express dependencies explicitly. Run independent units concurrently only with
   isolated write surfaces.
5. Keep one source of truth for status. Use the host plan facility when sufficient;
   use Taskforge for durable cross-session state.

Do not create commits, branches, worktrees, pushes, external messages, or destructive
changes unless the user request or an accepted task policy authorizes them.

## Execute

### Single-agent mode

For each ready unit:

1. Adopt the assigned role and implement the scoped change.
2. Run the exact acceptance checks.
3. Switch to Reviewer: inspect the diff or artifact from first principles.
4. Fix findings, rerun checks, and record the evidence.
5. Mark the unit complete only after evidence is green.

Use a deliberate review pass, but label it **self-review**. For high-risk work where
independence matters, recommend or request team review instead of overstating
assurance.

### Team mode

Prefer the host's native subagent mechanism. Give each worker only:

- the objective and boundaries;
- exact files or artifacts it owns;
- acceptance commands;
- repository instructions it must read;
- mutation and commit policy;
- a compact report contract.

Use bundled CLI adapters only when native agents are unavailable or the user asks
for Claude/Codex CLI runners. Before using them, read
[references/runner-options.md](references/runner-options.md). Run:

```bash
scripts/spawn-agent.sh <role> <repo-path> "<task>" [output-file]
```

The root `spawn-*.sh` files are compatibility shims for older callers.

Never run two writers in the same worktree concurrently. Give parallel workers
separate worktrees or disjoint non-repository outputs, and isolate databases,
ports, caches, and generated files. If isolation is uncertain, run sequentially.

## Review and gate

Require evidence proportional to risk:

1. Compare the result with the stated outcome and non-goals.
2. Inspect the actual diff or artifact; do not accept a worker summary as proof.
3. Check correctness, security, error paths, scope, maintainability, and repository
   conventions.
4. Run relevant tests, lint, type checks, or artifact validation.
5. Confirm no control-plane files, secrets, or unrelated changes entered the result.

In team mode, prefer an independent reviewer that did not produce the change. An
opposite model family can reduce correlated blind spots but is not a magic gate;
capability, context isolation, and evidence matter more than brand. The current lead
owns final acceptance regardless of provider.

Return findings in this format:

```text
path:line: severity: problem. fix.
```

If no actionable findings remain, report verification performed and residual risk.

## Handle failures

- Retry only after changing instructions, evidence, effort, role, or provider.
- Escalate capability when the current profile cannot resolve uncertainty.
- Preserve partial safe work and exact errors.
- Mark work blocked only when a concrete external decision or unavailable dependency
  prevents progress.
- Fall back to single-agent execution when coordination infrastructure fails and the
  remaining work is still safe and authorized.

## Finish

Report the delivered outcome, verification evidence, review type
(`self` or `independent`), and any residual risk. Do not claim completion while a
required check is red or a required deliverable remains.
