#!/usr/bin/env python3
"""Offline unit tests for the dispatch decision surface.

No network, no subprocesses, no real clock. Every value the daemon would read
from the world is injected, so these tests assert on the logic itself.
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "skills", "dispatch"))

from dispatch import config as config_mod  # noqa: E402
from dispatch import backends, governor, lanes, parser, scheduler, state, usage, winddown, worker  # noqa: E402

CONFIG = config_mod.DEFAULTS
MILLION = 1_000_000


class TestParser(unittest.TestCase):
    def test_bare_commands(self):
        for text, kind in [("status", "status"), ("/queue", "queue"), ("q", "queue"),
                           ("usage", "usage"), ("PAUSE", "pause"), ("resume", "resume")]:
            self.assertEqual(parser.parse(text)["kind"], kind, text)

    def test_run_on_repo(self):
        command = parser.parse("run the migration on qpay-backend")
        self.assertEqual(command["kind"], "run")
        self.assertEqual(command["repo"], "qpay-backend")
        self.assertEqual(command["prompt"], "the migration")
        self.assertEqual(command["isolation"], "repo")

    def test_run_colon_form(self):
        command = parser.parse("run qpay: bump deps")
        self.assertEqual((command["repo"], command["prompt"]), ("qpay", "bump deps"))

    def test_worktree_suffix(self):
        command = parser.parse("run the audit on escrow in a worktree")
        self.assertEqual(command["isolation"], "worktree")
        self.assertEqual(command["repo"], "escrow")

    def test_ids_normalize(self):
        for raw in ("cancel t-7", "cancel 7", "cancel t-0007"):
            self.assertEqual(parser.parse(raw), {"kind": "cancel", "id": "t-0007"}, raw)
        self.assertEqual(parser.parse("logs t-12")["kind"], "logs")

    def test_freeform_falls_through(self):
        command = parser.parse("run the migration on qpay and bump deps on escrow")
        # Two repos in one sentence is exactly what the fast path must not guess at.
        self.assertEqual(command["kind"], "run")
        self.assertEqual(command["repo"], "escrow")
        self.assertEqual(parser.parse("please tidy things up")["kind"], "unparsed")

    def test_empty_is_unparsed(self):
        self.assertEqual(parser.parse("")["kind"], "unparsed")
        self.assertEqual(parser.parse(None)["kind"], "unparsed")


class TestGovernorLadder(unittest.TestCase):
    def test_boundaries(self):
        cases = [(0.0, 3), (39.9, 3), (40.0, 2), (64.9, 2), (65.0, 1),
                 (84.9, 1), (85.0, 0), (99.0, 0), (100.0, 0)]
        for pct, expected in cases:
            self.assertEqual(governor.max_concurrency(pct), expected, pct)

    def test_unknown_usage_allows_one(self):
        self.assertEqual(governor.max_concurrency(None), 1)


class TestGovernorPolling(unittest.TestCase):
    def test_first_poll_always_allowed(self):
        self.assertTrue(governor.should_poll(governor.blank(), 1000.0, config=CONFIG))

    def test_floor_beats_force(self):
        snapshot = dict(governor.blank(), polled_at=1000.0)
        self.assertFalse(governor.should_poll(snapshot, 1030.0, force=True, config=CONFIG))
        self.assertTrue(governor.should_poll(snapshot, 1061.0, force=True, config=CONFIG))

    def test_idle_and_hot_cadence(self):
        snapshot = dict(governor.blank(), polled_at=1000.0)
        self.assertFalse(governor.should_poll(snapshot, 1000.0 + 200, config=CONFIG))
        self.assertTrue(governor.should_poll(snapshot, 1000.0 + 200, hot=True, config=CONFIG))
        self.assertTrue(governor.should_poll(snapshot, 1000.0 + 601, config=CONFIG))

    def test_crossing_reset_forces_a_poll(self):
        snapshot = dict(governor.blank(), polled_at=1000.0, session_reset=1100.0)
        self.assertTrue(governor.should_poll(snapshot, 1101.0, config=CONFIG))


class TestGovernorEstimate(unittest.TestCase):
    def _two_polls(self):
        snapshot = governor.blank()
        snapshot = governor.record_poll(
            snapshot, {"ok": True, "at": 1000.0, "session_pct": 10.0,
                       "session_reset": 20000.0, "week_pct": 5.0, "week_reset": None}, 0)
        snapshot = governor.record_poll(
            snapshot, {"ok": True, "at": 2000.0, "session_pct": 20.0,
                       "session_reset": 20000.0, "week_pct": 6.0, "week_reset": None},
            10 * MILLION)
        return snapshot

    def test_ratio_is_learned_from_two_polls(self):
        snapshot = self._two_polls()
        self.assertAlmostEqual(snapshot["session_ratio"], 10.0 / (10 * MILLION))

    def test_projection_between_polls(self):
        snapshot = self._two_polls()
        reading = governor.estimate(snapshot, 2500.0, 15 * MILLION)
        self.assertAlmostEqual(reading["session_pct"], 25.0)
        self.assertEqual(reading["source"], "projected")
        self.assertFalse(reading["stale"])

    def test_no_tokens_burned_reads_as_measured(self):
        snapshot = self._two_polls()
        reading = governor.estimate(snapshot, 2100.0, 10 * MILLION)
        self.assertEqual(reading["source"], "measured")
        self.assertAlmostEqual(reading["session_pct"], 20.0)

    def test_projection_is_clamped(self):
        snapshot = self._two_polls()
        reading = governor.estimate(snapshot, 2500.0, 500 * MILLION)
        self.assertEqual(reading["session_pct"], 100.0)

    def test_never_polled_is_stale(self):
        reading = governor.estimate(governor.blank(), 1000.0, 0)
        self.assertTrue(reading["stale"])
        self.assertIsNone(reading["session_pct"])

    def test_window_rollover_is_stale(self):
        snapshot = self._two_polls()
        reading = governor.estimate(snapshot, 20001.0, 12 * MILLION)
        self.assertTrue(reading["stale"])
        self.assertEqual(reading["source"], "post-reset")

    def test_window_rollover_does_not_teach_a_ratio(self):
        snapshot = self._two_polls()
        before = snapshot["session_ratio"]
        rolled = governor.record_poll(
            snapshot, {"ok": True, "at": 21000.0, "session_pct": 2.0,
                       "session_reset": 40000.0, "week_pct": 7.0, "week_reset": None},
            12 * MILLION)
        self.assertEqual(rolled["session_ratio"], before)

    def test_failed_poll_keeps_last_good_reading(self):
        snapshot = self._two_polls()
        failed = governor.record_poll(snapshot, {"ok": False, "error": "timeout"}, 11 * MILLION)
        self.assertEqual(failed["session_pct"], 20.0)
        self.assertFalse(failed["last_poll_ok"])

    def test_limit_error_overrides_projection(self):
        snapshot = governor.note_limit_error(self._two_polls(), 9000.0)
        reading = governor.estimate(snapshot, 2500.0, 10 * MILLION)
        self.assertEqual(reading["session_pct"], 100.0)
        self.assertEqual(reading["source"], "limit-error")
        self.assertEqual(governor.max_concurrency(reading["session_pct"]), 0)

    def test_override_expires_on_a_later_poll(self):
        snapshot = governor.note_limit_error(self._two_polls(), 9000.0)
        cleared = governor.record_poll(
            snapshot, {"ok": True, "at": 9100.0, "session_pct": 3.0,
                       "session_reset": 30000.0, "week_pct": 7.0, "week_reset": None},
            12 * MILLION)
        self.assertIsNone(cleared["override_until"])
        self.assertEqual(governor.estimate(cleared, 9200.0, 12 * MILLION)["source"], "measured")

    def test_seed_ratio_used_before_two_polls(self):
        snapshot = governor.record_poll(
            governor.blank(), {"ok": True, "at": 1000.0, "session_pct": 10.0,
                               "session_reset": 20000.0, "week_pct": None,
                               "week_reset": None}, 0)
        reading = governor.estimate(snapshot, 1100.0, 5 * MILLION)
        self.assertAlmostEqual(reading["session_pct"], 10.0 + 5 * MILLION * governor.SEED_PCT_PER_TOKEN)


class TestUsageParsing(unittest.TestCase):
    def test_percentages_and_resets(self):
        text = ("Current session: 47% used\n"
                "  Resets 7:50pm\n"
                "Current week (all models): 12% used\n"
                "  Resets Feb 5\n")
        now = 1_000_000.0
        reading = usage.parse_usage_text(text, now)
        self.assertEqual(reading["session_pct"], 47.0)
        self.assertEqual(reading["week_pct"], 12.0)
        self.assertIsNotNone(reading["session_reset"])
        self.assertGreater(reading["session_reset"], now)

    def test_inline_reset(self):
        reading = usage.parse_usage_text("Current session: 80% used, resets in 2h 30m", 0.0)
        self.assertEqual(reading["session_pct"], 80.0)
        self.assertAlmostEqual(reading["session_reset"], 9000.0)

    def test_no_percentages(self):
        reading = usage.parse_usage_text("nothing useful here", 0.0)
        self.assertIsNone(reading["session_pct"])

    def test_limit_error_epoch(self):
        self.assertEqual(
            usage.parse_limit_error("Claude AI usage limit reached|1755600000"),
            1755600000.0)

    def test_limit_error_milliseconds(self):
        self.assertEqual(
            usage.parse_limit_error("usage limit reached|1755600000000"),
            1755600000.0)

    def test_non_limit_text(self):
        self.assertIsNone(usage.parse_limit_error("everything is fine"))

    def test_clock_reset_rolls_to_tomorrow(self):
        # 12:00 local; a 7:50am reset must mean tomorrow, not eight hours ago.
        import time as _time

        noon = _time.mktime((2026, 2, 3, 12, 0, 0, 0, 1, -1))
        self.assertGreater(usage.parse_reset("7:50am", noon), noon)


class TestScheduler(unittest.TestCase):
    def _ctx(self, **over):
        queue = {"next_id": 2, "tasks": []}
        ctx = {"mode": "running", "queue": queue, "running": 0,
               "session_pct": 10.0, "week_pct": 5.0, "stale": False,
               "est_cost_pct": 6.0, "config": CONFIG,
               "lock_free": lambda name: True}
        ctx.update(over)
        return ctx

    def _task(self, **over):
        task = {"id": "t-0001", "repo": "demo", "state": "queued", "priority": 5,
                "deps": [], "isolation": "repo"}
        task.update(over)
        return task

    def test_admits_when_clear(self):
        ok, reason = scheduler.admit(self._task(), self._ctx())
        self.assertTrue(ok, reason)

    def test_mode_gate(self):
        ok, reason = scheduler.admit(self._task(), self._ctx(mode="frozen"))
        self.assertFalse(ok)
        self.assertIn("frozen", reason)

    def test_unmet_dependency(self):
        ctx = self._ctx()
        ctx["queue"]["tasks"] = [{"id": "t-0002", "state": "running"}]
        ok, reason = scheduler.admit(self._task(deps=["t-0002"]), ctx)
        self.assertFalse(ok)
        self.assertIn("dependencies", reason)

    def test_met_dependency(self):
        ctx = self._ctx()
        ctx["queue"]["tasks"] = [{"id": "t-0002", "state": "done"}]
        self.assertTrue(scheduler.admit(self._task(deps=["t-0002"]), ctx)[0])

    def test_stale_usage_blocks(self):
        ok, reason = scheduler.admit(self._task(), self._ctx(stale=True))
        self.assertFalse(ok)
        self.assertIn("poll", reason)

    def test_weekly_soft_limit(self):
        ok, reason = scheduler.admit(self._task(), self._ctx(week_pct=91.0))
        self.assertFalse(ok)
        self.assertIn("weekly", reason)

    def test_concurrency_ladder_enforced(self):
        # 50% allows two workers; the third is refused.
        self.assertTrue(scheduler.admit(self._task(), self._ctx(session_pct=50.0, running=1))[0])
        ok, reason = scheduler.admit(self._task(), self._ctx(session_pct=50.0, running=2))
        self.assertFalse(ok)
        self.assertIn("concurrency", reason)

    def test_headroom_refuses_a_step_that_would_cross_soft(self):
        ok, reason = scheduler.admit(self._task(), self._ctx(session_pct=80.0, est_cost_pct=6.0))
        self.assertFalse(ok)
        self.assertIn("soft limit", reason)

    def test_headroom_allows_a_step_that_fits(self):
        self.assertTrue(scheduler.admit(self._task(), self._ctx(session_pct=79.0, est_cost_pct=6.0))[0])

    def test_busy_repo_lock(self):
        ok, reason = scheduler.admit(self._task(), self._ctx(lock_free=lambda name: False))
        self.assertFalse(ok)
        self.assertIn("busy", reason)

    def test_worktree_isolation_uses_its_own_lock(self):
        self.assertEqual(scheduler.lock_name(self._task()), "repo-demo")
        self.assertEqual(scheduler.lock_name(self._task(isolation="worktree")), "worktree-t-0001")

    def test_paused_drains_before_queued(self):
        queue = {"next_id": 4, "tasks": [
            {"id": "t-0001", "state": "queued", "priority": 5},
            {"id": "t-0002", "state": "paused", "priority": 9},
            {"id": "t-0003", "state": "done", "priority": 1},
        ]}
        order = [t["id"] for t in scheduler.runnable(queue)]
        self.assertEqual(order, ["t-0002", "t-0001"])

    def test_priority_orders_within_a_state(self):
        queue = {"next_id": 3, "tasks": [
            {"id": "t-0001", "state": "queued", "priority": 9},
            {"id": "t-0002", "state": "queued", "priority": 1},
        ]}
        self.assertEqual([t["id"] for t in scheduler.runnable(queue)], ["t-0002", "t-0001"])

    def test_next_admissible_reports_why_others_failed(self):
        ctx = self._ctx(session_pct=50.0, running=2)
        ctx["queue"]["tasks"] = [self._task()]
        chosen, reasons = scheduler.next_admissible(ctx)
        self.assertIsNone(chosen)
        self.assertEqual(reasons[0][0], "t-0001")


class TestWindDown(unittest.TestCase):
    def test_stays_running_below_soft(self):
        self.assertEqual(winddown.next_mode("running", 50.0, 1, CONFIG), "running")

    def test_soft_limit_with_work_in_flight_drains(self):
        self.assertEqual(winddown.next_mode("running", 86.0, 1, CONFIG), "winding-down")

    def test_soft_limit_with_nothing_running_freezes(self):
        self.assertEqual(winddown.next_mode("running", 86.0, 0, CONFIG), "frozen")

    def test_hard_limit_still_drains_before_freezing(self):
        self.assertEqual(winddown.next_mode("winding-down", 96.0, 1, CONFIG), "winding-down")
        self.assertTrue(winddown.should_terminate("winding-down", 96.0, CONFIG))

    def test_soft_limit_does_not_terminate_in_flight_work(self):
        self.assertFalse(winddown.should_terminate("winding-down", 86.0, CONFIG))

    def test_recovers_if_usage_falls_back(self):
        self.assertEqual(winddown.next_mode("winding-down", 40.0, 0, CONFIG), "running")

    def test_frozen_is_sticky(self):
        self.assertEqual(winddown.next_mode("frozen", 5.0, 0, CONFIG), "frozen")

    def test_unknown_usage_never_escalates(self):
        self.assertEqual(winddown.next_mode("running", None, 0, CONFIG), "running")
        self.assertEqual(winddown.next_mode("running", 99.0, 0, CONFIG, stale=True), "running")

    def test_resume_is_armed_after_the_reset(self):
        self.assertEqual(winddown.resume_at(5000.0), 5000.0 + winddown.RESUME_DELAY)
        self.assertIsNone(winddown.resume_at(None))

    def test_resume_requires_a_confirmed_reset(self):
        doc = {"mode": "frozen", "armed_resume_at": 5060.0}
        self.assertFalse(winddown.can_resume(doc, 3.0, 5000.0, CONFIG, False)[0])
        self.assertFalse(winddown.can_resume(doc, 90.0, 5100.0, CONFIG, False)[0])
        self.assertFalse(winddown.can_resume(doc, None, 5100.0, CONFIG, True)[0])
        self.assertTrue(winddown.can_resume(doc, 3.0, 5100.0, CONFIG, False)[0])

    def test_resume_only_applies_when_frozen(self):
        doc = {"mode": "running", "armed_resume_at": None}
        self.assertFalse(winddown.can_resume(doc, 3.0, 5100.0, CONFIG, False)[0])


class TestWorkerContract(unittest.TestCase):
    def test_status_block_from_json_envelope(self):
        envelope = ('{"session_id": "sess-9", "result": "did a thing\\n'
                    '```json\\n{\\"status\\": \\"continue\\", \\"summary\\": \\"half\\", '
                    '\\"next\\": \\"rest\\"}\\n```"}')
        parsed = worker.parse_status(envelope, "/unused")
        self.assertEqual(parsed["status"], "continue")
        self.assertEqual(parsed["session_id"], "sess-9")
        self.assertEqual(parsed["next"], "rest")

    def test_last_block_wins(self):
        text = ('```json\n{"status": "continue", "summary": "a"}\n```\n'
                '```json\n{"status": "complete", "summary": "b"}\n```')
        self.assertEqual(worker.parse_status(text, "/unused")["status"], "complete")

    def test_missing_block_is_not_a_status(self):
        self.assertIsNone(worker.parse_status("I finished everything!", "/unused")["status"])

    def test_invalid_status_value_rejected(self):
        self.assertIsNone(
            worker.parse_status('```json\n{"status": "done"}\n```', "/unused")["status"])

    def test_next_state_matrix(self):
        self.assertEqual(worker.next_state("complete", "running"), "done")
        self.assertEqual(worker.next_state("blocked", "running"), "blocked")
        self.assertEqual(worker.next_state("continue", "running"), "queued")
        self.assertEqual(worker.next_state("continue", "winding-down"), "paused")
        self.assertEqual(worker.next_state(None, "running"), "failed")

    def test_house_rules_name_the_task_branch(self):
        rules = backends.claude.house_rules({"branch": "tg/t-0042"})
        self.assertIn("tg/t-0042", rules)
        self.assertIn("Never push", rules)

    def test_resume_flag_only_with_a_session(self):
        task = {"prompt": "do", "branch": "tg/t-1", "session_id": None}
        self.assertNotIn("--resume", worker.build_command(
            task, "/p/prompt.txt", "/repo", "/unused"))
        task["session_id"] = "sess-1"
        argv = worker.build_command(task, "/p/prompt.txt", "/repo", "/unused")
        self.assertEqual(argv[argv.index("--resume") + 1], "sess-1")


class TestCostModel(unittest.TestCase):
    def test_default_until_measured(self):
        self.assertEqual(governor.est_cost_pct({}, "demo", CONFIG),
                         CONFIG["default_est_cost_pct"])

    def test_learned_cost_moves_toward_observation(self):
        doc = {}
        governor.learn_cost(doc, "demo", 10.0)
        self.assertEqual(doc["repo_cost_pct"]["demo"], 10.0)
        governor.learn_cost(doc, "demo", 20.0)
        self.assertAlmostEqual(doc["repo_cost_pct"]["demo"], 13.0)

    def test_negative_observations_ignored(self):
        doc = {"repo_cost_pct": {"demo": 5.0}}
        governor.learn_cost(doc, "demo", -1)
        self.assertEqual(doc["repo_cost_pct"]["demo"], 5.0)


class TestStateDurability(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._previous = os.environ.get("DISPATCH_HOME")
        os.environ["DISPATCH_HOME"] = self._tmp.name

    def tearDown(self):
        if self._previous is None:
            os.environ.pop("DISPATCH_HOME", None)
        else:
            os.environ["DISPATCH_HOME"] = self._previous
        self._tmp.cleanup()

    def test_missing_documents_read_as_empty(self):
        self.assertEqual(state.read_queue()["tasks"], [])
        self.assertEqual(state.read_state()["mode"], {"claude": "running", "codex": "running"})

    def test_corrupt_document_does_not_crash(self):
        config_mod.ensure_dirs()
        with open(config_mod.queue_path(), "w") as fh:
            fh.write("{not json")
        self.assertEqual(state.read_queue()["tasks"], [])

    def test_ids_are_sequential_and_padded(self):
        with state.mutate_queue() as queue:
            first = state.new_task(queue, "demo", "one")
            second = state.new_task(queue, "demo", "two")
        self.assertEqual((first["id"], second["id"]), ("t-0001", "t-0002"))
        self.assertEqual(state.find(state.read_queue(), "t-0002")["prompt"], "two")

    def test_task_defaults_to_its_own_branch(self):
        with state.mutate_queue() as queue:
            task = state.new_task(queue, "demo", "one")
        self.assertEqual(task["branch"], "tg/t-0001")

    def test_locks_are_exclusive(self):
        handle = state.try_lock("repo-demo")
        self.assertIsNotNone(handle)
        try:
            self.assertIsNone(state.try_lock("repo-demo"))
            other = state.try_lock("repo-other")
            self.assertIsNotNone(other)
            state.release(other)
        finally:
            state.release(handle)
        regained = state.try_lock("repo-demo")
        self.assertIsNotNone(regained)
        state.release(regained)

    def test_write_is_atomic_on_replace(self):
        state.write(config_mod.state_path(), {"mode": "frozen"})
        self.assertEqual(state.read_state()["mode"], {"claude": "frozen", "codex": "frozen"})
        leftovers = [n for n in os.listdir(config_mod.home()) if n.startswith(".tmp-")]
        self.assertEqual(leftovers, [])


class TestCodexGovernor(unittest.TestCase):
    """Codex limits come from codex's own logs, so these tests write logs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "sessions", "2026", "08", "20")
        os.makedirs(self.root)
        self.now = 1_800_000_000.0

    def _write(self, name, records):
        path = os.path.join(self.root, name)
        with open(path, "w") as fh:
            for record in records:
                fh.write(json.dumps(record) + "\n")
        return path

    def _limits(self, primary=None, secondary=None):
        return {"payload": {"info": {"rate_limits": {
            "limit_id": "codex", "primary": primary, "secondary": secondary}}}}

    def test_reads_both_windows(self):
        self._write("a.jsonl", [self._limits(
            primary={"used_percent": 12.0, "window_minutes": 300,
                     "resets_at": self.now + 3600},
            secondary={"used_percent": 64.0, "window_minutes": 10080,
                       "resets_at": self.now + 86400})])
        reading = governor.codex.reading(self.now, root=self.tmp.name)
        self.assertTrue(reading["ok"])
        self.assertEqual(reading["session_pct"], 12.0)
        self.assertEqual(reading["week_pct"], 64.0)

    def test_keys_by_window_not_by_slot(self):
        """The weekly window turns up under `primary` on some records."""
        self._write("a.jsonl", [self._limits(
            primary={"used_percent": 100.0, "window_minutes": 10080,
                     "resets_at": self.now + 86400})])
        reading = governor.codex.reading(self.now, root=self.tmp.name)
        self.assertEqual(reading["week_pct"], 100.0)
        self.assertIsNone(reading["session_pct"])

    def test_null_limits_are_skipped(self):
        self._write("a.jsonl", [self._limits(), self._limits(
            primary={"used_percent": 5.0, "window_minutes": 300,
                     "resets_at": self.now + 60})])
        self.assertEqual(governor.codex.reading(
            self.now, root=self.tmp.name)["session_pct"], 5.0)

    def test_expired_reading_is_discarded(self):
        self._write("a.jsonl", [self._limits(
            primary={"used_percent": 99.0, "window_minutes": 300,
                     "resets_at": self.now - 1})])
        reading = governor.codex.reading(self.now, root=self.tmp.name)
        self.assertFalse(reading["ok"])
        self.assertIsNone(reading["session_pct"])

    def test_newest_file_wins(self):
        old = self._write("old.jsonl", [self._limits(
            primary={"used_percent": 10.0, "window_minutes": 300,
                     "resets_at": self.now + 60})])
        new = self._write("new.jsonl", [self._limits(
            primary={"used_percent": 90.0, "window_minutes": 300,
                     "resets_at": self.now + 60})])
        os.utime(old, (self.now - 600, self.now - 600))
        os.utime(new, (self.now, self.now))
        self.assertEqual(governor.codex.reading(
            self.now, root=self.tmp.name)["session_pct"], 90.0)

    def test_last_record_in_a_file_wins(self):
        self._write("a.jsonl", [
            self._limits(primary={"used_percent": 10.0, "window_minutes": 300,
                                  "resets_at": self.now + 60}),
            self._limits(primary={"used_percent": 40.0, "window_minutes": 300,
                                  "resets_at": self.now + 60})])
        self.assertEqual(governor.codex.reading(
            self.now, root=self.tmp.name)["session_pct"], 40.0)

    def test_empty_tree_is_optimistic_not_stale(self):
        """No data must not block the lane -- the only way to get a reading is
        to run codex, so blocking on absence would deadlock."""
        empty = os.path.join(self.tmp.name, "nothing")
        estimate = governor.codex.estimate(self.now, root=empty)
        self.assertFalse(estimate["stale"])
        self.assertEqual(estimate["session_pct"], 0.0)
        self.assertEqual(estimate["source"], "codex-unknown")

    def test_estimate_reports_the_worse_window(self):
        self._write("a.jsonl", [self._limits(
            primary={"used_percent": 12.0, "window_minutes": 300,
                     "resets_at": self.now + 3600},
            secondary={"used_percent": 100.0, "window_minutes": 10080,
                       "resets_at": self.now + 86400})])
        estimate = governor.codex.estimate(self.now, root=self.tmp.name)
        self.assertEqual(estimate["session_pct"], 12.0)
        self.assertEqual(estimate["week_pct"], 100.0)
        self.assertEqual(estimate["source"], "codex-logs")

    def test_malformed_lines_do_not_raise(self):
        path = os.path.join(self.root, "bad.jsonl")
        with open(path, "w") as fh:
            fh.write("not json\n")
            fh.write(json.dumps(self._limits(
                primary={"used_percent": 7.0, "window_minutes": 300,
                         "resets_at": self.now + 60})) + "\n")
        self.assertEqual(governor.codex.reading(
            self.now, root=self.tmp.name)["session_pct"], 7.0)


