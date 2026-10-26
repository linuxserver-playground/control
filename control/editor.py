"""
The website's editor for the documentation and the blog. Members edit the Markdown under the site repo's
`src/content/docs/` and `src/content/posts/` on the website; every save is a commit by the member on their
own branch, `edit/<login>/docs/<section>/<slug>` or `edit/<login>/posts/<slug>`, and a pull request
publishes it: an open PR is the draft, the site's checks build it, merging it is publishing.

Sign-in (auth.py) keeps nothing of the user's. Editing asks once more, for the GitHub App's own
authorisation (`/edit/authorize`, which comes back through `/login/callback`), and keeps the user-to-server
token it returns, encrypted with a key derived from the session secret, so commits and PRs are the member's
own. That token may do whatever both the App and the member may do in the org, so it never leaves this
process and is only ever used on the site repo, on the two content trees. It is refreshed when it expires,
dropped when GitHub stops accepting it (or the session secret changes), and revoked on sign-out.

Reads are member-only GETs; saves, publishing and discarding are POSTs with the CSRF token, rate limited
apart from the member actions (manage.py), and each writes an `actions` row whatever the outcome.
"""

import asyncio
import base64
import binascii
import datetime
import hashlib
import logging
import re
import secrets
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote, urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from . import auth, manage
from .db import utcnow
from .github import GitHub, GraphQLError
from .state import State, state_of

log = logging.getLogger(__name__)

router = APIRouter()

PURPOSE = "edit"
"""The `purpose` of a pending OAuth cookie that authorises the editor rather than signs in."""
DOC = re.compile(r"src/content/docs/(?P<section>[a-z0-9-]+)/(?P<slug>[a-z0-9][a-z0-9-]*)\.md")
POST = re.compile(r"src/content/posts/(?P<slug>[a-z0-9][a-z0-9_-]*)/index\.md")
ASSET = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.(?:png|jpe?g|gif|webp)", re.IGNORECASE)
"""Images people may add beside a page. No SVG: the site serves it from its own origin, where it could script."""
SHOWN = re.compile(r".+\.(?:png|jpe?g|gif|webp|svg)", re.IGNORECASE)
"""Images already in a folder, listed for the editor's preview and its picker."""
MARKDOWN_MAX = 512 * 1024
ASSET_MAX = 5 * 1024 * 1024
SAVE_MAX = 20 * 1024 * 1024
ASSETS_MAX = 20
REFRESH_MARGIN = datetime.timedelta(minutes=5)
_locks: dict[str, asyncio.Lock] = {}


# -- entries ------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    """A docs page or a blog post, by the path of its Markdown file in the site repo."""

    path: str
    kind: Literal["doc", "post"]
    key: str
    """`docs/<section>/<slug>` or `posts/<slug>`: the tail of the edit branch, and how people name it."""

    @property
    def folder(self) -> str:
        return self.path.rsplit("/", 1)[0]

    def branch(self, login: str) -> str:
        return f"edit/{login.lower()}/{self.key}"

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


def entry_of_branch(branch: str, login: str) -> Entry | None:
    prefix = f"edit/{login.lower()}/"
    if not branch.startswith(prefix):
        return None
    key = branch[len(prefix) :]
    if key.startswith("docs/"):
        path = f"src/content/{key}.md"
    elif key.startswith("posts/"):
        path = f"src/content/{key}/index.md"
    else:
        return None
    try:
        return entry_of(path)
    except HTTPException:
        return None


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


async def _images(github: GitHub, repo: str, folder: str, ref: str) -> list[str]:
    try:
        listing = await github.get_json(f"/repos/{repo}/contents/{quote(folder)}", {"ref": ref})
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return []
        raise
    if not isinstance(listing, list):
        return []
    return sorted(str(i["name"]) for i in listing if i.get("type") == "file" and SHOWN.fullmatch(str(i["name"])))


class Pull(BaseModel):
    number: int
    title: str
    url: str
    branch: str
    path: str | None = None
    """The entry's Markdown file, when the branch is one of the editor's."""
    draft: bool = False
    checks: Literal["pending", "success", "failure"] | None = None
    updated_at: str | None = None


