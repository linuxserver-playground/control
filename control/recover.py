"""
Self-healing: GitHub loses webhooks, answers 500s, drops runners and flakes on clones and registry calls now and
then, and nobody should babysit that. heal.py repairs the jobs whose completion webhook never came; this module
repeats the work that failed for reasons outside the image, so people hear only about what stays broken. It runs in
the scheduler's minute loop (`tick`), so `CONTROL_SCHEDULER=false` stops it with every other dispatch.

**Pages.** Each image repo's Pages site is deployed from its Releases, statelessly: at the end of a live Publish (its
`Pages (live)` step) or alone, in a `reason: pages` run whose one job is `Pages`. A failed deploy leaves the
previous site up, stale, and repeating one is always safe. When a deploy fails (a `Pages` job that failed, a live
Publish whose `Pages (live)` step failed, or a `build.summary` whose PAGES_FIELD says so), control dispatches
`reason: pages` on the repo's default branch after `heal_pages_backoff_minutes` (5 min, 30 min, 2 h, then daily),
one row per repo so at most one retry is ever pending, and clears it at the first deploy that succeeds, whoever
started it. After `heal_pages_attempts` retries without one it gives up and reports. A repo without a Pages site
(pages.py reports those) is never retried.

**Re-runs.** A live build run (push or dispatch on a live branch, first attempt) that failed is judged once from its
jobs: every failed job's failing step must be plain infrastructure (INFRA_STEPS: checkout, runner setup, registry
login, artifacts, publishing tags already built and tested, the release upload), not the image build, the tests or
a check. Then control re-runs its failed jobs once (`rerun-failed-jobs`), `heal_rerun_delay_minutes` after it
failed, while it is still the branch's newest build and nothing else runs on the branch. Pull request runs are
never touched, so neither are forks. A run is never re-run twice, by control at least: one `heal_reruns` row each.

**Capacity.** Both wait for free runner slots the way batch runs are sized (batch.capacity: the org's limit less
`batch_reserve`, the jobs running and queued and the batch runs' widths), so a retry never takes a slot an image,
hub or site build is waiting for; at most `heal_per_tick` actions start a minute.

**Reporting.** Every action is a log line (`heal:`) and an `actions` row by `control` (on /management); a Pages
dispatch is also a scheduler decision of kind `heal`. Give-ups (Pages never deployed again, a re-run that failed
again, a run the fleet stayed too busy to re-run) go to Discord grouped, at most once an hour, as the run archive's
do (`report`).
"""

from __future__ import annotations

import datetime
import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from . import batch
from .model import PAGES_REASON, Job, PagesHeal, RunHeal, image_name_of, is_pages_job

if TYPE_CHECKING:
    from .github import GitHub
    from .state import State

log = logging.getLogger(__name__)

KIND_HEAL = "heal"
"""The scheduler decisions of control's own Pages retries."""
ACTOR = "control"
"""Who the `actions` rows of self-healing name."""

PAGES_FIELD = "pages"
"""The `build.summary` field with the live Publish's Pages outcome (`deployed`, `failed`, `skipped`). Optional:
events without it leave the outcome to the jobs. Keep in step with builder's control-event schema."""
PAGES_DEPLOYED = "deployed"
PAGES_FAILED = "failed"
PAGES_LIVE_STEP = "Pages (live)"
"""The live Publish's deploy step. It is `continue-on-error`, so only a step reported failed counts; its success
is not read as a deploy, which the summary's PAGES_FIELD or the next `Pages` job says."""
PUBLISH_JOB = "Publish"
PAGES_SCAN = datetime.timedelta(hours=6)
"""Deploys that finished this long ago are still read each minute; each is taken into account once."""

