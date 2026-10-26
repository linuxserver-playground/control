"""
What signed-in members may do: re-run or cancel a run, dispatch a build, set and clear skip flags,
trigger a package check, re-read the fleet manifests. Every action refuses anyone who is not an active
member (401 anonymous, 403 otherwise), is rate limited per user, and writes an `actions` row whatever the
outcome, which `/management` shows to everyone. Each action is one `prepare_*` function that validates the
fields and returns the operation; two thin route families run it: `POST /manage/{action}` (the HTML forms,
CSRF token as the `csrf` field, redirect back) and `POST /api/actions/{action}` (the website, JSON body,
CSRF token as `X-CSRF-Token`, JSON result).
"""

import datetime
import logging
from collections.abc import Awaitable, Callable

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from . import auth, fleet, scheduler
from .github import GitHub, installation_client
from .model import image_name_of
from .state import State, state_of

log = logging.getLogger(__name__)

router = APIRouter()

RATE_LIMIT = 30
EDIT_RATE_LIMIT = 120
RATE_WINDOW = datetime.timedelta(hours=1)
REASONS = ("manual", "rebuild", "packages", "upstream")
"""The `reason` choices build.yml's workflow_dispatch accepts."""

Fields = dict[str, str]
Operation = Callable[[State, GitHub, "Actor"], Awaitable[str]]


class Actor:
    """The verified member behind a request, with the refreshed session to set on the response."""

    def __init__(self, user: auth.User, refreshed: bool) -> None:
        self.user = user
        self.refreshed = refreshed

    @property
    def login(self) -> str:
        return self.user.login


class Prepared:
    """A validated action: what to log it as and the operation that performs it."""

    def __init__(self, action: str, target: str | None, operation: Operation) -> None:
        self.action = action
        self.target = target
        self.operation = operation


class ActionRequest(BaseModel):
    """The JSON body of `POST /api/actions/{action}`: the same fields as the form of the same name."""

    run_id: int | None = None
    repo: str | None = None
    branch: str | None = None
    reason: str | None = None
    scope: str | None = None

    def fields(self) -> Fields:
        return {k: str(v) for k, v in self.model_dump(exclude_none=True).items()}


class ActionResult(BaseModel):
    ok: bool
    action_id: int
    detail: str


class Outcome:
    def __init__(self, actor: Actor, action_id: int, result: str, detail: str) -> None:
        self.actor = actor
        self.action_id = action_id
        self.result = result
        self.detail = detail


async def require_member(request: Request, csrf: str | None, editing: bool = False) -> Actor:
    """401 for nobody, 403 for a non-member or a bad CSRF token, 429 over the rate limit (the website editor's
    saves, `editing`, count against their own, larger one)."""
    state = state_of(request)
    user = auth.current_user(request)
    if user is None:
        raise HTTPException(401, "sign in first")
    user, refreshed = await auth.refresh_if_stale(state, user)
    if not user.member:
        raise HTTPException(403, f"{user.login} is not a member of {state.settings.github_org}")
    if not auth.csrf_ok(user, csrf):
        raise HTTPException(403, "bad CSRF token; reload the page and try again")
    since = (datetime.datetime.now(datetime.UTC) - RATE_WINDOW).isoformat(timespec="seconds")
    limit = EDIT_RATE_LIMIT if editing else RATE_LIMIT
    if state.model.count_actions_since(user.login, since, editing) >= limit:
        raise HTTPException(429, f"more than {limit} {'edits' if editing else 'actions'} in an hour; wait a while")
    return Actor(user, refreshed)


async def read_form(request: Request) -> Fields:
    return {k: str(v) for k, v in (await request.form()).items()}