def _pull(raw: dict[str, Any], login: str) -> Pull:
    branch = str(raw["head"]["ref"])
    entry = entry_of_branch(branch, login)
    return Pull(
        number=int(raw["number"]),
        title=str(raw["title"]),
        url=str(raw["html_url"]),
        branch=branch,
        path=entry.path if entry else None,
        draft=bool(raw.get("draft")),
        updated_at=raw.get("updated_at"),
    )


async def _open_pull(github: GitHub, repo: str, branch: str) -> Pull | None:
    owner = repo.split("/", 1)[0]
    pulls = await github.get_json(f"/repos/{repo}/pulls", {"state": "open", "head": f"{owner}:{branch}"})
    return _pull(pulls[0], branch.split("/")[1]) if pulls else None


async def _merged(github: GitHub, repo: str, branch: str, sha: str) -> bool:
    """Whether a pull request merged this branch at exactly this commit (squash merges included)."""
    owner = repo.split("/", 1)[0]
    closed = await github.get_json(f"/repos/{repo}/pulls", {"state": "closed", "head": f"{owner}:{branch}"})
    return any(p.get("merged_at") and p["head"]["sha"] == sha for p in closed)


async def _checks(github: GitHub, repo: str, sha: str) -> Literal["pending", "success", "failure"] | None:
    """The PR head's check runs in one word: the site's check.yml builds and validates every change."""
    try:
        runs = (await github.get_json(f"/repos/{repo}/commits/{sha}/check-runs", {"per_page": 100})).get(
            "check_runs", []
        )
    except httpx.HTTPStatusError:
        return None
    if not runs:
        return None
    if any(r.get("status") != "completed" for r in runs):
        return "pending"
    if any(r.get("conclusion") not in ("success", "skipped", "neutral") for r in runs):
        return "failure"
    return "success"


# -- reads --------------------------------------------------------------------------------------------------------


class Status(BaseModel):
    """What the editor needs before it opens: who, whether they authorised it, whether GitHub lets them push,
    and their open edits. Anonymous visitors get `authenticated: false`, never a 401."""

    authenticated: bool
    member: bool = False
    login: str | None = None
    authorized: bool = False
    can_push: bool = False
    repo: str
    base: str | None = None
    edits: list[Pull] = Field(default_factory=list)


@router.get("/api/editor/status")
async def status(request: Request) -> Status:
    state = state_of(request)
    repo = repo_of(state)
    user = auth.current_user(request)
    if user is None:
        return Status(authenticated=False, repo=repo)
    user, _ = await auth.refresh_if_stale(state, user)
    answer = Status(authenticated=True, member=user.member, login=user.login, repo=repo)
    if not user.member:
        return answer
    token = await user_token(state, user.login)
    if token is None:
        return answer
    github = GitHub(state.http, token)
    try:
        meta = await github.get_json(f"/repos/{repo}")
        pulls = await github.get_json(f"/repos/{repo}/pulls", {"state": "open", "per_page": 100})
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            drop(state, user.login)
            return answer
        raise _github_error(state, user.login, exc) from exc
    edits: list[Pull] = []
    for raw in pulls:
        head = raw["head"]
        if (head.get("repo") or {}).get("full_name") == repo and entry_of_branch(str(head["ref"]), user.login):
            edit = _pull(raw, user.login)
            edit.checks = await _checks(github, repo, str(head["sha"]))
            edits.append(edit)
    return answer.model_copy(
        update={
            "authorized": True,
            "can_push": bool((meta.get("permissions") or {}).get("push")),
            "base": str(meta.get("default_branch") or "main"),
            "edits": edits,
        }
    )


class EntryPage(BaseModel):
    """One docs page or post as the editor opens it: from the member's edit branch when there is one."""

    path: str
    kind: Literal["doc", "post"]
    key: str
    exists: bool
    """Whether the file is on the branch it was read from; false for a new entry."""
    published: bool
    """Whether the file is on the default branch, i.e. live on the site."""
    content: str
    branch: str
    """The member's edit branch for this entry (it may not exist yet)."""
    on_branch: bool
    """Whether `content` came from that branch rather than the default branch."""
    base: str
    head: str
    """The commit the content was read at: send it back with the save, which refuses if the branch moved."""
    images: list[str]
    """Image files in the entry's folder at `head`, for the preview and the picker."""
    raw_base: str
    """Where the preview loads those images from: `<raw_base><name>`."""
    pull: Pull | None = None


