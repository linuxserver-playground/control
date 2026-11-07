"""The hub's hourly Refresh dispatch."""

import datetime
import json
from typing import Any

import httpx
import pytest

from control import relay
from control.github import GitHub
from control.model import Repo
from control.state import State

from .conftest import fixture, mock_client


def have(state: State, *names: str) -> None:
    for name in names:
        full = f"{state.settings.github_org}/{name}"
        state.model.upsert_repo(Repo.from_github({**fixture("api.repos")[0], "full_name": full, "name": name}))


@pytest.mark.anyio
async def test_hub_refresh_dispatches_hourly(state: State) -> None:
    now = datetime.datetime(2026, 10, 25, 12, 0, tzinfo=datetime.UTC)
    seen: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(204)

    have(state, "hub")
    assert relay.hub_refresh_due(state, now)
    async with mock_client(handler) as http:
        assert await relay.dispatch_hub_refresh(state, GitHub(http, "ghs_token"), now)
    path = f"/repos/{state.settings.github_org}/hub/actions/workflows/refresh.yml/dispatches"
    assert seen == [(path, {"ref": "main"})]
    assert not relay.hub_refresh_due(state, now + datetime.timedelta(minutes=59))
    assert relay.hub_refresh_due(state, now + datetime.timedelta(hours=1))


@pytest.mark.anyio
async def test_failed_hub_refresh_stays_due(state: State) -> None:
    have(state, "hub")
    async with mock_client(lambda request: httpx.Response(422)) as http:
        assert not await relay.dispatch_hub_refresh(state, GitHub(http, "ghs_token"))
    assert relay.hub_refresh_due(state)


@pytest.mark.anyio
async def test_no_hub_refresh_in_an_org_without_a_hub(state: State) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request {request.url}")

    async with mock_client(handler) as http:
        assert not await relay.dispatch_hub_refresh(state, GitHub(http, "ghs_token"))
