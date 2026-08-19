"""Worker supervision: run one step of one task, then decide what happens next.

A "step" is one headless agent invocation. Steps are deliberately small: the
agent is told to do one coherent chunk and stop, so that the wind-down path
always has a clean seam to stop at, and so a step that dies costs little.

Every step ends by parsing a fenced JSON status block out of the agent's own
output. That block -- not an exit code, not a heuristic -- decides whether the
task is complete, wants another step, or is blocked.
"""
import json
import os
import re
import signal
import subprocess

from . import usage

HOUSE_RULES = """
Operating rules for this run:
- Do one coherent chunk of work, then stop. Do not try to finish everything.
- Commit checkpoints to the branch {branch}. Never commit to main.
- Never push. Never force-push. Never rewrite published history.
- If you need a decision only the user can make, stop and report blocked.
- End your final message with a fenced json block, exactly:

```json
{{"status": "complete|continue|blocked", "summary": "...", "next": "..."}}
```
"""

FENCE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)
VALID = ("complete", "continue", "blocked")


def house_rules(task):
    return HOUSE_RULES.format(branch=task["branch"]).strip()


def build_prompt(task):
    return "%s\n\n%s" % (task["prompt"].strip(), house_rules(task))


def build_command(task, unsafe=True):
    """The argv for one step. ``--resume`` continues a live session."""
    argv = ["claude", "-p", build_prompt(task), "--output-format", "json"]
    if unsafe:
        argv.append("--dangerously-skip-permissions")
    if task.get("session_id"):
        argv.extend(["--resume", task["session_id"]])
    return argv


def parse_status(output):
    """Extract the agent's self-reported status block.

    Accepts either raw text or the ``--output-format json`` envelope, whose
    ``result`` field holds the text the fence lives in.
    """
    text = output or ""
    session_id = None
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            envelope = json.loads(stripped)
        except ValueError:
            envelope = None
        if isinstance(envelope, dict):
            session_id = envelope.get("session_id")
            if isinstance(envelope.get("result"), str):
                text = envelope["result"]

    blocks = FENCE.findall(text)
    for raw in reversed(blocks):
        try:
            block = json.loads(raw)
        except ValueError:
            continue
        if isinstance(block, dict) and block.get("status") in VALID:
            return {"status": block["status"],
                    "summary": block.get("summary") or "",
                    "next": block.get("next") or "",
                    "session_id": session_id}
    return {"status": None, "summary": "", "next": "", "session_id": session_id}


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


def run_step(task, cwd, config, env=None, popen=None, sleeper=None):
    """Execute one step under a wall-clock cap. Returns a result dict.

    ``popen`` and ``sleeper`` are injected in tests. The termination path is
    SIGTERM, a grace period, then SIGKILL: a checkpointing agent deserves the
    chance to finish its commit, but not indefinitely.
    """
    popen = popen or subprocess.Popen
    argv = build_command(task)
    process = popen(argv, cwd=cwd, env=env or os.environ.copy(),
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    timed_out = False
    try:
        output, _ = process.communicate(timeout=config["step_timeout"])
    except subprocess.TimeoutExpired:
        timed_out = True
        output = _terminate(process, config, sleeper)

    result = parse_status(output)
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
