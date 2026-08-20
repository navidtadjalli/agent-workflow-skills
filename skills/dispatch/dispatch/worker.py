"""Worker supervision: run one step of one task, then decide what happens next.

A "step" is one headless agent invocation. Steps are deliberately small: the
agent is told to do one coherent chunk and stop, so that the wind-down path
always has a clean seam to stop at, and so a step that dies costs little.

Every step ends by asking the task's backend to parse its self-reported
status. That status -- not an exit code, not a heuristic -- decides whether
the task is complete, wants another step, or is blocked.
"""
import os
import signal
import subprocess

from . import backends, config as config_mod, lanes, usage


def build_prompt(task):
    """The full text the agent receives: the request, then the house rules."""
    backend = backends.get(lanes.of(task))
    return "%s\n\n%s" % (task["prompt"].strip(), backend.house_rules(task))


def write_prompt(task, task_dir):
    """Persist the prompt beside the task's other artifacts, return its path.

    The prompt is a file rather than an argv element for three reasons, in
    order of how likely each is to bite: chat text is arbitrary and quoting it
    is a correctness problem; a long prompt can exceed ARG_MAX; and the exact
    bytes the agent saw belong next to steps.jsonl when a step has to be
    diagnosed later.
    """
    os.makedirs(task_dir, exist_ok=True)
    path = os.path.join(task_dir, "prompt.txt")
    with open(path, "w") as handle:
        handle.write(build_prompt(task))
    return path


def build_command(task, prompt_path, cwd, task_dir, unsafe=True):
    return backends.get(lanes.of(task)).build_command(
        task, prompt_path, cwd, task_dir, unsafe=unsafe)


def parse_status(output, task_dir, task=None):
    return backends.get(lanes.of(task or {})).parse_result(output, task_dir)


def next_state(status, mode):
    """Task state after a step, given its status and the current mode.

    A step that finished while winding down is checkpointed and paused rather
    than immediately requeued -- the queue is closed, and pausing keeps the
    session id and handoff for the post-reset resume.
    """
    if status == "complete":
        return "done"
    if status == "blocked":
        return "blocked"
    if status is None:
        return "failed"
    return "paused" if mode != "running" else "queued"


def run_step(task, cwd, config, env=None, popen=None, sleeper=None, task_dir=None):
    """Execute one step under a wall-clock cap. Returns a result dict.

    ``popen`` and ``sleeper`` are injected in tests. The termination path is
    SIGTERM, a grace period, then SIGKILL: a checkpointing agent deserves the
    chance to finish its commit, but not indefinitely.
    """
    popen = popen or subprocess.Popen
    task_dir = task_dir or config_mod.task_dir(task["id"])
    prompt_path = write_prompt(task, task_dir)
    argv = build_command(task, prompt_path, cwd, task_dir)

    with open(prompt_path) as prompt_handle:
        process = popen(argv, cwd=cwd, env=env or os.environ.copy(),
                        stdin=prompt_handle,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        timed_out = False
        try:
            output, _ = process.communicate(timeout=config["step_timeout"])
        except subprocess.TimeoutExpired:
            timed_out = True
            output = _terminate(process, config, sleeper)

    result = parse_status(output, task_dir, task)
    result["timed_out"] = timed_out
    result["returncode"] = process.returncode
    result["output"] = output or ""
    result["limit_reset_at"] = usage.parse_limit_error(output)
    if result["limit_reset_at"]:
        result["status"] = None
    return result


def _terminate(process, config, sleeper=None):
    """SIGTERM, wait out the grace period, then SIGKILL. Returns any output."""
    import time as _time

    sleeper = sleeper or _time.sleep
    try:
        process.send_signal(signal.SIGTERM)
    except OSError:
        pass
    waited = 0.0
    while waited < config["term_grace"]:
        if process.poll() is not None:
            break
        sleeper(0.5)
        waited += 0.5
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
    try:
        output, _ = process.communicate(timeout=10)
    except Exception:  # noqa: BLE001 - the step is already over; salvage what we can
        output = ""
    return output


def git(args, cwd, runner=None):
    runner = runner or subprocess.run
    return runner(["git"] + list(args), cwd=cwd, capture_output=True, text=True)


def checkpoint(task, cwd, message, runner=None):
    """Commit whatever the step left behind onto the task branch.

    Called both at a clean step boundary and as salvage after a kill, so it
    must be safe on an already-clean tree.
    """
    branch = task["branch"]
    current = git(["rev-parse", "--abbrev-ref", "HEAD"], cwd, runner)
    if (current.stdout or "").strip() != branch:
        checkout = git(["checkout", "-B", branch], cwd, runner)
        if checkout.returncode != 0:
            return {"ok": False, "error": (checkout.stderr or "").strip()}
    status = git(["status", "--porcelain"], cwd, runner)
    if not (status.stdout or "").strip():
        return {"ok": True, "committed": False}
    git(["add", "-A"], cwd, runner)
    commit = git(["commit", "-m", message], cwd, runner)
    return {"ok": commit.returncode == 0, "committed": commit.returncode == 0,
            "error": (commit.stderr or "").strip() or None}


def write_handoff(task_dir, task, result):
    """The note a fresh worker reads when the original session id is gone."""
    os.makedirs(task_dir, exist_ok=True)
    lines = [
        "# Handoff for %s" % task["id"],
        "",
        "Repo: %s" % task["repo"],
        "Branch: %s" % task["branch"],
        "Steps done: %d" % task.get("steps_done", 0),
        "",
        "## Original request",
        "",
        task["prompt"].strip(),
        "",
        "## Last step reported",
        "",
        "Status: %s" % (result.get("status") or "unknown"),
        "Summary: %s" % (result.get("summary") or "-"),
        "Next: %s" % (result.get("next") or "-"),
        "",
        "Read `git log %s` for what actually landed." % task["branch"],
        "",
    ]
    path = os.path.join(task_dir, "handoff.md")
    with open(path, "w") as fh:
        fh.write("\n".join(lines))
    return path
