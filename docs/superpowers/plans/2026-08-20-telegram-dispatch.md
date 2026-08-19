# Telegram-only Dispatch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the `dispatch` daemon the sole Telegram interface for queueing and supervising both `claude` and `codex` headless work, each agent governed by its own plan limits.

**Architecture:** The existing single-agent daemon gains a second agent backend behind a three-function interface, a second governor that reads codex's own rate limits for free from its session logs, and a second concurrency lane. One queue, one chat socket, one process; repo locks stay shared so the two lanes never edit one checkout at once. Repos are discovered from `~/Projects` rather than configured by hand.

**Tech Stack:** Python 3, standard library only. `unittest`. Existing suites `tests/dispatch_test.py` (offline, injected clock/usage) and `tests/dispatch_integration.py` (stub agent on PATH, real git fixture), both driven by `tests/run.sh`.

**Spec:** `docs/superpowers/specs/2026-08-20-telegram-dispatch-design.md`

## Global Constraints

- **Standard library only.** No third-party dependency may be added to `skills/dispatch/dispatch/`.
- **No test spends a real request.** No test may invoke `claude`, `codex`, or the Telegram API. Agents are stubbed; usage readings are injected; the clock is injected.
- **`DISPATCH_HOME` must keep working.** Every path goes through `config.path()`; tests point the whole system at a temp directory via that variable.
- **Existing state must load.** A `queue.json` or `state.json` written by the current version must be readable after every task. `mode` as a bare string expands to both lanes; `task.agent` absent means `"claude"`.
- **The bot token is never copied into dispatch state.** It is read at runtime from `~/.claude/channels/telegram/.env` via `config.read_token()`.
- **Workers never push and never commit to the default branch.** Every checkpoint lands on `tg/<id>`.
- **`bash tests/run.sh` must exit 0 at the end of every task.** It is currently green; keep it green.
- **Chat replies are plain text.** No Markdown parse mode is set on `sendMessage`, so no reply may depend on formatting.

---

## File Structure

**New files:**

| Path | Responsibility |
|---|---|
| `skills/dispatch/dispatch/lanes.py` | Lane constants; migration of `mode` and `armed_resume_at` between the old scalar form and the per-lane dict form. |
| `skills/dispatch/dispatch/governor/__init__.py` | Re-exports the Claude governor's public names so every existing `governor.foo(...)` call site keeps working; exposes `codex` submodule. |
| `skills/dispatch/dispatch/governor/claude.py` | Today's `governor.py`, moved unchanged. |
| `skills/dispatch/dispatch/governor/codex.py` | Codex plan limits read from `~/.codex/sessions/**/*.jsonl`. No polling, no interpolation. |
| `skills/dispatch/dispatch/backends/__init__.py` | Backend registry: `get(agent)` returns the module for `"claude"` or `"codex"`. |
| `skills/dispatch/dispatch/backends/claude.py` | `build_command` / `parse_result` / `resume_args` / `house_rules` for Claude. |
| `skills/dispatch/dispatch/backends/codex.py` | Same three functions for codex, plus its status schema path. |
| `skills/dispatch/dispatch/backends/status.schema.json` | JSON Schema forcing codex's final message to `{status, summary, next}`. |
| `skills/dispatch/dispatch/repos.py` | Discovery of `~/Projects`; git vs non-git classification; chat rendering. |
| `skills/dispatch/dispatch/sessions.py` | Session dashboard, ported from `~/.claude/skills/manage/manage.py`. Scanning and rendering only. |
| `skills/dispatch/dispatch/volume.py` | Token-volume parsers, ported from `~/.claude/scripts/usage_tg.py`, with the clock injected. |

**Modified files:**

| Path | Change |
|---|---|
| `skills/dispatch/dispatch/state.py` | `STATE_EMPTY` gains per-lane `mode`/`armed_resume_at`; `read_state` normalizes; `new_task` gains `agent`. |
| `skills/dispatch/dispatch/scheduler.py` | `runnable` filters by agent; `admit` unchanged. |
| `skills/dispatch/dispatch/worker.py` | Delegates argv and result parsing to a backend; writes the prompt file; feeds it on stdin. |
| `skills/dispatch/dispatch/parser.py` | `claude`/`codex` run verbs, `need_repo` rejection, `sessions`, `repos`, `usage poll`, `pause <lane>`. |
| `skills/dispatch/dispatch/daemon.py` | Per-lane tick, mode, wind-down, dispatch; new chat commands; composed `usage`. |
| `skills/dispatch/dispatch/cli.py` | `up` / `down` / `up --if-dead`; `setup` disables the conflicting plugin instead of refusing. |
| `skills/dispatch/dispatch/__init__.py` | `__all__` gains the new modules. |
| `tests/dispatch_test.py` | New test classes per task. |
| `tests/dispatch_integration.py` | Two-lane and prompt-file integration cases. |
| `tests/run.sh` | Stop requiring `skills/dispatch/SKILL.md`. |

**Deleted (Task 11 only, after everything else is green):** `skills/dispatch/SKILL.md`, `skills/dispatch/references/`, `~/.claude/scripts/telegram_health.sh`, `~/.claude/scripts/usage_tg.py`, `~/.claude/skills/manage/`, the health memory file and its `MEMORY.md` line, the health cron entry.

---

### Task 1: Codex governor

Split `governor.py` into a package so the Claude governor keeps its exact public surface, then add the codex governor beside it. Codex writes its own plan limits into its session logs, so this governor spends nothing.

**Files:**
- Create: `skills/dispatch/dispatch/governor/__init__.py`
- Create: `skills/dispatch/dispatch/governor/claude.py` (git mv of `governor.py`)
- Create: `skills/dispatch/dispatch/governor/codex.py`
- Delete: `skills/dispatch/dispatch/governor.py`
- Test: `tests/dispatch_test.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `governor.codex.reading(now, root=None, scan_limit=40) -> dict` with keys `ok, at, session_pct, session_reset, week_pct, week_reset`.
  - `governor.codex.estimate(now, root=None) -> dict` with keys `session_pct, week_pct, source, stale, resets_at` — the same shape `governor.estimate` returns, so `scheduler.admit` consumes either without knowing which.
  - `governor.codex.summary(now, root=None) -> str`.
  - Every existing name (`blank`, `max_concurrency`, `should_poll`, `record_poll`, `note_limit_error`, `estimate`, `est_cost_pct`, `learn_cost`, `summary`, `SEED_PCT_PER_TOKEN`, `RATIO_SMOOTHING`, `LADDER`, `EMPTY`) remains importable as `governor.<name>`.

- [ ] **Step 1: Move the Claude governor into a package**

```bash
cd skills/dispatch/dispatch
mkdir governor.pkg
git mv governor.py governor.pkg/claude.py
git mv governor.pkg governor
```

- [ ] **Step 2: Write `governor/__init__.py`**

```python
"""Two governors, one namespace.

``governor.<name>`` is the Claude governor, unchanged -- every existing call
site predates the split and must keep working. ``governor.codex`` is the second
one, which is cheaper in kind: codex writes its own plan limits to disk, so
there is nothing to poll and nothing to interpolate.
"""
from . import claude, codex
from .claude import (  # noqa: F401
    EMPTY,
    LADDER,
    RATIO_SMOOTHING,
    SEED_PCT_PER_TOKEN,
    blank,
    est_cost_pct,
    estimate,
    learn_cost,
    max_concurrency,
    note_limit_error,
    poll_interval,
    record_poll,
    should_poll,
    summary,
)

__all__ = [
    "claude", "codex", "EMPTY", "LADDER", "RATIO_SMOOTHING",
    "SEED_PCT_PER_TOKEN", "blank", "est_cost_pct", "estimate", "learn_cost",
    "max_concurrency", "note_limit_error", "poll_interval", "record_poll",
    "should_poll", "summary",
]
```

- [ ] **Step 3: Run the existing suite to prove the move changed nothing**

Run: `bash tests/run.sh`
Expected: all pass, including `ok: dispatch unit tests`. If an import fails, a name is missing from the re-export list above.

- [ ] **Step 4: Write the failing tests for the codex governor**

Append to `tests/dispatch_test.py`:

```python
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
```

Add `import json` to the test file's imports if it is not already there.

- [ ] **Step 5: Run the tests to verify they fail**

Run: `python3 -m unittest tests.dispatch_test.TestCodexGovernor -v`
Expected: FAIL — `AttributeError: module 'dispatch.governor' has no attribute 'codex'` is fine at this point only if Step 2 was skipped; otherwise every test fails with `AttributeError: ... has no attribute 'reading'`.

- [ ] **Step 6: Write `governor/codex.py`**

```python
"""Codex plan limits, read for free from codex's own session logs.

The Claude governor exists because ``/usage`` is the only source of truth and
asking costs a request. Codex has no such problem: every ``token_count`` event
it writes carries the server's own rate-limit block, so the reading is already
on disk by the time we want it.

    "rate_limits": {
      "primary":   {"used_percent": 99.0, "window_minutes": 300,   "resets_at": ...},
      "secondary": {"used_percent": 95.0, "window_minutes": 10080, "resets_at": ...}
    }

Three things the data forces. The same window turns up under either slot
depending on ``limit_id``, so entries are keyed by ``window_minutes`` and the
slot name is ignored. Both slots are frequently ``null``, which means "no
information", not "zero". And a record whose ``resets_at`` has passed describes
a window that has since rolled over, so it is discarded rather than believed.

Freshness differs in kind from the Claude governor's. A codex percentage is
exactly as old as your last codex run, and waiting cannot improve it -- the only
way to get a newer one is to run codex. So absence is deliberately *not* a
blocking condition: the lane starts optimistic and the first task's own output
corrects it. Blocking on absence would deadlock the lane permanently.
"""
import glob
import json
import os

SESSION_WINDOW_MINUTES = 300
WEEK_WINDOW_MINUTES = 10080

# Newest N session files to look at. Limits are written on every turn, so the
# newest handful always carries the answer; scanning the whole tree would be
# pure waste on an account with years of sessions.
SCAN_LIMIT = 40

