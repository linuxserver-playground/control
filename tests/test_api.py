"""The website's contract: CORS for the allowed origins, the cookie that crosses sites, /api/me, the JSON
actions with the double-submitted CSRF token, and the sign-in round trip back to the site."""

import datetime
import json
from collections.abc import Iterator
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.responses import Response
from fastapi.testclient import TestClient

from control import auth, manage, views
from control.app import create_app
from control.config import Settings
from control.github_app import AppCredentials
from control.state import State

from .conftest import FINISHED_SEED, REPO, install, mock_github, seed, sign_in
from .test_auth import FakeOAuth
from .test_manage import FakeActions

SITE = "https://linuxserver-playground.github.io"
ELSEWHERE = "https://evil.example"

JSON_ACTIONS: list[tuple[str, dict[str, object]]] = [
    ("rerun", {"run_id": 5001}),
    ("cancel", {"run_id": 5001}),
    ("dispatch", {"repo": "docker-sonarr", "branch": "main", "reason": "manual"}),
    ("packages", {"repo": "docker-sonarr", "branch": "main"}),
    ("skip", {"scope": "sonarr/main", "reason": "x"}),
    ("unskip", {"scope": "sonarr/main"}),
    ("reconcile", {}),
]


@pytest.fixture
def github(state: State, credentials: AppCredentials, monkeypatch: pytest.MonkeyPatch) -> FakeActions:
    install(state, credentials)
    fake = FakeActions()
    mock_github(state, monkeypatch, fake)
    seed(state, *FINISHED_SEED)
    return fake


@pytest.fixture
def closed_client(settings: Settings) -> Iterator[TestClient]:
    """No external origin allowed: no CORS, Lax cookies."""
    with TestClient(create_app(settings.model_copy(update={"allowed_origins": ()}))) as client:
        yield client


def actions(client: TestClient) -> list[tuple[str, str, str | None, str]]:
    page = views.ActionsPage.model_validate(client.get("/api/actions.json").json())
    return [(a.actor, a.action, a.target, a.result) for a in page.actions]


# -- settings and CORS ---------------------------------------------------------------------------------------------


def test_allowed_origins_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings().allowed_origins == (SITE,)
    monkeypatch.setenv("CONTROL_ALLOWED_ORIGINS", f" {SITE}/ , https://Preview.Example:8443,,")
    assert Settings.from_env().allowed_origins == (SITE, "https://preview.example:8443")
    monkeypatch.setenv("CONTROL_ALLOWED_ORIGINS", "")
    assert Settings.from_env().allowed_origins == ()
    for bad in ("*", "https://*.github.io", "github.io", f"{SITE}/site", "ftp://x"):
        monkeypatch.setenv("CONTROL_ALLOWED_ORIGINS", bad)
        with pytest.raises(ValueError):
            Settings.from_env()


