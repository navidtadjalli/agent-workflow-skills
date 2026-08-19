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
WITH_ID = re.compile(
    r"^(?P<verb>cancel|logs|log|show|retry)\s+(?P<id>t-?\d+|\d+)\s*$", re.IGNORECASE)
BARE = {
    "status": "status",
    "queue": "queue",
    "q": "queue",
    "usage": "usage",
    "pause": "pause",
    "resume": "resume",
    "help": "help",
}
ISOLATION = re.compile(r"\s+(?:in\s+a\s+)?worktree\s*$", re.IGNORECASE)


def normalize_id(raw):
    """``7``, ``t-7`` and ``t-0007`` all name the same task."""
    digits = re.sub(r"\D", "", str(raw))
    if not digits:
        return None
    return "t-%04d" % int(digits)


def parse(text):
    """Return a command dict. ``kind == 'unparsed'`` means: ask a model."""
    if text is None:
        return {"kind": "unparsed", "text": ""}
    stripped = text.strip()
    if not stripped:
        return {"kind": "unparsed", "text": ""}

    lowered = stripped.lower().lstrip("/")
    if lowered in BARE:
        return {"kind": BARE[lowered]}

    body = stripped.lstrip("/")

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
                return {"kind": "run", "prompt": prompt, "repo": repo,
                        "isolation": isolation}

    return {"kind": "unparsed", "text": stripped}


def render_ack(task_id, mode, resumes_at_text=None):
    """The one-line reply the daemon sends back on intake."""
    parts = ["queued %s" % task_id, mode]
    if mode != "running" and resumes_at_text:
        parts.append("resumes ~%s" % resumes_at_text)
    return " · ".join(parts)
