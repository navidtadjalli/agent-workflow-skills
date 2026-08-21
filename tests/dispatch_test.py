#!/usr/bin/env python3
"""Offline unit tests for the dispatch decision surface.

No network, no subprocesses, no real clock. Every value the daemon would read
from the world is injected, so these tests assert on the logic itself.
"""
import contextlib
import inspect
import io
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "skills", "dispatch"))

from dispatch import config as config_mod  # noqa: E402
from dispatch import backends, cli, governor, lanes, parser, repos, scheduler, sessions, state, usage, volume, winddown, worker  # noqa: E402
from dispatch import worktrees  # noqa: E402

CONFIG = config_mod.DEFAULTS
MILLION = 1_000_000


def _alternatives(pattern, group):
    """The literal alternatives a named group in ``pattern`` accepts."""
    match = re.search(r"\(\?P<%s>([^)]+)\)" % group, pattern.pattern)
    return match.group(1).split("|") if match else []


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


    def test_every_kind_it_can_emit_is_declared_in_KINDS(self):
        """KINDS is the contract the daemon's chat surface is checked against.

        A kind that reaches the daemon without a branch falls through to
        free-form intake and can answer with silence, so a new verb must not be
        able to slip past the enumeration. Derived from the parser itself rather
        than listed by hand: a hand-written list is exactly what drifts.
        """
        source = inspect.getsource(parser)
        for literal in re.findall(r'"kind":\s*"([a-z_]+)"', source):
            self.assertIn(literal, parser.KINDS, literal)

        probes = list(parser.BARE)
        probes += ["%s 1" % verb for verb in _alternatives(parser.WITH_ID, "verb")]
        probes += ["%s %s" % (verb, lane)
                   for verb in _alternatives(parser.LANE_MODE, "verb")
                   for lane in _alternatives(parser.LANE_MODE, "lane")]
        probes += ["%s tidy up on qpay" % agent
                   for agent in _alternatives(parser.AGENT_RUN, "agent")]
        probes += ["%s tidy up" % agent
                   for agent in _alternatives(parser.AGENT_BARE, "agent")]
        probes += ["usage poll", "sessions qpay", "run a thing on qpay",
                   "anything else at all", ""]
        self.assertGreater(len(probes), len(parser.KINDS))
        for probe in probes:
            self.assertIn(parser.parse(probe)["kind"], parser.KINDS, probe)


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

    def test_a_genuine_rollover_costs_exactly_one_confirming_poll(self):
        """The legitimate path must keep working: the window really does roll
        over, one poll confirms it, and the fresh reading carries the next
        reset -- after which the cadence takes over again."""
        snapshot = governor.record_poll(
            governor.blank(), {"ok": True, "at": 1000.0, "session_pct": 40.0,
                               "session_reset": 5000.0, "week_pct": 5.0,
                               "week_reset": 600000.0}, 0)
        self.assertTrue(governor.should_poll(snapshot, 5001.0, config=CONFIG))
        confirmed = governor.record_poll(
            snapshot, {"ok": True, "at": 5001.0, "session_pct": 1.0,
                       "session_reset": 23000.0, "week_pct": 6.0,
                       "week_reset": 600000.0}, 0)
        self.assertEqual(confirmed["session_reset"], 23000.0)
        self.assertFalse(governor.should_poll(confirmed, 5062.0, config=CONFIG))

    def test_a_reset_already_past_is_never_stored(self):
        """A reset behind the poll's own clock satisfies should_poll's
        rolled-over branch on every tick, so storing one turns the governor
        into a request pump against the limit it is measuring."""
        snapshot = governor.record_poll(
            governor.blank(), {"ok": True, "at": 5000.0, "session_pct": 18.0,
                               "session_reset": 1200.0, "week_pct": 17.0,
                               "week_reset": 900.0}, 0)
        self.assertIsNone(snapshot["session_reset"])
        self.assertIsNone(snapshot["week_reset"])
        self.assertFalse(governor.should_poll(snapshot, 5061.0, config=CONFIG))
        self.assertFalse(governor.estimate(snapshot, 5061.0, 0)["stale"])

    def test_a_latched_past_reset_is_cleared_by_the_next_poll(self):
        """State latched before this fix -- or a limit error carrying an old
        epoch -- must not survive a poll, whatever the new reading says."""
        latched = dict(governor.blank(), polled_at=1000.0, session_reset=1200.0,
                       session_pct=18.0, tokens_at_poll=0)
        self.assertTrue(governor.should_poll(latched, 5000.0, config=CONFIG))
        cleared = governor.record_poll(
            latched, {"ok": True, "at": 5000.0, "session_pct": 20.0,
                      "session_reset": None, "week_pct": None,
                      "week_reset": None}, 0)
        self.assertIsNone(cleared["session_reset"])
        self.assertFalse(governor.should_poll(cleared, 5061.0, config=CONFIG))

    def test_a_future_reset_survives_a_reading_that_omits_one(self):
        snapshot = governor.record_poll(
            governor.blank(), {"ok": True, "at": 1000.0, "session_pct": 10.0,
                               "session_reset": 20000.0, "week_pct": 5.0,
                               "week_reset": 600000.0}, 0)
        kept = governor.record_poll(
            snapshot, {"ok": True, "at": 2000.0, "session_pct": 12.0,
                       "session_reset": 20000.0, "week_pct": None,
                       "week_reset": None}, MILLION)
        self.assertEqual(kept["session_reset"], 20000.0)
        self.assertEqual(kept["week_reset"], 600000.0)

    def test_a_failed_poll_does_not_touch_a_stored_reset(self):
        snapshot = governor.record_poll(
            governor.blank(), {"ok": True, "at": 1000.0, "session_pct": 10.0,
                               "session_reset": 20000.0, "week_pct": 5.0,
                               "week_reset": 600000.0}, 0)
        failed = governor.record_poll(snapshot, {"ok": False, "error": "timeout"}, 0)
        self.assertEqual(failed["session_reset"], 20000.0)


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


def _local(year, month, day, hour=0, minute=0):
    """Epoch of a local wall-clock moment.

    Both the injected ``now`` and the expected answer go through this, so every
    assertion below holds in any timezone and in whatever month the suite
    happens to be run.
    """
    return time.mktime((year, month, day, hour, minute, 0, 0, 1, -1))


class TestResetParsing(unittest.TestCase):
    """Reset expressions in the shapes ``/usage`` actually emits.

    ``REAL_OUTPUT`` is verbatim from one real ``claude -p /usage`` call. No test
    here -- or anywhere -- may spend another one.
    """

    REAL_OUTPUT = (
        "Current session: 18% used \u00b7 resets Aug 20, 7:20pm (Asia/Tehran)\n"
        "Current week (all models): 17% used \u00b7 resets Aug 26, 1:30pm (Asia/Tehran)\n")

    def test_month_day_and_time(self):
        now = _local(2026, 8, 20, 15, 50)
        self.assertEqual(usage.parse_reset("Aug 20, 7:20pm (Asia/Tehran)", now),
                         _local(2026, 8, 20, 19, 20))
        self.assertEqual(usage.parse_reset("Aug 26, 1:30pm (Asia/Tehran)", now),
                         _local(2026, 8, 26, 13, 30))

    def test_month_day_and_time_read_from_another_month(self):
        # The same strings six months earlier: the answer is a function of the
        # expression, not of the month the suite runs in.
        now = _local(2026, 2, 3, 12, 0)
        self.assertEqual(usage.parse_reset("Aug 20, 7:20pm (Asia/Tehran)", now),
                         _local(2026, 8, 20, 19, 20))
        self.assertEqual(usage.parse_reset("Aug 26, 1:30pm (Asia/Tehran)", now),
                         _local(2026, 8, 26, 13, 30))

    def test_month_day_on_a_twenty_four_hour_clock(self):
        # The CLI's rendering follows the locale; both forms must land.
        now = _local(2026, 8, 20, 15, 50)
        self.assertEqual(usage.parse_reset("Aug 20, 19:20", now),
                         _local(2026, 8, 20, 19, 20))
        self.assertEqual(usage.parse_reset("Aug 20, 19:20 (Asia/Tehran)", now),
                         _local(2026, 8, 20, 19, 20))

    def test_month_day_around_midnight_and_noon(self):
        now = _local(2026, 8, 20, 10, 0)
        self.assertEqual(usage.parse_reset("Aug 21, 12:05am", now),
                         _local(2026, 8, 21, 0, 5))
        self.assertEqual(usage.parse_reset("Aug 20, 12:05pm", now),
                         _local(2026, 8, 20, 12, 5))

    def test_a_year_is_not_mistaken_for_a_time(self):
        now = _local(2026, 8, 20, 10, 0)
        self.assertEqual(usage.parse_reset("Aug 21 2026", now), _local(2026, 8, 21))

    def test_a_bare_calendar_day_is_still_midnight(self):
        now = _local(2026, 2, 3, 12, 0)
        self.assertEqual(usage.parse_reset("Feb 5", now), _local(2026, 2, 5))

    def test_a_bare_calendar_day_far_past_still_rolls_a_year(self):
        now = _local(2026, 8, 20, 15, 50)
        self.assertEqual(usage.parse_reset("Feb 5", now), _local(2027, 2, 5))

    def test_clock_and_relative_forms_are_unchanged(self):
        now = _local(2026, 8, 20, 15, 50)
        self.assertEqual(usage.parse_reset("7:50pm", now), _local(2026, 8, 20, 19, 50))
        self.assertEqual(usage.parse_reset("7:50am", now), _local(2026, 8, 21, 7, 50))
        self.assertEqual(usage.parse_reset("in 2h 15m", now), now + 2 * 3600 + 15 * 60)
        self.assertEqual(usage.parse_reset("in 45m", now), now + 45 * 60)

    def test_iso_form_is_unchanged(self):
        now = _local(2026, 8, 20, 15, 50)
        self.assertEqual(usage.parse_reset("2026-08-20T19:20:00", now),
                         _local(2026, 8, 20, 19, 20))

    def test_absolute_epochs_are_taken_literally(self):
        # An epoch states a moment outright rather than reconstructing one from
        # a partial expression, so it is not second-guessed here even when it
        # is behind `now` -- `governor.record_poll` is what refuses to store a
        # past reset, whichever branch produced it.
        now = _local(2026, 8, 20, 15, 50)
        self.assertEqual(usage.parse_reset("1755600000", now), 1755600000.0)
        self.assertEqual(usage.parse_reset("1755600000000", now), 1755600000.0)

    def test_a_reset_in_the_past_is_not_a_reset(self):
        # Every one of these expressions describes a future moment, so a result
        # behind `now` is a misparse or a stale reading. "Unknown" is the only
        # honest answer: a past reset tells the governor the window just rolled
        # over, on every single tick, forever.
        self.assertIsNone(
            usage.parse_reset("Aug 20, 7:20pm (Asia/Tehran)", _local(2026, 8, 20, 20, 0)))
        self.assertIsNone(usage.parse_reset("Aug 20, 19:20", _local(2026, 8, 20, 20, 0)))
        # A `Feb 5` only slightly past: too near for the year-roll, still past.
        self.assertIsNone(usage.parse_reset("Feb 5", _local(2026, 2, 5, 14, 0)))
        self.assertIsNone(usage.parse_reset("Feb 5", _local(2026, 3, 1, 9, 0)))

    def test_unparseable_expressions_stay_none(self):
        now = _local(2026, 8, 20, 15, 50)
        for text in ("", "   ", "sometime soon", "Xyz 20, 7:20pm", "Aug 20, 99:99"):
            self.assertIsNone(usage.parse_reset(text, now), text)

    def test_the_real_usage_output(self):
        now = _local(2026, 8, 20, 15, 50)
        reading = usage.parse_usage_text(self.REAL_OUTPUT, now)
        self.assertEqual(reading["session_pct"], 18.0)
        self.assertEqual(reading["week_pct"], 17.0)
        self.assertEqual(reading["session_reset"], _local(2026, 8, 20, 19, 20))
        self.assertEqual(reading["week_reset"], _local(2026, 8, 26, 13, 30))

    def test_the_real_usage_output_leaves_the_lane_admitting(self):
        """The live symptom: the session reset landed 15 hours in the past, so
        every estimate came back `post-reset`/stale and `admit` refused every
        task with "usage unknown; poll first"."""
        now = _local(2026, 8, 20, 15, 50)
        reading = dict(usage.parse_usage_text(self.REAL_OUTPUT, now), ok=True, at=now)
        snapshot = governor.record_poll(governor.blank(), reading, 0)
        estimate = governor.estimate(snapshot, now + 60, 0)
        self.assertFalse(estimate["stale"])
        self.assertEqual(estimate["source"], "measured")
        # And it does not poll again every floor-length tick.
        self.assertFalse(governor.should_poll(snapshot, now + 61, config=CONFIG))


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

    def test_two_isolated_tasks_in_one_repo_take_different_locks(self):
        """Keyed on the task id, so nothing serializes them. Correct only
        because `daemon._start` now gives each one its own checkout -- before
        it did, this was the whole bug."""
        first = scheduler.lock_name(self._task(isolation="worktree"))
        second = scheduler.lock_name(self._task(id="t-0002", isolation="worktree"))
        self.assertNotEqual(first, second)
        self.assertNotEqual(first, scheduler.lock_name(self._task()))

    def test_the_creation_lock_cannot_collide_with_a_task_lock(self):
        """`worktree-add-<repo>` guards one repo's `.git/worktrees`; task ids
        are always `t-NNNN`, so no repository name reaches that namespace."""
        self.assertNotEqual(worktrees.add_lock_name("t-0001"),
                            scheduler.lock_name(self._task(isolation="worktree")))

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


