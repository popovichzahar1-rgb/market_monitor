"""Coinbase Exchange public market data (no key): sharp 24h moves of BTC/ETH.

Docs: https://docs.cdp.coinbase.com/exchange/reference/exchangerestapi_getproductstats
`GET /products/<id>/stats` returns the price 24 h ago (`open`) and now (`last`),
so the 24 h change needs no extra history. Public endpoints allow ~10 req/s; we
make one request per product per run.
An item is produced when |24h change| >= threshold, at most once per cooldown
window (the window id is part of the URL, so dedup suppresses repeats).
Latest prices are stored under the same key as before (`prices:crypto`) for the digest.
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
from market_monitor.collectors.coingecko import PRICES_KEY, window_bucket
from market_monitor.config import CoinbaseSourceConfig
from market_monitor.http import json_or_error
from market_monitor.models import DATA_TAG, Item

log = logging.getLogger(__name__)

API = "https://api.exchange.coinbase.com"
API_HOST = "api.exchange.coinbase.com"
PRICE_PAGES = {"BTC": "bitcoin", "ETH": "ethereum"}


def parse_stats(payload: object) -> tuple[float, float]:
    """(last price, change over 24 h in %)."""
    if not isinstance(payload, dict):
        raise CollectorError("unexpected Coinbase payload")
    try:
        last, open_ = float(payload["last"]), float(payload["open"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CollectorError(f"incomplete Coinbase stats: {exc}") from None
    if open_ <= 0:
        raise CollectorError("invalid Coinbase open price")
    return last, (last - open_) / open_ * 100


def move_item(symbol: str, price: float, change: float, cfg: CoinbaseSourceConfig,
              now: datetime) -> Item | None:
    if abs(change) < cfg.move_threshold_pct:
        return None
    direction = "up" if change > 0 else "down"
    page = PRICE_PAGES.get(symbol, symbol.lower())
    return Item(
        source="coinbase",
        published_at=now,
        title=f"{symbol} {direction} {change:+.1f}% in 24h to ${price:,.0f}",
        url=f"https://www.coinbase.com/price/{page}?move={window_bucket(now, cfg.cooldown_hours)}",
        tags=["crypto", "price_move", DATA_TAG],
        entities=[symbol],
        meta={"price_usd": price, "change_24h_pct": round(change, 2)},
    )


@register
class CoinbaseCollector(Collector):
    name = "coinbase"
    source_config: CoinbaseSourceConfig

    def collect(self, ctx: CollectContext) -> CollectResult:
        cfg = self.source_config
        ctx.http.set_host_interval(API_HOST, cfg.min_interval_s)
        result = CollectResult()
        snapshot: dict[str, dict] = {}
        for symbol, product in cfg.products.items():
            try:
                price, change = parse_stats(json_or_error(
                    ctx.http.get(f"{API}/products/{product}/stats")))
            except Exception as exc:
                log.warning("coinbase %s failed: %s", product, exc)
                result.errors.append(f"{product}: {exc}")
                continue
            snapshot[symbol] = {"usd": price, "change_24h_pct": round(change, 2)}
            if item := move_item(symbol, price, change, cfg, ctx.now):
                result.items.append(item)
        if snapshot:
            ctx.state.kv_set(PRICES_KEY, json.dumps(
                {"at": ctx.now.isoformat(), "source": "Coinbase", "coins": snapshot}))
        elif cfg.products:
            raise CollectorError("; ".join(result.errors[:2]))
        return result
