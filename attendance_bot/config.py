"""Configuration loading for the Telegram Attendance Tracker.

Configuration is read from environment variables. A local ``.env`` file, if
present next to the project root, is loaded first (a tiny parser is used so
that no third-party dependency such as python-dotenv is required).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path | None = None) -> None:
    """Load ``KEY=VALUE`` pairs from a .env file into ``os.environ``.

    Existing environment variables are never overwritten. Lines that are
    blank or start with ``#`` are ignored. Surrounding quotes are stripped.
    """
    dotenv_path = path or (PROJECT_ROOT / ".env")
    if not dotenv_path.exists():
        return
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _parse_admin_ids(raw: str) -> set[int]:
    ids: set[int] = set()
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            ids.add(int(chunk))
        except ValueError:
            continue
    return ids


@dataclass
class Config:
    """Runtime configuration for the bot."""

    bot_token: str
    db_path: str = "data/attendance.db"
    admin_telegram_ids: set[int] = field(default_factory=set)
    tz_offset_hours: float = 7.0  # Cambodia / Indochina Time (ICT, UTC+7)
    geofence_radius_meters: float = 20.0
    poll_timeout_seconds: int = 30

    @classmethod
    def from_env(cls, *, require_token: bool = True) -> "Config":
        load_dotenv()
        token = os.environ.get("BOT_TOKEN", "").strip()
        if require_token and not token:
            raise RuntimeError(
                "BOT_TOKEN is not set. Copy .env.example to .env and set your "
                "bot token from @BotFather, or export BOT_TOKEN in the environment."
            )

        def _float(name: str, default: float) -> float:
            try:
                return float(os.environ.get(name, "").strip() or default)
            except ValueError:
                return default

        def _int(name: str, default: int) -> int:
            try:
                return int(os.environ.get(name, "").strip() or default)
            except ValueError:
                return default

        return cls(
            bot_token=token,
            db_path=os.environ.get("DB_PATH", "data/attendance.db").strip()
            or "data/attendance.db",
            admin_telegram_ids=_parse_admin_ids(
                os.environ.get("ADMIN_TELEGRAM_IDS", "")
            ),
            tz_offset_hours=_float("TZ_OFFSET_HOURS", 7.0),
            geofence_radius_meters=_float("GEOFENCE_RADIUS_METERS", 20.0),
            poll_timeout_seconds=_int("POLL_TIMEOUT_SECONDS", 30),
        )
