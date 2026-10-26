"""A job whose completion webhook never arrived is read again from the API once its run has completed."""

import asyncio
import json
from typing import Any

import httpx
import pytest

from control import heal
from control.github_app import AppCredentials
from control.state import State

from .conftest import REPO, fixture, install, mock_github, seed, token_response


def test_a_completed_run_with_a_running_job_is_healed(
    state: State, credentials: AppCredentials, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(state, "workflow_run.requested", "workflow_job.in_progress", "workflow_run.completed")
    assert state.model.runs_with_unfinished_jobs() == [(REPO, 5001)]
    install(state, credentials)
    completed: dict[str, Any] = fixture("workflow_job.completed")["workflow_job"]
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if (token := token_response(request)) is not None:
            return token
        seen.append(request.url.path)
        return httpx.Response(200, json={"total_count": 1, "jobs": [completed]})

    mock_github(state, monkeypatch, handler)
    assert asyncio.run(heal.heal(state, delay=0)) == 1
    assert seen == [f"/repos/{REPO}/actions/runs/5001/jobs"]
    job = state.model.job(9001)
    assert job and job.status == "completed" and job.conclusion == "success"
    assert state.model.runs_with_unfinished_jobs() == []
    # Nothing left dangling: no API call at all.
    assert asyncio.run(heal.heal(state, delay=0)) == 0 and len(seen) == 1


def test_only_a_completed_run_schedules_a_heal(state: State) -> None:
    assert heal.schedule_heal(state, {"action": "in_progress"}) is None
    assert json.dumps(fixture("workflow_run.completed")["action"]) == '"completed"'
