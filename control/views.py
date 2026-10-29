"""
The dashboard. Every page is computed once into a pydantic model, rendered server-side from a template,
and also served as JSON under `/api/<page>.json` so other tools (and tests) read the same numbers people
see; the website on its own origin renders every page from that JSON (CORS for CONTROL_ALLOWED_ORIGINS is
set up in app.py). `/api/stream` is the server-sent events feed the live pages refresh on.
"""

import contextlib
import datetime
import urllib.parse
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel

from . import auth, badge, recover
from . import batch as batches
from . import scheduler as sched
from .backfill import last_backfill
from .db import Delivery
from .fleet import helper_branches, helper_repos, last_discovery
from .housekeeping import last_housekeeping
from .manage import REASONS
from .model import (
    PAGES_REASON,
    Action,
    BatchRun,
    BatchUnit,
    Build,
    Issue,
    Job,
    Manifest,
    PullRequest,
    Release,
    Repo,
    Run,
    SchedulerDecision,
    Skip,
    is_pages_job,
    stuck_threshold,
)
from .reconcile import last_reconcile
from .state import State, state_of
from .stream import Broadcaster, sse
from .templating import package_version, render

router = APIRouter()

KEEPALIVE_SECONDS = 15.0
BUILD_VERSION = Path("/build_version")
"""Written by the Dockerfile: `Linuxserver.io version: <sha7>`, the commit the running image was built from."""


def now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def html(request: Request, template: str, page: BaseModel, user: auth.User | None = None, **extra: Any) -> HTMLResponse:
    """Every page knows who is looking: members see the action forms, everyone else the same page read-only.
    `user` overrides the cookie when the caller just refreshed the membership verdict."""
    state = state_of(request)
    user = user or auth.current_user(request)
    return HTMLResponse(
        render(
            template,
            page=page,
            settings=state.settings,
            path=request.url.path,
            user=user,
            member=bool(user and user.member),
            csrf=user.csrf if user else "",
            **extra,
        )
    )


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
    """The branch's newest release tag."""
    released_at: str | None
    upstream: str | None
    """The upstream version the scheduler last resolved, else the one the last build reported."""
    drift: str = "unknown"
    """ahead (upstream newer than released), same, or unknown."""
    next_check: str | None = None
    """When the scheduler looks at this branch next; None for branches discovery has not found live."""
    last_check: str | None = None
    last_outcome: str | None = None
    source: str | None = None
    """The manifest's upstream.version.type."""
    upstream_url: str | None = None
    """Where to read the upstream release the board shows: its tag, commit or package version page when the source
    names one, else the project's releases page or website."""
    package_check: bool = True
    skip: list[str] = []
    """Skip-flag scopes that hold the branch back, plus `archived`."""


class HelperFailure(BaseModel):
    """A helper repo branch whose newest build failed."""

    branch: str
    run_id: int
    conclusion: str
    html_url: str | None
    at: str


class HelperRepo(BaseModel):
    """A `docker-*` repo that is not an image (no image.yml on its default branch), such as docker-mods, with the
    state of each branch's newest build."""

    repo: str
    name: str
    archived: bool
    branches: int
    """Branches with a push or dispatch build (skipped runs, like a guarded default branch's, do not count)."""
    success: int
    running: int
    failing: list[str]
    """Branches whose newest build failed, timed out or could not start."""
    failures: list[HelperFailure] = []
    """The same branches with the run that failed, newest first."""
    last_run_at: str | None


class FleetPage(BaseModel):
    rows: list[FleetRow]
    images: int
    helpers: list[HelperRepo] = []
    generated_at: str


class JobView(BaseModel):
    job: Job
    elapsed_seconds: float | None
    threshold_seconds: float | None
    stuck: bool
    branch: str | None = None
    """The branch the job's run builds (the queue fills it in from the run; jobs carry no branch)."""
    event: str | None = None
    """What started the run: push, pull_request, workflow_dispatch, schedule."""


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


class ImagePage(BaseModel):
    repo: Repo
    image: str
    tag: str | None
    """The release tag the page is narrowed to (`latest`, `develop`…), or None for the whole image."""
    rows: list[FleetRow]
    latest: Build | None
    builds: list[Build]
    releases: list[Release]
    open_pulls: int
    open_issues: int
    badge_url: str


