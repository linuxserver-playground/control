"""
What signed-in members may do: re-run or cancel a run, dispatch a build, set and clear skip flags,
trigger a package check, re-read the fleet manifests. Every route is a POST with the session's CSRF token,
refuses anyone who is not an active member (403), is rate limited per user, and writes an `actions` row
whatever the outcome, which `/management` shows to everyone.
"""

import datetime
import logging
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from . import auth, fleet, scheduler
from .github import GitHub, installation_client
from .model import image_name_of
from .state import State, state_of

log = logging.getLogger(__name__)

router = APIRouter(prefix="/manage")

RATE_LIMIT = 30
RATE_WINDOW = datetime.timedelta(hours=1)
REASONS = ("manual", "rebuild", "packages", "upstream")
"""The `reason` choices build.yml's workflow_dispatch accepts."""

Operation = Callable[[State, GitHub, "Actor"], Awaitable[str]]


class Actor:
    """The verified member behind a request, with the refreshed session to set on the response."""

    def __init__(self, user: auth.User, refreshed: bool) -> None:
        self.user = user
        self.refreshed = refreshed

    @property
    def login(self) -> str:
        return self.user.login


async def require_member(request: Request, form: dict[str, Any]) -> Actor:
    state = state_of(request)
    user = auth.current_user(request)
    if user is None:
        raise HTTPException(403, "sign in first")
    user, refreshed = await auth.refresh_if_stale(state, user)
    if not user.member:
        raise HTTPException(403, f"{user.login} is not a member of {state.settings.github_org}")
    if not secrets.compare_digest(user.csrf, str(form.get("csrf") or "")):
        raise HTTPException(403, "bad CSRF token; reload the page and try again")
    since = (datetime.datetime.now(datetime.UTC) - RATE_WINDOW).isoformat(timespec="seconds")
    if state.model.count_actions_since(user.login, since) >= RATE_LIMIT:
        raise HTTPException(429, f"more than {RATE_LIMIT} actions in an hour; wait a while")
    return Actor(user, refreshed)


async def read_form(request: Request) -> dict[str, str]:
    return {k: str(v) for k, v in (await request.form()).items()}


async def perform(
    request: Request, form: dict[str, str], action: str, target: str | None, operation: Operation
) -> RedirectResponse:
    """Check the member, run the operation with the installation client, log it, redirect back."""
    state = state_of(request)
    actor = await require_member(request, form)
    try:
        github = await installation_client(state)
        if github is None:
            raise HTTPException(503, "GitHub App not configured or not installed")
        result, detail = "ok", await operation(state, github, actor)
    except HTTPException as exc:
        result, detail = "error", str(exc.detail)
    except httpx.HTTPStatusError as exc:
        result, detail = "error", f"GitHub answered {exc.response.status_code} for {exc.request.url.path}"
    except Exception as exc:
        log.exception("%s by %s failed", action, actor.login)
        result, detail = "error", f"{type(exc).__name__}: {exc}"
    action_id = state.model.add_action(actor.login, action, target, result, detail)
    state.stream.publish("actions", action_id)
    log.info("%s %s %s by %s: %s", action, target or "", result, actor.login, detail)
    response = RedirectResponse(auth.safe_next(form.get("next") or "/management"), status_code=303)
    if actor.refreshed:
        auth.set_session(response, state, actor.user)
    return response


def _form_run(form: dict[str, Any]) -> int:
    try:
        return int(form.get("run_id") or "")
    except ValueError as exc:
        raise HTTPException(400, "run_id must be a number") from exc


def _form_repo(state: State, form: dict[str, Any]) -> str:
    """`docker-sonarr` or `org/docker-sonarr`; must be a repo we know so nobody dispatches elsewhere."""
    repo = state.model.repo_by_name(str(form.get("repo") or ""))
    if repo is None or not repo.is_image:
        raise HTTPException(404, f"unknown image repository {form.get('repo')!r}")
    return repo.full_name


def _form_branch(form: dict[str, Any]) -> str:
    branch = str(form.get("branch") or "").strip()
    if not branch or any(c.isspace() for c in branch) or branch.startswith("-"):
        raise HTTPException(400, "branch is required")
    return branch


