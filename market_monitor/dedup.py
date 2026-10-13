"""Duplicate detection across sources.

A new item is a duplicate of a stored (or earlier in this run) item when:
1. its normalised URL is known (directly or as an alias of a merged duplicate);
2. its body text hash matches (syndicated copies of the same article);
3. its headline is near-identical (rapidfuzz token_sort_ratio >= threshold)
   within the time window AND both headlines share the same numbers, acronyms and
   proper names ("Fed holds rates" vs "ECB holds rates" are different stories).
   Skipped for `data` items whose titles are templated.

Duplicates are not stored as separate items: the canonical item's `mentions`
counter grows instead, which scoring later uses as a "widely reported" signal.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from rapidfuzz import fuzz, process

from market_monitor.config import DedupConfig
from market_monitor.models import DATA_TAG, Item
from market_monitor.storage import Storage
from market_monitor.textutil import content_hash, distinguishing_tokens, title_key


class Deduplicator:
    def __init__(self, storage: Storage, config: DedupConfig, now: datetime) -> None:
        self.storage = storage
        self.config = config
        self.hashes: dict[str, str] = {}
        self.title_keys: list[str] = []
        self.title_ids: list[str] = []
        self.title_names: list[set[str]] = []
        for row in storage.recent_dedup_keys(now - timedelta(hours=config.window_hours)):
            self._index(row["id"], row["text_hash"], row["title"],
                        DATA_TAG in json.loads(row["tags"]))

    def _index(self, item_id: str, text_hash: str | None, title: str, is_data: bool) -> None:
        if text_hash:
            self.hashes.setdefault(text_hash, item_id)
        tkey = title_key(title)
        if not is_data and len(tkey.split()) >= self.config.min_title_tokens:
            self.title_keys.append(tkey)
            self.title_ids.append(item_id)
            self.title_names.append(distinguishing_tokens(title))

    def find_duplicate(self, item: Item) -> str | None:
        """Id of the canonical item this one duplicates, or None if it is new."""
        known = self.storage.find_by_url(item.url_norm)
        if known:
            return known
        if item.is_data:
            return None
        text_hash = content_hash(item.text, self.config.min_text_chars_for_hash)
        if text_hash and text_hash in self.hashes:
            return self.hashes[text_hash]
        tkey = title_key(item.title)
        if len(tkey.split()) >= self.config.min_title_tokens and self.title_keys:
            names = distinguishing_tokens(item.title)
            for _, _, index in process.extract(
                tkey, self.title_keys, scorer=fuzz.token_sort_ratio,
                score_cutoff=self.config.title_similarity, limit=5,
            ):
                if self.title_names[index] == names:
                    return self.title_ids[index]
        return None

    def remember(self, item: Item) -> None:
        """Register a freshly stored item so later items in the same run dedup against it."""
        text_hash = None if item.is_data else content_hash(item.text, self.config.min_text_chars_for_hash)
        self._index(item.id, text_hash, item.title, item.is_data)
