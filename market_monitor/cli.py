"""Command line entry point.

    python -m market_monitor run [--dry-run] [--no-digest]  commands → collect → score → alerts → digest if due
    python -m market_monitor digest [--dry-run] [--force]   send the daily digest (if due, or forced)
    python -m market_monitor commands [--dry-run]           only process pending bot commands
    python -m market_monitor status                         print source health
    python -m market_monitor explain "headline"             show how a headline would be scored

--dry-run prints Telegram messages instead of sending them and does not consume
bot updates (the real bot will still see pending commands).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from market_monitor import pipeline
from market_monitor.alerts import AlertManager, AlertView
from market_monitor.collectors import build_collectors
from market_monitor.collectors.sec_edgar import configure_http as configure_sec_http
from market_monitor.collectors.sec_edgar import load_ticker_map
from market_monitor.config import Config, Secrets, load_config
from market_monitor.digest import build_digest, digest_due, last_digest_at, mark_digest_sent
from market_monitor.http import HttpClient
from market_monitor.logging_setup import setup_logging
from market_monitor.models import Item
from market_monitor.notify import formatting as fmt
from market_monitor.notify.base import ConsoleNotifier, Notifier
from market_monitor.notify.commands import CommandProcessor
from market_monitor.notify.telegram import TelegramClient, TelegramError, TelegramNotifier
from market_monitor.scoring import Scorer
from market_monitor.scoring.sentiment import load_sentiment
from market_monitor.settings import Settings
from market_monitor.storage import Storage, iso
from market_monitor.watchlist import WatchlistMatcher

log = logging.getLogger("market_monitor")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="market_monitor")
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml")
    parser.add_argument("--db", default=None, help="SQLite path (overrides storage.path)")
    parser.add_argument("--log-level", default="INFO")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="process commands, collect, score, alert, digest if due")
    run.add_argument("--dry-run", action="store_true", help="print messages instead of sending")
    run.add_argument("--only", nargs="*", help="run only these sources (e.g. rss gdelt)")
    run.add_argument("--no-digest", action="store_true", help="never send the digest from this run")
    digest = sub.add_parser("digest", help="send the daily digest")
    digest.add_argument("--dry-run", action="store_true")
    digest.add_argument("--force", action="store_true", help="send even if not due yet")
    commands = sub.add_parser("commands", help="process pending bot commands only")
    commands.add_argument("--dry-run", action="store_true")
    sub.add_parser("status", help="print source health")
    state = sub.add_parser("state", help="pack/unpack the encrypted state file (CI)")
    state.add_argument("action", choices=["pack", "unpack"])
    state.add_argument("--file", required=True, help="packed state file (.db.enc)")
    state.add_argument("--allow-plaintext", action="store_true",
                       help="pack without STATE_ENCRYPTION_KEY (never for a public repo)")
    explain = sub.add_parser("explain", help="score a headline and show why")
    explain.add_argument("title")
    explain.add_argument("--text", default="")
    explain.add_argument("--source", default="manual")
    explain.add_argument("--tags", nargs="*", default=[])
    return parser


class App:
    """Wires storage, HTTP, Telegram and settings for one CLI invocation."""

    def __init__(self, args: argparse.Namespace, base: Config, secrets: Secrets, now: datetime,
                 http: HttpClient | None, stack: ExitStack) -> None:
        self.args, self.base, self.secrets, self.now = args, base, secrets, now
        self.dry_run = bool(getattr(args, "dry_run", False))
        self.storage = stack.enter_context(Storage(args.db or base.storage.path))
        self.http = stack.enter_context(http or HttpClient(base.http))
        if secrets.sec_contact_email:
            # Also covers sec.gov RSS feeds, not only the EDGAR collector.
            configure_sec_http(self.http, secrets.sec_contact_email,
                               base.sources.sec_edgar.min_interval_s)
        self.settings = Settings(base, self.storage)
        self.telegram: TelegramClient | None = None
        self.telegram_problem: str | None = None
        if secrets.telegram_bot_token and secrets.telegram_chat_id:
            self.telegram = TelegramClient(self.http, secrets.telegram_bot_token, base.telegram)
        else:
            self.telegram_problem = "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set"
        self.notifier: Notifier
        if self.dry_run or self.telegram is None:
            if not self.dry_run:
                log.error("%s: printing messages instead of sending", self.telegram_problem)
            self.notifier = ConsoleNotifier()
        else:
            self.notifier = TelegramNotifier(self.telegram, secrets.telegram_chat_id)

    @property
    def config(self) -> Config:
        return self.settings.effective()

    def process_commands(self) -> None:
        if self.telegram is None:
            return
        processor = CommandProcessor(
            self.telegram, self.secrets.telegram_chat_id, self.settings, self.notifier,
            status=self.status_text, lookup=self.lookup_ticker, save_offset=not self.dry_run)
        handled = processor.process()
        if handled:
            log.info("handled %d bot commands", handled)

    def lookup_ticker(self, ticker: str) -> tuple[str, int | None] | None:
        if not self.secrets.sec_contact_email:
            return None
        mapping = load_ticker_map(self.http, self.storage, self.now,
                                  self.base.sources.sec_edgar.tickers_cache_hours)
        entry = mapping.get(ticker)
        return (entry["title"], entry["cik"]) if entry else None

    def status_text(self) -> str:
        config, storage, now = self.config, self.storage, self.now
        day_ago = now - timedelta(hours=24)
        counts = storage.alert_counts_since(day_ago)
        last = last_digest_at(storage)
        sentiment = load_sentiment(config.scoring.sentiment)
        stats = {
            "items_24h": storage.conn.execute(
                "SELECT COUNT(*) FROM items WHERE fetched_at >= ?", (iso(day_ago),)).fetchone()[0],
            "sent_24h": counts.get("sent", 0),
            "rate_limited_24h": counts.get("rate_limited", 0),
            "queued": len(storage.alerts_by_status("queued")),
            "last_digest": fmt.local_time(config, last, "%d.%m %H:%M") if last else None,
            "sentiment": sentiment.name if sentiment else "вимкнено",
        }
        return fmt.format_status(config, storage.source_health(now), stats, now)

    def send_digest(self, force: bool = False) -> bool:
        config = self.config
        if not force and not digest_due(config, self.storage, self.now):
            return False
        data = build_digest(config, self.storage, self.now)
        try:
            for part in fmt.format_digest(data, config):
                self.notifier.send(part)
        except TelegramError as exc:
            log.error("digest send failed (will retry next run): %s", exc)
            return False
        if not self.dry_run:
            mark_digest_sent(self.storage, self.now)
        log.info("digest sent (%d items)", sum(len(v) for v in data.items_by_topic.values()))
        return True


def alert_views(report: pipeline.RunReport, storage: Storage) -> list[AlertView]:
    pairs = [(i, s) for i, s in report.scored] + [(i, s) for i, s, _ in report.rescored]
    mentions = storage.mentions(i.id for i, _ in pairs)
    return [
        AlertView(item_id=i.id, title=i.title, url=i.url, text=i.text, source=i.source,
                  published_at=i.published_at, topic=s.topic, score=s.score, flags=s.flags,
                  sentiment_label=s.sentiment_label, mentions=mentions.get(i.id, 1))
        for i, s in pairs
    ]


def cmd_run(app: App) -> int:
    if app.dry_run:
        log.info("dry-run: bot commands are not consumed")
    app.process_commands()
    config = app.config  # includes changes made by commands just now
    matcher = WatchlistMatcher(config.watchlist)
    report = pipeline.collect(config, app.secrets, app.storage, app.http, app.now,
                              collectors=build_collectors(config, only=app.args.only),
                              enrich=matcher.enrich if matcher else None)
    pipeline.score(report, config, app.storage, Scorer(config), app.now)
    outcome = AlertManager(config, app.storage, app.notifier, app.dry_run).process(
        alert_views(report, app.storage), app.now)
    if not app.args.no_digest:
        app.send_digest()
    log.info(
        "run finished: %d new items, alerts sent=%d queued=%d flushed=%d rate_limited=%d; %s",
        len(report.new_items), len(outcome.sent), len(outcome.queued), len(outcome.flushed),
        len(outcome.rate_limited),
        ", ".join(f"{k}={v:.2f}s" for k, v in report.timings.items()),
    )
    write_step_summary(report, outcome)
    if app.telegram is None and not app.dry_run:
        return 3
    return 1 if report.sources and len(report.failed_sources) == len(report.sources) else 0


def write_step_summary(report: pipeline.RunReport, outcome) -> None:
    """Markdown run report for the GitHub Actions job page (no secrets, no item texts)."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [
        "### Market Monitor run",
        "",
        "| stage | seconds |", "|---|---|",
        *[f"| {k} | {v:.1f} |" for k, v in report.timings.items()],
        "",
        f"New items: **{len(report.new_items)}** · alerts sent {len(outcome.sent)}, "
        f"queued {len(outcome.queued)}, flushed {len(outcome.flushed)}, "
        f"rate-limited {len(outcome.rate_limited)}",
        "",
        "| source | ok | fetched | new | dup | seconds | error |", "|---|---|---|---|---|---|---|",
        *[f"| {s.source} | {'✅' if s.ok else '❌'} | {s.fetched} | {s.new} | {s.duplicates} | "
          f"{s.duration_s:.1f} | {(s.error or '').replace('|', '/')[:200]} |" for s in report.sources],
    ]
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def cmd_digest(app: App) -> int:
    app.process_commands()
    sent = app.send_digest(force=app.args.force)
    if not sent:
        log.info("digest not due (digest_time %s); use --force to send now", app.config.digest_time)
    return 0


