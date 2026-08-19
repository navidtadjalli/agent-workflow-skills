# Dispatch — design

Status: implemented. Daemon `dispatchd`, CLI `dispatch`, skill `dispatch`.

## Problem

Work queued from chat has to survive the plan's rate-limit window. The failure
mode to avoid is not "we hit the limit" -- that is inevitable -- but hitting it
*badly*: a worker killed mid-edit, a dirty tree, no record of what it did, and
no way to pick the thread back up after the window resets.

So the system needs three things a bare job queue does not have: a live estimate
of how much of the window is spent, a stopping seam that is cheap to resume
from, and an intake path that keeps answering while everything else is blocked.

## Components

- **`dispatchd`** — Python 3 stdlib-only daemon under `systemd --user`. Owns the
  chat socket, queue, governor, scheduler, worker supervisor, timers, notifier.
  No model call in its hot path.
- **`dispatch` CLI** — same module: `status · queue · add · pause · resume ·
  cancel · logs · usage · setup · run`. Reads and writes the same locked state,
  so it works whether or not the daemon is up.
- **`dispatch` skill** — teaches a session to read that state and phrase worker
  tasks. Thin on purpose; the daemon is the system.

## State

```
~/.claude/dispatch/          (DISPATCH_HOME overrides, which is how tests run)
  config.json     thresholds, repo aliases, chat allowlist
  queue.json      tasks — durable, flock-guarded
  state.json      governor snapshot, chat offset, mode, armed timer
  locks/<repo>.lock
  tasks/<id>/     task.md · steps.jsonl · worker.log · handoff.md
```

Task record: `repo, prompt, state, priority, deps, isolation, branch,
session_id, steps_done, est_cost_pct, last_error`.

Both documents are written temp-then-rename and mutated only under flock, so a
crash mid-write cannot truncate them and the CLI cannot interleave a
read-modify-write with the daemon. The bot token is never copied into this tree;
it is read from the channel env file at runtime.

## Governor

Truth is `claude -p "/usage"` → `session_pct, session_reset, week_pct,
week_reset`. It is expensive: asking costs a request against the limit it
reports. So it is polled at most once per 60s, roughly every 10 minutes idle,
every 3 minutes hot, and always right after a step ends -- the moment the
estimate is least trustworthy.

Between polls the governor spends nothing. It re-reads the token totals the
local transcripts already record and projects forward:

```
session_pct ≈ last_polled_pct + (tokens_now − tokens_at_poll) × ratio
```

`ratio` is *measured*, not assumed: each pair of consecutive polls yields
`Δpct / Δtokens`, smoothed into a running value. Tokens-per-percent depends on
model mix, cache hit rate, and plan, none of which are knowable up front. Until
two polls exist, a conservative seed of 1% per million tokens is used. A pair
whose percentage went *down* measures nothing -- the window rolled over between
polls -- and is discarded.

If a worker dies with a real usage-limit error, the reset timestamp parsed out
of that error overrides the projection outright and pins usage at 100% until it
passes. Reactive truth beats proactive estimate.

Two situations are marked `stale`, and nothing is admitted on a stale reading:
no successful poll has ever happened, or the window reset and the token baseline
still refers to the previous window.

## Scheduler

A task is admitted only if **all** hold:

1. `deps` satisfied
2. repo lock free — same repo serializes; `isolation=worktree` takes a
   per-worktree lock instead
3. `mode == running`
4. usage is not stale
5. `week_pct < week_soft`
6. `running < max_concurrency(session_pct)` — `<40% → 3 · 40–65% → 2 ·
   65–85% → 1 · ≥85% → 0`
7. `session_pct + est_cost_pct(task) ≤ session_soft`

`est_cost_pct` is learned per repo from observed step costs, seeded
conservatively. Ordering is paused-before-queued, then priority, then id:
a paused task already carries loaded context, so finishing it is cheaper than
starting a fresh one.

## Wind-down

```
soft 85%  mode=winding-down — no new dispatch, no new step;
          in-flight step runs to completion
step end  checkpoint: commit dirty tree to tg/<id>, save session_id,
          write handoff.md, task → paused
hard 95%  SIGTERM stragglers (20s grace → SIGKILL), salvage-commit
all down  mode=frozen; resume armed for reset+60s;
          notify: done / paused / resumes-at
```

Letting the in-flight step finish at the soft limit is deliberate: killing it
would waste everything it already spent and leave the tree dirty. The hard limit
changes only *how* in-flight work ends, not where the system lands.

## Resume

The timer only wakes the daemon; the reset is confirmed by a fresh poll before
anything is dispatched. A timer that fired early would otherwise spend the new
window's first request hitting the same wall. Paused tasks drain first, resuming
against their saved session id; if that session is gone, a fresh worker is
seeded with `handoff.md` and `git log tg/<id>`.

## Intake while frozen

The daemon never stops accepting. A zero-model fast path covers `run <text> on
<alias>`, `status`, `queue`, `usage`, `logs <id>`, `cancel <id>`, `pause`,
`resume`. Free-form text goes to a small parse call; if that is itself rate
limited, the raw text is stored `needs_parse` and parsed after the reset. Either
way the reply is immediate: `queued t-0012 · frozen · resumes ~7:50pm`.

## Worker contract

```
cd <repo|worktree> && claude -p "<task + house rules>" \
  --dangerously-skip-permissions --output-format json [--resume <sid>]
```

House rules are injected every step: do one coherent chunk then stop, commit
checkpoints to `tg/<id>`, never push, never touch the default branch, end with a
fenced JSON status block `{"status": "complete|continue|blocked", "summary":
..., "next": ...}`. That block -- not the exit code -- decides whether the task
is done, requeued, or blocked. A step with no parseable block is `failed`, never
silently treated as complete. The supervisor also enforces a wall-clock cap per
step.

## Testing

`tests/dispatch_test.py` covers the decision surface as pure functions with an
injected clock and injected usage: parser, governor threshold and ratio math,
admission rules, wind-down transitions, worker status parsing, state durability.
`tests/dispatch_integration.py` drives whole ticks against a stub agent on PATH
and a real git fixture -- including the limit-error freeze and the confirmed
resume. No real API call, no real chat call, no real usage spent. Both run from
`tests/run.sh`.

## Setup and risks

`dispatch setup` writes config and prints the systemd unit; it installs nothing
and starts nothing. It refuses outright while `telegram@claude-plugins-official`
is enabled, because two consumers of one bot get 409s from the chat API, and it
never edits the user's settings to resolve that itself.

Two risks worth naming. Workers run with `--dangerously-skip-permissions`, so
repository content is untrusted input to an unattended agent -- only aliased
repos are dispatchable, and that alias list is the whole trust boundary.
And `/usage` polling itself costs requests, which is why the 60s floor exists
and why nothing is polled while frozen.

## Deviations from the original plan

- **Resume timer is internal, not `systemd-run`.** The daemon is already alive
  and already ticking; arming an external one-shot unit would add an install
  step and a second thing that can drift out of sync with `state.json`. The
  armed timestamp is stored in state and checked each tick.
- **`dispatch setup` never disables the conflicting plugin.** The plan said
  "refuses to start until removed"; the implementation refuses and prints
  instructions, and will not edit `settings.json` under any flag.
