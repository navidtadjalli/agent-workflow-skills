"""Real isolation for the tasks that asked for it.

``isolation="worktree"`` is a promise that a task does not contend for its
repository: :func:`scheduler.lock_name` hands such a task a lock of its own
instead of the shared ``repo-<name>`` one, so two of them are admitted in the
same tick. That is only safe if they genuinely run in different directories.
This module is what makes them do so. Nothing here ever falls back to the
parent checkout -- an isolated task that cannot get a worktree is blocked with
a reason, because running it in the shared tree is the failure the lock name
was already claiming could not happen.

**Where.** ``$DISPATCH_HOME/worktrees/<task-id>`` by default: beside the task's
other artifacts, and outside ``projects_root``. Inside the parent repository
would pollute it, and worse -- ``repos.discover`` accepts a ``.git`` *file* as
evidence of a repository, and a linked worktree has exactly that -- a worktree
parked among the checkouts would be listed as a dispatchable repo in its own
right. A chat message could then queue ordinary repo-lane work directly into
another task's private tree, under a different lock, which is the original bug
wearing a different hat. The default location cannot be reached that way.
``worktree_root`` moves it for the one reason that argues for moving it; see
``docs/operations.md`` on codex trust levels.

**Lifecycle.** Created as a step is about to start, and reused by every later
step of the same task: a task that pauses at a wind-down resumes into the same
tree, so the tree has to still be there. Removed once the task can never take
another step -- ``done``, ``cancelled``, or gone from the queue. Kept for
``paused`` (it is resumed into) and kept for ``blocked`` and ``failed`` too:
those are the states someone reads ``handoff.md`` about, and whatever the last
step left uncommitted exists nowhere else. The cost is a directory that outlives
the task until it is cancelled; the alternative is deleting the only copy of
work that never reached a commit.

**Serialization.** ``git worktree add`` writes into the parent repository's
``.git/worktrees/`` and creates a ref, so two creations against one repository
race. They are serialized on ``worktree-add-<repo>``, taken *non-blocking* and
held only across the git call itself. Deliberately not the ``repo-<name>``
lock: a repo-lane worker holds that one for a whole step, so waiting on it would
make isolated tasks queue behind precisely the work they were declared isolated
from, and it would be taken while already holding ``worktree-<id>`` -- a second
lock inside the first, on a path that also runs a subprocess. No queue or state
document is open while any of this runs.
"""
import contextlib
import os
import re
import shutil

from . import config as config_mod
from . import state
from .worker import git


def root(config=None):
    """Directory holding every task's worktree."""
    config = config_mod.load() if config is None else config
    override = config.get("worktree_root")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return config_mod.path("worktrees")


def path(task_id, config=None):
    """Where ``task_id``'s worktree lives, whether or not it exists yet."""
    return os.path.join(root(config), task_id)


# What the sweep is allowed to delete. Task ids are `t-NNNN` and nothing else
# is this module's to remove: anything a person drops in the worktree root is
# left exactly where they put it, because the alternative is `rmtree` on a
# directory nobody here created.
TASK_ID = re.compile(r"^t-\d+$")


def owned(name):
    """Whether ``name`` in the worktree root is one of ours to reclaim."""
    return bool(TASK_ID.match(name))


def add_lock_name(repo):
    """The short-lived lock guarding one repository's worktree metadata.

    A different namespace from the ``worktree-<task-id>`` lock the scheduler
    hands out: task ids are always ``t-NNNN``, so no repository name can
    produce a collision with one.
    """
    return "worktree-add-%s" % repo


def _result(target=None, error=None, retry=False):
    return {"path": target, "error": error, "retry": retry}


def _detail(completed):
    """One readable line out of a failed git invocation."""
    for stream in ((completed.stderr or ""), (completed.stdout or "")):
        lines = [line.strip() for line in stream.splitlines() if line.strip()]
        for line in lines:
            if line.startswith(("fatal:", "error:", "warning:")):
                return line[:200]
        if lines:
            return lines[-1][:200]
    return "git exited %s" % completed.returncode


def _absolute(value, base):
    """``rev-parse --git-common-dir`` answers relative to its own cwd."""
    return os.path.realpath(value if os.path.isabs(value)
                            else os.path.join(base, value))


def _branch_exists(repo_path, branch, runner):
    return git(["rev-parse", "--verify", "--quiet", "refs/heads/%s" % branch],
               repo_path, runner).returncode == 0


