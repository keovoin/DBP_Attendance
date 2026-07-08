"""Time helpers that honour a fixed UTC offset.

The bot avoids third-party timezone libraries. A single configurable offset
(``TZ_OFFSET_HOURS``) is applied to UTC to produce local timestamps and to
determine the "current date" for attendance logic.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def tzinfo_for_offset(offset_hours: float) -> timezone:
    return timezone(timedelta(hours=offset_hours))


def now(offset_hours: float) -> datetime:
    """Return the current local time for the configured offset."""
    return datetime.now(tzinfo_for_offset(offset_hours))


def now_iso(offset_hours: float) -> str:
    """Current local time as ``YYYY-MM-DD HH:MM:SS``."""
    return now(offset_hours).strftime("%Y-%m-%d %H:%M:%S")


def today_iso(offset_hours: float) -> str:
    """Current local date as ``YYYY-MM-DD``."""
    return now(offset_hours).strftime("%Y-%m-%d")


def is_valid_date(value: str) -> bool:
    """Validate an ISO ``YYYY-MM-DD`` date string."""
    if not _DATE_RE.match(value):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except ValueError:
        return False
