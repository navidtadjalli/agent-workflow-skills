"""Codex backend.

Codex gives a stronger status contract than Claude for free. ``--output-schema``
makes the model's final message conform to a schema, and ``-o`` writes that
message to a file, so the status is read as JSON rather than regexed out of an
event stream. The house rules therefore drop the fenced-block instruction; the
rest is identical, because the wind-down path depends on it.

Continuation is a subcommand (``codex exec resume <id>``), not a trailing flag.
That is the one shape difference from Claude that ``build_command`` has to
respect, and it is why ``resume_args`` returns a positional fragment here.

Known gap: the session id is recovered by looking for ``thread_id`` /
``session_id`` / ``conversation_id`` anywhere in the ``--json`` event stream. If
none is present the step still settles correctly -- the id is simply ``None``,
and the next step starts a fresh codex session seeded from ``handoff.md``, which
is the same fallback a lost Claude session takes.
"""
import json
import os

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "status.schema.json")

HOUSE_RULES = """
Operating rules for this run:
- Do one coherent chunk of work, then stop. Do not try to finish everything.
- Commit checkpoints to the branch {branch}. Never commit to main.
- Never push. Never force-push. Never rewrite published history.
- If you need a decision only the user can make, stop and report blocked.
- Your final message must be the JSON object required by the output schema:
  status is "complete" when the deliverable is done and verified, "continue"
  when another step is needed, "blocked" when only the user can decide.
"""

VALID = ("complete", "continue", "blocked")
SESSION_KEYS = ("thread_id", "session_id", "conversation_id")


def house_rules(task):
    return HOUSE_RULES.format(branch=task["branch"]).strip()


def resume_args(session_id):
    """Codex continues with a subcommand, so this is positional."""
    return ["resume", session_id] if session_id else []


def build_command(task, prompt_path, cwd, task_dir, unsafe=True):
    argv = ["codex", "exec"]
    argv.extend(resume_args(task.get("session_id")))
    argv.extend([
        "-",                       # prompt on stdin
        "--json",
        "-C", cwd,
        "--skip-git-repo-check",
        "--output-schema", SCHEMA_PATH,
        "-o", os.path.join(task_dir, "last.json"),
    ])
    if unsafe:
        argv.append("--dangerously-bypass-approvals-and-sandbox")
    return argv


def _session_id(output):
    for line in (output or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        for key in SESSION_KEYS:
            value = event.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def parse_result(output, task_dir):
    session_id = _session_id(output)
    try:
        with open(os.path.join(task_dir, "last.json")) as fh:
            block = json.load(fh)
    except (OSError, ValueError):
        return {"status": None, "summary": "", "next": "", "session_id": session_id}
    if not isinstance(block, dict) or block.get("status") not in VALID:
        return {"status": None, "summary": "", "next": "", "session_id": session_id}
    return {"status": block["status"],
            "summary": block.get("summary") or "",
            "next": block.get("next") or "",
            "session_id": session_id}
