# `isolation="worktree"` now creates a real worktree

Branch `fix-worktree-isolation`, base `cca996e`.

## The bug, restated

`scheduler.lock_name` keys an `isolation="worktree"` task on its **id**, so two
of them on one repository take different locks, both pass admission, and both
were launched with `cwd = self.repo_path(task["repo"])` — the same checkout.
Two unattended agents then ran `git checkout -B tg/<id>` and `git add -A && git
commit` in one working tree. Unused the feature was inert; used, it removed the
only guard.

## TDD evidence

The regression test was written first and run against BASE:

```
FAIL: test_two_worktree_tasks_never_share_a_checkout
AssertionError: '/tmp/tmp8iflu332/demo-repo' == '/tmp/tmp8iflu332/demo-repo'
```

It asserts on the working directories the steps are *actually launched in*, by
wrapping `daemon.run_step` and recording `cwd`.

After the fix, the whole new set was re-run against the old execution path
(`_start`'s worktree branch disabled, everything else in place). 12 of the 13
new integration tests fail without it:

```
ERROR: test_a_stale_registration_does_not_wedge_the_next_step
FAIL:  test_a_blocked_task_keeps_its_worktree_to_be_looked_at
FAIL:  test_a_branch_already_checked_out_blocks_instead_of_falling_back
FAIL:  test_a_finished_task_gives_its_worktree_back
FAIL:  test_a_foreign_directory_in_the_way_is_refused_not_clobbered
FAIL:  test_a_paused_task_keeps_the_worktree_it_will_resume_into
FAIL:  test_a_repo_with_no_commits_blocks_instead_of_running_in_the_parent
FAIL:  test_an_isolated_step_commits_to_its_branch_and_leaves_the_parent_alone
FAIL:  test_an_isolated_task_runs_beside_repo_lane_work_in_one_repo
FAIL:  test_cancelling_a_paused_task_releases_its_worktree
FAIL:  test_the_worktree_lives_where_no_repo_discovery_can_reach_it
FAIL:  test_two_worktree_tasks_never_share_a_checkout
FAILED (failures=11, errors=1)
```

and the daemon-level unit tests likewise:

```
FAIL: TestIsolationNeverFallsBack.test_a_creation_failure_blocks_the_task_and_says_why
FAIL: TestIsolationNeverFallsBack.test_metadata_held_elsewhere_defers_rather_than_blocking
FAIL: TestIsolationNeverFallsBack.test_the_step_runs_in_the_worktree_not_the_repo
```

`test_a_second_step_resumes_into_the_same_worktree` and
`test_a_codex_step_is_pointed_at_the_worktree_too` pass vacuously against the
old code (one checkout is trivially "the same" as itself); both carry an extra
assertion that the directory is not the parent repo, which is what makes them
mean something, and both were added after the fix rather than as red tests.

Final state — all three gates green:

```
bash tests/run.sh                          -> all tests passed, exit 0
python3 -W error tests/dispatch_test.py    -> 374 OK   (was 362)
python3 -W error tests/dispatch_integration.py -> 42 OK (was 27)
```

No test invokes `claude`, `codex`, or Telegram; the stub CLIs and `NullChat`
are unchanged in that respect. Real `git` is used throughout the integration
suite, against repositories built in `tempfile.TemporaryDirectory()`. Nothing
reads `~/Projects`, `~/.claude`, `~/.codex`, or the developer's home:
`DISPATCH_HOME`, `projects_root`, `DISPATCH_TOKEN_ENV`, `DISPATCH_TRANSCRIPTS`
and `DISPATCH_CODEX_SESSIONS` are all pointed into the temp tree, as before.

## Answers to the design questions

### Placement — `$DISPATCH_HOME/worktrees/<task-id>`

**Could a worktree at this location be discovered by `repos.discover` as a
dispatchable repo? No.** `discover` lists the direct children of
`projects_root` (default `~/Projects`); the default worktree root is
`~/.claude/dispatch/worktrees`, which is not under it, and the only other way
into `discover` is the explicit `repos` alias map, which nothing here writes.
`tests/dispatch_test.py::TestWorktreePlacement` asserts this, and asserts the
hazard is real by copying the same directory into the projects root and showing
that `discover` *does* then mark it `git: True`.

That hazard is the reason for the choice, and it is worse than "pollutes the
repo". A linked worktree's `.git` is a **file** holding a `gitdir:` pointer, and
`repos._entry` uses `os.path.exists`, which accepts a file — deliberately, per
its own comment. So a worktree parked among the checkouts would be listed by the
`repos` chat verb and dispatchable under its own alias, on a `repo-<name>` lock
that has nothing to do with `worktree-<id>`. A chat message could point a second
unattended agent straight into an isolated task's private tree. That is the
original bug with a different lock name.

Inside the parent repo was rejected for the ordinary reasons too: it shows up in
the parent's `git status`, it is inside the blast radius of the worker's own
`git add -A`, and a `.gitignore` entry is a promise every one of 23 repos would
have to keep.

`$DISPATCH_HOME` also gives tests and `dispatch down/up` a single root to
relocate, matching how every other piece of dispatch state is handled.

**One consequence I am not happy about, and one knob for it.** Per
`docs/operations.md`, `codex exec resume` accepts no sandbox flag, so from step
two onward a codex worker is confined by the path-keyed `trust_level` entries in
`~/.codex/config.toml`. On this machine `~/Projects` is trusted and everything
else is not, so a **multi-step codex task in a worktree under `~/.claude/` runs
read-only after its first step**. That is a real reduction in what the feature
can do, it is machine configuration rather than something this code can fix, and
it would be invisible if I did not say so. So there is now a `worktree_root`
config key (default: `<DISPATCH_HOME>/worktrees`), documented in
`docs/operations.md` alongside the three remedies: trust the worktree root in
`~/.codex/config.toml`, point `worktree_root` at a directory inside `~/Projects`
that is **not a direct child** of it (a direct child would be discovered), or
use the claude lane for isolated work. Claude workers are unaffected — their
confinement is the directory they are pointed at.

### Lifecycle

Created in `_start`, just before the step, and reused by every later step of the
same task. Released by a queue-driven sweep, `_reclaim_worktrees()`, run once at
the end of every tick.

| Task state | Worktree | Why |
|---|---|---|
| `done` | removed | Every commit is on `tg/<id>` in the parent. The tree holds nothing the branch does not |
| `cancelled` | removed | Same, and nobody is coming back to it |
| gone from the queue | removed | Orphan; `queue.json` was reset or rewritten |
| `paused` | **kept** | Mandatory: the post-reset step resumes into it |
| `queued` (after a `continue` step) | kept | The next step resumes into it |
| `running` | kept | Obviously |
| `blocked` | kept | Judgement call, taken toward keeping |
| `failed` | kept | Same |

The `blocked`/`failed` call: those two, plus `paused`, are exactly the three
states for which `_settle` writes `handoff.md`. The file exists to be read by a
person or by a fresh worker, and until now it could only point at the branch.
Whatever the failed step left **uncommitted** — the interesting part of a
`checkpoint failed` — exists nowhere else at all. Deleting it to save a
directory is the wrong trade for a system whose stated contract is "work is
parked, not lost". `handoff.md` now carries a `Working directory:` line so the
tree can actually be found (`worker.write_handoff` takes a `cwd`).

The cost is honest and documented: a task left `blocked` forever keeps its tree
forever. `dispatch cancel <id>` releases it, and because the sweep runs off the
queue rather than out of `_settle`, it also collects a task cancelled from the
CLI while it was parked and one abandoned by a daemon that died mid-step —
neither of which `_settle` ever sees.

`git worktree prune` runs inside the creation lock before every `add` and after
every `remove`. The `add`-side prune is load-bearing: a registration whose
directory has vanished (a wiped `DISPATCH_HOME`, a crash between `add` and the
first step) makes `git worktree add` refuse the very path it is being asked to
rebuild. `test_a_stale_registration_does_not_wedge_the_next_step` covers it.

The sweep calls `shutil.rmtree`, so it only ever touches entries matching
`^t-\d+$` (`worktrees.owned`). Anything else a person drops in that directory
stays put — `test_the_sweep_only_removes_what_it_created`.

### Creation is a repo mutation

`git worktree add` writes the parent's `.git/worktrees/` and creates a ref, so
two creations against one repository race. It is serialized on a **dedicated**
`worktree-add-<repo>` lock, taken with `state.try_lock` (non-blocking) and held
only across `prune` + `add`, never across the step.

It is deliberately **not** the `repo-<name>` lock, for three reasons:

1. **It would defeat the isolation.** A repo-lane worker holds `repo-<name>` for
   the whole of its step. Blocking on it would make isolated tasks queue behind
   precisely the work they were declared isolated from.
2. **It would be a second lock taken inside the first.** `_start` already holds
   `worktree-<id>` at that point. Adding a blocking acquisition of a lock held
   for step-length by another worker, on a code path that then runs a
   subprocess, is how a tick stalls indefinitely.
3. **It would hold a lock across a subprocess.** Taking it non-blocking and
   deferring is the only shape that does not.

`worktree-add-<repo>` cannot collide with a scheduler lock: task ids are always
`t-NNNN`, so `worktree-<id>` never matches `worktree-add-*`
(`test_the_creation_lock_cannot_collide_with_a_task_lock`).

If the add-lock is held, `ensure` returns `{"retry": True}` and `_start` returns
`False` — the task simply is not startable this tick, exactly as a busy repo
lock behaves. In practice only the daemon creates worktrees and `_dispatch` is
single-threaded, so this is defence rather than a live path; it is tested
directly (`test_a_busy_creation_lock_defers_instead_of_failing`, which also
asserts **no git ran at all** while the lock was held).

No queue or state document is open across any git call. `mutate_queue` is
entered only after `ensure` has returned, and `notify` (which reaches the
network) is called after the task lock has been released.

### Failure

`ensure` returns three shapes and `_start` handles each: `{"path": ...}` run
there, `{"retry": True}` don't start this tick, `{"error": ...}` **block**.

Blocking sets `state = "blocked"`, puts `no worktree · <git's own words>` in
`last_error`, and sends a chat notice — `dispatch queue` and the notice both
carry the reason. `blocked` rather than `failed` because nothing was lost, no
step ran, and every one of these is a repository condition a person fixes and
then `dispatch retry`s.

Covered, each with an assertion that **no step was launched anywhere**:

| Condition | Reason the user sees |
|---|---|
| Repo with no commits | `no worktree · fatal: invalid reference: HEAD` |
| `tg/<id>` already checked out (parent or another worktree) | `no worktree · fatal: 'tg/t-0001' is already used by worktree at ...` |
| A foreign directory at the worktree path | `no worktree · ... is not a git worktree · remove it by hand` |
| A worktree there belonging to a different repository | `no worktree · ... belongs to another repository` |
| `git` not on PATH, unwritable root, full disk | `no worktree · could not create ...: <errno>` |
| Any other `git worktree add` failure | git's first `fatal:`/`error:` line, capped at 200 chars |

`_inspect` and `ensure` both catch `OSError`, because an exception out of
`_start` would take the whole tick down and do it again on the next one.

A directory in the way is **refused, not deleted** — the daemon does not remove
a directory it did not create. An *empty* one is removed, since a half-finished
`add` leaves one behind and it carries nothing.

**Falling back to the parent checkout is not implemented anywhere**, and the
`_start` comment says so in as many words. Four tests assert the negative:
`test_a_repo_with_no_commits_blocks_instead_of_running_in_the_parent`,
`test_a_branch_already_checked_out_blocks_instead_of_falling_back`,
`test_a_foreign_directory_in_the_way_is_refused_not_clobbered`, and
`TestIsolationNeverFallsBack`.

New recovery rows for all of these are in `docs/operations.md`.

### Interaction with `worker.checkpoint`

`checkpoint` runs `git rev-parse --abbrev-ref HEAD` and skips `git checkout -B
<branch>` when HEAD already *is* the branch. `git worktree add` creates the
worktree **on** `tg/<id>`, so HEAD is `tg/<id>` from the first byte and the
checkout is skipped. That is necessary, not merely tidy: git refuses to check
out a branch that is live in another worktree, so a `checkout -B` here would
fail outright if the branch were also checked out elsewhere.

Consequences, all asserted in
`test_an_isolated_step_commits_to_its_branch_and_leaves_the_parent_alone`:

- the parent's `rev-parse --abbrev-ref HEAD` is unchanged after the step (the
  shared-checkout path dragged the parent onto `tg/<id>`);