class TestLanes(unittest.TestCase):
    def test_scalar_mode_expands_to_both_lanes(self):
        self.assertEqual(lanes.normalize_mode("winding-down"),
                         {"claude": "winding-down", "codex": "winding-down"})

    def test_dict_mode_fills_missing_lane(self):
        self.assertEqual(lanes.normalize_mode({"claude": "frozen"}),
                         {"claude": "frozen", "codex": "running"})

    def test_none_mode_defaults_to_running(self):
        self.assertEqual(lanes.normalize_mode(None),
                         {"claude": "running", "codex": "running"})

    def test_unknown_lane_is_dropped(self):
        self.assertEqual(lanes.normalize_mode({"claude": "frozen", "gemini": "running"}),
                         {"claude": "frozen", "codex": "running"})

    def test_scalar_armed_expands(self):
        self.assertEqual(lanes.normalize_armed(1234.0),
                         {"claude": 1234.0, "codex": 1234.0})

    def test_armed_defaults_to_none(self):
        self.assertEqual(lanes.normalize_armed(None),
                         {"claude": None, "codex": None})

    def test_task_lane_defaults_to_claude(self):
        self.assertEqual(lanes.of({"id": "t-0001"}), "claude")
        self.assertEqual(lanes.of({"id": "t-0001", "agent": "codex"}), "codex")

    def test_unknown_agent_falls_back_to_claude(self):
        self.assertEqual(lanes.of({"agent": "gemini"}), "claude")


