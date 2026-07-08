"""Shared test fixtures and a fake Telegram client.

The fake client records every outbound call so tests can assert on the
messages, keyboards and documents the bot would have sent, without any
network access.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from attendance_bot.config import Config
from attendance_bot.db import Database
from attendance_bot.handlers import AttendanceBot


class FakeClient:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.documents: list[dict] = []
        self.answered: list[str] = []

    def send_message(self, chat_id, text, *, reply_markup=None, parse_mode=None):
        self.messages.append(
            {"chat_id": chat_id, "text": text, "reply_markup": reply_markup}
        )
        return {"message_id": len(self.messages)}

    def answer_callback_query(self, callback_query_id, text=None):
        self.answered.append(callback_query_id)
        return True

    def send_document(self, chat_id, filename, content, *, caption=None):
        self.documents.append(
            {
                "chat_id": chat_id,
                "filename": filename,
                "content": content,
                "caption": caption,
            }
        )
        return {"message_id": len(self.documents)}

    # Convenience helpers -------------------------------------------------
    @property
    def last_text(self) -> str:
        return self.messages[-1]["text"] if self.messages else ""

    @property
    def last_markup(self):
        return self.messages[-1]["reply_markup"] if self.messages else None

    def all_text(self) -> str:
        return "\n".join(m["text"] for m in self.messages)

    def reset(self) -> None:
        self.messages.clear()
        self.documents.clear()
        self.answered.clear()


@pytest.fixture
def config() -> Config:
    return Config(
        bot_token="test-token",
        db_path=":memory:",
        admin_telegram_ids={999},
        tz_offset_hours=0.0,
        geofence_radius_meters=20.0,
        poll_timeout_seconds=1,
    )


@pytest.fixture
def db() -> Database:
    database = Database(":memory:")
    yield database
    database.close()


@pytest.fixture
def client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def bot(client, db, config) -> AttendanceBot:
    return AttendanceBot(client, db, config)


# ---- Update-building helpers ------------------------------------------- #
def message_update(user_id: int, text: str, *, first_name="Test", last_name="User"):
    return {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "chat": {"id": user_id},
            "from": {
                "id": user_id,
                "first_name": first_name,
                "last_name": last_name,
            },
            "text": text,
        },
    }


def location_update(user_id: int, latitude: float, longitude: float):
    return {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "chat": {"id": user_id},
            "from": {"id": user_id, "first_name": "Test"},
            "location": {"latitude": latitude, "longitude": longitude},
        },
    }


def callback_update(user_id: int, data: str):
    return {
        "update_id": 1,
        "callback_query": {
            "id": "cb1",
            "from": {"id": user_id, "first_name": "Test"},
            "message": {"message_id": 1, "chat": {"id": user_id}},
            "data": data,
        },
    }