- the parent's `git status --porcelain` is empty and `worked.txt` never appears
  there;
- `git log tg/t-0001` in the **parent** shows `t-0001 step 1`, and
  `git show tg/t-0001:worked.txt` has the content — the branch is where every
  other surface already looks, and it stayed there.

The integration stubs were changed to write into their own **cwd** rather than
into a fixed `STUB_REPO`; without that, "where did the step actually work" was
unobservable and the fixture would have quietly written into the parent no
matter which directory the step ran in.

`git worktree remove --force` is used on release, so gitignored build artifacts
in a `done` worktree are discarded along with it. Everything tracked is already
committed by that point.

### `scheduler.lock_name` — confirmed correct, unchanged

`worktree-<task-id>` is right now that the worktrees are real. Two isolated
tasks in one repo genuinely cannot touch each other's files, and keying on the
id also prevents two steps of the *same* task overlapping — which matters more
than before, since they now share a directory across steps. Two tests pin it:
`test_two_isolated_tasks_in_one_repo_take_different_locks` and the integration
test that shows an isolated task starting in the same tick as repo-lane work in
the same repository. The one new coupling between an isolated task and its
repository — the `.git` mutation — is carried by `worktree-add-<repo>`, which
is held for milliseconds and is invisible to admission.

## Files changed

