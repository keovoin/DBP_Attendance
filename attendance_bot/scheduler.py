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
from .db import CFG_LAST_EVENING, CFG_LAST_MORNING, Database

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
        if not sched.reminders_enabled:
            return
        today = now.strftime(timeutil.DATE_FMT)
        if now.weekday() not in sched.days:
            return
        now_hhmm = now.strftime("%H:%M")
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

    def _safe_send(self, chat_id: int, text: str) -> None:
        try:
            self.client.send_message(chat_id, text)
        except Exception as exc:  # a blocked user shouldn't stop the loop
            logger.debug("Could not send reminder to %s: %s", chat_id, exc)