def cmd_status(app: App) -> int:
    rows = app.storage.source_health(app.now)
    print(f"items stored: {app.storage.count_items()}")
    for h in rows:
        state = "OK " if h.ok else "ERR"
        print(f"{state} {h.source:<12} last={h.last_run:%Y-%m-%d %H:%M}Z new24h={h.new_24h}"
              + (f" error={h.last_error}" if h.last_error else ""))
    return 0


def cmd_explain(args: argparse.Namespace, config: Config, now: datetime) -> int:
    item = Item(source=args.source, published_at=now, title=args.title, text=args.text,
                url="https://example.invalid/explain", tags=args.tags)
    (result,) = Scorer(config).score([item])
    print(f"score:     {result.score}")
    print(f"topic:     {result.topic}")
    print(f"flags:     {', '.join(result.flags) or '-'}")
    tone = f"{result.sentiment_label} ({result.sentiment:+.2f}) via {result.sentiment_model}" \
        if result.sentiment is not None else f"not run (below prefilter {config.scoring.prefilter_min})"
    print(f"sentiment: {tone}")
    print("reasons:")
    for reason in result.reasons or ["(nothing matched)"]:
        print(f"  {reason}")
    from market_monitor.alerts import AlertView as AV, alert_reason
    view = AV(item_id=item.id, title=item.title, url=item.url, text="", source=item.source,
              published_at=now, topic=result.topic, score=result.score, flags=result.flags)
    print(f"alert:     {alert_reason(view, config) or 'no (digest only)'}")
    return 0


