"""Lanes: one per agent, each with its own governor and its own wind-down.

The two lanes are independent in everything except repo locks. A task belongs
to exactly one, decided at enqueue by which verb the user typed.

``mode`` and ``armed_resume_at`` used to be scalars. They are dicts now, and the
readers below accept both forms -- a state file written by the previous version
has to keep loading, and the migration is cheap enough to do on every read
rather than as a one-shot upgrade step that could be skipped.
"""

CLAUDE = "claude"
CODEX = "codex"
ALL = (CLAUDE, CODEX)

RUNNING = "running"


def of(task):
    """The lane a task belongs to. Anything unrecognised is Claude's."""
    agent = (task or {}).get("agent")
    return agent if agent in ALL else CLAUDE


def normalize_mode(value):
    """Per-lane modes, from either the dict form or the old scalar form."""
    if isinstance(value, dict):
        return {lane: value.get(lane) or RUNNING for lane in ALL}
    scalar = value or RUNNING
    return {lane: scalar for lane in ALL}


def normalize_armed(value):
    """Per-lane armed-resume timestamps, from either form."""
    if isinstance(value, dict):
        return {lane: value.get(lane) for lane in ALL}
    return {lane: value for lane in ALL}
