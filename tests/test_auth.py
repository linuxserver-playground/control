import datetime
import json
from collections.abc import Iterator
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from control import auth
from control.app import create_app
from control.config import Settings
from control.github_app import AppCredentials
from control.state import State

from .conftest import PUBLIC_URL, install, mock_github, sign_in, token_response


class FakeOAuth:
    """GitHub's OAuth token exchange, /user, and the org membership lookup."""

    def __init__(self, member: bool = True, login: str = "octocat") -> None:
        self.member = member
        self.login = login
        self.exchanges: list[dict[str, list[str]]] = []
        self.memberships: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if (token := token_response(request)) is not None:
            return token
        path = request.url.path
        if request.url.host == "github.com" and path == "/login/oauth/access_token":
            assert request.headers["Accept"] == "application/json"
            self.exchanges.append(parse_qs(request.content.decode()))
            return httpx.Response(200, json={"access_token": "gho_user", "token_type": "bearer"})
        if path == "/user":
            assert request.headers["Authorization"] == "Bearer gho_user"
            return httpx.Response(200, json={"login": self.login, "avatar_url": "https://avatars.example/o"})
        if path == "/user/memberships/orgs/example-org":
            assert request.headers["Authorization"] == "Bearer gho_user"  # the user's own token
            self.memberships.append("self")
            if self.member:
                return httpx.Response(200, json={"state": "active", "role": "admin"})
            return httpx.Response(404, json={"message": "Not Found"})
        if path.startswith("/orgs/example-org/memberships/"):
            assert request.headers["Authorization"] == "Bearer ghs_token"  # the installation token, not the user's
            self.memberships.append(path.rsplit("/", 1)[-1])
            if self.member:
                return httpx.Response(200, json={"state": "active", "role": "member"})
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(404, json={"message": f"no fixture for {path}"})


@pytest.fixture
def https_client(settings: Settings) -> Iterator[TestClient]:
    """The session cookie is Secure (the public URL is https), so the client must speak https to send it back."""
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        yield client


@pytest.fixture
def https_state(https_client: TestClient) -> State:
    control: State = https_client.app.state.control  # type: ignore[attr-defined]
    return control


def test_sign_and_unsign_roundtrip_and_tamper() -> None:
    token = auth.sign("s", {"login": "o", "issued_at": "2026-10-24T12:00:00+00:00"})
    assert auth.unsign("s", token, datetime.timedelta(days=3650)) == {
        "login": "o",
        "issued_at": "2026-10-24T12:00:00+00:00",
    }
    assert auth.unsign("other", token, datetime.timedelta(days=3650)) is None
    body, _, mac = token.rpartition(".")
    assert auth.unsign("s", body + "x." + mac, datetime.timedelta(days=1)) is None
    assert auth.unsign("s", token, datetime.timedelta(seconds=1)) is None  # expired
    assert auth.unsign("s", None, datetime.timedelta(days=1)) is None
    assert auth.unsign("s", "garbage", datetime.timedelta(days=1)) is None


def test_login_redirects_to_github_with_state_cookie(
    client: TestClient, state: State, configured: AppCredentials
) -> None:
    response = client.get("/login", params={"next": "/management"}, follow_redirects=False)
    assert response.status_code == 303
    target = urlparse(response.headers["location"])
    assert target.netloc == "github.com" and target.path == "/login/oauth/authorize"
    query = parse_qs(target.query)
    assert query["client_id"] == [configured.client_id]
    assert query["redirect_uri"] == [f"{PUBLIC_URL}/login/callback"]
    cookie = response.cookies.get(auth.OAUTH_COOKIE)
    pending = auth.unsign(state.session_secret, cookie, auth.OAUTH_TTL)
    assert pending and pending["state"] == query["state"][0] and pending["next"] == "/management"
    set_cookie = response.headers["set-cookie"]
    assert "HttpOnly" in set_cookie and "Secure" in set_cookie and "SameSite=lax" in set_cookie.replace("Lax", "lax")


def test_login_needs_the_app(client: TestClient) -> None:
    assert client.get("/login", follow_redirects=False).status_code == 503


