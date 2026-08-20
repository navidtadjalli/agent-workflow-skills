# Telegram-only dispatch — design

Status: designed, not implemented. Extends `2026-08-18-dispatch-design.md`;
that document remains accurate for everything not restated here.

## Problem

Dispatch was built as one of two doors: a daemon you could reach from chat, and
a terminal you could reach directly. It was never installed, and the door that
did exist -- the `telegram@claude-plugins-official` bridge -- is an MCP stdio
child of whichever Claude session enabled it. When that session exits the bridge
dies, and because Telegram permits exactly one `getUpdates` consumer per token,
a newly started session silently steals the channel from an older one. An hourly
cron existed only to detect and repair that.

The decision this document implements is to stop having two doors. Telegram
becomes the sole interface: every prompt arrives there, every answer leaves
there, and the daemon -- not a session-parented bridge -- owns the socket. That
removes the failure the cron was guarding, and replaces it with a different one
worth naming up front: if the daemon dies, there is no other way in.

Three capabilities have to survive the consolidation, because they are the
reason the interface is worth having: usage reporting across both agents, a
queue you can see, and the ability to run either agent against a named repo.

## What changes

| Area | Before | After |
|---|---|---|
| Agents | `claude` only, argv-embedded prompt | `claude` and `codex`, prompt on stdin from a file |
| Governor | one, Claude plan | two, independent, per lane |
| Concurrency | one ladder | one ladder per lane; repo locks shared |
| `mode` | a string | a dict keyed by lane |
| Repos | hand-configured aliases | discovered from `~/Projects` each tick |
| `usage` | governor estimate | estimate + codex + volume; `usage poll` for truth |
| Chat verbs | `run … on <repo>` | plus `claude`, `codex`, `sessions`, `repos` |
| Supervision | systemd unit, printed not installed | tmux session, cron watchdog |
| Terminal skills | `dispatch`, `manage` | removed; `orchestrate`, `taskforge` kept |

## Backends

`worker.py` hardcodes `["claude", "-p", prompt, …]`. That becomes a two-member
family under `dispatch/backends/`, each exposing the same three functions:

```python
build_command(task, prompt_path, cwd) -> argv
parse_result(stdout, task_dir)        -> {status, summary, next, session_id}
resume_args(session_id)               -> [str]
reset(task_dir)                       -> None
```

`reset` is called immediately before each step launches and clears whatever the
backend reads its status from on disk. Codex reports through a file written by
`-o`, which it does not touch when it dies; without the clear, a step killed at
`step_timeout` reads the previous step's block and settles as that step's
success. Claude reports in its own stdout, so its `reset` does nothing.

The prompt is written once at enqueue to `tasks/<id>/prompt.txt` and fed on
stdin. Three reasons, in order of how likely each is to bite: shell quoting of
arbitrary chat text is a correctness problem, not a style one; `ARG_MAX` caps a
long prompt; and the exact bytes the agent received end up on disk next to
`steps.jsonl`, which is what makes a failed step diagnosable after the fact.

```
claude   claude -p - --output-format json
                --dangerously-skip-permissions
                [--resume <sid>]                        < prompt.txt

codex    codex exec [resume <sid>] - --json -C <cwd>
                --skip-git-repo-check
                --output-schema <pkg>/backends/status.schema.json
                -o <tasks/<id>/last.json>
                --dangerously-bypass-approvals-and-sandbox
                                                        < prompt.txt
```

Note that codex continuation is a subcommand (`codex exec resume <sid>`), not a
trailing flag as it is for Claude (`--resume <sid>`). `resume_args` therefore
returns a positional fragment for codex and an option pair for Claude, and
`build_command` inserts it at the position that backend requires. The status
schema is a static file shipped with the package; `last.json` is per task.

Codex gets a stronger status contract than Claude. `--output-schema` forces the
final message to match `{"status", "summary", "next"}`, and `-o` writes it to a
file, so `parse_result` reads JSON rather than hunting a fenced block in a JSONL
event stream. Claude keeps the fence regex, unchanged. Both normalise to the
same three states -- `complete | continue | blocked` -- so `next_state()`, the
checkpoint logic, and the whole wind-down path are untouched.

`task.agent` is a new field, `"claude"` or `"codex"`, defaulting to `"claude"`
when absent so a queue written by the previous version still loads.

House rules are injected for both. The codex variant drops the fenced-block
instruction, since the schema enforces it, and keeps the rest: one coherent
chunk then stop, checkpoint to `tg/<id>`, never push, never touch the default
branch, report `blocked` rather than guess.

## Two governors

`governor.py` splits. `governor/claude.py` is today's code verbatim: poll
`claude -p /usage`, measure a percent-per-token ratio across poll pairs,
interpolate on free transcript token counts between polls, 60s floor, reactive
override from a real limit error.