def test_cors_only_for_allowed_origins(client: TestClient, closed_client: TestClient) -> None:
    allowed = client.get("/api/fleet.json", headers={"Origin": SITE})
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == SITE
    assert allowed.headers["access-control-allow-credentials"] == "true"
    assert "origin" in allowed.headers.get("vary", "").lower()
    assert "access-control-allow-origin" not in client.get("/api/fleet.json", headers={"Origin": ELSEWHERE}).headers
    assert "access-control-allow-origin" not in client.get("/api/fleet.json").headers
    assert "access-control-allow-origin" not in closed_client.get("/api/fleet.json", headers={"Origin": SITE}).headers

    preflight = client.options(
        "/api/actions/skip",
        headers={
            "Origin": SITE,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type, x-csrf-token",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == SITE
    assert "POST" in preflight.headers["access-control-allow-methods"]
    allowed_headers = preflight.headers["access-control-allow-headers"].lower()
    assert "x-csrf-token" in allowed_headers and "content-type" in allowed_headers
    denied = client.options("/api/actions/skip", headers={"Origin": ELSEWHERE, "Access-Control-Request-Method": "POST"})
    assert denied.status_code == 400 and "access-control-allow-origin" not in denied.headers


# -- the cookie ----------------------------------------------------------------------------------------------------


def test_cookie_is_cross_site_only_when_an_origin_is_allowed(settings: Settings) -> None:
    user = auth.new_user("octocat", None, True)
    cases: list[tuple[dict[str, object], str, bool]] = [
        ({}, "none", True),  # https public URL, the default origin allowed
        ({"allowed_origins": ()}, "lax", True),
        ({"public_url": "http://localhost:8080"}, "lax", False),  # browsers refuse SameSite=None without Secure
    ]
    for update, expected, secure in cases:
        with TestClient(create_app(settings.model_copy(update=update))) as client:
            state: State = client.app.state.control  # type: ignore[attr-defined]
            assert auth.session_samesite(state) == expected
            response = Response()
            auth.set_session(response, state, user)
            header = response.headers["set-cookie"].lower()
            assert f"samesite={expected}" in header and ("secure" in header) is secure
            cleared = Response()
            auth.clear_session(cleared, state)
            assert f"samesite={expected}" in cleared.headers["set-cookie"].lower()


# -- /api/me and /api/logout ---------------------------------------------------------------------------------------


def test_me_is_never_401(client: TestClient, state: State) -> None:
    anonymous = client.get("/api/me", headers={"Origin": SITE})
    assert anonymous.status_code == 200 and anonymous.headers["access-control-allow-origin"] == SITE
    assert anonymous.json() == {
        "authenticated": False,
        "login": None,
        "avatar_url": None,
        "member": False,
        "checked_at": None,
        "csrf": None,
    }
    client.cookies.set(auth.SESSION_COOKIE, "forged.token")
    assert client.get("/api/me").json()["authenticated"] is False
    body, _, mac = auth.sign(state.session_secret, auth.new_user("x", None, True).model_dump()).rpartition(".")
    client.cookies.set(auth.SESSION_COOKIE, body + "." + mac[::-1])  # wrong signature
    assert client.get("/api/me").json()["authenticated"] is False
    client.cookies.set(auth.SESSION_COOKIE, auth.sign("other-secret", auth.new_user("x", None, True).model_dump()))
    assert client.get("/api/me").json()["authenticated"] is False

    client.cookies.clear()
    user = sign_in(client, state)
    me = auth.Me.model_validate(client.get("/api/me").json())
    assert me.authenticated and me.login == "octocat" and me.member and me.csrf == user.csrf
    assert me.avatar_url == "https://avatars.example/octocat" and me.checked_at == user.checked_at


def test_me_refreshes_a_stale_membership(client: TestClient, state: State, github: FakeActions) -> None:
    stale_at = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=2)).isoformat(timespec="seconds")
    sign_in(client, state, member=False, checked_at=stale_at)
    response = client.get("/api/me")
    me = auth.Me.model_validate(response.json())
    assert me.member and me.checked_at and me.checked_at > stale_at
    assert "/orgs/example-org/memberships/octocat" in github.gets
    header = response.headers["set-cookie"].lower()
    assert header.startswith(auth.SESSION_COOKIE) and "samesite=none" in header and "secure" in header


