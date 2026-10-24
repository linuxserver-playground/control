"""
Plain sqlite3, one short-lived connection per call. FastAPI runs sync handlers in a threadpool and sqlite
connections are not shareable across threads, so opening per call is both the simple and the correct
choice at this size. Schema changes are numbered migrations applied in order; `schema_version` in `kv`
remembers how far we got, which is why `kv` itself is created outside the migrations.
"""

import datetime
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pydantic import BaseModel

MIGRATIONS: list[str] = [
    # 1: webhook deliveries, stored whole so handlers added later can replay them.
    """
    CREATE TABLE deliveries (
        id INTEGER PRIMARY KEY,
        delivery_id TEXT NOT NULL UNIQUE,
        event TEXT NOT NULL,
        action TEXT,
        repo TEXT,
        installation_id INTEGER,
        received_at TEXT NOT NULL,
        payload TEXT NOT NULL
    );
    CREATE INDEX deliveries_received_at ON deliveries (received_at DESC);
    """,
]


class Delivery(BaseModel):
    id: int
    delivery_id: str
    event: str
    action: str | None
    repo: str | None
    installation_id: int | None
    received_at: str


def utcnow() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """A connection that is closed on exit (sqlite3's own context manager only ends the transaction)."""
        conn = sqlite3.connect(self.path, isolation_level=None)  # autocommit; migrations use explicit BEGIN
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            yield conn
        finally:
            conn.close()

    def migrate(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            # small state: installation id, setup state token, schema version
            conn.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            row = conn.execute("SELECT value FROM kv WHERE key = 'schema_version'").fetchone()
            current = int(row["value"]) if row else 0
            for number, sql in enumerate(MIGRATIONS[current:], start=current + 1):
                conn.execute("BEGIN")
                for statement in _statements(sql):
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO kv (key, value) VALUES ('schema_version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (str(number),),
                )
                conn.execute("COMMIT")

    def healthy(self) -> bool:
        try:
            with self.connect() as conn:
                conn.execute("SELECT 1 FROM kv LIMIT 1")
        except sqlite3.Error:
            return False
        return True

    # -- kv -------------------------------------------------------------------------------------------------------

    def get(self, key: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def set(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def delete(self, key: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM kv WHERE key = ?", (key,))

    # -- deliveries -----------------------------------------------------------------------------------------------

    def insert_delivery(
        self,
        delivery_id: str,
        event: str,
        payload: dict[str, Any],
        received_at: str | None = None,
    ) -> bool:
        """Store one webhook delivery. Returns False when GitHub redelivered something we already have."""
        repo = payload.get("repository")
        installation = payload.get("installation")
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO deliveries "
                "(delivery_id, event, action, repo, installation_id, received_at, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    delivery_id,
                    event,
                    payload.get("action"),
                    repo.get("full_name") if isinstance(repo, dict) else None,
                    installation.get("id") if isinstance(installation, dict) else None,
                    received_at or utcnow(),
                    json.dumps(payload, separators=(",", ":")),
                ),
            )
        return cursor.rowcount == 1

    def count_deliveries(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM deliveries").fetchone()
        return int(row["n"])

    def recent_deliveries(self, limit: int = 20) -> list[Delivery]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, delivery_id, event, action, repo, installation_id, received_at FROM deliveries "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [Delivery(**dict(row)) for row in rows]


def _statements(sql: str) -> list[str]:
    """Split a migration into statements so each runs inside our explicit transaction (executescript commits)."""
    return [part.strip() for part in sql.split(";") if part.strip()]
