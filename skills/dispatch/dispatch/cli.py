"""Command line client. Same state as the daemon, no privileged access.

Every subcommand reads the same flock-guarded documents the daemon writes, so
the CLI works whether or not the daemon is running -- `dispatch add` while the
daemon is down simply queues, and the task starts when the daemon comes back.
"""
import argparse
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time

from . import chat as chat_mod
from . import config as config_mod
from . import daemon as daemon_mod
from . import governor, lanes, repos, state, usage, volume

PLUGIN_KEY = "telegram@claude-plugins-official"
TMUX_SESSION = "dispatchd"
AGENTS = ("claude", "codex")


def _snapshot(now=None):
    now = now or time.time()
    doc = state.read_state()
    snapshot = doc.get("governor") or governor.blank()
    tokens = usage.transcript_tokens(now)
    return doc, snapshot, tokens, now


def cmd_status(args):
    """Both lanes, always.

    This is the surface the user is left with when the chat one is down, which
    is exactly when a report about half the system is worst-case.
    """
    doc, snapshot, tokens, now = _snapshot()
    readings = {lanes.CLAUDE: governor.estimate(snapshot, now, tokens),
                lanes.CODEX: governor.codex.estimate(now)}
    queue = state.read_queue()
    counts = {}
    for task in queue["tasks"]:
        counts[task["state"]] = counts.get(task["state"], 0) + 1
    for lane in lanes.ALL:
        print("%-7s %s · %s" % (lane, doc["mode"][lane],
                                governor.pct_line(readings[lane])))
        armed = doc["armed_resume_at"][lane]
        if armed:
            print("        %s resume armed %s" % (
                lane, time.strftime("%Y-%m-%d %H:%M", time.localtime(armed))))
        hold = daemon_mod.hold_line(doc, lane, doc["mode"][lane])
        if hold:
            print(hold)
    print("queue   %s" % (" ".join("%s %d" % kv for kv in sorted(counts.items()))
                          or "empty"))
    chat_line = daemon_mod.chat_status_line(doc)
    if chat_line:
        # The one thing this surface can report that the chat one cannot: if
        # the transport is down, its own status reply never arrives.
        print(chat_line)
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
    """Queue a task, resolving the repo exactly the way the daemon does.

    Through ``repos``, not through ``cfg["repos"]``: the alias map was retired
    when discovery landed and ``setup`` leaves it empty, so resolving against it
    refused every repo the daemon accepts from chat -- and this command is the
    fallback for when chat is the thing that is down.
    """
    cfg = config_mod.load()
    found = repos.discover(root=repos.root_path(cfg), overrides=cfg.get("repos"))
    if repos.resolve(args.repo, found=found) is None:
        print(repos.reject_reason(args.repo, found), file=sys.stderr)
        return 2
    with state.mutate_queue() as queue:
        task = state.new_task(queue, args.repo, args.prompt,
                              priority=args.priority,
                              deps=args.dep or [],
                              isolation="worktree" if args.worktree else "repo",
                              agent=getattr(args, "agent", None) or lanes.CLAUDE)
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
    """`dispatch pause [lane]`, meaning exactly what `pause [lane]` does in chat."""
    print(daemon_mod.set_mode("pause", getattr(args, "lane", None)))
    return 0


def cmd_resume(args):
    print(daemon_mod.set_mode("resume", getattr(args, "lane", None)))
    return 0


