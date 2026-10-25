import base64
from typing import Any

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from control import fleet
from control.github import GitHub
from control.github_app import AppCredentials
from control.model import Repo
from control.state import State

from .conftest import REPO, deliver, fixture, install, mock_client, token_response

MAIN: dict[str, Any] = {
    "schema": 1,
    "name": "sonarr",
    "upstream": {"version": {"type": "json", "url": "https://services.sonarr.tv/v1/releases", "jq": ".[0]"}},
    "release": {
        "branch": "main",
        "tag": "latest",
        "channel": "stable",
        "tags": [{"tag": "latest", "description": "stable"}, {"tag": "develop", "description": "dev"}],
    },
    "build": {"base": "alpine", "platforms": ["amd64", "arm64"]},
}
DEVELOP: dict[str, Any] = {
    **MAIN,
    "upstream": {"version": {"type": "github_release", "repo": "Sonarr/Sonarr", "prerelease": True}},
    "release": {"branch": "develop", "tag": "develop", "channel": "prerelease"},
    "build": {"base": "alpine", "package_check": False},
}


def contents(data: dict[str, Any], sha: str) -> dict[str, Any]:
    text = yaml.safe_dump(data)
    return {"sha": sha, "encoding": "base64", "content": base64.b64encode(text.encode()).decode()}


class FakeRepo:
    """GitHub as discovery sees it: repos, branches and image.yml per ref, with the shas under our control."""

    def __init__(self) -> None:
        self.files: dict[str, tuple[dict[str, Any], str]] = {
            "main": (MAIN, "sha-main-1"),
            "develop": (DEVELOP, "sha-dev-1"),
        }
        self.branches = ["main", "develop", "feature-x"]
        self.requests: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if (token := token_response(request)) is not None:
            return token
        path = request.url.path
        self.requests.append(f"{request.method} {path}?{request.url.query.decode()}")
        assert request.headers["Authorization"] == "Bearer ghs_token"
        if path == "/orgs/example-org/repos":
            return httpx.Response(200, json=fixture("api.repos"))
        if path == f"/repos/{REPO}/branches":
            return httpx.Response(200, json=[{"name": b} for b in self.branches])
        if path == f"/repos/{REPO}/contents/image.yml":
            ref = request.url.params.get("ref")
            if ref in self.files:
                data, sha = self.files[ref]
                return httpx.Response(200, json=contents(data, sha))
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(404, json={"message": f"no fixture for {path}"})


@pytest.mark.anyio
async def test_discovery_stores_default_and_tag_branches(state: State) -> None:
    github = FakeRepo()
    async with mock_client(github) as http:
        counts = await fleet.Discovery(GitHub(http, "ghs_token"), "example-org", state.model, state.stream).run()
    assert counts == {"repos": 1, "branches": 2, "fetched": 2, "unchanged": 0, "errors": 0}
    rows = state.model.manifests(REPO)
    assert [(m.branch, m.release_tag, m.sha, m.package_check) for m in rows] == [
        ("develop", "develop", "sha-dev-1", False),
        ("main", "latest", "sha-main-1", True),
    ]
    main = state.model.manifest(REPO, "main")
    assert main and main.image == "sonarr" and main.source_type == "json" and main.manifest["schema"] == 1
    assert main.version_source == MAIN["upstream"]["version"]
    # The branch named after the tag was read first; the stray feature branch was never touched.
    reads = [r.split("?ref=")[1] for r in github.requests if "contents/image.yml" in r]
    assert reads == ["main", "develop"]
    # Discovery is about image repos only; the backfill records the rest.
    assert state.model.repo("example-org/workflows") is None

    # A second run with nothing changed only touches fetched_at.
    async with mock_client(github) as http:
        counts = await fleet.Discovery(GitHub(http, "ghs_token"), "example-org", state.model, state.stream).run()
    assert counts["unchanged"] == 2 and counts["fetched"] == 0

    # The develop branch stops publishing its tag: its row goes.
    github.files["develop"] = (
        {**DEVELOP, "release": {"branch": "develop", "tag": "nightly", "channel": "prerelease"}},
        "sha-dev-2",
    )
    async with mock_client(github) as http:
        await fleet.Discovery(GitHub(http, "ghs_token"), "example-org", state.model, state.stream).run()
    assert [m.branch for m in state.model.manifests(REPO)] == ["main"]


