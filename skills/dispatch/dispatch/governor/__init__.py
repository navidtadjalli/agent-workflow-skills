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

__all__ = [
    "claude", "codex", "EMPTY", "LADDER", "RATIO_SMOOTHING",
    "SEED_PCT_PER_TOKEN", "blank", "est_cost_pct", "estimate", "learn_cost",
    "max_concurrency", "note_limit_error", "poll_interval", "record_poll",
    "should_poll", "summary",
]