class DecisionView(BaseModel):
    decision: SchedulerDecision
    info: dict[str, Any]
    """The detail JSON: versions, reason, run URL."""
    run: Run | None
    """For a dispatch: the workflow_dispatch run that appeared on the branch right after."""


class Upcoming(BaseModel):
    image: str
    branch: str
    kind: str
    at: str


class SchedulerPage(BaseModel):
    enabled: bool
    decisions: list[DecisionView]
    image: str | None
    outcome: str | None
    kind: str | None
    images: list[str]
    outcomes: list[str]
    kinds: list[str]
    upcoming: list[Upcoming]
    live_branches: int
    generated_at: str


class SkipsPage(BaseModel):
    skips: list[Skip]


class ActionsPage(BaseModel):
    actions: list[Action]
    """The public member-action log, newest first."""


class BatchProjectView(BaseModel):
    repo: str
    workflow: str
    ref: str
    schedule: str
    cap: int
    """The most `parallel` the workflow accepts."""
    live: bool
    """Dispatched for real (CONTROL_BATCH_LIVE); otherwise decisions are dry runs beside the repo's own cron."""
    note: str
    next_at: str
    parallel_now: int
    """The width a run started now would get."""
    active_run: Run | None
    last_decision: DecisionView | None
    last_dispatch: DecisionView | None
    """The newest dispatch, dry runs included, with the run it started."""
    runs: list[BatchRun]
    """The newest evaluated runs, with the units that failed."""
    failing: list[BatchUnit]
    """Units whose newest result is a failure."""


class BatchPage(BaseModel):
    enabled: bool
    capacity: batches.Capacity
    projects: list[BatchProjectView]
    decisions: list[DecisionView]
    generated_at: str


class StatusPage(BaseModel):
    version: str
    revision: str | None
    """The commit the running image was built from, when it runs from the published image."""
    org: str
    app_slug: str | None
    app_id: int | None
    app_url: str | None
    install_url: str | None
    installation_id: int | None
    discord: bool
    events_secret: bool
    backfill_on_start: bool
    scheduler: bool
    deliveries_count: int
    deliveries: list[Delivery]
    counts: dict[str, int]
    last_backfill: dict[str, Any] | None
    last_discovery: dict[str, Any] | None
    last_reconcile: dict[str, Any] | None = None
    last_housekeeping: dict[str, Any] | None = None
    db_bytes: int = 0
    healing: dict[str, Any] | None = None
    """What self-healing is retrying now (recover.status): Pages sites and failed runs it will re-run."""


class ManagementPage(BaseModel):
    member: str | None
    """The signed-in member's login; None shows the log read-only."""
    signed_in: str | None
    actions: list[Action]
    skips: list[Skip]
    branches: list[Manifest]
    """Every live branch, for the dispatch and skip forms."""
    repos: list[str]
    reasons: list[str]
    last_discovery: dict[str, Any] | None


# -- helpers -----------------------------------------------------------------------------------------------------


def job_view(state: State, job: Job, now: datetime.datetime, run: Run | None = None) -> JobView:
    elapsed = job.duration_seconds(now)
    threshold = None
    stuck = False
    if job.status == "in_progress":
        # A Pages deploy is short and rare, so its own history makes a twitchy p95; it waits the default hour.
        samples = [] if is_pages_job(job.name) else state.model.job_durations(job.repo, job.name)
        threshold = stuck_threshold(samples)
        stuck = elapsed is not None and elapsed > threshold
    return JobView(
        job=job,
        elapsed_seconds=elapsed,
        threshold_seconds=threshold,
        stuck=stuck,
        branch=run.branch if run else None,
        event=run.event if run else None,
    )


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


def drift(upstream: str | None, released: str | None) -> str:
    if upstream is None or released is None:
        return "unknown"
    return "same" if upstream == released else "ahead"


def project_website(manifest: Manifest) -> str | None:
    upstream = manifest.manifest.get("upstream")
    url = upstream.get("url") if isinstance(upstream, dict) else None
    return str(url) if url else None


