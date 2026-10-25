"""
The `/events` endpoint the shared workflow POSTs to (CONVENTIONS §10): a control-event signed with
HMAC-SHA256 over the raw body using the per-org `CONTROL_EVENTS_SECRET`, validated against the `contracts`
schema, stored in `deliveries` as `control:<type>` so it replays like a webhook, then applied and
announced on Discord. A live build gets one message, posted on `build.started` and edited into the
summary; dev and pr builds only get the summary. The embeds link to the website, the single pane of glass.
"""

import datetime
import hashlib
import json
import logging
from typing import Annotated, Any

from contracts import errors as contract_errors
from contracts import format_error
from fastapi import APIRouter, Header, HTTPException, Request

from . import handlers
from .config import Settings
from .github import installation_client
from .model import Build, Model, Run
from .state import State, state_of
from .webhooks import signature_valid

log = logging.getLogger(__name__)

router = APIRouter()

GREEN = 0x3FB950
RED = 0xF85149
GREY = 0x8B949E
AMBER = 0xD29922
DOT = {GREEN: "\U0001f7e2", RED: "\U0001f534", AMBER: "\U0001f7e1", GREY: "\u26aa"}
"""The colour as a word: a circle emoji in the embed's own colour in front of the result word."""
RESULT_WORD = {"success": "passed", "failure": "failed", "skipped": "skipped", "cancelled": "cancelled"}

# Discord's embed limits (title, description, field name, field value, footer, author name, fields, total).
TITLE_LIMIT = 256
DESCRIPTION_LIMIT = 4096
FIELD_NAME_LIMIT = 256
FIELD_VALUE_LIMIT = 1024
FOOTER_LIMIT = 2048
AUTHOR_LIMIT = 256
FIELDS_LIMIT = 25
EMBED_LIMIT = 6000
ELLIPSIS = "\u2026"


def validate_event(payload: Any) -> list[str]:
    """Schema violations as `path: message` strings; empty means valid."""
    return [format_error(error) for error in contract_errors("control-event", payload)]


def clip(text: str, limit: int) -> str:
    """`text` cut to `limit` characters with a trailing ellipsis; Discord rejects the whole message otherwise."""
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)] + ELLIPSIS


def within_limits(embed: dict[str, Any]) -> dict[str, Any]:
    """Every part of an embed clipped to its limit, then the description again when the total still exceeds
    the 6000-character message limit; only the first 25 fields are kept."""
    if "title" in embed:
        embed["title"] = clip(str(embed["title"]), TITLE_LIMIT)
    if "description" in embed:
        embed["description"] = clip(str(embed["description"]), DESCRIPTION_LIMIT)
    if "footer" in embed:
        embed["footer"]["text"] = clip(str(embed["footer"]["text"]), FOOTER_LIMIT)
    if "author" in embed:
        embed["author"]["name"] = clip(str(embed["author"]["name"]), AUTHOR_LIMIT)
    fields = [
        {**f, "name": clip(str(f["name"]), FIELD_NAME_LIMIT), "value": clip(str(f["value"]), FIELD_VALUE_LIMIT)}
        for f in embed.get("fields", [])[:FIELDS_LIMIT]
    ]
    if fields:
        embed["fields"] = fields
    excess = embed_length(embed) - EMBED_LIMIT
    if excess > 0 and "description" in embed:
        embed["description"] = clip(str(embed["description"]), max(len(str(embed["description"])) - excess, 1))
    # Still over (many long fields): shorten the longest field value, then drop fields from the end.
    while (excess := embed_length(embed) - EMBED_LIMIT) > 0 and embed.get("fields"):
        longest = max(embed["fields"], key=lambda f: len(str(f["value"])))
        if len(str(longest["value"])) <= 1:
            embed["fields"].pop()
            continue
        longest["value"] = clip(str(longest["value"]), max(len(str(longest["value"])) - excess, 1))
    return embed