BUILD_EVENTS = ("push", "workflow_dispatch", "repository_dispatch", "schedule")
"""Runs that are live or scheduled builds once their branch is live. Pull request runs never are."""
JUDGE_PER_TICK = 10
"""Failed runs whose jobs control reads per minute at most."""
RERUN_LOST = datetime.timedelta(hours=24)
"""A re-run whose outcome never arrived (a lost webhook) is dropped silently after this."""

INFRA_STEPS: dict[str, str] = {
    "Set up job": "GitHub assigning the runner and downloading the actions",
    "Complete job": "the runner's own teardown",
    "Check out repository": "a git clone from GitHub",
    "Check out shared workflows": "a git clone from GitHub",
    "Check out repository (package check)": "a git clone from GitHub",
    "Free disk space": "deleting the runner's preinstalled toolchains, best effort",
    "Set up buildx": "downloading and starting BuildKit",
    "Log in to GHCR": "the registry login",
    "Upload manifest": "the artifact service",
    "Download manifest": "the artifact service",
    "Upload bundle files": "the artifact service",
    "Upload image (not pushed)": "the artifact service",
    "Download bundle files": "the artifact service",
    "Upload bundle": "the artifact service",
    "Next build number": "reading the repo's Releases and tags from GitHub",
    "Publish tags": "registry calls that tag images already built and tested; repeating them is idempotent",
    "Release (live)": "the GitHub Release; a re-run finds it and re-uploads the assets with --clobber",
    "Notify control plane": "the POST to /events, which control de-duplicates",
}
"""Top-level steps of workflows' image-build.yml whose failure is GitHub's or the network's, never the image's: their
inputs were made and checked by earlier steps, and repeating them changes nothing that is already right. The jobs API
only names top-level steps, so a composite that mixes a download with a check (`Read manifest`, `Emit build.json`:
pip installs and validation) is left out, as are `Build`, `Test`, `Fail on test failure`, `Resolve upstream
version`, the generated-file and shellcheck steps and every other step: those fail again on a re-run or are the
image's own problem. `Post <step>` (an action's cleanup) counts like its step."""
RUNNER_LOST = "the runner"
"""What a failed job with no failed step failed in: the runner was lost or never started."""
CONSEQUENCE_STEPS = frozenset({"Fail if any job failed"})
"""Publish's last step, failing because another job failed; that job decides."""

REPORT_EVERY = datetime.timedelta(hours=1)
REPORTED_AT = "heal_reported_at"
"""kv key: when the last give-up summary was posted (or tried)."""
REPORT_SHOWN = 5


def _iso(value: datetime.datetime) -> str:
    return value.astimezone(datetime.UTC).isoformat(timespec="seconds")