@router.get("/api/editor/entry")
async def entry(request: Request, path: str) -> EntryPage:
    state = state_of(request)
    user = await _member(request)
    target = entry_of(path)
    repo = repo_of(state)
    github = await _client(state, user.login)
    branch = target.branch(user.login)
    try:
        meta = await github.get_json(f"/repos/{repo}")
        base = str(meta.get("default_branch") or "main")
        branch_head = await _head(github, repo, branch)
        pull = await _open_pull(github, repo, branch) if branch_head else None
        if branch_head and pull is None and await _merged(github, repo, branch, branch_head):
            # Published already and the branch was kept: start the next edit from the default branch.
            await github.delete(f"/repos/{repo}/git/refs/heads/{quote(branch)}")
            branch_head = None
        on_branch = branch_head is not None
        head = branch_head or await _head(github, repo, base)
        if head is None:
            raise HTTPException(502, f"{repo} has no {base} branch")
        content = await _file(github, repo, target.path, head)
        published = content is not None if not on_branch else await _file(github, repo, target.path, base) is not None
        images = await _images(github, repo, target.folder, head)
    except httpx.HTTPStatusError as exc:
        raise _github_error(state, user.login, exc) from exc
    return EntryPage(
        path=target.path,
        kind=target.kind,
        key=target.key,
        exists=content is not None,
        published=published,
        content=content or "",
        branch=branch,
        on_branch=on_branch,
        base=base,
        head=head,
        images=images,
        raw_base=f"https://raw.githubusercontent.com/{repo}/{head}/{target.folder}/",
        pull=pull,
    )


# -- writes -------------------------------------------------------------------------------------------------------


class Asset(BaseModel):
    name: str
    data: str
    """The file, base64."""


class SaveRequest(BaseModel):
    path: str
    content: str
    head: str
    """The `head` the editor loaded (or last saved): the branch must still point there."""
    message: str | None = None
    assets: list[Asset] = Field(default_factory=list)


class SaveResult(BaseModel):
    ok: bool
    action_id: int
    detail: str
    branch: str | None = None
    head: str | None = None
    commit_url: str | None = None


class PathRequest(BaseModel):
    path: str
    title: str | None = None
    """For publishing: the entry's title, which names the pull request."""


class PublishResult(BaseModel):
    ok: bool
    action_id: int
    detail: str
    pull: Pull | None = None


COMMIT = """
mutation($input: CreateCommitOnBranchInput!) {
  createCommitOnBranch(input: $input) { commit { oid url } }
}
"""


def _headline(message: str | None, fallback: str) -> str:
    line = (message or "").strip().splitlines()[0].strip() if (message or "").strip() else ""
    return line[:100] if line else fallback


