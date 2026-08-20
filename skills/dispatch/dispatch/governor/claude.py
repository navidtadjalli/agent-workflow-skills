"""The governor: what fraction of the plan window is already spent.

Polling ``/usage`` is the only source of truth, and it is expensive -- it burns
a request against the limit it reports. So the governor polls rarely and fills
the gaps for free:

* At each real poll it records ``(percent, tokens)``. Two consecutive polls give
  a measured ``percent per token`` ratio.
* Between polls it re-reads the transcript token counter -- free, local -- and
  projects ``last_percent + delta_tokens * ratio``.
* If a worker dies with a real usage-limit error, the reset timestamp in that
  error overrides the estimate outright: reactive truth beats projection.

The ratio is learned rather than assumed because tokens-per-percent depends on
model, cache hit rate, and plan, none of which are knowable up front.
"""

# Seeded conservatively until two real polls have been observed: assume one
# percent of the session window per million tokens.
SEED_PCT_PER_TOKEN = 1.0 / 1_000_000
RATIO_SMOOTHING = 0.5

# Concurrency ladder, read as: while session usage is below `pct`, allow `n`
# workers at once. The last rung is the wind-down floor.
LADDER = ((40.0, 3), (65.0, 2), (85.0, 1), (float("inf"), 0))

EMPTY = {
    "session_pct": None,
    "session_reset": None,
    "week_pct": None,
    "week_reset": None,
    "polled_at": None,
    "tokens_at_poll": None,
    "session_ratio": None,
    "week_ratio": None,
    "override_until": None,
    "last_poll_ok": None,
    "last_error": None,
}


def blank():
    return dict(EMPTY)


def max_concurrency(session_pct):
    """Workers allowed to run at once at this usage level."""
    if session_pct is None:
        return 1
    for ceiling, allowed in LADDER:
        if session_pct < ceiling:
            return allowed
    return 0