| File | Change |
|---|---|
| `skills/dispatch/dispatch/worktrees.py` | **new**. `root` / `path` / `owned` / `add_lock_name` / `ensure` / `discard`. Stdlib only; all git through `worker.git` |
| `skills/dispatch/dispatch/daemon.py` | `_start` creates and uses the worktree, or blocks; `_block` helper (also now used by the pre-existing repo-missing path); `_reclaim_worktrees` + `_note`; `RECLAIMABLE`; `repo_path` on the running entry; handoff gets `cwd` |
| `skills/dispatch/dispatch/config.py` | `worktree_root` default (`None` → `<DISPATCH_HOME>/worktrees`) |
| `skills/dispatch/dispatch/worker.py` | `write_handoff(..., cwd=None)` records the working directory |
| `docs/operations.md` | state-tree entries, `worktree_root` config key, new **Isolated worktrees** section, four recovery rows |
| `docs/superpowers/specs/2026-08-20-telegram-dispatch-design.md` | **Isolation** section (the bug, placement, lifecycle, serialization, failure, checkpointing); testing list extended |
| `tests/dispatch_integration.py` | 15 new tests; `_enqueue` takes `isolation`; `_watch` helper; stubs write into their own cwd |
| `tests/dispatch_test.py` | 12 new tests across `TestScheduler`, `TestWorktreePlacement`, `TestWorktreeGitFailures`, `TestIsolationNeverFallsBack` |

