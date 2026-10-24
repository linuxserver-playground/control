"""
The dashboard. Every page is computed once into a pydantic model, rendered server-side from a template,
and also served as JSON under `/api/<page>.json` so other tools (and tests) read the same numbers people
see. `/api/stream` is the server-sent events feed the live pages refresh on.
"""

import datetime
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel

from . import badge
from .backfill import last_backfill
from .db import Delivery
from .model import Build, Issue, Job, PullRequest, Release, Repo, Run, SchedulerDecision, stuck_threshold
from .state import State, state_of
from .stream import sse
from .templating import package_version, render

router = APIRouter()

KEEPALIVE_SECONDS = 15.0


def now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def html(request: Request, template: str, page: BaseModel, **extra: Any) -> HTMLResponse:
    state = state_of(request)
    return HTMLResponse(render(template, page=page, settings=state.settings, path=request.url.path, **extra))


# -- page models --------------------------------------------------------------------------------------------------


class FleetRow(BaseModel):
    repo: str
    image: str
    branch: str
    default_branch: bool
    archived: bool
    result: str | None
    """success, failure, cancelled, skipped, running, or None when no live build has been seen."""
    built_at: str | None
    run_id: int | None
    reason: str | None
    version_tag: str | None
    released: str | None
    released_at: str | None
    upstream: str | None
    drift: str = "unknown"
    next_check: str | None = None
    skip: list[str] = []


class FleetPage(BaseModel):
    rows: list[FleetRow]
    images: int
    generated_at: str


class JobView(BaseModel):
    job: Job
    elapsed_seconds: float | None
    threshold_seconds: float | None
    stuck: bool


class RunView(BaseModel):
    run: Run
    jobs: list[JobView]
    elapsed_seconds: float | None
    stuck: bool
    build: Build | None


class RunsPage(BaseModel):
    active: list[RunView]
    recent: list[Run]
    generated_at: str


class RunPage(BaseModel):
    run: Run
    jobs: list[JobView]
    builds: list[Build]
    elapsed_seconds: float | None


class QueuePage(BaseModel):
    queued: list[JobView]
    in_progress: list[JobView]
    limit: int
    in_use: int
    utilisation: float
    generated_at: str


class PullView(BaseModel):
    pull: PullRequest
    run: Run | None
    quarantine_tag: str | None


class PullsPage(BaseModel):
    pulls: list[PullView]


class IssuesPage(BaseModel):
    issues: list[Issue]


class ReleaseView(BaseModel):
    release: Release
    build: Build | None


class ReleasesPage(BaseModel):
    releases: list[ReleaseView]


class BuildPage(BaseModel):
    build: Build
    bundle: dict[str, Any] | None
    run: Run | None
    jobs: list[JobView]


class SchedulerPage(BaseModel):
    decisions: list[SchedulerDecision]


class StatusPage(BaseModel):
    version: str
    org: str
    app_slug: str | None
    app_id: int | None
    app_url: str | None
    install_url: str | None
    installation_id: int | None
    discord: bool
    events_secret: bool
    backfill_on_start: bool
    deliveries_count: int
    deliveries: list[Delivery]
    counts: dict[str, int]
    last_backfill: dict[str, Any] | None


class ManagementPage(BaseModel):
    member: str | None
    actions: list[str]


# -- helpers -----------------------------------------------------------------------------------------------------


def job_view(state: State, job: Job, now: datetime.datetime) -> JobView:
    elapsed = job.duration_seconds(now)
    threshold = None
    stuck = False
    if job.status == "in_progress":
        threshold = stuck_threshold(state.model.job_durations(job.repo, job.name))
        stuck = elapsed is not None and elapsed > threshold
    return JobView(job=job, elapsed_seconds=elapsed, threshold_seconds=threshold, stuck=stuck)


def run_elapsed(run: Run, jobs: list[Job], now: datetime.datetime) -> float | None:
    start = run.started_at or run.created_at
    if not start:
        return None
    if run.status == "completed":
        ends = [j.completed_at for j in jobs if j.completed_at]
        end = datetime.datetime.fromisoformat(max(ends)) if ends else datetime.datetime.fromisoformat(run.updated_at)
    else:
        end = now
    return max(0.0, (end - datetime.datetime.fromisoformat(start)).total_seconds())


def run_view(state: State, run: Run, now: datetime.datetime) -> RunView:
    jobs = state.model.jobs_for_run(run.id)
    views = [job_view(state, job, now) for job in jobs]
    builds = state.model.builds_for_run(run.id)
    return RunView(
        run=run,
        jobs=views,
        elapsed_seconds=run_elapsed(run, jobs, now),
        stuck=any(v.stuck for v in views),
        build=builds[0] if builds else None,
    )


def pick_release(releases: list[Release], branch: str, default_branch: str, branches: list[str]) -> Release | None:
    """Releases are tagged `[T-]<upstream>-ls<N>` (§3): a non-default branch owns tags with its prefix, the
    default branch owns the rest."""
    if branch != default_branch:
        return next((r for r in releases if r.tag.startswith(f"{branch}-")), None)
    others = [f"{b}-" for b in branches if b != default_branch]
    return next((r for r in releases if not any(r.tag.startswith(prefix) for prefix in others)), None)


