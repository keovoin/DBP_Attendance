"""Time helpers that honour a fixed UTC offset.

The bot avoids third-party timezone libraries. A single configurable offset
(``TZ_OFFSET_HOURS``) is applied to UTC to produce local timestamps and to
determine the "current date" for attendance logic.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

DATETIME_FMT = "%Y-%m-%d %H:%M:%S"
DATE_FMT = "%Y-%m-%d"

# Monday=0 .. Sunday=6 (matches datetime.weekday()).
WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
WEEKDAY_FULL = [
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
]


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
    if not value or not _DATE_RE.match(value):
        return False
    try:
        datetime.strptime(value, DATE_FMT)
        return True
    except ValueError:
        return False


def is_valid_hhmm(value: str) -> bool:
    """Validate a ``HH:MM`` 24-hour time string."""
    return bool(value) and bool(_HHMM_RE.match(value))


def parse_dt(value: Optional[str]) -> Optional[datetime]:
    """Parse a stored ``YYYY-MM-DD HH:MM:SS`` timestamp, or None."""
    if not value:
        return None
    try:
        return datetime.strptime(value, DATETIME_FMT)
    except (ValueError, TypeError):
        return None


def duration_hours(start_iso: Optional[str], end_iso: Optional[str]) -> float:
    """Hours between two stored timestamps (0 if either is missing/invalid)."""
    a = parse_dt(start_iso)
    b = parse_dt(end_iso)
    if a is None or b is None:
        return 0.0
    return max((b - a).total_seconds() / 3600.0, 0.0)


def format_hours(hours: float) -> str:
    """Render a float number of hours as e.g. ``7h 30m``."""
    total_minutes = int(round(hours * 60))
    h, m = divmod(total_minutes, 60)
    if h and m:
        return f"{h}h {m}m"
    if h:
        return f"{h}h"
    return f"{m}m"


def weekday_of(date_str: str) -> Optional[int]:
    """Return the weekday index (Mon=0..Sun=6) for a YYYY-MM-DD date."""
    try:
        return datetime.strptime(date_str, DATE_FMT).weekday()
    except (ValueError, TypeError):
        return None


def is_workday(date_str: str, work_days: set) -> bool:
    """True if the date falls on one of the configured working weekdays."""
    wd = weekday_of(date_str)
    return wd is not None and wd in work_days


def is_time_after(dt: datetime, hhmm: str) -> bool:
    """True if ``dt``'s time of day is later than the ``HH:MM`` threshold."""
    if not is_valid_hhmm(hhmm):
        return False
    hour, minute = (int(x) for x in hhmm.split(":"))
    threshold = dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return dt > threshold


def is_after_with_grace(dt: datetime, hhmm: str, grace_minutes: int = 0) -> bool:
    """True if ``dt`` is later than ``HH:MM`` plus a grace window (minutes).

    Used for late detection so an admin can allow a few minutes' leeway before
    a clock-in counts as late.
    """
    if not is_valid_hhmm(hhmm):
        return False
    hour, minute = (int(x) for x in hhmm.split(":"))
    threshold = dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if grace_minutes:
        threshold += timedelta(minutes=grace_minutes)
    return dt > threshold


def month_range(dt: datetime) -> tuple[str, str]:
    """Return (first_day, last_day) ISO dates for the month of ``dt``."""
    first = dt.replace(day=1)
    if first.month == 12:
        next_first = first.replace(year=first.year + 1, month=1)
    else:
        next_first = first.replace(month=first.month + 1)
    last = next_first - timedelta(days=1)
    return first.strftime(DATE_FMT), last.strftime(DATE_FMT)


def week_range(dt: datetime) -> tuple[str, str]:
    """Return (Monday, Sunday) ISO dates for the week containing ``dt``."""
    monday = dt - timedelta(days=dt.weekday())
    sunday = monday + timedelta(days=6)
    return monday.strftime(DATE_FMT), sunday.strftime(DATE_FMT)


def count_workdays(start_date: str, end_date: str, work_days: set) -> int:
    """Count configured working days in an inclusive date range."""
    try:
        start = datetime.strptime(start_date, DATE_FMT)
        end = datetime.strptime(end_date, DATE_FMT)
    except (ValueError, TypeError):
        return 0
    count = 0
    cur = start
    while cur <= end:
        if cur.weekday() in work_days:
            count += 1
        cur += timedelta(days=1)
    return count
