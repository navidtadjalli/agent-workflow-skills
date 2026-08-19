# Dispatch operations

## Setup

`dispatch setup` writes config and prints the systemd unit. It deliberately does
not install or start anything, and it refuses outright while an in-session chat
plugin owns the same bot -- two consumers polling one bot get 409s, and the
in-session one is probably the socket you are talking to right now. It will
never edit your settings for you.

```bash
dispatch setup --repo qpay=~/Projects/qpay-backend --chat <chat-id>
```

State lives under `~/.claude/dispatch/` (override with `DISPATCH_HOME`):

```
config.json   thresholds, repo aliases, chat allowlist
queue.json    tasks
state.json    governor snapshot, chat offset, mode, armed resume
locks/        flock files, one per repo or isolated worktree
tasks/<id>/   task.md · steps.jsonl · worker.log · handoff.md
```

The bot token is never copied here. It is read at runtime from the channel env
file.

## What the governor actually knows

Only `/usage` reports real plan percentages, and asking costs a request against
the limit it reports. So:

1. A real poll records `(percent, tokens)`. Two polls give a measured
   percent-per-token ratio for this plan, model mix, and cache behaviour.
2. Between polls the daemon re-reads the local transcript token counter -- free
   -- and projects `last_percent + delta_tokens * ratio`.
3. A worker that dies with a genuine usage-limit error overrides all of it: the
   reset timestamp in that error is truth, the projection is not.

Polls are floored at 60s apart no matter who asks, and never happen while
frozen.

The concurrency ladder follows the estimate: below 40% three workers, to 65%
two, to 85% one, above that none.

## Wind-down and resume

At the soft limit the mode becomes `winding-down`: no new dispatch, no new step,
but the in-flight step runs to its own stopping point -- killing it mid-edit
would waste what it already spent. At the step boundary the tree is committed to
the task branch, the session id and a handoff note are saved, and the task is
paused. At the hard limit stragglers get SIGTERM, a grace period, then SIGKILL,
and whatever they produced is salvage-committed.

Once nothing is running the mode is `frozen` and a resume is armed for just
after the reported reset. When it fires the daemon re-polls before believing it;
a timer that fires early would spend the new window's first request hitting the
same wall. Paused tasks drain before queued ones, because they carry loaded
context: they resume against their saved session id, or, if that session is
gone, a fresh worker is seeded with `handoff.md` and the branch history.

## Recovery

| Symptom | Cause | Action |
|---|---|---|
| `usage unknown (never polled)` | No successful poll yet | `dispatch usage --poll` |
| Task stuck `running`, no process | Daemon died mid-step | Restart the daemon; the lock is released with the process, then `dispatch cancel` and re-add |
| Task `failed`, log looks complete | Worker omitted its status block | `dispatch logs <id>`, then re-add with the acceptance restated |
| Everything `blocked` on one repo | Repo path missing from config | Re-run `dispatch setup --repo alias=path` |
| Mode stays `frozen` past the reset | Reset time was misparsed | `dispatch usage --poll`, then `dispatch resume` |

## Safety

Workers run without permission prompts, so repository content is untrusted input
to an unattended agent. Only aliased repos are dispatchable, and that alias list
is the entire trust boundary -- keep it short. Workers never push and never
commit to the default branch; every checkpoint lands on the task branch.
