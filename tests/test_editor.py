import base64
import datetime
import json
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from control import editor
from control.app import create_app
from control.config import Settings
from control.github_app import AppCredentials
from control.state import State

from .conftest import PUBLIC_URL, install, mock_github, sign_in, token_response

REPO = "example-org/site"
DOC = "src/content/docs/general/swag.md"
POST = "src/content/posts/webtop-7/index.md"
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()


class FakeSite:
    """Just enough of GitHub for the editor: OAuth, the site repo's branches, commits, files and pull requests."""

    def __init__(self, login: str = "octocat") -> None:
        self.login = login
        self.commits: dict[str, dict[str, str]] = {
            "c0": {DOC: "---\ntitle: SWAG\n---\nOld\n", "src/content/docs/general/a.png": "png"}
        }
        self.branches: dict[str, str] = {"main": "c0"}
        self.pulls: list[dict[str, Any]] = []
        self.exchanges: list[dict[str, list[str]]] = []
        self.revoked: list[str] = []
        self.commit_inputs: list[dict[str, Any]] = []
        self.push = True
        self.reject_token: str | None = None
        self.tokens = 0

    def _token(self) -> dict[str, Any]:
        self.tokens += 1
        return {
            "access_token": f"ghu_{self.tokens}",
            "refresh_token": f"ghr_{self.tokens}",
            "expires_in": 28800,
            "refresh_token_expires_in": 15897600,
        }

    def _files(self, ref: str) -> dict[str, str] | None:
        sha = self.branches.get(ref, ref)
        return self.commits.get(sha)

    def _pull(self, number: int) -> dict[str, Any]:
        return next(p for p in self.pulls if p["number"] == number)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if (token := token_response(request)) is not None:
            return token
        path, method = unquote(request.url.path), request.method
        params = dict(request.url.params)
        if request.url.host == "github.com" and path == "/login/oauth/access_token":
            fields = parse_qs(request.content.decode())
            self.exchanges.append(fields)
            if fields.get("grant_type") == ["refresh_token"] and fields["refresh_token"] == ["ghr_dead"]:
                return httpx.Response(200, json={"error": "bad_refresh_token"})
            return httpx.Response(200, json=self._token())
        if path.startswith("/applications/") and path.endswith("/token") and method == "DELETE":
            self.revoked.append(json.loads(request.content)["access_token"])
            return httpx.Response(204)
        auth_header = request.headers.get("Authorization", "")
        if self.reject_token and auth_header == f"Bearer {self.reject_token}":
            return httpx.Response(401, json={"message": "Bad credentials"})
        assert auth_header.startswith("Bearer ghu_"), f"{path} must use the member's token, got {auth_header}"
        if path == "/user":
            return httpx.Response(200, json={"login": self.login})
        if path == f"/repos/{REPO}":
            return httpx.Response(200, json={"default_branch": "main", "permissions": {"push": self.push}})
        if path.startswith(f"/repos/{REPO}/git/ref/heads/"):
            name = path.removeprefix(f"/repos/{REPO}/git/ref/heads/")
            if name not in self.branches:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"object": {"sha": self.branches[name]}})
        if path == f"/repos/{REPO}/git/refs" and method == "POST":
            body = json.loads(request.content)
            if body["sha"] not in self.commits:
                return httpx.Response(422, json={"message": "Object does not exist"})
            self.branches[body["ref"].removeprefix("refs/heads/")] = body["sha"]
            return httpx.Response(201, json={})
        if path.startswith(f"/repos/{REPO}/git/refs/heads/") and method == "DELETE":
            del self.branches[path.removeprefix(f"/repos/{REPO}/git/refs/heads/")]
            return httpx.Response(204)
        if path.startswith(f"/repos/{REPO}/contents/"):
            files = self._files(params["ref"]) or {}
            target = path.removeprefix(f"/repos/{REPO}/contents/")
            if target in files:
                content = base64.b64encode(files[target].encode()).decode()
                return httpx.Response(200, json={"type": "file", "encoding": "base64", "content": content})
            inside = [f for f in files if f.startswith(f"{target}/") and "/" not in f[len(target) + 1 :]]
            if inside:
                return httpx.Response(200, json=[{"type": "file", "name": f.rsplit("/", 1)[1]} for f in inside])
            return httpx.Response(404, json={"message": "Not Found"})
        if path == f"/repos/{REPO}/pulls" and method == "GET":
            found = [p for p in self.pulls if p["state"] == params.get("state", "open")]
            if "head" in params:
                found = [p for p in found if f"example-org:{p['head']['ref']}" == params["head"]]
            return httpx.Response(200, json=found)
        if path == f"/repos/{REPO}/pulls" and method == "POST":
            body = json.loads(request.content)
            number = len(self.pulls) + 1
            pull = {
                "number": number,
                "title": body["title"],
                "body": body["body"],
                "html_url": f"https://github.com/{REPO}/pull/{number}",
                "state": "open",
                "draft": False,
                "merged_at": None,
                "updated_at": "2026-10-26T12:00:00Z",
                "head": {"ref": body["head"], "sha": self.branches[body["head"]], "repo": {"full_name": REPO}},
            }
            self.pulls.append(pull)
            return httpx.Response(201, json=pull)
        if path.startswith(f"/repos/{REPO}/pulls/") and method == "PATCH":
            self._pull(int(path.rsplit("/", 1)[1])).update(json.loads(request.content))
            return httpx.Response(200, json={})
        if path.startswith(f"/repos/{REPO}/commits/") and path.endswith("/check-runs"):
            return httpx.Response(200, json={"check_runs": [{"status": "completed", "conclusion": "success"}]})
        if path == "/graphql":
            variables = json.loads(request.content)["variables"]["input"]
            self.commit_inputs.append(variables)
            branch = variables["branch"]["branchName"]
            if self.branches.get(branch) != variables["expectedHeadOid"]:
                return httpx.Response(200, json={"errors": [{"message": "Expected branch to point to x"}]})
            files = dict(self.commits[self.branches[branch]])
            for added in variables["fileChanges"]["additions"]:
                files[added["path"]] = base64.b64decode(added["contents"]).decode(errors="replace")
            sha = f"c{len(self.commits)}"
            self.commits[sha] = files
            self.branches[branch] = sha
            url = f"https://github.com/{REPO}/commit/{sha}"
            return httpx.Response(200, json={"data": {"createCommitOnBranch": {"commit": {"oid": sha, "url": url}}}})
        return httpx.Response(404, json={"message": f"no fixture for {method} {path}"})


