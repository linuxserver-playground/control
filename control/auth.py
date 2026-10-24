"""
Member sign-in with the GitHub App's OAuth client. The user's access token is used for exactly one call
(`GET /user`, to learn who they are) and then dropped: membership of the org is checked with our own
installation token (`GET /orgs/{org}/memberships/{login}`), so nothing of the user's is stored. What we
keep is a signed cookie: login, avatar, the member verdict and when it was reached (re-checked hourly),
and a CSRF token every management form carries. The secret comes from CONTROL_SESSION_SECRET or is random
per process, in which case everyone is signed out by a restart.
"""

import base64
import datetime
import hashlib
import hmac
import json
import logging
import secrets
from typing import Any
from urllib.parse import urlencode

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


def set_session(response: Response, state: State, user: User) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        sign(state.session_secret, user.model_dump()),
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True,
        secure=_secure(state),
        samesite="lax",
        path="/",
    )


def clear_session(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def new_user(login: str, avatar_url: str | None, member: bool) -> User:
    now = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    return User(
        login=login, avatar_url=avatar_url, member=member, checked_at=now, csrf=secrets.token_urlsafe(24), issued_at=now
    )


async def is_member(state: State, login: str) -> bool:
    """Active membership of the org, read with the installation token. Not configured, not installed, or any
    GitHub error all mean "no": a member can sign in again once the app works."""
    try:
        github = await installation_client(state)
        if github is None:
            log.warning("cannot check membership of %s: GitHub App not configured or not installed", login)
            return False
        response = await github.http.get(
            f"{API}/orgs/{state.settings.github_org}/memberships/{login}", headers=github.headers
        )
    except httpx.HTTPError:
        log.exception("membership check for %s failed", login)
        return False
    if response.status_code != 200:
        return False
    body = response.json()
    return isinstance(body, dict) and body.get("state") == "active"


async def refresh_if_stale(state: State, user: User) -> tuple[User, bool]:
    """(user, changed): the membership verdict re-taken once it is an hour old."""
    if not user.stale():
        return user, False
    member = await is_member(state, user.login)
    now = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    return user.model_copy(update={"member": member, "checked_at": now}), True


def safe_next(value: str | None) -> str:
    """Only local paths come back from `next`, so the login flow cannot be used to bounce people elsewhere."""
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return "/"


# -- routes -------------------------------------------------------------------------------------------------------


@router.get("/login")
def login(request: Request, next: str | None = None) -> RedirectResponse:
    state = state_of(request)
    credentials = state.credentials.load()
    if credentials is None:
        raise HTTPException(503, "GitHub App not configured yet; sign-in needs its OAuth client")
    token = secrets.token_urlsafe(24)
    issued = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    query = urlencode(
        {
            "client_id": credentials.client_id,
            "redirect_uri": f"{state.settings.public_url}/login/callback",
            "state": token,
        }
    )
    response = RedirectResponse(f"{GITHUB}/login/oauth/authorize?{query}", status_code=303)
    response.set_cookie(
        OAUTH_COOKIE,
        sign(state.session_secret, {"state": token, "next": safe_next(next), "issued_at": issued}),
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
    credentials = app_state.credentials.load()
    if credentials is None:
        raise HTTPException(503, "GitHub App not configured yet")
    pending = unsign(app_state.session_secret, request.cookies.get(OAUTH_COOKIE), OAUTH_TTL)
    if pending is None or not secrets.compare_digest(str(pending.get("state", "")), state):
        raise HTTPException(400, "unknown or expired sign-in state; start again at /login")
    try:
        exchange = await app_state.http.post(
            f"{GITHUB}/login/oauth/access_token",
            data={
                "client_id": credentials.client_id,
                "client_secret": credentials.client_secret,
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
    user = new_user(login, profile.get("avatar_url"), await is_member(app_state, login))
    log.info("%s signed in (member: %s)", login, user.member)
    response = RedirectResponse(safe_next(str(pending.get("next") or "/")), status_code=303)
    set_session(response, app_state, user)
    response.delete_cookie(OAUTH_COOKIE, path="/login")
    return response


@router.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    """POST with the CSRF token, like every other action; an unsigned logout is still only a redirect."""
    form = await request.form()
    user = current_user(request)
    response = RedirectResponse(safe_next(str(form.get("next") or "/")), status_code=303)
    if user and secrets.compare_digest(user.csrf, str(form.get("csrf") or "")):
        clear_session(response)
    return response
