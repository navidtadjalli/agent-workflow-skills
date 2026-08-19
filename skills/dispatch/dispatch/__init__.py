"""dispatch — queue, governor, and scheduler for headless agent work.

Standard library only. The daemon (``dispatchd``) owns the chat socket, the
queue, and the workers; the CLI (``dispatch``) is a thin client over the same
state. Every module below the daemon is a pure function of injected clock and
usage values so it can be tested offline.
"""

__all__ = [
    "config",
    "state",
    "usage",
    "governor",
    "scheduler",
    "winddown",
    "parser",
    "worker",
    "daemon",
    "cli",
]