@pytest.fixture
def https_client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        yield client


@pytest.fixture
def https_state(https_client: TestClient) -> State:
    control: State = https_client.app.state.control  # type: ignore[attr-defined]
    return control


@pytest.fixture
def site(https_state: State, credentials: AppCredentials, monkeypatch: pytest.MonkeyPatch) -> FakeSite:
    install(https_state, credentials)
    fake = FakeSite()
    mock_github(https_state, monkeypatch, fake)
    monkeypatch.setattr(https_state, "settings", https_state.settings.model_copy(update={"site_repo": "site"}))
    return fake


def authorise(state: State, login: str = "octocat", expires_in: int = 28800, refresh: str = "ghr_0") -> None:
    now = datetime.datetime.now(datetime.UTC)
    body = {"access_token": "ghu_0", "refresh_token": refresh, "expires_in": expires_in}
    editor.store(state, login, editor._grant_of(body, now))


def test_paths_are_limited_to_the_two_content_trees() -> None:
    assert editor.entry_of(DOC).key == "docs/general/swag"
    assert editor.entry_of(POST).key == "posts/webtop-7"
    assert editor.entry_of(POST).branch("OctoCat") == "edit/octocat/posts/webtop-7"
    assert editor.entry_of_branch("edit/octocat/docs/general/swag", "octocat") == editor.entry_of(DOC)
    assert editor.entry_of_branch("edit/someone/docs/general/swag", "octocat") is None
    for bad in (
        "src/content/docs/../../.github/workflows/deploy.yml",
        "src/content/docs/general/nested/page.md",
        "src/content/posts/x/other.md",
        ".github/workflows/deploy.yml",
        "src/content/docs/general/Swag.md",
    ):
        with pytest.raises(Exception, match="path must be"):
            editor.entry_of(bad)


