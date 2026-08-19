---
name: taskforge
description: Turn a feature, PRD, project, migration, audit, or rough multi-step idea into a numbered, dependency-ordered, resumable initiative under .tasks. Use for taskforge, breaking work into tasks, durable execution plans, acceptance-driven decomposition, or plans that may run across sessions. Generate plans that one capable agent can execute sequentially or that the orchestrate skill can run with an authorized team. Do not use for quick answers or small edits that need no durable plan.
---

# Taskforge

Create a durable execution control plane, not ceremony. Make every task independently
understandable, verifiable, and resumable. Keep single-agent execution complete;
team execution is an optional optimization through `orchestrate`.

## Establish policy

Before writing files:

1. Read repository instructions and inspect relevant code, docs, tests, and worktree
   state.
2. Clarify only material ambiguity that would change scope, architecture, acceptance,
   or authority. Otherwise record assumptions.
3. Select an execution mode:
   - **plan-only** when the user asks only for a plan;
   - **single** by default when the user asks to execute or finish;
   - **team** only when delegation is requested or authorized and useful.
4. Record mutation policy, commit policy, push policy, protected paths or branches,
   and whether the control plane is local or tracked.

Default to a local, untracked `.tasks/` control plane, no commits, and no pushes
unless the user grants broader authority. Do not modify `.gitignore` merely to hide
the initiative.

## Decompose

Create the fewest tasks that preserve clear ownership and verification. For each
task define:

- zero-padded number and imperative title;
- outcome and explicit non-goals;
- role: `architect`, `builder`, `dev`, `qa`, or `scribe`;
- profile: `strong`, `balanced`, or `fast`;
- dependencies and phase or concurrency group;
- concrete steps and edge cases;
- exact acceptance commands or artifact checks;
- expected evidence and mutation/commit authority.

Put hard phase boundaries wherever later work depends on an earlier safety net.
Do not create hollow tasks that produce no independently reviewable result.

## Materialize the initiative

Choose the next free two-digit prefix under `.tasks/` and create:

```text
.tasks/<NN>-<initiative-slug>/
├── readme.md
├── acceptance.md
├── NN-<task-slug>.md
└── prompts/
    ├── 00-ORCHESTRATOR.md
    ├── STATE.md
    ├── _shared.md
    └── task-NN.md
```

Read every template completely before filling it:

- [templates/orchestrator.template.md](templates/orchestrator.template.md)
- [templates/state.template.md](templates/state.template.md)
- [templates/shared.template.md](templates/shared.template.md)
- [templates/task.template.md](templates/task.template.md)
- [templates/spec.template.md](templates/spec.template.md)
- [templates/readme.template.md](templates/readme.template.md)
- [templates/acceptance.template.md](templates/acceptance.template.md)

Resolve every `{{PLACEHOLDER}}`. Keep detailed requirements in each numbered spec;
keep `prompts/task-NN.md` as a thin route to the spec and shared rules.

Resolve and record the actual Taskforge and Orchestrate skill directories. Prefer the
loaded skill paths; otherwise check `$HOME/.agents/skills/` and
`$HOME/.claude/skills/`. Do not assume a single vendor-specific home.

## Protect state ownership

`prompts/STATE.md` is the only progress ledger. Only the lead role changes it:

- In **single** mode, the current agent updates it only when switching back to lead
  after implementation and verification.
- In **team** mode, workers never edit `STATE.md`, any other prompt, or the runbook.
  They report evidence; the lead inspects it and changes state.

Use [scripts/ledger.sh](scripts/ledger.sh) to detect accidental or out-of-role ledger
drift and retain recovery snapshots. Treat it as a recovery mechanism, not a security
boundary: another process with the same account and filesystem access can also alter
the snapshot store.

Seal only a known-good lead update. On a mismatch, inspect the diff and repository
evidence before deciding whether to restore. Never accept a state transition merely
because a worker claims it.

## Validate

Run:

```bash
scripts/validate-initiative.sh .tasks/<NN>-<initiative-slug>
```

Fix every missing file, unresolved placeholder, invalid status, prompt/spec mismatch,
or worker instruction that attempts to mutate `STATE.md`. Then seal the initial
ledger baseline.

## Execute or hand off

If the user requested execution, read
[references/execution-loop.md](references/execution-loop.md) and run the initiative
now. Do not require a fresh session or a particular model. Continue in the current
session while safe progress remains.

If the user requested only planning, return:

- the initiative path;
- mode and policy choices;
- task/phase summary;
- validation result;
- the START/RESUME prompt from `00-ORCHESTRATOR.md`.

Never claim a heartbeat, background wake, independent review, commit, or test run that
the current host did not actually provide.
