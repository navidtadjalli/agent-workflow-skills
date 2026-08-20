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
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(ROOT, "skills", "dispatch", "scripts", "dispatch")
sys.path.insert(0, os.path.join(ROOT, "skills", "dispatch"))

from dispatch import chat as chat_mod  # noqa: E402
from dispatch import config as config_mod  # noqa: E402
from dispatch import daemon as daemon_mod  # noqa: E402
from dispatch import lanes  # noqa: E402
from dispatch import state  # noqa: E402

# The one chat the fixture daemon serves. Nothing reaches Telegram -- the
# transport is a NullChat -- but the allowlist has to name somebody, because
# an empty one is now a refusal to serve chat at all.
CHAT_ID = "4242"

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
# What the daemon really launched, for the tests that assert on confinement.
# There is no other way to see it: the flags are chosen deep inside run_step.
open(os.environ["STUB_ARGV"], "a").write(json.dumps(argv) + "\\n")
prompt = sys.stdin.read()
out = argv[argv.index("-o") + 1]
mode = os.environ.get("STUB_MODE", "complete")
if mode == "die":
    # Killed at the step timeout, or crashed: no status file is written.
    sys.stderr.write("codex stub died\\n")
    sys.exit(3)
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

        self.codex_argv = os.path.join(root, "codex-argv.jsonl")
        self._env = dict(os.environ)
        os.environ.update({
            "DISPATCH_HOME": self.home,
            "PATH": bin_dir + os.pathsep + os.environ["PATH"],
            "STUB_REPO": self.repo,
            "STUB_MODE": "complete",
            "STUB_ARGV": self.codex_argv,
            "GIT_AUTHOR_NAME": "dispatch test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "dispatch test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
            # Never let a test read the real channel token.
            "DISPATCH_TOKEN_ENV": os.path.join(root, "absent.env"),
            # The daemon is handed its readings; a CLI subprocess is not, so
            # the roots themselves point here rather than at the real
            # ~/.claude/projects and ~/.codex/sessions.
            "DISPATCH_TRANSCRIPTS": os.path.join(root, "transcripts"),
            "DISPATCH_CODEX_SESSIONS": os.path.join(root, "codex-sessions"),
        })

        # On disk as well as injected: a CLI subprocess gets no in-memory
        # config, and without this `dispatch add` would discover the
        # developer's real ~/Projects.
        # A chat id, because an empty allowlist now means nobody: the daemon
        # refuses to build a transport for it, and a fixture that ships one
        # would be testing a configuration no deployment may run in.
        state.write(config_mod.config_path(), {
            "projects_root": os.path.join(root, "empty-root"),
            "repos": {"demo": self.repo, "other": self.repo2},
            "chat_allowlist": [CHAT_ID]})

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
        # An empty projects root, so discovery never sees the real ~/Projects.
        config = {"projects_root": os.path.join(self.tmp.name, "empty-root"),
                  "repos": {"demo": self.repo, "other": self.repo2},
                  "chat_allowlist": [CHAT_ID]}
        config.update(over.pop("config_extra", {}))
        return daemon_mod.Daemon(
            config=config,
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

    def test_a_dead_codex_step_does_not_settle_as_the_previous_one(self):
        """`-o` writes the status file only when codex finishes. Nothing
        cleared it between steps, so a step that died read the previous step's
        block and settled as its success -- another `steps_done`, another
        checkpoint commit, and a summary describing work it never did."""
        os.environ["STUB_MODE"] = "continue"
        task = self._enqueue("codex work", agent="codex")
        daemon = self._daemon()
        daemon.tick()
        self.assertEqual(state.find(state.read_queue(), task["id"])["state"],
                         "queued")

        os.environ["STUB_MODE"] = "die"
        self.now += 120
        daemon.tick()
        settled = state.find(state.read_queue(), task["id"])
        self.assertEqual(settled["state"], "failed")
        self.assertEqual(settled["last_error"], "no status block from worker")
        self.assertFalse(os.path.exists(
            os.path.join(config_mod.task_dir(task["id"]), "last.json")))

    def test_a_checkpoint_that_cannot_run_fails_the_task(self):
        """A `tg` branch blocks `tg/<id>`, so `checkout -B` really fails.

        The work is uncommitted; reporting `done` would point chat and
        `handoff.md` at a branch with nothing on it."""
        self._git("branch", "tg", cwd=self.repo2)
        task = self._enqueue("work", repo="other")
        daemon = self._daemon()
        daemon.tick()

        settled = state.find(state.read_queue(), task["id"])
        self.assertEqual(settled["state"], "failed")
        self.assertIn("checkpoint failed", settled["last_error"])
        self.assertTrue(any("not committed" in notice for notice in daemon.notices),
                        daemon.notices)
        directory = config_mod.task_dir(task["id"])
        with open(os.path.join(directory, "steps.jsonl")) as fh:
            step = json.loads(fh.read().strip())
        self.assertTrue(step["checkpoint_error"])
        self.assertTrue(os.path.exists(os.path.join(directory, "handoff.md")))

    def _codex_argv(self):
        with open(self.codex_argv) as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def test_a_codex_step_is_launched_inside_the_sandbox(self):
        """Asserted on the real launch: the flag is chosen inside `run_step`,
        so a unit test of `build_command` alone would not prove it arrives.

        Against the stub, never a real codex -- the weekly window is full, and
        this mode is unverified against the live tool for that reason."""
        self._enqueue(agent="codex")
        self._daemon().tick()
        argv = self._codex_argv()[0]
        self.assertIn("--approve-for-me", argv)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)

    def test_the_bypass_is_one_config_value_away(self):
        """If the reviewed mode stalls on approvals in real use, this is the
        recovery: an edit to config.json and a restart, not a code change."""
        self._enqueue(agent="codex")
        self._daemon(config_extra={"codex_sandbox": "bypass"}).tick()
        argv = self._codex_argv()[0]
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertNotIn("--approve-for-me", argv)

    def test_the_cli_queues_against_the_same_repos_the_daemon_accepts(self):
        """`dispatch add` is the whole fallback when Telegram is down."""
        out = subprocess.run([sys.executable, CLI, "add", "demo", "do the thing",
                              "--agent", "codex"],
                             capture_output=True, text=True, env=os.environ)
        self.assertEqual(out.returncode, 0, out.stderr)
        task = state.read_queue()["tasks"][0]
        self.assertEqual((task["repo"], task["agent"]), ("demo", lanes.CODEX))
        self.assertIn(task["id"], out.stdout)

        refused = subprocess.run([sys.executable, CLI, "add", "nowhere", "x"],
                                 capture_output=True, text=True, env=os.environ)
        self.assertEqual(refused.returncode, 2)
        self.assertIn("unknown repo", refused.stderr)
        self.assertIn("demo", refused.stderr)

    def test_cli_status_and_queue_run_against_the_same_state(self):
        self._enqueue()
        out = subprocess.run([sys.executable, CLI, "queue"], capture_output=True,
                             text=True, env=os.environ)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("t-0001", out.stdout)

        out = subprocess.run([sys.executable, CLI, "status"], capture_output=True,
                             text=True, env=os.environ)
        self.assertEqual(out.returncode, 0, out.stderr)
        # Both lanes, because the CLI is what the user reaches for when the
        # chat surface is down, and half the truth is worst-case there.
        self.assertRegex(out.stdout, r"claude\s+running")
        self.assertRegex(out.stdout, r"codex\s+running")
        self.assertIn("queued 1", out.stdout)

    def test_the_cli_works_through_a_symlink_on_path(self):
        """Task 11 puts `dispatch` on PATH as a symlink.

        `abspath` does not resolve one, so the launcher would insert the
        symlink's own directory into `sys.path` and fail to import the package.
        """
        link = os.path.join(self.tmp.name, "bin", "dispatch")
        os.symlink(CLI, link)
        out = subprocess.run([link, "status"], capture_output=True, text=True,
                             env=os.environ)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertRegex(out.stdout, r"claude\s+running")

    def test_the_tmux_launch_command_really_starts_the_daemon(self):
        """What `dispatch up` hands tmux has to be a program that keeps running.

        `python3 -m dispatch.cli run` imports cli.py and exits 0 -- there is no
        `__main__` guard -- so the session would die on launch and the cron
        watchdog would relaunch it every five minutes forever. Zero ticks, so
        nothing is polled and no request is spent; the daemon's own directories
        are the evidence that it was actually constructed.
        """
        from dispatch import cli as cli_mod

        home = os.path.join(self.tmp.name, "up-home")
        env = dict(os.environ, DISPATCH_HOME=home)
        command, cwd = cli_mod._daemon_argv()
        # Through a shell, because that is how tmux runs it -- and because the
        # PATH the daemon must have rides on the command as an assignment prefix.
        out = subprocess.run(["sh", "-c", command + " --ticks 0"], cwd=cwd,
                             capture_output=True, text=True, env=env,
                             stdin=subprocess.DEVNULL)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(os.path.isdir(os.path.join(home, "locks")), out.stdout)
        # This home has no config.json, so the allowlist is empty. The daemon
        # still starts -- refusing would be a crash loop under the watchdog --
        # and says why it will answer nobody.
        self.assertIn("allowlist", out.stderr)

    def test_the_launch_command_pins_path_through_the_shell(self):
        """The daemon's children exec a bare `claude` and a bare `codex`.

        tmux gives a session the server's environment, and after a reboot the
        server is whichever one cron started, so the PATH has to come from the
        command rather than be inherited. Measured on tmux 3.6, `new-session -e
        PATH=` is ignored and this prefix is not; here it is proved against a
        real shell with a deliberately bare ambient PATH.
        """
        from dispatch import cli as cli_mod

        pinned = cli_mod.daemon_path()
        seen = subprocess.run(
            ["sh", "-c", "PATH=%s printenv PATH" % shlex.quote(pinned)],
            capture_output=True, text=True,
            env=dict(os.environ, PATH="/usr/bin:/bin"))
        self.assertEqual(seen.returncode, 0, seen.stderr)
        self.assertEqual(seen.stdout.strip(), pinned)

    def test_setup_disables_the_conflicting_chat_plugin(self):
        settings = os.path.join(self.tmp.name, "settings.json")
        with open(settings, "w") as fh:
            fh.write('{"enabledPlugins": {"telegram@claude-plugins-official": true}}')
        out = subprocess.run(
            [sys.executable, CLI, "setup", "--settings", settings,
             "--repo", "demo=" + self.repo],
            capture_output=True, text=True, env=os.environ)
        self.assertEqual(out.returncode, 0, out.stderr)
        with open(settings) as fh:
            self.assertFalse(json.load(fh)["enabledPlugins"][
                "telegram@claude-plugins-official"])
        self.assertTrue(os.path.exists(settings + ".bak"))
        self.assertTrue(os.path.exists(config_mod.config_path()))


if __name__ == "__main__":
    unittest.main(verbosity=1)
