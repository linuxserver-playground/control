"""
GitHub Pages on every image repository. Each image's build deploys its heavy data (the blobs a Release holds no
CORS for) to its own repository's Pages, so every `docker-*` repo needs Pages enabled with `build_type=workflow` and
its `github-pages` environment open to every branch (live branches are not all the default one). The existing fleet
was set up by hand; control does the same for every repo it finds after that: on `repository created`, on the first
push to a repo with nothing live yet, and on every fleet discovery for a repo it has not done. A repo is done once,
recorded in `repo_pages`, and never touched again.

It needs the App's Pages and Administration write permissions. An installation without them (the token says what
it was granted) is skipped cleanly: control logs it and posts it to Discord once, and tries again by itself once
the org has accepted the new permissions.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

from .github import GitHub, installation_client
from .model import Repo
from .state import State

log = logging.getLogger(__name__)

NEEDED: dict[str, str] = {"pages": "write", "administration": "write"}
"""The installation permissions Pages setup needs: Pages to create the site, Administration for the environment."""
PERMISSIONS_MISSING = "pages_permissions_missing"
"""kv key: JSON list of the permissions last reported missing, so the warning goes out once, not every pass."""
ENVIRONMENT = "github-pages"


class PermissionMissing(Exception):
    pass


def missing_permissions(state: State) -> list[str] | None:
    """The needed permissions the installation lacks, or None while no token has said what it has."""
    installation_id = state.installation_id
    granted = state.tokens.permissions(installation_id) if installation_id is not None else None
    if granted is None:
        return None
    return [f"{name}: {level}" for name, level in NEEDED.items() if granted.get(name) != level]


async def ensure_repo(github: GitHub, repo: str) -> str:
    """Pages on with `build_type=workflow` and the environment open to every branch. Returns what it found or did."""
    try:
        try:
            site: Any = await github.get_json(f"/repos/{repo}/pages")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
            site = None
        if site is None:
            try:
                await github.post(f"/repos/{repo}/pages", {"build_type": "workflow"})
                detail = "enabled"
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 409:  # created meanwhile
                    raise
                detail = "present"
        elif isinstance(site, dict) and site.get("build_type") != "workflow":
            await github.put(f"/repos/{repo}/pages", {"build_type": "workflow"})
            detail = f"switched from {site.get('build_type')} to workflow"
        else:
            detail = "present"
        await github.put(f"/repos/{repo}/environments/{ENVIRONMENT}", {"deployment_branch_policy": None})
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 403:
            raise PermissionMissing from exc
        raise
    return detail


async def blocked(state: State, missing: list[str]) -> None:
    """Say once (across restarts too) that Pages setup waits for permissions, and which."""
    value = json.dumps(missing)
    if state.db.get(PERMISSIONS_MISSING) == value:
        log.debug("pages setup still waiting for %s", ", ".join(missing))
        return
    state.db.set(PERMISSIONS_MISSING, value)
    log.warning(
        "pages setup skipped: the GitHub App installation lacks %s; grant them on the App and accept them on the "
        "installation, and control enables Pages on the new image repos by itself",
        ", ".join(missing),
    )
    await state.discord.post(
        f"⚠️ control cannot enable GitHub Pages on new image repos: the App installation lacks "
        f"{', '.join(missing)}. Grant them under the App's permissions and accept them on the organisation's "
        "installation; control picks it up within the hour."
    )


async def ensure(state: State, github: GitHub, repos: list[Repo]) -> dict[str, int]:
    """Set up Pages on every image repo of `repos` not done yet. Never raises."""
    if not state.settings.repo_pages:
        return {}
    done = state.model.repo_pages_done()
    todo = [r for r in repos if r.is_image and not r.archived and r.full_name not in done]
    if not todo:
        return {}
    missing = missing_permissions(state)
    if missing:
        await blocked(state, missing)
        return {"blocked": len(todo)}
    counts = {"done": 0, "errors": 0}
    for repo in todo:
        try:
            detail = await ensure_repo(github, repo.full_name)
        except PermissionMissing:
            await blocked(state, [f"{name}: {level}" for name, level in NEEDED.items()])
            counts["blocked"] = len(todo) - counts["done"] - counts["errors"]
            return counts
        except Exception:  # one repo's failure must not stop the others; the next discovery tries again
            counts["errors"] += 1
            log.exception("pages setup failed for %s", repo.full_name)
            continue
        state.model.set_repo_pages(repo.full_name, detail)
        counts["done"] += 1
        log.info("pages for %s: %s, %s environment open to every branch", repo.full_name, detail, ENVIRONMENT)
    if counts["done"] and state.db.get(PERMISSIONS_MISSING) is not None:
        state.db.delete(PERMISSIONS_MISSING)
        log.info("pages setup has its permissions again")
    return counts


async def ensure_one(state: State, full_name: str) -> dict[str, int]:
    """One repo, from a webhook. Never raises."""
    try:
        repo = state.model.repo(full_name)
        github = await installation_client(state)
        if repo is None or github is None:
            return {}
        return await ensure(state, github, [repo])
    except Exception:
        log.exception("pages setup for %s failed", full_name)
        return {}


def schedule(state: State, payload: dict[str, Any]) -> asyncio.Task[Any] | None:
    """Fire-and-forget from the webhook handler, for a `docker-*` repository just created in the org."""
    repository = payload.get("repository")
    if payload.get("action") != "created" or not isinstance(repository, dict):
        return None
    if not str(repository.get("name", "")).startswith("docker-") or not state.settings.repo_pages:
        return None
    return state.spawn(ensure_one(state, str(repository["full_name"])))
