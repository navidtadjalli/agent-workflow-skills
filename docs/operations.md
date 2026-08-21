# Dispatch operations

## Setup

`dispatch setup` writes config. It starts nothing -- `dispatch up` does that --
but it does make one edit outside its own state: it turns off the in-session
chat plugin that owns the same bot, because two consumers polling one bot get
409s. Your `settings.json` is copied verbatim to `settings.json.bak` first, and
one entry changes: `enabledPlugins` is either a map, in which case the plugin's
boolean becomes `false`, or a list, in which case the plugin's entry is removed.
Nothing else in the file is touched, and the original key order is kept.

**Setup exits non-zero if the plugin is still enabled when it finishes.** It
still writes the config and prints every warning first -- only the verdict
changes. One token admits exactly one consumer, so starting the daemon in that
state produces a bot that 409s on every poll while `status`, `queue`, tmux and
the watchdog all stay green. Turn the plugin off by hand and re-run.
Pass `--keep-plugin` to be warned instead of edited; that is a deliberate
choice, and it still exits 0.

```bash
dispatch setup --repo qpay=~/Projects/qpay-backend --chat <chat-id>
dispatch up          # start the daemon in tmux; idempotent
dispatch down        # stop it
dispatch logs --daemon   # what the daemon is saying right now
```

State lives under `~/.claude/dispatch/` (override with `DISPATCH_HOME`):

```
config.json   thresholds, repo overrides, chat allowlist, projects root,
              codex sandbox mode
queue.json    tasks
state.json    governor snapshot · chat offset · per-lane mode and armed resume
              · chat transport health (last error, consecutive failures, when)
              · hold_reason: why each lane last started nothing
              · repo_cost_pct (reserved; nothing writes it yet)
daemon.log    tracebacks the tmux pane cannot keep, plus startup failures;
daemon.log.1  rolls over once at 1MB
locks/        flock files: `repo-<name>` per repo, `worktree-<id>` per
              isolated task, `worktree-add-<repo>` around the git metadata
              mutation that creates or removes one
tasks/<id>/   prompt.txt · steps.jsonl · worker.log · handoff.md
              · last.json (codex only; cleared before every step, so a
                step that dies cannot be read as the previous one)
worktrees/    one linked git worktree per live `isolation=worktree` task,
<id>/         named for the task; see "Isolated worktrees" below
```

`daemon.log` is the one that matters after a crash: the tmux session dies with
the daemon and takes its scrollback with it, so `logs --daemon` falls back to
this file when there is no pane left to read.

The chat-health fields never contain the bot token -- the transport redacts it
out of any error message before recording it.

The bot token is never copied here. It is read at runtime from the channel env
file.

If `config.json` cannot be read -- a truncated write, a bad hand-edit -- every
command says so on stderr and falls back to the defaults. The defaults carry an
empty allowlist, which now means *nobody*, so the fallback refuses chat rather
than opening it. `dispatch setup` keeps the unparseable file as
`config.json.corrupt` before it writes a new one, because the repo overrides and
chat ids in there are the hardest part to reconstruct.

Config keys worth knowing (all optional; the rest are governor thresholds):

```
chat_allowlist   chat ids allowed to drive the daemon. Empty means nobody.
projects_root    where repos are discovered (default ~/Projects)
repos            alias -> path, for repos outside that root
codex_sandbox    how codex workers are confined (default approve-for-me)
worktree_root    where isolated tasks get their checkout
                 (default <DISPATCH_HOME>/worktrees)
```

## Isolated worktrees

`codex bump deps on qpay in a worktree` in chat, or `dispatch add qpay "..."
--worktree`, marks a task `isolation=worktree`. That is a claim about
contention: the scheduler gives such a task a `worktree-<id>` lock instead of
the shared `repo-<name>` one, so two of them on the same repository are both
admitted at once. The daemon therefore has to give each one a checkout of its
own, and it does -- a real `git worktree`, created just before the first step
and reused by every later step of that task.

**It never falls back to the parent checkout.** If the worktree cannot be
created the task is `blocked` with the git error attached, and chat is told.
Running it in the shared tree instead would be two unattended agents doing
`git checkout -B` and `git add -A && git commit` in one working directory,
which is the failure the lock name already claimed was impossible.

```
~/.claude/dispatch/worktrees/t-0042      the task's checkout, on branch tg/t-0042
~/Projects/qpay                          untouched: its HEAD never moves
```

