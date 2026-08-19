"""Codex plan limits, read for free from codex's own session logs.

The Claude governor exists because ``/usage`` is the only source of truth and
asking costs a request. Codex has no such problem: every ``token_count`` event
it writes carries the server's own rate-limit block, so the reading is already
on disk by the time we want it.

    "rate_limits": {
      "primary":   {"used_percent": 99.0, "window_minutes": 300,   "resets_at": ...},
      "secondary": {"used_percent": 95.0, "window_minutes": 10080, "resets_at": ...}
    }

Three things the data forces. The same window turns up under either slot
depending on ``limit_id``, so entries are keyed by ``window_minutes`` and the
slot name is ignored. Both slots are frequently ``null``, which means "no
information", not "zero". And a record whose ``resets_at`` has passed describes
a window that has since rolled over, so it is discarded rather than believed.

Freshness differs in kind from the Claude governor's. A codex percentage is
exactly as old as your last codex run, and waiting cannot improve it -- the only
way to get a newer one is to run codex. So absence is deliberately *not* a
blocking condition: the lane starts optimistic and the first task's own output
corrects it. Blocking on absence would deadlock the lane permanently.
"""
import glob
import json
import os

SESSION_WINDOW_MINUTES = 300
WEEK_WINDOW_MINUTES = 10080

# Newest N session files to look at. Limits are written on every turn, so the
# newest handful always carries the answer; scanning the whole tree would be
# pure waste on an account with years of sessions.
SCAN_LIMIT = 40

# Bytes read from the end of each file. Limits appear on every turn, so the
# tail always holds the newest one, and a long session file can be very large.
TAIL_BYTES = 262_144


def default_root():
    return os.path.join(os.path.expanduser("~"), ".codex", "sessions")


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _tail_lines(path):
    """Complete lines from the tail of a file, cheaply."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - TAIL_BYTES))
            data = handle.read()
    except OSError:
        return []
    lines = data.decode("utf-8", "ignore").splitlines()
    # The first line is a fragment whenever we seeked into the middle.
    return lines[1:] if size > TAIL_BYTES and lines else lines


def _entries(node):
    """Every populated limit dict reachable from ``node``, at any depth."""
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            block = current.get("rate_limits")
            if isinstance(block, dict):
                for slot in ("primary", "secondary"):
                    entry = block.get(slot)
                    if (isinstance(entry, dict)
                            and entry.get("used_percent") is not None
                            and entry.get("window_minutes") is not None
                            and entry.get("resets_at") is not None):
                        yield entry
            stack.extend(v for v in current.values() if isinstance(v, (dict, list)))
        elif isinstance(current, list):
            stack.extend(v for v in current if isinstance(v, (dict, list)))


def _file_windows(path, now):
    """Newest unexpired entry per window within one file."""
    found = {}
    for line in _tail_lines(path):
        if '"rate_limits"' not in line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        for entry in _entries(record):
            if float(entry["resets_at"]) <= now:
                continue  # that window has already rolled over
            found[int(entry["window_minutes"])] = entry
    return found


def reading(now, root=None, scan_limit=SCAN_LIMIT):
    """Newest unexpired limit per window. ``ok`` is False when none was found."""
    root = root or default_root()
    paths = sorted(glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True),
                   key=_mtime, reverse=True)[:scan_limit]
    best = {}
    for path in paths:
        for window, entry in _file_windows(path, now).items():
            best.setdefault(window, entry)  # newest file scanned first
        if SESSION_WINDOW_MINUTES in best and WEEK_WINDOW_MINUTES in best:
            break

    session = best.get(SESSION_WINDOW_MINUTES)
    week = best.get(WEEK_WINDOW_MINUTES)
    return {
        "ok": bool(best),
        "at": now,
        "session_pct": None if session is None else float(session["used_percent"]),
        "session_reset": None if session is None else float(session["resets_at"]),
        "week_pct": None if week is None else float(week["used_percent"]),
        "week_reset": None if week is None else float(week["resets_at"]),
    }


def estimate(now, root=None, scan_limit=SCAN_LIMIT):
    """Admission-shaped view, matching ``governor.estimate``'s keys.

    ``stale`` is always False. See the module docstring: absence of a reading
    is not a reason to block, because running codex is the only thing that
    produces one.
    """
    current = reading(now, root=root, scan_limit=scan_limit)
    if not current["ok"]:
        return {"session_pct": 0.0, "week_pct": None, "source": "codex-unknown",
                "stale": False, "resets_at": None}
    return {
        "session_pct": current["session_pct"] if current["session_pct"] is not None else 0.0,
        "week_pct": current["week_pct"],
        "source": "codex-logs",
        "stale": False,
        "resets_at": current["session_reset"] or current["week_reset"],
    }


def summary(now, root=None):
    """One line for `status` and `usage`."""
    current = reading(now, root=root)
    if not current["ok"]:
        return "codex: no reading (run codex once)"
    parts = []
    if current["session_pct"] is not None:
        parts.append("5h %.0f%%" % current["session_pct"])
    if current["week_pct"] is not None:
        parts.append("7d %.0f%%" % current["week_pct"])
    return "codex: " + " · ".join(parts)
