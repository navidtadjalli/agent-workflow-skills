#!/usr/bin/env python3
"""End-to-end dispatch pass against stub agent CLIs.

A fake `claude` and a fake `codex` on PATH stand in for the real ones, so a full
tick -- admit, run a step, parse the status block, checkpoint to the task
branch, settle the task -- runs with no API call and no real usage. The clock,
both plan readings, and the chat transport are injected; only the subprocess and
git are real. Each stub honours its own contract: `claude` takes the prompt on
stdin and emits a fenced status block, `codex` writes its status JSON where `-o`
points and announces its thread id on the event stream.
"""
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "skills", "dispatch"))

from dispatch import chat as chat_mod  # noqa: E402
from dispatch import config as config_mod  # noqa: E402
from dispatch import daemon as daemon_mod  # noqa: E402
from dispatch import lanes  # noqa: E402
from dispatch import state  # noqa: E402

STUB = """#!/usr/bin/env python3
import json, os, sys
mode = os.environ.get("STUB_MODE", "complete")
prompt = sys.stdin.read()
if mode == "limit":
    print("Claude AI usage limit reached|%s" % os.environ["STUB_RESET"])
    sys.exit(1)
open(os.path.join(os.environ["STUB_REPO"], "worked.txt"), "a").write("step\\n")
body = "prompt-was: %s\\n```json\\n%s\\n```" % (prompt.strip(), json.dumps(
    {"status": mode, "summary": "stub step", "next": "more"}))
print(json.dumps({"session_id": "sess-stub", "result": body}))
"""

CODEX_STUB = """#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
prompt = sys.stdin.read()
out = argv[argv.index("-o") + 1]
mode = os.environ.get("STUB_MODE", "complete")
open(os.path.join(os.environ["STUB_REPO"], "worked.txt"), "a").write("codex\\n")
with open(out, "w") as fh:
    json.dump({"status": mode, "summary": "codex stub", "next": ""}, fh)
print(json.dumps({"type": "thread.started", "thread_id": "codex-stub"}))
print(json.dumps({"type": "item.done", "prompt_len": len(prompt)}))
"""


class DispatchIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self.home = os.path.join(root, "dispatch-home")
        self.repo = os.path.join(root, "demo-repo")
        # A second checkout, so a lane blocked on `demo` has somewhere else to
        # go and "did the lane stall?" is answerable rather than assumed.
        self.repo2 = os.path.join(root, "other-repo")
        bin_dir = os.path.join(root, "bin")
        os.makedirs(bin_dir)
        os.makedirs(self.repo)
        os.makedirs(self.repo2)
        # Repo discovery points here, so it never enumerates the real ~/Projects.
        os.makedirs(os.path.join(root, "empty-root"))

        for name, body in (("claude", STUB), ("codex", CODEX_STUB)):
            stub_path = os.path.join(bin_dir, name)
            with open(stub_path, "w") as fh:
                fh.write(body)
            os.chmod(stub_path, 0o755)

        self._env = dict(os.environ)
        os.environ.update({
            "DISPATCH_HOME": self.home,
            "PATH": bin_dir + os.pathsep + os.environ["PATH"],
            "STUB_REPO": self.repo,
            "STUB_MODE": "complete",
            "GIT_AUTHOR_NAME": "dispatch test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "dispatch test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
            # Never let a test read the real channel token.
            "DISPATCH_TOKEN_ENV": os.path.join(root, "absent.env"),
        })

        for repo in (self.repo, self.repo2):
            self._git("init", "-q", cwd=repo)
            with open(os.path.join(repo, "README.md"), "w") as fh:
                fh.write("fixture\n")
            self._git("add", "-A", cwd=repo)
            self._git("commit", "-qm", "initial", cwd=repo)
        self.now = 1_776_000_000.0

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()

    def _git(self, *args, cwd=None):
        return subprocess.run(["git"] + list(args), cwd=cwd or self.repo,
                              capture_output=True, text=True, check=False)

    def _daemon(self, session_pct=10.0, codex_pct=5.0, **over):
        reading = {"ok": True, "at": self.now, "session_pct": session_pct,
                   "session_reset": self.now + 7200, "week_pct": 5.0,
                   "week_reset": self.now + 500000}
        reading.update(over.pop("reading", {}))
        codex_reading = {"session_pct": codex_pct, "week_pct": 0.0,
                         "source": "codex-logs", "stale": False, "resets_at": None}
        codex_reading.update(over.pop("codex_reading", {}))
        return daemon_mod.Daemon(
            # An empty projects root, so discovery never sees the real ~/Projects.
            config={"projects_root": os.path.join(self.tmp.name, "empty-root"),
                    "repos": {"demo": self.repo, "other": self.repo2},
                    "chat_allowlist": []},
            clock=lambda: self.now,
            poll_usage=lambda: reading,
            count_tokens=lambda: 0,
            codex_estimate=lambda now: codex_reading,
            chat=chat_mod.NullChat(),
            executor=daemon_mod.InlineExecutor(),
            **over)

    def _enqueue(self, prompt="do the thing", agent="claude", repo="demo"):
        with state.mutate_queue() as queue:
            return state.new_task(queue, repo, prompt, agent=agent)

    def _modes(self, claude="running", codex="running"):
        return {lanes.CLAUDE: claude, lanes.CODEX: codex}

    def _readings(self, claude_pct=10.0, codex_pct=5.0, resets_at=None):
        return {lanes.CLAUDE: {"session_pct": claude_pct, "week_pct": 5.0,
                               "stale": False, "source": "measured",
                               "resets_at": resets_at},
                lanes.CODEX: {"session_pct": codex_pct, "week_pct": 0.0,
                              "stale": False, "source": "codex-logs",
                              "resets_at": None}}

    def test_step_runs_checkpoints_and_completes(self):
        task = self._enqueue()
        result = self._daemon().tick()

        self.assertEqual(result["mode"][lanes.CLAUDE], "running")
        settled = state.find(state.read_queue(), task["id"])
        self.assertEqual(settled["state"], "done")
        self.assertEqual(settled["steps_done"], 1)
        self.assertEqual(settled["session_id"], "sess-stub")

        branches = self._git("branch", "--list", "tg/t-0001").stdout
        self.assertIn("tg/t-0001", branches)
        log = self._git("log", "--oneline", "tg/t-0001").stdout
        self.assertIn("t-0001 step 1", log)

        steps = os.path.join(config_mod.task_dir(task["id"]), "steps.jsonl")
        self.assertTrue(os.path.exists(steps))
        with open(steps) as fh:
            self.assertEqual(len(fh.read().strip().splitlines()), 1)

    def test_continue_requeues_for_another_step(self):
        os.environ["STUB_MODE"] = "continue"
        task = self._enqueue()
        daemon = self._daemon()
        daemon.tick()
        self.assertEqual(state.find(state.read_queue(), task["id"])["state"], "queued")

        # A second tick picks it back up and runs another step.
        self.now += 120
        daemon.tick()
        self.assertEqual(state.find(state.read_queue(), task["id"])["steps_done"], 2)

    def test_repo_lock_serializes_two_tasks_in_one_repo(self):
        os.environ["STUB_MODE"] = "continue"
        self._enqueue("first")
        self._enqueue("second")
        daemon = self._daemon()

        started = []
        original = daemon.run_step

        def watched(task, cwd):
            started.append(task["id"])
            return original(task, cwd)

        daemon.run_step = watched
        daemon.tick()
        # Both are admissible on usage; only one may hold the repo lock.
        self.assertEqual(started, ["t-0001"])

    def test_soft_limit_stops_new_dispatch(self):
        task = self._enqueue()
        result = self._daemon(session_pct=90.0).tick()
        self.assertEqual(result["mode"][lanes.CLAUDE], "frozen")
        self.assertEqual(state.find(state.read_queue(), task["id"])["state"], "queued")
        self.assertIsNotNone(state.read_state()["armed_resume_at"][lanes.CLAUDE])

    def test_headroom_refuses_a_step_that_would_cross_the_soft_limit(self):
        task = self._enqueue()
        # 82% + the 6% default step estimate lands past the 85% soft limit,
        # while staying below it, so the mode is still running.
        result = self._daemon(session_pct=82.0).tick()
        self.assertEqual(result["mode"][lanes.CLAUDE], "running")
        self.assertEqual(state.find(state.read_queue(), task["id"])["state"], "queued")

    def test_usage_limit_error_freezes_and_pauses_the_task(self):
        os.environ["STUB_MODE"] = "limit"
        os.environ["STUB_RESET"] = str(int(self.now + 3600))
        task = self._enqueue()
        self._daemon().tick()

        settled = state.find(state.read_queue(), task["id"])
        self.assertEqual(settled["state"], "paused")
        self.assertEqual(settled["last_error"], "usage limit")

        doc = state.read_state()
        self.assertEqual(doc["mode"][lanes.CLAUDE], "frozen")
        self.assertEqual(doc["governor"]["session_pct"], 100.0)
        self.assertEqual(doc["armed_resume_at"][lanes.CLAUDE], self.now + 3600 + 60)
        self.assertTrue(os.path.exists(
            os.path.join(config_mod.task_dir(task["id"]), "handoff.md")))

    def test_frozen_resumes_only_after_a_confirmed_reset(self):
        os.environ["STUB_MODE"] = "limit"
        os.environ["STUB_RESET"] = str(int(self.now + 3600))
        task = self._enqueue()
        self._daemon().tick()
        self.assertEqual(state.read_state()["mode"][lanes.CLAUDE], "frozen")

        # Timer time arrives and a fresh reading confirms the window rolled over.
        self.now += 3700
        os.environ["STUB_MODE"] = "complete"
        daemon = self._daemon(session_pct=4.0)
        daemon.tick()

        self.assertEqual(state.read_state()["mode"][lanes.CLAUDE], "running")
        self.assertEqual(state.find(state.read_queue(), task["id"])["state"], "done")

    def test_unknown_repo_is_refused_not_guessed(self):
        daemon = self._daemon()
        reply = daemon.handle_command("run something on nowhere", self._modes(),
                                      self._readings(),
                                      daemon_mod.governor.blank(), self.now, 0)
        self.assertIn("unknown repo", reply)
        self.assertEqual(state.read_queue()["tasks"], [])

    def test_chat_run_command_enqueues(self):
        daemon = self._daemon()
        reply = daemon.handle_command("run the migration on demo", self._modes(),
                                      self._readings(),
                                      daemon_mod.governor.blank(), self.now, 0)
        self.assertTrue(reply.startswith("queued t-0001"))
        self.assertEqual(state.read_queue()["tasks"][0]["prompt"], "the migration")

    def test_freeform_while_frozen_is_stored_not_lost(self):
        daemon = self._daemon()
        reply = daemon.handle_command("please tidy up the deps everywhere",
                                      self._modes(claude="frozen"),
                                      self._readings(claude_pct=99.0,
                                                     resets_at=self.now + 60),
                                      daemon_mod.governor.blank(), self.now, 0)
        self.assertIn("stored", reply)
        self.assertEqual(state.read_queue()["tasks"][0]["state"], "needs_parse")

    def test_prompt_reaches_the_agent_on_stdin(self):
        """End-to-end proof that the prompt file is plumbed to stdin."""
        task = self._enqueue("plant this exact phrase")
        self._daemon().tick()
        with open(os.path.join(config_mod.task_dir(task["id"]), "worker.log")) as fh:
            log = fh.read()
        self.assertIn("prompt-was: plant this exact phrase", log)
        prompt_file = os.path.join(config_mod.task_dir(task["id"]), "prompt.txt")
        self.assertTrue(os.path.exists(prompt_file))
        with open(prompt_file) as fh:
            self.assertIn("Never push", fh.read())

    def test_codex_task_runs_through_its_own_backend(self):
        task = self._enqueue("codex work", agent="codex")
        self._daemon().tick()
        settled = state.find(state.read_queue(), task["id"])
        self.assertEqual(settled["state"], "done")
        self.assertEqual(settled["session_id"], "codex-stub")
        self.assertIn("tg/t-0001", self._git("branch", "--list", "tg/t-0001").stdout)

    def test_codex_lane_runs_while_the_claude_lane_is_frozen(self):
        self._enqueue("claude work", agent="claude")
        self._enqueue("codex work", agent="codex")
        result = self._daemon(session_pct=99.0, codex_pct=5.0).tick()
        tasks = {t["id"]: t for t in state.read_queue()["tasks"]}
        self.assertEqual(tasks["t-0002"]["state"], "done")
        self.assertIn(tasks["t-0001"]["state"], ("queued", "paused"))
        self.assertIn(result["mode"]["claude"], ("winding-down", "frozen"))
        self.assertEqual(result["mode"]["codex"], "running")

    def test_lanes_contend_for_one_repo(self):
        """A claude worker and a codex worker must not share a checkout.

        The third task is what makes this a test rather than a tautology.
        `flock` alone would produce `started == ["t-0001"]` even with a per-lane
        lock set, because a second `LOCK_EX|LOCK_NB` on a fresh descriptor is
        denied within one process too. What the shared set actually buys is that
        `t-0002` is refused at *admission* and stepped over, so `t-0003` in
        another checkout still gets its turn -- rather than reaching `_start`,
        failing the flock, and ending the lane's dispatch pass.
        """
        os.environ["STUB_MODE"] = "continue"
        self._enqueue("first", agent="claude")
        self._enqueue("second", agent="codex")
        self._enqueue("third", agent="codex", repo="other")
        daemon = self._daemon()
        started = []
        original = daemon.run_step

        def watched(task, cwd):
            started.append(task["id"])
            return original(task, cwd)

        daemon.run_step = watched
        daemon.tick()
        self.assertEqual(started, ["t-0001", "t-0003"])

    def test_codex_admitted_on_a_reading_whose_window_has_not_reset(self):
        """A pending `resets_at` is not a reason to hold the lane back.

        Deliberately not a staleness test: `governor.codex` guarantees `stale`
        is always False, because a codex percentage is only as fresh as the last
        codex run and the only way to improve it is to run codex. Injecting
        `stale: True` here would assert against that guarantee rather than for
        it, so what is pinned is the neighbouring gate -- a future reset
        timestamp passes admission untouched.
        """
        task = self._enqueue("codex work", agent="codex")
        daemon = self._daemon(codex_reading={
            "session_pct": 20.0, "week_pct": 0.0, "source": "codex-logs",
            "stale": False, "resets_at": self.now + 60})
        daemon.tick()
        self.assertEqual(state.find(state.read_queue(), task["id"])["state"], "done")

    def test_no_codex_reading_at_all_still_admits(self):
        task = self._enqueue("codex work", agent="codex")
        daemon = self._daemon(codex_reading={
            "session_pct": 0.0, "week_pct": None, "source": "codex-unknown",
            "stale": False, "resets_at": None})
        daemon.tick()
        self.assertEqual(state.find(state.read_queue(), task["id"])["state"], "done")

    def test_cli_status_and_queue_run_against_the_same_state(self):
        self._enqueue()
        cli = os.path.join(ROOT, "skills", "dispatch", "scripts", "dispatch")
        out = subprocess.run([sys.executable, cli, "queue"], capture_output=True,
                             text=True, env=os.environ)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("t-0001", out.stdout)

        out = subprocess.run([sys.executable, cli, "status"], capture_output=True,
                             text=True, env=os.environ)
        self.assertIn("mode: running", out.stdout)

    def test_setup_refuses_while_the_chat_plugin_owns_the_bot(self):
        settings = os.path.join(self.tmp.name, "settings.json")
        with open(settings, "w") as fh:
            fh.write('{"enabledPlugins": {"telegram@claude-plugins-official": true}}')
        cli = os.path.join(ROOT, "skills", "dispatch", "scripts", "dispatch")
        out = subprocess.run(
            [sys.executable, cli, "setup", "--settings", settings,
             "--repo", "demo=" + self.repo],
            capture_output=True, text=True, env=os.environ)
        self.assertEqual(out.returncode, 3)
        self.assertIn("refusing", out.stderr)
        self.assertFalse(os.path.exists(config_mod.config_path()))


if __name__ == "__main__":
    unittest.main(verbosity=1)
