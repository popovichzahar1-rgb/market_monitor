from conftest import fixture_bytes, make_config, route
from market_monitor import cli, pipeline
from market_monitor.collectors import CollectResult, Collector
from market_monitor.collectors.rss import RssCollector


class Exploding(Collector):
    name = "exploding"

    def collect(self, ctx):
        raise RuntimeError("source down")


HANDLER = route({"https://news.example.com/rss": fixture_bytes("rss_markets.xml"),
                 "https://crypto.example.org/atom": fixture_bytes("atom_crypto.xml")})


def test_failing_collector_is_isolated(make_http, storage, secrets, now):
    config = make_config()
    collectors = [Exploding(config.sources.rss), RssCollector(config.sources.rss)]
    report = pipeline.collect(config, secrets, storage, make_http(HANDLER), now, collectors)
    assert [s.source for s in report.failed_sources] == ["exploding"]
    assert len(report.new_items) == 4
    health = {h.source: h for h in storage.source_health(now)}
    assert "source down" in health["exploding"].last_error


def test_second_run_has_no_new_items(make_http, storage, secrets, now):
    config = make_config()
    http = make_http(HANDLER)
    first = pipeline.collect(config, secrets, storage, http, now)
    second = pipeline.collect(config, secrets, storage, http, now)
    assert len(first.new_items) == 4 and second.new_items == []


def test_cli_dry_run_prints_message(make_http, tmp_path, capsys, now):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sources:\n  rss:\n    feeds:\n"
        "      - {name: markets, url: 'https://news.example.com/rss', tags: [markets]}\n"
        "      - {name: crypto, url: 'https://crypto.example.org/atom', tags: [crypto]}\n"
    )
    code = cli.main(["--config", str(cfg), "--db", str(tmp_path / "s.db"), "run", "--dry-run"],
                    http=make_http(HANDLER), now=now)
    out = capsys.readouterr().out
    assert code == 0
    assert "[dry-run] Telegram message" in out
    assert "Oil jumps 6% after attack on Gulf tanker" in out
    assert "Джерела: 2/2 працюють" in out  # rss + sec_edgar (no-op, empty watchlist)
    assert "не є фінансовою порадою" in out


def test_cli_reports_config_error(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("timezone: Mars/Olympus\n")
    assert cli.main(["--config", str(cfg), "run", "--dry-run"]) == 2


def test_example_and_real_configs_are_valid():
    from market_monitor.config import load_config

    real = load_config("config.yaml")
    example = load_config("config.example.yaml")
    assert real.watchlist == []  # starts empty by design
    assert len(example.watchlist) >= 2
    assert real.sources.rss.feeds


def full_handler(request):
    """Serves fixtures for every source in the real config.yaml; anything else -> 404."""
    import httpx

    from test_collectors_data import CG_ROUTES, SEC_ROUTES, fred_handler

    url = str(request.url.copy_with(query=None))
    if request.url.path == "/robots.txt":
        return httpx.Response(404)
    if request.url.host == "api.stlouisfed.org":
        return fred_handler(request)
    fixtures = {
        "https://www.federalreserve.gov/feeds/press_all.xml": fixture_bytes("rss_markets.xml"),
        "https://cointelegraph.com/rss": fixture_bytes("atom_crypto.xml"),
        "https://api.gdeltproject.org/api/v2/doc/doc": fixture_bytes("gdelt_artlist.json"),
        "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson":
            fixture_bytes("usgs_quakes.geojson"),
        **CG_ROUTES, **SEC_ROUTES,
    }
    if url in fixtures:
        return httpx.Response(200, content=fixtures[url])
    return httpx.Response(404)


def test_full_config_end_to_end(make_http, storage, secrets, now):
    from market_monitor.config import load_config

    config = load_config("config.yaml").model_copy(update={"watchlist": []})
    report = pipeline.collect(config, secrets, storage, make_http(full_handler), now)
    by_source = {s.source: s for s in report.sources}
    assert set(by_source) == {"rss", "gdelt", "sec_edgar", "fred", "coingecko", "usgs"}
    assert all(s.ok for s in report.sources)  # partial feed failures don't fail a source
    # 6 GDELT queries return the same fixture: one copy each is new, the rest are duplicates;
    # within the fixture the two Hormuz headlines are merged as one story.
    assert by_source["gdelt"].new == 2
    assert by_source["gdelt"].duplicates == by_source["gdelt"].fetched - 2
    titles = {i.title for i in report.new_items}
    assert "Total crypto market cap -6.3% in 24h to $2.05T" in titles
    assert "Earthquake: M 7.2 - 25 km E of Hualien City, Taiwan" in titles
    assert any(t.startswith("Upcoming US release: Consumer Price Index") for t in titles)
