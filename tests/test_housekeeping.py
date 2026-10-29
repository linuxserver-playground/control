"""Daily delivery retention and database snapshots."""

import asyncio
import datetime
import shutil
import sqlite3

import pytest

from control import housekeeping
from control.state import State

from .conftest import fixture

NOW = datetime.datetime(2026, 10, 30, 3, 0, tzinfo=datetime.UTC)


def test_old_deliveries_go_and_snapshots_rotate(state: State) -> None:
    payload = fixture("workflow_run.requested")
    for n, days in enumerate((20, 8, 6, 1)):
        received = (NOW - datetime.timedelta(days=days)).isoformat(timespec="seconds")
        state.db.insert_delivery(f"d-{n}", "workflow_run", payload, received_at=received)
    state.db.insert_delivery("d-check", "check_run", {"action": "completed"})  # stored before intake dropped them
    record = asyncio.run(housekeeping.tick(state, NOW))
    assert record and record["errors"] == [] and record["deliveries_pruned"] == 3
    assert state.db.count_deliveries() == 2 and state.db.has_delivery("d-2") and not state.db.has_delivery("d-0")
    snapshot = state.settings.data_dir / "snapshots" / "control-2026-10-30.db"
    assert record["snapshot"] == snapshot.name and snapshot.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(snapshot) as conn:
        assert conn.execute("SELECT COUNT(*) FROM deliveries").fetchone() == (2,)

    # Daily: not again the same day; the oldest snapshots go once there are more than `snapshots_keep`.
    assert asyncio.run(housekeeping.tick(state, NOW + datetime.timedelta(hours=1))) is None
    for day in range(1, 4):
        assert asyncio.run(housekeeping.tick(state, NOW + datetime.timedelta(days=day)))
    names = sorted(p.name for p in snapshot.parent.iterdir())
    assert names == ["control-2026-11-01.db", "control-2026-11-02.db"]


def test_no_snapshot_without_room_for_it(state: State, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "disk_usage", lambda _: shutil._ntuple_diskusage(10, 9, 1))
    record = asyncio.run(housekeeping.tick(state, NOW))
    assert record and "snapshot" not in record and record["errors"][0].startswith("snapshot skipped: 0 MiB free")
    assert not (state.settings.data_dir / "snapshots").exists()
