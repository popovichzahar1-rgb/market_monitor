"""Minimal Telegram Bot API client (sendMessage, getUpdates, callbacks).

The bot token is part of every request URL; it never reaches logs because
`http._safe_url` masks `/bot<token>/` and `RedactingFilter` masks token patterns.
"""

from __future__ import annotations

import logging
from typing import Any

from market_monitor.config import TelegramConfig
from market_monitor.http import HttpClient, HttpError

log = logging.getLogger(__name__)


class TelegramError(Exception):
    pass


def split_message(text: str, limit: int) -> list[str]:
    """Split on line boundaries so HTML tags (always closed within a line) stay valid."""
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        if len(line) > limit:  # pathological single line: flush, then hard-split it
            if current:
                chunks.append(current)
                current = ""
            while len(line) > limit:
                chunks.append(line[:limit])
                line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


class TelegramClient:
    def __init__(self, http: HttpClient, token: str, config: TelegramConfig) -> None:
        if not token:
            raise TelegramError("TELEGRAM_BOT_TOKEN is not set")
        self.http = http
        self.config = config
        self._base = f"{config.api_base.rstrip('/')}/bot{token}"

    def call(self, method: str, payload: dict[str, Any], retry: bool = True) -> Any:
        try:
            resp = self.http.post(f"{self._base}/{method}", json=payload, retry=retry)
        except HttpError as exc:
            description = ""
            if exc.response is not None:
                try:
                    description = exc.response.json().get("description", "")
                except ValueError:
                    pass
            raise TelegramError(f"{method} failed: HTTP {exc.status} {description}".strip()) from None
        data = resp.json()
        if not data.get("ok"):
            raise TelegramError(f"{method} failed: {data.get('description', 'unknown error')}")
        return data.get("result")

    def send_message(self, chat_id: str, text: str,
                     buttons: list[list[dict]] | None = None) -> int | None:
        """Send (splitting long texts); returns the id of the last message part."""
        message_id = None
        chunks = split_message(text, self.config.max_message_chars)
        for index, chunk in enumerate(chunks):
            payload: dict[str, Any] = {
                "chat_id": chat_id, "text": chunk, "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": True},
            }
            if buttons and index == len(chunks) - 1:
                payload["reply_markup"] = {"inline_keyboard": buttons}
            result = self.call("sendMessage", payload)
            message_id = result.get("message_id") if isinstance(result, dict) else None
        return message_id

    def get_updates(self, offset: int | None) -> list[dict]:
        payload: dict[str, Any] = {"timeout": 0, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        return self.call("getUpdates", payload) or []

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text},
                  retry=False)

    def edit_buttons(self, chat_id: str, message_id: int, buttons: list[list[dict]] | None) -> None:
        self.call("editMessageReplyMarkup", {
            "chat_id": chat_id, "message_id": message_id,
            "reply_markup": {"inline_keyboard": buttons or []},
        }, retry=False)


class TelegramNotifier:
    """Notifier that sends to the single allowed chat."""

    def __init__(self, client: TelegramClient, chat_id: str) -> None:
        if not chat_id:
            raise TelegramError("TELEGRAM_CHAT_ID is not set")
        self.client = client
        self.chat_id = chat_id

    def send(self, text: str, buttons: list[list[dict]] | None = None) -> int | None:
        return self.client.send_message(self.chat_id, text, buttons)
