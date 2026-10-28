"""Pages on every image repo: enabled once (build_type=workflow, environment open), skipped without permissions."""

import asyncio
import datetime
import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from control import fleet, mods, pages
from control.github import installation_client
from control.github_app import AppCredentials
from control.model import Repo
from control.state import State

from .conftest import REPO, deliver, fixture, install, mock_discord

GRANTED = {"actions": "write", "contents": "write", "pages": "write", "administration": "write"}
LIVE_TODAY = {"actions": "write", "contents": "write", "metadata": "read", "packages": "write"}


class PagesWorld:
    def __init__(self, permissions: dict[str, str] | None = GRANTED) -> None:
        self.permissions = permissions
        self.site: dict[str, Any] | None = None
        self.forbidden = False
        self.requests: list[tuple[str, str, Any]] = []
        self.discord: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/access_tokens"):
            expires = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
            body: dict[str, Any] = {"token": "ghs_token", "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ")}
            if self.permissions is not None:
                body["permissions"] = self.permissions
            return httpx.Response(201, json=body)
        if "discord.com" in str(request.url):
            self.discord.append(json.loads(request.content).get("content", ""))
            return httpx.Response(200, json={"id": "m"})
        payload: dict[str, Any] = json.loads(request.content) if request.content else {}
        self.requests.append((request.method, path, payload))
        if self.forbidden and request.method != "GET":
            return httpx.Response(403, json={"message": "Resource not accessible by integration"})
        if path == f"/repos/{REPO}/pages":
            if request.method == "GET":
                return httpx.Response(200, json=self.site) if self.site else httpx.Response(404, json={})
            if request.method == "POST":
                self.site = {"build_type": payload["build_type"]}
                return httpx.Response(201, json=self.site)
            if request.method == "PUT":
                self.site = {"build_type": payload["build_type"]}
                return httpx.Response(204)
        if path == f"/repos/{REPO}/environments/github-pages" and request.method == "PUT":
            return httpx.Response(200, json={"name": "github-pages"})
        return httpx.Response(404, json={"message": f"no fixture for {path}"})

    def writes(self) -> list[tuple[str, str, Any]]:
        return [r for r in self.requests if r[0] != "GET"]


def sonarr(**fields: Any) -> Repo:
    data = {**fixture("repository.created")["repository"], "name": "docker-sonarr", "full_name": REPO}
    return Repo.from_github({**data, **fields})


@pytest.fixture
def world(state: State, credentials: AppCredentials, monkeypatch: pytest.MonkeyPatch) -> PagesWorld:
    world = PagesWorld()
    install(state, credentials)
    mock_discord(state, monkeypatch, world)
    return world


def ensure(state: State, repos: list[Repo]) -> dict[str, int]:
    async def go() -> dict[str, int]:
        github = await installation_client(state)
        assert github is not None
        return await pages.ensure(state, github, repos)

    return asyncio.run(go())


def test_a_new_repo_gets_pages_and_an_open_environment_once(state: State, world: PagesWorld) -> None:
    assert ensure(state, [sonarr()]) == {"done": 1, "errors": 0}
    assert world.writes() == [
        ("POST", f"/repos/{REPO}/pages", {"build_type": "workflow"}),
        ("PUT", f"/repos/{REPO}/environments/github-pages", {"deployment_branch_policy": None}),
    ]
    assert state.model.repo_pages_done() == {REPO}
    world.requests.clear()
    assert ensure(state, [sonarr()]) == {} and world.requests == []


def test_a_branch_built_site_is_switched_to_workflow(state: State, world: PagesWorld) -> None:
    world.site = {"build_type": "legacy"}
    ensure(state, [sonarr()])
    assert ("PUT", f"/repos/{REPO}/pages", {"build_type": "workflow"}) in world.writes()
    assert world.site == {"build_type": "workflow"}


def test_archived_and_non_image_repos_are_left_alone(state: State, world: PagesWorld) -> None:
    other = Repo.from_github({**fixture("repository.created")["repository"], "name": "hub", "full_name": "o/hub"})
    assert ensure(state, [sonarr(archived=True), other]) == {}
    assert world.requests == []


def test_without_the_permissions_it_skips_and_says_so_once(
    state: State, credentials: AppCredentials, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = PagesWorld(LIVE_TODAY)
    install(state, credentials)
    mock_discord(state, monkeypatch, world)
    assert ensure(state, [sonarr()]) == {"blocked": 1}
    assert ensure(state, [sonarr()]) == {"blocked": 1}
    assert world.requests == []  # the token said so; nothing was even tried
    assert len(world.discord) == 1
    assert "pages: write" in world.discord[0] and "administration: write" in world.discord[0]
    assert state.model.repo_pages_done() == set()
    # The org accepts the new permissions: the installation event drops the token, the next one carries them.
    world.permissions = GRANTED
    state.tokens.forget(777)
    assert ensure(state, [sonarr()]) == {"done": 1, "errors": 0}
    assert state.db.get(pages.PERMISSIONS_MISSING) is None


def test_a_403_with_unknown_permissions_counts_as_missing(
    state: State, credentials: AppCredentials, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = PagesWorld(permissions=None)
    world.forbidden = True
    install(state, credentials)
    mock_discord(state, monkeypatch, world)
    assert ensure(state, [sonarr()]) == {"done": 0, "errors": 0, "blocked": 1}
    assert state.model.repo_pages_done() == set() and len(world.discord) == 1


def test_a_created_docker_repo_schedules_pages(
    client: TestClient, state: State, configured: AppCredentials, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[str] = []

    async def fake(_state: State, full_name: str) -> dict[str, int]:
        called.append(full_name)
        return {}

    monkeypatch.setattr(pages, "ensure_one", fake)
    payload = fixture("repository.created")
    assert deliver(client, configured.webhook_secret, "repository", payload).status_code == 202
    assert called == [payload["repository"]["full_name"]]
    assert pages.schedule(state, {**payload, "action": "archived"}) is None
    hub = {**payload, "repository": {**payload["repository"], "name": "hub"}}
    assert pages.schedule(state, hub) is None


def test_new_permissions_accepted_drops_the_cached_token(
    client: TestClient, state: State, configured: AppCredentials, monkeypatch: pytest.MonkeyPatch
) -> None:
    forgotten: list[int] = []
    monkeypatch.setattr(state.tokens, "forget", forgotten.append)
    payload = {"action": "new_permissions_accepted", "installation": {"id": 777}}
    assert deliver(client, configured.webhook_secret, "installation", payload).status_code == 202
    assert forgotten == [777]


def test_every_discovery_sets_up_the_repos_it_found(
    state: State, world: PagesWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run(self: fleet.Discovery) -> dict[str, int]:
        self.found.append(sonarr())
        return {"repos": 1}

    async def no_mods(*_args: Any) -> dict[str, int]:
        return {}

    monkeypatch.setattr(fleet.Discovery, "run", run)
    monkeypatch.setattr(mods, "discover", no_mods)
    record = asyncio.run(fleet.discover_once(state))
    assert record["pages"] == {"done": 1, "errors": 0}
    assert state.model.repo_pages_done() == {REPO}
    assert "pages" not in asyncio.run(fleet.discover_once(state))