The location is outside `projects_root` on purpose. A linked worktree's `.git`
is a *file*, and repo discovery treats a `.git` file as a dispatchable
repository, so a worktree parked among the checkouts would be listed by `repos`
and reachable from chat under its own name -- another lane pointed straight
into an isolated task's private tree, under a different lock.

Commits go where they always went: `tg/<id>` in the parent repository. The
worktree holds nothing the branch does not, which is what makes it disposable.

| Task ends | Worktree |
|---|---|
| `done`, `cancelled`, or gone from the queue | removed, with its registration pruned. The branch stays |
| `paused` | **kept** -- the next step after the reset resumes into it |
| `blocked`, `failed` | kept -- `handoff.md` names the directory, and whatever the last step left uncommitted lives nowhere else |

Kept trees are released by `dispatch cancel <id>`; the sweep runs off the queue
once a tick, so it also collects a task cancelled while it was parked and one
left behind by a daemon that died mid-step. Nothing collects the tree of a task
you leave `blocked` forever -- that is deliberate, and it is a disk cost.

Codex workers in a worktree are confined the same way as anywhere else -- see
**Codex confinement past the first step** below. A worktree is its own git repo
root, so codex's trust map never covers one; `worktree_root` exists for other
reasons, but moving worktrees somewhere "trusted" is not one of them and does
not work.

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
| Everything `blocked` on one repo | The folder lost its `.git`, or was renamed or moved out of the projects root | `repos` in chat lists what is dispatchable; a repo outside the root needs `dispatch setup --repo alias=path` |
| A stored free-form message came back `blocked` | It parsed to a repo that is not dispatchable; nothing, or only part, was queued -- the notice says which | Re-send it as `claude <task> on <repo>` |
| Mode stays `frozen` past the reset | Reset time was misparsed | `dispatch usage --poll`, then `dispatch resume` |
| A lane reads `frozen` with a low session percentage | Its weekly window is at or above the soft limit | Nothing: it resumes when the week resets. `dispatch status` shows both windows |
| The bot answers nothing, everything else looks healthy | Chat transport is failing | `dispatch status` prints the last transport error and how many polls have failed in a row |
| The bot answers nobody, and `status` says the allowlist is empty | `config.json` is missing, did not parse, or holds something that is not a list of ids | `dispatch setup --chat <chat-id>`, then **`dispatch down && dispatch up`** -- the transport is built once at startup and is never rebuilt, so a config edit does not revive a running daemon. Check stderr for a `config.json.corrupt` line first |
| Session gone, no explanation in the pane | The daemon died with it | `dispatch logs --daemon` falls back to `daemon.log`, which outlives the session |
| Task `failed` with `checkpoint failed: ...` | The step's work could not be committed | Fix the repo (a stale lock, a conflicting ref), then re-add; the tree still holds the work |
| Task `blocked` with `no worktree: fatal: invalid reference: HEAD` | `isolation=worktree` against a repository with no commits | Make one commit in the repo, then `dispatch retry <id>` |
| Task `blocked` with `no worktree: fatal: 'tg/<id>' is already used by worktree at ...` | The task branch is checked out somewhere else | Move that checkout off the branch, or remove the stale worktree with `git worktree remove`, then `dispatch retry <id>` |
| Task `blocked` with `no worktree: ... is not a git worktree` | Something else is sitting at the task's worktree path | Look at it, move it out of the way yourself -- the daemon will not delete a directory it did not create -- then `dispatch retry <id>` |
| A codex task reports success from step two onward while changing nothing | Fixed: continuations were falling back to codex's own trust configuration, which is read-only for most paths. See "Codex confinement past the first step" |

## Safety

Workers run without permission prompts, so repository content is untrusted input
to an unattended agent. Every git repo under the projects root is dispatchable
-- that is the trust boundary, and the `repos` chat verb is what prints it. Workers never push and never
commit to the default branch; every checkpoint lands on the task branch.

**The chat allowlist is the authentication boundary, and it fails closed.** An
empty allowlist admits nobody: `Chat` refuses to be built without one, and the
daemon runs with no chat transport, printing why at startup and reporting it in
`dispatch status`, rather than answering every chat that finds the bot. It still
starts, so the queue and `dispatch add` keep working while the allowlist is
fixed -- a daemon that refused to start would just be relaunched by the watchdog
every five minutes.

