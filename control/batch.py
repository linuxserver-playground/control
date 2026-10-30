"""
Batch build projects: repositories of the org that are not images but build wide matrices on the same runners
(selkies-layers, proot-apps, wheelie). DECISIONS: they share the org, their builds are capped so they always
leave runner slots free for image, hub and site builds, and control starts each run with its matrix width (the
workflow's `parallel` input, which becomes its `max-parallel`) worked out from the jobs already running.

Sizing: `parallel = clamp(limit - reserve - busy, minimum, min(maximum, the workflow's own cap))`, where `busy` is

- every job in progress outside the batch projects;
- every job queued outside them: an image build waiting for a runner takes one within seconds;
- for each batch project with a run going, the width it was started with or its jobs in progress, whichever is
  more: between two matrix stages a run briefly holds nothing, then takes its width again. A push or cron run was
  not started by control, so it holds the workflow default, not the width of control's last dispatch.

Queued jobs of the batch projects are left out. A run holds most of its matrix back with `max-parallel` and GitHub
shows those jobs as queued (a proot-apps package check queues about a hundred), so they are not demand and never
stop anything.

Schedule: each project has a fixed slot (PROJECTS). When it comes round:

1. a run of the project still going: look again every RETRY until the deferral window closes, then skip the slot;
2. wheelie only: run its gap check (the vendored `scripts/gaps.py`, HTTP only, no runner); no gap, no dispatch;
3. size the run. With fewer than `minimum` slots free the fleet is busy: look again every RETRY, and when the
   window closes start at the minimum anyway, so a batch always progresses;
4. `workflow_dispatch` the workflow with `parallel`, for projects in CONTROL_BATCH_LIVE; the others are recorded
   as dry runs while their own cron still fires.

Every outcome is a `scheduler_decisions` row of kind `batch` (image = the repository name, branch = the ref).

Watching: each finished run is evaluated once. Every job is a unit (a selkies layer-distro-arch, a proot-apps app,
a wheelie distro and arch) whose pass or fail state is kept in `batch_units`. Discord hears when a unit goes from
passing (or unknown) to failing, and once when it recovers; a unit that stays red is silent. Discord also hears
when control cannot dispatch a run, when a dispatched run never appears or never starts, and when a run is far
past its usual length. Successful runs and deferrals for a busy fleet post nothing.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import BaseModel

from . import events
from .model import BatchRun, BatchUnit, Job, Model, Run, SchedulerDecision, p95
from .vendor import wheelie_gaps as gaps

if TYPE_CHECKING:
    from .github import GitHub
    from .scheduler import Decision, Scheduler
    from .state import State

log = logging.getLogger(__name__)

KIND_BATCH = "batch"
DAY = datetime.timedelta(days=1)
HOUR = datetime.timedelta(hours=1)
RETRY = datetime.timedelta(minutes=10)
"""How often a deferred slot is looked at again."""
DEFER = datetime.timedelta(hours=3)
"""How long after its slot a project may wait for a busy fleet or its own previous run (half the period at most)."""
PENDING = datetime.timedelta(minutes=15)
"""A dispatch whose run has not appeared yet still holds its width; past this, it is reported as never started."""
NOT_STARTED = datetime.timedelta(hours=1)
"""A run still queued this long after it was created is reported."""
OVERDUE_FLOOR = datetime.timedelta(hours=3)
OVERDUE_UNKNOWN = datetime.timedelta(hours=12)
"""A run is overdue past twice its usual length (p95 of recent successes), at least an hour more and at least
OVERDUE_FLOOR; with fewer than three past runs to go by, past OVERDUE_UNKNOWN."""
RETIRE_AFTER = datetime.timedelta(days=3)
"""A failing unit that no run of its workflow has built for this long, while that workflow kept running, is gone:
an app disabled or removed, a layer dropped, a wheel PyPI now ships. Its failure is dropped quietly."""
STALE_RUN = datetime.timedelta(hours=48)
"""An 'active' run older than this lost its completion webhook; it neither blocks a project nor holds slots."""
JOB_MAX = datetime.timedelta(hours=6)
QUEUE_MAX = datetime.timedelta(hours=24)
"""GitHub ends a job after 6 hours and drops one queued for 24; older open jobs are lost webhooks."""
SETTLE = datetime.timedelta(minutes=10)
"""A finished run whose jobs are not all marked finished waits this long for their webhooks (and heal.py)."""
LOOKBACK = datetime.timedelta(days=3)
DEFAULT_PARALLEL = 4
"""What a run control did not start (a push, a cron, a hand dispatch) uses: the workflows' default."""
GAP_TIMEOUT = 180.0
GAP_ALERT_AFTER = 6
"""Gap-check failures in a row (PyPI or GitHub unreachable) before Discord hears; a failed dispatch is reported
at once."""
FAILED = frozenset({"failure", "timed_out", "startup_failure"})
WATCH_SINCE = "batch_watch_since"
"""kv key: when control started watching batch runs. Runs that finished before are evaluated without a message,
so the first start learns every unit's state instead of reporting old failures as new."""
RUN_UNIT = "(run)"
"""The unit for a run that failed before any job did (a startup failure, a broken workflow file)."""


