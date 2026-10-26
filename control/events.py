"""
The `/events` endpoint the shared workflow POSTs to (CONVENTIONS §10): a control-event signed with
HMAC-SHA256 over the raw body using the per-org `CONTROL_EVENTS_SECRET`, validated against the `contracts`
schema, stored in `deliveries` as `control:<type>` so it replays like a webhook, then applied and
announced on Discord. A live build gets one message, posted on `build.started` and edited into the
summary; dev and pr builds only get the summary, and a package check only when it fails (the release build
it dispatches is what counts). A released build links its page on the website, the single pane of glass;
that page reaches the website minutes later (hub, then site), so its summary says "publishing" and points at
control until publish.py sees the page live and edits it into the final form. Any other build links the
report GitHub shows: a pull request build its report comment, a dev or failed build its run's job summary.
"""

import asyncio
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
from .db import utcnow
from .github import installation_client
from .model import IGNORED_WORKFLOW_NAMES, Build, Model
from .state import State, state_of
from .webhooks import signature_valid

log = logging.getLogger(__name__)

router = APIRouter()

# The Jenkins-era colours: live builds bright, dev and pull request builds dark.
GREEN = 0x19A719
DARK_GREEN = 0x3C6124
RED = 0xFF0000
DARK_RED = 0xC15253
GREY = 0x8B949E
AMBER = 0xD29922
DOT = {
    GREEN: "\U0001f7e2",
    DARK_GREEN: "\U0001f7e2",
    RED: "\U0001f534",
    DARK_RED: "\U0001f534",
    AMBER: "\U0001f7e1",
    GREY: "\u26aa",
}
REPORT_MARKER = "<!-- lsio-build-report -->"
"""Marks the pull request comment the PR report workflow keeps up to date (workflows/scripts/pr_report.py)."""
REPORT_BOT = "github-actions[bot]"
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


def awaiting_site(build: Build) -> bool:
    """A released build whose page is not on the website yet (or never made it)."""
    return build.publish_state in ("pending", "overdue", "expired")


def embed_color(build: Build) -> int:
    if build.running or awaiting_site(build):
        return AMBER
    live = build.mode == "live"
    if build.result == "success":
        return GREEN if live else DARK_GREEN
    if build.result == "failure":
        return RED if live else DARK_RED
    return GREY


def result_word(build: Build) -> str:
    word = "running" if build.running else RESULT_WORD.get(str(build.result), str(build.result))
    if build.publish_state == "pending":
        return f"{word}, publishing"
    if build.publish_state in ("overdue", "expired"):
        return f"{word}, not on the site"
    return word


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


def released(build: Build) -> bool:
    """A live build that published a release, so the website will get a page for it."""
    return build.mode == "live" and build.result == "success" and bool(build.version_tag and build.release_url)


def is_package_check(build: Build) -> bool:
    """A live `reason: packages` run compares package tables and never releases (the follow-up build does)."""
    return build.mode == "live" and build.reason == "packages" and not build.release_url


def announced(build: Build) -> bool:
    """Whether Discord hears of this build. A package check only when it failed: whether it found new packages
    or not, what matters is the release build it dispatches, which is announced on its own."""
    if is_package_check(build):
        return build.result == "failure"
    return True


def summary_url(model: Model, build: Build) -> str | None:
    """The run page scrolled to the Publish job's summary, which opens with the build's report."""
    if not build.run_url:
        return None
    publish = next(
        (j for j in model.jobs_for_run(build.run_id) if j.name.rsplit("/", 1)[-1].strip() == "Publish"), None
    )
    return f"{build.run_url}#summary-{publish.id}" if publish else build.run_url


async def pr_comment_url(state: State, build: Build) -> str | None:
    """The report comment on the build's pull request, once the PR report workflow has written it. Never raises."""
    if not build.pull_request:
        return None
    try:
        github = await installation_client(state)
        if github is None:
            return None
        comments = await github.paginate(f"/repos/{build.repo}/issues/{build.pull_request}/comments", {"per_page": 100})
    except Exception:
        log.exception("report comment lookup failed for %s #%s", build.repo, build.pull_request)
        return None
    for comment in comments if isinstance(comments, list) else []:
        if REPORT_MARKER in str(comment.get("body") or "") and (comment.get("user") or {}).get("login") == REPORT_BOT:
            return str(comment.get("html_url") or "") or None
    return None


async def report_url(state: State, build: Build) -> str | None:
    """Where a build without a website page has its report: the pull request comment, else the run summary."""
    if build.running:
        return None
    if build.mode == "pr":
        return await pr_comment_url(state, build)
    return summary_url(state.model, build)


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


