"""Agent backends. Two of them, resolved by a task's ``agent`` field."""
from . import claude, codex

REGISTRY = {"claude": claude, "codex": codex}


def get(agent):
    """Backend module for ``agent``. Anything unrecognised is Claude's."""
    return REGISTRY.get(agent, claude)


__all__ = ["claude", "codex", "get", "REGISTRY"]
