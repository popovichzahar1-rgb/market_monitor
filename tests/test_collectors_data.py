"""GDELT, SEC EDGAR, FRED, CoinGecko and USGS collectors against fixtures."""

import json
from datetime import timedelta

import httpx
import pytest

from conftest import fixture_bytes, route
from market_monitor.collectors import CollectContext, CollectorError
from market_monitor.collectors.coingecko import CoinGeckoCollector
from market_monitor.collectors.fred import FredCollector
from market_monitor.collectors.gdelt import GdeltCollector
from market_monitor.collectors.sec_edgar import SecEdgarCollector
from market_monitor.collectors.usgs import UsgsCollector
from market_monitor.config import Config, Secrets


def make_ctx(make_http, handler, storage, now, secrets=None, **config):
    cfg = Config.model_validate(config)
    return CollectContext(http=make_http(handler), config=cfg,
                          secrets=secrets or Secrets(sec_contact_email="me@example.com",
                                                     fred_api_key="k"),
                          state=storage, now=now), cfg


# ---- GDELT --------------------------------------------------------------------

GDELT_CFG = {"sources": {"gdelt": {"enabled": True, "queries": [
    {"name": "energy", "query": "(oil OR gas) (sanctions OR tanker)", "tags": ["geopolitics", "energy"]}]}}}


def test_gdelt_parses_articles_and_sends_query(make_http, storage, now):
    seen = {}

    def api(request):
        seen.update(request.url.params)
        return httpx.Response(200, content=fixture_bytes("gdelt_artlist.json"))

    ctx, cfg = make_ctx(make_http, route({"https://api.gdeltproject.org/api/v2/doc/doc": api}),
                        storage, now, **GDELT_CFG)
    result = GdeltCollector(cfg.sources.gdelt).collect(ctx)
    assert len(result.items) == 3
    assert result.items[0].source == "gdelt:energy"
    assert result.items[0].tags == ["geopolitics", "energy"]
    assert result.items[0].published_at.isoformat() == "2026-10-13T11:15:00+00:00"
    assert seen["query"].endswith("sourcelang:english") and seen["mode"] == "ArtList"


def test_gdelt_rate_limit_text_page_is_an_error(make_http, storage, now):
    handler = route({"https://api.gdeltproject.org/api/v2/doc/doc":
                     lambda r: httpx.Response(200, text="Please limit requests to one every 5 seconds")})
    ctx, cfg = make_ctx(make_http, handler, storage, now, **GDELT_CFG)
    with pytest.raises(CollectorError, match="invalid JSON"):
        GdeltCollector(cfg.sources.gdelt).collect(ctx)


# ---- SEC EDGAR ----------------------------------------------------------------

SEC_ROUTES = {
    "https://www.sec.gov/files/company_tickers.json": fixture_bytes("sec_company_tickers.json"),
    "https://data.sec.gov/submissions/CIK0000320193.json": fixture_bytes("sec_submissions_aapl.json"),
}


def test_sec_recent_filings_for_watchlist(make_http, storage, now):
    agents = []

    def wrapped(request):
        agents.append(request.headers["User-Agent"])
        return route(SEC_ROUTES)(request)

    ctx, cfg = make_ctx(make_http, wrapped, storage, now,
                        watchlist=[{"ticker": "aapl"}, {"ticker": "SAP"}])
    result = SecEdgarCollector(cfg.sources.sec_edgar).collect(ctx)
    titles = [i.title for i in result.items]
    # form 4 is not in `forms`, the 10-K is older than max_age_days, SAP has no SEC listing
    assert titles == [
        "AAPL: SEC 8-K filed — 2.02 results of operations (earnings)",
        "AAPL: SEC 10-Q filed",
    ]
    item = result.items[0]
    assert item.url == "https://www.sec.gov/Archives/edgar/data/320193/000032019326000101/aapl-20261012.htm"
    assert item.entities == ["AAPL"] and "form:8-K" in item.tags
    assert result.errors == []
    assert all("me@example.com" in ua for ua in agents)  # SEC fair-access User-Agent


def test_sec_ticker_map_is_cached(make_http, storage, now):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return route(SEC_ROUTES)(request)

    ctx, cfg = make_ctx(make_http, handler, storage, now, watchlist=[{"ticker": "AAPL"}])
    collector = SecEdgarCollector(cfg.sources.sec_edgar)
    collector.collect(ctx)
    collector.collect(ctx)
    assert calls.count("/files/company_tickers.json") == 1


def test_sec_requires_contact_email(make_http, storage, now):
    ctx, cfg = make_ctx(make_http, route(SEC_ROUTES), storage, now, secrets=Secrets(),
                        watchlist=[{"ticker": "AAPL"}])
    with pytest.raises(CollectorError, match="SEC_CONTACT_EMAIL"):
        SecEdgarCollector(cfg.sources.sec_edgar).collect(ctx)


def test_sec_empty_watchlist_is_noop(make_http, storage, now):
    ctx, cfg = make_ctx(make_http, route({}), storage, now)
    assert SecEdgarCollector(cfg.sources.sec_edgar).collect(ctx).items == []


# ---- FRED ---------------------------------------------------------------------

FRED_CFG = {"sources": {"fred": {
    "enabled": True,
    "series": [
        {"id": "CPIAUCSL", "name": "US CPI", "mode": "release", "tags": ["inflation", "US"]},
        {"id": "DCOILBRENTEU", "name": "Brent crude", "mode": "move", "threshold_pct": 5,
         "tags": ["commodities", "energy"]},
    ],
    "calendar_releases": ["Consumer Price Index", "Employment Situation"],
}}}