# Bytes read from the end of each file. Limits appear on every turn, so the
# tail always holds the newest one, and a long session file can be very large.
TAIL_BYTES = 262_144


def default_root():
    return os.path.join(os.path.expanduser("~"), ".codex", "sessions")


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _tail_lines(path):
    """Complete lines from the tail of a file, cheaply."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - TAIL_BYTES))
            data = handle.read()
    except OSError:
        return []
    lines = data.decode("utf-8", "ignore").splitlines()
    # The first line is a fragment whenever we seeked into the middle.
    return lines[1:] if size > TAIL_BYTES and lines else lines


def _entries(node):
    """Every populated limit dict reachable from ``node``, at any depth."""
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            block = current.get("rate_limits")
            if isinstance(block, dict):
                for slot in ("primary", "secondary"):
                    entry = block.get(slot)
                    if (isinstance(entry, dict)
                            and entry.get("used_percent") is not None
                            and entry.get("window_minutes") is not None
                            and entry.get("resets_at") is not None):
                        yield entry
            stack.extend(v for v in current.values() if isinstance(v, (dict, list)))
        elif isinstance(current, list):
            stack.extend(v for v in current if isinstance(v, (dict, list)))


def _file_windows(path, now):
    """Newest unexpired entry per window within one file."""
    found = {}
    for line in _tail_lines(path):
        if '"rate_limits"' not in line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        for entry in _entries(record):
            if float(entry["resets_at"]) <= now:
                continue  # that window has already rolled over
            found[int(entry["window_minutes"])] = entry
    return found


def reading(now, root=None, scan_limit=SCAN_LIMIT):
    """Newest unexpired limit per window. ``ok`` is False when none was found."""
    root = root or default_root()
    paths = sorted(glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True),
                   key=_mtime, reverse=True)[:scan_limit]
    best = {}
    for path in paths:
        for window, entry in _file_windows(path, now).items():
            best.setdefault(window, entry)  # newest file scanned first
        if SESSION_WINDOW_MINUTES in best and WEEK_WINDOW_MINUTES in best:
            break

    session = best.get(SESSION_WINDOW_MINUTES)
    week = best.get(WEEK_WINDOW_MINUTES)
    return {
        "ok": bool(best),
        "at": now,
        "session_pct": None if session is None else float(session["used_percent"]),
        "session_reset": None if session is None else float(session["resets_at"]),
        "week_pct": None if week is None else float(week["used_percent"]),
        "week_reset": None if week is None else float(week["resets_at"]),
    }


def estimate(now, root=None, scan_limit=SCAN_LIMIT):
    """Admission-shaped view, matching ``governor.estimate``'s keys.

    ``stale`` is always False. See the module docstring: absence of a reading
    is not a reason to block, because running codex is the only thing that
    produces one.
    """
    current = reading(now, root=root, scan_limit=scan_limit)
    if not current["ok"]:
        return {"session_pct": 0.0, "week_pct": None, "source": "codex-unknown",
                "stale": False, "resets_at": None}
    return {
        "session_pct": current["session_pct"] if current["session_pct"] is not None else 0.0,
        "week_pct": current["week_pct"],
        "source": "codex-logs",
        "stale": False,
        "resets_at": current["session_reset"] or current["week_reset"],
    }


def summary(now, root=None):
    """One line for `status` and `usage`."""
    current = reading(now, root=root)
    if not current["ok"]:
        return "codex: no reading (run codex once)"
    parts = []
    if current["session_pct"] is not None:
        parts.append("5h %.0f%%" % current["session_pct"])
    if current["week_pct"] is not None:
        parts.append("7d %.0f%%" % current["week_pct"])
    return "codex: " + " · ".join(parts)
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `python3 -m unittest tests.dispatch_test.TestCodexGovernor -v`
Expected: 9 tests PASS.

- [ ] **Step 8: Run the whole suite**

Run: `bash tests/run.sh`
Expected: exit 0, all `ok:` lines.

- [ ] **Step 9: Commit**

```bash
git add skills/dispatch/dispatch/governor tests/dispatch_test.py
git commit -m "feat(dispatch): codex governor reading limits from session logs"
```

---

### Task 2: Lane primitives and per-lane state

`mode` becomes a dict keyed by lane. Everything that reads it must keep working against a state file written by the current version, so migration happens on read.

**Files:**
- Create: `skills/dispatch/dispatch/lanes.py`
- Modify: `skills/dispatch/dispatch/state.py` (`STATE_EMPTY`, `read_state`, `new_task`)
- Modify: `skills/dispatch/dispatch/__init__.py` (`__all__`)
- Test: `tests/dispatch_test.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `lanes.CLAUDE = "claude"`, `lanes.CODEX = "codex"`, `lanes.ALL = ("claude", "codex")`
  - `lanes.normalize_mode(value) -> dict[str, str]`
  - `lanes.normalize_armed(value) -> dict[str, float|None]`
  - `lanes.of(task) -> str` — a task's lane, defaulting to `"claude"`
  - `state.new_task(queue, repo, prompt, priority=5, deps=None, isolation="repo", branch=None, agent="claude")` — new trailing keyword; the record gains an `"agent"` key.
  - `state.read_state()` returns `mode` and `armed_resume_at` as dicts, always.

- [ ] **Step 1: Write the failing tests**

Append to `tests/dispatch_test.py`:

```python
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
```

Add `lanes` to the module import line at the top of `tests/dispatch_test.py`:

```python
from dispatch import governor, lanes, parser, scheduler, state, usage, winddown, worker  # noqa: E402
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.dispatch_test.TestLanes tests.dispatch_test.TestStateMigration -v`
Expected: FAIL with `ImportError: cannot import name 'lanes'`.

- [ ] **Step 3: Write `lanes.py`**

```python
"""Lanes: one per agent, each with its own governor and its own wind-down.

The two lanes are independent in everything except repo locks. A task belongs
to exactly one, decided at enqueue by which verb the user typed.

``mode`` and ``armed_resume_at`` used to be scalars. They are dicts now, and the
readers below accept both forms -- a state file written by the previous version
has to keep loading, and the migration is cheap enough to do on every read
rather than as a one-shot upgrade step that could be skipped.
"""

CLAUDE = "claude"
CODEX = "codex"
ALL = (CLAUDE, CODEX)

RUNNING = "running"


def of(task):
    """The lane a task belongs to. Anything unrecognised is Claude's."""
    agent = (task or {}).get("agent")
    return agent if agent in ALL else CLAUDE


def normalize_mode(value):
    """Per-lane modes, from either the dict form or the old scalar form."""
    if isinstance(value, dict):
        return {lane: value.get(lane) or RUNNING for lane in ALL}
    scalar = value or RUNNING
    return {lane: scalar for lane in ALL}


def normalize_armed(value):
    """Per-lane armed-resume timestamps, from either form."""
    if isinstance(value, dict):
        return {lane: value.get(lane) for lane in ALL}
    return {lane: value for lane in ALL}
```

- [ ] **Step 4: Update `state.py`**

Replace `STATE_EMPTY` and add normalization to `read_state`. In `skills/dispatch/dispatch/state.py`:

```python
from . import config
from . import lanes

QUEUE_EMPTY = {"next_id": 1, "tasks": []}
STATE_EMPTY = {
    "mode": {"claude": "running", "codex": "running"},
    "chat_offset": 0,
    "governor": {},
    "armed_resume_at": {"claude": None, "codex": None},
    "repo_cost_pct": {},
}
```

```python
def read_state():
    doc = read(config.state_path(), STATE_EMPTY)
    doc["mode"] = lanes.normalize_mode(doc.get("mode"))
    doc["armed_resume_at"] = lanes.normalize_armed(doc.get("armed_resume_at"))
    return doc
```

- [ ] **Step 5: Give tasks an agent**

In `state.new_task`, add the parameter and the record key:

```python
def new_task(queue, repo, prompt, priority=5, deps=None, isolation="repo",
             branch=None, agent="claude"):
    """Append a task to ``queue`` and return the record."""
    task_id = "t-%04d" % queue["next_id"]
    queue["next_id"] += 1
    task = {
        "id": task_id,
        "repo": repo,
        "prompt": prompt,
        "agent": agent if agent in lanes.ALL else lanes.CLAUDE,
        "state": "queued",
        "priority": priority,
        "deps": list(deps or []),
        "isolation": isolation,
        "branch": branch or ("tg/%s" % task_id),
        "session_id": None,
        "steps_done": 0,
        "est_cost_pct": None,
        "last_error": None,
    }
    queue["tasks"].append(task)
    return task
```

- [ ] **Step 6: Add `lanes` to `__init__.py`**

In `skills/dispatch/dispatch/__init__.py`, insert `"lanes",` into `__all__` after `"state",`.

- [ ] **Step 7: Run the new tests**

Run: `python3 -m unittest tests.dispatch_test.TestLanes tests.dispatch_test.TestStateMigration -v`
Expected: 12 tests PASS.

- [ ] **Step 8: Run the whole suite and fix the fallout**

Run: `bash tests/run.sh`
Expected: failures in `daemon.py` and `cli.py`, which compare `doc["mode"]` to a string. This task's job is only to stop them crashing; the real per-lane logic lands in Task 9. Apply the minimal bridge — in `daemon.py` and `cli.py`, wherever `state_doc.get("mode", ...)` is read as a scalar, read the Claude lane instead:

```python
mode = state_doc["mode"][lanes.CLAUDE]
```

and wherever a scalar is written (`doc["mode"] = "paused"`), write both lanes:

```python
doc["mode"] = {lane: "paused" for lane in lanes.ALL}
```

Add `from . import lanes` to both modules. Re-run until green.

- [ ] **Step 9: Commit**

```bash
git add skills/dispatch/dispatch tests/dispatch_test.py
git commit -m "feat(dispatch): per-lane mode with backward-compatible migration"
```

---

### Task 3: Agent backends and the prompt file

`worker.py` hardcodes Claude's argv and Claude's fenced-block contract. Both move behind a backend interface, and the prompt stops travelling in argv.

**Files:**
- Create: `skills/dispatch/dispatch/backends/__init__.py`
- Create: `skills/dispatch/dispatch/backends/claude.py`
- Create: `skills/dispatch/dispatch/backends/codex.py`
- Create: `skills/dispatch/dispatch/backends/status.schema.json`
- Modify: `skills/dispatch/dispatch/worker.py`
- Test: `tests/dispatch_test.py`

**Interfaces:**
- Consumes: `lanes.of(task)` from Task 2.
- Produces, on both backend modules:
  - `build_command(task, prompt_path, cwd, task_dir, unsafe=True) -> list[str]`
  - `resume_args(session_id) -> list[str]`
  - `parse_result(output, task_dir) -> {"status", "summary", "next", "session_id"}`
  - `house_rules(task) -> str`
  - `backends.get(agent) -> module`
  - `worker.write_prompt(task, task_dir) -> str` (path)
  - `worker.run_step(task, cwd, config, env=None, popen=None, sleeper=None, task_dir=None)` — `task_dir` is new and defaults to `config_mod.task_dir(task["id"])`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/dispatch_test.py`:

