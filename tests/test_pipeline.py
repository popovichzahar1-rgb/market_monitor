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
    assert "Джерела: 1/1 працюють" in out
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
