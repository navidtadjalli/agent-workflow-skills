"""What can be dispatched to, and what merely exists.

There is no hand-maintained alias map any more. Every folder under the projects
root is listed; the ones containing ``.git`` are dispatchable. That distinction
is load-bearing rather than cosmetic: the worker contract checkpoints to
``tg/<id>``, so a folder with no repository has nowhere to park work when the
window winds down, and the work would be lost rather than deferred.

Widening the boundary from "aliases you typed" to "every git repo under
~/Projects" is a real widening, and it is why ``render`` exists -- the boundary
should be one chat message away, not a config file you have to remember to read.
"""
import os

from . import config as config_mod


def root_path(config=None):
    config = config or config_mod.load()
    return os.path.abspath(os.path.expanduser(config.get("projects_root") or "~/Projects"))


def _entry(path):
    return {"path": path, "git": os.path.isdir(os.path.join(path, ".git"))}


def discover(root=None, overrides=None):
    """Every candidate folder, keyed by the name you would type in chat."""
    root = root or root_path()
    found = {}
    try:
        names = sorted(os.listdir(root))
    except OSError:
        names = []
    for name in names:
        if name.startswith("."):
            continue
        path = os.path.join(root, name)
        if os.path.isdir(path):
            found[name] = _entry(path)
    for alias, path in (overrides or {}).items():
        found[alias] = _entry(os.path.abspath(os.path.expanduser(path)))
    return found


def dispatchable(found):
    return {name: entry for name, entry in found.items() if entry["git"]}


def resolve(alias, root=None, overrides=None, found=None):
    """Path for ``alias``, or None if it is unknown or not dispatchable."""
    found = discover(root=root, overrides=overrides) if found is None else found
    entry = found.get(alias)
    if entry is None or not entry["git"]:
        return None
    return entry["path"]


def reject_reason(alias, found):
    """Why ``alias`` cannot be dispatched to. Assumes resolve() returned None."""
    entry = found.get(alias)
    if entry is None:
        names = ", ".join(sorted(dispatchable(found))) or "none"
        return "unknown repo '%s' · dispatchable: %s" % (alias, names)
    return "'%s' has no git repo · not dispatchable" % alias


def render(found, limit=60):
    """The `repos` chat reply."""
    if not found:
        return "no folders found under %s" % root_path()
    names = sorted(found)
    lines = ["%d folders · %d dispatchable" % (len(names), len(dispatchable(found)))]
    for name in names[:limit]:
        lines.append("  %s%s" % (name, "" if found[name]["git"] else "  (no git)"))
    if len(names) > limit:
        lines.append("  ... %d more" % (len(names) - limit))
    return "\n".join(lines)