`governor/codex.py` needs none of that, because codex writes its own limits to
disk. Every `token_count` event in `~/.codex/sessions/**/*.jsonl` carries:

```json
"rate_limits": {
  "primary":   {"used_percent": 99.0, "window_minutes": 300,   "resets_at": 1782934544},
  "secondary": {"used_percent": 95.0, "window_minutes": 10080, "resets_at": 1783527387}
}
```

So the codex governor reads the newest record with a non-null limit and is done.
No request is spent, no ratio is measured, no interpolation is needed. Three
details the data forces:

- **Key by `window_minutes`, not by slot.** The same window appears under
  `primary` on some records and `secondary` on others, depending on `limit_id`
  (`codex` vs `premium`). 300 is the session window, 10080 the week.
- **`null` is common and means nothing.** Records with both limits null are
  skipped, not treated as zero.
- **A record whose `resets_at` has already passed is discarded.** It describes a
  window that has since rolled over.

Freshness differs in kind from the Claude governor's. A codex percentage is
exactly as old as your last codex invocation, and no amount of waiting improves
it. There is therefore no `stale` gate that blocks admission: if the newest
reading has expired, the lane starts optimistic and the first task's own output
corrects it. Blocking instead would deadlock -- the only way to get a fresh
reading is to run codex.

Both governors write into one snapshot, `{"claude": {...}, "codex": {...}}`.
Thresholds are per lane in `config.json`, defaulting to the existing values.

## Lanes

Admission moves from global to per-lane. A task is admitted when:

1. `deps` satisfied
2. repo lock free
3. **its own lane's** `mode == running`
4. **its own lane's** governor permits it
5. `running_in_lane < max_concurrency(lane_pct)`
6. `lane_pct + est_cost_pct(task) <= lane_session_soft`

The repo lock stays global across lanes. That is the only coupling between them,
and it is deliberate: a codex worker and a claude worker editing one checkout at
once would interleave commits on the same branch.

**Correction to the 2026-08-18 design on `est_cost_pct`.** That document says it
is "learned per repo from observed step costs, seeded conservatively". Only the
seed is real. `governor.est_cost_pct` reads `state.json`'s `repo_cost_pct` and
falls back to `default_est_cost_pct` (6.0); `governor.learn_cost` exists and is
tested, but nothing in the daemon calls it, so `repo_cost_pct` is never written
and the value used by rule 6 is always the 6% default. Read the rule as "a flat
6% headroom check", not as an adaptive one.

*Known gap, deliberately left open here:* wiring the learning needs a per-step
cost actually observed somewhere in the settle path -- the session percentage
before the step against the one after it -- and the Claude governor only has a
real number at a poll, which is paced behind a floor and is not aligned with
step boundaries. Estimating the delta from the projection would teach the model
its own guess. Until there is a measurement worth learning from, the constant is
the honest implementation.

**Both windows drive wind-down.** Rule 6 is not the only per-lane gate:
`week_pct >= week_soft` refuses admission too, and the wind-down machine reads
the same predicate. A lane at or above its weekly soft limit is `frozen` -- not
`running` -- and its resume is armed for the weekly reset rather than the
session one. Reporting `running` while admitting nothing would invert the one
distinction this document asks the user to read: `running` is dispatching,
`frozen` is deferred.

```
claude  session 91%  ->  winding-down, resume ~7:50pm
codex   5h 12%       ->  running, 2 slots free
qpay lock held       ->  both lanes wait, whoever asked first
```

`state.json`'s `mode` becomes `{"claude": "running", "codex": "frozen"}`. The
reader accepts the old string form and expands it to both lanes, so an existing
state file loads. `armed_resume_at` becomes per-lane for the same reason.

Wind-down, checkpointing, freeze, and confirmed resume all keep their existing
semantics -- they simply run twice, over disjoint task sets.

**Known starting condition:** the codex 7-day window currently reads 100% with a
reset around 2026-08-21. The codex lane will be frozen from first boot. This is
correct behaviour and must not be read as a defect.

## Chat surface

The parser gains two run verbs and two read verbs. Both run verbs require an
explicit repo -- a bare `claude <prompt>` is rejected with the dispatchable
list rather than defaulting anywhere. Workers run without permission prompts, so "which
repository" is a safety question, and guessing is the wrong answer.

```
claude <prompt> on <repo>          queue, agent=claude
codex  <prompt> on <repo>          queue, agent=codex
claude <repo>: <prompt>            colon form, same thing
… on <repo> in a worktree          isolation=worktree, as before

run <task> on <repo>               unchanged; agent=claude
status · queue · usage · usage poll · sessions · repos
logs <id> · cancel <id> · retry <id> · pause · resume · help
```