class TestLaneAdmission(unittest.TestCase):
    def _queue(self):
        return {"next_id": 3, "tasks": [
            {"id": "t-0001", "repo": "qpay", "agent": "claude", "state": "queued",
             "priority": 5, "deps": [], "isolation": "repo"},
            {"id": "t-0002", "repo": "poook", "agent": "codex", "state": "queued",
             "priority": 5, "deps": [], "isolation": "repo"},
        ]}

    def _ctx(self, queue, **over):
        ctx = {"mode": "running", "queue": queue, "running": 0,
               "session_pct": 10.0, "week_pct": 10.0, "stale": False,
               "est_cost_pct": 1.0, "config": CONFIG,
               "lock_free": lambda name: True}
        ctx.update(over)
        return ctx

    def test_runnable_filters_by_lane(self):
        queue = self._queue()
        self.assertEqual([t["id"] for t in scheduler.runnable(queue, "claude")],
                         ["t-0001"])
        self.assertEqual([t["id"] for t in scheduler.runnable(queue, "codex")],
                         ["t-0002"])

    def test_runnable_unfiltered_returns_both(self):
        self.assertEqual(len(scheduler.runnable(self._queue())), 2)

    def test_a_task_without_an_agent_is_in_the_claude_lane(self):
        queue = {"next_id": 2, "tasks": [
            {"id": "t-0001", "repo": "qpay", "state": "queued", "priority": 5,
             "deps": [], "isolation": "repo"}]}
        self.assertEqual(len(scheduler.runnable(queue, "claude")), 1)
        self.assertEqual(len(scheduler.runnable(queue, "codex")), 0)

    def test_one_lane_frozen_does_not_block_the_other(self):
        queue = self._queue()
        claude_task, codex_task = queue["tasks"]
        frozen = self._ctx(queue, mode="frozen")
        ok, reason = scheduler.admit(claude_task, frozen)
        self.assertFalse(ok)
        self.assertIn("frozen", reason)
        ok, _ = scheduler.admit(codex_task, self._ctx(queue, mode="running"))
        self.assertTrue(ok)

    def test_repo_lock_is_shared_across_lanes(self):
        """A codex worker and a claude worker must not share a checkout."""
        queue = self._queue()
        queue["tasks"][1]["repo"] = "qpay"
        held = {"repo-qpay"}
        ctx = self._ctx(queue, lock_free=lambda name: name not in held)
        ok, reason = scheduler.admit(queue["tasks"][1], ctx)
        self.assertFalse(ok)
        self.assertIn("busy", reason)

    def test_worktree_isolation_still_bypasses_the_repo_lock(self):
        queue = self._queue()
        queue["tasks"][1]["repo"] = "qpay"
        queue["tasks"][1]["isolation"] = "worktree"
        held = {"repo-qpay"}
        ctx = self._ctx(queue, lock_free=lambda name: name not in held)
        ok, _ = scheduler.admit(queue["tasks"][1], ctx)
        self.assertTrue(ok)

    def test_per_lane_concurrency_counts_only_that_lane(self):
        queue = self._queue()
        ctx = self._ctx(queue, running=0, session_pct=70.0)
        ok, _ = scheduler.admit(queue["tasks"][1], ctx)
        self.assertTrue(ok)
        ctx = self._ctx(queue, running=1, session_pct=70.0)
        ok, reason = scheduler.admit(queue["tasks"][1], ctx)
        self.assertFalse(ok)
        self.assertIn("concurrency", reason)


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

    def test_paused_is_sticky_below_the_soft_limit(self):
        self.assertEqual(winddown.next_mode(winddown.PAUSED, 5.0, 0, CONFIG),
                         winddown.PAUSED)

    def test_paused_is_sticky_above_the_soft_limit(self):
        """A pause that converts to frozen auto-resumes itself later.

        `can_resume` only fires on `frozen`, so a lane the user paused by hand
        while usage was high used to be handed back to the scheduler the moment
        the window reset -- a control command that reported success and did not
        hold, on the only surface the user has.
        """
        self.assertEqual(winddown.next_mode(winddown.PAUSED, 99.0, 0, CONFIG),
                         winddown.PAUSED)
        self.assertEqual(winddown.next_mode(winddown.PAUSED, 99.0, 1, CONFIG),
                         winddown.PAUSED)

    def test_paused_is_sticky_when_the_reading_is_unusable(self):
        self.assertEqual(winddown.next_mode(winddown.PAUSED, None, 0, CONFIG),
                         winddown.PAUSED)
        self.assertEqual(
            winddown.next_mode(winddown.PAUSED, 99.0, 0, CONFIG, stale=True),
            winddown.PAUSED)

    def test_a_paused_lane_never_satisfies_can_resume(self):
        """Nothing but an explicit resume may take a lane out of paused."""
        doc = {"mode": winddown.PAUSED, "armed_resume_at": None}
        self.assertFalse(winddown.can_resume(doc, 3.0, 5100.0, CONFIG, False)[0])

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

    def test_the_configured_sandbox_reaches_the_argv_through_the_worker(self):
        task = {"prompt": "do", "branch": "tg/t-1", "agent": "codex",
                "session_id": None}
        self.assertIn("--approve-for-me", worker.build_command(
            task, "/p/prompt.txt", "/repo", "/unused"))
        self.assertIn("--dangerously-bypass-approvals-and-sandbox",
                      worker.build_command(task, "/p/prompt.txt", "/repo",
                                           "/unused",
                                           config={"codex_sandbox": "bypass"}))

    def test_run_step_hands_the_backend_the_config_it_was_given(self):
        """`run_step` is the only caller of `build_command` that holds the
        config, so a mode that never reaches it is a mode nobody can select."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        task = {"id": "t-0001", "repo": "qpay", "prompt": "do", "agent": "codex",
                "branch": "tg/t-1", "session_id": None}
        seen = []

        class _Process:
            returncode = 0

            def communicate(self, timeout=None):
                return "", None

        def popen(argv, **kwargs):
            seen.append(argv)
            return _Process()

        config = dict(CONFIG, codex_sandbox="workspace-write")
        worker.run_step(task, tmp.name, config, popen=popen, task_dir=tmp.name)
        self.assertEqual(seen[0][-2:], ["-s", "workspace-write"])

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
        self.assertTrue(argv[argv.index("--output-schema") + 1].endswith(
            "status.schema.json"))
        self.assertEqual(argv[argv.index("-o") + 1],
                         os.path.join(self.tmp.name, "last.json"))

    def test_codex_runs_in_its_sandbox_by_default(self):
        """`--approve-for-me` is unattended *and* confined: approvals are
        routed through codex's automatic review, and the workspace is the repo
        the task named. The bypass flag is neither."""
        argv = backends.codex.build_command(
            dict(self.task, agent="codex"), "/p/prompt.txt", "/repo", self.tmp.name)
        self.assertIn("--approve-for-me", argv)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)

    def test_the_configured_sandbox_selects_the_flags(self):
        """One config value, because the reviewed mode is unverified against a
        live codex: if it stalls, the user flips this rather than waiting for a
        code change."""
        cases = {"approve-for-me": ["--approve-for-me"],
                 "read-only": ["-s", "read-only"],
                 "workspace-write": ["-s", "workspace-write"],
                 "danger-full-access": ["-s", "danger-full-access"],
                 "bypass": ["--dangerously-bypass-approvals-and-sandbox"]}
        for mode, expected in cases.items():
            argv = backends.codex.build_command(
                dict(self.task, agent="codex"), "/p/prompt.txt", "/repo",
                self.tmp.name, config={"codex_sandbox": mode})
            self.assertEqual(argv[-len(expected):], expected, mode)
            self.assertEqual(argv[:3], ["codex", "exec", "-"], mode)

    def test_the_default_config_selects_the_sandboxed_mode(self):
        self.assertEqual(config_mod.DEFAULTS["codex_sandbox"], "approve-for-me")
        argv = backends.codex.build_command(
            dict(self.task, agent="codex"), "/p/prompt.txt", "/repo",
            self.tmp.name, config=config_mod.DEFAULTS)
        self.assertIn("--approve-for-me", argv)

    def test_an_unknown_sandbox_falls_back_to_the_confined_one_and_says_so(self):
        """A typo in a security-relevant key must not be read as the most
        permissive thing on the list."""
        with contextlib.redirect_stderr(io.StringIO()) as err:
            argv = backends.codex.build_command(
                dict(self.task, agent="codex"), "/p/prompt.txt", "/repo",
                self.tmp.name, config={"codex_sandbox": "yolo"})
        self.assertIn("--approve-for-me", argv)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertIn("yolo", err.getvalue())

    def test_a_resumed_step_carries_no_flag_the_resume_parser_rejects(self):
        """`codex exec resume` is a different parser from `codex exec`.

        Measured against codex-cli 0.148.0: `codex exec resume -s ... --help`
        and `... --approve-for-me --help` both exit 2 with `unexpected
        argument`. A rejected flag is not a weaker sandbox, it is a step that
        never starts -- so a continuation carries neither, and says the same
        thing with `-c` instead.
        """
        task = dict(self.task, agent="codex", session_id="abc")
        for mode in ("approve-for-me", "read-only", "workspace-write",
                     "danger-full-access", None):
            argv = backends.codex.build_command(
                task, "/p/prompt.txt", "/repo", self.tmp.name,
                config={"codex_sandbox": mode} if mode else None)
            self.assertEqual(argv[:3], ["codex", "exec", "resume"], mode)
            self.assertNotIn("--approve-for-me", argv, mode)
            self.assertNotIn("-s", argv, mode)

    # Every option `codex exec resume` accepts, read from its own --help on
    # codex-cli 0.148.0. `codex exec` takes a wider set -- `-C`, `-s`,
    # `--approve-for-me`, `--add-dir`, `-p` -- and the resume subcommand
    # rejects each of them with `error: unexpected argument`, exit 2, before
    # doing any work. Two flag-shape bugs have now come out of this argv, so
    # the whole shape is asserted rather than one flag at a time.
    RESUME_ACCEPTS = {
        "-c", "--config", "--last", "--all", "--enable", "--disable",
        "-i", "--image", "--strict-config", "-m", "--model",
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-bypass-hook-trust", "--skip-git-repo-check",
        "--ephemeral", "--ignore-user-config", "--ignore-rules",
        "--output-schema", "--json", "-o", "--output-last-message",
    }

    def test_a_sandbox_mode_that_is_not_a_string_takes_the_default_path(self):
        """`{"codex_sandbox": ["bypass"]}` raised `TypeError: unhashable type`
        out of the dict lookup, inside the worker, killing the step rather than
        the value."""
        for mode in (["bypass"], {"mode": "bypass"}, 7, True):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                argv = backends.codex.build_command(
                    dict(self.task, agent="codex"), "/p/prompt.txt", "/repo",
                    self.tmp.name, config={"codex_sandbox": mode})
            self.assertIn("--approve-for-me", argv, repr(mode))
            self.assertNotIn("--dangerously-bypass-approvals-and-sandbox",
                             argv, repr(mode))
            self.assertIn("codex_sandbox", err.getvalue(), repr(mode))

    def test_a_resumed_step_does_not_pass_a_working_directory(self):
        """`-C` is rejected by the resume parser, and unnecessary anywhere:
        `run_step` launches the process with `cwd=cwd` regardless."""
        first = backends.codex.build_command(
            dict(self.task, agent="codex"), "/p/prompt.txt", "/repo",
            self.tmp.name)
        self.assertEqual(first[first.index("-C") + 1], "/repo")

        resumed = backends.codex.build_command(
            dict(self.task, agent="codex", session_id="abc"), "/p/prompt.txt",
            "/repo", self.tmp.name)
        self.assertNotIn("-C", resumed)
        self.assertNotIn("/repo", resumed)

    def test_a_resumed_argv_carries_only_options_the_resume_parser_accepts(self):
        for mode in ("approve-for-me", "read-only", "workspace-write",
                     "danger-full-access", "bypass", None):
            argv = backends.codex.build_command(
                dict(self.task, agent="codex", session_id="abc"),
                "/p/prompt.txt", "/repo", self.tmp.name,
                config={"codex_sandbox": mode} if mode else None)
            self.assertEqual(argv[:4], ["codex", "exec", "resume", "abc"], mode)
            # The bare `-` is the PROMPT positional, not an option: `codex exec
            # resume [SESSION_ID] [PROMPT]`, and "if `-` is used, read from
            # stdin" -- which is how the prompt file is fed.
            self.assertEqual(argv[4], "-", mode)
            offenders = [a for a in argv[5:]
                         if a.startswith("-") and a != "-"
                         and a not in self.RESUME_ACCEPTS]
            self.assertEqual(offenders, [], "%s: %s" % (mode, argv))

    # The confinement a resumed step gets, per configured mode. Verified
    # against codex-cli 0.148.0 with `codex debug prompt-input` -- a local
    # renderer, no session and no request -- in a directory codex does not
    # trust, reading the `sandbox_mode` the model is actually told about:
    #
    #   (no override)                     -> read-only    <- what resumes used to get
    #   -c sandbox_mode="read-only"       -> read-only
    #   -c sandbox_mode="workspace-write" -> workspace-write
    #   -c sandbox_mode="danger-full-access" -> danger-full-access
    RESUME_SANDBOX = {
        "read-only": "read-only",
        # `--approve-for-me` is documented as "route approval requests through
        # automatic review using the workspace-write sandbox", so this is the
        # sandbox half of it, exactly.
        "approve-for-me": "workspace-write",
        "workspace-write": "workspace-write",
        "danger-full-access": "danger-full-access",
    }

    def _resumed(self, mode):
        return backends.codex.build_command(
            dict(self.task, agent="codex", session_id="abc"), "/p/prompt.txt",
            "/repo", self.tmp.name, config={"codex_sandbox": mode})

    def test_the_configured_sandbox_reaches_every_step_not_just_the_first(self):
        """`codex exec resume` takes no sandbox flag, so a continuation used to
        fall back to codex's own trust configuration -- which resolves on the
        **git repository root**, not on an ancestor. Measured on this machine:
        `~/Projects` is trusted and so is `~/Projects/qpay-backend`, but
        `~/Projects/agent-workflow-skills` is a repo with no entry of its own
        and renders read-only, as does every linked worktree, whose repo root is
        itself. Step one wrote; step two onward could read and think and change
        nothing, and reported success for it."""
        for mode, sandbox in self.RESUME_SANDBOX.items():
            argv = self._resumed(mode)
            self.assertIn("-c", argv, mode)
            self.assertIn('sandbox_mode="%s"' % sandbox, argv, mode)
            # Paired: `-c` immediately precedes the value it carries.
            self.assertEqual(argv[argv.index('sandbox_mode="%s"' % sandbox) - 1],
                             "-c", mode)

    def test_a_resumed_step_is_told_not_to_ask_for_an_escalation(self):
        """Nothing can answer one. `--approve-for-me` routes approvals through
        automatic review and the resume parser rejects it; no config key was
        found that turns that reviewer on (`approval_policy="granular"` is a
        struct wanting `sandbox_approval` and `rules` -- execpolicy matching,
        not automatic review). With the policy left unset the model is handed
        the full escalation instructions and invited to ask, unattended, for
        something no reviewer and no human will answer: a step that stalls
        until `step_timeout` kills it. `never` renders "Approval policy is
        currently never. Do not provide the `sandbox_permissions` for any
        reason, commands will be rejected", so it works inside the sandbox or
        reports blocked."""
        for mode in self.RESUME_SANDBOX:
            argv = self._resumed(mode)
            self.assertIn('approval_policy="never"', argv, mode)
            self.assertEqual(argv[argv.index('approval_policy="never"') - 1],
                             "-c", mode)

    def test_the_first_step_is_unchanged_by_any_of_it(self):
        """The `-c` translation is for the resume parser only; a first step
        keeps the flags that were already measured against the real tool."""
        for mode in ("read-only", "approve-for-me", "workspace-write",
                     "danger-full-access", "bypass"):
            argv = backends.codex.build_command(
                dict(self.task, agent="codex"), "/p/prompt.txt", "/repo",
                self.tmp.name, config={"codex_sandbox": mode})
            self.assertNotIn('sandbox_mode="%s"' % mode, argv, mode)
            self.assertNotIn('approval_policy="never"', argv, mode)

    def test_strict_config_is_not_used(self):
        """It rejects unrecognized fields in the user's own `config.toml`, so
        here it would let one stale key fail every continuation of every task
        while first steps kept working. It would not catch a typo in the keys
        emitted above either: an unrecognized `-c` key is silently ignored,
        while a bad *value* already errors loudly on its own
        (`unknown variant ... expected one of read-only, workspace-write,
        danger-full-access`)."""
        for mode in ("approve-for-me", "bypass"):
            self.assertNotIn("--strict-config", self._resumed(mode), mode)

    def test_an_unknown_mode_resumes_into_the_default_sandbox_too(self):
        with contextlib.redirect_stderr(io.StringIO()):
            argv = self._resumed("yolo")
        self.assertIn('sandbox_mode="workspace-write"', argv)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)

    def test_an_explicit_bypass_is_the_one_mode_a_resume_still_takes(self):
        argv = backends.codex.build_command(
            dict(self.task, agent="codex", session_id="abc"), "/p/prompt.txt",
            "/repo", self.tmp.name, config={"codex_sandbox": "bypass"})
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", argv)

    def test_a_safe_build_asks_for_no_sandbox_flag_at_all(self):
        """`unsafe=False` is unchanged: it emits nothing and lets codex decide,
        whatever the configured mode says."""
        argv = backends.codex.build_command(
            dict(self.task, agent="codex"), "/p/prompt.txt", "/repo",
            self.tmp.name, unsafe=False, config={"codex_sandbox": "bypass"})
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertNotIn("--approve-for-me", argv)
        self.assertNotIn("-s", argv)

    def test_claude_has_no_sandbox_option_and_ignores_the_key(self):
        """codex-only, deliberately: `claude` has no equivalent flag."""
        argv = backends.claude.build_command(
            self.task, "/p/prompt.txt", "/repo", self.tmp.name,
            config={"codex_sandbox": "read-only"})
        self.assertNotIn("-s", argv)
        self.assertNotIn("--approve-for-me", argv)
        self.assertIn("--dangerously-skip-permissions", argv)

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
        with open(path) as fh:
            body = fh.read()
        self.assertTrue(body.startswith("ship it"))
        self.assertIn("tg/t-0002", body)

    def test_prompt_with_shell_metacharacters_survives_verbatim(self):
        """The reason the prompt is a file: chat text is arbitrary."""
        nasty = 'rm -rf $HOME; echo "`whoami`" && exit 1'
        task = {"id": "t-0003", "repo": "qpay", "prompt": nasty,
                "branch": "tg/t-0003", "agent": "claude"}
        with open(worker.write_prompt(task, self.tmp.name)) as fh:
            self.assertIn(nasty, fh.read())

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
            stdin = kwargs.get("stdin")
            recorded["stdin"] = stdin
            recorded["stdin_content"] = stdin.read() if stdin else None
            return FakeProcess()

        task = {"id": "t-0004", "repo": "qpay", "prompt": "secret words",
                "branch": "tg/t-0004", "agent": "claude", "session_id": None}
        worker.run_step(task, self.tmp.name, CONFIG, popen=fake_popen,
                        task_dir=self.tmp.name)
        self.assertNotIn("secret words", " ".join(recorded["argv"]))
        self.assertIsNotNone(recorded["stdin"])
        self.assertIn("secret words", recorded["stdin_content"])


class TestRepoDiscovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "Projects")
        for name in ("qpay-backend", "poook", "notes"):
            os.makedirs(os.path.join(self.root, name))
        for name in ("qpay-backend", "poook"):
            os.makedirs(os.path.join(self.root, name, ".git"))

    def test_git_folders_are_dispatchable(self):
        found = repos.discover(root=self.root)
        self.assertTrue(found["qpay-backend"]["git"])
        self.assertTrue(found["poook"]["git"])

    def test_non_git_folders_are_listed_but_not_dispatchable(self):
        found = repos.discover(root=self.root)
        self.assertIn("notes", found)
        self.assertFalse(found["notes"]["git"])
        self.assertNotIn("notes", repos.dispatchable(found))

    def test_resolve_refuses_a_non_git_folder(self):
        self.assertIsNone(repos.resolve("notes", root=self.root))
        self.assertIn("no git", repos.reject_reason("notes", repos.discover(root=self.root)))

    def test_resolve_refuses_an_unknown_alias(self):
        self.assertIsNone(repos.resolve("nope", root=self.root))
        self.assertIn("unknown", repos.reject_reason("nope", repos.discover(root=self.root)))

    def test_resolve_returns_the_path_for_a_git_folder(self):
        self.assertEqual(repos.resolve("poook", root=self.root),
                         os.path.join(self.root, "poook"))

    def test_the_dispatch_source_repo_is_never_dispatchable(self):
        """The bot must not run an unattended agent in its own checkout.

        A worker there does `git checkout -B tg/<id>` in the very tree the
        daemon's code is read from: it switches the branch under anyone
        working in it, and the next restart runs whatever the worker left.
        """
        source = repos.source_repo()
        self.assertIsNotNone(source)
        self.assertTrue(os.path.isdir(os.path.join(source, ".git")))
        found = repos.discover(root=os.path.dirname(source))
        self.assertNotIn(os.path.basename(source), found)

    def test_the_source_repo_can_be_kept_when_asked(self):
        """The exclusion is a default, not a law -- tests need the escape."""
        source = repos.source_repo()
        found = repos.discover(root=os.path.dirname(source), drop_source=False)
        self.assertIn(os.path.basename(source), found)

    def test_exclude_drops_a_named_path(self):
        found = repos.discover(root=self.root, exclude=[
            os.path.join(self.root, "poook")])
        self.assertNotIn("poook", found)
        self.assertIn("qpay-backend", found)

    def test_exclusion_survives_a_symlinked_path(self):
        """Compared by realpath, so an equivalent spelling still matches."""
        link = os.path.join(self.tmp.name, "link-to-poook")
        os.symlink(os.path.join(self.root, "poook"), link)
        found = repos.discover(root=self.root, exclude=[link])
        self.assertNotIn("poook", found)

    def test_linked_worktree_is_dispatchable(self):
        """A linked worktree's .git is a file holding a gitdir: pointer."""
        worktree = os.path.join(self.root, "linked")
        os.makedirs(worktree)
        with open(os.path.join(worktree, ".git"), "w") as fh:
            fh.write("gitdir: /somewhere/.git/worktrees/linked\n")
        found = repos.discover(root=self.root)
        self.assertTrue(found["linked"]["git"])
        self.assertIn("linked", repos.dispatchable(found))
        self.assertEqual(repos.resolve("linked", root=self.root), worktree)

    def test_hidden_folders_are_ignored(self):
        os.makedirs(os.path.join(self.root, ".cache"))
        self.assertNotIn(".cache", repos.discover(root=self.root))

    def test_files_are_ignored(self):
        with open(os.path.join(self.root, "README.md"), "w") as fh:
            fh.write("x")
        self.assertNotIn("README.md", repos.discover(root=self.root))

    def test_overrides_add_paths_outside_the_root(self):
        outside = os.path.join(self.tmp.name, "elsewhere")
        os.makedirs(os.path.join(outside, ".git"))
        found = repos.discover(root=self.root, overrides={"other": outside})
        self.assertEqual(found["other"]["path"], outside)
        self.assertTrue(found["other"]["git"])

    def test_missing_root_is_empty_not_an_error(self):
        self.assertEqual(repos.discover(root=os.path.join(self.tmp.name, "gone")), {})

    def test_render_marks_what_cannot_be_dispatched(self):
        text = repos.render(repos.discover(root=self.root))
        self.assertIn("qpay-backend", text)
        self.assertIn("notes", text)
        self.assertIn("no git", text)
        self.assertIn("3 folders", text)


class TestAgentVerbs(unittest.TestCase):
    def test_claude_verb_with_repo(self):
        command = parser.parse("claude fix the failing auth test on qpay-backend")
        self.assertEqual(command["kind"], "run")
        self.assertEqual(command["agent"], "claude")
        self.assertEqual(command["repo"], "qpay-backend")
        self.assertEqual(command["prompt"], "fix the failing auth test")

    def test_codex_verb_with_repo(self):
        command = parser.parse("codex bump the deps on poook")
        self.assertEqual((command["agent"], command["repo"], command["prompt"]),
                         ("codex", "poook", "bump the deps"))

    def test_agent_colon_form(self):
        command = parser.parse("codex qpay: rerun the migration")
        self.assertEqual((command["agent"], command["repo"], command["prompt"]),
                         ("codex", "qpay", "rerun the migration"))

    def test_agent_verb_with_worktree(self):
        command = parser.parse("codex audit the deps on escrow in a worktree")
        self.assertEqual(command["isolation"], "worktree")
        self.assertEqual(command["repo"], "escrow")

    def test_bare_agent_verb_demands_a_repo(self):
        command = parser.parse("claude fix the failing auth test")
        self.assertEqual(command["kind"], "need_repo")
        self.assertEqual(command["agent"], "claude")

    def test_run_verb_still_defaults_to_claude(self):
        command = parser.parse("run the migration on qpay-backend")
        self.assertEqual(command["kind"], "run")
        self.assertEqual(command["agent"], "claude")

    def test_run_verb_wins_over_a_prompt_starting_with_an_agent_name(self):
        """`run claude tests on qpay` is a run, whose prompt is `claude tests`."""
        command = parser.parse("run claude tests on qpay")
        self.assertEqual(command["agent"], "claude")
        self.assertEqual(command["prompt"], "claude tests")

    def test_bare_agent_word_alone_is_not_a_run(self):
        self.assertEqual(parser.parse("claude")["kind"], "unparsed")


class TestReadVerbs(unittest.TestCase):
    def test_ping_parses_bare_and_case_insensitively(self):
        for text in ("ping", "PING", "/ping", "  ping  "):
            self.assertEqual(parser.parse(text)["kind"], "ping", text)

    def test_ping_with_trailing_words_is_not_a_ping(self):
        """`ping the server` is free-form, not a connectivity check."""
        self.assertNotEqual(parser.parse("ping the server")["kind"], "ping")

    def test_usage_is_free_by_default(self):
        command = parser.parse("usage")
        self.assertEqual(command["kind"], "usage")
        self.assertFalse(command["poll"])

    def test_usage_poll_spends_a_request(self):
        for text in ("usage poll", "usage --poll", "/usage poll"):
            command = parser.parse(text)
            self.assertEqual(command["kind"], "usage", text)
            self.assertTrue(command["poll"], text)

    def test_sessions_unfiltered(self):
        command = parser.parse("sessions")
        self.assertEqual(command["kind"], "sessions")
        self.assertIsNone(command["project"])

    def test_sessions_filtered(self):
        self.assertEqual(parser.parse("sessions qpay")["project"], "qpay")

    def test_repos_has_aliases(self):
        for text in ("repos", "projects", "/repos"):
            self.assertEqual(parser.parse(text)["kind"], "repos", text)

    def test_pause_defaults_to_both_lanes(self):
        command = parser.parse("pause")
        self.assertEqual(command["kind"], "pause")
        self.assertIsNone(command["lane"])

    def test_pause_can_name_a_lane(self):
        self.assertEqual(parser.parse("pause codex")["lane"], "codex")
        self.assertEqual(parser.parse("resume claude")["lane"], "claude")

    def test_pause_with_a_nonsense_lane_is_not_a_pause(self):
        self.assertNotEqual(parser.parse("pause everything")["kind"], "pause")