No change to the task record shape, so **existing `queue.json` entries load
unchanged**: `isolation` is read with `.get`, and `branch` falls back to
`tg/<id>` if a record somehow lacks it. `docs/superpowers/specs/2026-08-18-…`
was left alone — it is the historical design and its two lines on isolation are
what the code now actually does.

## Concerns

1. **Codex continuations outside a trusted path.** *Superseded — see the
   addendum below.* The effect was real; my account of the cause was wrong,
   the problem turned out to be much wider than worktrees, and it is now
   fixed in code rather than worked around in documentation.

2. **`git worktree add` runs on the tick thread.** For a large repository the
   checkout could take seconds, during which chat is not answered. The tick
   already shells out to `claude /usage` and to a 120-second free-form parse, so
   this is well inside the existing envelope, but it is new latency on a path
   that had none.

3. **An orphaned worktree leaves a stale registration.** If a task's record is
   gone from `queue.json` entirely, the sweep has no repo to run `git worktree
   remove` against and only `rmtree`s the directory. The registration in the
   parent's `.git/worktrees/` survives until the next `ensure` for that repo
   prunes it. Harmless, and self-healing, but not immediate.

4. **`blocked`/`failed` trees accumulate.** By design, argued above. Nothing
   reaps them but `dispatch cancel`. If that turns out to hurt, the honest fix
   is an age-based sweep with a notice, not a silent delete.

