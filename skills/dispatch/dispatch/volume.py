"""Token volume by window, project, and model — parsed from local logs.

Ported from ``~/.claude/scripts/usage_tg.py``, which ran standalone once and
sent its own Telegram message. As a module the daemon imports, it gives up
everything that assumed a one-shot process: the module-level ``NOW`` becomes
a parameter on every function here, so a long-lived import does not freeze
the 5h window at daemon start, and every function is a pure function of the
clock and the log roots it is given -- which is also what makes it testable
without touching a real home directory.

It also gives up the one paid call the script made. ``plan_limits()`` shelled
out to ``claude -p "/usage"`` to get the real percentages, since those are
never written to disk. The governor (see ``governor.claude``) already owns
that call and paces it behind a floor, so this module does not repeat it --
``render()`` reports volume only, and never reaches a subprocess. The
percentages are composed alongside this report at the call site.
"""
import glob
import json
import os
import time
from collections import Counter

HOME = os.path.expanduser("~")

HOUR = 3600
WINDOWS = [("5h", 5 * HOUR), ("24h", 24 * HOUR), ("7d", 7 * 24 * HOUR)]


def human(n):
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if n >= div:
            return "%.1f%s" % (n / div, unit)
    return str(int(n))


def parse_ts(ts):
    try:
        return time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return None


def claude_usage(now, root=None):
    """Token totals from Claude Code transcripts, windowed off ``now``."""
    root = root or os.path.join(HOME, ".claude", "projects")
    win = Counter()
    proj = Counter()          # project -> tokens in the shortest window
    models = Counter()
    msgs_5h = 0
    seen = set()
    for f in glob.glob(os.path.join(root, "*", "*.jsonl")):
        if now - os.path.getmtime(f) > WINDOWS[-1][1]:
            continue
        name = os.path.basename(os.path.dirname(f))
        name = name.replace("-home-navid-Projects-", "") or "Projects"
        if name.startswith("-home-navid-Projects"):
            name = "Projects"
        try:
            with open(f, errors="ignore") as fh:
                for line in fh:
                    if '"usage"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    m = d.get("message") or {}
                    u = m.get("usage") or {}
                    if not u:
                        continue
                    mid = m.get("id")
                    if mid:
                        if mid in seen:
                            continue
                        seen.add(mid)
                    t = parse_ts(d.get("timestamp") or "")
                    if t is None:
                        continue
                    age = now - t
                    tot = sum(u.get(k, 0) or 0 for k in (
                        "input_tokens", "output_tokens",
                        "cache_creation_input_tokens", "cache_read_input_tokens"))
                    out = u.get("output_tokens", 0) or 0
                    for label, span in WINDOWS:
                        if age < span:
                            win[label] += tot
                            win[label + ":out"] += out
                    if age < WINDOWS[0][1]:
                        proj[name] += tot
                        models[m.get("model") or "?"] += tot
                        msgs_5h += 1
        except Exception:
            continue
    return win, proj, models, msgs_5h


def codex_usage(now, root=None):
    """Newest total_token_usage per session file touched in the last 24h.

    The record's own ``total_tokens`` is read directly rather than recomputed
    from ``input_tokens`` + ``output_tokens``: it is the field the session log
    already labels as the total, and trusting it is what lets a session whose
    record only carries a subset of sub-fields still report correctly.
    """
    root = root or os.path.join(HOME, ".codex", "sessions")
    tot = Counter()
    proj = Counter()
    files = glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True)
    for f in files:
        age = now - os.path.getmtime(f)
        if age > 24 * HOUR:
            continue
        cwd, last = None, None
        try:
            with open(f, errors="ignore") as fh:
                for line in fh:
                    if cwd is None and '"cwd"' in line:
                        try:
                            cwd = (json.loads(line).get("payload") or {}).get("cwd")
                        except Exception:
                            pass
                        continue
                    if '"total_token_usage"' not in line:
                        continue
                    try:
                        p = json.loads(line).get("payload") or {}
                    except Exception:
                        continue
                    info = p.get("info") or p
                    if isinstance(info, dict) and isinstance(info.get("total_token_usage"), dict):
                        last = info["total_token_usage"]
        except Exception:
            continue
        if last:
            n = last.get("total_tokens", 0) or 0
            tot["all"] += n
            tot["out"] += last.get("output_tokens", 0) or 0
            if age < 5 * HOUR:
                tot["5h"] += n
            proj[os.path.basename(cwd) if cwd else "?"] += n
    return tot, proj


def active_sessions(now, claude_root=None, codex_root=None):
    """Session logs written to in the last 10 minutes, by project."""
    claude_root = claude_root or os.path.join(HOME, ".claude", "projects")
    codex_root = codex_root or os.path.join(HOME, ".codex", "sessions")
    out = []
    for f in glob.glob(os.path.join(claude_root, "*", "*.jsonl")):
        age = now - os.path.getmtime(f)
        if age < 600:
            name = os.path.basename(os.path.dirname(f)).replace("-home-navid-Projects-", "") or "Projects"
            out.append((int(age / 60), name))
    for f in glob.glob(os.path.join(codex_root, "**", "*.jsonl"), recursive=True):
        age = now - os.path.getmtime(f)
        if age < 600:
            out.append((int(age / 60), "codex"))
    out.sort()
    return out


def render(now, claude_root=None, codex_root=None):
    """The volume-only report: no plan-limit percentages, no subprocess."""
    win, proj, models, msgs = claude_usage(now, root=claude_root)
    cx, cxproj = codex_usage(now, root=codex_root)
    act = active_sessions(now, claude_root=claude_root, codex_root=codex_root)

    lines = ["USAGE — %s" % time.strftime("%Y-%m-%d %H:%M", time.localtime(now))]
    lines.append("CLAUDE CODE (context tokens / output)")
    for label, _ in WINDOWS:
        lines.append("  %-4s %8s  out %s" % (label, human(win[label]), human(win[label + ":out"])))
    lines.append("  %d assistant turns in last 5h" % msgs)
    if proj:
        lines.append("  5h by project: " + " · ".join(
            "%s %s" % (k, human(v)) for k, v in proj.most_common(6)))
    if models:
        lines.append("  5h by model: " + " · ".join(
            "%s %s" % (k.replace("claude-", ""), human(v)) for k, v in models.most_common(4)))

    lines.append("")
    lines.append("CODEX (sessions touched in 24h)")
    lines.append("  24h %s  ·  5h %s  ·  out %s" % (human(cx["all"]), human(cx["5h"]), human(cx["out"])))
    if cxproj:
        lines.append("  by project: " + " · ".join(
            "%s %s" % (k, human(v)) for k, v in cxproj.most_common(5)))

    lines.append("")
    if act:
        lines.append("ACTIVE NOW (<10m): " + " · ".join("%s %dm" % (n, a) for a, n in act[:6]))
    else:
        lines.append("ACTIVE NOW: nothing running")

    lines.append("")
    lines.append("Volume only — plan-limit % comes from usage poll, not from disk.")
    return "\n".join(lines)


if __name__ == "__main__":
    import time as _time

    print(render(_time.time()))
