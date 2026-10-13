from datetime import timedelta

from market_monitor.models import Item
from market_monitor.storage import MIGRATIONS, Storage


def make_item(now, url="https://a.com/x", title="t"):
    return Item(source="rss:a", published_at=now, title=title, url=url, tags=["macro"])


def test_migrations_applied(storage):
    assert storage.schema_version == len(MIGRATIONS)


def test_insert_dedups_by_normalised_url(storage, now):
    first = storage.insert_items([make_item(now)], now)
    again = storage.insert_items(
        [make_item(now, url="http://www.a.com/x/?utm_source=rss"), make_item(now, url="https://a.com/y")],
        now,
    )
    assert len(first) == 1
    assert [i.url for i in again] == ["https://a.com/y"]
    assert storage.count_items() == 2


def test_items_roundtrip(storage, now):
    storage.insert_items([make_item(now)], now)
    (item,) = list(storage.iter_items())
    assert item.tags == ["macro"] and item.published_at == now


def test_kv(storage):
    assert storage.kv_get("x") is None
    storage.kv_set("x", "1")
    storage.kv_set("x", "2")
    assert storage.kv_get("x") == "2"


def test_source_health(storage, now):
    storage.record_source_run("rss", now - timedelta(hours=1), True, 10, 4, 1.0)
    storage.record_source_run("rss", now, False, 0, 0, 1.0, "boom")
    storage.record_source_run("gdelt", now, True, 3, 3, 0.5)
    health = {h.source: h for h in storage.source_health(now)}
    assert health["rss"].ok is False and health["rss"].last_error == "boom"
    assert health["rss"].last_success == now - timedelta(hours=1)
    assert health["rss"].new_24h == 4
    assert health["gdelt"].ok


def test_prune(storage, now):
    storage.insert_items([make_item(now)], now - timedelta(days=40))
    storage.insert_items([make_item(now, url="https://a.com/new")], now)
    assert storage.prune(30, now) == 1
    assert storage.count_items() == 1


def test_file_db_persists(tmp_path, now):
    path = tmp_path / "sub" / "state.db"
    with Storage(path) as s:
        s.insert_items([make_item(now)], now)
    with Storage(path) as s:
        assert s.count_items() == 1
    assert not (tmp_path / "sub" / "state.db-wal").exists()