def upstream_url(source: dict[str, Any], version: str | None, website: str | None = None) -> str | None:
    """The upstream release page for `version` from an image.yml `upstream.version` source. A `transform` means the
    version is not the tag as upstream wrote it (a `v` dropped, say), so the link stops at the releases page rather
    than guess a tag that 404s. Sources without a page of their own (os, command, json, …) fall back to `website`."""
    exact = bool(version) and not source.get("transform")
    v = urllib.parse.quote(version or "", safe="")
    repo = str(source.get("repo") or "")
    match source.get("type"):
        case "github_release" if repo:
            return f"https://github.com/{repo}/releases" + (f"/tag/{v}" if exact else "")
        case "codeberg_release" if repo:
            return f"https://codeberg.org/{repo}/releases" + (f"/tag/{v}" if exact else "")
        case "gitea_release" if repo and source.get("base_url"):
            return f"{str(source['base_url']).rstrip('/')}/{repo}/releases" + (f"/tag/{v}" if exact else "")
        case "github_commit" if repo:
            return f"https://github.com/{repo}/commit/{v}" if exact else f"https://github.com/{repo}/commits"
        case "gitlab_commit" if source.get("web_url"):
            base = str(source["web_url"]).rstrip("/")
            return f"{base}/-/commit/{v}" if exact else f"{base}/-/commits"
        case "npm" if source.get("package"):
            return f"https://www.npmjs.com/package/{source['package']}" + (f"/v/{v}" if exact else "")
        case "pip" if source.get("package"):
            return f"https://pypi.org/project/{source['package']}/" + (f"{v}/" if exact else "")
        case "arch_package" if source.get("package"):
            repo_name, arch = source.get("repo", "extra"), source.get("arch", "x86_64")
            return f"https://archlinux.org/packages/{repo_name}/{arch}/{source['package']}/"
    return website


# -- page builders -----------------------------------------------------------------------------------------------


def fleet_rows(state: State, repos: list[Repo]) -> list[FleetRow]:
    """One row per live branch: the manifests discovery found, or (before discovery has run for a repo) the
    default branch plus every branch that has had a live build."""
    rows: list[FleetRow] = []
    now = now_utc()
    skips = state.model.skips()
    for repo in repos:
        latest = {b.branch: b for b in state.model.latest_builds_per_branch(repo.full_name, "live") if b.branch}
        manifests = {m.branch: m for m in state.model.manifests(repo.full_name)}
        known = set(manifests) if manifests else set(latest) | {repo.default_branch}
        branches = sorted(known, key=lambda b: (b != repo.default_branch, b))
        releases = state.model.releases_for_repo(repo.full_name)
        image = repo.image_name or repo.name
        for branch in branches:
            build = latest.get(branch)
            manifest = manifests.get(branch)
            release = pick_release(releases, branch, repo.default_branch, branches)
            decision = state.model.last_decision(image, branch, sched.KIND_UPSTREAM)
            info = decision.info if decision else {}
            upstream = info.get("upstream") or (build.upstream_clean if build else None)
            released = info.get("released")
            if released is None and release is not None:
                release_tag = "latest" if branch == repo.default_branch else branch
                others = [b for b in branches if b != branch]
                released = sched.released_from_tag(release.tag, release_tag, others)
            if released is None and build is not None:
                released = build.upstream_clean
            flags = [skip.scope for skip in skips if skip.matches(image, branch, upstream)]
            scheduled = manifest is not None and state.settings.scheduler and not repo.archived
            rows.append(
                FleetRow(
                    repo=repo.full_name,
                    image=image,
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
                    upstream=upstream,
                    drift=drift(upstream, released),
                    next_check=sched.next_upstream_check(repo.full_name, branch, now).isoformat(timespec="seconds")
                    if scheduled
                    else None,
                    last_check=decision.at if decision else None,
                    last_outcome=decision.outcome if decision else None,
                    source=manifest.source_type if manifest else None,
                    upstream_url=upstream_url(manifest.version_source, upstream, project_website(manifest))
                    if manifest
                    else None,
                    package_check=manifest.package_check if manifest else True,
                    skip=(["archived"] if repo.archived else []) + flags,
                )
            )
    return rows


