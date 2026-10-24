import datetime
import json
import time
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from control import handlers, views
from control.fleet import LAST_DISCOVERY, manifest_row
from control.github_app import AppCredentials
from control.model import Repo
from control.state import State

from .conftest import FINISHED_SEED, REPO, fixture, install, mock_github, seed, sign_in, token_response

MUTATING: list[tuple[str, dict[str, str]]] = [
    ("/manage/rerun", {"run_id": "5001"}),
    ("/manage/cancel", {"run_id": "5001"}),
    ("/manage/dispatch", {"repo": "docker-sonarr", "branch": "main", "reason": "manual"}),
    ("/manage/packages", {"repo": "docker-sonarr", "branch": "main"}),
    ("/manage/skip", {"scope": "sonarr/main", "reason": "x"}),
    ("/manage/unskip", {"scope": "sonarr/main"}),
    ("/manage/reconcile", {}),
]


class FakeActions:
    """The GitHub endpoints the actions call; membership is always active here."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, Any]] = []
        self.gets: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if (token := token_response(request)) is not None:
            return token
        path = request.url.path
        if request.method == "GET":
            self.gets.append(path)
            if path.startswith("/orgs/example-org/memberships/"):
                return httpx.Response(200, json={"state": "active"})
            if path == "/orgs/example-org/repos":
                return httpx.Response(200, json=[])
            return httpx.Response(404, json={})
        assert request.headers["Authorization"] == "Bearer ghs_token"
        self.posts.append((path, json.loads(request.content) if request.content else None))
        if path.endswith("/rerun-failed-jobs"):
            return httpx.Response(201, json={})
        if path.endswith("/cancel"):
            return httpx.Response(202, json={})
        if path.endswith("/dispatches"):
            return httpx.Response(204)
        return httpx.Response(404, json={})


@pytest.fixture
def github(state: State, credentials: AppCredentials, monkeypatch: pytest.MonkeyPatch) -> FakeActions:
    install(state, credentials)
    fake = FakeActions()
    mock_github(state, monkeypatch, fake)
    seed(state, *FINISHED_SEED)
    return fake


def actions(client: TestClient) -> list[tuple[str, str, str | None, str]]:
    page = views.ManagementPage.model_validate(client.get("/api/management.json").json())
    return [(a.actor, a.action, a.target, a.result) for a in page.actions]


@pytest.mark.parametrize(("path", "form"), MUTATING)
def test_anonymous_and_non_members_get_403(
    client: TestClient, state: State, github: FakeActions, path: str, form: dict[str, str]
) -> None:
    assert client.post(path, data=form, follow_redirects=False).status_code == 403
    guest = sign_in(client, state, login="stranger", member=False)
    assert client.post(path, data={**form, "csrf": guest.csrf}, follow_redirects=False).status_code == 403
    assert github.posts == [] and actions(client) == [] and state.model.skips() == []


@pytest.mark.parametrize(("path", "form"), MUTATING)
def test_member_without_csrf_gets_403(
    client: TestClient, state: State, github: FakeActions, path: str, form: dict[str, str]
) -> None:
    sign_in(client, state)
    assert client.post(path, data=form, follow_redirects=False).status_code == 403
    assert client.post(path, data={**form, "csrf": "wrong"}, follow_redirects=False).status_code == 403
    assert github.posts == [] and actions(client) == []


def test_rerun_and_cancel(client: TestClient, state: State, github: FakeActions) -> None:
    user = sign_in(client, state)
    response = client.post(
        "/manage/rerun", data={"run_id": "5001", "csrf": user.csrf, "next": "/runs/5001"}, follow_redirects=False
    )
    assert response.status_code == 303 and response.headers["location"] == "/runs/5001"
    response = client.post("/manage/cancel", data={"run_id": "5001", "csrf": user.csrf}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/management"
    assert github.posts == [
        (f"/repos/{REPO}/actions/runs/5001/rerun-failed-jobs", None),
        (f"/repos/{REPO}/actions/runs/5001/cancel", None),
    ]
    assert actions(client) == [("octocat", "cancel", "5001", "ok"), ("octocat", "rerun", "5001", "ok")]
    # An unknown run is logged as an error, not a crash.
    client.post("/manage/rerun", data={"run_id": "9", "csrf": user.csrf}, follow_redirects=False)
    assert actions(client)[0] == ("octocat", "rerun", "9", "error")
    assert client.post("/manage/rerun", data={"run_id": "x", "csrf": user.csrf}).status_code == 400
    page = client.get("/management").text
    assert "run 9 unknown" in page and "re-run of failed jobs requested" in page


def test_dispatch_and_package_check(client: TestClient, state: State, github: FakeActions) -> None:
    user = sign_in(client, state)
    for reason in ("manual", "rebuild", "upstream"):
        form = {"repo": "docker-sonarr", "branch": "develop", "reason": reason, "csrf": user.csrf}
        assert client.post("/manage/dispatch", data=form, follow_redirects=False).status_code == 303
    form = {"repo": "example-org/docker-sonarr", "branch": "main", "csrf": user.csrf}
    assert client.post("/manage/packages", data=form, follow_redirects=False).status_code == 303
    assert github.posts == [
        (f"/repos/{REPO}/actions/workflows/build.yml/dispatches", {"ref": "develop", "inputs": {"reason": r}})
        for r in ("manual", "rebuild", "upstream")
    ] + [(f"/repos/{REPO}/actions/workflows/build.yml/dispatches", {"ref": "main", "inputs": {"reason": "packages"}})]
    # Every dispatch is also a scheduler decision, so the log and the cooldown see it.
    decisions = state.model.recent_decisions()
    assert [(d.kind, d.outcome, d.branch) for d in decisions] == [
        ("packages", "dispatch", "main"),
        ("manual", "dispatch", "develop"),
        ("manual", "dispatch", "develop"),
        ("manual", "dispatch", "develop"),
    ]
    assert decisions[0].info["reason"] == "member action by octocat"
    assert actions(client)[0] == ("octocat", "packages", f"{REPO}@main", "ok")
    assert actions(client)[1] == ("octocat", "dispatch", f"{REPO}@develop (upstream)", "ok")
    # Bad input never reaches GitHub.
    bad = {"repo": "docker-nothing", "branch": "main", "reason": "manual", "csrf": user.csrf}
    assert client.post("/manage/dispatch", data=bad).status_code == 404
    bad = {"repo": "docker-sonarr", "branch": "main", "reason": "yolo", "csrf": user.csrf}
    assert client.post("/manage/dispatch", data=bad).status_code == 400
    bad = {"repo": "docker-sonarr", "branch": "", "reason": "manual", "csrf": user.csrf}
    assert client.post("/manage/dispatch", data=bad).status_code == 400
    assert len(github.posts) == 4


def test_skip_flags(client: TestClient, state: State, github: FakeActions) -> None:
    user = sign_in(client, state)
    for scope in ("sonarr", "sonarr/main", "sonarr/main/4.0.21", "/radarr/"):
        form = {"scope": scope, "reason": "testing", "csrf": user.csrf}
        assert client.post("/manage/skip", data=form, follow_redirects=False).status_code == 303
    skips = views.SkipsPage.model_validate(client.get("/api/skips.json").json()).skips
    assert [(s.scope, s.reason, s.set_by) for s in skips] == [
        ("radarr", "testing", "octocat"),
        ("sonarr", "testing", "octocat"),
        ("sonarr/main", "testing", "octocat"),
        ("sonarr/main/4.0.21", "testing", "octocat"),
    ]
    assert client.post("/manage/skip", data={"scope": "a/b/c/d", "csrf": user.csrf}).status_code == 400
    assert client.post("/manage/skip", data={"scope": "a//c", "csrf": user.csrf}).status_code == 400
    # The fleet board shows the flags that apply to the branch.
    rows = views.FleetPage.model_validate(client.get("/api/fleet.json").json()).rows
    sonarr = next(r for r in rows if r.image == "sonarr")
    assert sonarr.skip == ["sonarr", "sonarr/main"]  # the version flag names a version the branch is not at
    assert (
        client.post("/manage/unskip", data={"scope": "sonarr", "csrf": user.csrf}, follow_redirects=False).status_code
        == 303
    )
    assert [s.scope for s in state.model.skips()] == ["radarr", "sonarr/main", "sonarr/main/4.0.21"]
    client.post("/manage/unskip", data={"scope": "sonarr", "csrf": user.csrf}, follow_redirects=False)
    assert actions(client)[0] == ("octocat", "unskip", "sonarr", "error")
    page = client.get("/management").text
    assert 'name="scope" value="sonarr/main"' in page and "no skip flag on sonarr" in page


def test_reconcile_runs_discovery(client: TestClient, state: State, github: FakeActions) -> None:
    user = sign_in(client, state)
    response = client.post("/manage/reconcile", data={"csrf": user.csrf}, follow_redirects=False)
    assert response.status_code == 303
    for _ in range(50):
        if (record := state.db.get(LAST_DISCOVERY)) and json.loads(record).get("finished"):
            break
        time.sleep(0.02)
    assert record and json.loads(record)["counts"] == {
        "repos": 0,
        "branches": 0,
        "fetched": 0,
        "unchanged": 0,
        "errors": 0,
    }
    assert "/orgs/example-org/repos" in github.gets
    assert actions(client)[0] == ("octocat", "reconcile", None, "ok")


def test_rate_limit(client: TestClient, state: State, github: FakeActions) -> None:
    user = sign_in(client, state)
    for _ in range(30):
        state.model.add_action("octocat", "skip", "x", "ok")
    response = client.post("/manage/skip", data={"scope": "sonarr", "csrf": user.csrf}, follow_redirects=False)
    assert response.status_code == 429 and state.model.skips() == []
    # Another member is unaffected, and old actions do not count.
    other = sign_in(client, state, login="hubot")
    assert (
        client.post("/manage/skip", data={"scope": "sonarr", "csrf": other.csrf}, follow_redirects=False).status_code
        == 303
    )
    old = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=2)).isoformat(timespec="seconds")
    with state.db.connect() as conn:
        conn.execute("UPDATE actions SET at = ? WHERE actor = 'octocat'", (old,))
    user = sign_in(client, state)
    assert (
        client.post("/manage/unskip", data={"scope": "sonarr", "csrf": user.csrf}, follow_redirects=False).status_code
        == 303
    )


def test_stale_member_is_rechecked_before_acting(
    client: TestClient, state: State, github: FakeActions, monkeypatch: pytest.MonkeyPatch
) -> None:
    stale_at = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=2)).isoformat(timespec="seconds")
    user = sign_in(client, state, member=True, checked_at=stale_at)
    response = client.post("/manage/skip", data={"scope": "sonarr", "csrf": user.csrf}, follow_redirects=False)
    assert response.status_code == 303 and "/orgs/example-org/memberships/octocat" in github.gets
    assert response.cookies.get("control_session")  # the refreshed verdict travels back
    # And when the org dropped them, the action is refused.
    gone = FakeActions()

    def not_member(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/orgs/example-org/memberships/"):
            return httpx.Response(404, json={})
        return gone(request)

    mock_github(state, monkeypatch, not_member)
    client.cookies.clear()
    user = sign_in(client, state, member=True, checked_at=stale_at)
    response = client.post("/manage/unskip", data={"scope": "sonarr", "csrf": user.csrf}, follow_redirects=False)
    assert response.status_code == 403 and state.model.skips()


def test_member_pages_show_the_buttons(client: TestClient, state: State, github: FakeActions) -> None:
    repo = Repo.from_github(fixture("api.repos")[0])
    state.model.upsert_manifest(
        manifest_row(repo.full_name, "main", "s", {"name": "sonarr", "release": {"tag": "latest"}})
    )
    anonymous = client.get("/runs/5001").text
    assert 'action="/manage/' not in anonymous
    sign_in(client, state)
    run = client.get("/runs/5001").text
    assert 'action="/manage/rerun"' not in run  # the run succeeded: nothing to re-run
    assert 'action="/manage/cancel"' not in run
    running = fixture("workflow_run.in_progress")
    running["workflow_run"]["id"] = 5002
    handlers.process(state.model, state.stream, "workflow_run", running)
    assert 'action="/manage/cancel"' in client.get("/runs/5002").text
    image = client.get("/images/sonarr").text
    assert 'action="/manage/dispatch"' in image and 'action="/manage/skip"' in image
    fleet = client.get("/").text
    assert 'action="/manage/dispatch"' in fleet and 'name="scope" value="sonarr/main"' in fleet
    management = client.get("/management").text
    assert 'action="/manage/reconcile"' in management and 'action="/manage/packages"' in management