def poll_interval(snapshot, hot, config):
    """Seconds to wait before the next real poll is even considered."""
    floor = config["poll_floor"]
    wanted = config["poll_hot"] if hot else config["poll_idle"]
    if snapshot.get("last_poll_ok") is False:
        wanted = max(floor, wanted // 2)
    return max(floor, wanted)


def should_poll(snapshot, now, hot=False, force=False, config=None):
    """True when a real ``/usage`` call is warranted.

    ``force`` is set after a step ends -- the moment the estimate is least
    trustworthy -- but the ``poll_floor`` still applies, so a burst of short
    steps cannot turn the governor into a request sink.
    """
    config = config or {}
    floor = config.get("poll_floor", 60)
    polled_at = snapshot.get("polled_at")
    if polled_at is None:
        return True
    since = now - polled_at
    if since < floor:
        return False
    if force:
        return True
    reset = snapshot.get("session_reset")
    if reset and now >= reset:
        return True
    return since >= poll_interval(snapshot, hot, {
        "poll_floor": floor,
        "poll_idle": config.get("poll_idle", 600),
        "poll_hot": config.get("poll_hot", 180),
    })


def record_poll(snapshot, reading, tokens_now):
    """Fold a fresh ``/usage`` reading into the snapshot and relearn the ratio.

    Returns the updated snapshot. A failed poll is recorded but never destroys
    the previous good reading -- a stale truth beats no truth.
    """
    updated = dict(snapshot)
    if not reading.get("ok"):
        updated["last_poll_ok"] = False
        updated["last_error"] = reading.get("error")
        return updated

    updated["session_ratio"] = _relearn(
        snapshot.get("session_ratio"),
        snapshot.get("session_pct"), reading.get("session_pct"),
        snapshot.get("tokens_at_poll"), tokens_now)
    updated["week_ratio"] = _relearn(
        snapshot.get("week_ratio"),
        snapshot.get("week_pct"), reading.get("week_pct"),
        snapshot.get("tokens_at_poll"), tokens_now)

    for key in ("session_pct", "session_reset", "week_pct", "week_reset"):
        if reading.get(key) is not None:
            updated[key] = reading[key]
    updated["polled_at"] = reading.get("at")
    updated["tokens_at_poll"] = tokens_now
    updated["last_poll_ok"] = True
    updated["last_error"] = None

    override = updated.get("override_until")
    if override and reading.get("at") and reading["at"] >= override:
        updated["override_until"] = None
    return updated


def _relearn(current, old_pct, new_pct, old_tokens, new_tokens):
    """Blend a newly measured percent-per-token into the running ratio."""
    if None in (old_pct, new_pct, old_tokens, new_tokens):
        return current
    delta_tokens = new_tokens - old_tokens
    delta_pct = new_pct - old_pct
    # A drop means the window rolled over between polls; that pair measures
    # nothing. A zero token delta would divide by zero.
    if delta_tokens <= 0 or delta_pct <= 0:
        return current
    measured = delta_pct / delta_tokens
    if current is None:
        return measured
    return current * (1 - RATIO_SMOOTHING) + measured * RATIO_SMOOTHING


def note_limit_error(snapshot, reset_at):
    """Record that a worker hit the real limit; pin usage until ``reset_at``."""
    updated = dict(snapshot)
    if reset_at:
        updated["override_until"] = reset_at
        updated["session_reset"] = reset_at
    updated["session_pct"] = 100.0
    return updated


def estimate(snapshot, now, tokens_now):
    """Current usage: measured where possible, projected otherwise.

    ``stale`` marks an estimate the caller should not trust for admission --
    either nothing has ever been polled, or the session window rolled over and
    the token baseline no longer corresponds to the new window.
    """
    # `week_resets_at` rides along on every branch: a lane frozen by the weekly
    # window has to arm its resume for when *that* window rolls over, not for
    # the session reset an hour from now.
    week_reset = snapshot.get("week_reset")

    override = snapshot.get("override_until")
    if override and now < override:
        return {"session_pct": 100.0, "week_pct": snapshot.get("week_pct") or 100.0,
                "source": "limit-error", "stale": False,
                "resets_at": override, "week_resets_at": week_reset}

    session_pct = snapshot.get("session_pct")
    if session_pct is None or snapshot.get("tokens_at_poll") is None:
        return {"session_pct": None, "week_pct": snapshot.get("week_pct"),
                "source": "unknown", "stale": True,
                "resets_at": snapshot.get("session_reset"),
                "week_resets_at": week_reset}

    reset = snapshot.get("session_reset")
    if reset and now >= reset:
        # The window rolled over. Real usage is near zero, but the token
        # baseline is from the previous window, so demand a fresh poll before
        # anything is admitted on the strength of it.
        return {"session_pct": 0.0, "week_pct": snapshot.get("week_pct"),
                "source": "post-reset", "stale": True, "resets_at": None,
                "week_resets_at": week_reset}

    burned = max(0, tokens_now - snapshot["tokens_at_poll"])
    session_ratio = snapshot.get("session_ratio") or SEED_PCT_PER_TOKEN
    week_ratio = snapshot.get("week_ratio") or (SEED_PCT_PER_TOKEN / 7)
    week_pct = snapshot.get("week_pct")
    return {
        "session_pct": _clamp(session_pct + burned * session_ratio),
        "week_pct": None if week_pct is None else _clamp(week_pct + burned * week_ratio),
        "source": "measured" if burned == 0 else "projected",
        "stale": False,
        "resets_at": reset,
        "week_resets_at": week_reset,
    }


def _clamp(value):
    return max(0.0, min(100.0, value))


def est_cost_pct(state_doc, repo, config):
    """Learned cost of one step in ``repo``, as percent of the session window."""
    learned = (state_doc.get("repo_cost_pct") or {}).get(repo)
    if isinstance(learned, (int, float)) and learned > 0:
        return float(learned)
    return float(config["default_est_cost_pct"])


def learn_cost(state_doc, repo, observed_pct):
    """Fold an observed step cost into the per-repo running average."""
    if observed_pct is None or observed_pct < 0:
        return
    costs = state_doc.setdefault("repo_cost_pct", {})
    previous = costs.get(repo)
    costs[repo] = observed_pct if previous is None else previous * 0.7 + observed_pct * 0.3


def summary(snapshot, now, tokens_now):
    """One-line human summary for `dispatch usage` and chat replies."""
    reading = estimate(snapshot, now, tokens_now)
    if reading["session_pct"] is None:
        return "usage unknown (never polled)"
    parts = ["session %.0f%%" % reading["session_pct"]]
    if reading["week_pct"] is not None:
        parts.append("week %.0f%%" % reading["week_pct"])
    parts.append(reading["source"])
    parts.append("max %d worker(s)" % max_concurrency(reading["session_pct"]))
    return " · ".join(parts)