5. **Multi-daemon behaviour is defended but untested against a real second
   process.** The `retry` path is unit-tested with the lock genuinely held, but
   only one daemon is ever supposed to run, so the two-process case has not been
   exercised end to end.

6. **`git worktree add` concurrent with a repo-lane `git commit` in the same
   repository is not serialized**, and I concluded it does not need to be: `add`
   creates a new `.git/worktrees/<name>` directory and one new ref, while the
   commit takes `.git/index.lock` and updates a different ref. They share no
   lock file. I reasoned this from the ref/lock layout rather than measuring it
   under contention, which is the weakest link in this change.


---

# Addendum: `codex_sandbox` now applies to every step

Third commit on the same branch. Prompted by the coordinator, who measured that
`codex debug prompt-input` renders `read-only` in a worktree, in this
repository, and in `/tmp` alike, and concluded that location has nothing to do
with it.

## What I measured, and where it differs from both of us

`codex debug prompt-input` is a local renderer — no session, no request, no
quota — and the `<permissions instructions>` block it emits names the sandbox
verbatim: `` `sandbox_mode` is `read-only` ``. Against the live
`~/.codex/config.toml` on this machine, codex-cli 0.148.0:

```
~/Projects                            -> workspace-write  (`projects` entry; not a git repo)
~/Projects/qpay-backend               -> workspace-write  (its own `projects` entry)
~/Projects/qpay-backend/<subdir>      -> workspace-write  (same git repo root)
~/Projects/.probe-nongit              -> workspace-write  (no git root; trusted ancestor)
~/Projects/agent-workflow-skills      -> read-only        (git repo, no entry of its own)
~/Projects/agent-workflow-skills/docs -> read-only        (same git repo root)
/tmp/untrusted-probe-dir              -> read-only
```

So **location does matter** — the coordinator's three probes happened to be
three untrusted locations. The rule the data fits: **trust resolves on the git
repository root when there is one, and on the nearest trusted ancestor of `cwd`
when there is not.** `~/Projects` carries `trust_level = "trusted"`, but
`~/Projects/agent-workflow-skills` is its own repo root with no entry and does
not inherit; `~/Projects/qpay-backend` has an entry of its own, so its
subdirectories do.

My original concern #1 was wrong in the same direction. I said "`~/Projects` is
trusted, so a worktree under `~/.claude/` is not". The truth is that **a linked
worktree is its own git repo root**, so no ancestor's trust ever reaches it,
wherever it is parked. The remedy I documented — "set `worktree_root` to a
directory inside `~/Projects`" — **does not work**, and that line is now removed
from `docs/operations.md` and the design spec.

The coordinator's central claim survives all of this and is the important part:
**every multi-step codex task in a repository without its own `projects` entry
ran read-only from step two onward** — worktree or not — because `codex exec
resume` carries no sandbox flag at all. Step one wrote; every step after it
could read and think and change nothing, and reported `complete`.
`~/.codex/config.toml` has entries for 13 repositories; this one is not among
them.

## The mapping I chose

`--approve-for-me`'s own help settles the sandbox half: *"Route approval
requests through automatic review using the workspace-write sandbox."*

