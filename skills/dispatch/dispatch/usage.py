"""The governor's two inputs: real plan percentages, and free token volume.

``poll`` shells out to the agent CLI's ``/usage`` command. That is the only
place the true plan percentages exist -- they are not written to disk -- and it
costs one request against the very limit it reports, so the governor calls it
rarely and estimates in between.

``transcript_tokens`` is the free half: it sums the ``usage`` blocks that local
session transcripts already record. It says nothing about plan percentages on
its own, but its *delta* is proportional to consumption, which is enough to
interpolate between two real polls.
"""
import glob
import json
import os
import re
import subprocess
import time

from . import config

SESSION_WINDOW = 5 * 3600
WEEK_WINDOW = 7 * 24 * 3600

_PCT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_RESET_INLINE = re.compile(r"resets?\b[^0-9A-Za-z]*(.+)$", re.IGNORECASE)
_EPOCH_LIMIT = re.compile(r"usage limit reached\s*\|\s*(\d{9,13})", re.IGNORECASE)
_CLOCK = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", re.IGNORECASE)
_RELATIVE = re.compile(r"(?:in\s+)?(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?", re.IGNORECASE)
# The day half of a calendar reset, left deliberately open at the end: what
# follows is either nothing (`Feb 5`) or a time of day (`Aug 20, 7:20pm`), and
# the time is read separately by the same code that reads a bare clock.
_MONTH_DAY = re.compile(r"^([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?!\d)",
                        re.IGNORECASE)
_MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()


def _at(year, month, day, hour, minute):
    """Epoch of a local wall-clock moment. ``-1`` lets libc settle DST."""
    return time.mktime((year, month, day, hour, minute, 0, 0, 1, -1))


def _future(stamp, now):
    """``stamp`` if it is still ahead, else None.

    Every expression these parsers read names the moment a window *will* roll
    over, so a result behind ``now`` is never a real answer -- it is a misparse
    of an ambiguous string, a clock skew, or a reading the CLI produced before
    the window it describes had already turned. None is the safe form of all
    three: callers already treat a missing reset as "unknown" and fall back to
    their normal cadence, whereas a past reset tells the governor the window
    just rolled over -- on every tick, for as long as it is stored.
    """
    return stamp if stamp is not None and stamp > now else None


def _clock_fields(text, require_marker=False):
    """``(hour, minute)`` for a wall-clock time at the head of ``text``.

    ``require_marker`` demands an explicit ``:mm`` or an ``am``/``pm``, which is
    what separates ``Aug 21, 7pm`` from ``Aug 21 2026``: after a calendar day a
    bare run of digits is far more likely to be a year than an hour.
    """
    match = _CLOCK.match(text)
    if not match:
        return None
    if require_marker and not (match.group(2) or match.group(3)):
        return None
    if text[match.end():match.end() + 1].isdigit():
        return None  # more digits follow: a year, not an hour
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    suffix = (match.group(3) or "").lower()
    if suffix == "pm" and hour < 12:
        hour += 12
    elif suffix == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return hour, minute


