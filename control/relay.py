"""
Cross-repository nudges GitHub cannot do with a workflow token: when the hub's Deploy workflow finishes
successfully, tell the website to refresh (repository_dispatch `hub-updated`). The control plane already
receives every workflow_run webhook and holds the App installation token, so it is the natural relay. Best
effort: the site also rebuilds on its daily cron.
"""

import asyncio
import logging
from typing import Any

from .github import installation_client
from .state import State

log = logging.getLogger(__name__)

HUB_DEPLOY_WORKFLOW = "Deploy"


def is_hub_deploy_success(state: State, payload: dict[str, Any]) -> bool:
    run = payload.get("workflow_run") or {}
    repository = payload.get("repository") or {}
    return (
        payload.get("action") == "completed"
        and run.get("conclusion") == "success"
        and run.get("name") == HUB_DEPLOY_WORKFLOW
        and repository.get("full_name") == f"{state.settings.github_org}/{state.settings.hub_repo}"
    )


async def dispatch_site_refresh(state: State, payload: dict[str, Any]) -> bool:
    """POST repository_dispatch hub-updated to the site repo. Returns whether GitHub accepted it."""
    run = payload.get("workflow_run") or {}
    try:
        github = await installation_client(state)
        if github is None:
            return False
        response = await github.post(
            f"/repos/{state.settings.github_org}/{state.settings.site_repo}/dispatches",
            json={
                "event_type": "hub-updated",
                "client_payload": {"hub_run_id": run.get("id"), "head_sha": run.get("head_sha")},
            },
        )
    except Exception:  # best effort; the site's cron is the fallback
        log.exception("site refresh dispatch failed")
        return False
    if response.status_code != 204:
        log.warning("site refresh dispatch returned %s", response.status_code)
        return False
    log.info("site refresh dispatched after hub run %s", run.get("id"))
    return True


def schedule_site_refresh(state: State, payload: dict[str, Any]) -> asyncio.Task[Any] | None:
    """Fire-and-forget from the webhook handler."""
    if not is_hub_deploy_success(state, payload):
        return None
    return state.spawn(dispatch_site_refresh(state, payload))
