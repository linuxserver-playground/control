import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from control.config import Settings
from control.github_app import AppCredentials
from control.setup import SETUP_STATE
from control.state import State

from .conftest import PUBLIC_URL
from .test_github_app import CONVERSION


def issued_state(state: State) -> str:
    stored = state.db.get(SETUP_STATE)
    assert stored
    return stored.split(" ")[0]


def test_setup_page_posts_manifest_to_github(client: TestClient, state: State) -> None:
    page = client.get("/setup")
    assert page.status_code == 200
    token = issued_state(state)
    assert f'action="https://github.com/organizations/example-org/settings/apps/new?state={token}"' in page.text
    assert "Create GitHub App" in page.text
    assert f"{PUBLIC_URL}/webhooks/github" in page.text


def test_setup_token_required_when_set(settings: Settings) -> None:
    guarded = settings.model_copy(update={"setup_token": "letmein"})
    from control.app import create_app

    with TestClient(create_app(guarded)) as client:
        assert client.get("/setup").status_code == 403
        assert client.get("/setup", params={"token": "wrong"}).status_code == 403
        assert client.get("/setup", params={"token": "letmein"}).status_code == 200


def test_callback_rejects_unknown_state(client: TestClient) -> None:
    client.get("/setup")
    assert client.get("/setup/callback", params={"code": "c", "state": "forged"}).status_code == 400


def test_callback_converts_persists_and_redirects_to_install(
    client: TestClient, state: State, monkeypatch: pytest.MonkeyPatch
) -> None:
    posted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.github.com":
            return httpx.Response(201, json=CONVERSION)
        posted.append(json.loads(request.content)["content"])
        return httpx.Response(200, json={"id": "1"})

    # Swap the app's shared client for one with a mock transport; Discord shares it too.
    mock = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(state, "http", mock)
    monkeypatch.setattr(state.discord, "client", mock)
    monkeypatch.setattr(state.discord, "webhook_url", "https://discord.com/api/webhooks/1/t")

    client.get("/setup")
    response = client.get(
        "/setup/callback", params={"code": "one-time", "state": issued_state(state)}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "https://github.com/apps/octoapp/installations/new"
    saved = state.credentials.load()
    assert saved and saved.slug == "octoapp"
    assert posted == ["GitHub App **octoapp** created: https://github.com/apps/octoapp"]
    # The state token is single use.
    assert state.db.get(SETUP_STATE) is None
    # And a second conversion is refused now that the app exists.
    assert client.get("/setup/callback", params={"code": "again", "state": "x"}).status_code == 409


def test_installed_stores_installation_and_redirects_home(
    client: TestClient, state: State, configured: AppCredentials
) -> None:
    response = client.get(
        "/setup/installed", params={"installation_id": 4242, "setup_action": "install"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert state.installation_id == 4242
    page = client.get("/setup")
    assert "installed" in page.text and "4242" in page.text


def test_setup_page_when_configured_links_install(client: TestClient, configured: AppCredentials) -> None:
    page = client.get("/setup")
    assert configured.install_url in page.text
    assert "not installed yet" in page.text
    assert parse_qs(urlparse(configured.install_url).query) == {}