| `codex_sandbox` | first step (unchanged) | every later step (new) |
|---|---|---|
| `read-only` | `-s read-only` | `-c sandbox_mode="read-only"` |
| `approve-for-me` (default) | `--approve-for-me` | `-c sandbox_mode="workspace-write"` |
| `workspace-write` | `-s workspace-write` | `-c sandbox_mode="workspace-write"` |
| `danger-full-access` | `-s danger-full-access` | `-c sandbox_mode="danger-full-access"` |
| `bypass` | `--dangerously-bypass-approvals-and-sandbox` | the same flag |
| unknown / non-string | falls back to `approve-for-me`, warns | falls back with it |

Every non-bypass mode also carries `-c approval_policy="never"`.

Verified end to end in an untrusted directory, through the real
`worker.build_command` rather than by hand:

```
no override (what a resume used to get)  sandbox=read-only          approvals=escalation-invited
read-only            -> sandbox=read-only           approvals=never   OK
approve-for-me       -> sandbox=workspace-write     approvals=never   OK
workspace-write      -> sandbox=workspace-write     approvals=never   OK
danger-full-access   -> sandbox=danger-full-access  approvals=never   OK
```

and every argv shape parses on both paths (real argv + `--help`, stdin closed):

```
read-only first rc=0 | read-only resume rc=0 | approve-for-me first rc=0
approve-for-me resume rc=0 | workspace-write first rc=0 | workspace-write resume rc=0
danger-full-access first rc=0 | danger-full-access resume rc=0
bypass first rc=0 | bypass resume rc=0
```

**Does the resumed sandbox now match the first step's, per mode?** Yes for all
five on the sandbox axis. `read-only`, `workspace-write`, `danger-full-access`
and `bypass` match exactly; `approve-for-me` matches the sandbox that flag
itself selects. On the *approvals* axis `bypass` matches and the other four do
not — see below.

## `approval_policy`

**It is a real key, not a guess.** A bad value is a loud deserialize error
naming the variants, which is how I enumerated them without documentation:

```
$ codex debug prompt-input x -c approval_policy="nonsense-value"
Error: unknown variant `nonsense-value`, expected one of
       `untrusted`, `on-failure`, `on-request`, `granular`, `never`
```

Only `approval_policy` responds; `ask_for_approval`, `approvals` and
`approval_mode` are silently ignored — which also establishes that an
unrecognized `-c` key is a no-op rather than an error.

What each renders, with `sandbox_mode="workspace-write"`:

- `never` (505 chars) — *"Approval policy is currently never. Do not provide
  the `sandbox_permissions` for any reason, commands will be rejected."* No
  escalation section at all.
- `untrusted` (632) — *"The harness will escalate most commands for user
  approval, apart from a limited allowlist of safe read commands."*
- `on-request` / `on-failure` (4824, byte-identical) — the full "Escalation
  Requests" instructions telling the model how and when to ask.
- unset, i.e. the old behaviour — the same 4824-char escalation-invited shape.
- `granular` — not a string variant at all: `invalid type: unit variant,
  expected newtype variant`, then `missing field sandbox_approval`, then
  `missing field rules`. `GranularApprovalConfig` wants a boolean
  `sandbox_approval` and a `rules` list. That is execpolicy rule matching, not
  automatic review.

**Automatic review could not be reproduced on the resume path.** No key turns
it on; `--approve-for-me` is the only thing that does, and `codex exec resume`
rejects it. So I picked, and here is the defence.

Three losses to choose between:

- **unset** — the model is invited to request escalation. On a resumed step
  there is no auto-reviewer and no human, so the request cannot be answered.
  Cost: the step hangs to `step_timeout` (3600s), gets SIGTERM then SIGKILL,
  and salvages whatever it had. The "succeeds while doing nothing" shape again,
  in a slower costume.
- **bypass** — no sandbox at all, on every continuation of every task. Cost:
  the confinement.
- **`never`** — the model is told up front that escalation will be refused, so
  it does not ask. Cost: an escalation automatic review *would* have granted,
  such as a network install, is refused instead.

I chose `never`. It is the only one whose cost is bounded and visible: the
worker either does the work inside its workspace — which is everything a
checkpointing worker needs, since the contract is commit to `tg/<id>` in `cwd`
— or reports `blocked`, a first-class outcome that reaches chat. The other two
cost the step or the sandbox, silently.