```python
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
```

Add `backends` to the test imports:

```python
from dispatch import backends, governor, lanes, parser, scheduler, state, usage, winddown, worker  # noqa: E402
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.dispatch_test.TestBackends tests.dispatch_test.TestPromptFile -v`
Expected: FAIL with `ImportError: cannot import name 'backends'`.

- [ ] **Step 3: Write `backends/status.schema.json`**

```json
{
  "type": "object",
  "properties": {
    "status": {
      "type": "string",
      "enum": ["complete", "continue", "blocked"],
      "description": "complete when the deliverable is done and verified, continue when another step is needed, blocked when only the user can decide"
    },
    "summary": {
      "type": "string",
      "description": "what this step actually changed"
    },
    "next": {
      "type": "string",
      "description": "what the following step should pick up, empty when complete"
    }
  },
  "required": ["status", "summary", "next"],
  "additionalProperties": false
}
```

- [ ] **Step 4: Write `backends/claude.py`**

```python
"""Claude backend: argv, house rules, and the fenced status block.

Everything here was previously inline in ``worker.py`` and is unchanged in
behaviour, with one exception: the prompt arrives on stdin from a file rather
than embedded in argv.
"""
import json
import re

HOUSE_RULES = """
Operating rules for this run:
- Do one coherent chunk of work, then stop. Do not try to finish everything.
- Commit checkpoints to the branch {branch}. Never commit to main.
- Never push. Never force-push. Never rewrite published history.
- If you need a decision only the user can make, stop and report blocked.
- End your final message with a fenced json block, exactly:

```json
{{"status": "complete|continue|blocked", "summary": "...", "next": "..."}}
```
"""

FENCE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)
VALID = ("complete", "continue", "blocked")


def house_rules(task):
    return HOUSE_RULES.format(branch=task["branch"]).strip()


def resume_args(session_id):
    """Claude continues with an option pair."""
    return ["--resume", session_id] if session_id else []


def build_command(task, prompt_path, cwd, task_dir, unsafe=True):
    argv = ["claude", "-p", "-", "--output-format", "json"]
    if unsafe:
        argv.append("--dangerously-skip-permissions")
    argv.extend(resume_args(task.get("session_id")))
    return argv


def parse_result(output, task_dir):
    """Extract the agent's self-reported status block.

    Accepts either raw text or the ``--output-format json`` envelope, whose
    ``result`` field holds the text the fence lives in.
    """
    text = output or ""
    session_id = None
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            envelope = json.loads(stripped)
        except ValueError:
            envelope = None
        if isinstance(envelope, dict):
            session_id = envelope.get("session_id")
            if isinstance(envelope.get("result"), str):
                text = envelope["result"]

    for raw in reversed(FENCE.findall(text)):
        try:
            block = json.loads(raw)
        except ValueError:
            continue
        if isinstance(block, dict) and block.get("status") in VALID:
            return {"status": block["status"],
                    "summary": block.get("summary") or "",
                    "next": block.get("next") or "",
                    "session_id": session_id}
    return {"status": None, "summary": "", "next": "", "session_id": session_id}
```

- [ ] **Step 5: Write `backends/codex.py`**

```python
"""Codex backend.

Codex gives a stronger status contract than Claude for free. ``--output-schema``
makes the model's final message conform to a schema, and ``-o`` writes that
message to a file, so the status is read as JSON rather than regexed out of an
event stream. The house rules therefore drop the fenced-block instruction; the
rest is identical, because the wind-down path depends on it.

Continuation is a subcommand (``codex exec resume <id>``), not a trailing flag.
That is the one shape difference from Claude that ``build_command`` has to
respect, and it is why ``resume_args`` returns a positional fragment here.

Known gap: the session id is recovered by looking for ``thread_id`` /
``session_id`` / ``conversation_id`` anywhere in the ``--json`` event stream. If
none is present the step still settles correctly -- the id is simply ``None``,
and the next step starts a fresh codex session seeded from ``handoff.md``, which
is the same fallback a lost Claude session takes.
"""
import json
import os

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "status.schema.json")

HOUSE_RULES = """
Operating rules for this run:
- Do one coherent chunk of work, then stop. Do not try to finish everything.
- Commit checkpoints to the branch {branch}. Never commit to main.
- Never push. Never force-push. Never rewrite published history.
- If you need a decision only the user can make, stop and report blocked.
- Your final message must be the JSON object required by the output schema:
  status is "complete" when the deliverable is done and verified, "continue"
  when another step is needed, "blocked" when only the user can decide.
"""

VALID = ("complete", "continue", "blocked")
SESSION_KEYS = ("thread_id", "session_id", "conversation_id")


def house_rules(task):
    return HOUSE_RULES.format(branch=task["branch"]).strip()


def resume_args(session_id):
    """Codex continues with a subcommand, so this is positional."""
    return ["resume", session_id] if session_id else []


def build_command(task, prompt_path, cwd, task_dir, unsafe=True):
    argv = ["codex", "exec"]
    argv.extend(resume_args(task.get("session_id")))
    argv.extend([
        "-",                       # prompt on stdin
        "--json",
        "-C", cwd,
        "--skip-git-repo-check",
        "--output-schema", SCHEMA_PATH,
        "-o", os.path.join(task_dir, "last.json"),
    ])
    if unsafe:
        argv.append("--dangerously-bypass-approvals-and-sandbox")
    return argv


def _session_id(output):
    for line in (output or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        for key in SESSION_KEYS:
            value = event.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def parse_result(output, task_dir):
    session_id = _session_id(output)
    try:
        with open(os.path.join(task_dir, "last.json")) as fh:
            block = json.load(fh)
    except (OSError, ValueError):
        return {"status": None, "summary": "", "next": "", "session_id": session_id}
    if not isinstance(block, dict) or block.get("status") not in VALID:
        return {"status": None, "summary": "", "next": "", "session_id": session_id}
    return {"status": block["status"],
            "summary": block.get("summary") or "",
            "next": block.get("next") or "",
            "session_id": session_id}
```

- [ ] **Step 6: Write `backends/__init__.py`**

```python
"""Agent backends. Two of them, resolved by a task's ``agent`` field."""
from . import claude, codex

REGISTRY = {"claude": claude, "codex": codex}


def get(agent):
    """Backend module for ``agent``. Anything unrecognised is Claude's."""
    return REGISTRY.get(agent, claude)


__all__ = ["claude", "codex", "get", "REGISTRY"]
```

- [ ] **Step 7: Rewrite `worker.py` to delegate**

Replace the top of `skills/dispatch/dispatch/worker.py` — the `HOUSE_RULES`, `FENCE`, `VALID`, `house_rules`, `build_prompt`, `build_command`, and `parse_status` definitions — with:

```python
import os
import signal
import subprocess

from . import backends, config as config_mod, lanes, usage


def build_prompt(task):
    """The full text the agent receives: the request, then the house rules."""
    backend = backends.get(lanes.of(task))
    return "%s\n\n%s" % (task["prompt"].strip(), backend.house_rules(task))


def write_prompt(task, task_dir):
    """Persist the prompt beside the task's other artifacts, return its path.

    The prompt is a file rather than an argv element for three reasons, in
    order of how likely each is to bite: chat text is arbitrary and quoting it
    is a correctness problem; a long prompt can exceed ARG_MAX; and the exact
    bytes the agent saw belong next to steps.jsonl when a step has to be
    diagnosed later.
    """
    os.makedirs(task_dir, exist_ok=True)
    path = os.path.join(task_dir, "prompt.txt")
    with open(path, "w") as handle:
        handle.write(build_prompt(task))
    return path


def build_command(task, prompt_path, cwd, task_dir, unsafe=True):
    return backends.get(lanes.of(task)).build_command(
        task, prompt_path, cwd, task_dir, unsafe=unsafe)


def parse_status(output, task_dir, task=None):
    return backends.get(lanes.of(task or {})).parse_result(output, task_dir)
```

Then rewrite `run_step` to write the prompt, open it as stdin, and parse through the backend:

```python
def run_step(task, cwd, config, env=None, popen=None, sleeper=None, task_dir=None):
    """Execute one step under a wall-clock cap. Returns a result dict.

    ``popen`` and ``sleeper`` are injected in tests. The termination path is
    SIGTERM, a grace period, then SIGKILL: a checkpointing agent deserves the
    chance to finish its commit, but not indefinitely.
    """
    popen = popen or subprocess.Popen
    task_dir = task_dir or config_mod.task_dir(task["id"])
    prompt_path = write_prompt(task, task_dir)
    argv = build_command(task, prompt_path, cwd, task_dir)

    with open(prompt_path) as prompt_handle:
        process = popen(argv, cwd=cwd, env=env or os.environ.copy(),
                        stdin=prompt_handle,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        timed_out = False
        try:
            output, _ = process.communicate(timeout=config["step_timeout"])
        except subprocess.TimeoutExpired:
            timed_out = True
            output = _terminate(process, config, sleeper)

    result = parse_status(output, task_dir, task)
    result["timed_out"] = timed_out
    result["returncode"] = process.returncode
    result["output"] = output or ""
    result["limit_reset_at"] = usage.parse_limit_error(output)
    if result["limit_reset_at"]:
        result["status"] = None
    return result
```

