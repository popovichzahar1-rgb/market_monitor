"""GDELT DOC 2.0 API: worldwide news search (geopolitics, disasters, macro, crypto).

Docs: https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/
Free, no key. Rate limit: one request per 5 seconds, so keep `queries` short and
combine terms with OR inside a query instead of adding more queries.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from market_monitor.collectors.base import (
    CollectContext,
    CollectResult,
    Collector,
    CollectorError,
    register,
)
from market_monitor.config import GdeltQuery, GdeltSourceConfig
from market_monitor.http import json_or_error
from market_monitor.models import Item

log = logging.getLogger(__name__)

API_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
API_HOST = "api.gdeltproject.org"


def parse_seendate(value: str) -> datetime:
    return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


def parse_articles(payload: object, query: GdeltQuery, now: datetime) -> list[Item]:
    if not isinstance(payload, dict):
        raise CollectorError("unexpected GDELT payload")
    items = []
    for art in payload.get("articles") or []:
        url, title = art.get("url"), art.get("title")
        if not url or not title:
            continue
        try:
            published = min(parse_seendate(art.get("seendate", "")), now)
        except ValueError:
            published = now
        country = art.get("sourcecountry") or ""
        items.append(
            Item(
                source=f"gdelt:{query.name}",
                published_at=published,
                title=title,
                text="",
                url=url,
                tags=list(query.tags),
                meta={"domain": art.get("domain", ""), "source_country": country},
            )
        )
    return items


@register
class GdeltCollector(Collector):
    name = "gdelt"
    source_config: GdeltSourceConfig

    def collect(self, ctx: CollectContext) -> CollectResult:
        cfg = self.source_config
        ctx.http.set_host_interval(API_HOST, cfg.min_interval_s)
        result = CollectResult()
        for query in cfg.queries:
            full_query = query.query
            if cfg.language:
                full_query += f" sourcelang:{cfg.language}"
            params = {
                "query": full_query,
                "mode": "ArtList",
                "format": "json",
                "maxrecords": str(cfg.max_records),
                "timespan": cfg.timespan,
                "sort": "DateDesc",
            }
            try:
                resp = ctx.http.get(API_URL, params=params)
                items = parse_articles(json_or_error(resp), query, ctx.now)
                log.info("gdelt %s: %d articles", query.name, len(items))
                result.items.extend(items)
            except Exception as exc:
                log.warning("gdelt query %s failed: %s", query.name, exc)
                result.errors.append(f"{query.name}: {exc}")
        if cfg.queries and len(result.errors) == len(cfg.queries):
            raise CollectorError("all GDELT queries failed: " + "; ".join(result.errors[:2]))
        return result