class TestVolume(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 1_800_000_000.0

    def _claude_log(self, project, records):
        directory = os.path.join(self.tmp.name, "claude", project)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "s.jsonl")
        with open(path, "w") as fh:
            for record in records:
                fh.write(json.dumps(record) + "\n")
        os.utime(path, (self.now, self.now))
        return path

    def _turn(self, offset, message_id, total):
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S",
                              time.localtime(self.now - offset))
        return {"timestamp": stamp, "message": {
            "id": message_id, "model": "claude-opus-5",
            "usage": {"input_tokens": total - 10, "output_tokens": 10,
                      "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 0}}}

    def test_clock_is_injected_not_global(self):
        """The port's whole point: NOW was a module constant."""
        self._claude_log("-home-navid-Projects-qpay", [self._turn(60, "m1", 1000)])
        win, _, _, _ = volume.claude_usage(
            self.now, root=os.path.join(self.tmp.name, "claude"))
        self.assertEqual(win["5h"], 1000)
        # Same tree, a clock two days later: nothing is in the 5h window.
        win, _, _, _ = volume.claude_usage(
            self.now + 2 * 86400, root=os.path.join(self.tmp.name, "claude"))
        self.assertEqual(win["5h"], 0)

    def test_duplicate_message_ids_count_once(self):
        """A resumed session replays earlier messages into a new file."""
        self._claude_log("-home-navid-Projects-qpay",
                         [self._turn(60, "m1", 1000), self._turn(50, "m1", 1000)])
        win, _, _, _ = volume.claude_usage(
            self.now, root=os.path.join(self.tmp.name, "claude"))
        self.assertEqual(win["5h"], 1000)

    def test_project_attribution(self):
        self._claude_log("-home-navid-Projects-qpay", [self._turn(60, "m1", 500)])
        self._claude_log("-home-navid-Projects-poook", [self._turn(60, "m2", 300)])
        _, proj, _, _ = volume.claude_usage(
            self.now, root=os.path.join(self.tmp.name, "claude"))
        self.assertEqual(proj["qpay"], 500)
        self.assertEqual(proj["poook"], 300)

    def test_codex_takes_the_newest_total_per_session(self):
        directory = os.path.join(self.tmp.name, "codex", "2026", "08", "20")
        os.makedirs(directory)
        path = os.path.join(directory, "r.jsonl")
        with open(path, "w") as fh:
            fh.write(json.dumps({"payload": {"cwd": "/home/navid/Projects/qpay"}}) + "\n")
            fh.write(json.dumps({"payload": {"info": {"total_token_usage": {
                "total_tokens": 100, "output_tokens": 10}}}}) + "\n")
            fh.write(json.dumps({"payload": {"info": {"total_token_usage": {
                "total_tokens": 900, "output_tokens": 90}}}}) + "\n")
        os.utime(path, (self.now, self.now))
        totals, _ = volume.codex_usage(
            self.now, root=os.path.join(self.tmp.name, "codex"))
        self.assertEqual(totals["all"], 900)

    def test_codex_falls_back_to_parts_when_total_is_absent(self):
        """Older records carry only the sub-fields; they must not count zero."""
        directory = os.path.join(self.tmp.name, "codex", "2026", "08", "19")
        os.makedirs(directory)
        path = os.path.join(directory, "r.jsonl")
        with open(path, "w") as fh:
            fh.write(json.dumps({"payload": {"cwd": "/home/navid/Projects/qpay"}}) + "\n")
            fh.write(json.dumps({"payload": {"info": {"total_token_usage": {
                "input_tokens": 700, "output_tokens": 200}}}}) + "\n")
        os.utime(path, (self.now, self.now))
        totals, _ = volume.codex_usage(
            self.now, root=os.path.join(self.tmp.name, "codex"))
        self.assertEqual(totals["all"], 900)
        self.assertEqual(totals["out"], 200)

    def test_human_scales(self):
        self.assertEqual(volume.human(1_500_000), "1.5M")
        self.assertEqual(volume.human(2_000), "2.0K")
        self.assertEqual(volume.human(42), "42")

    def test_render_never_spends_a_request(self):
        """`usage` must be free; only `usage poll` may spend a request.

        The subprocess mock only catches an *unswallowed* call: the original
        `plan_limits()` wrapped its `subprocess.run` in `except Exception`, so
        a reintroduced copy with that same swallowing pattern would eat the
        injected error here and still return normally. The real backstop is
        the `hasattr` assertion below -- it fails the moment `plan_limits`
        exists again, regardless of how carefully it hides its own call.
        """
        empty = os.path.join(self.tmp.name, "none")
        boom = AssertionError("render() spawned a subprocess")
        with mock.patch("subprocess.run", side_effect=boom), \
                mock.patch("subprocess.Popen", side_effect=boom):
            text = volume.render(self.now, claude_root=empty, codex_root=empty)
        self.assertIn("CLAUDE", text)
        self.assertFalse(hasattr(volume, "plan_limits"),
                         "plan_limits must not survive the port")

    def test_render_on_an_empty_tree_does_not_raise(self):
        text = volume.render(self.now,
                             claude_root=os.path.join(self.tmp.name, "none"),
                             codex_root=os.path.join(self.tmp.name, "none"))
        self.assertIn("CLAUDE", text)


class TestSessions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 1_800_000_000.0
        self.root = os.path.join(self.tmp.name, "projects")

    def _session(self, project, sid, records, age=60):
        directory = os.path.join(self.root, project)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, sid + ".jsonl")
        with open(path, "w") as fh:
            for record in records:
                fh.write(json.dumps(record) + "\n")
        os.utime(path, (self.now - age, self.now - age))
        return path

    def test_scan_reads_title_cwd_and_turn_counts(self):
        path = self._session("-home-navid-Projects-qpay", "abc", [
            {"type": "ai-title", "aiTitle": "Fix the auth test"},
            {"type": "user", "cwd": "/home/navid/Projects/qpay-backend"},
            {"type": "assistant"},
            {"type": "last-prompt", "lastPrompt": "now run the tests"}])
        summary = sessions.scan(path, self.now)
        self.assertEqual(summary["title"], "Fix the auth test")
        self.assertEqual(summary["n_user"], 1)
        self.assertEqual(summary["n_asst"], 1)
        self.assertEqual(summary["last_prompt"], "now run the tests")

    def test_empty_stub_sessions_are_skipped(self):
        path = self._session("-home-navid-Projects-qpay", "empty", [
            {"type": "mode", "mode": "default"}])
        self.assertIsNone(sessions.scan(path, self.now))

    def test_collect_is_newest_first(self):
        self._session("-home-navid-Projects-qpay", "old", [
            {"type": "user"}, {"type": "ai-title", "aiTitle": "older"}], age=9000)
        self._session("-home-navid-Projects-qpay", "new", [
            {"type": "user"}, {"type": "ai-title", "aiTitle": "newer"}], age=60)
        titles = [s["title"] for s in sessions.collect(self.now, root=self.root)]
        self.assertEqual(titles[0], "newer")

    def test_collect_filters_by_project(self):
        self._session("-home-navid-Projects-qpay", "a", [
            {"type": "user", "cwd": "/home/navid/Projects/qpay-backend"}])
        self._session("-home-navid-Projects-poook", "b", [
            {"type": "user", "cwd": "/home/navid/Projects/poook"}])
        rows = sessions.collect(self.now, root=self.root, project="qpay")
        self.assertEqual(len(rows), 1)

    def test_collect_respects_the_limit(self):
        for index in range(5):
            self._session("-home-navid-Projects-qpay", "s%d" % index,
                          [{"type": "user"}], age=60 + index)
        self.assertEqual(len(sessions.collect(self.now, root=self.root, limit=3)), 3)

    def test_render_on_an_empty_tree(self):
        self.assertIn("no sessions", sessions.render(
            self.now, root=os.path.join(self.tmp.name, "gone")).lower())

    def test_render_is_plain_text(self):
        """Chat replies set no parse mode, so markdown tables would be noise."""
        self._session("-home-navid-Projects-qpay", "a", [
            {"type": "user"}, {"type": "ai-title", "aiTitle": "Fix auth"}])
        text = sessions.render(self.now, root=self.root)
        self.assertIn("Fix auth", text)
        self.assertNotIn("|---", text)

    def test_launch_is_not_exposed(self):
        """A launch from chat produces a terminal nobody is typing into."""
        self.assertFalse(hasattr(sessions, "cmd_launch"))
        self.assertFalse(hasattr(sessions, "launch"))


class TestTwoLaneDaemon(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._previous = os.environ.get("DISPATCH_HOME")
        os.environ["DISPATCH_HOME"] = os.path.join(self.tmp.name, "home")
        self.addCleanup(self._restore)
        # The daemon reads this path when it builds its own transport. Nothing
        # here may so much as stat the developer's real channel token.
        token = mock.patch.dict(
            os.environ,
            {"DISPATCH_TOKEN_ENV": os.path.join(self.tmp.name, "absent.env")})
        token.start()
        self.addCleanup(token.stop)
        self.projects = os.path.join(self.tmp.name, "Projects")
        for name in ("qpay", "poook"):
            os.makedirs(os.path.join(self.projects, name, ".git"))
        self.logs = os.path.join(self.tmp.name, "empty-logs")
        os.makedirs(self.logs)
        self.now = 1_800_000_000.0

    def _restore(self):
        if self._previous is None:
            os.environ.pop("DISPATCH_HOME", None)
        else:
            os.environ["DISPATCH_HOME"] = self._previous

    def _surface(self):
        return ({"claude": "running", "codex": "running"},
                {"claude": {"session_pct": 1.0, "week_pct": 1.0, "stale": False,
                            "source": "measured", "resets_at": None},
                 "codex": {"session_pct": 1.0, "week_pct": 1.0, "stale": False,
                           "source": "codex-logs", "resets_at": None}})

    def _say(self, daemon, text, now=None):
        modes, readings = self._surface()
        return daemon.handle_command(text, modes, readings, governor.blank(),
                                     self.now if now is None else now, 0,
                                     chat_id="1")

    def test_free_form_proposes_instead_of_queueing(self):
        """A typo must not become an unattended agent in a real repository."""
        daemon = self._daemon()
        daemon.freeform = lambda text: ([{"repo": "qpay", "prompt": "tidy deps"}], None)
        reply = self._say(daemon, "tidy the deps everywhere")
        self.assertIn("parsed as:", reply)
        self.assertIn('claude "tidy deps" on qpay', reply)
        self.assertIn("reply `yes` to queue", reply)
        self.assertEqual(state.read_queue()["tasks"], [])

    def test_yes_queues_the_proposal(self):
        daemon = self._daemon()
        daemon.freeform = lambda text: ([{"repo": "qpay", "prompt": "tidy deps"}], None)
        self._say(daemon, "tidy the deps everywhere")
        reply = self._say(daemon, "yes")
        self.assertIn("queued t-0001", reply)
        task = state.read_queue()["tasks"][0]
        self.assertEqual((task["repo"], task["prompt"]), ("qpay", "tidy deps"))
        self.assertEqual(state.read_state()["pending_confirm"], {})

    def test_no_drops_the_proposal(self):
        daemon = self._daemon()
        daemon.freeform = lambda text: ([{"repo": "qpay", "prompt": "tidy deps"}], None)
        self._say(daemon, "tidy the deps everywhere")
        self.assertEqual(self._say(daemon, "no"), "dropped")
        self.assertEqual(state.read_queue()["tasks"], [])
        self.assertEqual(self._say(daemon, "yes"), "nothing to confirm")

    def test_a_proposal_expires_rather_than_queueing_later(self):
        """A `yes` typed hours later must not queue forgotten work."""
        from dispatch import daemon as daemon_mod
        daemon = self._daemon()
        daemon.freeform = lambda text: ([{"repo": "qpay", "prompt": "tidy deps"}], None)
        self._say(daemon, "tidy the deps everywhere")
        reply = self._say(daemon, "yes", now=self.now + daemon_mod.PENDING_TTL + 1)
        self.assertIn("expired", reply)
        self.assertEqual(state.read_queue()["tasks"], [])
        self.assertEqual(self._say(daemon, "yes"), "nothing to confirm")

    def test_yes_with_nothing_pending_says_so(self):
        self.assertEqual(self._say(self._daemon(), "yes"), "nothing to confirm")

    def test_a_second_proposal_replaces_the_first(self):
        """One outstanding proposal per chat, so `yes` is never ambiguous."""
        daemon = self._daemon()
        daemon.freeform = lambda text: ([{"repo": "qpay", "prompt": text}], None)
        self._say(daemon, "first thing")
        self._say(daemon, "second thing")
        self._say(daemon, "yes")
        prompts = [t["prompt"] for t in state.read_queue()["tasks"]]
        self.assertEqual(prompts, ["second thing"])

    def test_an_unresolvable_repo_is_named_before_the_yes(self):
        daemon = self._daemon()
        daemon.freeform = lambda text: ([{"repo": "qpay", "prompt": "good"},
                                         {"repo": "typo-repo", "prompt": "bad"}], None)
        reply = self._say(daemon, "do two things")
        self.assertIn('claude "good" on qpay', reply)
        self.assertIn("skipping", reply)
        self.assertIn("typo-repo", reply)
        self._say(daemon, "yes")
        self.assertEqual([t["prompt"] for t in state.read_queue()["tasks"]], ["good"])

    def test_nothing_dispatchable_parks_no_proposal(self):
        daemon = self._daemon()
        daemon.freeform = lambda text: ([{"repo": "typo-repo", "prompt": "x"}], None)
        reply = self._say(daemon, "do a thing")
        self.assertIn("nothing dispatchable", reply)
        self.assertEqual(state.read_state()["pending_confirm"], {})
        self.assertEqual(state.read_queue()["tasks"], [])

    def test_ping_answers_pong(self):
        daemon = self._daemon()
        self.assertEqual(daemon.handle_command(
            "ping", {"claude": "running", "codex": "running"},
            {"claude": {"session_pct": 1.0, "week_pct": 1.0, "stale": False,
                        "source": "measured", "resets_at": None},
             "codex": {"session_pct": 1.0, "week_pct": 1.0, "stale": False,
                       "source": "codex-logs", "resets_at": None}},
            governor.blank(), self.now, 0), "pong")

    def test_ping_answers_even_when_everything_else_would_raise(self):
        """ping is what you send when you do not know the daemon is alive.

        Every other argument is poisoned here: no readings, no modes, a
        snapshot that is not a dict, and a clock that raises. Any command that
        consults state, the governor, or the volume report would blow up on
        these; ping must not, because the moment you need it is the moment the
        rest of the surface is broken.
        """
        daemon = self._daemon()

        def exploding_clock():
            raise AssertionError("ping must not read the clock")

        daemon.clock = exploding_clock
        daemon.volume_block = lambda now: 1 / 0
        self.assertEqual(daemon.handle_command("ping", {}, {}, None, None, None),
                         "pong")

    def _daemon(self, claude_pct=10.0, codex_pct=10.0, run_step=None,
                checkpoint=None):
        from dispatch import chat as chat_mod
        from dispatch import daemon as daemon_mod
        daemon = daemon_mod.Daemon(
            # The repos here are `.git` directories, not repositories: a real
            # checkpoint cannot succeed against them, and a settle that ignores
            # that is the bug `test_a_failed_checkpoint_...` covers. Injected,
            # so these tests assert on lane behaviour and the integration suite
            # keeps the real git path.
            checkpoint=checkpoint or (lambda task, cwd, message: {
                "ok": True, "committed": True}),
            config={"projects_root": self.projects, "chat_allowlist": ["1"]},
            clock=lambda: self.now,
            poll_usage=lambda: {"ok": True, "at": self.now,
                                "session_pct": claude_pct, "session_reset": self.now + 3600,
                                "week_pct": 5.0, "week_reset": self.now + 86400},
            count_tokens=lambda: 0,
            codex_estimate=lambda now: {
                "session_pct": codex_pct, "week_pct": 0.0,
                "source": "codex-logs", "stale": False, "resets_at": None},
            chat=chat_mod.NullChat(),
            run_step=run_step or (lambda task, cwd: {
                "status": "complete", "summary": "ok", "next": "",
                "output": "", "limit_reset_at": None, "session_id": None,
                "timed_out": False}),
            executor=daemon_mod.InlineExecutor())
        # The volume report globs whole log trees. Point it at an empty one so
        # no test reads the developer's real ~/.claude or ~/.codex.
        daemon.volume_block = lambda now: volume.render(
            now, claude_root=self.logs, codex_root=self.logs)
        self.addCleanup(self._release_locks, daemon)
        return daemon

    @staticmethod
    def _release_locks(daemon):
        """A reap drops a worker's repo lock. Tests that never reap drop it here."""
        for entry in daemon.running.values():
            state.release(entry["lock"])
        daemon.running.clear()

    def test_tick_returns_a_mode_per_lane(self):
        result = self._daemon().tick()
        self.assertEqual(set(result["mode"]), {"claude", "codex"})

    def test_a_frozen_claude_lane_still_dispatches_codex(self):
        started = []
        daemon = self._daemon(claude_pct=99.0, codex_pct=5.0,
                              run_step=lambda task, cwd: started.append(task["id"]) or {
                                  "status": "complete", "summary": "", "next": "",
                                  "output": "", "limit_reset_at": None,
                                  "session_id": None, "timed_out": False})
        with state.mutate_queue() as queue:
            state.new_task(queue, "qpay", "claude work", agent="claude")
            state.new_task(queue, "poook", "codex work", agent="codex")
        daemon.tick()
        tasks = {t["id"]: t for t in state.read_queue()["tasks"]}
        self.assertEqual(tasks["t-0002"]["state"], "done")
        self.assertIn(tasks["t-0001"]["state"], ("queued", "paused"))

    def test_the_repo_lock_is_shared_between_lanes(self):
        """Two tasks on one repo must not run in the same tick."""
        started = []

        def run_step(task, cwd):
            started.append(task["id"])
            return {"status": "continue", "summary": "", "next": "", "output": "",
                    "limit_reset_at": None, "session_id": None, "timed_out": False}

        daemon = self._daemon(run_step=run_step)
        daemon.executor = _NeverFinishes()
        with state.mutate_queue() as queue:
            state.new_task(queue, "qpay", "a", agent="claude")
            state.new_task(queue, "qpay", "b", agent="codex")
        daemon.tick()
        self.assertEqual(len(daemon.running), 1)

    def test_a_busy_repo_does_not_stall_the_rest_of_the_lane(self):
        """The shared lock is an admission check, not a failed start.

        A codex task blocked by a claude worker has to be stepped over, so the
        next codex task in the queue still gets its turn this tick. Letting it
        reach `_start` and fail on the flock would end the lane's dispatch pass.
        """
        daemon = self._daemon()
        daemon.executor = _NeverFinishes()
        with state.mutate_queue() as queue:
            state.new_task(queue, "qpay", "claude holds this repo", agent="claude")
            state.new_task(queue, "qpay", "codex wants the same repo", agent="codex")
            state.new_task(queue, "poook", "codex elsewhere", agent="codex")
        daemon.tick()
        self.assertEqual(sorted(daemon.running), ["t-0001", "t-0003"])

    def test_unknown_repo_is_refused_with_the_dispatchable_list(self):
        daemon = self._daemon()
        reply = daemon.handle_command(
            "claude do a thing on nope", {"claude": "running", "codex": "running"},
            {"claude": {"session_pct": 1.0, "week_pct": 1.0, "stale": False,
                        "source": "measured", "resets_at": None},
             "codex": {"session_pct": 1.0, "week_pct": 1.0, "stale": False,
                       "source": "codex-logs", "resets_at": None}},
            governor.blank(), self.now, 0)
        self.assertIn("unknown repo", reply)
        self.assertIn("qpay", reply)

    def test_bare_agent_verb_is_refused_with_the_repo_list(self):
        daemon = self._daemon()
        reply = daemon.handle_command(
            "claude fix the auth test", {"claude": "running", "codex": "running"},
            {"claude": {"session_pct": 1.0, "week_pct": 1.0, "stale": False,
                        "source": "measured", "resets_at": None},
             "codex": {"session_pct": 1.0, "week_pct": 1.0, "stale": False,
                       "source": "codex-logs", "resets_at": None}},
            governor.blank(), self.now, 0)
        self.assertIn("need a repo", reply)
        self.assertIn("qpay", reply)

    def test_codex_verb_enqueues_into_the_codex_lane(self):
        daemon = self._daemon()
        daemon.executor = _NeverFinishes()
        daemon.handle_command(
            "codex bump deps on poook", {"claude": "running", "codex": "running"},
            {"claude": {"session_pct": 1.0, "week_pct": 1.0, "stale": False,
                        "source": "measured", "resets_at": None},
             "codex": {"session_pct": 1.0, "week_pct": 1.0, "stale": False,
                       "source": "codex-logs", "resets_at": None}},
            governor.blank(), self.now, 0)
        self.assertEqual(state.read_queue()["tasks"][0]["agent"], "codex")

    def test_pause_one_lane_leaves_the_other_running(self):
        daemon = self._daemon()
        modes = {"claude": "running", "codex": "running"}
        readings = {"claude": {"session_pct": 1.0, "week_pct": 1.0, "stale": False,
                               "source": "measured", "resets_at": None},
                    "codex": {"session_pct": 1.0, "week_pct": 1.0, "stale": False,
                              "source": "codex-logs", "resets_at": None}}
        daemon.handle_command("pause codex", modes, readings,
                              governor.blank(), self.now, 0)
        doc = state.read_state()
        self.assertEqual(doc["mode"]["codex"], "paused")
        self.assertEqual(doc["mode"]["claude"], "running")

    def test_usage_without_poll_spends_nothing(self):
        polled = []
        daemon = self._daemon()
        daemon.poll_usage = lambda: polled.append(1) or {"ok": False}
        readings = {"claude": {"session_pct": 41.0, "week_pct": 62.0, "stale": False,
                               "source": "projected", "resets_at": None},
                    "codex": {"session_pct": 12.0, "week_pct": 100.0, "stale": False,
                              "source": "codex-logs", "resets_at": None}}
        reply = daemon.usage_reply(False, governor.blank(), readings, self.now, 0)
        self.assertEqual(polled, [])
        self.assertIn("41", reply)
        self.assertIn("12", reply)

    def test_usage_poll_respects_the_floor(self):
        daemon = self._daemon()
        polled = []
        daemon.poll_usage = lambda: polled.append(1) or {
            "ok": True, "at": self.now, "session_pct": 44.0,
            "session_reset": self.now + 60, "week_pct": 1.0, "week_reset": None}
        snapshot = dict(governor.blank(), polled_at=self.now - 5,
                        session_pct=40.0, tokens_at_poll=0)
        readings = {"claude": governor.estimate(snapshot, self.now, 0),
                    "codex": {"session_pct": 1.0, "week_pct": 1.0, "stale": False,
                              "source": "codex-logs", "resets_at": None}}
        reply = daemon.usage_reply(True, snapshot, readings, self.now, 0)
        self.assertEqual(polled, [])
        self.assertIn("floor", reply)

    def test_a_pause_from_chat_is_not_reverted_by_the_same_tick(self):
        """Telegram is the only surface the user has.

        A control command that reports success and then quietly undoes itself is
        worse than one that fails loudly: the lane keeps spending plan budget
        and there is nowhere else to notice from.
        """
        daemon = self._daemon()
        daemon.chat = _ScriptedChat("pause codex", update_id=7)
        with state.mutate_queue() as queue:
            state.new_task(queue, "qpay", "work", agent="claude")
        daemon.tick()
        doc = state.read_state()
        self.assertEqual(doc["mode"][lanes.CODEX], "paused")
        self.assertEqual(doc["mode"][lanes.CLAUDE], winddown.RUNNING)
        self.assertIn("paused codex", daemon.chat.sent[0][1])

    def test_a_paused_lane_is_never_resumed_by_a_usage_reading(self):
        """The tick may not undo a pause, however the window moves.

        Above the soft limit the pre-fix machine turned `paused` into `frozen`,
        and `frozen` resumes itself once a fresh reading confirms the reset --
        so a lane the user paused by hand quietly started spending again.
        """
        daemon = self._daemon(claude_pct=99.0)
        daemon.set_mode("pause", lanes.CLAUDE)
        daemon.tick()
        doc = state.read_state()
        self.assertEqual(doc["mode"][lanes.CLAUDE], winddown.PAUSED)
        self.assertEqual(doc["mode"][lanes.CODEX], winddown.RUNNING)

        # The window resets: a frozen lane would come back here, a paused one
        # must not. Past poll_idle, so the tick takes a fresh reading.
        self.now += 700
        daemon = self._daemon(claude_pct=5.0)
        daemon.tick()
        self.assertEqual(state.read_state()["mode"][lanes.CLAUDE], winddown.PAUSED)

        daemon.set_mode("resume", lanes.CLAUDE)
        daemon.tick()
        self.assertEqual(state.read_state()["mode"][lanes.CLAUDE], winddown.RUNNING)

    def test_a_limit_error_does_not_overwrite_a_pause(self):
        """The user pauses while a step is in flight, and the step hits the wall.

        Freezing the lane here would arm a resume, and the lane would let itself
        go again at the reset -- the same pause discarded through another door.
        The limit is still recorded, so resuming early does not walk back in.
        """
        def run_step(task, cwd):
            daemon.set_mode("pause", lanes.CLAUDE)
            return {"status": "continue", "summary": "hit the wall", "next": "",
                    "output": "", "limit_reset_at": self.now + 3600,
                    "session_id": None, "timed_out": False}

        daemon = self._daemon(run_step=run_step)
        with state.mutate_queue() as queue:
            state.new_task(queue, "qpay", "work", agent="claude")
        daemon.tick()
        doc = state.read_state()
        self.assertEqual(doc["mode"][lanes.CLAUDE], winddown.PAUSED)
        self.assertIsNone(doc["armed_resume_at"][lanes.CLAUDE])
        self.assertEqual(doc["governor"]["session_pct"], 100.0)
        self.assertEqual(state.read_queue()["tasks"][0]["state"], "paused")

    def test_a_chat_poll_result_is_not_reverted_by_the_same_tick(self):
        """`usage poll` spends a real request; discarding its answer at the end
        of the tick would spend it for nothing."""
        daemon = self._daemon()
        daemon.chat = _ScriptedChat("usage poll", update_id=7)
        daemon.poll_usage = lambda: {"ok": True, "at": self.now, "session_pct": 44.0,
                                     "session_reset": self.now + 3600,
                                     "week_pct": 5.0, "week_reset": None}
        with state.mutate_state() as doc:
            # Past the poll floor but short of the idle interval, so the tick
            # itself will not poll and the chat command's poll is the only one.
            doc["governor"] = dict(governor.blank(), polled_at=self.now - 61,
                                   session_pct=10.0, tokens_at_poll=0,
                                   session_reset=self.now + 3600)
        with state.mutate_queue() as queue:
            state.new_task(queue, "qpay", "work", agent="claude")
        daemon.tick()
        self.assertEqual(state.read_state()["governor"]["session_pct"], 44.0)

    def test_a_codex_settle_does_not_force_a_claude_poll(self):
        """A codex step spends no Claude budget, so its ending tells the Claude
        governor nothing worth paying a request to confirm."""
        daemon = self._daemon()
        polled = []
        original = daemon.poll_usage
        daemon.poll_usage = lambda: polled.append(1) or original()
        with state.mutate_queue() as queue:
            state.new_task(queue, "poook", "codex work", agent="codex")
        daemon.tick()
        self.assertEqual(len(polled), 1)  # the first tick has never polled
        self.assertEqual(state.read_queue()["tasks"][0]["state"], "done")
        self.now += 61  # past poll_floor, well short of poll_idle
        daemon.tick()
        self.assertEqual(len(polled), 1)

    def test_a_running_codex_worker_does_not_make_the_claude_poll_hot(self):
        """`hot` shortens the interval before the next paid Claude poll. A busy
        codex lane is not a reason to spend a Claude request sooner."""
        daemon = self._daemon()
        daemon.executor = _NeverFinishes()
        polled = []
        original = daemon.poll_usage
        daemon.poll_usage = lambda: polled.append(1) or original()
        with state.mutate_queue() as queue:
            state.new_task(queue, "poook", "codex work", agent="codex")
        daemon.tick()
        self.assertEqual(len(polled), 1)
        self.assertEqual(len(daemon.running), 1)
        self.now += 200  # past poll_hot (180), short of poll_idle (600)
        daemon.tick()
        self.assertEqual(len(polled), 1)

    def test_a_frozen_codex_lane_does_not_spend_claude_requests(self):
        """The codex reading comes free from disk and no `/usage` call can
        refresh it, so an unconfirmed codex resume timer must not arm a Claude
        poll -- while the lane is frozen its percentage cannot fall, so the
        timer would re-arm every tick and leak a request per poll_floor forever.
        """
        daemon = self._daemon(codex_pct=99.0)
        polled = []
        original = daemon.poll_usage
        daemon.poll_usage = lambda: polled.append(1) or original()
        with state.mutate_state() as doc:
            doc["mode"][lanes.CODEX] = winddown.FROZEN
            doc["armed_resume_at"][lanes.CODEX] = self.now - 10
            doc["governor"] = dict(governor.blank(), polled_at=self.now - 61,
                                   session_pct=10.0, tokens_at_poll=0,
                                   session_reset=self.now + 3600)
        daemon.tick()
        daemon.tick()
        self.assertEqual(polled, [])
        self.assertEqual(state.read_state()["mode"][lanes.CODEX], winddown.FROZEN)

    def test_the_volume_report_is_called_and_its_failure_surfaces(self):
        """volume skips a log it cannot read and carries on. The daemon must not
        also swallow a report that fails outright."""
        daemon = self._daemon()
        # Drop the empty-log seam this harness installs, so the real method runs.
        del daemon.volume_block
        readings = {"claude": {"session_pct": 1.0, "week_pct": 1.0, "stale": False,
                               "source": "measured", "resets_at": None},
                    "codex": {"session_pct": 1.0, "week_pct": 1.0, "stale": False,
                              "source": "codex-logs", "resets_at": None}}
        with mock.patch.object(volume, "render", return_value="VOLUME") as render:
            reply = daemon.usage_reply(False, governor.blank(), readings, self.now, 0)
        render.assert_called_once_with(self.now)
        self.assertIn("VOLUME", reply)
        with mock.patch.object(volume, "render", side_effect=OSError("bad log")):
            reply = daemon.usage_reply(False, governor.blank(), readings, self.now, 0)
        self.assertIn("volume report unavailable", reply)
        self.assertIn("bad log", reply)

    def test_every_parser_kind_gets_a_reply(self):
        """Silence is the worst reply a chat surface can give.

        An empty reply is indistinguishable from a dropped message, so every
        kind the parser can emit -- including the ones that carry no ``text``
        and used to fall through to free-form intake -- must answer with
        something.
        """
        samples = ["ping", "yes", "no",
                   "status", "queue", "usage", "usage poll", "help", "repos",
                   "sessions", "sessions qpay", "pause", "resume", "pause codex",
                   "resume claude", "cancel 1", "logs 1", "retry 1",
                   "claude fix the auth test", "claude tidy up on qpay",
                   "run the migration on qpay", "hello there", "", "   "]
        covered = {parser.parse(text)["kind"] for text in samples}
        self.assertEqual(covered, set(parser.KINDS))

        daemon = self._daemon()
        daemon.freeform = lambda text: (None, "no model in tests")
        modes = {"claude": "running", "codex": "running"}
        readings = {"claude": {"session_pct": 1.0, "week_pct": 1.0, "stale": False,
                               "source": "measured", "resets_at": None},
                    "codex": {"session_pct": 1.0, "week_pct": 1.0, "stale": False,
                              "source": "codex-logs", "resets_at": None}}
        with mock.patch.object(sessions, "render", return_value="no sessions found"):
            for text in samples:
                reply = daemon.handle_command(text, modes, readings,
                                              governor.blank(), self.now, 0)
                self.assertTrue(reply, "empty reply for %r" % text)
                self.assertIsInstance(reply, str)

    def test_chat_offset_survives_a_step_settling_in_the_same_tick(self):
        """The tick's state document is read before intake runs.

        Writing that copy back whole after a step settles would rewind the chat
        cursor to where it stood at the top of the tick, and every command in
        the batch would be replayed on the next poll.
        """
        daemon = self._daemon()
        daemon.chat = _ScriptedChat("status", update_id=7)
        with state.mutate_queue() as queue:
            state.new_task(queue, "qpay", "work", agent="claude")
        daemon.tick()
        self.assertEqual(state.read_state()["chat_offset"], 8)
        self.assertEqual(state.read_queue()["tasks"][0]["state"], "done")

    # -- the week window, which the mode machine used to ignore --------------

    def _week_exhausted_codex(self, daemon, week_pct=100.0):
        daemon.codex_estimate = lambda now: {
            "session_pct": 0.0, "session_known": False, "week_pct": week_pct,
            "source": "codex-logs", "stale": False,
            "resets_at": self.now + 3600, "week_resets_at": self.now + 86400}

    def test_a_lane_at_100_percent_of_its_week_freezes_instead_of_running(self):
        """`admit` refuses at the weekly soft limit and the mode machine did
        not, so the lane reported `running` -- "dispatching normally" -- while
        dispatching nothing at all. This is the codex lane's real state."""
        daemon = self._daemon()
        self._week_exhausted_codex(daemon)
        with state.mutate_queue() as queue:
            state.new_task(queue, "poook", "codex work", agent="codex")
        result = daemon.tick()
        self.assertEqual(result["mode"][lanes.CODEX], winddown.FROZEN)
        self.assertEqual(state.read_state()["mode"][lanes.CODEX], winddown.FROZEN)
        self.assertEqual(result["mode"][lanes.CLAUDE], winddown.RUNNING)
        self.assertEqual(state.read_queue()["tasks"][0]["state"], "queued")

    def test_the_ack_for_a_week_exhausted_lane_does_not_claim_it_is_running(self):
        daemon = self._daemon()
        self._week_exhausted_codex(daemon)
        daemon.chat = _ScriptedChat("codex bump deps on poook", update_id=3)
        daemon.tick()
        reply = daemon.chat.sent[-1][1]
        self.assertIn("queued t-0001", reply)
        self.assertIn(winddown.FROZEN, reply)
        self.assertNotIn(winddown.RUNNING, reply)

    def test_a_week_freeze_waits_for_the_week_to_reset_not_the_session(self):
        daemon = self._daemon()
        self._week_exhausted_codex(daemon)
        daemon.tick()
        self.assertEqual(state.read_state()["armed_resume_at"][lanes.CODEX],
                         self.now + 86400 + winddown.RESUME_DELAY)

    def test_the_freeze_notice_does_not_report_a_missing_reading_as_zero(self):
        daemon = self._daemon()
        self._week_exhausted_codex(daemon)
        daemon.tick()
        frozen = [notice for notice in daemon.notices if "frozen" in notice]
        self.assertEqual(len(frozen), 1, daemon.notices)
        self.assertIn("7d 100%", frozen[0])
        self.assertNotIn("5h 0%", frozen[0])

    def test_a_week_frozen_lane_does_not_flap_back_to_running(self):
        """The session window is fine and the timer has fired; only the week
        is full. Resuming here would re-freeze on the next tick and send a
        freeze notice every tick until the week rolled over."""
        daemon = self._daemon()
        self._week_exhausted_codex(daemon)
        daemon.tick()
        with state.mutate_state() as doc:
            doc["armed_resume_at"][lanes.CODEX] = self.now - 1
        self.assertEqual(daemon.tick()["mode"][lanes.CODEX], winddown.FROZEN)
        freezes = [n for n in daemon.notices if "frozen" in n]
        self.assertEqual(len(freezes), 1, daemon.notices)

    def test_a_week_frozen_lane_returns_to_running_when_the_week_resets(self):
        """The other half of the freeze: it has to end. Frozen is only left
        through `can_resume`, which now also consults the week."""
        daemon = self._daemon()
        self._week_exhausted_codex(daemon)
        with state.mutate_queue() as queue:
            state.new_task(queue, "poook", "codex work", agent="codex")
        daemon.tick()
        self.assertEqual(state.read_state()["mode"][lanes.CODEX], winddown.FROZEN)
        self.assertEqual(state.read_queue()["tasks"][0]["state"], "queued")

        # The week rolls over: the armed timer has passed and codex's own logs
        # now report a fresh window.
        self.now += 86400 + winddown.RESUME_DELAY + 1
        daemon.codex_estimate = lambda now: {
            "session_pct": 1.0, "session_known": True, "week_pct": 2.0,
            "source": "codex-logs", "stale": False, "resets_at": None,
            "week_resets_at": None}
        result = daemon.tick()

        self.assertEqual(result["mode"][lanes.CODEX], winddown.RUNNING)
        self.assertIsNone(state.read_state()["armed_resume_at"][lanes.CODEX])
        self.assertTrue(any("codex resumed" in notice for notice in daemon.notices),
                        daemon.notices)
        # Recovered to the point of actually dispatching, not just to a label.
        self.assertEqual(state.read_queue()["tasks"][0]["state"], "done")

    def test_a_week_frozen_claude_lane_does_not_re_poll_every_tick(self):
        """The armed timer fires on the session reset, which cannot help: only
        the week rolling over can, and the idle cadence already covers that."""
        daemon = self._daemon()
        polled = []
        original = daemon.poll_usage
        daemon.poll_usage = lambda: polled.append(1) or original()
        with state.mutate_state() as doc:
            doc["mode"][lanes.CLAUDE] = winddown.FROZEN
            doc["armed_resume_at"][lanes.CLAUDE] = self.now - 10
            doc["governor"] = dict(governor.blank(), polled_at=self.now - 61,
                                   session_pct=3.0, tokens_at_poll=0,
                                   session_reset=self.now + 3600,
                                   week_pct=100.0, week_reset=self.now + 86400)
        daemon.tick()
        daemon.tick()
        self.assertEqual(polled, [])
        self.assertEqual(state.read_state()["mode"][lanes.CLAUDE], winddown.FROZEN)

    def test_the_status_line_stops_reporting_a_missing_reading_as_zero(self):
        daemon = self._daemon()
        readings = {lanes.CLAUDE: {"session_pct": 1.0, "week_pct": 1.0,
                                   "stale": False, "source": "measured",
                                   "resets_at": None},
                    lanes.CODEX: {"session_pct": 0.0, "session_known": False,
                                  "week_pct": 100.0, "stale": False,
                                  "source": "codex-unknown", "resets_at": None}}
        line = daemon.status_line({lanes.CLAUDE: "running", lanes.CODEX: "frozen"},
                                  governor.blank(), readings, self.now, 0)
        codex_line = [row for row in line.splitlines() if row.startswith("codex")][0]
        self.assertIn("frozen", codex_line)
        self.assertIn("week 100%", codex_line)
        self.assertNotIn("session 0%", codex_line)


    # -- why a running lane started nothing ---------------------------------

    def test_a_lane_that_starts_nothing_records_the_reason_admission_gave(self):
        """`admit` returns a reason string and the dispatch pass threw it away.
        A `running` lane whose queue never moves is the hardest state to read
        from a chat window."""
        daemon = self._daemon()
        daemon.executor = _NeverFinishes()
        with state.mutate_queue() as queue:
            state.new_task(queue, "qpay", "first", agent="claude")
            state.new_task(queue, "qpay", "second", agent="claude")
        daemon.tick()
        reason = state.read_state()["hold_reason"][lanes.CLAUDE]
        self.assertIn("t-0002", reason)
        self.assertIn("busy", reason)
        readings = {lanes.CLAUDE: {"session_pct": 1.0, "week_pct": 1.0,
                                   "stale": False, "source": "measured",
                                   "resets_at": None},
                    lanes.CODEX: {"session_pct": 1.0, "week_pct": 1.0,
                                  "stale": False, "source": "codex-logs",
                                  "resets_at": None}}
        line = daemon.status_line({lanes.CLAUDE: "running", lanes.CODEX: "running"},
                                  governor.blank(), readings, self.now, 0)
        self.assertIn("holding:", line)
        self.assertIn("t-0002", line)

    def test_the_headroom_refusal_is_explained_too(self):
        """The one refusal that leaves the mode `running` and the queue still."""
        daemon = self._daemon(claude_pct=82.0)
        with state.mutate_queue() as queue:
            state.new_task(queue, "qpay", "work", agent="claude")
        result = daemon.tick()
        self.assertEqual(result["mode"][lanes.CLAUDE], winddown.RUNNING)
        self.assertEqual(state.read_queue()["tasks"][0]["state"], "queued")
        self.assertIn("soft limit", state.read_state()["hold_reason"][lanes.CLAUDE])

    def test_an_empty_lane_is_not_holding_anything(self):
        daemon = self._daemon()
        daemon.tick()
        doc = state.read_state()
        # Nothing to explain, and nothing written to say so.
        self.assertEqual(doc["hold_reason"], {})
        from dispatch import daemon as daemon_mod
        self.assertIsNone(daemon_mod.hold_line(doc, lanes.CLAUDE, winddown.RUNNING))

    def test_a_hold_is_not_reported_for_a_lane_that_is_not_running(self):
        """Every other mode explains itself; a stale hold under it would not."""
        from dispatch import daemon as daemon_mod
        doc = {"hold_reason": {lanes.CODEX: "t-0001 mode is frozen"}}
        self.assertIsNone(daemon_mod.hold_line(doc, lanes.CODEX, winddown.FROZEN))
        self.assertIsNotNone(daemon_mod.hold_line(doc, lanes.CODEX, winddown.RUNNING))

    # -- the transport, and what happens when it is gone --------------------

    def test_a_chat_failure_is_recorded_printed_and_reported(self):
        """A dead transport looks exactly like silence: daemon up, queue
        healthy, watchdog quiet, bot answering nothing."""
        daemon = self._daemon()
        daemon.chat = _BrokenChat("HTTPError: HTTP Error 409: Conflict", failures=0)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            daemon.tick()
        doc = state.read_state()
        self.assertEqual(doc["chat_last_error"], "HTTPError: HTTP Error 409: Conflict")
        self.assertEqual(doc["chat_failures"], 1)
        self.assertEqual(doc["chat_error_at"], self.now)
        self.assertIn("409", err.getvalue())
        readings = {lanes.CLAUDE: {"session_pct": 1.0, "week_pct": 1.0,
                                   "stale": False, "source": "measured",
                                   "resets_at": None},
                    lanes.CODEX: {"session_pct": 1.0, "week_pct": 1.0,
                                  "stale": False, "source": "codex-logs",
                                  "resets_at": None}}
        line = daemon.status_line({lanes.CLAUDE: "running", lanes.CODEX: "running"},
                                  governor.blank(), readings, self.now, 0)
        self.assertIn("409", line)

    def test_a_standing_chat_failure_is_not_reprinted_every_tick(self):
        """The count climbs on every tick of an outage; the message does not,
        and the pane has to stay readable."""
        daemon = self._daemon()
        daemon.chat = _BrokenChat("HTTPError: HTTP Error 409: Conflict", failures=0)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            for _ in range(5):
                daemon.tick()
        self.assertEqual(err.getvalue().count("chat transport error"), 1)
        self.assertEqual(state.read_state()["chat_failures"], 5)

    def test_a_recovered_transport_clears_the_report(self):
        from dispatch import chat as chat_mod
        daemon = self._daemon()
        daemon.chat = _BrokenChat("HTTPError: HTTP Error 409: Conflict")
        with contextlib.redirect_stderr(io.StringIO()):
            daemon.tick()
        daemon.chat = chat_mod.NullChat()
        with contextlib.redirect_stderr(io.StringIO()) as err:
            daemon.tick()
        self.assertIsNone(state.read_state()["chat_last_error"])
        self.assertIn("recovered", err.getvalue())
        line = daemon.status_line(
            {lanes.CLAUDE: "running", lanes.CODEX: "running"}, governor.blank(),
            {lanes.CLAUDE: {"session_pct": 1.0, "week_pct": 1.0, "stale": False,
                            "source": "measured", "resets_at": None},
             lanes.CODEX: {"session_pct": 1.0, "week_pct": 1.0, "stale": False,
                           "source": "codex-logs", "resets_at": None}},
            self.now, 0)
        self.assertNotIn("chat", line)

    def test_a_missing_token_is_a_reported_condition_not_a_silent_downgrade(self):
        daemon = self._daemon()
        with contextlib.redirect_stderr(io.StringIO()) as err:
            chat = daemon._default_chat()
        self.assertIn("no bot token", chat.last_error)
        self.assertIn("no bot token", err.getvalue())

    def test_an_empty_allowlist_refuses_chat_rather_than_serving_everyone(self):
        """A token and no allowlist is the corrupt-config shape. The daemon
        must not build a live transport there: it reports the condition and
        runs without one, the same way it does with no token at all."""
        from dispatch import chat as chat_mod
        from dispatch import daemon as daemon_mod

        env = os.path.join(self.tmp.name, "token.env")
        with open(env, "w") as fh:
            fh.write("TELEGRAM_BOT_TOKEN=123456:SECRET-BOT-TOKEN\n")
        daemon = self._daemon()
        daemon.config["chat_allowlist"] = []
        with mock.patch.dict(os.environ, {"DISPATCH_TOKEN_ENV": env}):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                chat = daemon._default_chat()

        self.assertIsInstance(chat, chat_mod.NullChat)
        self.assertIn("allowlist", chat.last_error)
        self.assertIn("allowlist", err.getvalue())
        self.assertNotIn("SECRET-BOT-TOKEN", chat.last_error + err.getvalue())

        # And it is a standing condition on the surface that reports transport
        # health, so `dispatch status` says why the bot is answering nobody.
        daemon.chat = chat
        with contextlib.redirect_stderr(io.StringIO()):
            daemon.tick()
        self.assertIn("allowlist",
                      daemon_mod.chat_status_line(state.read_state()))

    def _token_env(self):
        env = os.path.join(self.tmp.name, "token.env")
        with open(env, "w") as fh:
            fh.write("TELEGRAM_BOT_TOKEN=123456:SECRET-BOT-TOKEN\n")
        return mock.patch.dict(os.environ, {"DISPATCH_TOKEN_ENV": env})

    # value in config.json -> transport, and the ids it will admit.
    ALLOWLIST_SHAPES = [
        ("7256243815", "Chat", ["7256243815"]),
        (7256243815, "Chat", ["7256243815"]),
        (["7256243815"], "Chat", ["7256243815"]),
        (["7256243815", 42], "Chat", ["7256243815", "42"]),
        (True, "NullChat", []),
        ({"a": 1}, "NullChat", []),
        ([], "NullChat", []),
        ([None], "NullChat", []),
        (["", "  "], "NullChat", []),
    ]

    def test_every_allowlist_shape_from_config_lands_somewhere_safe(self):
        """Asserted through `_default_chat`, which is the only path that runs.

        The constructor guard in `chat.py` was reached with a list already
        built by the daemon, so it never saw the shapes it was written for: a
        bare string produced a live transport admitting chat id `7` while
        denying the owner, a bare int crashed the daemon before the tick guard
        could catch it, and a dict produced a transport admitting `a`.
        """
        for value, transport, expected in self.ALLOWLIST_SHAPES:
            daemon = self._daemon()
            daemon.config["chat_allowlist"] = value
            with self._token_env():
                with contextlib.redirect_stderr(io.StringIO()) as err:
                    chat = daemon._default_chat()
            self.assertEqual(type(chat).__name__, transport, repr(value))
            self.assertEqual(getattr(chat, "allowlist", []), expected, repr(value))
            if transport == "NullChat":
                self.assertIn("allowlist", chat.last_error, repr(value))
                self.assertIn("allowlist", err.getvalue(), repr(value))

    def test_notify_reaches_the_listed_ids_and_no_others(self):
        """Outbound side of the same value. A bare string used to fan every
        task notice out to ten single-digit chat ids."""
        from dispatch import chat as chat_mod

        for value, _, expected in self.ALLOWLIST_SHAPES:
            daemon = self._daemon()
            daemon.config["chat_allowlist"] = value
            daemon.chat = chat_mod.NullChat()
            daemon.notify("step done")
            self.assertEqual([chat_id for chat_id, _ in daemon.chat.sent],
                             expected, repr(value))

    # -- crash handling ------------------------------------------------------

    def test_a_tick_that_raises_does_not_take_the_daemon_with_it(self):
        """The tmux session dies with the process, and the traceback with the
        pane. A deterministic error would be a permanent lockout."""
        daemon = self._daemon()
        ticks = []

        def explode():
            ticks.append(1)
            raise RuntimeError("kaboom")

        daemon.tick = explode
        with contextlib.redirect_stderr(io.StringIO()) as err:
            daemon.run(interval=0, ticks=3, sleeper=lambda seconds: None)
        self.assertEqual(len(ticks), 3)
        self.assertIn("kaboom", err.getvalue())
        with open(config_mod.daemon_log_path()) as fh:
            log = fh.read()
        self.assertEqual(log.count("tick failed"), 3)
        self.assertIn("RuntimeError: kaboom", log)

    def test_the_crash_log_does_not_grow_without_bound(self):
        """A tick that raises every interval writes a traceback every
        interval. A full disk is a worse failure than the one being logged."""
        from dispatch import daemon as daemon_mod
        daemon = self._daemon()
        config_mod.ensure_dirs()
        path = config_mod.daemon_log_path()
        with open(path, "w") as fh:
            fh.write("x" * (daemon_mod.DAEMON_LOG_MAX + 1))
        with contextlib.redirect_stderr(io.StringIO()):
            daemon.log_crash("RuntimeError: kaboom")
        self.assertTrue(os.path.exists(path + ".1"))
        with open(path) as fh:
            body = fh.read()
        self.assertIn("kaboom", body)
        self.assertLess(len(body), 1000)

    def test_a_daemon_log_that_cannot_be_written_is_not_fatal_either(self):
        daemon = self._daemon()
        with mock.patch("builtins.open", side_effect=OSError("read-only file system")):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                daemon.log_crash("Traceback: nowhere to put this")
        self.assertIn("could not write", err.getvalue())
        self.assertIn("nowhere to put this", err.getvalue())

    # -- a checkpoint that did not happen ------------------------------------

    def test_a_failed_checkpoint_fails_the_task_instead_of_reporting_done(self):
        """`done` on an empty branch is the one outcome the wind-down contract
        rules out: work is parked, never lost."""
        daemon = self._daemon(checkpoint=lambda task, cwd, message: {
            "ok": False, "error": "fatal: cannot lock ref 'refs/heads/tg/t-0001'"})
        with state.mutate_queue() as queue:
            state.new_task(queue, "qpay", "work", agent="claude")
        daemon.tick()
        task = state.read_queue()["tasks"][0]
        self.assertEqual(task["state"], "failed")
        self.assertIn("cannot lock ref", task["last_error"])
        self.assertTrue(any("not committed" in notice for notice in daemon.notices),
                        daemon.notices)
        directory = config_mod.task_dir("t-0001")
        with open(os.path.join(directory, "steps.jsonl")) as fh:
            step = json.loads(fh.read().strip())
        self.assertIn("cannot lock ref", step["checkpoint_error"])
        self.assertTrue(os.path.exists(os.path.join(directory, "handoff.md")))

    def test_a_successful_checkpoint_still_settles_normally(self):
        daemon = self._daemon(checkpoint=lambda task, cwd, message: {
            "ok": True, "committed": True})
        with state.mutate_queue() as queue:
            state.new_task(queue, "qpay", "work", agent="claude")
        daemon.tick()
        self.assertEqual(state.read_queue()["tasks"][0]["state"], "done")
        with open(os.path.join(config_mod.task_dir("t-0001"), "steps.jsonl")) as fh:
            self.assertIsNone(json.loads(fh.read().strip())["checkpoint_error"])

    # -- stored free-form text, which nothing used to come back for ----------

    def _stored(self, text="tidy the deps everywhere"):
        with state.mutate_queue() as queue:
            task = state.new_task(queue, "?", text)
            task["state"] = "needs_parse"
        return task["id"]

    def test_stored_text_is_parsed_on_a_later_tick(self):
        daemon = self._daemon()
        seen = []
        daemon.freeform = lambda text: (
            seen.append(text) or [{"repo": "qpay", "prompt": "tidy deps"}], None)
        self._stored()
        daemon.tick()
        tasks = {t["id"]: t for t in state.read_queue()["tasks"]}
        self.assertEqual(seen, ["tidy the deps everywhere"])
        self.assertEqual(tasks["t-0001"]["state"], "parsed")
        # Proposed, not queued: text stored while the window was exhausted is
        # exactly the text nobody has looked at since.
        self.assertNotIn("t-0002", tasks)
        pending = state.read_state()["pending_confirm"]["1"]
        self.assertEqual(pending["tasks"], [{"repo": "qpay", "prompt": "tidy deps"}])
        self.assertTrue(any("reply `yes` to queue" in n for n in daemon.notices),
                        daemon.notices)

    def test_stored_text_is_left_alone_while_the_lane_is_not_running(self):
        daemon = self._daemon(claude_pct=99.0)
        seen = []
        daemon.freeform = lambda text: (seen.append(text), (None, "no model"))[1]
        self._stored()
        daemon.tick()
        self.assertEqual(seen, [])
        self.assertEqual(state.read_queue()["tasks"][0]["state"], "needs_parse")

    def test_only_one_stored_message_is_parsed_per_tick(self):
        """The parse is a subprocess with a two-minute cap, on the loop that
        also answers chat."""
        daemon = self._daemon()
        seen = []
        daemon.freeform = lambda text: (
            seen.append(text) or [{"repo": "qpay", "prompt": "x"}], None)
        self._stored("first")
        self._stored("second")
        daemon.tick()
        self.assertEqual(seen, ["first"])

    def test_a_stored_message_whose_repo_does_not_resolve_is_not_dropped(self):
        """`enqueue` returns its rejection as a string; it neither raises nor
        queues. Discarding it marked the stored text `parsed` with nothing
        queued and nothing said -- and `queue` hides `parsed` by default, so
        the request was gone from every surface after "parsed after reset"."""
        daemon = self._daemon()
        daemon.freeform = lambda text: ([{"repo": "qpay", "prompt": "first"},
                                         {"repo": "typo-repo", "prompt": "second"}],
                                        None)
        self._stored()
        daemon.tick()
        tasks = {t["id"]: t for t in state.read_queue()["tasks"]}
        self.assertEqual(tasks["t-0001"]["state"], "parsed")
        # The half that resolves is proposed; the half that does not is named
        # in the same message, so the rejection is visible before `yes` rather
        # than discovered after it.
        pending = state.read_state()["pending_confirm"]["1"]
        self.assertEqual([i["prompt"] for i in pending["tasks"]], ["first"])
        self.assertTrue(any("typo-repo" in notice for notice in daemon.notices),
                        daemon.notices)
        self.assertTrue(any("skipping" in notice for notice in daemon.notices),
                        daemon.notices)

    def test_a_stored_message_with_no_usable_repo_at_all_queues_nothing(self):
        daemon = self._daemon()
        daemon.freeform = lambda text: ([{"repo": "typo-repo", "prompt": "x"}], None)
        self._stored()
        daemon.tick()
        tasks = state.read_queue()["tasks"]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["state"], "blocked")
        self.assertIn("dispatchable", tasks[0]["last_error"])
        # Nothing to say yes to, so nothing is parked waiting for a yes.
        self.assertEqual(state.read_state()["pending_confirm"], {})

    def test_a_stored_message_that_parses_cleanly_still_settles_parsed(self):
        daemon = self._daemon()
        daemon.freeform = lambda text: ([{"repo": "qpay", "prompt": "ok"}], None)
        self._stored()
        daemon.tick()
        stored = state.read_queue()["tasks"][0]
        self.assertEqual(stored["state"], "parsed")
        self.assertIsNone(stored["last_error"])

    def test_a_parse_that_keeps_failing_is_handed_back_not_retried_forever(self):
        from dispatch import daemon as daemon_mod
        daemon = self._daemon()
        seen = []
        daemon.freeform = lambda text: (seen.append(text), (None, "no model"))[1]
        self._stored()
        for _ in range(6):
            daemon.tick()
        self.assertEqual(len(seen), daemon_mod.PARSE_ATTEMPTS)
        task = state.read_queue()["tasks"][0]
        self.assertEqual(task["state"], "blocked")
        self.assertEqual(task["last_error"], "no model")
        self.assertTrue(any("send it again" in notice for notice in daemon.notices),
                        daemon.notices)


class _BrokenChat:
    """A transport that keeps failing, counting the way the real one does."""

    def __init__(self, error, failures=1):
        self.last_error = error
        self.failures = failures
        self.sent = []

    def allowed(self, chat_id):
        return True

    def poll(self, offset):
        self.failures += 1
        return [], offset

    def send(self, chat_id, text):
        self.sent.append((chat_id, text))
        return True


class _NeverFinishes:
    """Executor whose futures stay pending, so `running` survives the tick."""

    def submit(self, fn, *args, **kwargs):
        import concurrent.futures
        return concurrent.futures.Future()

    def shutdown(self, wait=True):
        return None


class _ScriptedChat:
    """Delivers one message once, then goes quiet. Records what was sent back."""

    def __init__(self, text, update_id):
        self.text = text
        self.next_offset = update_id + 1
        self.sent = []

    def allowed(self, chat_id):
        return True

    def poll(self, offset):
        if offset >= self.next_offset:
            return [], offset
        return [{"chat_id": "1", "text": self.text}], self.next_offset

    def send(self, chat_id, text):
        self.sent.append((chat_id, text))
        return True


class _Args:
    """Stands in for an argparse namespace, with only the attrs a test sets."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _only_writes_fail(path):
    """Wrap `open` so writing one specific path raises, reads still work."""
    real = open

    def guarded(file, mode="r", *args, **kwargs):
        if file == path and "w" in mode:
            raise OSError("read-only file system")
        return real(file, mode, *args, **kwargs)

    return guarded


class _CliEnv(unittest.TestCase):
    """Base for CLI tests: every root the CLI reads lives in a temp tree.

    The daemon injects its clock, its transcript counter and its codex reading;
    the CLI has no such seams, so the roots themselves are pointed elsewhere.
    Without that, `dispatch status` reads the developer's real ~/.claude and
    ~/.codex.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.transcripts = os.path.join(self.tmp.name, "transcripts")
        self.codex_logs = os.path.join(self.tmp.name, "codex-sessions")
        self.bin = self._write_agents(os.path.join(self.tmp.name, "bin"),
                                      "claude", "codex")
        patcher = mock.patch.dict(os.environ, {
            "DISPATCH_HOME": os.path.join(self.tmp.name, "home"),
            "DISPATCH_TRANSCRIPTS": self.transcripts,
            "DISPATCH_CODEX_SESSIONS": self.codex_logs,
            "DISPATCH_TOKEN_ENV": os.path.join(self.tmp.name, "absent.env"),
            # `~` and PATH too: `dispatch up` probes both for the agents, and
            # a test may not so much as stat the developer's home directory.
            "HOME": self.tmp.name,
            "PATH": self.bin,
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _write_agents(directory, *names):
        """Executables named like the agents, so nothing probes a real one."""
        os.makedirs(directory, exist_ok=True)
        for name in names:
            target = os.path.join(directory, name)
            with open(target, "w") as fh:
                fh.write("#!/bin/sh\nexit 0\n")
            os.chmod(target, 0o755)
        return directory

    def _capture(self, func, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = func(*args)
        return code, out.getvalue(), err.getvalue()

    def _write_codex_limits(self, session_pct, week_pct, now):
        directory = os.path.join(self.codex_logs, "2026", "08", "20")
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "a.jsonl"), "w") as fh:
            fh.write(json.dumps({"payload": {"info": {"rate_limits": {
                "primary": {"used_percent": session_pct, "window_minutes": 300,
                            "resets_at": now + 3600},
                "secondary": {"used_percent": week_pct, "window_minutes": 10080,
                              "resets_at": now + 86400}}}}}) + "\n")


class TestConfiguredRoots(_CliEnv):
    """The two read-only roots the CLI cannot be handed as arguments."""

    def test_transcript_tokens_follows_the_configured_root(self):
        now = time.time()
        directory = os.path.join(self.transcripts, "-home-someone-proj")
        os.makedirs(directory)
        with open(os.path.join(directory, "session.jsonl"), "w") as fh:
            fh.write(json.dumps({
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
                "message": {"id": "m1", "usage": {"input_tokens": 7,
                                                  "output_tokens": 3}}}) + "\n")
        self.assertEqual(usage.transcript_tokens(now), 10)

    def test_transcript_tokens_still_takes_an_explicit_root(self):
        self.assertEqual(usage.transcript_tokens(
            time.time(), root=os.path.join(self.tmp.name, "nothing")), 0)

    def test_the_codex_governor_follows_the_configured_root(self):
        now = time.time()
        self._write_codex_limits(42.0, 8.0, now)
        reading = governor.codex.estimate(now)
        self.assertEqual(reading["session_pct"], 42.0)
        self.assertEqual(reading["source"], "codex-logs")


class TestCliLanes(_CliEnv):
    """`status`, `pause` and `resume` speak about both lanes, or one by name."""

    def test_status_reports_the_mode_and_usage_of_both_lanes(self):
        now = time.time()
        self._write_codex_limits(42.0, 8.0, now)
        code, out, _ = self._capture(cli.cmd_status, _Args())
        self.assertEqual(code, 0)
        self.assertRegex(out, r"claude\s+running")
        self.assertRegex(out, r"codex\s+running")
        self.assertIn("42%", out)
        self.assertIn("queue", out)

    def test_status_tells_the_truth_about_a_single_paused_lane(self):
        self._capture(cli.cmd_pause, _Args(lane=lanes.CODEX))
        _, out, _ = self._capture(cli.cmd_status, _Args())
        self.assertRegex(out, r"claude\s+running")
        self.assertRegex(out, r"codex\s+paused")

    def test_status_names_the_lane_whose_resume_is_armed(self):
        with state.mutate_state() as doc:
            doc["mode"][lanes.CODEX] = winddown.FROZEN
            doc["armed_resume_at"][lanes.CODEX] = time.time() + 3600
        _, out, _ = self._capture(cli.cmd_status, _Args())
        armed = [line for line in out.splitlines() if "resume armed" in line]
        self.assertEqual(len(armed), 1)
        self.assertIn(lanes.CODEX, armed[0])

    def test_pause_takes_one_lane_and_leaves_the_other_alone(self):
        code, out, _ = self._capture(cli.cmd_pause, _Args(lane=lanes.CODEX))
        self.assertEqual(code, 0)
        self.assertIn(lanes.CODEX, out)
        doc = state.read_state()
        self.assertEqual(doc["mode"][lanes.CODEX], winddown.PAUSED)
        self.assertEqual(doc["mode"][lanes.CLAUDE], winddown.RUNNING)

    def test_a_bare_pause_and_resume_still_mean_both_lanes(self):
        self._capture(cli.cmd_pause, _Args(lane=None))
        self.assertEqual(state.read_state()["mode"],
                         {lanes.CLAUDE: winddown.PAUSED, lanes.CODEX: winddown.PAUSED})
        self._capture(cli.cmd_resume, _Args(lane=None))
        self.assertEqual(state.read_state()["mode"],
                         {lanes.CLAUDE: winddown.RUNNING, lanes.CODEX: winddown.RUNNING})

    def test_resume_clears_only_the_named_lanes_armed_timer(self):
        with state.mutate_state() as doc:
            doc["mode"] = {lanes.CLAUDE: winddown.FROZEN, lanes.CODEX: winddown.FROZEN}
            doc["armed_resume_at"] = {lanes.CLAUDE: 111.0, lanes.CODEX: 222.0}
        self._capture(cli.cmd_resume, _Args(lane=lanes.CLAUDE))
        doc = state.read_state()
        self.assertEqual(doc["mode"][lanes.CLAUDE], winddown.RUNNING)
        self.assertIsNone(doc["armed_resume_at"][lanes.CLAUDE])
        self.assertEqual(doc["mode"][lanes.CODEX], winddown.FROZEN)
        self.assertEqual(doc["armed_resume_at"][lanes.CODEX], 222.0)

    def test_the_cli_and_the_chat_verbs_write_the_same_thing(self):
        """One implementation, so the two surfaces cannot drift apart."""
        from dispatch import daemon as daemon_mod
        self._capture(cli.cmd_pause, _Args(lane=lanes.CODEX))
        from_cli = state.read_state()["mode"]
        self._capture(cli.cmd_resume, _Args(lane=None))
        daemon_mod.set_mode("pause", lanes.CODEX)
        self.assertEqual(state.read_state()["mode"], from_cli)

    def test_the_parser_accepts_a_lane_after_pause_and_resume(self):
        parser_ = cli.build_parser()
        self.assertEqual(parser_.parse_args(["pause", "codex"]).lane, "codex")
        self.assertEqual(parser_.parse_args(["resume", "claude"]).lane, "claude")
        self.assertIsNone(parser_.parse_args(["pause"]).lane)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser_.parse_args(["pause", "nonsense"])

    def test_usage_reports_both_lanes_and_the_volume_block(self):
        """The chat verb reports both governors plus volume; this reported the
        Claude lane alone, on the surface you reach for when chat is down."""
        now = time.time()
        self._write_codex_limits(42.0, 8.0, now)
        code, out, err = self._capture(cli.cmd_usage, _Args(poll=False))
        self.assertEqual((code, err), (0, ""))
        self.assertIn("CLAUDE", out)
        self.assertIn("CODEX", out)
        self.assertIn("42%", out)
        self.assertIn("5h", out)  # the volume report

    def test_usage_reads_the_configured_log_roots_not_a_home_directory(self):
        """`volume` defaults to the home it was ported from; every other root
        this CLI reads is configurable, and so is this one."""
        now = time.time()
        directory = os.path.join(self.transcripts, "-home-someone-proj")
        os.makedirs(directory)
        with open(os.path.join(directory, "session.jsonl"), "w") as fh:
            fh.write(json.dumps({
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
                "message": {"id": "m1", "model": "sonnet",
                            "usage": {"input_tokens": 700000,
                                      "output_tokens": 3}}}) + "\n")
        _, out, _ = self._capture(cli.cmd_usage, _Args(poll=False))
        self.assertIn("700", out.replace(".0K", "K"))

    def test_status_repeats_why_a_running_lane_started_nothing(self):
        with state.mutate_state() as doc:
            doc["hold_reason"] = {lanes.CLAUDE: "t-0003 qpay is busy"}
        _, out, _ = self._capture(cli.cmd_status, _Args())
        self.assertIn("holding: t-0003 qpay is busy", out)

    def test_status_reports_a_recorded_chat_failure(self):
        with state.mutate_state() as doc:
            doc["chat_last_error"] = "HTTPError: HTTP Error 409: Conflict"
            doc["chat_failures"] = 12
            doc["chat_error_at"] = time.time()
        code, out, _ = self._capture(cli.cmd_status, _Args())
        self.assertEqual(code, 0)
        self.assertIn("409", out)
        self.assertIn("12 consecutive", out)

    def test_status_says_nothing_about_chat_when_it_is_working(self):
        _, out, _ = self._capture(cli.cmd_status, _Args())
        self.assertNotIn("chat", out)

    def test_a_pause_written_by_the_cli_survives_the_daemon(self):
        """The CLI is what the user reaches for when Telegram is down.

        A pause typed there while usage is high must not be converted to frozen
        and then auto-resumed by the next tick.
        """
        self._capture(cli.cmd_pause, _Args(lane=lanes.CLAUDE))
        doc = state.read_state()
        self.assertEqual(
            winddown.next_mode(doc["mode"][lanes.CLAUDE], 99.0, 0, CONFIG),
            winddown.PAUSED)


class TestCliLifecycle(_CliEnv):
    """`dispatch up/down/logs --daemon`. tmux is injected, never really run."""

    def _runner(self, alive, stdout="", survives=True):
        """A fake tmux that remembers what it was told to do.

        `up` now re-checks the session after creating it, so a runner that
        answers `has-session` from a constant would report every launch as
        having died. `survives=False` is the failure being modelled: the
        session is created and the daemon dies before the check.
        """
        calls = []
        session = {"alive": alive}

        class Result:
            def __init__(self, code, out=""):
                self.returncode = code
                self.stdout = out
                self.stderr = ""

        def runner(argv, **kwargs):
            calls.append(argv)
            if argv[:2] == ["tmux", "has-session"]:
                return Result(0 if session["alive"] else 1)
            if argv[:2] == ["tmux", "new-session"]:
                session["alive"] = survives
            if argv[:2] == ["tmux", "kill-session"]:
                session["alive"] = False
            return Result(0, stdout)

        return runner, calls

    def _new_session(self, calls):
        started = [c for c in calls if c[:2] == ["tmux", "new-session"]]
        self.assertEqual(len(started), 1)
        return started[0]

    def _launch_argv(self, calls):
        """The launched program, past the environment assignments in front."""
        argv = shlex.split(self._new_session(calls)[-1])
        while argv and re.match(r"^[A-Z_][A-Z0-9_]*=", argv[0]):
            argv.pop(0)
        return argv

    def _launch_path(self, calls):
        argv = shlex.split(self._new_session(calls)[-1])
        self.assertTrue(argv[0].startswith("PATH="), argv[0])
        return argv[0][len("PATH="):].split(os.pathsep)


    def test_session_alive_reads_has_session(self):
        runner, _ = self._runner(alive=True)
        self.assertTrue(cli.session_alive(runner))
        runner, _ = self._runner(alive=False)
        self.assertFalse(cli.session_alive(runner))

    def test_up_starts_a_detached_session_when_dead(self):
        runner, calls = self._runner(alive=False)
        code, _, _ = self._capture(cli.cmd_up, _Args(if_dead=False, runner=runner, settle=0))
        self.assertEqual(code, 0)
        started = self._new_session(calls)
        self.assertIn("-d", started)
        self.assertIn(cli.TMUX_SESSION, started)

    def test_up_says_when_the_allowlist_would_refuse_every_chat(self):
        """The cutover makes Telegram the only interface. Starting a daemon
        whose allowlist is empty produces a bot that answers nobody, and every
        other health surface stays green -- so `up` says it out loud, the same
        way it does about a missing token."""
        runner, _ = self._runner(alive=False)
        code, _, err = self._capture(
            cli.cmd_up, _Args(if_dead=False, runner=runner, settle=0))
        # Still zero: the daemon runs the queue, and `dispatch add` still works.
        self.assertEqual(code, 0)
        self.assertIn("chat_allowlist is empty", err)
        self.assertIn("dispatch setup --chat", err)

    def test_up_reports_an_unreadable_config_once(self):
        """One command, one reading of the file: `_warn_empty_allowlist` takes
        the config `up` already loaded rather than loading its own."""
        config_mod.ensure_dirs()
        with open(config_mod.config_path(), "w") as fh:
            fh.write('{"chat_allowlist": ["1"')
        runner, _ = self._runner(alive=False)
        _, _, err = self._capture(
            cli.cmd_up, _Args(if_dead=False, runner=runner, settle=0))
        self.assertEqual(err.count("is unreadable"), 1, err)
        self.assertIn("chat_allowlist is empty", err)

    def test_up_is_quiet_about_an_allowlist_that_names_somebody(self):
        state.write(config_mod.config_path(), {"chat_allowlist": ["7256243815"]})
        runner, _ = self._runner(alive=False)
        _, _, err = self._capture(
            cli.cmd_up, _Args(if_dead=False, runner=runner, settle=0))
        self.assertNotIn("chat_allowlist", err)

    def test_up_is_idempotent_when_alive(self):
        runner, calls = self._runner(alive=True)
        self._capture(cli.cmd_up, _Args(if_dead=False, runner=runner, settle=0))
        self.assertEqual([c for c in calls if c[:2] == ["tmux", "new-session"]], [])

    def test_if_dead_is_silent_when_alive(self):
        runner, _ = self._runner(alive=True)
        code, out, err = self._capture(cli.cmd_up, _Args(if_dead=True, runner=runner, settle=0))
        self.assertEqual((code, out, err), (0, "", ""))

    def test_up_launches_the_script_not_an_importable_module(self):
        """`python3 -m dispatch.cli run` imports cli.py and exits 0.

        cli.py has no `__main__` guard, so the tmux session would die the
        instant it started and the cron watchdog would relaunch it every five
        minutes forever. What `up` runs has to be the script with the guard.
        """
        runner, calls = self._runner(alive=False)
        self._capture(cli.cmd_up, _Args(if_dead=False, runner=runner, settle=0))
        command = self._new_session(calls)[-1]
        self.assertNotIn("-m dispatch.cli", command)
        argv = self._launch_argv(calls)
        self.assertEqual(argv[0], sys.executable)
        self.assertEqual(os.path.basename(argv[1]), "dispatchd")
        self.assertTrue(os.path.exists(argv[1]), argv[1])
        with open(argv[1]) as fh:
            self.assertIn('if __name__ == "__main__":', fh.read())

    def test_up_runs_the_session_from_the_package_directory(self):
        runner, calls = self._runner(alive=False)
        self._capture(cli.cmd_up, _Args(if_dead=False, runner=runner, settle=0))
        started = self._new_session(calls)
        cwd = started[started.index("-c") + 1]
        self.assertTrue(os.path.isdir(os.path.join(cwd, "dispatch")), cwd)

    def test_up_reports_a_failure_to_start(self):
        def runner(argv, **kwargs):
            class Result:
                returncode = 1
                stdout = ""
                stderr = "no server running"
            return Result()

        code, out, err = self._capture(cli.cmd_up, _Args(if_dead=False, runner=runner, settle=0))
        self.assertEqual(code, 1)
        self.assertIn("no server running", err)
        self.assertEqual(out, "")

    def test_up_survives_tmux_not_being_installed(self):
        """From cron a traceback is invisible; a non-zero exit is not."""
        def runner(argv, **kwargs):
            raise FileNotFoundError(2, "No such file or directory", "tmux")

        code, out, err = self._capture(cli.cmd_up, _Args(if_dead=False, runner=runner, settle=0))
        self.assertEqual(code, 1)
        self.assertIn("tmux", err)

    def test_down_kills_the_session(self):
        runner, calls = self._runner(alive=True)
        code, _, _ = self._capture(cli.cmd_down, _Args(runner=runner))
        self.assertEqual(code, 0)
        self.assertIn(["tmux", "kill-session", "-t", cli.TMUX_SESSION], calls)

    def test_down_says_so_when_nothing_is_running(self):
        runner, calls = self._runner(alive=False)
        code, out, _ = self._capture(cli.cmd_down, _Args(runner=runner))
        self.assertEqual(code, 0)
        self.assertIn("not running", out)
        self.assertEqual([c for c in calls if c[1] == "kill-session"], [])

    def test_logs_daemon_reads_the_tmux_pane(self):
        runner, calls = self._runner(alive=True, stdout="daemon says hello")
        code, out, _ = self._capture(cli.cmd_logs, _Args(daemon=True, id=None,
                                                         runner=runner))
        self.assertEqual(code, 0)
        self.assertIn("capture-pane", calls[-1])
        self.assertIn(cli.TMUX_SESSION, calls[-1])
        self.assertIn("daemon says hello", out)

    def test_logs_daemon_says_so_when_the_session_is_gone(self):
        def runner(argv, **kwargs):
            class Result:
                returncode = 1
                stdout = ""
                stderr = "can't find session: dispatchd"
            return Result()

        code, out, err = self._capture(cli.cmd_logs, _Args(daemon=True, id=None,
                                                           runner=runner))
        self.assertNotEqual(code, 0)
        self.assertEqual(out, "")
        self.assertIn(cli.TMUX_SESSION, err)

    def test_logs_without_an_id_or_daemon_is_refused(self):
        code, out, err = self._capture(cli.cmd_logs, _Args(daemon=False, id=None))
        self.assertEqual(code, 2)
        self.assertIn("--daemon", err)
        self.assertEqual(out, "")

    def test_logs_still_reads_a_task_log(self):
        directory = config_mod.task_dir("t-0001")
        os.makedirs(directory)
        with open(os.path.join(directory, "worker.log"), "w") as fh:
            fh.write("step output")
        code, out, _ = self._capture(cli.cmd_logs, _Args(daemon=False, id="1"))
        self.assertEqual(code, 0)
        self.assertIn("step output", out)

    def test_up_pins_a_path_the_agents_can_be_found_on(self):
        """The session inherits the tmux server's environment, and after a
        reboot the server is the one cron started -- `/usr/bin:/bin`, without
        the directory the agents install into. The daemon would look healthy,
        the watchdog would stay quiet, chat would answer, and every task would
        fail with command-not-found. So the launch pins PATH itself.
        """
        agent_bin = self._write_agents(
            os.path.join(self.tmp.name, "agent-bin"), "claude", "codex")
        with mock.patch.dict(os.environ,
                             {"PATH": os.pathsep.join(["/usr/bin", agent_bin])}):
            runner, calls = self._runner(alive=False)
            code, _, err = self._capture(cli.cmd_up, _Args(if_dead=False, runner=runner, settle=0))
            resolved = os.path.dirname(shutil.which("claude"))
        self.assertEqual(code, 0)
        # The token warning is expected here (this harness has no token file);
        # what must not appear is a complaint about the agents.
        self.assertNotIn("not on the daemon's PATH", err)
        directories = self._launch_path(calls)
        # The directory `claude` actually resolves to, not one assumed for it.
        self.assertIn(resolved, directories)
        self.assertIn(agent_bin, directories)

    def test_up_adds_the_user_bin_directory_a_cron_path_lacks(self):
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}):
            runner, calls = self._runner(alive=False)
            self._capture(cli.cmd_up, _Args(if_dead=False, runner=runner, settle=0))
        directories = self._launch_path(calls)
        self.assertIn(os.path.join(os.path.expanduser("~"), ".local", "bin"),
                      directories)
        self.assertEqual(directories[:2], ["/usr/bin", "/bin"])

    def test_up_warns_when_the_pinned_path_still_cannot_reach_an_agent(self):
        """It starts anyway -- one working lane still answers chat, and refusing
        would take the only surface down too -- but this is the failure that
        looks like success, so it is said out loud."""
        empty = os.path.join(self.tmp.name, "empty-bin")
        os.makedirs(empty)
        with mock.patch.dict(os.environ, {"PATH": empty}):
            runner, _ = self._runner(alive=False)
            code, out, err = self._capture(cli.cmd_up, _Args(if_dead=False, runner=runner, settle=0))
        self.assertEqual(code, 0)
        self.assertIn("started dispatchd", out)
        self.assertIn("claude", err)
        self.assertIn("codex", err)

    def test_down_reports_a_kill_that_failed(self):
        def runner(argv, **kwargs):
            class Result:
                returncode = 0 if argv[:2] == ["tmux", "has-session"] else 1
                stdout = ""
                stderr = "" if argv[:2] == ["tmux", "has-session"] else "server exited"
            return Result()

        code, out, err = self._capture(cli.cmd_down, _Args(runner=runner))
        self.assertEqual(code, 1)
        self.assertNotIn("stopped", out)
        self.assertIn("server exited", err)

    def test_up_reports_a_session_that_died_on_startup(self):
        """`new-session` returns 0 the moment the session exists; with
        `remain-on-exit off` a daemon that dies during startup takes the
        session with it milliseconds later. `up` said "started" regardless --
        the same shape as the `-m dispatch.cli run` bug."""
        config_mod.ensure_dirs()
        with open(config_mod.daemon_log_path(), "w") as fh:
            fh.write("dispatchd died at 2026-08-20 09:00:00\n"
                     "Traceback (most recent call last):\n"
                     "ModuleNotFoundError: No module named 'dispatch'\n")
        runner, calls = self._runner(alive=False, survives=False)
        code, out, err = self._capture(
            cli.cmd_up, _Args(if_dead=False, runner=runner, settle=0))
        self.assertEqual(code, 1)
        self.assertNotIn("started dispatchd", out)
        self.assertIn("exited immediately", err)
        self.assertIn("ModuleNotFoundError", err)
        self.assertEqual(len([c for c in calls if c[:2] == ["tmux", "new-session"]]), 1)

    def test_up_says_so_when_a_dead_launch_left_nothing_behind(self):
        runner, _ = self._runner(alive=False, survives=False)
        code, _, err = self._capture(
            cli.cmd_up, _Args(if_dead=False, runner=runner, settle=0))
        self.assertEqual(code, 1)
        self.assertIn("dispatch run", err)

    def test_up_warns_when_there_is_no_bot_token(self):
        """Without one the daemon runs with no chat transport at all, which
        after the cutover means no interface at all."""
        runner, _ = self._runner(alive=False)
        code, out, err = self._capture(
            cli.cmd_up, _Args(if_dead=False, runner=runner, settle=0))
        self.assertEqual(code, 0)
        self.assertIn("started dispatchd", out)
        self.assertIn("no bot token", err)

    def test_up_is_quiet_about_the_token_when_there_is_one(self):
        token = os.path.join(self.tmp.name, "token.env")
        with open(token, "w") as fh:
            fh.write("TELEGRAM_BOT_TOKEN=123456:not-a-real-token\n")
        with mock.patch.dict(os.environ, {"DISPATCH_TOKEN_ENV": token}):
            runner, _ = self._runner(alive=False)
            code, _, err = self._capture(
                cli.cmd_up, _Args(if_dead=False, runner=runner, settle=0))
        self.assertEqual(code, 0)
        self.assertNotIn("bot token", err)

    def test_logs_daemon_falls_back_to_the_daemon_log(self):
        """`capture-pane` reads the live pane, so it can never say why the
        previous daemon died -- and that is the case you need a log for."""
        config_mod.ensure_dirs()
        with open(config_mod.daemon_log_path(), "w") as fh:
            fh.write("tick failed at 2026-08-20 09:00:00\nRuntimeError: kaboom\n")

        def runner(argv, **kwargs):
            class Result:
                returncode = 1
                stdout = ""
                stderr = "can't find session: dispatchd"
            return Result()

        code, out, err = self._capture(cli.cmd_logs, _Args(daemon=True, id=None,
                                                           runner=runner))
        self.assertEqual((code, err), (0, ""))
        self.assertIn("kaboom", out)
        self.assertIn(config_mod.daemon_log_path(), out)

    def test_logs_daemon_falls_back_when_the_pane_is_empty(self):
        config_mod.ensure_dirs()
        with open(config_mod.daemon_log_path(), "w") as fh:
            fh.write("tick failed at 2026-08-20 09:00:00\nRuntimeError: kaboom\n")
        runner, _ = self._runner(alive=True, stdout="   \n")
        code, out, _ = self._capture(cli.cmd_logs, _Args(daemon=True, id=None,
                                                         runner=runner))
        self.assertEqual(code, 0)
        self.assertIn("kaboom", out)

    def test_logs_daemon_still_prefers_the_live_pane(self):
        config_mod.ensure_dirs()
        with open(config_mod.daemon_log_path(), "w") as fh:
            fh.write("an old crash\n")
        runner, _ = self._runner(alive=True, stdout="daemon says hello")
        code, out, _ = self._capture(cli.cmd_logs, _Args(daemon=True, id=None,
                                                         runner=runner))
        self.assertEqual(code, 0)
        self.assertIn("daemon says hello", out)
        self.assertNotIn("an old crash", out)

    def test_the_parser_wires_up_down_and_logs_daemon(self):
        parser_ = cli.build_parser()
        self.assertIs(parser_.parse_args(["up"]).func, cli.cmd_up)
        self.assertFalse(parser_.parse_args(["up"]).if_dead)
        self.assertTrue(parser_.parse_args(["up", "--if-dead"]).if_dead)
        self.assertIs(parser_.parse_args(["down"]).func, cli.cmd_down)
        self.assertTrue(parser_.parse_args(["logs", "--daemon"]).daemon)
        self.assertIsNone(parser_.parse_args(["logs", "--daemon"]).id)


class TestCliSetup(_CliEnv):
    """`setup` edits the user's settings.json, so it is held to that standard."""

    ORIGINAL = ('{\n    "model": "opus",\n    "hooks": {"PreToolUse": [{"matcher": "Bash"}]},\n'
                '    "enabledPlugins": {\n'
                '        "telegram@claude-plugins-official": true,\n'
                '        "superpowers@claude-plugins-official": true\n    }\n}\n')

    def _settings(self, text=None):
        path = os.path.join(self.tmp.name, "settings.json")
        with open(path, "w") as fh:
            fh.write(self.ORIGINAL if text is None else text)
        return path

    def test_disable_plugin_flips_the_boolean_and_backs_up(self):
        path = self._settings()
        backup = cli.disable_plugin(path)
        self.assertTrue(os.path.exists(backup))
        with open(path) as fh:
            settings = json.load(fh)
        self.assertFalse(settings["enabledPlugins"][cli.PLUGIN_KEY])
        self.assertTrue(settings["enabledPlugins"]["superpowers@claude-plugins-official"])
        self.assertEqual(settings["model"], "opus")

    def test_disable_plugin_changes_exactly_one_value(self):
        path = self._settings()
        with open(path) as fh:
            before = json.load(fh)
        cli.disable_plugin(path)
        with open(path) as fh:
            after = json.load(fh)
        before["enabledPlugins"][cli.PLUGIN_KEY] = False
        self.assertEqual(after, before)

    def test_the_backup_is_the_users_file_byte_for_byte(self):
        """A reformatted backup is not a backup: restoring it would still
        rewrite a file we were asked not to rewrite."""
        path = self._settings()
        backup = cli.disable_plugin(path)
        with open(backup) as fh:
            self.assertEqual(fh.read(), self.ORIGINAL)

    def test_disable_plugin_is_a_noop_when_already_off(self):
        path = self._settings(
            '{"enabledPlugins": {"telegram@claude-plugins-official": false}}')
        self.assertIsNone(cli.disable_plugin(path))
        self.assertFalse(os.path.exists(path + ".bak"))

    def test_disable_plugin_leaves_unrelated_keys_intact(self):
        path = self._settings()
        cli.disable_plugin(path)
        with open(path) as fh:
            self.assertEqual(json.load(fh)["hooks"],
                             {"PreToolUse": [{"matcher": "Bash"}]})

    def test_disable_plugin_is_a_noop_on_an_unreadable_file(self):
        self.assertIsNone(cli.disable_plugin(
            os.path.join(self.tmp.name, "absent.json")))
        path = self._settings("not json at all")
        self.assertIsNone(cli.disable_plugin(path))
        self.assertFalse(os.path.exists(path + ".bak"))

    def test_a_failed_write_names_the_backup_and_leaves_the_file_alone(self):
        """The one path where cmd_setup's "backup: ..." line never prints.

        A traceback out of main() would tell the user their settings file is in
        trouble and not where the copy of it went.
        """
        path = self._settings()
        with mock.patch("os.replace", side_effect=OSError("no space left on device")):
            result, out, err = self._capture(cli.disable_plugin, path)
        self.assertIsNone(result)
        self.assertEqual(out, "")
        self.assertIn(path + ".bak", err)
        with open(path) as fh:
            self.assertEqual(fh.read(), self.ORIGINAL)
        with open(path + ".bak") as fh:
            self.assertEqual(fh.read(), self.ORIGINAL)
        leftovers = [name for name in os.listdir(os.path.dirname(path))
                     if name.startswith(".dispatch-")]
        self.assertEqual(leftovers, [])

    def test_a_failed_backup_changes_nothing(self):
        path = self._settings()
        with mock.patch("builtins.open", side_effect=_only_writes_fail(path + ".bak")):
            result, _, err = self._capture(cli.disable_plugin, path)
        self.assertIsNone(result)
        self.assertIn("nothing was changed", err)
        with open(path) as fh:
            self.assertEqual(fh.read(), self.ORIGINAL)

    def test_a_symlinked_settings_file_stays_a_symlink(self):
        """Renaming onto the link would leave the user with a regular file
        where they had put a link into a dotfiles checkout."""
        real = os.path.join(self.tmp.name, "real-settings.json")
        with open(real, "w") as fh:
            fh.write(self.ORIGINAL)
        link = os.path.join(self.tmp.name, "settings.json")
        os.symlink(real, link)
        backup = cli.disable_plugin(link)
        self.assertEqual(backup, link + ".bak")
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.path.realpath(link), real)
        with open(real) as fh:
            self.assertFalse(json.load(fh)["enabledPlugins"][cli.PLUGIN_KEY])

    def test_both_files_keep_the_original_permissions(self):
        path = self._settings()
        os.chmod(path, 0o640)
        backup = cli.disable_plugin(path)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o640)
        self.assertEqual(stat.S_IMODE(os.stat(backup).st_mode), 0o640)

    def test_setup_disables_the_plugin_and_writes_config(self):
        path = self._settings()
        code, out, _ = self._capture(cli.cmd_setup, _Args(
            settings=path, keep_plugin=False, repo=None, chat=["7256243815"]))
        self.assertEqual(code, 0)
        self.assertIn("disabled %s" % cli.PLUGIN_KEY, out)
        self.assertIn(path + ".bak", out)
        self.assertIn("dispatch up", out)
        with open(path) as fh:
            self.assertFalse(json.load(fh)["enabledPlugins"][cli.PLUGIN_KEY])
        self.assertEqual(config_mod.load()["chat_allowlist"], ["7256243815"])

    def test_setup_keeps_a_config_it_could_not_read_before_overwriting_it(self):
        """`setup` merges onto `config.load()`, and a corrupt file loads as
        defaults -- so writing would silently discard whatever repo overrides
        and chat ids were in there. The bytes are kept next to it instead."""
        config_mod.ensure_dirs()
        with open(config_mod.config_path(), "w") as fh:
            fh.write('{"chat_allowlist": ["7256243815"')  # a truncated write
        path = self._settings()
        code, _, err = self._capture(cli.cmd_setup, _Args(
            settings=path, keep_plugin=False, repo=None, chat=["1"]))
        self.assertEqual(code, 0)
        kept = config_mod.config_path() + ".corrupt"
        self.assertIn(kept, err)
        with open(kept) as fh:
            self.assertEqual(fh.read(), '{"chat_allowlist": ["7256243815"')
        self.assertEqual(config_mod.load()["chat_allowlist"], ["1"])

    def test_setup_survives_a_config_it_cannot_even_read(self):
        """`body` was bound inside the try, so a *read* failure fell through to
        `fh.write(body)` and raised UnboundLocalError. `main()` does not catch,
        and this runs after the plugin has already been disabled -- so the
        cutover would be left with neither interface, in the exact scenario the
        function was added to handle."""
        config_mod.ensure_dirs()
        cases = {}
        with open(config_mod.config_path(), "wb") as fh:
            fh.write(b"\xff\xfe not utf-8")            # read() raises ValueError
        cases["undecodable"] = None
        path = self._settings()
        code, _, err = self._capture(cli.cmd_setup, _Args(
            settings=path, keep_plugin=False, repo=None, chat=["1"]))
        self.assertEqual(code, 0)
        self.assertIn(config_mod.config_path(), err)
        # Nothing could be read, so nothing is kept -- and nothing crashes.
        self.assertFalse(os.path.exists(config_mod.config_path() + ".corrupt"))
        self.assertEqual(config_mod.load()["chat_allowlist"], ["1"])

    def test_setup_survives_a_config_it_is_not_allowed_to_read(self):
        config_mod.ensure_dirs()
        with open(config_mod.config_path(), "w") as fh:
            fh.write('{"chat_allowlist": ["7256243815"')
        os.chmod(config_mod.config_path(), 0o000)
        self.addCleanup(lambda: os.path.exists(config_mod.config_path())
                        and os.chmod(config_mod.config_path(), 0o600))
        path = self._settings()
        code, _, err = self._capture(cli.cmd_setup, _Args(
            settings=path, keep_plugin=False, repo=None, chat=["1"]))
        self.assertEqual(code, 0)
        self.assertIn("could not", err.lower())

    def test_the_kept_copy_is_no_more_readable_than_the_config(self):
        """It holds the same chat ids and repo paths, at the ambient umask."""
        config_mod.ensure_dirs()
        with open(config_mod.config_path(), "w") as fh:
            fh.write('{"chat_allowlist": ["7256243815"')
        os.chmod(config_mod.config_path(), 0o600)
        path = self._settings()
        self._capture(cli.cmd_setup, _Args(
            settings=path, keep_plugin=False, repo=None, chat=["1"]))
        kept = config_mod.config_path() + ".corrupt"
        self.assertEqual(stat.S_IMODE(os.stat(kept).st_mode), 0o600)

    def test_setup_warns_when_it_writes_a_config_that_admits_nobody(self):
        """`--chat` is the whole authentication boundary, and setup is where a
        cutover would notice it was never passed."""
        path = self._settings()
        code, _, err = self._capture(cli.cmd_setup, _Args(
            settings=path, keep_plugin=False, repo=None, chat=None))
        self.assertEqual(code, 0)
        self.assertIn("chat_allowlist is empty", err)
        code, _, err = self._capture(cli.cmd_setup, _Args(
            settings=path, keep_plugin=False, repo=None, chat=["7256243815"]))
        self.assertNotIn("chat_allowlist is empty", err)

    def test_setup_keeps_the_plugin_when_told_to_and_warns(self):
        path = self._settings()
        code, _, err = self._capture(cli.cmd_setup, _Args(
            settings=path, keep_plugin=True, repo=None, chat=None))
        self.assertEqual(code, 0)
        self.assertIn("409", err)
        with open(path) as fh:
            self.assertTrue(json.load(fh)["enabledPlugins"][cli.PLUGIN_KEY])
        self.assertFalse(os.path.exists(path + ".bak"))

    LIST_FORM = ('{"enabledPlugins": ["telegram@claude-plugins-official", '
                 '"superpowers@claude-plugins-official"]}')

    def test_the_reader_and_the_writer_agree_on_both_shapes(self):
        """`_plugin_enabled` read the list form and `disable_plugin` refused
        it, so a genuinely enabled plugin was reported as impossible to turn
        off -- and setup said "do it by hand" about something it could do."""
        for text in (self.ORIGINAL, self.LIST_FORM):
            path = self._settings(text)
            self.assertTrue(cli._plugin_enabled(path), text)
            self.assertIsNotNone(cli.disable_plugin(path), text)
            self.assertFalse(cli._plugin_enabled(path), text)
            os.unlink(path + ".bak")

    def test_the_list_form_loses_only_that_entry(self):
        path = self._settings(self.LIST_FORM)
        cli.disable_plugin(path)
        with open(path) as fh:
            self.assertEqual(json.load(fh)["enabledPlugins"],
                             ["superpowers@claude-plugins-official"])

    def test_the_list_form_is_a_noop_when_the_plugin_is_not_in_it(self):
        path = self._settings('{"enabledPlugins": ["superpowers@claude-plugins-official"]}')
        self.assertIsNone(cli.disable_plugin(path))
        self.assertFalse(os.path.exists(path + ".bak"))

    def test_setup_exits_non_zero_when_the_plugin_is_still_enabled(self):
        """One token admits one consumer. Leaving the plugin on guarantees the
        409 this command exists to prevent, and a scripted cutover reads the
        exit status, not the stderr line."""
        path = self._settings()
        with mock.patch.object(cli, "disable_plugin", return_value=None):
            code, out, err = self._capture(cli.cmd_setup, _Args(
                settings=path, keep_plugin=False, repo=None, chat=["7256243815"]))
        self.assertEqual(code, 1)
        self.assertIn("still enabled", err)
        self.assertNotIn("dispatch up", out)
        # Everything else it does still happened; only the verdict changed.
        self.assertEqual(config_mod.load()["chat_allowlist"], ["7256243815"])

    def test_the_setup_parser_swaps_force_for_keep_plugin(self):
        """Parser wiring only. That `setup` actually disables the plugin is
        `test_setup_disables_the_plugin_and_writes_config`'s job."""
        parser_ = cli.build_parser()
        self.assertTrue(parser_.parse_args(["setup", "--keep-plugin"]).keep_plugin)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser_.parse_args(["setup", "--force"])