def embed_length(embed: dict[str, Any]) -> int:
    total = len(str(embed.get("title", ""))) + len(str(embed.get("description", "")))
    total += len(str(embed.get("footer", {}).get("text", ""))) + len(str(embed.get("author", {}).get("name", "")))
    return total + sum(len(str(f["name"])) + len(str(f["value"])) for f in embed.get("fields", []))


def two_columns(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Discord lays inline fields three to a row; a blank third field after every pair keeps two columns."""
    laid: list[dict[str, Any]] = []
    for index, field in enumerate(fields):
        if index and index % 2 == 0:
            laid.append({"name": "\u200b", "value": "\u200b", "inline": True})
        laid.append({**field, "inline": True})
    return laid


def embed_color(build: Build) -> int:
    if build.running:
        return AMBER
    if build.result == "success":
        return GREEN if build.mode == "live" else GREY
    if build.result == "failure":
        return RED
    return GREY


def result_word(build: Build) -> str:
    return "running" if build.running else RESULT_WORD.get(str(build.result), str(build.result))


def mib(size: Any) -> str | None:
    try:
        return f"{int(size) / (1024 * 1024):.1f} MiB"
    except (TypeError, ValueError):
        return None


def seconds(value: Any) -> str | None:
    try:
        total = round(float(value))
    except (TypeError, ValueError):
        return None
    minutes, secs = divmod(total, 60)
    return f"{minutes}m{secs:02d}s" if minutes else f"{secs}s"


def clock(timestamp: str | None) -> str | None:
    """`12:09` from an ISO timestamp (UTC, as every timestamp in the model is)."""
    if not timestamp:
        return None
    try:
        return datetime.datetime.fromisoformat(timestamp.replace("Z", "+00:00")).strftime("%H:%M")
    except ValueError:
        return None


def site_image_url(site_url: str, image: str) -> str:
    return f"{site_url}/images/{image}/"


def site_build_url(site_url: str, image: str, tag: str) -> str:
    return f"{site_url}/images/{image}/builds/{tag}/"


def site_scheduler_url(site_url: str, image: str) -> str:
    return f"{site_url}/fleet/scheduler/?image={image}"


def control_build_url(public_url: str, build: Build) -> str:
    return f"{public_url}/builds/{build.repo}/{build.run_id}"


def is_package_check(build: Build) -> bool:
    """A live `reason: packages` run compares package tables and never releases (the follow-up build does)."""
    return build.mode == "live" and build.reason == "packages" and not build.release_url


def packages_changed(bundle: dict[str, Any], previous: Build | None) -> str | None:
    """`packages changed (amd64 +3, arm64 +3)` against the previous release's bundle, `packages unchanged`
    when every arch hashes the same, None without both summaries. The summary carries counts and a table
    hash per arch, so the delta is in package count, not a list of upgrades (the hub's diff has those)."""
    current = bundle.get("packages")
    before = previous.bundle.get("packages") if previous and previous.bundle else None
    if not isinstance(current, dict) or not isinstance(before, dict):
        return None
    deltas: list[str] = []
    changed = False
    for arch in sorted(current):
        now, then = current.get(arch), before.get(arch)
        if not isinstance(now, dict) or not isinstance(then, dict):
            continue
        if now.get("hash") != then.get("hash"):
            changed = True
            try:
                delta = int(now.get("count", 0)) - int(then.get("count", 0))
            except (TypeError, ValueError):
                delta = 0
            if delta:
                deltas.append(f"{arch} {delta:+d}")
    if not changed:
        return "packages unchanged"
    return "packages changed" + (f" ({', '.join(deltas)})" if deltas else "")


def description_of(build: Build, previous: Build | None, follow_up: Run | None) -> str:
    """One line: mode · reason · what the build meant for the image."""
    parts = [build.mode or "?", build.reason or "?"]
    if build.pull_request:
        parts.append(f"PR #{build.pull_request}")
    if build.failed_job and build.result == "failure":
        parts.append(f"failed job: {build.failed_job}")
    bundle = build.bundle
    if is_package_check(build):
        if build.result == "skipped":
            parts.append("packages unchanged, nothing released")
        elif build.result == "success":
            parts.append("new package table committed")
            if follow_up:
                parts.append(f"release build [run {follow_up.id}]({follow_up.html_url})")
            else:
                parts.append("release build follows")
    elif bundle and (changed := packages_changed(bundle, previous)):
        parts.append(changed)
    return " \u00b7 ".join(parts)


def platforms_field(build: Build) -> str:
    """One line per arch: digest (short), compressed size and the arch's own test outcome; a failed arch says so."""
    if not build.platforms:
        return "pending" if build.running else "\u2014"
    bundle = build.bundle or {}
    records = {str(p.get("arch")): p for p in bundle.get("platforms", []) if isinstance(p, dict)}
    per_arch = bundle.get("test", {}).get("per_arch", {}) if isinstance(bundle.get("test"), dict) else {}
    lines = []
    for platform in build.platforms:
        record = records.get(platform.arch, {})
        if platform.result not in (None, "success"):
            lines.append(f"**{platform.arch}** {platform.result or 'unknown'}")
            continue
        bits = [f"**{platform.arch}**"]
        digest = str(record.get("digest") or "")
        if digest:
            bits.append(f"`{digest.removeprefix('sha256:')[:12]}`")
        if size := mib(record.get("size")):
            bits.append(size)
        tested = per_arch.get(platform.arch)
        if isinstance(tested, bool):
            bits.append("tests passed" if tested else "tests failed")
        elif build.test_success is not None:
            bits.append("tests passed" if build.test_success else "tests failed")
        lines.append(" ".join(bits))
    return "\n".join(lines)


def upstream_field(build: Build) -> str:
    bundle = build.bundle or {}
    version = bundle.get("version", {}) if isinstance(bundle.get("version"), dict) else {}
    upstream = build.upstream or version.get("upstream")
    if not upstream:
        return "\u2014"
    source_url = version.get("source_url")
    text = f"[{upstream}]({source_url})" if source_url else str(upstream)
    if source := version.get("source"):
        text += f" ({source})"
    return text


def timing_field(build: Build) -> str:
    bundle = build.bundle or {}
    lines = []
    per_arch = [
        f"{p.get('arch')} {secs}"
        for p in bundle.get("platforms", [])
        if isinstance(p, dict) and (secs := seconds(p.get("build_seconds")))
    ]
    if per_arch:
        lines.append(" \u00b7 ".join(per_arch))
    started, finished = clock(build.created_at), clock(build.updated_at)
    if build.running:
        lines.append(f"started {started} UTC")
    elif started and started != finished:
        line = f"{started} \u2192 {finished} UTC"
        if elapsed := seconds(instant(build.updated_at) - instant(build.created_at)):
            line += f" ({elapsed})"
        lines.append(line)
    else:
        lines.append(f"finished {finished} UTC")
    return "\n".join(lines)


def instant(timestamp: str) -> float:
    try:
        return datetime.datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def links_field(build: Build, public_url: str, site_url: str) -> str:
    links = []
    if build.mode == "live" and build.version_tag and build.release_url:
        links.append(f"[CI report]({site_build_url(site_url, build.image, build.version_tag)})")
    if build.run_url:
        links.append(f"[run]({build.run_url})")
    if build.release_url:
        links.append(f"[release]({build.release_url})")
    if build.artifact_url:
        links.append(f"[artifact]({build.artifact_url})")
    if build.pull_request:
        links.append(f"[PR #{build.pull_request}](https://github.com/{build.repo}/pull/{build.pull_request})")
    links.append(f"[control]({control_build_url(public_url, build)})")
    return " \u00b7 ".join(links)


def build_embed(
    build: Build, settings: Settings, previous: Build | None = None, follow_up: Run | None = None
) -> dict[str, Any]:
    """One embed per build; the same shape for the provisional and the final message so an edit reads well.
    A released live build points at its page on the website (the embed url and the "CI report" link), the
    image name above the title at the image page; dev, pr and package-check runs point at the control run
    page. `previous` is the previous release's build (for the packages line) and `follow_up` the release
    build a package check dispatched, when control has seen either."""
    color = embed_color(build)
    released = build.mode == "live" and build.version_tag and build.release_url
    url = (
        site_build_url(settings.site_url, build.image, str(build.version_tag))
        if released
        else control_build_url(settings.public_url, build)
    )
    fields = [
        {"name": "Platforms", "value": platforms_field(build)},
        {"name": "Upstream", "value": upstream_field(build)},
        {"name": "Timing", "value": timing_field(build)},
        {"name": "Links", "value": links_field(build, settings.public_url, settings.site_url)},
    ]
    title_tag = build.version_tag or build.branch or ""
    embed = {
        "author": {"name": build.image, "url": site_image_url(settings.site_url, build.image)},
        "title": f"{build.image} {title_tag}".strip() + f" \u00b7 {DOT[color]} {result_word(build)}",
        "url": url,
        "color": color,
        "description": description_of(build, previous, follow_up),
        "fields": two_columns(fields),
        "footer": {"text": f"{build.repo} \u00b7 run {build.run_id}"},
        "timestamp": build.updated_at,
    }
    return within_limits(embed)


def previous_build(model: Model, build: Build) -> Build | None:
    """The build of the previous release named in the bundle, when control has it with its bundle."""
    bundle = build.bundle or {}
    previous = bundle.get("previous") if isinstance(bundle.get("previous"), dict) else None
    tag = previous.get("tag") if previous else None
    return model.build_for_tag(build.repo, str(tag), with_bundle=True) if tag else None


def follow_up_run(model: Model, build: Build) -> Run | None:
    """The release build a package check dispatched on its branch after committing the new table, when its
    run has already arrived by webhook (the same lookup the scheduler page uses for its dispatches)."""
    if not (is_package_check(build) and build.result == "success" and build.branch):
        return None
    return model.run_after(build.repo, build.branch, "workflow_dispatch", build.created_at, exclude_run_id=build.run_id)


async def announce(state: State, build: Build, event_type: str) -> None:
    """Post or edit the Discord message for a build. Never raises; Discord failures are logged by the client."""
    if not state.discord.configured:
        return
    if event_type == "build.started" and build.mode != "live":
        return
    embed = build_embed(build, state.settings, previous_build(state.model, build), follow_up_run(state.model, build))
    if build.discord_message_id and await state.discord.edit(build.discord_message_id, embeds=[embed]):
        return
    message_id = await state.discord.post(embeds=[embed])
    if message_id:
        state.model.set_build_message(build.repo, build.run_id, message_id)


async def notify_hub(state: State, build: Build) -> None:
    """Tell the hub a live release exists so it ingests the bundle now instead of at its hourly reconcile.

    The hub's deploy workflow listens for repository_dispatch `image-released` with {repository, tag}.
    Best effort: the hub reconciles on its own anyway, so a failure here only costs freshness.
    """
    # Only a build that actually released (a package check reports success without releasing anything).
    if build.mode != "live" or build.result != "success" or not build.version_tag or not build.release_url:
        return
    try:
        github = await installation_client(state)
        if github is None:
            return
        response = await github.post(
            f"/repos/{state.settings.github_org}/{state.settings.hub_repo}/dispatches",
            json={
                "event_type": "image-released",
                "client_payload": {"repository": build.repo, "tag": build.version_tag},
            },
        )
        if response.status_code != 204:
            log.warning("hub dispatch for %s %s returned %s", build.repo, build.version_tag, response.status_code)
        else:
            log.info("hub dispatch sent for %s %s", build.repo, build.version_tag)
    except Exception:  # never let a hub hiccup fail the event intake
        log.exception("hub dispatch failed for %s %s", build.repo, build.version_tag)


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
        if event_type == "build.summary":
            await notify_hub(state, build)
    log.info("%s for %s run %s (%s)", event_type, payload["repository"], payload["run_id"], payload.get("result", ""))
    return {"status": "accepted", "type": event_type, "applied": applied}
