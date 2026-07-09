"""Daily reminder scheduler.

Runs in a background thread and, on configured working days, sends:

* a **morning reminder** at the work start time to members who have not
  clocked in yet, and
* an **evening reminder** at the work end time to members who are still
  clocked in (forgot to clock out).

Each reminder fires at most once per day (guarded by a stored "last sent"
date). The scheduler uses its own database connection so it never contends
with the bot's polling thread.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime

from . import timeutil
from .config import Config
from .db import (
    CFG_LAST_AUTO_CLOCKOUT,
    CFG_LAST_EVENING,
    CFG_LAST_HOLIDAY_NOTICE,
    CFG_LAST_MORNING,
    Database,
)

logger = logging.getLogger("attendance_bot.scheduler")


class ReminderScheduler:
    def __init__(self, client, config: Config, interval_seconds: int = 60) -> None:
        self.client = client
        self.config = config
        self.interval = interval_seconds
        self._stop = threading.Event()
        self.db: Database | None = None

    def start(self) -> threading.Thread:
        self.db = Database(self.config.db_path)
        thread = threading.Thread(
            target=self._run, name="reminder-scheduler", daemon=True
        )
        thread.start()
        logger.info("Reminder scheduler started (checks every %ds).", self.interval)
        return thread

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.check_and_send(timeutil.now(self.config.tz_offset_hours))
            except Exception as exc:  # never let the thread die
                logger.warning("Reminder tick failed: %s", exc)
            self._stop.wait(self.interval)

    # Pure-ish logic, separated so it can be unit-tested with a fixed ``now``.
    def check_and_send(self, now: datetime) -> None:
        db = self.db
        if db is None:
            return
        sched = db.get_work_schedule()
        today = now.strftime(timeutil.DATE_FMT)
        now_hhmm = now.strftime("%H:%M")
        is_workday = now.weekday() in sched.days
        holiday_name = db.get_holiday_name(today)

        # On a public holiday, don't nag people to clock in - send a one-time
        # holiday greeting instead.
        if (
            sched.reminders_enabled and is_workday and holiday_name
            and now_hhmm >= sched.start
            and db.get_config(CFG_LAST_HOLIDAY_NOTICE) != today
        ):
            for m in db.list_members():
                self._safe_send(
                    m.telegram_id,
                    f"\U0001F389 Today is a public holiday: {holiday_name}. "
                    "Enjoy your day off - no need to clock in!",
                )
            db.set_config(CFG_LAST_HOLIDAY_NOTICE, today)

        # Reminders only run on working days that are not holidays.
        if sched.reminders_enabled and is_workday and not holiday_name:
            members = db.list_members()
            if now_hhmm >= sched.start and db.get_config(CFG_LAST_MORNING) != today:
                for m in members:
                    if db.get_entry_by_date(m.telegram_id, today) is None:
                        self._safe_send(
                            m.telegram_id,
                            "\u23F0 Good morning! You haven't clocked in yet today. "
                            "Send /clockin when you start.",
                        )
                db.set_config(CFG_LAST_MORNING, today)

            if now_hhmm >= sched.end and db.get_config(CFG_LAST_EVENING) != today:
                for m in members:
                    if db.get_open_entry(m.telegram_id, today) is not None:
                        self._safe_send(
                            m.telegram_id,
                            "\U0001F319 You're still clocked in. Remember to "
                            "/clockout before you leave.",
                        )
                db.set_config(CFG_LAST_EVENING, today)

        # Auto clock-out runs every day (people can be clocked in on any day).
        if (
            sched.auto_clockout_enabled
            and now_hhmm >= sched.auto_clockout_time
            and db.get_config(CFG_LAST_AUTO_CLOCKOUT) != today
        ):
            self._auto_clock_out(db, today, sched.auto_clockout_time)
            db.set_config(CFG_LAST_AUTO_CLOCKOUT, today)

    def _auto_clock_out(self, db: Database, date: str, hhmm: str) -> None:
        """Close any entries still open at the end of the day."""
        stamp = f"{date} {hhmm}:00"
        for entry in db.list_open_entries(date):
            db.set_clock_out(entry.id, stamp)
            self._safe_send(
                entry.telegram_id,
                f"\U0001F6CC You forgot to clock out, so you were automatically "
                f"clocked out at {hhmm}. If that's wrong, ask an admin to correct it.",
            )

    def _safe_send(self, chat_id: int, text: str) -> None:
        try:
            self.client.send_message(chat_id, text)
        except Exception as exc:  # a blocked user shouldn't stop the loop
            logger.debug("Could not send reminder to %s: %s", chat_id, exc)