Anything that is not a list of ids is treated as empty, not as a best effort.
`"chat_allowlist": "7256243815"` means that one id (not ten single-character
ones); an unquoted number means that id; `true`, an object, or a list of blanks
mean nobody, and land on the same refusal. The daemon, the transport and the
CLI all read the value through one function, so they cannot disagree about what
it means. Note that the allowlist is read once at startup: after editing it,
`dispatch down && dispatch up`.

**Codex workers run inside codex's own sandbox.** `codex_sandbox` selects the
mode, and the default is `approve-for-me`: unattended, with approval requests
routed through codex's automatic review, and the workspace confined to the repo
the task names. The other values are `read-only`, `workspace-write`,
`danger-full-access`, and `bypass` -- the last being
`--dangerously-bypass-approvals-and-sandbox`, the previous behaviour, kept as an
explicit opt-out for when a task genuinely needs it (network installs, work
outside the workspace) or if the reviewed mode turns out to stall. It is one
config value and a daemon restart, not a code change:

```bash
python3 - <<'EOF'
import json, os
path = os.path.expanduser("~/.claude/dispatch/config.json")
cfg = json.load(open(path))
cfg["codex_sandbox"] = "bypass"
json.dump(cfg, open(path, "w"), indent=2, sort_keys=True)
EOF
dispatch down && dispatch up
```

### Codex confinement past the first step

`codex exec resume` parses a narrower set of options than `codex exec` and
accepts neither `-s` nor `--approve-for-me` (measured on codex-cli 0.148.0);
it is also not passed `-C`, which that parser rejects outright, though the
worker launches the process in the repository either way.

**This used to mean `codex_sandbox` applied only to a task's first step.** A
continuation carried no sandbox flag at all and fell back to codex's own trust
configuration -- and that resolves on the **git repository root**, not on any
ancestor directory. Measured on this machine with `codex debug prompt-input`,
which renders the prompt locally with no session and no request:

```
~/Projects                        workspace-write   (a `projects` entry; not a repo)
~/Projects/qpay-backend           workspace-write   (its own `projects` entry)
~/Projects/qpay-backend/anything  workspace-write   (same repo root)
~/Projects/agent-workflow-skills  read-only         (a repo with no entry of its own,
                                                     even though ~/Projects has one)
/tmp/anywhere                     read-only
```

So every repository without a `projects` entry of its own, and every isolated
worktree without exception, ran **read-only from step two onward** -- reading
and thinking and changing nothing, then reporting `complete`. That is the
"succeeds while doing nothing" shape, and it was not visible from any surface.

**It is fixed.** A resumed step now carries the configured mode as `-c`
overrides, which `resume` does accept:

| `codex_sandbox` | first step | every later step |
|---|---|---|
| `read-only` | `-s read-only` | `-c sandbox_mode="read-only"` |
| `approve-for-me` (default) | `--approve-for-me` | `-c sandbox_mode="workspace-write"` |
| `workspace-write` | `-s workspace-write` | `-c sandbox_mode="workspace-write"` |
| `danger-full-access` | `-s danger-full-access` | `-c sandbox_mode="danger-full-access"` |
| `bypass` | `--dangerously-bypass-approvals-and-sandbox` | the same flag |

`approve-for-me` maps to `workspace-write` because that is what the flag itself
selects: its own help reads "route approval requests through automatic review
using the workspace-write sandbox".

**The sandbox now matches on every step; approvals do not, and cannot.** The
other half of `--approve-for-me` is the automatic reviewer, and no way was found
to turn it on for a resumed step -- the flag is rejected and
`approval_policy = "granular"` is execpolicy rule matching, not automatic
review. So resumed steps also carry `-c approval_policy="never"`, which tells
the model up front that escalation will be refused. The alternative is worse:
with the policy unset the model is handed the full escalation instructions and
invited to ask, unattended, for something no reviewer and no human will answer
-- a step that stalls until `step_timeout` kills it. What is lost is an
escalation that automatic review would have granted, such as a network install;
what is kept is everything inside the workspace, which is what a checkpointing
worker needs.

`--strict-config` is deliberately not used: it rejects unrecognized fields in
`config.toml`, so one stale key in your own file would fail every continuation
of every task while first steps kept working.

Claude has no equivalent flag: `--dangerously-skip-permissions` is how a
headless Claude worker runs unattended, and the confinement there is the repo it
is pointed at.

If a codex step ever fails with `error: unexpected argument` and exit status 2,
that is this class of bug and not the agent: the flag was rejected by the parser
before any work happened, so there is no status file and the task settles as
`failed`. `dispatch logs <id>` shows the parse error verbatim.

