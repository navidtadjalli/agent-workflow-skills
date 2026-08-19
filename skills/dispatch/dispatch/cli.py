"""Command line client. Same state as the daemon, no privileged access.

Every subcommand reads the same flock-guarded documents the daemon writes, so
the CLI works whether or not the daemon is running -- `dispatch add` while the
daemon is down simply queues, and the task starts when the daemon comes back.
"""
import argparse
import json
import os
import sys

from . import config as config_mod
from . import daemon as daemon_mod
from . import governor, lanes, state, usage, winddown

PLUGIN_KEY = "telegram@claude-plugins-official"


def _snapshot(now=None):
    import time

    now = now or time.time()
    doc = state.read_state()
    snapshot = doc.get("governor") or governor.blank()
    tokens = usage.transcript_tokens(now)
    return doc, snapshot, tokens, now


def cmd_status(args):
    doc, snapshot, tokens, now = _snapshot()
    queue = state.read_queue()
    counts = {}
    for task in queue["tasks"]:
        counts[task["state"]] = counts.get(task["state"], 0) + 1
    print("mode: %s" % doc["mode"][lanes.CLAUDE])
    print("usage: %s" % governor.summary(snapshot, now, tokens))
    if doc["armed_resume_at"][lanes.CLAUDE]:
        import time

        print("resume armed: %s" % time.strftime(
            "%Y-%m-%d %H:%M", time.localtime(doc["armed_resume_at"][lanes.CLAUDE])))
    print("queue: %s" % (" ".join("%s=%d" % kv for kv in sorted(counts.items()))
                         or "empty"))
    return 0


def cmd_queue(args):
    queue = state.read_queue()
    if args.json:
        json.dump(queue, sys.stdout, indent=2, sort_keys=True)
        print()
        return 0
    if not queue["tasks"]:
        print("queue empty")
        return 0
    for task in queue["tasks"]:
        if not args.all and task["state"] in ("done", "cancelled", "parsed"):
            continue
        print("%-8s %-11s %-20s steps=%d%s" % (
            task["id"], task["state"], task["repo"], task["steps_done"],
            "  " + task["last_error"] if task.get("last_error") else ""))
    return 0


def cmd_add(args):
    cfg = config_mod.load()
    repos = cfg.get("repos") or {}
    if args.repo not in repos:
        print("unknown repo '%s' · configured: %s" % (
            args.repo, ", ".join(sorted(repos)) or "none"), file=sys.stderr)
        return 2
    with state.mutate_queue() as queue:
        task = state.new_task(queue, args.repo, args.prompt,
                              priority=args.priority,
                              deps=args.dep or [],
                              isolation="worktree" if args.worktree else "repo")
    print(task["id"])
    return 0


def cmd_cancel(args):
    from . import parser

    task_id = parser.normalize_id(args.id)
    with state.mutate_queue() as queue:
        task = state.find(queue, task_id)
        if task is None:
            print("no such task %s" % args.id, file=sys.stderr)
            return 2
        task["state"] = "cancelling" if task["state"] == "running" else "cancelled"
        print("%s %s" % (task_id, task["state"]))
    return 0


def cmd_pause(args):
    with state.mutate_state() as doc:
        doc["mode"] = {lane: "paused" for lane in lanes.ALL}
    print("paused")
    return 0


def cmd_resume(args):
    with state.mutate_state() as doc:
        doc["mode"] = {lane: winddown.RUNNING for lane in lanes.ALL}
        doc["armed_resume_at"] = {lane: None for lane in lanes.ALL}
    print("running")
    return 0


def cmd_logs(args):
    from . import parser

    task_id = parser.normalize_id(args.id)
    path = os.path.join(config_mod.task_dir(task_id), "worker.log")
    try:
        with open(path) as fh:
            sys.stdout.write(fh.read())
    except OSError:
        print("no log for %s" % task_id, file=sys.stderr)
        return 2
    return 0


def cmd_usage(args):
    doc, snapshot, tokens, now = _snapshot()
    if args.poll:
        reading = usage.poll(now=now)
        if not reading.get("ok"):
            print("poll failed: %s" % reading.get("error"), file=sys.stderr)
            return 2
        snapshot = governor.record_poll(snapshot, reading, tokens)
        with state.mutate_state() as live:
            live["governor"] = snapshot
    print(governor.summary(snapshot, now, tokens))
    return 0


def cmd_run(args):
    daemon = daemon_mod.Daemon()
    daemon.run(interval=args.interval, ticks=args.ticks)
    return 0


