"""The hub's and the site's Deploys on control's slots: requested by what changed, dispatched at most once a slot."""

import datetime
import json
from typing import Any

import httpx
import pytest

from control import deploys
from control.github import GitHub
from control.model import Repo, Run
from control.state import State

from .conftest import fixture, mock_client

ORG = "example-org"
HUB = f"{ORG}/hub"
SITE = f"{ORG}/site"
# A hub slot starts here (a multiple of 400 s since the epoch); the site's starts 200 s later.
T0 = datetime.datetime.fromtimestamp(4_440_000 * 400, datetime.UTC)
SECOND = datetime.timedelta(seconds=1)


def have(state: State, *names: str) -> None:
    for name in names:
        full = f"{state.settings.github_org}/{name}"
        state.model.upsert_repo(Repo.from_github({**fixture("api.repos")[0], "full_name": full, "name": name}))


def running(state: State, repo: str, run_id: int, at: datetime.datetime) -> None:
    data = {
        "id": run_id,
        "name": "Deploy",
        "event": "workflow_dispatch",
        "head_branch": "main",
        "head_sha": "c" * 40,
        "status": "in_progress",
        "conclusion": None,
        "html_url": f"https://github.com/{repo}/actions/runs/{run_id}",
        "created_at": at.isoformat(),
        "updated_at": at.isoformat(),
        "run_attempt": 1,
    }
    state.model.upsert_run(Run.from_github(repo, data))


class World:
    def __init__(self, status: int = 204) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.status = status

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.posts.append((request.url.path, json.loads(request.content)))
        return httpx.Response(self.status)


async def tick(state: State, world: World, at: datetime.datetime) -> int:
    async with mock_client(world.handler) as http:
        return await deploys.tick(state, GitHub(http, "ghs_token"), at)


def dispatch(repo: str, inputs: dict[str, str] | None = None) -> tuple[str, dict[str, Any]]:
    return (
        f"/repos/{repo}/actions/workflows/deploy.yml/dispatches",
        {"ref": "main", **({"inputs": inputs} if inputs else {})},
    )


def quiet(state: State, at: datetime.datetime) -> None:
    """No backstop due: the hub dispatched just now and the site had its daily full refresh."""
    state.db.set(deploys.FULL_REFRESH_DAY, at.date().isoformat())
    for repo in (HUB, SITE):
        state.db.set(deploys.KEY + repo, json.dumps({"last_dispatch_at": (at - deploys.SLOT).isoformat()}))


@pytest.mark.anyio
async def test_nothing_requested_dispatches_nothing(state: State) -> None:
    have(state, "hub", "site")
    quiet(state, T0)
    world = World()
    assert await tick(state, world, T0) == 0 and world.posts == []


@pytest.mark.anyio
async def test_requests_wait_for_the_next_slot_and_go_out_together(state: State) -> None:
    have(state, "hub", "site")
    quiet(state, T0)
    world = World()
    assert deploys.request(state, HUB, "release sonarr 1-ls1", force=f"{ORG}/docker-sonarr@1-ls1")
    assert deploys.request(state, HUB, "release radarr 2-ls3", force=f"{ORG}/docker-radarr@2-ls3")
    assert await tick(state, world, T0 + 30 * SECOND) == 1
    force = f"{ORG}/docker-sonarr@1-ls1,{ORG}/docker-radarr@2-ls3"
    assert world.posts == [dispatch(HUB, {"force": force})]
    # A release a minute later waits for the next slot: one dispatch per slot, 400 s apart at least.
    deploys.request(state, HUB, "release sonarr 1-ls2", force=f"{ORG}/docker-sonarr@1-ls2")
    assert await tick(state, world, T0 + 90 * SECOND) == 0
    assert await tick(state, world, T0 + deploys.SLOT + 20 * SECOND) == 0  # a new slot, but not 400 s yet
    assert await tick(state, world, T0 + deploys.SLOT + 31 * SECOND) == 1
    assert world.posts[-1] == dispatch(HUB, {"force": f"{ORG}/docker-sonarr@1-ls2"})
    assert deploys.status(state, T0)[HUB]["pending"] is None


@pytest.mark.anyio
async def test_at_most_nine_an_hour(state: State) -> None:
    have(state, "hub")
    world = World()
    at = T0
    while at < T0 + datetime.timedelta(hours=1):
        deploys.request(state, HUB, "busy")
        await tick(state, world, at)
        at += 30 * SECOND
    assert len(world.posts) == 9


@pytest.mark.anyio
async def test_a_running_deploy_holds_the_next(state: State) -> None:
    have(state, "hub", "site")
    quiet(state, T0)
    world = World()
    running(state, HUB, 900, T0 - 60 * SECOND)
    deploys.request(state, HUB, "main abc1234")
    deploys.request(state, SITE, "hub published abc1234")
    # The hub waits for its own run; the site waits for the hub's too, so it reads what the hub serves.
    assert await tick(state, world, T0 + 250 * SECOND) == 0
    assert world.posts == []