Leave `next_state`, `_terminate`, `git`, `checkpoint`, and `write_handoff` untouched.

- [ ] **Step 8: Update the daemon's call into the worker**

In `daemon.py`, `_default_run_step` must pass the task directory so the codex backend and the reaper agree on where `last.json` lives:

```python
    def _default_run_step(self, task, cwd):
        from . import worker
        return worker.run_step(task, cwd, self.config,
                               task_dir=config_mod.task_dir(task["id"]))
```

- [ ] **Step 9: Run the new tests**

Run: `python3 -m unittest tests.dispatch_test.TestBackends tests.dispatch_test.TestPromptFile -v`
Expected: 20 tests PASS.

- [ ] **Step 10: Fix the existing worker tests**

Run: `bash tests/run.sh`
Expected: failures in whichever existing tests call `worker.build_command(task)` or `worker.parse_status(output)` with the old signatures. Update those call sites to the new ones — `worker.build_command(task, prompt_path, cwd, task_dir)` and `worker.parse_status(output, task_dir, task)`. Re-run until green.

- [ ] **Step 11: Commit**

```bash
git add skills/dispatch/dispatch tests/dispatch_test.py
git commit -m "feat(dispatch): agent backends with the prompt on stdin from a file"
```

---

### Task 4: Repo discovery

The alias map goes away. Repos are whatever is under `~/Projects`, and only the git ones can be dispatched to — a worker checkpoints to `tg/<id>`, and a non-git folder has nowhere to put that, so a wind-down there would discard work instead of parking it.

**Files:**
- Create: `skills/dispatch/dispatch/repos.py`
- Modify: `skills/dispatch/dispatch/config.py` (add `projects_root`)
- Modify: `skills/dispatch/dispatch/__init__.py` (`__all__`)
- Test: `tests/dispatch_test.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `repos.root_path(config=None) -> str` — the discovery root, expanded
  - `repos.discover(root=None, overrides=None) -> dict[str, {"path": str, "git": bool}]`
  - `repos.resolve(alias, root=None, overrides=None, found=None) -> str|None` — path, or None when unknown **or** not dispatchable; pass `found` to reuse one `discover()` call
  - `repos.dispatchable(found) -> dict` — the git subset
  - `repos.render(found) -> str` — the `repos` chat reply
  - `repos.reject_reason(alias, found) -> str` — why an alias cannot be dispatched to
  - `config.DEFAULTS["projects_root"] = "~/Projects"`

- [ ] **Step 1: Write the failing tests**

Append to `tests/dispatch_test.py`:

```python
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
```

Add `repos` to the test import line.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.dispatch_test.TestRepoDiscovery -v`
Expected: FAIL with `ImportError: cannot import name 'repos'`.

- [ ] **Step 3: Add the root to config defaults**

In `skills/dispatch/dispatch/config.py`, inside `DEFAULTS`, replace the `"repos": {}` line with:

```python
    # Repos are discovered under this root; `repos` holds overrides only --
    # aliases pointing somewhere else entirely.
    "projects_root": "~/Projects",
    "repos": {},
```

- [ ] **Step 4: Write `repos.py`**

```python
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
```

- [ ] **Step 5: Add `repos` to `__init__.py`**

Insert `"repos",` into `__all__`.

- [ ] **Step 6: Run the tests**

Run: `python3 -m unittest tests.dispatch_test.TestRepoDiscovery -v`
Expected: 10 tests PASS.

- [ ] **Step 7: Run the whole suite**

Run: `bash tests/run.sh`
Expected: exit 0. `daemon.repo_path` still reads the old config map; it is rewired in Task 9.

- [ ] **Step 8: Commit**

```bash
git add skills/dispatch/dispatch tests/dispatch_test.py
git commit -m "feat(dispatch): discover repos from ~/Projects, git ones dispatchable"
```

---

### Task 5: Per-lane admission

Admission barely changes. `admit` already takes everything it needs through `ctx`; what changes is that the daemon builds one `ctx` per lane, and `runnable` learns to filter.

**Files:**
- Modify: `skills/dispatch/dispatch/scheduler.py`
- Test: `tests/dispatch_test.py`

**Interfaces:**
- Consumes: `lanes.of(task)` from Task 2.
- Produces: `scheduler.runnable(queue, agent=None) -> list[task]` — unfiltered when `agent` is None, so every existing call site keeps its behaviour.

- [ ] **Step 1: Write the failing tests**

Append to `tests/dispatch_test.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.dispatch_test.TestLaneAdmission -v`
Expected: FAIL — `runnable() takes 1 positional argument but 2 were given`.

- [ ] **Step 3: Implement the filter**

In `skills/dispatch/dispatch/scheduler.py`, add the import and replace `runnable`:

```python
from . import governor, lanes
```

```python
def runnable(queue, agent=None):
    """Tasks eligible to start, optionally narrowed to one lane.

    ``agent=None`` returns every lane, which is what the CLI and the status
    surfaces want; the daemon passes a lane, because admission is per lane.
    """
    tasks = (t for t in queue["tasks"] if t["state"] in ("queued", "paused"))
    if agent is not None:
        tasks = (t for t in tasks if lanes.of(t) == agent)
    return sorted(tasks, key=sort_key)
```

- [ ] **Step 4: Run the tests**

Run: `python3 -m unittest tests.dispatch_test.TestLaneAdmission -v`
Expected: 7 tests PASS.

- [ ] **Step 5: Run the whole suite**

Run: `bash tests/run.sh`
Expected: exit 0.

- [ ] **Step 6: Commit**

```bash
git add skills/dispatch/dispatch/scheduler.py tests/dispatch_test.py
git commit -m "feat(dispatch): lane-aware runnable set"
```

---

### Task 6: Chat verbs

Two run verbs, two read verbs, a lane-scoped pause, and an explicit rejection when a run verb names no repo. Everything here must parse with zero model calls — that is the property that lets the daemon keep answering while a window is exhausted.

**Files:**
- Modify: `skills/dispatch/dispatch/parser.py`
- Test: `tests/dispatch_test.py`

**Interfaces:**
- Consumes: nothing.
- Produces, from `parser.parse(text)`:
  - `{"kind": "run", "agent": "claude"|"codex", "prompt": str, "repo": str, "isolation": str}`
  - `{"kind": "need_repo", "agent": str, "prompt": str}`
  - `{"kind": "usage", "poll": bool}`
  - `{"kind": "sessions", "project": str|None}`
  - `{"kind": "repos"}`
  - `{"kind": "pause"|"resume", "lane": "claude"|"codex"|None}`
  - every previously produced shape, unchanged except that `run` now always carries `agent`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/dispatch_test.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.dispatch_test.TestAgentVerbs tests.dispatch_test.TestReadVerbs -v`
Expected: FAIL — `KeyError: 'agent'` and `KeyError: 'poll'`.

- [ ] **Step 3: Add the patterns**

In `skills/dispatch/dispatch/parser.py`, after the existing `RUN_COLON` definition, add:

```python
AGENTS = ("claude", "codex")
AGENT_RUN = re.compile(
    r"^(?P<agent>claude|codex)\s+(?P<prompt>.+?)\s+(?:on|in)\s+(?P<repo>[\w.\-/]+)\s*$",
    re.IGNORECASE | re.DOTALL)
AGENT_COLON = re.compile(
    r"^(?P<agent>claude|codex)\s+(?P<repo>[\w.\-/]+)\s*:\s*(?P<prompt>.+)$",
    re.IGNORECASE | re.DOTALL)
AGENT_BARE = re.compile(
    r"^(?P<agent>claude|codex)\s+(?P<prompt>.+)$", re.IGNORECASE | re.DOTALL)
USAGE_POLL = re.compile(r"^usage\s+(?:--)?poll\s*$", re.IGNORECASE)
SESSIONS = re.compile(r"^sessions(?:\s+(?P<project>[\w.\-/]+))?\s*$", re.IGNORECASE)
LANE_MODE = re.compile(r"^(?P<verb>pause|resume)\s+(?P<lane>claude|codex)\s*$",
                       re.IGNORECASE)
```

Extend `BARE` with the new zero-argument words:

```python
BARE = {
    "status": "status",
    "queue": "queue",
    "q": "queue",
    "usage": "usage",
    "pause": "pause",
    "resume": "resume",
    "help": "help",
    "sessions": "sessions",
    "repos": "repos",
    "projects": "repos",
}
```

- [ ] **Step 4: Rewrite `parse`**

Replace the body of `parse` in `parser.py` with:

```python
def parse(text):
    """Return a command dict. ``kind == 'unparsed'`` means: ask a model.

    Order matters. ``run`` is checked before the agent verbs so that
    ``run claude tests on qpay`` stays a run whose prompt happens to start with
    an agent's name, and the bare-agent rejection is checked last so it only
    catches text that no other rule claimed.
    """
    if text is None:
        return {"kind": "unparsed", "text": ""}
    stripped = text.strip()
    if not stripped:
        return {"kind": "unparsed", "text": ""}

    lowered = stripped.lower().lstrip("/")
    if lowered in BARE:
        kind = BARE[lowered]
        if kind == "usage":
            return {"kind": "usage", "poll": False}
        if kind == "sessions":
            return {"kind": "sessions", "project": None}
        if kind in ("pause", "resume"):
            return {"kind": kind, "lane": None}
        return {"kind": kind}

    body = stripped.lstrip("/")

    if USAGE_POLL.match(body):
        return {"kind": "usage", "poll": True}

    lane_mode = LANE_MODE.match(body)
    if lane_mode:
        return {"kind": lane_mode.group("verb").lower(),
                "lane": lane_mode.group("lane").lower()}

    sessions = SESSIONS.match(body)
    if sessions:
        return {"kind": "sessions", "project": sessions.group("project")}

    with_id = WITH_ID.match(body)
    if with_id:
        verb = with_id.group("verb").lower()
        verb = "logs" if verb in ("logs", "log", "show") else verb
        return {"kind": verb, "id": normalize_id(with_id.group("id"))}

    isolation = "repo"
    candidate = body
    if ISOLATION.search(candidate):
        isolation = "worktree"
        candidate = ISOLATION.sub("", candidate)

    for pattern in (RUN_COLON, RUN):
        match = pattern.match(candidate)
        if match:
            prompt = match.group("prompt").strip()
            repo = match.group("repo").strip().rstrip(".,")
            if prompt and repo:
                return {"kind": "run", "agent": "claude", "prompt": prompt,
                        "repo": repo, "isolation": isolation}

    for pattern in (AGENT_COLON, AGENT_RUN):
        match = pattern.match(candidate)
        if match:
            prompt = match.group("prompt").strip()
            repo = match.group("repo").strip().rstrip(".,")
            if prompt and repo:
                return {"kind": "run", "agent": match.group("agent").lower(),
                        "prompt": prompt, "repo": repo, "isolation": isolation}

    bare_agent = AGENT_BARE.match(candidate)
    if bare_agent:
        return {"kind": "need_repo", "agent": bare_agent.group("agent").lower(),
                "prompt": bare_agent.group("prompt").strip()}

    return {"kind": "unparsed", "text": stripped}