def resolve_repo(state: State, name: str) -> str:
    """URLs use the short repo name (`docker-sonarr`); the tables use `org/docker-sonarr`."""
    if "/" in name:
        return name
    repo = state.model.repo_by_name(name)
    return repo.full_name if repo else f"{state.settings.github_org}/{name}"


def current_member(request: Request) -> str | None:
    """Member sign-in is a later increment; until then nobody is a member and management stays read-only."""
    return None


# -- page builders -----------------------------------------------------------------------------------------------


def fleet_page(state: State) -> FleetPage:
    rows: list[FleetRow] = []
    repos = state.model.repos(images_only=True)
    for repo in repos:
        latest = {b.branch: b for b in state.model.latest_builds_per_branch(repo.full_name, "live") if b.branch}
        branches = sorted(set(latest) | {repo.default_branch}, key=lambda b: (b != repo.default_branch, b))
        releases = state.model.releases_for_repo(repo.full_name)
        for branch in branches:
            build = latest.get(branch)
            release = pick_release(releases, branch, repo.default_branch, branches)
            rows.append(
                FleetRow(
                    repo=repo.full_name,
                    image=repo.image_name or repo.name,
                    branch=branch,
                    default_branch=branch == repo.default_branch,
                    archived=repo.archived,
                    result="running" if build and build.running else (build.result if build else None),
                    built_at=build.received_at if build else None,
                    run_id=build.run_id if build else None,
                    reason=build.reason if build else None,
                    version_tag=build.version_tag if build else None,
                    released=release.tag if release else None,
                    released_at=release.published_at if release else None,
                    upstream=build.upstream if build else None,
                    skip=["archived"] if repo.archived else [],
                )
            )
    return FleetPage(rows=rows, images=len(repos), generated_at=now_utc().isoformat(timespec="seconds"))


def runs_page(state: State) -> RunsPage:
    now = now_utc()
    return RunsPage(
        active=[run_view(state, run, now) for run in state.model.active_runs()],
        recent=state.model.recent_runs(50),
        generated_at=now.isoformat(timespec="seconds"),
    )


def run_page(state: State, run_id: int) -> RunPage:
    run = state.model.run(run_id)
    if run is None:
        raise HTTPException(404, f"run {run_id} unknown")
    now = now_utc()
    jobs = state.model.jobs_for_run(run_id)
    return RunPage(
        run=run,
        jobs=[job_view(state, job, now) for job in jobs],
        builds=state.model.builds_for_run(run_id),
        elapsed_seconds=run_elapsed(run, jobs, now),
    )


def queue_page(state: State) -> QueuePage:
    now = now_utc()
    in_progress = [job_view(state, job, now) for job in state.model.jobs_with_status("in_progress")]
    queued = [job_view(state, job, now) for job in state.model.jobs_with_status("queued")]
    limit = state.settings.concurrency_limit
    return QueuePage(
        queued=queued,
        in_progress=in_progress,
        limit=limit,
        in_use=len(in_progress),
        utilisation=round(len(in_progress) / limit, 3) if limit else 0.0,
        generated_at=now.isoformat(timespec="seconds"),
    )


def pulls_page(state: State) -> PullsPage:
    views: list[PullView] = []
    repos: dict[str, Repo | None] = {}
    for pr in state.model.open_pull_requests():
        if pr.repo not in repos:
            repos[pr.repo] = state.model.repo(pr.repo)
        repo = repos[pr.repo]
        run = state.model.latest_run_for_sha(pr.repo, pr.head_sha, "pull_request") if pr.head_sha else None
        quarantine = None
        if repo and repo.image_name and pr.head_sha:
            quarantine = (
                f"ghcr.io/{state.settings.github_org}/untrusted-pr/{repo.image_name}:pr-{pr.number}-{pr.head_sha[:7]}"
            )
        views.append(PullView(pull=pr, run=run, quarantine_tag=quarantine))
    return PullsPage(pulls=views)


def issues_page(state: State) -> IssuesPage:
    return IssuesPage(issues=state.model.open_issues())


def releases_page(state: State) -> ReleasesPage:
    return ReleasesPage(
        releases=[
            ReleaseView(release=release, build=state.model.build_for_tag(release.repo, release.tag))
            for release in state.model.recent_releases(100)
        ]
    )


def build_page(state: State, repo: str, run_id: int) -> BuildPage:
    full_name = resolve_repo(state, repo)
    build = state.model.build(full_name, run_id)
    if build is None:
        raise HTTPException(404, f"no build for {full_name} run {run_id}")
    now = now_utc()
    return BuildPage(
        build=build.model_copy(update={"bundle_json": None}),
        bundle=build.bundle,
        run=state.model.run(run_id),
        jobs=[job_view(state, job, now) for job in state.model.jobs_for_run(run_id)],
    )


def scheduler_page(state: State) -> SchedulerPage:
    return SchedulerPage(decisions=state.model.recent_decisions(200))


