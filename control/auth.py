"""
Member sign-in. Preferably with a classic OAuth App (OAUTH_CLIENT_ID/SECRET, scopes read:user read:org):
its consent screen lists those two scopes, and the user's token is used for two calls, `GET /user` and
`GET /user/memberships/orgs/{org}`, then dropped. Without an OAuth App the GitHub App's own OAuth client is
used and membership is read with the installation token (`GET /orgs/{org}/memberships/{login}`, which needs
the App's organisation permission Members: read). Either way nothing of the user's is stored (the website
editor, editor.py, keeps a token only for members who authorise it separately, and sign-out revokes it). What we
keep is a signed cookie: login, avatar, the member verdict and when it was reached (re-checked hourly),
and a CSRF token every management form carries. The secret comes from CONTROL_SESSION_SECRET or is random
per process, in which case everyone is signed out by a restart.

The website on another origin (CONTROL_ALLOWED_ORIGINS) uses the same cookie: it is issued `SameSite=None;
Secure` whenever an external origin is allowed, so credentialed cross-origin fetches carry it, `/api/me`
tells the site who is signed in (and hands over the CSRF token, which JSON actions send back as
`X-CSRF-Token`), and `/login?next=<allowed origin url>` returns there after GitHub.
"""

import base64
import datetime
import hashlib
import hmac
import json
import logging
import secrets
from collections.abc import Sequence
from typing import Any, Literal
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from .github import installation_client
from .state import State, state_of

log = logging.getLogger(__name__)

router = APIRouter()

SESSION_COOKIE = "control_session"
OAUTH_COOKIE = "control_oauth"
SESSION_TTL = datetime.timedelta(days=7)
OAUTH_TTL = datetime.timedelta(minutes=10)
MEMBER_RECHECK = datetime.timedelta(hours=1)
GITHUB = "https://github.com"
API = "https://api.github.com"


class User(BaseModel):
    login: str
    avatar_url: str | None = None
    member: bool = False
    checked_at: str
    """When the membership verdict was last taken from GitHub."""
    csrf: str
    issued_at: str

    def stale(self, now: datetime.datetime | None = None) -> bool:
        at = now or datetime.datetime.now(datetime.UTC)
        return at - datetime.datetime.fromisoformat(self.checked_at) >= MEMBER_RECHECK


# -- signing ------------------------------------------------------------------------------------------------------


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(secret: str, payload: dict[str, Any]) -> str:
    """`<base64url json>.<hex hmac-sha256>`; the payload carries its own issued_at for expiry."""
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    mac = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{mac}"


def unsign(secret: str, token: str | None, max_age: datetime.timedelta) -> dict[str, Any] | None:
    """The payload when the signature holds and `issued_at` is within `max_age`; None for anything else."""
    if not token or "." not in token:
        return None
    body, _, mac = token.rpartition(".")
    expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, mac):
        return None
    try:
        payload = json.loads(_unb64(body))
        issued = datetime.datetime.fromisoformat(str(payload["issued_at"]))
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(payload, dict) or datetime.datetime.now(datetime.UTC) - issued > max_age:
        return None
    return payload


# -- sessions -----------------------------------------------------------------------------------------------------


def current_user(request: Request) -> User | None:
    state = state_of(request)
    payload = unsign(state.session_secret, request.cookies.get(SESSION_COOKIE), SESSION_TTL)
    if payload is None:
        return None
    try:
        return User.model_validate(payload)
    except ValueError:
        return None


def current_member(request: Request) -> str | None:
    """The login of a signed-in org member, else None; the read-only pages use it to show the forms."""
    user = current_user(request)
    return user.login if user and user.member else None


def _secure(state: State) -> bool:
    return state.settings.public_url.startswith("https://")


def session_samesite(state: State) -> Literal["lax", "none"]:
    """`None` so the cookie rides on credentialed fetches from an allowed origin (browsers accept `SameSite=None`
    only with `Secure`, so plain http stays `Lax`); `Lax` when no external origin may drive the API."""
    return "none" if state.settings.allowed_origins and _secure(state) else "lax"


def set_session(response: Response, state: State, user: User) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        sign(state.session_secret, user.model_dump()),
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True,
        secure=_secure(state),
        samesite=session_samesite(state),
        path="/",
    )


def clear_session(response: Response, state: State) -> None:
    """The clearing Set-Cookie carries the same attributes: a cross-site response may only set `SameSite=None`."""
    response.delete_cookie(
        SESSION_COOKIE, path="/", httponly=True, secure=_secure(state), samesite=session_samesite(state)
    )