def _inspect(target, repo_path, runner):
    """Judge whatever is already sitting where the worktree goes.

    Three answers: reuse it, refuse with a reason, or nothing is there and it
    can be created. A directory that is not this repository's worktree is
    refused rather than deleted -- it is not this code's to throw away.
    """
    if not os.path.exists(target):
        return _result()
    if not os.path.isdir(target):
        return _result(error="%s exists and is not a directory" % target)
    try:
        empty = not os.listdir(target)
    except OSError as exc:
        return _result(error="cannot read %s: %s" % (target, exc))
    if empty:
        # Half-finished creations leave one behind; an empty directory carries
        # nothing worth refusing over.
        with contextlib.suppress(OSError):
            os.rmdir(target)
        return _result()

    info = git(["rev-parse", "--show-toplevel", "--git-common-dir"], target, runner)
    lines = [line.strip() for line in (info.stdout or "").splitlines() if line.strip()]
    if info.returncode != 0 or len(lines) < 2:
        return _result(error="%s is not a git worktree · remove it by hand" % target)
    if os.path.realpath(lines[0]) != os.path.realpath(target):
        return _result(error="%s sits inside another checkout (%s)" % (target, lines[0]))
    parent = git(["rev-parse", "--git-common-dir"], repo_path, runner)
    if parent.returncode != 0 or not (parent.stdout or "").strip():
        return _result(error=_detail(parent))
    if _absolute(lines[1], target) != _absolute(parent.stdout.strip(), repo_path):
        return _result(error="%s belongs to another repository" % target)
    return _result(target=target)


def ensure(task, repo_path, config=None, runner=None):
    """The directory this task's step must run in.

    Returns one of three shapes. ``{"path": <dir>}`` -- run there.
    ``{"error": <reason>}`` -- the task must be blocked with that reason, and
    no step may start. ``{"retry": True}`` -- another process holds this
    repository's worktree metadata right now, so the task simply does not start
    this tick, exactly as a busy repo lock behaves.
    """
    target = path(task["id"], config)
    try:
        existing = _inspect(target, repo_path, runner)
    except OSError as exc:
        # No git on PATH, an unreadable path. A raise here would take the whole
        # tick down and do it again on the next one.
        return _result(error="could not inspect %s: %s" % (target, exc))
    if existing["path"] or existing["error"]:
        return existing

    branch = task.get("branch") or "tg/%s" % task["id"]
    handle = state.try_lock(add_lock_name(task["repo"]))
    if handle is None:
        return _result(retry=True)
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        # Registrations whose directory is gone -- a wiped home, a crash
        # between `add` and the first step -- make `add` refuse this very path.
        git(["worktree", "prune"], repo_path, runner)
        if _branch_exists(repo_path, branch, runner):
            args = ["worktree", "add", "--", target, branch]
        else:
            args = ["worktree", "add", "-b", branch, "--", target, "HEAD"]
        added = git(args, repo_path, runner)
    except OSError as exc:
        # No git on PATH, an unwritable root, a full disk.
        return _result(error="could not create %s: %s" % (target, exc))
    finally:
        state.release(handle)

    if added.returncode != 0:
        # `add` can leave the directory behind on its way out; a retry has to
        # start from nothing rather than from a shell of a worktree.
        discard(task["id"], repo=task["repo"], repo_path=repo_path,
                config=config, runner=runner)
        return _result(error=_detail(added))
    return _result(target=target)


def discard(task_id, repo=None, repo_path=None, config=None, runner=None):
    """Remove a task's worktree and its registration. Best effort.

    Failure here is not a failure of the task: every commit is on ``tg/<id>``
    in the parent repository, and a leftover directory costs disk, not work.
    The branch is deliberately left alone -- it is the whole point of the task.
    """
    target = path(task_id, config)
    known_repo = repo_path and os.path.isdir(repo_path)
    if not os.path.exists(target) and not known_repo:
        return {"ok": True, "error": None}
    handle = state.try_lock(add_lock_name(repo or task_id))
    if handle is None:
        return {"ok": False, "error": "worktree metadata for %s is busy"
                                      % (repo or task_id)}
    try:
        if known_repo:
            git(["worktree", "remove", "--force", "--", target], repo_path, runner)
        if os.path.exists(target):
            shutil.rmtree(target, ignore_errors=True)
        if known_repo:
            git(["worktree", "prune"], repo_path, runner)
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        state.release(handle)
    if os.path.exists(target):
        return {"ok": False, "error": "could not remove %s" % target}
    return {"ok": True, "error": None}
