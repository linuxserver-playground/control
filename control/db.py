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
    # 2: the state model handlers build from deliveries, builds from /events, and the scheduler's log.
    # Every table has created_at/updated_at; GitHub's own timestamps are used when the payload has them.
    """
    CREATE TABLE repos (
        full_name TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        default_branch TEXT NOT NULL DEFAULT 'main',
        archived INTEGER NOT NULL DEFAULT 0,
        is_image INTEGER NOT NULL DEFAULT 0,
        image_name TEXT,
        last_seen TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE runs (
        id INTEGER PRIMARY KEY,
        repo TEXT NOT NULL,
        workflow_name TEXT,
        event TEXT,
        branch TEXT,
        head_sha TEXT,
        run_number INTEGER,
        status TEXT,
        conclusion TEXT,
        started_at TEXT,
        html_url TEXT,
        actor TEXT,
        display_title TEXT,
        attempt INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX runs_repo ON runs (repo, created_at DESC);
    CREATE INDEX runs_status ON runs (status);
    CREATE INDEX runs_head_sha ON runs (head_sha);
    CREATE TABLE jobs (
        id INTEGER PRIMARY KEY,
        run_id INTEGER NOT NULL,
        repo TEXT NOT NULL,
        name TEXT NOT NULL,
        status TEXT,
        conclusion TEXT,
        started_at TEXT,
        completed_at TEXT,
        runner_name TEXT,
        runner_group TEXT,
        labels TEXT NOT NULL DEFAULT '[]',
        steps_json TEXT NOT NULL DEFAULT '[]',
        html_url TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX jobs_run ON jobs (run_id);
    CREATE INDEX jobs_status ON jobs (status);
    CREATE INDEX jobs_repo_name ON jobs (repo, name);
    CREATE TABLE pull_requests (
        repo TEXT NOT NULL,
        number INTEGER NOT NULL,
        title TEXT NOT NULL,
        author TEXT,
        state TEXT NOT NULL,
        draft INTEGER NOT NULL DEFAULT 0,
        head_sha TEXT,
        head_fork INTEGER NOT NULL DEFAULT 0,
        merged_at TEXT,
        review_state TEXT,
        html_url TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (repo, number)
    );
    CREATE INDEX pull_requests_state ON pull_requests (state);
    CREATE TABLE issues (
        repo TEXT NOT NULL,
        number INTEGER NOT NULL,
        title TEXT NOT NULL,
        author TEXT,
        state TEXT NOT NULL,
        labels TEXT NOT NULL DEFAULT '[]',
        html_url TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (repo, number)
    );
    CREATE INDEX issues_state ON issues (state);
    CREATE TABLE releases (
        repo TEXT NOT NULL,
        tag TEXT NOT NULL,
        name TEXT,
        prerelease INTEGER NOT NULL DEFAULT 0,
        published_at TEXT,
        html_url TEXT,
        body TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (repo, tag)
    );
    CREATE INDEX releases_published ON releases (published_at DESC);
    CREATE TABLE builds (
        repo TEXT NOT NULL,
        run_id INTEGER NOT NULL,
        run_url TEXT,
        image TEXT NOT NULL,
        branch TEXT,
        mode TEXT,
        pull_request INTEGER,
        reason TEXT,
        result TEXT,
        failed_job TEXT,
        version_tag TEXT,
        release_tag TEXT,
        upstream TEXT,
        upstream_clean TEXT,
        semver TEXT,
        ls_build INTEGER,
        test_success INTEGER,
        release_url TEXT,
        artifact_url TEXT,
        index_digest TEXT,
        platforms TEXT NOT NULL DEFAULT '[]',
        received_at TEXT NOT NULL,
        bundle_json TEXT,
        discord_message_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (repo, run_id)
    );
    CREATE INDEX builds_image ON builds (image, mode, received_at DESC);
    CREATE TABLE scheduler_decisions (
        id INTEGER PRIMARY KEY,
        at TEXT NOT NULL,
        image TEXT NOT NULL,
        branch TEXT NOT NULL,
        outcome TEXT NOT NULL,
        detail TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX scheduler_decisions_at ON scheduler_decisions (at DESC);
    """,
    # 3: the scheduler increment. `manifests` is every live branch's image.yml (fleet discovery), `skips` the
    # flags that hold the scheduler back, `actions` what signed-in members did, and decisions get a `kind`
    # (upstream | packages | manual) so the two schedules keep separate histories.
    """
    CREATE TABLE manifests (
        repo TEXT NOT NULL,
        branch TEXT NOT NULL,
        sha TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        manifest_json TEXT NOT NULL,
        release_tag TEXT,
        version_source_json TEXT,
        package_check INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (repo, branch)
    );
    CREATE TABLE skips (
        scope TEXT PRIMARY KEY,
        reason TEXT,
        set_by TEXT NOT NULL,
        set_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE actions (
        id INTEGER PRIMARY KEY,
        at TEXT NOT NULL,
        actor TEXT NOT NULL,
        action TEXT NOT NULL,
        target TEXT,
        result TEXT NOT NULL,
        detail TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX actions_actor_at ON actions (actor, at DESC);
    ALTER TABLE scheduler_decisions ADD COLUMN kind TEXT NOT NULL DEFAULT 'upstream';
    CREATE INDEX scheduler_decisions_branch ON scheduler_decisions (image, branch, kind, id DESC);
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
                    repo.get("full_name") if isinstance(repo, dict) else repo if isinstance(repo, str) else None,
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

    def iter_deliveries(self, batch: int = 500) -> Iterator[tuple[int, str, dict[str, Any]]]:
        """Every delivery in arrival order, as (id, event, payload), for replaying through the handlers."""
        last = 0
        while True:
            with self.connect() as conn:
                rows = conn.execute(
                    "SELECT id, event, payload FROM deliveries WHERE id > ? ORDER BY id LIMIT ?", (last, batch)
                ).fetchall()
            if not rows:
                return
            for row in rows:
                last = int(row["id"])
                payload = json.loads(row["payload"])
                yield last, str(row["event"]), payload if isinstance(payload, dict) else {}


def _statements(sql: str) -> list[str]:
    """Split a migration into statements so each runs inside our explicit transaction (executescript commits)."""
    return [part.strip() for part in sql.split(";") if part.strip()]