class TestCliAdd(_CliEnv):
    """`dispatch add` is the only way to queue work when Telegram is down.

    It resolved against the hand-configured alias map, which `setup` no longer
    populates, so it refused every repo the daemon happily accepts from chat.
    There was no test for it at all -- which is how it survived the branch.
    """

    def setUp(self):
        super().setUp()
        self.projects = os.path.join(self.tmp.name, "Projects")
        os.makedirs(os.path.join(self.projects, "qpay", ".git"))
        os.makedirs(os.path.join(self.projects, "notes"))  # a folder, not a repo
        self._config()

    def _config(self, **over):
        cfg = {"projects_root": self.projects}
        cfg.update(over)
        state.write(config_mod.config_path(), cfg)

    def _args(self, **over):
        base = dict(repo="qpay", prompt="do the thing", priority=5, dep=None,
                    worktree=False, agent=lanes.CLAUDE)
        base.update(over)
        return _Args(**base)

    def test_add_queues_a_discovered_repo(self):
        code, out, err = self._capture(cli.cmd_add, self._args())
        self.assertEqual((code, err), (0, ""))
        self.assertIn("t-0001", out)
        task = state.read_queue()["tasks"][0]
        self.assertEqual((task["repo"], task["prompt"]), ("qpay", "do the thing"))

    def test_add_refuses_a_folder_that_is_not_a_repo(self):
        code, out, err = self._capture(cli.cmd_add, self._args(repo="notes"))
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("not dispatchable", err)
        self.assertEqual(state.read_queue()["tasks"], [])

    def test_add_refuses_an_unknown_repo_the_way_the_daemon_does(self):
        """One resolver, one rejection message -- the CLI must not have its own."""
        code, _, err = self._capture(cli.cmd_add, self._args(repo="nope"))
        self.assertEqual(code, 2)
        found = repos.discover(root=self.projects)
        self.assertIn(repos.reject_reason("nope", found), err)
        self.assertIn("qpay", err)

    def test_add_honours_a_configured_override_outside_the_root(self):
        outside = os.path.join(self.tmp.name, "elsewhere")
        os.makedirs(os.path.join(outside, ".git"))
        self._config(repos={"elsewhere": outside})
        code, _, err = self._capture(cli.cmd_add, self._args(repo="elsewhere"))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(state.read_queue()["tasks"][0]["repo"], "elsewhere")

    def test_add_still_carries_priority_deps_and_isolation(self):
        self._capture(cli.cmd_add, self._args())
        self._capture(cli.cmd_add, self._args(priority=1, dep=["t-0001"],
                                              worktree=True))
        task = state.read_queue()["tasks"][1]
        self.assertEqual(task["priority"], 1)
        self.assertEqual(task["deps"], ["t-0001"])
        self.assertEqual(task["isolation"], "worktree")

    def test_add_puts_the_task_in_the_named_lane(self):
        self._capture(cli.cmd_add, self._args(agent=lanes.CODEX))
        self.assertEqual(state.read_queue()["tasks"][0]["agent"], lanes.CODEX)

    def test_add_defaults_to_the_claude_lane(self):
        self._capture(cli.cmd_add, self._args())
        self.assertEqual(state.read_queue()["tasks"][0]["agent"], lanes.CLAUDE)

    def test_the_add_parser_wires_the_lane_flag(self):
        parser_ = cli.build_parser()
        self.assertEqual(parser_.parse_args(["add", "qpay", "x"]).agent, lanes.CLAUDE)
        self.assertEqual(
            parser_.parse_args(["add", "qpay", "x", "--agent", "codex"]).agent,
            lanes.CODEX)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser_.parse_args(["add", "qpay", "x", "--agent", "gemini"])


