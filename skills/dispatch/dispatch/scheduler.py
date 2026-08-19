"""Admission control: which queued task, if any, may start right now.

Every predicate is a pure function of an injected context so the whole decision
surface is testable without a filesystem, a clock, or a live plan.
"""

from . import governor


def lock_name(task):
    """Serialization key. Same repo serializes unless the task is isolated."""
    if task.get("isolation") == "worktree":
        return "worktree-%s" % task["id"]
    return "repo-%s" % task["repo"]


def deps_satisfied(task, queue):
    done = {t["id"] for t in queue["tasks"] if t["state"] == "done"}
    return all(dep in done for dep in task.get("deps") or [])


def sort_key(task):
    """Paused before queued -- a paused task already carries loaded context."""
    return (0 if task["state"] == "paused" else 1,
            task.get("priority", 5),
            task["id"])


def runnable(queue):
    return sorted((t for t in queue["tasks"] if t["state"] in ("queued", "paused")),
                  key=sort_key)


def admit(task, ctx):
    """Decide whether ``task`` may start. Returns ``(bool, reason)``.

    ``ctx`` carries: ``mode``, ``queue``, ``running``, ``session_pct``,
    ``week_pct``, ``stale``, ``est_cost_pct``, ``config``, and ``lock_free`` --
    a callable taking a lock name.
    """
    config = ctx["config"]

    if ctx["mode"] != "running":
        return False, "mode is %s" % ctx["mode"]

    if not deps_satisfied(task, ctx["queue"]):
        return False, "dependencies not done"

    if ctx.get("stale"):
        return False, "usage unknown; poll first"

    session_pct = ctx["session_pct"]
    week_pct = ctx.get("week_pct")

    if week_pct is not None and week_pct >= config["week_soft"]:
        return False, "weekly usage %.0f%% at or above soft limit" % week_pct

    allowed = governor.max_concurrency(session_pct)
    if ctx["running"] >= allowed:
        return False, "concurrency %d/%d at %.0f%% session usage" % (
            ctx["running"], allowed, session_pct)

    projected = session_pct + ctx["est_cost_pct"]
    if projected > config["session_soft"]:
        return False, "step would reach %.0f%%, past the %.0f%% soft limit" % (
            projected, config["session_soft"])

    if not ctx["lock_free"](lock_name(task)):
        return False, "%s is busy" % task["repo"]

    return True, "admitted"


def next_admissible(ctx):
    """First task that passes admission, with the reasons the others failed."""
    reasons = []
    for task in runnable(ctx["queue"]):
        ok, reason = admit(task, ctx)
        if ok:
            return task, reasons
        reasons.append((task["id"], reason))
    return None, reasons