def _plugin_enabled(settings_path):
    try:
        with open(settings_path) as fh:
            settings = json.load(fh)
    except (OSError, ValueError):
        return False
    enabled = settings.get("enabledPlugins") or {}
    if isinstance(enabled, dict):
        return bool(enabled.get(PLUGIN_KEY))
    return PLUGIN_KEY in enabled


def cmd_setup(args):
    """Write config and print the unit. Never starts anything, never edits settings.

    The refusal below is not caution for its own sake: two processes polling the
    same bot get 409s from the chat API, and the in-session plugin is very likely
    the socket the user is talking to us on right now.
    """
    config_mod.ensure_dirs()
    settings_path = args.settings or os.path.join(
        os.path.expanduser("~"), ".claude", "settings.json")
    if _plugin_enabled(settings_path) and not args.force:
        print("refusing: %s is enabled in %s" % (PLUGIN_KEY, settings_path), file=sys.stderr)
        print("Two consumers of the same bot conflict. Disable that plugin yourself,",
              file=sys.stderr)
        print("then re-run `dispatch setup`. This command will not edit your settings.",
              file=sys.stderr)
        return 3

    cfg = config_mod.load()
    if args.repo:
        repos = dict(cfg.get("repos") or {})
        for entry in args.repo:
            alias, _, path = entry.partition("=")
            if not path:
                print("repo must be alias=path, got: %s" % entry, file=sys.stderr)
                return 2
            repos[alias] = os.path.abspath(os.path.expanduser(path))
        cfg["repos"] = repos
    if args.chat:
        cfg["chat_allowlist"] = sorted(set((cfg.get("chat_allowlist") or []) + args.chat))
    state.write(config_mod.config_path(), cfg)
    print("wrote %s" % config_mod.config_path())

    if config_mod.read_token() is None:
        print("warning: no bot token at %s; chat intake stays offline"
              % config_mod.token_env_path(), file=sys.stderr)

    unit = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "systemd", "dispatchd.service.template")
    print("unit template: %s" % unit)
    print("install it yourself with:")
    print("  mkdir -p ~/.config/systemd/user")
    print("  sed 's|@EXEC@|%s|' %s > ~/.config/systemd/user/dispatchd.service"
          % (os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "scripts", "dispatchd"), unit))
    print("  systemctl --user daemon-reload && systemctl --user enable --now dispatchd")
    return 0


def build_parser():
    root = argparse.ArgumentParser(prog="dispatch", description="queued headless agent work")
    subs = root.add_subparsers(dest="command")

    subs.add_parser("status").set_defaults(func=cmd_status)

    queue = subs.add_parser("queue")
    queue.add_argument("--all", action="store_true", help="include finished tasks")
    queue.add_argument("--json", action="store_true")
    queue.set_defaults(func=cmd_queue)

    add = subs.add_parser("add")
    add.add_argument("repo")
    add.add_argument("prompt")
    add.add_argument("--priority", type=int, default=5)
    add.add_argument("--dep", action="append")
    add.add_argument("--worktree", action="store_true")
    add.set_defaults(func=cmd_add)

    cancel = subs.add_parser("cancel")
    cancel.add_argument("id")
    cancel.set_defaults(func=cmd_cancel)

    subs.add_parser("pause").set_defaults(func=cmd_pause)
    subs.add_parser("resume").set_defaults(func=cmd_resume)

    logs = subs.add_parser("logs")
    logs.add_argument("id")
    logs.set_defaults(func=cmd_logs)

    usage_cmd = subs.add_parser("usage")
    usage_cmd.add_argument("--poll", action="store_true",
                           help="spend one request on a real /usage reading")
    usage_cmd.set_defaults(func=cmd_usage)

    run = subs.add_parser("run", help="run the daemon loop in the foreground")
    run.add_argument("--interval", type=float, default=5)
    run.add_argument("--ticks", type=int, default=None)
    run.set_defaults(func=cmd_run)

    setup = subs.add_parser("setup")
    setup.add_argument("--repo", action="append", metavar="ALIAS=PATH")
    setup.add_argument("--chat", action="append", metavar="CHAT_ID")
    setup.add_argument("--settings", help="settings file to check for a conflicting plugin")
    setup.add_argument("--force", action="store_true",
                       help="write config even if a conflicting chat plugin is enabled")
    setup.set_defaults(func=cmd_setup)

    return root


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not getattr(args, "func", None):
        build_parser().print_help()
        return 2
    return args.func(args)
