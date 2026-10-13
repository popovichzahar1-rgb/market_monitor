"""Run orchestration: collect from every source, normalise, store."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

from market_monitor.collectors import CollectContext, Collector, build_collectors
from market_monitor.config import Config, Secrets
from market_monitor.http import HttpClient
from market_monitor.models import Item
from market_monitor.storage import Storage

log = logging.getLogger(__name__)


@dataclass
class SourceStatus:
    source: str
    ok: bool
    fetched: int = 0
    new: int = 0
    duration_s: float = 0.0
    error: str | None = None


@dataclass
class RunReport:
    started_at: datetime
    sources: list[SourceStatus] = field(default_factory=list)
    new_items: list[Item] = field(default_factory=list)
    duration_s: float = 0.0

    @property
    def failed_sources(self) -> list[SourceStatus]:
        return [s for s in self.sources if not s.ok]


def collect(
    config: Config,
    secrets: Secrets,
    storage: Storage,
    http: HttpClient,
    now: datetime,
    collectors: list[Collector] | None = None,
) -> RunReport:
    report = RunReport(started_at=now)
    run_start = time.monotonic()
    ctx = CollectContext(http=http, config=config, secrets=secrets, state=storage, now=now)
    for collector in collectors if collectors is not None else build_collectors(config):
        start = time.monotonic()
        status = SourceStatus(source=collector.name, ok=True)
        try:
            result = collector.collect(ctx)
            new_items = storage.insert_items(result.items, now)
            status.fetched, status.new = len(result.items), len(new_items)
            if result.errors:
                status.error = "; ".join(result.errors)[:500]
            report.new_items.extend(new_items)
        except Exception as exc:  # isolate failures per source
            log.exception("collector %s failed", collector.name)
            status.ok, status.error = False, f"{type(exc).__name__}: {exc}"[:500]
        status.duration_s = time.monotonic() - start
        storage.record_source_run(
            status.source, now, status.ok, status.fetched, status.new,
            status.duration_s, status.error,
        )
        log.info(
            "source %s: ok=%s fetched=%d new=%d in %.1fs",
            status.source, status.ok, status.fetched, status.new, status.duration_s,
        )
        report.sources.append(status)
    storage.prune(config.storage.retention_days, now)
    report.duration_s = time.monotonic() - run_start
    return report
