"""Two governors, one namespace.

``governor.<name>`` is the Claude governor, unchanged -- every existing call
site predates the split and must keep working. ``governor.codex`` is the second
one, which is cheaper in kind: codex writes its own plan limits to disk, so
there is nothing to poll and nothing to interpolate.
"""
from . import claude, codex
from .claude import (  # noqa: F401
    EMPTY,
    LADDER,
    RATIO_SMOOTHING,
    SEED_PCT_PER_TOKEN,
    blank,
    est_cost_pct,
    estimate,
    learn_cost,
    max_concurrency,
    note_limit_error,
    poll_interval,
    record_poll,
    should_poll,
    summary,
)

def pct_line(reading):
    """One lane's percentages, from either governor's estimate.

    Both ``estimate`` functions return the same keys on purpose, so there is
    one renderer rather than one per surface -- chat, `dispatch status` and
    `dispatch usage` must never disagree about what a lane is doing.
    """
    if reading.get("session_pct") is None:
        return "unknown"
    parts = ["session %.0f%%" % reading["session_pct"]]
    if reading.get("week_pct") is not None:
        parts.append("week %.0f%%" % reading["week_pct"])
    parts.append(reading.get("source") or "unknown")
    return " · ".join(parts)


__all__ = [
    "claude", "codex", "EMPTY", "LADDER", "RATIO_SMOOTHING",
    "SEED_PCT_PER_TOKEN", "blank", "est_cost_pct", "estimate", "learn_cost",
    "max_concurrency", "note_limit_error", "pct_line", "poll_interval",
    "record_poll", "should_poll", "summary",
]