def fred_handler(request):
    path = request.url.path
    if path.endswith("/series/observations"):
        name = {"CPIAUCSL": "fred_cpi.json", "DCOILBRENTEU": "fred_brent.json"}[request.url.params["series_id"]]
        return httpx.Response(200, content=fixture_bytes(name))
    if path.endswith("/releases/dates"):
        return httpx.Response(200, content=fixture_bytes("fred_release_dates.json"))
    return httpx.Response(404)


def test_fred_first_run_is_baseline_then_new_observations_alert(make_http, storage, now):
    ctx, cfg = make_ctx(make_http, fred_handler, storage, now, **FRED_CFG)
    collector = FredCollector(cfg.sources.fred)
    first = collector.collect(ctx)
    # Baseline: no series items, but the calendar is reported.
    assert [i.source for i in first.items] == ["fred:calendar", "fred:calendar"]
    assert first.items[0].meta["event_date"] == "2026-10-15"

    # Pretend we had seen older observations before.
    storage.kv_set("fred:last:CPIAUCSL", "2026-08-01")
    storage.kv_set("fred:last:DCOILBRENTEU", "2026-10-09")
    second = collector.collect(ctx)
    series_items = [i for i in second.items if i.source == "fred"]
    assert [i.title for i in series_items] == [
        "US CPI: 330.12 (2026-09-01), change +1.32 (+0.4%)",
        "Brent crude: 88.40 (2026-10-12), change +6.30 (+7.7%)",
    ]
    assert "data" in series_items[0].tags
    assert json.loads(storage.kv_get("fred:value:CPIAUCSL"))["value"] == 330.12


def test_fred_move_below_threshold_is_silent(make_http, storage, now):
    cfg_dict = json.loads(json.dumps(FRED_CFG))
    cfg_dict["sources"]["fred"]["series"][1]["threshold_pct"] = 10
    cfg_dict["sources"]["fred"]["calendar_releases"] = []
    ctx, cfg = make_ctx(make_http, fred_handler, storage, now, **cfg_dict)
    storage.kv_set("fred:last:DCOILBRENTEU", "2026-10-09")
    storage.kv_set("fred:last:CPIAUCSL", "2026-09-01")
    assert FredCollector(cfg.sources.fred).collect(ctx).items == []


def test_fred_requires_key_and_key_not_in_errors(make_http, storage, now):
    ctx, cfg = make_ctx(make_http, fred_handler, storage, now, secrets=Secrets(), **FRED_CFG)
    with pytest.raises(CollectorError, match="FRED_API_KEY"):
        FredCollector(cfg.sources.fred).collect(ctx)
    ctx, cfg = make_ctx(make_http, lambda r: httpx.Response(500), storage, now,
                        secrets=Secrets(fred_api_key="SECRETKEY123"), **FRED_CFG)
    with pytest.raises(CollectorError) as err:
        FredCollector(cfg.sources.fred).collect(ctx)
    assert "SECRETKEY123" not in str(err.value)


# ---- CoinGecko ----------------------------------------------------------------

CG_ROUTES = {
    "https://api.coingecko.com/api/v3/simple/price": fixture_bytes("coingecko_price.json"),
    "https://api.coingecko.com/api/v3/global": fixture_bytes("coingecko_global.json"),
}


def test_coingecko_moves_and_snapshot(make_http, storage, now):
    ctx, cfg = make_ctx(make_http, route(CG_ROUTES), storage, now,
                        sources={"coingecko": {"enabled": True}})
    result = CoinGeckoCollector(cfg.sources.coingecko).collect(ctx)
    assert [i.title for i in result.items] == [
        "BTC down -8.4% in 24h to $58,120",
        "Total crypto market cap -6.3% in 24h to $2.05T",
    ]
    snap = json.loads(storage.kv_get("prices:crypto"))
    assert snap["coins"]["ETH"]["usd"] == 2410.1


def test_coingecko_same_window_gives_same_url(make_http, storage, now):
    ctx, cfg = make_ctx(make_http, route(CG_ROUTES), storage, now,
                        sources={"coingecko": {"enabled": True, "cooldown_hours": 6}})
    a = CoinGeckoCollector(cfg.sources.coingecko).collect(ctx).items[0]
    ctx.now = now + timedelta(minutes=30)
    b = CoinGeckoCollector(cfg.sources.coingecko).collect(ctx).items[0]
    assert a.url_norm == b.url_norm  # deduplicated -> no repeated alert in the window


# ---- USGS ---------------------------------------------------------------------

def test_usgs_filters_by_magnitude_or_alert(make_http, storage, now):
    url = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson"
    ctx, cfg = make_ctx(make_http, route({url: fixture_bytes("usgs_quakes.geojson")}), storage, now,
                        sources={"usgs": {"enabled": True}})
    items = UsgsCollector(cfg.sources.usgs).collect(ctx).items
    assert [i.meta["magnitude"] for i in items] == [7.2, 5.9]  # 5.9 passes via red alert
    assert items[0].title == "Earthquake: M 7.2 - 25 km E of Hualien City, Taiwan"
    assert items[0].meta["tsunami"] is True
    assert items[0].published_at.isoformat() == "2026-10-13T11:00:00+00:00"


def test_gdelt_circuit_breaker_on_429(make_http, storage, now):
    calls = []

    def api(request):
        calls.append(request.url.params["query"])
        return httpx.Response(429, headers={"Retry-After": "60"})

    cfg = {"sources": {"gdelt": {"enabled": True, "queries": [
        {"name": f"q{i}", "query": f"term{i}"} for i in range(4)]}}}
    ctx, config = make_ctx(make_http, route({"https://api.gdeltproject.org/api/v2/doc/doc": api}),
                           storage, now, **cfg)
    with pytest.raises(CollectorError, match="429"):
        GdeltCollector(config.sources.gdelt).collect(ctx)
    assert len(calls) == 1  # no retries, remaining queries skipped
