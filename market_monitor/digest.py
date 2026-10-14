"""Daily digest: top events per topic, market snapshot, calendar, watchlist, sources.

`digest_due` makes the digest independent of exact cron timing: any run after the
configured local time sends it once per day (Actions schedules may be late).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from market_monitor.alerts import AlertView, view_from_row
from market_monitor.collectors.sec_edgar import LATEST_KEY, PROFILE_KEY
from market_monitor.config import Config, WatchItem
from market_monitor.storage import SourceHealth, Storage, from_iso, iso
from market_monitor.watchlist import WatchlistMatcher

LAST_DIGEST_KEY = "digest:last_sent_at"


@dataclass
class CompanyBlock:
    watch: WatchItem
    profile: dict = field(default_factory=dict)
    latest_filings: dict = field(default_factory=dict)  # form -> {date, items, url}
    filings: list[AlertView] = field(default_factory=list)  # SEC filings in the window
    news: list[AlertView] = field(default_factory=list)  # news about the company itself
    related: list[tuple[AlertView, list[str]]] = field(default_factory=list)  # peers/suppliers/sector

    @property
    def empty(self) -> bool:
        return not (self.filings or self.news or self.related)


@dataclass
class DigestData:
    now: datetime
    since: datetime
    collected: int = 0
    items_by_topic: dict[str, list[AlertView]] = field(default_factory=dict)
    alerted_ids: set[str] = field(default_factory=set)
    alerts_sent: int = 0
    rate_limited: int = 0
    market: list[str] = field(default_factory=list)
    calendar: list[tuple[str, str]] = field(default_factory=list)
    # None when the watchlist is empty: the block is omitted entirely.
    watchlist: list[CompanyBlock] | None = None
    sources: list[SourceHealth] = field(default_factory=list)


def last_digest_at(storage: Storage) -> datetime | None:
    raw = storage.kv_get(LAST_DIGEST_KEY)
    return from_iso(raw) if raw else None


def digest_due(config: Config, storage: Storage, now: datetime) -> bool:
    local = now.astimezone(config.tz)
    hour, minute = map(int, config.digest_time.split(":"))
    scheduled = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if local < scheduled:
        return False
    last = last_digest_at(storage)
    return last is None or last < scheduled


def mark_digest_sent(storage: Storage, now: datetime) -> None:
    storage.kv_set(LAST_DIGEST_KEY, iso(now))


def market_snapshot(config: Config, storage: Storage) -> list[str]:
    out = []
    raw = storage.kv_get("prices:crypto")
    if raw:
        for symbol, data in json.loads(raw).get("coins", {}).items():
            if data.get("usd") is None:
                continue
            change = data.get("change_24h_pct")
            change_txt = f" ({change:+.1f}%)" if change is not None else ""
            out.append(f"{symbol} ${data['usd']:,.0f}{change_txt}")
    for series, label in config.digest.market_series.items():
        raw = storage.kv_get(f"fred:value:{series}")
        if raw:
            value = json.loads(raw)
            out.append(f"{label} {value['value']:,.2f} ({value['date'][5:]})")
    return out


def upcoming_calendar(config: Config, storage: Storage, now: datetime) -> list[tuple[str, str]]:
    today = now.astimezone(config.tz).date()
    horizon = today + timedelta(days=config.digest.calendar_days)
    seen, out = set(), []
    rows = storage.conn.execute(
        "SELECT meta FROM items WHERE source = 'fred:calendar'").fetchall()
    for row in rows:
        meta = json.loads(row["meta"])
        date, name = meta.get("event_date"), meta.get("release")
        if not date or not name or (date, name) in seen:
            continue
        if today.isoformat() <= date <= horizon.isoformat():
            seen.add((date, name))
            out.append((date, name))
    out.sort()
    return [(f"{d[8:10]}.{d[5:7]}", n) for d, n in out]


def build_digest(config: Config, storage: Storage, now: datetime) -> DigestData:
    cfg = config.digest
    floor = now - timedelta(hours=cfg.max_window_hours)
    last = last_digest_at(storage)
    since = max(last, floor) if last else now - timedelta(hours=24)
    data = DigestData(now=now, since=since)

    rows = storage.items_fetched_since(since)
    data.collected = len(rows)
    counts = storage.alert_counts_since(since)
    data.alerts_sent = counts.get("sent", 0) + counts.get("dry_run", 0)
    data.rate_limited = counts.get("rate_limited", 0)
    data.alerted_ids = {r["item_id"] for r in storage.conn.execute(
        "SELECT item_id FROM alerts WHERE status IN ('sent', 'dry_run') AND created_at >= ?", (iso(since),))}

    topics = [t for t in config.scoring.topics if t not in config.muted_topics]
    data.items_by_topic = {t: [] for t in topics}
    views = [view_from_row(r) for r in rows if "calendar" not in json.loads(r["tags"])]
    for view in views:  # rows come sorted by score
        bucket = data.items_by_topic.get(view.topic)
        if bucket is not None and view.score >= cfg.min_score and len(bucket) < cfg.per_topic:
            bucket.append(view)

    if config.watchlist:
        data.watchlist = company_blocks(config, storage, rows, views)

    data.market = market_snapshot(config, storage)
    data.calendar = upcoming_calendar(config, storage, now)
    data.sources = storage.source_health(now)
    return data


def company_blocks(config: Config, storage: Storage, rows: list, views: list[AlertView]) -> list[CompanyBlock]:
    """Per-company context. Matching is redone here (not only read from stored
    entities) so a company added with /watch today gets today's news too."""
    cfg = config.digest
    matcher = WatchlistMatcher(config.watchlist)
    blocks = {w.ticker: CompanyBlock(watch=w) for w in config.watchlist}
    for ticker, block in blocks.items():
        if raw := storage.kv_get(PROFILE_KEY.format(ticker=ticker)):
            block.profile = json.loads(raw)
        if raw := storage.kv_get(LATEST_KEY.format(ticker=ticker)):
            block.latest_filings = json.loads(raw)
    by_id = {r["id"]: r for r in rows}
    for view in views:  # sorted by score
        row = by_id[view.item_id]
        m = matcher.match(view.title, view.text, json.loads(row["entities"]))
        for ticker in m.direct:
            block = blocks[ticker]
            if view.source == "sec_edgar":
                block.filings.append(view)
            elif len(block.news) < cfg.company_news:
                block.news.append(view)
        for ticker, kinds in m.related.items():
            block = blocks[ticker]
            if view.score >= cfg.company_related_min_score and len(block.related) < cfg.company_related:
                block.related.append((view, kinds))
    return list(blocks.values())
