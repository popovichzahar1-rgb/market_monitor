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