def cmd_state(args: argparse.Namespace, config: Config, secrets: Secrets) -> int:
    from market_monitor.state import StateError, pack, unpack

    db = args.db or config.storage.path
    key = secrets.state_encryption_key
    try:
        if args.action == "pack":
            if not key and not args.allow_plaintext:
                log.error("STATE_ENCRYPTION_KEY is not set; refusing to write unencrypted state")
                return 2
            size = pack(db, args.file, key or None)
            log.info("state packed: %s (%.1f KB%s)", args.file, size / 1024,
                     "" if key else ", NOT encrypted")
        else:
            unpack(args.file, db, key or None)
            log.info("state restored into %s", db)
    except StateError as exc:
        log.error("state %s failed: %s", args.action, exc)
        return 2
    return 0


def main(argv: list[str] | None = None, http: HttpClient | None = None,
         now: datetime | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    secrets = Secrets.from_env()
    setup_logging(args.log_level, secrets.values())
    try:
        config = load_config(args.config)
    except Exception as exc:
        log.error("config error: %s", exc)
        return 2
    now = now or datetime.now(timezone.utc)
    if args.command == "state":
        return cmd_state(args, config, secrets)
    if args.command == "explain":
        return cmd_explain(args, config, now)
    with ExitStack() as stack:
        app = App(args, config, secrets, now, http, stack)
        if args.command == "run":
            return cmd_run(app)
        if args.command == "digest":
            return cmd_digest(app)
        if args.command == "commands":
            app.process_commands()
            return 0
        if args.command == "status":
            return cmd_status(app)
    return 2


if __name__ == "__main__":
    sys.exit(main())
