from datetime import timedelta

from conftest import FakeTelegram
from market_monitor import cli
from market_monitor.models import Item
from market_monitor.storage import Storage
from test_telegram import e2e_handler, processor


def test_explain_shows_breakdown_and_alert_decision(capsys):
    assert cli.main(["explain", "Hackers drain $200M from crypto exchange, withdrawals halted",
                     "--source", "rss:coindesk"]) == 0
    out = capsys.readouterr().out
    assert "flags:     exchange_hack" in out
    assert "rule:exchange_hack +55" in out
    assert "alert:     urgent:exchange_hack" in out
    cli.main(["explain", "Celebrity chef opens restaurant"])
    assert "alert:     no (digest only)" in capsys.readouterr().out


def test_status_command(tmp_path, capsys, now, monkeypatch):
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(name, raising=False)
    db = tmp_path / "s.db"
    with Storage(db) as s:
        s.insert_item(Item(source="x", published_at=now, title="t", url="https://x.test/1"), now)
        s.record_source_run("rss", now, False, 0, 0, 1.0, "HTTP 404")
    assert cli.main(["--db", str(db), "status"], now=now) == 0
    out = capsys.readouterr().out
    assert "items stored: 1" in out and "ERR rss" in out and "HTTP 404" in out


def test_commands_subcommand_and_status_reply(make_http, tmp_path, telegram_env, now):
    fake = FakeTelegram()
    fake.add_message("/status")
    db = str(tmp_path / "s.db")
    assert cli.main(["--db", db, "commands"], http=make_http(e2e_handler(fake)), now=now) == 0
    (reply,) = [p["text"] for p in fake.sent]
    assert reply.startswith("🛠 <b>Стан</b>") and "Тон тексту" in reply


def test_digest_command_respects_override_time(make_http, tmp_path, telegram_env, now):
    fake = FakeTelegram()
    fake.add_message("/digest_time 13:00")   # now = 14:00 local -> due right away
    db = str(tmp_path / "s.db")
    cli.main(["--db", db, "digest"], http=make_http(e2e_handler(fake)), now=now)
    texts = [p["text"] for p in fake.sent]
    assert texts[0].startswith("🕗 Дайджест щодня о 13:00")
    assert any(t.startswith("📊 <b>Щоденний дайджест") for t in texts)
    fake.calls.clear()
    cli.main(["--db", db, "digest"], http=make_http(e2e_handler(fake)), now=now + timedelta(hours=1))
    assert fake.sent == []   # once per day


def test_command_error_branches(make_http, storage):
    fake = FakeTelegram()
    for text in ["/help", "/nonsense", "/topics off nosuchtopic", "/topics maybe", "/threshold abc",
                 "/threshold weird 50", "/threshold 150", "/digest_time", "/watch", "/unwatch",
                 "/watch MSFT", "/unwatch MSFT", "/peers", "/feedback"]:
        fake.add_message(text)
    processor(make_http, storage, fake, lookup=lambda t: None).process()
    r = [p["text"] for p in fake.sent]
    assert "/watch TICKER" in r[0]
    assert r[1].startswith("Невідома команда")
    assert r[2].startswith("Невідома тема")
    assert r[3].startswith("Формат: /topics")
    assert r[4].startswith("Поріг — число") and r[6].startswith("Поріг — число")
    assert r[5].startswith("Тип порогу")
    assert r[7].startswith("🕗 Дайджест щодня о 20:00")
    assert r[8] == "Вкажіть тикер: /watch AAPL" and r[9] == "Вкажіть тикер: /unwatch AAPL"
    assert r[11] == "🗑 MSFT прибрано зі списку."
    assert r[12].startswith("Формат: /peers")
    assert r[13] == "Звіт за відгуками недоступний."
