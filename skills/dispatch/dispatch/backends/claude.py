"""Claude backend: argv, house rules, and the fenced status block.

Everything here was previously inline in ``worker.py`` and is unchanged in
behaviour, with one exception: the prompt arrives on stdin from a file rather
than embedded in argv.
"""
import json
import re

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


def resume_args(session_id):
    """Claude continues with an option pair."""
    return ["--resume", session_id] if session_id else []


def build_command(task, prompt_path, cwd, task_dir, unsafe=True):
    argv = ["claude", "-p", "-", "--output-format", "json"]
    if unsafe:
        argv.append("--dangerously-skip-permissions")
    argv.extend(resume_args(task.get("session_id")))
    return argv


def parse_result(output, task_dir):
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

    for raw in reversed(FENCE.findall(text)):
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
