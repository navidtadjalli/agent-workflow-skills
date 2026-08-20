"""The daemon: one tick, repeated.

Everything expensive or non-deterministic is injected -- clock, usage poll,
token counter, codex reading, chat transport, step runner, executor -- so the
whole control loop can be driven step by step in a test with no network, no
subprocesses, and no real time passing.

The loop never blocks on a task. A repo whose lock is held is simply not
admissible this tick; a step that is still running is left alone. That is what
lets intake keep answering while the plan window is exhausted.

There are two lanes, one per agent, and they are independent in everything the
tick does: each has its own governor reading, its own concurrency ladder, and
its own wind-down machine, so an exhausted Claude window says nothing about
whether codex may run. The single coupling is the per-repo lock, and it is
deliberate: both lanes checkpoint to ``tg/<id>`` in the same checkout, so two
workers in one repo would interleave commits and corrupt each other's work.
"""
import concurrent.futures
import json
import os
import subprocess
import time

from . import chat as chat_mod
from . import config as config_mod
from . import governor, lanes, parser, repos, scheduler, sessions, state, usage, volume, winddown

PARSE_PROMPT = (
    "Convert this request into a JSON array of tasks. Each element must have "
    '"repo" (one of: %s) and "prompt". Reply with JSON only, no prose.\n\n%s')


class InlineExecutor:
    """Runs work synchronously. Used in tests; keeps tick() deterministic."""

    def submit(self, fn, *args, **kwargs):
        future = concurrent.futures.Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as exc:  # noqa: BLE001 - mirror executor semantics
            future.set_exception(exc)
        return future

    def shutdown(self, wait=True):
        return None


