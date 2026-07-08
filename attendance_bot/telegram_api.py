"""A minimal Telegram Bot API client built on the standard library.

Only the handful of Bot API methods needed by the attendance tracker are
implemented: ``getUpdates`` (long polling), ``sendMessage``,
``answerCallbackQuery``, ``getMe`` and ``sendDocument`` (multipart upload).

No third-party HTTP library is required; everything uses ``urllib``.
"""

from __future__ import annotations

import json
import mimetypes
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Optional


class TelegramError(Exception):
    """Raised when the Telegram Bot API returns an error response."""


class TelegramClient:
    def __init__(self, token: str, timeout: int = 30) -> None:
        self.token = token
        self.timeout = timeout
        self.base_url = f"https://api.telegram.org/bot{token}"

    # ------------------------------------------------------------------ #
    # Low-level request helpers
    # ------------------------------------------------------------------ #
    def _call(self, method: str, payload: Optional[dict] = None, *, timeout: Optional[int] = None) -> Any:
        url = f"{self.base_url}/{method}"
        data = None
        headers = {"Content-Type": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        return self._read(request, timeout=timeout)

    def _read(self, request: urllib.request.Request, *, timeout: Optional[int] = None) -> Any:
        effective_timeout = timeout if timeout is not None else self.timeout + 5
        try:
            with urllib.request.urlopen(request, timeout=effective_timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:  # pragma: no cover - network path
            detail = exc.read().decode("utf-8", errors="replace")
            raise TelegramError(f"HTTP {exc.code}: {detail}") from exc
        if not body.get("ok"):
            raise TelegramError(
                f"Telegram API error: {body.get('description', 'unknown error')}"
            )
        return body.get("result")

    # ------------------------------------------------------------------ #
    # Bot API methods
    # ------------------------------------------------------------------ #
    def get_me(self) -> dict:
        return self._call("getMe")

    def get_updates(self, offset: Optional[int] = None) -> list[dict]:
        payload: dict[str, Any] = {
            "timeout": self.timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        return self._call("getUpdates", payload, timeout=self.timeout + 10) or []

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: Optional[dict] = None,
        parse_mode: Optional[str] = None,
    ) -> dict:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        if parse_mode is not None:
            payload["parse_mode"] = parse_mode
        return self._call("sendMessage", payload)

    def answer_callback_query(
        self, callback_query_id: str, text: Optional[str] = None
    ) -> Any:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text is not None:
            payload["text"] = text
        return self._call("answerCallbackQuery", payload)

    def send_document(
        self,
        chat_id: int,
        filename: str,
        content: bytes,
        *,
        caption: Optional[str] = None,
    ) -> dict:
        """Upload an in-memory document using a multipart/form-data request."""
        boundary = uuid.uuid4().hex
        parts: list[bytes] = []

        def add_field(name: str, value: str) -> None:
            parts.append(
                (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                    f"{value}\r\n"
                ).encode("utf-8")
            )

        add_field("chat_id", str(chat_id))
        if caption:
            add_field("caption", caption)

        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="document"; filename="{filename}"\r\n'
                f"Content-Type: {mime}\r\n\r\n"
            ).encode("utf-8")
        )
        parts.append(content)
        parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
        body = b"".join(parts)

        url = f"{self.base_url}/sendDocument"
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        return self._read(request)


# ---------------------------------------------------------------------- #
# Keyboard builders
# ---------------------------------------------------------------------- #
def inline_keyboard(rows: list[list[tuple[str, str]]]) -> dict:
    """Build an inline keyboard from ``(text, callback_data)`` tuples."""
    return {
        "inline_keyboard": [
            [{"text": text, "callback_data": data} for text, data in row]
            for row in rows
        ]
    }


def location_request_keyboard(button_text: str = "\U0001F4CD Share my location") -> dict:
    """A one-time reply keyboard whose single button requests the location."""
    return {
        "keyboard": [[{"text": button_text, "request_location": True}]],
        "resize_keyboard": True,
        "one_time_keyboard": True,
    }


def remove_keyboard() -> dict:
    return {"remove_keyboard": True}
