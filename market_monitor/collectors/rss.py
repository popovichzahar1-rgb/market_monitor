"""Generic RSS/Atom collector for news outlets, central banks and crypto media.

Feeds are listed in config.yaml under `sources.rss.feeds`. Uses conditional GET
(ETag / Last-Modified) so unchanged feeds cost almost nothing.
"""

from __future__ import annotations

import calendar
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import feedparser

from market_monitor.collectors.base import (
    CollectContext,
    CollectResult,
    Collector,
    CollectorError,
    register,
)
from market_monitor.config import FeedConfig, RssSourceConfig
from market_monitor.models import Item
from market_monitor.textutil import clean_html, truncate

log = logging.getLogger(__name__)


def _entry_time(entry: dict) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = entry.get(key)
        if parsed:
            return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)
    return None


def _entry_text(entry: dict) -> str:
    if entry.get("summary"):
        return clean_html(entry["summary"])
    for content in entry.get("content") or []:
        if content.get("value"):
            return clean_html(content["value"])
    return ""


def parse_feed(
    content: bytes, feed: FeedConfig, cfg: RssSourceConfig, now: datetime
) -> list[Item]:
    parsed = feedparser.parse(content)
    if parsed.bozo and not parsed.entries:
        raise CollectorError(f"unparseable feed: {parsed.get('bozo_exception')}")
    cutoff = now - timedelta(hours=cfg.max_age_hours)
    items: list[Item] = []
    for entry in parsed.entries[: cfg.max_items_per_feed]:
        link, title = entry.get("link"), clean_html(entry.get("title"))
        if not link or not title:
            continue
        published = _entry_time(entry) or now
        if published < cutoff:
            continue
        # Feeds sometimes carry future dates (timezone bugs); clamp to "now".
        published = min(published, now)
        items.append(
            Item(
                source=f"rss:{feed.name}",
                published_at=published,
                title=title,
                text=truncate(_entry_text(entry), cfg.text_max_chars),
                url=link,
                tags=list(feed.tags),
            )
        )
    return items


@register
class RssCollector(Collector):
    name = "rss"
    source_config: RssSourceConfig

    def collect(self, ctx: CollectContext) -> CollectResult:
        result = CollectResult()
        feeds = [f for f in self.source_config.feeds if f.enabled]
        budget = ctx.http.remaining_budget()

        def fetch(feed: FeedConfig) -> list[Item]:
            ctx.http.set_budget(budget)  # inherit the collector's budget in this thread
            try:
                return self._collect_feed(ctx, feed)
            finally:
                ctx.http.set_budget(None)

        failed = 0
        # Feeds live on different hosts: fetch them concurrently (per-host limits still apply).
        with ThreadPoolExecutor(max_workers=max(1, self.source_config.workers)) as pool:
            futures = [pool.submit(fetch, feed) for feed in feeds]
            for feed, future in zip(feeds, futures):
                try:
                    result.items.extend(future.result())
                except Exception as exc:  # one broken feed must not stop the others
                    failed += 1
                    log.warning("rss feed %s failed: %s", feed.name, exc)
                    result.errors.append(f"{feed.name}: {exc}")
        if feeds and failed == len(feeds):
            raise CollectorError(f"all {failed} feeds failed: " + "; ".join(result.errors[:3]))
        return result

    def _collect_feed(self, ctx: CollectContext, feed: FeedConfig) -> list[Item]:
        etag_key, modified_key = f"rss:etag:{feed.url}", f"rss:modified:{feed.url}"
        headers = {}
        if etag := ctx.state.kv_get(etag_key):
            headers["If-None-Match"] = etag
        if modified := ctx.state.kv_get(modified_key):
            headers["If-Modified-Since"] = modified
        resp = ctx.http.get(feed.url, headers=headers)
        if resp.status_code == 304:
            log.debug("feed %s not modified", feed.name)
            return []
        items = parse_feed(resp.content, feed, self.source_config, ctx.now)
        # Only remember validators after a successful parse.
        if etag := resp.headers.get("ETag"):
            ctx.state.kv_set(etag_key, etag)
        if modified := resp.headers.get("Last-Modified"):
            ctx.state.kv_set(modified_key, modified)
        log.info("feed %s: %d items", feed.name, len(items))
        return items