def test_callback_signs_in_a_member(
    https_client: TestClient, https_state: State, credentials: AppCredentials, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(https_state, credentials)
    github = FakeOAuth(member=True)
    mock_github(https_state, monkeypatch, github)
    start = https_client.get("/login", params={"next": "/runs"}, follow_redirects=False)
    state_token = parse_qs(urlparse(start.headers["location"]).query)["state"][0]

    assert https_client.get("/login/callback", params={"code": "c", "state": "forged"}).status_code == 400
    done = https_client.get(
        "/login/callback", params={"code": "one-time", "state": state_token}, follow_redirects=False
    )
    assert done.status_code == 303 and done.headers["location"] == "/runs"
    assert github.exchanges == [
        {
            "client_id": [credentials.client_id],
            "client_secret": [credentials.client_secret],
            "code": ["one-time"],
            "redirect_uri": [f"{PUBLIC_URL}/login/callback"],
        }
    ]
    assert github.memberships == ["octocat"]
    cookie = https_client.cookies.get(auth.SESSION_COOKIE)
    user = auth.User.model_validate(auth.unsign(https_state.session_secret, cookie, auth.SESSION_TTL) or {})
    assert user.login == "octocat" and user.member and user.csrf
    session_header = next(h for h in done.headers.get_list("set-cookie") if h.startswith(auth.SESSION_COOKIE))
    # An external origin is allowed by default, so the cookie must travel on its credentialed fetches.
    assert "HttpOnly" in session_header and "Secure" in session_header and "samesite=none" in session_header.lower()

    page = https_client.get("/management")
    assert "Signed in as <strong>octocat</strong>" in page.text and 'action="/manage/dispatch"' in page.text
    assert "Sign out" in https_client.get("/").text

    # Sign out needs the CSRF token; with it the cookie is cleared.
    https_client.post("/logout", data={"csrf": "wrong", "next": "/"}, follow_redirects=False)
    assert https_client.cookies.get(auth.SESSION_COOKIE)
    out = https_client.post("/logout", data={"csrf": user.csrf, "next": "/"}, follow_redirects=False)
    assert out.status_code == 303 and not https_client.cookies.get(auth.SESSION_COOKIE)


def test_callback_signs_in_a_non_member_as_guest(
    https_client: TestClient, https_state: State, credentials: AppCredentials, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(https_state, credentials)
    mock_github(https_state, monkeypatch, FakeOAuth(member=False, login="stranger"))
    start = https_client.get("/login", follow_redirects=False)
    state_token = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    done = https_client.get("/login/callback", params={"code": "c", "state": state_token}, follow_redirects=False)
    assert done.status_code == 303 and done.headers["location"] == "/"
    page = https_client.get("/management")
    assert "not an active member" in page.text and 'action="/manage/' not in page.text
    assert "guest" in https_client.get("/").text


def test_callback_without_installation_means_no_member(
    https_client: TestClient, https_state: State, credentials: AppCredentials, monkeypatch: pytest.MonkeyPatch
) -> None:
    https_state.credentials.save(credentials)  # app exists but is not installed: membership cannot be checked
    github = FakeOAuth(member=True)
    mock_github(https_state, monkeypatch, github)
    start = https_client.get("/login", follow_redirects=False)
    state_token = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    https_client.get("/login/callback", params={"code": "c", "state": state_token}, follow_redirects=False)
    user = auth.User.model_validate(
        auth.unsign(https_state.session_secret, https_client.cookies.get(auth.SESSION_COOKIE), auth.SESSION_TTL) or {}
    )
    assert user.login == "octocat" and not user.member and github.memberships == []


def test_pages_take_membership_from_the_cookie(
    client: TestClient, state: State, credentials: AppCredentials, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(state, credentials)
    github = FakeOAuth(member=False)
    mock_github(state, monkeypatch, github)
    old = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=2)).isoformat(timespec="seconds")
    sign_in(client, state, member=True, checked_at=old)
    # Page loads never ask GitHub, however old the verdict: actions confirm it (test_manage).
    assert "not an active member" not in client.get("/management").text
    assert client.get("/api/me").json()["member"] is True
    assert github.memberships == []


def test_safe_next() -> None:
    assert auth.safe_next("/runs/5") == "/runs/5"
    assert auth.safe_next("//evil.example") == "/"
    assert auth.safe_next("https://evil.example") == "/"
    assert auth.safe_next(None) == "/"
    site = ("https://linuxserver-playground.github.io",)
    assert auth.safe_next("https://linuxserver-playground.github.io/site/fleet/?x=1", site).endswith("/site/fleet/?x=1")
    assert (
        auth.safe_next("HTTPS://LinuxServer-Playground.github.io/site/", site)
        == "HTTPS://LinuxServer-Playground.github.io/site/"
    )
    assert auth.safe_next("https://linuxserver-playground.github.io.evil.example/", site) == "/"
    assert auth.safe_next("https://linuxserver-playground.github.io:8443/", site) == "/"
    assert auth.safe_next("http://linuxserver-playground.github.io/", site) == "/"
    assert auth.safe_next("https://linuxserver-playground.github.io/site/", ()) == "/"
    assert auth.safe_next("javascript:alert(1)", site) == "/"


def test_session_secret_from_env_or_random(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    monkeypatch.setenv("CONTROL_SESSION_SECRET", "fixed")
    monkeypatch.setenv("CONTROL_SCHEDULER", "false")
    from_env = Settings.from_env()
    assert from_env.session_secret == "fixed" and from_env.scheduler is False
    with TestClient(create_app(settings.model_copy(update={"session_secret": "fixed"}))) as client:
        first: State = client.app.state.control  # type: ignore[attr-defined]
        assert first.session_secret == "fixed"
    with TestClient(create_app(settings)) as client:
        second: State = client.app.state.control  # type: ignore[attr-defined]
        assert second.session_secret != "fixed" and len(second.session_secret) == 64


def test_current_user_ignores_bad_cookies(client: TestClient, state: State) -> None:
    client.cookies.set(auth.SESSION_COOKIE, "not-a-token")
    assert "Sign in" in client.get("/").text
    client.cookies.set(auth.SESSION_COOKIE, auth.sign(state.session_secret, {"issued_at": "2026-10-24T00:00:00+00:00"}))
    assert "Sign in" in client.get("/").text  # signed but not a User
    assert json.loads(client.get("/api/management.json").text)["signed_in"] is None


def test_classic_oauth_app_sign_in_uses_scopes_and_the_user_token(
    https_client: TestClient, https_state: State, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With OAUTH_CLIENT_ID/SECRET set, GitHub shows the familiar scope consent and no App permission is needed."""
    monkeypatch.setattr(
        https_state,
        "settings",
        https_state.settings.model_copy(update={"oauth_client_id": "Ov23li", "oauth_client_secret": "s3"}),
    )
    github = FakeOAuth(member=True)
    mock_github(https_state, monkeypatch, github)
    start = https_client.get("/login", follow_redirects=False)
    query = parse_qs(urlparse(start.headers["location"]).query)
    assert query["client_id"] == ["Ov23li"] and query["scope"] == ["read:user read:org"]
    done = https_client.get("/login/callback", params={"code": "c", "state": query["state"][0]}, follow_redirects=False)
    assert done.status_code == 303
    assert github.exchanges[0]["client_id"] == ["Ov23li"] and github.exchanges[0]["client_secret"] == ["s3"]
    assert github.memberships == ["self"]  # read with the user's token, never the installation's
    cookie = https_client.cookies.get(auth.SESSION_COOKIE)
    user = auth.User.model_validate(auth.unsign(https_state.session_secret, cookie, auth.SESSION_TTL) or {})
    assert user.login == "octocat" and user.member


def test_action_is_refused_when_membership_cannot_be_confirmed(
    https_state: State, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from fastapi import HTTPException

    def handler(request: httpx.Request) -> httpx.Response:
        if (token := token_response(request)) is not None:
            return token
        return httpx.Response(403, json={"message": "Resource not accessible by integration"})

    mock_github(https_state, monkeypatch, handler)
    user = auth.new_user("octocat", None, True)
    with pytest.raises(HTTPException) as refused:
        asyncio.run(auth.confirm_member(https_state, user))
    assert refused.value.status_code == 503
