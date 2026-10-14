"""SQLite storage: items, source health, key-value state. Single file, no WAL,
so the database can be copied/committed between CI runs as one file."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Iterator

from market_monitor.models import Item
from market_monitor.textutil import content_hash, title_key

if TYPE_CHECKING:
    from market_monitor.scoring import Score

# Each entry upgrades the schema by one version. Never edit old entries, append new ones.
MIGRATIONS: list[str] = [
    """
    CREATE TABLE items (
        id           TEXT PRIMARY KEY,
        source       TEXT NOT NULL,
        published_at TEXT NOT NULL,
        fetched_at   TEXT NOT NULL,
        title        TEXT NOT NULL,
        text         TEXT NOT NULL DEFAULT '',
        url          TEXT NOT NULL,
        url_norm     TEXT NOT NULL UNIQUE,
        tags         TEXT NOT NULL DEFAULT '[]',
        entities     TEXT NOT NULL DEFAULT '[]'
    );
    CREATE INDEX idx_items_published ON items(published_at);
    CREATE INDEX idx_items_fetched ON items(fetched_at);

    CREATE TABLE source_runs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        source      TEXT NOT NULL,
        run_at      TEXT NOT NULL,
        ok          INTEGER NOT NULL,
        fetched     INTEGER NOT NULL DEFAULT 0,
        new         INTEGER NOT NULL DEFAULT 0,
        duration_s  REAL NOT NULL DEFAULT 0,
        error       TEXT
    );
    CREATE INDEX idx_source_runs ON source_runs(source, run_at);

    CREATE TABLE kv (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
    # 2: content dedup, mention counting, structured meta
    """
    ALTER TABLE items ADD COLUMN text_hash TEXT;
    ALTER TABLE items ADD COLUMN title_key TEXT NOT NULL DEFAULT '';
    ALTER TABLE items ADD COLUMN meta TEXT NOT NULL DEFAULT '{}';
    ALTER TABLE items ADD COLUMN mentions INTEGER NOT NULL DEFAULT 1;
    ALTER TABLE items ADD COLUMN other_sources TEXT NOT NULL DEFAULT '[]';
    CREATE INDEX idx_items_text_hash ON items(text_hash);

    -- URLs of duplicates merged into a canonical item, so repeats are caught by URL.
    CREATE TABLE item_aliases (
        url_norm TEXT PRIMARY KEY,
        item_id  TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
        source   TEXT NOT NULL,
        seen_at  TEXT NOT NULL
    );
    CREATE INDEX idx_item_aliases_item ON item_aliases(item_id);

    ALTER TABLE source_runs ADD COLUMN duplicates INTEGER NOT NULL DEFAULT 0;
    """,
    # 3: scoring results
    """
    ALTER TABLE items ADD COLUMN score REAL;
    ALTER TABLE items ADD COLUMN topic TEXT;
    ALTER TABLE items ADD COLUMN flags TEXT NOT NULL DEFAULT '[]';
    ALTER TABLE items ADD COLUMN reasons TEXT NOT NULL DEFAULT '[]';
    ALTER TABLE items ADD COLUMN sentiment REAL;
    ALTER TABLE items ADD COLUMN sentiment_label TEXT;
    ALTER TABLE items ADD COLUMN scored_at TEXT;
    CREATE INDEX idx_items_score ON items(score);
    """,
    # 4: alert history (one row per item ever considered for an alert)
    """
    CREATE TABLE alerts (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id     TEXT NOT NULL UNIQUE,
        created_at  TEXT NOT NULL,
        sent_at     TEXT,
        -- sent | queued (quiet hours) | rate_limited | dry_run | failed
        status      TEXT NOT NULL,
        reason      TEXT NOT NULL,
        score       REAL NOT NULL,
        message_id  INTEGER,
        feedback    INTEGER,
        feedback_at TEXT
    );
    CREATE INDEX idx_alerts_status ON alerts(status, sent_at);
    """,
]


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def from_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


@dataclass
class SourceHealth:
    source: str
    last_run: datetime
    ok: bool
    last_error: str | None
    last_success: datetime | None
    new_24h: int


