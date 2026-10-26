"""
The website's editor for the documentation and the blog. Editors (members who may push to the site repo) edit
the Markdown under `src/content/docs/` and `src/content/posts/` on the website: every save is a commit by
them on the entry's edit branch, `edit/<login>/docs/<section>/<slug>` or `edit/<login>/posts/<slug>`, and a
pull request publishes it: an open PR is the draft, the site's checks build it, merging it is publishing.
Deleting a page or post is the same: one commit that removes the entry and its files, and its pull request.

An edit belongs to the entry, not to a person. Everyone sees the open edits of what they are reading
(`/api/editor/pending`, read with the installation token) and joins one by naming its branch: their saves go
onto it, so one change stays one pull request however many people work on it.

Sign-in (auth.py) keeps nothing of the user's. Editing asks once more, for the GitHub App's own
authorisation (`/edit/authorize`, which comes back through `/login/callback`), and keeps the user-to-server
token it returns, encrypted with a key derived from the session secret, so commits and PRs are the member's
own. That token may do whatever both the App and the member may do in the org, so it never leaves this
process and is only ever used on the site repo, on the content trees and their media. It is refreshed when it
expires, dropped when GitHub stops accepting it (or the session secret changes), and revoked on sign-out.

Files go up one at a time, as git blobs, when they are added in the editor (`/api/editor/upload`); a save
then names them, so a save request stays small whatever the videos weigh. Images sit beside the Markdown (the
site optimises them); videos are served as they are, from `public/media/<slug>/` for a post and
`public/media/docs/<section>/<slug>/` for a docs page. Commits are made with the git data API (tree, commit,
then a fast-forward of the branch, which refuses when someone saved in between).

Reads are member-only GETs; writes are POSTs with the CSRF token, rate limited apart from the member actions
(manage.py), and each writes an `actions` row whatever the outcome.
"""

import asyncio
import base64
import binascii
import datetime
import hashlib
import logging
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote, urlencode

import httpx
import yaml
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from . import auth, manage
from .db import utcnow
from .github import GitHub, GraphQLError, installation_client
from .state import State, state_of

log = logging.getLogger(__name__)

router = APIRouter()

PURPOSE = "edit"
"""The `purpose` of a pending OAuth cookie that authorises the editor rather than signs in."""
DOC = re.compile(r"src/content/docs/(?P<section>[a-z0-9-]+)/(?P<slug>[a-z0-9][a-z0-9-]*)\.md")
POST = re.compile(r"src/content/posts/(?P<slug>[a-z0-9][a-z0-9_-]*)/index\.md")
EDIT_BRANCH = re.compile(r"edit/(?P<login>[a-z0-9](?:[a-z0-9-]{0,38}))/(?P<key>.+)")
IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.(?:png|jpe?g|gif|webp)", re.IGNORECASE)
"""Images people may add beside a page. No SVG: the site serves it from its own origin, where it could script."""
VIDEO = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.(?:mp4|webm)", re.IGNORECASE)
"""Videos, served as uploaded: nothing is transcoded, so they must already play in browsers."""
SHOWN = re.compile(r".+\.(?:png|jpe?g|gif|webp|svg)", re.IGNORECASE)
"""Images already in a folder, listed for the editor's preview and its picker."""
MARKDOWN_MAX = 512 * 1024
IMAGE_MAX = 5 * 1024 * 1024
VIDEO_MAX = 50 * 1024 * 1024
FILES_MAX = 40
REFRESH_MARGIN = datetime.timedelta(minutes=5)
PENDING_TTL = 30.0
EDITOR_TTL = 600.0
_locks: dict[str, asyncio.Lock] = {}
_pending: dict[str, tuple[float, list["Pending"]]] = {}
_editors: dict[str, tuple[float, bool | None]] = {}


# -- entries ------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    """A docs page or a blog post, by the path of its Markdown file in the site repo."""

    path: str
    kind: Literal["doc", "post"]
    key: str
    """`docs/<section>/<slug>` or `posts/<slug>`: the tail of an edit branch, and how people name it."""

    @property
    def folder(self) -> str:
        return self.path.rsplit("/", 1)[0]

    @property
    def media(self) -> str:
        """Where its videos live in the repo; the site serves `public/` at its root."""
        return f"public/media/{self.key[6:]}" if self.kind == "post" else f"public/media/{self.key}"

    def branch(self, login: str) -> str:
        return f"edit/{login.lower()}/{self.key}"

    @property
    def noun(self) -> str:
        return "docs page" if self.kind == "doc" else "post"

    @property
    def label(self) -> str:
        return f"the docs page {self.key.removeprefix('docs/')}" if self.kind == "doc" else f"the post {self.key[6:]}"


def entry_of(path: str) -> Entry:
    """The entry a path names, or 400: only the two content trees, in the shapes the site's loaders read."""
    if match := DOC.fullmatch(path):
        return Entry(path, "doc", f"docs/{match['section']}/{match['slug']}")
    if match := POST.fullmatch(path):
        return Entry(path, "post", f"posts/{match['slug']}")
    raise HTTPException(400, "path must be src/content/docs/<section>/<slug>.md or src/content/posts/<slug>/index.md")


def entry_of_key(key: str) -> Entry | None:
    path = f"src/content/{key}.md" if key.startswith("docs/") else f"src/content/{key}/index.md"
    try:
        return entry_of(path)
    except HTTPException:
        return None


def entry_of_branch(branch: str) -> tuple[str, Entry] | None:
    """(owner, entry) of an edit branch, or None for any other branch."""
    match = EDIT_BRANCH.fullmatch(branch)
    entry = entry_of_key(match["key"]) if match else None
    return (match["login"], entry) if match and entry else None