```

- [ ] **Step 5: Update the help text**

In `daemon.handle_command`, replace the `help` reply:

```python
        if kind == "help":
            return ("claude <task> on <repo> · codex <task> on <repo> · "
                    "status · queue · usage · usage poll · sessions · repos · "
                    "logs <id> · cancel <id> · retry <id> · "
                    "pause [lane] · resume [lane]")
```

- [ ] **Step 6: Run the tests**

Run: `python3 -m unittest tests.dispatch_test.TestAgentVerbs tests.dispatch_test.TestReadVerbs -v`
Expected: 16 tests PASS.

- [ ] **Step 7: Run the whole suite and fix the fallout**

Run: `bash tests/run.sh`
Expected: failures where `handle_command` reads `command["kind"] == "usage"` and existing tests assert `parse("usage") == {"kind": "usage"}`. Update those assertions to the new shape. `handle_command`'s `usage`, `pause`, and `resume` branches are fully rewired in Task 9; for now make them ignore the extra keys so the suite passes.

- [ ] **Step 8: Commit**

```bash
git add skills/dispatch/dispatch tests/dispatch_test.py
git commit -m "feat(dispatch): claude/codex chat verbs, sessions, repos, usage poll"
```

---

### Task 7: Token volume parsers

`~/.claude/scripts/usage_tg.py` becomes a module. The functional change is small — the module-level `NOW` constant becomes a parameter, which is also what makes it testable — and the standalone report must keep producing the same output.

**Files:**
- Create: `skills/dispatch/dispatch/volume.py` (ported from `~/.claude/scripts/usage_tg.py`)
- Modify: `skills/dispatch/dispatch/__init__.py` (`__all__`)
- Test: `tests/dispatch_test.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `volume.claude_usage(now, root=None) -> (win: Counter, proj: Counter, models: Counter, msgs: int)`
  - `volume.codex_usage(now, root=None) -> (tot: Counter, proj: Counter)`
  - `volume.active_sessions(now, claude_root=None, codex_root=None) -> list[(minutes, name)]`
  - `volume.human(n) -> str`
  - `volume.render(now, claude_root=None, codex_root=None) -> str` — the volume-only block, no plan-limit call
  - `volume.WINDOWS = [("5h", 18000), ("24h", 86400), ("7d", 604800)]`

- [ ] **Step 1: Copy the script into the package**

```bash
cp ~/.claude/scripts/usage_tg.py skills/dispatch/dispatch/volume.py
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/dispatch_test.py`:

```python
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

    def test_human_scales(self):
        self.assertEqual(volume.human(1_500_000), "1.5M")
        self.assertEqual(volume.human(2_000), "2.0K")
        self.assertEqual(volume.human(42), "42")

    def test_render_never_calls_the_paid_endpoint(self):
        """`usage` must be free; only `usage poll` may spend a request."""
        source = open(volume.__file__).read()
        self.assertNotIn("/usage", source.split("def render")[1])

    def test_render_on_an_empty_tree_does_not_raise(self):
        text = volume.render(self.now,
                             claude_root=os.path.join(self.tmp.name, "none"),
                             codex_root=os.path.join(self.tmp.name, "none"))
        self.assertIn("CLAUDE", text)
```

Add `volume` to the test imports and `import time` if absent.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `python3 -m unittest tests.dispatch_test.TestVolume -v`
Expected: FAIL — `claude_usage() takes 0 positional arguments but 1 was given`.

- [ ] **Step 4: Refactor `volume.py`**

Apply these changes to the copied file:

1. Delete the module-level `NOW = time.time()`, `CHAT_ID`, `ENV_PATH`, and the `send()` function — the daemon owns chat now.
2. Change `HOME`-rooted globs into parameters:
   - `claude_usage(now, root=None)` with `root = root or os.path.join(HOME, ".claude", "projects")`
   - `codex_usage(now, root=None)` with `root = root or os.path.join(HOME, ".codex", "sessions")`
   - `active_sessions(now, claude_root=None, codex_root=None)`
3. Replace every use of the old global `NOW` inside those functions with the `now` parameter.
4. Delete `plan_limits()` — the Claude governor already owns the paid `/usage` call, and duplicating it here is how `usage` would accidentally become expensive.
5. Rename `build()` to `render(now, claude_root=None, codex_root=None)`, drop its `plan_limits()` call and the two lines around it, and thread `now` through.
6. Replace the `__main__` block with:

```python
if __name__ == "__main__":
    import time as _time

    print(render(_time.time()))
```

- [ ] **Step 5: Add `volume` to `__init__.py`**

Insert `"volume",` into `__all__`.

- [ ] **Step 6: Run the tests**

Run: `python3 -m unittest tests.dispatch_test.TestVolume -v`
Expected: 7 tests PASS.

- [ ] **Step 7: Verify the standalone report still runs**

Run: `python3 skills/dispatch/dispatch/volume.py | head -20`
Expected: a `USAGE — <date>` header followed by `CLAUDE CODE` and `CODEX` blocks. No plan-limit section, and it must return in under a second — if it hangs, a `plan_limits()` call survived step 4.

- [ ] **Step 8: Run the whole suite**

Run: `bash tests/run.sh`
Expected: exit 0.

- [ ] **Step 9: Commit**

```bash
git add skills/dispatch/dispatch tests/dispatch_test.py
git commit -m "feat(dispatch): port usage_tg volume parsers with an injected clock"
```

---

### Task 8: Session dashboard

`manage.py` becomes a module. Only the scanning and rendering come across; the project listing is `repos.py`'s job now, and `launch` is deliberately dropped.

**Files:**
- Create: `skills/dispatch/dispatch/sessions.py` (ported from `~/.claude/skills/manage/manage.py`)
- Modify: `skills/dispatch/dispatch/__init__.py` (`__all__`)
- Test: `tests/dispatch_test.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `sessions.scan(path, now) -> dict|None` — one session summary, or None for an empty stub
  - `sessions.collect(now, root=None, project=None, limit=12) -> list[dict]` — newest first
  - `sessions.render(now, root=None, project=None, limit=12) -> str`

- [ ] **Step 1: Copy the script into the package**

```bash
cp ~/.claude/skills/manage/manage.py skills/dispatch/dispatch/sessions.py
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/dispatch_test.py`:

```python
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
```

Add `sessions` to the test imports.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `python3 -m unittest tests.dispatch_test.TestSessions -v`
Expected: FAIL — `module 'dispatch.sessions' has no attribute 'scan'`.

- [ ] **Step 4: Refactor `sessions.py`**

Apply these changes to the copied file:

1. Delete `list_projects`, `cmd_projects`, `_resolve_target`, `cmd_launch`, `main`, the `argparse` import, and the `__main__` block. `repos.py` owns the project list; launch is dropped.
2. Rename `scan_session(path)` to `scan(path, now)`, and pass `now` into the `_rel` and `_status` helpers instead of letting them call `time.time()`.
3. Change `_rel(ts_epoch)` to `_rel(ts_epoch, now)` and `_status(ts_epoch)` to `_status(ts_epoch, now)`, replacing their internal `time.time()` calls with the parameter.
4. Replace `cmd_list` with:

```python
def collect(now, root=None, project=None, limit=12):
    """Session summaries, newest first, optionally filtered by project."""
    root = root or SESS_ROOT
    rows = []
    for path in glob.glob(os.path.join(root, "*", "*.jsonl")):
        summary = scan(path, now)
        if summary is None:
            continue
        if project and project.lower() not in _project_label(summary).lower():
            continue
        rows.append(summary)
    rows.sort(key=lambda s: s["activity"] or 0, reverse=True)
    return rows[:limit]


def render(now, root=None, project=None, limit=12):
    """Plain-text dashboard. No markdown -- chat replies set no parse mode."""
    rows = collect(now, root=root, project=project, limit=limit)
    if not rows:
        return "no sessions found"
    lines = ["%d session(s)%s" % (len(rows),
                                  " in %s" % project if project else "")]
    for summary in rows:
        lines.append("%s %s · %s · %d↑%d↓ · %s" % (
            _status(summary["activity"], now),
            _trunc(summary["title"] or summary["last_prompt"] or summary["id"], 44),
            _project_label(summary),
            summary["n_user"], summary["n_asst"],
            _rel(summary["activity"], now)))
        if summary["last_prompt"]:
            lines.append("    ↳ %s" % _trunc(summary["last_prompt"], 70))
    return "\n".join(lines)
