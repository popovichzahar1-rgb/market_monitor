import json
from datetime import datetime, timedelta, timezone

from market_monitor.config import load_config
from market_monitor.digest import build_digest, digest_due, mark_digest_sent
from market_monitor.models import Item
from market_monitor.notify.formatting import format_digest
from market_monitor.scoring import Score

REAL = load_config("config.yaml")
EVENING = datetime(2026, 10, 13, 18, 5, tzinfo=timezone.utc)  # 20:05 local


def add(storage, n, score, topic, now, tags=None, entities=None, meta=None, source="rss:x"):
    item = Item(source=source, published_at=now, title=f"Story {n}", url=f"https://x.test/{n}",
                tags=tags or [], entities=entities or [], meta=meta or {})
    storage.insert_item(item, now)
    storage.save_score(item.id, Score(score=score, topic=topic), now)
    return item


def test_digest_due_once_per_day(storage):
    before = datetime(2026, 10, 13, 17, 55, tzinfo=timezone.utc)  # 19:55 local
    assert not digest_due(REAL, storage, before)
    assert digest_due(REAL, storage, EVENING)
    mark_digest_sent(storage, EVENING)
    assert not digest_due(REAL, storage, EVENING + timedelta(hours=2))
    assert digest_due(REAL, storage, EVENING + timedelta(days=1))
    # Delayed cron run still sends that day's digest.
    assert digest_due(REAL, storage, EVENING + timedelta(days=1, hours=1, minutes=40))


def test_digest_time_override(storage):
    cfg = REAL.model_copy(update={"digest_time": "21:30"})
    assert not digest_due(cfg, storage, EVENING)


def test_build_digest_topics_scores_calendar_market(storage):
    t = EVENING - timedelta(hours=3)
    for n in range(7):
        add(storage, n, 90 - n, "macro", t)
    add(storage, 20, 25, "crypto", t)          # below min_score
    add(storage, 21, 60, "crypto", t)
    add(storage, 30, 10, "macro", t, tags=["calendar", "data"], source="fred:calendar",
        meta={"event_date": "2026-10-15", "release": "Consumer Price Index"})
    storage.kv_set("prices:crypto", json.dumps({"coins": {"BTC": {"usd": 58120.5, "change_24h_pct": -8.4}}}))
    storage.kv_set("fred:value:DGS10", json.dumps({"name": "10Y", "date": "2026-10-12", "value": 4.12}))

    data = build_digest(REAL, storage, EVENING)
    assert [v.title for v in data.items_by_topic["macro"]] == [f"Story {n}" for n in range(5)]
    assert [v.title for v in data.items_by_topic["crypto"]] == ["Story 21"]
    assert data.calendar == [("15.10", "Consumer Price Index")]
    assert data.market == ["BTC $58,120 (-8.4%)", "UST 10Y 4.12 (10-12)"]
    assert data.watchlist is None   # empty watchlist -> no company block

    (text,) = format_digest(data, REAL)
    assert "Щоденний дайджест · 13.10.2026" in text
    assert "Мій список відстеження" not in text
    assert "Consumer Price Index" in text
    assert text.rstrip().endswith("Рішення ухвалюєте ви.")
    assert "не є фінансовою порадою" in text


def test_digest_watchlist_block_and_muted_topics(storage):
    cfg = type(REAL).model_validate({**REAL.model_dump(), "muted_topics": ["crypto"],
                                     "watchlist": [{"ticker": "AAPL"}, {"ticker": "XOM"}]})
    t = EVENING - timedelta(hours=1)
    add(storage, 1, 70, "equities", t, entities=["AAPL"])
    add(storage, 2, 90, "crypto", t)
    data = build_digest(cfg, storage, EVENING)
    assert [v.title for v in data.watchlist["AAPL"]] == ["Story 1"]
    assert data.watchlist["XOM"] == []
    assert "crypto" not in data.items_by_topic
    (text,) = format_digest(data, cfg)
    assert "💼 <b>Мій список відстеження</b>" in text and "нічого нового" in text
    assert "Вимкнені теми: Крипта" in text


def test_digest_window_starts_at_previous_digest(storage):
    old = EVENING - timedelta(hours=30)
    add(storage, 1, 90, "macro", old)
    mark_digest_sent(storage, EVENING - timedelta(hours=24))
    add(storage, 2, 90, "macro", EVENING - timedelta(hours=1))
    data = build_digest(REAL, storage, EVENING)
    assert [v.title for v in data.items_by_topic["macro"]] == ["Story 2"]


def test_plural():
    from market_monitor.notify.formatting import sources_count
    assert [sources_count(n) for n in (1, 2, 5, 11, 12, 21, 22, 25)] == [
        "1 джерело", "2 джерела", "5 джерел", "11 джерел", "12 джерел", "21 джерело",
        "22 джерела", "25 джерел"]
