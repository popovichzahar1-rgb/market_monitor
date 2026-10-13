from datetime import datetime, timedelta, timezone

from market_monitor.models import Item, normalize_url


def test_normalize_url_strips_tracking_and_www():
    a = normalize_url("http://www.Example.com/news/story/?utm_source=x&id=5&fbclid=abc#top")
    b = normalize_url("https://example.com/news/story?id=5")
    assert a == b == "https://example.com/news/story?id=5"


def test_item_id_is_stable_across_tracking_variants():
    t = datetime(2026, 10, 13, tzinfo=timezone.utc)
    a = Item(source="s", published_at=t, title="x", url="https://a.com/x?utm_medium=rss")
    b = Item(source="s", published_at=t, title="x", url="https://www.a.com/x")
    assert a.id == b.id and len(a.id) == 16


def test_item_naive_datetime_becomes_utc_and_text_is_collapsed():
    naive = datetime(2026, 10, 13, 12, 0)
    item = Item(source="s", published_at=naive, title="  A \n  title ", url="https://a.com",
                tags=["x", "x", "y"])
    assert item.published_at.tzinfo is not None
    assert item.title == "A title"
    assert item.tags == ["x", "y"]


def test_item_converts_offsets_to_utc():
    t = datetime(2026, 10, 13, 14, 0, tzinfo=timezone(timedelta(hours=2)))
    item = Item(source="s", published_at=t, title="x", url="https://a.com")
    assert item.published_at == datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