`queue` gains a lane column. `status` reports both governors and both modes.
`pause` and `resume` take an optional lane (`pause codex`), defaulting to both.

Everything above parses with zero model calls, which is the property that lets
the daemon keep answering while a window is exhausted. Free-form text still
falls through to a small parse call, and still degrades to `needs_parse` storage
if that call is itself rate limited.

### `usage`

Free by default. It composes three sources, none of which costs a request:

- the Claude governor's cached estimate, with the age of the last real poll
- codex percentages read from the session logs
- token volume and per-project breakdown from the log parsers

`usage poll` additionally runs `claude -p /usage`, honours the 60s floor, and
writes the result back as the governor's new baseline -- so paying for the truth
also improves every subsequent free estimate.

```
CLAUDE  session 41% (est) · week 62% (est) · last real poll 8m ago
CODEX   5h 12% · 7d 100% resets Aug 21
5h vol  claude 4.2M · codex 830K
by proj qpay 2.1M · poook 900K
```

### `sessions` and `repos`

`manage.py` moves into the package as `dispatch/sessions.py` and is imported,
not shelled out to. `sessions` renders the session dashboard; `sessions <proj>`
filters it. `repos` lists every folder under `~/Projects`, marking which are
dispatchable.

`manage launch` is deliberately not exposed. It spawns a GUI terminal running
interactive `claude`; triggered from Telegram that produces a session at a
prompt with nobody typing into it.

## Repo discovery

There is no hand-maintained alias map. Each tick scans `~/Projects/*/`:

- a folder containing `.git` is **dispatchable**
- a folder without one is **listed but rejected** on `run`, with the reason

The distinction is not cosmetic. The worker contract checkpoints to `tg/<id>`;
a non-git folder has nowhere to checkpoint, so a wind-down would discard work
instead of parking it. At the time of writing that is 23 dispatchable of 34.

`config.json` keeps a `repos` map only for overrides -- paths outside
`~/Projects`. It is empty by default.

The trust boundary is now "every git repo under `~/Projects`", which is wider
than the previous "aliases you typed". `repos` exists so that boundary is one
message away rather than a file you have to remember to read.

## Volume parsing

`usage_tg.py` moves into the package as `dispatch/volume.py`, refactored so its
parsers -- `claude_usage`, `codex_usage`, `active_sessions` -- are importable and
side-effect free, and so `plan_limits()` (the paid `/usage` call) is opt-in
rather than unconditional. Its module-level `NOW` constant becomes an injected
parameter, which is also what makes it testable. The standalone `--print` entry
point is retained and produces the same report as before.

## Supervision

The daemon runs in a detached tmux session named `dispatchd`. `dispatch up`
starts it if absent and reports if present; `dispatch up --if-dead` is the same
check with no output on the happy path. `dispatch down` kills the session;
`dispatch logs --daemon` tails its output.

Two cron entries guard it:

```
*/5 * * * * dispatch up --if-dead >/dev/null 2>&1
@reboot     dispatch up --if-dead >/dev/null 2>&1
```

This reinstates a cron line immediately after removing one, which deserves an
explanation rather than a shrug. The old cron guarded a bridge that was a child
of an interactive session and died whenever that session did -- a structural
fragility no watchdog could fix, only paper over. The new one guards a
standalone daemon whose only expected causes of death are a crash or a reboot.
The watchdog is a backstop here, not a workaround.

The 5-minute interval is chosen against the failure it covers: a crash is
invisible from Telegram, so the recovery window is the time you would otherwise
sit wondering why the bot went quiet.

## Teardown

Ordered, because one Telegram token admits exactly one consumer.

1. `dispatch setup --chat 7256243815`
2. remove the health cron line
3. kill the running bridge processes
4. `dispatch up`
5. verify by sending `status` from Telegram
6. install the two watchdog cron entries

**Step 1 now disables the plugin itself, and the separate manual step for it is
gone.** It was step 3, and leaving it there would have made this sequence abort
before reaching it: `setup` turns `telegram@claude-plugins-official` off (it
handles both the map and the list form of `enabledPlugins`, backing the file up
verbatim first) and **exits non-zero if it could not**, so under `set -e` the
script stops at step 1 -- ahead of a manual fallback written to run at step 3.

Dropped rather than moved ahead of step 1, deliberately: `disable_plugin` copies
the original bytes to `settings.json.bak` before editing and renames the new
content into place over `realpath`, so a symlinked `settings.json` stays a
symlink and a crash mid-write cannot truncate it. A hand edit gets none of that.
If step 1 does exit non-zero it has already said which file it could not change;
turn the plugin off by hand then, and re-run step 1 before continuing.

Deleted once step 5 passes:

