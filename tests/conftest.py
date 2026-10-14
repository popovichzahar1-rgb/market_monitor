from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import httpx
import pytest

from market_monitor.config import Config, HttpConfig, Secrets
from market_monitor.http import HttpClient
from market_monitor.storage import Storage

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture
def now() -> datetime:
    return NOW


@pytest.fixture
def storage():
    with Storage(":memory:") as s:
        yield s


@pytest.fixture
def secrets() -> Secrets:
    return Secrets(sec_contact_email="test@example.com", fred_api_key="testfredkey")


@pytest.fixture
def make_http() -> Callable[..., HttpClient]:
    """Build an HttpClient backed by a request handler; no real network, no sleeping."""
    clients: list[HttpClient] = []

    def factory(handler: Callable[[httpx.Request], httpx.Response], **http_overrides) -> HttpClient:
        cfg = HttpConfig(**{"backoff_s": 0.0, "per_host_min_interval_s": 0.0, **http_overrides})
        client = HttpClient(cfg, transport=httpx.MockTransport(handler), sleep=lambda s: None)
        clients.append(client)
        return client

    yield factory
    for c in clients:
        c.close()


def route(mapping: dict[str, bytes | int | Callable[[httpx.Request], httpx.Response]]):
    """Handler that serves fixture bytes by URL (without query); robots.txt -> 404."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url.copy_with(query=None))
        if request.url.path == "/robots.txt" and url not in mapping:
            return httpx.Response(404)
        target = mapping.get(url)
        if target is None:
            return httpx.Response(404)
        if callable(target):
            return target(request)
        if isinstance(target, int):
            return httpx.Response(target)
        return httpx.Response(200, content=target)

    return handler


def make_config(**overrides) -> Config:
    data = {
        "storage": {"path": ":memory:"},
        "sources": {
            "rss": {
                "feeds": [
                    {"name": "markets", "url": "https://news.example.com/rss", "tags": ["markets"]},
                    {"name": "crypto", "url": "https://crypto.example.org/atom", "tags": ["crypto"]},
                ]
            }
        },
    }
    data.update(overrides)
    return Config.model_validate(data)


TEST_TOKEN = "123456789:AAH-test-token-abcdefghijklmnopqrstuvwxyz"
TEST_CHAT = "4242"


class FakeTelegram:
    """In-memory Telegram Bot API: records calls, serves queued updates."""

    def __init__(self, fail_methods: set[str] | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.updates: list[dict] = []
        self.next_message_id = 100
        self.fail_methods = fail_methods or set()

    @property
    def sent(self) -> list[dict]:
        return [p for m, p in self.calls if m == "sendMessage"]

    def add_message(self, text: str, chat_id: str = TEST_CHAT) -> None:
        update_id = 1000 + len(self.updates)
        self.updates.append({"update_id": update_id, "message": {
            "message_id": update_id, "chat": {"id": int(chat_id)}, "text": text}})

    def handles(self, request: httpx.Request) -> bool:
        return request.url.host == "api.telegram.org"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        import json

        method = request.url.path.rsplit("/", 1)[-1]
        assert f"/bot{TEST_TOKEN}/" in request.url.path
        payload = json.loads(request.content or b"{}")
        self.calls.append((method, payload))
        if method in self.fail_methods:
            return httpx.Response(400, json={"ok": False, "description": "Bad Request: test failure"})
        if method == "sendMessage":
            self.next_message_id += 1
            return httpx.Response(200, json={"ok": True, "result": {"message_id": self.next_message_id}})
        if method == "getUpdates":
            offset = payload.get("offset") or 0
            pending = [u for u in self.updates if u["update_id"] >= offset]
            return httpx.Response(200, json={"ok": True, "result": pending})
        return httpx.Response(200, json={"ok": True, "result": True})


@pytest.fixture
def telegram_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", TEST_CHAT)
    for name in ("FRED_API_KEY", "SEC_CONTACT_EMAIL"):
        monkeypatch.delenv(name, raising=False)
