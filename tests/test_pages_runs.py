"""A `reason=pages` run deploys the Pages site and builds nothing: control never takes it for a build."""

import datetime
import logging
from typing import Any

import pytest

from control import handlers, views
from control.state import State

from .conftest import REPO, fixture


def run(state: State, run_id: int, created_at: str, status: str = "in_progress", conclusion: str | None = None) -> None:
    payload = fixture("workflow_run.in_progress")
    data = {**payload["workflow_run"], "id": run_id, "event": "workflow_dispatch", "status": status}
    data.update(conclusion=conclusion, created_at=created_at, updated_at=created_at)
    handlers.process(state.model, state.stream, "workflow_run", {**payload, "workflow_run": data})


def job(
    state: State, run_id: int, job_id: int, name: str, status: str, conclusion: str | None = None, **extra: Any
) -> None:
    payload = fixture("workflow_job.in_progress")
    data = {**payload["workflow_job"], "id": job_id, "run_id": run_id, "name": name, "status": status}
    data.update(conclusion=conclusion, **extra)
    action = "completed" if status == "completed" else status
    handlers.process(state.model, state.stream, "workflow_job", {**payload, "action": action, "workflow_job": data})


def pages_run(state: State, run_id: int = 6001, status: str = "in_progress") -> None:
    run(state, run_id, "2026-10-24T13:00:00Z", status=status)
    job(state, run_id, run_id * 10, "build / Prepare", "completed", "skipped")
    job(state, run_id, run_id * 10 + 1, "build / Publish", "completed", "skipped")
    job(state, run_id, run_id * 10 + 2, "build / Pages", "in_progress")


def test_a_pages_run_is_not_a_build_in_flight(state: State) -> None:
    pages_run(state)
    assert state.model.active_run_for_branch(REPO, "main") is None
    assert state.model.latest_run_for_branch(REPO, "main") is None
    assert state.model.latest_branch_runs(REPO) == []
    # It still shows where runs show.
    assert [r.id for r in state.model.active_runs()] == [6001]


def test_a_build_with_its_pages_job_skipped_is_a_build(state: State) -> None:
    pages_run(state)
    run(state, 6002, "2026-10-24T14:00:00Z")
    job(state, 6002, 60020, "build / Prepare", "in_progress")
    job(state, 6002, 60021, "build / Pages", "completed", "skipped")
    active = state.model.active_run_for_branch(REPO, "main")
    assert active is not None and active.id == 6002
    # A run whose jobs control has not seen yet is taken for a build.
    run(state, 6003, "2026-10-24T15:00:00Z")
    latest = state.model.latest_run_for_branch(REPO, "main")
    assert latest is not None and latest.id == 6003


def test_a_dispatch_decision_finds_its_own_kind_of_run(state: State) -> None:
    pages_run(state)
    run(state, 6002, "2026-10-24T13:01:00Z")
    job(state, 6002, 60020, "build / Prepare", "in_progress")
    after = "2026-10-24T12:59:00+00:00"
    build = state.model.run_after(REPO, "main", "workflow_dispatch", after)
    pages = state.model.run_after(REPO, "main", "workflow_dispatch", after, pages=True)
    assert build is not None and build.id == 6002
    assert pages is not None and pages.id == 6001


def test_a_failed_pages_deploy_is_a_log_line(state: State, caplog: pytest.LogCaptureFixture) -> None:
    pages_run(state)
    url = "https://github.com/example-org/docker-sonarr/actions/runs/6001/job/60012"
    with caplog.at_level(logging.WARNING, logger="control.handlers"):
        job(state, 6001, 60012, "build / Pages", "completed", "failure", html_url=url)
    assert any("pages deploy failed" in r.getMessage() and url in r.getMessage() for r in caplog.records)
    # Nothing a build would leave: no builds row, so no fleet result, Discord message or run archive.
    assert state.model.builds_for_run(6001) == []


def test_a_pages_job_waits_the_default_hour_before_it_counts_as_stuck(state: State) -> None:
    pages_run(state)
    pages = state.model.job(60012)
    assert pages is not None
    now = datetime.datetime.fromisoformat(str(pages.started_at)) + datetime.timedelta(minutes=10)
    view = views.job_view(state, pages, now)
    assert view.threshold_seconds == 3600 and not view.stuck
