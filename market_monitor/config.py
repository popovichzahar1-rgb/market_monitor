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
from typing import Literal

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


class GdeltQuery(_Strict):
    name: str
    query: str
    tags: list[str] = Field(default_factory=list)


class GdeltSourceConfig(SourceConfig):
    # GDELT DOC 2.0 API asks for at most one request every 5 seconds.
    min_interval_s: float = 5.5
    timespan: str = "3h"
    max_records: int = 50
    language: str = "english"
    queries: list[GdeltQuery] = Field(default_factory=list)


class SecEdgarSourceConfig(SourceConfig):
    forms: list[str] = Field(default_factory=lambda: ["8-K", "10-Q", "10-K", "6-K", "20-F"])
    max_age_days: int = 7
    tickers_cache_hours: int = 24
    # SEC fair-access policy: max 10 requests/second.
    min_interval_s: float = 0.15


class FredSeries(_Strict):
    id: str
    name: str
    # release: every new observation is an item; move: only when change >= threshold
    mode: Literal["release", "move"] = "release"
    threshold_pct: float | None = None  # relative change, %
    threshold_abs: float | None = None  # absolute change in series units (e.g. yield points)
    tags: list[str] = Field(default_factory=list)


class FredSourceConfig(SourceConfig):
    series: list[FredSeries] = Field(default_factory=list)
    calendar_days_ahead: int = 7
    # Release names (substring, case-insensitive) to put into the event calendar.
    calendar_releases: list[str] = Field(default_factory=list)
    min_interval_s: float = 0.6


class CoinGeckoSourceConfig(SourceConfig):
    coins: dict[str, str] = Field(default_factory=lambda: {"bitcoin": "BTC", "ethereum": "ETH"})
    move_threshold_pct: float = 7.0
    market_move_threshold_pct: float = 5.0
    cooldown_hours: int = 6
    min_interval_s: float = 2.5


class UsgsSourceConfig(SourceConfig):
    feed_url: str = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson"
    min_magnitude: float = 6.5
    alert_levels: list[str] = Field(default_factory=lambda: ["orange", "red"])


class SourcesConfig(_Strict):
    rss: RssSourceConfig = Field(default_factory=RssSourceConfig)
    gdelt: GdeltSourceConfig = Field(default_factory=lambda: GdeltSourceConfig(enabled=False))
    sec_edgar: SecEdgarSourceConfig = Field(default_factory=SecEdgarSourceConfig)
    fred: FredSourceConfig = Field(default_factory=lambda: FredSourceConfig(enabled=False))
    coingecko: CoinGeckoSourceConfig = Field(default_factory=lambda: CoinGeckoSourceConfig(enabled=False))
    usgs: UsgsSourceConfig = Field(default_factory=lambda: UsgsSourceConfig(enabled=False))


class DedupConfig(_Strict):
    window_hours: int = 48
    # rapidfuzz token_sort_ratio (0-100) above which two headlines are the same story
    title_similarity: float = 90.0
    min_title_tokens: int = 4
    min_text_chars_for_hash: int = 120


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
    dedup: DedupConfig = Field(default_factory=DedupConfig)

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
