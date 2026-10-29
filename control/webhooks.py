"""
Webhook intake. Every delivery is verified against the app's webhook secret and stored whole before any
handler sees it, so a handler bug loses nothing: `python -m control replay` reprocesses the table. GitHub
retries on non-2xx, so we only answer 4xx/5xx when storing would be wrong (bad signature, no secret yet).
"""

import hashlib
import hmac
import json
import logging
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Request, Response

from . import archive, fleet, handlers, heal, mods, relay
from .state import INSTALLATION_ID, state_of

log = logging.getLogger(__name__)

router = APIRouter()


def signature_valid(secret: str, body: bytes, header: str | None) -> bool:
    """X-Hub-Signature-256 is "sha256=<hex hmac>" of the raw body; compare in constant time."""
    if not header:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


@router.post("/webhooks/github", status_code=202)
async def github_webhook(
    request: Request,
    x_github_event: Annotated[str, Header()],
    x_github_delivery: Annotated[str, Header()],
    x_hub_signature_256: Annotated[str | None, Header()] = None,
) -> Response:
    state = state_of(request)
    credentials = state.credentials.load()
    if credentials is None:
        raise HTTPException(503, "GitHub App not configured yet")
    body = await request.body()
    if not signature_valid(credentials.webhook_secret, body, x_hub_signature_256):
        raise HTTPException(401, "bad signature")
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise HTTPException(400, "body is not JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "body is not a JSON object")

    stored = state.db.insert_delivery(x_github_delivery, x_github_event, payload)
    if not stored:
        log.info("duplicate delivery %s ignored", x_github_delivery)
        return Response(status_code=202)

    # Both `installation` and `installation_repositories` carry the installation; "deleted" means it is gone.
    if x_github_event in ("installation", "installation_repositories"):
        installation = payload.get("installation") or {}
        if payload.get("action") == "deleted":
            state.db.delete(INSTALLATION_ID)
        elif isinstance(installation.get("id"), int):
            state.db.set(INSTALLATION_ID, str(installation["id"]))
    log.info("stored %s %s (%s)", x_github_event, payload.get("action", ""), x_github_delivery)
    handlers.process(state.model, state.stream, x_github_event, payload)
    if x_github_event == "push":
        fleet.schedule_refresh(state, payload)  # re-reads image.yml for a live branch, in the background
        fleet.forget_deleted_branch(state, payload)  # a deleted helper repo branch leaves the fleet board
        mods.schedule_refresh(state, payload)  # re-reads a docker-mods branch's mod.yml when the push touched it
    elif x_github_event == "workflow_run":
        relay.schedule_site_refresh(state, payload)  # a successful hub Deploy makes the website refresh
        archive.wake(state, payload)  # a completed released run gets its record and logs onto its Release
        heal.schedule_heal(state, payload)  # a completed run's jobs whose own webhook never came
        from .events import schedule_pr_report_refresh  # events imports this module

        schedule_pr_report_refresh(state, payload)  # Discord links the report comment a PR report run wrote
        from .scheduler import schedule_mod_followup  # scheduler imports events, which imports this module

        schedule_mod_followup(state, payload)  # a mod with version lines builds its next missing line at once
    return Response(status_code=202)
