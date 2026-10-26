"""
Cross-repository nudges GitHub cannot do with a workflow token: when the hub's Deploy workflow finishes
successfully, tell the website to refresh (repository_dispatch `hub-updated`). The control plane already
receives every workflow_run webhook and holds the App installation token, so it is the natural relay. Best
effort: the site also rebuilds on its daily cron.

It also starts the hub's hourly refresh (`workflow_dispatch` of refresh.yml: stars, GHCR tags, the mods index and the
vulnerability rescans), because GitHub skips most runs of the hub's own hourly cron.
"""

import asyncio
import datetime
import logging
from typing import Any

from .github import GitHub, installation_client
from .state import State

log = logging.getLogger(__name__)

HUB_DEPLOY_WORKFLOW = "Deploy"
HUB_REFRESH_FILE = "refresh.yml"
HUB_REFRESH_INTERVAL = datetime.timedelta(hours=1)
LAST_HUB_REFRESH = "last_hub_refresh"
"""kv key: ISO time of the last hub refresh dispatch that GitHub accepted."""


def is_hub_deploy_success(state: State, payload: dict[str, Any]) -> bool:
    run = payload.get("workflow_run") or {}
    repository = payload.get("repository") or {}
    return (
        payload.get("action") == "completed"
        and run.get("conclusion") == "success"
        and run.get("name") == HUB_DEPLOY_WORKFLOW
        and repository.get("full_name") == f"{state.settings.github_org}/{state.settings.hub_repo}"
    )


def consumers(state: State) -> tuple[str, ...]:
    """The repos that rebuild from hub data: the configured list, always including the site repo."""
    names = list(state.settings.hub_consumers)
    if state.settings.site_repo not in names:
        names.insert(0, state.settings.site_repo)
    return tuple(names)


async def dispatch_site_refresh(state: State, payload: dict[str, Any]) -> bool:
    """POST repository_dispatch hub-updated to every hub consumer (site, docs). True when all were accepted."""
    run = payload.get("workflow_run") or {}
    body = {
        "event_type": "hub-updated",
        "client_payload": {"hub_run_id": run.get("id"), "head_sha": run.get("head_sha")},
    }
    try:
        github = await installation_client(state)
        if github is None:
            return False
    except Exception:  # best effort; the consumers' crons are the fallback
        log.exception("hub-updated dispatch failed")
        return False
    ok = True
    for repo in consumers(state):
        try:
            response = await github.post(f"/repos/{state.settings.github_org}/{repo}/dispatches", json=body)
        except Exception:
            log.exception("hub-updated dispatch to %s failed", repo)
            ok = False
            continue
        if response.status_code != 204:
            log.warning("hub-updated dispatch to %s returned %s", repo, response.status_code)
            ok = False
        else:
            log.info("hub-updated dispatched to %s after hub run %s", repo, run.get("id"))
    return ok


def schedule_site_refresh(state: State, payload: dict[str, Any]) -> asyncio.Task[Any] | None:
    """Fire-and-forget from the webhook handler."""
    if not is_hub_deploy_success(state, payload):
        return None
    return state.spawn(dispatch_site_refresh(state, payload))


def hub_refresh_due(state: State, now: datetime.datetime | None = None) -> bool:
    last = state.db.get(LAST_HUB_REFRESH)
    if not last:
        return True
    at = now or datetime.datetime.now(datetime.UTC)
    return at - datetime.datetime.fromisoformat(last) >= HUB_REFRESH_INTERVAL


async def dispatch_hub_refresh(state: State, github: GitHub, now: datetime.datetime | None = None) -> bool:
    """`workflow_dispatch` of the hub's Refresh on main, the same as its cron. Never raises."""
    at = now or datetime.datetime.now(datetime.UTC)
    repo = f"{state.settings.github_org}/{state.settings.hub_repo}"
    try:
        await github.post(f"/repos/{repo}/actions/workflows/{HUB_REFRESH_FILE}/dispatches", {"ref": "main"})
    except Exception:
        log.exception("hub refresh dispatch failed")
        return False
    state.db.set(LAST_HUB_REFRESH, at.isoformat(timespec="seconds"))
    log.info("hub refresh dispatched")
    return True
