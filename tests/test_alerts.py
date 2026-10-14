from datetime import datetime, timedelta, timezone

from market_monitor.alerts import AlertManager, AlertView, alert_reason, in_quiet_hours
from market_monitor.config import Config, load_config
from market_monitor.models import Item
from market_monitor.notify.base import ConsoleNotifier
from market_monitor.notify.telegram import TelegramError

REAL = load_config("config.yaml")
# 2026-10-13 12:00 UTC = 14:00 Europe/Bratislava (CEST): outside quiet hours.
DAY = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
NIGHT = datetime(2026, 10, 13, 22, 30, tzinfo=timezone.utc)  # 00:30 local


def view(n=1, score=85.0, flags=None, topic="macro", published=DAY):
    return AlertView(item_id=f"item{n}", title=f"Title {n}", url=f"https://x.test/{n}",
                     text="Body text", source="rss:fed", published_at=published, topic=topic,
                     score=score, flags=flags or [])


def store(storage, views):
    """Alerts reference items, so insert matching rows first."""
    for v in views:
        storage.insert_item(Item(id=v.item_id, source=v.source, published_at=v.published_at,
                                 title=v.title, url=v.url), v.published_at)
        storage.conn.execute("UPDATE items SET score=?, topic=?, flags=? WHERE id=?",
                             (v.score, v.topic, str(v.flags).replace("'", '"'), v.item_id))
    return views


def test_quiet_hours_wrap_midnight():
    assert in_quiet_hours(REAL, NIGHT)
    assert not in_quiet_hours(REAL, DAY)
    edge = datetime(2026, 10, 13, 5, 0, tzinfo=timezone.utc)  # 07:00 local -> quiet over
    assert not in_quiet_hours(REAL, edge)
    assert in_quiet_hours(REAL, edge - timedelta(minutes=1))
    assert not in_quiet_hours(Config.model_validate({"alerts": {"quiet_hours": None}}), NIGHT)


def test_alert_reason_order():
    assert alert_reason(view(score=56, flags=["central_bank_decision"]), REAL) == "always:central_bank_decision"
    assert alert_reason(view(score=66, flags=["exchange_hack"]), REAL) == "urgent:exchange_hack"
    assert alert_reason(view(score=64, flags=["exchange_hack"]), REAL) is None
    assert alert_reason(view(score=71, flags=["asset:BTC"]), REAL) == "priority:asset:BTC"
    assert alert_reason(view(score=79, flags=["asset:ETH"]), REAL) == "priority:asset:ETH"
    assert alert_reason(view(score=79), REAL) is None
    assert alert_reason(view(score=80), REAL) == "threshold"
    muted = REAL.model_copy(update={"muted_topics": ["macro"]})
    assert alert_reason(view(score=99), muted) is None


def test_sends_once_and_records_history(storage):
    notifier = ConsoleNotifier()
    manager = AlertManager(REAL, storage, notifier)
    views = store(storage, [view(1, 90), view(2, 50)])
    out = manager.process(views, DAY)
    assert [v.item_id for v in out.sent] == ["item1"]
    assert "Title 1" in notifier.sent[0] and "🚨" in notifier.sent[0]
    # Same item (e.g. re-scored later) never alerts twice.
    assert manager.process(views, DAY + timedelta(minutes=30)).sent == []


def test_rate_limit_per_hour(storage):
    notifier = ConsoleNotifier()
    manager = AlertManager(REAL, storage, notifier)
    views = store(storage, [view(n, 80 + n) for n in range(7)])
    out = manager.process(views, DAY)
    assert len(out.sent) == 5 and len(out.rate_limited) == 2
    assert [v.score for v in out.sent] == [86, 85, 84, 83, 82]   # highest first
    more = store(storage, [view(10, 95)])
    assert manager.process(more, DAY + timedelta(minutes=50)).rate_limited  # still within the hour
    later = store(storage, [view(11, 95)])
    assert manager.process(later, DAY + timedelta(minutes=61)).sent


def test_quiet_hours_queue_then_single_morning_message(storage):
    notifier = ConsoleNotifier()
    manager = AlertManager(REAL, storage, notifier)
    out = manager.process(store(storage, [view(1, 90), view(2, 85, ["exchange_hack"], "crypto")]), NIGHT)
    assert len(out.queued) == 2 and notifier.sent == []
    morning = datetime(2026, 10, 14, 5, 5, tzinfo=timezone.utc)  # 07:05 local
    out = manager.process(store(storage, [view(3, 90, published=morning)]), morning)
    assert len(out.flushed) == 2 and len(out.sent) == 1
    assert notifier.sent[0].startswith("🌙") and "Title 1" in notifier.sent[0]
    assert "злам" in notifier.sent[0]  # flag label shown in the batch


def test_stale_items_are_not_alerted(storage):
    manager = AlertManager(REAL, storage, ConsoleNotifier())
    old = store(storage, [view(1, 99, published=DAY - timedelta(hours=13))])
    assert manager.process(old, DAY).sent == []


def test_send_failure_requeues(storage):
    class Failing:
        def send(self, text, buttons=None):
            raise TelegramError("network down")

    manager = AlertManager(REAL, storage, Failing())
    out = manager.process(store(storage, [view(1, 90)]), DAY)
    assert out.failed and storage.alerts_by_status("queued")
    notifier = ConsoleNotifier()
    out = AlertManager(REAL, storage, notifier).process([], DAY + timedelta(minutes=30))
    assert len(out.flushed) == 1 and not storage.alerts_by_status("queued")


def test_dry_run_does_not_count_towards_limit(storage):
    manager = AlertManager(REAL, storage, ConsoleNotifier(), dry_run=True)
    manager.process(store(storage, [view(n, 90) for n in range(6)]), DAY)
    assert storage.alerts_sent_since(DAY - timedelta(hours=1)) == 0
