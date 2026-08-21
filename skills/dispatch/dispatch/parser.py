"""Frozen-intake command parser.

The daemon accepts messages even when the plan window is exhausted, so the
common commands must parse with zero model calls -- otherwise "what's my queue"
would itself be rate-limited. Anything this parser does not recognize is handed
to a small model call, and if *that* is unavailable the raw text is stored as
``needs_parse`` and re-read once the window resets.
"""
import re

RUN = re.compile(
    r"^run\s+(?P<prompt>.+?)\s+(?:on|in)\s+(?P<repo>[\w.\-/]+)\s*$", re.IGNORECASE | re.DOTALL)
RUN_COLON = re.compile(
    r"^run\s+(?P<repo>[\w.\-/]+)\s*:\s*(?P<prompt>.+)$", re.IGNORECASE | re.DOTALL)
AGENTS = ("claude", "codex")
AGENT_RUN = re.compile(
    r"^(?P<agent>claude|codex)\s+(?P<prompt>.+?)\s+(?:on|in)\s+(?P<repo>[\w.\-/]+)\s*$",
    re.IGNORECASE | re.DOTALL)
AGENT_COLON = re.compile(
    r"^(?P<agent>claude|codex)\s+(?P<repo>[\w.\-/]+)\s*:\s*(?P<prompt>.+)$",
    re.IGNORECASE | re.DOTALL)
AGENT_BARE = re.compile(
    r"^(?P<agent>claude|codex)\s+(?P<prompt>.+)$", re.IGNORECASE | re.DOTALL)
USAGE_POLL = re.compile(r"^usage\s+(?:--)?poll\s*$", re.IGNORECASE)
SESSIONS = re.compile(r"^sessions(?:\s+(?P<project>[\w.\-/]+))?\s*$", re.IGNORECASE)
LANE_MODE = re.compile(r"^(?P<verb>pause|resume)\s+(?P<lane>claude|codex)\s*$",
                       re.IGNORECASE)
WITH_ID = re.compile(
    r"^(?P<verb>cancel|logs|log|show|retry)\s+(?P<id>t-?\d+|\d+)\s*$", re.IGNORECASE)
BARE = {
    "ping": "ping",
    # Answers to a free-form proposal. Bare `cancel` is deliberately not one:
    # `cancel <id>` already means something else, and a one-word overlap on a
    # destructive verb is not worth the convenience.
    "yes": "confirm",
    "y": "confirm",
    "no": "deny",
    "n": "deny",
    "status": "status",
    "queue": "queue",
    "q": "queue",
    "usage": "usage",
    "pause": "pause",
    "resume": "resume",
    "help": "help",
    "sessions": "sessions",
    "repos": "repos",
    "projects": "repos",
}
ISOLATION = re.compile(r"\s+(?:in\s+a\s+)?worktree\s*$", re.IGNORECASE)

# Every kind ``parse`` can return, declared rather than inferred. The daemon's
# chat surface must have a branch for each: a kind with no branch falls through
# to free-form intake, which answers with nothing when there is no text -- and
# on a chat surface, silence is indistinguishable from a dropped message.
KINDS = ("cancel", "confirm", "deny", "help", "logs", "need_repo", "pause",
         "ping", "queue", "repos", "resume", "retry", "run", "sessions",
         "status", "unparsed", "usage")


def normalize_id(raw):
    """``7``, ``t-7`` and ``t-0007`` all name the same task."""
    digits = re.sub(r"\D", "", str(raw))
    if not digits:
        return None
    return "t-%04d" % int(digits)


def parse(text):
    """Return a command dict. ``kind == 'unparsed'`` means: ask a model.

    Order matters. ``run`` is checked before the agent verbs so that
    ``run claude tests on qpay`` stays a run whose prompt happens to start with
    an agent's name, and the bare-agent rejection is checked last so it only
    catches text that no other rule claimed.
    """
    if text is None:
        return {"kind": "unparsed", "text": ""}
    stripped = text.strip()
    if not stripped:
        return {"kind": "unparsed", "text": ""}

    lowered = stripped.lower().lstrip("/")
    if lowered in BARE:
        kind = BARE[lowered]
        if kind == "usage":
            return {"kind": "usage", "poll": False}
        if kind == "sessions":
            return {"kind": "sessions", "project": None}
        if kind in ("pause", "resume"):
            return {"kind": kind, "lane": None}
        return {"kind": kind}

    body = stripped.lstrip("/")

    if USAGE_POLL.match(body):
        return {"kind": "usage", "poll": True}

    lane_mode = LANE_MODE.match(body)
    if lane_mode:
        return {"kind": lane_mode.group("verb").lower(),
                "lane": lane_mode.group("lane").lower()}

    sessions = SESSIONS.match(body)
    if sessions:
        return {"kind": "sessions", "project": sessions.group("project")}

    with_id = WITH_ID.match(body)
    if with_id:
        verb = with_id.group("verb").lower()
        verb = "logs" if verb in ("logs", "log", "show") else verb
        return {"kind": verb, "id": normalize_id(with_id.group("id"))}

    isolation = "repo"
    candidate = body
    if ISOLATION.search(candidate):
        isolation = "worktree"
        candidate = ISOLATION.sub("", candidate)

    for pattern in (RUN_COLON, RUN):
        match = pattern.match(candidate)
        if match:
            prompt = match.group("prompt").strip()
            repo = match.group("repo").strip().rstrip(".,")
            if prompt and repo:
                return {"kind": "run", "agent": "claude", "prompt": prompt,
                        "repo": repo, "isolation": isolation}

    for pattern in (AGENT_COLON, AGENT_RUN):
        match = pattern.match(candidate)
        if match:
            prompt = match.group("prompt").strip()
            repo = match.group("repo").strip().rstrip(".,")
            if prompt and repo:
                return {"kind": "run", "agent": match.group("agent").lower(),
                        "prompt": prompt, "repo": repo, "isolation": isolation}

    bare_agent = AGENT_BARE.match(candidate)
    if bare_agent:
        return {"kind": "need_repo", "agent": bare_agent.group("agent").lower(),
                "prompt": bare_agent.group("prompt").strip()}

    return {"kind": "unparsed", "text": stripped}


def render_ack(task_id, mode, resumes_at_text=None):
    """The one-line reply the daemon sends back on intake."""
    parts = ["queued %s" % task_id, mode]
    if mode != "running" and resumes_at_text:
        parts.append("resumes ~%s" % resumes_at_text)
    return " · ".join(parts)
