from datetime import timedelta

from market_monitor.config import DedupConfig
from market_monitor.dedup import Deduplicator
from market_monitor.models import Item
from market_monitor.pipeline import store_new
from market_monitor.config import Config

LONG_TEXT = ("The Federal Reserve left its benchmark interest rate unchanged on Tuesday, "
             "while signalling that two cuts remain likely before the end of the year as "
             "inflation continues to cool across goods and services.")


def item(now, url, title, text="", source="rss:a", tags=None):
    return Item(source=source, published_at=now, title=title, text=text, url=url, tags=tags or [])


def run(storage, now, items, **dedup):
    config = Config.model_validate({"dedup": dedup})
    return store_new(items, storage, Deduplicator(storage, config.dedup, now), now, config)


def test_same_url_with_tracking_is_duplicate(storage, now):
    new = run(storage, now, [item(now, "https://a.com/x", "Fed holds rates steady today"),
                             item(now, "https://www.a.com/x?utm_source=tw", "Different title entirely here")])
    assert len(new) == 1


def test_syndicated_text_is_duplicate_and_counted(storage, now):
    a = item(now, "https://a.com/fed", "Fed keeps rates unchanged", LONG_TEXT, source="rss:a")
    b = item(now, "https://b.com/story-123", "Powell: two cuts still on table", LONG_TEXT, source="rss:b")
    new = run(storage, now, [a, b])
    assert [i.id for i in new] == [a.id]
    row = storage.get_item_row(a.id)
    assert row["mentions"] == 2 and "rss:b" in row["other_sources"]
    # The duplicate URL is now an alias, so it is caught by URL next time.
    assert storage.find_by_url(b.url_norm) == a.id


def test_similar_headlines_are_duplicates_across_runs(storage, now):
    run(storage, now, [item(now, "https://a.com/1", "Iran seizes oil tanker in Strait of Hormuz, crude prices climb")])
    new = run(storage, now + timedelta(hours=1), [
        item(now, "https://b.com/2", "Iran seizes oil tanker in Strait of Hormuz; crude prices climb - Reuters"),
        item(now, "https://c.com/3", "EU agrees new sanctions package targeting LNG shipments"),
    ])
    assert [i.url for i in new] == ["https://c.com/3"]


def test_different_central_banks_are_not_merged(storage, now):
    new = run(storage, now, [
        item(now, "https://a.com/1", "Fed holds interest rates steady at September meeting"),
        item(now, "https://a.com/2", "ECB holds interest rates steady at September meeting"),
    ])
    # Fuzzy score is ~95, but the acronyms differ -> different events, both kept.
    assert len(new) == 2


def test_different_numbers_are_not_merged(storage, now):
    new = run(storage, now, [
        item(now, "https://a.com/1", "Brent crude jumps 6% after tanker attack in Gulf"),
        item(now, "https://a.com/2", "Brent crude jumps 9% after tanker attack in Gulf"),
    ])
    assert len(new) == 2


def test_short_titles_are_not_fuzzy_matched(storage, now):
    new = run(storage, now, [item(now, "https://a.com/1", "Markets wrap"),
                             item(now, "https://b.com/2", "Markets wrap")])
    assert len(new) == 2


def test_data_items_skip_fuzzy_dedup(storage, now):
    new = run(storage, now, [
        item(now, "https://x.com/btc?move=1", "BTC down -8.4% in 24h to $58,120", tags=["data"]),
        item(now, "https://x.com/btc?move=2", "BTC down -8.5% in 24h to $58,000", tags=["data"]),
    ])
    assert len(new) == 2


def test_items_outside_window_are_not_matched(storage, now):
    old = now - timedelta(hours=100)
    run(storage, old, [item(old, "https://a.com/1", "Iran seizes oil tanker in Strait of Hormuz today")])
    new = run(storage, now, [item(now, "https://b.com/1", "Iran seizes oil tanker in Strait of Hormuz today")],
              window_hours=48)
    assert len(new) == 1