class TestWeekWindow(unittest.TestCase):
    """The week window and the wind-down machine used to disagree.

    `admit` refuses at `week_pct >= week_soft`; `next_mode` never looked at the
    week at all. A lane at 100% of its weekly window therefore reported
    `running` -- which the user is told to read as "dispatching normally" --
    and dispatched nothing, forever, with no reason given anywhere.
    """

    def test_a_full_week_window_freezes_an_idle_lane(self):
        self.assertEqual(
            winddown.next_mode("running", 3.0, 0, CONFIG, week_pct=100.0),
            winddown.FROZEN)

    def test_a_full_week_window_drains_in_flight_work_first(self):
        self.assertEqual(
            winddown.next_mode("running", 3.0, 1, CONFIG, week_pct=100.0),
            winddown.WINDING_DOWN)

    def test_below_the_week_soft_limit_nothing_changes(self):
        self.assertEqual(
            winddown.next_mode("running", 3.0, 0, CONFIG, week_pct=89.9),
            winddown.RUNNING)

    def test_the_boundary_is_the_same_one_admission_uses(self):
        soft = CONFIG["week_soft"]
        self.assertEqual(winddown.next_mode("running", 3.0, 0, CONFIG,
                                            week_pct=soft), winddown.FROZEN)
        ok, _ = scheduler.admit(
            {"id": "t-0001", "repo": "demo", "state": "queued", "priority": 5,
             "deps": [], "isolation": "repo"},
            {"mode": "running", "queue": {"tasks": []}, "running": 0,
             "session_pct": 3.0, "week_pct": soft, "stale": False,
             "est_cost_pct": 1.0, "config": CONFIG, "lock_free": lambda n: True})
        self.assertFalse(ok)

    def test_a_pause_still_outranks_the_week_window(self):
        self.assertEqual(
            winddown.next_mode(winddown.PAUSED, 3.0, 0, CONFIG, week_pct=100.0),
            winddown.PAUSED)

    def test_an_unknown_week_reading_changes_nothing(self):
        self.assertEqual(
            winddown.next_mode("running", 3.0, 0, CONFIG, week_pct=None),
            winddown.RUNNING)

    def test_a_confirmed_session_reset_does_not_resume_a_week_frozen_lane(self):
        """Otherwise the lane resumes and re-freezes on every tick, and the
        user gets a freeze notice per tick until the week rolls over."""
        doc = {"mode": winddown.FROZEN, "armed_resume_at": 5060.0}
        ok, why = winddown.can_resume(doc, 3.0, 5100.0, CONFIG, False,
                                      week_pct=100.0)
        self.assertFalse(ok)
        self.assertIn("week", why)
        self.assertTrue(winddown.can_resume(doc, 3.0, 5100.0, CONFIG, False,
                                            week_pct=10.0)[0])

    def test_a_freeze_waits_for_whichever_window_is_actually_full(self):
        reading = {"session_pct": 3.0, "week_pct": 100.0,
                   "resets_at": 5000.0, "week_resets_at": 900000.0}
        self.assertEqual(winddown.binding_reset(reading, CONFIG), 900000.0)
        self.assertEqual(
            winddown.binding_reset(dict(reading, week_pct=1.0), CONFIG), 5000.0)

    def test_a_lane_with_no_session_reading_does_not_report_zero_percent(self):
        """`session 0%` reads as an empty window; it meant "no reading at all"."""
        line = governor.pct_line({"session_pct": 0.0, "session_known": False,
                                  "week_pct": 100.0, "source": "codex-unknown"})
        self.assertNotIn("session 0%", line)
        self.assertIn("week 100%", line)
        self.assertIn("session", line)

    def test_a_real_zero_still_reads_as_zero(self):
        self.assertIn("session 0%", governor.pct_line(
            {"session_pct": 0.0, "week_pct": 1.0, "source": "measured"}))

    def test_the_codex_estimate_says_whether_it_read_a_session_percentage(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        now = 1_800_000_000.0
        directory = os.path.join(tmp.name, "sessions")
        os.makedirs(directory)
        with open(os.path.join(directory, "a.jsonl"), "w") as fh:
            fh.write(json.dumps({"payload": {"info": {"rate_limits": {
                "primary": {"used_percent": 100.0, "window_minutes": 10080,
                            "resets_at": now + 86400}}}}}) + "\n")
        estimate = governor.codex.estimate(now, root=directory)
        # A week-only record: the session percentage is absent, not zero.
        self.assertEqual(estimate["session_pct"], 0.0)
        self.assertFalse(estimate["session_known"])
        self.assertEqual(estimate["week_resets_at"], now + 86400)
        self.assertNotIn("session 0%", governor.pct_line(estimate))
        self.assertFalse(governor.codex.estimate(
            now, root=os.path.join(tmp.name, "nothing"))["session_known"])


class _StepProcess:
    """A step that either writes a status file or dies without writing one."""

    def __init__(self, task_dir, block=None, hangs=False):
        self.task_dir = task_dir
        self.block = block
        self.hangs = hangs
        self.returncode = 0
        self.signals = []

    def _write(self):
        if self.block is None:
            return
        with open(os.path.join(self.task_dir, "last.json"), "w") as fh:
            json.dump(self.block, fh)

    def communicate(self, timeout=None):
        if self.hangs and not self.signals:
            raise subprocess.TimeoutExpired("codex", timeout or 0)
        self._write()
        return "", None

    def send_signal(self, signal_number):
        self.signals.append(signal_number)

    def kill(self):
        self.signals.append("kill")
        self.returncode = -9

    def poll(self):
        # Ignores SIGTERM, so the grace period expires and SIGKILL follows.
        return self.returncode if "kill" in self.signals else None


class _Response:
    """The shape ``urlopen`` returns: a context manager over bytes."""

    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self):
        return self._payload