class TestStateMigration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._previous = os.environ.get("DISPATCH_HOME")
        os.environ["DISPATCH_HOME"] = self.tmp.name
        self.addCleanup(self._restore)
        config_mod.ensure_dirs()

    def _restore(self):
        if self._previous is None:
            os.environ.pop("DISPATCH_HOME", None)
        else:
            os.environ["DISPATCH_HOME"] = self._previous

    def test_old_state_file_loads_with_lanes(self):
        """A state.json written by the pre-lane version must still load."""
        state.write(config_mod.state_path(), {
            "mode": "frozen", "chat_offset": 12, "governor": {},
            "armed_resume_at": 999.0, "repo_cost_pct": {}})
        doc = state.read_state()
        self.assertEqual(doc["mode"], {"claude": "frozen", "codex": "frozen"})
        self.assertEqual(doc["armed_resume_at"], {"claude": 999.0, "codex": 999.0})
        self.assertEqual(doc["chat_offset"], 12)

    def test_fresh_state_is_per_lane(self):
        doc = state.read_state()
        self.assertEqual(doc["mode"], {"claude": "running", "codex": "running"})

    def test_new_task_records_its_agent(self):
        queue = dict(state.QUEUE_EMPTY, tasks=[])
        task = state.new_task(queue, "qpay", "do a thing", agent="codex")
        self.assertEqual(task["agent"], "codex")

    def test_new_task_defaults_to_claude(self):
        queue = dict(state.QUEUE_EMPTY, tasks=[])
        self.assertEqual(state.new_task(queue, "qpay", "x")["agent"], "claude")


