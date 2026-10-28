import hashlib
import hmac
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from control import auth, handlers
from control.app import create_app
from control.config import Settings
from control.github_app import AppCredentials
from control.state import INSTALLATION_ID, State

PUBLIC_URL = "https://ci.example.test:8443"
EVENTS_SECRET = "events-secret"
FIXTURES = Path(__file__).parent / "fixtures"
REPO = "example-org/docker-sonarr"


def fixture(name: str) -> Any:
    """A JSON fixture by stem: `fixture("workflow_run.completed")`."""
    return json.loads((FIXTURES / f"{name}.json").read_text())


def seed(state: State, *names: str) -> None:
    """Run fixtures through the handlers directly (no signatures); `control.*` fixtures are /events bodies."""
    for name in names:
        payload = fixture(name)
        event = f"control:{payload['type']}" if name.startswith("control.") else name.split(".")[0]
        handlers.process(state.model, state.stream, event, payload)


ACTIVE_SEED = (
    "repository.created",
    "workflow_run.requested",
    "workflow_job.queued",
    "workflow_run.in_progress",
    "workflow_job.in_progress",
    "pull_request.opened",
    "pull_request_review.submitted",
    "issues.opened",
    "release.published",
    "control.build.started",
)
"""A run in flight with one open PR, issue and release: what the live pages show."""

FINISHED_SEED = (*ACTIVE_SEED, "workflow_job.completed", "workflow_run.completed", "control.build.summary")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Each test gets its own data dir, so the db and credentials never leak between tests."""
    return Settings(
        public_url=PUBLIC_URL,
        data_dir=tmp_path / "data",
        github_org="example-org",
        events_secret=EVENTS_SECRET,
        publish_watch=False,  # tests drive publish.check_once themselves
        run_archive=False,  # tests drive archive.archive_once themselves
    )


@pytest.fixture(scope="session")
def private_pem() -> str:
    """One RSA key for the whole session; generating keys is the slow part of these tests."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
    ).decode()


@pytest.fixture
def credentials(private_pem: str) -> AppCredentials:
    return AppCredentials(
        id=12345,
        slug="linuxserver-ci-playground",
        client_id="Iv1.abc",
        client_secret="client-secret",
        webhook_secret="webhook-secret",
        pem=private_pem,
        html_url="https://github.com/apps/linuxserver-ci-playground",
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as client:
        yield client


@pytest.fixture
def state(client: TestClient) -> State:
    control: State = client.app.state.control  # type: ignore[attr-defined]
    return control


@pytest.fixture
def configured(state: State, credentials: AppCredentials) -> AppCredentials:
    """The app with its GitHub App credentials already on disk."""
    state.credentials.save(credentials)
    return credentials


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def deliver(
    client: TestClient,
    secret: str | None,
    event: str,
    payload: dict[str, Any],
    delivery_id: str = "d-1",
    signature: str | None = None,
) -> httpx2.Response:
    """POST a webhook the way GitHub does; `secret=None` leaves the signature header off."""
    body = json.dumps(payload).encode()
    headers = {"X-GitHub-Event": event, "X-GitHub-Delivery": delivery_id, "Content-Type": "application/json"}
    if signature is not None:
        headers["X-Hub-Signature-256"] = signature
    elif secret is not None:
        headers["X-Hub-Signature-256"] = sign(secret, body)
    return client.post("/webhooks/github", content=body, headers=headers)


def mock_client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def mock_github(state: State, monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    """Point every outbound call (GitHub, Discord) at a mock transport; Discord stays unconfigured."""
    mock = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(state, "http", mock)
    monkeypatch.setattr(state.discord, "client", mock)
    monkeypatch.setattr(state.tokens, "client", mock)


def install(state: State, credentials: AppCredentials, installation_id: int = 777) -> None:
    """Credentials on disk and an installation id in kv: what `installation_client` needs."""
    state.credentials.save(credentials)
    state.db.set(INSTALLATION_ID, str(installation_id))


def token_response(request: httpx.Request) -> httpx.Response | None:
    """The installation token exchange, for handlers that serve the rest of the API themselves."""
    if request.url.path.startswith("/app/installations/") and request.url.path.endswith("/access_tokens"):
        import datetime

        expires = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
        return httpx.Response(201, json={"token": "ghs_token", "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ")})
    return None


def sign_in(client: TestClient, state: State, login: str = "octocat", member: bool = True, **fields: Any) -> auth.User:
    """Put a signed session cookie on the test client, as /login/callback would, and return it (for the CSRF)."""
    user = auth.new_user(login, f"https://avatars.example/{login}", member).model_copy(update=fields)
    client.cookies.set(auth.SESSION_COOKIE, auth.sign(state.session_secret, user.model_dump()))
    return user


def mock_discord(state: State, monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    """Point the app's shared HTTP client (GitHub and Discord alike) at a mock transport and enable Discord."""
    mock = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(state, "http", mock)
    monkeypatch.setattr(state.discord, "client", mock)
    monkeypatch.setattr(state.discord, "webhook_url", "https://discord.com/api/webhooks/1/t")
    monkeypatch.setattr(state.tokens, "client", mock)


def post_event(client: TestClient, payload: dict[str, Any], secret: str | None = EVENTS_SECRET) -> httpx2.Response:
    """POST a control-event the way the shared workflow does; `secret=None` leaves the signature off."""
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if secret is not None:
        headers["X-Control-Signature"] = sign(secret, body)
    return client.post("/events", content=body, headers=headers)
