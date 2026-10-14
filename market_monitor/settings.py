"""Runtime overrides set through bot commands, layered over config.yaml.

config.yaml holds defaults (committed, public); the database holds what the user
changed via Telegram (/watch, /threshold, /digest_time, /topics). Overrides are
stored as JSON under kv keys `override:<name>`; `effective_config` merges them.
"""

from __future__ import annotations

import json
from typing import Any

from market_monitor.config import Config, WatchItem, parse_hhmm

PREFIX = "override:"
THRESHOLD_KINDS = {"general": "threshold", "urgent": "urgent_threshold",
                   "priority": "priority_threshold"}


class Settings:
    def __init__(self, base: Config, storage) -> None:
        self.base = base
        self.storage = storage

    def _get(self, name: str) -> Any:
        raw = self.storage.kv_get(PREFIX + name)
        return json.loads(raw) if raw is not None else None

    def _set(self, name: str, value: Any) -> None:
        self.storage.kv_set(PREFIX + name, json.dumps(value, ensure_ascii=False))

    def effective(self) -> Config:
        """config.yaml with all stored overrides applied (validated again)."""
        data = self.base.model_dump()
        if (watchlist := self._get("watchlist")) is not None:
            data["watchlist"] = watchlist
        if (digest_time := self._get("digest_time")) is not None:
            data["digest_time"] = digest_time
        if (muted := self._get("muted_topics")) is not None:
            data["muted_topics"] = muted
        for key in THRESHOLD_KINDS.values():
            if (value := self._get(f"alerts.{key}")) is not None:
                data["alerts"][key] = value
        return Config.model_validate(data)

    # ---- watchlist ---------------------------------------------------------

    def watchlist(self) -> list[WatchItem]:
        return self.effective().watchlist

    def add_watch(self, item: WatchItem) -> bool:
        current = self.watchlist()
        if any(w.ticker == item.ticker for w in current):
            return False
        self._set("watchlist", [w.model_dump() for w in [*current, item]])
        return True

    def remove_watch(self, ticker: str) -> bool:
        ticker = ticker.upper()
        current = self.watchlist()
        remaining = [w for w in current if w.ticker != ticker]
        if len(remaining) == len(current):
            return False
        self._set("watchlist", [w.model_dump() for w in remaining])
        return True

    # ---- scalar settings ---------------------------------------------------

    def set_threshold(self, kind: str, value: float) -> None:
        if kind not in THRESHOLD_KINDS:
            raise ValueError(kind)
        if not 0 <= value <= 100:
            raise ValueError("threshold must be within 0..100")
        self._set(f"alerts.{THRESHOLD_KINDS[kind]}", value)

    def set_digest_time(self, value: str) -> str:
        normalised = parse_hhmm(value)
        self._set("digest_time", normalised)
        return normalised

    def set_topic_muted(self, topic: str, muted: bool) -> None:
        current = set(self.effective().muted_topics)
        current.add(topic) if muted else current.discard(topic)
        self._set("muted_topics", sorted(current))