class TestBackends(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.task = {"id": "t-0001", "repo": "qpay", "prompt": "do a thing",
                     "branch": "tg/t-0001", "agent": "claude", "session_id": None}

    def test_registry_resolves_both(self):
        self.assertIs(backends.get("claude"), backends.claude)
        self.assertIs(backends.get("codex"), backends.codex)

    def test_registry_defaults_to_claude(self):
        self.assertIs(backends.get("gemini"), backends.claude)
        self.assertIs(backends.get(None), backends.claude)

    def test_claude_reads_prompt_from_stdin(self):
        argv = backends.claude.build_command(
            self.task, "/p/prompt.txt", "/repo", self.tmp.name)
        self.assertEqual(argv[:3], ["claude", "-p", "-"])
        self.assertIn("--dangerously-skip-permissions", argv)
        self.assertNotIn("do a thing", argv)

    def test_claude_resume_is_an_option_pair(self):
        self.assertEqual(backends.claude.resume_args("abc"), ["--resume", "abc"])
        self.assertEqual(backends.claude.resume_args(None), [])

    def test_codex_resume_is_a_positional_subcommand(self):
        """codex continues with `exec resume <id>`, not a trailing flag."""
        self.assertEqual(backends.codex.resume_args("abc"), ["resume", "abc"])
        self.assertEqual(backends.codex.resume_args(None), [])

    def test_codex_places_resume_before_the_prompt_marker(self):
        task = dict(self.task, agent="codex", session_id="abc")
        argv = backends.codex.build_command(
            task, "/p/prompt.txt", "/repo", self.tmp.name)
        self.assertEqual(argv[:4], ["codex", "exec", "resume", "abc"])
        self.assertLess(argv.index("resume"), argv.index("-"))

    def test_codex_command_shape(self):
        argv = backends.codex.build_command(
            dict(self.task, agent="codex"), "/p/prompt.txt", "/repo", self.tmp.name)
        self.assertEqual(argv[:3], ["codex", "exec", "-"])
        self.assertIn("--json", argv)
        self.assertEqual(argv[argv.index("-C") + 1], "/repo")
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertTrue(argv[argv.index("--output-schema") + 1].endswith(
            "status.schema.json"))
        self.assertEqual(argv[argv.index("-o") + 1],
                         os.path.join(self.tmp.name, "last.json"))

    def test_codex_schema_file_is_valid_json_and_requires_status(self):
        with open(backends.codex.SCHEMA_PATH) as fh:
            schema = json.load(fh)
        self.assertIn("status", schema["properties"])
        self.assertIn("status", schema["required"])

    def test_codex_parses_the_last_message_file(self):
        with open(os.path.join(self.tmp.name, "last.json"), "w") as fh:
            json.dump({"status": "complete", "summary": "did it", "next": ""}, fh)
        result = backends.codex.parse_result("", self.tmp.name)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["summary"], "did it")

    def test_codex_missing_last_message_is_a_failure_not_a_crash(self):
        result = backends.codex.parse_result("", self.tmp.name)
        self.assertIsNone(result["status"])

    def test_codex_malformed_last_message_is_a_failure(self):
        with open(os.path.join(self.tmp.name, "last.json"), "w") as fh:
            fh.write("{not json")
        self.assertIsNone(backends.codex.parse_result("", self.tmp.name)["status"])

    def test_codex_rejects_an_invalid_status_value(self):
        with open(os.path.join(self.tmp.name, "last.json"), "w") as fh:
            json.dump({"status": "finished-ish", "summary": ""}, fh)
        self.assertIsNone(backends.codex.parse_result("", self.tmp.name)["status"])

    def test_codex_finds_a_session_id_under_any_of_its_names(self):
        for key in ("thread_id", "session_id", "conversation_id"):
            stream = json.dumps({"type": "thread.started", key: "sess-9"})
            result = backends.codex.parse_result(stream, self.tmp.name)
            self.assertEqual(result["session_id"], "sess-9", key)

    def test_codex_without_a_session_id_returns_none(self):
        result = backends.codex.parse_result('{"type":"item.done"}', self.tmp.name)
        self.assertIsNone(result["session_id"])

    def test_claude_fence_parsing_is_unchanged(self):
        output = json.dumps({"session_id": "s1", "result":
                             'done\n```json\n{"status": "complete", "summary": "ok"}\n```'})
        result = backends.claude.parse_result(output, self.tmp.name)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["session_id"], "s1")

    def test_codex_house_rules_omit_the_fence_instruction(self):
        rules = backends.codex.house_rules(self.task)
        self.assertNotIn("```json", rules)
        self.assertIn("tg/t-0001", rules)
        self.assertIn("blocked", rules)

    def test_claude_house_rules_keep_the_fence_instruction(self):
        self.assertIn("```json", backends.claude.house_rules(self.task))


class TestPromptFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_prompt_file_holds_prompt_plus_house_rules(self):
        task = {"id": "t-0002", "repo": "qpay", "prompt": "ship it",
                "branch": "tg/t-0002", "agent": "claude"}
        path = worker.write_prompt(task, self.tmp.name)
        body = open(path).read()
        self.assertTrue(body.startswith("ship it"))
        self.assertIn("tg/t-0002", body)

    def test_prompt_with_shell_metacharacters_survives_verbatim(self):
        """The reason the prompt is a file: chat text is arbitrary."""
        nasty = 'rm -rf $HOME; echo "`whoami`" && exit 1'
        task = {"id": "t-0003", "repo": "qpay", "prompt": nasty,
                "branch": "tg/t-0003", "agent": "claude"}
        self.assertIn(nasty, open(worker.write_prompt(task, self.tmp.name)).read())

    def test_prompt_is_fed_on_stdin_not_argv(self):
        recorded = {}

        class FakeProcess:
            returncode = 0

            def communicate(self, timeout=None):
                return "", None

            def poll(self):
                return 0

        def fake_popen(argv, **kwargs):
            recorded["argv"] = argv
            recorded["stdin"] = kwargs.get("stdin")
            return FakeProcess()

        task = {"id": "t-0004", "repo": "qpay", "prompt": "secret words",
                "branch": "tg/t-0004", "agent": "claude", "session_id": None}
        worker.run_step(task, self.tmp.name, CONFIG, popen=fake_popen,
                        task_dir=self.tmp.name)
        self.assertNotIn("secret words", " ".join(recorded["argv"]))
        self.assertIsNotNone(recorded["stdin"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