async def execute(request: Request, csrf: str | None, prepared: Prepared) -> Outcome:
    """Check the member, run the operation with the installation client, log it whatever happened."""
    state = state_of(request)
    actor = await require_member(request, csrf)
    try:
        github = await installation_client(state)
        if github is None:
            raise HTTPException(503, "GitHub App not configured or not installed")
        result, detail = "ok", await prepared.operation(state, github, actor)
    except HTTPException as exc:
        result, detail = "error", str(exc.detail)
    except httpx.HTTPStatusError as exc:
        result, detail = "error", f"GitHub answered {exc.response.status_code} for {exc.request.url.path}"
    except Exception as exc:
        log.exception("%s by %s failed", prepared.action, actor.login)
        result, detail = "error", f"{type(exc).__name__}: {exc}"
    action_id = state.model.add_action(actor.login, prepared.action, prepared.target, result, detail)
    state.stream.publish("actions", action_id)
    log.info("%s %s %s by %s: %s", prepared.action, prepared.target or "", result, actor.login, detail)
    return Outcome(actor, action_id, result, detail)


# -- fields -------------------------------------------------------------------------------------------------------


def _form_run(form: Fields) -> int:
    try:
        return int(form.get("run_id") or "")
    except ValueError as exc:
        raise HTTPException(400, "run_id must be a number") from exc


def _form_repo(state: State, form: Fields) -> str:
    """`docker-sonarr` or `org/docker-sonarr`; must be a repo we know so nobody dispatches elsewhere."""
    repo = state.model.repo_by_name(str(form.get("repo") or ""))
    if repo is None or not repo.is_image:
        raise HTTPException(404, f"unknown image repository {form.get('repo')!r}")
    return repo.full_name


def _form_branch(form: Fields) -> str:
    branch = str(form.get("branch") or "").strip()
    if not branch or any(c.isspace() for c in branch) or branch.startswith("-"):
        raise HTTPException(400, "branch is required")
    return branch


def _form_scope(form: Fields) -> str:
    scope = str(form.get("scope") or "").strip().strip("/")
    parts = scope.split("/")
    if not scope or not 1 <= len(parts) <= 3 or not all(parts):
        raise HTTPException(400, "scope must be image, image/branch or image/branch/version")
    return scope


# -- actions ------------------------------------------------------------------------------------------------------


def prepare_rerun(state: State, form: Fields) -> Prepared:
    run_id = _form_run(form)

    async def operation(state: State, github: GitHub, actor: Actor) -> str:
        run = state.model.run(run_id)
        if run is None:
            raise HTTPException(404, f"run {run_id} unknown")
        await github.post(f"/repos/{run.repo}/actions/runs/{run_id}/rerun-failed-jobs")
        return f"re-run of failed jobs requested for {run.repo} run {run_id}"

    return Prepared("rerun", str(run_id), operation)


def prepare_cancel(state: State, form: Fields) -> Prepared:
    run_id = _form_run(form)

    async def operation(state: State, github: GitHub, actor: Actor) -> str:
        run = state.model.run(run_id)
        if run is None:
            raise HTTPException(404, f"run {run_id} unknown")
        await github.post(f"/repos/{run.repo}/actions/runs/{run_id}/cancel")
        return f"cancel requested for {run.repo} run {run_id}"

    return Prepared("cancel", str(run_id), operation)


async def _dispatch(state: State, github: GitHub, repo: str, branch: str, reason: str, kind: str, by: str) -> str:
    image = image_name_of(repo.rsplit("/", 1)[-1]) or repo
    detail = {"reason": f"member action by {by}"}
    decision = await scheduler.Scheduler(state, github).dispatch(repo, branch, image, kind, reason, detail)
    if decision.outcome != "dispatch":
        raise HTTPException(502, str(decision.detail.get("message") or "dispatch failed"))
    return f"build.yml dispatched on {repo} {branch} with reason {reason}"


