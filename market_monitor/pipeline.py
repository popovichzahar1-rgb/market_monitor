"""Run orchestration: collect from every source, normalise, store."""

from __future__ import annotations

import dataclasses
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from market_monitor.collectors import CollectContext, CollectResult, Collector, build_collectors
from market_monitor.config import Config, Secrets
from market_monitor.dedup import Deduplicator
from market_monitor.http import HttpClient
from market_monitor.models import Item
from market_monitor.scoring import Score, Scorer
from market_monitor.scoring.sentiment import Sentiment
from market_monitor.storage import Storage, row_to_item

log = logging.getLogger(__name__)


@dataclass
class SourceStatus:
    source: str
    ok: bool
    fetched: int = 0
    new: int = 0
    duplicates: int = 0
    duration_s: float = 0.0
    error: str | None = None


@dataclass
class RunReport:
    started_at: datetime
    sources: list[SourceStatus] = field(default_factory=list)
    new_items: list[Item] = field(default_factory=list)
    # Older items that got new mentions (duplicates from other sources) this run.
    bumped_ids: set[str] = field(default_factory=set)
    scored: list[tuple[Item, Score]] = field(default_factory=list)
    # (item, new score, previous score) for bumped items whose score was recomputed.
    rescored: list[tuple[Item, Score, float | None]] = field(default_factory=list)
    duration_s: float = 0.0
    timings: dict[str, float] = field(default_factory=dict)

    @property
    def failed_sources(self) -> list[SourceStatus]:
        return [s for s in self.sources if not s.ok]


class BufferedState:
    """Collector-side key/value state: reads go to the database under its lock,
    writes are buffered and flushed by the main thread after the collector ends."""

    def __init__(self, storage: Storage) -> None:
        self.storage = storage
        self.pending: dict[str, str] = {}

    def kv_get(self, key: str, default: str | None = None) -> str | None:
        if key in self.pending:
            return self.pending[key]
        with self.storage.lock:
            return self.storage.kv_get(key, default)

    def kv_set(self, key: str, value: str) -> None:
        self.pending[key] = value

    def flush(self) -> None:
        for key, value in self.pending.items():
            self.storage.kv_set(key, value)
        self.pending.clear()


def _run_collector(collector: Collector, ctx: CollectContext) -> tuple[CollectResult | None, Exception | None, float]:
    start = time.monotonic()
    try:
        return collector.collect(ctx), None, time.monotonic() - start
    except Exception as exc:  # isolate failures per source
        log.exception("collector %s failed", collector.name)
        return None, exc, time.monotonic() - start


def collect(
    config: Config,
    secrets: Secrets,
    storage: Storage,
    http: HttpClient,
    now: datetime,
    collectors: list[Collector] | None = None,
    enrich: Callable[[Item], Item] | None = None,
) -> RunReport:
    """Collect from all sources in parallel threads; dedup, enrich and store results
    in the main thread in a fixed collector order (deterministic). `enrich` (e.g.
    watchlist matching) runs on every item before dedup and storage."""
    report = RunReport(started_at=now)
    run_start = time.monotonic()
    collectors = collectors if collectors is not None else build_collectors(config)
    dedup = Deduplicator(storage, config.dedup, now)
    base_ctx = CollectContext(http=http, config=config, secrets=secrets, state=storage, now=now)
    states = [BufferedState(storage) for _ in collectors]
    workers = max(1, min(config.collect_workers, len(collectors) or 1))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="collect") as pool:
        futures = [pool.submit(_run_collector, c, dataclasses.replace(base_ctx, state=st))
                   for c, st in zip(collectors, states)]
        for collector, state, future in zip(collectors, states, futures):
            result, error, duration = future.result()
            status = SourceStatus(source=collector.name, ok=error is None, duration_s=duration)
            with storage.lock:
                state.flush()
                if result is not None:
                    if enrich is not None:
                        result.items = [enrich(i) for i in result.items]
                    new_items = store_new(result.items, storage, dedup, now, config,
                                          report.bumped_ids)
                    status.fetched, status.new = len(result.items), len(new_items)
                    status.duplicates = status.fetched - status.new
                    if result.errors:
                        status.error = "; ".join(result.errors)[:500]
                    report.new_items.extend(new_items)
                else:
                    status.error = f"{type(error).__name__}: {error}"[:500]
                storage.record_source_run(
                    status.source, now, status.ok, status.fetched, status.new,
                    status.duration_s, status.error, status.duplicates,
                )
            log.info(
                "source %s: ok=%s fetched=%d new=%d dup=%d in %.1fs",
                status.source, status.ok, status.fetched, status.new, status.duplicates,
                status.duration_s,
            )
            report.sources.append(status)
    storage.prune(config.storage.retention_days, now)
    report.duration_s = time.monotonic() - run_start
    report.timings["collect"] = report.duration_s
    return report


def score(report: RunReport, config: Config, storage: Storage, scorer: Scorer,
          now: datetime) -> RunReport:
    """Score new items, and re-score older items whose mention count grew."""
    start = time.monotonic()
    new_ids = {i.id for i in report.new_items}
    mentions = storage.mentions(new_ids | report.bumped_ids)
    scores = scorer.score(report.new_items, mentions)
    for item, sc in zip(report.new_items, scores):
        storage.save_score(item.id, sc, now)
    report.scored = list(zip(report.new_items, scores))

    rows = storage.load_items(report.bumped_ids - new_ids)
    if rows:
        items = [row_to_item(r) for r in rows]
        known = {r["id"]: Sentiment(r["sentiment_label"], r["sentiment"], 1.0)
                 for r in rows if r["sentiment"] is not None}
        for item, sc, row in zip(items, scorer.score(items, mentions, known), rows):
            storage.save_score(item.id, sc, now)
            report.rescored.append((item, sc, row["score"]))
    report.timings["score"] = time.monotonic() - start
    report.timings["sentiment_model"] = scorer.last_model_seconds
    return report


def store_new(items: list[Item], storage: Storage, dedup: Deduplicator, now: datetime,
              config: Config, bumped: set[str] | None = None) -> list[Item]:
    """Store non-duplicate items; merge duplicates into their canonical item."""
    new_items: list[Item] = []
    for item in items:
        canonical = dedup.find_duplicate(item)
        if canonical is not None:
            if canonical != item.id and storage.add_mention(canonical, item, now) and bumped is not None:
                bumped.add(canonical)
            continue
        if storage.insert_item(item, now, config.dedup.min_text_chars_for_hash):
            dedup.remember(item)
            new_items.append(item)
    return new_items
