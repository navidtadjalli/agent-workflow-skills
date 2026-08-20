"""Codex backend.

Codex gives a stronger status contract than Claude for free. ``--output-schema``
makes the model's final message conform to a schema, and ``-o`` writes that
message to a file, so the status is read as JSON rather than regexed out of an
event stream. The house rules therefore drop the fenced-block instruction; the
rest is identical, because the wind-down path depends on it.

Continuation is a subcommand (``codex exec resume <id>``), not a trailing flag.
That is the one shape difference from Claude that ``build_command`` has to
respect, and it is why ``resume_args`` returns a positional fragment here.

How a worker is confined is a config value, ``codex_sandbox``, and its default
is ``--approve-for-me``: unattended like the bypass flag it replaces, but with
approval requests routed through codex's own automatic review and the workspace
confined to the repo the task named. The bypass flag remains selectable, because
the reviewed mode has not been exercised against a live codex here -- the weekly
window was full -- and a mode that stalls on approvals must be recoverable by
editing one value, not by editing this file and redeploying.

That applies to a first step. ``codex exec resume`` parses a narrower set of
options than ``codex exec`` and takes neither ``-s`` nor ``--approve-for-me``,
so a continuation runs under codex's own default policy unless the bypass was
asked for explicitly. Related, and *not* fixed here: the same narrower parser
also rejects the ``-C`` this function passes on every step, so a resumed codex
step exits 2 before it starts. That predates this change and needs a live codex
to verify a fix against; see the hardening report.

Known gap: the session id is recovered by looking for ``thread_id`` /
``session_id`` / ``conversation_id`` anywhere in the ``--json`` event stream. If
none is present the step still settles correctly -- the id is simply ``None``,
and the next step starts a fresh codex session seeded from ``handoff.md``, which
is the same fallback a lost Claude session takes.
"""
import json
import os
import sys

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

# What each ``codex_sandbox`` value puts on the command line. Ordered as codex
# orders them, most confined first; `bypass` is the old behaviour, kept as an
# explicit opt-out rather than a default.
SANDBOX_MODES = {
    "read-only": ["-s", "read-only"],
    "approve-for-me": ["--approve-for-me"],
    "workspace-write": ["-s", "workspace-write"],
    "danger-full-access": ["-s", "danger-full-access"],
    "bypass": ["--dangerously-bypass-approvals-and-sandbox"],
}
DEFAULT_SANDBOX = "approve-for-me"

# `codex exec resume` is a narrower parser than `codex exec`. Measured on
# codex-cli 0.148.0, it rejects `-s` and `--approve-for-me` outright -- `error:
# unexpected argument` -- and takes only the bypass flag. A flag it rejects is
# not a weaker sandbox; it is a step that exits 2 before doing anything. So a
# continuation carries only what resume accepts, and otherwise runs under
# codex's own default policy.
RESUME_MODES = ("bypass",)


STATUS_FILE = "last.json"


def house_rules(task):
    return HOUSE_RULES.format(branch=task["branch"]).strip()


def reset(task_dir):
    """Drop the previous step's status file before a new step starts.

    ``-o`` overwrites this file when codex finishes normally and leaves it
    untouched when the process dies -- at ``step_timeout``, on SIGKILL, on a
    crash. Nothing else cleared it, so a dead step read the *previous* step's
    block and settled as that step's success: requeued with an incremented
    ``steps_done`` and a checkpoint commit labelled with a summary describing
    work it never did. With no step cap anywhere, that repeats.
    """
    try:
        os.unlink(os.path.join(task_dir, STATUS_FILE))
    except OSError:
        pass


def resume_args(session_id):
    """Codex continues with a subcommand, so this is positional."""
    return ["resume", session_id] if session_id else []


def sandbox_args(config=None, resuming=False):
    """The flags for the configured mode, defaulting to the confined one.

    An unrecognised value resolves to the default rather than to the most
    permissive entry: a typo in a key that decides how much of the disk an
    unattended agent can write is not a licence to write all of it.

    ``resuming`` drops anything the ``resume`` subcommand does not accept.
    """
    mode = (config or {}).get("codex_sandbox") or DEFAULT_SANDBOX
    args = SANDBOX_MODES.get(mode)
    if args is None:
        print("warning: unknown codex_sandbox %r; using %s. Valid: %s"
              % (mode, DEFAULT_SANDBOX, ", ".join(sorted(SANDBOX_MODES))),
              file=sys.stderr)
        mode, args = DEFAULT_SANDBOX, SANDBOX_MODES[DEFAULT_SANDBOX]
    if resuming and mode not in RESUME_MODES:
        return []
    return list(args)


def build_command(task, prompt_path, cwd, task_dir, unsafe=True, config=None):
    resuming = bool(task.get("session_id"))
    argv = ["codex", "exec"]
    argv.extend(resume_args(task.get("session_id")))
    argv.extend([
        "-",                       # prompt on stdin
        "--json",
        "-C", cwd,
        "--skip-git-repo-check",
        "--output-schema", SCHEMA_PATH,
        "-o", os.path.join(task_dir, STATUS_FILE),
    ])
    if unsafe:
        # Unchanged in meaning: `unsafe=False` asks for no approval or sandbox
        # flag at all and lets codex apply its own defaults.
        argv.extend(sandbox_args(config, resuming=resuming))
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
        with open(os.path.join(task_dir, STATUS_FILE)) as fh:
            block = json.load(fh)
    except (OSError, ValueError):
        return {"status": None, "summary": "", "next": "", "session_id": session_id}
    if not isinstance(block, dict) or block.get("status") not in VALID:
        return {"status": None, "summary": "", "next": "", "session_id": session_id}
    return {"status": block["status"],
            "summary": block.get("summary") or "",
            "next": block.get("next") or "",
            "session_id": session_id}