class Storage:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=DELETE")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.migrate()

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    @property
    def schema_version(self) -> int:
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    def migrate(self) -> None:
        version = self.schema_version
        for index, script in enumerate(MIGRATIONS[version:], start=version + 1):
            with self.conn:
                self.conn.executescript(script)
                self.conn.execute(f"PRAGMA user_version = {index}")

    # ---- items -------------------------------------------------------------

    def insert_item(self, item: Item, now: datetime, hash_min_chars: int = 120) -> bool:
        """Insert one item; returns False when its id or normalised URL already exists."""
        with self.conn:
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO items
                   (id, source, published_at, fetched_at, title, text, url, url_norm, tags,
                    entities, meta, text_hash, title_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    item.id, item.source, iso(item.published_at), iso(now), item.title,
                    item.text, item.url, item.url_norm,
                    json.dumps(item.tags, ensure_ascii=False),
                    json.dumps(item.entities, ensure_ascii=False),
                    json.dumps(item.meta, ensure_ascii=False, default=str),
                    None if item.is_data else content_hash(item.text, hash_min_chars),
                    title_key(item.title),
                ),
            )
        return bool(cur.rowcount)

    def insert_items(self, items: Iterable[Item], now: datetime) -> list[Item]:
        """Insert items skipping known URLs (no content dedup). Returns the new ones."""
        return [item for item in items if self.insert_item(item, now)]

    def find_by_url(self, url_norm: str) -> str | None:
        """Id of the item stored under this normalised URL, directly or as an alias."""
        row = self.conn.execute(
            "SELECT id FROM items WHERE url_norm = ? UNION ALL "
            "SELECT item_id FROM item_aliases WHERE url_norm = ? LIMIT 1",
            (url_norm, url_norm),
        ).fetchone()
        return row[0] if row else None

    def recent_dedup_keys(self, since: datetime) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT id, text_hash, title, title_key, tags FROM items WHERE published_at >= ?",
            (iso(since),),
        ).fetchall()

    def add_mention(self, item_id: str, duplicate: Item, now: datetime) -> bool:
        """Merge a duplicate into its canonical item: count it and remember its URL/source.

        Returns False when this exact URL was already merged before (not a new mention).
        """
        with self.conn:
            row = self.conn.execute(
                "SELECT other_sources FROM items WHERE id = ?", (item_id,)
            ).fetchone()
            if row is None:
                return False
            known_alias = self.conn.execute(
                "SELECT 1 FROM item_aliases WHERE url_norm = ?", (duplicate.url_norm,)
            ).fetchone()
            if known_alias or duplicate.url_norm == self.conn.execute(
                "SELECT url_norm FROM items WHERE id = ?", (item_id,)
            ).fetchone()[0]:
                return False
            sources = json.loads(row["other_sources"])
            if duplicate.source not in sources:
                sources.append(duplicate.source)
            self.conn.execute(
                "UPDATE items SET mentions = mentions + 1, other_sources = ? WHERE id = ?",
                (json.dumps(sources), item_id),
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO item_aliases (url_norm, item_id, source, seen_at) "
                "VALUES (?, ?, ?, ?)",
                (duplicate.url_norm, item_id, duplicate.source, iso(now)),
            )
        return True

    def mentions(self, ids: Iterable[str]) -> dict[str, int]:
        return {r["id"]: r["mentions"] for r in self.load_items(ids)}

    def save_score(self, item_id: str, score: "Score", now: datetime) -> None:
        with self.conn:
            self.conn.execute(
                """UPDATE items SET score = ?, topic = ?, flags = ?, reasons = ?,
                   sentiment = COALESCE(?, sentiment),
                   sentiment_label = COALESCE(?, sentiment_label), scored_at = ?
                   WHERE id = ?""",
                (score.score, score.topic, json.dumps(score.flags), json.dumps(score.reasons),
                 score.sentiment, score.sentiment_label, iso(now), item_id),
            )

    def load_items(self, ids: Iterable[str]) -> list[sqlite3.Row]:
        ids = list(ids)
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        return self.conn.execute(f"SELECT * FROM items WHERE id IN ({marks})", ids).fetchall()

    def get_item_row(self, item_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()

    def count_items(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]

    def iter_items(self, since: datetime | None = None) -> Iterator[Item]:
        query, args = "SELECT * FROM items", ()
        if since is not None:
            query, args = query + " WHERE published_at >= ?", (iso(since),)
        for row in self.conn.execute(query + " ORDER BY published_at DESC", args):
            yield row_to_item(row)

    def prune(self, retention_days: int, now: datetime) -> int:
        cutoff = iso(now - timedelta(days=retention_days))
        with self.conn:
            removed = self.conn.execute("DELETE FROM items WHERE fetched_at < ?", (cutoff,)).rowcount
            self.conn.execute("DELETE FROM source_runs WHERE run_at < ?", (cutoff,))
        return removed

    def items_fetched_since(self, since: datetime) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM items WHERE fetched_at >= ? AND score IS NOT NULL "
            "ORDER BY score DESC, published_at DESC",
            (iso(since),),
        ).fetchall()

    # ---- alerts ------------------------------------------------------------

    def alert_exists(self, item_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM alerts WHERE item_id = ?", (item_id,)).fetchone() is not None

    def record_alert(self, item_id: str, status: str, reason: str, score: float,
                     now: datetime, message_id: int | None = None) -> int:
        sent_at = iso(now) if status in ("sent", "dry_run") else None
        with self.conn:
            cur = self.conn.execute(
                """INSERT INTO alerts (item_id, created_at, sent_at, status, reason, score, message_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(item_id) DO UPDATE SET status = excluded.status,
                     sent_at = COALESCE(excluded.sent_at, alerts.sent_at),
                     message_id = COALESCE(excluded.message_id, alerts.message_id)""",
                (item_id, iso(now), sent_at, status, reason, score, message_id),
            )
        return cur.lastrowid

    def set_alert_message(self, item_ids: list[str], message_id: int | None) -> None:
        with self.conn:
            self.conn.executemany(
                "UPDATE alerts SET message_id = ? WHERE item_id = ?",
                [(message_id, i) for i in item_ids])

    def alerts_sent_since(self, since: datetime) -> int:
        """Telegram messages sent (a morning batch counts once)."""
        return self.conn.execute(
            "SELECT COUNT(DISTINCT COALESCE(message_id, id)) FROM alerts "
            "WHERE status = 'sent' AND sent_at >= ?", (iso(since),)).fetchone()[0]

    def alerts_by_status(self, status: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT a.*, i.title, i.url, i.topic, i.source, i.published_at, i.text, i.flags, "
            "i.sentiment_label, i.mentions FROM alerts a JOIN items i ON i.id = a.item_id "
            "WHERE a.status = ? ORDER BY a.score DESC", (status,)).fetchall()

    def alert_counts_since(self, since: datetime) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM alerts WHERE created_at >= ? GROUP BY status",
            (iso(since),)).fetchall()
        return {r["status"]: r["n"] for r in rows}

    # ---- source health -----------------------------------------------------

    def record_source_run(
        self, source: str, run_at: datetime, ok: bool, fetched: int, new: int,
        duration_s: float, error: str | None = None, duplicates: int = 0,
    ) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO source_runs
                   (source, run_at, ok, fetched, new, duration_s, error, duplicates)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (source, iso(run_at), int(ok), fetched, new, round(duration_s, 3), error,
                 duplicates),
            )

    def source_health(self, now: datetime) -> list[SourceHealth]:
        day_ago = iso(now - timedelta(hours=24))
        rows = self.conn.execute(
            """
            SELECT s.source,
                   MAX(s.run_at) AS last_run,
                   (SELECT ok FROM source_runs x WHERE x.source = s.source ORDER BY run_at DESC, id DESC LIMIT 1) AS ok,
                   (SELECT error FROM source_runs x WHERE x.source = s.source ORDER BY run_at DESC, id DESC LIMIT 1) AS error,
                   (SELECT MAX(run_at) FROM source_runs x WHERE x.source = s.source AND x.ok = 1) AS last_success,
                   (SELECT COALESCE(SUM(new), 0) FROM source_runs x WHERE x.source = s.source AND x.run_at >= ?) AS new_24h
            FROM source_runs s GROUP BY s.source ORDER BY s.source
            """,
            (day_ago,),
        ).fetchall()
        return [
            SourceHealth(
                source=r["source"], last_run=from_iso(r["last_run"]), ok=bool(r["ok"]),
                last_error=r["error"],
                last_success=from_iso(r["last_success"]) if r["last_success"] else None,
                new_24h=r["new_24h"],
            )
            for r in rows
        ]

    # ---- key/value state ---------------------------------------------------

    def kv_get(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def kv_set(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def kv_delete(self, key: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM kv WHERE key = ?", (key,))


def row_to_item(row: sqlite3.Row) -> Item:
    return Item(
        id=row["id"], source=row["source"], published_at=from_iso(row["published_at"]),
        title=row["title"], text=row["text"], url=row["url"],
        tags=json.loads(row["tags"]), entities=json.loads(row["entities"]),
        meta=json.loads(row["meta"]),
    )
