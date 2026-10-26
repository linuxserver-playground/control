import base64
import datetime
import hashlib
import json
import re
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
MP4 = base64.b64encode(b"\x00\x00\x00\x18ftypmp42").decode()


class FakeSite:
    """Just enough of GitHub for the editor: OAuth, the site repo's branches, commits, trees, blobs and pull
    requests (REST as the member), and the open-edits GraphQL and permission check (as the installation)."""

    def __init__(self, login: str = "octocat") -> None:
        self.login = login
        self.blobs: dict[str, str] = {}
        self.commits: dict[str, tuple[dict[str, str], str | None]] = {
            "c0": (
                {
                    DOC: "---\ntitle: SWAG\n---\nOld ![a](./a.png) ![s](./shared.png)\n",
                    "src/content/docs/general/a.png": "png",
                    "src/content/docs/general/shared.png": "png",
                    "src/content/docs/general/other.md": "---\ntitle: Other\n---\n![s](./shared.png)\n",
                    "src/content/posts/old/index.md": "---\ntitle: Old post\n---\nHi\n",
                    "src/content/posts/old/hero.jpg": "jpg",
                    "public/media/old/clip.mp4": "mp4",
                    "public/media/other/keep.mp4": "mp4",
                },
                None,
            )
        }
        self.branches: dict[str, str] = {"main": "c0"}
        self.pulls: list[dict[str, Any]] = []
        self.exchanges: list[dict[str, list[str]]] = []
        self.revoked: list[str] = []
        self.messages: list[str] = []
        self.permission = "write"
        self.reject_token: str | None = None
        self.tokens = 0
        self.graphql_calls = 0

    def _token(self) -> dict[str, Any]:
        self.tokens += 1
        return {
            "access_token": f"ghu_{self.tokens}",
            "refresh_token": f"ghr_{self.tokens}",
            "expires_in": 28800,
            "refresh_token_expires_in": 15897600,
        }

    def files(self, ref: str) -> dict[str, str]:
        return self.commits[self.branches.get(ref, ref)][0]

    def _pull(self, number: int) -> dict[str, Any]:
        return next(p for p in self.pulls if p["number"] == number)

    def add_pull(self, branch: str, title: str = "t", state: str = "open", merged: bool = False) -> dict[str, Any]:
        number = len(self.pulls) + 1
        pull = {
            "number": number,
            "title": title,
            "html_url": f"https://github.com/{REPO}/pull/{number}",
            "state": state,
            "draft": False,
            "merged_at": "2026-10-26T00:00:00Z" if merged else None,
            "updated_at": "2026-10-26T12:00:00Z",
            "head": {"ref": branch, "sha": self.branches[branch], "repo": {"full_name": REPO}},
        }
        self.pulls.append(pull)
        return pull

    def _graphql(self, query: str) -> dict[str, Any]:
        self.graphql_calls += 1
        if "refs(" in query:
            nodes: list[dict[str, Any]] = []
            for name, sha in self.branches.items():
                if not name.startswith("edit/"):
                    continue
                open_pulls = [p for p in self.pulls if p["head"]["ref"] == name and p["state"] == "open"]
                prs = [
                    {
                        "number": p["number"],
                        "title": p["title"],
                        "url": p["html_url"],
                        "isDraft": False,
                        "updatedAt": p["updated_at"],
                        "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]},
                    }
                    for p in open_pulls
                ]
                nodes.append(
                    {
                        "name": name.removeprefix("edit/"),
                        "target": {"oid": sha, "committedDate": f"2026-10-26T12:00:0{len(nodes)}Z"},
                        "associatedPullRequests": {"nodes": prs},
                    }
                )
            return {"repository": {"defaultBranchRef": {"name": "main"}, "refs": {"nodes": nodes}}}
        answer: dict[str, Any] = {}
        for alias, ref, path in re.findall(r'(\w+): object\(expression: "([^:"]+):([^"]+)"\)', query):
            text = self.files(ref).get(path)
            answer[alias] = None if text is None else ({"text": text} if alias.startswith("b") else {"oid": "x"})
        return {"repository": answer}

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
        if path == "/graphql" or path.endswith("/permission"):
            assert auth_header == "Bearer ghs_token", f"{path} reads with the installation token"
            if path == "/graphql":
                return httpx.Response(200, json={"data": self._graphql(json.loads(request.content)["query"])})
            return httpx.Response(200, json={"permission": self.permission})
        if self.reject_token and auth_header == f"Bearer {self.reject_token}":
            return httpx.Response(401, json={"message": "Bad credentials"})
        assert auth_header.startswith("Bearer ghu_"), f"{path} must use the member's token, got {auth_header}"
        body = json.loads(request.content) if request.content else {}
        if path == "/user":
            return httpx.Response(200, json={"login": self.login})
        if path == f"/repos/{REPO}":
            return httpx.Response(200, json={"default_branch": "main"})
        if path.startswith(f"/repos/{REPO}/git/ref/heads/"):
            name = path.removeprefix(f"/repos/{REPO}/git/ref/heads/")
            if name not in self.branches:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"object": {"sha": self.branches[name]}})
        if path == f"/repos/{REPO}/git/refs" and method == "POST":
            if body["sha"] not in self.commits:
                return httpx.Response(422, json={"message": "Object does not exist"})
            self.branches[body["ref"].removeprefix("refs/heads/")] = body["sha"]
            return httpx.Response(201, json={})
        if path.startswith(f"/repos/{REPO}/git/refs/heads/"):
            name = path.removeprefix(f"/repos/{REPO}/git/refs/heads/")
            if method == "DELETE":
                del self.branches[name]
                return httpx.Response(204)
            parent = self.commits[body["sha"]][1]
            if self.branches.get(name) != parent and not body.get("force"):
                return httpx.Response(422, json={"message": "Update is not a fast forward"})
            self.branches[name] = body["sha"]
            return httpx.Response(200, json={})
        if path == f"/repos/{REPO}/git/blobs":
            raw = base64.b64decode(body["content"]) if body["encoding"] == "base64" else body["content"].encode()
            sha = hashlib.sha1(raw).hexdigest()
            self.blobs[sha] = raw.decode(errors="replace")
            return httpx.Response(201, json={"sha": sha})
        if path.startswith(f"/repos/{REPO}/git/commits/"):
            return httpx.Response(200, json={"tree": {"sha": f"t-{path.rsplit('/', 1)[1]}"}})
        if path.startswith(f"/repos/{REPO}/git/trees/") and method == "GET":
            files = self.commits[path.rsplit("/", 1)[1].removeprefix("t-")][0]
            return httpx.Response(200, json={"tree": [{"path": f, "type": "blob"} for f in files]})
        if path == f"/repos/{REPO}/git/trees":
            files = dict(self.commits[body["base_tree"].removeprefix("t-")][0])
            for entry in body["tree"]:
                if entry["sha"] is None:
                    files.pop(entry["path"], None)
                else:
                    files[entry["path"]] = self.blobs[entry["sha"]]
            sha = f"c{len(self.commits)}"
            self.commits[sha] = (files, None)
            return httpx.Response(201, json={"sha": f"t-{sha}"})
        if path == f"/repos/{REPO}/git/commits":
            sha = body["tree"].removeprefix("t-")
            self.commits[sha] = (self.commits[sha][0], body["parents"][0])
            self.messages.append(body["message"])
            return httpx.Response(201, json={"sha": sha, "html_url": f"https://github.com/{REPO}/commit/{sha}"})
        if path.startswith(f"/repos/{REPO}/contents/"):
            files = self.files(params["ref"])
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
            pull = self.add_pull(body["head"], body["title"])
            pull["body"] = body["body"]
            return httpx.Response(201, json=pull)
        if path.startswith(f"/repos/{REPO}/pulls/") and method == "PATCH":
            self._pull(int(path.rsplit("/", 1)[1])).update(body)
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"message": f"no fixture for {method} {path}"})