@dataclass(frozen=True)
class Project:
    repo: str
    """Repository name in the org; also the decision's `image`."""
    workflow: str
    """The workflow file control dispatches."""
    ref: str
    every: datetime.timedelta
    offset: datetime.timedelta
    """The slot, from 00:00 UTC: daily projects keep their old cron's time, so they stay apart."""
    cap: int
    """The most `parallel` the workflow accepts (it clamps the input itself)."""
    workflows: tuple[str, ...]
    """Workflow names (as runs carry them) that are this project's batch work: a run of any of them blocks a new
    start, holds slots, and is watched for failures."""
    gaps: bool = False
    """Run wheelie's gap check first and dispatch only when it finds a missing wheel."""
    shared_units: bool = False
    """The workflows build the same units under the same job names, so a unit that passes in one of them is fixed in
    all: proot-apps' package check fails an app, a push fixes it, and build-on-commit's pass clears the failure."""
    note: str = ""

    @property
    def defer_window(self) -> datetime.timedelta:
        return min(DEFER, self.every / 2)

    def last_slot(self, now: datetime.datetime) -> datetime.datetime:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0) + self.offset
        return start + self.every * ((now - start) // self.every)

    def next_slot(self, now: datetime.datetime) -> datetime.datetime:
        return self.last_slot(now) + self.every

    @property
    def schedule(self) -> str:
        minutes = int(self.offset.total_seconds() // 60)
        if self.every == HOUR:
            return f"hourly at :{minutes % 60:02d}"
        return f"daily at {minutes // 60:02d}:{minutes % 60:02d} UTC"


PROJECTS: tuple[Project, ...] = (
    Project(
        repo="proot-apps",
        workflow="package-check.yml",
        ref="master",
        every=DAY,
        offset=datetime.timedelta(0),
        cap=16,
        workflows=("Daily Package check", "build-on-commit"),
        shared_units=True,
        note="Daily package check of every app; it publishes the apps whose packages changed in the same pass. "
        "Its cron runs only where SCHEDULED_BY_CONTROL is not 'true' (forks).",
    ),
    Project(
        repo="selkies-layers",
        workflow="build.yml",
        ref="main",
        every=DAY,
        offset=datetime.timedelta(hours=4, minutes=30),
        cap=16,
        workflows=("Build Layers",),
        note="Daily rebuild of every layer, distro and arch (about 46 build jobs). The workflow clamps parallel "
        "to 1-16.",
    ),
    Project(
        repo="wheelie",
        workflow="build.yml",
        ref="main",
        every=HOUR,
        offset=datetime.timedelta(minutes=20),
        cap=12,
        workflows=("Build",),
        gaps=True,
        note="Control runs the gap check hourly and dispatches only when PyPI and the index lack a wheel; the "
        "build has one job per distro and arch with a gap.",
    ),
)


def project_named(name: str) -> Project | None:
    return next((p for p in PROJECTS if p.repo == name), None)


def _at(value: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(value)


def _iso(value: datetime.datetime) -> str:
    return value.isoformat(timespec="seconds")


# -- sizing -------------------------------------------------------------------------------------------------------


class Capacity(BaseModel):
    limit: int
    reserve: int
    minimum: int
    maximum: int
    running: int
    """Jobs in progress outside the batch projects."""
    waiting: int
    """Jobs queued outside the batch projects: each takes a slot as soon as one frees."""
    held: int
    """Jobs of batch projects queued behind their own max-parallel: shown, never counted."""
    batch: dict[str, int]
    """Slots each batch project with a run going holds: its width or its jobs in progress, whichever is more."""
    free: int
    """limit - reserve - running - waiting - the batch slots: what a new batch run may take."""

    def width(self, cap: int) -> int:
        return max(self.minimum, min(self.free, self.maximum, cap))

    @property
    def busy(self) -> bool:
        """Fewer slots free than the narrowest run: starting now would eat into the reserve."""
        return self.free < self.minimum


def active_runs(model: Model, org: str, project: Project, now: datetime.datetime) -> list[Run]:
    """The project's batch runs that have not finished (pull request runs and lost webhooks aside), newest first."""
    repo = f"{org}/{project.repo}"
    return [
        run
        for run in model.active_runs()
        if run.repo == repo
        and run.event != "pull_request"
        and run.workflow_name in project.workflows
        and now - _at(run.created_at) < STALE_RUN
    ]


def last_live_dispatch(model: Model, project: Project) -> SchedulerDecision | None:
    """The newest dispatch control really sent for the project (dry runs aside)."""
    for decision in model.recent_decisions(20, project.repo, "dispatch", KIND_BATCH):
        if decision.image == project.repo and not decision.info.get("dry_run"):
            return decision
    return None


def capacity(state: State, now: datetime.datetime, exclude: str | None = None) -> Capacity:
    """The org's runner slots right now, from the jobs control holds from `workflow_job` webhooks. `exclude` is the
    project being sized: only its jobs in progress count (a pull request build, say), never its width."""
    settings = state.settings
    org = settings.github_org
    repos = {f"{org}/{p.repo}": p for p in PROJECTS}
    running = waiting = held = 0
    in_progress: dict[str, int] = {}
    for job in state.model.open_jobs(_iso(now - JOB_MAX), _iso(now - QUEUE_MAX)):
        if job.repo in repos:
            if job.status == "in_progress":
                in_progress[job.repo] = in_progress.get(job.repo, 0) + 1
            else:
                held += 1
        elif job.status == "in_progress":
            running += 1
        else:
            waiting += 1
    batch: dict[str, int] = {}
    for repo, project in repos.items():
        width = 0
        if project.repo != exclude:
            runs = active_runs(state.model, org, project, now)
            last = last_live_dispatch(state.model, project)
            # Only a dispatched run carries the width control chose; a push or cron run has the workflow default.
            dispatched = [r for r in runs if r.event in (None, "workflow_dispatch")]
            if last and (now - _at(last.at) < PENDING or any(_at(r.created_at) >= _at(last.at) for r in dispatched)):
                width = int(last.info.get("parallel") or DEFAULT_PARALLEL)
            elif runs:
                width = DEFAULT_PARALLEL
        if slots := max(in_progress.get(repo, 0), width):
            batch[project.repo] = slots
    free = settings.concurrency_limit - settings.batch_reserve - running - waiting - sum(batch.values())
    return Capacity(
        limit=settings.concurrency_limit,
        reserve=settings.batch_reserve,
        minimum=settings.batch_min_parallel,
        maximum=settings.batch_max_parallel,
        running=running,
        waiting=waiting,
        held=held,
        batch=batch,
        free=free,
    )


def overdue_after(samples: list[float]) -> datetime.timedelta:
    if len(samples) < 3:
        return OVERDUE_UNKNOWN
    usual = datetime.timedelta(seconds=p95(samples))
    return max(usual * 2, usual + HOUR, OVERDUE_FLOOR)


def describe(exc: Exception) -> str:
    """An HTTP error as a person needs it: the status and GitHub's own message (a 403 is a missing permission)."""
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            message = exc.response.json().get("message")
        except ValueError:
            message = None
        text = f"HTTP {exc.response.status_code}" + (f": {message}" if message else "")
        if exc.response.status_code in (403, 404):
            text += " (does the GitHub App have Actions write on this repository?)"
        return text
    return f"{type(exc).__name__}: {exc}"


# -- the batch scheduler ------------------------------------------------------------------------------------------


class Batch:
    """The batch half of the scheduler: it records through `Scheduler.record` so the decision log, the stream
    and the dry-run switch are shared."""

    def __init__(self, scheduler: Scheduler, pypi_fetch: Callable[[str], bytes | None] = gaps.http_fetch) -> None:
        self.scheduler = scheduler
        self.state = scheduler.state
        self.model = scheduler.model
        self.pypi_fetch = pypi_fetch

    @property
    def github(self) -> GitHub | None:
        return self.scheduler.github

    def now(self) -> datetime.datetime:
        return self.scheduler.clock()

    def full(self, project: Project) -> str:
        return f"{self.state.settings.github_org}/{project.repo}"

    def live(self, project: Project) -> bool:
        return project.repo in self.state.settings.batch_live

    def record(self, project: Project, outcome: str, detail: dict[str, Any]) -> Decision:
        return self.scheduler.record(project.repo, project.ref, KIND_BATCH, outcome, detail)

    def due(self, project: Project, now: datetime.datetime) -> bool:
        """Due once per slot, plus each `retry_at` a deferral or error left. A slot missed for longer than its
        deferral window (control was down, or just deployed) is left for the next one rather than run late."""
        slot = project.last_slot(now)
        last = self.model.last_decision(project.repo, project.ref, KIND_BATCH)
        if last is None or _at(last.at) < slot:
            return now - slot <= project.defer_window
        retry = last.info.get("retry_at")
        return bool(retry) and now >= _at(str(retry))

    async def tick(self) -> list[Decision]:
        now = self.now()
        decisions = [await self.check(project) for project in PROJECTS if self.due(project, now)]
        if not self.scheduler.dry_run:
            await self.watch()
        return decisions

    # -- one check --------------------------------------------------------------------------------------------------

    async def check(self, project: Project, forced: bool = False) -> Decision:
        """The slot's decision. `forced` (the CLI) acts now: no waiting for a busy fleet."""
        now = self.now()
        slot = project.last_slot(now)
        may_wait = not forced and now + RETRY <= slot + project.defer_window
        detail: dict[str, Any] = {"workflow": project.workflow, "slot": _iso(slot), "live": self.live(project)}
        if forced:
            detail["forced"] = True

        runs = active_runs(self.model, self.state.settings.github_org, project, now)
        if runs:
            detail.update(reason="previous run in progress", run_id=runs[0].id, run_url=runs[0].html_url)
            if may_wait:
                detail.update(deferred=True, retry_at=_iso(now + RETRY))
            else:
                detail["reason"] += ": slot skipped"
            return self.record(project, "skipped", detail)

        legs = None
        if project.gaps:
            try:
                found = await self.find_gaps(project)
            except Exception as exc:
                detail.update(stage="gap check", message=describe(exc))
                return await self.error(project, detail, may_wait, now)
            detail["gaps"] = found
            if not found:
                detail["reason"] = "no gaps: PyPI or the index has every wheel"
                return self.record(project, "unchanged", detail)
            legs = len(found)
            if failed := self.failed_recently(project, found, now):
                detail.update(failed)
                return self.record(project, "skipped", detail)

        cap = capacity(self.state, now, exclude=project.repo)
        width = cap.width(project.cap) if legs is None else min(cap.width(project.cap), legs)
        detail.update(parallel=width, capacity=cap.model_dump())
        if cap.busy:
            if may_wait:
                detail.update(reason="fleet busy", deferred=True, retry_at=_iso(now + RETRY))
                return self.record(project, "skipped", detail)
            detail["reason"] = "fleet busy at the end of the window: started at the minimum"
        else:
            detail["reason"] = f"gaps on {legs} distro and arch legs" if legs else "scheduled"
        return await self.dispatch(project, width, detail, may_wait, now)

    def failed_recently(
        self, project: Project, found: list[dict[str, Any]], now: datetime.datetime
    ) -> dict[str, Any] | None:
        """Why not to dispatch wheelie again: the last run for these same gaps failed less than a day ago. A wheel
        that does not build would otherwise be rebuilt every hour; the failure was reported when the run ended."""
        last = last_live_dispatch(self.model, project)
        if last is None or last.info.get("gaps") != found or now - _at(last.at) >= DAY:
            return None
        run = self.model.run_after(
            self.full(project), project.ref, "workflow_dispatch", last.at, int(PENDING.total_seconds())
        )
        if run is None or run.status != "completed" or run.conclusion not in FAILED:
            return None
        return {"reason": "the same gaps failed to build: tried again daily", "run_id": run.id, "run_url": run.html_url}

    async def dispatch(
        self, project: Project, width: int, detail: dict[str, Any], may_wait: bool, now: datetime.datetime
    ) -> Decision:
        inputs = {"parallel": str(width)}
        detail["inputs"] = inputs
        if not detail["live"] or self.scheduler.dry_run:
            detail["dry_run"] = True
            return self.record(project, "dispatch", detail)
        if self.github is None:
            detail.update(stage="dispatch", message="GitHub App not configured or not installed")
            return await self.error(project, detail, may_wait, now)
        try:
            await self.github.post(
                f"/repos/{self.full(project)}/actions/workflows/{project.workflow}/dispatches",
                {"ref": project.ref, "inputs": inputs},
            )
        except httpx.HTTPError as exc:
            detail.update(stage="dispatch", message=describe(exc))
            return await self.error(project, detail, may_wait, now)
        return self.record(project, "dispatch", detail)

    async def error(self, project: Project, detail: dict[str, Any], may_wait: bool, now: datetime.datetime) -> Decision:
        if may_wait:
            detail["retry_at"] = _iso(now + RETRY)
        decision = self.record(project, "error", detail)
        if self.scheduler.dry_run:
            return decision
        streak = self.model.consecutive_errors(project.repo, project.ref, KIND_BATCH)
        if streak == (GAP_ALERT_AFTER if detail.get("stage") == "gap check" else 1):
            if detail.get("stage") == "gap check":
                headline, text = "gap check failing", f"The gap check failed {streak} times in a row"
            else:
                headline, text = "dispatch failed", f"Control could not start `{project.workflow}` on `{project.ref}`"
            text += f":\n`{detail.get('message')}`"
            if detail.get("retry_at"):
                text += "\nIt tries again within the slot's window; this message is not repeated while it fails."
            await self.post(self.embed(project, headline, text, events.RED))
        return decision

    async def find_gaps(self, project: Project) -> list[dict[str, Any]]:
        """wheelie's gap check over HTTP: distros.json, packages.txt and the index pages from the repo at its ref
        (what the Build workflow's own gap check reads from its checkout), the releases from PyPI."""
        if self.github is None:
            raise RuntimeError("GitHub App not configured or not installed")
        base = f"/repos/{self.full(project)}/contents"
        params = {"ref": project.ref}
        config = json.loads(await self.github.get_text(f"{base}/distros.json", params))
        listed = await self.github.get_text(f"{base}/packages.txt", params)
        packages = [line.split("#", 1)[0].strip() for line in listed.splitlines() if line.split("#", 1)[0].strip()]
        pages: dict[str, str | None] = {}
        for index in sorted({str(d["index"]) for d in config["distros"].values()}):
            try:
                pages[index] = await self.github.get_text(f"{base}/docs/{index}/index.html", params)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
                pages[index] = None
        found = await asyncio.wait_for(
            asyncio.to_thread(gaps.find_gaps, config, packages, pages.get, self.pypi_fetch), GAP_TIMEOUT
        )
        return [dict(gap) for gap in found]

    # -- watching ---------------------------------------------------------------------------------------------------

    async def watch(self) -> None:
        now = self.now()
        since = self.state.db.get(WATCH_SINCE)
        if since is None:
            since = _iso(now)
            self.state.db.set(WATCH_SINCE, since)
        for project in PROJECTS:
            try:
                await self.evaluate_runs(project, now, since)
                self.retire_units(project)
                await self.watch_dispatch(project, now)
                await self.watch_active(project, now)
            except Exception:
                log.exception("watching %s failed", project.repo)

    async def evaluate_runs(self, project: Project, now: datetime.datetime, since: str) -> None:
        for run in self.model.completed_runs(self.full(project), project.workflows, _iso(now - LOOKBACK)):
            if self.model.batch_run_seen(run.id, run.attempt):
                continue
            jobs = self.model.jobs_for_run(run.id)
            if (not jobs or any(j.status != "completed" for j in jobs)) and now - _at(run.updated_at) < SETTLE:
                continue
            await self.evaluate(project, run, jobs, announce=_at(run.updated_at) >= _at(since))

    async def evaluate(self, project: Project, run: Run, jobs: list[Job], announce: bool = True) -> BatchRun:
        """Move each unit of a finished run to pass or fail and tell Discord about the changes. A re-run keeps the
        run id, so the newest job of each name wins; cancelled and skipped jobs change nothing."""
        now = self.now()
        repo = self.full(project)
        workflow = run.workflow_name or project.workflow
        latest: dict[str, Job] = {}
        for job in jobs:
            latest[job.name] = job
        outcomes: dict[str, tuple[str, str | None]] = {}
        for name, job in latest.items():
            if job.conclusion in FAILED:
                outcomes[name] = ("fail", job.html_url)
            elif job.conclusion == "success":
                outcomes[name] = ("pass", job.html_url)
        if run.conclusion in FAILED and not any(state == "fail" for state, _ in outcomes.values()):
            outcomes[RUN_UNIT] = ("fail", run.html_url)
        elif run.conclusion in FAILED or run.conclusion == "success":
            outcomes[RUN_UNIT] = ("pass", run.html_url)

        failed: list[str] = []
        newly: list[str] = []
        recovered: list[str] = []
        for unit, (state, url) in sorted(outcomes.items()):
            if state == "fail":
                failed.append(unit)
            previous = self.model.batch_unit(repo, workflow, unit)
            if previous and _at(previous.run_created_at) > _at(run.created_at):
                continue  # a newer run already set it
            if state == "fail" and (previous is None or previous.state != "fail"):
                newly.append(unit)
            if state == "pass" and previous is not None and previous.state == "fail":
                recovered.append(unit)
            self.model.upsert_batch_unit(
                BatchUnit(
                    repo=repo,
                    workflow=workflow,
                    unit=unit,
                    state=state,
                    since=previous.since if previous and previous.state == state else run.updated_at,
                    run_id=run.id,
                    run_created_at=run.created_at,
                    html_url=url,
                    updated_at=_iso(now),
                )
            )
        if project.shared_units:
            recovered += self.clear_elsewhere(project, run, workflow, outcomes)
        alerted = False
        if announce and (newly or recovered):
            embeds = []
            if newly:
                embeds.append(self.failure_embed(project, run, newly, failed, latest))
            if recovered:
                embeds.append(self.recovery_embed(project, run, recovered))
            alerted = await self.state.discord.post(embeds=embeds) is not None
        record = BatchRun(
            run_id=run.id,
            attempt=run.attempt,
            repo=repo,
            workflow=workflow,
            conclusion=run.conclusion,
            html_url=run.html_url,
            completed_at=run.updated_at,
            failed=failed,
            newly_failing=newly,
            recovered=recovered,
            alerted=alerted,
            evaluated_at=_iso(now),
        )
        self.model.add_batch_run(record)
        self.state.stream.publish("batch_runs", [repo, run.id])
        return record

    def retire_units(self, project: Project) -> list[str]:
        """Drop the failures of units their workflow's runs stopped building (RETIRE_AFTER). Returns them."""
        repo = self.full(project)
        newest: dict[str, datetime.datetime] = {}
        for record in self.model.batch_runs(repo, 50):
            if record.workflow is None or record.completed_at is None:
                continue
            at = _at(record.completed_at)
            newest[record.workflow] = max(newest.get(record.workflow, at), at)
        retired: list[str] = []
        for unit in self.model.batch_units(repo, "fail"):
            latest = newest.get(unit.workflow)
            if latest is not None and latest - _at(unit.run_created_at) > RETIRE_AFTER:
                self.model.delete_batch_unit(repo, unit.workflow, unit.unit)
                retired.append(unit.unit)
        if retired:
            log.info("%s: retired failing units no run builds any more: %s", project.repo, ", ".join(retired))
            self.state.stream.publish("batch_runs", [repo, 0])
        return retired

    def clear_elsewhere(
        self, project: Project, run: Run, workflow: str, outcomes: dict[str, tuple[str, str | None]]
    ) -> list[str]:
        """Units this run passed that an older run of another of the project's workflows failed: fixed there too."""
        repo = self.full(project)
        cleared: list[str] = []
        for unit, (state, url) in sorted(outcomes.items()):
            if state != "pass" or unit == RUN_UNIT:
                continue
            for other in project.workflows:
                previous = self.model.batch_unit(repo, other, unit) if other != workflow else None
                if previous is None or previous.state != "fail" or _at(previous.run_created_at) > _at(run.created_at):
                    continue
                self.model.upsert_batch_unit(
                    previous.model_copy(
                        update={
                            "state": "pass",
                            "since": run.updated_at,
                            "run_id": run.id,
                            "run_created_at": run.created_at,
                            "html_url": url,
                            "updated_at": _iso(self.now()),
                        }
                    )
                )
                if unit not in cleared:
                    cleared.append(unit)
        return cleared

    async def watch_dispatch(self, project: Project, now: datetime.datetime) -> None:
        """A dispatch GitHub accepted but whose run never appeared: reported once."""
        last = last_live_dispatch(self.model, project)
        if last is None or not PENDING < now - _at(last.at) < LOOKBACK:
            return
        key = f"batch_alerted:dispatch:{last.id}"
        if self.state.db.get(key):
            return
        if self.model.run_after(
            self.full(project), project.ref, "workflow_dispatch", last.at, int(PENDING.total_seconds())
        ):
            return
        self.state.db.set(key, _iso(now))
        text = (
            f"`{project.workflow}` was dispatched on `{project.ref}` {last.at} (parallel {last.info.get('parallel')}), "
            f"and no run appeared within {int(PENDING.total_seconds() // 60)} minutes."
        )
        await self.post(self.embed(project, "dispatched run never appeared", text, events.RED))

    async def watch_active(self, project: Project, now: datetime.datetime) -> None:
        """Runs that sit queued, or run far past their usual length: reported once per run attempt."""
        for run in active_runs(self.model, self.state.settings.github_org, project, now):
            if run.status != "in_progress":
                age = now - _at(run.created_at)
                if age <= NOT_STARTED:
                    continue
                key = f"batch_alerted:queued:{run.id}:{run.attempt}"
                headline = "run not started"
                text = (
                    f"[{run.workflow_name} #{run.run_number}]({run.html_url}) has been {run.status} for {_hours(age)}."
                )
            else:
                age = now - _at(run.started_at or run.created_at)
                limit = overdue_after(self.model.run_durations(run.repo, str(run.workflow_name)))
                if age <= limit:
                    continue
                key = f"batch_alerted:overdue:{run.id}:{run.attempt}"
                headline = "run overdue"
                text = (
                    f"[{run.workflow_name} #{run.run_number}]({run.html_url}) has run for {_hours(age)}, "
                    f"past the {_hours(limit)} its recent runs make overdue."
                )
            if self.state.db.get(key):
                continue
            self.state.db.set(key, _iso(now))
            await self.post(self.embed(project, headline, text, events.AMBER, run.html_url))

    # -- Discord ----------------------------------------------------------------------------------------------------

    async def post(self, embed: dict[str, Any]) -> None:
        if not self.scheduler.dry_run:
            await self.state.discord.post(embeds=[embed])

    def embed(
        self,
        project: Project,
        headline: str,
        description: str,
        color: int,
        url: str | None = None,
        fields: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """A batch embed: `<repo> · <dot> <headline>`, linking the run (or control's batch page)."""
        settings = self.state.settings
        repo_url = f"https://github.com/{self.full(project)}"
        links = f"[repository]({repo_url}) · [control]({settings.public_url}/batch)"
        return events.within_limits(
            {
                "author": {"name": project.repo, "url": repo_url},
                "title": f"{project.repo} · {events.DOT[color]} {headline}",
                "url": url or f"{settings.public_url}/batch",
                "color": color,
                "description": description,
                "fields": [*(fields or []), {"name": "Links", "value": links, "inline": False}],
                "footer": {"text": f"batch · {project.repo}"},
                "timestamp": _iso(self.now()),
            }
        )

    def packages_by_leg(self, project: Project, run: Run) -> dict[str, str]:
        """wheelie: `<distro> <arch>` (its job name) to the packages the dispatching gap check found for it."""
        if not project.gaps:
            return {}
        for decision in self.model.recent_decisions(50, project.repo, "dispatch", KIND_BATCH):
            if _at(decision.at) <= _at(run.created_at) and isinstance(decision.info.get("gaps"), list):
                return {
                    f"{g.get('distro')} {g.get('arch')}": ", ".join(str(p) for p in g.get("packages") or [])
                    for g in decision.info["gaps"]
                    if isinstance(g, dict)
                }
        return {}

    def unit_line(self, unit: str, url: str | None, workflow: str, packages: dict[str, str]) -> str:
        name = f"{workflow} run" if unit == RUN_UNIT else unit
        line = f"[`{name}`]({url})" if url else f"`{name}`"
        if packages.get(unit):
            line += f" ({packages[unit]})"
        return f"- {line}"

    def failure_embed(
        self, project: Project, run: Run, newly: list[str], failed: list[str], jobs: dict[str, Job]
    ) -> dict[str, Any]:
        workflow = str(run.workflow_name)
        packages = self.packages_by_leg(project, run)
        still = len(failed) - len(newly)
        text = f"[{workflow} #{run.run_number}]({run.html_url}) {run.conclusion}: {len(newly)} newly failing"
        text += f", {still} still failing from before (not repeated).\n" if still else ".\n"
        text += "\n".join(
            self.unit_line(u, jobs[u].html_url if u in jobs else run.html_url, workflow, packages) for u in newly
        )
        headline = (
            f"{len(newly)} failing" if len(newly) > 1 else f"{newly[0] if newly[0] != RUN_UNIT else 'run'} failing"
        )
        return self.embed(project, headline, text, events.RED, run.html_url)

    def recovery_embed(self, project: Project, run: Run, recovered: list[str]) -> dict[str, Any]:
        workflow = str(run.workflow_name)
        text = f"Passing again in [{workflow} #{run.run_number}]({run.html_url}):\n"
        text += "\n".join(self.unit_line(u, None, workflow, {}) for u in recovered)
        return self.embed(project, "recovered", text, events.GREEN, run.html_url)


def _hours(delta: datetime.timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    return f"{minutes // 60}h{minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"