def helper_row(state: State, repo: Repo) -> HelperRepo:
    known = helper_branches(state, repo.full_name)
    runs = [
        r
        for r in state.model.latest_branch_runs(repo.full_name)
        if r.conclusion != "skipped"
        # A branch deleted since its last run is gone; one made after the last discovery is not listed yet.
        and (known is None or r.branch in known[1] or r.created_at > known[0])
    ]
    failed = [r for r in runs if r.conclusion in ("failure", "timed_out", "startup_failure")]
    return HelperRepo(
        repo=repo.full_name,
        name=repo.name,
        archived=repo.archived,
        branches=len(runs),
        success=sum(r.conclusion == "success" for r in runs),
        running=sum(r.status != "completed" for r in runs),
        failing=sorted(r.branch or "" for r in failed),
        failures=[
            HelperFailure(
                branch=r.branch or "", run_id=r.id, conclusion=r.conclusion or "", html_url=r.html_url, at=r.created_at
            )
            for r in sorted(failed, key=lambda r: r.created_at, reverse=True)
        ],
        last_run_at=max((r.created_at for r in runs), default=None),
    )


def fleet_page(state: State) -> FleetPage:
    helpers = helper_repos(state)
    repos = [r for r in state.model.repos(images_only=True) if r.full_name not in helpers]
    return FleetPage(
        rows=fleet_rows(state, repos),
        images=len(repos),
        helpers=[helper_row(state, r) for r in state.model.repos(images_only=True) if r.full_name in helpers],
        generated_at=now_utc().isoformat(timespec="seconds"),
    )


def image_page(state: State, name: str, tag: str | None = None) -> ImagePage:
    repo = state.model.repo_by_name(f"docker-{name}")
    if repo is None or not repo.is_image:
        raise HTTPException(404, f"image {name} unknown")
    rows = fleet_rows(state, [repo])
    builds = state.model.builds_for_image(name, 100)
    releases = state.model.releases_for_repo(repo.full_name, 100)
    if tag is not None:
        live_branches = [row.branch for row in rows]
        rows = [row for row in rows if _row_publishes(state, row, tag)]
        builds = [b for b in builds if b.mode == "live" and b.publishes(tag)]
        if tag == "latest":
            releases = [r for r in releases if _default_release(r, live_branches)]
        else:
            releases = [r for r in releases if r.tag.startswith(f"{tag}-")]
    live = [b for b in builds if b.mode == "live" and not b.running]
    return ImagePage(
        repo=repo,
        image=name,
        tag=tag,
        rows=rows,
        latest=live[0] if live else None,
        builds=builds,
        releases=releases,
        open_pulls=sum(1 for pr in state.model.open_pull_requests() if pr.repo == repo.full_name),
        open_issues=sum(1 for issue in state.model.open_issues() if issue.repo == repo.full_name),
        badge_url=f"{state.settings.public_url}/badge/{name}/{tag or 'latest'}.svg",
    )


def _row_publishes(state: State, row: FleetRow, tag: str) -> bool:
    """A fleet row owns a release tag when its last build says so, else by the branch naming convention."""
    if row.run_id is not None:
        build = state.model.build(row.repo, row.run_id)
        if build and build.release_tag:
            return build.release_tag == tag
    return (tag == "latest" and row.default_branch) or row.branch == tag


def _default_release(release: Release, live_branches: list[str]) -> bool:
    return not any(release.tag.startswith(f"{b}-") for b in live_branches if b != "latest")


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
    runs: dict[int, Run | None] = {}

    def view(job: Job) -> JobView:
        if job.run_id not in runs:
            runs[job.run_id] = state.model.run(job.run_id)
        return job_view(state, job, now, runs[job.run_id])

    in_progress = [view(job) for job in state.model.jobs_with_status("in_progress")]
    queued = [view(job) for job in state.model.jobs_with_status("queued")]
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


def build_page(state: State, owner: str, repo: str, run_id: int) -> BuildPage:
    full_name = f"{owner}/{repo}"
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


OUTCOMES = ["unchanged", "skipped", "dispatch", "error"]


