import json
from datetime import timedelta

from conftest import TEST_CHAT, FakeTelegram
from market_monitor import cli
from market_monitor.alerts import AlertManager, AlertView
from market_monitor.config import load_config
from market_monitor.feedback import (FeedbackHandler, build_report, buttons_for, mark_chosen,
                                     parse_callback)
from market_monitor.models import Item
from market_monitor.notify.formatting import format_feedback_report
from market_monitor.scoring import Score

REAL = load_config("config.yaml")


def add_alert(storage, now, n, vote=None, flags=None, reasons=None, source="rss:x", reason="urgent:x",
              score=70.0, age_days=0):
    t = now - timedelta(days=age_days)
    item = Item(source=source, published_at=t, title=f"Alert {n}", url=f"https://x.test/{n}")
    storage.insert_item(item, t)
    storage.save_score(item.id, Score(score=score, topic="crypto", flags=flags or [],
                                      reasons=reasons or []), t)
    storage.record_alert(item.id, "sent", reason, score, t, message_id=500 + n)
    if vote is not None:
        storage.set_feedback(item.id, vote, t)
    return item.id


def test_buttons_and_callback_data():
    (row,) = buttons_for(["abc123"])
    assert [b["text"] for b in row] == ["👍 Корисно", "👎 Шум"]
    assert row[0]["callback_data"] == "fb:u:abc123" and len(row[1]["callback_data"]) <= 64
    rows = buttons_for(["a", "b"])
    assert rows[1][0]["text"] == "2. 👍 Корисно"
    assert parse_callback("fb:d:abc") == ("abc", -1)
    assert parse_callback("fb:x:abc") is None and parse_callback("other") is None


def test_mark_chosen_allows_changing_vote():
    kb = buttons_for(["a", "b"])
    first = mark_chosen(kb, "a", 1)
    assert first[0][0]["text"] == "✅ 1. 👍 Корисно" and first[1][0]["text"] == "2. 👍 Корисно"
    changed = mark_chosen(first, "a", -1)
    assert changed[0][0]["text"] == "1. 👍 Корисно" and changed[0][1]["text"] == "✅ 1. 👎 Шум"


def test_alerts_carry_buttons(storage, now, make_http):
    from market_monitor.notify.telegram import TelegramClient, TelegramNotifier
    from conftest import TEST_TOKEN
    from market_monitor.config import TelegramConfig

    fake = FakeTelegram()
    client = TelegramClient(make_http(fake, respect_robots_txt=False), TEST_TOKEN, TelegramConfig())
    item = Item(source="rss:x", published_at=now, title="Exchange hacked", url="https://x.test/h")
    storage.insert_item(item, now)
    view = AlertView(item_id=item.id, title=item.title, url=item.url, text="", source="rss:x",
                     published_at=now, topic="crypto", score=90, flags=[])
    AlertManager(REAL, storage, TelegramNotifier(client, TEST_CHAT)).process([view], now)
    (payload,) = fake.sent
    assert payload["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == f"fb:u:{item.id}"


def callback_update(update_id, data, chat_id=TEST_CHAT, keyboard=None):
    return {"update_id": update_id, "callback_query": {
        "id": f"cb{update_id}", "data": data,
        "message": {"message_id": 777, "chat": {"id": int(chat_id)},
                    "reply_markup": {"inline_keyboard": keyboard or []}}}}


def test_callback_flow_via_commands(storage, now, make_http):
    from test_telegram import processor

    item_id = add_alert(storage, now, 1)
    kb = buttons_for([item_id])
    fake = FakeTelegram()
    fake.updates += [
        callback_update(1, f"fb:d:{item_id}", keyboard=kb),
        callback_update(2, f"fb:u:{item_id}", chat_id="999", keyboard=kb),   # stranger
        callback_update(3, "fb:u:unknownid", keyboard=kb),
    ]
    proc = processor(make_http, storage, fake)
    proc.on_callback = FeedbackHandler(storage, proc.client, now)
    assert proc.process() == 2
    answers = [p for m, p in fake.calls if m == "answerCallbackQuery"]
    assert answers[0]["text"] == "Дякую! Врахую: шум 👎"
    assert answers[1]["text"] == "Цей алерт уже не знайдено в історії."
    (edit,) = [p for m, p in fake.calls if m == "editMessageReplyMarkup"]
    assert edit["message_id"] == 777 and edit["reply_markup"]["inline_keyboard"][0][1]["text"] == "✅ 👎 Шум"
    (row,) = storage.feedback_rows(now - timedelta(days=1))
    assert row["feedback"] == -1   # the stranger's 👍 was ignored


def test_report_and_suggestions(storage, now):
    for n in range(4):  # a noisy rule from a noisy source
        add_alert(storage, now, n, vote=-1, flags=["crypto_regulation"],
                  reasons=["rule:crypto_regulation +40", "assets:BTC +10"], source="rss:decrypt",
                  reason="urgent:crypto_regulation")
    for n in range(10, 13):  # a good rule
        add_alert(storage, now, n, vote=1, flags=["energy_supply_shock"],
                  reasons=["rule:energy_supply_shock +45"], source="rss:oilprice",
                  reason="urgent:energy_supply_shock")
    add_alert(storage, now, 20)                                  # no vote
    report = build_report(storage, now)
    assert (report.overall.up, report.overall.down, report.unrated) == (3, 4, 1)
    assert report.by_rule["crypto_regulation"].down == 4
    text = " ".join(report.suggestions)
    assert "«crypto_regulation»: 4 з 4" in text
    assert "rss:decrypt" in text and "/threshold urgent 70" not in text  # urgent is 4 of 7
    assert "«energy_supply_shock» працює добре" in text
    msg = format_feedback_report(report)
    assert "👍 3 · 👎 4 · без оцінки 1" in msg and "Alert 3" in msg


def test_empty_report():
    from market_monitor.feedback import FeedbackReport

    assert "Ще немає оцінок" in format_feedback_report(FeedbackReport(days=30))


def test_prune_keeps_voted_items(storage, now):
    voted = add_alert(storage, now, 1, vote=1, age_days=40)
    add_alert(storage, now, 2, age_days=40)
    storage.prune(30, now)
    ids = [r["item_id"] for r in storage.feedback_rows(now - timedelta(days=100))]
    assert ids == [voted]


def test_cli_feedback_command(tmp_path, capsys, now, monkeypatch):
    from market_monitor.storage import Storage

    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(name, raising=False)
    db = tmp_path / "s.db"
    with Storage(db) as s:
        add_alert(s, now, 1, vote=-1, flags=["opec_decision"], reasons=["rule:opec_decision +40"])
    assert cli.main(["--db", str(db), "feedback"], now=now) == 0
    out = capsys.readouterr().out
    assert "Відгуки за 30 днів" in out and "opec_decision" in out and "<b>" not in out
