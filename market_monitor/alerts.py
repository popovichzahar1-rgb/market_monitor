"""Urgent alerts: selection, quiet hours, hourly rate limit, history.

Decision for one scored item (first match wins):
1. topic muted via /topics                          -> no alert
2. a flag listed in `alerts.always` and score >= its minimum
3. urgent category (rule with `alert: true` or `urgent_data_flags`)
   and score >= urgent_threshold
4. watchlist company / BTC / ETH and score >= priority_threshold
5. score >= threshold
Every item is alerted at most once (alerts.item_id is UNIQUE).
During quiet hours alerts are queued and sent as one message afterwards.
Beyond `max_per_hour` they are marked rate_limited and left for the digest.
Send failures are re-queued and retried with the next batch.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from market_monitor.config import Config
from market_monitor.feedback import buttons_for
from market_monitor.notify.base import Notifier
from market_monitor.notify.formatting import format_alert, format_quiet_batch
from market_monitor.notify.telegram import TelegramError
from market_monitor.storage import Storage, from_iso

log = logging.getLogger(__name__)


@dataclass
class AlertView:
    """Everything needed to render an alert, from a fresh score or a stored row."""
    item_id: str
    title: str
    url: str
    text: str
    source: str
    published_at: datetime
    topic: str
    score: float
    flags: list[str]
    sentiment_label: str | None = None
    mentions: int = 1
    reason: str = ""


@dataclass
class AlertOutcome:
    sent: list[AlertView] = field(default_factory=list)
    queued: list[AlertView] = field(default_factory=list)
    rate_limited: list[AlertView] = field(default_factory=list)
    flushed: list[AlertView] = field(default_factory=list)
    failed: list[AlertView] = field(default_factory=list)


def in_quiet_hours(config: Config, now: datetime) -> bool:
    quiet = config.alerts.quiet_hours
    if quiet is None:
        return False
    local = now.astimezone(config.tz).time()
    start, end = time.fromisoformat(quiet.start), time.fromisoformat(quiet.end)
    if start == end:
        return False
    if start < end:
        return start <= local < end
    return local >= start or local < end


def urgent_flags(config: Config) -> set[str]:
    return {r.name for r in config.scoring.rules if r.alert} | set(config.alerts.urgent_data_flags)


def alert_reason(view: AlertView, config: Config) -> str | None:
    cfg = config.alerts
    if view.topic in config.muted_topics:
        return None
    flags = set(view.flags)
    for flag, minimum in cfg.always.items():
        if flag in flags and view.score >= minimum:
            return f"always:{flag}"
    urgent = sorted(flags & urgent_flags(config))
    if urgent and view.score >= cfg.urgent_threshold:
        return f"urgent:{urgent[0]}"
    priority = sorted(flags & set(cfg.priority_flags))
    if priority and view.score >= cfg.priority_threshold:
        return f"priority:{priority[0]}"
    if view.score >= cfg.threshold:
        return "threshold"
    return None


def view_from_row(row) -> AlertView:
    return AlertView(
        item_id=row["item_id"] if "item_id" in row.keys() else row["id"],
        title=row["title"], url=row["url"], text=row["text"], source=row["source"],
        published_at=from_iso(row["published_at"]), topic=row["topic"] or "other",
        score=row["score"], flags=json.loads(row["flags"]),
        sentiment_label=row["sentiment_label"], mentions=row["mentions"],
        reason=row["reason"] if "reason" in row.keys() else "",
    )


class AlertManager:
    def __init__(self, config: Config, storage: Storage, notifier: Notifier,
                 dry_run: bool = False) -> None:
        self.config = config
        self.storage = storage
        self.notifier = notifier
        self.sent_status = "dry_run" if dry_run else "sent"

    def select(self, views: list[AlertView], now: datetime) -> list[AlertView]:
        max_age = timedelta(hours=self.config.alerts.max_age_hours)
        chosen = []
        for view in views:
            if now - view.published_at > max_age or self.storage.alert_exists(view.item_id):
                continue
            reason = alert_reason(view, self.config)
            if reason:
                view.reason = reason
                chosen.append(view)
        return sorted(chosen, key=lambda v: v.score, reverse=True)

    def process(self, views: list[AlertView], now: datetime) -> AlertOutcome:
        outcome = AlertOutcome()
        if not self.config.alerts.enabled:
            return outcome
        candidates = self.select(views, now)
        if in_quiet_hours(self.config, now):
            for view in candidates:
                self.storage.record_alert(view.item_id, "queued", view.reason, view.score, now)
                outcome.queued.append(view)
            if candidates:
                log.info("quiet hours: queued %d alerts", len(candidates))
            return outcome

        self._flush_queue(now, outcome)

        hour_ago = now - timedelta(hours=1)
        remaining = max(0, self.config.alerts.max_per_hour - self.storage.alerts_sent_since(hour_ago))
        for view in candidates:
            if remaining <= 0:
                self.storage.record_alert(view.item_id, "rate_limited", view.reason, view.score, now)
                outcome.rate_limited.append(view)
                continue
            try:
                message_id = self.notifier.send(format_alert(view, self.config),
                                                buttons_for([view.item_id]))
            except TelegramError as exc:
                log.error("alert send failed, re-queued: %s", exc)
                self.storage.record_alert(view.item_id, "queued", view.reason, view.score, now)
                outcome.failed.append(view)
                continue
            self.storage.record_alert(view.item_id, self.sent_status, view.reason, view.score,
                                      now, message_id)
            outcome.sent.append(view)
            remaining -= 1
        if outcome.rate_limited:
            log.warning("hourly alert limit reached: %d alerts left for the digest",
                        len(outcome.rate_limited))
        return outcome

    def _flush_queue(self, now: datetime, outcome: AlertOutcome) -> None:
        """Send everything queued (quiet hours, earlier send failures) as one message."""
        rows = self.storage.alerts_by_status("queued")
        if not rows:
            return
        views = [view_from_row(r) for r in rows]
        shown = views[: self.config.alerts.quiet_batch_max]
        try:
            message_id = self.notifier.send(format_quiet_batch(views, self.config),
                                            buttons_for([v.item_id for v in shown]))
        except TelegramError as exc:
            log.error("queued alerts send failed, will retry: %s", exc)
            return
        for view in views:
            self.storage.record_alert(view.item_id, self.sent_status, view.reason, view.score,
                                      now, message_id)
        outcome.flushed.extend(views)