def branch_for(target: Entry, login: str, branch: str | None) -> str:
    """The branch a request works on: the one it names (anyone's edit of this entry), else the member's own."""
    if not branch:
        return target.branch(login)
    found = entry_of_branch(branch)
    if found is None or found[1] != target:
        raise HTTPException(400, f"{branch!r} is not an edit branch of {target.key}")
    return branch


# -- tokens -------------------------------------------------------------------------------------------------------


class Grant(BaseModel):
    token: str
    refresh_token: str | None = None
    expires_at: str | None = None
    refresh_expires_at: str | None = None

    def expiring(self, now: datetime.datetime) -> bool:
        return bool(self.expires_at) and datetime.datetime.fromisoformat(str(self.expires_at)) - now < REFRESH_MARGIN


def _fernet(state: State) -> Fernet:
    key = hashlib.sha256(b"control editor tokens\0" + state.session_secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def _grant_of(body: dict[str, Any], now: datetime.datetime) -> Grant:
    """GitHub's token answer. Apps with expiring user tokens send `expires_in` and a refresh token; others do not."""

    def at(seconds: Any) -> str | None:
        return (now + datetime.timedelta(seconds=int(seconds))).isoformat(timespec="seconds") if seconds else None

    return Grant(
        token=str(body["access_token"]),
        refresh_token=body.get("refresh_token"),
        expires_at=at(body.get("expires_in")),
        refresh_expires_at=at(body.get("refresh_token_expires_in")),
    )


def store(state: State, login: str, grant: Grant) -> None:
    fernet, now = _fernet(state), utcnow()
    token = fernet.encrypt(grant.token.encode()).decode()
    refresh = fernet.encrypt(grant.refresh_token.encode()).decode() if grant.refresh_token else None
    with state.db.connect() as conn:
        conn.execute(
            "INSERT INTO editor_tokens (login, token, refresh_token, expires_at, refresh_expires_at, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(login) DO UPDATE SET token = excluded.token, "
            "refresh_token = excluded.refresh_token, expires_at = excluded.expires_at, "
            "refresh_expires_at = excluded.refresh_expires_at, updated_at = excluded.updated_at",
            (login.lower(), token, refresh, grant.expires_at, grant.refresh_expires_at, now, now),
        )


def load(state: State, login: str) -> Grant | None:
    """The member's grant, or None (also when it was encrypted under another session secret: then it is dropped)."""
    with state.db.connect() as conn:
        row = conn.execute("SELECT * FROM editor_tokens WHERE login = ?", (login.lower(),)).fetchone()
    if row is None:
        return None
    fernet = _fernet(state)
    try:
        token = fernet.decrypt(row["token"].encode()).decode()
        refresh = fernet.decrypt(row["refresh_token"].encode()).decode() if row["refresh_token"] else None
    except InvalidToken:
        log.warning("editor token of %s unreadable (session secret changed); dropped", login)
        drop(state, login)
        return None
    return Grant(
        token=token, refresh_token=refresh, expires_at=row["expires_at"], refresh_expires_at=row["refresh_expires_at"]
    )


def drop(state: State, login: str) -> None:
    with state.db.connect() as conn:
        conn.execute("DELETE FROM editor_tokens WHERE login = ?", (login.lower(),))


def _app_client(state: State) -> auth.OAuthClient:
    """Always the GitHub App's own OAuth client: a classic OAuth App's token could write far outside the org."""
    credentials = state.credentials.load()
    if credentials is None:
        raise HTTPException(503, "the GitHub App is not configured")
    return auth.OAuthClient(
        client_id=credentials.client_id, client_secret=credentials.client_secret, scopes=None, user_membership=False
    )


async def _exchange(state: State, fields: dict[str, str]) -> dict[str, Any]:
    oauth = _app_client(state)
    response = await state.http.post(
        f"{auth.GITHUB}/login/oauth/access_token",
        data={"client_id": oauth.client_id, "client_secret": oauth.client_secret, **fields},
        headers={"Accept": "application/json"},
    )
    response.raise_for_status()
    body: dict[str, Any] = response.json()
    return body


async def user_token(state: State, login: str) -> str | None:
    """A working token for the member, refreshed when it is about to expire; None means authorise again."""
    grant = load(state, login)
    if grant is None or not grant.expiring(datetime.datetime.now(datetime.UTC)):
        return grant.token if grant else None
    async with _locks.setdefault(login.lower(), asyncio.Lock()):
        # Another request may have refreshed it while this one waited; GitHub's refresh tokens are single use.
        grant = load(state, login)
        now = datetime.datetime.now(datetime.UTC)
        if grant is None or not grant.expiring(now):
            return grant.token if grant else None
        if not grant.refresh_token:
            drop(state, login)
            return None
        try:
            body = await _exchange(state, {"grant_type": "refresh_token", "refresh_token": grant.refresh_token})
        except httpx.HTTPError:
            log.exception("refreshing the editor token of %s failed", login)
            return None
        if not body.get("access_token"):
            log.info("editor token of %s not refreshed (%s); authorise again", login, body.get("error"))
            drop(state, login)
            return None
        fresh = _grant_of(body, now)
        store(state, login, fresh)
        return fresh.token


async def revoke(state: State, login: str) -> None:
    """Drop the member's token and tell GitHub to forget it. Never raises: sign-out must not fail on this."""
    grant = load(state, login)
    drop(state, login)
    credentials = state.credentials.load()
    if grant is None or credentials is None:
        return
    try:
        await state.http.request(
            "DELETE",
            f"{auth.API}/applications/{credentials.client_id}/token",
            json={"access_token": grant.token},
            auth=(credentials.client_id, credentials.client_secret),
            headers={"Accept": "application/vnd.github+json"},
        )
    except httpx.HTTPError:
        log.exception("revoking the editor token of %s failed", login)


# -- authorisation ------------------------------------------------------------------------------------------------


@router.get("/edit/authorize")
def authorize(request: Request, next: str | None = None) -> RedirectResponse:
    """Start the editor's authorisation: a signed-in member only, through the App's OAuth client and back to
    `/login/callback`, the App's registered callback, which hands a pending cookie with this purpose to `finish`."""
    state = state_of(request)
    user = auth.current_user(request)
    target = auth.safe_next(next, state.settings.allowed_origins)
    if user is None:
        return RedirectResponse(f"/login?{urlencode({'next': target})}", status_code=303)
    if not user.member:
        raise HTTPException(403, f"{user.login} is not a member of {state.settings.github_org}")
    oauth = _app_client(state)
    token = secrets.token_urlsafe(24)
    issued = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    query = urlencode(
        {"client_id": oauth.client_id, "redirect_uri": f"{state.settings.public_url}/login/callback", "state": token}
    )
    response = RedirectResponse(f"{auth.GITHUB}/login/oauth/authorize?{query}", status_code=303)
    pending = {"state": token, "next": target, "issued_at": issued, "purpose": PURPOSE, "login": user.login}
    response.set_cookie(
        auth.OAUTH_COOKIE,
        auth.sign(state.session_secret, pending),
        max_age=int(auth.OAUTH_TTL.total_seconds()),
        httponly=True,
        secure=state.settings.public_url.startswith("https://"),
        samesite="lax",
        path="/login",
    )
    return response


async def finish(request: Request, pending: dict[str, Any], code: str) -> RedirectResponse:
    """The second half, from `/login/callback`: the token must belong to the member who is signed in."""
    state = state_of(request)
    user = auth.current_user(request)
    if user is None or not user.member or user.login.lower() != str(pending.get("login", "")).lower():
        raise HTTPException(400, "sign in as the member who started this, then authorise the editor again")
    now = datetime.datetime.now(datetime.UTC)
    try:
        body = await _exchange(state, {"code": code, "redirect_uri": f"{state.settings.public_url}/login/callback"})
        if not body.get("access_token"):
            raise HTTPException(502, f"GitHub did not return a token: {body.get('error', 'unknown')}")
        grant = _grant_of(body, now)
        who = await GitHub(state.http, grant.token).get_json("/user")
    except httpx.HTTPError as exc:
        log.exception("editor authorisation failed")
        raise HTTPException(502, f"GitHub rejected the authorisation: {exc}") from exc
    if str(who.get("login", "")).lower() != user.login.lower():
        raise HTTPException(400, f"GitHub authorised {who.get('login')}, but {user.login} is signed in here")
    store(state, user.login, grant)
    state.model.add_action(user.login, "edit-authorize", repo_of(state), "ok", "authorised the website editor")
    log.info("%s authorised the website editor", user.login)
    response = RedirectResponse(auth.safe_next(str(pending.get("next") or "/"), state.settings.allowed_origins), 303)
    response.delete_cookie(auth.OAUTH_COOKIE, path="/login")
    return response


# -- GitHub, as the member ----------------------------------------------------------------------------------------


def repo_of(state: State) -> str:
    return f"{state.settings.github_org}/{state.settings.site_repo}"


async def _member(request: Request) -> auth.User:
    """Reads need a member, not a CSRF token: CORS keeps their answers to the allowed origins."""
    state = state_of(request)
    user = auth.current_user(request)
    if user is None:
        raise HTTPException(401, "sign in first")
    user, _ = await auth.refresh_if_stale(state, user)
    if not user.member:
        raise HTTPException(403, f"{user.login} is not a member of {state.settings.github_org}")
    return user


async def _client(state: State, login: str) -> GitHub:
    token = await user_token(state, login)
    if token is None:
        raise HTTPException(428, "authorise the editor first")
    return GitHub(state.http, token)


def _github_error(state: State, login: str, exc: httpx.HTTPStatusError) -> HTTPException:
    """GitHub's refusal as ours. A 401 means the member revoked us on GitHub: forget the token, ask again."""
    status = exc.response.status_code
    if status == 401:
        drop(state, login)
        return HTTPException(428, "GitHub no longer accepts the editor's authorisation; authorise it again")
    if status in (403, 404):
        return HTTPException(403, f"{login} cannot write to {repo_of(state)} (GitHub answered {status})")
    return HTTPException(502, f"GitHub answered {status} for {exc.request.url.path}")


async def _head(github: GitHub, repo: str, branch: str) -> str | None:
    """The commit a branch points at, or None when there is no such branch."""
    try:
        ref = await github.get_json(f"/repos/{repo}/git/ref/heads/{quote(branch)}")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None
        raise
    return str(ref["object"]["sha"])


async def _file(github: GitHub, repo: str, path: str, ref: str) -> str | None:
    try:
        body = await github.get_json(f"/repos/{repo}/contents/{quote(path)}", {"ref": ref})
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None
        raise
    if not isinstance(body, dict) or body.get("type") != "file":
        return None
    if body.get("encoding") == "base64":
        return base64.b64decode(str(body.get("content", ""))).decode()
    # Over 1 MB the contents API leaves the content out; Markdown that large is refused on save anyway.
    raise HTTPException(413, f"{path} is too large for the editor")


async def _listing(github: GitHub, repo: str, folder: str, ref: str) -> list[str]:
    """File names in a folder at a ref (none when it does not exist)."""
    try:
        listing = await github.get_json(f"/repos/{repo}/contents/{quote(folder)}", {"ref": ref})
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return []
        raise
    if not isinstance(listing, list):
        return []
    return sorted(str(i["name"]) for i in listing if i.get("type") == "file")


async def _tree(github: GitHub, repo: str, sha: str) -> list[str]:
    """Every file path in a commit."""
    commit = await github.get_json(f"/repos/{repo}/git/commits/{sha}")
    tree = await github.get_json(f"/repos/{repo}/git/trees/{commit['tree']['sha']}", {"recursive": "1"})
    return [str(t["path"]) for t in tree.get("tree", []) if t.get("type") == "blob"]


class Pull(BaseModel):
    number: int
    title: str
    url: str
    branch: str
    draft: bool = False
    checks: Literal["pending", "success", "failure"] | None = None
    updated_at: str | None = None


def _pull(raw: dict[str, Any]) -> Pull:
    return Pull(
        number=int(raw["number"]),
        title=str(raw["title"]),
        url=str(raw["html_url"]),
        branch=str(raw["head"]["ref"]),
        draft=bool(raw.get("draft")),
        updated_at=raw.get("updated_at"),
    )


async def _open_pull(github: GitHub, repo: str, branch: str) -> Pull | None:
    owner = repo.split("/", 1)[0]
    pulls = await github.get_json(f"/repos/{repo}/pulls", {"state": "open", "head": f"{owner}:{branch}"})
    return _pull(pulls[0]) if pulls else None


async def _merged(github: GitHub, repo: str, branch: str, sha: str) -> bool:
    """Whether a pull request merged this branch at exactly this commit (squash merges included)."""
    owner = repo.split("/", 1)[0]
    closed = await github.get_json(f"/repos/{repo}/pulls", {"state": "closed", "head": f"{owner}:{branch}"})
    return any(p.get("merged_at") and p["head"]["sha"] == sha for p in closed)


def _title(markdown: str | None) -> str | None:
    """The front matter's `title`, when it has one."""
    match = re.match(r"---\r?\n(.*?)\r?\n---", markdown or "", re.DOTALL)
    try:
        data = yaml.safe_load(match[1]) if match else None
    except yaml.YAMLError:
        return None
    title = data.get("title") if isinstance(data, dict) else None
    return str(title) if title else None


# -- who may edit -------------------------------------------------------------------------------------------------


async def can_edit(state: State, login: str) -> bool | None:
    """Whether GitHub lets the member push to the site repo, read with the installation token so the site can show
    its buttons before the editor is authorised; None when GitHub cannot say. Cached for ten minutes."""
    key = f"{state.db.path}:{login.lower()}"
    cached = _editors.get(key)
    if cached and time.monotonic() - cached[0] < EDITOR_TTL:
        return cached[1]
    verdict: bool | None = None
    try:
        github = await installation_client(state)
        if github is not None:
            body = await github.get_json(f"/repos/{repo_of(state)}/collaborators/{login}/permission")
            verdict = body.get("permission") in ("admin", "maintain", "write")
    except httpx.HTTPError:
        log.warning("cannot read %s's permission on %s", login, repo_of(state))
    _editors[key] = (time.monotonic(), verdict)
    return verdict


# -- open edits ---------------------------------------------------------------------------------------------------


class Pending(BaseModel):
    """An open edit of an entry: a branch `edit/<owner>/<key>`, and its pull request once there is one."""

    path: str
    kind: Literal["doc", "post"]
    key: str
    branch: str
    owner: str
    """Who started it (the branch's login); others may have joined since."""
    change: Literal["new", "edit", "delete"]
    title: str | None = None
    head: str
    updated_at: str | None = None
    pull: Pull | None = None


REFS = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef { name }
    refs(refPrefix: "refs/heads/edit/", first: 100) {
      nodes {
        name
        target { ... on Commit { oid committedDate } }
        associatedPullRequests(states: OPEN, first: 1) {
          nodes {
            number title url isDraft updatedAt headRefName
            commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
          }
        }
      }
    }
  }
}
"""

ROLLUP: dict[str, Literal["pending", "success", "failure"]] = {
    "SUCCESS": "success",
    "PENDING": "pending",
    "EXPECTED": "pending",
    "FAILURE": "failure",
    "ERROR": "failure",
}


def forget_pending(state: State) -> None:
    _pending.pop(str(state.db.path), None)


async def pending(state: State) -> list[Pending]:
    """Every open edit in the site repo, by anyone: two GraphQL calls with the installation token (the branches with
    their pull requests, then each one's Markdown and whether the default branch has it), cached briefly."""
    key = str(state.db.path)
    cached = _pending.get(key)
    if cached and time.monotonic() - cached[0] < PENDING_TTL:
        return cached[1]
    github = await installation_client(state)
    if github is None:
        raise HTTPException(503, "the GitHub App is not configured or not installed")
    owner, name = repo_of(state).split("/", 1)
    data = await github.graphql(REFS, {"owner": owner, "name": name})
    repo = data["repository"]
    base = str((repo.get("defaultBranchRef") or {}).get("name") or "main")
    found: list[tuple[str, str, Entry, dict[str, Any]]] = []
    for node in repo["refs"]["nodes"]:
        branch = f"edit/{node['name']}"
        if (hit := entry_of_branch(branch)) is not None:
            found.append((branch, hit[0], hit[1], node))
    items: list[Pending] = []
    if found:
        aliases = []
        for i, (_, _, target, node) in enumerate(found):
            oid = node["target"]["oid"]
            aliases.append(f'b{i}: object(expression: "{oid}:{target.path}") {{ ... on Blob {{ text }} }}')
            aliases.append(f'm{i}: object(expression: "{base}:{target.path}") {{ oid }}')
        query = "query($owner: String!, $name: String!) { repository(owner: $owner, name: $name) { %s } }"
        files = (await github.graphql(query % " ".join(aliases), {"owner": owner, "name": name}))["repository"]
        for i, (branch, login, target, node) in enumerate(found):
            text = (files.get(f"b{i}") or {}).get("text")
            live = files.get(f"m{i}") is not None
            if text is None and not live:
                continue  # an edit branch of something that exists nowhere: nothing to show
            raw = (node.get("associatedPullRequests") or {}).get("nodes") or []
            pull = None
            if raw:
                pr = raw[0]
                commits = (pr.get("commits") or {}).get("nodes") or []
                rollup = ((commits[0]["commit"].get("statusCheckRollup") or {}) if commits else {}).get("state")
                pull = Pull(
                    number=int(pr["number"]),
                    title=str(pr["title"]),
                    url=str(pr["url"]),
                    branch=branch,
                    draft=bool(pr.get("isDraft")),
                    checks=ROLLUP.get(str(rollup)) if rollup else None,
                    updated_at=pr.get("updatedAt"),
                )
            items.append(
                Pending(
                    path=target.path,
                    kind=target.kind,
                    key=target.key,
                    branch=branch,
                    owner=login,
                    change="delete" if text is None else "edit" if live else "new",
                    title=_title(text) or (pull.title if pull else None),
                    head=str(node["target"]["oid"]),
                    updated_at=(node["target"] or {}).get("committedDate"),
                    pull=pull,
                )
            )
    items.sort(key=lambda p: p.updated_at or "", reverse=True)
    _pending[key] = (time.monotonic(), items)
    return items


# -- reads --------------------------------------------------------------------------------------------------------


class Status(BaseModel):
    """What the site needs to know about the visitor: whether to show the editing buttons (`can_edit`) and whether
    the editor still has to be authorised. Anonymous visitors get `authenticated: false`, never a 401."""

    authenticated: bool
    member: bool = False
    login: str | None = None
    can_edit: bool = False
    authorized: bool = False
    repo: str
    base: str = "main"


@router.get("/api/editor/status")
async def status(request: Request) -> Status:
    state = state_of(request)
    repo = repo_of(state)
    user = auth.current_user(request)
    if user is None:
        return Status(authenticated=False, repo=repo)
    user, _ = await auth.refresh_if_stale(state, user)
    if not user.member:
        return Status(authenticated=True, login=user.login, repo=repo)
    verdict = await can_edit(state, user.login)
    authorized = await user_token(state, user.login) is not None
    return Status(
        authenticated=True,
        member=True,
        login=user.login,
        can_edit=user.member if verdict is None else verdict,
        authorized=authorized,
        repo=repo,
    )


class PendingPage(BaseModel):
    edits: list[Pending]


@router.get("/api/editor/pending")
async def pending_route(request: Request) -> PendingPage:
    """Open edits for the site's pages to show beside what they change: one list, filtered by the page."""
    state = state_of(request)
    await _member(request)
    try:
        return PendingPage(edits=await pending(state))
    except (httpx.HTTPError, GraphQLError) as exc:
        log.exception("reading open edits failed")
        raise HTTPException(502, f"GitHub did not list the open edits: {exc}") from exc


class EntryPage(BaseModel):
    """One docs page or post as the editor opens it, from an edit branch when there is one."""

    path: str
    kind: Literal["doc", "post"]
    key: str
    exists: bool
    """Whether the file is on the branch it was read from; false for a new entry (or one this edit deletes)."""
    published: bool
    """Whether the file is on the default branch, i.e. live on the site."""
    content: str
    branch: str
    """The edit branch saves go to: the one asked for, or the member's own (which may not exist yet)."""
    owner: str
    """Who started that branch."""
    on_branch: bool
    """Whether `content` came from that branch rather than the default branch."""
    base: str
    head: str
    """The commit the content was read at: send it back with the save, which refuses if the branch moved."""
    images: list[str]
    """Images beside the entry at `head`, for the preview and the pickers."""
    media: list[str]
    """Videos in the entry's media folder at `head`."""
    media_url: str
    """The site path the entry's videos are served at: `<media_url><name>`."""
    raw_base: str
    """Where the preview loads the entry's committed images: `<raw_base><name>`."""
    raw_root: str
    """The repo at `head`, for root-relative files (`/media/…` is `<raw_root>public/media/…`)."""
    others: list[str]
    """Other open edit branches of this entry, which the member could join instead."""
    pull: Pull | None = None


@router.get("/api/editor/entry")
async def entry(request: Request, path: str, branch: str | None = None) -> EntryPage:
    state = state_of(request)
    user = await _member(request)
    target = entry_of(path)
    repo = repo_of(state)
    github = await _client(state, user.login)
    work = branch_for(target, user.login, branch)
    try:
        meta = await github.get_json(f"/repos/{repo}")
        base = str(meta.get("default_branch") or "main")
        branch_head = await _head(github, repo, work)
        pull = await _open_pull(github, repo, work) if branch_head else None
        if branch_head and pull is None and await _merged(github, repo, work, branch_head):
            # Published already and the branch was kept: start the next edit from the default branch.
            await github.delete(f"/repos/{repo}/git/refs/heads/{quote(work)}")
            branch_head = None
            forget_pending(state)
        on_branch = branch_head is not None
        head = branch_head or await _head(github, repo, base)
        if head is None:
            raise HTTPException(502, f"{repo} has no {base} branch")
        content = await _file(github, repo, target.path, head)
        published = content is not None if not on_branch else await _file(github, repo, target.path, base) is not None
        images = [n for n in await _listing(github, repo, target.folder, head) if SHOWN.fullmatch(n)]
        media = [n for n in await _listing(github, repo, target.media, head) if VIDEO.fullmatch(n)]
    except httpx.HTTPStatusError as exc:
        raise _github_error(state, user.login, exc) from exc
    try:
        others = [p.branch for p in await pending(state) if p.key == target.key and p.branch != work]
    except (httpx.HTTPError, GraphQLError, HTTPException):
        others = []
    owner = (entry_of_branch(work) or (user.login, target))[0]
    return EntryPage(
        path=target.path,
        kind=target.kind,
        key=target.key,
        exists=content is not None,
        published=published,
        content=content or "",
        branch=work,
        owner=owner,
        on_branch=on_branch,
        base=base,
        head=head,
        images=images,
        media=media,
        media_url=f"/{target.media.removeprefix('public/')}/",
        raw_base=f"https://raw.githubusercontent.com/{repo}/{head}/{target.folder}/",
        raw_root=f"https://raw.githubusercontent.com/{repo}/{head}/",
        others=others,
        pull=pull,
    )


def _referenced(name: str, markdown: str) -> bool:
    return re.search(rf"(?:^|[\s(\[\"'/]){re.escape(name)}(?:[?#)\]\"'\s]|$)", markdown) is not None


async def _removal(github: GitHub, repo: str, target: Entry, head: str) -> list[str]:
    """What deleting an entry removes at a commit: a post's whole folder; a docs page and the images beside it that
    no other page in its folder uses; either one's media folder."""
    files = await _tree(github, repo, head)
    media = [f for f in files if f.startswith(f"{target.media}/")]
    if target.kind == "post":
        return sorted([f for f in files if f.startswith(f"{target.folder}/")] + media)
    if target.path not in files:
        return sorted(media)
    beside = [f for f in files if f.rsplit("/", 1)[0] == target.folder]
    pages = {f: await _file(github, repo, f, head) or "" for f in beside if f.endswith(".md")}
    mine = pages.pop(target.path, "")
    assets = [
        f
        for f in beside
        if not f.endswith(".md")
        and _referenced(f.rsplit("/", 1)[1], mine)
        and not any(_referenced(f.rsplit("/", 1)[1], other) for other in pages.values())
    ]
    return sorted([target.path, *assets, *media])


class Removal(BaseModel):
    path: str
    branch: str
    title: str | None
    files: list[str]
    """What the delete removes, as of now."""


@router.get("/api/editor/removal")
async def removal(request: Request, path: str, branch: str | None = None) -> Removal:
    """What deleting an entry would remove, for the confirmation."""
    state = state_of(request)
    user = await _member(request)
    target = entry_of(path)
    repo = repo_of(state)
    github = await _client(state, user.login)
    work = branch_for(target, user.login, branch)
    try:
        head = await _head(github, repo, work) or await _head(github, repo, "main")
        if head is None:
            raise HTTPException(502, f"{repo} has no main branch")
        files = await _removal(github, repo, target, head)
        title = _title(await _file(github, repo, target.path, head))
    except httpx.HTTPStatusError as exc:
        raise _github_error(state, user.login, exc) from exc
    return Removal(path=target.path, branch=work, title=title, files=files)


# -- writes -------------------------------------------------------------------------------------------------------


class UploadRequest(BaseModel):
    path: str
    """The entry the file belongs to."""
    name: str
    data: str
    """The file, base64."""


class Uploaded(BaseModel):
    name: str
    kind: Literal["image", "video"]
    sha: str
    """The git blob; name it in the next save."""
    target: str
    """Where the save puts it in the repo."""
    url: str
    """How the Markdown refers to it: `./<name>` beside the entry, or the site path of a video."""


def _placed(target: Entry, name: str) -> tuple[Literal["image", "video"], str, str]:
    """(kind, repo path, Markdown reference) of a file added to an entry, or 400 for a name or type we refuse."""
    if IMAGE.fullmatch(name):
        return "image", f"{target.folder}/{name}", f"./{name}"
    if VIDEO.fullmatch(name):
        return "video", f"{target.media}/{name}", f"/{target.media.removeprefix('public/')}/{name}"
    raise HTTPException(
        400, f"{name!r}: png, jpg, gif or webp images and mp4 or webm videos, named with a-z, 0-9, . _ -"
    )


@router.post("/api/editor/upload")
async def upload(request: Request, body: UploadRequest) -> Uploaded:
    """One file up as a git blob, before any save names it. Unreferenced blobs are garbage-collected by GitHub."""
    state = state_of(request)
    actor = await manage.require_member(request, request.headers.get("X-CSRF-Token"), editing=True)
    target = entry_of(body.path)
    kind, where, url = _placed(target, body.name)
    try:
        data = base64.b64decode(body.data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(400, f"{body.name}: not base64") from exc
    limit = IMAGE_MAX if kind == "image" else VIDEO_MAX
    if len(data) > limit:
        raise HTTPException(413, f"{body.name} is over {limit // (1024 * 1024)} MiB")
    github = await _client(state, actor.login)
    try:
        blob = await github.post(
            f"/repos/{repo_of(state)}/git/blobs", {"content": base64.b64encode(data).decode(), "encoding": "base64"}
        )
    except httpx.HTTPStatusError as exc:
        raise _github_error(state, actor.login, exc) from exc
    return Uploaded(name=body.name, kind=kind, sha=str(blob.json()["sha"]), target=where, url=url)


class FileRef(BaseModel):
    name: str
    sha: str


class SaveRequest(BaseModel):
    path: str
    content: str
    head: str
    """The `head` the editor loaded (or last saved): the branch must still point there."""
    branch: str | None = None
    """An edit branch of this entry to save onto (joining it); the member's own when absent."""
    message: str | None = None
    files: list[FileRef] = Field(default_factory=list)
    """Files uploaded since the last save."""


class SaveResult(BaseModel):
    ok: bool
    action_id: int
    detail: str
    branch: str | None = None
    head: str | None = None
    commit_url: str | None = None


class PathRequest(BaseModel):
    path: str
    branch: str | None = None
    title: str | None = None
    """For publishing and deleting: the entry's title, which names the pull request."""
    message: str | None = None


class PublishResult(BaseModel):
    ok: bool
    action_id: int
    detail: str
    pull: Pull | None = None


def _headline(message: str | None, fallback: str) -> str:
    line = (message or "").strip().splitlines()[0].strip() if (message or "").strip() else ""
    return line[:100] if line else fallback


async def _commit(
    github: GitHub, repo: str, branch: str, head: str, message: str, changes: list[tuple[str, str | None]]
) -> tuple[str, str]:
    """One commit on `branch` from `head` that sets each path to a blob (None removes it); (oid, url). The branch
    moves only as a fast-forward, so a save made elsewhere in between is a 409, never overwritten."""
    base = await github.get_json(f"/repos/{repo}/git/commits/{head}")
    entries = [{"path": p, "mode": "100644", "type": "blob", "sha": sha} for p, sha in changes]
    tree = (await github.post(f"/repos/{repo}/git/trees", {"base_tree": base["tree"]["sha"], "tree": entries})).json()
    commit = (
        await github.post(f"/repos/{repo}/git/commits", {"message": message, "tree": tree["sha"], "parents": [head]})
    ).json()
    try:
        await github.patch(f"/repos/{repo}/git/refs/heads/{quote(branch)}", {"sha": commit["sha"], "force": False})
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 422:
            raise HTTPException(
                409, f"{branch} moved since you loaded it (saved elsewhere?); reload to see it"
            ) from exc
        raise
    return str(commit["sha"]), str(commit["html_url"])


async def _branch_at(github: GitHub, repo: str, branch: str, head: str) -> str:
    """The branch's head, after creating it at `head` when it does not exist; 409 when it moved past `head`."""
    current = await _head(github, repo, branch)
    if current is None:
        try:
            await github.post(f"/repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": head})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 422:
                raise HTTPException(409, "the commit you loaded is gone; reload the entry") from exc
            raise
        return head
    if current != head:
        raise HTTPException(409, f"{branch} moved since you loaded it (saved elsewhere?); reload to see it")
    return current


async def _open_or_reuse(github: GitHub, repo: str, branch: str, title: str, text: str) -> tuple[Pull, bool]:
    if (existing := await _open_pull(github, repo, branch)) is not None:
        return existing, False
    meta = await github.get_json(f"/repos/{repo}")
    base = str(meta.get("default_branch") or "main")
    raw = (
        await github.post(f"/repos/{repo}/pulls", {"title": title, "head": branch, "base": base, "body": text})
    ).json()
    return _pull(raw), True


@dataclass
class Logged:
    action_id: int
    ok: bool
    detail: str
    value: Any
    status: int | None
    """The HTTP status a refusal carried, so a route can still answer 409 for a conflict after logging it."""


async def _logged(state: State, login: str, action: str, target: str, work: Any) -> Logged:
    """Run a write and log it whatever happens. Refusals become `ok: false` with the reason, except a missing
    authorisation or the rate limit, which stay errors (nothing was attempted)."""
    value: Any = None
    status: int | None = None
    try:
        detail, value = await work
        ok = True
    except HTTPException as exc:
        if exc.status_code in (401, 428, 429):
            raise
        ok, detail, status = False, str(exc.detail), exc.status_code
    except httpx.HTTPStatusError as exc:
        error = _github_error(state, login, exc)
        ok, detail, status = False, str(error.detail), error.status_code
    except GraphQLError as exc:
        ok, detail, status = False, str(exc), None
    forget_pending(state)
    action_id = state.model.add_action(login, action, target, "ok" if ok else "error", detail)
    state.stream.publish("actions", action_id)
    log.info("%s %s %s by %s: %s", action, target, "ok" if ok else "error", login, detail)
    return Logged(action_id, ok, detail, value, status)


def _answer(state: State, actor: manage.Actor, model: BaseModel) -> JSONResponse:
    response = JSONResponse(model.model_dump())
    if actor.refreshed:
        auth.set_session(response, state, actor.user)
    return response


@router.post("/api/editor/save")
async def save(request: Request, body: SaveRequest) -> JSONResponse:
    """One commit on the edit branch with the Markdown and the files uploaded since the last save. A new branch
    starts at the commit the editor loaded, so a change made on the default branch meanwhile shows as a conflict in
    the pull request instead of being lost."""
    state = state_of(request)
    actor = await manage.require_member(request, request.headers.get("X-CSRF-Token"), editing=True)
    target = entry_of(body.path)
    content = body.content.replace("\r\n", "\n").rstrip("\n") + "\n"
    if len(content.encode()) > MARKDOWN_MAX:
        raise HTTPException(413, f"the Markdown is over {MARKDOWN_MAX // 1024} KiB")
    if len(body.files) > FILES_MAX:
        raise HTTPException(413, f"at most {FILES_MAX} files per save")
    placed = [(_placed(target, f.name)[1], f.sha) for f in body.files]
    if any(not re.fullmatch(r"[0-9a-f]{40}", sha) for _, sha in placed):
        raise HTTPException(400, "files must name the blobs /api/editor/upload returned")
    repo = repo_of(state)
    github = await _client(state, actor.login)
    branch = branch_for(target, actor.login, body.branch)

    async def work() -> tuple[str, dict[str, str]]:
        head = await _branch_at(github, repo, branch, body.head)
        new = await _file(github, repo, target.path, head) is None
        markdown = (await github.post(f"/repos/{repo}/git/blobs", {"content": content, "encoding": "utf-8"})).json()
        message = _headline(body.message, f"{'Add' if new else 'Update'} {target.label}")
        oid, url = await _commit(github, repo, branch, head, message, [(target.path, str(markdown["sha"])), *placed])
        extra = f" with {len(placed)} file{'s' if len(placed) > 1 else ''}" if placed else ""
        return f"saved {target.key} to {branch}{extra}", {"oid": oid, "url": url}

    done = await _logged(state, actor.login, "edit-save", target.key, work())
    if done.status == 409:
        raise HTTPException(409, done.detail)
    commit = done.value
    answer = SaveResult(
        ok=done.ok,
        action_id=done.action_id,
        detail=done.detail,
        branch=branch if commit else None,
        head=commit["oid"] if commit else None,
        commit_url=commit["url"] if commit else None,
    )
    return _answer(state, actor, answer)


@router.post("/api/editor/publish")
async def publish(request: Request, body: PathRequest) -> JSONResponse:
    """Open the pull request for an edit (or return the open one). Merging it publishes."""
    state = state_of(request)
    actor = await manage.require_member(request, request.headers.get("X-CSRF-Token"), editing=True)
    target = entry_of(body.path)
    repo = repo_of(state)
    github = await _client(state, actor.login)
    branch = branch_for(target, actor.login, body.branch)

    async def work() -> tuple[str, Pull]:
        if await _head(github, repo, branch) is None:
            raise HTTPException(404, "nothing saved yet: save first")
        new = await _file(github, repo, target.path, "main") is None
        title = (body.title or "").strip().splitlines()[0][:80] if (body.title or "").strip() else target.key
        text = (
            f"{'A new' if new else 'An edit to the'} {target.noun} `{target.path}`, written in the website editor.\n\n"
            "Merging this pull request publishes it; the site's checks build it first. "
            "Further saves in the editor add commits here."
        )
        pull, opened = await _open_or_reuse(
            github, repo, branch, f'{"Add" if new else "Update"} the {target.noun} "{title}"', text
        )
        if not opened:
            return f"pull request #{pull.number} is already open", pull
        return f"opened pull request #{pull.number} for {target.key}", pull

    done = await _logged(state, actor.login, "edit-publish", target.key, work())
    answer = PublishResult(ok=done.ok, action_id=done.action_id, detail=done.detail, pull=done.value)
    return _answer(state, actor, answer)


@router.post("/api/editor/delete")
async def delete(request: Request, body: PathRequest) -> JSONResponse:
    """Propose deleting an entry: one commit that removes it and its files, and its pull request (on the entry's open
    edit when it is joined, else the member's own branch from the default branch). Nothing redirects: after the merge
    the address is simply gone."""
    state = state_of(request)
    actor = await manage.require_member(request, request.headers.get("X-CSRF-Token"), editing=True)
    target = entry_of(body.path)
    repo = repo_of(state)
    github = await _client(state, actor.login)
    branch = branch_for(target, actor.login, body.branch)

    async def work() -> tuple[str, Pull]:
        head = await _head(github, repo, branch)
        if head is None:
            main = await _head(github, repo, "main")
            if main is None:
                raise HTTPException(502, f"{repo} has no main branch")
            head = await _branch_at(github, repo, branch, main)
        files = await _removal(github, repo, target, head)
        if not files:
            raise HTTPException(404, f"{target.key} has nothing to delete")
        title = (body.title or "").strip().splitlines()[0][:80] if (body.title or "").strip() else target.key
        message = _headline(body.message, f"Delete {target.label}")
        await _commit(github, repo, branch, head, message, [(f, None) for f in files])
        listed = "\n".join(f"- `{f}`" for f in files)
        text = f"Deletes the {target.noun} `{target.path}`, from the website editor. Merging it removes:\n\n{listed}\n"
        heading = f'Delete the {target.noun} "{title}"'
        pull, opened = await _open_or_reuse(github, repo, branch, heading, text)
        if not opened:
            # The edit it joined becomes a deletion: say so where reviewers look.
            await github.patch(f"/repos/{repo}/pulls/{pull.number}", {"title": heading, "body": text})
            pull = pull.model_copy(update={"title": heading})
        return f"proposed deleting {target.key} ({len(files)} files) in pull request #{pull.number}", pull

    done = await _logged(state, actor.login, "edit-delete", target.key, work())
    if done.status == 409:
        raise HTTPException(409, done.detail)
    answer = PublishResult(ok=done.ok, action_id=done.action_id, detail=done.detail, pull=done.value)
    return _answer(state, actor, answer)


@router.post("/api/editor/discard")
async def discard(request: Request, body: PathRequest) -> JSONResponse:
    """Throw an edit away: close its pull request and delete the branch. The default branch is untouched."""
    state = state_of(request)
    actor = await manage.require_member(request, request.headers.get("X-CSRF-Token"), editing=True)
    target = entry_of(body.path)
    repo = repo_of(state)
    github = await _client(state, actor.login)
    branch = branch_for(target, actor.login, body.branch)

    async def work() -> tuple[str, None]:
        if await _head(github, repo, branch) is None:
            raise HTTPException(404, f"no edit of {target.key} to discard")
        closed = ""
        if (pull := await _open_pull(github, repo, branch)) is not None:
            await github.patch(f"/repos/{repo}/pulls/{pull.number}", {"state": "closed"})
            closed = f", closed #{pull.number}"
        await github.delete(f"/repos/{repo}/git/refs/heads/{quote(branch)}")
        return f"discarded {branch}{closed}", None

    done = await _logged(state, actor.login, "edit-discard", target.key, work())
    return _answer(state, actor, PublishResult(ok=done.ok, action_id=done.action_id, detail=done.detail))


@router.post("/api/editor/revoke", status_code=204)
async def revoke_route(request: Request) -> Response:
    """Forget the editor's authorisation (sign-in stays). Sign-out does this too."""
    state = state_of(request)
    user = auth.current_user(request)
    if user is None:
        raise HTTPException(401, "not signed in")
    if not auth.csrf_ok(user, request.headers.get("X-CSRF-Token")):
        raise HTTPException(403, "bad CSRF token; read it again from /api/me")
    await revoke(state, user.login)
    return Response(status_code=204)
