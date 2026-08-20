"""Session dashboard -- what's running, what was I working on.

Ported from ``~/.claude/skills/manage/manage.py``, which was a standalone
script: it read ``time.time()`` inside its own helpers and printed a
markdown table meant for a terminal. Neither survives inside a daemon. The
clock becomes a parameter on every function here, for the same reason as
``volume.py`` -- a long-lived import must not freeze "how long ago" at
import time. And the render is plain text, not markdown: it goes straight
into a Telegram message with no parse mode set, so a ``|---`` table row
would show up as literal noise rather than a table.

Two things the original script did are deliberately not here. Listing
launchable project folders is ``repos.py``'s job now (it additionally knows
which folders are git repos and therefore dispatchable, so a second
implementation of "list the projects" would just drift from it). And
``launch`` spawned a GUI terminal running interactive ``claude`` -- fine when
a person typed the command themselves, but triggered from Telegram it would
leave a session sitting at a prompt with nobody there to type into it. That
subprocess-spawning code does not come across.
"""
import glob
import json
import os
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
SESS_ROOT = os.path.join(HOME, ".claude", "projects")


def _rel(ts_epoch, now):
    if not ts_epoch:
        return "?"
    d = now - ts_epoch
    if d < 90:
        return "just now"
    for unit, sec in (("d", 86400), ("h", 3600), ("m", 60)):
        if d >= sec:
            return "%d%s ago" % (d // sec, unit)
    return "just now"


def _status(ts_epoch, now):
    if not ts_epoch:
        return "⚪ idle"
    d = now - ts_epoch
    if d < 600:
        return "🟢 active"
    if d < 86400:
        return "🟡 today"
    if d < 7 * 86400:
        return "🔵 this week"
    return "⚪ idle"


def _parse_iso(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(
            tzinfo=timezone.utc).timestamp()
    except Exception:
        return None


def scan(path, now):
    """Read one .jsonl session -> summary dict, or None if empty/unreadable."""
    title = last_prompt = cwd = None
    mode = perm = None
    n_user = n_asst = 0
    last_ts = None
    try:
        with open(path, "r", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                t = d.get("type")
                if t == "ai-title":
                    title = d.get("aiTitle") or title
                elif t == "last-prompt":
                    last_prompt = d.get("lastPrompt") or last_prompt
                elif t == "mode":
                    mode = d.get("mode") or mode
                elif t == "permission-mode":
                    perm = d.get("permissionMode") or perm
                elif t == "user":
                    n_user += 1
                elif t == "assistant":
                    n_asst += 1
                if d.get("cwd"):
                    cwd = d["cwd"]
                ts = _parse_iso(d.get("timestamp", "")) if d.get("timestamp") else None
                if ts and (last_ts is None or ts > last_ts):
                    last_ts = ts
    except Exception:
        return None
    # mtime is the reliable "last touched" signal even if records lack ts
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None
    activity = max(x for x in (last_ts, mtime) if x) if (last_ts or mtime) else None
    if n_user == 0 and n_asst == 0 and not title:
        return None  # empty stub session
    sid = os.path.basename(path)[:-6]
    return {
        "id": sid,
        "title": title,
        "last_prompt": last_prompt,
        "cwd": cwd,
        "mode": mode,
        "perm": perm,
        "n_user": n_user,
        "n_asst": n_asst,
        "activity": activity,
        "path": path,
    }


def _project_label(s):
    if s["cwd"]:
        return os.path.basename(s["cwd"].rstrip("/")) or s["cwd"]
    # fall back to decoding the parent dir name
    parent = os.path.basename(os.path.dirname(s["path"]))
    return parent.lstrip("-").replace("-home-navid-Projects-", "").replace("-", "/") or parent


def _trunc(t, n):
    if not t:
        return ""
    t = " ".join(t.split())
    return t if len(t) <= n else t[: n - 1] + "…"


def collect(now, root=None, project=None, limit=12):
    """Session summaries, newest first, optionally filtered by project."""
    root = root or SESS_ROOT
    rows = []
    for path in glob.glob(os.path.join(root, "*", "*.jsonl")):
        summary = scan(path, now)
        if summary is None:
            continue
        if project and project.lower() not in _project_label(summary).lower():
            continue
        rows.append(summary)
    rows.sort(key=lambda s: s["activity"] or 0, reverse=True)
    return rows[:limit]


def render(now, root=None, project=None, limit=12):
    """Plain-text dashboard. No markdown -- chat replies set no parse mode."""
    rows = collect(now, root=root, project=project, limit=limit)
    if not rows:
        return "no sessions found"
    lines = ["%d session(s)%s" % (len(rows),
                                  " in %s" % project if project else "")]
    for summary in rows:
        lines.append("%s %s · %s · %d↑%d↓ · %s" % (
            _status(summary["activity"], now),
            _trunc(summary["title"] or summary["last_prompt"] or summary["id"], 44),
            _project_label(summary),
            summary["n_user"], summary["n_asst"],
            _rel(summary["activity"], now)))
        if summary["last_prompt"]:
            lines.append("    ↳ %s" % _trunc(summary["last_prompt"], 70))
    return "\n".join(lines)