def description_of(build: Build, previous: Build | None) -> str:
    """One line: mode · reason · what the build meant for the image."""
    parts = [build.mode or "?", build.reason or "?"]
    if build.pull_request:
        parts.append(f"PR #{build.pull_request}")
    if build.failed_job and build.result == "failure":
        parts.append(f"failed job: {build.failed_job}")
    bundle = build.bundle
    if not is_package_check(build) and bundle and (changed := packages_changed(bundle, previous)):
        parts.append(changed)
    if build.publish_state == "pending":
        parts.append("publishing to the website")
    elif build.publish_state == "overdue":
        parts.append(f"\u26a0\ufe0f not on the website yet: {build.publish_note or 'no diagnosis'}")
    elif build.publish_state == "expired":
        parts.append(f"\u26a0\ufe0f never reached the website: {build.publish_note or 'no diagnosis'}")
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


def links_field(build: Build, public_url: str, site_url: str, report: str | None = None) -> str:
    links = []
    if build.mode == "live" and build.version_tag and build.release_url:
        if build.publish_state == "pending":
            links.append("CI report (publishing\u2026)")
        else:
            links.append(f"[CI report]({site_build_url(site_url, build.image, build.version_tag)})")
    elif report:
        links.append(f"[CI report]({report})")
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
    build: Build, settings: Settings, previous: Build | None = None, report: str | None = None
) -> dict[str, Any]:
    """One embed per build; the same shape for the provisional and the final message so an edit reads well.
    A released live build points at its page on the website (the embed url and the "CI report" link), the
    image name above the title at the image page. Any other finished build points at `report`, the report
    GitHub shows (`report_url`: the pull request comment, or the run's job summary), and a running one or one
    without a report at the control run page. `previous` is the previous release's build (for the packages
    line), when control has it."""
    color = embed_color(build)
    released = build.mode == "live" and build.version_tag and build.release_url and not awaiting_site(build)
    if released:
        url = site_build_url(settings.site_url, build.image, str(build.version_tag))
    elif report and not (build.mode == "live" and build.release_url):
        url = report
    else:
        url = control_build_url(settings.public_url, build)
    fields = [
        {"name": "Platforms", "value": platforms_field(build)},
        {"name": "Upstream", "value": upstream_field(build)},
        {"name": "Timing", "value": timing_field(build)},
        {"name": "Links", "value": links_field(build, settings.public_url, settings.site_url, report)},
    ]
    title_tag = build.version_tag or build.branch or ""
    embed = {
        "author": {"name": build.image, "url": site_image_url(settings.site_url, build.image)},
        "title": f"{build.image} {title_tag}".strip() + f" \u00b7 {DOT[color]} {result_word(build)}",
        "url": url,
        "color": color,
        "description": description_of(build, previous),
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


async def announce(state: State, build: Build, event_type: str) -> None:
    """Post or edit the Discord message for a build. Never raises; Discord failures are logged by the client."""
    if not state.discord.configured:
        return
    if event_type == "build.started" and (build.mode != "live" or build.reason == "packages"):
        return
    if not announced(build):
        return
    report = None if (build.mode == "live" and build.release_url) else await report_url(state, build)
    embed = build_embed(build, state.settings, previous_build(state.model, build), report)
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
    if applied and build is not None and event_type == "build.summary" and released(build):
        state.model.start_publication(build.repo, build.run_id, utcnow())
        build = state.model.build(build.repo, build.run_id)
    if applied and build is not None:
        await announce(state, build, event_type)
        if event_type == "build.summary":
            await notify_hub(state, build)
    log.info("%s for %s run %s (%s)", event_type, payload["repository"], payload["run_id"], payload.get("result", ""))
    return {"status": "accepted", "type": event_type, "applied": applied}


async def refresh_pr_reports(state: State, repo: str) -> int:
    """After a PR report run: point the latest message of each recent pull request build of `repo` at the report
    comment it wrote (the first build of a pull request finishes before its comment exists). Never raises."""
    seen: set[int] = set()
    count = 0
    for build in state.model.recent_builds(200):
        if build.repo != repo or build.mode != "pr" or not build.pull_request or not build.discord_message_id:
            continue
        if build.pull_request in seen:
            continue
        seen.add(build.pull_request)
        await announce(state, build, "build.reported")
        count += 1
    return count


def schedule_pr_report_refresh(state: State, payload: dict[str, Any]) -> asyncio.Task[Any] | None:
    """Fire-and-forget from the webhook handler, for a completed run of an image repository's PR report."""
    run = payload.get("workflow_run") or {}
    repo = (payload.get("repository") or {}).get("full_name")
    if payload.get("action") != "completed" or run.get("name") not in IGNORED_WORKFLOW_NAMES or not repo:
        return None
    return state.spawn(refresh_pr_reports(state, str(repo)))