def test_authorize_needs_a_member_and_uses_the_app_client(
    https_client: TestClient, https_state: State, site: FakeSite, credentials: AppCredentials
) -> None:
    out = https_client.get("/edit/authorize", params={"next": "/x"}, follow_redirects=False)
    assert out.status_code == 303 and out.headers["location"].startswith("/login?")
    sign_in(https_client, https_state, member=False)
    assert https_client.get("/edit/authorize", follow_redirects=False).status_code == 403

    sign_in(https_client, https_state)
    nxt = "https://linuxserver-playground.github.io/site/edit/?doc=general/swag"
    start = https_client.get("/edit/authorize", params={"next": nxt}, follow_redirects=False)
    query = parse_qs(urlparse(start.headers["location"]).query)
    assert query["client_id"] == [credentials.client_id] and "scope" not in query
    assert query["redirect_uri"] == [f"{PUBLIC_URL}/login/callback"]

    done = https_client.get("/login/callback", params={"code": "c", "state": query["state"][0]}, follow_redirects=False)
    assert done.status_code == 303 and done.headers["location"] == nxt
    assert site.exchanges[0]["client_id"] == [credentials.client_id]
    grant = editor.load(https_state, "octocat")
    assert grant and grant.token == "ghu_1" and grant.refresh_token == "ghr_1" and grant.expires_at
    with https_state.db.connect() as conn:
        row = dict(conn.execute("SELECT * FROM editor_tokens").fetchone())
    assert "ghu_1" not in json.dumps(row)  # stored encrypted
    assert https_state.model.recent_actions()[0].action == "edit-authorize"
    # The sign-in session is untouched by the editor's authorisation.
    assert https_client.get("/api/me").json()["login"] == "octocat"


def test_authorize_refuses_a_token_for_someone_else(
    https_client: TestClient, https_state: State, site: FakeSite
) -> None:
    sign_in(https_client, https_state)
    start = https_client.get("/edit/authorize", follow_redirects=False)
    state_token = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    site.login = "mallory"
    done = https_client.get("/login/callback", params={"code": "c", "state": state_token}, follow_redirects=False)
    assert done.status_code == 400 and editor.load(https_state, "octocat") is None


def test_status(https_client: TestClient, https_state: State, site: FakeSite) -> None:
    assert https_client.get("/api/editor/status").json() == {
        "authenticated": False,
        "member": False,
        "login": None,
        "authorized": False,
        "can_push": False,
        "repo": REPO,
        "base": None,
        "edits": [],
    }
    sign_in(https_client, https_state)
    body = https_client.get("/api/editor/status").json()
    assert body["member"] and not body["authorized"]
    authorise(https_state)
    site.branches["edit/octocat/docs/general/swag"] = "c0"
    site.branches["feature"] = "c0"
    for number, ref in ((1, "edit/octocat/docs/general/swag"), (2, "feature")):
        site.pulls.append(
            {
                "number": number,
                "title": "t",
                "html_url": f"https://github.com/{REPO}/pull/{number}",
                "state": "open",
                "head": {"ref": ref, "sha": "c0", "repo": {"full_name": REPO}},
            }
        )
    body = https_client.get("/api/editor/status").json()
    assert body["authorized"] and body["can_push"] and body["base"] == "main"
    assert [(e["number"], e["path"], e["checks"]) for e in body["edits"]] == [(1, DOC, "success")]


def test_entry_reads_main_then_the_edit_branch(https_client: TestClient, https_state: State, site: FakeSite) -> None:
    sign_in(https_client, https_state)
    assert https_client.get("/api/editor/entry", params={"path": DOC}).status_code == 428
    authorise(https_state)
    assert https_client.get("/api/editor/entry", params={"path": "README.md"}).status_code == 400
    page = https_client.get("/api/editor/entry", params={"path": DOC}).json()
    assert page["exists"] and page["published"] and not page["on_branch"]
    assert page["content"].endswith("Old\n") and page["head"] == "c0" and page["images"] == ["a.png"]
    assert page["raw_base"] == f"https://raw.githubusercontent.com/{REPO}/c0/src/content/docs/general/"

    new = https_client.get("/api/editor/entry", params={"path": POST}).json()
    assert not new["exists"] and not new["published"] and new["content"] == "" and new["images"] == []