def _form_scope(form: dict[str, Any]) -> str:
    scope = str(form.get("scope") or "").strip().strip("/")
    parts = scope.split("/")
    if not scope or not 1 <= len(parts) <= 3 or not all(parts):
        raise HTTPException(400, "scope must be image, image/branch or image/branch/version")
    return scope


@router.post("/rerun")
async def rerun(request: Request) -> RedirectResponse:
    form = await read_form(request)
    run_id = _form_run(form)

    async def operation(state: State, github: GitHub, actor: Actor) -> str:
        run = state.model.run(run_id)
        if run is None:
            raise HTTPException(404, f"run {run_id} unknown")
        await github.post(f"/repos/{run.repo}/actions/runs/{run_id}/rerun-failed-jobs")
        return f"re-run of failed jobs requested for {run.repo} run {run_id}"

    return await perform(request, form, "rerun", str(run_id), operation)


@router.post("/cancel")
async def cancel(request: Request) -> RedirectResponse:
    form = await read_form(request)
    run_id = _form_run(form)

    async def operation(state: State, github: GitHub, actor: Actor) -> str:
        run = state.model.run(run_id)
        if run is None:
            raise HTTPException(404, f"run {run_id} unknown")
        await github.post(f"/repos/{run.repo}/actions/runs/{run_id}/cancel")
        return f"cancel requested for {run.repo} run {run_id}"

    return await perform(request, form, "cancel", str(run_id), operation)


async def _dispatch(state: State, github: GitHub, repo: str, branch: str, reason: str, kind: str, by: str) -> str:
    image = image_name_of(repo.rsplit("/", 1)[-1]) or repo
    detail = {"reason": f"member action by {by}"}
    decision = await scheduler.Scheduler(state, github).dispatch(repo, branch, image, kind, reason, detail)
    if decision.outcome != "dispatch":
        raise HTTPException(502, str(decision.detail.get("message") or "dispatch failed"))
    return f"build.yml dispatched on {repo} {branch} with reason {reason}"


@router.post("/dispatch")
async def dispatch(request: Request) -> RedirectResponse:
    form = await read_form(request)
    state = state_of(request)
    reason = str(form.get("reason") or "manual")
    if reason not in REASONS:
        raise HTTPException(400, f"reason must be one of {', '.join(REASONS)}")
    repo, branch = _form_repo(state, form), _form_branch(form)

    async def operation(state: State, github: GitHub, actor: Actor) -> str:
        return await _dispatch(state, github, repo, branch, reason, scheduler.KIND_MANUAL, actor.login)

    return await perform(request, form, "dispatch", f"{repo}@{branch} ({reason})", operation)


@router.post("/packages")
async def packages(request: Request) -> RedirectResponse:
    """A package check now: the same dispatch as the weekly one, and it resets that branch's weekly slot."""
    form = await read_form(request)
    state = state_of(request)
    repo, branch = _form_repo(state, form), _form_branch(form)

    async def operation(state: State, github: GitHub, actor: Actor) -> str:
        return await _dispatch(state, github, repo, branch, "packages", scheduler.KIND_PACKAGES, actor.login)

    return await perform(request, form, "packages", f"{repo}@{branch}", operation)


@router.post("/skip")
async def skip(request: Request) -> RedirectResponse:
    form = await read_form(request)
    scope = _form_scope(form)
    reason = str(form.get("reason") or "").strip() or None

    async def operation(state: State, github: GitHub, actor: Actor) -> str:
        state.model.set_skip(scope, reason, actor.login)
        state.stream.publish("skips", scope)
        return f"skip flag set on {scope}" + (f": {reason}" if reason else "")

    return await perform(request, form, "skip", scope, operation)


@router.post("/unskip")
async def unskip(request: Request) -> RedirectResponse:
    form = await read_form(request)
    scope = _form_scope(form)

    async def operation(state: State, github: GitHub, actor: Actor) -> str:
        if not state.model.remove_skip(scope):
            raise HTTPException(404, f"no skip flag on {scope}")
        state.stream.publish("skips", scope)
        return f"skip flag removed from {scope}"

    return await perform(request, form, "unskip", scope, operation)


@router.post("/reconcile")
async def reconcile(request: Request) -> RedirectResponse:
    form = await read_form(request)

    async def operation(state: State, github: GitHub, actor: Actor) -> str:
        state.spawn(fleet.discover_once(state))
        return "fleet discovery started; the outcome shows on /status"

    return await perform(request, form, "reconcile", None, operation)
