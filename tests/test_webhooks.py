from fastapi.testclient import TestClient

from control.github_app import AppCredentials
from control.state import State
from control.webhooks import signature_valid

from .conftest import deliver, sign

PING = {"zen": "Keep it logically awesome.", "hook_id": 1}


def test_signature_helper() -> None:
    assert signature_valid("s", b"body", sign("s", b"body"))
    assert not signature_valid("s", b"body", sign("other", b"body"))
    assert not signature_valid("s", b"body", None)
    assert not signature_valid("s", b"body", "sha256=nothex")


def test_valid_signature_stores_delivery(client: TestClient, configured: AppCredentials, state: State) -> None:
    response = deliver(client, configured.webhook_secret, "ping", PING, delivery_id="ping-1")
    assert response.status_code == 202
    assert state.db.count_deliveries() == 1
    (stored,) = state.db.recent_deliveries()
    assert (stored.event, stored.delivery_id, stored.action, stored.repo) == ("ping", "ping-1", None, None)


def test_invalid_signature_is_401(client: TestClient, configured: AppCredentials, state: State) -> None:
    response = deliver(client, "wrong-secret", "ping", PING)
    assert response.status_code == 401
    assert state.db.count_deliveries() == 0


def test_missing_signature_is_401(client: TestClient, configured: AppCredentials) -> None:
    assert deliver(client, None, "ping", PING).status_code == 401


def test_unconfigured_app_is_503(client: TestClient) -> None:
    assert deliver(client, "anything", "ping", PING).status_code == 503


def test_duplicate_delivery_ignored(client: TestClient, configured: AppCredentials, state: State) -> None:
    payload = {"action": "opened", "repository": {"full_name": "example-org/docker-x"}}
    first = deliver(client, configured.webhook_secret, "pull_request", payload, delivery_id="dup")
    second = deliver(client, configured.webhook_secret, "pull_request", payload, delivery_id="dup")
    assert (first.status_code, second.status_code) == (202, 202)
    assert state.db.count_deliveries() == 1
    (stored,) = state.db.recent_deliveries()
    assert (stored.action, stored.repo) == ("opened", "example-org/docker-x")


def test_installation_event_stores_installation_id(
    client: TestClient, configured: AppCredentials, state: State
) -> None:
    assert state.installation_id is None
    payload = {"action": "created", "installation": {"id": 777, "account": {"login": "example-org"}}}
    assert deliver(client, configured.webhook_secret, "installation", payload, delivery_id="inst-1").status_code == 202
    assert state.installation_id == 777
    (stored,) = state.db.recent_deliveries()
    assert stored.installation_id == 777

    gone = {"action": "deleted", "installation": {"id": 777}}
    deliver(client, configured.webhook_secret, "installation", gone, delivery_id="inst-2")
    assert state.installation_id is None


def test_status_page_lists_deliveries(client: TestClient, configured: AppCredentials) -> None:
    deliver(client, configured.webhook_secret, "push", {"repository": {"full_name": "example-org/docker-y"}})
    page = client.get("/status")
    assert page.status_code == 200
    assert "example-org/docker-y" in page.text
    assert configured.slug in page.text
    assert client.get("/healthz").json() == {"status": "ok", "db": True}
