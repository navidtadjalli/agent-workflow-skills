---
name: dispatch
description: Queue and supervise headless agent work against a plan-usage budget. Use when work should run in the background, continue across a rate-limit window, be dispatched from chat, or be serialized per repository. Covers queueing tasks, reading queue and usage state, pausing and resuming, and phrasing worker tasks so they checkpoint and stop cleanly. Do not invoke for work the current session should just do now.
---

# Dispatch

The daemon is the system. This skill is how an interactive session reads that
system's state and puts well-shaped work into it. Do not reimplement queueing,
governing, or scheduling in a session -- drive the CLI.

## Know what is running before anything else

```bash
dispatch status      # both lanes: mode, usage, queue counts
dispatch queue       # live tasks
dispatch usage       # cached estimate; --poll spends a request for the truth
```

`mode` is the fact that matters:

| Mode | Meaning | What to tell the user |
|---|---|---|
| `running` | Dispatching normally | Queue depth and what is in flight |
| `winding-down` | Past the soft limit; in-flight step finishes, nothing new starts | What will still land, what waits |
| `frozen` | Nothing running; resume armed for after the window resets | When it resumes |
| `paused` | A human paused it | That it needs an explicit `dispatch resume`; usage never lifts it |

Never say work "failed" when the mode is `winding-down` or `frozen`. It is
deferred, and its branch and handoff note are intact.

## Queue work

```bash
dispatch add <repo> "<task>"
dispatch add <repo> "<task>" --agent codex         # the codex lane; default is claude
dispatch add <repo> "<task>" --worktree            # isolate; do not serialize on the repo
dispatch add <repo> "<task>" --dep t-0007          # wait for another task
```

`<repo>` is a folder under the projects root, discovered each time -- there is
no hand-maintained alias list any more. A folder without a `.git` is listed but
refused, because a worker there would have nowhere to checkpoint. Workers run
without permission prompts, so "every git repo under the projects root" is the
trust boundary; if a name is refused, say so rather than widening it.

## Phrase tasks so they survive a wind-down

The supervisor injects the house rules -- one chunk then stop, checkpoint to the
task branch, never push, end with a status block. Your job is the part it cannot
supply:

- State the outcome and the acceptance evidence, not a procedure.
- Name the verification command the worker should run before claiming complete.
- Say what is out of scope, because a worker with no permission prompts will
  otherwise take the widest reading.
- Keep each task to one coherent deliverable. Split with `--dep` instead of
  writing a task with three phases in it.

A task that cannot be verified from inside the repo does not belong in the
queue; do it in session instead.

## Read results

```bash
dispatch logs t-0007       # raw worker output
dispatch logs --daemon     # what the daemon itself is saying
dispatch cancel t-0007     # running tasks finish their current step first
```

Per-task artifacts live under `~/.claude/dispatch/tasks/<id>/`: `steps.jsonl`
(one line per step), `worker.log`, and `handoff.md` for anything paused or
blocked. When reporting to the user, read `steps.jsonl` for the shape of the
work and `git log <branch>` for what actually landed -- the worker's own summary
is a claim, not evidence.

## Do not

- Do not start, stop, or install the daemon as a side effect of another task.
- Do not edit `~/.claude/dispatch/*.json` by hand; the CLI holds the locks.
- Do not poll `/usage` in a loop. Each poll spends a request against the limit
  it reports; the daemon already paces this.
- Do not enqueue work that needs a human decision mid-flight. It will come back
  `blocked` after burning a step.

See `references/operations.md` for setup, the governor's math, and recovery.
