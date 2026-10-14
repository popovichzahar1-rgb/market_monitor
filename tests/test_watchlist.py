import json
from datetime import timedelta

import pytest

from conftest import fixture_bytes
from market_monitor.collectors.sec_edgar import company_snapshot
from market_monitor.config import Config, WatchItem, load_config
from market_monitor.digest import build_digest
from market_monitor.models import Item
from market_monitor.notify.formatting import format_digest
from market_monitor.scoring import Score, Scorer
from market_monitor.watchlist import WatchlistMatcher

APPLE = WatchItem(ticker="AAPL", name="Apple Inc.", aliases=["Apple", "iPhone maker"],
                  peers=["MSFT", "Samsung"], suppliers=["TSM", "Foxconn"],
                  sector_keywords=["smartphone sales"])
NOW_CO = WatchItem(ticker="NOW", name="ServiceNow")
XOM = WatchItem(ticker="XOM", name="Exxon Mobil", aliases=["Exxon"], sector_keywords=["oil price*"])


@pytest.fixture
def matcher():
    return WatchlistMatcher([APPLE, NOW_CO, XOM])


@pytest.mark.parametrize("text,direct", [
    ("Apple unveils new chip", ["AAPL"]),
    ("APPLE SHARES FALL", ["AAPL"]),                        # names: any case
    ("The iPhone maker cut prices in China", ["AAPL"]),     # alias
    ("Apple's quarterly results", ["AAPL"]),
    ("AAPL slides 3% premarket", ["AAPL"]),                 # ticker, case-sensitive
    ("aapl is lowercase here", []),
    ("Pineapple prices rise", []),                          # whole words only
    ("Apples and oranges", []),
    ("Stocks rise now as Fed pauses", []),                  # common-word ticker in plain text
    ("NOW shares jump", []),                                # ...even uppercase
    ("$NOW jumps after earnings", ["NOW"]),                 # strong forms only
    ("ServiceNow (NYSE: NOW) beats estimates", ["NOW"]),
    ("Exxon to buy shale rival", ["XOM"]),
])
def test_direct_matching(matcher, text, direct):
    assert matcher.match(text).direct == direct


def test_related_matching_and_direct_precedence(matcher):
    m = matcher.match("Foxconn plant halted; MSFT also hit", "Global smartphone sales slow")
    assert m.direct == []
    assert m.related["AAPL"] == ["peer:MSFT", "supplier:Foxconn", "sector"]
    assert matcher.match("Oil prices surge 5%").related == {"XOM": ["sector"]}
    both = matcher.match("Apple and Samsung settle patent case")
    assert both.direct == ["AAPL"] and "AAPL" not in both.related
    assert matcher.match("Samsung's new phone", "").related == {"AAPL": ["peer:Samsung"]}


def test_enrich_and_existing_entities(matcher, now):
    item = Item(source="rss:x", published_at=now, title="TSMC says Foxconn orders strong",
                url="https://x.test/1")
    matcher.enrich(item)
    assert item.entities == [] and item.meta["related"]["AAPL"] == ["supplier:Foxconn"]
    sec = Item(source="sec_edgar", published_at=now, title="Filing", url="https://x.test/2",
               entities=["AAPL"])
    assert matcher.enrich(sec).entities == ["AAPL"]


def test_scoring_direct_vs_related(now):
    cfg = Config.model_validate({"watchlist": [APPLE.model_dump()]})
    matcher = WatchlistMatcher(cfg.watchlist)
    direct = matcher.enrich(Item(source="x", published_at=now, title="Apple recalls chargers",
                                 url="https://x.test/a"))
    related = matcher.enrich(Item(source="x", published_at=now, title="Foxconn factory fire",
                                  url="https://x.test/b"))
    d, r = Scorer(cfg, use_default_model=False).score([direct, related])
    assert d.score == 25 and "watchlist" in d.flags
    assert r.score == 8 and "watchlist" not in r.flags
    assert any(x.startswith("related:AAPL(supplier:Foxconn)") for x in r.reasons)


def test_sec_company_snapshot():
    payload = json.loads(fixture_bytes("sec_submissions_aapl.json"))
    latest, profile = company_snapshot(payload, 320193)
    assert sorted(latest) == ["10-K", "10-Q", "8-K"]
    assert latest["10-K"]["date"] == "2026-08-01"   # included regardless of age
    assert latest["8-K"]["items"] == "2.02,9.01"
    assert profile == {"name": "Apple Inc.", "sic_description": "ELECTRONIC COMPUTERS",
                       "exchanges": ["Nasdaq"]}


def test_digest_company_block(storage, now):
    base = load_config("config.yaml")
    cfg = type(base).model_validate({**base.model_dump(), "watchlist": [APPLE.model_dump(),
                                                                         XOM.model_dump()]})
    t = now - timedelta(hours=2)

    def add(title, score, source="rss:x", entities=None):
        item = Item(source=source, published_at=t, title=title, url=f"https://x.test/{abs(hash(title))}",
                    entities=entities or [])
        storage.insert_item(item, t)
        storage.save_score(item.id, Score(score=score, topic="equities"), t)

    add("Apple shares hit record", 60)                    # no stored entity: matched live
    add("AAPL: SEC 8-K filed — 2.02 results of operations (earnings)", 75, "sec_edgar", ["AAPL"])
    add("Foxconn warns of iPhone component delays", 45)   # related (supplier) via alias? no: supplier
    add("Foxconn minor update", 10)                       # related but below related min score
    storage.kv_set("sec:latest:AAPL", json.dumps({"10-Q": {"date": "2026-08-01", "items": "",
                                                           "url": "https://sec.test/q"}}))
    storage.kv_set("sec:profile:AAPL", json.dumps({"name": "Apple Inc.",
                                                   "sic_description": "ELECTRONIC COMPUTERS"}))
    data = build_digest(cfg, storage, now)
    aapl, xom = data.watchlist
    assert [v.title for v in aapl.news] == ["Apple shares hit record"]
    assert [v.title for v in aapl.filings][0].startswith("AAPL: SEC 8-K")
    assert [(v.title, k) for v, k in aapl.related] == [
        ("Foxconn warns of iPhone component delays", ["supplier:Foxconn"])]
    assert xom.empty
    (text,) = format_digest(data, cfg)
    assert "<b>AAPL</b> · Apple Inc." in text
    assert "📄 Останні звіти SEC: " in text and "10-Q 01.08.26" in text
    assert "🔗 <b>45</b> <i>[постачальник Foxconn]</i>" in text
    assert "<b>XOM</b> · Exxon Mobil" in text and "нічого нового за період" in text