@pytest.fixture(autouse=True)
def fresh_caches() -> Iterator[None]:
    editor._pending.clear()
    editor._editors.clear()
    yield


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
    return fake


def authorise(state: State, login: str = "octocat", expires_in: int = 28800, refresh: str = "ghr_0") -> None:
    now = datetime.datetime.now(datetime.UTC)
    body = {"access_token": "ghu_0", "refresh_token": refresh, "expires_in": expires_in}
    editor.store(state, login, editor._grant_of(body, now))


def upload(client: TestClient, csrf: str, path: str, name: str, data: str) -> dict[str, Any]:
    answer = client.post(
        "/api/editor/upload", json={"path": path, "name": name, "data": data}, headers={"X-CSRF-Token": csrf}
    )
    assert answer.status_code == 200, answer.text
    body: dict[str, Any] = answer.json()
    return body


def test_paths_and_branches() -> None:
    assert editor.entry_of(DOC).key == "docs/general/swag"
    assert editor.entry_of(DOC).media == "public/media/docs/general/swag"
    assert editor.entry_of(POST).media == "public/media/webtop-7"
    assert editor.entry_of(POST).branch("OctoCat") == "edit/octocat/posts/webtop-7"
    assert editor.entry_of_branch("edit/alice/docs/general/swag") == ("alice", editor.entry_of(DOC))
    assert editor.entry_of_branch("feature/x") is None
    alice = "edit/alice/docs/general/swag"
    assert editor.branch_for(editor.entry_of(DOC), "bob", alice) == alice
    for bad in ("main", "edit/alice/posts/webtop-7", "edit/alice/docs/general/swag/x"):
        with pytest.raises(Exception, match="not an edit branch"):
            editor.branch_for(editor.entry_of(DOC), "bob", bad)
    for bad in (
        "src/content/docs/../../.github/workflows/deploy.yml",
        "src/content/docs/general/nested/page.md",
        "src/content/posts/x/other.md",
        ".github/workflows/deploy.yml",
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
    grant = editor.load(https_state, "octocat")
    assert grant and grant.token == "ghu_1" and grant.refresh_token == "ghr_1" and grant.expires_at
    with https_state.db.connect() as conn:
        row = dict(conn.execute("SELECT * FROM editor_tokens").fetchone())
    assert "ghu_1" not in json.dumps(row)  # stored encrypted
    assert https_state.model.recent_actions()[0].action == "edit-authorize"
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


def test_status_says_who_may_edit_before_authorising(
    https_client: TestClient, https_state: State, site: FakeSite
) -> None:
    anonymous = https_client.get("/api/editor/status").json()
    assert anonymous["authenticated"] is False and anonymous["can_edit"] is False
    sign_in(https_client, https_state)
    body = https_client.get("/api/editor/status").json()
    assert body["member"] and body["can_edit"] and not body["authorized"]
    authorise(https_state)
    assert https_client.get("/api/editor/status").json()["authorized"]
    editor._editors.clear()
    site.permission = "read"
    assert not https_client.get("/api/editor/status").json()["can_edit"]
    sign_in(https_client, https_state, login="guest", member=False)
    assert not https_client.get("/api/editor/status").json()["can_edit"]


def test_pending_lists_every_open_edit_by_anyone(https_client: TestClient, https_state: State, site: FakeSite) -> None:
    sign_in(https_client, https_state)
    assert https_client.get("/api/editor/pending").status_code == 200  # no user token needed
    main = site.files("c0")
    site.commits["c1"] = ({**main, DOC: "---\ntitle: SWAG v2\n---\n"}, "c0")
    site.commits["c2"] = ({**main, POST: "---\ntitle: Webtop 7\n---\n"}, "c0")
    site.commits["c3"] = ({k: v for k, v in main.items() if not k.startswith("src/content/posts/old/")}, "c0")
    site.branches |= {
        "edit/alice/docs/general/swag": "c1",
        "edit/bob/posts/webtop-7": "c2",
        "edit/carol/posts/old": "c3",
        "edit/dave/posts/ghost": "c0",
        "feature": "c0",
    }
    site.add_pull("edit/alice/docs/general/swag", "Update the docs page")
    editor._pending.clear()
    edits = {e["branch"]: e for e in https_client.get("/api/editor/pending").json()["edits"]}
    assert set(edits) == {"edit/alice/docs/general/swag", "edit/bob/posts/webtop-7", "edit/carol/posts/old"}
    alice, bob = edits["edit/alice/docs/general/swag"], edits["edit/bob/posts/webtop-7"]
    assert alice["change"] == "edit" and alice["title"] == "SWAG v2" and alice["pull"]["checks"] == "success"
    assert bob["change"] == "new" and bob["owner"] == "bob" and bob["pull"] is None
    assert edits["edit/carol/posts/old"]["change"] == "delete"
    calls = site.graphql_calls
    https_client.get("/api/editor/pending")
    assert site.graphql_calls == calls  # cached


def test_entry_reads_main_then_an_edit_branch(https_client: TestClient, https_state: State, site: FakeSite) -> None:
    sign_in(https_client, https_state)
    assert https_client.get("/api/editor/entry", params={"path": DOC}).status_code == 428
    authorise(https_state)
    assert https_client.get("/api/editor/entry", params={"path": "README.md"}).status_code == 400
    page = https_client.get("/api/editor/entry", params={"path": DOC}).json()
    assert page["exists"] and page["published"] and not page["on_branch"] and page["owner"] == "octocat"
    assert page["head"] == "c0" and page["images"] == ["a.png", "shared.png"] and page["others"] == []
    assert page["media_url"] == "/media/docs/general/swag/"
    assert page["raw_root"] == f"https://raw.githubusercontent.com/{REPO}/c0/"

    old = https_client.get("/api/editor/entry", params={"path": "src/content/posts/old/index.md"}).json()
    assert old["media"] == ["clip.mp4"] and old["images"] == ["hero.jpg"]

    site.commits["c1"] = ({**site.files("c0"), DOC: "alice's\n"}, "c0")
    site.branches["edit/alice/docs/general/swag"] = "c1"
    editor._pending.clear()
    mine = https_client.get("/api/editor/entry", params={"path": DOC}).json()
    assert mine["others"] == ["edit/alice/docs/general/swag"] and not mine["on_branch"]
    joined = https_client.get(
        "/api/editor/entry", params={"path": DOC, "branch": "edit/alice/docs/general/swag"}
    ).json()
    assert joined["on_branch"] and joined["content"] == "alice's\n" and joined["owner"] == "alice"
    bad = https_client.get("/api/editor/entry", params={"path": DOC, "branch": "edit/alice/posts/x"})
    assert bad.status_code == 400


def test_upload_save_publish_and_discard(https_client: TestClient, https_state: State, site: FakeSite) -> None:
    user = sign_in(https_client, https_state)
    authorise(https_state)
    headers = {"X-CSRF-Token": user.csrf}
    image = upload(https_client, user.csrf, POST, "hero.png", PNG)
    assert image["kind"] == "image" and image["target"] == "src/content/posts/webtop-7/hero.png"
    assert image["url"] == "./hero.png"
    video = upload(https_client, user.csrf, POST, "demo.mp4", MP4)
    assert video["target"] == "public/media/webtop-7/demo.mp4" and video["url"] == "/media/webtop-7/demo.mp4"

    body = {"path": POST, "content": "---\ntitle: Webtop 7\n---\r\nHello", "head": "c0"}
    assert https_client.post("/api/editor/save", json=body).status_code == 403  # no CSRF token
    files = [{"name": "hero.png", "sha": image["sha"]}, {"name": "demo.mp4", "sha": video["sha"]}]
    saved = https_client.post("/api/editor/save", json={**body, "files": files}, headers=headers).json()
    assert saved["ok"] and saved["branch"] == "edit/octocat/posts/webtop-7"
    head = saved["head"]
    assert site.messages[-1] == "Add the post webtop-7"
    tree = site.files(head)
    assert tree[POST] == "---\ntitle: Webtop 7\n---\nHello\n"  # LF and one final newline
    assert "src/content/posts/webtop-7/hero.png" in tree and "public/media/webtop-7/demo.mp4" in tree
    assert site.branches["main"] == "c0"

    # Saving from a stale head is a conflict, not an overwrite.
    stale = https_client.post("/api/editor/save", json=body, headers=headers)
    assert stale.status_code == 409 and "moved since" in stale.json()["detail"]
    again = https_client.post(
        "/api/editor/save", json={**body, "head": head, "message": "Fix a typo"}, headers=headers
    ).json()
    assert again["ok"] and site.messages[-1] == "Fix a typo"

    opened = https_client.post("/api/editor/publish", json={"path": POST, "title": "Webtop 7"}, headers=headers).json()
    assert opened["ok"] and opened["pull"]["number"] == 1 and site.pulls[0]["title"] == 'Add the post "Webtop 7"'
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


def test_joining_someone_elses_edit(https_client: TestClient, https_state: State, site: FakeSite) -> None:
    alice = "edit/alice/docs/general/swag"
    site.commits["c1"] = ({**site.files("c0"), DOC: "alice's\n"}, "c0")
    site.branches[alice] = "c1"
    site.add_pull(alice, "Update the docs page")
    user = sign_in(https_client, https_state, login="bob")
    authorise(https_state, login="bob")
    headers = {"X-CSRF-Token": user.csrf}
    saved = https_client.post(
        "/api/editor/save", json={"path": DOC, "content": "bob's", "head": "c1", "branch": alice}, headers=headers
    ).json()
    assert saved["ok"] and saved["branch"] == alice and site.files(alice)[DOC] == "bob's\n"
    assert "edit/bob/docs/general/swag" not in site.branches
    pr = https_client.post("/api/editor/publish", json={"path": DOC, "branch": alice}, headers=headers).json()
    assert pr["ok"] and pr["pull"]["number"] == 1 and len(site.pulls) == 1  # one change, one pull request


def test_save_refuses_what_it_must_not_write(https_client: TestClient, https_state: State, site: FakeSite) -> None:
    user = sign_in(https_client, https_state)
    authorise(https_state)
    headers = {"X-CSRF-Token": user.csrf}
    for name, data, status in (
        ("evil.svg", PNG, 400),
        ("../x.png", PNG, 400),
        ("clip.mov", MP4, 400),
        ("ok.png", "not base64!", 400),
        ("big.png", base64.b64encode(b"0" * (editor.IMAGE_MAX + 1)).decode(), 413),
    ):
        answer = https_client.post(
            "/api/editor/upload", json={"path": DOC, "name": name, "data": data}, headers=headers
        )
        assert answer.status_code == status, name
    base = {"path": DOC, "content": "x", "head": "c0"}
    huge = https_client.post("/api/editor/save", json={**base, "content": "x" * editor.MARKDOWN_MAX}, headers=headers)
    assert huge.status_code == 413
    forged = https_client.post(
        "/api/editor/save", json={**base, "files": [{"name": "a.png", "sha": "nope"}]}, headers=headers
    )
    assert forged.status_code == 400
    gone = https_client.post("/api/editor/save", json={**base, "head": "nope"}, headers=headers)
    assert gone.status_code == 409 and "reload" in gone.json()["detail"]
    assert site.messages == []


def test_deleting_a_post_removes_its_folder_and_media(
    https_client: TestClient, https_state: State, site: FakeSite
) -> None:
    user = sign_in(https_client, https_state)
    authorise(https_state)
    old = "src/content/posts/old/index.md"
    plan = https_client.get("/api/editor/removal", params={"path": old}).json()
    assert plan["title"] == "Old post"
    assert plan["files"] == ["public/media/old/clip.mp4", "src/content/posts/old/hero.jpg", old]
    done = https_client.post(
        "/api/editor/delete", json={"path": old, "title": "Old post"}, headers={"X-CSRF-Token": user.csrf}
    ).json()
    assert done["ok"] and done["pull"]["number"] == 1
    branch = "edit/octocat/posts/old"
    tree = site.files(branch)
    assert not any(f.startswith(("src/content/posts/old/", "public/media/old/")) for f in tree)
    assert "public/media/other/keep.mp4" in tree and site.branches["main"] == "c0"
    assert site.messages[-1] == "Delete the post old" and site.pulls[0]["title"] == 'Delete the post "Old post"'
    edits = https_client.get("/api/editor/pending").json()["edits"]
    assert [(e["branch"], e["change"]) for e in edits] == [(branch, "delete")]


def test_deleting_a_doc_keeps_images_other_pages_use(
    https_client: TestClient, https_state: State, site: FakeSite
) -> None:
    user = sign_in(https_client, https_state)
    authorise(https_state)
    plan = https_client.get("/api/editor/removal", params={"path": DOC}).json()
    assert plan["files"] == ["src/content/docs/general/a.png", DOC]  # shared.png is used by other.md
    # Deleting an entry that has an open edit turns that edit into the deletion.
    alice = "edit/alice/docs/general/swag"
    site.commits["c1"] = ({**site.files("c0"), DOC: "alice's\n![a](./a.png)\n"}, "c0")
    site.branches[alice] = "c1"
    site.add_pull(alice, "Update the docs page")
    done = https_client.post(
        "/api/editor/delete",
        json={"path": DOC, "branch": alice, "title": "SWAG"},
        headers={"X-CSRF-Token": user.csrf},
    ).json()
    assert done["ok"] and len(site.pulls) == 1 and site.pulls[0]["title"] == 'Delete the docs page "SWAG"'
    assert DOC not in site.files(alice)


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
    site.commits["c9"] = ({DOC: "branch\n"}, "c0")
    site.branches["edit/octocat/docs/general/swag"] = "c9"
    site.add_pull("edit/octocat/docs/general/swag", state="closed", merged=True)
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
