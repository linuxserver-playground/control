"""Pages on every image repo is checked, never changed: one warning per repo without a site, cleared once it has one."""

import asyncio
import json
from typing import Any

import httpx
import pytest

from control import fleet, mods, pages
from control.github import GitHub
from control.github_app import AppCredentials
from control.state import State

from .conftest import REPO, fixture, install, mock_client, mock_discord, token_response
from .test_fleet import FakeRepo

MODS = "example-org/docker-mods"


class Discord:
    def __init__(self) -> None:
        self.posts: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if (token := token_response(request)) is not None:
            return token
        self.posts.append(json.loads(request.content).get("content", ""))
        return httpx.Response(200, json={"id": "m"})


@pytest.fixture
def discord(state: State, monkeypatch: pytest.MonkeyPatch) -> Discord:
    world = Discord()
    mock_discord(state, monkeypatch, world)
    return world


def test_a_repo_without_pages_is_reported_once_and_cleared_when_it_has_a_site(
    state: State, discord: Discord, caplog: pytest.LogCaptureFixture
) -> None:
    assert asyncio.run(pages.check(state, {REPO: False})) == [REPO]
    assert asyncio.run(pages.check(state, {REPO: False})) == [REPO]
    assert len(discord.posts) == 1
    assert discord.posts[0] == f"⚠️ {REPO} has no GitHub Pages site: its build files will not show on the website. " + (
        "Enable it as the new-image recipe says (Pages from GitHub Actions, the github-pages environment open to "
        "every branch)."
    )
    assert sum("has no GitHub Pages site" in r.getMessage() for r in caplog.records) == 1
    assert state.model.pages_missing() == {REPO}
    # A repo discovery did not read this pass keeps its state.
    assert asyncio.run(pages.check(state, {})) == [] and state.model.pages_missing() == {REPO}
    assert asyncio.run(pages.check(state, {REPO: True})) == []
    assert state.model.pages_missing() == set()
    # Losing the site again is news again.
    asyncio.run(pages.check(state, {REPO: False}))
    assert len(discord.posts) == 2


def test_the_old_setup_state_is_dropped(state: State, discord: Discord) -> None:
    state.db.set(pages.LEGACY_KV, '["pages: write"]')
    asyncio.run(pages.check(state, {REPO: True}))
    assert state.db.get(pages.LEGACY_KV) is None and discord.posts == []


class FakeOrg(FakeRepo):
    """sonarr without Pages, docker-mods (a helper repo, no image.yml) without Pages, and an archived image."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/orgs/example-org/repos":
            sonarr = {**fixture("api.repos")[0], "has_pages": False}
            helper = {**sonarr, "name": "docker-mods", "full_name": MODS}
            old = {**sonarr, "name": "docker-old", "full_name": "example-org/docker-old", "archived": True}
            return httpx.Response(200, json=[sonarr, helper, old])
        return super().__call__(request)


@pytest.mark.anyio
async def test_discovery_reads_has_pages_of_image_repos_only(state: State) -> None:
    async with mock_client(FakeOrg()) as http:
        discovery = fleet.Discovery(GitHub(http, "ghs_token"), "example-org", state.model, state.stream)
        await discovery.run()
    assert discovery.has_pages == {REPO: False}


def test_the_discovery_record_lists_repos_without_pages(
    state: State, credentials: AppCredentials, discord: Discord, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(state, credentials)

    async def run(self: fleet.Discovery) -> dict[str, int]:
        self.has_pages[REPO] = False
        return {"repos": 1}

    async def no_mods(*_args: Any) -> dict[str, int]:
        return {}

    monkeypatch.setattr(fleet.Discovery, "run", run)
    monkeypatch.setattr(mods, "discover", no_mods)
    assert asyncio.run(fleet.discover_once(state))["pages_missing"] == [REPO]
    assert fleet.last_discovery(state)["pages_missing"] == [REPO]  # type: ignore[index]
    assert len(discord.posts) == 1
    monkeypatch.setattr(state, "settings", state.settings.model_copy(update={"pages_check": False}))
    assert "pages_missing" not in asyncio.run(fleet.discover_once(state))
