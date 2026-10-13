"""Command line entry point.

    python -m market_monitor run [--dry-run]     collect (later: score, alert)
    python -m market_monitor status              print source health
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

from market_monitor.config import Config, Secrets, load_config
from market_monitor.http import HttpClient
from market_monitor.logging_setup import setup_logging
from market_monitor.notify.base import ConsoleNotifier, Notifier
from market_monitor.notify.formatting import format_run_preview
from market_monitor import pipeline
from market_monitor.storage import Storage

log = logging.getLogger("market_monitor")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="market_monitor")
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml")
    parser.add_argument("--db", default=None, help="SQLite path (overrides storage.path)")
    parser.add_argument("--log-level", default="INFO")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="collect sources and send alerts")
    run.add_argument("--dry-run", action="store_true", help="print messages instead of sending")
    run.add_argument("--only", nargs="*", help="run only these sources (e.g. rss)")
    sub.add_parser("status", help="print source health")
    return parser


def make_notifier(dry_run: bool) -> Notifier:
    if dry_run:
        return ConsoleNotifier()
    # Telegram notifier arrives in stage 4; until then fall back to console output.
    log.warning("Telegram is not implemented yet; printing messages instead")
    return ConsoleNotifier()


def cmd_run(args: argparse.Namespace, config: Config, secrets: Secrets, now: datetime,
            http: HttpClient | None = None) -> int:
    from market_monitor.collectors import build_collectors

    with Storage(args.db or config.storage.path) as storage, (
        http or HttpClient(config.http)
    ) as client:
        report = pipeline.collect(
            config, secrets, storage, client, now,
            collectors=build_collectors(config, only=args.only),
        )
        make_notifier(args.dry_run).send(format_run_preview(report, config.tz))
    log.info("run finished in %.1fs, %d new items", report.duration_s, len(report.new_items))
    return 0 if len(report.failed_sources) < len(report.sources) or not report.sources else 1


def cmd_status(args: argparse.Namespace, config: Config, now: datetime) -> int:
    with Storage(args.db or config.storage.path) as storage:
        rows = storage.source_health(now)
        print(f"items stored: {storage.count_items()}")
        for h in rows:
            state = "OK " if h.ok else "ERR"
            print(f"{state} {h.source:<12} last={h.last_run:%Y-%m-%d %H:%M}Z new24h={h.new_24h}"
                  + (f" error={h.last_error}" if h.last_error else ""))
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
    if args.command == "run":
        return cmd_run(args, config, secrets, now, http=http)
    if args.command == "status":
        return cmd_status(args, config, now)
    return 2


if __name__ == "__main__":
    sys.exit(main())
