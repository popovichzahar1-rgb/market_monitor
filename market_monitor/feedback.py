"""👍/👎 feedback on alerts, and a report that turns it into tuning suggestions.

Buttons carry `fb:u:<item_id>` / `fb:d:<item_id>` (well under Telegram's 64-byte
callback_data limit). Votes are stored in `alerts.feedback` (+1 / -1); the user
can change a vote by pressing the other button later. Nothing is tuned
automatically: the report (`/feedback`, `python -m market_monitor feedback`)
suggests which config.yaml weights to change, so behaviour stays explainable.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from market_monitor.notify.telegram import TelegramClient, TelegramError
from market_monitor.storage import Storage

log = logging.getLogger(__name__)

PREFIX = "fb"
VOTES = {"u": 1, "d": -1}
_RULE_RE = re.compile(r"^rule:(\S+)")


def buttons_for(item_ids: list[str]) -> list[list[dict]]:
    """One row per alert: [👍, 👎]. Rows are numbered when there are several."""
    rows = []
    for n, item_id in enumerate(item_ids, start=1):
        label = f"{n}. " if len(item_ids) > 1 else ""
        rows.append([
            {"text": f"{label}👍 Корисно", "callback_data": f"{PREFIX}:u:{item_id}"},
            {"text": f"{label}👎 Шум", "callback_data": f"{PREFIX}:d:{item_id}"},
        ])
    return rows


def parse_callback(data: str) -> tuple[str, int] | None:
    parts = (data or "").split(":")
    if len(parts) == 3 and parts[0] == PREFIX and parts[1] in VOTES and parts[2]:
        return parts[2], VOTES[parts[1]]
    return None


def mark_chosen(keyboard: list[list[dict]], item_id: str, vote: int) -> list[list[dict]]:
    """Keep the keyboard but show the chosen vote on the pressed row (both buttons stay,
    so the user can still change their mind)."""
    out = []
    for row in keyboard:
        new_row = []
        for button in row:
            parsed = parse_callback(button.get("callback_data", ""))
            text = button["text"].removeprefix("✅ ")
            if parsed and parsed[0] == item_id and parsed[1] == vote:
                text = "✅ " + text
            new_row.append({**button, "text": text})
        out.append(new_row)
    return out


class FeedbackHandler:
    """Callback handler for CommandProcessor (`on_callback`)."""

    def __init__(self, storage: Storage, client: TelegramClient, now: datetime) -> None:
        self.storage = storage
        self.client = client
        self.now = now

    def __call__(self, callback: dict) -> str:
        parsed = parse_callback(callback.get("data", ""))
        if parsed is None:
            return ""
        item_id, vote = parsed
        if not self.storage.set_feedback(item_id, vote, self.now):
            return "Цей алерт уже не знайдено в історії."
        message = callback.get("message") or {}
        keyboard = (message.get("reply_markup") or {}).get("inline_keyboard")
        if keyboard and message.get("message_id"):
            try:
                self.client.edit_buttons(str(message["chat"]["id"]), message["message_id"],
                                         mark_chosen(keyboard, item_id, vote))
            except TelegramError as exc:  # cosmetic only; the vote is already stored
                log.warning("could not update buttons: %s", exc)
        return "Дякую! Врахую: корисно 👍" if vote > 0 else "Дякую! Врахую: шум 👎"


# ---- report ---------------------------------------------------------------------

@dataclass
class Tally:
    up: int = 0
    down: int = 0

    @property
    def total(self) -> int:
        return self.up + self.down

    @property
    def down_ratio(self) -> float:
        return self.down / self.total if self.total else 0.0

    def add(self, vote: int) -> None:
        if vote > 0:
            self.up += 1
        else:
            self.down += 1


@dataclass
class FeedbackReport:
    days: int
    overall: Tally = field(default_factory=Tally)
    unrated: int = 0
    by_rule: dict[str, Tally] = field(default_factory=lambda: defaultdict(Tally))
    by_source: dict[str, Tally] = field(default_factory=lambda: defaultdict(Tally))
    by_topic: dict[str, Tally] = field(default_factory=lambda: defaultdict(Tally))
    by_reason: dict[str, Tally] = field(default_factory=lambda: defaultdict(Tally))
    noisy_examples: list[tuple[float, str]] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)


def build_report(storage: Storage, now: datetime, days: int = 30, min_votes: int = 3) -> FeedbackReport:
    report = FeedbackReport(days=days)
    for row in storage.feedback_rows(now - timedelta(days=days)):
        vote = row["feedback"]
        if vote is None:
            report.unrated += 1
            continue
        report.overall.add(vote)
        rules = [m.group(1) for r in json.loads(row["reasons"]) if (m := _RULE_RE.match(r))]
        flags = [f for f in json.loads(row["flags"]) if not f.startswith("asset:")]
        for name in dict.fromkeys(rules + flags):
            report.by_rule[name].add(vote)
        report.by_source[row["source"]].add(vote)
        report.by_topic[row["topic"] or "other"].add(vote)
        report.by_reason[row["reason"].split(":")[0]].add(vote)
        if vote < 0:
            report.noisy_examples.append((row["score"], row["title"]))
    report.noisy_examples.sort(reverse=True)
    report.suggestions = suggestions(report, min_votes)
    return report


def suggestions(report: FeedbackReport, min_votes: int) -> list[str]:
    out = []
    for name, t in sorted(report.by_rule.items(), key=lambda kv: -kv[1].down_ratio):
        if t.total >= min_votes and t.down_ratio >= 0.6:
            out.append(f"Правило/категорія «{name}»: {t.down} з {t.total} — шум. Зменште `weight` "
                       f"у scoring.rules або звузьте `any`/`also_any`; для категорій даних — пороги в alerts.")
    for name, t in sorted(report.by_source.items(), key=lambda kv: -kv[1].down_ratio):
        if t.total >= min_votes and t.down_ratio >= 0.6:
            out.append(f"Джерело {name}: {t.down} з {t.total} — шум. Зменште scoring.source_weights.{name}.")
    for kind, t in report.by_reason.items():
        if t.total >= min_votes and t.down_ratio >= 0.6:
            hint = {"threshold": "/threshold 85", "urgent": "/threshold urgent 70",
                    "priority": "/threshold priority 75"}.get(kind)
            if hint:
                out.append(f"Алерти через «{kind}»: {t.down} з {t.total} — шум. Спробуйте {hint}.")
    if report.overall.total >= 2 * min_votes and report.overall.down_ratio <= 0.2:
        out.append("Майже всі алерти корисні: якщо хочеться більше, знизьте пороги на 5 (/threshold).")
    for name, t in report.by_rule.items():
        if t.total >= min_votes and t.down == 0:
            out.append(f"Правило/категорія «{name}» працює добре ({t.up}👍): можна лишити як є.")
    return out
