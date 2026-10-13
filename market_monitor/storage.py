"""SQLite storage: items, source health, key-value state. Single file, no WAL,
so the database can be copied/committed between CI runs as one file."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator

from market_monitor.models import Item

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

    def insert_items(self, items: Iterable[Item], now: datetime) -> list[Item]:
        """Insert items, skipping ones already stored (same id or normalised URL).

        Returns only the newly inserted items.
        """
        inserted: list[Item] = []
        with self.conn:
            for item in items:
                cur = self.conn.execute(
                    """INSERT OR IGNORE INTO items
                       (id, source, published_at, fetched_at, title, text, url, url_norm, tags, entities)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        item.id, item.source, iso(item.published_at), iso(now), item.title,
                        item.text, item.url, item.url_norm,
                        json.dumps(item.tags, ensure_ascii=False),
                        json.dumps(item.entities, ensure_ascii=False),
                    ),
                )
                if cur.rowcount:
                    inserted.append(item)
        return inserted

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

    # ---- source health -----------------------------------------------------

    def record_source_run(
        self, source: str, run_at: datetime, ok: bool, fetched: int, new: int,
        duration_s: float, error: str | None = None,
    ) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO source_runs (source, run_at, ok, fetched, new, duration_s, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (source, iso(run_at), int(ok), fetched, new, round(duration_s, 3), error),
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
    )