To be plain: this is the one place a resumed step is **not** equivalent to the
first. Under `--approve-for-me`, step one can escalate through automatic
review; step two onward cannot escalate at all.

## `--strict-config`

**Not used**, and this is a judgement rather than a measurement. It "errors out
when config.toml contains fields that are not recognized by this version of
Codex" — it validates the *user's own file*. On this path that would let one
stale key in `~/.codex/config.toml` fail every continuation of every task while
first steps, which do not carry the flag, kept working; step one and step two
would disagree about config strictness. It also would not catch a typo in the
keys emitted here: an unrecognized `-c` key is silently ignored, while a bad
value already fails loudly on its own. Whether it validates `-c` overrides at
all could not be measured without starting a real session, so the argument
rests on the documented behaviour plus the failure mode it would introduce.

## Tests

5 new unit tests in `TestBackends`, 3 new integration tests. Red without the
fix (resume branch reverted to `return []`):

```
FAIL:  test_the_configured_sandbox_reaches_every_step_not_just_the_first
FAIL:  test_a_resumed_step_is_told_not_to_ask_for_an_escalation
FAIL:  test_an_unknown_mode_resumes_into_the_default_sandbox_too
ERROR: test_the_configured_sandbox_survives_into_a_resumed_codex_step
FAIL:  test_a_resumed_step_in_a_worktree_is_confined_the_same_way
```

The integration tests assert on the **real launch**: the codex stub records its
own `sys.argv[1:]`, so flags chosen deep inside `build_command` are checked as
they actually arrive. No real codex, no session, no request — the weekly window
is untouched. The pre-existing `RESUME_ACCEPTS` guard still holds: `-c` is on
the accepted list and the values are not option-shaped.

Final: `bash tests/run.sh` exit 0; `dispatch_test.py` **379 OK**;
`dispatch_integration.py` **45 OK**; both under `-W error`.

## Files changed in this addendum

- `skills/dispatch/dispatch/backends/codex.py` — `RESUME_SANDBOX`,
  `RESUME_APPROVAL`, the resume branch of `sandbox_args`, and a module
  docstring rewritten around the measurements
- `docs/operations.md` — "**`read-only` and `workspace-write` are
  first-step-only settings**" replaced by "**Codex confinement past the first
  step**" with the per-mode table; the wrong worktree remedy removed; the
  recovery row replaced
- `docs/superpowers/specs/2026-08-20-telegram-dispatch-design.md` — a **Codex
  confinement** section; the worktree bullet corrected
- `tests/dispatch_test.py`, `tests/dispatch_integration.py` — the tests above

## Remaining concerns

1. **The approvals axis still does not match**, as argued above. A codex task
   that genuinely needs to escalate mid-run — a dependency install on step three
   — is now refused rather than auto-approved, and should report `blocked`.
   Worth watching for `blocked` tasks whose reason is an install.
2. **`never` is inferred safe from the rendered prompt, not from a live run.**
   The text says such commands "will be rejected", and I read that as a
   rejection rather than a hang. Not verified against a real codex; the weekly
   window is at 100%.
3. **`approve-for-me` -> `workspace-write` rests on the flag's help string.** A
   strong source, but if the flag also widens `writable_roots` beyond `cwd`, a
   resumed step is slightly more confined than its first. Measured writable
   roots under `sandbox_mode=workspace-write` are `/tmp` and `cwd`.
4. **`codex debug prompt-input` is assumed to share the config loader with
   `codex exec`.** Same binary, same `-c` parser, same error text — but a
   different subcommand, and `exec` may apply non-interactive defaults `debug`
   does not. In particular I could not observe what approval policy `codex exec`
   itself defaults to; if it already forces `never`, this change is a no-op on
   that axis and a pure win on the sandbox axis.
5. **The trust rule is inferred from seven data points**, not from source. It
   fits all seven, and the practical conclusion — a worktree is its own repo
   root and never inherits trust — is one the fix no longer depends on, since
   the override now applies unconditionally.
