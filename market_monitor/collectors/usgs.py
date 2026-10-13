"""USGS earthquake feed (GeoJSON, public domain): strong quakes that can hit
supply chains (semiconductors in Taiwan/Japan, ports, energy infrastructure).

Feed docs: https://earthquake.usgs.gov/earthquakes/feed/v1.0/geojson.php
"""

from __future__ import annotations

from datetime import datetime, timezone

from market_monitor.collectors.base import (
    CollectContext,
    CollectResult,
    Collector,
    CollectorError,
    register,
)
from market_monitor.config import UsgsSourceConfig
from market_monitor.http import json_or_error
from market_monitor.models import DATA_TAG, Item


def parse_quakes(payload: dict, cfg: UsgsSourceConfig, now: datetime) -> list[Item]:
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
        raise CollectorError("unexpected USGS payload")
    items = []
    alert_levels = {a.lower() for a in cfg.alert_levels}
    for feature in payload.get("features") or []:
        props = feature.get("properties") or {}
        mag, alert = props.get("mag"), (props.get("alert") or "").lower()
        if mag is None:
            continue
        if mag < cfg.min_magnitude and alert not in alert_levels:
            continue
        published = datetime.fromtimestamp(props.get("time", 0) / 1000, tz=timezone.utc)
        tsunami = bool(props.get("tsunami"))
        items.append(
            Item(
                source="usgs",
                published_at=min(published, now),
                title=f"Earthquake: {props.get('title', f'M {mag}')}",
                text=(f"Magnitude {mag}. PAGER alert level: {alert or 'none'}. "
                      f"Tsunami flag: {'yes' if tsunami else 'no'}."),
                url=props.get("url") or f"https://earthquake.usgs.gov/earthquakes/eventpage/{feature.get('id')}",
                tags=["disaster", "earthquake", DATA_TAG],
                meta={"magnitude": mag, "alert": alert or None, "tsunami": tsunami,
                      "place": props.get("place")},
            )
        )
    return items


@register
class UsgsCollector(Collector):
    name = "usgs"
    source_config: UsgsSourceConfig

    def collect(self, ctx: CollectContext) -> CollectResult:
        resp = ctx.http.get(self.source_config.feed_url)
        return CollectResult(items=parse_quakes(json_or_error(resp), self.source_config, ctx.now))
