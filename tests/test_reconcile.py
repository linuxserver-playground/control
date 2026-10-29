"""The 30-minute backstop for lost webhooks: redelivery, unfinished runs and open threads."""

import asyncio
import datetime
from typing import Any

import httpx
import pytest

from control import reconcile
from control.github import GitHub
from control.github_app import AppCredentials
from control.model import Issue, PullRequest, Run
from control.state import State

from .conftest import REPO, fixture, install, mock_github, token_response

NOW = datetime.datetime(2026, 10, 25, 12, 0, tzinfo=datetime.UTC)


def at(minutes: int) -> str:
    return (NOW - datetime.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def delivery(id_: int, guid: str, status_code: int, minutes: int, redelivery: bool = False) -> dict[str, Any]:
    return {
        "id": id_,
        "guid": guid,
        "delivered_at": at(minutes),
        "redelivery": redelivery,
        "status_code": status_code,
        "event": "workflow_run",
        "action": "completed",
    }


class FakeGitHub:
    """The App's delivery log, two runs, one pull request and the org's open threads."""

    def __init__(self) -> None:
        self.requests: list[str] = []
        self.posts: list[str] = []
        self.auth: dict[str, str] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if (token := token_response(request)) is not None:
            return token
        path = request.url.path
        self.requests.append(f"{request.method} {path}")
        self.auth[path] = request.headers["Authorization"]
        if request.method == "POST":
            self.posts.append(path)
            return httpx.Response(202, json={})
        if path == "/app/hook/deliveries":
            page = [
                delivery(1, "g-ok", 202, 5),
                delivery(2, "g-lost", 0, 6),  # control was down: send it again
                delivery(3, "g-stored", 502, 7),  # control stored it, then answered badly: nothing to do
                delivery(4, "g-refused", 400, 8),
                delivery(5, "g-refused", 400, 9, redelivery=True),
                delivery(6, "g-refused", 400, 10, redelivery=True),
                delivery(7, "g-refused", 400, 11, redelivery=True),  # three tries: given up
                delivery(8, "g-before", 0, 24 * 60 + 30),  # before the window: ignored, and the paging stops
            ]
            next_page = '<https://api.github.com/app/hook/deliveries?cursor=x>; rel="next"'
            return httpx.Response(200, json=page, headers={"Link": next_page})
        if path == f"/repos/{REPO}/actions/runs/5001":
            completed = fixture("workflow_run.completed")["workflow_run"]
            return httpx.Response(200, json={**completed, "name": "build changed"})
        if path == f"/repos/{REPO}/actions/workflows":
            return httpx.Response(200, json={"total_count": 1, "workflows": [{"id": 300, "name": "Build image"}]})
        if path == f"/repos/{REPO}/actions/runs/5001/jobs":
            return httpx.Response(200, json=fixture("api.jobs"))
        if path == f"/repos/{REPO}/pulls/7":
            return httpx.Response(200, json={**fixture("api.pulls")[0], "state": "closed", "updated_at": at(1)})
        if path == f"/repos/{REPO}/pulls/8":
            return httpx.Response(200, json={**fixture("api.pulls")[0], "number": 8})
        if path == "/search/issues":
            url = f"https://api.github.com/repos/{REPO}"
            if "is:pr" in request.url.params["q"]:
                return httpx.Response(200, json={"total_count": 1, "items": [{"number": 8, "repository_url": url}]})
            issue = {**fixture("api.issues")[0], "repository_url": url}
            return httpx.Response(200, json={"total_count": 1, "items": [issue]})
        return httpx.Response(404, json={"message": "Not Found"})


@pytest.fixture(autouse=True)
def fresh_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reconcile, "_started", False)


def test_a_pass_repairs_lost_deliveries_runs_and_threads(
    state: State, credentials: AppCredentials, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(state, credentials)
    github = FakeGitHub()
    mock_github(state, monkeypatch, github)
    state.db.insert_delivery("g-stored", "workflow_run", fixture("workflow_run.requested"))
    requested = fixture("workflow_run.requested")["workflow_run"]
    state.model.upsert_run(Run.from_github(REPO, requested, "Build image"))  # its completion was lost
    state.model.upsert_run(Run.from_github(REPO, {**requested, "id": 5002}, "Build image"))  # deleted on GitHub
    state.model.upsert_pull_request(PullRequest.from_github(REPO, fixture("api.pulls")[0]))  # closed meanwhile

    record = asyncio.run(reconcile.tick(state, GitHub(state.http, "ghs_token"), NOW))

    assert record and record["errors"] == []
    # 5001 completed, its job finished, 5002 dropped; #7 closed, #8 and issue #12 added.
    assert record["counts"] == {"redelivered": 1, "runs": 3, "threads": 3}
    assert github.posts == ["/app/hook/deliveries/2/attempts"]
    assert github.auth["/app/hook/deliveries"].startswith("Bearer ey")  # the App's JWT, not the installation token
    assert sum(r.endswith("/app/hook/deliveries") for r in github.requests) == 1  # the window ended on page one
    run = state.model.run(5001)
    assert run and (run.status, run.workflow_name) == ("completed", "Build image")
    assert state.model.run(5002) is None
    pr = state.model.pull_request(REPO, 7)
    assert pr and pr.state == "closed"
    assert [p.number for p in state.model.open_pull_requests()] == [8]
    assert [i.number for i in state.model.open_issues()] == [12]
    assert reconcile.last_reconcile(state) == record

    # Not due again for 30 minutes; due at once after a restart.
    assert asyncio.run(reconcile.tick(state, GitHub(state.http, "ghs_token"), NOW)) is None
    monkeypatch.setattr(reconcile, "_started", False)
    assert asyncio.run(reconcile.tick(state, GitHub(state.http, "ghs_token"), NOW)) is not None


def test_a_failing_step_does_not_stop_the_others(
    state: State, credentials: AppCredentials, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(state, credentials)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/hook/deliveries":
            return httpx.Response(500, json={"message": "boom"})
        if request.url.path == "/search/issues":
            return httpx.Response(200, json={"total_count": 0, "items": []})
        return httpx.Response(404, json={"message": "Not Found"})  # the issue's repository is gone

    mock_github(state, monkeypatch, handler)
    state.model.upsert_issue(Issue.from_github(REPO, fixture("api.issues")[0]))
    record = asyncio.run(reconcile.tick(state, GitHub(state.http, "ghs_token"), NOW))
    assert record and record["counts"] == {"runs": 0, "threads": 0}
    assert len(record["errors"]) == 1 and record["errors"][0].startswith("redelivered: HTTPStatusError")
