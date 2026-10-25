"""
Holding a released build's Discord message until its page is on the website.

A release reaches the website in steps: the hub ingests it and deploys, then the site refreshes and deploys, a
few minutes normally and longer when a burst of releases queues behind one another. The summary therefore says
"publishing" and links control instead of a page that would 404. Every minute this checks each held build's site
page and, once it answers, edits the message into its final form.

Nothing is left dangling: past `publish_deadline_minutes` the message says the page is overdue and a separate
post (which notifies, where an edit does not) says where it is stuck, from the hub's own data: the hub has not
ingested the release, or the hub has it and the site has not published it. Control keeps watching; a page that
turns up later clears the warning with a short follow-up, and after `publish_expiry_hours` control stops and says
so once more.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from collections.abc import Callable

import httpx

from .db import utcnow
from .events import announce, site_build_url
from .model import Build
from .state import State

log = logging.getLogger(__name__)

CHECK_SECONDS = 60.0
"""Pages answers within the minute of a deploy; checking more often only adds requests."""


def _instant(timestamp: str | None) -> float:
    if not timestamp:
        return 0.0
    try:
        return datetime.datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


async def page_live(http: httpx.AsyncClient, url: str) -> bool:
    try:
        response = await http.get(url, follow_redirects=True, timeout=15)
    except httpx.HTTPError:
        return False
    return response.status_code == 200


async def hub_has(http: httpx.AsyncClient, hub_url: str, image: str, tag: str) -> bool | None:
    """Whether the hub's published data lists the build; None when the hub cannot be read."""
    try:
        response = await http.get(f"{hub_url}/api/v1/catalog/{image}/builds.json", timeout=15)
    except httpx.HTTPError:
        return None
    if response.status_code == 404:
        return False
    if response.status_code != 200:
        return None
    try:
        builds = response.json()["data"]["builds"]
    except (ValueError, KeyError, TypeError):
        return None
    return any(isinstance(b, dict) and b.get("tag") == tag for b in builds)


async def diagnose(state: State, build: Build) -> str:
    """Where a release is stuck on its way to the website, in a sentence with the runs to look at."""
    settings = state.settings
    org = settings.github_org
    hub_runs = f"https://github.com/{org}/{settings.hub_repo}/actions/workflows/deploy.yml"
    site_runs = f"https://github.com/{org}/{settings.site_repo}/actions"
    has = await hub_has(state.http, settings.hub_url, build.image, str(build.version_tag))
    if has is None:
        return f"the hub could not be read ({settings.hub_url})"
    if not has:
        return f"the hub has not ingested it (hub runs: {hub_runs})"
    return f"the hub has it, the site has not published it (site runs: {site_runs})"


async def check_once(state: State, now: Callable[[], str] = utcnow) -> None:
    settings = state.settings
    deadline = settings.publish_deadline_minutes * 60
    expiry = settings.publish_expiry_hours * 3600
    for build in state.model.publications():
        tag = str(build.version_tag)
        url = site_build_url(settings.site_url, build.image, tag)
        age = _instant(now()) - _instant(build.publish_started_at)
        minutes = int(age // 60)
        if await page_live(state.http, url):
            state.model.set_publication(build.repo, build.run_id, "live", published_at=now())
            log.info("%s %s is on the website after %s min", build.image, tag, minutes)
            await _refresh(state, build)
            if build.publish_state == "overdue":
                await state.discord.post(f"✅ **{build.image} {tag}** reached the website after {minutes} min: {url}")
            continue
        if build.publish_state == "pending" and age >= deadline:
            note = await diagnose(state, build)
            state.model.set_publication(build.repo, build.run_id, "overdue", note=note)
            log.warning("%s %s not on the website after %s min: %s", build.image, tag, minutes, note)
            await _refresh(state, build)
            await state.discord.post(
                f"⚠️ **{build.image} {tag}** was released {minutes} min ago and its website page is still "
                f"missing: {note}. Release: {build.release_url}"
            )
        elif build.publish_state == "overdue" and age >= expiry:
            note = await diagnose(state, build)
            state.model.set_publication(build.repo, build.run_id, "expired", note=note)
            log.error("%s %s never reached the website: %s", build.image, tag, note)
            await _refresh(state, build)
            await state.discord.post(
                f"❌ **{build.image} {tag}** still has no website page {settings.publish_expiry_hours} h after "
                f"its release, control has stopped watching: {note}. Release: {build.release_url}"
            )


async def _refresh(state: State, build: Build) -> None:
    """Edit the build's message into its new publication state."""
    fresh = state.model.build(build.repo, build.run_id)
    if fresh is not None:
        await announce(state, fresh, "build.published")


async def run(state: State) -> None:
    """The background task the app starts beside the server."""
    while True:
        try:
            await check_once(state)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("publication check failed")
        await asyncio.sleep(CHECK_SECONDS)
