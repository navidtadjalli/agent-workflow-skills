# dispatch

A Telegram bot that queues headless `claude` and `codex` work against your plan
limits and stops cleanly before it hits them.

Python 3, standard library only. One process: it owns the chat socket, the
queue, two governors, the scheduler, and the workers.

## Why it exists

Work queued from chat has to survive the plan's rate-limit window. The failure
to avoid is not "we hit the limit" — that is inevitable — but hitting it
*badly*: a worker killed mid-edit, a dirty tree, no record of what it did, and
no way to pick the thread back up after the window resets.

So the daemon keeps a live estimate of how much of the window is spent, has a
stopping seam that is cheap to resume from, and keeps answering chat while
everything else is blocked.

## Talking to it

```
claude <task> on <repo>          queue it in the claude lane
codex  <task> on <repo>          queue it in the codex lane
claude <repo>: <task>            same, colon form
… on <repo> in a worktree        isolate it; do not serialize on the repo

ping                             pong — the liveness check
status                           both lanes, both governors, queue counts
queue                            live tasks, with their lane
usage                            free: estimates, codex limits, token volume
usage poll                       spends one request for the real numbers
sessions [project]               Claude Code session dashboard
repos                            every folder under ~/Projects, marked
logs <id> · cancel <id> · retry <id>
pause [lane] · resume [lane]     omit the lane for both
yes / no                         answer a free-form proposal
help
```

A run verb without a repo is refused rather than defaulted. Workers run with
permission prompts disabled, so "which repository" is a safety question and
guessing is the wrong answer.

Anything the parser does not recognise is sent to a small model, turned into
work, and **offered back for a `yes`** rather than queued. See "Free-form
intake asks first" in [../../docs/operations.md](../../docs/operations.md).

Every command above parses with zero model calls. That is deliberate: if
`status` needed a model to parse, it would be rate-limited exactly when you
most need to ask what is happening.

## Two lanes

`claude` and `codex` each get their own governor, concurrency ladder, and
wind-down cycle. They are independent in everything except the per-repository
lock, which is shared — a codex worker and a claude worker must never hold one
checkout at once, because both commit checkpoints to the same branch.

The governors differ in kind, not just degree:

- **claude** — the only source of truth is `claude -p /usage`, and asking costs
  a request against the limit it reports. So it polls rarely, measures a
  percent-per-token ratio across poll pairs, and interpolates for free from
  local transcript token counts in between.
- **codex** — writes the server's own rate-limit block into every turn of its
  session logs, so the reading is already on disk. No polling, no
  interpolation, no cost. Entries are keyed by `window_minutes`, because the
  same window arrives under either the `primary` or `secondary` slot depending
  on `limit_id`.

## Layout

```
dispatch/
  daemon.py      the tick: poll, settle, decide modes, take intake, dispatch
  cli.py         `dispatch` — same state as the daemon, works while it is down
  parser.py      chat text -> a command dict, with zero model calls
  chat.py        Telegram long-poll and send; nothing else
  scheduler.py   admission: may this task start right now?
  governor/      claude.py (poll + interpolate) · codex.py (read from disk)
  winddown.py    running -> winding-down -> frozen, and the way back
  worker.py      run one step under a wall-clock cap, then settle it
  backends/      claude.py · codex.py — argv, house rules, status contract
  worktrees.py   real git worktrees for isolated tasks
  repos.py       discovery under ~/Projects; git folders are dispatchable
  state.py       flock-guarded JSON, written temp-then-rename
  lanes.py       lane constants and the per-lane state migration
  sessions.py    the session dashboard behind `sessions`
  volume.py      token volume behind `usage`
  usage.py       /usage parsing and limit-error parsing
  config.py      defaults and on-disk locations
scripts/
  dispatch       CLI entry point
  dispatchd      daemon entry point — what `dispatch up` runs in tmux
```

Every module below the daemon is a pure function of injected clock and usage
values, which is why the whole decision surface is testable with no network,
no subprocess, and no real time passing.

## Running it

```bash
dispatch setup --chat <chat-id>   # writes config; disables the conflicting plugin
dispatch up                       # start in tmux (idempotent)
dispatch status                   # works whether or not the daemon is up
dispatch logs --daemon            # tail the daemon's own output
dispatch down
```

Nothing supervises the daemon except cron:

```
*/5 * * * * dispatch up --if-dead >/dev/null 2>&1
@reboot     dispatch up --if-dead >/dev/null 2>&1
```

That covers a crash and a reboot. It does not cover a daemon that starts and
then fails to poll — with chat as the only interface, that failure looks
exactly like a quiet day.

## State

```
~/.claude/dispatch/          (DISPATCH_HOME overrides it; tests use that)
  config.json     thresholds, chat allowlist, repo overrides
  queue.json      tasks
  state.json      governor snapshots, chat offset, per-lane mode, timers
  locks/<name>.lock
  tasks/<id>/     prompt.txt · steps.jsonl · worker.log
                  handoff.md — only once a task is paused, blocked, or failed
```

Both documents are written temp-then-rename and mutated only under `flock`, so
a crash mid-write cannot truncate them and the CLI cannot interleave a
read-modify-write with the daemon.

**The bot token is never copied here.** It is read at runtime from the channel
env file.

## Safety

Workers run without permission prompts, so repository content is untrusted
input to an unattended agent. Two boundaries hold that in:

- **Who can talk to it.** The chat allowlist, and it fails closed.
- **Where it can run.** Every git repo under `~/Projects` — *except* the
  repository this code itself lives in, which discovery drops. A worker there
  would `git checkout -B tg/<id>` in the tree the daemon reads its own code
  from, switching the branch under anyone working in it.

Workers never push, and never commit to the default branch. Every checkpoint
lands on `tg/<id>`.

## More

- [../../docs/operations.md](../../docs/operations.md) — setup, the governor's
  math, wind-down, and the recovery table
- [../../docs/superpowers/specs/2026-08-20-telegram-dispatch-design.md](../../docs/superpowers/specs/2026-08-20-telegram-dispatch-design.md)
  — the design and why each part is shaped the way it is
- `tests/dispatch_test.py` and `tests/dispatch_integration.py`, both run by
  `tests/run.sh`. No test spends a real request.
