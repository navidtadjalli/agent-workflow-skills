"""Wind-down: stop cleanly before the plan limit stops us badly.

Four modes, and the transitions between them are the whole point:

``running``        dispatching normally.
``winding-down``   past the soft limit. No new dispatch and no new step, but an
                   in-flight step is allowed to finish -- killing it mid-edit
                   would leave a dirty tree and waste what it already spent.
``frozen``         nothing running. A resume is armed for just after the plan
                   window resets.
``paused``         a human said stop. Usage never enters or leaves this mode --
                   only ``resume`` does.

The hard limit is the exception: at that point a straggler is terminated
(SIGTERM, grace, then SIGKILL) and whatever it produced is salvage-committed.
"""

RUNNING = "running"
WINDING_DOWN = "winding-down"
FROZEN = "frozen"
PAUSED = "paused"

RESUME_DELAY = 60  # seconds after the reset before resuming, for clock skew


def next_mode(mode, session_pct, running, config, stale=False):
    """Pure transition function. Returns the mode implied by the situation."""
    if mode == PAUSED:
        # A human outranks a reading. Left to the rules below, a lane paused
        # above the soft limit would become `frozen`, and `frozen` hands itself
        # back to the scheduler as soon as a fresh reading confirms the reset --
        # so an explicit pause would quietly start spending again. Only `resume`
        # leaves this mode.
        return PAUSED

    if session_pct is None or stale:
        # Never escalate on a guess; hold the current mode and let the caller
        # poll. Only an explicit pause or a real reading moves us.
        return mode

    if mode == FROZEN:
        # Only a confirmed reset leaves frozen; see can_resume.
        return FROZEN

    if session_pct >= config["session_soft"]:
        # Past soft or hard, the destination is the same: drain, then freeze.
        # The hard limit changes how in-flight work ends, not where we land.
        return FROZEN if running == 0 else WINDING_DOWN
    if mode == WINDING_DOWN and running == 0:
        # Usage fell back under the soft limit before we finished draining --
        # a window reset, or another session's usage aging out.
        return RUNNING
    return mode


def should_terminate(mode, session_pct, config):
    """True when in-flight work must be cut short rather than allowed to end."""
    return (mode in (WINDING_DOWN, FROZEN)
            and session_pct is not None
            and session_pct >= config["session_hard"])


def resume_at(session_reset):
    """When to arm the resume timer, given the reported reset."""
    return None if not session_reset else session_reset + RESUME_DELAY


def can_resume(state_doc, session_pct, now, config, stale):
    """Whether an armed resume should actually fire.

    The timer only wakes us; the reset is confirmed by a fresh reading, because
    a timer that fires early would burn the first request of the new window on
    a task that immediately hits the same wall.
    """
    if state_doc.get("mode") != FROZEN:
        return False, "not frozen"
    armed = state_doc.get("armed_resume_at")
    if armed and now < armed:
        return False, "resume armed for later"
    if stale or session_pct is None:
        return False, "usage unknown; poll before resuming"
    if session_pct >= config["session_soft"]:
        return False, "usage still %.0f%%" % session_pct
    return True, "reset confirmed"