def decision_view(state: State, decision: SchedulerDecision) -> DecisionView:
    run = None
    if decision.outcome == "dispatch" and not decision.info.get("dry_run"):
        if decision.kind == batches.KIND_BATCH:
            full_name: str | None = f"{state.settings.github_org}/{decision.image}"
        else:
            repo = state.model.repo_by_name(f"docker-{decision.image}")
            full_name = repo.full_name if repo else None
        if full_name:
            pages = decision.info.get("dispatch_reason") == PAGES_REASON
            run = state.model.run_after(full_name, decision.branch, "workflow_dispatch", decision.at, pages=pages)
    return DecisionView(decision=decision, info=decision.info, run=run)


def batch_page(state: State) -> BatchPage:
    now = now_utc()
    org = state.settings.github_org
    projects: list[BatchProjectView] = []
    for project in batches.PROJECTS:
        full_name = f"{org}/{project.repo}"
        last = state.model.last_decision(project.repo, project.ref, batches.KIND_BATCH)
        dispatches = state.model.recent_decisions(1, project.repo, "dispatch", batches.KIND_BATCH)
        active = batches.active_runs(state.model, org, project, now)
        projects.append(
            BatchProjectView(
                repo=project.repo,
                workflow=project.workflow,
                ref=project.ref,
                schedule=project.schedule,
                cap=project.cap,
                live=project.repo in state.settings.batch_live,
                note=project.note,
                next_at=project.next_slot(now).isoformat(timespec="seconds"),
                parallel_now=batches.capacity(state, now, exclude=project.repo).width(project.cap),
                active_run=active[0] if active else None,
                last_decision=decision_view(state, last) if last else None,
                last_dispatch=decision_view(state, dispatches[0]) if dispatches else None,
                runs=state.model.batch_runs(full_name, 10),
                failing=state.model.batch_units(full_name, "fail"),
            )
        )
    return BatchPage(
        enabled=state.settings.scheduler,
        capacity=batches.capacity(state, now),
        projects=projects,
        decisions=[decision_view(state, d) for d in state.model.recent_decisions(50, kind=batches.KIND_BATCH)],
        generated_at=now.isoformat(timespec="seconds"),
    )


def running_revision(path: Path = BUILD_VERSION) -> str | None:
    try:
        first = path.read_text().splitlines()[0]
    except (OSError, IndexError):
        return None
    return first.rpartition(":")[2].strip() or None


def scheduler_page(
    state: State, image: str | None = None, outcome: str | None = None, kind: str | None = None
) -> SchedulerPage:
    now = now_utc()
    manifests = state.model.manifests()
    recent = state.model.recent_decisions(200, image or None, outcome or None, kind or None)
    return SchedulerPage(
        enabled=state.settings.scheduler,
        decisions=[decision_view(state, d) for d in recent],
        image=image or None,
        outcome=outcome or None,
        kind=kind or None,
        images=state.model.decision_images(),
        outcomes=OUTCOMES,
        kinds=list(sched.KINDS),
        upcoming=[
            Upcoming(**row)
            for row in sched.upcoming(
                manifests, now, mod_manifests=state.model.mod_manifests(scheduled=True), org=state.settings.github_org
            )
        ],
        live_branches=len(manifests),
        generated_at=now.isoformat(timespec="seconds"),
    )


def skips_page(state: State) -> SkipsPage:
    return SkipsPage(skips=state.model.skips())


def actions_page(state: State) -> ActionsPage:
    return ActionsPage(actions=state.model.recent_actions(100))


def status_page(state: State) -> StatusPage:
    credentials = state.credentials.load()
    return StatusPage(
        version=package_version(),
        revision=running_revision(),
        org=state.settings.github_org,
        app_slug=credentials.slug if credentials else None,
        app_id=credentials.id if credentials else None,
        app_url=credentials.html_url if credentials else None,
        install_url=credentials.install_url if credentials else None,
        installation_id=state.installation_id,
        discord=state.discord.configured,
        events_secret=bool(state.settings.events_secret),
        backfill_on_start=state.settings.backfill_on_start,
        scheduler=state.settings.scheduler,
        deliveries_count=state.db.count_deliveries(),
        deliveries=state.db.recent_deliveries(20),
        counts=state.model.counts(),
        last_backfill=last_backfill(state),
        last_discovery=last_discovery(state),
        last_reconcile=last_reconcile(state),
        last_housekeeping=last_housekeeping(state),
        db_bytes=state.db.size_bytes(),
        healing=recover.status(state),
    )