def status_page(state: State) -> StatusPage:
    credentials = state.credentials.load()
    return StatusPage(
        version=package_version(),
        org=state.settings.github_org,
        app_slug=credentials.slug if credentials else None,
        app_id=credentials.id if credentials else None,
        app_url=credentials.html_url if credentials else None,
        install_url=credentials.install_url if credentials else None,
        installation_id=state.installation_id,
        discord=state.discord.configured,
        events_secret=bool(state.settings.events_secret),
        backfill_on_start=state.settings.backfill_on_start,
        deliveries_count=state.db.count_deliveries(),
        deliveries=state.db.recent_deliveries(20),
        counts=state.model.counts(),
        last_backfill=last_backfill(state),
    )


MANAGEMENT_ACTIONS = ["Trigger live build", "Re-run failed jobs", "Cancel run", "Pause scheduler", "Skip image"]


def management_page(request: Request) -> ManagementPage:
    return ManagementPage(member=current_member(request), actions=MANAGEMENT_ACTIONS)


# -- routes: HTML -------------------------------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
def fleet(request: Request) -> HTMLResponse:
    return html(request, "fleet.html", fleet_page(state_of(request)), live=True)


@router.get("/runs", response_class=HTMLResponse)
def runs(request: Request) -> HTMLResponse:
    return html(request, "runs.html", runs_page(state_of(request)), live=True)


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run(request: Request, run_id: int) -> HTMLResponse:
    return html(request, "run.html", run_page(state_of(request), run_id), live=True)


@router.get("/queue", response_class=HTMLResponse)
def queue(request: Request) -> HTMLResponse:
    return html(request, "queue.html", queue_page(state_of(request)), live=True)


@router.get("/pulls", response_class=HTMLResponse)
def pulls(request: Request) -> HTMLResponse:
    return html(request, "pulls.html", pulls_page(state_of(request)))


@router.get("/issues", response_class=HTMLResponse)
def issues(request: Request) -> HTMLResponse:
    return html(request, "issues.html", issues_page(state_of(request)))


@router.get("/releases", response_class=HTMLResponse)
def releases(request: Request) -> HTMLResponse:
    return html(request, "releases.html", releases_page(state_of(request)))


@router.get("/builds/{repo:path}/{run_id:int}", response_class=HTMLResponse)
def build(request: Request, repo: str, run_id: int) -> HTMLResponse:
    return html(request, "build.html", build_page(state_of(request), repo, run_id))


@router.get("/scheduler", response_class=HTMLResponse)
def scheduler(request: Request) -> HTMLResponse:
    return html(request, "scheduler.html", scheduler_page(state_of(request)))


@router.get("/status", response_class=HTMLResponse)
def status(request: Request) -> HTMLResponse:
    return html(request, "status.html", status_page(state_of(request)))


@router.get("/management", response_class=HTMLResponse)
def management(request: Request) -> HTMLResponse:
    return html(request, "management.html", management_page(request))


# -- routes: JSON -------------------------------------------------------------------------------------------------


@router.get("/api/fleet.json")
def fleet_json(request: Request) -> FleetPage:
    return fleet_page(state_of(request))


@router.get("/api/runs.json")
def runs_json(request: Request) -> RunsPage:
    return runs_page(state_of(request))


@router.get("/api/runs/{run_id}.json")
def run_json(request: Request, run_id: int) -> RunPage:
    return run_page(state_of(request), run_id)


@router.get("/api/queue.json")
def queue_json(request: Request) -> QueuePage:
    return queue_page(state_of(request))


@router.get("/api/pulls.json")
def pulls_json(request: Request) -> PullsPage:
    return pulls_page(state_of(request))


@router.get("/api/issues.json")
def issues_json(request: Request) -> IssuesPage:
    return issues_page(state_of(request))


@router.get("/api/releases.json")
def releases_json(request: Request) -> ReleasesPage:
    return releases_page(state_of(request))


@router.get("/api/builds/{repo:path}/{run_id:int}.json")
def build_json(request: Request, repo: str, run_id: int) -> BuildPage:
    return build_page(state_of(request), repo, run_id)


@router.get("/api/scheduler.json")
def scheduler_json(request: Request) -> SchedulerPage:
    return scheduler_page(state_of(request))


@router.get("/api/status.json")
def status_json(request: Request) -> StatusPage:
    return status_page(state_of(request))


@router.get("/api/management.json")
def management_json(request: Request) -> ManagementPage:
    return management_page(request)


# -- routes: stream and badge -------------------------------------------------------------------------------------


async def event_stream(state: State) -> AsyncIterator[str]:
    yield ": connected\n\n"
    async for event in state.stream.subscribe(keepalive=KEEPALIVE_SECONDS):
        yield sse(event) if event is not None else ": keepalive\n\n"


@router.get("/api/stream")
async def stream(request: Request) -> StreamingResponse:
    return StreamingResponse(
        event_stream(state_of(request)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/badge/{image}/{tag}.svg")
def badge_svg(request: Request, image: str, tag: str) -> Response:
    build = state_of(request).model.latest_live_build(image, tag)
    return Response(
        badge.ci_badge(build.result if build else None),
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=300"},
    )