def test_logout_json(client: TestClient, state: State) -> None:
    assert client.post("/api/logout").status_code == 401
    user = sign_in(client, state)
    assert client.post("/api/logout", headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert client.get("/api/me").json()["authenticated"] is True
    out = client.post("/api/logout", headers={"X-CSRF-Token": user.csrf, "Origin": SITE})
    assert out.status_code == 204 and out.headers["access-control-allow-origin"] == SITE
    header = out.headers["set-cookie"].lower()
    # The clearing cookie is Secure too, so the plain-http test jar ignores it; the header is the proof here.
    assert header.startswith(f"{auth.SESSION_COOKIE}=") and "samesite=none" in header and "max-age=0" in header


# -- JSON actions --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("action", "body"), JSON_ACTIONS)
def test_json_action_needs_a_member_and_the_csrf_header(
    client: TestClient, state: State, github: FakeActions, action: str, body: dict[str, object]
) -> None:
    path = f"/api/actions/{action}"
    anonymous = client.post(path, json=body)
    assert anonymous.status_code == 401 and anonymous.json() == {"detail": "sign in first"}
    guest = sign_in(client, state, login="stranger", member=False)
    assert client.post(path, json=body, headers={"X-CSRF-Token": guest.csrf}).status_code == 403
    client.cookies.clear()
    member = sign_in(client, state)
    # The cookie alone is ambient: without the double-submitted token the action is refused.
    missing = client.post(path, json=body)
    assert missing.status_code == 403 and "CSRF" in missing.json()["detail"]
    assert client.post(path, json=body, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert client.post(path, json={**body, "csrf": member.csrf}).status_code == 403  # body is not the header
    assert github.posts == [] and actions(client) == [] and state.model.skips() == []


def test_json_actions_share_the_form_code_path(client: TestClient, state: State, github: FakeActions) -> None:
    user = sign_in(client, state)
    headers = {"X-CSRF-Token": user.csrf, "Origin": SITE}
    done = client.post("/api/actions/rerun", json={"run_id": 5001}, headers=headers)
    assert done.status_code == 200 and done.headers["access-control-allow-origin"] == SITE
    result = manage.ActionResult.model_validate(done.json())
    assert result.ok and result.detail == f"re-run of failed jobs requested for {REPO} run 5001"
    assert client.post("/api/actions/cancel", json={"run_id": 5001}, headers=headers).json()["ok"] is True
    dispatch = {"repo": "docker-sonarr", "branch": "develop", "reason": "rebuild"}
    assert client.post("/api/actions/dispatch", json=dispatch, headers=headers).json()["ok"] is True
    packages = {"repo": f"{REPO}", "branch": "main"}
    assert client.post("/api/actions/packages", json=packages, headers=headers).json()["ok"] is True
    assert github.posts == [
        (f"/repos/{REPO}/actions/runs/5001/rerun-failed-jobs", None),
        (f"/repos/{REPO}/actions/runs/5001/cancel", None),
        (f"/repos/{REPO}/actions/workflows/build.yml/dispatches", {"ref": "develop", "inputs": {"reason": "rebuild"}}),
        (f"/repos/{REPO}/actions/workflows/build.yml/dispatches", {"ref": "main", "inputs": {"reason": "packages"}}),
    ]
    skipped = client.post("/api/actions/skip", json={"scope": "/sonarr/main/", "reason": "hold"}, headers=headers)
    assert skipped.json()["ok"] and [s.scope for s in state.model.skips()] == ["sonarr/main"]
    assert client.post("/api/actions/unskip", json={"scope": "sonarr/main"}, headers=headers).json()["ok"] is True
    # A refused operation is still logged, and comes back as ok: false rather than an HTTP error.
    again = client.post("/api/actions/unskip", json={"scope": "sonarr/main"}, headers=headers).json()
    assert again["ok"] is False and again["detail"] == "no skip flag on sonarr/main"
    assert again["action_id"] == state.model.recent_actions(1)[0].id
    unknown_run = client.post("/api/actions/rerun", json={"run_id": 9}, headers=headers).json()
    assert unknown_run == {"ok": False, "action_id": unknown_run["action_id"], "detail": "run 9 unknown"}
    assert client.post("/api/actions/reconcile", headers=headers).json()["ok"] is True  # no body at all
    assert [a[1:] for a in actions(client)] == [
        ("reconcile", None, "ok"),
        ("rerun", "9", "error"),
        ("unskip", "sonarr/main", "error"),
        ("unskip", "sonarr/main", "ok"),
        ("skip", "sonarr/main", "ok"),
        ("packages", f"{REPO}@main", "ok"),
        ("dispatch", f"{REPO}@develop (rebuild)", "ok"),
        ("cancel", "5001", "ok"),
        ("rerun", "5001", "ok"),
    ]
    assert all(a[0] == "octocat" for a in actions(client))
    # The same rows show on the HTML page and in management.json.
    assert "re-run of failed jobs requested" in client.get("/management").text
    assert len(client.get("/api/management.json").json()["actions"]) == 9
    # Bad fields never reach GitHub and never make a row: the same checks as the forms.
    bad_headers = headers
    assert client.post("/api/actions/rerun", json={"run_id": "x"}, headers=bad_headers).status_code == 422
    assert client.post("/api/actions/rerun", json={}, headers=bad_headers).status_code == 400
    assert (
        client.post("/api/actions/dispatch", json={**dispatch, "repo": "docker-no"}, headers=headers).status_code == 404
    )
    assert client.post("/api/actions/dispatch", json={**dispatch, "reason": "yolo"}, headers=headers).status_code == 400
    assert client.post("/api/actions/skip", json={"scope": "a/b/c/d"}, headers=headers).status_code == 400
    # The reconcile's discovery reads the mods over GraphQL; nothing else reached GitHub.
    assert len([p for p in github.posts if p[0] != "/graphql"]) == 4 and len(actions(client)) == 9


def test_json_action_rate_limit_and_stale_member(client: TestClient, state: State, github: FakeActions) -> None:
    stale_at = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=2)).isoformat(timespec="seconds")
    user = sign_in(client, state, member=True, checked_at=stale_at)
    response = client.post("/api/actions/skip", json={"scope": "sonarr"}, headers={"X-CSRF-Token": user.csrf})
    assert response.status_code == 200 and "/orgs/example-org/memberships/octocat" in github.gets
    assert response.cookies.get(auth.SESSION_COOKIE)  # the refreshed verdict travels back
    for _ in range(30):
        state.model.add_action("octocat", "skip", "x", "ok")
    limited = client.post("/api/actions/unskip", json={"scope": "sonarr"}, headers={"X-CSRF-Token": user.csrf})
    assert limited.status_code == 429 and "actions in an hour" in limited.json()["detail"]
    assert [s.scope for s in state.model.skips()] == ["sonarr"]


