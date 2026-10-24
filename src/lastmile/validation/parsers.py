"""Flexible, explicit input parsers.

Parsing never guesses: it either understands an input or reports a clear
error so the user can correct it.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Callable

_RELATIVE_PATTERNS: list[tuple[re.Pattern[str], Callable[[re.Match[str], datetime], timedelta]]] = [
    (
        re.compile(r"^in\s+(\d+)\s*(min|min:mins?|m)\b.*$", re.IGNORECASE),
        lambda m, _now: timedelta(minutes=int(m.group(1))),
    ),
    (
        re.compile(r"^in\s+(\d+)\s*(h|hr|hrs|hours?)\b.*$", re.IGNORECASE),
        lambda m, _now: timedelta(hours=int(m.group(1))),
    ),
    (
        re.compile(r"^in\s+(\d+)\s*(d|day|days)\b.*$", re.IGNORECASE),
        lambda m, _now: timedelta(days=int(m.group(1))),
    ),
    (
        re.compile(r"^(today|tonight)\s*@\s*(\d{1,2})([:.](\d{2}))?\s*(am|pm)?$", re.IGNORECASE),
        lambda m, _now: timedelta(0),  # handled specially below
    ),
]


def _naive_local_to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_flexible_datetime(text: str) -> tuple[datetime | None, str]:
    """Parse a datetime string; never guesses.

    Accepted:
      * ISO 8601 ("2026-10-24T18:00", "...+01:00", with T or space)
      * "in 30 min", "in 3 hours", "in 2 days"  (relative to now, UTC)
      * "today 17:30" / "tomorrow 09:00" / "tomorrow 9am"
    Returns (aware_utc_datetime, "") or (None, error_message).
    """
    if not text or not text.strip():
        return None, "value is required"
    value = text.strip()

    now = datetime.now(timezone.utc)

    # ISO 8601 (accept a space instead of T).
    iso_candidate = value.replace(" ", "T", 1) if " " in value and "T" not in value else value
    try:
        parsed = datetime.fromisoformat(iso_candidate.replace("Z", "+00:00"))
        return _naive_local_to_utc(parsed), ""
    except ValueError:
        pass

    # Relative: "in 30 min" / "in 3 hours" / "in 2 days"
    m = re.match(r"^in\s+(?:about\s+)?(\d+(?:h|\.\d+)?)\s*(min|mins|minutes?|h|hr|hrs|hours?|d|days)\b.*$",
                 value, re.IGNORECASE)
    if m:
        amount = float(m.group(1))
        unit = m.group(2).lower()

        if unit.startswith("m"):
            return now + timedelta(minutes=amount), ""
        if unit.startswith("h"):
            return now + timedelta(hours=amount), ""
        return now + timedelta(days=amount), ""

    # "today 17:30" / "tomorrow 9am" / "tonight 20h"
    m = re.match(r"^(today|tonight|tomorrow)\s+(\d{1,2})([:.](\d{2}))?\s*(am|pm)?$", value, re.IGNORECASE)
    if m:
        day_word = m.group(1).lower()
        hour = int(m.group(2))
        minute = int(m.group(4) or 0)
        ampm = (m.group(5) or "").lower()
        if ampm == "pm" and hour < 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0
        if hour > 23:
            return None, f"hour {hour} is out of range"
        base = now + (timedelta(days=1) if day_word == "tomorrow" else timedelta(0))
        target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return target, ""

    # Bare "HH:MM" -> today (or tomorrow if already past)
    m = re.match(r"^(\d{1,2}):(\d{2})$", value)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        if hour > 23 or minute > 59:
            return None, f"{value} is not a valid time"
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target, ""

    return None, (
        f"could not parse datetime {text!r}. Use ISO 8601 "
        "(e.g. 2026-10-24T18:00) or forms like 'in 3 hours', 'today 17:30', "
        "'tomorrow 09:00'"
    )