def test_save_publish_and_discard(https_client: TestClient, https_state: State, site: FakeSite) -> None:
    user = sign_in(https_client, https_state)
    authorise(https_state)
    headers = {"X-CSRF-Token": user.csrf}
    body = {"path": POST, "content": "---\ntitle: Webtop 7\n---\r\nHello", "head": "c0"}
    assert https_client.post("/api/editor/save", json=body).status_code == 403  # no CSRF token

    saved = https_client.post(
        "/api/editor/save", json={**body, "assets": [{"name": "hero.png", "data": PNG}]}, headers=headers
    ).json()
    assert saved["ok"] and saved["branch"] == "edit/octocat/posts/webtop-7" and saved["head"] == "c1"
    commit = site.commit_inputs[0]
    assert commit["message"]["headline"] == "Add the post webtop-7"
    files = site.commits["c1"]
    assert files[POST] == "---\ntitle: Webtop 7\n---\nHello\n"  # LF and one final newline
    assert "src/content/posts/webtop-7/hero.png" in files and site.branches["main"] == "c0"

    # Saving from a stale head is a conflict, not an overwrite.
    stale = https_client.post("/api/editor/save", json=body, headers=headers)
    assert stale.status_code == 409 and "moved since" in stale.json()["detail"]
    again = https_client.post(
        "/api/editor/save", json={**body, "head": "c1", "message": "Fix a typo"}, headers=headers
    ).json()
    assert again["ok"] and site.commit_inputs[-1]["message"]["headline"] == "Fix a typo"

    page = https_client.get("/api/editor/entry", params={"path": POST}).json()
    assert page["on_branch"] and page["exists"] and not page["published"] and page["head"] == "c2"
    assert page["images"] == ["hero.png"] and page["pull"] is None

    opened = https_client.post("/api/editor/publish", json={"path": POST, "title": "Webtop 7"}, headers=headers).json()
    assert opened["ok"] and opened["pull"]["number"] == 1 and opened["pull"]["path"] == POST
    assert site.pulls[0]["title"] == 'Add the post "Webtop 7"'
    twice = https_client.post("/api/editor/publish", json={"path": POST}, headers=headers).json()
    assert twice["ok"] and "already open" in twice["detail"] and len(site.pulls) == 1

    gone = https_client.post("/api/editor/discard", json={"path": POST}, headers=headers).json()
    assert gone["ok"] and "closed #1" in gone["detail"]
    assert site.pulls[0]["state"] == "closed" and "edit/octocat/posts/webtop-7" not in site.branches
    actions = [(a.action, a.result) for a in https_state.model.recent_actions()]
    assert actions[:5] == [
        ("edit-discard", "ok"),
        ("edit-publish", "ok"),
        ("edit-publish", "ok"),
        ("edit-save", "ok"),
        ("edit-save", "error"),
    ]


def test_save_refuses_what_it_must_not_write(https_client: TestClient, https_state: State, site: FakeSite) -> None:
    user = sign_in(https_client, https_state)
    authorise(https_state)
    headers = {"X-CSRF-Token": user.csrf}
    base = {"path": DOC, "content": "x", "head": "c0"}
    for asset, status in (
        ({"name": "evil.svg", "data": PNG}, 400),
        ({"name": "../x.png", "data": PNG}, 400),
        ({"name": "ok.png", "data": "not base64!"}, 400),
        ({"name": "big.png", "data": base64.b64encode(b"0" * (editor.ASSET_MAX + 1)).decode()}, 413),
    ):
        answer = https_client.post("/api/editor/save", json={**base, "assets": [asset]}, headers=headers)
        assert answer.status_code == status, asset["name"]
    huge = https_client.post("/api/editor/save", json={**base, "content": "x" * editor.MARKDOWN_MAX}, headers=headers)
    assert huge.status_code == 413
    gone = https_client.post("/api/editor/save", json={**base, "head": "nope"}, headers=headers)
    assert gone.status_code == 409 and "reload" in gone.json()["detail"]
    assert site.commit_inputs == []