# -- JSON pages ----------------------------------------------------------------------------------------------------


def test_actions_json_is_the_public_log(client: TestClient, state: State) -> None:
    assert client.get("/api/actions.json").json() == {"actions": []}
    state.model.add_action("octocat", "skip", "sonarr", "ok", "skip flag set on sonarr")
    state.model.add_action("hubot", "rerun", "5001", "error", "run 5001 unknown")
    assert actions(client) == [("hubot", "rerun", "5001", "error"), ("octocat", "skip", "sonarr", "ok")]


def test_site_pages_render_from_seeded_data(client: TestClient, state: State) -> None:
    seed(state, *FINISHED_SEED)
    state.model.add_decision("sonarr", "main", "dispatch", '{"upstream": "4.0.21", "released": "4.0.20"}')
    state.model.set_skip("sonarr/main", "hold", "octocat")
    for path, model in (
        ("/api/runs/5001.json", views.RunPage),
        ("/api/images/sonarr.json", views.ImagePage),
        (f"/api/builds/{REPO}/5001.json", views.BuildPage),
        ("/api/scheduler.json?kind=upstream&image=sonarr&outcome=dispatch", views.SchedulerPage),
        ("/api/skips.json", views.SkipsPage),
        ("/api/actions.json", views.ActionsPage),
    ):
        response = client.get(path, headers={"Origin": SITE})
        assert response.status_code == 200 and response.headers["access-control-allow-origin"] == SITE, path
        model.model_validate(response.json())
    scheduler = views.SchedulerPage.model_validate(
        client.get("/api/scheduler.json?kind=upstream&image=sonarr&outcome=dispatch").json()
    )
    assert [v.decision.outcome for v in scheduler.decisions] == ["dispatch"]
    assert [s.scope for s in views.SkipsPage.model_validate(client.get("/api/skips.json").json()).skips] == [
        "sonarr/main"
    ]


def test_stream_answers_cross_origin(client: TestClient) -> None:
    """EventSource(url, {withCredentials: true}) from the site: a CORS-approved, public stream."""
    with client.stream("GET", "/api/stream", headers={"Origin": SITE}) as response:
        assert response.status_code == 200 and response.headers["access-control-allow-origin"] == SITE
        assert response.headers["access-control-allow-credentials"] == "true"
        assert next(response.iter_lines()) == ": connected"


# -- sign-in round trip to the site ---------------------------------------------------------------------------------


@pytest.fixture
def https_client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        yield client


def test_login_returns_to_the_site(
    https_client: TestClient, credentials: AppCredentials, monkeypatch: pytest.MonkeyPatch
) -> None:
    state: State = https_client.app.state.control  # type: ignore[attr-defined]
    install(state, credentials)
    mock_github(state, monkeypatch, FakeOAuth(member=True))
    back = f"{SITE}/site/management/?tab=skips"
    start = https_client.get("/login", params={"next": back}, follow_redirects=False)
    assert start.status_code == 303
    pending = auth.unsign(state.session_secret, start.cookies.get(auth.OAUTH_COOKIE), auth.OAUTH_TTL)
    assert pending and pending["next"] == back
    state_token = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    done = https_client.get("/login/callback", params={"code": "c", "state": state_token}, follow_redirects=False)
    assert done.status_code == 303 and done.headers["location"] == back
    assert "#" not in done.headers["location"] and "token" not in done.headers["location"]
    session = next(h for h in done.headers.get_list("set-cookie") if h.startswith(auth.SESSION_COOKIE)).lower()
    assert "samesite=none" in session and "secure" in session and "httponly" in session
    me = auth.Me.model_validate(https_client.get("/api/me", headers={"Origin": SITE}).json())
    assert me.authenticated and me.login == "octocat" and me.member and me.csrf
    # Sign out from the site, then `next` elsewhere is never honoured.
    assert https_client.post("/api/logout", headers={"X-CSRF-Token": me.csrf}).status_code == 204
    elsewhere = https_client.get("/login", params={"next": f"{ELSEWHERE}/"}, follow_redirects=False)
    pending = auth.unsign(state.session_secret, elsewhere.cookies.get(auth.OAUTH_COOKIE), auth.OAUTH_TTL)
    assert pending and pending["next"] == "/"
    assert json.loads(https_client.get("/api/me").text)["authenticated"] is False