@pytest.mark.anyio
async def test_discovery_hunts_for_a_tag_on_oddly_named_branches(state: State) -> None:
    github = FakeRepo()
    github.branches = ["main", "feature-x", "dev-branch"]
    github.files = {
        "main": (MAIN, "s1"),
        "dev-branch": ({**DEVELOP, "release": {**DEVELOP["release"], "branch": "dev-branch"}}, "s2"),
    }
    async with mock_client(github) as http:
        await fleet.Discovery(GitHub(http, "ghs_token"), "example-org", state.model, state.stream).run()
    assert [m.branch for m in state.model.manifests(REPO)] == ["dev-branch", "main"]
    # A manifest whose release.branch names another branch is a dev checkout, not a live branch.
    github.files["dev-branch"] = (DEVELOP, "s3")  # says branch: develop
    async with mock_client(github) as http:
        await fleet.Discovery(GitHub(http, "ghs_token"), "example-org", state.model, state.stream).run()
    assert [m.branch for m in state.model.manifests(REPO)] == ["main"]


@pytest.mark.anyio
async def test_refresh_branch_after_push(state: State) -> None:
    github = FakeRepo()
    repo = Repo.from_github(fixture("api.repos")[0])
    state.model.upsert_repo(repo)
    async with mock_client(github) as http:
        discovery = fleet.Discovery(GitHub(http, "ghs_token"), "example-org", state.model, state.stream)
        await discovery.run()
        github.files["main"] = ({**MAIN, "build": {"package_check": False}}, "sha-main-2")
        refreshed = await discovery.refresh_branch(repo, "main")
        assert refreshed and refreshed.sha == "sha-main-2" and refreshed.package_check is False
        # The branch disappears: so does its row.
        del github.files["develop"]
        assert await discovery.refresh_branch(repo, "develop") is None
        assert [m.branch for m in state.model.manifests(REPO)] == ["main"]


def test_manifest_row_defaults_and_parse() -> None:
    row = fleet.manifest_row(REPO, "main", "abc", {"name": "x"})
    assert row.release_tag is None and row.version_source_json is None and row.package_check is True
    assert row.image == "x" and row.source_type is None
    bare = fleet.manifest_row(REPO, "main", "abc", {})
    assert bare.image == "sonarr"  # from the repo name
    parsed = fleet.parse_manifest("release:\n  tag: 2024-01-01\n  branch: main\n")
    assert parsed["release"]["tag"] == "2024-01-01"  # a date-looking tag stays a string
    with pytest.raises(ValueError):
        fleet.parse_manifest("- just\n- a list\n")
    assert fleet.release_tags(MAIN) == ["latest", "develop"]
    assert fleet.push_branch({"ref": "refs/heads/develop"}) == "develop"
    assert fleet.push_branch({"ref": "refs/tags/v1"}) is None


def test_discovery_due_and_record(state: State) -> None:
    assert fleet.discovery_due(state)
    state.db.set(fleet.LAST_DISCOVERY, '{"started": "2026-10-24T12:00:00+00:00"}')
    import datetime

    assert not fleet.discovery_due(state, datetime.datetime(2026, 10, 24, 12, 29, tzinfo=datetime.UTC))
    assert fleet.discovery_due(state, datetime.datetime(2026, 10, 24, 12, 30, tzinfo=datetime.UTC))


@pytest.mark.anyio
async def test_discover_once_without_installation_records_error(state: State) -> None:
    record = await fleet.discover_once(state)
    assert record["error"] == "GitHub App not configured or not installed"
    assert fleet.last_discovery(state) == record


def test_push_webhook_schedules_a_refresh(
    client: TestClient, state: State, configured: AppCredentials, monkeypatch: pytest.MonkeyPatch
) -> None:
    github = FakeRepo()
    install(state, configured)
    from .conftest import mock_github

    mock_github(state, monkeypatch, github)
    state.model.upsert_repo(Repo.from_github(fixture("api.repos")[0]))
    payload = fixture("push")
    payload["repository"]["default_branch"] = "main"
    assert deliver(client, configured.webhook_secret, "push", payload).status_code == 202
    # The refresh ran in the background inside the app's loop; by the time the client returns it is done.
    import time

    for _ in range(50):
        if state.model.manifest(REPO, "main"):
            break
        time.sleep(0.02)
    row = state.model.manifest(REPO, "main")
    assert row and row.sha == "sha-main-1"
    # The repo had nothing live, so the first push found its other live branches too.
    for _ in range(50):
        if state.model.manifest(REPO, "develop"):
            break
        time.sleep(0.02)
    assert [m.branch for m in state.model.manifests(REPO)] == ["develop", "main"]
    # A push to a branch nobody tracks is ignored without a GitHub call.
    before = len(github.requests)
    payload["ref"] = "refs/heads/feature-x"
    assert deliver(client, configured.webhook_secret, "push", payload, delivery_id="d-2").status_code == 202
    time.sleep(0.05)
    assert len(github.requests) == before