```

5. Change `SESS_ROOT` to be computed rather than assumed, if it is not already: `SESS_ROOT = os.path.join(os.path.expanduser("~"), ".claude", "projects")`.

- [ ] **Step 5: Add `sessions` to `__init__.py`**

Insert `"sessions",` into `__all__`.

- [ ] **Step 6: Run the tests**

Run: `python3 -m unittest tests.dispatch_test.TestSessions -v`
Expected: 8 tests PASS.

- [ ] **Step 7: Run the whole suite**

Run: `bash tests/run.sh`
Expected: exit 0.

- [ ] **Step 8: Commit**

```bash
git add skills/dispatch/dispatch tests/dispatch_test.py
git commit -m "feat(dispatch): port the session dashboard, drop launch"
```

---

### Task 9: Two-lane daemon

Everything built so far is inert until the daemon runs two lanes. This is the task where the tick, the wind-down state machine, dispatch, and the chat surface all become lane-aware.

**Files:**
- Modify: `skills/dispatch/dispatch/daemon.py`
- Test: `tests/dispatch_test.py`, `tests/dispatch_integration.py`

**Interfaces:**
- Consumes: `lanes` (Task 2), `backends` (Task 3), `repos` (Task 4), `scheduler.runnable(queue, agent)` (Task 5), the parser shapes (Task 6), `volume.render` (Task 7), `sessions.render` (Task 8), `governor.codex.estimate` (Task 1).
- Produces:
  - `Daemon(..., codex_estimate=None)` — new injection point, called as `codex_estimate(now)`
  - `Daemon.tick() -> {"mode": dict, "readings": dict, "running": list}` — `mode` is now a dict, not a string
  - `Daemon.handle_command(text, modes, readings, snapshot, now, tokens) -> str|None`
  - `Daemon.usage_reply(poll, snapshot, readings, now, tokens) -> str`

- [ ] **Step 1: Write the failing tests**

Append to `tests/dispatch_test.py`:

```python
class TestTwoLaneDaemon(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._previous = os.environ.get("DISPATCH_HOME")
        os.environ["DISPATCH_HOME"] = os.path.join(self.tmp.name, "home")
        self.addCleanup(self._restore)
        self.projects = os.path.join(self.tmp.name, "Projects")
        for name in ("qpay", "poook"):
            os.makedirs(os.path.join(self.projects, name, ".git"))
        self.now = 1_800_000_000.0

    def _restore(self):
        if self._previous is None:
            os.environ.pop("DISPATCH_HOME", None)
        else:
            os.environ["DISPATCH_HOME"] = self._previous

    def _daemon(self, claude_pct=10.0, codex_pct=10.0, run_step=None):
        from dispatch import chat as chat_mod
        from dispatch import daemon as daemon_mod
        return daemon_mod.Daemon(
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


class _NeverFinishes:
    """Executor whose futures stay pending, so `running` survives the tick."""

    def submit(self, fn, *args, **kwargs):
        import concurrent.futures
        return concurrent.futures.Future()

    def shutdown(self, wait=True):
        return None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.dispatch_test.TestTwoLaneDaemon -v`
Expected: FAIL — `Daemon.__init__() got an unexpected keyword argument 'codex_estimate'`.

- [ ] **Step 3: Add the codex injection point**

In `daemon.py`, extend `__init__`:

```python
    def __init__(self, config=None, clock=None, poll_usage=None, count_tokens=None,
                 chat=None, run_step=None, executor=None, freeform=None,
                 checkpoint=None, codex_estimate=None):
```

and inside it, after `self.count_tokens = ...`:

```python
        self.codex_estimate = codex_estimate or (
            lambda now: governor.codex.estimate(now))
```

Add `lanes`, `repos`, `sessions`, and `volume` to the module imports:

```python
from . import governor, lanes, parser, repos, scheduler, sessions, state, usage, volume, winddown
```

- [ ] **Step 4: Rewrite `tick`**

```python
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

        readings = {
            lanes.CLAUDE: governor.estimate(snapshot, now, tokens),
            lanes.CODEX: self.codex_estimate(now),
        }
        # From here on the document is the single authority: a limit error
        # discovered while settling a step must not be overwritten by the
        # pre-settle snapshot.
        state_doc["governor"] = snapshot

        if self._reap(state_doc, snapshot):
            self._force_poll = True
        modes = self._apply_modes(state_doc, snapshot, readings, now, tokens)

        self._intake(modes, readings, snapshot, now, tokens)
        for lane in lanes.ALL:
            if modes[lane] == winddown.RUNNING:
                self._dispatch(lane, readings[lane], now)

        if self._reap(state_doc, snapshot):
            self._force_poll = True
            modes = self._apply_modes(state_doc, snapshot, readings, now, tokens)

        return {"mode": modes, "readings": readings, "running": sorted(self.running)}
```

- [ ] **Step 5: Make the wind-down machine per-lane**

Replace `_apply_mode` with `_apply_modes`, which runs the identical state machine once per lane over disjoint task sets:

```python
    def _apply_modes(self, state_doc, snapshot, readings, now, tokens):
        """Advance each lane's wind-down machine and persist the result."""
        modes = dict(state_doc["mode"])
        for lane in lanes.ALL:
            modes[lane] = self._apply_lane_mode(
                state_doc, lane, snapshot, readings[lane], now, tokens)
        state_doc["mode"] = modes
        state.write(config_mod.state_path(), state_doc)
        return modes

    def _apply_lane_mode(self, state_doc, lane, snapshot, reading, now, tokens):
        running = self._running_in(lane)
        previous = state_doc["mode"][lane]
        mode = winddown.next_mode(previous, reading["session_pct"], running,
                                  self.config, reading["stale"])

        if mode == winddown.FROZEN and previous != winddown.FROZEN:
            armed = winddown.resume_at(reading.get("resets_at"))
            state_doc["armed_resume_at"][lane] = armed
            self.notify("%s frozen at %s · %s" % (
                lane, self._lane_summary(lane, snapshot, reading, now, tokens),
                "resumes ~%s" % self._reset_text(reading) if armed else
                "resume not scheduled"))

        if mode == winddown.FROZEN:
            allowed, _ = winddown.can_resume(
                {"mode": mode, "armed_resume_at": state_doc["armed_resume_at"][lane]},
                reading["session_pct"], now, self.config, reading["stale"])
            if allowed:
                mode = winddown.RUNNING
                state_doc["armed_resume_at"][lane] = None
                self.notify("%s resumed · %s" % (
                    lane, self._lane_summary(lane, snapshot, reading, now, tokens)))
            elif (state_doc["armed_resume_at"][lane]
                  and now >= state_doc["armed_resume_at"][lane]):
                # The timer fired but the window has not actually rolled over.
                # Confirm with a real poll next tick rather than trusting it.
                self._force_poll = True
        return mode

    def _running_in(self, lane):
        return sum(1 for entry in self.running.values()
                   if lanes.of(entry["task"]) == lane)

    def _lane_summary(self, lane, snapshot, reading, now, tokens):
        if lane == lanes.CLAUDE:
            return governor.summary(snapshot, now, tokens)
        parts = []
        if reading.get("session_pct") is not None:
            parts.append("5h %.0f%%" % reading["session_pct"])
        if reading.get("week_pct") is not None:
            parts.append("7d %.0f%%" % reading["week_pct"])
        parts.append(reading.get("source") or "unknown")
        return " · ".join(parts)
```

Note `can_resume` is handed a two-key dict rather than the whole document, because the document's `mode` is no longer the scalar it expects.

- [ ] **Step 6: Make dispatch per-lane**

Replace `_dispatch` and the lock-name set in its context:

```python
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
                ctx["est_cost_pct"] = governor.est_cost_pct(
                    state_doc, task["repo"], self.config)
                ok, _ = scheduler.admit(task, ctx)
                if ok:
                    candidate = task
                    break
            if candidate is None:
                return
            if not self._start(candidate, now):
                return
```

- [ ] **Step 7: Resolve repos by discovery**

Replace `repo_path` and the `_start` guard:

```python
    def found_repos(self):
        return repos.discover(root=repos.root_path(self.config),
                              overrides=self.config.get("repos"))

    def repo_path(self, repo):
        """Resolve a repo name. Unknown or non-git names are refused."""
        return repos.resolve(repo, found=self.found_repos())
```

- [ ] **Step 8: Rewire `handle_command`**

```python
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
            lane = command.get("agent", lanes.CLAUDE)
            return self.enqueue(command["repo"], command["prompt"],
                                command.get("isolation", "repo"),
                                modes[lane], readings[lane], agent=lane)
        return self.enqueue_freeform(command.get("text", ""),
                                     modes[lanes.CLAUDE], readings[lanes.CLAUDE])

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
```

Update `enqueue` to record the lane and reject non-dispatchable repos with the reason:

```python
    def enqueue(self, repo, prompt, isolation, mode, reading, agent="claude"):
        found = self.found_repos()
        if repos.resolve(repo, found=found) is None:
            return repos.reject_reason(repo, found)
        with state.mutate_queue() as queue:
            task = state.new_task(queue, repo, prompt, isolation=isolation,
                                  agent=agent)
        return "%s · %s" % (parser.render_ack(task["id"], mode,
                                              self._reset_text(reading)), agent)
```

Update `enqueue_freeform`'s two `self.enqueue(...)` calls to pass `agent="claude"` explicitly, and `_default_freeform` to list discovered repos rather than the config map:

```python
        repos_text = ", ".join(sorted(repos.dispatchable(self.found_repos()))) or "none"
```

- [ ] **Step 9: Compose the usage reply**

```python
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
        lines.append(volume.render(now))
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
```

- [ ] **Step 10: Update `status_line`, `queue_line`, and `_intake`**

```python
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
```

In `_intake`, change the signature to `(self, modes, readings, snapshot, now, tokens)` and the `handle_command` call to pass `modes` and `readings` through.

- [ ] **Step 11: Make settle lane-aware**

In `_reap`, drop the now-unused `reading` parameter (`def _reap(self, state_doc, snapshot)`), and in `_settle` scope the limit-error handling to the task's lane:

```python
    def _settle(self, task_id, entry, result, state_doc, snapshot):
        task = entry["task"]
        lane = lanes.of(task)
        mode = state_doc["mode"][lane]

        if result.get("limit_reset_at"):
            # Only the Claude governor carries an override; the codex lane
            # re-reads its own limits from disk next tick and self-corrects.
            if lane == lanes.CLAUDE:
                state_doc["governor"] = governor.note_limit_error(
                    state_doc.get("governor") or snapshot, result["limit_reset_at"])
            state_doc["mode"][lane] = winddown.FROZEN
            state_doc["armed_resume_at"][lane] = winddown.resume_at(
                result["limit_reset_at"])
```

Further down, replace the `worker_next_state(...)` call's mode argument:

```python
                record["state"] = worker_next_state(result.get("status"),
                                                    state_doc["mode"][lane])
```

and prefix the completion notice with the lane:

```python
        if settled["state"] in ("done", "blocked", "failed", "cancelled"):
            self.notify("%s [%s] %s · %s" % (task_id, lane, settled["state"],
                                             summary or "-"))
```

- [ ] **Step 12: Run the new tests**

Run: `python3 -m unittest tests.dispatch_test.TestTwoLaneDaemon -v`
Expected: 9 tests PASS.

- [ ] **Step 13: Extend the integration harness**

Three harness changes in `tests/dispatch_integration.py`, all in `setUp` / `_daemon` / `_enqueue`.

First, the stub `claude` must read its prompt from stdin and echo it, so the prompt-file plumbing is observable end to end. Replace `STUB` with:

```python
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
```

Second, add a stub `codex` beside it, honouring the schema contract — it writes `last.json` where `-o` points and emits a `thread.started` event:

```python
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
```

Write it into the same `bin_dir` as the file `codex`, `chmod 0o755`, exactly as `setUp` already does for `claude`.

Third, `_daemon` must not discover the real `~/Projects`, and needs a codex reading a test can vary:

```python
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
                    "repos": {"demo": self.repo}, "chat_allowlist": []},
            clock=lambda: self.now,
            poll_usage=lambda: reading,
            count_tokens=lambda: 0,
            codex_estimate=lambda now: codex_reading,
            chat=chat_mod.NullChat(),
            executor=daemon_mod.InlineExecutor(),
            **over)

    def _enqueue(self, prompt="do the thing", agent="claude"):
        with state.mutate_queue() as queue:
            return state.new_task(queue, "demo", prompt, agent=agent)
