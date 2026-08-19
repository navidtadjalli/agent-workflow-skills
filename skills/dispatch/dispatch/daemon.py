"""The daemon: one tick, repeated.

Everything expensive or non-deterministic is injected -- clock, usage poll,
token counter, chat transport, step runner, executor -- so the whole control
loop can be driven step by step in a test with no network, no subprocesses, and
no real time passing.

The loop never blocks on a task. A repo whose lock is held is simply not
admissible this tick; a step that is still running is left alone. That is what
lets intake keep answering while the plan window is exhausted.
"""
import concurrent.futures
import json
import os
import subprocess
import time

from . import chat as chat_mod
from . import config as config_mod
from . import governor, parser, scheduler, state, usage, winddown

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
                 checkpoint=None):
        config_mod.ensure_dirs()
        self.config = config_mod.load(config)
        self.clock = clock or time.time
        self.poll_usage = poll_usage or (lambda: usage.poll(now=self.clock()))
        self.count_tokens = count_tokens or (lambda: usage.transcript_tokens(self.clock()))
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
        return worker.run_step(task, cwd, self.config)

    def _default_checkpoint(self, task, cwd, message):
        from . import worker
        return worker.checkpoint(task, cwd, message)

    def _default_freeform(self, text):
        """Ask a small model to split free-form intake into tasks."""
        repos = ", ".join(sorted((self.config.get("repos") or {}).keys())) or "none configured"
        try:
            completed = subprocess.run(
                ["claude", "-p", PARSE_PROMPT % (repos, text), "--output-format", "text"],
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

    def repo_path(self, repo):
        """Resolve a repo alias. Unknown aliases are refused, not guessed."""
        return (self.config.get("repos") or {}).get(repo)

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
        state_doc = state.read_state()
        snapshot = state_doc.get("governor") or governor.blank()
        tokens = self.count_tokens()

        hot = bool(self.running)
        if governor.should_poll(snapshot, now, hot=hot, force=self._force_poll,
                                config=self.config):
            snapshot = governor.record_poll(snapshot, self.poll_usage(), tokens)
            self._force_poll = False

        reading = governor.estimate(snapshot, now, tokens)
        # From here on the document is the single authority: a limit error
        # discovered while settling a step must not be overwritten by the
        # pre-settle snapshot.
        state_doc["governor"] = snapshot

        # Settle anything that finished since the last tick, then decide the mode
        # from the world as it is now.
        if self._reap(state_doc, snapshot, reading):
            self._force_poll = True
        mode = self._apply_mode(state_doc, snapshot, reading, now, tokens)

        self._intake(mode, reading, snapshot, now, tokens)
        if mode == winddown.RUNNING:
            self._dispatch(reading, now)

        # A step can also finish within this tick -- always with the inline
        # executor, occasionally with a fast real step. Settling it here rather
        # than a whole interval later keeps the queue moving.
        if self._reap(state_doc, snapshot, reading):
            self._force_poll = True
            mode = self._apply_mode(state_doc, snapshot, reading, now, tokens)

        return {"mode": mode, "reading": reading, "running": sorted(self.running)}

    def _apply_mode(self, state_doc, snapshot, reading, now, tokens):
        """Advance the wind-down state machine and persist the result."""
        mode = winddown.next_mode(state_doc.get("mode", winddown.RUNNING),
                                  reading["session_pct"], len(self.running),
                                  self.config, reading["stale"])

        if mode == winddown.FROZEN and state_doc.get("mode") != winddown.FROZEN:
            armed = winddown.resume_at(reading.get("resets_at"))
            state_doc["armed_resume_at"] = armed
            self.notify("frozen at %s · %s" % (
                governor.summary(snapshot, now, tokens),
                "resumes ~%s" % self._reset_text(reading) if armed else "resume not scheduled"))

        if mode == winddown.FROZEN:
            allowed, _ = winddown.can_resume(
                dict(state_doc, mode=mode), reading["session_pct"], now,
                self.config, reading["stale"])
            if allowed:
                mode = winddown.RUNNING
                state_doc["armed_resume_at"] = None
                self.notify("resumed · %s" % governor.summary(snapshot, now, tokens))
            elif state_doc.get("armed_resume_at") and now >= state_doc["armed_resume_at"]:
                # The timer fired but the window has not actually rolled over.
                # Confirm with a real poll next tick rather than trusting it.
                self._force_poll = True

        state_doc["mode"] = mode
        state.write(config_mod.state_path(), state_doc)
        return mode

    # -- intake ------------------------------------------------------------

    def _intake(self, mode, reading, snapshot, now, tokens):
        state_doc = state.read_state()
        messages, next_offset = self.chat.poll(state_doc.get("chat_offset", 0))
        if next_offset != state_doc.get("chat_offset", 0):
            with state.mutate_state() as doc:
                doc["chat_offset"] = next_offset
        for message in messages:
            reply = self.handle_command(message["text"], mode, reading, snapshot,
                                        now, tokens)
            if reply:
                self.chat.send(message["chat_id"], reply)

    def handle_command(self, text, mode, reading, snapshot, now, tokens):
        command = parser.parse(text)
        kind = command["kind"]

        if kind == "usage":
            return governor.summary(snapshot, now, tokens)
        if kind == "status":
            return self.status_line(mode, snapshot, now, tokens)
        if kind == "queue":
            return self.queue_line()
        if kind == "help":
            return ("run <task> on <repo> · status · queue · usage · "
                    "logs <id> · cancel <id> · pause · resume")
        if kind == "pause":
            with state.mutate_state() as doc:
                doc["mode"] = "paused"
            return "paused · in-flight step finishes, nothing new starts"
        if kind == "resume":
            with state.mutate_state() as doc:
                doc["mode"] = winddown.RUNNING
                doc["armed_resume_at"] = None
            return "resumed"
        if kind == "cancel":
            return self.cancel(command.get("id"))
        if kind == "logs":
            return self.logs(command.get("id"))
        if kind == "retry":
            return self.retry(command.get("id"))
        if kind == "run":
            return self.enqueue(command["repo"], command["prompt"],
                                command.get("isolation", "repo"), mode, reading)
        return self.enqueue_freeform(command.get("text", ""), mode, reading)

    def enqueue(self, repo, prompt, isolation, mode, reading):
        if self.repo_path(repo) is None:
            known = ", ".join(sorted((self.config.get("repos") or {}).keys())) or "none"
            return "unknown repo '%s' · configured: %s" % (repo, known)
        with state.mutate_queue() as queue:
            task = state.new_task(queue, repo, prompt, isolation=isolation)
        return parser.render_ack(task["id"], mode, self._reset_text(reading))

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
            reply = self.enqueue(item["repo"], item["prompt"], "repo", mode, reading)
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
                self.enqueue(item["repo"], item["prompt"], "repo", mode, reading)
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

    def status_line(self, mode, snapshot, now, tokens):
        queue = state.read_queue()
        counts = {}
        for task in queue["tasks"]:
            counts[task["state"]] = counts.get(task["state"], 0) + 1
        parts = [mode, governor.summary(snapshot, now, tokens)]
        if counts:
            parts.append(" ".join("%s %d" % kv for kv in sorted(counts.items())))
        else:
            parts.append("queue empty")
        return " · ".join(parts)

    def queue_line(self, limit=10):
        tasks = state.read_queue()["tasks"]
        live = [t for t in tasks if t["state"] not in ("done", "cancelled", "parsed")]
        if not live:
            return "queue empty"
        rows = ["%s %s %s (%d steps)" % (t["id"], t["state"], t["repo"], t["steps_done"])
                for t in live[:limit]]
        if len(live) > limit:
            rows.append("... %d more" % (len(live) - limit))
        return "\n".join(rows)

    # -- dispatch and reap -------------------------------------------------

    def _dispatch(self, reading, now):
        while True:
            queue = state.read_queue()
            state_doc = state.read_state()
            ctx = {
                "mode": state_doc.get("mode", winddown.RUNNING),
                "queue": queue,
                "running": len(self.running),
                "session_pct": reading["session_pct"],
                "week_pct": reading["week_pct"],
                "stale": reading["stale"],
                "config": self.config,
                "lock_free": lambda name: name not in
                {entry["lock_name"] for entry in self.running.values()},
                "est_cost_pct": 0.0,
            }
            candidate = None
            for task in scheduler.runnable(queue):
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
                record["last_error"] = "repo path not configured or missing"
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

    def _reap(self, state_doc, snapshot, reading):
        """Collect finished steps and decide each task's next state."""
        finished = []
        for task_id, entry in list(self.running.items()):
            if not entry["future"].done():
                continue
            finished.append(task_id)
            self.running.pop(task_id, None)
            state.release(entry["lock"])
            try:
                result = entry["future"].result()
            except Exception as exc:  # noqa: BLE001 - a crashed step is a failed step
                result = {"status": None, "summary": "", "next": "",
                          "output": str(exc), "limit_reset_at": None,
                          "session_id": None, "timed_out": False}
            self._settle(task_id, entry, result, state_doc, snapshot, reading)
        return finished

    def _settle(self, task_id, entry, result, state_doc, snapshot, reading):
        task = entry["task"]
        mode = state_doc.get("mode", winddown.RUNNING)

        if result.get("limit_reset_at"):
            state_doc["governor"] = governor.note_limit_error(
                state_doc.get("governor") or snapshot, result["limit_reset_at"])
            state_doc["mode"] = winddown.FROZEN
            state_doc["armed_resume_at"] = winddown.resume_at(result["limit_reset_at"])

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
                record["state"] = worker_next_state(result.get("status"),
                                                    state_doc.get("mode", mode))
                if record["state"] == "failed":
                    record["last_error"] = "no status block from worker"
            settled = dict(record)

        if settled["state"] in ("paused", "blocked", "failed"):
            from . import worker as worker_mod
            worker_mod.write_handoff(directory, settled, result)
        if settled["state"] in ("done", "blocked", "failed", "cancelled"):
            self.notify("%s %s · %s" % (task_id, settled["state"], summary or "-"))

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