```
~/.claude/scripts/telegram_health.sh
~/.claude/scripts/usage_tg.py
~/.claude/skills/manage/
~/.claude/projects/-home-navid-Projects/memory/telegram-channel-health.md
~/.claude/projects/-home-navid-Projects/memory/MEMORY.md  (its line only)
skills/dispatch/SKILL.md
```

Moved: `references/operations.md` -> `docs/operations.md`. It is the only record
of the governor math and the recovery table, and losing that to a tidy-up would
be a bad trade. It stops being a skill; it stays as documentation.

Kept: `skills/orchestrate` and `skills/taskforge`, with their `~/.claude/skills`
symlinks. Headless workers are Claude sessions and do load user skills, so
deleting a skill removes it from every worker too. That is the desired outcome
for `manage` and `dispatch` -- a worker has no business listing your sessions or
queueing more work -- and the wrong outcome for orchestrate and taskforge, which
a worker can legitimately use.

The `dispatch` CLI stays on `PATH` for laptop-side debugging. It is not an
interface in the sense this document uses the word; it is what you reach for
when the interface is down.

Finally, the whole tree is committed and pushed to
`github.com/navidtadjalli/agent-workflow-skills`.

## Consequences

**Every prompt becomes a headless worker.** There is no interactive
back-and-forth left. A task that needs a decision comes back `blocked` with a
note, having already spent a step. Acceptance criteria have to be in the message
rather than negotiated afterwards, and the `dispatch` skill's task-phrasing
guidance -- which is being deleted as a skill -- is now the user's job, not a
session's.

**The trust boundary widened.** Any git repo under `~/Projects` can be targeted
by an unattended agent running without permission prompts, from any message that
passes the chat allowlist. The allowlist is a single chat id and is the only
thing standing in front of that.

Because it is the only thing, it fails closed. An empty allowlist means nobody,
not everybody: `Chat.allowed` denies it, `Chat` refuses to be constructed
without one at all, and the daemon answers an empty allowlist by running with no
chat transport and a standing reason -- printed at startup and shown by
`dispatch status` -- rather than by serving one. That matters because the
allowlist can vanish on its own: `config.load()` falls back to `DEFAULTS`, whose
allowlist is empty, whenever `config.json` is missing or does not parse. A
truncated write used to turn authentication off while the bot went on answering
normally. Now a config that cannot be read says so on stderr, and the fallback
it lands on admits nobody.

The daemon still starts in that state, deliberately. It keeps draining the queue
for `dispatch add`, and `dispatch status` can say why chat is refused; a daemon
that refused to start would be relaunched by the cron watchdog every five
minutes, with the reason going down with each pane.

**A dead daemon is a lockout.** The cron watchdog covers crash and reboot. It
does not cover a bug that makes the daemon start and then fail to poll, or a
revoked token. Recovery from those requires physical access.

## Testing

Extends the existing suites; the injected-clock, injected-usage, stub-agent
approach is unchanged, and no test spends a real request.

Pure-function coverage in `tests/dispatch_test.py`:

- codex rate-limit extraction: window keying, null records, expired records,
  both slot orderings, empty session tree
- per-lane admission: each lane's gate in isolation, shared repo lock contention
  between lanes, concurrency ladder per lane
- `mode` migration: old string form expands to both lanes
- parser: both new run verbs, colon form, worktree suffix, missing-repo
  rejection, `pause <lane>`
- codex `parse_result` against a schema-conformant last-message file, a
  malformed one, and a missing one
- repo discovery: git vs non-git classification, override merge

Integration coverage in `tests/dispatch_integration.py`:

- a codex task and a claude task contending for one repo lock
- one lane freezing while the other keeps dispatching
- a codex task admitted from a stale-but-unexpired reading
- prompt-file round trip: enqueue writes it, the stub agent reads it on stdin

`dispatch/volume.py` gets its own tests once `NOW` is injectable, over a fixture
tree of session logs.

## Deviations from the 2026-08-18 design

- **systemd replaced by tmux plus a cron watchdog.** The original refused to
  install anything; this one is asked to be self-sustaining, and the user chose
  tmux over a unit file.
- **Setup no longer refuses while the chat plugin is enabled -- it disables it.**
  The original would not edit `settings.json` under any flag. That restraint
  assumed the plugin was a peer interface worth protecting. It is now the thing
  being replaced, and leaving it enabled guarantees the 409 the original was
  trying to avoid. The edit is confined to flipping one boolean.
- **Aliases are discovered, not configured.** The original treated the alias
  list as the trust boundary and kept it short by hand. That is no longer
  compatible with "send every prompt from Telegram", so the boundary moves to
  `~/Projects` plus a git check, and `repos` makes it inspectable.
- **The `dispatch` skill is deleted.** The original called the skill the way a
  session reads the system. With no interactive sessions, it only reached
  workers, which should not be driving the queue.
