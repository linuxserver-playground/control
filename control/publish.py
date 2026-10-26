"""
Holding a released build's Discord message until its page is on the website.

A release reaches the website when the hub has ingested and deployed it: the site renders a build it predates
from the hub's data in the browser, and its own rebuild, which turns that into a static page, comes later. The
summary says "publishing" and links control until then, instead of a page that would show nothing. Every 20
seconds this checks each held build's site page and the hub's builds list and, once either has it, edits the
message into its final form.

Nothing is left dangling: past `publish_deadline_minutes` the message says the page is overdue and a separate
post (which notifies, where an edit does not) says where it is stuck, from the hub's own data: the hub has not
ingested the release, or the hub has it and the site has not published it. Releases that cross the deadline in the
same check for the same reason share one post, since a burst of them waits on the same deploy. Control keeps
watching; a page that turns up later clears the warning with a short follow-up, and after `publish_expiry_hours`
control stops and says so once more.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from collections.abc import Callable
from typing import NamedTuple

import httpx

from .db import utcnow
from .events import announce, site_build_url
from .model import Build
from .state import State

log = logging.getLogger(__name__)

CHECK_SECONDS = 20.0
"""How often held builds are checked: two small requests each, and only while a release is held."""


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
    alerts: Alerts = {}
    for build in state.model.publications():
        tag = str(build.version_tag)
        url = site_build_url(settings.site_url, build.image, tag)
        age = _instant(now()) - _instant(build.publish_started_at)
        minutes = int(age // 60)
        # The site renders a build it predates from the hub's data (its 404 fallback), so the page works as soon as
        # the hub serves the build, before the site's own rebuild makes it a static page.
        if await page_live(state.http, url) or await hub_has(state.http, settings.hub_url, build.image, tag):
            state.model.set_publication(build.repo, build.run_id, "live", published_at=now())
            log.info("%s %s is on the website after %s min", build.image, tag, minutes)
            await _refresh(state, build)
            if build.publish_state == "overdue":
                message = f"✅ **{build.image} {tag}** reached the website after {minutes} min: {url}"
                alerts.setdefault(("live", ""), []).append(Alert(f"{build.image} {tag}", minutes, message))
            continue
        if build.publish_state == "pending" and age >= deadline:
            note = await diagnose(state, build)
            state.model.set_publication(build.repo, build.run_id, "overdue", note=note)
            log.warning("%s %s not on the website after %s min: %s", build.image, tag, minutes, note)
            await _refresh(state, build)
            message = (
                f"⚠️ **{build.image} {tag}** was released {minutes} min ago and its website page is still "
                f"missing: {note}. Release: {build.release_url}"
            )
            alerts.setdefault(("overdue", note), []).append(Alert(f"{build.image} {tag}", minutes, message))
        elif build.publish_state == "overdue" and age >= expiry:
            note = await diagnose(state, build)
            state.model.set_publication(build.repo, build.run_id, "expired", note=note)
            log.error("%s %s never reached the website: %s", build.image, tag, note)
            await _refresh(state, build)
            message = (
                f"❌ **{build.image} {tag}** still has no website page {settings.publish_expiry_hours} h after "
                f"its release, control has stopped watching: {note}. Release: {build.release_url}"
            )
            alerts.setdefault(("expired", note), []).append(Alert(f"{build.image} {tag}", minutes, message))
    for (kind, note), group in alerts.items():
        if len(group) == 1:
            await state.discord.post(group[0].message)
            continue
        for content in grouped(kind, note, group, settings.publish_expiry_hours):
            await state.discord.post(content)


class Alert(NamedTuple):
    release: str
    minutes: int
    message: str
    """The post when this release is the only one in its group."""


Alerts = dict[tuple[str, str], list[Alert]]

DISCORD_LIMIT = 2000


def grouped(kind: str, note: str, group: list[Alert], expiry_hours: int) -> list[str]:
    """
    One post for every release that changed state in the same check and for the same reason. A burst of releases
    queues behind one hub or site deploy, so they cross the deadline together; one post each buried the channel.
    Split into several posts only past Discord's message limit.
    """
    low, high = min(a.minutes for a in group), max(a.minutes for a in group)
    span = f"{low}" if low == high else f"{low} to {high}"
    head = {
        "live": f"✅ {len(group)} releases reached the website after {span} min:",
        "overdue": f"⚠️ {len(group)} releases, {span} min old, still have no website page: {note}.",
        "expired": (
            f"❌ {len(group)} releases still have no website page {expiry_hours} h after their release, control has "
            f"stopped watching: {note}."
        ),
    }[kind]
    posts, lines = [], [head]
    for alert in group:
        line = f"- **{alert.release}**"
        if len("\n".join([*lines, line])) > DISCORD_LIMIT:
            posts.append("\n".join(lines))
            lines = [head]
        lines.append(line)
    posts.append("\n".join(lines))
    return posts


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
