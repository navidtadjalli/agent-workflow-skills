"""Durable JSON documents guarded by flock.

Two documents matter: ``queue.json`` (tasks) and ``state.json`` (governor
snapshot, chat offset, mode, armed timer). Both are written by
write-temp-then-rename so a crash mid-write cannot truncate them, and both are
mutated only inside ``locked()`` so the daemon and a CLI invocation cannot
interleave a read-modify-write.
"""
import contextlib
import errno
import fcntl
import json
import os
import tempfile

from . import config
from . import lanes

QUEUE_EMPTY = {"next_id": 1, "tasks": []}
STATE_EMPTY = {
    "mode": {"claude": "running", "codex": "running"},
    "chat_offset": 0,
    "governor": {},
    "armed_resume_at": {"claude": None, "codex": None},
    "repo_cost_pct": {},
    # Chat transport health. Never the token itself -- see ``chat.Chat._redact``.
    "chat_last_error": None,
    "chat_failures": 0,
    "chat_error_at": None,
    # Per lane: why the last dispatch pass started nothing.
    "hold_reason": {},
}


@contextlib.contextmanager
def locked(name):
    """Hold an exclusive advisory lock named ``name`` for the block."""
    config.ensure_dirs()
    lock_file = config.lock_path(name)
    os.makedirs(os.path.dirname(lock_file), exist_ok=True)
    handle = open(lock_file, "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield handle
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def try_lock(name):
    """Take ``name`` without blocking. Returns a handle, or None if held.

    The caller must keep the handle alive for as long as it holds the lock and
    call :func:`release` to drop it. Used for per-repo serialization, where a
    busy repo means "not admissible now", never "wait here".
    """
    config.ensure_dirs()
    lock_file = config.lock_path(name)
    os.makedirs(os.path.dirname(lock_file), exist_ok=True)
    handle = open(lock_file, "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            return None
        raise
    return handle


def release(handle):
    if handle is None:
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def read(path, empty):
    try:
        with open(path) as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return json.loads(json.dumps(empty))
    except (OSError, ValueError):
        return json.loads(json.dumps(empty))
    if not isinstance(data, dict):
        return json.loads(json.dumps(empty))
    merged = json.loads(json.dumps(empty))
    merged.update(data)
    return merged


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def read_queue():
    return read(config.queue_path(), QUEUE_EMPTY)


def read_state():
    doc = read(config.state_path(), STATE_EMPTY)
    doc["mode"] = lanes.normalize_mode(doc.get("mode"))
    doc["armed_resume_at"] = lanes.normalize_armed(doc.get("armed_resume_at"))
    return doc


@contextlib.contextmanager
def mutate_queue():
    with locked("queue"):
        data = read_queue()
        yield data
        write(config.queue_path(), data)


@contextlib.contextmanager
def mutate_state():
    with locked("state"):
        data = read_state()
        yield data
        write(config.state_path(), data)


def new_task(queue, repo, prompt, priority=5, deps=None, isolation="repo", branch=None,
             agent="claude"):
    """Append a task to ``queue`` and return the record."""
    task_id = "t-%04d" % queue["next_id"]
    queue["next_id"] += 1
    task = {
        "id": task_id,
        "repo": repo,
        "prompt": prompt,
        "agent": agent if agent in lanes.ALL else lanes.CLAUDE,
        "state": "queued",
        "priority": priority,
        "deps": list(deps or []),
        "isolation": isolation,
        "branch": branch or ("tg/%s" % task_id),
        "session_id": None,
        "steps_done": 0,
        "est_cost_pct": None,
        "last_error": None,
    }
    queue["tasks"].append(task)
    return task


def find(queue, task_id):
    for task in queue["tasks"]:
        if task["id"] == task_id:
            return task
    return None