def _files(request: SaveRequest, target: Entry) -> list[dict[str, str]]:
    """The commit's additions, base64: the Markdown (LF, one final newline) and the images beside it."""
    content = request.content.replace("\r\n", "\n").rstrip("\n") + "\n"
    markdown = content.encode()
    if len(markdown) > MARKDOWN_MAX:
        raise HTTPException(413, f"the Markdown is over {MARKDOWN_MAX // 1024} KiB")
    if len(request.assets) > ASSETS_MAX:
        raise HTTPException(413, f"at most {ASSETS_MAX} images per save")
    files = [{"path": target.path, "contents": base64.b64encode(markdown).decode()}]
    total = len(markdown)
    for asset in request.assets:
        if not ASSET.fullmatch(asset.name) or asset.name.lower() == "index.md":
            raise HTTPException(400, f"{asset.name!r}: images are png, jpg, gif or webp, named with a-z, 0-9, . _ -")
        try:
            data = base64.b64decode(asset.data, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(400, f"{asset.name}: not base64") from exc
        if len(data) > ASSET_MAX:
            raise HTTPException(413, f"{asset.name} is over {ASSET_MAX // (1024 * 1024)} MiB")
        total += len(data)
        files.append({"path": f"{target.folder}/{asset.name}", "contents": base64.b64encode(data).decode()})
    if total > SAVE_MAX:
        raise HTTPException(413, f"one save carries at most {SAVE_MAX // (1024 * 1024)} MiB")
    return files


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
        # createCommitOnBranch refuses a moved branch with "Expected branch to point to ..."
        ok, detail, status = False, str(exc), 409 if "expected branch" in str(exc).lower() else None
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
    """One commit on the member's edit branch, created from the commit they loaded when it does not exist yet
    (so a change made on the default branch meanwhile shows as a conflict in the PR, never silently lost)."""
    state = state_of(request)
    actor = await manage.require_member(request, request.headers.get("X-CSRF-Token"), editing=True)
    target = entry_of(body.path)
    files = _files(body, target)
    repo = repo_of(state)
    github = await _client(state, actor.login)
    branch = target.branch(actor.login)

    async def work() -> tuple[str, dict[str, str]]:
        head = await _head(github, repo, branch)
        if head is None:
            try:
                await github.post(f"/repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": body.head})
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 422:
                    raise HTTPException(409, "the commit you loaded is gone; reload the entry") from exc
                raise
            head = body.head
        elif head != body.head:
            raise HTTPException(409, f"{branch} moved since you loaded it (saved elsewhere?); reload to see it")
        new = await _file(github, repo, target.path, head) is None
        fallback = f"{'Add' if new else 'Update'} {target.label}"
        data = await github.graphql(
            COMMIT,
            {
                "input": {
                    "branch": {"repositoryNameWithOwner": repo, "branchName": branch},
                    "expectedHeadOid": head,
                    "message": {"headline": _headline(body.message, fallback)},
                    "fileChanges": {"additions": files},
                }
            },
        )
        commit = data["createCommitOnBranch"]["commit"]
        images = f" with {len(files) - 1} image{'s' if len(files) > 2 else ''}" if len(files) > 1 else ""
        return f"saved {target.key} to {branch}{images}", {"oid": str(commit["oid"]), "url": str(commit["url"])}

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
    """Open the pull request for the member's edit (or return the open one). Merging it publishes."""
    state = state_of(request)
    actor = await manage.require_member(request, request.headers.get("X-CSRF-Token"), editing=True)
    target = entry_of(body.path)
    repo = repo_of(state)
    github = await _client(state, actor.login)
    branch = target.branch(actor.login)

    async def work() -> tuple[str, Pull]:
        if await _head(github, repo, branch) is None:
            raise HTTPException(404, "nothing saved yet: save first")
        if (existing := await _open_pull(github, repo, branch)) is not None:
            return f"pull request #{existing.number} is already open", existing
        meta = await github.get_json(f"/repos/{repo}")
        base = str(meta.get("default_branch") or "main")
        new = await _file(github, repo, target.path, base) is None
        title = (body.title or "").strip().splitlines()[0][:80] if (body.title or "").strip() else target.key
        noun = "docs page" if target.kind == "doc" else "post"
        text = (
            f"{'A new' if new else 'An edit to the'} {noun} `{target.path}`, written in the website editor.\n\n"
            "Merging this pull request publishes it; the site's checks build it first. "
            "Further saves in the editor add commits here."
        )
        raw = (
            await github.post(
                f"/repos/{repo}/pulls",
                {
                    "title": f'{"Add" if new else "Update"} the {noun} "{title}"',
                    "head": branch,
                    "base": base,
                    "body": text,
                },
            )
        ).json()
        pull = _pull(raw, actor.login)
        return f"opened pull request #{pull.number} for {target.key}", pull

    done = await _logged(state, actor.login, "edit-publish", target.key, work())
    return _answer(
        state, actor, PublishResult(ok=done.ok, action_id=done.action_id, detail=done.detail, pull=done.value)
    )


@router.post("/api/editor/discard")
async def discard(request: Request, body: PathRequest) -> JSONResponse:
    """Throw an edit away: close its pull request and delete the branch. The default branch is untouched."""
    state = state_of(request)
    actor = await manage.require_member(request, request.headers.get("X-CSRF-Token"), editing=True)
    target = entry_of(body.path)
    repo = repo_of(state)
    github = await _client(state, actor.login)
    branch = target.branch(actor.login)

    async def work() -> tuple[str, None]:
        if await _head(github, repo, branch) is None:
            raise HTTPException(404, f"no edit of {target.key} to discard")
        closed = ""
        if (pull := await _open_pull(github, repo, branch)) is not None:
            await github.patch(f"/repos/{repo}/pulls/{pull.number}", {"state": "closed"})
            closed = f", closed #{pull.number}"
        await github.delete(f"/repos/{repo}/git/refs/heads/{quote(branch)}")
        return f"discarded the edit of {target.key}{closed}", None

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