class Daemon:
    def __init__(self, config=None, clock=None, poll_usage=None, count_tokens=None,
                 chat=None, run_step=None, executor=None, freeform=None,
                 checkpoint=None, codex_estimate=None):
        config_mod.ensure_dirs()
        self.config = config_mod.load(config)
        self.clock = clock or time.time
        self.poll_usage = poll_usage or (lambda: usage.poll(now=self.clock()))
        self.count_tokens = count_tokens or (lambda: usage.transcript_tokens(self.clock()))
        # Free, unlike the Claude poll: codex writes the server's own limit
        # block into its session logs, so this is a disk read on every tick.
        self.codex_estimate = codex_estimate or (
            lambda now: governor.codex.estimate(now))
        self.chat = chat if chat is not None else self._default_chat()
        self.run_step = run_step or self._default_run_step
        self.executor = executor or concurrent.futures.ThreadPoolExecutor(max_workers=4)
        self.freeform = freeform or self._default_freeform
        self.checkpoint = checkpoint or self._default_checkpoint
        self.running = {}
        self._force_poll = False
        self.notices = []

    # -- wiring defaults ---------------------------------------------------

    def _default_chat(self):
        token = config_mod.read_token()
        if not token:
            return chat_mod.NullChat()
        return chat_mod.Chat(token, self.config.get("chat_allowlist"))

    def _default_run_step(self, task, cwd):
        from . import worker
        return worker.run_step(task, cwd, self.config,
                               task_dir=config_mod.task_dir(task["id"]))

    def _default_checkpoint(self, task, cwd, message):
        from . import worker
        return worker.checkpoint(task, cwd, message)

    def _default_freeform(self, text):
        """Ask a small model to split free-form intake into tasks."""
        repos_text = ", ".join(sorted(repos.dispatchable(self.found_repos()))) or "none"
        try:
            completed = subprocess.run(
                ["claude", "-p", PARSE_PROMPT % (repos_text, text), "--output-format", "text"],
                capture_output=True, text=True, timeout=120)
        except Exception as exc:  # noqa: BLE001 - parsing must never kill the daemon
            return None, str(exc)
        body = (completed.stdout or "").strip()
        start, end = body.find("["), body.rfind("]")
        if start < 0 or end < start:
            return None, "no JSON array in parse output"
        try:
            items = json.loads(body[start:end + 1])
        except ValueError as exc:
            return None, str(exc)
        tasks = [i for i in items if isinstance(i, dict) and i.get("repo") and i.get("prompt")]
        return (tasks or None), (None if tasks else "no usable tasks")

    # -- helpers -----------------------------------------------------------

    def found_repos(self):
        return repos.discover(root=repos.root_path(self.config),
                              overrides=self.config.get("repos"))

    def repo_path(self, repo):
        """Resolve a repo name. Unknown or non-git names are refused."""
        return repos.resolve(repo, found=self.found_repos())

    def volume_block(self, now):
        """The free token-volume report, with a total failure made visible.

        ``volume`` skips a log it cannot read and carries on, which is right for
        the one-shot report it was ported from and wrong for a process that
        lives for weeks -- a corrupt file or a permissions problem would never
        surface anywhere. Threading a diagnostics channel back out of it is more
        than this reply needs, but the cheap half belongs here, at the one call
        site: if the report cannot be produced at all, say so rather than
        sending a usage message with a silent hole in it.
        """
        try:
            return volume.render(now)
        except Exception as exc:  # noqa: BLE001 - a bad log must not break `usage`
            return "volume report unavailable: %s" % exc

    def notify(self, text):
        self.notices.append(text)
        for chat_id in self.config.get("chat_allowlist") or []:
            self.chat.send(chat_id, text)

    def _reset_text(self, reading):
        resets = reading.get("resets_at")
        if not resets:
            return None
        return time.strftime("%-I:%M%p", time.localtime(resets)).lower()

    # -- the tick ----------------------------------------------------------

    def tick(self):
        now = self.clock()
        snapshot = state.read_state().get("governor") or governor.blank()
        tokens = self.count_tokens()

        # `hot` and `_force_poll` are Claude-only machinery: both shorten the
        # wait before we spend a request on `/usage`. Only Claude work moves the
        # Claude window, so only Claude work may shorten it.
        hot = bool(self._running_in(lanes.CLAUDE))
        if governor.should_poll(snapshot, now, hot=hot, force=self._force_poll,
                                config=self.config):
            snapshot = governor.record_poll(snapshot, self.poll_usage(), tokens)
            self._force_poll = False
            with state.mutate_state() as doc:
                doc["governor"] = snapshot

        readings = {
            lanes.CLAUDE: governor.estimate(snapshot, now, tokens),
            lanes.CODEX: self.codex_estimate(now),
        }

        # Settle anything that finished since the last tick, then decide each
        # lane's mode from the world as it is now.
        if lanes.CLAUDE in self._reap(snapshot):
            self._force_poll = True
        modes = self._apply_modes(snapshot, readings, now, tokens)

        self._intake(modes, readings, snapshot, now, tokens)
        for lane in lanes.ALL:
            if modes[lane] == winddown.RUNNING:
                self._dispatch(lane, readings[lane], now)

        # A step can also finish within this tick -- always with the inline
        # executor, occasionally with a fast real step. Settling it here rather
        # than a whole interval later keeps the queue moving.
        settled = self._reap(snapshot)
        if lanes.CLAUDE in settled:
            self._force_poll = True
        if settled:
            modes = self._apply_modes(snapshot, readings, now, tokens)

        return {"mode": modes, "readings": readings, "running": sorted(self.running)}

    def _apply_modes(self, snapshot, readings, now, tokens):
        """Advance each lane's wind-down machine and persist the result.

        The whole read-modify-write happens under the state lock and seeds from
        the live document, never from a copy taken at the top of the tick.
        Intake runs between the two calls to this method and writes these very
        fields -- `pause`/`resume` set a lane's mode, `usage poll` replaces the
        governor snapshot -- so a tick carrying its own copy forward would
        acknowledge a chat command and then silently undo it. On the only
        surface the user has, that is worse than refusing outright.

        Notices are buffered and sent after the lock is released: ``notify``
        reaches the network, and no chat round trip belongs inside a flock.
        """
        notices = []
        with state.mutate_state() as doc:
            modes = {}
            for lane in lanes.ALL:
                modes[lane] = self._apply_lane_mode(
                    doc, lane, snapshot, readings[lane], now, tokens, notices)
            doc["mode"] = modes
        for text in notices:
            self.notify(text)
        return modes

    def _apply_lane_mode(self, doc, lane, snapshot, reading, now, tokens, notices):
        """The same state machine as before, over one lane's tasks only."""
        running = self._running_in(lane)
        previous = doc["mode"][lane]
        summary = self._lane_summary(lane, doc.get("governor") or snapshot,
                                     reading, now, tokens)
        mode = winddown.next_mode(previous, reading["session_pct"], running,
                                  self.config, reading["stale"])

        if mode == winddown.FROZEN and previous != winddown.FROZEN:
            armed = winddown.resume_at(reading.get("resets_at"))
            doc["armed_resume_at"][lane] = armed
            notices.append("%s frozen at %s · %s" % (
                lane, summary,
                "resumes ~%s" % self._reset_text(reading) if armed else
                "resume not scheduled"))

        if mode == winddown.FROZEN:
            # can_resume predates per-lane modes and still expects the scalar
            # shape, so it is handed this lane's two fields rather than the
            # document they now live in.
            allowed, _ = winddown.can_resume(
                {"mode": mode, "armed_resume_at": doc["armed_resume_at"][lane]},
                reading["session_pct"], now, self.config, reading["stale"])
            if allowed:
                mode = winddown.RUNNING
                doc["armed_resume_at"][lane] = None
                notices.append("%s resumed · %s" % (lane, summary))
            elif (lane == lanes.CLAUDE
                  and doc["armed_resume_at"][lane]
                  and now >= doc["armed_resume_at"][lane]):
                # The timer fired but the window has not actually rolled over.
                # Confirm with a real poll next tick rather than trusting it.
                # Claude only: nothing a `/usage` call returns can refresh a
                # codex reading, and a frozen codex lane's percentage cannot
                # fall on its own, so this would re-arm forever and leak a
                # request per poll_floor on the lane designed to be free.
                self._force_poll = True
        return mode

    def _running_in(self, lane):
        return sum(1 for entry in self.running.values()
                   if lanes.of(entry["task"]) == lane)

    def _lane_summary(self, lane, snapshot, reading, now, tokens):
        """One line about a lane, from whichever governor owns it."""
        if lane == lanes.CLAUDE:
            return governor.summary(snapshot, now, tokens)
        parts = []
        if reading.get("session_pct") is not None:
            parts.append("5h %.0f%%" % reading["session_pct"])
        if reading.get("week_pct") is not None:
            parts.append("7d %.0f%%" % reading["week_pct"])
        parts.append(reading.get("source") or "unknown")
        return " · ".join(parts)

    # -- intake ------------------------------------------------------------

    def _intake(self, modes, readings, snapshot, now, tokens):
        state_doc = state.read_state()
        messages, next_offset = self.chat.poll(state_doc.get("chat_offset", 0))
        if next_offset != state_doc.get("chat_offset", 0):
            with state.mutate_state() as doc:
                doc["chat_offset"] = next_offset
        for message in messages:
            reply = self.handle_command(message["text"], modes, readings, snapshot,
                                        now, tokens)
            if reply:
                self.chat.send(message["chat_id"], reply)

    def handle_command(self, text, modes, readings, snapshot, now, tokens):
        command = parser.parse(text)
        kind = command["kind"]

        if kind == "usage":
            return self.usage_reply(command.get("poll"), snapshot, readings,
                                    now, tokens)
        if kind == "status":
            return self.status_line(modes, snapshot, readings, now, tokens)
        if kind == "queue":
            return self.queue_line()
        if kind == "sessions":
            return sessions.render(now, project=command.get("project"))
        if kind == "repos":
            return repos.render(self.found_repos())
        if kind == "help":
            return ("claude <task> on <repo> · codex <task> on <repo> · "
                    "status · queue · usage · usage poll · sessions · repos · "
                    "logs <id> · cancel <id> · retry <id> · "
                    "pause [lane] · resume [lane]")
        if kind in ("pause", "resume"):
            return self.set_mode(kind, command.get("lane"))
        if kind == "cancel":
            return self.cancel(command.get("id"))
        if kind == "logs":
            return self.logs(command.get("id"))
        if kind == "retry":
            return self.retry(command.get("id"))
        if kind == "need_repo":
            names = ", ".join(sorted(repos.dispatchable(self.found_repos()))) or "none"
            return "need a repo · try: %s <task> on <repo> · dispatchable: %s" % (
                command["agent"], names)
        if kind == "run":
            # Through lanes.of, so an agent name nothing recognizes lands in the
            # Claude lane rather than raising a KeyError on `modes`.
            lane = lanes.of(command)
            return self.enqueue(command["repo"], command["prompt"],
                                command.get("isolation", "repo"),
                                modes[lane], readings[lane], agent=lane)
        reply = self.enqueue_freeform(command.get("text", ""),
                                      modes[lanes.CLAUDE], readings[lanes.CLAUDE])
        # An empty reply is indistinguishable from a dropped message, and a
        # chat surface that sometimes says nothing is worse than one that says
        # something useless. Every kind the parser emits lands somewhere.
        return reply or "nothing to do · send `help` for what I understand"

    def set_mode(self, verb, lane):
        """Pause or resume one lane, or both when none is named."""
        targets = [lane] if lane else list(lanes.ALL)
        value = "paused" if verb == "pause" else winddown.RUNNING
        with state.mutate_state() as doc:
            for target in targets:
                doc["mode"][target] = value
                if verb == "resume":
                    doc["armed_resume_at"][target] = None
        if verb == "pause":
            return "paused %s · in-flight steps finish, nothing new starts" % (
                ", ".join(targets))
        return "resumed %s" % ", ".join(targets)

    def enqueue(self, repo, prompt, isolation, mode, reading, agent="claude"):
        found = self.found_repos()
        if repos.resolve(repo, found=found) is None:
            return repos.reject_reason(repo, found)
        with state.mutate_queue() as queue:
            task = state.new_task(queue, repo, prompt, isolation=isolation,
                                  agent=agent)
        return "%s · %s" % (parser.render_ack(task["id"], mode,
                                              self._reset_text(reading)), agent)

    def enqueue_freeform(self, text, mode, reading):
        """Free-form intake. Never lost, even when the parse itself is blocked."""
        if not text:
            return None
        if mode != winddown.RUNNING:
            with state.mutate_queue() as queue:
                task = state.new_task(queue, "?", text)
                task["state"] = "needs_parse"
            return "stored %s · %s · parsed after reset" % (task["id"], mode)
        tasks, error = self.freeform(text)
        if not tasks:
            with state.mutate_queue() as queue:
                task = state.new_task(queue, "?", text)
                task["state"] = "needs_parse"
                task["last_error"] = error
            return "stored %s · could not parse now (%s)" % (task["id"], error)
        ids = []
        for item in tasks:
            reply = self.enqueue(item["repo"], item["prompt"], "repo", mode, reading,
                                 agent=lanes.CLAUDE)
            if reply.startswith("queued"):
                ids.append(reply.split()[1])
            else:
                return reply
        return "queued %s · %s" % (", ".join(ids), mode)

    def parse_pending(self, mode, reading):
        """Re-parse anything stored while the window was exhausted."""
        pending = [t for t in state.read_queue()["tasks"] if t["state"] == "needs_parse"]
        for task in pending:
            tasks, error = self.freeform(task["prompt"])
            if not tasks:
                with state.mutate_queue() as queue:
                    state.find(queue, task["id"])["last_error"] = error
                continue
            for item in tasks:
                self.enqueue(item["repo"], item["prompt"], "repo", mode, reading,
                             agent=lanes.CLAUDE)
            with state.mutate_queue() as queue:
                state.find(queue, task["id"])["state"] = "parsed"
        return len(pending)

    # -- queue operations --------------------------------------------------

    def cancel(self, task_id):
        if not task_id:
            return "cancel needs a task id"
        with state.mutate_queue() as queue:
            task = state.find(queue, task_id)
            if task is None:
                return "no such task %s" % task_id
            if task["state"] == "running":
                task["state"] = "cancelling"
                return "%s cancelling · current step finishes first" % task_id
            task["state"] = "cancelled"
        return "%s cancelled" % task_id

    def retry(self, task_id):
        if not task_id:
            return "retry needs a task id"
        with state.mutate_queue() as queue:
            task = state.find(queue, task_id)
            if task is None:
                return "no such task %s" % task_id
            task["state"] = "queued"
            task["last_error"] = None
        return "%s requeued" % task_id

    def logs(self, task_id, limit=1200):
        if not task_id:
            return "logs needs a task id"
        path = os.path.join(config_mod.task_dir(task_id), "worker.log")
        try:
            with open(path) as fh:
                body = fh.read()
        except OSError:
            return "no log for %s yet" % task_id
        return body[-limit:] if body else "log for %s is empty" % task_id

    def usage_reply(self, poll, snapshot, readings, now, tokens):
        """Free by default. Only an explicit `usage poll` spends a request."""
        lines = []
        if poll:
            if not governor.should_poll(snapshot, now, force=True,
                                        config=self.config):
                lines.append("poll skipped · %ds floor since the last one"
                             % self.config["poll_floor"])
            else:
                reading = self.poll_usage()
                if reading.get("ok"):
                    snapshot = governor.record_poll(snapshot, reading, tokens)
                    with state.mutate_state() as doc:
                        doc["governor"] = snapshot
                    readings = dict(readings)
                    readings[lanes.CLAUDE] = governor.estimate(snapshot, now, tokens)
                    lines.append("polled · real numbers below")
                else:
                    lines.append("poll failed: %s" % reading.get("error"))

        claude = readings[lanes.CLAUDE]
        codex = readings[lanes.CODEX]
        lines.append("CLAUDE  %s" % self._pct_line(claude))
        polled_at = snapshot.get("polled_at")
        if polled_at:
            lines.append("        last real poll %dm ago" % ((now - polled_at) // 60))
        lines.append("CODEX   %s" % self._pct_line(codex))
        lines.append("")
        lines.append(self.volume_block(now))
        return "\n".join(lines)

    @staticmethod
    def _pct_line(reading):
        if reading.get("session_pct") is None:
            return "unknown"
        parts = ["session %.0f%%" % reading["session_pct"]]
        if reading.get("week_pct") is not None:
            parts.append("week %.0f%%" % reading["week_pct"])
        parts.append(reading.get("source") or "unknown")
        return " · ".join(parts)

    def status_line(self, modes, snapshot, readings, now, tokens):
        queue = state.read_queue()
        counts = {}
        for task in queue["tasks"]:
            counts[task["state"]] = counts.get(task["state"], 0) + 1
        lines = [
            "claude  %s · %s" % (modes[lanes.CLAUDE],
                                 self._pct_line(readings[lanes.CLAUDE])),
            "codex   %s · %s" % (modes[lanes.CODEX],
                                 self._pct_line(readings[lanes.CODEX])),
            "queue   %s" % (" ".join("%s %d" % kv for kv in sorted(counts.items()))
                            or "empty"),
        ]
        return "\n".join(lines)

    def queue_line(self, limit=10):
        tasks = state.read_queue()["tasks"]
        live = [t for t in tasks if t["state"] not in ("done", "cancelled", "parsed")]
        if not live:
            return "queue empty"
        rows = ["%s %-6s %-11s %s (%d steps)" % (
            t["id"], lanes.of(t), t["state"], t["repo"], t["steps_done"])
            for t in live[:limit]]
        if len(live) > limit:
            rows.append("... %d more" % (len(live) - limit))
        return "\n".join(rows)

    # -- dispatch and reap -------------------------------------------------

    def _dispatch(self, lane, reading, now):
        while True:
            queue = state.read_queue()
            state_doc = state.read_state()
            held = {entry["lock_name"] for entry in self.running.values()}
            ctx = {
                "mode": state_doc["mode"][lane],
                "queue": queue,
                "running": self._running_in(lane),
                "session_pct": reading["session_pct"],
                "week_pct": reading["week_pct"],
                "stale": reading["stale"],
                "config": self.config,
                # Shared across lanes on purpose: a codex worker and a claude
                # worker must never hold the same checkout at once.
                "lock_free": lambda name: name not in held,
                "est_cost_pct": 0.0,
            }
            candidate = None
            for task in scheduler.runnable(queue, lane):
                ctx["est_cost_pct"] = governor.est_cost_pct(state_doc, task["repo"], self.config)
                ok, _ = scheduler.admit(task, ctx)
                if ok:
                    candidate = task
                    break
            if candidate is None:
                return
            if not self._start(candidate, now):
                return

    def _start(self, task, now):
        cwd = self.repo_path(task["repo"])
        if cwd is None or not os.path.isdir(cwd):
            with state.mutate_queue() as queue:
                record = state.find(queue, task["id"])
                record["state"] = "blocked"
                record["last_error"] = "repo is not dispatchable or has gone missing"
            return True
        name = scheduler.lock_name(task)
        handle = state.try_lock(name)
        if handle is None:
            return False
        with state.mutate_queue() as queue:
            record = state.find(queue, task["id"])
            record["state"] = "running"
            snapshot = dict(record)
        future = self.executor.submit(self.run_step, snapshot, cwd)
        self.running[task["id"]] = {
            "future": future, "lock": handle, "lock_name": name,
            "cwd": cwd, "started_at": now, "task": snapshot,
        }
        return True

    def _reap(self, snapshot):
        """Collect finished steps. Returns the set of lanes that settled one.

        The caller needs the lanes, not the task ids: a Claude step ending is
        the moment the Claude estimate is least trustworthy and worth a poll,
        while a codex step ending changes nothing a free disk read will not.
        """
        settled = set()
        for task_id, entry in list(self.running.items()):
            if not entry["future"].done():
                continue
            settled.add(lanes.of(entry["task"]))
            self.running.pop(task_id, None)
            state.release(entry["lock"])
            try:
                result = entry["future"].result()
            except Exception as exc:  # noqa: BLE001 - a crashed step is a failed step
                result = {"status": None, "summary": "", "next": "",
                          "output": str(exc), "limit_reset_at": None,
                          "session_id": None, "timed_out": False}
            self._settle(task_id, entry, result, snapshot)
        return settled

    def _settle(self, task_id, entry, result, snapshot):
        task = entry["task"]
        lane = lanes.of(task)

        if result.get("limit_reset_at"):
            # Written straight through under the lock rather than staged on the
            # tick's copy: _apply_modes seeds from the live document, so a
            # freeze parked in a copy would simply not be there when it looked.
            with state.mutate_state() as doc:
                # Only the Claude governor carries an override; the codex lane
                # re-reads its own limits from disk next tick and self-corrects.
                if lane == lanes.CLAUDE:
                    doc["governor"] = governor.note_limit_error(
                        doc.get("governor") or snapshot, result["limit_reset_at"])
                doc["mode"][lane] = winddown.FROZEN
                doc["armed_resume_at"][lane] = winddown.resume_at(
                    result["limit_reset_at"])

        # After the freeze above and after intake, so a step that finished into
        # a lane the user just paused is paused rather than requeued.
        mode = state.read_state()["mode"][lane]
        summary = (result.get("summary") or "").strip()
        message = "%s step %d: %s" % (task_id, task.get("steps_done", 0) + 1,
                                      summary or "checkpoint")
        self.checkpoint(task, entry["cwd"], message)

        directory = config_mod.task_dir(task_id)
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "worker.log"), "a") as fh:
            fh.write(result.get("output") or "")
            fh.write("\n")
        with open(os.path.join(directory, "steps.jsonl"), "a") as fh:
            fh.write(json.dumps({
                "at": self.clock(), "status": result.get("status"),
                "summary": summary, "next": result.get("next"),
                "timed_out": result.get("timed_out"),
            }) + "\n")

        with state.mutate_queue() as queue:
            record = state.find(queue, task_id)
            if record is None:
                return
            record["steps_done"] = record.get("steps_done", 0) + 1
            if result.get("session_id"):
                record["session_id"] = result["session_id"]
            if record["state"] == "cancelling":
                record["state"] = "cancelled"
            elif result.get("limit_reset_at"):
                record["state"] = "paused"
                record["last_error"] = "usage limit"
            else:
                record["state"] = worker_next_state(result.get("status"), mode)
                if record["state"] == "failed":
                    record["last_error"] = "no status block from worker"
            settled = dict(record)

        if settled["state"] in ("paused", "blocked", "failed"):
            from . import worker as worker_mod
            worker_mod.write_handoff(directory, settled, result)
        if settled["state"] in ("done", "blocked", "failed", "cancelled"):
            self.notify("%s [%s] %s · %s" % (task_id, lane, settled["state"],
                                             summary or "-"))

    # -- run loop ----------------------------------------------------------

    def run(self, interval=5, ticks=None, sleeper=None):
        sleeper = sleeper or time.sleep
        count = 0
        while ticks is None or count < ticks:
            self.tick()
            count += 1
            if ticks is not None and count >= ticks:
                break
            sleeper(interval)


def worker_next_state(status, mode):
    from . import worker
    return worker.next_state(status, mode)
