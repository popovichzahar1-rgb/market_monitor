"""CoinGecko public API (no key): sharp 24h moves of BTC/ETH and the total crypto market.

Docs: https://docs.coingecko.com/reference/introduction  The keyless public tier
allows a handful of calls per minute; we make two per run.
An item is produced when |24h change| >= threshold, at most once per cooldown window.
Latest prices are also stored in the state store for the daily digest.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from market_monitor.collectors.base import (
    CollectContext,
    CollectResult,
    Collector,
    CollectorError,
    register,
)
from market_monitor.config import CoinGeckoSourceConfig
from market_monitor.http import json_or_error
from market_monitor.models import DATA_TAG, Item

log = logging.getLogger(__name__)

API = "https://api.coingecko.com/api/v3"
API_HOST = "api.coingecko.com"
PRICES_KEY = "prices:crypto"


def window_bucket(now: datetime, hours: int) -> str:
    """Identifier of the cooldown window: same window -> same URL -> deduplicated."""
    return str(int(now.timestamp()) // (hours * 3600))


def price_items(payload: dict, cfg: CoinGeckoSourceConfig, now: datetime) -> list[Item]:
    items = []
    for coin_id, symbol in cfg.coins.items():
        data = payload.get(coin_id) or {}
        price, change = data.get("usd"), data.get("usd_24h_change")
        if price is None or change is None or abs(change) < cfg.move_threshold_pct:
            continue
        direction = "up" if change > 0 else "down"
        items.append(
            Item(
                source="coingecko",
                published_at=now,
                title=f"{symbol} {direction} {change:+.1f}% in 24h to ${price:,.0f}",
                url=f"https://www.coingecko.com/en/coins/{coin_id}?move={window_bucket(now, cfg.cooldown_hours)}",
                tags=["crypto", "price_move", DATA_TAG],
                entities=[symbol],
                meta={"price_usd": price, "change_24h_pct": round(change, 2)},
            )
        )
    return items


def market_item(payload: dict, cfg: CoinGeckoSourceConfig, now: datetime) -> Item | None:
    data = payload.get("data") or {}
    change = data.get("market_cap_change_percentage_24h_usd")
    total = (data.get("total_market_cap") or {}).get("usd")
    if change is None or abs(change) < cfg.market_move_threshold_pct:
        return None
    total_txt = f" to ${total / 1e12:.2f}T" if total else ""
    return Item(
        source="coingecko",
        published_at=now,
        title=f"Total crypto market cap {change:+.1f}% in 24h{total_txt}",
        url=f"https://www.coingecko.com/en/global-charts?move={window_bucket(now, cfg.cooldown_hours)}",
        tags=["crypto", "price_move", "market_wide", DATA_TAG],
        meta={"market_cap_usd": total, "change_24h_pct": round(change, 2)},
    )


@register
class CoinGeckoCollector(Collector):
    name = "coingecko"
    source_config: CoinGeckoSourceConfig

    def collect(self, ctx: CollectContext) -> CollectResult:
        cfg = self.source_config
        ctx.http.set_host_interval(API_HOST, cfg.min_interval_s)
        result = CollectResult()
        try:
            prices = json_or_error(ctx.http.get(f"{API}/simple/price", params={
                "ids": ",".join(cfg.coins), "vs_currencies": "usd",
                "include_24hr_change": "true"}))
            result.items.extend(price_items(prices, cfg, ctx.now))
            snapshot = {
                cfg.coins[c]: {"usd": v.get("usd"), "change_24h_pct": v.get("usd_24h_change")}
                for c, v in prices.items() if c in cfg.coins
            }
            ctx.state.kv_set(PRICES_KEY, json.dumps({"at": ctx.now.isoformat(), "coins": snapshot}))
        except Exception as exc:
            result.errors.append(f"prices: {exc}")
        try:
            item = market_item(json_or_error(ctx.http.get(f"{API}/global")), cfg, ctx.now)
            if item:
                result.items.append(item)
        except Exception as exc:
            result.errors.append(f"global: {exc}")
        if len(result.errors) == 2:
            raise CollectorError("; ".join(result.errors))
        return result
