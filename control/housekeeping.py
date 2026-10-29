"""
Housekeeping, once a day from the scheduler's loop:

- **Delivery retention.** Every webhook is stored whole so a handler added later can replay it, which makes
  `deliveries` most of the database and grew it by about 17,000 rows a day in the playground. Deliveries older than
  `deliveries_keep_days` are deleted (in batches, so webhook intake never waits long on the lock); a replay then
  rebuilds that window, and the backfill and reconcile cover anything older. SQLite reuses the freed pages.
- **Snapshots.** A compacted copy of the database (`VACUUM INTO`, consistent while control runs) goes to
  `<data dir>/snapshots/`, keeping the newest `snapshots_keep`. They guard against a bad migration or a corrupted
  file, not against losing the disk: copy the data dir off the host for that (deploy/n97/README.md).

Each pass is `last_housekeeping` on /status. Never raises.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
from pathlib import Path
from typing import Any

from .state import State

log = logging.getLogger(__name__)

LAST_HOUSEKEEPING = "last_housekeeping"
INTERVAL = datetime.timedelta(days=1)
PRUNE_BATCH = 5000


def last_housekeeping(state: State) -> dict[str, Any] | None:
    raw = state.db.get(LAST_HOUSEKEEPING)
    loaded = json.loads(raw) if raw else None
    return loaded if isinstance(loaded, dict) else None


def due(state: State, now: datetime.datetime) -> bool:
    last = last_housekeeping(state)
    if not last or not last.get("started"):
        return True
    return now - datetime.datetime.fromisoformat(str(last["started"])) >= INTERVAL


async def tick(state: State, now: datetime.datetime) -> dict[str, Any] | None:
    """One pass when due; the record it stored, or None. Never raises."""
    if not due(state, now):
        return None
    record: dict[str, Any] = {"started": now.isoformat(timespec="seconds"), "finished": None, "errors": []}
    state.db.set(LAST_HOUSEKEEPING, json.dumps(record))
    settings = state.settings
    if settings.deliveries_keep_days > 0:
        before = (now - datetime.timedelta(days=settings.deliveries_keep_days)).isoformat(timespec="seconds")
        try:
            record["deliveries_pruned"] = await asyncio.to_thread(state.db.prune_deliveries, before, PRUNE_BATCH)
        except Exception as exc:
            log.exception("housekeeping: pruning deliveries failed")
            record["errors"].append(f"prune: {type(exc).__name__}: {exc}")
    if settings.snapshots_keep > 0:
        try:
            path = await asyncio.to_thread(snapshot, state, now, settings.snapshots_keep)
            record["snapshot"] = path.name
            record["snapshot_bytes"] = path.stat().st_size
        except Exception as exc:
            log.exception("housekeeping: snapshot failed")
            record["errors"].append(f"snapshot: {type(exc).__name__}: {exc}")
    record["finished"] = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    state.db.set(LAST_HOUSEKEEPING, json.dumps(record))
    log.info("housekeeping: %s", {k: v for k, v in record.items() if k not in ("started", "finished")})
    return record


def snapshot(state: State, now: datetime.datetime, keep: int) -> Path:
    """Write today's snapshot (replacing one already made today) and delete all but the newest `keep`."""
    folder = state.settings.data_dir / "snapshots"
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o700)  # the database holds every webhook payload: as secret as the data dir
    path = folder / f"control-{now:%Y-%m-%d}.db"
    path.unlink(missing_ok=True)  # VACUUM INTO refuses an existing file
    state.db.vacuum_into(path)
    path.chmod(0o600)
    for old in sorted(folder.glob("control-*.db"))[:-keep]:
        old.unlink()
    return path
