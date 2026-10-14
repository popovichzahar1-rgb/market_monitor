import logging
from datetime import timedelta

import httpx
import pytest

from conftest import TEST_CHAT, TEST_TOKEN, FakeTelegram, fixture_bytes
from market_monitor import cli
from market_monitor.config import TelegramConfig, load_config
from market_monitor.notify.base import ConsoleNotifier
from market_monitor.notify.commands import OFFSET_KEY, CommandProcessor
from market_monitor.notify.telegram import TelegramClient, TelegramError, split_message
from market_monitor.settings import Settings

REAL = load_config("config.yaml")


def make_client(make_http, fake):
    return TelegramClient(make_http(fake, respect_robots_txt=False), TEST_TOKEN, TelegramConfig())


def processor(make_http, storage, fake, lookup=None, save_offset=True):
    client = make_client(make_http, fake)
    from market_monitor.notify.telegram import TelegramNotifier

    return CommandProcessor(client, TEST_CHAT, Settings(REAL, storage),
                            TelegramNotifier(client, TEST_CHAT), status=lambda: "STATUS",
                            lookup=lookup, save_offset=save_offset)


# ---- client -------------------------------------------------------------------

def test_split_message_keeps_lines_whole():
    text = "\n".join(f"line {i} " + "x" * 50 for i in range(100))
    parts = split_message(text, 1000)
    assert all(len(p) <= 1000 for p in parts)
    assert "\n".join(parts) == text


def test_send_message_payload_and_long_text(make_http):
    fake = FakeTelegram()
    client = make_client(make_http, fake)
    client.send_message(TEST_CHAT, "<b>hi</b>\n" + "y" * 5000, buttons=[[{"text": "ok", "callback_data": "x"}]])
    assert [len(p["text"]) for p in fake.sent] == [9, 4000, 1000]  # order kept, long line hard-split
    assert fake.sent[0]["parse_mode"] == "HTML"
    assert fake.sent[0]["link_preview_options"] == {"is_disabled": True}
    assert "reply_markup" in fake.sent[-1] and "reply_markup" not in fake.sent[0]


def test_errors_never_contain_the_token(make_http, caplog):
    fake = FakeTelegram(fail_methods={"sendMessage"})
    client = make_client(make_http, fake)
    with pytest.raises(TelegramError) as err:
        client.send_message(TEST_CHAT, "x")
    assert TEST_TOKEN not in str(err.value)
    assert "test failure" in str(err.value)


# ---- commands -----------------------------------------------------------------

def test_commands_and_allowlist(make_http, storage):
    fake = FakeTelegram()
    fake.add_message("/watch aapl")
    fake.add_message("/watch AAPL")
    fake.add_message("/watch AAPL", chat_id="999")        # stranger: ignored, no reply
    fake.add_message("/unwatch MSFT")
    fake.add_message("/threshold 75")
    fake.add_message("/threshold urgent 60")
    fake.add_message("/digest_time 7:5")
    fake.add_message("/digest_time 25:00")
    fake.add_message("/topics off crypto")
    fake.add_message("/status@MyMarketBot")
    fake.add_message("hello")
    proc = processor(make_http, storage, fake, lookup=lambda t: ("Apple Inc.", 320193))
    assert proc.process() == 10
    replies = [p["text"] for p in fake.sent]
    assert all(p["chat_id"] == TEST_CHAT for p in fake.sent)
    assert replies[0].startswith("✅ Додано AAPL (Apple Inc.)")
    assert "уже є у списку" in replies[1]
    assert "MSFT немає у списку" in replies[2]
    assert "загальний: <b>75</b>" in replies[3]
    assert "термінові категорії: <b>60</b>" in replies[4]
    assert "07:05" in replies[5]
    assert "Формат часу" in replies[6]
    assert "🔕 вимкнено" in replies[7]
    assert replies[8] == "STATUS"
    assert "лише команди" in replies[9]

    cfg = Settings(REAL, storage).effective()
    (watch,) = cfg.watchlist
    assert (watch.ticker, watch.name, watch.cik, watch.aliases) == ("AAPL", "Apple Inc.", "320193", ["Apple"])
    assert cfg.alerts.threshold == 75 and cfg.alerts.urgent_threshold == 60
    assert cfg.digest_time == "07:05" and cfg.muted_topics == ["crypto"]

    # Offset persisted: nothing is replayed on the next run.
    assert storage.kv_get(OFFSET_KEY) == str(fake.updates[-1]["update_id"] + 1)
    fake.calls.clear()
    assert proc.process() == 0 and fake.sent == []


def test_watch_unknown_ticker_and_invalid(make_http, storage):
    fake = FakeTelegram()
    fake.add_message("/watch SAP")
    fake.add_message("/watch не_тикер")
    processor(make_http, storage, fake, lookup=lambda t: None).process()
    assert "не знайдено в базі SEC" in fake.sent[0]["text"]
    assert "не схоже на тикер" in fake.sent[1]["text"]


def test_dry_run_does_not_consume_updates(make_http, storage):
    fake = FakeTelegram()
    fake.add_message("/list")
    processor(make_http, storage, fake, save_offset=False).process()
    assert storage.kv_get(OFFSET_KEY) is None


# ---- end to end through the CLI ------------------------------------------------

def e2e_handler(fake):
    from test_pipeline import full_handler

    def handler(request):
        return fake(request) if fake.handles(request) else full_handler(request)
    return handler