```

Add `os.makedirs(os.path.join(root, "empty-root"))` to `setUp`.

- [ ] **Step 14: Write the failing integration tests**

Append to `tests/dispatch_integration.py`:

```python
    def test_prompt_reaches_the_agent_on_stdin(self):
        """End-to-end proof that the prompt file is plumbed to stdin."""
        task = self._enqueue("plant this exact phrase")
        self._daemon().tick()
        log = open(os.path.join(config_mod.task_dir(task["id"]), "worker.log")).read()
        self.assertIn("prompt-was: plant this exact phrase", log)
        prompt_file = os.path.join(config_mod.task_dir(task["id"]), "prompt.txt")
        self.assertTrue(os.path.exists(prompt_file))
        self.assertIn("Never push", open(prompt_file).read())

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
        """A claude worker and a codex worker must not share a checkout."""
        os.environ["STUB_MODE"] = "continue"
        self._enqueue("first", agent="claude")
        self._enqueue("second", agent="codex")
        daemon = self._daemon()
        started = []
        original = daemon.run_step

        def watched(task, cwd):
            started.append(task["id"])
            return original(task, cwd)

        daemon.run_step = watched
        daemon.tick()
        self.assertEqual(started, ["t-0001"])

    def test_codex_admitted_on_a_stale_but_unexpired_reading(self):
        """A codex percentage is only as fresh as the last codex run, and waiting
        cannot improve it -- so age alone must never block the lane."""
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
```

Run: `python3 -m unittest tests.dispatch_integration -v`
Expected: 6 new tests PASS once Steps 3-12 are in place.

- [ ] **Step 15: Run the whole suite**

Run: `bash tests/run.sh`
Expected: exit 0. Existing tests asserting `tick()["mode"] == "running"` need updating to `tick()["mode"]["claude"]` -- in `tests/dispatch_integration.py` that is `test_step_runs_checkpoints_and_completes` and every other assertion on `result["mode"]`.

- [ ] **Step 16: Commit**

```bash
git add skills/dispatch/dispatch tests/
git commit -m "feat(dispatch): two-lane daemon with per-lane wind-down and chat surface"
```

---

### Task 10: CLI lifecycle and settings cutover

The daemon needs a way to be started that you can remember at 2am, and `setup` has to stop refusing and start doing.

**Files:**
- Modify: `skills/dispatch/dispatch/cli.py`
- Test: `tests/dispatch_test.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `cli.TMUX_SESSION = "dispatchd"`
  - `cli.session_alive(runner=None) -> bool`
  - `cli.cmd_up(args)` — `args.if_dead` suppresses output when already alive
  - `cli.cmd_down(args)`
  - `cli.disable_plugin(settings_path) -> str|None` — returns the backup path, or None when nothing needed changing

- [ ] **Step 1: Write the failing tests**

Append to `tests/dispatch_test.py`:

```python
class TestCliLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _runner(self, alive):
        calls = []

        class Result:
            def __init__(self, code):
                self.returncode = code
                self.stdout = ""
                self.stderr = ""

        def runner(argv, **kwargs):
            calls.append(argv)
            if argv[:2] == ["tmux", "has-session"]:
                return Result(0 if alive else 1)
            return Result(0)

        return runner, calls

    def test_session_alive_reads_has_session(self):
        runner, _ = self._runner(alive=True)
        self.assertTrue(cli.session_alive(runner))
        runner, _ = self._runner(alive=False)
        self.assertFalse(cli.session_alive(runner))

    def test_up_starts_a_detached_session_when_dead(self):
        runner, calls = self._runner(alive=False)
        cli.cmd_up(_Args(if_dead=False, runner=runner))
        started = [c for c in calls if c[:2] == ["tmux", "new-session"]]
        self.assertEqual(len(started), 1)
        self.assertIn("-d", started[0])
        self.assertIn(cli.TMUX_SESSION, started[0])

    def test_up_is_idempotent_when_alive(self):
        runner, calls = self._runner(alive=True)
        cli.cmd_up(_Args(if_dead=False, runner=runner))
        self.assertEqual([c for c in calls if c[:2] == ["tmux", "new-session"]], [])

    def test_if_dead_is_silent_when_alive(self):
        import io
        import contextlib
        runner, _ = self._runner(alive=True)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            cli.cmd_up(_Args(if_dead=True, runner=runner))
        self.assertEqual(buffer.getvalue(), "")

    def test_disable_plugin_flips_the_boolean_and_backs_up(self):
        path = os.path.join(self.tmp.name, "settings.json")
        with open(path, "w") as fh:
            json.dump({"model": "opus", "enabledPlugins": {
                "telegram@claude-plugins-official": True,
                "superpowers@claude-plugins-official": True}}, fh)
        backup = cli.disable_plugin(path)
        self.assertTrue(os.path.exists(backup))
        with open(path) as fh:
            settings = json.load(fh)
        self.assertFalse(settings["enabledPlugins"]["telegram@claude-plugins-official"])
        self.assertTrue(settings["enabledPlugins"]["superpowers@claude-plugins-official"])
        self.assertEqual(settings["model"], "opus")

    def test_disable_plugin_is_a_noop_when_already_off(self):
        path = os.path.join(self.tmp.name, "settings.json")
        with open(path, "w") as fh:
            json.dump({"enabledPlugins": {
                "telegram@claude-plugins-official": False}}, fh)
        self.assertIsNone(cli.disable_plugin(path))

    def test_logs_daemon_reads_the_tmux_pane(self):
        captured = {}

        class Result:
            returncode = 0
            stdout = "daemon says hello"
            stderr = ""

        def runner(argv, **kwargs):
            captured["argv"] = argv
            return Result()

        import contextlib
        import io as _io
        buffer = _io.StringIO()
        with contextlib.redirect_stdout(buffer):
            cli.cmd_logs(_Args(daemon=True, id=None, runner=runner))
        self.assertIn("capture-pane", captured["argv"])
        self.assertIn("daemon says hello", buffer.getvalue())

    def test_disable_plugin_leaves_unrelated_keys_intact(self):
        path = os.path.join(self.tmp.name, "settings.json")
        original = {"hooks": {"PreToolUse": [{"matcher": "Bash"}]},
                    "enabledPlugins": {"telegram@claude-plugins-official": True}}
        with open(path, "w") as fh:
            json.dump(original, fh)
        cli.disable_plugin(path)
        with open(path) as fh:
            self.assertEqual(json.load(fh)["hooks"], original["hooks"])


class _Args:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
```

Add `from dispatch import cli` to the test imports.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.dispatch_test.TestCliLifecycle -v`
Expected: FAIL — `module 'dispatch.cli' has no attribute 'session_alive'`.

- [ ] **Step 3: Add the lifecycle commands**

In `cli.py`, after the imports:

```python
import subprocess

TMUX_SESSION = "dispatchd"


def _tmux(argv, runner=None):
    runner = runner or subprocess.run
    return runner(["tmux"] + list(argv), capture_output=True, text=True)


def session_alive(runner=None):
    return _tmux(["has-session", "-t", TMUX_SESSION], runner).returncode == 0


def _daemon_argv():
    """The command the tmux session runs. Absolute, because cron has no PATH."""
    package = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return "%s -m dispatch.cli run" % sys.executable, package


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
    print("started dispatchd in tmux session '%s'" % TMUX_SESSION)
    return 0


def cmd_down(args):
    runner = getattr(args, "runner", None)
    if not session_alive(runner):
        print("dispatchd is not running")
        return 0
    _tmux(["kill-session", "-t", TMUX_SESSION], runner)
    print("stopped dispatchd")
    return 0
```

Register them in `build_parser`:

```python
    up = subs.add_parser("up", help="start the daemon in tmux (idempotent)")
    up.add_argument("--if-dead", action="store_true",
                    help="say nothing when it is already running")
    up.set_defaults(func=cmd_up)

    subs.add_parser("down", help="stop the tmux session").set_defaults(func=cmd_down)