def new_user(login: str, avatar_url: str | None, member: bool) -> User:
    now = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    return User(
        login=login, avatar_url=avatar_url, member=member, checked_at=now, csrf=secrets.token_urlsafe(24), issued_at=now
    )


class OAuthClient(BaseModel):
    client_id: str
    client_secret: str
    scopes: str | None
    """Scopes to request. A classic OAuth App needs them; a GitHub App ignores them."""
    user_membership: bool
    """True when membership is read with the user's token (classic OAuth App), False for the installation token."""


def oauth_client(state: State) -> OAuthClient | None:
    settings = state.settings
    if settings.oauth_client_id and settings.oauth_client_secret:
        return OAuthClient(
            client_id=settings.oauth_client_id,
            client_secret=settings.oauth_client_secret,
            scopes="read:user read:org",
            user_membership=True,
        )
    credentials = state.credentials.load()
    if credentials is None:
        return None
    return OAuthClient(
        client_id=credentials.client_id, client_secret=credentials.client_secret, scopes=None, user_membership=False
    )


def _active(response: httpx.Response) -> bool:
    if response.status_code != 200:
        return False
    body = response.json()
    return isinstance(body, dict) and body.get("state") == "active"


async def is_member_via_user(state: State, access_token: str) -> bool:
    """Active membership read with the signed-in user's token (scope read:org). Any failure means "no"."""
    try:
        response = await state.http.get(
            f"{API}/user/memberships/orgs/{state.settings.github_org}",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"},
        )
    except httpx.HTTPError:
        log.exception("user membership check failed")
        return False
    return _active(response)


async def installation_membership(state: State, login: str) -> bool | None:
    """Active membership read with the installation token; None when the check is unavailable (app not
    configured or installed, no Members permission, network error) rather than a definite no."""
    try:
        github = await installation_client(state)
        if github is None:
            log.warning("cannot check membership of %s: GitHub App not configured or not installed", login)
            return None
        response = await github.http.get(
            f"{API}/orgs/{state.settings.github_org}/memberships/{login}", headers=github.headers
        )
    except httpx.HTTPError:
        log.exception("membership check for %s failed", login)
        return None
    if response.status_code == 403:
        log.warning("membership check unavailable: the App lacks the organisation permission Members: read")
        return None
    return _active(response)


async def is_member(state: State, login: str) -> bool:
    """Active membership of the org, read with the installation token; unavailable counts as no."""
    return bool(await installation_membership(state, login))


async def refresh_if_stale(state: State, user: User) -> tuple[User, bool]:
    """(user, changed): the membership verdict re-taken once it is an hour old. When the installation token
    cannot answer (classic OAuth App sign-in without the Members permission) the verdict taken at sign-in is
    kept for the life of the session, which is capped by SESSION_TTL."""
    if not user.stale():
        return user, False
    verdict = await installation_membership(state, user.login)
    now = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    member = user.member if verdict is None else verdict
    return user.model_copy(update={"member": member, "checked_at": now}), True


def safe_next(value: str | None, origins: Sequence[str] = ()) -> str:
    """Local paths, or an absolute URL on an allowed origin (the website), come back from `next`; anything else
    is `/`, so the login flow cannot be used to bounce people elsewhere."""
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    if value and origins:
        parts = urlsplit(value)
        if parts.scheme and parts.netloc and f"{parts.scheme}://{parts.netloc}".lower() in origins:
            return value
    return "/"


def csrf_ok(user: User, token: str | None) -> bool:
    return secrets.compare_digest(user.csrf, token or "")


class Me(BaseModel):
    """Who is looking, for the website: never a 401, an anonymous visitor is `authenticated: false`. The CSRF
    token is here because the cookie is HttpOnly and JSON actions must send it back as `X-CSRF-Token`."""

    authenticated: bool
    login: str | None = None
    avatar_url: str | None = None
    member: bool = False
    checked_at: str | None = None
    csrf: str | None = None


# -- routes -------------------------------------------------------------------------------------------------------


@router.get("/login")
def login(request: Request, next: str | None = None) -> RedirectResponse:
    state = state_of(request)
    oauth = oauth_client(state)
    if oauth is None:
        raise HTTPException(503, "sign-in not configured: set OAUTH_CLIENT_ID/SECRET or bootstrap the GitHub App")
    token = secrets.token_urlsafe(24)
    issued = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    params = {
        "client_id": oauth.client_id,
        "redirect_uri": f"{state.settings.public_url}/login/callback",
        "state": token,
    }
    if oauth.scopes:
        params["scope"] = oauth.scopes
    query = urlencode(params)
    response = RedirectResponse(f"{GITHUB}/login/oauth/authorize?{query}", status_code=303)
    # Lax is right here: GitHub brings the browser back by a top-level navigation, which sends Lax cookies.
    pending = {"state": token, "next": safe_next(next, state.settings.allowed_origins), "issued_at": issued}
    response.set_cookie(
        OAUTH_COOKIE,
        sign(state.session_secret, pending),
        max_age=int(OAUTH_TTL.total_seconds()),
        httponly=True,
        secure=_secure(state),
        samesite="lax",
        path="/login",
    )
    return response