class TestChatTransportHealth(unittest.TestCase):
    """Every transport failure used to return `[], offset` and say nothing.

    After the cutover Telegram is the only interface, and its total failure is
    indistinguishable from nobody sending anything: the daemon is up, the queue
    is healthy, the watchdog is quiet, and the bot answers nothing.
    """

    TOKEN = "123456:SECRET-BOT-TOKEN"

    def _chat(self, opener):
        from dispatch import chat as chat_mod
        # An allowlist, because a live transport now refuses to exist without
        # one: an empty allowlist is nobody, and there is nothing to poll for.
        return chat_mod.Chat(self.TOKEN, ["1"], opener=opener)

    def test_a_failed_poll_is_counted_and_its_reason_kept(self):
        chat = self._chat(mock.Mock(side_effect=OSError("connection refused")))
        self.assertEqual(chat.poll(7), ([], 7))
        self.assertEqual(chat.failures, 1)
        chat.poll(7)
        self.assertEqual(chat.failures, 2)
        self.assertIn("connection refused", chat.last_error)

    def test_a_working_poll_clears_the_record(self):
        calls = []

        def opener(request, timeout=None):
            calls.append(1)
            if len(calls) == 1:
                raise OSError("connection refused")
            return _Response({"result": []})

        chat = self._chat(opener)
        chat.poll(0)
        self.assertEqual(chat.failures, 1)
        self.assertEqual(chat.poll(0), ([], 0))
        self.assertEqual(chat.failures, 0)
        self.assertIsNone(chat.last_error)

    def test_the_token_never_reaches_the_recorded_error(self):
        """`chat_last_error` is written to state.json, and the token is never
        copied there."""
        chat = self._chat(mock.Mock(side_effect=OSError(
            "HTTP Error 409 for https://api.telegram.org/bot%s/getUpdates"
            % TestChatTransportHealth.TOKEN)))
        chat.poll(0)
        self.assertNotIn("SECRET-BOT-TOKEN", chat.last_error)
        self.assertIn("<token>", chat.last_error)

    def test_a_failed_send_counts_too(self):
        chat = self._chat(mock.Mock(side_effect=OSError("timed out")))
        self.assertFalse(chat.send("1", "hello"))
        self.assertEqual(chat.failures, 1)

    def test_the_null_transport_carries_the_reason_there_is_no_transport(self):
        from dispatch import chat as chat_mod
        self.assertIsNone(chat_mod.NullChat().last_error)
        offline = chat_mod.NullChat(reason="no bot token at /nowhere")
        self.assertIn("no bot token", offline.last_error)
        self.assertEqual(offline.failures, 0)
        self.assertEqual(offline.poll(3), ([], 3))
        self.assertIn("no bot token", offline.last_error)


