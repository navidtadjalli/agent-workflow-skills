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
from dispatch import governor, parser, scheduler, state, usage, winddown, worker  # noqa: E402

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
        parsed = worker.parse_status(envelope)
        self.assertEqual(parsed["status"], "continue")
        self.assertEqual(parsed["session_id"], "sess-9")
        self.assertEqual(parsed["next"], "rest")

    def test_last_block_wins(self):
        text = ('```json\n{"status": "continue", "summary": "a"}\n```\n'
                '```json\n{"status": "complete", "summary": "b"}\n```')
        self.assertEqual(worker.parse_status(text)["status"], "complete")

    def test_missing_block_is_not_a_status(self):
        self.assertIsNone(worker.parse_status("I finished everything!")["status"])

    def test_invalid_status_value_rejected(self):
        self.assertIsNone(worker.parse_status('```json\n{"status": "done"}\n```')["status"])

    def test_next_state_matrix(self):
        self.assertEqual(worker.next_state("complete", "running"), "done")
        self.assertEqual(worker.next_state("blocked", "running"), "blocked")
        self.assertEqual(worker.next_state("continue", "running"), "queued")
        self.assertEqual(worker.next_state("continue", "winding-down"), "paused")
        self.assertEqual(worker.next_state(None, "running"), "failed")

    def test_house_rules_name_the_task_branch(self):
        rules = worker.house_rules({"branch": "tg/t-0042"})
        self.assertIn("tg/t-0042", rules)
        self.assertIn("Never push", rules)

    def test_resume_flag_only_with_a_session(self):
        task = {"prompt": "do", "branch": "tg/t-1", "session_id": None}
        self.assertNotIn("--resume", worker.build_command(task))
        task["session_id"] = "sess-1"
        argv = worker.build_command(task)
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
        self.assertEqual(state.read_state()["mode"], "running")

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
        self.assertEqual(state.read_state()["mode"], "frozen")
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


if __name__ == "__main__":
    unittest.main(verbosity=1)