@router.get("/login/callback")
async def login_callback(request: Request, code: str, state: str) -> RedirectResponse:
    app_state = state_of(request)
    oauth = oauth_client(app_state)
    if oauth is None:
        raise HTTPException(503, "sign-in not configured")
    pending = unsign(app_state.session_secret, request.cookies.get(OAUTH_COOKIE), OAUTH_TTL)
    if pending is None or not secrets.compare_digest(str(pending.get("state", "")), state):
        raise HTTPException(400, "unknown or expired sign-in state; start again at /login")
    if pending.get("purpose") == "edit":
        # The website editor's authorisation shares the App's one registered callback (editor.py).
        from . import editor

        return await editor.finish(request, pending, code)
    try:
        exchange = await app_state.http.post(
            f"{GITHUB}/login/oauth/access_token",
            data={
                "client_id": oauth.client_id,
                "client_secret": oauth.client_secret,
                "code": code,
                "redirect_uri": f"{app_state.settings.public_url}/login/callback",
            },
            headers={"Accept": "application/json"},
        )
        exchange.raise_for_status()
        access_token = exchange.json().get("access_token")
        if not access_token:
            raise HTTPException(502, f"GitHub did not return a token: {exchange.json().get('error', 'unknown')}")
        who = await app_state.http.get(
            f"{API}/user",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"},
        )
        who.raise_for_status()
    except httpx.HTTPError as exc:
        log.exception("OAuth exchange failed")
        raise HTTPException(502, f"GitHub rejected the sign-in: {exc}") from exc
    profile = who.json()
    login = str(profile["login"])
    if oauth.user_membership:
        member = await is_member_via_user(app_state, access_token)
    else:
        member = await is_member(app_state, login)
    user = new_user(login, profile.get("avatar_url"), member)
    log.info("%s signed in (member: %s)", login, user.member)
    target = safe_next(str(pending.get("next") or "/"), app_state.settings.allowed_origins)
    response = RedirectResponse(target, status_code=303)
    set_session(response, app_state, user)
    response.delete_cookie(OAUTH_COOKIE, path="/login")
    return response


@router.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    """POST with the CSRF token, like every other action; an unsigned logout is still only a redirect."""
    state = state_of(request)
    form = await request.form()
    user = current_user(request)
    target = safe_next(str(form.get("next") or "/"), state.settings.allowed_origins)
    response = RedirectResponse(target, status_code=303)
    if user and csrf_ok(user, str(form.get("csrf") or "")):
        clear_session(response, state)
        await _forget_editor(state, user.login)
    return response


async def _forget_editor(state: State, login: str) -> None:
    """Signing out also revokes the website editor's token, the one thing of the user's control keeps."""
    from . import editor

    await editor.revoke(state, login)


@router.get("/api/me")
async def me(request: Request) -> Response:
    """The website's sign-in state. Re-checks a stale membership like /management does, and rewrites the cookie."""
    state = state_of(request)
    user = current_user(request)
    if user is None:
        return Response(Me(authenticated=False).model_dump_json(), media_type="application/json")
    user, changed = await refresh_if_stale(state, user)
    body = Me(
        authenticated=True,
        login=user.login,
        avatar_url=user.avatar_url,
        member=user.member,
        checked_at=user.checked_at,
        csrf=user.csrf,
    )
    response = Response(body.model_dump_json(), media_type="application/json")
    if changed:
        set_session(response, state, user)
    return response


@router.post("/api/logout", status_code=204)
async def logout_json(request: Request) -> Response:
    """Sign-out for the website: the CSRF token from `/api/me` as `X-CSRF-Token`; 401 anonymous, 403 bad token."""
    state = state_of(request)
    user = current_user(request)
    if user is None:
        raise HTTPException(401, "not signed in")
    if not csrf_ok(user, request.headers.get("X-CSRF-Token")):
        raise HTTPException(403, "bad CSRF token; read it again from /api/me")
    response = Response(status_code=204)
    clear_session(response, state)
    await _forget_editor(state, user.login)
    return response