def parse_reset(text, now):
    """Best-effort epoch for a reset expression. None when unparseable.

    Handles the shapes ``/usage`` and limit errors actually emit: a calendar day
    with a time (``Aug 20, 7:20pm (Asia/Tehran)`` -- what the CLI really prints,
    in either 12- or 24-hour form and with the zone in parentheses), a bare
    calendar day (``Feb 5``), a wall clock time (``7:50pm``), a relative span
    (``in 2h 15m``), a full ISO timestamp, and a bare epoch.

    Every reconstructed result is passed through ``_future``: a reset that has
    already happened is reported as unknown rather than believed. The two
    absolute forms -- ISO and bare epoch -- are exempt, because they state a
    moment outright instead of rebuilding one from a partial expression;
    ``governor.record_poll`` is the layer that refuses to *store* a past reset,
    whichever branch produced it.
    """
    if not text:
        return None
    raw = text.strip().rstrip(".)")
    if not raw:
        return None

    iso = re.search(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})", raw)
    if iso:
        try:
            return time.mktime(time.strptime(iso.group(1) + " " + iso.group(2),
                                             "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            return None

    if re.match(r"^\d{9,13}$", raw):
        value = float(raw)
        return value / 1000.0 if value > 1e11 else value

    month = _MONTH_DAY.match(raw)
    if month and month.group(1).lower() in _MONTHS:
        day = int(month.group(2))
        if not 1 <= day <= 31:
            return None
        number = _MONTHS.index(month.group(1).lower()) + 1
        # Whatever follows the day is the time of day, when there is one. It is
        # optional because `Feb 5` really does arrive with no time -- but when
        # the CLI does print one, dropping it puts the reset up to a full day
        # wrong, and a session reset an hour early reads as a window that has
        # already rolled over.
        rest = raw[month.end():].lstrip(" \t,")
        hour, minute = _clock_fields(rest, require_marker=True) or (0, 0)
        base = time.localtime(now)
        stamp = _at(base.tm_year, number, day, hour, minute)
        if stamp < now - 180 * 86400:
            # Far enough behind that the expression must mean next year's date
            # rather than this year's -- `Feb 5` read in August.
            stamp = _at(base.tm_year + 1, number, day, hour, minute)
        return _future(stamp, now)

    relative = _RELATIVE.match(raw)
    if relative and (relative.group(1) or relative.group(2)):
        return _future(
            now + int(relative.group(1) or 0) * 3600 + int(relative.group(2) or 0) * 60,
            now)

    clock = _clock_fields(raw)
    if clock:
        base = time.localtime(now)
        stamp = _at(base.tm_year, base.tm_mon, base.tm_mday, clock[0], clock[1])
        if stamp <= now:
            stamp += 86400  # a time of day that has passed means tomorrow
        return _future(stamp, now)
    return None


def parse_usage_text(text, now=None):
    """Pull session/week percentages and resets out of ``/usage`` output."""
    now = time.time() if now is None else now
    result = {"session_pct": None, "session_reset": None,
              "week_pct": None, "week_reset": None}
    pending = None
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if "session" in lowered:
            pending = "session"
        elif "week" in lowered:
            pending = "week"
        elif not lowered.startswith("reset"):
            continue
        if pending is None:
            continue
        percent = _PCT.search(stripped)
        if percent and result[pending + "_pct"] is None:
            result[pending + "_pct"] = float(percent.group(1))
        reset = _RESET_INLINE.search(stripped)
        if reset and result[pending + "_reset"] is None:
            result[pending + "_reset"] = parse_reset(reset.group(1), now)
    return result


def parse_limit_error(text, now=None):
    """Epoch at which a usage-limit error says the limit clears, else None."""
    now = time.time() if now is None else now
    if not text:
        return None
    epoch = _EPOCH_LIMIT.search(text)
    if epoch:
        value = float(epoch.group(1))
        return value / 1000.0 if value > 1e11 else value
    if "limit" not in text.lower():
        return None
    reset = _RESET_INLINE.search(text)
    return parse_reset(reset.group(1), now) if reset else None


def poll(runner=None, timeout=120, now=None):
    """Ask the agent CLI for real plan percentages. Costs one request."""
    now = time.time() if now is None else now
    if runner is None:
        def runner():
            return subprocess.run(
                ["claude", "-p", "/usage", "--output-format", "text"],
                capture_output=True, text=True, timeout=timeout).stdout
    try:
        text = runner()
    except Exception as exc:  # noqa: BLE001 - a failed poll must never crash the daemon
        return {"ok": False, "error": str(exc), "at": now}
    snapshot = parse_usage_text(text, now)
    snapshot["ok"] = snapshot["session_pct"] is not None
    snapshot["at"] = now
    if not snapshot["ok"]:
        snapshot["error"] = "no percentages in /usage output"
    return snapshot


def transcript_tokens(now=None, root=None, window=SESSION_WINDOW):
    """Total tokens recorded in local transcripts within ``window``.

    Deduplicated by assistant message id, because a resumed session replays
    earlier messages into a new file.
    """
    now = time.time() if now is None else now
    root = root or config.transcripts_root()
    total = 0
    seen = set()
    for path in glob.glob(os.path.join(root, "*", "*.jsonl")):
        try:
            if now - os.path.getmtime(path) > window:
                continue
        except OSError:
            continue
        try:
            handle = open(path, errors="ignore")
        except OSError:
            continue
        with handle:
            for line in handle:
                if '"usage"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                message = record.get("message") or {}
                block = message.get("usage") or {}
                if not block:
                    continue
                message_id = message.get("id")
                if message_id:
                    if message_id in seen:
                        continue
                    seen.add(message_id)
                stamp = _timestamp(record.get("timestamp"))
                if stamp is None or now - stamp > window:
                    continue
                total += sum(block.get(key, 0) or 0 for key in (
                    "input_tokens", "output_tokens",
                    "cache_creation_input_tokens", "cache_read_input_tokens"))
    return total


def _timestamp(value):
    if not value:
        return None
    try:
        return time.mktime(time.strptime(value[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return None