def _at(value: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(value)


def _plural(count: int, word: str, many: str | None = None) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {many or word + 's'}"


def _most_common(values: list[str]) -> str:
    return Counter(values).most_common(1)[0][0] if values else "unknown"


def _log_action(state: State, action: str, target: str, result: str, detail: str) -> None:
    action_id = state.model.add_action(ACTOR, action, target, result, detail)
    state.stream.publish("actions", action_id)


# -- judging a failed run -------------------------------------------------------------------------------------------


def is_infra_step(name: str) -> bool:
    return name in INFRA_STEPS or (name.startswith("Post ") and name.removeprefix("Post ") in INFRA_STEPS)


def failing_step(job: Job) -> str | None:
    """The step a failed job failed in: the first that failed, else the first cancelled (a runner lost mid-step,
    a timeout), else None."""
    steps = sorted(job.steps, key=lambda s: s.number if s.number is not None else 0)
    for conclusion in ("failure", "cancelled"):
        for step in steps:
            if step.conclusion == conclusion:
                return step.name
    return None


@dataclass
class Verdict:
    infra: bool
    step: str | None
    """`<job>: <step>`, the step that decided it."""
    note: str
    slots: int


def judge(jobs: list[Job]) -> Verdict:
    """Whether a failed run failed only in infrastructure. Every failed job must have failed in an INFRA_STEPS step
    (or lost its runner); Publish's `Fail if any job failed` follows another job's failure and defers to it. Any
    other failing step, or no failed job at all, means no re-run."""
    builds = [job for job in jobs if not is_pages_job(job.name)]
    failed = [job for job in builds if job.conclusion == "failure"]
    slots = max(1, sum(1 for job in builds if job.conclusion != "success"))
    if not failed:
        return Verdict(False, None, "no failed job", slots)
    infra: list[tuple[str, str]] = []
    for job in failed:
        step = failing_step(job)
        if step is None:
            infra.append((f"{job.name}: {RUNNER_LOST}", "the runner was lost or never started"))
        elif step in CONSEQUENCE_STEPS and len(failed) > 1:
            continue
        elif is_infra_step(step):
            infra.append((f"{job.name}: {step}", INFRA_STEPS[step.removeprefix("Post ")]))
        else:
            return Verdict(False, f"{job.name}: {step}", f"{step} failed, which is not infrastructure", slots)
    if not infra:
        return Verdict(False, None, "no failing step to judge", slots)
    step, why = infra[0]
    return Verdict(True, step, f"{step.split(': ', 1)[1]} failed ({why})", slots)


# -- Pages ----------------------------------------------------------------------------------------------------------


def _backoff(state: State, attempts: int) -> datetime.timedelta:
    steps = state.settings.heal_pages_backoff_minutes
    return datetime.timedelta(minutes=steps[min(attempts, len(steps) - 1)])


def _give_up_pages(state: State, row: PagesHeal, why: str, now: str, reported: bool = False) -> None:
    note = f"{row.note or 'the deploy failed'}; {why}"
    state.model.upsert_pages_heal(
        row.model_copy(
            update={"state": "gave-up", "next_attempt_at": None, "note": note, "updated_at": now}
            | ({"reported_at": now} if reported else {})
        )
    )
    log.error("heal: gave up on the Pages site of %s: %s", row.repo, note)


def observe_pages(state: State, repo: str, ok: bool, at: str, run_id: int | None, note: str | None, now: str) -> None:
    """Take one finished deploy into account, once: a success clears the repo, a failure starts its retries (or,
    after the last retry was dispatched, gives up)."""
    if not state.settings.heal_pages:
        return
    row = state.model.pages_heal(repo)
    if row is not None and row.last_outcome_at and at <= row.last_outcome_at:
        return
    if ok:
        if row is None:
            return
        if row.state != "ok":
            log.info("heal: the Pages site of %s deployed again (%s retries by control)", repo, row.attempts)
        state.model.upsert_pages_heal(
            PagesHeal(
                repo=repo, state="ok", last_outcome_at=at, run_id=run_id, created_at=row.created_at, updated_at=now
            )
        )
        return
    if row is None or row.state == "ok":
        wait = _backoff(state, 0)
        state.model.upsert_pages_heal(
            PagesHeal(
                repo=repo,
                state="retrying",
                failed_since=at,
                last_outcome_at=at,
                next_attempt_at=_iso(_at(at) + wait),
                run_id=run_id,
                note=note,
                created_at=row.created_at if row else now,
                updated_at=now,
            )
        )
        log.warning("heal: the Pages deploy of %s failed (%s); control redeploys it in %s", repo, note, wait)
        return
    row = row.model_copy(update={"last_outcome_at": at, "run_id": run_id, "note": note or row.note, "updated_at": now})
    last_retried = row.state == "retrying" and row.attempts >= state.settings.heal_pages_attempts
    if last_retried and row.last_dispatch_at and at >= row.last_dispatch_at:
        _give_up_pages(state, row, f"still failing after {_plural(row.attempts, 'retry', 'retries')}", now)
        return
    state.model.upsert_pages_heal(row)


def pages_outcome(job: Job) -> tuple[bool, str | None] | None:
    """(succeeded, why not) for a job that deployed Pages, or None for one that says nothing about it."""
    if is_pages_job(job.name):
        if job.conclusion == "success":
            return True, None
        step = failing_step(job)
        return False, f"{step} failed" if step else "the Pages job failed"
    if job.name == PUBLISH_JOB or job.name.endswith(f" / {PUBLISH_JOB}"):
        for live in job.steps:
            if live.name == PAGES_LIVE_STEP and live.conclusion == "failure":
                return False, f"{PAGES_LIVE_STEP} failed in the live Publish"
    return None


def scan_pages(state: State, now: datetime.datetime) -> None:
    """Read every deploy that finished lately, oldest first, into the rows."""
    for job in state.model.pages_outcomes(_iso(now - PAGES_SCAN)):
        outcome = pages_outcome(job)
        if outcome is not None and job.completed_at:
            observe_pages(state, job.repo, outcome[0], job.completed_at, job.run_id, outcome[1], _iso(now))


def observe_summary(state: State, payload: dict[str, Any], now: Callable[[], datetime.datetime] | None = None) -> None:
    """From /events: a live build's summary that reports its Pages deploy (PAGES_FIELD). Never raises."""
    try:
        outcome = payload.get(PAGES_FIELD)
        if payload.get("mode") != "live" or outcome not in (PAGES_DEPLOYED, PAGES_FAILED):
            return
        at = _iso((now or (lambda: datetime.datetime.now(datetime.UTC)))())
        note = "the live Publish reported the deploy failed" if outcome == PAGES_FAILED else None
        observe_pages(
            state, str(payload["repository"]), outcome == PAGES_DEPLOYED, at, int(payload["run_id"]), note, at
        )
    except Exception:
        log.exception("heal: reading the Pages outcome of a build summary failed")


def _busy_repo(state: State, repo: str, now: datetime.datetime) -> bool:
    """A run of the repo going that is not a pull request's: a Pages deploy (control's last retry, say) or a build
    whose Publish deploys anyway. A run 'going' for days lost its completion webhook and holds nothing."""
    return any(
        r.repo == repo
        and r.event not in ("pull_request", "pull_request_target")
        and now - _at(r.created_at) < batch.STALE_RUN
        for r in state.model.active_runs()
    )


async def _retry_pages(state: State, github: GitHub, row: PagesHeal, now: datetime.datetime) -> bool:
    """Dispatch the next retry of a due row, or say why not. True when a dispatch was sent (or tried)."""
    from .scheduler import Scheduler  # the scheduler imports this module

    at = _iso(now)
    settings = state.settings
    if row.attempts >= settings.heal_pages_attempts:
        _give_up_pages(state, row, f"no deploy succeeded after {_plural(row.attempts, 'retry', 'retries')}", at)
        return False
    repo = state.model.repo(row.repo)
    if repo is None or repo.archived or not repo.is_image:
        state.model.upsert_pages_heal(row.model_copy(update={"state": "ok", "next_attempt_at": None, "updated_at": at}))
        log.info("heal: %s is gone or archived; its Pages retries stop", row.repo)
        return False
    if row.repo in state.model.pages_missing():
        _give_up_pages(state, row, "the repo has no Pages site (reported on its own)", at, reported=True)
        return False
    branch = repo.default_branch
    image = image_name_of(repo.name) or repo.name
    if (manifest := state.model.manifest(row.repo, branch)) is not None:
        image = manifest.image
    attempt = row.attempts + 1
    detail = {
        "reason": f"self-healing: the Pages deploy failed ({row.note or 'no reason given'})",
        "attempt": attempt,
        "attempts": settings.heal_pages_attempts,
        "failed_run_id": row.run_id,
    }
    decision = await Scheduler(state, github).dispatch(row.repo, branch, image, KIND_HEAL, PAGES_REASON, detail)
    ok = decision.outcome == "dispatch"
    wait = _backoff(state, attempt)
    state.model.upsert_pages_heal(
        row.model_copy(
            update={
                "attempts": attempt,
                "last_dispatch_at": at,
                "next_attempt_at": _iso(now + wait),
                "updated_at": at,
            }
        )
    )
    text = (
        f"Pages redeploy {attempt} of {settings.heal_pages_attempts} after a failed deploy ({row.note or 'no reason'})"
        + ("" if ok else f": {decision.detail.get('message')}")
    )
    _log_action(state, "dispatch", f"{row.repo}@{branch} ({PAGES_REASON})", "ok" if ok else "error", text)
    log.info("heal: %s on %s; next look in %s", text, row.repo, wait)
    return True


# -- re-runs --------------------------------------------------------------------------------------------------------


async def judge_new_failures(state: State, github: GitHub, now: datetime.datetime) -> None:
    """Judge the live build runs that failed since the window opened and settled for the delay. Each gets one row."""
    settings = state.settings
    since = _iso(now - datetime.timedelta(hours=settings.heal_rerun_window_hours))
    delay = datetime.timedelta(minutes=settings.heal_rerun_delay_minutes)
    at = _iso(now)
    for run in state.model.failed_build_runs(since, _iso(now - delay), BUILD_EVENTS)[:JUDGE_PER_TICK]:
        try:
            data = await github.paginate(
                f"/repos/{run.repo}/actions/runs/{run.id}/jobs", {"per_page": 100}, key="jobs", limit=200
            )
        except httpx.HTTPError as exc:
            log.warning(
                "heal: reading the jobs of %s run %s failed (%s); tried again next minute", run.repo, run.id, exc
            )
            continue
        jobs = [Job.from_github(run.repo, item) for item in data if isinstance(item, dict)]
        for job in jobs:
            if state.model.upsert_job(job):
                state.stream.publish("jobs", job.id)
        verdict = judge(jobs)
        state.model.upsert_run_heal(
            RunHeal(
                repo=run.repo,
                run_id=run.id,
                branch=run.branch,
                attempt=run.attempt,
                state="pending" if verdict.infra else "declined",
                step=verdict.step,
                note=verdict.note,
                slots=verdict.slots,
                not_before=at,
                created_at=at,
                updated_at=at,
            )
        )
        if verdict.infra:
            log.info(
                "heal: %s run %s failed in infrastructure (%s); re-running it once", run.repo, run.id, verdict.note
            )
        else:
            log.info("heal: %s run %s is not re-run: %s (%s)", run.repo, run.id, verdict.note, verdict.step or "-")


def _settle_run(state: State, row: RunHeal, new_state: str, note: str, at: str) -> None:
    state.model.upsert_run_heal(row.model_copy(update={"state": new_state, "note": note, "updated_at": at}))
    level = logging.WARNING if new_state in ("failed", "gave-up") else logging.INFO
    log.log(level, "heal: %s run %s %s: %s", row.repo, row.run_id, new_state, note)


async def _rerun(state: State, github: GitHub, row: RunHeal, free: int, now: datetime.datetime) -> int:
    """Re-run a pending row's failed jobs if it is still worth it and fits. Returns the slots it took (0: none)."""
    at = _iso(now)
    run = state.model.run(row.run_id)
    window = datetime.timedelta(hours=state.settings.heal_rerun_window_hours)
    if run is None:
        _settle_run(state, row, "superseded", "the run is gone", at)
        return 0
    if run.attempt > row.attempt or run.status != "completed":
        _settle_run(state, row, "superseded", "someone re-ran it first", at)
        return 0
    newest = state.model.newest_branch_build(row.repo, row.branch or "")
    if newest is not None and newest.id != run.id:
        _settle_run(state, row, "superseded", f"run {newest.id} built the branch since", at)
        return 0
    if now - _at(run.updated_at) > window:
        _settle_run(state, row, "gave-up", f"{row.note}; the fleet had no free slot for {window}", at)
        return 0
    manifest = state.model.manifest(row.repo, row.branch or "")
    image = manifest.image if manifest else image_name_of(row.repo.rsplit("/", 1)[-1]) or row.repo
    if skips := state.model.skips_for(image, row.branch or ""):
        _settle_run(state, row, "declined", f"held back by the skip flag {skips[0].scope}", at)
        return 0
    if free < row.slots:
        return 0
    try:
        await github.post(f"/repos/{row.repo}/actions/runs/{row.run_id}/rerun-failed-jobs")
    except httpx.HTTPStatusError as exc:
        note = f"{row.note}; the re-run was refused (GitHub answered {exc.response.status_code})"
        _settle_run(state, row, "gave-up", note, at)
        _log_action(state, "rerun", str(row.run_id), "error", f"self-healing re-run of {row.repo}: {note}")
        return 0
    except httpx.HTTPError as exc:
        log.warning("heal: re-running %s run %s failed (%s); tried again next minute", row.repo, row.run_id, exc)
        return 0
    state.model.upsert_run_heal(row.model_copy(update={"state": "rerun", "requested_at": at, "updated_at": at}))
    text = f"self-healing re-run of the failed jobs of {row.repo} {row.branch}: {row.note}"
    _log_action(state, "rerun", str(row.run_id), "ok", text)
    log.info("heal: %s (run %s)", text, row.run_id)
    return row.slots


def follow_reruns(state: State, now: datetime.datetime) -> None:
    """Record what each requested re-run came to: passed, failed again (reported) or overtaken."""
    at = _iso(now)
    for row in state.model.run_heals("rerun"):
        run = state.model.run(row.run_id)
        if run is not None and run.attempt > row.attempt and run.status == "completed":
            if run.conclusion == "success":
                _settle_run(state, row, "passed", f"{row.note}; the re-run passed", at)
            elif run.conclusion == "cancelled":
                _settle_run(state, row, "superseded", f"{row.note}; the re-run was cancelled", at)
            else:
                _settle_run(state, row, "failed", f"{row.note}; failed again after the re-run", at)
        elif row.requested_at and now - _at(row.requested_at) > RERUN_LOST:
            _settle_run(state, row, "superseded", f"{row.note}; no outcome of the re-run arrived", at)


# -- the Discord summary --------------------------------------------------------------------------------------------


def _repo_links(rows: list[PagesHeal]) -> str:
    named = [
        f"[{row.repo.rsplit('/', 1)[-1]}](<https://github.com/{row.repo}/actions"
        + (f"/runs/{row.run_id}>)" if row.run_id else ">)")
        for row in rows[:REPORT_SHOWN]
    ]
    more = len(rows) - REPORT_SHOWN
    return ", ".join(named) + (f" and {more} more" if more > 0 else "")


def _run_links(rows: list[RunHeal]) -> str:
    named = [
        f"[{row.repo.rsplit('/', 1)[-1]} {row.branch}](<https://github.com/{row.repo}/actions/runs/{row.run_id}>)"
        for row in rows[:REPORT_SHOWN]
    ]
    more = len(rows) - REPORT_SHOWN
    return ", ".join(named) + (f" and {more} more" if more > 0 else "")


def _step_of(row: RunHeal) -> str:
    return (row.step or "unknown").split(": ", 1)[-1]


def summary(pages: list[PagesHeal], runs: list[RunHeal]) -> str:
    """The grouped Discord post: what control could not heal. Reasons are control's own notes (a step name, an HTTP
    status), never log text."""
    parts: list[str] = []
    if pages:
        reasons = [(row.note or "unknown").split("; ", 1)[0] for row in pages]
        parts.append(
            f"⚠️ **Self-healing**: control gave up redeploying the Pages site of {_plural(len(pages), 'repo')}; "
            f"the previous site stays up, stale, until a deploy succeeds. Most common reason: "
            f"{_most_common(reasons)}. {_repo_links(pages)}"
        )
    again = [row for row in runs if row.state == "failed"]
    stuck = [row for row in runs if row.state != "failed"]
    if again:
        parts.append(
            f"⚠️ **Self-healing**: {_plural(len(again), 'live build')} failed again after control re-ran "
            f"{'its' if len(again) == 1 else 'their'} failed jobs once for an infrastructure failure; most common "
            f"step: {_most_common([_step_of(row) for row in again])}. {_run_links(again)}"
        )
    if stuck:
        reasons = [(row.note or "unknown").split("; ", 1)[-1] for row in stuck]
        parts.append(
            f"⚠️ **Self-healing**: control could not re-run {_plural(len(stuck), 'failed live build')}; most common "
            f"reason: {_most_common(reasons)}. {_run_links(stuck)}"
        )
    return "\n\n".join(parts) + "\n\nEach is in control's log (`heal:`); what control retried is on /management."


async def report(state: State, now: datetime.datetime) -> bool:
    """Post one summary of the give-ups since the last one, at most once per REPORT_EVERY (the first at once). True
    when one went out; a post Discord refuses leaves them for the next."""
    at = _iso(now)
    last = state.db.get(REPORTED_AT)
    if last and now - _at(last) < REPORT_EVERY:
        return False
    pages, runs = state.model.unreported_heals()
    if not pages and not runs:
        return False
    state.db.set(REPORTED_AT, at)
    posted = await state.discord.post(summary(pages, runs))
    if posted is None and state.discord.configured:
        log.warning(
            "heal: the summary of %s give-ups did not post; it is tried again in an hour", len(pages) + len(runs)
        )
        return False
    state.model.mark_heals_reported([row.repo for row in pages], [(row.repo, row.run_id) for row in runs], at)
    return True


# -- the minute loop ------------------------------------------------------------------------------------------------


async def tick(state: State, github: GitHub, now: datetime.datetime | None = None) -> int:
    """One pass from the scheduler loop: read new outcomes, start what is due within capacity and the per-minute
    budget, follow re-runs, report give-ups. Returns how many actions it started. Never raises."""
    now = now or datetime.datetime.now(datetime.UTC)
    settings = state.settings
    started = 0
    try:
        free = batch.capacity(state, now).free
        budget = settings.heal_per_tick
        if settings.heal_pages:
            scan_pages(state, now)
            for row in state.model.pages_heals("retrying"):
                if started >= budget:
                    break
                if row.next_attempt_at and row.next_attempt_at > _iso(now):
                    continue
                if row.attempts < settings.heal_pages_attempts and (_busy_repo(state, row.repo, now) or free < 1):
                    continue
                if await _retry_pages(state, github, row, now):
                    started += 1
                    free -= 1
        if settings.heal_reruns:
            follow_reruns(state, now)
            await judge_new_failures(state, github, now)
            for heal in state.model.run_heals("pending"):
                if started >= budget:
                    break
                if heal.not_before and _iso(now) < heal.not_before:
                    continue
                if taken := await _rerun(state, github, heal, free, now):
                    started += 1
                    free -= taken
        await report(state, now)
    except Exception:
        log.exception("heal: the self-healing pass failed")
    return started


def status(state: State) -> dict[str, Any]:
    """What self-healing is doing, for /api/status.json."""
    return {
        "pages_retrying": [
            row.model_dump(include={"repo", "attempts", "next_attempt_at", "note"})
            for row in state.model.pages_heals("retrying")
        ],
        "pages_given_up": [row.repo for row in state.model.pages_heals("gave-up")],
        "reruns_pending": [
            {"repo": row.repo, "run_id": row.run_id, "step": row.step} for row in state.model.run_heals("pending")
        ],
        "reruns_requested": [
            {"repo": row.repo, "run_id": row.run_id, "step": row.step} for row in state.model.run_heals("rerun")
        ],
    }
