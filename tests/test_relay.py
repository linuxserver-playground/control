"""The hub-deploy-to-site relay: only a successful hub Deploy run dispatches, and only to the site repo."""

import asyncio
import datetime
import json
from typing import Any

import httpx
import pytest

from control import relay
from control.github import GitHub
from control.github_app import AppCredentials
from control.model import Repo
from control.state import State

from .conftest import fixture, install, mock_client, mock_github, token_response


def have(state: State, *names: str) -> None:
    for name in names:
        full = f"{state.settings.github_org}/{name}"
        state.model.upsert_repo(Repo.from_github({**fixture("api.repos")[0], "full_name": full, "name": name}))


def hub_run(
    state: State,
    *,
    action: str = "completed",
    conclusion: str = "success",
    name: str = "Deploy",
    repo: str | None = None,
) -> dict[str, Any]:
    return {
        "action": action,
        "workflow_run": {"id": 42, "name": name, "conclusion": conclusion, "head_sha": "a" * 40},
        "repository": {"full_name": repo or f"{state.settings.github_org}/{state.settings.hub_repo}"},
    }


def test_only_a_successful_hub_deploy_qualifies(state: State) -> None:
    assert relay.is_hub_deploy_success(state, hub_run(state))
    assert not relay.is_hub_deploy_success(state, hub_run(state, conclusion="failure"))
    assert not relay.is_hub_deploy_success(state, hub_run(state, action="requested"))
    assert not relay.is_hub_deploy_success(state, hub_run(state, name="Check"))
    assert not relay.is_hub_deploy_success(state, hub_run(state, repo=f"{state.settings.github_org}/docker-sonarr"))


def test_dispatch_goes_to_every_hub_consumer(
    state: State, credentials: AppCredentials, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(state, credentials)
    have(state, "site")
    seen: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if (token := token_response(request)) is not None:
            return token
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(204)

    mock_github(state, monkeypatch, handler)
    assert asyncio.run(relay.dispatch_site_refresh(state, hub_run(state))) is True
    body = {"event_type": "hub-updated", "client_payload": {"hub_run_id": 42, "head_sha": "a" * 40}}
    assert seen == [(f"/repos/{state.settings.github_org}/{repo}/dispatches", body) for repo in ("site",)]


def test_an_org_without_the_consumers_dispatches_to_none(
    state: State, credentials: AppCredentials, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Labs control: its org has no site repository, so a hub deploy nudges nothing."""
    install(state, credentials)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if (token := token_response(request)) is not None:
            return token
        seen.append(request.url.path)
        return httpx.Response(204)

    mock_github(state, monkeypatch, handler)
    assert relay.consumers(state) == ()
    assert asyncio.run(relay.dispatch_site_refresh(state, hub_run(state))) is True
    assert seen == []


def test_dispatch_without_installation_is_a_quiet_no(state: State) -> None:
    assert asyncio.run(relay.dispatch_site_refresh(state, hub_run(state))) is False


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