@pytest.mark.anyio
async def test_the_site_slot_is_half_a_slot_after_the_hubs(state: State) -> None:
    have(state, "hub", "site")
    quiet(state, T0)
    world = World()
    deploys.request(state, SITE, "main abc1234")
    assert await tick(state, world, T0 + 100 * SECOND) == 1  # the site's slot that began 200 s before T0
    deploys.request(state, SITE, "main def5678")
    assert await tick(state, world, T0 + 300 * SECOND) == 0  # a new site slot, but 200 s after the last dispatch
    assert await tick(state, world, T0 + 500 * SECOND) == 1
    assert world.posts == [dispatch(SITE), dispatch(SITE)]


@pytest.mark.anyio
async def test_a_failed_dispatch_stays_requested(state: State) -> None:
    have(state, "hub")
    quiet(state, T0)
    deploys.request(state, HUB, "main abc1234")
    assert await tick(state, World(status=500), T0) == 0
    assert deploys.status(state, T0)[HUB]["pending"]["count"] == 1
    world = World()
    assert await tick(state, world, T0 + 60 * SECOND) == 1 and world.posts == [dispatch(HUB)]


def test_requests_made_while_a_dispatch_was_in_flight_stay() -> None:
    sent = {"count": 2, "reasons": ["a", "b"], "force": ["x@1"]}
    now = {"count": 3, "reasons": ["a", "b", "c"], "force": ["x@1", "y@2"], "refresh_all": True}
    at = T0
    assert deploys._after(now, sent, at) == {
        "since": at.isoformat(timespec="seconds"),
        "count": 1,
        "reasons": ["c"],
        "force": ["y@2"],
        "refresh_all": True,
    }
    assert deploys._after(sent, sent, at) is None


@pytest.mark.anyio
async def test_backstops(state: State) -> None:
    have(state, "hub", "site")
    world = World()
    # Never dispatched: the hub reconciles; past 04:17 UTC the site makes its daily full refresh.
    morning = datetime.datetime(2026, 11, 7, 4, 20, tzinfo=datetime.UTC)
    assert await tick(state, world, morning) == 2
    assert dispatch(HUB) in world.posts and dispatch(SITE, {"refresh_all": "true"}) in world.posts
    world.posts.clear()
    # Within the hour nothing else is due, and the full refresh is once a day.
    assert await tick(state, world, morning + datetime.timedelta(minutes=30)) == 0
    assert await tick(state, world, morning + datetime.timedelta(minutes=61)) == 1
    assert world.posts == [dispatch(HUB)]


def push(repo: str, ref: str, message: str = "x", after: str = "abc1234def") -> dict[str, Any]:
    return {
        "ref": ref,
        "after": after,
        "repository": {"full_name": repo, "name": repo.rsplit("/", 1)[-1]},
        "head_commit": {"message": message},
    }


def reasons(state: State, repo: str) -> list[str]:
    pending = deploys.status(state)[repo]["pending"]
    return pending["reasons"] if pending else []


def test_webhooks_that_make_a_deploy_due(state: State) -> None:
    have(state, "hub", "site", "docker-sonarr")
    deploys.observe(state, "push", push(HUB, "refs/heads/main"))
    deploys.observe(state, "push", push(HUB, "refs/heads/data", "Update data"))  # the Deploy's own data commit
    deploys.observe(state, "push", push(HUB, "refs/heads/data", "Refresh data"))
    assert reasons(state, HUB) == ["main abc1234", "refreshed data"]
    deploys.observe(state, "push", push(HUB, "refs/heads/gh-pages"))
    deploys.observe(state, "push", push(SITE, "refs/heads/main"))
    deploys.observe(state, "push", push(SITE, "refs/heads/data"))  # the site's snapshots
    deploys.observe(state, "push", push(SITE, "refs/heads/edit/someone/post"))
    assert reasons(state, SITE) == ["hub published abc1234", "main abc1234"]
    deploys.observe(state, "push", push(f"{ORG}/docker-sonarr", "refs/heads/main"))
    deploys.observe(state, "repository", {"action": "deleted", "repository": {"name": "docker-gone"}})
    deploys.observe(state, "repository", {"action": "edited", "repository": {"name": "docker-sonarr"}})
    assert reasons(state, HUB)[-1] == "docker-gone deleted" and len(reasons(state, HUB)) == 3


def test_an_org_without_a_hub_or_site_requests_nothing(state: State) -> None:
    assert deploys.targets(state) == []
    assert not deploys.request(state, HUB, "release")
    assert deploys.status(state) == {}