def test_publish_before_saving(https_client: TestClient, https_state: State, site: FakeSite) -> None:
    user = sign_in(https_client, https_state)
    authorise(https_state)
    answer = https_client.post("/api/editor/publish", json={"path": DOC}, headers={"X-CSRF-Token": user.csrf}).json()
    assert not answer["ok"] and "save first" in answer["detail"]


def test_a_merged_branch_is_cleared_when_the_entry_opens(
    https_client: TestClient, https_state: State, site: FakeSite
) -> None:
    sign_in(https_client, https_state)
    authorise(https_state)
    site.commits["c9"] = {DOC: "branch\n"}
    site.branches["edit/octocat/docs/general/swag"] = "c9"
    site.pulls.append(
        {
            "number": 1,
            "state": "closed",
            "merged_at": "2026-10-26T00:00:00Z",
            "head": {"ref": "edit/octocat/docs/general/swag", "sha": "c9"},
        }
    )
    page = https_client.get("/api/editor/entry", params={"path": DOC}).json()
    assert not page["on_branch"] and page["head"] == "c0" and "edit/octocat/docs/general/swag" not in site.branches


def test_expiring_tokens_are_refreshed_or_dropped(https_client: TestClient, https_state: State, site: FakeSite) -> None:
    sign_in(https_client, https_state)
    authorise(https_state, expires_in=60)
    assert https_client.get("/api/editor/entry", params={"path": DOC}).status_code == 200
    assert site.exchanges[-1]["grant_type"] == ["refresh_token"] and site.exchanges[-1]["refresh_token"] == ["ghr_0"]
    grant = editor.load(https_state, "octocat")
    assert grant and grant.token == "ghu_1" and not grant.expiring(datetime.datetime.now(datetime.UTC))

    authorise(https_state, expires_in=60, refresh="ghr_dead")
    assert https_client.get("/api/editor/entry", params={"path": DOC}).status_code == 428
    assert editor.load(https_state, "octocat") is None


def test_a_token_github_no_longer_accepts_is_dropped(
    https_client: TestClient, https_state: State, site: FakeSite
) -> None:
    sign_in(https_client, https_state)
    authorise(https_state)
    site.reject_token = "ghu_0"
    assert https_client.get("/api/editor/entry", params={"path": DOC}).status_code == 428
    assert editor.load(https_state, "octocat") is None
    authorise(https_state)
    assert not https_client.get("/api/editor/status").json()["authorized"]


def test_another_session_secret_cannot_read_the_token(https_state: State, monkeypatch: pytest.MonkeyPatch) -> None:
    authorise(https_state)
    monkeypatch.setattr(https_state, "session_secret", "another")
    assert editor.load(https_state, "octocat") is None
    with https_state.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM editor_tokens").fetchone()[0] == 0


def test_sign_out_revokes_the_editor_token(https_client: TestClient, https_state: State, site: FakeSite) -> None:
    user = sign_in(https_client, https_state)
    authorise(https_state)
    assert https_client.post("/api/logout", headers={"X-CSRF-Token": user.csrf}).status_code == 204
    assert site.revoked == ["ghu_0"] and editor.load(https_state, "octocat") is None

    user = sign_in(https_client, https_state)
    authorise(https_state)
    assert https_client.post("/api/editor/revoke", headers={"X-CSRF-Token": "bad"}).status_code == 403
    assert https_client.post("/api/editor/revoke", headers={"X-CSRF-Token": user.csrf}).status_code == 204
    assert site.revoked == ["ghu_0", "ghu_0"] and https_client.get("/api/me").json()["authenticated"]


def test_edits_have_their_own_rate_limit(https_state: State) -> None:
    for _ in range(3):
        https_state.model.add_action("octocat", "edit-save", "posts/x", "ok")
    https_state.model.add_action("octocat", "dispatch", "repo@main", "ok")
    assert https_state.model.count_actions_since("octocat", "2000-01-01") == 1
    assert https_state.model.count_actions_since("octocat", "2000-01-01", editing=True) == 3
