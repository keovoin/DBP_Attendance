"""Application wiring and the long-polling loop."""

from __future__ import annotations

import logging
import time

from .config import Config
from .db import Database
from .handlers import AttendanceBot
from .health import start_health_server
from .telegram_api import TelegramClient, TelegramError

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

    # Start the health-check server early so the hosting platform's proxy can
    # connect to the machine while we establish the Telegram connection.
    try:
        start_health_server(config.health_port)
    except OSError as exc:
        logger.warning("Could not start health-check server: %s", exc)

    try:
        me = client.get_me()
        logger.info(
            "Starting bot @%s (id=%s)", me.get("username"), me.get("id")
        )
    except TelegramError as exc:
        logger.error("Could not reach Telegram (check BOT_TOKEN): %s", exc)
        return

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