class TestChatAllowlist(unittest.TestCase):
    """The allowlist is the whole authentication boundary after the cutover.

    An empty one used to mean "everybody". ``config.load()`` falls back to the
    defaults when config.json is missing or corrupt, and the default allowlist
    is empty, so a truncated write or a bad hand-edit turned authentication off
    while the bot went on answering normally -- and every message that reached
    it could point an unattended agent at any repo under the projects root.
    """

    TOKEN = "123456:SECRET-BOT-TOKEN"

    def _chat_mod(self):
        from dispatch import chat as chat_mod
        return chat_mod

    def test_an_empty_allowlist_admits_nobody(self):
        """Empty means nobody. This is the assertion the boundary rests on."""
        chat = self._chat_mod().Chat(self.TOKEN, ["1"])
        chat.allowlist = []  # what a corrupt or missing config used to produce
        self.assertFalse(chat.allowed("1"))
        self.assertFalse(chat.allowed("7256243815"))

    def test_a_populated_allowlist_admits_only_what_it_lists(self):
        chat = self._chat_mod().Chat(self.TOKEN, [7256243815])
        self.assertTrue(chat.allowed("7256243815"))
        self.assertTrue(chat.allowed(7256243815))
        self.assertFalse(chat.allowed("7256243816"))
        self.assertFalse(chat.allowed(""))

    def test_a_live_transport_refuses_to_exist_without_an_allowlist(self):
        """Defence in depth: even if `allowed` were widened again later, there
        is no way to build a transport that has nobody to admit."""
        for empty in (None, [], ()):
            with self.assertRaises(ValueError):
                self._chat_mod().Chat(self.TOKEN, empty)

    # Every shape a hand-edited config.json can hold, and what each must
    # normalise to. `chat_allowlist` is typed by whoever edits the file, and
    # the file is required to stay hand-repairable, so "wrong type" is an
    # expected input rather than an impossible one.
    SHAPES = [
        ("7256243815", ["7256243815"]),      # one id, no brackets
        (b"7256243815", ["7256243815"]),
        (7256243815, ["7256243815"]),        # unquoted in JSON
        (["7256243815"], ["7256243815"]),
        (["7256243815", 42], ["7256243815", "42"]),
        (("7256243815",), ["7256243815"]),
        (True, []),                          # not an allowlist
        (False, []),
        ({"a": 1}, []),                      # would have iterated to ["a"]
        (1.5, []),
        (None, []),
        ([], []),
        ([None], []),                        # would have become ["None"]
        (["", "   "], []),                   # non-empty, admitting nobody real
    ]

    def test_normalize_reduces_every_config_shape_to_ids(self):
        """One normaliser, because there are three consumers and they must not
        disagree about what a config value means."""
        for value, expected in self.SHAPES:
            self.assertEqual(
                self._chat_mod().normalize_allowlist(value), expected, repr(value))

    def test_normalize_never_raises(self):
        """It runs at daemon startup, on a value a person typed. Raising there
        is the cron crash-loop the NullChat fallback exists to prevent."""
        class Awkward:
            def __str__(self):
                return "9"

        for value in (object(), Awkward(), [Awkward()], set(), {"a": 1}.keys()):
            self._chat_mod().normalize_allowlist(value)

    def test_a_single_id_written_as_a_string_is_not_read_as_digits(self):
        """`"chat_allowlist": "725..."` iterates into characters, and "7" is a
        chat id someone could hold. The production path is asserted in
        `TestTwoLaneDaemon`; this is the constructor's own half."""
        chat = self._chat_mod().Chat(self.TOKEN, "7256243815")
        self.assertEqual(chat.allowlist, ["7256243815"])
        self.assertFalse(chat.allowed("7"))

    def test_the_constructor_refuses_every_shape_that_names_nobody(self):
        for value, expected in self.SHAPES:
            if expected:
                continue
            with self.assertRaises(ValueError, msg=repr(value)):
                self._chat_mod().Chat(self.TOKEN, value)

    def test_poll_drops_a_message_from_an_unlisted_chat(self):
        payload = {"result": [
            {"update_id": 1, "message": {"text": "status", "chat": {"id": 1},
                                         "message_id": 11}},
            {"update_id": 2, "message": {"text": "claude rm -rf on qpay",
                                         "chat": {"id": 999}, "message_id": 12}}]}
        chat = self._chat_mod().Chat(
            self.TOKEN, ["1"],
            opener=lambda request, timeout=None: _Response(payload))
        messages, offset = chat.poll(0)
        self.assertEqual([m["chat_id"] for m in messages], ["1"])
        self.assertEqual(offset, 3)

    def test_the_null_transport_admits_because_it_delivers_nothing(self):
        """`NullChat.allowed` staying True is safe: it polls nothing, so there
        is no message for it to admit, and it sends to a list."""
        null = self._chat_mod().NullChat()
        self.assertTrue(null.allowed("999"))
        self.assertEqual(null.poll(0), ([], 0))


