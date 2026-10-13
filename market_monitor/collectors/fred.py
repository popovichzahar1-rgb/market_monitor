"""FRED (St. Louis Fed): macro data releases and the upcoming release calendar.

Docs: https://fred.stlouisfed.org/docs/api/fred/  Requires a free FRED_API_KEY.
Series in `release` mode produce an item for every new observation (CPI, payrolls);
`move` mode only when the change exceeds a threshold (daily oil, yields).
The first time a series is seen we only remember its latest date (no backfill flood).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from market_monitor.collectors.base import (
    CollectContext,
    CollectResult,
    Collector,
    CollectorError,
    register,
)
from market_monitor.config import FredSeries, FredSourceConfig
from market_monitor.http import json_or_error
from market_monitor.models import DATA_TAG, Item

log = logging.getLogger(__name__)

API = "https://api.stlouisfed.org/fred"
API_HOST = "api.stlouisfed.org"


def valid_observations(payload: dict) -> list[tuple[str, float]]:
    """(date, value) pairs, newest first, skipping FRED's '.' placeholders."""
    out = []
    for obs in payload.get("observations") or []:
        try:
            out.append((obs["date"], float(obs["value"])))
        except (KeyError, ValueError):
            continue
    return out


def format_value(value: float) -> str:
    return f"{value:,.2f}" if abs(value) < 1e5 else f"{value:,.0f}"


def observation_item(series: FredSeries, observations: list[tuple[str, float]],
                     last_seen: str | None, now: datetime) -> Item | None:
    if not observations:
        return None
    date, value = observations[0]
    if last_seen is None or date <= last_seen:
        return None  # first sighting (baseline) or nothing new
    prev = observations[1][1] if len(observations) > 1 else None
    change_abs = value - prev if prev is not None else None
    change_pct = (change_abs / abs(prev) * 100) if prev not in (None, 0) else None
    if series.mode == "move":
        hit_pct = series.threshold_pct is not None and change_pct is not None \
            and abs(change_pct) >= series.threshold_pct
        hit_abs = series.threshold_abs is not None and change_abs is not None \
            and abs(change_abs) >= series.threshold_abs
        if not (hit_pct or hit_abs):
            return None
    title = f"{series.name}: {format_value(value)} ({date})"
    if change_abs is not None:
        title += f", change {change_abs:+.2f}"
        if change_pct is not None:
            title += f" ({change_pct:+.1f}%)"
    return Item(
        source="fred",
        published_at=now,
        title=title,
        text=f"FRED series {series.id} new observation for {date}: {value}"
             + (f" (previous {prev})." if prev is not None else "."),
        url=f"https://fred.stlouisfed.org/series/{series.id}?obs={date}",
        tags=["macro", DATA_TAG, *series.tags],
        meta={"series": series.id, "date": date, "value": value, "previous": prev,
              "change_abs": change_abs, "change_pct": change_pct, "mode": series.mode},
    )


def calendar_items(payload: dict, cfg: FredSourceConfig, now: datetime) -> list[Item]:
    wanted = [r.lower() for r in cfg.calendar_releases]
    items = []
    for rel in payload.get("release_dates") or []:
        name, date, rid = rel.get("release_name", ""), rel.get("date", ""), rel.get("release_id")
        if not name or not date or not any(w in name.lower() for w in wanted):
            continue
        items.append(
            Item(
                source="fred:calendar",
                published_at=now,
                title=f"Upcoming US release: {name} on {date}",
                url=f"https://fred.stlouisfed.org/releases/calendar?rid={rid}&date={date}",
                tags=["macro", "calendar", "US", DATA_TAG],
                meta={"event_date": date, "release_id": rid, "release": name},
            )
        )
    return items


@register
class FredCollector(Collector):
    name = "fred"
    source_config: FredSourceConfig

    def collect(self, ctx: CollectContext) -> CollectResult:
        key = ctx.secrets.fred_api_key
        if not key:
            raise CollectorError("FRED_API_KEY is not set")
        cfg = self.source_config
        ctx.http.set_host_interval(API_HOST, cfg.min_interval_s)
        result = CollectResult()
        base = {"api_key": key, "file_type": "json"}
        attempts = 0
        for series in cfg.series:
            attempts += 1
            try:
                resp = ctx.http.get(f"{API}/series/observations", params={
                    **base, "series_id": series.id, "sort_order": "desc", "limit": "6"})
                observations = valid_observations(json_or_error(resp))
                state_key = f"fred:last:{series.id}"
                item = observation_item(series, observations, ctx.state.kv_get(state_key), ctx.now)
                if observations:
                    ctx.state.kv_set(state_key, observations[0][0])
                    ctx.state.kv_set(f"fred:value:{series.id}", json.dumps(
                        {"name": series.name, "date": observations[0][0], "value": observations[0][1]}))
                if item:
                    result.items.append(item)
            except Exception as exc:
                log.warning("fred %s failed: %s", series.id, exc)
                result.errors.append(f"{series.id}: {exc}")
        if cfg.calendar_releases:
            attempts += 1
            try:
                start = ctx.now.date()
                resp = ctx.http.get(f"{API}/releases/dates", params={
                    **base,
                    "realtime_start": start.isoformat(),
                    "realtime_end": (start + timedelta(days=cfg.calendar_days_ahead)).isoformat(),
                    "include_release_dates_with_no_data": "true",
                    "sort_order": "asc", "limit": "1000"})
                result.items.extend(calendar_items(json_or_error(resp), cfg, ctx.now))
            except Exception as exc:
                log.warning("fred calendar failed: %s", exc)
                result.errors.append(f"calendar: {exc}")
        if attempts and len(result.errors) == attempts:
            raise CollectorError("all FRED requests failed: " + "; ".join(result.errors[:2]))
        return result