def management_page(state: State, user: auth.User | None) -> ManagementPage:
    manifests = state.model.manifests()
    helpers = helper_repos(state)
    repos = sorted({m.repo for m in manifests} | {r.full_name for r in state.model.repos(images_only=True)} - helpers)
    return ManagementPage(
        member=user.login if user and user.member else None,
        signed_in=user.login if user else None,
        actions=state.model.recent_actions(100),
        skips=state.model.skips(),
        branches=manifests,
        repos=repos,
        reasons=list(REASONS),
        last_discovery=last_discovery(state),
    )


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


@router.get("/builds/{owner}/{repo}/{run_id}", response_class=HTMLResponse)
def build(request: Request, owner: str, repo: str, run_id: int) -> HTMLResponse:
    return html(request, "build.html", build_page(state_of(request), owner, repo, run_id))


@router.get("/images/{name}", response_class=HTMLResponse)
def image(request: Request, name: str) -> HTMLResponse:
    return html(request, "image.html", image_page(state_of(request), name), live=True)


@router.get("/images/{name}/{tag}", response_class=HTMLResponse)
def image_tag(request: Request, name: str, tag: str) -> HTMLResponse:
    return html(request, "image.html", image_page(state_of(request), name, tag), live=True)


@router.get("/scheduler", response_class=HTMLResponse)
def scheduler(
    request: Request, image: str | None = None, outcome: str | None = None, kind: str | None = None
) -> HTMLResponse:
    return html(request, "scheduler.html", scheduler_page(state_of(request), image, outcome, kind), live=True)


@router.get("/batch", response_class=HTMLResponse)
def batch(request: Request) -> HTMLResponse:
    return html(request, "batch.html", batch_page(state_of(request)), live=True)


@router.get("/status", response_class=HTMLResponse)
def status(request: Request) -> HTMLResponse:
    return html(request, "status.html", status_page(state_of(request)))


@router.get("/management", response_class=HTMLResponse)
async def management(request: Request) -> HTMLResponse:
    """The action forms show for whoever the session cookie says is a member; every action confirms it with GitHub."""
    state = state_of(request)
    user = auth.current_user(request)
    return html(request, "management.html", management_page(state, user), user=user, live=True)


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


@router.get("/api/builds/{owner}/{repo}/{run_id}.json")
def build_json(request: Request, owner: str, repo: str, run_id: int) -> BuildPage:
    return build_page(state_of(request), owner, repo, run_id)


@router.get("/api/images/{name}.json")
def image_json(request: Request, name: str) -> ImagePage:
    return image_page(state_of(request), name)


@router.get("/api/images/{name}/{tag}.json")
def image_tag_json(request: Request, name: str, tag: str) -> ImagePage:
    return image_page(state_of(request), name, tag)


@router.get("/api/scheduler.json")
def scheduler_json(
    request: Request, image: str | None = None, outcome: str | None = None, kind: str | None = None
) -> SchedulerPage:
    return scheduler_page(state_of(request), image, outcome, kind)


@router.get("/api/batch.json")
def batch_json(request: Request) -> BatchPage:
    return batch_page(state_of(request))


@router.get("/api/skips.json")
def skips_json(request: Request) -> SkipsPage:
    return skips_page(state_of(request))


@router.get("/api/actions.json")
def actions_json(request: Request) -> ActionsPage:
    return actions_page(state_of(request))


@router.get("/api/status.json")
def status_json(request: Request) -> StatusPage:
    return status_page(state_of(request))


@router.get("/api/management.json")
def management_json(request: Request) -> ManagementPage:
    return management_page(state_of(request), auth.current_user(request))


# -- routes: stream and badge -------------------------------------------------------------------------------------


async def event_stream(broadcaster: Broadcaster, keepalive: float = KEEPALIVE_SECONDS) -> AsyncGenerator[str]:
    yield ": connected\n\n"
    # aclosing: when the client goes away the subscription is dropped now, not at garbage collection.
    async with contextlib.aclosing(broadcaster.subscribe(keepalive=keepalive)) as events:
        async for event in events:
            yield sse(event) if event is not None else ": keepalive\n\n"


@router.get("/api/stream")
async def stream(request: Request) -> StreamingResponse:
    return StreamingResponse(
        event_stream(state_of(request).stream),
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
