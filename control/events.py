"""
The `/events` endpoint the shared workflow POSTs to (CONVENTIONS §10): a control-event signed with
HMAC-SHA256 over the raw body using the per-org `CONTROL_EVENTS_SECRET`, validated against the `contracts`
schema, stored in `deliveries` as `control:<type>` so it replays like a webhook, then applied and
announced on Discord. A live build gets one message, posted on `build.started` and edited into the
summary; dev and pr builds only get the summary.
"""

import hashlib
import json
import logging
from typing import Annotated, Any

from contracts import errors as contract_errors
from contracts import format_error
from fastapi import APIRouter, Header, HTTPException, Request

from . import handlers
from .model import Build
from .state import State, state_of
from .webhooks import signature_valid

log = logging.getLogger(__name__)

router = APIRouter()

GREEN = 0x3FB950
RED = 0xF85149
GREY = 0x8B949E
AMBER = 0xD29922


def validate_event(payload: Any) -> list[str]:
    """Schema violations as `path: message` strings; empty means valid."""
    return [format_error(error) for error in contract_errors("control-event", payload)]


def embed_color(build: Build) -> int:
    if build.running:
        return AMBER
    if build.result == "success":
        return GREEN if build.mode == "live" else GREY
    if build.result == "failure":
        return RED
    return GREY


def platforms_field(build: Build) -> str:
    """`amd64, arm64` when every arch succeeded, else each arch with its own outcome."""
    if not build.platforms:
        return "—"
    if all(p.result in (None, "success") for p in build.platforms):
        return ", ".join(build.archs)
    return ", ".join(f"{p.arch} ({p.result or 'unknown'})" for p in build.platforms)


def build_embed(build: Build, public_url: str, hub_url: str | None = None) -> dict[str, Any]:
    """One embed per build; the same shape for the provisional and the final message so an edit reads well."""
    links = []
    # The hub renders the CI results page from the release assets (CONVENTIONS §10); only live builds have one.
    if hub_url and build.mode == "live" and build.version_tag and build.image:
        links.append(f"[results]({hub_url}/builds/{build.image}/{build.version_tag}/)")
    if build.run_url:
        links.append(f"[run]({build.run_url})")
    if build.release_url:
        links.append(f"[release]({build.release_url})")
    if build.artifact_url:
        links.append(f"[artifact]({build.artifact_url})")
    links.append(f"[control]({public_url}/builds/{build.repo}/{build.run_id})")
    result = "running" if build.running else str(build.result)
    if build.failed_job:
        result += f" ({build.failed_job})"
    if build.test_success is None:
        test = "not run" if not build.running else "pending"
    else:
        test = "passed" if build.test_success else "failed"
    title_tag = build.version_tag or build.branch or ""
    fields = [
        {"name": "mode", "value": build.mode or "?", "inline": True},
        {"name": "reason", "value": build.reason or "?", "inline": True},
        {"name": "result", "value": result, "inline": True},
        {"name": "platforms", "value": platforms_field(build), "inline": True},
        {"name": "test", "value": test, "inline": True},
        {"name": "links", "value": " · ".join(links), "inline": False},
    ]
    if build.pull_request:
        fields.insert(1, {"name": "pull request", "value": f"#{build.pull_request}", "inline": True})
    return {
        "title": f"{build.image} {title_tag}".strip(),
        "url": build.run_url,
        "color": embed_color(build),
        "fields": fields,
        "footer": {"text": build.repo},
        "timestamp": build.updated_at,
    }


async def announce(state: State, build: Build, event_type: str) -> None:
    """Post or edit the Discord message for a build. Never raises; Discord failures are logged by the client."""
    if not state.discord.configured:
        return
    if event_type == "build.started" and build.mode != "live":
        return
    embed = build_embed(build, state.settings.public_url, state.settings.hub_url)
    if build.discord_message_id and await state.discord.edit(build.discord_message_id, embeds=[embed]):
        return
    message_id = await state.discord.post(embeds=[embed])
    if message_id:
        state.model.set_build_message(build.repo, build.run_id, message_id)


@router.post("/events", status_code=202)
async def control_event(
    request: Request,
    x_control_signature: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    state = state_of(request)
    secret = state.settings.events_secret
    if not secret:
        raise HTTPException(503, "CONTROL_EVENTS_SECRET not set; /events is disabled")
    body = await request.body()
    if not signature_valid(secret, body, x_control_signature):
        raise HTTPException(401, "bad signature")
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise HTTPException(400, "body is not JSON") from exc
    problems = validate_event(payload)
    if problems:
        raise HTTPException(400, {"message": "event does not match control-event.schema.json", "errors": problems})

    event_type = str(payload["type"])
    # The body is its own identity: the workflow retrying a POST must not produce a second row or message.
    delivery_id = "control-" + hashlib.sha256(body).hexdigest()[:40]
    stored = state.db.insert_delivery(delivery_id, handlers.CONTROL_EVENT_PREFIX + event_type, payload)
    if not stored:
        log.info("duplicate control event %s ignored", delivery_id)
        return {"status": "duplicate", "type": event_type}
    applied = handlers.process(state.model, state.stream, handlers.CONTROL_EVENT_PREFIX + event_type, payload)
    build = state.model.build(str(payload["repository"]), int(payload["run_id"]))
    if applied and build is not None:
        await announce(state, build, event_type)
    log.info("%s for %s run %s (%s)", event_type, payload["repository"], payload["run_id"], payload.get("result", ""))
    return {"status": "accepted", "type": event_type, "applied": applied}