def test_cli_run_sends_alerts_and_handles_commands(make_http, tmp_path, telegram_env, now):
    fake = FakeTelegram()
    fake.add_message("/watch AAPL")
    db = str(tmp_path / "state.db")
    http = make_http(e2e_handler(fake), retries=0)
    code = cli.main(["--db", db, "run", "--no-digest"], http=http, now=now)
    assert code == 0
    methods = [m for m, _ in fake.calls]
    assert methods[0] == "getUpdates"             # commands first
    texts = [p["text"] for p in fake.sent]
    assert texts[0].startswith("✅ Додано AAPL")
    alerts = [t for t in texts if t.startswith("🚨")]
    assert 1 <= len(alerts) <= 5                  # hourly limit
    assert any("exchange halts withdrawals" in t for t in alerts)

    # Second run an hour later: no repeated alerts, no replayed commands.
    fake.calls.clear()
    http2 = make_http(e2e_handler(fake), retries=0)
    cli.main(["--db", db, "run", "--no-digest"], http=http2, now=now + timedelta(hours=1))
    assert not [p for p in fake.sent if p["text"].startswith(("🚨", "✅"))]


def test_cli_digest_force_and_due(make_http, tmp_path, telegram_env, now):
    fake = FakeTelegram()
    db = str(tmp_path / "state.db")
    cli.main(["--db", db, "run", "--no-digest"], http=make_http(e2e_handler(fake), retries=0), now=now)
    fake.calls.clear()
    # 12:00 UTC = 14:00 local: not due yet ...
    cli.main(["--db", db, "digest"], http=make_http(e2e_handler(fake), retries=0), now=now)
    assert fake.sent == []
    # ... but --force sends it.
    cli.main(["--db", db, "digest", "--force"], http=make_http(e2e_handler(fake), retries=0), now=now)
    (digest,) = [p["text"] for p in fake.sent]
    assert "Щоденний дайджест" in digest and "не є фінансовою порадою" in digest
    assert "Мій список відстеження" not in digest


def test_cli_run_without_telegram_secrets_prints_and_flags(make_http, tmp_path, monkeypatch, capsys, now):
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(name, raising=False)
    from test_pipeline import full_handler

    code = cli.main(["--db", str(tmp_path / "s.db"), "run", "--no-digest"],
                    http=make_http(full_handler, retries=0), now=now)
    assert code == 3
    assert "[dry-run] Telegram message" in capsys.readouterr().out


def test_cli_dry_run_never_calls_telegram_send(make_http, tmp_path, telegram_env, capsys, now):
    fake = FakeTelegram()
    fake.add_message("/list")
    cli.main(["--db", str(tmp_path / "s.db"), "run", "--dry-run", "--no-digest"],
             http=make_http(e2e_handler(fake), retries=0), now=now)
    assert fake.sent == []
    out = capsys.readouterr().out
    assert "Список відстеження порожній" in out and "🚨" in out


def test_link_commands(make_http, storage):
    fake = FakeTelegram()
    fake.add_message("/watch AAPL iPhone maker")
    fake.add_message("/peers AAPL MSFT, Samsung Electronics")
    fake.add_message("/suppliers AAPL TSM Foxconn")
    fake.add_message("/sector AAPL smartphone sales, App Store")
    fake.add_message("/alias AAPL Apple, iPhone maker, Cupertino")
    fake.add_message("/peers XOM CVX")
    fake.add_message("/suppliers AAPL -")
    fake.add_message("/list")
    processor(make_http, storage, fake, lookup=lambda t: ("Apple Inc.", 320193)).process()
    replies = [p["text"] for p in fake.sent]
    assert "Шукаю в новинах: AAPL, Apple, iPhone maker" in replies[0]
    assert replies[1] == "✅ AAPL: конкуренти — MSFT, Samsung Electronics"
    assert replies[2] == "✅ AAPL: постачальники — TSM, Foxconn"
    assert "XOM немає у списку" in replies[5]
    assert replies[6] == "✅ AAPL: постачальники — —"
    assert "конкуренти: MSFT, Samsung Electronics" in replies[7]
    (w,) = Settings(REAL, storage).effective().watchlist
    assert w.sector_keywords == ["smartphone sales", "App Store"]
    assert w.aliases == ["Apple", "iPhone maker", "Cupertino"] and w.suppliers == []


def test_cli_watch_then_run_links_news_and_sec(make_http, tmp_path, telegram_env, monkeypatch, now):
    monkeypatch.setenv("SEC_CONTACT_EMAIL", "me@example.com")
    fake = FakeTelegram()
    fake.add_message("/watch AAPL")
    db = str(tmp_path / "state.db")
    cli.main(["--db", db, "run", "--no-digest"], http=make_http(e2e_handler(fake), retries=0), now=now)
    assert fake.sent[0]["text"].startswith("✅ Додано AAPL (Apple Inc.)")
    fake.calls.clear()
    cli.main(["--db", db, "digest", "--force"], http=make_http(e2e_handler(fake), retries=0),
             now=now + timedelta(hours=1))
    (digest,) = [p["text"] for p in fake.sent]
    assert "<b>AAPL</b> · Apple Inc. · <i>Electronic computers</i>" in digest
    assert "AAPL: SEC 8-K filed — 2.02 results of operations (earnings)" in digest
    assert "Останні звіти SEC" in digest
