"""
Bootstrapping the GitHub App through the manifest flow:

1. GET /setup renders a form that POSTs our manifest to GitHub's "new app from manifest" page.
2. GitHub creates the app and redirects the browser to /setup/callback?code=...&state=...
3. We convert the code into credentials, store them, and send the browser on to install the app.
4. GitHub redirects back to /setup/installed?installation_id=... when that is done.

`state` is a random token that must come back unchanged, so a stranger cannot hand us a code for an app
of their own.
"""

import datetime
import json
import logging
import secrets

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .github_app import convert, manifest
from .state import INSTALLATION_ID, State, state_of
from .templating import render

log = logging.getLogger(__name__)

router = APIRouter()

SETUP_STATE = "setup_state"
"""kv key: "<token> <expires_at iso>" while a manifest flow is in progress."""
STATE_TTL = datetime.timedelta(minutes=15)


def require_setup_token(state: State, token: str | None) -> None:
    expected = state.settings.setup_token
    if expected and not (token and secrets.compare_digest(expected, token)):
        raise HTTPException(403, "setup token required")


def new_state_token(state: State) -> str:
    token = secrets.token_urlsafe(32)
    expires = datetime.datetime.now(datetime.UTC) + STATE_TTL
    state.db.set(SETUP_STATE, f"{token} {expires.isoformat()}")
    return token


def consume_state_token(state: State, token: str) -> bool:
    """True once per issued token, and only before it expires."""
    stored = state.db.get(SETUP_STATE)
    if not stored:
        return False
    expected, _, expires = stored.partition(" ")
    state.db.delete(SETUP_STATE)
    if datetime.datetime.fromisoformat(expires) < datetime.datetime.now(datetime.UTC):
        return False
    return secrets.compare_digest(expected, token)


@router.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request, token: str | None = None) -> HTMLResponse:
    state = state_of(request)
    require_setup_token(state, token)
    credentials = state.credentials.load()
    if credentials:
        return HTMLResponse(
            render(
                "setup_done.html",
                settings=state.settings,
                credentials=credentials,
                installation_id=state.installation_id,
            )
        )
    return HTMLResponse(
        render(
            "setup.html",
            settings=state.settings,
            action=f"https://github.com/organizations/{state.settings.github_org}/settings/apps/new"
            f"?state={new_state_token(state)}",
            manifest=json.dumps(manifest(state.settings)),
        )
    )


@router.get("/setup/callback")
async def setup_callback(request: Request, code: str, state: str) -> RedirectResponse:
    app_state = state_of(request)
    if app_state.credentials.load():
        raise HTTPException(409, "GitHub App already configured")
    if not consume_state_token(app_state, state):
        raise HTTPException(400, "unknown or expired state; start again at /setup")
    try:
        credentials = await convert(code, app_state.http)
    except httpx.HTTPError as exc:
        log.exception("manifest conversion failed")
        raise HTTPException(502, f"GitHub rejected the conversion: {exc}") from exc
    app_state.credentials.save(credentials)
    log.info("GitHub App %s (id %s) created", credentials.slug, credentials.id)
    await app_state.discord.post(f"GitHub App **{credentials.slug}** created: {credentials.html_url}")
    return RedirectResponse(credentials.install_url, status_code=303)


@router.get("/setup/installed")
async def setup_installed(
    request: Request, installation_id: int | None = None, setup_action: str | None = None
) -> RedirectResponse:
    state = state_of(request)
    credentials = state.credentials.load()
    # Only GitHub sends us here, but the URL is public: without an app there is nothing to install.
    if credentials and installation_id is not None:
        state.db.set(INSTALLATION_ID, str(installation_id))
        log.info("installation %s (%s)", installation_id, setup_action)
        await state.discord.post(f"GitHub App **{credentials.slug}** installed on **{state.settings.github_org}**")
    return RedirectResponse("/", status_code=303)
