import asyncio
import datetime
import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from control.app import create_app
from control.backfill import RATE_LIMIT_FLOOR, Backfill, backfill_once, last_backfill
from control.config import Settings
from control.github_app import API, AppCredentials
from control.state import LAST_BACKFILL, State

from .conftest import REPO, fixture, mock_client

WORKFLOWS = "example-org/workflows"


class FakeGitHub:
    """Serves the api.* fixtures the way api.github.com would, with pagination on the repo list."""

    def __init__(self, remaining: int = 4000) -> None:
        self.requests: list[httpx.Request] = []
        self.remaining = remaining
        self.sleeps: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/app/installations/777/access_tokens":
            assert request.headers["Authorization"].startswith("Bearer ey")  # the app JWT
            expires = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
            token = {"token": "ghs_token", "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ")}
            return httpx.Response(201, json=token)
        assert request.headers["Authorization"] == "Bearer ghs_token"
        assert request.headers["Accept"] == "application/vnd.github+json"
        headers = {
            "X-RateLimit-Remaining": str(self.remaining),
            "X-RateLimit-Reset": str(int(datetime.datetime.now(datetime.UTC).timestamp()) + 5),
        }
        repos = fixture("api.repos")
        if path == "/orgs/example-org/repos":
            if request.url.params.get("page") == "2":
                return httpx.Response(200, json=repos[1:], headers=headers)
            headers["Link"] = f'<{API}/orgs/example-org/repos?per_page=100&page=2>; rel="next"'
            return httpx.Response(200, json=repos[:1], headers=headers)
        if path == f"/repos/{REPO}/actions/runs":
            runs = fixture("api.runs")
            runs["workflow_runs"][0]["name"] = "build sonarr"  # a run-name: the workflow's name comes from its id
            return httpx.Response(200, json=runs, headers=headers)
        if path == f"/repos/{REPO}/actions/workflows":
            return httpx.Response(
                200, json={"total_count": 1, "workflows": [{"id": 300, "name": "Build image"}]}, headers=headers
            )
        if path == f"/repos/{REPO}/actions/runs/5001/jobs":
            return httpx.Response(200, json=fixture("api.jobs"), headers=headers)
        if path == f"/repos/{REPO}/pulls":
            return httpx.Response(200, json=fixture("api.pulls"), headers=headers)
        if path == f"/repos/{REPO}/issues":
            return httpx.Response(200, json=fixture("api.issues"), headers=headers)
        if path == f"/repos/{REPO}/releases":
            return httpx.Response(200, json=fixture("api.releases"), headers=headers)
        if path.startswith(f"/repos/{WORKFLOWS}/"):
            empty: Any = {"total_count": 0, "workflow_runs": []} if path.endswith("/actions/runs") else []
            return httpx.Response(200, json=empty, headers=headers)
        if path == "/app/installations/777/access_tokens":
            expires = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
            return httpx.Response(
                201, json={"token": "ghs_token", "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ")}
            )
        return httpx.Response(404, json={"message": f"no fixture for {path}"}, headers=headers)


@pytest.mark.anyio
async def test_backfill_fills_every_table_and_is_idempotent(state: State) -> None:
    github = FakeGitHub()
    async with mock_client(github) as http:
        counts = await Backfill(http, "ghs_token", "example-org", state.model, state.stream, sleep=github.sleep).run()
    assert counts == {"repos": 2, "runs": 1, "jobs": 1, "pulls": 1, "issues": 1, "releases": 1}
    assert [r.name for r in state.model.repos()] == ["docker-sonarr", "workflows"]
    run = state.model.run(5001)
    assert run and run.status == "completed" and run.conclusion == "success" and run.workflow_name == "Build image"
    job = state.model.job(9001)
    assert job and job.status == "completed" and len(job.steps) == 2
    assert [pr.number for pr in state.model.open_pull_requests()] == [7]
    assert [i.number for i in state.model.open_issues()] == [12]  # #13 is a PR seen through the issues API
    assert [r.tag for r in state.model.recent_releases()] == ["4.0.20.3014-ls1"]
    assert github.sleeps == []
    first = len(github.requests)
    assert any(r.url.params.get("page") == "2" for r in github.requests)

    async with mock_client(github) as http:
        counts = await Backfill(http, "ghs_token", "example-org", state.model, state.stream, sleep=github.sleep).run()
    assert counts["runs"] == 1 and state.model.counts()["runs"] == 1 and state.model.counts()["jobs"] == 1
    # The finished run's jobs are already known, so the second pass saves that call.
    assert len(github.requests) - first == first - 1


@pytest.mark.anyio
async def test_backfill_sleeps_when_the_rate_limit_is_low(state: State) -> None:
    github = FakeGitHub(remaining=RATE_LIMIT_FLOOR - 1)
    async with mock_client(github) as http:
        await Backfill(http, "ghs_token", "example-org", state.model, state.stream, sleep=github.sleep).run()
    assert github.sleeps and all(1 <= s <= 7 for s in github.sleeps)
    assert len(github.sleeps) == len(github.requests)


@pytest.mark.anyio
async def test_backfill_retries_once_after_a_secondary_limit(state: State) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(403, json={"message": "slow down"}, headers={"Retry-After": "2"})
        return httpx.Response(200, json=[], headers={"X-RateLimit-Remaining": "100"})

    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async with mock_client(handler) as http:
        backfill = Backfill(http, "t", "example-org", state.model, state.stream, sleep)
        response = await backfill.get(f"{API}/orgs/example-org/repos")
    assert response.status_code == 200 and attempts == 2 and sleeps == [3.0]


def test_backfill_once_records_outcome(
    client: TestClient, state: State, configured: AppCredentials, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Not installed yet: skipped with a reason, nothing fetched.
    record = state_backfill(client, state)
    assert record["error"] == "GitHub App not configured or not installed"
    assert last_backfill(state) == record

    github = FakeGitHub()
    mock = httpx.AsyncClient(transport=httpx.MockTransport(github))
    monkeypatch.setattr(state, "http", mock)
    monkeypatch.setattr(state.tokens, "client", mock)
    state.db.set("installation_id", "777")
    record = state_backfill(client, state)
    assert record["error"] is None and record["counts"]["repos"] == 2 and record["finished"]
    assert json.loads(state.db.get(LAST_BACKFILL) or "{}") == record
    page = client.get("/status")
    assert "repos 2" in page.text
    assert client.get("/api/status.json").json()["last_backfill"]["counts"]["runs"] == 1


def state_backfill(client: TestClient, state: State) -> dict[str, Any]:
    """Run `backfill_once` on the app's own event loop, like the startup task does."""
    assert client.portal
    return client.portal.call(backfill_once, state)


def test_backfill_on_start_runs_in_the_background(settings: Settings) -> None:
    boot = settings.model_copy(update={"backfill_on_start": True})
    with TestClient(create_app(boot)) as client:
        state: State = client.app.state.control  # type: ignore[attr-defined]
        # Without an installation the task records why it skipped and the server is unaffected.
        assert client.get("/healthz").status_code == 200
        assert client.portal
        record = client.portal.call(_wait_for_backfill, state)
    assert record and record["error"] == "GitHub App not configured or not installed"


async def _wait_for_backfill(state: State) -> dict[str, Any] | None:
    for _ in range(50):
        record = last_backfill(state)
        if record and (record.get("finished") or record.get("error")):
            return record
        await asyncio.sleep(0.02)
    return last_backfill(state)