def cmd_logs(args):
    from . import parser

    if getattr(args, "daemon", False):
        result = _tmux(["capture-pane", "-pt", TMUX_SESSION],
                       getattr(args, "runner", None))
        if result.returncode == 0 and (result.stdout or "").strip():
            sys.stdout.write(result.stdout)
            return 0
        # `capture-pane` reads the *live* pane, so it can never say why the
        # previous daemon died -- and the case you most want a log for is
        # exactly the one where there is no pane left to read.
        log = _daemon_log_tail()
        if log:
            print("%s:" % config_mod.daemon_log_path())
            print(log)
            return 0
        detail = (result.stderr or "").strip()
        print("no output from tmux session '%s'%s · nothing in %s either"
              % (TMUX_SESSION, " · " + detail if detail else "",
                 config_mod.daemon_log_path()),
              file=sys.stderr)
        return 2
    if not args.id:
        print("logs needs a task id, or --daemon", file=sys.stderr)
        return 2
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
    """Both lanes and the volume report, the same composition chat gets.

    Reporting only the Claude lane here meant the terminal fallback could not
    answer the question the codex lane's own wind-down turns on.
    """
    doc, snapshot, tokens, now = _snapshot()
    if args.poll:
        reading = usage.poll(now=now)
        if not reading.get("ok"):
            print("poll failed: %s" % reading.get("error"), file=sys.stderr)
            return 2
        snapshot = governor.record_poll(snapshot, reading, tokens)
        with state.mutate_state() as live:
            live["governor"] = snapshot
    print("CLAUDE  %s" % governor.summary(snapshot, now, tokens))
    polled_at = snapshot.get("polled_at")
    if polled_at:
        print("        last real poll %dm ago" % ((now - polled_at) // 60))
    print("CODEX   %s" % governor.pct_line(governor.codex.estimate(now)))
    print()
    try:
        # Explicit roots: `volume` defaults to the home directory it was ported
        # from, and every other root this CLI reads is configurable.
        print(volume.render(now, claude_root=config_mod.transcripts_root(),
                            codex_root=config_mod.codex_sessions_root()))
    except Exception as exc:  # noqa: BLE001 - a bad log must not hide the rest
        print("volume report unavailable: %s" % exc, file=sys.stderr)
    return 0


def cmd_run(args):
    daemon = daemon_mod.Daemon()
    daemon.run(interval=args.interval, ticks=args.ticks)
    return 0


class _Unrunnable:
    """A command that could not be started, shaped like a CompletedProcess.

    From cron a traceback goes nowhere; a non-zero return does not.
    """

    returncode = 127
    stdout = ""

    def __init__(self, message):
        self.stderr = message


def _tmux(argv, runner=None):
    runner = runner or subprocess.run
    try:
        return runner(["tmux"] + list(argv), capture_output=True, text=True)
    except OSError as exc:
        return _Unrunnable("tmux: %s" % exc)


def session_alive(runner=None):
    return _tmux(["has-session", "-t", TMUX_SESSION], runner).returncode == 0


def daemon_path():
    """The PATH to pin on the daemon, rather than whatever it would inherit.

    ``new-session`` starts the tmux server with the caller's environment when no
    server is running, and every process in the session inherits it -- the
    daemon, and the workers it execs a bare ``claude`` or ``codex`` through, and
    ``usage.poll``. The cron watchdog wins that race after a reboot, so the
    environment is cron's: ``/usr/bin:/bin``, without the user bin directory both
    agents install into. Nothing would look wrong -- the session exists, so the
    watchdog stays quiet; the daemon is healthy, so chat answers -- and every
    task would fail with command-not-found.

    The launcher's own PATH, plus ``~/.local/bin`` when it is missing, which is
    where both agents live. Appended rather than prepended: a human running
    `dispatch up` should get the same agent their shell would.
    """
    parts = [part for part in (os.environ.get("PATH") or "").split(os.pathsep)
             if part]
    user_bin = os.path.join(os.path.expanduser("~"), ".local", "bin")
    if user_bin not in parts:
        parts.append(user_bin)
    return os.pathsep.join(parts)


def _daemon_argv():
    """The command the tmux session runs, and the directory it runs it in.

    Absolute, because cron has no PATH. Through ``realpath``, because Task 11
    puts this CLI on PATH as a symlink and the daemon must still be found next
    to the package rather than next to the link.

    It is the ``dispatchd`` script, not ``python3 -m dispatch.cli run``: this
    module has no ``__main__`` guard, so ``-m`` would import it and exit 0 --
    tmux would report a successful launch of a session that was already gone,
    and the watchdog would do it again five minutes later, forever.

    PATH rides on the command as an assignment prefix, not as ``new-session -e``:
    measured on tmux 3.6, a pane takes its PATH from the server and an ``-e
    PATH=`` is silently ignored, while the prefix is applied by the shell tmux
    runs the command through.
    """
    package = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    script = os.path.join(package, "scripts", "dispatchd")
    command = "PATH=%s %s %s" % (shlex.quote(daemon_path()),
                                 shlex.quote(sys.executable), shlex.quote(script))
    return command, package


def _keep_unreadable_config():
    """Copy a config.json that does not parse aside before setup rewrites it.

    ``setup`` merges its arguments onto ``config.load()``, and a file that
    cannot be read loads as the defaults -- so the write that follows would
    discard whatever repo overrides and chat ids the broken file still holds.
    They are the two things hardest to reconstruct from memory, so the bytes
    are kept rather than the operator's recollection of them.
    """
    path = config_mod.config_path()
    # Bound before the try: a *read* that fails leaves nothing to copy, and
    # falling through to write it raised UnboundLocalError -- a traceback out
    # of `main()`, in the repair command, after the plugin has already been
    # disabled. That leaves the cutover with neither interface.
    body = None
    try:
        with open(path) as fh:
            body = fh.read()
        json.loads(body)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        read_error = exc
    else:
        return None
    if body is None:
        print("warning: could not read %s to keep a copy (%s: %s); it is left "
              "where it is" % (path, type(read_error).__name__, read_error),
              file=sys.stderr)
        return None
    kept = path + ".corrupt"
    try:
        with open(kept, "w") as fh:
            fh.write(body)
        # The same ids and repo paths as the file it came from, so the same
        # mode: the copy is written at the ambient umask otherwise.
        os.chmod(kept, 0o600)
    except OSError as exc:
        print("warning: could not keep the unreadable %s (%s)" % (path, exc),
              file=sys.stderr)
        return None
    print("warning: %s did not parse; kept it as %s before overwriting"
          % (path, kept), file=sys.stderr)
    return kept


def _warn_empty_allowlist(cfg=None):
    """Say, on stderr, that this configuration answers nobody.

    An empty allowlist is refused rather than served -- see
    ``daemon._default_chat`` -- and after the cutover a bot that answers nobody
    is indistinguishable from a healthy daemon with nothing to do. Both places
    that hand the user a running system say so; neither refuses, because the
    queue and `dispatch add` still work without chat.
    """
    cfg = config_mod.load() if cfg is None else cfg
    if chat_mod.normalize_allowlist(cfg.get("chat_allowlist")):
        return False
    print("warning: chat_allowlist is empty; the daemon will refuse every "
          "chat. Fix it with: dispatch setup --chat <chat-id>", file=sys.stderr)
    return True


def cmd_up(args):
    """Start the daemon in tmux. Idempotent; --if-dead makes it silent."""
    runner = getattr(args, "runner", None)
    if session_alive(runner):
        if not getattr(args, "if_dead", False):
            print("dispatchd already running in tmux session '%s'" % TMUX_SESSION)
        return 0
    command, cwd = _daemon_argv()
    result = _tmux(["new-session", "-d", "-s", TMUX_SESSION, "-c", cwd, command],
                   runner)
    if result.returncode != 0:
        print("failed to start: %s" % (result.stderr or "").strip(), file=sys.stderr)
        return 1
    if not _survived(runner, getattr(args, "settle", 1.0)):
        # `new-session` returns 0 the moment the session exists. With the
        # default `remain-on-exit off`, a daemon that dies during startup takes
        # the session with it milliseconds later -- and `up` would report
        # success, the watchdog would find nothing to do five minutes later,
        # and the traceback would have gone down with the pane.
        print("failed to start: dispatchd exited immediately", file=sys.stderr)
        for line in _startup_diagnostics(runner):
            print(line, file=sys.stderr)
        return 1
    print("started dispatchd in tmux session '%s'" % TMUX_SESSION)
    missing = [agent for agent in AGENTS
               if shutil.which(agent, path=daemon_path()) is None]
    if missing:
        # Starting anyway: a daemon with one working lane still answers chat,
        # and refusing would take the only surface down with it. But this is the
        # failure that looks like success, so it is said out loud.
        print("warning: %s not on the daemon's PATH; that lane will fail with "
              "command-not-found" % ", ".join(missing), file=sys.stderr)
    if config_mod.read_token() is None:
        # Same shape as the missing-agent warning, and worse in kind: without a
        # token the daemon runs with no chat transport at all, which after the
        # cutover means no interface at all.
        print("warning: no bot token at %s; the daemon will run with no chat "
              "transport" % config_mod.token_env_path(), file=sys.stderr)
    # Loaded here and passed down, as `setup` does: `load` reports a config it
    # cannot read, and one command reading the file twice would say so twice.
    _warn_empty_allowlist(config_mod.load())
    return 0


def _survived(runner, settle):
    """Whether the session is still there a moment after it was created."""
    if settle:
        time.sleep(settle)
    return session_alive(runner)


def _startup_diagnostics(runner, limit=2000):
    """Whatever the dead launch left behind: the pane, then the daemon log.

    The pane is usually gone with the session, which is exactly why the daemon
    writes its own crash log; this reads both rather than assuming either.
    """
    lines = []
    pane = _tmux(["capture-pane", "-pt", TMUX_SESSION], runner)
    if pane.returncode == 0 and (pane.stdout or "").strip():
        lines.append(pane.stdout.strip()[-limit:])
    log = _daemon_log_tail(limit)
    if log:
        lines.append("%s:" % config_mod.daemon_log_path())
        lines.append(log)
    if not lines:
        lines.append("no output captured · run `dispatch run` in the foreground "
                     "to see the failure")
    return lines


def _daemon_log_tail(limit=4000):
    """The end of the daemon's own log, or None when there is none."""
    try:
        with open(config_mod.daemon_log_path()) as fh:
            body = fh.read()
    except OSError:
        return None
    body = body.strip()
    return body[-limit:] if body else None


def cmd_down(args):
    runner = getattr(args, "runner", None)
    if not session_alive(runner):
        print("dispatchd is not running")
        return 0
    result = _tmux(["kill-session", "-t", TMUX_SESSION], runner)
    if result.returncode != 0:
        # "exactly one consumer of the bot token" is the premise of the whole
        # cutover, so being told it stopped when it did not is the wrong
        # direction to be wrong in.
        print("failed to stop dispatchd: %s" % (result.stderr or "").strip(),
              file=sys.stderr)
        return 1
    print("stopped dispatchd")
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


def _copy_mode(source, target):
    """Give ``target`` ``source``'s permissions. Best effort; it is a copy."""
    try:
        os.chmod(target, stat.S_IMODE(os.stat(source).st_mode))
    except OSError:
        pass


def _without_plugin(enabled):
    """``enabledPlugins`` with this plugin off, or None if it was already off.

    Both shapes are real: a dict of ``{key: bool}`` and a plain list of enabled
    keys. ``_plugin_enabled`` reads both, so this has to write both -- handling
    only the dict meant a genuinely enabled plugin was reported as impossible
    to disable, which is the one path where setup says "turn it off by hand"
    about something it could have turned off.
    """
    if isinstance(enabled, dict):
        return dict(enabled, **{PLUGIN_KEY: False}) if enabled.get(PLUGIN_KEY) else None
    if isinstance(enabled, list):
        return ([key for key in enabled if key != PLUGIN_KEY]
                if PLUGIN_KEY in enabled else None)
    return None


def disable_plugin(settings_path):
    """Turn the conflicting chat plugin off. Returns the backup path, or None.

    Exactly one value changes, and the file is copied verbatim first. The copy
    is the original bytes rather than a re-serialization, because the point of a
    backup is to put back exactly what was there -- a reformatted one would
    still rewrite the file the user asked us not to rewrite. For the same reason
    the edit keeps the original key order instead of sorting.

    The new content is renamed into place over ``realpath``: atomic, so there is
    no instant in which the user's settings exist truncated, and still the same
    inode a symlinked ``settings.json`` points at rather than a regular file
    replacing the link. Nothing here raises -- on the one path where the file
    cannot be written, ``cmd_setup``'s "backup: ..." line would never print, so
    this says where the copy went itself.
    """
    try:
        with open(settings_path) as fh:
            original = fh.read()
        settings = json.loads(original)
    except (OSError, ValueError):
        return None
    if not isinstance(settings, dict):
        return None
    disabled = _without_plugin(settings.get("enabledPlugins"))
    if disabled is None:
        return None

    backup = settings_path + ".bak"
    try:
        with open(backup, "w") as fh:
            fh.write(original)
        _copy_mode(settings_path, backup)
    except OSError as exc:
        print("could not back up %s: %s · nothing was changed"
              % (settings_path, exc), file=sys.stderr)
        return None

    settings["enabledPlugins"] = disabled
    target = os.path.realpath(settings_path)
    try:
        handle, temporary = tempfile.mkstemp(
            dir=os.path.dirname(target) or ".", prefix=".dispatch-settings-")
        try:
            with os.fdopen(handle, "w") as fh:
                json.dump(settings, fh, indent=2)
                fh.write("\n")
            _copy_mode(target, temporary)
            os.replace(temporary, target)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
    except OSError as exc:
        print("could not write %s: %s · it is unchanged and a copy of it is at %s"
              % (settings_path, exc, backup), file=sys.stderr)
        return None
    return backup


def cmd_setup(args):
    """Write config, and take the bot away from the plugin. Starts nothing.

    The 2026-08-18 design refused to touch ``settings.json`` because the plugin
    was a peer interface worth protecting. It is the thing being replaced now,
    and leaving it enabled guarantees the 409 the refusal was avoiding: one
    token admits exactly one consumer.
    """
    config_mod.ensure_dirs()
    settings_path = args.settings or os.path.join(
        os.path.expanduser("~"), ".claude", "settings.json")
    still_enabled = False
    if _plugin_enabled(settings_path):
        if args.keep_plugin:
            print("warning: %s is still enabled; two consumers of one bot get 409s"
                  % PLUGIN_KEY, file=sys.stderr)
        else:
            backup = disable_plugin(settings_path)
            if backup:
                print("disabled %s in %s (backup: %s)"
                      % (PLUGIN_KEY, settings_path, backup))
            else:
                still_enabled = True
                print("could not disable %s in %s; turn it off by hand before "
                      "starting the daemon" % (PLUGIN_KEY, settings_path),
                      file=sys.stderr)

    _keep_unreadable_config()
    cfg = config_mod.load()
    if args.repo:
        # `overrides`, not `repos`: that name is the discovery module here now.
        overrides = dict(cfg.get("repos") or {})
        for entry in args.repo:
            alias, _, path = entry.partition("=")
            if not path:
                print("repo must be alias=path, got: %s" % entry, file=sys.stderr)
                return 2
            overrides[alias] = os.path.abspath(os.path.expanduser(path))
        cfg["repos"] = overrides
    if args.chat:
        cfg["chat_allowlist"] = sorted(set((cfg.get("chat_allowlist") or []) + args.chat))
    state.write(config_mod.config_path(), cfg)
    print("wrote %s" % config_mod.config_path())

    if config_mod.read_token() is None:
        print("warning: no bot token at %s; chat intake stays offline"
              % config_mod.token_env_path(), file=sys.stderr)

    _warn_empty_allowlist(cfg)

    if still_enabled:
        # Non-zero, though the config was written and everything else worked.
        # One token admits exactly one consumer: with the plugin left enabled,
        # `dispatch up` produces a daemon that 409s on every poll while every
        # health surface stays green. A stderr line the operator may not be
        # watching is not enough to stop a scripted cutover from continuing --
        # an exit status is.
        print("setup incomplete: %s is still enabled" % PLUGIN_KEY,
              file=sys.stderr)
        return 1

    print("start it with: dispatch up")
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
    add.add_argument("--agent", choices=list(AGENTS), default=lanes.CLAUDE,
                     help="which lane to queue into (default: claude)")
    add.set_defaults(func=cmd_add)

    cancel = subs.add_parser("cancel")
    cancel.add_argument("id")
    cancel.set_defaults(func=cmd_cancel)

    for verb, func in (("pause", cmd_pause), ("resume", cmd_resume)):
        lane = subs.add_parser(verb, help="%s one lane, or both" % verb)
        lane.add_argument("lane", nargs="?", choices=list(lanes.ALL),
                          help="which lane; omit for both")
        lane.set_defaults(func=func)

    logs = subs.add_parser("logs")
    logs.add_argument("id", nargs="?")
    logs.add_argument("--daemon", action="store_true",
                      help="show the daemon's own output instead of a task's")
    logs.set_defaults(func=cmd_logs)

    usage_cmd = subs.add_parser("usage")
    usage_cmd.add_argument("--poll", action="store_true",
                           help="spend one request on a real /usage reading")
    usage_cmd.set_defaults(func=cmd_usage)

    run = subs.add_parser("run", help="run the daemon loop in the foreground")
    run.add_argument("--interval", type=float, default=5)
    run.add_argument("--ticks", type=int, default=None)
    run.set_defaults(func=cmd_run)

    up = subs.add_parser("up", help="start the daemon in tmux (idempotent)")
    up.add_argument("--if-dead", action="store_true",
                    help="say nothing when it is already running")
    up.set_defaults(func=cmd_up)

    subs.add_parser("down", help="stop the tmux session").set_defaults(func=cmd_down)

    setup = subs.add_parser("setup")
    setup.add_argument("--repo", action="append", metavar="ALIAS=PATH")
    setup.add_argument("--chat", action="append", metavar="CHAT_ID")
    setup.add_argument("--settings", help="settings file holding the plugin switch")
    setup.add_argument("--keep-plugin", action="store_true",
                       help="do not disable the conflicting chat plugin")
    setup.set_defaults(func=cmd_setup)

    return root


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not getattr(args, "func", None):
        build_parser().print_help()
        return 2
    return args.func(args)
