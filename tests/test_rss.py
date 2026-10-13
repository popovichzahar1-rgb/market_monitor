import httpx
import pytest

from conftest import fixture_bytes, make_config, route
from market_monitor.collectors import CollectContext, CollectorError
from market_monitor.collectors.rss import RssCollector, parse_feed


def ctx_for(make_http, handler, storage, secrets, now, config=None):
    config = config or make_config()
    return CollectContext(http=make_http(handler), config=config, secrets=secrets,
                          state=storage, now=now), config


def test_parse_rss_fixture(now):
    cfg = make_config().sources.rss
    items = parse_feed(fixture_bytes("rss_markets.xml"), cfg.feeds[0], cfg, now)
    assert [i.title for i in items] == [
        "Fed holds rates steady, signals two cuts later this year",
        "Oil jumps 6% after attack on Gulf tanker",
    ]  # no-link item and >48h old item are skipped
    fed = items[0]
    assert fed.source == "rss:markets"
    assert fed.tags == ["markets"]
    assert fed.text == "The Federal Reserve held its benchmark rate at 4.25%–4.50%."
    assert fed.published_at.isoformat() == "2026-10-13T11:30:00+00:00"
    assert fed.url_norm == "https://news.example.com/fed-holds-rates"


def test_parse_atom_fixture(now):
    cfg = make_config().sources.rss
    items = parse_feed(fixture_bytes("atom_crypto.xml"), cfg.feeds[1], cfg, now)
    assert len(items) == 2
    assert items[0].text == "Hackers drained hot wallets. BTC fell 4%."


def test_garbage_feed_raises(now):
    cfg = make_config().sources.rss
    with pytest.raises(CollectorError):
        parse_feed(b"<html><body>not a feed", cfg.feeds[0], cfg, now)


def test_collector_one_failing_feed_does_not_break_others(make_http, storage, secrets, now):
    handler = route({"https://news.example.com/rss": 500,
                     "https://crypto.example.org/atom": fixture_bytes("atom_crypto.xml")})
    ctx, config = ctx_for(make_http, handler, storage, secrets, now)
    result = RssCollector(config.sources.rss).collect(ctx)
    assert len(result.items) == 2
    assert len(result.errors) == 1 and result.errors[0].startswith("markets:")


def test_collector_all_feeds_failing_raises(make_http, storage, secrets, now):
    ctx, config = ctx_for(make_http, route({}), storage, secrets, now)
    with pytest.raises(CollectorError, match="all 2 feeds failed"):
        RssCollector(config.sources.rss).collect(ctx)


def test_conditional_get_uses_etag(make_http, storage, secrets, now):
    seen_headers = []

    def feed(request):
        seen_headers.append(request.headers.get("If-None-Match"))
        if request.headers.get("If-None-Match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, content=fixture_bytes("rss_markets.xml"), headers={"ETag": '"v1"'})

    handler = route({"https://news.example.com/rss": feed,
                     "https://crypto.example.org/atom": fixture_bytes("atom_crypto.xml")})
    ctx, config = ctx_for(make_http, handler, storage, secrets, now)
    collector = RssCollector(config.sources.rss)
    first = collector.collect(ctx)
    second = collector.collect(ctx)
    assert seen_headers == [None, '"v1"']
    assert len(first.items) == 4 and len(second.items) == 2  # markets feed returned 304
