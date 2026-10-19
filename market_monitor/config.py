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
    max_retry_after_s: float = 20.0
    # Max seconds one collector may spend (requests + waits) per run.
    source_budget_s: float = 90.0
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
    workers: int = 8
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
    # The release calendar endpoint is slow (20-30 s) and changes rarely.
    calendar_refresh_hours: int = 12
    min_interval_s: float = 0.6


class CoinGeckoSourceConfig(SourceConfig):
    coins: dict[str, str] = Field(default_factory=lambda: {"bitcoin": "BTC", "ethereum": "ETH"})
    move_threshold_pct: float = 7.0
    market_move_threshold_pct: float = 5.0
    cooldown_hours: int = 6
    min_interval_s: float = 2.5


class CoinbaseSourceConfig(SourceConfig):
    # symbol -> Coinbase Exchange product id
    products: dict[str, str] = Field(default_factory=lambda: {"BTC": "BTC-USD", "ETH": "ETH-USD"})
    move_threshold_pct: float = 7.0
    cooldown_hours: int = 6
    min_interval_s: float = 0.4


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
    coinbase: CoinbaseSourceConfig = Field(default_factory=lambda: CoinbaseSourceConfig(enabled=False))
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
    # Phrases that make news "related via sector" (e.g. [oil price, refining]).
    sector_keywords: list[str] = Field(default_factory=list)

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


class ScoringRule(_Strict):
    """Weighted keyword rule. Phrases are case-insensitive whole words; `*` is a suffix
    wildcard ("sanction*"). `any` must match; if `also_any` is set, one of those must
    match too; if `tags_any` is set, the item must carry one of those tags."""

    name: str
    weight: float
    any: list[str]
    also_any: list[str] = Field(default_factory=list)
    tags_any: list[str] = Field(default_factory=list)
    title_only: bool = False
    topic: str | None = None
    # Marks an "urgent" category: such items may be sent as alerts (see alerts config).
    alert: bool = False


class TopicConfig(_Strict):
    label: str
    tags: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)


class SentimentConfig(_Strict):
    # auto: ONNX FinBERT if the model is present, otherwise the built-in lexicon.
    backend: Literal["auto", "onnx", "lexicon", "none"] = "auto"
    model_dir: str = "models/finbert-onnx"
    max_length: int = 96
    batch_size: int = 16
    threads: int = 2
    # Up to `weight` points for strong (positive or negative) tone, plus
    # `negative_extra` when clearly negative: shocks tend to move markets more.
    weight: float = 10.0
    negative_extra: float = 5.0


class DataScoringConfig(_Strict):
    price_move_base: float = 55.0
    price_move_per_pct: float = 4.0  # per % beyond the collector threshold
    fred_release_base: float = 35.0
    fred_major_series: list[str] = Field(
        default_factory=lambda: ["CPIAUCSL", "CPILFESL", "PCEPILFE", "PAYEMS"])
    fred_major_bonus: float = 25.0
    fred_move_base: float = 50.0
    calendar_base: float = 10.0
    quake_base: float = 35.0
    quake_per_magnitude: float = 20.0  # per magnitude unit above 6.5
    quake_alert_bonus: dict[str, float] = Field(
        default_factory=lambda: {"orange": 15.0, "red": 30.0})
    quake_tsunami_bonus: float = 10.0
    sec_8k_base: float = 50.0
    sec_periodic_base: float = 35.0
    sec_major_8k_items: list[str] = Field(
        default_factory=lambda: ["1.03", "1.05", "2.02", "2.06", "3.01", "4.02", "5.01"])
    sec_major_bonus: float = 25.0


class ScoringConfig(_Strict):
    # Only items whose rule score reaches this are sent to the sentiment model.
    prefilter_min: float = 25.0
    rules: list[ScoringRule] = Field(default_factory=list)
    topics: dict[str, TopicConfig] = Field(default_factory=dict)
    default_topic: str = "other"
    # Points by source; key matches the source exactly or as a prefix ("gdelt" -> "gdelt:*").
    source_weights: dict[str, float] = Field(default_factory=dict)
    mentions_bonus_per_source: float = 6.0
    mentions_bonus_max: float = 18.0
    asset_keywords: dict[str, list[str]] = Field(default_factory=dict)
    asset_bonus: float = 10.0
    region_keywords: dict[str, list[str]] = Field(default_factory=dict)
    region_bonus: float = 5.0
    watchlist_bonus: float = 25.0
    # Peer / supplier / sector news of a watchlist company (context, no alert flag).
    watchlist_related_bonus: float = 8.0
    data: DataScoringConfig = Field(default_factory=DataScoringConfig)
    sentiment: SentimentConfig = Field(default_factory=SentimentConfig)


class QuietHours(_Strict):
    start: str = "23:00"
    end: str = "07:00"

    @field_validator("start", "end")
    @classmethod
    def _hhmm(cls, value: str) -> str:
        return parse_hhmm(value)


class AlertsConfig(_Strict):
    enabled: bool = True
    # General threshold for any item.
    threshold: float = 80.0
    # Items in an urgent category (a rule with `alert: true` or a data flag below).
    urgent_threshold: float = 65.0
    urgent_data_flags: list[str] = Field(
        default_factory=lambda: ["crypto_price_move", "major_quake", "sec_8k"])
    # Items about watchlist companies, BTC or ETH.
    priority_threshold: float = 70.0
    priority_flags: list[str] = Field(default_factory=lambda: ["watchlist", "asset:BTC", "asset:ETH"])
    # flag -> minimum score: alert even below the thresholds above.
    always: dict[str, float] = Field(
        default_factory=lambda: {"central_bank_decision": 55.0, "sec_8k": 45.0})
    max_per_hour: int = 5
    quiet_hours: QuietHours | None = Field(default_factory=QuietHours)
    # Never alert on items older than this (feed backfills, late discoveries).
    max_age_hours: float = 12.0
    # Max alerts listed in the morning batch message.
    quiet_batch_max: int = 15


class DigestConfig(_Strict):
    min_score: float = 30.0
    per_topic: int = 5
    # Watchlist block, per company.
    company_news: int = 3
    company_related: int = 3
    company_related_min_score: float = 30.0
    # Window: since the previous digest, but at most this many hours back.
    max_window_hours: int = 36
    calendar_days: int = 7
    market_series: dict[str, str] = Field(default_factory=lambda: {
        "DCOILBRENTEU": "Brent", "DHHNGSP": "Газ HH", "DGS10": "UST 10Y", "VIXCLS": "VIX"})


class TelegramConfig(_Strict):
    api_base: str = "https://api.telegram.org"
    max_message_chars: int = 4000


class Config(_Strict):
    # Collectors run in parallel threads (per-host rate limits still apply).
    collect_workers: int = 6
    timezone: str = "Europe/Bratislava"
    digest_time: str = "20:00"
    regions: list[str] = Field(default_factory=lambda: ["global"])
    assets: AssetsConfig = Field(default_factory=AssetsConfig)
    watchlist: list[WatchItem] = Field(default_factory=list)
    http: HttpConfig = Field(default_factory=HttpConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    dedup: DedupConfig = Field(default_factory=DedupConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    digest: DigestConfig = Field(default_factory=DigestConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    # Topics hidden from alerts and digest (toggled with /topics).
    muted_topics: list[str] = Field(default_factory=list)

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
