"""The hub-deploy-to-site relay: only a successful hub Deploy run dispatches, and only to the site repo."""

import asyncio
import json
from typing import Any

import httpx
import pytest

from control import relay
from control.github_app import AppCredentials
from control.state import State

from .conftest import install, mock_github, token_response


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


def test_dispatch_goes_to_the_site_repo(
    state: State, credentials: AppCredentials, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(state, credentials)
    seen: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if (token := token_response(request)) is not None:
            return token
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(204)

    mock_github(state, monkeypatch, handler)
    assert asyncio.run(relay.dispatch_site_refresh(state, hub_run(state))) is True
    assert seen == [
        (
            f"/repos/{state.settings.github_org}/{state.settings.site_repo}/dispatches",
            {"event_type": "hub-updated", "client_payload": {"hub_run_id": 42, "head_sha": "a" * 40}},
        )
    ]


def test_dispatch_without_installation_is_a_quiet_no(state: State) -> None:
    assert asyncio.run(relay.dispatch_site_refresh(state, hub_run(state))) is False