def prepare_dispatch(state: State, form: Fields) -> Prepared:
    reason = str(form.get("reason") or "manual")
    if reason not in REASONS:
        raise HTTPException(400, f"reason must be one of {', '.join(REASONS)}")
    repo, branch = _form_repo(state, form), _form_branch(form)

    async def operation(state: State, github: GitHub, actor: Actor) -> str:
        return await _dispatch(state, github, repo, branch, reason, scheduler.KIND_MANUAL, actor.login)

    return Prepared("dispatch", f"{repo}@{branch} ({reason})", operation)


def prepare_packages(state: State, form: Fields) -> Prepared:
    """A package check now: the same dispatch as the weekly one, and it resets that branch's weekly slot."""
    repo, branch = _form_repo(state, form), _form_branch(form)

    async def operation(state: State, github: GitHub, actor: Actor) -> str:
        return await _dispatch(state, github, repo, branch, "packages", scheduler.KIND_PACKAGES, actor.login)

    return Prepared("packages", f"{repo}@{branch}", operation)


def prepare_skip(state: State, form: Fields) -> Prepared:
    scope = _form_scope(form)
    reason = str(form.get("reason") or "").strip() or None

    async def operation(state: State, github: GitHub, actor: Actor) -> str:
        state.model.set_skip(scope, reason, actor.login)
        state.stream.publish("skips", scope)
        return f"skip flag set on {scope}" + (f": {reason}" if reason else "")

    return Prepared("skip", scope, operation)


def prepare_unskip(state: State, form: Fields) -> Prepared:
    scope = _form_scope(form)

    async def operation(state: State, github: GitHub, actor: Actor) -> str:
        if not state.model.remove_skip(scope):
            raise HTTPException(404, f"no skip flag on {scope}")
        state.stream.publish("skips", scope)
        return f"skip flag removed from {scope}"

    return Prepared("unskip", scope, operation)


def prepare_reconcile(state: State, form: Fields) -> Prepared:
    async def operation(state: State, github: GitHub, actor: Actor) -> str:
        state.spawn(fleet.discover_once(state))
        return "fleet discovery started; the outcome shows on /status"

    return Prepared("reconcile", None, operation)


ACTIONS: dict[str, Callable[[State, Fields], Prepared]] = {
    "rerun": prepare_rerun,
    "cancel": prepare_cancel,
    "dispatch": prepare_dispatch,
    "packages": prepare_packages,
    "skip": prepare_skip,
    "unskip": prepare_unskip,
    "reconcile": prepare_reconcile,
}


def prepare(state: State, action: str, fields: Fields) -> Prepared:
    if action not in ACTIONS:
        raise HTTPException(404, f"unknown action {action!r}")
    return ACTIONS[action](state, fields)


# -- routes -------------------------------------------------------------------------------------------------------


@router.post("/manage/{action}")
async def manage_form(request: Request, action: str) -> RedirectResponse:
    """The HTML forms: fields and the CSRF token in the body, then back to the page (`next`)."""
    state = state_of(request)
    form = await read_form(request)
    outcome = await execute(request, form.get("csrf"), prepare(state, action, form))
    target = auth.safe_next(form.get("next") or "/management", state.settings.allowed_origins)
    response = RedirectResponse(target, status_code=303)
    if outcome.actor.refreshed:
        auth.set_session(response, state, outcome.actor.user)
    return response


@router.post("/api/actions/{action}")
async def manage_json(request: Request, action: str, body: ActionRequest | None = None) -> JSONResponse:
    """The website: a JSON body, the CSRF token from `/api/me` as `X-CSRF-Token`, and the logged outcome back
    (`ok: false` with the detail when GitHub or the check refused; 400/404 for bad fields, 401/403/429 as above)."""
    state = state_of(request)
    fields = body.fields() if body else {}
    prepared = prepare(state, action, fields)
    outcome = await execute(request, request.headers.get("X-CSRF-Token"), prepared)
    result = ActionResult(ok=outcome.result == "ok", action_id=outcome.action_id, detail=outcome.detail)
    response = JSONResponse(result.model_dump())
    if outcome.actor.refreshed:
        auth.set_session(response, state, outcome.actor.user)
    return response