class TestConfigIntegrity(unittest.TestCase):
    """config.json carries a security boundary, so failing to read it is not
    an event that may pass in silence."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = mock.patch.dict(os.environ, {"DISPATCH_HOME": self.tmp.name})
        home.start()
        self.addCleanup(home.stop)

    def _load(self, overrides=None):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            cfg = config_mod.load(overrides)
        return cfg, err.getvalue()

    def test_a_missing_config_is_normal_and_stays_silent(self):
        """First run, and every DISPATCH_HOME a test points at."""
        cfg, err = self._load()
        self.assertEqual(err, "")
        self.assertEqual(cfg["chat_allowlist"], [])

    def test_a_corrupt_config_says_so_on_stderr(self):
        with open(config_mod.config_path(), "w") as fh:
            fh.write('{"chat_allowlist": ["7256243815"')  # a truncated write
        cfg, err = self._load()
        self.assertIn(config_mod.config_path(), err)
        self.assertIn("allowlist", err)
        # Falls back to defaults, which now admit nobody rather than everybody.
        self.assertEqual(cfg["chat_allowlist"], [])

    def test_a_config_that_is_not_an_object_is_corruption_too(self):
        with open(config_mod.config_path(), "w") as fh:
            fh.write('["chat_allowlist"]')
        cfg, err = self._load()
        self.assertIn("not an object", err)
        self.assertEqual(cfg["chat_allowlist"], [])

    def test_a_config_that_is_literally_null_is_corruption_too(self):
        """Valid JSON, and the one wrong shape that used to pass in silence --
        `json.load` returns None and the old guard only checked non-None."""
        with open(config_mod.config_path(), "w") as fh:
            fh.write("null")
        cfg, err = self._load()
        self.assertIn(config_mod.config_path(), err)
        self.assertEqual(cfg["chat_allowlist"], [])

    def test_a_readable_config_still_loads_and_still_takes_overrides(self):
        state.write(config_mod.config_path(),
                    {"chat_allowlist": ["1"], "poll_floor": 5})
        cfg, err = self._load()
        self.assertEqual(err, "")
        self.assertEqual(cfg["chat_allowlist"], ["1"])
        self.assertEqual(cfg["poll_floor"], 5)
        self.assertEqual(self._load({"poll_floor": 9})[0]["poll_floor"], 9)


class TestStepStatusIsNotInherited(unittest.TestCase):
    """A step that dies must not settle as the previous step's success.

    The codex backend reads its status from ``task_dir/last.json``, written by
    ``codex -o``. Nothing cleared it between steps, so a step SIGKILLed at
    ``step_timeout`` read the *previous* step's block: status ``continue``,
    requeued, ``steps_done`` incremented, and a checkpoint commit labelled with
    a summary describing work this step never did. There is no step cap
    anywhere, so that loops for as long as the plan window allows.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.task = {"id": "t-0001", "repo": "qpay", "prompt": "do a thing",
                     "branch": "tg/t-0001", "agent": "codex", "session_id": None}

    def _stale(self):
        with open(os.path.join(self.tmp.name, "last.json"), "w") as fh:
            json.dump({"status": "continue", "summary": "step one did the schema",
                       "next": "wire it up"}, fh)

    def _run(self, block=None, hangs=False):
        process = _StepProcess(self.tmp.name, block=block, hangs=hangs)
        result = worker.run_step(self.task, self.tmp.name, CONFIG,
                                 popen=lambda argv, **kwargs: process,
                                 sleeper=lambda seconds: None,
                                 task_dir=self.tmp.name)
        return result, process

    def test_a_killed_step_does_not_inherit_the_previous_summary(self):
        self._stale()
        result, process = self._run(hangs=True)
        self.assertTrue(result["timed_out"])
        self.assertIn("kill", process.signals)
        self.assertIsNone(result["status"])
        self.assertEqual(result["summary"], "")

    def test_a_step_that_writes_nothing_reports_nothing(self):
        self._stale()
        result, _ = self._run()
        self.assertIsNone(result["status"])

    def test_a_step_that_writes_its_own_status_still_settles(self):
        self._stale()
        result, _ = self._run(block={"status": "complete", "summary": "this step",
                                     "next": ""})
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["summary"], "this step")

    def test_clearing_is_safe_when_there_is_nothing_to_clear(self):
        result, _ = self._run(block={"status": "complete", "summary": "first",
                                     "next": ""})
        self.assertEqual(result["status"], "complete")

    def test_a_claude_step_is_unaffected(self):
        """Claude reports in its stdout, so there is nothing to clear."""
        task = dict(self.task, agent="claude")
        process = _StepProcess(self.tmp.name)
        process.communicate = lambda timeout=None: (
            '```json\n{"status": "complete", "summary": "ok"}\n```', None)
        result = worker.run_step(task, self.tmp.name, CONFIG,
                                 popen=lambda argv, **kwargs: process,
                                 task_dir=self.tmp.name)
        self.assertEqual(result["status"], "complete")


class TestWorktreePlacement(unittest.TestCase):
    """Where isolated checkouts go, and what can see them there.

    No git here: this is about the location, and the location is what decides
    whether a worktree can be dispatched to as a repository in its own right.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "dispatch-home")
        home = mock.patch.dict(os.environ, {"DISPATCH_HOME": self.home})
        home.start()
        self.addCleanup(home.stop)
        self.projects = os.path.join(self.tmp.name, "Projects")
        os.makedirs(os.path.join(self.projects, "qpay", ".git"))

    def test_the_default_root_is_dispatch_state_not_the_projects_tree(self):
        root = worktrees.root({})
        self.assertEqual(os.path.realpath(root),
                         os.path.realpath(os.path.join(self.home, "worktrees")))
        self.assertFalse(os.path.realpath(root).startswith(
            os.path.realpath(self.projects) + os.sep))
        self.assertEqual(worktrees.path("t-0007", {}),
                         os.path.join(root, "t-0007"))

    def test_the_root_is_one_config_value_away(self):
        """The only reason to move it: codex resume steps carry no sandbox
        flag and fall back to the path-keyed trust levels in
        `~/.codex/config.toml`. That is a machine fact, not a code change."""
        elsewhere = os.path.join(self.tmp.name, "trusted")
        self.assertEqual(worktrees.root({"worktree_root": elsewhere}), elsewhere)

    def test_a_worktree_at_the_default_root_is_not_discoverable_as_a_repo(self):
        """`repos._entry` accepts a `.git` *file*, which is exactly what a
        linked worktree has. Parked among the checkouts, one would be listed
        and dispatchable -- another lane could then be pointed straight into an
        isolated task's private tree, under a different lock."""
        tree = worktrees.path("t-0001", {})
        os.makedirs(tree)
        with open(os.path.join(tree, ".git"), "w") as fh:
            fh.write("gitdir: %s/qpay/.git/worktrees/t-0001\n" % self.projects)

        found = repos.discover(root=self.projects)
        self.assertEqual(sorted(found), ["qpay"])
        self.assertIsNone(repos.resolve("t-0001", root=self.projects))
        # And the hazard is real: dropped into the projects root it *would* be.
        planted = os.path.join(self.projects, "t-0001")
        shutil.copytree(tree, planted)
        self.assertTrue(repos.discover(root=self.projects)["t-0001"]["git"])


class TestWorktreeGitFailures(unittest.TestCase):
    """git is a subprocess, and every way it can go wrong has to come back as
    a reason on the task rather than as an exception out of the tick."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = mock.patch.dict(
            os.environ, {"DISPATCH_HOME": os.path.join(self.tmp.name, "home")})
        home.start()
        self.addCleanup(home.stop)
        self.task = {"id": "t-0001", "repo": "qpay", "branch": "tg/t-0001"}
        self.repo = os.path.join(self.tmp.name, "qpay")
        os.makedirs(self.repo)

    def test_no_git_on_path_is_a_reason_not_a_traceback(self):
        def missing(args, cwd=None, **kwargs):
            raise FileNotFoundError(2, "No such file or directory: 'git'")

        made = worktrees.ensure(self.task, self.repo, config={}, runner=missing)
        self.assertIsNone(made["path"])
        self.assertIn("git", made["error"])

    def test_a_failed_add_reports_gits_own_words(self):
        def runner(args, cwd=None, **kwargs):
            failed = args[1] == "worktree" and args[2] == "add"
            return subprocess.CompletedProcess(
                args, 1 if failed else 1,
                stdout="Preparing worktree (new branch)\n" if failed else "",
                stderr="fatal: invalid reference: HEAD\n" if failed else "")

        made = worktrees.ensure(self.task, self.repo, config={}, runner=runner)
        self.assertIsNone(made["path"])
        self.assertEqual(made["error"], "fatal: invalid reference: HEAD")
        self.assertFalse(made["retry"])

    def test_a_busy_creation_lock_defers_instead_of_failing(self):
        held = state.try_lock(worktrees.add_lock_name("qpay"))
        self.addCleanup(state.release, held)
        calls = []
        made = worktrees.ensure(self.task, self.repo, config={},
                                runner=lambda *a, **k: calls.append(a))
        self.assertTrue(made["retry"])
        self.assertIsNone(made["error"])
        self.assertEqual(calls, [], "no git may run while the lock is held")


class TestIsolationNeverFallsBack(unittest.TestCase):
    """An isolated task that cannot get a worktree must not run anywhere.

    The one behaviour that may never be implemented is using the parent
    checkout instead: `scheduler.lock_name` has already told admission that
    this task does not contend for the repo, so the shared tree is exactly
    where it must not go.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = mock.patch.dict(
            os.environ,
            {"DISPATCH_HOME": os.path.join(self.tmp.name, "home"),
             "DISPATCH_TOKEN_ENV": os.path.join(self.tmp.name, "absent.env")})
        home.start()
        self.addCleanup(home.stop)
        self.projects = os.path.join(self.tmp.name, "Projects")
        os.makedirs(os.path.join(self.projects, "qpay", ".git"))
        self.logs = os.path.join(self.tmp.name, "empty-logs")
        os.makedirs(self.logs)
        self.now = 1_800_000_000.0
        self.started = []

    def _daemon(self):
        from dispatch import chat as chat_mod
        from dispatch import daemon as daemon_mod
        daemon = daemon_mod.Daemon(
            checkpoint=lambda task, cwd, message: {"ok": True, "committed": True},
            config={"projects_root": self.projects, "chat_allowlist": ["1"]},
            clock=lambda: self.now,
            poll_usage=lambda: {"ok": True, "at": self.now, "session_pct": 10.0,
                                "session_reset": self.now + 3600, "week_pct": 5.0,
                                "week_reset": self.now + 86400},
            count_tokens=lambda: 0,
            codex_estimate=lambda now: {"session_pct": 10.0, "week_pct": 0.0,
                                        "source": "codex-logs", "stale": False,
                                        "resets_at": None},
            chat=chat_mod.NullChat(),
            run_step=lambda task, cwd: self.started.append((task["id"], cwd)) or {
                "status": "complete", "summary": "ok", "next": "", "output": "",
                "limit_reset_at": None, "session_id": None, "timed_out": False},
            executor=daemon_mod.InlineExecutor())
        daemon.volume_block = lambda now: volume.render(
            now, claude_root=self.logs, codex_root=self.logs)
        self.addCleanup(self._release, daemon)
        return daemon

    @staticmethod
    def _release(daemon):
        for entry in daemon.running.values():
            state.release(entry["lock"])
        daemon.running.clear()

    def _queue(self):
        with state.mutate_queue() as queue:
            state.new_task(queue, "qpay", "isolated", isolation="worktree")

    def test_a_creation_failure_blocks_the_task_and_says_why(self):
        self._queue()
        daemon = self._daemon()
        with mock.patch.object(worktrees, "ensure", return_value={
                "path": None, "error": "fatal: invalid reference: HEAD",
                "retry": False}):
            daemon.tick()

        self.assertEqual(self.started, [])
        task = state.find(state.read_queue(), "t-0001")
        self.assertEqual(task["state"], "blocked")
        self.assertIn("no worktree", task["last_error"])
        self.assertIn("invalid reference", task["last_error"])
        self.assertTrue(any("no worktree" in n for n in daemon.notices),
                        daemon.notices)

    def test_metadata_held_elsewhere_defers_rather_than_blocking(self):
        """Another process mid-`worktree add` in this repository is not an
        error. The task stays queued, exactly as a busy repo lock leaves it."""
        self._queue()
        daemon = self._daemon()
        with mock.patch.object(worktrees, "ensure", return_value={
                "path": None, "error": None, "retry": True}):
            daemon.tick()

        self.assertEqual(self.started, [])
        self.assertEqual(state.find(state.read_queue(), "t-0001")["state"], "queued")

    def test_the_step_runs_in_the_worktree_not_the_repo(self):
        self._queue()
        tree = os.path.join(self.tmp.name, "somewhere-else")
        daemon = self._daemon()
        with mock.patch.object(worktrees, "ensure", return_value={
                "path": tree, "error": None, "retry": False}):
            daemon.tick()

        self.assertEqual(self.started, [("t-0001", tree)])

    def test_a_repo_lane_task_is_untouched_by_any_of_this(self):
        with state.mutate_queue() as queue:
            state.new_task(queue, "qpay", "shared")
        with mock.patch.object(worktrees, "ensure",
                               side_effect=AssertionError("not for repo tasks")):
            self._daemon().tick()
        self.assertEqual(self.started,
                         [("t-0001", os.path.join(self.projects, "qpay"))])


if __name__ == "__main__":
    unittest.main(verbosity=1)
