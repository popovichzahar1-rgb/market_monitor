"""Configuration: static defaults from config.yaml, secrets from the environment.

Runtime changes made through bot commands (/watch, /threshold, ...) are stored as
overrides in the database and layered on top of this file (see stage 4).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Strict(BaseModel):
    # Typos in config.yaml should fail loudly instead of being silently ignored.
    model_config = ConfigDict(extra="forbid")


class HttpConfig(_Strict):
    timeout_s: float = 20.0
    retries: int = 3
    backoff_s: float = 2.0
    max_retry_after_s: float = 60.0
    per_host_min_interval_s: float = 1.0
    respect_robots_txt: bool = True
    user_agent: str = "MarketMonitor/0.1 (personal non-commercial news monitor)"


class FeedConfig(_Strict):
    name: str
    url: str
    tags: list[str] = Field(default_factory=list)
    enabled: bool = True


class SourceConfig(_Strict):
    enabled: bool = True


class RssSourceConfig(SourceConfig):
    max_items_per_feed: int = 40
    max_age_hours: int = 48
    text_max_chars: int = 1500
    feeds: list[FeedConfig] = Field(default_factory=list)


class SourcesConfig(_Strict):
    rss: RssSourceConfig = Field(default_factory=RssSourceConfig)


class WatchItem(_Strict):
    ticker: str
    name: str = ""
    aliases: list[str] = Field(default_factory=list)
    cik: str | None = None
    sector: str = ""
    peers: list[str] = Field(default_factory=list)
    suppliers: list[str] = Field(default_factory=list)

    @field_validator("ticker")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.strip().upper()


class AssetsConfig(_Strict):
    equities_markets: list[str] = Field(default_factory=lambda: ["US", "EU"])
    crypto: list[str] = Field(default_factory=lambda: ["BTC", "ETH"])
    crypto_market_wide: bool = True


class StorageConfig(_Strict):
    path: str = "data/state.db"
    retention_days: int = 30


class Config(_Strict):
    timezone: str = "Europe/Bratislava"
    digest_time: str = "20:00"
    regions: list[str] = Field(default_factory=lambda: ["global"])
    assets: AssetsConfig = Field(default_factory=AssetsConfig)
    watchlist: list[WatchItem] = Field(default_factory=list)
    http: HttpConfig = Field(default_factory=HttpConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    sources: SourcesConfig = Field(default_factory=SourcesConfig)

    @field_validator("timezone")
    @classmethod
    def _valid_tz(cls, value: str) -> str:
        ZoneInfo(value)  # raises on unknown zone
        return value

    @field_validator("digest_time")
    @classmethod
    def _valid_hhmm(cls, value: str) -> str:
        return parse_hhmm(value)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def parse_hhmm(value: str) -> str:
    hours, _, minutes = value.strip().partition(":")
    h, m = int(hours), int(minutes or 0)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"invalid time {value!r}, expected HH:MM")
    return f"{h:02d}:{m:02d}"


def load_config(path: str | Path) -> Config:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Copy config.example.yaml to config.yaml and adjust it."
        )
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Config.model_validate(data)


@dataclass(frozen=True)
class Secrets:
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    fred_api_key: str = ""
    sec_contact_email: str = ""
    state_encryption_key: str = ""

    @classmethod
    def from_env(cls) -> "Secrets":
        return cls(
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
            fred_api_key=os.environ.get("FRED_API_KEY", "").strip(),
            sec_contact_email=os.environ.get("SEC_CONTACT_EMAIL", "").strip(),
            state_encryption_key=os.environ.get("STATE_ENCRYPTION_KEY", "").strip(),
        )

    def values(self) -> list[str]:
        """All secret values, used to redact logs."""
        return [v for v in vars(self).values() if v]
