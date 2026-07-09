"""Application wiring and the long-polling loop."""

from __future__ import annotations

import logging
import time

from .config import Config
from .db import Database
from .handlers import AttendanceBot
from .health import start_health_server
from .scheduler import ReminderScheduler
from .telegram_api import TelegramClient, TelegramError
from .web import start_web_portal

logger = logging.getLogger("attendance_bot")


def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = Config.from_env()
    db = Database(config.db_path)
    client = TelegramClient(config.bot_token, timeout=config.poll_timeout_seconds)
    bot = AttendanceBot(client, db, config)

    # Serve HTTP on the machine's port so the hosting platform's proxy can
    # reach it. If an admin portal password is set, run the full web dashboard;
    # otherwise run only a minimal health endpoint.
    try:
        if config.admin_portal_password:
            start_web_portal(config, client)
        else:
            logger.info(
                "ADMIN_PORTAL_PASSWORD not set - starting health endpoint only "
                "(no admin dashboard)."
            )
            start_health_server(config.health_port)
    except OSError as exc:
        logger.warning("Could not start HTTP server: %s", exc)

    # Background daily reminders (morning clock-in / evening clock-out).
    try:
        ReminderScheduler(client, config).start()
    except Exception as exc:  # pragma: no cover - non-fatal
        logger.warning("Could not start reminder scheduler: %s", exc)

    try:
        me = client.get_me()
        logger.info(
            "Starting bot @%s (id=%s)", me.get("username"), me.get("id")
        )
    except TelegramError as exc:
        # Do NOT exit: a clean exit would let the platform mark the machine as
        # stopped. Keep the process alive and let the polling loop retry so the
        # error stays visible in the logs (usually a wrong/missing BOT_TOKEN).
        logger.error(
            "Could not reach Telegram at startup (check BOT_TOKEN): %s. "
            "Retrying in the polling loop...",
            exc,
        )

    offset = None
    logger.info("Polling for updates. Press Ctrl+C to stop.")
    while True:
        try:
            updates = client.get_updates(offset=offset)
        except KeyboardInterrupt:
            logger.info("Shutting down.")
            break
        except TelegramError as exc:
            logger.warning("getUpdates failed: %s", exc)
            time.sleep(3)
            continue
        except Exception as exc:  # network hiccups, timeouts, etc.
            logger.warning("Polling error: %s", exc)
            time.sleep(3)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            bot.handle_update(update)


if __name__ == "__main__":  # pragma: no cover
    try:
        run()
    except KeyboardInterrupt:
        pass