```

The spec also promises `dispatch logs --daemon`. Add the flag to the existing `logs` subparser and branch on it, so one command covers both "what did this task do" and "what is the daemon saying":

```python
    logs.add_argument("--daemon", action="store_true",
                      help="show the daemon's own output instead of a task's")
```

Make `id` optional (`logs.add_argument("id", nargs="?")`) and add the branch at the top of `cmd_logs`:

```python
def cmd_logs(args):
    from . import parser

    if getattr(args, "daemon", False):
        result = _tmux(["capture-pane", "-pt", TMUX_SESSION],
                       getattr(args, "runner", None))
        if result.returncode != 0:
            print("dispatchd is not running", file=sys.stderr)
            return 2
        sys.stdout.write(result.stdout)
        return 0
    if not args.id:
        print("logs needs a task id, or --daemon", file=sys.stderr)
        return 2
```

leaving the rest of the existing body unchanged below it.

- [ ] **Step 4: Replace the setup refusal with an edit**

The 2026-08-18 design refused to touch `settings.json` because the plugin was a peer interface worth protecting. It is now the thing being replaced, and leaving it enabled guarantees the 409 the refusal was avoiding. Replace `_plugin_enabled`'s use in `cmd_setup` with:

```python
def disable_plugin(settings_path):
    """Turn the conflicting chat plugin off. Returns the backup path, or None.

    Exactly one boolean changes. The file is backed up first, because it is the
    user's, not ours, and a JSON round-trip reformats whatever it touches.
    """
    try:
        with open(settings_path) as fh:
            settings = json.load(fh)
    except (OSError, ValueError):
        return None
    enabled = settings.get("enabledPlugins")
    if not isinstance(enabled, dict) or not enabled.get(PLUGIN_KEY):
        return None
    backup = settings_path + ".bak"
    with open(backup, "w") as fh:
        json.dump(settings, fh, indent=2, sort_keys=True)
        fh.write("\n")
    enabled[PLUGIN_KEY] = False
    with open(settings_path, "w") as fh:
        json.dump(settings, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return backup
```

In `cmd_setup`, replace the refusal block with:

```python
    if _plugin_enabled(settings_path):
        if args.keep_plugin:
            print("warning: %s is still enabled; two consumers of one bot get 409s"
                  % PLUGIN_KEY, file=sys.stderr)
        else:
            backup = disable_plugin(settings_path)
            print("disabled %s in %s (backup: %s)" % (PLUGIN_KEY, settings_path, backup))
```

and add the flag in `build_parser`:

```python
    setup.add_argument("--keep-plugin", action="store_true",
                       help="do not disable the conflicting chat plugin")
```

Delete the `--force` flag and the systemd unit instructions at the end of `cmd_setup`, replacing them with:

```python
    print("start it with: dispatch up")
```

- [ ] **Step 5: Run the tests**

Run: `python3 -m unittest tests.dispatch_test.TestCliLifecycle -v`
Expected: 8 tests PASS.

- [ ] **Step 6: Run the whole suite**

Run: `bash tests/run.sh`
Expected: one failure — `tests/dispatch_integration.py::test_setup_refuses_while_the_chat_plugin_owns_the_bot`, which asserts the old refusal. Rewrite it as `test_setup_disables_the_conflicting_chat_plugin`: assert the return code is 0, that the plugin's boolean is now `False` in the settings fixture, and that `settings.json.bak` exists. Re-run until green.

- [ ] **Step 7: Commit**

```bash
git add skills/dispatch/dispatch/cli.py tests/dispatch_test.py
git commit -m "feat(dispatch): dispatch up/down, and setup disables the chat plugin"
```

---

### Task 11: Teardown, install, cutover

Everything above is code with tests. This task touches the live machine, in an order that matters: one Telegram token admits exactly one consumer, so the old one must be gone before the new one starts.

**Files:**
- Modify: `tests/run.sh`
- Delete: `skills/dispatch/SKILL.md`, `skills/dispatch/references/`, `skills/dispatch/agents/openai.yaml`
- Move: `skills/dispatch/references/operations.md` → `docs/operations.md`
- Delete (outside the repo): `~/.claude/scripts/telegram_health.sh`, `~/.claude/scripts/usage_tg.py`, `~/.claude/skills/manage/`, the health memory, the health cron line

**Interfaces:**
- Consumes: everything from Tasks 1–10.
- Produces: a running daemon reachable from Telegram, and a pushed repo.

- [ ] **Step 1: Free the test suite from the dispatch skill**

`tests/run.sh` requires `SKILL.md` and `agents/openai.yaml` for every skill it names, so deleting them breaks the suite. Edit the loop:

```bash
for skill_name in taskforge orchestrate; do
```

Run: `bash tests/run.sh`
Expected: exit 0, still showing `ok: skill metadata`.

- [ ] **Step 2: Preserve the operations knowledge, drop the skill**

```bash
git mv skills/dispatch/references/operations.md docs/operations.md
git rm -r skills/dispatch/SKILL.md skills/dispatch/references skills/dispatch/agents
```

Then update `docs/operations.md`: it documents one governor, systemd, and hand-configured aliases. Rewrite the "Setup", "What the governor actually knows", and "Safety" sections to match the two-lane, tmux, discovered-repos reality, and add the codex governor's log-reading model. Keep the recovery table and extend it:

| Symptom | Cause | Action |
|---|---|---|
| `codex: no reading (run codex once)` | No codex run since the last reset | Expected; the first codex task corrects it |
| One lane frozen, the other running | Independent governors | Working as designed; `status` shows both |
| Bot silent, tmux session gone | Daemon crashed | `dispatch up`; the cron watchdog does this within 5m |

- [ ] **Step 3: Run the suite and commit the repo-side teardown**

Run: `bash tests/run.sh`
Expected: exit 0.

```bash
git add -A
git commit -m "refactor(dispatch): drop the skill, keep operations as docs"
```

- [ ] **Step 4: Put the CLI on PATH**

```bash
ln -sf ~/Projects/agent-workflow-skills/skills/dispatch/scripts/dispatch ~/.local/bin/dispatch
dispatch --help
```

Expected: the subcommand list, including `up` and `down`.

- [ ] **Step 5: Write config**

```bash
dispatch setup --chat 7256243815
```

Expected: `wrote ~/.claude/dispatch/config.json`, a line reporting the plugin was disabled with its backup path, and `start it with: dispatch up`. No warning about a missing token — if one appears, the token file is not where `config.read_token()` looks.

- [ ] **Step 6: Remove the old cron line**

```bash
crontab -l | grep -v telegram_health | crontab -
crontab -l
```

Expected: no `telegram_health` line. Do this **before** killing the bridge — otherwise the hourly check can spawn a fresh Claude session that steals the token back.

- [ ] **Step 7: Confirm the plugin is off and kill the bridge**

```bash
python3 -c "import json;print(json.load(open('$HOME/.claude/settings.json'))['enabledPlugins'])"
pkill -f 'bun server.ts' || true
pgrep -af 'server.ts' || echo "bridge gone"
```

Expected: `telegram@claude-plugins-official` is `False`, and no `server.ts` process remains.

**This is the point of no return for the Telegram channel.** From here the terminal is the only way to reach a live Claude session until Step 8 succeeds.

- [ ] **Step 8: Start the daemon and verify from Telegram**

```bash
dispatch up
tmux ls
```

Then, from Telegram, send `status`. Expected: a three-line reply naming both lanes and the queue. If nothing arrives:

```bash
tmux capture-pane -pt dispatchd | tail -30
```

Expected failure modes and their causes: no token → `config.read_token()` returned None and the daemon is on `NullChat`; 409 → something else is still polling the bot; no reply but no error → the chat allowlist does not contain your chat id.

- [ ] **Step 9: Verify the rest of the surface from Telegram**

Send each and confirm a sane reply: `repos` (34 folders, 23 dispatchable), `sessions`, `usage` (returns in under a second, both agents), `queue` (empty), `help`.

Then a real end-to-end run: `claude add a one-line comment to the top of README.md on agent-workflow-skills`. Expected: an immediate `queued t-0001 · running · claude`, then a completion notice, and a commit on branch `tg/t-0001`.

- [ ] **Step 10: Install the watchdog**

```bash
( crontab -l; \
  echo '*/5 * * * * /home/navid/.local/bin/dispatch up --if-dead >/dev/null 2>&1'; \
  echo '@reboot /home/navid/.local/bin/dispatch up --if-dead >/dev/null 2>&1' \
) | crontab -
crontab -l
```

Verify it actually recovers:

```bash
dispatch down
sleep 330
tmux ls | grep dispatchd
```

Expected: the session is back. If not, cron's environment lacks something the script needs — check `grep CRON /var/log/syslog | tail`.

- [ ] **Step 11: Delete what is now dead**

Only after Step 9 passed:

```bash
rm -f ~/.claude/scripts/telegram_health.sh ~/.claude/scripts/usage_tg.py
rm -rf ~/.claude/skills/manage
rm -f ~/.claude/projects/-home-navid-Projects/memory/telegram-channel-health.md
```

Then remove the `[Telegram Channel Health]` line from `~/.claude/projects/-home-navid-Projects/memory/MEMORY.md`, leaving the other two entries.

Confirm the surviving skills are intact:

```bash
ls -l ~/.claude/skills/
```

Expected: `graphify`, `orchestrate` → repo, `taskforge` → repo. No `manage`.

- [ ] **Step 12: Record what the machine now looks like**

Write a `project` memory at `~/.claude/projects/-home-navid-Projects/memory/telegram-dispatch.md` describing: the daemon is a standalone tmux process (not an MCP child), Telegram is the only interface, both lanes and where each governor's numbers come from, the cron watchdog, and that `dispatch up` is the recovery command. Add its line to `MEMORY.md`. This replaces the health memory deleted in Step 11.

- [ ] **Step 13: Push**

```bash
cd ~/Projects/agent-workflow-skills
git status --short
bash tests/run.sh
git push origin main
```

Expected: clean tree, green suite, push accepted. This is the "so I won't lose it" step — the bot, both governors, the plan, and the spec are now in `github.com/navidtadjalli/agent-workflow-skills`.
