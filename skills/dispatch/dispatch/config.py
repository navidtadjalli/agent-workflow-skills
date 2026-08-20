"""Configuration and on-disk locations.

Everything is rooted at ``DISPATCH_HOME`` (default ``~/.claude/dispatch``) so
tests can point the whole system at a temporary directory.
"""
import os
import sys

DEFAULTS = {
    # Governor thresholds, in percent of the plan window.
    "session_soft": 85.0,
    "session_hard": 95.0,
    "week_soft": 90.0,
    # Poll cadence, seconds. `floor` is never violated, whatever asks.
    "poll_floor": 60,
    "poll_idle": 600,
    "poll_hot": 180,
    # Worker supervision.
    "step_timeout": 3600,
    "term_grace": 20,
    # How codex workers are confined. `approve-for-me` is unattended and keeps
    # codex's sandbox: approvals go through its automatic review and the
    # workspace is the repo the task named. `read-only`, `workspace-write` and
    # `danger-full-access` select `-s` directly; `bypass` is
    # `--dangerously-bypass-approvals-and-sandbox`, the explicit opt-out.
    # Claude has no equivalent and ignores this.
    "codex_sandbox": "approve-for-me",
    # Cost model: percent of the session window a single step is assumed to
    # burn before a repo has any measured history.
    "default_est_cost_pct": 6.0,
    # Chat. Empty means nobody: the daemon refuses to serve chat until an id
    # is listed here, rather than admitting every chat that finds the bot.
    "chat_allowlist": [],
    # Repos are discovered under this root; `repos` holds overrides only --
    # aliases pointing somewhere else entirely.
    "projects_root": "~/Projects",
    "repos": {},
}


def home():
    """Root directory for all dispatch state."""
    override = os.environ.get("DISPATCH_HOME")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(os.path.expanduser("~"), ".claude", "dispatch")


def path(*parts):
    return os.path.join(home(), *parts)


def config_path():
    return path("config.json")


def queue_path():
    return path("queue.json")


def state_path():
    return path("state.json")


def daemon_log_path():
    """Where the daemon appends what the tmux pane cannot keep.

    ``logs --daemon`` captures the *live* pane, so it can never show why the
    previous daemon died -- and a daemon that dies takes its session, and its
    scrollback, with it. This file outlives both.
    """
    return path("daemon.log")


def lock_path(name):
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
    return path("locks", safe + ".lock")


def task_dir(task_id):
    return path("tasks", task_id)


def token_env_path():
    """Where the chat bot token lives. Never copied into dispatch state."""
    return os.environ.get(
        "DISPATCH_TOKEN_ENV",
        os.path.join(os.path.expanduser("~"), ".claude", "channels", "telegram", ".env"),
    )


def transcripts_root():
    """Where Claude writes session transcripts, which the governor counts.

    The daemon is handed a token counter and can be pointed anywhere; the CLI
    computes the same number itself and had no such seam, so `dispatch status`
    read whichever home directory it happened to run under -- including a test
    runner's. Overridable for the same reason ``DISPATCH_TOKEN_ENV`` is.
    """
    return os.environ.get(
        "DISPATCH_TRANSCRIPTS",
        os.path.join(os.path.expanduser("~"), ".claude", "projects"),
    )


def codex_sessions_root():
    """Where codex writes the session logs its plan limits come from."""
    return os.environ.get(
        "DISPATCH_CODEX_SESSIONS",
        os.path.join(os.path.expanduser("~"), ".codex", "sessions"),
    )


def ensure_dirs():
    for sub in ("", "locks", "tasks"):
        os.makedirs(path(sub) if sub else home(), exist_ok=True)


def _unreadable(detail):
    """Say, out loud, that the file holding the trust boundary did not parse.

    Swallowing this is how the allowlist could vanish without anyone knowing:
    a truncated write or a bad hand-edit fell back to ``DEFAULTS``, and the bot
    went on answering. The fallback stays -- raising would take the daemon and
    the CLI down together, leaving no way to fix the file that broke them --
    but the defaults it falls back to now admit nobody, and this line names the
    file so the condition is repairable rather than merely survivable.
    """
    print("warning: %s is unreadable (%s); falling back to defaults, whose "
          "chat allowlist is empty -- chat intake stays refused until the file "
          "is fixed" % (config_path(), detail), file=sys.stderr)


def load(overrides=None):
    """Merge defaults with config.json and an optional in-memory override.

    A missing file is normal -- first run, and every test home -- and stays
    silent. Anything else that stops the file being read is reported.
    """
    import json

    cfg = dict(DEFAULTS)
    loaded = None
    try:
        with open(config_path()) as fh:
            loaded = json.load(fh)
    except FileNotFoundError:
        loaded = None
    except (OSError, ValueError) as exc:
        _unreadable("%s: %s" % (type(exc).__name__, exc))
        loaded = None
    else:
        if loaded is not None and not isinstance(loaded, dict):
            # Valid JSON, wrong shape. `cfg.update` on a list raises, and on a
            # string of two-character items silently invents keys.
            _unreadable("top level is %s, not an object"
                        % type(loaded).__name__)
            loaded = None
    if loaded:
        cfg.update(loaded)
    if overrides:
        cfg.update(overrides)
    return cfg


def read_token():
    """Read the bot token from the channel env file. Returns None if absent."""
    try:
        with open(token_env_path()) as fh:
            for line in fh:
                if line.startswith("TELEGRAM_BOT_TOKEN="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        return None
    return None
